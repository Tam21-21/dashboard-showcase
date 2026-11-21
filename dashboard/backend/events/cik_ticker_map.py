"""Reusable CIK<->ticker map loader (pre-M4 Brief A1 / D4).

Extracted from `cli.cmd_load_cik_ticker_map` so BOTH the CLI subcommand and the new
`POST /api/research/cik-ticker-map` route call one source of truth (the same
`bulk_reparse` / `load_universe` decoupling precedent). Fetches (or reads) SEC's
`company_tickers_exchange.json` and idempotently inserts each `(cik, ticker)` into
`research_cik_ticker_map` under `source='sec_company_tickers'`.

Idempotency rests on the `uq_cik_ticker_source` UNIQUE index over `(cik, ticker, source)`
(`eventsdb.py` `_DDL_CIK_TICKER_MAP`, Phase-2 Unit 4 / A4a-012): the insert is `INSERT OR
IGNORE`, so a duplicate is skipped atomically at the storage layer (race-proof under concurrent
loads), replacing the old application-level `INSERT ... WHERE NOT EXISTS` read-then-write guard.
The two fetch/parse helpers and the SEC constants moved here unchanged from `cli.py`; they had
no other caller.
"""
from __future__ import annotations

import re                                                       # short_name: state code and legal endings
import json                                                     # parse the SEC payload / a local fixture
import time                                                     # loaded_at epoch default
import urllib.error                                             # the error types callers map
import urllib.request                                           # the live SEC GET
from dataclasses import dataclass                               # the structured loader result
from pathlib import Path                                        # source_file typing
from typing import Optional, Union                              # optional overrides, like the sibling loaders

from .. import config, eventsdb                                 # config: the SEC User-Agent; eventsdb: the bridge table + transaction + path default
from .clients.edgar import decompress_sec_body                  # the single owner of the SEC gzip/deflate decode
from .tickers import normalize_ticker                           # the single canonical bridge-key owner (item 1 / B1)

# SEC's authoritative bridge file. The list is updated daily and includes every
# ticker SEC has on file for the registered company set. Per arch §8.4 (free
# source path). Verified URL. (Moved verbatim from cli.py.)
SEC_COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# The one source label these rows carry; also the WHERE-NOT-EXISTS dedup key (arch §4.7).
_SOURCE = "sec_company_tickers"

# LIKE wildcards a user could type in the catalog search; escaped so a literal % or _ in the
# search term is matched as itself, not as a wildcard. The ESCAPE char is backslash (escaped first).
LIKE_WILDCARD_ESCAPES = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})

# The catalog search query (ANA-1 §1.3). It lives here, with the loader that owns this table, so the
# ANA-1 probe can EXPLAIN the exact route query without importing the fastapi route layer (the laptop
# has no fastapi). One row per distinct (cik, ticker): the inner UNION gathers every map_id matching
# the prefix on ticker OR name; the ROW_NUMBER de-dup then takes the sec_company_tickers row first
# (it carries the backfilled name), the map_id tie-breaker keeping the pick deterministic when only
# non-sec sources exist. Both LIKE legs are case-insensitive (ASCII) prefix matches, ESCAPE '\' so a
# literal % or _ in q is matched as itself.
SEARCH_SQL = r"""
SELECT cik, ticker, name FROM (
    SELECT cik, ticker, name,
           ROW_NUMBER() OVER (
               PARTITION BY cik, ticker
               ORDER BY CASE source WHEN 'sec_company_tickers' THEN 0 ELSE 1 END, map_id
           ) AS rn
      FROM research_cik_ticker_map
     WHERE map_id IN (
           SELECT map_id FROM research_cik_ticker_map
            WHERE ticker LIKE :prefix ESCAPE '\'
           UNION
           SELECT map_id FROM research_cik_ticker_map
            WHERE name LIKE :prefix ESCAPE '\'
     )
)
WHERE rn = 1
ORDER BY CASE WHEN ticker = :ticker_exact THEN 0 ELSE 1 END,
         (name IS NULL), length(name), name COLLATE NOCASE, ticker
LIMIT :limit
"""


def search(conn, q: str, limit: int) -> list[dict]:
    """Search the CIK<->ticker bridge by ticker or company name (ANA-1 §1.3, moved here by lane LK1 so the
    catalog route and /api/display/search run the same query).

    Case-insensitive PREFIX match of ``q`` against ticker AND name, one row per distinct (cik, ticker),
    exact ticker first, then by name. ``conn`` is an events.db connection with ``sqlite3.Row`` rows
    (eventsdb.get_events_conn). Returns [{"cik", "ticker", "name"}]. A blank ``q`` is a caller bug and
    raises ValueError; the routes refuse it with a 422 before they get here.
    """
    term = q.strip()                                         # trim so a stray space is not part of the prefix
    if not term:
        raise ValueError("search needs a non-blank q")
    prefix = term.translate(LIKE_WILDCARD_ESCAPES) + "%"     # escaped prefix (LIKE is case-insensitive ASCII)
    rows = conn.execute(
        SEARCH_SQL, {"prefix": prefix, "ticker_exact": term.upper(), "limit": limit},  # tickers are stored upper-cased
    ).fetchall()
    return [{"cik": r["cik"], "ticker": r["ticker"], "name": r["name"]} for r in rows]


def _osa(a: str, b: str, cap: int) -> int:
    """Optimal string alignment distance, bounded: one character replaced, added or left out, or two
    neighbours swapped, each counts 1 (display_methods.md, "Search by name or ticker", bullet "Closest
    matches"). Returns the true distance when it is ``cap`` or less, and ``cap + 1`` for anything farther.

    Fast and still exact: lengths more than ``cap`` apart answer at once; in row i only the cells with
    |i - j| <= cap are worked out (every other cell counts as cap + 1); a row whose smallest cell is above
    ``cap`` ends the work."""
    over = cap + 1
    la = len(a)
    lb = len(b)
    if abs(la - lb) > cap:
        return over
    prev2 = None
    prev = [j if j <= cap else over for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [over] * (lb + 1)
        if i <= cap:
            cur[0] = i
        lo = max(1, i - cap)
        hi = min(lb, i + cap)
        smallest = cur[0] if lo > 1 or i <= cap else over
        for j in range(lo, hi + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            if v > over:
                v = over
            cur[j] = v
            if v < smallest:
                smallest = v
        if smallest > cap:
            return over
        prev2 = prev
        prev = cur
    return min(prev[lb], over)


# One row per (cik, ticker), the sec_company_tickers row first then map_id: the same pick as SEARCH_SQL, no LIKE.
_CLOSEST_ROWS_SQL = """
SELECT cik, ticker, name FROM (
    SELECT cik, ticker, name,
           ROW_NUMBER() OVER (
               PARTITION BY cik, ticker
               ORDER BY CASE source WHEN 'sec_company_tickers' THEN 0 ELSE 1 END, map_id
           ) AS rn
      FROM research_cik_ticker_map
)
WHERE rn = 1
"""


def closest(conn, q: str, limit: int) -> list[dict]:
    """The rows nearest to ``q`` when nothing starts with it (lane LK2s, DECISIONS #494 item 4;
    display_methods.md, "Search by name or ticker", bullet "Closest matches").

    Fewer than 4 characters typed: []. Allowed distance 1 for 4 to 7 characters, 2 for 8 or more. A row's
    distance is the smallest of the ticker's, and the first k letters of its name for k = len(q) - 1, len(q),
    len(q) + 1. Order: distance, a missing name last, shorter name, name without case, ticker.
    Returns [{"cik", "ticker", "name"}], at most ``limit``.
    """
    term = q.strip().upper()
    if len(term) < 4:
        return []
    cap = 2 if len(term) >= 8 else 1
    ks = (len(term) - 1, len(term), len(term) + 1)
    found = []
    for r in conn.execute(_CLOSEST_ROWS_SQL).fetchall():
        ticker = r["ticker"]
        name = r["name"]
        best = _osa(term, ticker.upper(), cap)
        if name:
            upper_name = name.upper()
            for k in ks:
                if 1 <= k <= len(upper_name):
                    best = min(best, _osa(term, upper_name[:k], cap))
        if best <= cap:
            found.append((best, name is None, len(name) if name else 0, (name or "").lower(), ticker, r["cik"], name))
    found.sort(key=lambda t: t[:5])
    return [{"cik": t[5], "ticker": t[4], "name": t[6]} for t in found[:limit]]


def name_for_ticker(conn, ticker: str) -> Optional[str]:
    """The company name the map holds for ``ticker`` (lane LK1d): the sec_company_tickers row first,
    then the lowest map_id; None when the ticker is absent or its rows carry no name."""
    row = conn.execute(
        "SELECT name FROM research_cik_ticker_map WHERE ticker = ? AND name IS NOT NULL "
        "ORDER BY CASE source WHEN 'sec_company_tickers' THEN 0 ELSE 1 END, map_id LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row is not None else None


# A trailing state code ("/DE", "/MD/"): a slash, two or three letters, an optional slash.
_STATE_CODE = re.compile(r"\s*/[A-Za-z]{2,3}/?\s*$")
# One trailing legal ending, with an optional comma before and an optional full stop after.
# Longer alternatives come first so "Incorporated" is not read as "Inc".
_LEGAL_ENDING = re.compile(
    r"(?:^|[\s,]+)(?:Incorporated|Corporation|Holdings|Company|Limited|Group|Corp|Inc|Co|Ltd|plc)\.?\s*$",
    re.IGNORECASE,
)
_AMPERSAND_CO = re.compile(r"\s*&\s*Co\.?\s*$", re.IGNORECASE)


def short_name(name: Optional[str]) -> Optional[str]:
    """A company name without its state code and legal endings, for matching news text (lane LK1d).

    "Apple Inc." gives "Apple"; "TEST HOLDINGS INC /DE" gives "TEST". The case is kept. None when
    nothing is left (for example "Group Inc") or the name is None."""
    if name is None:
        return None
    text = _STATE_CODE.sub("", name.strip())
    while True:
        stripped = _AMPERSAND_CO.sub("", text)
        if stripped == text:
            stripped = _LEGAL_ENDING.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = text.strip(" ,")
    return text or None


@dataclass
class CikTickerMapResult:
    """Outcome of one `load_cik_ticker_map` call (what the CLI prints, what the route returns)."""

    rows_in_payload: int        # how many (cik, ticker) pairs the SEC payload yielded
    inserted: int               # how many were genuinely new rows
    skipped_existing: int       # how many already existed (the idempotency count)
    names_backfilled: int       # how many existing rows had their name filled or renamed (ANA-1 §1.2)


def _fetch_company_tickers_json(url: str = SEC_COMPANY_TICKERS_EXCHANGE_URL,
                                user_agent: str | None = None) -> dict:
    """GET the SEC bridge JSON; return the parsed dict.

    Honors the User-Agent rule. Raises urllib.error.URLError (or its HTTPError subclass)
    for network errors; the caller maps it to a clean exit code (CLI) or a 502 (route).
    """
    ua = user_agent or config.SEC_USER_AGENT                   # the one SEC User-Agent setting
    request = urllib.request.Request(url, headers={
        "User-Agent": ua,
        # Request gzip/deflate to cut SEC bandwidth. Stdlib urllib does NOT
        # auto-decode Content-Encoding, so we decode the body ourselves via the
        # shared decompress_sec_body helper (events/clients/edgar.py) — the same
        # decode EdgarClient.get uses. Without it, SEC's gzip body reaches
        # json.loads as compressed bytes and fails on the 0x8b gzip magic byte.
        "Accept-Encoding": "gzip, deflate",
        "Accept": "application/json",
    })
    # 30 s socket timeout: SEC mirrors are typically < 5 s but a slow connection should
    # not hang the caller indefinitely.
    with urllib.request.urlopen(request, timeout=30) as response:
        raw_body = response.read()                              # possibly gzip/deflate-compressed
        content_encoding = response.headers.get("Content-Encoding")  # None when served uncompressed
    return json.loads(decompress_sec_body(raw_body, content_encoding))


def _parse_company_tickers_payload(payload: dict) -> list[tuple[int, str, Optional[str]]]:
    """Translate the SEC payload shape `{fields:[...], data:[[...], ...]}` to (cik, ticker, name) tuples.

    SEC's company_tickers_exchange.json shape (verified at writing):
        {"fields": ["cik", "name", "ticker", "exchange"],
         "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"], ...]}

    The (cik, ticker) projection is the bridge key; `name` is now also persisted (ANA-1
    §1.2, the nullable display column). `exchange` is still dropped. The name is normalized
    (strip + collapse internal whitespace) but otherwise kept verbatim — it is a display
    string, never upper-cased. A row whose name cell is missing or non-string yields a None
    name (the column is nullable). Raises ValueError if the payload lacks cik or ticker.
    """
    fields = payload.get("fields") or []                       # the column-name header row
    data = payload.get("data") or []                           # the row matrix
    try:
        cik_idx = fields.index("cik")                          # locate the cik column
        ticker_idx = fields.index("ticker")                    # locate the ticker column
    except ValueError as exc:                                  # a payload without those columns is unusable
        raise ValueError(
            f"SEC payload missing required 'cik' or 'ticker' field; got {fields!r}"
        ) from exc
    name_idx = fields.index("name") if "name" in fields else None  # name is optional (nullable column)
    rows: list[tuple[int, str, Optional[str]]] = []
    for entry in data:
        if not isinstance(entry, list) or len(entry) <= max(cik_idx, ticker_idx):
            # SEC occasionally ships rows missing a tail column for delisted entries; skip
            # those rather than failing the whole load (arch §8.4).
            continue
        cik_val = entry[cik_idx]                               # the cik cell
        ticker_val = entry[ticker_idx]                         # the ticker cell
        if not isinstance(cik_val, int) or not isinstance(ticker_val, str):
            continue                                          # a wrong-typed cell is a skip, not a crash
        norm_ticker = normalize_ticker(ticker_val)            # canonical bridge key (item 1 / B1): trim + upper + dot->hyphen
        if not norm_ticker:                                   # an empty ticker after trimming is useless; skip it
            continue
        name_val: Optional[str] = None                        # default null when the cell is absent/non-string
        if name_idx is not None and len(entry) > name_idx:    # the row reaches the name column
            raw_name = entry[name_idx]                         # the name cell
            if isinstance(raw_name, str):                      # a non-string name cell stays None
                collapsed = " ".join(raw_name.split())         # strip + collapse internal whitespace, keep case
                name_val = collapsed or None                   # an empty/whitespace name is null, not ""
        rows.append((cik_val, norm_ticker, name_val))         # store the canonical key + the display name
    return rows


def load_cik_ticker_map(
    *,
    source_file: Union[str, Path, None] = None,                # None -> fetch the live SEC URL
    events_db_path: Union[str, Path, None] = None,             # override the events.db path (probes)
    now_epoch: Optional[int] = None,                          # pin loaded_at for determinism (probes)
) -> CikTickerMapResult:
    """Fetch (or read) the SEC bridge JSON and idempotently insert each (cik, ticker) row.

    `source_file` None fetches the live SEC URL; a path reads a local fixture. Raises:
      - FileNotFoundError / json.JSONDecodeError  for a bad/missing source file
      - urllib.error.HTTPError / urllib.error.URLError  for a failed live fetch
      - ValueError  for a payload missing the required fields
    Each caller maps these to its own surface (the CLI to exit 2; the route to 400/502).
    """
    if source_file is not None:                               # read a local JSON fixture
        payload = json.loads(Path(source_file).read_text(encoding="utf-8"))
    else:                                                    # default: fetch the live SEC URL
        payload = _fetch_company_tickers_json()
    rows = _parse_company_tickers_payload(payload)           # extract the (cik, ticker, name) projection

    now = int(time.time()) if now_epoch is None else now_epoch  # the loaded_at stamp
    # All writes run inside one BEGIN IMMEDIATE. The write is TWO statements (ANA-1 §1.2):
    # (1) an idempotent INSERT OR IGNORE — the uq_cik_ticker_source UNIQUE index over
    # (cik, ticker, source) (Phase-2 Unit 4 / A4a-012) skips a duplicate atomically at the
    # storage layer (race-proof), so the row is written on a genuinely new (cik, ticker) and
    # silently skipped otherwise, also writing the name on new rows; (2) a name backfill UPDATE
    # over the payload rows that carry a name, filling every null name on a first reload and
    # overwriting a renamed one in place (the bridge tracks current identity, not name history).
    # effective_from / effective_to stay NULL per EL3 / arch §4.7. The inserted/skipped split
    # comes from the COUNT-by-source delta around the INSERT, which stays exact under OR IGNORE:
    # every inserted row carries the fixed literal source 'sec_company_tickers' (= _SOURCE, the
    # COUNT predicate), so each new row moves the count by one and each skip by zero. The only
    # constraint this INSERT can fire is the new unique index — cik/ticker/source/loaded_at are
    # the table's only NOT NULLs (all supplied) and source is a fixed in-enum literal — so a skip
    # is unambiguously a uniqueness conflict, never a masked NOT NULL/CHECK failure.
    # names_backfilled comes from the UPDATE's Connection.total_changes delta.
    update_rows = [(name, cik, ticker, name) for cik, ticker, name in rows if name is not None]
    with eventsdb.events_transaction(events_db_path) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM research_cik_ticker_map WHERE source = ?", (_SOURCE,)
        ).fetchone()[0]
        conn.executemany(
            """INSERT OR IGNORE INTO research_cik_ticker_map
                   (cik, ticker, effective_from, effective_to, source, loaded_at, name)
               VALUES (?, ?, NULL, NULL, 'sec_company_tickers', ?, ?)""",
            [(cik, ticker, now, name) for cik, ticker, name in rows],
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM research_cik_ticker_map WHERE source = ?", (_SOURCE,)
        ).fetchone()[0]
        # Backfill names on existing rows. The (name IS NULL OR name IS NOT :name) guard means
        # an unchanged reload matches nothing (no row modified); a null-name or renamed row matches.
        # A just-inserted row already carries its name, so it never matches here (no double count).
        # Count the modifications via Connection.total_changes (a documented, spec-stable attribute:
        # total rows modified on this connection) measured around the UPDATE only, not cursor.rowcount
        # (whose executemany accumulation is a CPython implementation detail that DB-API 2.0 permits to
        # be -1). The delta is exactly the rows the backfill touched.
        changes_before = conn.total_changes
        conn.executemany(
            """UPDATE research_cik_ticker_map
                  SET name = ?
                WHERE cik = ? AND ticker = ? AND source = 'sec_company_tickers'
                  AND (name IS NULL OR name IS NOT ?)""",
            update_rows,
        )
        names_backfilled = conn.total_changes - changes_before
    inserted = after - before                                # the count of genuinely new rows
    skipped = len(rows) - inserted                           # the rest were already present (prior load or intra-payload dup)
    return CikTickerMapResult(
        rows_in_payload=len(rows), inserted=inserted, skipped_existing=skipped,
        names_backfilled=names_backfilled,
    )
