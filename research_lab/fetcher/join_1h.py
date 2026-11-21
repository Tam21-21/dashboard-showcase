"""The hourly series joined on the laptop (lane D2b, docs/lanes/D2b.md, 2026-10-11).

One file per ticker, <out>/<TICKER>.US.parquet, holding two parts of one series:

  built_from_1min  hourly bars built from the 1-minute store (lane D2a, research_lab/fetcher/resample_1h.py),
                   from 2004 to 2019-12-31;
  native_1h        EODHD's own hourly bars, from 2020-10-09.

EODHD has no hourly data between the two (January to October 2020). That span is recorded, never filled:
<out>/_gaps.parquet holds one row per ticker per gap (ticker, start, end, reason).

Native bars outside 09:30 to 15:30 New York time are dropped. The built series is regular hours only with no
16:00 bar (D2a step 1), and the native file carries the session-closing 16:00 phantom bar and a few stray
off-hours prints. Native bars are checked after that cut (a flag on a dropped bar cannot reach the series; the holiday ones
are named in the log); built files are checked whole. A native bar dated before 2020-10-09 is dropped as well, because the series has no native
part before it. A native bar at or before the last built bar is an overlap and stops that ticker.

Every source file goes through the data check (research_lab/universe/data_check.py flag_rows) first. A file
whose kept rows carry a flag is refused with the flag named, and nothing is written for its ticker. The check never
edits a file, and nor does this module: the sources are read only.

The columns are the native file's eight (timestamp, gmtoffset, datetime, open, high, low, close, volume) plus
source, so the hourly reader (research_lab/docker/research-runner/eodhd_hourly.py) takes the file as it takes
a native one. timestamp is UTC epoch seconds, datetime the same bar start as UTC text, gmtoffset 0.

A ticker's gap row is written when it has built rows and either native rows follow or its built part runs to
2019-12-31. It runs from the day after the last built bar to the day before the first native bar (or the day
before 2020-10-09 when the ticker has no native part). A ticker whose built part ended earlier, with no
native part, simply ends there: that is a delisting, not a gap in a live series. A ticker with native rows
only has no gap.

DDIA check (docs/books/kleppmann_canon.md, the derived data chapter), two lines:
1. The joined files are derived data: a pure function of the built files and the native files, rebuilt whole
   by this command and never edited by hand. Each write is temp file, fsync, rename, so a reader never sees half a file.
2. The gap is kept as data in its own table, not as a missing stretch a reader has to guess at, so a page or a
   check can say "EODHD has nothing here" instead of treating the silence as a quiet market.

CLI: python -m research_lab.fetcher.join_1h [--built DIR] [--native DIR] [--out DIR] [--tickers A,B]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd

from dataclasses import dataclass, field

from research_lab.universe.data_check import closed_day_mask, flag_rows

NY = "America/New_York"
SUFFIX = ".US.parquet"
GAPS_FILE = "_gaps.parquet"

BUILT_END = pd.Timestamp("2019-12-31")  # the last session date D2a built (checked on the SSD files 2026-10-11)
NATIVE_START = pd.Timestamp("2020-10-09")  # the lane plan's first date of the native part
FIRST_BAR = pd.Timedelta(hours=9, minutes=30)  # the regular session's first bar start
LAST_BAR = pd.Timedelta(hours=15, minutes=30)  # the regular session's last bar start; native 16:00 is the phantom
GAP_REASON = "no hourly data from EODHD"

SOURCE_BUILT = "built_from_1min"
SOURCE_NATIVE = "native_1h"

OUT_COLUMNS = ["timestamp", "gmtoffset", "datetime", "open", "high", "low", "close", "volume", "source"]
_PRICE_COLUMNS = ("open", "high", "low", "close", "volume")
GAP_COLUMNS = ["ticker", "start", "end", "reason"]

DEFAULT_BUILT_DIR = "~/market_data/eodhd/staging_1h_from_1min"


class JoinRefused(ValueError):
    """One ticker's files cannot be joined. The message names the ticker, the source and the cause; ``ticker``
    holds the ticker."""

    def __init__(self, ticker: str, message: str):
        super().__init__(f"join_1h: {ticker}: {message}")
        self.ticker = ticker


@dataclass
class JoinLog:
    """What the join dropped, so nothing is silent. ``off_hours``: per ticker, the native rows from 2020-10-09
    on that fall outside 09:30 to 15:30. ``before_start``: per ticker, the native rows dated before 2020-10-09.
    ``holiday_bars``: each dropped native bar that sits on a market holiday, as (ticker, date, time)."""
    off_hours: "dict[str, int]" = field(default_factory=dict)
    before_start: "dict[str, int]" = field(default_factory=dict)
    holiday_bars: "list[tuple[str, str, str]]" = field(default_factory=list)
    removed_stale: "list[str]" = field(default_factory=list)  # tickers whose old joined file was removed on a refusal


class NothingToJoin(JoinRefused):
    """The ticker's files hold no row the series keeps. No file is written. This is a fact about the data, not a
    defect in a file, so the command reports it apart from the flagged and malformed refusals and does not fail on it."""


def _local(timestamps: pd.Series) -> pd.Series:
    """UTC epoch seconds to naive New York local time."""
    return pd.to_datetime(timestamps, unit="s", utc=True).dt.tz_convert(NY).dt.tz_localize(None)


def _check_source(ticker: str, source: str, frame: pd.DataFrame, bar_times: pd.DatetimeIndex) -> None:
    """Refuse a source file that has missing columns, a repeated stamp or a data check flag."""
    missing = [c for c in ("timestamp", *_PRICE_COLUMNS) if c not in frame.columns]
    if missing:
        raise JoinRefused(ticker, f"{source} file is missing columns {missing}; got {list(frame.columns)}")
    if frame["timestamp"].duplicated().any():
        raise JoinRefused(ticker, f"{source} file repeats a bar stamp")
    checked = frame[["high", "low", "close"]].copy()
    checked.index = bar_times
    flagged = flag_rows(checked)
    if len(flagged):
        names = sorted(set(flagged["flag"]))
        first = flagged["date"].iloc[0]
        raise JoinRefused(
            ticker, f"{source} file is flagged by the data check: {', '.join(names)} "
                    f"({len(flagged)} flagged rows, first on {first}); refusing it, nothing written"
        )


def _shape(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    """The output columns for one part: UTC epoch, offset 0, UTC text, prices, volume as float, the source."""
    out = pd.DataFrame({"timestamp": frame["timestamp"].astype("int64").to_numpy()})
    out["gmtoffset"] = 0
    out["datetime"] = pd.to_datetime(out["timestamp"], unit="s", utc=True).dt.strftime("%Y-%m-%d %H:%M:%S")
    for column in ("open", "high", "low", "close", "volume"):
        out[column] = frame[column].astype("float64").to_numpy()
    out["source"] = source
    return out[OUT_COLUMNS]


def join_ticker(ticker: str, built: "pd.DataFrame | None", native: "pd.DataFrame | None", log: "JoinLog | None" = None):
    """Join one ticker. Returns (joined frame, gap row as a dict or None). Raises JoinRefused. When ``log`` is
    given, the native rows dropped and the holiday bars among them are recorded in it."""
    if built is None and native is None:
        raise JoinRefused(ticker, "no built file and no native file")
    parts = []
    last_built_ts = None
    last_built_date = None
    if built is not None:
        if not isinstance(built.index, pd.DatetimeIndex):
            raise JoinRefused(ticker, f"built file needs a DatetimeIndex of bar starts; got {type(built.index).__name__}")
        _check_source(ticker, "built", built, built.index)
        if len(built) == 0:
            built = None
        else:
            built_local = _local(built["timestamp"])
            if built_local.max().normalize() > BUILT_END:
                raise JoinRefused(ticker, f"built file runs past {BUILT_END.date()} (to {built_local.max()})")
            parts.append(_shape(built.sort_values("timestamp"), SOURCE_BUILT))
            last_built_ts = int(built["timestamp"].max())
            last_built_date = built_local.max().normalize()
    first_native_date = None
    if native is not None:
        native_local = _local(native["timestamp"]) if "timestamp" in native.columns else None
        if native_local is None:
            raise JoinRefused(ticker, f"native file is missing columns ['timestamp']; got {list(native.columns)}")
        if last_built_ts is not None and (native["timestamp"] <= last_built_ts).any():
            raise JoinRefused(ticker, "native rows overlap the built rows in time (a native bar at or before the last built bar)")
        day = native_local.dt.normalize()
        time_of_day = native_local - day
        in_hours = (time_of_day >= FIRST_BAR) & (time_of_day <= LAST_BAR)
        keep = ((day >= NATIVE_START) & in_hours).to_numpy()
        # The data check runs on the rows the join keeps (planning session ruling, 2026-10-11): a flag on a bar
        # the join drops anyway cannot reach the series. The dropped bars are counted and the holiday ones named.
        _check_source(ticker, "native", native[keep], pd.DatetimeIndex(native_local[keep]))
        off_hours = ((day >= NATIVE_START) & ~in_hours).to_numpy()
        if log is not None:
            if off_hours.any():
                log.off_hours[ticker] = int(off_hours.sum())
            before = int((day < NATIVE_START).sum())
            if before:
                log.before_start[ticker] = before
            if off_hours.any():
                closed = closed_day_mask(day[off_hours])
                for stamp in native_local[off_hours][closed.to_numpy()]:
                    log.holiday_bars.append((ticker, stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M")))
        kept = native[keep]
        if len(kept):
            parts.append(_shape(kept.sort_values("timestamp"), SOURCE_NATIVE))
            first_native_date = native_local[keep].min().normalize()
    if not parts:
        raise NothingToJoin(ticker, "no rows left after the join (empty files, or only native rows outside regular hours or before the native start)")
    joined = pd.concat(parts, ignore_index=True)
    gap = None
    if last_built_date is not None and (first_native_date is not None or last_built_date == BUILT_END):
        start = last_built_date + pd.Timedelta(days=1)
        end = (first_native_date if first_native_date is not None else NATIVE_START) - pd.Timedelta(days=1)
        if start <= end:
            gap = {"ticker": ticker, "start": start, "end": end, "reason": GAP_REASON}
    return joined, gap


def _write_atomic(frame: pd.DataFrame, final: Path) -> None:
    """Temp file, fsync, rename, fsync of the folder; the temp file is removed if the rename did not happen."""
    tmp = final.with_name(f"{final.name}.tmp.{os.getpid()}")
    committed = False
    try:
        frame.to_parquet(tmp, index=False)
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)
        committed = True
        dir_fd = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if not committed and tmp.exists():
            tmp.unlink()


def _tickers_in(folder: Path) -> "set[str]":
    return {p.name[: -len(SUFFIX)] for p in folder.glob(f"*{SUFFIX}")}


def run(built_dir, native_dir, out_dir, tickers: "list[str] | None" = None, log: "JoinLog | None" = None) -> "list[JoinRefused]":
    """Join every ticker found in either folder (or just ``tickers``) into ``out_dir`` and write the gaps file.
    Returns the refusals, one per ticker that was refused and not written. Raises when a folder is missing."""
    built_dir, native_dir, out_dir = Path(built_dir), Path(native_dir), Path(out_dir)
    for label, folder in (("built", built_dir), ("native", native_dir)):
        if not folder.is_dir():
            raise FileNotFoundError(f"join_1h: the {label} folder {folder} does not exist")
    out_dir.mkdir(parents=True, exist_ok=True)
    names = sorted(set(tickers) if tickers is not None else _tickers_in(built_dir) | _tickers_in(native_dir))
    refusals: "list[JoinRefused]" = []
    gaps = []
    for ticker in names:
        built_path, native_path = built_dir / f"{ticker}{SUFFIX}", native_dir / f"{ticker}{SUFFIX}"
        try:
            built = pd.read_parquet(built_path) if built_path.exists() else None
            native = pd.read_parquet(native_path) if native_path.exists() else None
            joined, gap = join_ticker(ticker, built, native, log)
        except JoinRefused as exc:
            refusals.append(exc)
            stale = out_dir / f"{ticker}{SUFFIX}"
            if stale.exists():  # an old file from an earlier run must not outlive the refusal
                stale.unlink()
                if log is not None:
                    log.removed_stale.append(ticker)
                print(f"join_1h: removed stale joined file: {ticker}", flush=True)
            continue
        _write_atomic(joined, out_dir / f"{ticker}{SUFFIX}")
        if gap is not None:
            gaps.append(gap)
    gap_frame = pd.DataFrame(gaps, columns=GAP_COLUMNS)
    gaps_path = out_dir / GAPS_FILE
    if tickers is not None and gaps_path.exists():  # a partial run merges: other tickers' rows stay
        old = pd.read_parquet(gaps_path)
        touched = set(names)
        kept_rows = old[~old["ticker"].isin(touched)]
        gap_frame = pd.concat([kept_rows, gap_frame], ignore_index=True) if len(gap_frame) else kept_rows
        gap_frame = gap_frame.sort_values("ticker", kind="stable").reset_index(drop=True)
    _write_atomic(gap_frame, gaps_path)  # written even when empty, so a stale file never outlives a run
    return refusals


def main(argv: "list[str] | None" = None) -> int:
    from research_lab.worker.store_root import resolve_eodhd_store_root

    parser = argparse.ArgumentParser(prog="python -m research_lab.fetcher.join_1h")
    parser.add_argument("--built", default=os.path.expanduser(DEFAULT_BUILT_DIR),
                        help="folder holding D2a's built files, copied off the SSD (default: %(default)s)")
    parser.add_argument("--native", default=None, help="native hourly folder (default: <store>/1h)")
    parser.add_argument("--out", default=None, help="folder to write (default: <store>/1h_joined)")
    parser.add_argument("--tickers", default=None, help="comma-separated tickers, e.g. AAPL,MSFT (default: all)")
    parser.add_argument("--log-dir", default=None, help="folder for the CSV logs of what was dropped and refused")
    args = parser.parse_args(argv)
    store = resolve_eodhd_store_root()
    native = Path(args.native) if args.native else store / "1h"
    out = Path(args.out) if args.out else store / "1h_joined"
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    log = JoinLog()
    refusals = run(args.built, native, out, tickers, log)
    written = len(_tickers_in(out))
    gap_rows = len(pd.read_parquet(out / GAPS_FILE)) if (out / GAPS_FILE).exists() else 0
    empty = [r for r in refusals if isinstance(r, NothingToJoin)]
    refusals = [r for r in refusals if not isinstance(r, NothingToJoin)]
    size_bytes = sum(p.stat().st_size for p in out.iterdir() if p.is_file())
    print(f"join_1h: {written} joined files in {out} ({size_bytes / 1e9:.2f} GB), {gap_rows} gap rows, "
          f"{len(refusals)} refused, {len(empty)} with nothing to join")
    holiday_dates = sorted({d for _, d, _ in log.holiday_bars})
    print(f"join_1h: {sum(log.off_hours.values())} native rows dropped outside regular hours in {len(log.off_hours)} tickers; "
          f"{len(log.holiday_bars)} holiday bars in {len({t for t, _, _ in log.holiday_bars})} tickers on dates {holiday_dates}")
    for refusal in empty + refusals:
        print(f"  REFUSED {refusal}")
    if args.log_dir:
        folder = Path(args.log_dir)
        folder.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(sorted(log.off_hours.items()), columns=["ticker", "rows"]).to_csv(folder / "dropped_outside_regular_hours.csv", index=False)
        pd.DataFrame(sorted(log.before_start.items()), columns=["ticker", "rows"]).to_csv(folder / "dropped_before_native_start.csv", index=False)
        pd.DataFrame(sorted(log.holiday_bars), columns=["ticker", "date", "time"]).to_csv(folder / "holiday_bars.csv", index=False)
        pd.DataFrame([("nothing_to_join" if isinstance(r, NothingToJoin) else "refused", r.ticker, str(r)) for r in empty + refusals],
                     columns=["kind", "ticker", "message"]).to_csv(folder / "refused_and_empty.csv", index=False)
        print(f"join_1h: logs written to {folder}")
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
