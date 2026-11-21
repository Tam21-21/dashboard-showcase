"""Fake-job handler. M1 behavior preserved verbatim except for the heartbeat, refactored to the
KIND_HANDLERS dispatch signature.

Stranded-job cleaner step 1 (spec brief §3.1): the heartbeat used to sit inside the log loop,
gated on a 30-second constant, so it fired only when a log line happened to come round AND
30 seconds had passed since the last one. That constant was this module's only consumer and it is
deleted with this change, so it is described here rather than named -- a docstring pointing at a
symbol that no longer exists is a citation the tree contradicts. Step 0a measured the achieved gap at 180.206 s against
an intended 30 for a ``duration_seconds=180, log_lines=1`` payload, and the brief ruled in advance
that an unbounded gap is repaired by moving the heartbeat, not by inflating the threshold to cover
it. So the heartbeat now runs on its own concurrent task, on the same shape every other kind uses.
"""
import asyncio

import httpx

from helpers import (
    heartbeat_loop,
    stop_heartbeat,
    report_job_completed,
    report_log,
)


async def run(job: dict, http: httpx.AsyncClient) -> None:
    job_id   = job["id"]
    payload  = job["payload"]
    duration  = float(payload.get("duration_seconds", 5))
    log_lines = int(payload.get("log_lines", 10))
    interval  = duration / max(log_lines, 1)

    # Start the heartbeat task before the log loop, so a long-duration / few-log-lines payload
    # (the exact shape that measured 180.206 s under the old gate) still heartbeats at the cadence.
    heartbeat_task = asyncio.create_task(heartbeat_loop(job_id, "fake", http))
    try:
        for i in range(log_lines):
            await asyncio.sleep(interval)
            await report_log(job_id, f"fake step {i+1}/{log_lines}", http)
    finally:
        # Stop heartbeating the instant this job's own work is done: a task that outlives the
        # handler would keep posting for a job about to report terminal (the failure mode
        # `worker_heartbeat_after_terminal` exists to catch on the server side).
        await stop_heartbeat(heartbeat_task, job_id, "fake")

    await report_job_completed(job_id, {"fake_metric": 1.23}, http)
