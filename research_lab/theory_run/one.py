"""python -m research_lab.theory_run.one --spec <spec.json> --out <dir>  (lane GV2-5a)

Runs one generator v2 strategy (a theory with one set of parameter picks) over its stock group and writes equity.parquet,
trades.parquet and result.json into <dir>. Meant for research-runner:m3-latest with research_lab mounted; it needs vbt.

The spec is a JSON object:
  theory         path of the theory TOML
  picks          {letter: number}, one value for every letter the theory uses
  source_shape   "raw_synthetic" (GV2-1's files) or "stored" (dataset parquet files in the stored shape)
  data, codes    raw_synthetic: the folder with manifest.json and the list of codes to trade
  files          stored: {ticker: path of the dataset parquet}
  earnings       optional: {"path": an earnings slice parquet, "ciks": {ticker: cik}}; adds the five earnings frames (earnings.py) a theory may read
  window         [start, end] dates, both included; bars before start only warm the indicators
  init_cash      starting cash of the group
  costs          {"fees": "none", "ibkr_fixed" or "alpaca", "slippage": fraction}
  exits          {"stop": number or null, "target": number or null}, read only for a stop or target the theory leaves
                 "calibrated" (the exit calibration is another lane); the key must be there, null meaning no stop
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

from research_lab.theories import form
from research_lab.theory_run import earnings
from research_lab.theory_run import frames as loader
from research_lab.theory_run import identity, signals, simulate

SPEC_KEYS = ("theory", "picks", "source_shape", "window", "init_cash", "costs", "exits")


def _pick(value, picks: dict, what: str):
    if isinstance(value, str):
        if value not in picks:
            raise ValueError("%s: pick %r is missing from the spec's picks" % (what, value))
        return picks[value]
    return value


def exit_rules(theory, picks: dict, spec_exits: dict, frames=None) -> "simulate.ExitRules":
    """The theory's way out as ExitRules. `frames` (the group's bars) is needed for exit groups and for a trailing stop: they read the bars."""
    table = theory.data["exit"]
    sell = trail = atr = None
    if theory.exit_groups:
        if frames is None:
            raise ValueError("the theory has %d exit groups: exit_rules needs the frames to compute their signals" % len(theory.exit_groups))
        sell = signals.exit_signals(theory, picks, frames)
    if "trail" in table:
        if frames is None:
            raise ValueError("the theory has a trailing stop: exit_rules needs the frames to compute its ATR")
        trail = float(_pick(table["trail"], picks, "exit.trail"))
        atr = signals.atr_frame(frames, int(_pick(table["trail_atr"], picks, "exit.trail_atr")))
    out = {}
    for key in ("stop", "target"):
        value = table.get(key, form.NONE)
        if value == form.NONE:
            if spec_exits.get(key) is not None:
                raise ValueError("the spec gives exits.%s but the theory has none" % key)
            out[key] = None
        elif value == form.CALIBRATED:
            if key not in spec_exits:
                raise ValueError("the theory's %s is \"calibrated\": the spec must give exits.%s, a number or null for none "
                                 "(the exit calibration is GV2-6)" % (key, key))
            out[key] = spec_exits[key]
        else:
            if spec_exits.get(key) is not None:
                raise ValueError("the spec gives exits.%s but the theory already sets it" % key)
            out[key] = float(_pick(value, picks, "exit." + key))
    hold = table.get("max_hold")
    max_hold = None if hold is None else int(_pick(hold, picks, "exit.max_hold"))
    return simulate.ExitRules(max_hold=max_hold, stop=out["stop"], target=out["target"], sell=sell, trail=trail, atr=atr)


def _load_bars(spec: dict) -> tuple:
    shape = spec["source_shape"]
    if shape == "raw_synthetic":
        for key in ("data", "codes"):
            if key not in spec:
                raise ValueError('source_shape "raw_synthetic" needs %r in the spec' % key)
        return loader.load_raw_synthetic(spec["data"], spec["codes"])
    if shape == "stored":
        if "files" not in spec:
            raise ValueError('source_shape "stored" needs "files" in the spec')
        return loader.load_stored(spec["files"])
    raise ValueError('source_shape must be "raw_synthetic" or "stored", got %r' % (shape,))


def load_group(spec: dict) -> tuple:
    """The group's bars and file hashes. When the spec has "earnings" ({"path": an earnings slice parquet, "ciks": {ticker: cik}}) the five
    earnings frames of earnings.py are added to the frames; the hashes stay those of the bar files."""
    frames, shas = _load_bars(spec)
    if "earnings" in spec:
        frames.update(earnings.earnings_frames(spec["earnings"], frames["close"]))
    return frames, shas


def _finite(value):
    return value if isinstance(value, (int, str)) or value is None or math.isfinite(value) else None


def run_spec(spec: dict, out: Path) -> dict:
    missing = [k for k in SPEC_KEYS if k not in spec]
    if missing:
        raise ValueError("the spec lacks %s" % ", ".join(missing))
    theory = form.load(spec["theory"])
    picks = spec["picks"]
    if theory.data["theory"]["entry_time"] != "next_open":
        raise ValueError("this runner fills at the next open; the theory's entry_time is %r" % theory.data["theory"]["entry_time"])
    method = theory.data["money"]["method"]
    frames, shas = load_group(spec)
    entries = signals.entry_signals(theory, picks, frames)
    rules = exit_rules(theory, picks, spec["exits"], frames)
    window = tuple(spec["window"])
    trail_atr = None if rules.trail is None else int(_pick(theory.data["exit"]["trail_atr"], picks, "exit.trail_atr"))
    res = simulate.run_group(frames, entries, rules, {"method": method, "init_cash": spec["init_cash"]}, spec["costs"], window)

    out.mkdir(parents=True, exist_ok=True)
    equity = pd.DataFrame({"date": res.equity.index, "equity": res.equity.to_numpy(), "bar_return": res.bar_returns.to_numpy()})
    equity.to_parquet(out / "equity.parquet", index=False)
    res.trades.to_parquet(out / "trades.parquet", index=False)
    result = {
        "theory": theory.data["theory"]["name"], "picks": picks, "window": list(window), "tickers": list(frames["close"].columns),
        "init_cash": spec["init_cash"], "costs": spec["costs"],
        "exits": {"max_hold": rules.max_hold, "stop": rules.stop, "target": rules.target, "trail": rules.trail, "trail_atr": trail_atr,
                  "exit_groups": len(theory.exit_groups)},
        "exit_reasons": res.exit_reasons,
        "sizing_refusals": res.sizing_refusals,
        "sharpe_annualised": _finite(res.sharpe), "sharpe_per_bar": _finite(res.sharpe_per_bar),
        "num_trades": res.num_trades, "num_open_at_end": res.num_open_at_end, "max_open_at_once": res.max_open_at_once,
        "total_return": _finite(res.total_return), "max_drawdown": _finite(res.max_drawdown),
        "fees_paid": _finite(res.fees_paid), "slippage_paid": _finite(res.slippage_paid),
        "entries_planned": res.entries_planned, "entries_refused": res.entries_refused, "fee_passes": res.fee_passes, "fee_cycles": res.fee_cycles,
        "members_sha256": shas, "member_set_sha256": identity.member_set_sha256(shas.values()),
    }
    (out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.theory_run.one")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_spec(json.loads(args.spec.read_text(encoding="utf-8")), args.out)
    print("wrote equity.parquet, trades.parquet and result.json to %s: %d trades, sharpe %s" % (args.out, result["num_trades"], result["sharpe_annualised"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
