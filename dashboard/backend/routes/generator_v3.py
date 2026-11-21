"""Generator v3 routes (lane GV3-W2, DECISIONS #467): what the Generator page needs to save a template, preview what it makes,
approve it and start the searches, and read the tree while it runs (template, theories, each theory's runs, and once its search is
done its rounds, kept strategies and pick).

Browser routes with no auth dependency, like routes/generator.py's `router`: Caddy's basic auth guards them at the edge.
Every query opens its own connection at call time and reads the database path from researchdb.DB_PATH, so a probe can point the
module at a scratch file. The tables are gendb.py's; the runs are researchdb's; the artifacts are artifact_store.store's.

Every refusal is a plain sentence: no snake_case word, no class name, no trace. A route never lets a raw exception text out; the
refusals it knows are named below and turned into a sentence, anything else is a bug and surfaces as the server's own 500.

The stages after the search (optimise, walk-forward, the judge) are lane GV3-W3; the final test is lane GV3-F.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from research_lab.theories import form, kept_back
from research_lab.theory_search import narrow, sample

from .. import artifact_store, gendb, researchdb, reservedb, stage_runner, tracker

router = APIRouter(prefix="/api/research/generator/v3", tags=["generator-v3"])

# The search settings GV2-5b measured on SYN_VOL (research_lab/theory_e2e/run.py, SEARCH_N_ITER and SEARCH_ONC_CAP: "ONC cap 10,
# n_iter 100,000"), and its costs and cash (COSTS, INIT_CASH there; fees "alpaca" is DECISIONS #456, slippage 0.0003 GV2-5b's value).
# A round's own size and limits are the theory's [search] table (DECISIONS #470), not set here.
SEARCH_N_ITER = 100000
SEARCH_ONC_CAP = 10
COSTS = {"fees": "alpaca", "slippage": 0.0003}
INIT_CASH = 100000.0

PARIS = ZoneInfo("Europe/Paris")  # the same zone routes/ops_systems.py shows days in; not imported from there, which pulls in the monitor store

_GROUP_LABEL =re.compile(r"^GROUP_[A-Z0-9_]+$")  # researchdb.check_theory_dispatch's rule for the group's name
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SNAKE_WORD = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")

# The refusals a dispatch can make by itself, each turned into a sentence naming the theory.
_DISPATCH_REFUSALS = (researchdb.TheoryRunInvalid, stage_runner.UnknownStageWord, stage_runner.CandidateVersionMoved,
                      stage_runner.DailyStoreNotConfigured, stage_runner.LeadInUnavailable, stage_runner.WindowTooSmallForScheme,
                      stage_runner.WarmupUnsatisfied, stage_runner.LookbackNotDeclared, tracker.ChronologyRefused,
                      tracker.DrawWindowMissing, tracker.ReserveCutCrossed, reservedb.ReserveError)


class SaveTemplateBody(BaseModel):
    content: object = None


class PreviewBody(BaseModel):
    universe_id: object = None
    seed: object = None
    optimise_window: object = None
    later_window: object = None


class ApproveBody(PreviewBody):
    group_label: object = None
    calendar_id: object = None
    data_start: object = None


# ---- plain sentences ---------------------------------------------------------------------------------------------------------

def _plain(text: str) -> str:
    """A refusal's text with every snake_case word said in plain words (optimise_window becomes optimise window)."""
    return _SNAKE_WORD.sub(lambda m: m.group(0).replace("_", " "), str(text))


def _part_in_words(part: str) -> str:
    return part.replace(".*", " each").replace(".", " ").replace("_", " ")


def _refuse(status: int, sentence: str) -> HTTPException:
    return HTTPException(status_code=status, detail=_plain(sentence))


def _db_path():
    return researchdb.DB_PATH


def _template(sha256: str) -> dict:
    try:
        return gendb.get_template(_db_path(), sha256)
    except gendb.TemplateNotFound as exc:
        raise _refuse(404, "No saved template has that fingerprint. Save the template first.") from exc


# ---- the body's values, checked by hand so a wrong one is a sentence and not a list of field errors ----------------------------

def _whole_number(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse(422, f"The {label} must be a whole number.")
    return value


def _day(value, label: str) -> str:
    if not isinstance(value, str) or not _DATE.match(value):
        raise _refuse(422, f"The {label} must be a date written like 2017-01-03.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise _refuse(422, f"The {label} is not a real date: {value}.") from exc
    return value


def _window(value, label: str) -> tuple:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _refuse(422, f"The {label} must be a start date and an end date.")
    start, end = _day(value[0], label + " start"), _day(value[1], label + " end")
    if start > end:
        raise _refuse(422, f"The {label} starts on {start}, after it ends on {end}.")
    return start, end


def _now() -> int:
    return int(time.time())


def _ts_day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _group(universe_id) -> dict:
    """The stock group (a price universe) with its members and its test window, or a 422 in a sentence."""
    universe_id = _whole_number(universe_id, "stock group number")
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, display_name, window_start_ts, window_end_ts FROM research_datasets WHERE id = ? AND dataset_type = ? AND status != 'deleted';",
            (universe_id, researchdb.DATASET_TYPE_PRICE_UNIVERSE)).fetchone()
    if row is None:
        raise _refuse(422, f"There is no stock group with number {universe_id}.")
    if row["window_start_ts"] is None or row["window_end_ts"] is None:
        raise _refuse(422, f"The stock group {row['name']} has no test window, so its screen years are unknown.")
    members = researchdb.list_price_universe_members(universe_id)
    return {"id": universe_id, "name": row["name"], "display_name": row["display_name"], "members": members,
            "screen": (_ts_day(row["window_start_ts"]), _ts_day(row["window_end_ts"]))}


def _split(members: list, seed: int) -> tuple:
    """(searched, kept back): the same draw approve records."""
    try:
        kept = kept_back.draw_kept_back(members, seed)
    except ValueError as exc:
        raise _refuse(422, "The stock group is too small: " + str(exc) + ".") from exc
    return [m for m in members if m not in kept], kept


def _day_after(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _day_before(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _reserve_cut_day() -> str:
    try:
        return _ts_day(reservedb.read_reserve_cut_date_ts())
    except reservedb.ReserveError as exc:
        raise HTTPException(status_code=500, detail="The reserve start date is not set, so the walk-forward and later years cannot be checked.") from exc


def walkforward_years(optimise_end: str) -> list:
    """The walk-forward years: the day after the optimise window ends to the day before the reserve starts (the walk-forward never
    reads the reserve, and the later window starts on or after the cut). They can be empty: the start is then after the end."""
    return [_day_after(optimise_end), _day_before(_reserve_cut_day())]


def _years(group: dict, optimise: tuple, later: tuple) -> dict:
    """The years the four parts of a run use, checked in order: the optimise window after the screen years, the later window after
    the optimise window, the optimise window before the reserve cut, the later window on or after it, the walk-forward years not empty."""
    cut = _reserve_cut_day()
    screen = group["screen"]
    if not screen[1] < optimise[0]:
        raise _refuse(422, f"The optimise window must start after the screen years end on {screen[1]}.")
    if not optimise[1] < later[0]:
        raise _refuse(422, f"The later window must start after the optimise window ends on {optimise[1]}.")
    if not optimise[1] < cut:
        raise _refuse(422, f"The optimise window must end before the reserve starts on {cut}; it ends on {optimise[1]}.")
    if later[0] < cut:
        raise _refuse(422, f"The later window must start on or after the reserve starts on {cut}; it starts on {later[0]}.")
    walkforward = walkforward_years(optimise[1])
    if walkforward[0] > walkforward[1]:
        raise _refuse(422, f"There are no walk-forward years: they would run from {walkforward[0]} to {walkforward[1]}. "
                           f"End the optimise window earlier than {optimise[1]}.")
    return {"screen": list(screen), "optimise": list(optimise), "walkforward": walkforward, "later": list(later)}


def _checked_inputs(body: PreviewBody) -> dict:
    group = _group(body.universe_id)
    seed = _whole_number(body.seed, "seed")
    optimise = _window(body.optimise_window, "optimise window")
    later = _window(body.later_window, "later window")
    searched, kept = _split(group["members"], seed)
    return {"group": group, "seed": seed, "optimise": optimise, "later": later, "searched": searched, "kept_back": kept,
            "years": _years(group, optimise, later)}


# ---- templates ---------------------------------------------------------------------------------------------------------------

def _theory_count(content: str) -> int:
    return len(gendb.expand_preview(content))


@router.get("/templates")
def get_templates() -> list:
    out = []
    for row in gendb.list_templates(_db_path()):
        content = gendb.get_template(_db_path(), row["sha256"])["content"]
        out.append({"name": row["name"], "display_name": row["display_name"], "sha256": row["sha256"], "saved_at": row["created_at"],
                    "theory_count": _theory_count(content)})
    return out


@router.post("/templates")
def post_template(body: SaveTemplateBody) -> dict:
    if not isinstance(body.content, str) or not body.content.strip():
        raise _refuse(422, "Send the template text as content.")
    try:
        saved = gendb.save_template(_db_path(), body.content, "editor", int(time.time()))
    except form.TheoryFormError as exc:
        raise _refuse(422, f"The template is not valid: {exc.detail} (in {_part_in_words(exc.part)}).") from exc
    return {"name": saved["name"], "display_name": saved["display_name"], "sha256": saved["sha256"], "created": saved["created"]}


@router.post("/templates/{sha256}/preview")
def post_preview(sha256: str, body: PreviewBody) -> dict:
    """What an approve would make, with no write: the theories, the stocks split and the years."""
    template = _template(sha256)
    inputs = _checked_inputs(body)
    try:
        theories = gendb.expand_preview(template["content"])
    except form.TheoryFormError as exc:
        raise _refuse(422, f"The template is not valid: {exc.detail} (in {_part_in_words(exc.part)}).") from exc
    out = []
    for theory in theories:
        loaded = form.loads(theory["theory_toml"], theory["name"])
        letters = sample.letters_and_rules(loaded)[0]
        search = loaded.data.get("search")
        round_one = None
        if search is not None:
            box = narrow.first_box(loaded, search["values_per_number"])
            round_one = {letter: {"count": len(axis.values), "from": float(axis.lo), "to": float(axis.hi), "step": float(axis.step)}
                         for letter, axis in box.items()}
        out.append({"name": theory["name"], "display_name": theory["display_name"], "choices": theory["choices"], "letters": letters,
                    "values_per_number": None if search is None else search["values_per_number"], "round_one": round_one})
    return {"template": {"name": template["name"], "display_name": template["display_name"], "sha256": template["sha256"]},
            "theories": out,
            "stocks": {"searched": inputs["searched"], "kept_back": inputs["kept_back"]},
            "years": inputs["years"],
            "run_time_estimate": None}  # nothing measures a run's time yet; the page says so


# ---- approve and resume ------------------------------------------------------------------------------------------------------

def _start_searches(batch_id: int) -> list:
    """Dispatch the search of every theory in the batch that has no screen run yet, in order, linking each run as it is made.
    A refused dispatch stops here with a sentence naming the theory; the runs made before it stay linked."""
    tree = gendb.read_tree(_db_path(), batch_id)
    batch = tree["batch"]
    todo = [t for t in tree["theories"] if not any(r["stage"] == "screen" for r in t["runs"])]
    if not todo:
        raise _refuse(422, f"There is nothing to resume: every theory in batch {batch_id} already has its search.")
    run_ids = []
    for theory in todo:
        now = int(time.time())
        search = {"seed": batch["seed"], "n_iter": SEARCH_N_ITER, "onc_cap": SEARCH_ONC_CAP,
                  "optimise_window": [batch["optimise_start"], batch["optimise_end"]],
                  "final": {"kept_back": batch["kept_back"], "later_window": [batch["later_start"], batch["later_end"]]}}
        try:
            result = stage_runner.dispatch_theory_stage(
                "screen", batch["universe_id"], now, calendar_id=batch["calendar_id"], theory_text=theory["theory_toml"],
                group_label=batch["group_label"], data_start=batch["data_start"], search=search, costs=dict(COSTS), init_cash=INIT_CASH)
        except _DISPATCH_REFUSALS as exc:
            raise _refuse(422, f"The search for {theory['display_name']} was refused: {exc}. Batch {batch_id} is saved with the searches "
                               f"made before it; start the rest from the batch.") from exc
        gendb.record_theory_run(_db_path(), theory["id"], "screen", result["run_id"], now)
        run_ids.append(result["run_id"])
    return run_ids


def _short_day(ts: int) -> str:
    """The Paris day of a time written like 6 Nov (no leading zero)."""
    d = datetime.fromtimestamp(ts, PARIS)
    return f"{d.day} {d:%b}"


def _searched_group_name(group_name: str, template_name: str, now: int) -> str:
    """'<group>, <template> search, <D Mon>', with ' (n)' (the smallest n >= 2 not held) when a price universe, a deleted one too,
    already holds that name. One read; two approves at the same moment may still pick the same number (accepted)."""
    base = f"{group_name}, {template_name} search, {_short_day(now)}"
    with researchdb.get_conn() as conn:
        held = {r["display_name"] for r in conn.execute(
            "SELECT display_name FROM research_datasets WHERE dataset_type = ? AND display_name IS NOT NULL AND substr(display_name, 1, ?) = ?;",
            (researchdb.DATASET_TYPE_PRICE_UNIVERSE, len(base), base)).fetchall()}
    if base not in held:
        return base
    n = 2
    while f"{base} ({n})" in held:
        n += 1
    return f"{base} ({n})"


@router.post("/templates/{sha256}/approve")
def post_approve(sha256: str, body: ApproveBody) -> dict:
    template = _template(sha256)
    inputs = _checked_inputs(body)
    group_label = body.group_label
    if not isinstance(group_label, str) or not _GROUP_LABEL.match(group_label):
        raise _refuse(422, "The group label must be capital letters, digits and underscores, starting with GROUP_ (for example GROUP_SYN_VOL).")
    calendar_id = body.calendar_id
    if not isinstance(calendar_id, str) or not calendar_id.strip():
        raise _refuse(422, "The calendar name must be a non-empty word or phrase.")
    data_start = _day(body.data_start, "data start")
    if any(b["calendar_id"] == calendar_id for b in gendb.list_batches(_db_path())):
        raise _refuse(422, f"The calendar {calendar_id} is already used by another batch. Each batch needs its own calendar name.")
    group, searched = inputs["group"], inputs["searched"]
    window_start_ts, window_end_ts = (researchdb._date_to_ts(d) for d in group["screen"])
    now = _now()
    searched_name = None
    if group["display_name"] is not None:
        searched_name = _searched_group_name(group["display_name"], template["display_name"], now)
    try:
        created = researchdb.create_price_universe(f"{group_label} searched {calendar_id}", searched, window_start_ts, window_end_ts,
                                                   display_name=searched_name)
    except researchdb.PriceUniverseNameExists as exc:
        raise _refuse(422, f"A stock group called {group_label} searched {calendar_id} already exists. Use another calendar name.") from exc
    except researchdb.StrategyValidationError as exc:
        base_name = f"{group['display_name']}, {template['display_name']} search, {_short_day(now)}"
        raise _refuse(422, f"The searched group's name {base_name} cannot be used: {exc}.") from exc
    except (researchdb.PriceUniverseEmptyError, researchdb.PriceUniverseMixedSource, researchdb.PriceUniverseWindowInvalid,
            researchdb.PriceUniverseWindowRequired) as exc:
        raise _refuse(422, f"The searched stocks could not be made into a group: {exc}.") from exc
    try:
        batch_id = gendb.record_batch(
            _db_path(), template_sha256=template["sha256"], universe_id=group["id"], searched_universe_id=created["universe_id"],
            group_label=group_label, members=group["members"], seed=inputs["seed"], data_start=data_start, screen=group["screen"],
            optimise=inputs["optimise"], later=inputs["later"], calendar_id=calendar_id, now=now)
    except sqlite3.IntegrityError as exc:
        raise _refuse(422, f"The calendar {calendar_id} is already used by another batch. Each batch needs its own calendar name.") from exc
    except form.TheoryFormError as exc:
        raise _refuse(422, f"The template is not valid: {exc.detail} (in {_part_in_words(exc.part)}).") from exc
    return {"batch_id": batch_id, "run_ids": _start_searches(batch_id)}


# ---- pause, resume and stop (GV3-W4) -----------------------------------------------------------------------------------------

def paris_day_words(ts: int) -> str:
    """The Paris day of a time written like 31 Oct 2026."""
    d = datetime.fromtimestamp(ts, PARIS)
    return f"{d.day} {d:%b %Y}"


def cancel_queued(theories: list) -> tuple:
    """Cancel every linked run of these theories that has not started: (cancelled run ids, still-running run ids). A pending run (a
    claimed theory job included) goes to researchdb.request_cancel on its job. 'cancelled' counts as cancelled, 'unsupported' (a
    running theory job, which the worker cannot be told to kill) as still running, 'already' as neither. Any other answer is a bug."""
    cancelled, still_running = [], []
    for theory in theories:
        for link in theory["runs"]:
            run = researchdb.get_run(link["run_id"])
            if run is None or run["status"] not in ("pending", "running"):
                continue
            answer = researchdb.request_cancel(run["job_id"])
            action = answer["action"]
            if action == "cancelled":
                cancelled.append(link["run_id"])
            elif action == "unsupported":
                still_running.append(link["run_id"])
            elif action != "already":
                raise RuntimeError(f"cancelling run {link['run_id']} (job {run['job_id']}) answered {action!r}, which this route does not know")
    return cancelled, still_running


def _no_batch(batch_id: int) -> HTTPException:
    return _refuse(404, f"There is no batch with number {batch_id}.")


@router.post("/batches/{batch_id}/pause")
def post_pause(batch_id: int) -> dict:
    try:
        control = gendb.record_batch_control(_db_path(), batch_id, "pause", int(time.time()))
    except gendb.BatchNotFound as exc:
        raise _no_batch(batch_id) from exc
    except gendb.BatchControlRefused as exc:
        if exc.state == "paused":
            raise _refuse(409, f"Batch {batch_id} is already paused.") from exc
        raise _refuse(409, f"Batch {batch_id} was stopped on {paris_day_words(exc.since)}. A stopped batch cannot be paused.") from exc
    return {"batch_id": batch_id, "state": control["state"], "since": control["since"]}


@router.post("/batches/{batch_id}/stop")
def post_stop(batch_id: int) -> dict:
    try:
        control = gendb.record_batch_control(_db_path(), batch_id, "stop", int(time.time()))
    except gendb.BatchNotFound as exc:
        raise _no_batch(batch_id) from exc
    except gendb.BatchControlRefused as exc:
        raise _refuse(409, f"Batch {batch_id} was already stopped on {paris_day_words(exc.since)}.") from exc
    theories = [t for t in gendb.read_tree(_db_path(), batch_id)["theories"] if t["stop"] is None]
    cancelled, still_running = cancel_queued(theories)
    return {"batch_id": batch_id, "state": control["state"], "since": control["since"],
            "cancelled_runs": cancelled, "still_running": still_running}


@router.post("/batches/{batch_id}/resume")
def post_resume(batch_id: int) -> dict:
    try:
        tree = gendb.read_tree(_db_path(), batch_id)
    except gendb.BatchNotFound as exc:
        raise _no_batch(batch_id) from exc
    control = gendb.batch_controls(_db_path()).get(batch_id, {"state": "running", "since": None})
    if control["state"] == "stopped":
        raise _refuse(409, f"Batch {batch_id} was stopped on {paris_day_words(control['since'])}. A stopped batch cannot be resumed.")
    if control["state"] == "paused":
        try:
            gendb.record_batch_control(_db_path(), batch_id, "resume", int(time.time()))
        except gendb.BatchControlRefused as exc:  # another click changed the state between the read and the write
            if exc.state == "stopped":
                raise _refuse(409, f"Batch {batch_id} was stopped on {paris_day_words(exc.since)}. A stopped batch cannot be resumed.") from exc
            raise _refuse(409, f"Batch {batch_id} is not paused.") from exc
        if not any(not any(r["stage"] == "screen" for r in t["runs"]) for t in tree["theories"]):
            return {"batch_id": batch_id, "state": "running", "run_ids": []}
    try:
        run_ids = _start_searches(batch_id)
    except gendb.BatchNotFound as exc:
        raise _no_batch(batch_id) from exc
    return {"batch_id": batch_id, "state": "running", "run_ids": run_ids}


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: int) -> dict:
    """Remove a stopped batch from the list (GD1a): one appended row, nothing deleted."""
    try:
        done = gendb.remove_batch(_db_path(), batch_id, int(time.time()))
    except gendb.BatchNotFound as exc:
        raise _no_batch(batch_id) from exc
    except gendb.BatchRemoveRefused as exc:
        if exc.word == "not-stopped":
            raise _refuse(409, f"Batch {batch_id} is not stopped. Stop it first.") from exc
        if exc.word == "in-flight":
            raise _refuse(409, f"Batch {batch_id} still has {len(exc.names)} runs going. Wait for them to end.") from exc
        raise _refuse(409, f"Batch {batch_id} has strategies the judge graded ({', '.join(exc.names)}). Their record stays on this page.") from exc
    return {"batch_id": done["batch_id"], "removed_at": done["removed_at"]}


# ---- the batches and the tree ------------------------------------------------------------------------------------------------

_UNNAMED_GROUP = "an unnamed stock group"


def _batch_name(batch: dict, facts: dict, template_names: dict) -> str:
    """The searched group's display name when set, else '<source group name or an unnamed stock group>, <template> search, <D Mon>'."""
    own = facts.get(batch["searched_universe_id"], {}).get("display_name")
    if own is not None:
        return own
    source = facts.get(batch["universe_id"], {}).get("display_name") or _UNNAMED_GROUP
    return f"{source}, {template_names[batch['template_sha256']]} search, {_short_day(batch['approved_at'])}"


def _public_batch(batch: dict, control: dict, synthetic: bool, display_name: str) -> dict:
    out = {k: v for k, v in batch.items() if k not in ("members_json", "kept_back_json")}
    out["control"] = control
    out["synthetic"] = synthetic
    out["display_name"] = display_name
    out["walkforward"] = walkforward_years(batch["optimise_end"])
    return out


def _universe_facts(universe_ids: list) -> dict:
    """{id: {"synthetic": bool, "display_name": str or None}} for these dataset ids, in one SELECT. A missing row is not listed."""
    if not universe_ids:
        return {}
    marks = ", ".join("?" * len(universe_ids))
    with researchdb.get_conn() as conn:
        rows = conn.execute(f"SELECT id, source, display_name FROM research_datasets WHERE id IN ({marks});", tuple(universe_ids)).fetchall()
    return {r["id"]: {"synthetic": r["source"] == "synthetic", "display_name": r["display_name"]} for r in rows}


_RUNNING = {"state": "running", "since": None}


@router.get("/batches")
def get_batches(include_synthetic: bool = False, include_removed: bool = False) -> list:
    batches = [b for b in gendb.list_batches(_db_path()) if include_removed or b["removed_at"] is None]
    facts = _universe_facts(sorted({u for b in batches for u in (b["universe_id"], b["searched_universe_id"])}))
    templates = gendb.template_names(_db_path()) if batches else {}
    controls = gendb.batch_controls(_db_path())
    return [_public_batch(b, controls.get(b["id"], dict(_RUNNING)), facts.get(b["universe_id"], {}).get("synthetic", False),
                          _batch_name(b, facts, templates))
            for b in batches if include_synthetic or not facts.get(b["universe_id"], {}).get("synthetic", False)]


@router.get("/trials-per-day")
def get_trials_per_day(include_synthetic: bool = False) -> dict:
    return trials_per_day(int(time.time()), include_synthetic)


def trials_per_day(now: int, include_synthetic: bool) -> dict:
    """How many strategies the searches tried on each Paris day, from the stored trial-family rows: {"time_zone", "today", "days":
    [{"day", "trials", "searches"}]}, from the day of the oldest counted family to today, a day with none shown as zeros, oldest first.
    Empty days when no family is counted. A family whose run is gone still counts; one whose run read synthetic stocks is left out
    unless include_synthetic."""
    synthetic_clause = "" if include_synthetic else f"WHERE {researchdb._NOT_SYNTHETIC_RUN_PRED} "
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT f.n_trials AS n_trials, f.created_at AS created_at FROM research_trial_families f "
            f"LEFT JOIN research_runs r ON r.id = f.run_id {synthetic_clause};").fetchall()
    today = datetime.fromtimestamp(now, PARIS).date()
    counts: dict = {}
    for row in rows:
        day = datetime.fromtimestamp(row["created_at"], PARIS).date()
        trials, searches = counts.get(day, (0, 0))
        counts[day] = (trials + row["n_trials"], searches + 1)
    days = []
    if counts:
        day = min(counts)
        while day <= max(today, max(counts)):
            trials, searches = counts.get(day, (0, 0))
            days.append({"day": day.isoformat(), "trials": trials, "searches": searches})
            day += timedelta(days=1)
    return {"time_zone": "Europe/Paris", "today": today.isoformat(), "days": days}


def _read_artifact(run_id: int, name: str, theory_name: str):
    """One JSON file of a finished search, from the artifact store. A file that is missing or unreadable is a 500 naming the run and file."""
    try:
        with artifact_store.store.open(run_id, name) as handle:
            return json.load(handle)
    except artifact_store.ArtifactNotFound as exc:
        raise HTTPException(status_code=500, detail=f"The search for {theory_name} (run {run_id}) is marked done but its file {name} is missing.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"The search for {theory_name} (run {run_id}) has a file {name} that cannot be read.") from exc


@router.get("/batches/{batch_id}/tree")
def get_tree(batch_id: int) -> dict:
    try:
        tree = gendb.read_tree(_db_path(), batch_id)
    except gendb.BatchNotFound as exc:
        raise _refuse(404, f"There is no batch with number {batch_id}.") from exc
    theories = []
    for theory in tree["theories"]:
        runs = []
        for link in theory["runs"]:
            run = researchdb.get_run(link["run_id"])
            runs.append({"stage": link["stage"], "run_id": link["run_id"], "status": run["status"] if run is not None else "missing"})
        search, final = None, "not run"
        screen = next((r for r in runs if r["stage"] == "screen"), None)
        if screen is not None and screen["status"] == "succeeded":
            run_id = screen["run_id"]
            search = {"rounds": _read_artifact(run_id, "rounds.json", theory["display_name"]),
                      "finalists": _read_artifact(run_id, "finalists.json", theory["display_name"]),
                      "pick": _read_artifact(run_id, "pick.json", theory["display_name"])}
            if artifact_store.store.exists(run_id, "final.json"):
                final = _read_artifact(run_id, "final.json", theory["display_name"])
        saved = []
        for row in theory["saved"]:
            entry = {"rank": row["rank"], "display_name": _plain(row["display_name"]), "picks": row["picks"], "verdict": None, "refusal": None}
            if row["verdict_id"] is not None:
                with researchdb.get_conn() as conn:
                    verdict_row = conn.execute("SELECT verdict FROM research_judge_verdicts WHERE id = ?;", (row["verdict_id"],)).fetchone()
                entry["verdict"] = {"word": verdict_row["verdict"] if verdict_row is not None else "missing", "id": row["verdict_id"]}
            else:
                entry["refusal"] = _plain(row["refusal"])
            saved.append(entry)
        stop = theory["stop"]
        if stop is not None:
            stop = {"step": stop["step"], "reason": _plain(stop["reason"])}
        theories.append({"id": theory["id"], "name": theory["theory_name"], "display_name": theory["display_name"],
                         "choices": theory["choices"], "runs": runs, "search": search, "final": final, "saved": saved, "stop": stop})
    template = tree["template"]
    batch = tree["batch"]
    control = gendb.batch_controls(_db_path()).get(batch["id"], dict(_RUNNING))
    facts = _universe_facts([batch["universe_id"], batch["searched_universe_id"]])
    names = {template["sha256"]: template["display_name"]}
    return {"batch": _public_batch(batch, control, facts.get(batch["universe_id"], {}).get("synthetic", False), _batch_name(batch, facts, names)),
            "template": {"name": template["name"], "display_name": template["display_name"], "sha256": template["sha256"]},
            "theories": theories}
