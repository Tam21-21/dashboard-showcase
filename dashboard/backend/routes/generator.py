"""Generator routes: the Generator page's first half (set 6 day, lane GP, U.4, docs/plans/night/
2026-09-13_set6b_day/PLAN.md section 8) AND second half (set 12 night, lane G2, U.4 second half,
docs/plans/night/2026-09-14_set9a_night/PLAN.md section 11, corrected by docs/plans/night/
2026-09-20/PLAN.md section 8): PREVIEW, RUN GENERATOR, the job the worker relays facts through,
and the job-status/worker-status reads.

Registered by main.py with one include_router line, a NEW router file nobody else's area touches
this set (the plan's own lean: "GP owns a new router routes/generator.py on main.py, one include
line, main.py otherwise nobody's").

No auth dependency on the browser routes below, on purpose, matching every other browser route in
this backend (dashboard/backend/CLAUDE.md, "How work flows": "Caddy basic auth handles humans at
the edge"). Set 16b lane WD (#423): the two worker-facing routes (the theory-content read the
worker itself calls, and the facts relay) live on a SEPARATE router, worker_router, prefixed
/api/research/worker/generator, gated behind require_worker_api_key at the router level (imported
from .research rather than re-implemented, the SAME dependency object every worker route in this
backend already shares) -- the only prefix production's Caddy lets a non-human caller through
without HTTP basic auth (dashboard/deploy/Caddyfile's own @research_worker matcher).

Every query below opens its own connection at call time (researchdb.get_conn() / .transaction()),
never a connection captured once at import time, the same discipline routes/judge.py's own
docstring states and for the same reason: it is what lets a probe point the module's DB_PATH at a
scratch file before making a request and have this router actually read that file.
"""
from __future__ import annotations

# asyncio: runs the host-side generation loop in a thread (never on the event loop, never inside
# the worker, which has no database access and must never import vectorbtpro).
# hashlib: the theory content's own identity check (POST /theories echoes the saved sha256).
# json: job payload/result encoding (research_jobs.payload_json / result_json are plain TEXT).
# re: the theory_id slug shape (round-2 fold, finding F1).
# tempfile/shutil: the throwaway file theory.py's real parser needs (it takes a Path, not raw
# text, and widening its contract is not this lane's file to touch), reused by the facts relay.
# sys/Path: the repo-root sys.path guard below, mirroring catalog.py's own _ensure_strategies_on_path.
# time: research_jobs.created_at / finished_at are unix seconds, the same column shape every
# other job-writing route in this backend already uses.
import asyncio
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import researchdb
from .research import require_worker_api_key

router = APIRouter(prefix="/api/research/generator", tags=["generator"])

# Set 16b, lane WD (#423): the worker's front door. Production's Caddy only lets
# /api/research/worker/*, /api/research/share/* and four PWA files past human basic-auth
# (dashboard/deploy/Caddyfile's own @research_worker matcher), so every route the laptop worker
# itself calls (never a human) belongs under /api/research/worker/, behind require_worker_api_key,
# on its OWN router rather than the browser router above: an APIRouter's own prefix concatenates
# with every path it registers, so a worker route cannot live on `router` (whose prefix is the
# browser-facing /api/research/generator) without doubling that prefix into the URL. Both routes
# below require the key at the ROUTER level (the same shape routes/research.py's own worker_router
# already uses), so neither can ship un-gated by a missed per-route decorator.
worker_router = APIRouter(
    prefix="/api/research/worker/generator", tags=["generator-worker"],
    dependencies=[Depends(require_worker_api_key)],
)

# ---- ops routing law monitors (docs/ops/ops_monitor_registry.md; the row text itself is
# docs/units/GP/monitor_rows.md, since this lane's own areas do not include the registry file) --
# In-memory, process-local, read live and never cached, the same shape every sibling monitor dict
# in this backend already uses (judgedb.JUDGE_MONITOR_COUNTERS, routes/judge.py's own
# JUDGE_ROUTE_COUNTERS): an ops.db write path is parked until October (#300), so this is the
# honest interim shape until the operations page ships.
GENERATOR_MONITOR_COUNTERS: dict[str, int] = {
    # Every successful POST /theories, so a THEORIES editor whose save path silently breaks (or
    # one nobody uses) is a flat counter rather than invisible.
    "theories_saved_by_editor": 0,
    # Every candidate whose stored refusal_reason falls through _reason_words()'s own known
    # prefixes and exact tokens to its "refused: <token>" default: a stored refusal code with no
    # plain-words mapping is exactly the silent-failure class the ops routing law exists to catch.
    "generated_candidates_unmapped_reason": 0,
    # generator_preview_writes (docs/units/G2/monitor_rows.md): rows a write=false (PREVIEW) job
    # actually wrote. Must always read 0; a nonzero reading is a BLOCKER, since it means the
    # write=False guard in research_lab.generator.loop.generate_one was bypassed somewhere.
    "preview_writes": 0,
    # filler_compile_refused (docs/units/GR/monitor_rows.md, lane GR, AC-12): a filler
    # research_lab/generator/compile_facts.py's own per-filler isolation (AC-7) could not
    # compile in the pinned image, e.g. one needing an input this generator never hands it.
    # A rising count means a theory names a filler this build can never generate from, which
    # would otherwise be invisible (the theory's OTHER fillers still compile and generate
    # fine, so nothing on the page alone looks broken).
    "filler_compile_refused": 0,
}
# A GAUGE, not a counter: the CURRENT count, set fresh on every GET /catalogue-marks call to how
# many strategies that call just found marked final, never accumulated across calls.
GENERATOR_MONITOR_GAUGES: dict[str, int] = {"strategies_marked_final": 0}


def _ensure_repo_root_on_path() -> None:
    """Puts the repo root on sys.path so `research_lab.generator.*` imports resolve, the same
    sys.path idiom catalog.py's own _ensure_strategies_on_path uses for `strategies.*` (the
    plan's own words, criterion 1). research_lab carries no __init__.py (a PEP 420 namespace
    package, `ls research_lab/__init__.py` finds nothing), so importing anything under it needs
    the REPO ROOT itself on sys.path, one level above what catalog.py's own helper inserts (that
    one inserts research_lab/ itself, for the bare `strategies.*` names). This call is idempotent
    and a defensive no-op in every context that already has the repo root on sys.path (production's
    PYTHONPATH, dashboard/backend/paperdb.py's own bare `from research_lab.strategies.helpers
    import sizing` proves the repo root already resolves there with zero sys.path code); it is the
    one thing that also makes the same import resolve inside a bare-router probe that built its
    own FastAPI() app around only this module and has not already added the repo root itself.

    routes/generator.py -> parents[0]=routes, [1]=backend, [2]=dashboard, [3]=repo root.
    """
    repo_root = str(Path(__file__).resolve().parents[3])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


_ensure_repo_root_on_path()

from research_lab.generator import theory as generator_theory  # noqa: E402
from research_lab.generator import facts as generator_facts  # noqa: E402
from research_lab.generator import loop as generator_loop  # noqa: E402
from research_lab.generator.emit import derive_strategy_name  # noqa: E402

# ---- SKELETONS (criterion 1, 3) --------------------------------------------------------------

# The three knob keys the one built skeleton (band_cap) exposes, and their human labels. A module
# constant, not a per-request literal, so the mapping is one place and a probe can assert against
# it directly. Every key is a real, committed docs/research_lab/parameter_provenance.md heading
# (verified: `grep -n '^## search.generator_' docs/research_lab/parameter_provenance.md`), never
# an invented one.
_KNOB_LABELS: dict[str, str] = {
    "search.generator_entry_quantile": "Entry quantile",
    "search.generator_exit_quantile": "Exit quantile",
    "search.generator_holding_cap": "Holding cap",
}


def _knob_payload(key: str) -> dict:
    """One knob's own JSON shape: its key, its human label, and its provenance entry READ THROUGH
    the existing provenance reader (researchdb.provenance_entry), never a second, independently
    typed copy of the grid numbers that could drift from the committed file. A missing provenance
    entry (the DB-first-then-markdown reader answers None for both domains) answers the knob with
    the literal word "absent", never a literal grid: there is nothing to show, so nothing is shown.
    """
    entry = researchdb.provenance_entry(key)
    if entry is None:
        return {"key": key, "label": _KNOB_LABELS.get(key, key), "provenance": "absent"}
    return {"key": key, "label": _KNOB_LABELS.get(key, key), "provenance": entry}


@router.get("/skeletons")
def get_skeletons():
    """The admissible skeletons, read from the real module (never a fixture copy): each with its
    three knob keys and, per knob, the provenance entry the grid rests on. NO SKELETON REGISTERED
    is not a state this build can reach (ADMISSIBLE_SKELETONS names exactly one today), so this
    route never has to render that word.
    """
    return [
        {"id": skeleton_id, "knobs": [_knob_payload(key) for key in _KNOB_LABELS]}
        for skeleton_id in generator_theory.ADMISSIBLE_SKELETONS
    ]


# ---- THEORIES and the editor (criterion 1, 4) ------------------------------------------------


class SaveTheoryBody(BaseModel):
    theory_id: str
    content: str


# The theory's own identity shape (round-2 fold, finding F1): theory.py's own contract makes
# theory_id a FILE's stem (research_lab/generator/theory.py's Theory.theory_id docstring), so an
# editor-typed id is held to that same shape: lowercase, starts with a letter, letters/digits/
# underscores only, 3 to 64 characters total. Checked BEFORE the content parser runs (the plan's
# own ordering), so a malformed id is refused without ever writing a temp file for the content.
_THEORY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_THEORY_ID_RULE_SENTENCE = (
    "theory_id must be lowercase, start with a letter, contain only letters, digits and "
    "underscores, and be 3 to 64 characters long"
)


def _validate_theory_id(theory_id: str) -> None:
    """Raises HTTPException(422, ...) naming the rule in plain words when theory_id does not
    match the slug shape. A pydantic-level str already guarantees a string is present; this is
    the SHAPE check the plan's own criterion 1 asks for."""
    if not _THEORY_ID_RE.match(theory_id):
        raise HTTPException(status_code=422, detail=_THEORY_ID_RULE_SENTENCE)


def _validated_theory_content(content: str) -> None:
    """Runs the REAL theory.py parser over `content` (theory.py's own five-heading, skeleton,
    filler and look-ahead checks), via a throwaway temp file: load_theory takes a Path, not raw
    text, and widening that contract is research_lab/generator/theory.py, not this lane's file.
    Raises HTTPException(422, ...) naming the offending heading on any refusal, with the temp
    file's own filesystem path stripped out of the message first (rule 9: no machine identifier,
    and a generated temp path is exactly that, ever visible in a rendered error).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="generator-theory-"))
    tmp_path = tmp_dir / "theory.md"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        generator_theory.load_theory(tmp_path)
    except generator_theory.TheoryLoadError as exc:
        # Every TheoryLoadError message theory.py raises opens with "theory file {path} ...";
        # this is the EXACT path we just gave it, so a plain string replace (not a regex) removes
        # it precisely, leaving the informative tail (which heading, which id) untouched.
        detail = str(exc).replace(f"theory file {tmp_path} ", "the theory content ")
        raise HTTPException(status_code=422, detail=detail) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/theories")
def get_theories():
    """The newest row per theory_id (every saved version still reachable one at a time through
    GET /theories/{sha256} below), each with its own version count and the count of candidate
    rows naming its own sha256 (the fact the page's NEVER RUN word is derived from). Rows come
    back newest-created-first from researchdb.list_theories(); a dict keeps only the FIRST row
    seen per theory_id, which is that theory_id's own newest row, and iterating grouped.items()
    afterwards preserves that same newest-first order across theory_ids too.
    """
    rows = researchdb.list_theories()
    grouped: dict[str, dict] = {}
    version_counts: dict[str, int] = {}
    for row in rows:
        version_counts[row["theory_id"]] = version_counts.get(row["theory_id"], 0) + 1
        grouped.setdefault(row["theory_id"], row)
    out = []
    with researchdb.get_conn() as conn:
        for theory_id, row in grouped.items():
            candidate_count = conn.execute(
                "SELECT COUNT(*) AS n FROM research_generated_candidates WHERE theory_sha256 = ?;",
                (row["sha256"],),
            ).fetchone()["n"]
            out.append({
                "theory_id": theory_id,
                "sha256": row["sha256"],
                "source": row["source"],
                "created_at": row["created_at"],
                "versions": version_counts[theory_id],
                "candidates": candidate_count,
            })
    return out


def _theory_by_sha_or_404(sha256: str) -> dict:
    """Shared lookup for both theory-content reads below (the browser route here, and its worker
    twin on worker_router, set 16b lane WD AC-1): one 404 word, never two copies that could drift.
    """
    row = researchdb.get_theory_by_sha(sha256)
    if row is None:
        raise HTTPException(status_code=404, detail="no theory found for the given content hash")
    return row


@router.get("/theories/{sha256}")
def get_theory(sha256: str):
    """One saved theory version by its own content identity. 404 for an unknown sha256. Untouched
    by set 16b lane WD (#423): the Generator page keeps calling this exact route, generator/api.ts
    line 83."""
    return _theory_by_sha_or_404(sha256)


@worker_router.get("/theories/{sha256}")
def get_theory_worker(sha256: str):
    """Set 16b lane WD (#423) AC-1: the worker's own twin of GET /theories/{sha256} above, same
    content for the same sha256, reached under /api/research/worker/ so Caddy's basic-auth front
    door lets the worker through at all (the browser route above sits outside that prefix; a
    laptop worker calling it directly never even reaches this backend, it gets Caddy's own 401).
    Behind require_worker_api_key at the router level (worker_router's own dependencies=)."""
    return _theory_by_sha_or_404(sha256)


@router.post("/theories")
def post_theory(body: SaveTheoryBody):
    """Round-2 fold, finding F1: takes {theory_id, content}. The slug shape is checked FIRST (a
    422 naming the rule, before the content parser ever runs, the plan's own ordering), then the
    generator's own parser validates the content (a 422 naming the missing heading), then
    researchdb.save_theory: append-only, the same (theory_id, content) pair twice is one row, the
    same theory_id with different content is a new version row, and the response always carries
    the {theory_id, sha256} of the row that now holds this content (an identical text already
    saved under another id answers with THAT row's own id, per save_theory's own docstring).
    """
    _validate_theory_id(body.theory_id)
    _validated_theory_content(body.content)
    theory_id, sha256 = researchdb.save_theory(body.theory_id, body.content, source="editor")
    GENERATOR_MONITOR_COUNTERS["theories_saved_by_editor"] += 1
    return {"theory_id": theory_id, "sha256": sha256}


# ---- CANDIDATES (criterion 1, 5) --------------------------------------------------------------

# Refusal-reason prefixes, mapped to plain words (the UI plan's own table, section 2.3.1). A
# module constant, not per-request logic, so the mapping is one place a probe can exercise
# directly rather than reverse-engineering it from rendered output.
_REASON_WORD_PREFIXES: dict[str, str] = {
    "allowlist": "uses a word the theory did not allow",
    "lookahead": "reads future bars",
    # AC-7/AC-12 (lane GR): compile_facts.py's own per-filler refusal, e.g. a filler whose
    # compiled indicator class needs an input this generator never hands it. Mapped so the
    # page never shows the raw "refused: filler_compile_failed:..." fallback for it (which
    # would also, wrongly, move generated_candidates_unmapped_reason for a KNOWN reason).
    "filler_compile_failed": "could not be compiled",
    # Code-quality round 1, SF3 (lane GR): loop.py's generate_one refuses, by name, any
    # filler whose compiled facts declare a parameter (a non-empty param_names) before this
    # build's run()/sweep_signals() has any way to read it through self.param at runtime.
    # Mapped here so the page shows plain words rather than the raw
    # "refused: filler_declares_unread_params:<names>" fallback, and so a KNOWN reason never
    # moves generated_candidates_unmapped_reason.
    "filler_declares_unread_params": "declares a knob this build cannot sweep yet",
}
# Exact-match refusal tokens (no ":"-separated argument), mapped the same way.
_REASON_WORD_EXACT: dict[str, str] = {
    "derived_name_collision": "a strategy with this name already exists",
}


def _reason_words(decision: str, refusal_reason: "str | None") -> str:
    """The candidate's decision in plain words (the UI plan's own table): "accepted" for an
    accepted row; the mapped phrase for a known prefix or exact token; "refused: <token>" for any
    other stored token, so a refusal reason this build has never seen still reads as a refusal
    naming its own stored word, never a blank or a crash.
    """
    if decision == "accepted":
        return "accepted"
    token = refusal_reason or ""
    if token in _REASON_WORD_EXACT:
        return _REASON_WORD_EXACT[token]
    prefix, sep, rest = token.partition(":")
    if sep and prefix in _REASON_WORD_PREFIXES:
        return f"{_REASON_WORD_PREFIXES[prefix]}: {rest}"
    # An unrecognised token: count it (the generated_candidates_unmapped_reason monitor) before
    # falling back, so a new refusal code that ships with no plain-words mapping is visible rather
    # than silently reading as "refused: <raw token>" forever.
    GENERATOR_MONITOR_COUNTERS["generated_candidates_unmapped_reason"] += 1
    return f"refused: {token}"


def _job_outcome_reason_words(decision: str, reason: "str | None") -> str:
    """Plain words for one run_generator JOB outcome's own reason (PREVIEW's would-be list,
    RUN GENERATOR's result list): extends _reason_words (which only ever sees 'accepted' or
    'refused', since a 'skipped' row is never written to research_generated_candidates at all)
    with the two 'skipped' shapes a job outcome CAN carry: the pre-existing-row identity match
    (loop.generate_one's own step 6/pre-check, reason=None) and the not-selected skip
    (generate()'s own restriction to a RUN GENERATOR's ticked subset, reason='not-selected').
    """
    if decision == "skipped":
        return "not selected" if reason == "not-selected" else "already generated"
    return _reason_words(decision, reason)


@router.get("/candidates")
def get_candidates(theory_sha: "str | None" = None, decision: "str | None" = None):
    """Every generated candidate (optionally filtered by theory_sha256 and/or decision), joined to
    research_strategies for the name. reason_words is computed server-side (see _reason_words)
    so the frontend never re-derives the mapping. AC-9 lifecycle: when the joined strategy row is
    gone (a soft reference with no cascade), the name is still derivable from this candidate's OWN
    identity fields via the SAME formula the generator used to name it (emit.derive_strategy_name,
    a pure function of theory_id/skeleton_id/filler_id), and the link is honestly reported absent
    (strategy_id null in the response) rather than pointing at nothing.
    """
    query = (
        "SELECT g.theory_id AS theory_id, g.theory_sha256 AS theory_sha256, "
        "       g.skeleton_id AS skeleton_id, g.filler_id AS filler_id, g.expression AS expression, "
        "       g.decision AS decision, g.refusal_reason AS refusal_reason, "
        "       g.strategy_id AS strategy_id, g.generated_at AS generated_at, "
        "       s.name AS strategy_name "
        "  FROM research_generated_candidates g "
        "  LEFT JOIN research_strategies s ON s.id = g.strategy_id "
    )
    clauses: list[str] = []
    params: list = []
    if theory_sha is not None:
        clauses.append("g.theory_sha256 = ?")
        params.append(theory_sha)
    if decision is not None:
        clauses.append("g.decision = ?")
        params.append(decision)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY g.generated_at DESC, g.id DESC;"

    with researchdb.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    out = []
    for row in rows:
        strategy_id = row["strategy_id"]
        strategy_name = row["strategy_name"]
        if strategy_id is not None and strategy_name is None:
            # The soft-referenced strategy row no longer exists: re-derive the name it was given
            # at generation time and report the link honestly absent.
            strategy_name = derive_strategy_name(row["theory_id"], row["skeleton_id"], row["filler_id"])
            strategy_id = None
        out.append({
            "theory_id": row["theory_id"],
            "theory_sha256": row["theory_sha256"],
            "skeleton_id": row["skeleton_id"],
            "filler_id": row["filler_id"],
            "expression": row["expression"],
            "decision": row["decision"],
            "refusal_reason": row["refusal_reason"],
            "reason_words": _reason_words(row["decision"], row["refusal_reason"]),
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "generated_at": row["generated_at"],
        })
    return out


# ---- catalogue-marks (criterion 1, 8) ----------------------------------------------------------

# "Is this row the CURRENT one for its candidate key" (strategy_id, strategy_version, variation_id,
# ticker, dataset_sha256): the same rule routes/judge.py's own _IS_CURRENT_EXPR and
# routes/research.py's own _RUNS_VERDICT_IS_CURRENT_EXPR compute, duplicated here rather than
# imported from either file ON PURPOSE, matching routes/research.py's own stated reason: those are
# DIFFERENT lanes' areas for this night set, and importing one lane's private name into another
# file while both are being built concurrently would couple them at import time. The text is
# schema-derived (the candidate-key definition itself), so the copies can only drift if the schema
# changes, a review event on every file either way.
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


def _judge_verdicts_table_present(conn) -> bool:
    """True when research_judge_verdicts exists in the file this connection opened. Guards a
    store where researchdb.init_db() ran but judgedb.init_judge_db() has not (a probe fixture
    shape; production always calls both at boot in order): a fresh store then reads every strategy
    as carrying no FINAL mark, never a crash."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'research_judge_verdicts'"
    ).fetchone() is not None


@router.get("/catalogue-marks")
def get_catalogue_marks():
    """Per active strategy id: `final` (a current, non-advisory 'pass' verdict on ANY of its
    cells) and `theory_sha` (from its own accepted candidate row, if any). A strategy with
    neither mark is OMITTED from the response entirely (never a `final: false` key, which would
    still read as a literal "final" substring to a caller checking for the mark's presence).
    """
    with researchdb.get_conn() as conn:
        if _judge_verdicts_table_present(conn):
            final_rows = conn.execute(
                f"""
                SELECT DISTINCT v.strategy_id AS strategy_id
                  FROM research_judge_verdicts AS v
                  JOIN research_strategies AS s ON s.id = v.strategy_id AND s.status = 'active'
                 WHERE v.verdict = 'pass'
                   AND {_VERDICT_IS_CURRENT_EXPR};
                """
            ).fetchall()
            final_ids = {row["strategy_id"] for row in final_rows}
        else:
            final_ids = set()
        theory_rows = conn.execute(
            "SELECT g.strategy_id AS strategy_id, g.theory_sha256 AS theory_sha256 "
            "  FROM research_generated_candidates g "
            "  JOIN research_strategies s ON s.id = g.strategy_id AND s.status = 'active' "
            " WHERE g.decision = 'accepted';"
        ).fetchall()

    marks: dict[int, dict] = {}
    for strategy_id in final_ids:
        marks.setdefault(strategy_id, {})["final"] = True
    for row in theory_rows:
        marks.setdefault(row["strategy_id"], {})["theory_sha"] = row["theory_sha256"]
    # The strategies_marked_final gauge: this call's own fresh count, never accumulated.
    GENERATOR_MONITOR_GAUGES["strategies_marked_final"] = len(final_ids)
    return {str(strategy_id): entry for strategy_id, entry in marks.items()}


# ---- PREVIEW, RUN GENERATOR, the job and the worker read (U.4 second half, lane G2) -----------
#
# UI plan 2.3 ("PREVIEW, the gate"): PREVIEW runs the whole loop with write=false and shows the
# would-be candidates; RUN GENERATOR writes only the pairs Tamer ticked, and refuses unless a
# succeeded PREVIEW already exists for the theory's CURRENT (newest-saved) content and every
# ticked pair was accepted in it. Both are the SAME job kind, run_generator, told apart only by
# the payload's own `write` flag: one job, one worker handler, one relay route.


def _newest_theory_version(theory_id: str) -> "dict | None":
    """The newest saved version of theory_id, or None when it has never been saved at all.

    Reuses researchdb.list_theories() -- already returned newest-created-first, the exact
    ordering get_theories() above groups from -- rather than a second, independently-ordered
    query that could drift from it.
    """
    for row in researchdb.list_theories():
        if row["theory_id"] == theory_id:
            return row
    return None


def _insert_run_generator_job(theory_id: str, theory_sha256: str, *, write: bool,
                               selected: "list[list[str]] | None") -> int:
    """The job-insert idiom routes/retest.py's own dispatch route already uses verbatim
    (a bare INSERT INTO research_jobs inside researchdb.transaction(), never a second helper in
    researchdb.py, which is this lane's stop condition): one pending run_generator job, its
    payload the four fields the worker handler and the /facts relay both read back.
    """
    now = int(time.time())
    payload = {
        "theory_sha256": theory_sha256,
        "theory_id": theory_id,
        "write": write,
        "selected": selected,
    }
    with researchdb.transaction() as conn:
        job_row = conn.execute(
            "INSERT INTO research_jobs (kind, status, payload_json, created_at) "
            "VALUES ('run_generator', 'pending', ?, ?) RETURNING id;",
            (json.dumps(payload), now),
        ).fetchone()
        return int(job_row["id"])


@router.post("/theories/{theory_id}/preview")
def post_theory_preview(theory_id: str):
    """PREVIEW: a run_generator job with write=false over theory_id's own NEWEST saved version.
    404 when theory_id has never been saved at all (there is nothing to preview)."""
    row = _newest_theory_version(theory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no theory found for the given theory_id")
    job_id = _insert_run_generator_job(theory_id, row["sha256"], write=False, selected=None)
    return {"job_id": job_id}


class GenerateBody(BaseModel):
    selected: "list[list[str]]"  # [[skeleton_id, filler_id], ...], the ticked accepted rows


@router.post("/theories/{theory_id}/generate")
def post_theory_generate(theory_id: str, body: GenerateBody):
    """RUN GENERATOR: a run_generator job with write=true, restricted to `selected`. Refuses by
    name (never a job dispatched on a guess): 404 theory_id never saved; 409 preview-required when
    no succeeded preview exists for theory_id's CURRENT sha, or one exists but does not accept
    every selected pair; 409 preview-stale when a succeeded preview exists but for an OLDER saved
    version than theory_id's current one (the theory was edited after that preview ran).
    """
    row = _newest_theory_version(theory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no theory found for the given theory_id")
    theory_sha256 = row["sha256"]
    selected_pairs = [(pair[0], pair[1]) for pair in body.selected]

    with researchdb.get_conn() as conn:
        preview_row = conn.execute(
            "SELECT result_json, json_extract(payload_json, '$.theory_sha256') AS theory_sha256 "
            "  FROM research_jobs "
            " WHERE kind = 'run_generator' AND status = 'succeeded' "
            "   AND json_extract(payload_json, '$.theory_id') = ? "
            "   AND json_extract(payload_json, '$.write') = 0 "
            " ORDER BY id DESC LIMIT 1;",
            (theory_id,),
        ).fetchone()
    if preview_row is None:
        raise HTTPException(status_code=409, detail="preview-required")
    if preview_row["theory_sha256"] != theory_sha256:
        raise HTTPException(status_code=409, detail="preview-stale")

    outcomes = json.loads(preview_row["result_json"] or "[]")
    accepted_pairs = {
        (outcome["skeleton_id"], outcome["filler_id"])
        for outcome in outcomes if outcome["decision"] == "accepted"
    }
    if not selected_pairs or any(pair not in accepted_pairs for pair in selected_pairs):
        raise HTTPException(status_code=409, detail="preview-required")

    job_id = _insert_run_generator_job(
        theory_id, theory_sha256, write=True,
        selected=[list(pair) for pair in selected_pairs],
    )
    return {"job_id": job_id}


# The four job-state words the page renders (UI plan 2.3). Round 2 (G2-F2): the vocabulary of
# research_jobs.status values this router understands is CLOSED to exactly the five keys below.
# A status word outside that set is a genuine server-side bug (a status this router was never told
# about was written to the row), never something to paper over, so get_generator_job raises a
# named 500 instead of silently answering QUEUED for a status that may not be queued at all.
_JOB_STATE_WORDS: "dict[str, str]" = {
    "pending": "QUEUED",
    "picked_up": "RUNNING",
    "succeeded": "DONE",
    "failed": "FAILED",
    "cancelled": "FAILED",
}


def generator_jobs_by_status() -> "dict[str, int]":
    """Monitor row generator_jobs_by_status (docs/units/G2/monitor_rows.md): a live count of
    every run_generator job grouped by its durable research_jobs.status. Plain read-derived state
    (matching tracker.py's own query_* functions), never a process counter, because the fact this
    row watches (how many jobs are stuck pending, how many failed) already lives durably in the
    table and does not need a second in-memory copy that a restart would lose."""
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM research_jobs WHERE kind = 'run_generator' GROUP BY status;"
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}


@router.get("/jobs/{job_id}")
def get_generator_job(job_id: int):
    """The job's state word and, once DONE, its outcome list (one entry per (skeleton, filler)
    pair: decision, reason, strategy_id, strategy_name, candidate_id). 404 for an id that is not a
    run_generator job at all (an unknown id, or a real job of some other kind)."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT id, kind, status, error, result_json FROM research_jobs WHERE id = ?;",
            (job_id,),
        ).fetchone()
    if row is None or row["kind"] != "run_generator":
        raise HTTPException(status_code=404, detail="no run_generator job found for the given id")
    error = row["error"]
    if row["status"] == "cancelled" and error is None:
        error = "cancelled"
    outcomes = json.loads(row["result_json"]) if row["result_json"] else None
    # Round 2 (G2-F2): no fallback. A status word this router does not recognise is refused by
    # name with a 500, naming the job id and the exact unrecognised status, rather than silently
    # reported as QUEUED, which the hard prohibition on fallbacks forbids doing here.
    if row["status"] not in _JOB_STATE_WORDS:
        raise HTTPException(
            status_code=500,
            detail=f"generator-job-status-unrecognised: job {job_id} has status {row['status']!r}",
        )
    return {
        "id": row["id"],
        "state": _JOB_STATE_WORDS[row["status"]],
        "error": error,
        "outcomes": outcomes,
    }


@router.get("/worker")
def get_generator_worker_status():
    """'online' or 'offline', read live from the real any_worker_online() reader (the same one
    the full-metrics route already uses for its own worker-availability state), never a fixture
    copy of the answer."""
    return {"status": "online" if researchdb.any_worker_online() else "offline"}


def _fail_generator_job(job_id: int, error: str, now: int) -> None:
    """Mark a run_generator job terminally failed from directly inside the /facts handler, never
    bounced back through the worker's own generic /complete route: every failure this function
    reports happens strictly AFTER the worker's own relay already succeeded, so the worker has
    nothing left to report and the backend, which already has database access here, writes the
    terminal state itself."""
    with researchdb.transaction() as conn:
        conn.execute(
            "UPDATE research_jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?;",
            (now, error, job_id),
        )


class GeneratorFactsIn(BaseModel):
    """The worker's own relay body: compile_facts.py's printed JSON object, unchanged
    (research_lab.generator.facts.load_facts' own three required top-level keys)."""
    image: str
    vbt_version: str
    fillers: dict


@worker_router.post("/jobs/{job_id}/facts")
async def post_generator_job_facts(job_id: int, body: GeneratorFactsIn):
    """The worker's relay of compile_facts.py's own compiled-facts JSON (worker-scoped: the
    worker never imports vectorbtpro and never runs the generation loop itself). This route runs
    the host-side loop -- research_lab.generator.loop.generate, in a thread so the event loop is
    never blocked by it -- and marks the job terminal itself (succeeded, or failed by name on a
    stale/mismatched facts file), because only this handler knows the real per-pair outcome.
    404 unknown job; 409 a job id that names some other kind.

    Set 16b lane WD (#423) AC-2: moved from the browser-prefixed /api/research/generator router to
    worker_router (/api/research/worker/generator), which is the ONLY prefix production's Caddy
    lets a non-human caller reach; the old path no longer exists. Behaviour is otherwise unchanged,
    and require_worker_api_key now applies at the router level rather than a per-route decorator.
    """
    now = int(time.time())
    with researchdb.get_conn() as conn:
        job_row = conn.execute(
            "SELECT id, kind, payload_json FROM research_jobs WHERE id = ?;", (job_id,),
        ).fetchone()
    if job_row is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job_row["kind"] != "run_generator":
        raise HTTPException(status_code=409, detail=f"job {job_id} is not a run_generator job")

    payload = json.loads(job_row["payload_json"])
    theory_id = payload["theory_id"]
    theory_sha256 = payload["theory_sha256"]
    write = bool(payload["write"])
    selected = None
    if payload.get("selected") is not None:
        selected = {(pair[0], pair[1]) for pair in payload["selected"]}

    theory_row = researchdb.get_theory_by_sha(theory_sha256)
    if theory_row is None:
        _fail_generator_job(job_id, f"theory content not found for sha256={theory_sha256}", now)
        return {"ok": True}

    tmp_dir = Path(tempfile.mkdtemp(prefix="generator-facts-"))
    try:
        theory_path = tmp_dir / f"{theory_id}.md"
        theory_path.write_text(theory_row["content"], encoding="utf-8")
        try:
            theory = generator_theory.load_theory(theory_path)
        except generator_theory.TheoryLoadError as exc:
            _fail_generator_job(job_id, f"theory reload failed: {exc}", now)
            return {"ok": True}

        facts_path = tmp_dir / "facts.json"
        facts_path.write_text(json.dumps(body.model_dump()), encoding="utf-8")
        try:
            facts = generator_facts.load_facts(facts_path, required_filler_ids=theory.fillers)
        except generator_facts.FactsLoadError as exc:
            _fail_generator_job(job_id, f"facts load failed: {exc}", now)
            return {"ok": True}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    outcomes = await asyncio.to_thread(
        generator_loop.generate, theory, facts, write=write, selected=selected)

    if not write:
        # generator_preview_writes (docs/units/G2/monitor_rows.md): an accepted/refused outcome
        # under write=False carries a candidate_id only via the pre-existing-row 'skipped' shape
        # (loop.generate_one's own write=False branches never call either write helper), so a
        # nonzero count here means exactly what the monitor row says: a write happened where the
        # PREVIEW gate should have prevented one.
        preview_writes = sum(
            1 for o in outcomes if o.decision in ("accepted", "refused") and o.candidate_id is not None
        )
        GENERATOR_MONITOR_COUNTERS["preview_writes"] += preview_writes

    # filler_compile_refused (AC-12): once per outcome refused at the compile step itself
    # (research_lab/generator/compile_facts.py's own per-filler isolation, AC-7), counted in
    # PREVIEW and RUN GENERATOR alike, so a filler this build can never compile is visible.
    GENERATOR_MONITOR_COUNTERS["filler_compile_refused"] += sum(
        1 for o in outcomes
        if o.decision == "refused" and (o.reason or "").startswith("filler_compile_failed:")
    )

    result = [
        {
            "skeleton_id": o.skeleton_id,
            "filler_id": o.filler_id,
            "decision": o.decision,
            "reason": o.reason,
            "reason_words": _job_outcome_reason_words(o.decision, o.reason),
            "strategy_id": o.strategy_id,
            "strategy_name": o.strategy_name,
            "candidate_id": o.candidate_id,
            "expression": o.expression,
        }
        for o in outcomes
    ]
    with researchdb.transaction() as conn:
        conn.execute(
            "UPDATE research_jobs SET status = 'succeeded', finished_at = ?, result_json = ? "
            "WHERE id = ?;",
            (now, json.dumps(result), job_id),
        )
    return {"ok": True}
