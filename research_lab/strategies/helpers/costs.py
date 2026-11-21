"""The neutral-key cost read (M4 R15, extracted from the D2 cost legs)."""
from __future__ import annotations  # lazy annotations → import stays light

from typing import TYPE_CHECKING  # keep the contract-type import type-check-only

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    from strategies.base.settings import SettingRow  # the typed settings rows


def default_costs(settings: "list[SettingRow]") -> tuple[float, float]:
    """Return ``(fees, slippage)`` from the neutral ``default`` cost keys (M4 D2).

    Dispatch injects ``fees/default`` and ``slippage/default`` resolved from the run's asset class
    (D2), and a strategy reads them here instead of restating the two ``get_setting`` calls: the read
    ``ma_cross`` and the event cost legs each do. Pure: reads ``settings``, never mutates. Fail-loud on a
    missing key (``get_setting`` raises): a missing neutral cost key is a dispatch-injection bug, not a
    default to swallow.
    """
    from strategies.base.settings import get_setting  # function-local: the contract-surface-light idiom

    fees = float(get_setting(settings, "fees", "default"))          # per-trade commission fraction (neutral key)
    slippage = float(get_setting(settings, "slippage", "default"))  # per-trade slippage fraction (neutral key)
    return fees, slippage


# --- IBKR Pro Fixed pricing (the ibkr_costs unit, night nine, 2026-09-05) ----------------------------
# Source, mechanism, sensitivity and bounds for these three figures live in
# docs/research_lab/parameter_provenance.md under the three `fees.ibkr_fixed.*` headings (fetched from
# IBKR's own pricing page on 2026-09-04). Do not restate this arithmetic anywhere else in the tree
# (AC-5, docs/units/ibkr_costs/unit_brief.md): every engine call site imports and calls the one
# function below rather than reproducing the formula.
IBKR_FIXED_PER_SHARE_RATE = 0.005           # USD per share, IBKR Pro Fixed, every monthly volume tier
IBKR_FIXED_MINIMUM_PER_ORDER = 1.00         # USD floor, per order
IBKR_FIXED_MAX_PCT_OF_TRADE_VALUE = 0.01    # 1% of trade value, ceiling per order

# Ops routing law (docs/units/ibkr_costs/unit_brief.md's own "what can break" #3: "only one engine
# call site is wired, so one path prices differently from another"). In-memory only, incremented
# every time a real fill is actually priced, so a future ops-wiring unit can read it from the worker
# process the same way sizing.py's/exits.py's/portfolio.py's own counters already are (research_lab
# has no database access of its own; root CLAUDE.md, research_lab/CLAUDE.md). The reader is owed, the
# same gap those three counters' own registry rows already name; see
# docs/units/ibkr_costs/monitor_rows.md for the drafted registry row. A call count that stays at
# zero after a real backtest that should have traded is the signal this counter exists to surface:
# code that imports and references this function (what the AST probes prove) but never actually
# CALLS it at runtime would be invisible to those probes and visible here.
IBKR_FEE_MONITOR_COUNTERS: dict[str, int] = {
    "ibkr_fixed_fee_calls": 0,  # every real fill this function has priced, across every call site
}


def ibkr_fixed_fee(shares: float, price: float) -> float:
    """The IBKR Pro Fixed commission for one order: given the order's own share count and its own
    fill price, return the dollar cost charged for that one fill.

    Applies the three rules in the order IBKR itself applies them: the per-share rate first, then the
    per-order minimum as a floor (raises a too-small raw cost up to the minimum), then the 1% cap as a
    ceiling applied last (lowers a too-large floored cost back down, even below the minimum for a
    very-low-priced trade: the cap is an absolute ceiling on what any one order costs). IBKR's own
    three published worked examples confirm all three numbers arithmetically (see the per-share
    heading's own Mechanism section in parameter_provenance.md for the fetch date and the worked
    numbers): 100 shares at USD 25 costs USD 1.00 (the minimum binds), 1,000 shares at USD 25 costs
    USD 5.00 (the rate binds), 1,000 shares at USD 0.25 costs USD 2.50 (the cap binds).

    ``shares``/``price`` are read as magnitudes (``abs``): a fee is charged on the size of a trade,
    not its direction, and a fill price is never negative. Every real caller already carries the
    trade's own side separately (``event_pipeline.PlacedTrade.shares`` is already a magnitude;
    ``portfolio.py``'s own sizing is already long-only), so this function never needs to know which
    way the trade went. Deterministic arithmetic; its only side effect is the in-memory monitoring
    counter above, never any I/O, so it stays safe to call once per fill in a loop (how every real
    call site in this tree uses it), or with a single scalar order (how this unit's own probe calls
    it against IBKR's three worked examples).
    """
    IBKR_FEE_MONITOR_COUNTERS["ibkr_fixed_fee_calls"] += 1  # in-memory only; see the comment above the dict
    shares_magnitude = abs(shares)         # a fee is charged on size traded, never on direction
    price_magnitude = abs(price)           # a fill price is never negative; guards a bad caller input
    trade_value = shares_magnitude * price_magnitude
    raw_cost = shares_magnitude * IBKR_FIXED_PER_SHARE_RATE                     # the per-share rate alone
    floored_cost = max(raw_cost, IBKR_FIXED_MINIMUM_PER_ORDER)                  # the minimum binds if raw_cost is lower
    capped_cost = min(floored_cost, IBKR_FIXED_MAX_PCT_OF_TRADE_VALUE * trade_value)  # the 1% cap binds last
    return capped_cost
