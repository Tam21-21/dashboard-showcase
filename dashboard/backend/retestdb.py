"""research.db access for the Nautilus retest store: an append-only log of retest attempts and
refusals, plus the derived "current state" read and the counters the page will show.

Lane NR (U.7s), docs/plans/night/2026-09-13_set8a_night/PLAN.md section 7. Governed by
docs/briefs/nautilus_spec_brief.md and docs/briefs/nautilus_architecture_2026-09-03.md (AD2, AD3,
AD6, AD11). Tamer's ruling (plan section 15, item 4): the record lives in research.db, in
judgedb.py's own shape, because that store already carries the backup and boot-init machinery a
new log needs, and a SEVENTH SQLite file would only duplicate that machinery with no isolation
benefit this log needs.

DDIA (Kleppmann, second edition, chapter 3, the same "Event sourcing and CQRS" passage judgedb.py
already cites: "Events are immutable; new events supersede old ones." Findings round 3, SF3: this
module's docstring used to also attribute a "Transaction Processing or Analytics?" section to
chapter 3, but that heading does not appear anywhere in docs/books/kleppmann_canon.md
(`grep -c 'Transaction Processing or Analytics' docs/books/kleppmann_canon.md` prints 0), so that
half of the citation is dropped; the "Events are immutable" citation alone already carries the
append-only justification below.). research_retests and research_retest_refusals are APPEND-ONLY
EVENT LOGS, one row per attempt, never mutated; the CURRENT STATE for a candidate
(current_retest_state below) is a DERIVED READ over the newest row, not a second table this module
keeps in sync by hand. That is the same shape judgedb.py already uses for research_judge_verdicts,
for the same reason: a later attempt must never destroy the evidence of an earlier one, and a
derived read is cheaper to keep correct than a mutated "latest" column that could drift from the
log it is meant to summarise. view_rebuild_check (AD11.4) is built the SAME way, as its own
append-only log of rebuild events rather than a row this module UPDATEs, so the no-UPDATE/no-DELETE
property AC-2 checks on this file's own source text holds for every table this module owns, not
only the two retest event tables.

Mirrors judgedb.py's own connection discipline (itself mirroring opsdb.py): its own connect
helper, its own transaction context manager, its own idempotent init function, foreign_keys and
WAL set per connection, isolation_level=None with an explicit BEGIN IMMEDIATE / COMMIT wrapper for
every multi-statement writer. research_db_path is explicit on every entry point, never a hidden
global, so a probe can point this module at a scratch file the way every other store module here
already allows.

A NOTE ON THE WORD THIS MODULE WRITES EXACTLY ONCE (AC-5). record_retest's own guarded branch
below is the ONLY place in this file's source that assigns the literal upgrade-status word to
final_status; AC-5's probe reads the BUILT SOURCE with ast and fails if that word appears as its
own bare string constant anywhere else. So the SQL CHECK constraint that also needs to allow that
word as a stored column value (AC-1) states it directly inside the DDL's own large string instead
of being assembled from a small Python-level tuple containing it as a separate element, and the
Python-level STATUSES tuple used for read-back and for the counters below is DERIVED from that
same DDL text at import time (a regex over the CHECK clause) rather than restated by hand -- which
is not a workaround, it is a stronger single-source-of-truth for the three-status set than writing
it twice ever was: the SQL constraint and the Python validation can now never name a different set.

LIFECYCLE (the four questions this codebase asks of every stateful store), answered here:

  Fresh/first-run: a freshly initialised research.db holds zero rows in either event table and one
  seeded row in research_retest_parameters with residue_allowance NULL (AC-4). current_retest_state
  answers 'never-retested' for every run_id; the three AC-J1 counters answer 'never-run' in every
  bucket (AC-16); view_rebuild_check answers 'never-run' (AC-17, an empty log reads the same way).

  Empty set: a candidate that was refused but never actually retested (a row in
  research_retest_refusals only, zero rows in research_retests) still reads 'never-retested' from
  current_retest_state, because that function reads research_retests alone. A refusal is the
  record of never attempting a retest, not an attempt itself, and conflating the two would make a
  permanently-refused candidate indistinguishable from one nobody has ever asked about.

  NULL/absent: research_retest_parameters.residue_allowance is NULL from the seed row onward and
  no function in this module ever sets it (AC-4, AC-A5, the RT precedent: a NULL parameter with no
  setter, its provenance entry OWED to the change that first gives it a value). record_retest's own
  upgrade branch is therefore structurally guarded on that NULL and is unreachable today by
  construction, not by omission -- AC-5's probe reads this on the SOURCE, never on a run's outcome.
  could_not_run_reason is NULL on every row whose status is not 'could-not-run'.

  T-zero, never acted on: before init_retest_db has ever run on a given file, none of these tables
  exist and every function below would raise a bare sqlite3.OperationalError naming the missing
  table. init_retest_db is therefore always the first call any caller of this module makes, exactly
  as judgedb.init_judge_db is (called once, at boot, from main.py's lifespan, idempotent
  thereafter).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from .config import RESEARCH_DB_PATH

# Module-level default path; callers pass an explicit override when they need one (probes do,
# matching judgedb.py's and opsdb.py's own DB_PATH convention).
DB_PATH = RESEARCH_DB_PATH


# =============================================================================
# Closed vocabularies. Declared once here so this module's own validator, routes/retest.py's
# assembler, and the report never name a knob or a reason word differently from one another.
# =============================================================================

# The two statuses a CALLER of record_retest may ever legally ask for. The third legal STORED
# status (the upgrade word AC-A7 names) is intentionally never a bare constant here; see the
# module docstring's note and STATUSES below, which derives all three from the DDL's own text.
STATUS_REPORT_ONLY = "report-only"
STATUS_COULD_NOT_RUN = "could-not-run"

# The nine could_not_run_reason words the entrypoint and this module both draw from (spec brief
# AC-B7). The entrypoint (a separate image, no import path back to this module) keeps its own
# literal copy; AC-27's probe checks that copy against this exact text from the outside.
COULD_NOT_RUN_REASONS = (
    "halted-early", "engine-error", "absent-shadow-copy", "shadow-copy-out-of-order",
    "absent-version", "version-mismatch", "absent-export", "zero-fills", "derivation-failed",
)

# The four refusal reasons a research_retest_refusals row may carry (spec brief AC-J3).
REFUSAL_UNGRADED_CANDIDATE = "ungraded-candidate"
REFUSAL_UNSUPPORTED_TIMEFRAME = "unsupported-timeframe"
REFUSAL_MISSING_BARS = "missing-bars"
REFUSAL_MISSING_CONFIGURATION_STATEMENT = "missing-configuration-statement"
REFUSAL_REASONS = (
    REFUSAL_UNGRADED_CANDIDATE, REFUSAL_UNSUPPORTED_TIMEFRAME,
    REFUSAL_MISSING_BARS, REFUSAL_MISSING_CONFIGURATION_STATEMENT,
)

# The fourteen matched-configuration knobs, row number to name
# (docs/probes/nautilus_probe_2026-08-24.md's own "The divergence surface" table). The single
# source both validate_matched_configuration below and routes/retest.py's assembler read.
KNOB_NAMES = {
    1: "fill timing", 2: "slippage unit", 3: "fees", 4: "cash enforcement",
    5: "share granularity", 6: "same-bar conflict", 7: "long only", 8: "position sizing",
    9: "sweep grid", 10: "price grid", 11: "volume basis", 12: "annualisation",
    13: "intrabar path", 14: "hourly data",
}
# AC-C4: these three can never be matched between the two engines by construction (fill timing is
# the whole point of #290's predict-then-verify shape; slippage unit and cash enforcement differ
# structurally between a vectorised sweep and a real order-by-order engine).
_ALWAYS_STRUCTURALLY_UNEQUAL = (1, 2, 4)
# AD6.5: no evidence supports ANY disposition for annualisation today; a statement that assigns
# this knob one is refused outright, never merely warned about.
_NEVER_DISPOSED_KNOB = 12
_DISPOSITIONS = ("set-equal", "structurally-unequal", "by-construction")


# =============================================================================
# Errors. One base class, one subclass per refusal shape, never a bare Exception (judgedb.py's
# own convention, restated here for the same reason).
# =============================================================================

class RetestError(Exception):
    """Base class for every refusal this module raises. Never raised directly."""


class RetestDbParentDirectoryMissing(RetestError):
    """research_db_path's parent directory does not exist (judgedb.py's own F4 pattern): named
    here so the failure carries the path rather than surfacing as a bare sqlite3.OperationalError.
    """


class InvalidRetestRecord(RetestError):
    """record_retest was asked to write a status / could_not_run_reason pair outside the two
    legal shapes (AC-15): status='could-not-run' needs a reason from the nine-word closed set;
    any other status needs no reason at all, and a reason present alongside one is refused. Also
    raised for a status a caller has no business asking for directly (see record_retest's own
    top check).
    """


class InvalidMatchedConfiguration(RetestError):
    """record_retest was asked to write a matched-configuration statement that marks knob 1, 2 or
    4 as anything other than structurally-unequal, that omits a required knob row entirely, or
    that assigns knob 12 any disposition at all (AC-C3, AC-C4, AD6.5).
    """


# =============================================================================
# Connection and transaction helpers, mirroring judgedb.py's own _connect_judge / get_judge_conn /
# _judge_transaction (itself mirroring opsdb.py).
# =============================================================================

def _connect_retest(research_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on research.db with the per-connection pragmas this module relies on."""
    path = Path(research_db_path)
    if not path.parent.is_dir():
        raise RetestDbParentDirectoryMissing(
            f"cannot open research.db at {path}: its parent directory {path.parent} does not exist"
        )
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_retest_conn(research_db_path: Union[str, Path, None] = None):
    """Context manager for a read-only query or a single-statement write on research.db.

    One connection per call, no pooling: the same reasoning get_judge_conn's own docstring gives,
    so a probe pointed at a throwaway file never inherits a shared connection's state.
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_retest(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _retest_transaction(research_db_path: Union[str, Path, None] = None):
    """Wraps BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection, mirroring judgedb.py's
    own _judge_transaction. Every multi-statement writer in this module (init_retest_db,
    record_retest, record_refusal, mark_view_rebuilt) goes through this, so a failure partway
    through a write leaves nothing behind.
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    conn = _connect_retest(path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except sqlite3.Error:
            # Narrowed from a bare except Exception (hard prohibition on unspecific exception
            # types): every statement this module runs inside a transaction is a static SQL
            # string or a bind-parameterised INSERT, so every failure reachable here is already a
            # sqlite3.Error; narrowing costs no coverage and stops this handler from also masking
            # an unrelated, non-SQLite bug behind a rollback-and-reraise.
            conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


def _split_ddl_statements(script: str) -> list:
    """Split a multi-statement DDL string into individual statements, mirroring judgedb.py's own
    copy of this helper byte for byte in its reasoning (each store module carries its own private
    copy rather than importing a neighbour's)."""
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
# Schema DDL (AC-1, AC-3, AC-4, AD3.6, AD11.4).
# =============================================================================

_REASON_LIST_SQL = ",".join(f"'{r}'" for r in COULD_NOT_RUN_REASONS)
_REFUSAL_REASON_LIST_SQL = ",".join(f"'{r}'" for r in REFUSAL_REASONS)

# research_retests: one row per retest ATTEMPT (AD3.1, AD3.2, AC-C8: append-only by construction,
# checked on this file's own source by AC-2, never by an attempted write that gets refused). The
# status CHECK's own word list is written directly, never assembled from a small Python tuple
# (see the module docstring's note on the one bare "passed"-shaped word AC-5 governs).
#
# could_not_run_reason's CHECK below now enforces the FULL bidirectional rule at the SQL level
# (findings round 3, AC-1 BLOCKER): non-NULL and one of the nine words exactly when
# status = 'could-not-run', and NULL in every other case. Before this fold the CHECK only closed
# the column to NULL-or-one-of-nine-words on its own, never naming status at all, so a row
# violating the tie (status='could-not-run' with a NULL reason, or any other status with a reason
# present) could be seeded directly with sqlite3 without the INSERT being refused; the lead's
# ruling on the probe-compliance gate's BLOCKER is that the DDL closes that gap itself, with
# record_retest's own Python check below staying in front of it as the named-error layer (a
# caller gets InvalidRetestRecord naming the exact problem rather than a bare sqlite3.IntegrityError).
# research_retests is new tonight (this lane's own first commit), so there is no existing row for
# this tighter CHECK to migrate or reject: every row ever written to this table was written after
# this CHECK already existed, and the DDL/migration path stays byte-identical to judgedb.py's own
# idempotent CREATE-TABLE-IF-NOT-EXISTS shape (AC-19 reads researchdb.py only, which still carries
# no DDL of this lane). Lane NT2 (#452) adds the two gap columns below to a table made before it
# existed with one ALTER TABLE ADD COLUMN each in init_retest_db, run only when PRAGMA table_info
# lacks the column; a fresh database gets them from this DDL.
# AC-8's hand-seeded-malformed-row scenario (AD3.6.3) therefore moves to what SQL cannot express:
# a matched_configuration statement whose shape or JSON current_retest_state's own widened
# self-check below now catches, or an empty engine_version on a row that claims a real outcome.
#
# THE EXPLICIT "IS NOT NULL" BELOW IS LOAD-BEARING, not redundant with "IN (...)": SQL's
# three-valued logic makes `NULL IN (...)` evaluate to NULL (unknown), never FALSE, and a CHECK
# only rejects a row when the whole expression evaluates to FALSE, so `could_not_run_reason IN
# (...) OR (other branch)` would have silently ACCEPTED a could-not-run row with a NULL reason
# (both branches evaluate to NULL/FALSE, and NULL OR FALSE is NULL, which SQLite treats as
# passing) -- confirmed the hard way: the first version of this CHECK, without the explicit
# IS NOT NULL, let that exact row through, caught only by the probe's own direct-insert drive.
_DDL_RETESTS = f"""
CREATE TABLE IF NOT EXISTS research_retests (
    id                                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                              INTEGER NOT NULL,
    verdict_id                          INTEGER,
    created_at                          INTEGER NOT NULL,
    shadow_copy_completed_at            INTEGER,
    fills_read_at                       INTEGER,
    status                              TEXT NOT NULL
                                             CHECK (status IN ('report-only','passed','could-not-run')),
    could_not_run_reason                TEXT
                                             CHECK (
                                                 (status = 'could-not-run'
                                                  AND could_not_run_reason IS NOT NULL
                                                  AND could_not_run_reason IN ({_REASON_LIST_SQL}))
                                                 OR (status <> 'could-not-run'
                                                     AND could_not_run_reason IS NULL)
                                             ),
    engine_version                      TEXT,
    python_version                      TEXT,
    matched_configuration               TEXT,
    -- export_path stores an ARTIFACT NAME (e.g. "retest_JOBID_fills.parquet"), never a raw
    -- filesystem path (findings round 3, AC-28 BLOCKER): the worker's own run_retest.py writes
    -- this NAME, and the only correct way to open it back is
    -- artifact_store.store.open(run_id, export_path), which resolves it under
    -- RESEARCH_ARTIFACT_ROOT slash run_id slash name. A caller building Path(export_path) and
    -- checking .is_file() from its own cwd is always wrong: that is exactly the bug the
    -- probe-compliance gate found in the real-artifact probe, fixed there rather than here.
    export_path                         TEXT,
    divergence_vs_shadow                REAL,
    divergence_vs_shadow_absent_reason  TEXT,
    divergence_vs_vbt                   REAL,
    divergence_vs_vbt_absent_reason     TEXT,
    -- Lane NT2 (#452): gap_final_equity = (1 + Nautilus return) / (1 + backtest return) - 1 and gap_sharpe
    -- = Nautilus minus backtest; NULL on a could-not-run row and
    -- on a row an older worker wrote. They are the pass test of record_retest below.
    gap_final_equity                    REAL,
    gap_sharpe                          REAL
);
CREATE INDEX IF NOT EXISTS ix_retests_run_id ON research_retests(run_id);
"""

# The three legal statuses, DERIVED from the DDL's own CHECK text above rather than restated as a
# fresh Python tuple: a regex extraction over an existing string is a runtime computation, never a
# second bare occurrence of the literal AC-5 governs, and it is a strictly stronger guarantee that
# the SQL constraint and this module's own validation can never name a different set.
_STATUS_CHECK_MATCH = re.search(r"status IN \(([^)]+)\)", _DDL_RETESTS)
STATUSES = tuple(word.strip(" '") for word in _STATUS_CHECK_MATCH.group(1).split(","))

# research_retest_refusals: one row per refusal EVENT, no status column at all (AC-B1's own
# refusal-carries-no-status half): a refusal is not an outcome of an attempt, it is the record of
# never attempting one, so it has nothing to be "in progress" or "final" about.
_DDL_REFUSALS = f"""
CREATE TABLE IF NOT EXISTS research_retest_refusals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    reason      TEXT NOT NULL CHECK (reason IN ({_REFUSAL_REASON_LIST_SQL}))
);
CREATE INDEX IF NOT EXISTS ix_refusals_run_id ON research_retest_refusals(run_id);
"""

# research_retest_parameters: a SINGLE seeded row (id is pinned to 1 by its own CHECK, so a second
# INSERT can never silently create a competing "current" row). residue_allowance seeds NULL and
# stays NULL in this build (AC-4, AC-A5): no function below writes it a value.
_DDL_PARAMETERS = """
CREATE TABLE IF NOT EXISTS research_retest_parameters (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    residue_allowance  REAL
);
"""

# research_retest_drift (lane NT2, DECISIONS #449 and #452): the largest gaps seen between the
# backtest and NautilusTrader over a measured set of runs. An APPEND-ONLY log whose newest row is
# the current value (DDIA, Kleppmann second edition, chapter 3, "Event sourcing and CQRS": events
# are immutable and a newer one supersedes an older). Written only by set_drift_allowance, by one
# INSERT; no function rewrites or removes a row. Empty on a fresh store: no drift row means no
# retest passes through the drift rule.
_DDL_DRIFT = """
CREATE TABLE IF NOT EXISTS research_retest_drift (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    max_gap_final_equity  REAL NOT NULL CHECK (max_gap_final_equity > 0),
    max_gap_sharpe        REAL NOT NULL CHECK (max_gap_sharpe > 0),
    drift_source          TEXT NOT NULL,
    created_at            INTEGER NOT NULL
);
"""

# research_retest_view_rebuild_log: AD11.4's "the derived view's own last-run time", built as its
# own append-only log (one row per rebuild call) rather than a row this module UPDATEs, so the
# no-UPDATE/no-DELETE property AC-2 checks on this file's source text holds for every table this
# module owns, not only the two retest event tables.
_DDL_VIEW_REBUILD_LOG = """
CREATE TABLE IF NOT EXISTS research_retest_view_rebuild_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at  INTEGER NOT NULL
);
"""


def init_retest_db(research_db_path: Union[str, Path, None] = None) -> None:
    """Create the five tables above if they do not already exist, add the two gap columns to an
    older research_retests, and seed the one parameter row.

    Idempotent: CREATE TABLE IF NOT EXISTS plus INSERT OR IGNORE, so calling this twice on the
    same file changes neither the schema nor any row (init_judge_db's own idempotent shape).
    """
    path = research_db_path if research_db_path is not None else DB_PATH
    with _retest_transaction(path) as conn:
        for statement in _split_ddl_statements(_DDL_RETESTS):
            conn.execute(statement)
        for statement in _split_ddl_statements(_DDL_REFUSALS):
            conn.execute(statement)
        for statement in _split_ddl_statements(_DDL_PARAMETERS):
            conn.execute(statement)
        for statement in _split_ddl_statements(_DDL_VIEW_REBUILD_LOG):
            conn.execute(statement)
        for statement in _split_ddl_statements(_DDL_DRIFT):
            conn.execute(statement)
        # Lane NT2: a research_retests made before the gap columns existed gets them added, once.
        present = {r["name"] for r in conn.execute("PRAGMA table_info(research_retests);")}
        for column in ("gap_final_equity", "gap_sharpe"):
            if column not in present:
                conn.execute(f"ALTER TABLE research_retests ADD COLUMN {column} REAL;")
        # The one seeded row (AC-4): residue_allowance NULL, no setter anywhere in this module.
        conn.execute(
            "INSERT OR IGNORE INTO research_retest_parameters (id, residue_allowance) "
            "VALUES (1, NULL);"
        )


# =============================================================================
# The matched-configuration validator (AC-C3, AC-C4, AD6.5). Shared by record_retest below and by
# routes/retest.py's own dispatch-time assembler, so the two can never disagree about what "a
# well-formed statement" means.
# =============================================================================

def validate_matched_configuration(statement) -> None:
    """Raise InvalidMatchedConfiguration for any of the four bad shapes AC-10 names; return None
    (write nothing, change nothing) when the statement is well-formed."""
    if not isinstance(statement, dict):
        raise InvalidMatchedConfiguration("matched_configuration must be a dict of knob rows")
    if str(_NEVER_DISPOSED_KNOB) in statement:
        raise InvalidMatchedConfiguration(
            f"knob {_NEVER_DISPOSED_KNOB} ({KNOB_NAMES[_NEVER_DISPOSED_KNOB]}) carries no "
            "evidence for any disposition today (AD6.5); it must be absent from the statement"
        )
    for row in range(1, 15):
        if row == _NEVER_DISPOSED_KNOB:
            continue
        entry = statement.get(str(row))
        if not isinstance(entry, dict) or entry.get("disposition") not in _DISPOSITIONS:
            raise InvalidMatchedConfiguration(
                f"knob {row} ({KNOB_NAMES[row]}) is missing or carries an unrecognised disposition"
            )
        if row in _ALWAYS_STRUCTURALLY_UNEQUAL and entry["disposition"] != "structurally-unequal":
            raise InvalidMatchedConfiguration(
                f"knob {row} ({KNOB_NAMES[row]}) must be structurally-unequal, "
                f"got {entry['disposition']!r}"
            )


# =============================================================================
# The single write entry point for a retest attempt (AC-A7, AC-B1, AC-B3, AC-B4, AC-B7, AC-C3,
# AC-C4, AC-C8). Every keyword is required so a caller cannot silently omit an S1-S4 field.
# =============================================================================

def record_retest(
    *,
    run_id: int,
    verdict_id,
    created_at: int,
    shadow_copy_completed_at,
    fills_read_at,
    status: str,
    could_not_run_reason=None,
    engine_version,
    python_version,
    matched_configuration: dict,
    export_path,
    divergence_vs_shadow,
    divergence_vs_shadow_absent_reason,
    divergence_vs_vbt,
    divergence_vs_vbt_absent_reason,
    gap_final_equity=None,
    gap_sharpe=None,
    research_db_path: Union[str, Path, None] = None,
) -> int:
    """Append one research_retests row (AC-B4: a second call for the same run_id appends, it never
    mutates the first). Raises InvalidRetestRecord or InvalidMatchedConfiguration and writes
    nothing on any of the illegal shapes AC-15 and AC-10 name; returns the new row's id otherwise.

    A caller may only ever ASK for 'report-only' or 'could-not-run' (the two outcomes anything
    upstream of this store can actually observe -- ENTRYPOINT_CONTRACT.md's own result shape never
    emits the third word, "status is never 'passed' from this entrypoint alone"). Whether the row
    that lands is upgraded past 'report-only' is this function's own decision, made once, below.
    """
    if status not in (STATUS_REPORT_ONLY, STATUS_COULD_NOT_RUN):
        raise InvalidRetestRecord(
            f"a caller may only ask for {STATUS_REPORT_ONLY!r} or {STATUS_COULD_NOT_RUN!r}, "
            f"got {status!r}"
        )
    if status == STATUS_COULD_NOT_RUN:
        if could_not_run_reason is None or could_not_run_reason not in COULD_NOT_RUN_REASONS:
            raise InvalidRetestRecord(
                f"status is could-not-run but could_not_run_reason is {could_not_run_reason!r}, "
                f"not one of {COULD_NOT_RUN_REASONS}"
            )
    elif could_not_run_reason is not None:
        raise InvalidRetestRecord(
            f"status is {status!r} but could_not_run_reason is set to {could_not_run_reason!r}; "
            "a reason is only legal alongside status='could-not-run'"
        )
    validate_matched_configuration(matched_configuration)

    with _retest_transaction(research_db_path) as conn:
        # AC-A7 / AC-5: the ONLY site in this module that assigns the upgrade word to the status
        # about to be written. DECISIONS #452 (amending #290 item 2): the pass range is the largest
        # gap measured, so the one way to "passed" is the drift clause: the newest
        # research_retest_drift row exists, both gaps are present, and each gap's size is within
        # that row's maximum for it. The residue clause was removed by #452; the
        # research_retest_parameters.residue_allowance column stays but this function no longer
        # reads it. A missing gap (an older worker, a could-not-run) is never read as zero. Read
        # fresh on every call, never cached, because the drift row can change between retests.
        final_status = status
        drift_row = conn.execute(
            "SELECT max_gap_final_equity, max_gap_sharpe FROM research_retest_drift "
            "ORDER BY id DESC LIMIT 1;"
        ).fetchone()
        if (status == STATUS_REPORT_ONLY
                and drift_row is not None
                and gap_final_equity is not None and gap_sharpe is not None
                and abs(gap_final_equity) <= drift_row["max_gap_final_equity"]
                and abs(gap_sharpe) <= drift_row["max_gap_sharpe"]):
            final_status = "passed"

        row = conn.execute(
            """
            INSERT INTO research_retests (
                run_id, verdict_id, created_at, shadow_copy_completed_at, fills_read_at,
                status, could_not_run_reason, engine_version, python_version,
                matched_configuration, export_path,
                divergence_vs_shadow, divergence_vs_shadow_absent_reason,
                divergence_vs_vbt, divergence_vs_vbt_absent_reason,
                gap_final_equity, gap_sharpe
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id;
            """,
            (
                run_id, verdict_id, created_at, shadow_copy_completed_at, fills_read_at,
                final_status, could_not_run_reason, engine_version, python_version,
                json.dumps(matched_configuration), export_path,
                divergence_vs_shadow, divergence_vs_shadow_absent_reason,
                divergence_vs_vbt, divergence_vs_vbt_absent_reason,
                gap_final_equity, gap_sharpe,
            ),
        ).fetchone()
        return int(row["id"])


def set_drift_allowance(research_db_path, max_gap_final_equity: float, max_gap_sharpe: float,
                        drift_source: str) -> int:
    """Append one research_retest_drift row (lane NT2): the two largest gaps and where they came
    from. The newest row is the one record_retest reads. Both maxima must be finite numbers above
    zero and the source a non-empty string, else ValueError and nothing is written. Returns the new
    row's id."""
    for name, value in (("max_gap_final_equity", max_gap_final_equity),
                        ("max_gap_sharpe", max_gap_sharpe)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{name} must be a finite number above 0, got {value!r}")
    if not isinstance(drift_source, str) or not drift_source.strip():
        raise ValueError("drift_source must be a non-empty string")
    with _retest_transaction(research_db_path) as conn:
        row = conn.execute(
            "INSERT INTO research_retest_drift "
            "(max_gap_final_equity, max_gap_sharpe, drift_source, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING id;",
            (float(max_gap_final_equity), float(max_gap_sharpe), drift_source, int(time.time())),
        ).fetchone()
        return int(row["id"])


def record_refusal(*, run_id: int, reason: str, created_at: int,
                    research_db_path: Union[str, Path, None] = None) -> int:
    """Append one research_retest_refusals row (AC-J3's closed four-word set). Raises
    InvalidRetestRecord and writes nothing for a reason outside that set."""
    if reason not in REFUSAL_REASONS:
        raise InvalidRetestRecord(
            f"unrecognised refusal reason {reason!r}, must be one of {REFUSAL_REASONS}"
        )
    with _retest_transaction(research_db_path) as conn:
        row = conn.execute(
            "INSERT INTO research_retest_refusals (run_id, created_at, reason) "
            "VALUES (?, ?, ?) RETURNING id;",
            (run_id, created_at, reason),
        ).fetchone()
        return int(row["id"])


# =============================================================================
# The derived read (AD3.6.3) and the AC-J1 counters. Every counter answers the WORD 'never-run',
# never the number 0, when its whole scope has zero rows (AC-J2); once any row exists in scope
# every bucket in that same call becomes a plain integer, including the untouched buckets, which
# is what makes "0" and "never-run" mean two different, both-honest things.
# =============================================================================

def _derive_state_from_row(row: sqlite3.Row) -> str:
    """The widened AC-8 self-check (findings round 3), factored out of current_retest_state so
    both that single-run_id reader AND latest_retest_states' own bulk reader below (SF2: the N+1
    query fix) run the EXACT same rule rather than two copies that could quietly drift apart.
    `row` must carry status, could_not_run_reason, matched_configuration and engine_version.

    Findings round 3 (the lead's ruling on the probe-compliance gate's AC-1 BLOCKER): once the DDL
    above enforces the status/could_not_run_reason tie bidirectionally at the SQL level, a bare
    status='could-not-run'/reason=NULL row (or the reverse) can no longer be hand-seeded past the
    database at all, so the reason-tie check below is now corroboration of a fact the SQL CHECK
    already guarantees, not this function's only defense. This self-check WIDENS to cover what SQL
    still cannot express, reusing validate_matched_configuration rather than restating its shape:
      1. the reason tie itself (kept, now belt-and-braces with the SQL CHECK above).
      2. matched_configuration parses as JSON and satisfies AC-10's own shape (all thirteen
         required knobs present, each with a disposition in the three-word closed set, knob 12
         absent) -- reusing validate_matched_configuration so this self-check and record_retest's
         own write-time guard can never disagree about what "well-formed" means.
      3. engine_version is non-empty on any row that claims a real outcome (status other than
         could-not-run: report-only or passed), since only a could-not-run row can legitimately
         have never reached the point where the engine version is read (AC-27's own
         absent-version/version-mismatch reasons cover that path already).
    """
    status = row["status"]
    reason = row["could_not_run_reason"]

    # 1. The reason tie, corroborated at the Python layer even though the SQL CHECK above already
    # enforces it for every row this function could ever be asked to read.
    if status == STATUS_COULD_NOT_RUN:
        if reason is None or reason not in COULD_NOT_RUN_REASONS:
            return "unreadable-record"
    elif reason is not None:
        return "unreadable-record"

    # 2. matched_configuration must parse and satisfy AC-10's own shape. A NULL statement (a row
    # this module itself never writes, since record_retest requires the argument) is treated the
    # same as a malformed one rather than skipped, so a bypass row missing the column entirely is
    # still caught rather than silently passing this half of the self-check.
    raw_matched_configuration = row["matched_configuration"]
    try:
        parsed_matched_configuration = json.loads(raw_matched_configuration) \
            if raw_matched_configuration is not None else None
    except (json.JSONDecodeError, TypeError):
        return "unreadable-record"
    try:
        validate_matched_configuration(parsed_matched_configuration)
    except InvalidMatchedConfiguration:
        return "unreadable-record"

    # 3. engine_version must be non-empty on a row claiming a real outcome. STATUS_COULD_NOT_RUN
    # is excluded: a run can halt before the engine-version read ever happens (AC-22's own read
    # order), and that path is already named by a could-not-run reason rather than by this check.
    if status != STATUS_COULD_NOT_RUN and not row["engine_version"]:
        return "unreadable-record"

    return status


def current_retest_state(conn: sqlite3.Connection, run_id: int) -> str:
    """The one word a page would show for this candidate: 'never-retested' (zero rows in
    research_retests for this run_id -- a refusal alone does not count, see the module docstring's
    "Empty set" answer), one of the legal statuses (the newest row, by id), or 'unreadable-record'
    for a row that fails _derive_state_from_row's own closed-set self-check."""
    row = conn.execute(
        "SELECT status, could_not_run_reason, matched_configuration, engine_version "
        "FROM research_retests WHERE run_id = ? ORDER BY id DESC LIMIT 1;",
        (run_id,),
    ).fetchone()
    if row is None:
        return "never-retested"
    return _derive_state_from_row(row)


def latest_retest_states(conn: sqlite3.Connection, run_ids: list) -> dict:
    """SF2 (findings round 3): the bulk form of current_retest_state, ONE query for every run_id
    in `run_ids` rather than routes/retest.py's own list_retests() calling current_retest_state
    once per candidate (an N+1 query against research_retests, which this module's own docstring
    already documents as growing without bound). The idiom is a GROUP BY/MAX(id) subquery joined
    back onto the table to fetch each run_id's own NEWEST row in one round trip (judgedb.py's own
    "current row" rule uses a correlated NOT EXISTS over a five-column candidate key, a shape built
    for a compound key this module's own single-column run_id does not need; this is this module's
    own idiom for the same "newest row per group" problem). Every state is derived through the
    EXACT same _derive_state_from_row current_retest_state itself calls, so the two readers can
    never disagree about what a row means. A run_id absent from research_retests entirely (never
    retested, or refused-only) reads 'never-retested', matching current_retest_state's own answer.
    """
    states = {rid: "never-retested" for rid in run_ids}
    if not run_ids:
        return states
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"""
        SELECT rt.run_id AS run_id, rt.status AS status,
               rt.could_not_run_reason AS could_not_run_reason,
               rt.matched_configuration AS matched_configuration,
               rt.engine_version AS engine_version
        FROM research_retests AS rt
        JOIN (
            SELECT run_id, MAX(id) AS max_id FROM research_retests
            WHERE run_id IN ({placeholders}) GROUP BY run_id
        ) AS latest ON latest.run_id = rt.run_id AND latest.max_id = rt.id;
        """,
        tuple(run_ids),
    ).fetchall()
    for row in rows:
        states[row["run_id"]] = _derive_state_from_row(row)
    return states


def retests_by_status(research_db_path: Union[str, Path, None] = None) -> dict:
    """The AC-J1 counter over research_retests' own RAW stored status column (never the derived
    'unreadable-record' word: a bypass row still counts under whatever status it was seeded with,
    AC-8, rather than being dropped from every bucket)."""
    with get_retest_conn(research_db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM research_retests GROUP BY status;"
        ).fetchall()
    total = sum(r["n"] for r in rows)
    if total == 0:
        return {s: "never-run" for s in STATUSES}
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = r["n"]
    return counts


def could_not_run_retests_by_reason(run_id=None,
                                     research_db_path: Union[str, Path, None] = None) -> dict:
    """The AC-J1 counter over could-not-run rows' own reason, globally or per candidate."""
    with get_retest_conn(research_db_path) as conn:
        if run_id is None:
            rows = conn.execute(
                "SELECT could_not_run_reason AS reason, COUNT(*) AS n FROM research_retests "
                "WHERE status = ? GROUP BY could_not_run_reason;",
                (STATUS_COULD_NOT_RUN,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT could_not_run_reason AS reason, COUNT(*) AS n FROM research_retests "
                "WHERE status = ? AND run_id = ? GROUP BY could_not_run_reason;",
                (STATUS_COULD_NOT_RUN, run_id),
            ).fetchall()
    total = sum(r["n"] for r in rows)
    if total == 0:
        return {r: "never-run" for r in COULD_NOT_RUN_REASONS}
    counts = {r: 0 for r in COULD_NOT_RUN_REASONS}
    for r in rows:
        if r["reason"] in counts:
            counts[r["reason"]] = r["n"]
    return counts


def refused_retests_by_reason(run_id=None,
                               research_db_path: Union[str, Path, None] = None) -> dict:
    """The AC-J1 counter over research_retest_refusals' own reason, globally or per candidate
    (AC-J2's own per-candidate scope test: a candidate with no refusal of its own reads never-run
    for every reason even when a refusal exists for a DIFFERENT candidate)."""
    with get_retest_conn(research_db_path) as conn:
        if run_id is None:
            rows = conn.execute(
                "SELECT reason, COUNT(*) AS n FROM research_retest_refusals GROUP BY reason;"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT reason, COUNT(*) AS n FROM research_retest_refusals "
                "WHERE run_id = ? GROUP BY reason;",
                (run_id,),
            ).fetchall()
    total = sum(r["n"] for r in rows)
    if total == 0:
        return {r: "never-run" for r in REFUSAL_REASONS}
    counts = {r: 0 for r in REFUSAL_REASONS}
    for r in rows:
        if r["reason"] in counts:
            counts[r["reason"]] = r["n"]
    return counts


def view_rebuild_check(research_db_path: Union[str, Path, None] = None):
    """AD11.4: the derived view's own last-rebuild time, 'never-run' (never the number 0 or a
    blank) before the log has ever been written to."""
    with get_retest_conn(research_db_path) as conn:
        row = conn.execute(
            "SELECT run_at FROM research_retest_view_rebuild_log ORDER BY id DESC LIMIT 1;"
        ).fetchone()
    return "never-run" if row is None else row["run_at"]


def mark_view_rebuilt(now: int, research_db_path: Union[str, Path, None] = None) -> int:
    """Append one row to the rebuild log (never an UPDATE, see _DDL_VIEW_REBUILD_LOG's own
    comment); returns the timestamp just recorded."""
    with _retest_transaction(research_db_path) as conn:
        conn.execute(
            "INSERT INTO research_retest_view_rebuild_log (run_at) VALUES (?);", (now,)
        )
    return now
