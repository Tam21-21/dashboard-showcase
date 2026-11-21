"""Research lab command-line interface.

Subcommands operate directly on the research DB (the same `researchdb` module the
FastAPI app uses), targeting whatever `RESEARCH_DB_PATH` points at. Run from the
repo root:

    python -m dashboard.backend.cli register-strategy --file research_lab/strategies/ma_cross.py
    python -m dashboard.backend.cli load-cik-ticker-map
    python -m dashboard.backend.cli bulk-reparse --batch-id 7 --reparse-dir /tmp/restage [--apply]

Subcommands:
  - register-strategy  (M3 brief C): idempotent versioned strategy write
  - load-cik-ticker-map (M2.5 brief C): populate research_cik_ticker_map from
    SEC's company_tickers_exchange.json (or a local fixture via --source-file).
    Idempotent: re-runs do not duplicate (cik, ticker) rows for the same source.
  - bulk-reparse (M2.5 brief G): re-parse active events under a target batch_id
    against pre-staged submission .txt files; route correction-candidates
    (same company set) vs rollback-candidates (different company set) per arch
    §5.4 round-5 correction 2. Default dry-run; --apply opens correction-batches
    one per correction-eligible event. Rollback candidates are NEVER applied
    here — operator runs POST /event-batches/{id}/rollback with the typed
    confirmation phrase per EL42.
  - load-universe (M3.5 brief A): load a today's-list universe from a
    constituents file — one research_datasets catalog row (dataset_type=
    'universe') plus one research_universe_membership row per resolved CIK.
    Each ticker resolves via research_cik_ticker_map; an unresolvable or
    ambiguous ticker is reported and counted, never silently dropped.
    Idempotent: re-running for the same --name replaces the membership set.

The CLI does not broadcast SSE — it runs out-of-process from the app — so it just
prints the action.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error                                            # the wrapper maps these to exit 2
from pathlib import Path

from dashboard.backend import researchdb


def cmd_register_strategy(args: argparse.Namespace) -> int:
    """Register (or re-register) a strategy from a .py file. Returns a process exit code."""
    path = Path(args.file)

    # Rule 1 (clean UTF-8) is enforced here, at the read boundary: strict decoding
    # raises on any byte that is not valid UTF-8.
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    except UnicodeDecodeError as exc:
        print(f"error: file is not clean UTF-8: {exc}", file=sys.stderr)
        return 2

    # Name defaults to the file stem (ma_cross.py -> 'ma_cross'); --name overrides.
    name = args.name or path.stem

    # upsert_strategy validates the source (size, parses, non-empty docstring) before
    # writing, so the CLI just maps a validation failure to a clean error + exit code.
    # DN1: a new strategy needs a display name; an existing one keeps its own unless one is given.
    if args.display_name is None and not any(r["name"] == name for r in researchdb.list_strategy_names()):
        print("error: a new strategy needs --display-name", file=sys.stderr)
        return 2
    try:
        strategy_id, version, action = researchdb.upsert_strategy(name, source, display_name=args.display_name)
    except researchdb.StrategyValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{action}: strategy '{name}' id={strategy_id} version={version}")
    return 0


# M2.5 brief C — load-cik-ticker-map subcommand.

# The SEC fetch/parse helpers + the idempotent insert moved to
# dashboard/backend/events/cik_ticker_map.py (pre-M4 Brief A1 / D4), so this CLI
# subcommand and the new POST /cik-ticker-map route share one implementation. The cmd
# below is now a thin wrapper that maps the module's exceptions to CLI exit codes.


def cmd_load_cik_ticker_map(args: argparse.Namespace) -> int:
    """Populate `research_cik_ticker_map` from SEC's company_tickers_exchange.json.

    Thin wrapper over events.cik_ticker_map.load_cik_ticker_map (the shared implementation
    the POST /cik-ticker-map route also calls). Idempotent: existing (cik, ticker,
    source='sec_company_tickers') rows are not duplicated. Source: live SEC URL by default,
    or a local file via --source-file (for probes and offline tests). Maps the module's
    exceptions to exit 2 (the operator-argument-problem convention).
    """
    # Local import so the cli module doesn't pay the events-stack import cost on every
    # invocation (the bulk_reparse / load_universe precedent).
    from dashboard.backend.events import cik_ticker_map as cik_map_mod
    try:
        result = cik_map_mod.load_cik_ticker_map(
            source_file=args.source_file,            # None → fetch the live SEC URL
            events_db_path=args.events_db_path,      # None → eventsdb.DB_PATH default
        )
    except FileNotFoundError:
        print(f"error: source file not found: {args.source_file}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: source file is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:            # HTTPError first: it subclasses URLError
        print(f"error: HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"error: HTTP fetch failed: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:                        # a payload missing the required fields
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"load-cik-ticker-map: rows in payload={result.rows_in_payload} "
          f"inserted={result.inserted} skipped_existing={result.skipped_existing} "
          f"names_backfilled={result.names_backfilled}")
    return 0


def cmd_bulk_reparse(args: argparse.Namespace) -> int:
    """M2.5 brief G: bulk re-parse a batch's active events; route correction vs rollback.

    Spec brief §11 brief G row + arch v6 §5.4 round-5 correction 2. Wraps
    `dashboard/backend/events/bulk_reparse.bulk_reparse` and prints the result
    as JSON so an operator can pipe to jq or grep through correction /
    rollback lists.

    Exits 0 on a clean run (zero correction failures or no --apply); exits 1
    if --apply was set and any correction call failed (operator should
    investigate `correction_failures` in the JSON).
    """
    # Local import so the cli module doesn't pay the import cost on every
    # invocation (the bulk_reparse module pulls in events.write_core +
    # parsers + eventsdb).
    from dashboard.backend.events import bulk_reparse as br_mod
    events_db_path = args.events_db_path  # None → eventsdb.DB_PATH default
    try:
        result = br_mod.bulk_reparse(
            batch_id=args.batch_id,
            reparse_dir=args.reparse_dir,
            events_db_path=events_db_path,
            apply=args.apply,
            max_events=args.max_events,
        )
    except ValueError as exc:
        # reparse_dir does not exist; surface as exit 2 (same convention as
        # the other CLI subcommands for "operator argument problem").
        print(f"bulk-reparse: {exc}", file=sys.stderr)
        return 2

    summary = {
        "batch_id": args.batch_id,
        "apply": args.apply,
        "scanned": result.scanned,
        "skipped_missing_file": result.skipped_missing_file,
        "skipped_parse_failure": result.skipped_parse_failure,
        "correction_candidates": result.correction_candidates,
        "rollback_candidates": result.rollback_candidates,
        "corrections_applied": result.corrections_applied,
        "correction_batch_ids": result.correction_batch_ids,
        "correction_failures": result.correction_failures,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if result.correction_failures:
        return 1
    return 0


def cmd_load_universe(args: argparse.Namespace) -> int:
    """M3.5 brief A: load a today's-list universe from a constituents file.

    Reads one ticker per line (blank lines and `#` comments ignored), resolves
    each via `research_cik_ticker_map`, and writes the catalog row + one
    membership row per resolved CIK (entered_at=0 / left_at=NULL, the today's-list
    back-date, DECISIONS #54). Tickers must match the bridge's casing (SEC
    tickers are uppercase); a casing or coverage mismatch is reported as
    unresolvable, never silently dropped (EL66 / R14). Idempotent: re-running for
    the same --name reuses the catalog id and replaces the membership set.

    Exit 0 on a successful load (exclusions are reported, not a failure); exit 2
    on an operator-argument problem (missing/empty file).
    """
    # Local import so the cli module does not pay the events-package import cost
    # on every invocation (the bulk-reparse precedent).
    from dashboard.backend.events import universe as universe_mod

    path = Path(args.constituents_file)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"error: constituents file not found: {path}", file=sys.stderr)
        return 2

    # One ticker per line; strip whitespace, skip blank lines and # comments.
    # No case transform: a casing mismatch surfaces as an unresolvable ticker
    # (visible in the report) rather than a silent miss.
    constituents = []
    for line in text.splitlines():
        ticker = line.strip()
        if not ticker or ticker.startswith("#"):
            continue
        constituents.append(ticker)
    if not constituents:
        print(f"error: constituents file {path} has no tickers", file=sys.stderr)
        return 2

    result = universe_mod.load_universe(
        universe_name=args.name,
        constituent_list=constituents,
        source_label=args.source_label,
        events_db_path=args.events_db_path,
        research_db_path=args.research_db_path,
    )

    # Print the full report so an operator sees exactly what loaded and what did
    # not (the counted-not-dropped exclusion lists).
    summary = {
        "universe_id": result.universe_id,
        "universe_name": args.name,
        "created": result.created,
        "constituents_in": result.constituents_in,
        "loaded": result.loaded,
        "excluded": result.excluded,
        "unresolvable": result.unresolvable,
        "ambiguous": result.ambiguous,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_clean_orphan_candidates(args: argparse.Namespace) -> int:
    """SID1: list generator records whose strategy no longer exists; with --apply, delete exactly those."""
    if args.apply:
        rows = researchdb.remove_orphan_generated_candidates()
    else:
        with researchdb.get_conn() as conn:
            rows = researchdb.list_orphan_generated_candidates(conn)
    for r in rows:
        print(f"id={r['id']} theory_id={r['theory_id']} skeleton_id={r['skeleton_id']} "
              f"filler_id={r['filler_id']} strategy_id={r['strategy_id']}")
    if args.apply:
        print(f"removed {len(rows)} rows")
    else:
        print(f"dry run: {len(rows)} rows would be removed; add --apply to remove them")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser; subcommands per the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m dashboard.backend.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register-strategy", help="register a strategy .py file into the research DB")
    reg.add_argument("--file", required=True, help="path to the strategy .py file")
    reg.add_argument("--name", default=None, help="strategy name (default: the file stem)")
    reg.add_argument("--display-name", default=None, dest="display_name",
                     help="the name pages show; required when the strategy name is new")
    reg.set_defaults(func=cmd_register_strategy)

    bridge = sub.add_parser(
        "load-cik-ticker-map",
        help="populate research_cik_ticker_map from SEC's company_tickers_exchange.json",
    )
    bridge.add_argument(
        "--source-file",
        default=None,
        help="local JSON file (SEC company_tickers_exchange.json shape); default fetches the live URL",
    )
    bridge.add_argument(
        "--events-db-path",
        default=None,
        help="override the events.db path (default: the EVENTS_DB_PATH env var or config default)",
    )
    bridge.set_defaults(func=cmd_load_cik_ticker_map)

    reparse = sub.add_parser(
        "bulk-reparse",
        help="re-parse a batch's events against staged .txt files; route correction vs rollback",
    )
    reparse.add_argument("--batch-id", type=int, required=True,
                         help="events.db batch_id whose active events to re-parse")
    reparse.add_argument("--reparse-dir", required=True,
                         help="dir containing <accession_number>.txt files for each event to re-parse")
    reparse.add_argument("--apply", action="store_true",
                         help="apply corrections for company-set-unchanged events (default: dry-run)")
    reparse.add_argument("--max-events", type=int, default=None,
                         help="cap the scan at N events (debug / safety)")
    reparse.add_argument("--events-db-path", default=None,
                         help="override the events.db path")
    reparse.set_defaults(func=cmd_bulk_reparse)

    universe = sub.add_parser(
        "load-universe",
        help="load a today's-list universe (catalog row + membership) from a constituents file",
    )
    universe.add_argument("--name", required=True,
                          help="universe name (e.g. 'SP500'); the catalog row's name and symbol discriminant")
    universe.add_argument("--constituents-file", required=True,
                          help="file with one ticker per line (blank lines and # comments ignored)")
    universe.add_argument("--source-label", required=True,
                          help="provenance label stored on the catalog row and each membership row")
    universe.add_argument("--events-db-path", default=None,
                          help="override the events.db path (default: EVENTS_DB_PATH env or config default)")
    universe.add_argument("--research-db-path", default=None,
                          help="override the research.db path (default: RESEARCH_DB_PATH env or config default)")
    universe.set_defaults(func=cmd_load_universe)

    orphans = sub.add_parser(
        "clean-orphan-candidates",
        help="list (or with --apply remove) generator records whose strategy no longer exists",
    )
    orphans.add_argument("--apply", action="store_true", help="delete the listed rows (default: dry run)")
    orphans.set_defaults(func=cmd_clean_orphan_candidates)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch. Returns the subcommand's exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
