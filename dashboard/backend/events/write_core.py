"""M2.5 shared write core — write_event_batch, apply_correction, apply_rollback.

Brief B per spec brief v4 §11 / EL36. Three public functions; all reuse the
single-source close-hash machinery from `events/hash_queries.py`.

Public surface
--------------
write_event_batch(events_db_path, *, source, kind, policy, records,
                  request_id, source_descriptor, commit_chunk_size=1000,
                  parent_batch_id=None) -> BatchResult
    For kind in ('fetch', 'load'). Chunked commits, per-type validation,
    partial-UNIQUE dedup, CAS-guarded heartbeat, content_hash at close,
    `record_level` quality report builder.

apply_correction(events_db_path, *, event_id, corrected_payload, companies,
                 trigger_note) -> BatchResult
    Single-row UPDATE-in-place + history INSERT. Asserts target batch is
    not 'running' (CorrectionAgainstRunningBatch). Asserts incoming
    `companies` set equals stored (CorrectionAltersCompanyLinks).

apply_rollback(events_db_path, *, target_batch_id, trigger_note) -> BatchResult
    Bulk flip per arch §7.2. Moves every active event under target_batch_id
    to the new rollback batch with status='rolled_back'.

Records (the iterator passed to write_event_batch) are plain dicts with these
keys per spec brief §4.4 envelope (the loader/fetcher resolves company-link
fields BEFORE calling write_event_batch; CIKs arrive in `companies[i].cik`,
not as `raw_ticker`):

    {
      "event_type":             "sec_13d" | "analyst_rating" | "news",
      "natural_key":            str,
      "producer_id":            str | None,
      "knowledge_at":           int,
      "occurred_at":            int | None,
      "source":                 "sec_edgar" | "canonical_loader" | "benzinga" | "alpaca_news",
      "origin":                 "fetched" | "loaded" | "imported",
      "payload_schema_version": int,
      "raw_input":              str | None,
      "payload":                dict,                 # per-type validated
      "companies":              list[{cik, role, cusip}],
      "accession_number":       str | None,           # for EDGAR quarantine row
    }
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from .. import eventsdb
from . import hash_queries
from . import types as event_types
from .errors import (
    CorrectionAgainstRunningBatch,
    CorrectionAltersCompanyLinks,
    LoaderEnvelopeInvalid,
    LoaderJsonDecodeError,
    MissingRequiredField,
    PayloadValidationError,
    RecordValidationAbort,
    TickerNotFoundInBridge,
    UnknownField,
    WrongFieldType,
)

# Brief E: loader-side per-line exception classes that the iterator raises
# under policy='abort_on_first_error'. Mirror image of PayloadValidationError →
# RecordValidationAbort: the loader classes are themselves the batch-level
# failure_class string (per spec brief §7.2 duality), so the abort path
# records the raising class verbatim rather than escalating to a wrapper
# class.
_LOADER_ABORT_CLASSES: tuple = (
    LoaderJsonDecodeError,
    LoaderEnvelopeInvalid,
    TickerNotFoundInBridge,
)

# Tuning constants per spec brief §5 EL38.
DEFAULT_COMMIT_CHUNK_SIZE = 1000          # write_event_batch chunk size
QUARANTINE_RAW_INPUT_MAX_BYTES = 65_536   # 64 KiB cap on quarantine raw_input
FAILURE_MESSAGE_MAX_BYTES = 4_096         # 4 KiB cap on quarantine failure_message
# Sample caps on the record_level quality report (§7.1 field notes).
QUARANTINES_SAMPLES_MAX = 10
DEDUP_COLLISIONS_MAX = 50

# §7.0.1 progressive-snapshot toggle: M2.5 default OFF (the reaper's
# failure_context carries enough forensic state at M2.5 volumes).
ENABLE_PROGRESSIVE_SNAPSHOT = False


# ---------------------------------------------------------------------------
# Result + report shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchResult:
    """Return value from the three public write functions."""
    batch_id: int
    status: str                      # one of the spec §3.4 status enum values
    event_count: int                 # rows actually written to research_events
    quarantine_count: int
    content_hash: Optional[str]      # NULL for failed pre-record-iteration batches
    failure_class: Optional[str]     # NULL for succeeded* states
    quality_report: dict             # parsed JSON, ready for the renderers
    # Brief H: per-(event_type, source) catalog refresh outcomes from the
    # clean-close path. Empty list if write_event_batch was called without
    # `research_db_path` (the catalog refresh is opt-in: probes targeting
    # only events.db pass research_db_path=None to keep tests isolated;
    # the fetcher script and the loader endpoint pass the real path).
    # `dict` rather than CatalogRefreshResult so the dataclass keeps no
    # cross-module reference and the field round-trips through JSON cleanly.
    catalog_refreshes: list = field(default_factory=list)


@dataclass
class _WriteState:
    """Accumulator for the record_level quality report (§7.1) inside one batch.

    Builds up as records iterate; flushed into a `record_level`-shaped dict at
    close via `_build_record_level_report()`.
    """
    records_seen: int = 0
    events_written: int = 0
    dedup_hits: int = 0
    dedup_collisions_with_changed_payload: int = 0
    quarantines: int = 0
    provisional_fields_populated: int = 0
    quarantines_by_class: dict = field(default_factory=dict)
    quarantines_samples: list = field(default_factory=list)
    dedup_collisions: list = field(default_factory=list)
    natural_key_prefix_counts: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_batch(conn: sqlite3.Connection, *, source: str, kind: str,
                policy: str, source_descriptor: str,
                request_id: Optional[int],
                parent_batch_id: Optional[int]) -> int:
    """INSERT a `running` batch row and return its batch_id.

    Per EL29 / round-5 correction 3: last_heartbeat_at is NOT NULL; we write
    `now` on the initial INSERT so the column is honest from the moment the
    batch exists.
    """
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO research_event_batches
               (request_id, kind, parent_batch_id, source, policy,
                source_descriptor, started_at, last_heartbeat_at, status,
                event_count, quarantine_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, 0)""",
        (request_id, kind, parent_batch_id, source, policy,
         source_descriptor, now, now),
    )
    return cur.lastrowid


def _cas_chunk_commit(conn: sqlite3.Connection, batch_id: int,
                      *, event_count_delta: int = 0,
                      quarantine_count_delta: int = 0) -> bool:
    """Run the CAS-guarded UPDATE on the batch row. Return True iff the row was 'running'.

    The single statement updates last_heartbeat_at + cached counters (so the
    quality-report builder reads them off the row at close instead of from
    Python locals) and uses RETURNING 1 as the witness. If the reaper has
    flipped the batch to 'failed' since the last chunk, RETURNING is empty
    and we return False so the caller can ROLLBACK the chunk and abort.

    Per EL19 / round-4 BLv5-1b. Spec brief §3.4 names the partial index
    `ix_batches_running_heartbeat` to serve the reaper sweep; the CAS-guard
    UPDATE here is what makes the reaper's flip race-safe at the writer
    end.
    """
    now = int(time.time())
    cur = conn.execute(
        """UPDATE research_event_batches
              SET last_heartbeat_at = ?,
                  event_count       = event_count + ?,
                  quarantine_count  = quarantine_count + ?
            WHERE batch_id = ? AND status = 'running'
        RETURNING 1""",
        (now, event_count_delta, quarantine_count_delta, batch_id),
    )
    rows = cur.fetchall()
    return len(rows) == 1


def _close_batch_cas(conn: sqlite3.Connection, batch_id: int,
                     *, status: str, content_hash: Optional[str],
                     quality_report_json: str,
                     failure_class: Optional[str] = None,
                     failure_context_json: Optional[str] = None) -> bool:
    """Run the CAS-guarded close UPDATE. Return True iff the batch was 'running'.

    Status transitions out of 'running' atomically; on success the function
    leaves the row in its new terminal state. On CAS failure (reaper got
    here first) the caller must NOT clobber the reaper's failure_class.
    """
    now = int(time.time())
    cur = conn.execute(
        """UPDATE research_event_batches
              SET finished_at    = ?,
                  status         = ?,
                  content_hash   = ?,
                  quality_report = ?,
                  failure_class  = COALESCE(?, failure_class),
                  failure_context = COALESCE(?, failure_context)
            WHERE batch_id = ? AND status = 'running'
        RETURNING 1""",
        (now, status, content_hash, quality_report_json,
         failure_class, failure_context_json, batch_id),
    )
    return len(cur.fetchall()) == 1


def _insert_event_and_companies(conn: sqlite3.Connection, *, record: dict,
                                batch_id: int) -> int:
    """INSERT one event row + its companies rows. Return the new event_id."""
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO research_events
               (event_type, status, producer_id, natural_key,
                occurred_at, knowledge_at, ingested_at, source, origin,
                batch_id, payload, payload_schema_version, raw_input, version)
           VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (record["event_type"], record.get("producer_id"), record["natural_key"],
         record.get("occurred_at"), record["knowledge_at"], now,
         record["source"], record["origin"], batch_id,
         hash_queries.canonical_json(record["payload"]),
         record["payload_schema_version"], record.get("raw_input")),
    )
    event_id = cur.lastrowid
    for entry in record["companies"]:
        conn.execute(
            """INSERT INTO research_event_companies (event_id, cik, role, cusip)
               VALUES (?, ?, ?, ?)""",
            (event_id, entry["cik"], entry["role"], entry.get("cusip")),
        )
    return event_id


def _insert_history_snapshot(conn: sqlite3.Connection, *, event_row: sqlite3.Row,
                             correction_batch_id: int,
                             new_status_for_history: str) -> None:
    """INSERT one research_events_history row capturing the pre-change current state.

    `event_row` is the SELECT * result against research_events at the point of
    capture. `new_status_for_history` is what the history row records as its
    own status field (per spec §3.2: the history row captures the state that
    is being superseded).
    """
    now = int(time.time())
    conn.execute(
        """INSERT INTO research_events_history
               (event_id, event_type, status, producer_id, natural_key,
                occurred_at, knowledge_at, ingested_at, source, origin,
                batch_id, payload, payload_schema_version, raw_input,
                version, corrected_at, correction_batch_id, prev_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_row["event_id"], event_row["event_type"], new_status_for_history,
         event_row["producer_id"], event_row["natural_key"],
         event_row["occurred_at"], event_row["knowledge_at"],
         event_row["ingested_at"], event_row["source"], event_row["origin"],
         event_row["batch_id"], event_row["payload"],
         event_row["payload_schema_version"], event_row["raw_input"],
         event_row["version"], now, correction_batch_id,
         event_row["version"]),
    )


def _quarantine_record(conn: sqlite3.Connection, *, batch_id: int,
                       record: dict, exc: Exception,
                       failure_step: str) -> int:
    """INSERT one research_event_quarantines row. Return its quarantine_id.

    Bounds raw_input to 64 KiB and failure_message to 4 KiB per spec §5 / EL38.
    """
    now = int(time.time())
    raw_input = record.get("raw_input") or ""
    if isinstance(raw_input, str):
        excerpt = raw_input.encode("utf-8")[:QUARANTINE_RAW_INPUT_MAX_BYTES].decode(
            "utf-8", errors="replace"
        )
    else:
        excerpt = str(raw_input)[:QUARANTINE_RAW_INPUT_MAX_BYTES]
    msg = str(exc)[:FAILURE_MESSAGE_MAX_BYTES]
    cur = conn.execute(
        """INSERT INTO research_event_quarantines
               (batch_id, event_type, raw_input, accession_number,
                failure_step, failure_class, failure_message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, record["event_type"], excerpt,
         record.get("accession_number"),
         failure_step, type(exc).__name__, msg, now),
    )
    return cur.lastrowid


def _build_record_level_report(state: _WriteState, *,
                               terminal_failure: Optional[dict] = None,
                               event_type_for_natural_key_prefix: str = "sec_13d") -> dict:
    """Serialize the in-memory accumulator into the §7.1 record_level shape.

    The dedup_summary_by_natural_key_prefix field is 13D-only per §7.1; for
    analyst_rating batches the natural_key has no meaningful prefix, so the
    field is omitted from the report (renderer hides the section).
    """
    report = {
        "shape": "record_level",
        "totals": {
            "records_seen": state.records_seen,
            "events_written": state.events_written,
            "dedup_hits": state.dedup_hits,
            "dedup_collisions_with_changed_payload": state.dedup_collisions_with_changed_payload,
            "quarantines": state.quarantines,
            "provisional_fields_populated": state.provisional_fields_populated,
        },
        "quarantines_summary_by_failure_class": dict(state.quarantines_by_class),
        "quarantines_samples": list(state.quarantines_samples[:QUARANTINES_SAMPLES_MAX]),
        "dedup_collisions": list(state.dedup_collisions[:DEDUP_COLLISIONS_MAX]),
        "terminal_failure": terminal_failure,  # null on clean close (literal None → null in JSON)
    }
    if event_type_for_natural_key_prefix == "sec_13d":
        # Top-20 filer-CIK prefixes; remainder bucket under "_other" per §7.1.
        sorted_groups = sorted(
            state.natural_key_prefix_counts.items(),
            key=lambda kv: kv[1], reverse=True,
        )
        top = dict(sorted_groups[:20])
        if len(sorted_groups) > 20:
            top["_other"] = sum(v for _, v in sorted_groups[20:])
        report["dedup_summary_by_natural_key_prefix"] = top
    return report


# ---------------------------------------------------------------------------
# Public: write_event_batch
# ---------------------------------------------------------------------------


def write_event_batch(
    events_db_path: Union[str, Path, None] = None,
    *,
    source: str,
    kind: str,
    policy: str,
    records: Iterable[dict],
    request_id: Optional[int] = None,
    source_descriptor: str,
    commit_chunk_size: int = DEFAULT_COMMIT_CHUNK_SIZE,
    parent_batch_id: Optional[int] = None,
    heartbeat_max_seconds: Optional[float] = None,
    on_batch_open: Optional[Callable[[int], None]] = None,
    parse_quarantines_collector: Optional[list[tuple[dict, Exception, str]]] = None,
    research_db_path: Union[str, Path, None] = None,
) -> BatchResult:
    """Write a fetch / load batch. Spec brief §11 / EL36 / EL18 / EL19.

    Iterates `records`, validates each per-type, dedups against the partial
    UNIQUE on (event_type, natural_key) WHERE status='active', commits in
    chunks of `commit_chunk_size`. On per-record validation failure:
      - policy='quarantine':       quarantine the record, batch continues
      - policy='abort_on_first_error': escalate to RecordValidationAbort,
                                   roll back prior chunk's events for this
                                   batch, close the batch as failed.

    At close, compute content_hash via hash_queries.CLOSE_HASH_SQL and store
    the record_level quality report on the batch row.

    heartbeat_max_seconds (brief D): when set, the per-record loop also
    flushes whatever is pending if wall-clock since the last chunk commit
    exceeds this threshold. Closes the EL18 / arch §5.2 step 2a T-bound
    "K filings OR T seconds whichever first" path: the chunk-commit CAS
    UPDATE doubles as the heartbeat, so a long-tail per-filing fetch
    (>30s in arch's working defaults) still touches `last_heartbeat_at`
    before the reaper's stale threshold trips.

    on_batch_open (brief D): when set, called once with the newly-INSERTed
    `batch_id` immediately after _open_batch returns. The EDGAR fetcher
    uses this to register the open batch_id with its SIGTERM handler so
    the bounded handler can UPDATE status='cancelled_partial' on the
    right row (arch §11 §20 SFv5-3). Side-effect only; the callback's
    return value is ignored.

    parse_quarantines_collector (brief D): an optional list of
    `(record, exception, failure_step)` triples the caller mutates DURING
    iteration of `records` (each EdgarParseFatal the fetcher catches
    appends one entry). write_event_batch reads the list at clean close —
    AFTER the records iterator has run through — and INSERTs each entry
    into research_event_quarantines on the same batch_id. This
    surfaces parse-time failures into the batch's `quality_report` totals
    + per-class summary alongside the validate-time quarantines the per-
    record loop catches. Spec brief §3.6's `failure_step` enum allows
    'parse' specifically for this path. By-reference list semantics is
    chosen (vs a callable thunk) so the fetcher's natural code pattern —
    a list it appends to during iteration — works without wrapping.

    research_db_path (brief H): when set, after the close UPDATE commits
    on a succeeded* batch we enumerate the distinct (event_type, source)
    pairs under batch_id and call refresh_event_catalog_for() for each
    against the supplied research.db. Default None keeps brief B / D
    probes isolated to events.db (they have no research.db). The fetcher
    script and the canonical loader endpoint both pass the real path so
    the catalog refresh wiring (MO6 + DECISIONS #35 follow-up) fires per
    arch §5.6 step 4. Best-effort: a catalog refresh failure does not
    fail the batch's clean close; reconcile_event_catalog at dashboard
    startup is the eventually-consistent backstop.
    """
    if kind not in ("fetch", "load"):
        raise ValueError(f"write_event_batch requires kind in (fetch, load); got {kind!r}")
    if policy not in ("quarantine", "abort_on_first_error"):
        raise ValueError(f"write_event_batch requires policy in (quarantine, "
                         f"abort_on_first_error); got {policy!r}")

    state = _WriteState()
    # Open the batch row in its own short transaction so the batch_id exists
    # for any subsequent quarantine row INSERT even if the very first chunk
    # ends up aborting.
    with eventsdb.events_transaction(events_db_path) as conn:
        batch_id = _open_batch(
            conn,
            source=source, kind=kind, policy=policy,
            source_descriptor=source_descriptor,
            request_id=request_id, parent_batch_id=parent_batch_id,
        )

    # Brief D: register the batch_id with the caller (e.g. the EDGAR fetcher's
    # SIGTERM handler) right after _open_batch. Done outside the batch-open
    # transaction so a callback exception does not roll back the INSERT — the
    # batch row already exists durably.
    if on_batch_open is not None:
        on_batch_open(batch_id)

    # In-batch natural-key tracking: (event_type, natural_key) → payload-canonical-json
    # so a same-batch duplicate of the same key triggers the dedup-hit path
    # instead of crashing on the partial UNIQUE INSERT.
    in_batch_keys: dict[tuple[str, str], str] = {}
    chunk_event_ids: list[int] = []  # event_ids inserted in the current chunk
    committed_event_ids: list[int] = []  # event_ids that have survived a chunk commit

    # The iteration loop opens one transaction per chunk; the CAS-guarded chunk
    # UPDATE inside that transaction lets the reaper invalidate a stale writer.
    pending = []  # accumulated records this chunk
    pending_quarantines: list[tuple[dict, PayloadValidationError, str]] = []

    def flush_chunk(conn_inner: sqlite3.Connection, *,
                    pending_records, pending_quars):
        """INSERT every pending record and quarantine; run the CAS guard."""
        # Insert events + their companies
        for rec in pending_records:
            event_id = _insert_event_and_companies(conn_inner, record=rec,
                                                    batch_id=batch_id)
            chunk_event_ids.append(event_id)
        # Insert quarantines
        for rec, exc, failure_step in pending_quars:
            _quarantine_record(conn_inner, batch_id=batch_id, record=rec,
                               exc=exc, failure_step=failure_step)
        # CAS guard: update the row's heartbeat + counters atomically. If the
        # reaper flipped status='failed' between the prior chunk and this
        # commit, RETURNING is empty and we surface CAS failure to the caller.
        ok = _cas_chunk_commit(
            conn_inner, batch_id,
            event_count_delta=len(pending_records),
            quarantine_count_delta=len(pending_quars),
        )
        return ok

    # We do not stream `records` lazily inside a transaction — instead we
    # accumulate into Python lists and open a fresh transaction once a chunk
    # is ready. That keeps the writer-lock duration bounded by the chunk
    # size, not by the producer's iteration speed (DDIA Ch 8 §Single-object
    # and multi-object operations).
    #
    # Brief E: abort_exc now widens to either a `RecordValidationAbort` (the
    # per-record payload-validation path) OR one of `_LOADER_ABORT_CLASSES`
    # (loader trio raised by the iterator on abort policy). Both terminate
    # iteration and trigger the same rollback-and-close sequence below; the
    # close uses `abort_failure_class` so the batch row records the right
    # value per §7.2 (RecordValidationAbort for payload, the loader class
    # name verbatim for the loader trio).
    abort_exc: Optional[Exception] = None
    abort_failure_class: Optional[str] = None
    abort_terminal_failure: Optional[dict] = None
    # Brief D heartbeat_max_seconds tracking: wall-clock at the last chunk
    # commit (or at batch open if no chunk has flushed yet). Combined with
    # the commit_chunk_size threshold below to form the "K records OR T
    # seconds whichever first" cadence arch §5.2 step 2a specifies.
    last_chunk_at = time.time()
    try:
        for raw_record in records:
            state.records_seen += 1
            record_index_in_batch = state.records_seen - 1

            # Per-type validation
            event_type = raw_record.get("event_type")
            try:
                schema_version, _required, _optional, validate = event_types.get_spec(event_type)
            except KeyError:
                raise WrongFieldType(
                    f"record {record_index_in_batch}: unknown event_type {event_type!r}"
                )

            try:
                normalized_payload = validate(raw_record["payload"])
            except PayloadValidationError as exc:
                # Per-record vs batch-level escalation depending on policy
                if policy == "quarantine":
                    state.quarantines += 1
                    state.quarantines_by_class[type(exc).__name__] = (
                        state.quarantines_by_class.get(type(exc).__name__, 0) + 1
                    )
                    if len(state.quarantines_samples) < QUARANTINES_SAMPLES_MAX:
                        state.quarantines_samples.append({
                            "quarantine_id": None,  # populated after flush_chunk INSERTs
                            "event_type": event_type,
                            "accession_number": raw_record.get("accession_number"),
                            "failure_step": "validate",
                            "failure_class": type(exc).__name__,
                            "failure_message": str(exc)[:FAILURE_MESSAGE_MAX_BYTES],
                            "raw_input_excerpt": (raw_record.get("raw_input") or "")[:2048],
                        })
                    pending_quarantines.append((raw_record, exc, "validate"))
                    continue
                # abort_on_first_error: raise RecordValidationAbort after the
                # prior chunk's events are rolled back below.
                abort_exc = RecordValidationAbort(
                    f"per-record {type(exc).__name__} under policy=abort_on_first_error",
                    underlying_class_name=type(exc).__name__,
                    record_index_in_batch=record_index_in_batch,
                    natural_key=raw_record.get("natural_key"),
                    raw_input_excerpt=(raw_record.get("raw_input") or "")[:2048],
                )
                abort_failure_class = "RecordValidationAbort"
                abort_terminal_failure = {
                    "failure_class": "RecordValidationAbort",
                    "underlying_class": type(exc).__name__,
                    "record_index_in_batch": record_index_in_batch,
                    "natural_key": raw_record.get("natural_key"),
                    "failure_message": str(abort_exc)[:FAILURE_MESSAGE_MAX_BYTES],
                    "raw_input_excerpt": (raw_record.get("raw_input") or "")[:2048],
                    "rolled_back_event_count": 0,  # filled after the prior-chunk rollback
                }
                break

            # Carry-through normalized payload back onto the record
            rec = dict(raw_record)
            rec["payload"] = normalized_payload
            rec["payload_schema_version"] = schema_version

            # Dedup detection.
            # (1) Same-batch dedup: have we seen this (event_type, natural_key) already?
            key = (rec["event_type"], rec["natural_key"])
            if key in in_batch_keys:
                stored_canonical = in_batch_keys[key]
                new_canonical = hash_queries.canonical_json(rec["payload"])
                _record_dedup_hit(state, rec, stored_canonical, new_canonical)
                continue

            # (2) Cross-batch dedup: SELECT against research_events with status='active'.
            with eventsdb.get_events_conn(events_db_path) as conn_read:
                row = conn_read.execute(
                    """SELECT event_id, payload FROM research_events
                        WHERE event_type=? AND natural_key=? AND status='active'""",
                    (rec["event_type"], rec["natural_key"]),
                ).fetchone()
            if row is not None:
                stored_canonical = hash_queries.canonical_json(row["payload"])
                new_canonical = hash_queries.canonical_json(rec["payload"])
                _record_dedup_hit(state, rec, stored_canonical, new_canonical)
                continue

            # No dedup — this record will be INSERTed at the next chunk flush.
            in_batch_keys[key] = hash_queries.canonical_json(rec["payload"])
            pending.append(rec)

            # Chunk flush threshold: K records OR T wall-clock seconds whichever
            # first (the T branch is brief-D-only via heartbeat_max_seconds).
            size_triggered = len(pending) >= commit_chunk_size
            time_triggered = (
                heartbeat_max_seconds is not None
                and (pending or pending_quarantines)
                and (time.time() - last_chunk_at) >= heartbeat_max_seconds
            )
            if size_triggered or time_triggered:
                with eventsdb.events_transaction(events_db_path) as conn:
                    ok = flush_chunk(conn, pending_records=pending,
                                     pending_quars=pending_quarantines)
                    if not ok:
                        # Reaper got us. ROLLBACK is implicit on the exception below.
                        raise _ReaperFlippedBatch(batch_id)
                    committed_event_ids.extend(chunk_event_ids)
                    chunk_event_ids.clear()
                    pending = []
                    pending_quarantines = []
                last_chunk_at = time.time()
    except _LOADER_ABORT_CLASSES as exc:
        # Brief E loader-side abort. The iterator (loaders/jsonl_canonical.py)
        # raises one of the loader trio when policy='abort_on_first_error'
        # encounters a per-line failure. Under quarantine policy the iterator
        # appends to parse_quarantines_collector instead and never reaches
        # this branch.
        #
        # Per §7.2: the batch's failure_class is the loader class name verbatim
        # (LoaderEnvelopeInvalid / LoaderJsonDecodeError / TickerNotFoundInBridge),
        # NOT escalated to RecordValidationAbort (the latter is reserved for
        # per-type payload validation failures). Records in `pending` are
        # discarded (never INSERTed); records in `committed_event_ids` get
        # rolled back below using the same path the RecordValidationAbort
        # branch uses.
        abort_exc = exc
        abort_failure_class = type(exc).__name__
        # The records_seen counter has been bumped for the line that raised
        # (the loader's per-line work happens inside next(records), which
        # bumps records_seen for the line about to be yielded; on raise we've
        # already incremented). Use records_seen - 1 for the failing index.
        abort_terminal_failure = {
            "failure_class": abort_failure_class,
            "underlying_class": abort_failure_class,
            "record_index_in_batch": max(state.records_seen - 1, 0),
            "natural_key": None,  # loader-trio raises happen before natural_key is built
            "failure_message": str(exc)[:FAILURE_MESSAGE_MAX_BYTES],
            "raw_input_excerpt": "",  # loader put the line text into failure_message
            "rolled_back_event_count": 0,  # filled after the prior-chunk rollback below
        }

    # Brief D: flush parse-time quarantines the caller's iterator accumulated.
    # Each entry was appended by the iterator inside the records loop; we INSERT
    # them all here in one transaction so the close-time totals + per-class
    # summary include them. Done before the final chunk flush so the chunk
    # commit's CAS UPDATE sees the cumulative quarantine count.
    if parse_quarantines_collector:
        parse_quars_snapshot = list(parse_quarantines_collector)
        with eventsdb.events_transaction(events_db_path) as conn:
            for rec, exc, failure_step in parse_quars_snapshot:
                _quarantine_record(
                    conn, batch_id=batch_id, record=rec, exc=exc,
                    failure_step=failure_step,
                )
        state.quarantines += len(parse_quars_snapshot)
        for _rec, _exc, _fs in parse_quars_snapshot:
            cls = type(_exc).__name__
            state.quarantines_by_class[cls] = state.quarantines_by_class.get(cls, 0) + 1
            if len(state.quarantines_samples) < QUARANTINES_SAMPLES_MAX:
                state.quarantines_samples.append({
                    "quarantine_id": None,
                    "event_type": _rec.get("event_type"),
                    "accession_number": _rec.get("accession_number"),
                    "failure_step": _fs,
                    "failure_class": cls,
                    "failure_message": str(_exc)[:FAILURE_MESSAGE_MAX_BYTES],
                    "raw_input_excerpt": (_rec.get("raw_input") or "")[:2048],
                })

    # Final flush at end of iteration
    if abort_exc is None:
        if pending or pending_quarantines:
            with eventsdb.events_transaction(events_db_path) as conn:
                ok = flush_chunk(conn, pending_records=pending,
                                 pending_quars=pending_quarantines)
                if not ok:
                    raise _ReaperFlippedBatch(batch_id)
                committed_event_ids.extend(chunk_event_ids)
                chunk_event_ids.clear()
                pending = []
                pending_quarantines = []
        # Folding parse-quars into the batch row's quarantine_count counter
        # (the chunk flush above adds only `pending_quarantines` from the
        # validate-step path; we need to bump the row's counter to include
        # the parse-step quarantines we INSERTed in the block above).
        if parse_quarantines_collector:
            n = len(list(parse_quarantines_collector))
            if n:
                with eventsdb.events_transaction(events_db_path) as conn:
                    conn.execute(
                        "UPDATE research_event_batches "
                        "SET quarantine_count = quarantine_count + ? "
                        "WHERE batch_id = ?",
                        (n, batch_id),
                    )
    else:
        # Even on abort we need to flush any quarantines accumulated before the
        # aborting record (under quarantine policy this branch isn't reached;
        # under abort_on_first_error a record can only fail at validate-time, so
        # no quarantines are pending here). Roll back the prior chunk's events.
        rolled_back = 0
        with eventsdb.events_transaction(events_db_path) as conn:
            # Per §5.1 step 3 failed-event rollback: insert history + flip status
            for eid in committed_event_ids:
                event_row = conn.execute(
                    "SELECT * FROM research_events WHERE event_id = ?",
                    (eid,),
                ).fetchone()
                if event_row is None:
                    continue
                _insert_history_snapshot(
                    conn,
                    event_row=event_row,
                    correction_batch_id=batch_id,    # failed-batch self-rollback per §3.2
                    new_status_for_history="active",  # pre-rollback state was active
                )
                conn.execute(
                    "UPDATE research_events SET status='rolled_back' WHERE event_id = ?",
                    (eid,),
                )
                rolled_back += 1
        # Mirror the rolled-back count into the RecordValidationAbort instance
        # so any downstream observer sees a true value. Loader-trio exceptions
        # do not declare a rolled_back_event_count attribute; the count lives
        # in abort_terminal_failure regardless.
        if isinstance(abort_exc, RecordValidationAbort):
            abort_exc.rolled_back_event_count = rolled_back
        abort_terminal_failure["rolled_back_event_count"] = rolled_back
        # Close the batch as failed
        with eventsdb.events_transaction(events_db_path) as conn:
            # content_hash is computed even on failed close — the rows that
            # WERE written (and now rolled_back) are part of batch_id history
            # per the §8.5 amendment (close-hash keys on batch_id alone, no
            # status filter); the history-inclusive verification probe at
            # brief N step 9 expects this.
            content_hash = hash_queries.compute_content_hash(conn, batch_id)
            state.events_written = 0   # all committed events were rolled back
            report = _build_record_level_report(
                state, terminal_failure=abort_terminal_failure,
            )
            _close_batch_cas(
                conn, batch_id,
                status="failed",
                content_hash=content_hash,
                quality_report_json=hash_queries.canonical_json(report),
                failure_class=abort_failure_class,
            )
        return BatchResult(
            batch_id=batch_id, status="failed",
            event_count=0, quarantine_count=state.quarantines,
            content_hash=content_hash, failure_class=abort_failure_class,
            quality_report=report,
        )

    # Clean close
    state.events_written = len(committed_event_ids)
    # Per-type natural_key_prefix accounting now that we know the records that
    # actually committed. For sec_13d the prefix is the filer_cik segment.
    # Done from the DB rather than Python locals so the count includes dedup
    # paths' contributions consistently with §7.1.
    if state.events_written or state.dedup_hits:
        with eventsdb.get_events_conn(events_db_path) as conn_read:
            for row in conn_read.execute(
                """SELECT natural_key FROM research_events
                    WHERE batch_id=? AND status='active'""",
                (batch_id,),
            ):
                prefix = row["natural_key"].split("_", 1)[0] + "_"
                state.natural_key_prefix_counts[prefix] = (
                    state.natural_key_prefix_counts.get(prefix, 0) + 1
                )

    with eventsdb.events_transaction(events_db_path) as conn:
        content_hash = hash_queries.compute_content_hash(conn, batch_id)
        status = "succeeded_with_quarantines" if state.quarantines > 0 else "succeeded"
        report = _build_record_level_report(state, terminal_failure=None)
        _close_batch_cas(
            conn, batch_id,
            status=status,
            content_hash=content_hash,
            quality_report_json=hash_queries.canonical_json(report),
        )

    # Brief H: post-commit catalog refresh per MO6 / arch §5.6 step 4 /
    # DECISIONS #35 follow-up. Best-effort; failures are logged inside the
    # helper. The catalog refresh runs OUTSIDE the events-transaction above
    # because (a) it ATTACHes events.db onto a research.db connection and
    # SQLite forbids ATTACH inside a transaction, (b) the close UPDATE has
    # already committed so the MIN/MAX/COUNT subqueries see the post-close
    # active rows. Only fires when research_db_path is set (opt-in keeps
    # brief B / D probes isolated to events.db).
    catalog_refreshes: list = []
    if research_db_path is not None:
        # Import here so eventsdb-only callers (probes) don't pay the import
        # cost of the catalog module which itself imports researchdb.
        from . import catalog as event_catalog
        for r in event_catalog.refresh_catalog_for_batch(
            batch_id,
            events_db_path=events_db_path,
            research_db_path=research_db_path,
        ):
            catalog_refreshes.append({
                "event_type": r.event_type,
                "source": r.source,
                "row_count": r.row_count,
                "start_at": r.start_at,
                "end_at": r.end_at,
                "inserted": r.inserted,
                "changed": r.changed,
                "name": r.name,
            })

    return BatchResult(
        batch_id=batch_id, status=status,
        event_count=state.events_written, quarantine_count=state.quarantines,
        content_hash=content_hash, failure_class=None,
        quality_report=report,
        catalog_refreshes=catalog_refreshes,
    )


def _record_dedup_hit(state: _WriteState, rec: dict,
                      stored_canonical: str, new_canonical: str) -> None:
    """Side-effect helper: bump dedup totals and append a collision sample.

    Per §7.1 / MO5: every dedup hit is in `dedup_hits`; dedup hits whose
    payload differs are ALSO in `dedup_collisions_with_changed_payload`
    (the second is a subset of the first).
    """
    state.dedup_hits += 1
    if stored_canonical != new_canonical:
        state.dedup_collisions_with_changed_payload += 1
        if len(state.dedup_collisions) < DEDUP_COLLISIONS_MAX:
            state.dedup_collisions.append({
                "natural_key": rec["natural_key"],
                "stored_payload_canonical_sha256_prefix":
                    hash_queries.canonical_json(stored_canonical)[:16] + "...",
                "new_payload_canonical_sha256_prefix":
                    new_canonical[:16] + "...",
            })


class _ReaperFlippedBatch(RuntimeError):
    """Internal signal that the CAS guard failed (reaper got there first).

    Not a public class; the writer catches this in a future revision to
    surface as a clean BatchResult shape. M2.5 brief B raises it up;
    brief I's reaper / SIGTERM handler is the consumer that ensures the
    failed-batch self-rollback runs in this case (via the secondary sweep
    at startup).
    """

    def __init__(self, batch_id: int):
        super().__init__(f"reaper flipped batch_id={batch_id} under writer")
        self.batch_id = batch_id


# ---------------------------------------------------------------------------
# Public: apply_correction
# ---------------------------------------------------------------------------


def apply_correction(
    events_db_path: Union[str, Path, None] = None,
    *,
    event_id: int,
    corrected_payload: dict,
    companies: list[dict],
    trigger_note: str,
) -> BatchResult:
    """Apply a single-row correction. Spec brief §5.4 / EL15 / EL22 / EL23.

    Refusal paths:
      - Target event not found  → ValueError
      - Target's batch is `running`        → CorrectionAgainstRunningBatch (EL23)
      - Incoming `companies` set differs from stored → CorrectionAltersCompanyLinks (EL23)

    On success: opens a correction-batch, inserts a history snapshot, updates
    the current row's payload + batch_id + version, computes the
    correction-batch's content_hash, writes the correction_summary quality
    report shape.
    """
    with eventsdb.events_transaction(events_db_path) as conn:
        # Read the target event row + its parent batch status atomically
        target = conn.execute(
            "SELECT * FROM research_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if target is None:
            raise ValueError(f"apply_correction: event_id={event_id} not found")
        parent_batch = conn.execute(
            "SELECT status, source FROM research_event_batches WHERE batch_id = ?",
            (target["batch_id"],),
        ).fetchone()
        if parent_batch is None:
            raise ValueError(f"apply_correction: parent batch {target['batch_id']} missing")
        if parent_batch["status"] == "running":
            raise CorrectionAgainstRunningBatch(
                f"target event_id={event_id} belongs to batch_id={target['batch_id']} "
                f"which is still status='running'",
                event_id=event_id, batch_id=target["batch_id"],
            )

        # Validate the corrected payload against the per-type validator
        _, _, _, validate = event_types.get_spec(target["event_type"])
        normalized = validate(corrected_payload)

        # Company-link guard (EL23). Read stored set, compare to incoming.
        stored_companies = list(conn.execute(
            "SELECT cik, role, cusip FROM research_event_companies WHERE event_id = ?",
            (event_id,),
        ).fetchall())
        stored_set = {(r["cik"], r["role"], r["cusip"]) for r in stored_companies}
        incoming_set = {
            (entry["cik"], entry["role"], entry.get("cusip"))
            for entry in companies
        }
        if stored_set != incoming_set:
            added = [
                {"cik": c, "role": r, "cusip": u}
                for (c, r, u) in incoming_set - stored_set
            ]
            removed = [
                {"cik": c, "role": r, "cusip": u}
                for (c, r, u) in stored_set - incoming_set
            ]
            raise CorrectionAltersCompanyLinks(
                f"correction would alter company links on event_id={event_id}",
                event_id=event_id, added=added, removed=removed,
            )

        # Open the correction-batch row inside the same transaction so the
        # history INSERT below references a real batch_id.
        correction_batch_id = _open_batch(
            conn,
            source=parent_batch["source"],
            kind="correction",
            policy="quarantine",          # correction never quarantines, but the column is NOT NULL
            source_descriptor=hash_queries.canonical_json({"trigger_note": trigger_note}),
            request_id=None,              # correction-batches carry NULL per arch §4.4
            parent_batch_id=target["batch_id"],
        )

        # History snapshot — captures the pre-correction state
        _insert_history_snapshot(
            conn, event_row=target,
            correction_batch_id=correction_batch_id,
            new_status_for_history=target["status"],
        )

        # UPDATE-in-place: payload + batch_id + version
        new_payload_json = hash_queries.canonical_json(normalized)
        conn.execute(
            """UPDATE research_events
                  SET payload = ?, batch_id = ?, version = version + 1
                WHERE event_id = ?""",
            (new_payload_json, correction_batch_id, event_id),
        )

        # Compute content_hash for the correction-batch (one row: the corrected one)
        content_hash = hash_queries.compute_content_hash(conn, correction_batch_id)

        # Detect which payload keys actually differ (top-level only)
        prev_payload = json.loads(target["payload"])
        diff_keys = sorted(
            k for k in set(prev_payload) | set(normalized)
            if prev_payload.get(k) != normalized.get(k)
        )

        report = {
            "shape": "correction_summary",
            "event_id": event_id,
            "natural_key": target["natural_key"],
            "prev_version": target["version"],
            "new_version": target["version"] + 1,
            "parent_batch_id": target["batch_id"],
            "trigger_note": trigger_note,
            "payload_diff_keys": diff_keys,
        }

        _close_batch_cas(
            conn, correction_batch_id,
            status="succeeded",
            content_hash=content_hash,
            quality_report_json=hash_queries.canonical_json(report),
        )

    return BatchResult(
        batch_id=correction_batch_id, status="succeeded",
        event_count=1, quarantine_count=0,
        content_hash=content_hash, failure_class=None,
        quality_report=report,
    )


# ---------------------------------------------------------------------------
# Public: apply_rollback
# ---------------------------------------------------------------------------


def apply_rollback(
    events_db_path: Union[str, Path, None] = None,
    *,
    target_batch_id: int,
    trigger_note: str,
) -> BatchResult:
    """Roll back every active event in target_batch_id. Spec brief §5.5 / arch §7.2.

    Opens a rollback-batch, walks every active event under target_batch_id,
    inserts a history snapshot for each, flips status to 'rolled_back' and
    moves batch_id to the new rollback-batch (per arch §7.2 step 3 / spec
    §8.5: the rollback-batch's content_hash covers the rolled-back rows
    under batch_id=R, with the close-hash filter dropped per EL21).
    """
    with eventsdb.events_transaction(events_db_path) as conn:
        target_batch = conn.execute(
            "SELECT status, source, kind FROM research_event_batches WHERE batch_id = ?",
            (target_batch_id,),
        ).fetchone()
        if target_batch is None:
            raise ValueError(f"apply_rollback: target batch_id={target_batch_id} not found")
        if target_batch["kind"] not in ("fetch", "load"):
            # Per spec brief §6.9 rollback eligibility: only fetch / load batches
            # may be rolled back via the same endpoint; correction / rollback
            # are not eligible (a correction-batch rollback would unwind the
            # correction, which is a different flow).
            raise ValueError(
                f"apply_rollback: target batch_id={target_batch_id} has kind={target_batch['kind']!r}; "
                f"only fetch / load batches are rollback-eligible"
            )

        rollback_batch_id = _open_batch(
            conn,
            source=target_batch["source"],
            kind="rollback",
            policy="quarantine",       # column is NOT NULL; rollback never quarantines
            source_descriptor=hash_queries.canonical_json({"trigger_note": trigger_note}),
            request_id=None,
            parent_batch_id=target_batch_id,
        )

        # Walk every active event under the target batch
        active_rows = list(conn.execute(
            "SELECT * FROM research_events WHERE batch_id=? AND status='active'",
            (target_batch_id,),
        ).fetchall())

        for ev in active_rows:
            _insert_history_snapshot(
                conn, event_row=ev,
                correction_batch_id=rollback_batch_id,
                new_status_for_history="active",  # pre-rollback state was active
            )
            conn.execute(
                """UPDATE research_events
                      SET status='rolled_back',
                          batch_id = ?,
                          version  = version + 1
                    WHERE event_id = ?""",
                (rollback_batch_id, ev["event_id"]),
            )

        # Flip the target batch to status='rolled_back'
        conn.execute(
            """UPDATE research_event_batches
                  SET status='rolled_back'
                WHERE batch_id = ? AND status IN ('succeeded','succeeded_with_quarantines')""",
            (target_batch_id,),
        )

        # Content_hash for the rollback-batch (covers the rolled-back rows
        # now living under batch_id=rollback_batch_id; per EL21 the close-hash
        # SQL has no status filter so this returns rows correctly).
        content_hash = hash_queries.compute_content_hash(conn, rollback_batch_id)

        report = {
            "shape": "rollback_summary",
            "target_batch_id": target_batch_id,
            "rolled_back_event_count": len(active_rows),
            "trigger_note": trigger_note,
        }

        _close_batch_cas(
            conn, rollback_batch_id,
            status="succeeded",
            content_hash=content_hash,
            quality_report_json=hash_queries.canonical_json(report),
        )

    return BatchResult(
        batch_id=rollback_batch_id, status="succeeded",
        event_count=len(active_rows), quarantine_count=0,
        content_hash=content_hash, failure_class=None,
        quality_report=report,
    )
