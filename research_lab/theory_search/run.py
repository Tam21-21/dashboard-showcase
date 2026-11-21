"""python -m research_lab.theory_search.run --spec <spec.json> --out <dir>  (lane GV2-5b)

Runs the generator v2 search of one theory on one stock group and writes, into <dir>:
  trials.parquet   one row per trial: trial_id, picks (JSON text), window, step, round (int8, the search round that first ran it, 0 for an
                   optimise-window trial), sharpe, num_trades, and the five quality numbers closed, win_share, max_drawdown, avg_month,
                   recovery (float64, null where the number does not exist; quality.py)
  returns.parquet  one row per trial, in the same order: trial_id, window, bar_returns (the trial's daily bar returns, float32)
  finalists.json   the kept strategies (GV3-S2, #467 items 5 and 6): the best `keep` ([search]) of all rounds that meet the book's quality
                   condition (quality.select), best screen Sharpe first. Each entry: rank, trial_id, picks, step, round, screen_sharpe,
                   screen_num_trades, the five numbers and the optimise-window result; a calibrated theory's entry also carries its
                   calibrated stop and target (sigma units, by gap bucket) or its calibration_error. [] when the theory is "not suitable"
  family.json      the screen family's identity and the numbers the judge will use: K, V, the cap, the reason ONC did not run
  screen_dates.json  the screen trials' bar dates as ISO strings, in order (the index of every screen row of returns.parquet; GV3-FU1: the merge
                   of several searches' trials reads it)
  pick.json        the pick (best optimise-window Sharpe among the kept), or picks null with a reason ("not suitable after round 1" also
                   carries the closest strategy's numbers), and what the search did: "search" holds the same notes as rounds.json
  rounds.json      the rounds (GV3-S1): per round its box per letter (from, to, step, values), the valid strategies, how many ran ("all" or a
                   sample), the strategies, the new trials and the best trial ids that narrow the next box; then the quality answer
                   (quality.suitable: yes or no, the pool, the closest strategy) and the stop word: "not suitable", "ran every
                   combination", "max rounds" or "no strategy cleared 30 trades"
  bars_observed.json  {"members": {ticker: n}, "total": sum} (lane GV2-8b, DECISIONS #459 item 2): each stock's close bars that are not
                   NaN, from the first bar of its stored file (the search reads the file whole, so every bar before the screen window is
                   lead-in) to the later of the screen window's and the optimise window's ends
  final.json       only when the spec has a final: {kept_back, later_window, strategies}, one entry per finalists.json entry (final.py)
The same spec and seed give byte-identical files. Meant for research-runner:m3-latest with research_lab mounted; it needs vbt.

The spec is a JSON object:
  theory, source_shape, data + codes (raw_synthetic) or files (stored), init_cash, costs   as in research_lab.theory_run.one
  group_label      the name of the stock group (for family.json)
  screen_window, optimise_window   [start, end] dates, both included
  seed             the seed of every draw
  n_iter           paths per mesh in the stop and target calibration
  onc_cap          the most clusters ONC may find
  workers          optional, 1: worker processes for the strategies (the files do not depend on it)
  final            optional (GV3-F, #467 item 5, #473): {kept_back: [tickers], later_window: [start, end]}. The search then reads only the other
                   stocks and writes final.json too (final.py): per kept strategy its run on the kept-back stocks over the screen window and
                   on the searched stocks over the later window, five numbers and meets each, and held_up. bars_observed.json counts these reads
The size of a round and every other search setting live in the theory's [search] table (DECISIONS #470); a spec with size or
box_limit is refused by name.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research_lab.theory_run import identity
from research_lab.theory_search import family, final, quality, search

SPEC_KEYS = ("theory", "source_shape", "group_label", "seed", "n_iter", "onc_cap", "screen_window", "optimise_window", "init_cash", "costs")


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean(value):
    """JSON-safe: NaN and infinity become null, numpy numbers become plain ones."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    return value


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(_clean(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_trials(out: Path, trials: list) -> None:
    table = pa.table({
        "trial_id": pa.array([t.trial_id for t in trials], type=pa.int32()),
        "picks": pa.array([json.dumps(t.picks, sort_keys=True) for t in trials], type=pa.string()),
        "window": pa.array([t.window for t in trials], type=pa.string()),
        "step": pa.array([t.step for t in trials], type=pa.int8()),
        "round": pa.array([t.round for t in trials], type=pa.int8()),
        "sharpe": pa.array([t.sharpe for t in trials], type=pa.float64()),
        "num_trades": pa.array([t.num_trades for t in trials], type=pa.int32()),
        "closed": pa.array([t.closed for t in trials], type=pa.float64()),
        "win_share": pa.array([t.win_share for t in trials], type=pa.float64()),
        "max_drawdown": pa.array([t.max_drawdown for t in trials], type=pa.float64()),
        "avg_month": pa.array([t.avg_month for t in trials], type=pa.float64()),
        "recovery": pa.array([t.recovery for t in trials], type=pa.float64()),
    })
    pq.write_table(table, out / "trials.parquet", compression="snappy")


def _write_returns(out: Path, trials: list) -> None:
    table = pa.table({
        "trial_id": pa.array([t.trial_id for t in trials], type=pa.int32()),
        "window": pa.array([t.window for t in trials], type=pa.string()),
        "bar_returns": pa.array([t.bar_returns for t in trials], type=pa.list_(pa.float32())),
    })
    pq.write_table(table, out / "returns.parquet", compression="snappy")


def bars_observed(frames: dict, screen_window, optimise_window) -> dict:
    """Each member's close bars that are not NaN, from the frames' first bar to the later of the two windows' ends (both included)."""
    close = frames["close"]
    end = max(pd.Timestamp(screen_window[1]), pd.Timestamp(optimise_window[1]))
    if close.index.tz is not None:  # a stored-shape dataset has a UTC index; read the window dates in it, as search._fillable does
        end = end.tz_localize(close.index.tz)
    members = {str(t): int(n) for t, n in close.loc[:end].notna().sum().items()}
    return {"members": members, "total": sum(members.values())}


def bars_observed_final(searched_frames: dict, kept_frames: dict, spec: dict) -> dict:
    """bars_observed.json of a search with a final (GV3-F): each searched stock's non-NaN closes to the latest of the screen, optimise and
    later windows' ends, each kept-back stock's to the screen window's end (the final test reads the kept-back stocks over the screen years only)."""
    end = max(spec["screen_window"][1], spec["optimise_window"][1], spec["final"]["later_window"][1])   # ISO dates sort as text
    members = {**final.bars_count(searched_frames, end), **final.bars_count(kept_frames, spec["screen_window"][1])}
    members = {t: members[t] for t in sorted(members)}
    return {"members": members, "total": sum(members.values())}


def run_spec(spec: dict, out: Path, bars: quality.Bars = quality.BOOK) -> dict:
    """Runs the search and writes the files. `bars` is a TEST HOOK (quality.py): main() never passes it."""
    for gone in ("size", "box_limit"):
        if gone in spec:
            raise ValueError("the spec has '%s': a round's size and limits are the theory's [search] (#470), not a spec key" % gone)
    missing = [k for k in SPEC_KEYS if k not in spec]
    if missing:
        raise ValueError("the spec lacks %s" % ", ".join(missing))
    if spec["onc_cap"] < 2:
        raise ValueError("onc_cap must be at least 2, got %r" % (spec["onc_cap"],))
    search_spec, kept_spec = (spec, None) if "final" not in spec else final.split_spec(spec)   # split_spec refuses a bad final by name
    ctx = search.load_context(search_spec)
    evaluator = search.Evaluator(search_spec, ctx)
    try:
        screen = search.run_rounds(spec, evaluator, bars=bars)
    finally:
        evaluator.close()

    not_suitable = screen.notes["stop"] == "not suitable"
    finalists = [] if not_suitable else quality.select(screen.trials, search.settings(ctx.theory).keep, bars)
    calibrated = search.is_calibrated(ctx.theory)
    entries, optimise = [], {}
    next_id = len(screen.trials)
    for rank, trial in enumerate(finalists, start=1):
        make = search.calibrate_finalist if calibrated else search.plain_finalist
        entry, opt = make(spec, ctx, trial, rank, next_id)
        entries.append(entry)
        if opt is not None:
            optimise[rank] = opt
            next_id += 1
    pick_entry, pick_trial = search.choose_pick(entries, optimise)

    screen_trials = screen.trials
    returns = pd.DataFrame({t.trial_id: t.bar_returns for t in screen_trials}, index=screen_trials[0].dates)
    summary = family.family_summary(
        pd.DataFrame({"trial_id": [t.trial_id for t in screen_trials], "num_trades": [t.num_trades for t in screen_trials]}), returns,
        onc_cap=int(spec["onc_cap"]), seed=int(spec["seed"]),
        selected=None if pick_entry is None else pick_entry["trial_id"])

    out.mkdir(parents=True, exist_ok=True)
    all_trials = screen_trials + [optimise[r] for r in sorted(optimise)]
    _write_trials(out, all_trials)
    _write_returns(out, all_trials)
    _write_json(out / "finalists.json", entries)
    _write_json(out / "rounds.json", screen.notes)
    if kept_spec is None:
        _write_json(out / "bars_observed.json", bars_observed(ctx.frames, spec["screen_window"], spec["optimise_window"]))
    else:
        ctx_kept = search.load_context(kept_spec)
        strategies = [final.final_entry(spec, ctx_kept, ctx, entry, bars) for entry in entries]
        _write_json(out / "final.json", {"kept_back": spec["final"]["kept_back"], "later_window": spec["final"]["later_window"], "strategies": strategies})
        _write_json(out / "bars_observed.json", bars_observed_final(ctx.frames, ctx_kept.frames, spec))

    fam = {
        "theory_name": ctx.theory.data["theory"]["name"], "group_label": spec["group_label"],
        "member_set_sha256": identity.member_set_sha256(ctx.shas.values()),
        "window_start": spec["screen_window"][0], "window_end": spec["screen_window"][1],
        "n_trials": summary["n_trials"], "n_in_onc": summary["n_in_onc"], "k_effective": summary["k_effective"],
        "onc_cap": summary["onc_cap"], "cap_bound": summary["cap_bound"], "v": summary["v"],
        "selected_frequency": summary["selected_frequency"], "reason": summary["reason"],
        "trials_sha256": _sha256_of(out / "trials.parquet"), "returns_sha256": _sha256_of(out / "returns.parquet"),
        "n_cleared_bar": summary["n_cleared_bar"], "n_zero_variance": summary["n_zero_variance"], "cluster_sizes": summary["cluster_sizes"],
        "onc_cap_asked": int(spec["onc_cap"]),
    }
    _write_json(out / "family.json", fam)
    _write_json(out / "screen_dates.json", [d.isoformat() for d in screen_trials[0].dates])

    if pick_entry is None:
        if not_suitable:
            pick = {"picks": None, "reason": "not suitable after round 1", "closest": screen.notes["quality"]["closest"]}
        elif not finalists:
            pick = {"picks": None, "reason": "no strategy met the quality condition"}
        elif calibrated:
            pick = {"picks": None, "reason": "no finalist had a calibrated stop and target and a defined optimise-window Sharpe"}
        else:
            pick = {"picks": None, "reason": "no kept strategy had a defined optimise-window Sharpe"}
    else:
        pick = {"picks": pick_entry["picks"], "finalist_rank": pick_entry["rank"], "screen_trial_id": pick_entry["trial_id"],
                "optimise_trial_id": pick_trial.trial_id, "optimise_sharpe": pick_trial.sharpe, "optimise_num_trades": pick_trial.num_trades,
                "screen_sharpe": pick_entry["screen_sharpe"], "reason": None}
        if calibrated:
            pick["calibration"] = pick_entry["calibration"]
    pick["search"] = screen.notes
    pick["spec"] = {k: spec[k] for k in ("seed", "n_iter", "onc_cap", "screen_window", "optimise_window", "costs", "init_cash")}
    _write_json(out / "pick.json", pick)
    return fam


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.theory_search.run")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    fam = run_spec(json.loads(args.spec.read_text(encoding="utf-8")), args.out)
    print("wrote the search files to %s: %d trials, K %s, V %s, reason %s" % (args.out, fam["n_trials"], fam["k_effective"], fam["v"], fam["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
