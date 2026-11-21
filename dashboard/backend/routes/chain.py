"""The chain runner's four control routes (lane CN, set 16c, `#427`, spec section 6, prefix
`/api/research/chains`). Everything here is a THIN write: mint a request, flip an operator flag,
or record Tamer's own pick. The actual state machine (what happens next) lives entirely in
`dashboard/backend/chain_runner.py`, read every 30 seconds by the background loop `main.py` starts;
these routes never advance a chain themselves except the pick route, which is the one step E-1
itself says a route writes directly ("the pick route writes a cell: replaying").

Every refusal here follows the house idiom the spec's C-1/R-1 tables both use: the FastAPI
`detail` string is one of the closed words, a colon, then plain words, so the page can show the
word and the sentence together without a second lookup.
"""
from __future__ import annotations

import json  # the request's param_grid is stored as one JSON blob on the chain row
import time  # every write here stamps created_at/updated_at with the real wall clock
import uuid  # mints the chain's own private calendar id, "chain-" + a fresh uuid4 hex

import anyio.from_thread  # the routes are plain `def`, so a publish hops back onto the event loop through this
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from dashboard.backend import chain_runner, chain_words, generated, research_prechecks, researchdb, reservedb, stage_runner
from dashboard.backend.sse import broadcaster

router = APIRouter(prefix="/api/research/chains", tags=["research-chains"])


class ChainCreateIn(BaseModel):
    """The RUN THE CHAIN form's own body (spec section 6). `param_grid` is typed loosely as a
    plain dict, on purpose, NEVER `dict[str, list]`: a stricter Pydantic type would let FastAPI's
    own automatic validator answer a malformed grid with its own generic 422 body, before this
    route's own `param-grid-invalid` word ever gets a chance to run (CR-3's own fixture, a value
    that is not a list, exists to catch exactly that shortcut)."""
    strategy_id: int
    screen_start_ts: int
    optimise_start_ts: int
    walkforward_start_ts: int
    walkforward_end_ts: int
    param_grid: dict
    duel_with_strategy_id: "int | None" = None


class ChainCellIn(BaseModel):
    """The CHOOSE A CELL form's own body: one variation/ticker pair off the optimise run's own
    sweep grid (spec section 6, "posting the pick... with variation_id and ticker")."""
    variation_id: int
    ticker: str


def _refuse(status_code: int, word: str, sentence: str) -> HTTPException:
    """Every refusal in this file goes through here so the "<word>: <plain words>" shape (the
    house idiom the spec's C-1 table itself uses) is never typed out by hand twice the same way."""
    return HTTPException(status_code=status_code, detail=f"{word}: {sentence}")


def _publish_chain_changed(chain_ids: "list[int]") -> None:
    """AC-2: tell every open page that these chains moved, as the event research.chain_changed. The
    routes are plain `def` (they run in a worker thread), so the publish is handed back to the event
    loop with anyio. Called only after a write that went through, never after a refusal."""
    anyio.from_thread.run(broadcaster.publish, "research.chain_changed", {"chain_ids": chain_ids})


def _stalled_reasons(conn) -> "dict[int, str]":
    """Chain id to the runner's stall reason, for the two reasons the page shows as notes (N-1)."""
    shown = ("no-worker-online", "step-stalled")
    return {item["id"]: item["reason"] for item in chain_runner.chain_runner_stalled_chains(conn)
            if item["reason"] in shown}


def _add_page_keys(conn, chain: dict, stalled: "dict[int, str]") -> dict:
    """The keys the Chains page draws from, added to one chain row (a dict). One helper serves the
    list and the detail route so the two can never word a chain differently."""
    state = chain["state"]
    strategy = conn.execute("SELECT display_name FROM research_strategies WHERE id = ?;",
                            (chain["strategy_id"],)).fetchone()
    chain["display_name"] = strategy["display_name"] if strategy is not None else None
    chain["state_word"] = chain_words.STATE_WORDS[state]
    chain["group"] = chain_words.STATE_GROUPS[state]
    if state in chain_words.STATES_WITH_ENDED_STEP:
        chain["step"] = chain["ended_at_step"]
    else:
        chain["step"] = chain_words.STEP_FOR_STATE[state]
    note = None
    if chain["stop_requested"] == 1 and chain["group"] != "ended":
        note = "stop-requested"
    elif chain["id"] in stalled:
        note = stalled[chain["id"]]
    chain["note"] = note
    chain["note_word"] = chain_words.NOTE_WORDS[note] if note is not None else None
    reason_word = chain["reason_word"]
    chain["reason_sentence"] = chain_words.reason_sentence(reason_word) if reason_word is not None else None
    return chain


@router.get("")
def list_chains(limit: int = Query(200, ge=1, le=500)):
    """Every chain, newest first, worded for the page (lane P7-0). The reserve cut rides along so the
    page can draw the windows against it; if the cut is owed, no chain could start, so the list says so."""
    try:
        reserve_cut_ts = reservedb.read_reserve_cut_date_ts()
    except reservedb.ReserveParameterOwed as exc:
        raise _refuse(503, "reserve-cut-owed", str(exc)) from exc
    with researchdb.get_conn() as conn:
        rows = conn.execute("SELECT * FROM research_chains ORDER BY id DESC LIMIT ?;", (limit,)).fetchall()
        stalled = _stalled_reasons(conn)
        chains = []
        for row in rows:
            chain = dict(row)
            del chain["param_grid_json"]
            chains.append(_add_page_keys(conn, chain, stalled))
    return {"chains": chains, "reserve_cut_ts": reserve_cut_ts}


@router.get("/{chain_id}")
def get_chain(chain_id: int):
    """One chain's own current row, plus the two run ids CR-22's own real end-to-end proof polls
    for (the optimise run, once picking a cell needs its cells, and the key the chain's verdict is
    graded at; the walk-forward run, the one check 2 reads through it). Neither id is a stored column: both are the
    SAME derived fact `chain_runner._find_stage_run` already computes every pass, read here rather
    than duplicated as a second column this build would then have to keep in sync (spec section 8's
    own "state is DERIVED" argument, applied to these two facts the same way). Lane P7-0 adds the
    page keys the list carries and the chain's transition log."""
    with researchdb.get_conn() as conn:
        row = conn.execute("SELECT * FROM research_chains WHERE id = ?;", (chain_id,)).fetchone()
        if row is None:
            raise _refuse(404, "chain-not-found", f"chain {chain_id} was not found")
        chain = dict(row)
        chain["optimise_run_id"] = chain_runner._find_stage_run(
            conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "optimise")
        chain["walkforward_run_id"] = chain_runner._find_stage_run(
            conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "walkforward")
        _add_page_keys(conn, chain, _stalled_reasons(conn))
        transitions = []
        for trow in conn.execute(
                "SELECT * FROM research_chain_transitions WHERE chain_id = ? ORDER BY id;", (chain_id,)).fetchall():
            transition = dict(trow)
            transition["to_word"] = chain_words.STATE_WORDS[transition["to_state"]]
            transition["reason_sentence"] = (chain_words.reason_sentence(transition["reason_word"])
                                             if transition["reason_word"] is not None else None)
            transitions.append(transition)
        chain["transitions"] = transitions
    return chain


@router.post("", status_code=201)
def create_chain(body: ChainCreateIn):
    """CR-3/CR-4: the POST pre-flight, in the order spec section 2.4 lists the checks that run
    before any row is written, then the write itself (one chain, or a duel's two, sharing one
    calendar and one transaction)."""
    # 1. The four boundaries in order (2.2's own CHECK, repeated here so a malformed request never
    # reaches the database at all).
    if not (body.screen_start_ts < body.optimise_start_ts < body.walkforward_start_ts < body.walkforward_end_ts):
        raise _refuse(422, "window-order-invalid",
                      "screen_start_ts < optimise_start_ts < walkforward_start_ts < walkforward_end_ts must all hold")

    # 2. b4 (walkforward_end_ts) must stay before the reserve cut (2.4).
    if body.walkforward_end_ts >= reservedb.read_reserve_cut_date_ts():
        raise _refuse(422, "window-crosses-reserve-cut",
                      f"walkforward_end_ts {body.walkforward_end_ts} is at or past the reserve cut date")

    # 3. The grid's own shape: every value must be a list (CR-3's "param-grid-invalid" fixture).
    if not isinstance(body.param_grid, dict) or not all(isinstance(v, list) for v in body.param_grid.values()):
        raise _refuse(422, "param-grid-invalid", "every param_grid entry must map to a list of values")

    # 4. The strategy must exist and not be soft-deleted.
    strategy = researchdb.get_strategy(body.strategy_id)
    if strategy is None:
        raise _refuse(404, "strategy-not-found", f"strategy {body.strategy_id} was not found")
    if strategy["status"] == "deleted":
        raise _refuse(409, "strategy-deleted", f"strategy {body.strategy_id} is deleted")

    # 5. A generated strategy's source must not have drifted since generation.
    try:
        generated.assert_generation_record_matches(body.strategy_id)
    except generated.GeneratedSourceDrift as exc:
        raise _refuse(409, "generated-source-drift", str(exc)) from exc

    # 6. The daily bar store must be configured (every stage will need it).
    try:
        stage_runner.daily_store_path()
    except stage_runner.DailyStoreNotConfigured as exc:
        raise _refuse(503, "daily-store-not-configured", str(exc) or "EODHD_DAILY_STORE is unset or invalid") from exc

    # 7. The three ruled walk-forward settings must all carry a value (researchdb's own message
    # already begins with this exact word, so it is passed through rather than re-wrapped).
    try:
        researchdb.read_walkforward_parameters()
    except researchdb.WalkforwardParameterAbsent as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 8. The strategy must declare its own lookback (LookbackNotDeclared's message already begins
    # with this exact word too).
    if not stage_runner._declares_lookback_bars(strategy["source_code"]):
        raise HTTPException(status_code=409, detail=str(stage_runner.LookbackNotDeclared(body.strategy_id)))

    # 9. The quantile pre-check over the whole grid (F-4's moved, shared function).
    try:
        research_prechecks._refuse_bad_quantiles_in_grid(body.param_grid)
    except research_prechecks.QuantileOutOfRange as exc:
        raise _refuse(409, "quantile-out-of-range", str(exc)) from exc

    # 10. A named duel partner must itself exist.
    duel_partner = None
    if body.duel_with_strategy_id is not None:
        duel_partner = researchdb.get_strategy(body.duel_with_strategy_id)
        if duel_partner is None:
            raise _refuse(422, "duel-partner-invalid", f"strategy {body.duel_with_strategy_id} was not found")

    # Every pre-flight check passed: write the row (or the duel's two rows), one transaction,
    # actor 'page' (X-1), matching CR-4's own criterion of one chain row plus one transition row.
    now = int(time.time())
    calendar_id = f"chain-{uuid.uuid4().hex}"
    param_grid_json = json.dumps(body.param_grid)
    windows = (body.screen_start_ts, body.optimise_start_ts, body.walkforward_start_ts, body.walkforward_end_ts)
    with researchdb.transaction() as conn:
        chain_row = conn.execute(
            "INSERT INTO research_chains "
            "    (strategy_id, strategy_version, calendar_id, screen_start_ts, optimise_start_ts, "
            "     walkforward_start_ts, walkforward_end_ts, param_grid_json, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?) RETURNING id;",
            (body.strategy_id, strategy["version"], calendar_id, *windows, param_grid_json, now, now),
        ).fetchone()
        chain_id = int(chain_row["id"])
        conn.execute(
            "INSERT INTO research_chain_transitions (chain_id, from_state, to_state, actor, created_at) "
            "VALUES (?, NULL, 'waiting', 'page', ?);",
            (chain_id, now),
        )
        duel_chain_id = None
        if duel_partner is not None:
            partner_row = conn.execute(
                "INSERT INTO research_chains "
                "    (strategy_id, strategy_version, calendar_id, screen_start_ts, optimise_start_ts, "
                "     walkforward_start_ts, walkforward_end_ts, param_grid_json, duel_chain_id, state, "
                "     created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?) RETURNING id;",
                (body.duel_with_strategy_id, duel_partner["version"], calendar_id, *windows, param_grid_json,
                 chain_id, now, now),
            ).fetchone()
            duel_chain_id = int(partner_row["id"])
            conn.execute(
                "INSERT INTO research_chain_transitions (chain_id, from_state, to_state, actor, created_at) "
                "VALUES (?, NULL, 'waiting', 'page', ?);",
                (duel_chain_id, now),
            )
            conn.execute("UPDATE research_chains SET duel_chain_id = ? WHERE id = ?;", (duel_chain_id, chain_id))

    _publish_chain_changed([chain_id] if duel_chain_id is None else [chain_id, duel_chain_id])
    return JSONResponse(status_code=201, content={"status": "chain-started", "chain_id": chain_id,
                                                    "duel_chain_id": duel_chain_id})


@router.post("/{chain_id}/cell", status_code=202)
def choose_cell(chain_id: int, body: ChainCellIn):
    """E-1: "choosing-cell -[the pick route writes a cell]-> replaying". The one place this file
    itself moves a chain's state, because Tamer's own pick is not a fact any store already holds
    the way a run's own success is."""
    with researchdb.get_conn() as conn:
        chain_row = conn.execute("SELECT * FROM research_chains WHERE id = ?;", (chain_id,)).fetchone()
    if chain_row is None:
        raise _refuse(404, "chain-not-found", f"chain {chain_id} was not found")
    chain = dict(chain_row)
    if chain["state"] != "choosing-cell":
        raise _refuse(409, "chain-not-choosing", f"chain {chain_id} is {chain['state']!r}, not choosing-cell")

    with researchdb.get_conn() as conn:
        optimise_run_id = chain_runner._find_stage_run(
            conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "optimise"
        )
    if optimise_run_id is None or researchdb.get_run_cell(optimise_run_id, body.variation_id, body.ticker) is None:
        raise _refuse(404, "cell-not-in-optimise-run",
                      f"variation {body.variation_id} / ticker {body.ticker} is not a cell of this chain's optimise run")

    now = int(time.time())
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "UPDATE research_chains SET state = 'replaying', chosen_variation_id = ?, chosen_ticker = ?, "
            "  chosen_at = ?, updated_at = ? WHERE id = ? AND state = 'choosing-cell';",
            (body.variation_id, body.ticker, now, now, chain_id),
        )
        won = cur.rowcount > 0
        if won:
            conn.execute(
                "INSERT INTO research_chain_transitions (chain_id, from_state, to_state, actor, created_at) "
                "VALUES (?, 'choosing-cell', 'replaying', 'page', ?);",
                (chain_id, now),
            )
    if not won:
        # A concurrent pick or a pass-driven STOP won the race between the read above and this
        # write: nothing was changed, and the caller should re-read the chain rather than believe
        # its own pick took effect.
        raise _refuse(409, "chain-not-choosing", f"chain {chain_id} left choosing-cell before this pick landed")

    _publish_chain_changed([chain_id])
    return JSONResponse(status_code=202, content={"status": "cell-chosen", "chain_id": chain_id,
                                                    "variation_id": body.variation_id, "ticker": body.ticker})


@router.post("/{chain_id}/stop", status_code=202)
def stop_chain(chain_id: int):
    """Section 4.2: the route only SETS the flag; the next pass is what actually writes `stopped`
    (so no transition row is written here, and no draw/job/run is ever touched by this call)."""
    with researchdb.transaction() as conn:
        row = conn.execute("SELECT state FROM research_chains WHERE id = ?;", (chain_id,)).fetchone()
        if row is None:
            raise _refuse(404, "chain-not-found", f"chain {chain_id} was not found")
        if row["state"] not in chain_runner._NON_TERMINAL_STATES:
            raise _refuse(409, "chain-already-ended", f"chain {chain_id} is already {row['state']!r}")
        conn.execute("UPDATE research_chains SET stop_requested = 1 WHERE id = ?;", (chain_id,))
    _publish_chain_changed([chain_id])  # the flag write leaves updated_at alone, so only this route can tell the page
    return JSONResponse(status_code=202, content={"status": "chain-stop-requested", "chain_id": chain_id})


@router.post("/{chain_id}/retry", status_code=202)
def retry_chain(chain_id: int):
    """Spec section 4.5, table RETRY, read by `ended_at_step`. Every branch below writes through
    `chain_runner._write_transition` so the retry's own state change and its transition row share
    the SAME compare-and-set discipline every other write in this build uses."""
    with researchdb.get_conn() as conn:
        chain_row = conn.execute("SELECT * FROM research_chains WHERE id = ?;", (chain_id,)).fetchone()
    if chain_row is None:
        raise _refuse(404, "chain-not-found", f"chain {chain_id} was not found")
    chain = dict(chain_row)
    if chain["state"] not in ("refused", "failed"):
        raise _refuse(409, "chain-not-retryable", f"chain {chain_id} is {chain['state']!r}, not refused or failed")

    step = chain["ended_at_step"]
    now = int(time.time())

    if step in ("screen", "optimise"):
        with researchdb.get_conn() as conn:
            live, _given_back = chain_runner._live_and_given_back_draws(conn, chain, step)
        if live:
            raise _refuse(409, "retry-needs-new-calendar",
                          f"chain {chain_id}'s {step} step still holds a live granted draw")
        won = chain_runner._write_transition(chain_id, chain["state"], chain_runner._ACTIVE_STATE_FOR_STAGE[step], "page", now)
    elif step == "walkforward":
        # The EXCEPT case (4.5): even a GIVEN-BACK walk-forward draw still blocks a plain retry,
        # because check 2 would read the earlier, failed twin as the first draw at or after the
        # optimise end regardless.
        with researchdb.get_conn() as conn:
            live, given_back = chain_runner._live_and_given_back_draws(conn, chain, "walkforward")
        if live or given_back:
            raise _refuse(409, "retry-needs-new-calendar",
                          f"chain {chain_id}'s walkforward step already holds a draw under this calendar")
        won = chain_runner._write_transition(chain_id, chain["state"], "walking-forward", "page", now)
    elif step == "replay":
        won = chain_runner._write_transition(chain_id, chain["state"], "replaying", "page", now, set_retry_requested=1)
    elif step == "judge":
        if chain["reason_word"] in ("replay-absent", "replay-unverifiable"):
            won = chain_runner._write_transition(chain_id, chain["state"], "replaying", "page", now, set_retry_requested=1)
        else:
            won = chain_runner._write_transition(chain_id, chain["state"], "judging", "page", now, set_retry_requested=0)
    else:
        # 'choose' (unreachable in practice) and anything else this table does not name: the
        # route's own defensive fallback, so an unrecognised step is refused rather than silently
        # accepted or crashing.
        raise _refuse(409, "chain-not-retryable", f"chain {chain_id}'s ended_at_step {step!r} has no RETRY rule")

    if won:  # a lost compare-and-set wrote nothing, so there is nothing to tell the page
        _publish_chain_changed([chain_id])
    return JSONResponse(status_code=202, content={"status": "chain-retry-accepted", "chain_id": chain_id})
