"""real_orders.py: the candidate chain and the three-step real order path, lane RL, night two
(docs/plans/night/2026-09-12_set7a_night/PLAN.md section 8).

THIS IS A MONEY PATH. Real trading stays DISABLED tonight: nothing here writes a value for any of
the nine section-16 parameters, nothing writes a real_enabling_records row, and every order this
module can ever place still runs against a recording double, never a live broker connection.

THE THREE-STEP ORDER PATH (AC-O2, AC-L5), the same shape dashboard/backend/paperdb.py's own
submit_paper_order already uses, justified by DDIA chapter 8 ("Transactions"), quoted verbatim from
the main checkout's docs/books/kleppmann_canon.md:

    "External side effects (sending email) happen even on abort; 2PC can coordinate, or use
    idempotency."

    "**Atomicity** (better called *abortability*): if a fault aborts a transaction partway, every
    write so far is discarded. Without atomicity, you can't tell which writes took effect; with it,
    a failed transaction left the database unchanged."

In plain words: placing a real order at a venue is an external side effect a database transaction
can never undo (the venue may already have acted by the time a rollback runs), so this module never
wraps the venue call inside a transaction of its own. Instead it uses idempotency: (1) an intent row,
carrying a client-generated key, is inserted and committed ALONE, in its own short atomic step,
before the venue is ever called, so that step's own atomicity (Kleppmann's "abortability") guarantees
the row is either fully there or not there at all, with nothing of this module's own left open; (2)
the venue is called next, with no transaction of this module's own spanning the call, using that same
key so a retried call at the venue is idempotent there rather than a second order; (3) the venue's
validated response is recorded against the SAME row, matched by key, in its own second short atomic
step, never a second insert.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import uuid
from typing import Optional

from .realdb import (
    AdapterCannotDetermine,  # noqa: F401 (re-exported for callers that need the type by name)
    derive_enabled,
    get_kill_state,
    kill_check_reason,
    assert_parameters_configured,
    SIX_HARD_LIMIT_REASONS,
    LIMIT_PER_ORDER_NOTIONAL,
    LIMIT_TICKER_NET_CAP,
    LIMIT_TOTAL_NET_CAP,
    LIMIT_LOSS_WINDOW,
    LIMIT_LOSS_SINCE_GO,
    LIMIT_ORDER_RATE,
)
from . import real_limits
from . import real_reconciliation
from . import reservedb
from . import retestdb

logger = logging.getLogger(__name__)


class RealOrderRefused(RuntimeError):
    """Raised by submit_real_order for every pre-submission refusal, phase A and phase B alike,
    AFTER the refused row is written and committed. .reason carries the exact word a caller (or a
    probe) reads back.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("real order refused: %s" % reason)


class RealOrderVenueResponseInvalid(RuntimeError):
    """Raised when the adapter's own response fails finite/sign validation (AC-O6). The intent row
    is left exactly as step 1 committed it: this never writes a row and never records a refusal,
    because the venue may already have acted on a response this module simply cannot trust to
    describe truthfully.
    """


# AC-I5: this requirement is stated here, in the source, as a requirement ON THE ADAPTER, never
# silently assumed true by this module's own code. A non-empty string naming, case-insensitively,
# every one of the four words a probe checks for.
ADAPTER_IDEMPOTENCY_REQUIREMENT = (
    "The execution adapter is REQUIRED to accept the client-minted idempotency key on every "
    "submit_order call and round-trip it unchanged: a retried call carrying the same key must "
    "never place a second order at the venue. This module never assumes that behaviour silently; "
    "it is stated here so the requirement travels with the code that depends on it."
)


def mint_client_order_id() -> str:
    """A fresh idempotency key, minted once per order attempt, before the first check runs (AC-I1).
    A uuid4 string contains no colon (AC-I3), matching paperdb.submit_paper_order's own generator.
    """
    return str(uuid.uuid4())


def evaluate_candidate_chain(candidate: dict) -> Optional[str]:
    """Walk the candidate chain's five legs in section 6.1's own order. Each entry is True, False,
    or the sentinel string 'unreadable'; nautilus_retest_ok may also be None, meaning "leg absent,
    adoption declined", a THIRD state that is skipped entirely rather than treated as a failure.
    The first leg that is not True (skipping a None) returns its own named reason; 'unreadable'
    returns the one shared word for a leg whose record exists but cannot be read, distinct from the
    five leg-specific words. Leg six (the enabling record) is realdb.derive_enabled's own job and is
    never re-tested here.
    """
    legs = (
        ("judge_verdict_ok", "candidate-no-judge-verdict"),
        ("reserve_exam_ok", "candidate-no-reserve-exam"),
        ("nautilus_retest_ok", "candidate-no-nautilus-retest"),
        ("paper_verdict_ok", "candidate-no-paper-verdict"),
        ("promotion_requested", "candidate-no-promotion-request"),
    )
    for key, reason in legs:
        value = candidate[key]
        if value is None:
            # Adoption declined for this leg only (Nautilus): removes the leg, never opens a hole.
            continue
        if value == "unreadable":
            return "candidate-record-unreadable"
        if value is not True:
            return reason
    return None


def promotion_requested_for_run(paper_conn: sqlite3.Connection, paper_run_id: int) -> bool:
    """Leg five, read for real from paper.db's own paper_promotions (DECISIONS #481 item 3): True
    only when the run's newest request row is live, state 'requested' or 'granted'. The newest row
    is the one routes/paper.py _serialize_run reads: ORDER BY requested_at DESC, promotion_id DESC
    LIMIT 1. A declined or withdrawn newest row, or no row at all, reads False.
    """
    row = paper_conn.execute(
        "SELECT state FROM paper_promotions WHERE paper_run_id = ? "
        "ORDER BY requested_at DESC, promotion_id DESC LIMIT 1",
        (paper_run_id,),
    ).fetchone()
    return row is not None and row["state"] in ("requested", "granted")


# Copied verbatim from routes/judge.py _IS_CURRENT_EXPR (the judge page's own "this verdict is the
# current one" rule: not advisory, and no newer non-advisory row on the same five-column key). It is
# copied, not imported, because a route module is not a dependency of the order path. Query it
# FROM research_judge_verdicts AS v.
_VERDICT_IS_CURRENT_EXPR = """(
    v.is_advisory = 0 AND NOT EXISTS (
        SELECT 1 FROM research_judge_verdicts AS newer
         WHERE newer.strategy_id      = v.strategy_id
           AND newer.strategy_version = v.strategy_version
           AND newer.variation_id     = v.variation_id
           AND newer.ticker           = v.ticker
           AND newer.dataset_sha256   = v.dataset_sha256
           AND newer.is_advisory      = 0
           AND (newer.graded_at > v.graded_at
                OR (newer.graded_at = v.graded_at AND newer.id > v.id))
    )
)"""


def _current_pass_verdict_for_run(research_conn: sqlite3.Connection, source_run_id: int) -> bool:
    """True when research_judge_verdicts holds a row for this run whose verdict is 'pass' and which
    is current (see _VERDICT_IS_CURRENT_EXPR). False when there is none."""
    row = research_conn.execute(
        f"""
        SELECT 1 FROM research_judge_verdicts AS v
         WHERE v.run_id = ? AND v.verdict = 'pass' AND {_VERDICT_IS_CURRENT_EXPR}
         LIMIT 1
        """,
        (source_run_id,),
    ).fetchone()
    return row is not None


def read_candidate_chain(research_conn: sqlite3.Connection, paper_conn: sqlite3.Connection,
                         paper_run_id: int) -> dict:
    """The five candidate legs for one paper run, read from our stores (DBG-6c). Each value is
    True, False or 'unreadable', never None (the Nautilus retest leg is adopted, #437 ruling 3).
    research_conn must have row_factory sqlite3.Row.

    Every leg is read in its own try: a sqlite3.DatabaseError makes that leg 'unreadable' and logs
    it. A failed paper_runs read makes all five 'unreadable'. A paper run with no paper_runs row
    makes all five False. No query spans two stores and no paperdb function is called.
    """
    legs = ("judge_verdict_ok", "reserve_exam_ok", "nautilus_retest_ok", "paper_verdict_ok",
            "promotion_requested")
    try:
        run = paper_conn.execute(
            "SELECT source_run_id, strategy_id, strategy_version, end_reason, ended_at "
            "FROM paper_runs WHERE paper_run_id = ?", (paper_run_id,)).fetchone()
    except sqlite3.DatabaseError as exc:
        logger.warning("candidate leg paper_runs unreadable for paper_run_id=%s: %s",
                       paper_run_id, exc)
        return {leg: "unreadable" for leg in legs}
    if run is None:
        return {leg: False for leg in legs}

    chain = {}

    def read_leg(leg, reader):
        try:
            chain[leg] = reader()
        except sqlite3.DatabaseError as exc:
            logger.warning("candidate leg %s unreadable for paper_run_id=%s: %s",
                           leg, paper_run_id, exc)
            chain[leg] = "unreadable"

    def retest_leg():
        state = retestdb.current_retest_state(research_conn, run["source_run_id"])
        if state == "passed":
            return True
        if state == "unreadable-record":
            return "unreadable"
        return False

    read_leg("judge_verdict_ok",
             lambda: _current_pass_verdict_for_run(research_conn, run["source_run_id"]))
    read_leg("reserve_exam_ok",
             lambda: reservedb.reserve_exam_passed(
                 research_conn, run["strategy_id"], run["strategy_version"]))
    read_leg("nautilus_retest_ok", retest_leg)
    chain["paper_verdict_ok"] = run["end_reason"] == "promoted" and run["ended_at"] is not None
    read_leg("promotion_requested", lambda: promotion_requested_for_run(paper_conn, paper_run_id))
    return chain


def _is_finite_signed(value, allow_negative: bool, allow_zero: bool) -> bool:
    """True when value is a finite float (never NaN, never +/-infinity) whose sign is allowed. A
    local copy of the same predicate paperdb.py's own _is_finite_signed applies, kept separate
    rather than imported across a module boundary that marks its own copy private with a leading
    underscore.
    """
    if value is None or not math.isfinite(value):
        return False
    if value < 0 and not allow_negative:
        return False
    if value == 0 and not allow_zero:
        return False
    return True


# Round 1 fold (finding 7 / code-quality SF2): the six hard-limit reason words this function tests
# membership against are imported from realdb.py (SIX_HARD_LIMIT_REASONS above), never spelled out
# a second time here. The per-run slice fence's own word (limit-run-slice) is deliberately NOT in
# that set: the criteria doc's own words are "the per-run slice fence's bound is NOT one of the
# six", so a slice-fence refusal leaves all four breach_* columns NULL, exactly like a phase-A
# refusal.


def _breach_detail(conn: sqlite3.Connection, reason: Optional[str], account_id: str, symbol: str,
                    order_notional: float, order_signed_notional: float, now_iso: str) -> tuple:
    """(configured_value, would_be_figure, reconciliation_reading, reconciliation_reading_at) for
    one of the six AC-L6 hard-limit reasons; four Nones for every other reason (a phase-A refusal,
    or the separate limit-run-slice fence), because AC-L6's own rule is that a check which measured
    no figure at all leaves these four columns absent, never a fabricated zero.
    """
    if reason not in SIX_HARD_LIMIT_REASONS:
        return None, None, None, None

    if reason == LIMIT_PER_ORDER_NOTIONAL:
        configured = real_limits.configured_parameter_value(
            conn, "real_trading.per_order_notional_cap")
        # No reconciliation reading is used by this one check: it compares the order's own
        # notional straight against a configured cap.
        return configured, order_notional, None, None

    if reason in (LIMIT_TICKER_NET_CAP, LIMIT_TOTAL_NET_CAP):
        if reason == LIMIT_TICKER_NET_CAP:
            key = "real_trading.ticker_net_cap"
            reading = real_limits.latest_position_reading(conn, account_id, "ticker", symbol)
            open_intent = real_limits.open_intent_notional(conn, account_id, symbol)
        else:
            key = "real_trading.total_net_cap"
            reading = real_limits.latest_position_reading(conn, account_id, "account", None)
            open_intent = real_limits.open_intent_notional(conn, account_id, None)
        configured = real_limits.configured_parameter_value(conn, key)
        reconciled = reading["net_notional"] if reading is not None else None
        recon_at = reading["recorded_at"] if reading is not None else None
        would_be = None if reconciled is None else reconciled + open_intent + order_signed_notional
        return configured, would_be, reconciled, recon_at

    if reason in (LIMIT_LOSS_WINDOW, LIMIT_LOSS_SINCE_GO):
        run_row = real_limits.latest_completed_monitor_run(conn, account_id)
        current_value = run_row["account_value"] if run_row is not None else None
        recon_at = run_row["ran_at"] if run_row is not None else None
        if reason == LIMIT_LOSS_WINDOW:
            key = "real_trading.loss_limit_window"
            baseline_row = real_limits.latest_window_baseline(conn, account_id)
            baseline_value = baseline_row["account_value"] if baseline_row is not None else None
        else:
            key = "real_trading.loss_limit_since_go"
            enabling_row = real_limits.latest_enabling_record(conn, account_id)
            baseline_value = (enabling_row["account_value_at_go"]
                               if enabling_row is not None else None)
        configured = real_limits.configured_parameter_value(conn, key)
        would_be = (None if (current_value is None or baseline_value is None)
                    else baseline_value - current_value)
        return configured, would_be, current_value, recon_at

    # reason == LIMIT_ORDER_RATE (the only remaining member of SIX_HARD_LIMIT_REASONS)
    configured = real_limits.configured_parameter_value(conn, "real_trading.order_rate_cap")
    count = real_limits.order_rate_count(conn, account_id, now_iso)
    return configured, float(count), None, None


_REFUSED_ROW_INSERT_SQL = (
    "INSERT INTO real_orders (account_id, paper_run_id, symbol, side, "
    "intended_quantity, intended_notional, client_order_id, refusal_reason, "
    "submitting_call_site, recorded_at, breach_configured_value, "
    "breach_would_be_figure, breach_reconciliation_reading, "
    "breach_reconciliation_reading_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _insert_refused_row(conn: sqlite3.Connection, account_id: str, paper_run_id: int, symbol: str,
                         side: str, intended_quantity: float, intended_notional: float,
                         client_order_id: str, reason: str, submitting_call_site: str,
                         now_iso: str, breach_detail: tuple = (None, None, None, None)) -> None:
    """Execute the refused-row INSERT on conn, WITHOUT opening or closing any transaction of its
    own: the caller's own transaction (a phase-A refusal's own short one-statement step below, or
    phase B's check-and-reserve step) is what commits it. breach_detail defaults to all-NULL, the
    shape a phase-A refusal always carries (AC-L6: a check that measured no figure at all leaves
    the four breach_* columns absent, never a fabricated zero).
    """
    configured_value, would_be_figure, reconciliation_reading, reconciliation_reading_at = \
        breach_detail
    conn.execute(
        _REFUSED_ROW_INSERT_SQL,
        (account_id, paper_run_id, symbol, side, intended_quantity, intended_notional,
         client_order_id, reason, submitting_call_site, now_iso, configured_value,
         would_be_figure, reconciliation_reading, reconciliation_reading_at),
    )


def _write_phase_a_refusal(conn: sqlite3.Connection, account_id: str, paper_run_id: int,
                            symbol: str, side: str, intended_quantity: float,
                            intended_notional: float, client_order_id: str, reason: str,
                            submitting_call_site: str, now_iso: str) -> None:
    """Write and commit ONE phase-A refused row in its own short transaction (BEGIN IMMEDIATE,
    insert, COMMIT), never sharing scope with phase B's own check-and-reserve transaction below
    (round 1 fold, finding 4: AC-L5's own literal words are "opened after phase A's last available
    check", and phase A's three checks here read no state a concurrent order could race against
    the way phase B's account caps do, so each phase-A refusal is its own short atomic step rather
    than widening phase B's transaction to cover it too).
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        _insert_refused_row(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                             intended_notional, client_order_id, reason, submitting_call_site,
                             now_iso)
        conn.execute("COMMIT;")
    except (sqlite3.Error, sqlite3.Warning):
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise


def _order_input_reason(side, intended_quantity, intended_notional) -> tuple:
    """Check the order's own numbers, before any limit's arithmetic touches them. Returns a pair
    (reason, row_writable). A side that is not 'buy' or 'sell', or a quantity or notional that is
    not an int or float (None, a string, a bool) or is NaN or infinite, gives
    ('order-input-invalid', False): the refused row cannot hold such an input (the side CHECK, the
    REAL NOT NULL columns, NaN stored as NULL), so no row is written. Finite numbers with a valid
    side but a quantity or notional of 0 or below give ('order-input-invalid', True): a refused
    row can hold them. Otherwise (None, True). A real order of 0 shares or 0 notional is invalid.
    """
    if side not in ("buy", "sell"):
        return "order-input-invalid", False
    for value in (intended_quantity, intended_notional):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "order-input-invalid", False
        if not math.isfinite(value):
            return "order-input-invalid", False
    if not (_is_finite_signed(intended_quantity, allow_negative=False, allow_zero=False)
            and _is_finite_signed(intended_notional, allow_negative=False, allow_zero=False)):
        return "order-input-invalid", True
    return None, True


ORDER_NOT_WHOLE_SHARES = "order-not-whole-shares"


def _whole_share_reason(intended_quantity) -> Optional[str]:
    """B8, the last phase B check (AC-A2): a real order is expressed in whole shares, so a
    quantity that is not a whole number refuses 'order-not-whole-shares'. A float that holds a
    whole number (3.0) passes. B1 has already proved the quantity is a finite, positive number.
    """
    if not float(intended_quantity).is_integer():
        return ORDER_NOT_WHOLE_SHARES
    return None


def _refuse_without_row(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                         intended_notional, client_order_id, reason, submitting_call_site,
                         now_iso) -> None:
    """Stand in for _write_phase_a_refusal when the order's input cannot be stored: same
    signature, writes nothing. The caller raises RealOrderRefused right after, so the refusal is
    loud, only rowless.
    """
    return None


def submit_real_order(conn: sqlite3.Connection, paper_conn: sqlite3.Connection, account_id: str,
                       paper_run_id: int, symbol: str, side: str, intended_quantity: float,
                       intended_notional: float, adapter, now_iso: str,
                       submitting_call_site: str, research_conn: sqlite3.Connection) -> dict:
    """The composed gate plus the three-step order path (AC-A2's available checks tonight, AC-O2,
    AC-L5). conn is the real-trading store's own connection; paper_conn is a second, independent
    connection onto paper.db, used only for the slice fence and the promotion leg.

    Step 0: the gate refuses to RUN at all (raises, writes nothing) when any of the nine
    section-16 parameters is unconfigured (unchanged from night one's own order gate).
    Step 1: the idempotency key is minted before any check below runs (AC-I1), so a refused row
    carries the same key a filled one would have.
    Phase A (five checks now, night three's own two added: the enabling record, the kill switch,
    reconciliation freshness, the candidate chain, the adapter connection) each refuse, when they
    refuse at all, by writing and committing their OWN refused row in its own short transaction
    (round 1 fold, finding 4), before phase B's own transaction ever opens.
    Phase B (step 5, first failure wins) and the intent-row write run inside ONE transaction,
    opened only AFTER phase A's last available check returns, closed on whichever row commits
    (AC-L5): a REFUSED row commits and this function raises RealOrderRefused with nothing further
    attempted, or the INTENT row commits and step 7 runs next with nothing of this module's own
    left open on either connection (AC-O4).
    """
    # Step 0.
    assert_parameters_configured(conn)

    # Step 1.
    client_order_id = mint_client_order_id()

    # The order's own numbers are checked once, here; the verdict is used by every phase A refusal
    # (an unwritable input writes no row) and by B1 below.
    input_reason, row_writable = _order_input_reason(side, intended_quantity, intended_notional)
    write_refusal = _write_phase_a_refusal if row_writable else _refuse_without_row

    # Phase A 1: the enabling record. A refusal here is written and committed in its own short
    # transaction (see _write_phase_a_refusal's own docstring for why), never inside phase B's.
    enabled, enabled_reason = derive_enabled(conn, account_id, now_iso)
    if not enabled:
        write_refusal(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                                intended_notional, client_order_id, enabled_reason,
                                submitting_call_site, now_iso)
        raise RealOrderRefused(enabled_reason)

    # Phase A 2: the kill switch.
    kill_row = get_kill_state(conn, account_id)
    kill_reason = kill_check_reason(kill_row)
    if kill_reason is not None:
        write_refusal(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                                intended_notional, client_order_id, kill_reason,
                                submitting_call_site, now_iso)
        raise RealOrderRefused(kill_reason)

    # Phase A 3 (night three, AC-5): reconciliation freshness. Wired between the kill-state check
    # and the candidate chain (AC-18's own fixed order). The parameter carries no default anywhere
    # (AC-P1 already refused at step 0 if it were unconfigured, so a NULL reading here can only mean
    # the caller reached this point some other way, whether a race between step 0's own read and this
    # one or a caller that bypasses step 0 entirely). Round 4 fold: real_reconciliation's own
    # reconciliation_freshness_reason DOES re-check for that NULL itself now, refusing by name
    # ('reconciliation-max-age-absent') rather than crashing comparing None to a number, so this
    # check never guesses a bound of its own; it only asks the function to answer, whatever it reads.
    reconciliation_max_age = real_limits.configured_parameter_value(
        conn, "real_trading.reconciliation_max_age")
    reconciliation_reason = real_reconciliation.reconciliation_freshness_reason(
        conn, account_id, now_iso, reconciliation_max_age)
    if reconciliation_reason is not None:
        write_refusal(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                                intended_notional, client_order_id, reconciliation_reason,
                                submitting_call_site, now_iso)
        raise RealOrderRefused(reconciliation_reason)

    # Phase A 4: the candidate chain.
    chain_reason = evaluate_candidate_chain(
        read_candidate_chain(research_conn, paper_conn, paper_run_id))
    if chain_reason is not None:
        write_refusal(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                                intended_notional, client_order_id, chain_reason,
                                submitting_call_site, now_iso)
        raise RealOrderRefused(chain_reason)

    # Phase A 5 (night three, AC-17, AC-F3): the adapter's own connection state, the LAST phase-A
    # check, run after the candidate chain and before phase B's transaction ever opens. Only the
    # literal word 'connected' permits; every other word, and a raised exception, refuse identically
    # (AC-F3's own "a report that raises instead of answering is the same refusal").
    try:
        connection_state = adapter.connection_state()
    except AdapterCannotDetermine:
        # Round 2 fold, finding 4: a real adapter is expected to raise AdapterCannotDetermine when
        # it cannot answer whether it is connected, the SAME named exception every other adapter
        # capability in this module already raises for "I do not know" (report_account_value,
        # report_account_state, lookup_order), never a bare built-in RuntimeError. Catching this
        # specific, project-owned class here, rather than the much wider RuntimeError it happens to
        # subclass, means a genuinely different exception (a real bug in the adapter, a type error,
        # anything this module did not anticipate) PROPAGATES rather than being silently absorbed
        # as just another refusal; AC-F3's own "a report that raises instead of answering is the
        # same refusal" governs this one documented family, not every possible exception. The
        # exception is never swallowed silently either way: this refusal IS the handling, recorded
        # and raised as RealOrderRefused below.
        connection_state = None
    if connection_state != "connected":
        write_refusal(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                                intended_notional, client_order_id, "adapter-unavailable",
                                submitting_call_site, now_iso)
        raise RealOrderRefused("adapter-unavailable")

    # Phase B1: the order's own numbers, before every limit and before any transaction opens. A
    # writable input gets its refused row in its own short transaction, like a phase A refusal;
    # an unwritable one gets no row. No transaction is left open either way.
    if input_reason is not None:
        write_refusal(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                      intended_notional, client_order_id, input_reason, submitting_call_site,
                      now_iso)
        raise RealOrderRefused(input_reason)

    # A buy adds exposure, a sell reduces it: both position caps compare a PROJECTED net figure,
    # so the order's own contribution to that projection must carry the matching sign. Computed
    # only after B1, so no unchecked number reaches this arithmetic.
    order_signed_notional = intended_notional if side == "buy" else -intended_notional

    # Phase B (AC-L5): the transaction opens HERE, only after phase A's last available check
    # (the adapter connection check, just above) has already returned None, and closes on whichever
    # row this block commits below; no adapter or network call happens between the two.
    conn.execute("BEGIN IMMEDIATE;")
    try:
        # AC-A2's fixed order: B2 rate, B3 loss (window, then since go), B4 per-order notional,
        # B5 slice fence, B6 per-ticker cap, B7 total cap, B8 whole shares last.
        reason = real_limits.check_order_rate_cap(conn, account_id, now_iso)
        if reason is None:
            reason = real_limits.check_loss_limit_window(conn, account_id, now_iso)
        if reason is None:
            reason = real_limits.check_loss_limit_since_go(conn, account_id, now_iso)
        if reason is None:
            reason = real_limits.check_per_order_notional_cap(conn, intended_notional)
        if reason is None:
            reason = real_limits.check_slice_fence(conn, paper_conn, paper_run_id,
                                                     intended_notional)
        if reason is None:
            reason = real_limits.check_ticker_net_cap(conn, account_id, symbol,
                                                        order_signed_notional)
        if reason is None:
            reason = real_limits.check_total_net_cap(conn, account_id, order_signed_notional)
        if reason is None:
            reason = _whole_share_reason(intended_quantity)

        if reason is not None:
            breach = _breach_detail(conn, reason, account_id, symbol, intended_notional,
                                     order_signed_notional, now_iso)
            _insert_refused_row(conn, account_id, paper_run_id, symbol, side, intended_quantity,
                                 intended_notional, client_order_id, reason, submitting_call_site,
                                 now_iso, breach_detail=breach)
            conn.execute("COMMIT;")
            raise RealOrderRefused(reason)

        # Every check passed: STEP 1 of the three-step order path (AC-O2). Every optional column
        # stays NULL: nothing about this order is known yet except that it passed every check.
        conn.execute(
            "INSERT INTO real_orders (account_id, paper_run_id, symbol, side, intended_quantity, "
            "intended_notional, client_order_id, submitting_call_site, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, paper_run_id, symbol, side, intended_quantity, intended_notional,
             client_order_id, submitting_call_site, now_iso),
        )
        conn.execute("COMMIT;")
    except (sqlite3.Error, sqlite3.Warning):
        # A genuine database fault mid-check (a constraint violation, a lock timeout): undo
        # whatever this transaction holds, matching realdb.real_transaction's own narrowed
        # except, and re-raise unchanged. RealOrderRefused itself never reaches this clause: it is
        # raised only after its own COMMIT already succeeded, above.
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise

    # Round 1 fold, finding 5: the instant before the adapter call, captured as its own named
    # value rather than left for the UPDATE below to imply. This function models one logical
    # instant per call (now_iso is the caller's own "now" for the whole attempt, exactly as
    # paperdb.submit_paper_order's own step 3 reuses one now_iso for both submitted_at and
    # filled_at), so submitted_at below IS that captured instant, not a second clock read.
    submitted_at = now_iso

    # STEP 2 (AC-O2, AC-O4): the venue is called with our own key and with nothing of this
    # module's own open on either connection, so nothing spans the call (this file's own top
    # docstring, Kleppmann chapter 8's idempotency quote).
    response = adapter.submit_order(symbol, side, intended_quantity, client_order_id)

    # AC-O6, widened by round 1 fold finding 1: every field the venue reports is validated BEFORE
    # anything is stored, including 'accepted', which used to default permissively to True when
    # absent. A missing or non-bool 'accepted' is exactly as untrustworthy as a NaN quantity: the
    # response writes nothing at all, the row is left exactly as step 1 committed it, because the
    # venue may already have acted on an answer this module cannot trust to describe truthfully.
    submitted_quantity = response.get("submitted_quantity")
    filled_quantity = response.get("filled_quantity")
    accepted = response.get("accepted")
    if not (
        _is_finite_signed(submitted_quantity, allow_negative=False, allow_zero=True)
        and _is_finite_signed(filled_quantity, allow_negative=False, allow_zero=True)
        and isinstance(accepted, bool)
    ):
        raise RealOrderVenueResponseInvalid(
            f"account_id={account_id!r} symbol={symbol!r} client_order_id={client_order_id!r}: "
            "the adapter's response must report a finite, non-negative submitted_quantity and "
            "filled_quantity, and a present, boolean 'accepted' field, but got "
            f"submitted_quantity={submitted_quantity!r}, filled_quantity={filled_quantity!r}, "
            f"accepted={accepted!r} (type {type(accepted).__name__}); leaving the "
            "already-committed intent row open rather than storing a value that may not be true"
        )

    # STEP 3 (AC-O2), a fresh short transaction: UPDATE the SAME row, matched by client_order_id,
    # never a second INSERT. terminal_state is written only for a response reporting a rejection
    # with no fill (AC-O3's own one case this lane's build may write); every other response,
    # including a full fill, leaves the row as OPEN INTENT with its reservation intact.
    reports_no_fill = filled_quantity is None or filled_quantity == 0.0
    terminal_state = "rejected" if (not accepted and reports_no_fill) else None

    # W-14, night three (lane RT3): the venue's own acknowledgement and its own fill price, stored
    # alongside the same fields step 3 already wrote. A genuine fill's own fill_price is validated
    # finite and positive before storage, exactly like every other numeric field above (AC-O6); a
    # response reporting no fill carries no fill price to validate at all.
    fill_price = response.get("fill_price")
    if not reports_no_fill and not _is_finite_signed(fill_price, allow_negative=False,
                                                       allow_zero=False):
        raise RealOrderVenueResponseInvalid(
            f"account_id={account_id!r} symbol={symbol!r} client_order_id={client_order_id!r}: "
            "the adapter reported a non-zero fill but its own fill_price is not a finite, "
            f"positive number: fill_price={fill_price!r}; leaving the already-committed intent "
            "row open rather than storing a value that may not be true"
        )

    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute(
            "UPDATE real_orders SET submitted_at = ?, submitted_quantity = ?, "
            "filled_quantity = ?, venue_order_id = ?, resolved_at = ?, terminal_state = ?, "
            "venue_accepted = ?, fill_price = ? WHERE client_order_id = ?",
            (submitted_at, submitted_quantity, filled_quantity, response.get("venue_order_id"),
             now_iso, terminal_state, int(accepted), fill_price if not reports_no_fill else None,
             client_order_id),
        )
        conn.execute("COMMIT;")
    except (sqlite3.Error, sqlite3.Warning):
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise

    return {"authorized": True, "client_order_id": client_order_id, "terminal_state": terminal_state}
