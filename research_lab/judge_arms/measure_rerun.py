"""Time one judge re-run of a generated strategy (lane JG2-0, DECISIONS #451). The timing script is find only; make_perform and to_long
are check 7's re-run in the optimise stage (research_lab/theory_stage/optimise.py mcpt_entry, lane GV3-J7).

Check 7 re-runs the strategy on shuffled prices (research_lab.judge_arms.mcpt_bars); this script measures what one re-run costs, on GV2-1's
synthetic set (seed 20261012, SYN_VOL members) with the spec of research_lab/probes/theory_stage/_spec.py (capitulation_rebound, window
2017-01-03 to 2019-12-31, its three variations and costs). Run it inside research-runner:m3-latest with the tree mounted at /work and at
/pkg/research_lab (the probes' pattern); _spec.py reads the theory from /pkg/research_lab/theories and makes the set from /pkg.

  python -m research_lab.judge_arms.measure_rerun --repeats 30 --m 20 --out <empty or new folder>

Two timings:
  prepare_run  variation.prepare then variation.run for each variation, --repeats times: the cost of one re-run when the prices are as they are.
  mcpt         one mcpt_bars.mcpt call with m shuffles whose perform rebuilds the wide frames from the shuffled long bars and runs prepare then
               run for variation 0 (the factor, when ctx has one, is attached by date: lane ADJ1b). The call does m + 1 runs (the original and m shuffled), so one re-run costs seconds / (m + 1).
A fresh calibration per shuffled market is not timed: its inputs need a screen run (a later lane).

Writes only inside --out: the synthetic set in <out>/data and the result in <out>/rerun_timing.json.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import math
import os
import resource
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from research_lab.judge_arms import mcpt_bars
from research_lab.theory_stage import spec as stage_spec
from research_lab.theory_stage import variation as variation_mod

RESULT_FILE = "rerun_timing.json"
SPEC_FILE = Path(__file__).resolve().parents[1] / "probes" / "theory_stage" / "_spec.py"
RERUNS = 5000
FIELDS = ("open", "high", "low", "close", "volume")


def load_spec_module():
    """_spec.py, imported from the probe dir by path (never copied). It imports its sibling _fx, so the probe dir goes on sys.path."""
    if not SPEC_FILE.is_file():
        raise FileNotFoundError("the stage spec builder is not at %s" % SPEC_FILE)
    sys.path.insert(0, str(SPEC_FILE.parent))
    module_spec = importlib.util.spec_from_file_location("theory_stage_spec", SPEC_FILE)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def build_context(out: str) -> tuple:
    """Make the synthetic set under <out>/data and load the stage context for _spec.py's spec. Returns (ctx, spec)."""
    sp = load_spec_module()
    folder = os.path.join(out, "data")
    os.makedirs(folder, exist_ok=True)
    codes = sp.make_set(folder)
    spec = sp.spec(folder, codes)
    return stage_spec.load_context(spec), spec


def time_prepare_run(ctx, window: tuple, repeats: int) -> dict:
    """Seconds of prepare plus run per variation, `repeats` times each: {variation id as text: {median_s, max_s}}."""
    out = {}
    for var in ctx.variations:
        seconds = []
        for _ in range(repeats):
            start = time.perf_counter()
            prepared = variation_mod.prepare(ctx, var, window)
            variation_mod.run(ctx, prepared, window)
            seconds.append(time.perf_counter() - start)
        out[str(var["id"])] = {"median_s": statistics.median(seconds), "max_s": max(seconds)}
    return out


def to_long(ctx) -> dict:
    """ctx's wide frames (field by date by ticker) as one long frame per ticker: date, open, high, low, close, volume.
    Sessions where the stock has no price are dropped; dates are naive."""
    frames = ctx.frames
    out = {}
    for ticker in frames["close"].columns:
        long = pd.DataFrame({name: frames[name][ticker] for name in FIELDS}).dropna()
        index = pd.DatetimeIndex(long.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        long.insert(0, "date", index)
        out[ticker] = long.reset_index(drop=True)[mcpt_bars.COLUMNS]
    return out


def make_perform(ctx, var: dict, window: tuple):
    """perform(bars) for mcpt_bars.mcpt: rebuild the wide frames from the long bars in a copy of ctx, then prepare and run `var`.

    The factor (ctx.frames["factor"], adjusted price / price as traded) is kept by date and stock: each fake bar takes the real factor of
    its own date and stock. mcpt_bars moves the price changes and never the dates, and a factor counts the dividends and splits after that
    day, so it stays on its real date; fees go on real shares at the real price as in simulate.run_group (lane ADJ1, #422, DECISIONS #486
    item 4 B2). A factor frame with no value where the rebuilt close has one raises ValueError. With no factor frame nothing changes."""
    index_name = ctx.frames["close"].index.name
    factor = ctx.frames.get("factor")
    if factor is not None:
        factor = factor.copy()
        if factor.index.tz is not None:
            factor.index = factor.index.tz_localize(None)

    def rerun_sharpe(bars: dict) -> tuple:
        wide = {}
        for name in FIELDS:
            wide[name] = pd.DataFrame({t: f.set_index("date")[name] for t, f in bars.items()})
            wide[name].index.name = index_name
        if factor is not None:
            f = factor.reindex(index=wide["close"].index, columns=wide["close"].columns)
            missing = f.isna() & wide["close"].notna()
            if missing.to_numpy().any():
                row, col = np.argwhere(missing.to_numpy())[0]
                raise ValueError("the factor frame has no value for %s on %s (%d cells); check 7's re-run charges fees on real shares (lane ADJ1)"
                                 % (wide["close"].columns[col], wide["close"].index[row].date(), int(missing.to_numpy().sum())))
            f.index.name = index_name
            wide["factor"] = f
        fresh = dataclasses.replace(ctx, frames=wide)
        res = variation_mod.run(fresh, variation_mod.prepare(fresh, var, window), window)
        return res.sharpe, res.num_trades

    return rerun_sharpe


def cpu_info() -> dict:
    model = None
    with open("/proc/cpuinfo", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    if model is None:
        raise RuntimeError("/proc/cpuinfo has no 'model name' line")
    return {"model": model, "cores": os.cpu_count()}


def time_mcpt(ctx, window: tuple, spec: dict, m: int) -> dict:
    bars = to_long(ctx)
    perform = make_perform(ctx, ctx.variations[0], window)
    start = time.perf_counter()
    result = mcpt_bars.mcpt(perform, bars, spec["window_start"], m=m, seed=1)
    seconds = time.perf_counter() - start
    return {"seconds": seconds, "m": m, "projected_5000_s": seconds / (m + 1) * RERUNS, "dropped_dates": result["dropped_dates"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Time one judge re-run of a generated strategy (lane JG2-0).")
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--m", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.m < 1:
        parser.error("--repeats and --m must be at least 1")
    os.makedirs(args.out, exist_ok=True)
    ctx, spec = build_context(args.out)
    window = (spec["window_start"], spec["window_end"])
    prepare_run = time_prepare_run(ctx, window, args.repeats)
    mcpt = time_mcpt(ctx, window, spec, args.m)
    result = {
        "prepare_run": prepare_run,
        "mcpt": mcpt,
        "cpu": cpu_info(),
        "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
    }
    flat = [v for e in prepare_run.values() for v in e.values()] + [mcpt["seconds"], mcpt["projected_5000_s"], result["peak_rss_mb"]]
    if not all(math.isfinite(v) and v > 0 for v in flat):
        raise RuntimeError("a timing is not finite and above 0: %s" % json.dumps(result))
    with open(os.path.join(args.out, RESULT_FILE), "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
