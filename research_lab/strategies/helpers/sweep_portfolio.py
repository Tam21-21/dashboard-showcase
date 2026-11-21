"""Shared WIDE long-only portfolio build for a sweep batch (M5 1B §5.5, the sweep analogue of portfolio.py).

The single home for the vbt portfolio construction a sweep batch shares. ``long_only_signal_result``
(portfolio.py) builds ONE single-ticker single-param portfolio and extracts SCALAR stats; this builds ONE
WIDE ``from_signals`` over a batch of ``(combo x ticker)`` columns (R-PROD, the one-pass requirement) and
extracts the eight metrics + the signal funnel PER COLUMN.

The from_signals call is byte-identical to ``long_only_signal_result``'s (same kwargs, same execution
model: signal-at-close / next-open fill / 100%-cash / long-only) except ``fees``/``slippage`` are per-column
arrays (R-COST, §5.2) and ``freq``/``year_freq`` are the partition's index-derived annualization (R-CAL,
§5.3). That identity is the point: a single column of the wide pass equals the single-run portfolio
cell-for-cell (verified against the pinned build), which is the §11 assertion-3 cross-check.

Per-column extraction reuses the SAME single-path expressions (``build_run_result``'s stats dict via the
``_finite_or_none`` coercions, ``price_path_slippage_cost`` for the 8th metric, ``compute_signal_funnel``
for the funnel) read off the single-column slice ``pf[col]`` — so each cell is identical to what the proven
single path would have produced for that ``(ticker, combo)``, by construction. The per-column loop is the
"vectorized form is a build detail" the brief (§5.5) allows; the one wide ``from_signals`` is the measured
ceiling, the per-column scalar reads are secondary.

The vbt import is LAZY (inside the function) so importing ``strategies.helpers`` stays vbt-free at module
load, exactly as ``portfolio.py`` does (the catalog introspects this package without the engine).
"""
from __future__ import annotations  # lazy annotations: no runtime type evaluation, no eager imports

import logging  # stdlib, no import cost: makes a swallowed no_dominant_gap raise observable (F1)
from typing import TYPE_CHECKING  # keep the pandas import type-check-only

from strategies.helpers.alpaca_fees import buy_order_fee, sell_order_fee  # FEE1b (#459 item 4): Alpaca's charged fee of one order, the ONE fee source for the old engines
from strategies.helpers.exits import ExitSpec  # lane B (AC-3): the optional ruled three-line exit rule
from strategies.helpers.portfolio import (  # job D / ibkr_costs unit / lane LB (5.8e) round 2: the shared walk helpers
    CompoundingEquityError,  # lane D1b: an exit fill price that is undefined (a real halt) refuses one cell
    _compounding_shares,
    _fixed_fee_series,
    _walk_trade_segments,  # LB-F1: the same real-fill walk long_only_signal_result reads, reused here per column
)
from strategies.helpers.sizing import SIZING_MONITOR_COUNTERS, SizingError, SizingSpec, refusal_reason, session_days  # job D (AC-D2)/lane LB (5.8e): the shared rule's own config, now the default, and the shared last-bar counter

# Lane D1 / D1b: the refusals that mean "this one (combo, ticker) cell cannot be simulated honestly",
# caught by class (never a catch-all) wherever a sweep isolates one cell: a sizing refusal, or a trade
# whose exit fill price is undefined (reason=undefined_exit_price, a trading halt on the exit bar).
CELL_REFUSAL_ERRORS = (SizingError, CompoundingEquityError)

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.DataFrame referenced in the annotations below

logger = logging.getLogger(__name__)  # module logger; cheap at import, no vbt/pandas cost added


def _from_signals_wide(close, open, high, low, entries, exits, fees_arr, slippage_arr, freq, year_freq, init_cash,
                        size=None, size_type=None, fixed_fees=None):
    """The ONE wide from_signals call, shared by sweep_portfolio_cells (the 8-scalar cell write) AND
    cell_subportfolio (the M5 Track-C full-metrics cell RE-RUN).

    Centralizing the kwargs guarantees the two surfaces use the BYTE-IDENTICAL execution model
    (signal-at-close / next-open fill / long-only, per-column cost arrays, index-derived
    annualization), so a full-metrics re-run reproduces the stored cell (Gate 2) and the write path and the
    re-run path can never drift apart in a single kwarg. Lazy vbt import (the helper package stays vbt-free at
    load). Same-bar entry+exit -> vbt's default ConflictMode.Ignore, exactly as the single path.

    Lane A (sizing unit, AC-1): ``size``/``size_type`` default to ``None``, which is vbt's OWN
    signature default (docs/reference/vbt_pro_frozen_reference.md 5.1: "Every default is None in the
    signature... it means resolve from global settings at call time"). Job D (AC-D8) means BOTH real
    callers, ``sweep_portfolio_cells`` and ``cell_subportfolio``, now default to sizing rather than to
    ``None`` (each resolves its own default ``SizingSpec`` before it ever reaches this function), so a
    re-run through ``cell_subportfolio`` reproduces the cell an ordinary (unsized-override) write
    through ``sweep_portfolio_cells`` produced, by construction.

    ibkr_costs unit: ``fixed_fees`` defaults to ``None`` the same way (vbt's own global default,
    0.0), unless a caller passes a per-column IBKR Fixed cost frame built the same way ``size`` is
    (both callers build it whenever ``sizing`` governs, via ``_fixed_fee_series``).
    """
    import vectorbtpro as vbt  # the engine; lazy so importing strategies.helpers stays vbt-free at load
    return vbt.Portfolio.from_signals(
        close=close,                            # wide primary price frame
        entries=entries,                        # wide long entries
        exits=exits,                            # wide exits
        open=open,                              # next-open execution
        high=high,                              # MFE/MAE
        low=low,                                # MFE/MAE
        price=vbt.pf_enums.PriceType.NextOpen,  # execute at next bar's open (L14)
        init_cash=init_cash,                    # starting cash
        fees=fees_arr,                          # per-column commission (R-COST), the (1, ncol) row vector
        slippage=slippage_arr,                  # per-column slippage (R-COST)
        direction="longonly",                  # long-only rule
        freq=freq,                              # the partition bar period (gap-robust; R-CAL)
        year_freq=year_freq,                    # the partition annualization -> correct per-cell ann_factor
        size=size,                              # Lane A: None (vbt's own default) unless sizing was requested
        size_type=size_type,                    # Lane A: None (vbt's own default) unless sizing was requested
        fixed_fees=fixed_fees,                  # ibkr_costs unit: None (vbt's own default) unless sizing was requested
    )


def _own_history_bounds(close, col, union_first, union_last):
    """The member's own observed-bar window within the partition union (AD-M5-16): ``(first_valid, last_valid,
    ragged)``.

    ``ragged`` is True iff the member's window is narrower than the partition union (a late-listing or delisted
    member NaN-padded in the wide frame), in which case the caller slices the sub-portfolio + signal series to
    ``[first_valid, last_valid]`` before extracting ANY metric, so each metric reflects the member's real
    history, not the union span (the ragged-metric WINDOW fix). ``close`` carries unique-ticker columns; ``col``
    is the ``(param..., ticker)`` signal column with the ticker LAST, so the price-frame key is ``col[-1]``.
    Shared by sweep_portfolio_cells and cell_subportfolio so the own-history rule is defined in ONE place.
    """
    ticker = col[-1] if isinstance(col, tuple) else col          # the member ticker (the price-frame column key)
    first_valid = close[ticker].first_valid_index()             # the member's first real bar (None if all-NaN)
    last_valid = close[ticker].last_valid_index()               # the member's last real bar
    ragged = first_valid is not None and last_valid is not None and (
        first_valid != union_first or last_valid != union_last
    )  # the member's observed window is narrower than the partition union
    return first_valid, last_valid, ragged


def _forced_close_extension(close, open_, high, low, entries, final_exits):
    """Lane KA (docs/units/KA/criteria.md AC-3/AC-5, plan amendment 5 at `233acac5`): the SAME
    last-bar forced-exit gap `long_only_signal_result` had (portfolio.py) exists on this WIDE path too,
    per column: `vbt.Portfolio.from_signals` under `PriceType.NextOpen` never places an order for an
    exit signal with no next bar to price it against, so a trade closing exactly on the frame's own
    last bar stays "Open" with an unrealized mark, zero exit fee and zero exit slippage, contradicting
    the exits addendum's own ruled "close at the last bar's close" rule
    (`docs/units/KA/hand_derivation.md` section 2). Detected the SAME way (re-walking
    `_walk_trade_segments`, the SAME non-accumulation / ConflictMode.Ignore rules `from_signals` itself
    applies, over the SAME `entries`/`final_exits` the wide call is about to receive).

    Shared by both real callers of the wide `from_signals` (`sweep_portfolio_cells`'s own write path
    and `cell_subportfolio`'s own re-run, "Gate 2" of this file's own docstring: a re-run must
    reproduce a written cell), so the extension logic lives in exactly one place rather than drifting
    into two copies.

    When at least one column needs it, every column gains ONE synthetic bar (a single wide
    `from_signals` call needs one shared index, so a per-column-only extension is not possible): its
    own OPEN is that bar's own reference the forced fill prices against, the same technique
    `FillReach`/`fill_reach_after` (portfolio.py) already use for the entry-side last-bar edge,
    applied here to the exit side. A FORCING column's own close/high/low/open at the synthetic bar are
    its own true last bar's own close, repeated (a flat, no-new-information filler): this keeps
    `_own_history_bounds`'s own `last_valid_index()` extending to match the new `union_last`, so this
    column's own per-cell metrics below are NEVER ragged-sliced away from the post-fill state the
    synthetic bar exists to price (the trim `long_only_signal_result` needs for its own raw equity/
    trades output is unnecessary here: `sweep_portfolio_cells` returns only SCALAR per-column stats,
    never a raw equity curve or a trades table with an exposed timestamp column, so there is nothing
    to relabel). A NON-forcing column's own four series stay NaN at the synthetic bar, so the SAME
    EXISTING ragged-window logic (`_own_history_bounds`, already in this file) auto-excludes the
    synthetic bar from every OTHER column's own metrics (its own `last_valid_index()` stops at its own
    true last bar, `ragged` becomes True, and the caller's own existing `sub.loc[first_valid:last_valid]`
    slice drops the synthetic row) -- no column that never needed the extension has its own stats
    touched by another column's own forced close, with no second, per-column special case needed here.

    Returns ``(close, open_, high, low, entries, final_exits, forced_close_cols)`` unchanged (the
    identical objects, `forced_close_cols=[]`) when no column needs the extension.
    """
    n_bars = len(final_exits.index)                                   # this batch's own real bar count
    forced_close_cols = []                                            # columns whose own last real trade forces here
    for col in entries.columns:                                       # every (combo, ticker) column in this batch
        segments = _walk_trade_segments(entries[col], final_exits[col])  # the SAME segments vbt will resolve to
        if segments and segments[-1][1] == n_bars - 1:                # this column's own last trade closes on the last bar
            forced_close_cols.append(col)

    if not forced_close_cols:                                         # nothing in this batch needs the extension
        return close, open_, high, low, entries, final_exits, []

    import pandas as pd  # lazy, matching every other function in this module

    true_last_ts = close.index[-1]                                    # the true last bar every forced fill stands in for
    gap = (close.index[-1] - close.index[-2]) if len(close.index) > 1 else pd.Timedelta(days=1)
    synthetic_ts = true_last_ts + gap                                 # an arbitrary, never-read-as-a-real-date label
    synth_index = pd.DatetimeIndex([synthetic_ts])

    forcing_tickers = {                                               # the underlying tickers that actually need it
        (col[-1] if isinstance(col, tuple) else col) for col in forced_close_cols
    }
    # Row 6.13b (lane KF): `pd.concat` DROPS a column index's own `.name` (a plain Index, e.g.
    # `close.columns.name == "ticker"`) or every level's own name (a `MultiIndex`, e.g.
    # `entries.columns.names == [..., "ticker"]`) whenever the OTHER frame being concatenated
    # carries an unnamed column index -- which the dict-built synthetic frames below always did
    # before this fix, since `pd.DataFrame({col: [...] for col in cols})` and `pd.DataFrame([{...}])`
    # both build a plain, UNNAMED column Index regardless of what `cols` itself was named. The wide
    # `from_signals` call right after this function returns (`_from_signals_wide`) broadcasts a
    # ticker-only `close` against a `(combo..., ticker)` `entries`/`exits` by MATCHING the shared
    # "ticker" level NAME across the two frames (vbt's own cross-dimensional broadcast rule); once
    # that name is gone, vbt can no longer tell which axis of `entries` is the ticker axis and silently
    # emits a result wrapper whose own columns are `close`'s bare ticker labels (1 level, 7 columns)
    # instead of `entries`' own `(combo..., ticker)` tuples (28 columns on a real multi-combo sweep) --
    # so every later `pf[col]` lookup keyed by an `entries` column tuple raises `KeyError` on exactly
    # the column tuple that forced this synthetic bar's own ticker (reproduced on `strong_close_
    # follow_through`/MAG-7 2014-2019 because its NVDA leg's last trade closes on the frame's own
    # last bar; `weak_close_bounce` over the same window never forces a close, so it never hit this
    # path and its own sweep scenario stayed green). The fix: build each synthetic frame WITH the
    # real frame's own column Index object (`columns=close.columns` / `columns=entries.columns`),
    # never a dict, so both sides of every concat already carry the identical, named column index
    # before pandas ever has to decide whose name wins; and reassign `.columns` to that same captured
    # object right after each concat as a second, cheap guarantee that survives a future pandas
    # version changing its own union-naming rule.
    close_columns = close.columns                                    # captured BEFORE any concat, to restore after
    entries_columns = entries.columns                                 # the real (combo..., ticker) MultiIndex, named
    final_exits_columns = final_exits.columns                         # same shape as entries_columns, named the same
    synth_row = [                                                     # one value per ticker column, this one new bar
        float(close[ticker].iloc[-1]) if ticker in forcing_tickers else float("nan") for ticker in close_columns
    ]
    # Built WITH close's own column Index (never a dict), so this frame's columns are the IDENTICAL,
    # named object close/open/high/low already carry: concatenating two frames that share one column
    # Index object can never disagree about its own name.
    synth_frame = pd.DataFrame([synth_row], index=synth_index, columns=close_columns)
    # a flat, no-new-information bar for a forcing ticker (its own true last close, repeated), NaN for
    # every ticker that never needed the extension (excluded again below by the existing ragged logic).
    close = pd.concat([close, synth_frame])
    close.columns = close_columns                                     # belt-and-braces: restore the name regardless
    open_ = pd.concat([open_, synth_frame])
    open_.columns = close_columns
    high = pd.concat([high, synth_frame])
    high.columns = close_columns
    low = pd.concat([low, synth_frame])
    low.columns = close_columns
    # Same fix for entries/final_exits: a one-row, all-False frame built WITH entries' own (named)
    # MultiIndex as its columns, never re-derived from a dict, so the concat's two sides already
    # agree on every level's own name.
    entries = pd.concat([entries, pd.DataFrame(False, index=synth_index, columns=entries_columns)])
    entries.columns = entries_columns                                 # belt-and-braces: restore every level's name
    final_exits = pd.concat([final_exits, pd.DataFrame(False, index=synth_index, columns=final_exits_columns)])
    final_exits.columns = final_exits_columns
    return close, open_, high, low, entries, final_exits, forced_close_cols


def _factor_column(factor, ticker):
    """Lane D1: one ticker's per-day adjustment factor Series out of the wide factor frame, or None
    (factor 1) when no frame was given or this ticker carries none (a non-eodhd member)."""
    if factor is None or ticker not in factor.columns:
        return None
    return factor[ticker]


def cell_subportfolio(close, open, high, low, entries, exits, fees_arr, slippage_arr, freq, year_freq, init_cash,
                       *,                        # AC-D8: sizing is keyword-only, matching sweep_portfolio_cells's own
                       sizing: SizingSpec | None = SizingSpec(), factor=None):
    """Return the own-history-sliced sub-portfolio for a SINGLE ``(ticker x combo)`` column (M5 Track-C
    full-metrics cell RE-RUN, vectorized mode).

    Builds the SAME wide from_signals (``_from_signals_wide``) + own-history slice (``_own_history_bounds``)
    sweep_portfolio_cells produces a cell from, so the FULL metric set extracted from this sub (via
    ``build_run_result`` -> ``compute_full_metrics``) reproduces the stored 8-scalar cell by construction
    (Gate 2; the equality of ``from_value(sub.value)`` and ``sub.returns`` for both full-history and ragged
    columns is MCP-verified in the pinned build). Asserts exactly one column: this is a single-cell re-run,
    never a batch. Returns the (own-history-sliced) sub-portfolio; the caller runs ``build_run_result`` over it.

    AC-D8: ``sizing`` defaults to a real ``SizingSpec`` here too (mirroring ``sweep_portfolio_cells``'s
    own new default, job D), so an ORDINARY re-run (no override on either side) reproduces an ORDINARY
    write, by construction; passing ``sizing=None`` restores the pre-job-D unsized behaviour, matching
    whichever explicit override the write path used for the cell being re-run. The caller of THIS
    function is responsible for carrying through whatever sizing spec the stored cell was actually
    written with. This function proves the FUNCTION-LEVEL capability, never that the M5 cube itself
    persists which spec produced a historical cell (a separate, larger persistence question, out of
    this lane's scope; see the builder report).
    """
    if entries.shape[1] != 1:  # a single-cell re-run, not a batch: fail loud on a malformed caller
        raise ValueError(f"cell_subportfolio expects exactly one (ticker, combo) column, got {entries.shape[1]}")
    col = entries.columns[0]                                     # the one (param..., ticker) column

    # Lane LB (5.8e): the SAME last-bar fallback sweep_portfolio_cells applies, over this one column,
    # so a re-run of a stored cell never hits sizing.py's own last-bar refusal either. No reach is ever
    # attempted here (finding 2/criterion 3: the vectorized sweep's frames never carry one).
    # LB-F1 (round 2 fold, the lead's read 1): the entry is always removed from what vbt sees when a
    # raw True sits on the last bar, but the counter is incremented only when the walk's own segments
    # (`_walk_trade_segments`, the SAME non-accumulation and same-bar-conflict rules vbt itself
    # applies) show the engine would actually have opened a trade there -- a stale True from an
    # already-open position (an earlier, real entry never closed) or a same-bar exit is not a fill the
    # engine would ever have made, and must not count as one. `exits` here is this cell's own FINAL
    # exit series already (this function takes no exit_spec of its own), so no deferred computation is
    # needed the way the two exit_spec-bearing callers need.
    if sizing is not None and len(entries.index) > 0 and bool(entries.iloc[-1, 0]):
        last_bar_pos = len(entries.index) - 1  # the last bar's own integer position
        segments = _walk_trade_segments(entries.iloc[:, 0], exits.iloc[:, 0])
        engine_would_open = bool(segments) and segments[-1][0] == last_bar_pos
        entries = entries.copy()  # never mutate the caller's own frame
        entries.iloc[-1, 0] = False  # removed either way: vbt never sees it
        if engine_would_open:
            SIZING_MONITOR_COUNTERS["entries_masked_at_end"] += 1  # in-memory only; see sizing.py's own comment

    # AC-D8: the SAME per-trade compounding walk sweep_portfolio_cells's own write path uses, over
    # this one column, so a sized cell is reproduced rather than silently re-run unsized.
    size_wide = None                         # vbt kwarg: absent means its own unbounded-amount default
    size_type = None                         # vbt kwarg: absent means its own default size_type
    fixed_fees_wide = None                   # ibkr_costs unit: vbt kwarg default (global 0.0) unless sizing governs
    if sizing is not None:                   # sizing governs this re-run (the ordinary default, AC-D8)
        import pandas as pd  # lazy: only needed to assemble the one-column wide size frame when sizing governs
        import vectorbtpro as vbt  # lazy: SizeType is a vbt enum; the helper package stays vbt-free at load

        ticker = col[-1] if isinstance(col, tuple) else col     # the member ticker (the price-frame column key)
        shares, _cell_subportfolio_ending_equity = _compounding_shares(  # job D's one shared compounding walk (AC-D3/AC-D4), same as the write path
            close[ticker], high[ticker], low[ticker], open[ticker], entries[col], exits[col], sizing,
            init_cash=init_cash, fee_rate=fees_arr[0][0],  # a single-cell re-run: column 0 is the only column
            factor=_factor_column(factor, ticker),  # lane D1: real shares at the price actually traded
            slippage_rate=slippage_arr[0][0],  # lane KA (AC-5): this run's own real slippage rate, the same
            # single-column-re-run reading `fee_rate` already uses, so a re-run's own equity walk nets the
            # SAME fee and slippage the write path's own equity walk does.
            session_day=session_days(close.index),  # lane P2d: the SAME day labels the write path uses, so a
            # stored hourly cell re-runs with the same sizes (None for a daily index)
        )
        size_wide = pd.DataFrame({col: shares})  # one column, aligned to entries' own single column
        size_type = vbt.pf_enums.SizeType.Amount  # size counts literal units, matching sizing.py's own rounding
        # ibkr_costs unit: the same per-trade entry/exit fee walk the single-run path uses, over this
        # one column, computed from the SAME shares Series just built above.
        fixed_fees_series = _fixed_fee_series(
            close[ticker], open[ticker], entries[col], exits[col], shares, buy_order_fee,
            exit_fee_func=sell_order_fee,  # FEE1b: the exit leg is a sell
            factor=_factor_column(factor, ticker),  # lane D1
            slippage_rate=slippage_arr[0][0],  # AC-10 (KA round 5): the SAME single-column slippage
            # rate _compounding_shares just used above, so this re-run's own charged fee prices the
            # identical executed price the write path would have charged.
        )
        fixed_fees_wide = pd.DataFrame({col: fixed_fees_series})  # one column, aligned to entries' own single column

    # Lane KA fix #1 for this re-run path too ("Gate 2": a re-run must reproduce what the write path
    # actually wrote, so both need the identical forced-close treatment or they disagree on this exact
    # edge). Shared detection/extension logic, never a second copy: _forced_close_extension.
    close, open, high, low, entries, exits, forced_close_cols = _forced_close_extension(
        close, open, high, low, entries, exits,
    )
    if forced_close_cols:  # the one synthetic bar _forced_close_extension just appended needs a size/fee row too
        import pandas as pd  # lazy, matching every other function in this module

        synth_index = entries.index[-1:]
        if size_wide is not None:
            size_wide = pd.concat(
                [size_wide, pd.DataFrame({c: [float("nan")] for c in size_wide.columns}, index=synth_index)]
            )
        if fixed_fees_wide is not None:
            fixed_fees_wide = pd.concat(
                [fixed_fees_wide, pd.DataFrame({c: [0.0] for c in fixed_fees_wide.columns}, index=synth_index)]
            )

    pf = _from_signals_wide(close, open, high, low, entries, exits, fees_arr, slippage_arr, freq, year_freq,
                             init_cash, size=size_wide, size_type=size_type, fixed_fees=fixed_fees_wide)
    sub = pf[col]                                               # the single-column slice == this cell's portfolio
    first_valid, last_valid, ragged = _own_history_bounds(close, col, close.index[0], close.index[-1])
    if ragged:                                                  # slice to the member's own history (AD-M5-16)
        sub = sub.loc[first_valid:last_valid]
    return sub


def cell_metric_columns(sub, sharpe, slippage_rate: float) -> dict:
    """CS Amendment 1, A1: the TEN L-2 metric columns (researchdb._RUN_STATS_COLUMNS plus
    _CELL_DIST_COLUMNS), computed by the EXACT expressions ``sweep_portfolio_cells`` writes for a
    vectorized cell, pulled into one function so the write path below and the replay path
    (``compute_metrics_entrypoint.py``) call the SAME code and can never drift apart. This is the fix
    for the measured skew/kurtosis gap (about 2.5e-15 and 4e-15, docs/units/JC/reads/
    jc13_measurement.md): before this helper existed, the replay derived those two columns from
    ``equity.pct_change()`` (run_sweep_entrypoint._run_result_cell's own formula, correct for the
    zero/inline modes but never how a vectorized cell was written), a different floating-point path
    to the same quantity than ``sub.returns.skew()``/``kurtosis()`` here.

    ``sharpe`` is a parameter, not computed here, because ITS OWN computation branches on raggedness
    (the caller's own own-history-window logic below); a REPLAY's single-member re-run is never
    ragged against itself (Amendment 1's own "Unverified, and it fails closed" paragraph), so the
    replay side always passes ``_finite_or_none(sub.sharpe_ratio)``, the same plain property the
    full-history branch below uses. ``sub`` is the own-history-sliced sub-portfolio
    (``cell_subportfolio``'s own return value, or ``pf[col]`` sliced the same way inside the loop
    below); ``slippage_rate`` is this column's own scalar slippage rate for the 8th metric.
    """
    from strategies.base.vbt_extraction import _finite_or_none  # the exact single-path nullable-ratio coercion
    from strategies.base.slippage import price_path_slippage_cost  # the price-path per-fill slippage cost

    return {
        "total_return": float(sub.total_return),                 # overall return fraction (NOT NULL)
        "sharpe_ratio": sharpe,                                  # caller's own own-history / full-history choice
        "max_drawdown": abs(float(sub.max_drawdown)),            # positive fraction (NOT NULL)
        "num_trades": int(sub.trades.count()),                   # closed+EOD-open trade count (NOT NULL)
        "win_rate": _finite_or_none(sub.trades.win_rate),        # None if undefined (no trades)
        "total_fees_paid": float(sub.orders.fees.sum()),         # sum of order fees (NOT NULL)
        "total_slippage_cost": float(price_path_slippage_cost(sub, slippage_rate)),  # 8th metric, per-fill (NOT NULL)
        "profit_factor": _finite_or_none(sub.trades.profit_factor),  # None when gross loss is 0 (vbt inf -> None)
        "skew": _finite_or_none(sub.returns.skew()),             # third-moment shape; None on a too-short window
        "kurtosis": _finite_or_none(sub.returns.kurtosis()),     # excess kurtosis; None on a too-short window
    }


def _cscv_block_sums(bar_returns: list, s_blocks: int) -> "tuple[list, int]":
    """Lane C5 (set 16c, #427, plan amendment 4): the SAME aggregation
    `dashboard.backend.judge_checks.build_cscv_block_sums` performs, DUPLICATED here rather than
    imported, because this package must never import `dashboard.backend` (the engine/backend
    boundary this file's own module docstring precedent already crosses this way for
    `strategies.helpers.costs`'s constants, and `run_sweep_entrypoint.py`'s own comment states
    outright for `hashlib`/`param_config_hash`). Splits `bar_returns` (one column's per-bar simple
    return over the partition's own shared index; `None` where the member has no price for that
    bar) into `s_blocks` equal contiguous blocks of `(count, sum, sumsq)`, dropping the leftover
    bars from the START so every block is equal (AFML's own "equal dimensions" requirement); a
    `None` entry contributes 0 to both sums while still counting toward its block's own count
    (AC-2: "a bar a member has no price for contributes a return of 0"), so every trial's blocks
    share the SAME count and the matrix stays synchronous. Keep both copies identical by hand;
    `scripts/probes/check5_cscv`'s `c10` is the probe that would catch a drift in either.
    """
    total_bars = len(bar_returns)
    bars_dropped = total_bars % s_blocks
    kept = bar_returns[bars_dropped:]
    block_width = len(kept) // s_blocks
    blocks = []
    for i in range(s_blocks):
        chunk = kept[i * block_width:(i + 1) * block_width]
        count = len(chunk)
        total = 0.0
        totalsq = 0.0
        for r in chunk:
            if r is not None:
                total += r
                totalsq += r * r
        blocks.append((count, total, totalsq))
    return blocks, bars_dropped


def sweep_portfolio_cells(
    close: "pd.DataFrame",          # wide close for this batch (columns = the batch's (combo x ticker) product)
    open: "pd.DataFrame",           # wide open  (next-open fills), same columns/index
    high: "pd.DataFrame",           # wide high  (MFE/MAE)
    low: "pd.DataFrame",            # wide low   (MFE/MAE)
    entries: "pd.DataFrame",        # wide boolean entries, same columns
    exits: "pd.DataFrame",          # wide boolean exits, same columns
    indicators: dict,               # {name: wide DataFrame} warmup series, same columns (for the per-cell funnel)
    fees_arr,                       # per-column fees row-vector, shape (1, ncol) (R-COST)
    slippage_arr,                   # per-column slippage row-vector, shape (1, ncol)
    slippage_rates: list,           # per-column slippage rate scalars (aligned to columns) for the 8th metric
    freq,                           # the partition bar period (pd.Timedelta), R-CAL
    year_freq,                      # the partition year_freq (pd.Timedelta) so vbt ann_factor = the index-derived factor
    init_cash: float,               # starting cash in $ (the ma_cross reference default flows through)
    *,                               # Lane A (sizing unit): sizing is keyword-only, matching long_only_signal_result
    factor: "pd.DataFrame | None" = None,  # lane D1: wide per-day adjustment factor, columns = tickers that
    # carry one (eodhd daily); a ticker absent from it (or factor=None) sizes at factor 1, as before D1
    sizing: SizingSpec | None = SizingSpec(),  # job D (AC-D2/AC-D8): the shared rule is now the DEFAULT for
    # this batch too, not an option nobody requests; `sizing=None` stays a valid, EXPLICIT opt-out (see
    # long_only_signal_result's own docstring for the same contract on the single-run path).
    exit_spec: ExitSpec | None = None,  # lane B (AC-3): the ruled three exit lines, per column. None (the
    # default) is unchanged, additive behaviour: each column's own `exits` argument is used exactly as
    # given. When given, REPLACES each column's own exits, mirroring long_only_signal_result exactly.
    refusals: "list | None" = None,  # lane D1: when a list is given, a SizingError for one column is recorded
    # here and that column is left out of the batch; None (the default) keeps the old abort-the-batch behaviour
    s_blocks: int | None = None,  # lane C5 (set 16c, #427, plan amendment 4): check 5's own frozen
    # cscv_split_count (S), forwarded ONLY when the container resolved one for this sweep. None
    # (the default) is byte-identical, additive, old behaviour: no extra key on any cell dict, no
    # extra pass over `pf` -- the block sums below are read off the SAME per-column `sub` this
    # loop already extracts stats from, not a second wide from_signals (R-PROD stands).
) -> list:
    """Run ONE wide from_signals over the batch and return one per-column cell dict.

    Returns a list aligned to ``entries.columns``: each element is
    ``{"column": <the column-index tuple>, "stats": {<8 metrics>}, "funnel": {<9 counts>}}``. The caller
    (the engine) maps each column tuple to ``(variation_id, ticker, param_json, dataset_sha256)`` and derives
    ``zero_reason`` from the funnel counts (classify_zero_reason), then writes the cube cell + the trial.

    Job D (AC-D2/AC-D3, `DECISIONS.md` #321): with ``sizing`` governing (the new default), this is the
    WIDE SWEEP path's own use of the ONE shared place (``strategies.helpers.sizing.compute_position_sizes``,
    via ``portfolio._compounding_shares``) ``long_only_signal_result`` (the single-run path) also reads
    sizing from, so the two can never drift into separately hand-written size logic. Sizing is computed
    PER COLUMN (each ``(combo, ticker)`` column reads its own ticker's OHLC, keyed by ``col[-1]``, the
    same convention ``_own_history_bounds`` uses), and PER TRADE within that column (AC-D3: the equity
    realized so far, not this batch's static ``init_cash`` alone), then assembled into a wide size frame
    BEFORE the one wide ``from_signals`` call below, because vbt needs the whole size array up front. A
    ``SizingError`` raised for any one column propagates out of this function uncaught, aborting the
    WHOLE batch rather than silently degrading or skipping just that column (this unit's own logged
    design choice: see the builder report for the argument).

    Lane D1 (2026-10-07, Tamer: one unsizeable stock must not kill a sweep): when the caller passes a
    ``refusals`` list, a ``SizingError`` for one column is caught (that exception class only, never a
    catch-all; lane D1b adds ``CompoundingEquityError``, an undefined exit fill price, through
    ``CELL_REFUSAL_ERRORS``), recorded as ``{"column", "error", "reason", "message"}`` in ``refusals``, and the column is
    left out of the batch; the returned cells then cover only the columns that sized, in their original
    order. The caller is responsible for recording each refusal where a person can see it. With
    ``refusals=None`` the old behaviour above is unchanged.
    """
    import vectorbtpro as vbt  # the engine; imported lazily here so the helper package stays vbt-free at load
    # _finite_or_none stays imported here for the ragged/full-history sharpe branch below; the ten
    # metric columns' own _finite_or_none and price_path_slippage_cost calls now live inside
    # cell_metric_columns (CS Amendment 1, A1), which imports both itself.
    from strategies.base.vbt_extraction import _finite_or_none  # the exact single-path nullable-ratio coercion
    from strategies.helpers.funnel import compute_signal_funnel  # the production signal funnel (per column)
    from strategies.helpers.annualization import (  # the per-member OWN-history annualization (AD-M5-16 factor fix)
        AnnualizationError, resolve_annualization,
    )

    # Lane LB (5.8e, `DECISIONS.md` #406 ruling 2): a TRUE entry sitting on THIS batch's own last bar
    # has no next bar INSIDE the frame to fill on (sizing.py's own "pos + 1 >= n_bars" refusal, reached
    # per column through `_compounding_shares` below). None of the three sweep modes ever carries a
    # reach (finding 2/criterion 3: their frames clip INCLUSIVELY at window_end_ts, so the fallback is
    # the only outcome here, never a priced reach); the entry is removed from what vbt ever sees either
    # way, per column, before the exit-line computation below could seed a ruled exit on a position
    # that will never open. `entries_masked_at_end_by_col` rides on each column's own emitted cell dict.
    # LB-F1 (round 2 fold, the lead's read 1): whether the COUNT is recorded per column is decided
    # AFTER final_exits is built below, by the walk's own segments over the UNMASKED entries -- a raw
    # True from an already-open position or a same-bar exit conflict is not a fill the engine would
    # ever have made.
    entries_masked_at_end_by_col: dict = {}  # {column: 1}, one entry per column the walk confirms opened, absent means 0
    entries_before_end_mask = entries  # LB-F1: the frame as it stands here, before the last-bar-specific removal
    last_bar_signal_cols: list = []  # columns carrying a raw True on the last bar, whether or not it would open
    if sizing is not None and len(entries.index) > 0:
        last_ts = entries.index[-1]
        last_bar_signal_cols = [col for col in entries.columns if bool(entries.loc[last_ts, col])]
        if last_bar_signal_cols:
            entries = entries.copy()  # never mutate the caller's own frame (portfolio.py's own precedent)
            for col in last_bar_signal_cols:
                entries.loc[last_ts, col] = False  # always removed from what vbt sees, whatever the walk finds below

    # Lane B (AC-3): when the ruled three exit lines are requested, they REPLACE each column's own
    # `exits` argument, mirroring long_only_signal_result exactly (see that function's own comment for
    # why replacing rather than merging is the honest contract). Computed PER COLUMN, keyed by
    # ``col[-1]`` (the same convention `_own_history_bounds` uses), because compute_exit_lines takes
    # single-ticker Series, never a wide frame.
    # Lane P2d: the day labels of the shared price index (None for a daily index), computed once for the
    # whole batch: every column shares the one index. Used by the exit lines and the sizing walk below.
    session_day = session_days(close.index) if (sizing is not None or exit_spec is not None) else None
    if exit_spec is not None:                # the ruled rule was requested for this whole sweep batch
        import pandas as pd  # lazy: only needed to assemble the wide exits frame when exit_spec is given
        from strategies.helpers.exits import compute_exit_lines  # the ONE shared exit-line computation (AC-3)

        exit_spec_sizing = sizing if sizing is not None else SizingSpec()  # AC-5: z needs SOME SizingSpec
        # even when the caller opted out of position sizing (sizing=None); ExitSpec carries no
        # stop_multiple of its own, so the default spec is the only source left in that case.
        exits_columns = {}  # {(combo..., ticker): per-bar boolean exits Series}, one entry per batch column
        for col in entries.columns:          # every (combo, ticker) column, same order as entries/exits
            ticker = col[-1] if isinstance(col, tuple) else col  # the member ticker (the price-frame column key)
            exit_lines = compute_exit_lines(
                close[ticker], high[ticker], low[ticker], open[ticker], entries[col], exit_spec_sizing,
                profit_ratio=exit_spec.profit_ratio, time_limit_bars=exit_spec.time_limit_bars,
                session_day=session_day,     # lane P2d: the same daily volatility sizing reads
            )
            exits_columns[col] = exit_lines.exits
        final_exits = pd.DataFrame(exits_columns)  # one column per (combo, ticker), aligned to entries' own columns
    else:
        final_exits = exits                  # unchanged: each column's own signal, exactly as given

    # LB-F1 (round 2 fold, the lead's read 1): now that final_exits is known, decide PER COLUMN
    # whether the engine would actually have opened a trade on the last bar, by the SAME walk vbt's
    # own from_signals resolves to (flat position, no same-bar exit conflict, `_walk_trade_segments`).
    # Read over `entries_before_end_mask` (the frame before the last-bar-specific removal above, so an
    # already-open position from an earlier real entry reads correctly) and `final_exits` (the ruled
    # exit lines when requested, else each column's own signal, exactly as vbt itself receives it): a
    # segment opens on the last bar only when the walk agrees the engine would have opened one there.
    if last_bar_signal_cols:
        last_bar_pos = len(entries_before_end_mask.index) - 1  # the last bar's own integer position
        for col in last_bar_signal_cols:
            segments = _walk_trade_segments(entries_before_end_mask[col], final_exits[col])
            if segments and segments[-1][0] == last_bar_pos:
                entries_masked_at_end_by_col[col] = 1
                SIZING_MONITOR_COUNTERS["entries_masked_at_end"] += 1  # in-memory only; see sizing.py's own comment

    # Job D (AC-D2/AC-D8): size/size_type stay None (vbt's own default, so from_signals behaves exactly
    # as it always did) unless sizing is not the explicit None opt-out.
    size_wide = None                         # vbt kwarg: absent means its own unbounded-amount default
    size_type = None                         # vbt kwarg: absent means its own default size_type
    fixed_fees_wide = None                   # ibkr_costs unit: vbt kwarg default (global 0.0) unless sizing governs
    if sizing is not None:                   # the shared rule governs this batch (the ordinary default, AC-D2)
        import pandas as pd  # lazy: only needed to assemble the wide size frame when sizing governs

        size_columns = {}  # {(combo..., ticker): per-bar share-count Series}, one entry per batch column
        refused_cols = set()  # lane D1: columns whose sizing refused, recorded in `refusals`
        fixed_fees_columns = {}  # {(combo..., ticker): per-bar IBKR-fee Series}, one entry per batch column
        # B1 fix: enumerate so `j` is this column's own positional index into `fees_arr`, matching the
        # shape this function's own later extraction loop uses (`for j, col in enumerate(entries.columns):`
        # below), so fees_arr[0][j] is the fee OF this column and never a different one (round-1 code-quality
        # finding B1's trap one: a constant or misaligned index would size against another ticker's commission,
        # a plausible-looking but silently wrong number).
        for j, col in enumerate(entries.columns):  # every (combo, ticker) column, same order as entries/exits/fees_arr
            ticker = col[-1] if isinstance(col, tuple) else col  # the member ticker (the price-frame column key)
            try:  # lane D1: a SizingError for this one column is recorded and the column left out (see docstring)
                col_shares, _ = _compounding_shares(  # AC-D3: one compute_position_sizes call PER TRADE in this column
                    close[ticker], high[ticker], low[ticker], open[ticker], entries[col], final_exits[col], sizing,
                    init_cash=init_cash,         # only this column's FIRST trade reads this directly; later trades compound
                    # B1 fix, trap two: fees_arr is a plain list of lists on the real caller
                    # (research_lab/docker/research-runner/run_sweep_entrypoint.py builds
                    # `fees_arr = [[fees_by_ticker[t] for t in b_tickers]]`, never a numpy array), so the numpy
                    # indexing form `fees_arr[0, j]` raises TypeError on it. `fees_arr[0][j]` works on both a
                    # plain list of lists and a numpy array, so that is the form used here; do not "tidy" this
                    # back to the comma form, it only works on one of the two real shapes.
                    fee_rate=fees_arr[0][j],     # the fee OF this column, read positionally, aligned to entries.columns
                    factor=_factor_column(factor, ticker),  # lane D1: real shares at the price actually traded
                    slippage_rate=slippage_rates[j],  # lane KA (AC-5): this column's own real slippage rate, the SAME
                    # per-column scalar `cell_metric_columns` already reads for the 8th metric (both trace back to
                    # the SAME per-ticker setting), so the sizing walk nets the identical slippage the reported
                    # portfolio itself is about to charge.
                    session_day=session_day,     # lane P2d: day labels for bars shorter than a day (None for daily)
                )  # NaN except at this column's own entry bars
            except CELL_REFUSAL_ERRORS as exc:  # narrow by class (DECISIONS #432 territory: one item isolated, recorded)
                if refusals is None:
                    raise  # the caller kept the old abort-the-batch behaviour
                refusals.append({"column": col, "error": type(exc).__name__,
                                 "reason": refusal_reason(str(exc)), "message": str(exc)})
                refused_cols.add(col)
                continue
            size_columns[col] = col_shares
            # ibkr_costs unit: the same per-trade entry/exit fee walk the single-run path uses, over
            # this one column, computed from the SAME shares Series just built above.
            fixed_fees_columns[col] = _fixed_fee_series(
                close[ticker], open[ticker], entries[col], final_exits[col], size_columns[col], buy_order_fee,
                exit_fee_func=sell_order_fee,  # FEE1b: the exit leg is a sell
                factor=_factor_column(factor, ticker),  # lane D1
                slippage_rate=slippage_rates[j],  # AC-10 (KA round 5): this column's own real slippage
                # rate, the SAME per-column scalar _compounding_shares just used above for this column,
                # so the batch's charged fee prices the identical executed price the equity walk assumed.
            )
        if refused_cols:  # lane D1: leave every refused column out of the batch, keeping column order
            kept_idx = [j for j, col in enumerate(entries.columns) if col not in refused_cols]
            kept_cols = [entries.columns[j] for j in kept_idx]
            if not kept_cols:
                return []  # every column refused: no portfolio to build; the refusals carry the reasons
            entries = entries[kept_cols]
            final_exits = final_exits[kept_cols]
            # Lane P1 (cn22's third run): selecting columns keeps every original ticker as a level value of
            # the column MultiIndex, so a ticker refused in EVERY combo stays in the level while the price
            # frames below drop it; vbt then refuses to align them ("Level 'ticker' in second index contains
            # values not in first index", measured with two or more combos). Drop the unused level values.
            if isinstance(entries.columns, pd.MultiIndex):
                entries.columns = entries.columns.remove_unused_levels()
                final_exits.columns = final_exits.columns.remove_unused_levels()
            # The price frames keep only tickers that still have a column: vbt cannot align price frames
            # carrying a ticker that no batch column has (measured in this image: "Cannot find common
            # levels to align indexes"), which is exactly what dropping a ticker's only column leaves.
            kept_tickers = list(dict.fromkeys(c[-1] if isinstance(c, tuple) else c for c in kept_cols))
            kept_tickers = [t for t in close.columns if t in kept_tickers]  # the price frames' own order
            close, open, high, low = close[kept_tickers], open[kept_tickers], high[kept_tickers], low[kept_tickers]
            indicators = {name: frame[kept_cols] for name, frame in indicators.items()}
            fees_arr = [[fees_arr[0][j] for j in kept_idx]]
            slippage_arr = [[slippage_arr[0][j] for j in kept_idx]]
            slippage_rates = [slippage_rates[j] for j in kept_idx]
        size_wide = pd.DataFrame(size_columns)  # one column per (combo, ticker), aligned to entries' own columns
        size_type = vbt.pf_enums.SizeType.Amount  # size counts literal units, matching sizing.py's own rounding
        fixed_fees_wide = pd.DataFrame(fixed_fees_columns)  # one column per (combo, ticker), same alignment

    # Lane KA finding KA-R1 (docs/units/KA/reads/read_1.md): the TRUE (pre-extension) close frame,
    # captured before _forced_close_extension appends its one synthetic bar below, so a forcing
    # column's own true-bars-only stats reconstruction (further down this loop) has the real close
    # series to build from, never the extended one.
    true_close_wide = close

    # Lane KA fix #1 for this wide path (docs/units/KA/criteria.md AC-3/AC-5): the SAME last-bar
    # forced-exit gap the single-run path had, per column. Shared detection/extension logic with
    # cell_subportfolio's own re-run path, never a second copy: _forced_close_extension.
    close, open, high, low, entries, final_exits, forced_close_cols = _forced_close_extension(
        close, open, high, low, entries, final_exits,
    )
    if forced_close_cols:  # the one synthetic bar _forced_close_extension just appended needs a size/fee row too
        import pandas as pd  # lazy: self-contained, never relies on the sizing-governed branch above having run

        synth_index = entries.index[-1:]
        # Code-quality round 1 SF1: this is the SAME batch (`close`'s own column count can equal
        # `entries`' own column count, more than one, the exact condition the fix above exists for)
        # reaching the SAME dict-comprehension shape (`pd.DataFrame({c: [...] for c in cols})`) that
        # built an UNNAMED column Index for `close`/`entries` before the fix. Built here `columns=
        # size_wide.columns` / `columns=fixed_fees_wide.columns` (never a dict), the identical pattern
        # the fix above uses, so this concat can never silently drop a name either, whether or not it
        # is load-bearing for THIS particular kwarg (unverified before this round; now matched anyway).
        if size_wide is not None:
            size_wide_columns = size_wide.columns  # captured BEFORE the concat, to restore after
            size_wide = pd.concat(
                [size_wide, pd.DataFrame(float("nan"), index=synth_index, columns=size_wide_columns)]
            )
            size_wide.columns = size_wide_columns  # belt-and-braces: restore the name regardless
        if fixed_fees_wide is not None:
            fixed_fees_wide_columns = fixed_fees_wide.columns  # captured BEFORE the concat, to restore after
            fixed_fees_wide = pd.concat(
                [fixed_fees_wide, pd.DataFrame(0.0, index=synth_index, columns=fixed_fees_wide_columns)]
            )
            fixed_fees_wide.columns = fixed_fees_wide_columns  # belt-and-braces: restore the name regardless

    # ONE wide pass via the shared from_signals (identical kwargs to long_only_signal_result, per-column cost
    # arrays + the partition annualization). Routed through _from_signals_wide so the cell WRITE path here and
    # the Track-C full-metrics cell RE-RUN (cell_subportfolio) use the byte-identical execution model (they
    # cannot drift in a kwarg). Same-bar entry+exit -> vbt's default ConflictMode.Ignore, as the single path.
    pf = _from_signals_wide(close, open, high, low, entries, final_exits, fees_arr, slippage_arr, freq, year_freq,
                             init_cash, size=size_wide, size_type=size_type, fixed_fees=fixed_fees_wide)

    union_first = close.index[0]   # the partition union index bounds, for the ragged-member no-op check
    union_last = close.index[-1]

    cells: list = []  # one entry per column, in column order
    for j, col in enumerate(entries.columns):  # iterate the batch's columns (the (combo x ticker) tuples)
        sub = pf[col]  # the single-column slice == the single-run portfolio for this (ticker, combo), verified
        # Lane C5 (set 16c, #427): a SEPARATE reference to the UNSLICED column, captured before the
        # ragged own-history slice below may reassign `sub`. AC-2's block sums are aggregated over
        # the PARTITION's own shared bar index (the calendar the whole sweep shares), never a
        # single member's own narrower window, so this needs the full-index value series even for
        # a ragged member. No new vbt call: `pf[col]` is the same column slice `sub` already is.
        full_sub = pf[col] if s_blocks is not None else None
        ent_col, ext_col = entries[col], final_exits[col]            # lane B: the exits `pf` actually used
        ind_col = {name: frame[col] for name, frame in indicators.items()}  # this column's warmup series

        # AD-M5-16 (the ragged-metric WINDOW fix): a member NaN-padded for its pre-listing bars in the partition
        # union index would otherwise have EVERY per-cell metric computed over the union span — folding its
        # pre-listing flat-cash period into the ratio (the probe measured a ragged member's Sharpe at 0.153
        # vs a true 0.429, a 64% distortion). So slice the column's portfolio AND its signal/warmup series to
        # the member's OWN observed-bar window before extracting any metric, so each metric reflects the
        # member's real history, not the union span. For a full-history member the window IS the union, so the
        # slice is skipped (a no-op) — only ragged members are sliced.
        # The member's own observed-bar window within the partition union, via the shared _own_history_bounds
        # (the same rule cell_subportfolio's re-run uses). close/open/high/low carry UNIQUE-TICKER columns and
        # `col` is the (param..., ticker) signal column with the ticker LAST, so the helper keys on col[-1].
        first_valid, last_valid, ragged = _own_history_bounds(close, col, union_first, union_last)
        if ragged:  # slice everything to the member's own history
            sub = sub.loc[first_valid:last_valid]                   # the portfolio over the member's own bars
            ent_col = ent_col.loc[first_valid:last_valid]           # aligned signal series (no pre-listing bars)
            ext_col = ext_col.loc[first_valid:last_valid]
            ind_col = {name: s.loc[first_valid:last_valid] for name, s in ind_col.items()}

        # AD-M5-16 (the ragged-metric FACTOR fix): the wide from_signals annualizes at the PARTITION union
        # year_freq, and a sliced sub-portfolio RETAINS that union factor — so a ragged member's Sharpe would
        # be annualized at the union factor, not its own. The factor is (len-1)/span (annualization.py), which
        # is span-DEPENDENT (empirically ~252/yr for daily, NOT a span-independent constant), so the union and
        # own factors differ by a fraction of a percent for same-calendar daily and more for a short member —
        # and using the union factor violates the spec-brief-§11 cell-for-cell identity vs a true standalone
        # run. So a ragged member's Sharpe is recomputed over its OWN sliced-index factor; a full-history
        # member already has the own factor (its window IS the union), so it keeps the property unchanged.
        # Sharpe is the only factor-dependent metric of the eight; a < 2-bar own window has no annualizable
        # Sharpe (AnnualizationError) -> an honest None, the same nullable contract as zero variance.
        col_freq, col_year_freq = freq, year_freq  # this column's own annualization; overridden below if ragged
        if ragged:
            try:
                own = resolve_annualization(sub.wrapper.index)  # the member's OWN index factor (freq + year_freq)
            except AnnualizationError as exc:  # bind the exception (F1): a too-short window and a genuinely
                # undecidable grid (no_dominant_gap) both land here, but only one of them is a data-quality
                # signal worth surfacing rather than a routine short-window outcome. Branch on the reason CODE
                # by name so the raise is OBSERVABLE (the brief's "what can break" table: "fail loud rather
                # than choose: raise, and count the raises") instead of vanishing into an identical None.
                if "no_dominant_gap" in str(exc):
                    logger.warning(  # structured fields, not string interpolation, per the global logging rule
                        "sweep_portfolio_no_dominant_gap",
                        extra={"reason": "no_dominant_gap", "member_bars": len(sub.wrapper.index)},
                    )
                sharpe = None  # honest None either way: a too-short window and an undecidable grid both mean
                # "no defensible own-window Sharpe for this member", the same nullable contract as zero variance
            else:  # own-factor Sharpe over the sliced returns == the member's true standalone single run
                col_freq, col_year_freq = own["freq"], own["year_freq"]  # this ragged member's own factor
                sharpe = _finite_or_none(
                    sub.returns.vbt.returns(freq=own["freq"], year_freq=own["year_freq"]).sharpe_ratio()
                )
        else:  # full-history member: the partition factor already IS its own factor (window == union)
            sharpe = _finite_or_none(sub.sharpe_ratio)

        # CS Amendment 1 (A1): the ten metric columns now come from the ONE shared helper
        # (cell_metric_columns, defined above), so this write path and the replay path
        # (compute_metrics_entrypoint.py) run the IDENTICAL expressions and can never drift apart.
        # This is the direct fix for the measured skew/kurtosis gap (jc13_measurement.md). The
        # helper's own docstring carries the full expression-by-expression provenance; only the
        # column-specific bookkeeping (entries_masked_at_end, not one of the ten L-2 columns) stays here.
        stats = cell_metric_columns(sub, sharpe, slippage_rates[j])
        # Lane KA finding KA-R1 (docs/units/KA/reads/read_1.md), extended by KA round 4 finding B1
        # (docs/units/KA/reads/battery_1.md): `sub` above still carries the one synthetic bar
        # `_forced_close_extension` appended for a forcing column (it is never ragged-sliced away, by
        # that helper's own design: its own non-NaN synthetic value keeps `_own_history_bounds`'s
        # `last_valid_index()` extended to match), so EVERY bar-index-dependent key `cell_metric_columns`
        # just computed above (`sharpe_ratio`, `max_drawdown`, and B1's own finding, `skew`/`kurtosis`,
        # all read off `sub.returns`/`sub.max_drawdown`) includes the phantom bar. Overwrite all four
        # from the SAME true-bars-only `from_orders` reconstruction the single-run path's own fix uses
        # (`true_bars_forced_close_stats`, `strategies.helpers.portfolio`, extended by B1's own fold to
        # carry skew/kurtosis too rather than a second reconstruction), built from this column's own
        # already-recorded fills (`sub.trades.records_readable`, renamed to the same canonical columns
        # `build_run_result` uses), over this column's own TRUE close series (`true_close_wide[ticker]`,
        # captured before `_forced_close_extension` ran) and this column's own freq/year_freq
        # (`col_freq`/`col_year_freq`, the same ragged-aware factor the sharpe branch above already
        # resolved). `sweep_portfolio_cells`'s own per-cell stats never carry `turnover`/
        # `max_gross_exposure` at all (k10's own docstring), so those two keys of the four
        # `true_bars_forced_close_stats` returns are read by the single-run caller only, never here.
        if col in forced_close_cols:
            from strategies.base.vbt_columns import VBT_TO_CANONICAL_TRADES  # the one shared rename map
            from strategies.helpers.portfolio import true_bars_forced_close_stats  # the shared true-bars fix

            ticker = col[-1] if isinstance(col, tuple) else col  # this column's own price-frame ticker key
            col_trades = sub.trades.records_readable.rename(columns=VBT_TO_CANONICAL_TRADES)  # canonical columns
            # This column's own exit fill still carries the SYNTHETIC bar's own timestamp (this path
            # never relabels it the way long_only_signal_result's own trim does, since this path
            # returns only scalar stats, never a raw trades table with an exposed timestamp column):
            # relabel it onto the true last bar before handing it to the true-bars reconstruction,
            # which knows only the true index and would otherwise raise KeyError looking it up.
            true_last_ts = true_close_wide.index[-1]  # the true last bar every forced fill stands in for
            synthetic_ts = close.index[-1]             # the one synthetic bar _forced_close_extension appended
            col_trades = col_trades.copy()
            col_trades["exit_index"] = col_trades["exit_index"].replace({synthetic_ts: true_last_ts})
            true_stats = true_bars_forced_close_stats(
                true_close_wide[ticker], col_trades, fees_arr[0][j], init_cash, col_freq, col_year_freq,
            )
            stats["sharpe_ratio"] = true_stats["sharpe_ratio"]
            stats["max_drawdown"] = true_stats["max_drawdown"]
            stats["skew"] = true_stats["skew"]        # AC-8/B1: cell_metric_columns's own sub.returns.skew()
            stats["kurtosis"] = true_stats["kurtosis"]  # AC-8/B1: cell_metric_columns's own sub.returns.kurtosis()
        # Lane LB (5.8e): this column's own fallback count (0 or 1; the vectorized sweep never
        # reaches, finding 2/criterion 3, so there is no fill_reach_ts to carry here). Rides in the
        # JSON payload and the in-process counter above; NOT one of the ten L-2 metric columns (no
        # schema change for this lane), so a reader durably sees it only via SIZING_MONITOR_COUNTERS
        # today, exactly as this lane's own "what can break" section states.
        stats["entries_masked_at_end"] = entries_masked_at_end_by_col.get(col, 0)
        # The per-cell funnel: the same compute_signal_funnel the single path uses, read off this column's
        # (own-history-sliced) portfolio slice + raw entry/exit Series + warmup indicators (so bars_after_warmup
        # is exact — verified). zero_reason is derived by the engine via classify_zero_reason.
        funnel = compute_signal_funnel(sub, ent_col, ext_col, ind_col)  # the 9 numeric counts
        # Lane KA (AC-3/AC-5): this column's own synthetic forced-close bar (if any) is a real trading
        # bar to vbt's own fill accounting, but it is not one of the store's own bars, so it must not be
        # counted in the funnel the same way long_only_signal_result's own forced_close_at_end branch
        # already corrects bars_total/bars_after_warmup on the single-run path (portfolio.py).
        if col in forced_close_cols:
            funnel["bars_total"] -= 1  # drop the one synthetic bar from the count the store actually holds
            if not indicators:  # mirror portfolio.py: no independent warmup series to re-derive this from
                funnel["bars_after_warmup"] = funnel["bars_total"]
        cell_entry = {"column": col, "stats": stats, "funnel": funnel}  # the engine maps column -> ids next
        if s_blocks is not None:
            # Lane C5 (set 16c, #427, plan amendment 4/round 2, AC-1/AC-2): the ADDITIVE per-block
            # sums for check 5's own cscv artifact, read off the SAME per-column portfolio value this
            # loop already computed (full_sub, unsliced), never a second wide from_signals. Lane KA's
            # forced last-bar close (AC-3/AC-5, this merge) appends ONE synthetic bar to the WHOLE
            # batch's shared index whenever any column in the batch forces a close; that bar is not
            # one of the store's own bars (the SAME reason the funnel fix just above drops it from
            # bars_total), so it must never enter a block sum either, for ANY column, forcing or not.
            # true_close_wide (captured above, before _forced_close_extension ran) carries the TRUE,
            # pre-extension bar count and price series; truncating full_sub's own value series to that
            # length BEFORE pct_change means the synthetic bar's own repeated-close value can never
            # leak into a return, not even the true last bar's own return just before it.
            ticker = col[-1] if isinstance(col, tuple) else col  # the price-frame column key, _own_history_bounds's own convention
            true_bar_count = len(true_close_wide.index)  # the TRUE (pre-extension) bar count every block sum must respect
            has_price = true_close_wide[ticker].notna()  # AC-2's own "no price for this bar" test, over the TRUE index only
            full_returns = full_sub.value.iloc[:true_bar_count].pct_change()  # truncate BEFORE pct_change
            bar_returns: list = []
            prev_has_price = False  # tracks whether the PREVIOUS bar in this loop had a real price
            for i in range(true_bar_count):
                if not bool(has_price.iloc[i]):
                    bar_returns.append(None)  # AC-2: no price for this bar -> None (zero-filled by _cscv_block_sums)
                    prev_has_price = False
                    continue
                if not prev_has_price:
                    # This member's own first priced bar in the partition (a fresh listing, or the
                    # bar right after a data gap): no valid prior value to compare against, so 0.0
                    # by the SAME convention run_sweep_entrypoint.py's own _run_result_cell already
                    # uses for the zero/inline paths (`res.equity.pct_change().fillna(0.0)`), never
                    # a crash and never a fabricated non-zero number.
                    bar_returns.append(0.0)
                else:
                    bar_returns.append(float(full_returns.iloc[i]))
                prev_has_price = True
            blocks, bars_dropped = _cscv_block_sums(bar_returns, s_blocks)
            cell_entry["cscv_block_sums"] = blocks
            cell_entry["cscv_bars_dropped"] = bars_dropped
        cells.append(cell_entry)

    return cells  # per-column cells for this batch; the engine adds the ids and emits the chunk
