"""python -m research_lab.theory_stage.run --spec <spec.json> --out <dir>  (lane GV2-7a)

Runs the optimise or the walk-forward stage of generator v2 strategies over their whole stock group and writes, into <dir>, the rows and
files the existing judge reads, with the group label in the ticker slot (see optimise.py and walkforward.py for the files). The same
spec gives the same bytes. Meant for research-runner:m3-latest with research_lab mounted; it needs vbt. The spec is described in spec.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from research_lab.theory_stage import optimise, spec as spec_mod, walkforward


def run_spec(spec: dict, out: Path) -> dict:
    ctx = spec_mod.load_context(spec)
    if spec["stage"] == "optimise":
        return optimise.run_stage(ctx, out)
    return walkforward.run_stage(ctx, out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.theory_stage.run")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    result = run_spec(spec, args.out)
    if spec["stage"] == "optimise":
        print("wrote the optimise files to %s: %d variation(s)" % (args.out, result["variations"]))
    else:
        print("wrote walkforward.json and equity.parquet to %s: %d slice(s)" % (args.out, len(result["slices"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
