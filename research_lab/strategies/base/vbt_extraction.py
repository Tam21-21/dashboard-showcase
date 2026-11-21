"""Shared helper: turn a finished vbt portfolio into a complete, canonical RunResult.

This is the single place vbt's portfolio object is read and translated into the
engine-neutral ``RunResult``. Every strategy calls it at the end of ``run()``, so
``run()`` returns a complete result and the vbt object never crosses the contract
boundary (decision: ``run()`` owns extraction; the container entrypoint only
serialises the slots — see DECISIONS, this session).

It is deliberately NOT re-exported from ``strategies.base`` so that importing the
contract surface stays free of vbt; only modules that actually run a backtest
import this one.

vbt API points verified against the pinned build (vbt 2026.4.7) on 2026-05-21:
``pf.value`` is a property; ``records_readable`` gives the 16 trade / 9 drawdown
labels in vbt_columns; ``pf.trades.mae`` / ``.mfe`` / ``.mae_returns`` /
``.mfe_returns`` are MappedArrays aligned positionally to the trade records;
``pf.sharpe_ratio`` / ``max_drawdown`` / ``total_return`` and
``pf.trades.win_rate`` / ``profit_factor`` are properties; ``pf.trades.count()``
is a method; ``pf.position_coverage`` is exposure; ``pf.orders.fees.sum()`` is
total fees.
"""
from __future__ import annotations  # lazy annotations

import math  # finite-check: coerce a non-finite ratio (inf/nan) to None for the nullable stats columns

from typing import TYPE_CHECKING  # keep the pandas/RunResult type imports light at module load

from strategies.base.run_result import RunResult  # the locked output type (pure, safe to import)
from strategies.base.vbt_columns import (  # canonical schemas + rename maps
    CANONICAL_DRAWDOWN_COLUMNS,
    CANONICAL_TRADE_COLUMNS,
    VBT_TO_CANONICAL_DRAWDOWNS,
    VBT_TO_CANONICAL_TRADES,
)

if TYPE_CHECKING:  # readers / type checker only
    import pandas as pd  # for the indicators/regimes annotations below


def _finite_or_none(value) -> float | None:
    """Coerce a non-finite ratio to None so it matches the nullable research_run_stats columns.

    vbt returns ``inf`` for ``profit_factor`` on an all-winners run (zero gross loss) and ``nan``
    for ``sharpe_ratio`` on a zero-variance run. Those are the three NULLABLE stats columns
    (``sharpe_ratio`` / ``win_rate`` / ``profit_factor``, researchdb.py:265-271), so a non-finite
    value becomes None here, exactly as the empty-run helper emits None for a no-trade run
    (event_pipeline.empty_run_result). This keeps json.dumps from writing an invalid ``Infinity`` /
    ``NaN`` token into stats.json that a share page's JSON.parse would reject.
    """
    v = float(value)  # vbt metrics come back as numpy scalars; normalize to a Python float first
    return v if math.isfinite(v) else None  # finite -> the number; inf/nan -> None (the nullable sentinel)


def extract_turnover(pf) -> float | None:
    """Annualized one-way portfolio turnover from the order fills (M5 Track C F6, the pinned turnover ruling).

    The pinned VBT build (2026.4.7) has NO native turnover method (a Portfolio has neither ``turnover``
    nor ``get_turnover``; vbt's own ``stats()`` carries none -- MCP-verified, twice), so this is the ONE
    metric on the full-metrics page computed FROM vbt's own records rather than called OFF a vbt accessor.
    It is NOT a reimplementation of a vbt formula (there is none to drift from, so the call-vbt-never-
    reimplement rule's failure mode -- a parallel formula silently diverging from vbt -- cannot occur); it
    is the standard portfolio-turnover definition computed from ``pf.orders``, pinned with provenance
    (docs/research_lab/parameter_provenance.md, the ``turnover`` row) and a hand-checked probe leg (the
    Gate-1 probe), exactly as N_min is pinned. The narrow, documented exception is logged in the
    correctness-spec amendment (turnover ruling, option a).

    Definition (one-way, annualized):
        turnover = 0.5 * sum(|fill size| * fill price) / mean(portfolio value) / span_years
    span_years is the own-history calendar span in 365.25-day years -- the SAME "year"
    ``annualization.resolve_annualization`` uses, so turnover's year matches every other annualized
    metric. The 0.5 makes it ONE-WAY (a buy plus the matching sell counts as one unit of turnover): the
    SEC / Grinold-Kahn convention, which reduces to Carver's "round trips per year of an average-size
    position" (carver-systematic-trading-tibot.md p.185/p.288) for a fully-invested book.

    Returns None (honest absence) when turnover is undefinable: a window under 2 bars (no span to
    annualize), a degenerate span, or a non-positive mean book value (a zero or underwater book, which
    would make the rate undefined or negative -- turnover is a non-negative rate by definition).
    Returns 0.0 when there are simply no fills
    (the strategy never traded -- a real zero, not an absence). The single pf-read site (build_run_result)
    AND the vectorized cell re-run both call this one helper, so turnover is computed identically on every
    surface (reuse over reinvention).
    """
    import pandas as pd  # lazy: vbt_extraction is imported only by modules that actually run a backtest

    value = pf.value  # the equity curve (own-history window for an already-sliced cell pf)
    if len(value) < 2:
        return None  # no span -> cannot annualize (mirrors AnnualizationError; spec §5 honest absence)
    span_years = float((value.index[-1] - value.index[0]) / pd.Timedelta(days=365.25))  # identical to resolve_annualization
    mean_value = float(value.mean())  # the average book value over the window (the turnover denominator)
    if not math.isfinite(span_years) or span_years <= 0 or mean_value <= 0.0:
        return None  # degenerate span or a zero/underwater book -> honest absence (turnover is a non-negative rate;
        #              a non-positive denominator would yield an undefined or negative value, never surfaced)
    orders = pf.orders.records_readable  # the fill records; 'Size' (magnitude) + 'Price' columns (MCP-verified)
    if len(orders) == 0:
        return 0.0  # never traded -> turnover is genuinely zero (a real value, not honest absence)
    # gross traded notional = sum over fills of |size| * price (Size is a positive magnitude; Side holds direction)
    traded_notional = float((orders["Size"].astype(float).abs() * orders["Price"].astype(float)).sum())
    return _finite_or_none(0.5 * traded_notional / mean_value / span_years)  # one-way, annualized


def build_run_result(
    pf,  # a vbt Portfolio produced by the strategy (typed loosely to avoid a vbt import here)
    indicators: dict[str, "pd.Series"],  # strategy-supplied overlay series (e.g. fast/slow SMA)
    regimes: dict[str, list[tuple[int, int, str]]] | None = None,  # optional regime intervals
    *,  # total_slippage_cost is keyword-only: the caller MUST name it, so it can never be passed by accident
    total_slippage_cost: float,  # the 8th derived stat; computed by the caller (per-fill measure, DECISIONS #66/#67)
) -> RunResult:
    """Extract all six RunResult slots from a finished vbt portfolio."""
    # --- equity: pf.value is a PROPERTY (pf.value() raises TypeError on 2026.4.7) ---
    equity = pf.value  # a pd.Series of account value per bar

    # --- trades: rename the 16 readable columns, join the 4 MAE/MFE columns ---
    trades = pf.trades.records_readable.rename(columns=VBT_TO_CANONICAL_TRADES)  # 16 canonical cols
    trades["mae"] = pf.trades.mae.values  # max adverse excursion ($), positional join
    trades["mfe"] = pf.trades.mfe.values  # max favourable excursion ($)
    trades["mae_return_pct"] = pf.trades.mae_returns.values  # MAE as a return fraction
    trades["mfe_return_pct"] = pf.trades.mfe_returns.values  # MFE as a return fraction
    trades = trades[CANONICAL_TRADE_COLUMNS]  # enforce the exact §3.8.1 column order

    # --- drawdowns: the readable frame is already canonical content, just rename ---
    drawdowns = pf.drawdowns.records_readable.rename(columns=VBT_TO_CANONICAL_DRAWDOWNS)
    drawdowns = drawdowns[CANONICAL_DRAWDOWN_COLUMNS]  # enforce the exact §3.8.2 column order

    # --- stats: the single-run metrics, keyed to match research_run_stats columns ---
    # total_slippage_cost is the 8th derived stat: the direct per-fill execution-shortfall sum
    # (DECISIONS #66/#67), computed by the CALLER from the one run's order records (the price path
    # recovers the reference from the rate, the event path from its fill model) and folded in here,
    # so the metric is assembled in ONE place alongside the other seven (SN-C3 / EL61) instead of
    # being bolted on by a second shadow pass in the entrypoint.
    stats: dict[str, float | None] = {
        "total_return": float(pf.total_return),  # overall return fraction (NOT NULL)
        "sharpe_ratio": _finite_or_none(pf.sharpe_ratio),  # annualised by the portfolio's freq (a price run infers it from the index; an event run sets it explicitly, BN-J1-3); None on zero variance
        "max_drawdown": abs(float(pf.max_drawdown)),  # stored as a positive fraction (schema §3.5, NOT NULL)
        "num_trades": float(pf.trades.count()),  # number of closed trades (NOT NULL)
        "win_rate": _finite_or_none(pf.trades.win_rate),  # fraction of winning trades; None if undefined
        "profit_factor": _finite_or_none(pf.trades.profit_factor),  # gross profit / gross loss; None when gross loss is 0 (vbt returns inf -> None)
        "total_fees_paid": float(pf.orders.fees.sum()),  # sum of all order fees (NOT NULL)
        "total_slippage_cost": float(total_slippage_cost),  # the per-fill measure (NOT NULL); a non-null float, 0.0 for no fills
        # M5 Track C (C2 §8.2 fold 2, F6): the two exposure/turnover scalars, extracted HERE at the single
        # pf-read site so EVERY RunResult.stats carries them — closing F6 for the single-run persist AND the
        # zero/inline cell re-run uniformly (the vectorized cell reads F6 off its own pf slice). NULLABLE, so
        # they ride beside the 8 fixed scalars without widening _RUN_STATS_COLUMNS; the lean cell write
        # (_run_result_cell) allow-lists its keys and does NOT pick them (the cell schema has no F6 columns).
        # max_gross_exposure is native VBT (pf.get_gross_exposure().max(); MCP-verified). TURNOVER has NO
        # native method in the pinned build, so it is computed from pf.orders by the one shared helper
        # extract_turnover (the pinned turnover ruling, option a -- a provenanced order-based formula, the
        # narrow documented exception to call-vbt-never-reimplement; correctness-spec amendment + the
        # parameter_provenance.md turnover row + the Gate-1 hand-checked probe leg). Never a fabricated value.
        "max_gross_exposure": _finite_or_none(pf.get_gross_exposure().max()),  # F6: max gross exposure (native)
        "turnover": extract_turnover(pf),  # F6: annualized one-way turnover from the order fills (the ruling)
    }

    # --- assemble the complete, canonical RunResult ---
    return RunResult(
        equity=equity,  # slot 1
        drawdowns=drawdowns,  # slot 2
        trades=trades,  # slot 3
        stats=stats,  # slot 4
        indicators=indicators,  # slot 5 (strategy-supplied)
        regimes=regimes or {},  # slot 6 (default empty: no regime detection in M3)
    )
