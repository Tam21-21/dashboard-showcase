"""Job handler: compute_run_metrics (M5 Track C, C2 §7 / C-metrics §2.3 — the full-metrics compute-on-demand).

The third compute handler (sibling of run_backtest.run / run_sweep.run). Like them the worker NEVER imports vbt
and NEVER touches a portfolio: it marshals the compute spec in, spawns research-runner:compute_metrics_entrypoint,
reads the ONE result line, and POSTs the computed payload to the backend cache (the container has
--network=none; the worker is the only thing that talks to the backend).

CONTRACT DIFFERENCE from run_backtest/run_sweep: a compute job NEVER touches the target run. The run is already
'succeeded' — only its post-hoc full-metrics compute can fail. So this handler reports JOB success/failure only
(report_job_completed / report_job_failed), never report_run_*. The payload deliberately carries `target_run_id`
(NOT `run_id`), so even the worker-loop backstop (which fails `payload.run_id` on an uncaught raise) cannot fail
the succeeded run; and this handler catches everything itself, so the backstop never fires.

Two modes (the entrypoint forks on spec['mode']):
  - single: fetch equity.parquet + trades.parquet from the backend artifact store over HTTP (C2 §2.1), mount
    them read-only, run the derive — no strategy, no dataset.
  - cell: write the FROZEN strategy source + the cell spec, mount the member's dataset source read-only, run
    the three-way re-run (the entrypoint branches on the frozen param_mode).

Import-light (mirrors run_sweep): httpx + the report helpers are imported lazily inside run().
"""
from __future__ import annotations

import asyncio
import hashlib  # lane JC: the replay artifacts' own sha256, computed the same way report_run_artifacts computes it
import json
import shutil
import time  # lane JC: the replay object's own replayed_at (integer epoch seconds, spec L-2)
from pathlib import Path

# Reuse the single-run handler's proven host-mapping + spawn primitives (no fork).
from jobs.run_backtest import (
    DATA_ROOT,
    DOCKER_IMAGE,
    NUMBA_CACHE,
    STDOUT_STREAM_LIMIT,
    dataset_source,
)

COMPUTE_MEMORY = "4g"  # a cell re-run's from_signals peak (a single member x combo) fits well within this
COMPUTE_CPUS = "2"     # same CPU budget as a single backtest
COMPUTE_TIMEOUT_S = 600.0  # a single-cell re-run / single-run derive is seconds; a hung container fails the job, never hangs forever
# The container mount path for the fetched single-run artifacts (equity/trades). The ONE source of this path,
# used BOTH for the docker -v mount AND injected into the spec as artifacts_dir, so the entrypoint reads it
# from the spec and the two can never disagree (the single-mode entrypoint reads spec['artifacts_dir']).
_SINGLE_ARTIFACTS_MOUNT = "/data/artifacts"
# Lane JC (candidate chain link spec, section 1.4 step 3): the one rw mount a replay compute job
# gets, where the container writes its own reproduced equity/trades series so the worker (the
# only thing in this pipeline that talks to the backend) can upload them as this run's own
# replay_equity_v{v}_{t}.parquet / replay_trades_v{v}_{t}.parquet artifacts.
_REPLAY_OUT_MOUNT = "/data/replay_out"


def _result_line(stdout: str) -> dict | None:
    """Find the entrypoint's single JSON result line ({"metrics":...} or {"error":...}) in stdout, or None."""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue  # a plain [metrics] log line, not the result
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "metrics" in obj or "error" in obj:
            return obj
    return None


# M5 fetcher job 10 what-can-break mode 4 ("the provenance block is forwarded but arrives empty, so
# a run records a provenance that says nothing and looks fine... a counter of saved runs whose
# provenance block is absent or empty, which must read zero"). Silent accumulator, same idiom as
# eodhd_hourly.py's own _record_refusal/report_refusals split: _cache_payload() itself must stay
# silent (research_lab/probes/reader_and_provenance/ac6_cache_payload_includes_engine_provenance
# .probe.py calls it directly with two deliberately-empty-provenance inputs and asserts EXACT stdout
# against its own .expected, so a print inside _cache_payload would drift that probe); this counter
# is only ever printed by report_empty_provenance_count(), called from run()'s own success path.
_empty_provenance_count = 0

# Lane MF (DECISIONS #423, found by lane JC's own code-quality gate 1): the one failure mode the
# outer except below cannot let slip past silently -- report_job_failed ITSELF raising inside the
# "the job already failed, now tell the backend" handler. Before this lane the inner except was a
# bare `pass`, so such a failure left no trace anywhere: no report reached the backend, nothing was
# logged, nothing was counted. Module-level, in-process, the RUN_GENERATOR_MONITOR_COUNTERS idiom
# of research_lab/worker/jobs/run_generator.py (a plain dict, incremented in place, never
# reassigned to a new object). Registry row drafted at docs/units/MF/monitor_rows.md for the lead
# to fold into docs/ops/ops_monitor_registry.md; its reader is owed, the same "counter EXISTS,
# reader OWED" status research_lab/CLAUDE.md already records for IBKR_FEE_MONITOR_COUNTERS.
COMPUTE_METRICS_MONITOR_COUNTERS = {
    "report_job_failed_swallowed": 0,
}


def _cache_payload(result: dict) -> dict:
    """M5 fetcher job 10, AC-6: extend the container's metrics dict with its own engine-provenance
    block, instead of forwarding result['metrics'] alone and silently dropping result['environment'].

    THE DEFECT this replaces (DECISIONS #324): compute_metrics_entrypoint.py's own main() already
    prints {"metrics": {...}, "environment": {...}} on its final result line (_environment(), defined
    in run_backtest_entrypoint.py, returns python_version / vbt_version / build_provenance / hostname)
    -- but the worker used to read result["metrics"] only, so a saved run could never name the engine
    that produced it.

    Extends the EXISTING provenance block rather than inventing a parallel one (this job's own design
    constraint): the metrics dict IS the record report_full_metrics_cache saves, so the container's
    environment block becomes a "provenance" key WITHIN it, not a separate call or a separate table.

    Returns a NEW dict (the caller's own result dict is never mutated): every original metric key,
    plus "provenance" carrying result["environment"] verbatim, or an empty dict when the container's
    result carried no environment at all (an honest empty, never a crash on a missing key). Silently
    counts an absent-or-empty provenance (ops monitor mode 4); never prints here (see the module-level
    comment on _empty_provenance_count just above for why).
    """
    global _empty_provenance_count
    payload = dict(result.get("metrics", {}))  # copy: never mutate the caller's own result dict
    provenance = dict(result.get("environment", {}))  # verbatim forward; {} if absent
    payload["provenance"] = provenance
    if not provenance:  # ops monitor mode 4: silent here; report_empty_provenance_count() prints it
        _empty_provenance_count += 1
    return payload


def report_empty_provenance_count() -> None:
    """Ops monitor (M5 fetcher job 10 what-can-break mode 4): a fire-only-when-non-zero summary of
    every saved run whose provenance block came out absent or empty since process start, which must
    read zero on a correctly configured engine. Prints nothing if none has been counted yet, the same
    idiom eodhd_hourly.py's own report_refusals()/report_uncorrected_jumps() already use."""
    if _empty_provenance_count == 0:  # nothing counted yet: stay silent, matching the established idiom
        return
    print(f"compute_run_metrics: full_metrics_cache_empty_provenance_count={_empty_provenance_count} "
          "(a saved run's provenance block came out absent or empty; expected 0)", flush=True)


def _write_compute_inputs(job_id: int, spec: dict, tmp_dir: Path) -> None:
    """Write run_spec.json (+ strategy.py for a cell or an aggregate re-run) into the per-job scratch dir."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    if spec["mode"] in ("cell", "aggregate"):  # both re-run members via _rerun_cell_runresult -> need strategy.py
        (tmp_dir / "strategy.py").write_text(spec["source_code"], encoding="utf-8")  # the FROZEN strategy source
    (tmp_dir / "run_spec.json").write_text(json.dumps(spec), encoding="utf-8")  # the entrypoint reads this


def _build_compute_docker_cmd(spec: dict, tmp_dir: Path) -> list[str]:
    """Assemble the docker argv: the mode-specific :ro mounts + the spec mount + the numba cache, --network=none.

    single: mount the fetched artifacts dir (equity/trades) at /data/artifacts. cell: mount each distinct member
    (and benchmark) dataset source dir at /data/{source}, plus the frozen strategy.py. No run-output mount (the
    compute emits its one result line on stdout, never writes artifacts), EXCEPT a replay (lane JC, candidate
    chain link spec section 1.4 step 3): one small rw mount at /data/replay_out, the ONLY way the reproduced
    replay's own equity/trades series leave the --network=none container, since it cannot POST them itself."""
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", COMPUTE_MEMORY,
        "--cpus", COMPUTE_CPUS,
        "--user", "1000:1000",
    ]
    if spec["mode"] == "single":
        cmd += ["-v", f"{tmp_dir / 'artifacts'}:{_SINGLE_ARTIFACTS_MOUNT}:ro"]  # the fetched equity/trades parquet
    else:  # cell / aggregate: one :ro mount per distinct dataset source + the frozen strategy.py (both re-run members)
        # a cell re-runs ONE member; an aggregate re-runs EVERY member -> gather the member dataset path(s) by mode
        # (an aggregate spec carries members:[...] not member, so indexing spec["member"] here would KeyError).
        members = spec["members"] if spec["mode"] == "aggregate" else [spec["member"]]
        paths = [m["dataset_path"] for m in members]
        bench = spec.get("benchmark")
        if bench and bench.get("dataset_path"):
            paths.append(bench["dataset_path"])
        for source in sorted({dataset_source(p) for p in paths}):  # distinct source subdirs (deterministic)
            cmd += ["-v", f"{DATA_ROOT / source}:/data/{source}:ro"]
        cmd += ["-v", f"{tmp_dir / 'strategy.py'}:/app/strategy.py:ro"]  # the frozen strategy source
    if spec.get("replay"):
        cmd += ["-v", f"{tmp_dir / 'replay_out'}:{_REPLAY_OUT_MOUNT}:rw"]  # the reproduced replay's own two files
    cmd += [
        "-v", f"{tmp_dir / 'run_spec.json'}:/app/run_spec.json:ro",  # the compute spec the entrypoint reads
        "-v", f"{NUMBA_CACHE}:/cache:rw",
        "-e", "NUMBA_CACHE_DIR=/cache",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONHASHSEED=0",  # deterministic set/dict iteration (the single-run profile)
        DOCKER_IMAGE,
        "python", "/app/compute_metrics_entrypoint.py",
    ]
    return cmd


async def _fetch_artifacts(run_id: int, names: list[str], dest: Path, http) -> None:
    """Fetch each named run artifact from the backend store over HTTP into `dest` (C2 §2.1, single mode)."""
    from helpers import HEADERS, SERVER_URL  # function-scope (keep the module import light for the probe)

    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        r = await http.get(f"{SERVER_URL}/api/research/worker/runs/{run_id}/artifacts/{name}", headers=HEADERS)
        r.raise_for_status()  # a missing artifact (purged run) -> the compute job fails -> route renders unavailable
        (dest / name).write_bytes(r.content)  # a run's equity/trades parquet is a few MB; .content is fine


async def _upload_replay_artifacts(run_id: int, variation_id: int, ticker: str, tmp_dir: Path,
                                   http) -> dict:
    """Lane JC (candidate chain link spec, section 1.4 step 3): read the reproduced replay's own
    equity/trades series back from the container's rw output mount, upload them under the source
    run's own id with the fixed names spec 1.4's "Where" gives, and return the L-2 "artifacts"
    sub-object ({"equity": {"name", "sha256"}, "trades": {"name", "sha256"}}) the caller folds into
    the replay object. Raises (uncaught, by design) if either file is missing: run()'s own outer
    except turns that into a failed job rather than a cache write with a half-uploaded replay."""
    from helpers import report_run_artifacts  # function-scope, matching this module's own lazy-import idiom

    out_dir = tmp_dir / "replay_out"
    equity_name = f"replay_equity_v{variation_id}_{ticker}.parquet"
    trades_name = f"replay_trades_v{variation_id}_{ticker}.parquet"
    equity_path = out_dir / "replay_equity.parquet"
    trades_path = out_dir / "replay_trades.parquet"
    equity_sha = hashlib.sha256(equity_path.read_bytes()).hexdigest()
    trades_sha = hashlib.sha256(trades_path.read_bytes()).hexdigest()
    await report_run_artifacts(run_id, {equity_name: equity_path, trades_name: trades_path}, http)
    return {
        "equity": {"name": equity_name, "sha256": equity_sha},
        "trades": {"name": trades_name, "sha256": trades_sha},
    }


async def _upload_buy_hold_artifact(run_id: int, variation_id: int, ticker: str, tmp_dir: Path,
                                    http) -> dict:
    """Lane C9 (set 16b, `#421`/`#423`, AC-1): the buy-and-hold control arm's own equity/trades
    series, read back from a `buy_hold` sub-directory of the SAME container rw output mount the
    candidate's own replay uses (`_write_replay_series` wrote it there a second time), uploaded
    under the source run's own id with the SAME `replay_equity_v{v}_{t}.parquet` naming lane JC
    already established, extended with a `buyhold` infix (`ac1_container_and_route_wiring`'s own
    resolution). Returns the same two-entry shape `_upload_replay_artifacts` returns.

    Called ONLY when the caller has already confirmed the container wrote the arm's own equity
    file (AC-4's own "no arm artifact" case never reaches this function at all), so a raise here
    means a genuinely unexpected partial write, never the ordinary absent-arm case.
    """
    from helpers import report_run_artifacts  # function-scope, matching this module's own lazy-import idiom

    out_dir = tmp_dir / "replay_out" / "buy_hold"
    equity_name = f"replay_buyhold_equity_v{variation_id}_{ticker}.parquet"
    trades_name = f"replay_buyhold_trades_v{variation_id}_{ticker}.parquet"
    equity_path = out_dir / "replay_equity.parquet"
    trades_path = out_dir / "replay_trades.parquet"
    equity_sha = hashlib.sha256(equity_path.read_bytes()).hexdigest()
    trades_sha = hashlib.sha256(trades_path.read_bytes()).hexdigest()
    await report_run_artifacts(run_id, {equity_name: equity_path, trades_name: trades_path}, http)
    return {
        "equity": {"name": equity_name, "sha256": equity_sha},
        "trades": {"name": trades_name, "sha256": trades_sha},
    }


async def run(job: dict, http) -> None:
    """Drive one full-metrics compute: marshal inputs, spawn, read the result, cache it. Never touch the run."""
    from helpers import (heartbeat_loop, log_event, report_full_metrics_cache, report_job_completed,
                         report_job_failed, stop_heartbeat)

    job_id = job["id"]
    p = job["payload"]
    run_id = p["target_run_id"]            # NOT 'run_id', a compute job must never let the backstop fail the run
    tmp_dir = Path(f"/tmp/compute_{job_id}")
    # None until the container is spawned below; the finally checks this before cancelling, so an
    # exception raised BEFORE the container exists (e.g. a bad payload key) has nothing to cancel.
    heartbeat_task = None
    try:
        if p["mode"] == "single":
            # Tell the entrypoint where the artifacts are mounted IN THE CONTAINER (it reads spec['artifacts_dir']);
            # the worker owns the mount path, so inject it here before writing run_spec.json. Must match the -v mount.
            p["artifacts_dir"] = _SINGLE_ARTIFACTS_MOUNT
        if p.get("replay"):
            # Lane JC: tell the entrypoint where its rw output mount lives IN THE CONTAINER, the
            # same "worker owns the mount path" idiom as artifacts_dir just above; must match the
            # -v mount _build_compute_docker_cmd adds when spec['replay'] is true.
            p["replay_out_dir"] = _REPLAY_OUT_MOUNT
        _write_compute_inputs(job_id, p, tmp_dir)
        if p.get("replay"):
            # Round 2, measured by the lead's own jc13_measurement.md AND reproduced here after the
            # A1 rebuild: docker auto-creates an ABSENT bind-mount host path as root, so the
            # container's own --user 1000:1000 (line ~141) cannot write into it, and the reproduced
            # replay files fail with a bare PermissionError. Pre-create the host side of the :rw
            # mount here, before the container ever starts, world-writable so UID 1000 can write
            # into it whatever the host's own umask (this directory holds nothing but two throwaway
            # parquet files for the lifetime of one job, deleted with the rest of tmp_dir below).
            (tmp_dir / "replay_out").mkdir(parents=True, exist_ok=True)
            (tmp_dir / "replay_out").chmod(0o777)
        if p["mode"] == "single":  # fetch the stored artifacts the derive reads (C2 §2.1)
            await _fetch_artifacts(run_id, p.get("artifacts", ["equity.parquet", "trades.parquet"]),
                                   tmp_dir / "artifacts", http)
        cmd = _build_compute_docker_cmd(p, tmp_dir)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=STDOUT_STREAM_LIMIT)
        # Heartbeat concurrently with the container now that it exists (spec brief §3.1). Not
        # starved: the work is `await asyncio.wait_for(proc.communicate(), ...)`, which yields.
        # Step 0a measured 1.005 s median against a 1.0 s ask.
        heartbeat_task = asyncio.create_task(heartbeat_loop(job_id, "compute_run_metrics", http))
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=COMPUTE_TIMEOUT_S)
        except asyncio.TimeoutError:  # a hung container -> kill + fail the job, never hang the queue
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await report_job_failed(job_id, f"compute container timed out after {COMPUTE_TIMEOUT_S:.0f}s", http)
            return
        rc = proc.returncode
        result = _result_line(out.decode("utf-8", errors="replace"))
        if rc != 0 or result is None or "error" in result:
            # a non-zero exit, no result line, or an error line -> fail the JOB (the run stays succeeded)
            stderr_tail = err.decode("utf-8", errors="replace").strip()[-500:]
            error = (result["error"] if result and "error" in result
                     else f"compute container exited {rc} without a metrics line; stderr: {stderr_tail}")
            await report_job_failed(job_id, error, http)
            return
        # success: cache the computed payload (metrics + engine provenance, AC-6), then complete the job
        cache_payload = _cache_payload(result)
        if p.get("replay"):
            # Lane JC (candidate chain link spec, section 1.4 steps 2-3): the container already
            # proved cell_metrics == expected_cell_metrics under the exact tolerance (or this
            # branch is never reached, since a mismatch is an error line the block above already
            # turned into a failed job). Pop the two transport-only keys the entrypoint carried the
            # comparison on, upload the reproduced series, and fold the L-2 "replay" object in.
            #
            # Amendment 1, A3 (read 1's own R7, a SHOULD-FIX): a STALE image (one baked before this
            # lane's own entrypoint edits) never writes either transport key, so cache_payload.pop
            # used to raise a bare KeyError -- naming a Python internal, never the actual cause.
            # Fail BY NAME instead, naming the image's own build provenance (read straight off this
            # same container's result, cache_payload["provenance"], set by _cache_payload above from
            # the entrypoint's own result["environment"]), so the operator reading the failed job
            # knows to rebuild the image rather than chase a phantom KeyError.
            if "_replay_cell_metrics" not in cache_payload or "_replay_expected_cell_metrics" not in cache_payload:
                provenance = cache_payload.get("provenance") or {}
                build_provenance = provenance.get("build_provenance", "unknown (no provenance block)")
                error = (
                    "stale runner image: this replay's container result carries neither "
                    "_replay_cell_metrics nor _replay_expected_cell_metrics, which only an image "
                    "baked with this lane's own compute_metrics_entrypoint.py ever writes; the "
                    f"image's own build provenance is {build_provenance!r} -- rebuild "
                    "research-runner:m3-latest from a commit that carries this lane's replay code"
                )
                await report_job_failed(job_id, error, http)
                return
            cell_metrics = cache_payload.pop("_replay_cell_metrics")
            expected_cell_metrics = cache_payload.pop("_replay_expected_cell_metrics")
            artifacts = await _upload_replay_artifacts(run_id, p["variation_id"], p["ticker"], tmp_dir, http)

            # Lane C9 (set 16b, AC-1): the buy-and-hold control arm. `_replay_buy_hold_error` is
            # `None` when the container computed it successfully, an error string when the
            # container's own arm construction raised (folded there rather than crashing the whole
            # replay job), and ABSENT ENTIRELY when the image predates this lane's own entrypoint
            # code (a stale image, the same "rebuild the image" case _replay_cell_metrics above
            # already guards for a DIFFERENT key) -- `.pop(..., None)` reads that third case as
            # `None` too, which is honest: an old image also never writes the `buy_hold` output
            # directory, so the upload attempt below is skipped for exactly the same reason either
            # way, and the route reads the resulting "no error, no artifact" pair as the arm being
            # absent (AC-4), the correct answer for both a stale image and a genuinely old replay.
            buy_hold_error = cache_payload.pop("_replay_buy_hold_error", None)
            buy_hold_artifacts = None
            if buy_hold_error is None:
                buy_hold_equity_path = tmp_dir / "replay_out" / "buy_hold" / "replay_equity.parquet"
                if buy_hold_equity_path.is_file():
                    buy_hold_artifacts = await _upload_buy_hold_artifact(
                        run_id, p["variation_id"], p["ticker"], tmp_dir, http)

            cache_payload["replay"] = {
                "cell_metrics": cell_metrics,
                "expected_cell_metrics": expected_cell_metrics,
                "tolerance": "exact",
                "member_dataset_sha256": p["member"]["dataset_sha256"],
                "artifacts": artifacts,
                "buy_hold_error": buy_hold_error,
                "buy_hold_artifacts": buy_hold_artifacts,
                "replayed_at": int(time.time()),
            }
        await report_full_metrics_cache(run_id, p["variation_id"], p["ticker"], p["formula_version"],
                                        cache_payload, http)
        report_empty_provenance_count()  # ops monitor mode 4: fire-only-when-non-zero, see module docstring
        await report_job_completed(job_id, {"target_run_id": run_id, "variation_id": p["variation_id"],
                                            "ticker": p["ticker"]}, http)
    except Exception as e:  # noqa: BLE001, catch ALL so an uncaught fault never reaches the run-failing backstop
        try:
            await report_job_failed(job_id, f"compute worker error: {type(e).__name__}: {e}", http)
        except Exception as report_exc:  # noqa: BLE001, even the failure report failing must not crash the poll loop
            # AC-1/AC-2/AC-3 (DECISIONS #423): the job's own failure report itself just raised, so
            # nothing was ever told to the backend and the run would otherwise vanish with zero
            # trace. Count it and log one structured line naming the job id and BOTH errors (the
            # original job failure and this report call's own failure) before returning normally,
            # so the poll loop still survives (AC-3) even though this occurrence was never reported.
            COMPUTE_METRICS_MONITOR_COUNTERS["report_job_failed_swallowed"] += 1
            log_event(
                "compute_run_metrics_report_job_failed_swallowed",
                job_id=job_id,
                original_error_type=type(e).__name__,
                original_error=str(e),
                report_error_type=type(report_exc).__name__,
                report_error=str(report_exc),
            )
        return
    finally:
        # Stop heartbeating on every exit path (timeout, job failure, success, or the outer except
        # above), the same finally that already removes the scratch dir, per the handoff's placement.
        await stop_heartbeat(heartbeat_task, job_id, "compute_run_metrics")  # None-safe: the task may never have been created
        shutil.rmtree(tmp_dir, ignore_errors=True)  # drop the per-job scratch
