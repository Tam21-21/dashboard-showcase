"""Job handlers: run_theory_search and run_theory_stage (lane GV2-7b, DECISIONS #445).

The worker side of generator v2's search, optimise stage and walk-forward stage. Like run_sweep it never imports vbt and
touches no portfolio: it writes the spec, starts research-runner:m3-latest with this tree's research_lab mounted read-only,
waits, reads the files the container wrote, and posts them to the backend (the container has --network=none, and the worker
has no database).

Per job:
  1. build the container's spec from the payload (build_spec) and write it, with the theory file for the search, to /tmp/run_{job_id}/;
  2. docker run (build_docker_cmd): research_lab at /work and /pkg/research_lab (read-only; /work first on sys.path so the image's
     baked `strategies` package does not shadow the tree's copy, /pkg so `research_lab.theory_stage` imports under its own name),
     each member's source folder read-only, the spec, an output folder at /out, --network=none;
  3. relay what the container wrote, in an order that flips the run to succeeded last where the plan allows:
       search      the five files as artifacts, family.json to POST /theory-family, then sweep-result;
       optimise    the cells (merged with their funnel rows) to sweep-chunk, the files as artifacts, one full-metrics-cache
                   payload per variation (with its replay object's replayed_at), then sweep-result;
       walkforward sweep-result, equity.parquet and walkforward.json as artifacts, then walkforward-result.
  4. a non-zero container exit fails the run and the job, with the tail of the container's output. Any other fault (a refused POST)
     is raised to worker.handle_job's backstop, which fails the run and the job; there is no catch-all here.

These kinds have no cancel path, so a heartbeat task runs beside the container and the relay and the cancel flag is not read.
"""
from __future__ import annotations

import asyncio
import collections
import importlib.util
import json
import shutil
import socket
import time
from pathlib import Path

from jobs.run_backtest import DATA_ROOT, DOCKER_IMAGE, NUMBA_CACHE, STDOUT_STREAM_LIMIT, dataset_source, prepare_run_dir
from jobs.run_sweep import SWEEP_CPUS  # the same CPU budget as a sweep (operational dial, justified there)

THEORY_MEMORY = "6g"  # DECISIONS #457: a theory run holds its ONC matrices in the parent, so it gets 6g, not a sweep's cap

RESEARCH_LAB_DIR = Path(__file__).resolve().parents[2]  # <root>/research_lab/worker/jobs/run_theory.py -> <root>/research_lab

KIND_SEARCH = "run_theory_search"
KIND_STAGE = "run_theory_stage"
KIND_MODULES = {KIND_SEARCH: "research_lab.theory_search.run", KIND_STAGE: "research_lab.theory_stage.run"}

CONTAINER_SPEC = "/app/theory_spec.json"
CONTAINER_THEORY = "/app/theory.toml"
CONTAINER_OUT = "/out"
FAILURE_TAIL_LINES = 20  # how many of the container's last output lines a failed run's error carries (a display length, not a backtest input)

SEARCH_FILES = ("family.json", "trials.parquet", "returns.parquet", "finalists.json", "pick.json", "rounds.json", "screen_dates.json")
CSCV_FILE = "cscv_blocks.parquet"
OPTIMISE_COMMON_FILES = ("cells.json", "funnel.json", "variations.json", "annualization.json", CSCV_FILE)
MCPT_M = 4999  # judge_checks._MCPT_REPLICATION_COUNT (#376 decision 2): check 7 grades only this many shuffled markets
MCPT_WORKERS = int(SWEEP_CPUS)  # the sweep's CPU budget (jobs/run_sweep.py), the processes that score the shuffled markets
FULLMETRICS_MODULE = RESEARCH_LAB_DIR / "strategies" / "helpers" / "full_metrics.py"


def build_spec(kind: str, payload: dict) -> dict:
    """The container's spec, from the job payload. Member files are the dataset paths the container sees (/data/<source>/...)."""
    files = {m["ticker"]: m["dataset_path"] for m in payload["members"]}
    if kind == KIND_SEARCH:
        spec = {
            "theory": CONTAINER_THEORY, "source_shape": "stored", "files": files, "group_label": payload["group_label"],
            "seed": payload["seed"], "n_iter": payload["n_iter"], "onc_cap": payload["onc_cap"],
            "screen_window": payload["screen_window"], "optimise_window": payload["optimise_window"],
            "init_cash": payload["init_cash"], "costs": payload["costs"],
        }
        if "workers" in payload:   # the round sizes live in the theory's [search] (#470), never in a payload
            spec["workers"] = payload["workers"]
        if "final" in payload:     # GV3-F: the kept-back stocks and the later window; the container splits the group and refuses a bad one by name
            spec["final"] = payload["final"]
        _add_earnings(spec, payload)
        return spec
    if kind != KIND_STAGE:
        raise ValueError(f"not a theory job kind: {kind!r}")
    spec = {
        "theory_toml": payload["theory_toml"], "group_label": payload["group_label"], "stage": payload["stage"],
        "source_shape": "stored", "files": files, "member_sha256": {m["ticker"]: m["sha256"] for m in payload["members"]},
        "data_start": payload["data_start"], "window_start": payload["window_start"], "window_end": payload["window_end"],
        "variations": payload["variations"], "costs": payload["costs"], "init_cash": payload["init_cash"], "seed": payload["seed"],
    }
    if payload["stage"] == "optimise":
        spec["cscv_s"] = payload["cscv_s"]
        spec["mcpt"] = {"m": MCPT_M, "workers": MCPT_WORKERS}
    elif payload["stage"] == "walkforward":
        spec.update({k: payload["walkforward"][k] for k in ("in_sample_bars", "out_of_sample_bars", "anchoring")})
    else:
        raise ValueError(f"a theory stage is optimise or walkforward, got {payload['stage']!r}")
    _add_earnings(spec, payload)
    return spec


def _add_earnings(spec: dict, payload: dict) -> None:
    """EA4: a theory that reads an earnings field has its slice mounted at /data/events/earnings/<run_id>.parquet; the container gets
    that path and each member's CIK (one.load_group reads spec["earnings"])."""
    if "earnings" in payload:
        spec["earnings"] = {"path": f"/data/events/earnings/{payload['run_id']}.parquet", "ciks": payload["earnings"]["ciks"]}


def build_docker_cmd(kind: str, members: list, tmp_dir: Path, out_dir: Path, research_lab_dir: Path = RESEARCH_LAB_DIR,
                     event_mounts=()) -> list:
    """The docker run argv: --network=none, research_lab read-only at /work and /pkg/research_lab, one read-only mount per
    distinct member source folder, one per event slice (`event_mounts`, host:container:ro specs from
    run_backtest.materialize_event_slices), the spec (and the theory file for the search) read-only, and the output folder read-write."""
    if kind not in KIND_MODULES:
        raise ValueError(f"not a theory job kind: {kind!r}")
    cmd = [
        "docker", "run", "--rm",
        "--network=none",
        "--memory", THEORY_MEMORY,
        "--cpus", SWEEP_CPUS,
        "--user", "1000:1000",
        "-v", f"{research_lab_dir}:/work:ro",
        "-v", f"{research_lab_dir}:/pkg/research_lab:ro",
    ]
    for source in sorted({dataset_source(m["dataset_path"]) for m in members}):
        cmd += ["-v", f"{DATA_ROOT / source}:/data/{source}:ro"]
    for mount in event_mounts:
        cmd += ["-v", mount]
    cmd += ["-v", f"{tmp_dir / 'theory_spec.json'}:{CONTAINER_SPEC}:ro"]
    if kind == KIND_SEARCH:
        cmd += ["-v", f"{tmp_dir / 'theory.toml'}:{CONTAINER_THEORY}:ro"]
    cmd += [
        "-v", f"{out_dir}:{CONTAINER_OUT}:rw",
        "-v", f"{NUMBA_CACHE}:/cache:rw",
        "-e", "NUMBA_CACHE_DIR=/cache",
        "-e", "PYTHONPATH=/work:/pkg",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONHASHSEED=0",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-w", "/work",
        DOCKER_IMAGE,
        "python", "-m", KIND_MODULES[kind], "--spec", CONTAINER_SPEC, "--out", CONTAINER_OUT,
    ]
    return cmd


def formula_version(strategy_version, build_provenance: str) -> str:
    """The full-metrics cache key, by the one function the backend uses (strategies.helpers.full_metrics.resolve_formula_version).

    Loaded from this tree's file by path: the module is standard library only at import (it imports pandas lazily), so this
    needs neither vbt nor a path change."""
    spec = importlib.util.spec_from_file_location("gv2_full_metrics", FULLMETRICS_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_formula_version(strategy_version, build_provenance)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def optimise_cells(out_dir: Path) -> tuple:
    """(cells, member_set_sha256, full_metrics_by_variation) from an optimise output folder.

    A cell is a row of cells.json joined with its funnel.json row, plus what the sweep-chunk route needs: param_json, the canonical
    JSON of the variation's picks (sorted keys, compact separators, as strategies.base.sweep.canonical_param_json), and
    dataset_sha256, the group's member-set identity that the stage wrote into every replay object."""
    cells = _read_json(out_dir / "cells.json")
    funnels = {(f["variation_id"], f["ticker"]): f for f in _read_json(out_dir / "funnel.json")}
    picks = _read_json(out_dir / "variations.json")
    if not cells:
        raise ValueError(f"{out_dir / 'cells.json'} holds no cell")
    metrics = {c["variation_id"]: _read_json(out_dir / f"full_metrics_v{c['variation_id']}.json") for c in cells}
    shas = {m["replay"]["member_dataset_sha256"] for m in metrics.values()}
    if len(shas) != 1:
        raise ValueError(f"the variations' replay objects name {len(shas)} member-set identities, expected one")
    sha = shas.pop()
    merged = []
    for cell in cells:
        key = (cell["variation_id"], cell["ticker"])
        if key not in funnels:
            raise ValueError(f"funnel.json has no row for variation {key[0]} of {key[1]!r}")
        row = {**cell, **{k: v for k, v in funnels[key].items() if k not in cell}}
        row["param_json"] = json.dumps(picks[str(cell["variation_id"])], sort_keys=True, separators=(",", ":"))
        row["dataset_sha256"] = sha
        merged.append(row)
    return merged, sha, metrics


def _environment(provenance: "dict | None") -> dict:
    """The run's environment columns: the container's own python, vbt and build identity when it reported them, and this host."""
    env = {"hostname": socket.gethostname()}
    if provenance is not None:
        env.update({k: provenance[k] for k in ("python_version", "vbt_version", "build_provenance")})
    return env


async def _stream(proc, job_id: int, http, tail: collections.deque) -> None:
    from helpers import report_log  # function scope: keep the module import light for the probe

    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        tail.append(line)
        await report_log(job_id, line, http)


async def _upload(run_id: int, folder: Path, names: list, http) -> None:
    from helpers import report_run_artifacts

    missing = [n for n in names if not (folder / n).is_file()]
    if missing:
        raise FileNotFoundError(f"the container did not write {missing} in {folder}")
    await report_run_artifacts(run_id, {n: folder / n for n in names}, http)


async def _post(http, run_id: int, route: str, body: dict) -> None:
    from helpers import HEADERS, SERVER_URL

    r = await http.post(f"{SERVER_URL}/api/research/worker/runs/{run_id}/{route}", json=body, headers=HEADERS)
    r.raise_for_status()


async def relay_search(run_id: int, out_dir: Path, http) -> None:
    bars_observed = _read_json(out_dir / "bars_observed.json")["total"]  # first: a missing file raises before anything is sent (GV2-8b)
    names = list(SEARCH_FILES) + (["final.json"] if (out_dir / "final.json").is_file() else [])  # GV3-F: only a search with a final writes it
    await _upload(run_id, out_dir, names, http)
    await _post(http, run_id, "theory-family", _read_json(out_dir / "family.json"))
    from helpers import report_sweep_result

    await report_sweep_result(run_id, None, _environment(None), 0, http, bars_observed=bars_observed)


async def relay_optimise(run_id: int, strategy_version, out_dir: Path, http) -> int:
    from helpers import report_full_metrics_cache, report_sweep_chunk, report_sweep_result

    cells, _sha, metrics = optimise_cells(out_dir)
    await report_sweep_chunk(run_id, cells, http)
    await _upload(run_id, out_dir, list(OPTIMISE_COMMON_FILES), http)
    provenance = next(iter(metrics.values()))["provenance"]
    version = formula_version(strategy_version, provenance["build_provenance"])
    for cell in cells:
        vid, label = cell["variation_id"], cell["ticker"]
        payload = metrics[vid]
        payload["replay"]["replayed_at"] = int(time.time())
        names = [f"full_metrics_v{vid}.json"] + [a["name"] for a in payload["replay"]["artifacts"].values()]
        names += [a["name"] for a in payload["replay"]["buy_hold_artifacts"].values()] if payload["replay"].get("buy_hold_artifacts") else []
        await _upload(run_id, out_dir, names, http)
        await report_full_metrics_cache(run_id, vid, label, version, payload, http)
    annualization = _read_json(out_dir / "annualization.json")
    await report_sweep_result(run_id, annualization, _environment(provenance), len(cells), http)
    return len(cells)


async def relay_walkforward(run_id: int, out_dir: Path, http) -> None:
    from helpers import report_sweep_result

    bars_observed = _read_json(out_dir / "bars_observed.json")["total"]  # first: a missing file raises before anything is sent (GV2-8b)
    body = _read_json(out_dir / "walkforward.json")
    await report_sweep_result(run_id, None, _environment(None), 0, http, bars_observed=bars_observed)
    await _upload(run_id, out_dir, ["equity.parquet", "walkforward.json"], http)
    await _post(http, run_id, "walkforward-result", {k: body[k] for k in (
        "slices", "in_sample_profit", "out_of_sample_profit", "wfa_boundary_buffer_bars", "total_return", "max_drawdown", "num_trades",
        "total_fees_paid", "total_slippage_cost", "slices_used", "slices_with_absent_profit")})


def _clear(folder: Path) -> None:
    """Empty the run's output folder, so a retried job never relays a file an earlier attempt left behind."""
    for child in folder.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()


async def run(job: dict, http) -> None:
    """Drive one theory job: spec, container, relay (see the module docstring)."""
    from helpers import heartbeat_loop, report_job_completed, report_job_failed, report_run_failed, stop_heartbeat

    job_id, kind, payload = job["id"], job["kind"], job["payload"]
    run_id = payload["run_id"]  # from the payload: the worker has no database
    spec = build_spec(kind, payload)
    tmp_dir = Path(f"/tmp/run_{job_id}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "theory_spec.json").write_text(json.dumps(spec), encoding="utf-8")
    if kind == KIND_SEARCH:
        (tmp_dir / "theory.toml").write_text(payload["theory_toml"], encoding="utf-8")
    out_dir = prepare_run_dir(run_id)
    _clear(out_dir)
    heartbeat = asyncio.create_task(heartbeat_loop(job_id, kind, http))
    try:
        event_mounts = []
        if "earnings" in payload:  # EA4: stage the members' earnings slice before the container; a failure starts no container
            from jobs import run_backtest  # function scope: the probe patches the module's attribute

            earnings = payload["earnings"]
            try:
                event_mounts = await run_backtest.materialize_event_slices(run_id, [{
                    "event_type": "earnings", "ciks": sorted(earnings["ciks"].values()),
                    "knowledge_from": earnings["knowledge_from"], "knowledge_to": earnings["knowledge_to"]}], http)
            except run_backtest.EventMaterializeError as exc:
                error = f"event data materialization failed: {exc}"
                await report_run_failed(run_id, error, http)
                await report_job_failed(job_id, error, http)
                return
        proc = await asyncio.create_subprocess_exec(
            *build_docker_cmd(kind, payload["members"], tmp_dir, out_dir, event_mounts=event_mounts),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=STDOUT_STREAM_LIMIT)
        tail = collections.deque(maxlen=FAILURE_TAIL_LINES)
        await _stream(proc, job_id, http, tail)
        rc = await proc.wait()
        if rc != 0:
            error = f"theory container exited {rc}: " + " | ".join(tail)
            await report_run_failed(run_id, error, http)
            await report_job_failed(job_id, error, http)
            return
        result = {"run_id": run_id}
        if kind == KIND_SEARCH:
            await relay_search(run_id, out_dir, http)
        elif payload["stage"] == "optimise":
            result["total_cells"] = await relay_optimise(run_id, payload["strategy_version"], out_dir, http)
        else:
            await relay_walkforward(run_id, out_dir, http)
        await report_job_completed(job_id, result, http)
    finally:
        await stop_heartbeat(heartbeat, job_id, kind)
        shutil.rmtree(tmp_dir, ignore_errors=True)
