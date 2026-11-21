"""Each company's sector and industry, read once from the SEC submissions record (lane UNI1).

The sector is EDGAR's own office label as it shows today (DECISIONS #453, no history kept); the industry is the
SIC code and its description (DECISIONS #477 item 3). One row per company in events.db research_company_profiles,
keyed by CIK (the SEC's company number), with the time it was read. A price universe's members reach the row by
CIK at read time; nothing is copied into a membership table.

The only network call is client.get_submissions_json(cik) (EdgarClient); this module has no URL or HTTP code.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable, Iterable, Optional

from .. import config, eventsdb
from . import tickers as event_tickers
from .clients.edgar import EdgarClient
from .errors import EdgarHttp429Persistent, EdgarHttp5xx, EdgarNetworkTimeout, EdgarParseFatal

log = logging.getLogger(__name__)

# The nine office labels, exact text from DECISIONS #453.
SECTOR_LABELS = frozenset({
    "Technology",
    "Finance",
    "Manufacturing",
    "Life Sciences",
    "Energy & Transportation",
    "Real Estate & Construction",
    "Trade & Services",
    "Industrial Applications and Services",
    "Crypto Assets",
})

_OWNER_ORG = re.compile(r"^\d{2} (.+)$")   # EDGAR writes "06 Technology": a two digit office number, a space, the label
_SIC = re.compile(r"^\d{4}$")              # the SIC code is four digits

_IN_CHUNK = 500                            # keeps one IN (...) list well under SQLite's variable limit


class CompanyProfileShapeError(ValueError):
    """A submissions record holds an office label or SIC code that is not in the shape we read.

    field is "ownerOrg" or "sic" and value is the text found, so the page can name it (UNI1b).
    """

    def __init__(self, message: str, field: str = "ownerOrg", value: object = None):
        super().__init__(message)
        self.field = field
        self.value = value


class ContactAddressMissing(Exception):
    """The SEC contact setting (config.SEC_USER_AGENT) is empty, so no read can be made (UNI1b)."""


# UNI1b: how long a company's own failed read waits before it is asked again. DECISIONS #480 item 4 (the SEC
# check runs once a day). An operational dial (#204), not a backtest parameter.
RETRY_AFTER_SECONDS = 86_400

NO_CONTACT_WORDS = "No SEC contact address is set on the server"
BAD_CONTACT_WORDS = "The SEC contact address on the server is not valid"


def parse_profile(sub: dict) -> dict:
    """The sector, SIC code and SIC description of one submissions record. Absent or empty gives None."""
    owner = sub.get("ownerOrg")
    sector: Optional[str] = None
    if owner not in (None, ""):
        match = _OWNER_ORG.match(owner) if isinstance(owner, str) else None
        if match is None or match.group(1) not in SECTOR_LABELS:
            raise CompanyProfileShapeError(
                f"ownerOrg {owner!r} is not a two digit number and one of the nine office labels", "ownerOrg", owner)
        sector = match.group(1)
    sic = sub.get("sic")
    if sic in (None, ""):
        sic = None
    elif not (isinstance(sic, str) and _SIC.match(sic)):
        raise CompanyProfileShapeError(f"sic {sic!r} is not four digits", "sic", sic)
    description = sub.get("sicDescription")
    if description == "":
        description = None
    return {"sector": sector, "sic": sic, "sic_description": description}


def store_profile(econn, cik: int, profile: dict, read_at: int) -> None:
    """Insert or replace the one row of a company."""
    econn.execute(
        "INSERT INTO research_company_profiles(cik, sector, sic, sic_description, read_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(cik) DO UPDATE SET sector = excluded.sector, sic = excluded.sic, "
        "sic_description = excluded.sic_description, read_at = excluded.read_at;",
        (int(cik), profile["sector"], profile["sic"], profile["sic_description"], int(read_at)))


def store_failure(econn, cik: int, kind: str, reason: str, failed_at: int) -> None:
    """Keep the newest failed read of a company that has no profile. A company with a profile gets no failure row."""
    if econn.execute("SELECT 1 FROM research_company_profiles WHERE cik = ?;", (int(cik),)).fetchone() is not None:
        return
    econn.execute(
        "INSERT INTO research_company_profile_failures(cik, kind, reason, failed_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(cik) DO UPDATE SET kind = excluded.kind, reason = excluded.reason, failed_at = excluded.failed_at;",
        (int(cik), kind, reason, int(failed_at)))


def failure_words(exc: Exception) -> str:
    """The plain words the page shows for one failed company read (UNI1b). The full error goes to the log."""
    if isinstance(exc, EdgarHttp5xx):
        return "The SEC server answered with an error"
    if isinstance(exc, EdgarHttp429Persistent):
        return "The SEC asked us to slow down"
    if isinstance(exc, EdgarNetworkTimeout):
        return "SEC did not answer in time"
    if isinstance(exc, EdgarParseFatal):
        return "The SEC has no readable record for this company"
    if isinstance(exc, CompanyProfileShapeError):
        if exc.field == "sic":
            return f"The SEC record has an industry code we do not know: {exc.value}"
        return f"The SEC record names a sector we do not know: {exc.value}"
    raise TypeError(f"no plain words for {type(exc).__name__}")


def failures_for(econn, ciks: Iterable[int]) -> dict[int, dict]:
    """CIK to its failure row (kind, reason, failed_at) for the CIKs that have one."""
    wanted = sorted({int(c) for c in ciks})
    out: dict[int, dict] = {}
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start:start + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for row in econn.execute(
                f"SELECT cik, kind, reason, failed_at FROM research_company_profile_failures "
                f"WHERE cik IN ({marks});", chunk):
            out[int(row["cik"])] = {"kind": row["kind"], "reason": row["reason"], "failed_at": row["failed_at"]}
    return out


def cik_rows(econn, tickers: Iterable[str]) -> dict[str, Optional[tuple[int, str]]]:
    """Ticker to (cik, name), or None when no map row has a name.

    The row is the one the member-detail name query picks: the SEC form of the ticker, source
    sec_company_tickers, name not null, no effective_to first, newest loaded_at, highest map_id.
    """
    out: dict[str, Optional[tuple[int, str]]] = {}
    for ticker in tickers:
        row = econn.execute(
            "SELECT cik, name FROM research_cik_ticker_map "
            "WHERE ticker = ? AND source = 'sec_company_tickers' AND name IS NOT NULL "
            "ORDER BY (effective_to IS NOT NULL), loaded_at DESC, map_id DESC LIMIT 1;",
            (event_tickers.normalize_ticker(ticker),)).fetchone()
        out[ticker] = (int(row["cik"]), row["name"]) if row is not None else None
    return out


def profiles_for(econn, ciks: Iterable[int]) -> dict[int, dict]:
    """CIK to its stored row (sector, sic, sic_description, read_at) for the CIKs that have one."""
    wanted = sorted({int(c) for c in ciks})
    out: dict[int, dict] = {}
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start:start + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for row in econn.execute(
                f"SELECT cik, sector, sic, sic_description, read_at FROM research_company_profiles "
                f"WHERE cik IN ({marks});", chunk):
            out[int(row["cik"])] = {"sector": row["sector"], "sic": row["sic"],
                                    "sic_description": row["sic_description"], "read_at": row["read_at"]}
    return out


def read_missing_profiles(client, ciks: Iterable[int], *, reread: bool = False,
                          now: Callable[[], float] = time.time) -> dict:
    """Read the companies that have no row (all of them with reread=True) and store each in its own write.

    Returns {asked, on_file, read, failed: [(cik, words)]}. A company that fails is named, logged and left
    without a profile row, with its failure row written in plain words (UNI1b); the others carry on. A read that
    stores a profile deletes the company's failure row in the same transaction.
    """
    wanted = sorted({int(c) for c in ciks})
    with eventsdb.get_events_conn() as econn:
        on_file = set() if reread else set(profiles_for(econn, wanted))
    read = 0
    failed: list[tuple[int, str]] = []
    for cik in wanted:
        if cik in on_file:
            continue
        try:
            profile = parse_profile(client.get_submissions_json(cik))
        except (EdgarHttp5xx, EdgarHttp429Persistent, EdgarNetworkTimeout, EdgarParseFatal,
                CompanyProfileShapeError) as exc:
            words = f"{type(exc).__name__}: {exc}"
            log.error("company profile for CIK %s not read: %s", cik, words)
            failed.append((cik, words))
            with eventsdb.events_transaction() as econn:
                store_failure(econn, cik, "company", failure_words(exc), int(now()))
            continue
        with eventsdb.events_transaction() as econn:
            store_profile(econn, cik, profile, int(now()))
            econn.execute("DELETE FROM research_company_profile_failures WHERE cik = ?;", (int(cik),))
        read += 1
    return {"asked": len(wanted), "on_file": len(on_file), "read": read, "failed": failed}


def due_ciks(now: float) -> list[int]:
    """The companies to read now, sorted: members of any price universe, with a CIK and no profile row, and either
    no failure row, a contact failure, or a company failure from RETRY_AFTER_SECONDS or more before now.

    Derived from the membership and the two tables at each pass, never kept as a queue (UNI1b).
    """
    from .. import researchdb  # researchdb imports this module at load, so the import waits until the call
    with researchdb.get_conn() as conn:
        tickers = sorted(r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM research_price_universe_member_spans;"))
    with eventsdb.get_events_conn() as econn:
        mapped = cik_rows(econn, tickers)
        ciks = sorted({m[0] for m in mapped.values() if m is not None})
        profiled = set(profiles_for(econn, ciks))
        failures = failures_for(econn, ciks)
    due = []
    for cik in ciks:
        if cik in profiled:
            continue
        failure = failures.get(cik)
        if failure is None or failure["kind"] == "contact" or now - failure["failed_at"] >= RETRY_AFTER_SECONDS:
            due.append(cik)
    return due


def build_client() -> EdgarClient:
    """The real client, from config.SEC_USER_AGENT read at call time. Raises ContactAddressMissing when empty, ValueError (from EdgarClient) when no "@"."""
    user_agent = config.SEC_USER_AGENT
    if not user_agent:
        raise ContactAddressMissing("the SEC contact setting (config.SEC_USER_AGENT) is empty")
    return EdgarClient(user_agent=user_agent)


def run_due_reads(now: float, client_factory: Callable[[], object] = build_client) -> dict:
    """One pass: build the client first, then read every due company.

    A client that cannot be built is a server-wide cause: each due company gets a contact failure and nothing is
    read. A pass whose client builds deletes every contact failure first, so a fixed setting is never held back.
    Returns {due, read, failed, cause}; cause is the contact words or None.
    """
    try:
        client = client_factory()
    except (ContactAddressMissing, ValueError) as exc:
        words = NO_CONTACT_WORDS if isinstance(exc, ContactAddressMissing) else BAD_CONTACT_WORDS
        log.error("company profile reads not started: %s (%s)", words, exc)
        due = due_ciks(now)
        with eventsdb.events_transaction() as econn:
            for cik in due:
                store_failure(econn, cik, "contact", words, int(now))
        return {"due": len(due), "read": 0, "failed": [], "cause": words}
    with eventsdb.events_transaction() as econn:
        econn.execute("DELETE FROM research_company_profile_failures WHERE kind = 'contact';")
    due = due_ciks(now)
    out = read_missing_profiles(client, due, now=lambda: now)
    return {"due": len(due), "read": out["read"], "failed": out["failed"], "cause": None}


_WAKE: Optional[asyncio.Event] = None


def wake() -> None:
    """Ask the reading loop to make a pass now (a universe was made or members were added). Call from the event loop."""
    global _WAKE
    if _WAKE is None:
        _WAKE = asyncio.Event()
    _WAKE.set()


async def wait_woken(timeout: float) -> None:
    """Return when wake() is called or after timeout seconds; clears the wake either way."""
    global _WAKE
    if _WAKE is None:
        _WAKE = asyncio.Event()
    try:
        await asyncio.wait_for(_WAKE.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    _WAKE.clear()
