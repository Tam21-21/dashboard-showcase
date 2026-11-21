"""real.db access: the store behind the real-trading path, night one (lane RT, docs/plans/night/
2026-09-12_set6a_night/PLAN.md section 9), building the parts of
docs/briefs/real_trading_plumbing_spec_brief.md this session's three-night split calls E1 (the
store itself), E2 (the kill switch), E3 (the enabling record), E7 (the counters, all derived at
read time rather than stored) and E8 (the kill drill record).

THIS BUILD NEVER SENDS AN ORDER AND NEVER TURNS REAL TRADING ON. Night one's own order-gate helper
(sheet item 5 of #394/#395; see the note beside the recording double below) is DELETED as of night
three: `real_orders.submit_real_order` is the whole order path now, tested only against a recording
double; no live broker connection exists in this repository (section 19 stop condition 2), and no
function anywhere in this module ever writes a row to `real_enabling_records` (section 18 decision
4, AC-G2): that append happens by hand, out of band, at the go the ledger records, which is the
money path law's own third precondition.

Why a separate file rather than a table added to an existing store (section 18 decision 1, DDIA
chapter 7, "Sharding for multitenancy", read line by line in the main checkout's
docs/books/kleppmann_canon.md). Three of that section's bullets apply here directly, quoted:
"Resource isolation between tenants.", "Permission isolation (separate physical stores limit
access-control bug blast radius).", "Per-tenant backup and restore." In plain words: the real
account is the one tenant on this whole system that most needs its own blast radius, because a
permission bug or a stray write can only reach what shares its file, and a store nothing else
writes can be backed up and restored on its own. There is one real account, not many small ones,
and no query anywhere in this module joins this store to any other, so the canon's own
counter-cases (many tiny tenants, cross-shard joins) do not bite here. This follows
`dashboard/backend/opsdb.py` and `dashboard/backend/paperdb.py`'s own precedent exactly: SQLite is
single-writer per database, so a money store never waits behind another store's writer, and its
backup unit is its own.

THIS IS A MONEY PATH. Every refusal below is a specific named exception or a specific named
string, recorded on the caller's own return value, and this module never quietly resizes or
substitutes a value to make a check pass (brief section 12, "every assumption failure is a loud
named stop"). A connection that forgot "PRAGMA foreign_keys = ON" would let an enabling record
name a kill drill row that was never written; `_connect_real` is the ONLY function in this module
that opens a raw connection, so it is the only place that pragma needs to be set.

E7's own rule, carried once here rather than restated at every function below: every counter and
gauge this build reports is DERIVED at read time from stored state, never a running total kept in
a module-level dictionary. `get_kill_state`, `get_kill_drill_state` and `derive_enabled` are
themselves that derivation: a fresh account reads the named word `never-armed` or `never-drilled`
rather than a number, and there is nothing to initialise and no window in which a fresh install
reads a zero before its first write.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from .config import REAL_DB_PATH

# Module-level default path; every probe overrides this explicitly (or points the routes at a
# throwaway file by assigning DB_PATH directly), matching paperdb.DB_PATH's own convention.
DB_PATH = REAL_DB_PATH

# Section 16's nine parameters, in the brief's own table order. This is the one place their order
# is decided; every other list in this module (the seed loop, the per-parameter comparison in
# derive_enabled) walks this same tuple rather than repeating the nine names a second time.
REAL_TRADING_PARAMETER_KEYS = (
    "real_trading.per_order_notional_cap",
    "real_trading.ticker_net_cap",
    "real_trading.total_net_cap",
    "real_trading.loss_limit_window",
    "real_trading.loss_limit_since_go",
    "real_trading.order_rate_cap",
    "real_trading.reconciliation_max_age",
    "real_trading.account_value_max_age",
    "real_trading.kill_drill_max_age",
)

# Lane RL, night two, round 1 fold (finding 7 / code-quality SF2): the six hard-limit refusal
# words (brief AC-L1, section 12.1's table) named ONCE here, beside this module's own other word
# tuples, rather than spelled out a second time as a literal string in both real_limits.py (which
# returns them) and real_orders.py (which classifies breach-detail rows by them). A rename or a
# seventh limit added to one file without the other used to be able to silently drop that limit's
# breach_* audit columns to NULL on a real breach; importing these same six names from here closes
# that gap structurally rather than by convention.
LIMIT_PER_ORDER_NOTIONAL = "limit-per-order-notional"
LIMIT_TICKER_NET_CAP = "limit-ticker-net-cap"
LIMIT_TOTAL_NET_CAP = "limit-total-net-cap"
LIMIT_LOSS_WINDOW = "limit-loss-window"
LIMIT_LOSS_SINCE_GO = "limit-loss-since-go"
LIMIT_ORDER_RATE = "limit-order-rate"
SIX_HARD_LIMIT_REASONS = frozenset((
    LIMIT_PER_ORDER_NOTIONAL, LIMIT_TICKER_NET_CAP, LIMIT_TOTAL_NET_CAP,
    LIMIT_LOSS_WINDOW, LIMIT_LOSS_SINCE_GO, LIMIT_ORDER_RATE,
))


# =============================================================================
# Exceptions. One per named refusal or corruption this module can raise. Every one carries the
# structured detail a caller needs (the offending state, the missing keys) rather than a bare
# message a caller would have to re-parse.
# =============================================================================

class RealDbParentDirectoryMissing(RuntimeError):
    """Raised when real_db_path's parent directory does not exist, before sqlite3 ever opens it."""


class RealTradingNotConfigured(RuntimeError):
    """Raised by assert_parameters_configured when one or more of the nine section-16 parameters
    has no configured value (a NULL row, or no row at all). AC-P1: no parameter in this unit has a
    default anywhere, so a missing value must refuse rather than silently read as unbounded.
    """

    def __init__(self, missing_keys):
        self.missing_keys = list(missing_keys)
        super().__init__(
            "the real-trading gate refuses to run: unconfigured parameter(s) %s"
            % ", ".join(self.missing_keys)
        )


class VerificationNotApplicable(RuntimeError):
    """Raised by run_kill_verification when the switch is currently disarmed-verified-clear: there
    is no kill in effect to verify, so the pass is refused rather than run against nothing.
    """

    def __init__(self, state):
        self.state = state
        super().__init__("verification not applicable: the switch is at '%s'" % state)


class KillCommandInTransaction(RuntimeError):
    """Raised by command_kill, before it writes or calls anything, when its connection is not an
    autocommit connection or is already inside a transaction: the armed write must be committed
    before the venue is reached, and only an autocommit connection outside a transaction commits
    it by itself.
    """


class KillDrillAdapterRefused(RuntimeError):
    """Raised by run_kill_drill, before it reads or writes anything, when it is given a real
    adapter (no check that an adapter is in a non-trading configuration exists yet) or a stub
    adapter that is not a RecordingAdapterDouble or a subclass of it: a drill must never be able
    to reach a venue that could hold or move money.
    """


class KillWriteFailed(RuntimeError):
    """Raised by command_kill when the write of the armed state matched other than exactly one
    row, so no venue call is made on the strength of a switch that is not stored as armed.
    """


class KillDisarmRefused(RuntimeError):
    """Raised by disarm_kill from every state except armed-verified-effective. Carries the
    refusing state by name so a caller (the route, in particular) can say why rather than behaving
    as if the disarm had worked.
    """

    def __init__(self, state):
        self.state = state
        super().__init__(
            "disarm refused: the switch is at '%s', only armed-verified-effective may be "
            "disarmed" % state
        )


class AdapterCannotDetermine(RuntimeError):
    """Raised by an adapter's own report_account_state() when it cannot ask the venue at all: a
    pass that could not ask, distinct from a pass that asked and got a clear or unclear answer.
    """


# =============================================================================
# Connection and transaction helpers, mirroring paperdb.py's own _connect_paper / get_paper_conn /
# paper_transaction shape exactly (brief section 3: "the real path uses the SAME shape").
# =============================================================================

def _connect_real(real_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on the real-trading store with the per-connection pragmas this module
    relies on set. The ONLY function in this module that calls sqlite3.connect directly; every
    other function reaches the database through get_real_conn or real_transaction, both of which
    call this, so this is the one place "PRAGMA foreign_keys = ON" can be forgotten.
    """
    path = Path(real_db_path)
    if not path.parent.is_dir():
        raise RealDbParentDirectoryMissing(
            "cannot open the real-trading store at %s: its parent directory %s does not exist"
            % (path, path.parent)
        )
    # Autocommit at the driver level: callers issue their own statements directly against the
    # connection they are handed, the same choice every probe in this lane's suite already makes
    # via its own plain sqlite3.connect(..., isolation_level=None) fixture helper.
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_real_conn(real_db_path: Union[str, Path, None] = None):
    """Context manager for a read-only query or a single-statement write on the real-trading
    store. Per-call lifecycle: opens a fresh connection on enter, closes it on exit.
    """
    path = real_db_path if real_db_path is not None else DB_PATH
    conn = _connect_real(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def real_transaction(real_db_path: Union[str, Path, None] = None):
    """Context manager wrapping BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection, for a
    caller (a future route) that needs more than one statement to commit together.
    """
    path = real_db_path if real_db_path is not None else DB_PATH
    conn = _connect_real(path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        # Narrowed from a bare catch-all covering every exception type (lane RL, AC-25,
        # docs/units/RT/reads/read_1.md NOTE 1): the only errors this block needs to roll back FOR
        # are ones sqlite3 itself can raise while a statement inside the `with` block runs (a
        # constraint violation, a lock timeout, a malformed statement). A caller's own exception
        # type (RealOrderRefused and the like) still propagates through unmatched, exactly as
        # before; the narrowing only removes the silent catch-all so a genuinely unrelated bug
        # inside a `with real_transaction():` block is never mistaken for a database error.
        except (sqlite3.Error, sqlite3.Warning):
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


# =============================================================================
# Schema DDL: four tables, all idempotent (CREATE TABLE IF NOT EXISTS), so init_real_db run twice
# on the same file changes nothing.
# =============================================================================

# E2, the kill switch (AC-K5, AC-K4). One row per account, created on first touch by
# ensure_kill_switch_row below, never by a migration back-filling a word onto an existing row
# (E2's own fresh answer). state is the five closed words, NOT NULL: a row that exists with an
# out-of-set or missing state is a corruption, never read as never-armed. commanded_action is
# nullable and closed at two words: NULL means no kill has ever been commanded, which is a meaning
# and never a gap. last_verified_at and last_verified_outcome carry the time and result of the
# last verification pass, so the surface can say WHEN the switch was last checked, not merely what
# it currently reads.
_DDL_REAL_KILL_SWITCH = """
CREATE TABLE IF NOT EXISTS real_kill_switch (
    account_id            TEXT    PRIMARY KEY,
    state                 TEXT    NOT NULL CHECK (state IN ('never-armed', 'disarmed-verified-clear', 'armed-unverified', 'armed-verified-effective', 'armed-verified-ineffective')),
    commanded_action       TEXT    CHECK (commanded_action IS NULL OR commanded_action IN ('halt', 'flatten')),
    last_verified_at        TEXT,
    last_verified_outcome     TEXT    CHECK (last_verified_outcome IS NULL OR last_verified_outcome IN ('asked-and-clear', 'asked-and-not-clear', 'could-not-ask'))
);
"""

# E8, the kill drill record (AC-K7). Append-only: no function in this module ever updates a row
# here. "never-drilled" is DERIVED (no row for the account at all), never a stored word, which is
# why this table carries no such value in its own closed outcome set.
_DDL_REAL_KILL_DRILLS = """
CREATE TABLE IF NOT EXISTS real_kill_drills (
    drill_id            INTEGER PRIMARY KEY,
    account_id           TEXT    NOT NULL,
    ran_at                 TEXT    NOT NULL,
    outcome                  TEXT    NOT NULL CHECK (outcome IN ('ran-and-passed', 'ran-and-failed')),
    cancel_exercised           INTEGER NOT NULL CHECK (cancel_exercised IN (0, 1)),
    close_exercised              INTEGER NOT NULL CHECK (close_exercised IN (0, 1))
);
"""

# E3, the enabling record (AC-G7, AC-G8, AC-G2). Nine fields beside the surrogate key. state is
# closed at exactly one word, granted, NOT NULL: there is no second state for an append-only
# record naming a go to carry. ledger_entry_reference, the two paper-period columns, drill_id and
# account_value_at_go are all nullable, because a record missing one of them is UNUSABLE rather
# than a schema violation (E3's own NULL answer): the gate reads its absence and refuses by name,
# it never rejects the write. NO INSERT, no fold-in-place UPDATE and no DELETE against this table
# appears anywhere else in this module: the strengthened absence proof this lane's probes check
# for is that the table exists, the gate reads it, and nothing here can author a go.
_DDL_REAL_ENABLING_RECORDS = """
CREATE TABLE IF NOT EXISTS real_enabling_records (
    enabling_record_id       INTEGER PRIMARY KEY,
    account_id                 TEXT    NOT NULL,
    state                        TEXT    NOT NULL CHECK (state = 'granted'),
    ledger_entry_reference          TEXT,
    recorded_at                       TEXT    NOT NULL,
    bound_params_json                  TEXT    NOT NULL,
    paper_period_start                   TEXT,
    paper_period_end                       TEXT,
    drill_id                                 INTEGER REFERENCES real_kill_drills(drill_id),
    account_value_at_go                        REAL
);
"""

# W-28, the configured-values store. init_real_db seeds the nine keys below with value NULL if
# absent (AC-P1: no default anywhere, so a fresh row and an unconfigured row read identically).
# No function in this module's own build sets a value: section 17 puts "setting any of section
# 16's nine values" out of this lane's scope, so every probe that needs a configured value writes
# it with its own raw UPDATE, never through a setter this module offers.
_DDL_REAL_TRADING_PARAMETERS = """
CREATE TABLE IF NOT EXISTS real_trading_parameters (
    key       TEXT    PRIMARY KEY,
    value     REAL
);
"""

# Lane RL (night two), four new tables. DDIA justification shared across all four rather than
# repeated at each one (DECISIONS DDIA workflow rule): Kleppmann, Data-Intensive Applications,
# chapter 3 ("Data Models and Query Languages"), the event sourcing and CQRS section, quoted
# verbatim from docs/books/kleppmann_canon.md: "Read-shape ≠ write-shape. Event log = append-only
# sequence of self-contained event records (typically JSON, with timestamps). Events are immutable;
# new events supersede old ones." and "CQRS = command query responsibility segregation: commands
# validate and append events; queries read materialized views derived from the log." In plain
# words: rather than keeping one mutable "current position" or "current account value" number that
# a bug could silently overwrite, every reading below is appended once, as its own row, and "the
# current one" is a VIEW a check derives at read time by asking for the newest row, exactly like
# E7's existing counters in this same file ("derived at read time from stored state, never a
# running total kept in a module-level dictionary"). This is why real_position_readings,
# real_loss_window_baselines and real_loss_monitor_runs are all append-only (no function in this
# lane's own build ever issues an UPDATE or DELETE against them): each row is one immutable fact
# (a reconciliation reading, a window's starting value, one monitor pass), and a check that wants
# "the current" one reads the newest row for its own key, never a value some other write silently
# changed underneath it. real_orders is the one exception (an intent row is inserted once and then
# updated exactly once, matched by its own idempotency key), justified separately in
# real_orders.py's own module docstring by chapter 8's transaction quote, not by this chapter 3
# quote.
#
# E6, the venue-side position reading (AC-L3). See the DDL comment inline for the CHECK
# constraints' own meaning; test-fixture-only writer tonight (insert_position_reading in the probe
# suite's own _lib.py), night three's reconciliation sweep becomes the real writer without
# reshaping this table (lead's read of 66ff6f63, finding 3).
_DDL_REAL_POSITION_READINGS = """
CREATE TABLE IF NOT EXISTS real_position_readings (
    reading_id     INTEGER PRIMARY KEY,
    account_id     TEXT    NOT NULL,
    scope          TEXT    NOT NULL CHECK (scope IN ('ticker', 'account')),
    ticker         TEXT    CHECK ((scope = 'account') = (ticker IS NULL)),
    net_notional   REAL,
    absent_reason  TEXT    CHECK ((net_notional IS NULL) != (absent_reason IS NULL)),
    recorded_at    TEXT    NOT NULL
);
"""

# E9, the loss-limit window baseline (AC-L10). One row per window boundary actually rolled into;
# "never-rolled" is derived by an absent row, never a stored word, matching this file's own E7 rule.
_DDL_REAL_LOSS_WINDOW_BASELINES = """
CREATE TABLE IF NOT EXISTS real_loss_window_baselines (
    baseline_id    INTEGER PRIMARY KEY,
    account_id     TEXT    NOT NULL,
    window_start   TEXT    NOT NULL,
    account_value  REAL    NOT NULL,
    read_at        TEXT    NOT NULL
);
"""

# E10, one row per PASS-7 evaluation, breach or not, completed or not (AC-L11, AC-L12, AC-L13).
# "never-run" is derived by an absent row; a failed run (completed=0) is never read as the gate's
# own account-value input, which is why account_value is nullable and NULL exactly when
# completed=0.
_DDL_REAL_LOSS_MONITOR_RUNS = """
CREATE TABLE IF NOT EXISTS real_loss_monitor_runs (
    run_id           INTEGER PRIMARY KEY,
    account_id       TEXT    NOT NULL,
    ran_at           TEXT    NOT NULL,
    completed        INTEGER NOT NULL CHECK (completed IN (0, 1)),
    account_value    REAL,
    window_drop      REAL,
    since_go_drop    REAL,
    armed            INTEGER NOT NULL CHECK (armed IN (0, 1))
);
"""

# Lane RT3 (night three), E6's own pass record (AC-R6, AC-R8, AC-R9): one row per reconciliation
# attempt, append-only, additive to the store. outcome is a closed set enforced on the SCHEMA itself
# (AC-9): 'clear' and 'disagreement' are COMPLETED passes (a real answer was reached);
# 'could-not-reach-venue' is a failed ATTEMPT, never returned by latest_reconciliation_pass, and does
# not move the successful-reconciliation age (E6's own three-state table). disagreement_detail_json
# is NULL for every outcome except 'disagreement', where it names the one scope that disagreed and
# both figures, in the shape real_reconciliation.py's own module docstring quotes.
_DDL_REAL_RECONCILIATION_RUNS = """
CREATE TABLE IF NOT EXISTS real_reconciliation_runs (
    run_id                     INTEGER PRIMARY KEY,
    account_id                 TEXT    NOT NULL,
    ran_at                     TEXT    NOT NULL,
    outcome                    TEXT    NOT NULL CHECK (outcome IN ('clear', 'disagreement', 'could-not-reach-venue')),
    malformed_count            INTEGER NOT NULL DEFAULT 0,
    disagreement_detail_json   TEXT    CHECK (outcome != 'could-not-reach-venue' OR disagreement_detail_json IS NULL)
);
"""

# W-25's own home for a failed loss-limit-window roll (fold S1, AC-24): one row per FAILED attempt,
# additive, append-only. window_start is NULL for the tick-level shape (real_limits.run_window_roll
# called with no window boundary to key by, because the store's own real_trading.loss_limit_window
# carries no window length) and an exact ISO string for the low-level shape driven directly with one.
# "never failed for this window" is derived by an absent row, matching this file's own E7 rule; a
# failed roll never writes a real_loss_window_baselines row (E9 stays exactly what it was), so this
# table is the ONLY record that a roll was attempted and did not complete.
_DDL_REAL_LOSS_WINDOW_ROLL_FAILURES = """
CREATE TABLE IF NOT EXISTS real_loss_window_roll_failures (
    failure_id     INTEGER PRIMARY KEY,
    account_id     TEXT    NOT NULL,
    window_start   TEXT,
    reason         TEXT    NOT NULL,
    recorded_at    TEXT    NOT NULL
);
"""

# E4/E5/W-17 combined onto one row (AC-O2 to AC-O6, AC-I1 to AC-I5, AC-L6). A row's shape is told
# apart by exactly two columns, refusal_reason and terminal_state (AC-O3): REFUSED (refusal_reason
# set), OPEN INTENT (both NULL), TERMINAL (terminal_state set, closed at exactly four words). The
# four breach_* columns are written only on a row refused by one of the six named hard limits
# (AC-L6); every other refusal, phase-A included, leaves all four NULL rather than a fabricated
# zero, because a phase-A check measured no figure to compare against any limit at all.
_DDL_REAL_ORDERS = """
CREATE TABLE IF NOT EXISTS real_orders (
    order_id                          INTEGER PRIMARY KEY,
    account_id                        TEXT    NOT NULL,
    paper_run_id                      INTEGER,
    symbol                            TEXT    NOT NULL,
    side                              TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    intended_quantity                 REAL    NOT NULL,
    intended_notional                 REAL    NOT NULL,
    submitted_quantity                REAL,
    filled_quantity                   REAL,
    client_order_id                   TEXT    NOT NULL UNIQUE,
    venue_order_id                    TEXT,
    refusal_reason                    TEXT,
    terminal_state                    TEXT    CHECK (terminal_state IS NULL OR terminal_state IN
                                          ('filled', 'cancelled', 'rejected', 'never-received')),
    submitting_call_site              TEXT    NOT NULL,
    recorded_at                       TEXT    NOT NULL,
    submitted_at                      TEXT,
    resolved_at                       TEXT,
    breach_configured_value           REAL,
    breach_would_be_figure            REAL,
    breach_reconciliation_reading     REAL,
    breach_reconciliation_reading_at  TEXT,
    venue_accepted                    INTEGER CHECK (venue_accepted IS NULL OR venue_accepted IN (0, 1)),
    fill_price                        REAL
);
"""
# W-14, lane RT3 (night three): the two columns above are ALSO added through _add_column_if_missing
# below, for the case where real.db already exists from a prior night's deploy and this CREATE TABLE
# IF NOT EXISTS therefore never runs against it. venue_accepted is the venue's own acknowledgement
# (step 3 of submit_real_order already reads 'accepted' off the adapter's response but, before this
# lane, never stored it); fill_price is the venue's own reported fill price. Both are NULL for every
# row written before this lane's own build (E-idiom: additive, no back-fill, no default).

# Lane RL round 1 fold (finding 6 / code-quality SF1): five indexes backing the account-scoped
# reads real_limits.py makes against real_orders and real_loss_monitor_runs, plus the natural
# lookup keys on real_position_readings and real_loss_window_baselines. DDIA chapter 4 ("Storage
# and Retrieval"), quoted verbatim from docs/books/kleppmann_canon.md: "Real DBs improve reads with
# indexes, paying for them with slower writes. Indexes are chosen, not automatic." (The finding
# that raised this named chapter 3; chapter 3 is where this module's own event-log/CQRS shape
# comes from, above, but the indexing quote itself lives in chapter 4, verified directly against
# the canon before writing this comment, and cited correctly here rather than repeating the wrong
# number.) In plain words: real_orders and real_loss_monitor_runs both grow without bound (one row
# per order attempt; one row per account per monitor tick, forever), and every one of the reads
# below runs on every single order submission or monitor tick, filtered by account_id; without an
# index SQLite's only plan is a full table scan, which gets slower every day this build runs.
# Additive only (CREATE INDEX IF NOT EXISTS): no existing row moves, no existing query's result
# changes, only the plan SQLite picks to answer it.
_INDEXES = (
    # real_limits.order_rate_count: WHERE account_id = ? AND recorded_at >= ? AND recorded_at <= ?
    "CREATE INDEX IF NOT EXISTS ix_real_orders_account_recorded ON "
    "real_orders(account_id, recorded_at);",
    # real_limits.open_intent_notional_for_run (the per-run slice fence): WHERE paper_run_id = ?
    "CREATE INDEX IF NOT EXISTS ix_real_orders_paper_run ON real_orders(paper_run_id);",
    # real_limits.latest_completed_monitor_run: WHERE account_id = ? AND completed = 1
    # ORDER BY run_id DESC LIMIT 1
    "CREATE INDEX IF NOT EXISTS ix_real_loss_monitor_runs_account_run ON "
    "real_loss_monitor_runs(account_id, run_id);",
    # real_limits.latest_position_reading: WHERE account_id = ? AND scope = ? AND ticker IS ?
    # ORDER BY reading_id DESC LIMIT 1
    "CREATE INDEX IF NOT EXISTS ix_real_position_readings_lookup ON "
    "real_position_readings(account_id, scope, ticker, reading_id);",
    # real_limits.latest_window_baseline: WHERE account_id = ? [AND window_start = ?]
    "CREATE INDEX IF NOT EXISTS ix_real_loss_window_baselines_account_window ON "
    "real_loss_window_baselines(account_id, window_start);",
)

_DDL_STATEMENTS_ORDERED = (
    _DDL_REAL_KILL_SWITCH,
    _DDL_REAL_KILL_DRILLS,
    _DDL_REAL_ENABLING_RECORDS,
    _DDL_REAL_TRADING_PARAMETERS,
    _DDL_REAL_POSITION_READINGS,
    _DDL_REAL_LOSS_WINDOW_BASELINES,
    _DDL_REAL_LOSS_MONITOR_RUNS,
    _DDL_REAL_ORDERS,
    _DDL_REAL_RECONCILIATION_RUNS,
    _DDL_REAL_LOSS_WINDOW_ROLL_FAILURES,
) + _INDEXES + (
    # Lane RT3: a lookup index for real_limits.latest_roll_failure's own (account_id, window_start)
    # read, matching this file's own DDIA chapter 4 justification above for the five earlier indexes.
    "CREATE INDEX IF NOT EXISTS ix_real_loss_window_roll_failures_account_window ON "
    "real_loss_window_roll_failures(account_id, window_start, failure_id);",
    # Round 5 fold, code-quality SF2: this file's own command_kill and real_limits.py's own
    # run_loss_monitor_pass both run "SELECT DISTINCT symbol FROM real_orders WHERE account_id = ?"
    # fresh, every kill and every 60-second tick, forever, against real_orders, a table this file's
    # own comment already calls unbounded above. With no (account_id, symbol) index SQLite can only
    # answer DISTINCT by visiting every row ever recorded for the account, so the cost grows with
    # the account's all-time order count rather than its currently-traded set. DDIA chapter 4
    # ("Storage and Retrieval"), the SAME verbatim sentence already quoted above for the five
    # earlier indexes (verified at docs/books/kleppmann_canon.md line 901): "Real DBs improve reads
    # with indexes, paying for them with slower writes. Indexes are chosen, not automatic." This one
    # is additive only, like every index in this tuple: no existing row moves, no existing query's
    # result changes, only the plan SQLite picks to answer it. It also backs
    # real_reconciliation.py's own _our_own_figures_by_symbol GROUP BY query (round 5 fold, finding
    # 2), the same (account_id, symbol) access pattern from a second caller. The full-history scan
    # itself is unchanged (the ticker set is still derived from history by design tonight, per the
    # sweep's own plan-section-8 mandate); this index is what bounds its cost, not what removes it.
    "CREATE INDEX IF NOT EXISTS ix_real_orders_account_symbol ON real_orders(account_id, symbol);",
)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                            column_def: str) -> None:
    """Add one column via ALTER TABLE, a no-op when it already exists. Matches
    researchdb.py's own helper of the same name and shape (checked here rather than imported,
    because this module and researchdb.py deliberately share no import edge): SQLite's own ALTER
    TABLE ADD COLUMN errors if the column is already there, so PRAGMA table_info is read first.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    if column not in existing:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, column_def))


def init_real_db(real_db_path: Union[str, Path, None] = None) -> None:
    """Create the real-trading store's four tables if the file does not already hold them, and
    seed the nine section-16 parameter keys with a NULL value if their rows are absent.

    Idempotent: every CREATE statement is IF NOT EXISTS and every seed row is INSERT OR IGNORE, so
    running this twice against the same file changes neither the schema nor any row (E1's fresh
    answer: a machine that has never run this reaches the exact same state a machine that has run
    it twice reaches). Mirrors paperdb.init_paper_db()'s own shape. An absent file is created by
    sqlite3.connect itself, never read as corruption; a path that cannot be opened as a database
    (for instance a directory) raises loudly here, on the first statement, rather than being
    silently recreated over, because recreating a money store over an unreadable one destroys the
    only record of what was traded.
    """
    path = real_db_path if real_db_path is not None else DB_PATH
    conn = _connect_real(path)
    try:
        for statement in _DDL_STATEMENTS_ORDERED:
            conn.execute(statement)
        # W-14, additive: a real.db created before this lane's own build has real_orders without
        # these two columns; the CREATE TABLE IF NOT EXISTS above never adds a column to a table
        # that already exists, so the migration runs here, every time, and is a no-op once applied.
        _add_column_if_missing(conn, "real_orders", "venue_accepted",
                                "INTEGER CHECK (venue_accepted IS NULL OR venue_accepted IN (0, 1))")
        _add_column_if_missing(conn, "real_orders", "fill_price", "REAL")
        # DBG-6b: which adapter each half of a drill ran against, and what each half found. A row
        # written before this lane reads 'unrecorded' in all four.
        for drill_column in ("stub_half_adapter", "stub_half_result",
                             "real_half_adapter", "real_half_result"):
            _add_column_if_missing(conn, "real_kill_drills", drill_column,
                                    "TEXT NOT NULL DEFAULT 'unrecorded'")
        for key in REAL_TRADING_PARAMETER_KEYS:
            conn.execute(
                "INSERT OR IGNORE INTO real_trading_parameters (key, value) VALUES (?, NULL)",
                (key,),
            )
    finally:
        conn.close()


# =============================================================================
# E2, the kill switch. AC-K1 (route half lives in routes/real.py), AC-K2 to AC-K10.
# =============================================================================

def ensure_kill_switch_row(conn: sqlite3.Connection, account_id: str) -> None:
    """Idempotent INSERT OR IGNORE of a fresh row for account_id: state='never-armed', everything
    else NULL. Every function below that reads or writes a switch row calls this first (or is
    called after something that already did), so "no account configured" and "never armed" read
    as the same fresh state rather than one of them crashing on a missing row.
    """
    conn.execute(
        "INSERT OR IGNORE INTO real_kill_switch (account_id, state) VALUES (?, 'never-armed')",
        (account_id,),
    )


def get_kill_state(conn: sqlite3.Connection, account_id: str) -> Optional[sqlite3.Row]:
    """The account's own switch row, or None when ensure_kill_switch_row has never been called for
    it (the no-account-configured case, E2's own empty-set answer). Always read from THIS
    connection, never cached, so a caller that closed and reopened its connection (AC-K3's own
    restart simulation) sees exactly what is durably stored.
    """
    return conn.execute(
        "SELECT account_id, state, commanded_action, last_verified_at, last_verified_outcome "
        "FROM real_kill_switch WHERE account_id = ?",
        (account_id,),
    ).fetchone()


def kill_check_reason(kill_row) -> Optional[str]:
    """A pure function over one switch row (or None): section 12.1's own table, restricted to the
    kill-state check. Returns None only when the row's state is disarmed-verified-clear (AC-K6);
    otherwise the matching word from AC-K5's own five, so a caller can display or count exactly
    which of the four refusing states it is; and 'kill-state-unreadable' when the row is missing
    entirely or its state is not one of the five closed words at all (E2's NULL answer, AC-A3's
    fail-closed rule): a corrupt or absent row is never read as never-armed, which would be a
    false statement about the most safety-critical control here.
    """
    # Renamed (lane RL, AC-25, docs/units/RT/reads/read_1.md NOTE 3) from a prior name that quoted
    # a wrong count: the tuple always held four words, so the name now matches its real arity.
    closed_four = (
        "never-armed", "armed-unverified", "armed-verified-effective", "armed-verified-ineffective",
    )
    if kill_row is None:
        return "kill-state-unreadable"
    state = kill_row["state"]
    if state == "disarmed-verified-clear":
        return None
    if state in closed_four:
        return state
    return "kill-state-unreadable"


def command_kill(conn: sqlite3.Connection, account_id: str, action: str, adapter, now_iso: str) -> str:
    """Command HALT or FLATTEN (AC-K2): one account-scoped cancel, and for flatten only, one
    account-scoped close, both taking no argument beyond the adapter itself (AC-K9: no order id,
    no run id). Writes state='armed-unverified' and commanded_action=action, whatever the prior
    state was (AC-K10 column 1: a kill re-issued from an already-armed state is not a no-op).
    Returns the new state.

    The order is check, ensure, write, then call. Check: the connection must be autocommit and
    outside a transaction (KillCommandInTransaction otherwise, nothing written or called), because
    the armed write has to be committed before the venue is reached. Ensure: the account's switch
    row is created if missing, because the UPDATE of a missing row would match nothing and arm
    nothing, silently. Write: the armed state is stored first, so a venue call that raises or hangs
    never leaves the switch looking unarmed; a write that did not match exactly one row raises
    KillWriteFailed. Call: only then does the venue hear the cancel (and the close, for flatten);
    an exception from the venue propagates unchanged and the row stays armed-unverified.
    """
    if conn.isolation_level is not None or conn.in_transaction:
        raise KillCommandInTransaction(
            "command_kill needs an autocommit connection outside a transaction, so the armed "
            "state is committed before the venue is called"
        )
    ensure_kill_switch_row(conn, account_id)
    cursor = conn.execute(
        "UPDATE real_kill_switch SET state = 'armed-unverified', commanded_action = ? "
        "WHERE account_id = ?",
        (action, account_id),
    )
    if cursor.rowcount != 1:
        raise KillWriteFailed(
            "the armed state matched %d rows for account '%s', expected exactly 1"
            % (cursor.rowcount, account_id)
        )
    adapter.cancel_all_orders()
    if action == "flatten":
        adapter.close_all_positions()
    # Night three (lane RT3), AC-R1's own "after every kill action" trigger: the reconciliation
    # sweep fires for this SAME account, over every ticker this account has ever placed a real
    # order for, the same instant a kill is issued, never waiting for the next scheduled tick. A
    # lazy, function-body import, so this module carries no top-of-file dependency on the
    # higher-level real_reconciliation module. The sweep's own outcome (clear, disagreement, or
    # could-not-reach-venue) is not this function's business: run_reconciliation_sweep already
    # handles an unreachable venue internally and never raises for it, so a kill command's own
    # success is never masked by a reconciliation pass that could not complete.
    from . import real_reconciliation  # noqa: PLC0415
    known_tickers = [row["symbol"] for row in conn.execute(
        "SELECT DISTINCT symbol FROM real_orders WHERE account_id = ?", (account_id,)
    ).fetchall()]
    real_reconciliation.run_reconciliation_sweep(conn, account_id, adapter, known_tickers, now_iso)
    return "armed-unverified"


def run_kill_verification(conn: sqlite3.Connection, account_id: str, adapter, now_iso: str) -> str:
    """Run one verification pass (PASS-3, AC-K4, AC-K10). Raises VerificationNotApplicable when the
    switch is currently disarmed-verified-clear, before the adapter is ever asked (there is no
    kill in effect to verify). Otherwise asks the adapter's own account-wide report; a pass that
    could not ask (AdapterCannotDetermine) leaves the state unchanged and records only that the
    attempt was made. A pass that asked compares the account against the expectation the
    commanded_action names: no working order for halt or for a never-commanded switch; no working
    order AND no open position for flatten. Clear moves a never-armed switch to
    disarmed-verified-clear and any other state to armed-verified-effective; not clear leaves
    never-armed unchanged and moves any other state to armed-verified-ineffective. Returns the
    resulting state.
    """
    row = get_kill_state(conn, account_id)
    current_state = row["state"]
    commanded_action = row["commanded_action"]

    if current_state == "disarmed-verified-clear":
        raise VerificationNotApplicable(current_state)

    try:
        report = adapter.report_account_state()
    except AdapterCannotDetermine:
        conn.execute(
            "UPDATE real_kill_switch SET last_verified_at = ?, "
            "last_verified_outcome = 'could-not-ask' WHERE account_id = ?",
            (now_iso, account_id),
        )
        return current_state

    working_orders = report["working_orders"]
    positions = report["positions"]
    if commanded_action == "flatten":
        is_clear = (len(working_orders) == 0) and (len(positions) == 0)
    else:
        # halt, or no kill ever commanded: the expectation never includes positions (AC-K2's own
        # table: halt does nothing to open positions, and a never-armed switch claims nothing
        # about a position this path never opened).
        is_clear = len(working_orders) == 0

    if is_clear:
        new_state = "disarmed-verified-clear" if current_state == "never-armed" else "armed-verified-effective"
        outcome = "asked-and-clear"
    else:
        new_state = "never-armed" if current_state == "never-armed" else "armed-verified-ineffective"
        outcome = "asked-and-not-clear"

    conn.execute(
        "UPDATE real_kill_switch SET state = ?, last_verified_at = ?, last_verified_outcome = ? "
        "WHERE account_id = ?",
        (new_state, now_iso, outcome, account_id),
    )
    return new_state


def disarm_kill(conn: sqlite3.Connection, account_id: str) -> str:
    """Disarm the switch (AC-K10 column 5). Permitted only from armed-verified-effective; every
    other state, including a missing row (read as never-armed for this purpose, the safe
    direction), raises KillDisarmRefused carrying the refusing state by name. Never touches
    commanded_action or the last-verification fields: only the state word moves.
    """
    row = get_kill_state(conn, account_id)
    current_state = row["state"] if row is not None else "never-armed"
    if current_state != "armed-verified-effective":
        raise KillDisarmRefused(current_state)
    conn.execute(
        "UPDATE real_kill_switch SET state = 'disarmed-verified-clear' WHERE account_id = ?",
        (account_id,),
    )
    return "disarmed-verified-clear"


# =============================================================================
# E8, the kill drill record. AC-K7.
# =============================================================================

def get_kill_drill_state(conn: sqlite3.Connection, account_id: str):
    """The newest drill row for account_id, or the literal string 'never-drilled' when none
    exists. E7's own rule: a never-acted-on entity reads a named word, never a number and never a
    stored default.
    """
    row = conn.execute(
        "SELECT drill_id, account_id, ran_at, outcome, cancel_exercised, close_exercised, "
        "stub_half_adapter, stub_half_result, real_half_adapter, real_half_result "
        "FROM real_kill_drills WHERE account_id = ? ORDER BY drill_id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if row is None:
        return "never-drilled"
    return row


def run_kill_drill(conn: sqlite3.Connection, account_id: str, stub_adapter, real_adapter,
                   now_iso: str) -> dict:
    """Run one kill drill (E8, AC-K7): really arm a drill switch, run the verification pass, and
    record which adapter each half ran against.

    Refusals come first, before anything is read or written. real_adapter must be None: no check
    that an adapter is in a non-trading configuration exists yet, so the real half never runs
    (KillDrillAdapterRefused otherwise). stub_adapter must be a RecordingAdapterDouble or a
    subclass of it (KillDrillAdapterRefused otherwise, naming its class). The connection must be
    autocommit and outside a transaction (KillCommandInTransaction otherwise, nothing written).

    The drill uses its own switch row, account_id + ':drill', so the account's own switch, orders,
    reconciliation runs and readings are never touched: the row is deleted, then command_kill
    ('flatten') arms it and sweeps under that id, then run_kill_verification asks the stub. The
    stub half reads "effective", "ineffective" or "unverified" (still armed-unverified). A drill
    switch left armed-verified-effective is disarmed; any other armed state stays. Because the real
    half never runs, the outcome is always 'ran-and-failed' (E8: ran-and-passed needs both halves
    effective). The pre-drill read that sets the exercised flags treats AdapterCannotDetermine as
    "nothing observed". An error from command_kill or the stub propagates and no drill row is
    written. The row carries the ACCOUNT's id, because the readers of drills look it up by account.
    """
    if real_adapter is not None:
        raise KillDrillAdapterRefused(
            "the kill drill refuses a real adapter: no check that an adapter is in a "
            "non-trading configuration exists yet"
        )
    if not isinstance(stub_adapter, RecordingAdapterDouble):
        stub_class = type(stub_adapter)
        raise KillDrillAdapterRefused(
            "the kill drill accepts only a RecordingAdapterDouble as its stub adapter, got %s.%s"
            % (stub_class.__module__, stub_class.__qualname__)
        )
    if conn.isolation_level is not None or conn.in_transaction:
        raise KillCommandInTransaction(
            "run_kill_drill needs an autocommit connection outside a transaction, so the armed "
            "state is committed before the adapter is called"
        )

    drill_account_id = account_id + ":drill"
    conn.execute("DELETE FROM real_kill_switch WHERE account_id = ?", (drill_account_id,))

    try:
        pre = stub_adapter.report_account_state()
        cancel_exercised = len(pre["working_orders"]) > 0
        close_exercised = len(pre["positions"]) > 0
    except AdapterCannotDetermine:
        cancel_exercised = False
        close_exercised = False

    command_kill(conn, drill_account_id, "flatten", stub_adapter, now_iso)
    verified_state = run_kill_verification(conn, drill_account_id, stub_adapter, now_iso)
    if verified_state == "armed-verified-effective":
        stub_half_result = "effective"
    elif verified_state == "armed-verified-ineffective":
        stub_half_result = "ineffective"
    else:
        stub_half_result = "unverified"
    real_half_result = "not-run-no-real-adapter"

    outcome = "ran-and-passed" if (
        stub_half_result == "effective" and real_half_result == "effective"
    ) else "ran-and-failed"
    stub_class = type(stub_adapter)
    cur = conn.execute(
        "INSERT INTO real_kill_drills (account_id, ran_at, outcome, cancel_exercised, "
        "close_exercised, stub_half_adapter, stub_half_result, real_half_adapter, "
        "real_half_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, now_iso, outcome, int(cancel_exercised), int(close_exercised),
         "%s.%s" % (stub_class.__module__, stub_class.__qualname__), stub_half_result,
         "none-configured", real_half_result),
    )
    if verified_state == "armed-verified-effective":
        disarm_kill(conn, drill_account_id)
    return {
        "drill_id": cur.lastrowid,
        "outcome": outcome,
        "cancel_exercised": cancel_exercised,
        "close_exercised": close_exercised,
        "stub_half_result": stub_half_result,
        "real_half_result": real_half_result,
    }


# =============================================================================
# W-28, the configured-values store, and its own read-only guard.
# =============================================================================

def assert_parameters_configured(conn: sqlite3.Connection) -> None:
    """Raise RealTradingNotConfigured, naming every one of the nine section-16 keys that has no
    configured value (a NULL row, or no row at all), unless all nine are set. AC-P1: no parameter
    in this unit has a default anywhere, so a missing value must stop the gate rather than read as
    unbounded.
    """
    rows = conn.execute("SELECT key, value FROM real_trading_parameters").fetchall()
    configured = {row["key"]: row["value"] for row in rows}
    missing = [key for key in REAL_TRADING_PARAMETER_KEYS if configured.get(key) is None]
    if missing:
        raise RealTradingNotConfigured(missing)


class RealLimitRefused(ValueError):
    """A set of limit values the store refuses. `code` is one of "real-limit-empty",
    "real-limit-unknown-key" or "real-limit-value-invalid"; `key` names the offending key when
    there is one."""

    def __init__(self, code: str, key: Optional[str] = None):
        super().__init__(code if key is None else "%s: %s" % (code, key))
        self.code = code
        self.key = key


def set_trading_parameters(conn: sqlite3.Connection, values: dict) -> None:
    """Store a subset of the nine section-16 values (DECISIONS #478 item 2). Every key must be one
    of REAL_TRADING_PARAMETER_KEYS in its full string form; every value must be a JSON number
    (never a bool, a string or null), finite and above 0; order_rate_cap must be a whole number.
    Everything is checked before anything is written, then all rows are written in one transaction:
    all or nothing. Values are stored as REAL. There is no default: a key left out of `values` is
    left exactly as it was.

    If the caller already holds a transaction on `conn` the writes join it (so the caller can read
    the old values, write, and derive the new answer atomically); otherwise this opens its own.
    This function never touches real_enabling_records: a changed value makes derive_enabled read
    'enabling-record-superseded' by itself (AC-G3, AC-L8).
    """
    if not isinstance(values, dict) or not values:
        raise RealLimitRefused("real-limit-empty")
    clean = {}
    for key, value in values.items():
        if key not in REAL_TRADING_PARAMETER_KEYS:
            raise RealLimitRefused("real-limit-unknown-key", key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RealLimitRefused("real-limit-value-invalid", key)
        try:
            number = float(value)
        except OverflowError:
            raise RealLimitRefused("real-limit-value-invalid", key)
        if not math.isfinite(number) or not number > 0:
            raise RealLimitRefused("real-limit-value-invalid", key)
        if key == "real_trading.order_rate_cap" and number != int(number):
            raise RealLimitRefused("real-limit-value-invalid", key)
        clean[key] = number

    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE;")
    try:
        for key, number in clean.items():
            conn.execute(
                "INSERT INTO real_trading_parameters (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, number),
            )
        if own_transaction:
            conn.execute("COMMIT;")
    except (sqlite3.Error, sqlite3.Warning):
        if own_transaction and conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise


def _iso_seconds_between(later_iso: str, earlier_iso: str) -> float:
    """Seconds between two of this module's own timestamp strings ("%Y-%m-%dT%H:%M:%SZ"), later
    minus earlier. Both sides use the same fixed format, so a naive parse (no timezone object
    attached) compares correctly without needing one.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    later = datetime.strptime(later_iso, fmt)
    earlier = datetime.strptime(earlier_iso, fmt)
    return (later - earlier).total_seconds()


# =============================================================================
# E3, the enabling record: read-only from this module's own side (AC-G2, AC-G6). No function
# below, and no function anywhere else in this file, ever inserts, updates or deletes a row in
# real_enabling_records.
# =============================================================================

def derive_enabled(conn: sqlite3.Connection, account_id: str, now_iso: str):
    """Derive whether account_id is enabled to trade for real, and if not, the one reason why
    (AC-G9's five tests, tried in this fixed order so the FIRST failure is the reason recorded):

      1. no record at all -> ('enabling-record-absent', E1's and E3's fresh answer).
      2. the newest record's ledger reference is NULL, its bound-parameter blob is missing any of
         the nine section-16 keys, or its drill reference names no real drill row at all ->
         'enabling-record-unusable' (nothing here can be read, so nothing here can authorise).
      3. any of the nine configured values now disagrees with what was bound at the go ->
         'enabling-record-superseded' (AC-G3, AC-L8: a parameter value may not change while the
         path stays enabled without invalidating the go that relied on the old one).
      4. the referenced drill's own outcome is not ran-and-passed -> 'kill-drill-not-passed'.
      5. the referenced drill is older than the configured real_trading.kill_drill_max_age ->
         'kill-drill-stale'.

    Returns (True, None) only when every test above passes. This function reads exactly one
    SELECT against real_enabling_records, ever: the newest row for the account, by id.
    """
    row = conn.execute(
        "SELECT enabling_record_id, account_id, state, ledger_entry_reference, recorded_at, "
        "bound_params_json, paper_period_start, paper_period_end, drill_id, account_value_at_go "
        "FROM real_enabling_records WHERE account_id = ? ORDER BY enabling_record_id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    if row is None:
        return False, "enabling-record-absent"

    if row["ledger_entry_reference"] is None:
        return False, "enabling-record-unusable"

    bound_params = json.loads(row["bound_params_json"])
    if not all(key in bound_params for key in REAL_TRADING_PARAMETER_KEYS):
        return False, "enabling-record-unusable"

    if row["drill_id"] is None:
        return False, "enabling-record-unusable"

    drill_row = conn.execute(
        "SELECT ran_at, outcome FROM real_kill_drills WHERE drill_id = ?",
        (row["drill_id"],),
    ).fetchone()
    if drill_row is None:
        return False, "enabling-record-unusable"

    configured_rows = conn.execute("SELECT key, value FROM real_trading_parameters").fetchall()
    configured = {r["key"]: r["value"] for r in configured_rows}
    for key in REAL_TRADING_PARAMETER_KEYS:
        if configured.get(key) != bound_params.get(key):
            return False, "enabling-record-superseded"

    if drill_row["outcome"] != "ran-and-passed":
        return False, "kill-drill-not-passed"

    max_age_key = "real_trading.kill_drill_max_age"
    age_seconds = _iso_seconds_between(now_iso, drill_row["ran_at"])
    if age_seconds > configured[max_age_key]:
        return False, "kill-drill-stale"

    return True, None


# =============================================================================
# Night three (lane RT3): night one's own two-check order-gate helper is DELETED here (sheet item 5
# of #394/#395, lane-list row 5.1c's own stop condition, AC-21). It skipped every phase-A check but
# its own two (the enabling record and the kill state) and had no production caller;
# real_orders.submit_real_order is now the ONLY path to adapter.submit_order anywhere in this
# repository, carrying those same two checks PLUS this lane's own two new ones (AC-15). Deleting it
# now, before any caller is wired, means there is never a second path to a venue call for a future
# lane to remember to close.
# =============================================================================
# The recording adapter double (AC-A7's shape, the capabilities this lane's build calls). No live
# broker connection exists in this repository; every probe and every route in this lane drives
# this double or one it constructs the same way.
# =============================================================================

class RecordingAdapterDouble:
    """A double standing in for the execution adapter, recording every call it receives by name,
    in order, and nothing else. clears_on_cancel and clears_on_close model whether the double
    behaves like a well-behaved venue (the default, True) or one that accepts a command yet leaves
    the account unchanged (AC-K8's own case: a venue that ACCEPTS the cancel yet still holds the
    order, which only a verification pass, never the command's own return, can catch).
    """

    def __init__(self, working_orders=(), positions=(), cannot_determine=False,
                 clears_on_cancel=True, clears_on_close=True, lookup_responses=None,
                 cannot_determine_lookup=False, connection_state="connected",
                 connection_state_raises=None):
        self._working_orders = list(working_orders)
        self._positions = list(positions)
        self.cannot_determine = cannot_determine
        self.clears_on_cancel = clears_on_cancel
        self.clears_on_close = clears_on_close
        self.calls = []
        # Night three (lane RT3), capability 2 (AC-A7, AC-A8): lookup_order's own configured
        # answers, keyed by client_order_id, and the separate "I do not know" flag capability 8
        # names, told apart from every one of lookup_responses' own four ordinary shapes.
        self._lookup_responses = dict(lookup_responses or {})
        self.cannot_determine_lookup = cannot_determine_lookup
        # Night three, capability 9 (AC-A7 row 9, AC-A8): connection_state's own configured word,
        # or the exception it raises instead of answering when connection_state_raises is given.
        self._connection_state = connection_state
        self._connection_state_raises = connection_state_raises

    def cancel_all_orders(self):
        """Account-scoped, no argument at all (AC-K9: no order id, no run id passed)."""
        self.calls.append("cancel_all_orders")
        if self.clears_on_cancel:
            self._working_orders = []

    def close_all_positions(self):
        """Account-scoped, no argument at all (AC-K9)."""
        self.calls.append("close_all_positions")
        if self.clears_on_close:
            self._positions = []

    def report_account_state(self):
        """The account's own working orders and open positions, asked directly rather than looked
        up by any id this path holds. Raises AdapterCannotDetermine when this double is configured
        as unable to ask at all.
        """
        self.calls.append("report_account_state")
        if self.cannot_determine:
            raise AdapterCannotDetermine("the adapter could not determine the account's state")
        return {
            "working_orders": list(self._working_orders),
            "positions": list(self._positions),
        }

    def submit_order(self, order):
        """The one capability night one's own order-gate helper used to call, kept for probes that
        still construct an order object this way (that helper itself is deleted, AC-21). Returns the
        order object unchanged, under a key alongside it, so a caller can prove the gate passed the
        same object through.
        """
        self.calls.append("submit_order")
        return {"accepted": True, "order": order}

    def lookup_order(self, client_order_id):
        """Night three, capability 2 (AC-A7, AC-R2): raises AdapterCannotDetermine when this double
        is configured cannot_determine_lookup=True (capability 8's own "I do not know" answer);
        otherwise returns the configured response for client_order_id UNCHANGED, or
        {'status': 'unknown'} when this id was never registered with the double at all (AC-R2's own
        fourth shape: "including for an order this process never saw acknowledged").
        """
        self.calls.append("lookup_order")
        if self.cannot_determine_lookup:
            raise AdapterCannotDetermine(
                "the adapter could not determine this order's status"
            )
        return self._lookup_responses.get(client_order_id, {"status": "unknown"})

    def connection_state(self):
        """Night three, capability 9 (AC-A7 row 9, AC-A8): raises whatever exception this double was
        constructed with (connection_state_raises) when one was given, matching AC-F3's "a report
        that raises instead of answering is the same refusal" as a genuine three-word answer;
        otherwise returns the configured word, one of 'connected', 'not connected',
        'cannot determine'.
        """
        self.calls.append("connection_state")
        if self._connection_state_raises is not None:
            raise self._connection_state_raises
        return self._connection_state
