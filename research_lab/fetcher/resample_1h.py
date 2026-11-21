"""Hourly bars built from the 1-minute store (lane D2a, docs/lanes/D2a.md, 2026-10-10).

Reads one ticker's 1-minute files from the SSD store (raw/1min/<SYMBOL>.US/<year>/<window>.parquet) and
builds hourly bars on the native hourly grid: each bar starts on the hour from the session's open (09:30
New York time, or the XNYS session's own open), spans one hour or to the session's close on a half day,
and aggregates the regular-session minutes that fall in it (open first, high max, low min, close last,
volume sum). Pre-market and post-market minutes are dropped. A bar with no minutes is absent, never filled.

DDIA check (docs/books/kleppmann_canon.md, the derived data chapter): these files are derived data. Each
one is a pure function of the minute store, so it is rebuilt whole and never edited by hand. The write is
temp file, fsync, rename, fsync of the folder, so a reader never sees half a file.

The index is the bar's start as a naive New York local timestamp (named bar_start), so the data check
(research_lab/universe/data_check.py flag_rows) reads it as a DatetimeIndex. The columns are timestamp (the
same bar start as UTC epoch seconds, for the join in lane D2b), open, high, low, close, volume and minutes
(how many minutes the bar was built from).

CLI: python -m research_lab.fetcher.resample_1h {SYMBOL|all} --out DIR
SYMBOL is the bare symbol (AAPL); its folder is AAPL.US and its file is AAPL.US.parquet.
"""

import argparse
import glob
import os
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import config  # the staging root and the SSD mount path

NY = "America/New_York"  # the bars are stamped in New York local time
CALENDAR_NAME = "XNYS"  # the exchange_calendars library's New York Stock Exchange calendar
CALENDAR_START = "1900-01-01"  # earlier than any minute in the store
ONE_HOUR = pd.Timedelta(hours=1)
MINUTE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
DAILY_COLUMNS = ("date", "high", "low", "close", "adjusted_close", "volume")  # what the cross-check and the fold read
SUFFIX = ".US"
DAILY_TOLERANCE = 1e-4  # relative; a report threshold for the cross-check only, never a gate (lane D2a step 4b b)
CROSS_CHECK_LARGEST = 5  # how many of the largest mismatches the report lists

_SCHEDULE = None  # (session open, session close) in New York local time, built once per process


class ResampleError(ValueError):
    """A ticker's minutes cannot be turned into hourly bars. The message names the ticker and the cause."""


class OffSessionMinutesError(ResampleError):
    """A minute is stamped on a day the XNYS calendar shows as closed. Such a minute is another market's
    data or a stray row; it is refused by day, never dropped quietly."""


class NoMinuteDataError(ResampleError):
    """The ticker has no 1-minute files in the store."""


@dataclass(frozen=True)
class BuildStats:
    """What the official-close fold did, for the log. folded: sessions whose last bar took the daily close.
    no_daily_close: sessions whose last bar kept its minute close, because the daily file had no close for
    that date (or no daily file at all). daily_file_present says which of the two it was."""

    sessions: int
    folded: int
    no_daily_close: int
    daily_file_present: bool


@dataclass(frozen=True)
class CrossCheck:
    """The day-by-day comparison with the daily raw bar, summarised for the log. largest holds up to five
    (date, field, built, daily, relative difference) tuples, largest first. volume_ratio_median is None when no
    day could be compared."""

    days_compared: int
    days_over_tolerance: int
    days_without_daily: int
    largest: tuple
    volume_ratio_median: object


def _check_symbol(symbol):
    """The build takes the bare symbol. A name carrying the exchange suffix is refused, not stripped."""
    if not symbol or symbol.endswith(SUFFIX) or "/" in symbol:
        raise ValueError(f"resample_1h takes the bare symbol (AAPL, not AAPL.US or a path); got {symbol!r}")


def _schedule():
    """The XNYS session opens and closes, as New York local naive timestamps indexed by session date.
    Built once per process; the library call takes about a second."""
    global _SCHEDULE
    if _SCHEDULE is None:
        import exchange_calendars  # lazy: only a caller that builds bars pays the import

        sessions = exchange_calendars.get_calendar(CALENDAR_NAME, start=CALENDAR_START).schedule
        index = pd.DatetimeIndex(sessions.index).tz_localize(None).normalize()
        opens = pd.Series(sessions["open"].dt.tz_convert(NY).dt.tz_localize(None).to_numpy(), index=index)
        closes = pd.Series(sessions["close"].dt.tz_convert(NY).dt.tz_localize(None).to_numpy(), index=index)
        _SCHEDULE = (opens, closes)
    return _SCHEDULE


def _read_minutes(symbol, staging):
    """Every minute file of the symbol, as one frame with a New York local column ny."""
    folder = os.path.join(staging, "raw", "1min", f"{symbol}{SUFFIX}")
    files = sorted(glob.glob(os.path.join(folder, "*", "*.parquet")))
    if not files:
        raise NoMinuteDataError(f"{symbol}: no 1-minute files under {folder}")
    minutes = pd.concat([pd.read_parquet(path, columns=list(MINUTE_COLUMNS)) for path in files], ignore_index=True)
    minutes["ny"] = pd.to_datetime(minutes["timestamp"], unit="s", utc=True).dt.tz_convert(NY).dt.tz_localize(None)
    return minutes


def _minute_bars(symbol, staging):
    """Hourly bars from the minute store, before the official close is folded in. Raises ResampleError (or a
    subclass) by name: no minutes, a minute on a closed day, a missing price, or a minute stamped twice."""
    minutes = _read_minutes(symbol, staging)
    if minutes["ny"].duplicated().any():
        dupes = int(minutes["ny"].duplicated().sum())
        raise ResampleError(f"{symbol}: {dupes} minute(s) stamped twice in the store; refusing to double count")
    opens, closes = _schedule()
    day = minutes["ny"].dt.normalize()
    session_open = pd.Series(opens.reindex(day).to_numpy(), index=minutes.index)
    session_close = pd.Series(closes.reindex(day).to_numpy(), index=minutes.index)
    closed = session_open.isna()
    if closed.any():
        closed_days = sorted({d.strftime("%Y-%m-%d") for d in day[closed]})
        raise OffSessionMinutesError(
            f"{symbol}: {int(closed.sum())} minute(s) on {len(closed_days)} day(s) the {CALENDAR_NAME} calendar "
            f"shows as closed, first {closed_days[:5]}; refusing to build"
        )

    regular = minutes[(minutes["ny"] >= session_open) & (minutes["ny"] < session_close)].copy()
    missing = regular[list(PRICE_COLUMNS)].isna().any(axis=1)
    if missing.any():
        raise ResampleError(f"{symbol}: {int(missing.sum())} regular-session minute(s) with a missing price or volume; "
                            f"refusing to build")
    open_of = session_open[regular.index]
    hours_in = (regular["ny"] - open_of) // ONE_HOUR
    regular["bar_start"] = open_of + hours_in * ONE_HOUR
    regular = regular.sort_values("ny", kind="stable")
    grouped = regular.groupby("bar_start", sort=True)
    bars = pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
        "minutes": grouped.size(),
    })
    bars.index.name = "bar_start"
    utc_seconds = bars.index.tz_localize(NY).tz_convert("UTC").asi8 // 1_000_000_000
    bars.insert(0, "timestamp", utc_seconds.astype(np.int64))
    bars["volume"] = bars["volume"].astype(np.int64)
    bars["minutes"] = bars["minutes"].astype(np.int64)
    return bars


def _daily_frame(symbol, staging):
    """The daily raw file indexed by naive date, or None when the symbol has no daily file. A duplicate date is
    refused by name: it would make the official close ambiguous."""
    path = os.path.join(staging, "raw", "daily", f"{symbol}{SUFFIX}.parquet")
    if not os.path.exists(path):
        return None
    daily = pd.read_parquet(path, columns=list(DAILY_COLUMNS))
    days = pd.to_datetime(daily["date"])
    if days.dt.tz is not None:
        days = days.dt.tz_localize(None)
    daily["day"] = days.dt.normalize()
    if daily["day"].duplicated().any():
        raise ResampleError(f"{symbol}: the daily file holds {int(daily['day'].duplicated().sum())} duplicate date(s)")
    return daily.set_index("day")


def _fold_official_close(bars, symbol, staging):
    """Fold the official close into each session's last built bar (DECISIONS #293 point 3, lane D2a step 4b a).

    The last bar of each session takes that date's raw close from the daily file; open, high, low and volume
    stay as built. A session with no daily close keeps its last minute's close and is counted. Returns the
    bars and a BuildStats."""
    daily = _daily_frame(symbol, staging)
    day = bars.index.normalize()
    last_positions = np.flatnonzero(~day.duplicated(keep="last"))
    closes = np.full(len(last_positions), np.nan)
    if daily is not None:
        closes = daily["close"].reindex(day[last_positions]).to_numpy(dtype=float)
    has = ~np.isnan(closes)
    out = bars.copy()
    close_column = out.columns.get_loc("close")
    out.iloc[last_positions[has], close_column] = closes[has]
    stats = BuildStats(
        sessions=int(len(last_positions)),
        folded=int(has.sum()),
        no_daily_close=int((~has).sum()),
        daily_file_present=daily is not None,
    )
    return out, stats


def build_hourly_with_stats(symbol, staging_root=None):
    """The hourly bars of one symbol (official close folded in) and the BuildStats for the log."""
    _check_symbol(symbol)
    staging = staging_root if staging_root is not None else config.staging_root()
    return _fold_official_close(_minute_bars(symbol, staging), symbol, staging)


def build_hourly(symbol, staging_root=None):
    """Hourly bars for one symbol, from its 1-minute store. Returns the frame described in the module docstring.

    ``staging_root`` defaults to the configured SSD root (config.staging_root). Raises ResampleError (or a
    subclass) by name: no minutes, a minute on a closed day, a missing price, or a minute stamped twice.
    """
    frame, _ = build_hourly_with_stats(symbol, staging_root)
    return frame


def cross_check(frame, symbol, staging_root=None):
    """Compare each built day with the daily raw bar (lane D2a step 4b b and c). A report only: nothing is
    gated, filtered or edited.

    A day is over tolerance when its high max or low min differs from the daily raw high or low by more than
    DAILY_TOLERANCE, relative to the daily value. The largest mismatches are listed (up to CROSS_CHECK_LARGEST).
    The volume ratio per day is the daily volume times adjusted_close / close, over the built regular-hours
    volume; its median is reported, never flagged. A built day with no daily row is counted, not compared."""
    _check_symbol(symbol)
    staging = staging_root if staging_root is not None else config.staging_root()
    daily = _daily_frame(symbol, staging)
    day = frame.index.normalize()
    per_day = frame.groupby(day).agg(high=("high", "max"), low=("low", "min"), volume=("volume", "sum"))
    if daily is None:
        have = np.zeros(len(per_day), dtype=bool)
        d = pd.DataFrame(index=per_day.index, columns=list(DAILY_COLUMNS), dtype=float)
    else:
        have = per_day.index.isin(daily.index)
        d = daily.reindex(per_day.index)
    days_compared = int(have.sum())
    rel_high = ((per_day["high"] - d["high"]).abs() / d["high"]).where(have)
    rel_low = ((per_day["low"] - d["low"]).abs() / d["low"]).where(have)
    field = pd.Series(np.where(rel_high.fillna(-1.0) >= rel_low.fillna(-1.0), "high", "low"), index=per_day.index)
    worst_rel = pd.concat([rel_high, rel_low], axis=1).max(axis=1)
    over = worst_rel > DAILY_TOLERANCE
    listed = worst_rel[over].sort_values(ascending=False).head(CROSS_CHECK_LARGEST)
    largest = []
    for when, rel in listed.items():
        name = field[when]
        built = per_day.at[when, name]
        raw = d.at[when, name]
        largest.append((when.strftime("%Y-%m-%d"), name, float(built), float(raw), float(rel)))
    ratio = (d["volume"] * d["adjusted_close"] / d["close"] / per_day["volume"]).where(have & (per_day["volume"] > 0))
    ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
    return CrossCheck(
        days_compared=days_compared,
        days_over_tolerance=int(over.sum()),
        days_without_daily=int((~have).sum()),
        largest=tuple(largest),
        volume_ratio_median=float(ratio.median()) if len(ratio) else None,
    )


def write_hourly(frame, out_dir, symbol):
    """Write one symbol's bars to out_dir/<SYMBOL>.US.parquet atomically and return the path.

    Steps: write to a sibling temp file, fsync it, rename it onto the final name, fsync the folder. The temp
    file is removed when the rename did not happen. A folder under the SSD mount is refused while the SSD is
    not mounted, so the write never lands on the root disk.
    """
    _check_symbol(symbol)
    mount = config.mountpoint()
    if os.path.abspath(out_dir).startswith(mount.rstrip("/") + "/") and not os.path.ismount(mount):
        raise ResampleError(f"{mount} is not mounted; refusing to write hourly bars to {out_dir}")
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, f"{symbol}{SUFFIX}.parquet")
    tmp = f"{final}.tmp.{os.getpid()}"
    committed = False
    try:
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=True), tmp)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        committed = True
    finally:
        if not committed and os.path.exists(tmp):
            os.remove(tmp)
    _fsync_folder(out_dir)
    return final


def _fsync_folder(path):
    """fsync a folder so a renamed entry survives a power loss."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _all_symbols(staging):
    """Every bare symbol with a 1-minute folder, sorted."""
    root = os.path.join(staging, "raw", "1min")
    names = sorted(name for name in os.listdir(root) if name.endswith(SUFFIX) and os.path.isdir(os.path.join(root, name)))
    return [name[: -len(SUFFIX)] for name in names]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m research_lab.fetcher.resample_1h")
    parser.add_argument("target", help="a bare symbol such as AAPL, or 'all'")
    parser.add_argument("--out", required=True, help="the folder to write <SYMBOL>.US.parquet files into")
    args = parser.parse_args(argv)
    started = time.monotonic()
    staging = config.staging_root()
    symbols = _all_symbols(staging) if args.target == "all" else [args.target]
    from ..universe.data_check import flag_rows  # the same data check D1 runs on the daily files

    totals = {"built": 0, "bars": 0, "folded": 0, "no_daily_close": 0, "flagged": 0,
              "days_compared": 0, "days_over": 0, "days_without_daily": 0}
    failures = []
    for symbol in symbols:
        try:
            frame, stats = build_hourly_with_stats(symbol, staging)
        except ResampleError as exc:
            failures.append(str(exc))
            print(f"REFUSED {exc}")
            continue
        check = cross_check(frame, symbol, staging)
        flagged = len(flag_rows(frame))
        write_hourly(frame, args.out, symbol)
        totals["built"] += 1
        totals["bars"] += len(frame)
        totals["folded"] += stats.folded
        totals["no_daily_close"] += stats.no_daily_close
        totals["flagged"] += flagged
        totals["days_compared"] += check.days_compared
        totals["days_over"] += check.days_over_tolerance
        totals["days_without_daily"] += check.days_without_daily
        print(f"{symbol} bars {len(frame)} folded {stats.folded} no_daily_close {stats.no_daily_close} "
              f"flagged {flagged} days_over {check.days_over_tolerance} of {check.days_compared} "
              f"volume_ratio_median {check.volume_ratio_median}")
        if args.target != "all":
            for row in check.largest:
                print(f"  largest {row[0]} {row[1]} built {row[2]} daily {row[3]} rel {row[4]:.6g}")
    seconds = time.monotonic() - started
    print(f"totals {totals} refused {len(failures)} in {seconds:.1f} s; out {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
