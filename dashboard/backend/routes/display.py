"""Alpaca hourly price bars for on-screen display ONLY. Plain-English: this file asks Alpaca (a
market-data company) for a ticker's recent hourly prices so the Data Library page can draw a
chart, and it never saves anything anywhere. Built to the exact shape of dashboard/backend/
weather.py (night plan 2026-09-09 section 10, lane DL): one external HTTP call, a short
in-memory cache, NO database, and no import from research_lab or anywhere under
dashboard/backend/research/. That boundary matters because a strategy backtest must never
accidentally read a display-only price the research pipeline never verified or recorded
(root CLAUDE.md, "Parameter provenance and data boundaries": "Display data ... never enters
research paths, backtests, or strategy-readable stores").

Credentials: ALPACA_API_KEY and ALPACA_SECRET_KEY are read straight from the process environment
(os.environ), the same two names research_lab/worker/jobs/fetch_dataset.py already reads and the
same two names already present in the server's .env (night plan section 10: "The credential
question is settled, and it is not a stop."). This file does NOT import dashboard/backend/config.py
on purpose: config.py is outside this lane's declared write area for the night's plan, and reading
os.environ directly here needs no change to it. Neither key is ever written to a log line, an
exception message, or a cached payload: every error path below names only the symbol and the kind
of failure, never the credential values.
"""
from __future__ import annotations

# datetime/timezone: compute the request window (the last few days) and the 15-minute-plus-margin
# cutoff the free SIP feed requires, in UTC, so the route's own clock never depends on server locale.
from datetime import datetime, timedelta, timezone
# logging: one line per successful fetch and per failure class, never a key.
import asyncio
import logging
# zoneinfo: Alpaca stamps a daily bar at midnight New York time; the history route reads the trading
# day as the New York date of that stamp.
from zoneinfo import ZoneInfo
# os: read the two Alpaca credentials directly from the environment (see the module docstring).
import os
import re
# time: a monotonic clock for the in-memory cache's age check (monotonic never jumps backward on
# an NTP correction the way wall-clock time can, matching weather.py's own cache).
import time

# httpx: the one external call this file makes, matching weather.py's own async client.
import httpx
# json: parse a research_events payload column for the market-news projection (AC-6).
import json
from fastapi import APIRouter, HTTPException, Query

# eventsdb: the SAME connection helper the live-feeds page (events/feeds.py) uses to read
# research_events, so market-news reads the identical rows through the identical path rather than
# a second, drifting pipeline (AC-6: "the same rows Event Ops reads").
from .. import eventsdb
from ..events import cik_ticker_map as event_cik_ticker_map
# The same Alpaca news client the news fetcher uses (lane LK1): one rate-limited page per call.
from ..events.clients.alpaca_news import (
    AlpacaNewsClient,
    AlpacaNewsHttp429Persistent,
    AlpacaNewsHttp5xx,
    AlpacaNewsNetworkTimeout,
    AlpacaNewsResponseInvalid,
)
# market_display: the new EODHD client, cache and daily budget module this lane adds. Night plan
# 2026-09-21 set 14 section 6: "New module dashboard/backend/market_display.py for the client, the
# cache and the budget; routes/display.py gains the routes only."
from .. import market_display

log = logging.getLogger("dashboard.display")

# How many hits the lookup box shows (lane LK1 step 2): a display dial, not a backtest parameter.
_SEARCH_LIMIT = 12

# /news: the ticker's allowed shape, and the four ways the Alpaca news client reports a failure.
_NEWS_TICKER_SHAPE = re.compile(r"[A-Z0-9.\-]{1,10}")
_NEWS_CLIENT_ERRORS = (
    AlpacaNewsHttp429Persistent,
    AlpacaNewsHttp5xx,
    AlpacaNewsNetworkTimeout,
    AlpacaNewsResponseInvalid,
)

router = APIRouter(prefix="/api/display")

_NEW_YORK = ZoneInfo("America/New_York")

# The Alpaca Market Data REST endpoint for historical bars (verified live in this repo by
# dashboard/verification_scripts/verify_alpaca_sip_access.py and named in
# docs/design/dashboard_ui_plan_2026-09-07.md section 2.2.1). "{symbol}" is filled in per request.
_ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"

# How long a cached response is served before this route asks Alpaca again. Bars at the hourly
# timeframe only change once an hour, so five minutes keeps the page reasonably fresh while
# staying far under Alpaca's 200-requests-per-minute Basic-plan throttle (verified in the same UI
# plan section) even if several people have the page open at once. This is a display refresh
# cadence, not a backtest parameter, so it does not belong in parameter_provenance.md (root
# CLAUDE.md, "Operational dials need inline justification only, #204).
_CACHE_TTL_SECONDS = 300.0

# The free SIP feed will not serve bars newer than "now minus fifteen minutes"; this route asks up
# to thirty minutes back for the same margin dashboard/backend/dataset_sources.py already codes
# (_ALPACA_END_LAG_SEC = 30 * 60, "clears the 15-min free-tier delay with margin").
_ALPACA_END_LAG_SECONDS = 30 * 60

# How far back the window reaches BY DEFAULT (no `start` given). Ten calendar days of hourly bars
# is comfortably more than a week of trading sessions (weekends and one holiday included), enough
# for the index chart to draw a readable multi-day line without asking Alpaca for more history than
# the page ever shows.
_WINDOW_DAYS = 10

# Night plan 2026-09-12 (set 5 day, lane PP): the FARTHEST back an explicit `start` may reach, so a
# paper run's own DETAILS chart can widen the window past the default ten days when that run has
# been open longer, without turning this into an unbounded historical-data route. 90 days is an
# operational display dial (root CLAUDE.md #204's inline-justification rule, not a backtest
# parameter): it covers a paper run through its first calendar quarter, comfortably past the outer
# edge of what this sprint's own paper period runs (Tamer defines that period, but nothing in this
# lane's own scope asks for more than a few weeks), while keeping one request bounded rather than
# letting a caller ask this in-memory, unevicted cache to hold years of a symbol's hourly bars.
_MAX_START_DAYS_BACK = 90

# Set 9 night lane DX (U.10 item 2): the long view is daily, not only hourly. One entry per
# supported Alpaca timeframe token: how far back the window reaches with no explicit `start`
# (default_days), the farthest back an explicit `start` may widen to (max_start_days_back), and
# how many bars this route asks Alpaca for per page (page_limit). A timeframe not in this table is
# refused 422 by name below, so a caller can never silently fall through to some other grain's
# numbers. "1Hour" keeps the exact pre-existing values (_WINDOW_DAYS, _MAX_START_DAYS_BACK, the
# literal 500 the route always asked for) so nothing about the SPY index chart's own behaviour
# moves. "1Day" is new: five years back by default, ten years at most (an operational display
# dial, #204, not a backtest parameter: it covers the paper page's own long view comfortably
# without turning this into an unbounded historical-data route), Alpaca SIP daily starting
# 2016-01-04 (root CLAUDE.md's own Alpaca row) so ten years already reaches past that start for
# any request made before 2026. page_limit 10_000 is Alpaca's own documented per-page cap for the
# v2 bars endpoint (docs.alpaca.markets/us/reference/stockbars, read 2026-09-15, the plan's own
# already-sourced fact; this builder has no live web access to re-read the page on the day, so the
# plan's own citation is the source of record here, named rather than silently trusted): a request
# beyond that many bars comes back with a `next_page_token` this route now follows (below) rather
# than silently truncating at whatever the caller happened to ask for.
_TIMEFRAME_WINDOWS: dict[str, dict[str, int]] = {
    # "bars_per_day": the WORST-CASE bar count per calendar day at this grain, used only to derive
    # a defensive page ceiling below, never to compute an actual request. 1Hour uses 24 (every
    # calendar hour, not the roughly 6.5 real trading hours a US equity session holds) so the
    # ceiling is generous rather than tuned to a market calendar this route does not otherwise
    # depend on; 1Day uses 1 (a daily bar is at most one per calendar day).
    "1Hour": {"default_days": _WINDOW_DAYS, "max_start_days_back": _MAX_START_DAYS_BACK, "page_limit": 500,
              "bars_per_day": 24},
    "1Day": {"default_days": 5 * 365, "max_start_days_back": 10 * 365, "page_limit": 10_000,
             "bars_per_day": 1},
}

# Set 9 night lane DX round 4 (SF3, code-quality pass 1): a defensive page ceiling for the
# next_page_token loop below, derived from the table above rather than a flat guess, so widening a
# timeframe's own window later widens its ceiling too instead of silently going stale. The
# arithmetic: the most bars a timeframe's own widest allowed window could ever hold is
# max_start_days_back * bars_per_day (both worst-case, see the comment above); dividing by
# page_limit gives the page count a request for that many bars would take; +1 page of slack absorbs
# Alpaca's own pagination not landing on an exact page boundary. This bounds a MISBEHAVING upstream
# (a next_page_token that never goes empty) without ever bounding a real request this route could
# legitimately make, since every real request asks for at most max_start_days_back worth of bars.
for _tf_key, _tf_window in _TIMEFRAME_WINDOWS.items():
    _max_bars = _tf_window["max_start_days_back"] * _tf_window["bars_per_day"]
    _tf_window["max_pages"] = -(-_max_bars // _tf_window["page_limit"]) + 1  # ceil-div + one page of slack
del _tf_key, _tf_window, _max_bars  # scratch loop variables, not part of this module's public surface

# The in-memory cache: one entry per (symbol, timeframe) pair, holding the last successful payload
# and when it was fetched. A process restart clears it, same as weather.py's module-level cache;
# nothing here is durable and nothing here is meant to be.
_cache: dict[str, dict] = {}

# Ops routing law (root CLAUDE.md: "no merge with a declared failure mode unmonitored"), matching
# the exact shape dashboard/backend/routes/ops.py's own _COUNTER_SOURCES tuple already reads from
# paperdb, judgedb and researchdb (dashboard/backend/CLAUDE.md's "routes/ops.py" row): a plain
# module-level dict, incremented at its one increment site below, readable by attribute name. The
# operations PAGE that would surface this is parked until October (#300), so this build's own
# "wired in" is the dict existing plus its registry row (docs/units/DL/monitor_rows.md), per the
# frontend-builder charter's own ops routing law text: "Until the operations page exists, 'wired
# in' means the counter or status exists and its registry row is written." A count that only rises
# is the right shape here: a stuck-refusing route (an expired key, a changed Alpaca response shape)
# is what turns the chart into a permanent, silent NO DATA for every visitor, and this is the signal
# that lets someone notice before a user has to report it.
DISPLAY_MONITOR_COUNTERS: dict[str, int] = {
    "display_route_refusals": 0,
    # Set 14 night, lane OB: the five failure classes the plan's own "what can break" list names
    # for the new EODHD market routes (plan section 6 item 10), extending this SAME dict rather
    # than starting a second one, per that item's own instruction ("the existing
    # DISPLAY_MONITOR_COUNTERS dict ... is the shape to extend"). Every one of these is a plain,
    # rising count: nothing here is ever decremented, and a process restart is the only reset.
    "market_key_absent": 0,  # a key a market route needed (EODHD, FMP, FRED or Alpaca) missing or empty
    "market_chunk_refused": 0,  # a quotes chunk (<=20 symbols) the upstream refused outright
    "market_daily_budget_spent": 0,  # a market route refused because today's call budget is gone
    "market_shape_changed": 0,  # EODHD answered 200 but the body was not the shape this route reads
    "market_all_routes_refusing": 0,  # every one of the five upstream-calling market routes' most
    # recent call ended in a 503, in the same process: a genuine "the whole EODHD integration is
    # down" signal, not any one route's own count
}

# AC-7: the newest upstream quote timestamp this process has ever seen, read by GET
# /api/display/market-session. A plain module-level dict (not a bare variable) so a probe can
# reach in and reset it directly (p07: `display_route._NEWEST_QUOTE_STAMP["iso"] = None`), exactly
# the pattern _cache and DISPLAY_MONITOR_COUNTERS already use for probe-visibility. "iso" is None
# until the first quote this process has ever fetched arrives; never a fabricated value.
_NEWEST_QUOTE_STAMP: dict = {"iso": None}

# The five routes this lane adds that ever call EODHD (market-news makes no upstream call at all
# and so is never a candidate for "every route refusing"; market-session never calls EODHD either).
# Tracks each one's OWN most recent outcome (True = last call returned 200 to its caller, False =
# the route itself answered 503). market_all_routes_refusing increments only once every one of
# these five has been called at least once AND every one of their most recent outcomes is a
# refusal -- a genuine "nothing in this integration is working right now" signal, not a proxy for
# any single route's own retry count.
_MARKET_ROUTE_NAMES = ("quotes", "history", "yield_curve", "commodities")
_LAST_ROUTE_OUTCOME: dict[str, bool] = {}


def _note_route_outcome(route_name: str, succeeded: bool) -> None:
    """Record whether `route_name`'s most recent call succeeded (True) or was refused (False,
    i.e. it answered 503), and roll the market_all_routes_refusing counter when every named route
    has been called at least once and every one of them is currently refusing."""
    _LAST_ROUTE_OUTCOME[route_name] = succeeded
    if len(_LAST_ROUTE_OUTCOME) == len(_MARKET_ROUTE_NAMES) and not any(_LAST_ROUTE_OUTCOME.values()):
        DISPLAY_MONITOR_COUNTERS["market_all_routes_refusing"] += 1


def _note_quote_stamp(iso_ts: "str | None") -> None:
    """Advance _NEWEST_QUOTE_STAMP to iso_ts if iso_ts is newer than what is already recorded.
    ISO-8601 strings produced with a fixed UTC offset (market_display.epoch_to_iso's own shape,
    "...+00:00") compare correctly as plain strings: later instants sort lexicographically later."""
    if iso_ts is None:
        return
    current = _NEWEST_QUOTE_STAMP["iso"]
    if current is None or iso_ts > current:
        _NEWEST_QUOTE_STAMP["iso"] = iso_ts


def _derive_session_word(stamp_iso: "str | None") -> str:
    """AC-7: the session word is derived from how FRESH the newest quote this process has seen
    is, never from an invented holiday calendar. No data yet -> "unknown" (never "open", which
    would be a fabricated claim). A quote no older than fifteen minutes reads "live"; anything
    older reads "stale". Fifteen minutes is an operational display dial (root CLAUDE.md #204: an
    inline reason is enough, no provenance entry) chosen to match the free SIP feed's own delay
    margin this file's Alpaca code already codes (_ALPACA_END_LAG_SECONDS above)."""
    if stamp_iso is None:
        return "unknown"
    stamp_dt = datetime.fromisoformat(stamp_iso)
    age_seconds = (datetime.now(timezone.utc) - stamp_dt).total_seconds()
    return "live" if 0 <= age_seconds < 15 * 60 else "stale"


class DisplayUpstreamError(RuntimeError):
    """Alpaca could not be reached, or refused the request. Carries a plain-English reason only;
    never the request URL (which would carry the API key as a header, not the URL, but the frontend
    map's own trap 7 is exactly about never rendering a raw URL to begin with) and never a bare
    status code standing in for an explanation."""


def _read_credentials() -> tuple[str, str]:
    """The two Alpaca keys from the environment, or a named, loud failure. Never logged, never
    echoed back in an exception message (only their ABSENCE is reported, never their value)."""
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise DisplayUpstreamError(
            "Alpaca is not configured on this server: the price data key is missing."
        )
    return api_key, secret_key


async def _fetch_bars_from_alpaca(symbol: str, timeframe: str, start_override: "datetime | None" = None,
                                  adjustment: str = "raw") -> list[dict]:
    """One or more rate-limited GETs to Alpaca's bars endpoint, returning the FULL parsed bar list
    (every page followed). Raises DisplayUpstreamError with a plain-English reason on any failure;
    never returns a partial or fabricated result.

    start_override, when given, replaces the timeframe's own default-days-back start (get_bars
    below has already bounded it to the timeframe's own max_start_days_back before this function
    ever sees it, so this function trusts the value rather than re-clamping it). adjustment is Alpaca's
    own corporate-action setting: "raw" (the default, what every earlier caller gets) or "all" (adjusted
    for splits and dividends, what the ticker lookup asks for). The timeframe
    itself is trusted to be a key of _TIMEFRAME_WINDOWS: get_bars refuses an unsupported one with a
    422 before this function is ever called, so the plain dict lookup below never raises KeyError.
    """
    api_key, secret_key = _read_credentials()
    window = _TIMEFRAME_WINDOWS[timeframe]

    now = datetime.now(timezone.utc)
    end = now - timedelta(seconds=_ALPACA_END_LAG_SECONDS)
    start = start_override if start_override is not None else end - timedelta(days=window["default_days"])
    params = {
        "timeframe": timeframe,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # "sip": the consolidated feed this account's key already reaches for other price work
        # (research_lab/docker/research-runner/fetch.py's fetch_alpaca_sip uses the same feed name),
        # behind the fifteen-minute delay _ALPACA_END_LAG_SECONDS already clears with margin.
        "feed": "sip",
        "adjustment": adjustment,
        "limit": window["page_limit"],
    }
    # Alpaca's own header names (data.alpaca.markets), matching this repo's existing Alpaca client
    # (dashboard/backend/events/clients/alpaca_news.py) and verification script exactly, so a key
    # that already works for news or research fetches also works here with no new grant.
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}

    all_bars: list[dict] = []
    page_token: "str | None" = None
    pages_fetched = 0
    async with httpx.AsyncClient(timeout=10) as client:
        # Set 9 night lane DX (U.10 item 2): follow Alpaca's own next_page_token until the
        # response carries none, so a wide daily window (up to ten years) is never silently
        # truncated at one page's worth of bars. A short hourly window never triggers a second
        # page in practice (its own page_limit already exceeds what ten days of hourly bars
        # holds), so this loop changes nothing observable for the pre-existing 1Hour path.
        # Round 4 (SF3, code-quality pass 1): the loop trusted the upstream to eventually stop
        # sending a token; it now refuses by name instead, once window["max_pages"] (derived above)
        # is exceeded, so a token that never goes empty fails loud rather than growing all_bars and
        # holding the request open without bound.
        while True:
            if pages_fetched >= window["max_pages"]:
                log.warning(
                    "display bars fetch exceeded its page ceiling: symbol=%s timeframe=%s pages=%d",
                    symbol, timeframe, pages_fetched,
                )
                raise DisplayUpstreamError(
                    f"Alpaca kept paging past the expected limit for {symbol} {timeframe} bars "
                    f"(stopped after {pages_fetched} pages); refusing rather than growing without bound."
                )
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            try:
                resp = await client.get(_ALPACA_BARS_URL.format(symbol=symbol), params=page_params, headers=headers)
            except httpx.RequestError as exc:
                # A network-level failure (DNS, connect, timeout): named by class, never by the
                # exception's own text, which can embed the request URL.
                log.warning("display bars fetch failed (network): symbol=%s timeframe=%s", symbol, timeframe)
                raise DisplayUpstreamError(f"Could not reach Alpaca to fetch {symbol} bars.") from exc

            if resp.status_code != 200:
                log.warning(
                    "display bars fetch refused: symbol=%s timeframe=%s status=%d",
                    symbol, timeframe, resp.status_code,
                )
                raise DisplayUpstreamError(f"Alpaca refused the request for {symbol} bars.")

            try:
                body = resp.json()
            except ValueError as exc:
                raise DisplayUpstreamError(f"Alpaca sent back something that was not readable for {symbol} bars.") from exc

            bars = body.get("bars")
            if bars is None:
                bars = []  # Alpaca's way of saying "no bars for this ticker in this window"
            if not isinstance(bars, list):
                raise DisplayUpstreamError(f"Alpaca's response for {symbol} bars was not in the shape this page expects.")
            all_bars.extend(bars)
            pages_fetched += 1  # counted after a page is actually consumed, so the ceiling check above is exact

            page_token = body.get("next_page_token")
            if not page_token:
                break
    return all_bars


def _to_display_bar(raw: dict) -> dict:
    """One Alpaca bar (keys o/h/l/c/v/t, the documented Market Data bars shape) -> this route's own
    response shape: the ISO time Alpaca sent (for a human to read) plus the UNIX-epoch SECONDS a
    chart needs for an hourly series (lightweight-charts' own installed typings say an intraday
    chart needs a numeric UTCTimestamp, not a date-only string). A bar missing an expected field
    fails loudly with the field named, never silently substituting zero.
    """
    try:
        iso_time = raw["t"]
        epoch_seconds = int(datetime.fromisoformat(iso_time.replace("Z", "+00:00")).timestamp())
        return {
            "time": iso_time,
            "epoch_seconds": epoch_seconds,
            "open": float(raw["o"]),
            "high": float(raw["h"]),
            "low": float(raw["l"]),
            "close": float(raw["c"]),
            "volume": float(raw["v"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise DisplayUpstreamError("Alpaca sent a bar this page could not read.") from exc


@router.get("/bars")
async def get_bars(
    symbol: str = Query(default="SPY"),
    # "1Hour" is Alpaca's own timeframe token (the UI plan section 2.2.1: "SPY ... in hourly bars").
    timeframe: str = Query(default="1Hour"),
    # Night plan 2026-09-12, lane PP: an optional ISO date (YYYY-MM-DD) widening the window's start
    # past the default _WINDOW_DAYS, so a paper run's own DETAILS chart (open longer than ten days)
    # can still draw its earliest fills. Bounded to _MAX_START_DAYS_BACK below; never unbounded.
    start: "str | None" = Query(default=None),
) -> dict:
    """Display-only hourly, daily (or whatever timeframe the table below names) bars for one
    symbol. Serves a cached payload when one is fresh; otherwise calls Alpaca, caches the result,
    and returns it. Raises a 503 with a plain-English `detail` on any failure, which the frontend
    renders verbatim (never a URL, never a bare status code: frontend map trap 7) as the chart's
    NO DATA reason.
    """
    # Set 9 night lane DX (U.10 item 2): a timeframe outside the supported table is refused by
    # name, 422, before any cache lookup or upstream call, rather than silently falling through to
    # some other grain's window numbers.
    if timeframe not in _TIMEFRAME_WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"timeframe must be one of {sorted(_TIMEFRAME_WINDOWS)}; got {timeframe!r}.",
        )
    window = _TIMEFRAME_WINDOWS[timeframe]

    start_dt: "datetime | None" = None
    if start is not None:
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"start must be an ISO date (YYYY-MM-DD); got {start!r}.",
            ) from exc
        earliest_allowed = datetime.now(timezone.utc) - timedelta(days=window["max_start_days_back"])
        # Clamped, never refused: a caller asking for more history than this route allows still
        # gets the MOST this route can honestly give, rather than an error over an honest request.
        if start_dt < earliest_allowed:
            start_dt = earliest_allowed

    # The cache key carries the CLAMPED start (rounded to the day) so a widened-window request and
    # the page's own default short-window request are never served each other's payload.
    cache_key = f"{symbol}:{timeframe}:{start_dt.date().isoformat() if start_dt is not None else ''}"
    cached = _cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached["cached_at_monotonic"]) < _CACHE_TTL_SECONDS:
        return cached["payload"]

    try:
        raw_bars = await _fetch_bars_from_alpaca(symbol, timeframe, start_dt)
        display_bars = [_to_display_bar(b) for b in raw_bars]
    except DisplayUpstreamError as exc:
        # The one increment site for DISPLAY_MONITOR_COUNTERS: every refusal this route ever returns
        # passes through exactly here, so a single counter, incremented once per refusal regardless
        # of which named failure caused it (missing key, network error, a bad Alpaca response
        # shape), already answers "how often is this route failing" without needing one key per
        # failure class.
        DISPLAY_MONITOR_COUNTERS["display_route_refusals"] += 1
        # 503: the page is fine, the upstream (Alpaca) is the problem, matching the judge page's own
        # "store cannot be opened" 503 idiom the night plan asks this route to follow.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": display_bars,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache[cache_key] = {"payload": payload, "cached_at_monotonic": time.monotonic()}
    log.debug("display bars served: symbol=%s timeframe=%s bar_count=%d", symbol, timeframe, len(display_bars))
    return payload


# ---------------------------------------------------------------------------------------------
# Night plan 2026-09-21 set 14, lane OB: the seven display-only EODHD market routes. Built to
# docs/units/OB/criteria.md AC-1 through AC-11. Every route below follows the same shape as
# get_bars above: read the key or refuse by name, serve a fresh cache hit, otherwise call
# market_display (never httpx directly -- that indirection is what lets a probe monkeypatch
# market_display.httpx.AsyncClient without touching this file), cache the result, return it. No
# database import here (grep -cE 'sqlite3|researchdb\.|paperdb\.|judgedb\.' over this block prints
# 0 outside get_market_news's own eventsdb read, which is a READ of the store Event Ops already
# reads, never a write and never a research path).
# ---------------------------------------------------------------------------------------------


@router.get("/quotes")
async def get_quotes(symbols: str = Query(...)) -> dict:
    """AC-1: one row per requested symbol, chunked at market_display.QUOTE_CHUNK_SIZE, with a
    refused chunk isolated so every OTHER symbol's row still returns."""
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not requested:
        raise HTTPException(status_code=422, detail="symbols must name at least one ticker.")

    try:
        market_display._read_credentials()
    except market_display.MarketKeyAbsent as exc:
        DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
        _note_route_outcome("quotes", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rows: dict[str, dict] = {}
    to_fetch: list[str] = []
    for sym in requested:
        cached = market_display.cache_get("quotes", sym)
        if cached is not None:
            rows[sym] = cached
        else:
            to_fetch.append(sym)

    chunks = [
        to_fetch[i : i + market_display.QUOTE_CHUNK_SIZE]
        for i in range(0, len(to_fetch), market_display.QUOTE_CHUNK_SIZE)
    ]

    if chunks:
        try:
            market_display.check_budget_or_raise(len(chunks))
        except market_display.MarketBudgetExhausted as exc:
            DISPLAY_MONITOR_COUNTERS["market_daily_budget_spent"] += 1
            _note_route_outcome("quotes", False)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    for chunk in chunks:
        ok, raw = await market_display.fetch_quotes_chunk(chunk)
        market_display.charge_budget(1)
        if not ok or raw is None:
            # AC-1's own isolation rule: this chunk's symbols come back absent with a reason;
            # every OTHER chunk (already fetched above, or fetched below) still returns.
            DISPLAY_MONITOR_COUNTERS["market_chunk_refused"] += 1
            for sym in chunk:
                rows[sym] = {
                    "symbol": sym,
                    "available": False,
                    "reason": "EODHD could not return a quote for this batch of symbols right now.",
                }
            continue

        matched: set[str] = set()
        for raw_row in raw:
            code = str(raw_row.get("code", "")).split(".")[0].upper()
            if code not in chunk:
                continue  # a row this chunk never asked for; ignore rather than guess whose it is
            iso_ts = market_display.epoch_to_iso(raw_row.get("timestamp"))
            row = {
                "symbol": code,
                "available": True,
                "last": raw_row.get("close"),
                "change": raw_row.get("change"),
                "change_percent": raw_row.get("change_p"),
                "timestamp": iso_ts,
            }
            rows[code] = row
            market_display.cache_set("quotes", code, row)
            _note_quote_stamp(iso_ts)
            matched.add(code)

        # A 200 response that omits a symbol this chunk asked for is a shape drift, not a refusal:
        # EODHD answered, just not for everyone. Never a fabricated zero.
        for sym in chunk:
            if sym not in matched:
                DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
                rows[sym] = {
                    "symbol": sym,
                    "available": False,
                    "reason": "EODHD's response did not include this symbol.",
                }

    _note_route_outcome("quotes", True)
    return {"quotes": [rows[s] for s in requested]}


def _alpaca_bar_as_daily_row(raw) -> dict:
    """One Alpaca daily bar (t, o, h, l, c, v) as the {date, open, high, low, close, volume} row the
    history route's row loop reads. The day is the America/New_York date of t (Alpaca stamps a daily
    bar at midnight New York time, so that date is the trading day). A field Alpaca left out stays
    None, and a t that cannot be read leaves the date None: the row loop then skips the row and
    counts it, never filling it."""
    if not isinstance(raw, dict):
        return {"date": None, "open": None, "high": None, "low": None, "close": None, "volume": None}
    day = None
    stamp = raw.get("t")
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        # A time with no offset would be read as server-local time, which says nothing about the
        # trading day: leave the date empty so the row is skipped and counted.
        if parsed is not None and parsed.tzinfo is not None:
            day = parsed.astimezone(_NEW_YORK).date().isoformat()
    return {
        "date": day,
        "open": raw.get("o"),
        "high": raw.get("h"),
        "low": raw.get("l"),
        "close": raw.get("c"),
        "volume": raw.get("v"),
    }


@router.get("/history")
async def get_history(symbol: str = Query(...), period: str = Query(default="1y"),
                      interval: str = Query(default="day")) -> dict:
    """AC-2: OHLC plus volume for one instrument, in the kit Chart primitive's own shape. Names
    which form "time" takes in the response itself (time_form) so the caller never has to guess.

    Lane MKT1: the symbol is looked up in market_display.DISPLAY_SERIES (or is a "<TICKER>.US"
    ticker); any other symbol is a 422 naming it, with no call and nothing tried in its place. The
    source that owns the series answers: FMP or Yahoo through market_display.fetch_daily_bars
    (cached twelve hours), Alpaca through this file's own bars code. Every bar's open, high, low,
    close and volume is the source's own.

    Lane LK1: a "<TICKER>.US" ticker is decided first: the EODHD store's daily file when it holds the
    ticker (no key, no budget), else Alpaca adjusted for splits and dividends. Either way the bars are
    cleaned (market_display.clean_daily) and the answer also carries "source", "last_day" (the day of
    the newest bar, or null with no bars) and "left_out" (how many bars were left out, by reason).

    Lane LK1b: for a ticker the store holds, the bars after the store's last day come from Alpaca,
    scaled so there is no jump at the join (market_display.join_scaled); the answer also carries
    "join" ({"day", "scale"} or null) and "fill_error" (why there is no join, or null). interval=hour
    gives Alpaca's hourly bars for period 1d or 1w (_hourly_history)."""
    symbol_u = symbol.upper()
    try:
        source, series_id = market_display.series_for(symbol_u)
    except market_display.MarketSymbolUnknown as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    lookup = symbol_u not in market_display.DISPLAY_SERIES  # a "<TICKER>.US" ticker (series_for proved the shape)
    interval_u = interval.strip().lower()
    if interval_u not in ("day", "hour"):
        raise HTTPException(status_code=422, detail="The interval must be day or hour.")
    if interval_u == "hour":
        if not lookup:
            raise HTTPException(status_code=422, detail="Hourly bars are only served for a ticker ending in .US.")
        if period.strip().lower() not in ("1d", "1w"):
            raise HTTPException(status_code=422, detail="Hourly bars are only served for a period of 1d or 1w.")
        return await _hourly_history(symbol_u, period, series_id)

    store_file = None
    if lookup:
        try:
            store_file = market_display.eodhd_store_file(symbol_u)
        except ValueError as exc:
            _note_route_outcome("history", False)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if store_file is None:  # the store's own join needs no key up front: a missing key is its fill_error
        try:
            market_display.require_source_keys(source)
        except market_display.MarketKeyAbsent as exc:
            DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
            _note_route_outcome("history", False)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if store_file is not None:
        cache_route, cache_key = "history", f"joined:{symbol_u}:{period}"
    elif lookup:
        cache_route, cache_key = "history", f"Alpaca:{symbol_u}:{period}"
    else:
        cache_route = "history" if source == "alpaca" else "history_daily"
        cache_key = f"{symbol_u}:{period}"
    cached = market_display.cache_get(cache_route, cache_key)
    if cached is not None:
        return cached

    if source != "alpaca":
        # Alpaca's own calls are not counted against this budget, as the /bars route's are not.
        try:
            market_display.check_budget_or_raise(1)
        except market_display.MarketBudgetExhausted as exc:
            DISPLAY_MONITOR_COUNTERS["market_daily_budget_spent"] += 1
            _note_route_outcome("history", False)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    left_out = None
    join = None
    fill_error = None
    try:
        if store_file is not None:
            store_rows, left_out = await asyncio.to_thread(market_display.read_eodhd_daily, store_file, "max")
            raw, join, fill_error, alpaca_left = await _continue_on_alpaca(series_id, store_rows)
            if join is not None:
                for reason, count in alpaca_left.items():
                    left_out[reason] += count
                data_source = "EODHD, then Alpaca"
            else:
                data_source = "EODHD"
            raw = market_display._cut_to_period(raw, period)
        elif source == "alpaca":
            days = (market_display.ALPACA_MAX_DAYS if period.strip().lower() == "max"
                    else market_display._period_to_days(period))
            window_start = datetime.now(timezone.utc) - timedelta(days=days)
            alpaca_bars = await _fetch_bars_from_alpaca(
                series_id, "1Day", window_start, adjustment="all" if lookup else "raw")
            raw = [_alpaca_bar_as_daily_row(bar) for bar in alpaca_bars]
            data_source = "Alpaca"
            if lookup:
                dated = [row for row in raw if row["date"] is not None]
                DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += len(raw) - len(dated)
                raw, left_out = await asyncio.to_thread(market_display.clean_rows, dated)
        else:
            raw = await market_display.fetch_daily_bars(source, series_id, period)
            data_source = source
    except (market_display.MarketUpstreamError, DisplayUpstreamError) as exc:
        DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
        _note_route_outcome("history", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    bars: list[dict] = []
    for raw_row in raw:
        try:
            date_str = raw_row["date"]
            epoch_seconds = int(
                datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            )
            bars.append(
                {
                    "time": date_str,
                    "epoch_seconds": epoch_seconds,
                    "open": float(raw_row["open"]),
                    "high": float(raw_row["high"]),
                    "low": float(raw_row["low"]),
                    "close": float(raw_row["close"]),
                    "volume": float(raw_row["volume"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            # One unreadable row never sinks the whole chart: skip it, count the drift, keep going.
            DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
            continue

    payload = {
        "symbol": symbol_u,
        "period": period,
        "bars": bars,
        # Named explicitly so a caller (OP) never has to guess: "time" is an ISO calendar date
        # string, "epoch_seconds" is the same instant as a UTC unix timestamp for an intraday-aware
        # chart, matching how DataLibrary.tsx already reads get_bars' own epoch_seconds field above.
        "time_form": "iso_date_string_plus_epoch_seconds",
    }
    if lookup:
        payload["source"] = data_source
        payload["last_day"] = bars[-1]["time"] if bars else None
        payload["left_out"] = left_out
    if store_file is not None:
        payload["join"] = join
        payload["fill_error"] = fill_error
    if fill_error is not None:
        # A join that failed is not remembered: the next request asks Alpaca again.
        _note_route_outcome("history", False)
        return payload
    market_display.cache_set(cache_route, cache_key, payload)
    _note_route_outcome("history", True)
    return payload


async def _continue_on_alpaca(series_id: str, store_rows: "list[dict]") -> tuple:
    """The store's bars continued on Alpaca after the store's last day D: Alpaca 1Day, adjustment all,
    from D minus 14 days to now, cleaned from D on (bars before D are never used, so never counted).
    Returns (bars, join, fill_error, alpaca_left_out). Any reason the join cannot be made (no keys,
    Alpaca refusing or unreachable, no usable bar on D) gives the store's bars alone, join None and the
    reason in plain words in fill_error; it never raises and never fakes a join."""
    no_counts: dict = {}  # nothing was joined, so no Alpaca count is added to the answer
    if not store_rows:
        rows, join, reason = market_display.join_scaled(store_rows, [])
        return rows, join, reason, no_counts
    try:
        market_display.require_source_keys("alpaca")
    except market_display.MarketKeyAbsent as exc:
        DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
        return store_rows, None, str(exc), no_counts
    day = store_rows[-1]["date"]
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(days=14)
    try:
        alpaca_bars = await _fetch_bars_from_alpaca(series_id, "1Day", start, adjustment="all")
    except DisplayUpstreamError as exc:
        DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
        return store_rows, None, str(exc), no_counts
    rows = [_alpaca_bar_as_daily_row(bar) for bar in alpaca_bars]
    dated = [row for row in rows if row["date"] is not None and row["date"] >= day]
    DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += sum(1 for row in rows if row["date"] is None)
    clean, alpaca_left = await asyncio.to_thread(market_display.clean_rows, dated)
    joined, join, reason = market_display.join_scaled(store_rows, clean)
    return joined, join, reason, alpaca_left


async def _hourly_history(symbol_u: str, period: str, series_id: str) -> dict:
    """Lane LK1b: Alpaca's hourly bars (1Hour, adjustment all, from now minus 7 days, pre-market and
    after-hours included) for a "<T>.US" ticker, period 1d (the newest New York date that has a regular
    bar, and every bar after it) or 1w (all). A bar's day is its New York date; "regular" is true when
    it starts 09 to 15 New York time. Cleaned by market_display.clean_hourly, never by the daily check."""
    try:
        market_display.require_source_keys("alpaca")
    except market_display.MarketKeyAbsent as exc:
        DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
        _note_route_outcome("history", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    cache_key = f"hour:{symbol_u}:{period}"
    cached = market_display.cache_get("history", cache_key)
    if cached is not None:
        return cached
    start = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        alpaca_bars = await _fetch_bars_from_alpaca(series_id, "1Hour", start, adjustment="all")
    except DisplayUpstreamError as exc:
        DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
        _note_route_outcome("history", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rows = []
    for raw in alpaca_bars:
        try:
            stamp = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("no offset")
        except (KeyError, TypeError, ValueError):
            DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
            continue
        stamp = stamp.astimezone(timezone.utc)
        rows.append({"time": stamp, "open": raw.get("o"), "high": raw.get("h"), "low": raw.get("l"),
                     "close": raw.get("c"), "volume": raw.get("v")})
    clean, left_out = market_display.clean_hourly(rows)
    clean.sort(key=lambda row: row["time"])
    with_day = []
    for row in clean:
        local = row["time"].astimezone(_NEW_YORK)
        with_day.append((local.date().isoformat(), 9 <= local.hour <= 15, row))
    regular_days = [day for day, regular, _ in with_day if regular]
    cut_day = max(regular_days) if regular_days else None
    if period.strip().lower() == "1d":
        with_day = [] if cut_day is None else [item for item in with_day if item[0] >= cut_day]
    bars = [{
        "time": row["time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "epoch_seconds": int(row["time"].timestamp()),
        "open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]),
        "close": float(row["close"]), "volume": float(row["volume"]),
        "regular": regular,
    } for _, regular, row in with_day]
    payload = {
        "symbol": symbol_u,
        "period": period,
        "bars": bars,
        "time_form": "utc_hour",
        "session": {"zone": "America/New_York", "open": "09:30", "close": "16:00"},
        "source": "Alpaca",
        "last_day": with_day[-1][0] if with_day else None,
        "left_out": left_out,
    }
    market_display.cache_set("history", cache_key, payload)
    _note_route_outcome("history", True)
    return payload


@router.get("/yield-curve")
async def get_yield_curve() -> dict:
    """AC-3: each tenor with its level AND its change in basis points against the previous
    observation, plus the observation dates both figures came from. Lane MKT1: read from FRED,
    one call per tenor in market_display.YIELD_TENORS."""
    try:
        market_display.require_source_keys("fred")
    except market_display.MarketKeyAbsent as exc:
        DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
        _note_route_outcome("yield_curve", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cached = market_display.cache_get("yield_curve", "_")
    if cached is not None:
        return cached

    try:
        market_display.check_budget_or_raise(len(market_display.YIELD_TENORS))
    except market_display.MarketBudgetExhausted as exc:
        DISPLAY_MONITOR_COUNTERS["market_daily_budget_spent"] += 1
        _note_route_outcome("yield_curve", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        raw = await market_display.fetch_yield_curve_fred()
    except market_display.MarketUpstreamError as exc:
        DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
        _note_route_outcome("yield_curve", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    required = {"tenor", "level", "change_bps", "observation_date", "previous_observation_date"}
    tenors: list[dict] = []
    for raw_row in raw:
        if not required.issubset(raw_row.keys()) or any(raw_row.get(k) is None for k in required):
            # A row missing a field ruling 7 requires: skip and count, never fabricate the gap.
            DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
            continue
        tenors.append({k: raw_row[k] for k in required})

    payload = {"tenors": tenors}
    market_display.cache_set("yield_curve", "_", payload)
    _note_route_outcome("yield_curve", True)
    return payload


@router.get("/commodities")
async def get_commodities() -> dict:
    """AC-4: each commodity with level, change and its OWN grain (daily or monthly), so the page
    can say which a figure is rather than imply a monthly print is today's price. Lane MKT1: read
    from FMP, one call per commodity in market_display.COMMODITY_SERIES, all daily. One commodity
    that fails fails the whole panel (503), naming it, rather than showing a panel with a gap."""
    try:
        market_display.require_source_keys("fmp")
    except market_display.MarketKeyAbsent as exc:
        DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
        _note_route_outcome("commodities", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cached = market_display.cache_get("commodities", "_")
    if cached is not None:
        return cached

    # One call per configured commodity, reserved up front so a partially-spent budget never
    # leaves this panel half-fetched and half-refused. The fetcher charges each call as it goes.
    try:
        market_display.check_budget_or_raise(len(market_display.COMMODITY_SERIES))
    except market_display.MarketBudgetExhausted as exc:
        DISPLAY_MONITOR_COUNTERS["market_daily_budget_spent"] += 1
        _note_route_outcome("commodities", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        raw = await market_display.fetch_commodities_fmp()
    except market_display.MarketUpstreamError as exc:
        DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
        _note_route_outcome("commodities", False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    required = {"name", "level", "change", "grain", "observation_date"}
    commodities: list[dict] = []
    for raw_row in raw:
        if not required.issubset(raw_row.keys()) or raw_row.get("grain") is None:
            DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
            continue
        row = {k: raw_row[k] for k in required}
        # The unit is None: FMP sends none for these series, an honest absence rather than a
        # fabricated one.
        row["unit"] = raw_row.get("unit")
        commodities.append(row)

    payload = {"commodities": commodities}
    market_display.cache_set("commodities", "_", payload)
    _note_route_outcome("commodities", True)
    return payload


@router.get("/search")
async def get_search(q: str = Query(..., min_length=1)) -> dict:
    """Lane LK1: a company name or ticker looked up in the SEC name list held in events.db
    (research_cik_ticker_map). No outbound call, no key, no budget: the list is a local table the
    weekly refresh keeps current. Each hit is {symbol "<TICKER>.US", ticker, name, cik}. Lane LK2s: only
    when nothing starts with the text and 4 or more characters were typed, the closest rows answer instead,
    each with "closest": true (display_methods.md, "Search by name or ticker", bullet "Closest matches")."""
    term = q.strip()
    if not term:
        raise HTTPException(status_code=422, detail="Type a company name or a ticker to search for.")

    def _search() -> tuple[list[dict], bool]:
        with eventsdb.get_events_conn() as conn:
            found = event_cik_ticker_map.search(conn, term, _SEARCH_LIMIT)
            if found:
                return found, False
            return event_cik_ticker_map.closest(conn, term, _SEARCH_LIMIT), True

    rows, is_closest = await asyncio.to_thread(_search)
    results = []
    for row in rows:
        item = {"symbol": f"{row['ticker']}.US", "ticker": row["ticker"], "name": row["name"], "cik": row["cik"]}
        if is_closest:
            item["closest"] = True
        results.append(item)
    return {"results": results}


def _whole_word_pattern(word: str, *, ignore_case: bool) -> "re.Pattern[str]":
    """A pattern for ``word`` neither preceded nor followed by a letter or digit (lane LK1d)."""
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
        re.IGNORECASE if ignore_case else 0,
    )


@router.get("/news")
async def get_news(
    ticker: str = Query(...),
    days: int = Query(default=30, ge=1, le=90),
) -> dict:
    """Lane LK1: a ticker's recent headlines from Alpaca's news feed (Benzinga is its publisher), up to
    fifty asked for, newest first, one page; only the articles whose headline or summary name the ticker or
    the company are kept, and the answer says how many were left out (lane LK1d). Nothing is stored; the answer is cached ten minutes per ticker and
    window. The ticker must be capital letters, digits, dots and dashes, one to ten of them."""
    if not _NEWS_TICKER_SHAPE.fullmatch(ticker):
        raise HTTPException(
            status_code=422,
            detail="A ticker is one to ten capital letters, digits, dots or dashes.",
        )
    cache_key = f"{ticker}:{days}"
    cached = market_display.cache_get("news", cache_key)
    if cached is not None:
        return cached
    try:
        api_key, secret_key = _read_credentials()
    except DisplayUpstreamError as exc:
        DISPLAY_MONITOR_COUNTERS["market_key_absent"] += 1
        raise HTTPException(
            status_code=503,
            detail="ALPACA_API_KEY and ALPACA_SECRET_KEY are missing on this server, "
                   "so the lookup cannot read news from Alpaca.",
        ) from exc

    start = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    news_client = AlpacaNewsClient(key_id=api_key, secret_key=secret_key)
    try:
        articles, _next_page = await asyncio.to_thread(
            news_client.get_news, symbols=ticker, start=start, sort="desc", limit=50
        )
    except _NEWS_CLIENT_ERRORS as exc:
        DISPLAY_MONITOR_COUNTERS["market_shape_changed"] += 1
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _company_name() -> str | None:
        with eventsdb.get_events_conn() as conn:
            return event_cik_ticker_map.short_name(event_cik_ticker_map.name_for_ticker(conn, ticker))

    company = await asyncio.to_thread(_company_name)
    ticker_pattern = _whole_word_pattern(ticker, ignore_case=False)
    company_pattern = _whole_word_pattern(company, ignore_case=True) if company else None
    kept = []
    for article in articles:
        text = (article.get("headline") or "") + " " + (article.get("summary") or "")
        if ticker_pattern.search(text) or (company_pattern is not None and company_pattern.search(text)):
            kept.append(article)

    payload = {
        "ticker": ticker,
        "source": "Benzinga, through Alpaca",
        "company_name": company,
        "left_out": len(articles) - len(kept),
        "news": [
            {
                "time": article.get("created_at"),
                "headline": article.get("headline"),
                "url": article.get("url"),
                "publisher": article.get("source"),
                "summary": article.get("summary") or "",
            }
            for article in kept
        ],
    }
    market_display.cache_set("news", cache_key, payload)
    return payload


@router.get("/market-news")
async def get_market_news(limit: int = Query(default=20, le=100)) -> dict:
    """AC-6: projects research_events rows of type 'news' to {time, ticker, kind, headline}.
    Reads the SAME rows Event Ops reads (events/feeds.py's own summarize_event_feeds), through the
    same eventsdb.get_events_conn() helper, and makes NO upstream call: news already lives in the
    events store this dashboard already fetches into, so asking EODHD again would be a second,
    drifting pipeline for data this codebase already has."""
    with eventsdb.get_events_conn() as conn:
        rows = conn.execute(
            "SELECT knowledge_at, payload FROM research_events "
            "WHERE status = 'active' AND event_type = 'news' "
            "ORDER BY knowledge_at DESC, event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    news: list[dict] = []
    for r in rows:
        payload = json.loads(r["payload"])
        symbols = payload.get("symbols") or []
        news.append(
            {
                "time": r["knowledge_at"],
                "ticker": symbols[0] if symbols else None,
                # "kind" is the payload's own news_source (e.g. "benzinga"), never the store's
                # ingest-pipeline "source" column (e.g. "alpaca_news"), which names WHO fetched the
                # article rather than who published it -- the distinction the page actually wants.
                "kind": payload.get("news_source"),
                "headline": payload.get("headline", ""),
            }
        )
    return {"news": news}


@router.get("/market-session")
async def get_market_session() -> dict:
    """AC-7: derived from the newest quote timestamp this process has actually seen, never an
    invented holiday calendar. Cached like every other route here (sheet item 4's own 60 second
    session TTL), though the underlying state (_NEWEST_QUOTE_STAMP) changes only when a quotes
    call runs, so this cache mainly protects against a page hammering this endpoint faster than
    the value it reads could plausibly have changed."""
    cached = market_display.cache_get("session", "_")
    if cached is not None:
        return cached
    stamp = _NEWEST_QUOTE_STAMP["iso"]
    payload = {"newest_quote_stamp": stamp, "session": _derive_session_word(stamp)}
    market_display.cache_set("session", "_", payload)
    return payload
