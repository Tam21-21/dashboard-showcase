"""Real-trading routes: the browser-facing side of the real-trading store (realdb.py).

Night one, lane RT (docs/plans/night/2026-09-12_set6a_night/PLAN.md section 9), building AC-K1's
ROUTE half only: arm (both commanded actions), disarm, run the verification pass, and read the
current state, matching #58 (every capability operable from the dashboard, not only from a
script). The kill switch's PAGE, which consumes these four routes, is a later day lane (5.1p); its
absence does not make these routes inert, and its arrival needs no change here.

THIS ROUTER NEVER SENDS AN ORDER. There is no order-submission route in this file at all: the only
path to adapter.submit_order is real_orders.submit_real_order (realdb.evaluate_order_gate was
deleted on 2026-09-13, DECISIONS #395 ruling 5, so no second gate exists), no live execution adapter
exists in this repository yet (section 19 stop condition 2), and turning real trading on is one of
the sprint's four synchronous stops.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from .. import config
from .. import paperdb
from .. import real_orders
from .. import realdb
from .. import real_limits
from .. import real_reconciliation

logger = logging.getLogger(__name__)

# Browser: no auth dependency here (Caddy basic auth handles humans at the edge; the same
# convention routes/paper.py's own router already follows).
router = APIRouter(prefix="/api/real", tags=["real"])


def _now_iso() -> str:
    """UTC now in this module's own fixed shape, matching realdb's own timestamp format exactly
    so a value this route writes reads identically to one a probe writes.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _NoAdapterConfigured(RuntimeError):
    """Raised by get_adapter's own default body: no live execution adapter is wired into this
    router yet (section 19 stop condition 2). A probe replaces this function wholesale
    (`real.get_adapter = lambda: some_double`) before ever calling a route that needs one, the
    same pattern the paper router's own StubVenue instantiation follows at its own call site.
    """


def get_adapter():
    """Resolve the execution adapter this router calls. The ONE place a route in this file reaches
    toward an adapter at all; every route below calls this exactly once, right before it needs one,
    rather than holding a module-level instance, so a probe's own monkeypatch of this name is
    always honoured on the very next call.
    """
    raise _NoAdapterConfigured(
        "No broker connection is set up yet, so the kill switch cannot reach a broker."
    )


class ArmRequest(BaseModel):
    account_id: str
    action: Literal["halt", "flatten"]


class AccountRequest(BaseModel):
    account_id: str


def _drill_state_word(drill_state) -> str:
    """get_kill_drill_state returns either the literal string 'never-drilled' or a drill row; a
    route response always carries a plain word, never a row object, so this picks the outcome off
    a row and passes the literal string through unchanged.
    """
    if isinstance(drill_state, str):
        return drill_state
    return drill_state["outcome"]


@router.get("/kill/state")
def get_kill_state_route(account_id: str):
    """Read the switch's current state and the drill's derived state. Creates the switch row on
    first read (ensure_kill_switch_row is idempotent), so a brand-new account reads never-armed
    rather than a 404: the switch's fresh state is a real, named value, not an error.
    """
    try:
        with realdb.get_real_conn(realdb.DB_PATH) as conn:
            realdb.ensure_kill_switch_row(conn, account_id)
            row = realdb.get_kill_state(conn, account_id)
            drill_state = realdb.get_kill_drill_state(conn, account_id)
    except (realdb.RealDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _real_store_503(exc) from exc
    return {
        "account_id": account_id,
        "state": row["state"],
        "commanded_action": row["commanded_action"],
        "last_verified_at": row["last_verified_at"],
        "last_verified_outcome": row["last_verified_outcome"],
        "drill_state": _drill_state_word(drill_state),
    }


@router.post("/kill/arm")
def post_kill_arm(body: ArmRequest):
    """Command HALT or FLATTEN (AC-K2's two commanded actions). Always moves to
    armed-unverified; the page's own display is what makes clear the effect is not yet confirmed.

    503 with a named detail when no adapter is configured (the current state of this build,
    section 19 stop condition 2), matching routes/judge.py's own precedent of a 503 naming what is
    missing rather than an unmapped 500 an operator cannot debug.
    """
    now = _now_iso()
    try:
        adapter = get_adapter()
    except _NoAdapterConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    with realdb.get_real_conn(realdb.DB_PATH) as conn:
        realdb.ensure_kill_switch_row(conn, body.account_id)
        state = realdb.command_kill(conn, body.account_id, body.action, adapter, now)
        row = realdb.get_kill_state(conn, body.account_id)
    return {
        "account_id": body.account_id,
        "state": state,
        "commanded_action": row["commanded_action"],
    }


@router.post("/kill/verify")
def post_kill_verify(body: AccountRequest):
    """Run one verification pass (PASS-3). Refused with 409 when the switch is currently
    disarmed-verified-clear: there is no kill in effect to verify.

    503 with a named detail when no adapter is configured (the current state of this build,
    section 19 stop condition 2), matching routes/judge.py's own precedent of a 503 naming what is
    missing rather than an unmapped 500 an operator cannot debug.
    """
    now = _now_iso()
    try:
        adapter = get_adapter()
    except _NoAdapterConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    with realdb.get_real_conn(realdb.DB_PATH) as conn:
        realdb.ensure_kill_switch_row(conn, body.account_id)
        try:
            state = realdb.run_kill_verification(conn, body.account_id, adapter, now)
        except realdb.VerificationNotApplicable as exc:
            raise HTTPException(
                status_code=409,
                detail="refused: verification not applicable from state '%s'" % exc.state,
            ) from exc
    return {"account_id": body.account_id, "state": state}


@router.post("/kill/disarm")
def post_kill_disarm(body: AccountRequest):
    """Disarm the switch. A disarm the switch's own state forbids is REFUSED, naming the refusing
    state (AC-K1, AC-K10 column 5), rather than behaving as if it had worked.
    """
    with realdb.get_real_conn(realdb.DB_PATH) as conn:
        realdb.ensure_kill_switch_row(conn, body.account_id)
        try:
            state = realdb.disarm_kill(conn, body.account_id)
        except realdb.KillDisarmRefused as exc:
            raise HTTPException(
                status_code=409,
                detail="refused: disarm not permitted from state '%s'" % exc.state,
            ) from exc
    return {"account_id": body.account_id, "state": state}


@router.get("/enabled")
def get_enabled_route(account_id: str):
    """Set 5 day, lane KS (5.1p), the kill switch page's one backend touch. Reads
    realdb.derive_enabled (AC-G9's five-test order, section 12.1) and reports whether account_id
    is currently enabled to trade for real, and the first failing reason when it is not. Nothing
    here can write a granted enabling record (AC-G2, section 18 decision 4), so this always
    reflects the store's own current, honest answer rather than a value this build could forge.
    The page's top line ("REAL TRADING · DISABLED · <reason>") is built from this response alone.

    503 with a named detail when the store itself cannot be read, and round 3's own fold
    (docs/units/KS/reviews/code_quality_pass1_2026-09-12.md, SF1) gives each of three shapes its
    own sentence rather than folding all of sqlite3.OperationalError into one: a missing parent
    directory (RealDbParentDirectoryMissing) reads "the real-trading store could not be opened";
    a table init_real_db never created (sqlite3.OperationalError, "no such table" in the driver's
    own text) reads "the real-trading store has no tables"; any OTHER OperationalError, the class
    "database is locked" belongs to, is a TRANSIENT, retry-able state on a money-path readout and
    reads "the real-trading store answered with an error, try again" rather than the two sentences
    above, which both imply the store cannot be opened at all and would tell an operator to treat
    a passing lock contention as the same permanent failure a missing table is. All three stay 503
    with the driver's own text appended verbatim; nothing here is swallowed, and every branch is a
    specific, named exception type, never a bare except, matching routes/judge.py's own STORE
    UNAVAILABLE precedent (_raise_store_absent): the page must say the store could not be opened,
    never silently read that as "disabled".
    """
    now = _now_iso()
    try:
        with realdb.get_real_conn(realdb.DB_PATH) as conn:
            enabled, reason = realdb.derive_enabled(conn, account_id, now)
    except realdb.RealDbParentDirectoryMissing as exc:
        raise HTTPException(
            status_code=503,
            detail="the real-trading store could not be opened: %s" % exc,
        ) from exc
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise HTTPException(
                status_code=503,
                detail=(
                    "the real-trading store has no tables: init_real_db never ran against %s"
                    % realdb.DB_PATH
                ),
            ) from exc
        # Every other OperationalError, "database is locked" foremost: a transient state, not a
        # missing store, so the detail says try again rather than implying the store is unusable.
        raise HTTPException(
            status_code=503,
            detail="the real-trading store answered with an error, try again: %s" % exc,
        ) from exc
    return {"account_id": account_id, "enabled": enabled, "reason": reason}


def _real_store_503(exc: Exception) -> HTTPException:
    """The 503 answer for a real.db that cannot be read, with the three sentences get_enabled_route
    gives (see its docstring): could not be opened, has no tables, or answered with an error
    (a transient state such as "database is locked"). The driver's own text is appended."""
    if isinstance(exc, realdb.RealDbParentDirectoryMissing):
        return HTTPException(
            status_code=503,
            detail="the real-trading store could not be opened: %s" % exc,
        )
    if "no such table" in str(exc):
        return HTTPException(
            status_code=503,
            detail=(
                "the real-trading store has no tables: init_real_db never ran against %s"
                % realdb.DB_PATH
            ),
        )
    return HTTPException(
        status_code=503,
        detail="the real-trading store answered with an error, try again: %s" % exc,
    )


def _paper_store_503(exc: Exception) -> HTTPException:
    """The same three sentences for paper.db, which readiness reads for approved promotions."""
    if isinstance(exc, paperdb.PaperDbParentDirectoryMissing):
        return HTTPException(
            status_code=503,
            detail="the paper store could not be opened: %s" % exc,
        )
    if "no such table" in str(exc):
        return HTTPException(
            status_code=503,
            detail=(
                "the paper store has no tables: init_paper_db never ran against %s"
                % paperdb.DB_PATH
            ),
        )
    return HTTPException(
        status_code=503,
        detail="the paper store answered with an error, try again: %s" % exc,
    )


def _configured_account() -> Optional[str]:
    """The one real account (config.REAL_ACCOUNT_ID, read per request), or None when unset, empty
    or only whitespace."""
    value = config.REAL_ACCOUNT_ID
    if value is None or not value.strip():
        return None
    return value


@router.get("/account")
def get_account_route():
    """The configured real account id (or null), which the Capital page passes to the kill routes."""
    return {"account_id": _configured_account()}


# The unit and plain meaning of each of the nine limits (brief section 16), keyed by the full
# "real_trading.*" strings of realdb.REAL_TRADING_PARAMETER_KEYS. The only place these words live.
_LIMIT_WORDS = {
    "real_trading.per_order_notional_cap": (
        "dollars", "the most one order may be worth"),
    "real_trading.ticker_net_cap": (
        "dollars", "the most the account may hold in one stock"),
    "real_trading.total_net_cap": (
        "dollars", "the most the account may hold in all stocks together"),
    "real_trading.loss_limit_window": (
        "dollars", "the largest drop in account value allowed inside the loss window "
                   "(the window's length is not chosen yet)"),
    "real_trading.loss_limit_since_go": (
        "dollars", "the largest drop in account value allowed since your go"),
    "real_trading.order_rate_cap": (
        "orders per %d seconds" % real_limits.ORDER_RATE_WINDOW_SECONDS,
        "how many orders may be sent in one minute"),
    "real_trading.reconciliation_max_age": (
        "seconds", "how old the last check of our records against the broker may be before "
                   "trading stops"),
    "real_trading.account_value_max_age": (
        "seconds", "how old the last account value reading may be before trading stops"),
    "real_trading.kill_drill_max_age": (
        "seconds", "how long a passed kill switch drill counts before the go must be renewed"),
}


def _limit_rows(conn: sqlite3.Connection) -> list:
    """The nine limits in key order: key, value (null when unset), unit, meaning."""
    stored = {row["key"]: row["value"]
              for row in conn.execute("SELECT key, value FROM real_trading_parameters").fetchall()}
    return [
        {
            "key": key,
            "value": stored.get(key),
            "unit": _LIMIT_WORDS[key][0],
            "meaning": _LIMIT_WORDS[key][1],
        }
        for key in realdb.REAL_TRADING_PARAMETER_KEYS
    ]


@router.get("/limits")
def get_limits_route():
    """The nine real-money limits as the store holds them. READ ONLY. 503, never 500, when real.db
    cannot be read."""
    try:
        with realdb.get_real_conn(realdb.DB_PATH) as conn:
            rows = _limit_rows(conn)
    except (realdb.RealDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _real_store_503(exc) from exc
    return {"limits": rows}


def _bound_values_match(conn: sqlite3.Connection) -> set:
    """Ids of every account's newest enabling record whose bound values equal the configured
    values right now (the same comparison derive_enabled makes). READ ONLY."""
    configured = {row["key"]: row["value"]
                  for row in conn.execute("SELECT key, value FROM real_trading_parameters").fetchall()}
    records = conn.execute(
        "SELECT enabling_record_id, bound_params_json FROM real_enabling_records "
        "WHERE enabling_record_id IN "
        "(SELECT MAX(enabling_record_id) FROM real_enabling_records GROUP BY account_id)"
    ).fetchall()
    matching = set()
    for record in records:
        bound = json.loads(record["bound_params_json"])
        if all(key in bound and configured.get(key) is not None
               and configured.get(key) == bound[key]
               for key in realdb.REAL_TRADING_PARAMETER_KEYS):
            matching.add(record["enabling_record_id"])
    return matching


@router.put("/limits")
def put_limits_route(body: Dict[str, Any] = Body(...)):
    """Set any subset of the nine limits. The values are checked as the raw JSON the caller sent
    (a string, true or null is refused, never turned into a number). All or nothing, in one
    transaction. The answer lists the nine rows, whether the configured account is still enabled,
    and the ids of the enabling records this change superseded (the parameters are global, so every
    account's newest record is checked). Nothing here writes an enabling record, and nothing
    sends, cancels or closes an order: a changed value only makes derive_enabled read
    'enabling-record-superseded' until a new go is recorded by hand (AC-L8, #390 ruling 2).
    """
    now = _now_iso()
    account_id = _configured_account()
    try:
        with realdb.real_transaction(realdb.DB_PATH) as conn:
            matched_before = _bound_values_match(conn)
            try:
                realdb.set_trading_parameters(conn, body)
            except realdb.RealLimitRefused as exc:
                raise HTTPException(status_code=422, detail=exc.code) from exc
            matched_after = _bound_values_match(conn)
            superseded = sorted(matched_before - matched_after)
            rows = _limit_rows(conn)
            if account_id is None:
                enabled, reason = None, None
            else:
                enabled, reason = realdb.derive_enabled(conn, account_id, now)
    except (realdb.RealDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _real_store_503(exc) from exc
    if reason is not None:
        reason = _evidence_word(reason)
    return {
        "limits": rows,
        "enabled": enabled,
        "enabled_reason": reason,
        "superseded_enabling_record_ids": superseded,
    }


# The plain sentences the Capital page shows on hover. The page never translates: every word the
# readiness code reads from the stores (derive_enabled's words, the drill words, the switch states)
# is turned into English here, and probe r07 fails if a word is added without a sentence.
_EVIDENCE_TEXT = {
    "recorded": "Your go is recorded and in force.",
    "enabling-record-absent": "No go is recorded for this account.",
    "enabling-record-unusable": (
        "The newest go record is missing its decisions log entry, a limit, or a kill drill "
        "that exists, so it does not count."
    ),
    "enabling-record-superseded": "A limit changed after the go, so the go must be given again.",
    "kill-drill-not-passed": "The kill drill named in the go did not pass.",
    "kill-drill-stale": "The kill drill named in the go is older than the kill drill age limit.",
    "ran-and-passed": "passed",
    "ran-and-failed": "failed",
    "never-drilled": "never run",
    "never-armed": "never armed",
    "disarmed-verified-clear": "off and checked clear",
    "armed-unverified": "on and not yet checked",
    "armed-verified-effective": "on and checked working",
    "armed-verified-ineffective": "on and checked not working",
    "no-switch-row": "Never armed.",
    "kill-switch-line": "Last drill %s; switch %s.",
    "limits-count": "%d of %d set.",
    "broker-connected": "A broker connection is set up.",
    "broker-absent": "No broker connection is set up yet.",
    "approved-none": "No strategy is approved for real money yet.",
    "approved-count": "%d approved.",
}


def _evidence_word(word: str) -> str:
    """The table's text for a word read from a store. A word the table lacks is a code change
    that forgot its sentence: fail loudly, never show the raw word."""
    try:
        return _EVIDENCE_TEXT[word]
    except KeyError:
        raise RuntimeError("no plain-English text for the readiness word %r" % (word,)) from None


_READINESS_ITEMS = ("go", "kill_switch", "limits", "broker", "approved_strategy")
_GOOD_STATE = {
    "go": "recorded",
    "kill_switch": "ready",
    "limits": "set",
    "broker": "connected",
    "approved_strategy": "approved",
}


@router.get("/readiness")
def get_readiness_route():
    """The Capital page's "before the first real order" checklist: five items on the one configured
    real account, in the mockup's order. READ ONLY: it never calls ensure_kill_switch_row or any
    writer, never calls the adapter (get_adapter only says whether one is configured) and writes
    no enabling record. 503, never 500, when real.db or paper.db cannot be read.
    """
    account_id = _configured_account()
    try:
        with paperdb.get_paper_conn(paperdb.DB_PATH) as paper_conn:
            approved = sum(
                1 for run_id in paperdb.promoted_run_ids(paper_conn)
                if real_orders.promotion_requested_for_run(paper_conn, run_id))
    except (paperdb.PaperDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _paper_store_503(exc) from exc

    if account_id is None:
        return {
            "account_id": None,
            "items": [
                {"item": name, "state": "not-yet", "evidence": "no real account configured"}
                for name in _READINESS_ITEMS
            ],
            "ready_count": 0,
            "approved_count": approved,
        }

    now = _now_iso()
    try:
        with realdb.get_real_conn(realdb.DB_PATH) as conn:
            enabled, reason = realdb.derive_enabled(conn, account_id, now)
            kill_row = realdb.get_kill_state(conn, account_id)
            drill_state = realdb.get_kill_drill_state(conn, account_id)
            param_rows = conn.execute("SELECT key, value FROM real_trading_parameters").fetchall()
    except (realdb.RealDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _real_store_503(exc) from exc

    items = {}

    if enabled:
        items["go"] = ("recorded", _evidence_word("recorded"))
    else:
        items["go"] = ("not-yet", _evidence_word(reason))

    values = {row["key"]: row["value"] for row in param_rows}
    unset = [key for key in realdb.REAL_TRADING_PARAMETER_KEYS if values.get(key) is None]
    total = len(realdb.REAL_TRADING_PARAMETER_KEYS)
    if unset:
        items["limits"] = (
            "not-set",
            _EVIDENCE_TEXT["limits-count"] % (total - len(unset), total),
        )
    else:
        items["limits"] = ("set", _EVIDENCE_TEXT["limits-count"] % (total, total))

    try:
        get_adapter()
        items["broker"] = ("connected", _EVIDENCE_TEXT["broker-connected"])
    except _NoAdapterConfigured as exc:
        logger.info("readiness: no broker connection: %s", exc)
        items["broker"] = ("not-connected", _EVIDENCE_TEXT["broker-absent"])

    if kill_row is None:
        items["kill_switch"] = ("not-ready", _EVIDENCE_TEXT["no-switch-row"])
    else:
        drill_word = _drill_state_word(drill_state)
        kill_line = _EVIDENCE_TEXT["kill-switch-line"] % (
            _evidence_word(drill_word), _evidence_word(kill_row["state"]))
        if drill_word == "ran-and-passed" and items["broker"][0] == "connected":
            items["kill_switch"] = ("ready", kill_line)
        else:
            items["kill_switch"] = ("not-ready", kill_line)

    if approved == 0:
        items["approved_strategy"] = ("none", _EVIDENCE_TEXT["approved-none"])
    else:
        items["approved_strategy"] = ("approved", _EVIDENCE_TEXT["approved-count"] % approved)

    ordered = [
        {"item": name, "state": items[name][0], "evidence": items[name][1]}
        for name in _READINESS_ITEMS
    ]
    ready_count = sum(1 for entry in ordered if entry["state"] == _GOOD_STATE[entry["item"]])
    return {"account_id": account_id, "items": ordered, "ready_count": ready_count,
            "approved_count": approved}


@router.get("/reconciliation/latest")
def get_reconciliation_latest_route(account_id: str):
    """Night three, lane RT3 (AC-R3): exposes the newest reconciliation PASS for an account. Our
    own figure and the venue's own figure are named by DISTINCT field-name prefixes (`ours_` /
    `venue_`), never an unlabelled number, both at the top level (a 'clear' pass's own account-scope
    reading) and inside the nested `disagreement` object (the one scope a 'disagreement' pass
    disagreed on). 'never-reconciled' (E6's own fresh answer, AC-R6) covers both a totally fresh
    account and one whose only history is failed attempts: neither is a COMPLETED pass, and this
    route only ever surfaces a completed one, matching
    real_reconciliation.latest_reconciliation_pass's own filter.

    A 'disagreement' pass reports NEITHER top-level figure (AC-R4: our own book is never presented
    as agreed when the last pass said otherwise), only the one disagreeing scope's own two figures,
    inside `disagreement`.
    """
    with realdb.get_real_conn(realdb.DB_PATH) as conn:
        latest = real_reconciliation.latest_reconciliation_pass(conn, account_id)

        if latest is None:
            return {
                "account_id": account_id,
                "outcome": "never-reconciled",
                "ours_account_net_notional": None,
                "venue_account_net_notional": None,
                "disagreement": None,
            }

        if latest["outcome"] == "disagreement":
            detail = json.loads(latest["disagreement_detail_json"])
            return {
                "account_id": account_id,
                "outcome": "disagreement",
                "ours_account_net_notional": None,
                "venue_account_net_notional": None,
                "disagreement": {
                    "scope": detail["scope"],
                    "ticker": detail.get("ticker"),
                    "ours_net_notional": detail["ours"],
                    "venue_net_notional": detail["venue"],
                },
            }

        # outcome == 'clear': the account's own book, computed fresh, beside the venue's own
        # newest STORED reading (this route never asks the adapter directly; it reads what the
        # last completed sweep already wrote).
        ours = real_reconciliation.our_own_account_figure(conn, account_id)
        venue_reading = real_limits.latest_position_reading(conn, account_id, "account", None)
        venue = venue_reading["net_notional"] if venue_reading is not None else None
        return {
            "account_id": account_id,
            "outcome": "clear",
            "ours_account_net_notional": ours,
            "venue_account_net_notional": venue,
            "disagreement": None,
        }
