"""M2.5 content-hash SQL + canonical-JSON helper — single source for brief B and brief N.

Per spec brief §8.5 / EL21. The close-time query and the history-inclusive
verification query both flow through this module so a future refactor cannot
silently desync them. Brief B's `write_event_batch` calls `compute_content_hash`
at close; brief N's verification probe calls `compute_history_inclusive_hash`.

Both queries key on `batch_id` alone (no `status='active'` filter), per EL21's
amendment to arch v6 §7.1: the filter is correct for fetch / load / correction
batches but wrong for rollback batches, whose rolled-back rows are `'rolled_back'`
by the time the close-hash SELECT runs.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Union


# Close-time SELECT: every row under :batch_id, ordered deterministically.
# json_group_array's in-aggregate ORDER BY needs SQLite >= 3.44 (asserted by
# eventsdb.init_events_db); the cik/role ordering inside the companies array
# is what makes the serialization stable across machines.
CLOSE_HASH_SQL = """
SELECT e.event_type,
       e.natural_key,
       e.payload,
       e.knowledge_at,
       e.occurred_at,
       e.source,
       (SELECT json_group_array(json_object('cik', cik, 'role', role, 'cusip', cusip)
                                ORDER BY cik, role)
          FROM research_event_companies ec
         WHERE ec.event_id = e.event_id)
  FROM research_events e
 WHERE e.batch_id = :batch_id
 ORDER BY e.natural_key
"""


# History-inclusive verification SQL: UNION ALL across history + current state,
# again keyed on batch_id alone. For a freshly-closed batch the UNION collapses
# to the current-rows branch; after a correction moves an event to a new
# batch_id, the history branch picks up the pre-correction state. Per spec §8.5.
HISTORY_INCLUSIVE_HASH_SQL = """
WITH batch_x_rows AS (
  SELECT event_type, natural_key, payload, knowledge_at, occurred_at,
         source, event_id
    FROM research_events_history
   WHERE batch_id = :batch_id
  UNION ALL
  SELECT event_type, natural_key, payload, knowledge_at, occurred_at,
         source, event_id
    FROM research_events
   WHERE batch_id = :batch_id
)
SELECT bx.event_type, bx.natural_key, bx.payload, bx.knowledge_at,
       bx.occurred_at, bx.source,
       (SELECT json_group_array(json_object('cik', cik, 'role', role,
                                            'cusip', cusip)
                                ORDER BY cik, role)
          FROM research_event_companies ec
         WHERE ec.event_id = bx.event_id)
  FROM batch_x_rows bx
 ORDER BY bx.natural_key
"""


def canonical_json(text_or_obj: Union[str, bytes, dict, list, None]) -> str:
    """Parse-then-serialize a JSON-shaped value for hash determinism (MN1).

    Both `payload` and the json_group_array column arrive from SQLite as
    JSON-encoded strings. Whitespace and key-order on the producer side are
    unspecified; we round-trip through `json.loads` then `json.dumps` with
    `sort_keys=True` + `separators=(',', ':')` so the bytes are stable for
    SHA256.

    Accepts a Python dict/list directly (skips the loads step) for callers
    that already have the value parsed.
    """
    if isinstance(text_or_obj, (str, bytes)):
        obj = json.loads(text_or_obj)
    else:
        obj = text_or_obj
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _serialize_row(row: tuple) -> bytes:
    """Serialize one (event_type, natural_key, payload, knowledge_at, occurred_at,
    source, companies_json_array) row for the hash stream per §8.5.

    Format:
        event_type | natural_key | canonical_json(payload) | knowledge_at |
        (occurred_at or '') | source | canonical_json(companies)
    """
    event_type, natural_key, payload, knowledge_at, occurred_at, source, companies = row
    # Convert occurred_at NULL → empty string for stable serialization (the §8.5
    # formula's `(occurred_at or '')` term is what makes a 13D occurred-at-NULL
    # row hash the same as itself across re-runs).
    occurred_at_str = "" if occurred_at is None else str(occurred_at)
    # companies arrives as a JSON-encoded TEXT from json_group_array; canonicalize.
    # For batches with no rows in research_event_companies (probe edge case the
    # locked fixture doesn't trip but the unit tests might), the subquery returns
    # an empty array '[]'; canonical_json normalizes that to '[]'.
    companies_canonical = canonical_json(companies) if companies else "[]"
    parts = [
        str(event_type),
        str(natural_key),
        canonical_json(payload),
        str(knowledge_at),
        occurred_at_str,
        str(source),
        companies_canonical,
    ]
    return "|".join(parts).encode("utf-8")


def compute_content_hash(conn: sqlite3.Connection, batch_id: int) -> str:
    """Run CLOSE_HASH_SQL for batch_id, serialize each row per §8.5, return SHA256 hex.

    Surrogate ids (event_id, batch_id) and operational fields (ingested_at,
    version) are deliberately excluded — only content the partial UNIQUE and
    join table express.
    """
    h = hashlib.sha256()
    cursor = conn.execute(CLOSE_HASH_SQL, {"batch_id": batch_id})
    # Stream row-by-row so a million-row batch does not blow Python memory.
    # The SELECT already ORDERs deterministically; we trust that order.
    for row in cursor:
        h.update(_serialize_row(tuple(row)))
        h.update(b"\n")  # row separator; without it two adjacent fields could elide
    return h.hexdigest()


def compute_history_inclusive_hash(conn: sqlite3.Connection, batch_id: int) -> str:
    """Run HISTORY_INCLUSIVE_HASH_SQL for batch_id, return SHA256 hex.

    For a batch with no corrections / no rollback applied yet, this equals
    compute_content_hash(conn, batch_id) row-for-row. After corrections move
    events to new batches, history rows carry the pre-correction state for
    the original batch and the equality still holds (per §8.5 + SFv4-1).
    """
    h = hashlib.sha256()
    cursor = conn.execute(HISTORY_INCLUSIVE_HASH_SQL, {"batch_id": batch_id})
    for row in cursor:
        h.update(_serialize_row(tuple(row)))
        h.update(b"\n")
    return h.hexdigest()
