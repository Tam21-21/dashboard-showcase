"""Job handler: run_sweep (M5 1B §5/§6 — the worker side of the sweep engine).

The sibling of run_backtest.run for a universe-wide, parameter-grid sweep. Like run_backtest the worker
NEVER imports vbt and NEVER touches a portfolio: it marshals the sweep spec in, spawns
research-runner:run_sweep_entrypoint, streams the container's per-chunk stdout, and POSTs each chunk to the
backend (the container has --network=none, so the worker is the only thing that talks to the backend). The
backend writes each chunk atomically + idempotently (complete_sweep_chunk, §6.1).

Per-sweep steps:
  1. read the already-committed cells (resolve_committed_cells) so a resume skips them (§6.2);
  2. write strategy.py + a sweep run_spec.json (the §3 payload + the committed skip-set) to /tmp/run_{job_id}/;
  3. docker run with one :ro mount per distinct dataset source + the strategy/spec mounts, --network=none;
  4. stream stdout: each {"cells": [...]} line is POSTed as one chunk (report_sweep_chunk); the final
     {"sweep_done": ...} line finalizes the run (report_sweep_result); the heartbeat carries cancel;
  5. on exit 0 with a sweep-done line: complete the job; on non-zero / no sweep-done: fail the run + job.

Import-light (mirrors run_backtest): httpx + the report helpers are imported lazily inside run().
"""
from __future__ import annotations

import asyncio
import json

# Reuse the single-run handler's proven host-mapping + spawn primitives (no fork).
from jobs.run_backtest import (
    CANCEL_GRACE_S,
    CANCEL_POLL_S,
    DATA_ROOT,
    DOCKER_IMAGE,
    NUMBA_CACHE,
    STDOUT_STREAM_LIMIT,
    dataset_source,
    prepare_run_dir,
)

SWEEP_MEMORY = "4g"  # a sweep chunk's from_signals peak is larger than a single run (bounded by size_cap_cells)
SWEEP_CPUS = "2"     # same CPU budget as a single backtest

# Lane C5 (set 16c, #427, AC-8): an operational dial, not a backtest parameter (#204's own
# distinction: this bounds STORAGE, never a computed result, so it needs an inline justification
# rather than a docs/research_lab/parameter_provenance.md row). 50 MiB. Each trial's own cscv
# block-sums row costs 3 float64 numbers (count, sum, sumsq) per block, `s_blocks` (16 in
# production, #421) blocks per trial: about 384 bytes/trial before parquet overhead (the same
# lower bound scripts/probes/check5_cscv's own c07 uses), so 50 MiB covers roughly 130,000 trials
# at S=16, comfortably above any universe sweep this build's own size_cap_cells lets run in one
# job, while still catching a pathological grid whose cscv artifact would otherwise dwarf every
# sibling per-run file (equity.parquet, trades.parquet) this store already holds.
CSCV_ARTIFACT_SIZE_CAP_BYTES = 50 * 1024 * 1024

# Duplicated from dashboard.backend.judge_checks.CSCV_ARTIFACT_FILENAME / the entrypoint's own
# _CSCV_ARTIFACT_FILENAME (the engine/backend boundary; this module has no database and must
# never import dashboard.backend). The name the container writes into the mounted run dir.
CSCV_ARTIFACT_FILENAME = "cscv_blocks.parquet"
# AC-8's own marker, uploaded IN PLACE of the real artifact when this sweep's own trial count
# would have breached the cap above (dashboard.backend.judge_checks.CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME's
# own docstring explains why a separate file, never a flag on the real one, carries this fact).
CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME = "cscv_blocks_over_cap.marker.parquet"


def cscv_artifact_would_exceed_cap(num_trials: int, s_blocks: int) -> bool:
    """AC-8: a pure size predicate over the REAL, post-sweep trial count (never an upfront
    estimate: run_sweep.py calls this only after the container's own sweep-done line reports the
    exact `total_cells`, so this is exact, not predicted-then-possibly-wrong). 3 float64 numbers
    per block (count, sum, sumsq), 8 bytes each, `s_blocks` blocks per trial: the same
    parquet-overhead-free lower bound `CSCV_ARTIFACT_SIZE_CAP_BYTES`'s own comment derives from.
    The boundary is drawn AT the cap: exactly at the cap does not breach it (strictly greater
    does), matching scripts/probes/check5_cscv's own c07.
    """
    predicted_bytes = num_trials * s_blocks * 3 * 8
    return predicted_bytes > CSCV_ARTIFACT_SIZE_CAP_BYTES


def write_sweep_inputs(job_id: int, run_id: int, source_code: str, spec: dict):
    """Write strategy.py + the sweep run_spec.json to /tmp/run_{job_id}/. Returns the tmp dir.

    strategy.py is the DB-versioned source verbatim (the container imports it); run_spec.json is the sweep
    payload (§3) the entrypoint reads (universe, param_grid, backtest_mode, selection_scope, registered_n,
    size_cap_cells, init_cash, param_mode, params_decl) plus the committed-cell skip-set for resume (§6.2).
    M5 C: param_mode ('zero'|'vectorized'|'inline') is the entrypoint's top-level fork; params_decl is the
    [{name,type}] the inline path needs (the engine has no class sweep_params to read for an inline strategy).
    """
    from pathlib import Path  # function-scope: keep the module import light for the probe

    tmp_dir = Path(f"/tmp/run_{job_id}")  # per-job scratch dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "strategy.py").write_text(source_code, encoding="utf-8")  # byte-for-byte DB source
    (tmp_dir / "run_spec.json").write_text(json.dumps(spec), encoding="utf-8")  # the sweep entrypoint input
    return tmp_dir


def build_sweep_docker_cmd(run_id: int, universe: list, tmp_dir, run_dir=None) -> list:
    """Assemble the docker run argv for one sweep: one :ro mount per distinct dataset source + the spec mounts.

    Each universe member's dataset_path is container-relative /data/{source}/.../x.parquet; the engine reads
    every member's parquet to build the wide frame (§5.1). So we mount each DISTINCT source dir :ro once
    (the price-run mount profile, generalized to N members). No run-output mount for an ORDINARY sweep: it
    writes no artifacts to disk, only per-chunk results on stdout (§6.1). --network=none, as every backtest.

    Fold item 2b (round 1): `run_dir` is the host output dir (`prepare_run_dir`'s own return value),
    given only for a WALK-FORWARD dispatch (the caller's own is_walkforward check) -- that stage
    writes exactly one artifact, equity.parquet, into a :rw mount at /data/runs/{run_id}, the SAME
    mount shape the single-run entrypoint already uses (run_backtest.build_docker_cmd), so the
    worker's existing upload helper (report_run_artifacts) can read it straight off disk with no new
    transport. None (every ordinary sweep) omits the mount entirely -- byte-identical to the
    pre-fold argv for every non-walkforward caller.
    """
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",          # no network for the sweep compute
        "--memory", SWEEP_MEMORY,
        "--cpus", SWEEP_CPUS,
        "--user", "1000:1000",
    ]
    sources = sorted({dataset_source(m["dataset_path"]) for m in universe})  # distinct source subdirs (deterministic)
    for source in sources:            # one :ro mount per source (all members under it are then readable)
        cmd += ["-v", f"{DATA_ROOT / source}:/data/{source}:ro"]
    if run_dir is not None:  # fold item 2b: only a walkforward dispatch gets the run-output mount
        cmd += ["-v", f"{run_dir}:/data/runs/{run_id}:rw"]
    cmd += [
        "-v", f"{tmp_dir / 'strategy.py'}:/app/strategy.py:ro",     # DB-versioned strategy, read-only
        "-v", f"{tmp_dir / 'run_spec.json'}:/app/run_spec.json:ro",  # the sweep run context, read-only
        "-v", f"{NUMBA_CACHE}:/cache:rw",                           # shared numba cache
        "-e", "NUMBA_CACHE_DIR=/cache",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONHASHSEED=0",      # deterministic set/dict iteration (the single-run profile, A5a-014)
        DOCKER_IMAGE,
        "python", "/app/run_sweep_entrypoint.py",
    ]
    return cmd


async def _stream_sweep(proc, job_id: int, run_id: int, http, sink: dict) -> None:
    """Stream the container stdout: POST each chunk, capture the sweep-done/error line, watch cancel.

    Each stdout line is also logged. A ``{"cells": [...]}`` line is POSTed as one atomic chunk
    (report_sweep_chunk) — a POST failure stops the stream and fails the run (the chunk is the atomic unit;
    the committed chunks stay and a resume re-runs the rest, §6). The ``{"sweep_done": ...}`` line is kept
    for the finalize; an ``{"error": ...}`` line is kept for the failure message. The heartbeat carries the
    cancel signal (the run_backtest contract); a cancel terminates the container between chunks, leaving the
    committed chunks intact (resumable).
    """
    import httpx  # function-scope: the watcher catches only a transport error (DBG-4); anything else fails the run
    from helpers import log_event, report_cell_refusals, report_heartbeat, report_log, report_sweep_chunk

    async def stream_lines() -> None:
        async for raw in proc.stdout:  # ends when the container exits and stdout drains
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            await report_log(job_id, line, http)
            if not (line.startswith("{") and line.endswith("}")):
                continue  # a plain [sweep] log line, not a JSON result line
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # not a result line we act on
            if "cells" in obj:  # one chunk -> POST it atomically (the per-chunk write, §6.1)
                resp = await report_sweep_chunk(run_id, obj["cells"], http)
                sink["last_progress"] = resp  # {"written","skipped","done","total"}: for the log/operator
            elif "cell_refusals" in obj:  # lane D1: refused cells -> recorded at once (a POST failure fails the run)
                await report_cell_refusals(run_id, obj["cell_refusals"], http)
            elif "walkforward_slice" in obj:  # lane WF (6.2b): buffer each slice; relayed as one POST after finalize
                sink["walkforward_slices"].append(obj["walkforward_slice"])
            elif obj.get("sweep_done"):  # the final line: carries run-level annualization + environment
                sink["sweep_done"] = obj
            elif "error" in obj:  # the container's machine-readable error line
                sink["error"] = obj["error"]

    async def watch_cancel() -> None:
        while proc.returncode is None:  # until the container exits
            await asyncio.sleep(CANCEL_POLL_S)
            if proc.returncode is not None:
                return
            try:
                requested = await report_heartbeat(job_id, http)  # cancel rides the heartbeat (EI-2)
            except httpx.HTTPError as exc:  # a transient transport failure: logged, and the next poll retries
                log_event("cancel_watch_heartbeat_failed", job_id=job_id, error=repr(exc))
                continue
            # Any other error is not transient: it propagates out of this watcher, and _stream_sweep stops
            # the container and fails the run (DBG-4), so a broken watcher never leaves a sweep unwatched.
            if requested:
                sink["cancelled"] = True
                try:
                    proc.terminate()  # SIGTERM -> grace -> SIGKILL (the run_backtest sequence)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=CANCEL_GRACE_S)
                    except asyncio.TimeoutError:
                        proc.kill()
                except ProcessLookupError:
                    pass  # the container exited between the check and the signal — fine
                return

    log_task = asyncio.create_task(stream_lines())
    watch_task = asyncio.create_task(watch_cancel())
    try:
        # DBG-4: stop waiting as soon as either task ends. A watcher error must not wait behind a container
        # that never exits (stdout would never close), so it is checked first.
        await asyncio.wait({log_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        if watch_task.done() and not watch_task.cancelled() and watch_task.exception() is not None:
            watcher_error = watch_task.exception()
            if proc.returncode is None:  # the container has no cancel path now: stop it before failing the run
                proc.kill()
            log_task.cancel()
            await asyncio.gather(log_task, return_exceptions=True)
            raise watcher_error
        await log_task            # returns once the process exits and stdout drains
    finally:
        # CQ-SF1 (scale-defect build): if stream_lines RAISED (e.g. a chunk POST got a non-2xx ->
        # report_sweep_chunk's raise_for_status), still cancel the cancel-watcher AND kill the container.
        # Otherwise the container's stdout has no reader, its pipe fills, and it hangs forever holding a
        # docker slot; the outer run() except then fails the run/job but the container would leak. This runs
        # on the normal drain path too (proc already exited -> the kill no-ops via the returncode check).
        watch_task.cancel()
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
    """Drive one sweep: resume-read, spawn, stream chunks, finalize (M5 1B §5/§6)."""
    import httpx  # for the precise POST-failure exception type below
    from helpers import (
        fail_run_and_job,
        fetch_cscv_split_count,
        report_job_cancelled,
        report_job_completed,
        report_job_failed,
        report_log,
        report_run_artifacts,  # lane C5: uploads the cscv artifact (or its over-cap marker) after success
        report_run_cancelled,
        report_run_failed,
        report_sweep_result,
        resolve_committed_cells,
    )

    job_id = job["id"]
    p = job["payload"]
    run_id = p["run_id"]                 # from the payload — the worker has no DB to resolve it from
    source_code = p["source_code"]       # DB-versioned strategy source, embedded by the backend (1C)
    universe = p["universe"]             # the resolved members (dataset_path, symbol, asset_class, fees, slippage, dataset_sha256)

    try:
        committed = await resolve_committed_cells(run_id, http)  # §6.2 resume skip-set (empty on a fresh sweep)
    except httpx.HTTPError as e:
        error = f"reading committed cells failed: {e}"  # a resume must not silently recompute everything
        await report_run_failed(run_id, error, http)
        await report_job_failed(job_id, error, http)
        return

    # Lane C5 (set 16c, #427, plan amendment 4): check 5's own frozen cscv_split_count (S), read
    # through the one worker route (routes/judge.py's worker_router) that can reach judgedb, since
    # this container has no database of its own. A failure here is NEVER fatal to the sweep
    # (amendment 4's own words): the sweep still completes and simply writes no cscv artifact,
    # which check 5 reads exactly like a sweep stored before this build (AC-5).
    try:
        cscv_split_count = await fetch_cscv_split_count(http)
    except httpx.HTTPError as e:
        cscv_split_count = None
        await report_log(job_id, f"cscv_split_count unavailable, sweep proceeds without the cscv artifact: {e}", http)

    # The sweep spec the entrypoint reads (§3) + the committed skip-set for resume.
    spec = {
        "run_id": run_id,
        "universe": universe,
        "param_grid": p["param_grid"],
        "backtest_mode": p.get("backtest_mode", "independent"),
        "selection_scope": p.get("selection_scope", "in_isolation"),  # B3.3: the dispatch default; the field always rides the payload so this fallback is normally dead, but it must AGREE with the dispatch default (a disagreeing dead default is a latent silent-fallback)
        "registered_n": p.get("registered_n"),
        "single_combo": p.get("single_combo", 0),     # B3.1/B4: 1 -> the entrypoint runs the zero-param single-run path + relaxes the empty-grid gate
        "param_mode": p.get("param_mode"),             # M5 C: 'zero'|'vectorized'|'inline'; the relay MUST forward it or an inline sweep falls to vectorized -> SweepNotSupported (B-C4b)
        "params_decl": p.get("params_decl"),           # M5 C: [{name,type}] for the inline mode (the engine has no class sweep_params to read); None otherwise
        "size_cap_cells": p["size_cap_cells"],
        "init_cash": p.get("init_cash", 10_000.0),
        "window_start_ts": p.get("window_start_ts"),  # B3.4/B4: the saved test window (NULL = open on that side); the entrypoint cold-slices each member before signals
        "window_end_ts": p.get("window_end_ts"),
        "settings": p.get("settings", []),            # B4: the frozen run-level settings the zero-param single-run path feeds to strat.run (the wide path reads costs per-member instead)
        "committed_cells": committed,
        "walkforward": p.get("walkforward"),          # lane WF (6.2b): the three ruled parameters, or None for an ordinary sweep
        "cscv_split_count": cscv_split_count,          # lane C5 (set 16c, #427): None when S was unavailable (amendment 4)
    }
    if "pinned_sizing_method" in p:  # P2h: the reserve exam's pin (a graded run's recorded sizing), copied only when the payload holds it
        spec["pinned_sizing_method"] = p["pinned_sizing_method"]
        spec["pinned_sizing_spec"] = p["pinned_sizing_spec"]
    tmp_dir = write_sweep_inputs(job_id, run_id, source_code, spec)
    # Fold item 2b (round 1): capture the 1000-owned run dir rather than discarding it -- a
    # walkforward dispatch now writes its own equity.parquet into it. Lane C5 (set 16c, #427):
    # an ORDINARY sweep now ALSO gets it mounted whenever S was resolved, since the container
    # writes the cscv artifact there; a sweep with no S (amendment 4) still gets no mount at all,
    # byte-identical to the pre-lane behaviour.
    run_dir = prepare_run_dir(run_id)
    is_walkforward = p.get("walkforward") is not None  # fold item 2b: only a walkforward dispatch mounts run_dir
    needs_run_dir_mount = is_walkforward or cscv_split_count is not None

    try:
        cmd = build_sweep_docker_cmd(run_id, universe, tmp_dir, run_dir=run_dir if needs_run_dir_mount else None)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=STDOUT_STREAM_LIMIT,  # 1.2 defense-in-depth: raise the 64 KB readline ceiling (entrypoint byte-bounds lines)
        )
        sink = {"sweep_done": None, "error": None, "cancelled": False, "last_progress": None,
                "walkforward_slices": []}  # lane WF (6.2b): every {"walkforward_slice": ...} line, buffered in order
        await _stream_sweep(proc, job_id, run_id, http, sink)
        rc = await proc.wait()

        if sink["cancelled"]:
            # Cancellation (§6.1): the committed chunks stay in the cube (resumable); the run is flipped to
            # 'cancelled' (the partial cube cells persist for a re-dispatch resume).
            await report_run_cancelled(run_id, http, status="cancelled")
            await report_job_cancelled(job_id, http)
            return

        if rc != 0 or sink["sweep_done"] is None:
            # A non-zero exit, or exit 0 without the final sweep-done line, is a failure (no silent success).
            error = sink["error"] or (f"sweep container exited {rc}" if rc != 0
                                      else "sweep container exited 0 without a sweep-done line")
            await report_run_failed(run_id, error, http)
            await report_job_failed(job_id, error, http)
            return

        # Finalize: flip the run to 'succeeded', record the run-level annualization (§15.1) + environment.
        done = sink["sweep_done"]
        try:
            await report_sweep_result(run_id, done.get("annualization"), done["environment"],
                                      done.get("total_cells", 0), http)
        except httpx.HTTPError as e:
            error = f"sweep finalize failed: {e}"  # the chunks committed, but the run is not flipped — fail loud
            await report_run_failed(run_id, error, http)
            await report_job_failed(job_id, error, http)
            return

        # Lane C5 (set 16c, #427, AC-1/AC-8): the run has already flipped to succeeded above.
        # Decide whether to upload the container's own cscv artifact or, when this sweep's REAL
        # trial count (done's own total_cells, exact, never an upfront estimate) would breach the
        # named size cap, upload the tiny over-cap marker in its place instead. Exactly one of the
        # two, never both and never a partial write ("never written half" stands): only attempted
        # when S was resolved and the container therefore had the mount to write into at all.
        if needs_run_dir_mount and cscv_split_count is not None:
            cscv_path = run_dir / CSCV_ARTIFACT_FILENAME
            if cscv_path.is_file():  # the container may have found no partition to run at all (an empty universe never reaches here, but a defensive check costs nothing)
                try:
                    if cscv_artifact_would_exceed_cap(done.get("total_cells", 0), cscv_split_count):
                        marker_path = run_dir / CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME
                        marker_path.write_bytes(b"")  # the marker's bytes carry no information; its NAME is the signal
                        await report_run_artifacts(run_id, {CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME: marker_path}, http)
                    else:
                        await report_run_artifacts(run_id, {CSCV_ARTIFACT_FILENAME: cscv_path}, http)
                except (httpx.HTTPError, OSError) as e:
                    # The run already succeeded (report_sweep_result above committed that fact,
                    # correctly): a failed cscv upload means only that this ONE optional artifact
                    # never landed, never that the run itself failed. Check 5 then reads exactly
                    # the AC-5 "missing artifact" wording, the same shape a real upload failure of
                    # any OTHER artifact in this codebase already degrades to (the walk-forward
                    # equity upload's own precedent immediately below).
                    await report_log(job_id, f"cscv artifact upload failed (run succeeded): {e}", http)

        # Lane WF (6.2b, AC-19): the walk-forward relay, one POST after the ordinary sweep-result
        # finalize above has already flipped the run to succeeded. The entrypoint's own sweep-done
        # line carries a "walkforward" key only for a walkforward run (spec["walkforward"] was not
        # None); an ordinary sweep's done line carries no such key and this block is skipped. Raw
        # httpx, not a new helpers.py function: helpers.py is outside this lane's own declared
        # areas (the night plan's lane table), so SERVER_URL and HEADERS are read from it (an
        # import, never a write) and the POST is built here, matching report_sweep_result's own
        # request shape one function up.
        wf_summary = done.get("walkforward")
        if wf_summary is not None:
            from helpers import HEADERS, SERVER_URL, report_run_artifacts  # read-only import: helpers.py itself is untouched
            # Fold item 2b (round 1): upload the container's own equity.parquet BEFORE the
            # walkforward-result POST below, so the backend's own equity_path write (gated on the
            # artifact store, the K.0 no-succeeded-without-bytes invariant) has something to find.
            # The file only exists when at least one slice produced a real out-of-sample leg
            # (the entrypoint's own stitched_equity_parts check) -- a zero-slice run uploads
            # nothing, honestly, rather than an empty or fabricated curve.
            equity_path = run_dir / "equity.parquet"
            if equity_path.is_file():
                try:
                    await report_run_artifacts(run_id, {"equity.parquet": equity_path}, http)
                except (httpx.HTTPError, OSError) as e:
                    # The run already succeeded; a failed equity upload means only that this one
                    # optional artifact did not land, never that the run itself failed. Logged, not
                    # fatal, matching the relay's own error-handling shape immediately below.
                    await report_log(job_id, f"walk-forward equity upload failed (run succeeded): {e}", http)
            try:
                wf_resp = await http.post(
                    f"{SERVER_URL}/api/research/worker/runs/{run_id}/walkforward-result",
                    json={
                        "slices": sink["walkforward_slices"],
                        "in_sample_profit": wf_summary.get("in_sample_profit"),
                        "out_of_sample_profit": wf_summary.get("out_of_sample_profit"),
                        "wfa_boundary_buffer_bars": wf_summary.get("wfa_boundary_buffer_bars", 0),
                        # Fold item 2c (round 1): the five general stats, computed by the entrypoint
                        # from the chained out-of-sample curve, replacing the old hardcoded 0/0.0/0.
                        "total_return": wf_summary.get("total_return"),
                        "max_drawdown": wf_summary.get("max_drawdown"),
                        "num_trades": wf_summary.get("num_trades", 0),
                        "total_fees_paid": wf_summary.get("total_fees_paid"),
                        "total_slippage_cost": wf_summary.get("total_slippage_cost"),
                        # Fold item 1 (round 1): the two slice-usage counts, carried on this body only
                        # (no new research_run_stats column) so the backend can log/inspect them.
                        "slices_used": wf_summary.get("slices_used", 0),
                        "slices_with_absent_profit": wf_summary.get("slices_with_absent_profit", 0),
                    },
                    headers=HEADERS,
                )
                wf_resp.raise_for_status()
            except httpx.HTTPError as e:
                # The sweep itself already succeeded (report_sweep_result above committed that fact,
                # correctly): a relay failure here means the slice rows never landed, never that the
                # run failed. Never flip an already-succeeded run to failed for this: the named
                # monitor row runs_missing_walkforward_record (docs/units/WF/monitor_rows.md) is the
                # mechanism that surfaces exactly this gap, so it is logged here, not papered over
                # and not mis-reported as a run failure.
                await report_log(job_id, f"walk-forward relay failed (run succeeded, slices not recorded): {e}", http)
            # Fold item 1 (round 1): log the slice-usage breakdown unconditionally (not only on a
            # relay failure), so an operator reading this job's log sees it every time, with no new
            # database column needed to hold it.
            await report_log(
                job_id,
                f"walk-forward slices: {wf_summary.get('slices_used', 0)} used, "
                f"{wf_summary.get('slices_with_absent_profit', 0)} with absent profit",
                http,
            )
        await report_job_completed(job_id, {"run_id": run_id, "total_cells": done.get("total_cells", 0)}, http)
    except Exception as e:  # noqa: BLE001 — 1.1 safety net: an unexpected fault (e.g. the readline-limit ValueError of scale-defect 1) must FAIL THE RUN loudly, never strand it 'pending'
        await fail_run_and_job(run_id, job_id, f"unexpected sweep worker error: {type(e).__name__}: {e}", http)
        return  # the finally below still runs (rmtree the scratch)
    finally:
        import shutil  # function-scope: keep the module import light for the probe
        shutil.rmtree(tmp_dir, ignore_errors=True)  # drop the per-job scratch
