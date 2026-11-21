"""research_lab/universe/data_check: the data check that runs after fetch (lane D1, 2026-10-07).

Flags rows of a price file that are wrong by FACT, never by a judgement threshold. Three flags:

  adjusted_close_at_ceiling -- adjusted_close at or above 999999.9999. EODHD writes this value when the
                               true adjusted price does not fit its field (measured 2026-10-07 on the
                               59,747-file daily store: 827 files, 762,072 rows; DPW traded at $1.65 while
                               its adjusted price reads 999999.9999 for 5,172 rows). The number is a cap,
                               not a price, so nothing computed from it means anything.
  non_positive_close        -- close at or below zero (4,176 files). No share trades at zero or less, and
                               the adjustment factor adjusted_close / close is undefined there.
  high_below_low            -- high below low on the same bar (90 files). Impossible by definition.
  bar_on_closed_day         -- a bar dated on a day the New York Stock Exchange held no session (lane D1b,
                               2026-10-08), read from the exchange_calendars library's XNYS calendar, never a
                               hand list. A US stock cannot trade on a day the market was shut; such a bar is
                               either another market's data filed under a US ticker (ESSA and SCCC carry
                               rupiah-sized prices and a Thanksgiving 2014 bar) or a stray row, and one such
                               bar puts a closed day into a sweep's shared date index, where every real member
                               has no price (lane P1's cn22, 2026-10-07).

The check never edits a file. It only reports; callers decide what to refuse (the universe rule drops a
name with a flag in its window or lead-in, research_lab/docker/research-runner/fetch.py refuses a
window holding one). fetch.py runs inside the image and cannot import this module, so it carries the
same three conditions; research_lab/probes/data_check pins that both agree on one fixture.

``flag_rows`` accepts any frame with high, low and close columns plus its bar dates (a ``date`` column or a
DatetimeIndex); adjusted_close is optional, so lane D2 (hourly bars built from the 1-minute store) runs the
same check on hourly bars that carry no adjusted price. The closed-day test reads each bar's calendar date as
stored (the date part, no time zone shift), so an hourly caller hands in New York local timestamps.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

CALENDAR_NAME = "XNYS"  # exchange_calendars' New York Stock Exchange calendar
CALENDAR_START = "1900-01-01"  # earlier than any bar in the store; the library builds sessions from here on

ADJUSTED_CLOSE_CEILING = 999999.9999  # EODHD's own cap value, read off the real store 2026-10-07

FLAG_ADJUSTED_CLOSE_AT_CEILING = "adjusted_close_at_ceiling"
FLAG_NON_POSITIVE_CLOSE = "non_positive_close"
FLAG_HIGH_BELOW_LOW = "high_below_low"
FLAG_BAR_ON_CLOSED_DAY = "bar_on_closed_day"
FLAGS = (FLAG_ADJUSTED_CLOSE_AT_CEILING, FLAG_NON_POSITIVE_CLOSE, FLAG_HIGH_BELOW_LOW, FLAG_BAR_ON_CLOSED_DAY)

REQUIRED_COLUMNS = ("high", "low", "close")
DAILY_COLUMNS = ("date", "high", "low", "close", "adjusted_close")  # the columns the store-wide scan reads

_PARQUET_SUFFIX = ".US.parquet"


class DataCheckInputError(ValueError):
    """The frame handed to the check lacks a column the check needs, or carries a bar date the exchange
    calendar cannot judge."""


_SESSIONS = None  # the XNYS session dates, built once per process on first use


def _session_dates() -> pd.DatetimeIndex:
    """Every XNYS session date from CALENDAR_START to the library's last known session, as naive
    midnight timestamps. Built once (the library takes about a second) and reused."""
    global _SESSIONS
    if _SESSIONS is None:
        import exchange_calendars  # lazy: only a caller that checks dates pays the import

        calendar = exchange_calendars.get_calendar(CALENDAR_NAME, start=CALENDAR_START)
        _SESSIONS = pd.DatetimeIndex(calendar.sessions).tz_localize(None).normalize()
    return _SESSIONS


def _bar_dates(frame: pd.DataFrame) -> pd.Series:
    """Each bar's calendar date (naive midnight), aligned to ``frame``'s index: the ``date`` column when
    present, else the frame's own DatetimeIndex. The date part as stored, never shifted by a time zone."""
    if "date" in frame.columns:
        stamps = pd.to_datetime(frame["date"])
    elif isinstance(frame.index, pd.DatetimeIndex):
        stamps = pd.Series(frame.index, index=frame.index)
    else:
        raise DataCheckInputError(
            f"data_check needs the bar dates (a 'date' column or a DatetimeIndex); got columns {list(frame.columns)} "
            f"and index {type(frame.index).__name__}"
        )
    if stamps.dt.tz is not None:
        stamps = stamps.dt.tz_localize(None)
    return stamps.dt.normalize()


def closed_day_mask(dates: pd.Series) -> pd.Series:
    """True where a date is not an XNYS session. A date before CALENDAR_START or after the calendar's
    last known session raises: the calendar cannot say, and an unknown day never reads as open or closed."""
    sessions = _session_dates()
    if dates.isna().any():  # a missing date is not a closed day; refuse rather than give the wrong reason
        raise DataCheckInputError(f"data_check: {int(dates.isna().sum())} bar(s) carry no date; cannot judge them")
    if len(dates):
        lo, hi = dates.min(), dates.max()
        if lo < sessions[0] or hi > sessions[-1]:
            raise DataCheckInputError(
                f"data_check: bar dates {lo.date()} to {hi.date()} fall outside the {CALENDAR_NAME} calendar's "
                f"known range {sessions[0].date()} to {sessions[-1].date()}; cannot tell open from closed days"
            )
    return ~dates.isin(sessions)


def flag_masks(frame: pd.DataFrame) -> "dict[str, pd.Series]":
    """One boolean Series per flag, aligned to ``frame``'s index. A flag whose column is absent
    (adjusted_close on an hourly frame) gets an all-False mask, because there is nothing to test."""
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise DataCheckInputError(
            f"data_check needs columns {list(REQUIRED_COLUMNS)}; missing {missing}; got {list(frame.columns)}"
        )
    closed = closed_day_mask(_bar_dates(frame))
    if "adjusted_close" in frame.columns:
        at_ceiling = frame["adjusted_close"] >= ADJUSTED_CLOSE_CEILING
    else:
        at_ceiling = pd.Series(False, index=frame.index)
    return {
        FLAG_ADJUSTED_CLOSE_AT_CEILING: at_ceiling,
        FLAG_NON_POSITIVE_CLOSE: frame["close"] <= 0,
        FLAG_HIGH_BELOW_LOW: frame["high"] < frame["low"],
        FLAG_BAR_ON_CLOSED_DAY: closed,
    }


def flags_in(frame: pd.DataFrame) -> "frozenset[str]":
    """The set of flags present anywhere in ``frame`` (empty when the frame is clean or empty)."""
    return frozenset(flag for flag, mask in flag_masks(frame).items() if bool(mask.any()))


def flag_rows(frame: pd.DataFrame, date_column: str = "date") -> pd.DataFrame:
    """Every flagged (date, flag) pair in ``frame``, sorted by date then flag. A row with two problems
    appears twice, once per flag. ``date_column`` names the column holding the bar's date; when the
    frame has no such column, the frame's own index is used (a fetched dataset's DatetimeIndex)."""
    dates = frame[date_column] if date_column in frame.columns else pd.Series(frame.index, index=frame.index)
    parts = [
        pd.DataFrame({"date": dates[mask], "flag": flag})
        for flag, mask in flag_masks(frame).items()
        if bool(mask.any())
    ]
    if not parts:
        return pd.DataFrame({"date": pd.Series(dtype=dates.dtype), "flag": pd.Series(dtype="object")})
    return pd.concat(parts).sort_values(["date", "flag"], kind="stable").reset_index(drop=True)


def scan_store(store: "str | Path") -> "dict[str, dict[str, int]]":
    """Scan every daily file in ``store`` and count, per flag, the files holding it and its rows.
    Returns {"files_scanned": {"all": n}, "files": {flag: n}, "rows": {flag: n}}. Raises when the store
    directory does not exist, so a wrong path never reads as a clean store."""
    store = Path(store)
    if not store.is_dir():
        raise FileNotFoundError(f"data_check: store directory {store} does not exist")
    files = {flag: 0 for flag in FLAGS}
    rows = {flag: 0 for flag in FLAGS}
    scanned = 0
    for path in sorted(store.glob(f"*{_PARQUET_SUFFIX}")):
        frame = pd.read_parquet(path, columns=list(DAILY_COLUMNS))
        scanned += 1
        for flag, mask in flag_masks(frame).items():
            n = int(mask.sum())
            if n:
                files[flag] += 1
                rows[flag] += n
    return {"files_scanned": {"all": scanned}, "files": files, "rows": rows}


def scan_store_rows(store: "str | Path") -> pd.DataFrame:
    """Every flagged row in ``store``, one line per (ticker, date, flag), sorted by ticker, date, flag.
    The per-stock, per-date report behind ``scan_store``'s counts. Raises on a missing store like
    ``scan_store``; an empty or clean store gives an empty frame with the same three columns."""
    store = Path(store)
    if not store.is_dir():
        raise FileNotFoundError(f"data_check: store directory {store} does not exist")
    parts = []
    for path in sorted(store.glob(f"*{_PARQUET_SUFFIX}")):
        rows = flag_rows(pd.read_parquet(path, columns=list(DAILY_COLUMNS)))
        if len(rows):
            rows.insert(0, "ticker", path.name[: -len(_PARQUET_SUFFIX)])
            parts.append(rows)
    if not parts:
        return pd.DataFrame({"ticker": pd.Series(dtype="object"), "date": pd.Series(dtype="object"),
                             "flag": pd.Series(dtype="object")})
    return pd.concat(parts, ignore_index=True)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research_lab.universe.data_check")
    parser.add_argument("--store", default=None,
                        help="daily store directory (default: <EODHD_STORE_ROOT>/daily)")
    parser.add_argument("--rows", action="store_true",
                        help="print every flagged row as ticker,date,flag (CSV with a header) instead of the counts")
    args = parser.parse_args(argv)
    if args.store is None:
        from research_lab.worker.store_root import resolve_eodhd_store_root
        store = resolve_eodhd_store_root() / "daily"
    else:
        store = Path(args.store)
    if args.rows:
        print(scan_store_rows(store).to_csv(index=False), end="")
        return 0
    report = scan_store(store)
    print(f"store {store}: {report['files_scanned']['all']} files scanned")
    for flag in FLAGS:
        print(f"{flag}: {report['files'][flag]} files, {report['rows'][flag]} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
