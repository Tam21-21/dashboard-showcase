"""research-runner run-sweep entrypoint (M5 1B §5; the sibling of run_backtest_entrypoint.py).

Baked into research-runner at /app/run_sweep_entrypoint.py. Owns the WIDE sweep compute: it is the only
process that imports vbt and touches portfolios, exactly like the single-run entrypoint, but instead of one
``strat.run`` it runs the universe-wide, parameter-grid sweep and emits per-chunk results on stdout for the
worker to POST (the container has ``--network=none``; the worker is the only thing that talks to the
backend). A sibling (not a branch of run_backtest_entrypoint) because the compute and the per-chunk
stdout-streaming differ enough from the single-run one-shot (§14.6); it REUSES the proven shared helpers
(``_load_strategy_class``, ``_environment``) from run_backtest_entrypoint so the strategy loader and the
environment authoring are not forked.

Flow (§5):
  1. read /app/run_spec.json (the sweep job payload, §3) and import /app/strategy.py;
  2. refuse a non-sweepable strategy, a 'shared_capital' mode (M7-paired), or a malformed payload — fail
     loud, no silent fallback (§3, §5.4);
  3. partition the universe by asset class; for each partition build the wide frame on that class's union
     index (so each partition is one clean calendar — no cross-class NaN), resolve the partition's
     index-derived annualization (R-CAL, §5.3), compute the wide signals once (the strategy's sweep_signals,
     §4), and run ONE from_signals per column-batch ≤ the cell cap (R-CEIL, §5.6);
  4. emit each batch as a chunk JSON line on stdout; the worker POSTs each chunk (the per-chunk atomic write,
     §6) and finalizes the run from the final sweep-done line.

The run-level annualization recording (§15.1): a single-asset-class sweep (one partition — the M5 default)
records its one factor; a mixed-asset-class sweep (more than one partition) records NULL run-level (the
honest absence — each cell is still annualized by its own partition's factor). The final sweep-done line
carries that decision for the worker to write at finalize.
"""
from __future__ import annotations  # lazy annotations

import dataclasses  # P2h: replace() the registry method's spec with the pinned one
import hashlib  # lane WF (6.2b): param_config_hash, sha256 of the canonical combo JSON, the same
# rule researchdb._param_config_hash uses; duplicated here because this container may never import
# dashboard.backend (the engine/backend boundary, AC-10/SR-16 applies to stage_runner.py, and this
# file is the engine side of that same boundary).
import json  # run_spec + the per-chunk / sweep-done stdout lines
import sys  # exit code
import traceback  # error reporting at the process boundary

import pandas as pd  # wide frames + parquet I/O
import vectorbtpro as vbt  # the engine (used here for nothing but to mirror the single entrypoint's import surface)

from strategies.base import (  # the baked contract surface (engine-neutral)
    EmptyWindowedUniverse,
    RunResult,
    SettingRow,
    StrategyInput,
    SweepInput,
    SweepNotSupported,
    canonical_combo_order,
    canonical_param_json,
    coerce_param_value,
)
from strategies.helpers import classify_zero_reason, resolve_annualization, sweep_portfolio_cells
from strategies.helpers.sweep_portfolio import CELL_REFUSAL_ERRORS  # lane D1/D1b: the per-cell refusals a sweep records and carries on past
from strategies.helpers.portfolio import LeadInStrategyInput  # lane SW (5.8, DECISIONS #383 decision 2): the
# SizedStrategyInput subclass (#376 ruling 4's own resolved_sizing) plus the additive entries_not_before
# field; not re-exported through strategies.helpers.__init__, which omits the sizing/portfolio submodules
# (see that file's own map), imported directly. This lane's ONE input type for both zero/inline members and
# a probe calling _process_single_combo/_process_inline_param directly (SizedStrategyInput itself is no
# longer built anywhere in this file: every construction site now uses LeadInStrategyInput, entries_not_before
# defaulting to None where no window applies, byte-identical to the bare SizedStrategyInput it replaces).
from strategies.helpers.sizing import (  # lane SW (5.8)/lane LB (5.8e)
    clip_frame_with_lead_in,
    SizingSpec,  # P2h: the five-field spec a pinned payload is rebuilt into
    refusal_reason,
    fill_reach_after,  # the last-bar fill reach, one bar past the granted window, for the fill only
    mask_entries_before,
    traded_bar_count,
)
from strategies.helpers.sizing_methods import resolve as resolve_sizing_method  # #376 ruling 4: the registry
from strategies.base import walkforward as engine_wf  # lane WF (6.2b): the small, hand-verified split primitive (#329 ruling 1)
# Reuse the single entrypoint's proven strategy loader + environment authoring (no fork, §14.6).
# Fold item 2b (round 1): also reuse its equity-writer and its run-output root, rather than
# re-inventing the (timestamp, equity) parquet shape a second time in this sibling file.
from run_backtest_entrypoint import _load_strategy_class, _environment, RUN_SPEC_PATH, _log, _write_equity, RUNS_ROOT, load_price_dataset

# Sentinel distinguishing "the caller passed no sizing kwarg at all" from "the caller explicitly
# passed None" (sweep_portfolio_cells's own EXPLICIT opt-out, unsized 100%-cash behaviour). Passing
# a bare None straight through here would silently flip every EXISTING caller of _process_partition
# that predates this unit (e.g. research_lab/probes/m5_trackC_gate2_probe.py's direct positional
# call) from sized to unsized, since sweep_portfolio_cells treats an explicit None differently from
# an omitted keyword. The sentinel keeps that old call path byte-for-byte unchanged.
_SIZING_NOT_GIVEN = object()


# M5 UU scale-defect 1.2: bound every emitted stdout result line by BYTES, never by member/cell count.
# The worker reads container stdout with an asyncio StreamReader whose default buffer limit is 64 KB; a
# single line that exceeds it raises ValueError('Separator is found, but chunk is longer than limit') and
# fails the whole sweep (run 14, a 100-member zero-param sweep, hit exactly this). The zero-param path
# emitted ALL member cells in one line, and the wide path emits one line per from_signals batch whose cell
# count is bounded by MEMORY (size_cap_cells / bars), not by line size — so at a large universe or a
# 1000-combo grid both could exceed 64 KB. _emit_cells_chunked re-splits any cell list into lines each
# under this budget, independent of how many cells the caller produced. The budget is well under 64 KB so
# the worker's raised StreamReader limit (run_sweep.py) is pure defense-in-depth, not load-bearing.
_EMIT_CHUNK_BYTES = 16 * 1024  # target max bytes per emitted stdout line (~4x margin under the 64 KB readline limit)

# Lane C5 (set 16c, #427, plan amendment 4, AC-1): duplicated from
# dashboard.backend.judge_checks.CSCV_ARTIFACT_FILENAME, never imported (the engine/backend
# boundary: this file's own hashlib/param_config_hash comment above already crosses it this way).
# Keep both literals identical by hand; scripts/probes/check5_cscv's own c02 and c06 are what
# would catch a drift.
_CSCV_ARTIFACT_FILENAME = "cscv_blocks.parquet"


def _maybe_write_cscv_artifact(run_id: int, s_blocks: "int | None", block_sums: dict,
                                bars_dropped: "int | None") -> None:
    """AC-1: write the run-wide cscv block-sums artifact into the mounted run-output dir, whenever
    `s_blocks` was resolved for this sweep (never a guessed one, amendment 4's own "the sweep
    still completes and writes no artifact" when S is unavailable). Written REGARDLESS of the
    trial count: a zero-cell sweep still gets a real, present, EMPTY file with the right columns
    (AC-4's own lifecycle clause), which is what lets check 5 tell "too few trials" apart from
    "missing artifact" (AC-5) -- an artifact that was never written at all, because S itself could
    not be read, is the ONLY case that reads as missing.

    Currently wired for the vectorized param_mode only (`sweep_portfolio_cells`'s own additive
    return, threaded through `_process_partition`): the zero/inline paths' own `_run_result_cell`
    is not extended here (a documented gap, not a silent one; see the builder report). A sweep run
    under either of those two modes therefore always reads "missing artifact" at grading time,
    the same honest reading a sweep stored before this build already gets.
    """
    if s_blocks is None:
        return  # S unavailable this sweep (amendment 4): write nothing, never guess one
    rows = []
    for (variation_id, ticker), blocks in block_sums.items():
        for block_index, (count, total, totalsq) in enumerate(blocks):
            rows.append({
                "variation_id": variation_id, "ticker": ticker, "block_index": block_index,
                "count": count, "sum": total, "sumsq": totalsq,
                # bars_dropped is None only when block_sums is also empty (no partition ever ran
                # the additive computation); 0 is the honest "nothing was ever summed" value.
                "bars_dropped": bars_dropped if bars_dropped is not None else 0,
            })
    out_dir = RUNS_ROOT / str(run_id)  # the :rw-mounted output dir (pre-created by the worker; never assumed here)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=["variation_id", "ticker", "block_index", "count", "sum", "sumsq", "bars_dropped"])
    frame.to_parquet(out_dir / _CSCV_ARTIFACT_FILENAME, index=False)


def _emit_cells_chunked(emit, cells: list[dict], chunk_index: int = 0) -> int:
    """Emit ``cells`` as one or more stdout lines, each bounded by the summed CELL bytes ``_EMIT_CHUNK_BYTES`` (1.2).

    The bound is on the cells' own serialized bytes; the ``{"chunk": N, "cells": [...]}`` frame + inter-cell
    commas (~40 bytes per line) ride on top, so the real line is marginally larger than the budget — negligible
    against the ~4x margin to the 64 KB readline limit (and the 8 MB raised worker ceiling).

    Each emitted line is ``{"chunk": i, "cells": [...]}`` — the SAME shape the worker already POSTs as one
    atomic, idempotent chunk (the backend writes on the cube PK (run_id, variation_id, ticker, row_scope),
    so the chunk boundary is purely a transport bound; ``chunk_index`` is informational). A cell larger than
    the budget on its own still emits alone (at least one cell per line, never an empty line). Returns the
    next chunk index so a caller emitting across several batches keeps the indices monotonic. Pure (no I/O
    beyond the injected ``emit``), so the probe can capture the lines and assert each is under the limit.
    """
    batch: list[dict] = []
    batch_bytes = 0
    for cell in cells:
        cell_bytes = len(json.dumps(cell))  # this cell's serialized contribution to the line
        if batch and batch_bytes + cell_bytes > _EMIT_CHUNK_BYTES:  # adding it would overflow -> flush first
            emit({"chunk": chunk_index, "cells": batch})
            chunk_index += 1
            batch, batch_bytes = [], 0
        batch.append(cell)
        batch_bytes += cell_bytes
    if batch:  # the final (or only) partial batch
        emit({"chunk": chunk_index, "cells": batch})
        chunk_index += 1
    return chunk_index


class AllCellsRefused(RuntimeError):
    """Lane D1: every cell of a sweep was refused by position sizing, so the run has no result at all.
    Each refusal was already sent on a cell_refusals line; this fails the run instead of a silent empty."""


def _refusal_record(exc: Exception, variation_id: int, ticker: str, param_json: str) -> dict:
    """Lane D1: one refused cell as the worker posts it (research_run_cell_refusals' own columns)."""
    message = str(exc)
    return {"variation_id": variation_id, "ticker": ticker, "param_json": param_json,
            "error": type(exc).__name__, "reason": refusal_reason(message), "message": message}


def _emit_cell_refusals(emit, refusals: list[dict]) -> None:
    """Lane D1: emit refused cells as {"cell_refusals": [...]} lines, byte-bounded like the cell chunks so
    a long list never exceeds the worker's readline limit. Each refusal line logs a count too."""
    batch: list[dict] = []
    batch_bytes = 0
    for record in refusals:
        record_bytes = len(json.dumps(record))
        if batch and batch_bytes + record_bytes > _EMIT_CHUNK_BYTES:
            emit({"cell_refusals": batch})
            batch, batch_bytes = [], 0
        batch.append(record)
        batch_bytes += record_bytes
    if batch:
        emit({"cell_refusals": batch})


def _check_not_all_refused(cells_emitted: int, refusals_emitted: int, committed) -> None:
    """Lane D1: refuse a sweep where sizing refused every cell (nothing was emitted, nothing was already
    committed by an earlier attempt, and at least one refusal was sent)."""
    if cells_emitted == 0 and refusals_emitted > 0 and not committed:
        raise AllCellsRefused(
            f"every cell of this sweep was refused by position sizing ({refusals_emitted} refusals); "
            "the run's cell refusals name each ticker, combo and reason"
        )


def _load_member_ohlc(dataset_path: str) -> pd.DataFrame:
    """Read one universe member's OHLCV parquet (title-case columns, DatetimeIndex), exactly as a price run."""
    return load_price_dataset(dataset_path)  # the M2-fetch shape (+ Factor on eodhd daily, lane D1) + tz-aware index


def _window_bound(ts, index) -> "pd.Timestamp | None":
    """Build a label-slice bound from a UTC-day epoch-second window value, matched to the frame index's tz.

    The window epochs are UTC-day-aligned seconds (B1, AD-UU-3). The bound is a tz-aware UTC Timestamp when
    the frame index is tz-aware (pandas compares absolute instants across zones correctly), and tz-naive
    (the UTC wall time) when the index is tz-naive — so the .loc slice never crashes on a tz-awareness
    mismatch. None passes through (open on that side = full history)."""
    if ts is None:  # open bound: no clip on this side
        return None
    t = pd.Timestamp(int(ts), unit="s", tz="UTC")  # the UTC-day-aligned bound
    if index.tz is None:  # a tz-naive parquet index -> a naive bound (keep the UTC wall time)
        t = t.tz_localize(None)
    return t


def _clip_frame_to_window(frame: pd.DataFrame, window_start_ts, window_end_ts) -> pd.DataFrame:
    """COLD-slice a price frame to ``[window_start, window_end]`` (each side only if non-NULL) BEFORE signals.

    The whole point of the window (AD-UU-3): clip the INPUT frame so the strategy's indicators are computed
    only within the window and never warm on a pre-window bar (a portfolio slice AFTER from_signals would be
    warm — pre-window data informing in-window signals, the era-mixing the window exists to prevent). The
    epochs are UTC-day-aligned (B1); the .loc label slice is inclusive on both present ends. A None bound is
    open on that side; both None -> the frame is returned unchanged (no window = full history, the common case)."""
    if window_start_ts is None and window_end_ts is None:  # no window -> full history (fast path)
        return frame
    lo = _window_bound(window_start_ts, frame.index)  # tz-matched lower bound (or None)
    hi = _window_bound(window_end_ts, frame.index)    # tz-matched upper bound (or None)
    return frame.loc[lo:hi]  # label slice; a None bound leaves that side open


def _build_wide_frames(members: list[dict]) -> dict[str, pd.DataFrame]:
    """Align a partition's members on their UNION index (NaN-pad gaps) into wide close/open/high/low frames.

    The columns are the member tickers (the symbol); the index is the union of every member's bars (R-CAL,
    §5.1 — vbt computes per-column results only over each column's live bars and does not invent pre-listing
    prices, so the NaN pad is correct). A single-member partition yields one-column frames. The four frames
    share the identical index and column order, so a column maps 1:1 to its ticker across all four.
    """
    per_field: dict[str, dict[str, pd.Series]] = {"Close": {}, "Open": {}, "High": {}, "Low": {}}
    factor_by_ticker: dict[str, pd.Series] = {}  # lane D1: only members whose dataset carries Factor (eodhd daily)
    for m in members:  # one member (ticker) at a time
        ohlc = _load_member_ohlc(m["dataset_path"])  # the member's OHLCV frame
        for field in per_field:  # collect each price field as a per-ticker Series
            per_field[field][m["symbol"]] = ohlc[field]  # title-case column -> {ticker: series}
        if "Factor" in ohlc.columns:
            factor_by_ticker[m["symbol"]] = ohlc["Factor"]
    if factor_by_ticker:  # a "Factor" frame exists only when some member has one; sizing reads factor 1 for the rest
        per_field["Factor"] = factor_by_ticker
    frames: dict[str, pd.DataFrame] = {}
    for field, by_ticker in per_field.items():  # build each wide frame by an outer-join concat on the union index
        wide = pd.DataFrame(by_ticker)  # columns = tickers, index = union of all members' indices (NaN-padded)
        wide.columns.name = "ticker"  # name the level so vbt carries `ticker` through to the result columns
        frames[field] = wide.sort_index()  # ascending time order (a union concat can interleave)
    return frames  # {"Close": wide, "Open": wide, "High": wide, "Low": wide}


def _run_level_annualization(partition_anns: list) -> dict | None:
    """The run-level annualization recording for a sweep (§15.1, the go/no-go fold).

    A single-asset-class sweep (one partition — the M5 default) records its one factor in the three run-level
    columns; a mixed-asset-class sweep (more than one partition) records NULL run-level (the honest absence —
    no single factor describes a multi-calendar run; each cell is still annualized by its own partition's
    factor, R-CAL). Pure so the probe can unit-test both branches. Timedeltas -> str for JSON, matching the
    single path's recording shape.
    """
    if len(partition_anns) == 1:  # single asset class -> one factor is the run's
        a = partition_anns[0]
        return {
            "annualization_factor": a["factor"],
            "annualization_freq": str(a["freq"]),
            "annualization_year_freq": str(a["year_freq"]),
        }
    return None  # mixed-calendar: no single run-level factor (§15.1)


def _combo_from_column(col, params_decl) -> dict:
    """Extract the parameter combo from a result column's MultiIndex tuple (drop the trailing ticker level).

    The wide result columns are ``(param1, param2, ..., ticker)`` (the strategy's sweep_signals levels). The
    combo is the param levels, coerced by each parameter's DECLARED kind (``params_decl`` = the strategy's
    ``sweep_params`` tuple) — so a window reads as an ``int`` (not numpy int64), making the canonical JSON
    deterministic. Returns ``{param_name: value}`` in the declared order.
    """
    return {p.name: coerce_param_value(col[i], p.kind) for i, p in enumerate(params_decl)}


def _process_partition(strat, members: list[dict], grid: dict, size_cap_cells: int,
                       init_cash: float, committed: set, params_decl,
                       variation_map: dict | None, emit,
                       window_start_ts=None, window_end_ts=None,
                       sizing=_SIZING_NOT_GIVEN, s_blocks: int | None = None,
                       cscv_out: "dict | None" = None) -> tuple[dict, dict, int]:
    """Run one asset-class partition: wide signals -> chunked from_signals -> per-chunk emit.

    Returns ``(variation_map, annualization, cells_emitted)``, the same 3-tuple every caller already
    unpacks. When ``cscv_out`` is a dict, the CSCV results are written into it under
    ``"block_sums"`` and ``"bars_dropped"`` (lane P3: the arity stays 3, so no existing caller breaks).
    ``variation_map`` (combo-json -> variation_id) is computed from the first partition and reused
    (every partition shares the same grid, so the same combo set + canonical sort -> the same ids);
    a later partition that disagrees is a bug and fails loud. ``annualization`` is this partition's
    index-derived ``(factor, freq, year_freq)`` (R-CAL). ``committed`` is the resume skip-set of
    ``(variation_id, ticker)`` already in the cube; emitted cells exclude it (the backend
    idempotence check is the correctness backstop regardless, §6.2). ``window_start_ts`` /
    ``window_end_ts`` are the COLD test-window epochs (AD-UU-3): the wide frames are clipped to them
    BEFORE signal generation, so indicators never warm on a pre-window bar.

    ``sizing`` (#376 ruling 4): the strategy's registry-resolved ``SizingSpec``, passed explicitly to
    ``sweep_portfolio_cells`` below when ``main()`` supplies one. Left at its ``_SIZING_NOT_GIVEN``
    sentinel by a caller that predates this unit (this function's own kwarg was added by this unit;
    an old positional call carries nothing here), which means "forward nothing" -- sweep_portfolio_cells
    then falls through to its own bare default, byte-for-byte the old behaviour.

    ``s_blocks`` (lane C5, set 16c, #427, plan amendment 4): check 5's own frozen cscv_split_count,
    forwarded to ``sweep_portfolio_cells`` unchanged. ``None`` (the default, e.g. S was
    unreachable this sweep, amendment 4) skips the whole additive computation, so
    ``cscv_block_sums`` comes back ``{}`` and ``cscv_bars_dropped`` comes back ``None``, and every
    OLD caller (this kwarg's own absence) sees byte-identical behaviour otherwise. When given,
    ``cscv_block_sums`` is ``{(variation_id, ticker): [(count, sum, sumsq), ...]}`` for every cell
    this partition emitted (a resumed, already-committed cell contributes nothing here, matching
    ``committed``'s own skip above; a resume of a sweep that started before this build therefore
    writes an artifact missing those older cells, a documented gap, never a silent guess).
    ``cscv_bars_dropped`` is this partition's own leftover count (see ``sweep_portfolio_cells``'s
    own docstring); a mixed-asset-class sweep's later partition OVERWRITES the earlier one's value
    here (a documented simplification: this build assumes one shared calendar, the M5 default).
    """
    cscv_block_sums: dict = {}       # lane C5: {(variation_id, ticker): [(count, sum, sumsq), ...]}
    cscv_bars_dropped: "int | None" = None  # lane C5: this partition's own leftover-bar count
    param_names = tuple(p.name for p in params_decl)  # the declared parameter order (variation sort key)
    frames = _build_wide_frames(members)  # the partition's wide close/open/high/low on its union index
    close, open_, high, low = frames["Close"], frames["Open"], frames["High"], frames["Low"]
    factor = frames.get("Factor")  # lane D1: wide per-day adjustment factor, or None when no member carries one
    if factor is not None:
        factor = factor.reindex(index=close.index)  # same union index as the price frames, so every clip below matches

    # B4 (AD-UU-3) COLD window slice: clip the wide frames to [window_start, window_end] BEFORE sweep_signals,
    # so the strategy's indicators are computed only within the window (cold by construction). This is NOT a
    # portfolio slice after from_signals (that would be warm: pre-window bars informing in-window signals, the
    # era-mixing the window prevents). The AD-M5-16 own-history slice (sweep_portfolio_cells) then runs WITHIN
    # the already-windowed data: a member listing mid-window gets its listing as first_valid; an older member
    # gets the window start. resolve_annualization below derives off the clipped index automatically.
    #
    # Lane SW (5.8, DECISIONS #383 decision 2): once main() has resolved a registry sizing spec (the ordinary
    # case; `sizing` is the _SIZING_NOT_GIVEN sentinel only for an old direct caller that predates that fix),
    # the clip is LEAD-IN-AWARE: it starts estimator_window bars before window_start_ts (all or nothing --
    # InsufficientLeadInError, never a shorter lead-in) so the estimator can warm up on real pre-window bars,
    # and entries_not_before (the window's own start) travels forward to mask any entry the lead-in bars would
    # otherwise let through. Four separate calls, one per frame, mirrors the four separate _clip_frame_to_window
    # calls this code already made: all four frames share the identical union index (_build_wide_frames' own
    # doc), so each call independently derives the identical clip bounds and entries_not_before boundary.
    has_sizing = sizing is not _SIZING_NOT_GIVEN  # True whenever main() resolved a registry spec (E-R1-SF3)
    partition_label = f"partition[{members[0]['asset_class']}]"  # members is never empty (partitions.setdefault)
    if has_sizing:
        close, entries_not_before = clip_frame_with_lead_in(
            close, window_start_ts, window_end_ts, sizing.estimator_window, partition_label)
        open_, _ = clip_frame_with_lead_in(open_, window_start_ts, window_end_ts, sizing.estimator_window, partition_label)
        high, _ = clip_frame_with_lead_in(high, window_start_ts, window_end_ts, sizing.estimator_window, partition_label)
        low, _ = clip_frame_with_lead_in(low, window_start_ts, window_end_ts, sizing.estimator_window, partition_label)
        if factor is not None:
            factor, _ = clip_frame_with_lead_in(factor, window_start_ts, window_end_ts, sizing.estimator_window, partition_label)
    else:
        close = _clip_frame_to_window(close, window_start_ts, window_end_ts)
        open_ = _clip_frame_to_window(open_, window_start_ts, window_end_ts)
        high = _clip_frame_to_window(high, window_start_ts, window_end_ts)
        low = _clip_frame_to_window(low, window_start_ts, window_end_ts)
        if factor is not None:
            factor = _clip_frame_to_window(factor, window_start_ts, window_end_ts)
        entries_not_before = None  # no spec resolved: no lead-in concept applies, byte-unchanged old behaviour

    # P2g: the lead-in bars this partition's clip used, counted once on the clipped frame: the bars before
    # entries_not_before (0 when nothing led in: no window start, or no sizing spec resolved).
    partition_lead_in_bars = 0 if entries_not_before is None else int((close.index < entries_not_before).sum())

    # SF-A: the all-empty windowed-universe check BEFORE resolve_annualization. The cold clip can leave the
    # whole partition with <2 union TRADED bars (every member lists after the window, or the window excludes
    # all data); resolve_annualization would then raise the GENERIC AnnualizationError. Raise the SPECIFIC
    # EmptyWindowedUniverse first so the user sees "no bars in this window", never the confusing generic error.
    # A window where at least one member has >=2 in-window bars passes (per-member empties surface as designed).
    # traded_bar_count (lane SW, 5.8), never len(close.index): once the frame can carry lead-in bars, its own
    # raw length can satisfy >=2 from lead-in bars ALONE even when the TRADED window itself is empty, so this
    # check must read the traded count, not the frame's whole length (entries_not_before=None -> the two agree
    # exactly, the pre-lane behaviour).
    if traded_bar_count(close.index, entries_not_before) < 2:
        raise EmptyWindowedUniverse(
            f"the test window leaves fewer than 2 bars across all {len(members)} member(s) of this partition "
            f"(window_start_ts={window_start_ts}, window_end_ts={window_end_ts}); widen the window or the universe"
        )

    # R-CAL (§5.3): the partition's annualization, index-derived on its (now windowed) shared calendar (the
    # same resolve_annualization #166 the single path uses), so each cell's Sharpe is annualized correctly.
    ann = resolve_annualization(close.index)  # {factor, freq, year_freq}

    # The wide signals for the whole partition in one parameterized pass (§4). The grid values are coerced
    # to each param's declared kind first (a JSON grid carries numbers untyped).
    params = {p.name: [coerce_param_value(v, p.kind) for v in grid[p.name]] for p in params_decl}
    entries, exits, indicators = strat.sweep_signals(SweepInput(close=close, open=open_, high=high, low=low), params)
    # Lane SW (5.8, DECISIONS #383 decision 2): mask out every entry column's signal strictly before
    # entries_not_before, so a lead-in bar can warm sweep_signals' own indicators (it was loaded into
    # close/open/high/low above) without ever letting a trade open on it. entries is a WIDE DataFrame here
    # (one column per (combo, ticker)); mask_entries_before handles that shape identically to a plain Series
    # (its own docstring: an index-aligned Series condition broadcasts across every column row-wise).
    entries = mask_entries_before(entries, entries_not_before)
    # Memory note (R-CEIL): `size_cap_cells` bounds the from_signals PEAK per batch (the measured ceiling),
    # but entries/exits/indicators here span the FULL partition x combo product and stay resident across all
    # batches (the slices below are views). For a strategy with K indicators, C combos, T tickers, B bars the
    # signal-generation peak is O(K x C x T x B), NOT bounded by the cap — so 1C must set the cap knowing a
    # wide grid x many-indicator strategy can push peak memory above it. Chunking signal generation itself is
    # a later refinement if a real grid ever exceeds memory before from_signals (the cap, the measured
    # ceiling, is what 1B enforces).

    # The combo per column -> the canonical variation_id map (shared across partitions). Build it from the
    # first partition; assert later partitions match (same grid -> same combos -> same ids).
    combos_by_col = [_combo_from_column(c, params_decl) for c in entries.columns]  # one combo per column
    unique_jsons = []  # de-dup combos preserving first-seen, then canonical-sort
    seen = set()
    for combo in combos_by_col:
        j = canonical_param_json(combo)
        if j not in seen:
            seen.add(j); unique_jsons.append(combo)
    ordered = canonical_combo_order(unique_jsons, param_names)  # the deterministic order (variation_id source)
    this_map = {canonical_param_json(c): vid for vid, c in enumerate(ordered)}  # combo-json -> variation_id
    if variation_map is None:
        variation_map = this_map  # first partition defines the ids
    elif this_map != variation_map:  # every partition shares the grid; a mismatch is a contract bug
        raise ValueError("partition combo set/order differs across asset classes — variation_id would be unstable")

    # Per-ticker cost lookup (R-COST, §5.2): each column charges its ticker's class profile, resolved by 1C
    # and carried in the spec per member. The engine reads it declaratively; it never imports the backend.
    fees_by_ticker = {m["symbol"]: float(m["fees"]) for m in members}
    slip_by_ticker = {m["symbol"]: float(m["slippage"]) for m in members}
    sha_by_ticker = {m["symbol"]: m["dataset_sha256"] for m in members}

    tickers = entries.columns.get_level_values("ticker")  # the ticker per column (aligned to columns)
    bars = len(close.index)  # the partition's bar count (the cell-cap denominator)
    ncols = entries.shape[1]  # total columns this partition would simulate
    # R-CEIL (§5.6): chunk the columns so batch_cols * bars <= the cap. The cap is on the from_signals pass
    # (the measured ceiling). At least one column per batch (a single column larger than the cap still runs).
    batch_cols = max(1, size_cap_cells // max(1, bars))
    cells_emitted = 0
    chunk_index = 0
    for start in range(0, ncols, batch_cols):  # one column-batch (one from_signals) at a time
        sl = slice(start, min(start + batch_cols, ncols))  # this batch's column positions
        b_entries = entries.iloc[:, sl]  # batch column slices of the signal/indicator frames
        b_exits = exits.iloc[:, sl]
        b_inds = {name: frame.iloc[:, sl] for name, frame in indicators.items()}  # batch indicator slices
        b_tickers = list(tickers[sl])  # the ticker per batch column (positionally aligned to b_entries)
        fees_arr = [[fees_by_ticker[t] for t in b_tickers]]  # (1, batch_ncols) per-column fees row vector
        slip_arr = [[slip_by_ticker[t] for t in b_tickers]]  # (1, batch_ncols) per-column slippage row vector
        slip_rates = [slip_by_ticker[t] for t in b_tickers]  # per-column slippage scalars for the 8th metric

        # Pass the FULL partition close/open/high/low (unique-ticker columns): vbt broadcasts each ticker
        # column to the batch's matching `ticker`-level columns (verified against the pinned build), so a
        # column slice of entries simulates correctly without rebuilding a per-column-duplicated price frame.
        # #376 ruling 4: sizing= is forwarded EXPLICITLY only when main() resolved one; otherwise the
        # kwarg is omitted entirely (never sizing=None, which is sweep_portfolio_cells's own distinct
        # unsized opt-out) so an old caller of _process_partition sees no behaviour change at all.
        sweep_cells_kwargs = {} if sizing is _SIZING_NOT_GIVEN else {"sizing": sizing}
        batch_refusals: list[dict] = []  # lane D1: columns whose sizing refused; the rest of the batch carries on
        # Lane C5 (set 16c, #427): forward s_blocks only when main() resolved one, the SAME
        # omit-the-kwarg-entirely convention `sizing` above already follows, so a caller that
        # predates this lane sees no behaviour change at all.
        if s_blocks is not None:
            sweep_cells_kwargs["s_blocks"] = s_blocks
        batch_cells = sweep_portfolio_cells(
            close=close, open=open_, high=high, low=low,
            entries=b_entries, exits=b_exits, indicators=b_inds,
            fees_arr=fees_arr, slippage_arr=slip_arr, slippage_rates=slip_rates,
            freq=ann["freq"], year_freq=ann["year_freq"], init_cash=init_cash,
            **sweep_cells_kwargs,
            factor=factor,  # lane D1: real-share sizing for members carrying a Factor column
            refusals=batch_refusals,
        )
        if batch_refusals:  # lane D1: record each refused cell where a person can see it (the worker posts these)
            refusal_records = []
            for refused in batch_refusals:
                rcol = refused["column"]
                rjson = canonical_param_json(_combo_from_column(rcol, params_decl))
                refusal_records.append({
                    "variation_id": variation_map[rjson], "ticker": rcol[-1], "param_json": rjson,
                    "error": refused["error"], "reason": refused["reason"], "message": refused["message"],
                })
            _emit_cell_refusals(emit, refusal_records)
            _log(f"[sweep] {len(refusal_records)} cell(s) refused by sizing in this batch; the rest carry on")

        out_cells = []  # the per-cell dicts to POST for this chunk
        for raw in batch_cells:  # one per-column cell from the helper
            col = raw["column"]  # the (param..., ticker) tuple
            ticker = col[-1]  # the ticker level is last
            combo = _combo_from_column(col, params_decl)  # the param combo
            param_json = canonical_param_json(combo)  # the canonical combo JSON (-> param_config_hash, variation row)
            variation_id = variation_map[param_json]  # the stable combo id
            if (variation_id, ticker) in committed:  # resume: skip an already-committed cell (idempotence backstop too)
                continue
            # Lane C5 (set 16c, #427): this cell's own per-block sums, additive, present only when
            # s_blocks was given (sweep_portfolio_cells's own contract above). Kept OUT of
            # out_cells below (never spread through raw["stats"]/raw["funnel"]): the per-chunk
            # POST goes to complete_sweep_chunk, whose schema this lane never touches; the block
            # sums travel to the LOCAL artifact file main() writes after every partition, never
            # over that wire.
            if "cscv_block_sums" in raw:
                cscv_block_sums[(variation_id, ticker)] = raw["cscv_block_sums"]
                cscv_bars_dropped = raw["cscv_bars_dropped"]  # every trial in one partition shares
                # the same shared-index block partition, so the last cell's value equals every
                # other cell's; overwritten here rather than asserted, matching this function's
                # own docstring on the mixed-asset-class simplification.
            # Lane DF2 (`6.1e`): the strategy's own per-combo lookback and used-degrees-of-freedom
            # declarations, so complete_sweep_chunk can write both onto research_run_variations.
            # None, never a substituted number, when the strategy has not declared the method (the
            # base Strategy raises NotImplementedError by name). The vectorized sweep is NEVER
            # refused for an undeclared strategy, unlike walk-forward's own lookback refusal below.
            try:
                lookback_bars_value = strat.lookback_bars(combo)
            except NotImplementedError:
                lookback_bars_value = None
            try:
                used_degrees_of_freedom_value = strat.used_degrees_of_freedom(combo)
            except NotImplementedError:
                used_degrees_of_freedom_value = None
            funnel = dict(raw["funnel"])  # the 9 numeric counts
            funnel["zero_reason"] = classify_zero_reason(funnel)  # derive the WHY-ZERO bucket (the shared classifier)
            funnel["lead_in_bars"] = partition_lead_in_bars  # P2g: the lead-in bars the shared clip used
            out_cells.append({
                "variation_id": variation_id,  # the combo id (stable across partitions)
                "ticker": ticker,  # the per-cell symbol
                "param_json": param_json,  # the canonical combo (backend hashes -> param_config_hash; writes variation row)
                "dataset_sha256": sha_by_ticker[ticker],  # the ticker's content identity (the count scope)
                "lookback_bars": lookback_bars_value,  # DF2: the strategy's own declared window, None if undeclared
                "used_degrees_of_freedom": used_degrees_of_freedom_value,  # DF2: Pardo's Udf, None if undeclared
                **raw["stats"],  # the 8 metrics + the 2 Track-B distribution scalars (skew, kurtosis)
                **funnel,  # the 9 funnel counts + zero_reason + lead_in_bars
            })
        if out_cells:  # emit only non-empty chunks (a fully-committed batch emits nothing on resume)
            # 1.2: re-split this from_signals batch into BYTE-bounded lines (a wide grid x many members can
            # make one batch's cell list exceed the 64 KB readline limit). chunk_index threads across batches
            # within THIS partition (informational only — it resets per partition; the worker ignores it and
            # writes idempotently on the cube PK). The worker POSTs each line as one atomic chunk.
            chunk_index = _emit_cells_chunked(emit, out_cells, chunk_index)
            cells_emitted += len(out_cells)

    if cscv_out is not None:
        cscv_out["block_sums"] = cscv_block_sums
        cscv_out["bars_dropped"] = cscv_bars_dropped
    return variation_map, ann, cells_emitted


def _run_result_cell(res: RunResult, member: dict, variation_id: int, param_json: str, finite_or_none,
                     *, lead_in_bars: "int | None" = None) -> dict:
    """Build one full sweep-shaped cell from a member's single-run RunResult (P-2 / M5 C).

    The pure extraction of the former ``_zero_param_cell``, parameterized on ``variation_id``/``param_json`` so
    BOTH the zero-param path (variation_id 0, param_json "{}") and the M5 C per-combo path (the combo's id +
    canonical JSON) build the IDENTICAL cell shape from a RunResult. The 8 scalars come from RunResult.stats
    (the SAME 8 keys the single run computes, _RUN_STATS_COLUMNS). skew/kurtosis are computed from the
    equity-derived per-bar returns ``equity.pct_change().fillna(0.0)`` via the IDENTICAL ``Series.skew()``/
    ``Series.kurtosis()`` + ``_finite_or_none`` expressions the wide sweep path uses (sweep_portfolio_cells) —
    that this equals the vbt ``pf.returns`` the sweep path computes its moments on is PROVEN by the B4 SF3.1
    probe leg, not assumed. The funnel comes from RunResult.stats's ``funnel_*`` keys (the single-run path folds
    the counts there) — NOT compute_signal_funnel, which needs a live vbt portfolio the RunResult does not carry
    (SF2). A bespoke strategy that bypasses the shared portfolio helper carries no ``funnel_*`` keys, so its cell
    carries NO funnel keys and the chunk write skips the funnel row (get_run_cells never joins research_run_funnel).
    """
    stats = res.stats  # the flat per-run metrics dict (8 keys + optional funnel_* transport keys)
    returns = res.equity.pct_change().fillna(0.0)  # per-bar returns from the equity curve (== pf.returns, SF3.1)
    cell = {
        "variation_id": variation_id,            # 0 for the zero-param run; the combo id for the per-combo path
        "ticker": member["symbol"],              # the member ticker (bare, AD-M5-14)
        "param_json": param_json,                # "{}" for zero-param; the canonical combo JSON for per-combo
        "dataset_sha256": member["dataset_sha256"],  # the member's content identity (the count scope)
        # the 8 _RUN_STATS_COLUMNS, read by the same keys the single run computes (funnel_* stripped below)
        "total_return": stats["total_return"],
        "sharpe_ratio": stats["sharpe_ratio"],
        "max_drawdown": stats["max_drawdown"],
        "num_trades": int(stats["num_trades"]),
        "win_rate": stats["win_rate"],
        "total_fees_paid": stats["total_fees_paid"],
        "total_slippage_cost": stats["total_slippage_cost"],
        "profit_factor": stats["profit_factor"],
        # the 2 Track-B distribution scalars from the equity-derived returns (identical moments to the wide path)
        "skew": finite_or_none(returns.skew()),       # third-moment shape; None on a too-short window
        "kurtosis": finite_or_none(returns.kurtosis()),  # excess kurtosis; None on a too-short window
    }
    # The funnel from RunResult.stats's funnel_* keys (the single path folds them there). NOT
    # compute_signal_funnel (no pf here, SF2). A bespoke strategy carries no funnel_* -> no funnel keys on the
    # cell -> the chunk write skips the funnel row for it (an honest opt-out, not a fabricated funnel).
    funnel_counts = {k[len("funnel_"):]: int(v) for k, v in stats.items() if k.startswith("funnel_")}
    if funnel_counts:  # present for a strategy built the sanctioned way (the shared portfolio helper)
        cell.update(funnel_counts)                   # the 9 funnel counts, flat (matches the wide cell shape)
        cell["zero_reason"] = classify_zero_reason(funnel_counts)  # the WHY-ZERO bucket (the shared classifier)
        if lead_in_bars is not None:  # P2g: the lead-in bars this member's clip used (both sweep callers pass it);
            cell["lead_in_bars"] = lead_in_bars  # a caller that does not know it leaves the key out, stored NULL
    return cell


def _process_single_combo(strat, universe: list[dict], window_start_ts, window_end_ts,
                          settings: list[dict], committed: set, emit, sizing_spec=None) -> int:
    """The zero-parameter single-combo path (P-2): run each member's single-run ``run`` over its COLD slice.

    Each member is run through the SAME single-run ``run`` path single-dataset/event runs use (every strategy
    implements it), over its window-clipped price frame, cold by construction (signals generated only within
    the window). The cell is built from the RunResult by ``_run_result_cell`` (variation_id 0, param_json "{}").
    A member with fewer than 2 in-window bars is SURFACED (logged) and EXCLUDED, never zero-padded (the
    NOT-NULL cell schema forbids a
    null-metric row, so a fabricated zero cell would read as a real flat result; the log is the honest
    surface, the no-silent-subset contract). If EVERY member is empty the run fails loud with
    EmptyWindowedUniverse (P-3). Emits the cells in byte-bounded chunks (1.2); returns the cell count.

    ``sizing_spec`` (#376 ruling 4 round 2, E-R1-SF3): the registry-resolved spec ``main()`` looked up
    before any branch ran; bound onto each member's ``LeadInStrategyInput`` (lane SW, 5.8) so ``strat.run()``
    (which calls ``long_only_signal_result`` with no ``sizing=`` of its own) sizes with the SAME method
    recorded in this run's environment, not with ``portfolio.py``'s bare default.
    """
    from strategies.base.vbt_extraction import _finite_or_none  # the exact single-path nullable-ratio coercion (lazy, vbt-side)

    setting_rows = [SettingRow(**row) for row in settings]  # the frozen run-level settings (the run reads its params + neutral costs here)
    out_cells: list[dict] = []  # one cell per resolvable member, all at variation_id=0
    empty_members: list[str] = []  # members with <2 in-window bars (surfaced + excluded, P-3)
    for m in universe:
        raw_ohlc = _load_member_ohlc(m["dataset_path"])
        # Lane SW (5.8, DECISIONS #383 decision 2): lead-in-aware clip when a registry spec is bound (the
        # ordinary case); the plain window-only clip, byte-unchanged, when sizing_spec is None (a probe
        # calling this function directly, matching this function's own pre-lane fallback for that shape).
        if sizing_spec is None:
            ohlc = _clip_frame_to_window(raw_ohlc, window_start_ts, window_end_ts)  # COLD slice
            entries_not_before = None
        else:
            ohlc, entries_not_before = clip_frame_with_lead_in(
                raw_ohlc, window_start_ts, window_end_ts, sizing_spec.estimator_window, m["symbol"])
        # traded_bar_count (lane SW, 5.8), never len(ohlc.index): a lead-in-extended frame can satisfy
        # >=2 from lead-in bars alone even when the TRADED window itself is empty (entries_not_before=None
        # makes the two agree exactly, the pre-lane behaviour).
        if traded_bar_count(ohlc.index, entries_not_before) < 2:  # no usable TRADED history -> surface + exclude (never zero-pad: NOT-NULL cell schema)
            empty_members.append(m["symbol"])
            continue
        ticker = m["symbol"]
        if (0, ticker) in committed:  # resume: this member's cell is already committed (idempotence backstop too)
            continue
        # #376 ruling 4 round 2 (E-R1-SF3): bind the registry-resolved spec (None when main() never
        # resolved one, e.g. a probe calling this function directly) so this cell sizes with the
        # method this run's environment records, matching the single-run entrypoint's own fix.
        # LeadInStrategyInput (lane SW, 5.8) rather than a bare SizedStrategyInput: entries_not_before=None
        # behaves identically to the old input (long_only_signal_result's own getattr default), so this is
        # the one input type every caller of this function now receives, sized or not, windowed or not.
        data = LeadInStrategyInput(prices=ohlc, resolved_sizing=sizing_spec, entries_not_before=entries_not_before)
        strat.validate_input(data, setting_rows)  # the V7 gate (default no-op), same as the single entrypoint
        try:  # lane D1: one member's sizing refusal is recorded; the other members carry on
            res: RunResult = strat.run(data, setting_rows)  # the full RunResult (8 stats + funnel_* + equity)
        except CELL_REFUSAL_ERRORS as exc:  # lane D1b: an undefined exit fill price is one cell's refusal too
            _emit_cell_refusals(emit, [_refusal_record(exc, 0, ticker, "{}")])
            _log(f"[sweep] single_combo: {ticker} refused ({refusal_reason(str(exc))}); the rest carry on")
            continue
        lead_in_bars = 0 if entries_not_before is None else int((ohlc.index < entries_not_before).sum())  # P2g
        out_cells.append(_run_result_cell(res, m, 0, "{}", _finite_or_none, lead_in_bars=lead_in_bars))  # the full sweep-shaped cell (variation_id 0, empty combo)
    # P-3: fail loud iff the window leaves NO member with usable data. `runnable` counts members with >=2
    # in-window bars (whether freshly run this call OR already committed on a resume) — so the guard keys off
    # the data, not on out_cells/committed state: a committed member is never empty (it produced a cell), so
    # runnable==0 can only mean every member is genuinely empty in the window. A fully-committed resume has
    # runnable>0 and out_cells empty -> a correct no-op (returns 0), never a false EmptyWindowedUniverse.
    runnable = len(universe) - len(empty_members)
    if runnable == 0:  # EVERY member is empty in the window -> fail loud (P-3), independent of resume state
        raise EmptyWindowedUniverse(
            f"the test window leaves fewer than 2 bars for every one of the {len(universe)} universe members "
            f"(window_start_ts={window_start_ts}, window_end_ts={window_end_ts}); widen the window or the universe"
        )
    if empty_members:  # surface the excluded members (report-not-drop, never a silent subset)
        _log(f"[sweep] single_combo excluded {len(empty_members)} member(s) with no in-window data: {empty_members}")
    # 1.2 (scale-defect 1): emit in BYTE-bounded chunks, never one line for the whole universe. At ~100+
    # members a single {"chunk": 0, "cells": out_cells} line exceeded the worker's 64 KB readline limit and
    # failed the run; _emit_cells_chunked splits it into lines each under the budget regardless of size.
    if out_cells:  # emit the resolvable cells as one or more atomic chunks (the worker POSTs each)
        _emit_cells_chunked(emit, out_cells)
    return len(out_cells)


def _process_inline_param(strat, universe: list[dict], grid: dict, params_decl_spec: list[dict],
                          window_start_ts, window_end_ts, settings: list[dict], committed: set, emit,
                          sizing_spec=None) -> int:
    """The per-(combo x ticker) inline-parameter path (M5 C): run each member's ``run()`` once per grid combo,
    injecting the combo via ``self.param``. ``sizing_spec`` (#376 ruling 4 round 2, E-R1-SF3): the same
    registry-resolved spec ``_process_single_combo`` binds, for the same reason (see that function).

    The generalization of ``_process_single_combo`` from one degenerate combo to the cartesian product of the
    grid. ``params_decl_spec`` is the ``[{name,type}]`` the backend AST-extracted from the strategy's
    ``self.param`` calls (an inline strategy declares no class ``sweep_params``, so the decls ride the spec). For
    each member (COLD-clipped to the window, AD-UU-3) and each combo: inject the combo (``_set_active_combo``),
    run the strategy, and build the SAME full sweep cell ``_run_result_cell`` builds at the combo's
    ``variation_id`` + canonical ``param_json`` (the SAME ids the wide path assigns via ``canonical_combo_order``,
    so the cube grain, variation rows, and read path are unchanged). Empty members are surfaced + excluded; an
    all-empty window fails loud (``EmptyWindowedUniverse``, P-3); cells emit in byte-bounded chunks (1.2). Returns
    the cell count. ``sweep_conditions`` are NOT applied here (the full product runs; conditions are a
    vectorized-path prune, AD-C4) — logged, never silently dropped."""
    import itertools  # function-scope: the cartesian product of the grid
    from strategies.base import SweepParam  # function-scope: reconstruct the decl (name + kind) from the spec
    from strategies.base.vbt_extraction import _finite_or_none  # the exact nullable-ratio coercion (lazy, vbt-side)

    params_decl = [SweepParam(d["name"], d["type"]) for d in params_decl_spec]  # {name,type} -> SweepParam(name, kind)
    param_names = tuple(p.name for p in params_decl)                            # the declared order (variation sort key)
    # the grid values coerced to each param's declared kind (the SAME coercion the wide path uses)
    grid_coerced = {p.name: [coerce_param_value(v, p.kind) for v in grid[p.name]] for p in params_decl}
    # the cartesian product in declared-param order -> combo dicts; canonical_combo_order -> the variation_id (the
    # SAME id scheme the wide path uses, so a re-dispatch / Track-C re-run lands the same ids)
    combos = [dict(zip(param_names, vals)) for vals in itertools.product(*(grid_coerced[n] for n in param_names))]
    ordered = canonical_combo_order(combos, param_names)
    variation_map = {canonical_param_json(c): vid for vid, c in enumerate(ordered)}  # combo-json -> variation_id

    if strat.sweep_conditions:  # AD-C4: the inline path runs the FULL product (conditions are a vectorized prune)
        _log(f"[sweep] inline: sweep_conditions {tuple(strat.sweep_conditions)} are NOT applied for an inline "
             f"grid; running the full product of {len(ordered)} combination(s)")

    setting_rows = [SettingRow(**row) for row in settings]  # the frozen run-level settings (run reads costs here)
    empty_members: list[str] = []  # members with <2 in-window bars (surfaced + excluded, P-3)
    cells_emitted = 0  # total cells emitted across all members
    chunk_index = 0    # threaded across members so the emitted chunk indices stay monotonic for the run
    for m in universe:
        raw_ohlc = _load_member_ohlc(m["dataset_path"])
        # Lane SW (5.8, DECISIONS #383 decision 2): the same lead-in-aware-or-fallback clip
        # _process_single_combo uses (see that function's own comment for the full reasoning).
        if sizing_spec is None:
            ohlc = _clip_frame_to_window(raw_ohlc, window_start_ts, window_end_ts)  # COLD slice
            entries_not_before = None
        else:
            ohlc, entries_not_before = clip_frame_with_lead_in(
                raw_ohlc, window_start_ts, window_end_ts, sizing_spec.estimator_window, m["symbol"])
        if traded_bar_count(ohlc.index, entries_not_before) < 2:  # no usable TRADED history -> surface + exclude (never zero-pad)
            empty_members.append(m["symbol"])
            continue
        ticker = m["symbol"]
        # LeadInStrategyInput (lane SW, 5.8): the one input type every caller now receives (see
        # _process_single_combo's own comment for why entries_not_before=None is a safe, identical default).
        data = LeadInStrategyInput(prices=ohlc, resolved_sizing=sizing_spec, entries_not_before=entries_not_before)  # E-R1-SF3: bound spec, one per member
        lead_in_bars = 0 if entries_not_before is None else int((ohlc.index < entries_not_before).sum())  # P2g: this member's lead-in
        member_cells: list[dict] = []  # this member's cells (combos), EMITTED per-member to bound peak memory
        for combo in ordered:  # one run() per combo, the combo injected via self.param
            param_json = canonical_param_json(combo)
            vid = variation_map[param_json]
            if (vid, ticker) in committed:  # resume: this (combo, ticker) cell is already committed
                continue
            strat._set_active_combo(combo)  # M5 C: inject the combo for this run() (self.param returns its values)
            strat.validate_input(data, setting_rows)  # the V7 gate; may key off a combo value (run after the inject)
            try:  # lane D1: one (combo, member) cell's sizing refusal is recorded; the rest carry on
                res: RunResult = strat.run(data, setting_rows)  # the full RunResult for this (combo, member)
            except CELL_REFUSAL_ERRORS as exc:  # lane D1b: an undefined exit fill price is one cell's refusal too
                _emit_cell_refusals(emit, [_refusal_record(exc, vid, ticker, param_json)])
                continue
            member_cells.append(_run_result_cell(res, m, vid, param_json, _finite_or_none, lead_in_bars=lead_in_bars))  # the full sweep cell
        strat._set_active_combo(None)  # clear after this member's combos (defensive; the next member re-sets per combo)
        if member_cells:  # emit THIS member's cells before the next member (per-member streaming: peak memory is
            # one member's combos, not combos x members; the worker also starts POSTing sooner). chunk_index threads
            # across members for monotonic indices; the same byte-bounded chunker (1.2) the other paths use.
            chunk_index = _emit_cells_chunked(emit, member_cells, chunk_index)
            cells_emitted += len(member_cells)
    runnable = len(universe) - len(empty_members)
    if runnable == 0:  # EVERY member is empty in the window -> fail loud (P-3), like the zero-param path
        raise EmptyWindowedUniverse(
            f"the test window leaves fewer than 2 bars for every one of the {len(universe)} universe members "
            f"(window_start_ts={window_start_ts}, window_end_ts={window_end_ts}); widen the window or the universe"
        )
    if empty_members:  # surface the excluded members (report-not-drop, never a silent subset)
        _log(f"[sweep] inline excluded {len(empty_members)} member(s) with no in-window data: {empty_members}")
    return cells_emitted


def _slice_frame_by_ts(frame: pd.DataFrame, start_ts: int, end_ts: int) -> pd.DataFrame:
    """A ticker's own price frame, label-sliced to [start_ts, end_ts] INCLUSIVE on both ends.

    Reuses _window_bound for the same tz-matching _clip_frame_to_window already relies on. This is
    deliberately inclusive on both sides (unlike _clip_frame_to_window's own use, which is also
    inclusive): compute_slice_bounds's own docstring says a slice's *_ts bounds are themselves real,
    included bar timestamps, so slicing to them with pandas' own inclusive .loc[] is the correct
    read, not an off-by-one.
    """
    lo = _window_bound(start_ts, frame.index)
    hi = _window_bound(end_ts, frame.index)
    return frame.loc[lo:hi]


def _slice_with_lead_in(raw_ohlc: pd.DataFrame, start_ts: int, end_ts: int, sizing_spec, member: str,
                        *, reach_before_ts: "int | None" = None):
    """Lane WU (5.8c, `DECISIONS.md` #383 decision 2, criterion 1): the walk-forward sibling of
    _process_vectorized_partition's own has_sizing branch above -- the SAME choice, applied to a
    single member's slice frame instead of a wide partition. When a sizing spec was resolved (the
    ordinary case, `sizing_spec=sizing_method.spec` at every real call site), the slice starts
    exactly `sizing_spec.estimator_window` bars before `start_ts` (all or nothing:
    InsufficientLeadInError, never a shorter lead-in) so the estimator can warm up on real
    pre-slice bars that `raw_ohlc` already holds (this member's own FULL price history, unread
    before this lane); when no spec was resolved (a bare direct caller predating this lane, the
    `sizing_spec=None` default), the old plain `_slice_frame_by_ts` clip and `entries_not_before=
    None` are unchanged, byte-identical to every walk-forward run before this lane.

    Lane LB (5.8e, `DECISIONS.md` #406 ruling 2), criterion 3: `reach_before_ts` is the GRANTED
    window's own end (`window_end_ts`, `_process_walkforward`'s own parameter), never this call's
    own `end_ts` (a slice's in-sample or out-of-sample bound) -- the reach is bounded to the whole
    granted draw, not to the one slice that happens to reach for it. Defaults to `None` (no reach
    attempted) for a bare direct caller predating this lane.

    Returns `(frame, entries_not_before, fill_reach)`: the first two are the exact pair
    `clip_frame_with_lead_in` itself returns, so every one of this function's three call sites below
    can bind them straight onto a `LeadInStrategyInput` the same way `_process_single_combo`/
    `_process_inline_param` already do; `fill_reach` is `sizing.fill_reach_after`'s own return, read
    off the SAME unclipped `raw_ohlc` this function already has in hand, or `None` when `sizing_spec`
    was never resolved (there is no `estimator_window`-clipped frame to reach past in that case).
    """
    if sizing_spec is not None:
        frame, entries_not_before = clip_frame_with_lead_in(raw_ohlc, start_ts, end_ts, sizing_spec.estimator_window, member)
        fill_reach = fill_reach_after(raw_ohlc, frame, reach_before_ts)
        return frame, entries_not_before, fill_reach
    return _slice_frame_by_ts(raw_ohlc, start_ts, end_ts), None, None


def _max_drawdown_from_equity(equity: "pd.Series") -> float:
    """The worst peak-to-trough fractional decline of an equity curve (fold item 2c, round 1).

    Plain pandas, not vbt: vbt's own `pf.max_drawdown` needs a live `Portfolio` object, and a
    STITCHED, cross-slice walk-forward curve has no single such object to read it off (each slice
    runs its own separate `from_signals` call). This is the same plain-pandas precedent
    `_run_result_cell` already uses for the equity-derived skew/kurtosis moments, applied to the
    running-peak drawdown definition instead: the running maximum (`cummax`), the fractional drop
    from it at every bar, and the worst (most negative) such drop, reported as a positive
    magnitude (`abs`), matching `pf.max_drawdown`'s own sign convention."""
    running_peak = equity.cummax()
    drawdown = (equity - running_peak) / running_peak
    return float(abs(drawdown.min()))


def _process_walkforward(strat, universe: list[dict], grid: dict, params_decl_spec: list[dict],
                         window_start_ts, window_end_ts, settings: list[dict], wf_spec: dict, emit,
                         run_id: int, sizing_spec=None, init_cash: float = 10_000.0) -> dict:
    """AC-16/AC-18/AC-19/AC-20 (lane WF, 6.2b): the walk-forward stage's own single-pass compute.

    ONE draw was already granted for the whole window (stage_runner.py's own AC-18 guarantee); this
    function never requests another and never touches the database. For each universe member (in
    practice exactly one, per this lane's own probes, but written generically): derive purge/embargo
    ONCE from that member's own slice-0 (walkforward.py's own documented resolution of the
    purge-depends-on-trades chicken-and-egg problem), compute the real slice bounds, then for every
    slice run the FULL grid over the in-sample sub-frame, pick the combo with the greatest in-sample
    total_return (this lane's own objective choice, disclosed here and in the build report: no
    "optimise stage" objective exists anywhere in this codebase to reuse, so the most natural
    already-recorded profit statistic, total_return, is what this lane picks), and run that ONE
    chosen combo over the out-of-sample sub-frame. Each slice is emitted immediately as a
    {"walkforward_slice": {...}} stdout line (the worker relays it, never re-computes it).

    Fold item 1 (round 1, BLOCKER LC1): a slice whose in-sample OR out-of-sample profit is absent
    (a non-finite total_return) contributes NEITHER its profit NOR its bar count to the run-level
    annualised figures -- it is skipped whole, never zero-substituted (a zero is a real number that
    would quietly drag the annualised ratio toward zero). `slices_used` counts the slices that DID
    contribute; `slices_with_absent_profit` counts the ones skipped this way; when `slices_used` is
    zero the run-level in-sample/out-of-sample profits are None, an honest absence.

    Fold item 2 (round 1, BLOCKER LC2 / the lead's read): the out-of-sample equity is CHAINED, not a
    bare concatenation of independent per-slice curves -- each slice's own curve (which restarts at
    `init_cash`) is rescaled by the running end value over `init_cash` before being appended, so the
    stitched curve compounds across slices and is continuous at every join. The chained curve is
    written directly to this run's own equity.parquet (the single-run artifact shape, `_write_equity`)
    rather than carried in the JSON summary, so the results-read route can serve it exactly like any
    other run's equity; `total_return`, `max_drawdown`, `num_trades`, `total_fees_paid` and
    `total_slippage_cost` are computed from that SAME chained curve and the summed out-of-sample
    per-slice figures, replacing the old first-insert hardcoded 0/0.0/0.

    Returns the run-level walkforward summary dict the final sweep-done line carries: the annualised
    in-sample/out-of-sample profit (AC-17, Pardo PDF page 293: "an annualized walk-forward profit and
    loss"), wfa_boundary_buffer_bars (purge_bars + embargo_bars, the boundary buffer
    check_2_walk_forward's own guard G1 needs), and the five general stats above. Total slice count
    rides on the caller's own sweep-done "total_cells" field (a walk-forward run's accounting unit is
    the SLICE, never a per-combo cell: no research_run_cells row is ever written for this stage).
    """
    import itertools  # function-scope, matching _process_inline_param's own convention
    from strategies.base import SweepParam  # function-scope: rebuild the decl from the spec
    from strategies.base.vbt_extraction import _finite_or_none  # the exact nullable-ratio coercion (lazy, vbt-side)

    params_decl = [SweepParam(d["name"], d["type"]) for d in params_decl_spec]
    param_names = tuple(p.name for p in params_decl)
    grid_coerced = {p.name: [coerce_param_value(v, p.kind) for v in grid[p.name]] for p in params_decl}
    combos = [dict(zip(param_names, vals)) for vals in itertools.product(*(grid_coerced[n] for n in param_names))]

    # Fold item 3 (round 1, BLOCKER-class U2/claims): the strategy's OWN declared lookback, over
    # EVERY combo in the grid (one embargo for the whole run, applied to every slice --
    # compute_slice_bounds's own contract already assumes a single purge/embargo pair). Computed
    # here, before any member's data is loaded and before even the provisional slice-0 run below, so
    # a strategy that has not implemented lookback_bars refuses BY NAME, before any slice runs.
    try:
        max_lookback_bars = max(strat.lookback_bars(combo) for combo in combos)
    except NotImplementedError as exc:
        raise ValueError(f"walkforward-lookback-undeclared: {exc}") from exc

    # Lane DF2 (`6.1e`, AC-2): Udf, the same per-combo shape as max_lookback_bars just above, but
    # this one never refuses the stage on an undeclared strategy: no walk-forward schema exists
    # yet to store this figure (AC-2's own documented gap, named again in this lane's report), so
    # an undeclared Udf is caught and logged, not raised.
    try:
        max_used_degrees_of_freedom = max(strat.used_degrees_of_freedom(combo) for combo in combos)
    except NotImplementedError:
        max_used_degrees_of_freedom = None  # undeclared: recorded nowhere yet, never refused
    _log(f"[walkforward] max_used_degrees_of_freedom={max_used_degrees_of_freedom}")

    setting_rows = [SettingRow(**row) for row in settings]

    in_sample_bars = wf_spec["in_sample_bars"]
    out_of_sample_bars = wf_spec["out_of_sample_bars"]
    anchoring = wf_spec["anchoring"]

    total_slices = 0  # the run's own accounting unit (never a per-combo cell count)
    slices_used = 0  # fold item 1: slices whose profit was finite on BOTH legs and so fed the run-level figures
    slices_with_absent_profit = 0  # fold item 1: slices skipped whole because either leg's profit was absent
    total_is_bars_raw = 0  # summed real in-sample bar counts, only over slices_used (AC-17's own annualising denominator)
    total_oos_bars_raw = 0
    total_is_profit_raw = 0.0  # summed raw (un-annualised) slice profits, only over slices_used
    total_oos_profit_raw = 0.0
    running_end_value = init_cash  # fold item 2a: the chained curve's own running end value, across every slice so far
    stitched_equity_parts: list["pd.Series"] = []  # fold item 2a: each slice's own OOS equity, rescaled onto the chain
    total_oos_trade_count = 0  # fold item 2c: summed OOS trade count, every slice that ran (independent of item 1's skip)
    total_oos_fees_raw = 0.0  # fold item 2c: summed OOS fees paid, every slice that ran
    total_oos_slippage_raw = 0.0  # fold item 2c: summed OOS slippage cost, every slice that ran
    purge_bars_used = 0  # the last member's own derived values; wfa_boundary_buffer_bars is one run-level figure
    embargo_bars_used = 0
    bars_per_year = None  # resolved once, from the first member with a usable price index (AC-17's shared factor)
    members_with_empty_split = 0  # round-5 fold (n7-WF-4a1c): counted rather than silently skipped, so the
    # eventual walkforward-no-out-of-sample-leg refusal (if every member lands here) can name WHY

    for m in universe:  # in practice one member (this lane's own probes); written generically
        raw_ohlc = _load_member_ohlc(m["dataset_path"])  # this ticker's own full price history
        bar_dates = [(str(ts), int(ts.timestamp())) for ts in raw_ohlc.index]  # (label, epoch_ts), ascending (the file's own order)

        if bars_per_year is None and len(raw_ohlc.index) >= 2:
            # AC-17's own shared bars-per-year figure, derived once from real price data (never a
            # literal): resolve_annualization's own "factor" field IS "the observed number of bars
            # per year", exactly what a run-level annualised profit needs to scale a raw per-bar rate.
            bars_per_year = resolve_annualization(raw_ohlc.index)["factor"]

        # Slice 0's own UNPULLED bounds (purge=embargo=0): a provisional split used only to learn
        # this member's own realized trade holds, per walkforward.py's own documented resolution of
        # the purge-depends-on-trades circularity. Never emitted, never counted as a real slice.
        provisional = engine_wf.compute_slice_bounds(
            bar_dates, window_start_ts, window_end_ts, in_sample_bars, out_of_sample_bars, anchoring, 0, 0,
        )
        if not provisional:  # AC-36 already refused this at dispatch time; an empty split here is a no-op, never a crash
            # Round-5 fold (n7-WF-4a1c): wf13's own state (a granted window entirely after this
            # member's last bar) lands here. Counted rather than a bare `continue`, so a run where
            # EVERY member's provisional split is empty can say so by name in its own refusal below,
            # instead of silently finishing with an honest-looking all-None summary and rc 0.
            members_with_empty_split += 1
            continue
        # Lane WU (5.8c): lead-in-aware, matching every other frame this function builds. The
        # provisional split's own in-sample start is slice 0's own start (the measured case,
        # r3's own shape), so this is where InsufficientLeadInError fires first for a member
        # whose store history runs out before the lead-in.
        provisional_is_frame, provisional_entries_not_before, provisional_fill_reach = _slice_with_lead_in(
            raw_ohlc, provisional[0]["in_sample_start_ts"], provisional[0]["in_sample_end_ts"],
            sizing_spec, m["symbol"], reach_before_ts=window_end_ts,
        )
        trade_holds: list[int] = []  # every realized trade's own bar-duration, across every combo's provisional run
        for combo in combos:
            strat._set_active_combo(combo)
            data = LeadInStrategyInput(
                prices=provisional_is_frame, resolved_sizing=sizing_spec,
                entries_not_before=provisional_entries_not_before, fill_reach=provisional_fill_reach,
            )
            strat.validate_input(data, setting_rows)
            res = strat.run(data, setting_rows)
            trades = res.trades
            if trades is not None and len(trades):
                # entry_index/exit_index carry the frame's own DatetimeIndex LABELS (vbt's readable
                # form), not positional integers, so a plain subtraction gives a Timedelta, not a bar
                # count; get_indexer maps each label back to its integer bar position in the same
                # frame those trades were run over, and the difference of positions is the real,
                # gap-robust bar-count hold this function needs.
                entry_pos = provisional_is_frame.index.get_indexer(trades["entry_index"])
                exit_pos = provisional_is_frame.index.get_indexer(trades["exit_index"])
                trade_holds.extend((exit_pos - entry_pos).tolist())
        strat._set_active_combo(None)

        # AC-16: never chosen. A fixed, class-declared time limit wins when the strategy carries one
        # (no such attribute exists on the Strategy base today, so this reads None for every strategy
        # in this build and the function's own trade-holds branch fires, exactly as wf04 proves); the
        # embargo is always the strategy's OWN declared lookback (fold item 3, round 1): max_lookback_bars,
        # derived once above from every combo's own lookback_bars(combo), never the sizing estimator's
        # window (a different number the round-1 build had wrongly borrowed for this role).
        declared_time_limit_bars = getattr(strat, "time_limit_bars", None)
        lookback_bars = max_lookback_bars  # the strategy's own declared lookback, computed once before this loop
        purge_bars, embargo_bars = engine_wf.derive_purge_and_embargo(declared_time_limit_bars, trade_holds, lookback_bars)
        purge_bars_used, embargo_bars_used = purge_bars, embargo_bars

        # The real slices: purge/embargo applied identically to every one (compute_slice_bounds's
        # own contract), never re-derived per slice.
        slices = engine_wf.compute_slice_bounds(
            bar_dates, window_start_ts, window_end_ts, in_sample_bars, out_of_sample_bars, anchoring,
            purge_bars, embargo_bars,
        )
        # Round-3 fold (n7-WF-4a1c), the lead's read: stage_runner.py's own pre-draw floor
        # (in_sample_bars + 2*out_of_sample_bars, AT ZERO BUFFER) cannot know this member's real
        # purge/embargo before the grid has even been read, so it is deliberately left unchanged
        # here -- it is the earliest check that can run, not the only one AC-36 needs. Once the
        # REAL buffer is known (purge_bars, embargo_bars, both derived above from the actual
        # trade holds and the grid's declared lookback), a window that cleared the pre-draw floor
        # can still be too small to hold two full slices once that buffer is subtracted out: with
        # ma_cross the embargo alone can run to tens of bars, so this state is reachable on a real
        # window, not a corner case. AC-36's own words ("never collapses to a single slice") are
        # enforced here, the last point before any slice actually runs.
        if len(slices) < 2:
            bars_held = sum(1 for _label, ts in bar_dates if window_start_ts <= ts < window_end_ts)  # same half-open count stage_runner.py's own floor uses
            bars_needed_with_buffer = in_sample_bars + 2 * out_of_sample_bars + embargo_bars  # the REAL floor, only knowable after purge/embargo are derived
            raise ValueError(
                f"walkforward-window-too-small-after-buffer: member {m['symbol']!r} holds "
                f"{bars_held} bars in the granted window, short of the {bars_needed_with_buffer}-bar "
                f"floor (in_sample_bars={in_sample_bars} + 2*out_of_sample_bars={out_of_sample_bars} "
                f"+ embargo_bars={embargo_bars}, with purge_bars={purge_bars} also applied per slice) "
                f"this scheme needs once the real buffer is known; only {len(slices)} slice(s) fit"
            )
        for s in slices:
            # Lane WU (5.8c): the real in-sample slice, lead-in-aware. s["in_sample_start_ts"] is
            # slice 0's own window start for the first slice (the measured case) and each later
            # slice's own rolling/anchored start otherwise; raw_ohlc already carries the bars this
            # needs (this member's own full price history).
            is_frame, is_entries_not_before, is_fill_reach = _slice_with_lead_in(
                raw_ohlc, s["in_sample_start_ts"], s["in_sample_end_ts"], sizing_spec, m["symbol"],
                reach_before_ts=window_end_ts,  # lane LB (5.8e): the GRANTED window's own end, never s["in_sample_end_ts"]
            )
            best_combo = None  # the in-sample winner for this slice (AC-19: "picks ... the objective")
            best_return = None
            best_is_res = None
            for combo in combos:  # the full grid, run over the in-sample sub-frame only
                strat._set_active_combo(combo)
                data = LeadInStrategyInput(
                    prices=is_frame, resolved_sizing=sizing_spec, entries_not_before=is_entries_not_before,
                    fill_reach=is_fill_reach,
                )
                strat.validate_input(data, setting_rows)
                res = strat.run(data, setting_rows)
                candidate_return = res.stats.get("total_return")
                if candidate_return is not None and (best_return is None or candidate_return > best_return):
                    best_return, best_combo, best_is_res = candidate_return, combo, res
            strat._set_active_combo(None)
            if best_combo is None:  # no combo produced a usable in-sample result: skip this slice honestly, never fabricate one
                continue

            # Lane WU (5.8c): the real out-of-sample slice, lead-in-aware the same way the
            # in-sample one just above is. The best combo (picked purely from the in-sample leg)
            # runs unchanged; only how its own frame is built has a lead-in now.
            oos_frame, oos_entries_not_before, oos_fill_reach = _slice_with_lead_in(
                raw_ohlc, s["out_of_sample_start_ts"], s["out_of_sample_end_ts"], sizing_spec, m["symbol"],
                reach_before_ts=window_end_ts,  # lane LB (5.8e): the GRANTED window's own end, never this slice's own end
            )
            strat._set_active_combo(best_combo)
            oos_data = LeadInStrategyInput(
                prices=oos_frame, resolved_sizing=sizing_spec, entries_not_before=oos_entries_not_before,
                fill_reach=oos_fill_reach,
            )
            strat.validate_input(oos_data, setting_rows)
            oos_res = strat.run(oos_data, setting_rows)
            strat._set_active_combo(None)

            is_profit_raw = _finite_or_none(best_is_res.stats.get("total_return"))
            oos_profit_raw = _finite_or_none(oos_res.stats.get("total_return"))
            # Lane WU (5.8c): traded_bar_count, never a bare len(...index) -- once either frame can
            # carry lead-in bars, its own raw length would over-count AC-17's annualising
            # denominator by exactly estimator_window bars per slice (sizing.py's own established
            # rule, already applied everywhere else a lead-in-extended frame is measured).
            is_bar_count = traded_bar_count(is_frame.index, is_entries_not_before)
            oos_bar_count = traded_bar_count(oos_frame.index, oos_entries_not_before)

            total_slices += 1
            # Fold item 1 (round 1, BLOCKER LC1): an absent profit on EITHER leg skips the WHOLE
            # slice's contribution to the run-level figures (never a zero-substitution, which is a
            # real number that would quietly drag the annualised ratio down). The two counts below
            # are what let a reader tell "every slice ran but had no usable profit" apart from "no
            # slice ran at all" -- the two different NULL causes LC1 named.
            if is_profit_raw is None or oos_profit_raw is None:
                slices_with_absent_profit += 1
            else:
                slices_used += 1
                total_is_bars_raw += is_bar_count
                total_oos_bars_raw += oos_bar_count
                total_is_profit_raw += is_profit_raw
                total_oos_profit_raw += oos_profit_raw

            # Lane WU (5.8c): oos_res.equity carries ONE VALUE PER BAR of oos_frame, lead-in bars
            # included (vbt's from_signals runs over the whole clipped frame; the reported research_
            # run_funnel.bars_total convention already established by sizing.py's own traded_bar_count
            # docstring is that the WHOLE clipped frame is "bars_total" -- lead-in bars are counted
            # there, on purpose). The STITCHED OOS EQUITY this function writes is a different thing:
            # a chain of out-of-sample TEST records, and a lead-in prefix bled in from BEFORE this
            # slice's own out-of-sample window started is not part of that test's own observed
            # performance (no entry is ever masked-open during it, so it is flat at init_cash and
            # carries zero information) -- keeping it would silently lengthen the written equity.parquet
            # past the sum of the slices' own out-of-sample bar counts, exactly the fold-2a length
            # invariant research_lab/probes/walk_forward/wf06a already asserts. Trimmed here with the
            # SAME boundary traded_bar_count uses (label-based .loc, left-inclusive), never a second,
            # hand-rolled position count.
            oos_traded_equity = (
                oos_res.equity if oos_entries_not_before is None else oos_res.equity.loc[oos_entries_not_before:]
            )

            # Fold item 2a (round 1, BLOCKER LC2): chain this slice's own out-of-sample equity
            # (which restarts at init_cash) onto the running curve by rescaling it to the value the
            # chain had already reached, so the stitched curve compounds and is continuous at the
            # join -- independent of item 1's profit-absence skip above, since the equity curve
            # itself is a real observed series regardless of whether total_return read as finite.
            chain_scale = running_end_value / init_cash
            chained_slice_equity = oos_traded_equity * chain_scale
            stitched_equity_parts.append(chained_slice_equity)
            running_end_value = float(chained_slice_equity.iloc[-1])

            # Fold item 2c (round 1, BLOCKER LC2 / the lead's read): the OOS trade/fee/slippage
            # figures, summed over every slice that ran (the same unconditional accounting the
            # per-slice "trade_count" emit below already uses), feeding the run's own general stats.
            total_oos_trade_count += int(oos_res.stats.get("num_trades") or 0)
            total_oos_fees_raw += float(oos_res.stats.get("total_fees_paid") or 0.0)
            total_oos_slippage_raw += float(oos_res.stats.get("total_slippage_cost") or 0.0)

            # Lane LB (5.8e): this slice's own reach and masked-at-end count, read off the two legs'
            # RunResult.stats (long_only_signal_result's own new keys). Guarantee (i) is unaffected by
            # either being read here: both are ADDITIVE stats keys that never influenced `pf`, `entries`
            # or any of the figures above, which are computed identically whether or not a reach fired.
            # The OOS leg is privileged for `fill_reach_ts` (the same convention "trade_count" above
            # already uses: this slice's own record leans on its out-of-sample leg), falling back to the
            # in-sample leg's own reach only when the OOS leg carried none; both legs' masked counts are
            # summed, since each is an independent, real fact about this one slice.
            slice_fill_reach_ts = oos_res.stats.get("fill_reach_ts")
            if slice_fill_reach_ts is None:
                slice_fill_reach_ts = best_is_res.stats.get("fill_reach_ts")
            slice_entries_masked_at_end = (
                int(best_is_res.stats.get("entries_masked_at_end") or 0)
                + int(oos_res.stats.get("entries_masked_at_end") or 0)
            )
            emit({"walkforward_slice": {
                "slice_index": s["slice_index"],
                "in_sample_start_ts": s["in_sample_start_ts"],
                "in_sample_end_ts": s["in_sample_end_ts"],
                "out_of_sample_start_ts": s["out_of_sample_start_ts"],
                "out_of_sample_end_ts": s["out_of_sample_end_ts"],
                # sha256 of the canonical combo JSON, the same rule researchdb._param_config_hash uses.
                "param_config_hash": hashlib.sha256(canonical_param_json(best_combo).encode("utf-8")).hexdigest(),
                "in_sample_profit": is_profit_raw,
                "out_of_sample_profit": oos_profit_raw,
                "trade_count": int(oos_res.stats.get("num_trades") or 0),
                "fill_reach_ts": slice_fill_reach_ts,
                "entries_masked_at_end": slice_entries_masked_at_end,
            }})

    # AC-17: the run-level figures are ANNUALISED (never the raw sum), scaled by ONE shared,
    # data-derived bars-per-year factor so the run_oos/run_is ratio equals the ratio of the raw
    # per-bar rates (the factor cancels out of a ratio it multiplies both sides of identically).
    if bars_per_year is not None and total_is_bars_raw > 0 and total_oos_bars_raw > 0:
        run_is_profit = (total_is_profit_raw / total_is_bars_raw) * bars_per_year
        run_oos_profit = (total_oos_profit_raw / total_oos_bars_raw) * bars_per_year
    else:  # no slice contributed a finite profit (an empty universe, a degenerate index, or every
        # slice's profit was absent, item 1's own slices_used == 0 case): an honest absence, never a
        # fabricated zero
        run_is_profit = None
        run_oos_profit = None

    # Fold item 2 (round 1, BLOCKER LC2 / the lead's read): the chained curve, and the five general
    # stats derived from it, exist only when at least one slice produced a real out-of-sample leg.
    #
    # Round-5 fold (n7-WF-4a1c): an EMPTY stitched_equity_parts here used to fall through to the
    # else branch below and return an honest-LOOKING but all-None summary with exit code 0 --
    # research_run_stats.total_return and max_drawdown are NOT NULL columns, so that summary would
    # only surface as a raw sqlite3.IntegrityError once it reached record_walkforward_run_stats,
    # after the worker had already relayed a clean sweep_done line with total_cells=0 and rc=0
    # (wf13's own reachable state: a granted window entirely after a member's last bar, so the
    # provisional split is empty for every member and no slice ever runs). Refuse here instead, by
    # name, before the summary is even assembled, naming the member count, how many of them hit an
    # empty provisional split, the total slices actually computed and how many of those were used.
    if not stitched_equity_parts:
        raise ValueError(
            f"walkforward-no-out-of-sample-leg: {len(universe)} member(s) in this run's universe, "
            f"{members_with_empty_split} of them with an empty provisional split (the granted "
            f"window does not overlap that member's own bars); {total_slices} slice(s) were "
            f"computed and {slices_used} used, but no member produced any out-of-sample leg at all"
        )
    stitched_equity_series = pd.concat(stitched_equity_parts)  # the FULL chained curve, in slice order
    run_total_return = _finite_or_none((running_end_value / init_cash) - 1.0)  # end over init_cash, minus one
    run_max_drawdown = _finite_or_none(_max_drawdown_from_equity(stitched_equity_series))
    # Round-6 fold (n7-WF-4a1c), code-quality pass 2 findings CQ3 (entrypoint half) and CQ4: the two
    # fee/slippage sums are computed here too, BEFORE the finiteness check below, so the check can
    # cover all four NOT NULL figures research_run_stats needs, not just the first two. They used to
    # be recomputed inline inside the return dict, after the write already happened, which is why a
    # non-finite one of these could reach the database uncaught.
    run_total_fees_paid = _finite_or_none(total_oos_fees_raw)
    run_total_slippage_cost = _finite_or_none(total_oos_slippage_raw)

    # Round-4 fold (n7-WF-4a1c), code-quality pass 1 finding B1(a), WIDENED round 6 (CQ3): all four of
    # total_return, max_drawdown, total_fees_paid and total_slippage_cost are NOT NULL columns in
    # research_run_stats even once stitched_equity_parts is non-empty -- any of the four sums the
    # chained curve derives can still resolve to a non-finite value (e.g. init_cash of zero, or an
    # overflowed fee accumulator). Refuse by name, listing exactly which figure(s) are non-finite,
    # the same way as the empty-parts case above, rather than let a NULL reach
    # record_walkforward_run_stats and raise a raw sqlite3.IntegrityError after the slices have
    # already been relayed.
    #
    # Round-6 fold (n7-WF-4a1c), CQ4: this check now runs BEFORE equity.parquet is written (the
    # mkdir/_write_equity/equity_path block moved below it). A refused run used to still leave an
    # orphan equity.parquet on disk in the mounted run directory, a resource leak with no matching
    # database row to ever clean it up; checked-then-write, never write-then-check, closes that.
    non_finite_figures = [
        name
        for name, value in (
            ("total_return", run_total_return),
            ("max_drawdown", run_max_drawdown),
            ("total_fees_paid", run_total_fees_paid),
            ("total_slippage_cost", run_total_slippage_cost),
        )
        if value is None
    ]
    if non_finite_figures:
        raise ValueError(
            f"walkforward-no-out-of-sample-leg: {total_slices} slice(s) ran, {slices_used} used, "
            "but the chained out-of-sample result resolved to a non-finite "
            f"{', '.join(non_finite_figures)}; this run has no usable out-of-sample leg to report"
        )

    # Item 2b (round 2 fold, n7-WF-4a1c): write the chained curve straight to this run's own
    # equity.parquet (the single-run artifact shape) rather than carrying the raw values in the
    # JSON summary. The directory is created here, defensively, rather than assumed pre-existing:
    # a real docker dispatch DOES get it pre-created by the worker's own prepare_run_dir before
    # the :rw mount (run_sweep.py, walkforward dispatches only), but a caller that runs this
    # entrypoint's main() directly with no docker/mount step at all (this lane's own wf06a/wf06b
    # probes) never creates it, so writing without first creating it raised OSError: "Cannot save
    # file into a non-existent directory". mkdir(exist_ok=True) is a no-op on the real, pre-created
    # mount and a real fix on the direct-call path, so both shapes now write correctly.
    # Round-6 fold (n7-WF-4a1c), CQ4: reached only on the success path now, after every NOT NULL
    # figure above is confirmed finite, so a refused run never creates this file at all.
    equity_out_dir = RUNS_ROOT / str(run_id)
    equity_out_dir.mkdir(parents=True, exist_ok=True)
    _write_equity(stitched_equity_series, equity_out_dir)
    equity_path = str(equity_out_dir / "equity.parquet")  # the same string research_runs.equity_path stores

    return {
        "in_sample_profit": _finite_or_none(run_is_profit) if run_is_profit is not None else None,
        "out_of_sample_profit": _finite_or_none(run_oos_profit) if run_oos_profit is not None else None,
        "wfa_boundary_buffer_bars": purge_bars_used + embargo_bars_used,  # guard G1's own boundary buffer
        "slice_count": total_slices,
        "slices_used": slices_used,  # fold item 1: relayed on the POST body, never a new DB column
        "slices_with_absent_profit": slices_with_absent_profit,  # fold item 1
        "total_return": run_total_return,  # fold item 2c
        "max_drawdown": run_max_drawdown,  # fold item 2c
        "num_trades": total_oos_trade_count,  # fold item 2c: an honest zero, not None (NOT NULL column)
        "total_fees_paid": run_total_fees_paid,  # fold item 2c, computed above (round 6: CQ3/CQ4)
        "total_slippage_cost": run_total_slippage_cost,  # fold item 2c, computed above (round 6: CQ3/CQ4)
        "equity_path": equity_path,  # round 2 fold item 1: the container-relative path actually written, or None
    }


def _declared_params_unread_by_run(strat, params_decl_spec: list[dict]) -> list[str]:
    """Return every DECLARED sweep-param name missing from a self.param(...) call inside strat's
    own run() SOURCE TEXT (lane WP, `6.5`, finding 2.3's own guard half,
    docs/plans/night/2026-09-19/PLAN.md section 6 item 2).

    A class that declares sweep_params for the VECTORIZED path (a class attribute plus a
    sweep_signals override) but never reads the same names via self.param leaves the per-combo
    walk-forward engine's own _set_active_combo call INERT: run() keeps reading whatever module
    constant or literal it always read, so every combo in the grid would silently produce the
    IDENTICAL backtest, the in-sample step would "pick" among identical numbers, and the
    out-of-sample leg would run whatever it picked. This is the danger the derivation alone would
    reintroduce; this function is what the caller checks before trusting a derived declaration.

    A textual scan of run()'s own source (inspect.getsource), the same self.param read the
    backend performs for the inline dispatch mode (dashboard/backend/researchdb.py's own
    _inline_params_from_tree), duplicated here in a narrower, textual form rather than shared,
    because this file cannot import dashboard.backend (the engine/backend boundary, #227/AC-10:
    research_lab/probes/walk_forward/wf10_import_boundary_regression_guard.probe.py guards this
    file against exactly that import). Scoped to run() alone, not the whole class, because run()
    is the only method the walk-forward engine actually calls per combo (_process_walkforward
    calls strat.run(), never a helper method directly). A name is considered read when the
    literal self.param("name" or self.param('name' substring appears anywhere in that source --
    enough for a yes/no per declared name, not a full parse (the backend's own AST reader already
    owns the strict, exact form; this file only needs to refuse a name that is plainly absent).
    """
    import inspect  # function-scope: needed only for this one guard, matching this file's own
    # established convention of importing a narrowly-scoped stdlib helper inside the function that
    # uses it (see _process_inline_param's own "import itertools" and "from strategies.base import
    # SweepParam", both function-scope for the identical reason).
    run_source = inspect.getsource(type(strat).run)  # the CLASS's own run method, never the bound instance
    unread = []  # declared names not found as a self.param(...) call in run()'s source, in declared order
    for decl in params_decl_spec:
        name = decl["name"]
        if f'self.param("{name}"' not in run_source and f"self.param('{name}'" not in run_source:
            unread.append(name)
    return unread


def main() -> int:
    """Run the sweep, emit per-chunk results + a final sweep-done line, return a process exit code."""
    try:
        spec = json.loads(RUN_SPEC_PATH.read_text(encoding="utf-8"))  # the sweep job payload (§3)
        run_id = spec["run_id"]
        universe = spec["universe"]  # list of members (dataset_path, symbol, asset_class, fees, slippage, dataset_sha256)
        grid = spec["param_grid"]  # {param_name: [values]} (+ optional conditions the strategy applies)
        backtest_mode = spec["backtest_mode"]  # 'independent' | 'shared_capital'
        size_cap_cells = int(spec["size_cap_cells"])  # the one-pass cell budget (R-CEIL)
        init_cash = float(spec.get("init_cash", 10_000.0))  # starting cash (the ma_cross reference default)
        committed = {(int(v), t) for v, t in spec.get("committed_cells", [])}  # resume skip-set (variation_id, ticker)
        single_combo = bool(spec.get("single_combo", 0))  # B4: the zero-param single-run path (B3 sets the flag)
        window_start_ts = spec.get("window_start_ts")  # B4 (AD-UU-3): the COLD test-window epochs (UTC-day); None = open
        window_end_ts = spec.get("window_end_ts")
        settings = spec.get("settings", [])  # B4: the frozen run-level settings (the zero-param run reads its params + neutral costs)
        # M5 C: the engine's top-level fork. Back-compat default: an in-flight pre-C payload has no param_mode,
        # so derive it from single_combo (1 -> 'zero', 0 -> 'vectorized') = the exact pre-C behaviour (no stranded job).
        param_mode = spec.get("param_mode") or ("zero" if single_combo else "vectorized")
        params_decl_spec = spec.get("params_decl")  # M5 C: [{name,type}] for the inline mode (None otherwise)
        # Lane C5 (set 16c, #427, plan amendment 4): check 5's own frozen cscv_split_count (S),
        # relayed by run_sweep.py from the one worker route that can read judgedb (this container
        # has no database, research_lab/CLAUDE.md's own "How a job runs"). None when the worker's
        # own read failed (amendment 4: "the sweep still completes and writes no artifact"): every
        # block below then skips the whole additive computation, never a guessed S.
        cscv_split_count = spec.get("cscv_split_count")

        # Fail-loud payload validation (§3): no silent default.
        if not universe:
            raise ValueError("sweep payload has an empty universe")
        # B4 gate relaxation: an empty grid is valid ONLY for the single-combo (zero-param) path; the explicit
        # flag (not an empty grid) is what relaxes the gate, so a stray empty grid without the flag still fails.
        if not grid and not single_combo:
            raise ValueError("sweep payload has an empty param_grid")
        if backtest_mode == "shared_capital":  # the M7-paired unit, not built in 1B (§5.4) — never silently independent
            raise ValueError("shared-capital sweeps are the M7-paired unit, not built in 1B (independent mode only)")
        if backtest_mode != "independent":
            raise ValueError(f"unknown backtest_mode {backtest_mode!r} (expected 'independent')")

        strat = _load_strategy_class()()  # the single Strategy subclass in the mounted file, instantiated

        # #376 ruling 4: resolve the strategy's declared sizing method through the registry BEFORE any
        # of the three param-mode branches below reads a single bar. An unknown id raises here, at the
        # gate, for every sweep mode uniformly. Round 2 (E-R1-SF3): every branch now sizes with this
        # SAME resolved spec, the vectorized branch by passing sizing= explicitly to sweep_portfolio_cells,
        # the zero and inline branches by binding it onto each LeadInStrategyInput they build (lane SW, 5.8;
        # their own strat.run() calls the strategy's run(), which calls long_only_signal_result with no
        # sizing= of its own, so the bound field is the only path the registry's spec can reach that call).
        if "pinned_sizing_method" in spec:
            # P2h (DECISIONS #302): the reserve exam pins the sizing its graded run recorded, so a move of the
            # registry default cannot change what the exam tests. The registry method is looked up by the
            # pinned id and only its spec is replaced; every branch below sizes with it.
            five = {f.name for f in dataclasses.fields(SizingSpec)}
            try:
                pinned = json.loads(spec["pinned_sizing_spec"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"recorded-sizing-spec-unreadable: run {run_id} (the pinned sizing_spec is not JSON)") from exc
            if not isinstance(pinned, dict) or set(pinned) != five:
                raise ValueError(f"recorded-sizing-spec-unreadable: run {run_id} (the pinned sizing_spec is not "
                                 f"exactly the five fields {sorted(five)})")
            sizing_method = dataclasses.replace(resolve_sizing_method(spec["pinned_sizing_method"]),
                                                spec=SizingSpec(**pinned))
        else:
            sizing_method = resolve_sizing_method(strat.sizing_method)
        _log(f"[sweep] sizing_method={sizing_method.id}")

        emitted = {"cells": 0, "refusals": 0}  # lane D1: what went out, for the every-cell-refused check

        def emit(obj: dict) -> None:
            emitted["cells"] += len(obj.get("cells", ()))
            emitted["refusals"] += len(obj.get("cell_refusals", ()))
            print(json.dumps(obj), flush=True)  # one JSON line per chunk; the worker streams + POSTs it

        # Lane WF (6.2b): the walkforward spec key forks BEFORE the ordinary param_mode dispatch below,
        # never inside it -- a walkforward strategy dispatches with param_mode='inline' AND declares
        # class sweep_params (WF06AStrategy's own shape), which the ordinary 'inline' branch below
        # refuses outright as a dispatch-contract violation. Checking here first means that refusal
        # never fires for a walkforward run.
        wf_spec = spec.get("walkforward")
        if wf_spec is not None:
            if not params_decl_spec:
                # Lane WP (`6.5`, finding 2.3): the backend only fills params_decl for the
                # 'inline' dispatch mode (researchdb.py sends it only when param_mode ==
                # 'inline'; the vectorized mode's own grid comes from the class's sweep_params
                # tuple instead, read a few dozen lines below at `params_decl = strat.sweep_params`).
                # A class-VECTORIZED strategy (declares class sweep_params, ma_cross's own real
                # shape) therefore reaches this fork with NO params_decl_spec at all today, and
                # hit the bare refusal below unconditionally. Derive the declaration straight
                # from the INSTANTIATED strategy's own sweep_params tuple -- the SAME attribute
                # the ordinary vectorized path already reads -- before falling back to the
                # original refusal for a strategy that declares neither.
                if not strat.sweep_params:
                    raise ValueError("spec carries a walkforward block but no params_decl")
                params_decl_spec = [{"name": p.name, "type": p.kind} for p in strat.sweep_params]
                # The guard: the half that makes the derivation SAFE (finding 2.3's own second
                # half). Deriving the declaration alone would replace a loud failure with a
                # silent WRONG RESULT for a strategy whose run() never actually reads the
                # injected combo: the grid would run in full, _set_active_combo would be inert,
                # every combo would return the identical backtest, the in-sample step would
                # "pick" among identical numbers, and the out-of-sample leg would run whatever it
                # picked. Refuse by name, before the first slice, when any declared name is
                # missing from run()'s own self.param(...) calls.
                unread = _declared_params_unread_by_run(strat, params_decl_spec)
                if unread:
                    raise ValueError(
                        f"walkforward-declared-params-unread: {type(strat).__name__} declares "
                        f"sweep_params {tuple(d['name'] for d in params_decl_spec)} but run() "
                        f"never reads {tuple(unread)} through self.param(...); the walk-forward "
                        "engine injects each combo via self.param, so an unread name would "
                        "silently run the identical backtest for every combo in the grid"
                    )
            _log(f"[sweep] run_id={run_id} walkforward members={len(universe)} grid={grid} "
                 f"window=({window_start_ts},{window_end_ts}) wf={wf_spec}")
            wf_result = _process_walkforward(
                strat, universe, grid, params_decl_spec, window_start_ts, window_end_ts, settings,
                wf_spec, emit, run_id, sizing_spec=sizing_method.spec, init_cash=init_cash,
            )
            total_cells = wf_result.pop("slice_count")  # AC-18/AC-19: the accounting unit is the SLICE, never a per-combo cell
            emit({"sweep_done": True, "total_cells": total_cells, "annualization": None,
                  "environment": _environment(sizing_method), "walkforward": wf_result})
            _log(f"[sweep] done total_cells={total_cells} (walkforward)")
            return 0

        # M5 C: the single top-level fork on param_mode (the zero arm is the former `if single_combo:` block).
        if param_mode == "zero":
            # B4 (P-2): the zero-parameter single-run path. The strategy MUST declare no sweep_params — B3
            # sets the zero mode only for a zero-param strategy, so a strategy that DOES declare params here is
            # a dispatch-contract violation (fail loud, never a silent mis-run).
            if strat.sweep_params:
                raise ValueError(
                    f"param_mode='zero' but {type(strat).__name__} declares sweep_params "
                    f"{tuple(p.name for p in strat.sweep_params)}; the dispatch contract is violated"
                )
            _log(f"[sweep] run_id={run_id} zero members={len(universe)} "
                 f"window=({window_start_ts},{window_end_ts})")
            total_cells = _process_single_combo(
                strat, universe, window_start_ts, window_end_ts, settings, committed, emit,
                sizing_spec=sizing_method.spec)  # E-R1-SF3: the registry-resolved spec, bound per member
            # Per-member runs each annualize on their OWN windowed index (ragged members differ), so there is
            # no single run-level factor — record NULL, the same honest absence a mixed-calendar sweep records.
            run_annualization = None
            _check_not_all_refused(emitted["cells"], emitted["refusals"], committed)  # lane D1
            emit({"sweep_done": True, "total_cells": total_cells,
                  "annualization": run_annualization, "environment": _environment(sizing_method)})
            _log(f"[sweep] done total_cells={total_cells}")
            return 0

        if param_mode == "inline":
            # M5 C: the per-(combo x ticker) inline-parameter path. The strategy declares its sweepable values
            # inline via self.param (no class sweep_params, no sweep_signals override — the dispatch resolved the
            # mode), so the decls ride the spec. Fail loud if the payload is malformed (no params_decl).
            if not params_decl_spec:
                raise ValueError("param_mode='inline' but the payload carries no params_decl")
            if strat.sweep_params:  # an inline strategy must NOT also declare class sweep_params (dispatch refuses it)
                raise ValueError(
                    f"param_mode='inline' but {type(strat).__name__} also declares class sweep_params; "
                    f"the dispatch contract is violated"
                )
            _log(f"[sweep] run_id={run_id} inline members={len(universe)} grid={grid} "
                 f"window=({window_start_ts},{window_end_ts})")
            total_cells = _process_inline_param(
                strat, universe, grid, params_decl_spec, window_start_ts, window_end_ts, settings, committed, emit,
                sizing_spec=sizing_method.spec)  # E-R1-SF3: the registry-resolved spec, bound per member/combo
            run_annualization = None  # per-member/own-window, the same honest absence the zero-param path records
            _check_not_all_refused(emitted["cells"], emitted["refusals"], committed)  # lane D1
            emit({"sweep_done": True, "total_cells": total_cells,
                  "annualization": run_annualization, "environment": _environment(sizing_method)})
            _log(f"[sweep] done total_cells={total_cells}")
            return 0

        # The wide/vectorized parameter-sweep path (param_mode == 'vectorized'; declared sweep_params).
        if not strat.sweep_params:  # not sweepable + not zero/inline -> refuse (no single-run fallback, §4)
            raise SweepNotSupported(f"{type(strat).__name__} declares no sweep_params; it is not sweepable")
        params_decl = strat.sweep_params  # the SweepParam tuple (names + kinds + declared order)
        _log(f"[sweep] run_id={run_id} members={len(universe)} grid={grid} cap={size_cap_cells} "
             f"window=({window_start_ts},{window_end_ts})")

        # Partition the universe by asset class (§5.3). Each partition is one clean calendar.
        partitions: dict[str, list[dict]] = {}
        for m in universe:
            partitions.setdefault(m["asset_class"], []).append(m)
        _log(f"[sweep] partitions={ {k: len(v) for k, v in partitions.items()} }")

        variation_map = None  # combo-json -> variation_id, defined by the first partition
        partition_anns: list[dict] = []  # each partition's annualization (for the run-level recording decision)
        total_cells = 0
        cscv_block_sums: dict = {}  # lane C5: accumulated across every partition (see main()'s own artifact write below)
        cscv_bars_dropped: "int | None" = None
        for asset_class, members in partitions.items():  # one wide pass per class (§5.3)
            partition_cscv: dict = {}  # lane P3: filled by _process_partition's cscv_out, arity unchanged
            variation_map, ann, n = _process_partition(
                strat, members, grid, size_cap_cells, init_cash, committed, params_decl, variation_map, emit,
                window_start_ts=window_start_ts, window_end_ts=window_end_ts,  # B4: the COLD window
                sizing=sizing_method.spec,  # #376 ruling 4: the registry-resolved spec, explicit
                s_blocks=cscv_split_count,  # lane C5: None unless main() resolved S above
                cscv_out=partition_cscv)
            partition_block_sums = partition_cscv["block_sums"]
            partition_bars_dropped = partition_cscv["bars_dropped"]
            partition_anns.append(ann)  # remember this class's factor
            total_cells += n
            cscv_block_sums.update(partition_block_sums)  # lane C5: every trial's own key is unique per partition
            if partition_bars_dropped is not None:
                cscv_bars_dropped = partition_bars_dropped  # last partition wins (the documented mixed-calendar simplification)
            _log(f"[sweep] partition asset_class={asset_class} cells_emitted={n}")

        # Run-level annualization recording (§15.1): single asset class -> the one factor; mixed -> NULL.
        run_annualization = _run_level_annualization(partition_anns)

        _check_not_all_refused(emitted["cells"], emitted["refusals"], committed)  # lane D1
        # Lane C5 (set 16c, #427, AC-1): write the cscv block-sums artifact to the mounted run
        # dir, whenever S was resolved, whatever the trial count (AC-4's own lifecycle: an empty
        # artifact for a zero-cell sweep is a REAL, present file check 5 reads as "too few trials",
        # never conflated with a genuinely missing one, AC-5). run_sweep.py (the worker) decides,
        # after this container exits, whether to upload this file or the over-cap marker instead
        # (AC-8), reading the REAL total_cells from this same sweep-done line.
        _maybe_write_cscv_artifact(run_id, cscv_split_count, cscv_block_sums, cscv_bars_dropped)

        # The final line: the worker finalizes the run (status, run-level annualization, environment) from it.
        emit({"sweep_done": True, "total_cells": total_cells,
              "annualization": run_annualization, "environment": _environment(sizing_method)})
        _log(f"[sweep] done total_cells={total_cells}")
        return 0
    except Exception as e:  # noqa: BLE001 — process boundary, mirrors run_backtest_entrypoint: clean error line + fail
        traceback.print_exc(file=sys.stderr)  # full trace to stderr
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)  # machine-readable error on stdout
        return 1


if __name__ == "__main__":
    sys.exit(main())  # propagate the exit code to docker (the worker keys off rc)
