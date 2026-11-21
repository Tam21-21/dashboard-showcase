"""The fundamentals materializer: events.db -> a fact-table parquet slice (M3.75 FUND-2 §6).

A PARALLEL materializer, not an extension of the event ``build_materialize_sql`` (arch §6, §6.1):
the event builder is hard-bound to ``research_events`` (its FROM names the event tables, it filters
``event_type``/``knowledge_at``, dispatches columns through ``OUTPUT_SCHEMAS[event_type]``, and
parses values out of the ``payload`` JSON). Fundamentals have a different source table, a different
filter axis (``filing_date`` + period), already-tabular values (no payload JSON), and a different
column set. Parameterizing the event builder would entangle two schemas in one query builder; a
parallel materializer is the lower-coupling call.

What it produces is the FACT TABLE SLICE — every kept filing for the universe's CIKs and the
requested concepts in the half-open knowledge window — NOT a pre-computed as-of snapshot. The
container recomputes the as-of join over this slice (``as_of_select``, §6.3, in the strategies-side
channel module), so the slice carries every vintage and the no-lookahead read is a pure function of
the (frozen) slice (R10, §6.4). DDIA Ch 1 ("Systems of record and derived data"): the live store is
the system of record, the slice is derived data, the as-of join is recompute over it.

Two load-bearing reuses, each stated rather than re-probed (NFR-8):
  - the CIK set binds as ONE json parameter joined via ``json_each(:ciks_json)`` (the event
    materializer's ``build.py:83``/``:121`` binding), NEVER an ``IN (?,?,...)`` expansion — an
    ``IN`` list would hit SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` ceiling (commonly 999) once a
    universe approaches that many CIKs (the prime-directive 5x-scale test lands there); the
    ``json_each`` binding exists precisely to avoid that variable-count wall (§6.1).
  - the ``filing_date >= knowledge_from AND filing_date < knowledge_to`` filter is the exact
    half-open ``[from, to)`` knowledge-window pattern the event materializer uses
    (``build.py:129-130``), here on the fundamentals knowledge axis (``filing_date``).

Date columns stay int64 epoch-seconds in the slice (unlike the event slice, which casts its
timestamp columns to ``timestamp[ns, UTC]``): the fundamentals store keys on epoch-second integers
(parse._iso_to_epoch) and the as-of join does INTEGER comparisons on ``period_end``/``filing_date``,
so carrying them as int64 keeps the slice byte-faithful to the store and the vectorized as-of
integer-clean. This is the deliberate, documented difference from ``materialize_event_slice`` (the
deviation-documentation rule).
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
from .concepts import FUNDAMENTAL_CONCEPTS

# The analytic columns the slice carries, in a fixed order. The surrogate ``fact_id`` and the
# lineage columns (``batch_id``, ``ingested_at``) are intentionally dropped — they are write-path
# bookkeeping, not analysis inputs; the slice is the consumer's view, not a store mirror (DDIA Ch 1
# derived-data discipline, NFR-10 store-size reasoning). The order matches the deterministic ORDER BY
# below so the row set + order is a pure function of the source state (logical re-derivability, R10).
_SLICE_COLUMNS: tuple[str, ...] = (
    "cik", "concept", "taxonomy", "xbrl_tag", "unit", "fiscal_year",
    "fiscal_period_code", "period_start", "period_end", "filing_date",
    "value", "source_form", "accession_number",
)

# The locked pyarrow schema for the slice — one entry per _SLICE_COLUMNS column, same order. The
# date columns are int64 epoch-seconds (the module-docstring deviation from the event slice). The
# two nullable columns are ``period_start`` (NULL for instant concepts, §1.1) and
# ``accession_number`` (nullable lineage, §1.1); every other column is NOT NULL in the store DDL.
_SLICE_SCHEMA = pa.schema([
    pa.field("cik", pa.int64(), nullable=False),
    pa.field("concept", pa.string(), nullable=False),
    pa.field("taxonomy", pa.string(), nullable=False),
    pa.field("xbrl_tag", pa.string(), nullable=False),
    pa.field("unit", pa.string(), nullable=False),
    pa.field("fiscal_year", pa.int64(), nullable=False),
    pa.field("fiscal_period_code", pa.string(), nullable=False),
    pa.field("period_start", pa.int64(), nullable=True),       # NULL for instant concepts
    pa.field("period_end", pa.int64(), nullable=False),
    pa.field("filing_date", pa.int64(), nullable=False),
    pa.field("value", pa.float64(), nullable=False),
    pa.field("source_form", pa.string(), nullable=False),
    pa.field("accession_number", pa.string(), nullable=True),  # nullable lineage
])


@dataclass(frozen=True)
class FundamentalsMaterializeResult:
    """What the materializer returns; the worker route maps it onto the HTTP response (§6.2)."""
    parquet_bytes: bytes        # the serialized fact-table parquet (the 200 body)
    fact_count: int             # parquet row count -> X-Fact-Count
    content_sha256: str         # hex sha256 of parquet_bytes -> X-Content-SHA256


def build_fundamentals_slice_sql(
    *,
    knowledge_from: int,
    knowledge_to: int,
    concepts: Sequence[str] = (),
) -> tuple[str, dict]:
    """Build the §6.1 fact-table SELECT + its bound-params dict (single source for build + probe).

    The CIK universe binds as ONE json parameter (``:ciks_json``) joined via ``json_each`` — the
    var-wall-safe binding, NOT an ``IN (?,?,...)`` expansion (§6.1). When ``concepts`` is non-empty
    it is bound the SAME way (``json_each(:concepts_json)``), so the concept filter also dodges the
    variable wall and a future deep concept set never re-opens it; an empty ``concepts`` means "all
    concepts" and adds no concept predicate. The ``filing_date`` filter is the half-open ``[from,
    to)`` knowledge window (``build.py:129-130``). The deterministic ORDER BY
    ``(cik, concept, fiscal_period_code, period_end, filing_date)`` is the §1.2 index order, making
    the row set + order a pure function of the source (logical re-derivability, R10).

    The caller binds ``:ciks_json`` (the materializer does, alongside the window) — this builder
    owns the window + concept params so the probe can EXPLAIN the returned (sql, params) pair.
    """
    # Qualify every column with the ``f`` alias: ``json_each`` exposes its own ``value`` (and ``key``)
    # column, so an unqualified ``value`` in the SELECT is "ambiguous column name". The result column
    # names stay unqualified (SQLite names a ``f.value`` result column ``value``), so row["value"] works.
    cols = ", ".join(f"f.{c}" for c in _SLICE_COLUMNS)         # the fixed analytic column list, alias-qualified
    params: dict = {                                          # the window params (ciks_json added by the caller)
        "knowledge_from": knowledge_from,                     # inclusive lower bound
        "knowledge_to": knowledge_to,                         # exclusive upper bound (half-open)
    }
    concept_clause = ""                                       # no concept predicate by default (all concepts)
    if concepts:                                             # a non-empty set -> json_each concept filter (var-wall-safe)
        params["concepts_json"] = json.dumps(list(concepts))  # one bound param for the whole concept set
        concept_clause = "\n           AND f.concept IN (SELECT value FROM json_each(:concepts_json))"
    # Flow concepts (a duration: net income, revenue...) enter the slice as the annual figure only, the scope
    # reads.py _scope_clause gives research; a quarterly or year-to-date flow row stays in the store but out of
    # the slice (lane FA1a). Instants (a point in time) pass in every period. One bound json parameter.
    params["flow_concepts_json"] = json.dumps(
        [spec.name for spec in FUNDAMENTAL_CONCEPTS if spec.period_semantics == "flow"])
    sql = f"""
        SELECT {cols}
          FROM research_fundamental_facts f
          JOIN json_each(:ciks_json) cl ON f.cik = cl.value
         WHERE f.filing_date >= :knowledge_from
           AND f.filing_date <  :knowledge_to{concept_clause}
           AND (f.fiscal_period_code = 'FY'
                OR f.concept NOT IN (SELECT value FROM json_each(:flow_concepts_json)))
         ORDER BY f.cik, f.concept, f.fiscal_period_code, f.period_end, f.filing_date
    """
    return sql, params


def materialize_fundamentals_slice(
    *,
    ciks: Sequence[int],
    knowledge_from: int,                 # inclusive lower bound (epoch s)
    knowledge_to: int,                   # exclusive upper bound (half-open, the build.py:129-130 pattern)
    concepts: Sequence[str] = (),        # default: all FUNDAMENTAL_CONCEPTS names
    events_db_path: Optional[Union[str, Path]] = None,
) -> FundamentalsMaterializeResult:
    """Query events.db for the fundamentals fact-table slice and return parquet bytes (§6.1).

    Selects every kept filing whose ``cik`` is in the universe set and whose ``filing_date`` is in
    the half-open window ``[knowledge_from, knowledge_to)``, for the requested concepts (all four
    when ``concepts`` is empty). The slice is ALL vintages (every filing), not an as-of snapshot —
    the container recomputes the as-of join over it (§6.3). Zero matching rows -> a valid 0-row
    parquet carrying the full schema (the event materializer's C2 empty-slice contract),
    fact_count=0.

    ``content_sha256`` is the sha256 of THESE exact bytes (a transfer-integrity checksum of this
    materialization), not a cross-run byte-stability claim — re-derivability is logical: the
    deterministic ORDER BY makes the row set + order a pure function of the (unchanged) source state,
    so a re-materialization is table-equal (parquet byte-stability is not a pyarrow guarantee and is
    not relied on — the event materializer's build.py docstring states the same).
    """
    sql, params = build_fundamentals_slice_sql(               # the single-source §6.1 SQL + window/concept params
        knowledge_from=knowledge_from, knowledge_to=knowledge_to, concepts=concepts,
    )
    params["ciks_json"] = json.dumps([int(c) for c in ciks])  # one bound param for the whole cik list (json_each)

    # Collect each column into a Python list, preserving the SELECT/_SLICE_COLUMNS order. A per-call
    # read connection, no transaction (the materialize_event_slice precedent); the (cik, concept, ...)
    # index prefix narrows the scan to the universe's CIKs, then filing_date is filtered after.
    columns: dict[str, list] = {name: [] for name in _SLICE_COLUMNS}
    with eventsdb.get_events_conn(events_db_path) as conn:     # per-call read connection (no transaction)
        for row in conn.execute(sql, params):                 # stream the join result in the deterministic order
            for name in _SLICE_COLUMNS:
                columns[name].append(row[name])               # row_factory=sqlite3.Row -> by-name access

    # Build one pyarrow array per column against the locked schema (validates types/nullability), then
    # serialize parquet exactly as the event materializer does (snappy, version 2.6).
    arrays = [pa.array(columns[name], type=_SLICE_SCHEMA.field(name).type) for name in _SLICE_COLUMNS]
    table = pa.Table.from_arrays(arrays, schema=_SLICE_SCHEMA)  # locked schema; a type mismatch raises here

    buf = io.BytesIO()                                         # in-memory sink; the body is buffered (the sha header precedes it)
    pq.write_table(table, buf, compression="snappy", version="2.6")
    parquet_bytes = buf.getvalue()                            # the 200 body

    return FundamentalsMaterializeResult(
        parquet_bytes=parquet_bytes,
        fact_count=table.num_rows,                            # X-Fact-Count
        content_sha256=hashlib.sha256(parquet_bytes).hexdigest(),  # X-Content-SHA256
    )


def default_concept_names() -> tuple[str, ...]:
    """The four shipped core concept names — the materializer's ``concepts=()`` ("all") set (§2.1).

    A helper so a caller (the worker route, a probe) can name the default explicitly rather than
    hard-coding the four strings; it stays in lockstep with ``FUNDAMENTAL_CONCEPTS`` (NFR-11: a new
    concept appended there is materialized by default with no edit here).
    """
    return tuple(spec.name for spec in FUNDAMENTAL_CONCEPTS)
