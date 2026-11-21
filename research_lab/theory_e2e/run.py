"""python -m research_lab.theory_e2e.run --out <dir>   (lane GV2-7c, DECISIONS #440 and #445)

The planted run of generator v2, end to end, on GV2-1's made-up stocks (SYNTHETIC, seed 20261012), for the groups SYN_VOL, SYN_TWN,
SYN_FLT, SYN_TRD in that order, each through the same steps and settings:

  search (step 1 best 5 percent, step 2 box, finalists with stop and target, the pick)
  -> optimise stage on the finalists -> walk-forward stage -> the judge on the pick's cell -> the trial family row.

It starts THIS TREE's backend on fresh databases in a mktemp folder and THIS TREE's worker pointed at it (a scratch RESEARCH_SERVER_URL
and worker key, scratch RESEARCH_DATA_ROOT and SYNTHETIC_STORE_ROOT). It never starts the systemd research-worker and never talks to
production: guard.py refuses a non-local RESEARCH_SERVER_URL and the systemd worker's environment file before anything starts.
Dispatch is the lane tree's own stage_runner.dispatch_theory_stage, called in this process on the scratch research.db, so run this with
the dashboard interpreter (fastapi, uvicorn). Every stage is dry-run under a calendar named gv2-planted (never 'real': no budget count).
One calendar id per group, because the draw gate refuses a window earlier than the strategy's mark on the same calendar and the four
groups share the theory's one strategy row.

Writes <out>/result.json (see score.py for its shape; rewritten after every group), <out>/answer.csv and <out>/summary.json (GV2-1's
answer sheet), and the logs. This module measures and reports; it never tunes a value.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from research_lab.theory_e2e import guard

SEED = 20261012                                   # GV2-1's set
GROUPS = ("SYN_VOL", "SYN_TWN", "SYN_FLT", "SYN_TRD")
CALENDAR_PREFIX = "gv2-planted"                   # a dry-run calendar; stage_runner's real-calendar monitor counts only 'real'
SCREEN = ("2004-01-02", "2016-12-30")             # GV2-1 summary.json windows (the screen, optimise and walk-forward windows of #329)
OPTIMISE = ("2017-01-03", "2019-12-31")
WALKFORWARD = ("2020-01-02", "2022-12-30")
RESERVE_START = "2023-01-03"                      # the reserve; no window below reaches it
DATA_START = "2004-01-01"
DATASET_END = "2027-01-01"
# The search settings GV2-5b measured on SYN_VOL (its Real check): ONC cap 10, n_iter 100,000.
# A round's size and limits are the theory's own [search] table (GV3-S1, DECISIONS #470), recorded in the result's settings.
SEARCH_N_ITER, SEARCH_ONC_CAP = 100000, 10
COSTS = {"fees": "alpaca", "slippage": 0.0003}
INIT_CASH = 100000.0
THEORY = "capitulation_rebound.toml"
CHECK_NAMES = 9


class StepFailed(Exception):
    """One step of the chain failed; the message carries the step, the raw error and what ran before it."""


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(message: str) -> None:
    print("[%s] %s" % (_now(), message), flush=True)


def epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main_checkout(tree: Path) -> Path:
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(tree), capture_output=True,
                            text=True, check=True).stdout.strip()
    return Path(common[: -len("/.git")]) if common.endswith("/.git") else Path(common).parent


def venv_python(tree: Path, relative: str) -> Path:
    for root in (tree, main_checkout(tree)):
        candidate = root / relative
        if candidate.exists():
            return candidate
    raise FileNotFoundError("no interpreter at %s in the tree or the main checkout" % relative)


class Http:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, body=None, ok=(200, 201), timeout=600):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, text = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            status, text = exc.code, exc.read().decode()
        parsed = json.loads(text) if text else None
        if status not in ok:
            raise StepFailed("%s %s answered %s: %s" % (method, path, status, text[:600]))
        return status, parsed


class Scratch:
    """The mktemp folder and the environment of the backend and the worker."""

    def __init__(self, tree: Path, port: int):
        self.root = Path(tempfile.mkdtemp(prefix="gv2-7c-"))
        for sub in ("art", "mirror", "data", "syn", "numba", "home", "logs"):
            (self.root / sub).mkdir()
        self.port = port
        self.url = "http://127.0.0.1:%d" % port
        self.key = secrets.token_hex(16)
        self.research_db = self.root / "research.db"
        self.env = {
            "DASHBOARD_USER_DB_PATH": str(self.root / "dashboard.db"), "RESEARCH_DB_PATH": str(self.research_db),
            "EVENTS_DB_PATH": str(self.root / "events.db"), "OPS_DB_PATH": str(self.root / "ops.db"),
            "PAPER_DB_PATH": str(self.root / "paper.db"), "REAL_DB_PATH": str(self.root / "real.db"),
            "RESEARCH_ARTIFACT_ROOT": str(self.root / "art"), "RESEARCH_STRATEGY_DIR": str(self.root / "mirror"),
            "RESEARCH_DATA_ROOT": str(self.root / "data"), "SYNTHETIC_STORE_ROOT": str(self.root / "syn"),
            "RESEARCH_NUMBA_CACHE": str(self.root / "numba"),
            "RESEARCH_WORKER_API_KEY": self.key, "RESEARCH_SERVER_URL": self.url,
            "PYTHONPATH": str(tree),
        }


def start_backend(tree: Path, scratch: Scratch) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(scratch.env)
    env["HOME"] = str(scratch.root / "home")
    logfile = open(scratch.root / "logs" / "backend.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "dashboard.backend.main:app", "--host", "127.0.0.1", "--port", str(scratch.port)],
                            cwd=str(tree), env=env, stdout=logfile, stderr=subprocess.STDOUT)
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            raise StepFailed("the scratch backend exited with %s; see %s" % (proc.returncode, scratch.root / "logs" / "backend.log"))
        try:
            with urllib.request.urlopen(scratch.url + "/api/judge/thresholds", timeout=5) as resp:
                if resp.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(1.5)
    raise StepFailed("the scratch backend did not answer in 180 seconds; see %s" % (scratch.root / "logs" / "backend.log"))


def start_worker(tree: Path, scratch: Scratch) -> subprocess.Popen:
    python = venv_python(tree, "research_lab/worker/.venv/bin/python")
    env = dict(os.environ)
    env.update(scratch.env)
    logfile = open(scratch.root / "logs" / "worker.log", "wb")
    return subprocess.Popen([str(python), str(tree / "research_lab" / "worker" / "worker.py")], cwd=str(tree / "research_lab" / "worker"),
                            env=env, stdout=logfile, stderr=subprocess.STDOUT)


def run_status(db: Path, run_id: int) -> dict:
    with sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT r.status, r.error, j.status AS job_status, j.error AS job_error FROM research_runs r "
                           "LEFT JOIN research_jobs j ON j.id = r.job_id WHERE r.id = ?;", (run_id,)).fetchone()
    return dict(row)


def wait_run(scratch: Scratch, run_id: int, what: str, workers: dict, limit_s: float) -> float:
    started = time.monotonic()   # not the wall clock: a laptop suspend or clock step must not end a wait
    while True:
        st = run_status(scratch.research_db, run_id)
        if st["status"] == "succeeded":
            return time.monotonic() - started
        if st["status"] in ("failed", "cancelled") or st["job_status"] == "failed":
            raise StepFailed("%s (run %d) ended %s: %s %s" % (what, run_id, st["status"], st["error"], st["job_error"]))
        for name, proc in workers.items():
            if proc.poll() is not None:
                raise StepFailed("%s (run %d): the scratch %s exited with %s" % (what, run_id, name, proc.returncode))
        if time.monotonic() - started > limit_s:
            raise StepFailed("%s (run %d) did not finish in %d seconds (status %s)" % (what, run_id, limit_s, st["status"]))
        time.sleep(5)


def read_artifact_json(scratch: Scratch, run_id: int, name: str):
    return json.loads((scratch.root / "art" / str(run_id) / name).read_text(encoding="utf-8"))


def register_datasets(http: Http, scratch: Scratch, codes: list) -> dict:
    ids = {}
    for code in codes:
        _s, r = http.call("POST", "/api/research/datasets", {"name": "%s 1d" % code, "source": "synthetic", "symbol": code, "timeframe": "1d",
                                                              "start": DATA_START + "T00:00:00Z", "end": DATASET_END + "T00:00:00Z"})
        ids[code] = r["id"]
    deadline = time.time() + 900
    pending = dict(ids)
    while pending and time.time() < deadline:
        for code, did in list(pending.items()):
            _s, d = http.call("GET", "/api/research/datasets/%d?include_synthetic=true" % did)
            if d["status"] == "ready":
                del pending[code]
            elif d["status"] in ("failed", "cancelled"):
                raise StepFailed("dataset %s ended %s: %s" % (code, d["status"], d.get("error")))
        time.sleep(2)
    if pending:
        raise StepFailed("datasets not ready after 900 seconds: %s" % sorted(pending))
    return ids


def make_universe(http: Http, name: str, tickers: list, window: tuple) -> int:
    _s, r = http.call("POST", "/api/research/price-universes",
                      {"name": name, "tickers": tickers, "window_start_ts": epoch(window[0]), "window_end_ts": epoch(window[1])})
    if r["member_count"] != len(tickers) or r["excluded"]:
        raise StepFailed("universe %s: members %s, excluded %s" % (name, r["member_count"], r["excluded"]))
    return r["universe_id"]


def normalise_checks(outcomes) -> list:
    """The judge's nine outcomes as [{check, outcome, reason}], whatever shape the route returned them in (kept raw as well)."""
    items = outcomes.items() if isinstance(outcomes, dict) else enumerate(outcomes, start=1)
    rows = []
    for key, value in items:
        if not isinstance(value, dict):
            rows.append({"check": key, "outcome": str(value), "reason": None})
            continue
        outcome = value.get("outcome", value.get("status", value.get("result")))
        reason = value.get("reason", value.get("note", value.get("detail")))
        rows.append({"check": value.get("check", value.get("check_id", key)), "outcome": outcome, "reason": reason})
    return rows


def group_split(members: list, seed: int) -> dict:
    """GV3-F (DECISIONS #467 item 5): the group's kept-back third, drawn once with the seed (research_lab.theories.kept_back.draw_kept_back,
    the same function the generator store uses), and the stocks the search, the optimise stage and the walk-forward stage use: the others,
    in the group's order."""
    from research_lab.theories import kept_back
    kept = kept_back.draw_kept_back(members, seed)
    return {"kept_back": kept, "searched": [m for m in members if m not in kept]}


def held_up_variations(final: "dict | None", variations: list) -> list:
    """GV3-F: the ids of the stage variations whose strategy held up in final.json (None when the search had no final). A strategy is matched
    to its variation by equal picks, as the pick is (split_finalists); a held-up strategy with no variation (its calibration failed) has none."""
    if final is None:
        return []
    held = [s["picks"] for s in final["strategies"] if s["held_up"]]
    return [v["id"] for v in variations if v["picks"] in held]


def last_bar_date(syn_folder: Path, codes: list) -> str:
    """The earliest of the stocks' last bar dates in GV2-1's raw files (the date column), as YYYY-MM-DD."""
    import pandas as pd
    ends = [pd.to_datetime(pd.read_parquet(syn_folder / ("%s.parquet" % code), columns=["date"])["date"]).max() for code in codes]
    return min(ends).strftime("%Y-%m-%d")


def judge_variation(http: Http, run_id: int, variation_id: int, label: str) -> dict:
    """POST the judge for one variation's cell of the optimise run; the result as theory_e2e records it (a refusal is recorded, not raised)."""
    status, verdict = http.call("POST", "/api/judge/runs/%d/judge?variation_id=%d&ticker=%s" % (run_id, variation_id, label), None,
                                ok=(200, 201, 409, 422))
    if status in (200, 201):
        return {"verdict": verdict["verdict"], "verdict_id": verdict["verdict_id"], "checks": normalise_checks(verdict["check_outcomes"]),
                "raw_check_outcomes": verdict["check_outcomes"]}
    return {"verdict": None, "refused": {"status": status, "detail": verdict}, "checks": []}


def run_group(http: Http, scratch: Scratch, workers: dict, theory_text: str, group: str, codes: list, universes: dict,
              dispatch, answer_workers: int, limits: dict) -> dict:
    label = "GROUP_" + group
    calendar = "%s-%s" % (CALENDAR_PREFIX, group)
    out = {"label": label, "calendar_id": calendar, "seconds": {}}
    started = time.time()
    # 1. the search: it reads the group's searched stocks only; the kept-back stocks and the later years are the final test (GV3-F, #473)
    split = group_split(codes, SEED)
    later_end = last_bar_date(scratch.root / "syn", split["searched"])
    if later_end <= RESERVE_START:
        raise StepFailed("%s: the searched stocks' earliest last bar %s is not after the later window's start %s" % (group, later_end, RESERVE_START))
    final_settings = {"kept_back": split["kept_back"], "later_window": [RESERVE_START, later_end]}
    out["kept_back"], out["searched"], out["later_window"] = split["kept_back"], split["searched"], final_settings["later_window"]
    log("%s: search n_iter %d, kept back %s, later window %s to %s" % (group, limits["n_iter"], ",".join(split["kept_back"]), RESERVE_START, later_end))
    search = {"seed": SEED, "n_iter": limits["n_iter"], "onc_cap": SEARCH_ONC_CAP, "optimise_window": list(OPTIMISE),
              "workers": answer_workers, "final": final_settings}
    s = dispatch("screen", universes["screen"], int(time.time()), theory_text=theory_text, calendar_id=calendar, group_label=label,
                 data_start=DATA_START, search=search, costs=COSTS, init_cash=INIT_CASH)
    out["search_run_id"] = s["run_id"]
    out["seconds"]["search"] = wait_run(scratch, s["run_id"], "%s search" % group, workers, limits["search"])
    log("%s: search done in %.0f s" % (group, out["seconds"]["search"]))
    family = read_artifact_json(scratch, s["run_id"], "family.json")
    finalists = read_artifact_json(scratch, s["run_id"], "finalists.json")
    pick = read_artifact_json(scratch, s["run_id"], "pick.json")
    final = read_artifact_json(scratch, s["run_id"], "final.json")
    out["final"] = final
    out["family"] = family
    out["finalists"], variations, pick_var = split_finalists(finalists, pick)
    out["pick"] = {k: v for k, v in pick.items() if k != "calibration"}
    if "calibration" in pick:
        out["pick"]["calibration_buckets"] = pick["calibration"]["buckets"]
    out["step1_best"] = step1_best(scratch, s["run_id"])
    out["box"] = step2_box(scratch, s["run_id"])
    if not finalists or pick.get("reason"):
        out["stopped"] = "the search gave no pick: %s" % pick.get("reason")
        return out
    if len(pick_var) != 1:
        raise StepFailed("%s: the pick %s is not exactly one finalist" % (group, pick["picks"]))
    out["pick_variation_id"] = pick_var[0]
    # 2. the optimise stage on the finalists
    o = dispatch("optimise", universes["optimise"], int(time.time()), search_run_id=s["run_id"], variations=variations)
    out["optimise_run_id"] = o["run_id"]
    out["seconds"]["optimise"] = wait_run(scratch, o["run_id"], "%s optimise" % group, workers, limits["stage"])
    log("%s: optimise done in %.0f s" % (group, out["seconds"]["optimise"]))
    cells = read_artifact_json(scratch, o["run_id"], "cells.json")
    out["optimise_cells"] = cells
    out["optimise_trades"] = optimise_trades(scratch, o["run_id"], pick_var[0], label)
    # 3. the walk-forward stage
    w = dispatch("walkforward", universes["walkforward"], int(time.time()), search_run_id=s["run_id"], variations=variations)
    out["walkforward_run_id"] = w["run_id"]
    out["seconds"]["walkforward"] = wait_run(scratch, w["run_id"], "%s walkforward" % group, workers, limits["stage"])
    log("%s: walkforward done in %.0f s" % (group, out["seconds"]["walkforward"]))
    wf = read_artifact_json(scratch, w["run_id"], "walkforward.json")
    out["walkforward"] = {k: v for k, v in wf.items() if k != "slices"}
    out["walkforward"]["slices_listed"] = len(wf.get("slices", []))
    # 4. the judge on the pick's cell
    out["verdict"] = judge_variation(http, o["run_id"], pick_var[0], label)
    # 5. the judge on every strategy that held up in the final test (GV3-F): "verdict" is the pick's, "verdicts" the held-up ones'
    held = held_up_variations(final, variations)
    out["verdicts"] = [{"variation_id": vid, "picks": next(v["picks"] for v in variations if v["id"] == vid)}
                       | (out["verdict"] if vid == pick_var[0] else judge_variation(http, o["run_id"], vid, label))   # the pick is judged once
                       for vid in held]
    log("%s: %d of %d kept strategies held up in the final test, judged" % (group, len(held), len(final["strategies"])))
    out["seconds"]["group"] = time.time() - started
    log("%s: verdict %s" % (group, out["verdict"]["verdict"]))
    return out


def split_finalists(finalists: list, pick: dict) -> tuple:
    """(rows for result.json, the stage variations, the variation ids equal to the pick). A finalist whose O-U fit failed carries
    "calibration_error" and no "calibration": it keeps its row and cannot be a variation (a calibrated exit needs its table).
    A finalist of a plain theory (GV3-S2) carries neither: it is a variation with exits {} (the theory's own exits)."""
    rows = [{k: v for k, v in f.items() if k != "calibration"} | ({"calibration_buckets": f["calibration"]["buckets"]} if "calibration" in f else {})
            for f in finalists]
    usable = [f for f in finalists if "calibration" in f or "calibration_error" not in f]
    variations = [{"id": i, "picks": f["picks"], "exits": {"calibration": f["calibration"]} if "calibration" in f else {}} for i, f in enumerate(usable)]
    pick_var = [i for i, f in enumerate(usable) if f["picks"] == pick["picks"]]
    return rows, variations, pick_var


def step1_best(scratch: Scratch, run_id: int) -> list:
    """The best 5 percent of step 1: the strategies on the screen window that cleared the 30-trade bar, best Sharpe first."""
    import math

    import pandas as pd
    trials = pd.read_parquet(scratch.root / "art" / str(run_id) / "trials.parquet")
    s1 = trials[(trials["window"] == "screen") & (trials["step"] == 1)]
    cleared = s1[(s1["num_trades"] >= 30) & s1["sharpe"].notna()]
    keep = math.ceil(len(cleared) * 5 / 100)
    best = cleared.sort_values("sharpe", ascending=False, kind="mergesort").head(keep)
    return [{"picks": json.loads(r.picks), "sharpe": float(r.sharpe), "num_trades": int(r.num_trades)} for r in best.itertuples()]


def step2_box(scratch: Scratch, run_id: int) -> dict:
    """Step 2's box as the trials show it: per letter, the lowest and highest value among the step 2 strategies."""
    import pandas as pd
    trials = pd.read_parquet(scratch.root / "art" / str(run_id) / "trials.parquet")
    s2 = trials[(trials["window"] == "screen") & (trials["step"] == 2)]
    box = {}
    for picks in s2["picks"]:
        for letter, value in json.loads(picks).items():
            lo, hi = box.get(letter, (value, value))
            box[letter] = (min(lo, value), max(hi, value))
    return {"strategies": int(len(s2)), "letters": {k: list(v) for k, v in box.items()}}


def optimise_trades(scratch: Scratch, run_id: int, variation_id: int, label: str) -> list:
    import pandas as pd
    path = scratch.root / "art" / str(run_id) / ("replay_trades_v%d_%s.parquet" % (variation_id, label))
    trades = pd.read_parquet(path)
    return [{"ticker": r.ticker, "entry_date": pd.Timestamp(r.entry_index).strftime("%Y-%m-%d")} for r in trades.itertuples()]


def theory_grid(tree: Path, theory_path: Path) -> dict:
    from research_lab.theories import form
    from research_lab.theory_search import sample
    theory = form.load(theory_path)
    letters, _rules = sample.letters_and_rules(theory)
    return {letter: list(sample.axis_values(theory, letter)) for letter in letters}


def _clean(value):
    """JSON-safe: NaN and infinity become null."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def write_result(out_dir: Path, result: dict) -> None:
    (out_dir / "result.json").write_text(json.dumps(_clean(result), indent=2, sort_keys=True, default=str, allow_nan=False) + "\n", encoding="utf-8")


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m research_lab.theory_e2e.run")
    p.add_argument("--out", required=True, help="folder for result.json, answer.csv, summary.json")
    p.add_argument("--groups", default=",".join(GROUPS), help="comma list, in this order")
    p.add_argument("--env-file", default=None, help="an environment file to load; refused when it is the systemd worker's")
    p.add_argument("--n-iter", type=int, default=SEARCH_N_ITER, help="O-U paths per exit mesh (default: GV2-5b's 100,000)")
    p.add_argument("--search-workers", type=int, default=4, help="processes inside the search container (GV2-5b's Real check used 4)")
    p.add_argument("--search-limit-s", type=float, default=7200)
    p.add_argument("--stage-limit-s", type=float, default=3600)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        guard.check_local_only(os.environ, args.env_file)
    except guard.RefusedToStart as exc:
        print("run.py refuses to start: %s" % exc, file=sys.stderr)
        return 2
    if args.env_file is not None:
        print("run.py: --env-file is checked but never loaded; the scratch settings are built in code", file=sys.stderr)
    tree = Path(__file__).resolve().parents[2]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = [g for g in args.groups.split(",") if g]
    scratch = Scratch(tree, free_port())
    os.environ.update(scratch.env)                   # this process dispatches on the scratch research.db
    log("scratch folder %s, backend %s" % (scratch.root, scratch.url))
    syn_python = sys.executable
    for cmd in (["make", "--out", str(scratch.root / "syn"), "--seed", str(SEED)], ["answer", "--data", str(scratch.root / "syn")]):
        subprocess.run([syn_python, "-m", "research_lab.synthetic"] + cmd, cwd=str(tree), env={**os.environ, "PYTHONPATH": str(tree)}, check=True,
                       capture_output=True)
    for name in ("answer.csv", "summary.json"):
        (out_dir / name).write_bytes((scratch.root / "syn" / name).read_bytes())
    from research_lab.exits import otr
    theory_path = tree / "research_lab" / "theories" / THEORY
    theory_text = theory_path.read_text(encoding="utf-8")
    result = {"seed": SEED, "synthetic": True, "scratch": str(scratch.root), "grid": theory_grid(tree, theory_path),
              "settings": {"search": tomllib.loads(theory_text).get("search"), "n_iter": args.n_iter, "onc_cap": SEARCH_ONC_CAP, "costs": COSTS, "init_cash": INIT_CASH,
                           "windows": {"screen": SCREEN, "optimise": OPTIMISE, "walkforward": WALKFORWARD}, "search_workers": args.search_workers,
                           "exit_mesh_top_sigma": float(max(otr.MESH_MULTIPLES))},
              "groups": {}, "errors": {}, "started_at": datetime.now().isoformat(timespec="seconds")}
    backend = worker = None
    began = time.time()
    try:
        backend = start_backend(tree, scratch)
        worker = start_worker(tree, scratch)
        http = Http(scratch.url)
        from dashboard.backend import stage_runner   # imported after the scratch environment is set
        codes = ["SYN_%s_%02d" % (g[4:], i) for g in groups for i in range(1, 7)]
        t0 = time.time()
        register_datasets(http, scratch, codes)
        result["seconds_datasets"] = time.time() - t0
        log("24 datasets ready" if len(codes) == 24 else "%d datasets ready" % len(codes))
        if any(epoch(w[1]) >= epoch(RESERVE_START) for w in (SCREEN, OPTIMISE, WALKFORWARD)):
            raise StepFailed("a window reaches the reserve")
        limits = {"search": args.search_limit_s, "stage": args.stage_limit_s, "n_iter": args.n_iter}
        for group in groups:
            members = ["SYN_%s_%02d" % (group[4:], i) for i in range(1, 7)]
            searched = group_split(members, SEED)["searched"]   # the screen universe holds the whole group (the search splits it); the stages test the searched stocks only (GV3-F)
            universes = {stage: make_universe(http, "%s %s" % (group, stage), tickers, window)
                         for stage, tickers, window in (("screen", members, SCREEN), ("optimise", searched, OPTIMISE), ("walkforward", searched, WALKFORWARD))}
            try:
                result["groups"][group] = run_group(http, scratch, {"backend": backend, "worker": worker}, theory_text, group, members, universes,
                                                     stage_runner.dispatch_theory_stage, args.search_workers, limits)
            except StepFailed as exc:
                result["errors"][group] = str(exc)
                log("%s: STEP FAILED: %s" % (group, exc))
            write_result(out_dir, result)
    finally:
        result["seconds_total"] = time.time() - began
        write_result(out_dir, result)
        for proc in (worker, backend):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for proc in (worker, backend):
            if proc is not None:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
    from research_lab.theory_e2e import score
    scored = score.score(result, out_dir / "answer.csv", out_dir / "summary.json")
    (out_dir / "score.json").write_text(json.dumps(scored, indent=2, default=str) + "\n", encoding="utf-8")
    for line in scored["lines"]:
        log("(%s) %s  %s  %s" % (line["line"], "TRUE " if line["ok"] else "FALSE", line["text"], json.dumps(line["numbers"], default=str)[:300]))
    for name, extra in scored["extras"].items():
        log("%s: check 1 %s; targets at the mesh top: %s" % (name, json.dumps(extra["check1_dof"], default=str), json.dumps(extra["targets_at_mesh_top"])))
    log("passed: %s" % scored["passed"])
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
