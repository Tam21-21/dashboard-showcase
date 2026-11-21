"""research.db access for the reserve exam's own record: an append-only log of exam attempts,
plus the two group C provenance readers the cut guard and the window rule both need, and the
supply query that tells the two apart from "spent."

Lane RE (6.4), docs/plans/night/2026-09-15_set9_night/PLAN.md section 8. Governed by
docs/briefs/dry_run_spec_brief.md section 7 and AC-21 to AC-27. Follows retestdb.py's shape on
purpose (Tamer's ruling, plan section 2's lean): its own module, its own table, its own connection
discipline, initialised from main.py beside init_retest_db(), because research.db already carries
the backup and boot-init machinery a new log needs and a SEVENTH SQLite file would only duplicate
that machinery with no isolation benefit this log needs (the same reasoning retestdb.py's own
docstring gives for research_retests).

DDIA (Kleppmann, second edition, chapter 3, "Event sourcing and CQRS": "Events are immutable; new
events supersede old ones"). research_reserve_exams is an APPEND-ONLY EVENT LOG, one row per exam
ATTEMPT, never mutated: a candidate's exam history must survive a later exam the same way
research_judge_verdicts' own history survives a later grading pass. There is no "current exam"
derived read here the way retestdb.py's current_retest_state reads one, because AC-25/AC-27 never
ask "what is this candidate's exam state" as a single word; reserve_exam_passed below answers the
one question this build's own money-path successor (row 5.1e) actually needs, by reading the
NEWEST row for the (strategy_id, strategy_version) pair, the same "newest wins" idiom every other
append-only log in this codebase already uses.

LIFECYCLE (the four questions this codebase asks of every stateful store), answered here:

  Fresh/first-run: a freshly initialised research.db holds zero research_reserve_exams rows.
  query_reserve_supply reads zero as spent against every enumerated window; reserve_exam_passed
  reads False for every candidate (no row to find).

  Empty set: a candidate that was refused before any exam ran (final-form-verdict-absent,
  final-form-not-survivor, final-form-params-unrecorded, reserve-grid-forbidden,
  reserve-seed-absent, reserve-supply-exhausted, ChronologyRefused, or any stage_runner refusal)
  writes NO row here at all: this table only ever records an exam that actually reached a real
  dispatch, never an attempt. reserve_exams_without_verdict (this lane's own monitor row) is a
  DIFFERENT population: an exam that DID dispatch, whose run has not yet been judged.

  NULL/absent: lead_in_reaches_window_index is NULL for the first enumerated window (nothing
  earlier than the cut is a reserve window for a lead-in to reach into) and non-NULL for every
  other window this build enumerates today (see reserve_exam.py's own docstring for why 278 bars
  always reaches at least one window back).

  T-zero, never acted on: before init_reserve_db has ever run on a given file, this table does not
  exist and every function below raises a bare sqlite3.OperationalError naming the missing table,
  with ONE exception added by lane EF (6.1h): reserve_exam_fitted_run_id, called from the GENERAL
  judge grading path rather than from this module's own reserve-exam-specific callers, checks
  sqlite_master first and reads (False, None) rather than raising (see its own docstring for why
  this is the correct answer, not an inconsistency). init_reserve_db is therefore always the first
  call any OTHER caller of this module makes, exactly as judgedb.init_judge_db and
  retestdb.init_retest_db already are (called once, at boot, from main.py's lifespan, idempotent
  thereafter).
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from .config import PARAMETER_PROVENANCE_PATH, RESEARCH_DB_PATH
from .judgedb import _PASS as SURVIVOR_VERDICT_WORD  # round 3 fold, SF1: import the survivor word rather than restate it (judgedb imports nothing from here, so no cycle), reserve_exam.py's own precedent
from .provenance_parse import _parse_provenance_bodies  # the same body parser researchdb.py already imports back into its own namespace

# Module-level default path; callers pass an explicit override when they need one (probes do,
# matching judgedb.py's and retestdb.py's own DB_PATH convention).
DB_PATH = RESEARCH_DB_PATH

# The two group C provenance keys this lane's own entries own (#399/#400). Named once here so the
# reader functions below and any future caller never spell either key differently.
RESERVE_CUT_DATE_KEY = "reserve.cut_date"
RESERVE_EXAM_WINDOW_MONTHS_KEY = "reserve.exam_window_months"

# AC-22's own seed_purpose word, the one tracker.py's request_draw records on the exam's own draw
# row when reserve_exam.py's context manager is active. Named once here (never restated in
# tracker.py or reserve_exam.py as a bare literal) so all three sites can only ever agree.
RESERVE_EXAM_SEED_PURPOSE = "reserve_exam_draw"


# =============================================================================
# Errors. One base class, one subclass per refusal shape, matching retestdb.py's own convention.
# =============================================================================

class ReserveError(Exception):
    """Base class for every refusal this module raises. Never raised directly."""


class ReserveDbParentDirectoryMissing(ReserveError):
    """research_db_path's parent directory does not exist (judgedb.py's own F4 pattern, restated
    here so the failure carries the path rather than surfacing as a bare sqlite3.OperationalError).
    """


class ReserveParameterOwed(ReserveError):
    """A group C reserve provenance entry (the cut date or the exam window) is missing, carries
    no Bounds subsection, or its Bounds body is the literal word OWED (#315 group C: "Policy,
    ruled by Tamer, never tested"; R-U6's own three-case NULL/ABSENT reading, restated for this
    lane's own two keys rather than reusing research_lab.universe's copy, since that module may
    never import dashboard.backend and this one already does). Never a substituted default."""

    def __init__(self, key: str) -> None:
        self.key = key  # the offending provenance key, named for the caller
        super().__init__(
            f"reserve provenance entry {key!r} in {PARAMETER_PROVENANCE_PATH} is owed or absent; "
            "refusing to guess a value for a group C policy fact"
        )


# =============================================================================
# The two group C provenance readers (criterion 1). Read LIVE from the committed markdown text on
# every call, never cached at import time, matching stage_runner.daily_store_path()'s own "read
# live" discipline for an environment-resolved fact: a change to the register between two calls
# must be honoured immediately, and a process that never restarts must never serve a stale value.
# =============================================================================

def _read_reserve_bounds_text(key: str) -> str:
    """The raw Bounds body (still markdown, not yet parsed to a value) for one '## key' entry in
    the parameter-provenance register. Raises ReserveParameterOwed for the heading missing
    entirely, the Bounds subsection missing or empty, or a Bounds body that starts with the
    literal word OWED (the register's own convention, matching research_lab.universe's identical
    three-case reading of the same three failure shapes)."""
    text = PARAMETER_PROVENANCE_PATH.read_text(encoding="utf-8")
    bodies = _parse_provenance_bodies(text)  # {'## key': {subsection: body}}, the same parser researchdb._check_parameter_provenance uses
    entry = bodies.get(key)
    if entry is None:
        raise ReserveParameterOwed(key)
    bounds = entry.get("Bounds", "").strip()
    if not bounds or bounds.upper().startswith("OWED"):
        raise ReserveParameterOwed(key)
    return bounds


def read_reserve_cut_date_ts() -> int:
    """The reserve cut date (#318 ruling 1: "Everything from 2023-01-01 onwards is reserve"), as
    a UTC-midnight epoch-second timestamp, read live from reserve.cut_date's own Bounds line. The
    ONE reader both tracker.py's cut guard and reserve_exam.enumerate_windows call, so the two can
    never disagree about where training ends and reserve begins."""
    bounds = _read_reserve_bounds_text(RESERVE_CUT_DATE_KEY)
    match = re.search(r"\d{4}-\d{2}-\d{2}", bounds)  # the entry's own value is stated as **YYYY-MM-DD**, RULED, not argued
    if match is None:  # a Bounds body that parsed but carries no date at all: the same refusal as owed
        raise ReserveParameterOwed(RESERVE_CUT_DATE_KEY)
    cut_date = datetime.strptime(match.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(cut_date.timestamp())


def read_reserve_exam_window_months() -> int:
    """The reserve exam's own window length in whole calendar months (#329 ruling 5: "THE
    RESERVE EXAM STAYS SIX MONTHS"), read live from reserve.exam_window_months's own Bounds line."""
    bounds = _read_reserve_bounds_text(RESERVE_EXAM_WINDOW_MONTHS_KEY)
    match = re.search(r"\d+", bounds)  # the entry's own value is stated as **6**, in WHOLE MONTHS
    if match is None:
        raise ReserveParameterOwed(RESERVE_EXAM_WINDOW_MONTHS_KEY)
    return int(match.group(0))


# =============================================================================
# Connection and transaction helpers, mirroring retestdb.py's own _connect_retest /
# get_retest_conn / _retest_transaction (itself mirroring judgedb.py, itself mirroring opsdb.py).
# =============================================================================

def _connect_reserve(research_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on research.db with the per-connection pragmas this module relies on."""
    path = Path(research_db_path)
    if not path.parent.is_dir():
        raise ReserveDbParentDirectoryMissing(
            f"cannot open research.db at {path}: its parent directory {path.parent} does not exist"
        )
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_reserve_conn(research_db_path: Union[str, Path, None] = None):
    """Context manager for a read-only query or a single-statement write on research.db. One
    connection per call, no pooling, matching retestdb.get_retest_conn's own reasoning."""
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_reserve(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _reserve_transaction(research_db_path: Union[str, Path, None] = None):
    """Wraps BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection, mirroring
    retestdb._retest_transaction. init_reserve_db and record_exam both go through this, so a
    failure partway through a write leaves nothing behind."""
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_reserve(path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except sqlite3.Error:
            # Narrowed from a bare except Exception (hard prohibition on unspecific exception
            # types), the same narrowing retestdb.py's own transaction wrapper carries: every
            # statement this module runs inside a transaction is a static SQL string or a
            # bind-parameterised INSERT, so every failure reachable here is already a
            # sqlite3.Error.
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


def _split_ddl_statements(script: str) -> list:
    """Split a multi-statement DDL string into individual statements, mirroring judgedb.py's and
    retestdb.py's own byte-identical copy of this helper (each store module carries its own
    private copy rather than importing a neighbour's)."""
    no_comment_lines = []
    for line in script.splitlines():
        comment_start = line.find("--")
        if comment_start == -1:
            no_comment_lines.append(line)
        else:
            no_comment_lines.append(line[:comment_start])
    cleaned = "\n".join(no_comment_lines)
    statements = []
    for piece in cleaned.split(";"):
        piece = piece.strip()
        if piece:
            statements.append(piece + ";")
    return statements


# =============================================================================
# Schema DDL (AC-26, AC-27). research_reserve_exams follows retestdb.py's research_retests shape:
# APPEND ONLY, keyed by its own draw, in the SAME file as the draws and verdicts it references
# (Kleppmann chapter 4's own concatenated-index argument, restated here for one table rather than
# a composite index: this table's own natural key IS the draw_id, so a UNIQUE index on it is the
# whole story, never a composite prefix).
# =============================================================================

_DDL_RESERVE_EXAMS = """
CREATE TABLE IF NOT EXISTS research_reserve_exams (
    exam_id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    -- draw_id: the tracker's own draw_requested row this exam's look was granted against.
    -- UNIQUE, so a re-delivered dispatch for the same draw (which the chronology gate itself
    -- already refuses as a second look, ChronologyRefused, criterion 9's own lifecycle answer)
    -- can never double-write this table even if that gate were ever bypassed.
    draw_id                       INTEGER NOT NULL UNIQUE,
    strategy_id                   INTEGER NOT NULL,   -- denormalized, no FK: survives a strategy delete, matching research_data_draws' own idiom
    strategy_version              INTEGER NOT NULL,   -- the GRADED version this exam ran (dispatch_stage's own pinned_version)
    window_index                  INTEGER NOT NULL,   -- which of the enumerated reserve windows (1-based) was drawn
    window_start_ts               INTEGER NOT NULL,   -- unix seconds UTC, the drawn window's own bounds
    window_end_ts                 INTEGER NOT NULL,
    seed                          INTEGER NOT NULL,   -- the seed that chose this window (AC-21: the same seed reproduces the same window)
    lead_in_reaches_window_index  INTEGER,             -- NULL for window 1 (nothing earlier is reserve); the earlier window index otherwise (AC-27)
    run_id                        INTEGER NOT NULL,   -- the dispatched run this exam's verdict, if any, will grade
    -- fitted_run_id (lane EF, 6.1h, docs/plans/night/2026-09-19/PLAN.md section 7): the run whose
    -- parameters were FITTED, the same run_id dispatch_exam already reads off the newest
    -- non-advisory verdict (research_judge_verdicts.run_id) and, before this lane, discarded. One
    -- additive nullable column on an APPEND-ONLY event log (DDIA, Kleppmann second edition,
    -- chapter 3, "Event sourcing and CQRS": "Events are immutable"): destroys nothing, needs no
    -- new index because ix_reserve_exams_run_id below already answers the one query that reads
    -- it (WHERE run_id = ?). Frozen at the moment dispatch_exam actually used it, never re-derived
    -- at grade time: re-running the newest-verdict query at grade time is time-varying, so a later
    -- verdict on the same strategy would change which run check 1 reads and a re-grade of the same
    -- exam could answer differently, which is the alternative this lane considered and rejected.
    -- NULL for every exam row written before this column existed (the t-zero lifecycle answer).
    fitted_run_id                 INTEGER,
    created_at                    INTEGER NOT NULL    -- unix seconds UTC, the append time
);
CREATE INDEX IF NOT EXISTS ix_reserve_exams_strategy
    ON research_reserve_exams(strategy_id, strategy_version, exam_id);
CREATE INDEX IF NOT EXISTS ix_reserve_exams_run_id ON research_reserve_exams(run_id);
"""


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, column_def: str) -> None:
    """Add a column via ALTER TABLE; no-op if it already exists.

    Byte-for-byte the same idiom as researchdb._add_column_if_missing (line 1443 there), copied
    rather than imported: this module's own docstring already establishes that each store module
    carries its own private copy of a shared shape (_split_ddl_statements above is the same
    pattern), and this is reservedb.py's first migration, so it had no copy of its own until now
    (lane EF, 6.1h, docs/plans/night/2026-09-19/PLAN.md section 7: "reservedb.py has no migration
    helper today; researchdb._add_column_if_missing is the idiom to follow"). SQLite's own ALTER
    TABLE ADD COLUMN errors if the column exists, so PRAGMA table_info is checked first.
    """
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}  # row[1] is the column name
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


def init_reserve_db(research_db_path: Union[str, Path, None] = None) -> None:
    """Create research_reserve_exams and its two indexes if they do not already exist, then
    migrate an existing (pre-lane-EF) copy of the table to carry fitted_run_id.

    Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS plus a guarded ALTER TABLE, so calling
    this twice on the same file changes neither the schema nor any row, matching
    retestdb.init_retest_db's own shape. A FRESH file gets fitted_run_id from _DDL_RESERVE_EXAMS's
    own CREATE TABLE above, so the ALTER below no-ops on it; an EXISTING file (research.db already
    carrying research_reserve_exams from before this lane) gets the column added, nullable, every
    already-written row reading NULL for it (SQLite's own ALTER TABLE ADD COLUMN semantics: an
    added column with no DEFAULT clause backfills every existing row with NULL), which is this
    lane's own t-zero lifecycle answer applied to the migration itself.
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    with _reserve_transaction(path) as conn:
        for statement in _split_ddl_statements(_DDL_RESERVE_EXAMS):
            conn.execute(statement)
        _add_column_if_missing(conn, "research_reserve_exams", "fitted_run_id", "INTEGER")


# =============================================================================
# The single write entry point (AC-26, AC-27).
# =============================================================================

def record_exam(
    *,
    draw_id: int,
    strategy_id: int,
    strategy_version: int,
    window_index: int,
    window_start_ts: int,
    window_end_ts: int,
    seed: int,
    lead_in_reaches_window_index,
    run_id: int,
    fitted_run_id=None,
    created_at: int,
    research_db_path: Union[str, Path, None] = None,
) -> int:
    """Append ONE research_reserve_exams row (AC-26: "the window actually drawn is recorded";
    AC-27: "the run RECORDS that fact"). Called by reserve_exam.dispatch_exam immediately after
    stage_runner.dispatch_stage (frozen for this lane) has already completed its own three
    transactions (request_draw, dispatch_sweep, link_run): this write is therefore its OWN, later,
    independent transaction rather than literally nested inside link_run's, the same shape
    tracker.record_bars_observed already uses for the SAME reason (tracker.py's own docstring:
    "A fourth, independent transaction... appends the bars-consumed observation once the run has
    actually reported"). draw_id's UNIQUE constraint is this table's own belt for the same buckle
    the chronology gate already fastens (see the DDL comment above).

    fitted_run_id (lane EF, 6.1h): the run whose parameters were fitted, i.e. the newest
    non-advisory verdict's own run_id dispatch_exam already read to admit this candidate and to
    fetch its single param_json combo. dispatch_exam (this module's own production caller) always
    passes the exact value it already holds. Defaults to None (MEASURED regression, fixed in the
    same change it was found in: research_lab/probes/reserve_exam/ac11_draws_without_exam_record's
    own direct call predates this column and calls record_exam with no fitted_run_id keyword at
    all; a required keyword-only argument with no default would have broken that call, an existing
    passing probe outside this lane's own fence, for a concern -- the durability gap between
    dispatch_stage's own three transactions and this one -- that has nothing to do with fitted-run
    provenance). A caller that never supplies it writes NULL, exactly this column's own documented
    t-zero shape (see the DDL comment above), never a guessed non-NULL value.
    """
    with _reserve_transaction(research_db_path) as conn:
        row = conn.execute(
            "INSERT INTO research_reserve_exams "
            "    (draw_id, strategy_id, strategy_version, window_index, window_start_ts, "
            "     window_end_ts, seed, lead_in_reaches_window_index, run_id, fitted_run_id, "
            "     created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING exam_id;",
            (draw_id, strategy_id, strategy_version, window_index, window_start_ts,
             window_end_ts, seed, lead_in_reaches_window_index, run_id, fitted_run_id, created_at),
        ).fetchone()
        return int(row["exam_id"])


# =============================================================================
# The supply query (criterion 2) and the money-path reader row 5.1e wires in (criterion 5).
# =============================================================================

def query_reserve_supply(conn: sqlite3.Connection, now_ts: int) -> dict:
    """The enumerated reserve windows and, for each, whether it is SPENT (#302: "windows are
    disjoint while supply lasts"; #329 ruling 7: "A RESERVE WINDOW IS SPENT BY A VERDICT, NOT BY
    A LOOK"). A window is spent when a granted, budget-counting ('real' calendar,
    counts_against_budget = 1) draw_requested row at stage='reserve' over EXACTLY that window
    (both bounds equal, never merely overlapping) is linked (any run_linked row naming a run) to a
    run for which at least one research_judge_verdicts row exists, WHATEVER its outcome (#329's
    own words: "every exam is counted in the luck accounting", which starts the moment a verdict
    lands, never only on a pass). GLOBAL across every candidate (#302), never scoped to one
    strategy: a window a DIFFERENT candidate's verdict already spent is unavailable to this one.

    Local import of reserve_exam, never at module level: this module and reserve_exam.py import
    each other in opposite directions (reserve_exam needs the store's read/write calls; this
    query needs the rule's own window enumeration), and a function-scoped import resolves the
    already-loaded module from sys.modules at call time rather than forcing one particular
    load order at import time, which is how research_lab/probes/tracker's own idiom of importing
    dashboard.backend modules only once they are needed already avoids the same class of problem.
    """
    from . import reserve_exam  # local import: see docstring

    windows = reserve_exam.enumerate_windows(now_ts)
    spent_rows = conn.execute(
        "SELECT DISTINCT d.window_start_ts AS window_start_ts, d.window_end_ts AS window_end_ts "
        "FROM research_data_draws d "
        "JOIN research_data_draws l "
        "  ON l.event_kind = 'run_linked' AND l.draw_id = d.id "
        "JOIN research_judge_verdicts v ON v.run_id = l.run_id "
        "WHERE d.event_kind = 'draw_requested' AND d.decision = 'granted' "
        "  AND d.stage = 'reserve' AND d.counts_against_budget = 1;"
    ).fetchall()
    spent_bounds = {(r["window_start_ts"], r["window_end_ts"]) for r in spent_rows}
    rows = []
    for w in windows:
        rows.append({
            "index": w.index, "start_ts": w.start_ts, "end_ts": w.end_ts,
            "spent": (w.start_ts, w.end_ts) in spent_bounds,
        })
    return {
        "enumerated": len(rows),
        "spent": sum(1 for r in rows if r["spent"]),
        "windows": rows,
    }


def reserve_exams_without_verdict(conn: sqlite3.Connection) -> "list[int]":
    """This lane's own monitor row (docs/units/RE/monitor_rows.md): an exam that DID dispatch
    (a real research_reserve_exams row exists) whose run has not yet been judged (no
    research_judge_verdicts row names its run_id yet). A look not yet spent, never an error by
    itself: the ordinary state of an exam between dispatch and the judge's own grading pass."""
    rows = conn.execute(
        "SELECT exam_id FROM research_reserve_exams e "
        " WHERE NOT EXISTS (SELECT 1 FROM research_judge_verdicts v WHERE v.run_id = e.run_id);"
    ).fetchall()
    return [r["exam_id"] for r in rows]


def reserve_draws_without_exam_record(conn: sqlite3.Connection) -> "list[int]":
    """Round 2 fold, F1 (docs/units/RE/reads/read_1.md): the durability gap between
    stage_runner.dispatch_stage's own three transactions (request_draw, dispatch_sweep, link_run,
    all frozen for this lane) and record_exam's own FOURTH, later one (reserve_exam.py's own
    docstring, record_bars_observed's precedent). A crash or a lock loss between the two leaves a
    GRANTED reserve-stage draw, already linked to a real run, with no research_reserve_exams row
    naming it: reserve_exam_passed reads False for a candidate whose exam actually ran, and
    reserve_exams_without_verdict cannot see it either, because that reader starts from
    research_reserve_exams rows, and this population by definition has none. Named by the draw's
    own id (research_data_draws.id on the granted draw_requested row), since that is the one row
    that survives the gap either way.

    Reproducing SQL (docs/units/RE/monitor_rows.md carries this verbatim): a granted draw_requested
    row at stage='reserve' that has a run_linked row naming it, and for which no
    research_reserve_exams row exists at draw_id equal to that draw's own id. Mirrors
    query_reserve_supply's own two-alias self-join over research_data_draws (d the draw, l its
    link), so the two queries can never silently disagree about what "linked" means.
    """
    rows = conn.execute(
        "SELECT d.id AS draw_id "
        "FROM research_data_draws d "
        "JOIN research_data_draws l ON l.event_kind = 'run_linked' AND l.draw_id = d.id "
        "WHERE d.event_kind = 'draw_requested' AND d.decision = 'granted' AND d.stage = 'reserve' "
        "  AND NOT EXISTS (SELECT 1 FROM research_reserve_exams e WHERE e.draw_id = d.id);"
    ).fetchall()
    return [r["draw_id"] for r in rows]


def reserve_exam_passed(conn: sqlite3.Connection, strategy_id: int, strategy_version: int) -> bool:
    """Whether this exact (strategy_id, strategy_version) candidate's reserve exam, if one has
    run, cleared the ordinary judge (the spec's own words: "the exam's verdict is the ordinary
    judge's over the exam run"). Reads the NEWEST research_reserve_exams row for this pair, then
    the newest non-advisory research_judge_verdicts row for that exam's own run_id. False when no
    exam has run yet for this exact pair, or the exam's own run has no verdict yet (a look not yet
    spent; see reserve_exams_without_verdict above). This is the reader row 5.1e wires into
    real_orders.evaluate_candidate_chain's own reserve_exam_ok leg (money path, not this lane's own
    scope); it is built and exposed here, unused elsewhere in this build, so that future lane
    imports a single, already-tested implementation rather than writing a second one.
    """
    exam_row = conn.execute(
        "SELECT run_id FROM research_reserve_exams "
        " WHERE strategy_id = ? AND strategy_version = ? ORDER BY exam_id DESC LIMIT 1;",
        (strategy_id, strategy_version),
    ).fetchone()
    if exam_row is None:
        return False
    verdict_row = conn.execute(
        "SELECT verdict FROM research_judge_verdicts WHERE run_id = ? AND is_advisory = 0 "
        " ORDER BY graded_at DESC, id DESC LIMIT 1;",
        (exam_row["run_id"],),
    ).fetchone()
    return verdict_row is not None and verdict_row["verdict"] == SURVIVOR_VERDICT_WORD


def reserve_exam_fitted_run_id(conn: sqlite3.Connection, run_id: int) -> "tuple[bool, int | None]":
    """Lane EF (6.1h): tells judge_checks.check_1_evidence_floor whether run_id is a reserve exam
    run and, if so, which run its parameters were fitted on. Returns (is_exam_run, fitted_run_id).

    (False, None): run_id names no research_reserve_exams row at all, so it is an ORDINARY run;
      check 1 grades it exactly as it always has, from its own record.
    (True, None): run_id IS a reserve exam run whose fitted_run_id was never recorded (every exam
      row written before this lane's own migration; see the DDL comment above). Tamer's own
      refusal condition: the caller refuses check 1 by name rather than skipping it quietly.
    (True, <int>): run_id IS a reserve exam run and its fitted run is this id. The caller reads
      Odf/Udf from THIS run's own funnel and variation rows instead of run_id's.

    LIFECYCLE, this reader's own four answers (a different question from research_reserve_exams'
    own module docstring above, which answers them for the TABLE; this answers them for THIS
    QUERY):

      Fresh/first-run. A freshly initialised research.db (init_reserve_db has run, zero rows in
      research_reserve_exams) reads (False, None) for every run_id: no exam has ever dispatched.

      Empty set. Identical to fresh/first-run above: an empty table and a table with rows none of
      which name this run_id both read (False, None), which is correct, since in both cases no
      exam row exists for run_id.

      NULL/absent. An exam row exists (is_exam_run=True) but its own fitted_run_id column reads
      NULL: (True, None), the refuse-by-name case documented above.

      T-zero, never acted on. UNIQUE among every reader in this module (see each one's own
      docstring: "every function below raises a bare sqlite3.OperationalError naming the missing
      table" before init_reserve_db has ever run): this ONE reader tolerates
      research_reserve_exams not existing on the file AT ALL, reading (False, None) rather than
      raising, checked against sqlite_master first (eventsdb.py's own precedent for a guarded
      table-existence check: "SELECT 1 FROM sqlite_master WHERE type='table' AND name=..."). This
      is deliberate and necessary, not an inconsistency: every OTHER reader here is only ever
      called from reserve-exam-specific code (reserve_exam.py, routes/reserve.py) whose own
      callers already guarantee init_reserve_db ran first (main.py's lifespan, unconditionally,
      every boot). This reader is called from judge_checks.check_1_evidence_floor, the GENERAL
      judge grading path, which must stay correct for a research.db where the reserve-exam
      subsystem was never touched at all -- and a table that was never created holds, by
      definition, exactly as many exam rows as an existing table with zero rows: none. Treating
      the two alike is not an approximation, it is the correct reading of "no exam is recorded
      for run_id" either way.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'research_reserve_exams';"
    ).fetchone()
    if table_exists is None:
        return False, None
    exam_row = conn.execute(
        "SELECT fitted_run_id FROM research_reserve_exams WHERE run_id = ?"
        " ORDER BY exam_id DESC LIMIT 1;",
        (run_id,),
    ).fetchone()
    if exam_row is None:
        return False, None
    return True, exam_row["fitted_run_id"]
