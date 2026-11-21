"""Universe sourcing + the verify-endpoints gate (brief §4 + its 2026-06-19 amendment).

The laptop has no local catalog, so the universe comes from EODHD's /exchange-symbol-list
(verified live: rows of Code/Country/Currency/Exchange/Isin/Name/Type, NO listing dates).
Index-constituent lists are 403 on this plan, so priority is by Type (tradeable only) +
Exchange (major venues first) + an optional user seed file — never a fabricated index list.
"""

import json  # encode the verify-pass record for the meta table
import os  # read the optional seed file
import time  # stamp the verify record

import httpx  # the async client type

from . import manifest  # record the verify pass + read it
from .client import (EodhdAuthError, EodhdCapError, EodhdShapeError,  # the error taxonomy
                     EodhdTransientError, fetch_json)
from .config import (EXCHANGE, FUNDAMENTALS_URL, MAJOR_VENUES_ONLY,  # endpoints + exchange + the venue scope
                     SYMBOL_LIST_URL)

# Only tradeable price series the research lab uses (the amendment scope filter).
TRADEABLE_TYPES = frozenset({"Common Stock", "ETF"})
# Major listing venues — the liquid core where every S&P 500 / Nasdaq-100 name lives.
MAJOR_EXCHANGES = frozenset({"NASDAQ", "NYSE", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS"})
SEED_FILENAME = "priority_seed.txt"  # optional user watchlist at <staging>/eodhd/priority_seed.txt


def to_symbol(code: str) -> str:
    """Map an EODHD US `Code` to the API symbol form (e.g. 'AAPL' -> 'AAPL.US')."""
    return f"{code}.{EXCHANGE}"  # the '.US' virtual-exchange suffix the data endpoints expect


async def fetch_symbol_rows(http: httpx.AsyncClient, key: str, timeout: float,
                            delisted: bool = False) -> list:
    """Fetch the raw symbol-list rows for US (active or delisted). Returns a list of dicts."""
    params = {"api_token": key, "fmt": "json"}  # base params
    if delisted:  # the delisted set is requested with delisted=1
        params["delisted"] = "1"
    ctx = f"symbol-list {EXCHANGE} delisted={delisted}"  # error context
    data = await fetch_json(http, SYMBOL_LIST_URL.format(exchange=EXCHANGE), params, ctx, timeout)  # live call
    if not isinstance(data, list):  # the endpoint must return an array
        raise EodhdShapeError(f"{ctx}: expected a list, got {type(data).__name__}")
    return data  # raw rows (Code/Type/Exchange/...)


async def verify_endpoints(http: httpx.AsyncClient, key: str, timeout: float,
                           conn) -> dict:
    """Run the §4 live shape probe and record the pass in the manifest `meta` table.

    Confirms /exchange-symbol-list/US returns the expected key set, and records whether the
    index-constituent endpoint is available (it is 403 on this plan). On success, writes the
    verify record `backfill` gates on. Raises on a genuine shape mismatch (fail loud).
    """
    rows = await fetch_symbol_rows(http, key, timeout, delisted=False)  # probe the active list
    if not rows:  # an empty universe is a real failure, not a coverage gap
        raise EodhdShapeError("verify-endpoints: /exchange-symbol-list/US returned no rows")
    sample_keys = sorted(rows[0].keys())  # the resolved field names
    for required in ("Code", "Type", "Exchange"):  # the fields priority + symbol-forming rely on
        if required not in rows[0]:  # fail loud if a load-bearing field is absent/renamed
            raise EodhdShapeError(
                f"verify-endpoints: /exchange-symbol-list row missing {required!r}; keys={sample_keys}"
            )
    has_listing_dates = any("date" in k.lower() for k in sample_keys)  # record whether span-bounding is possible

    # Probe the index-constituent endpoint; 403 is the KNOWN plan limit, not a failure.
    index_available = False  # default: forbidden on this plan
    try:
        await fetch_json(http, FUNDAMENTALS_URL.format(symbol="GSPC.INDX"),
                         {"api_token": key, "fmt": "json"}, "index GSPC.INDX", timeout)
        index_available = True  # if it ever succeeds on an upgraded plan, record that
    except (EodhdAuthError, EodhdShapeError, EodhdTransientError, EodhdCapError):
        # 403/forbidden (the known plan limit) surfaces as EodhdAuthError; a transient failure as
        # EodhdTransientError. Either way the index lists are unavailable and priority uses
        # Type+Exchange (the amendment's design path). A raw httpx error escaping fetch_json would
        # be a bug and is intentionally NOT caught here — it should propagate loudly.
        index_available = False

    record = {  # the verify record the gate reads
        "passed_at": int(time.time()),
        "symbol_list_keys": sample_keys,
        "has_listing_dates": has_listing_dates,
        "index_constituents_available": index_available,
        "active_count": len(rows),
    }
    manifest.set_meta(conn, manifest.VERIFY_META_KEY, json.dumps(record))  # the gate key
    return record  # the summary the CLI prints


def load_seed(staging_root: str) -> list:
    """Read the optional priority seed file (one symbol per line). Absent file -> []."""
    path = os.path.join(staging_root, SEED_FILENAME)  # the seed location (<staging_root>/priority_seed.txt)
    if not os.path.exists(path):  # no seed is fine — skip, no error
        return []
    out = []  # accumulate cleaned symbols
    with open(path) as fh:  # read line by line
        for line in fh:  # one symbol per line
            sym = line.strip()  # drop whitespace
            if sym and not sym.startswith("#"):  # skip blanks and comments
                out.append(sym)  # keep the symbol
    return out  # the user-prioritised front of the queue


def _tradeable(row: dict) -> bool:
    """True iff a symbol-list row is a tradeable price series we fetch (Common Stock / ETF)."""
    return row.get("Type") in TRADEABLE_TYPES  # the amendment's scope filter


def index_by_symbol(active_rows: list, delisted_rows: list) -> dict:
    """Map every symbol-list row to its API symbol form, active rows winning over delisted ones.

    The seed tier used to queue a symbol with `exchange=None` even when the symbol list said
    exactly which venue it trades on, and the discovery venue filter fails OPEN on None
    (backfill._discover_batch, AC-8). With a seed file holding the whole universe, as the laptop's
    has since 2026-08-15, that combination meant the filter never fired for any symbol. This index
    is what lets a seeded symbol carry its real venue, so the filter judges it like any other.
    A symbol in neither list keeps no row here, and therefore still fails open.
    """
    index = {}  # symbol -> the symbol-list row it came from
    for row in delisted_rows:  # written first so an active row of the same symbol overwrites it
        code = row.get("Code")  # the bare code the symbol form is built from
        if code:  # a row with no Code cannot be mapped to a symbol at all
            index[to_symbol(code)] = row
    for row in active_rows:  # active wins: a name both active and delisted is treated as active
        code = row.get("Code")
        if code:
            index[to_symbol(code)] = row
    return index  # the venue/type lookup both tiers below read


def _on_major_venue(row) -> bool:
    """True iff a symbol-list row is POSITIVELY placed on one of the six major venues.

    A missing row (None) is False here; every caller decides for itself whether an unknown venue
    is kept or dropped, because the two tiers want opposite answers: a listed symbol we cannot
    place is off-venue as far as the queue is concerned, while a SEEDED symbol we cannot place is
    the operator's explicit request and is kept.
    """
    return bool(row) and row.get("Exchange") in MAJOR_EXCHANGES  # the venue-quality test


def build_priority_queue(active_rows: list, delisted_rows: list, seed: list,
                         major_only: bool = MAJOR_VENUES_ONLY) -> list:
    """Build the ordered, de-duplicated fetch queue (brief S4 amendment tiers 0-3).

    Returns a list of dicts {symbol, tier, type, exchange, delisted}. A symbol appears once,
    in its highest-priority tier. Tier 0 = seed (verbatim, even if not in the list), tier 1 =
    active major-exchange tradeable, tier 2 = active other-exchange tradeable, tier 3 = delisted
    tradeable. Within a tier, order is stable (input order) so resume is deterministic.

    `major_only` (Tamer's ruling, 2026-09-11; default `config.MAJOR_VENUES_ONLY`) confines the
    queue to MAJOR_EXCHANGES. It drops tier 2 entirely, drops off-venue delisted names, and drops
    a seeded symbol the symbol list positively places off-venue; it keeps every major-venue name
    whether active or delisted, so survivorship coverage is untouched. A seeded symbol in NEITHER
    symbol list is KEPT even under `major_only`: the operator asked for it by name and no venue
    evidence exists to overrule that, which is the same fail-open direction the discovery filter
    already takes. Pass False for the full US list (the behaviour before the ruling).

    Every tier-0 row now carries the venue and type the symbol list gives for that symbol, instead
    of the literal type "seed" and a NULL exchange. `manifest.set_priority` writes that venue, and
    `backfill._discover_batch` reads it, so the discovery venue filter finally sees a venue for a
    seeded symbol. A symbol absent from both lists still carries exchange None (fail open).
    """
    queue = []  # the ordered output
    placed = set()  # symbols already queued (dedupe across tiers)
    index = index_by_symbol(active_rows, delisted_rows)  # venue/type evidence for every listed symbol

    for sym in seed:  # tier 0: the user's explicit front-of-queue
        if sym in placed:  # dedupe
            continue
        row = index.get(sym)  # the symbol list's own row for this seeded symbol, if it has one
        if major_only and row is not None and not _on_major_venue(row):  # positively off-venue -> dropped
            continue  # a seeded symbol with NO row falls through and is kept (fail open)
        queue.append({"symbol": sym, "tier": 0,
                      "type": row.get("Type") if row else "seed",  # the real type when known
                      "exchange": row.get("Exchange") if row else None,  # None only when unplaceable
                      "delisted": False})
        placed.add(sym)

    active_tradeable = [r for r in active_rows if _tradeable(r)]  # filter once
    for major in (True, False):  # tier 1 (major venues) then tier 2 (other venues)
        if major_only and not major:  # tier 2 is exactly the off-venue actives the ruling drops
            continue
        for row in active_tradeable:  # stable input order within the tier
            is_major = _on_major_venue(row)  # venue quality
            if is_major != major:  # this row belongs to the other tier
                continue
            sym = to_symbol(row["Code"])  # the API symbol form
            if sym in placed:  # already seeded
                continue
            queue.append({"symbol": sym, "tier": 1 if major else 2,
                          "type": row.get("Type"), "exchange": row.get("Exchange"), "delisted": False})
            placed.add(sym)

    for row in delisted_rows:  # tier 3: delisted survivorship (probe-then-fetch, tolerate empty)
        if not _tradeable(row):  # only tradeable delisted names
            continue
        if major_only and not _on_major_venue(row):  # off-venue delisted names leave the queue too
            continue
        sym = to_symbol(row["Code"])  # the API symbol form
        if sym in placed:  # a name both active and delisted stays in its active tier
            continue
        queue.append({"symbol": sym, "tier": 3, "type": row.get("Type"),
                      "exchange": row.get("Exchange"), "delisted": True})
        placed.add(sym)

    return queue  # the full ordered fetch plan
