"""The generator runner (lane GV3-W3b, DECISIONS #467 items 5 and 11, #472 item 8, #473): after a search's final test, the server takes the
strategies that held up one step at a time: an optimise run on them, one walk-forward run, then the judge on each, and saves each with a
display name. run_pass_once is one pass over every approved batch; main.py runs it every 30 seconds.

In plain words: a search ends with a list of strategies, and the final test marks which of them held up on stocks and years the search never
saw. This module does the rest of the work for those only: it dispatches the two later test runs, waits for them, asks the judge for a
verdict on each held-up strategy and writes down what came back. It keeps no state of its own. Each pass reads what exists (the
theory's run links, the draw log, the verdict table) and does the one next thing, so a restart at any point loses nothing and repeats nothing.

A paused batch, and a removed one (GD1a), is skipped whole (GV3-W4). In a stopped batch each theory without a stop row gets one, "Stopped by you on D.", at the
first of screen, optimise, walk-forward that had not succeeded (judge when all had), after its unstarted runs are cancelled; a theory
that already ended is left alone.

First match wins, per theory, in a batch that runs:
  a stop row, or a search still pending or running, no final.json, nothing held up: do nothing (a theory with nothing held up has no stages to run).
  a search run that is gone, failed or cancelled: one stop row at the screen step.
  no optimise link: find the run an earlier pass dispatched (granted draw and its run_linked row) and link it, or dispatch it.
  optimise pending or running: wait. Failed, cancelled or missing: one stop row. The walk-forward run follows the same three rules.
  both succeeded: for each held-up strategy not yet saved, read the verdict at the judge's exactly-once key, or ask judge_run.

A stop row is the only way a theory ends badly, and a theory with a stop row is never touched again. Nothing waits silently: a draw
with no linked run waits only until tracker.UNLINKED_DRAW_STALE_AGE_SECONDS, then stops the theory.

The stages dispatch through stage_runner.dispatch_theory_stage, so the draw gate (windows in strictly increasing time) still applies.
The held-up strategies come from research_lab.theory_e2e.run's own split_finalists and held_up_variations, the code GV3-F's planted run
uses, so the two cannot disagree about which strategy is which.
"""
from __future__ import annotations

import json
import logging
import re
import time

from fastapi import HTTPException

from research_lab.theory_e2e import run as theory_e2e

from . import artifact_store, chain_runner, gendb, judgedb, loop_counters, researchdb, stage_runner, tracker
from .routes import generator_v3
from .routes import judge as judge_routes

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

_STAGE_WORDS = {"optimise": "optimise", "walkforward": "walk-forward"}
_ALREADY_GRADED = re.compile(r"^run \d+ was already graded")
_JUDGE_WAIT_WORDS, _JUDGE_FAIL_WORDS, _JUDGE_REFUSE_WORDS = chain_runner._P1_WORDS["judge"]  # the words chain_runner already reads, per kind


def _db_path():
    return researchdb.DB_PATH


def _read_json(run_id: int, name: str):
    """One JSON file of a run from the artifact store. A missing or unreadable file raises, so the theory crash is counted and logged."""
    with artifact_store.store.open(run_id, name) as handle:
        return json.load(handle)


def _stop(theory: dict, step: str, sentence: str, now: int) -> None:
    """Write the theory's one stop row, in plain words (no snake_case word), and log it."""
    reason = generator_v3._plain(sentence)
    gendb.record_stop(_db_path(), theory["id"], step, reason, now)
    log.warning("generator runner: %s stopped at %s: %s", theory["display_name"], step, reason)


def held_up_strategies(search_run_id: int) -> list:
    """[{"rank", "variation": {"id", "picks", "exits"}}] for the strategies that held up in the search's final test, in rank order, their
    variation ids numbered from 0. Empty when the search has no final.json or nothing held up."""
    if not artifact_store.store.exists(search_run_id, "final.json"):
        return []
    final = _read_json(search_run_id, "final.json")
    finalists = _read_json(search_run_id, "finalists.json")
    pick = _read_json(search_run_id, "pick.json")
    _rows, variations, _pick_variation = theory_e2e.split_finalists(finalists, pick)
    found = []
    for variation_id in theory_e2e.held_up_variations(final, variations):
        variation = next(v for v in variations if v["id"] == variation_id)
        strategy = next(s for s in final["strategies"] if s["held_up"] and s["picks"] == variation["picks"])
        found.append({"rank": strategy["rank"], "variation": variation})
    found.sort(key=lambda item: item["rank"])
    return [{"rank": item["rank"], "variation": {"id": number, "picks": item["variation"]["picks"], "exits": item["variation"]["exits"]}}
            for number, item in enumerate(found)]


def _stage_window(batch: dict, stage: str) -> tuple:
    if stage == "optimise":
        return batch["optimise_start"], batch["optimise_end"]
    start, end = generator_v3.walkforward_years(batch["optimise_end"])
    return start, end


def _find_draw(payload: dict, calendar_id: str, stage: str) -> "dict | None":
    """The newest granted draw for this theory's strategy, calendar and stage, with the run its run_linked row names (None when no link
    row yet) and the link row's decision: {"id", "created_at", "run_id", "link_decision", "link_reason"}. None when no draw exists."""
    with researchdb.get_conn() as conn:
        draw = conn.execute(
            "SELECT id, created_at FROM research_data_draws WHERE event_kind = 'draw_requested' AND decision = 'granted' AND stage = ? "
            "AND strategy_id = ? AND strategy_version = ? AND calendar_id = ? ORDER BY id DESC LIMIT 1;",
            (stage, payload["strategy_id"], payload["strategy_version"], calendar_id)).fetchone()
        if draw is None:
            return None
        link = conn.execute(
            "SELECT run_id, decision, refusal_reason FROM research_data_draws WHERE event_kind = 'run_linked' AND draw_id = ? "
            "ORDER BY id DESC LIMIT 1;", (draw["id"],)).fetchone()
    return {"id": draw["id"], "created_at": draw["created_at"], "run_id": link["run_id"] if link is not None else None,
            "link_decision": link["decision"] if link is not None else None, "link_reason": link["refusal_reason"] if link is not None else None}


def _stage_universe(batch: dict, stage: str, window: tuple) -> "int | str":
    """The id of the stage's price universe (the batch's group less its kept-back stocks, over the stage's window), made or adopted
    when an equal-window one of the same name exists (chain_runner._attempt_stage_dispatch's rule). A sentence when it cannot be made."""
    tickers = [m for m in batch["members"] if m not in batch["kept_back"]]
    name = f"{batch['group_label']} {_STAGE_WORDS[stage]} {batch['calendar_id']}"
    start_ts, end_ts = researchdb._date_to_ts(window[0]), researchdb._date_to_ts(window[1])
    try:
        return researchdb.create_price_universe(name, tickers, start_ts, end_ts)["universe_id"]
    except researchdb.PriceUniverseNameExists as exc:
        with researchdb.get_conn() as conn:
            existing = conn.execute("SELECT window_start_ts, window_end_ts FROM research_datasets WHERE id = ?;", (exc.existing_id,)).fetchone()
        if existing is not None and existing["window_start_ts"] == start_ts and existing["window_end_ts"] == end_ts:
            return exc.existing_id
        return f"A stock group called {name} already exists with other years."
    except (researchdb.PriceUniverseEmptyError, researchdb.PriceUniverseMixedSource, researchdb.PriceUniverseWindowInvalid,
            researchdb.PriceUniverseWindowRequired) as exc:
        return f"The stocks for the {_STAGE_WORDS[stage]} run could not be made into a group: {exc}."


def _adopt_or_dispatch(batch: dict, theory: dict, stage: str, search_run_id: int, variations: list, now: int) -> "int | None":
    """The run id of the theory's stage once it is linked, or None when the pass must go no further (waiting, or a stop row written)."""
    payload = researchdb.read_theory_search_payload(search_run_id)
    word = _STAGE_WORDS[stage]
    draw = _find_draw(payload, batch["calendar_id"], stage)
    if draw is not None:
        if draw["link_decision"] == "refused":
            _stop(theory, stage, f"The {word} run for {theory['display_name']} was drawn but its link was refused: {draw['link_reason']}.", now)
            return None
        if draw["run_id"] is None:
            age = now - draw["created_at"]
            if age < tracker.UNLINKED_DRAW_STALE_AGE_SECONDS:
                return None  # the dispatch may still be finishing
            _stop(theory, stage, f"The {word} run for {theory['display_name']} was drawn {age} seconds ago and never started.", now)
            return None
        gendb.record_theory_run(_db_path(), theory["id"], stage, draw["run_id"], now)  # an earlier pass dispatched it and did not link it
        return draw["run_id"]
    universe = _stage_universe(batch, stage, _stage_window(batch, stage))
    if isinstance(universe, str):
        _stop(theory, stage, universe, now)
        return None
    try:
        result = stage_runner.dispatch_theory_stage(stage, universe, now, search_run_id=search_run_id, variations=variations)
    except generator_v3._DISPATCH_REFUSALS as exc:
        _stop(theory, stage, f"The {word} run for {theory['display_name']} was refused: {exc}.", now)
        return None
    gendb.record_theory_run(_db_path(), theory["id"], stage, result["run_id"], now)
    return result["run_id"]


def _stage_done(batch: dict, theory: dict, linked: dict, stage: str, search_run_id: int, variations: list, now: int) -> "int | None":
    """The stage's run id when it has succeeded; None while it waits or after a stop row was written. `linked` is {stage: run_id} of the
    theory's links; a link made here is added to it."""
    run_id = linked.get(stage)
    if run_id is None:
        run_id = _adopt_or_dispatch(batch, theory, stage, search_run_id, variations, now)
        if run_id is None:
            return None
        linked[stage] = run_id
    word = _STAGE_WORDS[stage]
    run = researchdb.get_run(run_id)
    if run is None:
        _stop(theory, stage, f"The {word} run {run_id} for {theory['display_name']} no longer exists.", now)
        return None
    status = run["status"]
    if status in ("pending", "running"):
        return None
    if status == "succeeded":
        return run_id
    if status in ("failed", "cancelled"):
        detail = run["error"] or f"it was {status} with no stored message"
        _stop(theory, stage, f"The {word} run {run_id} for {theory['display_name']} {status}: {detail}.", now)
        return None
    _stop(theory, stage, f"The {word} run {run_id} for {theory['display_name']} has the unknown status {status}.", now)
    return None


def _verdict_at_key(optimise_run_id: int, variation_id: int, ticker: str) -> "int | None":
    """The verdict already written at the judge's exactly-once key for this cell (chain_runner._process_judge_step's read), or None."""
    pass_id = judge_routes._judging_pass_id_for_cell(optimise_run_id, variation_id, ticker, judgedb.BATTERY_VERSION)
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM research_judge_verdicts WHERE run_id = ? AND variation_id = ? AND ticker = ? AND judging_pass_id = ?;",
            (optimise_run_id, variation_id, ticker, pass_id)).fetchone()
    return row["id"] if row is not None else None


def _judge_held_up(batch: dict, theory: dict, held: list, optimise_run_id: int, walkforward_run_id: int, now: int) -> None:
    ticker = batch["group_label"]
    done = {s["rank"] for s in theory["saved"]}
    for item in held:
        if item["rank"] in done:
            continue
        variation_id = item["variation"]["id"]
        verdict_id = _verdict_at_key(optimise_run_id, variation_id, ticker)
        refusal = None
        if verdict_id is None:
            try:
                verdict_id = judge_routes.judge_run(optimise_run_id, variation_id=variation_id, ticker=ticker)["verdict_id"]
            except HTTPException as exc:
                detail = str(exc.detail)
                if exc.status_code == 409 and _ALREADY_GRADED.match(detail):
                    verdict_id = _verdict_at_key(optimise_run_id, variation_id, ticker)
                    if verdict_id is None:
                        _stop(theory, "judge", f"The judge says strategy {item['rank']} of {theory['display_name']} was already graded, "
                                               f"but no verdict is stored for it: {detail}.", now)
                        return
                else:
                    word = detail.split(":", 1)[0]
                    if word in _JUDGE_WAIT_WORDS:
                        continue  # asked again next pass; nothing is saved
                    if word in _JUDGE_REFUSE_WORDS:
                        refusal = detail
                    else:  # a fail word or a word nobody anticipated (chain_runner's route-answer-unworded): the theory stops
                        _stop(theory, "judge", f"The judge gave no usable answer for strategy {item['rank']} of {theory['display_name']} "
                                               f"(status {exc.status_code}): {detail}.", now)
                        return
        gendb.record_saved_strategy(
            _db_path(), gen_theory_id=theory["id"], rank=item["rank"], display_name=f"{theory['display_name']}, strategy {item['rank']}",
            picks=item["variation"]["picks"], optimise_run_id=optimise_run_id, walkforward_run_id=walkforward_run_id,
            variation_id=variation_id, ticker=ticker, verdict_id=verdict_id, refusal=refusal, now=now)


def _settle_stopped(theory: dict, since: int, now: int) -> None:
    """A theory of a batch Tamer stopped: cancel what has not started and write its one stop row, "Stopped by you on D.", at the first
    step that had not succeeded. A theory that already ended (a stop row, or a succeeded search with nothing held up or every held-up
    strategy saved) is left as it is. Safe to repeat: the stop row ends it."""
    if theory["stop"] is not None:
        return
    linked = {l["stage"]: l["run_id"] for l in theory["runs"]}
    search_id = linked.get("screen")
    search_run = researchdb.get_run(search_id) if search_id is not None else None
    if search_run is not None and search_run["status"] == "succeeded":
        held = held_up_strategies(search_id)
        if not held or all(item["rank"] in {s["rank"] for s in theory["saved"]} for item in held):
            return
    step = "judge"
    for stage in ("screen", "optimise", "walkforward"):
        run = researchdb.get_run(linked[stage]) if stage in linked else None
        if run is None or run["status"] != "succeeded":
            step = stage
            break
    generator_v3.cancel_queued([theory])
    _stop(theory, step, f"Stopped by you on {generator_v3.paris_day_words(since)}.", now)


def _process_theory(batch: dict, theory: dict, now: int) -> None:
    if theory["stop"] is not None:
        return
    search = next((l for l in theory["runs"] if l["stage"] == "screen"), None)
    if search is None:
        return
    search_run = researchdb.get_run(search["run_id"])
    if search_run is None:
        _stop(theory, "screen", f"The search run {search['run_id']} for {theory['display_name']} no longer exists.", now)
        return
    if search_run["status"] in ("failed", "cancelled"):
        detail = search_run["error"] or f"it was {search_run['status']} with no stored message"
        _stop(theory, "screen", f"The search run {search['run_id']} for {theory['display_name']} {search_run['status']}: {detail}.", now)
        return
    if search_run["status"] != "succeeded":
        return
    held = held_up_strategies(search["run_id"])
    if not held or all(item["rank"] in {s["rank"] for s in theory["saved"]} for item in held):
        return
    variations = [item["variation"] for item in held]
    linked = {l["stage"]: l["run_id"] for l in theory["runs"]}
    optimise_run_id = _stage_done(batch, theory, linked, "optimise", search["run_id"], variations, now)
    if optimise_run_id is None:
        return
    walkforward_run_id = _stage_done(batch, theory, linked, "walkforward", search["run_id"], variations, now)
    if walkforward_run_id is None:
        return
    _judge_held_up(batch, theory, held, optimise_run_id, walkforward_run_id, now)


def run_pass_once(now: "int | None" = None) -> dict:
    """One pass over every batch and every theory in it. A theory whose step raises is logged in full and counted (DECISIONS #432: the
    catch-all isolates one theory so the others still move), and is tried again next pass. Returns {"theories": n, "crashes": k}."""
    now = int(time.time()) if now is None else now
    theories = crashes = 0
    controls = gendb.batch_controls(_db_path())
    for listed in gendb.list_batches(_db_path()):
        if listed["removed_at"] is not None:
            continue  # a removed batch is skipped whole (GD1a)
        control = controls.get(listed["id"], {"state": "running", "since": None})
        if control["state"] == "paused":
            continue  # a paused batch is skipped whole; the pause lives in the store, so a restart keeps it
        tree = gendb.read_tree(_db_path(), listed["id"])
        for theory in tree["theories"]:
            theories += 1
            try:
                if control["state"] == "stopped":
                    _settle_stopped(theory, control["since"], now)
                else:
                    _process_theory(tree["batch"], theory, now)
            except Exception:  # noqa: BLE001 -- DECISIONS #432: isolates one theory; logged in full and counted where Operations reads it
                crashes += 1
                log.exception("generator runner: theory %s (batch %s) crashed", theory["id"], listed["id"])
                loop_counters.count_generator_theory_crash()
    return {"theories": theories, "crashes": crashes}
