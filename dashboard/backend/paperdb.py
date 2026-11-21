"""paper.db access: the store behind paper trading, kept out of the research data path on purpose.

Lane P, night six (docs/handoffs/2026-09-03_night_six/10_job_paper_backend.md), building the
storage and business-logic half of docs/briefs/paper_trading_spec_brief.md (the page and its
rendering are a later, content-blocked lane; see that job file's "Scope" section). This module
owns a fifth SQLite file, separate from dashboard.db, research.db, events.db and ops.db, following
opsdb.py's own shape exactly (brief section 10.1): its own connection helper, its own transaction
context manager, its own idempotent init function, its own import-boundary guard.

Why a separate file (brief section 10.1, DDIA chapter 1: systems of record and derived data).
paper.db is the system of record for what happened in paper: fills, slices and equity snapshots.
research.db stays the system of record for strategies, runs and datasets. SQLite is single-writer
per database, and the paper node writes on every bar for every running strategy throughout the
trading day; putting that traffic inside research.db would put a continuous writer in front of
every research dispatch. A store that constrains order sizing also should not share a backup unit
or a write lock with research data. Nothing here carries a foreign key into research.db (the
backend area map's rule is categorical: "Nothing joins across the two research databases"), so a
paper_runs row denormalises the strategy id, name and version as they stood at admission, plus the
source run id, the same pattern research.db already uses for exactly this reason.

THIS IS A MONEY PATH. Every refusal below raises a specific, named exception, records the refusal
where section 10.3 says a refusal must be recorded, and never quietly resizes an order to fit
(brief section 12, "every assumption failure is a loud named stop"). A connection that forgot
"PRAGMA foreign_keys = ON" would turn every REFERENCES clause below into a comment and let an
orphan fill or equity point be written; _connect_paper is the ONLY function in this module that
opens a raw connection, so it is the only place that pragma needs to be set, and every other
function in this module reaches SQLite only through it (via get_paper_conn / paper_transaction).

Kill's five steps (brief section 6.4) are committed one at a time rather than as one big
transaction, on purpose. DDIA chapter 8 is blunt about what a local rollback can undo: "External
side effects (sending email) happen even on abort; 2PC can coordinate, or use idempotency."
Cancelling an order or closing a position at a venue is exactly such a side effect, so a rollback
here cannot un-cancel it there. Chapter 5 gives the shape this module borrows for the cheap half of
that idea: "Log every RPC and state change to a WAL so the workflow can resume exactly where it
left off." Two columns on paper_runs (kill_started_at, kill_step) are that WAL; press_kill commits
each completed step before attempting the next one's venue call, so a process that dies mid-Kill
leaves a durable, resumable record rather than a rollback that would silently un-happen a step the
venue already carried out. submit_paper_order's own venue call now gets the SAME idempotency
discipline, redesigned at night seven (DECISIONS #322) rather than merely committed early: an
INTENT row, carrying a client_order_id this module mints itself, is committed in its own short step
BEFORE the venue is ever called, so the id -- not a hopeful early commit of the fill -- is what
survives a crash and lets reconcile_open_intents ask the venue again, by that same id, whichever
side of the call the process died on. The fill is then written afterward by UPDATING that same row,
never a second INSERT, so paper_fills gains AT MOST one row per order attempt, never two -- an
attempt that never reaches an INSERT at all (intended_quantity or intended_notional itself
non-finite, AC-9a below, or the run or its slice state not found) leaves zero, loud rather than
silent. paper_fills is an append-only event log in the same spirit (DDIA chapter
3: "Event log = append-only sequence of self-contained event records... Events are immutable"), and
paper_slice_current / paper_equity are the derived, rebuildable views chapter 3's CQRS discussion
describes ("Materialized views are reproducible (delete, rebuild)").

The fixed percent per position (brief section 5.2, 13.1) has NO default anywhere in this module,
and no module-level constant or function-parameter default anywhere in this file names a sizing
percent, fraction or literal pot figure: this module has nothing to build for that surface, because
the sizing rule's own key does not exist until its provenance entry lands in
docs/research_lab/parameter_provenance.md, which is a different file this lane does not touch. See
this module's own absence of any such constant as the structural guarantee, not a promise elsewhere.
"""
from __future__ import annotations

import ast
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Union
from zoneinfo import ZoneInfo

from .config import PAPER_DB_PATH, PROJECT_ROOT

# Module-level default path; callers pass an explicit override when they need one (every probe
# does, matching config.py's own env-var pattern: PAPER_DB_PATH is overridable so a probe or a
# throwaway fixture never touches the real store).
DB_PATH = PAPER_DB_PATH

# Per-process counters for the ops routing law (job file "what can break", night seven), the same
# in-memory-only idiom research_lab/strategies/helpers/sizing.py's own SIZING_MONITOR_COUNTERS
# already established: no I/O, no print, no logging call, so incrementing a counter here can never
# add a byte to a probe's captured stdout and can never turn a PASS into a DRIFT against a
# committed .expected file. A future ops-wiring unit reads these from the running process and
# forwards them into docs/ops/ops_monitor_registry.md; this build reports the rows in its own done
# report rather than writing that file (DECISIONS #320: the row lands at merge, not from a lane).
PAPER_MONITOR_COUNTERS: dict = {
    # AC-9a: an order refused for a non-finite intended_quantity/intended_notional, a case
    # submit_paper_order cannot write a paper_fills row for at all (both columns are REAL NOT
    # NULL), so the raise alone would otherwise be the only trace this class of refusal ever left.
    "paper_unwritable_order_refusals": 0,
    # AC-9 "what can break" #3, counter half: an open intent reconcile_open_intents released
    # because the venue reported it was never received (lookup_order returned None). The gauge
    # half, reserved-but-unresolved capital per run, is open_intents_notional below, computed on
    # demand rather than stored, matching list_running_runs / list_ended_runs' own query shape.
    "paper_reconciliation_releases": 0,
}


# =============================================================================
# Exceptions. RuntimeError subclasses, one per named condition in brief section 12's table, plus
# the exceptions section 12 does not name: a missing parent directory before sqlite3 ever opens a
# file; a paper_runs row with no matching paper_slice_current row, which section 11's E3 calls a
# corruption rather than an empty state; PaperLongOnlyViolation, enforcing Tamer's ruled long-only
# policy on the sell side (gate findings B-AC-16 and B-AC-18, raised and verified by the lead
# against this module after section 12's table was written, so that table predates this rule);
# PaperNegativeNetQuantityInvariant, guarding _recompute_slice_state's own average-cost arithmetic
# against a net_quantity below zero, a state the long-only refusal above should make unreachable
# but which this module refuses to build a fence over rather than assume can never happen;
# PaperOrderInputInvalid, added night six round 4, refusing an order whose own intended_quantity,
# intended_notional or price_per_share is not finite and correctly signed, before any of the three
# reaches arithmetic that used to trust them unchecked; and PaperNumericInputInvalid, added night
# seven by the AC-0 root-cause dispatch, the same refusal for every OTHER public function in this
# module that takes a numeric magnitude as its own parameter: mark_position_price's new_price,
# admit_run's slice_opening_cash, and every parameter of capacity_ceiling and
# compute_available_buying_power. Round 4 fixed the one function a gate finding named
# (submit_paper_order); the same class recurred at round 5 on mark_position_price, a second function
# nothing had named yet, because each fix was a hand-written check for the one function in front of
# it rather than a rule applied to every function of the same shape. _is_finite_signed and
# _require_finite_signed, defined in the next section below, are now the ONE place "finite and
# correctly signed" is decided for this whole module; submit_paper_order's own inline check now
# calls the same predicate three times too, rather than keeping a fourth hand-inlined copy of the
# same pattern. PaperVenueResponseInvalid (AC-6a, night seven) is also one of the exceptions
# section 12 does not name, and the newest: the venue's own filled_quantity, fill_price and fees
# were never checked at all before this fold, and a NaN or an infinity in any of the three could
# reach a NULLABLE paper_fills column, either stored as a silent
# NULL (SUM then skips it) or stored as the literal, uncaught value. This one writes NO row: under
# DECISIONS #322 the intent row is already durably committed before
# the venue is ever called, so a malformed response is left as that same open intent (no terminal
# outcome), never recorded as a refusal (the venue may already have executed), and reconciliation
# resolves it later by client_order_id. Eight of these classes (PaperNegativeNetQuantityInvariant,
# PaperSliceFenceBreach, PaperLongOnlyViolation, PaperOrderNotExpressible, PaperRunKilling,
# PaperOrderInputInvalid, PaperNumericInputInvalid, PaperVenueResponseInvalid) are raised with a
# crafted message plus extra positional detail arguments so a caller can read structured values off
# the exception; _PaperRefusalWithDetail, defined just below, is their shared base and is why str()
# on any of them now returns the message alone rather than Python's own default rendering of a
# multi-argument exception (round 4 should-fix; see that class's own docstring for the confirmed
# behaviour this fixes).
# =============================================================================

class PaperDbParentDirectoryMissing(RuntimeError):
    """Raised when paper_db_path's parent directory does not exist, before sqlite3 ever opens it."""


class PaperSliceStateMissing(RuntimeError):
    """Raised when a paper_runs row has no matching paper_slice_current row.

    Section 11, E3's own "NULL or absent" answer: a missing slice row for an existing run is a
    corruption, not an empty state, so this module refuses rather than computing a default.
    """


class _PaperRefusalWithDetail(RuntimeError):
    """Shared base for this module's own refusal exceptions that are raised with a crafted message
    plus extra positional detail arguments (a paper_run_id, a symbol, an offending quantity) so a
    caller can read structured values straight off the exception instead of re-parsing the message
    string (round 4 should-fix). Python's own RuntimeError.__str__ returns repr(self.args), the
    WHOLE args tuple, once there is more than one positional argument; confirmed empirically before
    writing this fix (a bare RuntimeError("message", 1, 2) stringifies as "('message', 1, 2)", not
    "message"), so without this override a caller that logs str(exc) or formats %s on any of this
    module's own multi-argument exceptions would see a Python tuple literal instead of the intended
    message. This override changes only what str() reports: the extra positional args are
    deliberate and are never dropped, and .args plus repr(exc) still carry every one of them
    exactly as before.
    """

    def __str__(self) -> str:
        # args[0] is always the crafted message every one of this module's own raise sites passes
        # first; anything after it is structured detail meant for a caller reading .args directly,
        # never meant to appear inside the rendered string itself.
        return str(self.args[0]) if self.args else super().__str__()


class PaperNegativeNetQuantityInvariant(_PaperRefusalWithDetail):
    """Raised when a fill would derive a negative net_quantity for a symbol: this store's own
    average-cost arithmetic does not define what that means, so this halts loudly, naming the run,
    the symbol and the offending quantity, rather than computing a fence over an undefined state.

    Two raise sites now share this one invariant. _recompute_slice_state's own internal check
    (below) is the backstop for every caller, including mark_position_price, which never goes
    through submit_paper_order at all. submit_paper_order (round 4 should-fix pair) also raises it
    directly, one level up, once it has PROJECTED that writing a fill would trip that same backstop:
    gate finding B-AC-16/18 already makes this unreachable through a legitimate REQUEST (the
    long-only fence refuses any sell whose own submitted_quantity exceeds what is held), so the only
    way left to reach it is a venue that reports back filling MORE than it was ever asked to fill.
    submit_paper_order catches that case before writing a normal fill row, writes a durable anomaly
    row instead (filled_at left NULL on purpose, so no future recompute ever replays it and
    re-raises this on the same run forever), and raises this exception directly; see that function's
    own docstring for the full shape.
    """


class PaperSliceFenceBreach(_PaperRefusalWithDetail):
    """Raised when a BUY's intended notional exceeds a run's available buying power.

    Section 9.2: available buying power = opening slice cash + realised profit and loss minus
    committed capital in open positions and working orders. Never resized down to fit; the
    strategy intended a position and did not get it, and that is a counted, visible event. Gate
    finding B-AC-18 narrowed this to the buy side only: the fence exists to stop capital LEAVING
    the slice, and a sell returns capital rather than spending it, so a sell can never breach this
    fence; see PaperLongOnlyViolation below for what fences a sell instead.
    """


class PaperLongOnlyViolation(_PaperRefusalWithDetail):
    """Raised when a SELL's submitted quantity exceeds the quantity currently held long for that
    symbol.

    Gate finding B-AC-16/18: long only is Tamer's ruled policy (group C of the parameter taxonomy,
    2026-09-02), and section 12's table predates this rule so it names no exception for it; this is
    this build's own name for the refusal. A sell up to and including the held quantity is a
    legitimate exit and always succeeds, whatever available buying power reads, even exactly zero;
    a sell beyond the held quantity would open a short, which this module builds no path for at
    all, so it is refused, recorded, and never resized down to fit. "Submitted" is deliberate (gate
    finding B3, round 2): the compared number is submitted_quantity, floor(intended_notional /
    price_per_share), the same figure venue.submit_order below receives, never intended_quantity
    alone. intended_quantity, intended_notional and price_per_share are three independent inputs
    (see paper_fills' own DDL comment), so nothing ties them together except this fence testing the
    one that actually reaches the venue.
    """


class PaperOrderNotExpressible(_PaperRefusalWithDetail):
    """Raised when floor(intended_notional / price_per_share) is zero: the P > N cliff.

    Section 4.3: whole shares only means a position priced above its own intended notional cannot
    be expressed at any size. Never skipped in silence and never rounded up to one share the
    sizer never asked for.
    """


class PaperOrderInputInvalid(_PaperRefusalWithDetail):
    """Raised when intended_quantity, intended_notional or price_per_share is not a finite,
    correctly-signed number: NaN, positive or negative infinity, a non-positive price_per_share, or
    a negative intended_quantity or intended_notional (night six round 4, blocker).

    Checked once, as the FIRST thing submit_paper_order does after confirming the run exists, and
    before ANY other refusal write or arithmetic runs. That ordering closes two things at once.
    First, submitted_quantity = floor(intended_notional / price_per_share) alone opens three
    distinct hazards on unchecked inputs: a negative price_per_share flips submitted_quantity's own
    sign past both the buying-power fence (which tests intended_notional directly, never the
    post-division figure) and the P > N cliff (which only tests equality to zero, never sign);
    price_per_share == 0.0 raises an uncaught ZeroDivisionError; and NaN in intended_notional or
    price_per_share raises an uncaught ValueError from math.floor. A fourth hazard, not in the
    original finding and confirmed empirically while building this fix: an infinite intended_notional
    or price_per_share can make the ratio itself infinite, and math.floor raises OverflowError on
    that. Second, and the reason this check runs even before the kill check: _record_order_refusal
    binds intended_quantity and intended_notional into two REAL NOT NULL columns, and EVERY refusal
    write in this function goes through it, including the kill check immediately below. NaN and
    infinity behave DIFFERENTLY here, verified in the lead's own session (AC-0 root-cause dispatch,
    night seven, SQLite 3.46.1 through Python 3.14.4, an in-memory database, not necessarily this
    backend's own interpreter; correcting an earlier version of this paragraph that bundled the two
    together as one behaviour): a bound NaN IS converted to NULL, and the NOT NULL constraint then
    refuses that NULL, so any refusal write downstream of a NaN intended_quantity or
    intended_notional would raise an unrelated, confusing sqlite3.IntegrityError naming a column,
    not the caller's own malformed order (a SHOULD-FIX on its own under the sprint's own severity
    list: "an error message that cannot be debugged"). A bound infinity is NOT converted to
    anything: SQLite accepts it and stores the literal value as-is, even in a NOT NULL column, with
    no constraint to catch it, which is the worse failure because nothing raises at all. Validating
    first closes both for all five order-refusal kinds at once, not just this one.

    Like the other four order refusals, this writes a paper_fills row before raising, UNLESS the
    caller's own intended_quantity or intended_notional is not finite (NaN or either infinity), in
    which case no row is written at all: a NOT NULL column cannot honestly hold any of the three
    (NaN because the constraint refuses it; infinity because storing the literal value would be
    exactly the unvalidated write this exception exists to prevent), and rather than fabricate a
    numeric stand-in for a value that cannot be truthfully represented, this raises with no row, on
    the same footing as PaperSliceStateMissing and PaperPromoteWhileKilling elsewhere in this
    module, both of which also write no row when a row cannot or should not exist. The condition
    guarding the write is math.isfinite on both values, never math.isnan alone (round 4's original
    check tested isnan only; corrected by the AC-0 root-cause dispatch, night seven, since isnan
    alone would have let a bare infinity through into a NOT NULL column, the exact silent failure
    this whole exception exists to close). price_per_share is never itself a paper_fills column, so
    a malformed price_per_share alone never triggers the no-row case. This exception's own args
    always carry the true values, NaN and infinity included, since a Python exception's args tuple
    has no NOT NULL constraint to defeat it.
    """


class PaperNumericInputInvalid(_PaperRefusalWithDetail):
    """Raised when a numeric magnitude this module was given directly as a function's own parameter
    is not finite (NaN, positive infinity or negative infinity) or violates its own sign rule,
    before that value can reach arithmetic or a database column (night seven, AC-0 root-cause
    dispatch).

    PaperOrderInputInvalid, just above, keeps its own name, its own combined multi-value message and
    its own paper_fills refusal-recording behaviour: this dispatch only refactored its predicate to
    the shared one below and corrected its row-write guard from isnan to isfinite (see that
    exception's own docstring); it never became this exception. This exception is the
    general-purpose sibling for every OTHER public function
    in this module that takes a numeric magnitude and has no order, and no paper_fills row, to
    record a refusal against: mark_position_price's new_price (the named instance: a live-feed price
    with no check at all, reaching paper_slice_current.position_value and .equity, both REAL NOT
    NULL), admit_run's slice_opening_cash (which seeds four REAL NOT NULL columns across three
    tables and is read back on every later recompute for that run's whole life), and every parameter
    of capacity_ceiling and compute_available_buying_power, two pure functions with no database
    connection of their own whose return values or whose own already-stored inputs feed the same
    functions above. Raised by _require_finite_signed, defined in the next section below, which is
    also the one place submit_paper_order's own inline check now gets its predicate from, so this
    module has exactly one definition of "finite and correctly signed" rather than one written by
    hand per function.
    """


class PaperVenueResponseInvalid(_PaperRefusalWithDetail):
    """Raised when the venue's own filled_quantity, fill_price or fees is not finite or violates
    its sign rule (AC-6a, night seven). Checked with the SAME _is_finite_signed predicate this
    module already uses on the caller's own order inputs, applied here to the venue's ANSWER
    instead: filled_quantity and fees must be finite and >= 0 (a fill can legitimately be exactly
    zero shares' worth of fees, never negative or undefined); fill_price must be finite and > 0,
    the same strictly-positive rule price_per_share already carries.

    Writes NO row: by the time this function ever sees a venue response, DECISIONS #322's own
    intent row is already durably committed, so the venue may already have executed and a refusal
    row here would be a lie about a trade that happened. The intent row is left exactly as it was
    committed (open, no terminal outcome); this raises loudly so nothing is silent, and
    reconcile_open_intents (below) is what resolves the row later, by this order's own
    client_order_id, once the venue can be asked again.
    """


class PaperRunKilling(_PaperRefusalWithDetail):
    """Raised when an order is intended for a run whose Kill has begun (kill_started_at is set).

    Section 6.4: a run that is leaving paper does not open new positions. The order is recorded as
    a refusal (section 10.3) and never held until the Kill finishes.
    """


class PaperRunEnded(_PaperRefusalWithDetail):
    """Raised when an order is intended for a run that has ended (ended_at is set).

    An ended run takes no order, whatever end_reason holds: killed, promoted or NULL. The refusal is
    recorded like PaperRunKilling's (section 10.3) and the venue is never called.
    """


class PaperLimitUnset(_PaperRefusalWithDetail):
    """Raised when any of the six paper limits has no value (DECISIONS #478 item 1: a paper limit
    with no value blocks every paper order until Tamer sets it). `word` is paper-limit-unset and
    `keys` names the unset limits. The order is recorded as a refusal first."""

    word = "paper-limit-unset"

    def __init__(self, message: str, keys: tuple):
        super().__init__(message, keys)
        self.keys = keys


class PaperLimitBreach(_PaperRefusalWithDetail):
    """Raised when an order would break a set paper limit (DECISIONS #477 item 4), or when a loss
    limit cannot be checked because a run has no baseline (paper-loss-baseline-absent, fail
    closed). `word` is one of paper-limit-per-order-notional, paper-limit-ticker-net-cap,
    paper-limit-total-net-cap, paper-limit-loss-window, paper-limit-loss-since-go,
    paper-limit-order-rate, paper-loss-baseline-absent. `limit` and `figure` are the limit's value
    and the figure the order would have made. The order is recorded as a refusal first."""

    def __init__(self, word: str, message: str, limit, figure):
        super().__init__(message, word, limit, figure)
        self.word = word
        self.limit = limit
        self.figure = figure


class PaperPromoteWhileKilling(RuntimeError):
    """Raised when Promote is pressed for a run whose Kill has begun and has not finished.

    Section 6.5: a promotion request for a run that is being closed would point the real-capital
    path at a run that no longer exists. Writes no paper_promotions row at all.
    """


class PaperApproveRefused(RuntimeError):
    """Raised when APPROVE FOR REAL MONEY is pressed for a run that cannot be approved.

    word is 'run-not-running' (the run has already ended) or 'no-live-request' (the run's newest
    promotion row is absent or not 'requested'). The message is the sentence the page shows.
    """

    def __init__(self, word: str, message: str):
        super().__init__(message)
        self.word = word


class PaperMarketDataNotRealTime(RuntimeError):
    """Raised when the node cannot prove its market data feed is the real-time entitlement.

    Section 12, assumption A6: a paper run on a feed that is not proven real-time is a different
    experiment from one on real-time data, and there is no delayed-data path anywhere here.
    """


class PaperSessionTaken(RuntimeError):
    """Raised when the node's broker session is lost, or taken by the account's other username.

    Section 12, assumption A5: halts trading for the affected runs and reports; never reconnects
    in a silent loop.
    """


class PaperPositionMismatch(RuntimeError):
    """Raised when a locally recorded position disagrees with the venue's reported position.

    Section 12, assumption A9: our own book is never trusted against the venue's. Never adjusts
    the local book to match silently; halts the affected run and reports both figures.
    """


# =============================================================================
# Shared numeric-input validation (night seven, AC-0 root-cause dispatch over the unvalidated-
# numeric-input class that recurred at round 3, fixed only for submit_paper_order, then again at
# round 5 on mark_position_price, a different function). One predicate, one raiser, called by every
# public function below that takes a numeric magnitude as its own parameter: mark_position_price's
# new_price, admit_run's slice_opening_cash, and every parameter of capacity_ceiling and
# compute_available_buying_power. submit_paper_order keeps its own PaperOrderInputInvalid, its own
# combined multi-value message and its own paper_fills refusal-recording behaviour (see that
# exception's own docstring for why), but its inline check now calls _is_finite_signed three times
# rather than hand-writing "math.isfinite(x) and x >= 0.0" three times over, so this pair of
# functions is the one place "finite and correctly signed" is decided for the whole module, not four
# hand-written copies of the same three-line pattern that a fifth new numeric parameter could still
# be added without one, which is exactly how the class recurred the first two times.
# =============================================================================

def _is_finite_signed(value: float, *, allow_negative: bool, allow_zero: bool) -> bool:
    """True when value is finite (not NaN, not positive or negative infinity) and satisfies its own
    sign rule. allow_negative=False rejects anything below zero; allow_zero=False on top of that
    also rejects exactly zero, giving a strict positive, the same rule submit_paper_order's own
    price_per_share check already used before this fold. This is the ONE definition of "finite and
    correctly signed" in this module; _require_finite_signed below and submit_paper_order's own
    inline check both call it rather than each restating the same three lines.
    """
    if not math.isfinite(value):
        return False
    if not allow_negative and value < 0.0:
        return False
    if not allow_zero and value == 0.0:
        return False
    return True


def _require_finite_signed(name: str, value: float, *, allow_negative: bool, allow_zero: bool) -> None:
    """Raise PaperNumericInputInvalid, naming the parameter and its value, unless _is_finite_signed
    says value is usable. Every public function below that takes a numeric magnitude and is not
    submit_paper_order calls this once per magnitude parameter, as the first thing the function
    does, before that value can reach arithmetic or a database statement.
    """
    if not _is_finite_signed(value, allow_negative=allow_negative, allow_zero=allow_zero):
        if allow_negative:
            sign_rule = "finite"
        elif allow_zero:
            sign_rule = "finite and >= 0"
        else:
            sign_rule = "finite and > 0"
        raise PaperNumericInputInvalid(
            f"{name}={value!r} must be {sign_rule}, refusing rather than letting it reach "
            "arithmetic or a column",
            name, value,
        )


# =============================================================================
# Connection and transaction helpers, mirroring opsdb.py's _connect_ops / get_ops_conn /
# ops_transaction exactly (brief section 10.1: "paperdb gets the same guard, for the same reason,
# with the same fail-loud discipline").
# =============================================================================

def _connect_paper(paper_db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection on paper.db with the per-connection pragmas this module relies on set.

    This is the ONLY function in this module that calls sqlite3.connect directly; every other
    function reaches the database through get_paper_conn or paper_transaction, both of which call
    this. That makes this function the one place "PRAGMA foreign_keys = ON" can be forgotten, and
    it is set here, every time, before the connection is handed to any caller.
    """
    path = Path(paper_db_path)
    # A missing parent directory surfaces here as a named, debuggable error rather than a bare
    # sqlite3.OperationalError with no path in it. This module never creates the directory itself
    # (that would be an unrequested fallback); it names the problem loudly instead.
    if not path.parent.is_dir():
        raise PaperDbParentDirectoryMissing(
            f"cannot open paper.db at {path}: its parent directory {path.parent} does not exist"
        )
    # Autocommit mode at the driver level: this module owns every transaction explicitly with its
    # own BEGIN / COMMIT / ROLLBACK statements, the same choice opsdb.py and eventsdb.py make.
    conn = sqlite3.connect(str(path), isolation_level=None)
    # Foreign keys are per-connection in SQLite: without this, an FK violation silently no-ops,
    # which on a money store means an orphan fill or an orphan equity point is writable after all.
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL is persistent at the file level once set; re-running this on an already-WAL file is free.
    conn.execute("PRAGMA journal_mode = WAL;")
    # Dict-like rows so a caller reads row["column"] instead of indexing by position.
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_paper_conn(paper_db_path: Union[str, Path, None] = None):
    """Context manager for a read-only query or a single-statement write on paper.db.

    Per-call lifecycle: opens a fresh connection on enter, closes it on exit. No singleton, no
    pooling, so a probe or a caller pointed at a throwaway file never inherits hidden state from a
    previous call.
    """
    path = paper_db_path if paper_db_path is not None else DB_PATH
    conn = _connect_paper(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def paper_transaction(paper_db_path: Union[str, Path, None] = None):
    """Context manager wrapping BEGIN IMMEDIATE / COMMIT / ROLLBACK over a fresh connection.

    For multi-statement writes: the caller's block runs inside one transaction, committed on a
    clean exit and rolled back on any exception. A refused order's row is the one write that
    survives that rollback: _record_order_refusal commits it before the refusal raises. press_kill and submit_paper_order (both below)
    manage further transaction boundaries of their OWN inside this one, on the same connection, via
    _commit_and_reopen just below, so that a step already carried out at a venue is durable before
    the next step, or the next possible raise, runs; see each function's own docstring for why.
    """
    path = paper_db_path if paper_db_path is not None else DB_PATH
    conn = _connect_paper(path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        # Catches every exception on purpose, not narrowed to specific types. Any exception here
        # must trigger the rollback below, and every path out of this block ends in a raise:
        # either the original exception, unchanged, when the rollback step succeeds or finds
        # nothing to undo, or the rollback's own failure, with the original exception attached
        # as its context, when the rollback itself faults. Either way, nothing is ever swallowed
        # by catching broadly.
        except Exception:
            # A transaction is not always open here any more (night seven, DECISIONS #322 point 5
            # fold): submit_paper_order and press_kill now commit an early step ALONE, with nothing
            # reopened until after their own venue call returns, so an exception the venue call
            # itself raises, or AC-6a's own response validation raises, can reach this block with
            # conn.in_transaction already False. conn.in_transaction is a real sqlite3.Connection
            # attribute, confirmed empirically by this unit's own probe: False on a fresh
            # connection, True the instant an explicit BEGIN runs, still True after a write inside
            # it, False again after COMMIT. Rolling back unconditionally when nothing is open raises
            # sqlite3.OperationalError: cannot rollback - no transaction is active, and because that
            # raise happens from inside this except block, Python's own exception chaining makes
            # THAT what the caller sees instead of the real exception this block exists to
            # propagate unchanged (measured directly: six previously-green probes broke exactly
            # this way the moment nothing was open here). Checking in_transaction first is what
            # keeps a genuine rollback (something IS open, and it still must be undone) and a
            # genuine no-op (nothing is, so there is nothing to undo) both correct; what happens to
            # the exception in flight after that -- including the case where the ROLLBACK statement
            # itself faults -- is this except block's own opening comment above, stated once, not
            # restated here.
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
    finally:
        conn.close()


def _commit_and_reopen(conn: sqlite3.Connection) -> None:
    """Close out whatever transaction is currently open on conn, and immediately start a fresh one.

    Used ONLY where no venue call runs before the transaction this reopens is next touched (night
    seven, DECISIONS #322 point 5 fold: "without any transaction spanning the venue call"). Five
    call sites: _record_order_refusal, which commits the refusal row alone before its caller
    raises, so the row survives paper_transaction's rollback (DBG-6e; no venue call runs before
    the transaction it reopens is next touched, the raise follows at once); submit_paper_order's own anomaly path (the venue call already happened
    and already returned by the time this runs; the fill it actually executed is committed here,
    durably, before PaperNegativeNetQuantityInvariant is raised directly, so the venue call's own
    trace survives the raise -- gate finding B4, round 2, still honoured; nothing further calls the
    venue before the raise); and press_kill's own steps 3, 4 and 5 (step 3's venue call,
    close_all_positions, already ran and already returned before its OWN reopen further down runs
    this; steps 4 and 5 call no venue method at all -- see press_kill's own docstring). Everywhere a
    venue call DOES run next -- submit_paper_order's own step 1 to step 2, and press_kill's own
    steps 1 to 2 and 2 to 3 -- this function is NOT used any more: those sites instead COMMIT
    alone, call the venue with nothing open on conn, then BEGIN IMMEDIATE again only once the venue
    has answered, so no transaction spans the call itself (each site's own comment explains why).

    Same reason as ever for reopening at all (DDIA chapter 8, quoted at the top of this module): a
    step that just caused an external side effect at a venue must be durable before the next thing
    that could raise runs, so a later rollback can never silently un-happen it. Where the fill write,
    the recompute and the paper_slice_current update need to run inside ONE uninterrupted
    transaction with no intermediate commit (submit_paper_order's own sound-projection path), this
    function is not called at all between them, which is what closes the race an intermediate
    commit would leave open between a fill becoming durable and paper_slice_current reflecting it
    (round 4 should-fix pair; DDIA chapter 8's own Figure 8-2 example is the same shape: a
    multi-object transaction is needed precisely when "denormalized data kept in sync" must never
    be observed half-updated).
    conn always arrives here already inside an open transaction (every remaining call site's own
    caller opened one, either directly just above or via an earlier BEGIN IMMEDIATE this same
    function issued last time it ran), so this is always a real commit followed by a fresh BEGIN,
    never a commit with nothing to close.
    """
    conn.execute("COMMIT;")
    conn.execute("BEGIN IMMEDIATE;")


def _commit_if_open(conn: sqlite3.Connection) -> None:
    """Commit whatever transaction is open on conn; do nothing at all when none is.

    Guards a venue call immediately before it runs (DECISIONS #322 point 5), for a hazard the
    step's own preceding COMMIT cannot cover by itself: press_kill is resumable and can be re-
    entered directly at any step (kill_step is read back from paper_runs on entry, brief section
    6.4), so a call that resumes exactly at step 2 or step 3 never runs the step before it in THIS
    call, and the AMBIENT transaction its own caller's paper_transaction opened on entry is still
    sitting open, untouched, the moment that step's own venue call is about to run -- the same
    "transaction spans the venue call" shape this fold exists to close, just reached by resuming
    mid-sequence instead of by running straight through. Calling this unconditionally, right before
    each such venue call, commits that ambient transaction when it is the resume case, and is a
    genuine no-op when it is not (the sequential case, where the step just before already
    committed via its own plain COMMIT, so conn.in_transaction is already False here): unlike an
    unconditional COMMIT, which raises sqlite3.OperationalError: cannot commit - no transaction is
    active when nothing is open, this never raises either way.
    """
    if conn.in_transaction:
        conn.execute("COMMIT;")


# =============================================================================
# Schema DDL: five tables, four indexes (brief sections 10.2 through 10.5). Every CREATE statement
# is idempotent (IF NOT EXISTS), so init_paper_db() run twice on the same file changes nothing.
# paper_runs is created first because every other table's REFERENCES clause names it; SQLite parses
# a CREATE TABLE's foreign keys against whatever already exists, so the referenced table must exist
# first at creation time even though SQLite does not enforce the reference until a row is written.
# =============================================================================

_DDL_PAPER_RUNS = """
CREATE TABLE IF NOT EXISTS paper_runs (
    paper_run_id           INTEGER PRIMARY KEY,
    strategy_id             INTEGER NOT NULL,
    strategy_name            TEXT    NOT NULL,
    strategy_version          INTEGER NOT NULL,
    source_run_id              INTEGER NOT NULL,
    slice_opening_cash          REAL    NOT NULL,
    expected_bounds_json         TEXT    CHECK (expected_bounds_json IS NULL OR json_valid(expected_bounds_json)),
    started_at                    TEXT    NOT NULL,
    ended_at                       TEXT,
    end_reason                      TEXT    CHECK (end_reason IS NULL OR end_reason IN ('killed','promoted')),
    kill_started_at                  TEXT,
    kill_step                         TEXT    CHECK (kill_step IS NULL OR kill_step IN ('marked', 'orders_cancelled','positions_closed','fills_written','slice_returned'))
);
"""

# Append-only order-intent log (brief section 10.3, DDIA chapter 3's event log / CQRS shape). Four
# separate quantity columns, none derived from another at write time, so the quantisation gap
# between what the sizer intended and what the venue could actually take is a stored fact rather
# than a reconstruction (section 4.5). The two intended columns are always known, so they are
# NOT NULL (with a narrow, deliberate exception: see PaperOrderInputInvalid's own docstring for a
# case that writes no row at all, because a NOT NULL column cannot honestly hold a non-finite
# value: NaN or either infinity, verified to behave differently from each other, AC-0 root-cause
# dispatch, night seven; see that exception's own docstring for the distinction); a
# refusal our own code raised before the order ever reached a venue leaves submitted_quantity,
# submitted_at and venue_order_id all NULL (never a fabricated zero), and a working or refused
# order leaves filled_quantity, fill_price, fees and filled_at NULL until (or unless) a fill
# actually happens. recorded_at is the one column that is always known regardless of outcome,
# because it is our own order path's own clock, not the venue's. Round 4 adds a second reason
# filled_at can be NULL even though every other venue-reported column is populated: an ANOMALY row
# (submit_paper_order, the sound/unsound projection split), written when the venue's own fill
# cannot be safely folded into this run's derived state (an overfilling venue driving net_quantity
# negative). filled_at IS NOT NULL is the exact test _recompute_slice_state already uses to decide
# what counts as "happened" for this run (see that function below); leaving it NULL on an anomaly
# row is this table's own event log keeping the true, immutable fact of what the venue reported
# (DDIA chapter 3: "Provides an audit log") while the derived VIEW legitimately excludes an event
# its own projection cannot soundly represent, the same distinction chapter 3 draws between the log
# and a materialized view built from it ("Materialized views are reproducible (delete, rebuild)").
#
# client_order_id (night seven, DECISIONS #322 and #326, ruled, not this module's own choice): the
# idempotency key DDIA chapter 8 names ("use idempotency" as the alternative to 2PC for an external
# side effect a local rollback cannot undo). Minted by THIS module, once per order attempt, before
# the venue is ever called, and written into the intent row at the same moment that row is first
# committed (submit_paper_order, three-step redesign below) -- so the row exists, addressable by an
# id this code controls, before there is anything to say about it yet. NOT NULL makes a row without
# one unwritable rather than merely discouraged, which is what makes the crash-recovery query total
# rather than best effort; UNIQUE is what makes a second venue call for the same intent, made in
# error, a constraint violation rather than a silent duplicate. EVERY row this module ever writes to
# this table carries one: an open intent, a filled order, and a pre-flight refusal alike, generated
# once, before the first check runs, so a refusal is exactly as addressable as a fill.
_DDL_PAPER_FILLS = """
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id             INTEGER PRIMARY KEY,
    paper_run_id        INTEGER NOT NULL REFERENCES paper_runs(paper_run_id),
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    intended_quantity   REAL    NOT NULL,
    intended_notional   REAL    NOT NULL,
    submitted_quantity  REAL,
    filled_quantity     REAL,
    fill_price          REAL,
    fees                REAL,
    venue_order_id      TEXT,
    client_order_id     TEXT    NOT NULL UNIQUE,
    recorded_at         TEXT    NOT NULL,
    submitted_at        TEXT,
    filled_at           TEXT,
    refusal_reason      TEXT
);
"""

# Derived, mutable, one row per run (brief section 10.4): cash and position_value are the two
# addends, equity is their sum (never recomputed from a second expression; see _compute_equity
# below, the one place that formula lives), realised_pnl is a cumulative record of closed trades
# and committed_capital is the capital reserved by open positions. Rebuildable from paper_runs plus
# paper_fills at any time (DDIA chapter 3: "Materialized views are reproducible (delete, rebuild)"),
# which is exactly what _recompute_slice_state below does on every write.
_DDL_PAPER_SLICE_CURRENT = """
CREATE TABLE IF NOT EXISTS paper_slice_current (
    paper_run_id       INTEGER PRIMARY KEY REFERENCES paper_runs(paper_run_id),
    cash                REAL    NOT NULL,
    position_value       REAL    NOT NULL,
    equity                 REAL    NOT NULL,
    realised_pnl             REAL    NOT NULL,
    committed_capital          REAL    NOT NULL,
    updated_at                   TEXT    NOT NULL
);
"""

# The equity curve (brief section 10.4): one row per completed bar per running strategy. A bar the
# node did not process writes no row at all, which is a GAP a reader must see as a gap and never
# interpolate; equity is NOT NULL so a snapshot with an unknown value cannot be written in the
# first place, only be absent as a whole row.
_DDL_PAPER_EQUITY = """
CREATE TABLE IF NOT EXISTS paper_equity (
    paper_run_id  INTEGER NOT NULL REFERENCES paper_runs(paper_run_id),
    as_of         TEXT    NOT NULL,
    equity        REAL    NOT NULL,
    PRIMARY KEY (paper_run_id, as_of)
);
"""

# The promotion request record (brief section 10.5). The state column's CHECK enum is the only
# place in this whole module the word describing Tamer's own go appears: the real-capital path may
# act only on a row in that state carrying a non-NULL decision_ledger_entry, and no function in
# this module can write either; promote() below only ever writes the first word of this enum.
_DDL_PAPER_PROMOTIONS = """
CREATE TABLE IF NOT EXISTS paper_promotions (
    promotion_id            INTEGER PRIMARY KEY,
    paper_run_id             INTEGER NOT NULL REFERENCES paper_runs(paper_run_id),
    requested_at               TEXT    NOT NULL,
    state                         TEXT    NOT NULL CHECK (state IN ('requested','granted','declined','withdrawn')),
    decided_at                      TEXT,
    decision_ledger_entry              TEXT
);
"""

# Paper limits (lane UI19-1a, DECISIONS #477 items 4 and 5, #478 items 1 and 2): the same limit
# kinds as real money, with values of their own that Tamer sets on the Paper page. This lane stores
# and serves them; UI19-1b enforces them, and a limit with no value (NULL) blocks every paper order
# until Tamer sets it. DDIA check (docs/books/kleppmann_canon.md, chapter 1, "Systems of record and
# derived data"): this table is a system of record, one row per limit, each fact stored once.
# The six keys are the six order limits of realdb.REAL_TRADING_PARAMETER_KEYS. Its three max-age
# keys do not apply to paper: reconciliation_max_age guards the real reconciliation sweep (paper's
# reconcile_open_intents has no caller yet), account_value_max_age guards a broker account-value
# reading (paper's limits read only our own ledger), and kill_drill_max_age guards the real go's
# drill (paper has no go).
PAPER_LIMIT_KEYS = (
    "paper.per_order_notional_cap",
    "paper.ticker_net_cap",
    "paper.total_net_cap",
    "paper.loss_limit_window",
    "paper.loss_limit_since_go",
    "paper.order_rate_cap",
)

_DDL_PAPER_LIMITS = (
    "CREATE TABLE IF NOT EXISTS paper_limits (\n"
    "    key         TEXT PRIMARY KEY CHECK (key IN ("
    + ", ".join("'%s'" % k for k in PAPER_LIMIT_KEYS)
    + ")),\n"
    "    value       REAL CHECK (value IS NULL OR value > 0),\n"
    "    updated_at  TEXT\n"
    ");"
)

# Four indexes, each naming its query (brief sections 10.2, 10.3, 10.5; DDIA chapter 4: "Real DBs
# improve reads with indexes, paying for them with slower writes. Indexes are chosen, not
# automatic"). ix_paper_runs_running is PARTIAL because the running set stays the small part of a
# table that only grows: it serves "WHERE ended_at IS NULL ORDER BY started_at ASC", the card
# list's only query. ix_paper_runs_ended is a full index serving History's
# "WHERE ended_at IS NOT NULL ORDER BY ended_at DESC", a separate index because a partial index
# over the running rows cannot answer a query about the ended ones. ix_paper_fills_run_time serves
# a run's own fill history in time order, which the wins/losses count, the Sharpe input, the
# refusal counter and a resumed Kill's step 4 all walk in the same (paper_run_id, filled_at) order,
# so one index covers all of them. ix_paper_promotions_state serves "which promotion requests are
# outstanding, oldest first": "WHERE state = 'requested' ORDER BY requested_at ASC".
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_paper_runs_running ON paper_runs(started_at) WHERE ended_at IS NULL;",
    "CREATE INDEX IF NOT EXISTS ix_paper_runs_ended ON paper_runs(ended_at);",
    "CREATE INDEX IF NOT EXISTS ix_paper_fills_run_time ON paper_fills(paper_run_id, filled_at);",
    "CREATE INDEX IF NOT EXISTS ix_paper_promotions_state ON paper_promotions(state, requested_at);",
)

# Creation order: paper_runs first (every other table references it), then the rest in the order
# the brief itself introduces them (sections 10.3 to 10.5), then every index.
_DDL_STATEMENTS_ORDERED = (
    _DDL_PAPER_RUNS,
    _DDL_PAPER_FILLS,
    _DDL_PAPER_SLICE_CURRENT,
    _DDL_PAPER_EQUITY,
    _DDL_PAPER_PROMOTIONS,
    _DDL_PAPER_LIMITS,
) + _INDEXES


def init_paper_db(paper_db_path: Union[str, Path, None] = None) -> None:
    """Create paper.db's six tables and four indexes if the file does not already hold them.

    Idempotent: every statement is CREATE ... IF NOT EXISTS, so running this twice against the
    same file changes neither the schema nor any row. Mirrors opsdb.init_ops_db()'s own shape.
    """
    path = paper_db_path if paper_db_path is not None else DB_PATH
    with paper_transaction(path) as conn:
        for statement in _DDL_STATEMENTS_ORDERED:
            conn.execute(statement)
        # Seed the six rows empty. INSERT OR IGNORE keeps a value Tamer already set.
        for key in PAPER_LIMIT_KEYS:
            conn.execute("INSERT OR IGNORE INTO paper_limits (key, value) VALUES (?, NULL)", (key,))


class PaperLimitValueRefused(RuntimeError):
    """A paper-limit write refused. `word` is one of paper-limit-empty, paper-limit-unknown-key,
    paper-limit-value-invalid; the message starts with it and then says why in plain words."""

    def __init__(self, word: str, detail: str):
        super().__init__(f"{word}: {detail}")
        self.word = word


def get_paper_limits(conn: sqlite3.Connection) -> list:
    """The six paper limits in PAPER_LIMIT_KEYS order, each {"key", "value", "updated_at"};
    value is None while Tamer has not set it."""
    rows = {
        row["key"]: row
        for row in conn.execute("SELECT key, value, updated_at FROM paper_limits").fetchall()
    }
    missing = [key for key in PAPER_LIMIT_KEYS if key not in rows]
    if missing:
        raise PaperSliceStateMissing(
            f"paper_limits has no row for {missing!r}; init_paper_db seeds all six"
        )
    return [
        {"key": key, "value": rows[key]["value"], "updated_at": rows[key]["updated_at"]}
        for key in PAPER_LIMIT_KEYS
    ]


def set_paper_limits(conn: sqlite3.Connection, values: dict, now_iso: str) -> None:
    """Set any subset of the six paper limits, all or nothing. Every key and value is checked
    before the first write, so a refusal changes nothing. Call inside paper_transaction so the
    writes commit together."""
    if not isinstance(values, dict) or not values:
        raise PaperLimitValueRefused("paper-limit-empty", "no limit was given to set")
    for key in values:
        if key not in PAPER_LIMIT_KEYS:
            raise PaperLimitValueRefused(
                "paper-limit-unknown-key",
                f"{key!r} is not a paper limit; the six are {', '.join(PAPER_LIMIT_KEYS)}",
            )
    checked = {}
    for key, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not value > 0
        ):
            raise PaperLimitValueRefused(
                "paper-limit-value-invalid",
                f"{key} must be a number above zero, got {value!r}",
            )
        if key == "paper.order_rate_cap" and float(value) != math.floor(value):
            raise PaperLimitValueRefused(
                "paper-limit-value-invalid",
                f"{key} counts whole orders, got {value!r}",
            )
        checked[key] = float(value)
    for key, value in checked.items():
        conn.execute(
            "UPDATE paper_limits SET value = ?, updated_at = ? WHERE key = ?",
            (value, now_iso, key),
        )


# =============================================================================
# The one equity formula, and the derived-state recompute every write funnels through.
# =============================================================================

def _compute_equity(cash: float, position_value: float) -> float:
    """The one formula for equity anywhere in this store (brief section 10.4): equity =
    cash + position_value. Realised profit and loss is never folded in as a third addend, because
    that money is already sitting inside cash the moment a fill records it; adding it again would
    count the same profit twice.
    """
    return cash + position_value


# Below this absolute quantity a position counts as closed: float sums such as 0.1 + 0.2 - 0.3
# leave a remainder near 5.55e-17, which is rounding, never a share held.
_OPEN_POSITION_EPSILON = 1e-9


def _walk_average_cost(paper_run_id: int, opening_cash: float, fills: list,
                        price_overrides: Union[dict, None] = None) -> dict:
    """The average-cost walk itself (brief section 10.4's formula), pulled out of
    _recompute_slice_state so this ONE implementation serves both that function (the write path's
    own derived-state recompute) and this lane's new list_fills_with_stats read below (plan
    section 9 item 3, THE TRADES table's W/L figures). Before this split, routes/paper.py's own
    _serialize_run docstring named the alternative as a defect on purpose: "recomputing that walk
    a second time here, outside the function that owns it, is exactly the duplicated-logic risk
    the root-cause rule warns against." This function is that one owner; nothing else may
    reimplement it.

    `fills` is the ordered (oldest first) list of this run's own filled paper_fills rows, each
    carrying at least fill_id, symbol, side, filled_quantity, fill_price, fees. Returns the same
    five aggregate figures _recompute_slice_state has always returned PLUS
    `per_fill_realised_gain`, a dict of fill_id -> realised gain in dollars for every SELL fill
    (a buy fill realises nothing by this formula and is never a key in this dict). Raises
    PaperNegativeNetQuantityInvariant on the same invariant this function has always checked,
    byte-for-byte the prior inline loop's own condition and message.

    `per_symbol` (lane UI19-0) maps every symbol still held to {"quantity", "average_cost"}; a
    symbol whose absolute quantity is under 1e-9 is closed and has no entry. The other keys are
    unchanged.
    """
    price_overrides = price_overrides or {}
    net_quantity: dict = {}   # symbol -> current signed quantity held (buys positive, sells negative)
    avg_cost: dict = {}       # symbol -> quantity-weighted average cost per share of the OPEN lot
    last_price: dict = {}     # symbol -> the most recent price a fill has reported for it
    realised_pnl = 0.0        # cumulative realised profit and loss across every closing sell
    cash_delta = 0.0          # net change to cash since the slice's opening balance
    per_fill_realised_gain: dict = {}  # fill_id -> this ONE fill's own realised gain (sells only)

    for row in fills:
        symbol = row["symbol"]
        quantity = row["filled_quantity"]
        price = row["fill_price"]
        fees = row["fees"] or 0.0
        prior_quantity = net_quantity.get(symbol, 0.0)

        if row["side"] == "buy":
            # Cash leaves the slice for the notional bought, plus whatever fee the fill carried.
            cash_delta -= (quantity * price) + fees
            prior_cost = avg_cost.get(symbol, 0.0)
            new_quantity = prior_quantity + quantity
            if new_quantity != 0.0:
                # Blend the new shares into the running weighted-average cost of the open lot.
                avg_cost[symbol] = ((prior_quantity * prior_cost) + (quantity * price)) / new_quantity
            net_quantity[symbol] = new_quantity
        else:
            # Cash returns to the slice for the notional sold, minus whatever fee the fill carried.
            cash_delta += (quantity * price) - fees
            cost_basis = avg_cost.get(symbol, 0.0)
            fill_gain = quantity * (price - cost_basis)
            realised_pnl += fill_gain
            per_fill_realised_gain[row["fill_id"]] = fill_gain
            new_quantity = prior_quantity - quantity
            # Invariant guard (gate finding B-AC-16/18). submit_paper_order refuses every sell
            # whose quantity exceeds what is held (PaperLongOnlyViolation) before a fill is ever
            # written, so a negative net_quantity here is a state this average-cost arithmetic
            # never defines: halting loudly beats silently computing a fence over an undefined
            # position.
            if new_quantity < 0.0:
                raise PaperNegativeNetQuantityInvariant(
                    f"paper_run_id={paper_run_id!r} symbol={symbol!r}: derived net_quantity "
                    f"{new_quantity:.4f} is negative, a state this function's own average-cost "
                    "arithmetic does not define; refusing to compute a fence over it",
                    paper_run_id, symbol, new_quantity,
                )
            net_quantity[symbol] = new_quantity

        last_price[symbol] = price

    # A fresher mark (or the price of a fill just recorded, already folded in via the loop above)
    # wins over a symbol's own last fill price; every other symbol is untouched.
    last_price.update(price_overrides)

    position_value = sum(
        net_quantity.get(symbol, 0.0) * last_price.get(symbol, 0.0) for symbol in net_quantity
    )
    committed_capital = sum(
        net_quantity.get(symbol, 0.0) * avg_cost.get(symbol, 0.0) for symbol in net_quantity
    )
    cash = opening_cash + cash_delta
    equity = _compute_equity(cash, position_value)

    return {
        "cash": cash,
        "position_value": position_value,
        "equity": equity,
        "realised_pnl": realised_pnl,
        "committed_capital": committed_capital,
        "per_fill_realised_gain": per_fill_realised_gain,
        "per_symbol": {
            symbol: {"quantity": quantity, "average_cost": avg_cost.get(symbol, 0.0)}
            for symbol, quantity in net_quantity.items()
            if abs(quantity) >= _OPEN_POSITION_EPSILON
        },
    }


def _recompute_slice_state(conn: sqlite3.Connection, paper_run_id: int,
                            price_overrides: Union[dict, None] = None) -> dict:
    """Derive cash, position_value, equity, realised_pnl and committed_capital for one run purely
    from paper_runs.slice_opening_cash and that run's own paper_fills rows.

    Brief section 10.4 calls paper_slice_current derived data that "can be dropped and rebuilt
    from paper_fills plus paper_runs", and this is that rebuild, run on every write rather than
    only on demand, so the stored row never drifts from what the log actually says. The walk
    itself (average-cost accounting: a buy blends its price into the running quantity-weighted
    average cost of the open lot; a sell realises the gap between its own price and that average
    cost) lives in _walk_average_cost above, the one place this arithmetic runs.
    price_overrides lets a caller (mark_position_price) supply a fresher price for one symbol than
    its own last fill price, without inventing a fill that never happened; every other symbol
    keeps using its own most recent fill price, since a mark call carries no information about any
    symbol besides the one it names.
    """
    run_row = conn.execute(
        "SELECT slice_opening_cash FROM paper_runs WHERE paper_run_id = ?", (paper_run_id,)
    ).fetchone()
    if run_row is None:
        raise PaperSliceStateMissing(
            f"paper_run_id={paper_run_id!r} does not exist in paper_runs; cannot derive a slice "
            "state for a run that was never admitted"
        )

    # Only rows this module's own order path actually recorded a fill for: a working order or a
    # refusal has no filled_at and contributes nothing to a held position (brief section 10.3).
    fills = conn.execute(
        "SELECT fill_id, symbol, side, filled_quantity, fill_price, fees FROM paper_fills "
        "WHERE paper_run_id = ? AND filled_at IS NOT NULL ORDER BY filled_at ASC, fill_id ASC",
        (paper_run_id,),
    ).fetchall()

    walk = _walk_average_cost(paper_run_id, run_row["slice_opening_cash"], fills, price_overrides)
    return {
        "cash": walk["cash"],
        "position_value": walk["position_value"],
        "equity": walk["equity"],
        "realised_pnl": walk["realised_pnl"],
        "committed_capital": walk["committed_capital"],
    }


class PaperRunNotFound(LookupError):
    """No paper_runs row has this paper_run_id."""


def holdings(conn: sqlite3.Connection, paper_run_id: int) -> dict:
    """What one run holds now: {symbol: {"quantity", "average_cost"}}, from its own filled
    paper_fills, read exactly as _recompute_slice_state reads them and walked by the one
    _walk_average_cost. {} for a run with no fills; PaperRunNotFound for an unknown run; the walk's
    own PaperNegativeNetQuantityInvariant passes through. Nothing is stored: holdings are derived.
    """
    return holdings_and_cash(conn, paper_run_id)["per_symbol"]


def holdings_and_cash(conn: sqlite3.Connection, paper_run_id: int) -> dict:
    """The whole walk for one run (cash and per_symbol among its figures), same read as holdings."""
    run_row = conn.execute(
        "SELECT slice_opening_cash FROM paper_runs WHERE paper_run_id = ?", (paper_run_id,)
    ).fetchone()
    if run_row is None:
        raise PaperRunNotFound(f"paper_run_id={paper_run_id} does not exist.")
    fills = conn.execute(
        "SELECT fill_id, symbol, side, filled_quantity, fill_price, fees FROM paper_fills "
        "WHERE paper_run_id = ? AND filled_at IS NOT NULL ORDER BY filled_at ASC, fill_id ASC",
        (paper_run_id,),
    ).fetchall()
    return _walk_average_cost(paper_run_id, run_row["slice_opening_cash"], fills)


# =============================================================================
# The capital model's pure functions (brief section 9.1, 9.2). Neither takes a default for the
# figure it reads from the account or the store, on purpose: a pot figure or a fence figure with a
# fallback would be exactly the quiet substitution rule 3 of the brief's safe version forbids.
# =============================================================================

def capacity_ceiling(pot_value: float, slice_size: float) -> float:
    """The capacity ceiling: the pot's current value divided by the slice size (brief section 9.1).

    A ceiling, not a target, and computed fresh from whatever pot_value the caller just read from
    the account; no default and no module-level constant stands in for it anywhere in this file.
    Both inputs are validated first (AC-0 root-cause dispatch): a pot balance is never negative but
    can be legitimately zero (nothing left to allocate, a ceiling of zero, not an error), so
    pot_value only rejects NaN, both infinities and negative values; slice_size is the divisor, so
    it must be strictly positive, the same ZeroDivisionError-and-NaN hazard PaperOrderInputInvalid's
    own docstring already names for price_per_share elsewhere in this module.
    """
    _require_finite_signed("pot_value", pot_value, allow_negative=False, allow_zero=True)
    _require_finite_signed("slice_size", slice_size, allow_negative=False, allow_zero=False)
    return pot_value / slice_size


def compute_available_buying_power(slice_opening_cash: float, realised_pnl: float,
                                    committed_capital: float) -> float:
    """A run's available buying power (brief section 9.2): opening slice cash plus realised profit
    and loss, minus capital already committed to open positions and working orders. Deliberately
    NOT the same figure as paper_slice_current.equity, which includes unrealised gain; this figure
    excludes it, so an intended position can exceed this figure and be refused even when it would
    not exceed equity, which is the fence working rather than a disagreement between two numbers.

    Every input is validated first (AC-0 root-cause dispatch): slice_opening_cash is the same
    strictly-positive magnitude admit_run itself now requires at creation, checked again here
    because this function is a public entry point in its own right; realised_pnl is allowed to be
    negative, a losing run's honest state, so only NaN and both infinities are refused for it;
    committed_capital can be exactly zero (no open position) but is never negative.
    """
    _require_finite_signed("slice_opening_cash", slice_opening_cash, allow_negative=False, allow_zero=False)
    _require_finite_signed("realised_pnl", realised_pnl, allow_negative=True, allow_zero=True)
    _require_finite_signed("committed_capital", committed_capital, allow_negative=False, allow_zero=True)
    return slice_opening_cash + realised_pnl - committed_capital


# =============================================================================
# The venue boundary (brief section 10.6): the one place a broker's name could appear, and it does
# not appear here at all. StubVenue is the boundary's stub implementation for this lane, which
# builds no live connection (job file stop-condition 3).
# =============================================================================

class StubVenue:
    """Accepts everything (brief section 10.6): every submit_order call fills in full, and
    cancel_all_orders / close_all_positions are no-ops that report success, so a fence breach
    caught upstream of this class proves the refusal was ours, never the venue's (B-AC-3).

    client_order_id (AC-3, AC-5, night seven): a DEFAULT of None, never a required parameter,
    however much stricter reads -- a pre-existing, merged, currently-passing probe
    (ac3_stub_venue_accepts_everything_fence_still_holds.probe.py) calls this with only the
    original four positional arguments, and it must keep passing. When the id is absent, this
    double mints one of its own (the same STUB-{n} form as always) so the call still succeeds.
    Every record is kept in self._orders, keyed ONLY on client_order_id (the caller's id when one
    was given, this double's own minted one otherwise), so lookup_order below answers "by our id",
    never "by either id": the venue-minted STUB-{n} still rides along as the record's own
    venue_order_id field, but it is never a lookup key in its own right (ac5's own contract).
    """

    def __init__(self):
        self.calls: list = []
        self._next_order_id = 0
        self._orders: dict = {}  # client_order_id -> the same dict submit_order returned for it

    def submit_order(self, symbol: str, side: str, quantity: float, price: float,
                      client_order_id: Union[str, None] = None) -> dict:
        self.calls.append("submit_order")
        self._next_order_id += 1
        # No caller id supplied: mint one of our own so this call still succeeds either way,
        # exactly as it did before this module ever generated an id of its own.
        key = client_order_id if client_order_id is not None else f"STUB-{self._next_order_id}"
        record = {
            "filled_quantity": quantity,
            "fill_price": price,
            "fees": 0.0,
            "venue_order_id": f"STUB-{self._next_order_id}",
        }
        self._orders[key] = record
        return record

    def lookup_order(self, client_order_id: str) -> Union[dict, None]:
        """The same dict submit_order returned for that id, keyed on client_order_id ONLY, never on
        the venue-minted STUB-{n} id carried alongside it as a field; None when the id is unknown to
        this venue (reconcile_open_intents' own "never received it" release path, below).
        """
        return self._orders.get(client_order_id)

    def cancel_all_orders(self, paper_run_id: int) -> None:
        self.calls.append("cancel_all_orders")

    def close_all_positions(self, paper_run_id: int) -> None:
        self.calls.append("close_all_positions")


# =============================================================================
# The order path (brief sections 9.2, 4, 6.4, 12; B-AC-2/3/4/5).
# =============================================================================

def _record_order_refusal(conn: sqlite3.Connection, paper_run_id: int, symbol: str, side: str,
                           intended_quantity: float, intended_notional: float,
                           client_order_id: str, now_iso: str, reason: str) -> None:
    """Write one paper_fills row for an order OUR OWN code refused before it ever reached a venue
    (brief section 10.3). The two intended columns are always known and are stored; everything the
    order never had (submitted_quantity, submitted_at, venue_order_id, and the filled/* columns)
    stays NULL rather than a fabricated zero, so a refusal is never mistaken for a zero-size fill.
    recorded_at is set regardless, so every refusal is countable even though nothing about it ever
    reached a venue. client_order_id (night seven, DECISIONS #326) is stored too, NOT NULL and
    UNIQUE by the column's own schema: a refusal is exactly as addressable by its own id as a fill
    or an open intent is, and AC-3's own probe reads it back to prove a REFUSED row is a distinct,
    third shape from an OPEN INTENT or a FILLED one. CALLER OBLIGATION (round 4, widened by the AC-0
    root-cause dispatch, night seven): intended_quantity and intended_notional bind into two REAL
    NOT NULL columns, and NaN and infinity behave differently there, verified in the lead's own
    session (SQLite 3.46.1 through Python 3.14.4, an in-memory database, not necessarily this
    backend's own interpreter): a bound NaN converts to NULL and the NOT NULL constraint then
    refuses it, so this INSERT would reject a NaN with sqlite3.IntegrityError; a bound infinity is
    NOT converted to anything and would be accepted and stored as the literal value, silently, with
    no error at all, which is the worse failure of the two. Every caller of this function must
    already know both values are finite (covering NaN and both infinities, not NaN alone) before
    calling it. submit_paper_order's own validation (see PaperOrderInputInvalid) is what guarantees
    that for all five of its own order-refusal call sites; its own row-write guard tests
    math.isfinite on both values for exactly this reason.

    The row is committed before this returns (DBG-6e), by _commit_and_reopen, because every caller
    raises right after and the caller's paper_transaction rolls the open transaction back on that
    raise: without the commit the refusal row would be lost with it, and a refusal must be recorded
    and counted, never dropped (AC-L6, brief section 9.2). The commit also makes durable any
    earlier write the caller made in the same transaction. conn must arrive inside an open
    transaction, as every caller's does.
    """
    conn.execute(
        "INSERT INTO paper_fills (paper_run_id, symbol, side, intended_quantity, intended_notional, "
        " submitted_quantity, filled_quantity, fill_price, fees, venue_order_id, client_order_id, "
        " recorded_at, submitted_at, filled_at, refusal_reason) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, ?)",
        (paper_run_id, symbol, side, intended_quantity, intended_notional, client_order_id,
         now_iso, reason),
    )
    _commit_and_reopen(conn)


def _held_quantity(conn: sqlite3.Connection, paper_run_id: int, symbol: str) -> float:
    """The net signed quantity currently held for one symbol on one run (buys positive, sells
    negative), summed directly from paper_fills's own FILLED rows.

    Gate finding B-AC-16/18: deliberately independent of _recompute_slice_state's own internal
    net_quantity dict (which that function never returns to a caller anyway), so the sell-side
    long-only check in submit_paper_order below never leans on the very average-cost machinery a
    wrong fence could otherwise hide behind. Mirrors the probe suite's own independent oracle for
    this same figure.
    """
    rows = conn.execute(
        "SELECT side, filled_quantity FROM paper_fills WHERE paper_run_id = ? AND symbol = ? "
        "AND filled_at IS NOT NULL",
        (paper_run_id, symbol),
    ).fetchall()
    held = 0.0
    for row in rows:
        # A buy adds to the held quantity, a sell subtracts from it; side is constrained to
        # exactly "buy" or "sell" by paper_fills's own CHECK constraint.
        held += row["filled_quantity"] if row["side"] == "buy" else -row["filled_quantity"]
    return held


# Paper limit enforcement (lane UI19-1b, DECISIONS #477 item 4 and #478 item 1).
# The trailing window the order rate is counted over. It restates real_limits.ORDER_RATE_WINDOW_SECONDS
# (this module imports only .config and the standard library, so it cannot import real_limits); a
# probe asserts the two are equal. An operational dial (DECISIONS #204), never a limit: the cap is
# Tamer's own paper.order_rate_cap.
PAPER_ORDER_RATE_WINDOW_SECONDS = 60

_PAPER_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _new_york_day_start_iso(now_iso: str) -> str:
    """The start of the America/New_York calendar day that now_iso falls in, as UTC in the ISO form
    paper timestamps use. The loss-window baseline is the equity at this moment."""
    now_utc = datetime.strptime(now_iso, _PAPER_ISO_FORMAT).replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(ZoneInfo("America/New_York"))
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).strftime(_PAPER_ISO_FORMAT)


def _loss_window_baseline(conn: sqlite3.Connection, run_row, day_start_iso: str):
    """One run's equity at the start of today's New York day: the newest paper_equity row with
    as_of at or before that moment; else, for a run admitted today, its admission value (the
    credited slice, which is exactly the equity row admit_run writes at admission); else None.
    P6b's daily equity snapshot fills baselines later; until then a run older than today with no
    earlier row has no baseline and the order is refused, never guessed."""
    row = conn.execute(
        "SELECT equity FROM paper_equity WHERE paper_run_id = ? AND as_of <= ? "
        "ORDER BY as_of DESC LIMIT 1",
        (run_row["paper_run_id"], day_start_iso),
    ).fetchone()
    if row is not None:
        return row["equity"]
    if run_row["started_at"] >= day_start_iso:
        return run_row["slice_opening_cash"]
    return None


def _enforce_paper_limits(conn: sqlite3.Connection, paper_run_id: int, symbol: str, side: str,
                          intended_quantity: float, intended_notional: float,
                          price_per_share: float, client_order_id: str, now_iso: str) -> None:
    """Refuse the order, recording why, while any paper limit is unset or the order would break a
    set one. Checks, in this order: unset, per-order notional, per-ticker net cap, total net cap,
    loss over the window, loss since the go (counted from the newest save of the paper limits),
    order rate. Comparisons as real_limits': strictly
    greater for caps and losses, at or past for the rate.

    One valuation for the net caps and both losses (it differs from real money, whose caps read a
    reconciled market reading, because paper has none): every held symbol at its last fill price,
    the ledger's own mark, and this order's symbol at price_per_share, through _walk_average_cost's
    price_overrides. Equity now is that walk's equity.
    """
    def refuse(word, message, limit, figure):
        _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                               intended_notional, client_order_id, now_iso, word)
        raise PaperLimitBreach(word, f"{word}: {message}", limit, figure)

    limit_rows = get_paper_limits(conn)
    limits = {row["key"]: row["value"] for row in limit_rows}
    # The since-go total starts again at the newest save of any limit (DBG-6f): the newest
    # non-NULL updated_at over the six rows; None while no save is recorded.
    saved_stamps = [row["updated_at"] for row in limit_rows if row["updated_at"] is not None]
    reset_at = max(saved_stamps) if saved_stamps else None
    unset = tuple(key for key in PAPER_LIMIT_KEYS if limits[key] is None)
    if unset:
        _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                               intended_notional, client_order_id, now_iso, "paper-limit-unset")
        raise PaperLimitUnset(
            f"paper-limit-unset: no paper order can go out until every paper limit has a value; "
            f"not set yet: {', '.join(unset)}",
            unset,
        )

    per_order_cap = limits["paper.per_order_notional_cap"]
    if intended_notional > per_order_cap:
        refuse("paper-limit-per-order-notional",
               f"this order is {intended_notional:.4f}, above the per-order cap {per_order_cap:.4f}",
               per_order_cap, intended_notional)

    signed_notional = intended_notional if side == "buy" else -intended_notional
    open_runs = conn.execute(
        "SELECT paper_run_id, slice_opening_cash, started_at FROM paper_runs "
        "WHERE ended_at IS NULL ORDER BY paper_run_id"
    ).fetchall()
    walks = {}
    for run in open_runs:
        fills = conn.execute(
            "SELECT fill_id, symbol, side, filled_quantity, fill_price, fees FROM paper_fills "
            "WHERE paper_run_id = ? AND filled_at IS NOT NULL ORDER BY filled_at ASC, fill_id ASC",
            (run["paper_run_id"],),
        ).fetchall()
        walks[run["paper_run_id"]] = _walk_average_cost(
            run["paper_run_id"], run["slice_opening_cash"], fills, {symbol: price_per_share})

    ticker_held = 0.0
    ticker_intents = 0.0
    total_held = 0.0
    total_intents = 0.0
    for run in open_runs:
        run_id = run["paper_run_id"]
        walk = walks[run_id]
        held = walk["per_symbol"].get(symbol)
        if held is not None:
            ticker_held += held["quantity"] * price_per_share
        total_held += walk["position_value"]
        ticker_intents += open_intents_notional_for_symbol(conn, run_id, symbol)
        symbols_with_intents = conn.execute(
            "SELECT DISTINCT symbol FROM paper_fills WHERE paper_run_id = ? "
            "AND filled_at IS NULL AND refusal_reason IS NULL ORDER BY symbol",
            (run_id,),
        ).fetchall()
        for intent_row in symbols_with_intents:
            total_intents += open_intents_notional_for_symbol(conn, run_id, intent_row["symbol"])

    ticker_cap = limits["paper.ticker_net_cap"]
    ticker_figure = ticker_held + ticker_intents + signed_notional
    if ticker_figure > ticker_cap:
        refuse("paper-limit-ticker-net-cap",
               f"{symbol} would be {ticker_figure:.4f} net across every open run, above the "
               f"per-ticker cap {ticker_cap:.4f}", ticker_cap, ticker_figure)

    total_cap = limits["paper.total_net_cap"]
    total_figure = total_held + total_intents + signed_notional
    if total_figure > total_cap:
        refuse("paper-limit-total-net-cap",
               f"all positions would be {total_figure:.4f} net across every open run, above the "
               f"total cap {total_cap:.4f}", total_cap, total_figure)

    # The loss limits read the whole account (DECISIONS #477 item 4): a run that lost money and was
    # then killed or promoted still counts. Today's loss covers every run open now or ended at or
    # after today's New York start; the loss since the go covers every run, counted from the newest
    # save of the paper limits (reset_at): each run adds its equity at that moment (fills stamped
    # at or before it, last fill prices) minus its equity now. With no save recorded it counts from
    # each run's opening cash. An ended run's equity is its walk at its own last fill prices, with
    # no price override.
    day_start_iso = _new_york_day_start_iso(now_iso)
    all_runs = conn.execute(
        "SELECT paper_run_id, slice_opening_cash, started_at, ended_at FROM paper_runs "
        "ORDER BY paper_run_id"
    ).fetchall()
    window_loss = 0.0
    since_go_loss = 0.0
    for run in all_runs:
        run_id = run["paper_run_id"]
        if run["ended_at"] is None:
            equity_now = walks[run_id]["equity"]
        else:
            ended_fills = conn.execute(
                "SELECT fill_id, symbol, side, filled_quantity, fill_price, fees FROM paper_fills "
                "WHERE paper_run_id = ? AND filled_at IS NOT NULL ORDER BY filled_at ASC, fill_id ASC",
                (run_id,),
            ).fetchall()
            equity_now = _walk_average_cost(
                run_id, run["slice_opening_cash"], ended_fills, {})["equity"]
        if run["ended_at"] is None or run["ended_at"] >= day_start_iso:
            baseline = _loss_window_baseline(conn, run, day_start_iso)
            if baseline is None:
                refuse("paper-loss-baseline-absent",
                       f"run {run_id} has no equity reading at the start of today and was "
                       "not admitted today, so its loss today cannot be measured",
                       limits["paper.loss_limit_window"], None)
            window_loss += baseline - equity_now
        if reset_at is None:
            since_go_base = run["slice_opening_cash"]
        else:
            base_fills = conn.execute(
                "SELECT fill_id, symbol, side, filled_quantity, fill_price, fees FROM paper_fills "
                "WHERE paper_run_id = ? AND filled_at IS NOT NULL AND filled_at <= ? "
                "ORDER BY filled_at ASC, fill_id ASC",
                (run_id, reset_at),
            ).fetchall()
            since_go_base = _walk_average_cost(
                run_id, run["slice_opening_cash"], base_fills, {})["equity"]
        since_go_loss += since_go_base - equity_now

    window_cap = limits["paper.loss_limit_window"]
    if window_loss > window_cap:
        refuse("paper-limit-loss-window",
               f"the loss today across every run open today is {window_loss:.4f}, above the window "
               f"loss limit {window_cap:.4f}", window_cap, window_loss)

    since_go_cap = limits["paper.loss_limit_since_go"]
    if since_go_loss > since_go_cap:
        if reset_at is None:
            since_go_text = f"the loss since the runs began across every paper run is {since_go_loss:.4f}"
        else:
            since_go_text = (f"the loss since the paper limits were last saved at {reset_at} across "
                             f"every paper run is {since_go_loss:.4f}")
        refuse("paper-limit-loss-since-go",
               f"{since_go_text}, above the since-go loss limit {since_go_cap:.4f}",
               since_go_cap, since_go_loss)

    window_start_iso = (
        datetime.strptime(now_iso, _PAPER_ISO_FORMAT)
        - timedelta(seconds=PAPER_ORDER_RATE_WINDOW_SECONDS)
    ).strftime(_PAPER_ISO_FORMAT)
    # Rows that reached or are reaching the venue (refusal_reason IS NULL): a refusal never counts,
    # so a strategy that is refused cannot lock itself out. Counted over every run, as real money
    # counts over the account.
    rate_count = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_fills WHERE refusal_reason IS NULL "
        "AND recorded_at >= ? AND recorded_at <= ?",
        (window_start_iso, now_iso),
    ).fetchone()["n"]
    rate_cap = limits["paper.order_rate_cap"]
    if rate_count >= rate_cap:
        refuse("paper-limit-order-rate",
               f"{rate_count} orders in the last {PAPER_ORDER_RATE_WINDOW_SECONDS} seconds, at or "
               f"past the order rate cap {rate_cap:.0f}", rate_cap, rate_count)


def submit_paper_order(conn: sqlite3.Connection, paper_run_id: int, symbol: str, side: str,
                        intended_quantity: float, intended_notional: float,
                        price_per_share: float, venue, now_iso: str) -> dict:
    """Submit one order for one run, or refuse it loudly and record why.

    THREE SHORT COMMITTED STEPS (DECISIONS #322, night seven redesign; supersedes the single
    uninterrupted transaction every earlier round of this module used). Once every pre-submission
    refusal check below has passed: (1) an INTENT row is INSERTed, carrying a client_order_id this
    module mints itself, and committed ALONE, with a plain COMMIT and nothing reopened, in its own
    short step, BEFORE the venue is ever called; (2) the venue is called with that same id, with
    conn holding no open transaction at all, so nothing spans the call; (3) once the venue's
    response is validated, a fresh transaction is opened and the SAME row is UPDATED, matched on
    client_order_id, in its own short step, never a second INSERT. A crash at any point leaves a
    record that says which step was reached: refusal_reason set (REFUSED, step 1 for THIS order
    never ran); client_order_id set and every optional column still NULL (OPEN INTENT, step 1
    committed, step 3 never ran); or submitted_at and filled_at both set (FILLED, every step
    completed). On restart, reconcile_open_intents (below) asks the venue about every OPEN INTENT,
    by our own id, and either records the fill or releases the slice.

    A pre-submission refusal row is committed by _record_order_refusal before the refusal raises
    (DBG-6e), so it survives the caller's paper_transaction rollback; it also commits any earlier
    write the caller made in the same transaction.

    client_order_id is generated ONCE, before the first check below runs, so it is exactly as
    available to a pre-submission REFUSAL as it is to an intent or a fill: every row this function
    can possibly write carries one, which is what makes the three shapes above distinguishable by a
    column rather than by a convention (job file "what can break" #1).

    Check order (brief section 12's own listed order, widened by gate findings B-AC-16/18, then by
    round-2 gate findings B3 and B4, then by round 4's validation-first fix, unchanged by this
    redesign): the order's own three numbers (intended_quantity, intended_notional,
    price_per_share) are validated FIRST, before even the kill check (PaperOrderInputInvalid; see
    that exception's own docstring for why this one runs ahead of every other check). Once the
    numbers are known finite and correctly signed, a run that has ended (PaperRunEnded) and then a
    run whose Kill has begun (PaperRunKilling) refuse every new order outright; otherwise a BUY is fenced against this run's available buying
    power (PaperSliceFenceBreach), because that fence exists to stop capital LEAVING the slice and a
    buy is the only side that spends it -- WIDENED at night seven (AC-2b, DECISIONS #326): the
    committed_capital this fence tests now adds open_intents_notional (below), the intended_notional
    of every one of this run's own open intents (committed by an earlier order attempt but not yet
    at a terminal outcome), to paper_slice_current's own stored committed_capital, with NOTHING NEW
    STORED for it -- a stranded intent holds capacity until reconciliation releases it, which is the
    safe direction to fail; a SELL is fenced instead against submitted_quantity, the exact share
    count that will reach the venue (derived once, below, before either fence runs), tested against
    the quantity currently held long for this symbol on this run (PaperLongOnlyViolation). Gate
    finding B3: the sell fence used to test intended_quantity instead, a number paper_fills' own DDL
    comment calls independent of submitted_quantity, so a caller could clear the fence on
    intended_quantity while floor(intended_notional / price_per_share) still exceeded held; the
    fence and the venue now read the same variable, so that gap cannot reopen on its own. Otherwise
    whole-shares-only checks whether submitted_quantity is nonzero (PaperOrderNotExpressible).
    Every one of these six refusals writes a paper_fills row BEFORE raising (PaperOrderInputInvalid's
    own narrow non-finite exception aside; see its own docstring, and AC-9a below), so the
    strategy's intent is never lost even though the order never reached a venue.

    Step 3's own write still depends on a PROJECTION, not a hope (round 4 should-fix pair, preserved
    unchanged by this redesign). Once the venue's response is known well-formed (PaperVenueResponseInvalid,
    AC-6a, below), this projects whether writing that fill would leave a net_quantity this store's
    own average-cost arithmetic can represent (mirroring _recompute_slice_state's own invariant,
    using the same independent _held_quantity oracle the long-only fence above already reads). If
    the projection is SOUND, the UPDATE that lands the fill, the recompute and the paper_slice_current
    update all run inside the SAME transaction, opened fresh right after the venue's response was
    validated (see the code's own comment there), with no intermediate commit: nothing else can
    observe this run's derived state between the fill landing and its own effect on
    paper_slice_current, because the write lock this connection holds is never
    released until the whole call finishes (closing the round-4 race: a concurrent order on the SAME
    run reading paper_slice_current's realised_pnl and committed_capital before this fill's effect
    on them lands -- and AC-2b's own open_intents_notional widening closes the same race one step
    EARLIER again, since a concurrent buy's own fence check now sees this order's reservation the
    moment step 1 commits, well before step 3 ever runs). If the projection is UNSOUND (a venue that
    overfilled a sell past what is held), the SAME row is UPDATED with an ANOMALY shape instead:
    every column the venue actually reported is stored, but filled_at is deliberately left NULL, the
    exact test _recompute_slice_state already uses to decide what counts, so this run's history can
    never be poisoned by replaying the same anomaly on every future call (round-4, the other half).
    That row is committed via _commit_and_reopen before PaperNegativeNetQuantityInvariant is raised
    directly (matching press_kill's own use of the same primitive, for the DDIA chapter 8 reason
    this module's own top docstring quotes): the venue call is an external side effect this module
    cannot undo, so it must be durable before anything that raises can roll it back.

    PaperVenueResponseInvalid (AC-6a) is a THIRD outcome, neither a normal fill nor an anomaly:
    once the intent is committed the venue may already have executed, so a malformed response
    (a non-finite or wrongly-signed filled_quantity, fill_price or fees) is never recorded as a
    refusal and never written at all; the row is left exactly as step 1 committed it, this raises
    loudly, and reconcile_open_intents resolves the row later by this same client_order_id.
    """
    # Minted once, before the first check runs (see this function's own docstring): every row this
    # call could possibly write, an intent, a fill or a pre-flight refusal alike, carries this same
    # id, which is what DECISIONS #322/#326 call the idempotency key for the venue call below.
    client_order_id = str(uuid.uuid4())

    run_row = conn.execute(
        "SELECT kill_started_at, slice_opening_cash, ended_at, end_reason FROM paper_runs "
        "WHERE paper_run_id = ?",
        (paper_run_id,),
    ).fetchone()
    if run_row is None:
        raise PaperSliceStateMissing(
            f"paper_run_id={paper_run_id!r} does not exist in paper_runs; refusing rather than "
            "guessing a slice state for it"
        )

    # Validate the order's own three numbers before anything else touches them, including the kill
    # check just below (round 4 blocker; see PaperOrderInputInvalid's own docstring for the full
    # reasoning, including why this must run even before the kill check). _is_finite_signed (AC-0
    # root-cause dispatch, night seven) rejects NaN and both infinities in one call; the sign checks
    # then rule out a non-positive price_per_share (zero divides by zero; negative silently flips
    # submitted_quantity's own sign past both fences below) and a negative intended_notional or
    # intended_quantity (the same silent-sign-flip hazard from the other direction). This function
    # keeps its own PaperOrderInputInvalid, its own combined message below and its own
    # refusal-recording behaviour; only the predicate is now shared, so this is the one place in the
    # module besides _is_finite_signed itself that spells out what "finite and correctly signed"
    # means, rather than a fourth hand-inlined copy of the same three-line pattern.
    if not (
        _is_finite_signed(intended_quantity, allow_negative=False, allow_zero=True)
        and _is_finite_signed(intended_notional, allow_negative=False, allow_zero=True)
        and _is_finite_signed(price_per_share, allow_negative=False, allow_zero=False)
    ):
        # The two intended columns are REAL NOT NULL. Verified in the lead's own session (AC-0
        # root-cause dispatch, night seven, SQLite 3.46.1 through Python 3.14.4, an in-memory
        # database, not this backend's own interpreter): a bound NaN converts to NULL and the NOT
        # NULL constraint then refuses it, so a NaN cannot land in either column; a bound infinity
        # is NOT converted to anything and SQLite accepts and stores it as-is, even in a NOT NULL
        # column, so an infinity CAN land there with no constraint to catch it. This condition must
        # therefore test math.isfinite, covering NaN and both infinities, not math.isnan alone
        # (round 4's original check tested isnan only, which correctly skipped the write for NaN but
        # would have let a bare +/-inf intended_quantity or intended_notional through into the
        # column below; corrected here rather than left for a future round to find, since a
        # dispatch already covering this exact class was the one reading this line). price_per_share
        # is never itself a paper_fills column, so its own malformed value alone never bears on
        # whether this row can be written.
        if math.isfinite(intended_quantity) and math.isfinite(intended_notional):
            _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                                   intended_notional, client_order_id, now_iso,
                                   "PaperOrderInputInvalid")
        else:
            # AC-9a: intended_quantity or intended_notional is itself non-finite, so no row can be
            # honestly written above (a NOT NULL column cannot hold either truthfully). The raise
            # below is loud, so nothing is silent, but nothing would COUNT this refusal anywhere at
            # all without this line; this counter is that count, named for exactly this class.
            PAPER_MONITOR_COUNTERS["paper_unwritable_order_refusals"] += 1
        raise PaperOrderInputInvalid(
            f"paper_run_id={paper_run_id!r} symbol={symbol!r} side={side!r}: order inputs must all "
            "be finite and correctly signed (price_per_share > 0, intended_notional >= 0, "
            f"intended_quantity >= 0) but got intended_quantity={intended_quantity!r}, "
            f"intended_notional={intended_notional!r}, price_per_share={price_per_share!r}; "
            "refusing rather than computing floor(intended_notional / price_per_share) on them",
            paper_run_id, symbol, side, intended_quantity, intended_notional, price_per_share,
        )

    # A run that has ended takes no order, whatever its end_reason holds (PAP1a). Checked before the
    # kill mark: a run ended through STOP or APPROVE FOR REAL MONEY also has kill_started_at set, and
    # must be refused as ended, not as killing.
    if run_row["ended_at"] is not None:
        _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                               intended_notional, client_order_id, now_iso, "PaperRunEnded")
        raise PaperRunEnded(
            f"paper_run_id={paper_run_id!r}: this run ended at {run_row['ended_at']} with "
            f"end_reason={run_row['end_reason']!r}; an ended run takes no orders, refusing this one",
            paper_run_id, run_row["end_reason"],
        )

    # A run that is leaving paper does not open new positions (brief section 6.4). The refusal is
    # recorded like any other pre-submission refusal, so a position the strategy intended and did
    # not get is never dropped without a trace.
    if run_row["kill_started_at"] is not None:
        _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                               intended_notional, client_order_id, now_iso, "PaperRunKilling")
        raise PaperRunKilling(
            f"paper_run_id={paper_run_id!r}: Kill has begun (kill_started_at is set); refusing "
            "this order rather than holding or submitting it",
            paper_run_id,
        )

    # The paper limits (UI19-1b): any limit with no value blocks every order, then the six checks.
    # After the kill refusal and before the slice fences, so a refusal here is recorded like every
    # other pre-submission refusal and the venue is never reached.
    _enforce_paper_limits(conn, paper_run_id, symbol, side, intended_quantity, intended_notional,
                          price_per_share, client_order_id, now_iso)

    slice_row = conn.execute(
        "SELECT realised_pnl, committed_capital FROM paper_slice_current WHERE paper_run_id = ?",
        (paper_run_id,),
    ).fetchone()
    if slice_row is None:
        raise PaperSliceStateMissing(
            f"paper_run_id={paper_run_id!r} has a paper_runs row but no paper_slice_current row: "
            "a corruption (brief section 11, E3's NULL/absent answer); refusing rather than "
            "computing a default"
        )

    # submitted_quantity is derived ONCE, here, before either fence, so the sell fence and the
    # venue call below read the exact same number (gate finding B3). This is a pure computation,
    # not a check: nothing raises here, and the check order (after round 4's own validation-first
    # step above) stays kill, then fence, then whole-shares, exactly as this function's own
    # docstring documents. It is also safe from the three hazards PaperOrderInputInvalid closes:
    # price_per_share is already known finite and strictly positive by this point. floor(
    # intended_notional / price_per_share) == 0 (the P > N cliff) is still refused below, after the
    # fence, not here.
    submitted_quantity = float(math.floor(intended_notional / price_per_share))

    # The fence has two sides, one rule (gate finding B-AC-16/18). side is constrained to exactly
    # "buy" or "sell" by paper_fills's own CHECK constraint, so "not buy" means "sell" here.
    if side == "buy":
        # A BUY is fenced by available buying power (brief section 9.2): the fence exists to stop
        # capital LEAVING the slice, and a buy is the only side that spends it. Tested against
        # intended_notional, not submitted_quantity * price_per_share, on purpose: floor() only
        # rounds down, so the actual notional spent can never exceed intended_notional, which makes
        # this the STRICTER of the two numbers and the fence safe in the direction that matters.
        # Testing the smaller, post-floor figure would let a request over the fence quietly benefit
        # from the same rounding "never resized down to fit" (section 12) forbids doing on purpose.
        # AC-2b (night seven, DECISIONS #326): committed_capital alone only counts capital already
        # tied up in a FILLED position; it says nothing about an order this same run already
        # committed an intent for but has not yet heard back on. open_intents_notional (below) adds
        # exactly that, read fresh from paper_fills every call, nothing cached and nothing stored.
        # An order whose notional exceeds available buying power is refused, recorded, and never
        # quietly resized down to fit.
        available = compute_available_buying_power(
            run_row["slice_opening_cash"], slice_row["realised_pnl"],
            slice_row["committed_capital"] + open_intents_notional(conn, paper_run_id),
        )
        if intended_notional > available:
            _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                                   intended_notional, client_order_id, now_iso,
                                   "PaperSliceFenceBreach")
            raise PaperSliceFenceBreach(
                f"paper_run_id={paper_run_id!r}: requested notional {intended_notional:.4f} exceeds "
                f"available buying power {available:.4f}",
                paper_run_id, available, intended_notional,
            )
    else:
        # A SELL is fenced instead by the quantity currently held long for this symbol on this run
        # (long only is Tamer's ruled policy): a sell returns capital rather than spending it, so
        # the buying-power fence above is never the right check for it. Tested against
        # submitted_quantity, not intended_quantity (gate finding B3): intended_quantity,
        # intended_notional and price_per_share are three independent arguments (paper_fills' own
        # DDL comment), so nothing before this fold tied the number the fence tested to the number
        # that reached the venue, and a triple where intended_quantity cleared the fence while
        # floor(intended_notional / price_per_share) exceeded held could send a real short. Selling
        # up to and including the held quantity is a legitimate exit and always succeeds here,
        # whatever available buying power reads, even exactly zero; selling more would open a
        # short, which this module builds no path for at all, so it is refused, recorded, and never
        # resized down to fit.
        held = _held_quantity(conn, paper_run_id, symbol)
        if submitted_quantity > held:
            _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                                   intended_notional, client_order_id, now_iso,
                                   "PaperLongOnlyViolation")
            raise PaperLongOnlyViolation(
                f"paper_run_id={paper_run_id!r} symbol={symbol!r}: submitted sell quantity "
                f"{submitted_quantity:.4f} (floor of intended_notional={intended_notional:.4f} / "
                f"price_per_share={price_per_share:.4f}; intended_quantity was "
                f"{intended_quantity:.4f}) exceeds the {held:.4f} currently held long; long only "
                "is ruled policy and this module builds no short path",
                paper_run_id, symbol, submitted_quantity, held,
            )

    # Whole shares only (brief section 4.3): the P > N cliff. submitted_quantity == 0 means the
    # sizer's intended position cannot be expressed at any size at this price, and that is refused
    # rather than skipped in silence or rounded up to one share.
    if submitted_quantity == 0.0:
        _record_order_refusal(conn, paper_run_id, symbol, side, intended_quantity,
                               intended_notional, client_order_id, now_iso,
                               "PaperOrderNotExpressible")
        raise PaperOrderNotExpressible(
            f"paper_run_id={paper_run_id!r}: floor(intended_notional={intended_notional:.4f} / "
            f"price_per_share={price_per_share:.4f}) == 0, the P > N cliff; refusing rather than "
            "skipping or rounding up",
            paper_run_id, intended_notional, price_per_share,
        )

    # STEP 1 (DECISIONS #322): the intent, and only the intent, INSERTed and committed in its own
    # short step, durably, BEFORE the venue is ever called. Every optional column is NULL: nothing
    # about this order is known yet except that it passed every one of this run's own refusal
    # checks. This plain COMMIT's own effect is what a second, independent connection can actually
    # see (AC-2's own probe proves this for real, never by assertion). Deliberately a bare COMMIT
    # and NOT _commit_and_reopen: reopening here would immediately re-take SQLite's own RESERVED
    # write lock (that is what BEGIN IMMEDIATE means, before a single byte is written) and hold it
    # for the whole venue round trip next, which is exactly the shape DECISIONS #322 point 5 rules
    # out ("without any transaction spanning the venue call") and a probe now proves directly: a
    # second connection's own BEGIN IMMEDIATE would be refused with "database is locked" for as
    # long as the venue call takes. conn is left with NOTHING open from here until a fresh
    # BEGIN IMMEDIATE below, once the venue has answered and that answer is known well-formed.
    conn.execute(
        "INSERT INTO paper_fills (paper_run_id, symbol, side, intended_quantity, intended_notional, "
        " submitted_quantity, filled_quantity, fill_price, fees, venue_order_id, client_order_id, "
        " recorded_at, submitted_at, filled_at, refusal_reason) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL)",
        (paper_run_id, symbol, side, intended_quantity, intended_notional, client_order_id, now_iso),
    )
    conn.execute("COMMIT;")

    # STEP 2 (DECISIONS #322): the venue is called with our own id, which is what makes a second
    # call for the SAME intent (a reconciliation pass that misreads a lookup) idempotent at the
    # venue rather than a duplicate order -- DDIA chapter 8's own "or use idempotency", quoted at
    # this module's own top docstring.
    venue_result = venue.submit_order(symbol, side, submitted_quantity, price_per_share,
                                       client_order_id=client_order_id)

    # AC-6a (night seven): the venue's own answer is validated with the SAME predicate this
    # function already applies to the caller's own order inputs, applied here to filled_quantity,
    # fill_price and fees. A malformed answer writes NOTHING: the intent row committed in step 1 is
    # left exactly as it stood, because the venue may already have executed and a refusal row here
    # would be a lie about a trade that happened. reconcile_open_intents (below) is what resolves
    # this row later, by this same client_order_id, once the venue can be asked again.
    if not (
        _is_finite_signed(venue_result["filled_quantity"], allow_negative=False, allow_zero=True)
        and _is_finite_signed(venue_result["fill_price"], allow_negative=False, allow_zero=False)
        and _is_finite_signed(venue_result["fees"], allow_negative=False, allow_zero=True)
    ):
        raise PaperVenueResponseInvalid(
            f"paper_run_id={paper_run_id!r} symbol={symbol!r} client_order_id={client_order_id!r}: "
            "the venue's own response must be finite and correctly signed (fill_price > 0, "
            "filled_quantity >= 0, fees >= 0) but got "
            f"filled_quantity={venue_result['filled_quantity']!r}, "
            f"fill_price={venue_result['fill_price']!r}, fees={venue_result['fees']!r}; leaving the "
            "already-committed intent open rather than recording a refusal that may not be true",
            paper_run_id, symbol, client_order_id, venue_result,
        )

    # STEP 3 begins here (DECISIONS #322): the venue's response is now known well-formed, so a
    # fresh transaction is opened, only now, to hold the projection read and every write step 3
    # makes below in one uninterrupted whole (round 4 should-fix pair, preserved: see the
    # projection's own comment just below for why that still holds after this fold). Nothing was
    # open on conn from step 1's own plain COMMIT above until this line: the venue call and its
    # validation both ran, and could both have raised, with no transaction for either to leave
    # dangling (paper_transaction's own except block, DECISIONS #322 point 5 fold, is what makes
    # that safe regardless of which of the two raises).
    conn.execute("BEGIN IMMEDIATE;")

    # Project whether writing this fill would leave a net_quantity this store's own average-cost
    # arithmetic can represent, BEFORE writing anything (round 4 should-fix pair). Only a sell can
    # ever drive net_quantity negative (_recompute_slice_state's own invariant guard only ever fires
    # in its sell branch; a buy is purely additive), so a buy's projection is sound by construction.
    # A sell's projection reuses _held_quantity, the same independent oracle the long-only fence
    # above already reads, called again here rather than reusing that fence's own `held` local: the
    # two answer different questions (would the REQUEST fit, versus would the ACTUAL fill fit), and
    # this module's own established discipline is to keep such oracles independently re-derived
    # rather than lean on a value computed for a different purpose. Deliberately re-read fresh here,
    # inside the NEW transaction opened right after the venue answered, rather than assumed
    # unchanged since the fence's own earlier read: night seven's fix (DECISIONS #322 point 5)
    # leaves no transaction open across the venue call in between, so a concurrent order on this
    # same run and symbol could genuinely have filled in that gap and changed what is held; reading
    # fresh is what lets this projection see that truthfully, and if it makes the projection
    # UNSOUND, the anomaly path below (unchanged since round 4, gate finding B4) is what already
    # handles a venue call that landed more than this run can locally absorb, whatever the cause.
    # The venue's OWN reported filled_quantity is what is projected, never
    # submitted_quantity, because filled_quantity is the number that actually lands in paper_fills
    # and is therefore the number _recompute_slice_state will actually read back.
    if side == "sell":
        prior_held = _held_quantity(conn, paper_run_id, symbol)
        projected_net_quantity = prior_held - venue_result["filled_quantity"]
        projection_sound = projected_net_quantity >= 0.0
    else:
        projected_net_quantity = None
        projection_sound = True

    if projection_sound:
        # STEP 3, the common path (round 4 should-fix pair, preserved unchanged by this redesign):
        # the projection above already proves _recompute_slice_state cannot raise for this fill, so
        # the UPDATE that lands the fill, the recompute and the paper_slice_current update all run
        # inside the SAME open transaction with no intermediate commit: the race an intermediate
        # commit here would open (a concurrent order on this same run reading paper_slice_current
        # before this fill's effect on it lands) has no window to land in, because the write lock
        # this connection holds is never released until this whole call finishes (DDIA chapter 8: a
        # multi-object transaction is exactly what "denormalized data kept in sync," Figure 8-2's
        # own example, needs). This UPDATEs the SAME row step 1 committed, matched on
        # client_order_id, never a second INSERT (AC-2's own probe: one row per order attempt).
        conn.execute(
            "UPDATE paper_fills SET submitted_quantity = ?, filled_quantity = ?, fill_price = ?, "
            "fees = ?, venue_order_id = ?, submitted_at = ?, filled_at = ? WHERE client_order_id = ?",
            (
                submitted_quantity, venue_result["filled_quantity"], venue_result["fill_price"],
                venue_result["fees"], venue_result["venue_order_id"], now_iso, now_iso,
                client_order_id,
            ),
        )
        new_state = _recompute_slice_state(conn, paper_run_id)
        conn.execute(
            "UPDATE paper_slice_current SET cash = ?, position_value = ?, equity = ?, "
            "realised_pnl = ?, committed_capital = ?, updated_at = ? WHERE paper_run_id = ?",
            (
                new_state["cash"], new_state["position_value"], new_state["equity"],
                new_state["realised_pnl"], new_state["committed_capital"], now_iso, paper_run_id,
            ),
        )
    else:
        # The anomaly path (round 4 should-fix pair, preserved unchanged by this redesign): the
        # venue reported filling more of this sell than this run currently holds, so writing a
        # normal fill would make _recompute_slice_state raise PaperNegativeNetQuantityInvariant on
        # this run forever after (every later call replays the same paper_fills history from
        # scratch and hits the same row again). Instead this UPDATEs the SAME row (step 1's own
        # intent, matched on client_order_id) leaving filled_at = NULL on purpose, the same value
        # that already means "not counted in _recompute_slice_state's WHERE filled_at IS NOT NULL
        # query" for a working or refused order (see paper_fills' own DDL comment above), so this
        # run's history is never poisoned: every future recompute simply never sees this row. Every
        # OTHER column the venue actually reported is still written (submitted_quantity,
        # filled_quantity, fill_price, fees, venue_order_id, submitted_at), because the venue call
        # truly happened and this row is its durable trace (gate finding B-AC-20); only filled_at is
        # deliberately withheld, and refusal_reason is set so a reader can tell this row apart from
        # an ordinary open intent. Committed via _commit_and_reopen, the same primitive press_kill
        # and the previous round's B4 fix already use, before raising, for the identical DDIA
        # chapter 8 reason: the venue call is an external side effect this module cannot undo, so
        # it must be durable before anything that raises can roll it back.
        conn.execute(
            "UPDATE paper_fills SET submitted_quantity = ?, filled_quantity = ?, fill_price = ?, "
            "fees = ?, venue_order_id = ?, submitted_at = ?, refusal_reason = ? "
            "WHERE client_order_id = ?",
            (
                submitted_quantity, venue_result["filled_quantity"], venue_result["fill_price"],
                venue_result["fees"], venue_result["venue_order_id"], now_iso,
                "PaperNegativeNetQuantityInvariant", client_order_id,
            ),
        )
        _commit_and_reopen(conn)
        raise PaperNegativeNetQuantityInvariant(
            f"paper_run_id={paper_run_id!r} symbol={symbol!r}: the venue reported filling "
            f"{venue_result['filled_quantity']:.4f}, which would take net_quantity to "
            f"{projected_net_quantity:.4f}, negative and undefined for this store's average-cost "
            "arithmetic; the fill is recorded with filled_at left NULL so it is excluded from every "
            "future recompute rather than poisoning this run's derived state permanently",
            paper_run_id, symbol, projected_net_quantity, venue_result["filled_quantity"],
        )

    return {
        "filled_quantity": venue_result["filled_quantity"],
        "fill_price": venue_result["fill_price"],
        "fees": venue_result["fees"],
        "venue_order_id": venue_result["venue_order_id"],
    }


# =============================================================================
# Idempotency and reconciliation (DECISIONS #322, night seven). open_intents_notional feeds AC-2b's
# own widened buy fence above; reconcile_open_intents is what a restarting node calls to ask the
# venue, by our own id, about every intent that never reached a terminal outcome, and either records
# the fill or releases the slice. stranded_intents is the read-only gauge job file "what can break"
# #1 names: a count of intents open for longer than one reconciliation cycle ought to allow.
# =============================================================================

def open_intents_notional(conn: sqlite3.Connection, paper_run_id: int) -> float:
    """The intended_notional of every OPEN intent (committed, no terminal outcome yet) for one run
    (AC-2b, AC-9 "what can break" #3 gauge half): SUM(intended_notional) over this run's own
    paper_fills rows with filled_at IS NULL AND refusal_reason IS NULL, 0.0 when there are none.
    Nothing is cached: this is read fresh from the rows that already exist on every call, the same
    query-not-storage shape list_running_runs and list_ended_runs below already use.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(intended_notional), 0.0) AS total FROM paper_fills "
        "WHERE paper_run_id = ? AND filled_at IS NULL AND refusal_reason IS NULL",
        (paper_run_id,),
    ).fetchone()
    return row["total"]


def open_intents_notional_for_symbol(conn: sqlite3.Connection, paper_run_id: int,
                                     symbol: str) -> float:
    """The signed sum (buy +, sell -) of intended_notional over one run's open intents (filled_at
    IS NULL AND refusal_reason IS NULL) for one symbol, 0.0 when there are none (UI19-1b). The
    paper net caps count these as exposure that is on its way to the venue."""
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side = 'buy' THEN intended_notional "
        "ELSE -intended_notional END), 0.0) AS total FROM paper_fills "
        "WHERE paper_run_id = ? AND symbol = ? AND filled_at IS NULL AND refusal_reason IS NULL",
        (paper_run_id, symbol),
    ).fetchone()
    return float(row["total"])


def stranded_intents(conn: sqlite3.Connection, older_than_iso: str) -> list:
    """Every open intent (filled_at IS NULL AND refusal_reason IS NULL) system-wide, across every
    run, recorded before older_than_iso, oldest first (job file "what can break" #1's own gauge: an
    intent open for longer than one reconciliation cycle ought to allow). A gauge, not a stored
    counter, computed on demand like the two list_*_runs queries below.
    """
    return conn.execute(
        "SELECT * FROM paper_fills WHERE filled_at IS NULL AND refusal_reason IS NULL "
        "AND recorded_at < ? ORDER BY recorded_at ASC",
        (older_than_iso,),
    ).fetchall()


def reconcile_open_intents(conn: sqlite3.Connection, venue, now_iso: str) -> dict:
    """Ask the venue, BY OUR OWN client_order_id, about every open intent system-wide, and either
    record the fill or release the slice (AC-4, AC-5, DECISIONS #322).

    NEVER calls venue.submit_order: only venue.lookup_order, which is what makes this idempotent BY
    CONSTRUCTION rather than by a counter that would need a fault injected to ever move (job file
    "what can break" #2, the venue called twice for one intent) -- an absence proof, not a runtime
    signal, and it is one this function's own shape enforces rather than a discipline a caller must
    remember. A lookup answering None means the venue never received it: the row's own
    refusal_reason is set to "PaperIntentReleasedAtReconciliation" (submitted_at and filled_at both
    stay NULL, so this shape stays distinct from every other one paper_fills can hold), the release
    counter moves (job file "what can break" #3, counter half), and the slice this intent had
    reserved becomes available again the moment open_intents_notional is next read. A well-formed
    answer validates with the SAME predicate submit_paper_order's own AC-6a check uses and, once
    good, UPDATEs the SAME row (submitted_quantity is left NULL: reconciliation genuinely does not
    know what this process asked the venue for before it crashed, and this table's own discipline is
    never to fabricate a quantity it cannot truthfully represent, the same discipline
    PaperOrderInputInvalid's own docstring already states for a different column), recomputes and
    updates paper_slice_current, the SAME write shape submit_paper_order's own sound path uses. A
    malformed answer is left OPEN for the next pass, no exception raised: one bad row must never
    abort a sweep over many other rows that might resolve cleanly.
    """
    open_rows = conn.execute(
        "SELECT * FROM paper_fills WHERE filled_at IS NULL AND refusal_reason IS NULL "
        "ORDER BY recorded_at ASC"
    ).fetchall()

    resolved_filled = 0
    released = 0
    still_open = 0

    for row in open_rows:
        answer = venue.lookup_order(row["client_order_id"])

        if answer is None:
            # The venue never received it (or has forgotten it): release the reservation rather
            # than leave it stranded forever. refusal_reason distinguishes this from an ordinary
            # pre-submission refusal (which never reaches this query at all, since filled_at and
            # refusal_reason IS NULL is exactly the open-intent shape this query selects on).
            conn.execute(
                "UPDATE paper_fills SET refusal_reason = ? WHERE fill_id = ?",
                ("PaperIntentReleasedAtReconciliation", row["fill_id"]),
            )
            PAPER_MONITOR_COUNTERS["paper_reconciliation_releases"] += 1
            released += 1
            continue

        # The same finite-and-correctly-signed predicate submit_paper_order's own AC-6a check uses,
        # applied to the venue's lookup answer instead of its submit answer. A malformed answer is
        # left OPEN for the next pass rather than raised: this sweep must not abort over one bad row.
        if not (
            _is_finite_signed(answer.get("filled_quantity"), allow_negative=False, allow_zero=True)
            and _is_finite_signed(answer.get("fill_price"), allow_negative=False, allow_zero=False)
            and _is_finite_signed(answer.get("fees"), allow_negative=False, allow_zero=True)
        ):
            still_open += 1
            continue

        # A well-formed answer: the same write shape submit_paper_order's own sound path uses,
        # UPDATE then recompute then paper_slice_current update, all in this one call's own
        # transaction. submitted_quantity is deliberately left untouched (still NULL): this process
        # was never told what the crashed process asked the venue for, and fabricating it from
        # intended_notional would need a price_per_share this table never stores at all (paper_fills'
        # own DDL comment: "price_per_share is never itself a paper_fills column").
        conn.execute(
            "UPDATE paper_fills SET filled_quantity = ?, fill_price = ?, fees = ?, "
            "venue_order_id = ?, filled_at = ? WHERE fill_id = ?",
            (
                answer["filled_quantity"], answer["fill_price"], answer["fees"],
                answer["venue_order_id"], now_iso, row["fill_id"],
            ),
        )
        new_state = _recompute_slice_state(conn, row["paper_run_id"])
        conn.execute(
            "UPDATE paper_slice_current SET cash = ?, position_value = ?, equity = ?, "
            "realised_pnl = ?, committed_capital = ?, updated_at = ? WHERE paper_run_id = ?",
            (
                new_state["cash"], new_state["position_value"], new_state["equity"],
                new_state["realised_pnl"], new_state["committed_capital"], now_iso,
                row["paper_run_id"],
            ),
        )
        resolved_filled += 1

    return {"resolved_filled": resolved_filled, "released": released, "still_open": still_open}


# =============================================================================
# The sizing wrapper (AC-8, DECISIONS #323, paper brief section 9.4 Amendment 2). The ONLY place in
# this module a position size is computed: everything else here takes intended_quantity and
# intended_notional as given, already sized by the caller. This function IS that caller for the
# paper order path, so no second, hand-written size expression can ever grow anywhere else in it.
# =============================================================================

def size_and_submit_paper_order(conn: sqlite3.Connection, paper_run_id: int, symbol: str, side: str,
                                 close, high, low, open_, spec, equity: float,
                                 price_per_share: float, venue, now_iso: str,
                                 fee_rate: float = 0.0) -> dict:
    """Size one order with the shared rule, then submit it, in one call.

    close/high/low/open_ (brief Amendment 2, section 9.4): the run's own recent bars, ending at the
    bar that just closed and fired the entry signal. This wrapper places the ONE True entry at the
    SECOND-TO-LAST bar of that series, never the last: research_lab.strategies.helpers.sizing's own
    compute_position_sizes fills an entry at the NEXT bar's own open (its own docstring, derivation
    3.4's L14), so an entry on a series' own last bar has no next bar to fill against and always
    raises UndefinedVolatilityError, whatever the series' own length -- this is that function's own
    fixed contract, not a choice made here. A live paper node's own "decide right now" shape is
    exactly this: the bar that just closed and fired the signal is second-to-last, and the bar
    currently forming (whose live open is price_per_share, supplied separately below, never read
    off open_ itself) is last. equity is the slice's own equity at that moment (brief Amendment 2:
    "the slice's equity at that moment"), read by the caller moments before this call; this module
    holds no second, cached equity value anywhere for this wrapper to read instead.

    Calls compute_position_sizes exactly once (AC-8's own absence proof: no second, hand-written
    size expression anywhere in this module). Exactly one True entry means exactly one non-NaN value
    in the returned shares/stop_price series (compute_position_sizes' own docstring: both are
    "defined ONLY at entry bars"); dropna().iloc[-1] reads that one value regardless of its own
    integer position, rather than hard-coding which position that is. intended_notional is then
    derived from intended_quantity and price_per_share, and both are handed to submit_paper_order
    UNCHANGED, which does no arithmetic of its own on either: this function computes a size, that
    function submits an order, and the boundary between the two stays exactly that sharp.

    Any SizingError subclass (research_lab.strategies.helpers.sizing's own refusal family: undefined
    volatility, a cap-limited or unaffordable or zero-share answer) propagates unchanged: no order
    was ever formed, so this records no paper_fills row of its own for a sizing refusal.
    """
    # Lazy imports, matching sizing.py's own stated convention ("pandas itself is imported lazily
    # inside each function rather than at module load"): a caller of paperdb.py that never sizes an
    # order never pays the pandas / research_lab import cost just for loading this module.
    import pandas as pd
    from research_lab.strategies.helpers import sizing

    # One boolean entry flag, True on the second-to-last bar only; see this function's own
    # docstring for why not the last. Everywhere else in the series stays False.
    entries = pd.Series(False, index=close.index)
    entries.iloc[-2] = True

    result = sizing.compute_position_sizes(
        close, high, low, open_, entries, spec, equity=equity, fee_rate=fee_rate,
        session_day=sizing.session_days(close.index),  # lane P2d (#323): the same daily-volatility rule as a backtest
    )
    # dropna() isolates the one entry's own value (every other position is NaN by construction,
    # per compute_position_sizes' own docstring), so this reads correctly regardless of exactly
    # which integer position the entry landed at.
    intended_quantity = float(result.shares.dropna().iloc[-1])
    intended_notional = intended_quantity * price_per_share

    return submit_paper_order(
        conn, paper_run_id, symbol, side, intended_quantity, intended_notional, price_per_share,
        venue, now_iso,
    )


def mark_position_price(conn: sqlite3.Connection, paper_run_id: int, symbol: str,
                         new_price: float, as_of_iso: str) -> None:
    """Re-mark one symbol's price and recompute paper_slice_current.position_value and equity in
    the same write (brief section 10.4, B-AC-15). cash, realised_pnl and committed_capital are
    unaffected by a mark (no fill occurred), so recomputing the full state and writing all five
    columns is a no-op for those three and a real update for position_value and equity; this is
    the SAME recompute submit_paper_order uses, so there is no second expression for equity
    anywhere in this module.

    new_price is validated first (AC-0 root-cause dispatch, night seven: this was the named
    instance, a live-feed price with no finite or sign check at all, reaching
    paper_slice_current.position_value and .equity, both REAL NOT NULL, through
    _recompute_slice_state's own price_overrides). Must be finite and strictly positive, the same
    rule submit_paper_order already applies to price_per_share, before that value can be folded
    into position_value and equity for any symbol this run has ever traded.
    """
    _require_finite_signed("new_price", new_price, allow_negative=False, allow_zero=False)
    state = _recompute_slice_state(conn, paper_run_id, price_overrides={symbol: new_price})
    conn.execute(
        "UPDATE paper_slice_current SET cash = ?, position_value = ?, equity = ?, "
        "realised_pnl = ?, committed_capital = ?, updated_at = ? WHERE paper_run_id = ?",
        (
            state["cash"], state["position_value"], state["equity"], state["realised_pnl"],
            state["committed_capital"], as_of_iso, paper_run_id,
        ),
    )


# =============================================================================
# Kill (brief section 6.4; B-AC-7). Each step's write is committed durably before the next step's
# venue call runs (DECISIONS #322 point 5, night seven fold): steps 1 and 2 below COMMIT alone and
# leave nothing open across steps 2 and 3's own venue calls, reopening only once the venue has
# answered, the same shape submit_paper_order's own step 1 now uses. Steps 3, 4 and 5, where no
# venue call runs before the next write, still use _commit_and_reopen (defined above, in the
# connection/transaction helpers section, where submit_paper_order's own anomaly-path caller also
# lives). Either way a process that dies mid-Kill leaves a resumable record rather than a rollback
# that would silently un-happen a step the venue already carried out.
# =============================================================================

def kill_step5_return_slice(conn: sqlite3.Connection, paper_run_id: int, now_iso: str,
                             on_release=None) -> bool:
    """The one conditional UPDATE from brief section 6.4, verbatim: tests kill_step and advances it
    to 'slice_returned' in the same statement, so two callers racing this step can never both credit
    the pot. Deliberately no explicit BEGIN/COMMIT of its own: called both from inside press_kill's
    own ambient transaction and directly on a bare connection (a concurrent-press probe), and a bare
    single UPDATE auto-commits as its own atomic unit on this module's isolation_level=None
    connections either way; SQLite still serialises the two concurrent writers at the file level, so
    the loser's UPDATE matches zero rows once the winner's has already committed the new kill_step.

    Returns True and calls on_release() exactly once when this caller's UPDATE changed exactly one
    row (this caller owns the step, and the slice has just moved to the pot); returns False and
    calls nothing when it changed zero rows (another caller already did it).
    """
    cursor = conn.execute(
        "UPDATE paper_runs SET kill_step = 'slice_returned', ended_at = ?, end_reason = 'killed' "
        "WHERE paper_run_id = ? AND kill_step = 'fills_written' AND ended_at IS NULL",
        (now_iso, paper_run_id),
    )
    owned = cursor.rowcount == 1
    if owned and on_release is not None:
        on_release()
    return owned


def _close_out(conn: sqlite3.Connection, paper_run_id: int, venue, now_iso: str,
               kill_started_at, kill_step) -> str:
    """STOP's steps 1 to 4, moved unchanged from press_kill (lane KO1); press_kill's docstring describes them."""
    # STEP 1: mark. Only when this run has never had Kill pressed before (kill_started_at is NULL).
    if kill_started_at is None:
        conn.execute(
            "UPDATE paper_runs SET kill_started_at = ?, kill_step = 'marked' "
            "WHERE paper_run_id = ? AND kill_started_at IS NULL",
            (now_iso, paper_run_id),
        )
        # Commit alone, durably, and leave nothing open across step 2's own venue call next
        # (DECISIONS #322 point 5, the same shape submit_paper_order's own step 1 now uses): this
        # step's write must survive a crash, but no transaction may span the venue call that
        # follows. Reopened only inside step 2's own block below, once cancel_all_orders returns.
        conn.execute("COMMIT;")
        kill_step = "marked"

    # STEP 2: cancel every open order for this run.
    if kill_step in (None, "marked"):
        # Whatever is open on conn right now must be closed before the venue is called, never
        # merely assumed closed (DECISIONS #322 point 5): a call that ran step 1 just above already
        # committed alone, so this is a no-op; a call RESUMED directly at step 2 (kill_step was
        # already 'marked' on entry) skipped step 1 entirely, so its own paper_transaction's
        # ambient transaction is still open here, and this is what actually closes it.
        _commit_if_open(conn)
        venue.cancel_all_orders(paper_run_id)
        # _commit_if_open just above guarantees nothing was left open, so this always starts a
        # genuinely fresh transaction, before this step's own write (mirrors submit_paper_order's
        # own step 3 reopen, same DECISIONS #322 point 5 reason).
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            "UPDATE paper_runs SET kill_step = 'orders_cancelled' WHERE paper_run_id = ?",
            (paper_run_id,),
        )
        # Commit alone again: step 3's own venue call (close_all_positions) is next, and it must
        # not run with a transaction open either, for the same reason step 1's commit above does.
        conn.execute("COMMIT;")
        kill_step = "orders_cancelled"

    # STEP 3: close every open position for this run.
    if kill_step == "orders_cancelled":
        # Same guard as step 2's own, and for the identical reason: a call resumed exactly here
        # (kill_step already 'orders_cancelled' on entry) never ran steps 1 or 2 in THIS call, so
        # its own paper_transaction's ambient transaction is still open, untouched, at this point.
        _commit_if_open(conn)
        venue.close_all_positions(paper_run_id)
        # Reopen only now that the venue has answered (same reason as step 2's own reopen above).
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            "UPDATE paper_runs SET kill_step = 'positions_closed' WHERE paper_run_id = ?",
            (paper_run_id,),
        )
        # No venue call follows before step 4's own write (step 4's own docstring: "the state
        # transition alone"), so _commit_and_reopen (commit this step, reopen for the next one) is
        # safe to use again here, same as steps 4 and 5 below.
        _commit_and_reopen(conn)
        kill_step = "positions_closed"

    # STEP 4: write only unrecorded terminal fills (matched on venue_order_id). See press_kill's
    # docstring for why this lane's own scope makes this the transition alone.
    if kill_step == "positions_closed":
        conn.execute(
            "UPDATE paper_runs SET kill_step = 'fills_written' WHERE paper_run_id = ?",
            (paper_run_id,),
        )
        _commit_and_reopen(conn)
        kill_step = "fills_written"
    return kill_step


def press_kill(conn: sqlite3.Connection, paper_run_id: int, venue, now_iso: str) -> None:
    """Run Kill's sequence to completion, resuming from kill_step rather than restarting at step 1.

    Step 1 (mark) is a conditional UPDATE that only fires when kill_started_at is currently NULL,
    so a resumed press never repeats it and never rewrites when the Kill began. Steps 2 and 3 call
    the venue BEFORE writing their own completion, and (night seven, DECISIONS #322 point 5 fold)
    with NOTHING open on conn while that call is in flight: the step before commits alone and this
    step reopens only once the venue has answered, mirroring submit_paper_order's own step 1 to
    step 2 shape. If a venue call raises, this function lets that exception propagate unchanged
    (never swallowed, never wrapped); every prior step is already durably committed by that point,
    and there is nothing open here for that raise to leave dangling, since the transaction this
    step would have reopened never got the chance to start (paper_transaction's own except block
    checks conn.in_transaction before attempting a ROLLBACK, so a raise reaching it with nothing
    open there is correctly a no-op, never an error masking the venue's own exception). Steps 2
    and 3 are therefore only safe to resume because the venue interface's own contract makes them
    no-ops on an already-clean state (brief section 6.4); this module's StubVenue satisfies that by
    construction. Step 4 writes only unrecorded terminal fills; this lane builds no live venue
    adapter capable of reporting fills the process missed while it was down (no live node process
    is in this lane's scope), so with no working order ever left open in this module's own fixtures,
    this step is the state transition alone rather than a guess at an untested venue method. Step 5
    is kill_step5_return_slice above, called with no on_release callback here, because a
    single-threaded press_kill call has nobody else to notify.
    """
    row = conn.execute(
        "SELECT kill_started_at, kill_step FROM paper_runs WHERE paper_run_id = ?",
        (paper_run_id,),
    ).fetchone()
    if row is None:
        raise PaperSliceStateMissing(
            f"paper_run_id={paper_run_id!r} does not exist in paper_runs; cannot press Kill on it"
        )

    kill_started_at = row["kill_started_at"]
    kill_step = row["kill_step"]

    kill_step = _close_out(conn, paper_run_id, venue, now_iso, kill_started_at, kill_step)

    # STEP 5: return the slice to the pot. The conditional UPDATE inside kill_step5_return_slice is
    # the one statement that tests and advances in the same breath, so this call can never credit
    # the pot twice even under a concurrent second caller.
    if kill_step == "fills_written":
        kill_step5_return_slice(conn, paper_run_id, now_iso)
        _commit_and_reopen(conn)


# =============================================================================
# Promote (brief section 6.5, 10.5; B-AC-8). Writes a paper_promotions row in the first word of the
# state enum and nothing else; the real-capital path's own recorded-go step, owned by a different
# unit entirely, is the only writer that may ever set the state to its second word.
# =============================================================================

def promote(conn: sqlite3.Connection, paper_run_id: int, requested_at: str) -> int:
    """Write a promotion request for one run, or refuse it when that run's Kill has begun.

    While kill_started_at is set and ended_at is still NULL the run is being closed, and allowing a
    promotion request in that window would point the real-capital path at a run that no longer
    exists (brief section 6.5); this raises PaperPromoteWhileKilling and writes NO row at all,
    which is a CONTROL refusal (nothing was ordered, so there is nothing to store in paper_fills),
    unlike the five ORDER refusals submit_paper_order records above.
    """
    run_row = conn.execute(
        "SELECT kill_started_at, ended_at FROM paper_runs WHERE paper_run_id = ?",
        (paper_run_id,),
    ).fetchone()
    if run_row is not None and run_row["kill_started_at"] is not None and run_row["ended_at"] is None:
        raise PaperPromoteWhileKilling(
            f"paper_run_id={paper_run_id!r}: Kill has begun and has not finished; refusing to "
            "write a promotion request for a run that is being closed"
        )

    cursor = conn.execute(
        "INSERT INTO paper_promotions (paper_run_id, requested_at, state) VALUES (?, ?, 'requested')",
        (paper_run_id, requested_at),
    )
    return cursor.lastrowid


def approve_for_real_money(conn: sqlite3.Connection, paper_run_id: int, venue, now_iso: str) -> dict:
    """APPROVE FOR REAL MONEY: close a paper run out as STOP does, then end it 'promoted'.

    The one writer of end_reason 'promoted' (DECISIONS #481 item 2, #487 item 1). The route passes
    StubVenue, which closes nothing until lane P6b, so a stub close-out leaves any open position
    and open order in place. paper_promotions stays as it is: no state change, no decision fields.
    Refuses with PaperApproveRefused 'run-not-running' when the run has ended, and 'no-live-request'
    when its newest promotion row is absent or not 'requested'. A run whose STOP stopped part way
    resumes from its kill_step through _close_out, never restarts. Venue errors propagate.
    """
    run_row = conn.execute(
        "SELECT kill_started_at, kill_step, ended_at FROM paper_runs WHERE paper_run_id = ?",
        (paper_run_id,),
    ).fetchone()
    if run_row is None:
        raise PaperRunNotFound(f"paper_run_id={paper_run_id} does not exist.")
    if run_row["ended_at"] is not None:
        raise PaperApproveRefused(
            "run-not-running",
            "This strategy is no longer running on paper, so it cannot be approved for real money.",
        )
    promotion_row = conn.execute(
        "SELECT state FROM paper_promotions WHERE paper_run_id = ? "
        "ORDER BY requested_at DESC, promotion_id DESC LIMIT 1",
        (paper_run_id,),
    ).fetchone()
    if promotion_row is None or promotion_row["state"] != "requested":
        raise PaperApproveRefused(
            "no-live-request",
            "This strategy has not asked to leave paper. Press PROMOTE first.",
        )

    kill_step = _close_out(conn, paper_run_id, venue, now_iso,
                           run_row["kill_started_at"], run_row["kill_step"])

    cursor = conn.execute(
        "UPDATE paper_runs SET kill_step = 'slice_returned', ended_at = ?, end_reason = 'promoted' WHERE paper_run_id = ? AND kill_step = 'fills_written' AND ended_at IS NULL",
        (now_iso, paper_run_id),
    )
    if cursor.rowcount != 1:
        again = conn.execute(
            "SELECT kill_step, ended_at FROM paper_runs WHERE paper_run_id = ?", (paper_run_id,)
        ).fetchone()
        if again is not None and again["ended_at"] is not None:
            raise PaperApproveRefused(
                "run-not-running",
                "This strategy is no longer running on paper, so it cannot be approved for real money.",
            )
        raise RuntimeError(
            f"paper_run_id={paper_run_id} could not be ended as promoted: kill_step is "
            f"{again['kill_step'] if again is not None else kill_step!r} after the close-out, "
            "not 'fills_written'"
        )
    return {"paper_run_id": paper_run_id, "end_reason": "promoted", "ended_at": now_iso,
            "kill_step": "slice_returned"}


# =============================================================================
# Admission and the two read queries the card list and History need (brief sections 6.1, 6.2, 6.3,
# 6.6, 10.2; B-AC-9 storage half, B-AC-10 E3/E5/E8).
# =============================================================================

class PaperSyntheticDataRefused(RuntimeError):
    """admit_run was told the strategy's price data is synthetic. Synthetic data never reaches
    paper (CLAUDE.md, "Rules that never move"); nothing was written."""


def admit_run(conn: sqlite3.Connection, strategy_id: int, strategy_name: str,
              strategy_version: int, source_run_id: int, slice_opening_cash: float,
              started_at: str, expected_bounds_json: Union[str, None] = None,
              *, source_data_synthetic: bool) -> int:
    """Admit one strategy to paper: create its paper_runs row, its paper_slice_current row, and its
    first paper_equity snapshot, all in the SAME transaction, so a run never exists without a slice
    state (E3) and its equity series is never empty for a live run (E5). The first snapshot equals
    the credited slice, which is also the baseline the summary strip's "since each run started"
    window reads (brief section 6.2).

    slice_opening_cash is validated first (AC-0 root-cause dispatch, night seven): unchecked, it
    would seed FOUR REAL NOT NULL columns across three tables in this one call
    (paper_runs.slice_opening_cash, paper_slice_current.cash, paper_slice_current.equity,
    paper_equity.equity), and every later call to _recompute_slice_state for this run reads
    paper_runs.slice_opening_cash back (see that function above), so an invalid seed here would
    poison every derived figure this run ever produces for the run's whole life, not just this one
    write. Must be finite and strictly positive: a zero or negative slice cannot fund a single
    share.

    source_data_synthetic has no default, so no caller can forget to say it (lane P6a). True raises
    PaperSyntheticDataRefused before any write.
    """
    if not isinstance(source_data_synthetic, bool):
        raise TypeError(f"source_data_synthetic must be a bool, got {source_data_synthetic!r}")
    if source_data_synthetic:
        raise PaperSyntheticDataRefused(
            f"run {source_run_id} used synthetic data; synthetic data never reaches paper"
        )
    _require_finite_signed("slice_opening_cash", slice_opening_cash, allow_negative=False, allow_zero=False)
    cursor = conn.execute(
        "INSERT INTO paper_runs (strategy_id, strategy_name, strategy_version, source_run_id, "
        " slice_opening_cash, expected_bounds_json, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, strategy_name, strategy_version, source_run_id, slice_opening_cash,
         expected_bounds_json, started_at),
    )
    paper_run_id = cursor.lastrowid

    # E3's own "fresh and first run" answer: cash equals the opening slice, position_value and
    # committed_capital are real zeros (known, not unknown), equity equals the opening slice too.
    conn.execute(
        "INSERT INTO paper_slice_current (paper_run_id, cash, position_value, equity, "
        " realised_pnl, committed_capital, updated_at) VALUES (?, ?, 0.0, ?, 0.0, 0.0, ?)",
        (paper_run_id, slice_opening_cash, slice_opening_cash, started_at),
    )

    # E5's own "fresh and first run" answer: the first equity snapshot is written at admission.
    conn.execute(
        "INSERT INTO paper_equity (paper_run_id, as_of, equity) VALUES (?, ?, ?)",
        (paper_run_id, started_at, slice_opening_cash),
    )

    return paper_run_id


def list_running_runs(conn: sqlite3.Connection) -> list:
    """Currently running strategies, oldest first (brief section 6.3), served by the partial index
    ix_paper_runs_running.
    """
    return conn.execute(
        "SELECT * FROM paper_runs WHERE ended_at IS NULL ORDER BY started_at ASC"
    ).fetchall()


def promoted_run_ids(conn: sqlite3.Connection) -> list[int]:
    """Ids of runs that ended as promoted (ended_at set), in id order; read only (PRM1-0b)."""
    rows = conn.execute(
        "SELECT paper_run_id FROM paper_runs "
        "WHERE end_reason = 'promoted' AND ended_at IS NOT NULL ORDER BY paper_run_id"
    ).fetchall()
    return [row[0] for row in rows]


def list_ended_runs(conn: sqlite3.Connection) -> list:
    """Every past paper run, newest ended first (brief section 6.6, History), served by
    ix_paper_runs_ended. Returns an empty list, never an error, on a fresh table (E8).
    """
    return conn.execute(
        "SELECT * FROM paper_runs WHERE ended_at IS NOT NULL ORDER BY ended_at DESC"
    ).fetchall()


# =============================================================================
# Night plan 2026-09-12 (set 5 day, lane PP, U.8): three new READ functions for the paper page's
# DETAILS view, THE TRADES table and RETIRED IN PAPER. None of these writes a row; every one reads
# tables this module already owns, matching this lane's own charter restriction (paperdb.py is
# read-functions-only for this build: no CREATE, ALTER or DROP anywhere in this module).
# =============================================================================

def list_fills(conn: sqlite3.Connection, paper_run_id: int) -> list:
    """Every paper_fills row for one run, oldest first by recorded_at (the one column every row,
    filled or refused, always carries -- section 10.3's own "always known" column; filled_at
    would sort every refusal row as NULL-first rather than in true chronological order). Filled
    rows and refusal rows are BOTH returned, undistinguished by this function: the caller (THE
    TRADES table and DETAILS' own chart, plan section 9 items 2 and 3) reads filled_at and
    refusal_reason to tell them apart, matching rule 11's own "factual, or said in words" law --
    a refused order is counted and visible, never dropped from the log the page reads.
    """
    rows = conn.execute(
        "SELECT * FROM paper_fills WHERE paper_run_id = ? ORDER BY recorded_at ASC, fill_id ASC",
        (paper_run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def trade_stats(conn: sqlite3.Connection, paper_run_id: int) -> dict:
    """THE TRADES table's own W/L figures (plan section 9 item 3): win and loss counts, average
    win, average loss, largest win, largest loss, plus per_fill_realised_gain (fill_id -> dollars)
    so a caller can show the exact same number on each row that this function sums in aggregate,
    never a second, independently-computed formula (see _walk_average_cost's own docstring). A
    fill whose gain is exactly 0.0 (a flat close) counts as neither a win nor a loss, an
    operational reading choice this lane makes rather than one the brief rules on, named here
    rather than silently decided. A run with no closing sell fill yet returns zero counts and
    None for every dollar figure: a real, honest "nothing has closed" rather than a fabricated
    zero average (there is no average of zero numbers).
    """
    run_row = conn.execute(
        "SELECT slice_opening_cash FROM paper_runs WHERE paper_run_id = ?", (paper_run_id,)
    ).fetchone()
    if run_row is None:
        raise PaperSliceStateMissing(
            f"paper_run_id={paper_run_id!r} does not exist in paper_runs; cannot derive trade "
            "stats for a run that was never admitted"
        )
    fills = conn.execute(
        "SELECT fill_id, symbol, side, filled_quantity, fill_price, fees FROM paper_fills "
        "WHERE paper_run_id = ? AND filled_at IS NOT NULL ORDER BY filled_at ASC, fill_id ASC",
        (paper_run_id,),
    ).fetchall()
    walk = _walk_average_cost(paper_run_id, run_row["slice_opening_cash"], fills)
    gains = list(walk["per_fill_realised_gain"].values())
    wins = [g for g in gains if g > 0.0]
    losses = [g for g in gains if g < 0.0]
    return {
        "win_count": len(wins),
        "loss_count": len(losses),
        "average_win": (sum(wins) / len(wins)) if wins else None,
        "average_loss": (sum(losses) / len(losses)) if losses else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "per_fill_realised_gain": walk["per_fill_realised_gain"],
    }


def list_retired_strategies(conn: sqlite3.Connection) -> list:
    """Every strategy RETIRED IN PAPER (DECISIONS #376 ruling 5: a paper Kill retires the
    strategy; re-entry is only a new candidate through the full battery). Derived purely from
    paper_runs.end_reason = 'killed': this store holds no research_strategies row and writes none
    (paperdb.assert_no_research_import forbids the reverse direction; this function does not even
    read research.db, only its own table), so a strategy's catalogue mark is the CALLER's to
    apply -- this function only answers "which strategies has paper.db itself seen killed, and
    when", grouped by strategy_id so a strategy killed more than once (a version replayed after a
    later re-admission under the SAME id, which this store cannot itself prevent) reads once with
    its newest kill.
    """
    rows = conn.execute(
        "SELECT strategy_id, strategy_name, MAX(ended_at) AS retired_at FROM paper_runs "
        "WHERE end_reason = 'killed' GROUP BY strategy_id, strategy_name "
        "ORDER BY retired_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# The two pure-logic pieces of E7 (the paper node session, section 11) this lane can test without a
# live node process or a live broker connection: reconciliation's own halting rule, and session-loss
# detection. Both are probe-authored contract additions naming section 12's own exceptions.
# =============================================================================

def reconcile_positions(local_positions: dict, venue_positions: dict) -> None:
    """Raise PaperPositionMismatch when a locally recorded position disagrees with the venue's
    reported position, in any of three shapes (brief section 9.2, assumption A9; widened by gate
    finding B-AC-17 to match this exception's own class docstring, which promised all three and
    this function originally caught only the first):
      1. PRESENCE (this module's original behaviour, unchanged): the venue reports a nonzero
         position for a run local_positions has no entry for at all.
      2. QUANTITY DRIFT: both books know the run, and a symbol's quantity differs between them.
      3. REVERSE PRESENCE: local_positions has a nonzero position for a run venue_positions has no
         entry for at all. Not a hypothetical: press_kill's own step 3 closes a position at the
         venue directly (venue.close_all_positions), never through submit_paper_order, and step 4
         writes no paper_fills row recording that close, so a completed Kill leaves exactly this
         shape behind until the local side is reconciled too.
    Never adopts or drops an unexplained position silently in any of the three shapes; a caller
    that catches this halts the affected run and reports whatever figures the message names.
    """
    # A deterministic, sorted walk over every run id either book mentions, so the mismatch this
    # raises for a given pair of inputs never depends on Python's own dict or set iteration order.
    every_run_id = sorted(set(local_positions) | set(venue_positions))
    for run_id in every_run_id:
        local_symbols = local_positions.get(run_id)
        venue_symbols = venue_positions.get(run_id)

        if venue_symbols is not None and local_symbols is None:
            # Case 1: the venue knows this run; the local book has no entry for it at all.
            has_nonzero_position = any(quantity != 0 for quantity in venue_symbols.values())
            if has_nonzero_position:
                raise PaperPositionMismatch(
                    f"venue reports a nonzero position for paper_run_id={run_id!r} "
                    f"({venue_symbols!r}) with no local record for that run at all; refusing to "
                    "adopt it silently"
                )
            continue

        if local_symbols is not None and venue_symbols is None:
            # Case 3: the local book knows this run; the venue has no entry for it at all.
            has_nonzero_position = any(quantity != 0 for quantity in local_symbols.values())
            if has_nonzero_position:
                raise PaperPositionMismatch(
                    f"paper_run_id={run_id!r}: local position {local_symbols!r} has no matching "
                    "venue position at all (the venue reports nothing for this run); refusing to "
                    "drop it silently"
                )
            continue

        # Case 2: both books know this run (every_run_id's own construction guarantees at least
        # one side is non-None here, and the two branches above already handled "only one side");
        # compare every symbol either side names.
        for symbol in sorted(set(local_symbols) | set(venue_symbols)):
            local_quantity = local_symbols.get(symbol, 0.0)
            venue_quantity = venue_symbols.get(symbol, 0.0)
            if local_quantity != venue_quantity:
                raise PaperPositionMismatch(
                    f"paper_run_id={run_id!r} symbol={symbol!r}: local position "
                    f"{local_quantity!r} disagrees with venue position {venue_quantity!r}; "
                    "refusing to trust either book silently"
                )


def assert_session_active(session_state: str) -> None:
    """Raise PaperSessionTaken unless session_state is exactly "active" (brief section 12,
    assumption A5). Any other value, including one this module has never seen before, halts
    trading for the affected runs rather than being retried into silence.
    """
    if session_state != "active":
        raise PaperSessionTaken(
            f"paper node session_state={session_state!r} is not active; halting trading for the "
            "affected runs rather than reconnecting in a silent loop"
        )


def assert_realtime_feed(is_realtime_confirmed) -> None:
    """Raise PaperMarketDataNotRealTime unless is_realtime_confirmed is literally True (brief
    section 12, assumption A6, B-AC-6). No delayed-data parameter or fallback path exists anywhere
    in this module; a paper run on data that is not proven real-time is a different experiment, not
    a cheaper version of the same one.
    """
    if is_realtime_confirmed is not True:
        raise PaperMarketDataNotRealTime(
            f"the market data feed's real-time entitlement is not confirmed true "
            f"(is_realtime_confirmed={is_realtime_confirmed!r}); refusing to start rather than "
            "falling back to a delayed feed"
        )


# =============================================================================
# The import-boundary assertion (job file, "What the brief already decides" item 2): paperdb gets
# the same guard opsdb.py already has, including the case that file already gets right (raising
# when NEITHER scanned directory exists, rather than reading a tree that was never scanned as a
# boundary that holds). No B-AC-<n> criterion in section 17 names this rule, so no probe in this
# suite exercises it; built anyway because the job file requires it structurally, and named here so
# the gap in probe coverage is a stated fact rather than a silent one. This function is not yet
# called from anywhere: wiring it into main.py's lifespan, beside opsdb's own boundary check, is
# for whichever unit next touches main.py, since main.py is outside this lane's file boundary.
# =============================================================================

class PaperImportBoundaryViolation(RuntimeError):
    """Raised when a research-side module imports paperdb, crossing the money-path boundary."""


class PaperImportBoundaryUnverifiable(RuntimeError):
    """Raised when NEITHER research_lab/ nor dashboard/backend/research/ exists in the tree being
    scanned, so the import boundary could not be checked at all. A guard that scans nothing must
    never report itself as passing.
    """


def _file_imports_paperdb(py_path: Path) -> bool:
    """True if the given .py file's own import statements name "paperdb" anywhere, parsed with the
    stdlib ast module so the word "paperdb" appearing inside an unrelated comment or string literal
    can never produce a false positive.
    """
    tree = ast.parse(py_path.read_text(), filename=str(py_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "paperdb" or alias.name.endswith(".paperdb"):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and (node.module == "paperdb" or node.module.endswith(".paperdb")):
                return True                                       # "from dashboard.backend.paperdb import X"
            for alias in node.names:
                if alias.name == "paperdb":
                    return True                                   # "from dashboard.backend import paperdb"
    return False


def assert_no_research_import(repo_root: Union[str, Path, None] = None) -> None:
    """Fail loudly if any ".py" file under "<repo_root>/research_lab/" or
    "<repo_root>/dashboard/backend/research/" imports paperdb. Returns None on a clean tree that
    actually got scanned; raises PaperImportBoundaryUnverifiable instead of returning None when
    NEITHER directory exists, because a check that scans nothing must not report "clean".
    """
    root = Path(repo_root) if repo_root is not None else PROJECT_ROOT
    banned_dirs = (root / "research_lab", root / "dashboard" / "backend" / "research")
    any_dir_scanned = False
    for banned_dir in banned_dirs:
        if not banned_dir.is_dir():
            continue
        any_dir_scanned = True
        for dirpath, dirnames, filenames in os.walk(banned_dir):
            # Prune noise directories in place: a virtualenv or a bytecode cache can hold thousands
            # of unrelated .py files, and a vendored dependency could coincidentally name something
            # "paperdb" with no bearing on this repository's own boundary.
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                py_path = Path(dirpath) / filename
                if _file_imports_paperdb(py_path):
                    raise PaperImportBoundaryViolation(
                        f"{py_path} imports paperdb, crossing the research/money-path boundary "
                        "(brief section 10.1: paper.db state must stay physically out of the "
                        "research data path)"
                    )
    if not any_dir_scanned:
        raise PaperImportBoundaryUnverifiable(
            f"neither {banned_dirs[0]} nor {banned_dirs[1]} exists under {root}, so the "
            "research/money-path import boundary could not be checked"
        )
