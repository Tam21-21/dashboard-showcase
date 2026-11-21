"""Heartbeat reaper + secondary sweep — recovery paths for the event store.

Ships per spec brief v4 §11 brief I + arch v6 §5.1 / §5.2 / §11 §20 SFv5-3.

This module is brief D-ready (the EDGAR fetcher imports `reap_orphaned_running_batches`
and `secondary_sweep` at script step 0a / 0b) and brief I-complete (the
two functions ship their full bodies here, not stubs).

Public surface
--------------
reap_orphaned_running_batches(events_db_path, *, heartbeat_stale_seconds=300)
    -> ReapResult
  Step 0a per arch §5.2: find every batch in `status='running'` whose
  `last_heartbeat_at` is older than `:heartbeat_stale_seconds`, flip it to
  `failed` with `failure_class='OrphanedRunningBatch'`, then roll back
  every active event behind those flipped batches via the same failed-event
  rollback the write_core uses on its own abort path.

secondary_sweep(events_db_path) -> SweepResult
  Step 0b per arch §5.2 / round-5 correction 1: find every batch already
  in `failed` or `cancelled_partial` that still has `status='active'`
  events behind it (the SIGTERM-killed-mid-rollback case, or anything
  else that ended a batch without completing event rollback), and run the
  failed-event rollback over those events. For `cancelled_partial` batches
  the sweep additionally upgrades `quality_report` to the
  `cancelled_partial_stub` shape per §7.0.2 / §7.7. Idempotent: running it
  twice in a row over the same DB is a no-op.

Concurrency
-----------
Both functions run on `events.db` via the same `events_transaction()` /
`get_events_conn()` wrappers `write_core` uses. The CAS guard in
`_cas_chunk_commit` makes the writer-vs-reaper race safe at the writer
end: a chunk-commit's CAS UPDATE returns 0 rows once the reaper flips a
batch to `failed`, the writer surfaces `_ReaperFlippedBatch`, and the
secondary sweep picks up any events the writer had committed before the
flip.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from .. import eventsdb
from . import hash_queries

log = logging.getLogger("dashboard.events.sweeps")


# Default stale threshold per spec brief §5 (`heartbeat_stale_seconds=300`).
# With the §5.2 step 2a K=10 / T=30 heartbeat cadence this gives ≥10× margin.
DEFAULT_HEARTBEAT_STALE_SECONDS = 300


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReapResult:
    """Return shape for `reap_orphaned_running_batches`."""
    batches_reaped: list[int] = field(default_factory=list)
    events_rolled_back: int = 0


@dataclass(frozen=True)
class SweepResult:
    """Return shape for `secondary_sweep`."""
    batches_swept: list[int] = field(default_factory=list)
    events_rolled_back: int = 0
    cancelled_partial_stubs_written: int = 0


# ---------------------------------------------------------------------------
# Shared helper: roll back every active event behind a batch (§5.1 step 3)
# ---------------------------------------------------------------------------


def _rollback_active_events_for_batch(conn, batch_id: int) -> int:
    """INSERT history snapshots + UPDATE status='rolled_back' for each active row.

    Walks `research_events WHERE batch_id=? AND status='active'`. Returns
    the count rolled back. Same shape as `write_core`'s self-rollback on
    `abort_on_first_error`: the history row's `correction_batch_id` is set
    to `batch_id` itself (the failed/cancelled batch's own id) per spec
    §3.2's failed-event-rollback semantics.
    """
    now = int(time.time())
    rolled = 0
    rows = list(conn.execute(
        "SELECT * FROM research_events WHERE batch_id=? AND status='active'",
        (batch_id,),
    ).fetchall())
    for ev in rows:
        # History snapshot with correction_batch_id = batch_id (self-rollback).
        conn.execute(
            """INSERT INTO research_events_history
                   (event_id, event_type, status, producer_id, natural_key,
                    occurred_at, knowledge_at, ingested_at, source, origin,
                    batch_id, payload, payload_schema_version, raw_input,
                    version, corrected_at, correction_batch_id, prev_version)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ev["event_id"], ev["event_type"], ev["producer_id"],
             ev["natural_key"], ev["occurred_at"], ev["knowledge_at"],
             ev["ingested_at"], ev["source"], ev["origin"], ev["batch_id"],
             ev["payload"], ev["payload_schema_version"], ev["raw_input"],
             ev["version"], now, batch_id, ev["version"]),
        )
        conn.execute(
            "UPDATE research_events SET status='rolled_back' WHERE event_id = ?",
            (ev["event_id"],),
        )
        rolled += 1
    return rolled


# ---------------------------------------------------------------------------
# Primary sweep — heartbeat reaper
# ---------------------------------------------------------------------------


def reap_orphaned_running_batches(
    events_db_path: Union[str, Path, None] = None,
    *,
    heartbeat_stale_seconds: int = DEFAULT_HEARTBEAT_STALE_SECONDS,
) -> ReapResult:
    """Flip stale-heartbeat running batches → failed + roll back their events.

    Brief I per spec brief v4 §11. Runs under flock at fetcher step 0a and
    inside the dashboard's startup lifespan + hourly background task. The
    partial index `ix_batches_running_heartbeat` is the access path: a
    fast scan over just the live-batch set, not a full-table scan.

    Per arch §5.1 (the SQL): the UPDATE uses CAS on `status='running'` so
    a writer that closes its batch between SELECT and UPDATE is not
    over-written by the reaper. Then for each batch the reaper flipped, the
    failed-event rollback walks the active events and flips them to
    `status='rolled_back'`, freeing their natural_keys under the partial
    UNIQUE so a re-dispatch dedups cleanly.

    Idempotent: a re-run finds no stale `running` batches if the prior run
    already flipped them.
    """
    cutoff = int(time.time()) - heartbeat_stale_seconds
    reaped_ids: list[int] = []
    total_rolled = 0

    with eventsdb.events_transaction(events_db_path) as conn:
        # Discover stale batches first (one statement keys on the partial
        # index; SQLite's RETURNING returns the rows the UPDATE actually
        # touched so the reap is atomic at the row level).
        rows = conn.execute(
            """UPDATE research_event_batches
                  SET status         = 'failed',
                      failure_class  = 'OrphanedRunningBatch',
                      finished_at    = ?
                WHERE status='running'
                  AND last_heartbeat_at < ?
            RETURNING batch_id""",
            (int(time.time()), cutoff),
        ).fetchall()
        reaped_ids = [r["batch_id"] for r in rows]
        # Roll back active events behind each reaped batch, inside the same
        # transaction so the reap + rollback land atomically.
        for bid in reaped_ids:
            total_rolled += _rollback_active_events_for_batch(conn, bid)

    if reaped_ids:
        log.warning(
            "reap_orphaned_running_batches: flipped %d batch(es) to failed/"
            "OrphanedRunningBatch (stale_seconds=%d); rolled back %d active event(s)",
            len(reaped_ids), heartbeat_stale_seconds, total_rolled,
        )
    return ReapResult(batches_reaped=reaped_ids, events_rolled_back=total_rolled)


# ---------------------------------------------------------------------------
# Secondary sweep — strands from prior runs + cancelled_partial upgrade
# ---------------------------------------------------------------------------


def secondary_sweep(
    events_db_path: Union[str, Path, None] = None,
) -> SweepResult:
    """Roll back active events under `failed` / `cancelled_partial` batches.

    Brief I per spec brief v4 §11 / arch §5.2 step 0b / round-5 correction 1.
    Load-bearing recovery path: a SIGTERM-killed-mid-rollback fetcher leaves
    its batch in `cancelled_partial` with active events behind it (the
    bounded SIGTERM handler at arch §11 §20 SFv5-3 cannot afford the
    rollback work inline); this sweep is what completes the rollback so the
    natural_keys free up for re-dispatch.

    Behaviour:
      - For each batch in `('failed', 'cancelled_partial')` that has at
        least one `research_events` row still `status='active'`, run the
        failed-event rollback (same shape as `write_core`'s abort path).
      - For each `cancelled_partial` batch swept, UPDATE its `quality_report`
        to the `cancelled_partial_stub` JSON shape per §7.0.2 / §7.7. This
        upgrade happens only on the first sweep over each batch (subsequent
        sweeps see zero active events and skip the upgrade; idempotent).
      - Returns ()/(0)/(0) on the no-op tick.
    """
    swept_ids: list[int] = []
    total_rolled = 0
    stubs_written = 0

    with eventsdb.events_transaction(events_db_path) as conn:
        # Pick the batches that still carry active events. EXISTS gives a
        # fast filter on the partial-active set without a JOIN that drags
        # row counts. The `IN ('failed','cancelled_partial')` mirrors the
        # secondary-sweep predicate spec brief §5.2 / arch §5.2 step 0b name.
        candidate_rows = list(conn.execute(
            """SELECT batch_id, status
                 FROM research_event_batches
                WHERE status IN ('failed','cancelled_partial')
                  AND EXISTS (
                      SELECT 1 FROM research_events
                       WHERE research_events.batch_id = research_event_batches.batch_id
                         AND status='active'
                  )""",
        ).fetchall())
        for row in candidate_rows:
            bid = row["batch_id"]
            status_was = row["status"]
            n = _rollback_active_events_for_batch(conn, bid)
            swept_ids.append(bid)
            total_rolled += n
            # Upgrade the quality_report for cancelled_partial batches.
            if status_was == "cancelled_partial":
                stub = {
                    "shape": "cancelled_partial_stub",
                    "rolled_back_event_count": n,
                    "swept_at": int(time.time()),
                    "swept_by": "secondary_sweep",
                }
                conn.execute(
                    "UPDATE research_event_batches SET quality_report=? WHERE batch_id=?",
                    (hash_queries.canonical_json(stub), bid),
                )
                stubs_written += 1

    if swept_ids:
        log.warning(
            "secondary_sweep: rolled back %d active event(s) across %d batch(es); "
            "wrote %d cancelled_partial_stub(s)",
            total_rolled, len(swept_ids), stubs_written,
        )
    return SweepResult(
        batches_swept=swept_ids,
        events_rolled_back=total_rolled,
        cancelled_partial_stubs_written=stubs_written,
    )
