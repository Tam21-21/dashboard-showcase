"""Newsbot v3 — analyst-upgrade and 13D-activist event equity strategy (M3.5 reference user, EL49).

The first real user of the M3.5 event-driven framework. It exists to exercise the machinery end
to end (the event pipeline, the triple-barrier labeling engine, the peer-event feature layer, the
2024-02-05 regime branch, the universe membership gate), not to make money — the same role
``ma_cross`` plays for the price-strategy contract.

## What it trades

Two equity event types the M2.5 store holds: ``sec_13d`` (Schedule 13D activist-stake filings) and
``analyst_rating`` (analyst rating changes). Both are read as bullish reactions, so the default trade
side is LONG (the side is the strategy's choice, AFML meta-labeling — NOT the triple-barrier label
sign, which is the realized outcome). A future short-side variant overrides ``trade_side``.

## How it runs

It does NOT override ``run``: the shared ``EventStrategy`` pipeline (membership gate -> features ->
signal filter -> triple-barrier label -> fill model -> regime tag -> daily mark-to-market assembly)
is the same for every event strategy. Newsbot v3 fills only the class attributes and the
``feature_defs`` hook. Its barrier widths, holding horizon, vol-estimate window, position sizing,
regime mode, and peer-feature window are per-strategy settings under the ``event_strategy`` scope, read by
the pipeline via ``get_setting`` (spec brief §12); each is a user-set dial with a neutral default and a
``parameter_provenance.md`` entry.

## The peer feature

It declares one peer-event feature, ``peer_upgrade_breadth``: for each trigger event, the fraction of
the run's active universe that had an ``analyst_rating`` event in the no-lookahead lookback window
before the trigger. The peer set is the run's active universe (the membership-kept companies the
pipeline passes as ``universe_ciks``) — the honest stand-in for "industry peers" until an
industry-classification source exists (the store has none today; the same flagged-stand-in pattern as
the today's-list universe, OPEN-1). The feature is computed and reported; gating trades on a breadth
threshold is a trivial future override (it would add one provenance'd threshold dial), not built here.

## Scope

Runs honestly against a sparse / synthetic store (M3.5 exit criterion 7): the analyst classifier and
real 13D ingest are upstream-gated, so the framework's correctness is demonstrated on synthetic events
and (until the Alpaca-SIP daily load, PC-1) synthetic price.
"""
# A strategy file imports at module top, like ma_cross.py: the import-light rule binds only the
# strategies.base CONTRACT surface, not the strategy files themselves. The backend dispatch path never
# imports this file (it only ``ast.parse``s the source to read ``event_types`` and the docstring), so
# these imports do not affect validate_strategy_source / extract_event_types.
from strategies.base.event_strategy import EventStrategy  # the shared event pipeline base (M3.5)
from strategies.base.features import FeatureDef  # the feature declaration the feature_defs hook returns (brief F)
from strategies.base.settings import get_setting  # the typed (scope, key) settings reader


class NewsbotV3(EventStrategy):
    """Long, analyst-upgrade and 13D-activist event equity strategy on the M3.5 framework."""

    # The event types this strategy consumes (strategy-intrinsic). A literal tuple of string literals so
    # the backend can read it by AST without importing the class (the validate_strategy_source contract,
    # via _event_types_from_tree, researchdb.py). The materializer fans out over exactly these types (brief I).
    event_types = ("sec_13d", "analyst_rating")  # the two M2.5 event types this strategy consumes

    # M4 D1/D7: an event strategy targets a universe, which is always equities (the universe invariant),
    # so its scope is ("equities",). Declaring it lets the dispatch guard refuse a crypto-dataset run.
    asset_class_scope = ("equities",)

    # The per-strategy settings scope the shared run body reads (EventStrategy.run, event_strategy.py): every barrier /
    # sizing / regime / peer dial lives under this scope in _DEFAULT_SETTINGS_LAYOUT and is provenance'd.
    settings_scope = "event_strategy"  # the settings scope the shared pipeline reads dials from (renamed from 'newsbot_v3', M4.5 #161 — a scope names a stable method, not a strategy)

    # LONG: analyst upgrades and 13D activist stakes are bullish reactions (OD-J1-4). The side is the
    # strategy's choice (the fill/sizing seam supports SHORT); it is NOT the triple-barrier label sign.
    trade_side = 1  # +1 LONG (event_pipeline.LONG)

    def feature_defs(self, settings, universe_ciks=()):
        """Declare the one peer-event feature, ``peer_upgrade_breadth`` (analyst-upgrade breadth).

        ``universe_ciks`` is the run's active universe (the membership-kept ``subject_cik`` set the
        pipeline derives from the materialized slices, BN-J2-1) — Newsbot v3 uses it as its peer set.
        On an empty active universe (e.g. an empty-universe run where neither slice was materialized)
        return no feature, so the empty-case run still lands a clean six-slot RunResult and
        ``provide_features`` is never handed an empty ``peer_ciks`` (which it rejects, features.py:274).
        """
        if not universe_ciks:  # empty active universe -> no peer feature (the empty-case run stays clean)
            return []  # provide_features([]) returns {}; the pipeline assembles a clean six-slot RunResult
        return [FeatureDef(
            name="peer_upgrade_breadth",  # the key this feature lands under in StrategyInput.features
            event_type="analyst_rating",  # breadth is measured over the analyst-rating slice
            peer_ciks=list(universe_ciks),  # the active universe IS the peer set (the flagged stand-in, OD-J2-1)
            window_before=int(get_setting(settings, self.settings_scope, "peer_window_before")),  # the lookback, in seconds (provenance'd); read via self.settings_scope so a scope rename touches one place
            window_after=0,  # fixed 0: inert under the no-lookahead cap (BN-F1; NOT a settings key, no provenance owed)
            missingness=get_setting(settings, self.settings_scope, "peer_missingness"),  # 'zero'/'null'/'exclude' (provenance'd); read via self.settings_scope
        )]

    # signal_filter is NOT overridden: the EventStrategy default (EventStrategy.signal_filter, event_strategy.py) trades every
    # surviving event. The peer feature is computed and reported (it reaches signal_filter via features);
    # a breadth-threshold filter is a documented future override, not built here (brief §1.2).
