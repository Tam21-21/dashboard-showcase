"""M2.5 brief E canonical JSON-lines loader.

Spec brief v4 §4.4 (R23 envelope) + §4.4.1 (per-line flow) + §5.3 (loader
contract) + §6.1 (endpoint). One module per format; this one reads `.jsonl`
files where each line is one JSON object per the R23 envelope shape.

Public surface
--------------
assert_loader_file_readable(path)
    Whole-file structural check. Raises LoaderFileUnreadable per RB1 if the
    file is empty, not valid UTF-8, or appears to be a single top-level JSON
    array (the loader cannot stream a single array record-by-record without
    buffering, so this is rejected by design at M2.5 volumes).

iter_canonical_records(path, *, policy, events_db_path,
                       parse_quarantines_collector, now_epoch=None)
    Generator yielding one `dict` per line that passed envelope validation +
    ticker resolution. For per-line failures:
      - policy='quarantine': appends a triple to
        parse_quarantines_collector and skips the line
      - policy='abort_on_first_error': raises the underlying Loader*
        exception immediately (write_event_batch's outer catch is what
        rolls back the prior chunk and closes the batch failed; brief E
        extends that catch to recognise the loader trio).

R23 envelope per §4.4 — required keys and their per-event-type rules:

    event_type     str   IN ('sec_13d', 'analyst_rating')
                         NB: 'sec_13d' rejects (MN11 / RB1); 13D goes through
                         the EDGAR fetcher, not the canonical loader.
    producer_id    str   REQUIRED for analyst_rating; REJECTED for sec_13d.
                         The natural_key on the spine.
    knowledge_at   int   REQUIRED. Epoch seconds UTC. R11 time (2).
    occurred_at    int|None  OPTIONAL. R11 time (1). NULL for analyst.
    raw_input      str   REQUIRED. The producer's original input as text.
    payload        dict  REQUIRED. Per-type validated downstream.
    companies      list  REQUIRED. >=1 entry, each:
                         {raw_ticker: str, role: 'primary_issuer'|'co_mentioned',
                          cusip: str|None}.
                         The loader resolves raw_ticker → cik against
                         research_cik_ticker_map and replaces with
                         {cik, role, cusip}.

Producer-side `resolved_at` and `cik` are REJECTED (MA5 / RB1 lock): the
loader is the sole resolver. The R23 envelope check refuses any line that
pre-populates either.

The loader injects `payload.resolved_at = now_epoch` (seconds UTC) after the
companies-resolution step succeeds; per-type validators (analyst_rating
spec) REQUIRE the field, so a pre-resolution payload would already fail
validation if we somehow skipped injection.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

from .. import write_core
from ..tickers import normalize_ticker                          # canonical bridge-key lookup (item 1 / B1)
from ... import eventsdb
from ..errors import (
    LoaderEnvelopeInvalid,
    LoaderFileUnreadable,
    LoaderJsonDecodeError,
    TickerNotFoundInBridge,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# R23 envelope spec — declared statically so the validator is auditable
# alongside the failure messages.
# ---------------------------------------------------------------------------


# Allowed top-level keys + their types. The envelope validator REJECTS any
# unknown key (strict; matches the per-type payload validators).
_ENVELOPE_REQUIRED: dict[str, type] = {
    "event_type": str,
    "knowledge_at": int,
    "raw_input": str,
    "payload": dict,
    "companies": list,
}
_ENVELOPE_OPTIONAL: dict[str, type] = {
    # producer_id required for analyst_rating; rejected for sec_13d.
    # Per-event-type rule applied after the per-key type check below.
    "producer_id": str,
    # occurred_at can be int or None; None passes the present-but-null check.
    "occurred_at": int,
}

_ALLOWED_EVENT_TYPES = ("sec_13d", "analyst_rating", "earnings")
_ALLOWED_COMPANY_ROLES = ("primary_issuer", "co_mentioned")

# Whole-file structural detection threshold: peek the first N bytes to
# decide if the file is a single top-level JSON array. 256 bytes is enough
# to skip an arbitrary BOM + whitespace + a `[` token; we do not need to
# parse the whole file.
_PEEK_BYTES = 256


# ---------------------------------------------------------------------------
# Whole-file structural check (RB1 / LoaderFileUnreadable)
# ---------------------------------------------------------------------------


def assert_loader_file_readable(path: Union[str, Path]) -> None:
    """Raise LoaderFileUnreadable on whole-file structural failure.

    Detects three classes of failure BEFORE any line iteration begins:

      (a) file is empty (zero bytes after open)
      (b) file is not valid UTF-8 (first chunk's decode fails)
      (c) file appears to be a single top-level JSON array (first non-
          whitespace byte is `[`)

    These three are batch-level only per RB1 — distinct from per-line
    envelope failures (LoaderEnvelopeInvalid) which can be either per-
    record (quarantine) or batch-level (abort) per §7.2 duality.

    The check is intentionally cheap: open + read up to _PEEK_BYTES bytes
    + decode + scan to first non-whitespace byte. The loader does NOT
    fully parse the file here; bad JSON inside line 17 is a per-line
    failure (LoaderJsonDecodeError), not a whole-file failure.
    """
    p = Path(path)
    if not p.exists():
        # Caller checks exists() before us and returns 404 there. This is
        # a defensive raise so an upstream contract drift surfaces honestly.
        raise LoaderFileUnreadable(f"source file does not exist: {p}")
    if p.stat().st_size == 0:
        raise LoaderFileUnreadable(f"source file is empty: {p}")

    # Peek the first chunk to detect not-utf-8 + single-array structure.
    with open(p, "rb") as fh:
        head_bytes = fh.read(_PEEK_BYTES)
    try:
        head_text = head_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character split at exactly the _PEEK_BYTES boundary
        # raises UnicodeDecodeError but is NOT a malformed file. Retry with
        # `errors='replace'` to confirm the failure isn't a boundary artifact:
        # if decoding the whole peek with errors='strict' fails AND every
        # full-line decode would also fail, the file really isn't utf-8.
        # Cheap way: drop the last byte and retry. If still fails, raise.
        for trim in range(1, 4):
            try:
                head_bytes[:_PEEK_BYTES - trim].decode("utf-8")
                # Trimming saved it → real file content. Fall through to
                # whitespace scan below; the full-file iteration will catch
                # any per-line UTF-8 failures.
                break
            except UnicodeDecodeError:
                continue
        else:
            raise LoaderFileUnreadable(
                f"source file is not valid UTF-8 in the first {_PEEK_BYTES} "
                f"bytes: {p} ({exc})"
            ) from exc
        # Recover the trimmed-decoded head_text for the whitespace scan.
        head_text = head_bytes[:_PEEK_BYTES - trim].decode("utf-8")

    # Scan past whitespace; if the first non-whitespace byte is `[`, the file
    # is a single JSON array and the loader rejects it (we cannot stream a
    # single array record-by-record at M2.5 without buffering the whole file).
    stripped = head_text.lstrip()
    if stripped.startswith("["):
        raise LoaderFileUnreadable(
            f"source file appears to be a single JSON array (starts with `[`); "
            f"JSON-lines format requires one JSON object per line: {p}"
        )


# ---------------------------------------------------------------------------
# Per-line envelope validation
# ---------------------------------------------------------------------------


def _validate_envelope(record: dict, *, line_index: int) -> None:
    """Validate one decoded line against the R23 envelope. Raises LoaderEnvelopeInvalid.

    Strict: any unknown top-level key is rejected. Required-or-conditional
    per-event-type rules (producer_id required for analyst_rating; rejected
    for sec_13d; sec_13d-on-the-loader-path rejected) are applied here.

    Raises with `line_index` carried in the message so the operator can locate
    the failing line in their input file.
    """
    if not isinstance(record, dict):
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: top-level value must be a JSON object, "
            f"got {type(record).__name__}"
        )

    # Strict-key check: reject unknown top-level keys.
    allowed = set(_ENVELOPE_REQUIRED) | set(_ENVELOPE_OPTIONAL)
    extra = set(record) - allowed
    if extra:
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: unknown top-level key(s) {sorted(extra)}"
        )

    # Required keys present, non-None, right type.
    for key, expected_type in _ENVELOPE_REQUIRED.items():
        if key not in record:
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: missing required envelope key {key!r}"
            )
        value = record[key]
        if value is None:
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: envelope key {key!r} is null; must be present"
            )
        if expected_type is int:
            # Reject bool (subclass of int) explicitly so True / False as a
            # timestamp surfaces as a type error.
            if isinstance(value, bool) or not isinstance(value, int):
                raise LoaderEnvelopeInvalid(
                    f"line {line_index}: envelope key {key!r} expected int, "
                    f"got {type(value).__name__}"
                )
        elif expected_type is str:
            if not isinstance(value, str):
                raise LoaderEnvelopeInvalid(
                    f"line {line_index}: envelope key {key!r} expected str, "
                    f"got {type(value).__name__}"
                )
        elif expected_type is dict:
            if not isinstance(value, dict):
                raise LoaderEnvelopeInvalid(
                    f"line {line_index}: envelope key {key!r} expected object, "
                    f"got {type(value).__name__}"
                )
        elif expected_type is list:
            if not isinstance(value, list):
                raise LoaderEnvelopeInvalid(
                    f"line {line_index}: envelope key {key!r} expected array, "
                    f"got {type(value).__name__}"
                )

    # Optional-key type checks (None passes).
    for key, expected_type in _ENVELOPE_OPTIONAL.items():
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if expected_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise LoaderEnvelopeInvalid(
                    f"line {line_index}: envelope key {key!r} expected int, "
                    f"got {type(value).__name__}"
                )
        elif expected_type is str:
            if not isinstance(value, str):
                raise LoaderEnvelopeInvalid(
                    f"line {line_index}: envelope key {key!r} expected str, "
                    f"got {type(value).__name__}"
                )

    # event_type enum.
    event_type = record["event_type"]
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: event_type {event_type!r} is not in "
            f"{list(_ALLOWED_EVENT_TYPES)}"
        )

    # sec_13d-on-the-loader-path rejection per MN11 / RB1: 13D arrives through
    # the EDGAR fetcher only.
    if event_type == "sec_13d":
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: sec_13d records must be fetched through the "
            f"EDGAR service, not loaded via the canonical loader"
        )

    # producer_id required for analyst_rating (and the only event type a
    # canonical-loader line can carry post-MN11).
    if event_type == "analyst_rating" and "producer_id" not in record:
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: producer_id is required for "
            f"event_type='analyst_rating'"
        )

    # Reject producer-pre-populated `resolved_at` (in payload) and `cik` (in
    # any companies entry) per MA5 / RB1: the loader is the sole resolver.
    payload = record["payload"]
    if isinstance(payload, dict) and "resolved_at" in payload:
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: payload.resolved_at is loader-injected; "
            f"producer-pre-populated value rejected"
        )
    companies = record["companies"]
    if not companies:
        raise LoaderEnvelopeInvalid(
            f"line {line_index}: companies array must have at least one entry"
        )
    for ci, c in enumerate(companies):
        if not isinstance(c, dict):
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: companies[{ci}] must be a JSON object, "
                f"got {type(c).__name__}"
            )
        # Strict-key check: only raw_ticker / role / cusip permitted.
        company_extra = set(c) - {"raw_ticker", "role", "cusip"}
        if company_extra:
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: companies[{ci}] has unknown key(s) "
                f"{sorted(company_extra)}"
            )
        if "cik" in c:
            # Defensive: `cik` is not in the allowed set above, so this branch
            # is unreachable through the strict-key check. Kept so a future
            # widening of the allowed set surfaces the MA5/RB1 producer-cik
            # rejection rule loudly.
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: companies[{ci}].cik is loader-resolved; "
                f"producer-pre-populated value rejected"
            )
        if "raw_ticker" not in c or not isinstance(c.get("raw_ticker"), str):
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: companies[{ci}].raw_ticker missing or not a string"
            )
        if c.get("role") not in _ALLOWED_COMPANY_ROLES:
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: companies[{ci}].role {c.get('role')!r} "
                f"is not in {list(_ALLOWED_COMPANY_ROLES)}"
            )
        # cusip: optional, str or None
        if "cusip" in c and c["cusip"] is not None and not isinstance(c["cusip"], str):
            raise LoaderEnvelopeInvalid(
                f"line {line_index}: companies[{ci}].cusip must be string or null, "
                f"got {type(c['cusip']).__name__}"
            )


# ---------------------------------------------------------------------------
# Ticker-to-CIK resolution against research_cik_ticker_map
# ---------------------------------------------------------------------------


def _resolve_company_ciks(conn: sqlite3.Connection, *,
                          companies: list, line_index: int) -> list[dict]:
    """Replace each companies[i].raw_ticker with the resolved cik.

    Looks up `(ticker, effective_from)` against research_cik_ticker_map.
    Per spec brief §3.7: M2.5 sources leave effective_from / effective_to
    NULL, so the lookup matches the row whose ticker equals raw_ticker AND
    effective_from IS NULL. If a future paid source ships dated intervals,
    the lookup should switch to a knowledge_at-aware query; M2.5 does not.

    Returns a list of {cik, role, cusip} dicts ready for write_event_batch's
    `record["companies"]` shape. Raises TickerNotFoundInBridge on the first
    raw_ticker with no row.
    """
    resolved: list[dict] = []
    for ci, c in enumerate(companies):
        raw_ticker = c["raw_ticker"]
        # M2.5 lookup: effective_from IS NULL (the current snapshot). A future
        # paid source can extend this to interval-aware lookup.
        row = conn.execute(
            """SELECT cik FROM research_cik_ticker_map
                WHERE ticker = ? AND effective_from IS NULL
                ORDER BY loaded_at DESC LIMIT 1""",
            (normalize_ticker(raw_ticker),),                    # canonical bridge key (item 1 / B1)
        ).fetchone()
        if row is None:
            raise TickerNotFoundInBridge(
                f"line {line_index}: companies[{ci}].raw_ticker={raw_ticker!r} "
                f"has no row in research_cik_ticker_map (M2.5 current-snapshot "
                f"lookup; effective_from IS NULL)"
            )
        resolved.append({
            "cik": int(row["cik"]),
            "role": c["role"],
            "cusip": c.get("cusip"),
        })
    return resolved


# ---------------------------------------------------------------------------
# Build the canonical record shape write_event_batch consumes
# ---------------------------------------------------------------------------


def _build_canonical_record(envelope: dict, *, resolved_companies: list[dict],
                            now_epoch: int) -> dict:
    """Assemble the dict write_event_batch ingests.

    Per spec brief write_core docstring:

        record dict keys: event_type, natural_key, producer_id, knowledge_at,
        occurred_at, source, origin, payload_schema_version, raw_input,
        payload (dict), companies (list of {cik, role, cusip})

    The loader sets:
        natural_key            = producer_id  (analyst_rating per EL13)
        source                 = 'canonical_loader'
        origin                 = 'loaded'
        payload_schema_version = SPEC.PAYLOAD_SCHEMA_VERSION  (set by write_core's
                                  validator dispatch via events.types.get_spec())
        payload[resolved_at]   = now_epoch  (LOADER-INJECTED)
    """
    payload = dict(envelope["payload"])
    payload["resolved_at"] = now_epoch
    return {
        "event_type": envelope["event_type"],
        "producer_id": envelope.get("producer_id"),
        "natural_key": envelope["producer_id"],  # EL13: analyst natural_key = producer_id
        "knowledge_at": envelope["knowledge_at"],
        "occurred_at": envelope.get("occurred_at"),
        "source": "canonical_loader",
        "origin": "loaded",
        # payload_schema_version will be replaced by the per-type spec inside
        # write_event_batch's validate(payload) call; we pass `1` as a placeholder
        # so the record dict's keys are complete for downstream consumers.
        "payload_schema_version": 1,
        "raw_input": envelope["raw_input"],
        "payload": payload,
        "companies": resolved_companies,
    }


# ---------------------------------------------------------------------------
# Streaming iterator (the generator write_event_batch consumes)
# ---------------------------------------------------------------------------


def iter_canonical_records(
    path: Union[str, Path],
    *,
    policy: str,
    events_db_path: Union[str, Path, None] = None,
    parse_quarantines_collector: Optional[list] = None,
    now_epoch: Optional[int] = None,
) -> Iterator[dict]:
    """Stream one canonical record dict per valid line.

    For each line:
      1. JSON-decode; on failure → LoaderJsonDecodeError (quarantine or raise)
      2. Validate envelope; on failure → LoaderEnvelopeInvalid (quarantine or raise)
      3. Resolve raw_ticker → CIK; on miss → TickerNotFoundInBridge (quarantine or raise)
      4. Inject payload.resolved_at = now_epoch
      5. Yield the assembled record dict (write_event_batch validates the
         payload per-type and either writes or quarantines via the existing
         path; this loader does NOT pre-validate the payload).

    `parse_quarantines_collector` is the by-reference list write_event_batch
    consumes at close. The loader appends `(record_stub_with_raw_input,
    exception_instance, failure_step)` triples for each per-line failure
    under `policy='quarantine'`. Under `policy='abort_on_first_error'` it
    raises the exception immediately and write_event_batch's outer catch
    handles the batch-level escalation.

    Memory: the generator opens the file once and iterates line-by-line; peak
    memory is bounded by the single-line size, not the whole-file size. The
    DB connection for ticker resolution is opened once and held for the
    duration of the iteration.
    """
    if policy not in ("quarantine", "abort_on_first_error"):
        raise ValueError(f"iter_canonical_records: policy must be quarantine|"
                         f"abort_on_first_error, got {policy!r}")
    if now_epoch is None:
        import time
        now_epoch = int(time.time())

    with eventsdb.get_events_conn(events_db_path) as conn_bridge:
        with open(path, "r", encoding="utf-8") as fh:
            for line_index, raw_line in enumerate(fh):
                # Strip the trailing newline; preserve other whitespace inside
                # JSON literals.
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    # Empty / whitespace-only lines are silently skipped. JSON-
                    # lines convention permits trailing newlines; rejecting an
                    # empty line as LoaderJsonDecodeError would force operators
                    # to strip their files manually for no benefit.
                    continue

                # Step 1: JSON decode
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError as exc:
                    err = LoaderJsonDecodeError(
                        f"line {line_index}: JSON decode failed: {exc}"
                    )
                    if policy == "quarantine":
                        _enqueue_quarantine(
                            parse_quarantines_collector,
                            raw_line=line, exc=err, failure_step="parse",
                        )
                        continue
                    raise err

                # Step 2: Envelope validation
                try:
                    _validate_envelope(envelope, line_index=line_index)
                except LoaderEnvelopeInvalid as exc:
                    if policy == "quarantine":
                        _enqueue_quarantine(
                            parse_quarantines_collector,
                            raw_line=line, exc=exc, failure_step="validate",
                            event_type=envelope.get("event_type")
                                       if isinstance(envelope, dict) else None,
                        )
                        continue
                    raise

                # Step 3: Resolve raw_ticker → cik for every companies[] entry
                try:
                    resolved = _resolve_company_ciks(
                        conn_bridge,
                        companies=envelope["companies"],
                        line_index=line_index,
                    )
                except TickerNotFoundInBridge as exc:
                    if policy == "quarantine":
                        _enqueue_quarantine(
                            parse_quarantines_collector,
                            raw_line=line, exc=exc, failure_step="validate",
                            event_type=envelope.get("event_type"),
                        )
                        continue
                    raise

                # Step 4: assemble record + inject resolved_at
                record = _build_canonical_record(
                    envelope,
                    resolved_companies=resolved,
                    now_epoch=now_epoch,
                )
                # Step 5: yield. write_event_batch validates the payload per-type
                # and routes (quarantine or RecordValidationAbort) through the
                # existing path.
                yield record


def _enqueue_quarantine(
    collector: Optional[list],
    *,
    raw_line: str,
    exc: Exception,
    failure_step: str,
    event_type: Optional[str] = None,
) -> None:
    """Append one quarantine triple to the collector.

    Triple shape matches the existing brief D protocol write_event_batch
    consumes (`parse_quarantines_collector`):
        (record_stub_dict, exception_instance, failure_step_string)

    `event_type` defaults to "unknown" when the envelope failed before that
    field was visible; the quarantine row's `event_type` CHECK constraint
    permits only 'sec_13d' or 'analyst_rating' though, so we coerce to
    'analyst_rating' here (the canonical loader's primary type) and rely on
    the failure_message text for the operator to see the real cause.
    """
    if collector is None:
        # Defensive: if no collector was supplied the quarantine is lost.
        # Logged so an operator review of journal sees the swallowed event.
        log.warning(
            "loader quarantine dropped (no collector wired): step=%s class=%s msg=%s",
            failure_step, type(exc).__name__, str(exc)[:200],
        )
        return
    # raw_input is the line text; the CHECK constraint on
    # research_event_quarantines.event_type bounds the value to
    # ('sec_13d','analyst_rating'). Use the envelope's event_type if visible,
    # else fall back to the canonical loader's primary type so the INSERT
    # does not blow up on a JSON-decode-failure case.
    record_stub = {
        "event_type": event_type or "analyst_rating",
        "raw_input": raw_line[: write_core.QUARANTINE_RAW_INPUT_MAX_BYTES],
        "accession_number": None,  # canonical loader has no accession context
    }
    collector.append((record_stub, exc, failure_step))
