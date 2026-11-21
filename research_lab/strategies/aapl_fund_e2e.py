"""AAPL fundamentals-conditioned event strategy — the M3.75 end-to-end gate + MD6 event-template reference.

Minimal EventStrategy that exercises the FUND-3 fundamental-features channel end to end on real prod
data: it consumes ``analyst_rating`` events for Apple and reads one fundamental concept, ``net_income``,
as-of each event's decision date (no lookahead). The point is to prove the channel FIRES on a dispatched
run and that the as-of read respects the filing boundary, NOT to make money — the same throwaway-reference
role ``ma_cross`` plays for the price contract and ``newsbot_v3`` for the peer-feature contract. It doubles
as the reference implementation for M4's MD6 basic event template.

The fundamental feature ``net_income_asof`` is computed and reported on every traded event; gating trades
on it (e.g. trade only when net income is positive) is a one-line ``signal_filter`` override, deliberately
not added here (the same "computed, reported, gating-is-a-future-override" stance ``newsbot_v3`` takes for
its peer-breadth feature).

Reuses the ``event_strategy`` settings scope (barrier widths, sizing, regime dials) so the shared event
pipeline has its provenance'd settings without seeding a new scope — acceptable for a throwaway gate
strategy, and itself a captured M4 friction point (a brand-new strategy otherwise needs eight provenance'd
settings rows seeded before it can run).
"""
from strategies.base.event_strategy import EventStrategy  # the shared M3.5 event pipeline base (run() is inherited)
from strategies.base.fundamental_features import FundamentalFeatureDef  # the FUND-3 fundamental-feature declaration


class AaplFundE2E(EventStrategy):
    """Long analyst-rating event strategy on AAPL that reads net_income as-of each event (no lookahead)."""

    # The single event type this strategy consumes. A literal tuple of string literals so the backend reads
    # it by AST at dispatch (extract_event_types) without importing this module.
    event_types = ("analyst_rating",)

    # The fundamental concepts the worker must materialize for this run (M4 D3/AD6: the ONE declared
    # source). A literal tuple the backend AST-reads (extract_fundamental_concepts) to fan out the
    # materializer; feature_defs() below builds its FundamentalFeatureDef concept set FROM this same
    # attribute (concepts=self.fundamental_concepts), so the materialized slice and the as-of read can
    # never reference different concepts. The base run-setup assertion enforces the subset relationship.
    fundamental_concepts = ("net_income",)

    # M4 D1/D7: an analyst-rating event run targets a universe, which is always equities (the universe
    # invariant); declaring the scope lets the dispatch guard refuse a crypto-dataset run of this strategy.
    asset_class_scope = ("equities",)

    # Reuse the seeded event_strategy settings scope: the shared run() reads its barrier / sizing / regime dials
    # (all provenance'd, already present in research_settings on prod), so no new scope must be seeded here.
    settings_scope = "event_strategy"  # the shared method scope (renamed from 'newsbot_v3', M4.5 #161)

    # +1 LONG: an analyst upgrade is read as a bullish reaction. The side is the strategy's choice (AFML
    # meta-labeling), NOT the triple-barrier label sign.
    trade_side = 1

    def feature_defs(self, settings, universe_ciks=()):
        """Declare one fundamental feature: net_income read as-of each event's decision date.

        Single concept, so no ``combine`` is needed (the raw as-of value IS the feature). ``missingness
        ='null'`` keeps an event whose net_income is not yet known as-of its date and reports the feature as
        NaN, rather than dropping the event — so the run output shows the as-of read on every event, including
        the honest-missing case (never a fake zero, Q6).
        """
        return [FundamentalFeatureDef(  # the one fundamental feature, read as-of each analyst-rating event
            name="net_income_asof",  # the key this feature lands under in StrategyInput.features
            concepts=self.fundamental_concepts,  # M4 D3/AD6: built FROM the one declared attribute, not re-listed
            missingness="null",  # keep the event, report NaN when no fact is as-of-known (no fake zero)
        )]
