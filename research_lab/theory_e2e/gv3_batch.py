"""python -m research_lab.theory_e2e.gv3_batch --out <dir>   (lane GV3-B1, DECISIONS #467 items 5 and 11)

Runs ONE generator v3 template batch on GV2-1's made-up stocks (SYNTHETIC, seed 20261012) in theory_e2e's scratch set-up, through the real
routes: it saves the template, previews it, approves it, then watches the batch tree fill (the backend's generator runner does the stages
and the judge) until every theory has ended. It writes <out>/gv3_result.json after every poll, and at the end compares
GET /batches/{id}/tree with gendb.read_tree on the scratch research.db.

Run it with the dashboard interpreter. Like run.py it starts THIS TREE's backend and worker on scratch databases, refuses a non-local
RESEARCH_SERVER_URL (guard.py), and never touches production. Nothing here tunes a value: it measures and reports.

The module top imports only the standard library, guard and run, so the worker interpreter can import the pure functions
(codes_for, approve_body, theory_state, compare_tree); gendb, generator_runner and routes.generator_v3 are imported inside main.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from research_lab.theory_e2e import guard, run

POLL_S = 30                      # the pass interval of main.py's _generator_runner_loop
OPTIMISE_WINDOW = ["2017-01-03", "2019-12-31"]
DEFAULT_TEMPLATE = "research_lab/theories/templates/rebound.toml"
DEFAULT_LIMIT_S = 14400          # run.py's own defaults: 7200 for the search plus 3600 for each of the two stages
CRASH_MARKS = (("generator runner: theory", "crashed"),)
PASS_FAILED_MARK = "Generator runner pass failed"
SEARCH_ENDS = ("search failed", "search cancelled", "search missing")   # the search states that end a theory without a StepFailed
RUNNING_STATUSES = ("pending", "running")


# ---- pure functions ------------------------------------------------------------------------------------------------------------

def codes_for(manifest: dict, group: str) -> list:
    """The sorted codes of the made-up stocks in `group` (manifest["codes"] keys starting with group + "_")."""
    codes = sorted(c for c in manifest["codes"] if c.startswith(group + "_"))
    if not codes:
        raise ValueError("no made-up stocks for group %s" % group)
    return codes


def approve_body(universe_id: int, later_end: str, calendar_id: str, group: str) -> dict:
    """The body of POST /templates/{sha256}/approve."""
    return {"universe_id": universe_id, "seed": run.SEED, "optimise_window": list(OPTIMISE_WINDOW),
            "later_window": [run.RESERVE_START, later_end], "group_label": "GROUP_" + group, "calendar_id": calendar_id,
            "data_start": run.DATA_START}


def theory_state(theory: dict, held_count: int) -> str:
    """One theory of GET /batches/{id}/tree as one word: stopped, waiting, nothing held up, judged, or "search <status>"."""
    if theory["stop"] is not None:
        return "stopped"
    screen = next((r for r in theory["runs"] if r["stage"] == "screen"), None)
    if screen is None or screen["status"] in RUNNING_STATUSES:
        return "waiting"
    if screen["status"] == "succeeded":
        if held_count == 0:
            return "nothing held up"
        if len(theory["saved"]) >= held_count:
            return "judged"
        return "waiting"
    return "search " + screen["status"]


def compare_tree(tree: dict, read_tree: dict, plain) -> list:
    """One sentence per difference between the route's tree and gendb.read_tree, theory by theory in order. `plain` is the route's
    sentence cleaner (generator_v3._plain), applied to the stored words the route cleans."""
    out = []
    a, b = tree["theories"], read_tree["theories"]
    if len(a) != len(b):
        out.append("the route lists %d theories, the database %d" % (len(a), len(b)))
    for n, (route, stored) in enumerate(zip(a, b), start=1):
        label = "theory %d" % n
        if route["id"] != stored["id"]:
            out.append("%s: the route's id is %s, the database's %s" % (label, route["id"], stored["id"]))
        route_runs = [(r["stage"], r["run_id"]) for r in route["runs"]]
        stored_runs = [(r["stage"], r["run_id"]) for r in stored["runs"]]
        if route_runs != stored_runs:
            out.append("%s: the route's runs are %s, the database's %s" % (label, route_runs, stored_runs))
        route_saved = [(s["rank"], s["display_name"], s["picks"], s["verdict"]["id"] if s["verdict"] is not None else None, s["refusal"])
                       for s in route["saved"]]
        stored_saved = [(s["rank"], plain(s["display_name"]), s["picks"], s["verdict_id"], plain(s["refusal"]) if s["refusal"] is not None else None)
                        for s in stored["saved"]]
        if len(route_saved) != len(stored_saved):
            out.append("%s: the route lists %d saved strategies, the database %d" % (label, len(route_saved), len(stored_saved)))
        for route_row, stored_row in zip(route_saved, stored_saved):
            if route_row != stored_row:
                out.append("%s: saved strategy rank %s differs: the route has %s, the database %s" % (label, stored_row[0], route_row, stored_row))
        route_stop = None if route["stop"] is None else (route["stop"]["step"], route["stop"]["reason"])
        stored_stop = None if stored["stop"] is None else (stored["stop"]["step"], plain(stored["stop"]["reason"]))
        if route_stop != stored_stop:
            out.append("%s: the route's stop is %s, the database's %s" % (label, route_stop, stored_stop))
    return out


# ---- the driver ----------------------------------------------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m research_lab.theory_e2e.gv3_batch")
    p.add_argument("--out", required=True, help="folder for gv3_result.json, manifest.json and the answer files")
    p.add_argument("--template", default=DEFAULT_TEMPLATE, help="a template file, relative to the repo root unless absolute")
    p.add_argument("--data-version", type=int, choices=(1, 2), default=1, help="the made-up stocks set (default 1)")
    p.add_argument("--group", default="SYN_VOL")
    p.add_argument("--calendar", default="gv3-b1-check")
    p.add_argument("--limit-s", type=float, default=DEFAULT_LIMIT_S, help="seconds from approve to the last theory's end")
    return p.parse_args(argv)


def _write(out_dir: Path, result: dict) -> None:
    (out_dir / "gv3_result.json").write_text(json.dumps(run._clean(result), indent=2, sort_keys=True, default=str, allow_nan=False) + "\n",
                                             encoding="utf-8")


def _check_processes(procs: dict) -> None:
    for name, proc in procs.items():
        if proc.poll() is not None:
            raise run.StepFailed("the scratch %s exited with %s" % (name, proc.returncode))


def _check_backend_log(log_path: Path) -> None:
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else []
    for i, line in enumerate(lines):
        if PASS_FAILED_MARK in line:
            raise run.StepFailed("the generator runner pass failed in backend.log:\n" + "\n".join(lines[i:i + 41]))
        if any(all(mark in line for mark in marks) for marks in CRASH_MARKS):
            raise run.StepFailed("the generator runner crashed in backend.log: " + line)


def _verdict_checks(db: Path, verdict_id: int) -> dict:
    with sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30) as conn:
        row = conn.execute("SELECT check_outcomes_json FROM research_judge_verdicts WHERE id = ?;", (verdict_id,)).fetchone()
    if row is None:
        return {"verdict_id": verdict_id, "error": "no verdict row with this id"}
    checks = run.normalise_checks(json.loads(row[0]))
    check3 = next((c for c in checks if str(c["check"]) == "3"), None)
    return {"verdict_id": verdict_id, "check_3": check3, "checks": checks}


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        guard.check_local_only(os.environ)
    except guard.RefusedToStart as exc:
        print("gv3_batch.py refuses to start: %s" % exc, file=sys.stderr)
        return 2
    tree_root = Path(__file__).resolve().parents[2]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    template_path = Path(args.template)
    if not template_path.is_absolute():
        template_path = tree_root / template_path
    template_text = template_path.read_text(encoding="utf-8")
    scratch = run.Scratch(tree_root, run.free_port())
    os.environ.update(scratch.env)       # this process reads the scratch research.db and artifact store
    run.log("scratch folder %s, backend %s" % (scratch.root, scratch.url))
    from dashboard.backend import generator_runner, gendb   # imported after the scratch environment is set
    from dashboard.backend.routes import generator_v3

    result = {"synthetic": True, "data_version": args.data_version, "group": args.group, "seed": run.SEED,
              "template": {"path": str(template_path), "sha256": hashlib.sha256(template_text.encode("utf-8")).hexdigest()},
              "preview": None, "approve": None, "states": [], "tree": None, "read_tree": None, "differences": None, "verdicts": [],
              "errors": [], "seconds_total": None}
    began = time.monotonic()
    backend = worker = None
    batch_id = None
    failed = False
    try:
        try:
            syn = scratch.root / "syn"
            for cmd in (["make", "--out", str(syn), "--seed", str(run.SEED), "--version", str(args.data_version)], ["answer", "--data", str(syn)]):
                subprocess.run([sys.executable, "-m", "research_lab.synthetic"] + cmd, cwd=str(tree_root), env={**os.environ, "PYTHONPATH": str(tree_root)},
                               check=True, capture_output=True)
            names = ("manifest.json", "answer.csv", "summary.json") if args.data_version == 1 else ("manifest.json", "theory_answer.csv", "theory_summary.json")
            for name in names:
                (out_dir / name).write_bytes((syn / name).read_bytes())
            manifest = json.loads((syn / "manifest.json").read_text(encoding="utf-8"))
            backend = run.start_backend(tree_root, scratch)
            worker = run.start_worker(tree_root, scratch)
            procs = {"backend": backend, "worker": worker}
            http = run.Http(scratch.url)
            codes = codes_for(manifest, args.group)
            run.register_datasets(http, scratch, codes)
            run.log("%d datasets ready" % len(codes))
            universe_id = run.make_universe(http, "%s screen" % args.group, codes, run.SCREEN)
            searched = run.group_split(codes, run.SEED)["searched"]
            later_end = run.last_bar_date(syn, searched)
            body = approve_body(universe_id, later_end, args.calendar, args.group)
            _s, saved = http.call("POST", "/api/research/generator/v3/templates", {"content": template_text})
            sha = saved["sha256"]
            result["template"]["sha256"] = sha
            base = "/api/research/generator/v3/templates/%s" % sha
            _s, preview = http.call("POST", base + "/preview", {k: body[k] for k in ("universe_id", "seed", "optimise_window", "later_window")})
            result["preview"] = preview
            if sorted(preview["stocks"]["searched"]) != sorted(searched):
                raise run.StepFailed("the preview's searched stocks %s are not the driver's %s" % (preview["stocks"]["searched"], searched))
            _s, approved = http.call("POST", base + "/approve", body)
            result["approve"] = approved
            batch_id = approved["batch_id"]
            run.log("batch %d approved with runs %s" % (batch_id, approved["run_ids"]))
            approved_at = time.monotonic()
            while True:
                _check_processes(procs)
                _check_backend_log(scratch.root / "logs" / "backend.log")
                _s, tree = http.call("GET", "/api/research/generator/v3/batches/%d/tree" % batch_id)
                states = []
                for theory in tree["theories"]:
                    screen = next((r for r in theory["runs"] if r["stage"] == "screen"), None)
                    held = len(generator_runner.held_up_strategies(screen["run_id"])) if screen is not None and screen["status"] == "succeeded" else 0
                    states.append({"theory": theory["display_name"], "state": theory_state(theory, held), "held_up": held})
                result["tree"], result["states"] = tree, states
                result["seconds_total"] = time.monotonic() - began
                _write(out_dir, result)
                run.log("states: %s" % ", ".join(s["state"] for s in states))
                odd = [s for s in states if s["state"].startswith("search ") and s["state"] not in SEARCH_ENDS]
                if odd:
                    raise run.StepFailed("a search ended in a status this driver does not know: %s" % odd)
                if not any(s["state"] == "waiting" for s in states):
                    break
                if time.monotonic() - approved_at > args.limit_s:
                    raise run.StepFailed("the batch did not finish in %d seconds; states: %s" % (args.limit_s, [s["state"] for s in states]))
                time.sleep(POLL_S)
        except run.StepFailed as exc:
            failed = True
            result["errors"].append(str(exc))
            run.log("STEP FAILED: %s" % exc)
        if batch_id is not None:
            stored = gendb.read_tree(scratch.research_db, batch_id)
            result["read_tree"] = stored
            if result["tree"] is not None:
                result["differences"] = compare_tree(result["tree"], stored, generator_v3._plain)
            for theory in stored["theories"]:
                for row in theory["saved"]:
                    if row["verdict_id"] is not None:
                        result["verdicts"].append(_verdict_checks(scratch.research_db, row["verdict_id"]))
    finally:
        result["seconds_total"] = time.monotonic() - began
        _write(out_dir, result)
        for proc in (worker, backend):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for proc in (worker, backend):
            if proc is not None:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return 0 if not failed and result["differences"] == [] else 1


if __name__ == "__main__":
    sys.exit(main())
