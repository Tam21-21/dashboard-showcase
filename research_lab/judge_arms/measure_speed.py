"""Time one theory run and check 7's re-runs (lane GV3-SP, DECISIONS #469 item 3). Find only: nothing in the pipeline changes.

Run it inside research-runner:m3-latest with the tree mounted at /work and at /pkg/research_lab (the theory_run probes' pattern; the stage
probe dir is read from the tree by path, as measure_rerun.py does). It makes GV2-1's synthetic set (seed 20261012) under <out>/data.

  python -m research_lab.judge_arms.measure_speed --size small|large --fees ibkr_fixed|alpaca --repeats N --out <folder>
      Seconds of variation.prepare plus variation.run for variation 0, one warm-up run and then N timed runs: the median, the smallest and
      the largest. small: the six SYN_VOL stocks, 2017-01-03 to 2019-12-31, data from 2016-01-04. large: SYN_VOL, SYN_TRD, SYN_FLT 01 to 06
      and SYN_TWN 01 to 02, 2004-01-02 to 2016-12-30, data from 2003-01-02 (the planning session's profile of 2026-10-17).

  python -m research_lab.judge_arms.measure_speed --size small --fees alpaca --mcpt M --workers W --out <folder>
      One mcpt_bars.mcpt call of M shuffles over W workers, perform from measure_rerun.make_perform for variation 0: wall seconds and peak
      memory. Peak memory is read from the operating system: ru_maxrss of this process and ru_maxrss of its finished children (the largest
      single child); the sum is the most the group could have held at once, not a measurement of the moment.

Writes only <out>/speed.json (and the synthetic set under <out>/data).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import resource
import statistics
import time

from research_lab.judge_arms import mcpt_bars
from research_lab.judge_arms.measure_rerun import cpu_info, load_spec_module, make_perform, to_long
from research_lab.theory_stage import spec as stage_spec
from research_lab.theory_stage import variation as variation_mod

RESULT_FILE = "speed.json"
SLIPPAGE = 0.0005
LARGE_CODES = (["SYN_VOL_%02d" % i for i in range(1, 7)] + ["SYN_TRD_%02d" % i for i in range(1, 7)]
               + ["SYN_FLT_%02d" % i for i in range(1, 7)] + ["SYN_TWN_%02d" % i for i in range(1, 3)])
LARGE_WINDOW = {"data_start": "2003-01-02", "window_start": "2004-01-02", "window_end": "2016-12-30"}


def build_context(out: str, size: str, fees: str):
    """The stage context for the chosen set and fee model. Returns (ctx, spec)."""
    sp = load_spec_module()
    folder = os.path.join(out, "data")
    os.makedirs(folder, exist_ok=True)
    small_codes = sp.make_set(folder)
    costs = {"fees": fees, "slippage": SLIPPAGE}
    if size == "small":
        spec = sp.spec(folder, small_codes, costs=costs)
    else:
        spec = sp.spec(folder, LARGE_CODES, costs=costs, **LARGE_WINDOW)
    return stage_spec.load_context(spec), spec


def time_runs(ctx, window: tuple, repeats: int) -> dict:
    """prepare plus run of variation 0: one warm-up, then `repeats` timed runs."""
    var = ctx.variations[0]
    variation_mod.run(ctx, variation_mod.prepare(ctx, var, window), window)
    seconds = []
    for _ in range(repeats):
        start = time.perf_counter()
        variation_mod.run(ctx, variation_mod.prepare(ctx, var, window), window)
        seconds.append(time.perf_counter() - start)
    return {"repeats": repeats, "median_s": statistics.median(seconds), "min_s": min(seconds), "max_s": max(seconds)}


def time_mcpt(ctx, window: tuple, spec: dict, m: int, workers: int) -> dict:
    bars = to_long(ctx)
    perform = make_perform(ctx, ctx.variations[0], window)
    start = time.perf_counter()
    result = mcpt_bars.mcpt(perform, bars, spec["window_start"], m=m, seed=1, workers=workers)
    seconds = time.perf_counter() - start
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    return {"m": m, "workers": workers, "wall_s": seconds, "k": result["k"], "p_value": result["p_value"],
            "peak_rss_self_mb": own, "peak_rss_largest_child_mb": children, "peak_rss_sum_upper_bound_mb": own + children * workers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Time one theory run and check 7's re-runs (lane GV3-SP).")
    parser.add_argument("--size", choices=("small", "large"), required=True)
    parser.add_argument("--fees", choices=("ibkr_fixed", "alpaca"), required=True)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--mcpt", type=int, help="M, the number of shuffles; the mcpt timing instead of the run timing")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.mcpt is None and (args.repeats is None or args.repeats < 1):
        parser.error("give --repeats (at least 1) or --mcpt")
    if args.mcpt is not None and (args.mcpt < 1 or args.workers < 1):
        parser.error("--mcpt and --workers must be at least 1")
    os.makedirs(args.out, exist_ok=True)
    ctx, spec = build_context(args.out, args.size, args.fees)
    window = (spec["window_start"], spec["window_end"])
    if args.mcpt is None:
        result = {"size": args.size, "fees": args.fees, "run": time_runs(ctx, window, args.repeats)}
        flat = [result["run"]["median_s"], result["run"]["min_s"], result["run"]["max_s"]]
    else:
        result = {"size": args.size, "fees": args.fees, "mcpt": time_mcpt(ctx, window, spec, args.mcpt, args.workers)}
        flat = [result["mcpt"]["wall_s"], result["mcpt"]["peak_rss_self_mb"]]
    result["cpu"] = cpu_info()
    if not all(math.isfinite(v) and v > 0 for v in flat):
        raise RuntimeError("a timing is not finite and above 0: %s" % json.dumps(result))
    with open(os.path.join(args.out, RESULT_FILE), "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
