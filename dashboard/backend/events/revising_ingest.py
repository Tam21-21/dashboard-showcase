"""Shared revising-source ingestion orchestrator (Benzinga unit, architecture Decision 3).

A live source (Benzinga now; the wider M3.75 multi-source channel later) re-sees the same
underlying event on every poll/backfill. The default write path (`write_event_batch`) SKIPS a
natural-key hit even when the payload changed, recording a dedup statistic and dropping the
revision — silently losing it. This orchestrator fixes that by COMPOSING the two existing
write-core primitives, without modifying either:

  - NEW records (no active row)             -> one `write_event_batch` (kind='fetch')
  - CHANGED records (active row, payload differs) -> `apply_correction` per record (the active
        row's payload is corrected in place, the prior state snapshotted to history; the
        knowledge_at / occurred_at instants STAY at the first-seen value — the documented
        restatement model, architecture Decision 3 / B1)
  - UNCHANGED records (active row, identical payload) -> skip (counted no-op)

It owns the resolve-SELECT, so it holds the `event_id` that `apply_correction` needs (the
default dedup reads and discards it). A revision that would alter the resolved company links
(`CorrectionAltersCompanyLinks` — e.g. a re-tickered company across a same-id revision) is
QUARANTINED with a clear reason, never dropped and never crashed (NFR-3). The run returns one
aggregated honest summary across the N+1 batches (one fetch batch + one correction batch each).
"""
from __future__ import annotations

import json                                                  # parse the stored payload for the content compare
from pathlib import Path
from typing import Optional, Union

from .. import eventsdb                                       # the events.db connection helpers
from . import hash_queries                                   # canonical_json for the payload compare
from . import write_core                                     # write_event_batch + apply_correction
from .errors import CorrectionAltersCompanyLinks             # the re-ticker quarantine edge

# resolved_at is the loader-injected resolution INSTANT (set fresh on every ingest), not part of the
# rating's content. Excluding it from the dedup comparison means a re-ingest of the same rating at a
# later time is correctly UNCHANGED (a no-op), not a spurious correction — otherwise every overlapping
# poll would re-correct recent ratings and grow research_events_history without bound.
_NON_CONTENT_PAYLOAD_KEYS = frozenset({"resolved_at"})


def _content_canonical(payload) -> str:
    """Canonical JSON of a payload's CONTENT — the loader-injected fields excluded. Accepts a dict
    (the incoming record) or a JSON string (the stored payload column)."""
    obj = json.loads(payload) if isinstance(payload, str) else payload
    return hash_queries.canonical_json(
        {k: v for k, v in obj.items() if k not in _NON_CONTENT_PAYLOAD_KEYS})


def resolve_ingest_actions(events_db_path: Union[str, Path, None],
                           *, records: list[dict]) -> tuple[list[dict],
                                                            list[tuple[dict, int]],
                                                            list[dict]]:
    """Partition mapped records into (new, changed, unchanged) against the committed active store.

    Each record is a canonical record dict (the write_core shape) carrying its own event_type and
    natural_key. The active store is read in BULK (one chunked indexed range read per event_type,
    not one SELECT per record) so this scales for the wider multi-source channel; the partition is
    then done in memory. Returns (new_records, changed_pairs, unchanged_records), where changed_pairs
    is a list of (record, existing_event_id) — the event_id the correction path needs.
    """
    new_records: list[dict] = []                              # no active row -> insert
    changed_pairs: list[tuple[dict, int]] = []                # active row, payload differs -> correct
    unchanged_records: list[dict] = []                        # active row, identical payload -> no-op
    by_type: dict[str, list[dict]] = {}                       # group records by event_type for the bulk read
    for rec in records:
        by_type.setdefault(rec["event_type"], []).append(rec)
    with eventsdb.get_events_conn(events_db_path) as conn:     # one read connection for the whole partition
        for event_type, recs in by_type.items():
            existing: dict[str, tuple[int, str]] = {}         # natural_key -> (event_id, content-canonical payload)
            nks = [r["natural_key"] for r in recs]
            for i in range(0, len(nks), 500):                 # chunk to stay under SQLite's parameter cap
                chunk = nks[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(                          # one indexed range read over uq_events_natural_active
                    f"SELECT event_id, natural_key, payload FROM research_events "
                    f"WHERE event_type = ? AND status = 'active' AND natural_key IN ({placeholders})",
                    (event_type, *chunk),
                ).fetchall()
                for row in rows:
                    existing[row["natural_key"]] = (row["event_id"],
                                                    _content_canonical(row["payload"]))
            for rec in recs:                                  # partition this type's records in memory
                hit = existing.get(rec["natural_key"])
                if hit is None:                               # nothing active under this natural key
                    new_records.append(rec)
                    continue
                event_id, stored_canonical = hit
                if stored_canonical == _content_canonical(rec["payload"]):  # same CONTENT -> no-op
                    unchanged_records.append(rec)
                else:                                         # a genuine revision -> route to a correction
                    changed_pairs.append((rec, event_id))
    return new_records, changed_pairs, unchanged_records


def ingest_revising_batch(events_db_path: Union[str, Path, None],
                          research_db_path: Union[str, Path, None],
                          *, source: str, records: list[dict],
                          source_descriptor: str,
                          parse_quarantines: Optional[list[tuple[dict, Exception, str]]] = None) -> dict:
    """Ingest a batch from a revising source; return an aggregated run summary.

    NEW -> one `write_event_batch` (kind='fetch'); CHANGED -> `apply_correction` per record;
    UNCHANGED -> counted no-op. A `CorrectionAltersCompanyLinks` on a changed record is
    quarantined (counted, with the offending natural_key), never dropped or crashed.

    `parse_quarantines` is the fetcher's pre-ingest failures (unmappable raw rows,
    unresolvable tickers) as (raw_record, exception, failure_step) tuples; they are forwarded
    to write_event_batch's collector so they persist durably in research_event_quarantines
    under the fetch batch (the EDGAR fetcher pattern).
    """
    new_records, changed_pairs, unchanged_records = resolve_ingest_actions(
        events_db_path, records=records,
    )

    batch_ids: list[int] = []                                 # every batch this run opened (fetch + corrections)
    correction_event_ids: list[int] = []                      # the event_ids actually corrected
    quarantine_detail: list[dict] = []                        # the company-link-edge quarantines, with reasons
    inserted = 0                                              # rows the fetch batch wrote
    quarantine_from_write = 0                                 # parse/validation quarantines inside the fetch batch

    # NEW -> one fetch batch. Open it when there are new records OR pre-ingest quarantines to
    # persist; skip entirely when there is neither (an all-revision / all-unchanged run does
    # not open an empty batch row).
    if new_records or parse_quarantines:
        result = write_core.write_event_batch(
            events_db_path,
            source=source,                                    # 'benzinga' (widened source CHECK, BZ1.4)
            kind="fetch",                                     # an API fetch, not a file load
            policy="quarantine",                              # a bad record quarantines, never aborts the batch
            records=new_records,
            source_descriptor=source_descriptor,
            research_db_path=research_db_path,                # so the clean-close path refreshes the event catalog
            parse_quarantines_collector=parse_quarantines,    # the fetcher's pre-ingest quarantines persist here
        )
        batch_ids.append(result.batch_id)
        inserted = result.event_count
        quarantine_from_write = result.quarantine_count

    # CHANGED -> a correction per record. apply_correction opens its own correction batch and
    # snapshots the prior state to history; the target is always a prior-committed row (a record is
    # NEW xor CHANGED within one run), so its parent batch is never the fetch batch above.
    corrected = 0
    for rec, event_id in changed_pairs:
        try:
            cres = write_core.apply_correction(
                events_db_path,
                event_id=event_id,
                corrected_payload=rec["payload"],
                companies=rec["companies"],                   # must match the stored (cik, role, cusip) triple
                trigger_note=f"{source} revision: payload changed "
                             f"(natural_key={rec['natural_key']})",
            )
            batch_ids.append(cres.batch_id)
            correction_event_ids.append(event_id)
            corrected += 1
        except CorrectionAltersCompanyLinks as exc:           # the re-ticker edge: the resolved CIK changed
            quarantine_detail.append({                        # quarantine, never silently drop the revision
                "natural_key": rec["natural_key"],
                "reason": f"correction would alter company links: {exc}",
            })

    return {
        "source": source,
        "inserted": inserted,                                 # new events written
        "corrected": corrected,                               # revisions applied
        "unchanged": len(unchanged_records),                  # re-ingest no-ops
        "quarantined": len(quarantine_detail) + quarantine_from_write,  # company-link edges + write-time quarantines
        "quarantine_detail": quarantine_detail,               # the company-link-edge specifics
        "batch_ids": batch_ids,                               # the N+1 batch ids (audit)
        "correction_event_ids": correction_event_ids,         # the corrected event ids (audit)
    }
