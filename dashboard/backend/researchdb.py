"""Research lab DB access. SQLite WAL mode.

Three connection rules:
  1. foreign_keys is per-connection in SQLite — must set on every new conn
  2. journal_mode=WAL is persistent at the DB file level — set once is enough
     but re-running on each connect is a cheap no-op
  3. isolation_level=None means we manage transactions explicitly via
     BEGIN/COMMIT/ROLLBACK instead of letting the driver wrap things implicitly

Timestamps are unix seconds UTC. Frontend formats for display.
"""
import ast
import hashlib  # M3.5 brief I: event-run data-declaration fingerprint (dataset_sha256); night four lane S: theory file sha256
import json
import logging  # M5 U0: structured warning if the eodhd ticker manifest is absent at boot
import math  # lane WF (6.2b): math.isfinite guards a walk-forward profit figure before it is stored
import os  # Lane RM (set 16b, plan amendment 7): reads RESEARCH_DATA_ROOT to resolve a member dataset's own file, mirroring routes/research.py's own precedent
import re  # night-six lane C: matches a provenance entry's '### Group' body against the A-F tier letters
import sqlite3
import time
import tomllib  # UI17b-0: the ranges file and a theory's TOML text are parsed, never executed
import uuid as _uuid  # K.6 share secret (UUID4)
from collections import defaultdict  # night-seven lane G, DECISIONS #316: the refusal counters need a key for any letter, not only the four the old roster named
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone  # night four lane S: ISO-8601 UTC timestamps for research_theories/research_verdict_rejections (section 10.1's own column types); timedelta added by lane UB round 2 (UB-R2) for the inclusive-window-end to exclusive-fetch-end conversion below
from pathlib import Path  # M5 U0: resolve the committed eodhd_tickers.txt relative to this package

from . import artifact_store  # lane JC: the replay re-check's own artifact-existence read (put_full_metrics_cache_guarded)
from . import catalog  # lane JC round 2, R2: _ensure_strategies_on_path, so get_full_metrics_cache_by_cell can resolve the CURRENT formula_version (catalog.py imports only config + stdlib, so this is cycle-safe)
from . import dataset_sources  # W8: per-(source,timeframe) floor + latest-end, single source of truth
from .events import company_profiles, tickers as event_tickers  # DL1: normalize_ticker, the SEC form of a ticker for the member-detail name lookup (tickers.py imports nothing)
from . import theory_strategy  # UI17b-0: NAME_PREFIX only (standard library only, cycle-safe)
from . import reservedb  # SL1: the reserve cut a search's optimise_window may not reach (reservedb imports judgedb, provenance_parse and config only, so this is cycle-safe)
from . import eventsdb  # F7: BACKTEST_GRADE_EVENT_TYPES for the write-time event_types vocabulary check (cycle-safe: eventsdb does not import researchdb)
from .config import RESEARCH_DB_PATH, PARAMETER_PROVENANCE_PATH, RANGES_PATH, PROJECT_ROOT
# OPS-1: the provenance-markdown parser now lives in its own module (provenance_parse.py) so the
# operations page can reuse it without importing this whole database module. Imported BY NAME into
# THIS module's namespace on purpose, not `from . import provenance_parse` followed by qualified
# calls: eleven probe files across the repo reach these three names as attributes on `researchdb`
# itself (e.g. `researchdb._parse_provenance_headings`), and only a name-import leaves that
# attribute in place. `from . import provenance_parse` would leave no such attribute on
# `researchdb` and break every one of those probes with an AttributeError at their next run. Do not
# "tidy" this to the module form.
from .provenance_parse import (
    _REQUIRED_PROVENANCE_SECTIONS,
    _parse_provenance_headings,
    _parse_provenance_bodies,
)

log = logging.getLogger(__name__)  # M5 U0: module logger (the boot-loader warning)

# DB location is centralized in config.py (single source of truth, env-overridable)
DB_PATH = RESEARCH_DB_PATH
# Provenance file location, also env-overridable (module global so a probe can monkeypatch it)
PROVENANCE_PATH = PARAMETER_PROVENANCE_PATH
# Night four lane S (job 2.4, DECISIONS #377 ruling 2): the one committed theory file directory,
# imported once per content sha at init_db time. A module global (not hardcoded inline) so a probe
# can monkeypatch it, the same reason PROVENANCE_PATH above is a module global rather than an inline
# literal.
THEORIES_DIR = PROJECT_ROOT / "docs" / "research_lab" / "theories"
# OPS-1 boot fact (docs/ops/ops_monitor_registry.md -> boot_provenance_check_ran): the unix-seconds
# time _check_parameter_provenance() last returned WITHOUT raising, in THIS process. None means it
# has not completed successfully since this process started, which is exactly the state a dropped
# call from init_db, or an import rebound to stop running the check, would leave forever -- read
# through provenance_check_boot_fact() below, never this variable directly.
_provenance_check_passed_at: int | None = None


# ---------------------------------------------------------------------------
# The dataset-type registry (H4, pre-M4 Brief A1).
# ---------------------------------------------------------------------------
# Owner of which dataset types exist and each type's storage kind. Code-side
# only: deliberately NO database CHECK on research_datasets.dataset_type, so a new
# dataset type is a one-line edit here, never a migration (arch §2; the §8 additive
# catalog). Mirrors the shipped event-type registry (DECISIONS #70) with two
# deliberate divergences: no DB CHECK, and a per-type storage-kind attribute.
# Homed in this pyarrow-free datasets-schema owner so no read path is forced to
# pull in the parquet stack (the same rationale that put EVENT_TYPES in eventsdb.py).

DATASET_STORAGE_FILE = "file"   # carries a parquet file_path/sha256/file_bytes when ready
DATASET_STORAGE_ROW = "row"     # data is rows + metadata; file_path is NULL even when valid

DATASET_TYPE_PRICE = "price_dataset"        # a fetched OHLC price series (file-backed)
DATASET_TYPE_UNIVERSE = "universe"          # a today's-list constituent set (row-backed)
DATASET_TYPE_EVENT_SERIES = "event_series"  # a catalog row over an event store (row-backed)
DATASET_TYPE_FUNDAMENTALS = "fundamentals"  # M3.75: the one logical XBRL fundamentals store (row-backed)
DATASET_TYPE_PRICE_UNIVERSE = "price_universe"  # M5 U1 (AD-M5-11, R-UNIV): a ticker-keyed price universe (row-backed; the membership IS the data)

# research_jobs.kind values for the run-bearing job kinds (M5 UU scale-defect 1.3). The job kind — NOT the
# dataset_type — is the run's own nature: a SWEEP is the only kind that gets 'run_sweep'; BOTH a price run
# and an EVENT run get 'run_backtest' (event-vs-price is then the payload's event shape, see _run_nature).
# The B2 bridge let an event run sit on a price_universe dataset, so dataset_type stopped discriminating —
# these are the authoritative discriminators the dispatch INSERTs write (dispatch_run / dispatch_event_run /
# dispatch_sweep) and the run-nature classifier reads.
JOB_KIND_RUN_BACKTEST = "run_backtest"  # a single price run OR an event run (distinguished by the payload)
JOB_KIND_RUN_SWEEP = "run_sweep"        # a universe sweep (parameter grid or zero-param single_combo)
JOB_KIND_RUN_THEORY_SEARCH = "run_theory_search"  # GV2-7b: the generator v2 search of a theory over its stock group (stage screen)
JOB_KIND_RUN_THEORY_STAGE = "run_theory_stage"    # GV2-7b: a generator v2 optimise or walk-forward stage
JOB_KIND_RUN_FAMILY_UNION = "run_family_union"    # GV3-FU2: the worker merges several theories' trial families on one key (call 37)
THEORY_JOB_KINDS = (JOB_KIND_RUN_THEORY_SEARCH, JOB_KIND_RUN_THEORY_STAGE)  # both are sweeps by their nature (run_nature)

# The owner: dataset_type -> storage kind. THE single authority both for which types
# exist and for which side of the restore branch each falls on (arch §2/§5).
DATASET_TYPE_REGISTRY: dict[str, str] = {
    DATASET_TYPE_PRICE: DATASET_STORAGE_FILE,        # price datasets carry a real parquet file
    DATASET_TYPE_UNIVERSE: DATASET_STORAGE_ROW,      # universes are membership rows, no file
    DATASET_TYPE_EVENT_SERIES: DATASET_STORAGE_ROW,  # event-series are catalog rows, no file
    DATASET_TYPE_FUNDAMENTALS: DATASET_STORAGE_ROW,  # M3.75: a queryable bitemporal table, not a parquet file
    DATASET_TYPE_PRICE_UNIVERSE: DATASET_STORAGE_ROW,  # M5 U1: a price universe is membership rows, no file
}


class UnknownDatasetTypeError(ValueError):
    """A catalog write named a dataset_type not in DATASET_TYPE_REGISTRY."""


def dataset_storage_kind(dataset_type: str) -> str:
    """Return the storage kind for a known dataset_type, or raise UnknownDatasetTypeError.

    This is the runtime write gate: a writer that names an unregistered type fails here,
    at the point of writing, instead of inserting a silent typo (H4). Mirrors the
    dict-lookup-raises-typed-error shape of events/types.get_spec.
    """
    try:
        return DATASET_TYPE_REGISTRY[dataset_type]              # the happy path: a registered type
    except KeyError:                                           # an unregistered type is a typo/bug
        raise UnknownDatasetTypeError(                        # surface it loudly at the write site
            f"unknown dataset_type {dataset_type!r}; registered: {sorted(DATASET_TYPE_REGISTRY)}"
        )


class DatasetTypeRegistryDriftError(RuntimeError):
    """The dataset-type registry's invariants are violated at boot."""


def assert_dataset_type_registry_consistent() -> None:
    """Fail loudly at startup if the dataset-type registry's invariants are violated.

    Raises (never `assert`, which is stripped under `python -O` — mirroring
    output_schemas.assert_event_type_registry_consistent) a DatasetTypeRegistryDriftError
    if a named writer constant is not a registry key, or a registered storage kind is not
    one of the two valid kinds. Called explicitly in main.py's lifespan startup — a
    deliberate improvement over the event-type guard's import-side-effect home, which only
    fires if something happens to import its module at boot. Single-owner here means there
    is no second copy to drift (the event-type set is hand-listed in five-plus places).
    """
    # 1. every named writer constant is a registry key (no constant the writers use is unregistered)
    named = {DATASET_TYPE_PRICE, DATASET_TYPE_UNIVERSE, DATASET_TYPE_EVENT_SERIES,
             DATASET_TYPE_FUNDAMENTALS, DATASET_TYPE_PRICE_UNIVERSE}  # what writers bind (M3.75 added fundamentals; M5 U1 added price_universe)
    keys = set(DATASET_TYPE_REGISTRY)                                               # what the owner knows
    if named != keys:                                                              # any mismatch is drift
        raise DatasetTypeRegistryDriftError(
            f"dataset-type constants {sorted(named)} != registry keys {sorted(keys)}"
        )
    # 2. every storage kind is one of the two valid kinds (a typo'd kind breaks the restore branch)
    bad = {t: k for t, k in DATASET_TYPE_REGISTRY.items()
           if k not in (DATASET_STORAGE_FILE, DATASET_STORAGE_ROW)}                 # collect any invalid kind
    if bad:                                                                         # nothing bad on a clean registry
        raise DatasetTypeRegistryDriftError(f"invalid storage kinds: {bad}")


def _connect() -> sqlite3.Connection:
    """Open a connection with per-connection pragmas applied."""
    # autocommit mode; we own the transactions
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    # foreign keys: per-connection, cascade silently no-ops without this
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL: persistent, cheap to re-run
    conn.execute("PRAGMA journal_mode = WAL;")
    # row_factory gives dict-like rows so we can write row["column"]
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    """For read-only queries or single-statement writes."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    """For multi-statement writes. Commits on clean exit, rolls back on exception."""
    conn = _connect()
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


# All schema lives here. Idempotent — re-running init_db() is safe.
_DDL = """
CREATE TABLE IF NOT EXISTS research_jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL,
    payload_json    TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL,
    claimed_at      INTEGER,
    finished_at     INTEGER,
    worker_id       TEXT,
    last_heartbeat  INTEGER,
    error           TEXT,
    result_json     TEXT,
    progress_json   TEXT             -- M5 1A (R-QUEUE/AD-M5-5): sweep progress {"done":N,"total":M,"eta_s":...};
                                     -- the worker (1B/1C) writes it as it chunks; NULL for every non-sweep + pre-M5 job
);
CREATE INDEX IF NOT EXISTS ix_jobs_status_created
    ON research_jobs(status, created_at);

-- M5 Track C: a PARTIAL index over only the ACTIVE/FAILED compute-metrics jobs, so the full-metrics route's
-- in-flight/failed dedup reads (latest_full_metrics_job / enqueue_full_metrics_job) stay O(active jobs), never
-- O(all compute jobs ever). The WHERE clause keeps the index tiny (succeeded jobs are excluded).
CREATE INDEX IF NOT EXISTS ix_jobs_compute_active
    ON research_jobs(kind, id DESC) WHERE status IN ('pending', 'picked_up', 'failed');

CREATE TABLE IF NOT EXISTS research_job_events (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    event        TEXT    NOT NULL,
    payload_json TEXT,
    FOREIGN KEY (job_id) REFERENCES research_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_events_job_ts
    ON research_job_events(job_id, ts);

CREATE TABLE IF NOT EXISTS research_job_logs (
    id     INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    ts     INTEGER NOT NULL,
    level  TEXT    NOT NULL DEFAULT 'info',
    line   TEXT    NOT NULL,
    FOREIGN KEY (job_id) REFERENCES research_jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_logs_job_id
    ON research_job_logs(job_id, id);

CREATE TABLE IF NOT EXISTS research_workers (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    last_seen     INTEGER NOT NULL,
    online_since  INTEGER
);

-- ============================================================
-- research_datasets  (M2)
-- ============================================================
CREATE TABLE IF NOT EXISTS research_datasets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,                       -- user-friendly label
    source TEXT NOT NULL,                     -- 'binance' | 'alpaca_sip'
    symbol TEXT NOT NULL,                     -- raw symbol, e.g. 'BTC/USDT' or 'SPY'; for row-backed
                                              -- universe/price_universe rows, symbol mirrors name (the
                                              -- UNIQUE discriminator that keeps two same-source universes
                                              -- distinct; see create_price_universe / rename_price_universe)
    timeframe TEXT NOT NULL,                  -- '1m' | '5m' | '15m' | '1h' | '4h' | '1d'
    start_ts INTEGER NOT NULL,                -- unix seconds UTC, inclusive
    end_ts INTEGER NOT NULL,                  -- unix seconds UTC, exclusive
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'fetching' | 'ready' | 'failed' | 'deleted'
    rows INTEGER,                             -- populated on ready
    sha256 TEXT,                              -- of the parquet file, populated on ready
    file_path TEXT,                           -- container-relative /data/... path, populated on ready
    file_bytes INTEGER,                       -- populated on ready
    last_error TEXT,                          -- populated on failed
    created_at INTEGER NOT NULL,
    last_fetched_at INTEGER,                  -- populated on ready or failed
    -- covered_start_ts / covered_end_ts (the ACTUAL fetched bar range, unix s UTC; W3)
    -- are added in init_db() via _add_column_if_missing so the migration is idempotent.
    -- start_ts/end_ts above stay the REQUESTED window (and the UNIQUE key); covered_* are
    -- the derived value captured at fetch completion, NULL on legacy/row-backed rows.
    UNIQUE (source, symbol, timeframe, start_ts, end_ts)
);

CREATE INDEX IF NOT EXISTS idx_datasets_status ON research_datasets(status);
CREATE INDEX IF NOT EXISTS idx_datasets_source ON research_datasets(source);

-- ============================================================
-- research_settings  (M2)
-- ============================================================
-- Presentation columns (unit, label) are added via _add_column_if_missing
-- in init_db() so the migration is idempotent on existing databases.
CREATE TABLE IF NOT EXISTS research_settings (
    scope TEXT NOT NULL,                      -- e.g. 'fees', 'slippage', 'paths'
    key TEXT NOT NULL,                        -- e.g. 'crypto.default', 'equities.default'
    value TEXT NOT NULL,                      -- always TEXT; caller parses per value_type
    value_type TEXT NOT NULL,                 -- 'string' | 'number' | 'boolean' | 'json'
    description TEXT,                         -- human-readable, surfaced in SettingsPage
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope, key)
);

-- ============================================================
-- research_setting_scopes  (M2 brief F.1)
-- ============================================================
-- Per-scope presentation metadata (title, description, display order).
-- Seeded from _DEFAULT_SETTINGS_LAYOUT on every startup; rows can be
-- safely re-synced because nothing user-editable lives here.
CREATE TABLE IF NOT EXISTS research_setting_scopes (
    scope         TEXT    PRIMARY KEY,
    title         TEXT    NOT NULL,
    description   TEXT,
    display_order INTEGER NOT NULL DEFAULT 100,
    seeded_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_setting_scopes_order
    ON research_setting_scopes(display_order, scope);

-- ============================================================
-- research_settings_history  (M2)
-- ============================================================
CREATE TABLE IF NOT EXISTS research_settings_history (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,                           -- NULL on first insert
    new_value TEXT NOT NULL,
    changed_at INTEGER NOT NULL,
    changed_by TEXT NOT NULL DEFAULT 'user'
);

CREATE INDEX IF NOT EXISTS idx_settings_history_scope_key
    ON research_settings_history(scope, key, changed_at);

-- ============================================================
-- research_strategies  (M3 §3.2) — current state, one row per strategy name
-- ============================================================
CREATE TABLE IF NOT EXISTS research_strategies (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT    NOT NULL UNIQUE,         -- e.g. 'ma_cross'
    source_code          TEXT    NOT NULL,                -- full Python file content
    description          TEXT,                            -- human-readable markdown, from module docstring (L29)
    version              INTEGER NOT NULL DEFAULT 1,      -- bumped on each PUT or CLI register with changed content
    disk_mirror_path     TEXT,                            -- nullable; full host path of the mirrored file
    last_mirror_status   TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (last_mirror_status IN ('pending','synced','stale','failed')),
    last_mirror_error    TEXT,                            -- nullable; populated on failed hook
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
);

-- ============================================================
-- research_strategy_id_counter (lane SID1, DECISIONS #488 ruling 10): the highest strategy id ever given,
-- so a removed strategy's id is never handed out again. One row; the trigger raises it for any insert.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_strategy_id_counter (
    one      INTEGER PRIMARY KEY CHECK (one = 1),
    high_id  INTEGER NOT NULL CHECK (high_id >= 0)
);
CREATE TRIGGER IF NOT EXISTS trg_strategy_id_counter AFTER INSERT ON research_strategies
BEGIN
    INSERT OR IGNORE INTO research_strategy_id_counter (one, high_id) VALUES (1, NEW.id);
    UPDATE research_strategy_id_counter SET high_id = NEW.id WHERE one = 1 AND high_id < NEW.id;
END;

-- ============================================================
-- research_strategies_history  (M3 §3.3) — append-only event log, one row per PUT
-- ============================================================
CREATE TABLE IF NOT EXISTS research_strategies_history (
    id           INTEGER PRIMARY KEY,
    strategy_id  INTEGER NOT NULL,
    version      INTEGER NOT NULL,
    source_code  TEXT    NOT NULL,    -- full snapshot, not a diff
    description  TEXT,                -- snapshot of description at this version
    edited_at    INTEGER NOT NULL,
    FOREIGN KEY (strategy_id) REFERENCES research_strategies(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_strat_hist_sid ON research_strategies_history(strategy_id, version);

-- ============================================================
-- research_tags + research_strategy_tags  (M4 D6, AD5) — strategy organization tags
-- ============================================================
-- The REIFIED form (canon :448: IDs over free text for consistent style, easy global rename,
-- structured search): a tag rename touches one research_tags row, not every junction row. Open
-- vocabulary — a new tag name creates a row on first use (no pre-seeded closed list). Tags are
-- editable WITHOUT a version bump (MD3): they are keyed on strategy_id, not on source_code or the
-- append-only history, so a tag change never bumps the strategy version. Engine-neutral.
CREATE TABLE IF NOT EXISTS research_tags (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE         -- one row per tag name; a rename is O(1) here
);
CREATE TABLE IF NOT EXISTS research_strategy_tags (
    strategy_id  INTEGER NOT NULL,
    tag_id       INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, tag_id),           -- prevents a duplicate tag on a strategy
    FOREIGN KEY (strategy_id) REFERENCES research_strategies(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)      REFERENCES research_tags(id)       ON DELETE CASCADE
);

-- ============================================================
-- research_strategy_settings  (TERM M4.5 fold, brief 3a B1.1 / arch note D1 Option A)
-- ============================================================
-- The per-strategy override store: the system of record for the fact "strategy S sets
-- (scope, key) to V", distinct from research_settings (the lab-wide default for (scope, key)).
-- Each override stored once, addressable, history-able (DDIA Ch 3, each fact once). A run resolves
-- override -> global default -> code default and freezes the result into research_runs.settings_snapshot.
-- PK leads with strategy_id so "all overrides for this strategy" is a primary-key range seek.
-- FK ON DELETE CASCADE is correct under AD8.1 keep+orphan: a zero-run strategy hard-removal takes its
-- overrides; a tombstoned strategy keeps its row + overrides harmlessly (a completed run already froze
-- its resolved values). source_note is written in 3b (D4 override justification); 3a always writes NULL.
CREATE TABLE IF NOT EXISTS research_strategy_settings (
    strategy_id  INTEGER NOT NULL REFERENCES research_strategies(id) ON DELETE CASCADE,
    scope        TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    value        TEXT    NOT NULL,               -- TEXT, parsed per value_type (the research_settings convention)
    value_type   TEXT    NOT NULL,
    source_note  TEXT,                            -- written in 3b (D4); 3a always writes NULL
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, scope, key)
);

-- ============================================================
-- research_setting_provenance  (TERM M4.5 fold, brief 3b B1.1 / arch note D5 / AD7)
-- ============================================================
-- The writable provenance store: source / mechanism / sensitivity / bounds (the four prose fields) for a
-- USER-AUTHORED setting key, keyed 1:1 to (scope, key) exactly as AD7 specifies. Built-in keys keep their
-- provenance in committed markdown (parameter_provenance.md); this table is the DB half of the DB-first-
-- then-markdown read (provenance_entry, B1.2) — the two domains are disjoint (a built-in scope name may
-- not be authored here, the B1.4 namespace guard). bounds_spec is the ONE additive extension beyond AD7's
-- four prose fields (3b B2 / §C): a structured enforcement spec (JSON) so an authored param's bound is
-- machine-checkable (the prose `bounds` field is documentation only). Declared nullable for additive-
-- migration safety; the B1.4 write route guarantees every NEW authored row carries a well-formed one.
CREATE TABLE IF NOT EXISTS research_setting_provenance (
    scope        TEXT    NOT NULL,
    key          TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    mechanism    TEXT    NOT NULL,
    sensitivity  TEXT    NOT NULL,
    bounds       TEXT    NOT NULL,               -- the prose explanation (the four-field display)
    bounds_spec  TEXT,                            -- structured enforcement spec (JSON); see B2 / §C
    authored_at  INTEGER NOT NULL,
    PRIMARY KEY (scope, key)
);

-- ============================================================
-- research_deletions  (TERM F2, keep+orphan) — append-only human-readable record of a strategy delete
-- ============================================================
-- AD8's deletions log, built with the keep+orphan semantics (DECISIONS #127 amended by the TERM
-- strategy-delete arch note): deleting a strategy destroys its LIVE source (blanked on the tombstone
-- row) but KEEPS its runs (relabeled/orphaned) AND its version history, so an orphaned run can still
-- resolve the exact source it ran (the keep-history ruling). This row records THAT a strategy was
-- deleted and how many runs were ORPHANED (not destroyed). NO foreign key to research_strategies:
-- the row must outlive a true
-- row-removal of a zero-run strategy (the reproducibility ethos — a record that something was deleted
-- must never itself be deletable by a cascade).
CREATE TABLE IF NOT EXISTS research_deletions (
    id             INTEGER PRIMARY KEY,
    strategy_id    INTEGER NOT NULL,        -- the deleted strategy's id (informational; not an FK)
    strategy_name  TEXT    NOT NULL,        -- snapshot of the name at delete time
    orphaned_runs  INTEGER NOT NULL,        -- count of runs KEPT + orphaned by this delete (never destroyed)
    deleted_at     INTEGER NOT NULL
);

-- ============================================================
-- research_runs  (M3 §3.4) — one row per completed/failed/cancelled run
-- ============================================================
CREATE TABLE IF NOT EXISTS research_runs (
    id                     INTEGER PRIMARY KEY,
    job_id                 INTEGER NOT NULL UNIQUE,         -- one run per job
    strategy_id            INTEGER NOT NULL,
    strategy_version       INTEGER NOT NULL,                -- snapshot of source at dispatch
    dataset_id             INTEGER NOT NULL,
    dataset_sha256         TEXT    NOT NULL,                -- denormalized; survives dataset delete
    settings_snapshot      TEXT    NOT NULL,                -- JSON; full settings at dispatch
    name                   TEXT,                            -- user-supplied label, NULL when unset; not unique
    backtest_mode          TEXT    NOT NULL DEFAULT 'independent',  -- M5 1A (R-MODE/AD-M5-3): 'independent' | 'shared_capital';
                                                            -- every existing + M5 universe run is independent; the M7-paired
                                                            -- unit sets shared_capital. Ships now so schema/dispatch/cube know the dimension.
    -- Environment columns describe the CONTAINER (DECISIONS #20), known only after the
    -- run finishes. Brief E INSERTs this row at dispatch (status='pending'), so they are
    -- NULL until the /worker/runs/{id}/result callback fills them. NULLABLE (was NOT NULL
    -- in brief A's completion-only design) so a pending/cancelled/failed-before-start run
    -- carries an honest NULL, not a placeholder (brief E; rebuilt by migration, zero rows).
    vbt_version            TEXT,                            -- denormalized; e.g. "2026.4.7+b1727ac6"
    python_version         TEXT,                            -- "3.12.13"
    build_provenance       TEXT,                            -- "git:<commit>+vbt:<commit[:8]>" (renamed from docker_image_digest, DECISIONS #20)
    hostname               TEXT,                            -- "laptop-wsl2"
    -- sizing_method / sizing_spec / cost_model (#376 ruling 4, night three) are added by
    -- _add_column_if_missing in init_db, not here, mirroring the skew/kurtosis/universe_sha256
    -- migration-only precedent lower in this file: a fresh DB gets them from that call too, so a
    -- second CREATE-TABLE-shaped source of truth never has to stay in step with this one.
    created_at             INTEGER NOT NULL,
    started_at             INTEGER,
    finished_at            INTEGER,
    status                 TEXT    NOT NULL
                                   CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    error                  TEXT,
    equity_path            TEXT,                            -- container-relative; '/data/runs/{id}/equity.parquet'
    drawdowns_path         TEXT,
    trades_path            TEXT,
    stats_path             TEXT,
    indicators_path        TEXT,                            -- nullable; NULL if strategy emitted no indicators
    meta_path              TEXT,
    FOREIGN KEY (job_id)      REFERENCES research_jobs(id)       ON DELETE CASCADE,
    FOREIGN KEY (strategy_id) REFERENCES research_strategies(id) ON DELETE RESTRICT,
    FOREIGN KEY (dataset_id)  REFERENCES research_datasets(id)   ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_runs_strategy ON research_runs(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS ix_runs_dataset  ON research_runs(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS ix_runs_status   ON research_runs(status, created_at);

-- ============================================================
-- research_run_stats  (M3 §3.5) — flat fact table, one row per run
-- ============================================================
CREATE TABLE IF NOT EXISTS research_run_stats (
    run_id                INTEGER PRIMARY KEY,
    total_return          REAL    NOT NULL,
    sharpe_ratio          REAL,                             -- nullable; undefined if zero variance
    max_drawdown          REAL    NOT NULL,                 -- as fraction, e.g. 0.183 = 18.3%
    num_trades            INTEGER NOT NULL,
    win_rate              REAL,                             -- nullable if num_trades == 0
    total_fees_paid       REAL    NOT NULL,
    total_slippage_cost   REAL    NOT NULL,
    profit_factor         REAL,                             -- nullable if total_loss == 0
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_stats_sharpe ON research_run_stats(sharpe_ratio);
CREATE INDEX IF NOT EXISTS ix_stats_return ON research_run_stats(total_return);
CREATE INDEX IF NOT EXISTS ix_stats_dd     ON research_run_stats(max_drawdown);
CREATE INDEX IF NOT EXISTS ix_stats_trades ON research_run_stats(num_trades);

-- ============================================================
-- research_run_funnel  (Phase-2 Unit 3, audit concern #1) — the per-cell signal funnel FACT table.
-- One row per PRICE-run cell (event/legacy runs have no row; the funnel is a price-path concept — the
-- event path's analog is its coverage_* counts). The DDIA decision (brief §4): a normalized fact table,
-- NOT columns on research_run_stats. M5 1A (AD-M5-3/SF-A4, arch §12, ruling 4) WIDENS this from the
-- Phase-2 PRIMARY KEY (run_id) to the (run_id, variation_id, ticker, row_scope) sweep grain it was always
-- shaped for (DDIA Ch 3 star-schema: model the fact at the finest analysis grain — MANY rows per run
-- under a sweep). On a FRESH DB this new-grain DDL is born directly below; an OLD (run_id-grain) prod
-- table is migrated in place by _ensure_research_run_funnel_m5_grain (the post-transaction recreate). The
-- recreate's __new table MUST keep the column/PK/FK/CHECK definitions below logically identical (same
-- columns, order, types, NULLability, CHECKs, PK, FK; SQLite ignores comments + whitespace, and the probe
-- asserts both paths converge via PRAGMA table_info). The materialized cube stays DEFERRED (DDIA Ch 4:
-- "warehouses keep raw data and use cubes as accelerators"); these raw cells are what a cube accelerates.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_run_funnel (
    run_id               INTEGER NOT NULL,
    variation_id         INTEGER NOT NULL,                -- M5: the param combo within a sweep; 0 for a single run / legacy
    ticker               TEXT    NOT NULL,                -- M5: the per-ticker symbol; '' for a shared-capital group row
    row_scope            TEXT    NOT NULL                 -- M5 SF-A4 discriminator: a per-ticker cell vs a grouped-portfolio row
                                 CHECK (row_scope IN ('cell','group')),
    bars_total           INTEGER NOT NULL,                -- bars in the price series
    bars_after_warmup    INTEGER NOT NULL,                -- bars where every warmup indicator is non-NaN
    raw_entries          INTEGER NOT NULL,                -- True entry signals before the in-position/conflict drop
    entries_after_dedup  INTEGER NOT NULL,                -- buy orders placed (positions opened)
    raw_exits            INTEGER NOT NULL,                -- True exit signals
    fills                INTEGER NOT NULL,                -- filled orders (buys + sells)
    total_trades         INTEGER NOT NULL,                -- = pf.trades.count() = num_trades; incl. an EOD-open position
    exits_by_signal      INTEGER NOT NULL,                -- trades closed on a signal (status_closed)
    exits_by_eod         INTEGER NOT NULL,                -- trades still open at end of data (status_open)
    zero_reason          TEXT    NOT NULL                 -- WHY-ZERO bucket; 'N/A' when there are trades
                                 CHECK (zero_reason IN ('NO-DATA','ALL-NaN-WARMUP','THRESHOLD-NEVER-CROSSED','SIGNALS-BUT-NO-FILLS','ENTRIES-DEDUPED-AWAY','N/A')),
    lead_in_bars         INTEGER,                         -- P2g: the lead-in bars the run's clip used (before the window); NULL = not recorded (a run stored before P2g)
    PRIMARY KEY (run_id, variation_id, ticker, row_scope),
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_funnel_zero_reason ON research_run_funnel(zero_reason);  -- the primary M5 aggregation axis

-- ============================================================
-- M5 1A — the data spine (DECISIONS #49/#170; arch §3-§6, §8; brief docs/briefs/M5_phase1a_spec_brief.md).
-- Four new tables + two views. 1A is ADDITIVE and INERT beyond the trial-ledger backfill: nothing reads
-- or writes the cube/registration tables yet (that is 1B/1C).
-- ------------------------------------------------------------
-- research_trials: the append-only, FK-FREE trial ledger (AD-M5-1, R-LEDGER). The program-wide honesty
-- count survives run/dataset deletion BY THE ABSENCE OF ANY FK — identity is denormalized (the
-- research_runs.dataset_sha256 self-standing-row idiom). Append-only: no UPDATE, no OR REPLACE ever.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_trials (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL,                  -- denormalized; NO FK (survives run delete)
    variation_id       INTEGER NOT NULL,                  -- the param combo within the sweep; 0 for a single run / legacy
    ticker             TEXT    NOT NULL,                  -- evaluated asset's symbol; '' for a universe/event run or a group
    strategy_id        INTEGER NOT NULL,                  -- denormalized; NO FK
    strategy_version   INTEGER NOT NULL,                  -- denormalized snapshot of the source version at dispatch
    dataset_id         INTEGER NOT NULL,                  -- denormalized; NO FK
    dataset_sha256     TEXT    NOT NULL,                  -- denormalized content identity (the count scope, SF-A1 + FIX-1)
    split_id           TEXT    NOT NULL,                  -- content hash over the canonical split (AD-M5-4); 'FULL-SAMPLE' for legacy
    param_config_hash  TEXT    NOT NULL,                  -- hash of the canonical param combo (= variation_id source); 'LEGACY' for backfill
    sharpe             REAL,                              -- nullable; undefined when variance is zero / the run never completed
    trial_kind         TEXT    NOT NULL
                              CHECK (trial_kind IN ('single','sweep_combo','walkforward_path','legacy_backfill')),
    selection_scope    TEXT    NOT NULL
                              CHECK (selection_scope IN ('cumulative','in_isolation')),
    created_at         INTEGER NOT NULL
);
-- Load-bearing (SF-A1 + FIX-1): the default cross-strategy count fixes ONLY the dataset_sha256 prefix
-- (content identity, not the surrogate dataset_id), so a re-fetched identical dataset's trials aggregate
-- under one prefix and the count never splits. dataset_sha256 FIRST is deliberate (arch §3).
CREATE INDEX IF NOT EXISTS ix_trials_count_scope ON research_trials(dataset_sha256, strategy_id, created_at);

-- research_run_cells: the per-cell metrics fact table (AD-M5-2, SF-A2, R-CUBE). The per-cell SOURCE OF
-- TRUTH for the 8 metrics; research_run_stats stays as its derived run-level rollup (computed by 1B's
-- complete_run, NOT here). 1A creates the EMPTY table; nothing writes cells until 1B. CASCADEs (the cube
-- is run-scoped and recomputable — the deliberate opposite of research_trials' no-FK survival).
CREATE TABLE IF NOT EXISTS research_run_cells (
    run_id                INTEGER NOT NULL,
    variation_id          INTEGER NOT NULL,
    ticker                TEXT    NOT NULL,              -- real symbol for a cell; '' for a group row
    row_scope             TEXT    NOT NULL
                                  CHECK (row_scope IN ('cell','group')),
    total_return          REAL    NOT NULL,
    sharpe_ratio          REAL,                          -- nullable; undefined if zero variance
    max_drawdown          REAL    NOT NULL,
    num_trades            INTEGER NOT NULL,
    win_rate              REAL,                          -- nullable if num_trades == 0
    total_fees_paid       REAL    NOT NULL,
    total_slippage_cost   REAL    NOT NULL,
    profit_factor         REAL,                          -- nullable if total_loss == 0
    skew                  REAL,                          -- M5 Track B (F7): per-cell return-distribution skew; nullable (None on a <3-return window)
    kurtosis              REAL,                          -- M5 Track B (F7): per-cell excess kurtosis; nullable (None on a <4-return window)
    PRIMARY KEY (run_id, variation_id, ticker, row_scope),
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_cells_sharpe ON research_run_cells(run_id, sharpe_ratio);  -- grid sort keys (Ch 4)
CREATE INDEX IF NOT EXISTS ix_cells_return ON research_run_cells(run_id, total_return);
CREATE INDEX IF NOT EXISTS ix_cells_dd     ON research_run_cells(run_id, max_drawdown);

-- research_run_cell_refusals (lane D1, 2026-10-07): one row per sweep cell whose position sizing refused
-- (a SizingError for that one (combo, ticker), e.g. a $218,545 share on a $10,000 slice); the rest of the
-- sweep carries on and this is where the refused cell is recorded so a person can see it. DDIA check
-- (docs/books/kleppmann_canon.md ch 3 normalization, ch 4 primary-key index): one fact per cell, stored
-- once, keyed (run_id, variation_id, ticker) so a resumed sweep re-sending the same refusal writes nothing
-- new; the key's leading run_id serves the per-run read, so no second index. param_json is kept here as
-- well because a combo whose every cell refused has no research_run_variations row to join to. Append-only;
-- CASCADEs with its run like research_run_cells. Not a trial: no result was observed for a refused cell.
CREATE TABLE IF NOT EXISTS research_run_cell_refusals (
    run_id        INTEGER NOT NULL,
    variation_id  INTEGER NOT NULL,
    ticker        TEXT    NOT NULL,
    param_json    TEXT    NOT NULL,                      -- the canonical combo, '{}' for a zero-parameter sweep
    error         TEXT    NOT NULL,                      -- the SizingError subclass name, e.g. ZeroShareSizeError
    reason        TEXT    NOT NULL,                      -- its reason= token, e.g. zero_shares, cap_limited
    message       TEXT    NOT NULL,                      -- the full refusal message (prices, shares, dates)
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (run_id, variation_id, ticker),
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);

-- research_run_variations: the per-sweep variation_id -> human-readable param values lookup (AD-M5-2), so
-- the cube fact rows stay narrow (ids, not repeated param dicts). 1A creates the empty table; 1B writes it.
CREATE TABLE IF NOT EXISTS research_run_variations (
    run_id        INTEGER NOT NULL,
    variation_id  INTEGER NOT NULL,
    param_json    TEXT    NOT NULL,                      -- the canonical combo, e.g. {"fast": 10, "slow": 30}
    PRIMARY KEY (run_id, variation_id),
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);

-- research_sweep_registrations: the frozen pre-registration record (AD-M5-6, R-PREREG, ruling 2 — a
-- sibling table, NOT a trial_kind=registration ledger row). 1A creates the empty table; the write inside
-- the dispatch transaction + the executed-vs-registered reconciliation are 1C. CASCADEs (the durable
-- honesty lives in the FK-free research_trials; this is the live sweep's reconciliation record).
CREATE TABLE IF NOT EXISTS research_sweep_registrations (
    run_id                INTEGER PRIMARY KEY,           -- one registration per sweep
    registered_grid_hash  TEXT    NOT NULL,              -- hash of the frozen canonical parameter product
    registered_n          INTEGER NOT NULL,              -- the registered combination count
    selection_scope       TEXT    NOT NULL
                                  CHECK (selection_scope IN ('cumulative','in_isolation')),
    grid_json             TEXT    NOT NULL,              -- the frozen canonical grid (the registered product)
    registered_at         INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);

-- The two CQRS read-model views over the research_trials event log (AD-M5-1, SF-A1 + FIX-1). Plain SQLite
-- views (computed on read) at M5 scale; materializing into a table is deferred until measured slow (the
-- #168 cubes-are-accelerators discipline). The default cumulative count is cross-strategy per-CONTENT
-- (dataset_sha256), NOT per-strategy and NOT dataset_id-scoped: a re-fetched identical dataset takes a new
-- dataset_id, so a dataset_id-scoped count would split N, make the DSR less conservative, and open a
-- re-fetch-to-reset loophole (FIX-1).
-- trial_count is the DSR's honesty count N: it counts EVERY trial_kind, legacy_backfill INCLUDED (a
-- backfilled pre-M5 run was a real draw against the data). This is deliberately NOT the variance view's
-- variance_n (which excludes legacy_backfill); the Phase-3 DSR uses trial_count for N and variance_n for V.
CREATE VIEW IF NOT EXISTS v_trial_count_by_dataset AS
SELECT dataset_sha256, COUNT(*) AS trial_count
FROM research_trials
GROUP BY dataset_sha256;

-- The Sharpe-variance view EXCLUDES legacy_backfill rows: their full-sample Sharpes must not mix into a
-- variance taken over split-OOS Sharpes (two estimands). SQLite has no VAR()/STDEV(); the Phase-3 DSR
-- computes the sample variance V = (sharpe_sumsq - sharpe_sum*sharpe_sum/variance_n) / (variance_n - 1)
-- from these sums, using THIS view's variance_n (NOT v_trial_count_by_dataset.trial_count).
CREATE VIEW IF NOT EXISTS v_trial_sharpe_variance_by_dataset AS
SELECT dataset_sha256,
       COUNT(*)             AS variance_n,
       AVG(sharpe)          AS sharpe_mean,
       SUM(sharpe)          AS sharpe_sum,
       SUM(sharpe * sharpe) AS sharpe_sumsq
FROM research_trials
WHERE trial_kind <> 'legacy_backfill'
  AND sharpe IS NOT NULL
GROUP BY dataset_sha256;

-- ============================================================
-- research_shared_runs  (M3 §3.6) — permalink share rows
-- ============================================================
CREATE TABLE IF NOT EXISTS research_shared_runs (
    uuid                TEXT    PRIMARY KEY,                -- UUID4 string, the share secret
    run_id              INTEGER NOT NULL,
    include_source      INTEGER NOT NULL DEFAULT 1,         -- 0|1 boolean
    include_settings    INTEGER NOT NULL DEFAULT 1,
    include_indicators  INTEGER NOT NULL DEFAULT 1,
    include_trades      INTEGER NOT NULL DEFAULT 1,
    include_logs        INTEGER NOT NULL DEFAULT 0,
    view_limit          INTEGER,                            -- nullable; NULL = unlimited (API default 10 lives in brief K.6)
    view_count          INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    revoked_at          INTEGER,                            -- nullable; non-null = revoked
    last_viewed_at      INTEGER,                            -- nullable; updated on each successful GET
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_shared_runs_run    ON research_shared_runs(run_id);
CREATE INDEX IF NOT EXISTS ix_shared_runs_active ON research_shared_runs(run_id) WHERE revoked_at IS NULL;

-- ============================================================
-- research_eodhd_tickers (M5 U0 / AD-M5-14) — the EODHD daily availability index
-- ============================================================
-- A server-side list of "which EODHD tickers exist", so the Add-Dataset ticker search has
-- something to read (the daily parquet files are laptop-local; the backend runs on the server).
-- Filled by load_eodhd_tickers() from the committed manifest dashboard/backend/data/eodhd_tickers.txt.
-- The ticker is the BARE form (AD-M5-14: 'AAPL', the '.US.parquet' filename stem stripped) — the one
-- canonical wire/storage form U1's validate-on-create reads. The PK keeps the table ordered for the
-- prefix search. Additive: a fresh DB creates it here; an existing prod DB gains it on the next init_db.
CREATE TABLE IF NOT EXISTS research_eodhd_tickers (
    ticker TEXT PRIMARY KEY                      -- bare ticker, e.g. 'AAPL'; PK orders the table for the search
);

-- research_price_universe_membership (M5 U1, AD-M5-11 / R-UNIV): a ticker-keyed price universe's members.
-- Separate from the CIK research_universe_membership (eventsdb.py) by design: two entities, two natural keys
-- (a price universe is keyed by TICKER and validated by data availability; the CIK universe is keyed by
-- subject_cik). The PRIMARY KEY (universe_id, ticker) IS the UNIQUE(universe_id, ticker) and serves the
-- "list universe X's members" prefix scan directly (DDIA Ch 4 concatenated index). universe_id is a SOFT FK
-- (a plain column, the same idiom research_universe_membership uses), NOT a cascading FK, so a soft-deleted
-- universe keeps its membership and restore flips it back. Additive: a fresh DB creates it; an existing prod
-- DB gains it on the next init_db.
CREATE TABLE IF NOT EXISTS research_price_universe_membership (
    universe_id INTEGER NOT NULL,                -- soft FK to the price_universe catalog row in research_datasets
    ticker      TEXT    NOT NULL,                -- the BARE ticker (AD-M5-14), e.g. 'AAPL'; the durable identity
    added_at    INTEGER NOT NULL,               -- unix seconds UTC
    PRIMARY KEY (universe_id, ticker)           -- a member appears once per universe; prefix-orders by universe
);

-- research_price_universe_member_spans (DL1, DECISIONS #455): every span a ticker has been a member of a price
-- universe, with the date it was added and the date it was removed (removed_at NULL = still a member).
-- DDIA check (Ch 3, "Event sourcing and CQRS"): this table is the record of membership over time and
-- research_price_universe_membership stays today's list, derived from it and written in the same
-- transaction as every span change. Read patterns: (1) "who was in universe X on date D", served by
-- ix_member_spans_universe_added (universe_id, added_at); (2) "is this ticker's span open", served by the
-- partial unique index on (universe_id, ticker) WHERE removed_at IS NULL, which also stops two open spans.
-- A re-added ticker gets a second span; a removal never deletes a span. Additive: a fresh DB creates it, an
-- existing DB gains it (and one open span per current member) on the next init_db.
CREATE TABLE IF NOT EXISTS research_price_universe_member_spans (
    id          INTEGER PRIMARY KEY,
    universe_id INTEGER NOT NULL,                -- soft FK to the price_universe catalog row (the membership idiom)
    ticker      TEXT    NOT NULL,                -- the BARE ticker, as in research_price_universe_membership
    added_at    INTEGER NOT NULL,                -- unix seconds UTC
    removed_at  INTEGER,                         -- unix seconds UTC; NULL while the ticker is still a member
    CHECK (removed_at IS NULL OR removed_at >= added_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_member_spans_open
    ON research_price_universe_member_spans(universe_id, ticker) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_member_spans_universe_added
    ON research_price_universe_member_spans(universe_id, added_at);

-- research_sweep_members (M5 U2 / 1C, AD-M5-13 phase-2 orchestration): the run -> resolved member-dataset
-- link a materializing sweep writes at dispatch, so the ready-trigger can answer "is this sweep's whole
-- member set ready yet?" without re-resolving tickers->datasets on every dataset-ready callback (and it pins
-- the EXACT reused/created dataset per member, R-SKIP-aware).
-- DDIA pass (this is a NEW table, per the operating contract): a join table at the (run, dataset) grain. A
-- sweep run has N member datasets, and one member dataset can belong to many runs (R-SKIP reuses a ready
-- dataset across runs), so the relationship is genuinely M:N — DDIA Ch 3 normalization says a join table,
-- not a JSON blob of dataset_ids on the run (a blob cannot be indexed or joined, and would re-implement the
-- natural key by hand). Read patterns: (1) "list/are-ready this run's members" — a prefix scan fixing run_id,
-- served directly by the PRIMARY KEY (run_id, dataset_id) concatenated index (Ch 4, fix-the-prefix); (2) the
-- ready-trigger's reverse "which materializing runs include this dataset?" — a lookup by dataset_id (the
-- NON-prefix column), fired on EVERY dataset-ready callback (not just sweeps) and over a table that grows with
-- every sweep ever dispatched, so it gets its own index ix_sweep_members_dataset on (dataset_id) to stay
-- O(matches) not O(table). run_id is an FK ON DELETE CASCADE (the link is run-scoped derived data,
-- recomputable — the deliberate opposite of research_trials' FK-free survival); dataset_id is a SOFT
-- reference (a plain column, the research_price_universe_membership idiom), so a member dataset's lifecycle
-- stays independent of the link.
CREATE TABLE IF NOT EXISTS research_sweep_members (
    run_id     INTEGER NOT NULL,                 -- the materializing sweep run
    dataset_id INTEGER NOT NULL,                 -- soft ref to the resolved member price_dataset row (no cascading FK)
    PRIMARY KEY (run_id, dataset_id),            -- a member dataset appears once per run; prefix-orders by run
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_sweep_members_dataset ON research_sweep_members(dataset_id);  -- the reverse (by-dataset) ready-trigger lookup

-- ============================================================
-- research_full_metrics_cache (M5 Track C, C-metrics §2.2 / C2 §3) — the lazy, immutable full-metrics
-- accelerator (DDIA Ch 4 warehouse-not-cube, #168): the expensive full-metric set is computed ON DEMAND
-- (the worker derive for a single run, the re-run for a cell) and cached once, never pre-materialized over
-- the 50M-cell space. Recomputable (the raw artifacts + the frozen sweep inputs survive it), so it CASCADEs
-- with its run, the same recomputable-accelerator discipline research_run_cells uses. The key includes a
-- formula_version (the code/formula identity, C-metrics §2.2) so a formula/engine bump is a natural cache
-- miss (a new row), never a stale payload served. A single run uses the sentinel (variation_id=-1,
-- ticker='') which cannot collide with any cell (a real cell has variation_id>=0 and a non-empty ticker).
CREATE TABLE IF NOT EXISTS research_full_metrics_cache (
    run_id          INTEGER NOT NULL,
    variation_id    INTEGER NOT NULL,          -- -1 for a single run (no variation); the cell's variation_id otherwise
    ticker          TEXT    NOT NULL,          -- '' for a single run; the cell's bare ticker otherwise
    formula_version TEXT    NOT NULL,          -- the code/formula identity (C-metrics §2.2); a different version is a different row
    payload_json    TEXT    NOT NULL,          -- the computed full-metric set (immutable on the frozen inputs)
    computed_at     INTEGER NOT NULL,          -- unix seconds UTC, when the payload was computed
    PRIMARY KEY (run_id, variation_id, ticker, formula_version),
    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
);

-- ============================================================
-- research_benchmark_catalog (M5 Track C, C-bench §2.1) — one row per candidate benchmark, grouped by
-- exposure key (the mandate bucket). A normalized mapping table (DDIA Ch 3 star-schema dimension), NOT
-- columns bolted onto an existing table, because a benchmark is a many-candidates-per-exposure relation. The
-- informational stats (vol/return/Sharpe) are NOT stored here (they would go stale); they are computed on
-- demand from the benchmark ETF's own eodhd series (C-bench §2.1, the #168 compute-on-read discipline). Seed
-- data, loaded idempotently at boot (the load_eodhd_tickers precedent); every benchmark_ticker must exist in
-- the eodhd availability index (research_eodhd_tickers), validated on write (C-bench P1).
CREATE TABLE IF NOT EXISTS research_benchmark_catalog (
    exposure_key     TEXT NOT NULL,            -- the mandate bucket: 'broad_market' | 'sector:technology' | 'sector:energy' | ...
    benchmark_ticker TEXT NOT NULL,            -- the bare ETF ticker (AD-M5-14), e.g. 'SPY', 'XLK'
    label            TEXT NOT NULL,            -- human-readable: 'S&P 500 (SPY)', 'Technology sector (XLK)'
    is_default       INTEGER NOT NULL DEFAULT 0,-- one default per exposure_key (the pre-committed pick)
    provenance       TEXT NOT NULL,            -- why this ETF maps here (the mandate reason; chosen-by-exposure-not-fit)
    PRIMARY KEY (exposure_key, benchmark_ticker)
);

-- ============================================================
-- research_generated_candidates (generator unit 1, lane-list row 3.2, night one lane G1,
-- DECISIONS #308/#329, spec: docs/plans/night/2026-09-07/PLAN.md section 5, architecture
-- AD6.2) -- every expression the generator emits, accepted or refused, gets one row.
--
-- DDIA pass (docs/books/kleppmann_canon.md).
--   One file, research.db (AD6.1): SQLite cannot transact across files and this file's own
--   rule states "Nothing joins across the two research databases"; the candidate row must
--   be readable beside research_strategies in one transaction at dispatch.
--   Normalized (Ch 3, "Using IDs ... instead of free text gives: consistent style, no
--   ambiguity ... Cost: a join to resolve"): the row carries theory_id/theory_sha256/
--   skeleton_id/filler_id and the strategy_id soft reference, never a copy of the theory
--   or the source; expression/expression_sha256 are the one deliberate denormalisation,
--   because a REFUSED row has no strategy to join to and the expression is what a human
--   reads when triaging a refusal.
--   Exactly two indexes (Ch 4, an index "speeds up reads at the cost of slower writes"):
--   ux_gencand_identity, the unique index AD4.1 step 6 reads to make regeneration
--   idempotent, and ix_gencand_strategy, which the dispatch re-verification
--   (dashboard/backend/generated.py) and the generated_rows_without_candidate monitor
--   both read. No index on decision or refusal_reason: refusal counts are rare reads over
--   a table that grows by the product of two short lists.
--   Rows are immutable events (Ch 3, "Events are immutable; new events supersede old
--   ones"): a candidate row is written once and never updated; a refusal is a row, not a
--   state flip; a rerun writes nothing at all (the unique index makes that a no-op, not a
--   second row).
--   Schema-on-write with two CHECKs (Ch 3 on nullable columns as the cheap evolution
--   path): a half-written row (a refusal with no reason, or an acceptance with no
--   strategy) is unwritable, and the second CHECK fixes the write ORDER too -- an
--   'accepted' row needs strategy_id before it can exist, so the strategy write must
--   precede the candidate write for that decision (AD4.1 step 7).
--   Soft reference, no cascade (the same idiom this file already documents on
--   research_price_universe_membership): the record of what was generated must survive
--   the deletion of the strategy it produced, or the trial count's honesty is destroyed
--   by a delete.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_generated_candidates (
    id                 INTEGER PRIMARY KEY,
    theory_id          TEXT    NOT NULL,          -- the theory file's stem
    theory_sha256      TEXT    NOT NULL,          -- content identity of that file at generation time
    skeleton_id        TEXT    NOT NULL,          -- from the theory's Skeletons list
    filler_id          TEXT    NOT NULL,          -- from the theory's Fillers list
    expression         TEXT    NOT NULL,          -- the emitted formula string, verbatim
    expression_sha256  TEXT    NOT NULL,          -- content identity of that string
    decision           TEXT    NOT NULL
                               CHECK (decision IN ('accepted','refused')),
    refusal_reason     TEXT,                      -- NULL iff accepted
    strategy_id        INTEGER,                   -- soft ref to research_strategies.id; NULL iff refused
    generated_at       INTEGER NOT NULL,          -- unix seconds UTC
    CHECK ((decision = 'refused') = (refusal_reason IS NOT NULL)),
    CHECK ((decision = 'accepted') = (strategy_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_gencand_identity
    ON research_generated_candidates(theory_sha256, skeleton_id, filler_id, expression_sha256);
CREATE INDEX IF NOT EXISTS ix_gencand_strategy
    ON research_generated_candidates(strategy_id);

-- ============================================================
-- research_data_draws (lane T, night two, docs/plans/night/2026-09-08/PLAN.md section 6;
-- DECISIONS #329, spec: generator_requirements_2026-09-05.md section 6, architecture
-- generator_architecture_2026-09-05.md AD6.3) -- the data-use tracker's one table. An event
-- log of every stage draw, run link, bars-observed report and held-out name draw, never a
-- state table: reading "has this candidate consumed up to time T" is a query over this log,
-- not a column anywhere that could drift from it.
--
-- DDIA pass (docs/books/kleppmann_canon.md).
--   One file, research.db (Ch on transactions): the chronology read and its ledger append
--   must be atomic WITH EACH OTHER (R-T3), and research.db is where research_datasets,
--   research_runs and research_sweep_registrations already live; SQLite cannot transact
--   across files, and this file's own rule states "Nothing joins across the two research
--   databases."
--   An event log, not a state table (Ch 3, line 802: "Event log = append-only sequence of
--   self-contained event records ... Events are immutable; new events supersede old ones";
--   line 808: "order matters"): four kinds of row, each written once; the run link is a
--   LATER event rather than an update to the draw (R-T14), the observation a later event
--   still; the high-water mark is a query over granted requests, never a stored column.
--   Normalized, ids and no copies (Ch 3, line 448: "Using IDs ... instead of free text
--   gives: consistent style, no ambiguity ... Cost: a join to resolve"): the row carries
--   strategy_id/strategy_version/run_id/draw_id, never a strategy name or a copied window
--   string; the one deliberate denormalisation is the candidate columns copied onto
--   run_linked and bars_observed rows (they are ABOUT that draw's own candidate), so the
--   observation query needs no join back to the immutable draw row.
--   Three indexes, each with one reader (Ch 4, line 859: an index "speeds up reads at the
--   cost of slower writes"; line 1075 on a concatenated index fixing a prefix):
--   ix_draws_highwater is the concatenated index the high-water query fixes a five-column
--   prefix of on every dispatch; ix_draws_by_draw serves the run-link and observation reads;
--   ux_draws_names_drawn_once is the partial unique index making "once per calendar" a
--   database fact rather than an operational assumption. No index on decision alone or on
--   created_at: refusal counts and age queries are rare monitor reads over a table that
--   grows by a handful of rows per dispatch.
--   Schema-on-write with eleven CHECKs (Ch 3 on nullable columns as the cheap evolution
--   path; Ch 5, line 1305): every partial row the four kinds could produce is unwritable, so
--   the write order and the kind vocabulary are fixed by the store rather than by the code
--   remembering it correctly every time.
--   Soft references, no cascade (the same idiom this file already documents on
--   research_price_universe_membership and research_generated_candidates): the draw record
--   must survive the deletion of the strategy or the run it names, or the trial count and
--   the chronology itself would lie.
--   Append only. No UPDATE, no OR REPLACE, ever, with no exception at all -- the same
--   discipline research_trials already states in its own comment.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_data_draws (
    id                    INTEGER PRIMARY KEY,
    event_kind            TEXT    NOT NULL
                                  CHECK (event_kind IN ('draw_requested','run_linked','bars_observed','names_drawn')),
    strategy_id           INTEGER,              -- denormalized, NO FK: survives a strategy delete. NULL only on names_drawn
    strategy_version      INTEGER,              -- (strategy_id, strategy_version) IS the candidate. NULL only on names_drawn
    calendar_id           TEXT    NOT NULL,     -- 'real', or a named dry-run calendar
    counts_against_budget INTEGER NOT NULL
                                  CHECK (counts_against_budget IN (0,1)),
    stage                 TEXT
                                  CHECK (stage IS NULL OR stage IN ('screen','optimise','walkforward','reserve')),
    window_start_ts       INTEGER,              -- unix seconds UTC
    window_end_ts         INTEGER,
    member_count          INTEGER,              -- names in the drawn universe at request time
    decision              TEXT
                                  CHECK (decision IS NULL OR decision IN ('granted','refused')),
    refusal_reason        TEXT,
    seed                  INTEGER,
    seed_purpose          TEXT,
    run_id                INTEGER,              -- soft ref to research_runs.id; carried by run_linked ONLY (see the note below)
    draw_id               INTEGER,              -- on run_linked and bars_observed: the draw_requested row's id
    bars_observed         INTEGER,              -- on bars_observed only; 0 is a real value
    created_at            INTEGER NOT NULL,     -- unix seconds UTC, the append time, on every row of every kind
    CHECK ((event_kind = 'names_drawn') = (strategy_id IS NULL)),
    CHECK ((strategy_id IS NULL) = (strategy_version IS NULL)),
    CHECK ((event_kind IN ('draw_requested','run_linked')) = (decision IS NOT NULL)),
    CHECK ((event_kind = 'run_linked') = (draw_id IS NOT NULL AND run_id IS NOT NULL)),
    CHECK (event_kind <> 'draw_requested' OR run_id IS NULL),
    CHECK ((event_kind = 'bars_observed') = (draw_id IS NOT NULL AND bars_observed IS NOT NULL)),
    CHECK ((seed IS NULL) = (seed_purpose IS NULL)),
    CHECK (decision IS NOT 'refused' OR refusal_reason IS NOT NULL),
    CHECK (NOT (event_kind = 'draw_requested' AND decision = 'granted')
           OR (window_start_ts IS NOT NULL AND window_end_ts IS NOT NULL
               AND window_end_ts > window_start_ts AND stage IS NOT NULL)),
    CHECK (event_kind <> 'names_drawn' OR (seed IS NOT NULL AND member_count IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_draws_highwater
    ON research_data_draws(event_kind, strategy_id, strategy_version, calendar_id, decision, window_end_ts);
CREATE INDEX IF NOT EXISTS ix_draws_by_draw
    ON research_data_draws(draw_id);
-- Lane DG (`6.1g`, the DF2 map's own owed debt, the `#397` lean): link_run, draw_id_for_run and
-- export.py's stage read all look up a draw row by run_id on this append-only log (DDIA chapter 4,
-- "Multicolumn and secondary indexes" section: "Secondary index = lookup by other columns", which
-- is exactly this column, run_id, never the table's own primary key). The statement right below is
-- inside this SAME _DDL block, re-run on every init_db() call (executescript): that re-run IS the
-- idempotent migration for an already-deployed database, so no separate _add_column_if_missing
-- style helper is needed for a new index the way one is needed for a new column, since the
-- statement's own IF-NOT-EXISTS clause already makes it safe to re-run against a table that
-- already exists.
CREATE INDEX IF NOT EXISTS ix_draws_run_id
    ON research_data_draws(run_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_draws_names_drawn_once
    ON research_data_draws(calendar_id)
    WHERE event_kind = 'names_drawn';

-- ============================================================
-- research_theories (night four lane S, job 2.4, DECISIONS #377 ruling 2, docs/plans/night/
-- 2026-09-09_set4a_night/PLAN.md section 10) -- the theory store: one row per saved VERSION of a
-- theory file (docs/research_lab/theories/*.md), imported once by content sha at init_db time
-- (import_theory_files below). Append only: no UPDATE, no DELETE anywhere in this module (a probe
-- greps for both against this table's name).
--
-- DDIA pass (docs/books/kleppmann_canon.md), read for this build.
--   IDs over free text (Ch 3, line 448: "Using IDs ... instead of free text gives: consistent
--   style, no ambiguity ... Cost: a join to resolve"): theory_id is the file's stem, a stable
--   human-readable label, but sha256 is the identity a later reader actually COMPARES BY -- the
--   exact bytes research_lab/generator/theory.py hashes at generation time
--   (hashlib.sha256(path.read_bytes())), so a research_generated_candidates row's own
--   theory_sha256 resolves to exactly one row here (or to none, if the candidate predates this
--   table) with no ambiguity from a renamed or re-saved file.
--   Event log, not a state table (Ch 3, line 802: "Event log = append-only sequence of
--   self-contained event records ... Events are immutable; new events supersede old ones"): a new
--   version of a theory file is a NEW row, never an UPDATE of the old one -- the same shape
--   research_generated_candidates (directly above) and research_data_draws (directly above that)
--   already chose in this file, and for the same reason: rewriting a theory's saved content after
--   a generation run already read it would make that run's own provenance unable to be re-checked.
--   No nullable-evolution columns here (Ch 5, line 1305 is the two research_datasets columns
--   below, not this table): every column here is NOT NULL because this is a brand-new table with
--   no pre-existing rows to migrate, not an existing table gaining an optional field.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_theories (
    id          INTEGER PRIMARY KEY,
    theory_id   TEXT    NOT NULL,                  -- the theory file's stem (theory.py's own Theory.theory_id)
    sha256      TEXT    NOT NULL UNIQUE,            -- content identity: hashlib.sha256(raw file bytes)
    content     TEXT    NOT NULL,                   -- the file's full text at this saved version
    source      TEXT    NOT NULL CHECK (source IN ('file_import', 'editor')),  -- 'editor' is U.4's future write path; unused today
    created_at  TEXT    NOT NULL                    -- ISO-8601 UTC, the import/save time
);
CREATE INDEX IF NOT EXISTS ix_theories_theory_id_created
    ON research_theories(theory_id, created_at);

-- ============================================================
-- research_verdict_rejections (night four lane S, job 2.4, U.5's REJECT button, UI plan
-- 2.4.2 line 142: "REJECT (records Tamer's rejection on the verdict, a new column or table the
-- planner names)") -- Tamer's rejection of one graded verdict. A TABLE and not a column on
-- research_judge_verdicts (10.1's own decision): the verdict store is exactly-once and append-only
-- (judgedb.record_verdict's own ON CONFLICT DO NOTHING idiom), and a rejection is a LATER, separate
-- event about that verdict, never a rewrite of it.
--
-- DDIA pass (docs/books/kleppmann_canon.md), read for this build.
--   A later event, not an update (Ch 3, line 802 on event logs, the same citation
--   research_theories above already applies): the verdict row records what the battery decided at
--   grading time; a rejection is Tamer's own later decision about that verdict, appended beside
--   it, never a mutation of it -- so the verdict store's own exactly-once guarantee is never
--   touched by a rejection.
--   IDs over a join, soft reference rather than a FOREIGN KEY (Ch 3, line 448 on IDs; the same
--   soft-reference idiom research_generated_candidates and research_data_draws already use in
--   this file for a record that must survive the deletion of what it names): verdict_id and run_id
--   name rows in research_judge_verdicts and research_runs, but research_judge_verdicts is created
--   in a DIFFERENT DDL block (judgedb.py's own _DDL_VERDICTS, a different module), and SQLite
--   cannot enforce a FOREIGN KEY across two separately-created schemas the way it can within one
--   executescript. So the join is by id, and NOTHING here validates it at insert time:
--   record_verdict_rejection inserts verdict_id directly, with no lookup into judgedb. U.5's future
--   caller is expected to validate the verdict exists before calling it (S-R1-SF2, round 2:
--   query_verdict_rejections_without_verdict's own docstring already states this correctly, and
--   this paragraph is corrected to agree with it rather than the other way round); that same query
--   is the AFTER THE FACT guard, counting any rejection whose verdict_id names no row in
--   research_judge_verdicts, expected zero.
--   UNIQUE (verdict_id) makes "at most one rejection per verdict" a database fact rather than an
--   operational assumption -- the same idiom ux_draws_names_drawn_once above already uses for "at
--   most one held-out name draw per calendar".
-- ============================================================
CREATE TABLE IF NOT EXISTS research_verdict_rejections (
    id           INTEGER PRIMARY KEY,
    verdict_id   INTEGER NOT NULL UNIQUE,           -- soft ref: research_judge_verdicts.id (different DDL block, no FK)
    run_id       TEXT    NOT NULL,                  -- soft ref: research_runs.id, carried for a join-free read
    rejected_by  TEXT    NOT NULL,                  -- who pressed REJECT; U.5's own auth story, not decided here
    reason       TEXT,                              -- optional free text Tamer may add; nullable on purpose
    rejected_at  TEXT    NOT NULL                   -- ISO-8601 UTC, the press time
);
CREATE INDEX IF NOT EXISTS ix_verdict_rejections_run_id
    ON research_verdict_rejections(run_id);

-- ============================================================
-- research_walkforward_slices (lane WF, 6.2b, DECISIONS #392 ruling 4 / #329 ruling 1) -- one row
-- per (run_id, slice_index): the walk-forward stage's own per-slice record. Written ONCE per slice
-- by record_walkforward_slices, from the real entrypoint's own per-slice emit (run_sweep_entrypoint.py's
-- {"walkforward_slice": ...} lines, relayed through a new worker-scoped route). AC-19's own words:
-- "each slice's in-sample and out-of-sample figures are recorded per slice."
--
-- DDIA pass (docs/books/kleppmann_canon.md), read for this build.
--   Data locality (Ch 3, "Data locality" section: "Documents store as one continuous JSON/BSON
--   string. Good if you need most of the document; bad if you touch only one field (whole document
--   is read/rewritten)."): a walk-forward run's slices are read ONE AT A TIME far more often than
--   as a whole array (a dashboard detail view paging through slices, a probe asserting one slice's
--   own boundary), so a NORMALIZED table with one row per slice -- the SAME grain choice
--   research_run_cells already made for per-cell sweep results -- gives that locality; a single
--   JSON blob column holding the whole array (the settings_snapshot idiom two entries above, chosen
--   there because sizing_spec/cost_model ARE read and written as one self-contained fact, never by
--   an inner field) would be the wrong shape here, since it forces deserializing every slice to
--   read one. The run-level AGGREGATE figures (in_sample_profit, out_of_sample_profit,
--   wfa_boundary_buffer_bars) go the other way, as three plain nullable scalar columns on
--   research_run_stats directly below, because they ARE read together with that row's other eight
--   stats columns as one whole fact about the run (the flat-fact-table design this file's own
--   research_run_stats table already chose at its own top) -- adding a fourth table or a nested
--   blob for three scalars would fragment that row's own existing whole-row locality for no gain.
--   IDs over free text (Ch 3; the research_theories precedent two blocks above): param_config_hash
--   is the sha256 of the canonical combo JSON (researchdb._param_config_hash's own rule, duplicated
--   in the container since it may never import this module), never the combo JSON restated, so a
--   join to research_trials/research_run_cells resolves the actual parameter values on demand.
-- ============================================================
CREATE TABLE IF NOT EXISTS research_walkforward_slices (
    run_id                    INTEGER NOT NULL,             -- soft ref: research_runs.id (a run row always exists first, this table is additive-only)
    slice_index               INTEGER NOT NULL,             -- 0-based, in the order the entrypoint computed the slices
    in_sample_start_ts        INTEGER NOT NULL,             -- epoch seconds, the slice's own in-sample leg (inclusive both ends)
    in_sample_end_ts          INTEGER NOT NULL,
    out_of_sample_start_ts    INTEGER NOT NULL,             -- epoch seconds, the slice's own out-of-sample leg (inclusive both ends)
    out_of_sample_end_ts      INTEGER NOT NULL,
    param_config_hash         TEXT    NOT NULL,             -- sha256 of the winning combo's canonical param JSON for this slice
    in_sample_profit          REAL,                         -- nullable: a non-finite (nan/inf) figure is stored as NULL, an honest absence
    out_of_sample_profit      REAL,                         -- same nullability rule, same reason
    trade_count               INTEGER NOT NULL,             -- the slice's own out-of-sample trade count
    PRIMARY KEY (run_id, slice_index)
);

-- ============================================================
-- research_chains / research_chain_transitions (lane CN, set 16c, `#427`, docs/briefs/
-- chain_runner_spec_2026-09-24.md section 8). Two new tables, ADDITIVE ONLY: no existing table
-- changes (CR-1). The chain runner is a small loop, living in dashboard/backend/chain_runner.py,
-- that walks one candidate through screen -> optimise -> choose -> replay -> walkforward -> judge
-- automatically, on its own private calendar, and these two tables are what makes that request,
-- its current state and its full history durable across a backend restart (spec section 10: a
-- restart reads only these stores, nothing is held in memory between passes).
--
-- DDIA pass (docs/books/kleppmann_canon.md), read for this build, reasoning copied from the
-- spec's own section 8 (already read against the book there; restated here beside the DDL itself
-- so a reader of the schema sees the same argument the spec gives):
--   System of record vs derived data (Ch 1, "Systems of record and derived data": "System of
--   record: authoritative; normalized; each fact stored once." vs "a redundant transformation of
--   a system of record"). The request columns (strategy_id, the four *_ts boundaries,
--   param_grid_json), the pick (chosen_variation_id/chosen_ticker/chosen_at) and the two operator
--   flags (stop_requested, retry_requested) are facts nothing else holds, so they are the system
--   of record. `state` is DERIVED: every pass re-derives it from the draws, runs, jobs, replay
--   cache and verdicts (research_data_draws, research_runs, research_jobs,
--   research_full_metrics_cache, research_judge_verdicts), which win on any disagreement; it is
--   stored only so a page read and a CHECK constraint can see it without replaying every log.
--   Event log / CQRS (Ch 3, "Event sourcing and CQRS": "Event log = append-only sequence of
--   self-contained event records", advantage "Provides an audit log."). research_chain_transitions
--   is that log for every STOP, RETRY, pick and refusal; append-only, never UPDATEd or DELETEd,
--   matching research_data_draws' own precedent directly above.
--   Preventing lost updates (Ch 8, "Preventing lost updates": "Conditional writes (CAS)" /
--   "compare a version number column (optimistic locking)"). Every state write is
--   `UPDATE research_chains SET state = ? ... WHERE id = ? AND state = ?`: a STOP, a pick and a
--   pass racing on one row cannot lose each other's write, because each update names the state it
--   expects; a zero-row update means nothing is written and the next pass re-reads.
--   Single-object vs multi-object operations (Ch 8, "Single-object and multi-object operations":
--   "Multi-object transactions are needed for foreign-key integrity, denormalized data kept in
--   sync"). The chain row's state update and its own transition row are written in ONE
--   `researchdb.transaction()` block, so the two can never disagree about which state a chain was
--   ever actually in (CR-20's own atomicity criterion).
--   Constraints (Ch 8, "The meaning of ACID": "Database can enforce some (foreign keys,
--   uniqueness, check constraints; triggers/materialized views for the rest)"). The CHECKs below
--   make an unordered window, a real (non-chain) calendar, a refusal with no word, a word with no
--   detail, or a replay with no chosen cell UNWRITABLE, never merely documented as wrong; the
--   UNIQUE constraint makes "one chain per candidate per calendar" a database fact, matching the
--   tracker's own per-candidate gate.
--   Indexes (Ch 4, key terms and "Concatenated indexes": an index "speeds up reads at the cost of
--   slower writes"). No index on `state` itself: this table gains one row per RUN THE CHAIN press,
--   never per bar or per cell, so a pass's own scan over non-terminal rows is small (an estimate,
--   revisit if K-11's own gauge shows it growing); `ix_chain_transitions_chain` serves the one
--   query this build actually runs often, one chain's own history in insertion order.
--
-- Soft references throughout (strategy_id, duel_chain_id, chain_id on the transitions table): no
-- FOREIGN KEY, matching research_data_draws' own precedent in this same file. calendar_id is
-- forced to start 'chain-' by its own CHECK so a chain request can never share marks with a real
-- dispatch, a dry run or a jc13-style probe calendar (spec section 2.2).
-- ============================================================
CREATE TABLE IF NOT EXISTS research_chains (
    id                    INTEGER PRIMARY KEY,
    strategy_id           INTEGER NOT NULL,      -- soft ref, no FK, the research_data_draws idiom
    strategy_version      INTEGER NOT NULL,      -- pinned at creation, passed as pinned_version
    calendar_id           TEXT    NOT NULL CHECK (calendar_id LIKE 'chain-%'),
    screen_start_ts       INTEGER NOT NULL,
    optimise_start_ts     INTEGER NOT NULL,
    walkforward_start_ts  INTEGER NOT NULL,
    walkforward_end_ts    INTEGER NOT NULL,
    param_grid_json       TEXT    NOT NULL CHECK (json_valid(param_grid_json)),
    duel_chain_id         INTEGER,               -- the partner row, NULL for a single chain
    state                 TEXT    NOT NULL CHECK (state IN (
                              'waiting', 'screening', 'optimising', 'choosing-cell', 'replaying',
                              'walking-forward', 'judging', 'done', 'refused', 'failed', 'stopped'
                          )),
    ended_at_step         TEXT    CHECK (ended_at_step IS NULL OR ended_at_step IN (
                              'screen', 'optimise', 'choose', 'replay', 'walkforward', 'judge'
                          )),
    reason_word           TEXT,
    reason_detail         TEXT,
    chosen_variation_id   INTEGER,
    chosen_ticker         TEXT,
    chosen_at             INTEGER,
    stop_requested        INTEGER NOT NULL DEFAULT 0 CHECK (stop_requested IN (0,1)),
    retry_requested       INTEGER NOT NULL DEFAULT 0 CHECK (retry_requested IN (0,1)),
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL,
    CHECK (screen_start_ts < optimise_start_ts AND optimise_start_ts < walkforward_start_ts
           AND walkforward_start_ts < walkforward_end_ts),
    CHECK ((state IN ('refused','failed','stopped')) = (ended_at_step IS NOT NULL)),
    CHECK ((state IN ('refused','failed')) = (reason_word IS NOT NULL)),
    CHECK ((reason_word IS NULL) = (reason_detail IS NULL)),
    CHECK (reason_detail IS NULL OR length(reason_detail) > 0),
    CHECK ((chosen_variation_id IS NULL) = (chosen_ticker IS NULL)
           AND (chosen_ticker IS NULL) = (chosen_at IS NULL)),
    CHECK (state NOT IN ('replaying','walking-forward','judging','done') OR chosen_variation_id IS NOT NULL),
    CHECK (duel_chain_id IS NULL OR duel_chain_id <> id),
    UNIQUE (strategy_id, strategy_version, calendar_id)
);
CREATE TABLE IF NOT EXISTS research_chain_transitions (
    id          INTEGER PRIMARY KEY,
    chain_id    INTEGER NOT NULL,                -- soft ref to research_chains.id
    from_state  TEXT,                            -- NULL on the creation row only
    to_state    TEXT    NOT NULL,
    reason_word TEXT,                            -- set exactly when to_state is refused or failed
    detail      TEXT,                            -- set exactly when reason_word is
    actor       TEXT    NOT NULL CHECK (actor IN ('runner', 'page')),
    created_at  INTEGER NOT NULL,
    CHECK ((to_state IN ('refused','failed')) = (reason_word IS NOT NULL)),
    CHECK ((reason_word IS NULL) = (detail IS NULL)),
    CHECK (detail IS NULL OR length(detail) > 0)
);
CREATE INDEX IF NOT EXISTS ix_chain_transitions_chain ON research_chain_transitions(chain_id, id);

-- research_trial_families (lane GV2-7b, DECISIONS #445): one row per generator v2 search, the record of the family of
-- strategies that search tried (a theory, a stock group, the screen window). Append-only: a re-run of the same theory
-- on the same key adds a row and family_for() reads the newest. DERIVED DATA (DDIA ch 1, systems of record and derived
-- data): every number here can be rebuilt from the run's trials and returns artifacts, which stay the record; the
-- row is kept so the judge reads K and V without opening those files. Written in one transaction with its checks
-- (DDIA ch 8). No foreign key to research_runs, like research_trials: the record of how many things were tried must
-- outlive the run's row. The key (member_set_sha256, window_start_ts, window_end_ts) names the data the family drew on.
-- n_in_onc, k_effective, onc_cap, cap_bound and v are NULL exactly when reason is 'too-few-trials' (no clustering ran).
CREATE TABLE IF NOT EXISTS research_trial_families (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL UNIQUE,   -- soft ref to research_runs.id: the search run that made the family
    theory_name         TEXT    NOT NULL,
    group_label         TEXT    NOT NULL,
    member_set_sha256   TEXT    NOT NULL,
    window_start_ts     INTEGER NOT NULL,
    window_end_ts       INTEGER NOT NULL,
    n_trials            INTEGER NOT NULL,
    n_in_onc            INTEGER,
    k_effective         INTEGER,
    onc_cap             INTEGER,
    cap_bound           INTEGER CHECK (cap_bound IS NULL OR cap_bound IN (0, 1)),
    v                   REAL,
    selected_frequency  REAL    NOT NULL,
    reason              TEXT    CHECK (reason IS NULL OR reason = 'too-few-trials'),
    trials_sha256       TEXT    NOT NULL,
    returns_sha256      TEXT    NOT NULL,
    created_at          INTEGER NOT NULL,
    CHECK (reason IS NULL OR (n_in_onc IS NULL AND k_effective IS NULL AND onc_cap IS NULL AND cap_bound IS NULL AND v IS NULL)),
    CHECK (reason IS NOT NULL OR (n_in_onc IS NOT NULL AND k_effective IS NOT NULL AND onc_cap IS NOT NULL
                                  AND cap_bound IS NOT NULL AND v IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_trial_families_key
    ON research_trial_families(member_set_sha256, window_start_ts, window_end_ts, id);

-- research_trial_family_unions (lane GV3-FU2, call 37, AFML p. 275): the merged family of several theories that searched the same
-- stocks and screen window, one row per run_family_union job. DERIVED DATA (DDIA ch 1): every number can be rebuilt from the runs'
-- trials and returns artifacts, which stay the record; kept so the judge reads the merged N, K and V without opening them.
-- Append-only and idempotent on job_id (UNIQUE), written in one transaction with its checks (DDIA ch 8). No foreign key, like
-- research_trial_families: the record of how many things were tried outlives a run's row. run_ids_json is the sorted run ids the
-- union covers; family_for() uses a union only when it holds the newest run of every theory on the key. The nullable columns are NULL
-- exactly when reason is 'too-few-trials' (no clustering ran), as in research_trial_families.
CREATE TABLE IF NOT EXISTS research_trial_family_unions (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL UNIQUE,   -- soft ref to research_jobs.id: the run_family_union job that made it
    member_set_sha256   TEXT    NOT NULL,
    window_start_ts     INTEGER NOT NULL,
    window_end_ts       INTEGER NOT NULL,
    run_ids_json        TEXT    NOT NULL,
    n_trials            INTEGER NOT NULL,
    n_in_onc            INTEGER,
    k_effective         INTEGER,
    onc_cap             INTEGER,
    cap_bound           INTEGER CHECK (cap_bound IS NULL OR cap_bound IN (0, 1)),
    v                   REAL,
    reason              TEXT    CHECK (reason IS NULL OR reason = 'too-few-trials'),
    created_at          INTEGER NOT NULL,
    CHECK (reason IS NULL OR (n_in_onc IS NULL AND k_effective IS NULL AND onc_cap IS NULL AND cap_bound IS NULL AND v IS NULL)),
    CHECK (reason IS NOT NULL OR (n_in_onc IS NOT NULL AND k_effective IS NOT NULL AND onc_cap IS NOT NULL
                                  AND cap_bound IS NOT NULL AND v IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_trial_family_unions_key
    ON research_trial_family_unions(member_set_sha256, window_start_ts, window_end_ts, id);
"""


# =============================================================
# Settings seed layout (M2 brief F.1)
#
# To add a NEW SETTING within an existing scope:
#
#   1. Find the right scope_entry in _DEFAULT_SETTINGS_LAYOUT.
#   2. Append to its `settings` list:
#        {
#            'key': 'crypto.bybit',
#            'value': 0.001,
#            'value_type': 'number',
#            'unit': 'percent',
#            'label': 'Crypto (Bybit)',
#            'description': 'Commission applied per Bybit crypto trade.',
#        }
#   3. `sudo systemctl restart newsbot-dashboard.service`
#   4. The new setting now appears in /research/settings, correctly grouped,
#      formatted, with its description. No frontend code change required.
#
# To add a NEW SCOPE:
#
#   1. Append a new top-level entry to _DEFAULT_SETTINGS_LAYOUT:
#        {
#            'scope': 'execution',
#            'title': 'Execution',
#            'description': '...',
#            'display_order': 25,        # slot between slippage (20) and paths (30)
#            'settings': [ ... ],
#        }
#   2. Restart service. New section renders automatically in order.
#
# To add a NEW UNIT TYPE:
#
#   1. Backend: just use the new string in `unit` field; no constraint enforced.
#   2. Frontend: add a `case 'your_unit':` branch in src/lib/settingsFormat.ts.
# =============================================================
#
# `value_type` here mirrors the DB column name (kept stable from M2 part 1).
# The API response aliases it to `type` so the frontend can use the cleaner name.
_DEFAULT_SETTINGS_LAYOUT: list[dict] = [
    {
        'scope': 'fees',
        'title': 'Fees',
        'description': 'Trading commissions applied per trade in backtests.',
        'display_order': 10,
        'settings': [
            {
                'key': 'crypto.default',
                'value': 0.001,
                'value_type': 'number',
                'unit': 'percent',
                'label': 'Crypto (default)',
                'description': 'Percentage commission applied per crypto trade.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [0.00075, 0.001] (VIP-0 taker, BNB-discount floor to regular ceiling).
                'bounds': {'type': 'number', 'min': 0.00075, 'max': 0.001},
            },
            {
                'key': 'equities.default',
                'value': 0,
                'value_type': 'number',
                'unit': 'percent',
                'label': 'Equities (default)',
                'description': 'Percentage commission applied per equity trade.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound {0} — Alpaca retail equities are commission-free (a closed [0,0]).
                'bounds': {'type': 'number', 'min': 0, 'max': 0},
            },
            {
                'key': 'equities.fixed',
                'value': 0,
                'value_type': 'number',
                'unit': 'currency',
                'label': 'Equities (fixed per-share)',
                'description': 'Flat per-share fee applied to equity trades, on top of the percentage.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound {0}: no fixed per-share ticket fee while commission-free.
                'bounds': {'type': 'number', 'min': 0, 'max': 0},
            },
            {
                # ibkr_costs unit (night nine, 2026-09-05): IBKR Pro Fixed pricing, fetched 2026-09-04.
                # Provenance lives in docs/research_lab/parameter_provenance.md under the matching
                # `fees.ibkr_fixed.*` heading (AC-14); bounds are pinned CLOSED (min == max) below on
                # all three new rows, because these are IBKR's own published prices, not a dial this
                # lab widens.
                'key': 'ibkr_fixed.per_share',
                'value': 0.005,
                'value_type': 'number',
                'unit': 'currency',
                'label': 'IBKR Pro Fixed (per share)',
                'description': 'Per-share commission under IBKR Pro Fixed pricing, applied before the per-order minimum and the 1% cap.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                'bounds': {'type': 'number', 'min': 0.005, 'max': 0.005},
            },
            {
                'key': 'ibkr_fixed.minimum_per_order',
                'value': 1.00,
                'value_type': 'number',
                'unit': 'currency',
                'label': 'IBKR Pro Fixed (per-order minimum)',
                'description': 'Minimum commission per order under IBKR Pro Fixed pricing, applied when the per-share rate would otherwise charge less.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                'bounds': {'type': 'number', 'min': 1.00, 'max': 1.00},
            },
            {
                'key': 'ibkr_fixed.maximum_pct_of_trade_value',
                'value': 0.01,
                'value_type': 'number',
                'unit': 'percent',
                'label': 'IBKR Pro Fixed (per-order maximum)',
                'description': 'Maximum commission per order under IBKR Pro Fixed pricing, expressed as a fraction of trade value, applied when the per-share rate would otherwise charge more.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                'bounds': {'type': 'number', 'min': 0.01, 'max': 0.01},
            },
            {
                # FEE1a (2026-10-16): Alpaca's own pass-through regulatory fees, Brokerage Fee Schedule revised
                # 2026-10-12. Provenance under the matching `fees.alpaca.*` headings in
                # docs/research_lab/parameter_provenance.md; bounds are pinned CLOSED (min == max) because these are
                # Alpaca's published figures, not a dial this lab widens (the ibkr_fixed.* shape above).
                'key': 'alpaca.sec_fee_rate',
                'value': 0.0000206,
                'value_type': 'number',
                'unit': 'percent',
                'label': 'Alpaca SEC fee (fraction of sale value)',
                'description': 'Alpaca passes on the SEC fee on every sale: this fraction of the trade value, summed per day for the account and rounded up to the cent.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                'bounds': {'type': 'number', 'min': 0.0000206, 'max': 0.0000206},
                'ruled_by': '#459',
            },
            {
                'key': 'alpaca.taf_per_share',
                'value': 0,
                'value_type': 'number',
                'unit': 'currency',
                'label': 'Alpaca FINRA TAF (per share sold)',
                'description': 'Alpaca passes on the FINRA trading activity fee on sales; the schedule figure for this account is zero a share.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                'bounds': {'type': 'number', 'min': 0, 'max': 0},
                'ruled_by': '#459',
            },
            {
                'key': 'alpaca.cat_per_share',
                'value': 0.000003,
                'value_type': 'number',
                'unit': 'currency',
                'label': 'Alpaca CAT fee (per share)',
                'description': 'Alpaca passes on the consolidated audit trail fee on every share bought or sold, summed per day for the account and rounded up to the cent.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                'bounds': {'type': 'number', 'min': 0.000003, 'max': 0.000003},
                'ruled_by': '#459',
            },
        ],
    },
    {
        'scope': 'slippage',
        'title': 'Slippage',
        'description': 'Assumed price slippage per trade in backtests.',
        'display_order': 20,
        'settings': [
            {
                'key': 'crypto.default',
                'value': 0.0005,
                'value_type': 'number',
                'unit': 'percent',
                'label': 'Crypto (default)',
                'description': 'Assumed slippage applied per crypto trade.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [0.0001, 0.0015] (tight half-spread to the pre-impact-tier cap).
                'bounds': {'type': 'number', 'min': 0.0001, 'max': 0.0015},
            },
            {
                'key': 'equities.default',
                'value': 0.0003,
                'value_type': 'number',
                'unit': 'percent',
                'label': 'Equities (default)',
                'description': 'Assumed slippage applied per equity trade.',
                'overridable': True,  # a per-strategy cost dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [0.0001, 0.001] (large-cap half-spread to the small/mid-cap cap).
                'bounds': {'type': 'number', 'min': 0.0001, 'max': 0.001},
            },
        ],
    },
    {
        # Lane WF (6.2b, DECISIONS #392 ruling 4): the walk-forward stage's own three sizing
        # values, frozen by Tamer's own ruling rather than tuned by anyone in this lab (the same
        # "ruled, never a dial" shape ibkr_fixed.* uses above, under 'fees'). All three are
        # overridable: False for the same reason those rows are closed-bound: #392's own words are
        # "configured store values ... never a literal in code", not a per-strategy knob a sweep
        # or a settings-page edit may widen. Each key owes a parameter_provenance.md entry (the
        # _check_parameter_provenance cross-check, matching '## walkforward.*' headings).
        'scope': 'walkforward',
        'title': 'Walk-forward slicing',
        'description': 'The walk-forward stage\'s own in-sample/out-of-sample window scheme, frozen by ruling.',
        'display_order': 30,
        'settings': [
            {
                'key': 'in_sample_bars',
                'value': 84,
                'value_type': 'number',
                'unit': 'bars',
                'label': 'In-sample bars',
                'description': 'The length of each walk-forward fold\'s in-sample (training) window, in bars.',
                'overridable': False,  # #392 ruling 4: a configured store value, never a per-strategy dial
                # lane RV (docs/units/RV/criteria.md AC-1): the ruling that fixes this row's value, read
                # by the API and shown on the page as "Set by ruling #392". A row whose value is not
                # fixed by a ruling carries no ruled_by key at all.
                'ruled_by': '#392',
            },
            {
                'key': 'out_of_sample_bars',
                'value': 25,
                'value_type': 'number',
                'unit': 'bars',
                'label': 'Out-of-sample bars',
                'description': 'The length of each walk-forward fold\'s out-of-sample (test) window, in bars.',
                'overridable': False,  # #392 ruling 4: a configured store value, never a per-strategy dial
                'ruled_by': '#392',  # lane RV: same ruling as in_sample_bars above
            },
            {
                'key': 'anchoring',
                'value': 'rolling',
                'value_type': 'string',
                'unit': 'scheme',
                'label': 'Anchoring',
                'description': 'rolling (a fixed-length in-sample window that slides forward) or anchored (a fixed start that grows each fold).',
                'overridable': False,  # #392 ruling 4: a configured store value, never a per-strategy dial
                'ruled_by': '#392',  # lane RV: same ruling as in_sample_bars above
            },
        ],
    },
    # Phase-2 Unit 4 (A1c-005): the 'paths' scope (its only setting was 'data_root') is REMOVED.
    # data_root was dead — the worker reads RESEARCH_DATA_ROOT env / a file-relative root
    # (hard_delete_dataset.py), never this DB setting, so UI edits silently no-op. The existing
    # prod row is deleted at boot by the guarded cleanup DELETEs in init_db (after the seed).
    {
        # M4.5 fold (DECISIONS #161 R1): the shared event-strategy method scope. RENAMED from 'newsbot_v3'
        # to 'event_strategy' — a settings scope names a stable method/concept, never a strategy display name.
        # The shared EventStrategy pipeline reads each dial via get_setting(settings, self.settings_scope, key)
        # (EventStrategy.run, event_strategy.py; peer_window_before/peer_missingness via NewsbotV3.feature_defs,
        # which route through self.settings_scope). Each numeric is a user-set risk dial with a neutral default
        # (NOT a derived value); every key here owes a parameter_provenance.md entry (the _check_parameter_provenance
        # cross-check, which reads the matching '## event_strategy.*' headings renamed in lockstep). peer_window_after
        # is deliberately ABSENT (BN-F1: inert under the no-lookahead cap, must not be a layout key).
        'scope': 'event_strategy',
        'title': 'Event-strategy dials',
        'description': 'Risk and feature dials for the shared event strategy (analyst ratings and 13D filings).',
        'display_order': 40,
        'settings': [
            {
                'key': 'profit_take_mult',
                'value': 2.0,
                'value_type': 'number',
                'unit': 'multiple',
                'label': 'Profit-take width',
                'description': 'Upper barrier distance, in multiples of the volatility estimate. Higher means a wider profit target.',
                'overridable': True,  # a per-strategy risk dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [0.5, 4.0] vol units (below ~0.5 sits inside daily noise; above ~4 the time-out resolves first).
                'bounds': {'type': 'number', 'min': 0.5, 'max': 4.0},
            },
            {
                'key': 'stop_loss_mult',
                'value': 2.0,
                'value_type': 'number',
                'unit': 'multiple',
                'label': 'Stop-loss width',
                'description': 'Lower barrier distance, in multiples of the volatility estimate. Higher means a wider stop.',
                'overridable': True,  # a per-strategy risk dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [0.5, 4.0] vol units (same vol-scaling regime as the profit-take multiple).
                'bounds': {'type': 'number', 'min': 0.5, 'max': 4.0},
            },
            {
                'key': 'vertical_horizon',
                'value': 10,
                'value_type': 'number',
                'unit': 'bars',
                'label': 'Holding horizon',
                'description': 'Maximum holding period after entry, in daily bars, before the trade is timed out.',
                'overridable': True,  # a per-strategy risk dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [2, the run window] integer bars. min:2 is STRICTER than the runtime
                # floor <1 (triple_barrier.py:122) — it encodes the prose "a one-bar hold cannot path-resolve
                # a barrier", not the runtime guard; do not loosen to 1. No static max: the upper is the run
                # window, a per-run runtime value the triple-barrier walk clamps to last_pos, so an over-large
                # value degrades, never crashes.
                'bounds': {'type': 'integer', 'min': 2},
            },
            {
                'key': 'vol_window',
                'value': 20,
                'value_type': 'number',
                'unit': 'bars',
                'label': 'Volatility window',
                'description': 'Lookback length, in daily bars, for the volatility estimate the barriers are scaled by.',
                'overridable': True,  # a per-strategy risk dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [2, the run window] integer bars. min:2 to estimate a deviation; same
                # run-window runtime upper — an oversized window yields non-finite vol, caught and returned as
                # uncovered (triple_barrier.py), no crash. The <2 is caught here at write time.
                'bounds': {'type': 'integer', 'min': 2},
            },
            {
                'key': 'position_fraction',
                'value': 0.2,
                'value_type': 'number',
                'unit': 'fraction',
                'label': 'Position fraction',
                'description': 'Fraction of starting cash committed per trade. With the cap below, set to one over the cap keeps the book fully invested without leverage.',
                'overridable': True,  # a per-strategy sizing dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound (0, 1] — at or below 0 sizes nothing; above 1 a single trade exceeds
                # starting cash (leverage), rejected by the Sizer's `0 < f <= 1` guard (event_pipeline.py:89).
                'bounds': {'type': 'number', 'min': 0, 'exclusiveMin': True, 'max': 1},
            },
            {
                'key': 'max_concurrent',
                'value': 5,
                'value_type': 'number',
                'unit': 'count',
                'label': 'Max concurrent positions',
                'description': 'Most positions open at once. When more events fire than slots, the earliest by knowledge time are taken first.',
                'overridable': True,  # a per-strategy concurrency dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [1, ∞) integer. No max = unbounded above (a large cap is non-binding);
                # the crash value <1 is caught by min, matching the `max_concurrent < 1` guard in
                # select_and_place (event_pipeline.py:252-253).
                'bounds': {'type': 'integer', 'min': 1},
            },
            {
                'key': 'peer_window_before',
                'value': 2592000,
                'value_type': 'number',
                'unit': 'seconds',
                'label': 'Peer lookback window',
                'description': 'How far back, in seconds, to look for peer events when measuring peer breadth (default thirty days).',
                'overridable': True,  # a per-strategy feature dial (explicit, matching the bounds-set invariant)
                # 3b B2.1: prose bound [0, ∞) integer seconds. No max — a window longer than history just
                # finds no peers; a negative is caught by min, matching the `window_before < 0` guard
                # (features.py:203).
                'bounds': {'type': 'integer', 'min': 0},
            },
            {
                'key': 'regime_mode',
                'value': 'mark',
                'value_type': 'string',
                'unit': 'mode',
                'label': 'Regime mode',
                'description': 'How to treat the 2024-02-05 13D rule-change boundary: mark (run across it, flag it), split (report per regime), or restrict (keep one regime).',
                # 3b B2.1: an UNCONDITIONAL categorical — re-enabled for override now that structured bounds
                # exist. The {allowed} bound is the enforcement system of record (prose `regime.py:93`);
                # an out-of-domain string is rejected at write time, so the worker never sees a bad value.
                'overridable': True,
                'bounds': {'allowed': ['split', 'restrict', 'mark']},
            },
            {
                'key': 'regime_select',
                'value': '',
                'value_type': 'string',
                'unit': 'regime',
                'label': 'Restrict to regime',
                'description': 'Which regime to keep when the mode is restrict: pre_2024_02_05 or post_2024_02_05. Leave blank for no restriction.',
                # Conditional dial (valid set depends on regime_mode=restrict): a flat {allowed} cannot express it,
                # so it STAYS non-overridable this unit — a named follow-on (DECISIONS #161 R3 / 3b B2.1 SF2).
                'overridable': False,
            },
            {
                'key': 'peer_missingness',
                'value': 'zero',
                'value_type': 'string',
                'unit': 'mode',
                'label': 'Peer missingness',
                'description': 'How a peer with no event in the window counts: zero (counts as zero), null (voids the feature), or exclude (drops it from the denominator).',
                # 3b B2.1: an UNCONDITIONAL categorical — re-enabled for override with an {allowed} bound
                # (the enforcement system of record; prose `features.py:120`). An out-of-domain string is
                # rejected at write time.
                'overridable': True,
                'bounds': {'allowed': ['zero', 'null', 'exclude']},
            },
        ],
    },
]


def serialize_value(value, value_type: str) -> str:
    """Convert a typed Python value to the TEXT form stored in research_settings.value.

    Matches the existing DB convention: numbers as their `str()` repr,
    strings raw, booleans as 'true'/'false', json via json.dumps.
    """
    if value_type == 'string':
        return value if isinstance(value, str) else str(value)
    if value_type == 'number':
        # Preserve integer form when the value is integral (e.g. 0 stays '0',
        # matching the seed data shipped in part 1).
        if isinstance(value, bool):  # bool is an int in Python — guard first
            raise TypeError(f"value_type=number got bool: {value!r}")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return repr(value)
        # String coming in (e.g. from PUT body fallthrough) — pass through; validator handles bad input
        return str(value)
    if value_type == 'boolean':
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, str) and value in ('true', 'false'):
            return value
        raise TypeError(f"value_type=boolean got {value!r}")
    if value_type == 'json':
        if isinstance(value, str):
            json.loads(value)  # validate; raise if invalid
            return value
        return json.dumps(value)
    raise ValueError(f"unsupported value_type: {value_type!r}")


def parse_value(text, value_type: str):
    """Convert the TEXT form stored in research_settings.value back to its typed Python value."""
    if text is None:
        return None
    if value_type == 'string':
        return text
    if value_type == 'number':
        try:
            # Prefer int when the stored text round-trips through int cleanly.
            if isinstance(text, (int, float)):
                return text
            if '.' in text or 'e' in text or 'E' in text:
                return float(text)
            return int(text)
        except (ValueError, TypeError):
            # Tolerate bad rows rather than crash the response; surface the raw text.
            return text
    if value_type == 'boolean':
        return text == 'true'
    if value_type == 'json':
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
    return text


def _add_column_if_missing(conn, table: str, column: str, column_def: str) -> None:
    """Add a column via ALTER TABLE; no-op if it already exists.

    SQLite ALTER TABLE ADD COLUMN errors if the column exists, so we
    check PRAGMA table_info first.
    """
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}  # row[1] is the column name
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


def _rename_column_if_needed(conn, table: str, old: str, new: str) -> None:
    """Rename a column via ALTER TABLE; no-op once it is already renamed.

    Idempotent (DECISIONS #7): only renames when the old name is present and the
    new one is absent. A fresh DB is created with the new name by _DDL, so this
    no-ops there; an already-migrated DB (e.g. prod from brief A) still has the
    old name and gets renamed once. SQLite RENAME COLUMN needs 3.25.0+ (server
    and laptop both far newer).
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # current column names
    if old in cols and new not in cols:  # pre-rename state only
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def _drop_research_runs_if_env_notnull(conn) -> None:
    """Drop research_runs when its env columns are still NOT NULL, so _DDL recreates them nullable.

    Brief A created research_runs with vbt_version / python_version / build_provenance /
    hostname NOT NULL (a completion-only design). Brief E INSERTs the row at dispatch, when
    the container environment is not yet known, so those four must be nullable. SQLite cannot
    drop a NOT NULL constraint in place; the table has zero rows (brief E is its first writer),
    so the safe rebuild is DROP + recreate via _DDL. Idempotent: a no-op once the columns are
    nullable or the table is absent. Guarded on a zero row count so it can never lose real data.
    """
    info = conn.execute("PRAGMA table_info(research_runs);").fetchall()  # empty list if the table is absent
    if not info:
        return  # fresh DB — _DDL creates it nullable directly
    notnull = {row[1]: row[3] for row in info}  # column name -> notnull flag (1 = NOT NULL)
    env_cols = ("vbt_version", "python_version", "build_provenance", "hostname")
    if not any(notnull.get(c) for c in env_cols):
        return  # already nullable; nothing to do
    n = conn.execute("SELECT COUNT(*) FROM research_runs;").fetchone()[0]
    if n != 0:  # never discard real data — env nullability must be settled while the table is empty
        raise RuntimeError(f"research_runs has {n} rows; refusing to rebuild for nullable env columns")
    conn.execute("DROP TABLE research_runs;")  # children (stats/shares) are empty, so the FK permits the drop


def _seed_settings_layout(conn) -> None:
    """Seed scopes + settings + metadata from _DEFAULT_SETTINGS_LAYOUT.

    Per row:
      - INSERT OR IGNORE preserves any user edits to `value`.
      - UPDATE re-syncs the presentation fields (label/description/unit and
        scope title/description/display_order) — none of which are user-editable.
    Safe to run on every startup.
    """
    now = int(time.time())

    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:
        scope = scope_entry['scope']

        # ----- Scope row -----
        conn.execute(
            """
            INSERT OR IGNORE INTO research_setting_scopes
                (scope, title, description, display_order, seeded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope, scope_entry['title'], scope_entry['description'],
             scope_entry['display_order'], now),
        )
        conn.execute(
            """
            UPDATE research_setting_scopes
               SET title = ?, description = ?, display_order = ?
             WHERE scope = ?
            """,
            (scope_entry['title'], scope_entry['description'],
             scope_entry['display_order'], scope),
        )

        # ----- Settings rows -----
        for s in scope_entry['settings']:
            value_text = serialize_value(s['value'], s['value_type'])
            conn.execute(
                """
                INSERT OR IGNORE INTO research_settings
                    (scope, key, value, value_type, unit, label, description, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (scope, s['key'], value_text, s['value_type'],
                 s['unit'], s['label'], s['description'], now),
            )
            # Re-sync presentation fields. `value` and `value_type` are excluded —
            # value is user-editable, value_type doesn't change once shipped.
            conn.execute(
                """
                UPDATE research_settings
                   SET unit = ?, label = ?, description = ?
                 WHERE scope = ? AND key = ?
                """,
                (s['unit'], s['label'], s['description'], scope, s['key']),
            )


def get_default_value(scope: str, key: str):
    """Return the default value (typed) for a setting, or None if not in the layout."""
    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:
        if scope_entry['scope'] != scope:
            continue
        for s in scope_entry['settings']:
            if s['key'] == key:
                return s['value']
    return None


def setting_overridable(scope: str, key: str) -> bool:
    """Whether a setting is per-strategy overridable, from _DEFAULT_SETTINGS_LAYOUT (TERM M4.5 3a B1.5).

    Machine-level (paths.*) and the closed-set categorical dials carry overridable=False in the layout;
    everything else defaults True. A key absent from the layout defaults True (only built-ins are in the
    layout; the override write route separately rejects an unknown global key, so this fallback is for an
    authored strategy-namespaced key, which is its own strategy's and not a cross-strategy override).
    """
    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:
        if scope_entry['scope'] != scope:
            continue
        for s in scope_entry['settings']:
            if s['key'] == key:
                return bool(s.get('overridable', True))
    return True


def setting_ruled_by(scope: str, key: str) -> str | None:
    """The DECISIONS ruling number that fixes a setting's value, or None (lane RV, AC-1/AC-2).

    Read from _DEFAULT_SETTINGS_LAYOUT's own 'ruled_by' key, the same single-source-of-truth
    pattern setting_overridable and setting_bounds already use, so the API never carries a second,
    hand-typed list of which rows are ruled. A key absent from the layout, or a layout row with no
    'ruled_by' entry, both return None: no ruling fixes that value.
    """
    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:
        if scope_entry['scope'] != scope:
            continue
        for s in scope_entry['settings']:
            if s['key'] == key:
                return s.get('ruled_by')
    return None


def builtin_scopes() -> set[str]:
    """The scope names defined by _DEFAULT_SETTINGS_LAYOUT (the built-in domain — AD7 disjointness).

    The B1.4 write route reads this to reject an authored scope that collides with a built-in.
    """
    return {entry['scope'] for entry in _DEFAULT_SETTINGS_LAYOUT}


def setting_bounds(scope: str, key: str) -> dict | None:
    """The structured enforcement bound for a built-in setting, or None if the key has none (3b B2.1).

    Read from _DEFAULT_SETTINGS_LAYOUT. Every overridable built-in carries one (the B2.1 completeness
    invariant, asserted at boot by _check_overridable_bounds); a non-overridable key may carry None.
    """
    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:
        if scope_entry['scope'] != scope:
            continue
        for s in scope_entry['settings']:
            if s['key'] == key:
                return s.get('bounds')
    return None


def validate_bounds_spec(spec) -> None:
    """Raise ValueError unless `spec` is a well-formed structured bound (M4.5 3b B2.1 schema).

    Two shapes:
      numeric:     {"type": "number"|"integer", "min"?: n, "max"?: n,
                    "exclusiveMin"?: bool, "exclusiveMax"?: bool}  -- at least one of min/max present
      categorical: {"allowed": [<value>, ...]}  -- a non-empty list of allowed values

    A one-sided numeric bound (only min OR only max) IS well-formed: it is the honest encoding of a
    one-sided prose bound (an unbounded `∞` side, or a run-window-relative side enforced at run time).
    The check is presence + parseability, NOT two-sidedness.
    """
    if not isinstance(spec, dict):  # the spec must be a JSON object
        raise ValueError(f"bounds_spec must be a JSON object, got {type(spec).__name__}")
    if "allowed" in spec:  # categorical shape
        allowed = spec["allowed"]
        if not isinstance(allowed, list) or not allowed:  # a non-empty list of permitted values
            raise ValueError("categorical bounds_spec needs a non-empty 'allowed' list")
        # A categorical must NOT carry numeric-shape keys (a mixed spec is malformed, not silently ignored).
        numeric_keys = {"type", "min", "max", "exclusiveMin", "exclusiveMax"} & set(spec)
        if numeric_keys:
            raise ValueError(f"categorical bounds_spec must not carry numeric keys {sorted(numeric_keys)}")
        return
    btype = spec.get("type")  # numeric shape
    if btype not in ("number", "integer"):
        raise ValueError(
            "numeric bounds_spec needs type 'number' or 'integer' "
            "(or an 'allowed' list for a categorical bound)"
        )
    if "min" not in spec and "max" not in spec:  # at least one side must be bounded
        raise ValueError("numeric bounds_spec needs at least one of 'min'/'max'")
    for edge in ("min", "max"):  # each present endpoint must be a real number (a bool is not)
        if edge in spec and (isinstance(spec[edge], bool) or not isinstance(spec[edge], (int, float))):
            raise ValueError(f"numeric bounds_spec '{edge}' must be a number, got {spec[edge]!r}")
    for flag in ("exclusiveMin", "exclusiveMax"):  # the open-endpoint flags are booleans
        if flag in spec and not isinstance(spec[flag], bool):
            raise ValueError(f"bounds_spec '{flag}' must be a boolean")


def check_value_against_bounds(value, spec: dict) -> str | None:
    """Return a human error string if `value` violates the structured bound `spec`, else None (3b B2.2).

    Assumes `spec` is already well-formed (validate_bounds_spec). Inclusive endpoints by default;
    exclusiveMin/exclusiveMax open an endpoint; an absent min/max side is explicitly unbounded; type
    'integer' rejects a fractional value. Used by the override write (B2.2) and the authored-param
    self-consistency check (B2.3).
    """
    if "allowed" in spec:  # categorical: membership test
        if value not in spec["allowed"]:
            return f"{value!r} is not one of the allowed values {spec['allowed']}"
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):  # numeric: must be a number
        return f"{value!r} is not a number"
    if spec.get("type") == "integer" and float(value) != int(value):  # integer dial rejects a fraction
        return f"{value} must be a whole number"
    if "min" in spec:  # lower edge
        lo = spec["min"]
        if spec.get("exclusiveMin"):
            if value <= lo:
                return f"{value} must be greater than {lo}"
        elif value < lo:
            return f"{value} must be at least {lo}"
    if "max" in spec:  # upper edge
        hi = spec["max"]
        if spec.get("exclusiveMax"):
            if value >= hi:
                return f"{value} must be less than {hi}"
        elif value > hi:
            return f"{value} must be at most {hi}"
    return None


def set_setting_provenance(scope: str, key: str, source: str, mechanism: str, sensitivity: str,
                           bounds: str, bounds_spec: dict, authored_at: int) -> None:
    """Upsert a user-authored provenance row (M4.5 3b B1.4). The route validates the four prose fields,
    the well-formed bounds_spec, and the namespace guard BEFORE calling this; this is the DB write only."""
    spec_text = json.dumps(bounds_spec)  # store the structured spec as JSON text
    with transaction() as conn:
        conn.execute(
            "INSERT INTO research_setting_provenance "
            "  (scope, key, source, mechanism, sensitivity, bounds, bounds_spec, authored_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, key) DO UPDATE SET "
            "  source=excluded.source, mechanism=excluded.mechanism, "
            "  sensitivity=excluded.sensitivity, bounds=excluded.bounds, "
            "  bounds_spec=excluded.bounds_spec, authored_at=excluded.authored_at;",
            (scope, key, source, mechanism, sensitivity, bounds, spec_text, authored_at),
        )


def strategy_settings_resolved(strategy_id: int) -> list[dict]:
    """Per-strategy resolved settings for the workspace SETTINGS editor (TERM M4.5 3a B2 GET).

    For every global research_settings (scope, key): the resolved value (the strategy's override if it
    has one, else the inherited global), the source ('override'|'inherited'), the global value (the
    inherited reference), value_type/unit/label, the overridable flag, and source_note (3b). Authored-only
    override keys with no global (3b path) are appended honestly with source='override' and no inherited
    reference. The resolved VALUE here equals what _resolve_strategy_settings produces at dispatch — both
    overlay the override over the global by (scope, key) — so the editor shows exactly what a run would use.
    """
    with get_conn() as conn:
        globals_rows = conn.execute(
            "SELECT scope, key, value, value_type, unit, label FROM research_settings ORDER BY scope, key;"
        ).fetchall()
        ov_rows = conn.execute(
            "SELECT scope, key, value, value_type, source_note "
            "FROM research_strategy_settings WHERE strategy_id = ?;",
            (strategy_id,),
        ).fetchall()
    overrides = {(r["scope"], r["key"]): r for r in ov_rows}
    out: list[dict] = []
    seen: set = set()
    for g in globals_rows:
        sk = (g["scope"], g["key"])
        seen.add(sk)
        global_value = parse_value(g["value"], g["value_type"])
        ov = overrides.get(sk)
        if ov is not None:
            out.append({
                "scope": g["scope"], "key": g["key"],
                "value": parse_value(ov["value"], ov["value_type"]), "value_type": ov["value_type"],
                "unit": g["unit"], "label": g["label"], "source": "override",
                "global_value": global_value, "overridable": setting_overridable(g["scope"], g["key"]),
                "source_note": ov["source_note"],
            })
        else:
            out.append({
                "scope": g["scope"], "key": g["key"],
                "value": global_value, "value_type": g["value_type"],
                "unit": g["unit"], "label": g["label"], "source": "inherited",
                "global_value": global_value, "overridable": setting_overridable(g["scope"], g["key"]),
                "source_note": None,
            })
    for sk, ov in overrides.items():  # authored-only keys with no global (3b)
        if sk in seen:
            continue
        out.append({
            "scope": ov["scope"], "key": ov["key"],
            "value": parse_value(ov["value"], ov["value_type"]), "value_type": ov["value_type"],
            "unit": None, "label": None, "source": "override",
            "global_value": None, "overridable": True, "source_note": ov["source_note"],
        })
    return out


class ProvenanceCheckError(RuntimeError):
    """Raised at startup when parameter_provenance.md drifts from the settings layout (arch §19)."""


def _group_letter_from_provenance_body(body: str) -> str | None:
    """Extract the tier letter a '### Group' subsection body opens with, or None.

    Night-six Lane C (`DECISIONS.md` #304): every built-in provenance entry now carries a Group
    subsection naming one of six tier letters, A-F (job file PART 2). This helper mirrors the
    probes' own `extract_group_letter` (pt06/pt16 etc.) exactly, character for character, so a
    build that satisfies this function also satisfies theirs: strip markdown bold markers and
    surrounding whitespace, then require the first character to be A-F immediately followed by one
    of ',' ':' '.' '**', or by nothing but trailing whitespace to the end of the body. Returns None
    for an empty body, or for a body that does not open with a valid letter. Both callers only ever
    pass this function a body that DOES exist, so a None it returns is always a real Group entry that
    failed to parse, never a missing entry. `_check_parameter_provenance` calls it only once the
    '### Group' heading is confirmed present, and still treats that None as a boot-time failure
    (`ProvenanceCheckError`), since every documented entry must have a real letter; unchanged by
    night-seven Lane G (`DECISIONS.md` #316). `dispatch_sweep`'s frozen-parameter guard changed under
    that same unit: it now tests the raw provenance lookup for None itself, on `group_body`, BEFORE
    calling this function, so a key with no provenance entry at all (an unregistered strategy
    parameter, AC-3's own trap) is skipped with a bare `continue` and never reaches this function,
    because there is no tier to be frozen under. So a None this function DOES return to
    `dispatch_sweep` can only mean the Group body exists and failed to parse, and the guard now
    refuses that case fail-closed, counted under "UNREADABLE" in `SWEEP_GUARD_REFUSAL_COUNTERS`.
    """
    stripped = body.strip().lstrip("*").strip()  # drop markdown bold markers, then surrounding whitespace
    m = re.match(r"^([A-F])(?:[,:.]|\*\*|\s*$)", stripped)  # the same grammar the pt06/pt16 probes check
    return m.group(1) if m else None


def _layout_keys() -> set[str]:
    """Return the set of '{scope}.{key}' identifiers in _DEFAULT_SETTINGS_LAYOUT."""
    keys = set()  # accumulate every fully-qualified setting key
    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:  # walk each scope block
        for s in scope_entry['settings']:  # walk each setting within the scope
            keys.add(f"{scope_entry['scope']}.{s['key']}")  # join scope + key with a dot
    return keys


def provenance_entry(dotted_key: str) -> dict[str, str] | None:
    """The {source, mechanism, sensitivity, bounds} provenance for a '{scope}.{key}', or None.

    DB-first-then-markdown (M4.5 3b B1.2 / AD7, D5): a (scope, key) with a research_setting_provenance
    row (a USER-AUTHORED key) returns that row's four prose fields; a key with no DB row falls back to
    the parameter_provenance.md parse (a BUILT-IN key). One read API over two disjoint domains. Keys are
    lowercased to keep the existing JSON response shape (the ProvenanceInfo ⓘ drawer reads it unchanged).
    """
    # DB domain first: split the dotted key into (scope, key) the same way the layout-key construction
    # does (scope = the first dot-segment, key = the remainder — built-in keys carry dots inside the key
    # half, e.g. fees.crypto.default). A row here is an authored override of the markdown for that key.
    scope, _, key = dotted_key.partition(".")
    if key:  # a bare token with no dot is never a (scope, key) provenance entry
        with get_conn() as conn:
            row = conn.execute(
                "SELECT source, mechanism, sensitivity, bounds "
                "FROM research_setting_provenance WHERE scope=? AND key=?;",
                (scope, key),
            ).fetchone()
        if row is not None:
            return {name.lower(): row[name.lower()] for name in _REQUIRED_PROVENANCE_SECTIONS}

    # Markdown domain (the built-in keys): parsed fresh (small file; init_db already validates its shape).
    try:
        text = PROVENANCE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    sub = _parse_provenance_bodies(text).get(dotted_key)
    if sub is None:
        return None
    return {name.lower(): sub.get(name, "") for name in _REQUIRED_PROVENANCE_SECTIONS}


def _check_parameter_provenance() -> None:
    """Fail startup if provenance entries drift from the layout or miss a required subsection (arch §19).

    Scoped to the BUILT-IN (markdown) domain only (M4.5 3b B1.3 / D5): it validates parameter_provenance.md
    against the _DEFAULT_SETTINGS_LAYOUT keys (+ the documented exempt namespaces). It deliberately does NOT
    touch research_setting_provenance — user-authored keys are provenanced fail-loud at WRITE time (the
    B1.4 route refuses a row missing any of the four prose fields or a well-formed bounds_spec), so a
    boot-time markdown check over them would be both impossible (they are DB rows, not markdown) and
    redundant. The two domains are disjoint (the B1.4 namespace guard forbids an authored scope colliding
    with a built-in scope), so an authored key never reaches this built-in check.
    """
    try:
        text = PROVENANCE_PATH.read_text(encoding='utf-8')  # read the markdown source
    except FileNotFoundError as exc:  # file absent entirely is itself a build-time failure
        raise ProvenanceCheckError(
            f"parameter provenance file missing at {PROVENANCE_PATH}; "
            "every setting key needs an entry per arch §19"
        ) from exc
    documented = set(_parse_provenance_headings(text).keys())  # the keys actually documented as H2 headings
    expected = _layout_keys()  # the keys the settings layout requires
    missing = expected - documented  # layout keys with no provenance entry
    # M2.5 brief M: `event_counts.*` is a documented operational namespace per
    # spec brief v4 §11 brief M / §10 EL27 ("count-task, operational only,
    # does not flip Q3"). These entries record empirical magnitudes the
    # event-store operational stack measures (e.g.
    # `event_counts.sec_13d_total_estimate`); they are NOT tied to
    # `_DEFAULT_SETTINGS_LAYOUT` because they do not flow into any backtest
    # configuration. Permitting the namespace here keeps the spec brief's
    # file-location lock without forcing operational counters into the
    # runtime settings table.
    # M3.5 brief J2: `derived.*` is a second documented exempt namespace, mirroring `event_counts.*` above.
    # It holds run-time-DERIVED backtest parameters that are NOT settings-layout keys (so they have no
    # `_DEFAULT_SETTINGS_LAYOUT` row to match) but DO affect a backtest and so owe a provenance entry:
    # `derived.embargo_size` (the AFML Ch 7 embargo, derived from the label horizon) and
    # `derived.purge_horizon` (the per-label span itself, categorical). They are accepted here and still
    # validated for the four subsections below (spec brief §12 / §21 item 3; the J2 derived.* amendment).
    # Dashboard-improvements W8: `data_sources.*` is a third documented exempt namespace (mirroring
    # `event_counts.*`/`derived.*`), holding the per-(source,timeframe) earliest floors that affect a
    # fetch but are not `_DEFAULT_SETTINGS_LAYOUT` keys. Added to BOTH the exempt set HERE and the
    # four-subsection `checked_keys` union BELOW, so the floor entries are accepted AND validated.
    # M3.75 FUND-1 §11.1: `fundamentals.*` is a fourth documented exempt namespace (same pattern),
    # holding the categorical concept/form/scope/vintage/missingness/staleness defaults that affect a
    # backtest but are code constants and per-feature declaration fields, NOT settings-layout dials.
    # M5 Track C: `metric.*` is a fifth documented exempt namespace (same pattern), holding full-metrics
    # COMPUTED-metric DEFINITIONS that are not vbt-native and not settings-layout dials, but that we
    # defined ourselves and so owe a four-part provenance row: `metric.turnover` (the pinned one-way
    # order-based turnover formula, the turnover ruling) and `metric.benchmark_n_min` (the C-bench
    # minimum benchmark-overlap threshold). They do NOT flow into any backtest configuration (they shape
    # what the full-metrics page displays), so they have no `_DEFAULT_SETTINGS_LAYOUT` row to match;
    # accepted here AND validated for the four subsections below.
    # Night-five Lane A (2026-09-01 campaign, sizing unit brief Amendment 2): `sizing.` is a sixth
    # documented exempt namespace (same pattern), holding the position-sizing parameters
    # (risk_fraction, stop_multiple, the estimator selector, estimator_window, hard_cap_fraction, and
    # the deferred volatility_update_rule row) that AC-4 requires to be carried through the sweep
    # contract and optimised per combo, exactly like a strategy's own signal parameters. A
    # `_DEFAULT_SETTINGS_LAYOUT` row would be a global dial pinned outside the grid, which is what
    # AC-4 forbids, so these are run-time, per-combo backtest parameters with no settings-layout row
    # to match, following the `derived.*` precedent above. Accepted here AND validated for the four
    # subsections below.
    # Night-six Lane C (2026-09-03, `DECISIONS.md` #304 decision 4): `search.` is a seventh documented
    # exempt namespace (same pattern), holding the exits addendum's own grid-search ranges (the
    # trailing multiple, the profit ratio, the time limit and the shared estimator smoothing speed).
    # These are group-D provenance rows for numbers a sweep searches OVER, never a single settings-
    # layout dial, so there is no `_DEFAULT_SETTINGS_LAYOUT` row to match, following the `sizing.`
    # precedent immediately above. Accepted here AND validated for the four subsections below.
    # Set 9 night (2026-09-15, `DECISIONS.md` #400): `reserve.` is the eighth documented exempt namespace,
    # holding the reserve holdout's own group-C policy values (#315: the cut date and the exam window,
    # ruled by Tamer under #302 and #318, never searched, never a settings-layout dial), so lane RE's two
    # entries pass this gate without a `_DEFAULT_SETTINGS_LAYOUT` row. Same pattern as `search.` above.
    extra = {k for k in (documented - expected)
             if not k.startswith(("event_counts.", "derived.", "data_sources.", "fundamentals.", "metric.", "sizing.", "search.", "reserve."))}
    if missing or extra:  # the H2 set must equal the layout-key set exactly (modulo the documented namespace)
        raise ProvenanceCheckError(
            f"provenance/layout mismatch in {PROVENANCE_PATH}: "
            f"missing entries for {sorted(missing)}; unexpected entries {sorted(extra)}"
        )
    sections = _parse_provenance_headings(text)  # re-read the heading map to inspect each entry's subsections
    # All H2 entries — settings-layout and `event_counts.*` alike — must carry
    # the four mandatory H3 subsections so the renderer / tooltip surface
    # always has somewhere to read from.
    checked_keys = sorted(
        expected
        | {k for k in documented if k.startswith("event_counts.")}
        | {k for k in documented if k.startswith("derived.")}  # M3.5 J2: validate the derived.* entries' four subsections too
        | {k for k in documented if k.startswith("data_sources.")}  # W8: validate the data_sources.* floor entries too
        | {k for k in documented if k.startswith("fundamentals.")}  # M3.75 FUND-1: validate the fundamentals.* entries' four subsections too
        | {k for k in documented if k.startswith("metric.")}  # M5 Track C: validate the metric.* (turnover, benchmark_n_min) four subsections too
        | {k for k in documented if k.startswith("sizing.")}  # Night-five Lane A: validate the sizing.* (risk_fraction, stop_multiple, estimator, estimator_window, hard_cap_fraction) four subsections too
        | {k for k in documented if k.startswith("search.")}  # Night-six Lane C: validate the search.* (trailing multiple, profit ratio, time limit, smoothing speed) four subsections too
        | {k for k in documented if k.startswith("reserve.")}  # Set 9 night, lane RE (#400): validate the reserve.* (cut date, exam window) four subsections too
    )
    for key in checked_keys:
        gaps = [s for s in _REQUIRED_PROVENANCE_SECTIONS if s not in sections[key]]  # required H3s that are absent
        if gaps:  # a present H2 must carry all four H3 subsections
            raise ProvenanceCheckError(
                f"provenance entry '## {key}' in {PROVENANCE_PATH} "
                f"is missing required subsection(s): {gaps}"
            )
    # Night-six Lane C (`DECISIONS.md` #304, job file PART 2/3): every checked entry ALSO carries a
    # '### Group' subsection naming exactly one of the six tier letters A-F, so a sweep-time refusal
    # (see the guard in dispatch_sweep below) always has a letter to read. This is a SEPARATE loop
    # from the four-subsection check above, on purpose: `_REQUIRED_PROVENANCE_SECTIONS` is imported
    # from provenance_parse.py, shared with the operations page's own provenance file, whose keys
    # carry no group-letter concept at all, so 'Group' is never folded into that shared tuple.
    # `sections` (from `_parse_provenance_headings` above) already captures ANY '### ' heading, Group
    # included, so its presence is checked first, cheaply, before re-parsing bodies for the letter
    # itself. Three lifecycle states, each proven by its own probe: absent entirely (pt07), present
    # but blank (pt08), present but naming a letter outside A-F (pt09); a fourth, present-and-valid,
    # is the passing case every entry in the real register reaches today (pt10).
    bodies = _parse_provenance_bodies(text)  # re-parse for BODY text; `sections` above proves presence only
    for key in checked_keys:
        if "Group" not in sections[key]:  # lifecycle state 1: no '### Group' heading at all (pt07)
            raise ProvenanceCheckError(
                f"provenance entry '## {key}' in {PROVENANCE_PATH} carries no '### Group' subsection; "
                "`DECISIONS.md` #304 requires every entry to name a tier letter (A, B, C, D, E or F)"
            )
        group_body = bodies.get(key, {}).get("Group", "")  # the raw text under this entry's '### Group'
        letter = _group_letter_from_provenance_body(group_body)  # shared with dispatch_sweep's guard below
        if letter is None:  # lifecycle states 2 (blank, pt08) and 3 (unknown letter, pt09) land here too
            raise ProvenanceCheckError(
                f"provenance entry '## {key}' in {PROVENANCE_PATH} has a '### Group' subsection that "
                f"is empty or does not open with a valid tier letter (got {group_body!r}); "
                "`DECISIONS.md` #304 requires one of A, B, C, D, E or F"
            )


def provenance_check_boot_fact() -> dict[str, bool | int | None]:
    """The one fact OPS-1's silent-bypass monitor (boot_provenance_check_ran) reads.

    `passed` reflects the MOST RECENT check in this process, not whether one ever succeeded, and
    the difference is the whole point. init_db is idempotent and can run more than once; a later
    call whose check FAILS clears this back to False even if an earlier call passed, because the
    fact is cleared before the check runs rather than only written after it succeeds. `checked_at`
    is that most recent pass's unix-seconds timestamp, or None when the fact is not standing.

    Both stay at their "never ran" default if a refactor drops the call from init_db, or rebinds the
    import so nothing runs -- the dashboard would boot happily with nothing verifying provenance,
    and this is the one fact that can catch that, since a silent no-op raises nothing for anything
    else to notice.

    This paragraph was corrected by OPS-1's own code-quality gate: it previously said `passed` was
    True "once ... at least once in this process", which is exactly the one-way-latch behaviour the
    same fold had just removed one function away. Two passages stated one fact and only one of them
    was updated, which is the fold-protocol failure the contract names by name.
    """
    return {
        "passed": _provenance_check_passed_at is not None,  # False until a pass has been recorded
        "checked_at": _provenance_check_passed_at,  # unix seconds of the last pass, or None
    }


class OverridableBoundsError(RuntimeError):
    """Raised at startup when the overridable-set and the structured-bounds-set diverge (3b B2.1 / SF3)."""


def _check_overridable_bounds() -> None:
    """Fail startup unless the structured-bounds-set equals the overridable-set exactly (M4.5 3b B2.1).

    Every `overridable: true` built-in must carry a WELL-FORMED structured `bounds`, and no
    `overridable: false` one may carry a `bounds`. A missed transcription then fails the DEPLOY, not a
    user's first override attempt (SF3). Well-formed = parses to the B2.1 schema (validate_bounds_spec);
    a one-sided numeric bound IS well-formed (presence + parseability, not two-sidedness).
    """
    for scope_entry in _DEFAULT_SETTINGS_LAYOUT:  # walk every built-in setting
        for s in scope_entry['settings']:
            ident = f"{scope_entry['scope']}.{s['key']}"  # the fully-qualified key, for the error
            overridable = bool(s.get('overridable', True))  # default True (the layout convention)
            bounds = s.get('bounds')
            if overridable:  # an overridable setting MUST carry a well-formed bound
                if bounds is None:
                    raise OverridableBoundsError(
                        f"overridable setting '{ident}' has no structured 'bounds' (3b B2.1): every "
                        "overridable built-in must carry an enforceable bound"
                    )
                try:
                    validate_bounds_spec(bounds)  # presence + parseability
                except ValueError as exc:
                    raise OverridableBoundsError(
                        f"setting '{ident}' has a malformed structured 'bounds': {exc}"
                    ) from exc
            elif bounds is not None:  # a non-overridable setting must NOT carry one (keep the sets equal)
                raise OverridableBoundsError(
                    f"non-overridable setting '{ident}' carries a structured 'bounds' (3b B2.1): the "
                    "bounds-set must equal the overridable-set exactly"
                )


def _ensure_research_shared_runs_polymorphic(conn) -> None:
    """M2.5 brief F: migrate research_shared_runs to the polymorphic (run|batch) shape.

    Spec brief §6.7 / §3.9 R3. The original M3 K.6 table has `run_id INTEGER NOT NULL`
    plus a FK to research_runs(id). M2.5 needs the same share-row machinery to also
    point at `research_event_batches(batch_id)` rows in events.db, so the table is
    rebuilt with both run_id and batch_id nullable plus a table-level CHECK that
    exactly one is non-null.

    Idempotent: detection via PRAGMA table_info — if `batch_id` exists AND `run_id`
    is nullable, the rebuild has already run and we no-op. Otherwise the rebuild
    runs in its own BEGIN IMMEDIATE inside this helper (R3 per §3.9 / MO1 — runs
    separately from R1+R2 because PRAGMA foreign_keys cannot change mid-transaction
    and SQLite forbids nested BEGINs).

    Why a rebuild vs. column add: SQLite cannot drop NOT NULL on an existing column
    or add a CHECK that references multiple columns in-place; both require the
    CREATE/INSERT-SELECT/DROP/RENAME dance the M3 DECISIONS #21 rebuild pattern uses.

    No FK on batch_id: research_event_batches lives in events.db, and SQLite cannot
    declare FKs across attached databases. Lineage is enforced in code (the brief F
    share-create helper checks the batch_id exists in events.db before INSERTing
    into research.db); same risk acceptance as §3.3 of arch v6.
    """
    info_rows = conn.execute("PRAGMA table_info(research_shared_runs)").fetchall()
    if not info_rows:
        # Table does not exist yet — _DDL above CREATEs it with the M3 K.6 shape,
        # so the rebuild fires on the NEXT init_db() call after that CREATE. Not
        # a programming error; the rebuild lands at first call against a populated
        # M3 K.6 schema.
        return
    by_name = {row["name"]: row for row in info_rows}
    has_batch_id = "batch_id" in by_name
    run_id_nullable = bool(by_name.get("run_id") and not by_name["run_id"]["notnull"])
    if has_batch_id and run_id_nullable:
        # Already polymorphic. Idempotent no-op.
        return

    # Rebuild required. Toggle FK pragma off first because the INSERT-SELECT would
    # otherwise check FKs as it goes against a still-original-schema reference,
    # and the rename juggle would briefly leave references dangling. Same pattern
    # as M3 DECISIONS #21's research_runs rebuild.
    # The FK pragma is a per-connection switch; we explicitly save/restore.
    prior_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if prior_fk:
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                CREATE TABLE research_shared_runs__new (
                    uuid                TEXT    PRIMARY KEY,
                    run_id              INTEGER,
                    batch_id            INTEGER,
                    include_source      INTEGER NOT NULL DEFAULT 1,
                    include_settings    INTEGER NOT NULL DEFAULT 1,
                    include_indicators  INTEGER NOT NULL DEFAULT 1,
                    include_trades      INTEGER NOT NULL DEFAULT 1,
                    include_logs        INTEGER NOT NULL DEFAULT 0,
                    view_limit          INTEGER,
                    view_count          INTEGER NOT NULL DEFAULT 0,
                    created_at          INTEGER NOT NULL,
                    revoked_at          INTEGER,
                    last_viewed_at      INTEGER,
                    -- Exactly one of (run_id, batch_id) is non-null. SQLite supports
                    -- table-level CHECK constraints since v3.3.0; safely above our
                    -- 3.44 floor (events.db needs that for in-aggregate ORDER BY).
                    CHECK ((run_id IS NULL) != (batch_id IS NULL)),
                    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
                    -- No FK on batch_id (cross-DB).
                )
            """)
            # Copy every existing row, mapping each to a run_id row (M3 K.6 origin).
            conn.execute("""
                INSERT INTO research_shared_runs__new
                       (uuid, run_id, batch_id, include_source, include_settings,
                        include_indicators, include_trades, include_logs,
                        view_limit, view_count, created_at, revoked_at, last_viewed_at)
                SELECT  uuid, run_id, NULL,    include_source, include_settings,
                        include_indicators, include_trades, include_logs,
                        view_limit, view_count, created_at, revoked_at, last_viewed_at
                  FROM research_shared_runs
            """)
            conn.execute("DROP TABLE research_shared_runs")
            conn.execute("ALTER TABLE research_shared_runs__new RENAME TO research_shared_runs")
            # Recreate the two M3 K.6 indexes + add the M2.5 batch-side index.
            conn.execute("""
                CREATE INDEX IF NOT EXISTS ix_shared_runs_run
                  ON research_shared_runs(run_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS ix_shared_runs_active
                  ON research_shared_runs(run_id) WHERE revoked_at IS NULL
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS ix_shared_runs_batch
                  ON research_shared_runs(batch_id) WHERE revoked_at IS NULL
            """)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        if prior_fk:
            conn.execute("PRAGMA foreign_keys = ON")


def _ensure_research_run_funnel_m5_grain(conn) -> None:
    """M5 1A: migrate research_run_funnel from the Phase-2 PRIMARY KEY (run_id) grain to the
    M5 (run_id, variation_id, ticker, row_scope) sweep grain (AD-M5-3/SF-A4, arch §12, ruling 4).

    Modeled EXACTLY on _ensure_research_shared_runs_polymorphic: SQLite cannot widen a primary key
    in place, so the table is recreated (CREATE __new / INSERT-SELECT / DROP / RENAME) in its OWN
    BEGIN IMMEDIATE with the FK pragma saved -> OFF -> restored, called from a get_conn() block at the
    END of init_db AFTER the shared transaction commits (the pragma cannot toggle mid-transaction and
    SQLite forbids nested BEGINs; the INSERT-SELECT + DROP + RENAME would otherwise check/dangle the FK).

    Idempotent: detection via PRAGMA table_info — if `row_scope` already exists, the table is at the new
    grain (a fresh DB is born new-grain from _DDL) and we no-op. The __new CREATE below is kept BYTE-
    IDENTICAL to the _DDL research_run_funnel block (the probe asserts both paths converge on one schema).

    Quote-agnostic: PRAGMA table_info / DROP / RENAME operate on the LOGICAL table name, which SQLite
    resolves regardless of how the table was created (quoted or not), so the eventsdb-quoted-form hazard
    (which arose from string-matching a table's CREATE SQL) does not apply here — we never read sqlite_master
    text; we build __new from scratch and copy rows. The deploy gate dry-runs this on a prod-DB copy.

    Row-count preservation (the deploy-gate invariant): the recreate maps EVERY existing row to
    variation_id=0, row_scope='cell', ticker = the run's dataset symbol. The funnel is price-path-only
    (event/legacy runs have no row), and research_runs.dataset_id is ON DELETE RESTRICT, so every funnel
    row's run + dataset are live and the symbol is a real ticker. LEFT JOINs + COALESCE(symbol,'') are used
    anyway so a hypothetical orphan maps to ticker='' rather than being silently dropped — the row count is
    preserved unconditionally. No __SINGLE__/__GROUP__ sentinel is introduced (row_scope carries the grain).
    """
    info_rows = conn.execute("PRAGMA table_info(research_run_funnel)").fetchall()
    if not info_rows:
        # Table does not exist yet (no Phase-2 funnel on this DB) — _DDL/the fresh-DB create makes it at the
        # new grain, so there is nothing to migrate. Not a programming error.
        return
    by_name = {row["name"]: row for row in info_rows}
    if "row_scope" in by_name:
        # Already at the M5 grain (fresh DB born new-grain, or a prior migration ran). Idempotent no-op.
        return

    prior_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if prior_fk:
        conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # __new — MUST stay logically identical to the _DDL research_run_funnel grain (same columns/PK/FK/
            # CHECKs; SQLite ignores comments + whitespace; the probe asserts PRAGMA-level convergence).
            conn.execute("""
                CREATE TABLE research_run_funnel__new (
                    run_id               INTEGER NOT NULL,
                    variation_id         INTEGER NOT NULL,
                    ticker               TEXT    NOT NULL,
                    row_scope            TEXT    NOT NULL
                                                 CHECK (row_scope IN ('cell','group')),
                    bars_total           INTEGER NOT NULL,
                    bars_after_warmup    INTEGER NOT NULL,
                    raw_entries          INTEGER NOT NULL,
                    entries_after_dedup  INTEGER NOT NULL,
                    raw_exits            INTEGER NOT NULL,
                    fills                INTEGER NOT NULL,
                    total_trades         INTEGER NOT NULL,
                    exits_by_signal      INTEGER NOT NULL,
                    exits_by_eod         INTEGER NOT NULL,
                    zero_reason          TEXT    NOT NULL
                                                 CHECK (zero_reason IN ('NO-DATA','ALL-NaN-WARMUP','THRESHOLD-NEVER-CROSSED','SIGNALS-BUT-NO-FILLS','ENTRIES-DEDUPED-AWAY','N/A')),
                    lead_in_bars         INTEGER,
                    PRIMARY KEY (run_id, variation_id, ticker, row_scope),
                    FOREIGN KEY (run_id) REFERENCES research_runs(id) ON DELETE CASCADE
                )
            """)
            # Map every old run_id-grain row to the new grain: variation_id=0, row_scope='cell',
            # ticker = the run's dataset symbol (LEFT JOINs preserve the row even for a hypothetical orphan).
            conn.execute("""
                INSERT INTO research_run_funnel__new
                       (run_id, variation_id, ticker, row_scope, bars_total, bars_after_warmup,
                        raw_entries, entries_after_dedup, raw_exits, fills, total_trades,
                        exits_by_signal, exits_by_eod, zero_reason)
                SELECT f.run_id, 0, COALESCE(d.symbol, ''), 'cell',
                       f.bars_total, f.bars_after_warmup, f.raw_entries, f.entries_after_dedup,
                       f.raw_exits, f.fills, f.total_trades, f.exits_by_signal, f.exits_by_eod, f.zero_reason
                  FROM research_run_funnel f
                  LEFT JOIN research_runs r     ON r.id = f.run_id
                  LEFT JOIN research_datasets d ON d.id = r.dataset_id
            """)
            conn.execute("DROP TABLE research_run_funnel")
            conn.execute("ALTER TABLE research_run_funnel__new RENAME TO research_run_funnel")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_funnel_zero_reason ON research_run_funnel(zero_reason)"
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        if prior_fk:
            conn.execute("PRAGMA foreign_keys = ON")


def _backfill_research_trials(conn) -> None:
    """M5 1A: seed research_trials from every existing research_runs row so nothing before M5 is
    invisible (R-LEDGER, requirements §8, arch §3/§15). One 'legacy_backfill' trial per run.

    Runs INSIDE init_db's shared transaction, AFTER the dataset_type NULL-normalization, so the
    dataset_type branch is exhaustive (every research_datasets row carries a non-NULL discriminator).

    Two source tables: dataset_sha256 is read off research_runs.dataset_sha256 (the denormalized self-
    standing copy, NOT off research_datasets which has no such column); only ticker comes from the
    research_datasets join. ticker = the dataset symbol for a price_dataset run, '' otherwise (a universe /
    event / fundamentals dataset's symbol is not a tradable ticker). sharpe = research_run_stats.sharpe_ratio
    (LEFT JOIN; NULL when the run never completed or had zero variance). split_id / param_config_hash use the
    reserved sentinels 'FULL-SAMPLE' / 'LEGACY' (1B's sweep write path must never emit these as real values).

    Idempotent: inserts only for run_ids not already in research_trials. The NOT IN (subquery) guard is
    correct because research_trials.run_id is NOT NULL (no three-valued-logic trap). research_trials has no
    FK, so the rows it writes stand alone after any future run/dataset deletion.
    """
    conn.execute("""
        INSERT INTO research_trials
               (run_id, variation_id, ticker, strategy_id, strategy_version, dataset_id,
                dataset_sha256, split_id, param_config_hash, sharpe, trial_kind,
                selection_scope, created_at)
        SELECT r.id, 0,
               CASE WHEN COALESCE(d.dataset_type, 'price_dataset') = 'price_dataset'
                    THEN COALESCE(d.symbol, '') ELSE '' END,
               r.strategy_id, r.strategy_version, r.dataset_id,
               r.dataset_sha256, 'FULL-SAMPLE', 'LEGACY', s.sharpe_ratio, 'legacy_backfill',
               'cumulative', r.created_at
          FROM research_runs r
          LEFT JOIN research_datasets  d ON d.id = r.dataset_id
          LEFT JOIN research_run_stats s ON s.run_id = r.id
         WHERE r.id NOT IN (SELECT run_id FROM research_trials)
    """)


# ---------------------------------------------------------------------------
# M5 U0 (AD-M5-14) — the EODHD daily availability index: boot loader + search.
# ---------------------------------------------------------------------------
# Same LIKE-wildcard escape idiom as the CIK bridge search (events/cik_ticker_map.py):
# a literal %, _, or \ typed in the search term is matched as itself, not as a wildcard.
_EODHD_LIKE_WILDCARD_ESCAPES = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})

# The committed manifest the boot loader reads (a laptop-generated artifact, git-pulled to the
# server). Resolved relative to THIS package so it works regardless of the process CWD.
_EODHD_TICKERS_MANIFEST = Path(__file__).resolve().parent / "data" / "eodhd_tickers.txt"


class EodhdTickerCaseCollision(ValueError):
    """6.6: two lines in the committed manifest fold to the same value under case (e.g. 'abc' and
    'ABC'). The index's whole reason to exist is to answer "does the store hold a file spelled
    exactly like this", and the store's own filename case is the ONLY vocabulary that question can
    be asked in (research_lab.universe._store_codes never folds case either). A manifest that names
    the same underlying code twice, under two different spellings, cannot be loaded into a single
    PRIMARY KEY row without silently picking a winner, so this refuses loudly instead, naming both
    spellings, rather than let sqlite3's insert order make that choice invisibly."""

    def __init__(self, first: str, second: str):
        self.first = first
        self.second = second
        super().__init__(
            f"eodhd ticker manifest holds two codes that fold to the same value under case: "
            f"{first!r} and {second!r}; refusing to guess which spelling is the store's real one"
        )


def load_eodhd_tickers():
    """Fill research_eodhd_tickers from the committed manifest (M5 U0). Idempotent full-replace.

    The manifest (dashboard/backend/data/eodhd_tickers.txt) is a newline-delimited list of bare
    tickers generated on the laptop (gen_eodhd_manifest.py) and committed. This loader reads it and
    rewrites the table to match it exactly, so removals in the manifest are reflected too.

    6.6: the table is loaded VERBATIM, in the exact case the manifest spells each code (no
    .upper()). The manifest is generated from the store's own filenames (research_lab.universe
    trap 6: about one in thirty codes carries a retired-symbol suffix like the lower-case "_old" in
    "AD_old"), and this index exists so a store-derived membership row (stage_runner.py's
    build_screen_universe, through create_price_universe's store_derived path) can be validated by
    EXACT match against it and rebuilt into the same filename later. Upper-casing here would make
    that later rebuild look for a file that does not exist under that spelling.

    The manifest is a discovery aid for the Add-Dataset search, NOT on the critical backtest path
    (the adapter fails loud on a missing daily file regardless), so an absent/empty manifest is a
    logged degradation, not a boot failure. The deploy gate asserts the manifest is present. A
    manifest whose own content is internally inconsistent (two spellings of one code) is a
    different kind of problem, a corrupt committed artifact, and DOES fail loud (EodhdTickerCaseCollision):
    tolerating it would mean two different rows in this table both claim to be the same file.
    """
    path = _EODHD_TICKERS_MANIFEST
    try:
        raw = path.read_text(encoding="utf-8")  # whole file; it is tens of KB of bare tickers
    except FileNotFoundError:
        log.warning("eodhd ticker manifest absent; leaving research_eodhd_tickers empty",
                    extra={"manifest_path": str(path), "loaded_count": 0})
        return
    except OSError as exc:  # unreadable (permissions, IO), treat as absent, do not crash boot
        log.warning("eodhd ticker manifest unreadable; leaving research_eodhd_tickers empty",
                    extra={"manifest_path": str(path), "loaded_count": 0, "error": str(exc)})
        return
    # Normalize: strip, drop blanks, de-dup (order does not matter, the PK sorts on read). No
    # .upper(): the store's own spelling is this table's one and only vocabulary (6.6).
    tickers = sorted({line.strip() for line in raw.splitlines() if line.strip()})
    if not tickers:  # present but empty, same non-fatal degradation as absent
        log.warning("eodhd ticker manifest is empty; leaving research_eodhd_tickers empty",
                    extra={"manifest_path": str(path), "loaded_count": 0})
        return
    # Refuse a manifest naming the same code under two different spellings (fail loud, before any
    # write): a PRIMARY KEY column cannot hold both, and picking one silently would be exactly the
    # kind of one-place-becomes-two-place normalization bug 6.6 exists to close off.
    folded_to_spelling: dict[str, str] = {}
    for t in tickers:                                            # tickers is already sorted + de-duped
        folded_key = t.upper()
        if folded_key in folded_to_spelling:
            raise EodhdTickerCaseCollision(folded_to_spelling[folded_key], t)
        folded_to_spelling[folded_key] = t
    # Full-replace in ONE transaction: DELETE then bulk-INSERT, so the table reflects exactly the
    # committed artifact every boot (the idempotent refresh; removals handled).
    with transaction() as conn:
        conn.execute("DELETE FROM research_eodhd_tickers")
        conn.executemany("INSERT INTO research_eodhd_tickers(ticker) VALUES (?)",
                         [(t,) for t in tickers])
    log.info("loaded eodhd ticker manifest",
             extra={"manifest_path": str(path), "loaded_count": len(tickers)})


# M5 Track C C-bench (§2.1): the committed benchmark-catalog seed — the broad market + the standard sector
# SPDR ETFs, each mapped to its exposure_key with a provenance string (chosen by MANDATE / exposure, NOT by
# fit — the anti-bias record made durable, §3). Loaded idempotently at boot (the load_eodhd_tickers precedent).
# exposure_key is the mandate bucket; is_default marks the pre-committed pick per exposure. Each ticker is
# validated against the eodhd availability index on load (an absent one is logged + skipped, boot-safe).
_BENCHMARK_CATALOG_SEED = [
    # (exposure_key, benchmark_ticker, label, is_default, provenance)
    ("broad_market", "SPY", "S&P 500 (SPY)", 1,
     "the broad US large-cap market benchmark (SPDR S&P 500 ETF); the mandate default for any equity universe, "
     "chosen by exposure (broad market), not by fit"),
    ("sector:technology", "XLK", "Technology sector (XLK)", 1,
     "the Technology Select Sector SPDR; the mandate benchmark for a technology-exposure universe"),
    ("sector:energy", "XLE", "Energy sector (XLE)", 1,
     "the Energy Select Sector SPDR; the mandate benchmark for an energy-exposure universe"),
    ("sector:financials", "XLF", "Financials sector (XLF)", 1,
     "the Financial Select Sector SPDR; the mandate benchmark for a financials-exposure universe"),
    ("sector:health_care", "XLV", "Health Care sector (XLV)", 1,
     "the Health Care Select Sector SPDR; the mandate benchmark for a health-care-exposure universe"),
    ("sector:consumer_discretionary", "XLY", "Consumer Discretionary sector (XLY)", 1,
     "the Consumer Discretionary Select Sector SPDR; the mandate benchmark for that exposure"),
    ("sector:consumer_staples", "XLP", "Consumer Staples sector (XLP)", 1,
     "the Consumer Staples Select Sector SPDR; the mandate benchmark for that exposure"),
    ("sector:industrials", "XLI", "Industrials sector (XLI)", 1,
     "the Industrial Select Sector SPDR; the mandate benchmark for an industrials-exposure universe"),
    ("sector:materials", "XLB", "Materials sector (XLB)", 1,
     "the Materials Select Sector SPDR; the mandate benchmark for a materials-exposure universe"),
    ("sector:utilities", "XLU", "Utilities sector (XLU)", 1,
     "the Utilities Select Sector SPDR; the mandate benchmark for a utilities-exposure universe"),
    ("sector:real_estate", "XLRE", "Real Estate sector (XLRE)", 1,
     "the Real Estate Select Sector SPDR; the mandate benchmark for a real-estate-exposure universe"),
    ("sector:communication_services", "XLC", "Communication Services sector (XLC)", 1,
     "the Communication Services Select Sector SPDR; the mandate benchmark for that exposure"),
]

# the exposure key every equity universe is benchmarked against by default (the broad market). The sector +
# crypto benchmarks need a per-universe exposure CLASSIFIER (a named follow-on); until then the broad-market
# default is the honest, pre-committed, anti-bias-clean benchmark for an all-equities universe.
_BENCHMARK_BROAD_MARKET_KEY = "broad_market"


class BenchmarkNotAvailable(ValueError):
    """A benchmark_ticker is not in the eodhd availability index (C-bench §2.1 catalog-write validation)."""


def _benchmark_ticker_in_index(conn, ticker: str) -> bool:
    """True iff `ticker` is in research_eodhd_tickers (the availability index a price member also passes, AD-M5-11)."""
    return conn.execute(
        "SELECT 1 FROM research_eodhd_tickers WHERE ticker = ? LIMIT 1;", (ticker,)).fetchone() is not None


def add_benchmark_to_catalog(exposure_key: str, benchmark_ticker: str, label: str,
                             is_default: int, provenance: str) -> None:
    """Insert/replace ONE benchmark-catalog row, validating the ticker against the index FAIL-LOUD (C-bench §2.1).

    A ticker absent from research_eodhd_tickers raises BenchmarkNotAvailable (no silent accept that would later
    render a fake benchmark). This is the EXPLICIT write path (a future catalog-admin surface); the boot seed
    loader skips-with-warning instead, because boot must not crash on a not-yet-available ETF."""
    with transaction() as conn:
        if not _benchmark_ticker_in_index(conn, benchmark_ticker):
            raise BenchmarkNotAvailable(
                f"benchmark ticker {benchmark_ticker!r} is not in the eodhd availability index; "
                "cannot add it to the catalog (its data is not available, so it would render a fake benchmark)")
        conn.execute(
            "INSERT OR REPLACE INTO research_benchmark_catalog "
            "    (exposure_key, benchmark_ticker, label, is_default, provenance) VALUES (?, ?, ?, ?, ?);",
            (exposure_key, benchmark_ticker, label, int(is_default), provenance))


def load_benchmark_catalog() -> None:
    """Seed research_benchmark_catalog from the committed seed (C-bench §2.1). Idempotent; boot-safe.

    INSERT OR REPLACE each seed row so a seed edit (relabel, new exposure) applies every boot AND any later
    user-added row (a different PK) survives. A seed ticker not yet in the eodhd availability index is logged +
    skipped (its data is unavailable, so freezing it would later render a fake benchmark) — boot-safe, never a
    crash. Runs AFTER load_eodhd_tickers so the index it validates against is filled (init_db order)."""
    loaded, skipped = 0, []
    with transaction() as conn:
        for exposure_key, ticker, label, is_default, provenance in _BENCHMARK_CATALOG_SEED:
            if not _benchmark_ticker_in_index(conn, ticker):
                skipped.append(ticker)  # the ETF's data isn't available yet -> skip (don't seed a fake)
                continue
            conn.execute(
                "INSERT OR REPLACE INTO research_benchmark_catalog "
                "    (exposure_key, benchmark_ticker, label, is_default, provenance) VALUES (?, ?, ?, ?, ?);",
                (exposure_key, ticker, label, int(is_default), provenance))
            loaded += 1
    if skipped:
        log.warning("benchmark-catalog seed: tickers absent from the eodhd index were skipped",
                    extra={"loaded_count": loaded, "skipped": skipped})
    else:
        log.info("loaded benchmark catalog seed", extra={"loaded_count": loaded})


def get_default_benchmark(conn, exposure_key: str) -> dict | None:
    """The pre-committed default benchmark row for an exposure (C-bench §3), or None if none is cataloged.

    Returns {"benchmark_ticker", "label", "provenance"} for the is_default=1 row of `exposure_key` (the
    deterministic pick if several somehow carry is_default). The CHOICE the dispatch pre-commits + freezes."""
    return conn.execute(
        "SELECT benchmark_ticker, label, provenance FROM research_benchmark_catalog "
        "WHERE exposure_key = ? AND is_default = 1 ORDER BY benchmark_ticker LIMIT 1;",
        (exposure_key,)).fetchone()


def freeze_benchmark_sha_on_ready(dataset_id: int) -> int:
    """Phase-2 benchmark content-pin (C-bench §3): when a frozen benchmark dataset readies, pin its sha.

    The benchmark is NOT a sweep member, so the member-ready finalize (finalize_materializing_sweeps_for) does
    not cover it. This is the benchmark analogue of the universe_sha256 phase-2 write: it fills only a NULL
    benchmark_sha256 (idempotent — never overwrites a content already pinned), for every registration that
    froze THIS dataset as its benchmark. Returns the number of registrations pinned (0 if not ready / no sha)."""
    with transaction() as conn:
        ds = conn.execute(
            "SELECT sha256, status FROM research_datasets WHERE id = ?;", (dataset_id,)).fetchone()
        if ds is None or ds["status"] != "ready" or not ds["sha256"]:
            return 0  # not ready or no content hash yet -> nothing to pin
        cur = conn.execute(
            "UPDATE research_sweep_registrations SET benchmark_sha256 = ? "
            "WHERE benchmark_dataset_id = ? AND benchmark_sha256 IS NULL;",
            (ds["sha256"], dataset_id))
        return cur.rowcount


def search_eodhd_tickers(q: str, limit: int) -> list[str]:
    """Prefix-search the EODHD availability index, exact match first (M5 U0).

    Mirrors cik_ticker_map_search's semantics so the future F5 alpaca/binance merge sees two
    visually-parallel helpers. `q` is the already-trimmed term (the route trims + rejects blank);
    the parameter names (`prefix`, `ticker_exact`, `limit`) match the CIK precedent. Returns bare
    tickers, the exact match sorted first, then alphabetically.
    """
    prefix = q.translate(_EODHD_LIKE_WILDCARD_ESCAPES).upper() + "%"  # escaped, upper-cased prefix
    ticker_exact = q.upper()  # tickers are stored upper-cased; an exact hit sorts first
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ticker FROM research_eodhd_tickers "
            "WHERE ticker LIKE :prefix ESCAPE '\\' "
            "ORDER BY (ticker = :ticker_exact) DESC, ticker ASC "
            "LIMIT :limit",
            {"prefix": prefix, "ticker_exact": ticker_exact, "limit": limit},
        ).fetchall()
    return [r["ticker"] for r in rows]


# ---------------------------------------------------------------------------
# M5 U1 (AD-M5-11 / R-UNIV) — the ticker-keyed price universe: create + manage.
# ---------------------------------------------------------------------------
# A price universe is a row-backed catalog row (dataset_type='price_universe') plus a
# research_price_universe_membership row per resolved ticker. It is validated against U0's
# availability index (research_eodhd_tickers), NOT the CIK bridge — the whole point of R-UNIV
# (an ETF or foreign listing has price data but no CIK). U1 stores the durable TICKER only; the
# per-member eodhd dataset materialize + the universe identity hash are U2's dispatch_sweep
# (the AD-M5-12 boundary). No data is fetched here — a row-backed universe is 'ready' on create.

# The eodhd members are daily; the catalog row's timeframe records that. start/end are 0 sentinels
# (a universe has no time window of its own — the same shape the CIK universe row uses).
_PRICE_UNIVERSE_TIMEFRAME = "1d"
_PRICE_UNIVERSE_SOURCE = "eodhd"    # the members' data source; cosmetic on the row (members carry their own)


class PriceUniverseEmptyError(ValueError):
    """create_price_universe was given no ticker that is in the EODHD availability index (R-UNIV)."""


class PriceUniverseMixedSource(ValueError):
    """GV2-4a: a typed universe holds both made-up (SYN_) tickers and real ones; the two never mix (route -> 422)."""

    def __init__(self, synthetic: list[str], real: list[str]):
        self.synthetic = synthetic
        self.real = real
        super().__init__(
            f"a universe cannot mix made-up and real tickers: made-up {synthetic}, real {real}"
        )


class PriceUniverseNameExists(ValueError):
    """A price universe with this name already exists; carries the name + existing id so the route can 409."""

    def __init__(self, existing_id: int, name: str):
        self.existing_id = existing_id
        self.name = name
        super().__init__(f"a price universe named {name!r} already exists (id={existing_id})")


class PriceUniverseNotFound(LookupError):
    """dispatch_sweep was given a universe id that is not a price_universe catalog row (route -> 404)."""


class PriceUniverseLastMember(ValueError):
    """DL1: removing this ticker would leave the universe with no member (route -> 422)."""


class MemberNotFound(LookupError):
    """DL1: the ticker is not in the universe's current list (route -> 404)."""


class PriceUniverseAddNothing(ValueError):
    """DL1: an add where every ticker was excluded (not in the EODHD index) or already a member (route -> 422)."""

    def __init__(self, excluded: list[str], already: list[str]):
        self.excluded = excluded
        self.already = already
        super().__init__(f"nothing to add: not in the EODHD index {excluded}, already members {already}")


class PriceUniverseWindowInvalid(ValueError):
    """The test window is invalid (start_ts > end_ts) — surfaced as a clean 422 (AD-UU-3, B1)."""


class PriceUniverseWindowRequired(ValueError):
    """Set 9 night lane DX (U.10 item 5b, DECISIONS #399 sheet ruling 1): create_price_universe
    was given a window with one or both bounds absent. A universe with a NULL bound reads today as
    "full history" nowhere real: tracker.request_draw's own R-T15 refuses ANY draw whose window
    carries a NULL bound, naming it a NULL window bound that means the bound is unknown, never
    full history. So creating a universe that can never be drawn against is not a permissive
    default, it is a trap a person only discovers later, at dispatch time, with no obvious cause.
    Carries which side (or both) was missing so the route's 422 names it plainly."""

    def __init__(self, missing_sides: "list[str]"):
        self.missing_sides = missing_sides
        super().__init__(
            f"a price universe's test window is required on both sides; missing: {', '.join(missing_sides)} "
            "(a NULL bound means the bound is unknown, and the tracker refuses it, R-T15: never full history)"
        )


class SweepGridTooLarge(Exception):
    """dispatch_sweep's registered cell count (combinations x members) exceeds the cap (AD-UU-5, B3).

    A standalone Exception (NOT a ValueError) so the dispatch route maps it to a readable 422 with the
    product + cap, distinct from the bare-ValueError 400 (a malformed grid). Raised AFTER the member read,
    BEFORE any run/registration/job row is written, so an over-cap dispatch leaves no half-written sweep."""


class FrozenSweepParameterError(Exception):
    """dispatch_sweep's grid names a key whose provenance Group is anything other than A, OR a key
    whose provenance entry exists but carries no readable Group at all: FAIL-CLOSED, not a corrected
    roster (`DECISIONS.md` #316, replacing #304's hand-typed "B, C, E or F" deny list, which left
    group D wrongly passable). A standalone Exception, following SweepGridTooLarge's own precedent
    right above, so the dispatch route maps it to a readable 422 distinct from the bare-ValueError
    400 (a malformed grid). Raised BEFORE any run/registration/job row is written (job file PART 4c:
    a refused sweep never becomes a trial in the ledger), and BEFORE the member read SweepGridTooLarge
    waits for, since a frozen or malformed key makes the grid invalid regardless of how large it is.
    Group A (a strategy knob, tuned in every sweep) is the ONLY tier a grid is allowed to name.
    UPDATED by night four's allow-list unit (`#324`, `#316` item 4): a key with NO provenance entry
    at all is still not frozen under THIS exception (it carries no tier to be frozen under), but it
    is no longer automatically waved through either, see UndeclaredSweepParameterError below,
    which is what now refuses it when the strategy did not declare it as one of its own knobs."""


class UndeclaredSweepParameterError(Exception):
    """dispatch_sweep's grid names a key that is neither a provenance-tiered parameter (group A or
    otherwise) NOR one of the strategy's own declared sweep knobs (its class `sweep_params` for
    'vectorized' mode, or its `self.param(...)` calls for 'inline' mode): FAIL-CLOSED (`#324`,
    closing the hole `#316` item 4 left open on purpose: "A key with no provenance entry at all
    still passes, on purpose, because a strategy's own declared knobs carry no tier. That hole
    closes when the allow-list unit ships"). Two distinct grid keys reach this refusal, both proved
    by the same reasoning: the worker's own grid[p.name] read (run_sweep_entrypoint.py) is a LEFT
    JOIN over the strategy's declared params, so a key the strategy never declared would silently
    never be swept, which is worse than a loud refusal. (1) a grid key with no provenance entry at
    all that the strategy also does not declare (counted UNREGISTERED). (2) a grid key with a
    group-A provenance entry, so it passes the tier check above, that the strategy still never
    declared (counted UNDECLARED). A standalone Exception, mirroring FrozenSweepParameterError's own
    shape, so the dispatch route maps it to a readable 422 distinct from the bare-ValueError 400.
    Raised BEFORE any run/registration/job row is written, for the same reason FrozenSweepParameterError
    is. Never raised for a zero-parameter strategy's own non-empty-grid refusal, which stays the
    pre-existing bare ValueError (9.2's own sentence: that check is unchanged by this unit)."""


class SweepParameterMissing(Exception):
    """RS1: a vectorized or inline sweep's grid leaves out a parameter the strategy declares. The
    worker reads every declared parameter (grid[p.name]), so such a run would be accepted here and
    then fail in its container. Refused before any row is written, and before a data window is
    drawn (check_sweep). A standalone Exception beside UndeclaredSweepParameterError; the routes
    map it to a 422 'sweep_parameter_missing: ...' and the chain runner to the word
    'sweep-parameter-missing'."""


_DAY_SECONDS = 86_400  # one UTC day in seconds — the window granularity (date-only, F4)


def _normalize_window_ts(ts: int | None) -> int | None:
    """Floor an epoch-second window bound to UTC midnight (the day-aligned form AD-UU-3 P-1 requires),
    so a non-aligned input cannot silently shift the cold slice by part of a day. NULL stays NULL:
    an absent bound, never read as full history (item 5b, R-T15 refuses a NULL bound outright)."""
    if ts is None:                                              # an open-ended bound stays open (NULL propagates)
        return None
    return (int(ts) // _DAY_SECONDS) * _DAY_SECONDS             # integer-floor the epoch to the UTC day boundary


def _normalize_price_ticker(raw: str) -> str:
    """Normalize a TYPED ticker (a human pasting a symbol) to the bare-upper EODHD canonical form
    (AD-M5-14). This is the typed-path candidate key ONLY: since 6.6 the index itself no longer
    force-uppercases every row, so this upper-cased string is used to look the candidate up
    case-insensitively (create_price_universe's own resolve_map below), never compared with a bare
    `=` against the index. The value actually stored is the index's OWN spelling, not this one.

    Strip whitespace, upper-case, and strip a trailing '.US' if the user typed it ('no layer adds
    or strips .US except the adapter'). It deliberately does NOT transform dots: the index holds the
    EODHD share-class form (e.g. 'BRK-B' with a hyphen), so a typed 'BRK.B' validates as itself and
    is reported excluded if absent, never silently rewritten. This is NOT the SEC normalize_ticker
    (events/tickers.py), which does dot->hyphen for the CIK/event path; that is the wrong normalize here.
    """
    t = raw.strip().upper()
    if t.endswith(".US"):                                       # the API/file-stem suffix, never stored
        t = t[:-3]
    return t


def _resolve_price_tickers(tickers: list[str], *, store_derived: bool = False) -> tuple[list[str], list[str]]:
    """Normalise typed tickers and resolve them against the EODHD availability index (shared by create and add).

    Returns (resolved, excluded): resolved in the index's own spelling and typed order, excluded as typed but
    absent from the index. Never raises for an empty result (the caller decides: create raises
    PriceUniverseEmptyError, add raises PriceUniverseAddNothing). Raises PriceUniverseMixedSource when the
    typed list mixes made-up (SYN_) and real tickers.
    """
    # Normalize + de-dup (preserve first-seen order for a stable excluded report). 6.6: the two
    # callers need two different normalizations here, not just two different validation queries
    # below, because a store-derived code must never be re-cased at all (its spelling already IS
    # the file's own spelling; that is the whole point).
    seen: set[str] = set()
    unique: list[str] = []
    for raw in tickers:
        if store_derived:
            # research_lab.universe._store_codes' own bare filename stem, kept BYTE FOR BYTE. No
            # .upper(), no trailing-.US strip (the store never adds one): re-casing here would put
            # a second, silent normalization on this side of the boundary R9.2 says must stay in
            # exactly one place (create_price_universe's own typed-path branch below).
            t = raw.strip()
        else:
            t = _normalize_price_ticker(raw)                    # the typed path: trim/upper/drop .US
        if t and t not in seen:                                 # drop blanks and duplicates
            seen.add(t)
            unique.append(t)
    # Resolve each candidate against the availability index, batched so a large paste cannot exceed
    # the SQLite bound-variable limit (a hand-typed universe is small; the chunking is the
    # long-game guard). resolve_map's VALUE is always the index's own spelling, never the candidate:
    # for store_derived that is the same string (an exact match can only match itself), for typed
    # it may differ from what the human typed (e.g. 'AD_OLD' typed becomes 'AD_old' stored), which
    # is the 6.6 fix itself.
    # GV2-4a: made-up tickers (the SYN_ pattern) are not in the EODHD availability index and never will be; on
    # the typed path they are accepted by the pattern, and a universe holds all made-up or all real tickers.
    synthetic_tickers: list[str] = []
    if not store_derived:
        synthetic_tickers = [t for t in unique if dataset_sources.price_source_for_ticker(t) == "synthetic"]
        if synthetic_tickers:
            real_tickers = [t for t in unique if t not in synthetic_tickers]
            if real_tickers:
                raise PriceUniverseMixedSource(synthetic_tickers, real_tickers)  # fail loud (422) before any DB write
    resolve_map: dict[str, str] = {}
    if synthetic_tickers:
        resolve_map = {t: t for t in synthetic_tickers}  # accepted by the pattern; no index lookup
    elif unique:
        with get_conn() as conn:
            for i in range(0, len(unique), 500):                # chunk of 500 << SQLite's variable limit
                chunk = unique[i:i + 500]
                placeholders = ",".join("?" for _ in chunk)
                if store_derived:
                    # EXACT match: the store's own spelling must equal the index's own spelling
                    # byte for byte, because both ultimately name the same physical file.
                    rows = conn.execute(
                        f"SELECT ticker FROM research_eodhd_tickers WHERE ticker IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for r in rows:
                        resolve_map[r["ticker"]] = r["ticker"]
                else:
                    # Case-INSENSITIVE resolve: an upper-cased comparison column in the query,
                    # never a filesystem lookup (R9.2), returning the index's OWN spelling.
                    rows = conn.execute(
                        f"SELECT ticker FROM research_eodhd_tickers WHERE UPPER(ticker) IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for r in rows:
                        resolve_map[r["ticker"].upper()] = r["ticker"]
    resolved = [resolve_map[t] for t in unique if t in resolve_map]   # the index's own spelling, in typed order
    excluded = [t for t in unique if t not in resolve_map]            # typed but absent, reported, not dropped
    return resolved, excluded


def create_price_universe(name: str, tickers: list[str],
                          window_start_ts: int | None = None,
                          window_end_ts: int | None = None,
                          *, store_derived: bool = False,
                          display_name: str | None = None) -> dict:
    """Create a price universe: validate tickers against the availability index, persist the
    catalog row + one membership row per resolved ticker. Returns the resolved/excluded report.

    The test window (window_start_ts/window_end_ts, epoch seconds, AD-UU-3) is written in the SAME
    create transaction (atomic: the universe and its window are one write, requirements FR3); each
    bound is floored to UTC midnight (day-aligned), and start > end raises PriceUniverseWindowInvalid
    (a clean 422). Set 9 night lane DX (U.10 item 5b, DECISIONS #399 sheet ruling 1): BOTH bounds
    are now REQUIRED, raising PriceUniverseWindowRequired (a clean 422 naming the missing side or
    sides) when either is absent. This replaces the earlier "either bound may be NULL, open on
    that side means full history" reading: that reading was never true at the point a universe is
    actually USED, because tracker.request_draw's own R-T15 refuses any draw whose window carries
    a NULL bound. A universe nobody can draw against is a trap, not a convenience, so the trap is
    now refused at the one point it can be refused cheaply: creation, before any row exists.

    6.6, ONE vocabulary for a store-derived code: `store_derived=True` is for the ONE caller that
    already holds the store's own filename spelling (stage_runner.build_screen_universe, whose own
    names come from research_lab.universe._store_codes, verbatim, e.g. 'AD_old'). That path is kept
    byte for byte, never upper-cased, and validated by EXACT match against the index, because both
    sides ultimately have to name the same file on disk. `store_derived=False` (the default) is the
    TYPED path (a human pasting a symbol into the Add-Dataset box): it resolves CASE-INSENSITIVELY
    against the index (an upper-cased comparison column in the query, never the filesystem, R9.2)
    and stores the INDEX's OWN spelling, so a typed 'ad_old' still ends up in the membership table
    spelled 'AD_old', matching whatever stage_runner._bar_dates_for_ticker will later rebuild a
    path from. Validation is against research_eodhd_tickers (U0), NOT the CIK bridge (R-UNIV).
    Tickers are normalized per the branch above, de-duped, and partitioned into resolved (the
    index's own spelling) and excluded (absent from the index): the excluded are REPORTED, never
    silently dropped or re-cased (the report-the-excluded shape the CIK create uses). If nothing
    resolves, raise PriceUniverseEmptyError (fail loud, no fallback). A duplicate universe name
    raises PriceUniverseNameExists (a clean 409, not a raw IntegrityError on the research_datasets
    UNIQUE key).
    """
    display_name = _clean_display_name(display_name)            # UNI1: before any write; StrategyValidationError if blank or too long
    now = int(time.time())
    # Normalize the window to UTC midnight + validate ordering (AD-UU-3). Done before any DB work so a
    # bad window fails loud before the universe is created (no half-written universe).
    window_start_ts = _normalize_window_ts(window_start_ts)     # floor the start bound to UTC midnight (NULL stays NULL)
    window_end_ts = _normalize_window_ts(window_end_ts)         # floor the end bound to UTC midnight (NULL stays NULL)
    # Item 5b: both bounds are required from here on. Checked BEFORE the ordering check below (a
    # missing side is a more specific, more actionable refusal than "inverted", and checking order
    # matters because an absent bound would otherwise silently pass the "both set" ordering test).
    missing_sides = [side for side, ts in (("start", window_start_ts), ("end", window_end_ts)) if ts is None]
    if missing_sides:
        raise PriceUniverseWindowRequired(missing_sides)         # fail loud (422) before any DB write
    if window_start_ts is not None and window_end_ts is not None and window_start_ts > window_end_ts:  # both set + inverted
        raise PriceUniverseWindowInvalid(                       # fail loud (422) before any DB write
            f"test window start ({window_start_ts}) is after end ({window_end_ts})"
        )
    resolved, excluded = _resolve_price_tickers(tickers, store_derived=store_derived)
    if not resolved:                                            # nothing resolvable, fail loud (no fallback)
        raise PriceUniverseEmptyError(
            f"no typed ticker is in the EODHD availability index (excluded: {excluded})"
        )
    # GV2-4a: the helper refused a mixed list, so the resolved tickers are all made-up or all real.
    universe_source = (
        "synthetic" if (not store_derived and dataset_sources.price_source_for_ticker(resolved[0]) == "synthetic")
        else _PRICE_UNIVERSE_SOURCE
    )
    with transaction() as conn:
        # Name-collision check first: a duplicate name would otherwise hit the research_datasets
        # UNIQUE(source, symbol, timeframe, start_ts, end_ts) as a raw IntegrityError; surface it clean.
        existing = conn.execute(
            "SELECT id FROM research_datasets WHERE name = ? AND dataset_type = ?",
            (name, DATASET_TYPE_PRICE_UNIVERSE),
        ).fetchone()
        if existing is not None:
            raise PriceUniverseNameExists(int(existing["id"]), name)
        dataset_storage_kind(DATASET_TYPE_PRICE_UNIVERSE)       # H4 registry gate before the INSERT
        cur = conn.execute(
            """INSERT INTO research_datasets
                  (name, dataset_type, event_type, source,
                   symbol, timeframe, status,
                   start_ts, end_ts, rows,
                   sha256, file_path, file_bytes,
                   created_at, last_fetched_at,
                   window_start_ts, window_end_ts, display_name, display_name_from_name)
               VALUES (?, ?, NULL, ?,
                       ?, ?, 'ready',
                       0, 0, NULL,
                       NULL, NULL, NULL,
                       ?, ?,
                       ?, ?, ?, 0)""",
            (name, DATASET_TYPE_PRICE_UNIVERSE, universe_source,
             name, _PRICE_UNIVERSE_TIMEFRAME,                    # symbol=name keeps the UNIQUE key distinct per name
             now, now,
             window_start_ts, window_end_ts, display_name),     # the saved test window (AD-UU-3), both bounds required (item 5b); UNI1 display name
        )
        universe_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO research_price_universe_membership(universe_id, ticker, added_at) VALUES (?, ?, ?)",
            [(universe_id, t, now) for t in resolved],
        )
        conn.executemany(                                       # DL1: one open span per member, same transaction
            "INSERT INTO research_price_universe_member_spans(universe_id, ticker, added_at) VALUES (?, ?, ?)",
            [(universe_id, t, now) for t in resolved],
        )
    return {"universe_id": universe_id, "name": name, "display_name": display_name,
            "resolved": resolved, "excluded": excluded, "member_count": len(resolved),
            "window_start_ts": window_start_ts, "window_end_ts": window_end_ts}


def update_price_universe_window(universe_id: int,
                                 window_start_ts: int | None,
                                 window_end_ts: int | None) -> dict:
    """Set or clear a price universe's test window after creation (AD-UU-3, the PATCH /window sub-route).

    Each non-NULL bound is floored to UTC midnight; start > end raises PriceUniverseWindowInvalid (422).
    A universe_id that is not a price_universe catalog row raises PriceUniverseNotFound (404), never a
    silent no-op. Either bound may be set to NULL here (the admin correction path this route serves,
    item 5b's own create-time requirement does not apply after creation); a NULL bound is never full
    history, it is unknown, and the tracker's own R-T15 refuses a draw against it. Returns the stored
    window.
    """
    window_start_ts = _normalize_window_ts(window_start_ts)     # floor the start bound to UTC midnight (NULL stays NULL)
    window_end_ts = _normalize_window_ts(window_end_ts)         # floor the end bound to UTC midnight (NULL stays NULL)
    if window_start_ts is not None and window_end_ts is not None and window_start_ts > window_end_ts:  # both set + inverted
        raise PriceUniverseWindowInvalid(                       # fail loud (422) before touching the DB
            f"test window start ({window_start_ts}) is after end ({window_end_ts})"
        )
    with transaction() as conn:                                 # atomic: the existence check + the update commit together
        row = conn.execute(
            "SELECT id FROM research_datasets WHERE id = ? AND dataset_type = ?",  # must be a price_universe catalog row
            (universe_id, DATASET_TYPE_PRICE_UNIVERSE),
        ).fetchone()
        if row is None:                                         # not a price universe — fail loud (404), no no-op
            raise PriceUniverseNotFound(f"no price universe with id {universe_id}")
        conn.execute(
            "UPDATE research_datasets SET window_start_ts = ?, window_end_ts = ? WHERE id = ?",
            (window_start_ts, window_end_ts, universe_id),
        )
    return {"universe_id": universe_id,
            "window_start_ts": window_start_ts, "window_end_ts": window_end_ts}


def backfill_dataset_windows_from_coverage(conn) -> int:
    """Set 9 night lane DX (U.10 item 5a): copy a price dataset's already-measured coverage into
    its window, for every row that readied before `worker_dataset_result` started writing both
    columns in the same statement. Idempotent: only a `price_dataset` row with a NULL window AND
    a non-NULL coverage is touched, so a second call always affects zero rows. Runs on the
    caller's own already-open transaction (init_db's own idiom for a data-touching migration
    step), never a fallback value: a row with no coverage at all (a failed fetch, or a row from
    before W3 added the covered_* columns) is left exactly as it was, NULL on both sides, and
    stays refused by the tracker's own R-T15 rather than reading as "full history". Returns the
    number of rows touched, logged by name at the call site.

    DDIA (Kleppmann ch4, storage and retrieval, indexing): this UPDATE's WHERE clause (dataset_type, window_start_ts,
    window_end_ts, covered_start_ts, covered_end_ts) has NO index-backed path today, so every call
    is a full scan of `research_datasets`; answered here rather than left open, round 4, SF2
    (code-quality pass 1). Measured, not assumed: production reads about 521 ready price datasets
    and a few hundred rows in `research_datasets` overall on 2026-09-15 (the lead, over SSH,
    `sqlite3 <prod path> "SELECT COUNT(*) FROM research_datasets WHERE dataset_type='price_dataset'
    AND status='ready'"` for the first number, the bare table count for the second); this lane's
    own local database (`sqlite3 dashboard/research.db "SELECT COUNT(*) FROM research_datasets"`,
    run in the main checkout, since a fresh worktree carries no database file) reads 1 row today.
    A full scan of a table sized in the hundreds, run once per process boot rather than once per
    request, costs a fraction of a millisecond; the chapter's own principle is that an index is
    CHOSEN, never automatic, and always trades a real write cost (every insert now also maintains
    the index) for a read benefit, so one is worth adding only once the read pattern it serves is
    both frequent and felt at the table's actual size, and neither holds here yet. The named regime
    this gets revisited at, so the question
    is asked once rather than reopened every session: if `research_datasets` ever approaches the
    same rough order of magnitude the code-quality checklist's own future-proofing item names
    (100,000 rows), add `CREATE INDEX IF NOT EXISTS idx_datasets_backfill_window ON
    research_datasets(dataset_type, window_start_ts)` beside `idx_datasets_status` and
    `idx_datasets_source` above, which would make this scan index-backed for a fraction of the
    write cost a full covering index would add. No index added tonight."""
    cur = conn.execute(
        "UPDATE research_datasets "
        "   SET window_start_ts = covered_start_ts, window_end_ts = covered_end_ts "
        " WHERE dataset_type = 'price_dataset' "
        "   AND window_start_ts IS NULL AND window_end_ts IS NULL "
        "   AND covered_start_ts IS NOT NULL AND covered_end_ts IS NOT NULL;"
    )
    return cur.rowcount


def list_price_universe_members(universe_id: int) -> list[str]:
    """The members of a price universe, sorted (the UNIQUE(universe_id, ticker) prefix scan, AD-M5-11)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker",
            (universe_id,),
        ).fetchall()
    return [r["ticker"] for r in rows]


def _live_price_universe_row(conn, universe_id: int):
    """The price_universe catalog row that is not soft-deleted, or raise PriceUniverseNotFound (add/remove guard)."""
    row = conn.execute(
        "SELECT id, name, source FROM research_datasets WHERE id = ? AND dataset_type = ? AND status != 'deleted';",
        (universe_id, DATASET_TYPE_PRICE_UNIVERSE),
    ).fetchone()
    if row is None:
        raise PriceUniverseNotFound(f"price universe {universe_id} not found")
    return row


def add_price_universe_members(universe_id: int, tickers: list[str], now: int) -> dict:
    """DL1 (DECISIONS #455): add tickers to a price universe, each with its own dated span.

    The same normalising and EODHD-index check as create. Returns {"added", "excluded", "already"}.
    One BEGIN IMMEDIATE: the membership row and the span are written together. Raises
    PriceUniverseNotFound (unknown or soft-deleted universe), PriceUniverseMixedSource (a ticker whose
    source differs from the universe's), PriceUniverseAddNothing (every ticker excluded or already in).
    """
    resolved, excluded = _resolve_price_tickers(tickers)           # mixed typed lists raise here
    with transaction() as conn:
        urow = _live_price_universe_row(conn, universe_id)
        mismatched = [t for t in resolved if dataset_sources.price_source_for_ticker(t) != urow["source"]]
        if mismatched:
            current = [r["ticker"] for r in conn.execute(
                "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker;",
                (universe_id,)).fetchall()]
            if urow["source"] == "synthetic":
                raise PriceUniverseMixedSource(current, mismatched)
            raise PriceUniverseMixedSource(mismatched, current)
        have = {r["ticker"] for r in conn.execute(
            "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ?;",
            (universe_id,)).fetchall()}
        already = [t for t in resolved if t in have]
        added = [t for t in resolved if t not in have]
        if not added:
            raise PriceUniverseAddNothing(excluded, already)
        for t in added:
            # A span still open for a ticker whose row was deleted outside this code is closed first, so the
            # one-open-span index holds; it closes at the later of its own start and now.
            conn.execute(
                "UPDATE research_price_universe_member_spans SET removed_at = MAX(added_at, ?) "
                "WHERE universe_id = ? AND ticker = ? AND removed_at IS NULL;", (now, universe_id, t))
            conn.execute(
                "INSERT INTO research_price_universe_membership(universe_id, ticker, added_at) VALUES (?, ?, ?);",
                (universe_id, t, now))
            conn.execute(
                "INSERT INTO research_price_universe_member_spans(universe_id, ticker, added_at) VALUES (?, ?, ?);",
                (universe_id, t, now))
    return {"universe_id": universe_id, "added": added, "excluded": excluded, "already": already}


def remove_price_universe_member(universe_id: int, ticker: str, now: int) -> dict:
    """DL1 (DECISIONS #455): remove one ticker from a price universe; its span gets removed_at, never deleted.

    The ticker is matched case-insensitively against today's list. One BEGIN IMMEDIATE. Raises
    PriceUniverseNotFound, MemberNotFound (not in today's list), PriceUniverseLastMember (it is the only member).
    """
    typed = _normalize_price_ticker(ticker)
    with transaction() as conn:
        _live_price_universe_row(conn, universe_id)
        rows = conn.execute(
            "SELECT ticker, added_at FROM research_price_universe_membership WHERE universe_id = ?;",
            (universe_id,)).fetchall()
        match = [r for r in rows if r["ticker"].upper() == typed]
        if not match:
            raise MemberNotFound(f"{typed} is not a member of price universe {universe_id}")
        if len(rows) == 1:
            raise PriceUniverseLastMember(f"{match[0]['ticker']} is the only member of price universe {universe_id}")
        real, added_at = match[0]["ticker"], match[0]["added_at"]
        closed = conn.execute(
            "UPDATE research_price_universe_member_spans SET removed_at = ? "
            "WHERE universe_id = ? AND ticker = ? AND removed_at IS NULL;", (now, universe_id, real)).rowcount
        if closed == 0:   # a row written directly with no span (old probes do): record its whole span now
            conn.execute(
                "INSERT INTO research_price_universe_member_spans(universe_id, ticker, added_at, removed_at) "
                "VALUES (?, ?, ?, ?);", (universe_id, real, added_at, now))
        conn.execute(
            "DELETE FROM research_price_universe_membership WHERE universe_id = ? AND ticker = ?;",
            (universe_id, real))
    return {"universe_id": universe_id, "removed": real}


def _price_universe_members_as_of(conn, universe_id: int, as_of: int) -> dict[str, int]:
    """Map ticker -> added_at of the span that holds it at `as_of` (the one reader of the span record).

    A span counts when added_at <= as_of and it ended after as_of, or it is still open and its ticker is in
    today's list (an open span whose row was deleted directly does not count). A row of today's list with no
    open span (written directly, as old probes do) counts from its own added_at.
    """
    today = {r["ticker"]: r["added_at"] for r in conn.execute(
        "SELECT ticker, added_at FROM research_price_universe_membership WHERE universe_id = ?;",
        (universe_id,)).fetchall()}
    out: dict[str, int] = {}
    open_tickers: set[str] = set()
    for r in conn.execute(
        "SELECT ticker, added_at, removed_at FROM research_price_universe_member_spans WHERE universe_id = ?;",
        (universe_id,)).fetchall():
        if r["removed_at"] is None:
            open_tickers.add(r["ticker"])
        if r["added_at"] > as_of:
            continue
        if r["removed_at"] is None:
            if r["ticker"] in today:
                out[r["ticker"]] = r["added_at"]
        elif r["removed_at"] > as_of:
            out[r["ticker"]] = r["added_at"]
    for ticker, added_at in today.items():
        if ticker not in open_tickers and added_at <= as_of:
            out[ticker] = added_at
    return out


def price_universe_members_as_of_conn(conn, universe_id: int, as_of: int) -> list[str]:
    """Sorted members at `as_of`, read on an open research.db connection (the cross-module reader)."""
    return sorted(_price_universe_members_as_of(conn, universe_id, as_of))


def list_price_universe_members_as_of(universe_id: int, as_of: int) -> list[str]:
    """DL1: the members a price universe held at unix second `as_of`, sorted (DECISIONS #455)."""
    with get_conn() as conn:
        return price_universe_members_as_of_conn(conn, universe_id, as_of)


def price_universe_member_detail(universe_id: int, as_of: int | None = None) -> dict:
    """DL1: per member at `as_of` (default now): ticker, company name, added_at, first/last bar and bar count.

    Name: events.db research_cik_ticker_map rows from sec_company_tickers with a name, keyed by the SEC
    form of the ticker (BRK.B finds BRK-B), the row with no effective_to first, then the latest loaded_at.
    Coverage: the ready 1d price_dataset of the ticker's source with the widest requested window, then most
    rows, then lowest id; its covered_start_ts, covered_end_ts and rows. Nulls when there is none.
    Raises PriceUniverseNotFound for an unknown universe.
    """
    at = int(time.time()) if as_of is None else int(as_of)
    with get_conn() as conn:
        urow = conn.execute(
            "SELECT id, name FROM research_datasets WHERE id = ? AND dataset_type = ?;",
            (universe_id, DATASET_TYPE_PRICE_UNIVERSE)).fetchone()
        if urow is None:
            raise PriceUniverseNotFound(f"price universe {universe_id} not found")
        spans = _price_universe_members_as_of(conn, universe_id, at)
        coverage: dict[str, dict] = {}
        for ticker in spans:
            cov = conn.execute(
                "SELECT covered_start_ts, covered_end_ts, rows FROM research_datasets "
                "WHERE dataset_type = ? AND source = ? AND symbol = ? AND timeframe = '1d' AND status = 'ready' "
                "ORDER BY (end_ts - start_ts) DESC, rows DESC, id ASC LIMIT 1;",
                (DATASET_TYPE_PRICE, dataset_sources.price_source_for_ticker(ticker), ticker)).fetchone()
            coverage[ticker] = dict(cov) if cov is not None else {}
    with eventsdb.get_events_conn() as econn:
        mapped = company_profiles.cik_rows(econn, spans)
        profiles = company_profiles.profiles_for(econn, [m[0] for m in mapped.values() if m is not None])
        failures = company_profiles.failures_for(econn, [m[0] for m in mapped.values() if m is not None])
    members = []
    for t in sorted(spans):
        cik_name = mapped[t]
        profile = profiles.get(cik_name[0]) if cik_name is not None else None
        failure = failures.get(cik_name[0]) if cik_name is not None else None
        sector_missing, industry_missing = _profile_reasons(cik_name, profile, failure)
        members.append({
            "ticker": t, "name": cik_name[1] if cik_name is not None else None, "added_at": spans[t],
            "first_bar_ts": coverage[t].get("covered_start_ts"),
            "last_bar_ts": coverage[t].get("covered_end_ts"),
            "bars": coverage[t].get("rows"),
            "sector": profile["sector"] if profile else None,
            "sic": profile["sic"] if profile else None,
            "industry": profile["sic_description"] if profile else None,
            "profile_read_at": profile["read_at"] if profile else None,
            "sector_missing": sector_missing,
            "industry_missing": industry_missing,
        })
    return {"universe_id": universe_id, "name": urow["name"], "as_of": at, "members": members}


_NO_CIK_WORDS = "No SEC company number is on file for this ticker"
_NOT_READ_WORDS = "The SEC company record has not been read yet"


def _profile_reasons(cik_name, profile, failure=None) -> tuple[str | None, str | None]:
    """UNI1: why a member shows no sector and no industry, in plain words; None where the value is there.

    UNI1b: with no profile row, a failure row gives its own reason; a profile row wins over a failure row.
    """
    if cik_name is None:
        return _NO_CIK_WORDS, _NO_CIK_WORDS
    if profile is None:
        if failure is not None:
            words = f"The SEC company record could not be read: {failure['reason']}"
            return words, words
        return _NOT_READ_WORDS, _NOT_READ_WORDS
    return (None if profile["sector"] is not None else "The SEC company record names no sector office",
            None if profile["sic_description"] is not None else "The SEC company record names no industry")


def price_universe_sector_mix(include_synthetic: bool = False) -> list[dict]:
    """UNI1: per live price universe, today's members counted by sector (null for a member with none).

    By count descending, then label, null last. Synthetic-source universes are left out unless asked for.
    """
    with get_conn() as conn:
        where = "" if include_synthetic else " AND d.source != 'synthetic'"
        universes = conn.execute(
            "SELECT d.id FROM research_datasets d WHERE d.dataset_type = ? AND d.status != 'deleted'" + where +
            " ORDER BY d.id;", (DATASET_TYPE_PRICE_UNIVERSE,)).fetchall()
        members = {int(u["id"]): [] for u in universes}
        for row in conn.execute("SELECT universe_id, ticker FROM research_price_universe_membership;"):
            if int(row["universe_id"]) in members:
                members[int(row["universe_id"])].append(row["ticker"])
    with eventsdb.get_events_conn() as econn:
        every = sorted({t for ts in members.values() for t in ts})
        mapped = company_profiles.cik_rows(econn, every)
        profiles = company_profiles.profiles_for(econn, [m[0] for m in mapped.values() if m is not None])
    out = []
    for uid in sorted(members):
        counts: dict[str | None, int] = {}
        for t in members[uid]:
            cik_name = mapped[t]
            profile = profiles.get(cik_name[0]) if cik_name is not None else None
            sector = profile["sector"] if profile else None
            counts[sector] = counts.get(sector, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (kv[0] is None, -kv[1], kv[0] or ""))
        out.append({"universe_id": uid, "members": len(members[uid]),
                    "sectors": [{"sector": s, "count": n} for s, n in ordered]})
    return out


def list_runs_for_universe(universe_id: int, limit: int = 500) -> list[dict]:
    """DL1: the runs on a price universe (research_runs.dataset_id = the universe id), newest first.

    Served by ix_runs_dataset. No synthetic filter: the universe itself decides what its runs are.
    """
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            f"{_RUN_COLUMNS} FROM research_runs r "
            "LEFT JOIN research_run_stats s ON s.run_id = r.id "
            "WHERE r.dataset_id = ? ORDER BY r.created_at DESC, r.id DESC LIMIT ?;",
            (universe_id, limit),
        ).fetchall()
    return [_decode_sparkline(dict(r)) for r in rows]


def query_price_universe_members_not_in_index() -> list[dict]:
    """6.6's own t-zero monitor query (registry row price_universe_members_not_in_index): every
    membership row written BEFORE this merge, when the typed path still force-uppercased a
    store-derived spelling (e.g. 'AD_OLD' instead of 'AD_old'), so its ticker has no EXACT match
    in the index. These rows are never rewritten (a silent rewrite would be a second, hidden
    normalization pass over history no one asked for); this is a read-only count for the
    operations monitor to watch shrink as those old universes age out or are recreated. The
    counter itself EXISTS here; wiring an automatic reader into the operations page is owed
    (ops-page work is parked until October, DECISIONS #300)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT m.universe_id, m.ticker FROM research_price_universe_membership AS m "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM research_eodhd_tickers AS t WHERE t.ticker = m.ticker"
            ") ORDER BY m.universe_id, m.ticker;"
        ).fetchall()
    return [{"universe_id": r["universe_id"], "ticker": r["ticker"]} for r in rows]


def price_universe_member_counts() -> dict[int, int]:
    """Map price_universe id -> member count, for the Data-Library listing (the U1 member-count branch).

    A single GROUP BY over the membership table, the parallel to the CIK member_counts() — which reads
    the events.db research_universe_membership and returns None for a price_universe row.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT universe_id, COUNT(*) AS n FROM research_price_universe_membership GROUP BY universe_id"
        ).fetchall()
    return {int(r["universe_id"]): int(r["n"]) for r in rows}


def rename_price_universe(universe_id: int, name: str) -> bool:
    """Rename a price universe's catalog row. Returns True iff a price_universe row was updated.

    Updates BOTH `name` AND `symbol`: a price_universe row keeps `symbol == name` as the UNIQUE
    discriminator (create sets it so two universes never collide on the research_datasets UNIQUE key),
    so a rename that touched only `name` would leave a stale `symbol` — silently allowing a duplicate
    name and a later raw IntegrityError when a new universe re-uses the old name. A name collision is
    checked first and raised as PriceUniverseNameExists (a clean 409), mirroring create.
    """
    if not name or not name.strip():                            # fail loud, not a silent blank rename
        raise ValueError("price universe name cannot be blank")
    with transaction() as conn:
        clash = conn.execute(
            "SELECT id FROM research_datasets WHERE name = ? AND dataset_type = ? AND id != ?",
            (name, DATASET_TYPE_PRICE_UNIVERSE, universe_id),
        ).fetchone()
        if clash is not None:                                   # another price universe already holds this name
            raise PriceUniverseNameExists(int(clash["id"]), name)
        cur = conn.execute(
            "UPDATE research_datasets SET name = ?, symbol = ? WHERE id = ? AND dataset_type = ?",
            (name, name, universe_id, DATASET_TYPE_PRICE_UNIVERSE),  # keep symbol == name (the UNIQUE discriminator)
        )
        return cur.rowcount > 0


def set_price_universe_display_name(universe_id: int, display_name: str) -> bool:
    """UNI1: set the typed display name of a price universe. True iff a price_universe row was updated.

    A blank or over-long text raises StrategyValidationError before any write.
    """
    cleaned = _clean_display_name(display_name)
    if cleaned is None:
        raise StrategyValidationError("the display name cannot be empty")
    with transaction() as conn:
        cur = conn.execute("UPDATE research_datasets SET display_name = ?, display_name_from_name = 0 "
                           "WHERE id = ? AND dataset_type = ?;",
                           (cleaned, universe_id, DATASET_TYPE_PRICE_UNIVERSE))
        return cur.rowcount > 0


_DISPLAY_NAME_MAX = 60  # DN1: the longest display name a page has room for
# NM1a: rows created before these moments may hold a name made up from the stored name; later rows were typed through a door.
_STRATEGY_NAME_CUTOFF_TS = 1792504172   # DN1 merged (8013e2c6), 2026-10-20 15:49:32 +0200
_UNIVERSE_NAME_CUTOFF_TS = 1793461958   # the dashboard restart that deployed UNI1, 2026-10-31 15:52:38 UTC


def _plain_words_of_name(name: str) -> str:
    """DN1 backfill only: 'gen__' or 'theory__' dropped, runs of '_' and '-' to one space, first letter upper case."""
    for prefix in ("gen__", "theory__"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    words = " ".join(w for w in re.split(r"[_\-]+", name) if w)
    return words[:1].upper() + words[1:]


def _backfill_display_names(conn) -> None:
    """DN1: give each research_strategies row whose display_name is NULL the plain words of its name.

    For the rows that exist when the column is added. Idempotent: a second run finds no NULL row. New writes never
    use this (upsert_strategy stores NULL when it is given no name), so no derived name is written on a new row.
    """
    rows = conn.execute("SELECT id, name FROM research_strategies WHERE display_name IS NULL;").fetchall()
    for row in rows:
        conn.execute("UPDATE research_strategies SET display_name = ?, display_name_from_name = 1 WHERE id = ?;",
                     (_plain_words_of_name(row["name"]), row["id"]))


def _seed_strategy_id_counter(conn) -> None:
    """SID1: raise the strategy id counter to the highest id found in research_strategies and in the
    strategy_id column of every table that has one (research_deletions keeps removed strategies' ids).
    Never lowers it. Idempotent."""
    seed = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM research_strategies;").fetchone()[0])
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table';").fetchall()]
    for table in tables:
        if any(c["name"] == "strategy_id" for c in conn.execute(f'PRAGMA table_info("{table}");').fetchall()):
            seed = max(seed, int(conn.execute(f'SELECT COALESCE(MAX(strategy_id), 0) FROM "{table}";').fetchone()[0]))
    conn.execute("INSERT OR IGNORE INTO research_strategy_id_counter (one, high_id) VALUES (1, ?);", (seed,))
    conn.execute("UPDATE research_strategy_id_counter SET high_id = ? WHERE one = 1 AND high_id < ?;", (seed, seed))
    log.info("strategy id counter seeded from %s", seed)


def init_db():
    """Create tables, run column migrations, seed scopes and settings. Idempotent."""
    # Build-time gate: provenance must match the layout before we touch the DB (arch §19, fails fast).
    # OPS-1 boot fact, first half: clear the fact BEFORE the check runs, never only after it passes.
    # The comment below says init_db can run more than once in a process, which is exactly why this
    # line has to exist: with only the write-on-success half, a SECOND init_db() whose check FAILS
    # would raise past the write and leave the previous run's `passed: True` standing. The monitor
    # that reads this fact exists to catch provenance not being verified, so a stale True is the one
    # false positive it must never produce. Clearing first makes the fact mean "the most recent
    # check in this process passed" rather than "some check once passed".
    global _provenance_check_passed_at
    _provenance_check_passed_at = None
    _check_parameter_provenance()
    # OPS-1 boot fact: only recorded once the call above has returned WITHOUT raising, so a call
    # dropped by a future refactor leaves this at its "never ran" default forever instead of a
    # stale True (global because init_db can run more than once in a process; it is idempotent).
    # No `global` restatement here: it is already declared above, at the clear. A second one at this
    # point is a SyntaxError rather than a harmless redundancy, because Python requires the
    # declaration to precede every assignment of that name in the function -- and the assignment at
    # the clear is already behind us. MEASURED, on 3.14.4 and on the 3.12 the tests run under, after
    # a review pass argued a duplicate declaration should compile as redundancy:
    #     def f():
    #         global x
    #         x = None
    #         global x        # SyntaxError: name 'x' is assigned to before global declaration
    #         x = 1
    # An earlier `global` for the same name does not exempt a later one. This is how the first
    # version of the clear was caught, by py_compile, on the command that wrote it.
    _provenance_check_passed_at = int(time.time())
    # NOTE: the CLEAR that makes this honest on a re-run lives above, immediately before the check.
    # See the comment there; the two halves are a pair and moving either one alone breaks the fact.
    # M4.5 3b B2.1: the structured-bounds completeness invariant (overridable-set == bounds-set), fail-loud.
    _check_overridable_bounds()
    with get_conn() as conn:
        # Brief E: rebuild research_runs nullable-env (zero rows) BEFORE the CREATEs recreate it.
        _drop_research_runs_if_env_notnull(conn)
        conn.executescript(_DDL)
    with transaction() as conn:
        _seed_strategy_id_counter(conn)
    with transaction() as conn:
        # DL1: one open span per current membership row that has none (idempotent: the NOT EXISTS skips a
        # pair that already has an open span, so a second init_db inserts nothing).
        conn.execute(
            "INSERT INTO research_price_universe_member_spans(universe_id, ticker, added_at) "
            "SELECT m.universe_id, m.ticker, m.added_at FROM research_price_universe_membership m "
            "WHERE NOT EXISTS (SELECT 1 FROM research_price_universe_member_spans s "
            "WHERE s.universe_id = m.universe_id AND s.ticker = m.ticker AND s.removed_at IS NULL);"
        )
        # M2 brief F.1: add presentation columns to research_settings if missing.
        # `description` already exists from M2 part 1; helper no-ops if column is present.
        _add_column_if_missing(conn, 'research_settings', 'unit',  'TEXT')
        _add_column_if_missing(conn, 'research_settings', 'label', 'TEXT')
        _add_column_if_missing(conn, 'research_settings', 'description', 'TEXT')

        # M3 §3.1: cancellation flag on the pre-existing research_jobs table (only delta on an M2 table).
        _add_column_if_missing(conn, 'research_jobs', 'cancellation_requested',
                               'INTEGER NOT NULL DEFAULT 0')

        # M3 brief D (DECISIONS #20): rename the research_runs env column. Brief A created it as
        # docker_image_digest; a never-pushed local image has no registry digest, so the column now
        # holds a baked build-provenance string. Cheap + safe: research_runs has zero rows pre-brief-D.
        _rename_column_if_needed(conn, 'research_runs', 'docker_image_digest', 'build_provenance')

        # M3-close UX: user-supplied label for a run; nullable, not unique. Added via ALTER for the
        # already-deployed prod DB (research_runs has real rows now); _DDL above creates it on a
        # fresh DB.
        _add_column_if_missing(conn, 'research_runs', 'name', 'TEXT')

        # Unit-4 D2: the equity sparkline — a JSON array of up to 32 raw equity values (LTTB-32),
        # a MATERIALIZED derived column on research_runs (DDIA materialized view, kleppmann_canon
        # :888): computed once at finalize from equity.parquet, served straight from the row on the
        # hot runs-list read so no parquet is opened per row. Nullable: a non-succeeded run (and a
        # legacy run not yet backfilled) reads NULL. Additive, no table rebuild, no version bump.
        _add_column_if_missing(conn, 'research_runs', 'equity_sparkline', 'TEXT')

        # M4 D5 (AD3): the run's trial-provenance marker. NULL (legacy + ordinary runs) reads as
        # 'standard'; 'scratch' marks an editor test run (R8). Added migration-only via the same guarded
        # idempotent helper equity_sparkline used (#119), so it coexists as an INDEPENDENT nullable
        # additive column — no backfill, no table rebuild (canon :1305). Orthogonal to price-vs-event,
        # which stays derived via run_is_event; reads coalesce COALESCE(kind, 'standard'). No index at
        # M4 (the runs list caps at 500/pages 50; a WHERE kind='scratch' scan is cheap at this scale —
        # canon :988-994 is the add-an-index-later path if the row count ever makes the filter matter).
        _add_column_if_missing(conn, 'research_runs', 'kind', 'TEXT')

        # Phase-2 Unit 1 (A2b-001): the index-derived Sharpe annualization a run used, recorded so
        # cross-run Sharpe comparison is valid by construction and auditable. Three additive nullable
        # columns (DDIA Ch 5 schema evolution, canon:1116 — additive, backward/forward compatible, no
        # table rebuild, no version bump): annualization_factor (the comparable number = observed
        # bars/year), annualization_freq (the bar period), annualization_year_freq (the duration fed to
        # vbt; factor = year_freq/freq). Filled at finalize from meta.json (price runs); NULL for event
        # runs (their fixed daily 252 convention lives in event_pipeline) and legacy runs.
        _add_column_if_missing(conn, 'research_runs', 'annualization_factor', 'REAL')
        _add_column_if_missing(conn, 'research_runs', 'annualization_freq', 'TEXT')
        _add_column_if_missing(conn, 'research_runs', 'annualization_year_freq', 'TEXT')

        # U3 (pre-M4 Brief A1): index for the runs-by-name sort. It is a COMPOSITE EXPRESSION index
        # whose columns EXACTLY match runs_list_sql's NULLs-last ORDER BY — `(name IS NULL)`, then
        # `name COLLATE NOCASE` (ASC), then `id` DESC — so EXPLAIN QUERY PLAN uses it as a covering
        # index with NO temp B-tree sort. A plain `ON research_runs(name COLLATE NOCASE)` index does
        # NOT work here (verified at build): SQLite indexes ASC-order NULLs FIRST, but #5 (Cowork)
        # wants NULLs LAST, so the leading `name IS NULL` expression term forces a temp B-tree sort
        # unless the index itself carries that expression. The plain-index claim in the spec
        # (§A1.6.2 "the index still serves the non-NULL order") was a build-time defect; this
        # expression index reconciles #5 (NULLs-last) with A1-U3-2 (index used, no temp sort).
        # Created HERE (right after the name ALTER), not in _DDL, so the column is guaranteed to
        # exist before the index references it (the event_series partial index below does the same).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_runs_name "
            "ON research_runs((name IS NULL), name COLLATE NOCASE, id DESC)"
        )

        # TERM F2 (keep+orphan strategy delete): the soft-delete tombstone column on
        # research_strategies. 'active' (default, every existing row backfills to it) | 'deleted'.
        # A deleted strategy keeps its row (so orphaned runs' name join still resolves) but is hidden
        # from the rail/list and refused by the open/edit/dispatch guards. Additive, nullable-free via
        # the constant DEFAULT, idempotent through the helper — the dataset status precedent. NO index on
        # status: research_strategies is bounded (dozens to low hundreds of rows), the list reads are
        # on-demand not hot-loop, so a full scan is cheap (DDIA: add an index only when the row count
        # makes the filter matter — canon:988-994, the same call the run-kind column made).
        _add_column_if_missing(conn, 'research_strategies', 'status',
                               "TEXT NOT NULL DEFAULT 'active'")

        # DN1: the name a page shows for a strategy, typed by Tamer at every door. Nullable: NULL is the default for
        # a row that has none (DDIA ch. 5, "Schema evolution rules": add only fields that have a default). Both the
        # current row and its history snapshots carry it. The backfill below fills only the rows that exist today.
        _add_column_if_missing(conn, 'research_strategies', 'display_name', 'TEXT')
        _add_column_if_missing(conn, 'research_strategies_history', 'display_name', 'TEXT')
        # NM1a: 1 on a row whose name was made up from its stored name, never typed by a person. Added once; the one-time
        # UPDATE flags the older rows whose name equals the plain words of the name and that were created before DN1's
        # merge (a later row with the same text was typed through a door). DDIA ch. 5: NOT NULL DEFAULT 0 is backward
        # compatible. Runs before the backfill below, which flags the rows it fills itself.
        if not any(c["name"] == "display_name_from_name" for c in conn.execute("PRAGMA table_info(research_strategies);")):
            _add_column_if_missing(conn, 'research_strategies', 'display_name_from_name', 'INTEGER NOT NULL DEFAULT 0')
            for srow in conn.execute("SELECT id, name, display_name FROM research_strategies "
                                     "WHERE display_name IS NOT NULL AND created_at < ?;",
                                     (_STRATEGY_NAME_CUTOFF_TS,)).fetchall():
                if srow["display_name"] == _plain_words_of_name(srow["name"]):
                    conn.execute("UPDATE research_strategies SET display_name_from_name = 1 WHERE id = ?;", (srow["id"],))
        _backfill_display_names(conn)

        # M4.5 3b B2.2: source_note on the per-strategy override store (the D4 override justification, first
        # WRITTEN in 3b). The column is in research_strategy_settings' CREATE TABLE (born in 3a), so a DB
        # that ran 3a already has it; this guard is defense-in-depth for any DB whose table predates the
        # source_note column — idempotent, a no-op once present (the established additive-column pattern).
        _add_column_if_missing(conn, 'research_strategy_settings', 'source_note', 'TEXT')

        # M2.5 brief A (spec brief §3.8 + §3.9 R1+R2). Two discriminator columns
        # on research_datasets so the catalog can carry event-series rows alongside
        # the existing price datasets: dataset_type names the kind, event_type names
        # the event class (sec_13d | analyst_rating). Live verification at writing
        # confirmed neither column exists on the M2 table (researchdb.py lines
        # 116-133); both are nullable additions, idempotent through the helper.
        _add_column_if_missing(conn, 'research_datasets', 'dataset_type', 'TEXT')
        _add_column_if_missing(conn, 'research_datasets', 'event_type', 'TEXT')

        # Dashboard-improvements W3 (true coverage): two nullable columns holding the
        # ACTUAL first-to-last bar range of the fetched data (unix seconds UTC), captured
        # once at fetch completion. start_ts/end_ts stay the REQUESTED window (and the
        # UNIQUE key); covered_* are a derived value stored denormalized for cheap list
        # reads (DDIA systems-of-record vs derived data). Legacy rows keep NULL — the
        # frontend CoverageCell falls back to start_ts/end_ts. Idempotent via the helper.
        _add_column_if_missing(conn, 'research_datasets', 'covered_start_ts', 'INTEGER')
        _add_column_if_missing(conn, 'research_datasets', 'covered_end_ts', 'INTEGER')

        # Night four lane S (job 2.4, decision 1 of the night four plan ruled option (a) by Tamer,
        # 2026-09-08: two nullable columns on research_datasets, "the table that already owns the
        # record the fact describes"). store_id / store_fetched_at hold the EODHD store's own
        # comparable identity (research_lab/worker/store_identity.py's compute_store_identity),
        # written by the same UPDATE that already receives the worker's fetch result
        # (worker_dataset_result in routes/research.py), NULL when the worker sent none (a
        # non-eodhd fetch, or a worker built before this migration). Nullable evolution (DDIA Ch 5,
        # line 1305: "relational DBs add nullable columns cheaply, schema evolution fills missing
        # fields with nulls") on the exact covered_start_ts/covered_end_ts precedent directly
        # above: a legacy or non-eodhd row reads NULL on both, honestly absent rather than a
        # placeholder.
        _add_column_if_missing(conn, 'research_datasets', 'store_id', 'TEXT')
        _add_column_if_missing(conn, 'research_datasets', 'store_fetched_at', 'TEXT')

        # One-shot backfill: every existing row predates M2.5 and is a price dataset.
        # After this point every research_datasets row carries an explicit dataset_type
        # value (no more NULL discriminator). Idempotent — re-running matches zero rows.
        conn.execute(
            "UPDATE research_datasets SET dataset_type='price_dataset' WHERE dataset_type IS NULL"
        )

        # M5 1A (arch §4-§5, brief §4/§7): the two additive columns for existing DBs (a fresh DB gets
        # them from _DDL above — the _DDL-creates-new + migration-fixes-old pattern). _add_column_if_missing
        # cannot add a CHECK, so backtest_mode's admissible values ('independent'|'shared_capital') are
        # enforced in code at dispatch (1C), exactly as research_jobs.kind is unconstrained-at-schema and
        # validated by the worker's KIND_HANDLERS. Then seed the trial-ledger from existing research_runs.
        # The backfill runs HERE — inside this shared transaction, AFTER the dataset_type NULL-normalization
        # above — so its price-vs-else ticker branch is exhaustive (every research_datasets row now carries a
        # non-NULL discriminator). The funnel grain-widening recreate runs LATER, in its own get_conn() block
        # after this transaction commits (it toggles the FK pragma); the two touch disjoint tables.
        _add_column_if_missing(conn, 'research_runs', 'backtest_mode', "TEXT NOT NULL DEFAULT 'independent'")
        _add_column_if_missing(conn, 'research_jobs', 'progress_json', 'TEXT')
        _backfill_research_trials(conn)

        # M5 U2 (1C dispatch_sweep, AD-M5-12/13): three additive columns for existing DBs (a fresh DB gets
        # research_sweep_members from _DDL above; these columns are migration-only, the kind/annualization_*
        # precedent). All additive (DDIA Ch 5 schema evolution — no table rebuild, no version bump):
        #   - research_runs.universe_sha256 (TEXT, nullable): the content hash over the sorted member
        #     dataset_sha256 set (AD-M5-12, R-EXP), written by _finalize_materializing_sweep once the member
        #     data exists. NULL on a non-universe run and on a still-materializing sweep.
        #   - research_runs.grid_grew (INTEGER, 0/1, default 0): the executed-vs-registered reconcile flag
        #     (AD-M5-6, set by complete_sweep when the executed COMBINATION count exceeds registered_n). The
        #     NOT-NULL-DEFAULT idiom backfills every existing row to 0 (the backtest_mode precedent).
        #   - research_sweep_registrations.registered_cells (INTEGER, nullable): the real CELL denominator
        #     (registered_n x member_count), written by _finalize_materializing_sweep so the universe progress
        #     bar reads cells/cells, not the grain-mismatched combinations/cells (the §27 progress-denominator
        #     fix). NULL on an existing single-member 1B sweep, whose registered_n already equals its cell count.
        _add_column_if_missing(conn, 'research_runs', 'universe_sha256', 'TEXT')
        _add_column_if_missing(conn, 'research_runs', 'grid_grew', 'INTEGER NOT NULL DEFAULT 0')
        _add_column_if_missing(conn, 'research_sweep_registrations', 'registered_cells', 'INTEGER')

        # M5 Track B (F7, correctness spec §7): two additive nullable per-cell distribution scalars on
        # research_run_cells, persisted by the sweep at run time (sweep_portfolio_cells), read by the grid
        # SKEW column + the median-skew summary tile + the universe cross-sectional view. Additive + nullable
        # (DDIA Ch 5 — no rebuild): a legacy cell reads None (honest absence). Bounded floats, no per-cell
        # storage blowup; computed on the same own-history-sliced returns the cell's Sharpe uses. NOT added to
        # _RUN_STATS_COLUMNS (which projects the fixed 8-column research_run_stats single-run rollup) — they
        # ride a separate _CELL_DIST_COLUMNS used only on the cell write/read/summarize paths.
        _add_column_if_missing(conn, 'research_run_cells', 'skew', 'REAL')
        _add_column_if_missing(conn, 'research_run_cells', 'kurtosis', 'REAL')

        # M5 universe usability (AD-UU-3, backend brief B1): the saved test window on a price universe.
        # Two additive nullable epoch-second columns (UTC day-aligned). Set 9 night lane DX (U.10 item
        # 5b): a NULL on either side is never read as "full history" for any row, of any dataset_type;
        # it means the bound is unknown, and the tracker's own R-T15 refuses a draw against it. A
        # price_universe row gets both bounds at creation from here on (create_price_universe); a
        # price_dataset row gets both from the coverage the runner measured (worker_dataset_result,
        # backfill_dataset_windows_from_coverage); every other type stays NULL until it too has a real
        # source for a window (DDIA Ch 5 nullable evolution: no rewrite, backward and forward
        # compatible). The window is applied COLD at backtest time by slicing the input data before
        # signal generation (the worker, B4). Slicing at backtest time never trims the file on disk:
        # the parquet a ticker's own fetch wrote keeps everything it ever held, window or no window
        # (Tamer decision 2), so widening or removing a window later still finds the same rows.
        _add_column_if_missing(conn, 'research_datasets', 'window_start_ts', 'INTEGER')
        _add_column_if_missing(conn, 'research_datasets', 'window_end_ts', 'INTEGER')

        # UNI1: the name a page shows for a price universe, typed at create and on edit (DECISIONS #462 item 4).
        # Added only when missing; the plain words of the name go to the rows that exist at that moment, once.
        # A universe made later with no display name keeps NULL through every later init.
        if not any(c["name"] == "display_name" for c in conn.execute("PRAGMA table_info(research_datasets);")):
            _add_column_if_missing(conn, 'research_datasets', 'display_name', 'TEXT')
            for urow in conn.execute("SELECT id, name FROM research_datasets WHERE dataset_type = ?;",
                                     (DATASET_TYPE_PRICE_UNIVERSE,)).fetchall():
                conn.execute("UPDATE research_datasets SET display_name = ? WHERE id = ?;",
                             (_plain_words_of_name(urow["name"]), urow["id"]))
        # NM1a: the same flag for price universes, set once on the older rows whose name equals the plain words.
        if not any(c["name"] == "display_name_from_name" for c in conn.execute("PRAGMA table_info(research_datasets);")):
            _add_column_if_missing(conn, 'research_datasets', 'display_name_from_name', 'INTEGER NOT NULL DEFAULT 0')
            for urow in conn.execute("SELECT id, name, display_name FROM research_datasets "
                                     "WHERE dataset_type = ? AND display_name IS NOT NULL AND created_at < ?;",
                                     (DATASET_TYPE_PRICE_UNIVERSE, _UNIVERSE_NAME_CUTOFF_TS)).fetchall():
                if urow["display_name"] == _plain_words_of_name(urow["name"]):
                    conn.execute("UPDATE research_datasets SET display_name_from_name = 1 WHERE id = ?;", (urow["id"],))
        # M5 universe usability (AD-UU-4, backend brief B1): the single-combo flag — set when a strategy
        # declares zero sweep parameters, so the worker runs each ticker once at variation_id=0 (the
        # explicit signal that relaxes the three empty-grid fail-loud gates; an empty grid alone never does).
        _add_column_if_missing(conn, 'research_sweep_registrations', 'single_combo', 'INTEGER NOT NULL DEFAULT 0')

        # M5 Track C (C2 §8.2 / C-metrics §6, correctness spec §4.6): the two F6 portfolio metrics persisted
        # at run time on the single-run rollup. Additive + nullable (DDIA Ch 5): a legacy run predating them
        # reads None (honest absence). The worker extracts them in build_run_result (the single pf-read site,
        # C2 §2 fold 2), so every RunResult.stats carries them — closing F6 for the single-run persist AND the
        # zero/inline cell re-run uniformly. NOT added to _RUN_STATS_COLUMNS (the fixed-8 cell-write tuple) —
        # they ride a separate single-run rollup write; a cell never persists F6 (read from the re-run pf).
        _add_column_if_missing(conn, 'research_run_stats', 'max_gross_exposure', 'REAL')
        _add_column_if_missing(conn, 'research_run_stats', 'turnover', 'REAL')

        # Lane WF (6.2b, DECISIONS #392 ruling 4): the walk-forward stage's own run-level rollup.
        # Additive + nullable (DDIA Ch 5, the max_gross_exposure/turnover precedent immediately
        # above): a run predating this unit, or one that never ran the walkforward stage, reads
        # NULL on all three: an honest absence, never a zero standing in for "not walk-forward
        # graded". in_sample_profit/out_of_sample_profit are the ANNUALISED run-level figures AC-17
        # needs (never the raw per-slice sum: see walkforward.py and run_sweep_entrypoint.py's own
        # _process_walkforward); wfa_boundary_buffer_bars is purge_bars + embargo_bars, the input
        # judge_checks.check_2_walk_forward's own guard G1 (a positive sourced boundary length) reads.
        _add_column_if_missing(conn, 'research_run_stats', 'in_sample_profit', 'REAL')
        _add_column_if_missing(conn, 'research_run_stats', 'out_of_sample_profit', 'REAL')
        _add_column_if_missing(conn, 'research_run_stats', 'wfa_boundary_buffer_bars', 'INTEGER')

        # Lane LB (5.8e, `DECISIONS.md` #406 ruling 2): the last-bar fill reaches one bar, for the
        # fill only. Additive + nullable (DDIA Ch 5, Encoding and Evolution, the Dataflow through
        # databases section, quoted verbatim: "data outlives code." Old rows persist in their
        # original encoding until rewritten. LSM-tree compaction rewrites on its own; relational
        # DBs add nullable columns cheaply, schema evolution fills missing fields with nulls. --
        # exactly the idiom the three walk-forward columns immediately above already use). Three columns total
        # for this lane, one here and two on research_walkforward_slices below: `entries_masked_at_
        # end` HERE is the RUN-LEVEL figure, written by the single run's own complete_run stats
        # write and by the walk-forward upsert's own sum over its slices (record_walkforward_
        # result); `fill_reach_ts` and `entries_masked_at_end` on research_walkforward_slices below
        # are the PER-SLICE figures the run-level one sums. A row written before this merge reads
        # NULL on all three, which the bundle renders as "not recorded" and never as a fabricated
        # zero -- the SAME "data outlives code" reading Ch 5 gives an old row under a newer schema.
        _add_column_if_missing(conn, 'research_run_stats', 'entries_masked_at_end', 'INTEGER')

        # Lane LB (5.8e), the per-slice half of the same three-column migration: `fill_reach_ts`
        # (whole UTC epoch seconds, the reached bar's own timestamp, NULL when this slice's own
        # last-bar entry took the fallback or when neither leg had one at all) and
        # `entries_masked_at_end` (this slice's own count, summed over its in-sample and
        # out-of-sample legs) on research_walkforward_slices, written by _insert_walkforward_slices
        # from the real entrypoint's own per-slice emit. Additive + nullable, same DDIA reading as
        # research_run_stats's own sibling column immediately above.
        _add_column_if_missing(conn, 'research_walkforward_slices', 'fill_reach_ts', 'INTEGER')
        _add_column_if_missing(conn, 'research_walkforward_slices', 'entries_masked_at_end', 'INTEGER')

        # M5 Track C (C2 §2 folds 3+4, PINNED): the frozen-registration record gains, additive + nullable, the
        # window freeze (a COPY of the MUTABLE research_datasets window, so the cell re-run reads the FROZEN
        # window not the live universe row), the param_mode (the three-way re-run discriminator: 'zero' |
        # 'vectorized' | 'inline' — single_combo alone is only a 2-way split), and the benchmark freeze
        # (C-bench §3: benchmark_ticker + benchmark_dataset_id pre-committed at phase-1 dispatch, benchmark_sha256
        # the phase-2 content pin). One coherent additive-nullable evolution; a legacy registration reads NULL on
        # all of them. The dispatch_sweep WRITES (the freeze) + the catalog/seed/compute land in C.2/C-bench.
        _add_column_if_missing(conn, 'research_sweep_registrations', 'window_start_ts', 'INTEGER')
        _add_column_if_missing(conn, 'research_sweep_registrations', 'window_end_ts', 'INTEGER')
        _add_column_if_missing(conn, 'research_sweep_registrations', 'param_mode', 'TEXT')
        _add_column_if_missing(conn, 'research_sweep_registrations', 'benchmark_ticker', 'TEXT')
        _add_column_if_missing(conn, 'research_sweep_registrations', 'benchmark_dataset_id', 'INTEGER')
        _add_column_if_missing(conn, 'research_sweep_registrations', 'benchmark_sha256', 'TEXT')

        # #376 ruling 4 (the engine-and-sizing-provenance unit, night three, DECISIONS #370): three
        # additive nullable columns on research_runs, none on research_run_cells (Tamer's ruling,
        # accepted as a DEVIATION from the ideas brief's "every result and every cell" -- see this
        # unit's own report for the reasoning). Nullable evolution (DDIA Ch 5, the skew/kurtosis
        # precedent immediately above): a legacy row predating this unit reads NULL, never a
        # placeholder. sizing_method is a plain TEXT column because it IS queried by value (the
        # runs_missing_engine_provenance monitor, the unknown-method-refusal count); sizing_spec and
        # cost_model are one JSON document each (settings_snapshot's own idiom, DDIA Ch 3 locality):
        # each is one self-contained fact read whole, never queried by an inner field. Sizing is
        # uniform across a whole run today (no entrypoint passes a per-combo sizing), so the RUN is
        # the grain at which this fact exists; a per-cell column arrives additively when per-combo
        # sizing does, following run_id to inherit the run's own provenance until then (no join, no
        # per-cell copy of a fact that cannot yet vary within a run).
        _add_column_if_missing(conn, 'research_runs', 'sizing_method', 'TEXT')
        _add_column_if_missing(conn, 'research_runs', 'sizing_spec', 'TEXT')
        _add_column_if_missing(conn, 'research_runs', 'cost_model', 'TEXT')

        # Lane DF2 (`6.1e`, set 9a night, DECISIONS #395 rulings 1 and 2): two additive nullable
        # columns on research_run_variations, the per-combo Udf/Odf inputs check 1 (the evidence
        # floor, Pardo's Rdf = 100 x [1 - (Udf/Odf)], PDF page 163/164/326, book page 130-131/293)
        # needs to grade a run from its own record instead of always refusing. DDIA Ch 5 nullable
        # evolution (the skew/kurtosis and sizing_method/sizing_spec precedents immediately above):
        # a row written before this migration, or a run whose strategy never declared either
        # contract method, reads NULL on both, an honest absence, never a substituted number.
        # lookback_bars is the strategy's own declared walk-forward lookback (Strategy.lookback_bars,
        # lane WF); used_degrees_of_freedom is Pardo's own Udf (Strategy.used_degrees_of_freedom,
        # this lane). Both are recorded PER COMBO by the vectorized sweep entrypoint at emit time
        # (the backend cannot execute a strategy itself to compute them at grading time), so they
        # ride the existing per-variation row rather than a new table: one fact per (run_id,
        # variation_id), the same grain param_json already carries.
        _add_column_if_missing(conn, 'research_run_variations', 'lookback_bars', 'INTEGER')
        _add_column_if_missing(conn, 'research_run_variations', 'used_degrees_of_freedom', 'INTEGER')

        # Partial unique index supporting §9's UPSERT key (dataset_type='event_series',
        # event_type, source). Price-dataset rows have dataset_type='price_dataset' so
        # they are not in this index; arbitrary (event_type, source) collisions among
        # price rows are unaffected. Spec brief §9 / §3.9 R1+R2; step-3 finalization
        # of arch §13's partial-index delegation (not an arch amendment).
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_research_datasets_event_series
                  ON research_datasets(event_type, source)
                  WHERE dataset_type = 'event_series'"""
        )

        # M3.75 FUND-1 §4.3: the fundamentals catalog row is keyed on (source) alone — one
        # row per fundamentals source, and there is one source (sec_edgar), so one row total.
        # event_type stays NULL on it (a fundamentals source has no event type), which is why
        # the key is (source), not (event_type, source): SQLite treats NULLs as distinct, so a
        # (event_type, source) index would not constrain a NULL-event_type row to one. Mirrors
        # uq_research_datasets_event_series; price/event-series rows have a different
        # dataset_type so they are not in this partial index and cannot collide.
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_research_datasets_fundamentals
                  ON research_datasets(source)
                  WHERE dataset_type = 'fundamentals'"""
        )

        # TERM M4.5 fold (brief 3a B1.6 / arch §13 R1, DECISIONS #161): rename the settings scope
        # 'newsbot_v3' -> 'event_strategy' (a scope names a stable method/concept, never a strategy
        # display name). DATA-TOUCHING (not additive): existing research_settings + history rows under
        # the old scope are renamed so a user's edited values persist under the new scope. Guarded by the
        # WHERE clause and idempotent — after the rename no row matches, so a re-run is a no-op (DDIA Ch 8
        # atomicity). It runs HERE, before _seed_settings_layout re-seeds the event_strategy.* rows, so
        # the rename never collides with a freshly-seeded (event_strategy, key) row on the PK. The layout
        # seed scope, the parameter_provenance.md headings, the two strategy settings_scope attributes, and
        # the frontend settingLabel map are all renamed in lockstep in THIS commit so the boot bijection
        # (_check_parameter_provenance) holds. Run snapshots (research_runs.settings_snapshot) are immutable
        # and NOT touched; the override-aware drift path aliases the old scope (export.py).
        conn.execute(
            "UPDATE research_settings SET scope = 'event_strategy' WHERE scope = 'newsbot_v3'"
        )
        conn.execute(
            "UPDATE research_settings_history SET scope = 'event_strategy' WHERE scope = 'newsbot_v3'"
        )

        _seed_settings_layout(conn)

        # Phase-2 Unit 4 (A1c-005): delete the dead 'paths'/'data_root' setting. The worker reads
        # RESEARCH_DATA_ROOT env / a file-relative root (hard_delete_dataset.py), never this DB row,
        # so UI edits silently no-op. _seed_settings_layout is INSERT-OR-IGNORE only and never prunes
        # a row absent from the layout, so the existing prod row must be deleted here. Guarded to the
        # exact (scope, key) so it can only ever touch the dead paths row; idempotent (after the first
        # run no row matches). It runs AFTER the seed so the removed layout key is never re-added.
        # research_strategy_settings carried no override for it (it was overridable=False), but the
        # delete is written so it is correct regardless.
        conn.execute("DELETE FROM research_settings WHERE scope = 'paths' AND key = 'data_root'")
        conn.execute("DELETE FROM research_settings_history WHERE scope = 'paths' AND key = 'data_root'")
        conn.execute("DELETE FROM research_strategy_settings WHERE scope = 'paths' AND key = 'data_root'")
        conn.execute("DELETE FROM research_setting_scopes WHERE scope = 'paths'")

    # M2.5 brief F (spec brief §6.7 / §3.9 R3). Polymorphic share-table rebuild
    # runs OUTSIDE the R1+R2 transaction above because the helper opens its own
    # BEGIN IMMEDIATE (SQLite forbids nested BEGINs) and toggles PRAGMA
    # foreign_keys (per-connection switch that cannot change inside a
    # transaction). Idempotent — first call performs the CREATE/INSERT-SELECT/
    # DROP/RENAME rebuild; subsequent calls detect the already-polymorphic
    # shape via PRAGMA table_info and no-op.
    with get_conn() as conn:
        _ensure_research_shared_runs_polymorphic(conn)

    # Set 9 night lane DX (U.10 item 5a, DECISIONS #399 sheet ruling 1): a ONE-TIME catch-up for
    # every price_dataset row that readied BEFORE the UPDATE above started writing the window in
    # the same statement as the coverage. Idempotent by its own WHERE clause (only a row with a
    # NULL window and a real, already-measured coverage is touched, so a second call touches zero
    # rows), so it is safe to run on every boot rather than once behind a migration flag. This is
    # the honest source named in the plan's own sheet item 1: the coverage is the first and last
    # bar the runner already read from the immutable file it wrote (fetch.py's covered_range), so
    # copying it into the window is not a new measurement, only a delayed write of one already
    # taken. A row with no coverage (a failed or pre-W3 fetch) stays NULL and stays refused by the
    # tracker's own R-T15, never guessed.
    with transaction() as conn:
        backfilled = backfill_dataset_windows_from_coverage(conn)
    log.info("backfilled price dataset windows from measured coverage", extra={"backfilled_count": backfilled})

    # M5 1A (arch §12, ruling 4): the research_run_funnel grain-widening recreate. Runs OUTSIDE the
    # transaction above for the same reason as the polymorphic rebuild — its own BEGIN IMMEDIATE +
    # PRAGMA foreign_keys toggle. On a fresh DB it no-ops (the funnel is born new-grain from _DDL); on an
    # OLD-grain prod DB it rebuilds at the (run_id, variation_id, ticker, row_scope) grain. Idempotent.
    with get_conn() as conn:
        _ensure_research_run_funnel_m5_grain(conn)
        # P2g: a table already at the M5 grain (or just rebuilt above, whose __new already carries it) gets
        # lead_in_bars as a NULLable last column; every existing row keeps NULL (not recorded). DDIA: adding
        # a NULLable column is cheap and old rows stay readable. No-op when the column exists.
        _add_column_if_missing(conn, "research_run_funnel", "lead_in_bars", "INTEGER")

    # M5 U0 (AD-M5-14): fill the EODHD availability index from the committed manifest. Runs LAST,
    # after the table exists (created by executescript above) and outside the migration transaction
    # (it owns its own full-replace transaction). Non-fatal if the manifest is absent (logged warning).
    load_eodhd_tickers()

    # M5 Track C C-bench (§2.1): seed research_benchmark_catalog from the committed seed. AFTER
    # load_eodhd_tickers so the availability index it validates each ticker against is filled; boot-safe
    # (a seed ticker absent from the index is logged + skipped, never a crash; its own transaction).
    load_benchmark_catalog()

    # Night four lane S (job 2.4): import every committed theory file, once per content sha. Runs
    # LAST, after research_theories exists (created by the executescript above), in its own
    # transaction. UNLIKE the two best-effort loads directly above, this one is FAIL-LOUD (10.4's
    # own design: "a missing directory raises by name at init and the report says so") -- a missing
    # theories directory must never read as "zero theories exist", the same reasoning
    # _check_parameter_provenance already applies to a missing provenance heading.
    with transaction() as conn:
        import_theory_files(conn, THEORIES_DIR)


# =============================================================
# Night four lane S (job 2.4): the theory store, the theory-file import, and Tamer's REJECT event
# on a graded verdict (U.5). Three additive, append-only pieces; see the _DDL comments above for
# the full DDIA reasoning.
# =============================================================

def import_theory_files(conn, theories_dir: Path) -> None:
    """Import every '*.md' theory file under theories_dir into research_theories, once per content
    sha (INSERT OR IGNORE against the sha256 UNIQUE column, so a second call, or a call after a
    file's content is unchanged, adds no row -- idempotent by construction, no separate dedup
    check needed).

    theory_id is the file's stem, matching research_lab/generator/theory.py's own
    Theory.theory_id, so a row here and a research_generated_candidates row that cites the same
    sha256 describe the same file. sha256 is hashlib.sha256(raw file bytes), the exact hash
    theory.py computes at generation time (theory.py: `raw = path.read_bytes(); theory_sha256 =
    hashlib.sha256(raw).hexdigest()`), so a mismatch here is a real bug (the
    theory_import_sha_mismatch monitor) and not a normalisation artefact.

    Fails loud: raises FileNotFoundError, naming the path, when theories_dir does not exist. A
    missing import root must never look like "zero theories exist", the same fail-loud
    discipline _check_parameter_provenance already applies to a missing provenance heading. conn
    is an open connection (the caller's own transaction, matching this module's other
    conn-accepting writers)."""
    if not theories_dir.is_dir():
        raise FileNotFoundError(f"theories directory not found: {theories_dir}")
    imported_at = datetime.now(timezone.utc).isoformat()
    # sorted() so import order is deterministic across runs (matters only for created_at ties,
    # never for which rows exist, since INSERT OR IGNORE is keyed on content sha regardless of order).
    for path in sorted(theories_dir.glob("*.md")):
        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO research_theories (theory_id, sha256, content, source, created_at) "
            "VALUES (?, ?, ?, 'file_import', ?);",
            (path.stem, sha256, raw.decode("utf-8"), imported_at),
        )


def save_theory(theory_id: str, content: str, source: str = 'editor') -> tuple[str, str]:
    """The editor's own write path (U.4, lane GP, criterion 1, AMENDED by the lead's round-2 read
    F1), beside import_theory_files above. Append-only, the same INSERT OR IGNORE by sha256 idiom:
    saving the SAME (theory_id, content) pair twice writes one row. Saving the SAME theory_id with
    DIFFERENT content is a NEW version row (never an UPDATE, matching this table's own DDIA event-
    log comment above), which is the fix for F1: the caller now names the theory's own identity
    instead of one being derived from the content's own hash, so an edited-and-resaved theory
    keeps its identity across versions and GET /theories can group them, rather than every edit
    minting a brand-new theory nobody asked for.

    theory_id is the route's job to validate against the slug shape (this function trusts its
    caller, matching every other write function in this module); it is never derived here.

    research_theories.sha256 carries a GLOBAL UNIQUE constraint (not scoped per theory_id, see the
    DDL comment above), so when this content's sha256 ALREADY exists under a DIFFERENT theory_id,
    the INSERT OR IGNORE is a no-op and this function reads back and returns THAT existing row's
    own theory_id, never the caller's mismatched one: the row that now holds this content is the
    truth, not the identity the caller happened to ask for. Returns (theory_id, sha256) of that
    row, so a caller that asked for one identity but got another back can tell the difference.

    Opens its own transaction (unlike import_theory_files(conn, ...), which is always called
    inside init_db's own transaction): this is a standalone route-called write with no existing
    transaction to join, the same shape record_verdict_rejection's own conn-less siblings use
    elsewhere in this file when the plan names them with no conn parameter.
    """
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()  # content identity (theory.py's own rule)
    saved_at = datetime.now(timezone.utc).isoformat()  # ISO-8601 UTC, matching import_theory_files' own stamp
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO research_theories (theory_id, sha256, content, source, created_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (theory_id, sha256, content, source, saved_at),
        )
        # Read back the row that now holds this sha256 (same connection, so this sees the write
        # above even before COMMIT): the row's OWN theory_id, which may differ from the caller's
        # if the content already existed under another identity (see docstring above).
        row = conn.execute(
            "SELECT theory_id FROM research_theories WHERE sha256 = ?;",
            (sha256,),
        ).fetchone()
    return row["theory_id"], sha256


def list_theories() -> list[dict]:
    """Every imported theory version, newest first. Read-only, matching get_dataset's own
    dict-per-row return shape (row_factory=sqlite3.Row, converted to a plain dict here so a
    caller never depends on the sqlite3.Row type)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, theory_id, sha256, content, source, created_at "
            "  FROM research_theories ORDER BY created_at DESC, id DESC;"
        ).fetchall()
    return [dict(row) for row in rows]


def get_theory_by_sha(sha256: str) -> dict | None:
    """The one row whose content hash equals sha256, or None if no such row exists. sha256 is
    UNIQUE on this table, so this is always a single-row lookup, never a list."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, theory_id, sha256, content, source, created_at "
            "  FROM research_theories WHERE sha256 = ?;",
            (sha256,),
        ).fetchone()
    return dict(row) if row is not None else None


class DuplicateVerdictRejection(RuntimeError):
    """record_verdict_rejection found a rejection already recorded for this verdict_id (10.1's own
    decision: "UNIQUE (verdict_id) makes at most one rejection per verdict a database fact").
    Carries the verdict_id (StrategyNameExists' own precedent in this file: carry the id so the
    caller's HTTP response can name it) so a future U.5 route can render a 409 naming the id."""

    def __init__(self, verdict_id: int):
        self.verdict_id = verdict_id
        super().__init__(f"verdict {verdict_id} already has a recorded rejection")


def record_verdict_rejection(conn, verdict_id: int, run_id: int, rejected_by: str, reason: str | None) -> int:
    """Insert one rejection row for verdict_id (U.5's future REJECT button). conn is an open
    connection (the caller's own transaction, matching judgedb.record_verdict's own conn-passed
    shape). Returns the new row's integer id.

    Raises DuplicateVerdictRejection, naming the id, on a SECOND call for the same verdict_id,
    UNLIKE record_verdict's own ON CONFLICT DO NOTHING silent skip, because a second REJECT press
    for the same verdict is a caller bug (a double-click, a stale page re-post) this function
    surfaces loudly, not a legitimate race between two concurrent graders the way two grading
    passes racing IS (10.1's own reasoning: "a rejection is Tamer's later event on it", one
    human's own click, never two workers racing to grade the same cell). Any OTHER IntegrityError
    (a NOT NULL column left empty by the caller) is raised AS ITSELF, never relabelled: see the
    comment on the except clause below (night four lane S round 2, finding S-R1-SF1)."""
    rejected_at = datetime.now(timezone.utc).isoformat()
    try:
        cursor = conn.execute(
            "INSERT INTO research_verdict_rejections (verdict_id, run_id, rejected_by, reason, rejected_at) "
            "VALUES (?, ?, ?, ?, ?);",
            (verdict_id, str(run_id), rejected_by, reason, rejected_at),
        )
    except sqlite3.IntegrityError as exc:
        # This table declares FOUR NOT NULL columns (verdict_id, run_id, rejected_by, rejected_at),
        # not just the UNIQUE on verdict_id, so an IntegrityError here is not always the duplicate
        # this function exists to name (S-R1-SF1: U.5 has yet to wire rejected_by to an
        # authenticated identity, so a caller that leaves it empty was wrongly told "already
        # rejected"). We READ SQLITE'S OWN MESSAGE rather than assume, since it names the exact
        # constraint that fired: verified empirically against this build's sqlite3 (3.46.1) on
        # this table's own DDL, a real UNIQUE conflict on this column reads EXACTLY
        # "UNIQUE constraint failed: research_verdict_rejections.verdict_id", and nothing else
        # produces that string. Only that exact message becomes DuplicateVerdictRejection; a bare
        # `raise` re-raises anything else (a NOT NULL violation naming its own column) unchanged,
        # so a caller bug surfaces as the bug it is rather than as a false duplicate.
        if str(exc) == "UNIQUE constraint failed: research_verdict_rejections.verdict_id":
            raise DuplicateVerdictRejection(verdict_id) from exc
        raise
    return cursor.lastrowid


# ---- Night four lane S: the three monitor queries docs/units/S/monitor_rows.md describes. Each
# is a QUERY over durably recorded rows (tracker.py's own R-T12: "every such signal is a QUERY
# over a durably recorded row, never a process-local integer"), matching
# generated.query_generated_rows_without_candidate's own convention: called by an ops-monitor tick
# (owed, October), which supplies its own connection.

def query_datasets_missing_store_identity(conn, since_ts: int) -> int:
    """Count of ready eodhd price-dataset rows fetched at or after since_ts (unix seconds UTC, the
    merge time this monitor is meant to be read from) whose store_id is still NULL. Expected zero
    after the merge: every eodhd fetch computes and stamps an identity (fetch_dataset.py's own
    AC-7), so a NULL here past the merge means a worker built before this migration ran, or a bug
    in the write path. A PRE-merge row is expected NULL (the migration is additive, not a
    backfill) and is excluded by the since_ts filter on purpose, never counted as a failure."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM research_datasets "
        " WHERE dataset_type = 'price_dataset' AND source = 'eodhd' AND status = 'ready' "
        "   AND store_id IS NULL AND last_fetched_at >= ?;",
        (since_ts,),
    ).fetchone()
    return row["n"]


def query_theory_import_sha_mismatches(conn) -> int:
    """Count of research_theories rows whose stored sha256 disagrees with a FRESH re-hash of the
    CURRENT file on disk sharing the same theory_id (10.4's own named failure: "the import hashes
    normalised text while the generator hashes raw bytes"). Expected zero always: import_theory_files
    hashes the exact raw bytes theory.py itself hashes, so a mismatch here is a real bug, never
    drift from a legitimately re-saved file (a re-save produces a NEW row with a NEW sha, since
    this table is append-only, never a changed sha on the old row). A theory_id whose file has
    since been removed is skipped, not counted: this monitor answers "did the import hash the
    file correctly", not "does every imported file still exist on disk"."""
    rows = conn.execute("SELECT theory_id, sha256 FROM research_theories;").fetchall()
    mismatches = 0
    for row in rows:
        file_path = THEORIES_DIR / f"{row['theory_id']}.md"
        if not file_path.is_file():
            continue
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != row["sha256"]:
            mismatches += 1
    return mismatches


def query_verdict_rejections_without_verdict(conn) -> int:
    """Count of research_verdict_rejections rows whose verdict_id names no row in
    research_judge_verdicts (a soft reference, no FK: 10.1's own reasoning, the two tables are
    created by different DDL blocks but live in the SAME sqlite file, judgedb.DB_PATH being
    RESEARCH_DB_PATH itself, so one connection can query across them). Expected zero:
    record_verdict_rejection's own future caller (U.5) is expected to validate the verdict exists
    before calling it; a nonzero count means that validation was skipped somewhere. Raises
    sqlite3.OperationalError naming the missing table if judgedb.init_judge_db() has never run in
    this process against this file; that is a real precondition failure this function does not
    paper over, matching this whole module's no-fallback discipline."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM research_verdict_rejections AS rvr "
        " WHERE NOT EXISTS (SELECT 1 FROM research_judge_verdicts AS rjv WHERE rjv.id = rvr.verdict_id);"
    ).fetchone()
    return row["n"]


# =============================================================
# M3 brief C, strategy registration (DECISIONS #18)
#
# Shared write path used by the CLI now and by brief G's PUT later. Lives here,
# in the DB+domain layer, so both entry points version and snapshot identically.
# Does NOT broadcast SSE — the caller owns the event (the CLI prints; the PUT
# fires research.strategy_registered), because the CLI runs out-of-process from
# the FastAPI app and cannot reach the in-process broadcaster.
# =============================================================

# Max accepted strategy source size: 1 MiB (L16 / STATE.md brief-C rule).
_MAX_STRATEGY_SOURCE_BYTES = 1024 * 1024


class StrategyValidationError(ValueError):
    """Raised when a strategy source fails one of the four brief-C validation rules."""


class StrategyIdCounterMissing(RuntimeError):
    """SID1: research_strategy_id_counter has no row, so a new strategy id cannot be given safely."""


class StrategyNameExists(RuntimeError):
    """upsert_strategy(create_only=True) found the name already registered (pre-M4 Brief A1 / D5).

    Carries the existing strategy id so the create route can echo it in its 409 and point the
    operator at the edit screen."""

    def __init__(self, strategy_id: int):
        self.strategy_id = strategy_id  # the existing strategy's id, surfaced in the 409 body
        super().__init__(f"strategy name already exists (id={strategy_id})")


def _event_types_from_tree(tree: ast.Module) -> tuple[str, ...]:
    """Read a strategy's declared `event_types` off its AST (M3.5 brief I, OPEN-4 / EL47).

    The backend never imports strategy code (the sandbox boundary: only ast.parse, never
    exec), so `event_types` is read as the LITERAL the source declares — a class attribute
    `event_types = ("sec_13d", "analyst_rating")` on the EventStrategy subclass. event_types
    is a fact about the strategy, kept in one place (DDIA: one fact represented once;
    same as DECISIONS #18's description-derived-from-source), so dispatch reads it from the
    source it already holds rather than have the caller restate it.

    Contract (enforced here, also at validate_strategy_source so a bad source never stores):
    every `event_types` assignment must be a literal tuple of string literals. A computed or
    non-string value is rejected loudly (no fallback). Returns the declared tuple, or () when
    no class declares it (a price strategy like ma_cross). Raises StrategyValidationError on a
    malformed declaration or if two classes declare DIFFERENT non-empty tuples (ambiguous).
    """
    # Scans EVERY class that declares event_types, not just EventStrategy subclasses: the AST cannot
    # resolve a base class without importing (the deliberate no-exec boundary), so the literal-tuple
    # contract applies to any class declaring the name. Harmless — a non-strategy class declaring an
    # event_types attribute is exotic, and constraining it to a literal tuple of strings is benign.
    decls: list[tuple[str, ...]] = []  # one entry per class that declares event_types
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:  # only the class's own body, not nested defs
            # Either `event_types = (...)` (Assign) or `event_types: tuple[...] = (...)` (AnnAssign).
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "event_types" for t in stmt.targets
            ):
                value = stmt.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                  and stmt.target.id == "event_types" and stmt.value is not None):
                value = stmt.value
            else:
                continue
            # The value MUST be a literal tuple/list of string literals (the validated contract).
            if not isinstance(value, (ast.Tuple, ast.List)) or not all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                raise StrategyValidationError(
                    f"event_types in class {node.name!r} must be a literal tuple of string "
                    "literals (e.g. event_types = (\"sec_13d\", \"analyst_rating\")); a computed "
                    "or non-string value is not allowed (M3.5 brief I, OPEN-4 contract)"
                )
            decls.append(tuple(e.value for e in value.elts))
    non_empty = [t for t in decls if t]  # the base-class `= ()` and price strategies contribute nothing
    if not non_empty:
        return ()
    if len(set(non_empty)) > 1:  # two classes declaring different non-empty sets is ambiguous
        raise StrategyValidationError(
            "more than one class declares a different non-empty event_types; the strategy's "
            "event types are ambiguous (M3.5 brief I)"
        )
    return non_empty[0]


def extract_event_types(source_code: str) -> tuple[str, ...]:
    """Parse a strategy source and return its declared `event_types` literal (M3.5 brief I).

    Thin wrapper over _event_types_from_tree for the dispatch path. Raises
    StrategyValidationError on a malformed declaration (the same contract validate_strategy_source
    enforces at write time, so a stored source always parses cleanly here).
    """
    return _event_types_from_tree(ast.parse(source_code))


def _str_tuple_attr_from_tree(tree: ast.Module, attr: str, example: str) -> tuple[str, ...]:
    """Read a class-attribute literal tuple of strings off an AST (M3.75 FUND-4 §9, the dispatch read).

    The generalized form of the `event_types` literal read, used for `fundamental_concepts` (the
    concept set a strategy's FundamentalFeatureDefs need, §8). Kept separate from
    _event_types_from_tree so that locked M3.5 reader stays byte-unchanged (R19); the validation
    contract is identical — a class attribute `<attr> = ("a", "b")` must be a literal tuple/list of
    string literals, read with NO exec (the sandbox boundary). Returns the declared tuple, or () when
    no class declares it (every price/event-only strategy). Raises StrategyValidationError on a
    malformed declaration or two classes declaring DIFFERENT non-empty tuples (ambiguous).
    """
    decls: list[tuple[str, ...]] = []  # one entry per class that declares the attribute
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:  # only the class's own body
            # Either `<attr> = (...)` (Assign) or `<attr>: tuple[...] = (...)` (AnnAssign).
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == attr for t in stmt.targets
            ):
                value = stmt.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                  and stmt.target.id == attr and stmt.value is not None):
                value = stmt.value
            else:
                continue
            # The value MUST be a literal tuple/list of string literals (the same contract event_types uses).
            if not isinstance(value, (ast.Tuple, ast.List)) or not all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                raise StrategyValidationError(
                    f"{attr} in class {node.name!r} must be a literal tuple of string literals "
                    f"(e.g. {attr} = ({example!r},)); a computed or non-string value is not allowed "
                    "(M3.75 FUND-4)"
                )
            decls.append(tuple(e.value for e in value.elts))
    non_empty = [t for t in decls if t]  # the base-class `= ()` and strategies without it contribute nothing
    if not non_empty:
        return ()
    if len(set(non_empty)) > 1:  # two classes declaring different non-empty sets is ambiguous
        raise StrategyValidationError(
            f"more than one class declares a different non-empty {attr}; the strategy's "
            f"{attr} is ambiguous (M3.75 FUND-4)"
        )
    return non_empty[0]


def _sweep_param_pair(pair: ast.expr) -> dict | None:
    """Read one sweep_params element into {"name", "type"}, or None if it does not conform (lenient).

    Two declaration forms are accepted, both NO-exec AST reads:
      - the SweepParam constructor call the real strategies use: `SweepParam("fast_window", "int")` —
        two string-literal args (positional), or the `name=`/`kind=` keyword form.
      - a literal 2-string pair: `("fast_window", "int")` (a tuple or list of two string literals).
    `kind` (the SweepParam field) is the value TYPE the panel casts with, returned as "type".
    """
    # SweepParam(...) — a Call whose func is a Name/Attribute; read two string args (positional or name/kind kw).
    if isinstance(pair, ast.Call):
        pos = [a.value for a in pair.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        kw = {k.arg: k.value.value for k in pair.keywords
              if k.arg in ("name", "kind") and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)}
        name = pos[0] if len(pos) >= 1 else kw.get("name")
        kind = pos[1] if len(pos) >= 2 else kw.get("kind")
        if isinstance(name, str) and isinstance(kind, str):
            return {"name": name, "type": kind}
        return None
    # ("name", "kind") — a literal tuple/list of exactly two string literals.
    if (isinstance(pair, (ast.Tuple, ast.List)) and len(pair.elts) == 2
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in pair.elts)):
        return {"name": pair.elts[0].value, "type": pair.elts[1].value}
    return None


def _sweep_params_from_tree(tree: ast.Module) -> list[dict]:
    """Read a strategy's declared `sweep_params` off an AST (M5 U2 — the dispatch panel's grid fields).

    A class attribute `sweep_params = (SweepParam("fast_window", "int"), SweepParam("slow_window", "int"))`
    (the real reference form) — or the equivalent literal `(("fast_window", "int"), ...)` pairs — declares the
    names (and value types) the universe-sweep dispatch grid offers, so the panel renders one value-input per
    declared param instead of hardcoding names in the frontend (the backend stays the single source). Read
    with NO exec (the OPEN-4 sandbox boundary). This is DISPLAY metadata, not a dispatch guard (the dispatch
    reads the param_grid the panel sends, not this), so it is LENIENT — a strategy with no sweep_params (every
    price/event strategy) or a non-conforming declaration returns [], never raising and never breaking the
    status read. Returns [{"name": str, "type": str}, ...] for the first class that declares conforming
    elements, in declaration order.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:  # only the class's own body
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "sweep_params" for t in stmt.targets
            ):
                value = stmt.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                  and stmt.target.id == "sweep_params" and stmt.value is not None):
                value = stmt.value
            else:
                continue
            if not isinstance(value, (ast.Tuple, ast.List)):  # not a literal sequence -> ignore (lenient)
                continue
            params: list[dict] = []
            conforming = True
            for pair in value.elts:  # each element: a SweepParam(...) call or a 2-string literal pair
                parsed = _sweep_param_pair(pair)
                if parsed is None:
                    conforming = False
                    break
                params.append(parsed)
            if conforming and params:  # the first conforming, non-empty declaration wins
                return params
    return []  # no class declares a conforming sweep_params -> the panel shows the manual-add affordance


def _class_subclasses_strategy(node: ast.ClassDef) -> bool:
    """True iff a ClassDef names a Strategy-family base (M5 C — scope the inline self.param scan).

    Matches a base whose name ends in 'Strategy' (Strategy, EventStrategy, a '...Strategy' intermediate base),
    as a bare Name (`class S(Strategy)`) or an attribute access (`class S(base.Strategy)`). An aliased import
    (`from strategies.base import Strategy as S`) is not covered; the canonical import form is what strategies use."""
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else None)
        if name and name.endswith("Strategy"):
            return True
    return False


def _inline_params_from_tree(tree: ast.Module) -> list[dict]:
    """Read a strategy's inline `self.param(...)` declarations off an AST (M5 C — the per-combo sweep mode).

    Scans every class body for `self.param(<name-literal>, <default-literal>, type=<'int'|'float'>?)` calls and
    returns [{"name": str, "type": str}, ...] in source order, de-duped by name (first-seen). NO exec (the
    OPEN-4 sandbox boundary). Unlike the LENIENT `_sweep_params_from_tree` (a stray non-conforming class attr is
    ignored), this is STRICT — a malformed self.param (a non-literal name, a bad type=, or a non-literal default
    with no type= to infer from) RAISES StrategyValidationError, the same fail-loud discipline
    `_event_types_from_tree` uses: a value the author marked sweepable must reach the grid or fail loudly, never
    silently vanish. `type` is inferred from the default literal when the `type=` kwarg is absent
    (int -> "int", float -> "float", matching the Strategy.param runtime inference; bool -> "float")."""
    out: list[dict] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Scope the scan to a Strategy subclass: self.param is a Strategy method, so a self.param call in an
        # UNRELATED helper class is the author's bug (a runtime AttributeError), not a sweep declaration —
        # scanning it would pollute the grid or trip a false ambiguity. Match a base whose name ends in
        # 'Strategy' (Strategy, EventStrategy, a '...Strategy' intermediate base, or an attribute access ending
        # in one). An aliased import (from x import Strategy as S) is not covered; the canonical import form is used.
        if not _class_subclasses_strategy(node):
            continue
        for call in ast.walk(node):  # any self.param(...) call anywhere in the strategy class (run() or a helper method)
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "param" and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "self"):
                continue
            if not (call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
                raise StrategyValidationError(
                    "self.param(...)'s first argument (the parameter name) must be a string literal")
            name = call.args[0].value
            type_kw = next((k.value for k in call.keywords if k.arg == "type"), None)  # the explicit type= kwarg
            if type_kw is not None:
                if not (isinstance(type_kw, ast.Constant) and type_kw.value in ("int", "float")):
                    raise StrategyValidationError(
                        f"self.param({name!r}, ...) type= must be the literal 'int' or 'float'")
                kind = type_kw.value
            else:  # infer the grid type from the default literal (arg 1)
                default = call.args[1] if len(call.args) >= 2 else None
                if isinstance(default, ast.Constant) and isinstance(default.value, bool):
                    kind = "float"  # match Strategy.param: bool is excluded from int
                elif isinstance(default, ast.Constant) and isinstance(default.value, int):
                    kind = "int"
                elif isinstance(default, ast.Constant) and isinstance(default.value, float):
                    kind = "float"
                else:
                    raise StrategyValidationError(
                        f"self.param({name!r}, ...) needs an int/float literal default OR an explicit "
                        f"type='int'|'float' so the dispatch grid knows the value type")
            if name not in seen:  # de-dup by name, first-seen wins (a value used twice declares once)
                seen.add(name)
                out.append({"name": name, "type": kind})
    return out


def _has_sweep_signals_override(tree: ast.Module) -> bool:
    """True iff a class in the source defines `def sweep_signals` (the vectorized-sweep contract override, M5 C).

    The wide/vectorized path requires a strategy to OVERRIDE sweep_signals (the base raises SweepNotSupported);
    its presence distinguishes the vectorized mode from the inline mode in `_resolve_param_mode`. NO exec."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:  # the class's own methods
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "sweep_signals":
                    return True
    return False


def _resolve_param_mode(class_params: list[dict], inline_params: list[dict],
                        has_signals: bool) -> tuple[str, list[dict]]:
    """The single fail-loud sweep-mode discriminator (M5 C, AD-C2). Returns (mode, effective_grid_params).

    Modes: 'inline' (the per-combo run() path, inline self.param, no vectorized contract), 'vectorized' (the
    wide sweep_signals path, class sweep_params), 'zero' (the zero-param single-run path, neither). A strategy
    that declares BOTH an inline self.param AND a vectorized contract (class sweep_params OR a sweep_signals
    override) is AMBIGUOUS and refused (StrategyValidationError), never a silent mode pick, with ONE named
    exception below (lane WP round 2, 6.5, finding WP-F1): the RECOGNISED HYBRID.

    A recognised hybrid is a class whose inline self.param names are EXACTLY the same set its own class
    sweep_params declares, alongside a real vectorized contract (sweep_signals overridden, so the vectorized
    path is genuinely reachable, not merely declared). That shape resolves to 'vectorized' with the class's
    own params, the SAME answer an ordinary class-only strategy already gets, and it is the safe answer for
    two reasons. First, the mass sweep never calls run() at all: it runs sweep_signals directly over the
    whole grid, so the inline self.param reads inside run() are simply never reached by that path and cannot
    make it produce anything different from a strategy with no self.param calls at all. Second, the
    walk-forward per-combo path FORKS in run_sweep_entrypoint.py before this function's own mode dispatch is
    ever consulted (that branch derives its own params_decl straight from the class's sweep_params and calls
    run() once per combo through _set_active_combo), so 'vectorized' is never read as an instruction to run
    the per-combo path there; it only ever governs the ordinary wide-sweep dispatch, where it is exactly
    correct. A NAME MISMATCH (inline names differing from the declared class names) and a PARTIAL OVERLAP
    (inline names a strict subset or superset of the declared class names) stay exactly as ambiguous and
    refused as before this fold: only an EXACT set match, with a real sweep_signals override present, is
    recognised. The effective grid params are the inline params for 'inline', the class params for
    'vectorized' (including the recognised-hybrid case), and [] for 'zero', so the dispatch grid is
    mode-agnostic (it reads one {name,type} list either way)."""
    is_recognized_hybrid = bool(
        inline_params and class_params and has_signals
        and {p["name"] for p in inline_params} == {p["name"] for p in class_params}
    )
    if is_recognized_hybrid:
        return "vectorized", class_params
    if inline_params and (class_params or has_signals):
        raise StrategyValidationError(
            "strategy declares inline self.param parameters AND a vectorized sweep contract "
            "(sweep_params or a sweep_signals override); declare ONE sweep mechanism, not both")
    if inline_params:
        return "inline", inline_params
    if class_params:  # vectorized; the engine fails loud via SweepNotSupported at run if sweep_signals is absent
        return "vectorized", class_params
    return "zero", []


# The closed comparator set a `fundamental_screen` declaration may use (mirrors screen.py
# _COMPARATORS). Validated at the AST read so a bad operator is rejected at strategy WRITE time, not
# at the first screened run. concept-in-vocabulary validity is checked at the use site
# (screen_ciks_by_fundamental), the same shallow-at-dispatch / deep-at-use split event_types uses.
_FUNDAMENTAL_SCREEN_OPS = (">", ">=", "<", "<=")
_FUNDAMENTAL_SCREEN_KEYS = frozenset({"concept", "op", "threshold"})


def _fundamental_screen_from_tree(tree: ast.Module) -> dict | None:
    """Read a strategy's declared `fundamental_screen` dict literal off an AST (M3.75 FUND-4 §9).

    A class attribute `fundamental_screen = {"concept": "net_income", "op": ">", "threshold": 0.0}`
    declares the universe screen the worker passes to POST /worker/universes/resolve (§9). Read as a
    LITERAL with ast.literal_eval (literal-only evaluation, NO exec — the same sandbox boundary the
    event_types read holds), then shape-validated: exactly the keys {concept, op, threshold},
    concept a str, op in the closed comparator set, threshold a real number. Returns the validated
    dict (threshold coerced to float for a JSON-clean payload), or None when no class declares it.
    Raises StrategyValidationError on a malformed declaration or two classes declaring different
    screens (ambiguous).
    """
    decls: list[dict] = []  # one validated screen per class that declares it
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:  # only the class's own body
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "fundamental_screen" for t in stmt.targets
            ):
                value = stmt.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                  and stmt.target.id == "fundamental_screen" and stmt.value is not None):
                value = stmt.value
            else:
                continue
            # The value MUST be a dict literal; literal_eval rejects anything computed (no exec).
            if not isinstance(value, ast.Dict):
                raise StrategyValidationError(
                    f"fundamental_screen in class {node.name!r} must be a dict literal "
                    "(e.g. {\"concept\": \"net_income\", \"op\": \">\", \"threshold\": 0.0}); "
                    "a computed value is not allowed (M3.75 FUND-4 §9)"
                )
            try:
                screen = ast.literal_eval(value)  # safe: literals only, never executes strategy code
            except (ValueError, SyntaxError) as exc:
                raise StrategyValidationError(
                    f"fundamental_screen in class {node.name!r} is not a literal dict: {exc}"
                ) from exc
            # Shape: exactly the three keys, the right types, op in the closed comparator set.
            if set(screen) != _FUNDAMENTAL_SCREEN_KEYS:
                raise StrategyValidationError(
                    f"fundamental_screen in class {node.name!r} must have exactly the keys "
                    f"{sorted(_FUNDAMENTAL_SCREEN_KEYS)}; got {sorted(screen)} (M3.75 FUND-4 §9)"
                )
            if not isinstance(screen["concept"], str) or not screen["concept"]:
                raise StrategyValidationError(
                    f"fundamental_screen.concept in class {node.name!r} must be a non-empty string"
                )
            if screen["op"] not in _FUNDAMENTAL_SCREEN_OPS:
                raise StrategyValidationError(
                    f"fundamental_screen.op in class {node.name!r} must be one of "
                    f"{list(_FUNDAMENTAL_SCREEN_OPS)}; got {screen['op']!r} (M3.75 FUND-4 §9)"
                )
            # bool is an int subclass; reject it explicitly so a True threshold is not read as 1.
            if isinstance(screen["threshold"], bool) or not isinstance(screen["threshold"], (int, float)):
                raise StrategyValidationError(
                    f"fundamental_screen.threshold in class {node.name!r} must be a real number; "
                    f"got {screen['threshold']!r}"
                )
            decls.append({"concept": screen["concept"], "op": screen["op"],
                          "threshold": float(screen["threshold"])})  # float -> JSON-clean payload
    if not decls:
        return None
    if any(d != decls[0] for d in decls[1:]):  # two classes declaring different screens is ambiguous
        raise StrategyValidationError(
            "more than one class declares a different fundamental_screen; the strategy's screen "
            "is ambiguous (M3.75 FUND-4 §9)"
        )
    return decls[0]


def extract_fundamental_concepts(source_code: str) -> tuple[str, ...]:
    """Parse a strategy source and return its declared `fundamental_concepts` literal (FUND-4 §9).

    The concept set the strategy's FundamentalFeatureDefs read (§8). Thin wrapper over
    _str_tuple_attr_from_tree for the dispatch path; () when none is declared (every price/event-only
    strategy, so the run's payload omits the field and the worker skips fundamentals materialization).
    """
    return _str_tuple_attr_from_tree(ast.parse(source_code), "fundamental_concepts", "net_income")


def extract_fundamental_screen(source_code: str) -> dict | None:
    """Parse a strategy source and return its declared `fundamental_screen` dict, or None (FUND-4 §9).

    Thin wrapper over _fundamental_screen_from_tree for the dispatch path. None when no class
    declares a screen (every run that does not screen the universe on a fundamental).
    """
    return _fundamental_screen_from_tree(ast.parse(source_code))


def extract_asset_class_scope(source_code: str) -> tuple[str, ...]:
    """Parse a strategy source and return its declared `asset_class_scope` literal (M4 D1/D7).

    The asset classes the strategy is allowed to run on. Thin wrapper over _str_tuple_attr_from_tree
    for the dispatch guard (R2c): the dispatch path reads the declared scope by AST (no exec, the
    OPEN-4 boundary) and refuses a run whose dataset resolves to a class outside it. Returns () when no
    class declares it — an UNCONSTRAINED strategy the guard does not fire on (D7 empty-scope semantics),
    exactly as extract_event_types returns () for a non-event strategy. The closed-vocabulary check is
    enforced at write time by validate_strategy_source, so a stored source's scope is always drawn from
    {crypto, equities}; this read does not re-check it (the shallow-at-save / deep-at-use split).
    """
    return _str_tuple_attr_from_tree(ast.parse(source_code), "asset_class_scope", "equities")


def extract_sizing_method(source_code: str) -> str:
    """Parse a strategy source and return its declared `sizing_method` id (#376 ruling 4).

    A strategy inherits `Strategy.sizing_method = "vol_target_atr"` (research_lab/strategies/base/
    strategy.py) unless its own class body sets a different literal string, read here by AST, no
    exec, the same sandbox boundary `extract_asset_class_scope` stands on. The base default is
    imported LAZILY from research_lab (the `paperdb.py:1583` precedent: `research_lab.strategies.
    helpers.sizing` and `research_lab.strategies.base.strategy` both import the standard library
    only, so this never pulls pandas or vbt into the backend's boot), so the one true default lives
    in exactly one file and is never restated here as a second literal. Raises
    StrategyValidationError on a non-string declaration, matching every other AST reader in this
    module.

    Round 4 (the lead's own trial-merge finding): this import used to assume some EARLIER call in
    the same process had already put `research_lab/` on `sys.path` (catalog._ensure_strategies_on_path,
    idempotent), because `research_lab/strategies/base/__init__.py` itself does a BARE `from
    strategies...` import that only resolves once that path is present. On main this held by
    accident (a judge call always ran first); lane JC's own admission table now refuses a single
    run's grade before that accidental order is guaranteed, so `GET /api/research/strategies/{id}`
    (routes/research.py get_strategy) hit a bare `ModuleNotFoundError: No module named 'strategies'`
    the first time this function ran in a fresh process. Fixed at the root, self-sufficient, the
    same idiom routes/judge.py's own `_resolve_formula_version` already uses: call
    `catalog._ensure_strategies_on_path()` here, every time, before the import, rather than relying
    on any caller having done it first.
    """
    catalog._ensure_strategies_on_path()  # idempotent: makes this import self-sufficient, no hidden order dependency
    from research_lab.strategies.base.strategy import Strategy  # lazy: mirrors paperdb.py's sizing import

    tree = ast.parse(source_code)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:  # only the class's own body, the same shape _str_tuple_attr_from_tree reads
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "sizing_method" for t in stmt.targets
            ):
                value = stmt.value
            elif (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                  and stmt.target.id == "sizing_method" and stmt.value is not None):
                value = stmt.value
            else:
                continue
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                raise StrategyValidationError(
                    "sizing_method must be a literal string (e.g. sizing_method = \"vol_target_atr\"); "
                    "a computed or non-string value is not allowed (#376 ruling 4)"
                )
            return value.value  # this strategy's own explicit override
    return Strategy.sizing_method  # no override anywhere in this source: the registry's base default


def _is_sizing_target(target: "ast.expr") -> bool:
    """True when an assignment target is the name sizing_method or an attribute named sizing_method (also inside a tuple)."""
    if isinstance(target, ast.Name):
        return target.id == "sizing_method"
    if isinstance(target, ast.Attribute):
        return target.attr == "sizing_method"
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_is_sizing_target(t) for t in target.elts)
    if isinstance(target, ast.Starred):
        return _is_sizing_target(target.value)
    return False


def _sizing_method_outside_class(tree: "ast.AST") -> "tuple[int, str] | None":
    """ED1a: the first sizing_method assignment the server never reads, as (line, where), or None.

    extract_sizing_method reads only a plain or annotated assignment that is a direct statement of a
    class body. Any other assignment to sizing_method (a local in a method, self.sizing_method, a module
    line, a line inside an if or for in a class body) is refused, and an augmented assignment is refused
    everywhere. `where` names the nearest enclosing scope.
    """
    found: "list[tuple[int, str]]" = []

    def walk(node: "ast.AST", scopes: list, parent: "ast.AST | None") -> None:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_is_sizing_target(t) for t in targets):
                if isinstance(node, ast.AugAssign):
                    found.append((node.lineno, "an augmented assignment"))
                elif not isinstance(parent, ast.ClassDef):
                    scope = scopes[-1] if scopes else None
                    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        where = f"function {scope.name}"
                    elif isinstance(scope, ast.ClassDef):
                        where = f"a block inside class {scope.name}"
                    else:
                        where = "the module"
                    found.append((node.lineno, where))
        child_scopes = scopes + [node] if isinstance(
            node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else scopes
        for child in ast.iter_child_nodes(node):
            walk(child, child_scopes, node)

    walk(tree, [], None)
    if not found:
        return None
    return min(found, key=lambda f: f[0])


def extract_strategy_dispatch_metadata(source_code: str) -> dict:
    """Parse a strategy source ONCE and return its AST-read dispatch metadata (M4 R8/R9).

    {event_types, asset_class_scope}: the editor's test-run panel reads these off GET /strategies/{id}
    /status to choose the dispatch path (event vs price) and show the declared scope. One ast.parse over
    the shared tree, instead of two via extract_event_types + extract_asset_class_scope. () for either
    when undeclared. A contract-valid stored source always parses (validate_strategy_source enforces it).
    """
    tree = ast.parse(source_code)  # parse once; the reads share the tree
    # M5 C: resolve the sweep mode + the EFFECTIVE grid params off the same tree. `sweep_params` carries the
    # class sweep_params for a vectorized strategy and the inline self.param decls for an inline strategy, in the
    # identical {name,type} shape, so the dispatch grid is mode-agnostic; `param_mode` lets the grid show the
    # per-combo cost note for 'inline'. An ambiguous declaration raises (validate_strategy_source refuses it at save).
    mode, params = _resolve_param_mode(
        _sweep_params_from_tree(tree), _inline_params_from_tree(tree), _has_sweep_signals_override(tree))
    return {
        "event_types": _event_types_from_tree(tree),                                  # () for a price strategy
        "asset_class_scope": _str_tuple_attr_from_tree(tree, "asset_class_scope", "equities"),  # M4 D1
        "sweep_params": params,                                                        # M5 U2/C: the dispatch grid fields ([] if none)
        "param_mode": mode,                                                            # M5 C: 'zero' | 'vectorized' | 'inline'
    }


# The closed asset-class vocabulary a strategy's `asset_class_scope` may name (M4 D1). The same two
# classes asset_class_of resolves to; a member outside this set is a typo'd scope, caught at write
# time (the shallow-at-save / deep-at-use split event_types uses). A new class here is a deliberate
# vocabulary change, never an accident a save lets through. The dispatch-time extractor that READS the
# declared scope (extract_asset_class_scope) arrives with the Phase-3 guard that consumes it (R9); the
# Phase-2 save check below only enforces the closed vocabulary so the R5 scaffold is born valid.
_ASSET_CLASS_VOCAB = frozenset({"crypto", "equities"})


def validate_strategy_source(source_code: str) -> str:
    """Validate a strategy .py source and return its description (the module docstring).

    Three of the four brief-C rules live here (the fourth, clean UTF-8, is enforced
    at the file-read boundary by the caller, since a `str` is already valid Unicode
    and JSON callers get it for free):
      - size:      UTF-8 byte length must be <= 1 MiB
      - parses:    ast.parse must succeed
      - compiles:  compile() must succeed (M4 R6 — the symbol-table pass ast.parse skips)
      - docstring: ast.get_docstring must return non-empty text (it is the description, L29)
      - event_types (M3.5 brief I + F7): if a class declares event_types, it must be a literal
        tuple of string literals (so the dispatch-time AST read is a validated contract, never a
        guess) AND every type must be backtest-grade (BACKTEST_GRADE_EVENT_TYPES) — a non-backtest-
        grade type like 'news' is rejected at write time. A price strategy declares none and passes.
      - asset_class_scope (M4 D1): if declared, a literal string tuple drawn from the closed
        vocabulary {crypto, equities} — a typo'd class is rejected at write time.
      - sizing_method (ED1a): only a direct class-body assignment; anywhere else is refused.
    Raises StrategyValidationError with a specific message on any failure.
    """
    # Rule: size limit, measured on the UTF-8 encoding (the stored form).
    n_bytes = len(source_code.encode('utf-8'))  # byte length, not character count
    if n_bytes > _MAX_STRATEGY_SOURCE_BYTES:  # reject oversized files before parsing
        raise StrategyValidationError(
            f"strategy source is {n_bytes} bytes; limit is {_MAX_STRATEGY_SOURCE_BYTES} "
            f"({_MAX_STRATEGY_SOURCE_BYTES // 1024} KiB)"
        )
    # Rule: must parse as Python.
    try:
        tree = ast.parse(source_code)  # syntax check; also the tree we read the docstring from
    except SyntaxError as exc:  # specific: only a parse failure is a SyntaxError here
        raise StrategyValidationError(f"strategy source does not parse: {exc}") from exc
    # Rule: must compile (M4 R6). compile() runs the symbol-table pass ast.parse skips, so it catches
    # errors a bare parse lets through ('return'/'yield' outside a function, a duplicate argument name,
    # a global/nonlocal conflict). compile only builds bytecode — it executes NOTHING (no sandbox risk).
    # Folded HERE, not in the route, so the validate button and the save path can never disagree (R6).
    try:
        compile(tree, "<strategy>", "exec")  # bytecode-compile the already-parsed tree
    except (SyntaxError, ValueError) as exc:  # ValueError: e.g. too-deeply-nested literal
        raise StrategyValidationError(f"strategy source does not compile: {exc}") from exc
    # Rule: non-empty module docstring (it becomes the strategy description).
    docstring = ast.get_docstring(tree)  # None if absent; the raw text otherwise
    if docstring is None or not docstring.strip():  # whitespace-only counts as empty
        raise StrategyValidationError(
            "strategy module docstring is empty; it is required as the strategy description (L29)"
        )
    # Rule: event_types (if declared) must be a literal tuple of string literals (raises on malformed),
    # AND every declared type must be BACKTEST-GRADE (F7). The literal-tuple SHAPE is enforced by
    # _event_types_from_tree; the closed-VOCABULARY check below mirrors the asset_class_scope check —
    # a strategy declaring a non-backtest-grade type (e.g. 'news', which is ingest-and-display only)
    # is rejected at SAVE, the earliest honest point, never carried to a dispatch that the materializer
    # would 400 anyway. A price strategy declares none (empty tuple) and passes unchanged.
    declared_types = _event_types_from_tree(tree)  # shape-check + read (was a bare side-effect call)
    unknown_types = [t for t in declared_types if t not in eventsdb.BACKTEST_GRADE_EVENT_TYPES]
    if unknown_types:
        raise StrategyValidationError(
            f"event_types names non-backtest-grade or unknown types {sorted(unknown_types)}; "
            f"each must be one of {sorted(eventsdb.BACKTEST_GRADE_EVENT_TYPES)} "
            f"(news is ingest-and-display only, not strategy-readable — F7)"
        )
    # Rule (M3.75 FUND-4 §9): fundamental_concepts (if declared) must be a literal string tuple, and
    # fundamental_screen (if declared) a well-shaped dict literal — so the dispatch-time AST reads
    # are a validated contract, never a guess; a malformed declaration is rejected at write time.
    _str_tuple_attr_from_tree(tree, "fundamental_concepts", "net_income")  # reject a malformed fundamental_concepts
    _fundamental_screen_from_tree(tree)  # reject a malformed fundamental_screen
    # Rule (M4 D1): asset_class_scope (if declared) must be a literal string tuple drawn from the closed
    # vocabulary {crypto, equities}. The literal-tuple SHAPE is enforced by _str_tuple_attr_from_tree
    # (raises on a computed/non-string value); the closed-VOCABULARY check below is the new part — a
    # typo'd class ('crypals', 'forex') is rejected at write time, never carried to a dispatch that
    # would then have no cost profile for it. The scaffold (R5) is born declaring a valid scope.
    scope = _str_tuple_attr_from_tree(tree, "asset_class_scope", "equities")  # shape-check + read
    unknown = [c for c in scope if c not in _ASSET_CLASS_VOCAB]  # members outside the closed vocabulary
    if unknown:
        raise StrategyValidationError(
            f"asset_class_scope names unknown asset classes {sorted(unknown)}; "
            f"each must be one of {sorted(_ASSET_CLASS_VOCAB)} (M4 D1)"
        )
    # Rule (ED1a): sizing_method is read only from a class body; set anywhere else the server and the run disagree.
    misplaced = _sizing_method_outside_class(tree)
    if misplaced is not None:
        line, where = misplaced
        raise StrategyValidationError(
            f"sizing_method is set on line {line}, inside {where}, where the server never reads it; "
            f"set it once in the class body, for example sizing_method = \"vol_target_atr\""
        )
    # Rule (M5 C): resolve the sweep mode at SAVE so an AMBIGUOUS declaration (inline self.param AND a
    # vectorized sweep_params/sweep_signals contract) or a MALFORMED self.param (non-literal name/type/default)
    # is refused at the earliest honest point, never carried to a dispatch that would pick a mode silently.
    # The call raises StrategyValidationError on either; a single-mode (or no-sweep) strategy passes unchanged.
    _resolve_param_mode(
        _sweep_params_from_tree(tree), _inline_params_from_tree(tree), _has_sweep_signals_override(tree))
    return docstring  # the description, passed straight to upsert_strategy


def code_like(display_name: "str | None") -> "str | None":
    """NM1a: the reason a display name reads like code, or None when it reads like words (None gives None)."""
    if display_name is None:
        return None
    if "_" in display_name and re.fullmatch(r"[A-Z0-9_]+", display_name):
        return "it is written like a code (capitals and underscores)"
    if "_" in display_name:
        return "it has an underscore"
    return None


def _clean_display_name(display_name: "str | None") -> "str | None":
    """DN1: None stays None (no name given). A text is stripped and must be 1 to 60 characters; NM1a: not code-like."""
    if display_name is None:
        return None
    cleaned = display_name.strip()
    if not cleaned:
        raise StrategyValidationError("the display name cannot be blank")
    if len(cleaned) > _DISPLAY_NAME_MAX:
        raise StrategyValidationError(
            f"the display name is {len(cleaned)} characters; the most allowed is {_DISPLAY_NAME_MAX}")
    reason = code_like(cleaned)
    if reason is not None:
        raise StrategyValidationError(
            f"the display name {cleaned!r} reads like code: {reason}. Type the name a person would say.")
    return cleaned


def upsert_strategy(name: str, source_code: str,
                    now: int | None = None, create_only: bool = False,
                    display_name: "str | None" = None) -> tuple[int, int, str]:
    """Idempotent strategy write: validate, then current-state row + history snapshot, atomically.

    Validates `source_code` and derives the description (the module docstring) via
    validate_strategy_source — so the write path can never store an invalid source, and
    no caller can skip validation. Deviates from spec L16's (name, source_code, description)
    signature: the description is a pure function of the source (DECISIONS #18), so passing
    it separately only invites desync. Raises StrategyValidationError on a bad source.

    `create_only` (pre-M4 Brief A1 / D5): if True, an EXISTING name raises StrategyNameExists
    (carrying the existing id) INSTEAD of updating it — and the check happens INSIDE the write
    transaction, so the create route's 409 is race-free (no check-then-write TOCTOU). Default
    False keeps the create-or-update behaviour the CLI and PUT path rely on.

    `display_name` (DN1): the name pages show. Stored as given (stripped, 1 to 60 characters, else
    StrategyValidationError); None on a create stores NULL, never a derived name. On an existing row
    None keeps the stored name. The doors (routes, CLI, theory screen, generator) refuse a missing
    name in plain words; storage does not.

    Returns (strategy_id, version, action) where action is one of:
      - 'created'   : new name, inserted at version 1
      - 'updated'   : source changed, version bumped, history snapshot appended
      - 'renamed'   : same source, new display_name: the name and updated_at change, the version stays
                      (runs cite the version for the source), a history row is appended at that version
      - 'unchanged' : identical source_code and no new display_name, no write (true no-op, L16)
    Change detection compares source_code only; since the description is derived from the
    source, a source change is the only thing that can change the strategy's logical state.
    """
    description = validate_strategy_source(source_code)  # validate + derive description; raises if invalid
    display_name = _clean_display_name(display_name)  # before any write
    now = int(time.time()) if now is None else now  # one timestamp for the row + its history snapshot
    with transaction() as conn:  # BEGIN IMMEDIATE; both writes commit together or roll back together
        row = conn.execute(
            "SELECT id, source_code, version, display_name FROM research_strategies WHERE name = ?;",
            (name,),
        ).fetchone()

        if create_only and row is not None:  # ---- create-only: refuse an existing name in-txn ----
            raise StrategyNameExists(int(row["id"]))  # atomic 409: the SELECT + refusal share this BEGIN IMMEDIATE

        if row is None:  # ---- created: first registration of this name ----
            counter = conn.execute("SELECT high_id FROM research_strategy_id_counter WHERE one = 1;").fetchone()
            if counter is None:
                raise StrategyIdCounterMissing(
                    "the strategy id counter is missing: init_db has not run on this research.db")
            strategy_id = int(counter["high_id"]) + 1  # SID1: never an id that was given before
            conn.execute(
                "INSERT INTO research_strategies "
                "    (id, name, source_code, description, version, created_at, updated_at, display_name, display_name_from_name) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0);",
                (strategy_id, name, source_code, description, now, now, display_name),
            )
            conn.execute(
                "INSERT INTO research_strategies_history "
                "    (strategy_id, version, source_code, description, edited_at, display_name) "
                "VALUES (?, 1, ?, ?, ?, ?);",
                (strategy_id, source_code, description, now, display_name),
            )
            return (strategy_id, 1, 'created')

        strategy_id = row["id"]
        kept_name = row["display_name"] if display_name is None else display_name  # None keeps the stored name
        if row["source_code"] == source_code:
            if display_name is None:
                return (strategy_id, row["version"], 'unchanged')  # ---- no-op: identical source, no name given ----
            if display_name == row["display_name"]:
                conn.execute("UPDATE research_strategies SET display_name_from_name = 0 WHERE id = ?;", (strategy_id,))  # NM1a: retyped, so typed
                return (strategy_id, row["version"], 'unchanged')  # ---- no history row ----
            conn.execute(  # ---- renamed: same source, new name; the version stays ----
                "UPDATE research_strategies SET display_name = ?, display_name_from_name = 0, updated_at = ? WHERE id = ?;",
                (display_name, now, strategy_id),
            )
            conn.execute(
                "INSERT INTO research_strategies_history "
                "    (strategy_id, version, source_code, description, edited_at, display_name) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (strategy_id, row["version"], source_code, description, now, display_name),
            )
            return (strategy_id, row["version"], 'renamed')

        new_version = row["version"] + 1  # ---- updated: content changed ----
        conn.execute(
            "UPDATE research_strategies "
            "   SET source_code = ?, description = ?, version = ?, updated_at = ?, display_name = ?, "
            "       display_name_from_name = CASE WHEN ? IS NULL THEN display_name_from_name ELSE 0 END "
            " WHERE id = ?;",
            (source_code, description, new_version, now, kept_name, display_name, strategy_id),
        )
        conn.execute(
            "INSERT INTO research_strategies_history "
            "    (strategy_id, version, source_code, description, edited_at, display_name) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (strategy_id, new_version, source_code, description, now, kept_name),
        )
        return (strategy_id, new_version, 'updated')


def rename_strategy(strategy_id: int, display_name: str, now: int | None = None) -> None:
    """NM1a: a person renames any strategy, active or deleted, as upsert_strategy's 'renamed' branch does.

    One history row at the current version; the same text as stored clears the made-up flag only (no history row).
    Raises StrategyValidationError (blank, too long, code-like) before any write, StrategyNotFound for an unknown id.
    """
    cleaned = _clean_display_name(display_name)
    if cleaned is None:
        raise StrategyValidationError("the display name cannot be empty")
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        row = conn.execute("SELECT id, source_code, description, version, display_name FROM research_strategies WHERE id = ?;",
                           (strategy_id,)).fetchone()
        if row is None:
            raise StrategyNotFound(strategy_id)
        if row["display_name"] == cleaned:
            conn.execute("UPDATE research_strategies SET display_name_from_name = 0 WHERE id = ?;", (strategy_id,))
            return
        conn.execute("UPDATE research_strategies SET display_name = ?, display_name_from_name = 0, updated_at = ? WHERE id = ?;",
                     (cleaned, now, strategy_id))
        conn.execute(
            "INSERT INTO research_strategies_history "
            "    (strategy_id, version, source_code, description, edited_at, display_name) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (strategy_id, row["version"], row["source_code"], row["description"], now, cleaned))


# =============================================================
# M3 brief B.5 — dataset hard-delete (DECISIONS #16)
#
# Async/worker-mediated: the parquet lives on the laptop, the backend runs on the
# server, so the server cannot unlink the file itself. request_hard_delete validates
# the preconditions and queues a hard_delete_dataset job; the worker unlinks the file
# then calls back, and mark_hard_deleted writes the cleaned terminal state. Both are
# thin domain functions; the routes only map results to HTTP status codes (#17).
# =============================================================

class DatasetNotFound(LookupError):
    """No research_datasets row with the given id."""


class DatasetNotSoftDeleted(RuntimeError):
    """Hard-delete attempted on a dataset whose status is not 'deleted'."""


class DatasetHasRuns(RuntimeError):
    """Hard-delete attempted on a dataset still referenced by research_runs rows."""

    def __init__(self, count: int):
        self.count = count  # number of referencing runs, surfaced in the 409 body
        super().__init__(f"dataset has {count} runs; cannot hard-delete")


class DatasetFileMissing(RuntimeError):
    """Restore attempted on a file-backed dataset whose parquet is gone (hard-deleted) or
    never completed — a non-null file_path is required for a file-backed restore (D7). Also
    raised by update_dataset_to_latest (W8) when there is no existing file to append onto."""


class DatasetAlreadyFetching(RuntimeError):
    """Re-fetch attempted on a dataset already in 'fetching' status (D7)."""


class DatasetWindowConflict(RuntimeError):
    """W8 'Update to latest': the extended end_ts collides with another dataset's UNIQUE
    (source, symbol, timeframe, start_ts, end_ts) key (another dataset already holds the
    exact extended window)."""


# Job statuses that mean a queued job has not finished yet (used for dedupe).
_INFLIGHT_JOB_STATUSES = ('pending', 'picked_up')


def request_hard_delete(dataset_id: int, now: int | None = None) -> dict:
    """Validate hard-delete preconditions and queue a hard_delete_dataset job.

    Returns one of:
      {"action": "queued", "job_id": int, "file_path": str}  -> route returns 202
      {"action": "noop"}                                     -> route returns 200 (already clean)
    Raises (route maps each to a status code):
      DatasetNotFound        -> 404
      DatasetNotSoftDeleted  -> 409 "dataset must be soft-deleted first"
      DatasetHasRuns(count)  -> 409 "dataset has N runs; cannot hard-delete"
    Idempotency: file_path IS NULL means the file is already gone -> noop. An
    unfinished hard_delete_dataset job for the same dataset is reused, never duplicated.
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:  # one transaction so the precondition read and the queue insert are consistent
        ds = conn.execute(
            "SELECT id, status, file_path FROM research_datasets WHERE id = ?;",
            (dataset_id,),
        ).fetchone()
        if ds is None:
            raise DatasetNotFound(f"dataset {dataset_id} not found")
        if ds["status"] != 'deleted':  # must be soft-deleted (M2 DELETE) before a hard-delete
            raise DatasetNotSoftDeleted(f"dataset {dataset_id} status is {ds['status']!r}, not 'deleted'")

        n_runs = conn.execute(
            "SELECT COUNT(*) FROM research_runs WHERE dataset_id = ?;",
            (dataset_id,),
        ).fetchone()[0]
        if n_runs > 0:  # FK is ON DELETE RESTRICT (§3.4); a referenced dataset cannot have its file removed
            raise DatasetHasRuns(n_runs)

        if ds["file_path"] is None:  # nothing on disk to remove -> normalize to the cleaned state, no job
            conn.execute(
                "UPDATE research_datasets SET file_bytes = 0, sha256 = NULL WHERE id = ?;",
                (dataset_id,),
            )
            return {"action": "noop"}

        # Dedupe: reuse an unfinished hard_delete_dataset job for this dataset instead of queuing a second.
        candidates = conn.execute(
            "SELECT id, payload_json FROM research_jobs "
            "WHERE kind = 'hard_delete_dataset' AND status IN (?, ?);",
            _INFLIGHT_JOB_STATUSES,
        ).fetchall()
        existing = next(
            (r["id"] for r in candidates if json.loads(r["payload_json"]).get("dataset_id") == dataset_id),
            None,
        )
        if existing is not None:
            return {"action": "queued", "job_id": existing, "file_path": ds["file_path"]}

        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES ('hard_delete_dataset', ?, ?, 'pending') RETURNING id;",
            (json.dumps({"dataset_id": dataset_id, "file_path": ds["file_path"]}), now),
        ).fetchone()
        return {"action": "queued", "job_id": job["id"], "file_path": ds["file_path"]}


def restore_dataset(dataset_id: int) -> dict:
    """Restore a soft-deleted dataset to 'ready' (pre-M4 Brief A1 / D7). Mirrors
    request_hard_delete's shape. Branches on the REGISTRY storage kind (never on file_path
    alone, which is ambiguous — a row-backed universe and a hard-deleted price dataset both
    carry NULL):
      - row-backed (universe / event_series): flip status='ready' (no file involved; 'ready'
        is their only non-deleted status, so the soft-delete lost nothing recoverable).
      - file-backed (price_dataset): flip to 'ready' iff file_path IS NOT NULL; else refuse.
    The file-presence check is honest because the §A1.2 durable atomic write guarantees a
    non-null file_path always denotes a COMPLETE file, and only hard-delete nulls it (arch §5);
    the server never has to read the worker-held file.

    Returns:
      {"action": "restored", "id": id, "status": "ready"}   real transition (route broadcasts)
      {"action": "noop", "id": id, "status": <current>}     already non-deleted (no UPDATE/SSE)
    Raises (the route maps each):
      DatasetNotFound       -> 404
      DatasetFileMissing    -> 409 (file-backed, file_path NULL: hard-deleted or never fetched)
    """
    with transaction() as conn:  # one transaction so the precondition read and the flip are consistent
        ds = conn.execute(
            "SELECT id, status, dataset_type, file_path FROM research_datasets WHERE id = ?;",
            (dataset_id,),
        ).fetchone()
        if ds is None:
            raise DatasetNotFound(f"dataset {dataset_id} not found")
        if ds["status"] != 'deleted':  # idempotent: only a soft-deleted row is restorable
            return {"action": "noop", "id": dataset_id, "status": ds["status"]}
        kind = dataset_storage_kind(ds["dataset_type"])  # the registry branch (raises on a bad type)
        if kind == DATASET_STORAGE_FILE and ds["file_path"] is None:
            # A hard-deleted (file nulled) or never-completed file-backed dataset has no complete
            # file to point at, so it cannot be made 'ready' (arch §5).
            raise DatasetFileMissing(
                f"dataset {dataset_id} is file-backed but has no file "
                f"(hard-deleted or never fetched); cannot restore")
        conn.execute(
            "UPDATE research_datasets SET status='ready' WHERE id = ?;",
            (dataset_id,),
        )
    return {"action": "restored", "id": dataset_id, "status": "ready"}


def refetch_dataset(dataset_id: int, now: int | None = None) -> dict:
    """Re-queue a fetch for an existing dataset (pre-M4 Brief A1 / D7). create_dataset's
    ON CONFLICT DO NOTHING no-ops on an existing row and queues nothing, so this is the only
    re-fetch path. Reads the row's source/symbol/timeframe/start_ts/end_ts, refuses if it is
    currently fetching, else inserts a kind='fetch_dataset' job (the existing-row payload shape)
    and flips the row to 'pending' in ONE transaction. The worker writes via the §A1.2 durable
    atomic write, so a failed/partial re-fetch cannot corrupt the prior file (arch §5/§6).

    Returns {"action": "queued", "id": id, "status": "pending", "job_id": int}.
    Raises (the route maps each):
      DatasetNotFound          -> 404
      DatasetAlreadyFetching   -> 409 (status already 'fetching')
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        ds = conn.execute(
            "SELECT id, status, source, symbol, timeframe, start_ts, end_ts "
            "FROM research_datasets WHERE id = ?;",
            (dataset_id,),
        ).fetchone()
        if ds is None:
            raise DatasetNotFound(f"dataset {dataset_id} not found")
        if ds["status"] == 'fetching':  # a fetch is already in flight; do not double-queue
            raise DatasetAlreadyFetching(f"dataset {dataset_id} is already fetching")
        # Rebuild ISO-8601 'Z' start/end from the stored epoch — the job/worker contract is ISO
        # (fetch.py parses it with pd.Timestamp(s, tz='UTC') back to the same epoch, so the
        # out_path is identical). The create path stores body.start/end raw ISO; refetch
        # reconstructs the canonical 'Z' form from the int epoch (lossless at seconds precision).
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ds["start_ts"]))
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ds["end_ts"]))
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES ('fetch_dataset', ?, ?, 'pending') RETURNING id;",
            (json.dumps({
                "source": ds["source"], "symbol": ds["symbol"],
                "timeframe": ds["timeframe"],
                "start": start_iso, "end": end_iso,
                "dataset_id": dataset_id,
            }), now),
        ).fetchone()
        job_id = job["id"]
        conn.execute(
            "UPDATE research_datasets SET status='pending' WHERE id = ?;",
            (dataset_id,),
        )
    return {"action": "queued", "id": dataset_id, "status": "pending", "job_id": job_id}


def update_dataset_to_latest(dataset_id: int, now: int | None = None) -> dict:
    """W8 'Update to latest' (D8-1): EXTEND the window to the source's latest end and queue a fetch
    of the extended window.

    Distinct from refetch_dataset, which re-pulls the SAME window to repair. Here end_ts becomes the
    source's latest-available end (dataset_sources.latest_end_epoch) — a system-of-record change, so
    it is recorded at queue time — and the job payload carries `existing` (the current file_path) +
    `mode='append'`. fetch.py then re-cuts the whole window for a source that re-bases past prices
    after dividends and splits (eodhd, alpaca_sip; PX4, Tamer 2026-11-09), and for one that does not
    (binance) reads the existing parquet and downloads only the bars after its last one. The worker's
    durable atomic write + the D8-3 fetcher-owned cleanup repoint file_path; the result callback
    (worker_dataset_result) writes rows/sha256/file_path/file_bytes/covered_* as usual.

    Requires a ready, file-backed dataset (a non-null file_path to append onto). Returns
    {"action": "queued", "id", "status": "pending", "job_id", "new_end_ts"}, or
    {"action": "noop", "id", "status", "reason": "already_current"} when the window already reaches the
    source's latest end (no gap to fetch). Raises (the route maps each):
      DatasetNotFound        -> 404
      DatasetAlreadyFetching -> 409 (status already 'fetching')
      DatasetFileMissing     -> 409 (no existing file; full-fetch first)
      DatasetWindowConflict  -> 409 (the extended window collides with another dataset's UNIQUE key)
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        ds = conn.execute(
            "SELECT id, status, source, symbol, timeframe, start_ts, end_ts, file_path "
            "FROM research_datasets WHERE id = ?;",
            (dataset_id,),
        ).fetchone()
        if ds is None:
            raise DatasetNotFound(f"dataset {dataset_id} not found")
        if ds["status"] == 'fetching':  # a fetch is already in flight; do not double-queue
            raise DatasetAlreadyFetching(f"dataset {dataset_id} is already fetching")
        if ds["status"] == 'deleted':  # a soft-deleted row keeps its file_path; updating it would
            # silently UNDELETE it (extend end_ts + flip to pending) — restore it first, never here.
            raise DatasetNotFound(f"dataset {dataset_id} not found")
        if not ds["file_path"]:  # nothing to append onto — an incremental update needs an existing file
            raise DatasetFileMissing(
                f"dataset {dataset_id} has no existing file to update incrementally; re-fetch (full) first"
            )

        new_end_ts = dataset_sources.latest_end_epoch(ds["source"], now)  # the source's latest fetchable end
        if new_end_ts <= ds["end_ts"]:  # the requested window already reaches the source's latest end
            return {"action": "noop", "id": dataset_id, "status": ds["status"], "reason": "already_current"}

        # Rebuild the canonical ISO 'Z' window: original start, NEW end (lossless at seconds precision,
        # the fetch.py contract; mirrors refetch_dataset's reconstruction).
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ds["start_ts"]))
        new_end_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(new_end_ts))

        # Extend end_ts (the requested window) NOW — the UNIQUE key is cross-row, so an extended window
        # can collide with another dataset; surface that as a clear conflict rather than an opaque 500.
        try:
            conn.execute(
                "UPDATE research_datasets SET end_ts=?, status='pending' WHERE id=?;",
                (new_end_ts, dataset_id),
            )
        except sqlite3.IntegrityError as exc:
            raise DatasetWindowConflict(
                f"extending dataset {dataset_id} to end_ts={new_end_ts} collides with an existing "
                "dataset window (same source/symbol/timeframe/start/end)"
            ) from exc

        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES ('fetch_dataset', ?, ?, 'pending') RETURNING id;",
            (json.dumps({
                "source": ds["source"], "symbol": ds["symbol"],
                "timeframe": ds["timeframe"],
                "start": start_iso, "end": new_end_iso,
                "existing": ds["file_path"],  # the worker forwards this as --existing (W8 bridge)
                "mode": "append",             # -> fetch.py: the whole window re-cut, or the gap for binance (PX4)
                "dataset_id": dataset_id,
            }), now),
        ).fetchone()
        job_id = job["id"]
    return {"action": "queued", "id": dataset_id, "status": "pending",
            "job_id": job_id, "new_end_ts": new_end_ts}


def mark_hard_deleted(dataset_id: int) -> None:
    """Write the cleaned terminal state after the worker has removed the file.

    Sets file_path=NULL (the idempotency marker), file_bytes=0, sha256=NULL; leaves
    status='deleted'. Idempotent — re-running on an already-clean row is a no-op write.
    Raises DatasetNotFound if the id does not exist.
    """
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE research_datasets "
            "   SET file_path = NULL, file_bytes = 0, sha256 = NULL "
            " WHERE id = ?;",
            (dataset_id,),
        )
        if cur.rowcount == 0:
            raise DatasetNotFound(f"dataset {dataset_id} not found")


# =============================================================
# M3 brief E — run dispatch + lifecycle (DECISIONS #15, #20; DDIA Ch 8)
#
# Dispatch INSERTs research_jobs + research_runs in ONE transaction (multi-object
# atomicity, Kleppmann Ch 8 / Figure 8-3: a fault discards both, never an orphan job).
# The worker callbacks (complete/fail/cancel) likewise pair a research_runs UPDATE with
# its research_run_stats write in one transaction. Logic lives here; the routes are thin
# and own the SSE broadcast (the worker callbacks run in-process, so the route can reach
# the broadcaster — unlike the CLI). The worker has no DB access (DECISIONS #20): the run
# context it needs (run_id, source_code, dataset container path, settings) is embedded in
# the job payload here at dispatch.
# =============================================================

# The six container-relative artifact path columns the result callback fills (§3.4 / §3.8).
_RUN_ARTIFACT_COLUMNS = (
    "equity_path", "drawdowns_path", "trades_path", "stats_path", "indicators_path", "meta_path",
)
# The environment columns, NULL until the result callback copies them from meta.json (§3.4).
# #376 ruling 4 (night three) extended this from four to SEVEN: sizing_method / sizing_spec /
# cost_model ride the same generic write loop complete_run/complete_sweep already use for the
# original four (environment.get(c) for c in _RUN_ENV_COLUMNS), so a run whose environment dict
# omits them (a legacy container image predating this unit) writes NULL automatically, with no
# extra branch needed at either write site.
_RUN_ENV_COLUMNS = (
    "vbt_version", "python_version", "build_provenance", "hostname",
    "sizing_method", "sizing_spec", "cost_model",
)
# Phase-2 Unit 1: the three annualization columns, filled at finalize from meta.json's `annualization`
# dict (price runs only; NULL for event/legacy runs). Keys match the dict the entrypoint writes.
_RUN_ANNUALIZATION_COLUMNS = ("annualization_factor", "annualization_freq", "annualization_year_freq")
# The eight research_run_stats columns, in schema order (§3.5).
_RUN_STATS_COLUMNS = (
    "total_return", "sharpe_ratio", "max_drawdown", "num_trades",
    "win_rate", "total_fees_paid", "total_slippage_cost", "profit_factor",
)
# M5 Track B (F7, correctness spec §7): the two cheap per-cell distribution scalars, persisted by the sweep
# (sweep_portfolio_cells) on research_run_cells ONLY. Kept SEPARATE from _RUN_STATS_COLUMNS so the fixed
# 8-column research_run_stats single-run rollup (written from _RUN_STATS_COLUMNS) is untouched; these ride
# only the SWEEP cell write + the cell grid read + the distribution summary. Nullable (None on a too-short
# returns window). Two variation_id=0 writers differ (M5 B4): a `complete_run` single-run-as-cell (the
# non-sweep price-run path) does NOT carry them (it writes only _RUN_STATS_COLUMNS; the single run derives
# its distribution on read per C2), so they stay NULL there; a ZERO-PARAM sweep cell written by
# complete_sweep_chunk (B4 _zero_param_cell) DOES carry them (computed from the run's equity-derived returns).
_CELL_DIST_COLUMNS = ("skew", "kurtosis")
# Phase-2 Unit 3: the research_run_funnel columns (minus run_id), in schema order. The funnel dict the
# entrypoint assembles in meta["funnel"] carries exactly these keys; complete_run writes them verbatim.
# Cross-deployable contract: this must equal research_lab funnel.FUNNEL_COUNT_KEYS + ("zero_reason", "lead_in_bars").
# P2g: lead_in_bars is the run's recorded lead-in. An image from before P2g omits it, so the two guards accept
# the full set or the full set minus lead_in_bars (written NULL): the dashboard can deploy before the image.
# The two cannot import each other (separate deployables), but complete_run's partial-dict guard below
# raises ValueError on the first run if meta["funnel"]'s keys do not match this set — so any drift is
# caught fail-loud at the first backtest, never silent.
_RUN_FUNNEL_COLUMNS = (
    "bars_total", "bars_after_warmup", "raw_entries", "entries_after_dedup", "raw_exits",
    "fills", "total_trades", "exits_by_signal", "exits_by_eod", "zero_reason", "lead_in_bars",
)
# The one column an older image may leave out (see above); every other funnel key is required.
_RUN_FUNNEL_OPTIONAL_COLUMNS = ("lead_in_bars",)
_RUN_FUNNEL_REQUIRED_COLUMNS = tuple(c for c in _RUN_FUNNEL_COLUMNS if c not in _RUN_FUNNEL_OPTIONAL_COLUMNS)


def _funnel_key_set_ok(keys) -> bool:
    """True for exactly the full funnel key set, or the full set minus lead_in_bars (an image from before P2g)."""
    keys = set(keys)
    return keys == set(_RUN_FUNNEL_COLUMNS) or keys == set(_RUN_FUNNEL_REQUIRED_COLUMNS)


class StrategyNotFound(LookupError):
    """No research_strategies row with the given id."""


class StrategyDeleted(RuntimeError):
    """Open / edit / dispatch attempted on a soft-deleted (tombstoned) strategy (TERM F2 keep+orphan).

    A tombstone keeps its row (so orphaned runs' name join resolves) but is gone as a live artifact:
    the GET/PUT/dispatch routes raise this so the editor cannot load it, a PUT cannot resurrect it, and
    no new run can be dispatched against it."""


class DatasetNotRunnable(RuntimeError):
    """Dispatch attempted against a dataset that is not 'ready' or has no parquet on disk."""


class RunNotFound(LookupError):
    """No research_runs row with the given id (a callback for an unknown run)."""


class SizingSpecNotRecordedError(RuntimeError):
    """A run's sizing_spec column is NULL: no resolved SizingSpec was ever recorded for it.

    Raised by get_run_lead_in_bars (lane SW, plan section 9, DECISIONS #383 decision 2), which needs
    the run's OWN recorded estimator_window to know how many lead-in bars that run's dispatch asked
    for. A run that predates lane E's sizing-provenance columns (#376 ruling 4), or a legacy price
    run with no sizing at all, carries no estimator_window to derive from, so this refuses by name
    rather than guessing a value or falling back to the registry's current default (which could have
    changed since the run actually dispatched).
    """


class RecordedSizingSpecUnreadable(RuntimeError):
    """A run's recorded sizing_spec cannot be read back as the five SizingSpec fields.

    Raised by parse_recorded_sizing_spec (lane P2f) when the stored text is empty, is not JSON, is not
    a JSON object, or its keys are not exactly SizingSpec's five field names. A replay reproduces a run
    with the sizing it recorded; a spec that cannot be read is refused by name, never defaulted to the
    registry's current method. The message starts "recorded-sizing-spec-unreadable" and names the run.
    """


class UniverseNotFound(LookupError):
    """Event-run dispatch named a universe id with no dataset_type='universe' catalog row (M3.5 brief I)."""


class StrategyNotEventDriven(ValueError):
    """Event-run dispatch named a strategy that declares no event_types (M3.5 brief I, §8/EL47)."""


class UnknownAssetClass(ValueError):
    """No asset class (and therefore no cost profile) can be resolved for a dataset (M4 D1).

    Raised by asset_class_of when a (source, dataset_type) pair names data the system has no
    cost contract for — a third price source, or an unknown dataset_type. We refuse to dispatch
    rather than default to a cost profile (the run #21 lesson: a crypto fee silently applied to an
    equity instrument). The route maps it to HTTP 422 (the D7 error contract, Phase 3); until then
    it surfaces as a loud failure on an already-invalid run, never a silent wrong-cost run.
    """


class AssetClassScopeMismatch(ValueError):
    """A run targets a dataset whose asset class is outside the strategy's declared scope (M4 D7, AD2c).

    Raised inside dispatch after the dataset's asset class is derived, when the strategy declares a
    NON-EMPTY asset_class_scope and the derived class is not a member (e.g. an equities-scoped strategy
    dispatched on a crypto dataset). A contract-bound violation — the strategy's declared contract
    forbids what the request asks — semantically the same as StrategyNotEventDriven, so the route maps
    it to HTTP 422 (not a 409: the dataset's asset class is an immutable fact, not a transient state).
    An undeclared scope (the () base default) means UNCONSTRAINED and the guard does not fire (D7);
    the Phase-1 neutral-key routing already makes every run cost-correct, so this guard is
    defense-in-depth over a SEMANTIC class mismatch, not the cost fix itself.
    """


def _canonical_settings_snapshot(settings: list[dict]) -> str:
    """Serialize the settings list to the canonical form used for byte-comparison (G-a).

    sort_keys + fixed separators make the encoding deterministic, so settings_snapshot
    and any re-serialization of the same rows are byte-identical. Pinned by the probe.
    """
    return json.dumps(settings, sort_keys=True, separators=(",", ":"))


def _run_payload(run_id: int, source_code: str, dataset_path: str,
                 settings: list[dict]) -> dict:
    """Build the worker job payload for a run_backtest dispatch (the contract brief D reads).

    Factored out so the route and the probe can build the expected payload, and so the
    dispatch atomicity probe (E-4) has a seam to inject a fault between the two INSERTs.
    """
    return {
        "run_id": run_id,            # the worker has no DB; it reads the run id from here
        "source_code": source_code,  # DB-versioned strategy source, written verbatim into the container
        "dataset_path": dataset_path,  # container-relative /data/{source}/.../x.parquet
        "settings": settings,        # list of {scope, key, value, value_type} at dispatch
    }


def _event_run_payload(run_id: int, source_code: str, settings: list[dict], *,
                       universe_id: int, knowledge_from: int, knowledge_to: int,
                       event_types: tuple[str, ...],
                       fundamental_concepts: tuple[str, ...] = (),
                       fundamental_screen: dict | None = None,
                       members_as_of: int | None = None) -> dict:
    """Build the worker job payload for an event-run dispatch (M3.5 brief I, §8 / EL72).

    Sibling of _run_payload, kept separate so the M3 price-run payload stays byte-unchanged
    (R19). `dataset_path` is None: an event run has no base price dataset (its per-symbol price
    slices are provisioned in the dedicated price brief deferred from brief I), and
    build_docker_cmd suppresses the /data/{source} mount when dataset_path is None. The four
    event fields (universe_id, the window, event_types) are what the worker reads to resolve the
    universe and fan out the materializer (the §8 run() wiring); event_types is the AST-read
    strategy declaration (OPEN-4), passed as a list so the payload is JSON-serializable.

    M3.75 FUND-4 §9: two ADDITIVE fields the worker reads for the fundamentals path —
    `fundamental_concepts` (the concept set the strategy's FundamentalFeatureDefs need; the worker
    materializes the fact-table slice when present, §6.2) and `fundamental_screen` (the universe
    screen the worker passes to /worker/universes/resolve, §9). Both are OMITTED from the payload
    when the strategy declares neither (every event-only run), so an event-only run's payload — and
    therefore its docker argv and worker behaviour — is byte-unchanged (NFR-6).
    """
    payload = {
        "run_id": run_id,            # the worker has no DB; it reads the run id from here
        "source_code": source_code,  # DB-versioned strategy source, written verbatim into the container
        "dataset_path": None,        # no base price dataset (EL72); the price brief provisions per-symbol slices
        "settings": settings,        # list of {scope, key, value, value_type} at dispatch
        "universe_id": universe_id,  # run-level (OPEN-4); the worker resolves it to a CIK union (§4)
        "knowledge_from": knowledge_from,  # epoch s; backtest-window start (inclusive)
        "knowledge_to": knowledge_to,      # epoch s; window end / no-lookahead bound (exclusive, §4/R10)
        "event_types": list(event_types),  # strategy-intrinsic (OPEN-4); the worker fans out one slice per type
    }
    if fundamental_concepts:  # FUND-4 §9: present iff the strategy declares FundamentalFeatureDef concepts
        payload["fundamental_concepts"] = list(fundamental_concepts)  # the worker materializes the fact slice
    if fundamental_screen is not None:  # FUND-4 §9: present iff the strategy declares a universe screen
        payload["fundamental_screen"] = fundamental_screen  # the worker passes it to /worker/universes/resolve
    if members_as_of is not None:  # DL1: present iff the universe is a price universe (the list as of dispatch)
        payload["members_as_of"] = members_as_of  # the worker sends it to /worker/universes/resolve
    return payload


def _event_run_data_sha256(universe_id: int, knowledge_from: int, knowledge_to: int,
                           event_types: tuple[str, ...],
                           fundamental_concepts: tuple[str, ...] = (),
                           fundamental_screen: dict | None = None,
                           members_as_of: int | None = None) -> str:
    """Deterministic content fingerprint of an event run's data declaration (M3.5 brief I, BN-I).

    research_runs.dataset_sha256 is NOT NULL and denormalized so it survives the referenced row's
    deletion. An event run has no price parquet to hash, and §23 forbids a schema change to relax
    the column, so its fingerprint is the canonical data declaration (universe + window + event
    types) — the inputs that define which events the run reads. For M3.5's static today's-list
    membership the universe_id fully determines membership, so this is a faithful run-data identity.
    When a real point-in-time membership source lands (R14, §22), the fingerprint should fold in
    the resolved membership; flagged here, not built (no schema or behaviour owed in brief I).

    M3.75 FUND-4 §9: `fundamental_concepts` and `fundamental_screen` are folded in BECAUSE they
    change which data the run reads (a screened universe is a different member set; a feature concept
    is a different materialized slice). They are added with defaults `()`/`None`, so an event run that
    declares neither produces the SAME fingerprint as before this change (R19/NFR-6 — existing event
    runs keep their identity), and a screened/feature run gets a distinct, reproducible identity.
    """
    decl = {"universe_id": universe_id, "knowledge_from": knowledge_from,
            "knowledge_to": knowledge_to, "event_types": list(event_types)}
    if fundamental_concepts:  # omitted when absent -> existing event runs keep their exact fingerprint
        decl["fundamental_concepts"] = list(fundamental_concepts)
    if fundamental_screen is not None:  # omitted when absent -> existing event runs keep their exact fingerprint
        decl["fundamental_screen"] = fundamental_screen
    if members_as_of is not None:  # DL1: a price universe's list changes over time, so the run's date is part of its data
        decl["members_as_of"] = members_as_of  # omitted for a CIK universe -> its fingerprint is byte-unchanged
    canonical = json.dumps(decl, sort_keys=True, separators=(",", ":"))  # canonical form: deterministic across calls
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _live_settings(conn) -> list[dict]:
    """Read research_settings as a typed list of {scope, key, value, value_type}, ordered."""
    rows = conn.execute(
        "SELECT scope, key, value, value_type FROM research_settings ORDER BY scope, key;"
    ).fetchall()
    return [
        {"scope": r["scope"], "key": r["key"],
         "value": parse_value(r["value"], r["value_type"]), "value_type": r["value_type"]}
        for r in rows
    ]


def _resolve_strategy_settings(conn, strategy_id: int, settings: list[dict]) -> list[dict]:
    """Overlay a strategy's per-strategy overrides onto the global settings list.

    The SINGLE override-resolution implementation (TERM M4.5 brief 3a B1.2 / arch D2). Reads
    research_strategy_settings for this strategy, parses each value per its value_type, and overlays
    onto `settings` by (scope, key): an existing key is REPLACED (an override of a global default), a
    new key is APPENDED (an authored param with no global — reachable only via 3b's authoring path; in
    3a the value-only PUT route rejects an unknown key, so the append branch is exercised by the probe).
    Mutates and returns the SAME list object, so the one resolved list feeds BOTH the run snapshot
    (_canonical_settings_snapshot) and the worker payload (_run_payload/_event_run_payload) — executed
    values == recorded values by construction (R-OV3). Reused by both dispatch paths AND the drift read
    (research/export.py) — never reimplemented. Pure key-merge; no asset-class dependency.
    """
    rows = conn.execute(
        "SELECT scope, key, value, value_type FROM research_strategy_settings WHERE strategy_id = ?;",
        (strategy_id,),
    ).fetchall()
    if not rows:
        return settings
    index = {(s["scope"], s["key"]): s for s in settings}  # existing global rows by (scope, key)
    for r in rows:
        value = parse_value(r["value"], r["value_type"])
        existing = index.get((r["scope"], r["key"]))
        if existing is not None:
            existing["value"] = value                  # override the global value (same dict in the list)
            existing["value_type"] = r["value_type"]
        else:
            appended = {"scope": r["scope"], "key": r["key"],
                        "value": value, "value_type": r["value_type"]}  # authored param, no global (3b path)
            settings.append(appended)
            index[(r["scope"], r["key"])] = appended
    return settings


def list_distinct_price_dataset_symbols() -> list[str]:
    """The distinct symbols of every READY price dataset (F7 news fetch scope).

    Returns the sorted distinct `symbol` of research_datasets rows with
    dataset_type='price_dataset' (the polymorphic discriminator — the table also holds
    'event_series'/'universe'/'fundamentals' rows whose `symbol` is not a tradable ticker)
    AND status='ready' (a deleted/failed/pending dataset should not pull news). The F7 news
    fetcher reads this, then filters to equity symbols (excluding Binance-style 'X/Y' crypto)
    for the first cut. There is no pre-existing distinct-symbol read — resolve_price_slices
    CONSUMES a symbol list, it does not produce one.
    """
    with get_conn() as conn:                                   # read-only
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM research_datasets "
            "WHERE dataset_type = 'price_dataset' AND status = 'ready' "
            "ORDER BY symbol"
        ).fetchall()
    return [r["symbol"] for r in rows]


def resolve_price_slices(symbols: list[str], timeframe: str = "1d") -> dict:
    """Resolve each symbol to its ready daily price parquet (M3.5 price-provisioning brief, EL70 finalization).

    For each requested symbol, find the research_datasets row with dataset_type='price_dataset' (the
    polymorphic discriminator — the table also holds 'event_series' and 'universe' rows, so every read
    must filter the kind), timeframe=? (daily-only at M3.5), status='ready', and file_path NOT NULL,
    choosing the WIDEST single [start_ts, end_ts) row (tie-break: most rows, then lowest id, for a
    reproducible pick). Returns {"resolved": {symbol: container_relative_file_path}, "unresolved":
    [symbol, ...]}. The worker has no DB, so it calls this over POST /worker/price-slices/resolve;
    file_path is the container-relative /data/... path the worker already holds locally (no bytes move).
    """
    if not symbols:                                          # nothing to resolve
        return {"resolved": {}, "unresolved": []}
    placeholders = ",".join("?" for _ in symbols)            # one IN (...) query over the batch, not N queries
    with get_conn() as conn:                                 # read-only
        rows = conn.execute(
            "SELECT symbol, file_path FROM research_datasets "
            "WHERE dataset_type = 'price_dataset' "          # the polymorphic discriminator (never read a foreign row kind)
            "  AND timeframe = ? "                            # daily-only at M3.5 (the locked granularity)
            "  AND status = 'ready' AND file_path IS NOT NULL "
            f"  AND symbol IN ({placeholders}) "
            "ORDER BY symbol, (end_ts - start_ts) DESC, rows DESC, id ASC;",  # widest single row first, per symbol
            (timeframe, *symbols),
        ).fetchall()
    resolved: dict[str, str] = {}
    for row in rows:                                         # ORDER BY puts the chosen row first per symbol
        if row["symbol"] not in resolved:                   # keep the first (widest) row, ignore the rest
            resolved[row["symbol"]] = row["file_path"]
    unresolved = [s for s in symbols if s not in resolved]  # symbols with no ready daily price_dataset row
    return {"resolved": resolved, "unresolved": unresolved}


def _default_price_run_name(strategy_name: str, symbol: str, timeframe: str) -> str:
    """Auto-name for a price run when the caller supplies none (Unit-4 D3).

    Format: '{strategy} · {symbol} {TIMEFRAME}', e.g. 'ma_cross · GOOG 1H'. The timeframe is
    upper-cased so the stored '1h' reads as '1H'. A presentation constant, not a backtest
    parameter (no parameter_provenance entry); names are non-unique and the run id disambiguates.
    """
    return f"{strategy_name} · {symbol} {timeframe.upper()}"


def _default_event_run_name(strategy_name: str, universe_name: str,
                            knowledge_from: int, knowledge_to: int) -> str:
    """Auto-name for an event run when the caller supplies none (Unit-4 D3).

    Format: '{strategy} · {universe} {from_year}–{to_year}', e.g. 'aapl_fund_e2e · aapl-e2e
    2019–2026'. The window bounds are epoch seconds UTC; the years are read with time.gmtime
    (UTC, deterministic), so the same window always yields the same name.
    """
    y0 = time.gmtime(knowledge_from).tm_year
    y1 = time.gmtime(knowledge_to).tm_year
    return f"{strategy_name} · {universe_name} {y0}–{y1}"


# Asset-class cost routing (M4 D1/D2). asset_class_of maps a dataset to its asset class; the
# injection helper turns the operator-edited class-named cost keys into the single neutral key the
# base contract reads. Both live beside the dispatch functions that are their only callers.
_ASSET_CLASSES = ("crypto", "equities")  # the closed vocabulary; a dataset resolving to neither is refused


def asset_class_of(source: str | None, dataset_type: str) -> str:
    """Resolve a dataset's asset class from its catalog (source, dataset_type) — a pure function (M4 D1).

    The class decides which cost profile a run pays. Computed where needed, stored nowhere (AD2a).
    Engine-neutral: source, dataset_type, and the class names name no engine.

      - dataset_type='universe' -> equities is a NAMED INVARIANT: every universe is a set of SEC CIKs
        (US equities). `source` is unused on this branch (hence | None — the event path passes None
        rather than fetching a dead column). A non-equities universe kind, if ever introduced, revisits
        this invariant as a named change.
      - dataset_type='price_dataset' -> binance is crypto, alpaca_sip is equities. A third price source
        has no cost contract, so it is refused, never defaulted.
      - any other dataset_type has no cost contract, so it too is refused.

    Refusing rather than defaulting is the run #21 lesson: a guessed profile silently charged a crypto
    fee against an equity instrument. The refusal (UnknownAssetClass) maps to HTTP 422 at the route
    (D7, Phase 3); meanwhile it surfaces loud on an already-invalid run, never a silent wrong cost.
    """
    if dataset_type == "universe":            # the universe invariant: source ignored
        return "equities"
    if dataset_type == "price_dataset":
        if source == "binance":
            return "crypto"
        if source == "alpaca_sip":
            return "equities"
        if source == "eodhd":                # M5 data-store: EODHD daily is US equities (total-return)
            return "equities"
        if source == "synthetic":            # GV2-4a: made-up stocks are priced like US equities
            return "equities"
        raise UnknownAssetClass(             # a further price source is refused, not guessed
            f"cannot resolve an asset class for (source={source!r}, dataset_type='price_dataset'): "
            f"no cost profile is defined for this data source. Asset class must be one of "
            f"{{{', '.join(_ASSET_CLASSES)}}}; refusing dispatch rather than defaulting to a cost "
            f"profile (the run #21 lesson)."
        )
    raise UnknownAssetClass(                  # an unknown dataset_type is refused, not guessed
        f"cannot resolve an asset class for (source={source!r}, dataset_type={dataset_type!r}): "
        f"no cost profile is defined for this dataset type. Asset class must be one of "
        f"{{{', '.join(_ASSET_CLASSES)}}}; refusing dispatch rather than defaulting (the run #21 lesson)."
    )


def _dict_setting_value(settings: list[dict], scope: str, key: str) -> int | float:
    """Read one (scope, key) value out of a dispatch-time settings list — the dict-shaped get_setting.

    Dispatch holds settings as `list[dict]` (_live_settings, the storage + payload shape), NOT the
    worker-side `list[SettingRow]` that strategies read via get_setting (settings.py): get_setting does
    attribute access (row.scope) and cannot read these dicts. Same fail-loud contract as get_setting —
    KeyError when absent, never a silent fallback — because a missing class-named cost key is a seeding
    bug, not a default to swallow.
    """
    for row in settings:  # settings lists are tiny (single digits); a linear scan is fine
        if row["scope"] == scope and row["key"] == key:
            return row["value"]
    raise KeyError(f"cost setting not found: scope={scope!r} key={key!r}")


def _inject_neutral_cost_keys(settings: list[dict], asset_class: str) -> None:
    """Append the neutral cost rows (fees/default, slippage/default) for an asset class (M4 D2).

    Mutates `settings` IN PLACE, before the snapshot and the worker payload are built, so both carry
    the neutral rows and the worker stays a pure consumer (it derives no asset class). The class-named
    rows are left untouched: AD2 forbids rewriting `crypto.default`'s value (that would lie about what
    `crypto.default` is); the neutral rows are an ADDITIVE projection of the resolved class value.

    For every existing run the resolved value is byte-identical to the class-named key the base
    contract read before this change — an equities run reads the injected 'default' set to the equities
    value, a crypto run to the crypto value. Only the wrong pairing (run #21) changes, to the correct
    value. The append order (fees then slippage) is fixed so _canonical_settings_snapshot is
    deterministic across calls.
    """
    fees = _dict_setting_value(settings, "fees", f"{asset_class}.default")          # the class's fee, e.g. fees/crypto.default
    slippage = _dict_setting_value(settings, "slippage", f"{asset_class}.default")  # the class's slippage
    settings.append({"scope": "fees", "key": "default", "value": fees, "value_type": "number"})          # the neutral fee the base reads
    settings.append({"scope": "slippage", "key": "default", "value": slippage, "value_type": "number"})  # the neutral slippage


def _assert_asset_class_in_scope(source_code: str, strategy_name: str,
                                 asset_class: str, dataset_desc: str) -> None:
    """Fail closed when a run's asset class is outside the strategy's declared scope (M4 R2c/D7/AD2c).

    Reads the declared asset_class_scope off the strategy source by AST (extract_asset_class_scope, no
    exec). An EMPTY scope is UNCONSTRAINED — the guard does not fire (D7 empty-scope semantics). A
    non-empty scope that does not contain the derived asset class raises AssetClassScopeMismatch (mapped
    to 422 at the route), carrying full context. Called in BOTH dispatch functions after the class is
    derived and BEFORE the neutral-key injection + the job/run INSERTs, so a mismatched run never gets a
    cost profile or a row. Defense-in-depth over the Phase-1 cost routing, not the cost fix itself.
    """
    scope = extract_asset_class_scope(source_code)  # () = unconstrained; {crypto, equities} otherwise
    if scope and asset_class not in scope:  # a declared scope that excludes this run's class
        raise AssetClassScopeMismatch(
            f"strategy {strategy_name!r} declares asset-class scope {scope} but this run targets a "
            f"{asset_class} dataset ({dataset_desc}); refusing to run a "
            f"{'/'.join(scope)}-scoped strategy on {asset_class} data (fail-closed, the run #21 lesson)."
        )


def dispatch_run(strategy_id: int, dataset_id: int, name: str | None = None,
                 now: int | None = None, kind: str | None = None) -> dict:
    """Dispatch a run_backtest: INSERT the job + the pending run row atomically (Ch 8).

    Reads the strategy source/version, the dataset container path + sha256, and the live
    settings, then in ONE transaction inserts research_jobs (kind run_backtest) and
    research_runs (status pending, job_id linked, settings_snapshot + version + sha256
    captured, optional user-supplied `name`, env columns NULL), embeds run_id + source_code
    + dataset path + settings in the job payload, and writes the 'created' audit event.
    Returns the data the route needs for the research.run_created SSE plus the snapshot
    /settings the probe asserts. `name` is purely a UI label and never crosses into the
    worker payload.

    Raises:
      StrategyNotFound     -> 404
      StrategyDeleted      -> 409 (the strategy is soft-deleted; no new run on a tombstone)
      DatasetNotFound      -> 404
      DatasetNotRunnable   -> 409 (not 'ready', or no parquet on disk)
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:  # BEGIN IMMEDIATE; both INSERTs commit together or roll back together
        # Unit-4 D3: the SELECT also fetches `name` so a blank dispatch can compose a default.
        strat = conn.execute(
            "SELECT id, source_code, version, name, status FROM research_strategies WHERE id = ?;",
            (strategy_id,),
        ).fetchone()
        if strat is None:
            raise StrategyNotFound(f"strategy {strategy_id} not found")
        if strat["status"] == 'deleted':  # TERM F2: refuse a new run on a soft-deleted strategy
            raise StrategyDeleted(f"strategy {strategy_id} is deleted; cannot dispatch a new run")

        # Unit-4 D3: the SELECT also fetches `symbol`/`timeframe` for the composed default name.
        # M4 D2: it also fetches `source`/`dataset_type` so asset_class_of can route the run's cost
        # profile (the neutral-key injection below) without a second query.
        ds = conn.execute(
            "SELECT id, status, file_path, sha256, symbol, timeframe, source, dataset_type "
            "FROM research_datasets WHERE id = ?;",
            (dataset_id,),
        ).fetchone()
        if ds is None:
            raise DatasetNotFound(f"dataset {dataset_id} not found")
        if ds["status"] != "ready" or ds["file_path"] is None or ds["sha256"] is None:
            raise DatasetNotRunnable(
                f"dataset {dataset_id} is not runnable (status={ds['status']!r}, "
                f"file_path={'set' if ds['file_path'] else 'NULL'})"
            )

        # Unit-4 D3: the dispatch boundary is the single guarantee that a run always has a
        # readable name; a blank or whitespace-only name (from any caller, not just NewRunPage)
        # is replaced by the composed default before the INSERT. An explicit name is trimmed and
        # kept. The name is a UI label only and still never crosses into the worker payload.
        name = name.strip() if name else None
        if not name:
            name = _default_price_run_name(strat["name"], ds["symbol"], ds["timeframe"])

        settings = _live_settings(conn)  # typed rows
        # M4 D2: route this run's cost profile by asset class. Derive the class from the dataset's
        # (source, dataset_type), then inject the neutral fees/default + slippage/default rows the base
        # contract reads. Mutates `settings` BEFORE the snapshot and payload below, so both carry the
        # neutral rows. asset_class_of refuses (UnknownAssetClass) an unknown source rather than
        # guessing a cost profile (the run #21 lesson).
        asset_class = asset_class_of(ds["source"], ds["dataset_type"])
        # M4 R2c/D7: fail closed if this dataset's class is outside the strategy's declared scope, BEFORE
        # any cost injection or job/run INSERT. An unconstrained (undeclared) scope does not fire (D7).
        _assert_asset_class_in_scope(
            strat["source_code"], strat["name"], asset_class, f"{ds['symbol']} via {ds['source']}",
        )
        # TERM M4.5 3a B1.3: overlay this strategy's per-strategy overrides AFTER the asset-class guard
        # (which still fails closed first, on the unmodified scope) and BEFORE neutral-cost injection, so
        # the neutral fees/slippage derive from the OVERRIDDEN class rates. The one shared helper mutates
        # `settings` once; the snapshot and payload below read the same list (R-OV3).
        settings = _resolve_strategy_settings(conn, strategy_id, settings)
        _inject_neutral_cost_keys(settings, asset_class)
        snapshot = _canonical_settings_snapshot(settings)  # canonical JSON for storage + comparison (G-a)

        # 1. Job row first — research_runs.job_id is a NOT NULL UNIQUE FK, so the job id must exist.
        #    payload_json is filled in step 3 once run_id is known.
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES (?, '', ?, 'pending') RETURNING id;",  # kind bound from the constant run_nature reads (1.3 single-source)
            (JOB_KIND_RUN_BACKTEST, now),
        ).fetchone()
        job_id = job["id"]

        # 2. Pending run row (env columns NULL; the result callback fills them from meta.json).
        #    `name` is stored verbatim as supplied by the caller (None = no name); not unique.
        # M4 D5: `kind` marks the run's trial provenance — NULL for a standard run (existing callers),
        # 'scratch' for an editor test run (R8). It is a run-level marker, never in the worker payload.
        run = conn.execute(
            "INSERT INTO research_runs "
            "    (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "     settings_snapshot, name, created_at, status, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?) RETURNING id;",
            (job_id, strategy_id, strat["version"], dataset_id, ds["sha256"], snapshot, name, now, kind),
        ).fetchone()
        run_id = run["id"]

        # 3. Now that run_id exists, build the worker payload and write it onto the job.
        payload = _run_payload(run_id, strat["source_code"], ds["file_path"], settings)
        conn.execute(
            "UPDATE research_jobs SET payload_json = ? WHERE id = ?;",
            (json.dumps(payload), job_id),
        )

        # 4. Audit event, matching the M2 create_job convention.
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, 'created', ?);",
            (job_id, now, json.dumps({"kind": "run_backtest", "run_id": run_id})),
        )

    return {
        "job_id": job_id, "run_id": run_id, "strategy_id": strategy_id,
        "strategy_version": strat["version"], "dataset_id": dataset_id, "created_at": now,
        "name": name,  # Unit-4 D3: the resolved name (composed default when the caller gave none)
        "kind": kind,  # M4 D5: the trial-provenance marker (None=standard, 'scratch'=editor test run)
        "settings_snapshot": snapshot, "settings": settings, "payload": payload,
    }


def dispatch_event_run(strategy_id: int, universe_id: int,
                       knowledge_from: int, knowledge_to: int,
                       name: str | None = None, now: int | None = None,
                       kind: str | None = None) -> dict:
    """Dispatch an event-driven backtest: INSERT the job + pending run row atomically (M3.5 brief I, OPEN-6).

    The event-run sibling of dispatch_run (the M3 price-run dispatch is byte-unchanged, R19). An
    event run has no price dataset_id; it has a universe (universe_id) and a window
    (knowledge_from/knowledge_to), and event_types read off the strategy source by AST (OPEN-4).
    The job kind stays 'run_backtest' — the same worker handler runs it and run() branches on the
    event payload fields (§8). research_runs.dataset_id/dataset_sha256 are NOT NULL and §23 forbids
    a schema change, so (BN-I): dataset_id = universe_id (the universe IS a research_datasets row,
    so the FK is satisfied and ON DELETE RESTRICT correctly pins a universe a run depends on), and
    dataset_sha256 = the data-declaration fingerprint. The four event fields are embedded in the
    job payload (the worker has no DB to resolve them from). `name` is a UI label only, like
    dispatch_run. The window is validated at the route body (CreateEventJobBody).

    Raises:
      StrategyNotFound        -> 404
      StrategyDeleted         -> 409 (the strategy is soft-deleted; no new run on a tombstone)
      StrategyNotEventDriven  -> 422 (the strategy declares no event_types; not event-driven)
      StrategyValidationError -> 422 (the stored source has a malformed event_types declaration;
                                 normally precluded by validate_strategy_source, but a source stored
                                 before that contract existed can still trip the AST read here)
      UniverseNotFound        -> 404
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:  # BEGIN IMMEDIATE; both INSERTs commit together or roll back together
        # Unit-4 D3: the SELECT also fetches `name` so a blank dispatch can compose a default.
        strat = conn.execute(
            "SELECT id, source_code, version, name, status FROM research_strategies WHERE id = ?;",
            (strategy_id,),
        ).fetchone()
        if strat is None:
            raise StrategyNotFound(f"strategy {strategy_id} not found")
        if strat["status"] == 'deleted':  # TERM F2: refuse a new run on a soft-deleted strategy
            raise StrategyDeleted(f"strategy {strategy_id} is deleted; cannot dispatch a new run")

        event_types = extract_event_types(strat["source_code"])  # AST read of the declared literal (OPEN-4)
        if not event_types:
            raise StrategyNotEventDriven(
                f"strategy {strategy_id} declares no event_types; it is not an event-driven strategy"
            )
        # M3.75 FUND-4 §9: the fundamentals declarations, read off the SAME source by AST (no exec,
        # the OPEN-4 boundary). `fundamental_concepts` is the concept set the strategy's
        # FundamentalFeatureDefs need (the worker materializes the fact slice when present);
        # `fundamental_screen` is the universe screen the worker passes to /worker/universes/resolve.
        # Both default empty/None for every strategy that declares neither, so those runs are
        # byte-unchanged. validate_strategy_source already rejected a malformed declaration at write
        # time, so these reads do not surface a new dispatch-time failure for a contract-valid source.
        fundamental_concepts = extract_fundamental_concepts(strat["source_code"])  # () when none declared
        fundamental_screen = extract_fundamental_screen(strat["source_code"])      # None when none declared

        # The universe is a CIK universe (DATASET_TYPE_UNIVERSE) OR, since B2 (AD-UU-2, the read-time
        # bridge), a ticker-keyed price universe (DATASET_TYPE_PRICE_UNIVERSE) resolved to CIKs on read
        # at the worker resolve step. Checked on the transaction connection (not events.universe.universe_exists,
        # which opens its own) to keep the read inside the dispatch transaction. The two type values are
        # bound from the module-local constants (defined in this file) so a rename cannot drift this query;
        # the dispatch_sweep universe SELECT binds DATASET_TYPE_PRICE_UNIVERSE the same way.
        # Unit-4 D3: the SELECT also fetches the universe `name` for the composed default run name.
        urow = conn.execute(
            "SELECT id, name, dataset_type FROM research_datasets "
            "WHERE id = ? AND dataset_type IN (?, ?);",
            (universe_id, DATASET_TYPE_UNIVERSE, DATASET_TYPE_PRICE_UNIVERSE),
        ).fetchone()
        if urow is None:
            raise UniverseNotFound(f"universe {universe_id} not found")

        # Unit-4 D3: same single-guarantee idiom as dispatch_run — a blank/whitespace-only name
        # (from any caller) is replaced by the composed default before the INSERT; an explicit
        # name is trimmed and kept. The name is a UI label only and never crosses to the worker.
        name = name.strip() if name else None
        if not name:
            name = _default_event_run_name(strat["name"], urow["name"], knowledge_from, knowledge_to)

        # DL1: a price universe's list is dated, so the run pins the date it was dispatched on (its created_at)
        # and its resolve reads the list as of then; a CIK universe pins nothing (its membership has its own dates).
        members_as_of = now if urow["dataset_type"] == DATASET_TYPE_PRICE_UNIVERSE else None
        dataset_sha256 = _event_run_data_sha256(
            universe_id, knowledge_from, knowledge_to, event_types,
            fundamental_concepts, fundamental_screen,  # FUND-4 §9: fold the fundamentals declaration into the identity
            members_as_of)
        settings = _live_settings(conn)  # typed rows
        # M4 D2: an event run resolves to a set of SEC CIKs (equities). For a CIK 'universe' that is the
        # named invariant (asset_class_of(None, 'universe')). For a 'price_universe' (B2) the members are
        # eodhd price datasets, so the class is derived through the MEMBER data class (the dispatch_sweep
        # idiom) — never asset_class_of(_, 'price_universe'), which has no cost contract and would refuse.
        # Both branches resolve to equities. Inject the neutral fees/default + slippage/default rows below.
        if urow["dataset_type"] == DATASET_TYPE_PRICE_UNIVERSE:
            asset_class = asset_class_of("eodhd", DATASET_TYPE_PRICE)  # = equities (the member data class)
        else:
            asset_class = asset_class_of(None, "universe")            # = equities (the universe invariant)
        # M4 R2c/D7: fail closed if equities is outside the strategy's declared scope. For a correctly
        # declared event strategy (scope ("equities",)) it never fires — a universe is always equities —
        # so this is defense-in-depth that would catch e.g. a crypto-only strategy dispatched on a
        # universe. An unconstrained (undeclared) scope does not fire (D7).
        _assert_asset_class_in_scope(
            strat["source_code"], strat["name"], asset_class, f"universe {urow['name']!r}",
        )
        # TERM M4.5 3a B1.3: overlay this strategy's overrides after the guard, before neutral-cost
        # injection — the same single helper + ordering as the price path (R-OV3).
        settings = _resolve_strategy_settings(conn, strategy_id, settings)
        _inject_neutral_cost_keys(settings, asset_class)
        snapshot = _canonical_settings_snapshot(settings)  # canonical JSON for storage + comparison (G-a)

        # 1. Job row first — research_runs.job_id is a NOT NULL UNIQUE FK, so the job id must exist.
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES (?, '', ?, 'pending') RETURNING id;",  # kind bound from the constant run_nature reads (1.3 single-source)
            (JOB_KIND_RUN_BACKTEST, now),
        ).fetchone()
        job_id = job["id"]

        # 2. Pending run row. dataset_id = universe_id, dataset_sha256 = the declaration fingerprint (BN-I).
        #    M4 D5: `kind` marks the trial provenance (NULL=standard, 'scratch'=editor test run, R8).
        run = conn.execute(
            "INSERT INTO research_runs "
            "    (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "     settings_snapshot, name, created_at, status, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?) RETURNING id;",
            (job_id, strategy_id, strat["version"], universe_id, dataset_sha256, snapshot, name, now, kind),
        ).fetchone()
        run_id = run["id"]

        # 3. Now that run_id exists, build the worker payload and write it onto the job.
        payload = _event_run_payload(
            run_id, strat["source_code"], settings,
            universe_id=universe_id, knowledge_from=knowledge_from,
            knowledge_to=knowledge_to, event_types=event_types,
            fundamental_concepts=fundamental_concepts,  # FUND-4 §9: present iff declared (worker materializes the slice)
            fundamental_screen=fundamental_screen,      # FUND-4 §9: present iff declared (worker passes it to resolve)
            members_as_of=members_as_of,                # DL1: present iff a price universe
        )
        conn.execute(
            "UPDATE research_jobs SET payload_json = ? WHERE id = ?;",
            (json.dumps(payload), job_id),
        )

        # 4. Audit event, matching the dispatch_run convention (carries the event context).
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, 'created', ?);",
            (job_id, now, json.dumps({"kind": "run_backtest", "run_id": run_id,
                                      "universe_id": universe_id, "event_types": list(event_types)})),
        )

    return {
        "job_id": job_id, "run_id": run_id, "strategy_id": strategy_id,
        "strategy_version": strat["version"], "universe_id": universe_id,
        "knowledge_from": knowledge_from, "knowledge_to": knowledge_to,
        "event_types": list(event_types), "created_at": now,
        "name": name,  # Unit-4 D3: the resolved name (composed default when the caller gave none)
        "kind": kind,  # M4 D5: the trial-provenance marker (None=standard, 'scratch'=editor test run)
        "settings_snapshot": snapshot, "settings": settings, "payload": payload,
    }


# =============================================================
# M5 U2 / 1C — the universe-sweep dispatch (dispatch_sweep), AD-M5-12/13, the two-phase orchestration.
#
# Phase 1 (dispatch_sweep, atomic): freeze+hash the grid, then in ONE BEGIN IMMEDIATE transaction write the
# run row (status='pending'; the run's CHECK forbids 'materializing', so the materializing state rides the
# JOB), the frozen research_sweep_registrations row (the gate complete_sweep_chunk reads it), the run_sweep job
# stub (status='materializing', a status the poll's WHERE status='pending' never claims), and one
# research_sweep_members link per resolved member dataset. Phase 2 materializes each member async via the #177
# fetch_dataset path; once every member is 'ready', the backend-side _finalize_materializing_sweep assembles
# the six-field member list + the universe_sha256 and flips the job to 'pending' so the worker runs the sweep.
# The worker has no DB (DECISIONS #20) and the eodhd files are worker-local, so this assemble+enqueue MUST be
# backend-driven (the ready-trigger in worker_dataset_result), never a worker job that would block the single
# worker on N fetches.
# =============================================================

# The one-pass cell cap (R-CEIL, arch §10 / AD-M5-8): a job-config number, NOT a backtest parameter (above it
# the worker chunks — SAME results, bounded peak memory — so it owes no parameter_provenance, the same class as
# a chunk length). ~50M cells ≈ 3.5 GB peak, safely inside the realistic ~8 GB working budget with the dashboard
# running (the feasibility findings' measured ~70 MB/M-cells curve, docs/kickoffs/m5_vbt_feasibility_findings.md).
# Config, not schema, so a bigger machine raises it without a rebuild.
_SWEEP_SIZE_CAP_CELLS = 50_000_000

# The run_sweep job's pre-ready status: a value the poll's WHERE status='pending' never claims, so an
# un-materialized sweep is never handed to the worker. research_jobs.status has NO CHECK (unlike
# research_runs.status), so this is admissible; the RUN stays 'pending' and the materializing state lives on
# the JOB. _finalize_materializing_sweep flips it to 'pending' when every member is ready.
_MATERIALIZING_JOB_STATUS = "materializing"


def _freeze_sweep_grid(param_grid: dict) -> tuple[str, str, int]:
    """Freeze + hash a sweep's parameter grid (AD-M5-6/13 phase 1). Returns (grid_json, grid_hash, registered_n).

    The grid is {param_name: [values]} (the worker's contract). It is canonicalized so two dispatches of the
    same logical grid freeze identically: param names sorted, each value list de-duplicated and sorted (the
    worker assigns variation_ids by canonical_combo_order regardless of input order, so a different input order
    is the SAME grid). registered_n is the parameter-COMBINATION count (the product of the per-param unique
    value counts) — member-INDEPENDENT, the only honest count at registration time under the two-phase flow
    (the member count is unknown until materialize completes). A "no-grid" universe run is one value per param
    -> registered_n=1 (Step 5 degenerate one-combination grid). Engine-neutral: the backend hashes the
    declaration; vbt.Param generation is the worker's (AD-M5-10). Fails loud on a malformed grid (no fallback).
    """
    if not isinstance(param_grid, dict) or not param_grid:  # the worker refuses an empty grid too (§5.4)
        raise ValueError("sweep param_grid must be a non-empty {param_name: [values]} object")
    canonical: dict[str, list] = {}
    registered_n = 1
    for pname in sorted(param_grid):                     # sort param names for a deterministic freeze
        values = param_grid[pname]
        if not isinstance(values, list) or not values:   # every declared param needs at least one value
            raise ValueError(f"sweep param_grid[{pname!r}] must be a non-empty list of values")
        unique = list(dict.fromkeys(values))             # de-dup, preserving first-seen (the worker dedups combos too)
        try:
            unique = sorted(unique)                      # sort so value input order does not change the hash (numeric per the SweepParam contract)
        except TypeError as exc:                         # mixed/unsortable values are a MALFORMED grid, not a fallback to swallow:
            raise ValueError(                            # keeping insertion order here would silently break the freeze-determinism the hash relies on
                f"sweep param_grid[{pname!r}] values are not uniformly sortable (mixed types): {values!r}; "
                f"all values for one parameter must share a type (the SweepParam int/float contract)"
            ) from exc
        canonical[pname] = unique
        registered_n *= len(unique)                      # the combination count is the product of per-param value counts
    grid_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))  # canonical, deterministic
    grid_hash = hashlib.sha256(grid_json.encode("utf-8")).hexdigest()
    return grid_json, grid_hash, registered_n


def _sweep_universe_sha256(member_sha256s: list[str]) -> str:
    """The universe identity hash (AD-M5-12, R-EXP): a content hash over the SORTED set of member dataset_sha256.

    The same tickers at the same content reproduce the same universe identity; a member's data changing changes
    the hash. Computed in _finalize_materializing_sweep AFTER the member sha256 exist (phase 2). Sorted + set so
    the order the members were resolved in does not change the identity.
    """
    canonical = json.dumps(sorted(set(member_sha256s)), separators=(",", ":"))  # sorted set -> deterministic
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_sweep_run_name(strategy_name: str, universe_name: str) -> str:
    """Auto-name a universe sweep when the caller gives none: '{strategy} · {universe} (universe sweep)'."""
    return f"{strategy_name} · {universe_name} (universe sweep)"


# Lane RM (set 16b, plan amendment 7, docs/units/RM/reads/probe_report_2026-09-25_1.md): the fixed
# sizing lead-in bar count, restated here as its own literal for the SAME reason
# dashboard/backend/stage_runner.py restates it (SR-16's own comment: this file sits BELOW
# stage_runner in the import graph, and stage_runner imports researchdb, so the reverse import
# would cycle), one ruled source, DECISIONS #304 ruling 4 and
# docs/research_lab/parameter_provenance.md's sizing.estimator_window, answered by a THIRD plain
# restatement rather than a second import path.
_MEMBER_DATASET_MIN_LEAD_IN_BARS = 278

# Lane RM (set 16b, plan amendment 7, AC-9): a process-local counter, the SAME idiom
# LEAD_IN_PRECHECK_MONITOR_COUNTERS and STAGE_RUNNER_MONITOR_COUNTERS already use, for the ONE
# condition this fold newly makes visible: a candidate 'ready' member dataset that this run's own
# resolution PROVED short of the lead-in and skipped rather than reused. A steady trickle is
# harmless (it just means an earlier, narrower dataset exists for that symbol); a spike would mean
# something is repeatedly asking for windows the catalog cannot yet serve from any existing file.
MEMBER_DATASET_MONITOR_COUNTERS: dict[str, int] = {
    "member_dataset_lead_in_skips": 0,
    # Lane RM round 3 (docs/units/RM/reads/read_1.md, R1, SHOULD-FIX): a ready row whose file
    # this host cannot open at all (production has none: see the fix's own comment below) AND
    # whose stored window is not the canonical full floor-to-now window, so nothing here can
    # prove it covers the lead-in and it is skipped rather than trusted. Distinct from
    # member_dataset_lead_in_skips, which counts a row the file itself PROVED short.
    "member_dataset_unverifiable_narrow_skips": 0,
    # Row `3.4d` (DECISIONS #428/#429): a ready row this run PROVED spans a non-positive close
    # inside the run's own window-plus-lead-in span (the exact shape AC-1's universe-side test
    # excludes a name for), so it is skipped rather than reused, matching member_dataset_lead_in_skips'
    # own idiom for the bad-close test instead of the lead-in one.
    "member_dataset_bad_close_skips": 0,
    # Lane D1: a ready row whose fetched file predates the per-day Factor column (real-share sizing
    # needs it), skipped rather than reused; and the exact-window collision case, re-fetched in place.
    "member_dataset_stale_factor_skips": 0,
    "member_dataset_stale_factor_refetches": 0,
    # Lane UB round 2 (docs/units/UB/reads/read_1.md, UB-R1, SHOULD-FIX): a candidate (ready OR
    # pending/fetching alike) whose own stored end_ts does not reach the run's own window end at
    # all. Before this fold every created row ran to "now", so this gap was latent; once a row can
    # be narrowed to a run's own window_end (this lane's own AC-4), a LATER run with a later window
    # could otherwise reuse (or await) a row that stops short and silently trade on a frame that
    # ends early. Counted BEFORE the lead-in/bad-close checks below, for every candidate alike.
    "member_dataset_reach_skips": 0,
    # Row `3.4e` (DECISIONS #429, lane US): the SAME idiom as member_dataset_reach_skips, mirrored
    # onto the START boundary. A candidate (ready OR pending/fetching alike) whose own stored
    # start_ts is AFTER this run's own lead-in span start is skipped here too: `#429`'s own measured
    # case (ANG, dataset 97) showed `UB` narrowed only the END, on `UB`'s own now-corrected
    # start-side assumption that the canonical floor can never land on a bad bar (AC-3 below
    # corrects that assumption, by name, where it was originally stated). Once a freshly created
    # row can itself be narrowed to START after a bad close (AC-1 below), a
    # LATER run whose own span starts before that narrowed row's own start must never reuse or await
    # it, the same unbounded-growth risk member_dataset_reach_skips already guards on the other end.
    "member_dataset_start_reach_skips": 0,
}


def _member_dataset_covers_lead_in(conn, dataset_id: int, window_start_date: str) -> "bool | None":
    """Lane RM survey root cause 1: whether a CANDIDATE member dataset's own FILE actually holds at
    least _MEMBER_DATASET_MIN_LEAD_IN_BARS bars strictly before window_start_date, the exact count
    the worker will see when it opens THIS file, never the raw daily store
    stage_runner._check_warmups reads. A dataset can be 'ready' and still be narrower than the raw
    store its own symbol lives in (one created earlier for an explicit, shorter window by the plain
    POST /datasets route, say), and R-SKIP's old reuse rule picked it purely by symbol, ignoring
    this: the nine-scenario crash (InsufficientLeadInError, UndefinedVolatilityError) the survey
    traced to this function's caller.

    Returns True when the file demonstrably covers the lead-in (reuse is safe), False when it
    demonstrably falls short (reuse would repeat the crash), or None when the row is not yet ready
    or its file cannot be opened on this host: an unreachable container-relative path is a known,
    already-accepted architecture gap (routes/research.py's own _dataset_bar_count_for_lead_in_precheck
    precedent), never treated as "adequate" or "short" by this function; the caller's own fallthrough
    to creating a fresh dataset is what handles that case honestly, rather than this function
    guessing either way.
    """
    row = conn.execute(
        "SELECT status, file_path FROM research_datasets WHERE id = ?;", (dataset_id,),
    ).fetchone()
    if row is None or row["status"] != "ready" or row["file_path"] is None:
        return None  # not yet fetched: nothing on disk to count yet
    from research_lab.worker.pathmap import to_host_path  # lazy: mirrors paperdb.py's own precedent (dashboard/backend/CLAUDE.md)
    data_root = Path(os.environ.get("RESEARCH_DATA_ROOT", str(PROJECT_ROOT / "research_lab" / "data")))
    host_path = to_host_path(row["file_path"], data_root)
    try:
        import pandas as pd  # lazy: this module otherwise carries no pandas dependency, matching stage_runner's own lazy store read
        # A fetched member dataset's own file carries its dates as a DatetimeIndex named 'date'
        # (OHLCV columns 'Open'/'High'/'Low'/'Close'/'Volume'), NOT as a plain 'date' COLUMN the
        # way the raw daily store stage_runner._bar_dates_for_ticker reads does; the two are
        # written by different code paths and do not share a schema. Reading the index costs the
        # same as reading one column (pandas still loads the whole row group either way for a
        # parquet file with no column-pruned index), so no columns= filter narrows this further.
        frame = pd.read_parquet(host_path)
    except (FileNotFoundError, OSError):
        return None  # unreachable on this host: a known gap, not evidence either way (see docstring)
    bars_before = int((frame.index.strftime("%Y-%m-%d") < window_start_date).sum())
    return bars_before >= _MEMBER_DATASET_MIN_LEAD_IN_BARS


def _day_after_iso(date_str: str) -> str:
    """Lane UB round 2 (DECISIONS #428/#429, UB-R2): one calendar day after `date_str`
    ('YYYY-MM-DD' in, 'YYYY-MM-DD' out). The run's own window is INCLUSIVE on both ends
    (`run_sweep_entrypoint._clip_frame_to_window`'s own '[window_start, window_end]'), while
    `fetch.py`'s own `fetch_eodhd` clamps to `[start_ts, end_ts)`, end EXCLUSIVE. So a dataset row
    whose own requested end equals window_end_date's own epoch would never include the window's
    OWN LAST BAR at all: fetch_eodhd would stop one day short. The fix is to request one day past
    window_end_date instead, which is what every caller below does; this is the ONE place that
    conversion happens, so the create branch and the raw-store check it gates can never compute it
    two different ways and drift apart."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def _member_dataset_spans_bad_close(conn, dataset_id: int, window_start_date: str,
                                     window_end_date: str) -> "bool | None":
    """Row `3.4d` (DECISIONS #428/#429): whether a CANDIDATE member dataset's own FETCHED FILE
    holds a non-positive close inside [window_start_date minus its 278-bar lead-in,
    window_end_date] -- the SAME span `research_lab.universe`'s own AC-1 bad-close test covers on
    the raw-store side, read here from the member's own FETCHED file (mirrors
    `_member_dataset_covers_lead_in`'s own shape immediately above, one level up: same table read,
    same unreachable-file fallthrough). A row proven to span one must never be handed back for
    reuse, because the universe-side exclusion alone does not stop this problem (#429's own
    correction of #428): a stale, wide 'ready' file can still carry a bad close inside the run's
    own span even when today's universe() call would have excluded the ticker had it been asked.

    Returns True (spans one: never reuse), False (demonstrably clean) or None when the row is not
    ready or its file cannot be opened on this host -- the identical never-guess fallthrough
    `_member_dataset_covers_lead_in` already uses, so a row this host cannot verify is handled by
    the SAME caller-side logic that already exists for the lead-in question, not a new one."""
    row = conn.execute(
        "SELECT status, file_path FROM research_datasets WHERE id = ?;", (dataset_id,),
    ).fetchone()
    if row is None or row["status"] != "ready" or row["file_path"] is None:
        return None  # not yet fetched: nothing on disk to inspect yet
    from research_lab.worker.pathmap import to_host_path  # lazy: mirrors _member_dataset_covers_lead_in's own precedent directly above
    data_root = Path(os.environ.get("RESEARCH_DATA_ROOT", str(PROJECT_ROOT / "research_lab" / "data")))
    host_path = to_host_path(row["file_path"], data_root)
    try:
        import pandas as pd  # lazy: this module otherwise carries no pandas dependency (same precedent)
        # The FETCHED dataset's own on-disk shape: a DatetimeIndex named 'date' and OHLCV columns
        # Open/High/Low/Close/Volume (NOT the raw store's seven-column shape), exactly what
        # _member_dataset_covers_lead_in already reads.
        frame = pd.read_parquet(host_path)
    except (FileNotFoundError, OSError):
        return None  # unreachable on this host: a known gap, not evidence either way (see docstring)
    dates = frame.index.strftime("%Y-%m-%d")  # plain ISO strings, the same comparison basis the lead-in check above uses
    before_dates = sorted(dates[dates < window_start_date])  # ascending; a positional index from the end finds the lead-in span's own start date
    if len(before_dates) >= _MEMBER_DATASET_MIN_LEAD_IN_BARS:
        span_start = before_dates[len(before_dates) - _MEMBER_DATASET_MIN_LEAD_IN_BARS]  # exactly the fixed lead-in bar count strictly before window_start_date
    elif before_dates:
        span_start = before_dates[0]  # fewer bars exist than the lead-in asks for: the whole available pre-window history is the span, never a crash
    else:
        span_start = window_start_date  # no bars at all before the window: the span collapses to [window_start_date, window_end_date]
    in_span = (dates >= span_start) & (dates <= window_end_date)  # the same closed, inclusive-both-ends span AC-1's universe-side test uses
    # Lane D1: every data check flag, not only a non-positive close. The fetched file's Close IS the
    # adjusted close, so it stands in for both close and adjusted_close here (a ceiling row reads
    # 999999.9999 in Close; a raw non-positive close was already refused at fetch).
    from research_lab.universe import data_check  # lazy: same precedent as the pathmap import above
    span = frame.loc[in_span]
    checked = pd.DataFrame({"high": span["High"], "low": span["Low"], "close": span["Close"],
                            "adjusted_close": span["Close"]})
    return bool(data_check.flags_in(checked))


def _member_dataset_file_lacks_factor(conn, dataset_id: int) -> "bool | None":
    """Lane D1: whether a 'ready' eodhd daily member dataset's fetched file predates the per-day
    adjustment factor column `Factor` (fetch.py's fetch_eodhd writes it from lane D1 on; sizing reads it
    to count real shares). True means the file is stale and must not be reused; False means it carries
    the column; None when the row is not ready or the file cannot be opened on this host, the same
    never-guess fallthrough the two checks above use."""
    row = conn.execute(
        "SELECT status, file_path FROM research_datasets WHERE id = ?;", (dataset_id,),
    ).fetchone()
    if row is None or row["status"] != "ready" or row["file_path"] is None:
        return None
    from research_lab.worker.pathmap import to_host_path  # lazy: same precedent as above
    data_root = Path(os.environ.get("RESEARCH_DATA_ROOT", str(PROJECT_ROOT / "research_lab" / "data")))
    host_path = to_host_path(row["file_path"], data_root)
    try:
        import pyarrow.parquet as pq  # lazy; reads the schema only, never the rows
        names = pq.read_schema(host_path).names
    except (FileNotFoundError, OSError):
        return None
    return "Factor" not in names


def _raw_eodhd_narrowed_bounds(ticker: str, window_start_date: str,
                                window_end_date: str) -> "tuple[str, str | None, bool]":
    """Row `3.4e` (DECISIONS #429, lane US): ONE function computing BOTH bounds a freshly created
    member/benchmark dataset's own create branch, and the reuse loop's own reach check, need from
    `ticker`'s RAW eodhd daily file (resolved the SAME way every other reader in this repo resolves
    it, `research_lab.worker.store_root`'s own `EODHD_STORE_ROOT`, 'daily' grain), read ONCE so the
    two bounds can never be computed two different ways and drift apart (the exact risk a second,
    separate function would carry).

    This folds in, UNCHANGED, lane UB round 2's own END test (formerly the standalone
    `_raw_eodhd_has_bad_close_after`): whether a non-positive close exists on or after the day
    AFTER window_end_date -- the bars a narrowed fetch's own `[start, end)` will never request once
    narrowed to `_day_after_iso(window_end_date)`. AC-3's own `fetch.py` refusal fires on anything
    INSIDE the window actually requested, so a future bad bar like AEL's own 2024 rows is simply
    never looked at once the fetch stops short of it.

    NEW here (the correction of `#429`'s own measured gap, lane UB: `#429`'s own comment argued
    "the floor always predates any bad close this fix targets", which `#429`'s own CN/ANG measurement
    disproved): the SAME question on the START side. `span_start_date` is the run's own lead-in span
    start -- window_start_date minus `_MEMBER_DATASET_MIN_LEAD_IN_BARS` bars of this ticker's OWN
    actual trading history strictly before window_start_date, never calendar days, the identical
    idiom `_member_dataset_spans_bad_close` above and `research_lab.universe._ticker_coverage` both
    already use, so the member side and the universe side can never define "the span" two different
    ways. `narrowed_start_date` is None when no non-positive close exists strictly before
    span_start_date (the canonical floor stays exactly as before this fold, AC-4's clean-name case);
    otherwise it is the day after the LATEST such close, capped (by `min`) at span_start_date itself
    so a freshly created row's own start never lands LATER than the lead-in it must cover.

    Returns (span_start_date, narrowed_start_date, has_bad_close_after). False/None/the plain
    span_start_date, never a crash, when the raw file is absent or empty on this host (a ticker this
    host cannot see here is a fetch-time concern, not this check's)."""
    from research_lab.worker.store_root import resolve_eodhd_store_root  # lazy: mirrors this module's own lazy-import precedent throughout this area
    path = resolve_eodhd_store_root() / "daily" / f"{ticker}.US.parquet"
    if not path.is_file():
        return window_start_date, None, False  # no raw file on this host: nothing to prove either bound from; span_start degrades to window_start_date itself
    import pandas as pd  # lazy: this module otherwise carries no pandas dependency (same precedent)
    from research_lab.universe import data_check  # lane D1: "bad" now means any data check flag, defined once there
    frame = pd.read_parquet(path, columns=list(data_check.DAILY_COLUMNS))
    if frame.empty:
        return window_start_date, None, False
    flagged = pd.Series(False, index=frame.index)
    for mask in data_check.flag_masks(frame).values():
        flagged = flagged | mask
    dates = frame["date"]
    # START half (row 3.4e): the lead-in span start, computed from this ticker's OWN actual bars
    # before window_start_date, the SAME shape _member_dataset_spans_bad_close uses just above.
    before_dates = sorted(dates[dates < window_start_date])  # ascending; a positional index from the end finds the span's own start date
    if len(before_dates) >= _MEMBER_DATASET_MIN_LEAD_IN_BARS:
        span_start_date = before_dates[len(before_dates) - _MEMBER_DATASET_MIN_LEAD_IN_BARS]  # exactly the fixed lead-in bar count strictly before window_start_date
    elif before_dates:
        span_start_date = before_dates[0]  # fewer bars exist than the lead-in asks for: the whole available pre-window history is the span, never a crash
    else:
        span_start_date = window_start_date  # no bars at all before the window: the span collapses to window_start_date itself
    before_span = dates < span_start_date  # strictly before the span's own start: these are the bars a narrowed create would otherwise still request
    bad_before_span = frame.loc[before_span & flagged]
    narrowed_start_date: "str | None" = None
    if not bad_before_span.empty:
        latest_bad_close_date = bad_before_span["date"].max()  # the LATEST offending bar: narrowing past it is enough, no need to skip further history than that
        candidate_start_date = _day_after_iso(latest_bad_close_date)
        narrowed_start_date = min(candidate_start_date, span_start_date)  # never later than the span start itself: the lead-in must still be fully covered
    # END half (lane UB round 2's own test, unchanged):
    narrowed_fetch_end = _day_after_iso(window_end_date)  # the EXACT exclusive end the narrowed create branch below will request
    outside_narrowed_fetch = dates >= narrowed_fetch_end  # bars the narrowed fetch's own [start, narrowed_fetch_end) will never touch
    has_bad_close_after = bool(flagged[outside_narrowed_fetch].any())
    return span_start_date, narrowed_start_date, has_bad_close_after


def _resolve_or_create_member_dataset(conn, ticker: str, now: int, *, window_start_date: "str | None" = None,
                                       window_end_date: "str | None" = None) -> tuple[int, bool]:
    """Resolve a price-universe member ticker to its eodhd daily dataset, creating + queuing a fetch if absent.

    R-SKIP (AD-M5-12): reuse an existing non-failed eodhd price_dataset row for the ticker: a 'ready' one IS
    the pin (no fetch); a 'pending'/'fetching' one is AWAITED (linked; the trigger finalizes when it readies).
    Resolution is by (source='eodhd', dataset_type='price_dataset', timeframe='1d', symbol=ticker, status in
    ready/pending/fetching), ready-first then widest window then most rows then lowest id (a reproducible
    pick), NOT by the exact requested window, so a now-relative create window never defeats reuse.

    Lane RM fold (set 16b, plan amendment 7): "not by the exact requested window" is right for an
    ordinary re-run but was wrong when the picked row is narrower than THIS run's own 278-bar lead-in
    need (the survey's nine C's). When window_start_date is given, a READY candidate is reused only
    once _member_dataset_covers_lead_in confirms it (True), or the file cannot be verified on this
    host (None, the same "never guess" fallthrough that function's own docstring explains); a
    candidate this host can PROVE is short (False) is skipped in favour of the next one, or, if none
    qualifies, a freshly created dataset (below), which always covers the FULL floor-to-now window and
    so covers at least as much history as the raw store stage_runner._check_warmups already confirmed
    is sufficient. window_start_date is None for every caller that has no run window to check yet
    (the pre-existing behaviour, e.g. the benchmark resolution call), which reuses the first ready row
    exactly as before.

    Row `3.4d` fold (DECISIONS #428/#429): `window_end_date` is this same fold's own new keyword.
    When given, EVERY candidate (ready, pending or fetching alike) is first required to REACH this
    run's own window end at all -- a candidate whose own stored end_ts falls short is skipped
    outright, counted as `member_dataset_reach_skips` (round 2's own UB-R1 fix: once a row can be
    narrowed to an earlier run's window_end, below, a later run's own later window must never reuse
    or await a row that stops short of what it needs). A 'ready' candidate that then also passes the
    lead-in check is ALSO required to demonstrably not span a non-positive close inside
    [window_start_date minus the lead-in, window_end_date] (`_member_dataset_spans_bad_close`); a
    row PROVED to span one is skipped, the same shape as the lead-in skip just above.

    Row `3.4e` fold (DECISIONS #429, lane US), CORRECTING this docstring's own former sentence that
    "the floor always predates any bad close this fix targets": that argument is wrong, measured by
    the lead on 2026-10-03 against CN's real chain `cn22`, whose screen stage failed materializing
    ANG (dataset 97) because `ANG.US.parquet` holds non-positive closes from 2000 through 2018 and
    the then-current fix only ever narrowed the END, so a floor-to-now member fetch still requested
    every one of ANG's 2000-2011 bad rows even when the run's own 2014 screen window, plus its
    lead-in, held none of them. So BOTH bounds are now checked, from ONE raw-store read
    (`_raw_eodhd_narrowed_bounds`, which also answers the UB-R2 END question above): when
    window_start_date AND window_end_date are both given, EVERY candidate is also first required to
    START no later than this run's own lead-in span start (window_start_date minus the lead-in, read
    from this ticker's own raw-store bars) -- a candidate whose own stored start_ts falls after that
    span start is skipped outright, counted as `member_dataset_start_reach_skips`, the SAME shape as
    `member_dataset_reach_skips` just above, mirrored onto the other boundary. And when no usable row
    survives and a fresh dataset must be created, the raw store is checked for a bad close STRICTLY
    BEFORE that same span start: if one exists, the created row's requested START is narrowed to the
    day after the LATEST such close (never later than the span start itself, so the lead-in the run
    needs is still fully covered), instead of the canonical floor. The raw store is ALSO checked (as
    before this fold) for a bad close on or after the day after window_end_date (UB-R2's own
    corrected boundary): if one exists, the created row's requested END is narrowed to the day AFTER
    window_end_date (never window_end_date itself: the run's window is inclusive of its own end
    while `fetch_eodhd`'s own `[start, end)` is exclusive, so narrowing to window_end_date itself
    would drop the window's own last bar) instead of the canonical 'now', so the fetch this dispatch
    queues never asks for a window that would land on that future bad bar at all. `window_end_date`
    is None for every caller with no end bound to check yet (both the START and END raw-store checks
    stay off in that case), which behaves exactly as before this fold.

    Only when no usable row exists at all is a new pending dataset created via the #177 path (INSERT
    + an async fetch_dataset job, the create_dataset SQL inlined so it rides this dispatch
    transaction). Returns (dataset_id, is_ready). Runs inside dispatch_sweep's transaction (no nested
    transaction).
    """
    member_source = dataset_sources.price_source_for_ticker(ticker)  # GV2-4a: "synthetic" for a SYN_ code, else "eodhd"
    candidates = conn.execute(
        "SELECT id, status, start_ts, end_ts FROM research_datasets "
        "WHERE source = ? AND dataset_type = ? AND timeframe = ? AND symbol = ? "
        "  AND status IN ('ready', 'pending', 'fetching') "
        "ORDER BY (status = 'ready') DESC, (end_ts - start_ts) DESC, rows DESC, id ASC;",
        (member_source, DATASET_TYPE_PRICE, _PRICE_UNIVERSE_TIMEFRAME, ticker),
    ).fetchall()
    # R1 fix (lane RM round 3, SHOULD-FIX, docs/units/RM/reads/read_1.md), corrected by R1b
    # (round 4, docs/units/RM/reads/read_2.md): the canonical full floor's own START, computed
    # ONCE, lazily, only the first time a candidate's file cannot be opened at all (production has
    # NO dataset files on the backend host today, per the lead's own read; the fact is taken as
    # given here, never re-verified by connecting to production, which round 3's own report
    # flagged as a mistake). R1b's own correction: the END bound is NOT compared. `latest_end_
    # epoch("eodhd", now)` IS `now` itself, so a row created at an EARLIER dispatch's `now` (which
    # every real full-window row is, since dispatch time keeps moving forward) would never match a
    # LATER dispatch's own `now` on end_ts alone -- comparing the full pair, as R1's first fix did,
    # meant a real full-window row was skipped and a fresh one created on every single dispatch,
    # unbounded growth. The lead-in only ever needs bars BEFORE window_start_date, so only the
    # START matters: a row starting at the canonical floor holds everything the store has from
    # that floor onward, regardless of exactly when its own end_ts was stamped.
    full_window_start: "int | None" = None
    required_end_ts: "int | None" = None  # lazily computed once: the epoch a candidate's own end_ts must reach for THIS run's window (UB-R1)
    # Row `3.4e` (DECISIONS #429, lane US): the raw-store read `_raw_eodhd_narrowed_bounds` answers
    # BOTH the start-reach question here and the create branch's own narrowing below, computed at
    # most ONCE per call (lazily, only when both window bounds are given), so the two never drift.
    narrowed_bounds: "tuple[str, str | None, bool] | None" = None
    required_start_ts: "int | None" = None  # lazily computed once: the epoch a candidate's own start_ts must NOT exceed for THIS run's lead-in span
    for row in candidates:                                # R-SKIP: reuse (ready) or await (pending/fetching)
        # UB-R1 (docs/units/UB/reads/read_1.md, SHOULD-FIX): a candidate whose own stored end_ts
        # does not reach this run's own window end at all is skipped here, BEFORE any status
        # branching, for a ready row and a pending/fetching one alike. Before this lane every
        # created row ran to the canonical "now", so this gap was latent; once a row can be
        # narrowed to an EARLIER run's own window_end (this lane's own AC-4), a LATER run with a
        # later window must never reuse it (or keep awaiting it) only to read a frame that stops
        # short of what IT needs.
        if window_end_date is not None:
            if required_end_ts is None:
                required_end_date = _day_after_iso(window_end_date)  # the same exclusive boundary a freshly created row for THIS run would be given
                required_end_ts = int(datetime.strptime(required_end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            if row["end_ts"] is None or row["end_ts"] < required_end_ts:
                MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_reach_skips"] += 1
                continue  # this candidate's own data stops before this run's window end: never reuse or await it
        # Row `3.4e` (DECISIONS #429, lane US): the mirror check on the START boundary, same place
        # in the loop as UB-R1's end check just above, BEFORE any status branching. A candidate
        # whose own stored start_ts is AFTER this run's own lead-in span start cannot hold the full
        # lead-in this run needs, ready or not, so it is never reused or awaited.
        if window_start_date is not None and window_end_date is not None:
            if required_start_ts is None:
                narrowed_bounds = _raw_eodhd_narrowed_bounds(ticker, window_start_date, window_end_date)
                span_start_date = narrowed_bounds[0]
                required_start_ts = int(datetime.strptime(span_start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            if row["start_ts"] is not None and row["start_ts"] > required_start_ts:
                MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_start_reach_skips"] += 1
                continue  # this candidate's own data starts after this run's lead-in span: never reuse or await it
        if row["status"] != "ready":
            return int(row["id"]), False                  # unchanged: await a not-yet-ready row
        if window_start_date is None:
            return int(row["id"]), True                   # no window to check against: the pre-existing behaviour
        covers = _member_dataset_covers_lead_in(conn, int(row["id"]), window_start_date)
        if covers is True:                                # the file itself proves the lead-in: check row 3.4d's own bad-close test before trusting it further
            if window_end_date is not None:
                spans_bad_close = _member_dataset_spans_bad_close(conn, int(row["id"]), window_start_date, window_end_date)
                if spans_bad_close is True:                # PROVED to span a bad close in this run's own span: never reuse, fall through
                    MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_bad_close_skips"] += 1
                    continue
            if _member_dataset_file_lacks_factor(conn, int(row["id"])) is True:  # lane D1: a file from before the Factor column cannot size real shares
                MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_stale_factor_skips"] += 1
                continue
            return int(row["id"]), True                    # clears the lead-in AND (when checkable) carries no bad close in the span: reuse
        if covers is None:
            # The file cannot be opened on this host at all. The OLD rule reused ANY such row
            # blind, narrow or not, which is exactly what still crashes on production (no dataset
            # files exist there to verify, so root cause 1's own fix never actually ran on the
            # host that needs it). Reuse is safe here ONLY when this row's own stored START is the
            # canonical full floor (so it holds everything the store has from that floor onward,
            # and the raw-store lead-in precheck this ticker already passed elsewhere therefore
            # covers this row too); a row starting any later is unverifiable and is skipped
            # instead of trusted. The END is deliberately never compared (see the docstring above).
            if full_window_start is None:
                full_window_start = dataset_sources.earliest_floor_epoch(member_source, _PRICE_UNIVERSE_TIMEFRAME, now)
            if row["start_ts"] == full_window_start:
                return int(row["id"]), True               # starts at the canonical floor: reuse
            MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_unverifiable_narrow_skips"] += 1
            continue
        # else: covers is False -- this ready row is PROVABLY short of the lead-in this run
        # needs; do not repeat the crash by reusing it, fall through to the next candidate, or to
        # a fresh create below.
        MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_lead_in_skips"] += 1
    # No usable dataset -> create a pending one + queue the eodhd fetch (the #177 path, inlined). The window is
    # the canonical eodhd full floor->now (dataset_sources, the same the Data-Library "Full" button uses); the
    # adapter copies the whole local daily file, so the window is nominal — reuse above is by symbol, not window.
    window = dataset_sources.full_window_iso(member_source, _PRICE_UNIVERSE_TIMEFRAME, now)  # ISO {start,end} for the fetch payload
    start_ts = dataset_sources.earliest_floor_epoch(member_source, _PRICE_UNIVERSE_TIMEFRAME, now)  # epoch for the row's start_ts
    end_ts = dataset_sources.latest_end_epoch(member_source, now)                                   # epoch for the row's end_ts
    # Row `3.4d`/`3.4e` (DECISIONS #429): a floor-to-now fetch for this ticker would otherwise also
    # request every bar up to 'now', including any future non-positive close the raw store already
    # holds OUTSIDE this run's own span (AEL's own 2024 rows are exactly this shape against an
    # earlier screening window) -- AND, the `3.4e` correction of the sentence this comment used to
    # carry, every bar back to the canonical floor, including any PAST non-positive close the raw
    # store holds BEFORE this run's own lead-in span start (ANG's own 2000-2011 rows are exactly this
    # shape against the 2014 screen window CN's `cn22` chain measured). AC-3's own fetch.py refusal
    # fires on anything INSIDE the window actually requested, so narrowing BOTH ends never requests
    # either bad bar at all, rather than repairing or skipping it. `narrowed_bounds` was already
    # computed by the reuse loop above whenever both window bounds were given and at least one
    # candidate existed to check; when it was not (no candidates, or window_start_date/window_end_date
    # absent), it is computed here instead, so this ONE raw-store read per call is never duplicated.
    if window_start_date is not None and window_end_date is not None:
        if narrowed_bounds is None:
            narrowed_bounds = _raw_eodhd_narrowed_bounds(ticker, window_start_date, window_end_date)
        _span_start_date, narrowed_start_date, has_bad_close_after = narrowed_bounds
        if narrowed_start_date is not None:
            # row `3.4e`: narrow the START past the LATEST bad close before the lead-in span, capped
            # at the span start itself so the run's own lead-in is still fully covered.
            start_dt = datetime.strptime(narrowed_start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start_ts = int(start_dt.timestamp())
            window = {"start": narrowed_start_date, "end": window["end"]}
        if has_bad_close_after:
            # UB-R2 (unchanged): the row's own requested end is one day PAST window_end_date, never
            # window_end_date itself. The run's window is inclusive of window_end_date, but
            # fetch_eodhd's own `[start_ts, end_ts)` end is exclusive, so a row whose end_ts equalled
            # window_end_date's own epoch would never return the window's own LAST bar at all.
            # _day_after_iso is the one place this conversion happens, shared with
            # _raw_eodhd_narrowed_bounds above so the two can never compute the narrowed boundary two
            # different ways.
            narrowed_end_date = _day_after_iso(window_end_date)
            end_dt = datetime.strptime(narrowed_end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)  # "YYYY-MM-DD" at UTC midnight, the same convention dataset_sources' own epoch helpers use
            end_ts = int(end_dt.timestamp())  # the row's own end_ts, narrowed to the day after the run's window_end
            window = {"start": window["start"], "end": narrowed_end_date}  # the fetch job payload, narrowed the same way
    created = conn.execute(
        "INSERT INTO research_datasets "
        "    (name, source, symbol, timeframe, start_ts, end_ts, dataset_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, symbol, timeframe, start_ts, end_ts) DO NOTHING "
        "RETURNING id;",
        (ticker, member_source, ticker, _PRICE_UNIVERSE_TIMEFRAME, start_ts, end_ts, DATASET_TYPE_PRICE, now),
    ).fetchone()
    if created is None:                                   # the exact-window key already exists (a same-now create) -> reuse it
        existing = conn.execute(
            "SELECT id FROM research_datasets "
            "WHERE source=? AND symbol=? AND timeframe=? AND start_ts=? AND end_ts=?;",
            (member_source, ticker, _PRICE_UNIVERSE_TIMEFRAME, start_ts, end_ts),
        ).fetchone()
        if existing is None:  # the ON CONFLICT proved the row exists under the write lock; its absence is corruption
            raise RuntimeError(
                f"eodhd member {ticker!r} hit a UNIQUE conflict but the conflicting "
                f"({_PRICE_UNIVERSE_TIMEFRAME}, {start_ts}, {end_ts}) row is not found — data integrity violation"
            )
        if _member_dataset_file_lacks_factor(conn, int(existing["id"])) is True:
            # Lane D1: the exact-window row exists but its file predates the Factor column. Re-fetch it
            # in place (back to pending plus a new fetch job) rather than hand back a file the run
            # would refuse; the caller awaits it like any other pending row.
            MEMBER_DATASET_MONITOR_COUNTERS["member_dataset_stale_factor_refetches"] += 1
            conn.execute("UPDATE research_datasets SET status = 'pending' WHERE id = ?;", (int(existing["id"]),))
            conn.execute(
                "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
                "VALUES ('fetch_dataset', ?, ?, 'pending');",
                (json.dumps({"source": member_source, "symbol": ticker, "timeframe": _PRICE_UNIVERSE_TIMEFRAME,
                             "start": window["start"], "end": window["end"],
                             "dataset_id": int(existing["id"])}), now),
            )
        return int(existing["id"]), False
    dataset_id = int(created["id"])
    conn.execute(                                          # the async eodhd fetch (the worker copies the local daily file)
        "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
        "VALUES ('fetch_dataset', ?, ?, 'pending');",
        (json.dumps({"source": member_source, "symbol": ticker, "timeframe": _PRICE_UNIVERSE_TIMEFRAME,
                     "start": window["start"], "end": window["end"], "dataset_id": dataset_id}), now),
    )
    return dataset_id, False


# Night-seven Lane G (`DECISIONS.md` #316): fail-closed refuses every letter but A, so a fixed,
# hand-typed roster of counter keys is exactly the design the ruling rules out -- a letter invented
# later would have no key to increment under, and group F already had to be remembered into this
# dict's old four-key form once. A letter outside A-F can never reach this dict at all: boot itself
# refuses the register first, `_check_parameter_provenance` raising `ProvenanceCheckError` for any
# entry whose Group body does not open with A-F, proven by `pt09` for a planted letter `Q`. What the
# `defaultdict(int)` buys instead is a TOTAL increment: `SWEEP_GUARD_REFUSAL_COUNTERS[letter] += 1`
# (AC-5) is the same unconditional step for every refusable letter alike, so a refusal always stays a
# documented 422 rather than an unmapped crash, `"UNREADABLE"` needs no special-casing at its own
# increment site, and the pre-seeded key list can never drift out of step with the letter set the way
# the old four-key roster did. Pre-seeded with the five letters the register uses today that are now
# all refusable (B, C, D, E, F: D moves from passable to refused by this same ruling), so the dict is
# never empty even before a first refusal, plus "UNREADABLE" for a grid key whose Group body exists
# but does not parse to a letter at all (job 20 "what can break" item 2: refused fail-closed the same
# as any other non-A group, but with no letter name to file its count under). Mirrors the in-memory,
# process-local convention `SIZING_MONITOR_COUNTERS` in `research_lab/strategies/helpers/sizing.py`
# already uses for its own declared failure mode.
# DEFERRED, recorded per the layer law (docs/log/deferred_mechanisation.md row 71): nothing reads
# SWEEP_GUARD_REFUSAL_COUNTERS today, so wiring it into the operations page is out of this build's
# scope. A future ops-wiring unit reads this dict directly from the dashboard backend process
# (unlike sizing.py's counters, which the worker has to report over HTTP, this dict already lives
# in the same process that owns ops.db, so no HTTP hop is needed) and writes its counts into
# docs/ops/ops_monitor_registry.md's rows, one row per counted key.
# Night four's allow-list unit (`#324`) adds three keys: "UNREGISTERED" (a grid key with no
# provenance entry at all that the strategy also never declared), "UNDECLARED" (a grid key with a
# group-A provenance entry, or no entry, that the strategy never declared as a knob) and "NO_GROUP"
# (a provenance entry that exists but carries no readable '### Group' subsection at all, a state a
# clean boot cannot reach today per `_check_parameter_provenance`, but the guard re-reads the file
# fresh on every dispatch, so a post-boot edit to the file can still produce it). Pre-seeded here for
# the same reason the five letters above are: the dict is never empty even before a first refusal of
# that kind, and a monitor reader that lists this dict's keys sees every kind from the start.
SWEEP_GUARD_REFUSAL_COUNTERS: defaultdict[str, int] = defaultdict(
    int, {"B": 0, "C": 0, "D": 0, "E": 0, "F": 0, "UNREADABLE": 0,
          "UNREGISTERED": 0, "UNDECLARED": 0, "NO_GROUP": 0}
)


def _guard_sweep_grid_keys(strategy_id: int, param_grid: dict) -> None:
    """The grid-key half of dispatch_sweep's admission, moved out unchanged (RS1) so check_sweep can run it
    before a data window is drawn: the provenance tier guard and the undeclared-key check, both on a plain
    read of the strategy. Writes nothing. Raises FrozenSweepParameterError, UndeclaredSweepParameterError or ProvenanceCheckError."""
    # Night four's allow-list unit (`#324`, closing `#316` item 4's own named hole): read the
    # strategy's DECLARED sweep knobs before the guard loop runs, so a grid key with no provenance
    # entry can be told apart from a key the strategy itself never declared. get_strategy is the
    # same read-only lookup routes/research.py's chronology gate already uses for this id; it is a
    # PLAIN read (no BEGIN IMMEDIATE), so it never contends with or substitutes for the txn's own
    # strategy read a few lines below, which is what the actual dispatch decision acts on. If the
    # strategy id does not resolve here, or resolves to a soft-deleted row, `declared` is left None
    # and the guard loop below falls back to ITS pre-existing behaviour for every key (the old
    # continue-on-no-entry path); the transaction's own read still raises the real StrategyNotFound
    # / StrategyDeleted for that case, so this pre-read can never invent a wrong error type for
    # either of those two outcomes. `_apply_declared_guard` is also false for a ZERO-parameter
    # strategy on purpose: that mode's own non-empty-grid refusal is the pre-existing bare
    # ValueError a few dozen lines down, and 9.2's own sentence keeps it unchanged by this unit, so
    # the new declared-based refusals below never fire ahead of it.
    _pre_strat = get_strategy(strategy_id)
    declared: set[str] | None = None
    _pre_mode: str | None = None
    if _pre_strat is not None and _pre_strat["status"] != "deleted":
        _pre_tree = ast.parse(_pre_strat["source_code"])
        _pre_mode, _pre_params_decl = _resolve_param_mode(
            _sweep_params_from_tree(_pre_tree), _inline_params_from_tree(_pre_tree),
            _has_sweep_signals_override(_pre_tree),
        )
        declared = {p["name"] for p in _pre_params_decl}
    _apply_declared_guard = declared is not None and _pre_mode in ("vectorized", "inline")

    # Night-seven Lane G (`DECISIONS.md` #316): refuse a grid whose key names any provenance tier
    # OTHER than A, BEFORE any row is written, so a refused sweep never becomes a trial in the ledger
    # (job file PART 4c). This runs even before the grid freeze below, since a frozen key makes the
    # grid invalid regardless of its shape or size. FAIL-CLOSED, not a corrected roster: #316's own
    # reason is that a group invented later is refused without anyone remembering to edit a tuple,
    # and group F already had to be remembered into the old roster once. Group A (a strategy knob,
    # tuned in every sweep) is the ONLY tier a grid IS allowed to name; group D (the search rules) is
    # refused like every other frozen tier now, because #315 calls it "frozen before the first sweep,
    # owing provenance, never optimised", not exempt. PROVENANCE_PATH is read fresh here, exactly
    # like `_check_parameter_provenance`, never a cached copy, so a probe that monkey-patches the
    # module attribute is honoured the same way.
    try:
        guard_provenance_text = PROVENANCE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProvenanceCheckError(
            f"parameter provenance file missing at {PROVENANCE_PATH}; "
            "the frozen-parameter sweep guard cannot check a grid key's tier without it"
        ) from exc
    guard_provenance_bodies = _parse_provenance_bodies(guard_provenance_text)  # {key: {subsection: body}}
    for grid_key in param_grid:  # every key the caller wants to sweep, checked before any row is written
        entry = guard_provenance_bodies.get(grid_key)  # None only when NO entry exists for this key at all
        if entry is None:  # night four's allow-list unit (#324): a strategy's own declared knob still
            # passes with no tier (the pre-existing trap); anything else with no entry at all is now
            # refused UNREGISTERED, closing the hole `#316` item 4 left open on purpose. The check
            # only fires when the declared set is real and the mode admits a real grid (see
            # `_apply_declared_guard` above); an unresolvable strategy id or a zero-param strategy's
            # non-empty grid fall through to their own pre-existing outcomes untouched.
            if _apply_declared_guard and grid_key not in declared:
                SWEEP_GUARD_REFUSAL_COUNTERS["UNREGISTERED"] += 1
                raise UndeclaredSweepParameterError(
                    f"parameter '{grid_key}' has no provenance entry and is not one of strategy "
                    f"{strategy_id}'s declared sweep parameters ({sorted(declared)}); refused "
                    "fail-closed rather than silently never swept by the worker"
                )
            continue
        group_body = entry.get("Group")  # the entry EXISTS; None here means it has no Group subsection
        if group_body is None:  # night four's allow-list unit (#324): a documented entry with no
            # '### Group' subsection is a malformed or in-progress register row, never the same state
            # as "no entry at all" above (the old code's bug: both used to `continue` here). A clean
            # boot cannot reach this today (`_check_parameter_provenance` refuses it), but the guard
            # re-reads the file fresh on every dispatch, so a post-boot edit still can (AC-3).
            SWEEP_GUARD_REFUSAL_COUNTERS["NO_GROUP"] += 1
            raise FrozenSweepParameterError(
                f"parameter '{grid_key}' has a provenance entry at {PROVENANCE_PATH} with no "
                "'### Group' subsection; refused fail-closed until the register names its tier"
            )
        # A letter is read from a body that DOES exist; this is a separate None from the two cases
        # above, and none of the three are ever conflated: this key HAS a Group body, so a letter
        # that fails to parse is a malformed or unrecognised entry, not an absent one, and is refused too.
        frozen_letter = _group_letter_from_provenance_body(group_body)
        if frozen_letter == "A":  # the one tier a grid may name; every other outcome below is refused
            continue
        if frozen_letter is None:  # the Group body exists but does not parse to a letter: refuse, but
            # never call this key "frozen", since there is no real tier it was found to be under (job
            # 20 "what can break" item 2). SWEEP_GUARD_REFUSAL_COUNTERS is a defaultdict: this key
            # always exists, so the increment can never raise KeyError (AC-5).
            SWEEP_GUARD_REFUSAL_COUNTERS["UNREADABLE"] += 1
            raise FrozenSweepParameterError(
                f"parameter '{grid_key}' has a provenance entry whose group letter could not be "
                "read, so it is refused fail-closed"
            )
        # A real, known letter (today: B, C, D, E or F) other than A: refused fail-closed, and named
        # in the message exactly as it was before this unit for the four letters that already refused
        # (AC-6). SWEEP_GUARD_REFUSAL_COUNTERS is a defaultdict: a letter with no pre-seeded key (any
        # group invented after this build) still increments cleanly, never KeyError (AC-5). Message
        # text and its DECISIONS citation are UNCHANGED from before this unit: #316 rules the
        # COMPARISON fail-closed, never asks for new wording on the branch that already refused.
        SWEEP_GUARD_REFUSAL_COUNTERS[frozen_letter] += 1
        raise FrozenSweepParameterError(
            f"parameter '{grid_key}' is in provenance group {frozen_letter} and is frozen: not "
            "sweepable (the tier ruling narrows the POLICY, not the per-combo machinery; see "
            "DECISIONS #304)"
        )

    # Night four's allow-list unit (`#324`), the loop's second half: even a key that passed the
    # per-key loop above because it carries a group-A provenance entry must STILL be one of the
    # strategy's own declared knobs, because the worker's own grid[p.name] read is a LEFT JOIN over
    # the declared params (run_sweep_entrypoint.py). An undeclared key the guard let through above
    # would silently never be swept, never a loud refusal. Only checked when `_apply_declared_guard`
    # is true (a real vectorized/inline grid on a resolved, non-deleted strategy); the zero-param
    # strategy's own ValueError refusal of a non-empty grid stays untouched (9.2's own sentence).
    if _apply_declared_guard:
        undeclared_keys = sorted(k for k in param_grid if k not in declared)
        if undeclared_keys:
            SWEEP_GUARD_REFUSAL_COUNTERS["UNDECLARED"] += 1
            raise UndeclaredSweepParameterError(
                f"parameter(s) {undeclared_keys} are not among strategy {strategy_id}'s declared "
                f"sweep parameters ({sorted(declared)}); the worker only reads declared params, so "
                "an undeclared key would silently never be swept; refused fail-closed"
            )


def _sweep_admission(conn, strategy_id: int, universe_id: int, param_grid: dict,
                     defer_window_refusals: bool = False) -> dict:
    """The in-transaction half of dispatch_sweep's admission, moved out unchanged (RS1) so check_sweep can run it
    on a plain read connection before a data window is drawn. Reads only; every refusal dispatch_sweep raised
    before its first INSERT is raised here, in the same order, with the same words. Returns what the INSERTs
    need: strat, param_mode, params_decl, grid_json, grid_hash, registered_n, single_combo, urow, members, asset_class."""
    strat = conn.execute(
        "SELECT id, source_code, version, name, status FROM research_strategies WHERE id = ?;",
        (strategy_id,),
    ).fetchone()
    if strat is None:
        raise StrategyNotFound(f"strategy {strategy_id} not found")
    if strat["status"] == 'deleted':  # TERM F2: refuse a new run on a soft-deleted strategy
        raise StrategyDeleted(f"strategy {strategy_id} is deleted; cannot dispatch a new sweep")

    # B3.1 / M5 C: the grid contract is driven by the strategy's DECLARED sweep MODE, read off the embedded
    # source by AST (no exec — the OPEN-4 boundary). 'zero' (no params) -> the single-combo path (empty grid
    # required); 'vectorized' (class sweep_params) or 'inline' (self.param) -> a real grid is frozen the same
    # way. _resolve_param_mode raises on an ambiguous/malformed declaration (already refused at save by
    # validate_strategy_source; defense-in-depth here). For 'inline' the params_decl rides the worker payload
    # (the engine has no class sweep_params to read for an inline strategy), threaded at the payload write below.
    _tree = ast.parse(strat["source_code"])
    param_mode, params_decl = _resolve_param_mode(
        _sweep_params_from_tree(_tree), _inline_params_from_tree(_tree), _has_sweep_signals_override(_tree))

    # Lead's read, finding A-R1-SF1 (2026-09-08): the pre-transaction guard above validates
    # `param_grid` against a declared set read BEFORE this transaction opened (get_strategy,
    # a plain, non-locking read). That pre-read is only an EARLY exit now; THIS transaction's
    # own read of research_strategies, a few lines up, is the one the freeze and the worker
    # payload actually act on, so re-deriving the declared set from params_decl (computed just
    # above, from THIS read) and refusing here is what keeps the two decisions in agreement.
    # Without this, a strategy re-uploaded between the pre-read and this BEGIN IMMEDIATE (an
    # editor save and a sweep dispatch are two independent requests) could rename a knob, the
    # stale pre-read would still admit the grid's old name, this transaction would freeze the
    # grid against the NEW source, and the worker's own grid[p.name] left join would silently
    # drop the key -- the exact silent fail-open this lane exists to close, reopened in the
    # window between the two reads. Only for 'vectorized'/'inline' (a real grid is expected);
    # the zero-mode non-empty-grid ValueError below is untouched. BEGIN IMMEDIATE rolls back on
    # this raise, so no row is written, the same as every other refusal in this function.
    if param_mode in ("vectorized", "inline"):
        txn_declared = {p["name"] for p in params_decl}
        txn_undeclared_keys = sorted(k for k in param_grid if k not in txn_declared)
        if txn_undeclared_keys:
            SWEEP_GUARD_REFUSAL_COUNTERS["UNDECLARED"] += 1
            raise UndeclaredSweepParameterError(
                f"parameter(s) {txn_undeclared_keys} are not among strategy {strategy_id}'s "
                f"declared sweep parameters ({sorted(txn_declared)}), re-checked against this "
                "dispatch's own transaction read; the worker only reads declared params, so an "
                "undeclared key would silently never be swept; refused fail-closed"
            )
        missing_keys = sorted(k for k in txn_declared if k not in param_grid)
        if missing_keys:  # RS1: the worker reads every declared parameter, so a missing one fails in the container
            SWEEP_GUARD_REFUSAL_COUNTERS["MISSING"] += 1
            raise SweepParameterMissing(
                f"parameter(s) {missing_keys} are declared by strategy {strategy_id} but absent from the "
                "grid; the worker reads every declared parameter, so the run would fail in its container; refused"
            )

    if param_mode == "zero":
        # Zero declared params -> the single-combo path. The grid MUST be empty (a grid for a no-param
        # strategy is meaningless — fail loud, never silently ignored); the explicit flag relaxes the
        # worker gates, so an empty grid ALONE never does (B3/B4 contract).
        if param_grid:
            raise ValueError(
                f"strategy {strategy_id} declares no sweep_params; param_grid must be empty for a "
                f"zero-parameter universe run (got keys {sorted(param_grid)})"
            )
        grid_json = json.dumps({}, separators=(",", ":"))   # the canonical empty grid "{}"
        grid_hash = hashlib.sha256(grid_json.encode("utf-8")).hexdigest()  # NOT in the declaration fingerprint scope (strategy-scoped already)
        registered_n = 1                                    # one combination (the degenerate single combo)
        single_combo = 1
    else:
        # 'vectorized' or 'inline' -> a real grid is required; the freeze rejects an empty/malformed grid loud (#180(1)).
        grid_json, grid_hash, registered_n = _freeze_sweep_grid(param_grid)
        single_combo = 0

    # The universe must be a price_universe catalog row (research.db). Read on the txn connection so the
    # member read below is inside BEGIN IMMEDIATE (the dispatch_event_run pattern). B3.4: also read the
    # saved test window (B1) so dispatch can thread the epochs into the worker spec (cold-sliced in B4).
    urow = conn.execute(
        "SELECT id, name, window_start_ts, window_end_ts FROM research_datasets "
        "WHERE id = ? AND dataset_type = ?;",
        (universe_id, DATASET_TYPE_PRICE_UNIVERSE),
    ).fetchone()
    if defer_window_refusals and (
            urow is None or urow["window_start_ts"] is None or urow["window_end_ts"] is None):
        # RS1 step 2b: check_sweep leaves a missing universe or a NULL window bound to
        # tracker.request_draw, which refuses and records it (DrawWindowMissing). Returns early.
        return {"strat": strat, "param_mode": param_mode, "params_decl": params_decl,
                "grid_json": grid_json, "grid_hash": grid_hash, "registered_n": registered_n,
                "single_combo": single_combo, "urow": urow, "members": None, "asset_class": None}
    if urow is None:
        raise PriceUniverseNotFound(f"price universe {universe_id} not found")
    members = [r["ticker"] for r in conn.execute(   # the universe's bare-ticker members (sorted, distinct)
        "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker;",
        (universe_id,),
    ).fetchall()]
    if not members:  # no-fallback: an empty universe cannot be swept
        raise PriceUniverseEmptyError(f"price universe {universe_id} ({urow['name']!r}) has no members")

    # B3.2 over-cap admission (AD-UU-5): reject a too-large sweep right after the member read, BEFORE any
    # run/registration/job row is written, with the product + cap (a readable 422). registered_n
    # combinations x len(members) cells is the real materialize size.
    total_cells = registered_n * len(members)
    if total_cells > _SWEEP_SIZE_CAP_CELLS:
        raise SweepGridTooLarge(
            f"universe sweep would register {total_cells:,} cells "
            f"({registered_n:,} combinations x {len(members):,} members), over the cap of "
            f"{_SWEEP_SIZE_CAP_CELLS:,}; reduce the grid or the universe"
        )

    # Cost class for the members: a price universe is all eodhd, which asset_class_of maps to equities. The
    # neutral-key injection + the scope guard mirror dispatch_event_run (a universe is always equities).
    # NOTE: asset_class_of is called with the MEMBER data class ('eodhd','price_dataset'), never the
    # universe row's 'price_universe' type (which asset_class_of has no branch for and would refuse).
    asset_class = asset_class_of("eodhd", DATASET_TYPE_PRICE)  # = equities (UnknownAssetClass is unreachable while members are eodhd-only; a future source without a cost contract would raise it here, mapped to 422 at the route)
    _assert_asset_class_in_scope(  # fail closed if equities is outside the strategy's declared scope, BEFORE any INSERT
        strat["source_code"], strat["name"], asset_class, f"price universe {urow['name']!r}",
    )
    return {
        "strat": strat, "param_mode": param_mode, "params_decl": params_decl, "grid_json": grid_json,
        "grid_hash": grid_hash, "registered_n": registered_n, "single_combo": single_combo,
        "urow": urow, "members": members, "asset_class": asset_class,
    }


def check_sweep(strategy_id: int, universe_id: int, param_grid: dict) -> None:
    """The read-only twin of dispatch_sweep's admission, called before tracker.request_draw so a
    refused sweep burns no data window (RS1). Runs _guard_sweep_grid_keys, then _sweep_admission on a
    plain read connection: it writes nothing and opens no BEGIN IMMEDIATE. Raises exactly what
    dispatch_sweep would raise before its first INSERT, with the same words. Returns None."""
    _guard_sweep_grid_keys(strategy_id, param_grid)
    with get_conn() as conn:
        _sweep_admission(conn, strategy_id, universe_id, param_grid, defer_window_refusals=True)


def dispatch_sweep(strategy_id: int, universe_id: int, param_grid: dict,
                   selection_scope: str = "in_isolation", name: str | None = None,
                   now: int | None = None, kind: str | None = None,
                   walkforward: dict | None = None, pinned_sizing_method: str | None = None,
                   pinned_sizing_spec: str | None = None) -> dict:
    """Dispatch a universe sweep (M5 1C, AD-M5-13): the two-phase register-then-materialize-then-enqueue flow.

    PHASE 1 (this function, atomic): freeze+hash the grid, then in ONE transaction write the run row
    (status='pending'; the materializing state rides the JOB, whose 'materializing' status the poll never
    claims), the frozen research_sweep_registrations row (the gate complete_sweep_chunk reads it), the
    run_sweep job stub (the worker payload minus the member list, which needs materialized data), and one
    research_sweep_members link per resolved member dataset. Resolve-or-create each member's eodhd dataset
    (R-SKIP reuse; create+queue a fetch for the rest). If EVERY member is already 'ready', finalize
    synchronously (the common re-run case); else leave the run materializing — the ready-trigger
    (finalize_materializing_sweeps_for in worker_dataset_result) finalizes it when the last member readies.

    The grid contract depends on the strategy's DECLARED sweep_params (AD-UU-4, B3.1): a strategy that
    declares parameters needs a real param_grid (one value per param; the freeze rejects an empty grid loud —
    the #180(1) contract, the backend cannot synthesize a strategy's defaults). A strategy that declares NO
    sweep_params runs the zero-parameter single-combo path: param_grid must be empty, registered_n=1,
    grid_json="{}", and the explicit single_combo=1 flag (NOT an empty grid) is what relaxes the worker's
    empty-grid gates. Either way the worker writes one cell per member at variation_id=0 per combination.

    Raises:
      StrategyNotFound              -> 404
      StrategyDeleted               -> 409
      PriceUniverseNotFound         -> 404 (not a price_universe catalog row)
      PriceUniverseEmptyError       -> 422 (the universe has no members)
      FrozenSweepParameterError     -> 422 (a grid key's provenance Group is any letter other than A, including one that does not parse, or the entry carries no Group at all; before any row is written)
      UndeclaredSweepParameterError -> 422 (a grid key is neither a provenance-tiered parameter nor one of the strategy's own declared sweep knobs; before any row is written; #324)
      SweepGridTooLarge         -> 422 (combinations x members over the cap; before any row is written)
      ValueError                -> 400 (a malformed param_grid, a grid sent for a zero-param strategy, or a bad selection_scope)
      AssetClassScopeMismatch   -> 422 (the equities member class is outside the strategy's declared scope)
      UnknownAssetClass         -> 422 (a member source with no cost contract; unreachable with the eodhd-only
                                  member source today, but documented because it propagates through this boundary)

    Lane RS1: SweepParameterMissing (a vectorized or inline grid omits a declared parameter) is also
    raised before any row is written; the routes answer it 422. check_sweep runs the same refusals
    without writing, so a route can call it before it draws a data window.

    `walkforward` (lane WF, 6.2b, DECISIONS #392 ruling 4): the three ruled slicing values
    (dashboard.backend.stage_runner reads them via read_walkforward_parameters and passes them
    here), carried straight into the job payload untouched, as its OWN top-level key, entirely
    separate from param_grid's own provenance-tiered guard above (a walk-forward dispatch's grid
    is the strategy's own combos, not the walkforward.* settings, so the guard loop never sees this
    dict at all). None (every non-walkforward dispatch) writes a JSON null, which the sweep
    entrypoint's own spec.get("walkforward") reads as falsy: the ordinary vectorized/inline/zero
    fork.
    """
    now = int(time.time()) if now is None else now
    if (pinned_sizing_method is None) != (pinned_sizing_spec is None):  # P2h: the sizing pin is both keywords or neither
        raise ValueError("pinned_sizing_method and pinned_sizing_spec are given together or not at all")
    if selection_scope not in ("cumulative", "in_isolation"):  # validate the function contract before the txn so a bad value from any DIRECT caller fails fast (the route no longer sends it — F3); never a late CHECK IntegrityError
        raise ValueError(f"selection_scope must be 'cumulative' or 'in_isolation', got {selection_scope!r}")

    # RS1: the refusals that need no write run in two helpers that check_sweep shares, so a route can
    # call them before it draws a data window. Order and words are unchanged.
    _guard_sweep_grid_keys(strategy_id, param_grid)

    all_ready = True   # the fast-path flag: true iff every member resolves to an already-'ready' dataset
    with transaction() as conn:  # BEGIN IMMEDIATE; run + registration + job + member links commit together or roll back together
        adm = _sweep_admission(conn, strategy_id, universe_id, param_grid)
        strat, param_mode, params_decl = adm["strat"], adm["param_mode"], adm["params_decl"]
        grid_json, grid_hash, registered_n = adm["grid_json"], adm["grid_hash"], adm["registered_n"]
        single_combo, urow, members, asset_class = adm["single_combo"], adm["urow"], adm["members"], adm["asset_class"]
        name = name.strip() if name else None
        if not name:
            name = _default_sweep_run_name(strat["name"], urow["name"])
        settings = _live_settings(conn)
        settings = _resolve_strategy_settings(conn, strategy_id, settings)  # per-strategy overrides (the price/event path order)
        _inject_neutral_cost_keys(settings, asset_class)  # so the stored snapshot matches the single-run convention
        snapshot = _canonical_settings_snapshot(settings)

        # research_runs.dataset_sha256 is NOT NULL and a universe sweep has no single parquet (the BN-I idiom).
        # The phase-1 value is a DECLARATION fingerprint (universe + grid + scope); the real member-content
        # universe_sha256 is computed + stored separately once the members materialize (phase 2, R-EXP).
        decl = json.dumps({"universe_id": universe_id, "grid_hash": grid_hash, "selection_scope": selection_scope},
                          sort_keys=True, separators=(",", ":"))
        dataset_sha256 = hashlib.sha256(decl.encode("utf-8")).hexdigest()

        # 1. The run_sweep job stub. status='materializing' (the poll never claims it). payload_json='' for now;
        #    step 5 fills the PARTIAL payload (everything except the member list, which needs materialized data).
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES (?, '', ?, ?) RETURNING id;",  # kind bound from the constant run_nature reads (1.3 single-source)
            (JOB_KIND_RUN_SWEEP, now, _MATERIALIZING_JOB_STATUS),
        ).fetchone()
        job_id = job["id"]

        # 2. The pending run row (dataset_id = universe_id, dataset_sha256 = the declaration fingerprint; BN-I).
        run = conn.execute(
            "INSERT INTO research_runs "
            "    (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "     settings_snapshot, name, created_at, status, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?) RETURNING id;",
            (job_id, strategy_id, strat["version"], universe_id, dataset_sha256, snapshot, name, now, kind),
        ).fetchone()
        run_id = run["id"]

        # 3. The frozen pre-registration (R-PREREG, AD-M5-6) — the opening ledger fact the gate reads, written
        #    in the SAME transaction as the run. registered_cells stays NULL until phase 2 (member count unknown).
        #    B3.1: single_combo (0/1) records the zero-param path on the ledger so the worker reads it from the spec.
        #    M5 Track C (C2 folds 3+4): FREEZE the window + param_mode onto the registration here, in the same atomic
        #    txn. The live window lives on the MUTABLE research_datasets universe row (a later edit must NOT change a
        #    completed run's full-metrics cell re-run), and param_mode is the three-way re-run discriminator; freezing
        #    both copies on the immutable registration is what lets the cell re-run replay the EXACT window+mode it ran
        #    on (the freeze, not the live row).
        #    C-bench (§3): PRE-COMMIT the universe's benchmark here too, in the SAME atomic txn (R-PREREG: the
        #    benchmark is provably chosen BEFORE any cell ran, never swapped after results). M5 universes are
        #    eodhd equities, so the pre-committed pick is the broad-market default (SPY); a sector/crypto
        #    benchmark needs a per-universe exposure CLASSIFIER (a named follow-on). Resolve-or-create its eodhd
        #    dataset (a fetch if absent, exactly like a member) + freeze the CHOICE (ticker + dataset_id) now.
        #    benchmark_sha256 (the content pin) freezes now IF the dataset is already ready, else when it readies
        #    (freeze_benchmark_sha_on_ready, the worker_dataset_result hook). None if the broad-market default is
        #    not cataloged (SPY not yet in the eodhd index) -> the benchmark group renders honest absence.
        # Lane RM fold (set 16b, plan amendment 7), widened by row `3.4d` (DECISIONS #428/#429):
        # the run's own window start AND end, as the same YYYY-MM-DD strings
        # _member_dataset_covers_lead_in / _member_dataset_spans_bad_close compare against, so a
        # reused dataset is only picked when it actually covers this run's own lead-in and never
        # spans a bad close in the run's own span. Computed here, BEFORE the benchmark resolution
        # below, so the benchmark call gets the identical two dates the member loop further down
        # gets -- row `3.4d`'s own text treats the benchmark under the SAME rule a member gets.
        # NULL for an open-ended window (no bound to check against yet); that state is unchanged.
        member_window_start_date = (
            time.strftime("%Y-%m-%d", time.gmtime(urow["window_start_ts"]))
            if urow["window_start_ts"] is not None else None
        )
        member_window_end_date = (
            time.strftime("%Y-%m-%d", time.gmtime(urow["window_end_ts"]))
            if urow["window_end_ts"] is not None else None
        )
        benchmark_ticker = None
        benchmark_dataset_id = None
        benchmark_sha256 = None
        bench_row = get_default_benchmark(conn, _BENCHMARK_BROAD_MARKET_KEY)
        if bench_row is not None:
            benchmark_ticker = bench_row["benchmark_ticker"]
            benchmark_dataset_id, _bench_ready = _resolve_or_create_member_dataset(
                conn, benchmark_ticker, now,
                window_start_date=member_window_start_date, window_end_date=member_window_end_date,
            )
            if _bench_ready:  # already fetched in a prior dispatch -> pin its content now (else the ready hook does)
                _bsha = conn.execute(
                    "SELECT sha256 FROM research_datasets WHERE id = ?;", (benchmark_dataset_id,)).fetchone()
                benchmark_sha256 = _bsha["sha256"] if _bsha else None
        conn.execute(
            "INSERT INTO research_sweep_registrations "
            "    (run_id, registered_grid_hash, registered_n, selection_scope, grid_json, registered_at, single_combo, "
            "     window_start_ts, window_end_ts, param_mode, benchmark_ticker, benchmark_dataset_id, benchmark_sha256) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (run_id, grid_hash, registered_n, selection_scope, grid_json, now, single_combo,
             urow["window_start_ts"], urow["window_end_ts"], param_mode,
             benchmark_ticker, benchmark_dataset_id, benchmark_sha256),
        )

        # 4. Resolve-or-create + link each member dataset (R-SKIP). all_ready stays true only if EVERY member is
        #    already 'ready' (the synchronous fast path); any pending/created member -> a ready-trigger finalizes.
        # member_window_start_date / member_window_end_date are computed above, before the benchmark
        # resolution, so both call sites thread the identical run span (row `3.4d`).
        for ticker in members:
            member_dataset_id, is_ready = _resolve_or_create_member_dataset(
                conn, ticker, now, window_start_date=member_window_start_date,
                window_end_date=member_window_end_date,
            )
            conn.execute(
                "INSERT OR IGNORE INTO research_sweep_members (run_id, dataset_id) VALUES (?, ?);",
                (run_id, member_dataset_id),
            )
            all_ready = all_ready and is_ready

        # 5. Now run_id exists: write the PARTIAL worker payload onto the job (status stays 'materializing').
        #    source_code + the frozen grid are embedded NOW (the AD-M5-6/10 dispatch-time embed-source contract),
        #    so a later strategy edit cannot change what this sweep runs. Phase 2 adds the `universe` member list.
        partial_payload = {
            "run_id": run_id,
            "source_code": strat["source_code"],
            "param_grid": json.loads(grid_json),  # the canonical frozen grid (the worker expands it to combos; "{}" for single_combo)
            "backtest_mode": "independent",       # AD-M5-3 (M5 dispatches independent only; shared_capital is M7-paired)
            "selection_scope": selection_scope,
            "registered_n": registered_n,
            "single_combo": single_combo,         # B3.1: 1 -> the worker runs the zero-param single-run path + relaxes the empty-grid gate
            "param_mode": param_mode,             # M5 C: 'zero' | 'vectorized' | 'inline' (the entrypoint's top-level fork)
            "params_decl": params_decl if param_mode == "inline" else None,  # M5 C: [{name,type}] for inline (no class sweep_params to read); None otherwise
            "size_cap_cells": _SWEEP_SIZE_CAP_CELLS,
            "window_start_ts": urow["window_start_ts"],  # B3.4: the saved test window epochs (NULL = open on that side);
            "window_end_ts": urow["window_end_ts"],      #       the worker cold-slices each member to [start, end] before signals (B4)
            "walkforward": walkforward,  # lane WF (6.2b): the three ruled slicing values, or None for every ordinary sweep
        }
        if pinned_sizing_method is not None:  # P2h: the reserve exam's pin; absent for every other sweep, so their payload is unchanged
            partial_payload["pinned_sizing_method"] = pinned_sizing_method
            partial_payload["pinned_sizing_spec"] = pinned_sizing_spec
        conn.execute(
            "UPDATE research_jobs SET payload_json = ? WHERE id = ?;",
            (json.dumps(partial_payload), job_id),
        )

        # 6. Audit event (the dispatch_run convention; carries the sweep context).
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, 'created', ?);",
            (job_id, now, json.dumps({"kind": "run_sweep", "run_id": run_id,
                                      "universe_id": universe_id, "registered_n": registered_n,
                                      "member_count": len(members)})),
        )
        strategy_version = strat["version"]  # capture inside the txn for the return dict

    # Phase 2 entry (outside the dispatch txn): if every member was already 'ready', finalize synchronously —
    # the common re-run case (no fetch to wait on). Otherwise the run stays materializing and the ready-trigger
    # (finalize_materializing_sweeps_for in worker_dataset_result) enqueues the sweep when the last member
    # readies. _safe_finalize is idempotent on the job status (fast path + a stray trigger never double-enqueue)
    # and never strands the run — a finalization fault fails the sweep cleanly rather than leaving it stuck.
    finalized = _safe_finalize_sweep(run_id, now=now) if all_ready else False

    return {
        "job_id": job_id, "run_id": run_id, "strategy_id": strategy_id,
        "strategy_version": strategy_version, "universe_id": universe_id,
        "member_count": len(members), "registered_n": registered_n,
        "selection_scope": selection_scope, "created_at": now, "name": name, "kind": kind,
        "all_members_ready": all_ready, "finalized": finalized,
    }


# =============================================================
# Lane GV2-7b (DECISIONS #445): generator v2 theory jobs in the research store.
#
# The search of a theory over its stock group (stage screen), its optimise stage and its walk-forward stage are
# ordinary runs: one research_runs row, one registration, one job, with the data-draw gate in front
# (stage_runner.dispatch_theory_stage). All stages of one theory run share ONE strategy row and version (the
# search dispatch makes it) and ONE calendar id. The worker runs the container and relays the outputs through
# the existing sweep routes; the only new route is the one that records a search's trial family.
# =============================================================

class TheoryMemberNotReady(RuntimeError):
    """A stock of a theory run's universe has no ready dataset that covers the run's data (a dispatch refusal, 409).

    A theory run reads each stock's bars from `data_start` to the stage window's end, so a dataset that is not
    ready, has no file or sha256 recorded, or starts later or ends earlier than that, is refused by name; the
    dispatch never fetches or narrows anything on its own."""


class TheoryRunInvalid(ValueError):
    """The arguments of a theory dispatch do not form a run (a missing key, an empty variation list, a bad stage); 422."""


class TrialFamilyAlreadyRecorded(RuntimeError):
    """A search run already has its trial family row (a repeat POST after a restart); carries the existing row's id."""

    def __init__(self, run_id: int, row_id: int) -> None:
        self.run_id = run_id
        self.row_id = row_id
        super().__init__(f"theory-family-already-recorded: run {run_id} already has trial family row {row_id}")


class FamilyUnionPending(RuntimeError):
    """Two or more theories have a trial family on one key and no union covers the newest run of each. Carries the theory names and,
    when the newest run_family_union job for the key failed, that job's id and error (else job_id and error are None)."""

    def __init__(self, theories: list, job_id: "int | None" = None, error: "str | None" = None) -> None:
        self.theories = list(theories)
        self.job_id = job_id
        self.error = error
        msg = (f"family-union-pending: {len(self.theories)} theories ({', '.join(self.theories)}) searched this data "
               f"and their merged family is not computed yet")
        if job_id is not None:
            msg += f"; the last union job {job_id} failed: {error}"
        super().__init__(msg)


class FamilyUnionAlreadyRecorded(RuntimeError):
    """A run_family_union job already has its union row (a repeat POST); carries the existing row's id."""

    def __init__(self, job_id: int, row_id: int) -> None:
        self.job_id = job_id
        self.row_id = row_id
        super().__init__(f"family-union-already-recorded: job {job_id} already has union row {row_id}")


class FamilyUnionJobNotFound(LookupError):
    """The job id of a posted union is not a run_family_union job."""


_THEORY_GROUP_LABEL = re.compile(r"^GROUP_[A-Z0-9_]+$")  # the same pattern as research_lab/theory_stage/spec.py LABEL
_THEORY_SEARCH_KEYS = ("seed", "n_iter", "onc_cap", "optimise_window")  # the search settings a caller must give (a round's size and limits are the theory's [search], #470)
_THEORY_SEARCH_OPTIONAL_KEYS = ("workers", "final")  # the ones a caller may give; the search has its own default for workers; final (GV3-F) is the kept-back stocks and the later window


def _date_to_ts(day: str) -> int:
    """'YYYY-MM-DD' to epoch seconds at 00:00 UTC (the form the window columns use)."""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _ts_to_date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _check_theory_variations(variations) -> None:
    if not isinstance(variations, list) or not variations:
        raise TheoryRunInvalid("variations must be a non-empty list")
    ids = []
    for v in variations:
        if not isinstance(v, dict) or "id" not in v or not isinstance(v.get("picks"), dict):
            raise TheoryRunInvalid(f'each variation needs "id" and a "picks" object, got {v!r}')
        if isinstance(v["id"], bool) or not isinstance(v["id"], int) or v["id"] < 0:
            raise TheoryRunInvalid(f"a variation id must be a whole number from 0, got {v['id']!r}")
        ids.append(v["id"])
    if len(set(ids)) != len(ids):
        raise TheoryRunInvalid(f"variation ids must be distinct, got {ids}")


def _theory_members(conn, universe_id: int, tickers: list, data_start_ts: int, window_end_ts: int) -> list:
    """Each ticker's ready dataset (file path and sha256) that covers data_start to the window's end; raises TheoryMemberNotReady."""
    members = []
    for ticker in tickers:
        source = dataset_sources.price_source_for_ticker(ticker)
        row = conn.execute(
            "SELECT id, file_path, sha256, start_ts, end_ts FROM research_datasets "
            "WHERE source = ? AND dataset_type = ? AND timeframe = ? AND symbol = ? AND status = 'ready' "
            "ORDER BY (end_ts - start_ts) DESC, rows DESC, id ASC LIMIT 1;",
            (source, DATASET_TYPE_PRICE, _PRICE_UNIVERSE_TIMEFRAME, ticker),
        ).fetchone()
        if row is None:
            raise TheoryMemberNotReady(f"stock {ticker!r} of universe {universe_id} has no ready {source} daily dataset")
        if not row["file_path"] or not row["sha256"]:
            raise TheoryMemberNotReady(f"dataset {row['id']} of stock {ticker!r} has no file path or sha256 recorded")
        if row["start_ts"] > data_start_ts or row["end_ts"] < window_end_ts:
            raise TheoryMemberNotReady(
                f"dataset {row['id']} of stock {ticker!r} covers {_ts_to_date(row['start_ts'])} to {_ts_to_date(row['end_ts'])}; "
                f"the run needs {_ts_to_date(data_start_ts)} to {_ts_to_date(window_end_ts)}"
            )
        members.append({"ticker": ticker, "dataset_path": row["file_path"], "sha256": row["sha256"], "_dataset_id": row["id"], "_source": source})
    return members


def check_theory_dispatch(stage: str, universe_id: int, calendar_id: str, group_label: str, data_start: str, costs: dict, *,
                          search: "dict | None" = None, variations: "list | None" = None, cscv_s: "int | None" = None,
                          walkforward: "dict | None" = None, seed: "int | None" = None) -> dict:
    """Every refusal of a theory dispatch that needs no write, as plain reads: the arguments, check 5's frozen S, and the members'
    datasets. stage_runner.dispatch_theory_stage calls it BEFORE request_draw, so a refusal here leaves no granted draw behind
    (dispatch_stage's round-2 finding 1); dispatch_theory_run calls it again for the values it writes. A universe that is
    missing or has a NULL window bound is left to request_draw's own refusal and its durable row, as _check_warmups does.
    Returns {"kind", "grid_json", "registered_n", "single_combo", "cscv_s"}."""
    if stage not in ("screen", "optimise", "walkforward"):
        raise TheoryRunInvalid(f"stage must be screen, optimise or walkforward, got {stage!r}")
    if not isinstance(calendar_id, str) or not calendar_id:
        raise TheoryRunInvalid("calendar_id must be a non-empty string")
    if not isinstance(group_label, str) or not _THEORY_GROUP_LABEL.match(group_label):
        raise TheoryRunInvalid(f"group_label must match ^GROUP_[A-Z0-9_]+$ (the stage container refuses any other: the ticker slot holds a group's name, never a stock's), got {group_label!r}")
    if not isinstance(costs, dict):
        raise TheoryRunInvalid("costs must be an object")
    if costs.get("fees") == "ibkr_fixed":
        raise TheoryRunInvalid('costs.fees "ibkr_fixed" is refused: Alpaca is the broker (DECISIONS #456); use "alpaca"')
    if stage == "screen":
        if search is None or variations is not None:
            raise TheoryRunInvalid("the search (stage screen) takes `search` and no variations")
        missing = [k for k in _THEORY_SEARCH_KEYS if k not in search]
        unknown = [k for k in search if k not in _THEORY_SEARCH_KEYS + _THEORY_SEARCH_OPTIONAL_KEYS]
        if missing or unknown:
            raise TheoryRunInvalid(f"search settings: missing {missing}, unknown {unknown}")
        window = search["optimise_window"]
        if not (isinstance(window, (list, tuple)) and len(window) == 2 and all(isinstance(d, str) for d in window)):
            raise TheoryRunInvalid(f"search optimise_window must be [start, end], two YYYY-MM-DD dates, got {window!r}")
        try:
            end_ts = _date_to_ts(window[1])
        except ValueError as exc:
            raise TheoryRunInvalid(f"search optimise_window must be [start, end], two YYYY-MM-DD dates, got {window!r}") from exc
        cut_ts = reservedb.read_reserve_cut_date_ts()
        if end_ts >= cut_ts:  # the search reads its optimise window and calibrates on it: it may not touch the reserve (#399 item 4)
            raise TheoryRunInvalid(f"the search's optimise_window ends on {window[1]}, on or after the reserve cut {_ts_to_date(cut_ts)} "
                                   f"(DECISIONS #318): non-reserve stages never touch the reserve")
        if "final" in search:  # GV3-F: the final's own shape and the reserve cut; the group and the screen window are checked below, where the universe is read
            final = search["final"]
            if not isinstance(final, dict):
                raise TheoryRunInvalid(f"search final must be an object with kept_back and later_window, got {final!r}")
            missing = [k for k in ("kept_back", "later_window") if k not in final]
            unknown = [k for k in final if k not in ("kept_back", "later_window")]
            if missing or unknown:
                raise TheoryRunInvalid(f"search final: missing {missing}, unknown {unknown}")
            kept = final["kept_back"]
            if not isinstance(kept, list) or not kept or not all(isinstance(t, str) for t in kept) or len(set(kept)) != len(kept):
                raise TheoryRunInvalid(f"search final kept_back must be a non-empty list of distinct tickers, got {kept!r}")
            later = final["later_window"]
            if not (isinstance(later, (list, tuple)) and len(later) == 2):
                raise TheoryRunInvalid(f"search final later_window must be two dates [start, end], got {later!r}")
            try:
                later_start_ts, later_end_ts = _date_to_ts(later[0]), _date_to_ts(later[1])
            except (ValueError, TypeError) as exc:
                raise TheoryRunInvalid(f"search final later_window must be two dates YYYY-MM-DD, got {list(later)!r}") from exc
            if later_end_ts < later_start_ts:
                raise TheoryRunInvalid(f"search final later_window ends before it starts: {later[0]} to {later[1]}")
            if later_start_ts < cut_ts:  # DECISIONS #473: the later years are the newest, after every earlier stage's years
                raise TheoryRunInvalid(f"search final later_window starts on {later[0]}, before the reserve cut {_ts_to_date(cut_ts)} "
                                       f"(DECISIONS #473): the later years come after every earlier stage's years")
        kind = JOB_KIND_RUN_THEORY_SEARCH
        grid_json, registered_n, single_combo = "{}", 1, 1
    else:
        if search is not None:
            raise TheoryRunInvalid(f"stage {stage!r} takes variations, not search settings")
        _check_theory_variations(variations)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TheoryRunInvalid(f"a stage takes the search's seed (a whole number), got {seed!r}")
        if (stage == "walkforward") != (walkforward is not None):
            raise TheoryRunInvalid("the three ruled walk-forward values go with the walkforward stage and only with it")
        if stage == "optimise" and cscv_s is None:
            from dashboard.backend import judgedb  # noqa: PLC0415 - judgedb imports this module; the import is lazy
            with get_conn() as conn:
                frozen = judgedb.read_threshold(conn, 5, "cscv_split_count", judgedb.BATTERY_VERSION)
            if frozen is None:
                raise TheoryRunInvalid(f"check 5's cscv_split_count is not frozen under battery_version {judgedb.BATTERY_VERSION!r}")
            cscv_s = int(frozen["value_numeric"])
        if stage == "walkforward" and cscv_s is not None:
            raise TheoryRunInvalid("cscv_s belongs to the optimise stage")
        kind = JOB_KIND_RUN_THEORY_STAGE
        grid = {str(v["id"]): v["picks"] for v in sorted(variations, key=lambda v: v["id"])}
        grid_json = json.dumps(grid, sort_keys=True, separators=(",", ":"))
        registered_n, single_combo = len(variations), 0
    _date_to_ts(data_start)
    with get_conn() as conn:
        urow = conn.execute(
            "SELECT window_start_ts, window_end_ts FROM research_datasets WHERE id = ? AND dataset_type = ?;",
            (universe_id, DATASET_TYPE_PRICE_UNIVERSE)).fetchone()
        if urow is not None and urow["window_start_ts"] is not None and urow["window_end_ts"] is not None:
            tickers = [r["ticker"] for r in conn.execute(
                "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker;", (universe_id,)).fetchall()]
            if tickers:
                if stage == "screen" and "final" in search:  # GV3-F: the kept-back stocks are in the group but not all of it; the later window starts after the screen window
                    final = search["final"]
                    absent = [t for t in final["kept_back"] if t not in tickers]
                    if absent:
                        raise TheoryRunInvalid(f"search final kept_back names {absent}, not in the group {tickers}")
                    if len(final["kept_back"]) >= len(tickers):
                        raise TheoryRunInvalid(f"search final kept_back is the whole group ({len(tickers)} stocks): the search would have nothing to read")
                    if _date_to_ts(final["later_window"][0]) <= urow["window_end_ts"]:
                        raise TheoryRunInvalid(f"search final later_window starts on {final['later_window'][0]}, on or before the screen window's end "
                                               f"{_ts_to_date(urow['window_end_ts'])}: the later years must come after the search years")
                _theory_members(conn, universe_id, tickers, _date_to_ts(data_start), urow["window_end_ts"])
    return {"kind": kind, "grid_json": grid_json, "registered_n": registered_n, "single_combo": single_combo, "cscv_s": cscv_s}


def theory_earnings_ciks(theory_text: str, universe_id: int) -> "dict | None":
    """Lane EA4 (DECISIONS #480 item 8): the CIK of every member of a universe, when the theory reads an earnings field.

    Returns None when no top-level [operand.*] table has kind "field" and a field in research_lab.theories.form.EARNINGS_FIELDS.
    Otherwise {ticker: cik} for every member in research_price_universe_membership, resolved against events.db's current
    ticker map. Members with no CIK raise TheoryRunInvalid naming every such ticker; a search over a partial set would
    silently give those stocks no earnings."""
    from research_lab.theories.form import EARNINGS_FIELDS  # lazy: mirrors this module's other research_lab imports
    from .events.clients import benzinga_map
    from .events.errors import TickerNotFoundInBridge

    try:
        operands = tomllib.loads(theory_text).get("operand", {})
    except tomllib.TOMLDecodeError as exc:
        raise TheoryRunInvalid(f"the theory text is not readable TOML: {exc!r}") from exc
    if not isinstance(operands, dict) or not any(
            isinstance(t, dict) and t.get("kind") == "field" and t.get("field") in EARNINGS_FIELDS for t in operands.values()):
        return None
    with get_conn() as conn:
        tickers = [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker;", (universe_id,)).fetchall()]
    ciks: dict = {}
    missing = []
    with eventsdb.get_events_conn() as econn:
        for ticker in tickers:
            try:
                ciks[ticker] = benzinga_map.resolve_ticker_to_cik(econn, ticker)
            except TickerNotFoundInBridge:
                missing.append(ticker)
    if missing:
        raise TheoryRunInvalid(f"the theory reads an earnings field, but these members have no CIK in the ticker map: {missing}")
    return ciks


def dispatch_theory_run(stage: str, strategy_id: int, universe_id: int, calendar_id: str, theory_text: str, group_label: str,
                        data_start: str, costs: dict, init_cash: float, *, search: "dict | None" = None,
                        variations: "list | None" = None, cscv_s: "int | None" = None, walkforward: "dict | None" = None, seed: "int | None" = None,
                        earnings: "dict | None" = None, now: "int | None" = None) -> dict:
    """Write one theory run in ONE transaction: the run row, its registration, its job (ready to claim) and its member links.

    `stage` is 'screen' (the search: `search` carries seed, n_iter, onc_cap and optimise_window, and optionally
    workers; a round's size and limits are the theory's [search], DECISIONS #470), 'optimise' (`variations`, and check 5's block count S: `cscv_s`, or the frozen value when
    None) or 'walkforward' (`variations` and `walkforward`, the three ruled values of read_walkforward_parameters).
    The strategy row already exists (stage_runner.dispatch_theory_stage makes it from the theory text). The universe's
    saved window is the stage's window; the members are the universe's stocks, each with its dataset file and sha256,
    and each dataset must cover `data_start` (a date) to the window's end. The draw gate is NOT called here: the
    caller does request_draw before this and link_run after, as dispatch_stage does.

    The registration of an optimise or walk-forward stage freezes the variations (grid_json is {id: picks},
    registered_n their count). The search registers one search (grid_json "{}", registered_n 1): the number of
    strategies it tries is its own result, recorded as its trial family.

    `earnings` (EA4) is {"ciks": {ticker: cik}, "knowledge_to": ts} from theory_earnings_ciks, for a theory that reads an earnings
    field; None writes no earnings block.

    Raises StrategyNotFound, StrategyDeleted, PriceUniverseNotFound, PriceUniverseEmptyError, TheoryMemberNotReady,
    TheoryRunInvalid.
    """
    now = int(time.time()) if now is None else now
    plan = check_theory_dispatch(stage, universe_id, calendar_id, group_label, data_start, costs, search=search,
                                 variations=variations, cscv_s=cscv_s, walkforward=walkforward, seed=seed)
    kind, grid_json, registered_n, single_combo, cscv_s = (
        plan["kind"], plan["grid_json"], plan["registered_n"], plan["single_combo"], plan["cscv_s"])
    grid_hash = hashlib.sha256(grid_json.encode("utf-8")).hexdigest()
    data_start_ts = _date_to_ts(data_start)

    with transaction() as conn:
        strat = conn.execute(
            "SELECT id, version, name, status FROM research_strategies WHERE id = ?;", (strategy_id,)).fetchone()
        if strat is None:
            raise StrategyNotFound(f"strategy {strategy_id} not found")
        if strat["status"] == 'deleted':
            raise StrategyDeleted(f"strategy {strategy_id} is deleted; cannot dispatch a new theory run")
        urow = conn.execute(
            "SELECT id, name, window_start_ts, window_end_ts FROM research_datasets WHERE id = ? AND dataset_type = ?;",
            (universe_id, DATASET_TYPE_PRICE_UNIVERSE)).fetchone()
        if urow is None:
            raise PriceUniverseNotFound(f"price universe {universe_id} not found")
        if urow["window_start_ts"] is None or urow["window_end_ts"] is None:
            raise TheoryRunInvalid(f"price universe {universe_id} has no saved window; a theory run needs both bounds")
        if data_start_ts > urow["window_start_ts"]:
            raise TheoryRunInvalid(f"data_start {data_start} is after the window's start {_ts_to_date(urow['window_start_ts'])}")
        tickers = [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker;", (universe_id,)).fetchall()]
        if not tickers:
            raise PriceUniverseEmptyError(f"price universe {universe_id} ({urow['name']!r}) has no members")
        members = _theory_members(conn, universe_id, tickers, data_start_ts, urow["window_end_ts"])
        sources = {m["_source"] for m in members}
        if len(sources) != 1:
            raise TheoryRunInvalid(f"universe {universe_id} mixes data sources {sorted(sources)}")
        asset_class = asset_class_of(sources.pop(), DATASET_TYPE_PRICE)
        settings = _live_settings(conn)
        settings = _resolve_strategy_settings(conn, strategy_id, settings)
        _inject_neutral_cost_keys(settings, asset_class)
        snapshot = _canonical_settings_snapshot(settings)

        window_start, window_end = _ts_to_date(urow["window_start_ts"]), _ts_to_date(urow["window_end_ts"])
        decl = json.dumps({"kind": kind, "stage": stage, "universe_id": universe_id, "group_label": group_label,
                           "theory_sha256": hashlib.sha256(theory_text.encode("utf-8")).hexdigest(),
                           "members": {m["ticker"]: m["sha256"] for m in members}, "grid_hash": grid_hash,
                           "window": [window_start, window_end], "data_start": data_start},
                          sort_keys=True, separators=(",", ":"))
        dataset_sha256 = hashlib.sha256(decl.encode("utf-8")).hexdigest()
        name = f"{strat['name']} {stage} {group_label}"

        job_id = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) VALUES (?, '', ?, 'pending') RETURNING id;",
            (kind, now)).fetchone()["id"]
        run_id = conn.execute(
            "INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, settings_snapshot, "
            "name, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending') RETURNING id;",
            (job_id, strategy_id, strat["version"], universe_id, dataset_sha256, snapshot, name, now)).fetchone()["id"]
        conn.execute(
            "INSERT INTO research_sweep_registrations (run_id, registered_grid_hash, registered_n, selection_scope, grid_json, "
            "registered_at, single_combo, window_start_ts, window_end_ts, param_mode) "
            "VALUES (?, ?, ?, 'in_isolation', ?, ?, ?, ?, ?, 'theory');",
            (run_id, grid_hash, registered_n, grid_json, now, single_combo, urow["window_start_ts"], urow["window_end_ts"]))
        for m in members:
            conn.execute("INSERT OR IGNORE INTO research_sweep_members (run_id, dataset_id) VALUES (?, ?);", (run_id, m["_dataset_id"]))
        payload = {
            "run_id": run_id, "stage": stage, "strategy_id": strategy_id, "strategy_version": strat["version"],
            "calendar_id": calendar_id, "theory_toml": theory_text, "group_label": group_label,
            "members": [{"ticker": m["ticker"], "dataset_path": m["dataset_path"], "sha256": m["sha256"]} for m in members],
            "data_start": data_start, "window_start": window_start, "window_end": window_end,
            "costs": costs, "init_cash": init_cash,
        }
        if earnings is not None:  # EA4: {"ciks", "knowledge_to"}; the slice starts at 0 so days since a report before the data start are right
            payload["earnings"] = {"ciks": earnings["ciks"], "knowledge_from": 0, "knowledge_to": earnings["knowledge_to"]}
        if stage == "screen":
            payload["screen_window"] = [window_start, window_end]
            payload.update(search)
        else:
            payload["variations"] = variations
            payload["seed"] = seed  # the search's seed: the stage computes a gap bucket the finalist's table lacks with it
            if stage == "optimise":
                payload["cscv_s"] = cscv_s
            else:
                payload["walkforward"] = walkforward
        conn.execute("UPDATE research_jobs SET payload_json = ? WHERE id = ?;", (json.dumps(payload), job_id))
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) VALUES (?, ?, 'created', ?);",
            (job_id, now, json.dumps({"kind": kind, "run_id": run_id, "universe_id": universe_id, "registered_n": registered_n,
                                      "member_count": len(members), "stage": stage})))
        strategy_version = strat["version"]
    return {"job_id": job_id, "run_id": run_id, "strategy_id": strategy_id, "strategy_version": strategy_version,
            "universe_id": universe_id, "member_count": len(members), "registered_n": registered_n, "calendar_id": calendar_id,
            "stage": stage, "kind": kind, "name": name, "created_at": now}


def read_theory_search_payload(run_id: int) -> dict:
    """The job payload of a theory search run: the stage dispatches read the strategy id and version, the calendar id and the
    theory's settings from it. Raises RunNotFound for an unknown run and TheoryRunInvalid when the run is not a theory search."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT j.kind AS kind, j.payload_json AS payload_json FROM research_runs r JOIN research_jobs j ON r.job_id = j.id "
            "WHERE r.id = ?;", (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id} not found")
    if row["kind"] != JOB_KIND_RUN_THEORY_SEARCH:
        raise TheoryRunInvalid(f"run {run_id} is a {row['kind']!r} run, not a theory search")
    return json.loads(row["payload_json"])


_TRIAL_FAMILY_NULLABLE = ("n_in_onc", "k_effective", "onc_cap", "cap_bound", "v", "reason")
_TRIAL_FAMILY_KEYS = ("theory_name", "group_label", "member_set_sha256", "window_start", "window_end", "n_trials", "selected_frequency",
                      "trials_sha256", "returns_sha256") + _TRIAL_FAMILY_NULLABLE


def _theory_search_runs_on_key(conn, member_set_sha256: str, start_ts: int, end_ts: int) -> "dict[str, int]":
    """{theory name: the run id of its newest family row} on the key."""
    rows = conn.execute(
        "SELECT theory_name, run_id FROM research_trial_families WHERE id IN ("
        "  SELECT MAX(id) FROM research_trial_families WHERE member_set_sha256 = ? AND window_start_ts = ? AND window_end_ts = ? "
        "  GROUP BY theory_name);", (member_set_sha256, start_ts, end_ts)).fetchall()
    return {r["theory_name"]: r["run_id"] for r in rows}


def _enqueue_family_union(conn, member_set_sha256: str, start_ts: int, end_ts: int, now: int) -> "int | None":
    """On record_trial_family's own connection: when the key holds two or more theories, insert one pending run_family_union job
    over the newest run of each theory, unless a pending or picked_up job already has the same runs. Returns the job id or None."""
    newest = _theory_search_runs_on_key(conn, member_set_sha256, start_ts, end_ts)
    if len(newest) < 2:
        return None
    runs = sorted(newest.values())
    for job in conn.execute("SELECT payload_json FROM research_jobs WHERE kind = ? AND status IN ('pending', 'picked_up');",
                            (JOB_KIND_RUN_FAMILY_UNION,)).fetchall():
        if json.loads(job["payload_json"]).get("runs") == runs:
            return None
    earliest = conn.execute(
        "SELECT j.payload_json AS payload_json FROM research_runs r JOIN research_jobs j ON j.id = r.job_id WHERE r.id = ?;",
        (runs[0],)).fetchone()
    search = json.loads(earliest["payload_json"])
    payload = {"runs": runs, "onc_cap": search["onc_cap"], "seed": search["seed"], "member_set_sha256": member_set_sha256,
               "window_start": _ts_to_date(start_ts), "window_end": _ts_to_date(end_ts)}
    job_id = conn.execute("INSERT INTO research_jobs (kind, payload_json, created_at, status) VALUES (?, ?, ?, 'pending') RETURNING id;",
                          (JOB_KIND_RUN_FAMILY_UNION, json.dumps(payload), now)).fetchone()["id"]
    conn.execute("INSERT INTO research_job_events(job_id, ts, event, payload_json) VALUES (?, ?, 'created', ?);",
                 (job_id, now, json.dumps({"kind": JOB_KIND_RUN_FAMILY_UNION, "runs": runs})))
    return job_id


def record_trial_family(run_id: int, family: dict, now: "int | None" = None) -> dict:
    """Append the trial family one theory search made (the body is the search's family.json). Returns {"id", "run_id"}.

    One transaction does the checks and the insert: the run must be a theory search (RunNotFound, TheoryRunInvalid); a run that
    already has a row is TrialFamilyAlreadyRecorded (naming that row, so a repeat POST after a restart is harmless). Any theory
    name may add a row on the key (member_set_sha256, window_start_ts, window_end_ts); when the key then holds two or more theory
    names, a pending run_family_union job is enqueued in the same transaction (call 37: the judge counts the merged family, so the
    worker merges the theories' trials). A body whose numbers and reason disagree (the table's CHECKs) raises sqlite3.IntegrityError.
    Every key of family.json that the table holds must be present (nullable ones as null): a missing key is TheoryRunInvalid.
    """
    now = int(time.time()) if now is None else now
    missing = [k for k in _TRIAL_FAMILY_KEYS if k not in family]
    if missing:
        raise TheoryRunInvalid(f"the trial family lacks {missing}")
    start_ts, end_ts = _date_to_ts(family["window_start"]), _date_to_ts(family["window_end"])
    cap_bound = family["cap_bound"]
    with transaction() as conn:
        run = conn.execute(
            "SELECT j.kind AS kind FROM research_runs r JOIN research_jobs j ON r.job_id = j.id WHERE r.id = ?;", (run_id,)).fetchone()
        if run is None:
            raise RunNotFound(f"run {run_id} not found")
        if run["kind"] != JOB_KIND_RUN_THEORY_SEARCH:
            raise TheoryRunInvalid(f"run {run_id} is a {run['kind']!r} run; only a theory search has a trial family")
        existing = conn.execute("SELECT id FROM research_trial_families WHERE run_id = ?;", (run_id,)).fetchone()
        if existing is not None:
            raise TrialFamilyAlreadyRecorded(run_id, existing["id"])
        row_id = conn.execute(
            "INSERT INTO research_trial_families (run_id, theory_name, group_label, member_set_sha256, window_start_ts, window_end_ts, "
            "n_trials, n_in_onc, k_effective, onc_cap, cap_bound, v, selected_frequency, reason, trials_sha256, returns_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id;",
            (run_id, family["theory_name"], family["group_label"], family["member_set_sha256"], start_ts, end_ts,
             family["n_trials"], family["n_in_onc"], family["k_effective"], family["onc_cap"],
             None if cap_bound is None else int(bool(cap_bound)), family["v"], family["selected_frequency"], family["reason"],
             family["trials_sha256"], family["returns_sha256"], now)).fetchone()["id"]
        _enqueue_family_union(conn, family["member_set_sha256"], start_ts, end_ts, now)
    return {"id": row_id, "run_id": run_id}


_UNION_KEYS = ("run_ids", "member_set_sha256", "window_start", "window_end", "n_trials", "n_in_onc", "k_effective", "onc_cap",
               "cap_bound", "v", "reason")


def record_family_union(job_id: int, body: dict, now: "int | None" = None) -> dict:
    """Append the merged family a run_family_union job computed (the body is GV3-FU1's union.json). Returns {"id", "job_id"}.

    One transaction: the job must be a run_family_union job (FamilyUnionJobNotFound); a job that already has a row is
    FamilyUnionAlreadyRecorded (naming it); the body's run ids must be the job's payload runs and its key (member set sha, window
    start and end) the payload's, else TheoryRunInvalid. The table's CHECKs refuse numbers that disagree with the reason
    (sqlite3.IntegrityError)."""
    now = int(time.time()) if now is None else now
    missing = [k for k in _UNION_KEYS if k not in body]
    if missing:
        raise TheoryRunInvalid(f"the family union lacks {missing}")
    start_ts, end_ts = _date_to_ts(body["window_start"]), _date_to_ts(body["window_end"])
    run_ids = sorted(body["run_ids"])
    cap_bound = body["cap_bound"]
    with transaction() as conn:
        job = conn.execute("SELECT kind, payload_json FROM research_jobs WHERE id = ?;", (job_id,)).fetchone()
        if job is None or job["kind"] != JOB_KIND_RUN_FAMILY_UNION:
            raise FamilyUnionJobNotFound(f"job {job_id} is not a run_family_union job")
        existing = conn.execute("SELECT id FROM research_trial_family_unions WHERE job_id = ?;", (job_id,)).fetchone()
        if existing is not None:
            raise FamilyUnionAlreadyRecorded(job_id, existing["id"])
        payload = json.loads(job["payload_json"])
        if run_ids != sorted(payload["runs"]):
            raise TheoryRunInvalid(f"the union covers runs {run_ids}, job {job_id} asked for runs {sorted(payload['runs'])}")
        if (body["member_set_sha256"], start_ts, end_ts) != (
                payload["member_set_sha256"], _date_to_ts(payload["window_start"]), _date_to_ts(payload["window_end"])):
            raise TheoryRunInvalid(f"the union's member set or window differs from job {job_id}'s")
        row_id = conn.execute(
            "INSERT INTO research_trial_family_unions (job_id, member_set_sha256, window_start_ts, window_end_ts, run_ids_json, "
            "n_trials, n_in_onc, k_effective, onc_cap, cap_bound, v, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id;",
            (job_id, body["member_set_sha256"], start_ts, end_ts, json.dumps(run_ids), body["n_trials"], body["n_in_onc"],
             body["k_effective"], body["onc_cap"], None if cap_bound is None else int(bool(cap_bound)), body["v"], body["reason"],
             now)).fetchone()["id"]
    return {"id": row_id, "job_id": job_id}


def family_for(member_set_sha256: str, window_start_ts: int, window_end_ts: int) -> "dict | None":
    """The trial family the judge counts on the key (the data searches drew on), or None when no search was recorded on it.

    One theory on the key: its newest row, as recorded. Two or more: the newest union row whose run ids hold the newest run of
    every theory on the key, with the same keys the judge reads (n_trials, k_effective, v, reason, ...), "union_id" in place of
    "id" and "union": True. A union that misses a newer theory never stands for the key: none covering raises FamilyUnionPending
    (naming the theories, and the newest union job's id and error when it failed)."""
    with get_conn() as conn:
        newest = _theory_search_runs_on_key(conn, member_set_sha256, window_start_ts, window_end_ts)
        if not newest:
            return None
        if len(newest) == 1:
            row = conn.execute(
                "SELECT * FROM research_trial_families WHERE member_set_sha256 = ? AND window_start_ts = ? AND window_end_ts = ? "
                "ORDER BY id DESC LIMIT 1;", (member_set_sha256, window_start_ts, window_end_ts)).fetchone()
            return dict(row)
        need = set(newest.values())
        for row in conn.execute(
                "SELECT * FROM research_trial_family_unions WHERE member_set_sha256 = ? AND window_start_ts = ? AND window_end_ts = ? "
                "ORDER BY id DESC;", (member_set_sha256, window_start_ts, window_end_ts)).fetchall():
            if need <= set(json.loads(row["run_ids_json"])):
                union = dict(row)
                union["union_id"] = union.pop("id")
                union["union"] = True
                return union
        start_day, end_day = _ts_to_date(window_start_ts), _ts_to_date(window_end_ts)
        latest = None
        for job in conn.execute("SELECT id, status, error, payload_json FROM research_jobs WHERE kind = ? ORDER BY id DESC;",
                                (JOB_KIND_RUN_FAMILY_UNION,)).fetchall():
            p = json.loads(job["payload_json"])
            if (p.get("member_set_sha256"), p.get("window_start"), p.get("window_end")) == (member_set_sha256, start_day, end_day):
                latest = job
                break
    failed = latest is not None and latest["status"] == "failed"
    raise FamilyUnionPending(sorted(newest), latest["id"] if failed else None, latest["error"] if failed else None)


def _finalize_materializing_sweep(run_id: int, now: int | None = None) -> bool:
    """Assemble the six-field member list + the universe_sha256 and enqueue the run_sweep job — idempotent (AD-M5-13).

    The shared phase-2 step, called either synchronously by dispatch_sweep (fast path: all members already
    ready) or by the ready-trigger when the last member reaches 'ready'. In ONE transaction: re-check the job
    is still 'materializing' (else a no-op — another trigger already finalized; never a double-enqueue); read
    the run's members; if ANY member is NOT 'ready', return False (not all ready yet — a later trigger
    finalizes); else assemble the member list (R-COST per-member fees/slippage from the frozen
    settings_snapshot), compute universe_sha256, write the full worker payload + registered_cells (the real
    cell denominator), and flip the job to 'pending' so the poll hands it to the worker. Returns True iff it
    enqueued the sweep this call. The eodhd files are worker-local and the worker has no DB, so this assemble +
    enqueue is backend-driven, not a worker job (DECISIONS #20).
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        run = conn.execute(
            "SELECT r.id, r.job_id, r.settings_snapshot, j.status AS job_status, j.payload_json AS payload_json "
            "FROM research_runs r JOIN research_jobs j ON r.job_id = j.id WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RunNotFound(f"run {run_id} not found")
        if run["job_status"] != _MATERIALIZING_JOB_STATUS:  # already finalized / failed / claimed -> no-op (idempotent, no double-enqueue)
            return False
        members = conn.execute(
            "SELECT d.id, d.status, d.symbol, d.source, d.sha256, d.file_path "
            "FROM research_sweep_members m JOIN research_datasets d ON m.dataset_id = d.id "
            "WHERE m.run_id = ? ORDER BY d.symbol;",
            (run_id,),
        ).fetchall()
        if not members:  # a materializing sweep always has member links (dispatch wrote them) — a fail-loud invariant
            raise ValueError(f"materializing sweep run {run_id} has no member links")
        if any(m["status"] != "ready" for m in members):  # not all materialized yet -> a later ready-trigger finalizes
            return False
        # Every member is ready: assemble the six-field member list the worker reads. R-COST per-member
        # fees/slippage come from the frozen settings_snapshot, by each member's OWN asset class (eodhd ->
        # equities; per-member, so a future mixed universe is a new consumer, not a rebuild — AD-M5-13).
        settings = json.loads(run["settings_snapshot"])  # the frozen list[dict] of {scope,key,value,value_type}
        universe = []
        for m in members:
            member_class = asset_class_of(m["source"], DATASET_TYPE_PRICE)  # eodhd -> equities
            universe.append({
                "dataset_path": m["file_path"],     # container-relative parquet path (the worker mounts it)
                "symbol": m["symbol"],              # the BARE ticker (AD-M5-14)
                "asset_class": member_class,
                "fees": _dict_setting_value(settings, "fees", f"{member_class}.default"),        # the class fee number
                "slippage": _dict_setting_value(settings, "slippage", f"{member_class}.default"),  # the class slippage number
                "dataset_sha256": m["sha256"],      # the member's content identity (the count scope)
            })
        universe_sha256 = _sweep_universe_sha256([m["sha256"] for m in members])  # R-EXP universe identity
        reg = conn.execute(
            "SELECT registered_n FROM research_sweep_registrations WHERE run_id = ?;", (run_id,),
        ).fetchone()
        if reg is None:  # a materializing sweep ALWAYS has a registration (dispatch wrote both in one txn, R-PREREG) — its absence is corruption, fail loud (not a silent NULL denominator)
            raise ValueError(
                f"materializing sweep run {run_id} has no registration row; dispatch_sweep must write the "
                f"registration and the run in the same transaction (R-PREREG, AD-M5-6)"
            )
        registered_cells = reg["registered_n"] * len(members)  # the real CELL denominator (combinations x members)
        # The full worker payload = the frozen partial payload + the materialized member list.
        payload = json.loads(run["payload_json"])
        payload["universe"] = universe
        # B4 (P-2): the zero-param single-run path runs strat.run(data, settings), so the worker needs the
        # frozen run-level settings list. Thread the SAME settings_snapshot (already parsed above for the
        # per-member costs) into the payload — it is the {scope,key,value,value_type} shape the entrypoint
        # rebuilds SettingRow from. The wide parameter-sweep path ignores it (it reads costs per-member).
        payload["settings"] = settings
        conn.execute(  # record the universe identity on the run (R-EXP)
            "UPDATE research_runs SET universe_sha256 = ? WHERE id = ?;",
            (universe_sha256, run_id),
        )
        conn.execute(  # the progress-denominator fix (§27): the bar reads cells/cells, not combinations/cells
            "UPDATE research_sweep_registrations SET registered_cells = ? WHERE run_id = ?;",
            (registered_cells, run_id),
        )
        conn.execute(  # flip the job to claimable: the poll now hands it to the worker
            "UPDATE research_jobs SET payload_json = ?, status = 'pending' WHERE id = ?;",
            (json.dumps(payload), run["job_id"]),
        )
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, 'enqueued', ?);",
            (run["job_id"], now, json.dumps({"kind": "run_sweep", "run_id": run_id,
                                             "member_count": len(members),
                                             "universe_sha256": universe_sha256})),
        )
    return True


def _fail_sweep_run(run_id: int, error: str, now: int | None = None) -> None:
    """Fail ONE materializing sweep (its run + its job) — the finalization-error recovery, so a sweep is never
    stranded in 'materializing'. Idempotent: only a still-'materializing' job is failed (an already
    finalized/failed/claimed sweep is left untouched), so a stray double-call is a no-op.
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        row = conn.execute(
            "SELECT r.job_id AS job_id, j.status AS job_status "
            "FROM research_runs r JOIN research_jobs j ON r.job_id = j.id WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
        if row is None or row["job_status"] != _MATERIALIZING_JOB_STATUS:  # gone, or already past materializing -> no-op
            return
        conn.execute("UPDATE research_runs SET status='failed', error=?, finished_at=? WHERE id=?;",
                     (error, now, run_id))
        conn.execute("UPDATE research_jobs SET status='failed', error=?, finished_at=? WHERE id=?;",
                     (error, now, row["job_id"]))


def _safe_finalize_sweep(run_id: int, now: int | None = None) -> bool:
    """Finalize a materializing sweep WITHOUT ever stranding it. _finalize_materializing_sweep can raise on a
    finalization fault (a corrupt registration, a missing cost key in the frozen snapshot, a DB error). If it
    did, the sweep would otherwise be stuck in 'materializing' forever AND — when called from the ready-callback
    — would 500 the dataset-ready POST, which marks the (already-successful) fetch job failed. So catch the
    KNOWN finalization faults (not a catch-all), fail the sweep explicitly (recording the error), and return
    False — the dataset stays 'ready', the sweep leaves 'materializing', nothing is stranded. Used by BOTH the
    dispatch fast path and the ready-trigger. (`sqlite3.Error` is the documented DB-fault base — Operational +
    Integrity — a single named type, deliberately, not a bare except.)
    """
    try:
        return _finalize_materializing_sweep(run_id, now=now)  # the transaction rolls back on any raise (no partial finalize)
    except (RunNotFound, ValueError, KeyError, sqlite3.Error) as exc:  # the known finalization faults
        log.error("sweep finalization failed; failing the sweep so it is not stranded in 'materializing'",
                  extra={"run_id": run_id, "error": repr(exc)})
        try:  # the recovery write itself can hit a DB-layer fault; never let it re-raise into the caller (the ready-callback)
            _fail_sweep_run(run_id, f"sweep finalization failed: {exc!r}", now=now)
        except sqlite3.Error as fail_exc:  # a DB completely unreachable is infra, not a code bug — log, do not 500 the dataset-ready POST
            log.error("could not record the sweep finalization failure; the sweep may remain 'materializing'",
                      extra={"run_id": run_id, "error": repr(fail_exc)})
        return False


def finalize_materializing_sweeps_for(dataset_id: int, now: int | None = None) -> list[int]:
    """Ready-trigger (AD-M5-13): a dataset just reached 'ready' — finalize every materializing sweep it completes.

    Called by worker_dataset_result AFTER the ready UPDATE commits, outside that transaction (the broadcast
    pattern). Finds every materializing-sweep run that has this dataset as a member (the by-dataset index makes
    this O(matches), not O(table)) and calls the shared idempotent _finalize_materializing_sweep for each —
    which enqueues the sweep only when ALL its members are ready (so a multi-member sweep finalizes exactly
    once, when its LAST member readies). Returns the run ids enqueued this call (for the caller's log).
    """
    now = int(time.time()) if now is None else now
    with get_conn() as conn:  # read-only: find candidate runs (the finalize opens its own write transaction)
        rows = conn.execute(
            "SELECT m.run_id FROM research_sweep_members m "
            "JOIN research_runs r ON m.run_id = r.id "
            "JOIN research_jobs j ON r.job_id = j.id "
            "WHERE m.dataset_id = ? AND j.status = ?;",
            (dataset_id, _MATERIALIZING_JOB_STATUS),
        ).fetchall()
    enqueued = []
    for row in rows:
        if _safe_finalize_sweep(row["run_id"], now=now):  # idempotent + never strands; True iff this call enqueued the sweep
            enqueued.append(row["run_id"])
    return enqueued


def fail_materializing_sweeps_for(dataset_id: int, error: str, now: int | None = None) -> list[int]:
    """Failure-trigger (AD-M5-13 partial-materialize-failure, no-fallback): a member dataset failed -> fail its sweeps.

    Called by worker_dataset_failed after the failed UPDATE commits. Flips every materializing sweep that has
    this dataset as a member to status='failed' (recording the failing ticker), and fails its job too —
    enqueuing NOTHING. Never a silent subset (the no-fallback rule at the universe grain). Returns the failed
    run ids. Skips a sweep whose job is no longer 'materializing' (already finalized/failed).
    """
    now = int(time.time()) if now is None else now
    failed = []
    with transaction() as conn:
        rows = conn.execute(
            "SELECT m.run_id AS run_id, r.job_id AS job_id, d.symbol AS symbol "
            "FROM research_sweep_members m "
            "JOIN research_runs r ON m.run_id = r.id "
            "JOIN research_jobs j ON r.job_id = j.id "
            "JOIN research_datasets d ON m.dataset_id = d.id "
            "WHERE m.dataset_id = ? AND j.status = ?;",
            (dataset_id, _MATERIALIZING_JOB_STATUS),
        ).fetchall()
        for row in rows:
            msg = f"sweep member {row['symbol']!r} (dataset {dataset_id}) failed to materialize: {error}"
            conn.execute(
                "UPDATE research_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?;",
                (msg, now, row["run_id"]),
            )
            conn.execute(
                "UPDATE research_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?;",
                (msg, now, row["job_id"]),
            )
            failed.append(row["run_id"])
    return failed


def run_nature(run_id: int) -> str:
    """Classify a run as 'sweep' | 'event' | 'price' by its OWN nature, not its dataset_type (scale-defect 1.3).

    The single authoritative discriminator that run_is_event/run_is_sweep both read. The rule:
      - SWEEP: the run's job kind is 'run_sweep' (dispatch_sweep writes it) or one of the two generator v2 theory
        kinds 'run_theory_search' and 'run_theory_stage' (dispatch_theory_run writes them). A sweep
        writes per-cell rows + no single-run artifacts + no research_run_stats row, so its results read is
        GET /runs/{id}/cells, never the single-run bundle.
      - EVENT: a 'run_backtest' job whose dispatch built an EVENT payload. The payload's 'event_types' key is
        the positive marker only _event_run_payload sets (a price payload never carries it), and it mirrors
        the worker entrypoint's own branch (run_backtest_entrypoint: is_event = dataset_path is None). An
        event run writes NO price.parquet, so the result gate must use the event mandatory-artifact set.
      - PRICE: any other 'run_backtest' job (a single price run).

    Why not dataset_type: it was a PROXY (a price run -> 'price_dataset', an event run -> 'universe', a sweep
    -> 'price_universe'). The B2 bridge broke the proxy by letting an EVENT run sit on a 'price_universe'
    dataset — which the old predicates mis-read as BOTH not-event (so the result gate demanded a price.parquet
    the event run never writes -> a permanent 409) AND a sweep (so the bundle/cells reads misrouted it). The
    job kind + the payload's event shape are the run's intrinsic nature and are dataset-type-agnostic, so they
    classify every combination correctly: price sweep, event run, and single price run over ANY dataset type.

    Reads research_jobs (kind + payload_json) joined via research_runs.job_id (a NOT NULL UNIQUE FK, so the
    join always resolves for a real run). Raises RunNotFound if the run id is unknown.
    """
    with get_conn() as conn:  # get_conn closes in finally; a bare _connect() leaks until GC
        row = conn.execute(
            "SELECT j.kind AS job_kind, j.payload_json AS payload_json "
            "FROM research_runs r JOIN research_jobs j ON r.job_id = j.id "
            "WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
    if row is None:  # the run id (or its job) is unknown
        raise RunNotFound(f"run {run_id} not found")
    if row["job_kind"] == JOB_KIND_RUN_SWEEP or row["job_kind"] in THEORY_JOB_KINDS:  # dispatch_sweep's kind, and GV2-7b's two theory kinds
        return "sweep"
    # A 'run_backtest' job: event iff the dispatch built an event payload. 'event_types' is the positive
    # marker (only _event_run_payload sets it); an empty/garbled payload classifies as 'price' (the price
    # gate), never falsely event. payload_json is filled in the same dispatch txn as the run row, so it is
    # always the full JSON by the time any caller can read the run.
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if "event_types" in payload:  # the event-run marker, matching the worker's is_event branch
        return "event"
    return "price"


def run_is_event(run_id: int) -> bool:
    """True iff run_id is an event run (a 'run_backtest' job with an event payload), scale-defect 1.3.

    An event run's RunResult has no single base OHLCV frame, so the container writes NO price.parquet
    (run_backtest_entrypoint §3 guard, DECISIONS #23 P2); the result endpoint's mandatory-artifact gate
    must use the event set or it 409s forever on a legitimately-absent file. Now keyed on the run's nature
    (run_nature) — NOT dataset_type, which the B2 bridge made ambiguous for an event-run-over-a-price-universe.
    Raises RunNotFound if the run id is unknown.
    """
    return run_nature(run_id) == "event"


def run_is_sweep(run_id: int) -> bool:
    """True iff run_id is a universe sweep (its job kind is 'run_sweep'), AD-M5-13/15 + scale-defect 1.3.

    A sweep writes per-cell rows (research_run_cells) and NO single-run artifacts and NO research_run_stats
    row, so the single-run bundle read (which opens equity.parquet) must NOT be taken for it — the results
    read is GET /runs/{id}/cells instead. Now keyed on the run's nature (run_nature, i.e. the 'run_sweep' job
    kind) — NOT dataset_type, which mis-classified an event-run-over-a-price-universe as a sweep. Raises
    RunNotFound if the run id is unknown.
    """
    return run_nature(run_id) == "sweep"


def get_sweep_progress(run_id: int) -> dict:
    """The IN-FLIGHT read for a universe sweep (M5 U2 loop surface 2): cell done/total + per-member status.

    Drives the loop's IN FLIGHT panel while the sweep registers, materializes its members from the local
    EODHD store, and runs. Two grains, both honest:
      - done/total are the CELL grain (combinations x members): `done` = committed cells, read from the job's
        progress_json (complete_sweep_chunk writes it in the SAME txn as the cells, so it never drifts);
        absent before the first chunk -> 0. `total` = the frozen registered_cells from the registration
        (NULL on a pre-1C single-member sweep -> registered_n, which already equals its cell count). None
        only if there is no registration yet (a corrupt or mid-dispatch state).
      - members is the frozen member set (research_sweep_members) joined to each dataset's LIVE status, so a
        card reads ready / pending / fetching / error as the eodhd fetch progresses (a member fails the whole
        run, never a silent subset — the materialize contract).
    job_status distinguishes 'materializing' (members still fetching) from a claimed/running job, so the UI
    can label MATERIALIZE vs RUN. Raises RunNotFound for an unknown run; the route guards non-sweep runs.
    """
    with get_conn() as conn:
        run = conn.execute(
            "SELECT r.status AS run_status, j.status AS job_status, j.progress_json AS progress_json "
            "FROM research_runs r JOIN research_jobs j ON r.job_id = j.id WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RunNotFound(f"run {run_id} not found")
        reg = conn.execute(
            "SELECT registered_n, registered_cells FROM research_sweep_registrations WHERE run_id = ?;",
            (run_id,),
        ).fetchone()
        members = conn.execute(  # the frozen member set joined to each dataset's live materialization status
            "SELECT d.symbol AS ticker, d.status AS status "
            "FROM research_sweep_members m JOIN research_datasets d ON m.dataset_id = d.id "
            "WHERE m.run_id = ? ORDER BY d.symbol;",
            (run_id,),
        ).fetchall()
    done = int(json.loads(run["progress_json"]).get("done", 0)) if run["progress_json"] is not None else 0
    total = None  # no registration yet (mid-dispatch / corrupt) -> an honest None, never a fabricated count
    if reg is not None:
        # The CELL denominator. registered_cells (combinations x members) is frozen by the finalize once every
        # member is ready; BEFORE that it is NULL, so derive the same cell count from registered_n (the
        # combination count) x the frozen member count. Using registered_n alone would under-report cells and
        # make the IN-FLIGHT denominator jump (combinations -> cells) at finalize; this keeps it the cell grain
        # from dispatch. The two agree once registered_cells is set (it equals registered_n x member_count).
        total = reg["registered_cells"] if reg["registered_cells"] is not None else reg["registered_n"] * len(members)
    return {
        "run_id": run_id,
        "run_status": run["run_status"],
        "job_status": run["job_status"],
        "done": done,
        "total": total,
        "members": [{"ticker": m["ticker"], "status": m["status"]} for m in members],
    }


def complete_run(run_id: int, artifacts: dict, stats: dict, environment: dict,
                 now: int | None = None, equity_sparkline: list | None = None,
                 annualization: dict | None = None, *, funnel: dict | None = None,
                 variation: dict | None = None) -> dict:
    """Finalize a succeeded run: INSERT research_run_stats + UPDATE the run, atomically.

    Lane DG (`6.1g`): this is the SINGLE RUN's only writer of a `research_run_variations` row
    (always `variation_id = 0`); `complete_sweep_chunk` is the SWEEP's only writer of that table,
    one row per swept combo. `variation` (keyword-only; the dict the single-run entrypoint wrote
    to meta.json's `variation` section: `param_json`, `lookback_bars`,
    `used_degrees_of_freedom`) is None for a strategy that never declared `single_run_params`
    (an event run, a bespoke run, or a strategy predating this lane's contract), which writes NO
    row at all, exactly as a single run behaved before this lane. `param_json` arrives already
    computed by the container's own `canonical_param_json` call; this function never derives it.

    Sets the six artifact path columns, the seven env columns (copied from meta.json: the four
    original container fields plus sizing_method/sizing_spec/cost_model, #376 ruling 4),
    status='succeeded', finished_at. Idempotent on re-delivery via INSERT OR REPLACE for
    the stats row. Returns the data the route needs for the research.run_completed SSE.
    Raises RunNotFound if the run id is unknown.

    Unit-4 D2: `equity_sparkline` (a list of up to 32 numbers, computed by the caller from the
    stored equity.parquet via the artifact-layer helper) is stored as JSON text in the same
    transaction that writes the stats, so the materialized derived value is consistent with the
    run's artifacts by construction. None leaves the column NULL (kept a pure DB function — the
    parquet read lives in the artifact layer, never here).

    Phase-2 Unit 1: `annualization` (the dict the entrypoint wrote to meta.json's `annualization`
    section, keyed by the three `_RUN_ANNUALIZATION_COLUMNS` column names) records the index-derived
    Sharpe annualization this run used. Keyword-only with a None default so the existing call sites
    (route + probe suite) are untouched; None (event runs, legacy callers) leaves the columns NULL.

    Phase-2 Unit 3: `funnel` (keyword-only; the dict the entrypoint wrote to meta.json's `funnel` section,
    keyed by the `_RUN_FUNNEL_COLUMNS` names) is the price run's signal funnel. It is INSERTed into the
    research_run_funnel fact table in this same transaction, so the funnel row lands atomically with the
    stats and the run update. None (event/legacy/bespoke runs) writes NO funnel row — the funnel is a
    price-path concept (the event path's analog is its coverage_* stats).
    """
    now = int(time.time()) if now is None else now
    # Phase-2 Unit 1: fail loud on a PARTIAL annualization dict (a future-regression guard). The
    # entrypoint writes all three keys or the section is absent (None); a dict missing a key must not
    # silently NULL one column. None (event/legacy runs) is fine and writes all three NULL below.
    if annualization is not None and set(annualization) != set(_RUN_ANNUALIZATION_COLUMNS):
        raise ValueError(
            f"annualization dict keys mismatch: expected {sorted(_RUN_ANNUALIZATION_COLUMNS)}, "
            f"got {sorted(annualization)}"
        )
    # Phase-2 Unit 3: same fail-loud guard for the funnel dict — the entrypoint writes all keys or omits
    # the section (None); a dict missing a key must not silently NULL/skip a NOT NULL column.
    if funnel is not None and not _funnel_key_set_ok(funnel):
        raise ValueError(
            f"funnel dict keys mismatch: expected {sorted(_RUN_FUNNEL_COLUMNS)} (or without lead_in_bars), "
            f"got {sorted(funnel)}"
        )
    with transaction() as conn:
        # M5 1B (§7): fetch the run's identity + its dataset symbol/type in one read. The identity columns
        # are denormalized into the single run's research_trials ledger row; the dataset symbol/type give the
        # cell/funnel/trial ticker (the dataset symbol for a price run, '' for an event/universe run — the
        # same dataset_type branch the 1A backfill used at researchdb.py:1674-1676). COALESCE matches the
        # backfill (a legacy dataset row may predate the dataset_type column).
        row = conn.execute(
            "SELECT r.job_id, r.strategy_id, r.strategy_version, r.dataset_id, r.dataset_sha256, "
            "       r.settings_snapshot, COALESCE(d.dataset_type, 'price_dataset') AS dataset_type, "
            "       COALESCE(d.symbol, '') AS symbol "
            "  FROM research_runs r LEFT JOIN research_datasets d ON d.id = r.dataset_id "
            " WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id} not found")
        job_id = row["job_id"]
        ticker = row["symbol"] if row["dataset_type"] == "price_dataset" else ""  # cell/trial ticker (M5 1B §7)

        # research_run_stats: one row per run; OR REPLACE makes a retried callback idempotent. SF-A2 (§7): a
        # single run is a one-cell sweep — the cell written below is the per-cell source of truth and this
        # stats row is its derived run-level rollup (the same metric values, so it cannot drift). The cube
        # read path (grid/heatmap) reads research_run_cells; the existing run-detail read keeps reading here.
        # M5 Track C (C2 §8.2): persist the 2 F6 scalars (max_gross_exposure, turnover) onto the single-run
        # rollup HERE, read from the same stats dict build_run_result populated (the live pf was in hand then).
        # They ride BESIDE _RUN_STATS_COLUMNS (NOT added to it — that tuple is the fixed-8 cell-write contract;
        # a cell never persists F6, it re-extracts from the re-run pf). Nullable: a legacy run predating the
        # columns, or an event run whose stats dict omits them, reads None (honest absence). The single-run
        # full-metrics DERIVE path reads these back (no re-run); stats.get(...) is None-safe for both.
        # Lane LB (5.8e): entries_masked_at_end rides beside the F6 pair the same way, from
        # long_only_signal_result's own additive stats key (portfolio.py): an ordinary run with no
        # last-bar entry writes a real 0, an event/legacy run whose stats dict omits the key reads
        # None (honest absence, never a fabricated zero).
        conn.execute(
            "INSERT OR REPLACE INTO research_run_stats "
            "    (run_id, total_return, sharpe_ratio, max_drawdown, num_trades, "
            "     win_rate, total_fees_paid, total_slippage_cost, profit_factor, "
            "     max_gross_exposure, turnover, entries_masked_at_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (run_id, *(stats.get(c) for c in _RUN_STATS_COLUMNS),
             stats.get("max_gross_exposure"), stats.get("turnover"),
             stats.get("entries_masked_at_end")),
        )

        # research_runs: status + artifact paths + env columns + finished_at + the sparkline.
        # Unit-4 D2: equity_sparkline is set in this same UPDATE (JSON text, or NULL when not
        # supplied) so the materialized derived value lands atomically with the stats.
        set_cols = (
            ["status = 'succeeded'", "finished_at = ?", "error = NULL", "equity_sparkline = ?"]
            + [f"{c} = ?" for c in _RUN_ARTIFACT_COLUMNS]
            + [f"{c} = ?" for c in _RUN_ENV_COLUMNS]
            + [f"{c} = ?" for c in _RUN_ANNUALIZATION_COLUMNS]  # Phase-2 Unit 1: the run's annualization
        )
        params = (
            # An empty sparkline (a zero-row equity artifact) stores NULL, not "[]", so the run
            # stays honestly sparkline-less and remains eligible for the NULL-guarded backfill.
            [now, json.dumps(equity_sparkline) if equity_sparkline else None]
            + [artifacts.get(c) for c in _RUN_ARTIFACT_COLUMNS]
            + [environment.get(c) for c in _RUN_ENV_COLUMNS]
            # Phase-2 Unit 1: None (event/legacy runs) -> all three NULL; the dict is keyed by column name.
            + [(annualization or {}).get(c) for c in _RUN_ANNUALIZATION_COLUMNS]
            + [run_id]
        )
        conn.execute(
            f"UPDATE research_runs SET {', '.join(set_cols)} WHERE id = ?;", params
        )

        # M5 1B (§7, SF-A2): write the run's ONE cube cell (variation_id=0, row_scope='cell', the dataset
        # symbol or '' for an event run). OR REPLACE makes a retried callback idempotent at the cell grain.
        # The metrics ARE this run's stats — the cell is the per-cell source of truth, the stats row above
        # is its rollup. The cell-existence check below gates the (append-only) trial so a re-delivered
        # callback never double-appends a trial (the same exactly-once honesty the sweep path keeps).
        cell_existed = conn.execute(
            "SELECT 1 FROM research_run_cells WHERE run_id = ? AND variation_id = 0 AND ticker = ? AND row_scope = 'cell';",
            (run_id, ticker),
        ).fetchone() is not None
        conn.execute(
            "INSERT OR REPLACE INTO research_run_cells "
            "    (run_id, variation_id, ticker, row_scope, " + ", ".join(_RUN_STATS_COLUMNS) + ") "
            "VALUES (?, 0, ?, 'cell', " + ", ".join(["?"] * len(_RUN_STATS_COLUMNS)) + ");",
            (run_id, ticker, *(stats.get(c) for c in _RUN_STATS_COLUMNS)),
        )

        # M5 1B (§7, R-LEDGER): append the single run's ONE trial — making every new single run a counted
        # draw in the program-wide ledger (the backfill seeded pre-M5 runs; this keeps it growing). trial_kind
        # 'single', variation_id 0, REAL hashes (never the legacy 'LEGACY'/'FULL-SAMPLE' sentinels, §6.4):
        # param_config_hash over the run's canonical settings snapshot, split_id over the full-sample split
        # definition. selection_scope 'cumulative' (a single run is one cumulative draw). Gated on the cell
        # NOT already existing, so a retried callback appends exactly once (research_trials has no UNIQUE).
        if not cell_existed:
            conn.execute(
                "INSERT INTO research_trials "
                "    (run_id, variation_id, ticker, strategy_id, strategy_version, dataset_id, dataset_sha256, "
                "     split_id, param_config_hash, sharpe, trial_kind, selection_scope, created_at) "
                "VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 'single', 'cumulative', ?);",
                (run_id, ticker, row["strategy_id"], row["strategy_version"], row["dataset_id"],
                 row["dataset_sha256"], _full_sample_split_id(row["dataset_sha256"]),
                 _param_config_hash(row["settings_snapshot"]), stats.get("sharpe_ratio"), now),
            )

        # Phase-2 Unit 3 + M5 1B (§7, the SF1 regression repair): the signal-funnel fact row, atomic with the
        # stats/run/cell write. Only a price run carries a funnel; event/legacy/bespoke runs pass None and get
        # no row. The INSERT now supplies the 1A-widened grain columns (variation_id=0, the dataset ticker,
        # row_scope='cell') — WITHOUT which it raised `NOT NULL constraint failed: research_run_funnel.variation_id`
        # for every new price run finalized since 1A (the live latent regression, OPEN.md 2026-06-19). OR
        # REPLACE keeps a retried callback idempotent.
        if funnel is not None:
            cols = ", ".join(("run_id", "variation_id", "ticker", "row_scope", *_RUN_FUNNEL_COLUMNS))
            placeholders = ", ".join(["?"] * (4 + len(_RUN_FUNNEL_COLUMNS)))
            conn.execute(
                f"INSERT OR REPLACE INTO research_run_funnel ({cols}) VALUES ({placeholders});",
                (run_id, 0, ticker, "cell", *(funnel.get(c) for c in _RUN_FUNNEL_COLUMNS)),  # lead_in_bars absent -> NULL
            )

        # Lane DG (`6.1g`): the single run's own ONE research_run_variations row, atomic with the
        # rest of this finalize transaction. INSERT OR IGNORE (never REPLACE, and never the
        # sweep's own key range): a re-delivered result callback for the same run_id writes
        # nothing twice, matching complete_sweep_chunk's own idempotence rule for the same table.
        # None (an undeclared strategy, an event run, or a bespoke run) writes no row at all.
        if variation is not None:
            conn.execute(
                "INSERT OR IGNORE INTO research_run_variations "
                "    (run_id, variation_id, param_json, lookback_bars, used_degrees_of_freedom) "
                "VALUES (?, 0, ?, ?, ?);",
                (run_id, variation["param_json"], variation["lookback_bars"],
                 variation["used_degrees_of_freedom"]),
            )

    return {
        "run_id": run_id, "job_id": job_id, "finished_at": now, "status": "succeeded",
        "total_return": stats.get("total_return"), "sharpe_ratio": stats.get("sharpe_ratio"),
        "num_trades": stats.get("num_trades"),
    }


# ============================================================
# M5 1B — the sweep write path (sub-brief docs/briefs/M5_phase1b_spec_brief.md §6/§7).
# Shared hashing helpers (ONE backend home for both the single-run trial and the sweep-cell trial, so the
# two hash by the identical rule — no container/backend drift). Both emit REAL digests, never the legacy
# 'LEGACY'/'FULL-SAMPLE' sentinels (§6.4) the 1A backfill reserved.
# ============================================================
def _param_config_hash(param_json: str) -> str:
    """The real param_config_hash for a trial: sha256 of the canonical param-combo JSON (never 'LEGACY').

    The sweep engine emits the canonical combo JSON (sorted keys, strategies.base.canonical_param_json); the
    single path passes the run's canonical settings snapshot. Hashing here, in one home, means a single run
    and a sweep cell hash by the identical rule.
    """
    return hashlib.sha256(param_json.encode("utf-8")).hexdigest()


def _full_sample_split_id(dataset_sha256: str) -> str:
    """The real split_id for a Phase-1 full-sample trial: sha256 over {split_type, dataset_sha256} (never 'FULL-SAMPLE').

    AD-M5-4: split_id is a content hash over the canonical split definition. A Phase-1 trial is full-sample
    over the whole dataset (identified by its content hash), so the definition is {split_type:'full_sample',
    dataset_sha256}. Phase 3 adds 'holdout'/'walkforward' split_types under the same function. A real digest,
    so it never collides with the legacy 'FULL-SAMPLE' sentinel (§6.4).
    """
    canonical = json.dumps({"split_type": "full_sample", "dataset_sha256": dataset_sha256},
                           sort_keys=True, separators=(",", ":"))  # canonical, deterministic
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_sweep_committed_cells(run_id: int) -> list:
    """The (variation_id, ticker) cells already in the cube for this run — the resume skip-set (§6.2).

    Empty on a fresh sweep (the engine then runs the whole grid). The container skips recomputing these for
    efficiency; the complete_sweep_chunk idempotence gate is the correctness backstop regardless.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT variation_id, ticker FROM research_run_cells WHERE run_id = ? AND row_scope = 'cell';",
            (run_id,),
        ).fetchall()
    return [[r["variation_id"], r["ticker"]] for r in rows]


_CELL_REFUSAL_KEYS = ("variation_id", "ticker", "param_json", "error", "reason", "message")


def record_cell_refusals(run_id: int, refusals: list, now: int | None = None) -> dict:
    """Lane D1: write a sweep's refused cells (research_run_cell_refusals) in one transaction. A refusal
    already recorded for the same (run_id, variation_id, ticker) is skipped, so a resumed sweep that
    refuses the same cell again writes nothing twice. Returns {"written", "skipped"}. Raises RunNotFound
    for an unknown run and ValueError naming the first refusal that lacks a required key."""
    now = int(time.time()) if now is None else now
    for i, refusal in enumerate(refusals):
        missing = [k for k in _CELL_REFUSAL_KEYS if refusal.get(k) is None]
        if missing:
            raise ValueError(f"cell refusal {i} is missing {missing}")
    written = 0
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM research_runs WHERE id = ?;", (run_id,)).fetchone() is None:
            raise RunNotFound(f"run {run_id} not found")
        for refusal in refusals:
            cur = conn.execute(
                "INSERT OR IGNORE INTO research_run_cell_refusals "
                "(run_id, variation_id, ticker, param_json, error, reason, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
                (run_id, int(refusal["variation_id"]), str(refusal["ticker"]), str(refusal["param_json"]),
                 str(refusal["error"]), str(refusal["reason"]), str(refusal["message"]), now),
            )
            written += cur.rowcount
    return {"written": written, "skipped": len(refusals) - written}


def get_cell_refusals(run_id: int) -> list:
    """Lane D1: a run's refused cells, ordered by ticker then combo. Raises RunNotFound for an unknown run;
    an empty list for a run with none (the ordinary case)."""
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM research_runs WHERE id = ?;", (run_id,)).fetchone() is None:
            raise RunNotFound(f"run {run_id} not found")
        rows = conn.execute(
            "SELECT variation_id, ticker, param_json, error, reason, message, created_at "
            "FROM research_run_cell_refusals WHERE run_id = ? ORDER BY ticker, variation_id;",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_sweep_chunk(run_id: int, cells: list, now: int | None = None) -> dict:
    """Write one sweep chunk's cells in ONE transaction, atomically + exactly-once (§6.1 — the load-bearing write).

    For each cell, in the arch §7 write order:
      1. Exactly-once gate: if a research_run_cells row already exists for (run_id, variation_id, ticker,
         'cell') — its PRIMARY KEY — SKIP the whole cell (its trial/cell/funnel/variation were committed in a
         prior chunk). This is the dedup anchor the append-only, UNIQUE-less research_trials lacks: the cube
         row's existence is the committed-set checkpoint, so a re-POSTed chunk appends NO second trial.
      2. Else INSERT the trial (append-only), INSERT the cell (8 metrics), INSERT OR REPLACE the funnel
         (per-cell counts at the new grain), INSERT OR IGNORE the variation (combo -> param_json).
    The whole chunk commits or rolls back (Ch 8 atomicity, kleppmann_canon.md:1955) — a chunk that raises
    partway writes nothing and is re-run whole on resume. After the writes, advance research_jobs.progress_json
    (done = committed cells for the run, total = the frozen registered_n) IN THE SAME transaction, so progress
    can never drift from the cube.

    selection_scope and registered_n are read from the frozen research_sweep_registrations row (1C writes it,
    R-PREREG); a sweep with no registration raises (1B reads the frozen scope, it does not invent one).
    Returns {"written", "skipped", "done", "total"}. Raises RunNotFound for an unknown run.
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        run = conn.execute(
            "SELECT job_id, strategy_id, strategy_version, dataset_id FROM research_runs WHERE id = ?;",
            (run_id,),
        ).fetchone()
        if run is None:
            raise RunNotFound(f"run {run_id} not found")
        reg = conn.execute(
            "SELECT registered_n, registered_cells, selection_scope FROM research_sweep_registrations WHERE run_id = ?;",
            (run_id,),
        ).fetchone()
        if reg is None:  # 1B reads the frozen selection_scope/registered_n from the registration (1C writes it)
            raise ValueError(
                f"run {run_id} has no sweep registration; a sweep must be registered (R-PREREG) before cells are written"
            )
        selection_scope = reg["selection_scope"]  # frozen at registration; stamped on every trial (§6.3)
        registered_n = reg["registered_n"]         # the registered COMBINATION count (the trial/reconcile grain)
        # The progress DENOMINATOR is the CELL total (combinations x members), written by 1C's
        # _finalize_materializing_sweep into registered_cells — so `done` (a cell count) and `total` share one
        # grain and the universe bar reads cells/cells, not the grain-mismatched combinations/cells (§27 fix).
        # A pre-1C single-member sweep has registered_cells NULL, so it falls back to registered_n (which already
        # equals its cell count) and is unchanged.
        total = reg["registered_cells"] if reg["registered_cells"] is not None else registered_n

        # The exactly-once gate, read ONCE for the chunk (not per cell): load the run's committed
        # (variation_id, ticker) set from the cube via the PK-prefix (run_id, row_scope) scan, then test
        # membership in memory. This replaces an N-per-chunk SELECT with one query + a set — the same
        # idempotence (the cube PK is the committed-set checkpoint), bounded as chunk sizes grow at scale.
        already = {
            (r["variation_id"], r["ticker"])
            for r in conn.execute(
                "SELECT variation_id, ticker FROM research_run_cells WHERE run_id = ? AND row_scope = 'cell';",
                (run_id,),
            ).fetchall()
        }
        written = 0  # cells newly committed this chunk
        skipped = 0  # cells already present (the idempotence gate fired)
        for cell in cells:
            variation_id = int(cell["variation_id"])
            ticker = cell["ticker"]
            if (variation_id, ticker) in already:  # the gate: a committed (or already-written-this-chunk) cell is skipped whole
                skipped += 1
                continue
            param_json = cell["param_json"]
            dataset_sha256 = cell["dataset_sha256"]
            # Every cell carries all 8 metric keys + all funnel keys (the engine emits them; a nullable ratio
            # is present-with-None, not absent). Read them by SUBSCRIPT so a malformed cell (a missing key)
            # fails loud and rolls the chunk back — never silently NULLs a column (the funnel read below is
            # already subscript; the stats read matches it for one consistent fail-loud contract).
            # 1) the trial (append-only; sharpe denormalized from the cell for the variance view, §6.1)
            conn.execute(
                "INSERT INTO research_trials "
                "    (run_id, variation_id, ticker, strategy_id, strategy_version, dataset_id, dataset_sha256, "
                "     split_id, param_config_hash, sharpe, trial_kind, selection_scope, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sweep_combo', ?, ?);",
                (run_id, variation_id, ticker, run["strategy_id"], run["strategy_version"], run["dataset_id"],
                 dataset_sha256, _full_sample_split_id(dataset_sha256), _param_config_hash(param_json),
                 cell["sharpe_ratio"], selection_scope, now),
            )
            # 2) the cube cell (the 8 metrics + the 2 Track-B distribution scalars). The sweep cell carries
            # skew/kurtosis (sweep_portfolio_cells), so the write projects _RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS.
            # Subscript read (cell[c]) keeps the fail-loud contract: a malformed cell missing a key rolls the
            # chunk back rather than silently NULLing a column (the re-baked worker always sends both).
            _cell_cols = _RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS
            conn.execute(
                "INSERT INTO research_run_cells "
                "    (run_id, variation_id, ticker, row_scope, " + ", ".join(_cell_cols) + ") "
                "VALUES (?, ?, ?, 'cell', " + ", ".join(["?"] * len(_cell_cols)) + ");",
                (run_id, variation_id, ticker, *(cell[c] for c in _cell_cols)),
            )
            # 3) the per-cell funnel (OR REPLACE keeps a retried chunk idempotent at the funnel grain).
            # B4 SF2: the wide sweep path + a sanctioned zero-param cell carry ALL funnel keys -> write the row.
            # A bespoke zero-param strategy (run bypasses the shared portfolio helper) carries NO funnel keys ->
            # SKIP the funnel row (get_run_cells never joins research_run_funnel, so the cell still renders; the
            # funnel drill-down honestly shows its absence). A PARTIAL funnel (some keys, not all) is a contract
            # bug -> fail loud, never a half-written row.
            present_funnel = [c for c in _RUN_FUNNEL_COLUMNS if c in cell]
            if present_funnel and not _funnel_key_set_ok(present_funnel):
                raise ValueError(
                    f"cell (run_id={run_id}, variation_id={variation_id}, ticker={ticker!r}) carries a PARTIAL "
                    f"funnel ({len(present_funnel)}/{len(_RUN_FUNNEL_COLUMNS)} keys): {present_funnel}"
                )
            if present_funnel:  # all funnel keys present -> write; none present -> the bespoke opt-out (no row)
                fcols = ", ".join(("run_id", "variation_id", "ticker", "row_scope", *_RUN_FUNNEL_COLUMNS))
                fph = ", ".join(["?"] * (4 + len(_RUN_FUNNEL_COLUMNS)))
                conn.execute(
                    f"INSERT OR REPLACE INTO research_run_funnel ({fcols}) VALUES ({fph});",
                    (run_id, variation_id, ticker, "cell", *(cell.get(c) for c in _RUN_FUNNEL_COLUMNS)),  # lead_in_bars absent -> NULL
                )
            # 4) the variation lookup (once per variation_id, INSERT OR IGNORE dedups across
            # tickers/chunks). Lane DF2 (`6.1e`): also carries lookback_bars and
            # used_degrees_of_freedom, the strategy's own per-combo declarations the vectorized
            # sweep entrypoint now emits on every cell (None when the strategy's own method raises
            # NotImplementedError, never a substituted number). Read with cell.get, not cell[...]:
            # unlike the funnel's fail-loud subscript read above, an older caller's cell dict that
            # predates these two keys is a legitimate absence here, not a malformed cell.
            conn.execute(
                "INSERT OR IGNORE INTO research_run_variations "
                "    (run_id, variation_id, param_json, lookback_bars, used_degrees_of_freedom) "
                "VALUES (?, ?, ?, ?, ?);",
                (run_id, variation_id, param_json, cell.get("lookback_bars"), cell.get("used_degrees_of_freedom")),
            )
            already.add((variation_id, ticker))  # track within the chunk so a duplicate cell is skipped exactly-once
            written += 1

        done = conn.execute(
            "SELECT COUNT(*) AS n FROM research_run_cells WHERE run_id = ? AND row_scope = 'cell';",
            (run_id,),
        ).fetchone()["n"]
        # Progress advances IN this transaction, so it lands atomically with the chunk (§6.3); eta_s is an
        # honest NULL (not computed in 1B — a fabricated estimate would be worse than absent).
        conn.execute(
            "UPDATE research_jobs SET progress_json = ? WHERE id = ?;",
            (json.dumps({"done": done, "total": total, "eta_s": None}), run["job_id"]),
        )
    return {"written": written, "skipped": skipped, "done": done, "total": total}


def complete_sweep(run_id: int, annualization: dict | None = None, environment: dict | None = None,
                   now: int | None = None) -> dict:
    """Finalize a succeeded sweep run (§7): status='succeeded' + env provenance + run-level annualization; NO stats row.

    A multi-cell sweep has no single run-level metric tuple (§7/§14.5), so it writes NO research_run_stats
    row: the cube cells the chunks wrote are the result. It records the seven environment provenance
    columns (the container vbt/python/build/hostname plus sizing_method/sizing_spec/cost_model, #376
    ruling 4, copied from the sweep-done line, exactly as complete_run does for a single run, since a
    sweep run is no less reproducibility-tracked than a single one), the run-level
    annualization (one factor for a single-asset-class sweep; NULL for a mixed one, §15.1), and flips the run
    to succeeded. The annualization dict is guarded exactly as complete_run guards it (all three keys, or
    None). Raises RunNotFound for an unknown run.
    """
    now = int(time.time()) if now is None else now
    if annualization is not None and set(annualization) != set(_RUN_ANNUALIZATION_COLUMNS):
        raise ValueError(
            f"annualization dict keys mismatch: expected {sorted(_RUN_ANNUALIZATION_COLUMNS)}, "
            f"got {sorted(annualization)}"
        )
    with transaction() as conn:
        row = conn.execute("SELECT job_id FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id} not found")
        # The executed-vs-registered reconcile (AD-M5-6, at the COMBINATION grain, NEVER the cell grain): cells
        # = combinations x members, so comparing the executed CELL count against registered_n would false-fire
        # grid_grew on every honest multi-member universe run. Compare the executed distinct-variation_id count
        # (member-independent) against the frozen registered_n; executed-combinations-exceeds-registered (a grid
        # grown after peeking) sets grid_grew=1. An executed count BELOW registered_n is a grid shrink, not a
        # grow — grid_grew correctly stays 0 (whether that under-count adjusts the DSR's honest N is U4's, not U2's).
        executed_combos = conn.execute(
            "SELECT COUNT(DISTINCT variation_id) AS n FROM research_run_cells WHERE run_id = ? AND row_scope = 'cell';",
            (run_id,),
        ).fetchone()["n"]
        reg = conn.execute(
            "SELECT registered_n FROM research_sweep_registrations WHERE run_id = ?;", (run_id,),
        ).fetchone()
        grid_grew = 1 if (reg is not None and executed_combos > reg["registered_n"]) else 0
        set_cols = (
            ["status = 'succeeded'", "finished_at = ?", "error = NULL", "grid_grew = ?"]
            + [f"{c} = ?" for c in _RUN_ENV_COLUMNS]              # the container provenance (vbt/python/build/host/sizing/cost)
            + [f"{c} = ?" for c in _RUN_ANNUALIZATION_COLUMNS]
        )
        params = (
            [now, grid_grew]
            + [(environment or {}).get(c) for c in _RUN_ENV_COLUMNS]            # None -> NULL (no env reported)
            + [(annualization or {}).get(c) for c in _RUN_ANNUALIZATION_COLUMNS]  # None -> all three NULL (§15.1 mixed)
            + [run_id]
        )
        conn.execute(f"UPDATE research_runs SET {', '.join(set_cols)} WHERE id = ?;", params)
    return {"run_id": run_id, "job_id": row["job_id"], "finished_at": now, "status": "succeeded", "grid_grew": grid_grew}


def fail_run(run_id: int, error: str, now: int | None = None) -> dict:
    """Finalize a failed run: status='failed', error set, finished_at. Returns SSE data."""
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        row = conn.execute("SELECT job_id FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id} not found")
        conn.execute(
            "UPDATE research_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?;",
            (error, now, run_id),
        )
    return {"run_id": run_id, "job_id": row["job_id"], "finished_at": now,
            "status": "failed", "error": error}


def cancel_run(run_id: int, status: str = "cancelled", now: int | None = None) -> dict:
    """Finalize a cancelled run (brief I): set the terminal status, NULL all six artifact paths.

    The terminal status comes from the caller (the worker callback body), not hardcoded, so
    M5 run_sweep / M6 walk-forward 'cancelled_partial' reuse this unchanged. error stays NULL
    (cancellation is not an error). Returns the data the route needs for research.run_cancelled.
    Raises RunNotFound if the run id is unknown.
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        row = conn.execute("SELECT job_id FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"run {run_id} not found")
        null_paths = ", ".join(f"{c} = NULL" for c in _RUN_ARTIFACT_COLUMNS)
        conn.execute(
            f"UPDATE research_runs SET status = ?, finished_at = ?, error = NULL, {null_paths} "
            f"WHERE id = ?;",
            (status, now, run_id),
        )
    return {"run_id": run_id, "job_id": row["job_id"], "finished_at": now, "status": status}


# =============================================================
# Lane WF (6.2b): the walk-forward stage's own settings read and result writers.
# =============================================================

class WalkforwardParameterAbsent(RuntimeError):
    """WF-1: one or more of the three ruled walkforward.* settings is blank (research_settings.value
    == ''), the SAME 'no configured value' convention stage_runner.daily_store_path already uses.
    Names every absent key together, never only the first one found, so a caller sees the whole
    gap in one refusal rather than fixing keys one at a time. The message carries the literal
    reason token 'walkforward-parameter-absent' so a caller can match on the name, never on
    reworded prose."""

    def __init__(self, missing_keys: list) -> None:
        self.missing_keys = sorted(missing_keys)  # every absent key, together, alphabetically for a stable message
        super().__init__(
            f"walkforward-parameter-absent: {self.missing_keys} carry no configured value in the "
            "'walkforward' settings scope; refusing rather than guessing the #392 ruling 4 figures"
        )


def _finite_or_none(value) -> "float | None":
    """A walk-forward profit figure, coerced to a real, storable number or an honest NULL. nan and
    inf are never written (SQLite would store them, but a reader comparing against a frozen
    threshold must never see one), matching record_walkforward_slices/run_stats's own shared rule."""
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def read_walkforward_parameters() -> dict:
    """WF-1: the three ruled walkforward.* values (#392 ruling 4), read live from research_settings,
    never a literal in code. Raises WalkforwardParameterAbsent naming every blank key together when
    one or more is absent (the raw stored value is checked for '' BEFORE parse_value, the same order
    blank_setting's own precedent writes it in, since a blanked number would otherwise raise inside
    parse_value's own int() conversion rather than reading as a clean absence)."""
    keys = ("in_sample_bars", "out_of_sample_bars", "anchoring")
    with get_conn() as conn:
        rows = {
            row["key"]: (row["value"], row["value_type"])
            for row in conn.execute(
                "SELECT key, value, value_type FROM research_settings WHERE scope = 'walkforward';"
            ).fetchall()
        }
    missing = [k for k in keys if k not in rows or rows[k][0] == '']  # absent row or blanked value, both read as absent
    if missing:
        raise WalkforwardParameterAbsent(missing)
    return {k: parse_value(rows[k][0], rows[k][1]) for k in keys}


def _insert_walkforward_slices(conn, run_id: int, slices: list) -> None:
    """The INSERT OR REPLACE loop itself, taking an ALREADY-OPEN connection so it can run either
    inside record_walkforward_slices's own transaction or inside record_walkforward_result's
    shared one (round 4 fold, n7-WF-4a1c, code-quality pass 1 finding B1(b)). Kept as a private
    helper so the SQL text lives in exactly one place rather than two copies drifting apart.

    Lane LB (5.8e): `fill_reach_ts` and `entries_masked_at_end` are read with `.get(...)`, never a
    subscript, because a slice dict built before this lane (an older probe fixture, a stale worker
    image mid-rebake) carries neither key at all -- that is a legitimate absence, not a malformed
    slice, unlike the other keys this loop already reads by subscript. `entries_masked_at_end`
    defaults to `0` (an honest, always-computed count on every current entrypoint build, never a
    placeholder); `fill_reach_ts` defaults to `None` (a real absence: most slices never reach)."""
    for s in slices:
        conn.execute(
            "INSERT OR REPLACE INTO research_walkforward_slices "
            "    (run_id, slice_index, in_sample_start_ts, in_sample_end_ts, "
            "     out_of_sample_start_ts, out_of_sample_end_ts, param_config_hash, "
            "     in_sample_profit, out_of_sample_profit, trade_count, "
            "     fill_reach_ts, entries_masked_at_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (run_id, s["slice_index"], s["in_sample_start_ts"], s["in_sample_end_ts"],
             s["out_of_sample_start_ts"], s["out_of_sample_end_ts"], s["param_config_hash"],
             _finite_or_none(s["in_sample_profit"]), _finite_or_none(s["out_of_sample_profit"]),
             s["trade_count"], s.get("fill_reach_ts"), int(s.get("entries_masked_at_end") or 0)),
        )


def record_walkforward_slices(run_id: int, slices: list) -> None:
    """AC-19: one row per (run_id, slice_index), written from the real entrypoint's own per-slice
    emit. INSERT OR REPLACE (the research_run_cells idiom): idempotent on a re-delivered slice line,
    keyed on the table's own (run_id, slice_index) primary key, never a second row. Non-finite
    profit figures are stored as NULL (_finite_or_none), an honest absence rather than a stored
    nan/inf a later reader's comparison would silently mishandle."""
    with transaction() as conn:
        _insert_walkforward_slices(conn, run_id, slices)


def _check_walkforward_stats_complete(total_return, max_drawdown, num_trades,
                                       total_fees_paid, total_slippage_cost) -> None:
    """Round-4 fold (n7-WF-4a1c), code-quality pass 1 finding B1(b), WIDENED round 6 (code-quality
    pass 2 finding CQ3): research_run_stats.total_return, max_drawdown, num_trades,
    total_fees_paid AND total_slippage_cost are ALL five NOT NULL columns
    _upsert_walkforward_run_stats writes -- the round-4 build only guarded the first three, so a
    NaN fee or slippage sum (the entrypoint's own _finite_or_none(total_oos_fees_raw) turning a NaN
    into None) still reached the same raw sqlite3.IntegrityError this check exists to prevent.
    Refusing here, before any SQL runs, turns that into a named, catchable ValueError a caller can
    recognise by its own leading token, naming every offending column together rather than only
    the first one found."""
    offending = {
        "total_return": total_return, "max_drawdown": max_drawdown, "num_trades": num_trades,
        "total_fees_paid": total_fees_paid, "total_slippage_cost": total_slippage_cost,
    }
    missing = {name: value for name, value in offending.items() if value is None}
    if missing:
        missing_repr = ", ".join(f"{name}={value!r}" for name, value in missing.items())
        raise ValueError(
            "walkforward-run-stats-incomplete: total_return, max_drawdown, num_trades, "
            f"total_fees_paid and total_slippage_cost must be real numbers, got {missing_repr}"
        )


def _upsert_walkforward_run_stats(conn, run_id: int, in_sample_profit, out_of_sample_profit,
                                  wfa_boundary_buffer_bars: int, total_return, max_drawdown,
                                  num_trades: int, total_fees_paid, total_slippage_cost, *,
                                  entries_masked_at_end: "int | None" = None) -> None:
    """The INSERT/UPSERT itself, taking an ALREADY-OPEN connection for the same reason
    _insert_walkforward_slices does (round 4 fold, n7-WF-4a1c). Refuses via
    _check_walkforward_stats_complete BEFORE the SQL, so a bad call never reaches the database at
    all, on either call path (record_walkforward_run_stats's own transaction, or
    record_walkforward_result's shared one).

    `entries_masked_at_end` (lane LB, 5.8e) is keyword-only with a `None` default, so an existing
    caller predating this lane (`scripts/probes/walkforward_bundle/_lib.py`'s own direct call) is
    unaffected and writes NULL on this one nullable column, an honest "not measured", never a
    fabricated zero."""
    _check_walkforward_stats_complete(total_return, max_drawdown, num_trades, total_fees_paid, total_slippage_cost)
    conn.execute(
        "INSERT INTO research_run_stats "
        "    (run_id, total_return, max_drawdown, num_trades, total_fees_paid, "
        "     total_slippage_cost, in_sample_profit, out_of_sample_profit, wfa_boundary_buffer_bars, "
        "     entries_masked_at_end) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "    total_return = excluded.total_return, "
        "    max_drawdown = excluded.max_drawdown, "
        "    num_trades = excluded.num_trades, "
        "    total_fees_paid = excluded.total_fees_paid, "
        "    total_slippage_cost = excluded.total_slippage_cost, "
        "    in_sample_profit = excluded.in_sample_profit, "
        "    out_of_sample_profit = excluded.out_of_sample_profit, "
        "    wfa_boundary_buffer_bars = excluded.wfa_boundary_buffer_bars, "
        "    entries_masked_at_end = excluded.entries_masked_at_end;",
        (run_id, _finite_or_none(total_return), _finite_or_none(max_drawdown), num_trades,
         _finite_or_none(total_fees_paid), _finite_or_none(total_slippage_cost),
         _finite_or_none(in_sample_profit), _finite_or_none(out_of_sample_profit),
         wfa_boundary_buffer_bars, entries_masked_at_end),
    )


def record_walkforward_run_stats(run_id: int, in_sample_profit, out_of_sample_profit,
                                 wfa_boundary_buffer_bars: int, total_return, max_drawdown,
                                 num_trades: int, total_fees_paid, total_slippage_cost, *,
                                 entries_masked_at_end: "int | None" = None) -> None:
    """AC-17/AC-19 (fold item 2c, round 1 BLOCKER LC2 / the lead's read): the walk-forward stage's
    own run-level rollup. UPSERTS rather than assuming a row already exists: a walk-forward run is
    dispatched through dispatch_sweep (job kind run_sweep), whose own finalize (complete_sweep)
    writes NO research_run_stats row at all ('a multi-cell sweep has no single run-level metric',
    that function's own docstring): a walk-forward run is the one sweep shape that DOES have a
    genuine run-level figure, so this function is what gives it a row, the first time it is called
    for a run_id.

    The five general columns (total_return, max_drawdown, num_trades, total_fees_paid,
    total_slippage_cost) are no longer hardcoded 0/0.0/0: they are the STITCHED out-of-sample
    figures the entrypoint computes from the chained equity curve (end value over init_cash minus
    one; the chained curve's own max drawdown; the summed out-of-sample trade count, fees and
    slippage across every slice), read by the same statistics the single-run path already uses on
    a real equity Series -- fixing the round-1 finding that this row read as an honest-looking but
    wrong 0.0/0.0/0 for every walk-forward run. The ON CONFLICT branch now writes all EIGHT columns
    (not only the three walk-forward-specific ones), so a repeat relay call (a retried POST) updates
    the whole row consistently rather than leaving the five general columns pinned at their first
    value. Non-finite figures store as NULL (_finite_or_none); num_trades is a plain int (the NOT
    NULL schema column), never None -- an honest zero when no out-of-sample trade ever occurred.

    Round-4 fold (n7-WF-4a1c), code-quality pass 1 finding B1(b), widened round 6 (code-quality
    pass 2 finding CQ3): all FIVE of total_return, max_drawdown, num_trades, total_fees_paid and
    total_slippage_cost are refused BEFORE the SQL (_check_walkforward_stats_complete) when any is
    None, naming the reason walkforward-run-stats-incomplete, rather than letting a NULL reach the
    NOT NULL columns and raise a raw sqlite3.IntegrityError."""
    with transaction() as conn:
        _upsert_walkforward_run_stats(
            conn, run_id, in_sample_profit, out_of_sample_profit, wfa_boundary_buffer_bars,
            total_return, max_drawdown, num_trades, total_fees_paid, total_slippage_cost,
            entries_masked_at_end=entries_masked_at_end,
        )


def record_walkforward_result(run_id: int, slices: list, stats: dict) -> None:
    """Round-4 fold (n7-WF-4a1c), code-quality pass 1 finding B1(b): writes the slice rows and the
    run-level stats row inside ONE transaction, so a refused stats write (an incomplete NOT NULL
    set, all five as of round 6's CQ3, _check_walkforward_stats_complete) leaves no half-written
    record behind. Before this
    function existed, worker_walkforward_result called record_walkforward_slices and
    record_walkforward_run_stats as two SEPARATE transactions: the slices would already be
    committed by the time the stats write raised, so a caller who then retried the whole relay saw
    slice rows with no matching research_run_stats row, and query_runs_missing_walkforward_record
    could never surface it (the slice rows genuinely exist). Now either both writes commit
    together, or neither does.

    `stats` carries the same keyword names WalkforwardResultIn's own fields use: in_sample_profit,
    out_of_sample_profit, wfa_boundary_buffer_bars, total_return, max_drawdown, num_trades,
    total_fees_paid, total_slippage_cost.

    Lane LB (5.8e): the run-level `entries_masked_at_end` is the SUM over `slices`' own per-slice
    counts, derived here rather than trusted from a separately-relayed run-level number, so the
    per-slice rows stay the single source of truth for this figure (never two numbers that could
    silently drift apart). `s.get(...)` tolerates a slice dict predating this lane (an older probe
    fixture), reading its absence as `0`, never a crash."""
    total_entries_masked_at_end = sum(int(s.get("entries_masked_at_end") or 0) for s in slices)
    with transaction() as conn:
        _insert_walkforward_slices(conn, run_id, slices)
        _upsert_walkforward_run_stats(
            conn, run_id, stats["in_sample_profit"], stats["out_of_sample_profit"],
            stats["wfa_boundary_buffer_bars"], stats["total_return"], stats["max_drawdown"],
            stats["num_trades"], stats["total_fees_paid"], stats["total_slippage_cost"],
            entries_masked_at_end=total_entries_masked_at_end,
        )


def record_run_equity_path(run_id: int) -> None:
    """Set research_runs.equity_path to this run's own conventional container path (fold item 2b,
    round 1 BLOCKER LC2 / the lead's read).

    The SAME string shape a single run's own artifact-path column stores
    (f"/data/runs/{run_id}/equity.parquet", research_lab.worker.jobs.run_backtest's own
    container_artifact_paths convention), restated here as a literal rather than imported: this
    module never imports the worker's job code (the engine/backend boundary this codebase already
    keeps). The caller (worker_walkforward_result) checks artifact_store.store.exists(run_id,
    "equity.parquet") FIRST, so this only ever runs after the bytes are actually in the server
    store (K.0's own no-succeeded-without-bytes invariant) -- a zero-slice walk-forward run, which
    never uploads a file, never calls this, so its equity_path correctly stays NULL rather than
    pointing at bytes that do not exist."""
    with transaction() as conn:
        conn.execute(
            "UPDATE research_runs SET equity_path = ? WHERE id = ?;",
            (f"/data/runs/{run_id}/equity.parquet", run_id),
        )


def query_walkforward_slices_out_of_window(conn) -> int:
    """Registry row walkforward_slices_out_of_window: count of research_walkforward_slices rows
    whose own bounds fall OUTSIDE the granted window their run's walkforward draw named (AC-18's
    own words: "every slice lies inside that granted window"). Expected zero always:
    compute_slice_bounds's own contract already refuses to produce a slice that does not fit
    (walkforward.py's own docstring: "the scheme stops advancing the moment the next slice would
    not fit, rather than truncating one"), so a nonzero count here means that contract was
    violated somewhere between the entrypoint and this table, never an expected state. The window
    is read via the run's own run_linked row back to its draw_requested row (tracker.py's own
    draw_id_for_run precedent), the ONLY path from a run to the draw that granted its window; a
    slice whose run carries no such link is not counted here at all (that gap is
    runs_missing_walkforward_record's own job, immediately below).

    Fold item 4 (round 1, SHOULD-FIX LC3): a NULL window_start_ts or window_end_ts on the draw row
    counts its slices as OUT of window, never silently exempted. SQL's three-valued logic makes a
    plain `<` or `>=` comparison against a NULL bound evaluate to NULL, which a WHERE clause treats
    as "no match" -- so the naive query would report zero out-of-window slices for a run whose
    window bounds are NULL, which is a different fact from "every slice really fits": unknown is
    not the same as inside, and this table is not the place to assume the more comfortable of the
    two. The explicit `IS NULL` arms below make that case count, rather than vanish."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM research_walkforward_slices s "
        "JOIN research_data_draws rl ON rl.event_kind = 'run_linked' AND rl.run_id = s.run_id "
        "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
        "WHERE dr.window_start_ts IS NULL OR dr.window_end_ts IS NULL "
        "   OR s.in_sample_start_ts < dr.window_start_ts OR s.out_of_sample_end_ts >= dr.window_end_ts;"
    ).fetchone()
    return row["n"]


def query_runs_missing_walkforward_record(conn) -> int:
    """Registry row runs_missing_walkforward_record: count of runs that were GRANTED a walkforward
    draw and linked to a real run (research_data_draws' own run_linked row, decision='granted',
    its draw_requested row's stage='walkforward') but carry NO row at all in
    research_walkforward_slices. Expected zero on a healthy pipeline; a nonzero count names either
    an entrypoint that emitted no slices for a real window (a silent AC-36 gap this table would
    otherwise hide) or a worker-to-backend relay that failed after the run's own status already
    flipped to succeeded (run_sweep.py's own walk-forward relay, whose own failure path logs
    rather than fails the run for exactly this reason, named in this lane's build report)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM research_data_draws rl "
        "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
        "WHERE rl.event_kind = 'run_linked' AND rl.decision = 'granted' AND dr.stage = 'walkforward' "
        "  AND NOT EXISTS (SELECT 1 FROM research_walkforward_slices s WHERE s.run_id = rl.run_id);"
    ).fetchone()
    return row["n"]


# =============================================================
# M3 brief F: run read API (DDIA Ch 3/4 star-schema sort access paths, section 3.5)
# =============================================================

# sort key -> (ORDER BY column, the §3.5 index it must use). The four metric sorts drive the
# query FROM the research_run_stats fact table so SQLite scans the matching index in order
# (no temp B-tree): the access path §3.5 created those indexes for. A metric sort therefore
# lists only runs that HAVE that metric (completed runs); a pending run has no Sharpe to rank.
# 'created_at' is the default full list (all runs); it sorts on the integer primary key (rowid),
# which is creation order since ids are monotonic, so it needs no secondary index either.
_RUN_SORT = {
    "sharpe":   ("s.sharpe_ratio", "ix_stats_sharpe"),
    "return":   ("s.total_return", "ix_stats_return"),
    "drawdown": ("s.max_drawdown", "ix_stats_dd"),
    "trades":   ("s.num_trades",   "ix_stats_trades"),
}

# The runs + stats column projection shared by list and detail. Works under either join
# direction (it names r.* and s.* explicitly). NULL metric columns mean the run has no
# stats row yet (pending/failed/cancelled) — only possible in the created_at LEFT JOIN list.
_RUN_COLUMNS = (
    "SELECT r.id, r.job_id, r.strategy_id, r.strategy_version, r.dataset_id, r.dataset_sha256, "
    "       r.name, r.vbt_version, r.python_version, r.build_provenance, r.hostname, "
    "       r.sizing_method, r.sizing_spec, r.cost_model, "  # #376 ruling 4: the sizing/cost provenance, NULL pre-unit
    "       r.created_at, r.started_at, r.finished_at, r.status, r.error, "
    "       r.equity_path, r.drawdowns_path, r.trades_path, r.stats_path, r.indicators_path, r.meta_path, "
    "       r.equity_sparkline, "  # Unit-4 D2: the materialized sparkline, served from the row (no parquet open)
    "       COALESCE(r.kind, 'standard') AS kind, "  # M4 D5: trial-provenance marker; NULL/legacy reads 'standard'
    "       r.annualization_factor, r.annualization_freq, r.annualization_year_freq, "  # Phase-2 Unit 1: the run's annualization (NULL for event/legacy)
    "       s.total_return, s.sharpe_ratio, s.max_drawdown, s.num_trades, s.win_rate, "
    "       s.total_fees_paid, s.total_slippage_cost, s.profit_factor "
)


# M4 D5: the WHERE fragment for the runs-list kind filter. A CLOSED set of literal fragments (never a
# caller string interpolated into SQL): 'scratch' shows only editor test runs; 'standard' shows ordinary
# + legacy runs (NULL kind reads as standard); None/absent applies no filter. KeyError on an unknown key
# surfaces to the route as a 400 (the same fail-loud contract as the sort key).
_RUN_KIND_FILTERS = {
    "scratch": "r.kind = 'scratch'",
    "standard": "(r.kind IS NULL OR r.kind = 'standard')",
}


def _kind_where(kind_filter: str | None) -> str:
    """The `WHERE <kind predicate> ` fragment (with trailing space), or '' for no filter (M4 D5)."""
    if kind_filter is None:
        return ""
    return f"WHERE {_RUN_KIND_FILTERS[kind_filter]} "  # KeyError on an unknown filter -> route 400


# GV2-4a: a run is synthetic when the dataset it ran on (a price universe row included) has source
# 'synthetic'; the label is that one column, stored once. NOT EXISTS keeps a run with no matching dataset row.
_NOT_SYNTHETIC_RUN_PRED = (
    "NOT EXISTS (SELECT 1 FROM research_datasets d WHERE d.id = r.dataset_id AND d.source = 'synthetic')"
)


class RunDatasetMissing(LookupError):
    """The run, or the dataset row its dataset_id names, is not in research.db (GV2-4b).

    dataset_id is the id the run names, or None when the run row itself is absent."""

    def __init__(self, run_id: int, dataset_id: int | None):
        self.run_id = run_id
        self.dataset_id = dataset_id
        if dataset_id is None:
            super().__init__(f"run {run_id} has no run row in research.db")
        else:
            super().__init__(f"run {run_id} names dataset {dataset_id}, which has no row")


def run_data_is_synthetic(run_id: int) -> bool:
    """True when the run's dataset (a price dataset or a price universe row) has source 'synthetic'.

    Raises RunDatasetMissing, naming the run, when the run row or its dataset row is absent: the
    answer is then unknown, and the paper step must refuse on an unknown, never assume real data.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT r.dataset_id AS dataset_id, d.source AS source FROM research_runs r "
            "LEFT JOIN research_datasets d ON d.id = r.dataset_id WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
    if row is None:
        raise RunDatasetMissing(run_id, None)
    if row["source"] is None:
        raise RunDatasetMissing(run_id, row["dataset_id"])
    return row["source"] == "synthetic"


def _runs_where(kind_filter: str | None, include_synthetic: bool) -> str:
    """`WHERE p1 AND p2 ` from the kind predicate and the synthetic predicate, or '' when neither applies."""
    preds = []
    if kind_filter is not None:
        preds.append(_RUN_KIND_FILTERS[kind_filter])  # KeyError on an unknown filter -> route 400
    if not include_synthetic:
        preds.append(_NOT_SYNTHETIC_RUN_PRED)
    return ("WHERE " + " AND ".join(preds) + " ") if preds else ""


def runs_list_sql(sort: str, kind_filter: str | None = None, include_synthetic: bool = True) -> str:
    """The SELECT for GET /runs with the chosen sort + optional kind filter. KeyError on an unknown key.

    Exposed (not inlined) so the brief F probe can run EXPLAIN QUERY PLAN on the exact SQL
    and assert the §3.5 index is the access path (the claim, not just output order).
      - metric sort: FROM research_run_stats (index-backed), INNER JOIN runs — completed runs only.
      - created_at:  FROM research_runs LEFT JOIN stats, ORDER BY rowid — all runs, newest first.
    The M4 D5 kind filter (scratch | standard | None) adds a WHERE on research_runs to either path; it
    is the no-index measured-later scan the brief records (cheap at the 500-row cap, canon :988-994).
    """
    where = _runs_where(kind_filter, include_synthetic)  # '' or 'WHERE <predicate> [AND <predicate>] '
    if sort == "created_at":
        return (f"{_RUN_COLUMNS} FROM research_runs r "
                "LEFT JOIN research_run_stats s ON s.run_id = r.id "
                f"{where}ORDER BY r.id DESC LIMIT ? OFFSET ?;")
    if sort == "name":
        # U3 (pre-M4 Brief A1): name sort goes over research_runs with a LEFT JOIN to stats so it
        # keeps EVERY run — a name sort must not drop a run just because it has no stats row (the
        # metric INNER-JOIN family below does drop those). NULLs-last (Cowork, DECISIONS #74 #5):
        # the leading `r.name IS NULL` flag sorts unlabeled runs to the bottom; then case-insensitive
        # A->Z; `r.id DESC` is the deterministic tiebreak. This ORDER BY matches the composite
        # EXPRESSION index ix_runs_name ((name IS NULL), name COLLATE NOCASE, id DESC) column-for-
        # column, so EXPLAIN uses it as a covering index with no temp B-tree sort (a plain
        # name-only index would NOT, because ASC indexes order NULLs first; see the index comment).
        return (f"{_RUN_COLUMNS} FROM research_runs r "
                "LEFT JOIN research_run_stats s ON s.run_id = r.id "
                f"{where}ORDER BY r.name IS NULL, r.name COLLATE NOCASE ASC, r.id DESC LIMIT ? OFFSET ?;")
    column, _ = _RUN_SORT[sort]  # KeyError surfaces to the route as a 400
    return (f"{_RUN_COLUMNS} FROM research_run_stats s "
            "JOIN research_runs r ON r.id = s.run_id "
            f"{where}ORDER BY {column} DESC LIMIT ? OFFSET ?;")


def _decode_sparkline(d: dict) -> dict:
    """Unit-4 D2: decode the equity_sparkline JSON-text column to a number[] (or None) for the API.

    The column is stored as JSON text; the response carries a list. SQL NULL (a non-succeeded run,
    or a legacy run not yet backfilled) stays None — never a fake empty curve. A non-null but
    unparseable value fails loud (json.loads raises), never silently nulled.
    """
    raw = d.get("equity_sparkline")
    d["equity_sparkline"] = json.loads(raw) if raw is not None else None
    return d


def list_runs(limit: int = 50, offset: int = 0, sort: str = "created_at",
              kind_filter: str | None = None, include_synthetic: bool = False) -> list[dict]:
    """Paginated run list with stats joined, sorted per `sort` (default newest-first).

    M4 D5: `kind_filter` (scratch | standard | None) narrows to editor test runs or ordinary/legacy
    runs; None lists every run. KeyError on an unknown filter (the route validates it to a 400 first).
    GV2-4a: runs on synthetic datasets are left out unless `include_synthetic` is true.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_conn() as conn:
        rows = conn.execute(runs_list_sql(sort, kind_filter, include_synthetic), (limit, offset)).fetchall()
    return [_decode_sparkline(dict(r)) for r in rows]


def get_run(run_id: int) -> dict | None:
    """One run with its stats joined, or None if the id is unknown."""
    with get_conn() as conn:
        row = conn.execute(
            f"{_RUN_COLUMNS} FROM research_runs r "
            "LEFT JOIN research_run_stats s ON s.run_id = r.id WHERE r.id = ?;",
            (run_id,),
        ).fetchone()
    return _decode_sparkline(dict(row)) if row is not None else None


def get_run_funnel(run_id: int) -> dict | None:
    """The signal-funnel fact row for a run, or None (Phase-2 Unit 3).

    None means the run has no funnel: an event run (the funnel is a price-path concept), a legacy run
    that predates this unit, or a bespoke price strategy that bypassed long_only_signal_result. The UI
    shows the funnel panel only when this is present (an honest absence, never a fabricated zero-row).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT run_id, " + ", ".join(_RUN_FUNNEL_COLUMNS) + " FROM research_run_funnel WHERE run_id = ?;",
            (run_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_run_lead_in_bars(run_id: int, *, conn=None) -> int:
    """The number of lead-in bars a run's OWN recorded sizing spec asked for (lane SW, plan section 9).

    A pure read: no new column and no new table (design risk 1 -- this lane changes NO schema). It
    reads research_runs.sizing_spec, the JSON column #376 ruling 4 added and every one of the three
    research-runner entrypoints already writes on every dispatch, and returns its estimator_window
    field: the exact bar count clip_frame_with_lead_in demanded before window_start_ts for this run.
    A caller comparing a lead-in run's bars_total against "traded bars plus the lead-in" gets the
    lead-in size from the run itself, never from the sizing registry's CURRENT default, which could
    have changed since this run actually dispatched.

    Fold item 3 (code-quality pass 1, lane DF2 round 2): `conn` is keyword-only and defaults to
    `None`. A caller that already holds an open connection (check_1_evidence_floor, so this module's
    own docstring promise, "no check opens a connection beyond the one it's passed," stays true)
    passes it through and this function reads on that connection, never opening a second one. A
    caller with no connection of its own (the plan-section-9 callers this function was built for)
    leaves `conn=None` and this function opens and closes its own, exactly as before. Both branches
    run the SAME select, once, so there is one function body and no duplicated SQL.

    Raises RunNotFound (shared with every other run reader in this module) when run_id names no
    research_runs row, and SizingSpecNotRecordedError when the row exists but sizing_spec is NULL.
    """
    if conn is not None:  # the caller's own open connection: read on it, never open a second one
        row = conn.execute(
            "SELECT sizing_spec FROM research_runs WHERE id = ?;",
            (run_id,),
        ).fetchone()
    else:  # no connection supplied: open and close our own, exactly as before this fold
        with get_conn() as owned_conn:
            row = owned_conn.execute(
                "SELECT sizing_spec FROM research_runs WHERE id = ?;",
                (run_id,),
            ).fetchone()
    if row is None:  # no such run at all: the same "unknown id" case every other get_run_* raises on
        raise RunNotFound(f"no research_runs row with id={run_id}")
    if row["sizing_spec"] is None:  # the run exists but never recorded a resolved sizing spec
        raise SizingSpecNotRecordedError(
            f"run_id={run_id} has no recorded sizing_spec; cannot derive its lead-in bar count"
        )
    spec = json.loads(row["sizing_spec"])  # the dataclasses.asdict(SizingSpec) JSON the entrypoints wrote
    return int(spec["estimator_window"])  # the exact field clip_frame_with_lead_in reads to size a lead-in


def _percentile(sorted_vals: list, q: float):
    """The q-quantile (0..1) of a PRE-SORTED non-empty list, linear interpolation (the numpy 'linear' method).

    Pure Python: the backend is numpy-free and engine-neutral, and the cell counts a universe summary
    aggregates over are bounded (the R-CEIL size cap), so an O(n log n) sort + O(1) index is cheap.
    """
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)  # the fractional rank
    lo = int(pos)
    if lo + 1 >= len(sorted_vals):  # q == 1.0 (or rounding) -> the top value
        return sorted_vals[-1]
    return sorted_vals[lo] + (pos - lo) * (sorted_vals[lo + 1] - sorted_vals[lo])  # interpolate the two straddling values


def _summarize_cells(cells: list[dict]) -> dict:
    """The distribution-led R-SUMMARY (AD-M5-15): a per-metric distribution over the run's cells, on read.

    Leads with the DISTRIBUTION (count, min, p25, median, p75, max, mean across the tickers/combos), NOT a
    single winner — the multiple-testing-honesty rule (R-SUMMARY). None-safe: a nullable ratio is excluded
    from its own distribution (so `n` is the non-null count) but every cell still counts toward cell_count.
    Also reports pct_profitable (the share of cells with a positive total return). Computed on read; a
    materialized cube is deferred until measured slow (the #168 warehouse-not-cube discipline).
    """
    metrics: dict[str, dict] = {}
    # Summarize the 8 per-cell metrics PLUS the 2 Track-B distribution scalars (skew/kurtosis), so the read
    # carries a median-skew (and median-kurtosis) distribution. As the metric set grows (C-metrics), extend
    # the column tuples, not this loop.
    for m in (_RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS):
        vals = sorted(c[m] for c in cells if c[m] is not None)  # the metric's non-null values, ascending
        if not vals:  # every cell's value is NULL (e.g. an all-undefined ratio) — an honest empty distribution
            metrics[m] = {"n": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None, "mean": None}
            continue
        metrics[m] = {
            "n": len(vals),
            "min": vals[0], "max": vals[-1],
            "p25": _percentile(vals, 0.25), "median": _percentile(vals, 0.50), "p75": _percentile(vals, 0.75),
            "mean": sum(vals) / len(vals),
        }
    returns = [c["total_return"] for c in cells if c["total_return"] is not None]  # for the % profitable read
    pct_profitable = (sum(1 for r in returns if r > 0) / len(returns)) if returns else None
    return {"metrics": metrics, "pct_profitable": pct_profitable}  # cell_count is on the top-level read, not here


def get_run_cells(run_id: int) -> dict:
    """The universe-sweep results read (R-GRID + R-SUMMARY, AD-M5-15): every per-cell metric row for the run,
    plus a distribution-led aggregate over the cells.

    The grid is a prefix read on run_id over research_run_cells (the AD-M5-2 concatenated index), each cell's
    8 metrics joined to its variation params (research_run_variations); ordered best-Sharpe-first (NULLs last).
    The summary is computed on read (the #168 warehouse-not-cube discipline; materialize only when measured
    slow). The page is one sweep; the cell count is bounded by the R-CEIL cap, so returning all cells (no
    server paging at the minimal-slice scope — N tickers x combos) is honest. Raises RunNotFound if unknown.
    """
    with get_conn() as conn:
        run = conn.execute("SELECT id FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
        if run is None:
            raise RunNotFound(f"run {run_id} not found")
        cell_rows = conn.execute(
            "SELECT c.variation_id, c.ticker, " + ", ".join(f"c.{m}" for m in (_RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS)) + ", v.param_json "
            "FROM research_run_cells c "
            "LEFT JOIN research_run_variations v ON v.run_id = c.run_id AND v.variation_id = c.variation_id "
            "WHERE c.run_id = ? AND c.row_scope = 'cell' "
            "ORDER BY (c.sharpe_ratio IS NULL), c.sharpe_ratio DESC, c.ticker ASC;",  # best Sharpe first, NULLs last
            (run_id,),
        ).fetchall()
    cells = [dict(r) for r in cell_rows]
    return {
        "run_id": run_id,
        "cell_count": len(cells),
        "member_count": len({c["ticker"] for c in cells}),       # distinct tickers
        "variation_count": len({c["variation_id"] for c in cells}),  # distinct parameter combinations
        "cells": cells,                                          # the per-ticker (x combo) grid rows
        "summary": _summarize_cells(cells),                      # the distribution-led aggregate
    }


def get_run_cell(run_id: int, variation_id: int, ticker: str, row_scope: str = "cell") -> dict | None:
    """Point-lookup ONE (variation_id, ticker) cell of a sweep, or None if absent (M5 C-metrics §11 item 2).

    The single-cell analogue of get_run_cells (whose only reader is the whole-run plural). Backs the
    /full-metrics CELL route's 404 guard and supplies the cell's combo (param_json) for the re-run dispatch.
    The research_run_cells PK (run_id, variation_id, ticker, row_scope) supports the exact point lookup;
    returns the 8 metrics + the 2 Track-B distribution scalars joined to the cell's variation params, or None.
    NEVER raises on a missing cell (a point lookup, not a collection) -- the route turns None into a 404.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT c.variation_id, c.ticker, "
            + ", ".join(f"c.{m}" for m in (_RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS))
            + ", v.param_json "
            "FROM research_run_cells c "
            "LEFT JOIN research_run_variations v ON v.run_id = c.run_id AND v.variation_id = c.variation_id "
            "WHERE c.run_id = ? AND c.variation_id = ? AND c.ticker = ? AND c.row_scope = ?;",
            (run_id, variation_id, ticker, row_scope),
        ).fetchone()
    return dict(row) if row is not None else None


def get_full_metrics_cache(run_id: int, variation_id: int, ticker: str, formula_version: str) -> dict | None:
    """Read a cached full-metrics payload by its exact identity, or None on a miss (M5 C-metrics §2.2).

    The lazy, immutable warehouse-not-cube accelerator (#168 / DDIA Ch 4): a payload is computed once on first
    open and cached under (run_id, variation_id, ticker, formula_version); a code/formula bump changes
    formula_version (resolve_formula_version), so the old row is a natural cache MISS and the page recomputes
    rather than serving a stale number (the one real staleness vector, C2 §3). Single-run sentinel is
    (variation_id=-1, ticker=''). Returns {"payload": <dict>, "computed_at": <int>} or None.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_json, computed_at FROM research_full_metrics_cache "
            "WHERE run_id = ? AND variation_id = ? AND ticker = ? AND formula_version = ?;",
            (run_id, variation_id, ticker, formula_version),
        ).fetchone()
    if row is None:
        return None  # a cache miss (no row, or only rows under a different formula_version) -> the route recomputes
    return {"payload": json.loads(row["payload_json"]), "computed_at": row["computed_at"]}


def get_full_metrics_cache_by_cell(run_id: int, variation_id: int, ticker: str) -> "dict | None":
    """Lane JC (candidate chain link spec, L-1 rows 11/12 and section 3's own "graded_formula_version
    = the replay key's fourth field"): the CACHE ROW AT THIS CELL'S CURRENT formula_version only
    (Amendment 1, R2, a SHOULD-FIX at the lead's read 1). This used to read the newest row under ANY
    formula_version, on the theory that the route never knows in advance which formula_version an
    existing replay was stored under -- but spec row 11 names the FOUR-field key, and E-1's own
    ABSENT row says "a new formula_version" answers replay-absent, not "the newest row whatever its
    key". After FULLMETRICS_FORMULA_VERSION bumps (or the strategy/build_provenance changes), a
    replay stored under the OLD key must read as though none exists at the current one: the grade
    route then answers replay-absent and the replay route never answers replay-exists for it.
    Resolves "current" the same way _resolve_formula_version (routes/judge.py) does, duplicated here
    rather than imported from that module so this stays a pure researchdb call with the SAME 3-arg
    shape every existing caller (including three probes that call it directly) already uses.
    Returns {"payload", "computed_at", "formula_version"} or None on a miss (no row at this cell at
    the run's own current formula_version; an older row under a stale key is also a miss).
    """
    run = get_run(run_id)
    if run is None:
        return None
    catalog._ensure_strategies_on_path()
    from strategies.helpers.full_metrics import resolve_formula_version  # noqa: PLC0415

    current_formula_version = resolve_formula_version(run["strategy_version"], run["build_provenance"] or "")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT formula_version, payload_json, computed_at FROM research_full_metrics_cache "
            "WHERE run_id = ? AND variation_id = ? AND ticker = ? AND formula_version = ? "
            "ORDER BY computed_at DESC LIMIT 1;",
            (run_id, variation_id, ticker, current_formula_version),
        ).fetchone()
    if row is None:
        return None
    return {"payload": json.loads(row["payload_json"]), "computed_at": row["computed_at"],
            "formula_version": row["formula_version"]}


def put_full_metrics_cache(run_id: int, variation_id: int, ticker: str, formula_version: str,
                           payload_json: str, now: int | None = None) -> None:
    """Write (INSERT OR REPLACE) a worker-computed full-metrics payload into the cache (M5 C-metrics §2.2).

    INSERT OR REPLACE so a retried compute job is idempotent on the PK (run_id, variation_id, ticker,
    formula_version). payload_json is the already-serialized full-metric JSON the worker computed (stored
    verbatim); a non-string would be a programming error and is the caller's contract to json.dumps. The row
    CASCADE-deletes with the run (the schema FK), the recomputable-accelerator discipline.
    """
    ts = int(time.time()) if now is None else now  # the codebase now-convention (mirrors complete_run et al.)
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO research_full_metrics_cache "
            "    (run_id, variation_id, ticker, formula_version, payload_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (run_id, variation_id, ticker, formula_version, payload_json, ts),
        )


# Lane JC (candidate chain link spec 2026-09-23, section 1.4 "Overwrite rule" and section 7 row
# B-3): the overwrite guard's own two counters, process-local and reset at every start (the
# JUDGE_ROUTE_COUNTERS idiom this codebase already uses). "replay_payload_overwrite_refused"
# moves every time a plain payload is refused the chance to clobber a stored row that carries a
# real replay object; "full_metrics_cache_payload_unreadable" moves every time a stored payload
# does not parse as JSON and is therefore replaced (it could never have been read as a replay
# either way, so there is nothing to protect). Registry rows: docs/units/JC/monitor_rows.md.
# "replay_recheck_failed" (B-2) is named in the spec as living "in the judge route's dict", but
# the write it counts happens on this module's own guarded write, which is the only place that
# can see the failure; it is kept here, beside its two siblings, and drafted under that name in
# docs/units/JC/monitor_rows.md so a reader following the spec's own wording still finds it.
FULL_METRICS_CACHE_COUNTERS = {
    "replay_payload_overwrite_refused": 0,
    "full_metrics_cache_payload_unreadable": 0,
    "replay_recheck_failed": 0,
}


class ReplayRecheckFailed(Exception):
    """Raised by put_full_metrics_cache_guarded when an incoming payload's own `replay` object
    fails the backend's pre-write re-check (spec 1.4 step 4): either an artifact file it names is
    absent from the store, or a `cell_metrics` value it carries no longer agrees with the stored
    research_run_cells row under the `exact` tolerance (1.5). The caller (the worker route)
    answers 409 replay-recheck-failed with this exception's own message and writes no cache row."""


# The two W-3 words this module owns (JC-17): each defined exactly once, here, as a bare string
# literal, and referenced everywhere else by name so no use site re-types the text.
# ONE stated exception (round 5 fix, claims-auditor finding 3): WORD_REPLAY_NOT_REPRODUCED's own
# text is hand-retyped, as a bare word with no surrounding quote marks in the source comment here
# (so this sentence itself never becomes a second quoted-literal definition), inside
# research_lab/docker/research-runner/compute_metrics_entrypoint.py. That file runs INSIDE the
# research-runner docker image and never imports dashboard.backend (the same cross-deployable
# boundary this build already states elsewhere for _REPLAY_CELL_METRIC_COLUMNS), so it cannot
# reach this constant by import and the retyped copy there is structurally forced, not a drift.
WORD_REPLAY_RECHECK_FAILED = "replay-recheck-failed"
WORD_REPLAY_NOT_REPRODUCED = "replay-not-reproduced"


def _replay_values_match_exact(a, b) -> bool:
    """The `exact` tolerance (spec 1.5): every compared column equal, NULL equal only to NULL."""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _recheck_replay_payload(conn, run_id: int, variation_id: int, ticker: str, replay: dict) -> None:
    """Spec 1.4 step 4's own re-check, run inside the SAME transaction as the write (1.4: "The
    read of the stored row and the write sit in the one BEGIN IMMEDIATE transaction ... so no
    second write can land between them"). Raises ReplayRecheckFailed naming the first mismatch
    found; returns None (silently) on a full match."""
    artifacts = (replay or {}).get("artifacts") or {}
    for label in ("equity", "trades"):
        entry = artifacts.get(label)
        if not entry or not entry.get("name") or not entry.get("sha256"):
            raise ReplayRecheckFailed(
                f"{WORD_REPLAY_RECHECK_FAILED}: the replay payload's own artifacts.{label} is incomplete "
                f"for run {run_id}"
            )
        if not artifact_store.store.exists(run_id, entry["name"]):
            raise ReplayRecheckFailed(
                f"{WORD_REPLAY_RECHECK_FAILED}: artifact {entry['name']!r} is not in the store for run {run_id}"
            )
    cell_metrics = (replay or {}).get("cell_metrics") or {}
    cell_row = conn.execute(
        "SELECT * FROM research_run_cells WHERE run_id = ? AND variation_id = ? AND ticker = ? "
        "  AND row_scope = 'cell';",
        (run_id, variation_id, ticker),
    ).fetchone()
    if cell_row is None:
        raise ReplayRecheckFailed(
            f"{WORD_REPLAY_RECHECK_FAILED}: no research_run_cells row at (run_id={run_id}, "
            f"variation_id={variation_id}, ticker={ticker!r}) to re-check the replay against"
        )
    cell_dict = dict(cell_row)
    # Amendment 1, R6 (read 1's own SHOULD-FIX): this used to loop over the INCOMING cell_metrics
    # keys only, so an EMPTY or PARTIAL cell_metrics passed vacuously (nothing to disagree with).
    # Spec 1.4 step 4 says "cell_metrics still equals the stored row" -- all TEN L-2 columns,
    # present and equal, never a subset. Missing columns are named together, the same "read
    # together rather than stopping at the first" idiom judge_checks.check_9_controls uses.
    missing = [c for c in (_RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS) if c not in cell_metrics]
    if missing:
        raise ReplayRecheckFailed(
            f"{WORD_REPLAY_RECHECK_FAILED}: cell_metrics is missing {missing!r} of the ten L-2 "
            f"columns for run {run_id} (an empty or partial cell_metrics never passes this check)"
        )
    for column in (_RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS):
        value = cell_metrics[column]
        if not _replay_values_match_exact(value, cell_dict.get(column)):
            raise ReplayRecheckFailed(
                f"{WORD_REPLAY_RECHECK_FAILED}: cell_metrics={cell_metrics!r} "
                f"expected_cell_metrics={ {k: cell_dict.get(k) for k in (_RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS)} !r} "
                f"(column {column!r} disagrees for run {run_id})"
            )


def put_full_metrics_cache_guarded(run_id: int, variation_id: int, ticker: str, formula_version: str,
                                   payload: dict, now: int | None = None) -> None:
    """The replay-aware sibling of put_full_metrics_cache (spec 1.4 "Who and when" step 4, and the
    overwrite rule): re-checks a payload carrying a `replay` object BEFORE writing (raises
    ReplayRecheckFailed on any disagreement, writing nothing), and refuses to let a PLAIN payload
    (no `replay` object) overwrite a stored row that carries one (the overwrite guard), replacing
    only an UNREADABLE stored payload. "Carries `replay`" means, here as in the spec, a `replay`
    key whose value is a JSON object; a missing key or a `null` is no replay.
    """
    ts = int(time.time()) if now is None else now
    replay = payload.get("replay") if isinstance(payload, dict) else None
    incoming_carries_replay = isinstance(replay, dict)
    with transaction() as conn:
        if incoming_carries_replay:
            # 1.4 step 4: re-check BEFORE writing, on the same connection/transaction as the write.
            try:
                _recheck_replay_payload(conn, run_id, variation_id, ticker, replay)
            except ReplayRecheckFailed:
                FULL_METRICS_CACHE_COUNTERS["replay_recheck_failed"] += 1
                raise
        else:
            # The overwrite guard: a plain payload never clobbers a stored row that carries a real
            # replay object (a replay is a superset, so only a replay payload may replace one).
            existing = conn.execute(
                "SELECT payload_json FROM research_full_metrics_cache "
                "WHERE run_id = ? AND variation_id = ? AND ticker = ? AND formula_version = ?;",
                (run_id, variation_id, ticker, formula_version),
            ).fetchone()
            if existing is not None:
                try:
                    existing_payload = json.loads(existing["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    # An unreadable stored payload holds no readable replay either: nothing to
                    # protect, so the plain write proceeds and this is counted (1.4, NULL/ABSENT).
                    FULL_METRICS_CACHE_COUNTERS["full_metrics_cache_payload_unreadable"] += 1
                else:
                    if isinstance(existing_payload, dict) and isinstance(existing_payload.get("replay"), dict):
                        FULL_METRICS_CACHE_COUNTERS["replay_payload_overwrite_refused"] += 1
                        return  # keep the existing replay row unchanged; never a write error
        conn.execute(
            "INSERT OR REPLACE INTO research_full_metrics_cache "
            "    (run_id, variation_id, ticker, formula_version, payload_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (run_id, variation_id, ticker, formula_version, json.dumps(payload), ts),
        )


# M5 Track C (C-metrics §2 / C2 §7): the full-metrics compute ORCHESTRATION the /full-metrics route drives.
# The metrics are computed in the WORKER (the sole vbt home, C2 §1); the backend assembles the FROZEN compute
# inputs, enqueues the job (deduped + offline-aware), and serves the cache. The compute job kind:
JOB_KIND_COMPUTE_METRICS = "compute_run_metrics"  # the worker handler that computes one run's / cell's full metrics
# the single-run sentinel cell key (a single run has no variation/ticker — never collides with a real cell):
_FM_SINGLE_VARIATION = -1
_FM_SINGLE_TICKER = ""
# the universe-AGGREGATE sentinel ticker (M5 Track C, the aggregate spec brief §4): an aggregate is keyed
# (run_id, variation_id>=0, "*", formula_version) — "*" cannot collide with a real bare ticker (eodhd tickers
# are alphanumeric, never "*") nor with the single-run sentinel "", so the three cache key-spaces stay disjoint
# in the one research_full_metrics_cache table (no schema change; additive — DDIA Ch 5 / Ch 4 warehouse-not-cube).
_FM_AGGREGATE_TICKER = "*"


def build_single_metrics_compute_payload(run_id: int, formula_version: str) -> dict:
    """Assemble the worker compute_run_metrics payload for a SINGLE run (C2 §2.1, derive-on-read).

    The worker fetches equity.parquet + trades.parquet over HTTP (named in `artifacts`) and reads the
    persisted F6 + cost scalars from here (no re-run). Raises RunNotFound if the run has no stats row (a
    not-succeeded run; the route's status guard makes this unreachable in practice)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT max_gross_exposure, turnover, total_fees_paid, total_slippage_cost "
            "FROM research_run_stats WHERE run_id = ?;", (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id} has no stats row (cannot derive full metrics)")
    return {
        "mode": "single",
        "target_run_id": run_id, "variation_id": _FM_SINGLE_VARIATION, "ticker": _FM_SINGLE_TICKER,
        "formula_version": formula_version,
        "artifacts": ["equity.parquet", "trades.parquet"],  # the worker GETs these from the artifact store
        "f6": {"max_gross_exposure": row["max_gross_exposure"], "turnover": row["turnover"],
               "total_fees_paid": row["total_fees_paid"], "total_slippage_cost": row["total_slippage_cost"]},
    }


def parse_recorded_sizing_spec(run_id: int, text: str) -> dict:
    """Read a run's recorded sizing_spec text back as a dict of exactly SizingSpec's five fields (lane P2f).

    The one reader both compute payload builders call (the reserve exam reuses it). Raises
    RecordedSizingSpecUnreadable, naming the run, when the text is empty, not JSON, not a JSON object,
    or its keys are not exactly the field names of SizingSpec: an empty object "{}" is refused, never
    filled in from defaults. SizingSpec is imported lazily through the same path extract_sizing_method
    uses, so the one true field list lives in sizing.py and is never restated here.
    """
    catalog._ensure_strategies_on_path()  # idempotent: makes the lazy import below self-sufficient
    import dataclasses  # noqa: PLC0415 (stdlib, kept next to its only use)
    from research_lab.strategies.helpers.sizing import SizingSpec  # lazy: stdlib-only module, no vbt or pandas

    expected = {f.name for f in dataclasses.fields(SizingSpec)}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise RecordedSizingSpecUnreadable(
            f"recorded-sizing-spec-unreadable: run {run_id}'s sizing_spec is not JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise RecordedSizingSpecUnreadable(
            f"recorded-sizing-spec-unreadable: run {run_id}'s sizing_spec is not a JSON object")
    if set(parsed) != expected:
        raise RecordedSizingSpecUnreadable(
            f"recorded-sizing-spec-unreadable: run {run_id}'s sizing_spec has keys {sorted(parsed)}, "
            f"expected exactly {sorted(expected)}")
    return parsed


def _recorded_sizing_keys(run_id: int, sizing_method: "str | None", sizing_spec: "str | None") -> dict:
    """The "sizing_method" and "sizing_spec" payload keys for a run's compute payload (lane P2f, P2j).

    Both columns NULL (a run from an image older than #376 ruling 4): SizingSpecNotRecordedError naming the run,
    because no default sizing is used (DECISIONS #479 item 9, #488 item 9, #489 item 4).
    sizing_method NULL with sizing_spec set: only "sizing_spec", the stored text, after parse_recorded_sizing_spec.
    sizing_method set: both keys, the stored text unchanged, after parse_recorded_sizing_spec has checked it;
    sizing_spec NULL there raises SizingSpecNotRecordedError naming the run.
    """
    if sizing_method is None:
        if sizing_spec is None:
            raise SizingSpecNotRecordedError(
                f"run {run_id} has no recorded sizing, so it cannot be replayed with the sizing it used; "
                "no default sizing is used")
        parse_recorded_sizing_spec(run_id, sizing_spec)
        return {"sizing_spec": sizing_spec}
    if sizing_spec is None:
        raise SizingSpecNotRecordedError(
            f"run_id={run_id} recorded sizing_method {sizing_method!r} but no sizing_spec; "
            "cannot replay it with the sizing it used")
    parse_recorded_sizing_spec(run_id, sizing_spec)
    return {"sizing_method": sizing_method, "sizing_spec": sizing_spec}


def build_cell_metrics_compute_payload(run_id: int, variation_id: int, ticker: str, formula_version: str) -> dict:
    """Assemble the worker compute_run_metrics payload for ONE cell (C2 §7, the three-way re-run).

    Reads the FROZEN registration (param_mode + window — never the MUTABLE research_datasets universe row,
    C2 fold 4) + the FROZEN run_sweep job payload (source_code, settings, the resolved member, params_decl,
    init_cash) + the cell's combo (param_json). The benchmark freeze (benchmark_dataset_id -> the member's
    file path) is wired by C-bench (left absent here -> the benchmark group renders honest absence). Raises
    RunNotFound on any missing piece (a clear error, never a silent partial)."""
    with get_conn() as conn:
        reg = conn.execute(
            "SELECT param_mode, window_start_ts, window_end_ts, "
            "       benchmark_ticker, benchmark_dataset_id, benchmark_sha256 "
            "FROM research_sweep_registrations WHERE run_id = ?;", (run_id,)).fetchone()
        if reg is None:
            raise RunNotFound(f"run {run_id} has no sweep registration (cannot re-run a cell)")
        recorded_lead_in = conn.execute(  # P2e1: the lead-in bars this cell's own run used (None = not recorded)
            "SELECT lead_in_bars FROM research_run_funnel "
            "WHERE run_id = ? AND variation_id = ? AND ticker = ? AND row_scope = 'cell';",
            (run_id, variation_id, ticker)).fetchone()
        job = conn.execute(
            "SELECT j.payload_json FROM research_jobs j JOIN research_runs r ON r.job_id = j.id "
            "WHERE r.id = ?;", (run_id,)).fetchone()
        run_sizing = conn.execute(  # P2f: the sizing this run recorded (both NULL for a run from an older image)
            "SELECT sizing_method, sizing_spec FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
        # C-bench: resolve the FROZEN benchmark dataset to its container file path (the entrypoint loads it
        # for the benchmark group). Only when a benchmark was frozen AND its dataset is ready with a file;
        # otherwise the benchmark group renders honest absence (no fabricated benchmark).
        benchmark = None
        if reg["benchmark_dataset_id"] is not None:
            bds = conn.execute(
                "SELECT symbol, file_path, sha256, status FROM research_datasets WHERE id = ?;",
                (reg["benchmark_dataset_id"],)).fetchone()
            if bds is not None and bds["status"] == "ready" and bds["file_path"]:
                benchmark = {"dataset_path": bds["file_path"], "ticker": reg["benchmark_ticker"],
                             "dataset_sha256": reg["benchmark_sha256"] or bds["sha256"]}
    if job is None or not job["payload_json"]:
        raise RunNotFound(f"run {run_id} has no frozen job payload (cannot re-run a cell)")
    frozen = json.loads(job["payload_json"])  # the completed run_sweep job's frozen payload (never cleared)
    cell = get_run_cell(run_id, variation_id, ticker)  # the cell (for its combo); None if the id is unknown
    if cell is None:
        raise RunNotFound(f"cell for combination {variation_id}, ticker {ticker} not found for run {run_id}")
    member = next((m for m in (frozen.get("universe") or []) if m.get("symbol") == ticker), None)
    if member is None:  # the cell's member must be in the frozen universe (it produced the cell)
        raise RunNotFound(f"member {ticker!r} not in run {run_id}'s frozen universe")
    sizing_keys = _recorded_sizing_keys(run_id, run_sizing["sizing_method"], run_sizing["sizing_spec"])  # P2f
    return {
        "mode": "cell",
        "target_run_id": run_id, "variation_id": variation_id, "ticker": ticker,
        "formula_version": formula_version,
        "param_mode": reg["param_mode"],                       # the FROZEN three-way discriminator
        "source_code": frozen["source_code"],                  # the FROZEN strategy source (AD-M5-10)
        "member": {"dataset_path": member["dataset_path"], "symbol": member["symbol"],
                   "fees": member["fees"], "slippage": member["slippage"],
                   "dataset_sha256": member.get("dataset_sha256"),
                   # P2e1: the lead-in the stored cell used, so the replay clips the same bars; None when the
                   # row or the value is absent (a run stored before P2g): that member replays by the bar rule
                   "lead_in_bars": None if recorded_lead_in is None else recorded_lead_in["lead_in_bars"]},
        "combo": json.loads(cell["param_json"]) if cell.get("param_json") else {},  # the cell's combo ({} for zero)
        "params_decl": frozen.get("params_decl"),              # [{name,type}] for inline; None otherwise
        "window_start_ts": reg["window_start_ts"], "window_end_ts": reg["window_end_ts"],  # the FROZEN window
        "settings": frozen.get("settings", []),                # the FROZEN run-level settings
        "init_cash": frozen.get("init_cash", 10_000.0),
        "benchmark": benchmark,                                # the FROZEN benchmark (None -> honest absence)
        **sizing_keys,                                         # P2f: the recorded sizing (absent when both NULL)
    }


def build_aggregate_metrics_compute_payload(run_id: int, variation_id: int, formula_version: str) -> dict:
    """Assemble the worker compute payload for the universe AGGREGATE at one variation (aggregate spec brief §4).

    Mirrors build_cell_metrics_compute_payload, differing ONLY in: the FULL frozen universe member list (not one
    member), and the combo read from the VARIATION (research_run_variations.param_json), applied to every member.
    Reuses the SAME frozen-registration read (param_mode + window + the benchmark resolve-to-file) and the SAME
    RunNotFound fail-loud discipline. Raises RunNotFound on a missing registration / job payload / unknown variation."""
    with get_conn() as conn:
        reg = conn.execute(
            "SELECT param_mode, window_start_ts, window_end_ts, "
            "       benchmark_ticker, benchmark_dataset_id, benchmark_sha256 "
            "FROM research_sweep_registrations WHERE run_id = ?;", (run_id,)).fetchone()
        if reg is None:
            raise RunNotFound(f"run {run_id} has no sweep registration (cannot build the aggregate)")
        job = conn.execute(
            "SELECT j.payload_json FROM research_jobs j JOIN research_runs r ON r.job_id = j.id "
            "WHERE r.id = ?;", (run_id,)).fetchone()
        run_sizing = conn.execute(  # P2f: the sizing this run recorded (both NULL for a run from an older image)
            "SELECT sizing_method, sizing_spec FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
        var = conn.execute(  # the chosen variation's combo (applied to EVERY member); the aggregate is per-combo
            "SELECT param_json FROM research_run_variations WHERE run_id = ? AND variation_id = ?;",
            (run_id, variation_id)).fetchone()
        if var is None:
            raise RunNotFound(f"run {run_id} has no combination {variation_id} (cannot build the aggregate)")
        recorded_lead_ins = {  # P2e1: each member's own recorded lead-in at this variation (ticker -> bars or None)
            r["ticker"]: r["lead_in_bars"] for r in conn.execute(
                "SELECT ticker, lead_in_bars FROM research_run_funnel "
                "WHERE run_id = ? AND variation_id = ? AND row_scope = 'cell';", (run_id, variation_id)).fetchall()}
        # the SAME benchmark resolve-to-file as a cell (None -> the benchmark group renders honest absence)
        benchmark = None
        if reg["benchmark_dataset_id"] is not None:
            bds = conn.execute(
                "SELECT symbol, file_path, sha256, status FROM research_datasets WHERE id = ?;",
                (reg["benchmark_dataset_id"],)).fetchone()
            if bds is not None and bds["status"] == "ready" and bds["file_path"]:
                benchmark = {"dataset_path": bds["file_path"], "ticker": reg["benchmark_ticker"],
                             "dataset_sha256": reg["benchmark_sha256"] or bds["sha256"]}
    if job is None or not job["payload_json"]:
        raise RunNotFound(f"run {run_id} has no frozen job payload (cannot build the aggregate)")
    frozen = json.loads(job["payload_json"])  # the completed run_sweep job's frozen payload (never cleared)
    universe = frozen.get("universe") or []
    if not universe:  # a sweep with no frozen member list cannot be aggregated (a clear error, never a silent empty)
        raise RunNotFound(f"run {run_id} has no frozen universe members (cannot build the aggregate)")
    members = [{"dataset_path": m["dataset_path"], "symbol": m["symbol"],
                "fees": m["fees"], "slippage": m["slippage"], "dataset_sha256": m.get("dataset_sha256"),
                "lead_in_bars": recorded_lead_ins.get(m["symbol"])}  # P2e1: None -> that member replays by the bar rule
               for m in universe]
    sizing_keys = _recorded_sizing_keys(run_id, run_sizing["sizing_method"], run_sizing["sizing_spec"])  # P2f
    return {
        "mode": "aggregate",
        "target_run_id": run_id, "variation_id": variation_id, "ticker": _FM_AGGREGATE_TICKER,
        "formula_version": formula_version,
        "param_mode": reg["param_mode"],                       # the FROZEN three-way discriminator (per-member re-run)
        "source_code": frozen["source_code"],                  # the FROZEN strategy source (AD-M5-10)
        "members": members,                                    # the FULL frozen universe (each member re-run once)
        "combo": json.loads(var["param_json"]) if var["param_json"] else {},  # the variation's combo ({} for zero)
        "params_decl": frozen.get("params_decl"),              # [{name,type}] for inline; None otherwise
        "window_start_ts": reg["window_start_ts"], "window_end_ts": reg["window_end_ts"],  # the FROZEN window
        "settings": frozen.get("settings", []),                # the FROZEN run-level settings
        "init_cash": frozen.get("init_cash", 10_000.0),
        "benchmark": benchmark,                                # the FROZEN benchmark (None -> honest absence)
        **sizing_keys,                                         # P2f: the recorded sizing (absent when both NULL)
    }


def any_worker_online(stale_after: int = 120, now: int | None = None) -> bool:
    """True iff at least one worker reported online within `stale_after` seconds (the compute-availability check).

    The compute-on-demand path depends on a live worker (C2 §4). When none is online the /full-metrics route
    returns the honest 'unavailable: the research worker is offline' state, never a number."""
    now = int(time.time()) if now is None else now
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM research_workers WHERE status = 'online' AND last_seen >= ?;",
            (now - stale_after,)).fetchone()
    return row["c"] > 0


def latest_full_metrics_job(run_id: int, variation_id: int, ticker: str, formula_version: str) -> dict | None:
    """The most recent compute_run_metrics job for this exact cache key, or None (the route's in-flight read).

    Returns {"id", "status", "error"}. The route uses it to avoid double-enqueue (a pending/picked_up job is
    in flight) and to surface honest absence (a 'failed' job with no cache -> 'unavailable' + the reason,
    breaking the re-compute loop a permanently-failing cell -- e.g. a purged dataset -- would otherwise cause).
    Python-side payload match (no json_extract dependency); there are few in-flight compute jobs."""
    key = (run_id, variation_id, ticker, formula_version)
    with get_conn() as conn:
        # Filter to the ACTIVE/FAILED states only (never the unbounded succeeded history): a succeeded compute
        # job always wrote the cache (the handler raise_for_status's the cache POST), so a cache MISS means the
        # job for this key is in flight, failed, or absent — the only states the route acts on. The partial
        # index ix_jobs_compute_active backs this (and enqueue's dedup) so it stays O(active jobs), not O(all).
        rows = conn.execute(
            "SELECT id, status, error, payload_json FROM research_jobs "
            "WHERE kind = ? AND status IN ('pending', 'picked_up', 'failed') ORDER BY id DESC;",
            (JOB_KIND_COMPUTE_METRICS,)).fetchall()
    for r in rows:
        try:
            pj = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if (pj.get("target_run_id"), pj.get("variation_id"), pj.get("ticker"),
                pj.get("formula_version")) == key:
            return {"id": r["id"], "status": r["status"], "error": r["error"]}
    return None


def compute_job_in_flight_for_key(run_id: int, variation_id: int, ticker: str, formula_version: str) -> "int | None":
    """True (the job's own id) iff a compute_run_metrics job, replay or plain, is pending or
    picked_up for this exact cache key right now. Lane JC's replay route checks this BEFORE ever
    building a compute payload (build_cell_metrics_compute_payload can fail for reasons unrelated
    to an in-flight job, e.g. a stale frozen registration, and an in-flight refusal must never be
    shadowed by that unrelated failure); enqueue_replay_metrics_job re-checks the same fact
    atomically inside its own transaction, so a race between this read and the enqueue still
    raises ComputeJobInFlight rather than double-queuing.
    """
    key = (run_id, variation_id, ticker, formula_version)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM research_jobs "
            "WHERE kind = ? AND status IN ('pending', 'picked_up');",
            (JOB_KIND_COMPUTE_METRICS,)).fetchall()
    for r in rows:
        try:
            pj = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if (pj.get("target_run_id"), pj.get("variation_id"), pj.get("ticker"),
                pj.get("formula_version")) == key:
            return r["id"]
    return None


def latest_replay_job_for_key(run_id: int, variation_id: int, ticker: str, formula_version: str) -> "dict | None":
    """Amendment 1, A2 (spec section 4 outcome 3, E-1's own REFUSED state): the NEWEST
    compute_run_metrics job at this exact four-field key WHOSE PAYLOAD CARRIES replay: true (a
    plain job, replay absent or false, never blocks a replay -- outcome 3's own words). Only
    pending/picked_up/failed rows are read, the same active/failed scope latest_full_metrics_job
    uses, ordered by id DESC so the first match is the newest. Returns {"id", "status", "error"} or
    None when no replay job has ever been attempted at this key (an ABSENT key, spec E-1).
    """
    key = (run_id, variation_id, ticker, formula_version)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, status, error, payload_json FROM research_jobs "
            "WHERE kind = ? AND status IN ('pending', 'picked_up', 'failed') ORDER BY id DESC;",
            (JOB_KIND_COMPUTE_METRICS,)).fetchall()
    for r in rows:
        try:
            pj = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if pj.get("replay") is not True:
            continue  # A plain job at the same key never blocks a replay (outcome 3's own words).
        if (pj.get("target_run_id"), pj.get("variation_id"), pj.get("ticker"),
                pj.get("formula_version")) == key:
            return {"id": r["id"], "status": r["status"], "error": r["error"]}
    return None


class ComputeJobInFlight(Exception):
    """Raised by enqueue_replay_metrics_job (lane JC) when ANY compute_run_metrics job, replay or
    plain, is already queued or running for the exact same cache key. Unlike
    enqueue_full_metrics_job's own dedup (which silently hands back an in-flight job's id), the
    replay route must never silently reuse or silently queue a second one: spec section 4's own
    closing paragraph: "a plain job queued between the check and the enqueue would silently
    swallow the replay"; the route answers 409 compute-in-flight and enqueues nothing either way.
    """


def query_judge_replay_jobs_unfinished(now: int | None = None, stale_after: int = 120) -> "list[int]":
    """Lane JC monitor row B-4: compute_run_metrics job ids whose payload carries replay: true
    that are 'failed' with an error NOT beginning 'replay-not-reproduced:' (B-1 counts those
    separately), or 'picked_up' with a heartbeat older than any_worker_online's own stale_after.
    Expected empty; a stranded picked_up job's own exit is OWED to the missed-heartbeat sweep
    (research_lab/CLAUDE.md trap 7), which this lane does not build."""
    now = int(time.time()) if now is None else now
    ids: list[int] = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, status, error, payload_json, last_heartbeat FROM research_jobs "
            "WHERE kind = ? AND status IN ('failed', 'picked_up');",
            (JOB_KIND_COMPUTE_METRICS,)).fetchall()
    for r in rows:
        try:
            pj = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if pj.get("replay") is not True:
            continue
        if r["status"] == "failed":
            if not (r["error"] or "").startswith(f"{WORD_REPLAY_NOT_REPRODUCED}:"):
                ids.append(r["id"])
        elif r["status"] == "picked_up":
            hb = r["last_heartbeat"]
            if hb is None or hb < now - stale_after:
                ids.append(r["id"])
    return ids


def query_judge_replays_not_reproduced(limit: int = 500) -> "list[int]":
    """Lane JC monitor row B-1: compute_run_metrics job ids (replay: true) that failed because the
    container's own reproduction of a cell disagreed with the stored expected values (the entrypoint's
    own replay-not-reproduced error, spec 1.4 step 3). Uses SQL LIKE against WORD_REPLAY_NOT_REPRODUCED
    plus a colon, built as a bound parameter so the word's own definition stays the module's only
    bare string literal for it."""
    like_pattern = f"{WORD_REPLAY_NOT_REPRODUCED}:%"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM research_jobs WHERE kind = ? AND status = 'failed' "
            "  AND error LIKE ? ORDER BY id DESC LIMIT ?;",
            (JOB_KIND_COMPUTE_METRICS, like_pattern, limit)).fetchall()
    return [r["id"] for r in rows]


# Amendment 1, R5 (read 1's own SHOULD-FIX): "on a non-exam sweep" means a verdict whose run
# carries exactly one GRANTED run_linked row at stage 'optimise' -- a single-run verdict has no
# run_linked row at all (never a sweep), and a reserve-exam verdict's own granted link is stage
# 'reserve', not 'optimise'. -1 was never a verdict-table value (it is the single-run full-metrics
# CACHE sentinel, get_full_metrics_cache's own docstring), so filtering on it excluded nothing; this
# join is the real filter both B-5 and B-9 were missing.
_B5_B9_OPTIMISE_SWEEP_VERDICT_IDS_SQL = (
    "SELECT v.id AS id FROM research_judge_verdicts v "
    "WHERE EXISTS ("
    "  SELECT 1 FROM research_data_draws rl "
    "  JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
    "  WHERE rl.event_kind = 'run_linked' AND rl.run_id = v.run_id "
    "    AND rl.decision = 'granted' AND dr.stage = 'optimise'"
    ")"
)


def query_judge_cell_verdicts_without_replay() -> "list[int]":
    """Lane JC monitor row B-5: verdict rows on a non-exam sweep (Amendment 1, R5: a granted
    optimise-stage run_linked row, never a bare variation_id != -1 filter) with no cache row
    carrying `replay` at (run_id, variation_id, ticker, graded_formula_version). Realises the judge
    brief's own graded-without-a-replay monitor."""
    ids: list[int] = []
    with get_conn() as conn:
        verdict_rows = conn.execute(
            "SELECT v.id AS id, v.run_id AS run_id, v.variation_id AS variation_id, "
            "       v.ticker AS ticker, v.graded_formula_version AS graded_formula_version "
            "FROM research_judge_verdicts v "
            "WHERE v.id IN (" + _B5_B9_OPTIMISE_SWEEP_VERDICT_IDS_SQL + ");"
        ).fetchall()
        for v in verdict_rows:
            cache = conn.execute(
                "SELECT payload_json FROM research_full_metrics_cache "
                "WHERE run_id = ? AND variation_id = ? AND ticker = ? AND formula_version = ?;",
                (v["run_id"], v["variation_id"], v["ticker"], v["graded_formula_version"]),
            ).fetchone()
            carries_replay = False
            if cache is not None:
                try:
                    payload = json.loads(cache["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    payload = None
                carries_replay = isinstance(payload, dict) and isinstance(payload.get("replay"), dict)
            if not carries_replay:
                ids.append(v["id"])
    return ids


def query_judge_legacy_sweep_verdicts() -> "list[int]":
    """Lane JC monitor row B-9: verdict rows on a non-exam sweep (Amendment 1, R5: the same
    granted optimise-stage run_linked filter as B-5, above) whose dataset_sha256 equals the run's
    own research_runs.dataset_sha256 (the pre-JC path's own signature: variation 0, an arbitrary
    ticker, the declaration fingerprint rather than a member content sha). Before this fix a single-
    run verdict ALWAYS matched (its own dataset_sha256 IS the run's top-level column, by
    construction) and a reserve-exam verdict matched the same way, drowning the real signal.
    Expected to never grow after this lane merges."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT v.id AS id FROM research_judge_verdicts v "
            "JOIN research_runs r ON r.id = v.run_id "
            "WHERE v.dataset_sha256 = r.dataset_sha256 "
            "  AND v.id IN (" + _B5_B9_OPTIMISE_SWEEP_VERDICT_IDS_SQL + ");"
        ).fetchall()
    return [r["id"] for r in rows]


def enqueue_replay_metrics_job(payload: dict, now: int | None = None) -> int:
    """The replay-aware sibling of enqueue_full_metrics_job (spec section 4). Checked and inserted
    inside the SAME transaction, so no job can slip in between the check and the write: any
    compute_run_metrics job already pending or picked_up for this exact cache key (replay or
    plain) raises ComputeJobInFlight rather than being reused or shadowed; only when nothing is in
    flight does this insert the new (replay: true) job and return its id.
    """
    now = int(time.time()) if now is None else now
    key = (payload["target_run_id"], payload["variation_id"], payload["ticker"], payload["formula_version"])
    with transaction() as conn:
        for r in conn.execute(
                "SELECT id, payload_json FROM research_jobs "
                "WHERE kind = ? AND status IN ('pending', 'picked_up');",
                (JOB_KIND_COMPUTE_METRICS,)).fetchall():
            try:
                pj = json.loads(r["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if (pj.get("target_run_id"), pj.get("variation_id"), pj.get("ticker"),
                    pj.get("formula_version")) == key:
                raise ComputeJobInFlight(
                    f"compute-in-flight: job {r['id']} is already queued or running for this "
                    f"cache key"
                )
        row = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES (?, ?, ?, 'pending') RETURNING id;",
            (JOB_KIND_COMPUTE_METRICS, json.dumps(payload), now)).fetchone()
        return row["id"]


def enqueue_full_metrics_job(payload: dict, now: int | None = None) -> int:
    """Enqueue a compute_run_metrics job; return its id. Deduped: an already-in-flight job for the SAME cache
    key is reused (no double-compute). The caller checks for a prior FAILED job first (honest absence)."""
    now = int(time.time()) if now is None else now
    key = (payload["target_run_id"], payload["variation_id"], payload["ticker"], payload["formula_version"])
    with transaction() as conn:
        for r in conn.execute(
                "SELECT id, payload_json FROM research_jobs "
                "WHERE kind = ? AND status IN ('pending', 'picked_up');",
                (JOB_KIND_COMPUTE_METRICS,)).fetchall():
            try:
                pj = json.loads(r["payload_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if (pj.get("target_run_id"), pj.get("variation_id"), pj.get("ticker"),
                    pj.get("formula_version")) == key:
                return r["id"]  # already queued / running -> reuse (the in-flight job writes the cache)
        row = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at, status) "
            "VALUES (?, ?, ?, 'pending') RETURNING id;",
            (JOB_KIND_COMPUTE_METRICS, json.dumps(payload), now)).fetchone()
        return row["id"]


# TERM Runs-rebuild: the predicates the runs-summary aggregate reuses. Held as named constants (not
# inlined into an f-string brace) so the SQL stays literal and never interpolates a caller string, and
# so the same in-flight predicate reads identically in the count and the most-recent-row query.
_INFLIGHT_PRED = "r.status IN ('running', 'pending')"  # a run that is queued or executing right now
_SHARPE_PRESENT_PRED = "s.sharpe_ratio IS NOT NULL"     # only completed runs carry a Sharpe to rank


def runs_summary(kind_filter: str | None = None, include_synthetic: bool = False) -> dict:
    """Global aggregate over the run log for the Runs-list summary grid (TERM Runs rebuild).

    The runs LIST is paginated (GET /runs caps at 50 rows), so a header total computed from a single
    page would be a FALSE total once the log passes one page — exactly why D2.2 shipped without a grid
    (DECISIONS #143). This serves the honest global numbers the grid needs in ONE round-trip instead.

    Returns: total, by_status (status -> count), succeeded, success_rate (succeeded/total or None when
    empty), in_flight_count, best_sharpe ({run_id, name, sharpe_ratio} or None), and in_flight (the
    most-recent running/pending run {run_id, name, status, started_at} or None). The optional
    `kind_filter` (scratch | standard | None) scopes the summary to the SAME slice the list is showing,
    so a filtered list and its grid agree. KeyError on an unknown filter (the route validates to 400).

    DDIA (Ch 3 storage & retrieval; query-pattern rule). Aggregating in the system of record and
    shipping one small summary beats shipping every row to the client to count it. The GROUP BY status
    is a cheap full scan at the run-log's hundreds-of-rows scale (no index needed at this cardinality,
    the same measured-scan reasoning runs_list_sql records, canon :988-994); the best-Sharpe lookup is
    an index seek to the head of ix_stats_sharpe (ORDER BY s.sharpe_ratio DESC LIMIT 1), the §3.5 index
    that already backs the metric-sort list. No new index, no new write path.
    """
    kind_pred = _RUN_KIND_FILTERS[kind_filter] if kind_filter is not None else None  # KeyError -> 400
    synthetic_pred = None if include_synthetic else _NOT_SYNTHETIC_RUN_PRED  # GV2-4a: totals leave synthetic runs out too

    def _where(*extra: str) -> str:
        """Compose `WHERE p1 AND p2 ` from the active kind predicate + any extra predicates."""
        preds = [p for p in ((kind_pred, synthetic_pred) + extra) if p]
        return ("WHERE " + " AND ".join(preds) + " ") if preds else ""

    with get_conn() as conn:
        # One count per status (succeeded / failed / running / pending / cancelled).
        status_rows = conn.execute(
            f"SELECT r.status AS status, COUNT(*) AS n FROM research_runs r {_where()}GROUP BY r.status;"
        ).fetchall()
        by_status = {row["status"]: row["n"] for row in status_rows}
        # The best (highest) Sharpe across completed runs, with the owning run id + name for the sub-line.
        best = conn.execute(
            f"SELECT r.id AS id, r.name AS name, s.sharpe_ratio AS sharpe_ratio "
            f"FROM research_run_stats s JOIN research_runs r ON r.id = s.run_id "
            f"{_where(_SHARPE_PRESENT_PRED)}ORDER BY s.sharpe_ratio DESC LIMIT 1;"
        ).fetchone()
        # The most-recent in-flight run (started_at, falling back to created_at for a still-queued run).
        in_flight = conn.execute(
            f"SELECT r.id AS id, r.name AS name, r.status AS status, "
            f"       r.started_at AS started_at, r.created_at AS created_at FROM research_runs r "
            f"{_where(_INFLIGHT_PRED)}ORDER BY COALESCE(r.started_at, r.created_at) DESC LIMIT 1;"
        ).fetchone()

    total = sum(by_status.values())
    succeeded = by_status.get("succeeded", 0)
    return {
        "total": total,
        "by_status": by_status,
        "succeeded": succeeded,
        # None (not a fake 0) when there are no runs at all — an honest empty, divide-safe.
        "success_rate": (succeeded / total) if total else None,
        "in_flight_count": by_status.get("running", 0) + by_status.get("pending", 0),
        "best_sharpe": (
            {"run_id": best["id"], "name": best["name"], "sharpe_ratio": best["sharpe_ratio"]}
            if best is not None else None
        ),
        "in_flight": (
            {"run_id": in_flight["id"], "name": in_flight["name"], "status": in_flight["status"],
             "started_at": in_flight["started_at"] or in_flight["created_at"]}
            if in_flight is not None else None
        ),
    }


def list_runs_for_strategy(strategy_id: int, limit: int = 50, offset: int = 0,
                           include_synthetic: bool = False) -> list[dict]:
    """One strategy's run history, newest first (M4 R12, the library page per-strategy list).

    The WHERE strategy_id = ? + ORDER BY created_at DESC is served by the composite index
    ix_runs_strategy(strategy_id, created_at) (researchdb.py:338) — equality on the leading column,
    ordered scan (reversed) on the second, no temp B-tree (DDIA Ch3 composite-index access path; the
    M4 brief §4 / canon :988-994 lock this index as the join's path). LEFT JOIN to stats so a pending
    or failed run still appears in its strategy's history (not only completed runs). Each row carries
    r.strategy_version (in _RUN_COLUMNS), the version that run actually used.
    GV2-4a: runs on synthetic datasets are left out unless `include_synthetic` is true.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    synthetic_clause = "" if include_synthetic else f"AND {_NOT_SYNTHETIC_RUN_PRED} "
    with get_conn() as conn:
        rows = conn.execute(
            f"{_RUN_COLUMNS} FROM research_runs r "
            "LEFT JOIN research_run_stats s ON s.run_id = r.id "
            f"WHERE r.strategy_id = ? {synthetic_clause}"
            "ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?;",
            (strategy_id, limit, offset),
        ).fetchall()
    return [_decode_sparkline(dict(r)) for r in rows]


def last_run_strategy_version(strategy_id: int) -> int | None:
    """The strategy_version the strategy's most recent run used, or None if it has never run (M4 R11).

    Feeds the status badge: "which version the last run used" vs "which version the DB holds now".
    Same ix_runs_strategy access path as list_runs_for_strategy, LIMIT 1.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT strategy_version FROM research_runs WHERE strategy_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1;",
            (strategy_id,),
        ).fetchone()
    return int(row["strategy_version"]) if row is not None else None


def runs_missing_sparkline() -> list[int]:
    """Unit-4 D2 backfill support: ids of succeeded runs whose equity_sparkline is still NULL."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM research_runs WHERE status = 'succeeded' AND equity_sparkline IS NULL "
            "ORDER BY id;"
        ).fetchall()
    return [r["id"] for r in rows]


def set_run_sparkline(run_id: int, sparkline: list) -> bool:
    """Unit-4 D2 backfill: store a run's equity_sparkline ONLY if it is still NULL (idempotent).

    Returns True if a row was updated, False if it was already filled — the NULL-only guard is in
    the WHERE clause, so a re-run of the backfill leaves an already-filled row byte-unchanged.
    """
    with transaction() as conn:  # the production single-row write idiom (matches complete_run et al.)
        cur = conn.execute(
            "UPDATE research_runs SET equity_sparkline = ? "
            "WHERE id = ? AND equity_sparkline IS NULL;",
            # An empty sparkline stores NULL (not "[]"), so an empty-equity run stays eligible for
            # a later backfill rather than masquerading as filled.
            (json.dumps(sparkline) if sparkline else None, run_id),
        )
        return cur.rowcount > 0


# =============================================================
# M3 briefs G + H — strategy read + disk-mirror status (arch §8 derived-data discipline)
# =============================================================

def get_strategy(strategy_id: int) -> dict | None:
    """One strategy's current-state row by id, or None. Used by PUT (brief G) to resolve the name and
    by the build_run_bundle join. Returns the row EVEN IF soft-deleted (status='deleted'): an orphaned
    run's name join must keep resolving (TERM F2 keep+orphan). `status` is returned so the route guards
    can refuse open/edit/dispatch on a tombstone while the bundle read still resolves the name."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, display_name, source_code, description, version, disk_mirror_path, "
            "       last_mirror_status, last_mirror_error, status FROM research_strategies WHERE id = ?;",
            (strategy_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def all_strategies() -> list[dict]:
    """Every ACTIVE strategy's name + source_code, for the startup reconciliation pass (brief H).
    Soft-deleted strategies are excluded: their source is blanked and they are not re-runnable, so they
    must not participate in disk-mirror reconciliation (TERM F2)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, source_code FROM research_strategies WHERE status='active' ORDER BY id;"
        ).fetchall()
    return [dict(r) for r in rows]


def list_strategies() -> list[dict]:
    """Current-state strategy list for the workspace rail + the new-run dropdown (brief J). No source
    (only id/name/version/description needed). Excludes soft-deleted strategies (TERM F2): a tombstone
    is hidden from every picker; only the orphaned-run reads still resolve it (via get_strategy)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, display_name, version, description FROM research_strategies "
            "WHERE status='active' ORDER BY name;"
        ).fetchall()
    return [dict(r) for r in rows]


def list_strategy_names() -> list[dict]:
    """DN1: every strategy, active and deleted, as {id, name, display_name, status} ordered by id.

    Pages map a strategy id to its display name through this one read; a deleted strategy's orphaned runs
    stay listed, so the deleted rows are included. display_name is None for a row that has none.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, display_name, status, display_name_from_name FROM research_strategies ORDER BY id;"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        flagged = bool(d.pop("display_name_from_name"))
        d["needs_name"] = d["display_name"] is None or flagged  # NM1a
        out.append(d)
    return out


def list_strategies_overview(include_synthetic: bool = False) -> list[dict]:
    """UI17-0: every active strategy with its newest real run's curve and stats, in one SQL statement.

    The picked run is the newest (created_at, then id) succeeded standard run, synthetic runs left out
    unless `include_synthetic`. `run` is None when no run qualifies; the four stats are None when the
    run has no stats row. A non-null sparkline that cannot be parsed fails loud (_decode_sparkline).
    """
    synthetic_clause = "" if include_synthetic else f"AND {_NOT_SYNTHETIC_RUN_PRED} "
    sql = (
        "SELECT st.id AS id, st.name AS name, st.display_name AS display_name, st.version AS version, "
        "st.description AS description, "
        "pr.id AS run_id, pr.created_at AS run_created_at, pr.strategy_version AS run_strategy_version, "
        "pr.equity_sparkline AS run_equity_sparkline, s.sharpe_ratio AS run_sharpe_ratio, "
        "s.num_trades AS run_num_trades, s.total_return AS run_total_return, "
        "s.max_drawdown AS run_max_drawdown "
        "FROM research_strategies st "
        "LEFT JOIN research_runs pr ON pr.id = ("
        "SELECT r.id FROM research_runs r "
        f"WHERE r.strategy_id = st.id AND r.status = 'succeeded' AND {_RUN_KIND_FILTERS['standard']} "
        f"{synthetic_clause}"
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1) "
        "LEFT JOIN research_run_stats s ON s.run_id = pr.id "
        "WHERE st.status = 'active' ORDER BY st.name;"
    )
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    out = []
    for r in rows:
        run = None
        if r["run_id"] is not None:
            run = _decode_sparkline({
                "id": r["run_id"],
                "created_at": r["run_created_at"],
                "strategy_version": r["run_strategy_version"],
                "equity_sparkline": r["run_equity_sparkline"],
                "sharpe_ratio": r["run_sharpe_ratio"],
                "num_trades": r["run_num_trades"],
                "total_return": r["run_total_return"],
                "max_drawdown": r["run_max_drawdown"],
            })
        out.append({"id": r["id"], "name": r["name"], "display_name": r["display_name"], "version": r["version"],
                    "description": r["description"], "run": run})
    return out


_PROVENANCE_REL = "docs/research_lab/parameter_provenance.md"
_RANGES_REL = "research_lab/theories/ranges.toml"


def _line_of(path: Path, wanted: str) -> int | None:
    """1-based number of the first line of `path` that equals `wanted` exactly, else None."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    for number, line in enumerate(text.splitlines(), start=1):
        if line.rstrip() == wanted:
            return number
    return None


def _theory_toml_text(strategy_id: int, source_code: str) -> str:
    """The THEORY_TOML string constant of a stored theory strategy, read by ast and never executed."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError as exc:
        raise ValueError(f"strategy {strategy_id}: stored source does not parse: {exc}") from exc
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "THEORY_TOML" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
            raise ValueError(f"strategy {strategy_id}: THEORY_TOML is not a string constant")
    raise ValueError(f"strategy {strategy_id}: stored source has no THEORY_TOML string constant")


def _theory_parameters(strategy_id: int, source_code: str) -> list[dict]:
    try:
        theory = tomllib.loads(_theory_toml_text(strategy_id, source_code))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"strategy {strategy_id}: its THEORY_TOML does not parse: {exc}") from exc
    picks = theory.get("parameter_picks", {})
    if not picks:
        return []
    ranges_path = RANGES_PATH
    try:
        ranges = tomllib.loads(ranges_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"cannot read the ranges file {ranges_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"the ranges file {ranges_path} does not parse: {exc}") from exc
    out = []
    for letter, pick in picks.items():
        key = pick.get("range")
        rng = ranges.get(key) if key is not None else None
        if rng is None:
            value = {k: v for k, v in pick.items() if k != "range"}
            source = None
        else:
            if "values" in rng or "values" in pick:
                value = {"values": pick.get("values", rng.get("values"))}
            else:
                value = {f: pick.get(f, rng.get(f)) for f in ("from", "to", "step")}
            line = _line_of(ranges_path, f"[{key}]")
            source = {"file": _RANGES_REL, "key": key, "line": line, "why": rng.get("why")}
        out.append({"name": letter, "range": key, "value": value, "source": source})
    return out


def strategy_parameters(strategy_id: int, include_synthetic: bool = False) -> dict | None:
    """UI17b-0: every parameter of one strategy with its value and where it comes from (DECISIONS #459 item 8).

    None for an unknown or soft-deleted strategy. A standard strategy shows the values of its newest real
    single run (exactly one variation row); a theory strategy shows its picks, narrowed over ranges.toml.
    The stored source is read by ast and tomllib, never executed.
    """
    synthetic_clause = "" if include_synthetic else f"AND {_NOT_SYNTHETIC_RUN_PRED} "
    run_sql = (
        "SELECT r.id AS run_id, v.param_json AS param_json FROM research_runs r "
        "JOIN research_run_variations v ON v.run_id = r.id "
        "WHERE r.strategy_id = ? AND r.status = 'succeeded' AND " + _RUN_KIND_FILTERS["standard"] + " "
        + synthetic_clause +
        "AND (SELECT COUNT(*) FROM research_run_variations c WHERE c.run_id = r.id) = 1 "
        "ORDER BY r.created_at DESC, r.id DESC LIMIT 1;"
    )
    with get_conn() as conn:
        st = conn.execute(
            "SELECT id, name, source_code, status FROM research_strategies WHERE id = ?;", (strategy_id,)).fetchone()
        if st is None or st["status"] != "active":
            return None
        is_theory = st["name"].startswith(theory_strategy.NAME_PREFIX)
        run = None if is_theory else conn.execute(run_sql, (strategy_id,)).fetchone()
    if is_theory:
        return {"strategy_id": st["id"], "kind": "theory", "run_id": None,
                "parameters": _theory_parameters(st["id"], st["source_code"])}
    if run is None:
        return {"strategy_id": st["id"], "kind": "standard", "run_id": None, "parameters": []}
    values = json.loads(run["param_json"])
    parameters = []
    for name, value in values.items():
        line = _line_of(PROVENANCE_PATH, f"## {name}")
        source = None if line is None else {"file": _PROVENANCE_REL, "key": name, "line": line}
        parameters.append({"name": name, "range": None, "value": value, "source": source})
    return {"strategy_id": st["id"], "kind": "standard", "run_id": run["run_id"], "parameters": parameters}


def list_orphan_generated_candidates(conn) -> list[dict]:
    """SID1: generator records whose strategy_id names no research_strategies row (refused rows have NULL and are skipped)."""
    rows = conn.execute(
        "SELECT id, theory_id, skeleton_id, filler_id, strategy_id FROM research_generated_candidates "
        "WHERE strategy_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM research_strategies s WHERE s.id = research_generated_candidates.strategy_id) "
        "ORDER BY id;").fetchall()
    return [dict(r) for r in rows]


def remove_orphan_generated_candidates() -> list[dict]:
    """SID1: delete exactly the orphan rows, in one transaction; returns what was removed."""
    with transaction() as conn:
        orphans = list_orphan_generated_candidates(conn)
        conn.executemany("DELETE FROM research_generated_candidates WHERE id = ?;", [(o["id"],) for o in orphans])
        return orphans


def soft_delete_strategy(strategy_id: int, now: int | None = None) -> dict:
    """Delete a strategy under the keep+orphan policy (TERM F2; the strategy-delete arch note).

    Destroys the strategy's editable identity (its live source) and HIDES it, but KEEPS its runs
    (orphaned, never destroyed) along with their artifacts and share links. The version history is
    KEPT so an orphaned run can still show the exact source it ran (the keep-history ruling, 2026-06-16).

    One atomic transaction (the deletions-log row commits with the change, AD8's discipline; the
    guarantee is atomicity, not statement order). Two outcomes:
      - runs exist -> TOMBSTONE: status='deleted', source_code='' (blanked); the row + name + history
        survive, so every orphaned run keeps resolving its name AND its source.
      - no runs    -> TRUE row-removal: nothing to orphan, so DELETE the row (history + tags cascade
        away via ON DELETE CASCADE; ON DELETE RESTRICT permits it because no run references the row).

    Returns:
      {"action": "tombstoned", "id": id, "orphaned_runs": n}   runs were kept + orphaned
      {"action": "removed",    "id": id, "orphaned_runs": 0}   zero-run strategy fully erased
      {"action": "noop",       "id": id}                       already deleted (idempotent)
    Raises StrategyNotFound if no such id.
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        row = conn.execute(
            "SELECT id, name, status FROM research_strategies WHERE id = ?;",
            (strategy_id,),
        ).fetchone()
        if row is None:
            raise StrategyNotFound(f"strategy {strategy_id} not found")
        if row["status"] == 'deleted':
            return {"action": "noop", "id": strategy_id}  # idempotent: already a tombstone

        orphaned = conn.execute(
            "SELECT COUNT(*) FROM research_runs WHERE strategy_id = ?;",
            (strategy_id,),
        ).fetchone()[0]

        # The deletions-log row records the delete either way, in the same transaction (atomic).
        conn.execute(
            "INSERT INTO research_deletions (strategy_id, strategy_name, orphaned_runs, deleted_at) "
            "VALUES (?, ?, ?, ?);",
            (strategy_id, row["name"], orphaned, now),
        )

        if orphaned == 0:
            # No run references the strategy: ON DELETE RESTRICT permits the removal and history + tags
            # cascade away. Nothing to keep, so this is a complete erase (the zero-run draft case).
            conn.execute("DELETE FROM research_strategies WHERE id = ?;", (strategy_id,))
            return {"action": "removed", "id": strategy_id, "orphaned_runs": 0}

        # Runs exist: tombstone. Hide it (status) and blank the live editable source; keep the row,
        # the name, and research_strategies_history so the orphaned runs stay fully interpretable.
        conn.execute(
            "UPDATE research_strategies SET status='deleted', source_code='', updated_at=? WHERE id = ?;",
            (now, strategy_id),
        )
        return {"action": "tombstoned", "id": strategy_id, "orphaned_runs": orphaned}


def get_strategy_version(strategy_id: int, version: int) -> dict | None:
    """The source + description AS OF a specific version (research_strategies_history).

    The bundle (brief K.5 / RunExplorer K) must show the strategy as it was at the run's
    dispatch version, not the current row, which may have been edited since. Returns None if
    that (strategy_id, version) was never recorded.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source_code, description FROM research_strategies_history "
            "WHERE strategy_id = ? AND version = ? ORDER BY id DESC LIMIT 1;",
            (strategy_id, version),
        ).fetchone()
    return dict(row) if row is not None else None


def list_strategy_versions(strategy_id: int) -> list[dict]:
    """Every recorded version of a strategy, newest first (M4 R7, the version-history panel).

    Reads research_strategies_history (the append-only snapshot log, researchdb.py:290-298) via its
    index ix_strat_hist_sid(strategy_id, version): equality on strategy_id, ordered scan on version.
    Returns the light list {version, description, edited_at} — NOT source_code (the diff editor fetches
    a single version's source on demand via get_strategy_version, so the list stays cheap even for a
    strategy with many edits). Empty list if the strategy has no history (an unknown id, or — never —
    a strategy with zero versions, which upsert_strategy makes impossible).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT version, description, edited_at FROM research_strategies_history "
            "WHERE strategy_id = ? AND id IN ("
            "SELECT MIN(id) FROM research_strategies_history WHERE strategy_id = ? GROUP BY version) "
            "ORDER BY version DESC;",
            (strategy_id, strategy_id),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_settings_snapshot(run_id: int) -> str | None:
    """The canonical settings_snapshot JSON string captured at the run's dispatch, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT settings_snapshot FROM research_runs WHERE id = ?;", (run_id,)
        ).fetchone()
    return row["settings_snapshot"] if row is not None else None


def run_log_lines(job_id: int, limit: int = 5000) -> list[dict]:
    """All log lines for a run's job, oldest first, for the export bundle (include_logs)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, level, line FROM research_job_logs WHERE job_id = ? ORDER BY id ASC LIMIT ?;",
            (job_id, max(1, min(limit, 50000))),
        ).fetchall()
    return [dict(r) for r in rows]


def get_dataset(dataset_id: int) -> dict | None:
    """One dataset's full row (incl. soft-deleted), or None. Shared by GET /datasets/{id} and
    the run bundle (brief K.5 / RunExplorer K)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, source, symbol, timeframe, start_ts, end_ts, status, "
            "       dataset_type, rows, sha256, file_path, file_bytes, last_error, "
            "       created_at, last_fetched_at, covered_start_ts, covered_end_ts, "
            "       store_id, store_fetched_at "
            "FROM research_datasets WHERE id = ?;",  # dataset_type lets the bundle branch a universe vs a price dataset (BN-I); covered_* = W3 true coverage; store_* = night four lane S
            (dataset_id,),
        ).fetchone()
    return dict(row) if row is not None else None


# =============================================================
# M3 brief I — cancellation (arch §20; DDIA Ch 8 single-writer atomicity)
#
# Two timing cases (EI-pre): a cancel can land while the run_backtest job is still pending
# (unclaimed — no container exists) or already running. Pending is finalized directly by the
# backend in one transaction (job + run -> cancelled); running sets cancellation_requested=1
# and the worker hard-kills, then calls /worker/runs/{id}/cancelled (the worker has no DB).
# The 'cancelling' transient (arch §20 prose) is NOT used: nothing observes it and the atomic
# numba call is un-interruptible, so M3 goes running -> cancelled directly.
# =============================================================

class JobNotFound(LookupError):
    """No research_jobs row with the given id."""


_TERMINAL_JOB_STATUSES = ("succeeded", "failed", "cancelled")


def request_cancel(job_id: int, now: int | None = None) -> dict:
    """Cancel a job, branching on its current status (EI-pre). One transaction throughout.

    Returns one of:
      {"action": "cancelled", "job_id", "run_id"}   pending job finalized directly (run too if any)
      {"action": "requested", "job_id", "run_id"}   running run_backtest: flag set, worker will kill
      {"action": "already",   "job_id", "status"}   already terminal (route: 200 if cancelled, else 409)
      {"action": "unsupported", "job_id", "status"} running non-backtest job (route: 409, M1 behavior)
    Raises JobNotFound if the id is unknown.
    """
    now = int(time.time()) if now is None else now
    with transaction() as conn:
        job = conn.execute(
            "SELECT status, kind FROM research_jobs WHERE id = ?;", (job_id,)
        ).fetchone()
        if job is None:
            raise JobNotFound(f"job {job_id} not found")
        status, kind = job["status"], job["kind"]
        run = conn.execute("SELECT id FROM research_runs WHERE job_id = ?;", (job_id,)).fetchone()
        run_id = run["id"] if run is not None else None

        if status in _TERMINAL_JOB_STATUSES:
            return {"action": "already", "job_id": job_id, "status": status}

        if status == "pending":  # unclaimed — finalize directly, no worker (EI-pre pending branch)
            conn.execute(
                "UPDATE research_jobs SET status = 'cancelled', finished_at = ? WHERE id = ?;",
                (now, job_id),
            )
            if run_id is not None:  # run_backtest: finalize its pre-INSERTed run row too
                null_paths = ", ".join(f"{c} = NULL" for c in _RUN_ARTIFACT_COLUMNS)
                conn.execute(
                    f"UPDATE research_runs SET status = 'cancelled', finished_at = ?, error = NULL, "
                    f"{null_paths} WHERE id = ?;",
                    (now, run_id),
                )
            conn.execute(
                "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
                "VALUES (?, ?, 'cancelled', ?);",
                (job_id, now, json.dumps({"by": "browser", "pre_claim": True})),
            )
            return {"action": "cancelled", "job_id": job_id, "run_id": run_id}

        # picked_up / running
        if kind != "run_backtest":  # M3 only hard-kills the atomic backtest (L6); others stay M1
            return {"action": "unsupported", "job_id": job_id, "status": status}
        conn.execute(
            "UPDATE research_jobs SET cancellation_requested = 1 WHERE id = ?;", (job_id,)
        )
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, 'cancel_requested', ?);",
            (job_id, now, json.dumps({"by": "browser"})),
        )
        return {"action": "requested", "job_id": job_id, "run_id": run_id}


def job_cancellation_requested(job_id: int) -> bool:
    """Read the cancellation flag for a job (served to the worker in the heartbeat response)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cancellation_requested FROM research_jobs WHERE id = ?;", (job_id,)
        ).fetchone()
    return bool(row["cancellation_requested"]) if row is not None else False


def set_strategy_mirror(strategy_id: int, disk_mirror_path: str | None,
                        status: str, error: str | None) -> None:
    """Persist the disk-mirror outcome after a best-effort write (brief G). Never the source of truth."""
    with transaction() as conn:
        conn.execute(
            "UPDATE research_strategies "
            "   SET disk_mirror_path = ?, last_mirror_status = ?, last_mirror_error = ? "
            " WHERE id = ?;",
            (disk_mirror_path, status, error, strategy_id),
        )


# =============================================================
# K.6 — permalink share helpers (M3 spec §3.6, §5.6; arch §21)
# =============================================================
#
# UUID4 is the share secret (122 bits of entropy — the URL is the credential
# per the leakage threat model). create_share inserts; get_share reads; the
# atomic claim is a single UPDATE ... RETURNING that increments view_count and
# auto-revokes when the limit is hit (no TOCTOU window). update_share is
# locked one-way per Fork K6-M: a revoked share cannot be re-enabled via PATCH;
# the user creates a fresh share instead.


# Columns returned by share-read helpers — kept in one tuple so the SELECT
# projections and the dict comprehensions can't drift apart.
_SHARE_COLS: tuple[str, ...] = (
    "uuid", "run_id", "batch_id",
    "include_source", "include_settings", "include_indicators", "include_trades", "include_logs",
    "view_limit", "view_count",
    "created_at", "revoked_at", "last_viewed_at",
)


def _share_row_to_dict(row) -> dict:
    """Convert a sqlite Row (or tuple aligned with _SHARE_COLS) into a plain dict."""
    return {col: row[col] for col in _SHARE_COLS}


class ShareNotFound(LookupError):
    """No row in research_shared_runs for the requested uuid."""


class ShareRevoked(RuntimeError):
    """The share row exists but is already revoked or over its view limit (Fork K6-M, K6-F)."""


def create_share(
    run_id: int,
    *,
    include_source: bool = True,
    include_settings: bool = True,
    include_indicators: bool = True,
    include_trades: bool = True,
    include_logs: bool = False,
    view_limit: int | None = 10,  # API-handler default per L27; nullable; explicit None = unlimited
    now: int | None = None,
) -> dict:
    """Insert a new share row and return its row dict.

    `view_limit` defaults to 10 (L27); pass None for unlimited. The 5 content flags
    default to the L25 defaults (logs OFF, the rest ON). The uuid is uuid4 (the
    secret); the row's created_at is the server's now-timestamp.

    Note: this does NOT verify run_id exists — that's a route-layer concern (a
    404 lives there). The FK on research_runs(id) will reject an invalid run_id
    at INSERT time, but we let the caller translate.
    """
    share_uuid = str(_uuid.uuid4())  # 122-bit secret; never reuse, never derive
    ts = int(time.time()) if now is None else now
    # Single INSERT — no multi-statement transaction needed (one row, all-or-nothing
    # by definition). batch_id is explicitly NULL for the run-share path; the
    # table-level CHECK ((run_id IS NULL) != (batch_id IS NULL)) brief F added
    # rejects the (NULL, NULL) and (non-null, non-null) cases.
    with transaction() as conn:
        conn.execute(
            "INSERT INTO research_shared_runs "
            "    (uuid, run_id, batch_id, include_source, include_settings, include_indicators, "
            "     include_trades, include_logs, view_limit, view_count, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?);",
            (share_uuid, run_id,
             int(include_source), int(include_settings), int(include_indicators),
             int(include_trades), int(include_logs),
             view_limit, ts),
        )
        row = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
            (share_uuid,),
        ).fetchone()
    return _share_row_to_dict(row)


def create_batch_share(
    batch_id: int,
    *,
    include_source: bool = True,
    include_settings: bool = True,
    include_indicators: bool = True,
    include_trades: bool = True,
    include_logs: bool = False,
    view_limit: int | None = 10,
    now: int | None = None,
) -> dict:
    """M2.5 brief F: insert a polymorphic share row pointing at an events.db batch_id.

    Mirror image of create_share, but writes the new row with run_id=NULL and
    batch_id=:batch_id. The table-level CHECK constraint enforces exactly-one;
    no FK on batch_id (cross-DB; lineage enforced at the route layer's
    existence check against events.db before this is called).

    Content flags retain their M3 K.6 meaning where applicable:
      include_source       → "include batch source_descriptor"
      include_settings     → "include batch policy + request criteria"
      include_indicators   → ignored on batch shares
      include_trades       → ignored on batch shares
      include_logs         → "include full quality_report including quarantine details"
    The renderer reads the flags from the share row and gates sections accordingly.
    """
    share_uuid = str(_uuid.uuid4())
    ts = int(time.time()) if now is None else now
    with transaction() as conn:
        conn.execute(
            "INSERT INTO research_shared_runs "
            "    (uuid, run_id, batch_id, include_source, include_settings, include_indicators, "
            "     include_trades, include_logs, view_limit, view_count, created_at) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 0, ?);",
            (share_uuid, batch_id,
             int(include_source), int(include_settings), int(include_indicators),
             int(include_trades), int(include_logs),
             view_limit, ts),
        )
        row = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
            (share_uuid,),
        ).fetchone()
    return _share_row_to_dict(row)


def list_active_shares_for_batch(batch_id: int) -> list[dict]:
    """Return non-revoked share rows pointing at a given events.db batch_id.

    Backs the brief F admin UI's batch-share-management section (parallel to the
    M3 K.6 RunExplorer share management). Uses the brief F partial index
    `ix_shared_runs_batch` (WHERE revoked_at IS NULL) for an indexed lookup.
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs "
            "WHERE batch_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC;",
            (batch_id,),
        ).fetchall()
    return [_share_row_to_dict(r) for r in rows]


def get_share(share_uuid: str) -> dict | None:
    """Read a share row by uuid; None if no such row.

    Used by the route to distinguish 404 (missing) from 410 (revoked / over-limit);
    the existence check has a benign race with a concurrent revoke that resolves
    to the correct status downstream (per the architectural anchors).
    """
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
            (share_uuid,),
        ).fetchone()
    return _share_row_to_dict(row) if row is not None else None


def list_active_shares_for_run(run_id: int) -> list[dict]:
    """Return the active (non-revoked) share rows for a run, newest first.

    Backs the K.6 share-management section on RunExplorer. The partial index
    ix_shared_runs_active makes this lookup cheap even at high share counts.
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs "
            "WHERE run_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC;",
            (run_id,),
        ).fetchall()
    return [_share_row_to_dict(r) for r in rows]


def update_share(
    share_uuid: str,
    *,
    view_limit: int | None | object = ...,  # sentinel to distinguish "absent" from "None"
    include_source: bool | None = None,
    include_settings: bool | None = None,
    include_indicators: bool | None = None,
    include_trades: bool | None = None,
    include_logs: bool | None = None,
) -> dict:
    """PATCH a share's view_limit and/or content flags.

    Refuses to modify a revoked share — revoke is one-way per L26 / Fork K6-M.
    Raises ShareNotFound or ShareRevoked. Returns the updated row dict.

    `view_limit=...` (the default sentinel) means "do not change view_limit";
    `view_limit=None` means "set to NULL = unlimited"; `view_limit=<int>` sets it.
    The five content flags use `None` as "do not change" (no None-vs-False ambiguity
    because booleans serialise as 0/1 and "no change" is meaningful in PATCH semantics).
    """
    # Build the SET clause dynamically: only the keys the caller specified.
    sets: list[str] = []
    args: list = []
    if view_limit is not ...:  # caller wants to update view_limit
        sets.append("view_limit = ?")
        args.append(view_limit)
    for col, value in (
        ("include_source", include_source),
        ("include_settings", include_settings),
        ("include_indicators", include_indicators),
        ("include_trades", include_trades),
        ("include_logs", include_logs),
    ):
        if value is not None:
            sets.append(f"{col} = ?")
            args.append(int(value))
    # No-op PATCH: still validate existence and revoke state, but skip the write.
    with transaction() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
            (share_uuid,),
        ).fetchone()
        if row is None:
            raise ShareNotFound(share_uuid)
        if row["revoked_at"] is not None:
            raise ShareRevoked(share_uuid)
        if sets:
            args.append(share_uuid)
            conn.execute(
                f"UPDATE research_shared_runs SET {', '.join(sets)} WHERE uuid = ?;",
                args,
            )
            row = conn.execute(
                f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
                (share_uuid,),
            ).fetchone()
    return _share_row_to_dict(row)


def revoke_share(share_uuid: str, now: int | None = None) -> dict:
    """Manual revoke (DELETE /shares/{uuid}). Idempotent — second call is a no-op.

    Sets revoked_at = now on first revoke; on a second revoke the existing
    revoked_at stays (preserves the original timestamp for audit). Raises
    ShareNotFound if the uuid is unknown.
    """
    ts = int(time.time()) if now is None else now
    with transaction() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
            (share_uuid,),
        ).fetchone()
        if row is None:
            raise ShareNotFound(share_uuid)
        if row["revoked_at"] is None:
            conn.execute(
                "UPDATE research_shared_runs SET revoked_at = ? WHERE uuid = ?;",
                (ts, share_uuid),
            )
            row = conn.execute(
                f"SELECT {', '.join(_SHARE_COLS)} FROM research_shared_runs WHERE uuid = ?;",
                (share_uuid,),
            ).fetchone()
    return _share_row_to_dict(row)


def try_claim_share_view(share_uuid: str, now: int | None = None) -> dict | None:
    """Atomic view claim: increment view_count, stamp last_viewed_at, auto-revoke at limit.

    The single UPDATE ... RETURNING collapses check + increment + auto-revoke into
    one statement — no TOCTOU window. Returns:
    - The updated row dict (with content flags) on a successful claim. If the claim
      hit the view limit on this call, the returned row has revoked_at = now (auto-revoke
      just fired); the route uses this to publish research.share_revoked with
      reason="view_limit_reached".
    - None if the share is dead (revoked OR over-limit). The route returns 410.
    - The route distinguishes 404 (missing uuid entirely) by calling get_share first;
      this helper returns None in BOTH the missing-uuid and dead-share cases by design
      (the SQL WHERE filters both out).
    """
    ts = int(time.time()) if now is None else now
    # The RETURNING clause yields the post-update row state. The WHERE excludes
    # already-revoked rows AND already-over-limit rows; if either is true, the
    # UPDATE matches 0 rows and RETURNING is empty.
    with transaction() as conn:
        row = conn.execute(
            "UPDATE research_shared_runs "
            "   SET view_count    = view_count + 1, "
            "       last_viewed_at = ?, "
            "       revoked_at    = CASE "
            "                         WHEN view_limit IS NOT NULL "
            "                          AND view_count + 1 >= view_limit "
            "                         THEN ? "
            "                         ELSE revoked_at "
            "                       END "
            " WHERE uuid = ? "
            "   AND revoked_at IS NULL "
            "   AND (view_limit IS NULL OR view_count < view_limit) "
            f"RETURNING {', '.join(_SHARE_COLS)};",
            (ts, ts, share_uuid),
        ).fetchone()
    return _share_row_to_dict(row) if row is not None else None
