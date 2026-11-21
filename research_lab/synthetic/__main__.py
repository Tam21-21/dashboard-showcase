"""python -m research_lab.synthetic make --out <dir> --seed <n> [--version 1|2]  |  python -m research_lab.synthetic answer --data <dir>"""
from __future__ import annotations

import argparse
from pathlib import Path

from research_lab.synthetic import answer, make, make_v2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.synthetic")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("make", help="write the made-up stocks and manifest.json")
    m.add_argument("--out", required=True, type=Path)
    m.add_argument("--seed", required=True, type=int)
    m.add_argument("--version", type=int, choices=(1, 2), default=1,
                   help="1: 24 stocks (the first set, default); 2: 36 stocks with the rebound fixes")
    r = sub.add_parser("random-twins", help="write the twin set whose drops fall at random times (SYN_TWR_01 to 06)")
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--seed", required=True, type=int)
    a = sub.add_parser("answer", help="write the answer sheet from a data folder (follows the folder's version)")
    a.add_argument("--data", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "make":
        module = make if args.version == 1 else make_v2
        manifest = module.make(args.out, args.seed)
        dropped = sum(1 for c in manifest["codes"].values() for e in c["events"] if e["kind"] == "dropped")
        planted = sum(1 for c in manifest["codes"].values() for e in c["events"] if e["kind"] == "planted")
        print(f"wrote {len(manifest['codes'])} stocks to {args.out}: {planted} planted events, {dropped} dropped")
    elif args.command == "random-twins":
        manifest = make.make_random_twins(args.out, args.seed)
        drops = sum(1 for c in manifest["codes"].values() for e in c["events"] if e["kind"] == "twin_drop")
        print(f"wrote 6 stocks to {args.out}: {drops} twin drops at random times")
    elif answer.folder_version(args.data) == 2:
        answer.write_theory_answer(args.data)
        print(f"wrote theory_answer.csv and theory_summary.json to {args.data}")
    else:
        answer.write_answer(args.data)
        print(f"wrote answer.csv and summary.json to {args.data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
