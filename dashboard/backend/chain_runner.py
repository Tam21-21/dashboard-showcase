"""The chain runner (lane CN, set 16c, `#427`), governed by
docs/briefs/chain_runner_spec_2026-09-24.md. A small loop, living inside the dashboard backend
process, that walks one candidate through screen -> optimise -> choose -> replay -> walkforward ->
judge automatically, on its own private calendar (`chain-<uuid4 hex>`), recording every state it
passes through in the two new tables `research_chains` / `research_chain_transitions`
(researchdb.py's own `_DDL`). Nothing is held in memory between passes (spec section 3): every
pass reads the chain row plus the existing stores (draws, runs, jobs, replay cache, verdicts) and
re-derives what to do next, so a backend restart simply resumes.

Plain-English map of this file, top to bottom:
  - CHAIN_RUNNER_MONITOR_COUNTERS and the seven K-query functions (section 11's list K).
  - run_pass_once(now): the entry point main.py's own loop calls once per tick.
  - _process_one_chain: the per-chain dispatch table (STOP, waiting, the three stage states,
    choosing-cell, replaying, judging).
  - _process_stage_step / _attempt_stage_dispatch: the screen/optimise/walkforward machinery
    (section 1.1, section 4.1, section 4.4's adoption rules).
  - _process_replay_step / _process_judge_step: the replay and judge steps (section 1.2, P-1).
  - _handle_duel_check: the duel's own comparison (section 7).
  - _write_transition and _end_chain: the one compare-and-set write every state change goes
    through (section 4.3, section 8's atomicity argument).
"""
from __future__ import annotations

import json  # param_grid_json round-trips through this, and P-1's JSONResponse bodies are read back through it
import logging  # a crashed chain's own trace is logged here, never swallowed (spec section 4.2, "A step refused")
import sqlite3  # narrows a dispatch_stage OperationalError to the same two lock-contention message shapes tracker.py itself narrows on
import time  # wall-clock "now" for a real pass, and the pass-duration gauge (K-11)

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from dashboard.backend import generated, judgedb, research_prechecks, researchdb, stage_runner, tracker
from dashboard.backend.routes import judge as judge_routes
from research_lab import universe as universe_rule
from research_lab.fetcher.ops import quarantine_242

log = logging.getLogger(__name__)
# The standard library idiom for a module that logs (docs.python.org, "Configuring Logging for a
# Library"): a bare NullHandler here means a script that imports this module without configuring
# any logging (every probe in this suite; a bare python -c smoke check) prints nothing by default,
# while a real caller that DOES configure logging (main.py, at boot) still receives every record
# unchanged, since a NullHandler only stops the no-handler-found fallback, never a real handler
# further up the logger chain.
log.addHandler(logging.NullHandler())

# -------------------------------------------------------------------------------------------
# Section 11 (list K), the monitor counters and gauges (K-1, K-5, K-6, K-11). Process-local,
# in-memory, matching every sibling counter dict in this backend (STAGE_RUNNER_MONITOR_COUNTERS,
# GRID_SHAPE_MONITOR_COUNTERS): an absent key means zero since process start, never "not checked".
# -------------------------------------------------------------------------------------------
CHAIN_RUNNER_MONITOR_COUNTERS: dict = {
    # K-1: whether the loop is alive. GAUGE last_pass_at (an epoch second, None before the first
    # pass ever runs -- section 10's own FRESH rule, "no pass yet", never 0) and COUNTER passes.
    "chain_runner_last_pass_at": None,
    "chain_runner_passes": 0,
    # K-5: a dispatch_stage call lost a write-lock race (the SAME two SQLite message shapes
    # tracker.py itself narrows on: "database is locked" / "database table is locked").
    "chain_runner_lock_contention": 0,
    # K-6: an adoption fired -- a crash mid-step, or a second advancer, rather than a fresh draw,
    # universe or held-out row. Keyed by which of the five 4.4 rules fired.
    "chain_runner_adoptions": {"draw": 0, "chronology": 0, "link": 0, "universe": 0, "held_out": 0},
    # K-11: how long the last pass took, in seconds. A GAUGE, not a running total.
    "chain_runner_last_pass_seconds": None,
}

# K-15 and K-16 (lane P1, DECISIONS #432): the two catch-all sites that end no chain, counted. Tamer
# ruled on 2026-10-07 that a catch-all is allowed only to keep a background loop alive or to isolate
# one item, and only if it logs the full error and records the failure where a person can see it.
# The step-level sites already record it on the chain row (ended `failed`, the error in
# reason_detail); these two cannot, so each crash is counted here and read live at GET
# /api/ops/counters (routes/ops.py). A flat dict of ints, kept apart from the gauges above, because
# that route reports every value as a counter. Process-local, reset on restart, like every sibling.
CHAIN_RUNNER_CRASH_COUNTERS: dict = {
    # K-15: one chain's processing raised past every step handler (run_pass_once's per-chain site).
    # The chain keeps its state and is tried again next pass; this count says it keeps failing.
    "chain_pass_crashes": 0,
    # K-16: a whole pass raised (main.py's loop keeper). The loop sleeps and runs the next pass.
    "loop_pass_crashes": 0,
}


def count_loop_pass_crash() -> None:
    """K-16: main.py's loop keeper calls this when a whole pass raised. The increment lives here, in
    this module's own source, so routes/ops.py's declared-only scan (which reads the counter dict's
    own module file) sees the key as live."""
    CHAIN_RUNNER_CRASH_COUNTERS["loop_pass_crashes"] += 1


# T-1/S-1: the state a chain enters when a pass starts (or continues) each stage step, and the
# step word T-1 records on ended_at_step when that stage step ends the chain.
_STAGE_FOR_ACTIVE_STATE = {"screening": "screen", "optimising": "optimise", "walking-forward": "walkforward"}
_ACTIVE_STATE_FOR_STAGE = {v: k for k, v in _STAGE_FOR_ACTIVE_STATE.items()}
_NEXT_STATE_FOR_STAGE = {"screen": "optimising", "optimise": "choosing-cell", "walkforward": "judging"}

# The state-to-step map T-1 itself gives (section 5): every non-terminal state's own T-1 step,
# used by STOP (section 4.2: "the current step as ended_at_step").
_STEP_FOR_STATE = {
    "waiting": "screen", "screening": "screen", "optimising": "optimise", "choosing-cell": "choose",
    "replaying": "replay", "walking-forward": "walkforward", "judging": "judge",
}

# Every state a pass ever acts on automatically: the seven ACTIVE + WAITING-ON-TAMER states of
# S-1. done/stopped/refused/failed are TERMINAL or RETRYABLE TERMINAL: a pass never touches them
# on its own (only an explicit RETRY route call can, spec section 4.5).
_NON_TERMINAL_STATES = ("waiting", "screening", "optimising", "choosing-cell", "replaying", "walking-forward", "judging")

# Each stage's own pair of chain-row columns naming its window (spec section 2.2's four boundaries).
_STAGE_WINDOW_KEYS = {
    "screen": ("screen_start_ts", "optimise_start_ts"),
    "optimise": ("optimise_start_ts", "walkforward_start_ts"),
    "walkforward": ("walkforward_start_ts", "walkforward_end_ts"),
}

# R-1, restated as EXACT exception type -> word (spec section 5's own table). Matched by exact
# type, never by isinstance/subclass order, which is what makes "every class below ValueError is
# matched before the plain ValueError row" automatically true: a subclass's own exact entry is
# always checked before the generic ValueError fallback in _classify_exception below, regardless
# of where either sits in this dict's own insertion order.
_R1_EXACT_TYPE_TO_WORD = {
    stage_runner.CandidateVersionMoved: "candidate-version-moved",
    stage_runner.DailyStoreNotConfigured: "daily-store-not-configured",
    stage_runner.LeadInUnavailable: "lead-in-unavailable",
    stage_runner.WarmupUnsatisfied: "warmup-unsatisfied",
    stage_runner.AmbiguousBakedQuantileConstant: "quantile-constant-ambiguous",
    stage_runner.WindowTooSmallForScheme: "walkforward-window-too-small",
    stage_runner.ChosenTickerNotQualifying: "walkforward-chosen-not-qualifying",  # DECISIONS #434
    stage_runner.LookbackNotDeclared: "walkforward-lookback-undeclared",
    stage_runner.UnknownStageWord: "stage-word-unknown",  # unreachable: the runner only ever passes STAGE_WORDS literals
    researchdb.WalkforwardParameterAbsent: "walkforward-parameter-absent",
    research_prechecks.QuantileOutOfRange: "quantile-out-of-range",
    generated.GeneratedSourceDrift: "generated-source-drift",
    researchdb.StrategyNotFound: "strategy-not-found",
    researchdb.StrategyDeleted: "strategy-deleted",
    researchdb.PriceUniverseNotFound: "price-universe-not-found",
    researchdb.PriceUniverseEmptyError: "price-universe-empty",
    researchdb.PriceUniverseWindowInvalid: "price-universe-window-invalid",
    researchdb.FrozenSweepParameterError: "frozen-sweep-parameter",
    researchdb.UndeclaredSweepParameterError: "undeclared-sweep-parameter",
    researchdb.SweepParameterMissing: "sweep-parameter-missing",
    researchdb.SweepGridTooLarge: "sweep-grid-too-large",
    researchdb.UnknownAssetClass: "unknown-asset-class",
    researchdb.AssetClassScopeMismatch: "asset-class-scope-mismatch",
    tracker.DrawWindowMissing: "draw-window-missing",
    tracker.ReserveCutCrossed: "reserve-cut-crossed",
    universe_rule.UniverseParameterOwed: "universe-parameter-owed",
    universe_rule.ContaminationListUnavailable: "contamination-list-unavailable",
    universe_rule.ContaminationListEmpty: "contamination-list-empty",
    universe_rule.ContaminationListInert: "contamination-list-inert",
    quarantine_242.QuarantineReadError: "contamination-list-unreadable",
    universe_rule.TooFewQualifyingNames: "too-few-qualifying-names",
    universe_rule.HoldoutNotDrawnYet: "holdout-not-drawn",  # unreachable after the screen step's own draw, below
}

# P-1 (spec section 5): the chain's own partition of the chain-link spec's W-1, per calling step.
# WAIT/FAIL/REFUSE word sets, read off the words a route's own HTTPException.detail leads with.
_P1_WORDS = {
    "replay": (
        {"replay-queued", "compute-in-flight", "worker-offline"},
        {"replay-failed", "replay-not-reproduced"},
        {"cell-key-incomplete", "single-run-not-a-replay", "sweep-not-optimise", "walkforward-run-not-gradable",
         "source-link-refused", "sweep-needs-a-cell", "cell-not-found", "replay-mode-unknown",
         "replay-source-incomplete"},
    ),
    "judge": (
        {"walkforward-run-in-flight"},
        set(),
        {"cell-key-incomplete", "single-run-not-a-replay", "sweep-not-optimise", "walkforward-run-not-gradable",
         "source-link-refused", "sweep-needs-a-cell", "cell-not-found", "replay-absent", "replay-unverifiable"},
    ),
}

# W-2 (chain-link spec section 5): check 2's own three link-failure words. A `done` chain whose
# verdict names one of these (K-8) means the calendar rule that guarantees the walk-forward run
# check 2 reads is the SAME one this build's own construction linked -- has regressed.
_W2_LINK_WORDS = {"walkforward-run-not-found", "walkforward-run-ambiguous", "walkforward-link-refused"}


def _classify_exception(exc: Exception) -> "tuple[str, str]":
    """Maps an exception raised by a stage call to (reason_word, terminal_state). EXACT type
    lookup first (so a subclass is never shadowed by a parent-class row, spec section 5's own
    ordering rule made structural rather than relying on except-clause order), a bare ValueError
    fallback second (C-1's own "malformed param_grid" 400 has a chain-runner analogue,
    sweep-request-invalid), and step-crashed last: an exception outside R-1 is never guessed at
    and never swallowed (spec section 4.2, "A step refused")."""
    word = _R1_EXACT_TYPE_TO_WORD.get(type(exc))
    if word is not None:
        return word, "refused"
    if isinstance(exc, ValueError):
        return "sweep-request-invalid", "refused"
    return "step-crashed", "failed"


def _epoch_to_date(ts: int) -> str:
    """The same UTC calendar date string stage_runner._epoch_to_date_str produces, restated here
    (never imported: that helper is private to stage_runner.py) so the universe name this file
    mints matches the deterministic shape section 2.3 names: 'chain <C> <stage> <date> to <date>'."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# =============================================================================
# The seven K-query functions (section 11): K-2, K-3, K-7, K-8, K-9, K-10, K-13. Each takes an
# open connection (the ops-monitor tick's own convention, matching tracker.py's five queries) and
# returns a list of dicts, one per flagged row.
# =============================================================================

def _live_and_given_back_draws(conn, chain: dict, stage: str) -> "tuple[list[dict], list[dict]]":
    """Every granted draw_requested row for this chain's own (candidate, calendar, stage),
    partitioned into LIVE (not given back) and GIVEN BACK (DV's own _GIVEN_BACK_PREDICATE,
    read through tracker.query_draws_given_back so the two modules can never disagree on what
    'given back' means). Spec section 4.1: two or more LIVE draws is stage-draw-duplicated; one
    given-back draw followed by one live draw is a RETRY's normal trace, and the live one is the
    step's own draw."""
    rows = conn.execute(
        "SELECT * FROM research_data_draws WHERE event_kind = 'draw_requested' AND decision = 'granted' "
        "  AND strategy_id = ? AND strategy_version = ? AND calendar_id = ? AND stage = ? ORDER BY id;",
        (chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], stage),
    ).fetchall()
    draws = [dict(r) for r in rows]
    if not draws:
        return [], []
    ids = [d["id"] for d in draws]
    given_back_ids = set(tracker.query_draws_given_back(conn, ids))
    live = [d for d in draws if d["id"] not in given_back_ids]
    given_back = [d for d in draws if d["id"] in given_back_ids]
    return live, given_back


def _find_stage_run(conn, strategy_id: int, strategy_version: int, calendar_id: str, stage: str) -> "int | None":
    """The run this calendar's own stage step is linked to, via the ONLY path from a stage to its
    run: the granted run_linked row over the granted draw_requested row (tracker.draw_id_for_run's
    own precedent, read the other direction). None when no such link exists yet."""
    row = conn.execute(
        "SELECT dl.run_id AS run_id FROM research_data_draws dr "
        "JOIN research_data_draws dl ON dl.draw_id = dr.id AND dl.event_kind = 'run_linked' AND dl.decision = 'granted' "
        "WHERE dr.event_kind = 'draw_requested' AND dr.decision = 'granted' AND dr.stage = ? "
        "  AND dr.strategy_id = ? AND dr.strategy_version = ? AND dr.calendar_id = ? "
        "ORDER BY dr.id DESC LIMIT 1;",
        (stage, strategy_id, strategy_version, calendar_id),
    ).fetchone()
    return row["run_id"] if row is not None else None


def chain_runner_stalled_chains(conn) -> "list[dict]":
    """K-2: active chains whose step run or replay job is stuck -- a `picked_up` job whose
    heartbeat is older than any_worker_online's own 120-second stale_after, a `pending` job while
    no worker is online at all, or a sweep job still `materializing` (the run_sweep job stub the
    poll never claims)."""
    out = []
    for row in conn.execute(
        "SELECT * FROM research_chains WHERE state IN ('screening','optimising','walking-forward');"
    ).fetchall():
        chain = dict(row)
        stage = _STAGE_FOR_ACTIVE_STATE[chain["state"]]
        live, _ = _live_and_given_back_draws(conn, chain, stage)
        if len(live) != 1:
            continue
        link = conn.execute(
            "SELECT * FROM research_data_draws WHERE event_kind = 'run_linked' AND draw_id = ? "
            "  AND decision = 'granted' ORDER BY id DESC LIMIT 1;",
            (live[0]["id"],),
        ).fetchone()
        if link is None:
            continue
        run = conn.execute("SELECT * FROM research_runs WHERE id = ?;", (link["run_id"],)).fetchone()
        if run is None or run["job_id"] is None:
            continue
        job = conn.execute("SELECT * FROM research_jobs WHERE id = ?;", (run["job_id"],)).fetchone()
        if job is None:
            continue
        reason = None
        if job["status"] == "picked_up" and job["last_heartbeat"] is not None \
                and not researchdb.any_worker_online(now=int(time.time())):
            reason = "step-stalled"
        elif job["status"] == "pending" and not researchdb.any_worker_online():
            reason = "no-worker-online"
        elif job["status"] == "materializing":
            reason = "sweep-materializing"
        if reason is not None:
            out.append({"id": chain["id"], "stage": stage, "reason": reason})
    return out


def chains_changed_since(ts: int) -> "list[int]":
    """Lane P7-0: the ids of every chain written at or after second `ts` (its updated_at), oldest id
    first. main.py's loop calls it after a pass, with the second the pass started, to tell the Chains
    page which chains moved; the pass itself cannot publish, it runs in a worker thread."""
    with researchdb.get_conn() as conn:
        rows = conn.execute("SELECT id FROM research_chains WHERE updated_at >= ? ORDER BY id;", (ts,)).fetchall()
    return [int(r["id"]) for r in rows]


def chain_runner_ended_by_word(conn) -> "list[dict]":
    """K-3 (and K-4/K-12/K-14, grouped here rather than in a separate function, per section 11's
    own note): every chain currently `refused` or `failed`, grouped by state, ended_at_step and
    reason_word. step-crashed (K-4), duel-inputs-differ (K-12) and route-answer-unworded (K-14)
    are all just reason_word values this same grouping already covers."""
    rows = conn.execute(
        "SELECT state, ended_at_step, reason_word, COUNT(*) AS n FROM research_chains "
        " WHERE state IN ('refused', 'failed') GROUP BY state, ended_at_step, reason_word;"
    ).fetchall()
    return [dict(r) for r in rows]


def chain_calendar_foreign_draws(conn) -> "list[dict]":
    """K-7: a draw under a chain-shaped calendar (`chain-%`) that no research_chains row owns, or
    two LIVE granted draws for one stage under a calendar this build DOES own (which, by
    construction, should never happen: the chain-draw-duplicated fail word already catches that
    case from the runner's OWN side, and this query is the independent, durable check)."""
    out = []
    for row in conn.execute(
        "SELECT DISTINCT calendar_id FROM research_data_draws "
        " WHERE event_kind = 'draw_requested' AND decision = 'granted' AND calendar_id LIKE 'chain-%' "
        "   AND calendar_id NOT IN (SELECT calendar_id FROM research_chains);"
    ).fetchall():
        out.append({"calendar_id": row["calendar_id"], "reason": "no-matching-chain-row"})
    for row in conn.execute(
        "SELECT calendar_id, stage, strategy_id, strategy_version FROM research_data_draws "
        " WHERE event_kind = 'draw_requested' AND decision = 'granted' AND calendar_id LIKE 'chain-%' "
        " GROUP BY calendar_id, stage, strategy_id, strategy_version HAVING COUNT(*) > 1;"
    ).fetchall():
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM research_data_draws WHERE event_kind = 'draw_requested' AND decision = 'granted' "
            "  AND calendar_id = ? AND stage = ? AND strategy_id = ? AND strategy_version = ?;",
            (row["calendar_id"], row["stage"], row["strategy_id"], row["strategy_version"]),
        ).fetchall()]
        given_back = set(tracker.query_draws_given_back(conn, ids))
        if len(ids) - len(given_back) > 1:  # a given-back-then-live pair is RETRY's own normal trace, not foreign
            out.append({"calendar_id": row["calendar_id"], "stage": row["stage"], "reason": "two-live-granted-draws"})
    return out


def chain_runner_done_check2_unlinked(conn) -> "list[dict]":
    """K-8: `done` chains whose verdict's own check 2 reason is one of W-2's three link words --
    the calendar rule (spec section 2) regressed for this chain: its walk-forward draw was not
    genuinely the first granted draw at or after the optimise end under its own calendar."""
    out = []
    for row in conn.execute(
        "SELECT id, strategy_id, strategy_version, calendar_id, chosen_variation_id, chosen_ticker "
        "FROM research_chains WHERE state = 'done';"
    ).fetchall():
        chain = dict(row)
        # The verdict is keyed on the optimise run R (spec section 1.1; the judge refuses a walk-forward
        # run and reads it through R for check 2), never on the walk-forward run itself.
        optimise_run_id = _find_stage_run(conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "optimise")
        if optimise_run_id is None:
            continue
        verdict = conn.execute(
            "SELECT check_outcomes_json FROM research_judge_verdicts "
            " WHERE run_id = ? AND variation_id = ? AND ticker = ? ORDER BY graded_at DESC, id DESC LIMIT 1;",
            (optimise_run_id, chain["chosen_variation_id"], chain["chosen_ticker"]),
        ).fetchone()
        if verdict is None:
            continue
        for entry in json.loads(verdict["check_outcomes_json"]):
            if entry.get("check") == 2 and entry.get("reason") in _W2_LINK_WORDS:
                out.append({"id": chain["id"]})
                break
    return out


def chain_runner_waiting_for_cell(conn) -> "list[dict]":
    """K-9: chains waiting on Tamer's own pick, with their age (spec section 4.1: 'waits for
    Tamer indefinitely')."""
    now = int(time.time())
    rows = conn.execute("SELECT id, updated_at FROM research_chains WHERE state = 'choosing-cell';").fetchall()
    return [{"id": r["id"], "age_seconds": now - r["updated_at"]} for r in rows]


def chain_runner_stopped_with_run_in_flight(conn) -> "list[dict]":
    """K-10: a STOP that landed while a stage run was still in flight (spec section 4.2's own
    STOP note: "a STOP that lands while a pass is mid-action lets that action finish"). Purely
    informational."""
    out = []
    for row in conn.execute(
        "SELECT * FROM research_chains WHERE state = 'stopped' AND ended_at_step IN ('screen','optimise','walkforward');"
    ).fetchall():
        chain = dict(row)
        stage = chain["ended_at_step"]
        live, _ = _live_and_given_back_draws(conn, chain, stage)
        if len(live) != 1:
            continue
        link = conn.execute(
            "SELECT * FROM research_data_draws WHERE event_kind = 'run_linked' AND draw_id = ? "
            "  AND decision = 'granted' ORDER BY id DESC LIMIT 1;",
            (live[0]["id"],),
        ).fetchone()
        if link is None:
            continue
        run = conn.execute("SELECT status FROM research_runs WHERE id = ?;", (link["run_id"],)).fetchone()
        if run is not None and run["status"] in ("pending", "running"):
            out.append({"id": chain["id"], "run_id": link["run_id"]})
    return out


def chain_runner_universes_unused(conn) -> "list[dict]":
    """K-13: a chain universe (named 'chain %') that no research_runs.dataset_id names -- created
    (a build succeeded) but never read (the chain refused or failed before dispatching against
    it)."""
    rows = conn.execute(
        "SELECT id, name FROM research_datasets WHERE name LIKE 'chain %' "
        "  AND id NOT IN (SELECT dataset_id FROM research_runs WHERE dataset_id IS NOT NULL);"
    ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# The one compare-and-set write (spec section 4.3): every state change is one transaction, the
# UPDATE naming the state it expects and the transitions INSERT sharing that same transaction
# (section 8's own multi-object argument, CR-20's atomicity criterion).
# =============================================================================

def _write_transition(chain_id: int, from_state: str, to_state: str, actor: str, now: int, *,
                       reason_word: "str | None" = None, reason_detail: "str | None" = None,
                       ended_at_step: "str | None" = None, set_retry_requested: "int | None" = None) -> bool:
    """Writes the chain row's state UPDATE and its own transition row in ONE transaction. Returns
    True when this call actually won the compare-and-set (a concurrent STOP, pick or pass did not
    already move the row past from_state); False means nothing was written, and the caller should
    let the NEXT pass re-read rather than retry inside this one. Raises whatever either write
    itself raises (a CHECK violation on the transitions row rolls the chain row's own UPDATE back
    too, section 8's own atomicity, proven by CR-20)."""
    set_clauses = ["state = ?", "ended_at_step = ?", "reason_word = ?", "reason_detail = ?", "updated_at = ?"]
    params = [to_state, ended_at_step, reason_word, reason_detail, now]
    if set_retry_requested is not None:
        set_clauses.append("retry_requested = ?")
        params.append(set_retry_requested)
    sql = f"UPDATE research_chains SET {', '.join(set_clauses)} WHERE id = ? AND state = ?;"
    params += [chain_id, from_state]
    with researchdb.transaction() as conn:
        cur = conn.execute(sql, params)
        won = cur.rowcount > 0
        if won:
            conn.execute(
                "INSERT INTO research_chain_transitions "
                "    (chain_id, from_state, to_state, reason_word, detail, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (chain_id, from_state, to_state, reason_word, reason_detail, actor, now),
            )
    return won


def _clear_retry_flag(chain_id: int) -> None:
    """Section 1.2's own ordering rule: retry_requested is cleared BEFORE the replay route is
    called, in its own write, so a lost write (a process death between the clear and the call)
    can never turn one RETRY press into a re-queue loop (the chain-link spec's A2)."""
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_chains SET retry_requested = 0 WHERE id = ?;", (chain_id,))


def _end_chain(chain: dict, step: str, terminal_state: str, reason_word: str,
               reason_detail: "str | None", now: int) -> None:
    """The one place every 'refused' or 'failed' ending is written (section 8's detail rule): a
    source that stored no message, or an empty one, still yields a sentence naming the ids and
    saying so, never NULL and never an empty string."""
    if not reason_detail:
        reason_detail = f"{reason_word}: chain {chain['id']} ended at step {step!r} with no message recorded by its source"
    _write_transition(chain["id"], chain["state"], terminal_state, "runner", now,
                       reason_word=reason_word, reason_detail=reason_detail, ended_at_step=step)


def _advance_stage_forward(chain: dict, stage: str, now: int) -> None:
    _write_transition(chain["id"], chain["state"], _NEXT_STATE_FOR_STAGE[stage], "runner", now)


# =============================================================================
# The duel (spec section 7). Compared only at the point a stage run succeeds, against the
# partner's own run for the same stage IF one already exists; if the partner has not dispatched
# this stage yet there is nothing to compare and this chain proceeds normally, on its own.
# =============================================================================

def _run_dispatch_facts(conn, run_id: int) -> dict:
    """The three duel-comparable facts a run freezes at DISPATCH time (never at completion):
    its dataset_id (a sweep writes dataset_id = universe_id), its granted draw's own window, and
    its settings_snapshot (section 7 point 3's own frozen-settings argument)."""
    run = conn.execute("SELECT dataset_id, settings_snapshot FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
    draw = conn.execute(
        "SELECT dr.window_start_ts, dr.window_end_ts FROM research_data_draws dl "
        "JOIN research_data_draws dr ON dr.id = dl.draw_id AND dr.event_kind = 'draw_requested' "
        "WHERE dl.event_kind = 'run_linked' AND dl.run_id = ? AND dl.decision = 'granted' LIMIT 1;",
        (run_id,),
    ).fetchone()
    return {
        "dataset_id": run["dataset_id"] if run is not None else None,
        "settings_snapshot": run["settings_snapshot"] if run is not None else None,
        "window": (draw["window_start_ts"], draw["window_end_ts"]) if draw is not None else (None, None),
    }


def _handle_duel_check(chain: dict, stage: str, run_id: int, now: int) -> bool:
    """Section 7 point 4: when this chain's own stage run succeeds and its duel partner's run for
    the SAME stage already exists, compare dataset_id, granted window and settings_snapshot, in
    that order. Any difference moves BOTH chains to refused duel-inputs-differ in one transaction,
    naming the stage, both run ids and the first differing key. Returns True when a mismatch was
    found and written (the caller must stop, this chain already ended); False when there is
    nothing to compare yet, or the two runs agree (the caller proceeds to advance normally)."""
    with researchdb.get_conn() as conn:
        partner_row = conn.execute("SELECT * FROM research_chains WHERE id = ?;", (chain["duel_chain_id"],)).fetchone()
        if partner_row is None:
            return False
        partner = dict(partner_row)

        # Same-pass double-write guard: if the partner ALREADY ended refused duel-inputs-differ
        # at this exact stage (its own half of this SAME comparison already ran, inside this same
        # pass, before this chain was processed), end only THIS chain's own row rather than
        # touching the partner's a second time -- a second identical write would still be
        # state-consistent, but it would append a spurious duplicate transition row, which is
        # exactly the kind of extra, unread state DDIA's own event-log chapter (ch. 3/11) warns a
        # replayed or re-applied write can silently pile up.
        if partner["state"] == "refused" and partner["reason_word"] == "duel-inputs-differ" and partner["ended_at_step"] == stage:
            detail = (
                f"duel-inputs-differ: stage {stage!r} run {run_id} (chain {chain['id']}) matches its "
                f"partner's already-recorded mismatch against chain {partner['id']}"
            )
            _end_chain(chain, stage, "refused", "duel-inputs-differ", detail, now)
            return True

        partner_run_id = _find_stage_run(conn, partner["strategy_id"], partner["strategy_version"], chain["calendar_id"], stage)
        if partner_run_id is None:
            return False  # the partner has not dispatched this stage yet: nothing to compare
        mine = _run_dispatch_facts(conn, run_id)
        theirs = _run_dispatch_facts(conn, partner_run_id)

    differing_key = None
    for key in ("dataset_id", "window", "settings_snapshot"):
        if mine[key] != theirs[key]:
            differing_key = key
            break
    if differing_key is None:
        return False

    detail = (
        f"duel-inputs-differ: stage {stage!r} run {run_id} (chain {chain['id']}) vs "
        f"run {partner_run_id} (chain {partner['id']}) first differ at {differing_key}"
    )
    with researchdb.transaction() as conn:
        for row_id, prior_state in ((chain["id"], chain["state"]), (partner["id"], partner["state"])):
            cur = conn.execute(
                "UPDATE research_chains SET state = 'refused', ended_at_step = ?, reason_word = 'duel-inputs-differ', "
                "  reason_detail = ?, updated_at = ? WHERE id = ? AND state = ?;",
                (stage, detail, now, row_id, prior_state),
            )
            if cur.rowcount > 0:
                conn.execute(
                    "INSERT INTO research_chain_transitions "
                    "    (chain_id, from_state, to_state, reason_word, detail, actor, created_at) "
                    "VALUES (?, ?, 'refused', 'duel-inputs-differ', ?, 'runner', ?);",
                    (row_id, prior_state, detail, now),
                )
    return True


# =============================================================================
# The three stage steps: screen, optimise, walkforward (spec section 1.1, 4.1, 4.4).
# =============================================================================

def _attempt_stage_dispatch(chain: dict, stage: str, now: int) -> None:
    """"None means the step has not started, so start it" (section 4.1): build or adopt the
    stage's universe, re-verify the source has not drifted, run the quantile pre-check, then
    dispatch, in that order (section 1.1's own numbered list)."""
    start_key, end_key = _STAGE_WINDOW_KEYS[stage]
    window_start_ts, window_end_ts = chain[start_key], chain[end_key]

    # Step 1: build or adopt the stage's universe (section 2.3). `stage_runner.build_screen_universe`
    # is called through the MODULE attribute (never imported by name) so a test's own mock.patch of
    # it is honoured, exactly like this file's own call to `universe_rule.universe`.
    universe_name = f"chain {chain['calendar_id']} {stage} {_epoch_to_date(window_start_ts)} to {_epoch_to_date(window_end_ts)}"
    if stage == "walkforward":
        # DECISIONS #434: the walk-forward tests the chosen stock only. The ticker is in the name, so
        # an equal-window name collision below adopts a universe holding that same stock, never a
        # duel partner's different pick.
        universe_name = (f"chain {chain['calendar_id']} {stage} {chain['chosen_ticker']} "
                         f"{_epoch_to_date(window_start_ts)} to {_epoch_to_date(window_end_ts)}")
    try:
        try:
            if stage == "walkforward":
                universe_result = stage_runner.build_walkforward_universe(
                    universe_name, window_start_ts, window_end_ts, chain["calendar_id"], chain["chosen_ticker"])
            else:
                universe_result = stage_runner.build_screen_universe(universe_name, window_start_ts, window_end_ts, chain["calendar_id"])
        except universe_rule.HoldoutNotDrawnYet:
            # Section 2.3: "before the first universe of C, the screen step draws the held-out
            # names once, seed per decision D-6." This is the ONLY place that call runs: caught
            # here, around the real build_screen_universe call, so a test that mocks
            # build_screen_universe entirely (never raising this specific exception) never
            # touches the real daily store at all, and a real first dispatch under a fresh
            # calendar draws exactly once before its retry.
            store = stage_runner.daily_store_path()
            try:
                universe_rule.draw_held_out_names(chain["calendar_id"], store, universe_rule._DEFAULT_REFERENCE_PATH, 1, now)
            except sqlite3.IntegrityError:
                # A duel partner (sharing this SAME calendar) drew it first, in this same pass,
                # just before this chain was processed: the row now exists either way, so the
                # retry below succeeds off the partner's own draw.
                CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_adoptions"]["held_out"] += 1
            universe_result = stage_runner.build_screen_universe(universe_name, window_start_ts, window_end_ts, chain["calendar_id"])
        universe_id = universe_result["universe_id"]
    except researchdb.PriceUniverseNameExists as exc:
        with researchdb.get_conn() as conn:
            existing = conn.execute(
                "SELECT window_start_ts, window_end_ts FROM research_datasets WHERE id = ?;", (exc.existing_id,),
            ).fetchone()
        if existing is not None and existing["window_start_ts"] == window_start_ts and existing["window_end_ts"] == window_end_ts:
            # 4.4's A-rule universe: an equal-window name collision adopts the existing id.
            universe_id = exc.existing_id
            CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_adoptions"]["universe"] += 1
        else:
            _end_chain(chain, stage, "refused", "universe-name-taken", str(exc), now)
            return
    except Exception as exc:  # noqa: BLE001 -- DECISIONS #432: isolates one chain; ends it with the error recorded, logged in full when failed; every OTHER exception is classified below
        word, terminal = _classify_exception(exc)
        _end_chain(chain, stage, terminal, word, f"{type(exc).__name__}: {exc}", now)
        if terminal == "failed":
            log.exception("chain runner: universe build crashed for chain %s stage %s", chain["id"], stage)
        return

    # Step 2: re-verify the strategy's source has not drifted since generation (section 1.1 (2)).
    try:
        generated.assert_generation_record_matches(chain["strategy_id"])
    except generated.GeneratedSourceDrift as exc:
        _end_chain(chain, stage, "refused", "generated-source-drift", str(exc), now)
        return
    except Exception as exc:  # noqa: BLE001 -- DECISIONS #432: isolates one chain; ends it with the error recorded, logged in full when failed
        word, terminal = _classify_exception(exc)
        _end_chain(chain, stage, terminal, word, f"{type(exc).__name__}: {exc}", now)
        if terminal == "failed":
            log.exception("chain runner: generation check crashed for chain %s stage %s", chain["id"], stage)
        return

    # Step 3: the quantile pre-check over G (section 1.1 (3), F-4's own moved, shared function).
    param_grid = json.loads(chain["param_grid_json"])
    try:
        research_prechecks._refuse_bad_quantiles_in_grid(param_grid)
    except research_prechecks.QuantileOutOfRange as exc:
        _end_chain(chain, stage, "refused", "quantile-out-of-range", str(exc), now)
        return
    except Exception as exc:  # noqa: BLE001 -- DECISIONS #432: isolates one chain; ends it with the error recorded, logged in full when failed
        word, terminal = _classify_exception(exc)
        _end_chain(chain, stage, terminal, word, f"{type(exc).__name__}: {exc}", now)
        if terminal == "failed":
            log.exception("chain runner: quantile pre-check crashed for chain %s stage %s", chain["id"], stage)
        return

    # Step 4: stage_runner.dispatch_stage itself (section 1.1 (4)).
    try:
        stage_runner.dispatch_stage(chain["strategy_id"], universe_id, stage, chain["calendar_id"],
                                     param_grid, now, pinned_version=chain["strategy_version"])
    except tracker.ChronologyRefused as exc:
        # 4.4's A-rule chronology: tracker.py raises this ONLY for a genuine chronology refusal
        # (a lock-contended request instead raises a plain sqlite3.OperationalError, caught in its
        # own branch below), so the only question here is whether a concurrent pass's own draw
        # already won the race: if a live granted draw for the stage exists now, adopt it rather
        # than treating this call's own refusal as real; otherwise it IS real.
        with researchdb.get_conn() as conn:
            live_now, _ = _live_and_given_back_draws(conn, chain, stage)
        if live_now:
            CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_adoptions"]["chronology"] += 1
        else:
            _end_chain(chain, stage, "refused", "chronology-refused", str(exc), now)
        return
    except tracker.RunLinkFailed as exc:
        # 4.4's A-rule link: retry link_run ONCE at once; a second failure is the real refusal.
        try:
            tracker.link_run(exc.draw_id, exc.run_id, now)
            CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_adoptions"]["link"] += 1
        except tracker.RunLinkFailed as exc2:
            _end_chain(chain, stage, "refused", "run-link-failed",
                       f"draw {exc2.draw_id} / run {exc2.run_id}: {exc2}", now)
        return
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc) or "database table is locked" in str(exc):
            CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_lock_contention"] += 1
        else:
            _end_chain(chain, stage, "failed", "step-crashed", f"{type(exc).__name__}: {exc}", now)
            log.exception("chain runner: dispatch_stage raised an unrelated OperationalError for chain %s stage %s",
                          chain["id"], stage)
        return
    except Exception as exc:  # noqa: BLE001 -- DECISIONS #432: isolates one chain; ends it with the error recorded, logged in full when failed
        word, terminal = _classify_exception(exc)
        _end_chain(chain, stage, terminal, word, f"{type(exc).__name__}: {exc}", now)
        if terminal == "failed":
            log.exception("chain runner: dispatch_stage crashed for chain %s stage %s", chain["id"], stage)
        return
    # A clean dispatch leaves the chain in its own active state (screening/optimising/
    # walking-forward): the draw is granted and the run is pending/running, read and advanced on
    # a LATER pass once it actually succeeds.


def _process_stage_step(chain: dict, stage: str, now: int) -> None:
    # CN-R3/CN-P5 fix (round 4): DV's F-3 give-back predicate (tracker._GIVEN_BACK_PREDICATE)
    # marks a draw given-back the instant its linked run's status becomes 'failed', with nothing
    # stored -- that is BY DESIGN, so a RETRY (spec 4.5) can re-draw the same window. But taken
    # alone, "no live draw: start the step" then fires before "the run failed: end the chain" ever
    # gets a chance to run, because the given-back draw already vanished from the live set the
    # moment the run failed -- so the runner silently redrew and redispatched with no RETRY ever
    # called, forever, on any real sweep failure (found live in round 3's own cn22 trace, tickers
    # AEL then ANG, both "docker exited 1"). The fix (spec 4.1: "a granted link means read the
    # run: ... `failed` means `stage-run-failed`"; section 4.5: re-draws happen ONLY after an
    # accepted RETRY): read the step's facts over every granted draw requested SINCE this stage's
    # CURRENT incarnation began -- the chain's creation, or its latest accepted RETRY into this
    # step's own active state, whichever transition into that state is most recent. A given-back
    # draw inside that span is read exactly like a live one (its run decides the outcome, and
    # since the give-back predicate itself requires status='failed', that outcome is always
    # `stage-run-failed`); a given-back draw from BEFORE that span is an earlier, already-retried
    # attempt and is history, per the spec's own words. No retry cap, no new column, no second
    # copy of the give-back predicate: this reuses tracker.query_draws_given_back exactly as
    # before, only widening WHICH of its rows this function is willing to look at.
    with researchdb.get_conn() as conn:
        entry_row = conn.execute(
            "SELECT MAX(created_at) AS entry_ts FROM research_chain_transitions "
            "WHERE chain_id = ? AND to_state = ?;",
            (chain["id"], chain["state"]),
        ).fetchone()
        live, given_back = _live_and_given_back_draws(conn, chain, stage)
    # entry_ts falls back to the chain's own created_at (the spec's other named case, "the
    # chain's creation") when no transition row names this state as a to_state at all: every real
    # write path always writes one (_write_transition is the only place research_chains.state
    # changes), but a chain row that never passed through that write path -- created directly, the
    # shape every earlier fixture in this suite already uses to seed a chain straight into an
    # active state -- has none, and NULL must never be compared with >= (SQLite's own NULL
    # comparison rule would silently drop every row rather than error).
    entry_ts = entry_row["entry_ts"] if entry_row["entry_ts"] is not None else chain["created_at"]
    live_now = [d for d in live if d["created_at"] >= entry_ts]
    given_back_now = [d for d in given_back if d["created_at"] >= entry_ts]
    if len(live_now) > 1:
        _end_chain(chain, stage, "failed", "stage-draw-duplicated",
                   f"{len(live_now)} live granted draws exist for stage {stage!r} under calendar "
                   f"{chain['calendar_id']!r} since this incarnation began at {entry_ts}; expected at most one", now)
        return
    if not live_now and not given_back_now:
        _attempt_stage_dispatch(chain, stage, now)
        return

    # 4.4's A-rule draw: a live granted draw already there is adopted, never drawn again. Absent
    # a live one, the most recent given-back draw inside this incarnation is adopted instead (the
    # CN-P5 fix above): its run is always 'failed' (the give-back predicate's own requirement), so
    # this always ends the chain below rather than silently starting a fresh draw.
    CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_adoptions"]["draw"] += 1
    draw = live_now[0] if live_now else max(given_back_now, key=lambda d: d["id"])
    with researchdb.get_conn() as conn:
        link = conn.execute(
            "SELECT * FROM research_data_draws WHERE event_kind = 'run_linked' AND draw_id = ? ORDER BY id DESC LIMIT 1;",
            (draw["id"],),
        ).fetchone()
    if link is None:
        age = now - draw["created_at"]
        if age < tracker.UNLINKED_DRAW_STALE_AGE_SECONDS:
            return  # wait
        _end_chain(chain, stage, "failed", "stage-draw-orphaned",
                   f"draw {draw['id']} granted at {draw['created_at']} has no run_linked row after {age} seconds", now)
        return
    if link["decision"] == "refused":
        _end_chain(chain, stage, "failed", "stage-link-refused",
                   link["refusal_reason"] or f"draw {draw['id']}'s link (row {link['id']}) was refused", now)
        return

    run_id = link["run_id"]
    with researchdb.get_conn() as conn:
        run = conn.execute("SELECT * FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
    if run is None:
        _end_chain(chain, stage, "failed", "stage-run-deleted",
                   f"run {run_id} linked to draw {draw['id']} no longer exists", now)
        return
    status = run["status"]
    if status in ("pending", "running"):
        return  # wait
    if status == "succeeded":
        if chain["duel_chain_id"] is not None and _handle_duel_check(chain, stage, run_id, now):
            return  # both chains already ended refused duel-inputs-differ
        _advance_stage_forward(chain, stage, now)
        return
    if status == "failed":
        _end_chain(chain, stage, "failed", "stage-run-failed",
                   run["error"] or f"run {run_id} failed with no stored error message", now)
        return
    if status == "cancelled":
        _end_chain(chain, stage, "failed", "stage-run-cancelled", f"run {run_id} was cancelled", now)
        return
    _end_chain(chain, stage, "failed", "step-crashed", f"run {run_id} carries an unrecognised status {status!r}", now)


# =============================================================================
# The replay and judge steps (spec section 1.2, section 5's P-1).
# =============================================================================

def _resolve_formula_version_for_run(run_id: int) -> str:
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT strategy_version, build_provenance FROM research_runs WHERE id = ?;", (run_id,),
        ).fetchone()
    return judge_routes._resolve_formula_version(dict(row))


def _handle_replay_or_judge_outcome(chain: dict, step: str, exc: Exception, now: int) -> None:
    """Reads the word the route wrappers themselves read: `str(exc.detail).split(":", 1)[0]`
    (section 1.2). Every route refusal outside this call's own WAIT/FAIL/REFUSE vocabulary is
    route-answer-unworded (R-2): a word this build never anticipated for THIS step, never guessed
    at, never silently treated as success."""
    if not isinstance(exc, HTTPException):
        _end_chain(chain, step, "failed", "step-crashed", f"{type(exc).__name__}: {exc}", now)
        log.exception("chain runner: %s route call crashed for chain %s", step, chain["id"])
        return
    word = str(exc.detail).split(":", 1)[0]
    wait_words, fail_words, refuse_words = _P1_WORDS[step]
    if word in wait_words:
        return
    if word in fail_words:
        _end_chain(chain, step, "failed", word, str(exc.detail), now)
        return
    if word in refuse_words:
        _end_chain(chain, step, "refused", word, str(exc.detail), now)
        return
    _end_chain(chain, step, "refused", "route-answer-unworded", str(exc.detail), now)


def _process_replay_step(chain: dict, now: int) -> None:
    with researchdb.get_conn() as conn:
        optimise_run_id = _find_stage_run(conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "optimise")
    if optimise_run_id is None:
        _end_chain(chain, "replay", "failed", "step-crashed",
                   "the replaying step found no optimise run to key against", now)
        return
    v, t = chain["chosen_variation_id"], chain["chosen_ticker"]

    # Section 1.2 (1): a verifiable replay already at the key means PROCEED, read with the same
    # helper the route itself uses.
    cache = researchdb.get_full_metrics_cache_by_cell(optimise_run_id, v, t)
    if cache is not None and judge_routes._replay_unverifiable_reason(optimise_run_id, cache["payload"]) is None:
        _write_transition(chain["id"], "replaying", "walking-forward", "runner", now)
        return

    formula_version = _resolve_formula_version_for_run(optimise_run_id)
    # Section 1.2 (2): a compute job already in flight for this key means WAIT, no route call.
    if researchdb.compute_job_in_flight_for_key(optimise_run_id, v, t, formula_version) is not None:
        return
    # Section 1.2 (3): no worker online means WAIT, no route call.
    if not researchdb.any_worker_online():
        return

    # Section 1.2 (4): only now call replay_cell, clearing retry_requested in its own write
    # BEFORE the call (CN10's own ordering criterion).
    retry_flag = bool(chain["retry_requested"])
    if retry_flag:
        _clear_retry_flag(chain["id"])
    try:
        result = judge_routes.replay_cell(optimise_run_id, variation_id=v, ticker=t, retry=retry_flag)
    except Exception as exc:  # noqa: BLE001 -- DECISIONS #432; HTTPException is the expected shape, anything else ends the chain step-crashed, logged in full
        _handle_replay_or_judge_outcome(chain, "replay", exc, now)
        return

    if isinstance(result, JSONResponse):
        word = json.loads(bytes(result.body)).get("status")
    else:
        word = result.get("status")
    if word == "replay-exists":
        _write_transition(chain["id"], "replaying", "walking-forward", "runner", now)
    # replay-queued (or any other WAIT-shaped success) leaves the chain in 'replaying': nothing to write.


def _process_judge_step(chain: dict, now: int) -> None:
    # Section 1.1: the judge grades the replayed cell of the optimise run R, `judge_run(R, v, t)`, and
    # reads the walk-forward run through R for check 2 (routes/judge.py refuses a walk-forward run
    # outright, walkforward-run-not-gradable). The walk-forward run must still exist before grading.
    with researchdb.get_conn() as conn:
        wf_run_id = _find_stage_run(conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "walkforward")
        optimise_run_id = _find_stage_run(conn, chain["strategy_id"], chain["strategy_version"], chain["calendar_id"], "optimise")
    if wf_run_id is None:
        _end_chain(chain, "judge", "failed", "step-crashed",
                   "the judging step found no walkforward run for check 2 to read", now)
        return
    if optimise_run_id is None:
        _end_chain(chain, "judge", "failed", "step-crashed",
                   "the judging step found no optimise run to key the verdict against", now)
        return
    v, t = chain["chosen_variation_id"], chain["chosen_ticker"]

    # Section 1.1's own idempotent judge step: a row already at the exactly-once key (R, v, t, pass id)
    # means done, with no second call to judge_run.
    pass_id = judge_routes._judging_pass_id_for_cell(optimise_run_id, v, t, judgedb.BATTERY_VERSION)
    with researchdb.get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM research_judge_verdicts WHERE run_id = ? AND variation_id = ? AND ticker = ? "
            "  AND judging_pass_id = ?;",
            (optimise_run_id, v, t, pass_id),
        ).fetchone()
    if existing is not None:
        _write_transition(chain["id"], "judging", "done", "runner", now)
        return

    try:
        judge_routes.judge_run(optimise_run_id, variation_id=v, ticker=t)
    except Exception as exc:  # noqa: BLE001 -- DECISIONS #432: isolates one chain; ends it with the error recorded, logged in full when failed
        _handle_replay_or_judge_outcome(chain, "judge", exc, now)
        return
    _write_transition(chain["id"], "judging", "done", "runner", now)


# =============================================================================
# STOP, waiting, and the per-chain dispatch table (spec section 4.1, 4.2).
# =============================================================================

def _handle_stop(chain: dict, now: int) -> None:
    """Section 4.2: 'the next pass, before any action, moves the chain to stopped with the
    current step as ended_at_step.' Checked first, for every non-terminal state, before any
    stage-specific action runs (CR-12's own proof: no draw or job is written after STOP)."""
    step = _STEP_FOR_STATE[chain["state"]]
    _write_transition(chain["id"], chain["state"], "stopped", "runner", now, ended_at_step=step)


def _process_one_chain(chain: dict, now: int) -> None:
    if chain["stop_requested"]:
        _handle_stop(chain, now)
        return
    state = chain["state"]
    if state == "waiting":
        # Section 4.1: 'waiting' leaves when 'the next pass starts the screen step: screening.'
        # The actual dispatch attempt runs on the NEXT pass, once the chain is already
        # 'screening' (this is what lets a duel's two rows land in 'screening' together before
        # either one's own universe build is attempted, section 7 point 1).
        _write_transition(chain["id"], "waiting", "screening", "runner", now)
        return
    if state in _STAGE_FOR_ACTIVE_STATE:
        _process_stage_step(chain, _STAGE_FOR_ACTIVE_STATE[state], now)
        return
    if state == "choosing-cell":
        return  # waits for Tamer indefinitely (K-9)
    if state == "replaying":
        _process_replay_step(chain, now)
        return
    if state == "judging":
        _process_judge_step(chain, now)
        return
    # done/stopped/refused/failed never reach here: run_pass_once's own SELECT excludes them.


def run_pass_once(now: "int | None" = None) -> dict:
    """The entry point main.py's own background loop calls once per tick (via asyncio.to_thread,
    since every call here is synchronous SQLite work). Reads every chain not in a terminal state,
    derives each one's own next action from the stores, and acts. Nothing is held in memory
    between calls (spec section 10): a restart simply resumes here."""
    if now is None:
        now = int(time.time())
    pass_started = time.monotonic()
    CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_passes"] += 1
    CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_last_pass_at"] = now

    with researchdb.get_conn() as conn:
        placeholders = ", ".join("?" for _ in _NON_TERMINAL_STATES)
        rows = conn.execute(
            f"SELECT * FROM research_chains WHERE state IN ({placeholders}) ORDER BY id;",
            _NON_TERMINAL_STATES,
        ).fetchall()
        chains = [dict(r) for r in rows]

    processed = 0
    for chain in chains:
        try:
            _process_one_chain(chain, now)
        except Exception:  # noqa: BLE001 -- DECISIONS #432: isolates one chain; logged in full and counted (K-15)
            CHAIN_RUNNER_CRASH_COUNTERS["chain_pass_crashes"] += 1
            log.exception("chain runner: pass crashed processing chain %s", chain.get("id"))
        processed += 1

    CHAIN_RUNNER_MONITOR_COUNTERS["chain_runner_last_pass_seconds"] = time.monotonic() - pass_started
    return {"chains_processed": processed}
