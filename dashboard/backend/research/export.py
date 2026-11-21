"""Run bundle generator (M3 §5.1; DECISIONS #23 read path = K-read-2).

`build_run_bundle` is the ONE canonical parquet->JSON read path. RunExplorer (brief K)
reads through it, and the K.5 renderers (json / md / html) consume the same dict — so the
page and the export can never disagree (the single-write-path discipline of DECISIONS #18).

It reads a run's metadata + stats from SQLite (researchdb) and its time-series artifacts from
the server ArtifactStore (brief K.0). Only a `succeeded` run has artifacts; for any other
status it raises BundleNotAvailable and the caller (the route / RunExplorer) renders a
placeholder instead of a bundle (spec §5.5).
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from dashboard.backend import artifact_store, researchdb

log = logging.getLogger(__name__)

# stats columns get_run joins in from research_run_stats (§3.5), in schema order.
_STATS_KEYS = (
    "total_return", "sharpe_ratio", "max_drawdown", "num_trades",
    "win_rate", "total_fees_paid", "total_slippage_cost", "profit_factor",
)
# run metadata exposed in the bundle (path columns + env are handled separately).
_RUN_KEYS = (
    "id", "job_id", "name", "status", "created_at", "started_at", "finished_at",
    "strategy_id", "strategy_version", "dataset_id", "dataset_sha256", "error",
)
_ENV_KEYS = ("vbt_version", "python_version", "build_provenance", "hostname")
# DBG-4 part B (#376 ruling 4): the sizing and cost provenance a run records. sizing_method is a
# plain string; sizing_spec and cost_model are stored as one JSON text each and parsed for the bundle.
_ENV_JSON_KEYS = ("sizing_spec", "cost_model")


class BundleError(Exception):
    """A bundle could not be built for a run. Catch a subclass first when the cause matters."""


class RunNotFoundForBundle(BundleError):
    """The run row is absent from research.db (the run id is unknown)."""


class CorruptRunRecord(BundleError):
    """The run row exists but a stored field is broken (JSON text that does not parse).

    The run is there, so this is a server-side data fault, not a missing run; routes map it to 500.
    """


class BundleNotAvailable(Exception):
    """The run exists but is not `succeeded`, so it has no artifacts to bundle (spec §5.5)."""


class RunNotComparable(Exception):
    """A run named in a compare request is missing or not succeeded (Unit-4 D1).

    Only a succeeded run has an equity series and a stats row, so a missing or non-succeeded run
    cannot be overlaid. Carries the run id and its status so the route can refuse the whole compare
    with a clear 4xx naming the offender (reject-don't-truncate: no partial answer).
    """

    def __init__(self, run_id: int, status: str):
        self.run_id = run_id
        self.status = status
        super().__init__(f"run {run_id} is not comparable (status={status!r}); only succeeded runs compare")


class IndicatorsArtifactError(Exception):
    """indicators.parquet exists and is non-empty, but its index is not a DatetimeIndex.

    A malformed indicators artifact (the writer keeps a datetime64 index, §3.8.4). Carries the
    run id, artifact, and actual index type so the bundle route surfaces a specific reason
    instead of an opaque 500. An EMPTY frame is NOT this error — that is the no-indicators case
    (every event run, plus the pre-fix empty-write artifacts), returned as {} by `_indicators`.
    """


class BundleArtifactMissing(Exception):
    """A mandatory artifact for this run's own bundle shape is absent from the artifact store,
    even though the run itself is `succeeded` (lane WB, 6.2c, AC-4). IndicatorsArtifactError's
    sibling: a real gap between "the run finished" and "the bytes actually landed", never
    silently papered over as an empty or fabricated series. Today the only caller is the
    walk-forward equity read: run_sweep.py's own relay uploads equity.parquet after the run
    already flipped to succeeded (a separate POST from the one that finalizes the run), and its
    own comment discloses that a failed upload there does not fail the run, only that this one
    optional artifact does not land. Carries the run id and artifact name so the route surfaces a
    specific, debuggable reason instead of an opaque 500.
    """

    def __init__(self, run_id: int, artifact: str):
        self.run_id = run_id
        self.artifact = artifact
        super().__init__(
            f"run {run_id}: {artifact} is absent from the artifact store (the run itself "
            f"succeeded; only this one artifact's own upload did not land)"
        )


def _environment_block(run_id: int, run: dict) -> dict:
    """The bundle's environment block: the four environment keys, then the three provenance keys.

    A NULL stays None (a run recorded before the column existed, e.g. before 2026-09-08). A stored
    JSON text that does not parse raises CorruptRunRecord naming the run and the field: never a default.
    """
    block = {k: run.get(k) for k in _ENV_KEYS}
    block["sizing_method"] = run.get("sizing_method")
    for key in _ENV_JSON_KEYS:
        raw = run.get(key)
        if raw is None:
            block[key] = None
            continue
        try:
            block[key] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptRunRecord(f"run {run_id}: {key} is not valid JSON: {exc}") from exc
    return block


def _read_parquet(run_id: int, name: str) -> pd.DataFrame:
    """Read one of a run's parquet artifacts from the ArtifactStore into a DataFrame."""
    with artifact_store.store.open(run_id, name) as fh:
        return pd.read_parquet(fh)


def _equity_pairs(df: pd.DataFrame) -> list[list]:
    """equity.parquet (timestamp, equity) -> [[ts, value], ...], full fidelity (L23)."""
    return [[int(ts), float(v)] for ts, v in zip(df["timestamp"], df["equity"])]


# Unit-4 D2: the fixed sparkline length. A presentation constant (a runs-table cell is ~80-120px
# wide, so ~32 points reads the curve's shape as a thumbnail and no more); NOT a backtest
# parameter, so it carries no parameter_provenance entry.
SPARKLINE_POINTS = 32


def _lttb(values: list[float], target: int = SPARKLINE_POINTS) -> list[float]:
    """Largest-Triangle-Three-Buckets downsample of a value series to at most `target` points.

    Deterministic. Keeps the FIRST and LAST value exactly (so a sparkline's endpoints equal the
    equity series' endpoints), buckets the middle, and keeps per bucket the value forming the
    largest triangle with the previously-kept value and the next bucket's average — so the visible
    peaks and troughs (the drawdowns an equity sparkline exists to show) survive, where a plain
    every-Nth stride could skip them. The x-axis is the integer index (timestamps are not stored,
    D2). A series already <= target (or shorter than the 3 LTTB needs) is returned unchanged: no
    upsampling, no synthetic points. The published Steinarsson algorithm, a few lines, no dependency.
    """
    n = len(values)
    if target < 3 or n <= target:                      # too few to bucket, or already short enough
        return [float(v) for v in values]
    sampled = [float(values[0])]                        # the first value is always kept
    bucket_size = (n - 2) / (target - 2)                # the middle target-2 values, one per bucket
    a = 0                                               # index of the last value kept
    for i in range(target - 2):
        # The NEXT bucket's average point (x = index, y = value) — the third triangle vertex.
        nxt_start = int((i + 1) * bucket_size) + 1
        nxt_end = min(int((i + 2) * bucket_size) + 1, n)
        cnt = nxt_end - nxt_start                        # invariant: >= 1 (bucket_size > 1 when n > target)
        avg_x = sum(range(nxt_start, nxt_end)) / cnt
        avg_y = sum(values[nxt_start:nxt_end]) / cnt
        # The CURRENT bucket: pick the value whose triangle (a, candidate, next-avg) is largest.
        cur_start = int(i * bucket_size) + 1
        cur_end = int((i + 1) * bucket_size) + 1
        ax, ay = float(a), float(values[a])
        best_area, best = -1.0, cur_start
        for j in range(cur_start, cur_end):
            area = abs((ax - avg_x) * (values[j] - ay) - (ax - j) * (avg_y - ay))  # 2x area; the *0.5 is constant
            if area > best_area:
                best_area, best = area, j
        sampled.append(float(values[best]))
        a = best
    sampled.append(float(values[n - 1]))               # the last value is always kept
    return sampled


def equity_sparkline_from_artifact(run_id: int) -> list[float]:
    """Read a succeeded run's equity.parquet and return its LTTB-32 sparkline (Unit-4 D2).

    The SINGLE source of the downsample, used by BOTH the finalize hook (routes /runs/{id}/result)
    and the one-time backfill — so a backfilled sparkline is byte-identical to one a fresh run
    produces and the two can never diverge. Returns at most 32 floats (fewer if the series is
    shorter); the values are the raw equity, normalization is the renderer's job.
    """
    df = _read_parquet(run_id, "equity.parquet")
    return _lttb([float(v) for v in df["equity"]])


def backfill_equity_sparklines() -> dict:
    """Unit-4 D2 one-time backfill: fill equity_sparkline for succeeded runs that predate the
    column (still NULL). Idempotent — only NULL rows are selected and `set_run_sparkline` guards
    the UPDATE on NULL, so a re-run leaves already-filled rows byte-unchanged. Uses the SAME `_lttb`
    helper as the finalize hook, so a backfilled sparkline is byte-identical to a freshly-computed
    one. A succeeded run whose equity.parquet is missing from the store (e.g. a reaped legacy
    artifact) is skipped and counted, not crashed on — it stays honestly NULL (renders empty).
    Returns {filled, skipped_missing, skipped_empty} for the deploy-gate log.
    """
    filled = 0
    skipped_missing = 0
    skipped_empty = 0
    for run_id in researchdb.runs_missing_sparkline():
        if not artifact_store.store.exists(run_id, "equity.parquet"):
            skipped_missing += 1                        # legacy run, artifact gone -> stays NULL
            continue
        try:
            sparkline = equity_sparkline_from_artifact(run_id)
        except artifact_store.ArtifactNotFound:         # raced/removed between exists() and open()
            skipped_missing += 1
            continue
        if not sparkline:                               # a zero-row equity (e.g. a zero-trade run):
            skipped_empty += 1                          # nothing to store, stays honestly NULL, and
            continue                                    # NOT counted as filled (keeps the count honest)
        researchdb.set_run_sparkline(run_id, sparkline)
        filled += 1
    return {"filled": filled, "skipped_missing": skipped_missing, "skipped_empty": skipped_empty}


def build_run_comparison(run_ids: list[int]) -> list[dict]:
    """Unit-4 D1: the run-compare payload — {id, name, stats, equity} per run, in REQUESTED order.

    Only succeeded runs are comparable; a missing or non-succeeded run raises RunNotComparable
    (the route maps it to a 4xx naming the id + status, reject-don't-truncate — no partial answer).
    Each run's `equity` reuses the full-fidelity [[ts, value]] pair shape build_run_bundle serves
    (_equity_pairs), which is exactly how the architecture envisions M7's compare surface composing
    ("Multi-run bundle wraps build_run_bundle per run", arch §21) — so this is M7's seed, not a
    throwaway. Event runs compare normally (equity exists; no price-only field is in this payload).
    """
    out: list[dict] = []
    for run_id in run_ids:                                       # requested order preserved
        row = researchdb.get_run(run_id)
        if row is None:
            raise RunNotComparable(run_id, "missing")
        if row["status"] != "succeeded":
            raise RunNotComparable(run_id, row["status"])
        out.append({
            "id": row["id"],
            "name": row["name"],
            "stats": {k: row.get(k) for k in _STATS_KEYS},      # the eight research_run_stats fields
            "equity": _equity_pairs(_read_parquet(run_id, "equity.parquet")),
        })
    return out


def _price_rows(df: pd.DataFrame) -> list[dict]:
    """price.parquet -> [{timestamp, open, high, low, close, volume}, ...] (§3.8.7)."""
    return [
        {"timestamp": int(r.timestamp), "open": float(r.open), "high": float(r.high),
         "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
        for r in df.itertuples(index=False)
    ]


def _indicators(df: pd.DataFrame, run_id: int) -> dict[str, list[list]]:
    """indicators.parquet (DatetimeIndex, one col per indicator) -> {key: [[ts_s, val], ...]}.

    The datetime index -> UNIX seconds, paired with each column's value. as_unit("ns") first:
    the container writes the index at nanosecond resolution (pandas 2.3), but the server reads
    parquet datetimes at MICROSECOND resolution (pandas 3.0 default), so a bare asi8 // 10**9
    would be 1000x off. Normalising to ns makes the conversion resolution-independent.

    Two guards run before that conversion (the run-result black-screen fix):
    - An EMPTY frame -> {}. An event run writes no indicators (§8.6 returns indicators={}), and the
      pre-fix worker wrote an index-less empty parquet for it; read back, that frame is empty.
      Empty means "no indicators", i.e. {} — not an error, and it never reaches the datetime read.
    - A NON-empty frame whose index is not a DatetimeIndex is malformed (the writer keeps a
      datetime64 index, §3.8.4). Raise IndicatorsArtifactError with the run id, artifact, and actual
      index type, so the caller surfaces a specific reason rather than the bare `.as_unit()`
      AttributeError as an opaque 500. NO blanket fallback — a real malformation fails loud.
    """
    if df.empty:
        return {}  # no indicators to convert (event run, indicator-less run, or a pre-fix empty artifact)
    if not isinstance(df.index, pd.DatetimeIndex):
        raise IndicatorsArtifactError(
            f"run {run_id}: indicators.parquet is non-empty but has a "
            f"{type(df.index).__name__} index (expected a DatetimeIndex); columns={list(df.columns)}"
        )
    ts = df.index.as_unit("ns").asi8 // 10**9  # force ns, then ns -> seconds (resolution-safe)
    out: dict[str, list[list]] = {}
    for col in df.columns:
        out[col] = [[int(t), (None if pd.isna(v) else float(v))]
                    for t, v in zip(ts, df[col].to_numpy())]
    return out


def _records(df: pd.DataFrame) -> list[dict]:
    """A readable parquet (trades §3.8.1 / drawdowns §3.8.2) -> list of row dicts, NaN -> None."""
    return json.loads(df.to_json(orient="records"))  # to_json normalizes NaN->null and numpy types


def _benchmark_pairs(price_rows: list[dict], equity_pairs: list[list]) -> list[list]:
    """Buy-and-hold benchmark: equity-curve-shaped pairs from price + initial equity.

    Normalised so the benchmark starts at the same cash as the strategy: scale close
    by (equity[0] / close[0]). That is exactly what `vbt.Portfolio.from_holding`
    would emit for the same starting cash and zero fees / zero slippage — the same
    series viewed two different ways.

    Returns [] if either input is empty or the first close is not positive (degenerate
    data should not crash the bundle build).
    """
    if not price_rows or not equity_pairs:
        return []
    initial_equity = float(equity_pairs[0][1])  # equity[0] is the strategy's starting cash
    first_close = float(price_rows[0]["close"])  # the buy-in price
    if first_close <= 0:  # degenerate input; emit nothing rather than divide-by-zero
        return []
    scale = initial_equity / first_close  # multiplier that aligns t=0
    return [[int(p["timestamp"]), float(p["close"]) * scale] for p in price_rows]


# TERM M4.5 3a B1.4: the scope-rename alias. A run dispatched before the newsbot_v3 -> event_strategy
# rename froze its snapshot under the OLD scope; today's resolved values use the new scope. Normalizing
# the snapshot scope through this alias keeps the rename ALONE from showing as drift (the canon's
# reader-alias remedy for a field rename, Kleppmann Ch 5 canon:1278). Run snapshots are immutable and
# never rewritten; this alias is the one place the old name survives, by design.
_SCOPE_ALIASES = {"newsbot_v3": "event_strategy"}


def _settings_drift(snapshot_json: str | None, strategy_id: int | None = None) -> dict:
    """Compare the run's settings snapshot vs the strategy's RESOLVED settings today (override-aware).

    Returns {"drifted": bool, "changes": [{"scope", "key", "run_value", "current_value"}, ...]}.
    A change is recorded when the same (scope, key) exists in both with different values. Today's values
    are resolved THROUGH the run's strategy overrides (the SAME _resolve_strategy_settings helper the
    dispatch paths use — never a second overlay here), so "what changed since this ran" stays honest for
    an overriding strategy (R-OV4). Both sides are typed (the snapshot stores typed values; the live read
    is _live_settings), so the comparison never flips on representation alone (`"1e-3"` vs `0.001`). The
    pre-rename scope is aliased to the new one so the newsbot_v3 -> event_strategy rename alone is not
    drift. Keys present on only one side are not reported (snapshot freezes the layout that existed then).
    """
    if not snapshot_json:
        return {"drifted": False, "changes": []}
    snapshot = json.loads(snapshot_json)
    # Snapshot rows: {scope, key, value, value_type}; alias the (possibly pre-rename) scope, index by (scope,key).
    snap_index = {
        (_SCOPE_ALIASES.get(row["scope"], row["scope"]), row["key"]): row.get("value")
        for row in snapshot
    }
    # Today's RESOLVED values for this strategy: the global typed rows overlaid with its overrides via the
    # one shared helper. strategy_id is None only for a legacy caller; then the resolution is globals alone.
    with researchdb._connect() as conn:
        current = researchdb._live_settings(conn)
        if strategy_id is not None:
            current = researchdb._resolve_strategy_settings(conn, strategy_id, current)
    changes: list[dict] = []
    for row in current:
        scope, key, current_value = row["scope"], row["key"], row["value"]
        run_value = snap_index.get((scope, key))
        if run_value is not None and str(run_value) != str(current_value):
            changes.append({
                "scope": scope, "key": key,
                "run_value": run_value, "current_value": current_value,
            })
    return {"drifted": bool(changes), "changes": changes}


# Lane WB (6.2c, DECISIONS #394 ruling 1's own "by its STAGE"): the reason word each of the four
# artifacts a walk-forward run never writes carries in the bundle's own absent_artifacts dict, so
# a reader of the JSON (or the md/html render below) gets a named fact rather than a bare empty
# list with no explanation. run_sweep_entrypoint.py's own walk-forward branch (lines 868 to 953,
# the plan's own citation) writes only equity.parquet; drawdowns.parquet, price.parquet,
# trades.parquet and meta.json are never written for this run shape, by design, not by omission.
_WALKFORWARD_ABSENT_ARTIFACT_REASONS: dict[str, str] = {
    "drawdowns": "walkforward-entrypoint-writes-no-drawdowns-artifact",
    "price": "walkforward-entrypoint-writes-no-price-artifact",
    "trades": "walkforward-entrypoint-writes-no-trades-artifact",
    "meta": "walkforward-entrypoint-writes-no-meta-artifact",
}

# Ops routing law (root CLAUDE.md: "no merge with a declared failure mode unmonitored"), the same
# plain module-level counter dict shape dashboard/backend/routes/display.py's own
# DISPLAY_MONITOR_COUNTERS uses: registry row walkforward_bundle_reads_failed_by_reason. The two
# routes below (get_run_bundle, export_run in routes/research.py) increment this by reason word at
# their own except sites for a run this function already identified as walk-forward, so a stuck-
# refusing reason (a relay that keeps failing to upload equity.parquet, a run stuck at a
# non-succeeded status) is visible as a rising count, not silence. Registry row
# walkforward_runs_without_equity_artifact is the query function below instead of a counter,
# because it is a POINT-IN-TIME fact about the database + artifact store ("how many succeeded
# walk-forward runs have no equity.parquet right now"), not a rate of route hits.
WALKFORWARD_BUNDLE_MONITOR_COUNTERS: dict[str, int] = {
    "not-succeeded": 0, "artifact-missing": 0, "malformed-indicators": 0,
}


def query_walkforward_runs_without_equity_artifact() -> int:
    """Registry row walkforward_runs_without_equity_artifact: the count of succeeded walk-forward
    runs (a granted, linked draw at stage='walkforward', DECISIONS #394 ruling 1) whose artifact
    store holds no equity.parquet. Expected zero on a healthy pipeline: run_sweep.py's own relay
    uploads equity.parquet in the SAME call that then POSTs walkforward-result (lines 277 to 287),
    so a nonzero reading names a relay that failed after the run already flipped to succeeded (the
    disclosed, logged-not-fatal failure mode BundleArtifactMissing's own docstring names), never an
    expected state. Mirrors researchdb.query_runs_missing_walkforward_record's own self-join shape
    for "which runs are walk-forward", then checks the artifact store per run id -- a check
    researchdb.py itself cannot make (it never imports artifact_store), so this lives here.
    """
    conn = researchdb._connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT rl.run_id AS run_id FROM research_data_draws rl "
            "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
            "JOIN research_runs r ON r.id = rl.run_id "
            "WHERE rl.event_kind = 'run_linked' AND rl.decision = 'granted' AND dr.stage = 'walkforward' "
            "  AND r.status = 'succeeded';"
        ).fetchall()
    finally:
        conn.close()
    return sum(1 for row in rows if not artifact_store.store.exists(row["run_id"], "equity.parquet"))


def run_is_walkforward(run_id: int) -> bool:
    """AC-1: True iff this run's own draw was GRANTED at stage='walkforward' and LINKED to it
    (tracker.request_draw + tracker.link_run, the real write path stage_runner.dispatch_stage
    uses in production). DECISIONS #394 ruling 1 says a walk-forward run is admitted "by its
    STAGE"; the stage lives only on research_data_draws (the tracker's own append-only event
    log), never on research_runs itself (research_runs carries no stage column at all, so
    _RUN_COLUMNS in researchdb.py cannot project one). This mirrors
    researchdb.query_runs_missing_walkforward_record's own self-join shape exactly (a granted
    run_linked row joined back to its own draw_requested row), never a hand-rolled shortcut.

    This getter lives here, in export.py, rather than beside record_walkforward_slices in
    researchdb.py, ONLY because researchdb.py is a different lane's file tonight (the night plan,
    set 9a, section 9, item 1). Moving it to researchdb.py once that lane's own change has landed
    is a sprint note, not a design choice: a stage read belongs beside the tracker's own read
    helpers, not in the read-side render module, and the two lines below are exactly the kind of
    small, mechanical move that belongs in the next touch of that file.

    Never raises for an unknown run_id: a run that does not exist simply has no granted, linked
    walk-forward draw, so this reads False. The caller's own 404 handling (a separate check on the
    run itself) is what surfaces an unknown run; this function answers one question only.
    """
    conn = researchdb._connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM research_data_draws rl "
            "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
            "WHERE rl.event_kind = 'run_linked' AND rl.decision = 'granted' AND rl.run_id = ? "
            "  AND dr.stage = 'walkforward' LIMIT 1;",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()  # explicit close: sqlite3.Connection's own context manager only commits/rolls
        # back, it never closes the connection, so a bare "with researchdb._connect() as conn:"
        # (the shape _settings_drift above uses) leaks one connection object per call. This function
        # is called on every /bundle and /export request for a walk-forward run, so the extra two
        # lines here are worth it rather than repeating that existing leak in a new call site.
    return row is not None


def _read_walkforward_block(run_id: int) -> dict:
    """AC-2's new "walkforward" bundle block: the three run-level figures
    (in_sample_profit, out_of_sample_profit, wfa_boundary_buffer_bars) record_walkforward_run_stats
    wrote onto research_run_stats, plus every research_walkforward_slices row for this run in
    slice_index order, plus the absent-artifact reasons above. Read directly over
    researchdb._connect() because _RUN_COLUMNS (researchdb.py's own run+stats projection) does not
    carry the three walk-forward columns, and researchdb.py is not this lane's file to widen it in
    (the same reason run_is_walkforward above lives here rather than there).

    Lifecycle: a walk-forward run can be admitted here (status succeeded, a granted+linked
    walk-forward draw) before the worker's own walkforward-result relay has written a
    research_run_stats row for it -- the sweep-result POST flips the run to succeeded FIRST, and
    the walkforward-result POST that writes the stats row and the slice rows follows as a SEPARATE
    call (run_sweep.py lines 277 to 287, the plan's own citation). A missing stats row is answered
    honestly here (every run-level figure None, zero slices), never a crash and never a fabricated
    number -- the same "NULL is not the same as a fabricated value" discipline AC-6(e) states for
    a genuinely NULL wfa_boundary_buffer_bars.
    """
    conn = researchdb._connect()
    try:
        stats_row = conn.execute(
            "SELECT in_sample_profit, out_of_sample_profit, wfa_boundary_buffer_bars "
            "FROM research_run_stats WHERE run_id = ?;",
            (run_id,),
        ).fetchone()
        slice_rows = conn.execute(
            # Lane LB (5.8e): fill_reach_ts and entries_masked_at_end ride beside the original nine
            # columns, additive and nullable, so a pre-merge slice row simply reads None for both
            # (dict(r) below turns a NULL into a plain None key, never a fabricated 0 or absent key).
            "SELECT slice_index, in_sample_start_ts, in_sample_end_ts, "
            "       out_of_sample_start_ts, out_of_sample_end_ts, param_config_hash, "
            "       in_sample_profit, out_of_sample_profit, trade_count, "
            "       fill_reach_ts, entries_masked_at_end "
            "FROM research_walkforward_slices WHERE run_id = ? ORDER BY slice_index ASC;",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()  # same explicit close as run_is_walkforward above, same reason

    slices = [dict(r) for r in slice_rows]  # sqlite3.Row -> plain dict, JSON-serialisable as-is
    return {
        "kind": "walkforward",
        "slices": slices,
        "in_sample_profit": stats_row["in_sample_profit"] if stats_row is not None else None,
        "out_of_sample_profit": stats_row["out_of_sample_profit"] if stats_row is not None else None,
        "wfa_boundary_buffer_bars": (
            stats_row["wfa_boundary_buffer_bars"] if stats_row is not None else None
        ),
        "slice_count": len(slices),  # equals the table's own row count for this run, by construction
        "absent_artifacts": dict(_WALKFORWARD_ABSENT_ARTIFACT_REASONS),  # a fresh copy per call
    }


def build_run_bundle(
    run_id: int,
    *,
    include_source: bool = True,
    include_settings: bool = True,
    include_indicators: bool = True,
    include_trades: bool = True,
    include_logs: bool = False,
    is_walkforward: "bool | None" = None,
) -> dict:
    """Build the canonical, format-agnostic bundle dict for a succeeded run (§5.1).

    Raises RunNotFoundForBundle if the run is unknown, CorruptRunRecord if a stored provenance field
    is not valid JSON, BundleNotAvailable if it is not `succeeded`. Both subclasses are BundleError.

    Round 2 fold, SF1 (code_quality_pass2.md, confirmed against source): is_walkforward defaults to
    None. routes/research.py's shared _bundle_or_refuse helper already determines is_walkforward for
    every one of its three callers (get_run_bundle, export_run, the public share route) as part of
    its own admission/counter logic, so it now PASSES that value straight through here instead of
    letting this function silently repeat the same research_data_draws self-join a second time on
    every request. A direct caller with no admission context of its own (there is none in this
    codebase today, but the parameter stays optional so one is never forced to invent one) still gets
    the exact behaviour this function always had: is_walkforward is computed here when the caller
    passes None.
    """
    run = researchdb.get_run(run_id)
    if run is None:
        raise RunNotFoundForBundle(f"run {run_id} not found")
    if run["status"] != "succeeded":
        raise BundleNotAvailable(
            f"run {run_id} status is {run['status']}; bundles available only for succeeded runs"
        )

    # Strategy as it was AT the run's dispatch version (history), not the possibly-edited current row.
    strat = researchdb.get_strategy(run["strategy_id"]) or {}
    versioned = researchdb.get_strategy_version(run["strategy_id"], run["strategy_version"]) or {}
    dataset = researchdb.get_dataset(run["dataset_id"]) or {}
    # M5 UU scale-defect 1.3: classify event-ness by the RUN's nature (run_is_event), NOT the dataset_type.
    # The B2 bridge lets an event run target a price_universe (dataset_type='price_universe', not 'universe'),
    # so the old inline `dataset_type == "universe"` check mis-read it as a PRICE run and tried to read the
    # price.parquet an event run never writes -> a 500 on the bundle. run_is_event keys on the job kind + the
    # event payload (the single classifier), so it is correct for an event run over ANY universe type.
    is_event = researchdb.run_is_event(run_id)  # an event run writes no price.parquet (skip the price block below)
    # Lane WB (6.2c, AC-1/AC-2): a walk-forward run is dispatched under job kind 'run_sweep' (the
    # same kind an ordinary universe sweep uses), so it is neither is_event nor, on its own, exempt
    # from run_is_sweep's own kind check. The route (get_run_bundle) is what decides admission by
    # stage BEFORE calling this function; this flag here decides which artifacts this function reads
    # and what shape the returned bundle takes, entirely independent of the route's own admission
    # decision (a caller could, in principle, call build_run_bundle directly for a walk-forward run).
    #
    # Round 2 fold, SF1: the CALLER already knows this (routes/research.py's _bundle_or_refuse
    # determines it via the cheap run_is_sweep-first path before ever reaching here) and passes it
    # through the is_walkforward keyword, so this function no longer pays a SECOND, unconditional
    # research_data_draws self-join on every request when the answer is already in hand. It is
    # computed here, exactly as before this fold, only when a caller passes None (no admission
    # context of its own).
    if is_walkforward is None:
        is_walkforward = run_is_walkforward(run_id)

    # meta.json is uploaded by the worker's artifact step (C2.5 D1) and is mandatory in the store
    # (the K.0 gate, _MANDATORY_ARTIFACTS / _MANDATORY_ARTIFACTS_EVENT). It carries the regimes slot
    # and, for an event run, the universe window + event types. A genuinely-absent meta.json is only
    # possible for a legacy run that predates the C2.5 upload fix and could not be backfilled: that is
    # NOT silently defaulted to {} (which would read as an honest "no regimes" — the very gap C2.5
    # fixed). Instead it sets meta_missing=True so the read path and the UI can distinguish a real
    # pipeline gap from an honest empty (CLAUDE.md: no silent fallbacks).
    meta_missing = False
    try:
        with artifact_store.store.open(run_id, "meta.json") as fh:
            meta_art = json.loads(fh.read())  # bytes -> dict (json.loads accepts bytes)
    except artifact_store.ArtifactNotFound:
        meta_art = {}
        meta_missing = True
        log.warning("meta.json absent from store", extra={"run_id": run_id})
    if is_walkforward:
        # AC-2: the walk-forward entrypoint never writes meta.json at all (run_sweep_entrypoint.py
        # lines 868 to 953 write only equity.parquet), so an absent meta.json here is an honest,
        # by-design absence for this run shape, never the C2.5 pipeline gap meta_missing=True exists
        # to flag. The warning above still fires (the read genuinely found nothing, which is worth
        # logging once), but the bundle's own meta_missing reads False for a walk-forward run.
        meta_missing = False

    # regimes: the RunResult.regimes dict {kind: [(start, end, label), ...]} flattened to the band
    # LIST the frontend expects (research.ts regimes: unknown[]); [] for a price run (regimes {}).
    regime_bands = [
        {"kind": kind, "start": int(s), "end": int(e), "label": label}
        for kind, bands in (meta_art.get("regimes") or {}).items()
        for (s, e, label) in bands
    ]

    if is_event:  # render a UNIVERSE block, never a price-dataset block with sentinel timeframe/start_ts (§12)
        dataset_block = {
            "kind": "universe",  # the report shows a universe + window + event types, not a price series
            "id": dataset.get("id"),
            "name": dataset.get("symbol"),  # §3.3: the universe loader stores the universe NAME in `symbol`
            "source": dataset.get("source"),
            "window": meta_art.get("event_window"),  # [knowledge_from, knowledge_to]
            "event_types": meta_art.get("event_types"),  # the event types the run consumed
        }
    else:  # a price dataset (M3): the original block, byte-unchanged
        dataset_block = {
            "id": dataset.get("id"), "source": dataset.get("source"),
            "symbol": dataset.get("symbol"), "timeframe": dataset.get("timeframe"),
            "start_ts": dataset.get("start_ts"), "end_ts": dataset.get("end_ts"),
            "sha256": dataset.get("sha256"),
        }
    # An event run writes no price.parquet (the entrypoint skips _write_price, §3), so the price block
    # is empty for it; a price run reads its OHLCV as before. Lane WB: a walk-forward run's own
    # entrypoint writes no price.parquet either (AC-2), so it takes the same empty-price path.
    price_rows = [] if (is_event or is_walkforward) else _price_rows(_read_parquet(run_id, "price.parquet"))
    # AC-2: drawdowns.parquet is never written by the walk-forward entrypoint either; reading []
    # here instead of opening the artifact avoids an ArtifactNotFound crash on a legitimately
    # absent file, the same shape price_rows just took for is_event/is_walkforward.
    drawdowns_rows = [] if is_walkforward else _records(_read_parquet(run_id, "drawdowns.parquet"))
    # AC-4: equity.parquet IS mandatory for a walk-forward run's own bundle (it is the one artifact
    # the entrypoint's relay does write, run_sweep.py lines 277 to 287). Its absence here is a real
    # data gap (the relay's own disclosed failure mode, "the run already succeeded... only this one
    # optional artifact did not land"), never an honest by-design empty like price/drawdowns/trades
    # above -- so it raises BundleArtifactMissing by name instead of silently returning [] or
    # letting a bare ArtifactNotFound reach the route as an unnamed crash. An ordinary run's equity
    # read below this block is UNCHANGED: the K.0 mandatory-artifact gate already guarantees it
    # exists for every non-walk-forward succeeded run, so no try/except is added there.
    if is_walkforward:
        try:
            equity_pairs = _equity_pairs(_read_parquet(run_id, "equity.parquet"))
        except artifact_store.ArtifactNotFound as exc:
            raise BundleArtifactMissing(run_id, "equity.parquet") from exc
    else:
        equity_pairs = _equity_pairs(_read_parquet(run_id, "equity.parquet"))

    bundle: dict = {
        "schema_version": 1,
        "run": {k: run.get(k) for k in _RUN_KEYS},
        "stats": {k: run.get(k) for k in _STATS_KEYS},
        "strategy": {
            "name": strat.get("name"),
            "version": run["strategy_version"],
            "description": versioned.get("description", strat.get("description")),
        },
        "dataset": dataset_block,  # universe (event run) or price dataset (price run), BN-I §12
        "equity": equity_pairs,
        "drawdowns": drawdowns_rows,
        "price": price_rows,  # [] for an event run or a walk-forward run (neither writes price.parquet)
        "regimes": regime_bands,  # the rule-based 2024-02-05 band(s); [] for a price run (§12)
        # C2.5 D4: True only when meta.json is genuinely absent from the store (a legacy run that
        # predates the upload fix and could not be backfilled). False for every backfilled / new run,
        # including a price run with no regimes (an honest empty, not a gap) — the UI shows a distinct
        # pipeline-gap notice for True vs the honest-empty message for False.
        "meta_missing": meta_missing,
        "environment": _environment_block(run_id, run),
        # Phase-2 Unit 3: the price run's signal funnel (audit concern #1) — the per-run fact row that
        # makes a valid-zero distinguishable from a no-data zero. None for an event run (the funnel is a
        # price-path concept; the event analog is its coverage_* stats) or a legacy/bespoke run with no
        # row, so the UI renders the funnel panel only when present.
        "funnel": researchdb.get_run_funnel(run_id),
    }
    # V1: buy-and-hold benchmark derived from price + equity[0]. Always emitted —
    # cheap to compute (one scaled multiply per bar) and useful even for non-included
    # contexts (the strategy chart needs it; public shares can choose to omit).
    bundle["benchmark"] = _benchmark_pairs(bundle["price"], bundle["equity"])

    if include_source:
        bundle["strategy"]["source"] = versioned.get("source_code", strat.get("source_code"))
    if include_settings:
        snapshot = researchdb.get_run_settings_snapshot(run_id)
        bundle["settings_snapshot"] = json.loads(snapshot) if snapshot else None
        # A4: settings-drift report — values changed in research_settings since this
        # run's dispatch. Gated by include_settings (drift is meaningless without the
        # snapshot to compare against, and a public share that hides settings should
        # also hide which keys drifted).
        bundle["settings_drift"] = _settings_drift(snapshot, run["strategy_id"])
    if include_trades:
        # AC-2: trades.parquet is never written by the walk-forward entrypoint either.
        bundle["trades"] = [] if is_walkforward else _records(_read_parquet(run_id, "trades.parquet"))
    if include_indicators:
        # indicators.parquet is optional (spec §3.4): absent -> {} here, and an empty present frame
        # (every event run, plus pre-fix artifacts) -> {} inside _indicators. A non-empty malformed
        # frame raises IndicatorsArtifactError (surfaced by the route), never a silent or blank result.
        bundle["indicators"] = (
            _indicators(_read_parquet(run_id, "indicators.parquet"), run_id)
            if artifact_store.store.exists(run_id, "indicators.parquet") else {}
        )
    if include_logs:
        bundle["logs"] = researchdb.run_log_lines(run["job_id"])

    if is_walkforward:
        # AC-2: the new block, appended last so every existing reader that iterates the bundle's
        # OTHER keys is unaffected by its presence, and so a byte-diff against an ordinary run's
        # bundle (p3's own golden) shows nothing but this one addition for a walk-forward run.
        bundle["walkforward"] = _read_walkforward_block(run_id)

    return bundle


if __name__ == "__main__":  # one-time Unit-4 D2 backfill: python -m dashboard.backend.research.export
    print(backfill_equity_sparklines())
