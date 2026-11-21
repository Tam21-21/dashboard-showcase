"""M3.5 brief K — the live-feeds per-event-type summary read.

The live feeds page (spec brief §14, EL60) shows, per event type, the store's
current state: the total active-event count, the latest knowledge_at, and the
most recent few events. This module is the single read helper behind that page.

It reads research_events directly. The batch endpoints (GET /event-batches,
GET /event-batches/{id}) are batch-grain and expose no per-type event aggregates
(spec brief §16 build-time check), so a thin per-type read is the sanctioned
path, not a new design surface. Pure read: status='active' only, no writes, no
batch row.

The per-type list is enumerated from eventsdb.EVENT_TYPES (the canonical
pyarrow-free type registry), so EVERY known type appears even at zero count (the
honest empty state, §14). A future event type appears on the page with no edit to
THIS read path — its one enumerable owner is eventsdb.EVENT_TYPES; adding a type
there (together with the research_events CHECK, the output_schemas registry, and
the loader gate, whose agreement output_schemas.assert_event_type_registry_consistent
enforces at boot) is the only change. Sourcing the list from eventsdb (not from
output_schemas, which the brief originally named) keeps this read path off the
pyarrow/parquet stack — a read of a sqlite summary has no reason to import the
parquet writer.
"""
from __future__ import annotations

import json  # stdlib: parse the research_events.payload JSON column

from .. import eventsdb  # the events.db schema owner: the canonical EVENT_TYPES tuple + the reader


def summarize_event_feeds(conn, recent_n: int) -> list[dict]:
    """Per-event-type live-feed summary, read from research_events (active only).

    Args:
      conn: an open events.db connection. The route opens it INSIDE its
        asyncio.to_thread target (sqlite connections are single-thread); the
        probe injects a throwaway-DB connection on its own thread.
      recent_n: how many most-recent events to return per type (the caller
        clamps it; this helper trusts the clamped value).

    Returns one dict PER CANONICAL EVENT TYPE (eventsdb.EVENT_TYPES, sorted for
    a stable page order), even when a type has zero active events (count 0,
    latest None, recent_events []), so the page renders an honest per-type empty
    state. Each entry:
        {event_type, total_count, latest_knowledge_at,
         per_year: [{year, count}],   # ascending UTC years holding active events; [] when none
         recent_events: [
            {event_id, event_type, knowledge_at, occurred_at, source,
             natural_key, payload}   # payload parsed from its JSON column
        ]}
    """
    feeds: list[dict] = []
    # Stable page order: sort the canonical type keys (analyst_rating, sec_13d).
    for event_type in sorted(eventsdb.EVENT_TYPES):
        # Aggregate: active-event count + the latest knowledge_at for this type.
        # Equality on event_type + the aggregate over knowledge_at is served by
        # the existing composite index ix_events_type_knowledge(event_type,
        # knowledge_at); status is filtered post-walk (it is not in the index).
        agg = conn.execute(
            "SELECT COUNT(*) AS total_count, MAX(knowledge_at) AS latest_knowledge_at "
            "FROM research_events WHERE status = 'active' AND event_type = ?",
            (event_type,),
        ).fetchone()
        total_count = agg["total_count"]              # 0 when the type has no active rows
        latest_knowledge_at = agg["latest_knowledge_at"]  # NULL (None) when count is 0

        # Recent N: the most recent active events of this type, newest first. A
        # reverse index walk on (event_type, knowledge_at) with the rowid
        # (event_id, the INTEGER PRIMARY KEY) as the trailing key yields
        # knowledge_at DESC, event_id DESC with no separate sort; bounded by recent_n.
        rows = conn.execute(
            "SELECT event_id, event_type, knowledge_at, occurred_at, source, "
            "       natural_key, payload "
            "FROM research_events WHERE status = 'active' AND event_type = ? "
            "ORDER BY knowledge_at DESC, event_id DESC LIMIT ?",
            (event_type, recent_n),
        ).fetchall()

        # Per UTC calendar year of knowledge_at: active events of this type, ascending. Served by
        # ix_events_type_knowledge (event_type, knowledge_at). [] when the type has none.
        per_year = [
            {"year": y["year"], "count": y["count"]}
            for y in conn.execute(
                "SELECT CAST(strftime('%Y', knowledge_at, 'unixepoch') AS INTEGER) AS year, "
                "       COUNT(*) AS count "
                "FROM research_events WHERE status = 'active' AND event_type = ? "
                "GROUP BY year ORDER BY year",
                (event_type,),
            ).fetchall()
        ]

        recent_events: list[dict] = []
        for r in rows:
            recent_events.append({
                "event_id": r["event_id"],            # the stable per-event id
                "event_type": r["event_type"],        # echoed for the frontend formatter dispatch
                "knowledge_at": r["knowledge_at"],    # unix seconds; the "as of" time the feed sorts by
                "occurred_at": r["occurred_at"],      # unix seconds or NULL (the real-world event time)
                "source": r["source"],                # sec_edgar | canonical_loader
                "natural_key": r["natural_key"],      # the dedup key (e.g. filer_cik_accession for 13D)
                # payload carries a json_valid CHECK at write, so json.loads is safe; a
                # non-parseable payload is a real store-corruption bug and SHOULD fail loud.
                "payload": json.loads(r["payload"]),  # the per-type human-readable fields (frontend picks keys)
            })

        feeds.append({
            "event_type": event_type,                 # the type this entry summarizes
            "total_count": total_count,               # active-event count for the type (0 = honest empty state)
            "latest_knowledge_at": latest_knowledge_at,  # most recent knowledge_at, or None when empty
            "per_year": per_year,                     # [{year, count}] ascending, UTC year of knowledge_at; [] when empty
            "recent_events": recent_events,           # up to recent_n newest events, newest first
        })
    return feeds
