"""Earnings fields for a theory run, built from an earnings slice with no look-ahead (lane EA4-0, DECISIONS #480 item 8).

`earnings_frames(spec_earnings, close)` returns one float DataFrame per name in form.EARNINGS_FIELDS, shaped like `close` (dates by tickers).
`spec_earnings` is {"path": an earnings slice parquet in the materializer's schema (output_schemas COMMON_COLUMNS + EARNINGS_COLUMNS),
"ciks": {ticker: cik}}. Rows are joined to the stocks by CIK, and only rows whose role is primary_issuer count.

When a report is known: a row's knowledge_at is the headline time. Its report session is the first XNYS session whose close (UTC, early
closes included) is later than that time, so a headline at or after a session's close belongs to the next session. Every value is NaN before
its stock's first report session, and from a report session on it is that report's value (the signal is seen at the close, the order goes in
at the next open, as everywhere in theory_run).

The values, from a report session on (formulas of docs/research_lab/display_methods.md, "Surprise" and "Price reaction"; the closes are the run's own bars):
  eps_surprise_pct       100 x (eps_actual - eps_estimate) / |eps_estimate|, NaN when the estimate is 0
  eps_beat_miss          1 for BEAT, 0 for INLINE, -1 for MISS (the stored eps_word); any other word is refused
  revenue_surprise_pct   the same as eps_surprise_pct with sales_actual and sales_estimate
  days_since_report      XNYS sessions since the report session (0 on it)
  report_move_pct        100 x (close on the report session / the stock's last non-NaN close before it - 1), NaN when either is missing
An is_update row whose fiscal_quarter equals the current report's revises that report's surprise and beat or miss figures from its own report
session on; days and the move stay those of the report's first session. Any other row starts a new report.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from research_lab.theories.form import EARNINGS_FIELDS

REQUIRED_COLUMNS = ("event_id", "knowledge_at", "subject_cik", "role", "fiscal_quarter", "eps_actual", "eps_estimate", "eps_word",
                    "sales_actual", "sales_estimate", "is_update")
PRIMARY_ROLE = "primary_issuer"
BEAT_MISS = {"BEAT": 1.0, "INLINE": 0.0, "MISS": -1.0}
CALENDAR_NAME = "XNYS"
CALENDAR_START = "1900-01-01"


def _schedule() -> pd.DataFrame:
    import exchange_calendars  # lazy, as universe/data_check.py does: only a caller that needs sessions pays the import
    return exchange_calendars.get_calendar(CALENDAR_NAME, start=CALENDAR_START).schedule


def _surprise(actual, estimate):
    """100 x (actual - estimate) / |estimate|, NaN when the estimate is 0 (or a figure is missing)."""
    if estimate == 0 or pd.isna(actual) or pd.isna(estimate):
        return float("nan")
    return 100.0 * (actual - estimate) / abs(estimate)


def _figures(row) -> tuple:
    return (_surprise(row["eps_actual"], row["eps_estimate"]), BEAT_MISS[row["eps_word"]],
            _surprise(row["sales_actual"], row["sales_estimate"]))


def _read_slice(path) -> pd.DataFrame:
    table = pq.read_table(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in table.column_names]
    if missing:
        raise ValueError("%s: the earnings slice lacks column(s) %s; it must carry the materializer's earnings columns" % (path, ", ".join(missing)))
    return table.select(list(REQUIRED_COLUMNS)).to_pandas()


def _reports(rows: pd.DataFrame, closes_utc: np.ndarray, sessions: pd.DatetimeIndex) -> list:
    """The stock's reports in order: [{"session": position in `sessions`, "revisions": [(position, figures), ...]}]."""
    reports, current_quarter = [], None
    for _, row in rows.sort_values(["knowledge_at", "event_id"]).iterrows():
        if row["eps_word"] not in BEAT_MISS:
            raise ValueError("event %s: eps_word %r is not one of %s" % (row["event_id"], row["eps_word"], ", ".join(BEAT_MISS)))
        stamp = pd.Timestamp(row["knowledge_at"])
        position = int(np.searchsorted(closes_utc, stamp.to_datetime64(), side="right"))
        if position >= len(sessions):
            raise ValueError("event %s: knowledge_at %s is after the last XNYS session the calendar knows" % (row["event_id"], stamp))
        figures = _figures(row)
        if bool(row["is_update"]) and reports and current_quarter == row["fiscal_quarter"]:
            reports[-1]["revisions"].append((position, figures))
        else:
            reports.append({"session": position, "revisions": [(position, figures)]})
            current_quarter = row["fiscal_quarter"]
    return reports


def earnings_frames(spec_earnings: dict, close: pd.DataFrame) -> dict:
    ciks = spec_earnings["ciks"]
    missing = [t for t in close.columns if t not in ciks]
    if missing:
        raise ValueError("spec earnings.ciks has no entry for %s; every stock in the group needs its CIK" % ", ".join(map(str, missing)))
    schedule = _schedule()
    sessions = schedule.index
    dates = pd.DatetimeIndex(close.index)
    if dates.tz is not None:
        dates = dates.tz_localize(None)
    dates = dates.normalize()
    for d in dates:
        if d < sessions[0] or d > sessions[-1] or d not in sessions:
            raise ValueError("the bar dated %s is not an XNYS session (the calendar runs %s to %s)"
                             % (d.strftime("%Y-%m-%d"), sessions[0].strftime("%Y-%m-%d"), sessions[-1].strftime("%Y-%m-%d")))
    bar_position = sessions.get_indexer(dates)
    closes_utc = schedule["close"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()

    slice_rows = _read_slice(spec_earnings["path"])
    slice_rows = slice_rows[slice_rows["role"] == PRIMARY_ROLE].copy()
    slice_rows["knowledge_at"] = pd.to_datetime(slice_rows["knowledge_at"], utc=True).dt.tz_localize(None)

    shape = (len(dates), len(close.columns))
    out = {name: pd.DataFrame(np.full(shape, np.nan), index=close.index, columns=close.columns) for name in EARNINGS_FIELDS}
    for column, ticker in enumerate(close.columns):
        rows = slice_rows[slice_rows["subject_cik"] == ciks[ticker]]
        reports = _reports(rows, closes_utc, sessions)
        prices = close[ticker].to_numpy(dtype=float)
        for k, report in enumerate(reports):
            end = reports[k + 1]["session"] if k + 1 < len(reports) else None
            live = bar_position >= report["session"]
            if end is not None:
                live &= bar_position < end
            if not live.any():
                continue
            eps = np.full(len(dates), np.nan)
            beat = np.full(len(dates), np.nan)
            revenue = np.full(len(dates), np.nan)
            for j, (start, figures) in enumerate(report["revisions"]):
                stop = report["revisions"][j + 1][0] if j + 1 < len(report["revisions"]) else None
                part = bar_position >= start
                if stop is not None:
                    part &= bar_position < stop
                eps[part], beat[part], revenue[part] = figures
            out["eps_surprise_pct"].iloc[live, column] = eps[live]
            out["eps_beat_miss"].iloc[live, column] = beat[live]
            out["revenue_surprise_pct"].iloc[live, column] = revenue[live]
            out["days_since_report"].iloc[live, column] = (bar_position - report["session"])[live]
            on_report = np.flatnonzero(bar_position == report["session"])
            if len(on_report):
                at = int(on_report[0])
                before = prices[:at][~np.isnan(prices[:at])]
                if len(before) and not np.isnan(prices[at]):
                    out["report_move_pct"].iloc[live, column] = 100.0 * (prices[at] / before[-1] - 1.0)
    return out
