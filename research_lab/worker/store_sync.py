"""Copies every grain subdirectory of an EODHD store root into another root (M5 fetcher job 10, AC-2:
"resumable and read-only at the source, runs with the fetcher stopped, and never writes into the
source"). Intended to bring the laptop's consolidated replica (research_lab/worker/store_root.py's own
default) up to date from the external SSD, run manually by an operator with the fetcher stopped; this
module itself never opens or chooses /mnt/ssd -- the caller passes both paths, and no wiring anywhere
in this build points a call at that disk, per this unit's own standing rule against touching it.

Pure pathlib + shutil, no httpx, no env read at import time, matching store_root.py's own reasoning
for staying import-light enough for a plain-interpreter probe.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def _source_snapshot(source_root: Path) -> dict:
    """The ops signal M5 fetcher job 10 what-can-break mode 2 names by name: "the source's file count
    and newest timestamp recorded before and after, and a mismatch reported rather than swallowed."
    Pure reads (iteration + stat), never a write, so calling this before AND after a sync is itself
    part of the read-only guarantee, not an exception to it.
    """
    file_count = 0
    newest_mtime = None
    for p in source_root.rglob("*"):
        if not p.is_file():
            continue
        file_count += 1
        mtime = p.stat().st_mtime
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime = mtime
    return {"file_count": file_count, "newest_mtime": newest_mtime}


def sync_store(source_root: Path, dest_root: Path, dry_run: bool = False) -> dict:
    """Copy every grain subdirectory found directly under source_root into the same-named
    subdirectory under dest_root, skipping any destination file whose size already matches the
    source (see SHOULD-FIX 4 below for why size, not mere existence).

    dry_run (RA-5 gate-review SHOULD-FIX 9, 2026-09-04): when True, performs every read and the
    exact same size comparison a real call makes, and returns the SAME counts a real call would
    report, but never creates a directory or copies a byte at the destination. Reuses this same
    function's own copy/skip decision rather than a separate preview implementation, so a dry-run
    count can never drift from what a real call against the identical source/dest pair would do.
    Exists so the CLI's own --dry-run flag (this module's __main__ block, below) can answer "what
    would this do" before an operator commits to copying a real, multi-gigabyte store.

    Resumable by construction: a second call against the same two roots redoes nothing the first
    call already placed (an existing, correctly-sized destination file is left byte-and-mtime
    untouched, counted under files_skipped) and picks up whatever the source has grown since (a new
    file with no match at the destination is copied, counted under files_copied). This is a SIZE
    comparison, not a full content or mtime comparison: the source is the fetcher's own append-only
    price store, so a destination file whose size matches the source is assumed complete once placed.

    RA-5 gate-review SHOULD-FIX 4 (2026-09-04): a plain `dest_file.exists()` check, with no size or
    content test, mistook a TRUNCATED destination (the shape `shutil.copy2` leaves behind when a
    process is killed partway through writing -- this module writes straight to the destination, no
    temp-and-rename) for a complete one, and skipped it forever on every later resume. Comparing
    sizes closes that gap while still leaving an already-complete, unmodified file untouched (the
    resumable guarantee ac2_sync_resumable.probe.py already pins): a size MATCH is left alone; a size
    MISMATCH is re-copied, treated as a repair rather than a fresh copy but still counted under
    files_copied, since the observable outcome -- the destination now holds the source's own current
    bytes -- is identical either way.

    Read-only at the source by construction, not merely by policy: the only operation this function
    ever performs against a path under source_root is shutil.copy2's own source-side open, which is a
    plain read ('rb'); nothing here ever opens a source path for writing, creates, renames or deletes
    anything under source_root. A source mounted or chmod'd read-only therefore never raises a
    PermissionError from this function's own actions (AC-2's own guarantee, asserted BY MODE in the
    probe suite rather than by outcome).

    Ops monitor mode 2 ("the sync writes into its source and the next sync silently reverts it"):
    the source's own file count and newest mtime are snapshotted before and after the copy pass, and
    a mismatch (proof something wrote to, or otherwise changed, the source during this call, which a
    correctly read-only sync must never do) is printed loud rather than swallowed. Both snapshot
    values also ride along in the returned summary, so a caller can inspect them without parsing a
    log line.

    Returns a summary dict: files_copied (count of files newly copied or repaired this call, or,
    under dry_run, that WOULD be), files_skipped (count of files already present at the destination,
    correctly sized, and left untouched), source_file_count / source_newest_mtime (the source's own
    AFTER snapshot, for a caller that wants it without re-deriving it), and source_empty (RA-5
    gate-review SHOULD-FIX 3, 2026-09-04, below).

    RA-5 gate-review SHOULD-FIX 3 (2026-09-04): a source holding zero files (whether that genuinely
    means "nothing new since the last sync" or an anomaly, e.g. the source disk not mounted) used to
    produce the SAME all-zero returned dict as an ordinary already-fully-synced run, so a caller had
    no programmatic way to tell the two apart without separately inspecting source_file_count itself.
    source_empty is True exactly when the source held zero files this call (an EODHD store root that
    holds nothing at all is never a healthy state in production), so a caller can act on it (raise an
    alarm, halt an automated resync) without parsing a log line.
    """
    source_root = Path(source_root)  # accept a str or a Path, the same tolerance store_identity.py gives
    dest_root = Path(dest_root)
    files_copied = 0
    files_skipped = 0

    before = _source_snapshot(source_root)  # ops monitor mode 2: the source's own state before any copy

    # one pass per grain directory found directly under the source root, sorted for a deterministic
    # order (matches this codebase's own idiom, e.g. eodhd_hourly.py's sorted refusal-count report).
    for grain_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
        dest_grain_dir = dest_root / grain_dir.name
        if not dry_run:
            dest_grain_dir.mkdir(parents=True, exist_ok=True)  # create the matching grain dir if absent

        # every file under this grain, recursively (a grain may itself hold nested subdirectories),
        # sorted for the same deterministic-order reason as the grain pass above.
        for source_file in sorted(p for p in grain_dir.rglob("*") if p.is_file()):
            rel = source_file.relative_to(grain_dir)  # preserves any nested layout under the grain
            dest_file = dest_grain_dir / rel
            # SHOULD-FIX 4: `dest_file.exists()` alone cannot tell a complete destination from a
            # TRUNCATED one (the shape shutil.copy2 leaves behind when a process is killed mid-copy,
            # since this module writes straight to the destination with no temp-and-rename). Compare
            # sizes instead: a match means an earlier call already placed the FULL file (resumable,
            # never redone, matching ac2_sync_resumable.probe.py's own "untouched mtime" guarantee);
            # a mismatch (including the truncated-file case, where the destination is always SHORTER
            # than the source) means the destination is incomplete or stale, so it is re-copied. This
            # same comparison decides both the real count and the dry-run count (SHOULD-FIX 9): only
            # whether the copy actually happens below is gated on dry_run, never the decision itself.
            if dest_file.exists() and dest_file.stat().st_size == source_file.stat().st_size:
                files_skipped += 1  # already placed and complete: resumable, never redone
                continue
            if not dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)  # a nested rel path may need intermediate dirs
                shutil.copy2(source_file, dest_file)  # preserves mtime/perms; opens the source 'rb', read-only
            files_copied += 1  # counts a fresh copy, a repair, or (dry_run) a file that WOULD be either

    after = _source_snapshot(source_root)  # ops monitor mode 2: the source's own state after every copy
    if before != after:  # fire-only-when-mismatched: silent on the expected, correctly-read-only path
        print(
            f"store_sync: SOURCE CHANGED DURING SYNC (reason=source_mutated) "
            f"file_count={before['file_count']}->{after['file_count']} "
            f"newest_mtime={before['newest_mtime']}->{after['newest_mtime']}",
            flush=True,
        )

    return {
        "files_copied": files_copied,
        "files_skipped": files_skipped,
        "source_file_count": after["file_count"],
        "source_newest_mtime": after["newest_mtime"],
        # SHOULD-FIX 3: True exactly when the source held zero files this call, so a caller can
        # programmatically tell "the source has nothing new" apart from "the source is not mounted"
        # without separately inspecting source_file_count itself.
        "source_empty": after["file_count"] == 0,
    }


# RA-5 gate-review SHOULD-FIX 9 (2026-09-04): sync_store's own docstring already declares this
# module "run manually by an operator with the fetcher stopped," but nothing shipped an actual entry
# point an operator could run -- the gap the finding names ("no caller and no operator entry point
# anywhere"). A dashboard control is explicitly NOT this: #58 owes a UI control for every capability,
# but that is its own unit in October, and the write boundary for THIS build is research_lab/worker/,
# research_lab/docker/ and docs/units/reader_and_provenance/ only -- a new file under scripts/ or
# research_lab/fetcher/ops/ would sit outside it. So the CLI lives here, inside this module's own
# __main__ block, importable and runnable in place with no new file and no new write-boundary claim.
if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="store_sync.py",
        description=(
            "Operator CLI (M5 fetcher job 10, AC-2): copy every grain subdirectory of an EODHD "
            "store root into another root. Run manually, with the fetcher stopped, read-only at "
            "the source. Safe to re-run: resumable, and a truncated destination file is repaired."
        ),
    )
    parser.add_argument(
        "source_root", type=Path,
        help="the source store root (e.g. a mounted copy of /mnt/ssd/eodhd/raw)",
    )
    parser.add_argument(
        "dest_root", type=Path,
        help="the destination store root (e.g. the laptop's consolidated replica, ~/market_data/eodhd/raw)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be copied and skipped without writing anything to the destination",
    )
    cli_args = parser.parse_args()

    # R2-SF2 (findings fold R3, raised independently by both round-2 gates): this block used to call
    # sync_store with nothing around it, so two ordinary operator mistakes -- a source_root that does
    # not exist or is a file rather than a directory (raised inside sync_store's own
    # source_root.iterdir() call), and a dest_root that cannot be written (raised inside sync_store's
    # own dest_grain_dir.mkdir() call) -- surfaced as a raw Python traceback instead of this module's
    # own reason= idiom (compare the SOURCE CHANGED and source_empty lines below). MEASURED by hand
    # against a plain-interpreter script before this fix: source_root.rglob("*"), used by the before/
    # after snapshot, silently yields nothing for a missing or non-directory path, so the actual raise
    # happens one call later, at source_root.iterdir() in the main copy loop; a missing or unwritable
    # dest_root's parent raises at dest_grain_dir.mkdir(). Both are FileNotFoundError,
    # NotADirectoryError or PermissionError, all direct OSError subclasses, so OSError is the honest,
    # specific scope: it is not a bare except, it swallows nothing, and it neither substitutes a
    # default path nor creates the missing source, it only reports and exits.
    try:
        cli_summary = sync_store(cli_args.source_root, cli_args.dest_root, dry_run=cli_args.dry_run)
    except OSError as exc:
        print(
            f"store_sync: OPERATOR ERROR (reason=filesystem_error) path={exc.filename} "
            f"detail={exc.strerror or exc}; fix the source or destination path and re-run, this "
            f"call is resumable and will not redo any file already placed",
            file=sys.stderr, flush=True,
        )
        sys.exit(1)
    cli_label = "DRY RUN, nothing written" if cli_args.dry_run else "sync complete"
    # SHOULD-FIX 9's own "with a dry-run and the counts printed": every count from the returned
    # summary, printed once, on this same fire-always line (an operator running this by hand wants
    # to see the outcome unconditionally, not only on an anomaly, unlike the module's own
    # fire-only-when-broken monitor line above).
    print(
        f"store_sync: {cli_label}: files_copied={cli_summary['files_copied']} "
        f"files_skipped={cli_summary['files_skipped']} "
        f"source_file_count={cli_summary['source_file_count']} "
        f"source_empty={cli_summary['source_empty']}",
        flush=True,
    )
    if cli_summary["source_empty"]:
        # SHOULD-FIX 3's own signal, surfaced to the operator's own exit code too: a source with
        # zero files is never a healthy production state, so an automated caller of this CLI (e.g.
        # a cron wrapper) can detect the anomaly from the process exit status alone, without parsing
        # the printed line above.
        print("store_sync: WARNING source_empty=True (reason=source_empty); the source may not be "
              "mounted", file=sys.stderr, flush=True)
        sys.exit(1)
