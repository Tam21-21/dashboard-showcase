"""Job handler: run_family_union (lane GV3-FU1).

The merged trial family of several theory searches on one stock set and screen window (AFML p. 275: N and V count every trial tried
on the same data). Like run_theory it never imports vbt or pandas: it fetches each run's four search files over HTTP, writes the spec,
starts research-runner:m3-latest (the libraries each search's family used live there) with no network, and posts union.json.

Per job:
  1. fetch trials.parquet, returns.parquet, family.json and screen_dates.json of every run in the payload into <tmp>/in/<run_id>;
  2. write the spec (runs, onc_cap, seed, member_set_sha256, window_start, window_end) and run
     `python -m research_lab.theory_search.union --spec --in /in --out /out` (research_lab read-only at /work and /pkg/research_lab,
     <tmp>/in read-only, <tmp>/out the only writable mount, no data mounts, --network=none);
  3. exit 0: POST union.json plus job_id to /api/research/worker/family-unions, then report the job completed; any other exit reports
     the job failed with the tail of the container's output and posts nothing.
A refused fetch or post is raised to worker.handle_job's backstop, which fails the job; there is no catch-all here.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from jobs.compute_run_metrics import _fetch_artifacts
from jobs.run_backtest import DOCKER_IMAGE, STDOUT_STREAM_LIMIT
from jobs.run_sweep import SWEEP_CPUS
from jobs.run_theory import FAILURE_TAIL_LINES, RESEARCH_LAB_DIR, THEORY_MEMORY

FETCH_FILES = ["trials.parquet", "returns.parquet", "family.json", "screen_dates.json"]
SPEC_KEYS = ("runs", "onc_cap", "seed", "member_set_sha256", "window_start", "window_end")
CONTAINER_SPEC = "/app/union_spec.json"
MODULE = "research_lab.theory_search.union"


def build_docker_cmd(tmp_dir: Path, research_lab_dir: Path = RESEARCH_LAB_DIR) -> list:
    return [
        "docker", "run", "--rm",
        "--network=none",
        "--memory", THEORY_MEMORY,
        "--cpus", SWEEP_CPUS,
        "--user", "1000:1000",
        "-v", f"{research_lab_dir}:/work:ro",
        "-v", f"{research_lab_dir}:/pkg/research_lab:ro",
        "-v", f"{tmp_dir / 'union_spec.json'}:{CONTAINER_SPEC}:ro",
        "-v", f"{tmp_dir / 'in'}:/in:ro",
        "-v", f"{tmp_dir / 'out'}:/out:rw",
        "-e", "PYTHONPATH=/work:/pkg",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "PYTHONHASHSEED=0",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-w", "/work",
        DOCKER_IMAGE,
        "python", "-m", MODULE, "--spec", CONTAINER_SPEC, "--in", "/in", "--out", "/out",
    ]


async def _post_union(job_id: int, body: dict, http) -> None:
    from helpers import HEADERS, SERVER_URL

    r = await http.post(f"{SERVER_URL}/api/research/worker/family-unions", json={**body, "job_id": job_id}, headers=HEADERS)
    r.raise_for_status()


async def run(job: dict, http) -> None:
    from helpers import heartbeat_loop, report_job_completed, report_job_failed, stop_heartbeat

    job_id, kind, payload = job["id"], job["kind"], job["payload"]
    spec = {k: payload[k] for k in SPEC_KEYS}
    tmp_dir = Path(f"/tmp/family_union_{job_id}")
    shutil.rmtree(tmp_dir, ignore_errors=True)  # a retried job never reads an earlier attempt's files
    (tmp_dir / "out").mkdir(parents=True)
    heartbeat = asyncio.create_task(heartbeat_loop(job_id, kind, http))
    try:
        for run_id in spec["runs"]:
            await _fetch_artifacts(run_id, FETCH_FILES, tmp_dir / "in" / str(run_id), http)
        (tmp_dir / "union_spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (tmp_dir / "out").chmod(0o777)  # the container's user 1000 writes union.json here
        proc = await asyncio.create_subprocess_exec(
            *build_docker_cmd(tmp_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=STDOUT_STREAM_LIMIT)
        output, _ = await proc.communicate()
        if proc.returncode != 0:
            tail = output.decode("utf-8", errors="replace").splitlines()[-FAILURE_TAIL_LINES:]
            await report_job_failed(job_id, f"family union container exited {proc.returncode}: " + " | ".join(tail), http)
            return
        body = json.loads((tmp_dir / "out" / "union.json").read_text(encoding="utf-8"))
        await _post_union(job_id, body, http)
        await report_job_completed(job_id, {"run_ids": body["run_ids"]}, http)
    finally:
        await stop_heartbeat(heartbeat, job_id, kind)
        shutil.rmtree(tmp_dir, ignore_errors=True)
