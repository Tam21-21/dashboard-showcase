"""The host-side generation entry point (section 5.2 step 2):

    python -m research_lab.generator.cli generate <theory.md> --facts <facts.json>

Run with the repo root on sys.path (research_lab is a namespace package; no __init__.py
needed at the repo root, matching the probes' own `sys.path.insert(0, os.getcwd())`
idiom). This is the only file in the unit that prints a human-readable summary; every
other module returns plain data so a probe can assert on it directly.
"""
from __future__ import annotations

import argparse
import sys

from research_lab.generator.facts import load_facts
from research_lab.generator.loop import generate
from research_lab.generator.theory import load_theory


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="research_lab.generator.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_parser = subparsers.add_parser("generate", help="run the generation loop over a theory file")
    generate_parser.add_argument("theory_path", help="path to the theory .md file")
    generate_parser.add_argument("--facts", required=True, help="path to the compile-facts JSON file")
    # PREVIEW, the gate (UI plan 2.3): run every step and print the would-be decision, writing
    # nothing at all. Same loop, same facts file, write=False threaded straight to generate().
    generate_parser.add_argument("--preview", action="store_true",
                                  help="report decisions only; write no candidate or strategy row")
    # RUN GENERATOR restricted to a subset (the pairs Tamer ticked after a PREVIEW): repeatable,
    # each one "skeleton:filler" (the same two ids theory.py's own '## Skeletons'/'## Fillers'
    # lists carry). Omit entirely to generate every pair the theory names (the default today).
    generate_parser.add_argument("--select", action="append", default=[], metavar="skeleton:filler",
                                  help="restrict generation to this (skeleton, filler) pair; "
                                       "repeat for more than one; omit to generate every pair")
    args = parser.parse_args(argv)

    if args.command == "generate":
        theory = load_theory(args.theory_path)
        facts = load_facts(args.facts, required_filler_ids=theory.fillers)
        selected = None
        if args.select:
            selected = set()
            for item in args.select:
                skeleton_id, sep, filler_id = item.partition(":")
                if not sep:
                    parser.error(f"--select expects skeleton:filler, got {item!r}")
                selected.add((skeleton_id, filler_id))
        outcomes = generate(theory, facts, write=not args.preview, selected=selected)
        for outcome in outcomes:
            if outcome.decision == "accepted":
                print(f"ACCEPTED skeleton={outcome.skeleton_id} filler={outcome.filler_id} "
                      f"strategy_id={outcome.strategy_id} name={outcome.strategy_name!r}")
            elif outcome.decision == "refused":
                print(f"REFUSED skeleton={outcome.skeleton_id} filler={outcome.filler_id} "
                      f"reason={outcome.reason!r}")
            else:
                print(f"SKIPPED skeleton={outcome.skeleton_id} filler={outcome.filler_id} "
                      f"reason={outcome.reason!r} candidate_id={outcome.candidate_id}")
        return 0
    return 1  # unreachable: argparse's `required=True` refuses an unknown command first


if __name__ == "__main__":
    sys.exit(main())
