"""python -m research_lab.theory_search.measure --spec <spec.json> --out <dir> [--sizes 500,2000,10000] [--caps 10,50,100]  (lane GV2-5b, plan steps 3 and 7)

Measures the budget that DECISIONS #440 says to measure before the search size is chosen:
  sizes  step 1 at each size, on the spec's screen window: wall time, peak memory, strategies done. A size whose step 1 reaches
         30 minutes is stopped there and never run to the end; the search size is the largest size that finished. Sizes run
         in increasing order, each in its own process (so each peak memory is its own); the first stopped size ends the list.
  caps   ONC on the trials of the chosen size, at each cap: wall time and K; a cap that reaches 20 minutes is stopped; the
         ONC cap is the largest that finished.
It never reads the answer sheet. Writes measure.json into <dir> (and the step 1 returns of each size, for the cap runs) and
prints a table. Exit 3 when no size finished or no cap finished: the plan's stop-and-report cases.
The spec is the same as run.py's (size, onc_cap and the windows' other keys are read; `size` and `onc_cap` are overridden).
"""
from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from research_lab.theory_search import family, onc, sample, search

SIZE_LIMIT_SECONDS = 30 * 60   # step 3: a size whose step 1 reaches 30 minutes is stopped (plan step 3)
CAP_LIMIT_SECONDS = 20 * 60    # step 7: a cap whose ONC reaches 20 minutes is stopped (plan step 7)


def _peak_mb(which: int) -> float:
    return resource.getrusage(which).ru_maxrss / 1024.0   # Linux reports kilobytes


def size_one(spec: dict, out: Path, size: int) -> dict:
    started = time.monotonic()
    ctx = search.load_context(spec)
    evaluator = search.Evaluator(spec, ctx)
    window = tuple(spec["screen_window"])
    rows = sample.balanced_sample(ctx.theory, size, int(spec["seed"]))
    status, done = "finished", len(rows)
    try:
        results = evaluator.evaluate(rows, window, deadline=started + SIZE_LIMIT_SECONDS)
    except search.StepTimeout as exc:
        status, done, results = "stopped at 30 minutes", None, None
        note = str(exc)
    finally:
        evaluator.close()
    seconds = time.monotonic() - started
    out_row = {"size": size, "status": status, "seconds": round(seconds, 1), "strategies_done": done,
               "peak_main_mb": round(_peak_mb(resource.RUSAGE_SELF), 1), "peak_worker_mb": round(_peak_mb(resource.RUSAGE_CHILDREN), 1),
               "workers": evaluator.workers}
    if results is None:
        out_row["note"] = note
        return out_row
    table = pd.DataFrame({"sharpe": [r["sharpe"] for r in results], "num_trades": [r["num_trades"] for r in results]})
    out_row["cleared_bar"] = int(((table["num_trades"] >= search.MIN_TRADES) & table["sharpe"].notna()).sum())
    np.savez(out / ("step1_%d.npz" % size), returns=np.stack([r["bar_returns"] for r in results], axis=1),
             num_trades=np.array([r["num_trades"] for r in results]), dates=np.array(results[0]["dates"].strftime("%Y-%m-%d"), dtype="U10"))
    return out_row


def cap_one(spec: dict, out: Path, size: int, cap: int) -> dict:
    data = np.load(out / ("step1_%d.npz" % size), allow_pickle=True)  # our own file; older ones hold the dates as objects
    returns = pd.DataFrame(data["returns"], index=pd.DatetimeIndex(data["dates"]), columns=list(range(data["returns"].shape[1])))
    trials = pd.DataFrame({"trial_id": list(returns.columns), "num_trades": data["num_trades"]})
    started = time.monotonic()
    try:
        summary = family.family_summary(trials, returns, onc_cap=cap, seed=int(spec["seed"]), selected=None, deadline=started + CAP_LIMIT_SECONDS)
    except onc.OncTimeout:
        return {"cap": cap, "status": "stopped at 20 minutes", "seconds": round(time.monotonic() - started, 1),
                "peak_mb": round(_peak_mb(resource.RUSAGE_SELF), 1), "trials": len(trials)}
    return {"cap": cap, "status": "finished", "seconds": round(time.monotonic() - started, 1), "peak_mb": round(_peak_mb(resource.RUSAGE_SELF), 1),
            "trials": len(trials), "n_in_onc": summary["n_in_onc"], "k_effective": summary["k_effective"], "cap_bound": summary["cap_bound"],
            "reason": summary["reason"]}


def _child(args: list) -> dict:
    proc = subprocess.run([sys.executable, "-m", "research_lab.theory_search.measure"] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("measure %s failed: %s" % (args[0], (proc.stdout + proc.stderr)[-1500:]))
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.theory_search.measure")
    parser.add_argument("command", nargs="?", choices=("size-one", "cap-one"))
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--sizes", default="500,2000,10000")
    parser.add_argument("--caps", default="10,50,100")
    parser.add_argument("--size", type=int)
    parser.add_argument("--cap", type=int)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "size-one":
        print(json.dumps(size_one(spec, args.out, args.size)))
        return 0
    if args.command == "cap-one":
        print(json.dumps(cap_one(spec, args.out, args.size, args.cap)))
        return 0

    base = ["--spec", str(args.spec), "--out", str(args.out)]
    sizes, chosen = [], None
    for size in [int(s) for s in args.sizes.split(",")]:
        row = _child(["size-one"] + base + ["--size", str(size)])
        sizes.append(row)
        print("size %6d: %s, %.1f s, %s strategies, peak %.0f MB (main) %.0f MB (worker)" % (
            size, row["status"], row["seconds"], row["strategies_done"], row["peak_main_mb"], row["peak_worker_mb"]), flush=True)
        if row["status"] != "finished":
            break
        chosen = size
    caps, chosen_cap = [], None
    if chosen is not None:
        for cap in [int(c) for c in args.caps.split(",")]:
            row = _child(["cap-one"] + base + ["--size", str(chosen), "--cap", str(cap)])
            caps.append(row)
            print("cap %4d: %s, %.1f s, K %s" % (cap, row["status"], row["seconds"], row.get("k_effective")), flush=True)
            if row["status"] != "finished":
                break
            chosen_cap = cap
    report = {"sizes": sizes, "search_size": chosen, "caps": caps, "onc_cap": chosen_cap}
    (args.out / "measure.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("search size %s, ONC cap %s" % (chosen, chosen_cap))
    return 0 if chosen is not None and chosen_cap is not None else 3


if __name__ == "__main__":
    sys.exit(main())
