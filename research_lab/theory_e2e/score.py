"""Scoring the planted run against the answer sheet (lane GV2-7c, plan step 4). Pandas and the standard library only.

score(result, answer_csv, summary_json) gives one entry per line (a) to (h), each true or false with its numbers.
The test passes when (a) to (g) are all true; (h) lists every check outcome and never decides.

result.json, per group (run.py writes it; the hand files of the probes have the same shape):
  grid                     {letter: sorted list of the theory's grid values}      (top level, one theory)
  groups[GROUP]:
    step1_best             [{"picks": {...}, "sharpe": x, "num_trades": n}, ...]  the best 5 percent of step 1, best first
    pick                   {"picks": {...}, ...}
    optimise_trades        [{"ticker": "SYN_VOL_01", "entry_date": "2017-02-17"}, ...]  the pick's optimise-window trades
    walkforward            {"out_of_sample_profit": x, ...}
    verdict                {"verdict": "pass" | "fail" | "unjudgeable" | null, "checks": [{"check": n, "outcome": s, "reason": s}],
                            "raw_check_outcomes": [the judge's own list with each check's quantities]}
    family                 {"reason": null | "too-few-trials", ...}
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PLANTED_PICKS = {"N": 20, "W": 2.0, "k": 2.5, "R": 14, "L": 30, "H": 10}  # GV2-1's design values (answer.py)
PLANTED_SHARE_BAR = 0.8  # a bar set by the planning session for this report, not tuned (plan step 4 (c))
NINE_CHECKS = 9


def one_step_window(letter: str, grid: dict, planted: dict = PLANTED_PICKS) -> tuple:
    """The closed interval from the grid value below the planted one to the grid value above it (the planted value itself at an end)."""
    values = sorted(float(v) for v in grid[letter])
    target = float(planted[letter])
    if target not in values:
        raise ValueError(f"the planted value {target} of {letter} is not on the grid {values}")
    i = values.index(target)
    return values[max(i - 1, 0)], values[min(i + 1, len(values) - 1)]


def is_neighbour(picks: dict, grid: dict, planted: dict = PLANTED_PICKS) -> bool:
    """Every letter of `picks` is within one grid step of the planted value: the planted value or the grid value just below
    or just above it, or a value in between (a narrowed step 2 value that is off the grid)."""
    for letter in planted:
        if letter not in picks:
            return False
        lo, hi = one_step_window(letter, grid, planted)
        if not (lo <= float(picks[letter]) <= hi):
            return False
    return True


def planted_share(trades: list, answer: pd.DataFrame) -> tuple:
    """(planted trades, all trades): a trade is planted only when its (ticker, entry date) is a planted row of the answer sheet."""
    planted = set(zip(answer.loc[answer["kind"] == "planted", "code"], answer.loc[answer["kind"] == "planted", "entry_date"]))
    n = sum(1 for t in trades if (t["ticker"], str(t["entry_date"])[:10]) in planted)
    return n, len(trades)


def _line(key: str, ok: bool, text: str, numbers: dict) -> dict:
    return {"line": key, "ok": bool(ok), "text": text, "numbers": numbers}


def _group(result: dict, name: str) -> "dict | None":
    return result.get("groups", {}).get(name)


def _verdict(group: "dict | None") -> "str | None":
    if group is None or group.get("verdict") is None:
        return None
    return group["verdict"].get("verdict")


def check1_dof(group: "dict | None") -> dict:
    """Judge check 1's own numbers for a judged cell, read from the raw outcomes the judge returned (measured, never tuned):
    Udf and Odf when kappa passed, "kappa gate" when Taleb's gate stopped the check before Odf and Udf were read, otherwise
    "not read" with the check's own outcome and reason."""
    verdict = (group or {}).get("verdict")
    if not verdict:
        return {"read": "no verdict"}
    raw = verdict.get("raw_check_outcomes") or []
    first = [c for c in raw if isinstance(c, dict) and c.get("check") == 1]
    if not first:
        return {"read": "no verdict"}
    check = first[0]
    quantities = check.get("quantities") or {}
    kappa = quantities.get("kappa")
    if "udf" in quantities and "odf" in quantities:
        return {"read": "udf_odf", "udf": quantities["udf"], "odf": quantities["odf"], "kappa": kappa, "outcome": check.get("outcome")}
    if kappa is not None and "gate" in str(check.get("reason")):
        return {"read": "kappa gate", "kappa": kappa, "outcome": check.get("outcome")}
    out = {"read": "not read", "outcome": check.get("outcome"), "reason": check.get("reason")}
    if kappa is not None:
        out["kappa"] = kappa
    return out


def targets_at_mesh_top(pick: "dict | None", mesh_top_sigma: float) -> dict:
    """Do the pick's calibrated targets sit at the exit mesh's top (the largest target the mesh tries, in sigma)? yes when every
    gap bucket's target is at the top; the numbers are the bucket count, how many are at the top, and the largest gap."""
    buckets = (pick or {}).get("calibration_buckets")
    if not buckets:
        return {"at_top": "no pick"}
    at_top = sum(1 for b in buckets.values() if float(b["target_sigma"]) >= mesh_top_sigma)
    return {"at_top": "yes" if at_top == len(buckets) else "no", "buckets": len(buckets), "buckets_at_top": at_top,
            "mesh_top_sigma": mesh_top_sigma, "largest_gap_sigma": max(float(k) for k in buckets)}


def extras(result: dict) -> dict:
    """The measured extras of the report, per group: check 1's Udf and Odf (or kappa gate) and the targets against the mesh top."""
    top = (result.get("settings") or {}).get("exit_mesh_top_sigma")
    out = {}
    for name, g in result.get("groups", {}).items():
        g = g or {}
        out[name] = {"check1_dof": check1_dof(g),
                     "targets_at_mesh_top": targets_at_mesh_top(g.get("pick"), top) if top is not None else {"at_top": "mesh top not recorded"}}
    return out


def score(result, answer_csv, summary_json) -> dict:
    if not isinstance(result, dict):
        result = json.loads(Path(result).read_text(encoding="utf-8"))
    answer = pd.read_csv(answer_csv, dtype={"entry_date": str, "signal_date": str})
    summary = summary_json if isinstance(summary_json, dict) else json.loads(Path(summary_json).read_text(encoding="utf-8"))
    grid = result["grid"]
    lines = []

    vol = _group(result, "SYN_VOL")
    best = (vol or {}).get("step1_best") or []
    hits = [i + 1 for i, t in enumerate(best) if is_neighbour(t["picks"], grid)]
    lines.append(_line("a", bool(hits),
                       "SYN_VOL step 1's best 5 percent holds the planted picks or a one-step neighbour",
                       {"best_5_percent_size": len(best), "matching_trials": len(hits), "best_matching_rank": hits[0] if hits else None}))

    pick = (vol or {}).get("pick")
    pick_ok = bool(pick) and is_neighbour(pick["picks"], grid)
    lines.append(_line("b", pick_ok, "the SYN_VOL pick is the planted picks or a one-step neighbour",
                       {"pick": None if pick is None else pick["picks"]}))

    trades = (vol or {}).get("optimise_trades")
    if trades is None:
        lines.append(_line("c", False, "at least 80 percent of the SYN_VOL pick's optimise-window trades are planted ones",
                           {"trades": None, "planted": None, "share": None}))
    else:
        n_planted, n_all = planted_share(trades, answer)
        share = (n_planted / n_all) if n_all else None
        lines.append(_line("c", n_all > 0 and share >= PLANTED_SHARE_BAR,
                           "at least 80 percent of the SYN_VOL pick's optimise-window trades are planted ones",
                           {"trades": n_all, "planted": n_planted, "share": share, "bar": PLANTED_SHARE_BAR,
                            "planted_signals_in_window": summary.get("volatile", {}).get("optimise", {}).get("planted")}))

    wf = (vol or {}).get("walkforward")
    profit = None if wf is None else wf.get("out_of_sample_profit")
    lines.append(_line("d", profit is not None and profit > 0, "the SYN_VOL walk-forward out-of-sample profit is positive",
                       {"out_of_sample_profit": profit}))

    v_vol = _verdict(vol)
    lines.append(_line("e", v_vol == "pass", "the SYN_VOL verdict is pass", {"verdict": v_vol}))

    twn = _group(result, "SYN_TWN")
    v_twn = _verdict(twn)
    lines.append(_line("f", twn is not None and v_twn != "pass", "the SYN_TWN verdict is not pass",
                       {"verdict": v_twn, "ran": twn is not None}))

    ends = {}
    ok_g = True
    for name in ("SYN_FLT", "SYN_TRD"):
        g = _group(result, name)
        if g is None:
            ends[name] = "did not run"
            ok_g = False
            continue
        reason = (g.get("family") or {}).get("reason")
        verdict = _verdict(g)
        if reason == "too-few-trials":
            ends[name] = "family reason too-few-trials (expected)"
        elif verdict != "pass":
            ends[name] = f"verdict {verdict} (not pass)"
        else:
            ends[name] = "verdict pass"
            ok_g = False
    lines.append(_line("g", ok_g, "SYN_FLT and SYN_TRD each end with family reason too-few-trials, or else a verdict that is not pass",
                       ends))

    listed = {}
    complete = True
    for name, g in result.get("groups", {}).items():
        v = (g or {}).get("verdict")
        if v is None:
            continue
        listed[name] = [(c["check"], c["outcome"], c.get("reason")) for c in v.get("checks", [])]
        complete = complete and len(listed[name]) == NINE_CHECKS
    lines.append(_line("h", complete, "every check's outcome and reason, per judged group", listed))

    return {"lines": lines, "passed": all(line["ok"] for line in lines if line["line"] in "abcdefg"), "extras": extras(result)}
