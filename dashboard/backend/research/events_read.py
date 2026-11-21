"""The keyset event-row read for the D2 event-data table (Brief B, R11).

Extracted from the GET /events route so the probe exercises the REAL query (B-E1..E4, including the
EXPLAIN-QUERY-PLAN index-served check) rather than a copy. The order key follows the index per filter
so every path is index-served, no temp B-tree sort (arch §4b; DDIA secondary indexes, canon 988):
  - event_type set -> newest-first by (knowledge_at, event_id) via ix_events_type_knowledge.
  - batch_id only  -> newest-first by event_id DESC, the leaf order of ix_events_batch.
  - neither        -> newest-first by event_id DESC (the rowid).
  - both           -> rides ix_events_type_knowledge with batch_id as a residual filter.
Keyset pagination (an opaque cursor, not OFFSET) so concurrent ingestion never skips or repeats a row.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


class MalformedCursor(ValueError):
    """A client-supplied after_cursor that does not parse (a 400, not a 500).

    A narrow ValueError subclass so the route catches ONLY a bad cursor — it must NOT also catch the
    json.JSONDecodeError (itself a ValueError subclass) that read_events_page raises on a store-
    corrupted payload, which is a real server fault and must fail loud as a 500.
    """


def build_events_query(
    event_type: str | None,
    batch_id: int | None,
    after_cursor: str | None,
    limit: int,
) -> tuple[str, list[Any], str]:
    """Build the (sql, params, order_key) for one keyset page. Pure — no DB access, so the probe can
    EXPLAIN the exact string the route runs. order_key is 'knowledge_at' iff event_type is set."""
    use_knowledge = event_type is not None
    order_key = "knowledge_at" if use_knowledge else "event_id"
    # Static clause fragments only; every user value is a bound parameter, so the assembled string is safe.
    clauses = ["status = 'active'"]  # a rolled-back row is not live (the live-feeds discipline)
    params: list[Any] = []
    if event_type is not None:
        clauses.append("event_type = ?"); params.append(event_type)
    if batch_id is not None:
        clauses.append("batch_id = ?"); params.append(batch_id)
    if after_cursor:
        # Keyset "after" under a DESC walk = strictly less than the cursor tuple. A malformed cursor
        # (hand-crafted, or a knowledge_at cursor sent to the event_id path after the filter changed)
        # is a client error -> raise a contextful ValueError the route maps to 400, not a bare 500.
        try:
            if use_knowledge:
                ck, ce = after_cursor.split("_", 1)  # '<knowledge_at>_<event_id>'
                clauses.append("(knowledge_at < ? OR (knowledge_at = ? AND event_id < ?))")
                params += [int(ck), int(ck), int(ce)]
            else:
                clauses.append("event_id < ?"); params.append(int(after_cursor))
        except ValueError as exc:
            raise MalformedCursor(
                f"malformed after_cursor {after_cursor!r} for order_key={order_key}: {exc}"
            ) from exc
    order = "knowledge_at DESC, event_id DESC" if use_knowledge else "event_id DESC"
    sql = (
        "SELECT event_id, event_type, knowledge_at, occurred_at, source, natural_key, payload "
        "FROM research_events WHERE " + " AND ".join(clauses) +
        f" ORDER BY {order} LIMIT ?;"
    )
    return sql, params, order_key


def read_events_page(
    conn: sqlite3.Connection,
    event_type: str | None,
    batch_id: int | None,
    after_cursor: str | None,
    limit: int,
) -> dict:
    """Run one keyset page against `conn` and return {rows, order_key, next}.

    The full parsed payload is returned via a bare json.loads that FAILS LOUD on a store-corrupted
    payload (the json_valid CHECK at write backs it; no catch-all — the live-feeds convention). `next`
    is the cursor of the last row when the page is full (there may be more), else None.
    """
    sql, params, order_key = build_events_query(event_type, batch_id, after_cursor, limit)
    use_knowledge = order_key == "knowledge_at"
    db_rows = conn.execute(sql, (*params, limit)).fetchall()
    rows = [
        {
            "event_id": r["event_id"], "event_type": r["event_type"],
            "knowledge_at": r["knowledge_at"], "occurred_at": r["occurred_at"],
            "source": r["source"], "natural_key": r["natural_key"],
            "payload": json.loads(r["payload"]),  # fails loud on a malformed payload
        }
        for r in db_rows
    ]
    nxt = None
    if len(rows) == limit:  # a full page -> there may be more; emit the keyset cursor of the last row
        last = rows[-1]
        cursor = (f"{last['knowledge_at']}_{last['event_id']}" if use_knowledge
                  else str(last["event_id"]))
        nxt = {"after_cursor": cursor}
    return {"rows": rows, "order_key": order_key, "next": nxt}
