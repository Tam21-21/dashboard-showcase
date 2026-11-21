"""research.db access for the judge's verdict store: research_judge_verdicts, its single write
entry point and the rule that composes nine check outcomes into one verdict.

Night seven, lane J, docs/handoffs/2026-09-03_night_seven/11_job_lane_J_judge_unit_1.md. Governed
by docs/briefs/judge_architecture_2026-08-31.md (sections 2, 2b, 3) and
docs/briefs/judge_spec_brief.md (sections 1 and 2). This unit builds the STORE ONLY: none of the
nine checks, no threshold, no cardinal number. A future unit builds the checks and the shared
grading entry point (architecture section 3b) that calls compose_verdict below and then
record_verdict below with the result; this module never calls either of the nine checks and never
will.

Mirrors dashboard/backend/opsdb.py's own shape, the closest live precedent in this tree for a
brand-new, bounded SQLite store built as its own module: "its own connection helper, its own
transaction context manager, its own idempotent init function" (opsdb.py's own docstring), the same
convention the job file's own lean asks this module to follow.

Connection discipline mirrors opsdb.py's own three points, restated here because they hold for this
module for the same reasons:
  1. foreign_keys is per-connection in SQLite, so it is set on every new connection, even though no
     column of research_judge_verdicts carries a foreign key of its own (every reference in this
     table is denormalized, architecture section 2).
  2. journal_mode=WAL is persistent at the database-file level; re-setting it is a cheap no-op.
  3. isolation_level=None means this module manages transactions explicitly. record_verdict's own
     single INSERT statement commits itself under SQLite's autocommit behaviour with no explicit
     BEGIN/COMMIT needed, matching get_ops_conn's own documented "a single-statement write" case;
     init_judge_db's own multi-statement DDL uses the private _judge_transaction wrapper below for
     exactly the reason ops_transaction exists in opsdb.py.

research_db_path is explicit on every entry point (never a hidden global), the same reason
eventsdb.py and opsdb.py both give: a probe, or a future caller, can point this module at a
throwaway file instead of the real research.db.

LIFECYCLE (AC-10), the four questions this codebase's own convention asks of every stateful store,
answered here for the TABLE in lifecycle-checker's own vocabulary. compose_verdict below carries
its own four answers for the GRADING PASS, which is a different stateful thing (a pass has no rows
of its own; it reads and composes, it does not persist).

  Fresh/first-run: a freshly initialised research.db holds zero research_judge_verdicts rows
  (init_judge_db only creates the table and its index; it writes no seed row), and the narrowed
  "has this one cell been graded" read (architecture section 4 case 1) returns no row rather than
  raising or fabricating a verdict. The first call to record_verdict behaves exactly as the
  millionth: there is no counter to initialise and no prior row this module consults before
  writing.

  Empty set: this table has no empty-set state of its own to answer beyond "zero rows", covered by
  fresh/first-run above; a grading pass's own empty-set question belongs to compose_verdict.

  NULL/absent: every column of research_judge_verdicts is NOT NULL (architecture section 2), so a
  caller that omits one is refused at the schema level, not written with a placeholder.
  evidence_floor_outcome carries this rule for a reason of its own, stated beside it in the DDL
  below: a NULL there would reintroduce a fourth state outside the closed set of three.

  T-zero, never acted on: before any grading pass has ever run, research_judge_verdicts holds no
  rows, compose_verdict has never been called and no candidate has a verdict at all. That silence
  is not read as health by this module (telling "never graded" apart from "graded, none passed" is
  the ungraded-completed-run monitor's own job, a later unit); this module reports only what the
  table holds and never guesses a default from an absence.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from .config import RESEARCH_DB_PATH

# Module-level default path; callers pass an explicit override when they need one (probes do,
# matching opsdb.py's own DB_PATH convention).
DB_PATH = RESEARCH_DB_PATH


# =============================================================================
# Errors. "JudgeError with one subclass per refusal, never a bare Exception" (job file, "The
# module's public shape"). Every exception this module raises descends from JudgeError, so a
# caller can write one except clause to mean "the judge's verdict store refused something."
# =============================================================================

class JudgeError(Exception):
    """Base class for every refusal this module raises. Never raised directly."""


class JudgeDbParentDirectoryMissing(JudgeError):
    """Raised when research_db_path's parent directory does not exist, before sqlite3 ever opens
    it. Mirrors opsdb.py's own OpsDbParentDirectoryMissing (its own F4 fix): without this, a
    missing parent surfaces only as a bare sqlite3.OperationalError with no path named in it.
    """


class EmptyCheckOutcomesError(JudgeError):
    """Raised by compose_verdict when check_outcomes is empty. Spec brief section 2's own
    composition-rule lifecycle answer, quoted verbatim: "A grading pass whose recorded outcome set
    is EMPTY produces no verdict and fails loudly."
    """


class EmptyCoordinateBRosterError(JudgeError):
    """Raised by compose_verdict when battery_checks names check 1 alone, leaving coordinate B's
    own roster empty. Spec brief section 2, quoted verbatim: "an empty coordinate-B roster is NOT
    a legitimate battery version and the composition FAILS LOUDLY, writing no verdict..."
    """


class VerdictConstraintViolation(JudgeError):
    """Raised by record_verdict when the underlying INSERT fails a table constraint other than the
    exactly-once duplicate key, for example a NOT NULL column omitted or an unrecognised verdict
    value tripping the CHECK constraint. The original sqlite3.IntegrityError is chained as this
    exception's own cause, so no debugging context is lost, and no bare sqlite3 exception escapes
    this module's own write entry point either way.
    """


# =============================================================================
# Connection and transaction helpers, mirroring opsdb.py's own _connect_ops / get_ops_conn /
# ops_transaction.
# =============================================================================

def _connect_judge(research_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on research.db with the per-connection pragmas this module relies on."""
    path = Path(research_db_path)
    # Checked up front so a missing parent directory names itself in the error rather than
    # surfacing as a bare sqlite3.OperationalError, the same F4 reasoning opsdb.py already carries.
    # This module never creates the directory itself, which would be an unrequested fallback.
    if not path.parent.is_dir():
        raise JudgeDbParentDirectoryMissing(
            f"cannot open research.db at {path}: its parent directory {path.parent} does not exist"
        )
    # Autocommit mode at the driver level; this module manages multi-statement transactions itself
    # via _judge_transaction below, and a single-statement write commits on its own.
    conn = sqlite3.connect(str(path), isolation_level=None)
    # foreign_keys is per-connection in SQLite: without this, an FK violation silently no-ops.
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL is persistent at the file level once set; re-running this on an already-WAL file is free.
    conn.execute("PRAGMA journal_mode = WAL;")
    # Dict-like rows so a caller reads row["column"] instead of indexing by position.
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_judge_conn(research_db_path: Union[str, Path, None] = None):
    """Context manager for a read-only query or a single-statement write on research.db.

    Named in the job file, "mirroring get_ops_conn". Per-call lifecycle: opens a fresh connection
    on enter, closes it on exit. No singleton, no pooling, the same reasoning get_ops_conn's own
    docstring gives: a long-lived shared connection is exactly the hidden state a probe pointed at
    a throwaway file must not inherit. record_verdict below is written to be called exactly this
    way, one connection per call.
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_judge(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _judge_transaction(research_db_path: Union[str, Path, None] = None):
    """Wraps BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection, mirroring opsdb.py's own
    ops_transaction. Private: init_judge_db below is this module's only multi-statement writer
    today (creating the table and its index atomically, so a failure partway through leaves
    neither behind); record_verdict's own single INSERT commits itself under get_judge_conn's
    autocommit connection and needs no wrapper of its own.
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_judge(path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except sqlite3.Error:
            # Narrowed from a bare except Exception (hard prohibition on unspecific exception
            # types): the sole caller, init_judge_db below, only ever runs conn.execute() over
            # the hardcoded, static DDL string above, so every failure reachable here already is
            # a sqlite3.Error; narrowing costs no coverage and stops this handler from also
            # masking an unrelated, non-SQLite bug behind a rollback-and-reraise.
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


# =============================================================================
# Schema DDL: one table, one index (architecture section 2, section 2b item 5). AC-1's own
# exactly-seventeen-column list, and no others invented.
# =============================================================================

# research_judge_verdicts: one row per graded CELL per grading pass (architecture section 3 item
# 3), never per run and never per check. A CELL is the pair (variation_id, ticker) within one run;
# a check that fails records its own outcome in check_outcomes_json on the SAME row rather than
# forking the verdict. This is an append-only fact table in DDIA's own sense (architecture section
# 2b, quoting Kleppmann, second edition, chapter 3, "Event sourcing and CQRS": "Events are
# immutable; new events supersede old ones."). A row is never edited and never deleted: a later
# grading pass appends a new row that supersedes it, which is what the shortlist read (item 4
# below) filters for rather than what any write path here enforces.
#
# Every one of the seventeen columns is NOT NULL (architecture section 2's own nullability
# paragraph states every column's own constraint, and every one of them reads NOT NULL). Reasons
# are stated per column below rather than left for a reader to infer.
_DDL_VERDICTS = """
CREATE TABLE IF NOT EXISTS research_judge_verdicts (
    -- Ordinary SQLite rowid alias. Architecture section 2b item 1 states this table carries no
    -- AUTOINCREMENT, matching every table in researchdb.py.
    id                       INTEGER PRIMARY KEY,

    -- run_id: the source sweep run that produced the cell, denormalized with no foreign key,
    -- following research_trials' own survival idiom so a verdict is not lost if its run row is
    -- later cleaned up. Part of the exactly-once key (item 1 below).
    run_id                   INTEGER NOT NULL,

    -- judging_pass_id: the version-4 UUID text minted once by whichever decider decided to grade
    -- (architecture section 2b item 1), carried unchanged into any retry of that same decision.
    -- Part of the exactly-once key.
    judging_pass_id          TEXT    NOT NULL,

    -- variation_id, ticker: the CELL grain, matching research_run_cells' and research_trials' own
    -- row grain. ticker is never the empty string a 'group' aggregate row would carry, because the
    -- judge grades cells, never a group aggregate. Both are part of the exactly-once key AND the
    -- five-column candidate key (item 2 below).
    variation_id             INTEGER NOT NULL,
    ticker                   TEXT    NOT NULL,

    -- graded_formula_version: the fourth field of the research_full_metrics_cache key of the saved
    -- replay the battery actually read, copied from the cache key the battery resolved, never
    -- recomputed at write time. Part of the five-column candidate key.
    graded_formula_version   TEXT    NOT NULL,

    -- strategy_id, strategy_version: identify the strategy and its version graded. Part of the
    -- five-column candidate key.
    strategy_id              INTEGER NOT NULL,
    strategy_version         INTEGER NOT NULL,

    -- dataset_sha256: sourced per CELL, the same way research_trials sources its own column of
    -- this name, never the run-level hash. Part of the five-column candidate key.
    dataset_sha256           TEXT    NOT NULL,

    -- verdict: the composed result, exactly one of the three the vocabulary names (spec brief
    -- section 1). Enforced by a CHECK constraint (AC-3), so a fourth value is unwritable rather
    -- than merely undocumented. This is the ONLY enum-shaped CHECK constraint this build adds:
    -- AC-3 is the only acceptance criterion naming a CHECK constraint and it names verdict only,
    -- so decided_by and evidence_floor_outcome below carry no CHECK of their own.
    verdict                  TEXT    NOT NULL CHECK (verdict IN ('pass', 'fail', 'unjudgeable')),

    -- is_advisory: #214's stamped-advisory flag, set by the same write to 1 when decided_by is
    -- 'advisory' and to 0 otherwise. This module's own record_verdict takes it as a caller-
    -- supplied value: DERIVING it from decided_by is the calling decider's own job (architecture
    -- section 3b), out of this unit's scope. Excluded from the shortlist and from "current row"
    -- comparisons via is_advisory = 0 (item 3, item 4 below).
    is_advisory              INTEGER NOT NULL,

    -- battery_version: the frozen battery and threshold set this row was graded against (#193's
    -- freeze), what makes the freeze auditable after the fact (AC-7).
    battery_version           TEXT    NOT NULL,

    -- evidence_floor_outcome: check 1's own outcome, promoted to a typed column because it is the
    -- outcome the composition rule's first coordinate reads and the one a monitor queries
    -- directly. TEXT over the same closed set of three every check's outcome is drawn from, never
    -- a boolean: a NULL here would reintroduce a fourth state outside that set, which is why this
    -- column is NOT NULL like every other, even though AC-7 does not name it by name (AC-10's own
    -- NULL/absent answer for the table, in this module's own docstring above).
    evidence_floor_outcome    TEXT    NOT NULL,

    -- check_outcomes_json: a JSON list of one {check, outcome} entry per check the battery ran,
    -- schema-on-read by design so a tenth check needs no migration. NOT NULL, and never an empty
    -- list either (a graded run always ran at least check 1), but the "never empty" half is
    -- enforced by compose_verdict raising loudly on an empty outcome list rather than by a CHECK
    -- here, because an empty JSON array is itself valid JSON and this column's json_valid() check
    -- proves only that the stored TEXT is syntactically parseable, a format guarantee orthogonal
    -- to the content rule compose_verdict already carries. Mirrors opsdb.py's own config_json
    -- convention, CHECK (json_valid(config_json)).
    check_outcomes_json       TEXT    NOT NULL CHECK (json_valid(check_outcomes_json)),

    -- decided_by: which of the four deciders caused this pass (architecture section 3b, out of
    -- this unit's scope): 'initial', 'monthly', 'out-of-band' or 'advisory'. No CHECK constraint
    -- restricting it to those four values, for the same reason evidence_floor_outcome above
    -- carries none: the four-decider roster is section 3b's own business fact, out of scope for
    -- this build to encode as a schema-level enum.
    decided_by                TEXT    NOT NULL,

    -- warnings_json: a JSON list, always written, '[]' when no check raised a warning. Never NULL:
    -- architecture section 2 makes that a schema statement in its own words, "NULL is never a
    -- legitimate state for this column." Same json_valid() format guarantee as check_outcomes_json
    -- above.
    warnings_json              TEXT    NOT NULL CHECK (json_valid(warnings_json)),

    -- graded_at: when this row was written, an integer epoch timestamp. The CURRENT row rule (item
    -- 3 below) orders on this value first and id second.
    graded_at                  INTEGER NOT NULL,

    -- The exactly-once key (architecture section 2b item 1, restated section 3 item 3), quoted
    -- verbatim: "UNIQUE (run_id, variation_id, ticker, judging_pass_id)." A retry of the same
    -- decision therefore skips, cell by cell, and cannot write any of them twice (AC-6).
    UNIQUE (run_id, variation_id, ticker, judging_pass_id)
);

-- The index architecture section 2b item 5 derives from the query pattern the shortlist read and
-- the narrowed candidate-verdict read both use, citing DDIA chapter 4's own concatenated-index
-- argument from kleppmann_canon.md: a composite index serves queries that fix a PREFIX of its
-- columns, and is useless for a query that does not. The five candidate-key columns as the
-- fixed prefix, then the two ordering columns the CURRENT-row rule (item 3 below) compares on
-- last, column for column exactly as the architecture document names it.
CREATE INDEX IF NOT EXISTS ix_research_judge_verdicts_candidate_current
    ON research_judge_verdicts(
        strategy_id, strategy_version, variation_id, ticker, dataset_sha256, graded_at, id
    );
"""


# =============================================================================
# The thresholds store (docs/briefs/judge_pass_bars_2026-09-08.md, "The store:
# research_judge_thresholds"). DDL copied verbatim from that document's own fenced sql block,
# because the document explicitly specifies the DDL and defers only the choice of WHICH module
# builds it; night four (6.1a) is that build, and it picks judgedb.py beside _DDL_VERDICTS per
# that document's own stated lean ("the thresholds a verdict was graded against and the verdict
# itself are one subject with one reader").
# =============================================================================
_DDL_THRESHOLDS = """
CREATE TABLE IF NOT EXISTS research_judge_thresholds (
    -- Ordinary SQLite rowid alias, matching every table in researchdb.py and judgedb.py: no
    -- AUTOINCREMENT.
    id               INTEGER PRIMARY KEY,

    -- battery_version: the frozen battery and threshold set this row belongs to. It is the SAME
    -- string research_judge_verdicts.battery_version already carries, which is what makes a
    -- verdict auditable against the bars it was graded under. A later freeze mints a NEW
    -- battery_version and appends new rows; it never edits these (#193's "logged amendment",
    -- made a schema fact).
    battery_version  TEXT    NOT NULL,

    -- check_id: which of the nine checks owns the quantity. The closed range is a CHECK
    -- constraint because the battery is nine checks and a tenth check would be a schema change
    -- with its own review, not a silent insert.
    check_id         INTEGER NOT NULL CHECK (check_id BETWEEN 1 AND 9),

    -- quantity_key: the stable name of the quantity within its check, for example
    -- 'plateau_flatness_bar' or 'random_arm_replication_count'. Text and not an integer roster
    -- row, because the roster's row numbers are a document's numbering and this store outlives
    -- it.
    quantity_key     TEXT    NOT NULL,

    -- status: frozen means a value is set and gradeable; owed means the quantity is recorded as
    -- having no value. An owed row is a RECORDED ABSENCE and is never a value: a reader treats
    -- it exactly as it treats a missing row, which is not-evaluable.
    status           TEXT    NOT NULL CHECK (status IN ('frozen', 'owed')),

    -- value_kind: 'number' for a cardinal quantity, 'choice' for a frozen non-numeric decision
    -- such as the buy-and-hold arm's construction, 'none' for an owed row.
    value_kind       TEXT    NOT NULL CHECK (value_kind IN ('number', 'choice', 'none')),

    -- value_numeric: the number itself, NULL for anything that is not a number. NULL and not 0,
    -- so that a reader doing arithmetic on an owed or non-numeric quantity gets NULL rather than
    -- a silently valid zero.
    value_numeric    REAL,

    -- value_text: always written. For a number it is the number as a human reads it with its
    -- unit; for a choice it is the choice; for an owed row it is the GAP SENTENCE naming what is
    -- missing and why, so an absence carries its reason in the same row.
    value_text       TEXT    NOT NULL,

    -- units: 'probability', 'standard_deviations', 'percent_per_year', 'percent', 'count', or
    -- 'none' for a choice. This list is ILLUSTRATIVE, not enforced by a CHECK: the column takes
    -- whatever unit the row's own ruling states, in the ruling's own words, so a percent bar
    -- (compared against a value already expressed as a percent, not a 0-1 fraction) reads
    -- 'percent' rather than 'none'. Fixed in the read_1 fold (J1-R1-SF2): the wfe_pass_bar seed
    -- row originally stored 'none' while its own value_text read "at least 50 percent", which this
    -- comment's own prior wording ("a bar with no unit is how a percent gets compared against a
    -- fraction") had backed into as though it were the rule rather than a single row's mistake.
    units            TEXT    NOT NULL,

    -- comparison: which side of the bar passes. 'none' for a quantity that is not a bar, such as
    -- a replication count. 'strictly_below' (DBG-2b, DECISIONS #436 item 7): the p-value bars of
    -- checks 7 and 9 pass only when the value is strictly below the bar, never at it.
    comparison       TEXT    NOT NULL CHECK (comparison IN ('at_or_above', 'at_or_below',
                                                            'strictly_above', 'none', 'strictly_below')),

    -- source_kind: how the value was arrived at. 'book_stated' is the check's own anchor book;
    -- 'convention_other_book' is a value borrowed from a book that is not the anchor; 'ruling' is
    -- Tamer's own decision where no book states one; 'none' is only legal on an owed row,
    -- enforced below.
    source_kind      TEXT    NOT NULL CHECK (source_kind IN ('book_stated', 'convention_other_book',
                                                             'ruling', 'none')),

    -- The four source columns. source_pdf_page is TEXT, not INTEGER, because a citation can name
    -- a range ('207 to 211') and because AFML in this copy has no printed folio at all, in which
    -- case source_folio reads 'none printed in this copy'.
    source_book      TEXT    NOT NULL,
    source_folio     TEXT    NOT NULL,
    source_pdf_page  TEXT    NOT NULL,
    source_quote     TEXT    NOT NULL,

    -- frozen_at: when the row was written, an integer epoch timestamp, matching
    -- research_judge_verdicts.graded_at's own type.
    frozen_at        INTEGER NOT NULL,

    -- frozen_by_entry: the DECISIONS.md entry that ruled it, for example '#376'. Non-empty by
    -- CHECK: an empty string here would defeat the whole audit and NOT NULL alone does not stop
    -- it.
    frozen_by_entry  TEXT    NOT NULL CHECK (frozen_by_entry <> ''),

    -- An owed row carries no value of any kind; a frozen row carries one.
    CHECK ((status = 'frozen' AND value_kind IN ('number', 'choice'))
        OR (status = 'owed'   AND value_kind = 'none')),

    -- value_numeric is present exactly when the kind is a number.
    CHECK ((value_kind =  'number' AND value_numeric IS NOT NULL)
        OR (value_kind <> 'number' AND value_numeric IS NULL)),

    -- A frozen row must name how it was sourced. This is #216 and #193 made a schema fact: a
    -- value with no source cannot be written, and a value Tamer chose where no book states one
    -- must be recorded as a ruling rather than dressed as a book source.
    CHECK (status = 'owed' OR source_kind <> 'none')
);

-- The partial unique index on the current row. Within one battery_version, a quantity has at
-- most ONE frozen value; any number of owed rows may precede it in earlier versions. This is
-- what makes "the thresholds are frozen" a database fact rather than a convention: changing a
-- bar inside a frozen battery version is unwritable, so an amendment must mint a new
-- battery_version, which is exactly what #193 requires.
CREATE UNIQUE INDEX IF NOT EXISTS ux_judge_thresholds_frozen_once
    ON research_judge_thresholds(battery_version, check_id, quantity_key)
    WHERE status = 'frozen';
"""

# The OLD battery, night four's own build (section 8.2 of the night-four plan): the roster of all
# nine check ids this battery version names. Five of the nine (2, 5, 6, 7, 9) had no computing
# function yet at that time and always composed to not-evaluable through compose_verdict's own
# "missing check" branch, never through a row in this table.
OLD_BATTERY_VERSION = "2026-09-09.a"
BATTERY_CHECK_ROSTER = (1, 2, 3, 4, 5, 6, 7, 8, 9)

# Lane TH (set 16b, `#421`, `#423`), 2026-09-24: a NEW battery version, minted because `#193`
# requires an amendment to a frozen row to be a NEW battery_version, never an edit of an existing
# row (the DDL's own partial unique index makes that a database fact, not merely a convention).
# The OLD version's own 15 rows below (`_THRESHOLD_SEED_ROWS_2026_09_09_A`) are kept byte-for-byte
# as night four wrote them: this lane never edits or deletes a row of an earlier version, it only
# appends a new version's own rows under a new battery_version string.
BATTERY_VERSION = "2026-10-30.a"

# The OLD battery's own fifteen rows, UNCHANGED from night four's own build (kept verbatim so
# `seed_thresholds` can still seed "2026-09-09.a" exactly as it always has for a probe simulating a
# production database that already carries those rows; `#193`'s own "never edits or deletes a row
# of any earlier version" made a code fact, not merely a comment). Each tuple: (check_id,
# quantity_key, status, value_kind, value_numeric, value_text, units, comparison, source_kind,
# source_book, source_folio, source_pdf_page, source_quote, frozen_by_entry).
_THRESHOLD_SEED_ROWS_2026_09_09_A = (
    # --- The eight bars #376 ruling 8 froze (DECISIONS.md lines 10190-10199) ---------------------
    (2, "wfe_pass_bar", "frozen", "number", 50.0,
     "walk-forward efficiency (out-of-sample profit over in-sample profit) at least 50 percent",
     "percent", "at_or_above", "ruling",
     "Pardo, The Evaluation and Optimization of Trading Strategies (2nd ed)", "239", "239",
     "robust strategies above 50 or 60 percent, a warning below 25 percent", "#376"),
    (4, "plateau_flatness_bar", "frozen", "number", 1.0,
     "flatness within 1 standard deviation of the neighbourhood's own mean",
     "standard_deviations", "at_or_below", "book_stated",
     "Pardo, The Evaluation and Optimization of Trading Strategies (2nd ed)", "230", "230",
     "flatness within 1 standard deviation", "#376"),
    (5, "pbo_threshold", "frozen", "number", 0.5,
     "PBO at most 0.5, the no-skill point",
     "probability", "at_or_below", "ruling",
     "Advances in Financial Machine Learning (de Prado)", "none printed in this copy", "n/a",
     "PBO at most 0.5, the no-skill point", "#376"),
    (6, "mc_bound_pass_bar", "frozen", "number", 0.0,
     "the bootstrap lower bound on the mean return, strictly above 0",
     "none", "strictly_above", "book_stated",
     "Masters, Testing and Tuning Market Trading Systems", "222", "222",
     "the bootstrap lower bound on the mean return, strictly above 0", "#376"),
    (7, "mcpt_pvalue_threshold", "frozen", "number", 0.05,
     "MCPT (Monte Carlo permutation test) p-value below 0.05, a convention borrowed from Aronson"
     " and Masters, not TSSB (this check's own canon owner, which states none)",
     "probability", "at_or_below", "convention_other_book",
     "Aronson, Evidence-Based Technical Analysis / Masters, Testing and Tuning Market Trading"
     " Systems", "233 / 285", "233 / 285",
     "p-value below 0.05", "#376"),
    (8, "prom_minus_biggest_run_pass_bar", "frozen", "number", 0.0,
     "PROM minus the biggest winning run strictly above 0, the most stringent variant",
     "none", "strictly_above", "ruling",
     "Pardo, The Evaluation and Optimization of Trading Strategies (2nd ed)", "208", "208",
     "PROM minus the biggest winning run strictly above 0, the most stringent variant", "#376"),
    (9, "random_arm_pass_bar", "frozen", "number", 0.05,
     "the random-entry control arm's p-value below 0.05",
     "probability", "at_or_below", "book_stated",
     "Aronson, Evidence-Based Technical Analysis", "394", "394",
     "p-value below 0.05", "#376"),
    (9, "buy_hold_arm_pass_bar", "frozen", "number", 0.0,
     "the candidate's net return after costs strictly greater than buy-and-hold's over the same"
     " window, margin zero",
     "none", "strictly_above", "ruling",
     "n/a (#193's own words, chosen by Tamer)", "n/a", "n/a",
     "the candidate's net return after costs strictly greater than buy-and-hold's over the same"
     " window, margin zero", "#376"),
    # --- The ninth frozen row: check 3's DSR bar, sourced directly (spec brief section 3) -------
    (3, "dsr_pass_bar", "frozen", "number", 0.95,
     "the deflated Sharpe ratio (DSR) should exceed 0.95, the standard 5 percent significance"
     " level",
     "probability", "strictly_above", "book_stated",
     "Advances in Financial Machine Learning (de Prado)", "none printed in this copy", "274-276",
     "It should exceed 0.95, for the standard significance level of 5%.", "#213"),
    # --- The five owed roster rows (pass_bars sheet rows 4, 7, 9, 10, 11; not frozen by #376) ---
    (6, "bound_percentile", "owed", "none", None,
     "the percentile at which the pessimistic lower bound is read: OWED, proposed 0.10"
     " (book_stated, Masters folios 245, 246, 261) but not ratified by #376",
     "none", "none", "none", "", "", "", "", "#193"),
    (9, "random_arm_comparison_statistic", "owed", "none", None,
     "the random-entry arm's comparison statistic form: OWED, two candidate forms sourced"
     " (Aronson folio 240, Masters folio 285), lean Masters, not ratified by #376",
     "none", "none", "none", "", "", "", "", "#193"),
    (9, "random_arm_replication_count", "owed", "none", None,
     "the random-entry arm's replication count: OWED, proposed 5,000 or 4,999 per D7"
     " (book_stated, Aronson folios 240 and 243), not ratified by #376",
     "none", "none", "none", "", "", "", "", "#193"),
    (9, "buy_hold_arm_construction", "owed", "none", None,
     "the buy-and-hold control arm's own construction: SOURCED with a transfer caveat"
     " (book_stated, TSSB folio 14), not ratified as a frozen bar by #376",
     "none", "none", "none", "", "", "", "", "#193"),
    (9, "buy_hold_arm_comparison_statistic", "owed", "none", None,
     "the buy-and-hold arm's comparison statistic: PARTIAL, comparands sourced but the statistic"
     " itself is not (book_stated in part, TSSB folio 14)",
     "none", "none", "none", "", "", "", "", "#193"),
    # --- The plateau bandwidth: owed separately in judge_spec_brief.md section 7 -----------------
    (4, "plateau_bandwidth", "owed", "none", None,
     "how many neighbouring parameter cells on each side define the plateau's own neighbourhood:"
     " OWED, judge_spec_brief.md section 7 names it as a gap distinct from the flatness bar",
     "none", "none", "none", "", "", "", "", "#193"),
)

# The NEW battery's own sixteen rows (lane TH, `#421`, `#423`), 2026-09-24. The seven quantities
# `#421` did not touch (`wfe_pass_bar`, `plateau_flatness_bar`, `pbo_threshold`,
# `mc_bound_pass_bar`, `mcpt_pvalue_threshold`, `prom_minus_biggest_run_pass_bar`, `dsr_pass_bar`)
# are carried over with the identical value each already held; every book-sourced freeze below was
# read from the actual PDF page cited (docs/units/TH/book_reads.md carries the full read, folio,
# PDF page and the exact quote), never from a summary or a remembered value (#216). Same tuple
# shape as the old version's own rows above.
_THRESHOLD_SEED_ROWS_2026_09_24_A = (
    # --- The seven quantities #421 left untouched, carried over with their pre-lane value --------
    (2, "wfe_pass_bar", "frozen", "number", 50.0,
     "walk-forward efficiency (out-of-sample profit over in-sample profit) at least 50 percent",
     "percent", "at_or_above", "ruling",
     "Pardo, The Evaluation and Optimization of Trading Strategies (2nd ed)", "239", "239",
     "robust strategies above 50 or 60 percent, a warning below 25 percent", "#376"),
    (4, "plateau_flatness_bar", "frozen", "number", 1.0,
     "flatness within 1 standard deviation of the neighbourhood's own mean",
     "standard_deviations", "at_or_below", "book_stated",
     "Pardo, The Evaluation and Optimization of Trading Strategies (2nd ed)", "230", "230",
     "flatness within 1 standard deviation", "#376"),
    (5, "pbo_threshold", "frozen", "number", 0.5,
     "PBO at most 0.5, the no-skill point",
     "probability", "at_or_below", "ruling",
     "Advances in Financial Machine Learning (de Prado)", "none printed in this copy", "n/a",
     "PBO at most 0.5, the no-skill point", "#376"),
    (6, "mc_bound_pass_bar", "frozen", "number", 0.0,
     "the bootstrap lower bound on the mean return, strictly above 0",
     "none", "strictly_above", "book_stated",
     "Masters, Testing and Tuning Market Trading Systems", "222", "222",
     "the bootstrap lower bound on the mean return, strictly above 0", "#376"),
    (7, "mcpt_pvalue_threshold", "frozen", "number", 0.05,
     "MCPT (Monte Carlo permutation test) p-value below 0.05, a convention borrowed from Aronson"
     " and Masters, not TSSB (this check's own canon owner, which states none)",
     "probability", "at_or_below", "convention_other_book",
     "Aronson, Evidence-Based Technical Analysis / Masters, Testing and Tuning Market Trading"
     " Systems", "233 / 285", "233 / 285",
     "p-value below 0.05", "#376"),
    (8, "prom_minus_biggest_run_pass_bar", "frozen", "number", 0.0,
     "PROM minus the biggest winning run strictly above 0, the most stringent variant",
     "none", "strictly_above", "ruling",
     "Pardo, The Evaluation and Optimization of Trading Strategies (2nd ed)", "208", "208",
     "PROM minus the biggest winning run strictly above 0, the most stringent variant", "#376"),
    (3, "dsr_pass_bar", "frozen", "number", 0.95,
     "the deflated Sharpe ratio (DSR) should exceed 0.95, the standard 5 percent significance"
     " level",
     "probability", "strictly_above", "book_stated",
     "Advances in Financial Machine Learning (de Prado)", "none printed in this copy", "274-276",
     "It should exceed 0.95, for the standard significance level of 5%.", "#213"),
    # --- random_arm_pass_bar: `#423` item 1 asked only whether Aronson folio 394 still states the
    # bar, re-read for this build (docs/units/TH/book_reads.md). It does: folio 394 (this copy's
    # own printed page, verified by rendering it) reads the 5 percent significance level in the
    # same case-study section this bar belongs to, so the citation stays book_stated, unchanged,
    # value unchanged at 0.05.
    (9, "random_arm_pass_bar", "frozen", "number", 0.05,
     "the random-entry control arm's p-value below 0.05",
     "probability", "at_or_below", "book_stated",
     "Aronson, Evidence-Based Technical Analysis", "394", "394",
     "A 5 percent level of significance was chosen as a threshold for rejection of the null"
     " hypothesis.", "#376"),
    # --- buy_hold_arm_pass_bar: RESTATED per #421 item 3 / #423 sheet item 3. Same pass/fail
    # semantic (margin zero, strictly above), now expressed as a Sharpe-ratio difference instead of
    # a net-return difference, because buy_hold_arm_comparison_statistic below settles the
    # comparison statistic as the Sharpe ratio and this bar must compare on the same statistic.
    (9, "buy_hold_arm_pass_bar", "frozen", "number", 0.0,
     "the candidate's Sharpe ratio minus buy-and-hold's Sharpe ratio, strictly greater than zero"
     " (restated from the original net-return-after-costs bar, #421 item 3, #423 sheet item 3)",
     "none", "strictly_above", "ruling",
     "n/a (#421 item 3, restated by Tamer)", "n/a", "n/a",
     "the candidate's Sharpe ratio minus buy-and-hold's Sharpe ratio, strictly greater than zero",
     "#421"),
    # --- bound_percentile: NEWLY FROZEN at 0.10 (check 6), Masters folios 245, 246 and 261, all
    # three read for this build (docs/units/TH/book_reads.md).
    (6, "bound_percentile", "frozen", "number", 0.10,
     "the percentile at which check 6's pessimistic lower bound is read: 0.10 (Masters' own worked"
     " example, folios 245, 246 and 261, a 10 percent user-specified failure rate)",
     "probability", "none", "book_stated",
     "Masters, Testing and Tuning Market Trading Systems", "245, 246, 261", "245, 246, 261",
     "suppose we let p be 0.1, implying that we want a lower bound such that only 10 percent of"
     " the time in the future will the return be less than or equal to this lower bound.",
     "#421"),
    # --- random_arm_comparison_statistic: NEWLY FROZEN as Masters' (k+1)/(m+1) form (check 9),
    # folio 285, the same folio mcpt_pvalue_threshold above already cites for its own convention.
    (9, "random_arm_comparison_statistic", "frozen", "choice", None,
     "the random-entry control arm's comparison statistic: Masters' (k+1)/(m+1) form, k the count"
     " of permutations whose performance equals or exceeds the original result, m the total"
     " replication count",
     "none", "none", "book_stated",
     "Masters, Testing and Tuning Market Trading Systems", "285", "285",
     "there is probability (k+1)/(m+1) that we would have obtained this result or better by luck.",
     "#421"),
    # --- random_arm_replication_count: NEWLY FROZEN at 5,000 (check 9), Aronson's own worked
    # example, folios 240 and 243, both read for this build and both stating 5,000 (comma form),
    # settling the "5,000 or 4,999" choice `#423` left to this page read.
    (9, "random_arm_replication_count", "frozen", "number", 5000.0,
     "the random-entry arm's replication count: 5,000 Monte Carlo permutations, Aronson's own"
     " worked example (folios 240 and 243, both stating 5,000)",
     "count", "none", "book_stated",
     "Aronson, Evidence-Based Technical Analysis", "240, 243", "240, 243",
     "Steps 3 through 5 must be repeated 5,000 times.", "#421"),
    # --- buy_hold_arm_construction: NEWLY FROZEN (check 9), TSSB folio 14's own buy-and-hold
    # profit factor definition: a long position over every case, held for the same look-ahead
    # distance as the candidate's own holding period.
    (9, "buy_hold_arm_construction", "frozen", "choice", None,
     "the buy-and-hold control arm's own construction: a long position held over the same cases,"
     " for the candidate's own holding period (TSSB's own buy-and-hold profit factor definition,"
     " folio 14)",
     "none", "none", "book_stated",
     "TSSB (Statistically Sound Machine Learning for Algorithmic Trading, Aronson and Masters)",
     "14", "14",
     "The profit factor that would be obtained by treating each case as a single trade, taking a"
     " long position for every one of them and holding each for the look-ahead distance of the"
     " target variable.", "#421"),
    # --- buy_hold_arm_comparison_statistic: NEWLY FROZEN as a RULING (check 9), #421 item 3 /
    # #423 sheet item 3: the ordinary Sharpe ratio, no risk-free rate, computed the same way the
    # candidate's own Sharpe already is (AC-4's own provenance requirement, answered inline).
    (9, "buy_hold_arm_comparison_statistic", "frozen", "choice", None,
     "the buy-and-hold arm's comparison statistic: the ordinary Sharpe ratio, no risk-free rate"
     " subtracted, a ruling (#421 item 3, #423 sheet item 3). mechanism: computed with the same"
     " annualisation and formula as the candidate's own Sharpe ratio, so the two arms are"
     " comparable on the same statistic. sensitivity: a nonzero risk-free rate would shift both"
     " arms' Sharpe by the same subtracted term and cancel out of the margin buy_hold_arm_pass_bar"
     " compares, so the zero choice is not a load-bearing free parameter. bounds: holds only while"
     " both arms share the same evaluation period and annualisation factor; a mismatch in either"
     " is a different comparison, not this one.",
     "none", "none", "ruling",
     "n/a (#421 item 3, #423 sheet item 3, chosen by Tamer)", "n/a", "n/a",
     "the ordinary Sharpe ratio, no risk-free rate", "#421"),
    # --- plateau_bandwidth: NEWLY FROZEN as a RULING (check 4), #421 item 2: Tamer's own words for
    # a first prototype, no book value (AC-4's own provenance requirement, answered inline).
    (4, "plateau_bandwidth", "frozen", "number", 1.0,
     "how many neighbouring parameter cells on each side define the plateau's own neighbourhood: 1"
     " neighbour on each side, a ruling for a first prototype (#421 item 2, no book value)."
     " mechanism: check 4 needs at least this many neighbouring cells on both sides of the peak"
     " before it can compute a neighbourhood mean and standard deviation to compare the peak"
     " against. sensitivity: a wider bandwidth averages the peak's own neighbourhood over more"
     " cells, which would smooth out a narrow but genuine peak; a narrower bandwidth than 1 is not"
     " available (0 neighbours is no neighbourhood at all). bounds: this is a first-prototype"
     " choice, revisited if the sweep grid's own parameter spacing changes enough to make 1"
     " neighbour on each side too narrow or too wide a window.",
     "count", "none", "ruling",
     "n/a (#421 item 2, Tamer's own words for a first prototype)", "n/a", "n/a",
     "1 neighbour on each side, a first prototype", "#421"),
    # --- cscv_split_count: a NEW row (check 5), AFML's own worked example ("For instance, if"
    # " S = 16"), this copy carrying no printed folio (the same convention dsr_pass_bar above
    # already uses for this same book).
    (5, "cscv_split_count", "frozen", "number", 16.0,
     "the number of equal-sized partitions S that CSCV splits the trial history into before"
     " forming the C(S, S/2) combinations (AFML's own worked example uses S = 16)",
     "count", "none", "book_stated",
     "Advances in Financial Machine Learning (de Prado)", "none printed in this copy", "209",
     "For instance, if S = 16, we will form 12,780 combinations.", "#421"),
)

# DBG-2b (DECISIONS #436 item 7, 2026-10-10): a NEW battery version that makes the two p-value bars
# strict. Checks 7 (MCPT, `mcpt_pvalue_threshold`) and 9 (the random-entry arm,
# `random_arm_pass_bar`) pass only when the p-value is STRICTLY below 0.05. Every other row of
# "2026-09-24.a" is carried over unchanged, so the only difference between the two sets is the
# comparison word on those two rows (probe dbg2b_strict_bar, s7 checks exactly that).
_STRICT_BAR_QUANTITIES = frozenset({(7, "mcpt_pvalue_threshold"), (9, "random_arm_pass_bar")})
_THRESHOLD_SEED_ROWS_2026_10_10_A = tuple(
    row[:7] + ("strictly_below",) + row[8:]
    if (row[0], row[1]) in _STRICT_BAR_QUANTITIES else row
    for row in _THRESHOLD_SEED_ROWS_2026_09_24_A
)

# DBG-7b (DECISIONS #479 item 8, 2026-10-30): a NEW battery version. 2026-09-24.a and 2026-10-10.a
# carry TSSB folio 14's words on buy_hold_arm_construction while the arm is built as #451 rules
# (one long entry on the first bar, sold on the last), so this version records the arm as that
# ruling (#193: an amended frozen row mints a new version; #376 decision 1: a value no book states
# is recorded as Tamer's). Stored verdicts keep their version. Every other row is carried over.
_BUY_HOLD_CONSTRUCTION_451 = (
    9, "buy_hold_arm_construction", "frozen", "choice", None,
    "the buy-and-hold control arm's own construction: one long position entered at the replay"
    " window's first tradable bar and held to its last bar, through the candidate's own engine,"
    " costs and sizing, a ruling (#451, kept by #479 item 8)",
    "none", "none", "ruling",
    "n/a (#451, kept by #479 item 8, chosen by Tamer)", "n/a", "n/a",
    "check 9's buy-and-hold arm buys on the first bar and sells on the last", "#451",
)
_THRESHOLD_SEED_ROWS_2026_10_30_A = tuple(
    _BUY_HOLD_CONSTRUCTION_451 if (row[0], row[1]) == (9, "buy_hold_arm_construction") else row
    for row in _THRESHOLD_SEED_ROWS_2026_10_10_A
)

# Every battery version this module can seed, mapped to its own frozen rows. A version named here
# is seeded from its own tuple and never from another's; an unnamed version is refused.
_SEED_ROWS_BY_BATTERY_VERSION = {
    OLD_BATTERY_VERSION: _THRESHOLD_SEED_ROWS_2026_09_09_A,
    "2026-09-24.a": _THRESHOLD_SEED_ROWS_2026_09_24_A,
    "2026-10-10.a": _THRESHOLD_SEED_ROWS_2026_10_10_A,
    "2026-10-30.a": _THRESHOLD_SEED_ROWS_2026_10_30_A,
}


def seed_thresholds(conn, battery_version: str = BATTERY_VERSION, now: "int | None" = None) -> None:
    """INSERT OR IGNORE the module-level seed rows above for one battery_version (AC-1, AC-2).

    INSERT OR IGNORE against the partial unique index on (battery_version, check_id,
    quantity_key) WHERE status='frozen' only guards the FROZEN half; an owed row carries no such
    guard by the DDL's own design (any number of owed rows may exist), so this function also
    reads back the (battery_version, check_id, quantity_key) triples already present and skips
    re-inserting an owed row it already wrote, keeping init_judge_db's own "row count unchanged
    on a second call" promise (AC-1) for owed rows too, not only frozen ones.

    Lane TH (2026-09-24): exactly two seed-row sets exist today, the OLD battery's own rows and the
    CURRENT battery's own rows, and this function picks between them by NAME rather than always
    seeding whichever set is current. A caller naming `OLD_BATTERY_VERSION` explicitly (a probe
    simulating a production database that already carries "2026-09-09.a" rows before the new
    version's own seed ever runs) gets the old set back, byte-for-byte, whatever the module's own
    current `BATTERY_VERSION` happens to be. This is what makes "seeding the new version never
    edits or deletes a row of an earlier version" (`#193`) a code fact: the old rows are a separate,
    untouched tuple, not a filtered view of the new one.
    """
    import time

    ts = int(time.time()) if now is None else now
    if battery_version not in _SEED_ROWS_BY_BATTERY_VERSION:
        raise ValueError(
            f"seed_thresholds has no seed rows for battery_version {battery_version!r}; "
            f"the versions it can seed are {sorted(_SEED_ROWS_BY_BATTERY_VERSION)}"
        )
    rows_to_seed = _SEED_ROWS_BY_BATTERY_VERSION[battery_version]
    existing = {
        (r["check_id"], r["quantity_key"])
        for r in conn.execute(
            "SELECT check_id, quantity_key FROM research_judge_thresholds WHERE battery_version = ?;",
            (battery_version,),
        ).fetchall()
    }
    for row in rows_to_seed:
        (check_id, quantity_key, status, value_kind, value_numeric, value_text, units,
         comparison, source_kind, source_book, source_folio, source_pdf_page, source_quote,
         frozen_by_entry) = row
        if (check_id, quantity_key) in existing:
            continue
        conn.execute(
            "INSERT INTO research_judge_thresholds "
            "    (battery_version, check_id, quantity_key, status, value_kind, value_numeric,"
            "     value_text, units, comparison, source_kind, source_book, source_folio,"
            "     source_pdf_page, source_quote, frozen_at, frozen_by_entry) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (battery_version, check_id, quantity_key, status, value_kind, value_numeric,
             value_text, units, comparison, source_kind, source_book, source_folio,
             source_pdf_page, source_quote, ts, frozen_by_entry),
        )


def read_threshold(conn, check_id: int, quantity_key: str,
                    battery_version: str = BATTERY_VERSION) -> "dict | None":
    """The one reader every check function uses: the FROZEN row for one quantity under one
    battery version, or None when it is absent or still owed. A None here is what a check reads
    as "the input the check needs is absent" (the spec brief's own escalation rule): never a
    default, never a substituted number.
    """
    row = conn.execute(
        "SELECT * FROM research_judge_thresholds "
        " WHERE battery_version = ? AND check_id = ? AND quantity_key = ? AND status = 'frozen';",
        (battery_version, check_id, quantity_key),
    ).fetchone()
    return dict(row) if row is not None else None


def _split_ddl_statements(script: str) -> list[str]:
    """Split a multi-statement DDL string into individual SQL statements, stripping "--" line
    comments first so a semicolon typed inside a comment can never split a statement in half.

    Mirrors opsdb.py's own _split_ddl_statements (and eventsdb.py's before it, the same
    convention): conn.executescript() implicitly commits any open transaction before running,
    which would break the BEGIN IMMEDIATE / COMMIT wrapper init_judge_db runs inside via
    _judge_transaction, so each statement is executed individually instead. This codebase's own
    convention is that each store module carries its own private copy of this helper rather than
    importing a neighbour's, which opsdb.py's own docstring states for its copy relative to
    eventsdb.py's; this module does the same relative to opsdb.py.
    """
    # Walk the script one source line at a time so a "--" comment is found and cut per line, the
    # same granularity opsdb.py's own version uses (a "--" only starts a comment for the rest of
    # that one line in SQL, never across lines).
    no_comment_lines = []
    for line in script.splitlines():
        comment_start = line.find("--")
        if comment_start == -1:
            no_comment_lines.append(line)
        else:
            # Keep only the code before the comment marker, so any ";" living inside the comment
            # text can never reach the split below.
            no_comment_lines.append(line[:comment_start])
    # Rejoin into one string so a statement spanning multiple source lines (the CREATE TABLE and
    # the CREATE INDEX both do) is still one contiguous piece of text before the semicolon split.
    cleaned = "\n".join(no_comment_lines)
    statements = []
    for piece in cleaned.split(";"):
        piece = piece.strip()
        if piece:                                          # drop the empty tail after the last ";"
            statements.append(piece + ";")
    return statements


# DBG-2b (DECISIONS #436 item 7): the threshold table's comparison CHECK gains 'strictly_below'. SQLite
# cannot ALTER a CHECK, so a database created before this lane is rebuilt with the wider CHECK. The
# rebuild copies every row unchanged (a CHECK widening is backward compatible: every old row stays
# valid under the wider rule, so old readers keep working). It is the same rebuild eventsdb.py
# already uses for its own CHECK widenings (`_rebuild_table_widening_checks`), copied here because
# each store module keeps its own private copy of its helpers.
_THRESHOLDS_OLD_COMPARISON_TAIL = "'strictly_above', 'none')"
_THRESHOLDS_NEW_COMPARISON_TAIL = "'strictly_above', 'none', 'strictly_below')"


def _rebuild_thresholds_widening_check(conn) -> None:
    """Rebuild research_judge_thresholds with the comparison CHECK widened to strictly_below.

    The caller owns the transaction and has foreign_keys OFF. The live CREATE sql is read from
    sqlite_master and rewritten, never retyped, and the rebuild refuses (RuntimeError, naming the
    cause) when the table's CREATE prefix or the old CHECK tail is not exactly as expected.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_judge_thresholds'"
    ).fetchone()
    create_sql = row["sql"]
    # SQLite stores the name QUOTED once a table has been ALTER RENAMEd, and unquoted when it was
    # only ever CREATEd. Both forms are accepted; anything else is a changed schema, refused.
    quoted_prefix = 'CREATE TABLE "research_judge_thresholds"'
    plain_prefix = "CREATE TABLE research_judge_thresholds"
    if quoted_prefix in create_sql:
        create_prefix = quoted_prefix
    elif plain_prefix in create_sql:
        create_prefix = plain_prefix
    else:
        raise RuntimeError(
            f"DBG-2b migration: research_judge_thresholds CREATE sql lacks an expected CREATE TABLE "
            f"prefix (neither {quoted_prefix!r} nor {plain_prefix!r}); the live schema changed, "
            f"refusing to rebuild blindly"
        )
    if _THRESHOLDS_OLD_COMPARISON_TAIL not in create_sql:
        raise RuntimeError(
            f"DBG-2b migration: research_judge_thresholds CREATE sql lacks the expected comparison "
            f"CHECK tail {_THRESHOLDS_OLD_COMPARISON_TAIL!r}; the live schema changed, refusing to "
            f"rebuild blindly"
        )
    new_create = create_sql.replace(
        create_prefix, "CREATE TABLE research_judge_thresholds_new", 1
    ).replace(_THRESHOLDS_OLD_COMPARISON_TAIL, _THRESHOLDS_NEW_COMPARISON_TAIL, 1)
    cols = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(research_judge_thresholds)"))
    conn.execute("DROP TABLE IF EXISTS research_judge_thresholds_new")   # clean any partial prior run
    conn.execute(new_create)
    conn.execute(
        f"INSERT INTO research_judge_thresholds_new ({cols}) SELECT {cols} FROM research_judge_thresholds"
    )
    conn.execute("DROP TABLE research_judge_thresholds")
    conn.execute("ALTER TABLE research_judge_thresholds_new RENAME TO research_judge_thresholds")
    # Dropping the table dropped its unique index, so the index is recreated from the module's own DDL.
    for statement in _split_ddl_statements(_DDL_THRESHOLDS):
        if statement.startswith("CREATE UNIQUE INDEX"):
            conn.execute(statement)


def _migrate_thresholds_strictly_below(research_db_path: Union[str, Path, None] = None) -> None:
    """Widen an existing research_judge_thresholds to the strictly_below CHECK, once.

    Runs on its own connection BEFORE init_judge_db's transaction, because PRAGMA foreign_keys is a
    no-op inside a transaction and must be OFF before BEGIN for the rebuild. A fresh database has no
    table yet, so this is a no-op and init's own DDL creates the wider CHECK. A table already carrying
    strictly_below is left alone, so a second run changes nothing.
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_judge(path)
    try:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='research_judge_thresholds'"
        ).fetchone()
        if existing is None or "'strictly_below'" in existing["sql"]:
            return
        conn.execute("PRAGMA foreign_keys = OFF;")       # valid: no transaction is open yet
        conn.execute("BEGIN IMMEDIATE;")
        try:
            _rebuild_thresholds_widening_check(conn)
        except (sqlite3.Error, RuntimeError):
            conn.execute("ROLLBACK;")
            raise
        conn.execute("COMMIT;")
        conn.execute("PRAGMA foreign_keys = ON;")
    finally:
        conn.close()


def init_judge_db(research_db_path: Union[str, Path, None] = None) -> None:
    """Create research_judge_verdicts and its index if they do not already exist (AC-1).

    Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS, so calling this twice on the same file
    changes neither the schema nor any row, matching init_ops_db's own idempotent shape and
    answering this module's own fresh/first-run and t-zero lifecycle questions together (both
    states are visually identical for this table: zero rows either way, this module's own
    docstring above).
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    # DBG-2b: the CHECK widening runs first, on its own connection with foreign_keys OFF (see
    # _migrate_thresholds_strictly_below); the CREATE and seed below then run in one transaction.
    _migrate_thresholds_strictly_below(path)
    with _judge_transaction(path) as conn:
        for statement in _split_ddl_statements(_DDL_VERDICTS):
            conn.execute(statement)
        # Night four, lane J1 (6.1a): research_judge_thresholds joins the verdicts table in the
        # same idempotent init, then seeds the frozen bars and the recorded gaps in the same
        # transaction, so a fresh boot never observes the table present but unseeded.
        for statement in _split_ddl_statements(_DDL_THRESHOLDS):
            conn.execute(statement)
        seed_thresholds(conn)


# =============================================================================
# The composition rule (AC-5): spec brief section 2, "derived as a PARTITION of the outcome space
# rather than as a list of guards." This is the single expression of that rule; nothing else in
# this module composes a verdict.
# =============================================================================

# The three outcome values every check draws from (spec brief section 1: "kept apart deliberately
# and never collapsed into two"). Any string outside this set normalises to not-evaluable (spec
# brief section 2: "An outcome VALUE outside the closed set... is read as not-evaluable").
_CLEARED = "cleared"
_FAILED = "failed"
_NOT_EVALUABLE = "not-evaluable"
_RECOGNISED_OUTCOMES = frozenset((_CLEARED, _FAILED, _NOT_EVALUABLE))

# The three verdicts the vocabulary names (spec brief section 1), matching the CHECK constraint on
# the verdict column above.
_PASS = "pass"
_FAIL = "fail"
_UNJUDGEABLE = "unjudgeable"

# Check 1 is coordinate A; every other check the battery names is coordinate B (spec brief section
# 2, "The two coordinates the partition cuts on").
_COORDINATE_A_CHECK = 1


def _normalise_check_outcome(check_id, check_outcomes):
    """One check's outcome, normalised to the closed set of three before the partition rule reads
    it (spec brief section 2's own normalisation paragraph, one branch quoted per case below).

    check_outcomes is the caller's raw list of {"check": <int>, "outcome": <str>} entries;
    check_id is the one check this call resolves. Resolved by the DISTINCT raw outcome strings
    recorded for this check_id, which reads all four stated cases correctly in one pass:
      - zero entries (a MISSING check): not-evaluable, quoted, "read as not-evaluable before the
        rule is applied, never as cleared and never as absent."
      - exactly one distinct value, and it is one of the three recognised outcomes: that outcome,
        quoted, "two AGREEING entries... are read as that one outcome."
      - exactly one distinct value, but it is not one of the three: not-evaluable, quoted, "An
        unrecognised outcome value is read as not-evaluable."
      - more than one distinct value (a DISAGREEING duplicate): not-evaluable, quoted, "two
        DISAGREEING entries are read as not-evaluable... neither recorded value has more claim
        than the other to be the check's real outcome."
    """
    raw_values = {entry["outcome"] for entry in check_outcomes if entry["check"] == check_id}
    if len(raw_values) == 1:
        (only_value,) = raw_values
        if only_value in _RECOGNISED_OUTCOMES:
            return only_value
    # Zero entries, more than one distinct entry, or one unrecognised entry: all three read as
    # not-evaluable, for the three separate reasons this function's own docstring quotes above.
    return _NOT_EVALUABLE


def compose_verdict(check_outcomes, battery_checks):
    """Turn the outcomes of every check the battery ran into one verdict (AC-5).

    check_outcomes: a list of {"check": <int>, "outcome": <str>} dicts, architecture section 2's
      own storage shape for check_outcomes_json (one entry per check the battery ran; may hold
      zero, one, or more than one entry for an individual check, normalised by
      _normalise_check_outcome above before this rule reads it).
    battery_checks: every check id the row's own battery_version names (spec brief section 2: "the
      roster... For battery v1 that roster is the nine checks"), a list of ints including check 1.

    Returns one of 'pass', 'fail', 'unjudgeable' (spec brief section 1).

    Raises EmptyCheckOutcomesError if check_outcomes is empty. Raises EmptyCoordinateBRosterError
    if battery_checks's own non-check-1 remainder (coordinate B) is empty, meaning battery_checks
    names check 1 alone.

    LIFECYCLE (AC-10), the four questions in lifecycle-checker's own vocabulary, for the GRADING
    PASS this function composes (a different stateful thing from the TABLE, whose own four answers
    are in this module's own docstring above):

      Fresh/first-run. This function reads only the check_outcomes and battery_checks passed to
      THIS call. There is no seed value, no carried threshold and no previous verdict consulted, so
      the first composition on a fresh database behaves exactly as the millionth.

      Empty set. A grading pass whose recorded outcome set is EMPTY produces no verdict and fails
      loudly (EmptyCheckOutcomesError below): it cannot arise on the ordinary path, because check 1
      always runs, so an empty set means the battery did not run at all, and reading it as "nothing
      to report, therefore clean" would turn a broken battery into a pass.

      NULL/absent. An outcome missing for a check the battery names is treated as not-evaluable,
      never as cleared: silence from a check is never consent. Check 1 is not excepted: a missing
      check 1 outcome is not-evaluable, which this rule's own coordinate-A branch below sends
      straight to unjudgeable regardless of coordinate B.

      T-zero, never acted on. Before any grading pass has ever run, this rule has never been
      applied and no candidate has a verdict at all. "No verdict has ever been composed" stays
      distinct from "composed, and came out unjudgeable," which is the same distinction the
      TABLE's own fresh/first-run answer draws for a row that does not exist.
    """
    if not check_outcomes:
        raise EmptyCheckOutcomesError(
            "compose_verdict refuses an empty check_outcomes list: a grading pass that recorded "
            "no outcomes at all produces no verdict rather than a default one (spec brief section "
            "2, the composition rule's own empty-set lifecycle answer)"
        )

    # Coordinate B is every check the battery names other than check 1 (spec brief section 2). A
    # roster of check 1 alone leaves coordinate B empty, which is not a legitimate battery version.
    coordinate_b_roster = [c for c in battery_checks if c != _COORDINATE_A_CHECK]
    if not coordinate_b_roster:
        raise EmptyCoordinateBRosterError(
            "compose_verdict refuses a battery_checks roster whose only member is check 1: "
            "coordinate B would be vacuously all-cleared and a cleared floor would wrongly "
            "compose to pass on the evidence floor alone (spec brief section 2)"
        )

    # Coordinate A: check 1's own outcome, normalised. Exactly one of the three, spec brief
    # section 2: "those three are the whole outcome vocabulary and check 1 carries exactly one
    # outcome from it."
    coordinate_a = _normalise_check_outcome(_COORDINATE_A_CHECK, check_outcomes)

    # Coordinate B: every OTHER check the battery names, taken together. Order matters and is
    # fixed by the rule itself (spec brief section 2's own B1/B2/B3 partition): B1 is at least one
    # check failed; B2 is B1's own negation combined with at least one check not-evaluable. B1 is
    # therefore tested FIRST and wins on a mixed set (some failed AND some not-evaluable at once):
    # testing "some not-evaluable" first would read a mixed set as B2 instead, the exact trap the
    # job file names and the mixed-case probes exist to catch.
    b_outcomes = [_normalise_check_outcome(c, check_outcomes) for c in coordinate_b_roster]
    some_failed = _FAILED in b_outcomes
    some_not_evaluable = _NOT_EVALUABLE in b_outcomes

    # The 3x3 partition table, spec brief section 2, reproduced here as the single place gate
    # status is decided, in the document's own words: "pass is one cell and only one, the cell
    # where all nine cleared. fail is one cell and only one, the cell where the evidence floor
    # cleared and at least one later check was run against evidence and lost. Every other cell is
    # unjudgeable."
    if coordinate_a != _CLEARED:
        # The failed row and the not-evaluable row: every cell of both is unjudgeable, whatever
        # coordinate B reads. Quoted: "Both are unjudgeable, and they are unjudgeable for the same
        # reason the failed row is."
        return _UNJUDGEABLE
    if some_failed:
        # cleared row, B1 column: the floor cleared and a later check was run against evidence and
        # lost, so the loss is real and the verdict is fail.
        return _FAIL
    if some_not_evaluable:
        # cleared row, B2 column: the floor cleared, nothing failed, but something the battery
        # owed a number never produced one, so nothing decisive was established.
        return _UNJUDGEABLE
    # cleared row, B3 column: every check the battery named cleared.
    return _PASS


# =============================================================================
# The write entry point (AC-6) and its monitors.
# =============================================================================

# WHAT CAN BREAK, item 1 (job file, "what can break"): a verdict could be written for a run whose
# artifacts came from a backtest reaching past the reserve cut (#302, #318). This module states its
# own declared mode in this one sentence, per the job file's own instruction, rather than leaving a
# reader to assume one: the guarantee that a graded run's artifacts respect the reserve boundary is
# INHERITED here, not enforced here. This module reads no reserve boundary of its own, computes
# none and stores none. record_verdict below writes exactly the seventeen columns architecture
# section 2 names, and none of them is a reserve boundary.

# The sixteen caller-supplied columns of an INSERT (id is the autoincrement primary key and is
# never supplied), named once here so record_verdict below cannot mismatch a column name against
# its own placeholder count by hand.
_INSERT_COLUMNS = (
    "run_id", "judging_pass_id", "variation_id", "ticker", "graded_formula_version",
    "strategy_id", "strategy_version", "dataset_sha256", "verdict", "is_advisory",
    "battery_version", "evidence_floor_outcome", "check_outcomes_json", "decided_by",
    "warnings_json", "graded_at",
)

# The exactly-once key (architecture section 2b item 1), named once here so the ON CONFLICT target
# below is visibly the same four columns as the table's own UNIQUE constraint above.
_EXACTLY_ONCE_KEY_COLUMNS = ("run_id", "variation_id", "ticker", "judging_pass_id")

# Three counters for the three monitors the job file's own "what can break" section names (items 2,
# 3, 4; item 1 is the reserve-boundary note above and carries no counter of its own, per its own
# declared mode). Mirrors SIZING_MONITOR_COUNTERS (research_lab/strategies/helpers/sizing.py) and
# SWEEP_GUARD_REFUSAL_COUNTERS (researchdb.py): a module-level dict of int counters, read live by a
# caller through JUDGE_MONITOR_COUNTERS[key], never cached.
#
# duplicate_write_refused_count is driven LIVE, from inside record_verdict below, every time the
# exactly-once gate refuses a second write for an existing quadruple (item 2: "Two grading passes
# race and both write. The exactly-once gate is what stops it. Monitor: a counter of refused
# duplicate writes").
#
# verdict_outcome_count_mismatch_count and stale_battery_version_count are NAMED here, satisfying
# the ops routing law's "every named failure mode ships its counter", but neither is incremented by
# any code in THIS unit, and that gap is stated here rather than hidden. Item 3 (a verdict row
# whose stored outcome count does not equal the number of checks that ran) needs to know how many
# checks actually RAN for a given grading pass, information owned by the check-running units this
# build does not build (job file: "It builds NONE of the nine checks"). Item 4 (a verdict whose
# battery_version is not the current frozen set) needs to know what "the current frozen set" IS, a
# threshold-shaped fact this unit must never embed (job file stop condition: "Any threshold or
# cardinal number is needed... the cut is wrong"). Both counters exist so a future unit that DOES
# have the missing information increments this same dict rather than inventing a second one; wiring
# their increments is that future unit's own job.
JUDGE_MONITOR_COUNTERS = {
    "duplicate_write_refused_count": 0,
    "verdict_outcome_count_mismatch_count": 0,
    "stale_battery_version_count": 0,
}


def record_verdict(
    conn,
    run_id,
    judging_pass_id,
    variation_id,
    ticker,
    graded_formula_version,
    strategy_id,
    strategy_version,
    dataset_sha256,
    verdict,
    is_advisory,
    battery_version,
    evidence_floor_outcome,
    check_outcomes_json,
    decided_by,
    warnings_json,
    graded_at,
):
    """The single entry point that writes a verdict row (AC-6), named in the job file, "mirroring
    submit_paper_order(conn, ...)". conn is an open connection from get_judge_conn; the remaining
    sixteen arguments are the row's own columns (architecture section 2), named explicitly rather
    than accepted as **kwargs so a caller who forgets one fails at call time with a clear TypeError
    rather than writing a silently incomplete row.

    THE EXACTLY-ONCE GATE (AC-6, architecture section 3 item 3, borrowing researchdb.py's own
    cell-write idiom verbatim: "if a ... row already exists for (run_id, variation_id, ticker,
    'cell') ... SKIP the whole cell"). "Already exists" is exactly the table's own UNIQUE
    (run_id, variation_id, ticker, judging_pass_id) constraint (architecture section 2b item 1).
    The INSERT below targets that specific constraint with SQLite's own upsert syntax, ON CONFLICT
    (...) DO NOTHING: a conflict on THIS constraint is silently skipped, a quiet return matching
    architecture section 3 item 3's own words, "skips, cell by cell, and cannot write any of them
    twice", while a violation of any OTHER constraint on this table (a NOT NULL column omitted, or
    an invalid verdict value) is not targeted by this ON CONFLICT clause and still raises, which
    this function catches and re-raises as VerdictConstraintViolation so no bare
    sqlite3.IntegrityError escapes this entry point either way.

    A single atomic statement is used rather than a SELECT-then-INSERT specifically because two
    grading passes can race (job file, "what can break" item 2): a check-then-act pair leaves a
    window between the two statements where both callers can see "no row yet" and both insert,
    while one INSERT ... ON CONFLICT DO NOTHING is resolved entirely inside SQLite's own conflict
    handling, with no such window.

    Returns the new row's integer id on a genuine first write, or None when the write was skipped
    because the quadruple already existed. Either way the row already on disk, if any, is left
    completely alone: this function never reads it back and never rewrites it, so the first call's
    own stored verdict is never touched by a later call.
    """
    values_by_column = {
        "run_id": run_id, "judging_pass_id": judging_pass_id, "variation_id": variation_id,
        "ticker": ticker, "graded_formula_version": graded_formula_version,
        "strategy_id": strategy_id, "strategy_version": strategy_version,
        "dataset_sha256": dataset_sha256, "verdict": verdict, "is_advisory": is_advisory,
        "battery_version": battery_version, "evidence_floor_outcome": evidence_floor_outcome,
        "check_outcomes_json": check_outcomes_json, "decided_by": decided_by,
        "warnings_json": warnings_json, "graded_at": graded_at,
    }
    ordered_values = tuple(values_by_column[column] for column in _INSERT_COLUMNS)

    sql = "INSERT INTO research_judge_verdicts (%s) VALUES (%s) ON CONFLICT (%s) DO NOTHING" % (
        ", ".join(_INSERT_COLUMNS),
        ", ".join(["?"] * len(_INSERT_COLUMNS)),
        ", ".join(_EXACTLY_ONCE_KEY_COLUMNS),
    )

    try:
        cursor = conn.execute(sql, ordered_values)
    except sqlite3.IntegrityError as exc:
        # NOT the exactly-once conflict (that path never raises; the ON CONFLICT clause above
        # resolves it silently). Some OTHER constraint refused this row, for example a NOT NULL
        # column omitted or an unrecognised verdict value tripping the CHECK constraint. Re-raised
        # as a JudgeError subclass, chained with "from exc" so the original sqlite3 message and
        # traceback are not lost.
        raise VerdictConstraintViolation(
            f"record_verdict refused to write a row for judging_pass_id={judging_pass_id!r}, "
            f"run_id={run_id!r}, variation_id={variation_id!r}, ticker={ticker!r}: {exc}"
        ) from exc

    if cursor.rowcount == 0:
        # The exactly-once gate fired: a row for this quadruple already existed, so ON CONFLICT DO
        # NOTHING left it untouched and inserted nothing. Counted here because this IS the one
        # failure mode this unit's own scope can observe and drive (job file, "what can break"
        # item 2); the quiet return itself is the specified behaviour, architecture section 3 item
        # 3, not an error.
        JUDGE_MONITOR_COUNTERS["duplicate_write_refused_count"] += 1
        return None

    return cursor.lastrowid
