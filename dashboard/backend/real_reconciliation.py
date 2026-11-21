"""real_reconciliation.py: the venue reconciliation sweep, lane RT3, night three
(docs/plans/night/2026-09-13_set8a_night/PLAN.md section 8, brief AC-R1 to AC-R8).

THIS IS A MONEY PATH. This module never sends an order: it only reads the venue's own answers
(through the adapter double tonight, never a live broker) and writes what our own store already
knew alongside what the venue said. Nothing here can write a real_enabling_records row and nothing
here turns real trading on.

WHY A SWEEP RATHER THAN TRUSTING THE SUBMIT RESPONSE ALONE (round 5 fold, finding 1: the earlier
citation here named a chapter-8 section title and a quoted sentence that exist nowhere in
docs/books/kleppmann_canon.md; neither was verified against the book before it was written, which
the Book protocol requires, so it is replaced with a real, checked one). Kleppmann, Data-Intensive
Applications, Chapter 10: Consistency and Consensus (verified at docs/books/kleppmann_canon.md line
2498; quote below at line 2519). Quoted verbatim: "shared log: append-only log shared by multiple
writers; equivalent to total order broadcast and to consensus." In plain words, and without
overclaiming that this sweep runs a distributed consensus protocol (it does not; it is a single
process comparing two records, never multiple nodes voting): the underlying idea a shared log and
this sweep both rest on is the SAME one, that when more than one party can report on what happened,
"true" has to mean an AGREED answer reached through a fixed procedure applied to what each party
says, never whichever party is trusted by default. Here the two parties are night two's own submit
path (what we asked for and directly observed) and the venue's own record (what actually happened),
which can disagree because a submit call can time out, get rejected after a real fill, or land on a
connection nobody read the response from. The sweep is what makes the venue's own answer, asked by
our own key, the SINGLE SOURCE OF TRUTH for what happened to an order: night two's own submit path
records what it directly observed, and this module is the only path allowed to write the three words
(`filled`, `cancelled`, `never-received`) that a plain submit response alone cannot honestly claim,
because a submit call that itself failed to answer (timeout, connection drop) never runs night two's
own step 3 UPDATE at all.

WHY THE POSITION READ RUNS ONLY AFTER EVERY LOOKUP, AND WHY ONE TRANSACTION HOLDS BOTH KINDS OF
WRITE (AC-R8, DDIA chapter 7, "Sharding for multitenancy" is this store's own justification for its
separate file; the transaction ordering below is chapter 8's atomicity quote, restated once in
real_orders.py's own module docstring and applied here for the SAME reason): a position cap's own
arithmetic (real_limits.py) adds the reconciled venue figure to the sum of still-open intents. An
order stops counting as an open intent the instant its terminal state is written; if the position
read happened BEFORE that write, or in a separate transaction from it, a window would exist where an
order counts in NEITHER the reconciled figure NOR the open-intent sum, understating exposure for as
long as that window stays open. Reading positions after every lookup, and writing both together in
one commit, closes that window structurally rather than by discipline.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional

from .realdb import AdapterCannotDetermine

# The one fixed timestamp format every module in this lane's own build shares (matching realdb.py's
# own _iso_seconds_between and real_limits.py's own _seconds_between): a naive strptime compares
# correctly because every caller uses this same format.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# A tolerance, not a business rule: two floating-point sums of the same underlying orders can differ
# by less than a cent from rounding alone, and treating that as a real disagreement would halt an
# account over noise. Anything past this gap is a real disagreement between our book and the venue's.
_DISAGREEMENT_TOLERANCE = 1e-6


class ReconciliationCouldNotReachVenue(RuntimeError):
    """Raised internally, inside this module only, when a venue read fails partway through a sweep.
    run_reconciliation_sweep catches this itself and turns it into a `could-not-reach-venue` pass
    record; no caller of run_reconciliation_sweep ever sees this exception, so it carries no detail
    beyond its own name.
    """


def _seconds_between(later_iso: str, earlier_iso: str) -> float:
    """Seconds between two of this module's own timestamp strings, later minus earlier. A private
    copy of realdb._iso_seconds_between's own arithmetic, matching real_limits.py's own choice to
    keep this one small, stateless calculation local rather than reach into another module's
    underscore-prefixed internals.
    """
    later = datetime.strptime(later_iso, _ISO_FORMAT)
    earlier = datetime.strptime(earlier_iso, _ISO_FORMAT)
    return (later - earlier).total_seconds()


def latest_reconciliation_pass(conn: sqlite3.Connection, account_id: str) -> Optional[sqlite3.Row]:
    """The newest real_reconciliation_runs row for account_id whose own outcome is 'clear' or
    'disagreement' (AC-R6: a COMPLETED pass), or None when no such row exists at all. A
    'could-not-reach-venue' row is an ATTEMPT, not a success (E6's own words), and is never returned
    here: it does not move the successful-reconciliation age and it does not close the
    'never-reconciled' window (brief section 11.3, the E6 state table).
    """
    return conn.execute(
        "SELECT run_id, account_id, ran_at, outcome, malformed_count, disagreement_detail_json "
        "FROM real_reconciliation_runs WHERE account_id = ? AND outcome IN ('clear', 'disagreement') "
        "ORDER BY run_id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def reconciliation_freshness_reason(conn: sqlite3.Connection, account_id: str, now_iso: str,
                                     max_age_seconds: float) -> Optional[str]:
    """AC-R5's own table (docs/briefs/real_trading_plumbing_spec_brief.md, "What AC-R5's check
    returns in each state of the record"), as a pure function over the store's current state:

      - No completed pass exists at all (whether the account has never been reconciled, or has only
        ever produced failed attempts) -> 'reconciliation-never-run'. The brief's own table gives
        these two states the SAME reason on purpose: "Only failed attempts, never a success" and "No
        record of any kind" are both rows in that table, and both name `reconciliation-never-run`.
      - The newest completed pass is older than max_age_seconds, OR its own outcome was
        'disagreement' -> 'reconciliation-stale' (the SAME word for both; AC-R5's own sentence: "The
        two reconciliation-stale rows share one reason word on purpose").
      - Otherwise -> None (permit; the gate moves to its next check).

    max_age_seconds is the CALLER's own already-configured reading of
    real_trading.reconciliation_max_age. AC-P1 (submit_real_order's own step 0) already refuses the
    WHOLE gate before this function is ever reached when the parameter is unconfigured, so in the
    ordinary call path max_age_seconds is always a real number here.

    Round 4 fold, finding 1 (SHOULD-FIX, a NULL under a state that can occur): step 0 and this
    function read the SAME row from two separate, un-transacted queries (step 0's own check, then
    this call), so a caller reaching this point with max_age_seconds=None is still possible, either
    through a race (the parameter cleared between the two reads) or a caller that bypasses step 0
    entirely. Either way this function must refuse BY NAME, never crash comparing None to a number:
    a NULL here answers 'reconciliation-max-age-absent', a third named reason distinct from the two
    above, rather than letting `age_seconds > max_age_seconds` raise TypeError.
    """
    if max_age_seconds is None:
        return "reconciliation-max-age-absent"
    row = latest_reconciliation_pass(conn, account_id)
    if row is None:
        return "reconciliation-never-run"
    if row["outcome"] == "disagreement":
        return "reconciliation-stale"
    age_seconds = _seconds_between(now_iso, row["ran_at"])
    if age_seconds > max_age_seconds:
        return "reconciliation-stale"
    return None


def _classify_lookup_answer(answer):
    """Map one adapter.lookup_order(...) response onto AC-R2's four shapes, plus fold MF7's own
    override (a rejection carrying a non-zero fill is treated as a fill, never a plain rejection).
    Returns (terminal_state, fields) where terminal_state is one of 'filled', 'cancelled',
    'rejected', 'never-received', or None when the order is still 'working' (no write queued at
    all); returns (None, None) is never confused with the malformed sentinel below because the
    caller checks 'malformed' by identity against this exact string, never against a falsy value.

    A response with no readable 'status' key, or a 'status' this function does not recognise at
    all, is MALFORMED (AC-R7): the caller leaves that one row untouched and counts it, and this
    function itself never raises for it, because one bad row must never abort the read of the rest.
    """
    if not isinstance(answer, dict) or "status" not in answer:
        return "malformed", None
    status = answer["status"]

    if status == "filled":
        return "filled", {
            "filled_quantity": answer.get("filled_quantity"),
            "fill_price": answer.get("fill_price"),
            "venue_order_id": answer.get("venue_order_id"),
            "venue_accepted": True,
        }

    if status == "cancelled":
        return "cancelled", {
            "filled_quantity": None,
            "fill_price": None,
            "venue_order_id": answer.get("venue_order_id"),
            "venue_accepted": None,
        }

    if status == "rejected":
        filled_quantity = answer.get("filled_quantity")
        # Fold MF7's own rule: a rejection that ALSO reports a real fill is a fill, never a plain
        # rejection, because something did fill and position tracking wins over the venue's own word.
        if filled_quantity not in (None, 0, 0.0):
            return "filled", {
                "filled_quantity": filled_quantity,
                "fill_price": answer.get("fill_price"),
                "venue_order_id": answer.get("venue_order_id"),
                "venue_accepted": True,
            }
        return "rejected", {
            "filled_quantity": 0.0 if filled_quantity is None else filled_quantity,
            "fill_price": None,
            "venue_order_id": answer.get("venue_order_id"),
            "venue_accepted": False,
        }

    if status == "working":
        # Still open at the venue: no write queued, the row stays exactly as open as it started.
        return None, None

    if status == "unknown":
        # The venue never received this order at all (capability 2's own fourth shape).
        return "never-received", {
            "filled_quantity": None,
            "fill_price": None,
            "venue_order_id": None,
            "venue_accepted": None,
        }

    return "malformed", None


def _our_own_figure(conn: sqlite3.Connection, account_id: str, scope: str,
                     ticker: Optional[str]) -> float:
    """This account's own book for one scope, computed FRESH from real_orders at read time, never
    stored (AC-R8's own step 3): the signed sum of intended_notional over every TERMINAL 'filled'
    row, +1 for a buy and -1 for a sell, scoped to one ticker or unscoped for the account total. This
    is the account's own book BEFORE this pass's own pending terminal writes land (this function
    reads before phase 5's transaction ever opens), matching AC-R8's own argument that the direction
    which double-counts exposure for one extra window is the safe one.
    """
    signed_notional = "CASE WHEN side = 'buy' THEN intended_notional ELSE -intended_notional END"
    if scope == "account":
        row = conn.execute(
            "SELECT COALESCE(SUM(%s), 0.0) AS total FROM real_orders "
            "WHERE account_id = ? AND terminal_state = 'filled'" % signed_notional,
            (account_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(%s), 0.0) AS total FROM real_orders "
            "WHERE account_id = ? AND symbol = ? AND terminal_state = 'filled'" % signed_notional,
            (account_id, ticker),
        ).fetchone()
    return row["total"]


def _venue_figure(positions, scope: str, ticker: Optional[str]) -> Optional[float]:
    """The venue's own net_notional for one scope out of adapter.report_account_state()['positions'],
    or None when the venue's own list carries no matching entry at all (E6's own NULL answer: an
    absent figure is never read as zero).

    Regression fold (found running real_trading_1's own ac8, which passes plain string markers as
    'positions' to test the KILL command's own clearing behaviour, a fixture that predates this
    sweep and never claims to carry position dicts): a list entry that is not a dict cannot be read
    for any scope at all, so it is skipped exactly like a dict whose own 'scope' key simply does
    not match, rather than raising AttributeError and aborting command_kill's own new trigger call.
    A live adapter's report_account_state always returns position dicts; this guard only protects
    against a caller (test or otherwise) that hands this function something else.
    """
    for position in positions:
        if not isinstance(position, dict):
            continue
        if position.get("scope") != scope:
            continue
        if scope == "ticker" and position.get("ticker") != ticker:
            continue
        return position.get("net_notional")
    return None


def _insert_pass_record(conn: sqlite3.Connection, account_id: str, outcome: str, ran_at: str,
                         malformed_count: int, disagreement_detail, own_transaction: bool) -> int:
    """Insert ONE real_reconciliation_runs row. own_transaction=True wraps this single INSERT in its
    own short BEGIN IMMEDIATE / COMMIT (the 'could-not-reach-venue' and 'disagreement' paths, where
    this is the ONLY write the pass makes); own_transaction=False leaves the caller's own already-open
    transaction in charge of committing (the 'clear' path, where this INSERT is one statement among
    several that must all land together, AC-R8).
    """
    detail_json = json.dumps(disagreement_detail) if disagreement_detail is not None else None
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE;")
    try:
        cursor = conn.execute(
            "INSERT INTO real_reconciliation_runs (account_id, ran_at, outcome, malformed_count, "
            "disagreement_detail_json) VALUES (?, ?, ?, ?, ?)",
            (account_id, ran_at, outcome, malformed_count, detail_json),
        )
        if own_transaction:
            conn.execute("COMMIT;")
        return cursor.lastrowid
    except (sqlite3.Error, sqlite3.Warning):
        if own_transaction and conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise


def our_own_account_figure(conn: sqlite3.Connection, account_id: str) -> float:
    """Public entry point for routes/real.py's own reconciliation-latest route (AC-R3): this
    account's own book at the ACCOUNT scope, computed fresh, exactly as run_reconciliation_sweep's
    own step 3 computes it for the same scope.
    """
    return _our_own_figure(conn, account_id, "account", None)


def _our_own_figures_by_symbol(conn: sqlite3.Connection, account_id: str) -> dict:
    """Round 5 fold, code-quality SF1: every symbol's own signed filled total for this account,
    in ONE query, read once per sweep instead of once per ticker (the bug this replaces ran this
    same SUM, filtered to one symbol at a time, inside the comparison loop below, every 60-second
    tick and every kill command, against real_orders, a table that grows without bound). GROUP BY
    symbol gives every ticker's own total in one pass; the account-wide figure is this dict's own
    sum, computed by the caller, never a second query. A symbol with no filled row at all is simply
    absent from this dict, matching COALESCE's own zero-row behavior in the single-scope query
    _our_own_figure still runs for the route reader below.
    """
    signed_notional = "CASE WHEN side = 'buy' THEN intended_notional ELSE -intended_notional END"
    rows = conn.execute(
        "SELECT symbol, COALESCE(SUM(%s), 0.0) AS total FROM real_orders "
        "WHERE account_id = ? AND terminal_state = 'filled' GROUP BY symbol" % signed_notional,
        (account_id,),
    ).fetchall()
    return {row["symbol"]: row["total"] for row in rows}


def _our_own_figure_after_step1(by_symbol: dict, scope: str, ticker: Optional[str],
                                 queued_fills: dict) -> float:
    """Round 2 fix (AC-25, brief AC-R3): our own book for one scope, AFTER incorporating step 1's
    own pending answers, never before them. AC-R3's own words are that the comparison is against
    OUR EXPECTATION, which after step 1 already includes a fill step 1 just learned about; reading
    only what is already committed as 'filled' in real_orders ignores a fill this SAME pass just
    queued, so a fresh fill the venue reports and we have not yet recorded would misread as a
    disagreement, forever, since every later pass repeats the same stale comparison. queued_fills
    maps client_order_id -> (symbol, signed_notional) for every row this pass's own step 1 is about
    to mark 'filled' (never for 'cancelled', 'rejected' or 'never-received', which contribute no
    exposure); by_symbol (round 5 fold, code-quality SF1: one query per sweep, from
    _our_own_figures_by_symbol, taken as a plain dict instead of re-querying per scope) still
    excludes every queued fill, because their terminal_state is not written until step 4's
    transaction, so adding the two together double-counts nothing. The account scope sums every
    symbol's own committed total; a ticker scope reads just that one symbol's own total, 0.0 when
    the account has never had a filled row for it at all.
    """
    base = sum(by_symbol.values()) if scope == "account" else by_symbol.get(ticker, 0.0)
    queued_total = 0.0
    for symbol, signed_notional in queued_fills.values():
        if scope == "account" or symbol == ticker:
            queued_total += signed_notional
    return base + queued_total


def run_reconciliation_sweep(conn: sqlite3.Connection, account_id: str, adapter, tickers,
                              now_iso: str) -> dict:
    """AC-R1's mechanism, AC-R2 to AC-R8 the walk it performs. Returns
    {outcome: 'clear' | 'disagreement' | 'could-not-reach-venue', run_id: int, malformed_count: int}.

    Step 1 (AC-R2, AC-R7): every OPEN row for account_id (refusal_reason IS NULL AND terminal_state
    IS NULL) is looked up by our own key. A raise from the venue stops the WHOLE pass here (AC-R8's
    own first failure-part-way-through answer): no order row is touched, one 'could-not-reach-venue'
    pass record is written in its own short transaction, and this function returns immediately. A
    malformed answer for ONE row never aborts the read of the rest (AC-R7): that row is left open,
    counted, and the loop continues.

    Step 2 (AC-R3), run only AFTER every lookup above, never before: the account's own positions are
    read. A raise here is handled exactly like step 1's own raise, because nothing from step 1 has
    been written to disk yet (it is still in memory as queued writes), so there is nothing to undo.

    Step 3 (AC-R4, round 2 fix AC-25): our own book, AFTER step 1's own pending answers are folded
    in (never before them, AC-R3), and the venue's own figure are compared, scope by scope (the
    account total, then each ticker this pass was asked about). A scope the venue reports NO figure
    for is judged too, never skipped: it disagrees when our own post-step-1 figure is non-zero (a
    missing measurement standing in for a permissive one is itself a disagreement), and agrees when
    our own figure is genuinely zero (a real, agreeing absence). The first scope that disagrees
    makes the WHOLE pass 'disagreement': the pass record carries that one scope's own two figures.

    Step 4 (AC-R8, round 2 fix): ONE transaction writes every queued terminal state from step 1
    REGARDLESS of the outcome (AC-R8's own words carry no exemption for a disagreement: recording
    what the venue told us about OUR OWN orders is never "adjusting our book to the venue's
    position", which AC-R4 forbids only for the position READING, not the order's own terminal
    state), then, ONLY on 'clear', one real_position_readings row per scope this pass was asked
    about (AC-R4: our own book is never adjusted from the venue's reading on a disagreement, so no
    reading is written at all then, unchanged from round 1), then the pass record itself, then
    commits once. No adapter call happens between this transaction's open and its commit.
    """
    open_rows = conn.execute(
        "SELECT client_order_id, symbol, side, intended_notional FROM real_orders "
        "WHERE account_id = ? AND refusal_reason IS NULL AND terminal_state IS NULL",
        (account_id,),
    ).fetchall()

    queued_writes = {}
    # Round 2 fix (AC-25): the signed notional of every row THIS pass is about to mark 'filled',
    # keyed by client_order_id, so step 3's own comparison can see it before it is ever committed.
    queued_fills = {}
    malformed_count = 0
    for row in open_rows:
        client_order_id = row["client_order_id"]
        try:
            answer = adapter.lookup_order(client_order_id)
        except AdapterCannotDetermine:
            run_id = _insert_pass_record(conn, account_id, "could-not-reach-venue", now_iso, 0,
                                          None, own_transaction=True)
            return {"outcome": "could-not-reach-venue", "run_id": run_id, "malformed_count": 0}

        terminal_state, fields = _classify_lookup_answer(answer)
        if terminal_state == "malformed":
            malformed_count += 1
            continue
        if terminal_state is None:
            continue
        queued_writes[client_order_id] = (terminal_state, fields)
        if terminal_state == "filled":
            signed_notional = (row["intended_notional"] if row["side"] == "buy"
                                else -row["intended_notional"])
            queued_fills[client_order_id] = (row["symbol"], signed_notional)

    try:
        positions = adapter.report_account_state()["positions"]
    except AdapterCannotDetermine:
        run_id = _insert_pass_record(conn, account_id, "could-not-reach-venue", now_iso, 0,
                                      None, own_transaction=True)
        return {"outcome": "could-not-reach-venue", "run_id": run_id, "malformed_count": 0}

    # The account scope is always checked; each named ticker is checked in the order it was given.
    scopes = [("account", None)] + [("ticker", ticker) for ticker in tickers]

    venue_by_scope = {key: _venue_figure(positions, key[0], key[1]) for key in scopes}
    # Round 5 fold, code-quality SF1: one query for every symbol's own filled total, read ONCE
    # before the loop below, instead of one fresh SUM per ticker inside it.
    by_symbol = _our_own_figures_by_symbol(conn, account_id)

    disagreement_detail = None
    for scope, ticker in scopes:
        venue_value = venue_by_scope[(scope, ticker)]
        ours_value = _our_own_figure_after_step1(by_symbol, scope, ticker, queued_fills)
        if venue_value is None:
            # Round 2 fix (AC-25): a silent venue is judged, never skipped past. Our own figure
            # being genuinely zero is a real, agreeing absence; any other value means the venue's
            # silence is standing in for a permissive answer, which is itself a disagreement.
            if abs(ours_value) > _DISAGREEMENT_TOLERANCE:
                detail = {"scope": scope, "ours": ours_value, "venue": None}
                if scope == "ticker":
                    detail["ticker"] = ticker
                disagreement_detail = detail
                break
            continue
        if abs(venue_value - ours_value) > _DISAGREEMENT_TOLERANCE:
            detail = {"scope": scope, "ours": ours_value, "venue": venue_value}
            if scope == "ticker":
                detail["ticker"] = ticker
            disagreement_detail = detail
            break

    outcome = "disagreement" if disagreement_detail is not None else "clear"

    conn.execute("BEGIN IMMEDIATE;")
    try:
        # Round 2 fix (AC-25): step 1's own terminal writes land for BOTH outcomes, never only on
        # 'clear'; a disagreement no longer drops the fills this same pass just learned about.
        for client_order_id, (terminal_state, fields) in queued_writes.items():
            conn.execute(
                "UPDATE real_orders SET terminal_state = ?, filled_quantity = ?, fill_price = ?, "
                "venue_order_id = ?, venue_accepted = ?, resolved_at = ? WHERE client_order_id = ?",
                (terminal_state, fields["filled_quantity"], fields["fill_price"],
                 fields["venue_order_id"], fields["venue_accepted"], now_iso, client_order_id),
            )
        if outcome == "clear":
            # AC-R4, unchanged from round 1: a position reading is written only when every scope
            # agreed; a disagreement never adjusts our own book toward the venue's own reading.
            for scope, ticker in scopes:
                venue_value = venue_by_scope[(scope, ticker)]
                if venue_value is None:
                    conn.execute(
                        "INSERT INTO real_position_readings (account_id, scope, ticker, "
                        "net_notional, absent_reason, recorded_at) VALUES (?, ?, ?, NULL, "
                        "'not-reported-by-venue', ?)",
                        (account_id, scope, ticker, now_iso),
                    )
                else:
                    conn.execute(
                        "INSERT INTO real_position_readings (account_id, scope, ticker, "
                        "net_notional, absent_reason, recorded_at) VALUES (?, ?, ?, ?, NULL, ?)",
                        (account_id, scope, ticker, venue_value, now_iso),
                    )
        run_id = _insert_pass_record(conn, account_id, outcome, now_iso, malformed_count,
                                      disagreement_detail, own_transaction=False)
        conn.execute("COMMIT;")
    except (sqlite3.Error, sqlite3.Warning):
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise

    return {"outcome": outcome, "run_id": run_id, "malformed_count": malformed_count}
