"""Durable atomic parquet write for the fetch path (pre-M4 Brief A1, the #73 fix).

The dataset fetch used to write its parquet in place (`df.to_parquet(out_path)`),
so an interrupted or power-lost re-fetch could leave a PARTIAL file at the FINAL
path while the catalog row still pointed at it — a present-but-corrupt file. The
dataset-restore presence check (researchdb.restore_dataset, §A1.5) is only honest
if a non-null file_path ALWAYS denotes a complete file, so the integrity guarantee
moves UPSTREAM into the write itself (architecture note §5; DECISIONS #73).

This module is baked into the research-runner image the fetch path runs
(research-runner:m2-latest); fetch.py imports `durable_write_parquet` from it. It is
pure pandas/pyarrow + os/pathlib — no vbt, no network — so the worker-image probe leg
(research_lab/probes/pre_m4_brief_a1_durable_write_probe.py) can exercise it
in-container without a real fetch.
"""
from __future__ import annotations

import os                                                       # low-level fsync / replace / open
from pathlib import Path                                        # path handling for the temp sibling


def durable_write_parquet(df, out_path: Path) -> None:
    """Write df to out_path as parquet via a durable atomic replace.

    The order is load-bearing (arch §5): write a sibling temp -> fsync the temp's bytes
    -> os.replace onto the final path -> fsync the directory. An interrupted write leaves
    the prior COMPLETE file at out_path untouched (or absent on a first fetch) and never a
    partial. The temp is a SIBLING of out_path (same directory, so same filesystem), which
    is what makes os.replace atomic. The function returns only after the directory fsync, so
    the caller never reports a result over a rename that is not yet durable on disk.

    Raises whatever os.replace raises (OSError EXDEV) if out_path's parent is somehow a
    cross-filesystem boundary where a same-dir rename does not hold — fail loud, never a
    silent fallback (arch §5). By construction the temp is a sibling, so on the standard
    /data single-mount layout this holds.
    """
    out_path = Path(out_path)                                   # accept a str or Path defensively
    out_dir = out_path.parent                                  # the dataset's own directory under /data
    # The temp MUST be a sibling inside out_dir (same filesystem) so os.replace is atomic — never
    # /tmp. A hidden, deterministic name: a leftover from a failed prior run is simply overwritten.
    tmp_path = out_path.with_name(f".{out_path.name}.tmp")

    # 1. Write the full parquet to the sibling temp (NOT the final path, NOT /tmp).
    df.to_parquet(tmp_path)

    # 2. fsync the temp's BYTES to disk. pyarrow's to_parquet does not expose the file
    #    descriptor and generally does not fsync on close, so reopen the temp read-only and
    #    fsync that fd before the rename.
    fd = os.open(tmp_path, os.O_RDONLY)                         # reopen purely to obtain a syncable fd
    try:
        os.fsync(fd)                                           # the temp's data is now on stable storage
    finally:
        os.close(fd)                                           # always release the fd

    # 3. The atomic rename onto the final path. On one filesystem this is atomic: a reader sees
    #    either the old complete file or the new complete file, never a half-written one.
    os.replace(tmp_path, out_path)

    # 4. fsync the DIRECTORY so the rename (a directory-entry change) is itself durable. This MUST
    #    come AFTER the rename: fsyncing the dir before the rename would leave the rename unsynced,
    #    so a power loss after os.replace could revert out_path to the prior inode (or to nothing on
    #    a first fetch) — the exact dishonesty this defends against.
    dirfd = os.open(out_dir, os.O_RDONLY)                      # a directory fd is syncable on Linux
    try:
        os.fsync(dirfd)                                        # persist the directory-entry change
    finally:
        os.close(dirfd)                                        # always release the directory fd
    # 5. Only now does control return to the caller, which then reports the fetch result.
