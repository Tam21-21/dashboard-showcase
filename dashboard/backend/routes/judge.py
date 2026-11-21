"""Judge routes: the verdict store's browser-facing read side, PLUS (night four, lane J1, 6.1a)
the "JUDGE THIS RUN" write route.

Registered by main.py in the night-one pre-dispatch commit so lane J fills this file without
touching the route table. Lane J owns everything below the router line.

Night one (#372), lane J, docs/plans/night/2026-09-07/PLAN.md section 6. Governed by
docs/briefs/judge_architecture_2026-08-31.md sections 2, 2b (the candidate key and the CURRENT
row rule) and section 4 (the narrowed candidate-verdict read), and by dashboard/backend/judgedb.py.

CORRECTED, night four (this file's own earlier claim was wrong and is corrected here rather than
left in place): the two GET routes below still only ever READ the verdict store, but
`POST /runs/{run_id}/judge` (night four, lane J1, 6.1a) DOES call both `judgedb.compose_verdict`
and `judgedb.record_verdict`, the shared grading entry point this docstring used to say "a later
unit this build does not touch." That later unit is this one.

No auth dependency on this router, on purpose, matching every other browser route in this backend
(dashboard/backend/CLAUDE.md, "How work flows"): "That router carries no auth dependency by
design: Caddy basic auth handles humans at the edge."

Every query below opens its connection with judgedb.DB_PATH read AT CALL TIME, inside the route
function body, never captured once at import time. That is what lets a probe point
judgedb.DB_PATH at a scratch file before it makes a request and have this router actually read
that file (the plan's own words: "the path passed explicitly at each call").
"""
from __future__ import annotations

# hashlib: R1 (lane JC round 2, read 1)'s own real store round trip in _replay_unverifiable_reason
# below hashes the actual stored bytes against the declared sha256, rather than trusting the shape.
import hashlib
# json: the store keeps two columns as JSON text (check_outcomes_json, warnings_json); the browser
# wants plain lists, so this router parses them once here rather than shipping raw JSON-in-JSON
# text for the page to parse a second time.
import json
# math: used only by _sanitize_quantities below (math.isfinite), to tell an ordinary float apart
# from NaN or +/-infinity before that value is ever handed to json.dumps.
import math
import re
import time
import uuid
# datetime/timezone: the admission route's started_at column wants the same UTC "%Y-%m-%dT%H:%M:%SZ"
# form routes/paper.py's own _now_iso() writes, so a paper_runs row this route creates reads
# identically to one the paper node itself would write.
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
# BaseModel: the two POST bodies below (RejectBody, ApproveBody), matching every other typed POST
# body in this backend (routes/research.py's own CreateJobBody, for one).
from pydantic import BaseModel

# paperdb: the admission route's own target store (routes/judge.py may import paperdb the way
# routes/ops.py already does, set 5 day plan section 8 finding 2); a SECOND connection, paper.db,
# never a join across the two files (dashboard/backend/CLAUDE.md, "Nothing joins across the two
# research databases").
from .. import alpaca_paper_venue, artifact_store, catalog, judge_checks, judge_display, judgedb, paperdb, reservedb, researchdb, retestdb
from ..research import export
# require_worker_api_key: lane C5's own new worker route below (plan amendment 4) needs the SAME
# router-level worker-auth dependency retest.py and generator.py already import from research.py
# (dashboard/backend/CLAUDE.md, "How work flows": "The worker router does, on every route").
from .research import require_worker_api_key
# broadcaster: the one in-process pub/sub every live update goes through (sse.py); the admission
# route publishes paper.run_admitted so PP's live-stream handler can invalidate its queries the
# same way every other paper.* event already does.
from ..sse import broadcaster

router = APIRouter(prefix="/api/judge", tags=["judge"])

# Lane C5 (set 16c, #427, plan amendment 4): the container has no database (research_lab/CLAUDE.md,
# "How a job runs": "There is no database access anywhere in this area"), so the frozen
# cscv_split_count reaches it through this one worker-scoped route, matching generator.py's and
# retest.py's own worker_router shape exactly (prefix under /api/research/worker/, which is the
# ONLY prefix production's Caddy lets a non-human request through, dashboard/deploy/Caddyfile's
# own @research_worker matcher; scripts/probes/worker_front_door's static guard requires it).
worker_router = APIRouter(
    prefix="/api/research/worker/judge", tags=["judge-worker"],
    dependencies=[Depends(require_worker_api_key)],
)


@worker_router.get("/cscv-split-count")
def worker_cscv_split_count():
    """The worker's own read of check 5's frozen `cscv_split_count` (S), under the CURRENT
    `judgedb.BATTERY_VERSION`, so the sweep container can partition its shared bar index into S
    equal blocks while it still holds the data (AC-1). 503, never a guessed S, when the row is not
    frozen for this battery_version (a state that should not occur once #421 has run, but this
    route never assumes it): the worker's own caller (run_sweep.py) reads that as "S unavailable",
    skips writing the cscv artifact for this sweep, and check 5 later reads not-evaluable naming
    the missing artifact, exactly as it would for a sweep stored before this build (AC-5).
    """
    with researchdb.get_conn() as conn:
        row = judgedb.read_threshold(conn, 5, "cscv_split_count", judgedb.BATTERY_VERSION)
    if row is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "cscv-split-count-not-frozen: the judge's split count for check 5 is not set "
                "for the current battery"
            ),
        )
    return {"cscv_split_count": int(row["value_numeric"]), "battery_version": judgedb.BATTERY_VERSION}

# Amendment 1, A4 (read 1's R8, a BLOCKER-adjacent SHOULD-FIX): the constant that used to sit here
# (_INTERNAL_SERVER_ERROR_STATUS = 500) satisfied the TEXT of JC-3's own grep and not its own
# criterion -- the route could still ANSWER 500, just spelled through a name instead of a literal.
# JC-3 is now fully behavioural (no path through judge_run answers 500, whatever the source text
# holds), so the constant is deleted and the one call site below is rewritten to reuse row 10's own
# word: a reserve exam with no variation-0 cell is the SAME shape as "no cell at this key", so it
# answers 404 cell-not-found, naming the exam's own missing cell, never a bare server fault.

# The five-column candidate key architecture section 2b item 2 fixes, quoted there as staying
# "FIVE columns" even after two widenings: (strategy_id, strategy_version, variation_id, ticker,
# dataset_sha256). graded_formula_version is NOT part of it -- that column identifies which saved
# REPLAY a grading pass read, a different fact from which CANDIDATE it graded. Kept as one tuple so
# the verdicts list and the summary counts below can never drift onto different columns.
_CANDIDATE_KEY_COLUMNS = (
    "strategy_id", "strategy_version", "variation_id", "ticker", "dataset_sha256",
)

# "Is this row the CURRENT one" (architecture section 2b item 3, restated in SQL form at item 4):
# the greatest (graded_at, id) pair among the NON-ADVISORY rows sharing a candidate key. An
# advisory row is never current itself -- #214's advisory view must never be read as the
# candidate's general state (item 3's own reasoning). Written as a correlated NOT EXISTS, the same
# shape architecture section 2b item 4 gives the shortlist read verbatim, so this router tests the
# document's own query rather than a paraphrase of it.
_IS_CURRENT_EXPR = """(
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


def _table_present(conn) -> bool:
    """True when research_judge_verdicts exists in the file this connection opened.

    Checked with a direct query against sqlite_master rather than by pattern-matching the message
    of a failed SELECT: asking sqlite for the schema fact directly is a root-cause check, not a
    guess dressed up as one, and it can never be fooled by an unrelated OperationalError (a locked
    file, a disk error) that happens to mention the table's name in a different sentence.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'research_judge_verdicts'"
    ).fetchone()
    return row is not None


def _raise_store_absent(db_path) -> None:
    """The one 503 both routes below raise when the table has never been created (init_judge_db
    never ran against this file). The plan's own exact wording, so the page's STORE UNAVAILABLE
    state reads a stable, testable sentence rather than a string a probe has to guess at.
    """
    raise HTTPException(
        status_code=503,
        detail=(
            "the verdict store has not been created: research_judge_verdicts is missing "
            f"from {db_path}"
        ),
    )


# GV2-4b: a verdict whose run used a dataset with source 'synthetic' is hidden from the lists and
# counts unless the caller asks for it (CLAUDE.md: synthetic data is filtered out of every research
# view by default). A run with no dataset row is kept, as GV2-4a's run lists keep it.
_NOT_SYNTHETIC_VERDICT = """NOT EXISTS (
    SELECT 1 FROM research_runs AS sr JOIN research_datasets AS sd ON sd.id = sr.dataset_id
     WHERE sr.id = v.run_id AND sd.source = 'synthetic'
)"""


def _synthetic_where(include_synthetic: bool) -> str:
    """`WHERE <predicate> ` over the alias v, or '' when synthetic verdicts are asked for."""
    return "" if include_synthetic else f"WHERE {_NOT_SYNTHETIC_VERDICT} "


def _row_count(conn, include_synthetic: bool = True) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM research_judge_verdicts AS v {_synthetic_where(include_synthetic)}"
    ).fetchone()["n"]


def _serialise_row(row) -> dict:
    """One research_judge_verdicts row as the browser reads it: every column, the two JSON
    columns parsed into lists, the strategy's name joined in, "current" as this SELECT's own
    _IS_CURRENT_EXPR computed it, "trials" as its own subquery computed it (set 5 day, lane JP,
    plan section 8 criterion 4: "a count from research_run_cells for the run, exposed on the
    verdict row"), and the four run-stats figures survivor cards read (criterion 8): each an honest
    NULL, never a fake number, when research_run_stats has no row for this run yet (an event run,
    or a run stats has not been computed for).
    """
    strategy_name = row["strategy_name"]
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "judging_pass_id": row["judging_pass_id"],
        "variation_id": row["variation_id"],
        "ticker": row["ticker"],
        "graded_formula_version": row["graded_formula_version"],
        "strategy_id": row["strategy_id"],
        "strategy_version": row["strategy_version"],
        "dataset_sha256": row["dataset_sha256"],
        "verdict": row["verdict"],
        "is_advisory": bool(row["is_advisory"]),
        "battery_version": row["battery_version"],
        "evidence_floor_outcome": row["evidence_floor_outcome"],
        "check_outcomes": json.loads(row["check_outcomes_json"]),
        "decided_by": row["decided_by"],
        "warnings": json.loads(row["warnings_json"]),
        "graded_at": row["graded_at"],
        "current": bool(row["is_current"]),
        "trials": row["trials"],
        "total_return": row["total_return"],
        "max_drawdown": row["max_drawdown"],
        "num_trades": row["num_trades"],
        "sharpe_ratio": row["sharpe_ratio"],
        # A verdict row is denormalized with no foreign key (architecture section 2), so a
        # strategy row could in principle be gone while its verdict history survives. Falling back
        # to a plain, numbered placeholder here is honest and never crashes the list over one
        # orphaned row; it is not a guess at a name, it says exactly what it has left.
        "strategy_name": strategy_name if strategy_name is not None else f"strategy {row['strategy_id']}",
        # DBG-17: read from research_verdict_rejections in the list and detail SELECTs (UNIQUE
        # verdict_id, so the LEFT JOIN yields at most one rejection per row). rejected_at is null
        # when no rejection row exists; the survivors view drops rejected rows as awaiting_ok does.
        "rejected": bool(row["rejected"]),
        "rejected_at": row["rejected_at"],
    }


@router.get("/verdicts")
def list_verdicts(
    include_synthetic: bool = Query(default=False),
    # Pagination defaults are an operational dial, not a backtest parameter (#204: an inline
    # justification is enough here, parameter_provenance.md does not apply). 50 rows is one
    # screen's worth of the RECENTLY GRADED table; 500 is a generous ceiling so a caller cannot
    # accidentally pull the whole table's history in one request.
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """GET /api/judge/verdicts: the store's presence and row count, plus a page of verdict rows,
    newest grading first. Raises 503 (never an empty list) when the table itself does not exist.
    """
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        if not _table_present(conn):
            _raise_store_absent(judgedb.DB_PATH)
        rows = conn.execute(
            f"""
            SELECT v.*, s.name AS strategy_name, {_IS_CURRENT_EXPR} AS is_current,
                   (SELECT COUNT(*) FROM research_run_cells AS c
                     WHERE c.run_id = v.run_id AND c.row_scope = 'cell') AS trials,
                   rs.total_return, rs.max_drawdown, rs.num_trades, rs.sharpe_ratio,
                   (rvr.verdict_id IS NOT NULL) AS rejected, rvr.rejected_at AS rejected_at
              FROM research_judge_verdicts AS v
              LEFT JOIN research_strategies AS s ON s.id = v.strategy_id
              LEFT JOIN research_run_stats AS rs ON rs.run_id = v.run_id
              LEFT JOIN research_verdict_rejections AS rvr ON rvr.verdict_id = v.id
             {_synthetic_where(include_synthetic)}ORDER BY v.graded_at DESC, v.id DESC
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return {
            "store": {"table_present": True, "row_count": _row_count(conn, include_synthetic)},
            "rows": [_with_display(conn, _serialise_row(r)) for r in rows],
        }


@router.get("/summary")
def summary(include_synthetic: bool = Query(default=False)):
    """GET /api/judge/summary: the fact strip's FIVE numbers (set 5 day, lane JP, widened from
    four; UI plan 2.4.2's own strip: "214 SWEEPS GRADED · 3 SURVIVORS · 2 WAIT ON YOUR OK ·
    1 IN PAPER · 41 UNJUDGEABLE"). `graded`, `survivors` and `unjudgeable` are taken over the
    CURRENT, non-advisory row per candidate only, exactly as before (a candidate re-judged from
    pass to fail is counted once, under its latest verdict, never under both -- the reason
    "current" exists at all, architecture section 2b item 3). `failed` is dropped from the
    response: the strip shows five figures, not the prior four plus a fifth, and a failed verdict
    is visible on RECENTLY GRADED's own STOPPED BY column instead. `awaiting_ok` and `in_paper`
    are NEW: neither route this file exposed before this fold knew anything about paper.db, so
    both are computed here over a second connection to that store (paperdb.get_paper_conn), never
    joined across the two files. Raises the same 503 as /verdicts when the verdict table is absent.
    """
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        if not _table_present(conn):
            _raise_store_absent(judgedb.DB_PATH)
        counts = {"pass": 0, "fail": 0, "unjudgeable": 0}
        for r in conn.execute(
            f"""
            SELECT verdict, COUNT(*) AS n
              FROM (
                  SELECT v.verdict, {_IS_CURRENT_EXPR} AS is_current
                    FROM research_judge_verdicts AS v
                    {_synthetic_where(include_synthetic)}
              )
             WHERE is_current = 1
             GROUP BY verdict
            """
        ).fetchall():
            counts[r["verdict"]] = r["n"]
        store = {"table_present": True, "row_count": _row_count(conn, include_synthetic)}

        # Every CURRENT pass verdict's own (id, run_id) pair: the population awaiting_ok and
        # in_paper both partition (plan section 8 finding 2). Read on the SAME connection as the
        # counts above, so this summary is one consistent read of research.db even though the
        # paper.db half below opens separately.
        pass_rows = conn.execute(
            f"""
            SELECT v.id, v.run_id
              FROM research_judge_verdicts AS v
             WHERE v.verdict = 'pass' AND {_IS_CURRENT_EXPR}
               {"" if include_synthetic else "AND " + _NOT_SYNTHETIC_VERDICT}
            """
        ).fetchall()

    # research_verdict_rejections lives in research.db too (researchdb._DDL, the same file
    # judgedb.DB_PATH already points at), but it is researchdb.py's own table, written only by
    # researchdb.record_verdict_rejection, so it is read here through researchdb.get_conn rather
    # than reused off the judgedb connection above, matching how this file's own REJECT route
    # (below) writes it.
    rejected_verdict_ids: set[int] = set()
    if pass_rows:
        placeholders = ", ".join("?" for _ in pass_rows)
        with researchdb.get_conn() as rconn:
            rejected_verdict_ids = {
                row["verdict_id"]
                for row in rconn.execute(
                    f"SELECT verdict_id FROM research_verdict_rejections "
                    f"WHERE verdict_id IN ({placeholders});",
                    [r["id"] for r in pass_rows],
                ).fetchall()
            }

    # paper_runs.source_run_id: the admission route below (approve_verdict) is the only writer, and
    # it always sets source_run_id to the verdict's OWN run_id, so this set membership test is
    # exact, never a guess by name or by strategy.
    any_paper_run_ids: set[int] = set()
    running_paper_run_ids: set[int] = set()
    if pass_rows:
        placeholders = ", ".join("?" for _ in pass_rows)
        run_ids = [r["run_id"] for r in pass_rows]
        with paperdb.get_paper_conn(paperdb.DB_PATH) as pconn:
            for row in pconn.execute(
                f"SELECT source_run_id, ended_at FROM paper_runs "
                f"WHERE source_run_id IN ({placeholders});",
                run_ids,
            ).fetchall():
                any_paper_run_ids.add(row["source_run_id"])
                if row["ended_at"] is None:
                    running_paper_run_ids.add(row["source_run_id"])

    awaiting_ok = sum(
        1 for r in pass_rows
        if r["id"] not in rejected_verdict_ids and r["run_id"] not in any_paper_run_ids
    )
    in_paper = sum(1 for r in pass_rows if r["run_id"] in running_paper_run_ids)

    return {
        "store": store,
        "graded": counts["pass"] + counts["fail"] + counts["unjudgeable"],
        "survivors": counts["pass"],
        "awaiting_ok": awaiting_ok,
        "in_paper": in_paper,
        "unjudgeable": counts["unjudgeable"],
    }


# The PASS RATES hover's own source (plan section 8 criterion 3). One or more quantity_keys per
# check, the check's own FROZEN pass bar(s), read through judgedb.read_threshold -- an explicit
# mapping rather than a naming-convention guess, because check 9 alone gates on TWO arms (random
# entry and buy-and-hold, both frozen rows) and a suffix guess cannot tell that apart safely. Check
# 1 has NO row here on purpose: its own gate is Pardo's 30-trade caution plus his 90-percent
# degrees-of-freedom rule plus Taleb's kappa-above-0.15 threshold, composed inside
# judge_checks.check_1_evidence_floor itself (not a single stored bar this table can name), so its
# list below is always empty and the page's PASS BAR NOT SET reads for a different reason than an
# owed row -- both render the same words because neither has one number to show.
_PASS_BAR_QUANTITY_KEYS: dict[int, tuple[str, ...]] = {
    2: ("wfe_pass_bar",),
    3: ("dsr_pass_bar",),
    4: ("plateau_flatness_bar",),
    5: ("pbo_threshold",),
    6: ("mc_bound_pass_bar",),
    7: ("mcpt_pvalue_threshold",),
    8: ("prom_minus_biggest_run_pass_bar",),
    9: ("random_arm_pass_bar", "buy_hold_arm_pass_bar"),
}


@router.get("/thresholds")
def thresholds():
    """GET /api/judge/thresholds: one entry per check (1 to 9), each carrying the FROZEN bar(s)
    judgedb.read_threshold finds for it under the current BATTERY_VERSION -- never a default, never
    a substituted number (read_threshold's own docstring: "never a default, never a substituted
    number"). A check with an empty `bars` list (check 1 always; any other check whose bar is still
    `owed` in research_judge_thresholds) is PASS BAR NOT SET on the page; that word is the page's
    own composition, this route only reports what it found.
    """
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        checks: dict[str, dict] = {}
        for check_id in judgedb.BATTERY_CHECK_ROSTER:
            bars = []
            for quantity_key in _PASS_BAR_QUANTITY_KEYS.get(check_id, ()):
                row = judgedb.read_threshold(conn, check_id, quantity_key)
                if row is not None:
                    bars.append({
                        "quantity_key": quantity_key,
                        "value_text": row["value_text"],
                        "source_book": row["source_book"],
                        "source_folio": row["source_folio"],
                        "source_quote": row["source_quote"],
                    })
            checks[str(check_id)] = {"bars": bars}
    return {"battery_version": judgedb.BATTERY_VERSION, "checks": checks}


# =============================================================================
# POST /api/judge/runs/{run_id}/judge (night four, lane J1, 6.1a; set 5 night, lane JB, 6.1b).
# Grades all nine checks for real through `judge_checks`: checks 1, 3, 4, 8 (6.1a's own scope) and
# checks 2, 5, 6, 7, 9 (6.1b, this build). Every check id is now a REAL call into
# `dashboard.backend.judge_checks`, never a hardcoded literal: five of the nine (2, 5, 9 always,
# plus 6 and 7 whenever their own load-bearing input is absent) still normally read
# `not-evaluable`, by the same escalation rule the spec brief gives every absent input, but that
# reading now comes from the check's own computation against the run's own stored record, not
# from a literal this route used to echo (AC-J19; see this lane's own report,
# docs/units/JB/report_1.md, for which checks can return a real cleared/failed outcome today).
#
# THIS ROUTER now calls judgedb.compose_verdict and judgedb.record_verdict, unlike the two GET
# routes above: the module docstring at the top of this file, written by night one's lane J,
# said this router "never calls judgedb.record_verdict or judgedb.compose_verdict... a later unit
# this build does not touch." That later unit is this one; the sentence is now wrong and is
# named in the report as a map/docstring correction, not silently left in place.
# =============================================================================

# AC-J11 / the ops routing law: the route's own refusal counter, module-level, in-process,
# mirroring judgedb.JUDGE_MONITOR_COUNTERS's own shape (a dict of int counters, read live by a
# caller through JUDGE_ROUTE_COUNTERS[key]). monitor_rows.md's own judge_route_refusals row
# reads this dict; docs/units/J1/monitor_rows.md is where a reader is pointed at it (this route
# writes no operations-page row itself, the operations page being parked until October, #300).
JUDGE_ROUTE_COUNTERS = {
    "run_not_found_404_count": 0,
    "not_succeeded_409_count": 0,
    "duplicate_grade_409_count": 0,
}

# JUDGE_ROUTE_WORD_COUNTERS (Lane JC, section 7 row B-6) is defined further down this file, right
# after _W1_WORDS itself (moved there so the dict comprehension over _W1_WORDS runs after that
# tuple actually exists at module load time; a forward reference here raised NameError at import).


def _judging_pass_id_for(run_id: int, battery_version: str) -> str:
    """A DETERMINISTIC id for this route's own manual grading decision, keyed on run_id AND
    battery_version together.

    architecture section 2b item 1 calls this a "version-4 UUID text minted once ... carried
    unchanged into any retry of that same decision." A random per-call UUID4 cannot do that job
    for THIS route: two POSTs against the same run_id would each mint a different judging_pass_id
    and the exactly-once UNIQUE (run_id, variation_id, ticker, judging_pass_id) would never fire,
    which directly contradicts AC-J7's own requirement that a second call answers 409 with the
    first row's id. So this route deliberately uses uuid5 (deterministic, not random) over a
    fixed namespace and the run_id, trading "version 4" for "the same value on every retry",
    which is what this route's own exactly-once behaviour actually needs. Logged here rather than
    silently deviating from the architecture document's literal words.

    FIXED in the read_1 fold (J1-R1-SF1): the first draft seeded the uuid5 with run_id alone, so a
    run graded under battery_version "2026-09-09.a" would answer 409 forever, including under a
    future battery_version once check 2's WFE bar freezes (#193, "a changed bar mints a new
    version") -- exactly the state judgedb.JUDGE_MONITOR_COUNTERS's own stale_battery_version_count
    counter exists to name. Seeding with both means a battery-version change is a NEW grading
    decision (a new judging_pass_id, so a new row records under the new version) while a repeat
    POST under the SAME battery_version still resolves to the SAME id (still refused, still 409).
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"judge-route-manual-grade:{run_id}:{battery_version}"))


def _judging_pass_id_for_cell(run_id: int, variation_id: int, ticker: str, battery_version: str) -> str:
    """Lane JC (candidate chain link spec, section 3): the cell-grade sibling of
    _judging_pass_id_for above, keyed on the replayed cell itself so grading two different cells
    of the same optimise run are two distinct decisions (one row each), while a repeat grade of
    the SAME cell under the SAME battery_version still resolves to the same id and is refused
    (AC-J7's own exactly-once shape, extended to the cell grain). The single-run/exam path keeps
    using _judging_pass_id_for unchanged, as the spec's own words require."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"judge-route-cell-grade:{run_id}:{variation_id}:{ticker}:{battery_version}"))


def _bar_returns_from_equity(equity_df) -> "list[float] | None":
    """Bar-level RETURNS from an equity.parquet DataFrame (`timestamp`, `equity` columns,
    `research/export.py`'s own `_equity_pairs` shape): the percent change between each pair of
    consecutive equity values, which is the candidate's own observed bar-level return series
    checks 6, 7 and 9 read (judge_spec_brief.md architecture document section 3). None when there
    are fewer than 2 equity points (a single point has no bar-to-bar change to compute) or when
    every computed change is skipped, so a check reads this exactly like a missing series rather
    than crashing on an empty list.
    """
    values = equity_df["equity"].tolist()
    if len(values) < 2:
        return None
    returns = []
    for previous, current in zip(values, values[1:]):
        if previous == 0:
            continue  # a zero equity base has no meaningful percent return; skip rather than divide by zero
        returns.append((current - previous) / previous)
    return returns if returns else None


def _dated_bar_returns_from_equity(equity_df) -> "tuple[list, list] | tuple[None, None]":
    """Lane C9 (set 16b, `#421`/`#423`, `docs/units/C9/criteria.md`, F4, the lead's meaning review
    round 1 finding 4; AMENDED for `#426`, Tamer's ruling on the lead's read 1 of `a07d3be4`): each
    bar's own return, paired with its own bar DATE (the `timestamp` column, `_write_replay_series`'s
    own shape, unix seconds), because check 9's own Sharpe margin needs the candidate's and the
    buy-and-hold arm's daily return series paired by DATE, never by list position ("overlapping
    days" means a real date intersection). A SEPARATE function from `_bar_returns_from_equity`,
    never that one widened, so checks 6/7/8's own UNDATED PERCENT-CHANGE callers stay byte-unchanged.

    `#426`: Masters, *Testing and Tuning Market Trading Systems*, page 181, `docs/units/C9/book_reads.md`
    now also quotes -- "The price array must actually be the log of the real prices to make them
    scale independent" -- so check 9's OWN return series (this function's own output, never
    `_bar_returns_from_equity`'s) is the NATURAL-LOG bar-to-bar change of the stored equity curve,
    `ln(e_i / e_(i-1))`, not a simple percent change. A non-positive equity value has no real-valued
    log change, so that bar is SKIPPED BY NAME (`previous <= 0`), never a crash and never a fabricated
    number, the same no-crash convention the zero-base skip in `_bar_returns_from_equity` already
    follows for its own, different formula.

    Returns `(None, None)`, the same no-crash convention `_bar_returns_from_equity` already uses,
    when there are fewer than 2 equity points OR the frame carries no `timestamp` column at all:
    `judge_battery_1`'s own pre-existing `ac_j8` fixture parquet uses `ts`, never `timestamp`
    (nothing before this lane ever needed a dated series on this path), so this must tolerate that
    column's absence rather than raise `KeyError` (AC-7: that suite stays green).
    """
    if "timestamp" not in equity_df.columns:
        return None, None
    values = equity_df["equity"].tolist()
    timestamps = equity_df["timestamp"].tolist()
    if len(values) < 2:
        return None, None
    returns = []
    dates = []
    for i in range(1, len(values)):
        previous, current = values[i - 1], values[i]
        if previous <= 0:
            continue  # no real-valued natural log of a non-positive base: skip by name, never crash
        returns.append(math.log(current / previous))
        dates.append(int(timestamps[i]))
    return (returns, dates) if returns else (None, None)


def _read_cscv_artifact_for_run(run_id: int) -> "tuple[dict | None, int | None, bool]":
    """AC-5/AC-6: check 5's own artifact reader, the ONE place that resolves the cscv block-sums
    file for a run (never a per-cell name, AC-6: "keyed by run_id ALONE"), matching the
    `buy_hold_artifact_missing` idiom's own routes/judge.py-level counter site exactly.

    Returns `(block_sums, bars_dropped, artifact_missing)`. On a real miss (the artifact was never
    uploaded for this run, a sweep stored before this build or one whose worker upload failed):
    `(None, None, True)`, and `judge_checks.CHECK5_CSCV_MONITOR_COUNTERS['cscv_artifact_missing']`
    moves by exactly 1 (this is the ONLY place that counter increments; `check_5_cscv_pbo` itself
    never touches it, since only this reader knows a real store lookup actually missed).
    """
    try:
        df = export._read_parquet(run_id, judge_checks.CSCV_ARTIFACT_FILENAME)
    except artifact_store.ArtifactNotFound:
        judge_checks.CHECK5_CSCV_MONITOR_COUNTERS["cscv_artifact_missing"] += 1
        return None, None, True
    block_sums, bars_dropped = judge_checks.parse_cscv_artifact(df)
    return block_sums, bars_dropped, False


def _cscv_over_cap_marker(run_id: int) -> bool:
    """AC-8: whether the worker uploaded the over-cap MARKER for this run in place of the real
    cscv artifact (`judge_checks.CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME`'s own docstring explains
    why a separate file, rather than a flag on the real one, is what carries this distinction).
    Checked by the caller BEFORE `_read_cscv_artifact_for_run`, never inside it, so the ordinary
    missing-artifact counter is never charged for a run the worker deliberately skipped. Uses the
    store's own `exists()` (never `export._read_parquet`): the marker's bytes carry no parquet
    content at all, only its NAME is the signal, so parsing it would raise, not read cleanly.
    """
    return artifact_store.store.exists(run_id, judge_checks.CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME)


def _sanitize_quantities(value):
    """Lane QR (`6.1f`, set 6 day plan section 7 criterion 1): make one check's own `quantities`
    value safe to store as JSON text. A plain float that is NaN or +/-infinity has no JSON
    representation (the JSON standard admits only finite numbers), so it is replaced with `None`,
    which `json.dumps` writes as the literal `null`; every other value (an int, a string, a bool,
    an ordinary finite float, or -- defensively, since no check today nests one, but a future
    check might -- a nested dict or list) is walked recursively and returned unchanged. Applied to
    each check's own `quantities` dict BEFORE it is written into the `check_outcomes` entry below,
    so a check's real, book-derived arithmetic (Check 6's Monte Carlo bound, for one) can produce
    a non-finite number without that number ever reaching the `check_outcomes_json` column, whose
    own `CHECK (json_valid(check_outcomes_json))` constraint (judgedb.py) would otherwise refuse
    the INSERT outright once `allow_nan=False` is passed below (a raw NaN token is not valid JSON
    text, even though Python's own `json.loads` tolerates reading it back).
    """
    if isinstance(value, float):
        # math.isfinite is False for both NaN and +/-inf, True for every ordinary float; an int
        # or bool never reaches this branch since isinstance(value, float) is False for both.
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _sanitize_quantities(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_sanitize_quantities(inner) for inner in value]
    return value


# =============================================================================
# Lane JC (candidate chain link spec, docs/briefs/candidate_chain_link_spec_2026-09-23.md):
# L-1 (section 4), the admission table shared by both judge routes, the section 2 chain
# resolution, the replay-verifiability read (row 12), and the replay route itself (section 1.4).
# =============================================================================

# W-1: every route word both judge routes can answer. Each is written as a literal EXACTLY ONCE,
# right here (JC-17's own requirement), as its own named constant; every raise/return/compare site
# below references the constant rather than retyping the string, so "defined exactly once" holds
# by construction rather than by discipline (a second hand-typed copy would silently drift).
WORD_CELL_KEY_INCOMPLETE = "cell-key-incomplete"
WORD_SINGLE_RUN_NOT_A_REPLAY = "single-run-not-a-replay"
WORD_SWEEP_NOT_OPTIMISE = "sweep-not-optimise"
WORD_WALKFORWARD_RUN_NOT_GRADABLE = "walkforward-run-not-gradable"
WORD_SOURCE_LINK_REFUSED = "source-link-refused"
WORD_SWEEP_NEEDS_A_CELL = "sweep-needs-a-cell"
WORD_CELL_NOT_FOUND = "cell-not-found"
WORD_REPLAY_ABSENT = "replay-absent"
WORD_REPLAY_UNVERIFIABLE = "replay-unverifiable"
WORD_WALKFORWARD_RUN_IN_FLIGHT = "walkforward-run-in-flight"
WORD_COMPUTE_IN_FLIGHT = "compute-in-flight"
WORD_WORKER_OFFLINE = "worker-offline"
WORD_REPLAY_SOURCE_INCOMPLETE = "replay-source-incomplete"
WORD_REPLAY_QUEUED = "replay-queued"
WORD_REPLAY_EXISTS = "replay-exists"
# Amendment 1: two new W-1 words. WORD_REPLAY_FAILED (A2, outcome 3's "any other failure" branch);
# WORD_REPLAY_MODE_UNKNOWN (A1, outcome 4). W-3's own replay-not-reproduced is counted there, not
# restated here, per Amendment 1's own W-3 accounting rule.
WORD_REPLAY_FAILED = "replay-failed"
WORD_REPLAY_MODE_UNKNOWN = "replay-mode-unknown"

_W1_WORDS = (
    WORD_CELL_KEY_INCOMPLETE, WORD_SINGLE_RUN_NOT_A_REPLAY, WORD_SWEEP_NOT_OPTIMISE,
    WORD_WALKFORWARD_RUN_NOT_GRADABLE, WORD_SOURCE_LINK_REFUSED, WORD_SWEEP_NEEDS_A_CELL,
    WORD_CELL_NOT_FOUND, WORD_REPLAY_ABSENT, WORD_REPLAY_UNVERIFIABLE,
    WORD_WALKFORWARD_RUN_IN_FLIGHT, WORD_COMPUTE_IN_FLIGHT, WORD_WORKER_OFFLINE,
    WORD_REPLAY_SOURCE_INCOMPLETE, WORD_REPLAY_QUEUED, WORD_REPLAY_EXISTS,
    WORD_REPLAY_FAILED, WORD_REPLAY_MODE_UNKNOWN,
)

# Lane JC (section 7 row B-6): the admission table's own per-word refusal dictionary, an ADDITION
# beside JUDGE_ROUTE_COUNTERS above (never replacing it). Every W-1 word is pre-declared at 0 so an
# absent key is never mistaken for "not checked". Defined here, after _W1_WORDS, not at that dict's
# old home near JUDGE_ROUTE_COUNTERS, because a dict comprehension over _W1_WORDS needs the tuple to
# already exist at module load time.
JUDGE_ROUTE_WORD_COUNTERS = {word: 0 for word in _W1_WORDS}


def _resolve_formula_version(run: dict) -> str:
    """The one place both judge routes compute a run's own graded_formula_version: never client-
    supplied, always resolve_formula_version(strategy_version, build_provenance), lazily imported
    the same way the pre-existing grade route already did (catalog._ensure_strategies_on_path()
    puts research_lab/ on sys.path, idempotent)."""
    catalog._ensure_strategies_on_path()
    from strategies.helpers.full_metrics import resolve_formula_version  # noqa: PLC0415

    return resolve_formula_version(run["strategy_version"], run["build_provenance"] or "")


def _resolve_admission(run_id: int, variation_id: "int | None", ticker: "str | None", conn, *,
                       for_replay: bool):
    """L-1 (spec section 4), rows 1 to 10, shared by the grade route and the replay route. Raises
    HTTPException for every refusal row (the word first in `detail`, then a colon, then plain
    words, section 5's own idiom); returns (run, resolved_variation_id, resolved_ticker,
    is_reserve_exam) on admission. `is_reserve_exam` is only ever True for the grade route (row 7,
    "graded exactly as today"): `for_replay=True` turns that same row into row 9's own
    sweep-needs-a-cell refusal instead (spec section 4's own "one difference inside rows 1 to 10").
    """
    # Row 1: no such run.
    run = researchdb.get_run(run_id)
    if run is None:
        JUDGE_ROUTE_COUNTERS["run_not_found_404_count"] += 1
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    # Row 2: status not succeeded.
    if run["status"] != "succeeded":
        JUDGE_ROUTE_COUNTERS["not_succeeded_409_count"] += 1
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} has status {run['status']!r}; the judge grades a saved replay "
                    f"of a succeeded run only",
        )
    # Row 3: exactly one of variation_id/ticker given.
    cell_key_given = variation_id is not None and ticker is not None
    if (variation_id is None) != (ticker is None):
        raise HTTPException(
            status_code=422,
            detail=f"{WORD_CELL_KEY_INCOMPLETE}: name both variation_id and ticker together, or neither",
        )
    # Row 4: not a sweep (a single price or event run).
    nature = researchdb.run_nature(run_id)
    if nature != "sweep":
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SINGLE_RUN_NOT_A_REPLAY}: grade a replayed cell of the candidate's optimise run",
        )

    # Rows 5 to 9: R's own run_linked row, joined to its own draw_requested row for the stage
    # (the same join research/export.py's run_is_walkforward already uses, spec section 2).
    # Amendment 1, A5: draw_id is pulled through so a MORE-THAN-ONE link count can name each one.
    links = conn.execute(
        "SELECT rl.decision AS decision, rl.refusal_reason AS refusal_reason, dr.stage AS stage, "
        "       rl.draw_id AS draw_id "
        "FROM research_data_draws rl "
        "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
        "WHERE rl.event_kind = 'run_linked' AND rl.run_id = ?;",
        (run_id,),
    ).fetchall()
    if len(links) == 0:
        # Row 5: no run_linked row at all -> never a candidate's optimise run.
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SWEEP_NOT_OPTIMISE}: run {run_id} carries 0 run_linked rows; not a candidate's "
                    f"optimise run",
        )
    if len(links) > 1:
        # Amendment 1, A5 (read 1's R4): more than one link falls through ALL of rows 5-7a to row 8,
        # answering source-link-refused WHATEVER the stages, naming every link's own draw id and
        # stage (never "not exactly one" alone, which hid which links and where).
        named = ", ".join(f"draw {l['draw_id']} at stage {l['stage']!r}" for l in links)
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SOURCE_LINK_REFUSED}: run {run_id} carries {len(links)} run_linked rows "
                    f"({named}); a candidate's optimise run carries exactly one",
        )
    link = links[0]
    stage = link["stage"]

    if stage == "screen":
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SWEEP_NOT_OPTIMISE}: run {run_id}'s draw stage is 'screen'; not a candidate's "
                    f"optimise run",
        )
    if stage == "walkforward":
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_WALKFORWARD_RUN_NOT_GRADABLE}: the judge grades a replayed cell of this "
                    "candidate's optimise run and reads this run through it for check 2",
        )
    if stage == "reserve":
        # Row 7 (grade route, no cell key): the exam path, unobstructed. Row 7a (a cell key given
        # on a reserve run): refused the same as any other non-optimise stage. The replay route
        # always needs a cell (spec's own "one difference"): row 9's own word fires instead of the
        # exam path, whether or not a cell key happens to be given.
        if for_replay:
            raise HTTPException(status_code=422, detail=f"{WORD_SWEEP_NEEDS_A_CELL}: name a combination and a ticker")
        if not cell_key_given:
            return run, None, None, True  # row 7
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SWEEP_NOT_OPTIMISE}: run {run_id}'s draw stage is 'reserve'; not a candidate's "
                    f"optimise run",
        )
    if stage != "optimise":
        # The DDL's own CHECK admits only screen/optimise/walkforward/reserve/NULL, so this is
        # unreachable in practice; refused the same conservative way as an ambiguous link count.
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SWEEP_NOT_OPTIMISE}: run {run_id}'s draw stage is {stage!r}; not a candidate's "
                    f"optimise run",
        )

    # stage == 'optimise' from here.
    if link["decision"] != "granted":
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_SOURCE_LINK_REFUSED}: run {run_id}'s own optimise link is "
                    f"{link['decision']!r} ({link['refusal_reason']})",
        )
    if not cell_key_given:
        raise HTTPException(status_code=422, detail=f"{WORD_SWEEP_NEEDS_A_CELL}: name a combination and a ticker")

    # Row 10: the cell row itself.
    cell = researchdb.get_run_cell(run_id, variation_id, ticker)
    if cell is None:
        raise HTTPException(
            status_code=404,
            detail=f"{WORD_CELL_NOT_FOUND}: no cell at (run_id={run_id}, variation_id={variation_id}, "
                    f"ticker={ticker!r})",
        )
    return run, variation_id, ticker, False


_MCPT_ARTIFACT_COLUMNS = ("k", "m", "p_value", "original_performance", "seed", "statistic")


def _read_mcpt_artifact(run_id: int, name: str) -> dict:
    """Lane P4: the one-row parquet written by research_lab/judge_arms/mcpt_bars.py write_mcpt_artifact, as a
    dict for check 7. A file that is gone, or not one row with the six columns, refuses the grade by name
    (replay-unverifiable naming artifacts.mcpt); it is never read as 'no re-run'."""
    try:
        frame = export._read_parquet(run_id, name)
    except artifact_store.ArtifactNotFound as err:
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_UNVERIFIABLE}: artifacts.mcpt ({name!r}) is absent from the store") from err
    missing = [c for c in _MCPT_ARTIFACT_COLUMNS if c not in frame.columns]
    if missing or len(frame) != 1:
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_REPLAY_UNVERIFIABLE}: artifacts.mcpt ({name!r}) must be one row with columns "
                   f"{list(_MCPT_ARTIFACT_COLUMNS)}; found {len(frame)} rows, missing {missing}")
    row = frame.iloc[0]
    return {"k": int(row["k"]), "m": int(row["m"]), "p_value": float(row["p_value"]),
            "original_performance": float(row["original_performance"]), "seed": int(row["seed"]),
            "statistic": str(row["statistic"])}


def _check7_rerun_inputs(run_id: int, payload: dict) -> tuple:
    """Lane GV3-J7: check 7's two inputs from a stored payload ({"provenance", "replay"}), as (rerun_result, rerun_error).

    A provenance that is not a non-empty dict: 409 replay-unverifiable naming provenance. artifacts.mcpt only: (the file's row as a
    dict, None). mcpt_error only: (None, the text). Both, or an mcpt_error key whose value is not a non-empty string: 409 naming them.
    Neither: a theory strategy's payload (provenance.engine "research_lab.theory_stage") made before check 7 re-ran theory strategies
    gets (None, the text that says so), so check 7 is not evaluable and never the bar-return shuffle (#451); any other payload gets
    (None, None), check 7 as before."""
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_UNVERIFIABLE}: provenance must be a non-empty object, got {provenance!r}")
    replay = payload.get("replay") or {}
    entry = (replay.get("artifacts") or {}).get("mcpt")
    error = replay.get("mcpt_error")
    if "mcpt_error" in replay and (not isinstance(error, str) or not error):
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_UNVERIFIABLE}: mcpt_error must be a non-empty string, got {error!r}")
    if entry is not None and error is not None:
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_UNVERIFIABLE}: the replay carries both artifacts.mcpt and mcpt_error; it must carry one")
    if entry is not None:
        return _read_mcpt_artifact(run_id, entry["name"]), None
    if error is not None:
        return None, error
    if provenance.get("engine") == "research_lab.theory_stage":
        return None, "made before check 7 re-ran theory strategies (GV3-J7); re-run its optimise step"
    return None, None


_RANDOM_ARM_ARTIFACT_COLUMNS = ("k", "m", "p_value", "original_statistic", "seed")


def _read_random_arm_artifact(run_id: int, name: str) -> dict:
    """Lane P5: the one-row parquet written by research_lab/judge_arms/random_arm.py write_random_arm_artifact, as a
    dict for check 9. A file that is gone, or not one row with the five columns, refuses the grade by name
    (replay-unverifiable naming artifacts.random_arm); it is never read as 'no random arm'."""
    try:
        frame = export._read_parquet(run_id, name)
    except artifact_store.ArtifactNotFound as err:
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_UNVERIFIABLE}: artifacts.random_arm ({name!r}) is absent from the store") from err
    missing = [c for c in _RANDOM_ARM_ARTIFACT_COLUMNS if c not in frame.columns]
    if missing or len(frame) != 1:
        raise HTTPException(
            status_code=409,
            detail=f"{WORD_REPLAY_UNVERIFIABLE}: artifacts.random_arm ({name!r}) must be one row with columns "
                   f"{list(_RANDOM_ARM_ARTIFACT_COLUMNS)}; found {len(frame)} rows, missing {missing}")
    row = frame.iloc[0]
    return {"k": int(row["k"]), "m": int(row["m"]), "p_value": float(row["p_value"]),
            "original_statistic": float(row["original_statistic"]), "seed": int(row["seed"])}


def _replay_unverifiable_reason(run_id: int, payload: dict) -> "str | None":
    """L-1 row 12: None when the stored replay is verifiable; otherwise a short phrase naming
    which piece is missing or wrong, for the 409 detail. A replay is verifiable when: its own
    provenance block is non-empty, member_dataset_sha256 is present, both artifact entries carry a
    name and a sha256, AND (round 2, R1, a BLOCKER at the lead's read 1) the named file actually
    exists in the store and its real bytes hash to the declared sha256.

    R1's own correction: this used to be a shape check ONLY, on the theory that a store round trip
    belonged to the grade route's own read of the file, never to this admission check. The read
    found the real failure that theory hides: a SECOND replay of the same cell overwrites the two
    files under the SAME names (1.4 step 3, "tmp.replace(dest): atomic rename, overwrites a prior
    copy") and then fails its OWN backend re-check (step 4, _recheck_replay_payload) -- but the
    stored `artifacts` block still carries the OLD sha256 values from the replay that DID pass,
    because the cache row is never touched when the recheck refuses the write. A shape-only check
    reads that row as verifiable (every field present, well-shaped) even though the file on disk no
    longer matches the sha the row declares, so the grade would read a file the row never actually
    verified, silently. Hashing the real bytes here is the one place that catches it, at row 12,
    naming the mismatch by name, exactly as the spec's own row 12 condition already says: "a file's
    sha differs from artifacts" is one of row 12's own three refusal reasons, not a grade-time crash.
    """
    replay = payload.get("replay")
    if not isinstance(replay, dict):
        return "no replay object"
    if not payload.get("provenance"):
        return "provenance is empty"
    if not replay.get("member_dataset_sha256"):
        return "member_dataset_sha256 is absent"
    artifacts = replay.get("artifacts") or {}
    # Lane P4: the mcpt artifact (a permutation test already re-run on shuffled bars) is optional, but when
    # the replay declares it, it is verified exactly like the other two.
    # Lane P5: the random_arm artifact (check 9's random-entry arm) is optional and verified the same way.
    labels = ("equity", "trades") + tuple(x for x in ("mcpt", "random_arm") if x in artifacts)
    for label in labels:
        entry = artifacts.get(label)
        if not entry or not entry.get("name") or not entry.get("sha256"):
            return f"artifacts.{label} is incomplete"
        name = entry["name"]
        if not artifact_store.store.exists(run_id, name):
            return f"artifacts.{label} ({name!r}) is absent from the store"
        # Real round trip: hash the actual stored bytes and compare against the declared sha256,
        # never trust the row's own field once a file is confirmed present (R1's own correction).
        digest = hashlib.sha256()
        with artifact_store.store.open(run_id, name) as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            return f"artifacts.{label} ({name!r})'s sha256 disagrees with the stored file's own bytes"
    return None


def _resolve_check2_walkforward_run(conn, run_id: int) -> "tuple[str | None, int | None]":
    """Section 2's own chain-resolution rule: given R (an admitted, granted optimise-stage sweep),
    find R's own walk-forward run for check 2. Returns (word, wf_run_id): word is one of W-2's
    three check-2 link words (walkforward-run-not-found, walkforward-run-ambiguous,
    walkforward-link-refused) when check 2 itself must read not-evaluable, or the W-1 word
    walkforward-run-in-flight when the WHOLE grade must refuse (section 2's own table, row
    "in-flight"), or (None, wf_run_id) when a terminal run was ACTUALLY found (confirmed
    present by researchdb.get_run, not merely linked) and check_2_walk_forward should run
    against it.

    FIXED (round 5, lifecycle-checker finding, entity 13): the linked run row itself can be
    absent (researchdb.get_run(...) returns None, e.g. the run row was deleted after the link
    was granted). That case now answers walkforward-run-not-found by name, the same word
    already used for the other three absence cases above, rather than silently falling into
    the terminal-run branch below, which used to rely on check_2_walk_forward's own unrelated
    NOT_EVALUABLE fallback to avoid a wrong PASS, an accident rather than a checked case.
    """
    r_draw = conn.execute(
        "SELECT dr.strategy_id AS strategy_id, dr.strategy_version AS strategy_version, "
        "       dr.calendar_id AS calendar_id, dr.window_end_ts AS window_end_ts "
        "FROM research_data_draws rl "
        "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
        "WHERE rl.event_kind = 'run_linked' AND rl.run_id = ? AND rl.decision = 'granted' "
        "  AND dr.stage = 'optimise' LIMIT 1;",
        (run_id,),
    ).fetchone()
    if r_draw is None:
        return judge_checks.WORD_WALKFORWARD_RUN_NOT_FOUND, None  # defensive: _resolve_admission already proved this exists
    next_draw = conn.execute(
        "SELECT id, stage FROM research_data_draws "
        "WHERE event_kind = 'draw_requested' AND strategy_id = ? AND strategy_version = ? "
        "  AND calendar_id = ? AND decision = 'granted' AND window_start_ts >= ? "
        "ORDER BY window_start_ts ASC, id ASC LIMIT 1;",
        (r_draw["strategy_id"], r_draw["strategy_version"], r_draw["calendar_id"], r_draw["window_end_ts"]),
    ).fetchone()
    if next_draw is None or next_draw["stage"] != "walkforward":
        return judge_checks.WORD_WALKFORWARD_RUN_NOT_FOUND, None
    link_rows = conn.execute(
        "SELECT run_id, decision, refusal_reason FROM research_data_draws "
        "WHERE event_kind = 'run_linked' AND draw_id = ?;",
        (next_draw["id"],),
    ).fetchall()
    if len(link_rows) == 0:
        return judge_checks.WORD_WALKFORWARD_RUN_NOT_FOUND, None
    if len(link_rows) > 1:
        return judge_checks.WORD_WALKFORWARD_RUN_AMBIGUOUS, None
    link_row = link_rows[0]
    if link_row["decision"] != "granted":
        return judge_checks.WORD_WALKFORWARD_LINK_REFUSED, None
    wf_run = researchdb.get_run(link_row["run_id"])
    # Fix 4 (round 5): the granted link can name a run row that is no longer there (deleted after
    # the link was recorded). That is its own absence case, so it answers the same
    # walkforward-run-not-found word the other three absence branches above already use, instead
    # of falling through into the terminal-run return below with no run actually found.
    if wf_run is None:
        return judge_checks.WORD_WALKFORWARD_RUN_NOT_FOUND, None
    if wf_run["status"] in ("pending", "running"):
        return WORD_WALKFORWARD_RUN_IN_FLIGHT, None
    return None, link_row["run_id"]


def _expected_cell_metrics(run_id: int, variation_id: int, ticker: str) -> dict:
    """The stored research_run_cells row's own values for the ten metric columns spec L-2 names
    (researchdb._RUN_STATS_COLUMNS + _CELL_DIST_COLUMNS), carried in the replay's job payload as
    'expected_cell_metrics' for the worker's own comparison."""
    cell = researchdb.get_run_cell(run_id, variation_id, ticker)
    columns = researchdb._RUN_STATS_COLUMNS + researchdb._CELL_DIST_COLUMNS
    return {col: (cell.get(col) if cell else None) for col in columns}


@router.post("/runs/{run_id}/replay")
def replay_cell(run_id: int, variation_id: "int | None" = Query(default=None),
               ticker: "str | None" = Query(default=None), retry: bool = Query(default=False)):
    """POST /api/judge/runs/{run_id}/replay: request (or read the state of) the saved replay of
    one cell (spec section 1.4, "Who and when" step 1, and section 4's own replay-route outcomes,
    renumbered by Amendment 1). Admitted through the SAME L-1 rows 1-10 the grade route uses; its
    own outcomes past row 10, IN THIS ORDER: 1. 200 replay-exists when a verifiable replay is
    already stored. 2. 409 compute-in-flight when a compute job (replay or plain) is already
    queued or running for this exact cell key. 3. (Amendment 1, A2, unless retry=1) 409
    replay-not-reproduced or 409 replay-failed when the newest REPLAY job at this key failed. 4.
    (Amendment 1, A1) 409 replay-mode-unknown when the run's own registration row exists and its
    param_mode is NULL or unrecognised. 5. 409 worker-offline when no worker has reported online
    recently. 6. 409 replay-source-incomplete when the frozen registration/job payload cannot be
    assembled (including a MISSING registration row, which never reaches outcome 4). 7. otherwise
    202 replay-queued. `retry=1` is the one thing (besides a new formula_version) that unblocks
    outcome 3, mirroring `get_run_full_metrics`'s own `retry: bool = False` shape.

    Wrapped so EVERY W-1-word refusal (from `_resolve_admission` or this route's own outcomes) is
    counted in JUDGE_ROUTE_WORD_COUNTERS (B-6) by the word leading its `detail` string, without a
    hand-written increment at each raise site above.
    """
    try:
        return _replay_cell_impl(run_id, variation_id, ticker, retry=retry)
    except HTTPException as exc:
        word = str(exc.detail).split(":", 1)[0]
        if word in JUDGE_ROUTE_WORD_COUNTERS:
            JUDGE_ROUTE_WORD_COUNTERS[word] += 1
        raise


def _replay_cell_impl(run_id: int, variation_id: "int | None", ticker: "str | None", *, retry: bool = False):
    with researchdb.get_conn() as conn:
        run, v, t, _ = _resolve_admission(run_id, variation_id, ticker, conn, for_replay=True)

    # Outcome 1 (L-1 rows 11/12): read the cell's cache row at the run's own CURRENT
    # formula_version only (Amendment 1, R2: get_full_metrics_cache_by_cell no longer reads the
    # newest row under any formula_version -- a stale-key replay is treated as absent here).
    cache = researchdb.get_full_metrics_cache_by_cell(run_id, v, t)
    if cache is not None and _replay_unverifiable_reason(run_id, cache["payload"]) is None:
        return {"status": WORD_REPLAY_EXISTS, "run_id": run_id, "variation_id": v, "ticker": t}

    # A brand-new compute job's own formula_version IS the fresh, current resolve_formula_version()
    # read (the key a NEW write should land under, and the key outcomes 2-6 below all check against).
    formula_version = _resolve_formula_version(run)

    # Outcome 2, checked BEFORE everything past it (R3, the spec's own order): a plain job already
    # in flight for this key must answer compute-in-flight even when a later outcome would
    # separately refuse for an unrelated reason, so the in-flight refusal is never shadowed.
    if researchdb.compute_job_in_flight_for_key(run_id, v, t, formula_version) is not None:
        raise HTTPException(status_code=409, detail=f"{WORD_COMPUTE_IN_FLIGHT}: a compute job is already queued or running for this cell")

    # Outcome 3 (Amendment 1, A2): the newest REPLAY job at this key. If it failed, the route stops
    # re-queueing -- "a retry is always allowed" is WITHDRAWN for every failure (a deterministic
    # failure, e.g. a stale runner image, would otherwise loop the same way forever). Unblocked only
    # by retry=1 (this request) or a new formula_version (a different key, never reaches this branch).
    if not retry:
        latest_replay_job = researchdb.latest_replay_job_for_key(run_id, v, t, formula_version)
        if latest_replay_job is not None and latest_replay_job["status"] == "failed":
            err = latest_replay_job["error"] or ""
            if err.startswith(researchdb.WORD_REPLAY_NOT_REPRODUCED):
                raise HTTPException(status_code=409, detail=f"{researchdb.WORD_REPLAY_NOT_REPRODUCED}: {err}")
            raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_FAILED}: {err or 'no error recorded'}")

    # Outcome 4 (Amendment 1, A1): a NULL/unrecognised param_mode on a registration row that DOES
    # exist refuses by name, before anything is queued. A MISSING row (no registration at all)
    # is NOT this refusal -- it falls through to outcome 6 (replay-source-incomplete) below, since
    # build_cell_metrics_compute_payload is what actually detects that absence.
    with researchdb.get_conn() as conn:
        registration = conn.execute(
            "SELECT param_mode FROM research_sweep_registrations WHERE run_id = ?;", (run_id,),
        ).fetchone()
    if registration is not None and registration["param_mode"] not in ("zero", "inline", "vectorized"):
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_MODE_UNKNOWN}: {registration['param_mode']!r}")

    # Outcome 5.
    if not researchdb.any_worker_online():
        raise HTTPException(status_code=409, detail=f"{WORD_WORKER_OFFLINE}: no research worker has reported online recently")

    # Outcome 6.
    try:
        payload = researchdb.build_cell_metrics_compute_payload(run_id, v, t, formula_version)
    except (researchdb.RunNotFound, researchdb.SizingSpecNotRecordedError,  # P2f: no readable recorded sizing
            researchdb.RecordedSizingSpecUnreadable) as exc:
        raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_SOURCE_INCOMPLETE}: {exc}")
    payload["replay"] = True
    payload["expected_cell_metrics"] = _expected_cell_metrics(run_id, v, t)

    # Outcome 7.
    try:
        job_id = researchdb.enqueue_replay_metrics_job(payload)
    except researchdb.ComputeJobInFlight:
        raise HTTPException(status_code=409, detail=f"{WORD_COMPUTE_IN_FLIGHT}: a compute job is already queued or running for this cell")

    return JSONResponse(
        status_code=202,
        content={"status": WORD_REPLAY_QUEUED, "job_id": job_id, "run_id": run_id, "variation_id": v, "ticker": t},
    )


@router.post("/runs/{run_id}/judge")
def judge_run(run_id: int, variation_id: "int | None" = Query(default=None),
             ticker: "str | None" = Query(default=None)):
    """POST /api/judge/runs/{run_id}/judge: grade a succeeded run's own saved record.

    Admitted through L-1 (spec section 4): 404 on an unknown run; 409 on a run that is not
    `succeeded`; the full admission table for a sweep run (rows 3-14); 409 on a repeat grade under
    the exactly-once key (the existing row id in the detail). 200 with the new verdict row's id on
    a genuine first grade. A reserve exam run (row 7, no cell key) grades exactly as before this
    lane; a candidate's optimise-stage sweep grades the REPLAYED cell named by variation_id/ticker,
    reading its two series from the replay's own stored files and check 2 through the walk-forward
    run section 2 resolves, rather than an assumed variation-0 cell.

    Wrapped the same way replay_cell is: every W-1-word refusal is counted in
    JUDGE_ROUTE_WORD_COUNTERS (B-6) by its own leading word, without touching each raise site.
    """
    try:
        return _judge_run_impl(run_id, variation_id, ticker)
    except HTTPException as exc:
        word = str(exc.detail).split(":", 1)[0]
        if word in JUDGE_ROUTE_WORD_COUNTERS:
            JUDGE_ROUTE_WORD_COUNTERS[word] += 1
        raise


def _judge_run_impl(run_id: int, variation_id: "int | None", ticker: "str | None"):
    with researchdb.get_conn() as conn:
        run, resolved_variation_id, resolved_ticker, is_reserve_exam = _resolve_admission(
            run_id, variation_id, ticker, conn, for_replay=False)

        # Lane C9 (set 16b): the buy-and-hold control arm's own four check_9_controls keyword
        # arguments, initialised to their absent defaults here so BOTH branches below reach the
        # shared check_9_controls call at the bottom with all four bound. The reserve-exam branch
        # (row 7, no replay at all) never overrides these: check 9 reads them as "the buy-and-hold
        # arm is absent for this replay" for that path too, since a reserve exam has no replay to
        # carry one (AC-4's own generic absent-arm wording covers this case as well as an old,
        # pre-build replay).
        buy_hold_bar_returns = None
        buy_hold_error = None
        bar_returns_dates = None
        buy_hold_bar_returns_dates = None
        # `#426`: check 9's own candidate return series is a SEPARATE, DATED, LOG-change series
        # from `bar_returns` below (checks 6/7/8's own undated PERCENT-change series, unchanged).
        # Reusing `bar_returns` for check 9 was the bug the lead's read 1 found: the two formulas
        # disagree, and the candidate's own check-9 Sharpe must come from this variable, never that
        # one, on the real route (AC-5's own new route-path check exercises exactly this).
        candidate_log_returns = None

        random_arm_result = None  # lane P5: only a replay that declares artifacts.random_arm sets this
        rerun_result = None  # lane P4: only a replay that declares artifacts.mcpt sets this
        rerun_error = None  # lane GV3-J7: why a theory strategy's re-run could not be made
        if is_reserve_exam:
            # Row 7: graded exactly as today (the pre-existing single-cell-at-variation-0 path,
            # UNCHANGED so the exam stays gradable, spec section 4's own "the one named exception").
            cell = conn.execute(
                "SELECT variation_id, ticker FROM research_run_cells "
                " WHERE run_id = ? AND variation_id = 0 AND row_scope = 'cell';",
                (run_id,),
            ).fetchone()
            if cell is None:
                # Amendment 1, A4: a reserve exam with no variation-0 cell is a cell-not-found
                # refusal (row 10's own word), never a 500 -- JC-3 is behavioural, not textual.
                raise HTTPException(
                    status_code=404,
                    detail=f"{WORD_CELL_NOT_FOUND}: run {run_id} is succeeded but has no variation-0 "
                            f"research_run_cells row; the judge cannot identify the exam's own "
                            f"candidate cell to grade",
                )
            grade_variation_id, grade_ticker = cell["variation_id"], cell["ticker"]

            trades = None
            if run["trades_path"] is not None:
                try:
                    trades = export._read_parquet(run_id, "trades.parquet")
                except artifact_store.ArtifactNotFound:
                    trades = None
            bar_returns = None
            if run["equity_path"] is not None:
                try:
                    equity_df = export._read_parquet(run_id, "equity.parquet")
                except artifact_store.ArtifactNotFound:
                    equity_df = None
                if equity_df is not None:
                    bar_returns = _bar_returns_from_equity(equity_df)

            result_1 = judge_checks.check_1_evidence_floor(run_id, conn, trades, ticker=grade_ticker)
            result_2 = judge_checks.check_2_walk_forward(run_id, conn, judgedb.BATTERY_VERSION)
            result_3 = judge_checks.check_3_trial_count(run_id, conn, judgedb.BATTERY_VERSION, ticker=grade_ticker)
            result_4 = judge_checks.check_4_plateau(run_id, conn, judgedb.BATTERY_VERSION)
            graded_formula_version = _resolve_formula_version(run)
            # The exam path's own unchanged dataset_sha256 source: the graded run's own top-level
            # column, exactly as it read before this lane (the exam always grades variation 0 of
            # its own single run, so there is no separate per-cell member file to prefer).
            grade_dataset_sha256 = run["dataset_sha256"]
        else:
            # Rows 11-14: the replay route's own saved record is what this grade reads, at
            # whatever formula_version it was actually stored under (see the replay route's own
            # comment on get_full_metrics_cache_by_cell for why this is never a fresh
            # resolve_formula_version() call).
            grade_variation_id, grade_ticker = resolved_variation_id, resolved_ticker
            cache = researchdb.get_full_metrics_cache_by_cell(run_id, grade_variation_id, grade_ticker)
            replay = (cache or {}).get("payload", {}).get("replay") if cache else None
            if not isinstance(replay, dict):
                raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_ABSENT}: request the replay first")
            unverifiable_reason = _replay_unverifiable_reason(run_id, cache["payload"])
            if unverifiable_reason is not None:
                raise HTTPException(status_code=409, detail=f"{WORD_REPLAY_UNVERIFIABLE}: {unverifiable_reason}")

            # Section 2: the walk-forward run for check 2, or the reason it cannot be resolved. An
            # in-flight walk-forward run refuses the WHOLE grade (W-1, not a check-level reason).
            check2_word, wf_run_id = _resolve_check2_walkforward_run(conn, run_id)
            if check2_word is not None and check2_word in judge_checks.JC_CHECK2_CHECK3_MONITOR_COUNTERS:
                judge_checks.JC_CHECK2_CHECK3_MONITOR_COUNTERS[check2_word] += 1
            if check2_word == WORD_WALKFORWARD_RUN_IN_FLIGHT:
                raise HTTPException(status_code=409, detail=WORD_WALKFORWARD_RUN_IN_FLIGHT)

            # Same tolerance as the reserve-exam branch above (row 12's own shape-only check already
            # proved the ARTIFACT NAMES are declared; the file itself can still be absent, e.g. a
            # fixture that only declares metadata, or a real store whose upload genuinely failed
            # after the recheck passed). A missing file degrades checks 1/6/7/8/9 to not-evaluable
            # through their own trades=None/bar_returns=None handling, rather than a 500.
            trades = None
            try:
                trades = export._read_parquet(run_id, f"replay_trades_v{grade_variation_id}_{grade_ticker}.parquet")
            except artifact_store.ArtifactNotFound:
                trades = None
            bar_returns = None
            try:
                equity_df = export._read_parquet(run_id, f"replay_equity_v{grade_variation_id}_{grade_ticker}.parquet")
            except artifact_store.ArtifactNotFound:
                equity_df = None
            if equity_df is not None:
                # Checks 6/7/8: the SAME undated, PERCENT-change series as always, byte-unchanged
                # (`#426`'s own read confirmed these three must stay on this formula).
                bar_returns = _bar_returns_from_equity(equity_df)
                # `#426`: check 9's own candidate series is the DATED, NATURAL-LOG-change series,
                # read off the SAME equity frame already fetched above (never a second store round
                # trip), and kept in its OWN variable rather than reusing `bar_returns` -- reusing
                # it was the bug the lead's read 1 found (AC-5's own real-route check now proves the
                # two stay separate).
                candidate_log_returns, bar_returns_dates = _dated_bar_returns_from_equity(equity_df)

            # Lane P4 (#422): a replay that declares artifacts.mcpt carries a permutation test already
            # re-run on shuffled price bars; its hash was verified with the other two files above.
            # Lane GV3-J7: a theory strategy's replay (provenance engine research_lab.theory_stage) carries the file or mcpt_error.
            rerun_result, rerun_error = _check7_rerun_inputs(run_id, cache["payload"])
            # Lane P5 (#421 item 1): likewise check 9's random-entry arm; absent means None.
            random_arm_entry = (replay.get("artifacts") or {}).get("random_arm")
            if random_arm_entry is not None:
                random_arm_result = _read_random_arm_artifact(run_id, random_arm_entry["name"])

            # Lane C9 (set 16b, AC-1/AC-4): the buy-and-hold control arm, read off this SAME stored
            # replay object. `buy_hold_error` is read straight off the payload's own field, never
            # guessed (AC-1's own wiring: the worker folds a container-side arm failure into this
            # field rather than losing the candidate's own already-reproduced replay). A replay
            # computed before this build (or one whose upload otherwise never carried an arm) simply
            # carries no such key, and `.get()`'s own None default reads exactly like "no error
            # recorded, keep looking for the artifact" below, which then also reads absent (AC-4).
            buy_hold_error = replay.get("buy_hold_error")
            if buy_hold_error is None:
                try:
                    buy_hold_equity_df = export._read_parquet(
                        run_id, f"replay_buyhold_equity_v{grade_variation_id}_{grade_ticker}.parquet")
                except artifact_store.ArtifactNotFound:
                    # AC-6's own "the arm artifact missing at grading time" failure mode: counted
                    # HERE, at the route, the only place that knows the file was actually looked for
                    # and not found -- distinct from a container-side compute failure, which
                    # check_9_controls itself counts (buy_hold_error is not None, above).
                    judge_checks.CHECK9_BUY_HOLD_MONITOR_COUNTERS["buy_hold_artifact_missing"] += 1
                    buy_hold_equity_df = None
                if buy_hold_equity_df is not None:
                    buy_hold_bar_returns, buy_hold_bar_returns_dates = _dated_bar_returns_from_equity(
                        buy_hold_equity_df)

            result_1 = judge_checks.check_1_evidence_floor(
                run_id, conn, trades, ticker=grade_ticker, variation_id=grade_variation_id)
            if check2_word is not None:
                result_2 = judge_checks.CheckResult(2, judge_checks.NOT_EVALUABLE, {}, check2_word)
            else:
                result_2 = judge_checks.check_2_walk_forward(wf_run_id, conn, judgedb.BATTERY_VERSION)
            result_3 = judge_checks.check_3_trial_count(run_id, conn, judgedb.BATTERY_VERSION,
                                                        variation_id=grade_variation_id, ticker=grade_ticker)
            result_4 = judge_checks.check_4_plateau(run_id, conn, judgedb.BATTERY_VERSION,
                                                     centre=(grade_variation_id, grade_ticker))
            graded_formula_version = cache["formula_version"]  # spec section 3: "the replay key's fourth field"
            # Spec section 3: the stored verdict's own dataset_sha256 is "sourced per CELL" from the
            # replay's own member_dataset_sha256, never the sweep run's own top-level dataset_sha256
            # (which names the FULL universe file the sweep was dispatched against, not this one
            # member's own price data).
            grade_dataset_sha256 = replay.get("member_dataset_sha256")

        # AC-5/AC-6/AC-8: check 5 reads the artifact of THIS run (the optimise sweep the graded
        # replay came from in the else branch above; for the reserve-exam branch there is no
        # sweep concept at all, so the read below finds nothing and check 5 reads AC-5's own
        # not-evaluable wording, matching every other pre-C5 caller). The over-cap marker is
        # checked FIRST, before the ordinary reader below, so a genuinely-missing sweep's own
        # counter is never spuriously incremented for a run the worker deliberately skipped.
        cscv_artifact_over_cap = _cscv_over_cap_marker(run_id)
        if cscv_artifact_over_cap:
            cscv_block_sums, cscv_bars_dropped, cscv_artifact_missing = None, None, False
        else:
            cscv_block_sums, cscv_bars_dropped, cscv_artifact_missing = _read_cscv_artifact_for_run(run_id)
        result_5 = judge_checks.check_5_cscv_pbo(
            run_id, conn, judgedb.BATTERY_VERSION,
            cscv_block_sums=cscv_block_sums, cscv_bars_dropped=cscv_bars_dropped,
            cscv_artifact_missing=cscv_artifact_missing, cscv_artifact_over_cap=cscv_artifact_over_cap)
        result_6 = judge_checks.check_6_monte_carlo_bounds(run_id, conn, judgedb.BATTERY_VERSION, bar_returns, trades)
        result_7 = judge_checks.check_7_mcpt(run_id, conn, judgedb.BATTERY_VERSION, bar_returns, trades,
                                             rerun_result=rerun_result, rerun_error=rerun_error)
        result_8 = judge_checks.check_8_windfall(trades)
        result_9 = judge_checks.check_9_controls(
            run_id, conn, judgedb.BATTERY_VERSION, candidate_log_returns, trades,
            buy_hold_bar_returns=buy_hold_bar_returns, buy_hold_error=buy_hold_error,
            bar_returns_dates=bar_returns_dates, buy_hold_bar_returns_dates=buy_hold_bar_returns_dates,
            random_arm_result=random_arm_result)

    # Lane QR (`6.1f`, set 6 day plan section 7 criterion 1): each entry now widens to
    # {"check", "outcome", "quantities", "reason"}. "quantities" is the check's OWN dict
    # (judge_checks.CheckResult.quantities), never a value re-derived here, with every non-finite
    # float sanitised to JSON null by _sanitize_quantities above; "reason" is the check's OWN
    # string (CheckResult.reason). Before this fold the two extra keys did not exist at all, so a
    # row graded before this build (an "older row") still reads back with both keys ABSENT by
    # omission (json.loads of the older stored TEXT simply never produced them), never as an
    # empty {} or "" -- criterion 2's own requirement, held automatically by never having written
    # those rows with the wider shape in the first place.
    check_outcomes = [
        {"check": 1, "outcome": result_1.outcome,
         "quantities": _sanitize_quantities(result_1.quantities), "reason": result_1.reason},
        {"check": 2, "outcome": result_2.outcome,
         "quantities": _sanitize_quantities(result_2.quantities), "reason": result_2.reason},
        {"check": 3, "outcome": result_3.outcome,
         "quantities": _sanitize_quantities(result_3.quantities), "reason": result_3.reason},
        {"check": 4, "outcome": result_4.outcome,
         "quantities": _sanitize_quantities(result_4.quantities), "reason": result_4.reason},
        {"check": 5, "outcome": result_5.outcome,
         "quantities": _sanitize_quantities(result_5.quantities), "reason": result_5.reason},
        {"check": 6, "outcome": result_6.outcome,
         "quantities": _sanitize_quantities(result_6.quantities), "reason": result_6.reason},
        {"check": 7, "outcome": result_7.outcome,
         "quantities": _sanitize_quantities(result_7.quantities), "reason": result_7.reason},
        {"check": 8, "outcome": result_8.outcome,
         "quantities": _sanitize_quantities(result_8.quantities), "reason": result_8.reason},
        {"check": 9, "outcome": result_9.outcome,
         "quantities": _sanitize_quantities(result_9.quantities), "reason": result_9.reason},
    ]
    verdict = judgedb.compose_verdict(check_outcomes, battery_checks=list(judgedb.BATTERY_CHECK_ROSTER))

    warnings = (
        list(result_1.warnings) + list(result_2.warnings) + list(result_3.warnings)
        + list(result_4.warnings) + list(result_5.warnings) + list(result_6.warnings)
        + list(result_7.warnings) + list(result_8.warnings) + list(result_9.warnings)
    )

    # graded_formula_version was already resolved above (the exam path's own unchanged read, or
    # the cell-grade path's own replay-key read); this route never recomputes it a second time.
    now = int(time.time())
    judging_pass_id = (
        _judging_pass_id_for(run_id, judgedb.BATTERY_VERSION) if is_reserve_exam
        else _judging_pass_id_for_cell(run_id, grade_variation_id, grade_ticker, judgedb.BATTERY_VERSION)
    )
    with judgedb.get_judge_conn(judgedb.DB_PATH) as jconn:
        new_id = judgedb.record_verdict(
            jconn,
            run_id=run_id,
            judging_pass_id=judging_pass_id,
            variation_id=grade_variation_id,
            ticker=grade_ticker,
            graded_formula_version=graded_formula_version,
            strategy_id=run["strategy_id"],
            strategy_version=run["strategy_version"],
            dataset_sha256=grade_dataset_sha256,
            verdict=verdict,
            is_advisory=0,
            battery_version=judgedb.BATTERY_VERSION,
            evidence_floor_outcome=result_1.outcome,
            # allow_nan=False sits AFTER _sanitize_quantities has already removed every non-finite
            # float above: defence in depth (criterion 1's own words), so a non-finite value can
            # never reach the check_outcomes_json column as invalid "NaN" text even on a future
            # code path that skips the sanitiser by mistake -- json.dumps would raise ValueError
            # first, a loud failure, rather than silently writing text json_valid() would refuse.
            check_outcomes_json=json.dumps(check_outcomes, allow_nan=False),
            decided_by="battery",
            warnings_json=json.dumps(warnings),
            graded_at=now,
        )
    if new_id is None:
        # The exactly-once gate fired: the SAME (run_id, variation_id, ticker, judging_pass_id)
        # quadruple already has a row. Read it back so the 409 detail carries its id (AC-J7).
        JUDGE_ROUTE_COUNTERS["duplicate_grade_409_count"] += 1
        with judgedb.get_judge_conn(judgedb.DB_PATH) as jconn:
            existing = jconn.execute(
                "SELECT id FROM research_judge_verdicts "
                " WHERE run_id = ? AND variation_id = ? AND ticker = ? AND judging_pass_id = ?;",
                (run_id, grade_variation_id, grade_ticker, judging_pass_id),
            ).fetchone()
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} was already graded under battery {judgedb.BATTERY_VERSION} "
                    f"(existing verdict row id {existing['id'] if existing else None!r})",
        )
    # Lane JC: the response carries the graded CELL's own identity alongside the verdict, so a
    # caller never has to re-derive which run/variation/ticker/formula_version this grade covers
    # (jc08's own pin). Present for both the exam path and the cell-grade path, since both branches
    # above now set every one of these four names before reaching here.
    return {"verdict_id": new_id, "verdict": verdict, "battery_version": judgedb.BATTERY_VERSION,
            "check_outcomes": check_outcomes, "run_id": run_id, "variation_id": grade_variation_id,
            "ticker": grade_ticker, "graded_formula_version": graded_formula_version}


# =============================================================================
# GET /api/judge/verdicts/{verdict_id}, POST .../reject, POST .../approve (set 5 day, lane JP,
# U.5). Governing text: paper_trading_spec_brief.md sections 6.1, 9.1, 13.2; researchdb.py's
# record_verdict_rejection / DuplicateVerdictRejection; paperdb.py's admit_run / paper_transaction
# / StubVenue.
# =============================================================================

# The four admission refusals plus the one success path, counted by name (the ops routing law: a
# failure mode this build declares ships its own counter). Mirrors JUDGE_ROUTE_COUNTERS's own shape
# just above.
JUDGE_ADMISSION_COUNTERS = {
    "no_judged_backtest_at_this_slice_count": 0,
    "verdict_not_pass_count": 0,
    "verdict_not_current_count": 0,
    "reserve_exam_not_passed_count": 0,
    "retest_not_passed_count": 0,
    "pot_unreadable_from_venue_count": 0,
    "symbol_resolution_unavailable_from_venue_count": 0,
    "symbol_not_tradable_at_venue_count": 0,
    "pot_remainder_too_small_count": 0,
    "admitted_count": 0,
}

# REJECT's own refusal counter (the ops routing law again): a 404 (unknown verdict) or a 409
# (already rejected) each get their own name, mirroring JUDGE_ROUTE_COUNTERS's shape.
JUDGE_REJECTION_COUNTERS = {
    "verdict_not_found_404_count": 0,
    "already_rejected_409_count": 0,
    "rejected_count": 0,
}


class RejectBody(BaseModel):
    """POST body for REJECT. reason is optional free text Tamer may type; the default instance
    below (RejectBody()) is what lets the route accept a POST with no body at all, matching REJECT
    being a single press with nothing typed the common case.
    """
    reason: "str | None" = None


class ApproveBody(BaseModel):
    """POST body for APPROVE FOR PAPER. slice_opening_cash is REQUIRED (paper spec 9.1: the pot's
    size and every slice figure are read at runtime and never a literal default in code; this
    field is where the PAGE's own prefilled-but-editable control sends what it is showing, never a
    server-side constant standing in for it).
    """
    slice_opening_cash: float


def _get_verdict_row(conn, verdict_id: int):
    """The one verdict row REJECT and APPROVE both start from: every column plus the joined
    strategy name, matching list_verdicts's own LEFT JOIN shape. None when verdict_id names no row.
    """
    return conn.execute(
        "SELECT v.*, s.name AS strategy_name FROM research_judge_verdicts AS v "
        "LEFT JOIN research_strategies AS s ON s.id = v.strategy_id WHERE v.id = ?;",
        (verdict_id,),
    ).fetchone()


def _run_init_cash(conn, run_id: int) -> float:
    """The run's own frozen init_cash, read the SAME way researchdb.py's own
    build_cell_metrics_compute_payload / build_aggregate_metrics_compute_payload already read it
    (`frozen.get("init_cash", 10_000.0)`) -- no second implementation of that read, narrowed here
    to the one field the admission refusal needs. A run whose job payload is missing or empty (a
    very old or malformed row) reads the same 10_000.0 default those two functions already use,
    never a crash: the admission refusal this feeds is a COMPARISON, and a wrong comparison is
    still an honest refusal, never a 500.
    """
    job = conn.execute(
        "SELECT j.payload_json FROM research_jobs AS j "
        "JOIN research_runs AS r ON r.job_id = j.id WHERE r.id = ?;",
        (run_id,),
    ).fetchone()
    if job is None or not job["payload_json"]:
        return 10_000.0
    frozen = json.loads(job["payload_json"])
    return float(frozen.get("init_cash", 10_000.0))


def _with_display(conn, serialised: dict) -> dict:
    """Add each check's figure and bar (judge_display) and the run's judged capital to one row."""
    serialised["measures"] = judge_display.check_measures(
        serialised["check_outcomes"], conn, serialised["battery_version"])
    serialised["judged_capital"] = _run_init_cash(conn, serialised["run_id"])
    return serialised


@router.get("/verdicts/{verdict_id}")
def get_verdict(verdict_id: int):
    """GET /api/judge/verdicts/{verdict_id}: FULL RESULT's own read (UI plan 2.4.2, plan section 8
    criterion 5). One verdict row, exactly as _serialise_row shapes it for the list, plus "trials"
    the same subquery list_verdicts uses. 404 when the id names no row (never an empty object).
    """
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        if not _table_present(conn):
            _raise_store_absent(judgedb.DB_PATH)
        row = conn.execute(
            f"""
            SELECT v.*, s.name AS strategy_name, {_IS_CURRENT_EXPR} AS is_current,
                   (SELECT COUNT(*) FROM research_run_cells AS c
                     WHERE c.run_id = v.run_id AND c.row_scope = 'cell') AS trials,
                   rs.total_return, rs.max_drawdown, rs.num_trades, rs.sharpe_ratio,
                   (rvr.verdict_id IS NOT NULL) AS rejected, rvr.rejected_at AS rejected_at
              FROM research_judge_verdicts AS v
              LEFT JOIN research_strategies AS s ON s.id = v.strategy_id
              LEFT JOIN research_run_stats AS rs ON rs.run_id = v.run_id
              LEFT JOIN research_verdict_rejections AS rvr ON rvr.verdict_id = v.id
             WHERE v.id = ?;
            """,
            (verdict_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"verdict {verdict_id} not found")
        return _with_display(conn, _serialise_row(row))


@router.post("/verdicts/{verdict_id}/reject")
def reject_verdict(verdict_id: int, body: RejectBody = RejectBody()):
    """POST /api/judge/verdicts/{verdict_id}/reject: the survivor card's REJECT press (UI plan
    2.4.2). 404 when verdict_id names no row. 409, naming the id, on a second REJECT for the same
    verdict (researchdb.DuplicateVerdictRejection: UNIQUE (verdict_id), "at most one rejection per
    verdict a database fact"). rejected_by is the literal string "dashboard" -- the surface that
    recorded it (this lane's own lean, set 5 day plan section 2): no authenticated human identity
    reaches this backend, Caddy basic auth stops at the edge, and inventing a name would be a
    placeholder this codebase forbids.
    """
    with judgedb.get_judge_conn(judgedb.DB_PATH) as jconn:
        verdict_row = _get_verdict_row(jconn, verdict_id)
    if verdict_row is None:
        JUDGE_REJECTION_COUNTERS["verdict_not_found_404_count"] += 1
        raise HTTPException(status_code=404, detail=f"verdict {verdict_id} not found")

    with researchdb.transaction() as conn:
        try:
            rejection_id = researchdb.record_verdict_rejection(
                conn,
                verdict_id=verdict_id,
                run_id=verdict_row["run_id"],
                rejected_by="dashboard",
                reason=body.reason,
            )
        except researchdb.DuplicateVerdictRejection as exc:
            JUDGE_REJECTION_COUNTERS["already_rejected_409_count"] += 1
            raise HTTPException(
                status_code=409,
                detail=f"verdict {verdict_id} already has a recorded rejection",
            ) from exc
    JUDGE_REJECTION_COUNTERS["rejected_count"] += 1
    return {"rejection_id": rejection_id, "verdict_id": verdict_id}


_GROUP_LABEL_PATTERN = re.compile(r"^GROUP_[A-Z0-9_]+$")  # the same pattern as researchdb._THEORY_GROUP_LABEL


@router.post("/verdicts/{verdict_id}/approve")
async def approve_verdict(verdict_id: int, body: ApproveBody):
    """POST /api/judge/verdicts/{verdict_id}/approve: APPROVE FOR PAPER (UI plan 2.4.2, paper spec
    section 6.1). Refuses by name, in the spec's own order, and never assumes past a refusal a
    venue itself cannot answer:

      1. no-judged-backtest-at-this-slice: the run's own init_cash (its frozen job payload) does
         not match the requested slice, so grading this candidate at a different capital level
         would not represent trading it at the slice being credited (spec 13.2's own mechanism:
         "percent results diverge across account sizes through fees and affordability effects").
      2. verdict-not-pass: this route admits a PASS verdict only, stricter than spec 6.1's own
         "UNJUDGEABLE" wording (the set 5 day plan's own lean, section 2) and safe: a fail or
         unjudgeable candidate is refused here exactly as an unjudgeable one is.
      3. verdict-not-current: the verdict is advisory, or a newer non-advisory verdict exists for the
         same candidate key (_IS_CURRENT_EXPR). Only the current verdict may be admitted.
      4. reserve-exam-not-passed: this candidate has no passed reserve exam (reservedb.
         reserve_exam_passed; DECISIONS #329 ruling 4, #436 items 8 and 9).
      5. retest-not-passed: the newest Nautilus retest of the verdict's own run must read 'passed'
         (DECISIONS #437 ruling 3, with #329 ruling 4). retestdb.current_retest_state reads the
         newest row by id on the same connection as the reads above; 'never-retested',
         'report-only', 'could-not-run' and 'unreadable-record' all refuse.
      6. pot-unreadable-from-venue: the Alpaca paper account cannot be read (no paper keys, Alpaca
         unreachable, or the account is not ACTIVE; spec 9.1: the pot is read at runtime).
      7. symbol-not-tradable-at-venue / symbol-resolution-unavailable-from-venue: the run's ticker
         is empty or Alpaca says it is not tradable, or Alpaca cannot answer.
      8. pot-remainder-too-small: the slice is above the account equity minus the equity of every
         running slice; read and checked inside the admitting transaction.

    Nothing here sends an order (lane P6b). On no refusal, paperdb.admit_run runs
    inside one paper_transaction (E3/E5: the run, its slice state and its first equity snapshot are
    written atomically), then paper.run_admitted is published so PP's live-stream handler
    invalidates on it like every other paper.* event.
    """
    with judgedb.get_judge_conn(judgedb.DB_PATH) as jconn:
        verdict_row = _get_verdict_row(jconn, verdict_id)
    if verdict_row is None:
        raise HTTPException(status_code=404, detail=f"verdict {verdict_id} not found")

    with researchdb.get_conn() as conn:
        # Refusal 0 (GV2-4b, first so the later venue refusals never hide it): synthetic data never
        # reaches paper. Writes nothing to paper.
        try:
            run_is_synthetic = researchdb.run_data_is_synthetic(verdict_row["run_id"])
        except researchdb.RunDatasetMissing as exc:
            raise HTTPException(status_code=409, detail=f"run-dataset-missing: {exc}") from exc
        if run_is_synthetic:
            raise HTTPException(
                status_code=409,
                detail=f"synthetic-data-refused: run {verdict_row['run_id']} used synthetic data; "
                        "synthetic data never reaches paper",
            )
        # Refusal 0b (GV2-5c): a generator v2 group label names a set of stocks, never one tradable symbol.
        if _GROUP_LABEL_PATTERN.match(verdict_row["ticker"] or ""):
            raise HTTPException(
                status_code=409,
                detail=f"group-label-not-tradable: {verdict_row['ticker']} is a stock group, not one tradable symbol",
            )
        run_init_cash = _run_init_cash(conn, verdict_row["run_id"])

    # Refusal 1: the slice must match what the candidate was actually judged at. A small float
    # tolerance, not exact equality, because both figures round-trip through JSON and SQLite REAL.
    if abs(run_init_cash - body.slice_opening_cash) > 1e-6:
        JUDGE_ADMISSION_COUNTERS["no_judged_backtest_at_this_slice_count"] += 1
        raise HTTPException(
            status_code=409,
            detail=(
                "no-judged-backtest-at-this-slice: this candidate was judged at a backtest "
                f"capital of {run_init_cash:,.2f}, not the requested slice of "
                f"{body.slice_opening_cash:,.2f}"
            ),
        )

    # Refusal 2: only a PASS verdict may be admitted.
    if verdict_row["verdict"] != "pass":
        JUDGE_ADMISSION_COUNTERS["verdict_not_pass_count"] += 1
        raise HTTPException(
            status_code=409,
            detail=f"verdict-not-pass: verdict {verdict_id} is {verdict_row['verdict']!r}, "
                    f"not 'pass'",
        )

    # Refusal 3: only the CURRENT verdict may be admitted. An advisory verdict, or one a newer
    # non-advisory verdict has superseded, is refused here, on the same connection as the reserve
    # exam read below.
    with judgedb.get_judge_conn(judgedb.DB_PATH) as jconn:
        current_row = jconn.execute(
            f"SELECT v.id FROM research_judge_verdicts AS v WHERE v.id = ? AND {_IS_CURRENT_EXPR};",
            (verdict_id,),
        ).fetchone()
        if current_row is None:
            JUDGE_ADMISSION_COUNTERS["verdict_not_current_count"] += 1
            raise HTTPException(
                status_code=409,
                detail=f"verdict-not-current: verdict {verdict_id} is advisory or a newer verdict "
                        "exists for the same candidate; approve the current one",
            )

        # Refusal 4: the candidate must have a passed reserve exam (reservedb.reserve_exam_passed,
        # the one shared reader; reservedb.DB_PATH is this same research.db file).
        if not reservedb.reserve_exam_passed(
            jconn, verdict_row["strategy_id"], verdict_row["strategy_version"]
        ):
            JUDGE_ADMISSION_COUNTERS["reserve_exam_not_passed_count"] += 1
            raise HTTPException(
                status_code=409,
                detail=f"reserve-exam-not-passed: this candidate has no passed reserve exam yet "
                        f"(verdict {verdict_id})",
            )

        # Refusal 5: the newest Nautilus retest of this candidate's run must read 'passed'
        # (DECISIONS #437 ruling 3). Read on the same connection as the reads above.
        run_id = verdict_row["run_id"]
        retest_state = retestdb.current_retest_state(jconn, run_id)
        if retest_state != "passed":
            JUDGE_ADMISSION_COUNTERS["retest_not_passed_count"] += 1
            raise HTTPException(
                status_code=409,
                detail=f"retest-not-passed: the newest Nautilus retest of run {run_id} is "
                        f"'{retest_state}' (or: run {run_id} has no Nautilus retest yet).",
            )

    # Refusal 6: the Alpaca paper account must be readable and ACTIVE (spec 9.1: the pot is read at
    # runtime, never remembered). A missing key or an unreachable Alpaca refuses, with the cause.
    try:
        venue = alpaca_paper_venue.get_paper_venue()
        account = venue.get_account()
    except (alpaca_paper_venue.PaperVenueNotConfigured, alpaca_paper_venue.PaperVenueUnreachable) as exc:
        JUDGE_ADMISSION_COUNTERS["pot_unreadable_from_venue_count"] += 1
        raise HTTPException(status_code=409, detail=f"pot-unreadable-from-venue: {exc}") from exc
    if account["status"] != "ACTIVE":
        JUDGE_ADMISSION_COUNTERS["pot_unreadable_from_venue_count"] += 1
        raise HTTPException(
            status_code=409,
            detail=f"pot-unreadable-from-venue: the paper account is {account['status']}",
        )

    # Refusal 7: the run's own ticker must be a tradable symbol at the venue. A NULL or empty
    # ticker is refused without asking Alpaca.
    ticker = verdict_row["ticker"]
    if not ticker:
        JUDGE_ADMISSION_COUNTERS["symbol_not_tradable_at_venue_count"] += 1
        raise HTTPException(status_code=409, detail="symbol-not-tradable-at-venue: no ticker")
    try:
        tradable = venue.resolve_symbol(ticker)
    except alpaca_paper_venue.PaperVenueUnreachable as exc:
        JUDGE_ADMISSION_COUNTERS["symbol_resolution_unavailable_from_venue_count"] += 1
        raise HTTPException(
            status_code=409,
            detail=f"symbol-resolution-unavailable-from-venue: {exc}",
        ) from exc
    if not tradable:
        JUDGE_ADMISSION_COUNTERS["symbol_not_tradable_at_venue_count"] += 1
        raise HTTPException(status_code=409, detail=f"symbol-not-tradable-at-venue: {ticker}")

    # Refusal 8 and the admission, in one paper transaction: the pot's uncommitted remainder is the
    # account equity minus the equity of every running slice (paperdb.list_running_runs' rule). A
    # slice above it rolls back and nothing is written.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with paperdb.paper_transaction(paperdb.DB_PATH) as pconn:
        committed = pconn.execute(
            "SELECT COALESCE(SUM(s.equity), 0.0) FROM paper_runs AS r "
            "JOIN paper_slice_current AS s ON s.paper_run_id = r.paper_run_id "
            "WHERE r.ended_at IS NULL"
        ).fetchone()[0]
        remainder = account["equity"] - committed
        if body.slice_opening_cash > remainder:
            JUDGE_ADMISSION_COUNTERS["pot_remainder_too_small_count"] += 1
            raise HTTPException(
                status_code=409,
                detail=(
                    f"pot-remainder-too-small: {body.slice_opening_cash:,.2f} asked, "
                    f"{remainder:,.2f} uncommitted"
                ),
            )
        paper_run_id = paperdb.admit_run(
            pconn,
            strategy_id=verdict_row["strategy_id"],
            strategy_name=verdict_row["strategy_name"] or f"strategy {verdict_row['strategy_id']}",
            strategy_version=verdict_row["strategy_version"],
            source_run_id=verdict_row["run_id"],
            slice_opening_cash=body.slice_opening_cash,
            started_at=now,
            source_data_synthetic=run_is_synthetic,
        )
    JUDGE_ADMISSION_COUNTERS["admitted_count"] += 1
    await broadcaster.publish("paper.run_admitted", {
        "paper_run_id": paper_run_id,
        "verdict_id": verdict_id,
        "run_id": verdict_row["run_id"],
        "strategy_id": verdict_row["strategy_id"],
        "slice_opening_cash": body.slice_opening_cash,
    })
    return {"paper_run_id": paper_run_id, "verdict_id": verdict_id}
