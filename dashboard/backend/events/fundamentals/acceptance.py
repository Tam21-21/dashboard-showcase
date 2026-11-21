"""Filing acceptance times from the SEC submissions feed (lane FA1b-1, DECISIONS #479 item 10).

``acceptance_by_accession(client, cik, needed)`` answers: at what moment did the SEC accept each of these
filings? ``needed`` maps an accession number to the filing date (epoch seconds UTC) the fundamentals store holds
for it. The answer maps each accession to a UTC epoch second, or to None when the filing has no acceptance time.

The feed has a ``filings.recent`` block (the newest filings) and ``filings.files``, a list of older pages, each
with a filing-date range (``filingFrom`` to ``filingTo``). Every block holds parallel arrays; the ones read here
are ``accessionNumber`` and ``acceptanceDateTime``. The recent block is read first. An older page is fetched only
when its range holds the filing date of an accession that is still unfound, so a company with 70 old pages costs
only the pages its needed filings sit on. SEC's ranges are not exact, so an accession missed by
its range is searched on the other pages, nearest first; one found nowhere raises EdgarParseFatal.

``acceptanceDateTime`` is UTC ISO text: "2024-11-01T10:01:36.000Z" is 1730455296. An empty value means the filing
has none (None). Any other value that does not read raises EdgarParseFatal; it never becomes None, because a None
is stored as NULL and a read problem must not look like a filing with no time.
"""
from __future__ import annotations

import datetime as dt
from typing import Mapping, Optional

from ..errors import EdgarParseFatal


def _day(epoch: int) -> str:
    """The UTC calendar day of an epoch second, as YYYY-MM-DD (the form filingFrom and filingTo use)."""
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _parse_time(value: object, accession: str) -> Optional[int]:
    """One acceptanceDateTime value to a UTC epoch second; an empty value is None; anything else unreadable raises."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise EdgarParseFatal(f"acceptanceDateTime for {accession} is not text: {value!r}")
    try:
        moment = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise EdgarParseFatal(f"acceptanceDateTime for {accession} is not UTC ISO text: {value!r}") from exc
    return int(moment.replace(tzinfo=dt.timezone.utc).timestamp())


def _read_block(block: object, where: str, wanted: set[str], found: dict[str, Optional[int]]) -> None:
    """Take the wanted accessions out of one block of parallel arrays into ``found``."""
    if not isinstance(block, dict):
        raise EdgarParseFatal(f"submissions {where} is not an object")
    accessions = block.get("accessionNumber")
    times = block.get("acceptanceDateTime")
    if not isinstance(accessions, list) or not isinstance(times, list) or len(accessions) != len(times):
        raise EdgarParseFatal(f"submissions {where} lacks matching accessionNumber and acceptanceDateTime arrays")
    for accession, value in zip(accessions, times):
        if accession in wanted and accession not in found:
            found[accession] = _parse_time(value, accession)


def acceptance_by_accession(client, cik: int, needed: Mapping[str, int]) -> dict[str, Optional[int]]:
    """Return {accession: acceptance epoch or None} for every accession in ``needed``."""
    wanted = set(needed)
    found: dict[str, Optional[int]] = {}
    if not wanted:
        return {}
    submissions = client.get_submissions_json(cik)
    filings = submissions.get("filings") if isinstance(submissions, dict) else None
    if not isinstance(filings, dict) or "recent" not in filings:
        raise EdgarParseFatal(f"submissions for CIK {cik} has no filings.recent block")
    _read_block(filings["recent"], f"filings.recent of CIK {cik}", wanted, found)

    if all(a in found for a in wanted):
        return {accession: found[accession] for accession in needed}

    pages = filings.get("files") or []
    if not isinstance(pages, list):
        raise EdgarParseFatal(f"submissions filings.files of CIK {cik} is not a list")
    for page in pages:
        if not isinstance(page, dict) or not all(k in page for k in ("name", "filingFrom", "filingTo")):
            raise EdgarParseFatal(f"submissions filings.files entry of CIK {cik} lacks name, filingFrom or filingTo")

    # Pass 1: the pages whose range holds the filing date of an accession still unfound.
    fetched: set[str] = set()
    for page in pages:
        unfound_days = [_day(needed[a]) for a in wanted if a not in found]
        if not unfound_days:
            break
        if not any(page["filingFrom"] <= day <= page["filingTo"] for day in unfound_days):
            continue
        fetched.add(page["name"])
        _read_block(client.get_submissions_page(page["name"]), f"page {page['name']}", wanted, found)

    # Pass 2: SEC's ranges are not exact (a page can hold a filing dated a day past its filingTo), so an
    # accession still unfound is looked for on the remaining pages, nearest range first. It is never
    # returned as None for a miss: still unfound after every page raises, and the CIK is quarantined.
    def distance(page: dict) -> int:
        lo = dt.date.fromisoformat(page["filingFrom"])
        hi = dt.date.fromisoformat(page["filingTo"])
        gaps = []
        for a in wanted:
            if a not in found:
                day = dt.date.fromisoformat(_day(needed[a]))
                gaps.append(max((lo - day).days, (day - hi).days, 0))
        return min(gaps)

    remaining = [p for p in pages if p["name"] not in fetched]
    while remaining and any(a not in found for a in wanted):
        try:
            remaining.sort(key=distance)
        except ValueError as exc:
            raise EdgarParseFatal(f"submissions filingFrom or filingTo of CIK {cik} is not a date: {exc}") from exc
        page = remaining.pop(0)
        _read_block(client.get_submissions_page(page["name"]), f"page {page['name']}", wanted, found)
    unfound = sorted(a for a in wanted if a not in found)
    if unfound:
        raise EdgarParseFatal(
            f"submissions for CIK {cik}: {len(unfound)} accession(s) in no block, first {unfound[0]}")

    return {accession: found[accession] for accession in needed}
