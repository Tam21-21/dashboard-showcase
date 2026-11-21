"""research-runner compute-metrics entrypoint (M5 Track C, C2 §2 / C-metrics §2.3).

Baked at /app/compute_metrics_entrypoint.py — the third sibling of run_backtest_entrypoint.py and
run_sweep_entrypoint.py. The only process that imports vbt to produce a FULL-METRICS payload ON DEMAND for
ONE run or ONE cell. It reads /app/run_spec.json (the compute job spec the worker writes), computes the
complete metric set through the SINGLE formula authority strategies.helpers.full_metrics.compute_full_metrics,
and emits ONE JSON line {"metrics": {...}, "environment": {...}} (or an {"error": ...} line) on stdout for the
worker to read and POST to the backend cache-write route (the container is --network=none; the worker is the
only thing that talks to the backend).

Two modes (C2 §2):
  - single: DERIVE-on-read from the stored artifacts (equity.parquet + trades.parquet, fetched by the worker
    and mounted) + the persisted F6/cost scalars (carried in the spec). NO backtest re-run — the derived
    numbers stay byte-consistent with the charted equity (C2 §2.1).
  - cell: RE-RUN the one (ticker, combo) cell, three ways on the FROZEN param_mode (vectorized / zero /
    inline), replaying the FROZEN window cold, then extract the full set from the re-run via build_run_result
    (the ONE extractor every path routes through). The own-history annualization is derived inside
    compute_full_metrics from the (own-window) equity index, so the re-run reproduces the stored cell to
    epsilon (Gate 2; the from_value(sub.value)==sub.returns equality is MCP-verified for full-history AND
    ragged columns in the pinned build).

The benchmark group rides the cell re-run when the spec carries a frozen benchmark dataset (C-bench / C.4);
a single run, or a cell with no benchmark, passes bm_returns=None and the benchmark metrics render honest
absence (the spec §5 rule). No metric is ever fabricated.
"""
from __future__ import annotations  # lazy annotations

import dataclasses  # P2f: replace() the registry method's spec with the recorded one
import json  # the run_spec + the single stdout result/error line
import sys  # exit code
import traceback  # error reporting at the process boundary
from pathlib import Path  # artifact paths

import pandas as pd  # parquet I/O + the reconstructed equity/returns series
import vectorbtpro as vbt  # noqa: F401 — the engine; imported to mirror the sibling entrypoints' surface (cell re-run uses it)

from strategies.base import (  # the baked contract surface (engine-neutral)
    EmptyWindowedUniverse,
    RunResult,
    SettingRow,
    StrategyInput,
    SweepInput,
    SweepParam,
    coerce_param_value,
)
from strategies.base.slippage import price_path_slippage_cost  # the price-path per-fill slippage cost (vectorized re-run)
from strategies.base.vbt_extraction import build_run_result  # the ONE pf -> RunResult extractor (every path)
from strategies.helpers.annualization import resolve_annualization  # the own-history annualization (vectorized re-run)
from strategies.helpers.full_metrics import compute_full_metrics  # the single formula authority (C2 §1)
from strategies.helpers.portfolio import (  # lane SW (5.8, DECISIONS #383 decision 2): the
    # SizedStrategyInput subclass (#376 ruling 4's own resolved_sizing) plus the additive
    # entries_not_before field; not re-exported through strategies.helpers.__init__ (that file's
    # own map), imported directly, matching run_backtest_entrypoint.py's and
    # run_sweep_entrypoint.py's own import of it.
    LeadInStrategyInput,
    CompoundingEquityError,   # lane C9 (set 16b): a possible failure of long_only_signal_result,
    # caught (never crashed on) when the buy-and-hold arm construction hits it (AC-1's own "fold a
    # container-side arm failure into buy_hold_error" requirement).
    long_only_signal_result,  # lane C9 (set 16b): the buy-and-hold control arm's own engine path,
    # the SAME public helper ma_cross.py itself calls (AC-1: "through the engine's own portfolio
    # path", never a hand-rolled equity curve).
)
from strategies.helpers.sizing import (  # lane SW (5.8)
    clip_frame_with_lead_in, mask_entries_before, traded_bar_count,
    SizingError,  # lane C9 (set 16b): the base class every sizing failure the arm construction can
    # hit derives from (InsufficientLeadInError among them); caught, never crashed on.
)
from strategies.helpers.sizing_methods import resolve as resolve_sizing_method  # #376 ruling 4: the registry
from strategies.helpers.sizing import SizingSpec  # P2f: the five-field config a run recorded
from strategies.helpers.sweep_portfolio import cell_subportfolio, cell_metric_columns  # CS Amendment 1 (A1): the single-cell vectorized re-run sub-portfolio, and the shared ten-column extractor
# Reuse the proven loaders/helpers from the sibling entrypoints (no fork): the strategy loader + environment
# (single), and the member-OHLC read + cold window clip + wide-frame build (sweep).
from run_backtest_entrypoint import _load_strategy_class, _environment, RUN_SPEC_PATH, _log
from run_sweep_entrypoint import _load_member_ohlc, _clip_frame_to_window, _build_wide_frames, _run_result_cell

# Lane JC (candidate chain link spec 2026-09-23, section 1.4 step 2): the ten metric columns
# spec L-2's own "cell_metrics" field compares -- researchdb._RUN_STATS_COLUMNS (the 8-column
# research_run_cells rollup) plus researchdb._CELL_DIST_COLUMNS (skew, kurtosis), restated here
# rather than imported because this container never imports dashboard.backend (a different
# deployable, research_lab/CLAUDE.md's own boundary). Kept in the SAME order as researchdb.py's
# own tuple so a diff between the two files is a diff, not a reordering.
_REPLAY_CELL_METRIC_COLUMNS = (
    "total_return", "sharpe_ratio", "max_drawdown", "num_trades", "win_rate",
    "total_fees_paid", "total_slippage_cost", "profit_factor", "skew", "kurtosis",
)


def _read_equity_parquet(path: Path) -> pd.Series:
    """Reconstruct the equity Series (= pf.value) from a stored equity.parquet (the _write_equity shape).

    equity.parquet is {"timestamp": UNIX seconds UTC int, "equity": real} (run_backtest_entrypoint._write_equity).
    Rebuild the tz-aware DatetimeIndex from the seconds, so compute_full_metrics derives the SAME own-history
    annualization the run used (the index is the source of the factor, correctness spec §2)."""
    df = pd.read_parquet(path)  # the two-column frame written at run time
    idx = pd.to_datetime(df["timestamp"], unit="s", utc=True)  # seconds -> tz-aware UTC DatetimeIndex
    return pd.Series(df["equity"].to_numpy(), index=idx)  # the equity curve, exactly as pf.value


def _read_trades_parquet(path: Path) -> pd.DataFrame:
    """Read the stored trades.parquet (the canonical 20-column frame build_run_result wrote, renamed columns).

    compute_full_metrics reads ``pnl`` + ``return_pct`` off this frame (the canonical names). A no-trade run
    writes a 0-row frame (the columns still present), which compute_full_metrics maps to None trade metrics."""
    return pd.read_parquet(path)  # the canonical trades frame, column names + dtypes preserved by parquet


def _load_benchmark_returns(spec: dict):
    """The frozen benchmark ETF's daily simple returns for the benchmark group, or None (C-bench / C.4).

    The cell spec carries the resolved benchmark dataset_path when a benchmark was pre-committed at dispatch
    (C-bench §3); None (a single run -- the OPEN single-run-benchmark item -- or a cell whose exposure had no
    benchmark) -> the benchmark group renders honest absence (spec §5). The benchmark is clipped to the SAME
    frozen window the cell ran over, so its returns align to the strategy's own-history window; the regression
    runs over the date-INTERSECTION inside compute_full_metrics (C-bench §4), so a benchmark longer or shorter
    than the member is handled by the intersection, not assumed."""
    bench = spec.get("benchmark")  # {"dataset_path": "...", "ticker": "...", ...} or None
    if not bench or not bench.get("dataset_path"):
        return None  # no frozen benchmark -> honest absence (never a fabricated benchmark)
    bm = _clip_frame_to_window(  # clip the benchmark to the cell's FROZEN window (C-bench §4 member-aligned)
        _load_member_ohlc(bench["dataset_path"]), spec.get("window_start_ts"), spec.get("window_end_ts"))
    return bm["Close"].pct_change().fillna(0.0)  # simple daily returns; compute_full_metrics intersects to the overlap


def _replay_cell_metrics(rr: RunResult, spec: dict) -> dict:
    """Spec L-2's own 'cell_metrics' field, corrected by CS Amendment 1 (A1): the re-run's values
    for the ten metric columns (_REPLAY_CELL_METRIC_COLUMNS above), computed by the expressions
    that WROTE the stored cell, chosen by the run's recorded param_mode -- never one extractor for
    every mode, which was the round-1 build's wrong premise (the measured skew/kurtosis gap,
    docs/units/JC/reads/jc13_measurement.md).

    - 'vectorized': _rerun_cell_runresult's own vectorized branch already computed the ten columns
      straight off its own sub-portfolio through cell_metric_columns (the SAME shared helper
      sweep_portfolio_cells calls to WRITE a vectorized cell) and stashed them as the transport key
      'rr.stats["_vectorized_cell_metric_columns"]'; read that back rather than recomputing anything
      here, so there is exactly one call site for the vectorized expressions on the replay side too.
    - 'zero' / 'inline': _run_result_cell over the re-run's RunResult, unchanged (spec section 1.4's
      original words; these two modes' own write path already used this same extractor).
    Only the ten metric columns are kept: the cell's own identity fields (variation_id, ticker,
    param_json, dataset_sha256, and any funnel_* transport keys) are not part of the comparison.
    """
    if spec.get("param_mode") == "vectorized":
        full_cell = rr.stats["_vectorized_cell_metric_columns"]  # written by _rerun_cell_runresult's own vectorized branch
        return {col: full_cell.get(col) for col in _REPLAY_CELL_METRIC_COLUMNS}
    from strategies.base.vbt_extraction import _finite_or_none  # lazy, vbt-side (mirrors the sweep entrypoint's own import)
    member = spec["member"]
    combo_json = json.dumps(spec.get("combo") or {}, sort_keys=True)
    full_cell = _run_result_cell(rr, member, spec["variation_id"], combo_json, _finite_or_none)
    return {col: full_cell.get(col) for col in _REPLAY_CELL_METRIC_COLUMNS}


def _replay_values_match_exact(a, b) -> bool:
    """The 'exact' tolerance (spec 1.5): every compared column equal, NULL equal only to NULL."""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _write_replay_series(equity: "pd.Series", trades: "pd.DataFrame", out_dir: Path) -> None:
    """Write the replay's own equity/trades series to the mounted output dir (spec 1.4's own
    "Where": the worker reads these back and uploads them as replay_equity_v{v}_{t}.parquet /
    replay_trades_v{v}_{t}.parquet under the source run's id). equity.parquet's shape mirrors
    run_backtest_entrypoint._write_equity's own frame (timestamp seconds + equity real); trades
    keeps the canonical columns build_run_result already produced them in.

    Round 3 (the lead's own instruction after jc13's real e2e run): a VECTORIZED cell's own
    portfolio is built from a wide from_signals over a (param, ticker) MultiIndex column (spec
    section 1's "cell_subportfolio ... the one combo"), so build_run_result's own trades frame
    carries that whole tuple, e.g. (0.5, 'AAPL'), as an object-dtype 'column' field -- one raw
    Python tuple per row. Diagnosed directly against the real cell_subportfolio/build_run_result
    path (scripts/probes/judge_chain/.probe-logs/diag_trades_column.py, not a guess): pyarrow
    cannot write a column of raw tuples as a scalar type, and its own inference walks into the
    tuple's SECOND element ('AAPL') while trying to unify a type it started inferring as int64
    from the first, which is the exact ArrowInvalid jc13 hit. A run's own trades artifact never
    carries this: run_backtest_entrypoint's own zero/inline path builds a portfolio from a plain
    pd.Series (portfolio.py's long_only_signal_result), so vbt's own default single-column label
    is the plain scalar 0, matching what CANONICAL_TRADE_COLUMNS' own 'column' field always meant
    (research_lab/strategies/base/vbt_columns.py: "asset/column index within the portfolio") --
    never a compound key. A replay is by construction always exactly one member and one combo, so
    the tuple carries no information a reader needs: normalizing it to the SAME scalar 0 every
    other trades artifact already carries is what makes "the written replay trades file carry the
    same columns and types as a run's own trades artifact" true, at the root, for every param_mode
    (the zero/inline branches' own 'column' is already 0, so this is a no-op there).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({
        "timestamp": equity.index.asi8 // 10**9,  # tz-aware DatetimeIndex ns -> seconds, matching the run's own artifact
        "equity": equity.to_numpy(),
    })
    frame.to_parquet(out_dir / "replay_equity.parquet", index=False)
    if not pd.api.types.is_integer_dtype(trades["column"]):
        # A tuple (or any other compound) column label from a wide/vectorized re-run: collapse it
        # to the same trivial scalar every single-column portfolio's own trades artifact carries.
        # Checked by DTYPE, not row count, so a 0-row vectorized trades frame (still object dtype,
        # build_run_result never special-cases an empty result) is normalized the same way.
        # trades is a fresh, function-local frame here (never mutate the caller's own object).
        trades = trades.copy()
        trades["column"] = 0
    trades.to_parquet(out_dir / "replay_trades.parquet", index=False)


def _payload_from_runresult(rr: RunResult, bm_returns, bench_n_min) -> dict:
    """Compute the full-metrics payload from a re-run RunResult (the uniform cell path, all three modes).

    rr.stats carries the four cost/F6 scalars build_run_result extracts (max_gross_exposure + the pinned
    turnover + total_fees_paid + total_slippage_cost) for EVERY path (single, zero, inline, vectorized), so the
    full-metric compute is one call regardless of mode. The non-F6 metrics derive from rr.equity + rr.trades
    inside compute_full_metrics, at the own-history factor read off rr.equity's index."""
    s = rr.stats  # the flat per-run stats dict (8 + F6, the build_run_result contract)
    return compute_full_metrics(
        equity=rr.equity, trades=rr.trades,
        max_gross_exposure=s.get("max_gross_exposure"), turnover=s.get("turnover"),
        total_fees_paid=s.get("total_fees_paid"), total_slippage_cost=s.get("total_slippage_cost"),
        bm_returns=bm_returns, bench_n_min=bench_n_min)


def _single_metrics(spec: dict, bm_returns, bench_n_min) -> dict:
    """mode='single': DERIVE the full metrics from the stored artifacts + the persisted F6/cost scalars (C2 §2.1).

    No re-run: equity + trades are the stored artifacts (mounted at spec['artifacts_dir']); the F6 + cost
    scalars are the values persisted on research_run_stats at run time (carried in spec['f6']). A legacy run
    predating the F6 persist carries None for max_gross_exposure/turnover -> honest absence, never fabricated."""
    adir = Path(spec["artifacts_dir"])  # the mounted directory holding the run's equity/trades parquet
    equity = _read_equity_parquet(adir / "equity.parquet")  # = pf.value (the charted curve)
    trades = _read_trades_parquet(adir / "trades.parquet")  # the canonical trades frame
    f6 = spec.get("f6") or {}  # the persisted scalars: max_gross_exposure / turnover / total_fees_paid / total_slippage_cost
    return compute_full_metrics(
        equity=equity, trades=trades,
        max_gross_exposure=f6.get("max_gross_exposure"), turnover=f6.get("turnover"),
        total_fees_paid=f6.get("total_fees_paid"), total_slippage_cost=f6.get("total_slippage_cost"),
        bm_returns=bm_returns, bench_n_min=bench_n_min)


def _replay_lead_in_bars(member: dict, estimator_window: int) -> int:
    """P2e1: the lead-in, in bars, a replay clips with: the count its run recorded (member["lead_in_bars"],
    from research_run_funnel), so the clip reproduces the stored cell on any bar size; when that is absent
    (a run stored before P2g, or a payload built before P2e1) the bar rule every such run used,
    ``estimator_window`` bars. The replay never applies the intraday session rule on its own."""
    recorded = member.get("lead_in_bars")
    return estimator_window if recorded is None else int(recorded)


def _resolve_run_sizing_method(spec: dict, strat):
    """P2f (planner ruling 2026-10-29, DECISIONS #305): the sizing method THIS replay sizes with.

    When the payload carries "sizing_method" (the run recorded one), the replay uses that method id with
    the exact sizing spec the run recorded (the JSON text in "sizing_spec"), never the registry's current
    default: the registry method is looked up by the recorded id and only its spec is replaced. A recorded
    spec whose keys are not exactly SizingSpec's five fields raises a ValueError starting
    "recorded-sizing-spec-unreadable", which main prints as its error line. With "sizing_spec" alone (the
    run recorded a spec and no method) the strategy's declared method is resolved and the recorded spec
    replaces its own. With neither key (a run from an image older than #376 ruling 4) the replay is refused
    with a ValueError starting "sizing-not-recorded: ": no default sizing is used (P2j, DECISIONS #479
    item 9, #488 item 9, #489 item 4).
    """
    run_id = spec.get("target_run_id")
    if "sizing_method" not in spec and "sizing_spec" not in spec:
        raise ValueError(f"sizing-not-recorded: run {run_id} has no recorded sizing, so it cannot be replayed "
                         "with the sizing it used; no default sizing is used")
    five = {f.name for f in dataclasses.fields(SizingSpec)}
    try:
        recorded = json.loads(spec.get("sizing_spec"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"recorded-sizing-spec-unreadable: run {run_id} (the sizing_spec is not JSON)") from exc
    if not isinstance(recorded, dict) or set(recorded) != five:
        raise ValueError(f"recorded-sizing-spec-unreadable: run {run_id} (the sizing_spec is not exactly "
                         f"the five fields {sorted(five)})")
    method_id = spec["sizing_method"] if "sizing_method" in spec else strat.sizing_method
    return dataclasses.replace(resolve_sizing_method(method_id), spec=SizingSpec(**recorded))


def _rerun_cell_runresult(spec: dict, strat=None) -> RunResult:
    """mode='cell': RE-RUN the one (ticker, combo) cell three ways on the FROZEN param_mode, return a RunResult.

    Each branch replays the FROZEN window cold (clip before signals, AD-UU-3) and uses the EXACT engine path
    that produced the stored cell, so the result reproduces it (Gate 2):
      - vectorized: a single-member wide from_signals (the one combo) -> the own-history-sliced sub-portfolio
        (cell_subportfolio, the SHARED from_signals + slice sweep_portfolio_cells uses) -> build_run_result.
      - zero: the single-run run() over the cold slice (the _process_single_combo mechanism).
      - inline: the single-run run() PLUS strat._set_active_combo(combo) (the _process_inline_param mechanism),
        the combo read from the cell's persisted param_json.
    """
    # The frozen strategy: loaded from the mounted strategy.py in production; a pre-instantiated `strat` is
    # injected by the Gate-2 probe to test the re-run LOGIC directly (the strategy loader is tested elsewhere).
    strat = strat if strat is not None else _load_strategy_class()()
    member = spec["member"]  # {dataset_path, symbol, fees, slippage, dataset_sha256}
    pm = spec["param_mode"]  # the FROZEN production mode (the three-way discriminator)
    ws, we = spec.get("window_start_ts"), spec.get("window_end_ts")  # the FROZEN window (UTC-day epochs; None=open)
    init_cash = float(spec.get("init_cash", 10_000.0))  # the ma_cross reference default (carried in the spec)
    combo = spec.get("combo") or {}  # the cell's param combo ({} for zero; the canonical combo dict otherwise)
    setting_rows = [SettingRow(**row) for row in spec.get("settings", [])]  # the FROZEN run-level settings

    if pm == "zero":
        # #376 ruling 4 round 3: resolve the registry spec so strat.run() (which reaches
        # long_only_signal_result with no sizing= of its own, the strategy's own call site) sizes with
        # the SAME method this cell's provenance names, not portfolio.py's bare default (the lead's item,
        # the identical fix run_backtest_entrypoint.py and run_sweep_entrypoint.py already carry). Lane
        # SW (5.8) moves this resolution BEFORE the clip: clip_frame_with_lead_in needs estimator_window.
        sizing_method = _resolve_run_sizing_method(spec, strat)
        ohlc, entries_not_before = clip_frame_with_lead_in(  # lane SW (5.8): lead-in-aware COLD window slice
            _load_member_ohlc(member["dataset_path"]), ws, we, sizing_method.spec.estimator_window, member["symbol"],
            lead_in_bars=_replay_lead_in_bars(member, sizing_method.spec.estimator_window))
        # traded_bar_count (lane SW, 5.8), never len(ohlc.index): see run_sweep_entrypoint.py's own comment
        # on the identical replacement (a lead-in-extended frame's raw length can satisfy >=2 from lead-in
        # bars alone even when the TRADED window itself is empty; should not happen for a stored cell).
        if traded_bar_count(ohlc.index, entries_not_before) < 2:
            raise EmptyWindowedUniverse(f"the cell window leaves <2 bars for {member['symbol']}")
        data = LeadInStrategyInput(  # lane SW (5.8): the bound single-run input, now lead-in-aware
            prices=ohlc, resolved_sizing=sizing_method.spec, entries_not_before=entries_not_before)
        strat.validate_input(data, setting_rows)  # the V7 gate (default no-op)
        return strat.run(data, setting_rows)  # the full RunResult (the SAME _process_single_combo path)

    if pm == "inline":
        sizing_method = _resolve_run_sizing_method(spec, strat)  # round 3: same fix as the zero branch above
        ohlc, entries_not_before = clip_frame_with_lead_in(  # lane SW (5.8): lead-in-aware COLD window slice
            _load_member_ohlc(member["dataset_path"]), ws, we, sizing_method.spec.estimator_window, member["symbol"],
            lead_in_bars=_replay_lead_in_bars(member, sizing_method.spec.estimator_window))
        if traded_bar_count(ohlc.index, entries_not_before) < 2:  # lane SW (5.8): traded, not raw, bar count
            raise EmptyWindowedUniverse(f"the cell window leaves <2 bars for {member['symbol']}")
        params_decl = [SweepParam(d["name"], d["type"]) for d in spec["params_decl"]]  # the AST-extracted decls
        coerced = {p.name: coerce_param_value(combo[p.name], p.kind) for p in params_decl}  # combo at declared kinds
        data = LeadInStrategyInput(
            prices=ohlc, resolved_sizing=sizing_method.spec, entries_not_before=entries_not_before)
        strat._set_active_combo(coerced)  # inject the combo (self.param returns its values) -- the _process_inline_param mechanism
        strat.validate_input(data, setting_rows)  # the V7 gate (run after the inject; may key off a combo value)
        rr = strat.run(data, setting_rows)  # the full RunResult for this (combo, member)
        strat._set_active_combo(None)  # clear the injected combo (defensive)
        return rr

    if pm == "vectorized":
        params_decl = strat.sweep_params  # the declared SweepParam tuple (names + kinds + order)
        if not params_decl:  # a vectorized cell must come from a strategy that declares sweep_params
            raise ValueError(f"param_mode='vectorized' but {type(strat).__name__} declares no sweep_params")
        frames = _build_wide_frames([member])  # the member's 1-column wide close/open/high/low
        # #376 ruling 4: resolve the strategy's declared sizing method through the registry BEFORE the
        # clip (lane SW, 5.8, moved earlier than the original ordering: clip_frame_with_lead_in needs
        # estimator_window) and before the re-run sub-portfolio is built -- the vectorized cell re-run is
        # the one call site in this entrypoint that reaches cell_subportfolio directly, so it is wired the
        # same way run_sweep_entrypoint's own vectorized branch wires sweep_portfolio_cells. An unknown id
        # raises here, naming the id and the registered set, never a silent fall-back to cell_subportfolio's
        # own bare default.
        sizing_method = _resolve_run_sizing_method(spec, strat)
        # lane SW (5.8): lead-in-aware COLD window slice, one call per frame (mirrors _process_partition's
        # own four-separate-calls shape; all four frames share the identical single-member index).
        lead_in = _replay_lead_in_bars(member, sizing_method.spec.estimator_window)  # P2e1: one count for every frame
        close, entries_not_before = clip_frame_with_lead_in(
            frames["Close"], ws, we, sizing_method.spec.estimator_window, member["symbol"], lead_in_bars=lead_in)
        open_, _ = clip_frame_with_lead_in(
            frames["Open"], ws, we, sizing_method.spec.estimator_window, member["symbol"], lead_in_bars=lead_in)
        high, _ = clip_frame_with_lead_in(
            frames["High"], ws, we, sizing_method.spec.estimator_window, member["symbol"], lead_in_bars=lead_in)
        low, _ = clip_frame_with_lead_in(
            frames["Low"], ws, we, sizing_method.spec.estimator_window, member["symbol"], lead_in_bars=lead_in)
        factor = None  # lane D1: the member's per-day adjustment factor, when its dataset carries one
        if "Factor" in frames:
            factor, _ = clip_frame_with_lead_in(
                frames["Factor"], ws, we, sizing_method.spec.estimator_window, member["symbol"], lead_in_bars=lead_in)
        if traded_bar_count(close.index, entries_not_before) < 2:  # lane SW (5.8): traded, not raw, bar count
            raise EmptyWindowedUniverse(f"the cell window leaves <2 bars for {member['symbol']}")
        ann = resolve_annualization(close.index)  # the member's own (windowed) annualization factor
        params = {p.name: [coerce_param_value(combo[p.name], p.kind)] for p in params_decl}  # the ONE combo value per param
        entries, exits, _inds = strat.sweep_signals(  # the one-combo wide signals (1 combo x 1 ticker = 1 column)
            SweepInput(close=close, open=open_, high=high, low=low), params)
        # lane SW (5.8): mask out any entry strictly before entries_not_before, so a lead-in bar warms the
        # signal computation above without ever letting a trade open on it (mirrors _process_partition).
        entries = mask_entries_before(entries, entries_not_before)
        fees_arr = [[float(member["fees"])]]  # (1,1) per-column fees, R-COST
        slip_arr = [[float(member["slippage"])]]  # (1,1) per-column slippage
        sub = cell_subportfolio(  # the own-history-sliced sub-portfolio (the SHARED from_signals + slice)
            close, open_, high, low, entries, exits, fees_arr, slip_arr, ann["freq"], ann["year_freq"], init_cash,
            sizing=sizing_method.spec, factor=factor)
        total_slip = price_path_slippage_cost(sub, float(member["slippage"]))  # the 8th metric (per-fill), as the cell write
        rr = build_run_result(sub, indicators={}, total_slippage_cost=total_slip)  # the ONE extractor -> RunResult (8 + F6)
        # CS Amendment 1 (A1): a single-member replay re-run is NEVER ragged against itself (the
        # amendment's own "Unverified, and it fails closed" paragraph), so sharpe always takes the
        # plain full-history property here, the same branch sweep_portfolio_cells' own full-history
        # case uses. Computing the ten L-2 columns straight off `sub` through the SAME shared helper
        # the write path calls (cell_metric_columns) -- rather than later reconstructing them from
        # `rr.equity.pct_change()` the way _run_result_cell does for zero/inline -- is what makes
        # skew/kurtosis reproduce bit for bit; stashed as a transport key on rr.stats (the same idiom
        # run_sweep_entrypoint._run_result_cell already uses for its own funnel_* keys) rather than
        # widening RunResult's own frozen, engine-neutral six-slot contract for one param_mode.
        from strategies.base.vbt_extraction import _finite_or_none  # lazy, vbt-side (mirrors this file's other lazy imports)
        sharpe = _finite_or_none(sub.sharpe_ratio)
        rr.stats["_vectorized_cell_metric_columns"] = cell_metric_columns(sub, sharpe, float(member["slippage"]))
        return rr

    raise ValueError(f"unknown param_mode {pm!r} (expected 'vectorized' | 'zero' | 'inline')")


def _build_buy_hold_arm_runresult(spec: dict, sizing_method) -> RunResult:
    """Lane C9 (set 16b, `#421`/`#423`, `docs/units/C9/criteria.md` AC-1): the judge's
    buy-and-hold control arm (#451, kept by #479 item 8: one long entry at the first tradable
    bar, held to the last; not TSSB folio 14's long-on-every-bar arm). One long position from the replay window's own first
    tradable bar to its last, through `long_only_signal_result` -- the SAME public engine helper
    `strategies/ma_cross.py` itself calls, never a hand-rolled equity curve -- with the replay's
    own frozen costs and sizing, and NEVER the display benchmark table (`#193` safeguard 3: that
    table carries no cost model, so it cannot stand in for a graded control arm).

    Uses the SAME `sizing_method` `main()` already resolved once for the candidate cell (never a
    second, independently-resolved method that could silently diverge), so the arm's own first
    tradable bar (`entries_not_before`, the sizing method's own lead-in boundary) lines up with the
    candidate's own window exactly.
    """
    member = spec["member"]
    window_start_ts, window_end_ts = spec.get("window_start_ts"), spec.get("window_end_ts")
    ohlc, entries_not_before = clip_frame_with_lead_in(
        _load_member_ohlc(member["dataset_path"]), window_start_ts, window_end_ts,
        sizing_method.spec.estimator_window, member["symbol"],
        lead_in_bars=_replay_lead_in_bars(member, sizing_method.spec.estimator_window))
    data = LeadInStrategyInput(
        prices=ohlc, resolved_sizing=sizing_method.spec, entries_not_before=entries_not_before)
    entries = pd.Series(False, index=ohlc.index)
    first_traded_pos = ohlc.index.searchsorted(entries_not_before, side="left")
    entries.iloc[first_traded_pos] = True  # the ONE long entry, at the window's own first tradable bar
    exits = pd.Series(False, index=ohlc.index)  # no exit signal at all: held to the window's own last bar
    return long_only_signal_result(
        data, entries, exits, float(member["fees"]), float(member["slippage"]),
        init_cash=float(spec.get("init_cash", 10_000.0)), sizing=sizing_method.spec)


def _equal_weight_basket(member_returns: dict, init_cash: float):
    """The equal-weight, daily-rebalanced, survivorship-correct basket (correctness spec §6) from per-member
    own-window return Series. PURE (no re-run, no I/O) so the survivorship math is gate-testable directly.

    Outer-joins the member return Series on the UNION of their windows (NaN where a member has no bar that day),
    takes the per-day cross-sectional MEAN over the PRESENT members only (skipna — a member never contributes on
    a day it did not exist, so there is no survivorship leak), then the synthetic basket equity
    init_cash × (1 + r).cumprod(). Returns (basket_returns, basket_equity)."""
    frame = pd.concat(member_returns, axis=1)            # columns = symbols, index = the union of member windows
    basket_returns = frame.mean(axis=1, skipna=True)     # the equal-weight basket return per day (present members)
    basket_equity = init_cash * (1.0 + basket_returns).cumprod()  # the synthetic basket equity (the §6 curve)
    return basket_returns, basket_equity


def _aggregate_metrics(spec: dict, bm_returns, bench_n_min, strat=None) -> dict:
    """mode='aggregate': the equal-weight, survivorship-correct basket of every member's strategy returns at the
    chosen combo (correctness spec §6), with the full metric set + the benchmark group over the union overlap.

    Re-runs EACH member ONCE via _rerun_cell_runresult (the SAME three-way per-member re-run, frozen window cold),
    takes each member's per-bar strategy return (``equity.pct_change().fillna(0.0)`` — the §1 definition, the leading
    structural r=0 on the entry day), builds the cross-sectional skipna MEAN per day over the UNION of member windows
    (a member contributes only over its own observed life, so there is no survivorship leak), synthesizes the basket
    equity, and computes compute_full_metrics on it with ``trades=None`` (a basket has no single trade ledger, so the
    trade-level + F6 metrics render honest-None — correctness spec §5). A member whose frozen window leaves <2 bars is
    EXCLUDED and recorded in ``metrics['skipped_members']`` (honest, never a silent drop; it lives INSIDE the metrics
    dict because the worker caches only ``result['metrics']``)."""
    # the frozen strategy, loaded ONCE and reused across every member re-run (the per-process cost); a
    # pre-instantiated `strat` is injected by the Gate probe to test the basket LOGIC without the mounted loader.
    strat = strat if strat is not None else _load_strategy_class()()
    members = spec["members"]  # the FULL frozen universe (each: dataset_path, symbol, fees, slippage, dataset_sha256)
    init_cash = float(spec.get("init_cash", 10_000.0))  # the run's frozen starting capital (the ma_cross reference default)
    # the per-member re-run spec = the aggregate spec MINUS the member list, PLUS the one member (the
    # _rerun_cell_runresult shape: member + the frozen param_mode/combo/window/params_decl/settings/init_cash).
    base = {k: spec[k] for k in ("param_mode", "combo", "window_start_ts", "window_end_ts",
                                 "params_decl", "settings", "init_cash",
                                 "sizing_method", "sizing_spec", "target_run_id") if k in spec}
    member_returns = {}  # symbol -> the member's own-window strategy return Series
    skipped = []         # symbols whose frozen window left <2 bars (excluded from the basket, surfaced honestly)
    for m in members:
        try:
            rr = _rerun_cell_runresult({**base, "member": m}, strat=strat)  # the EXACT production per-member re-run
        except EmptyWindowedUniverse:
            skipped.append(m["symbol"])  # no in-window data -> legitimately not in the basket, never a fabricated 0
            continue
        member_returns[m["symbol"]] = rr.equity.pct_change().fillna(0.0)  # the §1 member strategy return series
    if not member_returns:  # every member's window left no data -> honest absence (the route renders unavailable)
        raise EmptyWindowedUniverse("the aggregate has no members with in-window data over the frozen window")
    _basket_returns, basket_equity = _equal_weight_basket(member_returns, init_cash)  # the §6 survivorship-correct basket
    metrics = compute_full_metrics(
        equity=basket_equity, trades=None,  # a basket has no single trade ledger -> honest-None trade/F6 (spec §5)
        max_gross_exposure=None, turnover=None, total_fees_paid=None, total_slippage_cost=None,
        bm_returns=bm_returns, bench_n_min=bench_n_min)  # the benchmark group over the union ∩ benchmark overlap
    # a basket has no trade ledger, so the trade-level metrics are not-applicable (compute_full_metrics returns
    # None for the trade-quality metrics on trades=None, but 0 for the COUNT — override it to None so the page
    # renders "not applicable" [a dash], never a misleading "0 trades" for a synthetic return series).
    metrics["total_trades"] = None
    metrics["skipped_members"] = skipped                 # INSIDE metrics (the only cached dict) — honest, never silent
    metrics["members_in_basket"] = len(member_returns)   # the basket denominator (honest composition)
    return metrics


def main() -> int:
    """Compute the full-metrics payload for the spec's run/cell, emit one result line, return a process code."""
    try:
        spec = json.loads(RUN_SPEC_PATH.read_text(encoding="utf-8"))  # the compute job spec (written by the worker)
        mode = spec["mode"]  # 'single' | 'cell'
        bench_n_min = spec.get("bench_n_min")  # C-bench candidate min-overlap (the backend passes it; None = no floor)
        bm_returns = _load_benchmark_returns(spec)  # the frozen benchmark returns, or None (honest absence)
        # #376 ruling 4 round 2 (E-R1-SF2): 'cell' mode loads a strategy and re-runs it, so its printed
        # environment carries sizing_method/sizing_spec of the method resolved for THIS cell, exactly as
        # the run_backtest/run_sweep entrypoints do (plan section 8.2). 'single' and 'aggregate' load no
        # strategy (single DERIVEs from stored artifacts; aggregate's own strat is loaded further down for
        # the basket re-run, not for provenance), so their environment carries neither field: the honest
        # absence _environment()'s own docstring already documents for a caller with no resolved method.
        sizing_method = None  # default for 'single'/'aggregate': no strategy loaded here, no fields to record
        if mode == "single":
            metrics = _single_metrics(spec, bm_returns, bench_n_min)  # DERIVE from the stored artifacts
        elif mode == "cell":
            strat = _load_strategy_class()()  # loaded HERE (not only inside _rerun_cell_runresult) so
            # main() can resolve and record the method for the environment line below, one instance reused
            sizing_method = _resolve_run_sizing_method(spec, strat)  # E-R1-SF2: the id/spec THIS cell used
            rr = _rerun_cell_runresult(spec, strat=strat)  # RE-RUN the cell three ways, same strat instance
            metrics = _payload_from_runresult(rr, bm_returns, bench_n_min)  # the full set from the re-run
            if spec.get("replay"):
                # Lane JC (candidate chain link spec, section 1.4 step 2): "the container re-runs
                # the cell, compares cell_metrics against expected_cell_metrics under the exact
                # rule, and on any difference prints an error line beginning replay-not-reproduced:
                # carrying both sets; the worker fails the job with it and uploads nothing." This
                # is #305's loud stop, checked here BEFORE anything is written to the output mount.
                #
                # The literal "replay-not-reproduced" below is HAND-TYPED on purpose (round 5 fix,
                # claims-auditor finding 3): this file runs inside the research-runner docker image
                # and never imports dashboard.backend, so it cannot reach
                # researchdb.WORD_REPLAY_NOT_REPRODUCED by name. researchdb.py's own comment above
                # that constant names this exact file as the one stated exception to "no use site
                # re-types the text". This is the same cross-deployable boundary already stated
                # elsewhere in this build for _REPLAY_CELL_METRIC_COLUMNS.
                cell_metrics = _replay_cell_metrics(rr, spec)
                expected = spec.get("expected_cell_metrics")
                if not isinstance(expected, dict):
                    print(json.dumps({"error": "replay-not-reproduced: expected_cell_metrics is "
                                                "absent from the replay payload; nothing to "
                                                "reproduce against"}), flush=True)
                    return 1
                if any(not _replay_values_match_exact(cell_metrics.get(c), expected.get(c))
                       for c in _REPLAY_CELL_METRIC_COLUMNS):
                    print(json.dumps({"error": f"replay-not-reproduced: cell_metrics={cell_metrics!r} "
                                                f"expected_cell_metrics={expected!r}"}), flush=True)
                    return 1
                # Reproduced: write the two replay series to the mounted rw output dir, and carry
                # both metric sets on the metrics dict so the worker can lift them into the L-2
                # "replay" object without recomputing anything (the worker never imports vbt).
                _write_replay_series(rr.equity, rr.trades, Path(spec["replay_out_dir"]))
                metrics["_replay_cell_metrics"] = cell_metrics
                metrics["_replay_expected_cell_metrics"] = expected

                # Lane C9 (set 16b, AC-1): the buy-and-hold control arm, computed in this SAME
                # container job, right beside the candidate's own reproduced replay. Written to a
                # `buy_hold` sub-directory of the SAME `replay_out_dir` (never a second,
                # hand-duplicated writer function: `_write_replay_series` again, unmodified), so the
                # candidate's own `replay_equity.parquet`/`replay_trades.parquet` and the arm's own
                # copies never collide inside the same rw mount. A bounded set of the arm's own
                # realistic failure modes (an empty windowed universe, a sizing failure, a
                # compounding-equity failure) is caught HERE and folded into `_replay_buy_hold_error`
                # rather than crashing the whole replay job: the candidate's own already-reproduced
                # replay must never be lost because the SEPARATE arm computation failed (AC-1's own
                # words, "rather than losing the candidate's own already-reproduced replay").
                try:
                    buy_hold_rr = _build_buy_hold_arm_runresult(spec, sizing_method)
                    _write_replay_series(
                        buy_hold_rr.equity, buy_hold_rr.trades,
                        Path(spec["replay_out_dir"]) / "buy_hold")
                    metrics["_replay_buy_hold_error"] = None
                except (EmptyWindowedUniverse, SizingError, CompoundingEquityError) as exc:
                    metrics["_replay_buy_hold_error"] = f"{type(exc).__name__}: {exc}"
        elif mode == "aggregate":
            metrics = _aggregate_metrics(spec, bm_returns, bench_n_min)  # the survivorship-correct equal-weight basket
        else:
            raise ValueError(f"unknown compute mode {mode!r} (expected 'single' | 'cell' | 'aggregate')")
        # ONE result line: the worker reads it and POSTs the payload to the cache-write route.
        print(json.dumps({"metrics": metrics, "environment": _environment(sizing_method)}), flush=True)
        _log(f"[metrics] run_id={spec.get('run_id')} mode={mode} computed {len(metrics)} metrics")
        return 0
    except Exception as e:  # noqa: BLE001 — process boundary, mirrors the sibling entrypoints: clean error line + fail
        traceback.print_exc(file=sys.stderr)  # full trace to stderr (the worker streams it)
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)  # machine-readable error on stdout
        return 1


if __name__ == "__main__":
    sys.exit(main())  # propagate the exit code to docker (the worker keys off rc)
