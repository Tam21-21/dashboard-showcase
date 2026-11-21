"""Paper-trading routes: the browser-facing side of paper.db (paperdb.py).

Registered by main.py in the night-one pre-dispatch commit so lane P fills this file without
touching the route table. Lane P owns everything below the router line.

Night one, lane P (#372, plan section 7). Two structural guards this file must keep true, checked
by the probe suite and by the lead's own read: the Promote route calls paperdb.promote and nothing
else (no venue, no network, no other table write), and Kill always runs through paperdb.StubVenue,
never a live broker, because no live venue adapter exists in this repo yet (#307).

Night plan 2026-09-12 (set 5 day, lane PP, U.8) widens this file: DETAILS' fills route, THE TRADES
table's W/L figures, HISTORY's derived figures and RETIRED IN PAPER, and a window selector on the
summary strip's UP OR DOWN / TRADES statistics (brief section 6.2). Kill and Promote become async
so they can publish an SSE event (dashboard.backend.sse.broadcaster, the same idiom
routes/research.py already uses) after their own transaction commits, replacing PaperPage.tsx's two
refetchInterval polls (plan section 9 item 5).
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from .. import paperdb
from ..alpaca_paper_venue import PaperVenueNotConfigured, PaperVenueUnreachable, get_paper_venue
from ..sse import broadcaster

# Browser: no auth dep here (Caddy basic auth handles humans at the edge; see routes/research.py's
# own browser_router for the same convention this file follows).
router = APIRouter(prefix="/api/paper", tags=["paper"])

# The summary strip's window selector (plan section 9 item 1, brief section 6.2): three named
# calendar spans, no other value accepted. A caller sending anything else gets a 422 naming the set
# (rule 3: "a page never invents a fourth word", enforced here on the route's own input side too).
_SUMMARY_WINDOWS = ("today", "this_week", "since_start")


def _now_iso() -> str:
    """UTC now as an ISO-shaped string, the same "%Y-%m-%dT%H:%M:%SZ" form the paper_backend probe
    suite's own now_iso() helper uses and the form every existing *_at column in paper.db already
    holds, so a value this route writes reads identically to one a probe or a future paper node
    process writes.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_start_iso(window: str, now: datetime) -> "str | None":
    """The window selector's own start boundary, computed in UTC because paper.db's own clock is
    UTC throughout (_now_iso's own form above) and the browser's timezone never reaches this
    route; a display dial, inline-justified rather than sourced (root CLAUDE.md #204). 'today' is
    the current UTC calendar day; 'this_week' is the current ISO week (Monday 00:00 UTC);
    'since_start' has no shared clock boundary at all (None): that window's own baseline is each
    RUN's own admission snapshot, read per-run by the caller, never a boundary every run shares
    (brief section 6.2's own distinction, quoted in this route's own docstring below).
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "since_start":
        return None
    if window == "today":
        return start_of_day.strftime("%Y-%m-%dT%H:%M:%SZ")
    if window == "this_week":
        start_of_week = start_of_day - timedelta(days=start_of_day.weekday())
        return start_of_week.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError(f"unknown window {window!r}")  # unreachable: the route validates window first


def _baseline_equity(conn: sqlite3.Connection, paper_run_id: int, window_start_iso: "str | None"):
    """One run's own baseline equity for the window selector (brief section 6.2, quoted verbatim
    in the summary route's own docstring): the first paper_equity snapshot AT OR AFTER
    window_start_iso, or -- for 'since_start' (window_start_iso is None) -- the run's own EARLIEST
    snapshot, which admit_run always writes at admission (paperdb.admit_run's own E5 comment).
    Returns None when no qualifying snapshot exists: today, with no live paper node yet writing a
    snapshot per completed bar (paperdb.py's only INSERT INTO paper_equity site is admit_run
    itself), a run admitted before the window opened and never marked or filled since genuinely
    has no snapshot inside a 'today' or 'this_week' window, and None is the honest answer rather
    than a fabricated baseline (see get_summary's own docstring for how the caller reads this).
    """
    if window_start_iso is None:
        row = conn.execute(
            "SELECT equity FROM paper_equity WHERE paper_run_id = ? ORDER BY as_of ASC LIMIT 1",
            (paper_run_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT equity FROM paper_equity WHERE paper_run_id = ? AND as_of >= ? "
            "ORDER BY as_of ASC LIMIT 1",
            (paper_run_id, window_start_iso),
        ).fetchone()
    return row["equity"] if row is not None else None


def _serialize_run(conn: sqlite3.Connection, run_row: sqlite3.Row) -> dict:
    """Turn one paper_runs row into the JSON shape the page reads: the run's own columns, its
    current slice state, its equity series capped at the newest 500 points, its parsed expected
    bounds, and the newest promotion request for it (or None when it has never asked to promote).

    Section 7's own escape hatch used to apply to one field: "the counts of paper_fills with
    positive and negative realised result where the schema makes that derivable, else the field is
    absent and the report says so." Lane PP (night plan 2026-09-12, set 5 day) makes it derivable:
    paperdb.trade_stats reuses the SAME average-cost walk paperdb._recompute_slice_state owns
    (paperdb._walk_average_cost, the one place that arithmetic runs), so this field is now the
    real per-run win and loss counts rather than a second, independently-computed implementation.
    """
    paper_run_id = run_row["paper_run_id"]

    # paper_slice_current always exists for a run admit_run ever created (E3: both rows are written
    # in the same transaction), so a run reaching this function with none is a genuine schema
    # invariant violation, never a state to paper over with a fallback value.
    slice_row = conn.execute(
        "SELECT cash, position_value, equity, realised_pnl, committed_capital, updated_at "
        "FROM paper_slice_current WHERE paper_run_id = ?",
        (paper_run_id,),
    ).fetchone()
    if slice_row is None:
        raise HTTPException(
            status_code=500,
            detail=f"paper_run_id={paper_run_id} has no paper_slice_current row",
        )

    # The newest 500 equity points, read newest-first (the index this query uses is unordered, so
    # ORDER BY as_of DESC LIMIT 500 is what actually caps it), then reversed back to chronological
    # order for a caller that wants first-to-latest.
    equity_rows = conn.execute(
        "SELECT as_of, equity FROM paper_equity WHERE paper_run_id = ? "
        "ORDER BY as_of DESC LIMIT 500",
        (paper_run_id,),
    ).fetchall()
    equity_series = [[r["as_of"], r["equity"]] for r in reversed(equity_rows)]

    expected_bounds = None
    if run_row["expected_bounds_json"] is not None:
        expected_bounds = json.loads(run_row["expected_bounds_json"])

    promotion_row = conn.execute(
        "SELECT promotion_id, requested_at, state, decided_at, decision_ledger_entry "
        "FROM paper_promotions WHERE paper_run_id = ? "
        "ORDER BY requested_at DESC, promotion_id DESC LIMIT 1",
        (paper_run_id,),
    ).fetchone()
    latest_promotion = dict(promotion_row) if promotion_row is not None else None

    # The card's own wins/losses (plan section 9 item 6), the real figure now that
    # paperdb.trade_stats exists; win_count/loss_count are always real integers (0 on a run with no
    # closing sell yet), never absent, so the card can show "0 W / 0 L" honestly on a fresh run.
    stats = paperdb.trade_stats(conn, paper_run_id)

    return {
        "paper_run_id": paper_run_id,
        "strategy_id": run_row["strategy_id"],
        "strategy_name": run_row["strategy_name"],
        "strategy_version": run_row["strategy_version"],
        "source_run_id": run_row["source_run_id"],
        "slice_opening_cash": run_row["slice_opening_cash"],
        "expected_bounds": expected_bounds,
        "started_at": run_row["started_at"],
        "ended_at": run_row["ended_at"],
        "end_reason": run_row["end_reason"],
        "kill_started_at": run_row["kill_started_at"],
        "kill_step": run_row["kill_step"],
        "slice": dict(slice_row),
        "equity_series": equity_series,
        "latest_promotion": latest_promotion,
        # Real figures now (see this function's own docstring above), never a fabricated zero and
        # never absent: a run with no closing sell yet reads win_count=0, loss_count=0 honestly.
        "realised_result_counts": {"win_count": stats["win_count"], "loss_count": stats["loss_count"]},
        # Every run's Kill can only ever run through paperdb.StubVenue (no live venue adapter
        # exists yet, #307), so the card must say so before a Kill is ever pressed, not only in
        # the kill response after the fact (the lead's read_1 finding 1). "stub" is the only
        # value this field can ever hold today.
        "venue": "stub",
    }


@router.get("/runs")
def get_running_runs():
    """Currently running strategies, oldest first (paperdb.list_running_runs' own order), each
    joined with its slice, equity series, parsed bounds and newest promotion request.
    """
    with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
        rows = paperdb.list_running_runs(conn)
        return [_serialize_run(conn, row) for row in rows]


@router.get("/history")
def get_history():
    """Every past paper run, newest ended first (paperdb.list_ended_runs' own order): the History
    disclosure's own data. Returns an empty list on a fresh store, never an error (E8).

    Widened (plan section 9 item 4) with the same derived figures a running card shows: the slice
    (so the return fraction can be shown), the trade count and W/L from paperdb.trade_stats, and
    retired_in_paper (end_reason == 'killed', #376 ruling 5). paper_slice_current's own row
    outlives the run itself (nothing deletes it on end), so this read is honest about what is
    actually stored today rather than a fresh computation: it is the LAST state Kill or Promote
    left the slice in, which for a killed run means whatever positions closed while it left paper.
    """
    with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
        rows = paperdb.list_ended_runs(conn)
        result = []
        for row in rows:
            paper_run_id = row["paper_run_id"]
            entry = dict(row)
            slice_row = conn.execute(
                "SELECT cash, position_value, equity, realised_pnl, committed_capital, updated_at "
                "FROM paper_slice_current WHERE paper_run_id = ?",
                (paper_run_id,),
            ).fetchone()
            entry["slice"] = dict(slice_row) if slice_row is not None else None
            opening = row["slice_opening_cash"]
            entry["return_fraction"] = (
                (slice_row["equity"] - opening) / opening
                if slice_row is not None and opening != 0.0
                else None
            )
            stats = paperdb.trade_stats(conn, paper_run_id)
            entry["win_count"] = stats["win_count"]
            entry["loss_count"] = stats["loss_count"]
            entry["trade_count"] = stats["win_count"] + stats["loss_count"]
            entry["retired_in_paper"] = row["end_reason"] == "killed"
            result.append(entry)
        return result


@router.get("/summary")
def get_summary(window: str = Query(default="today")):
    """The page's summary strip (plan section 9 item 1, brief section 6.2). CURRENT CAPITAL is the
    sum over running runs of cash + position_value (paper_slice_current.equity already IS that
    sum, paperdb._compute_equity's own formula): the brief's original wording adds "the pot's
    uncommitted remainder", but StubVenue has no account read at all (no live venue adapter
    exists, #307), so that remainder is NOT READABLE today and this route never fabricates it; the
    page's own hover says so instead of showing an invented pot figure (spec 9.1: "if any pot
    figure appears as a literal ... that is a defect").

    UP OR DOWN and TRADES are both scoped to `window` (today / this_week / since_start, brief
    section 6.2, quoted here verbatim): "the change in the RUNNING runs' equity across the
    selected window: for each run the page counts, its current equity minus its equity at the
    first paper_equity snapshot at or after the window's start, summed over those runs." Only
    running runs that HAVE a qualifying baseline (see _baseline_equity's own docstring) contribute
    to the sum; a running run with fills but no snapshot after the window opened -- today's real
    state for almost every run, since no live paper node yet writes a periodic snapshot -- simply
    is not counted in it, and delta_fraction reads None (an honest absence) rather than 0.0 when
    NO running run has a qualifying baseline, matching the brief's own t-zero rule that a zero must
    be a measured flat result, never a stand-in for "nothing to compare".

    FEES PAID SO FAR is a lifetime total across every run this store has ever recorded a fill for,
    running or ended, NOT scoped to the window (plan section 9 item 1's own wording keeps it
    separate from "TRADES in that window").
    """
    if window not in _SUMMARY_WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"window must be one of {', '.join(_SUMMARY_WINDOWS)}; got {window!r}.",
        )
    now = datetime.now(timezone.utc)
    window_start_iso = _window_start_iso(window, now)

    with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
        running = paperdb.list_running_runs(conn)
        total_equity = 0.0
        total_opening = 0.0
        delta_sum = 0.0
        baseline_sum = 0.0
        any_baseline = False
        trades_in_window = 0
        for run in running:
            paper_run_id = run["paper_run_id"]
            slice_row = conn.execute(
                "SELECT equity FROM paper_slice_current WHERE paper_run_id = ?",
                (paper_run_id,),
            ).fetchone()
            # Same invariant as _serialize_run above: a running run always has a slice row.
            if slice_row is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"paper_run_id={paper_run_id} has no paper_slice_current row",
                )
            total_equity += slice_row["equity"]
            total_opening += run["slice_opening_cash"]

            baseline = _baseline_equity(conn, paper_run_id, window_start_iso)
            if baseline is not None:
                any_baseline = True
                delta_sum += slice_row["equity"] - baseline
                baseline_sum += baseline

            if window_start_iso is None:
                fills_count_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM paper_fills "
                    "WHERE paper_run_id = ? AND filled_at IS NOT NULL",
                    (paper_run_id,),
                ).fetchone()
            else:
                fills_count_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM paper_fills "
                    "WHERE paper_run_id = ? AND filled_at IS NOT NULL AND filled_at >= ?",
                    (paper_run_id, window_start_iso),
                ).fetchone()
            trades_in_window += fills_count_row["n"]

        requested_count = conn.execute(
            "SELECT COUNT(*) AS n FROM paper_promotions WHERE state = 'requested'"
        ).fetchone()["n"]
        fees_total_row = conn.execute(
            "SELECT COALESCE(SUM(fees), 0.0) AS total FROM paper_fills WHERE filled_at IS NOT NULL"
        ).fetchone()

    # No qualifying baseline anywhere (a fresh store, every run ended, or no running run's equity
    # was snapshotted inside this window) means there is no honest percentage to report: a delta
    # with nothing to compare against is undefined, not zero, so this reads None rather than a
    # fabricated 0.0 the page would otherwise render as "unchanged" (brief section 6.2's own rule).
    delta_fraction = (
        None if not any_baseline or baseline_sum == 0.0 else delta_sum / baseline_sum
    )

    return {
        "total_equity": total_equity,
        "total_opening_cash": total_opening,
        "delta_fraction": delta_fraction,
        "running_count": len(running),
        "promotions_requested_count": requested_count,
        "trades_in_window": trades_in_window,
        "fees_paid_total": fees_total_row["total"],
        "window": window,
    }


@router.get("/runs/{run_id}/fills")
def get_run_fills(run_id: int):
    """DETAILS' own data (plan section 9 items 2 and 3): every fill for one run (paperdb.list_fills,
    filled rows and refusal rows both present) plus paperdb.trade_stats' aggregate W/L figures, with
    each fill's own realised gain merged in from trade_stats' per_fill_realised_gain (None on a buy
    fill or a refusal row, since neither ever closes a position). 404 when the run itself does not
    exist, so an empty fills list always means "no fills yet on a real run", never "this run id was
    never real" (the same distinction paperdb.PaperSliceStateMissing draws elsewhere in this file).
    """
    with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
        run_row = conn.execute(
            "SELECT paper_run_id FROM paper_runs WHERE paper_run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"paper_run_id={run_id} does not exist.")
        fills = paperdb.list_fills(conn, run_id)
        stats = paperdb.trade_stats(conn, run_id)

    per_fill_gain = stats.pop("per_fill_realised_gain")
    for fill in fills:
        fill["realised_gain"] = per_fill_gain.get(fill["fill_id"])
        # "opened or closed": long-only (paperdb.PaperLongOnlyViolation forbids a short), so a buy
        # always opens or adds to a position and a sell always reduces or closes one; a refusal row
        # (filled_at IS NULL) opened or closed nothing, since it never reached the venue.
        if fill["filled_at"] is None:
            fill["position_effect"] = None
        else:
            fill["position_effect"] = "opened" if fill["side"] == "buy" else "closed"

    return {"fills": fills, "trade_stats": stats}


_MARK_SOURCE = "alpaca latest trade, IEX"
_POSITION_EPSILON = 1e-9  # the same closed-position line paperdb._OPEN_POSITION_EPSILON draws


def _read_venue(symbols: list) -> dict:
    """The two Alpaca reads for the holdings route, run in a worker thread. Never raises for a
    venue problem: each read's failure comes back as its own error text, so the page can say why a
    mark or a position is missing. Reads only; nothing here can place, cancel or close an order."""
    out = {"trades": {}, "trades_error": None, "positions": None, "positions_error": None}
    try:
        venue = get_paper_venue()
    except (PaperVenueNotConfigured, PaperVenueUnreachable) as exc:
        out["trades_error"] = str(exc)
        out["positions_error"] = str(exc)
        return out
    if symbols:
        try:
            out["trades"] = venue.latest_trades(symbols)
        except (PaperVenueNotConfigured, PaperVenueUnreachable) as exc:
            out["trades_error"] = str(exc)
    try:
        out["positions"] = venue.position_quantities()
    except (PaperVenueNotConfigured, PaperVenueUnreachable) as exc:
        out["positions_error"] = str(exc)
    return out


@router.get("/runs/{run_id}/holdings")
async def get_run_holdings(run_id: int):
    """THE HOLDINGS table (DECISIONS #477 item 5): per stock what this run holds, its average cost,
    its worth now and open profit or loss. Quantity and cost come from our own fills (several runs
    share one Alpaca account); the mark is Alpaca's latest IEX trade, display data that never
    enters a research path or an order. `reconcile` compares our holdings summed over every open
    run with Alpaca's positions. Alpaca missing or down still answers 200 with the reason in the
    fields. A read: it places nothing.
    """
    try:
        with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
            walk = paperdb.holdings_and_cash(conn, run_id)
            open_ids = [r["paper_run_id"] for r in conn.execute(
                "SELECT paper_run_id FROM paper_runs WHERE ended_at IS NULL "
                "ORDER BY paper_run_id ASC").fetchall()]
            ours_total: dict = {}
            for open_id in open_ids:
                for symbol, held in paperdb.holdings(conn, open_id).items():
                    ours_total[symbol] = ours_total.get(symbol, 0.0) + held["quantity"]
    except paperdb.PaperRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except paperdb.PaperNegativeNetQuantityInvariant as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    per_symbol = walk["per_symbol"]
    symbols = sorted(per_symbol)
    venue = await asyncio.to_thread(_read_venue, sorted(set(symbols)))

    rows = []
    unpriced = []
    for symbol in symbols:
        held = per_symbol[symbol]
        quantity, average_cost = held["quantity"], held["average_cost"]
        trade = venue["trades"].get(symbol)
        if venue["trades_error"] is not None:
            reason = venue["trades_error"]
        elif trade is None:
            reason = "no trade in Alpaca's answer"
        else:
            reason = None
        row = {"symbol": symbol, "quantity": quantity, "average_cost": average_cost,
               "mark_price": None, "mark_source": None, "mark_time": None, "value": None,
               "open_pnl": None, "share": None, "mark_unavailable_reason": reason}
        if reason is None:
            price = trade["price"]
            row.update(mark_price=price, mark_source=_MARK_SOURCE, mark_time=trade["time"],
                       value=quantity * price, open_pnl=quantity * price - quantity * average_cost)
        else:
            unpriced.append(symbol)
        rows.append(row)

    cash = walk["cash"]
    marked_equity = None
    share_reason = None
    if unpriced:
        share_reason = "a mark is unavailable: " + ", ".join(unpriced)
    else:
        marked_equity = cash + sum(row["value"] for row in rows)
        if marked_equity <= 0:
            share_reason = "marked equity is not above zero"
        else:
            for row in rows:
                row["share"] = row["value"] / marked_equity

    positions = venue["positions"]
    reconcile = []
    for symbol in sorted(set(ours_total) | set(positions or {})):
        ours = ours_total.get(symbol, 0.0)
        if positions is None:
            alpaca, mismatch = None, None
        else:
            alpaca = positions.get(symbol, 0.0)
            mismatch = abs(ours - alpaca) > _POSITION_EPSILON
        reconcile.append({"symbol": symbol, "ours_quantity": ours, "alpaca_quantity": alpaca,
                          "mismatch": mismatch})

    return {"paper_run_id": run_id, "cash": cash, "marked_equity": marked_equity, "rows": rows,
            "share_unavailable_reason": share_reason, "reconcile": reconcile,
            "alpaca_positions_unavailable_reason": venue["positions_error"]}


@router.get("/retired")
def get_retired():
    """Every strategy RETIRED IN PAPER (paperdb.list_retired_strategies, #376 ruling 5). This
    lane's own surfaces (the History drawer) read the derived boolean straight off each history
    row instead of calling this route a second time; this route exists so a future reader -- named
    in the night plan as U.4's own catalogue mark, tomorrow's lane, not this one -- has one place
    to ask "which strategies has paper trading retired" without joining into paper.db itself.
    """
    with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
        return paperdb.list_retired_strategies(conn)


@router.post("/runs/{run_id}/kill")
async def post_kill(run_id: int):
    """Press Kill on one run. Always runs through paperdb.StubVenue: no live broker adapter exists
    in this repo (#307), so a stub kill is what this route can honestly offer, and the response
    says so (venue: "stub") rather than letting the card read a stub close as a real one.

    Async (plan section 9 item 5, replacing PaperPage.tsx's own refetchInterval poll): publishes
    paper.run_killed on dashboard.backend.sse.broadcaster AFTER the transaction that presses Kill
    has committed, the same "write first, publish after" order routes/research.py's own routes use
    (see e.g. create_job), so a subscriber that reacts to the event by re-fetching always reads the
    row this route just wrote, never a state that has not committed yet.
    """
    now = _now_iso()
    try:
        with paperdb.paper_transaction(paperdb.DB_PATH) as conn:
            paperdb.press_kill(conn, run_id, paperdb.StubVenue(), now)
            row = conn.execute(
                "SELECT * FROM paper_runs WHERE paper_run_id = ?", (run_id,)
            ).fetchone()
    except paperdb.PaperSliceStateMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = dict(row)
    result["venue"] = "stub"
    # Only a Kill that actually finished (end_reason set) is "run_killed"; a resumed press that
    # only advanced an intermediate step (brief section 6.4: pressing Kill again resumes rather
    # than restarts) is not yet a terminal event, and publishing one early would tell a subscriber
    # the run has left paper before paper_runs itself agrees.
    if result.get("end_reason") == "killed":
        await broadcaster.publish("paper.run_killed", {"paper_run_id": run_id})
    return result


@router.post("/runs/{run_id}/approve-real-money")
async def post_approve_real_money(run_id: int):
    """APPROVE FOR REAL MONEY for one run (DECISIONS #481 item 2, #487 item 1). Closes the run out
    as STOP does, always through paperdb.StubVenue (which closes nothing until lane P6b), then ends
    it 'promoted'. The response says venue "stub". No order, no real-money store, no event.
    """
    now = _now_iso()
    try:
        with paperdb.paper_transaction(paperdb.DB_PATH) as conn:
            result = paperdb.approve_for_real_money(conn, run_id, paperdb.StubVenue(), now)
    except paperdb.PaperRunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except paperdb.PaperApproveRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result["venue"] = "stub"
    return result


@router.post("/runs/{run_id}/promote")
async def post_promote(run_id: int):
    """Request promotion to real-capital trading for one run. Calls paperdb.promote and nothing
    else: no venue, no network, no other table (the structural guard the plan names). Writes
    exactly one paper_promotions row in state 'requested'; no function anywhere in this codebase
    can move a row past that word, per paperdb.promote's own docstring.

    Async for the same reason post_kill is (plan section 9 item 5): publishes paper.run_promoted
    after promote()'s own transaction commits. Note this event fires on the REQUEST, not on a
    later grant: paper_runs.end_reason only becomes 'promoted' when Promote itself ends the run
    (paperdb.promote's own docstring), which is a later, Money-path-gated step this route does not
    reach, so "requested" is the honest name for what just happened here. Only APPROVE FOR REAL MONEY
    (post_approve_real_money below) writes 'promoted'.
    """
    now = _now_iso()
    try:
        with paperdb.paper_transaction(paperdb.DB_PATH) as conn:
            promotion_id = paperdb.promote(conn, run_id, now)
    except paperdb.PaperPromoteWhileKilling as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await broadcaster.publish("paper.run_promoted", {"paper_run_id": run_id, "promotion_id": promotion_id})
    return {"promotion_id": promotion_id, "state": "requested"}


# Paper limits (lane UI19-1a, DECISIONS #477 items 4 and 5, #478 items 1 and 2). This lane only
# stores and serves them; submit_paper_order does not read them yet (UI19-1b).
_LIMIT_UNITS = {
    "paper.per_order_notional_cap": "dollars",
    "paper.ticker_net_cap": "dollars",
    "paper.total_net_cap": "dollars",
    "paper.loss_limit_window": "dollars",
    "paper.loss_limit_since_go": "dollars",
    "paper.order_rate_cap": "orders per 60 seconds",
}
_LIMIT_MEANINGS = {
    "paper.per_order_notional_cap": "the most one paper order may be worth",
    "paper.ticker_net_cap": "the most all paper runs together may hold in one stock",
    "paper.total_net_cap": "the most all paper runs together may hold in all stocks",
    "paper.loss_limit_window": "the largest drop in paper equity allowed since the start of today's New York day",
    "paper.loss_limit_since_go": "the largest drop in paper equity allowed across every paper run, counted from the last time the paper limits were saved",
    "paper.order_rate_cap": "how many paper orders may reach the venue in one minute",
}


def _limits_body(conn: sqlite3.Connection) -> dict:
    return {
        "limits": [
            {
                "key": row["key"],
                "value": row["value"],
                "unit": _LIMIT_UNITS[row["key"]],
                "meaning": _LIMIT_MEANINGS[row["key"]],
            }
            for row in paperdb.get_paper_limits(conn)
        ]
    }


def _limits_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=f"The paper limits cannot be read or saved right now, the paper store is not available: {exc}",
    )


@router.get("/limits")
def get_limits():
    """The six paper limits in their fixed order; value is null while Tamer has not set it."""
    try:
        with paperdb.get_paper_conn(paperdb.DB_PATH) as conn:
            return _limits_body(conn)
    except (paperdb.PaperDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _limits_unavailable(exc) from exc


@router.put("/limits")
def put_limits(values: dict):
    """Set any subset of the six limits, all or nothing; returns the same body as GET. A refused
    value answers 422 carrying the refusal word."""
    try:
        with paperdb.paper_transaction(paperdb.DB_PATH) as conn:
            paperdb.set_paper_limits(conn, values, _now_iso())
            return _limits_body(conn)
    except paperdb.PaperLimitValueRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (paperdb.PaperDbParentDirectoryMissing, sqlite3.OperationalError) as exc:
        raise _limits_unavailable(exc) from exc
