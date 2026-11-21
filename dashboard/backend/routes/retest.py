"""Retest routes: dispatch a NautilusTrader retest for an already-graded run, and read back its
record. Lane NR (U.7s), docs/plans/night/2026-09-13_set8a_night/PLAN.md section 7.

Every route on `router` below carries no auth dependency, matching every other browser route in
this backend (dashboard/backend/CLAUDE.md: "Caddy basic auth handles humans at the edge"). The
worker's own write entry point, relay_retest_result, lives on a SEPARATE router, worker_router
(/api/research/worker/retests, set 16b lane WD, #423), gated behind require_worker_api_key at the
router level: findings round 1 (2026-09-13) found it unguarded, a BLOCKER (a write reachable with
no authorisation), fixed in the same round it was found; lane WD's own move puts it under the only
prefix production's Caddy passes without human basic-auth at all, which is what makes the worker's
own key check the ONLY gate the request ever meets, rather than a check behind a wall the worker
could never have passed.

Nothing here calls either of the judge store's own write entry points (AC-D2: this router only
READS the judge's current verdict through the same join routes/research.py's own
_verdicts_for_run_ids already uses, it never writes one), and routes/judge.py is not imported and
not touched by this file at all.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import artifact_store, researchdb, retestdb
from . import research as research_routes
from .research import require_worker_api_key

router = APIRouter(prefix="/api/research/retests", tags=["retest"])

# Set 16b, lane WD (#423): the worker's front door. The completion callback below used to live on
# `router` above (the browser-facing /api/research/retests prefix), reachable only through Caddy's
# human basic-auth wall in production -- the worker itself can never send basic-auth credentials,
# so this route only ever worked for a caller Caddy would already have let through some other way.
# It now lives on its own router, prefixed /api/research/worker/retests, the ONLY prefix
# production's Caddy passes without a human login (dashboard/deploy/Caddyfile's own
# @research_worker matcher), gated behind require_worker_api_key at the router level (the same
# shape routes/research.py's own worker_router already uses).
worker_router = APIRouter(
    prefix="/api/research/worker/retests", tags=["retest-worker"],
    dependencies=[Depends(require_worker_api_key)],
)

# The ops routing law (plan section 8's own "what can break"): the fills route reading an
# artifact the store lacks is a NAMED failure mode, so it carries a counter, module-level and
# in-process, the same shape routes/judge.py's own JUDGE_ROUTE_COUNTERS already uses. Registry
# row drafted in docs/units/NP/monitor_rows.md and pasted into docs/ops/ops_monitor_registry.md
# in this same build.
RETEST_ROUTE_MONITOR_COUNTERS = {
    "fills_reads_failed_artifact_missing": 0,
}


def _candidate_run_and_strategy_names(conn, run_ids):
    """One join for every candidate's own run label and strategy name (plan section 8 criterion
    2's "run, strategy" columns), never a per-run_id query: the same "one extra query, never N"
    discipline routes/research.py's own _verdicts_for_run_ids already uses for this router's list
    route. Returns {run_id: {"run_name": str|None, "strategy_name": str}}; a run_id this dict has
    no entry for (a run row deleted after its retest was recorded) is the caller's to treat as
    absent, never a KeyError."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"""
        SELECT r.id AS run_id, r.name AS run_name, s.name AS strategy_name
        FROM research_runs AS r
        JOIN research_strategies AS s ON s.id = r.strategy_id
        WHERE r.id IN ({placeholders});
        """,
        tuple(run_ids),
    ).fetchall()
    return {row["run_id"]: {"run_name": row["run_name"], "strategy_name": row["strategy_name"]}
            for row in rows}


def _newest_retest_fields_by_run_id(conn, run_ids):
    """The newest research_retests row's OWN other columns per run_id (engine_version,
    could_not_run_reason, both gap figures, the row's own id for the fills route), restated here
    rather than added to retestdb.latest_retest_states (which deliberately returns only the
    DERIVED state word): this lane may not edit retestdb.py (its own stop condition), and the list
    route (plan section 8 criterion 2) needs these raw columns beside the state word. Same
    "newest row per run_id" idiom retestdb.latest_retest_states already uses (a GROUP BY/MAX(id)
    subquery joined back onto the table), restated rather than imported so this file never reaches
    into that module's private query shape."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"""
        SELECT rt.run_id AS run_id, rt.id AS retest_id, rt.engine_version AS engine_version,
               rt.could_not_run_reason AS could_not_run_reason,
               rt.divergence_vs_shadow AS divergence_vs_shadow,
               rt.divergence_vs_shadow_absent_reason AS divergence_vs_shadow_absent_reason,
               rt.divergence_vs_vbt AS divergence_vs_vbt,
               rt.divergence_vs_vbt_absent_reason AS divergence_vs_vbt_absent_reason
        FROM research_retests AS rt
        JOIN (
            SELECT run_id, MAX(id) AS max_id FROM research_retests
            WHERE run_id IN ({placeholders}) GROUP BY run_id
        ) AS latest ON latest.run_id = rt.run_id AND latest.max_id = rt.id;
        """,
        tuple(run_ids),
    ).fetchall()
    return {row["run_id"]: dict(row) for row in rows}


def _last_retest_job_failure(conn):
    """Plan section 8 criterion 4: the newest FAILED run_retest job's own reason and finished
    time, read straight off research_jobs (which already carries both for every job kind; see
    routes/research.py's worker_complete, which writes status/finished_at/error on every
    completion). None when no run_retest job has ever failed, never a fake current time.

    'reason' is the worker's own leading token before its first colon (research_lab's
    worker/jobs/run_retest.py's own report_job_failed calls all write "<reason>: <detail>", e.g.
    "nautilus-image-absent: nautilus-runner:1.231.0 not present on this host (docker image inspect
    exited 1)"): this is what lets the page show NAUTILUS NOT INSTALLED HERE specifically for that
    one reason while still carrying every other failure's own words. A message with no colon at
    all (run_retest.py's one-shot "nautilus-runner container wrote no retest_result.json") is its
    own whole reason, since there is no separate detail half to drop."""
    row = conn.execute(
        "SELECT error, finished_at FROM research_jobs "
        "WHERE kind = 'run_retest' AND status = 'failed' "
        "ORDER BY id DESC LIMIT 1;"
    ).fetchone()
    if row is None:
        return None
    error_text = row["error"] or ""
    reason = error_text.split(":", 1)[0].strip() if error_text else "unknown-failure"
    return {"reason": reason, "at": row["finished_at"]}


def _current_verdict_word(conn, run_id: int):
    """None means "no current judge verdict", read the exact way routes/research.py's own
    RunExplorer list already reads it, so a retest is never dispatched on a candidate the judge
    page itself would show as ungraded (AC-D1)."""
    return research_routes._verdicts_for_run_ids(conn, [run_id]).get(run_id)


def _run_and_dataset_row(conn, run_id: int):
    """The one row this route needs to check timeframe and read settings_snapshot, joined once."""
    return conn.execute(
        "SELECT r.id AS run_id, r.settings_snapshot AS settings_snapshot, "
        "       d.timeframe AS timeframe "
        "FROM research_runs AS r "
        "JOIN research_datasets AS d ON d.id = r.dataset_id "
        "WHERE r.id = ?;",
        (run_id,),
    ).fetchone()


def _assemble_matched_configuration(settings_snapshot_json):
    """Build the fourteen-knob matched-configuration statement AC-C3/AC-C4 requires from the
    run's own settings_snapshot, or return None when that snapshot carries no configuration
    information at all (the fourth refusal, missing-configuration-statement).

    Knobs 1, 2 and 4 are ALWAYS structurally-unequal (AC-C4: fill timing, slippage unit and cash
    enforcement can never be matched between a vectorised sweep and a real order-by-order engine).
    Knob 12 (annualisation) is never assigned any disposition (AD6.5: no evidence today supports
    one). Knobs 6, 7 and 8 (same-bar conflict, long only, position sizing) are written out by hand
    in the replay strategy rather than configured, so they are by-construction. Every remaining
    knob is matched by the retest's own construction (the same bars, the same fee/slippage
    settings passed through unchanged) and reads set-equal. This is the SAME rule
    research_lab/probes/nautilus_retest/_lib.py's own complete_matched_configuration() encodes for
    the probe fixtures, restated here as the real dispatch-time logic rather than a test double.
    """
    try:
        settings = json.loads(settings_snapshot_json) if settings_snapshot_json else {}
    except (json.JSONDecodeError, TypeError):
        settings = {}
    if not settings:
        return None
    statement = {}
    for row in range(1, 15):
        if row == 12:
            continue
        if row in (1, 2, 4):
            disposition = "structurally-unequal"
        elif row in (6, 7, 8):
            disposition = "by-construction"
        else:
            disposition = "set-equal"
        statement[str(row)] = {"name": retestdb.KNOB_NAMES[row], "disposition": disposition}
    return statement


@router.post("/runs/{run_id}/retest", status_code=201)
def dispatch_retest(run_id: int):
    """Evaluate the four refusals in order (AC-D1, AC-E3, AC-B8, AC-C3), stopping at the first
    that fires and writing exactly one refusal row for it; otherwise write exactly one
    research_jobs row of kind run_retest, in the same transaction as nothing else (the retest
    record itself is written only at completion, by /result below)."""
    now = int(time.time())
    with researchdb.get_conn() as conn:
        run_row = conn.execute(
            "SELECT id FROM research_runs WHERE id = ?;", (run_id,)
        ).fetchone()
        if run_row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")

        verdict_word = _current_verdict_word(conn, run_id)
        if verdict_word is None:
            retestdb.record_refusal(
                run_id=run_id, reason=retestdb.REFUSAL_UNGRADED_CANDIDATE, created_at=now
            )
            raise HTTPException(
                status_code=409, detail=f"refused: {retestdb.REFUSAL_UNGRADED_CANDIDATE}"
            )

        joined = _run_and_dataset_row(conn, run_id)
        if joined is None or joined["timeframe"] != "1d":
            retestdb.record_refusal(
                run_id=run_id, reason=retestdb.REFUSAL_UNSUPPORTED_TIMEFRAME, created_at=now
            )
            raise HTTPException(
                status_code=409, detail=f"refused: {retestdb.REFUSAL_UNSUPPORTED_TIMEFRAME}"
            )

        if not artifact_store.store.exists(run_id, "price.parquet"):
            retestdb.record_refusal(
                run_id=run_id, reason=retestdb.REFUSAL_MISSING_BARS, created_at=now
            )
            raise HTTPException(
                status_code=409, detail=f"refused: {retestdb.REFUSAL_MISSING_BARS}"
            )

        matched_configuration = _assemble_matched_configuration(joined["settings_snapshot"])
        if matched_configuration is None:
            retestdb.record_refusal(
                run_id=run_id, reason=retestdb.REFUSAL_MISSING_CONFIGURATION_STATEMENT,
                created_at=now,
            )
            raise HTTPException(
                status_code=409,
                detail=f"refused: {retestdb.REFUSAL_MISSING_CONFIGURATION_STATEMENT}",
            )

    # All four refusals cleared: dispatch the job. A fresh transaction, separate from the reads
    # above, so the refusal-writing paths above each commit their own single row immediately
    # rather than holding a write lock across the whole read sequence.
    payload = {
        "run_id": run_id,
        "matched_configuration": matched_configuration,
        # Informational only at dispatch time: record_retest (criterion 5) re-reads the LIVE
        # residue_allowance from research_retest_parameters at write time, never this snapshot.
        "residue_allowance": None,
    }
    with researchdb.transaction() as conn:
        job_row = conn.execute(
            "INSERT INTO research_jobs (kind, status, payload_json, created_at) "
            "VALUES ('run_retest', 'pending', ?, ?) RETURNING id;",
            (json.dumps(payload), now),
        ).fetchone()
        job_id = int(job_row["id"])
    return {"job_id": job_id}


class RetestResultIn(BaseModel):
    """The worker's own completion POST (ENTRYPOINT_CONTRACT.md's result shape, plus the job's own
    matched_configuration echoed through unchanged from dispatch time, plus verdict_id). status is
    Pydantic-validated to the two words a caller may ask for; record_retest enforces the rest."""
    run_id: int
    verdict_id: "int | None" = None
    created_at: int
    shadow_copy_completed_at: "int | None" = None
    fills_read_at: "int | None" = None
    status: str
    could_not_run_reason: "str | None" = None
    engine_version: "str | None" = None
    python_version: "str | None" = None
    matched_configuration: dict
    # An artifact NAME (findings round 3, AC-28 BLOCKER), never a raw filesystem path: the worker
    # posts run_retest.py's own artifact-store name here, stored through record_retest into the
    # column of the same name (see that column's own comment in retestdb.py's DDL for how a
    # reader opens it back, artifact_store.store.open(run_id, export_path), never Path(export_path)).
    export_path: "str | None" = None
    divergence_vs_shadow: "float | None" = None
    divergence_vs_shadow_absent_reason: "str | None" = None
    divergence_vs_vbt: "float | None" = None
    divergence_vs_vbt_absent_reason: "str | None" = None
    # Lane NT2 (#452): final-equity ratio gap and Sharpe gap, from the entrypoint's figures. Absent from an older
    # worker's body (None), which never passes through the drift rule.
    gap_final_equity: "float | None" = None
    gap_sharpe: "float | None" = None


@worker_router.post("/{job_id}/result")
def relay_retest_result(job_id: int, body: RetestResultIn):
    """The result relay (plan section 7 criterion 5): the record is written ONCE, at completion,
    here, by calling retestdb.record_retest with the worker's own reported fields. job_id is
    carried for symmetry with every other worker-facing completion route in this backend and for
    a future audit trail; the row itself is keyed on run_id, matching every other S1-S4 field.

    Gated behind the SAME worker-key dependency every route under routes/research.py's
    worker_router already carries (findings round 1, BLOCKER: this write had no auth check at
    all), now applied at the router level (worker_router's own dependencies=) rather than a
    per-route decorator: a missing or wrong X-Worker-API-Key header is refused with 401/500 before
    this body is ever parsed, read or written to research_retests.

    Set 16b lane WD (#423) AC-3: moved from the browser-prefixed /api/research/retests router
    (unreachable by the worker through production's Caddy in the first place, which only passes
    /api/research/worker/*, /api/research/share/* and four PWA files past human basic-auth) to
    worker_router (/api/research/worker/retests); the old path no longer exists. AC-3 adds a check
    this route never had before: job_id must name an EXISTING run_retest job (404 by name
    otherwise) whose own dispatched run_id equals body.run_id (409 by name on a mismatch), checked
    BEFORE record_retest ever runs, so neither refusal writes a row."""
    with researchdb.get_conn() as conn:
        job_row = conn.execute(
            "SELECT id, kind, payload_json FROM research_jobs WHERE id = ?;", (job_id,)
        ).fetchone()
    if job_row is None or job_row["kind"] != "run_retest":
        raise HTTPException(status_code=404, detail="retest job not found")
    job_payload = json.loads(job_row["payload_json"])
    if job_payload.get("run_id") != body.run_id:
        raise HTTPException(
            status_code=409,
            detail="run_id does not match the run this retest job was dispatched for",
        )
    try:
        retest_id = retestdb.record_retest(
            run_id=body.run_id,
            verdict_id=body.verdict_id,
            created_at=body.created_at,
            shadow_copy_completed_at=body.shadow_copy_completed_at,
            fills_read_at=body.fills_read_at,
            status=body.status,
            could_not_run_reason=body.could_not_run_reason,
            engine_version=body.engine_version,
            python_version=body.python_version,
            matched_configuration=body.matched_configuration,
            export_path=body.export_path,
            divergence_vs_shadow=body.divergence_vs_shadow,
            divergence_vs_shadow_absent_reason=body.divergence_vs_shadow_absent_reason,
            divergence_vs_vbt=body.divergence_vs_vbt,
            divergence_vs_vbt_absent_reason=body.divergence_vs_vbt_absent_reason,
            gap_final_equity=body.gap_final_equity,
            gap_sharpe=body.gap_sharpe,
        )
    except (retestdb.InvalidRetestRecord, retestdb.InvalidMatchedConfiguration) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "retest_id": retest_id, "job_id": job_id}


@router.get("/runs/{run_id}")
def get_retest_detail(run_id: int):
    """The derived state word plus every record for this one candidate (AC-13)."""
    with researchdb.get_conn() as conn:
        state = retestdb.current_retest_state(conn, run_id)
        retests = [dict(r) for r in conn.execute(
            "SELECT * FROM research_retests WHERE run_id = ? ORDER BY id ASC;", (run_id,)
        ).fetchall()]
        refusals = [dict(r) for r in conn.execute(
            "SELECT * FROM research_retest_refusals WHERE run_id = ? ORDER BY id ASC;", (run_id,)
        ).fetchall()]
    return {"run_id": run_id, "state": state, "retests": retests, "refusals": refusals}


@router.get("")
def list_retests():
    """Every candidate with at least one row of either kind, plus the AC-J1 global counters
    (AC-13: 'never-run' rather than 0 on a zero-row store, exposed under their own function
    names so a page can label each one honestly).

    Findings round 3, SF2: one query for every candidate's own state (retestdb.latest_retest_states),
    never one current_retest_state() call per candidate (an N+1 query against research_retests,
    which grows without bound, one row per retest attempt ever run).

    Widened for the Nautilus page (plan section 8 criterion 2): each candidate now also carries
    its run's own label, its strategy's name, the newest retest row's own id (for the fills route
    below), engine version, could_not_run_reason and both gap figures with their absent reasons --
    everything the page's RETESTS table renders, in one call, never a per-candidate follow-up read.
    Widened again for criterion 4: last_retest_job_failure, a SECOND top-level field beside
    counters, the newest failed run_retest job's own reason and time (None when none)."""
    with researchdb.get_conn() as conn:
        run_ids = sorted(
            {r["run_id"] for r in
             conn.execute("SELECT DISTINCT run_id FROM research_retests;").fetchall()}
            | {r["run_id"] for r in
               conn.execute("SELECT DISTINCT run_id FROM research_retest_refusals;").fetchall()}
        )
        states = retestdb.latest_retest_states(conn, run_ids)
        names = _candidate_run_and_strategy_names(conn, run_ids)
        retest_fields = _newest_retest_fields_by_run_id(conn, run_ids)
        candidates = []
        for rid in run_ids:
            name_row = names.get(rid, {"run_name": None, "strategy_name": None})
            fields = retest_fields.get(rid)
            candidates.append({
                "run_id": rid,
                "run_name": name_row["run_name"],
                "strategy_name": name_row["strategy_name"],
                "state": states[rid],
                "retest_id": fields["retest_id"] if fields else None,
                "could_not_run_reason": fields["could_not_run_reason"] if fields else None,
                "engine_version": fields["engine_version"] if fields else None,
                "divergence_vs_shadow": fields["divergence_vs_shadow"] if fields else None,
                "divergence_vs_shadow_absent_reason":
                    fields["divergence_vs_shadow_absent_reason"] if fields else None,
                "divergence_vs_vbt": fields["divergence_vs_vbt"] if fields else None,
                "divergence_vs_vbt_absent_reason":
                    fields["divergence_vs_vbt_absent_reason"] if fields else None,
            })
        last_job_failure = _last_retest_job_failure(conn)
    return {
        "candidates": candidates,
        "counters": {
            "retests_by_status": retestdb.retests_by_status(),
            "could_not_run_retests_by_reason": retestdb.could_not_run_retests_by_reason(),
            "refused_retests_by_reason": retestdb.refused_retests_by_reason(),
            "view_rebuild_check": retestdb.view_rebuild_check(),
        },
        "last_retest_job_failure": last_job_failure,
    }


@router.get("/{retest_id}/fills")
def get_retest_fills(retest_id: int):
    """FILLS side by side and GAP PER FILL (plan section 8 criteria 4 and 5): the export's own
    rows, read through the REAL artifact store (artifact_store.store.open(run_id, export_path),
    never a raw filesystem path -- the AC-28 lesson retestdb.py's own DDL comment on export_path
    already carries, restated here at the read site) and cast to plain JSON-safe python types
    (never a numpy/pandas scalar FastAPI's own encoder cannot serialise, the same discipline
    dashboard/backend/research/preview.py's own reduce_ohlc already uses).

    404 by name (never a blank list, which would read exactly like a real zero-fill run) for the
    three absent shapes this route can hit: the retest id itself does not exist; the row exists
    but carries no export at all (a could-not-run row, or lifecycle criterion 11's own "a record
    with a NULL export" -- the page's own FILLS - NO EXPORT RECORDED state); and the row names an
    export the store no longer holds."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT run_id, export_path FROM research_retests WHERE id = ?;", (retest_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"retest {retest_id} not found")
    if row["export_path"] is None:
        raise HTTPException(status_code=404, detail="no export recorded for this retest")

    import pandas as pd  # server-side parquet read, matching research/preview.py's own convention

    try:
        with artifact_store.store.open(row["run_id"], row["export_path"]) as fh:
            frame = pd.read_parquet(fh)
    except artifact_store.ArtifactNotFound:
        # The named failure mode (plan section 8's "what can break"): the retest row points at an
        # export the artifact store no longer holds. Counted BEFORE the 404 is raised, so a caller
        # that never reads the response body still leaves a trace an operator can find later.
        RETEST_ROUTE_MONITOR_COUNTERS["fills_reads_failed_artifact_missing"] += 1
        raise HTTPException(status_code=404, detail="export artifact missing from the store")

    # Explicit python-type casts (never a bare numpy scalar): ENTRYPOINT_CONTRACT.md's own eight
    # columns, at minimum, on every row. vbt_fill_price/diff_vbt are the two columns the contract
    # allows to be legitimately absent (the "no next bar available" case), read as None rather
    # than NaN so the frontend's own null check (never a NaN-vs-number comparison) is exact.
    fills = [
        {
            "signal_date": int(r["signal_date"]),
            "side": str(r["side"]),
            "signal_bar_close": float(r["signal_bar_close"]),
            "real_fill_price": float(r["real_fill_price"]),
            "shadow_fill_price": float(r["shadow_fill_price"]),
            "vbt_fill_price": float(r["vbt_fill_price"]) if pd.notna(r["vbt_fill_price"]) else None,
            "diff_shadow": float(r["diff_shadow"]),
            "diff_vbt": float(r["diff_vbt"]) if pd.notna(r["diff_vbt"]) else None,
        }
        for _, r in frame.iterrows()
    ]
    return {"retest_id": retest_id, "run_id": row["run_id"], "fills": fills}
