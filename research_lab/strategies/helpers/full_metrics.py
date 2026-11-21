"""The single full-metrics formula authority (M5 Track C, correctness spec §4).

ONE function — ``compute_full_metrics`` — produces the complete full-metrics page payload from a finished
backtest's RAW outputs (the equity curve, the canonical trades frame, the two F6 scalars, an optional aligned
benchmark return series). It is the single place every full-metrics consumer routes through, so the formulas
live in exactly one place (reuse over reinvention; the spec's "call VBT's own methods, never reimplement"
build rule). Two callers feed it the SAME raw shapes:

  - the SINGLE-RUN derive (compute_run_metrics): the stored equity.parquet + trades.parquet + the persisted F6
    scalars (correctness spec §2.1 / C2 §2.1 derive-on-read);
  - the CELL re-run (the three-way re-run): the re-run pf's equity + trades + F6 (C2 §2.2, the vectorized /
    zero-param / inline branches all yield these same shapes).

The two CORRECTNESS GATES validate this function: Gate 1 (the 8 derived == the 8 stored) and Gate 1b (every
reconstructible metric == the live pf's native VBT value, to epsilon). The build rule (call VBT, never
reimplement) cannot be proven by reading code, so Gate 1b is the proof — which is why the metric expressions
below were each verified against the pinned build (b1727ac6) via the VBT MCP before they were written:

  - return metrics: vbt.ReturnsAccessor.from_value(equity, init_value, freq, year_freq) + its native methods
    (sharpe/sortino/calmar/omega/annualized/annualized_volatility/downside_risk/value_at_risk/
    cond_value_at_risk/max_drawdown/total); skew/kurtosis off acc.obj (the returns series);
  - drawdown metrics: equity.vbt.drawdowns (native Drawdowns reconstructed from the equity) — get_avg_drawdown
    + get_max_duration, BOTH proven byte-equal to pf.drawdowns (the bar-count duration convention, not the
    calendar-span one a frame subtraction would give);
  - trade metrics: the canonical trades frame (expectancy = mean(pnl); sqn = sqrt(n)*mean(pnl)/std(pnl,ddof=1);
    best/worst = return_pct max/min; avg win/loss = mean return_pct over pnl>0 / pnl<0) — each proven equal to
    pf.trades.expectancy / .sqn / .returns / .winning.returns.mean() / .losing.returns.mean();
  - benchmark metrics: acc.alpha/beta/information_ratio/up_capture_ratio/down_capture_ratio(bm_returns=...),
    over the common-date overlap; IR annualized by sqrt(ann_factor) (the s107 Cowork override, spec §8.2).

F6: max_gross_exposure is native VBT (pf.get_gross_exposure().max()). TURNOVER has NO native method in the
pinned build (vbt's own stats() carries none, verified via the MCP), so it is computed FROM pf.orders by the
shared helper strategies.base.vbt_extraction.extract_turnover and passed IN to this function (the pinned
turnover ruling, option a: a provenanced one-way order-based formula -- the narrow, documented exception to
call-vbt-never-reimplement, since there is no vbt turnover to drift from). This function never computes it;
it only passes the caller-supplied value through _finite_or_none, so a caller that has no orders (a legacy
single run predating the F6 persist) still renders honest absence (None) rather than a fabricated value.

Every undefined metric returns None (the spec §5 honest-absence rule), never a fabricated 0.
"""
from __future__ import annotations

import logging  # stdlib, no import cost: makes a swallowed no_dominant_gap raise observable (F1)
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # type-only imports; the runtime imports are lazy (keep strategies.helpers vbt-free at import)
    import pandas as pd

logger = logging.getLogger(__name__)  # module logger; cheap at import, no vbt/pandas cost added


# The minimum overlapping bars below which the benchmark regression is too noisy to report (C-bench §5). A
# CANDIDATE, NOT a sourced default — it does not ship until its four-part parameter_provenance.md row exists;
# the caller passes it explicitly (None = no floor) so this module never bakes an unsourced threshold.
_BENCH_N_MIN_FLOOR = 2  # the hard floor below which no regression is mathematically possible (a 2-point line)


# The formula-code identity component of research_full_metrics_cache.formula_version (C-metrics §2.2 / §11
# item 1). Bump this BY HAND whenever the per-metric formula implementation in compute_full_metrics changes
# (a new metric, a changed expression, a corrected accessor): a bump makes every payload cached under the old
# version a cache MISS, so the page recomputes rather than serving a stale number (the one real staleness
# vector C2 §3 names; the inputs are otherwise immutable). It is NOT added to strategies.helpers.__all__, so
# it never surfaces in the catalog introspection (catalog.py reads __all__ only).
FULLMETRICS_FORMULA_VERSION = "1"  # initial: the s106-114 formula authority


def resolve_formula_version(strategy_version, engine_provenance: str) -> str:
    """Build the 3-part research_full_metrics_cache.formula_version key (C-metrics §2.2).

    Joins three identities that together determine the numbers, in a FIXED order, with '|' (a delimiter that
    cannot appear in a git-sha / version string), so two distinct triples can never collide into the same key
    and serve a stale payload:
      (a) FULLMETRICS_FORMULA_VERSION -- the formula-code identity (bumped by hand on any formula change);
      (b) strategy_version            -- the strategy code that produced the backtest (the run's version);
      (c) engine_provenance           -- the run's build_provenance ("git:<sha>+vbt:<commit>"), the engine +
          pinned-VBT identity that froze the run's inputs. The cell re-run reproduces the stored cell
          deterministically on those frozen inputs (Gate 2), so THIS identity -- not the live re-run image --
          is the correct cache-invalidation key: a post-pin engine change is gate-constrained to be
          result-preserving, and a result-changing formula edit is captured by bumping (a).

    A pure string function (no vbt), so the vbt-free backend calls it to build the cache-lookup key and the
    dispatch payload; the worker only echoes the value back at write time. Asserts '|' is absent from (b) and
    (c) before joining; a '|' present would be a contract violation and is raised loud (never silently
    swallowed into a colliding key).
    """
    sv = str(strategy_version)  # the run's strategy_version (an int in the DB) -> str for the key
    if "|" in sv or "|" in engine_provenance:
        raise ValueError(
            f"formula_version component contains the '|' delimiter: "
            f"strategy_version={sv!r}, engine_provenance={engine_provenance!r}")
    return f"{FULLMETRICS_FORMULA_VERSION}|{sv}|{engine_provenance}"


def _finite_or_none(x):
    """Coerce a non-finite value (NaN/inf) or a missing value to None — the spec §5 honest-absence coercion.

    Mirrors strategies.base.vbt_extraction._finite_or_none, duplicated here so this helper stays importable
    without dragging the vbt-side extraction module (the helpers package keeps its vbt imports lazy)."""
    import math  # lazy + cheap
    if x is None:
        return None  # a legitimately-absent value (e.g. an unset benchmark/F6 scalar) -> honest None
    # No try/except around float(): every caller passes None (handled above) or a numeric scalar, so a
    # non-numeric here is a real type bug and MUST fail loud (the no-papering rule), not be swallowed to None.
    xf = float(x)
    return xf if math.isfinite(xf) else None


def compute_full_metrics(
    *,
    equity: "pd.Series",
    trades: "Optional[pd.DataFrame]" = None,
    max_gross_exposure: Optional[float] = None,
    turnover: Optional[float] = None,
    total_fees_paid: Optional[float] = None,
    total_slippage_cost: Optional[float] = None,
    bm_returns: "Optional[pd.Series]" = None,
    bench_n_min: Optional[int] = None,
) -> dict:
    """Compute the complete full-metrics payload from a backtest's raw outputs (correctness spec §4).

    Args:
        equity: the equity curve (= pf.value), a pd.Series with a DatetimeIndex. The source of the return
            series, the drawdown episodes, and the period/value detail. Must carry the leading r[0]==0 bar
            (the spec §1 contract) — i.e. it is the full pf.value, untrimmed.
        trades: the canonical 20-column trades frame (pf.trades.records_readable renamed), or None (no trades
            → every trade metric is None).
        max_gross_exposure: the native VBT max gross exposure (pf.get_gross_exposure().max()), or None.
        turnover: the F6 turnover, computed by extract_turnover in build_run_result (the pinned ruling,
            spec amendment 2026-06-24); None only when undefinable (a <2-bar window or a non-positive book
            value), so a legacy single run predating the F6 persist still renders honest absence.
        total_fees_paid / total_slippage_cost: the two stored cost scalars (pass-through to the page).
        bm_returns: an optional benchmark daily-return Series for the benchmark group (alpha/beta/R²/IR/
            capture), aligned by date-intersection with the strategy returns inside this function.
        bench_n_min: the minimum overlapping bars below which the benchmark regression is not reported
            (C-bench §5). None = no floor beyond the hard 2-point minimum. A CANDIDATE value is the caller's
            to pass once its provenance row exists; this module never bakes one.

    Returns:
        A flat dict of metric name -> value (float | None | str), grouped per the mockup's FULL METRICS
        sections. Every undefined metric is None (honest absence), never a fabricated 0.
    """
    import numpy as np  # lazy
    import pandas as pd  # lazy
    import vectorbtpro as vbt  # lazy: only when metrics are actually computed (helpers package stays vbt-free at import)

    from strategies.helpers.annualization import AnnualizationError, resolve_annualization

    out: dict = {}

    # --- annualization (the AD-M5-16 own-window factor; every annualized metric uses it, spec §2) ----------
    try:
        ann = resolve_annualization(equity.index)  # {factor, freq, year_freq} from the equity's own index
    except AnnualizationError as exc:  # bind the exception (F1): a too-short equity window and a genuinely
        # undecidable grid (no_dominant_gap) both land here, but only one of them is a data-quality signal
        # worth surfacing rather than a routine short-window outcome. Branch on the reason CODE by name so
        # the raise is OBSERVABLE (the brief's "what can break" table: "fail loud rather than choose: raise,
        # and count the raises") instead of vanishing into an identical None.
        if "no_dominant_gap" in str(exc):
            logger.warning(  # structured fields, not string interpolation, per the global logging rule
                "full_metrics_no_dominant_gap",
                extra={"reason": "no_dominant_gap", "equity_bars": len(equity)},
            )
        ann = None  # every annualized metric is None either way: too-short window or an undecidable grid (spec §5)

    acc = None
    returns = None
    if ann is not None and len(equity) >= 2:
        # reconstruct the EXACT return series + annualization VBT used (the spec Gate-1 recipe). init_value is
        # equity.iloc[0] (== init_cash by the bar-0 argument; Gate 1 proves the equality numerically).
        acc = vbt.ReturnsAccessor.from_value(
            equity, init_value=float(equity.iloc[0]), freq=ann["freq"], year_freq=ann["year_freq"])
        returns = acc.obj  # the underlying per-bar simple-return series (carries the leading r[0]==0 bar)

    # --- DISTRIBUTION (spec §4.1) -------------------------------------------------------------------------
    if returns is not None:
        out["skew"] = _finite_or_none(returns.skew())            # pandas bias-corrected G1 (spec §4.1)
        out["kurtosis"] = _finite_or_none(returns.kurtosis())    # excess Fisher kurtosis (normal -> 0)
        out["var_95"] = _finite_or_none(acc.value_at_risk())     # empirical 5th percentile, signed-negative (spec §8.3)
        out["cvar_95"] = _finite_or_none(acc.cond_value_at_risk())  # mean of the worst-5% (signed-negative)
        out["best_day"] = _finite_or_none(returns.max())
        out["worst_day"] = _finite_or_none(returns.min())
        # the daily-return histogram for the DISTRIBUTION centerpiece (C-metrics §5): bucketed counts the
        # worker computes from the SAME returns series the moments use (the page renders the SVG from these,
        # never re-bucketing in the frontend). returns_mean drives the dashed mean line.
        out["returns_histogram"] = _returns_histogram(returns, np)
        out["returns_mean"] = _finite_or_none(returns.mean())
    else:
        for k in ("skew", "kurtosis", "var_95", "cvar_95", "best_day", "worst_day",
                  "returns_histogram", "returns_mean"):
            out[k] = None

    # --- RETURNS (spec §4.2) ------------------------------------------------------------------------------
    out["total_return"] = _finite_or_none(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) >= 1 else None
    out["cagr"] = _finite_or_none(acc.annualized()) if acc is not None else None  # geometric CAGR, own factor

    # --- RISK + RATIOS (spec §4.3; benchmark-gated alpha/beta/R²/IR/capture in the BENCHMARK block) --------
    if acc is not None:
        out["sharpe"] = _finite_or_none(acc.sharpe_ratio())
        out["sortino"] = _finite_or_none(acc.sortino_ratio())
        out["calmar"] = _finite_or_none(acc.calmar_ratio())
        out["omega"] = _finite_or_none(acc.omega_ratio())
        out["annualized_volatility"] = _finite_or_none(acc.annualized_volatility())
        out["downside_deviation"] = _finite_or_none(acc.downside_risk())
    else:
        for k in ("sharpe", "sortino", "calmar", "omega", "annualized_volatility", "downside_deviation"):
            out[k] = None

    # --- DRAWDOWN (spec §4.4) — native Drawdowns from the equity (proven byte-equal to pf.drawdowns) -------
    max_dd = None
    if acc is not None:
        max_dd = _finite_or_none(abs(float(acc.max_drawdown())))  # stored as a positive fraction (spec §4.4)
    out["max_drawdown"] = max_dd
    if len(equity) >= 2:
        dd = equity.vbt.drawdowns                                # native Drawdowns reconstructed from the equity
        out["avg_drawdown"] = _finite_or_none(abs(float(dd.get_avg_drawdown())))  # magnitude (positive)
        max_dur = dd.get_max_duration()                          # a pd.Timedelta (bar-count convention)
        out["max_drawdown_duration_days"] = (
            _finite_or_none(max_dur / pd.Timedelta(days=1)) if pd.notna(max_dur) else None)
    else:
        out["avg_drawdown"] = None
        out["max_drawdown_duration_days"] = None
    # recovery factor = total_return / max_drawdown (the two scalars; None if max_dd is 0/None)
    out["recovery_factor"] = (
        _finite_or_none(out["total_return"] / max_dd)
        if (out["total_return"] is not None and max_dd not in (None, 0.0)) else None)

    # --- TRADES (spec §4.5) — from the canonical trades frame (each expr proven == native pf.trades.*) -----
    if trades is not None and len(trades) > 0 and "pnl" in trades.columns:
        pnl = trades["pnl"].astype(float)
        ret = trades["return_pct"].astype(float) if "return_pct" in trades.columns else pd.Series(dtype=float)
        n = int(len(trades))
        out["total_trades"] = n
        out["expectancy"] = _finite_or_none(pnl.mean())                       # mean trade PnL ($)
        out["sqn"] = _finite_or_none(np.sqrt(n) * pnl.mean() / pnl.std(ddof=1))  # van Tharp; nan if std=0 -> None
        out["best_trade"] = _finite_or_none(ret.max()) if len(ret) else None
        out["worst_trade"] = _finite_or_none(ret.min()) if len(ret) else None
        wins = ret[pnl > 0]
        losses = ret[pnl < 0]
        out["avg_win"] = _finite_or_none(wins.mean()) if len(wins) else None
        out["avg_loss"] = _finite_or_none(losses.mean()) if len(losses) else None
        win_n = int((pnl > 0).sum())
        out["win_rate"] = _finite_or_none(win_n / n) if n else None
        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl < 0].sum())
        out["profit_factor"] = _finite_or_none(gross_profit / gross_loss) if gross_loss > 0 else None
    else:
        for k in ("total_trades", "expectancy", "sqn", "best_trade", "worst_trade",
                  "avg_win", "avg_loss", "win_rate", "profit_factor"):
            out[k] = 0 if k == "total_trades" else None

    # --- EXPOSURE + COST (spec §4.6; F6) ------------------------------------------------------------------
    out["max_gross_exposure"] = _finite_or_none(max_gross_exposure)
    out["turnover"] = _finite_or_none(turnover)   # caller-supplied (extract_turnover from pf.orders); None only if absent
    out["total_fees_paid"] = _finite_or_none(total_fees_paid)
    out["total_slippage_cost"] = _finite_or_none(total_slippage_cost)

    # --- PERIOD + CAPITAL (spec §4.7) ---------------------------------------------------------------------
    out["start"] = str(equity.index[0]) if len(equity) else None
    out["end"] = str(equity.index[-1]) if len(equity) else None
    out["bars"] = int(len(equity))
    out["start_value"] = _finite_or_none(equity.iloc[0]) if len(equity) else None
    out["end_value"] = _finite_or_none(equity.iloc[-1]) if len(equity) else None

    # --- BENCHMARK (spec §4.2/§4.3, C-bench) — over the common-date overlap; honest None on too-little ----
    bench = _benchmark_block(acc, returns, bm_returns, ann, bench_n_min, vbt, pd, np)
    out.update(bench)

    return out


def _returns_histogram(returns, np, bins: int = 25):
    """Bucket the per-bar returns into `bins` equal-width bins for the DISTRIBUTION centerpiece (C-metrics §5).

    Returns a list of ``{"lo", "hi", "count"}`` (the bin edges + the count), or None on a degenerate series
    (< 2 finite values, or all-equal so there is no spread). The worker computes it from the SAME returns
    series the moments use, so the histogram and skew/kurtosis describe the identical shape. The frontend
    renders the SVG from these counts (loss/gain/modal coloring) — it never re-buckets. Counts sum to the
    number of finite returns (np.histogram puts the max value in the last bin)."""
    vals = returns.dropna().to_numpy(dtype=float)
    if len(vals) < 2:
        return None
    lo, hi = float(vals.min()), float(vals.max())
    import math as _m  # local: the finite/degenerate guard
    if not (_m.isfinite(lo) and _m.isfinite(hi)) or hi <= lo:
        return None  # all-equal or non-finite -> no histogram (the cards still render)
    counts, edges = np.histogram(vals, bins=bins, range=(lo, hi))
    return [{"lo": float(edges[i]), "hi": float(edges[i + 1]), "count": int(counts[i])}
            for i in range(len(counts))]


def _benchmark_block(acc, returns, bm_returns, ann, bench_n_min, vbt, pd, np) -> dict:
    """The benchmark-gated metrics (alpha/beta/R²/IR/capture) over the strategy↔benchmark date overlap.

    Returns every benchmark key as None when there is no benchmark, too-little overlap, or a degenerate
    benchmark — the spec §5 / C-bench §5 honest-absence rule (never a fabricated 0). IR is annualized by
    sqrt(ann_factor) (the s107 Cowork override, spec §8.2) — the one place a VBT value is post-multiplied.
    """
    keys = ("benchmark_return", "alpha", "beta", "r_squared", "information_ratio",
            "up_capture", "down_capture", "benchmark_overlap_bars")
    out = {k: None for k in keys}
    if acc is None or returns is None or bm_returns is None or ann is None:
        return out
    # align by date intersection (the overlap), unconditionally (C-bench §4 — whichever side is shorter bounds it)
    common = returns.index.intersection(bm_returns.index)
    # The overlap bar count, surfaced for honest sample-size transparency: N_min (the noise threshold below
    # which a beta is too noisy to trust) stays a CANDIDATE until its provenance row exists (C-bench §5/§8),
    # so only the hard mathematical floor of 2 applies here; the UI shows benchmark_overlap_bars so a thin
    # overlap (a recent IPO) is read as such, never as a confident beta. bench_n_min is None until provenanced.
    out["benchmark_overlap_bars"] = int(len(common))
    floor = max(_BENCH_N_MIN_FLOOR, int(bench_n_min) if bench_n_min else _BENCH_N_MIN_FLOOR)
    if len(common) < floor:
        return out  # below the hard floor (a 2-point line) -> honest absence (benchmark_overlap_bars still reported)
    r = returns.reindex(common)
    bm = bm_returns.reindex(common)
    out["benchmark_return"] = _finite_or_none((1.0 + bm).prod() - 1.0)  # bm total return over the overlap
    # Rebuild the accessor over the overlap so the own-window factor matches the regressed window. The VBT
    # accessor methods RETURN nan (never raise) on a degenerate overlap — a zero-variance benchmark, a singular
    # regression (MCP-verified: alpha/beta/capture -> nan on a constant bm) — and _finite_or_none coerces every
    # nan to None (spec §5 honest absence). So no catch-all is needed: each metric is honest-None by the same
    # finite-coercion the rest of the page uses, never a fabricated number.
    oacc = r.vbt.returns(freq=ann["freq"], year_freq=ann["year_freq"])
    out["alpha"] = _finite_or_none(oacc.alpha(bm_returns=bm))            # VBT geometric alpha (spec §8.1)
    out["beta"] = _finite_or_none(oacc.beta(bm_returns=bm))             # cov/var (Ang Eq 6.8)
    ann_factor = float(oacc.ann_factor) if oacc.ann_factor is not None else None
    ir_per_period = _finite_or_none(oacc.information_ratio(bm_returns=bm))  # VBT per-period IR
    out["information_ratio"] = (
        _finite_or_none(ir_per_period * np.sqrt(ann_factor))
        if (ir_per_period is not None and ann_factor is not None and ann_factor > 0) else None)  # ANNUALIZED ×√ann_factor (spec §8.2)
    out["up_capture"] = _finite_or_none(oacc.up_capture_ratio(bm_returns=bm))
    out["down_capture"] = _finite_or_none(oacc.down_capture_ratio(bm_returns=bm))
    # R² = corr(r, bm)² over the overlap (single regressor; computed as squared correlation so it agrees with
    # the beta path to epsilon, spec §4.3). np.corrcoef returns nan on a zero-variance series (never raises).
    corr = _finite_or_none(np.corrcoef(r.values, bm.values)[0, 1])
    out["r_squared"] = _finite_or_none(corr * corr) if corr is not None else None
    return out
