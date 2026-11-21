"""Daily closes from Alpaca and the price reaction around an earnings release (lane EA3-0).

Method: docs/research_lab/display_methods.md, "Data library: Earnings" (Price reaction, Earnings
chart window) and "Earnings price reaction: source change (lane EA3)". These closes are display data
and never enter research paths (DECISIONS #461).

A session's close is 16:00 New York time. "Before" is the last close earlier than the release time,
"after" the first close later than it. Known limit: on a half-day session (13:00 close) a release
between 13:00 and 16:00 is placed by the 16:00 rule.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo

import httpx

_NY = ZoneInfo("America/New_York")
_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
# One page load's reads, not a market claim (planner ruling 2026-10-23).
CACHE_SECONDS = 15 * 60
# A guard against a page token that never ends; 21 days of one-day bars for 200 tickers is a few pages.
_MAX_PAGES = 50
WINDOW_SESSIONS = 10
LEAD_DAYS = 21

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


class ClosesUnavailable(RuntimeError):
    """Alpaca could not be reached or refused; the message is a plain sentence for the page."""


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _http_get(url: str, params: dict, headers: dict) -> tuple[int, dict]:
    """The one Alpaca call (tests replace it). Returns (status code, parsed body)."""
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=15)
    except httpx.RequestError as exc:
        raise ClosesUnavailable("Could not reach Alpaca to read the closing prices.") from exc
    if resp.status_code != 200:
        return resp.status_code, {}
    try:
        return 200, resp.json()
    except ValueError as exc:
        raise ClosesUnavailable("Alpaca sent back closing prices that could not be read.") from exc


def _credentials() -> tuple[str, str]:
    from dashboard.backend.routes import display  # the server's one place that reads the Alpaca pair

    try:
        return display._read_credentials()
    except display.DisplayUpstreamError as exc:
        raise ClosesUnavailable(str(exc)) from exc


def _end_param(end: dt.date) -> str:
    """The request's end time: the end of `end`'s day, but never later than 16 minutes ago.

    Alpaca refuses recent SIP data to keys without the real-time subscription (403 for the last
    15 minutes), so asking up to today's date makes every request fail.
    """
    day_end = dt.datetime(end.year, end.month, end.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    limit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)
    return min(day_end, limit).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_daily_closes(tickers: list[str], start: dt.date, end: dt.date) -> dict[str, list[tuple[str, float]]]:
    """Adjusted daily closes per ticker as (date text, close), oldest first, cached 15 minutes.

    One multi-symbol request per page, every page followed. Raises ClosesUnavailable on any failure.
    """
    symbols = sorted(set(tickers))
    if not symbols:
        return {}
    today_ny = dt.datetime.now(_NY).date().isoformat()
    key = (tuple(symbols), today_ny, start, end)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    api_key, secret_key = _credentials()
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    params = {
        "symbols": ",".join(symbols), "timeframe": "1Day", "adjustment": "all", "feed": "sip",
        "start": start.isoformat(), "end": _end_param(end), "limit": 10000,
    }
    out: dict[str, list[tuple[str, float]]] = {s: [] for s in symbols}
    page_token = None
    for _ in range(_MAX_PAGES):
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token
        status, body = _http_get(_BARS_URL, page_params, headers)
        if status != 200:
            raise ClosesUnavailable("Alpaca refused the request for closing prices.")
        bars = body.get("bars")
        if not isinstance(bars, dict):
            raise ClosesUnavailable("Alpaca's closing prices were not in the shape this page expects.")
        for symbol, rows in bars.items():
            if symbol not in out:
                continue
            for bar in rows:
                out[symbol].append((str(bar["t"])[:10], float(bar["c"])))
        page_token = body.get("next_page_token")
        if not page_token:
            break
    else:
        raise ClosesUnavailable("Alpaca kept sending pages of closing prices; stopped rather than wait.")
    for rows in out.values():
        rows.sort(key=lambda r: r[0])
    with _lock:
        _cache[key] = (time.monotonic(), out)
    return out


def _close_instant(day: str) -> float:
    y, m, d = (int(p) for p in day.split("-"))
    return dt.datetime(y, m, d, 16, 0, tzinfo=_NY).timestamp()


def reaction_window(release_at: int, series: list[tuple[str, float]]) -> dict:
    """The 10 sessions before and 10 after a release, with before_index, after_index, reaction.

    before_index: the last close whose 16:00 New York time is earlier than release_at (None when
    none was returned). after_index: the first close later than it (None until it exists).
    reaction = closes[after] / closes[before] - 1, rounded to 4 places, None when either is None.
    """
    pre = [r for r in series if _close_instant(r[0]) < release_at][-WINDOW_SESSIONS:]
    post = [r for r in series if _close_instant(r[0]) > release_at][:WINDOW_SESSIONS]
    window = pre + post
    before_index = len(pre) - 1 if pre else None
    after_index = len(pre) if post else None
    reaction = None
    if before_index is not None and after_index is not None:
        reaction = round(window[after_index][1] / window[before_index][1] - 1, 4)
    return {
        "dates": [r[0] for r in window], "values": [r[1] for r in window],
        "before_index": before_index, "after_index": after_index, "reaction": reaction,
    }
