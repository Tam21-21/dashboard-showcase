"""Quarantine #242's condemned intraday files (5-minute AND hourly), reversibly, never a delete.

Ruled by DECISIONS #317: 801 tickers proved contaminated by #263's price test (a strong, positive
check: their intraday bars, aggregated to daily OHLC, match a retired '_old' twin's daily record
exactly) move unconditionally. A further roughly 1,900 names the footer screen (last-bar-vs-daily-
last-bar) flags are quarantined ONLY if #263's price test also condemns them; the screen alone
condemns nothing (#317 ruling 4, AC-3). AC-4a (#326 ruling 5) adds one more refusal: a condemned
ticker with no daily file on disk never has its manifest units reset to 'pending', because the
shared bottom trim (research_lab/fetcher/tradinglife.py's spans_overlapping_life) cannot fire
without daily evidence, so a bare reset would re-download with no bottom trim at all and bring the
same contaminated bars straight back.

Every function below except `main` is SILENT: no print, no logging call, matching
strategies/helpers/sizing.py's own SIZING_MONITOR_COUNTERS discipline, because
research_lab/probes/quarantine_242/*.probe.py import plan()/execute()/etc. directly and
scripts/run_probes.sh diffs each probe's ENTIRE captured stdout against a committed .expected file
byte for byte; one stray print here would DRIFT every probe that calls it. Only `main()`, run as
`python -m research_lab.fetcher.ops.quarantine_242 <plan|execute> ...`, ever prints.

This module never touches /mnt/ssd/eodhd directly: every function takes `staging_root` as an
argument and operates on whatever path it is given, real store or a throwaway fixture copy alike.
"""

import argparse  # the plan/execute subcommands and their --staging-root/--manifest/--csv/--acknowledge flags
import datetime as dt  # calendar-day arithmetic for the footer screen's year-ahead gap
import os  # path joins, existence checks, the atomic os.replace move, directory listing
import shutil  # shutil.disk_usage: the CLI's free-space status line (what-can-break item 4)
import sqlite3  # sqlite3.Error: the specific exception type main() catches for a locked/misbehaving manifest (A7)
import sys  # sys.exit's process return code, sys.stderr for the CLI's refusal messages

import pyarrow.lib  # pyarrow's own read-failure exception type, matching tradinglife.py's import style
import pyarrow.parquet as pq  # the columnar reader: intraday bars, daily rows, and footer statistics

from research_lab.fetcher import manifest  # manifest.connect: the CLI's own read-write connection
from research_lab.fetcher import storage  # storage.relative_path: the ONE place raw paths are defined
from research_lab.fetcher import tradinglife  # daily_date_span / trading_life / _utc_date: reused, not reimplemented
from research_lab.fetcher.config import staging_root as default_staging_root  # the configured default root

# The two intraday grains this unit ever moves (AC-2's own scope: five-minute and hourly, never
# daily, never one-minute). Named once so the footer-screen union, would_move and the move loop
# below can never quietly drift onto two different lists of grains.
INTRADAY_GRAINS = ("5min", "1h")

# The default 801-style condemned-ticker CSV path (the CLI's own --csv default, per the contract).
DEFAULT_CONDEMNED_CSV = "/home/tamer/contaminated_5min.csv"

# Per-process counter for the ops routing law (job file "what can break" item 2 / AC-6's standing
# audit). In-memory ONLY, mirroring strategies/helpers/sizing.py's SIZING_MONITOR_COUNTERS: no I/O,
# no print, so incrementing this can never add a byte to a probe's captured stdout and can never
# turn a PASS into a DRIFT against a committed .expected file. A future reader (the operations page,
# parked to October per the September sprint contract) samples this from the process that ran the
# audit; until then this builder drafts AC-6's registry row into
# docs/units/quarantine_242/monitor_rows.md for the lead to paste into
# docs/ops/ops_monitor_registry.md at merge, because another lane is writing that file this round.
SPAN_AUDIT_COUNTERS: dict[str, int] = {
    "violations_found": 0,  # a 'done' 1h/5min/1min unit whose bars lie outside the ticker's own trading life
    "audits_run": 0,  # A5/QS6: incremented UNCONDITIONALLY on every span_audit_violations() call, so a reader
                      # can tell "never audited" (audits_run == 0) apart from "audited N times, genuinely
                      # clean" (audits_run >= 1 and violations_found == 0), which violations_found alone cannot say
}

# Fold QF6's own answer to Q5-B1 (CONFIRMED BLOCKER, round 5): footer_screen_names() below skips a
# ticker at TWO different sites, each via its own bare `continue`, and until this fold neither skip
# was counted anywhere. This is a SIBLING dict, never a set of new keys folded into
# SPAN_AUDIT_COUNTERS itself, because that dict's own name and comment scope it to AC-6's standing
# audit specifically; mixing an unrelated screen's counts into that namespace would make a future
# reader's grep for either concern also return the other's keys. It follows the IDENTICAL pattern
# SPAN_AUDIT_COUNTERS already establishes, per this fold's own dispatch ("follow that existing
# pattern rather than inventing a second one"): in-memory ONLY, no I/O, no print, so incrementing
# this can never add a byte to a probe's captured stdout and can never turn a PASS into a DRIFT
# against a committed .expected file. A future reader (the operations page, parked to October per
# the September sprint contract) samples this from the process that ran the screen; until then this
# builder drafts its registry rows into docs/units/quarantine_242/monitor_rows.md for the lead to
# paste into docs/ops/ops_monitor_registry.md at merge.
FOOTER_SCREEN_COUNTERS: dict[str, int] = {
    "screens_run": 0,  # fold QF6, mirroring SPAN_AUDIT_COUNTERS's own "audits_run": incremented
                       # UNCONDITIONALLY on every footer_screen_names() call, so a reader can tell
                       # "never ran" (screens_run == 0) apart from "ran repeatedly, an absent or
                       # empty interval directory every time" (screens_run >= 1, files_examined ==
                       # 0), which files_examined alone cannot say.
    "files_examined": 0,  # fold QF6: incremented once per real .parquet file this screen actually
                          # looked at, before either skip check below ever runs, so a reader can tell
                          # "ran across N files, both no-bounds counts genuinely zero" (files_examined
                          # >= 1) apart from "the interval directory was absent or empty, nothing was
                          # ever examined" (files_examined == 0), which neither no-bounds count alone
                          # could say -- the SAME "ran vs never ran" ambiguity
                          # null_ohlc_checked_ticker_count already answers for the NULL-OHLC counters,
                          # applied here to this screen instead.
    "intraday_no_bounds": 0,  # fold QF6: this grain's OWN file gave no usable footer bounds at all --
                              # unreadable, empty, missing the column, or no statistics. The
                              # SWALLOWED-ERROR class (Q5-B1): an intraday file that is supposed to be
                              # here failing to parse for screening purposes is unusual and worth a
                              # reader's attention.
    "daily_twin_no_bounds": 0,  # fold QF6: the ticker's daily twin gave no usable footer bounds, most
                                # often because no daily file exists for it at all. An ORDINARY state,
                                # not an error: counted so a reader can see how much of the store this
                                # screen could not evaluate, never to raise an alarm on its own.
}


class CrossFilesystemError(RuntimeError):
    """The quarantine root is not on the same filesystem as the staging root, so a 'move' would
    silently degrade into a copy plus a delete. STOP IF 2 (job file): #317 authorised a move, not a
    delete, so execute() refuses before touching a single file rather than papering over the gap."""


class Reverify801DisagreementError(RuntimeError):
    """The narrow price test genuinely CLEARS one or more 801-listed names (STOP IF 3): a real,
    performed comparison came back negative. execute() refuses the WHOLE run unless the caller
    passes acknowledge_801_disagreements=True, and even then the disagreeing name's own files stay
    excluded from the move (#317 does not authorise moving evidence that disagrees with itself)."""


class QuarantineReadError(RuntimeError):
    """A file this module reads DIRECTLY (a daily or intraday parquet, or the condemned-ticker CSV)
    exists but could not be parsed. Raised at the ONE read call that can fail, immediately around it,
    rather than left to propagate as the bare OSError/ArrowInvalid/UnicodeDecodeError Python itself
    raises: main() then needs exactly one except clause naming THIS type to cover every present and
    future direct-read call site, instead of one new except clause per site as each is discovered
    (root cause of Q3-SF2/Q4-SF2: _read_daily_rows and _aggregate_intraday_by_date let the raw
    exception through unwrapped, so main() had no name to catch it by). Mirrors
    tradinglife.TradingLifeUnreadable's own contract ("Surfaced, never silently ignored") for the
    files this module reads on its OWN behalf rather than through tradinglife.py; main() catches both
    together, since an operator reading a REFUSED: line needs the same one thing from either: which
    file, and why. A FUTURE read site raises one of these two names at its own read call and needs no
    new except clause in main() at all."""


class QuarantineFilesystemError(RuntimeError):
    """An operation this module performs directly against the filesystem, OTHER than parsing a
    file's content (QuarantineReadError's own scope), fails with an OS-level error: comparing two
    paths' devices (same_filesystem), listing a directory (footer_screen_names, restore_all), or
    moving a file into place (execute()'s move loop, restore_one). Raised at the ONE call that can
    fail, immediately around it, rather than left to propagate as the bare OSError Python itself
    raises: main() then needs exactly one except clause naming THIS type, beside QuarantineReadError,
    to cover every present and future direct filesystem-operation call site, instead of one new
    except clause per site as each is discovered (root cause of the round-4/round-5 recurrence on
    class unhandled-read-error-as-traceback, Q4-SF2/Q5-SF1: QuarantineReadError's own rule covered a
    content read only because only a content read had been changed to raise it, so a metadata syscall
    or a move still reached main() as whatever the operating system threw). A FUTURE
    filesystem-operation site raises this at its own call and needs no new except clause in main() at
    all, mirroring QuarantineReadError's own contract for content reads exactly. NOT
    CrossFilesystemError: that name is a POLICY refusal decided BEFORE any operation runs (STOP IF 2,
    the quarantine root is not co-located with staging_root at all); this name is the SAME check, or
    a sibling filesystem operation, FAILING OUTRIGHT at the OS level for a reason outside this
    module's control (permission, or a race)."""


def quarantine_root(staging_root: str) -> str:
    """The quarantine directory: a SIBLING of raw/, never inside it (AC-1's own absence proof)."""
    return os.path.join(staging_root, "quarantine")  # staging_root/quarantine, outside every raw/ read path


def quarantine_rel_path(interval: str, ticker: str) -> str:
    """The quarantine-relative path for one moved file: 'quarantine/<interval>/<ticker>.parquet'.

    5min/1h only (AC-2's own scope): a caller asking for any other grain is a programming error,
    exactly like storage.relative_path's own unknown-interval guard.
    """
    if interval not in INTRADAY_GRAINS:  # this unit never quarantines daily or one-minute files
        raise ValueError(f"quarantine_rel_path: unsupported interval {interval!r}, expected one of {INTRADAY_GRAINS}")
    return f"quarantine/{interval}/{ticker}.parquet"  # mirrors storage.relative_path's own 5min/1h shape


def _nearest_existing(path: str) -> str:
    """Walk up from `path` to the nearest ancestor directory that actually exists.

    same_filesystem() needs a real path to os.stat(): the quarantine root may not exist yet on a
    first-ever run, and its own parent, the staging root, always does, so this lets the colocation
    check run BEFORE any write at all rather than requiring a directory to be created first.
    """
    candidate = path  # start at the path itself
    while not os.path.exists(candidate):  # walk up until something on disk actually answers os.stat
        parent = os.path.dirname(candidate)  # one level up
        if parent == candidate:  # reached the filesystem root without finding anything: cannot happen in practice
            raise OSError(f"same_filesystem: no existing ancestor found for path {path!r}")
        candidate = parent  # keep climbing
    return candidate  # the nearest real, stat-able directory


def same_filesystem(path_a: str, path_b: str) -> bool:
    """True iff the two paths sit on the same filesystem device (os.stat().st_dev comparison).

    Either path may not exist yet; both are resolved to their nearest existing ancestor first, so
    this never requires creating a directory before it can answer.

    Raises QuarantineFilesystemError if either nearest-existing ancestor cannot be stat()'ed
    (permission, or a race where it vanishes between the walk-up and this call): this runs on EVERY
    execute() as the cross-device precondition (STOP IF 2), so a bare OSError here reaching main()
    unnamed was the single most-reachable site the lane-Q-round-5 enumeration found (Q5-SF1).
    """
    try:
        dev_a = os.stat(_nearest_existing(path_a)).st_dev  # the device number backing path_a
        dev_b = os.stat(_nearest_existing(path_b)).st_dev  # the device number backing path_b
    except OSError as exc:  # permission denied, or the ancestor vanished between the walk-up and this stat
        raise QuarantineFilesystemError(
            f"cannot determine the filesystem for {path_a!r} or {path_b!r}: {exc!r}") from exc
    return dev_a == dev_b  # same device -> a rename between them is a single atomic metadata update


def load_condemned_csv(csv_path: str) -> set:
    """Return the set of tickers in the 801-style condemned CSV's first column.

    The header row is skipped BY CONTENT (its first field literally reads 'ticker'), not by line
    position, so the file's shape is verified rather than assumed.

    Raises QuarantineReadError if csv_path does not exist, cannot be opened, or is not valid UTF-8
    text. This is plan()'s OWN FIRST CALL, reached before any other work on every plan/execute
    invocation: an operator-supplied --csv path that is missing or corrupt must refuse loudly here
    rather than reach main() as a raw traceback, the identical class Q3-SF2/Q4-SF2 named at this
    module's parquet read sites (see QuarantineReadError's own docstring).
    """
    tickers = set()  # accumulate the ticker column
    try:
        with open(csv_path, "r", encoding="utf-8") as fh:  # the CSV is small (hundreds of rows); read line by line
            for line in fh:  # walk every line, header included
                line = line.rstrip("\n")  # drop the trailing newline
                if not line:  # a blank line carries no ticker
                    continue
                first_field = line.split(",")[0]  # the ticker column is always first
                if first_field == "ticker":  # the header row, detected by its own content
                    continue
                tickers.add(first_field)  # a real condemned ticker
    except (OSError, UnicodeDecodeError) as exc:  # missing/unreadable path, or not valid UTF-8 text
        raise QuarantineReadError(f"condemned-ticker CSV {csv_path!r} could not be read: {exc!r}") from exc
    return tickers  # the 801-style set, unconditional (#317 ruling 4)


def daily_file_missing(staging_root: str, ticker: str) -> bool:
    """True iff the ticker has no daily parquet on disk at all (AC-4a's own trigger).

    Reuses tradinglife.daily_date_span directly, the SAME function the shared bottom trim itself
    calls, so this can never drift from what the fetch guard actually sees.
    """
    return tradinglife.daily_date_span(staging_root, ticker) is None  # None = no daily evidence recorded


def _footer_bounds(path: str, column: str):
    """Read the MIN and MAX of `column` from a parquet file's FOOTER STATISTICS only: no row is
    decoded. Returns (lo, hi), or None if the file is unreadable, empty, lacks the column, or
    carries no usable statistics. Generalises the method
    scripts/measurements/2026-09-02_store_raggedness/{last_bar_spread,grain_alignment}.py already
    use, but with SPECIFIC exception types (OSError, pyarrow.lib.ArrowInvalid) rather than those
    two scripts' own bare `except Exception`, matching tradinglife.py's tighter, core-path discipline.
    """
    try:
        meta = pq.ParquetFile(path).metadata  # the parquet footer: row-group stats, no row decode
    except (OSError, pyarrow.lib.ArrowInvalid):  # unreadable file: treat as "no usable statistic"
        return None
    if meta.num_rows == 0 or column not in meta.schema.names:  # an empty file, or the column is absent
        return None
    col_index = meta.schema.names.index(column)  # the column's position for the row-group stats lookup
    lo = hi = None  # accumulate across every row group
    for g in range(meta.num_row_groups):  # a file may hold more than one row group
        st = meta.row_group(g).column(col_index).statistics  # this group's min/max, or None
        if st is None or st.min is None or st.max is None:  # a group with no usable statistic
            continue
        lo = st.min if lo is None else min(lo, st.min)  # running minimum across groups
        hi = st.max if hi is None else max(hi, st.max)  # running maximum across groups
    if lo is None or hi is None:  # no row group carried a usable statistic
        return None
    return lo, hi  # the file's overall (min, max) for this column, footer-only


def _as_date(value) -> "dt.date":
    """Convert a footer statistic value (an intraday epoch int, or a daily 'YYYY-MM-DD' string)
    into a plain calendar date, so grains recorded on different axes can be compared on one."""
    if isinstance(value, str):  # a daily 'date' column value
        return dt.date.fromisoformat(value[:10])  # tolerate a trailing time component defensively
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).date()  # an intraday 'timestamp' epoch


def footer_screen_names(staging_root: str, interval: str, years_ahead_days: float = 365.0) -> set:
    """Flag a ticker whose `interval` file's LAST bar is more than `years_ahead_days` past its OWN
    raw/daily/<ticker>.parquet's LAST date: footer statistics only, no bar decoded, generalising
    scripts/measurements/2026-09-02_store_raggedness's own method into a reusable function. This is
    a SCREEN, not proof: AC-3 gates every name it flags through the narrow price test before moving.

    Raises QuarantineFilesystemError if the directory exists (the os.path.isdir check just below
    passed) but cannot be listed (permission, or a race where it is removed between that check and
    this call): the SAME rule QuarantineReadError already applies to a content read, applied here to
    a directory listing instead (Q5-SF1).

    Fold QF6 REVERSES one line of `docs/units/quarantine_242/rootcause_declared_mode_no_monitor.md`,
    quoted verbatim from its own lines 130-131: "Not a declared failure mode in the ops-routing
    sense; no counter needed; none added." That ruling covered eight skip/refusal sites across
    `_footer_bounds` and this function together in one paragraph; round 5's claims-auditor and
    lifecycle-checker both re-read it for THIS function's own two `continue` sites specifically and
    neither signed it off (Q5-B1, CONFIRMED BLOCKER under the sprint's own narrowed severity bar: "a
    declared failure mode with no monitor" reads as a swallowed error once a critic has looked at it
    twice and still finds nothing counting it). The reversal is narrow, not a rewrite of the whole
    ruling: `_footer_bounds`'s OWN internal behaviour is UNCHANGED, and the two hygiene skips this
    function also carries (a non-directory grain at the top, a stray non-parquet filename in the
    loop) stay uncounted, since a screen that UNDER-flags rather than MIS-condemns remains the
    correct, deliberate design AC-3 relies on -- nothing here aborts the scan, raises, or changes
    which ticker gets flagged. What changes is only that the two sites below now increment
    `FOOTER_SCREEN_COUNTERS`, so a swallowed skip has a NUMBER standing behind it instead of
    vanishing into a bare `continue`. The two sites are NEVER merged into one counter: site 1 (this
    grain's OWN file gives no usable bounds) is the SWALLOWED-ERROR class, since an intraday file
    that is supposed to be here failing to parse is unusual; site 2 (the daily twin gives no usable
    bounds, most often because it does not exist) is an ORDINARY state, not an error. A shared
    counter would destroy the one distinction it exists to carry.
    """
    FOOTER_SCREEN_COUNTERS["screens_run"] += 1  # fold QF6: unconditional, before any directory check,
                                                # mirroring SPAN_AUDIT_COUNTERS["audits_run"]'s own site
    flagged = set()  # the screened-in ticker set for this grain
    interval_dir = os.path.join(staging_root, "raw", interval)  # where this grain's files live
    if not os.path.isdir(interval_dir):  # nothing written yet at this grain: nothing to screen
        return flagged
    try:
        entries = sorted(os.listdir(interval_dir))  # deterministic order for reproducible reports
    except OSError as exc:  # permission denied, or interval_dir vanished after the isdir check above
        raise QuarantineFilesystemError(f"cannot list {interval_dir!r}: {exc!r}") from exc
    for fname in entries:
        if not fname.endswith(".parquet"):  # ignore any stray non-parquet entry
            continue
        ticker = fname[: -len(".parquet")]  # the bare ticker this file belongs to
        FOOTER_SCREEN_COUNTERS["files_examined"] += 1  # fold QF6: one more real candidate file this
                                                        # call genuinely looked at, counted before
                                                        # either skip check below runs
        intraday_bounds = _footer_bounds(os.path.join(interval_dir, fname), "timestamp")  # (min, max) epoch
        if intraday_bounds is None:  # unreadable or empty: cannot screen this one
            FOOTER_SCREEN_COUNTERS["intraday_no_bounds"] += 1  # fold QF6: the swallowed-error class, named
            continue
        daily_path = os.path.join(staging_root, storage.relative_path("daily", ticker=ticker))  # its own daily twin
        daily_bounds = _footer_bounds(daily_path, "date")  # (min, max) 'YYYY-MM-DD' strings
        if daily_bounds is None:  # no daily file to compare against at all: cannot screen this one
            FOOTER_SCREEN_COUNTERS["daily_twin_no_bounds"] += 1  # fold QF6: an ordinary state, named
            continue
        gap_days = (_as_date(intraday_bounds[1]) - _as_date(daily_bounds[1])).days  # intraday last minus daily last
        if gap_days > years_ahead_days:  # more than a year ahead: the screen's own line (nothing legitimate is)
            flagged.add(ticker)
    return flagged  # a screen, gated by the price test before it ever moves anything (AC-3)


def _drop_null_ohlc_rows(rows: list) -> tuple:
    """The ONE place a row missing any of open/high/low/close is filtered out of a raw parquet row
    list. Returns (kept_rows, dropped_count). Both `_read_daily_rows` and `_aggregate_intraday_by_date`
    call this rather than each re-implementing the same four-field NULL check: Q2-B1's own root cause
    was that this exact filter existed TWICE, independently, one copy wired to a counter and the other
    not, because the two copies could drift without either one noticing. One implementation left means
    a caller that wants the drop counted has only ONE place to wire a counter into, not two to
    remember, so the class this defect belongs to (a fold answers the named instance, not the shared
    idiom underneath it) cannot recur here again.
    """
    kept = [r for r in rows if r["open"] is not None and r["high"] is not None
           and r["low"] is not None and r["close"] is not None]  # every row with a genuinely usable OHLC
    return kept, len(rows) - len(kept)  # (survivors, how many were just dropped)


def _read_daily_rows(staging_root: str, ticker: str, null_drop_counter: list | None = None):
    """Read a ticker's daily file (if any) as {date: (open, high, low, close)}, RAW values only,
    never adjusted_close (#263's own cited numbers compare raw OHLC). Returns None if the file does
    not exist at all (a real, structural distinction from 'exists but carries zero rows').

    A row missing any of open/high/low/close is dropped here (A6/QS8), via the SAME
    `_drop_null_ohlc_rows` helper `_aggregate_intraday_by_date` calls, so the two siblings' NULL-OHLC
    discipline cannot drift apart the way it did at Q2-B1 (one counted, one silent): without the
    filter, a NULL daily row on a candidate date reads as "genuinely compared, genuinely disagreed" in
    price_test_verdict (a tuple carrying a None never equals a real one, so the comparison always
    fails), corrupting STOP IF 3's own disagreement count on garbage rather than on real evidence. With
    the filter, that same date simply has no usable daily row, exactly like a date the file never
    covered at all: UNDECIDABLE, never a false CLEARS. Measured against the real store 2026-09-03: 838
    daily files for the 801 condemned tickers, 1,411,786 rows, zero NULL today, so this is latent
    rather than live -- but the intraday side of these SAME tickers is up to 98 percent NULL (A2's own
    measurement), so the fix lands symmetrically now rather than waiting for a real daily NULL row to
    appear.

    `null_drop_counter`, if given, rides the same one-element-list, per-call-accumulator idiom
    `_aggregate_intraday_by_date` already uses (A2/QB1): never overwritten, only added into, so a
    caller threading the SAME list through both siblings gets one honest combined total. Omitted by
    default, so every EXISTING caller keeps its UNCHANGED OBSERVABLE CONTRACT.

    Surfaces, never swallows, a read failure: an unreadable daily file at this exact point is the
    price test's own core evidence path, so a corrupt file must fail loud rather than quietly pass a
    name that should have been condemned or cleared on real evidence. Raises QuarantineReadError
    (Q3-SF2/Q4-SF2's own root-cause fix: a NAMED exception raised AT this read call, never the bare
    OSError/ArrowInvalid Python itself raises, which main() has no way to catch by name), caught once
    in main() alongside tradinglife.TradingLifeUnreadable.
    """
    path = os.path.join(staging_root, storage.relative_path("daily", ticker=ticker))  # the daily parquet path
    if not os.path.exists(path):  # no daily record for this ticker at all
        return None  # None, never {}: "absent" and "empty" are different facts
    try:
        table = pq.read_table(path, columns=["date", "open", "high", "low", "close"])  # raw OHLC only
    except (OSError, pyarrow.lib.ArrowInvalid) as exc:  # exists but corrupt: Q3-SF2/Q4-SF2's own class
        raise QuarantineReadError(f"{ticker}: cannot read daily history at {path}: {exc!r}") from exc
    kept_rows, dropped_count = _drop_null_ohlc_rows(table.to_pylist())  # A6/QS8, via the shared helper
    if null_drop_counter is not None:  # a caller wants the running per-call total (plan()/execute() do)
        null_drop_counter[0] += dropped_count  # accumulate: many files can feed one plan() call's total
    return {row["date"]: (row["open"], row["high"], row["low"], row["close"]) for row in kept_rows}


def _aggregate_intraday_by_date(staging_root: str, ticker: str, interval: str,
                                null_drop_counter: list | None = None) -> dict:
    """Read an intraday file's bars (if any) and fold them into one OHLC row per calendar date:
    open = the chronologically first bar's own open, high = the max high, low = the min low,
    close = the chronologically last bar's own close, all RAW. Returns {date: (o,h,l,c)}, or {} if
    the file does not exist (nothing to aggregate reads downstream exactly like "no candidate date
    at all"). Also surfaces, never swallows, a read failure, via the SAME QuarantineReadError
    _read_daily_rows raises; see its own docstring for the reasoning and what main() does with it.

    `null_drop_counter`, if given, is a one-element list this function ADDS its own NULL-dropped-row
    count into (never overwrites): a per-call accumulator threaded down from plan() (A2/QB1), never a
    module-level counter, because execute() calls plan() internally exactly once and a module-level
    counter would double-count the same drops between a caller's own direct plan() call and
    execute()'s internal one. The NULL-OHLC filter itself lives in `_drop_null_ohlc_rows`, the ONE
    place both this function and `_read_daily_rows` filter a row missing open/high/low/close (Q2-B1):
    the two siblings share the filter so they cannot drift the way they did when each held its own
    copy and only one copy was wired to a counter.
    """
    path = os.path.join(staging_root, storage.relative_path(interval, ticker=ticker))  # the intraday parquet path
    if not os.path.exists(path):  # nothing written at this grain for this ticker
        return {}  # an empty aggregate, not an error: price_test_verdict reads this as UNDECIDABLE
    try:
        table = pq.read_table(path, columns=["timestamp", "open", "high", "low", "close"])  # raw OHLC + epoch
    except (OSError, pyarrow.lib.ArrowInvalid) as exc:  # exists but corrupt: Q3-SF2/Q4-SF2's own class
        raise QuarantineReadError(f"{ticker} {interval}: cannot read intraday history at {path}: {exc!r}") from exc
    rows = table.to_pylist()  # materialise: a condemned file is at most a few years of 5min/1h bars
    # A real EODHD intraday file can carry a bar with a NULL OHLC field (measured 2026-09-04 against
    # the real store: ACBA_old.US and other condemned names hold at least one such row). A bar
    # missing any of open/high/low/close cannot contribute a trustworthy value to an EXACT-match
    # aggregate, so it is dropped here rather than crashing or silently corrupting the day's OHLC;
    # this mirrors DECISIONS #263's own principle ("missing data is honest and visible while
    # contaminated data is well-formed"): a date left with no usable bars simply never becomes a
    # candidate date, which is the same safe outcome as the file never having a bar there at all.
    kept_rows, dropped_count = _drop_null_ohlc_rows(rows)  # A2/QB1, via the shared helper _read_daily_rows also uses
    if null_drop_counter is not None:  # a caller wants the running per-call total (plan()/execute() do)
        null_drop_counter[0] += dropped_count  # accumulate: many files can feed one plan() call's total
    rows = kept_rows  # continue with only the bars that survived the filter
    rows.sort(key=lambda r: r["timestamp"])  # chronological, regardless of on-disk order, so first/last are well-defined
    by_date: dict = {}  # date -> running OHLC accumulator
    for row in rows:  # walk bars in time order, every field already known non-null
        date = tradinglife._utc_date(row["timestamp"])  # the calendar date this bar belongs to (UTC)
        if date not in by_date:  # the first bar seen for this date
            by_date[date] = {"open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"]}
        else:  # a later bar on the same date
            agg = by_date[date]  # the running accumulator for this date
            agg["high"] = max(agg["high"], row["high"])  # the running max high
            agg["low"] = min(agg["low"], row["low"])  # the running min low
            agg["close"] = row["close"]  # overwritten every time: ends up being the LAST bar's close
    return {date: (v["open"], v["high"], v["low"], v["close"]) for date, v in by_date.items()}  # plain tuples


def _twin_ticker(ticker: str) -> str:
    """The same-base sibling differing only by the '_old' suffix, checked in BOTH directions:
    a retired '_old'-suffixed ticker's twin is the plain string; a plain ticker's twin is itself
    plus '_old.US'. Mirrors research_lab/fetcher/ops/narrowing_dry_run.py's own _reused_tickers
    convention, the tree's only other place this exact naming rule is already applied and reviewed.
    """
    suffix = "_old.US"  # EODHD's own retirement marker
    if ticker.endswith(suffix):  # a retired ticker: strip the suffix, restore the plain form
        return ticker[: -len(suffix)] + ".US"
    return ticker.replace(".US", "_old.US")  # a plain ticker: its retired twin, if EODHD ever reused the string


def price_test_verdict(staging_root: str, ticker: str, interval: str,
                       null_drop_counter: list | None = None) -> str:
    """#263's price test, operationalised, three-valued: 'CONDEMNS' / 'CLEARS' / 'UNDECIDABLE'.

    A CANDIDATE DATE is a date the ticker's OWN daily record has no row for (own daily absent
    entirely -> every aggregated date is a candidate). CONDEMNS wins on any checked match; else
    CLEARS if at least one candidate date's twin daily row was actually compared and disagreed;
    else UNDECIDABLE (no twin file, no candidate date, or the twin never had a row to compare).

    `null_drop_counter`, if given, rides straight through to _aggregate_intraday_by_date AND to both
    _read_daily_rows calls below, own and twin alike (A2/QB1's own per-call accumulator, now covering
    both the intraday and the daily side, Q2-B1). Omitted by default, so every EXISTING caller
    (q06-q12) keeps its UNCHANGED OBSERVABLE CONTRACT: this function's return value never depends on
    whether the caller happens to be counting drops.
    """
    agg = _aggregate_intraday_by_date(staging_root, ticker, interval,
                                      null_drop_counter=null_drop_counter)  # {date: (o,h,l,c)} or {}
    if not agg:  # no intraday bars at all to test
        return "UNDECIDABLE"
    own_daily = _read_daily_rows(staging_root, ticker,
                                 null_drop_counter=null_drop_counter)  # None (no file) or a dict (possibly empty)
    own_dates = set(own_daily) if own_daily is not None else set()  # absent daily -> no dates are "own"
    candidate_dates = sorted(date for date in agg if date not in own_dates)  # dates the own record is silent on
    if not candidate_dates:  # the own daily record already covers every aggregated date
        return "UNDECIDABLE"
    twin_daily = _read_daily_rows(staging_root, _twin_ticker(ticker),
                                  null_drop_counter=null_drop_counter)  # None (no twin file) or a dict
    if twin_daily is None:  # no twin file at all: nothing to compare against
        return "UNDECIDABLE"
    compared = False  # True once at least one candidate date's twin row was actually checked
    for date in candidate_dates:  # walk the dates the own record cannot explain
        if date in twin_daily:  # the twin genuinely has a row for this date: a real comparison
            compared = True
            if twin_daily[date] == agg[date]:  # exact OHLC match: a match on ANY checked date wins outright
                return "CONDEMNS"
    return "CLEARS" if compared else "UNDECIDABLE"  # a real, negative check vs. nothing ever compared


def price_test_passes(staging_root: str, ticker: str, interval: str,
                      null_drop_counter: list | None = None) -> bool:
    """True unless the price test positively CONDEMNS this (ticker, interval): CLEARS and
    UNDECIDABLE both pass, because AC-3's screen condemns nothing on its own, whatever the reason
    the price test did not condemn. Unchanged observable contract across the QA-2 three-way split.

    `null_drop_counter` rides straight through to price_test_verdict (A2/QB1), omitted by default.
    """
    return price_test_verdict(staging_root, ticker, interval,
                              null_drop_counter=null_drop_counter) != "CONDEMNS"  # only a positive match refuses this


def reverify_801(csv_path: str, staging_root: str, null_drop_counter: list | None = None) -> dict:
    """STOP IF 3, three-valued (QA-2), over a FOUR-WAY partition of the 801 list (this fold names the
    fourth bucket; the three-valued VERDICT price_test_verdict itself returns is unchanged). For every
    801-csv ticker carrying at least one real 5min/1h file: CONDEMNS on any present grain wins
    outright and is named into "reconfirmed" (agrees with the 801 list, not a disagreement -- until
    this fold this case fell through a bare `continue`, uncounted, the SAME shape of hole
    "no_file_to_test" was itself named to close, below, one round earlier); else CLEARS on any present
    grain is a genuine disagreement; else (every present grain UNDECIDABLE) the ticker is reported
    undecidable, never a disagreement. A ticker with NO present grain at all carries nothing to test
    and lands in the fourth bucket, "no_file_to_test" (A3/QS4): at base THIS case fell through into
    neither set and its size appeared nowhere, so "801 of 801 reconfirmed" and "none of the 801 had a
    file left to test" printed identically; naming it was what made the two states distinguishable
    again. The four buckets now PARTITION `load_condemned_csv(csv_path)` exhaustively -- their sizes
    sum to the condemned count -- so a reader can tell "every present-grain ticker reconfirmed,
    genuinely" apart from "the 801 list was empty, nothing ran" without any of the four reading as an
    unexplained zero.

    `null_drop_counter`, if given, rides straight through to every price_test_verdict call this makes
    (A2/QB1's own per-call accumulator). Omitted by default.
    """
    reconfirmed = set()  # CONDEMNS on any present grain: agrees with the 801 list, named rather than discarded
    disagreements = set()  # CLEARS only: the genuine, checked disagreements
    undecidable = set()  # every present grain UNDECIDABLE: could not be re-tested either way
    no_file_to_test = set()  # A3/QS4: no present grain at all, nothing to test, a fourth distinct bucket
    for ticker in load_condemned_csv(csv_path):  # every 801-listed name, unconditional membership aside
        present_grains = [iv for iv in INTRADAY_GRAINS  # only grains this ticker genuinely has on disk
                          if os.path.exists(os.path.join(staging_root, storage.relative_path(iv, ticker=ticker)))]
        if not present_grains:  # nothing to test: named now, rather than silently landing in neither set
            no_file_to_test.add(ticker)
            continue
        verdicts = [price_test_verdict(staging_root, ticker, iv, null_drop_counter=null_drop_counter)
                   for iv in present_grains]  # per present grain
        if "CONDEMNS" in verdicts:  # any grain reconfirming it wins outright: agrees with the 801 list
            reconfirmed.add(ticker)  # named, not discarded: this fold's own fix, same shape as no_file_to_test
            continue
        if "CLEARS" in verdicts:  # any grain genuinely, positively disagreeing
            disagreements.add(ticker)
        else:  # every present grain came back UNDECIDABLE
            undecidable.add(ticker)
    return {"reconfirmed": reconfirmed, "disagreements": disagreements, "undecidable": undecidable,
           "no_file_to_test": no_file_to_test}


def _quarantine_conflicts(would_move: dict, staging_root: str) -> list:
    """Every (ticker, interval) in `would_move` whose quarantine-side destination already holds a
    file (A1/QB2): a real collision a forward os.replace would silently clobber, since os.replace
    carries no destination check of its own. Read-only: used by plan() for the operator-facing
    preview BEFORE any move runs. execute()'s own move loop re-checks os.path.exists again, live,
    immediately before each actual os.replace, since a plan()-time answer can go stale between the
    two calls; this function's own result is a preview, never the authority the move loop trusts.

    This is the ONLY place a collision is detected anywhere in this module, and it is detected purely
    from two os.path.exists() checks at two fixed paths, with no persisted state behind either one
    (Q3-SF1's own root cause). That is why a collision this call reports today can vanish from every
    later call's report without this module ever having recorded that it happened: whatever changed
    on disk between two calls is the whole of what this function, or any caller of it, can ever know.
    """
    conflicts = []  # accumulate every colliding pair
    for ticker, interval in would_move:  # every pair genuinely on disk at the raw/ side
        dest = os.path.join(staging_root, quarantine_rel_path(interval, ticker))  # where it would land
        if os.path.exists(dest):  # already occupied: a forward move here would silently clobber it
            conflicts.append((ticker, interval))
    return sorted(conflicts)  # deterministic order, matching every other pairs list in this module


def _quarantine_conflict_detail(ticker: str, interval: str, raw_abs: str, quarantine_abs: str) -> dict:
    """One skipped (ticker, interval) collision, expanded past the bare pair `_quarantine_conflicts`
    and execute()'s own skip both already compute (Q2-SF1, found independently by all three round-2
    gates). The skip itself is correct and stays unchanged here: #317 authorised a MOVE, never a
    DELETE, so this tool must not pick a winner between two copies of evidence. What Q2-SF1 names is
    that the skip on its own leaves a PERMANENT STANDOFF nothing in this module can clear:
    restore_one() refuses the identical pair from the other direction too (its own guard sees the raw
    path already occupied), so the same pair skips again on every later plan()/execute() call, forever,
    with the operator told only a bare (ticker, interval) pair and nothing about how to end it.

    Q3-SF1's own root cause, answered here: this module keeps NO memory, across runs, of a pair it
    once flagged as colliding. Its entire view of a collision is two os.path.exists() checks at two
    fixed paths (see _quarantine_conflicts), so whichever of the two files the operator moves by hand
    decides, silently and permanently, what this tool can ever say about the pair again. The message
    this function built BEFORE this fold asserted one blanket promise, "re-run execute and this pair
    will then complete", true only for the choice the message's own wording did NOT favour, false for
    the one it did. This version drops that promise and names both outcomes instead. It still cannot
    RECORD which one the operator chose, because no persisted state exists here to record it into, so
    it says that plainly too rather than implying a memory this module does not have. Filling that gap
    for real needs a mechanism (a small persisted marker of "this pair was flagged"); the root-cause
    report for this fold names it as owed to docs/log/deferred_mechanisation.md, outside this fold's
    own write boundary to add.

    Returns both absolute paths plus one message naming what the operator must decide and what happens
    next for EACH choice, built ONCE here so plan()'s read-only preview, execute()'s own move loop AND
    restore_one()'s reverse-direction refusal all describe the identical collision the same way,
    rather than the two independently authored sentences that had already drifted apart once (Q3-SF1's
    own shape: execute()'s message needed correcting while restore_one() kept its own separate copy;
    this fold ends that by having restore_one() call this same function too).

    Q4-SF1, round 4, SHOULD-FIX (docs/units/quarantine_242/findings.tsv line 193): Q3-SF1 made this
    message HONEST; round 4 judged it as operator-facing text read at 3am and found the FORM fails,
    quoted verbatim from that finding: "one unbroken paragraph with no newline, printed as a single
    physical CLI line", both branches opening with "the near-identical words 'Move the ... file
    aside'". The gate's own test: can somebody holding two files at 3am act on this correctly and
    once? The fix below keeps every fact the pre-round-4 text carried and only changes the LAYOUT: a
    preamble, then two choices, each opened by a distinct "CHOICE 1:" / "CHOICE 2:" label so the
    differentiator is the very first thing read rather than something buried past nine identical
    words, and each closed by an explicit "Consequence:" clause so the outcome is never left implicit.

    Decision, argued rather than assumed: the return value stays ONE dict with ONE "message" string
    (now carrying embedded newlines), not three separate fields (preamble/choice_1/choice_2) that
    every caller would have to reassemble on its own. The whole point of this function existing at all
    is Q3-SF1's own fix: ONE authored description of this collision, called by plan()'s preview,
    execute()'s move loop AND restore_one()'s reverse-direction refusal alike, so a later fold can
    correct it in exactly one place. Returning parts instead of a whole would hand that same
    reassembly job to two separate call sites (_print_report and restore_one()) and reopen precisely
    the drift Q3-SF1 closed: two independently authored copies of one real-world fact, free to diverge
    the next time only one of them is touched. A single, fully assembled string is also what
    restore_one()'s `FileExistsError` needs verbatim (Python renders an exception's message as
    `str(exc)`, one string, not a dict of parts), so this shape needs no glue code at either call site
    at all: `restore_one()` still does exactly `raise FileExistsError(_quarantine_conflict_detail(...)
    ["message"])`, unchanged, and q32's own check that this text is IDENTICAL to what the collision
    path builds keeps passing on the identical mechanism it always tested.
    """
    preamble = (  # Q4-SF1: the shared context both choices need, read once, before either choice
        f"{ticker} {interval}: a file exists at BOTH the raw path {raw_abs!r} and the quarantine path "
        f"{quarantine_abs!r}, so this tool refuses to touch either one automatically. These are two "
        f"different downloads of the same ticker and interval: the quarantine copy is the evidence "
        f"#242 condemned, and the raw copy is whatever the fetcher wrote afterwards. This tool will "
        f"not choose between them (#317 authorised a move, never a delete), and it keeps NO record, "
        f"across runs, of which one you move, so read both outcomes below before you act, then re-run "
        f"plan afterward to see what actually happened rather than trusting this message."
    )
    choice_quarantine = (  # Q4-SF1: choice 1, its own label, its own self-contained consequence
        f"CHOICE 1: move the QUARANTINE file aside yourself, by hand, outside this tool. "
        f"Consequence: a later execute WILL then move the raw file into the vacated slot and reset "
        f"its manifest row, which is the only choice that lets this tool ever finish this pair, but "
        f"nothing will record that the file then at the quarantine path is a substitute, not the "
        f"original evidence, so copy the original somewhere safe under your own name first if that "
        f"distinction matters to you."
    )
    choice_raw = (  # Q4-SF1: choice 2, its own label, its own opposite, self-contained consequence
        f"CHOICE 2: move the RAW file aside instead. "
        f"Consequence: this pair has nothing left to move: it drops out of every future plan and "
        f"execute report, silently and permanently, with no move, no reset and no further mention of "
        f"{ticker} {interval}, unless a new file appears at the raw path."
    )
    # Q4-SF1's own fix, in one line: three blocks, separated by a blank line each, never one unbroken
    # paragraph. ONE authored assembly, here, matching the "single authored description" reasoning in
    # the docstring above: _print_report and restore_one() both consume this finished string, neither
    # one ever reassembles the parts itself.
    message = f"{preamble}\n\n{choice_quarantine}\n\n{choice_raw}"
    return {"ticker": ticker, "interval": interval, "raw_path": raw_abs,
           "quarantine_path": quarantine_abs, "message": message}


def plan(csv_path: str, staging_root: str, conn) -> dict:
    """READ-ONLY: never writes, never refuses. `conn` is accepted for signature symmetry with
    execute() (which genuinely needs it to read/write manifest rows); plan() itself answers purely
    from the CSV and the filesystem, so a caller can preview a run against a manifest that has not
    even been opened yet for anything beyond the connection itself.

    Returns {"final", "would_move", "no_daily_file", "price_test_failed", "price_test_cleared",
    "reverify_801_reconfirmed", "reverify_801_disagreements", "reverify_801_undecidable",
    "reverify_801_no_file", "null_ohlc_rows_dropped", "null_ohlc_checked_ticker_count",
    "quarantine_conflict_count", "quarantine_conflict_pairs", "quarantine_conflict_details"}. "final" =
    the 801 set (unconditional, #317 ruling 4) UNION the footer-screened names that FAIL the price
    test on at least one grain that flagged them; "would_move" only ever names a (ticker, interval)
    pair whose file genuinely exists on disk (AC-2's "only what exists moves"). reverify_801()'s own
    four sets ride along, read-only, so a caller (and the CLI) can see STOP IF 3's condition before
    execute() ever runs. null_ohlc_rows_dropped (A2/QB1, extended to the daily side at Q2-B1) is this
    call's own NULL-OHLC-row drop total; null_ohlc_checked_ticker_count is the number of distinct
    tickers a price test actually ran against this call (every footer_only ticker plus every condemned
    ticker with a present grain), so a reader can tell "zero rows dropped, genuinely nothing to drop"
    apart from "zero rows dropped because nothing was checked" without cross-referencing several other
    fields by hand (this fold's own companion fix, mirroring SPAN_AUDIT_COUNTERS's audits_run idiom,
    A5/QS6). quarantine_conflict_count/_pairs (A1/QB2) previews every destination collision BEFORE any
    move, so an operator sees it on `plan` alone, not only after execute() has already skipped past it.
    quarantine_conflict_details (Q2-SF1) carries the SAME pairs expanded with both absolute paths and
    one message naming what to decide and what to do, so the standoff a skip leaves behind is
    something an operator can act on before ever running execute(), not only something they are told
    about after.
    """
    del conn  # intentionally unused: see the docstring above; kept for parity with execute()'s signature
    condemned = load_condemned_csv(csv_path)  # the 801-style set, unconditional

    footer_by_grain = {iv: footer_screen_names(staging_root, iv) for iv in INTRADAY_GRAINS}  # per-grain screen
    footer_union = set().union(*footer_by_grain.values())  # every name flagged on at least one grain
    # #317 ruling 4's own framing is "the 801 tickers... [and] the roughly 1,900 FURTHER names the
    # footer screen flagged": the footer-gated test applies only to names NOT already 801-listed. A
    # name can legitimately be both (a large own-daily-vs-intraday gap is common for a genuinely
    # condemned ticker too), and testing it a second time through the simple boolean price-test gate
    # would be a category error: its real test is reverify_801's three-valued one, and its "final"
    # membership is already unconditional via `condemned` regardless of this boolean's outcome.
    footer_only = footer_union - condemned  # the second, price-test-gated set, disjoint from the 801 list by definition

    null_drop_counter = [0]  # A2/QB1: a fresh per-call accumulator, threaded through every aggregation below
    price_test_failed = set()  # footer-only names whose price test failed on a flagged grain: condemned
    price_test_cleared = set()  # footer-only names whose price test passed on every flagged grain: screened out
    for ticker in footer_only:  # only the grain(s) that actually flagged this ticker are tested (AC-3's own words)
        flagged_grains = [iv for iv in INTRADAY_GRAINS if ticker in footer_by_grain[iv]]  # which grain(s) flagged it
        if any(not price_test_passes(staging_root, ticker, iv, null_drop_counter=null_drop_counter)
              for iv in flagged_grains):  # fails on at least one
            price_test_failed.add(ticker)
        else:  # passes on every flagged grain
            price_test_cleared.add(ticker)

    final = condemned | price_test_failed  # the 801 set (unconditional) union the footer-screen survivors

    would_move: dict = {}  # (ticker, interval) -> the raw-relative path, only for files that genuinely exist
    for ticker in final:  # every condemned name, whichever path condemned it
        for interval in INTRADAY_GRAINS:  # only the two grains this unit ever moves
            rel = storage.relative_path(interval, ticker=ticker)  # the file's current raw/ location
            if os.path.exists(os.path.join(staging_root, rel)):  # AC-2: only what exists moves
                would_move[(ticker, interval)] = rel

    no_daily_file = {t for t in final if daily_file_missing(staging_root, t)}  # AC-4a's permanent-hole population

    rv = reverify_801(csv_path, staging_root, null_drop_counter=null_drop_counter)  # STOP IF 3's four-way split
    # This fold's own companion fix: every footer_only ticker lands in price_test_failed XOR
    # price_test_cleared, and every condemned ticker with a present grain lands in reverify_801's
    # reconfirmed/disagreements/undecidable (no_file_to_test tickers never reach a price test at all),
    # so this sum is the exact count of tickers a price test actually ran against this call: a direct,
    # load-bearing companion to null_ohlc_rows_dropped, not a count a reader has to reconstruct by
    # hand from five other fields.
    null_ohlc_checked_ticker_count = (len(price_test_failed) + len(price_test_cleared)
                                      + len(rv["reconfirmed"]) + len(rv["disagreements"]) + len(rv["undecidable"]))

    quarantine_conflicts = _quarantine_conflicts(would_move, staging_root)  # A1/QB2: preview, before any move
    quarantine_conflict_details = [  # Q2-SF1: the SAME pairs, expanded with both paths + the decide/act message
        _quarantine_conflict_detail(
            ticker, interval,
            os.path.join(staging_root, would_move[(ticker, interval)]),  # the raw file that could not move
            os.path.join(staging_root, quarantine_rel_path(interval, ticker)),  # what already occupies it
        )
        for ticker, interval in quarantine_conflicts  # reuses the SAME collision list; no filesystem re-check
    ]

    return {
        "final": final,  # every condemned ticker, by name
        "would_move": would_move,  # (ticker, interval) -> rel path, filtered to what exists on disk
        "no_daily_file": no_daily_file,  # final tickers with no daily evidence at all (AC-4a)
        "price_test_failed": price_test_failed,  # footer-screened names the price test condemned
        "price_test_cleared": price_test_cleared,  # footer-screened names the price test cleared
        "reverify_801_reconfirmed": rv["reconfirmed"],  # 801 names the narrow re-test reconfirmed (CONDEMNS)
        "reverify_801_disagreements": rv["disagreements"],  # 801 names the narrow re-test genuinely clears
        "reverify_801_undecidable": rv["undecidable"],  # 801 names the narrow re-test could not answer
        "reverify_801_no_file": rv["no_file_to_test"],  # A3/QS4: 801 names with no present grain to test at all
        "null_ohlc_rows_dropped": null_drop_counter[0],  # A2/QB1: this call's own NULL-OHLC-row drop total
        "null_ohlc_checked_ticker_count": null_ohlc_checked_ticker_count,  # distinguishes "ran, clean" from "never ran"
        "quarantine_conflict_count": len(quarantine_conflicts),  # A1/QB2: destinations already occupied
        "quarantine_conflict_pairs": quarantine_conflicts,  # named, never swallowed into the bare count
        "quarantine_conflict_details": quarantine_conflict_details,  # Q2-SF1: both paths + the decide/act message
    }


def execute(csv_path: str, staging_root: str, conn, acknowledge_801_disagreements: bool = False) -> dict:
    """Move the condemned files, reset their manifest units, never delete anything.

    Order, and it matters: (1) filesystem colocation FIRST, before any other check or write (STOP
    IF 2); (2) STOP IF 3: refuse the WHOLE run if reverify_801()'s disagreements (CLEARS) set is
    non-empty and unacknowledged; (3) move every (ticker, interval) in would_move EXCEPT a
    disagreeing ticker's own pairs (still excluded even once acknowledged) AND EXCEPT a pair whose
    quarantine-side destination already holds a file (A1/QB2): that ONE pair is skipped, named and
    counted, leaving both the existing quarantined copy and the newly re-fetched raw file untouched,
    rather than letting a bare os.replace silently clobber the original evidentiary file; (4) reset a
    moved unit to 'pending' UNLESS the ticker has no daily file (AC-4a), in which case it stays
    exactly as it was, quarantined but never resurrected for a bare re-download, OR UNLESS the moved
    pair had no fetch_unit row to flip at all (A4/QS5), in which case the gap is named rather than
    left to show up only as moved_5min/moved_1h silently outrunning units_reset. Both reset-skip
    reasons are named as exact (ticker, interval) pairs: reset_skipped_no_daily_file (this fold's own
    fix, AC-4a's own reason) alongside A4/QS5's reset_skipped_no_manifest_row, so moved_5min plus
    moved_1h always equals units_reset plus both skip counts. Before this fold a moved pair could
    vanish from every bucket a reader was shown whenever AC-4a's own skip was the reason: it left
    moved_5min/moved_1h and reset_skipped_no_manifest_row_count as the only two printed pieces of that
    arithmetic, and neither one ever explained the gap AC-4a caused.

    A quarantine-conflict skip (step 3 above) also gains quarantine_conflict_details (Q2-SF1, found
    independently by all three round-2 gates): the SAME skipped pairs, each carrying both absolute
    paths and one message naming what an operator must decide and do. The skip itself never changes;
    what changes is that the standoff it leaves, which restore_one() also refuses from the other
    direction and which nothing else in this module can clear, is now something an operator can act on
    rather than only be told about. No flag or code path here deletes, moves or renames either file on
    this tool's own behalf: the fix is entirely in what the operator is told.

    Q3-SF1 found that WHAT the operator was told there was itself wrong: the message promised one
    outcome, "re-run execute and this pair will then complete", false for one of the two choices it
    described. _quarantine_conflict_detail's own docstring carries the fix and the root cause; this
    branch's own code is unchanged by that fold, since the message it prints is built entirely inside
    that function.
    """
    quarantine_dir = quarantine_root(staging_root)  # the destination root, a sibling of raw/
    if not same_filesystem(staging_root, quarantine_dir):  # checked before any write at all (STOP IF 2)
        raise CrossFilesystemError(
            f"quarantine root {quarantine_dir!r} is not on the same filesystem as staging root "
            f"{staging_root!r}: a move here would silently become a copy plus a delete, which #317 "
            f"authorised a move, not a delete. Point EODHD_STAGING_ROOT (or --staging-root) at a "
            f"root whose own device also holds a writable quarantine/ subdirectory."
        )

    plan_result = plan(csv_path, staging_root, conn)  # the read-only preview: final/would_move/etc.
    disagreements = plan_result["reverify_801_disagreements"]  # the CLEARS-only set, never the undecidable one
    if disagreements and not acknowledge_801_disagreements:  # STOP IF 3: refuse the whole run, move nothing
        raise Reverify801DisagreementError(
            f"the narrow price test disagrees with the 801 list for {sorted(disagreements)}: a real, "
            f"performed comparison came back negative for a name #263's own list condemns. #317 "
            f"authorised a move on the 801's own evidence, not on evidence that disagrees with it. "
            f"Re-run execute(..., acknowledge_801_disagreements=True) to proceed; the disagreeing "
            f"name(s) will still be excluded from the move."
        )

    moved: dict = {}  # (ticker, interval) -> (from_rel, to_rel), only the pairs actually moved this run
    moved_intervals_by_ticker: dict = {}  # ticker -> set of intervals moved, for the per-ticker reset pass below
    quarantine_conflict_pairs = []  # A1/QB2: pairs SKIPPED because the destination already held a file
    quarantine_conflict_details = []  # Q2-SF1: the SAME pairs, each with both absolute paths + the decide/act message
    for (ticker, interval), rel_from in plan_result["would_move"].items():  # every pair genuinely on disk
        if ticker in disagreements:  # STOP IF 3's own words: "do not move that name", even once acknowledged
            continue
        rel_to = quarantine_rel_path(interval, ticker)  # the quarantine-side destination
        abs_from = os.path.join(staging_root, rel_from)  # the file's current location
        abs_to = os.path.join(staging_root, rel_to)  # where it lands
        if os.path.exists(abs_to):  # A1/QB2: something already sits here; os.replace would clobber it silently
            quarantine_conflict_pairs.append((ticker, interval))  # skip THIS pair only, name it, keep going
            quarantine_conflict_details.append(  # Q2-SF1: the operator-actionable form of the same skip
                _quarantine_conflict_detail(ticker, interval, abs_from, abs_to))
            continue  # both abs_from and abs_to are left exactly as they were, nothing touched
        try:
            os.makedirs(os.path.dirname(abs_to), exist_ok=True)  # ensure quarantine/<interval>/ exists first
            # A single os.rename/os.replace is an atomic filesystem metadata update on ONE device (DDIA ch 8,
            # "Atomicity ... if a fault aborts a transaction partway, every write so far is discarded"):
            # a crash at any instant leaves the file wholly at abs_from OR wholly at abs_to, never both, never
            # neither, and never a partial copy. same_filesystem() above is what makes this legal to rely on;
            # research_lab/fetcher/storage.py's own write_atomic leans on the identical guarantee for its commit.
            os.replace(abs_from, abs_to)  # the move itself, atomic because staging_root and quarantine_dir share a device
        except OSError as exc:  # permission, disk, or a race: neither this makedirs nor this replace is
                                 # guaranteed once STOP IF 2/A1 have both already passed, so this is an
                                 # OS-level failure, not a decision this module makes (Q5-SF1's own class)
            raise QuarantineFilesystemError(
                f"{ticker} {interval}: cannot move {abs_from!r} to {abs_to!r}: {exc!r}") from exc
        moved[(ticker, interval)] = (rel_from, rel_to)  # record what actually happened
        moved_intervals_by_ticker.setdefault(ticker, set()).add(interval)  # group by ticker for the reset pass
    quarantine_conflict_pairs.sort()  # deterministic order, matching every other pairs list in this module
    quarantine_conflict_details.sort(key=lambda d: (d["ticker"], d["interval"]))  # Q2-SF1: same deterministic order

    units_reset = set()  # (ticker, interval) pairs whose manifest row was actually flipped to 'pending'
    reset_skipped_no_manifest_row = []  # A4/QS5: moved pairs whose reset found no fetch_unit row to touch
    reset_skipped_no_daily_file = []  # this fold: moved pairs whose reset never even ran, AC-4a's own reason
    for ticker, intervals in moved_intervals_by_ticker.items():  # one daily-file check per ticker, not per interval
        if daily_file_missing(staging_root, ticker):  # AC-4a: no daily evidence -> the bottom trim cannot fire
            for interval in intervals:  # every grain this ticker moved this run, named rather than left to vanish
                reset_skipped_no_daily_file.append((ticker, interval))  # moved, but AC-4a is why it stays quarantined
            continue  # the file stays quarantined; the manifest row is left exactly as it was
        for interval in intervals:  # every grain actually moved for this ticker
            cursor = conn.execute(  # 5min/1h units always carry window_start=window_end=0 (backfill.py's own enqueue)
                "UPDATE fetch_unit SET status='pending', error=NULL WHERE ticker=? AND interval=? "
                "AND window_start=0 AND window_end=0",
                (ticker, interval),
            )
            if cursor.rowcount > 0:  # only count a reset that genuinely touched a manifest row
                units_reset.add((ticker, interval))
            else:  # A4/QS5: the UPDATE touched nothing, the only realistic cause being no fetch_unit row exists
                reset_skipped_no_manifest_row.append((ticker, interval))  # named, not left as a silent gap
    reset_skipped_no_manifest_row.sort()  # deterministic order, matching every other pairs list in this module
    reset_skipped_no_daily_file.sort()  # deterministic order, matching every other pairs list in this module
    conn.commit()  # persist every reset from this run in one transaction

    counts = {  # every number this run touched, named, never swallowed into a boolean (STOP IF 3's own lesson)
        "moved_5min": sum(1 for (_t, iv) in moved if iv == "5min"),
        "moved_1h": sum(1 for (_t, iv) in moved if iv == "1h"),
        "units_reset": len(units_reset),
        "price_test_cleared": len(plan_result["price_test_cleared"]),
        "price_test_failed": len(plan_result["price_test_failed"]),
        "no_daily_file_count": len(plan_result["no_daily_file"]),
        "no_daily_file_tickers": sorted(plan_result["no_daily_file"]),
        "reverify_801_reconfirmed_count": len(plan_result["reverify_801_reconfirmed"]),  # this fold
        "reverify_801_reconfirmed_tickers": sorted(plan_result["reverify_801_reconfirmed"]),  # this fold, named
        "reverify_801_disagreement_count": len(disagreements),
        "reverify_801_disagreement_tickers": sorted(disagreements),
        "reverify_801_undecidable_count": len(plan_result["reverify_801_undecidable"]),
        "reverify_801_undecidable_tickers": sorted(plan_result["reverify_801_undecidable"]),
        "reverify_801_no_file_count": len(plan_result["reverify_801_no_file"]),  # A3/QS4
        "reverify_801_no_file_tickers": sorted(plan_result["reverify_801_no_file"]),  # A3/QS4, named
        "null_ohlc_rows_dropped": plan_result["null_ohlc_rows_dropped"],  # A2/QB1, mirrored from plan_result
        "null_ohlc_checked_ticker_count": plan_result["null_ohlc_checked_ticker_count"],  # this fold, mirrored
        "quarantine_conflict_count": len(quarantine_conflict_pairs),  # A1/QB2, this run's ACTUAL move-time count
        "quarantine_conflict_pairs": quarantine_conflict_pairs,  # A1/QB2, named
        "quarantine_conflict_details": quarantine_conflict_details,  # Q2-SF1, both paths + the decide/act message
        "reset_skipped_no_manifest_row_count": len(reset_skipped_no_manifest_row),  # A4/QS5
        "reset_skipped_no_manifest_row_pairs": reset_skipped_no_manifest_row,  # A4/QS5, named
        "reset_skipped_no_daily_file_count": len(reset_skipped_no_daily_file),  # this fold: AC-4a's own pairs, named
        "reset_skipped_no_daily_file_pairs": reset_skipped_no_daily_file,  # this fold, named
    }

    return {**plan_result, "moved": moved, "units_reset": units_reset, "counts": counts}  # plan()'s keys plus these


def restore_one(staging_root: str, interval: str, ticker: str):
    """The inverse of one move: quarantine/<interval>/<ticker>.parquet back to its raw/ path.

    Returns the restored absolute path, or None if nothing was quarantined for this (ticker,
    interval) at all (a clean no-op). Raises FileExistsError rather than overwriting a file already
    sitting at the destination (the "re-fetched since quarantine" case): a silent overwrite there
    would destroy a possibly-genuine re-fetched file, exactly the data loss this whole unit exists
    to avoid (what-can-break item 1: "a move with no inverse is a delete").

    Q3-SF1's own root cause: this refusal is the IDENTICAL raw-and-quarantine-both-occupied collision
    execute()'s move loop already names via _quarantine_conflict_detail, seen from the opposite
    direction. Before this fold the two directions carried two independently authored sentences that
    had already drifted apart once (execute()'s own message needed correcting; this one did not know
    it); this one now BUILDS its message from that SAME function, so there is exactly one authored
    description of this collision left in the module, not two that a later fold can update only one of.

    Also raises QuarantineFilesystemError if the restore directory cannot be created or the file
    cannot be moved back for an OS-level reason (permission, disk, or a vanished source): the SAME
    rule QuarantineReadError already applies to a content read, applied here to the move itself
    (Q5-SF1).
    """
    q_abs = os.path.join(staging_root, quarantine_rel_path(interval, ticker))  # the quarantined copy's path
    if not os.path.exists(q_abs):  # nothing quarantined for this (ticker, interval)
        return None  # a clean no-op, not an error
    raw_abs = os.path.join(staging_root, storage.relative_path(interval, ticker=ticker))  # its original home
    if os.path.exists(raw_abs):  # something already sits at the destination: the SAME collision
                                  # execute()'s move loop names from the other direction (Q3-SF1)
        raise FileExistsError(_quarantine_conflict_detail(ticker, interval, raw_abs, q_abs)["message"])
    try:
        os.makedirs(os.path.dirname(raw_abs), exist_ok=True)  # ensure raw/<interval>/ still exists
        os.replace(q_abs, raw_abs)  # the same atomic same-filesystem rename the forward move used
    except OSError as exc:  # permission, disk, or q_abs vanished between the exists check and this move
        raise QuarantineFilesystemError(
            f"{ticker} {interval}: cannot restore {q_abs!r} to {raw_abs!r}: {exc!r}") from exc
    return raw_abs  # the restored path


def restore_all(staging_root: str) -> dict:
    """Restore every file currently sitting in quarantine, across both grains. Returns
    {"restored": [(ticker, interval), ...], "refused": [(ticker, interval), ...]}, sorted for a
    deterministic report; a refusal never deletes the quarantined copy, it is simply left in place.

    Raises QuarantineFilesystemError if a quarantine interval directory exists (the os.path.isdir
    check just below passed) but cannot be listed (permission, or a race where it is removed between
    that check and this call), or if restore_one() itself raises it for one file (Q5-SF1): main()'s
    restore branch is the ONE code path in this module that ran with NO exception handling at all
    before this fix, so either failure reached an operator as a raw traceback.
    """
    restored = []  # (ticker, interval) pairs successfully restored
    refused = []  # (ticker, interval) pairs refused (a destination file already exists)
    q_root = quarantine_root(staging_root)  # the quarantine tree's own root
    for interval in INTRADAY_GRAINS:  # walk both grains this unit ever quarantines
        interval_dir = os.path.join(q_root, interval)  # quarantine/<interval>/
        if not os.path.isdir(interval_dir):  # nothing was ever quarantined at this grain
            continue
        try:
            entries = sorted(os.listdir(interval_dir))  # deterministic order
        except OSError as exc:  # permission denied, or interval_dir vanished after the isdir check above
            raise QuarantineFilesystemError(f"cannot list {interval_dir!r}: {exc!r}") from exc
        for fname in entries:
            if not fname.endswith(".parquet"):  # ignore any stray non-parquet entry
                continue
            ticker = fname[: -len(".parquet")]  # the bare ticker this quarantined file belongs to
            try:
                result = restore_one(staging_root, interval, ticker)  # attempt the inverse
            except FileExistsError:  # the destination is already occupied
                refused.append((ticker, interval))
                continue
            if result is not None:  # a genuine restore happened (always true here: the file was just listed)
                restored.append((ticker, interval))
    return {"restored": restored, "refused": refused}


def span_audit_violations(staging_root: str, conn) -> list:
    """AC-6's standing check: every 'done' 1h/5min/1min unit whose recorded bars lie outside the
    ticker's own trading life, computed over the REAL narrowing functions
    (tradinglife.trading_life), never a reimplementation of the guard's own predicate. Meant to
    outlive this unit's own one-off clean-up: a future fetch that writes a span outside a ticker's
    trading life is caught here rather than discovered a year later.

    Returns a list of {"ticker", "interval", "reason"} dicts; increments SPAN_AUDIT_COUNTERS as it
    finds each one, in-memory only. Also increments SPAN_AUDIT_COUNTERS["audits_run"] UNCONDITIONALLY,
    once per call, whether or not any violation is found (A5/QS6): the only way a reader can tell
    "never audited" apart from "audited repeatedly, genuinely clean".
    """
    SPAN_AUDIT_COUNTERS["audits_run"] += 1  # A5/QS6: exactly once per call, before any violation logic runs
    violations = []  # every out-of-life unit found this run
    life_cache: dict = {}  # one daily read per ticker, shared across its 1h/5min/1min 'done' rows
    for interval in ("1h", "5min", "1min"):  # every grain this audit covers (never daily itself)
        rows = conn.execute(  # every unit this grain has ever landed successfully
            "SELECT ticker, window_start, window_end, path FROM fetch_unit WHERE interval=? AND status='done'",
            (interval,),
        ).fetchall()
        for ticker, window_start, window_end, path in rows:  # one 'done' unit at a time
            if ticker not in life_cache:  # read each ticker's daily-derived trading life at most once
                life_cache[ticker] = tradinglife.trading_life(staging_root, conn, ticker)
            life = life_cache[ticker]  # the cached (first, last, last_is_final) tuple, or None
            if life is None:  # no daily evidence at all: nothing to audit this span against
                continue
            first, last, last_is_final = life  # the ticker's own recorded trading life
            if not path:  # a 'done' row with no recorded path is a different problem, not this audit's
                continue
            abs_path = os.path.join(staging_root, path)  # the unit's actual file
            bounds = _footer_bounds(abs_path, "timestamp")  # (min, max) epoch, footer-only
            if bounds is None:  # unreadable, empty, or missing entirely: cannot audit this one
                continue
            min_date = tradinglife._utc_date(bounds[0])  # the file's earliest recorded bar date
            max_date = tradinglife._utc_date(bounds[1])  # the file's latest recorded bar date
            reason = None  # set below only if a rule actually fires
            if min_date < first:  # the bottom rule's own predicate: a bar before the ticker's first trade
                reason = (f"{ticker} {interval} [{window_start}..{window_end}]: earliest bar {min_date} "
                         f"is before the trading life start {first}")
            elif last_is_final and max_date > last:  # the top rule, only meaningful once the life is final
                reason = (f"{ticker} {interval} [{window_start}..{window_end}]: latest bar {max_date} "
                         f"is after the trading life end {last} (final={last_is_final})")
            if reason is not None:  # a genuine violation
                violations.append({"ticker": ticker, "interval": interval, "reason": reason})
                SPAN_AUDIT_COUNTERS["violations_found"] += 1  # per-process, cumulative, never reset (sizing.py's own style)
    return violations


def _build_arg_parser() -> argparse.ArgumentParser:
    """The plan/execute/restore CLI, --staging-root/--manifest/--csv common to plan and execute,
    execute's own --acknowledge-801-disagreements override (STOP IF 3), absent by default. `restore`
    is this builder's addition beyond the probe manifest's own minimum (plan/execute only, per
    AC-7's contract): the dispatch's own report format asks for a runnable inverse command, and a
    third subcommand on the SAME entry point is the one an operator can actually run, rather than a
    one-off Python snippet quoted in a report. Purely additive: no probe inspects the subcommand
    list, so this cannot narrow or change what q01 through q19 already prove about plan/execute."""
    parser = argparse.ArgumentParser(  # the top-level `python -m research_lab.fetcher.ops.quarantine_242` parser
        description="Quarantine #242's condemned intraday files (5min/1h) to a reversible sibling directory.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)  # plan, execute or restore, exactly one

    def _add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--staging-root", default=None,  # falls back to EODHD_STAGING_ROOT / the config default
                         help="defaults to EODHD_STAGING_ROOT / config.staging_root()")
        sub.add_argument("--manifest", default=None,  # falls back to <staging-root>/manifest.db
                         help="defaults to <staging-root>/manifest.db")
        sub.add_argument("--csv", default=DEFAULT_CONDEMNED_CSV,  # the 801-style condemned ticker list
                         help=f"the 801-style condemned ticker CSV (default {DEFAULT_CONDEMNED_CSV})")

    plan_parser = subparsers.add_parser("plan", help="read-only preview; writes nothing at all")
    _add_common(plan_parser)  # plan shares every common flag

    execute_parser = subparsers.add_parser("execute", help="move the condemned files and reset their manifest units")
    _add_common(execute_parser)  # execute shares every common flag too
    execute_parser.add_argument(  # STOP IF 3's own override, absent by default
        "--acknowledge-801-disagreements", action="store_true",
        help="proceed even though the narrow price test disagrees with one or more 801-listed names; "
             "the disagreeing name(s) still never move",
    )

    restore_parser = subparsers.add_parser(  # the inverse: no --manifest/--csv, restore_all needs neither
        "restore", help="restore every file currently sitting in quarantine back to raw/ (the inverse of execute)")
    restore_parser.add_argument("--staging-root", default=None,
                                help="defaults to EODHD_STAGING_ROOT / config.staging_root()")
    return parser


def _print_report(subcommand: str, result: dict, staging_root: str, manifest_path: str,
                  csv_path: str, acknowledge_flag: bool) -> None:
    """Print the labelled counts + a literal reproduce command, in narrowing_dry_run.py's own
    plain-text style. Called ONLY from main(): every importable function above stays silent, since
    probes call them directly and diff their captured stdout byte for byte against a committed
    .expected file.
    """
    if subcommand == "plan":  # plan() never moves or resets anything; report what it WOULD do
        moved_5min = sum(1 for (_t, iv) in result["would_move"] if iv == "5min")  # would-move count, 5min
        moved_1h = sum(1 for (_t, iv) in result["would_move"] if iv == "1h")  # would-move count, 1h
        units_reset_line = "units_reset=N/A (plan is read-only; nothing is moved or reset)"  # nothing to report yet
        reset_skip_line = ("reset_skipped_no_manifest_row=N/A "
                           "(plan is read-only; nothing is moved or reset)")  # A4/QS5, plan's own N/A twin
        price_test_failed = len(result["price_test_failed"])  # footer-screened names that would move
        price_test_cleared = len(result["price_test_cleared"])  # footer-screened names that would be spared
        no_daily_file_count = len(result["no_daily_file"])  # AC-4a population size, whether or not it moves yet
        no_daily_file_tickers = sorted(result["no_daily_file"])  # the named list, never a bare count
        rv_dis_count = len(result["reverify_801_disagreements"])  # STOP IF 3's own condition, previewed
        rv_dis_tickers = sorted(result["reverify_801_disagreements"])  # named, never swallowed
        rv_undecidable_count = len(result["reverify_801_undecidable"])  # the undecidable count, previewed
        rv_undecidable_tickers = sorted(result["reverify_801_undecidable"])  # named, never swallowed
        rv_no_file_count = len(result["reverify_801_no_file"])  # A3/QS4: previewed, same style as the other two
        rv_no_file_tickers = sorted(result["reverify_801_no_file"])  # named, never swallowed
        rv_reconfirmed_count = len(result["reverify_801_reconfirmed"])  # this fold: previewed, same style
        rv_reconfirmed_tickers = sorted(result["reverify_801_reconfirmed"])  # named, never swallowed
        null_dropped = result["null_ohlc_rows_dropped"]  # A2/QB1: this plan() call's own NULL-drop total
        null_checked = result["null_ohlc_checked_ticker_count"]  # this fold: "ran, clean" apart from "never ran"
        conflict_count = result["quarantine_conflict_count"]  # A1/QB2: previewed, before any move at all
        conflict_pairs = result["quarantine_conflict_pairs"]  # named, never swallowed
        conflict_details = result["quarantine_conflict_details"]  # Q2-SF1: same pairs, both paths + message
        no_daily_skip_line = ("reset_skipped_no_daily_file=N/A "
                              "(plan is read-only; nothing is moved or reset)")  # this fold, plan's own N/A twin
    else:  # execute already computed its own "counts" sub-dict from what genuinely happened
        counts = result["counts"]  # execute()'s own summary
        moved_5min = counts["moved_5min"]  # files actually moved, 5min
        moved_1h = counts["moved_1h"]  # files actually moved, 1h
        units_reset_line = f"units_reset={counts['units_reset']}"  # manifest rows actually reset to pending
        reset_skip_line = (f"reset_skipped_no_manifest_row={counts['reset_skipped_no_manifest_row_count']} "
                           f"pairs={counts['reset_skipped_no_manifest_row_pairs']}")  # A4/QS5, named
        price_test_failed = counts["price_test_failed"]  # footer-screened names condemned
        price_test_cleared = counts["price_test_cleared"]  # footer-screened names spared
        no_daily_file_count = counts["no_daily_file_count"]  # AC-4a population size
        no_daily_file_tickers = counts["no_daily_file_tickers"]  # the named list
        rv_dis_count = counts["reverify_801_disagreement_count"]  # STOP IF 3's own condition, as it actually ran
        rv_dis_tickers = counts["reverify_801_disagreement_tickers"]  # named
        rv_undecidable_count = counts["reverify_801_undecidable_count"]  # the undecidable count
        rv_undecidable_tickers = counts["reverify_801_undecidable_tickers"]  # named
        rv_no_file_count = counts["reverify_801_no_file_count"]  # A3/QS4, as it actually ran
        rv_no_file_tickers = counts["reverify_801_no_file_tickers"]  # named
        rv_reconfirmed_count = counts["reverify_801_reconfirmed_count"]  # this fold, as it actually ran
        rv_reconfirmed_tickers = counts["reverify_801_reconfirmed_tickers"]  # named
        null_dropped = counts["null_ohlc_rows_dropped"]  # A2/QB1, mirrored from the internal plan() call
        null_checked = counts["null_ohlc_checked_ticker_count"]  # this fold, mirrored from the internal plan() call
        conflict_count = counts["quarantine_conflict_count"]  # A1/QB2, this run's ACTUAL move-time count
        conflict_pairs = counts["quarantine_conflict_pairs"]  # named, never swallowed
        conflict_details = counts["quarantine_conflict_details"]  # Q2-SF1: same pairs, both paths + message
        no_daily_skip_line = (f"reset_skipped_no_daily_file={counts['reset_skipped_no_daily_file_count']} "
                              f"pairs={counts['reset_skipped_no_daily_file_pairs']}")  # this fold, named

    print(f"quarantine_242 {subcommand}: staging_root={staging_root} manifest={manifest_path} csv={csv_path}")
    try:
        free_bytes = shutil.disk_usage(staging_root).free  # what-can-break item 4: report free space with its command
        print(f"free space at {staging_root}: {free_bytes / (1024 ** 3):.1f} GiB "
             f"(a move is cheap only on the same filesystem, checked before any write; see df -h)")
    except OSError as exc:  # staging_root itself unreadable: say so rather than crash the report
        print(f"free space at {staging_root}: could not be read ({exc!r})")
    print(f"moved_5min={moved_5min}")  # the exact label AC-7/q19 requires, for both subcommands
    print(f"moved_1h={moved_1h}")  # ditto for the hourly grain
    print(units_reset_line)  # a real count for execute, an explicit N/A for the read-only preview
    print(reset_skip_line)  # A4/QS5: a real count+list for execute, an explicit N/A for the read-only preview
    print(no_daily_skip_line)  # this fold: AC-4a's OWN reset-skip reason, named the same way, an explicit N/A on plan
    print(f"price_test_failed={price_test_failed}")  # footer-screen names the price test condemned
    print(f"price_test_cleared={price_test_cleared}")  # footer-screen names the price test spared
    print(f"no_daily_file_count={no_daily_file_count} tickers={no_daily_file_tickers}")  # AC-4a's permanent hole
    print(f"reverify_801_reconfirmed={rv_reconfirmed_count} tickers={rv_reconfirmed_tickers}")  # this fold, named
    print(f"reverify_801_disagreements={rv_dis_count} tickers={rv_dis_tickers}")  # STOP IF 3's own count, named
    print(f"reverify_801_undecidable={rv_undecidable_count} tickers={rv_undecidable_tickers}")  # never swallowed
    print(f"reverify_801_no_file={rv_no_file_count} tickers={rv_no_file_tickers}")  # A3/QS4, never swallowed
    print(f"null_ohlc_rows_dropped={null_dropped}")  # A2/QB1: a declared failure mode now has a visible count
    print(f"null_ohlc_checked_ticker_count={null_checked}")  # this fold: "ran, clean" apart from "never ran"
    print(f"quarantine_conflict_count={conflict_count} pairs={conflict_pairs}")  # A1/QB2, never swallowed
    for detail in conflict_details:  # Q2-SF1, corrected at Q3-SF1 (content) and Q4-SF1 (form): every
                                      # collision names both real outcomes, laid out as a preamble plus
                                      # two clearly labelled, separately printed choices
        # Q4-SF1: detail["message"] now carries embedded newlines (a preamble, then two labelled
        # choices). Printing it in ONE print() call would still emit several physical lines to
        # stdout (a literal '\n' inside a string does that regardless of how many print() calls wrote
        # it), but every line AFTER the first would carry no "quarantine_conflict_detail: " prefix at
        # all: a bare "CHOICE 1: ..." line sitting among "moved_5min=" and "quarantine_conflict_count="
        # lines, indistinguishable by prefix from a different report field entirely. Splitting on '\n'
        # and printing each physical line through its own print() call keeps the SAME prefix on every
        # line of this collision, so the block stays greppable as one unit and can never be mistaken
        # for one of the OTHER lines this report prints around it (the dispatch's own standing check).
        for line in detail["message"].split("\n"):  # one physical line of THIS message at a time
            print(f"quarantine_conflict_detail: {line}")  # reachable on plan(), not only after execute()
    ack_suffix = " --acknowledge-801-disagreements" if acknowledge_flag else ""  # mirror the flag actually used
    print(f"reproduce: python -m research_lab.fetcher.ops.quarantine_242 {subcommand} "
         f"--staging-root {staging_root} --manifest {manifest_path} --csv {csv_path}{ack_suffix}")


def main() -> int:
    """The CLI entry point: `python -m research_lab.fetcher.ops.quarantine_242 <plan|execute|restore> ...`."""
    parser = _build_arg_parser()  # the plan/execute/restore subcommands and their flags
    args = parser.parse_args()  # parse sys.argv
    staging_root = args.staging_root or default_staging_root()  # the flag wins, else the configured default

    if args.subcommand == "restore":  # the inverse path: no manifest touched at all, files only
        try:
            result = restore_all(staging_root)  # walk quarantine/, move every file back, refuse a clobber
        except QuarantineFilesystemError as exc:  # Q5-SF1: this branch ran with NO exception handling
                                                   # at all before this fix (it returns before the
                                                   # try/except below is ever reached), so a listing or
                                                   # move failure here reached an operator as a raw
                                                   # traceback; this is the SAME named-exception rule
                                                   # QuarantineReadError already applies below, given
                                                   # its own local guard here for that reason
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 1
        print(f"quarantine_242 restore: staging_root={staging_root}")  # the same staging_root convention as plan/execute
        print(f"restored={len(result['restored'])} pairs={sorted(result['restored'])}")  # every (ticker, interval) restored
        print(f"refused={len(result['refused'])} pairs={sorted(result['refused'])}")  # every clobber refusal, named
        print(f"reproduce: python -m research_lab.fetcher.ops.quarantine_242 restore --staging-root {staging_root}")
        return 0  # a destination-collision refusal is reported via refused=, never an exception; an
                  # OS-level failure raises QuarantineFilesystemError, caught just above

    manifest_path = args.manifest or os.path.join(staging_root, "manifest.db")  # the flag wins, else the default
    conn = manifest.connect(manifest_path)  # a normal read-write connection; plan() itself never writes through it
    try:
        if args.subcommand == "plan":  # the read-only preview path
            result = plan(args.csv, staging_root, conn)  # never writes, never refuses
            _print_report("plan", result, staging_root, manifest_path, args.csv, False)  # plan has no ack flag
            return 0
        result = execute(  # the execute path: moves files, resets units, may refuse
            args.csv, staging_root, conn,
            acknowledge_801_disagreements=args.acknowledge_801_disagreements,
        )
        _print_report("execute", result, staging_root, manifest_path, args.csv, args.acknowledge_801_disagreements)
        return 0
    except CrossFilesystemError as exc:  # STOP IF 2: printed for an operator, never a raw traceback
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Reverify801DisagreementError as exc:  # STOP IF 3: printed for an operator, never a raw traceback
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (tradinglife.TradingLifeUnreadable, QuarantineReadError,
           QuarantineFilesystemError) as exc:  # Q3-SF2/Q4-SF2/Q5-SF1, root-caused TWICE: every DIRECT
                                                      # read failure this module can hit (a daily or
                                                      # intraday parquet, or the condemned-ticker CSV, via
                                                      # QuarantineReadError; the SAME daily file again via
                                                      # tradinglife.py's own daily_date_span, reached
                                                      # through daily_file_missing) funnels through the
                                                      # first two names; every DIRECT filesystem operation
                                                      # this module performs OTHER than a content read (a
                                                      # same-device check, a directory listing, a move)
                                                      # funnels through the third, QuarantineFilesystemError,
                                                      # added by the SECOND defect-root-cause dispatch on
                                                      # this class after the first one's own rule covered
                                                      # only content reads because only content reads had
                                                      # been changed to raise it. A FOURTH site of either
                                                      # kind, present or future, needs no new except clause
                                                      # here, only to raise one of these three names at its
                                                      # own call, matching the pattern QuarantineReadError's
                                                      # and QuarantineFilesystemError's own docstrings state.
                                                      # No fallback: a failure here is refused, never
                                                      # silently skipped, since skipping it would change
                                                      # which tickers get condemned, cleared or reset, or
                                                      # which files move. Each message already names the
                                                      # ticker/path/pair, so nothing is appended, matching
                                                      # the two simpler blocks above rather than the
                                                      # decorated sqlite3.Error one below.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:  # A7/QS9: any manifest database error (a lock held by a concurrent fetcher
                                  # run is the realistic, live case), refused with a named message rather
                                  # than an uncaught traceback; no data is lost here (files move before the
                                  # single deferred conn.commit() at the end of execute()'s reset loop), so
                                  # this is a debuggability fix, never a data-safety one
        print(f"REFUSED: the manifest database at {manifest_path!r} raised {exc!r}; a concurrent fetcher "
             f"run may be holding a lock on it, wait for that run to finish and retry", file=sys.stderr)
        return 1
    finally:
        conn.close()  # always release the manifest connection, success or refusal alike


if __name__ == "__main__":
    sys.exit(main())  # propagate main()'s 0/1 as the process exit code
