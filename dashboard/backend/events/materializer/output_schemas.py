"""Per-event-type materialized-parquet output-column registry (spec brief §8.1-§8.4).

The materializer dispatches on `event_type` to one ordered list of `ColumnSpec`.
Each spec says where the column's value comes from (`source_kind`) and, for
derived columns, how to compute it (`derive`). `build.py` walks the list to
fill the pyarrow table.

§8.4 cross-check (BL1): the materializer is FREE to drop or derive columns, but
every column it sources straight from `payload.X` MUST have `X` in the matching
per-type validator's REQUIRED_FIELDS ∪ OPTIONAL_FIELDS. Spine / join / derived
columns are exempt. `assert_payload_columns_covered()` enforces this and runs
once at import so a future validator edit that desyncs the materializer fails
loud at dashboard boot, not silently in a backtest.

Adding a future event type = adding one entry to OUTPUT_SCHEMAS + EVENT_TYPE_SOURCES.
No change to the existing types (arch §9.3 extensibility).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pyarrow as pa

# The two per-type validators whose field sets the §8.4 cross-check is checked
# against. Imported at module level so the import-time self-guard below can run.
from ..types import analyst_rating as _analyst_validator
from ..types import earnings as _earnings_validator
from ..types import sec_13d as _sec_13d_validator


# A row context dict is what build.py hands each derive() callable and what the
# value extractor reads spine/join/payload values from. Keys:
#   event_id, event_type, natural_key, knowledge_at, occurred_at, source,
#   producer_id  (spine columns, straight from research_events)
#   subject_cik, cusip, role                       (join, from research_event_companies)
#   payload                                        (parsed dict from research_events.payload)
#   bridge_ticker, membership_match, universe_id_present  (M3.5 brief B / EL65;
#       build.py injects all three unconditionally — the 13D bridge ticker, the
#       matched membership episode id, and the per-call universe-bound flag)
RowContext = dict


class PayloadColumnCoverageError(RuntimeError):
    """Raised when a materializer payload-sourced column has no matching validator field (§8.4 drift)."""


class EventTypeRegistryDriftError(RuntimeError):
    """Raised when the canonical event-type set disagrees across its hand-listed copies."""


@dataclass(frozen=True)
class ColumnSpec:
    """One output column. `source_kind` decides how build.py fills it.

    source_kind:
      'spine'   - value = ctx[source]; `source` is the research_events column / ctx key.
      'join'    - value = ctx[source]; `source` is the research_event_companies-derived ctx key.
      'payload' - value = ctx['payload'].get(source); `source` is the payload JSON key.
      'derived' - value = derive(ctx); `source` is a human note, `derive` does the work.
    arrow_type is the locked pyarrow type (§8.1-§8.3). nullable mirrors the spec
    (occurred_at / cusip / accession_number / old_grade / resolved_at are the
    only nullable columns).
    """
    name: str                                    # output parquet column name
    arrow_type: pa.DataType                      # locked pyarrow type per §8.1-§8.3
    source_kind: str                             # 'spine' | 'join' | 'payload' | 'derived'
    source: str                                  # ctx key / payload key / derivation note
    nullable: bool = False                       # schema nullability (default NOT NULL)
    derive: Optional[Callable[[RowContext], object]] = None  # set iff source_kind == 'derived'


# --- Derivation formulas for the sec_13d derived columns (§8.2, EL26) ---------
# The 13D natural_key is `filer_cik || '_' || accession_number` (EL12); the
# accession's own separators are dashes, never underscores, so split('_', 1) is
# unambiguous. A malformed natural_key is a data-integrity bug and is allowed to
# raise (no fallback) -> the route maps it to a 500 (A11).

def _derive_accession_number(ctx: RowContext) -> Optional[str]:
    """Common-column accession_number: the natural_key tail for 13D, the 8-K accession for earnings
    (payload, absent when no Item 2.02 8-K was found), NULL for analyst (§8.1)."""
    if ctx["event_type"] == "sec_13d":                 # 13D carries an accession in its natural_key
        return ctx["natural_key"].split("_", 1)[1]     # tail after the filer_cik prefix
    if ctx["event_type"] == "earnings":                # the column exists once; earnings fills it from its payload
        return ctx["payload"].get("accession_number")
    return None                                        # analyst rows have no accession


def _derive_filer_cik(ctx: RowContext) -> int:
    """sec_13d filer_cik: the natural_key head as int (§8.2 / MA1)."""
    return int(ctx["natural_key"].split("_", 1)[0])    # head before the first underscore


def _derive_filing_url(ctx: RowContext) -> str:
    """sec_13d filing_url: EDGAR archive dir under the FILER's cik (EL26)."""
    filer_cik, accession = ctx["natural_key"].split("_", 1)  # split once into head/tail
    no_dashes = accession.replace("-", "")             # EDGAR archive paths strip dashes
    return f"https://www.sec.gov/Archives/edgar/data/{int(filer_cik)}/{no_dashes}/"


# --- §3.2 / EL65 derived COMMON columns (M3.5 brief B) ------------------------
# Three denormalized columns both event types carry: the trade-time symbol, its
# coverage flag, and the per-event universe-membership flag (DECISIONS #54). All
# three are "derived" (so the §8.4 boot-time payload-coverage cross-check skips
# them) and read the row context independently, so there is NO inter-column
# evaluation-order dependency (each reads ctx fields, never another derived
# column's output). build.py supplies the join-sourced ctx keys `bridge_ticker`
# (the 13D bridge ticker), `membership_match` (the matched membership episode id),
# and `universe_id_present` (whether the call bound a universe_id).

def _symbol_source(ctx: RowContext) -> Optional[str]:
    """The per-type symbol source for a row, BEFORE the resolved/unresolved verdict.

    analyst_rating: the payload `raw_ticker` (always present, no bridge lookup).
    sec_13d: the bridge-resolved `bridge_ticker` build.py joined on (NULL when the
    bridge has no row for subject_cik). Both _derive_trade_time_symbol and
    _derive_symbol_coverage read THIS (the same ctx fields), so the two derives
    are order-independent — neither reads the other's computed column.
    """
    if ctx["event_type"] in ("analyst_rating", "earnings"):  # both carry the ticker in their payload
        return ctx["payload"].get("raw_ticker")
    return ctx["bridge_ticker"]                        # sec_13d: the joined bridge ticker (NULL value if no bridge row; key always present)


def _derive_trade_time_symbol(ctx: RowContext) -> Optional[str]:
    """trade_time_symbol: the per-type symbol source (EL64/EL65). NULL when 13D has no bridge row."""
    return _symbol_source(ctx)                         # NULL flows through as the 'unresolved' case


def _derive_symbol_coverage(ctx: RowContext) -> str:
    """symbol_coverage: 'resolved' when the per-type source is non-null, else 'unresolved' (2a §3, EL66)."""
    return "resolved" if _symbol_source(ctx) is not None else "unresolved"


def _derive_membership_valid(ctx: RowContext) -> Optional[bool]:
    """membership_valid: was subject_cik a universe member at this event's knowledge_at (DECISIONS #54).

    None  — no universe_id was supplied (an M2.5 direct caller); the gate is N/A.
    True  — a universe_id was bound and the membership LEFT JOIN matched an episode.
    False — a universe_id was bound but no episode covered this event's knowledge_at
            (out-of-membership, or the cik is not in this universe). The §9 pipeline
            drops-and-counts these (EL66/EL78); they are never silently dropped.
    """
    if not ctx["universe_id_present"]:                 # no universe declared -> flag is NULL
        return None
    return ctx["membership_match"] is not None         # bound: matched episode -> True, else False (key always present; value may be NULL)


# --- §8.1 common columns (every output parquet, both event types) -------------
# accession_number is listed ONLY here (derived); §8.2 documents the same
# derivation but the column is emitted once, not twice (§8.1 note / B14).
COMMON_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("event_id",     pa.int64(),                       "spine", "event_id"),
    ColumnSpec("event_type",   pa.string(),                      "spine", "event_type"),
    ColumnSpec("natural_key",  pa.string(),                      "spine", "natural_key"),
    ColumnSpec("knowledge_at", pa.timestamp("ns", tz="UTC"),     "spine", "knowledge_at"),
    ColumnSpec("occurred_at",  pa.timestamp("ns", tz="UTC"),     "spine", "occurred_at", nullable=True),
    ColumnSpec("source",       pa.string(),                      "spine", "source"),
    ColumnSpec("subject_cik",  pa.int64(),                       "join",  "subject_cik"),
    ColumnSpec("cusip",        pa.string(),                      "join",  "cusip", nullable=True),
    ColumnSpec("role",         pa.string(),                      "join",  "role"),
    ColumnSpec("accession_number", pa.string(),                  "derived", "natural_key.split('_',1)[1] for sec_13d else NULL",
               nullable=True, derive=_derive_accession_number),
    # M3.5 brief B (EL65, §3.2, DECISIONS #54): the trade-time symbol, its coverage
    # flag, and the per-event universe-membership flag. All "derived" (the §8.4
    # cross-check skips them); the no-network container reads these flat off the row.
    ColumnSpec("trade_time_symbol", pa.string(),                 "derived",
               "analyst_rating: payload raw_ticker; sec_13d: bridge-resolved ticker for subject_cik (as-of-today), else NULL",
               nullable=True, derive=_derive_trade_time_symbol),
    ColumnSpec("symbol_coverage", pa.string(),                   "derived",
               "'resolved' when trade_time_symbol is non-null, else 'unresolved' (2a §3 coverage flag, EL66)",
               derive=_derive_symbol_coverage),
    ColumnSpec("membership_valid", pa.bool_(),                   "derived",
               "True/False: was subject_cik a member of universe_id at this event's knowledge_at; "
               "NULL when no universe_id was supplied (2a §2 amended, per-event gate, EL66/EL78, DECISIONS #54)",
               nullable=True, derive=_derive_membership_valid),
]

# --- §8.2 sec_13d per-type columns (appended after the common set) ------------
SEC_13D_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("percent_owned", pa.float64(), "payload", "percent_owned", nullable=True),  # nullable since the parser fix: a 13D event is valid without a parsed cover-page %
    ColumnSpec("filer_name",    pa.string(),  "payload", "filer_name"),
    ColumnSpec("subject_name",  pa.string(),  "payload", "subject_name"),
    ColumnSpec("filer_cik",     pa.int64(),   "derived", "int(natural_key.split('_',1)[0])",
               derive=_derive_filer_cik),
    ColumnSpec("filing_url",    pa.string(),  "derived", "EDGAR archive dir under filer_cik (EL26)",
               derive=_derive_filing_url),
]

# --- §8.3 analyst_rating per-type columns ------------------------------------
ANALYST_RATING_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("raw_ticker",             pa.string(),  "payload", "raw_ticker"),
    ColumnSpec("action_company",         pa.string(),  "payload", "action_company"),
    ColumnSpec("new_grade",              pa.string(),  "payload", "new_grade"),
    ColumnSpec("old_grade",              pa.string(),  "payload", "old_grade", nullable=True),
    ColumnSpec("analyst_firm",           pa.string(),  "payload", "analyst_firm"),
    ColumnSpec("analyst_person",         pa.string(),  "payload", "analyst_person", nullable=True),
    ColumnSpec("pt_current",             pa.float64(), "payload", "pt_current", nullable=True),
    ColumnSpec("pt_prior",               pa.float64(), "payload", "pt_prior", nullable=True),
    ColumnSpec("importance",             pa.int64(),   "payload", "importance", nullable=True),
    ColumnSpec("exchange",               pa.string(),  "payload", "exchange", nullable=True),
    ColumnSpec("producer_id",            pa.string(),  "spine",   "producer_id"),
    ColumnSpec("producer_model_version", pa.string(),  "payload", "producer_model_version"),
    ColumnSpec("resolved_at", pa.timestamp("ns", tz="UTC"), "payload", "resolved_at", nullable=True),
]

# --- earnings per-type columns (lane EA2) -------------------------------------
# accession_number (the 8-K's, nullable) is the COMMON column of that name, filled from the payload
# for earnings, so the schema holds it once. filing_accepted_at is the 8-K's SEC acceptance time.
EARNINGS_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("raw_ticker",     pa.string(),  "payload", "raw_ticker"),
    ColumnSpec("fiscal_quarter", pa.int64(),   "payload", "fiscal_quarter"),
    ColumnSpec("adjusted",       pa.bool_(),   "payload", "adjusted"),
    ColumnSpec("eps_actual",     pa.float64(), "payload", "eps_actual"),
    ColumnSpec("eps_estimate",   pa.float64(), "payload", "eps_estimate"),
    ColumnSpec("eps_word",       pa.string(),  "payload", "eps_word"),
    ColumnSpec("sales_actual",   pa.float64(), "payload", "sales_actual"),
    ColumnSpec("sales_estimate", pa.float64(), "payload", "sales_estimate"),
    ColumnSpec("sales_word",     pa.string(),  "payload", "sales_word"),
    ColumnSpec("release_at",     pa.timestamp("ns", tz="UTC"), "payload", "release_at"),
    ColumnSpec("is_update",      pa.bool_(),   "payload", "is_update"),
    ColumnSpec("filing_accepted_at", pa.timestamp("ns", tz="UTC"), "payload", "filing_accepted_at",
               nullable=True),
]

# The registry the materializer dispatches on. Common columns first, then the
# per-type columns, so column order is stable and documented (B13).
OUTPUT_SCHEMAS: dict[str, list[ColumnSpec]] = {
    "sec_13d":        COMMON_COLUMNS + SEC_13D_COLUMNS,
    "analyst_rating": COMMON_COLUMNS + ANALYST_RATING_COLUMNS,
    "earnings":       COMMON_COLUMNS + EARNINGS_COLUMNS,
}

# event_type -> the SET of sources that can write that type. analyst_rating is a
# TWO-source type (the canonical JSONL loader + the Benzinga ratings calendar);
# sec_13d is single-source (the EDGAR fetcher). Used as the registered fallback for
# the X-Source-Series header on a ZERO-ROW materialization (where there is no row to
# read `source` from); on a non-empty result the header is derived from the rows'
# actual `source` values (build.py). A new SOURCE for an existing type extends the
# relevant set here (and the research_events / research_event_batches source CHECK
# enums in eventsdb.py); a new event TYPE adds a key (and trips the registry drift
# guard until every copy is updated).
EVENT_TYPE_SOURCES: dict[str, frozenset[str]] = {
    "sec_13d":        frozenset({"sec_edgar"}),
    "analyst_rating": frozenset({"canonical_loader", "benzinga"}),
    "earnings":       frozenset({"alpaca_news", "canonical_loader"}),
}


def arrow_schema(event_type: str) -> pa.Schema:
    """The locked pyarrow schema for one event type's output parquet."""
    specs = OUTPUT_SCHEMAS[event_type]                       # KeyError on an unknown type (route guards first)
    return pa.schema(
        [pa.field(s.name, s.arrow_type, nullable=s.nullable) for s in specs]
    )


def assert_payload_columns_covered() -> None:
    """§8.4 cross-check: every payload-sourced column's key is in its validator.

    Raises PayloadColumnCoverageError naming the offending (event_type, column,
    key) so a drift between a validator and the materializer surfaces loudly.
    Runs once at import (below) as the boot-time self-guard.
    """
    validators = {                                          # per-type field sets to check against
        "sec_13d": _sec_13d_validator,
        "analyst_rating": _analyst_validator,
        "earnings": _earnings_validator,
    }
    for event_type, specs in OUTPUT_SCHEMAS.items():        # walk every registered type
        validator = validators[event_type]                 # its validator module
        allowed = set(validator.REQUIRED_FIELDS) | set(validator.OPTIONAL_FIELDS)  # the payload keys it accepts
        for spec in specs:                                 # walk that type's columns
            if spec.source_kind != "payload":              # spine/join/derived are exempt
                continue
            if spec.source not in allowed:                 # the cross-check itself (raise, not assert: -O-proof)
                raise PayloadColumnCoverageError(
                    f"materializer {event_type} column {spec.name!r} reads payload key "
                    f"{spec.source!r}, which is not in the validator's "
                    f"REQUIRED_FIELDS ∪ OPTIONAL_FIELDS ({sorted(allowed)}); §8.4 drift"
                )


def assert_event_type_registry_consistent() -> None:
    """Cross-check the TWO-TIER event-type registry split (F7) at boot.

    The event-type set is enumerated in several hand-listed places with no structural
    link. F7 split them into two tiers, and this guard enforces both:

    - INGEST tier — every ADMISSIBLE type must be ingest-validatable:
        ``set(SPEC_REGISTRY) == set(eventsdb.EVENT_TYPES)``
      (``EVENT_TYPES`` is the pyarrow-free owner, matched by the
      ``research_events.event_type`` CHECK and enumerated by the feed/catalog read
      paths; ``SPEC_REGISTRY`` is the write-time validator dispatch). This assertion is
      NEW with F7 — without it a type added to ``EVENT_TYPES`` but forgotten in
      ``SPEC_REGISTRY`` would pass boot and fail only at first ingest.

    - BACKTEST tier — only BACKTEST-GRADE types may materialize / load:
        ``set(OUTPUT_SCHEMAS) == set(EVENT_TYPE_SOURCES)
         == set(jsonl_canonical._ALLOWED_EVENT_TYPES) == set(eventsdb.BACKTEST_GRADE_EVENT_TYPES)``
      A non-backtest-grade type (e.g. ``news``) is admissible + ingestable but held OUT
      of these three registries; this assertion makes that hold-out REQUIRED, not drift —
      it is the structural not-backtest-grade marker (a future edit adding news to any of
      the three fails startup).

    This raises (not ``assert`` — stripped under ``python -O``) at import so any drift
    fails dashboard startup loudly instead of surfacing as a silent gap later. Homed here
    (the heavy module, already imported at boot) so it checks itself against the light
    canonical owner, keeping the brief-K read path off the parquet stack.
    """
    from ... import eventsdb                                  # the pyarrow-free canonical owner (no cycle)
    from ..loaders import jsonl_canonical                     # the loader gate (imports eventsdb/write_core only)
    from ..types import SPEC_REGISTRY                         # the write-time validator dispatch (ingest tier)

    # Ingest tier: SPEC_REGISTRY must cover every admissible type.
    admissible = set(eventsdb.EVENT_TYPES)                    # the owner is the reference set
    if set(SPEC_REGISTRY) != admissible:
        raise EventTypeRegistryDriftError(
            f"ingest-tier drift: eventsdb.EVENT_TYPES={sorted(admissible)} but "
            f"SPEC_REGISTRY={sorted(SPEC_REGISTRY)}. Every admissible event type needs a "
            f"per-type validator in events/types.SPEC_REGISTRY (ingest validates via get_spec)."
        )

    # Backtest tier: the three strategy-readable registries must equal the backtest-grade subset.
    backtest = set(eventsdb.BACKTEST_GRADE_EVENT_TYPES)       # the strategy-readable subset
    copies = {                                               # every backtest registry that must equal it
        "EVENT_TYPE_SOURCES": set(EVENT_TYPE_SOURCES),
        "OUTPUT_SCHEMAS": set(OUTPUT_SCHEMAS),
        "jsonl_canonical._ALLOWED_EVENT_TYPES": set(jsonl_canonical._ALLOWED_EVENT_TYPES),
    }
    drifted = {name: sorted(s) for name, s in copies.items() if s != backtest}  # the ones that disagree
    if drifted:                                              # any disagreement is a hard boot failure
        raise EventTypeRegistryDriftError(
            f"backtest-tier drift: eventsdb.BACKTEST_GRADE_EVENT_TYPES={sorted(backtest)} but "
            + "; ".join(f"{name}={vals}" for name, vals in drifted.items())
            + ". A backtest-grade type lives in EVENT_TYPE_SOURCES/OUTPUT_SCHEMAS AND "
            "jsonl_canonical._ALLOWED_EVENT_TYPES; a non-backtest-grade type (e.g. news) is "
            "held OUT of all three (the structural not-backtest-grade marker)."
        )


# Boot-time self-guard: a validator edit that desyncs the materializer fails the
# import (and dashboard startup) instead of producing a wrong parquet later.
assert_payload_columns_covered()
# Boot-time self-guard: the canonical event-type set drifting across its copies
# (e.g. a new type added to eventsdb.EVENT_TYPES but not here, or vice versa) fails
# startup instead of a type silently never reaching the live-feeds page.
assert_event_type_registry_consistent()
