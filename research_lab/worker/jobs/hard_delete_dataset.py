"""Job handler: hard_delete_dataset (M3 brief B.5, DECISIONS #16).

Removes a soft-deleted dataset's parquet from the laptop, then tells the server to
write the cleaned terminal state (file_path NULL, file_bytes 0, sha256 NULL). The
backend cannot do this itself — the file is laptop-side, the backend is server-side
— so the DELETE endpoint queues this job and the worker carries it out.

Failure discipline (the reason DECISIONS #16 chose callback-after-delete over an
optimistic flip): if the file cannot be removed, the job is marked FAILED and the
hard_deleted callback is NOT called. The DB keeps file_path set (the file is still
there), so no dual-write inconsistency where the row says "gone" while the file
remains. removing returns True; an already-absent file returns False — both are
success and proceed to the callback; only a raise blocks it.

Import-light on purpose: httpx and the HTTP report helpers are imported lazily
inside run(), and annotations are lazy (`from __future__ import annotations`), so
the local probe can `import jobs.hard_delete_dataset` on a plain interpreter (no
httpx, no FastAPI) to exercise remove_dataset_file directly.
"""
from __future__ import annotations

import asyncio  # stdlib only: safe at module level, the plain-interpreter probe needs no install for it
import os
from pathlib import Path

from pathmap import to_host_path  # /data->host mapping, shared with fetch_dataset (Brief B, D-1); pathlib-only so this module stays import-light

# Worker-side data root: the same root that wrote the file (env override, else
# file-relative <root>/research_lab/data). NOT the dead paths.data_root DB setting
# (OPEN.md 2026-05-21) — no code reads that row; the worker resolves paths here.
_RESEARCH_LAB = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("RESEARCH_DATA_ROOT", _RESEARCH_LAB / "data"))


class OutsideDataRootError(Exception):
    """Raised when a dataset's stored file_path resolves outside the data root.

    Guards against ever unlinking beyond the data tree if a row's path is stale or
    tampered. A guard failure fails the job; it never falls through to an unlink.
    """


def remove_dataset_file(file_path: str, data_root: Path) -> bool:
    """Unlink one dataset parquet, guarded to stay inside data_root. Pure (no HTTP).

    Returns True if a file was removed, False if it was already absent (idempotent).
    Raises OutsideDataRootError if the resolved path is not under data_root.
    """
    target = to_host_path(file_path, data_root).resolve()  # map /data->root, then resolve symlinks/.. before the guard
    root = Path(data_root).resolve()
    if not target.is_relative_to(root):  # Python 3.9+; the containment guard
        raise OutsideDataRootError(f"refusing to delete {target}: not under data root {root}")
    try:
        target.unlink()  # remove the file
        return True
    except FileNotFoundError:
        return False  # already gone — idempotent success, still proceed to the callback


async def run(job: dict, http) -> None:
    # Lazy imports: keep module import-light for the plain-python3 local probe.
    from helpers import (
        heartbeat_loop,
        stop_heartbeat,
        report_dataset_hard_deleted,
        report_job_completed,
        report_job_failed,
        report_log,
    )

    job_id = job["id"]
    p = job["payload"]
    dataset_id = p["dataset_id"]
    file_path = p.get("file_path")

    # Heartbeat concurrently with this handler's own work. THE ONE HONEST EXCEPTION, kept here
    # rather than smoothed over: `remove_dataset_file` below is a synchronous `target.unlink()` that
    # is never awaited, so the heartbeat task genuinely CANNOT be scheduled while it runs. It is not
    # "not starved", it IS starved, for the span of one filesystem unlink. The placement is sound
    # anyway because that span is bounded and tiny -- step 0a measured the WHOLE job at 0.161 s,
    # three orders of magnitude inside the 10 s cadence -- not because nothing blocks.
    heartbeat_task = asyncio.create_task(heartbeat_loop(job_id, "hard_delete_dataset", http))
    try:
        # Guard or filesystem failure -> fail the job, do NOT call the callback (no dual-write).
        try:
            removed = remove_dataset_file(file_path, DATA_ROOT) if file_path else False
        except OutsideDataRootError as exc:
            await report_log(job_id, f"hard-delete dataset {dataset_id} refused: {exc}", http, level="error")
            await report_job_failed(job_id, str(exc), http)
            return
        except OSError as exc:  # real FS error (permissions, busy, etc.) — leave the row pointing at the file
            await report_log(job_id, f"hard-delete dataset {dataset_id} failed: {exc!r}", http, level="error")
            await report_job_failed(job_id, f"unlink failed: {exc!r}", http)
            return

        await report_log(
            job_id,
            f"hard-delete dataset {dataset_id}: {'removed' if removed else 'file already absent'} "
            f"{file_path or '(no file_path)'}",
            http,
        )
    finally:
        # Stop heartbeating the instant this handler returns, on every exit path above (the two
        # early returns and the success fall-through alike): a task that outlives the handler would
        # keep posting for a job about to report terminal (`worker_heartbeat_after_terminal` is the
        # server-side monitor for exactly that).
        await stop_heartbeat(heartbeat_task, job_id, "hard_delete_dataset")

    # File is gone (removed or already absent) -> tell the server to clean the row, then close the
    # job. HOISTED out of the try above so both terminal reports land AFTER the heartbeat has
    # stopped, matching `fake` and `fetch_dataset`. An earlier version left them inside the span and
    # justified it by claiming they sat inside this handler's own exception net -- a reason borrowed
    # from `compute_run_metrics`, which really does have an `except Exception`. This handler has no
    # such net at all, so the claim was simply false here and the divergence bought nothing except
    # a race that files benign lines against this build's own `worker_heartbeat_after_terminal`
    # monitor. Reached only on the success path: both failure branches above return early.
    await report_dataset_hard_deleted(dataset_id, http)
    await report_job_completed(job_id, {"dataset_id": dataset_id, "removed": removed}, http)
