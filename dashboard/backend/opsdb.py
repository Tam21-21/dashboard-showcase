"""ops.db access: the operations page's own store, kept out of the research data path on purpose.

OPS-3, spec brief docs/briefs/ops_build_brief.md section 12.2 (also 7.1, 7.2). This module owns a
fourth SQLite file, separate from dashboard.db, research.db and events.db, so that monitor state
never shares a write lock or a backup unit with research data. Mirrors eventsdb.py's own shape:
its own connection helper, its own transaction context manager, its own idempotent init function.

This unit builds the STORE ONLY. No grading, no sweeper, no routes, no page and no registry seed
live here; those are later units (sections 12.3 through 12.9 of the same brief). That includes
RETENTION: ops_observations grows without bound in this build, on purpose, because the section-12.4
sweeper unit owns the prune. See the growth note beside _DDL_OBSERVATIONS below for what bounds
that growth in practice until the prune ships. Growth watching itself is not built by this unit:
it will be covered by db_ops_health, the row already registered in
docs/ops/ops_monitor_registry.md for exactly this gap, whose status there is still "owed"
(round 2 fold, findings list for unit/ops-store).

Connection discipline mirrors eventsdb.py and researchdb.py:
  1. foreign_keys is per-connection in SQLite, so it is set on every new connection.
  2. journal_mode=WAL is persistent at the database-file level; re-setting it is a cheap no-op.
  3. isolation_level=None means this module manages transactions explicitly with BEGIN/COMMIT.

The ops_db_path argument is explicit on every entry point (never a hidden global) so a probe or a
test can point this module at a throwaway file instead of the real ops.db, the same pattern
eventsdb.py already uses for exactly this reason.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from .config import OPS_DB_PATH, PROJECT_ROOT

# Module-level default path; callers pass an explicit override when they need one (probes do).
DB_PATH = OPS_DB_PATH


# =============================================================================
# Connection and transaction helpers, mirroring eventsdb.py's get_events_conn / events_transaction.
# =============================================================================

class OpsDbParentDirectoryMissing(RuntimeError):
    """Raised when ops_db_path's parent directory does not exist, before sqlite3 ever opens it."""


def _connect_ops(ops_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on ops.db with the per-connection pragmas this module relies on applied."""
    path = Path(ops_db_path)
    # F4 fix: check the parent directory up front. Without this, a missing parent surfaces only as
    # a bare sqlite3.OperationalError with no path in it; we never create the directory ourselves
    # (that would be an unrequested fallback), we just name the problem loudly instead.
    if not path.parent.is_dir():
        raise OpsDbParentDirectoryMissing(
            f"cannot open ops.db at {path}: its parent directory {path.parent} does not exist"
        )
    # autocommit mode at the driver level; this module owns transactions explicitly via BEGIN/COMMIT.
    conn = sqlite3.connect(str(path), isolation_level=None)
    # foreign keys are per-connection in SQLite: without this, an FK violation silently no-ops.
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL is persistent at the file level once set; re-running this on an already-WAL file is free.
    conn.execute("PRAGMA journal_mode = WAL;")
    # dict-like rows so a caller reads row["column"] instead of indexing by position.
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_ops_conn(ops_db_path: Union[str, Path, None] = None):
    """Context manager for a read-only query or a single-statement write on ops.db.

    Per-call lifecycle: opens a fresh connection on enter, closes it on exit. No singleton, no
    pooling, matching eventsdb.get_events_conn's own reasoning (a long-lived shared connection is
    exactly the kind of hidden state a probe pointed at a throwaway file must not inherit).
    """
    path = ops_db_path if ops_db_path is not None else DB_PATH
    conn = _connect_ops(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def ops_transaction(ops_db_path: Union[str, Path, None] = None):
    """Context manager wrapping BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection.

    For multi-statement writes: the caller's block runs inside one transaction, committed on a
    clean exit and rolled back on any exception, which is the property brief section 7.4 depends
    on for the write path later units build on top of this store ("one transaction per
    observation": the observation insert, the transition insert and the current-state update all
    succeed together or none of them do).
    """
    path = ops_db_path if ops_db_path is not None else DB_PATH
    conn = _connect_ops(path)
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


# =============================================================================
# Schema DDL: five tables, four indexes (brief section 7.2). Every statement is idempotent
# (CREATE ... IF NOT EXISTS), so a partial run (process killed mid-init) can be retried cleanly
# on the next dashboard restart, and running init_ops_db() twice changes nothing (AC-2).
#
# Table order matters here even though SQLite does not strictly require it: ops_monitors is the
# foreign-key target of every other table, so it is created first, and ops_observations is created
# before ops_transitions and ops_monitor_current, which each reference an observation id.
# =============================================================================

# ops_monitors: one row per monitor, forever (never deleted, retired by a status flip, section
# 7.3). The six typed parameter columns (cadence_key/cadence_seconds, degraded_decay_key/
# degraded_decay_seconds, down_decay_key/down_decay_seconds) are the pairing rule of section 7.2:
# one typed key column beside one typed value column, three values, three keys. They carry NO
# "NOT NULL": two seeded monitors are event-driven and their windows are legitimately null
# (section 7.2), so this DDL states nullability rather than guessing a default. The threshold key
# and its value live only in config_json, never as a typed column of their own (section 7.2: "The
# threshold key is deliberately NOT a typed column").
_DDL_MONITORS = """
CREATE TABLE IF NOT EXISTS ops_monitors (
    monitor_id              TEXT    PRIMARY KEY,
    family                  TEXT    NOT NULL,
    display_name             TEXT    NOT NULL,
    kind                     TEXT    NOT NULL,
    enabled                  INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    status                   TEXT    NOT NULL DEFAULT 'active',
    cadence_key              TEXT,
    cadence_seconds          INTEGER,
    degraded_decay_key       TEXT,
    degraded_decay_seconds   INTEGER,
    down_decay_key           TEXT,
    down_decay_seconds       INTEGER,
    config_json              TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
    created_at                TEXT    NOT NULL,
    updated_at                TEXT    NOT NULL
);
"""

# ops_observations: append-only, one row per probe result or report-in (section 7.2). reported_at
# is nullable (a probe result is server-stamped only; a reporter-supplied stamp is not always
# present) and value_numeric is nullable (not every observation carries a number), both stated
# directly in section 7.2's own column shape. detail_json accepts NULL (many observations carry
# no extra detail) and, when present, must be valid JSON; the CHECK is written to pass on NULL
# rather than reject it, because a plain CHECK(json_valid(x)) would otherwise treat every NULL row
# as a constraint failure.
#
# GROWTH (fold F2, findings list for unit/ops-store). This DDL builds the table only: no purge, no
# TTL, no sweeper lives in this unit (that is section 12.4, "the prune", a later unit; this module's
# own top docstring already says so). So the DECISION, written down rather than left silent:
#   - This table grows without bound BY DESIGN in version one. Nothing here deletes a row.
#   - What bounds it IN PRACTICE, derived rather than guessed: the brief seeds a fixed registry of
#     31 monitor rows (30 monitored things plus the sweeper's own self-tick row, brief section 10
#     and section 7.6), and a monitor writes at most one observation per sweeper tick it is due
#     for. So write volume scales with (seeded monitor count) x (sweeper ticks per unit time), a
#     bounded product of two known-shape quantities, not an open-ended fan-out; it is not zero
#     growth, and it is not "however much traffic arrives" the way a public-facing log would be.
#   - Retention is OWNED by the section-12.4 sweeper unit ("the prune", pruned on an age window per
#     brief section 7.5: "ops_observations: pruned on an age window, because the current-state
#     table already holds the latest value per monitor, so old raw observations are superseded by
#     construction"). That unit provenances the actual window and batch size; this unit does not.
#   - Until that unit lands, growth watching is NOT yet built: nothing today reports this table's
#     row count. The registered fix is db_ops_health (docs/ops/ops_monitor_registry.md, Group C:
#     "the size, growth rate and integrity of ops.db, the store the page itself writes to"), listed
#     there with status "owed" and cross-referenced at open issue I-16 ("The operations store's own
#     growth is observed by nothing"). This module does not claim that row is live; it names the
#     row so the gap has an owner rather than a fabricated monitor name.
#
# DDIA basis (docs/books/kleppmann_canon.md, Chapter 12: Stream Processing, "Log-based message
# brokers" > "Disk space", read for this fold): "Log split into segments; old segments deleted or
# archived (or log-compacted). Bounded buffer = on-disk ring buffer." The same principle applies
# here even without Kafka's segment machinery: an append-only log needs an EXPLICIT bounding
# strategy or it grows forever, and the honest move when that strategy is not built yet is to say
# so in the schema's own comment, not to build a purge nobody asked this unit for and not to stay
# silent about the gap either.
_DDL_OBSERVATIONS = """
CREATE TABLE IF NOT EXISTS ops_observations (
    observation_id  INTEGER PRIMARY KEY,
    monitor_id       TEXT    NOT NULL REFERENCES ops_monitors(monitor_id),
    received_at      TEXT    NOT NULL,
    reported_at      TEXT,
    outcome          TEXT    NOT NULL,
    value_numeric    REAL,
    detail_json      TEXT    CHECK (detail_json IS NULL OR json_valid(detail_json)),
    error_text       TEXT
);

-- Serves the per-monitor recent series (the page's history strip for one monitor, ordered by
-- time) and the future retention prune scan this table's own comment below explains, "oldest
-- rows for this monitor first". Both walk this same (monitor_id, received_at) order, so one
-- index covers both without a full table scan. _split_ddl_statements strips "--" comments before
-- splitting on ";" (round 2 fold), so a semicolon inside this comment is no longer a hazard.
CREATE INDEX IF NOT EXISTS ix_ops_observations_monitor_received
    ON ops_observations(monitor_id, received_at);
"""

# ops_transitions: append-only, one row per state change only (section 7.2). previous_state is
# nullable: the very first transition a monitor ever writes has no real prior state to name (the
# t-zero cold-start case, section 7.2), so forcing it NOT NULL would make that first write
# unrepresentable. new_state, changed_at and reason are always present (a transition IS a new
# state, a time and a reason by definition), and caused_by_observation_id is always present
# because section 7.4 writes the observation and its transition inside the same transaction, so
# a transition never exists without the observation that caused it.
_DDL_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS ops_transitions (
    transition_id             INTEGER PRIMARY KEY,
    monitor_id                 TEXT    NOT NULL REFERENCES ops_monitors(monitor_id),
    previous_state              TEXT,
    new_state                   TEXT    NOT NULL,
    changed_at                  TEXT    NOT NULL,
    reason                       TEXT    NOT NULL,
    caused_by_observation_id    INTEGER NOT NULL REFERENCES ops_observations(observation_id)
);

-- Serves "since when" per monitor: the page's per-monitor detail view asks "what state changes
-- has THIS monitor had, in order", which walks (monitor_id, changed_at) directly.
CREATE INDEX IF NOT EXISTS ix_ops_transitions_monitor_changed
    ON ops_transitions(monitor_id, changed_at);
-- Serves the page-level RECENT ACTIVITIES strip: "the newest state changes across every monitor",
-- which needs changed_at alone, unfiltered by monitor_id, so it earns its own single-column index
-- rather than relying on the composite index above (a leading monitor_id column cannot serve a
-- query with no monitor_id filter).
CREATE INDEX IF NOT EXISTS ix_ops_transitions_changed
    ON ops_transitions(changed_at);
"""

# ops_monitor_current: one row per monitor, mutable, derived from the log above (section 7.2).
# Only monitor_id and current_state are required; every other column is nullable because a
# monitor's current row can legitimately exist before any of those facts are known (the t-zero
# lifecycle question, section 7.2: "last_proven is NULL at seed", "the acknowledgement fields are
# NULL at seed"). current_state is CHECK-constrained to exactly the three words the page renders
# (section 7.2 "State enumeration"; M6): a fourth value is a schema change, not something this
# constraint should silently accept.
_DDL_MONITOR_CURRENT = """
CREATE TABLE IF NOT EXISTS ops_monitor_current (
    monitor_id            TEXT    PRIMARY KEY REFERENCES ops_monitors(monitor_id),
    current_state          TEXT    NOT NULL CHECK (current_state IN ('OPERATIONAL', 'DEGRADED', 'DOWN')),
    last_proven_at          TEXT,
    last_observation_id    INTEGER REFERENCES ops_observations(observation_id),
    current_reason          TEXT,
    next_due_at              TEXT,
    acknowledged_at         TEXT
);

-- Serves the sweeper's due select (a later unit, section 12.3 onward): "which monitors are due
-- for a fresh observation right now", which scans next_due_at ordered ascending rather than every
-- row in the table.
CREATE INDEX IF NOT EXISTS ix_ops_monitor_current_next_due
    ON ops_monitor_current(next_due_at);
"""

# ops_daily_rollup: one row per monitor per UTC day, upserted by the sweeper (a later unit),
# holding the day's first, last, minimum, maximum and count of the numeric observation (section
# 7.2). The primary key is the composite (monitor_id, day): SQLite creates an automatic index for
# an inline PRIMARY KEY, which is excluded from the four named indexes this brief counts (section
# 7.2: "No fifth index without a named query that needs it"), so this table adds none of its own.
_DDL_DAILY_ROLLUP = """
CREATE TABLE IF NOT EXISTS ops_daily_rollup (
    monitor_id    TEXT    NOT NULL REFERENCES ops_monitors(monitor_id),
    day            TEXT    NOT NULL,
    first_value    REAL,
    last_value     REAL,
    min_value      REAL,
    max_value      REAL,
    count          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (monitor_id, day)
);
"""

# Creation order: parents before children, so a foreign key never names a table that does not
# exist yet at the point SQLite parses the CREATE TABLE statement.
_DDL_SCRIPTS_ORDERED = (
    _DDL_MONITORS,
    _DDL_OBSERVATIONS,
    _DDL_TRANSITIONS,
    _DDL_MONITOR_CURRENT,
    _DDL_DAILY_ROLLUP,
)


def _split_ddl_statements(script: str) -> list[str]:
    """Split a multi-statement DDL string into individual SQL statements.

    sqlite3's conn.executescript() implicitly commits any open transaction before running, which
    would break the ops_transaction() BEGIN IMMEDIATE / COMMIT wrapper init_ops_db() runs inside
    (the same reason eventsdb.py's own _split_ddl_statements exists). Round 2 fold (SF1, findings
    list for unit/ops-store): this now strips "--" line comments BEFORE splitting on ";", the same
    two-step approach eventsdb.py's own _split_ddl_statements already uses, so a semicolon typed
    inside a DDL comment here can no longer split a CREATE statement in half. The earlier version
    of this function split on a bare ";" with no comment awareness and relied on a docstring asking
    editors never to type a semicolon inside a "--" comment; that discipline already broke once (a
    semicolon inside a comment split a CREATE INDEX statement and turned eight probes red), so the
    fix is the same comment strip eventsdb.py already carries, not a stronger warning.
    """
    # Walk the script one source line at a time so a "--" comment can be found and cut per line,
    # the same granularity eventsdb.py's own version uses (a "--" only starts a comment for the
    # rest of that one line in SQL, never across lines).
    no_comment_lines = []
    for line in script.splitlines():
        # find() returns -1 when the line has no "--" at all, so the whole line survives untouched.
        comment_start = line.find("--")
        if comment_start == -1:
            no_comment_lines.append(line)
        else:
            # Keep only the code before the comment marker; the comment text itself is discarded,
            # so any ";" living inside it can never reach the split below.
            no_comment_lines.append(line[:comment_start])
    # Rejoin into one string so a statement that spans multiple source lines (every CREATE TABLE
    # here does) is still one contiguous piece of text before the semicolon split runs.
    cleaned = "\n".join(no_comment_lines)
    statements = []
    for piece in cleaned.split(";"):
        piece = piece.strip()
        if piece:                                                 # drop the empty tail after the last ';'
            statements.append(piece + ";")
    return statements


def init_ops_db(ops_db_path: Union[str, Path, None] = None) -> None:
    """Create ops.db's five tables and four indexes if the file does not already hold them.

    Idempotent: every statement is CREATE ... IF NOT EXISTS, so running this twice against the
    same file changes neither the schema nor any row (AC-2). Mirrors eventsdb.init_events_db()'s
    own idempotent shape, called from main.py's lifespan beside the three existing init calls
    (brief section 7.1: "opsdb.py follows eventsdb.py's shape ... its own idempotent
    init_ops_db() called from lifespan beside the three existing init calls").
    """
    path = ops_db_path if ops_db_path is not None else DB_PATH
    with ops_transaction(path) as conn:
        for ddl in _DDL_SCRIPTS_ORDERED:
            for statement in _split_ddl_statements(ddl):
                conn.execute(statement)


# =============================================================================
# The import-boundary assertion: a structural test, not a hope (brief section 7.1).
# =============================================================================

class OpsImportBoundaryViolation(RuntimeError):
    """Raised at boot when a research-side module imports opsdb.

    Section 7.1 states the reason directly: keeping monitor state physically out of the research
    data path only works if nothing under research_lab/ or dashboard/backend/research/ can reach
    this module at all, and a convention nobody enforces erodes silently. This exception makes the
    boundary a test rather than a hope, the same way researchdb.DatasetTypeRegistryDriftError
    makes the dataset-type registry's own invariant a test.
    """


class OpsImportBoundaryUnverifiable(RuntimeError):
    """Raised at boot when NEITHER banned directory exists in the tree being checked, so the
    import boundary could not be scanned at all (fold F1, findings list for unit/ops-store).

    A loop that finds zero directories to scan, falls through and returns None reads as "the
    boundary holds" to every caller, when what actually happened is that nothing was checked. A
    guard that passes without checking anything provides no protection while looking exactly like
    one does, which is the named worst outcome for this project's own guard checks. A tree with
    only ONE of the two banned directories is still a legitimate, checkable tree (a probe fixture
    commonly creates just one, and dashboard/backend/research/ is not guaranteed to exist in every
    checkout), so this fires only when NEITHER directory was found, never when one of the two is
    simply absent.
    """


def _file_imports_opsdb(py_path: Path) -> bool:
    """True if the given .py file's own import statements name "opsdb" anywhere.

    Covers both "import dashboard.backend.opsdb" (an ast.Import node whose dotted name ends in
    ".opsdb") and the two "from" forms: "from dashboard.backend import opsdb" (opsdb is one of the
    imported names) and "from dashboard.backend.opsdb import init_ops_db" (opsdb is the module
    itself). Parsed with the stdlib ast module, never a plain substring search, so the word
    "opsdb" appearing inside an unrelated comment or string literal can never produce a false
    positive.
    """
    tree = ast.parse(py_path.read_text(), filename=str(py_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "opsdb" or alias.name.endswith(".opsdb"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and (node.module == "opsdb" or node.module.endswith(".opsdb")):
                return True                                        # "from dashboard.backend.opsdb import X"
            for alias in node.names:
                if alias.name == "opsdb":
                    return True                                    # "from dashboard.backend import opsdb"
    return False


def assert_no_research_import(repo_root: Union[str, Path, None] = None) -> None:
    """Fail loudly if any ".py" file under "<repo_root>/research_lab/" or
    "<repo_root>/dashboard/backend/research/" imports opsdb. Returns None on a clean tree that
    actually got scanned; raises OpsImportBoundaryUnverifiable (fold F1) instead of returning None
    when NEITHER directory exists, because a check that scans nothing must not report "clean".

    repo_root defaults to the real repository root, derived the same three-parents-up way
    config.PROJECT_ROOT derives it from config.py's own location (this file sits in the same
    directory), overridable so a probe can point this at a throwaway fixture tree instead of the
    real repository (brief section 7.1: "Make that a test, not a hope"). Called explicitly in
    main.py's lifespan beside researchdb.assert_dataset_type_registry_consistent(), the same
    "raise here, no try/except" fail-loud-at-boot discipline that function already uses: a drift
    is a code bug that must stop boot, not a best-effort sweep that may fail benignly.
    """
    root = Path(repo_root) if repo_root is not None else PROJECT_ROOT
    banned_dirs = (root / "research_lab", root / "dashboard" / "backend" / "research")
    # F1 fix: track whether at least one banned directory actually existed and got walked, so a
    # tree holding neither one cannot fall through this loop and silently read as "boundary holds".
    any_dir_scanned = False
    for banned_dir in banned_dirs:
        if not banned_dir.is_dir():
            continue                                              # nothing to scan if this tree lacks the dir
        any_dir_scanned = True                                    # this one exists, so the loop below actually checks something
        for dirpath, dirnames, filenames in os.walk(banned_dir):
            # Prune noise directories in place before os.walk descends into them: a virtualenv or
            # a compiled-bytecode cache can hold thousands of unrelated .py files that would slow
            # this boot-time scan for no reason this check exists to serve, and a vendored
            # dependency could coincidentally name something "opsdb" with no bearing on this
            # repository's own boundary.
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                py_path = Path(dirpath) / filename
                if _file_imports_opsdb(py_path):
                    raise OpsImportBoundaryViolation(
                        f"{py_path} imports opsdb, crossing the research/operations boundary "
                        "(brief section 7.1: monitor state must stay physically out of the "
                        "research data path)"
                    )
    # F1 fix: if neither banned directory existed, the loop above scanned nothing and would
    # otherwise fall through and return None, which every caller reads as "the boundary holds".
    # That is a guard that passes without checking anything, so fail loudly and name both paths
    # that were looked for instead of silently reporting a clean tree this check never examined.
    if not any_dir_scanned:
        raise OpsImportBoundaryUnverifiable(
            f"neither {banned_dirs[0]} nor {banned_dirs[1]} exists under {root}, so the "
            "research/operations import boundary could not be checked (brief section 7.1: "
            "a guard that scans nothing must not report itself as passing)"
        )
