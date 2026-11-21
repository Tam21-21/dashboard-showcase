"""Research lab laptop worker. Polls server, runs jobs, reports.

M2 §7: refactored to a KIND_HANDLERS dispatch pattern. M1 fake-job logic
moved to jobs/fake.py with no behavior change; jobs/fetch_dataset.py is new.
Shared constants and report_* helpers live in helpers.py to keep jobs/* and
worker.py from importing each other (circular).
"""
import asyncio
import signal
import sys

import httpx

from helpers import (
    CONNECT_PHASE_EXCEPTIONS,
    HEADERS,
    PERMANENT,
    PERMANENT_EXCEPTIONS,
    PHASE_CONNECT,
    PHASE_MAY_HAVE_CLAIMED,
    POLL_INTERVAL_S,
    REQUEST_TIMEOUT_S,
    SERVER_URL,
    TRANSIENT_EXCEPTIONS,
    WORKER_ID,
    WORKER_SLOTS,
    FaultTracker,
    backoff_delay,
    classify_fault,
    heartbeat_loop,
    log_event,
    report_job_failed,
    report_run_failed,
    stop_heartbeat,
)
from jobs import (
    compute_run_metrics, fake, family_union, fetch_dataset, hard_delete_dataset, run_backtest, run_generator,
    run_retest, run_sweep, run_theory,
)

KIND_HANDLERS = {
    "fake":                fake.run,
    "fetch_dataset":       fetch_dataset.run,
    "hard_delete_dataset": hard_delete_dataset.run,
    "run_backtest":        run_backtest.run,
    "run_sweep":           run_sweep.run,  # M5 1B: the universe-wide parameter-grid sweep engine
    "compute_run_metrics": compute_run_metrics.run,  # M5 Track C: the full-metrics compute-on-demand (single derive / cell re-run)
    "run_retest":          run_retest.run,  # lane NR (U.7s): the NautilusTrader predict-then-verify retest
    "run_generator":       run_generator.run,  # lane G2 (U.4 second half): compile-facts relay for PREVIEW/RUN GENERATOR
    "run_theory_search":   run_theory.run,  # lane GV2-7b: the generator v2 search of a theory over its stock group
    "run_theory_stage":    run_theory.run,  # lane GV2-7b: a generator v2 optimise or walk-forward stage
    "run_family_union":    family_union.run,  # lane GV3-FU1: the merged trial family of several searches
}

# Lane W1: the kinds that share the worker's slots (helpers.WORKER_SLOTS). A fetch is one small
# container (2 GB, 2 CPUs) reading one parquet, so several at once fit the laptop. Every other kind,
# including any kind added later, waits until no slotted job is running and then runs alone, so the
# memory-heavy sweeps, backtests and retests never overlap anything.
SLOTTED_KINDS = frozenset({"fetch_dataset"})


async def handle_job(job: dict, http: httpx.AsyncClient) -> None:
    handler = KIND_HANDLERS.get(job["kind"])
    if handler is None:
        await report_job_failed(job["id"], f"unknown job kind: {job['kind']}", http)
        return
    try:
        await handler(job, http)
    except Exception as exc:  # noqa: BLE001 — backstop: a handler that escaped its own net must not strand its run
        # M5 UU scale-defect 1.1: the prior version failed only the JOB here, so a run-bearing job whose
        # handler raised past its own top-level net (or whose net's report itself errored) left the RUN
        # stuck 'pending'/'running'. Fail the run too when the payload carries a run_id (run_backtest /
        # run_sweep both embed it); dataset jobs (fetch/delete) and fake have no run_id, so this no-ops
        # for them. Best-effort on the run leg so the job-fail floor below still runs if it errors.
        run_id = (job.get("payload") or {}).get("run_id")
        if run_id is not None:
            # Inline (not fail_run_and_job) on purpose: this is the LAST-RESORT floor, reached only when a
            # handler escaped its own net, so each leg is best-effort and independent — fail_run_and_job runs
            # the legs unguarded, so its run-leg error would skip the job-leg and strand the JOB too. A run-leg
            # failure here is surfaced to stderr (the systemd journal), never silently swallowed.
            try:
                await report_run_failed(run_id, repr(exc), http)
            except Exception as run_exc:  # noqa: BLE001 — best-effort; the job-fail below is the floor
                print(f"[worker] backstop could not fail run {run_id} (job {job['id']}): {run_exc!r}",
                      file=sys.stderr, flush=True)
        await report_job_failed(job["id"], repr(exc), http)


# The loop's except clause, built from helpers' constants rather than written
# out (spec brief §3.1). HTTPStatusError is in it because rule 3's whole status
# partition arrives through it. Anything OUTSIDE this tuple is not caught at
# all: it propagates with no phase, no verdict and no orphan record, and if a
# job had been claimed it strands with nothing in the log to say so. That cost
# is accepted rather than designed around — the alternative is widening toward
# `except Exception`, which the project's hard prohibitions forbid.
CAUGHT = TRANSIENT_EXCEPTIONS + PERMANENT_EXCEPTIONS + (httpx.HTTPStatusError,)


async def main(stop: asyncio.Event | None = None, slots: int | None = None):
    # SIGTERM / SIGINT set a flag instead of killing the process, so the
    # finally block runs the graceful offline POST. `stop` is injectable so the
    # probe suite can exercise a shutdown mid-backoff without sending real
    # signals to its own process; production passes nothing.
    stop = asyncio.Event() if stop is None else stop
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # One tracker for the whole process: streak state is shared across all three
    # request legs, because a worker whose /online failed and whose polls keep
    # failing is in ONE outage, not two (spec brief §3.2 rule 4).
    tracker = FaultTracker()
    slots = WORKER_SLOTS if slots is None else slots  # injectable for the probe suite, as `stop` is
    running: set[asyncio.Task] = set()  # slotted jobs in flight, at most `slots` of them
    orphans = 0  # process-lifetime count of polls that MAY have orphaned a claim

    def absorb(exc, *, leg, phase, status=None):
        """Classify one fault and return the backoff delay, or raise to exit.

        Raises when the verdict is permanent, or when a ceiling fires. The
        orphan record is written BEFORE any raise, because a job stranded by a
        worker that then died is the only case an operator gets no other
        evidence for.
        """
        nonlocal orphans
        verdict, is_cert = classify_fault(exc, status=status) if status is not None \
            else classify_fault(exc)
        tracker.record_failure(is_cert=is_cert)

        # Phase comes from the CALL SITE (Cowork R4). Recorded on the poll leg
        # only: /online and /offline claim nothing, so they cannot orphan.
        if phase == PHASE_MAY_HAVE_CLAIMED and leg == "poll":
            orphans += 1
            # orphan_count_since_start, not a lifetime total: the counter is
            # per process and resets on restart. It is an upper bound on
            # strands (most polls return 204 with nothing to lose), and the
            # real reconciliation is the server-side picked_up query.
            log_event("possible_orphaned_claim", worker_id=WORKER_ID, leg=leg,
                      exception=type(exc).__name__, orphan_count_since_start=orphans)

        delay = backoff_delay(tracker.consecutive - 1)
        if tracker.should_log_full():
            log_event("worker_fault", worker_id=WORKER_ID, leg=leg, phase=phase, verdict=verdict,
                      exception=f"{type(exc).__module__}.{type(exc).__name__}",
                      message=str(exc), status=status,
                      url=f"{SERVER_URL}/api/research/worker/{leg}",
                      cert_verify=is_cert, consecutive=tracker.consecutive,
                      next_delay=round(delay, 3))

        if verdict == PERMANENT:
            log_event("worker_exit_permanent", worker_id=WORKER_ID, leg=leg, phase=phase,
                      exception=f"{type(exc).__module__}.{type(exc).__name__}",
                      message=str(exc), status=status)
            raise exc
        hit = tracker.ceiling_hit()
        if hit is not None:
            log_event("worker_exit_ceiling", worker_id=WORKER_ID, ceiling=hit, leg=leg,
                      streak_length=tracker.consecutive,
                      cert_verify=is_cert, message=str(exc))
            raise exc
        return delay

    async def settle(delay):
        """Stop-responsive wait. True when shutdown was requested mid-backoff."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    async def reap(at_most: int) -> None:
        """Wait until at most `at_most` slotted jobs are still running. A finished job's exception
        is re-raised here, so it propagates out of the loop exactly as an inline handle_job's
        exception did before job slots (see the comment at the handle_job call)."""
        for task in [t for t in running if t.done()]:
            running.discard(task)
            task.result()
        while len(running) > at_most:
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                running.discard(task)
                task.result()

    async def run_alone(job: dict) -> None:
        """Run an unslotted job once every slotted job has finished. The job is already claimed,
        so it heartbeats while it waits, or it would read as stranded."""
        if running:
            waiting_heartbeat = asyncio.create_task(heartbeat_loop(job["id"], job["kind"], client))
            try:
                await asyncio.wait(running)  # never raises: each outcome is read below
            finally:
                await stop_heartbeat(waiting_heartbeat, job["id"], job["kind"])
            crashed = [t for t in running if t.exception() is not None]
            if crashed:
                # A fetch escaped its handler while this job waited. The worker stops on that, as
                # it always has, so this job will not run: fail it by name first, or it would sit
                # claimed with no heartbeat and nothing to say why.
                reason = (f"not started: the worker stopped because a fetch job running beside it "
                          f"raised {crashed[0].exception()!r}")
                run_id = (job.get("payload") or {}).get("run_id")
                if run_id is not None:
                    await report_run_failed(run_id, reason, client)
                await report_job_failed(job["id"], reason, client)
            await reap(0)  # re-raises the first crash, after the failure above is reported
        await handle_job(job, client)

    def succeeded():
        rec = tracker.record_success()
        if rec is not None:
            log_event("worker_recovered", worker_id=WORKER_ID, **rec)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        # /online is attempted ONCE, not retried in place: the next attempt at
        # establishing liveness is simply the first poll, which re-registers the
        # worker anyway. A permanent fault here still exits, because a bad token
        # or a malformed URL should not wait for the first poll to be noticed.
        # The /online leg sits INSIDE the same try/finally as the loop, so every
        # exit path attempts the graceful offline POST exactly once. Left
        # outside, a permanent /online fault would be the one exit in this file
        # that skipped it — and the worker row may already read `online` from a
        # previous run, so retracting it is the honest thing to do even when
        # this process never confirmed liveness.
        try:
            try:
                await client.post(
                    f"{SERVER_URL}/api/research/worker/online",
                    json={"worker_id": WORKER_ID},
                    headers=HEADERS,
                )
            except CAUGHT as exc:
                absorb(exc, leg="online", phase=PHASE_MAY_HAVE_CLAIMED)

            while not stop.is_set():
                # A slot must be free before polling: never claim a job this worker cannot start.
                await reap(slots - 1)
                if stop.is_set():
                    break
                # --- catch site A: the request itself -----------------------
                # Connect phase ONLY for the provably-pre-request whitelist;
                # everything else here may have claimed, including a type this
                # code does not name.
                try:
                    r = await client.get(
                        f"{SERVER_URL}/api/research/worker/poll",
                        params={"worker_id": WORKER_ID},
                        headers=HEADERS,
                    )
                except CAUGHT as exc:
                    phase = (PHASE_CONNECT if isinstance(exc, CONNECT_PHASE_EXCEPTIONS)
                             else PHASE_MAY_HAVE_CLAIMED)
                    if await settle(absorb(exc, leg="poll", phase=phase)):
                        break
                    continue

                if r.status_code == 204:
                    succeeded()
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # --- catch site B: the response arrived ----------------------
                # The server ran the route or this response would not exist, so
                # everything here is may-have-claimed BY CONSTRUCTION, with no
                # exception list consulted. This is what closes the 502 hole.
                try:
                    r.raise_for_status()
                    job = r.json()
                except CAUGHT as exc:
                    status = (exc.response.status_code
                              if isinstance(exc, httpx.HTTPStatusError) else None)
                    if await settle(absorb(exc, leg="poll",
                                           phase=PHASE_MAY_HAVE_CLAIMED,
                                           status=status)):
                        break
                    continue

                succeeded()
                # OUTSIDE the contract on purpose: an exception escaping
                # handle_job propagates, exactly as it does today. Absorbing it
                # would classify a lost job-completion report as weather and
                # strand the job silently, and worse, after the work was done.
                if job.get("kind") in SLOTTED_KINDS:
                    running.add(asyncio.create_task(handle_job(job, client)))
                else:
                    await run_alone(job)
        finally:
            # Shutdown, or an exception leaving the loop: let every slotted job in flight finish
            # (each is a claimed job; abandoning it would strand it), then report any that raised.
            crashed = []
            if running:
                await asyncio.wait(running)
                for task in running:
                    if task.exception() is not None:
                        crashed.append(task.exception())
                        log_event("slotted_job_raised", worker_id=WORKER_ID,
                                  exception=f"{type(task.exception()).__module__}."
                                            f"{type(task.exception()).__name__}",
                                  message=str(task.exception()))
                running.clear()
            try:
                await client.post(
                    f"{SERVER_URL}/api/research/worker/offline",
                    json={"worker_id": WORKER_ID},
                    headers=HEADERS,
                )
            except httpx.HTTPError as exc:
                # Narrowed from a silent `except Exception: pass`. Still
                # best-effort — the process is exiting either way and the
                # server's stale last_seen is the real liveness signal — but no
                # longer invisible. Anything outside HTTPError propagates.
                log_event("offline_post_failed", worker_id=WORKER_ID,
                          exception=f"{type(exc).__module__}.{type(exc).__name__}",
                          message=str(exc))
            if crashed:
                raise crashed[0]  # the rest are in the log above; never swallowed


if __name__ == "__main__":
    asyncio.run(main())
