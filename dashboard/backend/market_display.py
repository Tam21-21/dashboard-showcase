"""Market-data clients, cache and daily call budget for the display-only market routes in
routes/display.py. Plain-English: this file knows HOW to ask for the Markets page's numbers, how
long to remember an answer before asking again, and how to stop asking once today's call allowance
is spent. It never writes to a database, and no research code reads anything from it, matching
routes/display.py's own boundary: display data (tape quotes, news, the calendar) never enters a
research path (root CLAUDE.md, "Parameter provenance and data boundaries"). The one thing it reads
from research_lab is the price check's own rules (universe/data_check.py, used by clean_daily) and
the store's folder (worker/store_root.py): a read in the other direction, nothing flows back.

Where each number comes from (lane MKT1, Tamer's ruling of 2026-11-14: each value from its proper
source, Yahoo the last resort; docs/handoffs/2026-11-14_markets_data_source.md):
  - daily history of five world indexes and the commodities: FMP (Financial Modeling Prep);
  - US Treasury yields: FRED (the St. Louis Federal Reserve's data service);
  - daily history of five more indexes and the dollar index: Yahoo, the last resort;
  - daily history of US funds: Alpaca, fetched by routes/display.py's own bars code;
  - live quotes: still EODHD, which has refused data calls since 2026-11-12 and is out of scope;
  - the ticker search (lane LK1): the SEC name list in events.db, read by routes/display.py, no call.
DISPLAY_SERIES, YIELD_TENORS and COMMODITY_SERIES below are the whole list; nothing else is read.

Built to the exact shape routes/display.py's own Alpaca code already uses (night plan
2026-09-21 set 14 section 6): a plain function reading a credential from the process environment
and raising a named error on absence, an in-process TTL cache keyed by monotonic time (never
wall-clock, which can jump backward on an NTP correction), and a request that fails loud with a
plain-English reason rather than ever fabricating a value.

The daily budget follows research_lab/fetcher/client.py's CostLedger shape (used - a running
count of calls spent today, cap - the ceiling, refuse rather than queue once used would exceed
cap) but is deliberately NOT that class: CostLedger is seeded from EODHD's own live /user counter
because the fetcher shares the real daily quota across many long-running processes, while this
budget is this one FastAPI process's own guess at how many calls a browser tab could cost across a
day of page polling (AC-9's own arithmetic: a 60 second poll bounds the dashboard near three
thousand calls a day). Reusing CostLedger would have made this process's budget silently share
state with a class built for a different lifetime, so a small dedicated pair of module-level
counters is the honest shape here.
"""
from __future__ import annotations

# datetime/timedelta/timezone: convert unix-second timestamps into ISO-8601 strings a human (and
# routes/display.py's own session-freshness logic) can read directly, and cut a daily history to its
# period.
from datetime import datetime, timedelta, timezone
# logging: one warning line per upstream failure class, never a credential value.
import logging
# os: read each source's key straight from the process environment, exactly as routes/display.py's
# own _read_credentials reads the Alpaca pair.
import os
# time: the monotonic clock backing every cache entry's age check (see the module docstring).
import time
# urllib.parse: percent-encode a Yahoo symbol such as "^FCHI" for the chart address.
import urllib.parse

# re: the shape of a US ticker (series_for) and of an EODHD exchange code.
import re

# httpx: the one external call this module makes. Imported as a plain module attribute (never
# "from httpx import AsyncClient") so a probe can monkeypatch market_display.httpx.AsyncClient to a
# fake and every call site below picks up the fake automatically.
import httpx

log = logging.getLogger("dashboard.market_display")

# The common base for every EODHD REST endpoint this module calls, matching
# research_lab/fetcher/config.py's own BASE constant so both halves of this codebase agree on the
# provider's own root.
_EODHD_BASE = "https://eodhd.com/api"

# AC-1's own measured trap: EODHD's batch real-time endpoint accepts at most this many symbols per
# call, and a single bad symbol fails the WHOLE call (a measured 404 on a 9-symbol batch carrying
# one bad index ticker). The quotes route in routes/display.py chunks requested symbols into groups
# of this size so a page asking for ~25 symbols never sends one oversized call EODHD would refuse
# outright.
QUOTE_CHUNK_SIZE = 20

# Sheet item 4's named per-route cache lifetimes (seconds), pinned by name and value (AC-9): a
# cached response is served without a new upstream call until its own route's TTL has passed.
# "news" (lane LK1) carries no sheet-item-4 default: ten minutes is an operational dial (root
# CLAUDE.md #204: an inline reason is enough), because headlines arrive a few an hour for one ticker
# and Alpaca's own rate limit is the thing a lookup window re-run on every keystroke could hit.
# Lane MKT1: "history" is now the Alpaca fund history only (as the /bars route caches it). The
# daily history FMP and Yahoo serve is "history_daily", twelve hours like the yield curve and the
# commodities: these sources publish one close a day, and neither FMP's call limit on our plan nor
# how long Yahoo keeps answering a plain script is known (hand-off 2026-11-14), so each series is
# asked for twice a day at most. An operational dial (#204), not a backtest parameter.
CACHE_TTL_SECONDS: dict[str, float] = {
    "quotes": 60.0,
    "history": 600.0,
    "history_daily": 12 * 3600.0,
    "yield_curve": 12 * 3600.0,
    "commodities": 12 * 3600.0,
    "session": 60.0,
    "news": 600.0,
}

# The daily call budget (AC-9): a running count of upstream calls spent today (module-level so it
# survives across requests within one process) and the ceiling it refuses at. Reset on every
# process restart, same as every other in-process counter in this codebase (routes/ops.py's own
# DISPLAY_MONITOR_COUNTERS docstring names the same lifecycle).
_BUDGET_USED = 0
_BUDGET_CAP = 5000  # AC-9's own named default: "a daily budget of 5000 calls"

# The in-memory cache: one entry per "{route}:{key}" string (e.g. "quotes:AAPL",
# "yield_curve:_" for a route with no per-request key), holding the last successful payload and
# the monotonic instant it was cached. A process restart clears it; nothing here is durable.
_cache: dict[str, dict] = {}


def reset_cache_for_probe() -> None:
    """Test-only reset: clears every cached entry so a probe's own scenario starts from a known
    empty cache regardless of what an earlier scenario in the same process left behind."""
    _cache.clear()


def reset_budget_for_probe(cap: int = 5000) -> None:
    """Test-only reset: zeroes the spent counter and sets the cap, so a probe can force a spent
    budget (cap=0) or restore the real default without waiting for a process restart."""
    global _BUDGET_USED, _BUDGET_CAP
    _BUDGET_USED = 0
    _BUDGET_CAP = cap


class MarketKeyAbsent(RuntimeError):
    """A key a source needs (EODHD_API_KEY, FMP_API_KEY, FRED_KEY, ALPACA_API_KEY or
    ALPACA_SECRET_KEY) is missing from the process environment. The message names only the
    absence, matching routes/display.py's own DisplayUpstreamError contract: never the value."""


class MarketUpstreamError(RuntimeError):
    """A source refused a request, sent back something this module could not read, or answered in
    a shape this module did not expect. Names the source and the series; never carries the API
    key, a bare status code standing in for an explanation, or a raw URL."""


class MarketSymbolUnknown(RuntimeError):
    """The page asked for a symbol that is not in DISPLAY_SERIES and is not a US ticker. Names the
    symbol. No source is called for it and nothing else is tried in its place."""


class MarketBudgetExhausted(RuntimeError):
    """Today's shared daily call budget is already spent. Raised BEFORE any upstream call is
    attempted, so refusing here always costs zero real calls."""


def _read_credentials() -> str:
    """The EODHD key from the environment, or a named, loud failure. Exactly the shape
    routes/display.py's own _read_credentials uses for the Alpaca pair: read straight from
    os.environ, never logged, never echoed back in an exception message."""
    api_key = os.environ.get("EODHD_API_KEY", "")
    if not api_key:
        raise MarketKeyAbsent(
            "EODHD is not configured on this server: the market data key is missing."
        )
    return api_key


def check_budget_or_raise(calls_needed: int = 1) -> None:
    """Raise MarketBudgetExhausted when spending `calls_needed` more calls would exceed the daily
    cap. Called BEFORE a chunked route commits to any of its upstream calls, so a request that
    cannot be fully served never partially spends the budget and then fails anyway."""
    if _BUDGET_USED + calls_needed > _BUDGET_CAP:
        raise MarketBudgetExhausted(
            f"Today's market data budget ({_BUDGET_CAP} calls) is spent; refusing further market "
            f"data requests until it resets."
        )


def charge_budget(calls: int = 1) -> None:
    """Account `calls` real upstream calls against the running daily counter, after a call is
    actually issued (never before, so a call that never happened is never charged)."""
    global _BUDGET_USED
    _BUDGET_USED += calls


def cache_get(route: str, key: str):
    """Return the cached payload for (route, key) if one exists and is still within that route's
    own TTL, else None. The TTL lookup uses `route` as the key into CACHE_TTL_SECONDS, so a typo'd
    route name fails loud (KeyError) rather than silently never expiring."""
    entry = _cache.get(f"{route}:{key}")
    if entry is None:
        return None
    ttl = CACHE_TTL_SECONDS[route]
    if (time.monotonic() - entry["cached_at_monotonic"]) >= ttl:
        return None
    return entry["payload"]


def cache_set(route: str, key: str, payload) -> None:
    """Store `payload` under "{route}:{key}" with the current monotonic instant, so the next
    cache_get for the same pair can measure its own age."""
    _cache[f"{route}:{key}"] = {"payload": payload, "cached_at_monotonic": time.monotonic()}


def epoch_to_iso(ts) -> "str | None":
    """A unix-second timestamp -> an ISO-8601 UTC string, or None when ts itself is absent. Used
    for every quote row's OWN timestamp (AC-1: "never a page-level as of")."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


_EXCHANGE_CODE_SHAPE = re.compile(r"[A-Z0-9]{2,}")


def _eodhd_symbol_code(symbol: str) -> str:
    """Best-effort EODHD exchange suffix for a bare ticker the page passes in (e.g. "AAPL" ->
    "AAPL.US"). Equities and funds get .US, the only exchange research_lab/fetcher/config.py's own
    EXCHANGE constant targets; a symbol carrying a dash ("BTC-USD") is read as a crypto pair and
    gets .CC, the suffix this suite's own fixtures use for crypto rows (e.g. "BTC-USD.CC" in
    scripts/probes/display_market/p07_impl.py); a bare six-letter alphabetic symbol with no dash
    ("EURUSD") is read as an FX pair and gets .FOREX, EODHD's own documented FX exchange code.
    UNVERIFIED beyond what this suite's own live probe (p12, AC-11) checks for plain US equities: a
    live FX or crypto quote request through this heuristic needs its own verification pass before
    this dashboard leans on it for anything beyond display, which it never does (the data-boundary
    law keeps display data out of every research and money path)."""
    if "." in symbol and _EXCHANGE_CODE_SHAPE.fullmatch(symbol.rpartition(".")[2]):
        return symbol  # already carries its EODHD exchange code, e.g. "GSPC.INDX"
    if "-" in symbol:
        return f"{symbol}.CC"
    if symbol.isalpha() and len(symbol) == 6:
        return f"{symbol}.FOREX"
    return f"{symbol}.US"


async def fetch_quotes_chunk(symbols: "list[str]") -> "tuple[bool, list | None]":
    """ONE upstream GET for up to QUOTE_CHUNK_SIZE symbols against EODHD's real-time batch
    endpoint (verified live by this suite's own probe-writer: scripts/probes/display_market/
    _market_route_lib.py's eodhd_quote_row docstring names "a GET to /api/real-time/<primary>?s=
    <rest>"). Returns (True, raw_rows) on a 200 with a parseable list or dict, or (False, None) on
    ANY failure -- a non-200 status, a network error, or an unparseable body -- so the caller can
    isolate a whole chunk's own failure (AC-1) without this function ever raising for a routine
    upstream refusal. Reads the key itself (never takes it as a parameter) so this function works
    identically whether called from the route, which already checked the key is present, or from
    the live probe (p12), which calls it directly."""
    api_key = _read_credentials()
    codes = [_eodhd_symbol_code(s) for s in symbols]
    primary, rest = codes[0], codes[1:]
    params = {"api_token": api_key, "fmt": "json"}
    if rest:
        params["s"] = ",".join(rest)
    url = f"{_EODHD_BASE}/real-time/{primary}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
    except httpx.RequestError:
        log.warning("market quotes chunk fetch failed (network): symbol_count=%d", len(symbols))
        return False, None
    if resp.status_code != 200:
        log.warning(
            "market quotes chunk refused: symbol_count=%d status=%d", len(symbols), resp.status_code
        )
        return False, None
    try:
        body = resp.json()
    except ValueError:
        log.warning("market quotes chunk response unreadable: symbol_count=%d", len(symbols))
        return False, None
    if isinstance(body, dict):
        body = [body]  # EODHD's real-time endpoint answers a bare object for a single-symbol call
    if not isinstance(body, list):
        return False, None
    return True, body


def _period_to_days(period: str) -> int:
    """A light, best-effort parse of this route's own `period` query text (e.g. "1y", "90d") into
    a day count, used only to bound how far back a daily history reaches. Never raises: an
    unparseable period falls back to one year, a reasonable default for a chart rather than a
    refusal over a display convenience parameter."""
    text = (period or "").strip().lower()
    if len(text) >= 2 and text[:-1].isdigit():
        n, unit = int(text[:-1]), text[-1]
        if unit == "d":
            return n
        if unit == "w":
            return n * 7
        if unit == "m":
            return n * 30
        if unit == "y":
            return n * 365
    return 365


# ---------------------------------------------------------------------------------------------
# Lane MKT1: where the Markets page's numbers come from (see the module docstring).
# ---------------------------------------------------------------------------------------------

# The only symbols /api/display/history serves besides a US ticker ending in ".US". The page's own
# names (EODHD-style codes such as "GSPC.INDX", kept so the frontend does not change) on the left;
# on the right the source that answers for it and the id that source knows it by. FMP answered each
# of its five in the live test of 2026-11-14 (docs/handoffs/2026-11-14_markets_data_source.md). In
# the same test FMP refused the CAC 40, DAX, Shanghai and the dollar index on our plan (402) and
# the Nasdaq 100 too; Yahoo, the last resort, serves those five (lane plan MKT1, step 2: the Nasdaq
# 100 is also on FRED, one day late, and the planning session chose Yahoo). Alpaca serves the US
# funds.
DISPLAY_SERIES: dict[str, tuple[str, str]] = {
    "GSPC.INDX": ("fmp", "^GSPC"),
    "STOXX50E.INDX": ("fmp", "^STOXX50E"),
    "N225.INDX": ("fmp", "^N225"),
    "HSI.INDX": ("fmp", "^HSI"),
    "VIX.INDX": ("fmp", "^VIX"),
    "NDX.INDX": ("yahoo", "^NDX"),
    "FCHI.INDX": ("yahoo", "^FCHI"),
    "GDAXI.INDX": ("yahoo", "^GDAXI"),
    "000001.SHG": ("yahoo", "000001.SS"),
    "DXY.INDX": ("yahoo", "DX-Y.NYB"),
    "XLK": ("alpaca", "XLK"),
    "XLI": ("alpaca", "XLI"),
    "XLF": ("alpaca", "XLF"),
    "XLV": ("alpaca", "XLV"),
    "XLE": ("alpaca", "XLE"),
    "XLY": ("alpaca", "XLY"),
    "XLP": ("alpaca", "XLP"),
    "XLRE": ("alpaca", "XLRE"),
    "XLC": ("alpaca", "XLC"),
    "XLU": ("alpaca", "XLU"),
    "XLB": ("alpaca", "XLB"),
    "IWM": ("alpaca", "IWM"),
    "SPY": ("alpaca", "SPY"),
    "EEM": ("alpaca", "EEM"),
    "EFA": ("alpaca", "EFA"),
    "HYG": ("alpaca", "HYG"),
    "IEF": ("alpaca", "IEF"),
}

# The four Treasury yields the page shows, as (the page's tenor label, FRED's series id), in the
# order the page draws them. No other maturity is read.
YIELD_TENORS: tuple[tuple[str, str], ...] = (
    ("3M", "DGS3MO"),
    ("2Y", "DGS2"),
    ("10Y", "DGS10"),
    ("30Y", "DGS30"),
)

# The two commodities the page shows, as (the page's name, FMP's symbol), in drawing order. No
# other commodity is read.
COMMODITY_SERIES: tuple[tuple[str, str], ...] = (
    ("Brent crude oil", "BZUSD"),
    ("Gold", "GCUSD"),
)

# A US ticker the Events tab and the search charts ask for ("AAPL.US") goes to Alpaca. The shape
# check is only a guard on what is put into Alpaca's address (letters, digits, dot, dash); it does
# not say a ticker exists, Alpaca answers that.
_US_TICKER_SHAPE = re.compile(r"[A-Z0-9][A-Z0-9.\-]*")

# What each source needs from the environment, and how the page's refusal words it. Yahoo needs no
# key. EODHD is not here: quotes and search keep their own _read_credentials.
_SOURCE_KEY_NAMES: dict[str, tuple[str, ...]] = {
    "fmp": ("FMP_API_KEY",),
    "fred": ("FRED_KEY",),
    "alpaca": ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
    "yahoo": (),
}
_SOURCE_PURPOSE: dict[str, str] = {
    "fmp": "so the Markets page cannot read prices from Financial Modeling Prep.",
    "fred": "so the Markets page cannot read the yield curve from FRED.",
    "alpaca": "so the Markets page cannot read fund prices from Alpaca.",
}

_FMP_PRICES_URL = "https://financialmodelingprep.com/stable/historical-price-eod/full"
_FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{series_id}"
# Yahoo answered an honest, named script in the live test of 2026-11-14 (13 calls, all 200).
_YAHOO_USER_AGENT = "trading-dashboard/1.0 (private; daily closes)"


def series_for(symbol: str) -> "tuple[str, str]":
    """The (source, series id) that answers for one of the page's symbols. A table entry first; else
    a US ticker written "<TICKER>.US" goes to Alpaca as that ticker; else MarketSymbolUnknown names
    the symbol. Nothing is guessed and no other source is tried."""
    entry = DISPLAY_SERIES.get(symbol)
    if entry is not None:
        return entry
    if symbol.endswith(".US") and _US_TICKER_SHAPE.fullmatch(symbol[: -len(".US")]):
        return ("alpaca", symbol[: -len(".US")])
    raise MarketSymbolUnknown(
        f"{symbol} is not a symbol the Markets page can read prices for."
    )


def require_source_keys(source: str) -> None:
    """Raise MarketKeyAbsent naming the first key `source` needs that is missing, or return when
    all are there. Called by the routes before any cache lookup or call."""
    try:
        names = _SOURCE_KEY_NAMES[source]
    except KeyError as exc:
        raise ValueError(f"No market data source is called {source!r}.") from exc
    for name in names:
        if not os.environ.get(name, ""):
            raise MarketKeyAbsent(f"{name} is missing on this server, {_SOURCE_PURPOSE[source]}")


def _source_key(source: str, name: str) -> str:
    """The key `name` of `source`, after the same check the routes make (so a fetcher called on its
    own fails the same named way). Never logged, never in a message."""
    require_source_keys(source)
    return os.environ[name]


async def _get_json(url: str, params: dict, headers: "dict | None", source_name: str, series_label: str):
    """ONE GET, charged to the daily budget once it was issued (a call that never left is never
    charged). Returns the parsed JSON body, or raises MarketUpstreamError naming the source and the
    series, never the key and never the URL."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.RequestError as exc:
        charge_budget(1)
        log.warning("market fetch failed (network): source=%s series=%s", source_name, series_label)
        raise MarketUpstreamError(f"Could not reach {source_name} to fetch {series_label}.") from exc
    charge_budget(1)
    if resp.status_code != 200:
        log.warning(
            "market fetch refused: source=%s series=%s status=%d", source_name, series_label, resp.status_code
        )
        raise MarketUpstreamError(f"{source_name} refused the request for {series_label}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise MarketUpstreamError(f"{source_name} sent back something unreadable for {series_label}.") from exc


def _fmp_bars(body, label: str) -> "list[dict]":
    """FMP's answer (a list of {date, open, high, low, close, volume, ...}, newest first) as
    [{date, open, high, low, close, volume}] oldest first. A field FMP leaves out stays None, for the
    route to skip and count, never filled. An answer that is not a non-empty list of dated bars is
    refused."""
    if not isinstance(body, list) or not body:
        raise MarketUpstreamError(f"FMP's answer for {label} was not a list of daily bars.")
    bars = []
    for raw in body:
        if not isinstance(raw, dict) or not isinstance(raw.get("date"), str):
            raise MarketUpstreamError(f"FMP's answer for {label} held a bar this page could not read.")
        bars.append(
            {
                "date": raw["date"],
                "open": raw.get("open"),
                "high": raw.get("high"),
                "low": raw.get("low"),
                "close": raw.get("close"),
                "volume": raw.get("volume"),
            }
        )
    bars.sort(key=lambda bar: bar["date"])
    return bars


def _yahoo_bars(body, label: str) -> "list[dict]":
    """Yahoo's chart answer as [{date, open, high, low, close, volume}] oldest first. The day of a
    bar is the date of its timestamp plus meta.gmtoffset (the exchange's own offset). A bar with a
    null close is left out; another null field stays None for the route to skip and count."""
    try:
        result = body["chart"]["result"][0]
        stamps = result["timestamp"]
        offset = result["meta"]["gmtoffset"]
        quote = result["indicators"]["quote"][0]
        columns = {name: quote[name] for name in ("open", "high", "low", "close", "volume")}
    except (KeyError, IndexError, TypeError) as exc:
        raise MarketUpstreamError(f"Yahoo's answer for {label} was not the daily chart this page reads.") from exc
    readable = (
        isinstance(stamps, list)
        and len(stamps) > 0
        and all(isinstance(stamp, (int, float)) for stamp in stamps)
        and isinstance(offset, (int, float))
        and all(isinstance(column, list) and len(column) == len(stamps) for column in columns.values())
    )
    if not readable:
        raise MarketUpstreamError(f"Yahoo's answer for {label} was not the daily chart this page reads.")
    bars = []
    for i, stamp in enumerate(stamps):
        if columns["close"][i] is None:
            continue
        day = datetime.fromtimestamp(stamp + offset, tz=timezone.utc).date().isoformat()
        bars.append(
            {
                "date": day,
                "open": columns["open"][i],
                "high": columns["high"][i],
                "low": columns["low"][i],
                "close": columns["close"][i],
                "volume": columns["volume"][i],
            }
        )
    bars.sort(key=lambda bar: bar["date"])
    return bars


def _within_period(bars: "list[dict]", period: str) -> "list[dict]":
    """The bars dated no earlier than today minus the period's day count (today in UTC)."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=_period_to_days(period))).isoformat()
    return [bar for bar in bars if bar["date"] >= cutoff]


async def fetch_daily_bars(source: str, series_id: str, period: str) -> "list[dict]":
    """One series' daily bars from FMP or Yahoo: [{date, open, high, low, close, volume}] oldest
    first, within the period. ONE call, charged to the daily budget. Every number is the source's
    own; a bar is never filled. Alpaca's fund history is fetched by routes/display.py's own bars
    code, not here. Yahoo is asked for two years (range 2y), so a longer period reaches no further
    back on a Yahoo series. Raises MarketUpstreamError naming source and series on any failure."""
    if source == "fmp":
        body = await _get_json(
            _FMP_PRICES_URL, {"symbol": series_id, "apikey": _source_key("fmp", "FMP_API_KEY")}, None, "FMP", series_id
        )
        bars = _fmp_bars(body, series_id)
    elif source == "yahoo":
        body = await _get_json(
            _YAHOO_CHART_URL.format(series_id=urllib.parse.quote(series_id)),
            {"range": "2y", "interval": "1d"},
            {"User-Agent": _YAHOO_USER_AGENT},
            "Yahoo",
            series_id,
        )
        bars = _yahoo_bars(body, series_id)
    else:
        raise ValueError(f"fetch_daily_bars does not serve source {source!r}.")
    return _within_period(bars, period)


def _fred_values(body, series_id: str) -> "list[tuple[str, float]]":
    """FRED's observations as [(date, value)] oldest first. FRED writes "." for a day with no value:
    those are skipped, never read as a number."""
    observations = body.get("observations") if isinstance(body, dict) else None
    if not isinstance(observations, list):
        raise MarketUpstreamError(f"FRED's answer for {series_id} was not a list of observations.")
    values = []
    for raw in observations:
        if not isinstance(raw, dict) or not isinstance(raw.get("date"), str) or not isinstance(raw.get("value"), str):
            raise MarketUpstreamError(f"FRED's answer for {series_id} held an observation this page could not read.")
        if raw["value"] == ".":
            continue
        try:
            values.append((raw["date"], float(raw["value"])))
        except ValueError as exc:
            raise MarketUpstreamError(f"FRED's answer for {series_id} held a value this page could not read.") from exc
    values.sort(key=lambda pair: pair[0])
    return values


async def fetch_yield_curve_fred() -> "list[dict]":
    """The four Treasury yields from FRED, one call per tenor in YIELD_TENORS order, each charged to
    the daily budget. Per tenor: its newest value, the change in basis points against the value
    before it (a yield is already in percent, so a difference times 100 is basis points), and both
    dates. A series with fewer than two values fails the whole curve, naming FRED and the series."""
    api_key = _source_key("fred", "FRED_KEY")
    tenors = []
    for tenor, series_id in YIELD_TENORS:
        body = await _get_json(
            _FRED_OBSERVATIONS_URL,
            {"series_id": series_id, "api_key": api_key, "file_type": "json"},
            None,
            "FRED",
            series_id,
        )
        values = _fred_values(body, series_id)
        if len(values) < 2:
            raise MarketUpstreamError(f"FRED had fewer than two values for {series_id}.")
        (previous_date, previous_level), (newest_date, newest_level) = values[-2], values[-1]
        tenors.append(
            {
                "tenor": tenor,
                "level": newest_level,
                "change_bps": round((newest_level - previous_level) * 100, 1),
                "observation_date": newest_date,
                "previous_observation_date": previous_date,
            }
        )
    return tenors


async def fetch_commodities_fmp() -> "list[dict]":
    """The two commodities from FMP, one call each in COMMODITY_SERIES order, each charged to the
    daily budget. Per commodity: its newest close as the level, the newest close minus the close
    before it as the change, grain "daily", no unit (FMP sends none, so none is made up) and the
    newest bar's date. One commodity that fails fails the whole panel, naming it."""
    api_key = _source_key("fmp", "FMP_API_KEY")
    rows = []
    for name, series_id in COMMODITY_SERIES:
        label = f"{name} ({series_id})"
        body = await _get_json(_FMP_PRICES_URL, {"symbol": series_id, "apikey": api_key}, None, "FMP", label)
        closes = [
            bar for bar in _fmp_bars(body, label) if isinstance(bar["close"], (int, float))
        ]
        if len(closes) < 2:
            raise MarketUpstreamError(f"FMP had fewer than two closing prices for {label}.")
        newest, previous = closes[-1], closes[-2]
        rows.append(
            {
                "name": name,
                "level": newest["close"],
                "change": newest["close"] - previous["close"],
                "grain": "daily",
                "unit": None,
                "observation_date": newest["date"],
            }
        )
    return rows


# ---------------------------------------------------------------------------------------------
# Lane LK1: the ticker lookup's price history, cleaned and named.
# ---------------------------------------------------------------------------------------------

# How far back "max" reaches on Alpaca: ten years (3650 days), the farthest the /bars route's own
# 1Day window allows (_TIMEFRAME_WINDOWS["1Day"]["max_start_days_back"] in routes/display.py). An EODHD
# "max" is the whole file instead.
ALPACA_MAX_DAYS = 3650

# The two reasons a bar can be left out besides the price check's own four flags
# (research_lab/universe/data_check.py FLAGS, by their own names). Every answer lists all six, with a
# zero where nothing was left out.
LEFT_OUT_EXTRA_KEYS = ("missing_price", "duplicate_day_disagrees")


def eodhd_store_file(symbol: str):
    """The EODHD store's daily file for "<TICKER>.US" if it exists, else None. The folder comes from
    the worker's store_root.resolve_eodhd_store_root (EODHD_STORE_ROOT, read at call time); a blank
    setting raises ValueError there, and the route turns that into a 503 with its words."""
    from research_lab.worker.store_root import resolve_eodhd_store_root

    path = resolve_eodhd_store_root() / "daily" / f"{symbol}.parquet"
    return path if path.is_file() else None


def clean_daily(frame):
    """Drop the bars that cannot be trusted, and count each reason. Input: a pandas frame with the
    columns date (YYYY-MM-DD text), open, high, low, close, volume and optionally adjusted_close.
    Returns (clean frame, left_out) where left_out always has all six keys:
      1. the price check's four flags (data_check.flag_masks): adjusted_close_at_ceiling,
         non_positive_close, high_below_low, bar_on_closed_day: each flagged row is dropped and counted
         once per flag it carries;
      2. missing_price: a bar with any of its numbers (open, high, low, close, adjusted_close when
         present, volume) empty is dropped;
      3. duplicate_day_disagrees: a date held by several bars that are not all identical loses every
         one of those bars (a day with identical copies keeps one, uncounted: nothing was lost).
    Nothing is repaired or filled. A date the exchange calendar cannot judge raises MarketUpstreamError."""
    from research_lab.universe import data_check

    left_out = {key: 0 for key in (*data_check.FLAGS, *LEFT_OUT_EXTRA_KEYS)}
    work = frame.reset_index(drop=True)
    if len(work) == 0:
        return work, left_out
    try:
        masks = data_check.flag_masks(work)
    except (data_check.DataCheckInputError, ValueError) as exc:
        raise MarketUpstreamError(f"The price check could not read this history: {exc}") from exc
    drop = None
    for flag, mask in masks.items():
        left_out[flag] = int(mask.sum())
        drop = mask if drop is None else (drop | mask)
    work = work[~drop]

    numbers = [c for c in ("open", "high", "low", "close", "adjusted_close", "volume") if c in work.columns]
    missing = work[numbers].isna().any(axis=1)
    left_out["missing_price"] = int(missing.sum())
    work = work[~missing]

    shared = work.duplicated(subset="date", keep=False)
    if shared.any():
        disagreeing = [day for day, group in work[shared].groupby("date") if len(group.drop_duplicates()) > 1]
        lost = work["date"].isin(disagreeing)
        left_out["duplicate_day_disagrees"] = int(lost.sum())
        work = work[~lost].drop_duplicates()
    return work.reset_index(drop=True), left_out


def _cut_to_period(rows: "list[dict]", period: str) -> "list[dict]":
    """Keep the bars no older than the period's day count back from the NEWEST bar in `rows` (not from
    today: a stale file still shows its own last stretch). "max" keeps everything."""
    if (period or "").strip().lower() == "max" or not rows:
        return rows
    newest = datetime.strptime(rows[-1]["date"], "%Y-%m-%d").date()
    cutoff = (newest - timedelta(days=_period_to_days(period))).isoformat()
    return [row for row in rows if row["date"] >= cutoff]


def read_eodhd_daily(path, period: str) -> "tuple[list[dict], dict]":
    """The EODHD store file at `path` as [{date, open, high, low, close, volume}] oldest first, cleaned
    (clean_daily) and THEN back-adjusted the way research_lab/docker/research-runner/fetch.py
    fetch_eodhd does: open, high and low times adjusted_close / close, close = adjusted_close, volume
    as stored. Cut to the period counted back from the file's newest bar; "max" is the whole file.
    Returns (bars, left_out). A file that cannot be read or lacks a column raises MarketUpstreamError."""
    import pandas as pd

    wanted = ["date", "open", "high", "low", "close", "adjusted_close", "volume"]
    try:
        raw = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        raise MarketUpstreamError(f"The stored price file for {path.stem} could not be read.") from exc
    lacking = [c for c in wanted if c not in raw.columns]
    if lacking:
        raise MarketUpstreamError(f"The stored price file for {path.stem} lacks the columns {', '.join(lacking)}.")
    clean, left_out = clean_daily(raw[wanted])
    factor = clean["adjusted_close"] / clean["close"]
    adjusted = pd.DataFrame({
        "date": clean["date"],
        "open": clean["open"] * factor,
        "high": clean["high"] * factor,
        "low": clean["low"] * factor,
        "close": clean["adjusted_close"],
        "volume": clean["volume"],
    }).sort_values("date", kind="stable")
    return _cut_to_period(adjusted.to_dict("records"), period), left_out


def clean_rows(rows: "list[dict]") -> "tuple[list[dict], dict]":
    """The same cleaning for bars that arrived as [{date, open, high, low, close, volume}] (Alpaca's,
    through the route's _alpaca_bar_as_daily_row). A number that is not a number counts as missing.
    Returns (bars oldest first, left_out)."""
    import pandas as pd

    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    clean, left_out = clean_daily(frame)
    return clean.sort_values("date", kind="stable").to_dict("records"), left_out


# ---------------------------------------------------------------------------------------------
# Lane LK1b: the lookup's prices continued on Alpaca after the store's last day, and hourly bars.
# ---------------------------------------------------------------------------------------------

# Every answer for hourly bars lists these four reasons, with a zero where nothing was left out.
HOURLY_LEFT_OUT_KEYS = ("missing_price", "non_positive_close", "high_below_low", "duplicate_time")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def clean_hourly(rows: "list[dict]") -> "tuple[list[dict], dict]":
    """The cleaning for hourly bars, given as [{time, open, high, low, close, volume}] where time is
    the bar's start (any value that is equal when the instant is equal). A bar is left out and
    counted once, under the FIRST reason that fits, in this order: one of its five numbers missing or
    not a number (missing_price); a close of zero or less (non_positive_close); a high below the low
    (high_below_low); a start time an earlier kept bar already has (duplicate_time, the first copy
    stays). Nothing is repaired or filled. The daily check's calendar flags do not apply to an hour.
    Returns (bars in the order given, left_out with all four keys)."""
    left_out = {key: 0 for key in HOURLY_LEFT_OUT_KEYS}
    kept: "list[dict]" = []
    seen = set()
    for row in rows:
        numbers = [row.get(name) for name in ("open", "high", "low", "close", "volume")]
        if not all(_is_number(n) for n in numbers):
            left_out["missing_price"] += 1
        elif row["close"] <= 0:
            left_out["non_positive_close"] += 1
        elif row["high"] < row["low"]:
            left_out["high_below_low"] += 1
        elif row["time"] in seen:
            left_out["duplicate_time"] += 1
        else:
            seen.add(row["time"])
            kept.append(row)
    return kept, left_out


def join_scaled(store_rows: "list[dict]", alpaca_rows: "list[dict]") -> "tuple[list[dict], dict | None, str | None]":
    """Continue the store's daily bars (oldest first, back-adjusted, close = adjusted close) with
    Alpaca's bars (cleaned, oldest first, adjusted for splits and dividends) after the store's last
    day D, with no jump at the join. Alpaca's adjusted prices sit a little under the store's (the two
    adjust for dividends a little differently), so k = Alpaca's close on D / the store's close on D,
    and every store bar's open, high, low and close is multiplied by k (volume unchanged).
    Returns (bars, {"day": D, "scale": k}, None) when joined, else (store_rows unchanged, None, reason
    in plain words): no store bars, no Alpaca bar on D, or a scale that is not a positive number."""
    if not store_rows:
        return store_rows, None, "The stored price file has no usable bars to continue from."
    day = store_rows[-1]["date"]
    on_day = [row for row in alpaca_rows if row["date"] == day]
    if not on_day:
        return store_rows, None, f"Alpaca has no usable bar on {day}, so the stored prices are shown as they are."
    scale = on_day[0]["close"] / store_rows[-1]["close"]
    if not (scale == scale and 0 < scale < float("inf")):
        return store_rows, None, (f"Alpaca's price on {day} could not be matched to the stored price, "
                                  "so the stored prices are shown as they are.")
    scaled = [
        {**row, "open": row["open"] * scale, "high": row["high"] * scale,
         "low": row["low"] * scale, "close": row["close"] * scale}
        for row in store_rows
    ]
    return scaled + [row for row in alpaca_rows if row["date"] > day], {"day": day, "scale": scale}, None


# ---------------------------------------------------------------------------------------------
# Lane LK1c: per-share figures in today's shares (DECISIONS #493).
# ---------------------------------------------------------------------------------------------

_ALPACA_SPLITS_URL = "https://data.alpaca.markets/v1/corporate-actions"
_ALPACA_SPLITS_PAGE_LIMIT = 1000        # Alpaca's own page size ceiling for this feed, measured 2026-11-16
_ALPACA_SPLITS_FIRST_DAY = "2016-01-01"  # where Alpaca's split list begins, measured 2026-11-16 (AAPL, NVDA)
SPLITS_BEFORE_NOTE = "Filed before 2016, where Alpaca's split list begins."


async def fetch_alpaca_splits(symbol: str, start: str, key_id: str, secret_key: str) -> "list[dict]":
    """Every forward and reverse split of `symbol` from `start` (YYYY-MM-DD) to today, from Alpaca's
    corporate-actions feed, as [{"ex_date": "YYYY-MM-DD", "ratio": new_rate / old_rate}] by ex_date.

    One call is checked against the daily budget before the first request; every later page is one more
    charged call. Raises MarketBudgetExhausted, or MarketUpstreamError (a refusal, an unreadable page, a shape
    this function does not know, a page token that repeats). Never returns a partial list.
    """
    check_budget_or_raise(1)
    label = f"{symbol} splits"
    unexpected = f"Alpaca sent back something unexpected for {label}."
    end = datetime.now(timezone.utc).date().isoformat()
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
    splits: list[dict] = []
    seen_tokens: set[str] = set()
    token: "str | None" = None
    while True:
        params = {"symbols": symbol, "types": "forward_split,reverse_split", "start": start, "end": end,
                  "limit": _ALPACA_SPLITS_PAGE_LIMIT}
        if token is not None:
            params["page_token"] = token
        body = await _get_json(_ALPACA_SPLITS_URL, params, headers, "Alpaca", label)
        if not isinstance(body, dict):
            raise MarketUpstreamError(unexpected)
        actions = body.get("corporate_actions")
        if actions is None:
            actions = {}
        if not isinstance(actions, dict):
            raise MarketUpstreamError(unexpected)
        for kind in ("forward_splits", "reverse_splits"):
            rows = actions.get(kind)
            if rows is None:
                continue
            if not isinstance(rows, list):
                raise MarketUpstreamError(unexpected)
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("ex_date"), str):
                    raise MarketUpstreamError(unexpected)
                old, new = row.get("old_rate"), row.get("new_rate")
                if not _is_number(old) or not _is_number(new) or old <= 0 or new <= 0:
                    raise MarketUpstreamError(unexpected)
                splits.append({"ex_date": row["ex_date"], "ratio": new / old})
        token = body.get("next_page_token")
        if token is None:
            break
        if not isinstance(token, str) or token in seen_tokens:
            raise MarketUpstreamError(unexpected)
        seen_tokens.add(token)
    return sorted(splits, key=lambda split: split["ex_date"])


def per_share_in_todays_shares(concepts: "list[dict]", splits: "list[dict]") -> "list[dict]":
    """The history's concept entries with every per-share value (unit "USD/shares") counted in today's shares.

    A value is divided by the ratio of every split whose ex_date is strictly after the value's filing date
    (a figure filed on the ex-date already counts the new shares), and gets "as_filed" with the number the
    company filed when that changes it. A value filed before 2016, where Alpaca's split list begins, stays as
    filed and carries SPLITS_BEFORE_NOTE. Other concepts pass through untouched. Returns new dicts.
    """
    out: list[dict] = []
    for entry in concepts:
        if entry["unit"] != "USD/shares":
            out.append(entry)
            continue
        values = []
        for value in entry["values"]:
            shown = dict(value)
            if value["filing_date"] < _ALPACA_SPLITS_FIRST_DAY:
                shown["note"] = SPLITS_BEFORE_NOTE
            else:
                adjusted = value["value"]
                for split in splits:
                    if split["ex_date"] > value["filing_date"]:
                        adjusted = adjusted / split["ratio"]
                if adjusted != value["value"]:
                    shown["value"] = adjusted
                    shown["as_filed"] = value["value"]
            values.append(shown)
        out.append({**entry, "values": values})
    return out
