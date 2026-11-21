"""Measure the drift between the backtest and NautilusTrader over a fixed set of past runs.

Lane NT2, DECISIONS #449 and #452. For each run in MEASURE_SET this reads the run folder's
trades.parquet, price.parquet and equity.parquet, replays the run through the nautilus-runner
image exactly as the worker's run_retest job does, and reads the two gaps the image reports
(gap_final_equity = (1 + nautilus_total_return) / (1 + backtest_total_return) - 1, so a run with a
huge return is judged by its relative gap; gap_sharpe = Nautilus minus backtest, NT1). The largest absolute value of each
gap over the set is the drift allowance a later retest must stay inside to be `passed`.

Run on the laptop, which has Docker and the run folders:

    python -m research_lab.nautilus_drift.measure --runs-root <runs dir> --out <empty dir>

The run folders are read in place and never written. Nothing here touches a database or a server.
A run that could not run, or whose gap is null, stops the measurement by name (DriftSetIncomplete);
a smaller set is never silently used.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The 13 laptop run folders with trades, price and equity parquet, checked 2026-10-13: the 15 such
# folders less runs 36 and 37 (no trades). docs/lanes/NT2.md, "Rests on".
MEASURE_SET = (1, 3, 4, 5, 6, 7, 10, 27, 31, 34, 35, 46, 48)

DEFAULT_IMAGE = "nautilus-runner:1.231.0"

# The knob numbers whose disposition is fixed by the retest store (retestdb.KNOB_NAMES and
# validate_matched_configuration): 1, 2, 4 structurally unequal; 6, 7, 8 by construction; knob 12
# absent; the rest set equal. Restated here because this module must import on the worker's plain
# interpreter, which cannot import dashboard.backend.
_KNOB_NAMES = {
    1: "fill timing", 2: "slippage unit", 3: "fees", 4: "cash enforcement",
    5: "share granularity", 6: "same-bar conflict", 7: "long only", 8: "position sizing",
    9: "sweep grid", 10: "price grid", 11: "volume basis", 12: "annualisation",
    13: "intrabar path", 14: "hourly data",
}


class DriftSetIncomplete(Exception):
    """A run of the measured set could not run or has a null gap, so no allowance is computed."""


def matched_configuration() -> dict:
    statement = {}
    for row in range(1, 15):
        if row == 12:
            continue
        if row in (1, 2, 4):
            disposition = "structurally-unequal"
        elif row in (6, 7, 8):
            disposition = "by-construction"
        else:
            disposition = "set-equal"
        statement[str(row)] = {"name": _KNOB_NAMES[row], "disposition": disposition}
    return statement


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def allowance(results: list) -> dict:
    """The largest absolute value of each gap over `results` (dicts with run_id, status,
    gap_final_equity, gap_sharpe). Raises DriftSetIncomplete naming every run that is
    could-not-run or has a missing or non-finite gap."""
    bad = []
    for item in results:
        if item.get("status") != "report-only":
            bad.append(f"run {item.get('run_id')}: status {item.get('status')!r}")
        elif not _is_number(item.get("gap_final_equity")) or not _is_number(item.get("gap_sharpe")):
            bad.append(f"run {item.get('run_id')}: a gap is null "
                       f"(gap_final_equity={item.get('gap_final_equity')!r}, "
                       f"gap_sharpe={item.get('gap_sharpe')!r})")
    if bad:
        raise DriftSetIncomplete("; ".join(bad))
    if not results:
        raise DriftSetIncomplete("no results")
    return {
        "max_abs_gap_final_equity": max(abs(item["gap_final_equity"]) for item in results),
        "max_abs_gap_sharpe": max(abs(item["gap_sharpe"]) for item in results),
    }


def _replay_one(run_id: int, run_dir: Path, image: str, convert, gap_final_equity) -> dict:
    import pandas as pd

    for name in ("trades.parquet", "price.parquet", "equity.parquet"):
        if not (run_dir / name).is_file():
            raise FileNotFoundError(f"run {run_id}: {run_dir / name} is missing")
    trades = pd.read_parquet(run_dir / "trades.parquet")
    price = pd.read_parquet(run_dir / "price.parquet")
    fixture_trades = convert(trades, price)
    with tempfile.TemporaryDirectory(prefix=f"drift_{run_id}_") as tmp:
        workdir = Path(tmp)
        out_dir = workdir / "out"
        out_dir.mkdir()
        fixture_trades.to_parquet(workdir / "trades.parquet", index=False)
        price.to_parquet(workdir / "bars.parquet", index=False)
        (workdir / "equity.parquet").write_bytes((run_dir / "equity.parquet").read_bytes())
        spec = {
            "run_id": run_id,
            "trades_path": "/fixtures/trades.parquet",
            "bars_path": "/fixtures/bars.parquet",
            "equity_path": "/fixtures/equity.parquet",
            "matched_configuration": matched_configuration(),
            "residue_allowance": None,
        }
        (workdir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{workdir}:/fixtures:ro", "-v", f"{out_dir}:/out:rw",
             "-e", "RETEST_SPEC_PATH=/fixtures/spec.json", "-e", "RETEST_OUTPUT_DIR=/out",
             image, "python3", "/app/run_retest_entrypoint.py"],
            capture_output=True, text=True)
        result_path = out_dir / "retest_result.json"
        if not result_path.is_file():
            return {"run_id": run_id, "status": "could-not-run",
                    "could_not_run_reason": f"no retest_result.json (docker exit {proc.returncode}): "
                                            f"{proc.stderr[-300:]}",
                    "gap_final_equity": None, "gap_sharpe": None}
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    figures = payload.get("figures") or {}
    return {
        "run_id": run_id,
        "status": payload.get("status"),
        "could_not_run_reason": payload.get("could_not_run_reason"),
        "trades": int(len(trades)),
        "fills_count": figures.get("fills_count"),
        "backtest_total_return": figures.get("backtest_total_return"),
        "backtest_sharpe": figures.get("backtest_sharpe"),
        "nautilus_total_return": figures.get("nautilus_total_return"),
        "nautilus_sharpe": figures.get("nautilus_sharpe"),
        "gap_final_equity": gap_final_equity(figures),
        "gap_sharpe": figures.get("gap_sharpe"),
    }


def measure(runs_root: Path, out_dir: Path, image: str, run_ids=MEASURE_SET) -> dict:
    """Replay every run of `run_ids`, write drift.json into out_dir and return its content."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
    from jobs import run_retest  # the worker's own trades converter, never a second copy

    results = []
    for run_id in run_ids:
        item = _replay_one(run_id, runs_root / str(run_id), image,
                           run_retest._convert_real_trades_to_fixture_schema, run_retest.gap_final_equity)
        print(f"run {run_id}: {item['status']} gap_final_equity={item['gap_final_equity']} "
              f"gap_sharpe={item['gap_sharpe']}", flush=True)
        results.append(item)
    maxima = allowance(results)
    document = {
        "image": image,
        "measured_at": int(time.time()),
        **maxima,
        "runs": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "drift.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--image", default=os.environ.get("NAUTILUS_RUNNER_IMAGE", DEFAULT_IMAGE))
    args = parser.parse_args(argv)
    try:
        document = measure(args.runs_root, args.out, args.image)
    except DriftSetIncomplete as exc:
        print(f"DriftSetIncomplete: {exc}", file=sys.stderr)
        return 1
    print(f"max_abs_gap_final_equity={document['max_abs_gap_final_equity']} "
          f"max_abs_gap_sharpe={document['max_abs_gap_sharpe']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
