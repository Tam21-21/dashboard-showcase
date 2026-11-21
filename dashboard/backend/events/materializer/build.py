"""Materializer engine: events.db query -> denormalized parquet bytes (spec brief §8.6 / §6.10).

`materialize_event_slice` is the single domain entry point the route and the
brief K / N probes all call. It runs the §8.6 SELECT (the single-source SQL,
built by `build_materialize_sql`), walks the rows through the per-type
`OUTPUT_SCHEMAS` registry, builds a pyarrow table with the locked schema, and
serializes parquet bytes. It reads events.db only and writes nothing back
(DDIA Ch 12: read-only derived-data producer; no dual write).

The X-Content-SHA256 the route returns is the sha256 of these exact bytes (a
transfer-integrity checksum of THIS materialization), not a cross-run
byte-stability claim. Re-derivability is logical: the deterministic ORDER BY
makes the row set + order a pure function of the (unchanged) source state, so a
re-materialization is table-equal (parquet byte-stability is not a pyarrow
guarantee and is not relied on).
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import pyarrow as pa
import pyarrow.parquet as pq

from ... import eventsdb
from . import output_schemas as _schemas

# The two statuses a materialize call may include (§6.10 include_status). Matches
# the research_events.status CHECK enum (eventsdb.py). A value outside this set is
# a caller bug (the route validates via Pydantic Literal before calling).
_ALLOWED_STATUS = ("active", "rolled_back")


@dataclass(frozen=True)
class MaterializeResult:
    """What the engine returns; the route maps it onto the HTTP response (§6.10)."""
    parquet_bytes: bytes        # the serialized parquet (the 200 body)
    event_count: int            # parquet row count -> X-Event-Count
    content_sha256: str         # hex sha256 of parquet_bytes -> X-Content-SHA256
    source_series: str          # "<event_type>:<source>" -> X-Source-Series


def build_materialize_sql(
    *,
    event_type: str,
    ciks: Sequence[int],
    knowledge_from: int,
    knowledge_to: int,
    include_status: Sequence[str] = ("active",),
    universe_id: Optional[int] = None,
) -> tuple[str, dict]:
    """Build the §8.6 SELECT + its bound-params dict (single source for build + probe).

    Generates one `:stN` placeholder per include_status value so a list binds
    cleanly (SQLite cannot bind a list to one named parameter). Mirrors the
    `_build_list_batches_sql` builder pattern in routes/research.py. The probe
    runs EXPLAIN QUERY PLAN against the returned (sql, params) tuple (L-11).

    M3.5 brief B (EL65, DECISIONS #54) adds two extra output columns to this one
    shared query — there is still NO per-type SQL branch (the per-type symbol
    logic lives in the derives, keyed on ctx['event_type']):
      - `bridge_ticker`: the deterministic single trade-time symbol for subject_cik,
        read only by the 13D derive (analyst rows read payload raw_ticker).
      - `membership_match`: the matched research_universe_membership episode for the
        event at its own knowledge_at, NULL when no `universe_id` is bound (an M2.5
        direct caller) or the event falls outside the company's membership window.
    `universe_id` is bound on every call (None -> SQLite NULL -> the membership
    join matches nothing -> membership_match NULL -> membership_valid NULL).
    """
    if not include_status:                                   # empty -> would make `IN ()` a syntax error
        raise ValueError("include_status must be non-empty")
    bad = [s for s in include_status if s not in _ALLOWED_STATUS]  # any value outside the enum
    if bad:                                                  # caller bug; fail loud, no fallback
        raise ValueError(f"include_status values not allowed: {bad}; allowed: {list(_ALLOWED_STATUS)}")

    status_names = [f"st{i}" for i in range(len(include_status))]   # st0, st1, ...
    status_placeholders = ", ".join(f":{n}" for n in status_names)  # ":st0, :st1"
    params: dict = {n: s for n, s in zip(status_names, include_status)}  # {"st0":"active",...}
    params["ciks_json"] = json.dumps(list(ciks))            # one bound param for the whole cik list (json_each)
    params["event_type"] = event_type                       # the single event type per call
    params["knowledge_from"] = knowledge_from               # inclusive lower bound
    params["knowledge_to"] = knowledge_to                   # exclusive upper bound (half-open window)
    params["universe_id"] = universe_id                     # bound into the membership join; None -> NULL (no match)

    # §8.6 SQL: arch §9.2 + e.producer_id (§8.3) + parameterized status IN (§6.10)
    # + deterministic ORDER BY (knowledge_at, event_id, cik) so the row order is a
    # pure function of the data (DDIA Ch 12 logical re-derivability).
    #
    # M3.5 brief B (EL65, §3.2, DECISIONS #54): the two added columns.
    # `bridge_ticker` is resolved with a correlated scalar subquery (LIMIT 1 over
    # the cik's bridge rows, ORDER BY loaded_at DESC, map_id ASC) — semantically
    # IDENTICAL to the spec's `ROW_NUMBER() ... rn = 1` (most-recently-loaded row,
    # ties broken by the surrogate; the OPEN-2 dual-class tie-break). The scalar
    # subquery is used in place of the spec's window-function-over-the-whole-table
    # form on purpose: a ROW_NUMBER() OVER (PARTITION BY cik ...) subquery forces a
    # full materialization (SCAN) of research_cik_ticker_map, which trips the locked
    # M2.5 brief-K L-11 invariant ("no base-table full scan"). The scalar subquery
    # resolves per driving cik through ix_cik_ticker_cik (verified: SEARCH, not
    # SCAN), keeping L-11 green and scaling with the slice's cik count rather than
    # the whole bridge. This deviation is SQL-shape only; the resolved ticker and
    # the dual-class tie-break are unchanged. (Surfaced as a brief-B build note.)
    # `membership_match` is the per-event universe gate (DECISIONS #54): a LEFT JOIN
    # on (universe_id, subject_cik) with the event's knowledge_at inside the episode;
    # episodes for one (universe, cik) do not overlap (§3.1 invariant) -> at most one
    # match. It uses ix_universe_membership_pit (verified: SEARCH).
    sql = f"""
        SELECT e.event_id, e.event_type, e.natural_key, e.knowledge_at,
               e.occurred_at, e.source, e.producer_id, e.payload,
               ec.cik AS subject_cik, ec.cusip, ec.role,
               (SELECT m.ticker FROM research_cik_ticker_map m
                 WHERE m.cik = ec.cik
                 ORDER BY m.loaded_at DESC, m.map_id ASC
                 LIMIT 1)                       AS bridge_ticker,
               um.membership_id                 AS membership_match
          FROM research_events e
          JOIN research_event_companies ec ON ec.event_id = e.event_id
          JOIN json_each(:ciks_json)       cl ON ec.cik    = cl.value
          LEFT JOIN research_universe_membership um
                 ON um.universe_id = :universe_id
                AND um.subject_cik = ec.cik
                AND um.entered_at <= e.knowledge_at
                AND (um.left_at IS NULL OR um.left_at > e.knowledge_at)
         WHERE e.event_type   = :event_type
           AND e.status       IN ({status_placeholders})
           AND e.knowledge_at >= :knowledge_from
           AND e.knowledge_at <  :knowledge_to
         ORDER BY e.knowledge_at, e.event_id, ec.cik
    """
    return sql, params


def _value_for(spec: _schemas.ColumnSpec, ctx: _schemas.RowContext):
    """Extract one column's value from a row context per the spec's source_kind."""
    if spec.source_kind in ("spine", "join"):               # straight from the ctx (== the SQL column)
        return ctx[spec.source]
    if spec.source_kind == "payload":                       # from the parsed payload dict; absent optional -> None
        return ctx["payload"].get(spec.source)
    if spec.source_kind == "derived":                       # computed by the registry's derive callable
        return spec.derive(ctx)
    # Unreachable given the four declared source kinds; a new kind must extend this ladder.
    raise ValueError(f"unknown source_kind {spec.source_kind!r} on column {spec.name!r}")


def _build_array(spec: _schemas.ColumnSpec, values: list) -> pa.Array:
    """Build one pyarrow array for a column, converting epoch-seconds -> timestamp[ns,UTC].

    The three timestamp columns (knowledge_at, occurred_at, resolved_at) hold
    epoch SECONDS as ints in the DB / payload. Building int64 first then casting
    int64 -> timestamp('s') -> timestamp('ns') interprets the ints as seconds
    (a direct `pa.array(ints, type=timestamp('ns'))` would wrongly read them as
    nanoseconds). Nulls pass through both casts.
    """
    if pa.types.is_timestamp(spec.arrow_type):              # the epoch-seconds -> ns path
        as_int = pa.array(values, type=pa.int64())         # raw epoch seconds (or null)
        return as_int.cast(pa.timestamp("s", tz="UTC")).cast(spec.arrow_type)
    return pa.array(values, type=spec.arrow_type)           # everything else builds directly to its locked type


def materialize_event_slice(
    *,
    event_type: str,
    ciks: Sequence[int],
    knowledge_from: int,
    knowledge_to: int,
    include_status: Sequence[str] = ("active",),
    universe_id: Optional[int] = None,
    events_db_path: Optional[Union[str, Path]] = None,
) -> MaterializeResult:
    """Query events.db for one (event_type, cik-universe, window) and return parquet bytes.

    Zero matching rows -> a valid 0-row parquet carrying the full schema (C2),
    event_count=0. A malformed 13D natural_key (derive can't split/int it) is a
    data-integrity bug and is allowed to raise -> the route maps it to 500 (A11).

    `universe_id` (M3.5 brief B, EL65, DECISIONS #54) drives the per-event
    membership flag: when supplied, each row's `membership_valid` is True/False per
    the membership-at-knowledge_at read; when None (an M2.5 direct caller), the flag
    is NULL on every row.
    """
    specs = _schemas.OUTPUT_SCHEMAS[event_type]             # KeyError on unknown type (route guards first)
    sql, params = build_materialize_sql(                    # the single-source §8.6 SQL + params
        event_type=event_type, ciks=ciks,
        knowledge_from=knowledge_from, knowledge_to=knowledge_to,
        include_status=include_status, universe_id=universe_id,
    )
    universe_id_present = universe_id is not None           # set once per call; distinguishes membership False from NULL

    contexts: list[_schemas.RowContext] = []               # one ctx per (event, cik) result row
    with eventsdb.get_events_conn(events_db_path) as conn:  # per-call read connection (no transaction)
        for row in conn.execute(sql, params):              # stream the join result
            contexts.append({
                "event_id": row["event_id"],               # spine surrogate id
                "event_type": row["event_type"],           # always == event_type, carried for the derive
                "natural_key": row["natural_key"],         # the 13D filer_cik_accession / analyst producer_id
                "knowledge_at": row["knowledge_at"],       # epoch seconds (-> timestamp)
                "occurred_at": row["occurred_at"],         # epoch seconds or None
                "source": row["source"],                   # 'sec_edgar' | 'canonical_loader' | 'benzinga'
                "producer_id": row["producer_id"],         # spine; analyst natural_key, NULL for 13D
                "payload": json.loads(row["payload"]),     # parse the JSON TEXT column for payload columns
                "subject_cik": row["subject_cik"],         # ec.cik aliased (EL25)
                "cusip": row["cusip"],                     # join, nullable
                "role": row["role"],                       # 'primary_issuer' | 'co_mentioned'
                "bridge_ticker": row["bridge_ticker"],     # EL65: deterministic bridge ticker (NULL if none); read by the 13D symbol derive
                "membership_match": row["membership_match"],  # EL65: matched membership episode id, NULL otherwise; read by the membership_valid derive
                "universe_id_present": universe_id_present,    # EL65: per-call flag, so membership_valid tells False from NULL
            })

    arrays = [                                              # one pyarrow array per registry column, in order
        _build_array(spec, [_value_for(spec, ctx) for ctx in contexts])
        for spec in specs
    ]
    table = pa.Table.from_arrays(arrays, schema=_schemas.arrow_schema(event_type))  # locked schema (validates types/nullability)

    buf = io.BytesIO()                                      # in-memory sink; the body is buffered (the sha header precedes it)
    pq.write_table(table, buf, compression="snappy", version="2.6")  # version 2.6 admits timestamp[ns] without coercion
    parquet_bytes = buf.getvalue()                          # the 200 body

    # X-Source-Series (BZ1.3): rows-first — the sources actually present in the
    # materialized rows — so a multi-source type (analyst_rating: benzinga +
    # canonical_loader) is reported honestly, not collapsed to one hard-coded source.
    # Zero rows -> the registered EVENT_TYPE_SOURCES fallback (no row to read source
    # from). Parse contract: sorted, comma-joined.
    present_sources = sorted({ctx["source"] for ctx in contexts})
    if not present_sources:
        present_sources = sorted(_schemas.EVENT_TYPE_SOURCES[event_type])
    source_series = f"{event_type}:{','.join(present_sources)}"

    return MaterializeResult(
        parquet_bytes=parquet_bytes,
        event_count=table.num_rows,                        # X-Event-Count
        content_sha256=hashlib.sha256(parquet_bytes).hexdigest(),  # X-Content-SHA256
        source_series=source_series,                       # X-Source-Series
    )
