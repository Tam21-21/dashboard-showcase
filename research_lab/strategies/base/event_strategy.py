"""``EventStrategy``: the shared base for every event-driven strategy (M3.5, EL47).

A ``Strategy`` subclass that carries the event pipeline shared by all event
strategies. Concrete strategies (Newsbot v3, EL49) subclass it and fill the two
hooks; they do not override ``run`` (the pipeline is the same for all of them).

The ``run`` signature is the locked contract ``run(data, settings) -> RunResult``
(``strategy.py:33``), unchanged from M3 — this subclass is exactly what that lock
was designed for (EL-c1, DECISIONS #12/#13). ``StrategyInput`` carries the event
slices, the per-symbol price context, and the (recompute-filled) features through
the three additive fields ``strategy_input.py`` gained at this brief (EL48).

Brief split (spec brief §18): brief D ships THIS scaffold — the class, the two
hooks, and the ``run`` signature with the ordered pipeline documented. Brief J1
wires ``run``'s body (the full pipeline, including the per-event membership gate,
EL47/EL78), because the body calls the labeling engine (brief E), the feature
layer (brief F), and the regime tag (brief H), and J1 is sequenced after all of
them. Until then ``run`` raises ``NotImplementedError`` the same way the abstract
``Strategy.run`` does — an importable, signature-correct scaffold, not a stub that
pretends to work.

Pure Python at import: this module imports only the ``Strategy`` base (itself
pandas/vbt-free). The labeling, feature, and regime imports the J1 body needs are
function-local in ``run`` so importing the contract surface stays light.
"""
from __future__ import annotations  # lazy annotations, consistent with the rest of base/

from typing import TYPE_CHECKING  # keep the data-type imports type-check-only

from strategies.base.strategy import Strategy  # the locked abstract base (pure Python)

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.Series / pd.DataFrame in the hook annotations

    # Brief F MUST create strategies/base/features.py exporting this exact name (§6); this
    # forward reference is the contract that binds brief F's module path and type name.
    from strategies.base.features import FeatureDef  # the feature/peer definition (brief F)
    from strategies.base.fundamental_features import FundamentalFeatureDef  # M3.75 FUND-3 (§8.1): the sibling fundamental declaration
    from strategies.base.run_result import RunResult  # the locked six-slot output type
    from strategies.base.settings import SettingRow  # the typed settings rows
    from strategies.base.strategy_input import StrategyInput  # the locked input type


class EventStrategy(Strategy):
    """Shared event pipeline. Concrete event strategies subclass this and fill the hooks."""

    # The event types this strategy consumes (strategy-intrinsic). The backend run-dispatch
    # reads this off the strategy at dispatch and embeds it in the worker job payload (§8),
    # because the worker mounts the strategy source as a file and does NOT import strategy
    # classes. Universe and window are run-level, not strategy-intrinsic (one strategy runs
    # on many universes/windows). The exact dispatch-read mechanism is OPEN-4 (§22).
    event_types: tuple[str, ...] = ()  # e.g. ("sec_13d", "analyst_rating")

    # The fundamental concepts this strategy reads as-of each event (M4 D3, AD6). The ONE declared
    # source: the materializer fans out off the AST-read attribute (extract_fundamental_concepts) at
    # dispatch, and feature_defs() builds each FundamentalFeatureDef's concept set FROM this same
    # attribute (concepts=self.fundamental_concepts) instead of re-listing the literal. The run-setup
    # assertion below binds every def's concepts to this set, so the AST read and the runtime defs can
    # never reference different concept sets without a loud failure. Default (): an event-only strategy
    # (newsbot_v3) declares no fundamental defs, so its check is vacuously true.
    fundamental_concepts: tuple[str, ...] = ()  # e.g. ("net_income",)

    # M4 D1/D7: see Strategy.asset_class_scope. Re-declared here (the same default) so the attribute is
    # an explicit part of the EventStrategy contract surface the catalog introspects, beside event_types
    # and fundamental_concepts. An event strategy targets a universe, which is always equities, so a
    # concrete event strategy declares ("equities",).
    asset_class_scope: tuple[str, ...] = ()

    # The per-strategy settings scope (spec brief §12): the barrier / sizing / regime parameters
    # are read via get_setting(settings, settings_scope, key). The concrete strategy sets it (e.g.
    # 'event_strategy'); the shared run() fails loud if it is unset (it cannot guess the scope).
    settings_scope: str = ""

    # The trade side for this strategy's events (OD-J1-4, §6.3): LONG by default (analyst upgrades
    # and 13D activist stakes are bullish). The side is the strategy's choice, NOT the label sign
    # (AFML meta-labeling); the fill/sizing seam supports SHORT. A subclass overrides for a short.
    trade_side: int = 1  # +1 LONG / -1 SHORT (event_pipeline.LONG / SHORT)

    def feature_defs(self, settings: "list[SettingRow]", universe_ciks: "tuple[int, ...]" = ()) -> "list[FeatureDef | FundamentalFeatureDef]":
        """The strategy's feature / peer-event definitions (brief F, §6; J2 BN-J2-1; M3.75 FUND-3 §8.5).

        Default: no features. A strategy that wants peer-event features overrides this
        and returns its ``FeatureDef`` list, read once at run setup off ``settings``.

        M3.75 FUND-3 (§8.5) widens the return annotation to the union
        ``list[FeatureDef | FundamentalFeatureDef]``: a strategy may now also declare fundamental
        features (read as-of each event's decision date over the materialized fundamentals slice).
        The widening is annotation-only and additive — an existing override returning
        ``list[FeatureDef]`` is a valid ``list[FeatureDef | FundamentalFeatureDef]``, and the base
        ``return []`` is valid for both (NFR-6, the DECISIONS #12 additive-extension discipline). The
        ``run`` body partitions the union by type (§8.5).

        ``universe_ciks`` is the run's active universe (the membership-kept ``subject_cik``
        set the pipeline derives from the materialized slices, J2 BN-J2-1) — the honest
        peer-set stand-in until an industry-classification source exists. It is a defaulted
        trailing argument, so an override that ignores it (or the base default below) is
        unaffected (the DECISIONS #12 additive-extension discipline; this is an additive
        hook extension, not a reopening of the J1-frozen pipeline architecture).
        """
        return []  # no peer features unless a subclass declares them (universe_ciks ignored here)

    def signal_filter(self, events: "pd.DataFrame", features: "dict[str, pd.DataFrame]") -> "pd.Series":
        """Which events to trade, as a boolean mask over ``events`` (default: trade all).

        ``events`` is the per-event frame the run pipeline assembles (one row per surviving
        event); ``features`` is the computed feature frames keyed by feature name. The
        default selects every event; a subclass narrows the set (e.g. by a feature
        threshold). Shape-agnostic on purpose — it masks whatever index it is given. Safe
        when ``events`` is empty: it returns an empty boolean Series with the same index,
        which the caller reads as zero events to trade.
        """
        import pandas as pd  # function-local: keeps the contract surface import light

        return pd.Series(True, index=events.index, dtype=bool)  # all-pass boolean mask over the event rows

    def run(self, data: "StrategyInput", settings: "list[SettingRow]") -> "RunResult":
        """Run the shared event pipeline and return a complete six-slot ``RunResult``.

        The ordered pipeline (wired in brief J1, EL47/EL78):

        1. Read the materialized event slices from ``data.events`` (per event type).
        2. Apply the per-event membership gate (EL78, DECISIONS #54): keep events whose
           ``membership_valid`` column is True or NULL (no universe declared); drop each
           ``membership_valid=False`` event from the labelled set and count it as the run's
           membership-excluded figure (§5). The gate runs entirely off the denormalized
           flat column, so the no-network container performs no membership lookup.
        3. Compute features via ``feature_defs`` / ``provide_features`` (brief F, §6).
        4. Select the events to trade via ``signal_filter``.
        5. For each surviving event compute the entry bar (EL52) and label it via
           ``triple_barrier.label_event`` (brief E, §10), reading the event's price path
           from ``data.event_prices[trade_time_symbol]`` over the event's window.
        6. Tag each label's regime (brief H, §13).
        7. Build the per-event purge/embargo masks via ``purge_embargo_masks`` (brief G,
           §11) when a train/test split is requested.
        8. Assemble the six-slot ``RunResult`` (§15).
        """
        # Function-local imports keep the contract surface (strategies.base) import-light: the heavy
        # modules (vbt via event_pipeline, pandas, the engines) load only when a run actually executes.
        from pathlib import Path  # provide_features' stored-mode write target; passed but not read under the recompute default (EL67)

        import pandas as pd  # the per-event frame assembly + the regime knowledge_at series

        from strategies.base import event_pipeline as ep  # the vbt-as-accountant assembly (this brief)
        from strategies.base import regime as rg  # the 2024-02-05 regime branch (brief H)
        from strategies.base import triple_barrier as tb  # the triple-barrier labeling engine (brief E)
        from strategies.base.features import provide_features  # the feature layer (brief F)
        from strategies.base.settings import get_setting  # the typed settings reader

        # --- run window + the rule-based regimes band (§11) ---
        if data.window is None:  # an event run always carries its window (BN-J1-2); None is a wiring bug
            raise ValueError(
                "EventStrategy.run requires data.window=(knowledge_from, knowledge_to); the container "
                "entrypoint fills it from run_spec.json for an event run (BN-J1-2)"
            )
        window_start, window_end = int(data.window[0]), int(data.window[1])  # the epoch-second run window
        regimes = rg.regimes_slot(window_start, window_end)  # the rule-based 2024-02-05 band(s) for the window

        # --- settings under the per-strategy scope (§12); fail loud on a missing key (J2 owes the values) ---
        if not self.settings_scope:  # the base cannot guess the scope; a concrete strategy must set it (§12)
            raise ValueError(f"{type(self).__name__} must set settings_scope (the per-strategy settings scope, §12)")
        scope = self.settings_scope  # e.g. 'event_strategy'
        profit_take_mult = float(get_setting(settings, scope, "profit_take_mult"))  # upper-barrier multiple (§10)
        stop_loss_mult = float(get_setting(settings, scope, "stop_loss_mult"))  # lower-barrier multiple
        vertical_horizon = int(get_setting(settings, scope, "vertical_horizon"))  # the time-out, in bars
        vol_window = int(get_setting(settings, scope, "vol_window"))  # the EWMA-std span, in bars
        regime_mode = get_setting(settings, scope, "regime_mode", default="mark")  # split / restrict / mark (default mark)
        # restrict's target regime (BN-H1). J2 adds the regime_select row whose neutral default is the empty
        # string ''; `or None` coerces ''/absent -> None so a default `mark` run does not trip apply_regime_mode's
        # "non-None target only under restrict" guard (regime.py:188-189). A real 'pre_/post_2024_02_05' passes through.
        target_regime = get_setting(settings, scope, "regime_select", default=None) or None  # '' -> None (BN-J2-2)
        position_fraction = float(get_setting(settings, scope, "position_fraction"))  # f (J2 owes value + provenance)
        max_concurrent = int(get_setting(settings, scope, "max_concurrent"))  # N (J2 owes value + provenance)
        # The market-order slippage rate: the neutral scope='slippage' key='default' row dispatch
        # routed by asset class (M4 D2; already provenance'd via the class-named source key), baked
        # into both market legs' fills; the per-fill execution shortfall is read back off the order
        # records against the un-slippaged references in assemble_event_run (DECISIONS #66/#67, §6.2).
        slippage_rate = float(get_setting(settings, "slippage", "default"))

        # --- 1. membership gate (EL78 / #54) + assemble the per-event frame for signal_filter ---
        coverage = {"membership_excluded": 0, "uncovered": 0}  # the run's honest coverage breakdown (§5)
        rows: list[dict] = []  # one dict per in-membership event
        kept_ciks: set[int] = set()  # the active universe: the membership-kept subject_ciks (the §6 peer set, BN-J2-1)
        for event_type in self.event_types:  # only the types this strategy declared (§8)
            slice_df = data.events.get(event_type)  # the materialized slice (may be empty or absent)
            if slice_df is None or len(slice_df) == 0:  # an empty / absent slice contributes no events (EL71)
                continue
            mv = slice_df["membership_valid"]  # the denormalized per-event membership flag (§3.2)
            keep = mv.isna() | mv.fillna(False).astype(bool)  # keep True OR NULL (no universe); drop explicit False
            coverage["membership_excluded"] += int((~keep).sum())  # drop-and-count the out-of-membership events
            # The active universe = the kept rows' subject_cik (BN-J2-1). Read from the slice (slice_df[keep]),
            # NOT events_frame, which never carries subject_cik (only event_id/event_type/knowledge_at/symbol below).
            # An out-of-membership CIK is excluded because it rides on a dropped (~keep) row.
            kept_ciks.update(int(c) for c in slice_df.loc[keep, "subject_cik"])  # the peer set for feature_defs
            for r in slice_df[keep].itertuples(index=False):  # one in-membership event at a time
                rows.append({
                    "event_id": int(r.event_id),  # the spine key
                    "event_type": event_type,  # which slice it came from
                    "knowledge_at": r.knowledge_at,  # the public-knowledge time (epoch or datetime64)
                    "trade_time_symbol": r.trade_time_symbol,  # the price-frame key
                })
        events_frame = (  # the per-event frame signal_filter receives (empty-safe)
            pd.DataFrame(rows).set_index("event_id")
            if rows else pd.DataFrame(columns=["event_type", "knowledge_at", "trade_time_symbol"])
        )

        # --- 2. features (recompute default, brief F); run_data_dir is the unused stored-mode target ---
        # feature_defs receives the active universe (BN-J2-1) so a peer-event strategy can use it as its peer set.
        universe_ciks = tuple(sorted(kept_ciks))  # deterministic order -> reproducible run fingerprint
        # M3.75 FUND-3 (§8.5 i): partition the union feature_defs list by type, then run BOTH feature
        # families into the one features dict. FeatureDef is TYPE_CHECKING-only at module scope
        # (event_strategy.py top), so the isinstance partition needs it promoted to a runtime import
        # here, beside the new fundamental imports (function-local, the contract-surface-light pattern).
        from strategies.base.features import FeatureDef  # promote to runtime for the isinstance partition
        from strategies.base.fundamental_features import FundamentalFeatureDef, provide_fundamental_features  # §8.1/§8.2
        all_defs = self.feature_defs(settings, universe_ciks)  # the union list (arch §7)
        event_defs = [d for d in all_defs if isinstance(d, FeatureDef)]  # the event / peer-event family
        fund_defs = [d for d in all_defs if isinstance(d, FundamentalFeatureDef)]  # the fundamental family
        # M4 D3 (AD6 enforcement): the single-source backstop. The declared fundamental_concepts (the one
        # union the materializer fans out off the AST) MUST be a superset of every concept any
        # FundamentalFeatureDef references. The per-def migration (concepts=self.fundamental_concepts)
        # builds the set FROM the attribute by construction; this assertion catches the orthogonal case
        # an override that hard-codes a literal concept OUTSIDE the declared set — so the AST-extracted
        # attribute and the runtime defs can never silently disagree (the failure AD6's enforcement names).
        # Vacuously true when the strategy declares no fundamental def (fund_defs empty): {} <= anything.
        declared = set(self.fundamental_concepts)                       # the one declared union (M4 D3)
        used = {c for d in fund_defs for c in d.concepts}               # every concept any def references
        if not used <= declared:                                        # a def referencing an undeclared concept
            raise ValueError(
                f"feature_defs references fundamental concepts {sorted(used - declared)} "
                f"not in the declared fundamental_concepts {sorted(declared)}; "
                f"the declared attribute is the single source the materializer fans out (M4 D3/AD6)"
            )
        features = provide_features(  # unchanged shape + args (the existing brief-F seam)
            event_defs, data.events, data.prices, run_data_dir=Path("/data/runs")
        )
        # The fundamental read (§8.2): full-length per-event frames keyed by event_id + the exclude-mode
        # drop set. A no-op (empty frames, empty set) when the strategy declares no FundamentalFeatureDef,
        # so every current price and event strategy is byte-unchanged (NFR-6).
        fund_features, fund_exclude_ids = provide_fundamental_features(fund_defs, data.events, data.fundamentals)
        # Fail loud on a name collision: an event FeatureDef and a FundamentalFeatureDef declared with the
        # SAME name would silently overwrite on the dict update -> assert disjoint keys (a strategy-authoring
        # footgun, caught loud not silent, §8.5).
        _dup = set(features) & set(fund_features)  # the two families' shared feature names
        if _dup:
            raise ValueError(f"feature name collision between event and fundamental features: {sorted(_dup)}")
        features.update(fund_features)  # both families feed the one features dict (keys proven disjoint)

        # --- 3. select which events to trade (default: all), then label + side the survivors ---
        trade_mask = self.signal_filter(events_frame, features)  # boolean mask over events_frame (default all-True)
        # M3.75 FUND-3 (§8.5 ii): the exclude-mode fundamental drop-and-count. Narrow the traded spine
        # by a boolean mask over events_frame — the membership-gate shape (the keep-mask at step 1),
        # applied HERE before the per-event trade loop. The regime-restrict (step 4) is the COUNTING
        # precedent only, not the mechanism: it list-filters post-label, which is too late — an event
        # excluded for a missing fundamental must never be labelled. The count is the MARGINAL drop
        # relative to the post-signal_filter traded set, NOT the raw superset isin: fund_exclude_ids is
        # computed over the full superset frame (§8.2), so it also contains events signal_filter already
        # rejected and out-of-membership events already counted under membership_excluded; those must not
        # be re-attributed here, or coverage["fundamental_excluded"] over-counts (a silent-wrong-number
        # in the R6 "is this data correct?" diagnostic). Gated on a non-empty exclude set so a strategy
        # with no exclude-mode fundamental def has byte-identical coverage (NFR-6 / §15).
        if fund_exclude_ids:  # an exclude-mode fundamental def removed at least one event
            fund_drop = events_frame.index.isin(fund_exclude_ids)  # superset membership over the traded spine
            coverage["fundamental_excluded"] = int((trade_mask & fund_drop).sum())  # MARGINAL: only what signal_filter had kept
            trade_mask = trade_mask & ~fund_drop  # remove the missing-fundamental events before labelling
        candidates: list[ep.EventCandidate] = []  # the labelled, covered, sided events
        for event_id, row in events_frame[trade_mask].iterrows():  # one selected event at a time
            symbol = row["trade_time_symbol"]  # the per-symbol price frame key
            price_path = data.event_prices.get(symbol)  # the OHLCV frame for this event's symbol
            if price_path is None or len(price_path) == 0:  # no resolved price series -> uncovered (drop-and-count, §5)
                coverage["uncovered"] += 1
                continue
            knowledge_at = ep._scalar_knowledge_epoch(row["knowledge_at"])  # normalized trigger time (epoch s)
            label = tb.label_event(  # brief E: the sole exit authority (entry bar + exit bar + kind)
                price_path=price_path, knowledge_at=knowledge_at,
                profit_take_mult=profit_take_mult, stop_loss_mult=stop_loss_mult,
                vertical_horizon=vertical_horizon, vol_window=vol_window,
            )
            if label is None:  # a zero-bar / no-usable-volatility event -> uncovered (drop-and-count, EL51)
                coverage["uncovered"] += 1
                continue
            candidates.append(ep.EventCandidate(
                event_id=int(event_id), symbol=symbol, side=self.trade_side,
                knowledge_at=knowledge_at, label=label,
            ))

        # --- 4. regime mode over the labelled events (brief H, #61): restrict drops-and-counts ---
        if candidates:  # apply_regime_mode needs a non-empty per-event spine
            ka = pd.Series(  # knowledge_at per surviving event, indexed by event_id (the regime spine)
                {c.event_id: c.knowledge_at for c in candidates}, dtype="int64"
            )
            outcome = rg.apply_regime_mode(ka, regime_mode, target_regime=target_regime)  # fail-loud on a bad mode/target
            kept_ids = set(outcome.keep[outcome.keep].index)  # the event ids the mode keeps (restrict drops some)
            coverage["regime_excluded"] = int(outcome.dropped_count)  # restrict's dropped count (0 for split/mark)
            candidates = [c for c in candidates if c.event_id in kept_ids]  # keep only the in-regime events

        # --- 5. (purge/embargo masks, §4 step 8) skipped: no train/test split is requested for a single run.
        #         The label spans (BarrierLabel.label_start/label_end) are available for the M6 CV layer (brief G).

        # --- 6. fill model + selection/cap + the daily mark-to-market assembly (the six slots, §6/§7/§8) ---
        sizer = ep.FixedFractionSizer(f=position_fraction)  # the default sizer (the §8 seam)
        placed, place_counts = ep.select_and_place(  # selection + concurrency cap + same-(symbol,bar) rule + booking
            candidates, data.event_prices, sizer, max_concurrent,
            entry_slippage=slippage_rate, exit_slippage=slippage_rate,  # one market-order rate, both legs (§6.2 reuse)
        )
        coverage.update(place_counts)  # fold the counted exclusions (cap / collision / last-bar) into coverage
        return ep.assemble_event_run(
            placed, data.event_prices, settings, regimes, window_start, window_end, coverage,
        )
