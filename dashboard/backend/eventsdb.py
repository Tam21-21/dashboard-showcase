"""M2.5 event-store DB access — mirrors researchdb.py's pattern.

A separate SQLite file (`events.db`) lives alongside `research.db` on the
server. The split is justified by per-database single-writer WAL semantics
and operational backup blast radius (EL1 / spec brief §3 / arch v6 §3.2).
Cross-DB foreign keys are not supported in SQLite; readers join across
files via `ATTACH DATABASE`.

Connection discipline mirrors researchdb.py:
  1. foreign_keys is per-connection in SQLite — must set on every new conn
  2. journal_mode=WAL is persistent at the DB file level
  3. isolation_level=None means we manage transactions explicitly

Per EL35 / round-3 RN7 the connection lifecycle is per-call (no singleton):
get_events_conn() and events_transaction() open a fresh connection on enter
and close it on exit. The events_db_path argument is explicit on every
entry point so probes can target a throwaway file.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from .config import EVENTS_DB_PATH

# Module-level default path; probes pass an override explicitly.
DB_PATH = EVENTS_DB_PATH

# Minimum SQLite version. 3.44 is required for in-aggregate ORDER BY inside
# json_group_array (spec brief §8.5 canonical close-hash SQL relies on it).
# 3.24 covers UPSERT (ON CONFLICT DO UPDATE), needed by §9 catalog refresh.
# 3.44 implies 3.24, so the single check below is sufficient.
_MIN_SQLITE_VERSION = (3, 44, 0)


class SQLiteVersionTooOld(RuntimeError):
    """Raised when the runtime SQLite is below the version §8.5 needs."""


def _assert_sqlite_version() -> None:
    """Fail fast if the runtime SQLite cannot support the spec-brief invariants."""
    # sqlite3.sqlite_version_info is (major, minor, patch) for the linked library,
    # which is what executes the SQL (not the Python wrapper version).
    if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
        need = ".".join(str(n) for n in _MIN_SQLITE_VERSION)
        have = sqlite3.sqlite_version
        raise SQLiteVersionTooOld(
            f"events.db requires SQLite >= {need} for in-aggregate ORDER BY "
            f"inside json_group_array (spec brief §8.5); runtime is {have}"
        )


def _connect_events(events_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on `events.db` with per-connection pragmas applied."""
    # autocommit mode; we own the transactions explicitly via BEGIN/COMMIT/ROLLBACK
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    # foreign keys are per-connection in SQLite; FK violations silently no-op without this
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL is persistent at the DB file level; re-running is a cheap no-op
    conn.execute("PRAGMA journal_mode = WAL;")
    # dict-like rows so callers can write row["column"] instead of indexing by position
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_events_conn(events_db_path: Union[str, Path, None] = None):
    """Context manager for read-only queries or single-statement writes on events.db.

    Per-call lifecycle: opens a fresh connection on enter, closes on exit.
    No singleton, no pooling (EL35 / RN7).
    """
    path = events_db_path if events_db_path is not None else DB_PATH
    conn = _connect_events(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def events_transaction(events_db_path: Union[str, Path, None] = None):
    """Context manager wrapping BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection.

    For multi-statement writes. Commits on clean exit, rolls back on exception.
    """
    path = events_db_path if events_db_path is not None else DB_PATH
    conn = _connect_events(path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


@contextmanager
def transaction_on(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE / COMMIT / ROLLBACK on an ALREADY-OPEN connection (no open/close).

    The `events_transaction` sibling for a caller that holds ONE connection across MANY short
    transactions — e.g. a multi-hundred-CIK backfill (fundamentals write path, §3.2) that must
    open the DB once, not once per CIK, yet still release the write lock between CIKs (a held
    BEGIN across the slow network fetches would block every other writer for the whole backfill).
    The caller owns the connection's lifecycle (open it via get_events_conn, close on exit); this
    only brackets one transaction on it. Commits on clean exit, rolls back + re-raises on exception
    so the connection is left clean for the next transaction.
    """
    conn.execute("BEGIN IMMEDIATE;")                            # acquire the write lock for this one transaction
    try:
        yield conn
        conn.execute("COMMIT;")                                 # release the lock on success
    except BaseException:
        # A rollback FENCE, not a catch-all that swallows: it re-raises unconditionally and never
        # absorbs the error. BaseException (not Exception) is the recognized must-not-suppress
        # form — it also rolls back on KeyboardInterrupt / SystemExit, leaving the HELD connection
        # clean for the next CIK's transaction (the sibling events_transaction closes its fresh
        # connection on the way out, so it does not need this; a held connection does).
        conn.execute("ROLLBACK;")
        raise


# =============================================================================
# Schema DDL. All nine tables (the eight M2.5 tables — seven schema tables plus
# the schema-version stamp — and the M3.5 brief A universe-membership table) are
# created together inside init_events_db() in one transaction per spec brief
# §3.9 + §3.1. Every statement is idempotent (CREATE ... IF NOT EXISTS).
# =============================================================================

# E2 - research_event_requests (created first; it is an FK target for batches).
# Per v4 / RB2 the source CHECK enum is narrowed to ('sec_edgar') only: load
# batches carry request_id = NULL, so 'canonical_loader' would be dead schema.
_DDL_REQUESTS = """
CREATE TABLE IF NOT EXISTS research_event_requests (
    request_id      INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL
                            CHECK (source IN ('sec_edgar')),
    criteria        TEXT    NOT NULL CHECK (json_valid(criteria)),
    created_at      INTEGER NOT NULL,
    last_batch_id   INTEGER,
    FOREIGN KEY (last_batch_id) REFERENCES research_event_batches(batch_id)
);

CREATE INDEX IF NOT EXISTS ix_requests_source ON research_event_requests(source);
"""

# E3 - research_event_batches. Self-FK on parent_batch_id; FK to requests already
# created at E2. The failure_class CHECK enum is the step-3 finalization of the
# enumeration arch §13 explicitly delegated to this brief (no CHECK exists in
# arch §4.4; the column is plain TEXT NULLABLE in arch).
_DDL_BATCHES = """
CREATE TABLE IF NOT EXISTS research_event_batches (
    batch_id            INTEGER PRIMARY KEY,
    request_id          INTEGER,
    kind                TEXT    NOT NULL
                                CHECK (kind IN ('fetch','load','correction','rollback')),
    parent_batch_id     INTEGER,
    source              TEXT    NOT NULL
                                CHECK (source IN ('sec_edgar','canonical_loader','benzinga','alpaca_news')),
    policy              TEXT    NOT NULL
                                CHECK (policy IN ('quarantine','abort_on_first_error')),
    realized_start_at   INTEGER,
    realized_end_at     INTEGER,
    source_descriptor   TEXT    NOT NULL,
    content_hash        TEXT,
    started_at          INTEGER NOT NULL,
    finished_at         INTEGER,
    last_heartbeat_at   INTEGER NOT NULL,
    status              TEXT    NOT NULL
                                CHECK (status IN ('running','succeeded','succeeded_with_quarantines','failed','rolled_back','cancelled_partial')),
    quarantine_count    INTEGER NOT NULL DEFAULT 0,
    event_count         INTEGER NOT NULL DEFAULT 0,
    quality_report      TEXT    CHECK (quality_report IS NULL OR json_valid(quality_report)),
    failure_class       TEXT    CHECK (
                                  failure_class IS NULL OR
                                  failure_class IN (
                                    'EdgarHttp5xx',
                                    'EdgarHttp429Persistent',
                                    'EdgarNetworkTimeout',
                                    'EdgarParseFatal',
                                    'LoaderFileUnreadable',
                                    'LoaderEnvelopeInvalid',
                                    'LoaderJsonDecodeError',
                                    'TickerNotFoundInBridge',
                                    'RecordValidationAbort',
                                    'OrphanedRunningBatch',
                                    'Cancelled'
                                  )
                                ),
    failure_context     TEXT    CHECK (failure_context IS NULL OR json_valid(failure_context)),
    FOREIGN KEY (request_id)      REFERENCES research_event_requests(request_id),
    FOREIGN KEY (parent_batch_id) REFERENCES research_event_batches(batch_id)
);

CREATE INDEX IF NOT EXISTS ix_batches_source_started
  ON research_event_batches(source, started_at);

-- Heartbeat-reaper sweep predicate access path: a partial index on the rare
-- value ('running') gives selectivity vs a full-table scan over every closed
-- batch ever written. Step-3 finalization per arch §13 delegation of partial-
-- index syntax (not an arch amendment).
CREATE INDEX IF NOT EXISTS ix_batches_running_heartbeat
  ON research_event_batches(last_heartbeat_at)
  WHERE status = 'running';
"""

# The canonical enumerable list of ALL admissible event types. MUST stay in sync
# with: the research_events.event_type CHECK below, the events/types.SPEC_REGISTRY
# keys (the write-time validator dispatch — the INGEST tier), and the read paths
# that enumerate every known type (events/feeds.py, catalog.py). Lives here, in the
# pyarrow-free schema owner, so any read path (e.g. the M3.5 brief K live-feeds
# summary) can enumerate every known type — including one with zero rows, for an
# honest empty state — without importing the parquet/pyarrow stack that
# output_schemas pulls in.
#
# F7 two-tier model: not every admissible type is strategy-readable. BACKTEST_GRADE
# is the SUBSET a strategy may materialize/load; the difference (here, "news") is the
# not-backtest-grade marker, enforced structurally — held OUT of the three backtest
# registries (output_schemas.OUTPUT_SCHEMAS / EVENT_TYPE_SOURCES and
# jsonl_canonical._ALLOWED_EVENT_TYPES), with output_schemas's boot guard asserting
# that hold-out (two-tier: SPEC_REGISTRY == EVENT_TYPES; the three backtest registries
# == BACKTEST_GRADE_EVENT_TYPES). News is ingest-and-display only (F7); a future unit
# decides its event-study semantics before it becomes backtest-grade.
#
# Adding a future ADMISSIBLE type touches this tuple, the CHECK below, and
# SPEC_REGISTRY. Adding a future BACKTEST-GRADE type ALSO touches BACKTEST_GRADE_EVENT_TYPES,
# the output_schemas registries, and the loader's _ALLOWED_EVENT_TYPES.
EVENT_TYPES: tuple[str, ...] = ("sec_13d", "analyst_rating", "news", "earnings")
# The subset of EVENT_TYPES a strategy may materialize/load. News is held out (the
# not-backtest-grade marker). Owned here so researchdb's write-time vocabulary check
# and output_schemas's boot guard read ONE source of truth.
BACKTEST_GRADE_EVENT_TYPES: tuple[str, ...] = ("sec_13d", "analyst_rating", "earnings")

# E4 - research_events. FKs to batches (E3). Partial UNIQUE on the active rows
# only per EL14: rollback flips status to 'rolled_back' and the row leaves the
# unique-index entries, so a re-ingest after rollback succeeds (BL2).
_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS research_events (
    event_id               INTEGER PRIMARY KEY,
    event_type             TEXT    NOT NULL
                                   CHECK (event_type IN ('sec_13d','analyst_rating','news','earnings')),
    status                 TEXT    NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('active','rolled_back')),
    producer_id            TEXT,
    natural_key            TEXT    NOT NULL,
    occurred_at            INTEGER,
    knowledge_at           INTEGER NOT NULL,
    ingested_at            INTEGER NOT NULL,
    source                 TEXT    NOT NULL
                                   CHECK (source IN ('sec_edgar','canonical_loader','benzinga','alpaca_news')),
    origin                 TEXT    NOT NULL
                                   CHECK (origin IN ('fetched','loaded','imported')),
    batch_id               INTEGER NOT NULL,
    payload                TEXT    NOT NULL CHECK (json_valid(payload)),
    payload_schema_version INTEGER NOT NULL,
    raw_input              TEXT,
    version                INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (batch_id) REFERENCES research_event_batches(batch_id)
);

-- Partial UNIQUE (EL14 / arch v6 BL2): a rolled-back row keeps its natural_key
-- in the table but is not in the unique index, so the corrected re-ingest can
-- write the same natural_key with status='active' alongside it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_natural_active
  ON research_events(event_type, natural_key)
  WHERE status = 'active';

-- Bulk-load access path per arch §4.1 round-1 M5: "events of one type over a
-- window" walks (event_type, knowledge_at).
CREATE INDEX IF NOT EXISTS ix_events_type_knowledge
  ON research_events(event_type, knowledge_at);

-- Hot path: §8.5 close-hash SELECT and every failed-event rollback filter on
-- batch_id; without this index those operations are full-table scans. Step-3
-- finalization per arch §13's partial-index delegation (not an arch amendment).
CREATE INDEX IF NOT EXISTS ix_events_batch
  ON research_events(batch_id);
"""

# E5 - research_events_history. Append-only two-table versioning (EL15).
# correction_batch_id is NOT NULL; for the failed-event rollback path (§5.1)
# the history row records correction_batch_id = batch_id (the failed batch's
# own id), so a reader can detect "rolled back by own batch failure" via
# correction_batch_id = batch_id (vs a normal rollback-batch where they differ).
_DDL_EVENTS_HISTORY = """
CREATE TABLE IF NOT EXISTS research_events_history (
    history_id             INTEGER PRIMARY KEY,
    event_id               INTEGER NOT NULL,
    event_type             TEXT    NOT NULL,
    status                 TEXT    NOT NULL
                                   CHECK (status IN ('active','rolled_back')),
    producer_id            TEXT,
    natural_key            TEXT    NOT NULL,
    occurred_at            INTEGER,
    knowledge_at           INTEGER NOT NULL,
    ingested_at            INTEGER NOT NULL,
    source                 TEXT    NOT NULL,
    origin                 TEXT    NOT NULL,
    batch_id               INTEGER NOT NULL,
    payload                TEXT    NOT NULL CHECK (json_valid(payload)),
    payload_schema_version INTEGER NOT NULL,
    raw_input              TEXT,
    version                INTEGER NOT NULL,
    corrected_at           INTEGER NOT NULL,
    correction_batch_id    INTEGER NOT NULL,
    prev_version           INTEGER,
    FOREIGN KEY (event_id)            REFERENCES research_events(event_id),
    FOREIGN KEY (batch_id)            REFERENCES research_event_batches(batch_id),
    FOREIGN KEY (correction_batch_id) REFERENCES research_event_batches(batch_id)
);

-- (event_id, version) serves point-in-time reads ("what was event E as of T").
CREATE INDEX IF NOT EXISTS ix_events_hist_event ON research_events_history(event_id, version);
-- batch_id and correction_batch_id both serve the §8.5 history-inclusive
-- verification query (both branches of the UNION ALL).
CREATE INDEX IF NOT EXISTS ix_events_hist_batch ON research_events_history(batch_id);
CREATE INDEX IF NOT EXISTS ix_events_hist_correction_batch ON research_events_history(correction_batch_id);
"""

# E6 - research_event_companies. (event_id, cik) is the PK; the reverse-direction
# index serves the bulk-load access pattern ("all events for this company set").
_DDL_COMPANIES = """
CREATE TABLE IF NOT EXISTS research_event_companies (
    event_id INTEGER NOT NULL,
    cik      INTEGER NOT NULL,
    role     TEXT    NOT NULL DEFAULT 'primary_issuer'
                     CHECK (role IN ('primary_issuer','co_mentioned')),
    cusip    TEXT,
    PRIMARY KEY (event_id, cik),
    FOREIGN KEY (event_id) REFERENCES research_events(event_id)
);

CREATE INDEX IF NOT EXISTS ix_event_companies_cik
  ON research_event_companies(cik, event_id);
"""

# E7 - research_event_quarantines. failure_class deliberately has no CHECK enum
# (MN6) because the per-record exception set is parser-dependent and grows when
# new event types land; the shared core constrains values in code instead.
_DDL_QUARANTINES = """
CREATE TABLE IF NOT EXISTS research_event_quarantines (
    quarantine_id     INTEGER PRIMARY KEY,
    batch_id          INTEGER NOT NULL,
    event_type        TEXT    NOT NULL,
    raw_input         TEXT    NOT NULL,
    accession_number  TEXT,
    failure_step      TEXT    NOT NULL
                              CHECK (failure_step IN ('parse','validate')),
    failure_class     TEXT    NOT NULL,
    failure_message   TEXT    NOT NULL,
    created_at        INTEGER NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES research_event_batches(batch_id)
);

CREATE INDEX IF NOT EXISTS ix_quarantines_batch ON research_event_quarantines(batch_id);
"""

# E8 - research_cik_ticker_map. effective_from / effective_to are nullable; M2.5
# sources leave both NULL (interval columns exist for a future paid/curated source).
# 'manual' is in the CHECK enum so an operator override can be inserted without
# a schema migration; M2.5 does not populate manual rows (RN9: step-3 finalized).
_DDL_CIK_TICKER_MAP = """
CREATE TABLE IF NOT EXISTS research_cik_ticker_map (
    map_id            INTEGER PRIMARY KEY,
    cik               INTEGER NOT NULL,
    ticker            TEXT    NOT NULL,
    effective_from    INTEGER,
    effective_to      INTEGER,
    source            TEXT    NOT NULL
                              CHECK (source IN ('sec_company_tickers','sec_submissions_api','openfigi','manual')),
    name              TEXT,
    loaded_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cik_ticker_ticker
  ON research_cik_ticker_map(ticker, effective_from);

CREATE INDEX IF NOT EXISTS ix_cik_ticker_cik
  ON research_cik_ticker_map(cik, effective_from);

-- ANA-1 §1.3: the catalog name-prefix search leg (`name LIKE 'q%'`). NOCASE collation so the
-- default case-insensitive LIKE can seek the index (a BINARY index is skipped for a
-- case-insensitive LIKE — the EXPLAIN-measured §8.1 decision). CREATE INDEX IF NOT EXISTS lands
-- on the existing prod db on the next boot (the name column is already present by then — the v5
-- ALTER runs before this DDL pass).
CREATE INDEX IF NOT EXISTS ix_cik_ticker_name
  ON research_cik_ticker_map(name COLLATE NOCASE);

-- Phase-2 Unit 4 (A4a-012, schema v7): the UNIQUE constraint over (cik, ticker, source). Moves
-- dedup from the application-level INSERT ... WHERE NOT EXISTS race (cik_ticker_map.py) into the
-- storage engine, enforced atomically per write (DDIA Ch 4, secondary index). A fresh DB is born
-- with it here; an existing DB gets it (after a dedupe) via _migrate_unique_cik_ticker_v7, which
-- runs BEFORE this DDL block so a duplicate-bearing prod DB never reaches this CREATE with dups.
CREATE UNIQUE INDEX IF NOT EXISTS uq_cik_ticker_source
  ON research_cik_ticker_map(cik, ticker, source);
"""

# E9 - research_events_schema_version. The eighth-table addition arch §11
# counted as seven; the queue-it-as-amendment note belongs to the step-3 lock
# DECISIONS entry (do not refile here).
_DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS research_events_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL,
    note       TEXT
);
"""

# E10 - research_universe_membership (M3.5 brief A; spec brief §3.1, EL62/EL74).
# The ninth table: point-in-time universe membership as a join table made
# historical by the slowly-changing-dimension pattern (validity interval).
# Membership keys on subject_cik (the same key the seeded event types carry and
# the materializer filters on). universe_id is a SOFT cross-DB reference to
# research.db research_datasets.id (dataset_type='universe'); no FOREIGN KEY
# because that row lives in a separate database file. This is 2a's one sanctioned
# new store table — no new column or index on any existing event-store table.
_DDL_UNIVERSE_MEMBERSHIP = """
CREATE TABLE IF NOT EXISTS research_universe_membership (
    membership_id   INTEGER PRIMARY KEY,          -- surrogate; the same (universe, cik) can recur with a new interval
    universe_id     INTEGER NOT NULL,             -- soft cross-DB reference to research_datasets.id (dataset_type='universe')
    subject_cik     INTEGER NOT NULL,             -- the company; the same key research_event_companies.cik uses
    entered_at      INTEGER NOT NULL,             -- epoch seconds UTC, inclusive: when the company entered the universe
    left_at         INTEGER,                      -- epoch seconds UTC, exclusive; NULL = still a member (open-ended)
    source          TEXT    NOT NULL,             -- provenance of this membership episode
    loaded_at       INTEGER NOT NULL              -- snapshot wall-clock (epoch seconds UTC)
);

-- The one additive serving index for this new type's point-in-time read (§4).
-- Part of the membership table's own schema, not an index on an existing table.
CREATE INDEX IF NOT EXISTS ix_universe_membership_pit
    ON research_universe_membership(universe_id, subject_cik, entered_at);

-- Item 1 / CQ-SF1: a partial covering index for the current-members aggregate
-- (universe.member_counts and the GET /datasets listing): COUNT(DISTINCT subject_cik)
-- GROUP BY universe_id WHERE left_at IS NULL. WHERE left_at IS NULL is not in the
-- pit index, so without this the aggregate full-scans the table on every dataset
-- listing; this partial index serves the GROUP BY + DISTINCT from index order and
-- stays small (only open-ended rows). SQLite has supported partial indexes since 3.8.0.
CREATE INDEX IF NOT EXISTS ix_universe_membership_open
    ON research_universe_membership(universe_id, subject_cik) WHERE left_at IS NULL;
"""

# F1 - research_fundamental_batches (M3.75 FUND-1 §1.4). The fundamentals lineage unit,
# SIBLING to research_event_batches (NOT a shared table with a discriminator): forcing
# fundamentals into the event batch table would couple them to its 13D-specific request_id
# FK and its event-specific failure_class enum (arch §2 separation). kind is fetch/refresh
# only — fundamentals have no correction/rollback at M3.75 (a restatement is a new fact row,
# never an in-place correction, §5.1). failure_class carries NO CHECK enum (the per-record
# set is parser-dependent and grows, mirroring research_event_quarantines.failure_class).
# The FK target of both other fundamentals tables, so it is created first.
_DDL_FUNDAMENTAL_BATCHES = """
CREATE TABLE IF NOT EXISTS research_fundamental_batches (
    batch_id            INTEGER PRIMARY KEY,
    kind                TEXT    NOT NULL
                                CHECK (kind IN ('fetch','refresh')),      -- fundamentals are fetch-and-refresh only (no correction/rollback at M3.75)
    source              TEXT    NOT NULL
                                CHECK (source IN ('sec_edgar')),          -- the one source; widen additively if a second ever lands
    started_at          INTEGER NOT NULL,
    finished_at         INTEGER,
    last_heartbeat_at   INTEGER NOT NULL,                                 -- per-CIK liveness write (§3.2); the reaper (§3.3) reads it
    status              TEXT    NOT NULL
                                CHECK (status IN ('running','succeeded','succeeded_with_quarantines','failed','cancelled_partial')),
    cik_count           INTEGER NOT NULL DEFAULT 0,                       -- companies attempted
    fact_count          INTEGER NOT NULL DEFAULT 0,                       -- facts written
    quarantine_count    INTEGER NOT NULL DEFAULT 0,
    quality_report      TEXT    CHECK (quality_report IS NULL OR json_valid(quality_report)),  -- the §3.4 coverage report
    failure_class       TEXT,                                            -- nullable, NO enum (parser-dependent set, mirrors the quarantine table)
    failure_context     TEXT    CHECK (failure_context IS NULL OR json_valid(failure_context))
);

-- Mirrors ix_batches_source_started: the batch-list access path for the dashboard console.
CREATE INDEX IF NOT EXISTS ix_fundamental_batches_status_started
    ON research_fundamental_batches(status, started_at);

-- Mirrors ix_batches_running_heartbeat: a partial index on the rare 'running' value serves
-- the _detect_running_fundamentals 409 query (§3.3) and the stale-batch reaper (§3.3) with
-- selectivity vs a full scan over every closed batch. Backs the per-CIK heartbeat (§3.2).
CREATE INDEX IF NOT EXISTS ix_fundamental_batches_running_heartbeat
    ON research_fundamental_batches(last_heartbeat_at) WHERE status = 'running';
"""

# F2 - research_fundamental_facts (M3.75 FUND-1 §1.1). The tall bitemporal fact table: one
# row per fact, concept-as-DATA (the concept column carries NO CHECK enum, so a new concept
# is an INSERT not an ALTER — the load-bearing NFR-11 decision). fiscal_period_code DOES carry
# a CHECK because the periodic set for 10-K/10-Q is a closed, stable enum (§2.3). period_start
# is in the row identity since schema 9 (lane FA1a): the row identity is the five-tuple plus
# IFNULL(period_start, -1), which the as-of unique index below enforces. The filing_date is the
# knowledge/version axis (it plays research_events.knowledge_at's role).
_DDL_FUNDAMENTAL_FACTS = """
CREATE TABLE IF NOT EXISTS research_fundamental_facts (
    fact_id              INTEGER PRIMARY KEY,                  -- surrogate
    cik                  INTEGER NOT NULL,                     -- the SEC company id (same key research_event_companies.cik uses)
    concept              TEXT    NOT NULL,                     -- internal normalized concept name; a VALUE, not a column (NFR-11) -> NO CHECK
    taxonomy             TEXT    NOT NULL,                     -- the XBRL taxonomy the raw tag came from ('us-gaap' | 'dei')
    xbrl_tag             TEXT    NOT NULL,                     -- the raw XBRL tag (e.g. 'NetIncomeLoss'); records which tag, so the shares fallback is visible
    unit                 TEXT    NOT NULL,                     -- the XBRL units key ('USD' | 'shares' | 'USD/shares')
    fiscal_year          INTEGER NOT NULL,                     -- the XBRL 'fy'
    fiscal_period_code   TEXT    NOT NULL
                                 CHECK (fiscal_period_code IN ('Q1','Q2','Q3','FY')),  -- the XBRL 'fp', closed periodic enum (§2.3)
    period_start         INTEGER,                              -- epoch s UTC, the XBRL 'start'; NULL for instant (stock) concepts (in the key since schema 9, as IFNULL(period_start, -1))
    period_end           INTEGER NOT NULL,                     -- epoch s UTC, the XBRL 'end' (the reporting-period end)
    filing_date          INTEGER NOT NULL,                     -- epoch s UTC, the XBRL 'filed' — THE knowledge/version axis (plays knowledge_at's role)
    value                REAL    NOT NULL,                     -- the XBRL 'val' (float64; all four concepts' magnitudes are << 2^53, exact)
    source_form          TEXT    NOT NULL,                     -- the XBRL 'form' as-seen ('10-K'|'10-Q'|'10-K/A'|'10-Q/A'); recorded-provenance, NO CHECK (the §2.4 filter is the guard)
    accession_number     TEXT,                                 -- the XBRL 'accn' (the filing's accession), for lineage; nullable
    batch_id             INTEGER NOT NULL,                     -- lineage (R6); FK to the fundamentals batch table (§1.4)
    ingested_at          INTEGER NOT NULL,                     -- wall-clock of the row insert (epoch s UTC)
    accepted_at          INTEGER,                              -- epoch s UTC the SEC accepted the filing (submissions feed); NULL only when the filing has none: then known from the day after filing_date (#479 item 10, lane FA1b-1)
    FOREIGN KEY (batch_id) REFERENCES research_fundamental_batches(batch_id)
);

-- The dual-role concatenated index (§1.2): it serves the hot as-of read AND enforces the
-- vintage-keeping uniqueness. The prefix (cik, concept, fiscal_period_code) is the access
-- pattern's fixed key (arch §4); period_end then filing_date are the two time axes, so the five-column
-- prefix still serves the as-of read. Since schema 9 (lane FA1a) period_start is in the key as a sixth
-- column: a quarter and a year-to-date figure from one filing share the five-tuple and differ only by
-- their start, so both must be storable. Every filing is kept, the original and each restatement as
-- separate rows distinguished by filing_date. SQLite treats NULLs as distinct in a unique index, and an
-- instant fact (a balance-sheet stock) has a NULL start, so the key uses IFNULL(period_start, -1): two
-- instants on one five-tuple still conflict. (-1 is no real epoch second the parser can produce.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_fundamental_facts_asof_v9
    ON research_fundamental_facts(cik, concept, fiscal_period_code, period_end, filing_date, IFNULL(period_start, -1));

-- The §10 status coverage-horizon read (MAX(filing_date) over the whole store, FUND-4) cannot use the
-- as-of index above: filing_date is its RIGHTMOST column, so with no leading (cik, concept, ...) prefix
-- bound a bare MAX falls to a full table scan that grows with the store (the DDIA Ch-3 unindexed-MAX
-- trap). This single-column index lets SQLite serve MAX(filing_date) as one B-tree descent. The extra
-- per-insert maintenance is negligible against the network-bound backfill it rides with.
CREATE INDEX IF NOT EXISTS ix_fundamental_facts_filing_date
    ON research_fundamental_facts(filing_date);
"""

# F3 - research_fundamental_quarantines (M3.75 FUND-1 §1.4). Sibling of research_event_quarantines;
# failure_step is fundamentals-specific (fetch/parse/normalize). failure_class carries NO CHECK
# (the parser-dependent growing set, mirroring the event quarantine table). FK to the
# fundamentals batch, so it is created after the batch table.
_DDL_FUNDAMENTAL_QUARANTINES = """
CREATE TABLE IF NOT EXISTS research_fundamental_quarantines (
    quarantine_id     INTEGER PRIMARY KEY,
    batch_id          INTEGER NOT NULL,
    cik               INTEGER,                                            -- the company, when known
    concept           TEXT,                                               -- the concept, when known
    failure_step      TEXT    NOT NULL
                              CHECK (failure_step IN ('fetch','parse','normalize')),
    failure_class     TEXT    NOT NULL,                                   -- NO enum (mirrors research_event_quarantines.failure_class)
    failure_message   TEXT    NOT NULL,
    raw_excerpt       TEXT,                                               -- a bounded excerpt of the offending raw fact, like the event quarantine raw_input
    created_at        INTEGER NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES research_fundamental_batches(batch_id)
);

CREATE INDEX IF NOT EXISTS ix_fundamental_quarantines_batch
    ON research_fundamental_quarantines(batch_id);
"""

# Ordered execution list. FK targets must exist before their FKs are checked
# at INSERT time, but CREATE TABLE itself only validates names (not FK
# targets), so the ordering matters for INSERT-time semantics, not for
# CREATE-time semantics. The list is kept in dependency order anyway for
# readability and to match spec brief §3.9. The three M3.75 fundamentals tables
# (FUND-1 §1) are appended last, with the batch table BEFORE the facts/quarantine
# tables that FK to it (§1.5).
# UNI1 - research_company_profiles: one row per company, keyed by CIK, read once from the SEC submissions
# record (ownerOrg office label as EDGAR shows it, DECISIONS #453; SIC code and description as the industry,
# #477 item 3). A fact stored once in its system of record; members reach it by CIK at read time (DDIA ch. 1,
# "Systems of record and derived data"; ch. 3, "Normalization, denormalization, joins"). Reads are by the key,
# so no other index.
_DDL_COMPANY_PROFILES = """
CREATE TABLE IF NOT EXISTS research_company_profiles (
    cik             INTEGER PRIMARY KEY,
    sector          TEXT,
    sic             TEXT,
    sic_description TEXT,
    read_at         INTEGER NOT NULL
);
"""

# UNI1b - research_company_profile_failures: the newest failed SEC read of a company that has no profile yet.
# One current row per company (a failure is its own fact, DDIA ch. 3 "Normalization, denormalization, joins";
# an append-only attempt log is the other sound shape, left out because no page reads history). The work list of
# companies still to read is derived from membership and these two tables at each pass, never kept as a queue
# (DDIA ch. 1 "Systems of record and derived data"), so a restart loses nothing. Reads are by the key.
_DDL_COMPANY_PROFILE_FAILURES = """
CREATE TABLE IF NOT EXISTS research_company_profile_failures (
    cik       INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('company', 'contact')),
    reason    TEXT NOT NULL,
    failed_at INTEGER NOT NULL
);
"""

# FA3 - research_fundamental_polls and research_fundamental_filings (DECISIONS #480 item 4, #488 item 13).
# DDIA check. Ch. 1 "Systems of record and derived data": a filing's state (known, pending, fetched,
# not_in_facts) is stored once, by accession number, in research_fundamental_filings; the list of companies
# followed is NOT stored, it is derived at each pass from the facts table. Ch. 3 "Event log": one research_fundamental_polls
# row per pass is an append-only record of what the pass saw and did (last, next, failures); the status page reads
# the newest row and never a counter kept elsewhere. Reads are by the key (accession, poll_id) except the pending
# work list, served by a small partial index that holds only the rows still waiting. Both tables are new, so the
# version stays 10 and nothing is migrated: create-if-missing adds them to a file of any age.
_DDL_FUNDAMENTAL_POLLS = """
CREATE TABLE IF NOT EXISTS research_fundamental_polls (
    poll_id          INTEGER PRIMARY KEY,
    started_at       INTEGER NOT NULL,
    finished_at      INTEGER,
    status           TEXT    NOT NULL
                             CHECK (status IN ('running','succeeded','succeeded_with_failures','failed')),
    ciks_checked     INTEGER NOT NULL DEFAULT 0,
    filings_new      INTEGER NOT NULL DEFAULT 0,
    filings_fetched  INTEGER NOT NULL DEFAULT 0,
    refresh_batch_id INTEGER,
    failures         TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(failures)),
    failure_message  TEXT,
    FOREIGN KEY (refresh_batch_id) REFERENCES research_fundamental_batches(batch_id)
);
"""

_DDL_FUNDAMENTAL_FILINGS = """
CREATE TABLE IF NOT EXISTS research_fundamental_filings (
    accession_number TEXT    PRIMARY KEY,
    cik              INTEGER NOT NULL,
    form             TEXT    NOT NULL,
    accepted_at      INTEGER,
    filing_date      INTEGER NOT NULL,
    status           TEXT    NOT NULL
                             CHECK (status IN ('known','pending','fetched','not_in_facts')),
    seen_at          INTEGER NOT NULL,
    settled_at       INTEGER,
    batch_id         INTEGER,
    FOREIGN KEY (batch_id) REFERENCES research_fundamental_batches(batch_id)
);

CREATE INDEX IF NOT EXISTS ix_fundamental_filings_pending
    ON research_fundamental_filings(cik) WHERE status = 'pending';
"""

_DDL_SCRIPTS_ORDERED = (
    _DDL_REQUESTS,
    _DDL_BATCHES,
    _DDL_EVENTS,
    _DDL_EVENTS_HISTORY,
    _DDL_COMPANIES,
    _DDL_QUARANTINES,
    _DDL_CIK_TICKER_MAP,
    _DDL_SCHEMA_VERSION,
    _DDL_UNIVERSE_MEMBERSHIP,
    _DDL_FUNDAMENTAL_BATCHES,       # M3.75 FUND-1: the FK target, before the facts/quarantine children
    _DDL_FUNDAMENTAL_FACTS,         # M3.75 FUND-1: the tall bitemporal fact table + the as-of unique index
    _DDL_FUNDAMENTAL_QUARANTINES,   # M3.75 FUND-1: the sibling quarantine table
    _DDL_COMPANY_PROFILES,          # UNI1: one row per company, sector and industry from the SEC
    _DDL_COMPANY_PROFILE_FAILURES,  # UNI1b: the newest failed read of a company with no profile
    _DDL_FUNDAMENTAL_POLLS,         # FA3: one row per SEC poll pass
    _DDL_FUNDAMENTAL_FILINGS,       # FA3: one row per periodic filing the poll has seen
)


def _split_ddl_statements(script: str) -> list[str]:
    """Split a multi-statement DDL string into individual SQL statements.

    sqlite3's conn.executescript() implicitly commits any open transaction
    before running the script, which would break the events_transaction()
    BEGIN IMMEDIATE / COMMIT wrapper init_events_db() runs inside. So we
    split on semicolons and run each statement via conn.execute().

    Care: a semicolon inside a `--` line comment is not a statement
    terminator. We strip line comments first, then split. SQLite's `/* */`
    block comments are not used in our DDL, so we do not handle them; the
    function would mis-split a script containing both inline `--` comments
    and unmatched block comments (a pathological case we never create here).
    """
    # Drop everything from `--` to end-of-line on each line. A `--` inside a
    # string literal would be miscounted, but our DDL has no `--` inside any
    # string literal (the only string literals are in CHECK enum value lists
    # like 'sec_edgar', 'quarantine', etc., none of which contain `--`).
    no_comments_lines = []
    for line in script.splitlines():
        idx = line.find("--")
        if idx == -1:
            no_comments_lines.append(line)
        else:
            no_comments_lines.append(line[:idx])
    cleaned = "\n".join(no_comments_lines)
    statements = []
    for chunk in cleaned.split(";"):
        stripped = chunk.strip()
        if stripped:
            statements.append(stripped + ";")
    return statements

# Current schema version. The brief A probe asserts the row exists at this value.
# Bumped 1 -> 2 in M3.5 brief A (the research_universe_membership table, EL74).
# Bumped 2 -> 3 by the Benzinga unit (the 'benzinga' source CHECK widening below).
# Bumped 3 -> 4 by M3.75 FUND-1 (the fundamentals fact + batch + quarantine tables).
# Bumped 4 -> 5 by ANA-1 (the nullable name column on research_cik_ticker_map).
# Bumped 5 -> 6 by F7 (the 'news' event_type + the 'alpaca_news' source CHECK widening).
# Bumped 6 -> 7 by Phase-2 Unit 4 (A4a-012, the UNIQUE index on research_cik_ticker_map(cik,ticker,source)).
# Bumped 7 -> 8 by lane EA1 (the 'earnings' event_type CHECK widening).
# Bumped 8 -> 9 by lane FA1a (period_start joins the fundamentals facts unique key; an index swap, no row touched).
# Bumped 9 -> 10 by lane FA1b-1 (a nullable accepted_at column on the fundamentals facts; no row touched).
SCHEMA_VERSION = 10


# --- v2 -> v3 source-CHECK widening migration --------------------------------
# SQLite cannot ALTER a CHECK, so admitting 'benzinga' on research_events.source and
# research_event_batches.source rebuilds the two tables via the sanctioned recipe.
# Runs on its OWN connection BEFORE init's BEGIN IMMEDIATE block: PRAGMA foreign_keys
# is a no-op inside an open transaction, and the DROP/RENAME must run with FK
# enforcement OFF (research_events has FK children research_events_history /
# research_event_companies; research_event_batches has four). Gated on sqlite_master
# so it is a no-op on a fresh DB (init's widened DDL creates the tables correct) and
# after the first successful run.
_OLD_SOURCE_CHECK = "CHECK (source IN ('sec_edgar','canonical_loader'))"
_NEW_SOURCE_CHECK = "CHECK (source IN ('sec_edgar','canonical_loader','benzinga'))"


def _rebuild_table_widening_source(conn: sqlite3.Connection, table: str) -> None:
    """Rebuild one table with the widened source CHECK, preserving rows and schema.

    Introspects the live CREATE sql from sqlite_master and rewrites only (a) the
    leading CREATE clause to a `<table>_new` shadow and (b) the source CHECK — never
    an FK reference, so a self-FK (research_event_batches.parent_batch_id) keeps
    pointing at the final table name after the rename. Copies rows with an explicit
    column list (robust vs SELECT *). The caller has FK enforcement OFF and owns the
    transaction; indexes are recreated by the caller after the rename.

    Idempotent / supersede-safe: returns a no-op when the v3 OLD check is absent —
    either this table is already at the v3-widened form, or a LATER migration (v6,
    which widened the same source CHECK to add 'alpaca_news') already rewrote it. The
    v6 rebuild ALTER-RENAMEs the table, so SQLite then stores the name QUOTED; accept
    BOTH the quoted and the plain CREATE prefix when a rebuild IS needed (mirrors the
    v6 helper fix, d0f042e). Without this, the first reboot after a v6 deploy re-runs
    v3 against the quoted, already-superseded schema and the guard fires wrongly.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    create_sql = row["sql"]
    if _NEW_SOURCE_CHECK in create_sql:
        return                                              # already at the v3-widened form
    if _OLD_SOURCE_CHECK not in create_sql:
        return                                              # already widened past v3 (v6 superseded it) — nothing to do
    # The OLD check is present, so v3 must widen it. SQLite stores the name quoted once a table has been
    # ALTER-RENAMEd and unquoted for a freshly CREATEd one — accept BOTH (the v6 helper does the same).
    quoted_prefix = f'CREATE TABLE "{table}"'
    plain_prefix = f"CREATE TABLE {table}"
    if quoted_prefix in create_sql:
        create_prefix = quoted_prefix
    elif plain_prefix in create_sql:
        create_prefix = plain_prefix
    else:
        raise RuntimeError(
            f"v3 migration: {table} carries the old source CHECK but no recognizable CREATE TABLE "
            f"prefix (neither {quoted_prefix!r} nor {plain_prefix!r}); refusing to rebuild blindly"
        )
    new_create = (
        create_sql
        .replace(create_prefix, f"CREATE TABLE {table}_new", 1)
        .replace(_OLD_SOURCE_CHECK, _NEW_SOURCE_CHECK)
    )
    cols = ", ".join(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")       # clean any partial prior run
    conn.execute(new_create)
    conn.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


def _migrate_source_check_v3(events_db_path: Union[str, Path]) -> None:
    """Apply the v2->v3 source-CHECK widening on an existing DB. No-op on a fresh DB."""
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_events'"
        ).fetchone()
        if existing is None:
            return                                          # fresh DB: init's widened DDL is correct
        if _NEW_SOURCE_CHECK in (existing["sql"] or ""):
            return                                          # already migrated
        if _OLD_SOURCE_CHECK not in (existing["sql"] or ""):
            return                                          # already widened past v3 (v6 superseded the source CHECK)
        conn.execute("PRAGMA foreign_keys = OFF;")          # valid: no pending transaction
        conn.execute("BEGIN;")
        try:
            # Rebuild the FK target (batches) first, then research_events.
            _rebuild_table_widening_source(conn, "research_event_batches")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_batches_source_started "
                         "ON research_event_batches(source, started_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_batches_running_heartbeat "
                         "ON research_event_batches(last_heartbeat_at) WHERE status = 'running';")
            _rebuild_table_widening_source(conn, "research_events")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_events_natural_active "
                         "ON research_events(event_type, natural_key) WHERE status = 'active';")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_type_knowledge "
                         "ON research_events(event_type, knowledge_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_batch "
                         "ON research_events(batch_id);")
            violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            if violations:
                raise RuntimeError(
                    f"v3 migration: foreign_key_check found {len(violations)} "
                    f"violation(s) after the rebuild; rolling back"
                )
            conn.execute("COMMIT;")
        except (sqlite3.DatabaseError, RuntimeError, OSError):
            # The rebuild's realistic failure surface: any SQLite error, the explicit
            # RuntimeError raises (unexpected schema / FK-check violation), or an OS error.
            # KeyboardInterrupt / SystemExit propagate WITHOUT a ROLLBACK here, but the outer
            # finally closes the connection, which rolls back the open transaction anyway.
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")       # restore enforcement
    finally:
        conn.close()


def _migrate_add_name_column_v5(events_db_path: Union[str, Path]) -> None:
    """Add the nullable name column to research_cik_ticker_map on an existing DB.

    The v4 fundamentals tables landed via CREATE TABLE IF NOT EXISTS because they
    were NEW tables; a column on an EXISTING table cannot land that way (the IF
    NOT EXISTS no-ops the whole table). So this guarded ALTER converges a fresh
    init (the column comes from the DDL) and an upgraded prod DB (the column comes
    from here), both stamped v5. Unlike the v3 CHECK-widening, an ALTER ADD COLUMN
    is transaction-safe and needs no FK-off table rebuild. Idempotent: a no-op on
    a fresh DB (the table does not exist yet) and after the first run (the column
    is already present).
    """
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_cik_ticker_map'"
        ).fetchone()
        if existing is None:
            return                                          # fresh DB: init's DDL creates the column
        columns = conn.execute(
            "PRAGMA table_info(research_cik_ticker_map)"
        ).fetchall()
        if any(col["name"] == "name" for col in columns):
            return                                          # already migrated (or fresh-DDL created it)
        conn.execute("ALTER TABLE research_cik_ticker_map ADD COLUMN name TEXT")
    finally:
        conn.close()


# --- v5 -> v6 news-type + alpaca_news-source CHECK widening migration (F7) ----
# F7 admits a new event_type ('news') and a new source ('alpaca_news'). SQLite cannot
# ALTER a CHECK, so the two affected tables are rebuilt via the sanctioned recipe (the
# v3 precedent). Unlike v3 (which widened ONE clause — source — per table), v6 widens
# TWO clauses on research_events (event_type AND source), so it uses a GENERALIZED
# rewrite helper taking a list of (old_clause, new_clause) pairs (the single-clause
# _rebuild_table_widening_source cannot do two). Runs on its OWN connection BEFORE
# init's BEGIN IMMEDIATE block (PRAGMA foreign_keys is a no-op inside a transaction),
# with FK enforcement OFF for the DROP/RENAME. Gated on sqlite_master so it is a no-op
# on a fresh DB (init's widened DDL is correct) and after the first successful run.
_V6_EVENTS_OLD_TYPE_CHECK = "CHECK (event_type IN ('sec_13d','analyst_rating'))"
_V6_EVENTS_NEW_TYPE_CHECK = "CHECK (event_type IN ('sec_13d','analyst_rating','news'))"
_V6_OLD_SOURCE_CHECK = "CHECK (source IN ('sec_edgar','canonical_loader','benzinga'))"
_V6_NEW_SOURCE_CHECK = "CHECK (source IN ('sec_edgar','canonical_loader','benzinga','alpaca_news'))"


def _rebuild_table_widening_checks(conn: sqlite3.Connection, table: str,
                                   replacements: list[tuple[str, str]]) -> None:
    """Rebuild one table applying a list of (old_clause, new_clause) CHECK rewrites.

    Generalizes _rebuild_table_widening_source (a single hardcoded source-clause swap)
    to N clauses. Introspects the live CREATE sql from sqlite_master, rewrites the leading
    CREATE clause to a <table>_new shadow plus each (old -> new) pair (each via one bounded
    .replace(..., 1)), copies rows with an explicit column list (robust vs SELECT *), then
    DROP + RENAME. Asserts the prefix and every old_clause are present before rewriting
    (refuse to rebuild blindly). The caller owns the transaction + has FK enforcement OFF;
    indexes are recreated by the caller after the rename.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    create_sql = row["sql"]                                    # the live CREATE as SQLite stored it
    # SQLite stores the table name QUOTED ("research_events") once a table has been ALTER-RENAMEd —
    # which the v3 source-widening rebuild did to both tables on prod — and UNQUOTED for a table that
    # was only ever CREATEd. Accept BOTH forms so v6 widens the table whether or not v3 rebuilt it
    # (the prod tables carry the quoted form; a fresh DB carries the unquoted form).
    quoted_prefix = f'CREATE TABLE "{table}"'
    plain_prefix = f"CREATE TABLE {table}"
    if quoted_prefix in create_sql:
        create_prefix = quoted_prefix
    elif plain_prefix in create_sql:
        create_prefix = plain_prefix
    else:
        raise RuntimeError(
            f"v6 migration: {table} CREATE sql lacks an expected CREATE TABLE prefix "
            f"(neither {quoted_prefix!r} nor {plain_prefix!r}); live schema changed — "
            f"refusing to rebuild blindly"
        )
    new_create = create_sql.replace(create_prefix, f"CREATE TABLE {table}_new", 1)
    for old_clause, new_clause in replacements:               # apply each CHECK widening
        if old_clause not in new_create:
            raise RuntimeError(
                f"v6 migration: {table} CREATE sql lacks the expected clause "
                f"{old_clause!r}; live schema changed — refusing to rebuild blindly"
            )
        new_create = new_create.replace(old_clause, new_clause, 1)
    cols = ", ".join(r["name"] for r in conn.execute(f"PRAGMA table_info({table})"))
    conn.execute(f"DROP TABLE IF EXISTS {table}_new")          # clean any partial prior run
    conn.execute(new_create)
    conn.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


def _migrate_news_type_v6(events_db_path: Union[str, Path]) -> None:
    """Apply the v5->v6 news event_type + alpaca_news source CHECK widening. No-op on a fresh DB."""
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_events'"
        ).fetchone()
        if existing is None:
            return                                             # fresh DB: init's widened DDL is correct
        if _V6_EVENTS_OLD_TYPE_CHECK not in (existing["sql"] or ""):
            return                                             # already migrated, or superseded by v8 (its new check is no longer a substring)
        conn.execute("PRAGMA foreign_keys = OFF;")             # valid: no pending transaction
        conn.execute("BEGIN;")
        try:
            # Rebuild the FK target (batches) first — widen its source CHECK — then research_events.
            _rebuild_table_widening_checks(
                conn, "research_event_batches",
                [(_V6_OLD_SOURCE_CHECK, _V6_NEW_SOURCE_CHECK)])
            conn.execute("CREATE INDEX IF NOT EXISTS ix_batches_source_started "
                         "ON research_event_batches(source, started_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_batches_running_heartbeat "
                         "ON research_event_batches(last_heartbeat_at) WHERE status = 'running';")
            # research_events: widen BOTH the event_type CHECK and the source CHECK.
            _rebuild_table_widening_checks(
                conn, "research_events",
                [(_V6_EVENTS_OLD_TYPE_CHECK, _V6_EVENTS_NEW_TYPE_CHECK),
                 (_V6_OLD_SOURCE_CHECK, _V6_NEW_SOURCE_CHECK)])
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_events_natural_active "
                         "ON research_events(event_type, natural_key) WHERE status = 'active';")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_type_knowledge "
                         "ON research_events(event_type, knowledge_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_batch "
                         "ON research_events(batch_id);")
            violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            if violations:
                raise RuntimeError(
                    f"v6 migration: foreign_key_check found {len(violations)} "
                    f"violation(s) after the rebuild; rolling back"
                )
            conn.execute("COMMIT;")
        except (sqlite3.DatabaseError, RuntimeError, OSError):
            # KeyboardInterrupt / SystemExit propagate WITHOUT a ROLLBACK here, but the
            # outer finally closes the connection, which rolls back the open transaction.
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")          # restore enforcement
    finally:
        conn.close()


# --- v6 -> v7 UNIQUE-index migration (Phase-2 Unit 4, A4a-012) ----------------
# Add the UNIQUE index on research_cik_ticker_map(cik, ticker, source). Unlike v3/v5/v6 this
# toggles NO foreign_keys pragma and rebuilds NO table (it is a DELETE + CREATE INDEX), so it does
# not need their pre-block FK-off slot for its own sake — but it MUST still run before the main DDL
# block, because that block's `CREATE UNIQUE INDEX IF NOT EXISTS uq_cik_ticker_source` would FAIL on
# an existing DB that still carries duplicate (cik, ticker, source) rows. The dedupe + the index
# creation happen in ONE transaction here so a duplicate-bearing DB can never reach the create with
# duplicates still present. No-op on a fresh DB (the table does not exist yet — the main DDL creates
# table + index cleanly) and after the first run (the index already exists).
def _migrate_unique_cik_ticker_v7(events_db_path: Union[str, Path]) -> None:
    """Dedupe research_cik_ticker_map over (cik, ticker, source) then add the UNIQUE index. No-op on a fresh DB."""
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Enable FK enforcement on this raw connection so the PRAGMA foreign_key_check below is real
    # (it returns nothing when foreign_keys is OFF, the sqlite3.connect default). No FK currently
    # references research_cik_ticker_map, so the check passes today; enabling it keeps the guard
    # honest if a future FK is added. Valid here only because no transaction is open yet.
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        index_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_cik_ticker_source'"
        ).fetchone()
        if index_row is not None:
            return                                             # already migrated (the unique index exists)
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_cik_ticker_map'"
        ).fetchone()
        if table_row is None:
            return                                             # fresh DB: the main DDL block creates table + index
        conn.execute("BEGIN;")
        try:
            # Dedupe: keep the lowest map_id per (cik, ticker, source), delete the rest. Expected to
            # delete ZERO rows on prod (the INSERT ... WHERE NOT EXISTS guard has always run); the
            # delete is defensive so the index creation below cannot fail on a stray duplicate. No
            # table FK-references research_cik_ticker_map.map_id, so deleting higher-id dups is safe.
            conn.execute(
                """DELETE FROM research_cik_ticker_map
                    WHERE map_id NOT IN (
                        SELECT MIN(map_id) FROM research_cik_ticker_map
                        GROUP BY cik, ticker, source
                    )"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_cik_ticker_source "
                "ON research_cik_ticker_map(cik, ticker, source);"
            )
            violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            if violations:
                raise RuntimeError(
                    f"v7 migration: foreign_key_check found {len(violations)} "
                    f"violation(s) after the dedupe; rolling back"
                )
            conn.execute("COMMIT;")
        except (sqlite3.DatabaseError, RuntimeError, OSError):
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


# --- v7 -> v8 earnings-type CHECK widening (lane EA1) -------------------------
# EA1 admits a new event_type ('earnings'). SQLite cannot ALTER a CHECK, so research_events is rebuilt
# with the same generalized helper v6 uses (it accepts both the plain and the quoted CREATE TABLE form,
# the quoted form being what a renamed table carries on prod). Own connection, FK enforcement OFF for
# the DROP/RENAME, before init's BEGIN block. A no-op on a fresh file (the DDL already lists earnings)
# and after the first run. earnings is admissible but not backtest-grade (BACKTEST_GRADE_EVENT_TYPES
# is unchanged; lane EA2 grades it).
_V8_EVENTS_OLD_TYPE_CHECK = "CHECK (event_type IN ('sec_13d','analyst_rating','news'))"
_V8_EVENTS_NEW_TYPE_CHECK = "CHECK (event_type IN ('sec_13d','analyst_rating','news','earnings'))"


def _migrate_earnings_type_v8(events_db_path: Union[str, Path]) -> None:
    """Apply the v7->v8 earnings event_type CHECK widening. No-op on a fresh DB and after the first run."""
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_events'"
        ).fetchone()
        if existing is None:
            return                                             # fresh DB: init's widened DDL is correct
        if _V8_EVENTS_NEW_TYPE_CHECK in (existing["sql"] or ""):
            return                                             # already migrated
        conn.execute("PRAGMA foreign_keys = OFF;")             # valid: no pending transaction
        conn.execute("BEGIN;")
        try:
            _rebuild_table_widening_checks(
                conn, "research_events",
                [(_V8_EVENTS_OLD_TYPE_CHECK, _V8_EVENTS_NEW_TYPE_CHECK)])
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_events_natural_active "
                         "ON research_events(event_type, natural_key) WHERE status = 'active';")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_type_knowledge "
                         "ON research_events(event_type, knowledge_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_batch "
                         "ON research_events(batch_id);")
            violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            if violations:
                raise RuntimeError(
                    f"v8 migration: foreign_key_check found {len(violations)} "
                    f"violation(s) after the rebuild; rolling back"
                )
            conn.execute("COMMIT;")
        except (sqlite3.DatabaseError, RuntimeError, OSError):
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON;")          # restore enforcement
    finally:
        conn.close()


# --- v8 -> v9 fundamentals key widening --------------------------------------
# The facts unique index gains period_start (IFNULL'd, because SQLite treats NULLs as distinct). The old
# key was coarser, so every stored row already meets the new one and the CREATE cannot fail on existing
# data; an index is derived from the rows, so swapping it touches none of them.
_V9_OLD_FACT_INDEX = "uq_fundamental_facts_asof"
_V9_NEW_FACT_INDEX_DDL = (
    "CREATE UNIQUE INDEX uq_fundamental_facts_asof_v9 ON research_fundamental_facts("
    "cik, concept, fiscal_period_code, period_end, filing_date, IFNULL(period_start, -1))"
)


def _migrate_fact_key_v9(events_db_path: Union[str, Path]) -> None:
    """Apply the v8->v9 index swap. No-op when there is no facts table, and after the first run."""
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_fundamental_facts'"
        ).fetchone()
        if has_table is None:
            return                                             # no facts table: init's DDL builds the v9 index
        has_old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (_V9_OLD_FACT_INDEX,)
        ).fetchone()
        if has_old is None:
            return                                             # already migrated, or a fresh v9 file
        conn.execute("BEGIN;")
        try:
            conn.execute(f"DROP INDEX {_V9_OLD_FACT_INDEX}")
            conn.execute(_V9_NEW_FACT_INDEX_DDL)
            conn.execute("COMMIT;")
        except sqlite3.DatabaseError:
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


def _migrate_fact_accepted_at_v10(events_db_path: Union[str, Path]) -> None:
    """Add the nullable accepted_at column to research_fundamental_facts (v9 -> v10, lane FA1b-1).

    A guarded ALTER (the _migrate_add_name_column_v5 pattern): the guard reads PRAGMA table_info, never the
    CREATE text. No-op when there is no facts table (init's DDL builds the column) and after the first run.
    Existing rows keep every old column and get NULL.
    """
    conn = sqlite3.connect(str(events_db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_fundamental_facts'"
        ).fetchone()
        if has_table is None:
            return
        columns = conn.execute("PRAGMA table_info(research_fundamental_facts)").fetchall()
        if any(col["name"] == "accepted_at" for col in columns):
            return
        conn.execute("ALTER TABLE research_fundamental_facts ADD COLUMN accepted_at INTEGER")
    finally:
        conn.close()


def init_events_db(events_db_path: Union[str, Path, None] = None) -> None:
    """Create all thirteen events.db tables, indexes, and the schema-version row. Idempotent.

    Run order matches spec brief §3.9 events.db migrations E1-E11 for the eight
    M2.5 tables, plus the M3.5 brief A ninth table (research_universe_membership,
    spec brief §3.1/§3.4), plus the three M3.75 FUND-1 fundamentals tables
    (research_fundamental_batches / _facts / _quarantines), plus the UNI1 company-profiles table,
    all appended to _DDL_SCRIPTS_ORDERED:
      E1.  per-connection pragmas (WAL + FK) — set in _connect_events
      E2-E9. CREATE TABLE IF NOT EXISTS for each of the eight M2.5 tables, then
             the ninth M3.5 universe-membership table
      E10. CREATE INDEX IF NOT EXISTS alongside each table's DDL
      E11. INSERT into research_events_schema_version WHERE NOT EXISTS

    Fails fast if the runtime SQLite is below 3.44 (§8.5 close-hash SQL
    relies on in-aggregate ORDER BY inside json_group_array, available
    only on 3.44+).
    """
    _assert_sqlite_version()
    path = events_db_path if events_db_path is not None else DB_PATH
    # v2 -> v3 source-CHECK widening MUST run before the BEGIN IMMEDIATE block below
    # (PRAGMA foreign_keys is a no-op inside a transaction). No-op on a fresh DB.
    _migrate_source_check_v3(path)
    # v4 -> v5: add the nullable name column to research_cik_ticker_map (ANA-1). A
    # guarded ALTER, not a CREATE TABLE IF NOT EXISTS (that no-ops on the existing
    # table). Runs before the BEGIN block, same as the v3 migration. No-op on a fresh DB.
    _migrate_add_name_column_v5(path)
    # v5 -> v6: widen the research_events event_type CHECK ('news') + the source CHECK
    # ('alpaca_news') on research_events and research_event_batches (F7). A table rebuild
    # (CHECK is un-ALTERable), so it runs before the BEGIN block with FK off, same as v3.
    # No-op on a fresh DB (init's widened DDL is correct) and after the first run.
    _migrate_news_type_v6(path)
    # v6 -> v7: dedupe research_cik_ticker_map over (cik, ticker, source) then add the UNIQUE index
    # (A4a-012). MUST run before the BEGIN block: the main DDL's CREATE UNIQUE INDEX would fail on a
    # duplicate-bearing existing DB. No-op on a fresh DB and after the first run.
    _migrate_unique_cik_ticker_v7(path)
    # v7 -> v8: widen the research_events event_type CHECK ('earnings', lane EA1). A table rebuild, so it
    # runs before the BEGIN block with FK off. No-op on a fresh DB and after the first run.
    _migrate_earnings_type_v8(path)
    # v8 -> v9: swap the fundamentals facts unique index for the one with period_start in the key (lane
    # FA1a). An index drop and create only: no table rebuild, no row touched. No-op on a fresh DB and
    # after the first run.
    _migrate_fact_key_v9(path)
    # v9 -> v10: add the nullable accepted_at column to the fundamentals facts (lane FA1b-1). A guarded
    # ALTER ADD COLUMN: no table rebuild, no row touched. No-op on a fresh DB and after the first run.
    _migrate_fact_accepted_at_v10(path)
    # All DDL runs inside one transaction. CREATE ... IF NOT EXISTS makes
    # every step idempotent so a partial run (process killed mid-init) can
    # be retried cleanly on the next dashboard restart. We deliberately use
    # per-statement conn.execute() instead of conn.executescript(), because
    # executescript() autocommits any pending transaction before running,
    # which would break the BEGIN IMMEDIATE / COMMIT wrapper.
    with events_transaction(path) as conn:
        for ddl in _DDL_SCRIPTS_ORDERED:
            for statement in _split_ddl_statements(ddl):
                conn.execute(statement)
        # E11: stamp the version row exactly once. WHERE NOT EXISTS guards a
        # re-run from inserting a duplicate (the PK would reject it anyway,
        # but the explicit guard is the idempotent pattern researchdb uses).
        conn.execute(
            """INSERT INTO research_events_schema_version (version, applied_at, note)
               SELECT ?, ?, ?
               WHERE NOT EXISTS (SELECT 1 FROM research_events_schema_version WHERE version = ?)""",
            (SCHEMA_VERSION, int(time.time()),
             "filing acceptance time (lane FA1b-1)", SCHEMA_VERSION),
        )
