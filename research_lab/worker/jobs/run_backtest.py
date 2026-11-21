"""Job handler: run_backtest (M3 brief D, arch §7, DECISIONS #20).

Spawns research-runner:m3-latest to run one strategy against one dataset, then
reports the result. The worker NEVER imports vbt and NEVER touches a portfolio —
the baked container entrypoint owns the whole backtest and run-finalize; the
worker only marshals inputs in and reads artifacts back out.

The run context arrives in the job PAYLOAD (the worker has no DB access — it talks
only over the dashboard HTTP API, system_reference §15). The backend embeds
`run_id`, the DB-versioned `source_code`, the dataset container path, and the
`settings` at dispatch (brief E), exactly like the M2 fetch_dataset payload.

Per-run steps:
  1. write the strategy source to /tmp/run_{job_id}/strategy.py and a run_spec.json
     (dataset path, settings, run_id) beside it;
  2. pre-create the host run dir ~/trading/research_lab/data/runs/{run_id}/ (the
     container user 1000 must be able to write into it — arch §7 pre-spawn step);
  3. docker run with the five-mount, --network=none profile (arch §7);
  4. stream container stdout as job logs;
  5. on exit 0: read stats.json + meta.json off disk and report the run result
     (succeeded) + complete the job; on non-zero: report the run + job failed.

Scope (DECISIONS #20): this handler only REPORTS over the worker callback; the
backend (brief E) writes research_runs. No row-state is touched here.

Import-light on purpose (mirrors hard_delete_dataset): httpx and the report
helpers are imported lazily inside run(), and the pure helpers below need no httpx
or worker env, so the brief D local probe can import this module on a plain
interpreter and exercise them directly.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

DOCKER_IMAGE = os.environ.get("RESEARCH_RUNNER_IMAGE", "research-runner:m3-latest")  # NOT m2-latest: m3 bakes the strategies/ package (arch §7); the env override is for a lane probe's own tag only

CANCEL_POLL_S = 1.0   # heartbeat-and-cancel-poll cadence during a backtest (L7 default)
CANCEL_GRACE_S = 30.0  # SIGTERM -> wait -> SIGKILL window (arch §20, L6 atomic-backtest contract)

# M5 UU scale-defect 1.2 (defense-in-depth): the StreamReader buffer limit for the container's stdout. The
# asyncio default is 64 KB, and a single line over it raises ValueError('...chunk is longer than limit') and
# crashed run 14's sweep. The entrypoint now byte-bounds every emitted line well under 64 KB (the real fix),
# so this raised ceiling is a backstop, not load-bearing. Reused by run_sweep's spawn (one source of truth).
STDOUT_STREAM_LIMIT = 8 * 1024 * 1024  # 8 MB readline buffer (~500x headroom over the entrypoint's line budget)


class EventMaterializeError(RuntimeError):
    """A (event_type, universe, window) slice failed to materialize (M3.5 brief I, EL71).

    Raised by materialize_event_slices naming the failed slice, so an event run fails honestly
    BEFORE any container starts, with no fallback to a partial mount set (CLAUDE.md: no
    fallbacks; the §8 failure semantics). The run() wiring catches it and reports the run failed.
    """


class FundamentalsMaterializeError(RuntimeError):
    """The fundamentals fact-table slice failed to materialize or stage (M3.75 FUND-2, §6.2).

    Raised by stage_fundamentals_slice on a materializer HTTP error, a disk failure, or a 200 body
    missing a required header, so a fundamentals-conditioned event run fails honestly BEFORE any
    container starts, with no fallback to a partial mount set (the EventMaterializeError precedent,
    CLAUDE.md: no fallbacks). The run() wiring catches it and reports the run AND the job failed.
    """


class PriceProvisionError(RuntimeError):
    """A price step failed BEFORE the container (M3.5 price-provisioning brief, DECISIONS #63; EL71-consistent).

    Raised either when the staged event slice cannot be read for symbol resolution, or when a resolved
    symbol's price source is in the catalog but unreadable on disk. The run() wiring catches it and
    reports the run AND the job failed, so the run never strands 'pending' (the mode
    materialize_event_slices wraps to prevent); no fallback to a partial mount set.
    """

# Host paths bind-mounted into the container, resolved relative to this file
# (<root>/research_lab/worker/jobs/), env-overridable — same convention as fetch_dataset.
_RESEARCH_LAB = Path(__file__).resolve().parents[2]
DATA_ROOT   = Path(os.environ.get("RESEARCH_DATA_ROOT", _RESEARCH_LAB / "data"))
NUMBA_CACHE = Path(os.environ.get("RESEARCH_NUMBA_CACHE", _RESEARCH_LAB / "numba-cache"))

# The six artifact filenames the entrypoint writes, keyed to the research_runs path columns.
_ARTIFACTS = {
    "equity_path":     "equity.parquet",
    "drawdowns_path":  "drawdowns.parquet",
    "trades_path":     "trades.parquet",
    "stats_path":      "stats.json",
    "indicators_path": "indicators.parquet",
    "meta_path":       "meta.json",
}


def container_to_host(container_path: str) -> Path:
    """Map a container-relative '/data/...' path to its host path under DATA_ROOT (arch §8).

    The same recovery rule M2 datasets use: replace the leading '/data' with the
    worker's bind-mount root. Guards the B.5 bug class (OPEN.md 2026-05-21) where a
    '/data/...' path was handed to the host filesystem unmapped.
    """
    prefix = "/data"  # the container mount root
    if not container_path.startswith(prefix + "/"):  # must be a /data/... path
        raise ValueError(f"not a container /data path: {container_path!r}")
    rel = container_path[len(prefix) + 1:]  # strip '/data/'
    return DATA_ROOT / rel  # rebase under the host data root


def container_artifact_paths(run_id: int) -> dict[str, str]:
    """The six container-relative artifact paths stored in research_runs (§3.4 / §3.8)."""
    base = f"/data/runs/{run_id}"  # container-relative run dir
    return {col: f"{base}/{name}" for col, name in _ARTIFACTS.items()}


def dataset_source(dataset_path: str) -> str:
    """The dataset's source subdir (e.g. 'binance') from its container path, for the :ro mount."""
    return container_to_host(dataset_path).relative_to(DATA_ROOT).parts[0]  # first segment under /data


def write_run_inputs(job_id: int, run_id: int, source_code: str,
                     dataset_path: str | None, settings: list[dict],
                     knowledge_from: int | None = None, knowledge_to: int | None = None) -> Path:
    """Write strategy.py + run_spec.json to /tmp/run_{job_id}/. Returns the tmp dir.

    strategy.py is the DB-versioned source verbatim (the container imports it);
    run_spec.json carries the run context the entrypoint reads (OD-2). `dataset_path`
    is None for an M3.5 event run (it has no base price dataset; EL72) and the run_spec
    records null — the entrypoint reads price from the per-symbol mounts instead (brief J1).

    For an EVENT run the caller passes `knowledge_from`/`knowledge_to`: the container
    entrypoint's `build_event_input` reads the run window off the spec
    (run_backtest_entrypoint.py:182), so it must be recorded here. A price run passes
    neither, leaving the spec byte-unchanged (R19 price-run invariant).
    """
    tmp_dir = Path(f"/tmp/run_{job_id}")  # per-job scratch dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "strategy.py").write_text(source_code, encoding="utf-8")  # byte-for-byte DB source
    run_spec = {"run_id": run_id, "dataset_path": dataset_path, "settings": settings}  # entrypoint input (dataset_path null for event runs)
    if knowledge_from is not None:  # event run: record the window the entrypoint's build_event_input reads (price run omits it -> spec byte-unchanged, R19)
        run_spec["knowledge_from"] = knowledge_from  # epoch s, inclusive window start
        run_spec["knowledge_to"] = knowledge_to      # epoch s, exclusive no-lookahead bound
    (tmp_dir / "run_spec.json").write_text(json.dumps(run_spec), encoding="utf-8")
    return tmp_dir


def prepare_run_dir(run_id: int) -> Path:
    """Pre-create the host run dir owned by uid 1000, mode 0755 (arch §7 pre-spawn step).

    The worker runs as uid 1000, so the dir is 1000-owned on creation; without
    pre-creating it Docker would make the mount root owned by root and the
    container user 1000 could not write artifacts into it. chmod is explicit
    because umask would otherwise clip the mode.
    """
    run_dir = DATA_ROOT / "runs" / str(run_id)  # host output dir
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o755)  # exact mode regardless of umask
    return run_dir


def build_docker_cmd(job_id: int, run_id: int, dataset_path: str | None, tmp_dir: Path,
                     event_mounts: list[str] | None = None) -> list[str]:
    """Assemble the docker run argv for one backtest (arch §7 mount profile, --network=none).

    `event_mounts` (M2.5 brief K / EL44) is an optional list of host:container:ro
    mount specs, one per event type, produced by materialize_event_slices. Each
    is inserted as a `-v <spec>` pair BEFORE the image — all `-v` flags must
    precede the image name in `docker run` argv, or they would be parsed as
    arguments to the container entrypoint instead of mounts. With no event_mounts
    the argv is byte-identical to the M3 five-mount profile (no behaviour change
    for price-only backtests; M3.5 supplies event_mounts).

    `dataset_path` is optional (M3.5 brief I / EL72; OPEN-6 no-base-dataset shape): a
    price run passes its container-relative /data/{source}/.../x.parquet and gets the
    base `/data/{source}:ro` mount; an EVENT run has no single base price dataset (its
    prices are the per-symbol slices carried in event_mounts, §7), so it passes None and
    the base mount is suppressed. With dataset_path set the argv is byte-identical to the
    pre-M3.5 profile (the M3 price-run path is unchanged, R19).
    """
    run_dir = DATA_ROOT / "runs" / str(run_id)  # host :rw output dir
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",                 # no network for backtests
        "--memory", "2g",
        "--cpus", "2",
        "--user", "1000:1000",
    ]
    if dataset_path is not None:             # base price dataset mount (price run); suppressed for an event run (EL72)
        source = dataset_source(dataset_path)  # e.g. 'binance'
        cmd += ["-v", f"{DATA_ROOT / source}:/data/{source}:ro"]   # dataset source, read-only — FIRST -v, as in the M3 profile
    cmd += [
        "-v", f"{run_dir}:/data/runs/{run_id}:rw",                # run artifacts, read-write
        "-v", f"{tmp_dir / 'strategy.py'}:/app/strategy.py:ro",   # DB-versioned strategy, read-only
        "-v", f"{tmp_dir / 'run_spec.json'}:/app/run_spec.json:ro",  # run context, read-only (OD-2)
        "-v", f"{NUMBA_CACHE}:/cache:rw",                         # shared numba cache
        "-e", "NUMBA_CACHE_DIR=/cache",
        "-e", "PYTHONUNBUFFERED=1",
        # Phase-2 Unit 4 (A5a-014): pin the hash seed so a user strategy that emits a set/dict
        # has deterministic iteration order across runs (reproducibility). Inert for the current
        # sorted-set strategies; load-bearing the moment an unsorted set/dict reaches an output.
        "-e", "PYTHONHASHSEED=0",
    ]
    for spec in (event_mounts or []):        # EL44: one :ro event-parquet mount per event type
        cmd += ["-v", spec]                  # inserted BEFORE the image (must precede it in the argv)
    cmd += [
        DOCKER_IMAGE,
        "python", "/app/run_backtest_entrypoint.py",
    ]
    return cmd


async def materialize_event_slices(run_id: int, slices: list[dict], http) -> list[str]:
    """Fan out one materializer call per (event_type, universe-slice) and stage each parquet (EL44, arch §9.4).

    M2.5 builds + unit-tests this; M3.5 owns the call site in run() (it owns the
    strategy data-declaration that produces `slices`). Each slice is a dict
    {event_type, ciks, knowledge_from, knowledge_to, include_status?}. For each:
    call the materializer endpoint, write the returned parquet to
    DATA_ROOT/events/{event_type}/{run_id}.parquet, write a {run_id}.meta.json
    sidecar (event count + content sha256 from the response headers, so a
    backtest's event slice is re-derivable per arch §9.4), and collect the
    host:container:ro mount spec. Returns the mount specs for
    build_docker_cmd(event_mounts=...).
    """
    import httpx  # function-scope: name the failed slice on a materializer error (EL71)
    from helpers import report_event_materialize  # function-scope import: worker import convention + patchable in the probe

    mounts: list[str] = []
    for sl in slices:
        event_type = sl["event_type"]                              # the single type for this slice
        include_status = sl.get("include_status", ["active"])      # default active-only (forensic is opt-in)
        try:
            parquet_bytes, headers = await report_event_materialize(   # one materializer POST per slice
                event_type, sl["ciks"], sl["knowledge_from"], sl["knowledge_to"],
                include_status, http,
                universe_id=sl.get("universe_id"),                 # M3.5 brief I: stamps membership_valid (EL65); None for M2.5 slices
            )
        except httpx.HTTPError as exc:                             # EL71: a partial fan-out fails the run with the failed slice NAMED
            raise EventMaterializeError(f"event materialization failed for event_type {event_type!r}: {exc}") from exc
        # Stage the returned slice. A disk failure (mkdir/write) or a 200 body missing a required
        # header (KeyError on x-event-count/x-content-sha256, or a non-int count -> ValueError)
        # must ALSO fail the run honestly (EL71: no fallback, no silently-skipped slice) rather than
        # escape unwrapped and leave the run stuck 'pending'. Wrap as EventMaterializeError naming the type.
        try:
            h = {k.lower(): v for k, v in headers.items()}        # httpx lowercases header keys; normalize before lookup
            et_dir = DATA_ROOT / "events" / event_type            # per-event-type host dir
            et_dir.mkdir(parents=True, exist_ok=True)             # create it (uid-1000-owned, like prepare_run_dir)
            (et_dir / f"{run_id}.parquet").write_bytes(parquet_bytes)  # the materialized slice
            meta = {                                              # arch §9.4 re-derivability sidecar
                "event_type": event_type,
                "run_id": run_id,
                "event_count": int(h["x-event-count"]),           # from the response header
                "content_sha256": h["x-content-sha256"],          # from the response header
                "source_series": h.get("x-source-series"),        # from the response header
            }
            (et_dir / f"{run_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        except (OSError, KeyError, ValueError) as exc:            # disk error, or a malformed/short 200 header set
            raise EventMaterializeError(f"event slice staging failed for event_type {event_type!r}: {exc}") from exc
        mounts.append(f"{et_dir}:/data/events/{event_type}:ro")    # the :ro mount spec for docker run
    return mounts


async def stage_fundamentals_slice(run_id: int, ciks: list[int], knowledge_to: int,
                                   concepts: list[str], http) -> list[str]:
    """Materialize + stage the ONE fundamentals fact-table slice and return its :ro mount spec (§6.2).

    The slice has NO lower filing_date bound (Fix 1): fundamentals are reference data read as-of each
    event, so the slice carries every filing up to ``knowledge_to`` (the no-lookahead ceiling) — a
    pre-window 10-K is still the latest-known at an in-window event. The real no-lookahead guard is the
    per-event ``filing_date < start_of_day(D)`` gate in ``as_of_select`` (the upper bound + that gate,
    never a lower window bound that would starve the as-of read).

    The fundamentals analogue of materialize_event_slices, but SINGLE-slice (one logical fact table,
    not one parquet per event type): call the FUND-2 materializer for (ciks, [0, knowledge_to), concepts), write
    the returned parquet to DATA_ROOT/fundamentals/facts/{run_id}.parquet, write a {run_id}.meta.json
    sidecar (fact count + content sha256 from the response headers, the §6.4 frozen-slice
    re-derivability record), and return the host:container:ro mount spec. The container reads it via
    the SAME generic read_run_slices reader (build_event_input, §8.4); build_docker_cmd needs no
    change (it is generic over the mount-spec list, §6.2). A materializer HTTP error, a disk failure,
    or a missing/short 200 header set fails the run loud (FundamentalsMaterializeError), never a
    partial mount (the EventMaterializeError discipline). Returns a one-element list so the caller can
    `event_mounts += stage_fundamentals_slice(...)`.
    """
    import httpx  # function-scope: name the failure on a materializer error (the worker import convention)
    from helpers import report_fundamentals_materialize  # function-scope: patchable in the probe

    try:
        parquet_bytes, headers = await report_fundamentals_materialize(  # one materializer POST (§6.2)
            ciks, 0, knowledge_to, concepts, http)             # lower bound 0 — reference data has no window floor (Fix 1)
    except httpx.HTTPError as exc:  # a slow/unreachable store or a non-2xx fails the run with no fallback
        raise FundamentalsMaterializeError(f"fundamentals materialization failed: {exc}") from exc
    # Stage the returned slice. A disk failure (mkdir/write) or a 200 body missing a required header
    # (KeyError on x-fact-count/x-content-sha256, or a non-int count -> ValueError) must ALSO fail the
    # run honestly (no fallback, no silently-skipped slice), the materialize_event_slices discipline.
    try:
        h = {k.lower(): v for k, v in headers.items()}        # httpx lowercases header keys; normalize before lookup
        facts_dir = DATA_ROOT / "fundamentals" / "facts"      # the single fact-table host dir (one subdir, §6.2)
        facts_dir.mkdir(parents=True, exist_ok=True)          # create it (uid-1000-owned, like prepare_run_dir)
        (facts_dir / f"{run_id}.parquet").write_bytes(parquet_bytes)  # the materialized fact-table slice
        meta = {                                              # §6.4 re-derivability sidecar
            "run_id": run_id,
            "fact_count": int(h["x-fact-count"]),             # from the response header
            "content_sha256": h["x-content-sha256"],          # from the response header
        }
        (facts_dir / f"{run_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    except (OSError, KeyError, ValueError) as exc:            # disk error, or a malformed/short 200 header set
        raise FundamentalsMaterializeError(f"fundamentals slice staging failed: {exc}") from exc
    return [f"{facts_dir}:/data/fundamentals/facts:ro"]        # the :ro mount spec (read by read_run_slices, §8.4)


def resolved_symbols_from(run_id: int, event_types: list[str]) -> list[str]:
    """Read the distinct resolved trade-time symbols off this run's staged event slices (M3.5 brief I, §8).

    For each event type, read exactly DATA_ROOT/events/{event_type}/{run_id}.parquet (this
    run's slice, written by materialize_event_slices) and collect the distinct
    `trade_time_symbol` values whose `symbol_coverage` is 'resolved' (brief B / EL64). The
    symbols come from the parquet columns, NOT the mount-spec strings (which carry only the
    event type, no symbol). An unresolved/None symbol is skipped here and rides the §5
    coverage figure, never a silent price-skip. Feeds provision_price_slices (§7). Returns a
    deterministically-ordered list (sorted) so the price-mount set is reproducible.

    Built-but-not-wired in brief I (Q2=B; DECISIONS #46 pattern): brief I never calls this, so brief I
    has no pyarrow runtime dependency. Its consumer is the deferred price-provisioning brief, which owns
    adding pyarrow to the worker venv + requirements as a ship pre-condition (OPEN.md, this session). The
    pyarrow import is function-scoped so importing this module stays light on a plain interpreter.
    """
    import pyarrow.parquet as pq  # function-scope: provisioned by the price brief that wires this (OPEN.md)

    symbols: set[str] = set()
    for event_type in event_types:
        path = DATA_ROOT / "events" / event_type / f"{run_id}.parquet"  # exactly this run's slice
        table = pq.read_table(path, columns=["trade_time_symbol", "symbol_coverage"])
        syms = table.column("trade_time_symbol").to_pylist()
        covs = table.column("symbol_coverage").to_pylist()
        for sym, cov in zip(syms, covs):
            if cov == "resolved" and sym:  # only resolved, non-null symbols get a price slice (§7)
                symbols.add(sym)
    return sorted(symbols)


def resolved_symbols_or_fail(run_id: int, event_types: list[str]) -> list[str]:
    """Read the staged slices' resolved symbols, converting a read/import fault into a named error.

    M3.5 price-provisioning brief (DECISIONS #63), the Cowork should-fix: resolved_symbols_from does a
    function-scoped pyarrow read of the staged slice, which can raise OSError (a missing/partial file),
    ArrowInvalid (a corrupt parquet — a subclass of ValueError in pyarrow 24.0.0), or
    ModuleNotFoundError (the pyarrow ship pre-condition skipped). Any of these MUST fail the run, not
    escape: the worker loop handle_job catches Exception and reports only the JOB failed, never the run,
    so an escaped read error would strand the run 'pending' (the mode materialize_event_slices wraps to
    prevent). The catch is specific (no catch-all) and needs no module-top pyarrow import.
    """
    try:
        return resolved_symbols_from(run_id, event_types)  # the brief-I helper, unchanged
    except (OSError, ValueError, ModuleNotFoundError) as exc:  # missing/partial file, corrupt parquet, absent pyarrow
        raise PriceProvisionError(
            f"reading the staged slice for price symbol resolution failed (run {run_id}): {exc}") from exc


async def provision_price_slices(run_id: int, symbols: list[str], http) -> list[str]:
    """Mount each resolved symbol's ready daily parquet :ro and return the mount specs (M3.5 price brief, EL70).

    Worker-local-superset model: the worker already holds the parquet; it bind-mounts the existing
    immutable catalog file directly, read-only, AT the {run_id}.parquet container leaf (so brief J1's
    §9 read-by-run_id reader finds it) — zero copy, no per-run write (a ready price dataset is
    immutable and run-independent). A symbol with no ready dataset gets no mount and rides the
    coverage figure (never a silent skip). A resolved symbol whose host file is missing/unreadable
    fails the run loud, the symbol named (no fallback, EL71). Bytes never move over the network; only
    file_path is resolved over HTTP (the worker has no DB).
    """
    from urllib.parse import quote               # function-scope: filesystem-safe symbol encoding (the §7 scheme)
    from helpers import resolve_price_slices      # function-scope import: worker import convention + patchable in the probe

    if not symbols:                               # zero resolved symbols -> no price mounts
        return []
    resolution = await resolve_price_slices(symbols, http)   # HTTP metadata only: {resolved:{sym:path}, unresolved:[...]}
    mounts: list[str] = []
    for symbol in sorted(resolution["resolved"]):            # sorted -> deterministic mount order; keyed by symbol (dedup)
        container_path = resolution["resolved"][symbol]      # container-relative /data/... from the catalog
        try:
            host = container_to_host(container_path)         # /data/... -> DATA_ROOT/... (the arch §8 map)
        except ValueError as exc:                            # a malformed catalog path (not container-relative) -> fail loud, named (inside the boundary, B1)
            raise PriceProvisionError(
                f"price provisioning failed for symbol {symbol!r}: bad catalog path {container_path!r}: {exc}") from exc
        if not host.is_file():                               # catalog says ready but the file is gone/unreadable on disk
            raise PriceProvisionError(                       # fail loud, name the symbol (EL71; outcome 3)
                f"price provisioning failed for symbol {symbol!r}: file not readable at {host}")
        enc = quote(symbol, safe="")                         # reversible per-char escape; brief J1 decodes with unquote
        mounts.append(f"{host}:/data/price/{enc}/{run_id}.parquet:ro")  # direct file mount AT the run_id leaf, read-only
    return mounts


def read_run_outputs(run_id: int) -> tuple[dict, dict]:
    """Read stats.json + meta.json off the host run dir (mapping /data → DATA_ROOT)."""
    base = f"/data/runs/{run_id}"  # container-relative
    stats = json.loads(container_to_host(f"{base}/stats.json").read_text(encoding="utf-8"))
    meta = json.loads(container_to_host(f"{base}/meta.json").read_text(encoding="utf-8"))
    return stats, meta


async def _stream_and_watch(proc, job_id: int, http, sink: dict) -> None:
    """Stream container stdout as logs AND poll the heartbeat for cancellation, concurrently.

    sink["last_json_line"] gets the entrypoint's final JSON line; sink["cancelled"] flips True
    if a cancel was requested mid-run, after the SIGTERM -> grace -> SIGKILL sequence (brief I).
    """
    import httpx  # function-scope: the watcher catches only a transport error (DBG-4); anything else fails the job
    from helpers import log_event, report_heartbeat, report_log

    async def stream_logs() -> None:
        async for raw in proc.stdout:  # ends when the container closes stdout (i.e. it exits)
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            await report_log(job_id, line, http)
            if line.startswith("{") and line.endswith("}"):
                sink["last_json_line"] = line

    async def watch_cancel() -> None:
        while proc.returncode is None:  # until the process exits
            await asyncio.sleep(CANCEL_POLL_S)
            if proc.returncode is not None:
                return
            try:
                requested = await report_heartbeat(job_id, http)  # cancel signal rides the heartbeat (EI-2)
            except httpx.HTTPError as exc:  # a transient transport failure: logged, and the next poll retries
                log_event("cancel_watch_heartbeat_failed", job_id=job_id, error=repr(exc))
                continue
            # Any other error is not transient. It propagates out of this watcher, and _stream_and_watch
            # stops the process and fails the job (DBG-4), so a broken watcher never leaves a run unwatched.
            if requested:
                sink["cancelled"] = True
                try:
                    proc.terminate()  # SIGTERM (docker run forwards it to the container)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=CANCEL_GRACE_S)
                    except asyncio.TimeoutError:
                        proc.kill()  # SIGKILL after the grace window (L6)
                except ProcessLookupError:
                    pass  # the container exited between the returncode check and the signal — fine
                return

    log_task = asyncio.create_task(stream_logs())
    watch_task = asyncio.create_task(watch_cancel())
    try:
        # DBG-4: stop waiting as soon as either task ends. A watcher error must not wait behind a process
        # that never exits (stdout would never close), so it is checked first.
        await asyncio.wait({log_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        if watch_task.done() and not watch_task.cancelled() and watch_task.exception() is not None:
            watcher_error = watch_task.exception()
            if proc.returncode is None:  # the process has no cancel path now: stop it before failing the job
                proc.kill()
            log_task.cancel()
            await asyncio.gather(log_task, return_exceptions=True)
            raise watcher_error
        await log_task            # returns once the process exits and stdout drains
    finally:
        # CQ-SF1 (scale-defect build, symmetric with run_sweep): if stream_logs RAISED, still cancel the
        # cancel-watcher AND kill the container, so a container with no stdout reader can never hang holding a
        # docker slot. On the normal drain path the process has already exited, so the kill no-ops.
        watch_task.cancel()       # stop polling; the process is done (or being killed)
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        if proc.returncode is None:  # still running with no stdout reader -> kill it (never leak a container)
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # it exited between the check and the signal — fine


async def run(job: dict, http) -> None:
    # Lazy imports keep this module importable on a plain interpreter for the local probe.
    import httpx  # for the precise upload-failure exception type below
    from helpers import (
        fail_run_and_job,
        report_job_cancelled,
        report_job_completed,
        report_job_failed,
        report_run_artifacts,
        report_run_cancelled,
        report_run_failed,
        report_run_result,
        resolve_universe,            # M3.5 brief I (§4): event-run universe -> CIK union
    )

    job_id = job["id"]
    p = job["payload"]
    run_id = p["run_id"]                  # from the payload — the worker has no DB to resolve it from
    source_code = p["source_code"]        # DB-versioned strategy source, embedded by the backend
    dataset_path = p["dataset_path"]      # container-relative /data/{source}/.../x.parquet, or None for an event run
    settings = p["settings"]              # list of {scope, key, value, value_type}

    tmp_dir = write_run_inputs(job_id, run_id, source_code, dataset_path, settings,
                               p.get("knowledge_from"), p.get("knowledge_to"))  # event runs carry the window; the entrypoint reads it off run_spec (build_event_input)
    prepare_run_dir(run_id)

    try:
        # M3.5 brief I (§8 / EL71, EL72): an event run carries the data declaration (universe +
        # window + event_types) instead of a base price dataset. Resolve the universe to a CIK
        # union (§4), pre-materialize one event slice per type (the seam writes meta.json + the
        # membership_valid flag, EL65), and build the docker command with those :ro event mounts
        # and dataset_path=None (no base /data/{source} mount). An empty union over the window is a
        # reported zero-event run, short-circuited before any materializer call so it never trips
        # the materializer's ciks_empty 400. Per-symbol PRICE provisioning is deferred to its own
        # pre-J1 brief (the worker-local-superset transport, also serving the D2 preview need); only
        # that call defers — this wiring, the short-circuit, and the failure semantics are brief I.
        event_types = p.get("event_types")   # present (a non-empty list) iff this is an event run
        if event_types is not None:
            # M3.75 FUND-4 §9: pass the strategy's universe screen through (present iff declared, the
            # dispatch populates it). The server post-filters the resolved union to the companies
            # whose as-known-on-D fundamental satisfies the screen. None for every existing run, so
            # the resolved CIK set is byte-unchanged (NFR-6).
            fundamental_screen = p.get("fundamental_screen")  # dict {concept, op, threshold}, or None
            try:
                ciks = await resolve_universe(
                    p["universe_id"], p["knowledge_from"], p["knowledge_to"], http, fundamental_screen,
                    members_as_of=p.get("members_as_of"))  # DL1: pinned at dispatch for a price universe, else None
            except httpx.HTTPError as e:
                # EL71: the universe store is slow/unreachable -> fail the run BEFORE any container.
                # Named distinctly from a materialize failure so the operator sees the step that failed.
                error = f"universe resolution failed: {e}"
                await report_run_failed(run_id, error, http)
                await report_job_failed(job_id, error, http)
                return
            if not ciks:                      # empty union over the window -> zero-event run, NO materializer call
                event_mounts: list[str] = []
            else:
                slices = [
                    {"event_type": et, "ciks": ciks, "universe_id": p["universe_id"],  # universe_id -> membership_valid (§3.2)
                     "knowledge_from": p["knowledge_from"], "knowledge_to": p["knowledge_to"]}
                    for et in event_types
                ]
                try:
                    event_mounts = await materialize_event_slices(run_id, slices, http)  # EL-c6 seam (writes meta.json), pre-materialize (EL71)
                except EventMaterializeError as e:
                    # EL71: a slow/unreachable store, a partial fan-out, or a staging failure (the failed
                    # slice is NAMED in e — the seam wraps every per-slice failure as EventMaterializeError)
                    # fails the run BEFORE any container, with no fallback to a partial mount set.
                    error = f"event data materialization failed: {e}"
                    await report_run_failed(run_id, error, http)
                    await report_job_failed(job_id, error, http)
                    return
                # Price provisioning (M3.5 price-provisioning brief, DECISIONS #63): read the resolved
                # symbols off the just-staged slices and mount each symbol's ready daily parquet :ro.
                # BOTH the staged-slice read AND the provisioning sit INSIDE the failure boundary — a
                # read/import fault (resolved_symbols_or_fail), an unreadable resolved source, or a
                # resolve-HTTP failure fails the RUN before the container, the step named, no fallback
                # (EL71), never a stuck-'pending' run (the worker loop fails only the job). Symmetric
                # with the materialize wrapping above.
                try:
                    symbols = resolved_symbols_or_fail(run_id, event_types)          # staged-slice read, inside the boundary
                    event_mounts += await provision_price_slices(run_id, symbols, http)  # extend with per-symbol :ro price mounts
                except (PriceProvisionError, httpx.HTTPError) as e:
                    error = f"price provisioning failed: {e}"
                    await report_run_failed(run_id, error, http)                     # fail the RUN (not just the job)
                    await report_job_failed(job_id, error, http)
                    return
                # M3.75 FUND-3 worked consumption (§6.2): when the dispatch payload carries the
                # strategy's fundamental concept set (FUND-4 dispatch populates `fundamental_concepts`
                # iff the strategy declares FundamentalFeatureDefs, the analogue of `event_types`),
                # materialize the fundamentals fact-table slice over the SAME resolved CIK union + window
                # and append its :ro mount. INSIDE the failure boundary, symmetric with the materialize /
                # price steps: a materializer/staging fault fails the RUN before the container, named, no
                # fallback. A run with no `fundamental_concepts` (every price / event-only run) skips this
                # entirely -> the event_mounts and the docker argv are byte-unchanged (NFR-6).
                fundamental_concepts = p.get("fundamental_concepts")  # list of concept names, or None
                if fundamental_concepts:
                    try:
                        # Fix 1: the fundamentals reference slice has no lower filing_date bound (it
                        # needs every filing up to knowledge_to) — encapsulated in stage_fundamentals_slice.
                        # A run with no fundamental_concepts skips this entirely (NFR-6, byte-unchanged).
                        event_mounts += await stage_fundamentals_slice(
                            run_id, ciks, p["knowledge_to"], fundamental_concepts, http)
                    except FundamentalsMaterializeError as e:
                        error = f"fundamentals materialization failed: {e}"
                        await report_run_failed(run_id, error, http)
                        await report_job_failed(job_id, error, http)
                        return
            cmd = build_docker_cmd(job_id, run_id, dataset_path=None, tmp_dir=tmp_dir, event_mounts=event_mounts)
        else:
            cmd = build_docker_cmd(job_id, run_id, dataset_path, tmp_dir)  # M3 price run, byte-unchanged (R19)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=STDOUT_STREAM_LIMIT,  # 1.2 defense-in-depth: raise the 64 KB readline ceiling (entrypoint byte-bounds lines)
        )
        sink = {"last_json_line": None, "cancelled": False}
        await _stream_and_watch(proc, job_id, http, sink)
        rc = await proc.wait()

        if sink["cancelled"]:
            # Cancellation (brief I): the backend NULLs the artifact paths + finalizes the run;
            # the worker drops any partial bytes the container flushed before SIGKILL.
            await report_run_cancelled(run_id, http)
            await report_job_cancelled(job_id, http)
            shutil.rmtree(DATA_ROOT / "runs" / str(run_id), ignore_errors=True)
            return

        if rc != 0:
            # Surface the container's own error message if it emitted one, else the exit code.
            error = f"container exited {rc}"
            if sink["last_json_line"]:
                try:
                    error = json.loads(sink["last_json_line"]).get("error", error)
                except json.JSONDecodeError:
                    pass
            await report_run_failed(run_id, error, http)
            await report_job_failed(job_id, error, http)
            return

        # Success: read what the container authored, copy it into the report (no vbt here).
        stats, meta = read_run_outputs(run_id)

        # K.0: upload the run artifacts to the server BEFORE reporting the result, so a run
        # never reaches 'succeeded' without its bytes in the server store (the K.0 invariant).
        # The parquets that exist on disk are sent — indicators.parquet is optional (§4.6).
        # meta.json is added UNCONDITIONALLY (C2.5 D1): the entrypoint always writes it (§12), and
        # it carries the regime bands + an event run's universe window/types the bundle reads — so
        # it is mandatory server-side (C2.5 D2). If it were somehow absent, read_bytes raises OSError
        # below and the run fails loud (correct: no succeeded-without-its-metadata lie).
        # An upload failure fails the run (honest: no server-visible result) rather than leaving
        # a succeeded-without-bytes lie; the laptop artifacts remain for inspection / re-dispatch.
        run_dir = DATA_ROOT / "runs" / str(run_id)
        artifacts = {
            name: run_dir / name
            for name in ("equity.parquet", "drawdowns.parquet", "trades.parquet",
                         "price.parquet", "indicators.parquet")
            if (run_dir / name).is_file()
        }
        artifacts["meta.json"] = run_dir / "meta.json"  # C2.5 D1: always written (F5), mandatory in the store
        try:
            await report_run_artifacts(run_id, artifacts, http)
        except (httpx.HTTPError, OSError) as e:
            error = f"artifact upload failed: {e}"
            await report_run_failed(run_id, error, http)
            await report_job_failed(job_id, error, http)
            return

        result = {
            "status": "succeeded",
            "run_id": run_id,
            "artifacts": container_artifact_paths(run_id),  # six container-relative paths
            "stats": stats,                                 # eight research_run_stats keys
            "environment": meta["environment"],             # seven fields since #376 ruling 4, copied verbatim (OD-7)
            "annualization": meta.get("annualization"),     # Phase-2 Unit 1 (price, index-derived) + Unit 7 (event, fixed daily-252); .get -> None only for legacy/error
            "funnel": meta.get("funnel"),                   # Phase-2 Unit 3: price-run signal funnel; .get -> None for event/bespoke runs
            "variation": meta.get("variation"),             # lane DG (`6.1g`): the single run's own combo; .get -> None for an undeclared strategy
        }
        await report_run_result(run_id, result, http)
        await report_job_completed(job_id, {"run_id": run_id}, http)
    except Exception as e:  # noqa: BLE001 — 1.1 safety net: an unexpected fault (e.g. the K.0 result-POST 409 of scale-defect 2, or any unhandled error) must FAIL THE RUN loudly, never strand it 'running'
        await fail_run_and_job(run_id, job_id, f"unexpected backtest worker error: {type(e).__name__}: {e}", http)
        return  # the finally below still runs (rmtree the scratch)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # drop the per-job scratch (strategy.py + run_spec.json)
