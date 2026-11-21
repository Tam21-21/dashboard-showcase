"""Per-ticker 1-min coverage discovery (brief §5.2).

1-min depth is per-ticker and must be DISCOVERED, never assumed (AAPL->2003, SPY->~2014,
a delisted name may be empty). We probe a few anchored 120-day windows deepest-first; the
deepest window with data brackets the floor. We start the deep walk from the deeper-neighbour
anchor so sparse older data is not missed (safe-early), and the leading empty windows are
marked 'empty', not 'error'. A name with no 1-min at any anchor has a NULL floor and is
skipped for 1-min entirely.
"""

import time  # 'now' for the anchored look-back (overridable for a deterministic probe)

import httpx  # the async client type

from .client import CapReached, fetch_json  # cap signal + the GET helper
from .config import COST_INTRADAY, INTRADAY_URL  # cost + endpoint
from .tradinglife import NoTradingOverlap, spans_before_final_end  # the top-only narrowing (brief S3.2)
from .windows import anchored_probe_windows  # the deepest-first probe spans

# Look-back anchors (years before now). Deepest ~22y reaches the EODHD 1-min floor (~2004);
# the spacing trades discovery cost (one intraday request = 5 calls per probed anchor) against
# how tightly the floor is bracketed. Operational dial under DECISIONS #204, not a backtest
# parameter: moving the spacing changes how tightly (and how cheaply) a floor is bracketed, never
# WHICH bars are eligible once a floor is found -- that is DEEP_1M_END's job (config.py), which IS
# provenanced.
DISCOVERY_ANCHORS_YEARS = (22, 18, 14, 10, 6, 3, 1)


async def discover_floor(http: httpx.AsyncClient, key: str, ledger, limiter,
                         timeout: float, symbol: str, life=None, now: int = None) -> int:
    """Return the approximate earliest 1-min epoch for `symbol`, or None if no coverage.

    Probes the anchored windows deepest-first; on the first window that returns bars, returns
    the deeper-neighbour anchor's start (safe-early) so the deep walk does not miss sparse older
    data. All-empty -> None (the caller marks coverage NULL and skips 1-min). Raises CapReached
    if the daily cap would be exceeded mid-discovery (the pool stops cleanly and resumes later).

    `life` narrows the anchors to the TOP rule only (brief S3.2 step 3), via
    `spans_before_final_end`, never `spans_overlapping_life`: the bottom trim would delete the
    empty deeper anchor the returned floor rests on. `life=None` (the default) probes every anchor,
    unchanged from before this parameter existed, so every existing caller keeps its meaning.
    Raises NoTradingOverlap when the filter leaves nothing to probe (outcome c: no anchor survives,
    so nothing is asked and the caller records no coverage row at all).

    `now` is injectable so a probe can pin the look-back reference; defaulting to None and reading
    the live clock only when None keeps production behaviour identical to before this parameter
    existed (mirrors `windows.anchored_probe_windows`, which already takes `now_ts` explicitly).
    """
    now = int(time.time()) if now is None else now  # the look-back reference: live clock, or a pinned one
    spans = anchored_probe_windows(now, DISCOVERY_ANCHORS_YEARS)  # deepest-first inclusive (from,to)
    spans = spans_before_final_end(spans, life)  # top-only trim: drop anchors opening after a FINAL last bar
    if not spans:  # every anchor lies outside the ticker's life -> nothing to probe (outcome c, brief S3.2 step 4)
        raise NoTradingOverlap(f"{symbol} 1min discovery: no anchor overlaps the ticker's trading life")
    prev_from = None  # the deeper anchor already tested (empty) — the safe-early start
    for frm, to in spans:  # walk deepest -> shallowest, over the surviving anchors only
        if not ledger.can_spend(COST_INTRADAY):  # respect the daily cap before issuing
            raise CapReached(f"cap reached during coverage discovery for {symbol}")
        await limiter.acquire()  # respect the per-minute rate
        params = {"api_token": key, "interval": "1m", "from": frm, "to": to, "fmt": "json"}  # one probe
        ctx = f"{symbol} discover 1m [{frm}..{to}]"  # error context
        data = await fetch_json(http, INTRADAY_URL.format(symbol=symbol), params, ctx, timeout)  # live probe
        ledger.charge(COST_INTRADAY)  # account the 5 calls
        if isinstance(data, list) and data:  # this anchor has bars -> floor is at/above its deeper neighbour
            return prev_from if prev_from is not None else frm  # safe-early: start from the deeper empty anchor
        prev_from = frm  # remember this (empty) anchor as the deeper neighbour for the next iteration
    return None  # no 1-min coverage at any anchor -> skip 1-min for this name
