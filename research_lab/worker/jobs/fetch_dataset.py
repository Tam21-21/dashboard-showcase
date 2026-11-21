"""Job handler: fetch_dataset.

Spawns research-runner:m2-latest with the requested source/symbol/timeframe/range.
Streams every container stdout line as a research.job_log event. Last line
starting with { and ending with } is parsed as the result JSON. On success
posts to /worker/datasets/{id}/result; on failure posts to /worker/datasets/{id}/failed and fails
the job with the same reason. A fetch that returns 0 rows is a failure.
"""
import asyncio
import json
import os
from pathlib import Path

import httpx

from helpers import (
    heartbeat_loop,
    report_dataset_failed,
    report_dataset_preview,
    report_dataset_ready,
    stop_heartbeat,
    report_job_completed,
    report_job_failed,
    report_log,
)
from pathmap import to_host_path  # /data->host mapping (Brief B, D-1); pathlib-only shared module
from store_root import resolve_eodhd_store_root, resolve_synthetic_store_root, report_store_grains  # M5 fetcher job 10 AC-1 + monitor mode 1; GV2-4a synthetic store
import synthetic_fetch  # GV2-4a: the made-up stocks are converted on the host, no container
from store_identity import compute_store_identity  # M5 fetcher job 10 AC-7: a comparable store identity

DOCKER_IMAGE = "research-runner:m2-latest"
# Host paths bind-mounted into the container. Resolved relative to this file
# (<root>/research_lab/worker/jobs/) so the worker runs regardless of where the
# repo is checked out; env-overridable for non-default layouts.
_RESEARCH_LAB = Path(__file__).resolve().parents[2]
DATA_ROOT    = Path(os.environ.get("RESEARCH_DATA_ROOT", _RESEARCH_LAB / "data"))
NUMBA_CACHE  = Path(os.environ.get("RESEARCH_NUMBA_CACHE", _RESEARCH_LAB / "numba-cache"))
# M5 fetcher job 10: the single container mount point for the WHOLE consolidated EODHD store root.
# Mounted ONCE, read-only, and every grain the container's own adapters read (daily, hourly, and any
# future grain) is a subdirectory INSIDE this one mount -- the design constraint this job names
# directly: "mount the store root once and let the grain select inside it, rather than adding a
# second single-purpose mount per grain." Replaces the old single-purpose daily-only mount point this
# file used to bind, which covered one grain only and had no hourly counterpart at all.
_EODHD_STORE_MOUNT = "/eodhd-store"


def _container_failure_reason(rc: int, last_json_line: str | None) -> str:
    """The reason a non-zero container exit is reported with.

    The container's own `error` text when its last JSON line carries one (a data-check refusal does),
    else the exit code. A last line that is not JSON (a traceback) has no error text of its own, so
    the exit code is the reason; the full output is already in the job's log stream.
    """
    if last_json_line is not None:
        try:
            parsed = json.loads(last_json_line)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, str) and error:
                return error
    return f"docker exited {rc}"


async def run(job: dict, http: httpx.AsyncClient) -> None:
    job_id     = job["id"]
    p          = job["payload"]
    dataset_id = p["dataset_id"]

    async def _fail(reason: str) -> None:
        # DBG-8: a failed fetch fails the dataset AND the job, with the same reason. Before this, the
        # job was never reported and stayed picked_up until a person noticed.
        await report_dataset_failed(dataset_id, reason, http)
        await report_job_failed(job_id, reason, http)

    # Pass Alpaca creds unconditionally; fetch.py only reads them when
    # source=alpaca_sip. Binance ignores them.
    alpaca_key    = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY", "")

    cmd = [
        "docker", "run", "--rm",
        "--network", "bridge",
        "--memory", "2g",
        "--cpus", "2",
        "--user", "1000:1000",
        "-v", f"{DATA_ROOT}:/data:rw",
        "-v", f"{NUMBA_CACHE}:/cache:rw",
        "-e", "NUMBA_CACHE_DIR=/cache",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", f"ALPACA_API_KEY={alpaca_key}",
        "-e", f"ALPACA_SECRET_KEY={alpaca_secret}",
    ]
    # M5 fetcher job 10: an eodhd fetch reads the laptop's consolidated store root, bind-mounted
    # read-only ONCE (AC-1). Resolve it at dispatch (env-overridable via EODHD_STORE_ROOT, see
    # store_root.py) and FAIL LOUD if it is absent: else docker would create an empty mount and every
    # symbol would miss with a confusing container-side error. AC-7: this run's own store identity (a
    # hash a later reader can compare, plus the store's own newest-file fetch date) is computed from
    # the SAME resolved root, so the saved record can later name the exact data version this run
    # opened; kept None for a non-eodhd source, which never reads this store at all.
    store_identity_for_result = None
    if p["source"] == "eodhd":
        eodhd_store_root = resolve_eodhd_store_root()
        if not eodhd_store_root.is_dir():
            await _fail(
                f"eodhd store not found at {eodhd_store_root} (set EODHD_STORE_ROOT before starting the worker)"
            )
            return
        # Both store walks run off the event loop (lane W1): with job slots, several fetches share this
        # loop, and a blocking walk would stall their heartbeats and log streams.
        await asyncio.to_thread(report_store_grains, eodhd_store_root)  # ops monitor mode 1: status, printed every dispatch
        # ONE mount for the whole store; both grains the container's adapters read (fetch_eodhd's own
        # daily path, eodhd_hourly's hourly path) are subdirectories inside this single mount, each
        # pointed at by its own already-established container-side env var name (unchanged: only what
        # these env vars are SET TO changes here, never how the container reads them).
        cmd += [
            "-v", f"{eodhd_store_root}:{_EODHD_STORE_MOUNT}:ro",
            "-e", f"EODHD_DAILY_ROOT={_EODHD_STORE_MOUNT}/daily",
            "-e", f"EODHD_HOURLY_ROOT={_EODHD_STORE_MOUNT}/1h",
            "-e", f"EODHD_HOURLY_JOINED_ROOT={_EODHD_STORE_MOUNT}/1h_joined",
        ]
        store_identity_for_result = await asyncio.to_thread(compute_store_identity, eodhd_store_root)
    cmd += [
        DOCKER_IMAGE,
        "python", "-u", "/app/fetch.py",
        "--source",    p["source"],
        "--symbol",    p["symbol"],
        "--timeframe", p["timeframe"],
        "--start",     p["start"],
        "--end",       p["end"],
        "--out-root",  "/data",
    ]

    # W8 incremental "Update to latest": the docker arg array is otherwise hardcoded to the five
    # fixed payload keys, so new keys would land in `p` but never reach the container and the run
    # would silently full-fetch the extended window. Forward --existing / --mode ONLY when the
    # payload carries them, so a plain re-fetch is unchanged and an incremental job actually runs
    # incremental. The host path is already a container path (researchdb stores /data/... paths).
    if p.get("existing"):
        cmd += ["--existing", p["existing"]]
    if p.get("mode"):
        cmd += ["--mode", p["mode"]]

    # GV2-4a: a synthetic fetch is a file conversion on the host (synthetic_fetch.fetch_to_store), so no
    # container starts; `cmd` above is then unused. Every other source starts its container exactly as before.
    if p["source"] == "synthetic":
        proc = None
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    # Heartbeat concurrently with the container while it runs (spec brief §3.1). Not starved by
    # this handler's work: both awaits below (`async for` over stdout, then `proc.wait()`) yield to
    # the event loop. Step 0a measured 1.006 s median against a 1.0 s ask.
    heartbeat_task = asyncio.create_task(heartbeat_loop(job_id, "fetch_dataset", http))
    # The heartbeat covers this handler's WHOLE body, not just the container phase. An earlier
    # version of this build closed the finally immediately after `proc.wait()`, which left the
    # reporting tail below unheartbeated -- and that tail contains the preview upload, which POSTs
    # a whole parquet ("a few MB" per its own comment) over the link the crash-loop unit measured
    # as unreliable. A span like that can outlast the stale threshold, so the job would have read
    # as stranded while it was healthy: the false-positive class that FAILS a live job, since the
    # sweep never re-queues. Every `return` below still runs the finally.
    try:
        if proc is None:
            # GV2-4a: the synthetic path. Reads <SYNTHETIC_STORE_ROOT>/<symbol>.parquet and writes the dataset
            # file under DATA_ROOT, in a thread so the heartbeat keeps running. A refusal (ValueError) or a
            # file problem (OSError) fails the dataset and the job with the cause as its reason.
            try:
                synthetic_store_root = resolve_synthetic_store_root()
                result = await asyncio.to_thread(
                    synthetic_fetch.fetch_to_store, p["symbol"], p["timeframe"], p["start"], p["end"],
                    synthetic_store_root, DATA_ROOT, p.get("mode") or "fetch",
                )
            except (ValueError, OSError) as exc:
                await _fail(f"synthetic fetch failed: {exc}")
                return
        else:
            last_json_line = None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                await report_log(job_id, line, http)
                if line.startswith("{") and line.endswith("}"):
                    last_json_line = line

            rc = await proc.wait()

            if rc != 0:
                await _fail(_container_failure_reason(rc, last_json_line))
                return
            if last_json_line is None:
                await _fail("no result JSON from container")
                return
            try:
                result = json.loads(last_json_line)
            except json.JSONDecodeError as e:
                await _fail(f"invalid result JSON: {e}")
                return
            if "error" in result:
                await _fail(result["error"])
                return
        # DBG-8: a fetch that wrote no rows is a failure with its own reason, never a ready dataset.
        # Before this, the 0-row result reached report_dataset_ready, the route refused it with 422
        # (DatasetWorkerResultIn.rows has ge=1), and the dataset stayed pending forever.
        if result.get("rows") == 0:
            await _fail("the fetch returned 0 rows for this window")
            return

        # AC-7 (M5 fetcher job 10): stamp this run's own store identity into the result before it is
        # saved, so a later reader can name the exact data version this fetch opened and compare it
        # against another run's own identity. Present only for an eodhd fetch, the store this job is
        # about; absent for alpaca_sip/binance, which never read this store at all.
        if store_identity_for_result is not None:
            result["store_identity"] = store_identity_for_result
            # RA-5 gate-review SHOULD-FIX 2 (2026-09-04): AC-7's first half is satisfied and probed
            # right here, this fetch job DOES compute and stamp the identity. Its second half, DURABLE
            # persistence into the saved run record so a later reader can query it back (rather than
            # only ever seeing it in this job's own log stream below), is NOT reachable yet:
            # DatasetWorkerResultIn (dashboard/backend/routes/research.py) has no store_identity
            # field, that model is a plain BaseModel that silently drops any unknown key (see its own
            # comment beside covered_start_ts/covered_end_ts), and the raw SQL UPDATE behind it does
            # not name the key either -- so this dict key is set here, forwarded over HTTP, and
            # dropped on arrival. Adding a column or a model field is a schema change, so this build
            # still does not do it here. The SHAPE of that change is RULED; WHICH DATABASE is not, and
            # that half IS now an OPEN.md entry, added by the lead at night eight's wrap on 2026-09-04
            # (`grep -c store_identity OPEN.md` returns 4; it returned 0 when this comment was first
            # written, and this unit's own citation probe went red the moment the entry landed, which
            # is that probe working rather than failing): DECISIONS.md #330
            # decision 1 (`grep -n "the store identity in job 10 alike" DECISIONS.md` -> line 8290)
            # says "the store identity in job 10 alike, is additive, idempotent, on the table that
            # already owns that key" -- one additive column, written in the same transaction as
            # the value's own computation, migration idempotent, DDIA read before the build.
            # NOT YET NAMED: which table. Decision 1's own worked example is the fetcher's per-ticker
            # `priority` table (research_lab/fetcher/manifest.py, keyed by ticker alone).
            # store_identity does not vary by ticker -- it is a whole-store fact (store_identity.py's
            # own docstring), the same value for every ticker fetched at one moment -- stamped once
            # per fetch call onto that call's own row. The table that owns THAT row is
            # `research_datasets` (dashboard/backend/researchdb.py, a different database), keyed by
            # (source, symbol, timeframe, start_ts, end_ts), not `priority`. Applying decision 1's
            # ruling to that table by name is the one step left; the shape is already settled.
            # ops monitor mode 5 (what-can-break: "the store identity does not move when the store
            # does"): the identity recorded per run, logged here so it is visible in this job's own
            # log stream even though the backend's dataset-result schema has no column for it yet.
            # A later reader comparing two runs' logs can already see whether the two store_id values
            # differ.
            await report_log(
                job_id,
                f"store identity for this fetch: store_id={store_identity_for_result['store_id']} "
                f"store_fetched_at={store_identity_for_result['store_fetched_at']}",
                http,
            )

        await report_dataset_ready(dataset_id, result, http)

        # Eager dataset-preview upload (Brief B, R9): resolve the container /data path to the worker's
        # host path and POST the full parquet to the server's artifact store so the dashboard can chart
        # it. BEST-EFFORT — ordered AFTER report_dataset_ready and its specific failure surface swallowed,
        # so a failed upload never fails an already-ready dataset (the read endpoint then shows the honest
        # available:false state, which the next re-fetch regenerates). Fires on every success path here
        # (initial fetch, re-fetch, W8 update-to-latest) because all three run this job; result["file_path"]
        # always names the surviving (possibly window-renamed) file, so no branch on mode is needed.
        host_parquet = to_host_path(result["file_path"], DATA_ROOT)
        try:
            await report_dataset_preview(dataset_id, host_parquet, http)
        except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:  # POST 4xx/5xx + connect/timeout + the local read
            await report_log(job_id, f"preview upload failed for dataset {dataset_id}: {exc!r}", http, level="warning")
    finally:
        # Stop heartbeating once this handler is done, on EVERY exit path above (the four early
        # returns, the success fall-through, and any raise alike): a task that outlives the handler
        # would keep posting for a job about to report terminal, which is what the server-side
        # `worker_heartbeat_after_terminal` monitor exists to catch.
        await stop_heartbeat(heartbeat_task, job_id, "fetch_dataset")

    # Reported AFTER the heartbeat has stopped: a heartbeat still in flight when the job goes
    # terminal is what the server's `worker_heartbeat_after_terminal` monitor flags, so the terminal
    # report is the one call deliberately left outside the heartbeat's span. It is a single small
    # POST, not the multi-megabyte upload that made the rest of this tail need covering.
    #
    # The four handlers DIVERGE here and the divergence is argued rather than accidental, because an
    # earlier version of this comment claimed "the same order every other handler uses" and that was
    # simply false. THREE report outside the span: `fake`, this handler, and `hard_delete_dataset`.
    # ONE reports INSIDE it, `compute_run_metrics`, because its terminal report sits inside the
    # handler's own exception net (it is wrapped by `except Exception` with its own failure report,
    # and its `finally` also removes the scratch dir), and hoisting it out would change which net
    # catches a failed report -- a behaviour change well outside this step.
    #
    # `hard_delete_dataset` USED to be the second inside case and was hoisted out by this loop's own
    # code-quality fold, on the finding that it has no exception net at all -- its justification had
    # been borrowed from `compute_run_metrics` and was simply false there. This sentence is being
    # corrected for the second time in one loop, which is the point worth carrying: a comment that
    # describes ANOTHER file's control flow goes stale when that file changes, and nothing mechanical
    # catches it. The residual cost of
    # reporting inside the span is a sub-millisecond race between the terminal write committing and
    # the cancel below, against a 10 s cadence, whose only consequence is an occasional benign line
    # on the after-terminal monitor. Recorded here and on that monitor's registry row rather than
    # silently normalised.
    await report_job_completed(job_id, {"dataset_id": dataset_id}, http)
