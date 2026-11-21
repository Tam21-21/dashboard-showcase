"""Job handler: run_retest (lane NR, U.7s, docs/plans/night/2026-09-13_set8a_night/PLAN.md
section 7).

Spawns nautilus-runner:1.231.0 to replay one already-graded run's own entry signals through a real
NautilusTrader engine, then relays what it reports back to the backend's retest store. The worker
NEVER imports vectorbtpro and NEVER computes a backtest metric itself: it only downloads the three
artifacts the graded run already wrote (trades.parquet, price.parquet, equity.parquet), converts the real
20-column trades schema into the container's own simple input schema, runs the container, and
relays its result. Import-light on purpose (the run_backtest.py convention): pandas and httpx are
imported inside run(), so this module stays importable on a plain interpreter, and AC-21's
image-absent probe never triggers a pandas/httpx import at all (the image check runs first).
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

DOCKER_IMAGE = "nautilus-runner:1.231.0"

# The same heartbeat-and-cancel-poll cadence run_backtest.py's own _stream_and_watch uses (L7
# default): a retest's real BacktestEngine replay can run long enough over a real run's own trade
# history that a dead job needs to be told apart from a live one, and a cancel request needs to
# reach a running container the same way it reaches every other long job (findings round 1, plan
# section 7 criterion 3: "the heartbeat route called as every other handler calls it").
CANCEL_POLL_S = 1.0
CANCEL_GRACE_S = 30.0  # SIGTERM -> wait -> SIGKILL window, the same run_backtest.py convention

# The ops routing law's counter for the failure modes this job can hit before it ever reaches a
# container. In-process, module-level, incremented at their own call sites below; the project's
# own established idiom for a counter whose reader is owed rather than built in the same change
# (research_lab/CLAUDE.md's own IBKR_FEE_MONITOR_COUNTERS precedent), recorded as such in
# docs/units/NR/monitor_rows.md (and, for the fourth key below, docs/units/WD/monitor_rows.md).
# THREE distinct keys on purpose for the docker-inspect failures below; a fourth, unrelated key
# (result_relay_failed) was added by set 16b lane WD for the result-relay HTTP call, which fails
# for a different reason entirely (a bad response from the backend, not a local docker fault) and
# is kept apart from these three for the same reason they are kept apart from each other.
# image_absent (findings round 4,
# SF4) means the daemon answered and the image genuinely is not there. docker_daemon_unresponsive
# (round 4, SF4) means the daemon never answered at all inside IMAGE_INSPECT_TIMEOUT_S, which says
# nothing about whether the image is present. docker_spawn_failed (round 6, SF5) means the
# subprocess itself never started at all (docker binary missing, or the OS refused to spawn it), so
# the daemon was never even reached and the timeout never even started counting: a third, earlier
# failure than either of the other two. Folding any one of these into another would make a future
# reader of that counter diagnose the wrong problem: pulling an image that is not missing, waiting
# out a daemon that was never asked, or restarting a daemon that was never contacted.
RETEST_JOB_MONITOR_COUNTERS = {
    "image_absent": 0,
    "docker_daemon_unresponsive": 0,
    "docker_spawn_failed": 0,
    # Set 16b, lane WD (#423): every non-2xx the worker sees relaying a finished retest's result
    # to the backend's worker-scoped result route (helpers.report_retest_result), e.g. the AC-3
    # 404/409 refusals or a genuine transport fault. docs/units/WD/monitor_rows.md.
    "result_relay_failed": 0,
}

# The image-inspect call's own timeout (DECISIONS #204, operational dial, inline justification
# only): a local `docker image inspect` against an already-known image answers in well under a
# second on this laptop (a metadata read off the local docker daemon, no network call), so a value
# in the low tens of seconds is generous headroom for a busy host while still bounding the single
# job this async worker loop is running (findings round 4, SF4: this call previously had no bound
# at all, and ran BEFORE the heartbeat/cancel task exists, so a wedged or restarting docker daemon
# on the worker host froze the whole single-threaded worker process with nothing recorded and no
# operator cancel path -- a state that will occur, per the review's own "restart" should-fix class).
IMAGE_INSPECT_TIMEOUT_S = 30


def gap_final_equity(figures):
    """Lane NT2 (#452): the relative gap in final equity, (1 + Nautilus total return) /
    (1 + backtest total return) - 1, from the entrypoint's figures. None when either figure is
    missing or 1 + backtest total return is not above zero (no ratio exists)."""
    if not figures:
        return None
    nautilus = figures.get("nautilus_total_return")
    backtest = figures.get("backtest_total_return")
    if nautilus is None or backtest is None or 1 + backtest <= 0:
        return None
    return (1 + nautilus) / (1 + backtest) - 1


def _convert_real_trades_to_fixture_schema(trades_df, price_df):
    """Convert the run's own canonical 20-column trades.parquet
    (research_lab/strategies/base/vbt_columns.py's CANONICAL_TRADE_COLUMNS) into the container's
    own input schema (ENTRYPOINT_CONTRACT.md): one row per trade, with entry_bar_ts, exit_bar_ts,
    side, size, entry_fee, exit_fee, entry_vbt_price and exit_vbt_price. On disk `entry_index` and
    `exit_index` are tz-aware pandas Timestamps, the FILL bars' own timestamps (verified against a
    real run's trades.parquet: dtype datetime64[ns, UTC]), never bar positions, so each is first
    matched against price.parquet's own timestamp column (plain int64 seconds, per
    research_lab/docker/research-runner/run_backtest_entrypoint.py's _write_price) to find that
    bar's POSITION. Our own engine fills at the NEXT bar's open after the signal (#290's own
    measured "fill_pos = sig_pos + 1" finding), so a leg's SIGNAL bar is the row immediately before
    its fill bar's position, and its timestamp is what the container schedules against. An open
    trade has exit_bar_ts null and only then. A fill on bar 0 (no earlier bar to signal on) or on
    no bar of price.parquet raises a ValueError starting "derivation-failed", never a skip."""
    import pandas as pd  # function-scope: keeps this module importable on a plain interpreter
    price_sorted = price_df.sort_values("timestamp").reset_index(drop=True)
    ts_to_pos = {int(ts): pos for pos, ts in enumerate(price_sorted["timestamp"])}

    def signal_bar_ts(fill_index, label, trade_id):
        if fill_index is None or pd.isna(fill_index):
            raise ValueError(f"derivation-failed: trade {trade_id} has no {label} time")
        fill_pos = ts_to_pos.get(int(pd.Timestamp(fill_index).timestamp()))
        if fill_pos is None:
            raise ValueError(
                f"derivation-failed: trade {trade_id} {label} fill is on no bar of price.parquet")
        if fill_pos == 0:
            raise ValueError(
                f"derivation-failed: trade {trade_id} {label} fill is on bar 0, which has no "
                f"earlier bar to signal on")
        return int(price_sorted.iloc[fill_pos - 1]["timestamp"])

    rows = []
    for position, (_, trade) in enumerate(trades_df.iterrows()):
        trade_id = trade.get("trade_id", position)
        status = str(trade.get("status"))
        if status not in ("Open", "Closed"):
            raise ValueError(f"derivation-failed: trade {trade_id} has status {status!r}")
        is_open = status == "Open"
        rows.append({
            "entry_bar_ts": signal_bar_ts(trade.get("entry_index"), "entry", trade_id),
            "exit_bar_ts": None if is_open else signal_bar_ts(trade.get("exit_index"), "exit", trade_id),
            "side": str(trade.get("direction") or "long").lower(),
            "size": float(trade.get("size")),
            "entry_fee": float(trade.get("entry_fees")),
            "exit_fee": float("nan") if is_open else float(trade.get("exit_fees")),
            "entry_vbt_price": float(trade.get("avg_entry_price")),
            "exit_vbt_price": float("nan") if is_open else float(trade.get("avg_exit_price")),
        })
    frame = pd.DataFrame(rows, columns=[
        "entry_bar_ts", "exit_bar_ts", "side", "size", "entry_fee", "exit_fee",
        "entry_vbt_price", "exit_vbt_price"])
    frame["entry_bar_ts"] = frame["entry_bar_ts"].astype("int64")
    frame["exit_bar_ts"] = frame["exit_bar_ts"].astype("Int64")
    return frame


async def _run_container_with_heartbeat(cmd: list, job_id: int, http) -> tuple[int, str, bool]:
    """Run the retest container, heartbeating on run_backtest.py's own cadence and honouring a
    cancel request the same way (SIGTERM, a grace window, then SIGKILL). Returns
    (returncode, combined stdout+stderr text, cancelled). The nested watch_cancel() closure below
    is the SAME shape run_backtest.py's _stream_and_watch already uses (a private watcher `run`
    reaches transitively), which is what research_lab/worker/probes/heartbeat_contract_probe.py's
    own static call-graph walk credits: a call sitting in a closure nothing invokes does not count,
    so the asyncio.create_task(watch_cancel()) line below is load-bearing, not decorative."""
    import httpx

    from helpers import report_heartbeat

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output_chunks: list[bytes] = []
    cancelled = {"value": False}

    async def drain_stdout() -> None:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            output_chunks.append(chunk)

    async def watch_cancel() -> None:
        while proc.returncode is None:
            await asyncio.sleep(CANCEL_POLL_S)
            if proc.returncode is not None:
                return
            try:
                requested = await report_heartbeat(job_id, http)
            # Narrowed from a bare except Exception (findings round 3, B2 BLOCKER): the only
            # things report_heartbeat's own body can raise past its own internal try/except
            # (which already swallows a non-JSON response body) are httpx's own transport errors
            # (a dropped connection, a timeout, a DNS failure -- httpx.HTTPError is the base class
            # every one of those derives from) and, rarely, a bare OSError surfacing from the
            # underlying socket layer beneath httpx. Logged rather than silently discarded (this
            # module's own established plain-print convention, worker.py's own backstop uses the
            # same shape rather than the logging module, which nothing in this package imports):
            # a heartbeat that keeps failing was previously invisible, and a stuck job looked
            # identical to a healthy one from the outside.
            except (httpx.HTTPError, OSError) as exc:
                print(f"[run_retest] heartbeat failed for job {job_id}: {exc!r}")
                continue
            if requested:
                cancelled["value"] = True
                try:
                    proc.terminate()  # SIGTERM (docker run forwards it to the container)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=CANCEL_GRACE_S)
                    except asyncio.TimeoutError:
                        proc.kill()  # SIGKILL after the grace window
                except ProcessLookupError:
                    pass  # the container exited between the check and the signal -- fine
                return

    drain_task = asyncio.create_task(drain_stdout())
    watch_task = asyncio.create_task(watch_cancel())
    try:
        await drain_task  # returns once the container closes stdout (i.e. it exits)
    finally:
        watch_task.cancel()  # stop polling; the process is done (or being killed)
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        if proc.returncode is None:  # still running with no stdout reader -> never leak a container
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    rc = await proc.wait()
    return rc, b"".join(output_chunks).decode("utf-8", errors="replace"), cancelled["value"]


async def run(job: dict, http) -> None:
    from helpers import (
        HEADERS, SERVER_URL, report_job_cancelled, report_job_completed, report_job_failed,
        report_retest_result, report_run_artifacts,
    )

    job_id = job["id"]
    payload = job["payload"]
    run_id = payload["run_id"]
    matched_configuration = payload["matched_configuration"]
    residue_allowance = payload.get("residue_allowance")

    # AC-21: docker image inspect BEFORE any docker run, and before any artifact download or
    # pandas/httpx use. No retest/refusal row is ever written on this path: the worker has no DB
    # access at all, so "no row written" is structural, not a separate thing to remember to skip.
    # Bounded by IMAGE_INSPECT_TIMEOUT_S (findings round 4, SF4): this call runs before the
    # heartbeat/cancel task exists (that task starts later, inside _run_container_with_heartbeat),
    # so an unbounded call here had no way to be told apart from a live job, and no way to cancel.
    try:
        inspect = subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE],
                                  capture_output=True, text=True, timeout=IMAGE_INSPECT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Findings round 6, SF5: this except clause sits BEFORE run()'s own outer try (the one
        # guarding the httpx/pandas/engine work below), so anything that escapes here would have
        # reached worker.py's generic handle_job backstop, which reads this payload's run_id and
        # calls report_run_failed on it, wrongly flipping an already-graded run to failed. Round 5
        # only bounded TimeoutExpired; an OSError from the spawn itself (docker binary absent, or
        # the OS refusing to fork/exec under resource pressure) is a SEPARATE exception family
        # (OSError is not a SubprocessError subclass) and was still escaping uncaught. The two
        # cases are told apart by type and reported with distinguishable messages and counters,
        # never merged, because they mean different things to whoever reads the counter later.
        if isinstance(exc, subprocess.TimeoutExpired):
            RETEST_JOB_MONITOR_COUNTERS["docker_daemon_unresponsive"] += 1
            await report_job_failed(
                job_id, f"docker-daemon-unresponsive: docker image inspect exceeded "
                        f"{IMAGE_INSPECT_TIMEOUT_S}s", http)
        else:
            RETEST_JOB_MONITOR_COUNTERS["docker_spawn_failed"] += 1
            await report_job_failed(
                job_id, f"docker-spawn-failed: {exc!r}", http)
        return
    if inspect.returncode != 0:
        RETEST_JOB_MONITOR_COUNTERS["image_absent"] += 1
        await report_job_failed(
            job_id, f"nautilus-image-absent: {DOCKER_IMAGE} not present on this host "
                    f"(docker image inspect exited {inspect.returncode})", http)
        return

    # Everything past this point can raise from a third-party engine, a malformed artifact, or a
    # transient HTTP fault; ANY of those escaping here would reach worker.py's own generic
    # backstop (handle_job), which sees this payload's run_id and calls report_run_failed on it,
    # incorrectly flipping an ALREADY-GRADED run to failed over a retest-only fault. This broad,
    # explicit backstop (the same justified shape run_backtest.py's own "1.1 safety net" uses) is
    # what keeps a retest failure a JOB failure only, never a run failure.
    try:
        import httpx
        import pandas as pd

        try:
            trades_resp = await http.get(
                f"{SERVER_URL}/api/research/worker/runs/{run_id}/artifacts/trades.parquet",
                headers=HEADERS)
            trades_resp.raise_for_status()
            price_resp = await http.get(
                f"{SERVER_URL}/api/research/worker/runs/{run_id}/artifacts/price.parquet",
                headers=HEADERS)
            price_resp.raise_for_status()
            equity_resp = await http.get(
                f"{SERVER_URL}/api/research/worker/runs/{run_id}/artifacts/equity.parquet",
                headers=HEADERS)
            equity_resp.raise_for_status()
        except httpx.HTTPError as exc:
            await report_job_failed(job_id, f"artifact download failed for run {run_id}: {exc}", http)
            return

        trades_df = pd.read_parquet(io.BytesIO(trades_resp.content))
        price_df = pd.read_parquet(io.BytesIO(price_resp.content))
        fixture_trades = _convert_real_trades_to_fixture_schema(trades_df, price_df)

        tmp_dir = Path(tempfile.mkdtemp(prefix=f"retest_{job_id}_"))
        try:
            out_dir = tmp_dir / "out"
            out_dir.mkdir()
            trades_path = tmp_dir / "trades.parquet"
            bars_path = tmp_dir / "bars.parquet"
            fixture_trades.to_parquet(trades_path, index=False)
            price_df.to_parquet(bars_path, index=False)
            (tmp_dir / "equity.parquet").write_bytes(equity_resp.content)
            spec = {
                "run_id": run_id,
                "trades_path": "/fixtures/trades.parquet",
                "bars_path": "/fixtures/bars.parquet",
                "equity_path": "/fixtures/equity.parquet",
                "matched_configuration": matched_configuration,
                "residue_allowance": residue_allowance,
            }
            (tmp_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")

            cmd = [
                "docker", "run", "--rm",
                "-v", f"{tmp_dir}:/fixtures:ro",
                "-v", f"{out_dir}:/out:rw",
                "-e", "RETEST_SPEC_PATH=/fixtures/spec.json",
                "-e", "RETEST_OUTPUT_DIR=/out",
                DOCKER_IMAGE, "python3", "/app/run_retest_entrypoint.py",
            ]
            rc, combined_output, cancelled = await _run_container_with_heartbeat(cmd, job_id, http)
            if cancelled:
                # No "run" to cancel (a retest job owns no research_runs row, unlike run_backtest):
                # the job itself is the only thing this kind can cancel.
                await report_job_cancelled(job_id, http)
                return
            if rc != 0:
                await report_job_failed(
                    job_id, f"nautilus-runner container exited {rc}: {combined_output[-2000:]}", http)
                return

            result_path = out_dir / "retest_result.json"
            if not result_path.is_file():
                await report_job_failed(
                    job_id, "nautilus-runner container wrote no retest_result.json", http)
                return
            result = json.loads(result_path.read_text(encoding="utf-8"))

            # Upload the fills export under a name namespaced by job_id (the run already owns its
            # own trades.parquet/price.parquet; this is a SEPARATE artifact, never overwriting
            # either), storing that NAME (not a raw filesystem path) as export_path.
            export_name = None
            if result.get("export_path"):
                fills_path = out_dir / "retest_fills.parquet"
                if fills_path.is_file():
                    export_name = f"retest_{job_id}_fills.parquet"
                    try:
                        await report_run_artifacts(run_id, {export_name: fills_path}, http)
                    except (httpx.HTTPError, OSError) as exc:
                        await report_job_failed(job_id, f"retest fills upload failed: {exc}", http)
                        return

            status = result.get("status")
            figures = result.get("figures") or {}
            if status == "report-only":
                divergence_vs_shadow = figures.get("mean_diff_shadow")
                divergence_vs_shadow_reason = None
                divergence_vs_vbt = figures.get("mean_diff_vbt")
                divergence_vs_vbt_reason = (
                    None if divergence_vs_vbt is not None
                    else "no next bar available for at least one signal")
            else:
                divergence_vs_shadow = None
                divergence_vs_shadow_reason = result.get("could_not_run_reason")
                divergence_vs_vbt = None
                divergence_vs_vbt_reason = result.get("could_not_run_reason")

            body = {
                "run_id": run_id,
                "created_at": int(time.time()),
                "shadow_copy_completed_at": result.get("shadow_copy_completed_at"),
                "fills_read_at": result.get("fills_read_at"),
                "status": status,
                "could_not_run_reason": result.get("could_not_run_reason"),
                "engine_version": result.get("engine_version"),
                "python_version": result.get("python_version"),
                "matched_configuration": matched_configuration,
                "export_path": export_name,
                "divergence_vs_shadow": divergence_vs_shadow,
                "divergence_vs_shadow_absent_reason": divergence_vs_shadow_reason,
                "divergence_vs_vbt": divergence_vs_vbt,
                "divergence_vs_vbt_absent_reason": divergence_vs_vbt_reason,
                # Lane NT2 (#452): the two gaps the drift rule tests (final-equity ratio gap, Sharpe gap);
                # None on a could-not-run, whose figures are null.
                "gap_final_equity": gap_final_equity(figures),
                "gap_sharpe": figures.get("gap_sharpe"),
            }
            try:
                await report_retest_result(job_id, body, http)
            except httpx.HTTPError as exc:
                RETEST_JOB_MONITOR_COUNTERS["result_relay_failed"] += 1
                await report_job_failed(job_id, f"retest result relay failed: {exc}", http)
                return

            await report_job_completed(job_id, {"run_id": run_id, "status": status}, http)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    # Findings round 3, B2 BLOCKER: considered narrowing to (OSError, httpx.HTTPError) -- the
    # docker subprocess's own errors (spawn failure, e.g. FileNotFoundError if the docker binary
    # itself is missing) and the one unguarded HTTP call in this block (report_job_completed,
    # line above, the only network call in this whole try body with no except of its own already
    # around it). That pair does NOT cover everything genuinely reachable here, verified rather
    # than assumed: pd.read_parquet on a malformed artifact can raise pyarrow.lib.ArrowInvalid,
    # which subclasses ValueError (confirmed against the installed pyarrow==24.0.0:
    # ArrowInvalid.__mro__ includes ValueError), and json.loads on a malformed retest_result.json
    # raises json.JSONDecodeError, also a ValueError subclass. Adding ValueError narrows further
    # but still risks missing a shape neither pandas/pyarrow/json failure mode covers today. This
    # is the ONE site of the three where the escape hatch applies (findings round 3, B2): the
    # consequence of an exception escaping THIS function is not a harmless crash, it is
    # worker.py's own generic backstop (handle_job) reading this job's payload's run_id and
    # calling report_run_failed on an ALREADY-GRADED run that the retest never touched -- a
    # cross-contamination bug strictly worse than an over-broad catch here, so Exception stays,
    # named for the lead's ruling rather than narrowed on a guess.
    except Exception as exc:  # noqa: BLE001 -- see the comment above: this floor keeps a retest
        # fault a JOB failure only, matching run_backtest.py's own justified "1.1 safety net" shape.
        await report_job_failed(
            job_id, f"unexpected retest worker error: {type(exc).__name__}: {exc}", http)
