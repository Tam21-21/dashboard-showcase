"""real_limits.py: the six hard money-path limits (brief section 12.1) plus the per-run slice fence,
lane RL, night two (docs/plans/night/2026-09-12_set7a_night/PLAN.md section 8).

CORE SAFETY RULE, repeated at every function below rather than assumed once: a missing or unreadable
limit, position figure or account-value reading must never be read as an infinite or permissive one.
Every function here that "cannot decide" returns a specific named reason string instead of guessing
None (permit). None is returned ONLY when a real comparison was made and the order sits inside the
configured bound.

Every check function is pure-ish: it takes a database connection and returns a refusal-reason string
or None, and it NEVER raises for a configured-but-breached limit (mirroring realdb.kill_check_reason's
own "return the word, never raise" shape, so real_orders.py composes these checks in a fixed order
without a chain of try/except).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

# Round 1 fold (finding 7 / code-quality SF2): the six hard-limit reason words are defined ONCE in
# realdb.py, beside its own other word tuples, and imported here rather than spelled out again as
# six separate literals; real_orders.py imports the same six names for its own breach-detail
# classification, so the two files can never drift apart on what the closed set is.
from .realdb import (
    AdapterCannotDetermine,
    command_kill,
    LIMIT_PER_ORDER_NOTIONAL,
    LIMIT_TICKER_NET_CAP,
    LIMIT_TOTAL_NET_CAP,
    LIMIT_LOSS_WINDOW,
    LIMIT_LOSS_SINCE_GO,
    LIMIT_ORDER_RATE,
)
from . import real_reconciliation

# The one fixed timestamp format every module in this lane's own build uses (matching realdb.py's
# own _iso_seconds_between and the probe suite's own now_iso() fixture): a naive strptime compares
# correctly without a timezone object because every caller uses this same format.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _seconds_between(later_iso: str, earlier_iso: str) -> float:
    """Seconds between two of this module's own timestamp strings, later minus earlier. A private
    copy of realdb._iso_seconds_between's own arithmetic rather than an import of a name that
    module marks private with its own leading underscore: two modules that both need one small,
    stateless calculation are better off each owning their own than reaching into each other's
    internals for it.
    """
    later = datetime.strptime(later_iso, _ISO_FORMAT)
    earlier = datetime.strptime(earlier_iso, _ISO_FORMAT)
    return (later - earlier).total_seconds()


# =============================================================================
# Small public reads, reused both by the check functions below and by real_orders.py's own
# breach-detail builder (AC-L6), so the same query is never written out twice.
# =============================================================================

def configured_parameter_value(conn: sqlite3.Connection, key: str) -> Optional[float]:
    """The current value of one section-16 parameter. Read fresh on every call, never cached: a
    caller upstream (submit_real_order's own step 0) is what guarantees all nine are configured
    before any check here runs, so this function itself does not re-guard that; it simply reads
    whatever real_trading_parameters currently holds for this key.
    """
    row = conn.execute(
        "SELECT value FROM real_trading_parameters WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else None


def latest_position_reading(conn: sqlite3.Connection, account_id: str, scope: str,
                             ticker: Optional[str] = None) -> Optional[sqlite3.Row]:
    """The newest real_position_readings row (by reading_id) for (account_id, scope, ticker), or
    None when no such row has ever been written (E6's own empty-set answer: no row reads exactly
    like a NULL net_notional to every caller of this function).
    """
    return conn.execute(
        "SELECT net_notional, absent_reason, recorded_at FROM real_position_readings "
        "WHERE account_id = ? AND scope = ? AND ticker IS ? "
        "ORDER BY reading_id DESC LIMIT 1",
        (account_id, scope, ticker),
    ).fetchone()


def open_intent_notional(conn: sqlite3.Connection, account_id: str,
                          symbol: Optional[str] = None) -> float:
    """The sum of intended_notional across this account's own OPEN INTENT rows (AC-L4's own idiom:
    refusal_reason IS NULL AND terminal_state IS NULL), optionally narrowed to one symbol for the
    per-ticker cap. Summed fresh from the order log on every call, nothing cached, and 0.0 for the
    empty set rather than an absent figure (AC-L1's own "an empty set never means no limit
    applies").
    """
    if symbol is None:
        row = conn.execute(
            "SELECT COALESCE(SUM(intended_notional), 0.0) AS total FROM real_orders "
            "WHERE account_id = ? AND refusal_reason IS NULL AND terminal_state IS NULL",
            (account_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(intended_notional), 0.0) AS total FROM real_orders "
            "WHERE account_id = ? AND symbol = ? AND refusal_reason IS NULL "
            "AND terminal_state IS NULL",
            (account_id, symbol),
        ).fetchone()
    return row["total"]


def open_intent_notional_for_run(conn: sqlite3.Connection, paper_run_id: int) -> float:
    """The same open-intent sum as open_intent_notional above, but scoped to one paper_run_id
    (AC-L2's own fold): the per-run slice fence must never see another run's own reservations.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(intended_notional), 0.0) AS total FROM real_orders "
        "WHERE paper_run_id = ? AND refusal_reason IS NULL AND terminal_state IS NULL",
        (paper_run_id,),
    ).fetchone()
    return row["total"]


def latest_completed_monitor_run(conn: sqlite3.Connection, account_id: str) -> Optional[sqlite3.Row]:
    """The newest COMPLETED real_loss_monitor_runs row for account_id (AC-L13: a failed run is
    never read as the gate's own account-value input), or None when no completed run exists at all
    ("never-run", E10's own empty-set answer, derived rather than stored).
    """
    return conn.execute(
        "SELECT run_id, ran_at, account_value FROM real_loss_monitor_runs "
        "WHERE account_id = ? AND completed = 1 ORDER BY run_id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def latest_window_baseline(conn: sqlite3.Connection, account_id: str,
                            window_start: Optional[str] = None) -> Optional[sqlite3.Row]:
    """The window baseline row for account_id: an EXACT match on window_start when it is given
    (never falls back to an older window's row, AC-L10), or the newest row for the account when it
    is omitted (the order path's own call, which has no window schedule to consult, per AC-L10's
    own OWED note: the newest row that was actually rolled into is the only "current" this lane's
    build can honestly compute).
    """
    if window_start is not None:
        return conn.execute(
            "SELECT account_value, read_at FROM real_loss_window_baselines "
            "WHERE account_id = ? AND window_start = ? ORDER BY baseline_id DESC LIMIT 1",
            (account_id, window_start),
        ).fetchone()
    return conn.execute(
        "SELECT account_value, read_at FROM real_loss_window_baselines "
        "WHERE account_id = ? ORDER BY baseline_id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def latest_enabling_record(conn: sqlite3.Connection, account_id: str) -> Optional[sqlite3.Row]:
    """The newest real_enabling_records row for account_id, or None when no record has ever been
    appended. Read-only, matching realdb.derive_enabled's own single SELECT against this table:
    nothing in this lane's own build ever writes to real_enabling_records.
    """
    return conn.execute(
        "SELECT account_value_at_go FROM real_enabling_records WHERE account_id = ? "
        "ORDER BY enabling_record_id DESC LIMIT 1",
        (account_id,),
    ).fetchone()


def order_rate_count(conn: sqlite3.Connection, account_id: str, now_iso: str) -> int:
    """The count of this account's own real_orders rows recorded within ORDER_RATE_WINDOW_SECONDS
    of now_iso (trailing window, never counting a row recorded after now_iso). Summed fresh on
    every call, matching open_intent_notional's own "nothing cached" discipline.
    """
    now_dt = datetime.strptime(now_iso, _ISO_FORMAT)
    threshold_iso = (now_dt - timedelta(seconds=ORDER_RATE_WINDOW_SECONDS)).strftime(_ISO_FORMAT)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM real_orders WHERE account_id = ? AND recorded_at >= ? "
        "AND recorded_at <= ?",
        (account_id, threshold_iso, now_iso),
    ).fetchone()
    return row["n"]


# An operational dial (DECISIONS #204: "needs only inline justification"), never a section-16
# parameter: it decides the SAMPLING WINDOW the rate is measured over, never whether an order
# passes, which is what real_trading.order_rate_cap alone decides. 60 seconds is a plain,
# round trailing window a human can reason about ("orders in the last minute"); the CAP itself is
# the safety-relevant number and it is Tamer's own configured parameter, not this constant.
ORDER_RATE_WINDOW_SECONDS = 60


# =============================================================================
# AC-L13: the gate's own account-value input. Read in a fixed order: current value, then the
# current window's baseline, matching the brief's own named order.
# =============================================================================

def read_current_account_value(conn: sqlite3.Connection, account_id: str,
                                 now_iso: str) -> tuple:
    """(value, reason): value is None whenever reason is not None, and vice versa. AC-L13's three
    outcomes: no completed real_loss_monitor_runs row -> (None, 'account-value-never-read'); the
    newest completed row older than the configured real_trading.account_value_max_age ->
    (None, 'account-value-stale'); that same row older than the CURRENT window's baseline row (when
    one exists at all; an account that has never rolled a window has nothing to compare against, so
    this second staleness test is simply skipped rather than treated as a third kind of failure) ->
    (None, 'account-value-stale') again; otherwise (value, None).
    """
    run_row = latest_completed_monitor_run(conn, account_id)
    if run_row is None:
        return None, "account-value-never-read"

    max_age = configured_parameter_value(conn, "real_trading.account_value_max_age")
    age_seconds = _seconds_between(now_iso, run_row["ran_at"])
    if age_seconds > max_age:
        return None, "account-value-stale"

    # The staleness-against-the-window-boundary test only applies when a window has actually been
    # rolled at least once; an account that has never rolled a window has no baseline row to
    # compare against, so nothing here can be "older than" it.
    baseline_row = latest_window_baseline(conn, account_id)
    if baseline_row is not None:
        run_dt = datetime.strptime(run_row["ran_at"], _ISO_FORMAT)
        baseline_dt = datetime.strptime(baseline_row["read_at"], _ISO_FORMAT)
        if run_dt < baseline_dt:
            return None, "account-value-stale"

    return run_row["account_value"], None


# =============================================================================
# The six hard limits (brief AC-L1), each its own named refusal reason.
# =============================================================================

def check_per_order_notional_cap(conn: sqlite3.Connection, order_notional: float) -> Optional[str]:
    """'limit-per-order-notional' when order_notional exceeds the configured cap."""
    cap = configured_parameter_value(conn, "real_trading.per_order_notional_cap")
    if order_notional > cap:
        return LIMIT_PER_ORDER_NOTIONAL
    return None


def check_ticker_net_cap(conn: sqlite3.Connection, account_id: str, ticker: str,
                          order_signed_notional: float) -> Optional[str]:
    """'position-figure-absent' when no reconciled reading exists for this ticker, or the reading
    itself is stored NULL with a reason (AC-L3: never read as zero, which would silently permit).
    Otherwise 'limit-ticker-net-cap' when the reconciled figure, plus this account's own open-intent
    notional on this ticker, plus the order's own signed notional, exceeds the configured cap.
    """
    reading = latest_position_reading(conn, account_id, "ticker", ticker)
    if reading is None or reading["net_notional"] is None:
        return "position-figure-absent"
    cap = configured_parameter_value(conn, "real_trading.ticker_net_cap")
    projected = reading["net_notional"] + open_intent_notional(conn, account_id, ticker) + \
        order_signed_notional
    if projected > cap:
        return LIMIT_TICKER_NET_CAP
    return None


def check_total_net_cap(conn: sqlite3.Connection, account_id: str,
                         order_signed_notional: float) -> Optional[str]:
    """Same shape as check_ticker_net_cap, reading the account-total row (scope='account', ticker
    NULL there by the table's own CHECK) and summing open-intent notional across every ticker.
    """
    reading = latest_position_reading(conn, account_id, "account", None)
    if reading is None or reading["net_notional"] is None:
        return "position-figure-absent"
    cap = configured_parameter_value(conn, "real_trading.total_net_cap")
    projected = reading["net_notional"] + open_intent_notional(conn, account_id, None) + \
        order_signed_notional
    if projected > cap:
        return LIMIT_TOTAL_NET_CAP
    return None


def check_loss_limit_window(conn: sqlite3.Connection, account_id: str, now_iso: str,
                             window_start: Optional[str] = None) -> Optional[str]:
    """read_current_account_value runs first (its own reason, if any, propagates unchanged).
    Otherwise reads the window baseline (exact match on window_start when given, the newest row
    for the account when omitted, never falling back to an OLDER window's row when an exact match
    was asked for and missing): absent -> 'loss-baseline-window-absent'; otherwise
    'limit-loss-window' when baseline minus current exceeds the configured
    real_trading.loss_limit_window.
    """
    value, reason = read_current_account_value(conn, account_id, now_iso)
    if reason is not None:
        return reason
    baseline_row = latest_window_baseline(conn, account_id, window_start)
    if baseline_row is None:
        return "loss-baseline-window-absent"
    cap = configured_parameter_value(conn, "real_trading.loss_limit_window")
    drop = baseline_row["account_value"] - value
    if drop > cap:
        return LIMIT_LOSS_WINDOW
    return None


def check_loss_limit_since_go(conn: sqlite3.Connection, account_id: str,
                               now_iso: str) -> Optional[str]:
    """Same shape as check_loss_limit_window, but the baseline is the newest enabling record's own
    account_value_at_go (never a different record, never falls back): absent or NULL ->
    'loss-baseline-since-go-absent'; otherwise 'limit-loss-since-go' when the drop exceeds the
    configured real_trading.loss_limit_since_go.
    """
    value, reason = read_current_account_value(conn, account_id, now_iso)
    if reason is not None:
        return reason
    enabling_row = latest_enabling_record(conn, account_id)
    if enabling_row is None or enabling_row["account_value_at_go"] is None:
        return "loss-baseline-since-go-absent"
    cap = configured_parameter_value(conn, "real_trading.loss_limit_since_go")
    drop = enabling_row["account_value_at_go"] - value
    if drop > cap:
        return LIMIT_LOSS_SINCE_GO
    return None


def check_order_rate_cap(conn: sqlite3.Connection, account_id: str, now_iso: str) -> Optional[str]:
    """'limit-order-rate' when the count of this account's own orders inside the trailing
    ORDER_RATE_WINDOW_SECONDS window is at or past the configured real_trading.order_rate_cap.
    """
    cap = configured_parameter_value(conn, "real_trading.order_rate_cap")
    count = order_rate_count(conn, account_id, now_iso)
    if count >= cap:
        return LIMIT_ORDER_RATE
    return None


# =============================================================================
# PASS-6 and PASS-7 (brief AC-L10, AC-L11, AC-L12): the two scheduled passes over account state.
# =============================================================================

def _record_roll_failure(conn: sqlite3.Connection, account_id: str, window_start,
                          reason: str, recorded_at: str) -> None:
    """W-25's own home for a failed roll (fold S1). Additive, append-only: no function in this
    module ever updates or deletes a row here, matching every other E-numbered table's own rule.
    """
    conn.execute(
        "INSERT INTO real_loss_window_roll_failures (account_id, window_start, reason, "
        "recorded_at) VALUES (?, ?, ?, ?)",
        (account_id, window_start, reason, recorded_at),
    )


def latest_roll_failure(conn: sqlite3.Connection, account_id: str, window_start) -> Optional[dict]:
    """The newest recorded roll failure for (account_id, window_start), as a plain dict
    {'reason': ..., 'recorded_at': ...}, or None when no failure has ever been recorded for that
    exact key (AC-24: this tells apart "nothing happened yet" from "no failure exists" from
    "a failure exists", by returning None only for the first two and a real dict for the third).
    window_start may itself be None (the tick-level shape's own key, AC-19); SQLite's own "IS"
    operator compares NULL-safely, matching latest_position_reading's own "ticker IS ?" idiom.
    """
    row = conn.execute(
        "SELECT reason, recorded_at FROM real_loss_window_roll_failures "
        "WHERE account_id = ? AND window_start IS ? ORDER BY failure_id DESC LIMIT 1",
        (account_id, window_start),
    ).fetchone()
    if row is None:
        return None
    return {"reason": row["reason"], "recorded_at": row["recorded_at"]}


def run_window_roll(conn: sqlite3.Connection, account_id: str, adapter, now_iso: str,
                     window_start: Optional[str] = None) -> dict:
    """PASS-6, in two shapes sharing one function (night three, lane RT3, fold S1, AC-19, AC-24).

    window_start=None is the TICK-LEVEL shape the scheduled per-account pass calls (AC-19): the
    store holds real_trading.loss_limit_window as one bare number with no window length of its own,
    so no boundary can ever be derived here, and this NEVER calls the adapter at all. It refuses one
    of two named reasons (loss-window-parameter-absent when the value is NULL,
    loss-window-length-absent when it is a configured number that still carries no window boundary).

    Round 3 fold, finding 1 (SHOULD-FIX, unbounded growth): run_loss_monitor_pass now calls this
    shape on every tick (round 2's own fold), so an enabled account whose loss window stays unset
    would otherwise write one real_loss_window_roll_failures row per minute forever, making the
    real_window_roll_failures monitor count meaningless (it would just count minutes elapsed, not
    distinct failures). This shape now reads latest_roll_failure(conn, account_id, None) FIRST, the
    same suppression idiom the explicit-window shape below already uses for its own retries, and
    records a NEW row only when no failure stands yet or the standing one carries a DIFFERENT reason
    (the parameter moved from unset to a number, or back): a repeat of the SAME reason returns the
    standing failure's own reason without writing. Either way the return shape is unchanged:
    {'completed': False, 'reason': ..., 'baseline_id': None}.

    window_start=<an explicit ISO string> is the LOW-LEVEL shape AC-24 drives directly: fold S1's
    own retry suppression checks latest_roll_failure for this EXACT window first, and if this window
    has already failed, returns that same failure without calling the adapter a second time. Only a
    window with no prior failure ever reaches adapter.report_account_value() (AC-L12's second
    reading). On success, writes ONE real_loss_window_baselines row and returns
    {'completed': True, 'baseline_id': ...}. On AdapterCannotDetermine, writes NOTHING to that table
    (E9's own NULL answer: a failed roll never fabricates a row it could not read a value for),
    records the failure, and returns {'completed': False, 'reason': ..., 'baseline_id': None}.
    """
    if window_start is None:
        configured = configured_parameter_value(conn, "real_trading.loss_limit_window")
        reason = ("loss-window-parameter-absent" if configured is None
                  else "loss-window-length-absent")
        # Finding 1: only write a new failure row when the tick-level key (window_start=None) has
        # never failed before, or its standing reason differs from what the parameter says today.
        # A same-reason repeat is suppressed (no write), the same idiom the explicit-window branch
        # below already uses, so one unset parameter does not grow this table once per minute.
        standing_failure = latest_roll_failure(conn, account_id, None)
        if standing_failure is None or standing_failure["reason"] != reason:
            _record_roll_failure(conn, account_id, None, reason, now_iso)
            return {"completed": False, "reason": reason, "baseline_id": None}
        return {"completed": False, "reason": standing_failure["reason"], "baseline_id": None}

    existing_failure = latest_roll_failure(conn, account_id, window_start)
    if existing_failure is not None:
        return {"completed": False, "reason": existing_failure["reason"], "baseline_id": None}

    try:
        value = adapter.report_account_value()
    except AdapterCannotDetermine:
        reason = "loss-window-account-value-unreadable"
        _record_roll_failure(conn, account_id, window_start, reason, now_iso)
        return {"completed": False, "reason": reason, "baseline_id": None}
    cur = conn.execute(
        "INSERT INTO real_loss_window_baselines (account_id, window_start, account_value, "
        "read_at) VALUES (?, ?, ?, ?)",
        (account_id, window_start, value, now_iso),
    )
    return {"completed": True, "baseline_id": cur.lastrowid}


def run_loss_monitor_pass(conn: sqlite3.Connection, account_id: str, adapter,
                           now_iso: str) -> dict:
    """The widened per-account tick body (round 2 fold, findings 2: AC-1, AC-19). Runs the
    reconciliation sweep FIRST, then the window-roll step, then PASS-7's own body, in that fixed
    order, every call, with this function's OWN signature left unchanged so main.py's existing
    call site (dashboard.backend.main, every 60 seconds per account) needs no edit at all: this
    lane's own areas do not include main.py, and the lane fence enforces that boundary as a write
    boundary, not merely a convention. Tickers for the sweep are derived HERE, inside this
    function, from real_orders' own distinct symbols for the account (the same query
    realdb.command_kill already runs for its own "after every kill action" trigger), so no caller
    anywhere has to know or pass them.

    This REPLACES round 1's own standalone run_scheduled_account_passes, which nothing in the tree
    ever called (main.py's own tick calls this function's name, not that one): folding the
    composition inside THIS function's own body is what actually wires it into the live tick,
    rather than adding a second, unreached entry point beside it.

    PASS-7 itself: reads adapter.report_account_value() (AC-L12's third reading) unconditionally,
    regardless of whether any order was ever submitted for this account. On AdapterCannotDetermine
    records a FAILED run (completed=False, armed=False) and does NOT arm: the one place failing
    closed does not mean refusing, it means recording that nothing could be evaluated this pass.

    On success, computes both drops against the current window's baseline and the newest enabling
    record's own account_value_at_go. A baseline missing on EITHER side means this pass cannot
    evaluate a breach at all (recorded as completed=True, armed=False, per the brief's own words:
    "it records that it could not evaluate rather than recording no breach"). Arms HALT (never
    flatten, DECISIONS #392 ruling 5) when either drop exceeds its OWN configured limit.
    Every call appends exactly one real_loss_monitor_runs row, breach or not, completed or not
    (E10's own empty-set and NULL answers). The return dict keeps completed/armed/run_id (PASS-7's
    own, unchanged keys) and gains 'reconciliation' and 'window_roll', the two new steps' own
    results, so an existing caller reading the old three keys is unaffected.
    """
    known_tickers = [row["symbol"] for row in conn.execute(
        "SELECT DISTINCT symbol FROM real_orders WHERE account_id = ?", (account_id,)
    ).fetchall()]
    reconciliation = real_reconciliation.run_reconciliation_sweep(
        conn, account_id, adapter, known_tickers, now_iso)
    # The tick-level shape (window_start=None) never calls the adapter at all (see run_window_roll's
    # own docstring): it always refuses by name tonight, so no exception can reach this call site.
    window_roll = run_window_roll(conn, account_id, adapter, now_iso, None)

    try:
        current_value = adapter.report_account_value()
    except AdapterCannotDetermine:
        cur = conn.execute(
            "INSERT INTO real_loss_monitor_runs (account_id, ran_at, completed, account_value, "
            "window_drop, since_go_drop, armed) VALUES (?, ?, 0, NULL, NULL, NULL, 0)",
            (account_id, now_iso),
        )
        return {"completed": False, "armed": False, "run_id": cur.lastrowid,
                "reconciliation": reconciliation, "window_roll": window_roll}

    window_baseline = latest_window_baseline(conn, account_id)
    enabling_row = latest_enabling_record(conn, account_id)
    have_since_go_baseline = enabling_row is not None and enabling_row["account_value_at_go"] is not None

    window_drop = None if window_baseline is None else (window_baseline["account_value"] - current_value)
    since_go_drop = None if not have_since_go_baseline else (enabling_row["account_value_at_go"] - current_value)

    if window_baseline is None or not have_since_go_baseline:
        # Cannot evaluate: at least one side of the two loss limits has nothing to compare
        # against, so this pass records the reading it took without claiming a verdict either way.
        armed = False
    else:
        window_cap = configured_parameter_value(conn, "real_trading.loss_limit_window")
        since_go_cap = configured_parameter_value(conn, "real_trading.loss_limit_since_go")
        # Found running AC-19's own account B (real_trading.loss_limit_window set to NULL to
        # prove the roll step's own named refusal): this scheduled pass runs independently of
        # submit_real_order's own step 0 (assert_parameters_configured), so a NULL cap can reach
        # here even though it can never reach the order path itself. A cap this pass cannot read
        # is a side it cannot evaluate, exactly like a missing baseline above, never a crash and
        # never a fabricated breach on a limit nobody has configured yet.
        window_breach = window_cap is not None and window_drop > window_cap
        since_go_breach = since_go_cap is not None and since_go_drop > since_go_cap
        armed = window_breach or since_go_breach

    cur = conn.execute(
        "INSERT INTO real_loss_monitor_runs (account_id, ran_at, completed, account_value, "
        "window_drop, since_go_drop, armed) VALUES (?, ?, 1, ?, ?, ?, ?)",
        (account_id, now_iso, current_value, window_drop, since_go_drop, int(armed)),
    )
    if armed:
        # HALT only, never flatten (DECISIONS #392 ruling 5): a loss-limit breach cancels
        # open orders and stops, it never closes positions on its own.
        command_kill(conn, account_id, "halt", adapter, now_iso)
    return {"completed": True, "armed": armed, "run_id": cur.lastrowid,
            "reconciliation": reconciliation, "window_roll": window_roll}


# =============================================================================
# The per-run slice fence (brief AC-L2, folded by the lead's read of 66ff6f63, finding 2). A
# SEVENTH fence, separate from the six named hard limits above: AC-L6's own four breach columns are
# never populated for this one (the criteria doc's own words: "the per-run slice fence's bound is
# NOT one of the six").
# =============================================================================

def _realised_pnl_for_run(conn: sqlite3.Connection, paper_run_id: int):
    """Realised profit and loss for one run's own TERMINAL 'filled' real_orders rows (night three,
    W-14, W-6), walked in fill order (resolved_at, then order_id as a tie-break) with the SAME
    quantity-weighted average-cost method paperdb._walk_average_cost already uses: a buy blends its
    price into the running average cost of the open lot; a sell realises
    quantity * (price - average_cost). real_orders carries no fees column, so none is subtracted
    here, unlike paperdb's own walk.

    Returns (realised_pnl, absent): absent is True the instant a filled row's own fill_price is
    NULL, and realised_pnl is then meaningless and must never be read as zero (AC-20).
    """
    fills = conn.execute(
        "SELECT symbol, side, filled_quantity, fill_price FROM real_orders "
        "WHERE paper_run_id = ? AND terminal_state = 'filled' "
        "ORDER BY resolved_at ASC, order_id ASC",
        (paper_run_id,),
    ).fetchall()
    net_quantity: dict = {}
    avg_cost: dict = {}
    realised_pnl = 0.0
    for row in fills:
        if row["fill_price"] is None:
            return None, True
        symbol = row["symbol"]
        quantity = row["filled_quantity"] or 0.0
        price = row["fill_price"]
        prior_quantity = net_quantity.get(symbol, 0.0)
        if row["side"] == "buy":
            prior_cost = avg_cost.get(symbol, 0.0)
            new_quantity = prior_quantity + quantity
            if new_quantity != 0.0:
                avg_cost[symbol] = ((prior_quantity * prior_cost) + (quantity * price)) / new_quantity
            net_quantity[symbol] = new_quantity
        else:
            cost_basis = avg_cost.get(symbol, 0.0)
            realised_pnl += quantity * (price - cost_basis)
            net_quantity[symbol] = prior_quantity - quantity
    return realised_pnl, False


def check_slice_fence(conn: sqlite3.Connection, paper_conn: sqlite3.Connection, paper_run_id: int,
                       order_notional: float) -> Optional[str]:
    """The bound is paper_runs.slice_opening_cash for paper_run_id (read through paper_conn, this
    lane never opens paper.db itself) PLUS this run's own realised profit-and-loss (night three,
    W-6, W-14: computed fresh from conn, this lane's own real_orders log) MINUS this run's own
    open-intent notional (also summed fresh from conn, scoped to paper_run_id), matching
    paperdb.compute_available_buying_power's own shape exactly: a missing run, or an unreadable
    slice figure, refuses 'slice-fence-bound-absent' before anything else is computed; a filled row
    on this run whose own fill_price is absent refuses 'slice-fence-realised-absent' before the
    bound is computed at all, rather than treating the missing figure as zero (AC-20). Over the
    bound refuses 'limit-run-slice'; at or under it permits.
    """
    run_row = paper_conn.execute(
        "SELECT slice_opening_cash FROM paper_runs WHERE paper_run_id = ?", (paper_run_id,)
    ).fetchone()
    if run_row is None or run_row["slice_opening_cash"] is None:
        return "slice-fence-bound-absent"

    realised_pnl, absent = _realised_pnl_for_run(conn, paper_run_id)
    if absent:
        return "slice-fence-realised-absent"

    bound = (run_row["slice_opening_cash"] + realised_pnl
             - open_intent_notional_for_run(conn, paper_run_id))
    if order_notional > bound:
        return "limit-run-slice"
    return None
