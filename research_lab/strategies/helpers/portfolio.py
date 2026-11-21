"""Shared long-only portfolio build for the R16 price templates (M4.5 3b B4 / R15).

The single home for the vbt portfolio construction the five price templates (MA cross, RSI, Bollinger,
MACD, breakout) share — extracted VERBATIM from ``ma_cross.py``'s tail (the ``from_signals`` build +
the per-fill slippage cost + the ``RunResult`` extraction). It exists so a TEMPLATED price strategy
never writes a vbt call itself (R-TMPL3, the M4 forward-constraint M4_architecture.md:127): the skeleton
computes its entry/exit signals declaratively in pandas and hands them here, exactly as ``default_costs``
is the single home for the neutral cost read. This mirrors how the event template stays vbt-free by
inheriting ``EventStrategy.run``.

The vbt import is LAZY (inside the function) so importing ``strategies.helpers`` stays vbt-free at module
load — the catalog (R13/R15) introspects this package without importing the engine, the same discipline
``strategies.base.__init__`` keeps.
"""
from __future__ import annotations  # lazy annotations: no runtime type evaluation, no eager imports

from dataclasses import dataclass  # LeadInStrategyInput's own decorator, matching SizedStrategyInput's (strategy.py)
from strategies.base import RunResult, StrategyInput  # engine-neutral contract types (no vbt)
from strategies.base.strategy import SizedStrategyInput  # #376 ruling 4's own additive-field pattern (see LeadInStrategyInput below)
from strategies.helpers.alpaca_fees import buy_order_fee, sell_order_fee  # FEE1b (#459 item 4): Alpaca's charged fee of one order, the ONE fee source for the old engines
from strategies.helpers.exits import ExitSpec  # lane B (AC-3): the optional ruled three-line exit rule
from strategies.helpers.sizing import (  # job D/lane SW (5.8)/lane LB (5.8e): the shared sizing config,
    SIZING_MONITOR_COUNTERS,  # the lead-in entry mask, the reach dataclass and the last-bar refusal
    FillReach,                # sizing's own frozen two-field dataclass, referenced by LeadInStrategyInput below
    SizingSpec,
    compute_position_sizes,
    mask_entries_before,
    session_days,  # lane P2d: the day label of every bar (None for a daily index), handed to the volatility
)

# #376 ruling 4 round 2 (E-R1-B1): sentinel distinguishing "the caller passed no sizing kwarg at all"
# from an explicit sizing=None (the documented unsized opt-out) or an explicit sizing=<spec>. Mirrors
# run_sweep_entrypoint.py's own _SIZING_NOT_GIVEN idiom rather than inventing a second shape for the
# same distinction. A caller that omits sizing= now falls back to data.resolved_sizing (the
# SizedStrategyInput field a container entrypoint binds before strat.run(), strategy.py) when one was
# bound, and to today's bare SizingSpec() default otherwise -- byte-unchanged for every host-side
# caller or probe that builds a plain StrategyInput and never heard of that subclass.
_SIZING_NOT_GIVEN = object()

# Per-process counter for the ops routing law (lane E round 5, E-B5): this module's own
# CompoundingEquityError had no counter of any kind before this fix, unlike its two siblings in this
# same lane (sizing.py's SIZING_MONITOR_COUNTERS, exits.py's EXITS_MONITOR_COUNTERS), whose own shape
# this pair copies. Incremented in-memory ONLY, deliberately: no I/O, no print, no logging call, so
# incrementing this can never add a byte to a probe's captured stdout/stderr and can never turn a PASS
# into a DRIFT against a committed `.expected` file. Reported as a PAIR, count beside population,
# because a counter that found nothing wrong and a counter that examined nothing both read zero. A
# future ops-wiring unit reads this pair from the worker process, the only place research_lab/ can
# report anything from, since this area has no database access (research_lab/CLAUDE.md), and forwards
# it into docs/ops/ops_monitor_registry.md's rows: the reader is owed and not yet built, the same gap
# sizing.py's own registry rows already name for its three counters; exits.py's pair carries no
# registry row of its own yet either.
PORTFOLIO_MONITOR_COUNTERS: dict[str, int] = {
    "portfolio_undefined_exit_price": 0,  # a trade refused (CompoundingEquityError) for an undefined exit fill price
    "portfolio_undefined_exit_price_population": 0,  # every trade whose equity this walk attempts to update (the denominator)
}


class CompoundingEquityError(Exception):
    """Raised by ``_compounding_shares`` (lane E round 4, E-SF7) when a trade's own EXIT fill price is
    undefined (NaN): a mid-history data gap or a member's own delisting tail, the same class of edge
    ``strategies.helpers.exits.UndefinedExitVolatilityError`` refuses inside the exit-line forward walk
    (E-SF6), but reachable here independently of that fix for two reasons. First, the exit FILL happens
    one bar AFTER the exit SIGNAL bar (L14): E-SF6 only confirms the signal bar's own high/low are
    defined, never the following bar's open this function actually fills on. Second, this walk also
    runs for a caller's own raw ``exits`` argument when no ``exit_spec`` is given (e.g. a template's
    ``crossed_below`` signal): that path never calls ``compute_exit_lines`` at all, so E-SF6's fix
    inside it never runs. Refused here, at the true point of failure, naming the trade and bar
    responsible (reason=undefined_exit_price), rather than left to silently poison ``equity`` with NaN
    and surface several trades later as an opaque ``ValueError`` out of ``compute_position_sizes`` with
    no trace back to this trade or this bar.
    """


@dataclass(frozen=True)  # frozen: matches StrategyInput's and SizedStrategyInput's own immutable contract
class LeadInStrategyInput(SizedStrategyInput):
    """``SizedStrategyInput`` plus ONE additive field: the exact ``pd.Timestamp`` (or ``None``) before
    which no entry may fire (lane SW, 5.8, `DECISIONS.md` #383 decision 2). ``prices`` on this subclass
    is already the LEAD-IN-EXTENDED frame ``clip_frame_with_lead_in`` returns -- estimator_window bars
    of history before ``entries_not_before``, PLUS the traded window -- so an estimator warms up on the
    lead-in bars while ``entries_not_before`` still refuses a trade on any of them (condition 1: "a
    lead-in is not a draw").

    Subclassing ``SizedStrategyInput`` rather than editing ``strategy_input.py`` (outside this lane's own
    areas) or ``strategy.py`` (the doc spine's own area map, never a build lane's to touch) is the SAME
    precedent ``SizedStrategyInput`` itself already set for ``resolved_sizing``, kept inside this unit's
    own files (``strategies/helpers/portfolio.py`` is squarely this lane's, unlike ``strategies/base/``).
    ``entries_not_before`` defaults to ``None`` so a plain ``StrategyInput`` or a bare
    ``SizedStrategyInput`` (built by any host-side caller or probe that never heard of this subclass) is
    unaffected -- ``long_only_signal_result`` reads it with ``getattr(data, "entries_not_before", None)``,
    never an ``isinstance`` check, the same idiom ``resolved_sizing`` already established, so a plain
    ``StrategyInput``, a ``SizedStrategyInput`` and this subclass all work at the same call site.

    ``fill_reach`` (lane LB, 5.8e, `DECISIONS.md` #406 ruling 2) is a SECOND additive field, the same
    precedent: a ``FillReach`` (or ``None``) naming the one real bar strictly past this frame's own
    last bar the entry may be priced against, when a signal sits on that last bar AND the tracker's
    own granted window still has room for one more bar (``sizing.fill_reach_after``'s own contract).
    Defaults to ``None`` so every existing caller of ``LeadInStrategyInput`` (predating this lane) and
    every OTHER ``StrategyInput`` shape is unaffected -- ``long_only_signal_result`` reads it with the
    same ``getattr(data, "fill_reach", None)`` idiom ``entries_not_before`` itself already established.
    """
    entries_not_before: "pd.Timestamp | None" = None
    fill_reach: "FillReach | None" = None


def _walk_trade_segments(entries, final_exits):
    """Reduce boolean entries/exits signals to a list of ``(entry_pos, exit_pos)`` integer bar
    positions, one tuple per trade, mirroring vbt's OWN non-accumulating, single-position long-only
    signal semantics (``direction="longonly"``, no ``accumulate``), INCLUDING its same-bar conflict
    rule (E-B4, lane E round 4): no ``conflict_mode`` is passed to ``from_signals`` below (see the
    comment just above that call), so vbt's default ``ConflictMode.Ignore`` governs -- when a bar
    carries BOTH an entry signal and an exit signal, that bar produces NO state change at all, whatever
    the position is doing at the time: flat plus both stays flat (no trade opens on that bar), open
    plus both stays open (no trade closes on that bar). An entry signal arriving while a position is
    already open is separately ignored here too, exactly as vbt ignores it. Both rules together are
    what let this walk and the actual simulation always agree on how many trades there are and where
    each one starts and ends. A trade still open when the series ends yields ``exit_pos=None``; job D
    never needs an equity update for a trade that never closed, since there is no later entry left in
    the series to size off it.

    Dtype checked against every REAL caller, not assumed (lane E round 4's own open question): both
    ``entries`` and ``final_exits`` are always plain ``dtype=bool`` with no possible NA in every real
    call shape traced -- the five price templates' own ``(a > b) & (a.shift(1) <= b.shift(1))``
    comparison chains, ``ma_cross``'s ``.vbt.crossed_above``/``crossed_below`` (single-run AND the wide,
    column-stacked sweep path via ``@vbt.parameterized(merge_func="column_stack")``, the one real
    ``sweep_signals`` in this repo), and ``compute_exit_lines``'s own ``exits`` output
    (``strategies/helpers/exits.py``, built as ``pd.Series(False, ..., dtype=bool)``) all verified
    empirically, against the pinned build, to stay plain bool with zero NA even across a NaN warmup
    region. No caller today passes a nullable ``"boolean"``-dtype series (whose NA would raise inside
    ``bool()``) or a float series carrying NaN as a signal (whose NaN ``bool()`` reads as True), so no
    guard for either shape is added here: a guard neither real caller can exercise would be untestable
    and unjustified. If a future caller ever passes something else, this docstring is the place to
    revisit that call.

    This is bookkeeping, not sizing arithmetic (AC-D4's absence proof is about a second
    ``SizingSpec``/``compute_position_sizes`` definition, never about a caller that calls the one
    shared function more than once): the actual risk-based formula still lives in exactly one place,
    ``strategies.helpers.sizing.compute_position_sizes``, called once per trade by the caller below.
    """
    segments = []                            # the list this function returns, one tuple per trade
    position_open = False                    # mirrors vbt's own single-position state, bar by bar
    entry_pos = None                         # the currently-open trade's own entry bar, or None
    n_bars = len(entries)                    # the series length, so the loop covers every bar exactly once
    for i in range(n_bars):                  # walk the whole series once, in chronological order
        entry_signal = bool(entries.iloc[i])       # this bar's own entry flag
        exit_signal = bool(final_exits.iloc[i])    # this bar's own exit flag
        if entry_signal and exit_signal:     # E-B4: ConflictMode.Ignore -- both true on the SAME bar
            continue                         # cancels out entirely; no state change, whatever position_open is
        if not position_open and entry_signal:  # a fresh entry, only honoured while flat
            position_open = True
            entry_pos = i
        elif position_open and exit_signal:  # this bar closes the currently open trade
            segments.append((entry_pos, i))
            position_open = False
            entry_pos = None
    if position_open:                        # the last trade never closed before the series ended
        segments.append((entry_pos, None))   # recorded with no exit; the caller needs no equity update for it
    return segments


def _compounding_shares(close, high, low, open_, entries, final_exits, spec, *, init_cash, fee_rate,
                         slippage_rate=0.0, factor=None, entry_fee_func=buy_order_fee, exit_fee_func=sell_order_fee,
                         session_day=None):
    """The per-trade share-count Series job D needs (AC-D2/AC-D3/AC-D5): one call to
    ``compute_position_sizes`` PER TRADE, each with the equity realized so far, rather than one call
    for the whole run against a single static equity value. ``compute_position_sizes`` itself "takes
    exactly one equity value per call" by its own signature (``strategies/helpers/sizing.py``), so
    compounding across trades is exactly the caller-level policy question its own docstring leaves
    open; this is that policy, built once and shared by both real call sites.

    Because the whole ``entries``/``final_exits`` signal series is already known before the simulation
    runs (this project's own signals are computed causally in pandas ahead of time, never derived by
    vbt mid-run), and because a fill always lands at a deterministic price (the next bar's open, or the
    last bar's own close when no next bar exists), each trade's own entry and exit price, and so its
    realized profit or loss, can be read directly from the price series without needing vbt to report
    anything back mid-simulation. That is what makes the helper stay fully authoritative (unit brief
    section 2.2's "Shape B"): no vbt callback or `SizeType.ValuePercent` route is needed at all, so the
    loud refusal on an unaffordable or zero-share trade (``SizingError``, uncaught, propagates exactly
    as it already did) is never at risk of becoming a silent partial fill.

    Returns ``(shares, ending_equity)``. ``shares`` is NaN except at each trade's own entry bar, the
    same shape ``compute_position_sizes`` itself returns, so it drops into ``from_signals``' ``size=``
    kwarg unchanged. ``ending_equity`` (lane LB, 5.8e) is the running equity value after every trade in
    ``entries``/``final_exits`` that actually closed, the SAME number the walk above already tracks for
    its own next-trade sizing -- returned rather than discarded so a caller can size ONE MORE trade,
    strictly after every trade this walk already knows about, off the identical running figure (never a
    second, independently-derived equity model): ``long_only_signal_result``'s own reach-pricing branch
    is that one caller.

    ``slippage_rate`` (lane KA, docs/units/KA/criteria.md AC-5, the E-SF4 comment below): the run's own
    per-fill slippage fraction, the SAME rate ``strategies.base.slippage.price_path_slippage_cost``
    recovers vbt's own executed price from (``executed = reference * (1 + s*rate)``, s=+1 buy, s=-1
    sell). Defaults to ``0.0`` (no slippage modelled) so a caller built before this fix -- both
    ``sweep_portfolio.py`` call sites, and any probe that calls this function directly -- keeps its
    prior, byte-unchanged answer for THIS one input; ``long_only_signal_result`` (this walk's own
    real-run caller) always passes its own ``slippage`` argument explicitly, so the real backtest path
    is unaffected by the default.

    ``factor`` (lane D1, real-share sizing): the per-day adjustment factor series, passed straight to
    ``compute_position_sizes`` so each trade sizes in real shares; the fixed IBKR fee on each leg is
    then priced on the real share count held at that fill bar (adjusted shares * factor) at the price
    actually traded there (executed price / factor), via ``_real_fee_inputs``. None means factor 1,
    byte-identical to every caller before D1.
    """
    import pandas as pd  # lazy: only when sizes are actually computed, matching this module's own style

    n_bars = len(close)                                    # the series length, for the "no next bar" fill rule
    equity = init_cash                                     # running equity; only ever moves when a trade CLOSES
    shares = pd.Series(float("nan"), index=close.index, dtype="float64")  # NaN until a trade's own entry fills it

    for entry_pos, exit_pos in _walk_trade_segments(entries, final_exits):  # one call per trade, in trade order
        single_entry = pd.Series(False, index=close.index, dtype=bool)  # isolates THIS trade for compute_position_sizes
        single_entry.iloc[entry_pos] = True
        sized = compute_position_sizes(      # AC-1's ruled rule, unchanged: the SAME formula, called once per trade
            close, high, low, open_, single_entry, spec,
            equity=equity,                    # AC-D3: this trade's OWN equity, not the run's static starting capital
            fee_rate=fee_rate,                # B1 fix, unchanged: this run's own commission fraction
            slippage_rate=slippage_rate,      # AC-9 (KA round 4, SF2): this call's own real slippage rate, the
            # SAME input this function's own fee/equity walk below already applies to entry_executed, so the
            # APPROVAL decision and the accounting agree on what "affordable" means.
            fixed_fee_func=entry_fee_func,    # AC-9 / FEE1b: the entry order's Alpaca fee, CALLED, never restated
            factor=factor,                    # lane D1: real shares at the price actually traded (None = factor 1)
            session_day=session_day,          # lane P2d: day labels for bars shorter than a day (None = bar-count path)
        )
        fill_shares = float(sized.shares.iloc[entry_pos])  # this trade's own share count, from the one shared rule
        shares.iloc[entry_pos] = fill_shares

        if exit_pos is not None:              # this trade actually closed: realize its P&L before the NEXT entry
            PORTFOLIO_MONITOR_COUNTERS["portfolio_undefined_exit_price_population"] += 1  # in-memory only; see the comment above the dict
            entry_price = open_.iloc[entry_pos + 1]         # L14: fills at the next bar's open, same as sizing.py
            # entry_price can never be NaN here: compute_position_sizes (called just above, same bar,
            # the identical `open_.iloc[pos + 1]` formula, sizing.py) already raised UndefinedVolatilityError
            # first if this exact value were undefined, so reaching this line at all already proves it is
            # defined -- a second check here would be unreachable dead code, not a guard.
            if exit_pos + 1 < n_bars:                       # a next bar exists to fill the exit on
                exit_price = open_.iloc[exit_pos + 1]
                exit_fill_pos = exit_pos + 1                # E-SF7: which bar this exit_price actually came from
            else:                                           # exits addendum 2.2: no next bar, forced fill at close
                exit_price = close.iloc[exit_pos]
                exit_fill_pos = exit_pos                    # E-SF7: which bar this exit_price actually came from

            if pd.isna(exit_price):  # E-SF7: a mid-history data gap or the member's own delisting tail can
                # leave the EXIT FILL bar undefined even when exits.py's own E-SF6 fix already confirmed the
                # EXIT SIGNAL bar's high/low were defined -- the fill happens ONE BAR LATER (L14), a bar that
                # fix never inspects, and E-SF6 gives no protection at all on this function's OTHER caller
                # shape (a raw caller-supplied `exits` signal with no exit_spec, which never touches
                # compute_exit_lines). Refuse HERE, at the true point of failure, naming the trade and bar
                # responsible, instead of letting a NaN equity silently poison the NEXT trade's
                # compute_position_sizes call several trades later with no trace back to this one (the
                # undebuggable "cannot convert float NaN to integer" the finding measured).
                PORTFOLIO_MONITOR_COUNTERS["portfolio_undefined_exit_price"] += 1  # in-memory only; see the comment above the dict
                raise CompoundingEquityError(
                    f"trade entered at bar {entry_pos} (fill price {entry_price!r}), exit signalled at "
                    f"bar {exit_pos}: the exit fill price is undefined (NaN) at bar {exit_fill_pos}; "
                    "cannot realize this trade's profit/loss or update the compounding equity the next "
                    "trade sizes against; reason=undefined_exit_price"
                )

            # E-SF4, CORRECTED (lane KA, docs/units/KA/criteria.md AC-5, k07): real cash is post-fee AND
            # post-slippage (the paper brief's equity formula, cash + position_value,
            # docs/briefs/paper_trading_spec_brief.md, which #323 requires this backtest sizer to
            # match). The PREVIOUS version of this comment only netted `fill_shares * price * fee_rate`
            # per leg and never netted slippage at all, so the running equity handed to the next trade
            # overstated real cash by exactly the fixed fee and the slippage every closed trade actually
            # paid (measured: `12.966455044208` dollars over 3 trades on the real KA fixture,
            # `docs/units/KA/hand_derivation.md` section 6). The real fee vbt charges on every fill is
            # BOTH components ADDED (this suite's own `d07_ac_d3_fee_deducted_from_compounded_equity`
            # probe already established this precedent, its own hand formula: `fee_rate * shares *
            # price + ibkr_fixed_fee(shares, price)`, "additive to the percentage fee_rate"): the
            # proportional `fees=` kwarg vbt charges on the fill value directly, PLUS the fixed
            # `ibkr_fixed_fee` charged via the separate `fixed_fees=` kwarg whenever sizing governs
            # (`_fixed_fee_series` below); an EARLIER version of this fix REPLACED the proportional term
            # with the fixed one instead of adding it, which this suite's own d07 caught (a genuinely
            # non-zero `fee_rate`, e.g. a non-equities cost model, would then never be netted at all).
            # Slippage is applied the SAME multiplicative formula
            # `strategies.base.slippage.price_path_slippage_cost` recovers vbt's own executed price from
            # (`executed = reference * (1 + s*rate)`, s=+1 a buy, s=-1 a sell): "one shared rule with the
            # reported portfolio, never a second equity model" means the identical FORMULA, not a second,
            # independently-invented one -- `_compounding_shares` cannot call into the real `pf` for this
            # (it runs BEFORE `pf` exists, to size the shares `pf` will need), so it applies the same
            # formula directly. Both fee components are priced off the EXECUTED (slippage-adjusted)
            # price, matching k01's own hand-derivation script and vbt's own proportional fee (which
            # charges on the actual fill price). AC-10 (KA round 5, `#430`): `_fixed_fee_series` below
            # ALSO prices its own fee on this same executed price, as of this fix; before round 5 it
            # read the raw, unslipped reference price instead, so this comment's "matching... below"
            # claim did not hold until this round made it true (the round-4 finding this fixes). The
            # REPORTED portfolio is unaffected either way: vbt computes and charges its own fee and
            # slippage itself when it builds the real, returned `pf`; only this sizing-facing equity
            # figure was ever wrong.
            entry_executed = entry_price * (1.0 + slippage_rate)  # s=+1: a buy fills ABOVE its reference
            exit_executed = exit_price * (1.0 - slippage_rate)    # s=-1: a sell fills BELOW its reference
            entry_fee = (  # proportional commission plus the fixed ibkr fee, additive (AC-5, d07's own precedent)
                fill_shares * entry_executed * fee_rate
                + entry_fee_func(*_real_fee_inputs(fill_shares, entry_executed, factor, entry_pos + 1))
            )
            exit_fee = (
                fill_shares * exit_executed * fee_rate
                + exit_fee_func(*_real_fee_inputs(fill_shares, exit_executed, factor, exit_fill_pos))
            )
            equity = equity + fill_shares * (exit_executed - entry_executed) - entry_fee - exit_fee  # the
            # paper brief's cash + position_value formula (docs/briefs/paper_trading_spec_brief.md), now
            # net of both fills' real fee AND slippage; position_value=0 here since only one position is
            # ever open and this trade has just closed
        # else: still open at the series' end (no exit_pos); no later entry reads this trade's own equity

    return shares, equity


def _real_fee_inputs(adjusted_shares, adjusted_price, factor, fill_pos):
    """Lane D1: the (shares, price) pair a per-share broker fee is charged on, at one fill bar. IBKR
    charges per REAL share at the price actually traded, so with a factor series the adjusted position
    becomes adjusted_shares * f real shares at adjusted_price / f (the trade value is unchanged). With
    no factor (None) both pass through untouched, so every pre-D1 caller's fee is byte-identical."""
    if factor is None:
        return adjusted_shares, adjusted_price
    f = float(factor.iloc[fill_pos])
    return adjusted_shares * f, adjusted_price / f


def _fixed_fee_series(close, open_, entries, final_exits, shares, fee_func, slippage_rate=0.0, factor=None, exit_fee_func=None):
    """The per-bar ``fixed_fees`` array ``from_signals`` needs (the ibkr_costs unit, AC-5/AC-8): a
    real fee at a bar where a REAL fill happens (a trade's entry, or its matching exit), carrying
    that fill's own ``fee_func`` dollar cost, and a FINITE value (0.0) at every other bar carrying
    a raw entry or exit signal.

    AC-10 (KA round 5, `#430`): ``fee_func`` is now called on the EXECUTED price, never the raw
    reference price, on both legs. ``_compounding_shares`` above already prices both fee components
    off ``entry_price * (1.0 + slippage_rate)`` (a buy fills above its reference) and
    ``exit_price * (1.0 - slippage_rate)`` (a sell fills below its reference), the SAME buy/sell
    convention ``strategies.base.slippage.price_path_slippage_cost`` uses; this function priced the
    fee on the raw, unslipped reference price instead, which agrees with the executed-price basis
    everywhere ``ibkr_fixed_fee``'s per-share rate or per-order minimum binds (both are
    price-independent) but disagrees once its 1% cap binds, since the cap is a percentage of the
    EXECUTED trade value. ``slippage_rate`` defaults to ``0.0``, which collapses ``executed = raw *
    (1.0 +/- 0.0) = raw`` exactly, so a caller that never passes it keeps today's behaviour
    byte-for-byte.

    CORRECTED, lane RM round 2 (ac4e, 2026-09-25): the previous version of this docstring said NaN
    was safe on every bar with no real fill, reasoning that vbt only reads ``fixed_fees`` at a bar
    where it is already placing an order. That reasoning was wrong for one case: vbt's own
    ``Portfolio.from_signals`` validates ``fixed_fees`` for finiteness at every bar carrying a RAW
    entry or exit signal, even one that never becomes a real fill (an exit signal firing while no
    position is open is a no-op trade-wise, e.g. a generated strategy's holding-cap exit scheduled
    for an entry that ``mask_entries_before`` later masked out of the lead-in window). A leftover
    NaN on such a bar reaches vbt's check and raises ``order.fixed_fees must be finite`` even though
    no money ever changes hands there. The fix below fills exactly those orphaned raw-signal bars
    with 0.0 after the real-fill loop runs, never touching a bar the loop already priced.

    Walks the SAME trade segments ``_compounding_shares`` already walked to build ``shares``: this
    function is called only after ``_compounding_shares`` has returned successfully for the identical
    ``entries``/``final_exits``, so every price read below is already known-defined (the sizing
    refusal in ``compute_position_sizes`` and the exit-price refusal in ``CompoundingEquityError``
    above have already run, without raising, on these same bars). Re-derives the SAME next-open /
    forced-close-at-end fill price ``_compounding_shares`` uses for its own equity walk (L14), rather
    than a second, independent pricing rule: the only thing computed twice here is WHICH bar a fill
    lands on, never the fee arithmetic itself, which is the one function ``fee_func`` supplies
    (``strategies.helpers.costs.ibkr_fixed_fee`` at every real call site, AC-5's single-source rule).

    ``fee_func`` is taken as a parameter, not imported here, so this walk stays a generic "given a fee
    function, price a fill" helper with no IBKR-specific number of its own; every real caller passes
    ``ibkr_fixed_fee`` explicitly, which is also what gives each caller's OWN file a direct import of
    and reference to the one fee function (rather than only an indirect, transitive dependency on it
    through this helper).

    A trade charges an IBKR-style fee on BOTH sides of a round trip (the entry buy and the exit sell
    are two separate broker fills, same share count), so both bars get an entry, not only the entry
    bar; a trade still open at the series' end pays only its entry-side fee, since no exit order has
    fired yet.
    """
    import pandas as pd  # lazy: matches this module's own style (pandas only imported when actually needed)

    n_bars = len(close)                                              # the series length, for the "no next bar" edge
    fixed_fees = pd.Series(float("nan"), index=close.index, dtype="float64")  # NaN until a real fill sets it
    for entry_pos, exit_pos in _walk_trade_segments(entries, final_exits):     # the SAME segments shares was built from
        fill_shares = shares.iloc[entry_pos]                         # this trade's own share count (already sized)
        entry_price = open_.iloc[entry_pos + 1]                      # L14: the same next-open fill _compounding_shares reads
        # AC-10: price the fee on the EXECUTED price, not the raw reference, matching
        # _compounding_shares's own `entry_price * (1.0 + slippage_rate)` (a buy fills above its
        # reference, s=+1).
        entry_executed = entry_price * (1.0 + slippage_rate)
        fixed_fees.iloc[entry_pos] = fee_func(*_real_fee_inputs(fill_shares, entry_executed, factor, entry_pos + 1))  # the entry-side fill's own fee (real shares, lane D1)
        if exit_pos is not None:                                     # this trade actually closed within the series
            if exit_pos + 1 < n_bars:                                 # a next bar exists to fill the exit on
                exit_price = open_.iloc[exit_pos + 1]                 # unchanged (I1-B1 fix): still priced from the next-open fill
                exit_fill_pos = exit_pos + 1                          # lane D1: the bar whose factor converts to real shares
            else:                                                     # forced fill at close: no next bar exists
                exit_price = close.iloc[exit_pos]                     # unchanged (I1-B1 fix): still priced from this bar's own close
                exit_fill_pos = exit_pos
            # AC-10: the exit leg's own executed price, matching _compounding_shares's own
            # `exit_price * (1.0 - slippage_rate)` (a sell fills below its reference, s=-1).
            exit_executed = exit_price * (1.0 - slippage_rate)
            # I1-B1 fix (findings.tsv, lane I round 1): write the exit fee at exit_pos, the exit SIGNAL
            # bar, never at the fill bar computed above. vbt reads `fixed_fees` at the bar where it
            # PLACES an order -- the signal bar, for both sides of a trade -- and fills at the next
            # bar's open; the fee's VALUE still comes from whichever fill price was resolved just
            # above, only the bar the value is WRITTEN to has moved. The forced-close branch already
            # wrote here (exit_pos == its own former exit_fill_pos), so this branch is unchanged.
            fixed_fees.iloc[exit_pos] = (fee_func if exit_fee_func is None else exit_fee_func)(*_real_fee_inputs(fill_shares, exit_executed, factor, exit_fill_pos))  # the exit-side fill's own fee, at the SIGNAL bar
        # else: still open at the series' end (no exit_pos); no exit order has fired, so no exit-side fee yet
    # Root-cause fix, lane RM round 2 (ac4e): vbt's own Portfolio.from_signals checks that every bar
    # carrying a RAW entry or exit signal has a finite fixed_fees value, even when that raw signal
    # never becomes a real fill (an exit signal that fires while no position is open is a no-op for
    # our own _walk_trade_segments walk above, so it never gets priced by the loop above; the classic
    # case is a forced-exit signal from a generated strategy's holding cap that was scheduled for an
    # entry which mask_entries_before later masked away, orphaning the exit). Such a bar places no
    # order, so its fee is correctly zero, not the IBKR rate; this line only touches bars the loop
    # above left at their initial NaN, so it can never change the fee already computed for a bar that
    # DOES place a real order (the STOP IF condition the brief names).
    no_fill_signal_mask = (entries | final_exits) & fixed_fees.isna()  # raw signal, still unpriced
    fixed_fees[no_fill_signal_mask] = 0.0  # finite and correct: no order is placed on this bar
    return fixed_fees


def true_bars_forced_close_stats(true_close, trades, fee_rate, init_cash, freq, year_freq):
    """Lane KA finding KA-R1 (docs/units/KA/reads/read_1.md): when fix #1 above prices a forced
    last-bar exit by running vbt on the series plus one synthetic bar, every BAR-INDEX-DEPENDENT
    stored statistic (sharpe_ratio, max_drawdown, max_gross_exposure, turnover) must still be read off
    the TRUE bars only, never the one extra synthetic bar that priced the fill. This function changes
    no dollar figure at all (the fill stays vbt's own, exactly as fix #1 computed it): it rebuilds a
    SEPARATE, honestly-true-bars-only vbt Portfolio from the fills ALREADY RECORDED in `trades` (the
    caller's own canonical trades frame, already relabeled onto true timestamps by fix #1's trim), via
    `vbt.Portfolio.from_orders`: each fill is placed at its own EXPLICIT bar and price, so there is no
    "next open" to lack and no synthetic bar is ever needed for this reconstruction. This is the SAME
    explicit-fill technique `strategies/base/event_pipeline.py`'s own `from_orders` call already uses
    in this codebase ("our prices, vbt keeps the books"), and the SAME reconstruction the probe-writer's
    own `k09`/`k10` (`research_lab/probes/known_answer/`) build independently to check this fix
    against, so the two agree by construction rather than by coincidence.

    `trades` carries the canonical columns (`entry_index`, `exit_index`, `size`, `avg_entry_price`,
    `avg_exit_price`, `entry_fees`, `exit_fees`, `status`); an "Open" trade (never this lane's own
    concern) gets no exit order placed here either, mirroring whatever `trades` already says. Returns
    a plain dict of the four corrected stats, ready to overwrite those same four keys in the caller's
    own `stats` dict; every other stats key (total_return, num_trades, win_rate, profit_factor,
    total_fees_paid, total_slippage_cost) is fill-derived, not bar-count-dependent, and already
    correct off the extended portfolio (k03's own AC-3/AC-5 check), so this function never touches
    them.
    """
    import pandas as pd  # lazy, matching every other pandas use in this module
    import vectorbtpro as vbt  # lazy: the engine, only needed for this true-bars reconstruction
    from strategies.base.vbt_extraction import _finite_or_none, extract_turnover  # the exact single-path coercions

    size = pd.Series(float("nan"), index=true_close.index, dtype="float64")       # NaN = no order this bar
    price = pd.Series(float("nan"), index=true_close.index, dtype="float64")      # the fill price, our own
    fixed_fees = pd.Series(float("nan"), index=true_close.index, dtype="float64")  # the fill's own recorded fee
    for _, row in trades.iterrows():                            # one row per closed (or still-open) trade
        entry_pos = true_close.index.get_loc(row["entry_index"])  # the entry fill's own true bar
        size.iloc[entry_pos] = float(row["size"])                # a positive buy order
        price.iloc[entry_pos] = float(row["avg_entry_price"])    # the entry fill's own recorded price
        fixed_fees.iloc[entry_pos] = float(row["entry_fees"])    # the entry fill's own recorded fee
        if row["status"] == "Closed":                            # an exit fill was actually recorded
            exit_pos = true_close.index.get_loc(row["exit_index"])  # the exit fill's own true bar (already relabeled)
            size.iloc[exit_pos] = -float(row["size"])             # a matching sell order closes the position
            price.iloc[exit_pos] = float(row["avg_exit_price"])   # the exit fill's own recorded price
            fixed_fees.iloc[exit_pos] = float(row["exit_fees"])   # the exit fill's own recorded fee
        # else ("Open"): no exit order placed here either; this lane's own fix never leaves a trade
        # open at the series' end (that gap is fix #1's own subject), so this branch is inert today.

    pf_true = vbt.Portfolio.from_orders(  # the accountant: our prices, vbt keeps the books (event_pipeline.py's idiom)
        close=true_close,                # the TRUE bars only, never the synthetic one
        size=size,                       # explicit per-order share counts (NaN where no order)
        price=price,                     # our own recorded fill prices; vbt never re-derives a fill
        size_type="amount",              # shares, so the price arg is the literal fill price
        fees=fee_rate,                   # the run's own proportional commission rate
        fixed_fees=fixed_fees,           # each fill's own already-recorded IBKR Fixed cost
        slippage=0.0,                    # slippage is already baked into `price`; never double-applied
        init_cash=init_cash,             # the same starting cash the extended portfolio used
        freq=freq,                       # the run's own observed bar period
        year_freq=year_freq,             # the run's own annualization convention
    )
    return {
        "sharpe_ratio": _finite_or_none(pf_true.sharpe_ratio),                       # None on zero variance
        "max_drawdown": abs(float(pf_true.max_drawdown)),                           # stored as a positive fraction
        "max_gross_exposure": _finite_or_none(pf_true.get_gross_exposure().max()),  # native vbt, true bars only
        "turnover": extract_turnover(pf_true),                                       # the one shared order-based formula
        # AC-8 (KA round 4, code-quality B1): skew/kurtosis are the sweep path's own two additional
        # bar-index-dependent keys (cell_metric_columns's own "sub.returns.skew()"/"kurtosis()" reads,
        # strategies.helpers.sweep_portfolio), added to this SAME true-bars reconstruction rather than
        # a second one, per the lead's own instruction. The single-run path's own rr.stats never
        # carries either key at all (build_run_result has no skew/kurtosis entry), so these two are
        # read here only for the sweep caller to overwrite; a single-run caller simply never reads them.
        "skew": _finite_or_none(pf_true.returns.skew()),      # third-moment shape; None on a too-short window
        "kurtosis": _finite_or_none(pf_true.returns.kurtosis()),  # excess kurtosis; None on a too-short window
    }


def long_only_signal_result(
    data: StrategyInput,            # the locked strategy input (close/open/high/low series)
    entries,                        # boolean entry-signal Series (True only on the entry bar)
    exits,                          # boolean exit-signal Series (True only on the exit bar)
    fees: float,                    # commission fraction from default_costs (the neutral key dispatch)
    slippage: float,               # slippage fraction from default_costs
    *,                              # the overlay + cash are keyword-only (named at the call site)
    indicators: dict | None = None,  # named series for the price-chart overlay (e.g. the SMAs), or none
    init_cash: float = 10_000.0,    # starting cash in $ (the ma_cross reference default)
    sizing: "SizingSpec | None | object" = _SIZING_NOT_GIVEN,  # job D (AC-D2/`DECISIONS.md` #321): the
    # shared rule is the DEFAULT for an ordinary call, not an option nobody requests; `size=None` (vbt's
    # own unbounded, unsized default) is no longer the path an ordinary run takes. `sizing=None` stays a
    # valid, EXPLICIT opt-out for a caller that deliberately wants the old unbounded-cash behaviour
    # (e.g. reproducing a result written before this default flipped, `DECISIONS.md` #324's own gap); it
    # is never the default a caller falls into by omission any more. #376 ruling 4 round 2 (E-R1-B1):
    # the default is now the _SIZING_NOT_GIVEN sentinel rather than a bare `SizingSpec()` literal, so
    # the body below can tell "nothing passed" apart from both explicit cases and read data.resolved_sizing
    # first; see the sentinel's own module-level comment.
    exit_spec: ExitSpec | None = None,  # lane B (AC-3): the ruled three exit lines (profit/trailing
    # stop/time limit). None (the default) is unchanged, additive behaviour: the caller's own `exits`
    # argument is used exactly as given, byte-identical to this function's behaviour before lane B.
    # When given, the three ruled lines are computed by ``strategies.helpers.exits.compute_exit_lines``
    # and REPLACE the caller's `exits` argument as this run's own exit rule (see the body below for
    # why replacing rather than merging is the honest contract here: two independently-timed exit
    # sources merged into one boolean series would hide which one actually closed a given trade).
) -> RunResult:
    """Build a complete long-only RunResult from boolean entry/exit signals (the ma_cross tail, extracted).

    Signal-at-close, fill-at-next-open (L14), long-only, the canonical price-rule execution model.
    With no ``sizing`` argument (the new default, job D) every entry is sized by
    ``strategies.helpers.sizing.compute_position_sizes`` (the ONE shared place both this function and
    ``sweep_portfolio.sweep_portfolio_cells`` read sizing from, AC-1), off the equity realized so far
    (``docs/briefs/paper_trading_spec_brief.md``'s equity formula, cash + position_value, which
    ``DECISIONS.md`` #323 requires this sizer to match, collapsing to plain cash between trades since
    only one position is ever open at once) rather than off this run's static ``init_cash`` alone
    (AC-D3): entries are walked in signal order and ``compute_position_sizes`` is called once per
    trade with that trade's own equity, so the single-run path compounds exactly the way the paper
    order path does (AC-D5). Passing ``sizing=None`` explicitly restores the old, unsized, 100%-of-
    cash behaviour. With ``exit_spec`` given, the three ruled exit lines (lane B) replace the
    caller's own ``exits`` argument as this run's exit rule (AC-3). The per-fill slippage cost is
    recovered from the SAME rate vbt applied (DECISIONS #66/#67), not a shadow pass. Returns the
    six-slot RunResult the contract mandates.
    """
    import vectorbtpro as vbt  # the engine; imported lazily here so the helper package stays vbt-free at load
    from strategies.base.run_result import RunResult  # the locked six-slot contract (rebuilt to merge the funnel)
    from strategies.base.slippage import price_path_slippage_cost  # per-fill slippage cost (price path)
    from strategies.base.vbt_extraction import build_run_result  # pf -> RunResult helper (vbt-coupled)
    from strategies.helpers.annualization import resolve_annualization  # Phase-2 Unit 1: index-derived annualization
    from strategies.helpers.funnel import compute_signal_funnel  # Phase-2 Unit 3: the production signal funnel

    # #376 ruling 4 round 2 (E-R1-B1): the caller passed no sizing= at all (a strategy's own run(), e.g.
    # ma_cross.py, which this unit does not and cannot edit). Read the registry-resolved spec a container
    # entrypoint bound onto `data` (SizedStrategyInput.resolved_sizing, strategy.py) before this call; a
    # plain StrategyInput carries no such attribute (getattr's default) and a bound-but-None value means
    # nobody resolved one for this run, so both fall back to the ORIGINAL bare SizingSpec() default,
    # byte-unchanged for every host-side caller or probe built before this fix existed. An EXPLICIT
    # sizing= (including sizing=None, the documented opt-out) skips this branch entirely and always wins.
    if sizing is _SIZING_NOT_GIVEN:
        bound = getattr(data, "resolved_sizing", None)
        sizing = bound if bound is not None else SizingSpec()

    # Lane SW (5.8, `DECISIONS.md` #383 decision 2): a lead-in bar is loaded so an estimator can warm up
    # on it, never so a trade can be taken on it ("a lead-in is not a draw"). Read the SAME getattr way
    # resolved_sizing is (LeadInStrategyInput's own additive field, portfolio.py): a plain StrategyInput
    # or a bare SizedStrategyInput carries no such attribute, so getattr's None default means "no window
    # boundary to enforce" -- byte-unchanged for every host-side caller or probe built before this fix
    # existed. Masking happens BEFORE the exit_spec branch below, so a masked-out (never-taken) entry
    # inside the lead-in never seeds a ruled exit line either.
    entries_not_before = getattr(data, "entries_not_before", None)
    entries = mask_entries_before(entries, entries_not_before)

    # Lane P2d: on bars shorter than a day the volatility behind the stop and the size is the previous
    # completed day's, measured on daily bars; the labels come from the price index here (sizing.py reads
    # no clock time itself). None for a daily index: every call below then takes today's path unchanged.
    # Read only when something below uses a volatility, so an unsized, exit-free run never needs them.
    session_day = session_days(data.close.index) if (sizing is not None or exit_spec is not None) else None

    # Lane LB (5.8e, `DECISIONS.md` #406 ruling 2): a TRUE entry signal sitting on THIS frame's own
    # last bar has no next bar INSIDE the frame to fill on -- sizing.py's own "pos + 1 >= n_bars"
    # refusal, the wall row 5.8e names (an entry on a walk-forward slice's own last bar aborting the
    # whole run). Only reachable through `compute_position_sizes`, so this only matters when sizing
    # governs (`sizing is not None`); the `sizing=None` opt-out never calls that function at all and so
    # never hit this wall in the first place. The entry is ALWAYS removed from the series handed to
    # vbt from this point on, BEFORE the exit_spec branch below (so a removed entry never seeds a
    # ruled exit line either) and before the sizing pass, exactly as criterion 2 requires, whatever
    # LB-F1's own check below decides -- removing a signal vbt's own non-accumulating, single-position
    # rule would have ignored anyway changes nothing vbt would have done.
    entries_masked_at_end = 0     # this call's own count of last-bar entries that opened nothing (the fallback)
    fill_reach_shares = None      # the reached fill's own share count, when a reach was priced (else None)
    fill_reach_ts_out = None      # the reached bar's own timestamp, when a reach was priced (else None)
    reached_last_bar_entry = False  # decided AFTER final_exits is known, below (LB-F1 round 2 fold): True
    # only when the walk's own segments show the engine would actually have opened a trade on this
    # frame's own last bar, never from a bare `bool(entries.iloc[-1])` read alone.
    fill_reach = getattr(data, "fill_reach", None)  # LeadInStrategyInput's own SECOND additive field (default None)
    last_bar_signal_present = (   # a raw True sat on the last bar, whether or not the engine would ever act on it
        sizing is not None and len(entries.index) > 0 and bool(entries.iloc[-1])
    )
    # LB-F1 (round 2 fold): the series AS IT STANDS here -- the lead-in mask already applied, the
    # last-bar-specific removal below NOT yet applied -- is the "unmasked entries" the walk's own
    # segment check reads after final_exits is known. A plain reference, not a copy: `entries` is about
    # to be REASSIGNED (never mutated in place) below, so this name keeps pointing at the pre-removal
    # series once that reassignment happens.
    entries_before_end_mask = entries
    if last_bar_signal_present:
        entries = entries.copy()       # never mutate the caller's own series (this module's own precedent)
        entries.iloc[-1] = False       # removed either way: vbt (and compute_position_sizes) never sees it

    # Phase-2 Unit 1 (A2b-001/A2b-002): resolve the Sharpe annualization from the observed price index,
    # the bar size, the gaps and the span, instead of letting vbt assume 24/7 (factor 8760). `freq`
    # and `year_freq` are passed explicitly below so vbt's ann_factor = year_freq/freq = the observed
    # bars/year (AFML Ch 14). Correct for daily/intraday, equities/crypto, with no asset-class input.
    ann = resolve_annualization(data.close.index)

    # Lane B (AC-3): when the ruled three exit lines are requested, they REPLACE the caller's own
    # `exits` argument as this run's complete exit rule, rather than merging with it: two
    # independently-timed exit sources folded into one boolean series would hide which one actually
    # closed a given trade, and no ruling or probe asks for that merge. `exits` is used exactly as the
    # caller built it, unchanged, when exit_spec is None (additive, byte-identical to lane B).
    if exit_spec is not None:                # the ruled rule was requested for this run
        from strategies.helpers.exits import compute_exit_lines  # the ONE shared exit-line computation (AC-3)

        exit_lines = compute_exit_lines(
            data.close, data.high, data.low, data.open, entries,
            sizing if sizing is not None else SizingSpec(),  # AC-5: z always comes from A SizingSpec; when the
            # caller opted out of position sizing (sizing=None) the default spec is the only source left,
            # since ExitSpec itself deliberately carries no stop_multiple of its own.
            profit_ratio=exit_spec.profit_ratio, time_limit_bars=exit_spec.time_limit_bars,
            session_day=session_day,         # lane P2d: the same daily volatility sizing reads
        )
        final_exits = exit_lines.exits       # the ruled rule is this run's whole exit condition now
    else:
        final_exits = exits                  # unchanged: the caller's own signal, exactly as given

    # LB-F1 (round 2 fold, the lead's read 1): a raw True on the last bar is not always a fill the
    # engine would have made. vbt opens a trade on a bar only when the position is flat AND that bar
    # carries no exit signal (`_walk_trade_segments`'s own rules, the SAME non-accumulation and
    # ConflictMode.Ignore `vbt.Portfolio.from_signals` applies below); a strategy whose entry can fire
    # while a position is already open (the generator's own two-threshold crossing rules,
    # `generator/emit.py:187-199`), or a bar that also carries an exit, must record NO reach and NO
    # masked count for a fill that never would have happened. Decided by re-walking the SAME trade
    # segments the engine itself resolves to, over `entries_before_end_mask` (the series before the
    # last-bar-specific removal above, so an already-open position from an earlier real entry reads
    # correctly) and `final_exits` (the ruled exit lines when requested, else the caller's own signal,
    # exactly as vbt itself receives it): a segment opens on the last bar only when the walk agrees the
    # engine would have opened one there.
    if last_bar_signal_present:
        last_bar_pos = len(entries_before_end_mask.index) - 1  # the last bar's own integer position
        segments = _walk_trade_segments(entries_before_end_mask, final_exits)
        reached_last_bar_entry = bool(segments) and segments[-1][0] == last_bar_pos
        if reached_last_bar_entry and fill_reach is None:
            entries_masked_at_end = 1  # sheet item 2(i) default (a): opens nothing, counted
            SIZING_MONITOR_COUNTERS["entries_masked_at_end"] += 1  # in-memory only; see sizing.py's own comment

    # Job D (AC-D2/AC-D3/AC-D4): size/size_type are passed to from_signals ONLY when sizing is not the
    # explicit None opt-out; the new default (a real SizingSpec) means an ordinary call now sizes.
    size = None                              # vbt kwarg: absent means its own unbounded-amount default
    size_type = None                         # vbt kwarg: absent means its own default size_type
    fixed_fees = None                        # ibkr_costs unit: vbt kwarg default (global 0.0) unless sizing governs
    if sizing is not None:                   # the shared rule governs this run (the ordinary default, AC-D2)
        factor = getattr(data, "factor", None)  # lane D1: the per-day adjustment factor, None when the data has none
        size, ending_equity = _compounding_shares(  # AC-D3: one compute_position_sizes call PER TRADE, not per run
            data.close, data.high, data.low, data.open, entries, final_exits, sizing,
            factor=factor,                   # lane D1: real shares at the price actually traded
            init_cash=init_cash,             # only the FIRST trade reads this directly; later trades compound
            fee_rate=fees,                   # B1 fix, unchanged: this run's own commission fraction, already
            # in scope here as `fees` (passed to from_signals below as `fees=fees`); without this the
            # affordability refusal in compute_position_sizes always evaluated at zero commission and could
            # never fire.
            slippage_rate=slippage,          # lane KA (AC-5): this run's own real slippage fraction, so the
            # equity handed to the NEXT trade is net of the same slippage vbt itself will charge on every fill.
            session_day=session_day,         # lane P2d: day labels for bars shorter than a day (None for daily)
        )
        size_type = vbt.pf_enums.SizeType.Amount  # size counts literal units, matching sizing.py's own rounding
        # ibkr_costs unit: the IBKR Fixed cost needs a known share count per trade, which only exists in
        # this branch (the sizing=None opt-out never precomputes shares, so it cannot be priced here; see
        # this unit's own builder report for that scope note). Computed from the SAME size Series just
        # built above, so the entry/exit fee always matches the trade the run actually places.
        fixed_fees = _fixed_fee_series(
            data.close, data.open, entries, final_exits, size, buy_order_fee,
            exit_fee_func=sell_order_fee,  # FEE1b: the exit leg is a sell, priced with the sell fee
            factor=factor,           # lane D1: the fee on real shares at the price traded
            slippage_rate=slippage,  # AC-10 (KA round 5): this run's own real slippage rate, the SAME
            # input _compounding_shares just used above, so the fee vbt actually charges prices the
            # IDENTICAL executed price the sizing walk's own equity figure already assumed.
        )

        # Lane LB (5.8e): the reach itself, priced STRICTLY AFTER `size`/`fixed_fees` are built (so it
        # can never influence them: `entries` already has the last-bar signal removed above, before
        # either was computed, guarantee (i) by construction rather than by argument). Only reachable
        # when a real signal sat on this frame's own last bar AND a reach was actually granted.
        if reached_last_bar_entry and fill_reach is not None:
            import pandas as pd  # lazy, matching every other function in this module

            # Extend close/high/low/open by exactly ONE synthetic row at fill_reach.ts, so
            # compute_position_sizes's own "pos + 1 >= n_bars" check and its
            # "entry_price = open_.iloc[pos + 1]" read both resolve naturally to the reached bar's own
            # OPEN -- the SAME formula every ordinary next-bar fill uses (criterion 2's own words),
            # proved rather than restated. close/high/low at the synthetic row are NaN and never read:
            # every other value compute_position_sizes reads for this one entry (volatility, the stop
            # distance) comes from THIS frame's own last bar, at or before `pos`, unaffected by a row
            # appended after it.
            reach_index = pd.DatetimeIndex([fill_reach.ts])
            ext_close = pd.concat([data.close, pd.Series([float("nan")], index=reach_index)])
            ext_high = pd.concat([data.high, pd.Series([float("nan")], index=reach_index)])
            ext_low = pd.concat([data.low, pd.Series([float("nan")], index=reach_index)])
            ext_open = pd.concat([data.open, pd.Series([fill_reach.open], index=reach_index)])
            # Lane D1: the reached bar's own factor is not carried (FillReach holds exactly ts and open,
            # pinned by #406), so the reach fill converts at this frame's last bar's factor. Exact unless a
            # dividend or split takes effect on the reached bar itself; a known, stated limit (LANES row).
            ext_factor = None if factor is None else pd.concat(
                [factor, pd.Series([float(factor.iloc[-1])], index=reach_index)])
            reach_entry = pd.Series(False, index=ext_close.index, dtype=bool)
            reach_entry.iloc[-2] = True  # this frame's own last bar, second-to-last once extended
            sized_reach = compute_position_sizes(
                ext_close, ext_high, ext_low, ext_open, reach_entry, sizing,
                equity=ending_equity,        # the SAME running equity every ordinary trade in this leg compounds from
                fee_rate=fees,
                slippage_rate=slippage,      # AC-9 (KA round 4, SF2): this run's own real slippage rate, the same
                # rate every ordinary fill in this run uses, so a reach entry's own approval reads the real cost too.
                fixed_fee_func=buy_order_fee,  # AC-9 / FEE1b: the reach entry is a buy, Alpaca's buy fee, CALLED, never restated
                factor=ext_factor,           # lane D1
                # lane P2d: the extended index (one more bar at the reach time) labelled the same way; the
                # entry bar reads the previous completed day as before, the appended bar is never read.
                session_day=None if session_day is None else session_days(ext_close.index),
            )
            fill_reach_shares = float(sized_reach.shares.iloc[-2])
            fill_reach_ts_out = fill_reach.ts
            SIZING_MONITOR_COUNTERS["entries_masked_at_end"] += 1  # in-memory only; see sizing.py's own comment

    # Lane KA fix #1 (docs/units/KA/criteria.md AC-3/AC-5; exits_derivation_addendum_2026-09-01.md
    # section 2.2): vbt.Portfolio.from_signals, under PriceType.NextOpen, never places an exit order for
    # an exit signal sitting on the series' own LAST bar (no next bar exists to price the fill against),
    # so the trade stays "Open" with an unrealized mark-to-close and zero exit fee/slippage on that leg,
    # contradicting the addendum's own ruled rule ("close at the last bar ... at that bar's close"; "the
    # money moved"), confirmed directly against a minimal vbt call
    # (docs/units/KA/hand_derivation.md section 2). Detected the SAME way the entry-side LB-F1 fix above
    # detects a reached last-bar entry: re-walk the ACTUAL trade segments vbt itself will resolve to
    # (`_walk_trade_segments`, the identical non-accumulation / ConflictMode.Ignore rules `from_signals`
    # applies below), never a bare `bool(final_exits.iloc[-1])` read, so a raw exit signal that never
    # actually closes an open position (no position open there, or cancelled by a same-bar entry) never
    # triggers this branch. Applies regardless of whether `sizing` governs this run: the gap is in vbt's
    # own NextOpen fill mechanics, not in the sizing walk.
    n_bars = len(final_exits.index)                                  # this run's own bar count
    closing_segments = _walk_trade_segments(entries, final_exits)    # the SAME segments vbt will resolve to
    forced_close_at_end = bool(closing_segments) and closing_segments[-1][1] == n_bars - 1

    if forced_close_at_end:
        import pandas as pd  # lazy, matching every other function in this module

        # One synthetic bar, purely a pricing device (docs/units/KA/hand_derivation.md section 7, the
        # SAME technique FillReach/fill_reach_after already use for the entry-side last-bar edge,
        # DECISIONS.md #406 ruling 2, applied here to the EXIT side instead): its own OPEN is the true
        # last bar's own CLOSE, so from_signals' own NextOpen fill formula (the SAME formula every
        # ordinary next-bar fill uses, never a second one) prices this forced exit at exactly the
        # addendum's ruled "that bar's close". close/high/low there are NaN and never read for pricing
        # THIS fill (open is the only price a NextOpen fill reads); every RunResult slot the synthetic
        # bar touches is corrected below, before this function returns, so it never reaches the caller.
        true_last_ts = data.close.index[-1]                           # the true last bar this fill stands in for
        gap = (data.close.index[-1] - data.close.index[-2]) if len(data.close.index) > 1 else pd.Timedelta(days=1)
        synthetic_ts = true_last_ts + gap                             # an arbitrary, never-read label past it
        synth_index = pd.DatetimeIndex([synthetic_ts])
        synth_close = pd.concat([data.close, pd.Series([float("nan")], index=synth_index)])
        synth_high = pd.concat([data.high, pd.Series([float("nan")], index=synth_index)])
        synth_low = pd.concat([data.low, pd.Series([float("nan")], index=synth_index)])
        synth_open = pd.concat([data.open, pd.Series([float(data.close.iloc[-1])], index=synth_index)])
        synth_entries = pd.concat([entries, pd.Series([False], index=synth_index, dtype=bool)])
        synth_exits = pd.concat([final_exits, pd.Series([False], index=synth_index, dtype=bool)])
        synth_size = pd.concat([size, pd.Series([float("nan")], index=synth_index)]) if size is not None else None
        synth_fixed_fees = (
            pd.concat([fixed_fees, pd.Series([0.0], index=synth_index)]) if fixed_fees is not None else None
        )
    else:
        synth_close, synth_high, synth_low, synth_open = data.close, data.high, data.low, data.open
        synth_entries, synth_exits = entries, final_exits
        synth_size, synth_fixed_fees = size, fixed_fees

    # signal-at-close / fill-at-next-open. open/high/low feed next-open fills + MAE/MFE.
    # Same-bar conflict (Phase-2 Unit 4, A2a-007): no conflict_mode is passed, so vbt's default
    # ConflictMode.Ignore governs: if an entry and an exit are both True on the SAME bar, BOTH are
    # ignored (that bar produces no order). Safe and deterministic: it never silently picks one side.
    # Named here so the behavior is a documented default, not a hidden one.
    pf = vbt.Portfolio.from_signals(
        close=synth_close,                      # lane KA fix #1: the true series, plus the synthetic bar when forced
        entries=synth_entries,                  # long entry signals (no entry ever sits on the synthetic bar)
        exits=synth_exits,                      # lane B: the ruled rule when requested, else the caller's own
        open=synth_open,                        # next-open execution needs the open prices
        high=synth_high,                        # high feeds max favourable/adverse excursion
        low=synth_low,                          # low feeds max favourable/adverse excursion
        price=vbt.pf_enums.PriceType.NextOpen,  # execute at next bar's open (L14)
        init_cash=init_cash,                    # starting cash
        fees=fees,                              # commission from settings
        slippage=slippage,                      # slippage from settings
        direction="longonly",                  # long-only rule
        freq=ann["freq"],                       # the observed bar period (gap-robust; folds A2b-002)
        year_freq=ann["year_freq"],             # bar_period x observed-bars-per-year -> correct ann_factor
        size=synth_size,                        # job D: None (the explicit opt-out) unless sizing governs
        size_type=size_type,                    # job D: None (the explicit opt-out) unless sizing governs
        fixed_fees=synth_fixed_fees,             # ibkr_costs unit: per-fill IBKR Fixed cost, None (global 0.0) unless sizing governs
    )

    # The direct per-fill slippage cost: recover each order's clean reference from the rate vbt applied and
    # sum the per-fill shortfall (DECISIONS #66/#67). At a zero rate every reference equals its price, so 0.0.
    slippage_cost = price_path_slippage_cost(pf, slippage)

    # Extract all six canonical RunResult slots; price templates declare no regimes.
    rr = build_run_result(pf, indicators=indicators or {}, regimes={}, total_slippage_cost=slippage_cost)

    if forced_close_at_end:
        # Lane KA fix #1, the trim: bookkeeping-only (never a second equity model, every dollar figure
        # below is read straight off vbt's own real fill on the synthetic bar; only the BAR LABEL a
        # value is reported against is corrected, relabelling the synthetic bar's own row back onto the
        # true last bar it stands in for, per the addendum's own "closes at the last bar the store
        # carries" wording). `equity`: drop the synthetic bar's own row, but let the true last bar's
        # value be the POST-FILL figure vbt recorded one bar later (the fill only exists on the
        # synthetic bar because that bar is the only "next open" the forced fill had to price against).
        fixed_equity = rr.equity.iloc[:n_bars].copy()
        fixed_equity.iloc[-1] = float(rr.equity.iloc[n_bars])
        # `trades`/`drawdowns`: any timestamp vbt attached to the synthetic bar is relabelled onto the
        # true last bar (a pure label correction; the price/fee/pnl/status columns already came from
        # vbt's own real fill and are untouched).
        fixed_trades = rr.trades.copy()
        fixed_trades["exit_index"] = fixed_trades["exit_index"].replace({synthetic_ts: true_last_ts})
        fixed_drawdowns = rr.drawdowns.copy()
        for _dd_col in ("start_index", "valley_index", "end_index"):
            fixed_drawdowns[_dd_col] = fixed_drawdowns[_dd_col].replace({synthetic_ts: true_last_ts})
        # Lane KA finding KA-R1 (docs/units/KA/reads/read_1.md): `rr.stats` above was read off the
        # EXTENDED (321-bar) portfolio, so its four bar-index-dependent figures (sharpe_ratio,
        # max_drawdown, max_gross_exposure, turnover) all include the phantom synthetic bar. Rebuild
        # them from a SEPARATE, honestly-true-bars-only reconstruction of this run's own already-
        # relabeled fills (true_bars_forced_close_stats, the from_orders technique event_pipeline.py
        # already uses), and overwrite only those four keys; every other stats key is fill-derived,
        # not bar-count-dependent, and already correct (k03's own AC-3/AC-5 check).
        # KA round 4 (AC-8, B1): `true_bars_forced_close_stats` now ALSO returns `skew`/`kurtosis` for
        # the sweep path's own overwrite (sweep_portfolio.py). The single-run path's own `rr.stats`
        # (built_run_result) never carries either key at all, so this merge names its four keys
        # EXPLICITLY rather than splatting the whole returned dict with `**`: a blanket `**` merge
        # would silently add `skew`/`kurtosis` to every single-run's stats too, which k11's own
        # enumeration probe (research_lab/probes/known_answer) catches as an unnamed new key.
        _true_stats = true_bars_forced_close_stats(
            data.close, fixed_trades, fees, init_cash, ann["freq"], ann["year_freq"],
        )
        fixed_stats = {**rr.stats}
        for _key in ("sharpe_ratio", "max_drawdown", "max_gross_exposure", "turnover"):
            fixed_stats[_key] = _true_stats[_key]
        rr = RunResult(
            equity=fixed_equity, drawdowns=fixed_drawdowns, trades=fixed_trades,
            stats=fixed_stats, indicators=rr.indicators, regimes=rr.regimes,
        )

    # Phase-2 Unit 3 (audit concern #1): every price run is born with its signal funnel, the counts
    # that make a valid-zero distinguishable from a no-data zero. The funnel is read from the finished
    # `pf` plus the raw `entries`/`exits` (computed in compute_signal_funnel), and merged into stats
    # under a `funnel_` namespace. RunResult is a frozen dataclass, so this REBUILDS it (the same
    # two-step the event path uses to fold coverage, event_pipeline.py:420-425) rather than mutating
    # rr.stats in place. The counts are numeric, so they ride the stats dict (the only transport out of
    # run()); the categorical zero_reason is derived downstream by classify_zero_reason (the entrypoint).
    funnel = compute_signal_funnel(pf, entries, final_exits, indicators or {})  # lane B: the exits `pf` actually used
    if forced_close_at_end:
        # `pf` carries the synthetic bar too, so `compute_signal_funnel`'s own `bars_total` (read off
        # `pf.wrapper.index`) overcounts by exactly one; every OTHER funnel count is an order/trade
        # COUNT (unaffected by an extra, signal-free bar carrying no order of its own). `bars_after_warmup`
        # equals `bars_total` in the no-indicators case (this function's own branch, mirrored here rather
        # than re-imported) and is otherwise derived purely from `indicators`' own length, never `pf`'s,
        # so only THAT one case needs the same correction.
        funnel["bars_total"] -= 1
        if not indicators:
            funnel["bars_after_warmup"] = funnel["bars_total"]
    stats = {**rr.stats, **{f"funnel_{k}": v for k, v in funnel.items()}}  # namespaced, no collision with the 8 stats
    # Lane LB (5.8e): three additive stats keys, read nowhere `pf`/vbt ever touches (guarantee (i) by
    # construction: `entries` already excluded the reached/masked signal before `pf` was built above).
    # `fill_reach_ts` rides as whole UTC epoch seconds, matching every other timestamp column this
    # project's schema stores as an INTEGER.
    stats["entries_masked_at_end"] = entries_masked_at_end
    stats["fill_reach_shares"] = fill_reach_shares
    stats["fill_reach_ts"] = int(fill_reach_ts_out.timestamp()) if fill_reach_ts_out is not None else None
    return RunResult(
        equity=rr.equity, drawdowns=rr.drawdowns, trades=rr.trades,
        stats=stats, indicators=rr.indicators, regimes=rr.regimes,
    )
