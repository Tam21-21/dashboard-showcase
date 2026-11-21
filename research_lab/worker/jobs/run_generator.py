"""Job handler: run_generator (lane G2, U.4 second half, PREVIEW/RUN GENERATOR;
docs/plans/night/2026-09-14_set9a_night/PLAN.md section 11, corrected by
docs/plans/night/2026-09-20/PLAN.md section 8).

Runs compile_facts.py -- the ONE file in the whole generator unit that imports vectorbtpro --
inside research-runner:m3-latest, over a theory this job downloads from the backend (the worker
has no database access at all, the same rule every job handler in this package already follows).
This handler NEVER imports vectorbtpro on the host and never runs the generation loop itself: it
only produces the compiled-facts JSON and relays it to the backend, which runs
research_lab.generator.loop.generate in a thread (the backend has database access and does not
need vbt). Import-light on purpose (the run_backtest.py / run_retest.py convention): httpx is
imported inside run(), so this module stays importable on a plain interpreter.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

DOCKER_IMAGE = "research-runner:m3-latest"

# research_lab/ itself: research_lab/worker/jobs/run_generator.py -> parents[0]=jobs,
# [1]=worker, [2]=research_lab (run_backtest.py's own _RESEARCH_LAB convention, copied here so
# this handler resolves the mount root regardless of where the worker process runs from).
_RESEARCH_LAB = Path(__file__).resolve().parents[2]

# DECISIONS #204, operational dial, inline justification only: run_retest.py's own
# IMAGE_INSPECT_TIMEOUT_S uses this exact bound for the identical call (a local metadata read off
# an already-known image, no network call), so the same generous headroom applies here.
IMAGE_INSPECT_TIMEOUT_S = 30

# The ops routing law's counter for the failure modes this job can hit before it ever reaches
# (or after it leaves) a container. In-process, module-level, the project's own established idiom
# for a counter whose reader is owed rather than built in the same change
# (research_lab/CLAUDE.md's own IBKR_FEE_MONITOR_COUNTERS precedent). image_absent is recorded in
# docs/units/G2/monitor_rows.md as generator_jobs_failed_image_absent. theory_fetch_failed and
# facts_relay_failed are the set 16b lane WD (#423) additions, docs/units/WD/monitor_rows.md:
# every non-2xx the worker sees on either of its two worker-route HTTP calls now counts here, so a
# worker route shipped broken (or answering the unknown-/api/-path 200 finding 1 fixed) is visible
# as a rising number rather than only as a report_job_failed line nobody watches.
RUN_GENERATOR_MONITOR_COUNTERS = {
    "image_absent": 0,
    "theory_fetch_failed": 0,
    "facts_relay_failed": 0,
}


async def run(job: dict, http) -> None:
    from helpers import (
        fetch_theory_content, heartbeat_loop, report_generator_facts, report_job_failed,
        stop_heartbeat,
    )

    job_id = job["id"]
    payload = job["payload"]
    theory_id = payload["theory_id"]
    theory_sha256 = payload["theory_sha256"]

    # AC-21-style precondition (run_retest.py's own idiom): docker image inspect BEFORE any
    # docker run, before any artifact/theory download, and before the heartbeat task exists. No
    # job state depends on a live daemon or on vbt before this check runs.
    try:
        inspect = subprocess.run(
            ["docker", "image", "inspect", DOCKER_IMAGE],
            capture_output=True, text=True, timeout=IMAGE_INSPECT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # A wedged daemon (TimeoutExpired) and a spawn failure (OSError, e.g. the docker binary
        # itself missing) are different failures with different operator responses, so they are
        # reported with distinguishable messages, the same distinction run_retest.py's own
        # docker_daemon_unresponsive / docker_spawn_failed split makes for the identical call.
        await report_job_failed(
            job_id, f"docker-unresponsive: docker image inspect for {DOCKER_IMAGE} failed: {exc!r}", http)
        return
    if inspect.returncode != 0:
        RUN_GENERATOR_MONITOR_COUNTERS["image_absent"] += 1
        await report_job_failed(
            job_id, f"research-runner-image-absent: {DOCKER_IMAGE} not present on this host "
                    f"(docker image inspect exited {inspect.returncode})", http)
        return

    # The worker has no database access at all (research_lab/CLAUDE.md, "How a job runs"), so the
    # theory's own saved content is fetched over the worker's own theories route (set 16b lane WD
    # AC-1, helpers.fetch_theory_content), never read off a local file the worker happens to hold.
    # Set 16b finding 1: the OLD call here hit the browser route with no worker key at all, and an
    # unknown /api/ path used to answer 200, so a bad sha256 died on `.json()["content"]` with a
    # bare KeyError instead of reaching this except clause; both are fixed by the route move.
    import httpx

    try:
        content = await fetch_theory_content(theory_sha256, http)
    except httpx.HTTPError as exc:
        RUN_GENERATOR_MONITOR_COUNTERS["theory_fetch_failed"] += 1
        await report_job_failed(
            job_id, f"theory content fetch failed for sha256={theory_sha256}: {exc}", http)
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"run_generator_{job_id}_"))
    try:
        theories_dir = tmp_dir / "theories"
        theories_dir.mkdir()
        theory_filename = f"{theory_id}.md"
        (theories_dir / theory_filename).write_text(content, encoding="utf-8")

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{_RESEARCH_LAB}:/work:ro",
            "-v", f"{theories_dir}:/theories:ro",
            "-w", "/work",
            DOCKER_IMAGE,
            "python", "generator/compile_facts.py", f"/theories/{theory_filename}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        # Heartbeat concurrently with the container while it runs (research_lab/CLAUDE.md trap 7:
        # an eighth job kind that skips this fails the committed heartbeat_contract_probe.py,
        # which resolves reachability from `run` outward). Placed directly in run()'s own scope,
        # the same flat shape fetch_dataset.py's own heartbeat_task line uses, rather than inside
        # a nested helper, so the probe's static call-graph walk finds it with no closure to cross.
        heartbeat_task = asyncio.create_task(heartbeat_loop(job_id, "run_generator", http))
        try:
            stdout, stderr = await proc.communicate()
        finally:
            await stop_heartbeat(heartbeat_task, job_id, "run_generator")

        if proc.returncode != 0:
            await report_job_failed(
                job_id, f"compile_facts.py exited {proc.returncode}: "
                        f"{stderr.decode('utf-8', errors='replace')[-2000:]}", http)
            return
        try:
            facts_payload = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            await report_job_failed(job_id, f"compile_facts.py produced invalid JSON: {exc}", http)
            return
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Relay the compiled facts to the backend, which runs the host-side generation loop itself
    # (the backend has database access and never needs vbt; this worker never calls loop.generate
    # and never marks the job terminal on success -- the backend's own /facts handler does that
    # once loop.generate has actually run, since only it knows the real per-pair outcome).
    try:
        await report_generator_facts(job_id, facts_payload, http)
    except httpx.HTTPError as exc:
        RUN_GENERATOR_MONITOR_COUNTERS["facts_relay_failed"] += 1
        await report_job_failed(job_id, f"facts relay failed: {exc}", http)
        return
