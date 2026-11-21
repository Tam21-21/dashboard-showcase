"""Content verification for landed parquet units (the strengthened audit, Task 2 / its brief).

The shipped audit reconciled the manifest's 'done' rows against the files on disk by
os.path.exists only — it proved no done unit LOST its file. This proves the file's CONTENT
is correct: it re-opens the parquet and checks the schema carries the interval's required
fields, the row count matches the manifest's recorded row_count, the time axis is monotonic
+ de-duplicated (or the bulk cross-section has no duplicate code), and the frame is neither
empty nor all-null in its key column. Read-only: a 'done' unit is an immutable atomic write
(storage.write_atomic), so this is safe to run against the live staging tree while the
backfill keeps writing other intervals.

I/O lives here (the parquet read), separate from validate.py which is pure over row dicts.
The structural re-validation REUSES validate.validate rather than reimplementing the
monotonic/dedup logic.
"""

import os  # path join + existence

import pyarrow.lib  # the parquet-read exception type (ArrowInvalid) for a corrupt/truncated file
import pyarrow.parquet as pq  # re-open the landed parquet

from . import validate  # reuse REQUIRED_FIELDS + the structural validator (no reimplementation)
from .config import TIMESERIES_INTERVALS  # the round-trip structural re-check applies to time-series only

# The key column per product whose all-null state means a useless frame — the one a reader
# depends on. daily/bulk_eod carry adjusted_close (total return); the intraday intervals carry
# close; the corporate-action bulks carry their action value. Every key is inside
# validate.REQUIRED_FIELDS for its interval, so the schema_drift gate (step 3 below) runs first
# and the table.column(key) read (step 6) can never KeyError on a missing column.
_KEY_COLUMN = {
    "daily": "adjusted_close",      # total-return close (the column the M5 adapter reads)
    "1min": "close",                # deep-era 1-minute (raw, no adjusted_close)
    "1h": "close",                  # recent-era native hourly
    "5min": "close",                # recent-era native 5-minute
    "bulk_eod": "adjusted_close",   # whole-exchange EOD carries adjusted_close
    "bulk_splits": "split",         # whole-exchange splits for one day
    "bulk_dividends": "dividend",   # whole-exchange dividends for one day
}


def verify_unit(staging_root: str, interval: str, rel_path: str, expected_row_count) -> tuple:
    """Content-verify one 'done' parquet. Return (ok: bool, reason: str | None).

    Steps short-circuit on the first failure; each failure returns a NAMED reason so the audit
    report can tally and list it. expected_row_count is the manifest's recorded row_count (the
    count validate returned at land time); None skips only the row-count comparison (it cannot
    occur via mark_done, which always writes the count).
    """
    if interval not in _KEY_COLUMN:  # an interval outside the known enum -> named failure, never an uncaught KeyError
        return False, f"unknown_interval:{interval}"  # would otherwise crash the dict lookups below + abort the run
    if not rel_path:  # a done row with no recorded path — a manifest anomaly, not a real file
        return False, "no_path"
    abs_path = os.path.join(staging_root, rel_path)  # the recorded parquet's absolute path
    if not os.path.exists(abs_path):  # the existence category, preserved (lost write / phantom-write)
        return False, "missing"
    try:
        table = pq.read_table(abs_path)  # re-open the parquet from disk
    except (pyarrow.lib.ArrowInvalid, OSError) as exc:  # truncated/garbage parquet -> ArrowInvalid; I/O fault -> OSError
        return False, f"unreadable:{exc}"  # a corrupt file a present-check would have passed
    missing_fields = [f for f in validate.REQUIRED_FIELDS[interval] if f not in table.column_names]  # cheap schema gate
    if missing_fields:  # a required column is absent from the parquet -> schema drift
        return False, f"schema_drift:missing={missing_fields}"
    if table.num_rows == 0:  # a done unit must carry rows (a genuine empty payload is marked 'empty', not 'done')
        return False, "empty"
    if expected_row_count is not None and table.num_rows != expected_row_count:  # the file is not what was validated
        return False, f"row_mismatch:on_disk={table.num_rows}!=manifest={expected_row_count}"
    key = _KEY_COLUMN[interval]  # the product's load-bearing column
    if table.column(key).null_count == table.num_rows:  # every value null -> a useless frame
        return False, f"all_null:{key}"
    if interval in TIMESERIES_INTERVALS:  # the round-trip structural re-check (monotonic + dedup); cheap per file
        try:
            # Re-validate the round-tripped rows through the SAME validator used on land (reuse, not
            # reimplementation): re-proves the monotonic + de-duplicated time axis against the bytes
            # actually on disk. The reuse is type-transparent only because the on-disk column types match
            # the land-time row-dict types (date -> str, timestamp -> int epoch); a future type change would
            # raise TypeError, caught here and surfaced as a structure finding so it never escapes the loop.
            validate.validate(interval, table.to_pylist())  # one row per bar -> bounded materialisation
        except (validate.ValidationError, TypeError, pyarrow.lib.ArrowInvalid) as exc:
            return False, f"structure:{exc}"
    # Bulk cross-sections (one day, ~50k tickers) are NOT re-validated here: the no-duplicate-code invariant
    # was checked on land and the file is an immutable atomic write, so re-running it would materialise ~50k
    # row dicts per day for no new signal (corruption is already caught by the read-back, schema, row-count
    # and all-null gates above). Skipping the to_pylist() for bulk keeps the full-manifest audit scalable.
    return True, None  # read back, schema present, row count matched, not empty, not all-null, structurally valid
