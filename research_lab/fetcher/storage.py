"""Atomic parquet writes + the raw-layout path mapping (brief §3, §5.3).

Every fetch unit writes exactly ONE parquet via temp-file -> fsync -> os.replace, so a
crash before the rename leaves no half-file that could be read as 'done'. Paths follow
the §3 inventory. pyarrow writes the table; we fsync the file and its directory so the
rename is durable across a power loss (a weeks-long laptop job will see reboots).
"""

import glob  # find stale temp files orphaned by a prior crash
import os  # temp-file naming, fsync, atomic replace, makedirs

import pyarrow as pa  # build the table from the raw row dicts
import pyarrow.parquet as pq  # write the parquet

from .config import mountpoint  # the SSD mount path, re-checked on every write (mid-run unmount guard)


def relative_path(interval: str, ticker: str = None, window_start: int = None,
                  year: int = None, day_str: str = None) -> str:
    """Return the staging-root-relative parquet path for a fetch unit (brief §3).

    The 1-min path is year-partitioned with one file per window (the SF1 single-file grain);
    daily/1h/5min are one file per ticker; bulk_* are one file per UTC day.
    """
    if interval == "daily":  # per-ticker EOD full history
        return f"raw/daily/{ticker}.parquet"
    if interval == "1min":  # deep-era 1-min: year directory, one file per window
        return f"raw/1min/{ticker}/{year}/{window_start}.parquet"
    if interval == "1h":  # recent-era native hourly, one file
        return f"raw/1h/{ticker}.parquet"
    if interval == "5min":  # recent-era native 5-minute, one file
        return f"raw/5min/{ticker}.parquet"
    if interval == "bulk_eod":  # whole-exchange EOD for one day
        return f"raw/bulk/eod/{day_str}.parquet"
    if interval == "bulk_splits":  # whole-exchange splits for one day
        return f"raw/bulk/splits/{day_str}.parquet"
    if interval == "bulk_dividends":  # whole-exchange dividends for one day
        return f"raw/bulk/dividends/{day_str}.parquet"
    raise ValueError(f"unknown interval {interval!r}")  # programming guard


def _fsync_dir(dir_path: str) -> None:
    """fsync a directory so a freshly-created/renamed entry survives a power loss."""
    fd = os.open(dir_path, os.O_RDONLY)  # open the directory for fsync
    try:
        os.fsync(fd)  # flush the directory entry to disk
    finally:
        os.close(fd)  # always release the descriptor


def write_atomic(staging_root: str, rel_path: str, rows: list) -> str:
    """Write `rows` (list of dicts) to `staging_root/rel_path` atomically. Return the abs path.

    Steps: build a pyarrow table -> write to a sibling temp file -> fsync the temp file ->
    os.replace onto the final path (atomic on one filesystem) -> fsync the directory. A crash
    at any point leaves either the old file or nothing, never a partial final file.
    """
    if not os.path.ismount(mountpoint()):  # mid-run guard: SSD vanished -> never makedirs/write on the root disk
        raise OSError(f"staging mount {mountpoint()} is gone (SSD unmounted mid-run); refusing to write {rel_path}")
    final_path = os.path.join(staging_root, rel_path)  # absolute destination
    parent = os.path.dirname(final_path)  # the directory to ensure + fsync
    os.makedirs(parent, exist_ok=True)  # create the year/ticker dirs if absent
    for stale in glob.glob(f"{final_path}.tmp.*"):  # remove temps orphaned by a prior crash on THIS path
        try:
            os.remove(stale)  # never read as 'done' anyway; clean them so dirs do not accumulate dead files
        except (FileNotFoundError, PermissionError):  # a vanished/unremovable stale temp is fine to skip;
            pass  # the write below uses its own unique pid-suffixed temp anyway (a real disk error still surfaces)
    table = pa.Table.from_pylist(rows)  # raw rows -> columnar table (per-column type inferred)
    tmp_path = f"{final_path}.tmp.{os.getpid()}"  # a unique sibling temp on the same filesystem
    pq.write_table(table, tmp_path)  # write the parquet to the temp file
    fd = os.open(tmp_path, os.O_RDONLY)  # reopen to force the bytes to disk
    try:
        os.fsync(fd)  # flush the temp file's contents
    finally:
        os.close(fd)  # release
    os.replace(tmp_path, final_path)  # atomic rename onto the final path (the commit)
    _fsync_dir(parent)  # make the rename durable
    return final_path  # the path the manifest records
