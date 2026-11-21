"""Earnings events derived from stored news (lane EA1).

Benzinga Newsdesk writes one fixed-form headline per earnings release, for example
"Apple Q3 EPS $1.57 Beats $1.42 Estimate, Sales $94.000B Beat $89.041B Estimate". Those headlines are
already stored as `news` events. `derive` reads them into `earnings` events (EPS and sales, actual and
estimate, release time), written through the shared write core. A headline that looks like earnings
but does not read is kept in the quarantine table with the reason, never dropped silently.

Two layers:
  parse_headline(headline)  pure: the fields of one headline, or EarningsHeadlineUnreadable.
  derive(events_db, mode)   reads news rows, writes events and quarantine rows, returns the counts.

Method source: docs/research_lab/display_methods.md, "Earnings release time" and "EPS and sales,
actual and estimate". The release time is the news row's created_at (the Newsdesk publishes at the
release), so knowledge_at = occurred_at = created_at and nothing is known earlier than the headline.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

from .. import config, eventsdb
from . import write_core
from .clients import benzinga_map
from .clients.edgar import EdgarClient
from .errors import (
    EdgarHttp5xx,
    EdgarHttp429Persistent,
    EdgarNetworkTimeout,
    EdgarParseFatal,
    TickerNotFoundInBridge,
)
from .types import earnings as earnings_type

log = logging.getLogger("dashboard.earnings.derive")

NEWSDESK_AUTHOR = "Benzinga Newsdesk"
SOURCE_DESCRIPTOR_NAME = "derive-earnings"
MODES = ("poll", "backfill")

# The official time (lane EA2): the 8-K with Item 2.02 accepted by the SEC within this many seconds
# either side of the headline time (planner ruling 2026-10-22: 24 hours, the nearest filing wins).
FILING_WINDOW_S = 24 * 3600
EARNINGS_ITEM = "2.02"

# A candidate is a Newsdesk row whose headline carries "Q<n> [Adj. ]EPS $".
_CANDIDATE_RE = re.compile(r"\bQ[1-4] (Adj\. )?EPS \$")

# The full form. "(x)" is a negative number; B, M, K are billions, millions, thousands.
_FULL_RE = re.compile(
    r"^(?:(Update|Correction|CORRECTION): )?(.+?) Q([1-4]) (Adj\. )?EPS "
    r"\$(\(?[0-9]+\.[0-9]+\)?) (Beats|Misses|Inline|In-Line) "
    r"\$(\(?[0-9]+\.[0-9]+\)?) Estimate, "
    r"Sales \$([0-9]+(?:\.[0-9]+)?)([BMK]) (Beat|Miss|Inline|In-Line) "
    r"\$([0-9]+(?:\.[0-9]+)?)([BMK]) Estimate$"
)
_UNITS = {"B": Decimal(10) ** 9, "M": Decimal(10) ** 6, "K": Decimal(10) ** 3}
_WORDS = {"Beats": "BEAT", "Beat": "BEAT", "Misses": "MISS", "Miss": "MISS",
          "Inline": "INLINE", "In-Line": "INLINE"}


class EarningsHeadlineUnreadable(ValueError):
    """A candidate headline (or its news row) cannot be read into an earnings event.

    The subclass name is what the quarantine row stores as failure_class, so it names the reason.
    """


class EarningsOffForm(EarningsHeadlineUnreadable):
    """The headline looks like earnings but does not match the Newsdesk form."""


class EarningsNotOneSymbol(EarningsHeadlineUnreadable):
    """The news row carries zero or several symbols, so the company is not certain."""


class EarningsNoCik(EarningsHeadlineUnreadable):
    """The row's ticker has no row in research_cik_ticker_map, so no company link can be made."""


def is_candidate(author: Optional[str], headline: str) -> bool:
    """True when a news row should be read as earnings: Newsdesk author and the Q<n> EPS form."""
    return author == NEWSDESK_AUTHOR and _CANDIDATE_RE.search(headline) is not None


def _number(text: str, headline: str) -> Decimal:
    """Read "1.57" or "(1.97)" as a Decimal; "(x)" is -x. A lone parenthesis is unreadable."""
    if text.startswith("(") != text.endswith(")"):
        raise EarningsOffForm(f"unbalanced parenthesis in number {text!r}: {headline!r}")
    if text.startswith("("):
        return -Decimal(text[1:-1])
    return Decimal(text)


def parse_headline(headline: str) -> dict:
    """The earnings fields of one headline, or EarningsOffForm naming why not.

    Returns fiscal_quarter, adjusted, eps_actual, eps_estimate, eps_word, sales_actual (US dollars),
    sales_estimate, sales_word and is_update. Decimal arithmetic keeps 622.918M exactly 622918000.
    """
    m = _FULL_RE.match(headline)
    if m is None:
        raise EarningsOffForm(f"headline does not match the Newsdesk earnings form: {headline!r}")
    (prefix, _company, quarter, adj, eps_a, eps_word, eps_e,
     sales_a, sales_a_unit, sales_word, sales_e, sales_e_unit) = m.groups()
    return {
        "fiscal_quarter": int(quarter),
        "adjusted": adj is not None,
        "eps_actual": float(_number(eps_a, headline)),
        "eps_estimate": float(_number(eps_e, headline)),
        "eps_word": _WORDS[eps_word],
        "sales_actual": float(Decimal(sales_a) * _UNITS[sales_a_unit]),
        "sales_estimate": float(Decimal(sales_e) * _UNITS[sales_e_unit]),
        "sales_word": _WORDS[sales_word],
        "is_update": prefix is not None,
    }


class SubmissionsShapeError(ValueError):
    """A submissions JSON does not hold filings.recent arrays (form, items, acceptanceDateTime, accessionNumber)."""


# The failures one company lookup can meet. Each is counted and logged by derive, never swallowed.
LOOKUP_ERRORS = (EdgarHttp5xx, EdgarHttp429Persistent, EdgarNetworkTimeout, EdgarParseFatal,
                 SubmissionsShapeError)


def _build_edgar_client() -> EdgarClient:
    """The real client, with the one SEC User-Agent setting (config.SEC_USER_AGENT)."""
    return EdgarClient(user_agent=config.SEC_USER_AGENT)


def find_earnings_8k(submissions: dict, release_at: int) -> Optional[tuple[int, str]]:
    """The (accepted epoch seconds, accession number) of the Item 2.02 8-K nearest to release_at.

    Only a form "8-K" whose items list holds "2.02" counts, and only when its acceptanceDateTime lies
    within FILING_WINDOW_S either side of release_at. None when there is none. acceptanceDateTime here
    is ISO 8601 UTC ("2026-07-30T20:30:36.000Z"), not the SGML Eastern form.
    """
    try:
        recent = submissions["filings"]["recent"]
        forms, items = recent["form"], recent["items"]
        accepted, accessions = recent["acceptanceDateTime"], recent["accessionNumber"]
    except (KeyError, TypeError) as exc:
        raise SubmissionsShapeError(f"submissions JSON has no filings.recent arrays: {exc!r}") from exc
    if not (len(forms) == len(items) == len(accepted) == len(accessions)):
        raise SubmissionsShapeError("submissions filings.recent arrays differ in length")
    best: Optional[tuple[int, int, str]] = None
    for form, item_list, when, accession in zip(forms, items, accepted, accessions):
        if form != "8-K" or EARNINGS_ITEM not in [i.strip() for i in item_list.split(",")]:
            continue
        try:
            epoch = int(dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
        except ValueError as exc:
            raise SubmissionsShapeError(f"unreadable acceptanceDateTime {when!r}") from exc
        gap = abs(epoch - release_at)
        if gap <= FILING_WINDOW_S and (best is None or (gap, epoch) < (best[0], best[1])):
            best = (gap, epoch, accession)
    return None if best is None else (best[1], best[2])


def _quarantine_entry(article_id: int, headline: str, exc: Exception) -> tuple[dict, Exception, str]:
    raw = json.dumps({"article_id": article_id, "headline": headline}, sort_keys=True)
    return ({"event_type": "earnings", "raw_input": raw}, exc, "parse")


def _read_candidates(conn, since_epoch: Optional[int] = None) -> list[dict]:
    """Every active news row that is a candidate, oldest first. No time window by default: an outage
    of any length is caught up on the next run. since_epoch (the --since day) limits it to news
    created at or after that instant."""
    sql = ("SELECT natural_key, payload FROM research_events "
           "WHERE event_type = 'news' AND status = 'active' "
           "AND json_extract(payload, '$.author') = ? "
           "AND json_extract(payload, '$.headline') LIKE '%EPS $%' ")
    params: list = [NEWSDESK_AUTHOR]
    if since_epoch is not None:
        sql += "AND json_extract(payload, '$.created_at') >= ? "
        params.append(since_epoch)
    rows = conn.execute(sql + "ORDER BY knowledge_at, natural_key", params).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload"])
        if is_candidate(payload.get("author"), payload["headline"]):
            out.append(payload)
    return out


def _already_done_article_ids(conn) -> set[int]:
    """Article ids that already have an active earnings event or an earnings quarantine row."""
    done = {int(r["natural_key"]) for r in conn.execute(
        "SELECT natural_key FROM research_events WHERE event_type = 'earnings' AND status = 'active'")}
    for r in conn.execute(
            "SELECT raw_input FROM research_event_quarantines WHERE event_type = 'earnings'"):
        done.add(int(json.loads(r["raw_input"])["article_id"]))
    return done


def _attach_filing_times(records: list[dict], edgar_client) -> int:
    """Add filing_accepted_at and accession_number to each record whose company has a matching 8-K.

    One submissions GET per CIK for the whole run (a cache keyed by CIK; a failed CIK stays failed and
    is not asked again). Returns the number of CIKs whose lookup failed.
    """
    cache: dict[int, Optional[dict]] = {}
    failed = 0
    client = edgar_client
    for rec in records:
        cik = rec["companies"][0]["cik"]
        if cik not in cache:
            if client is None:
                client = _build_edgar_client()
            try:
                cache[cik] = client.get_submissions_json(cik)
            except LOOKUP_ERRORS as exc:
                log.error("8-K lookup failed for CIK %s: %s: %s", cik, type(exc).__name__, exc)
                cache[cik] = None
                failed += 1
        subs = cache[cik]
        if subs is None:
            continue
        try:
            found = find_earnings_8k(subs, rec["payload"]["release_at"])
        except SubmissionsShapeError as exc:
            log.error("8-K lookup unreadable for CIK %s: %s", cik, exc)
            cache[cik] = None
            failed += 1
            continue
        if found is not None:
            rec["payload"]["filing_accepted_at"], rec["payload"]["accession_number"] = found
    return failed


def derive(events_db_path: Union[str, Path, None], mode: str,
           research_db_path: Union[str, Path, None] = None, *,
           edgar_client=None, since_epoch: Optional[int] = None) -> tuple[int, int, int, int]:
    """Read stored news into earnings events. Returns (candidates, written, unreadable, lookups_failed).

    Before writing, each new release gets the SEC time of its company's Item 2.02 8-K (see
    find_earnings_8k). The submissions JSON is fetched once per CIK per run. edgar_client=None builds
    the real EdgarClient from config.SEC_USER_AGENT the first time a
    lookup is needed; the probes pass a stub. A failed lookup is logged with its error, counted in
    lookups_failed, and that release is written without the two fields. No 2.02 8-K in the window:
    the fields stay absent.

    Both modes read every active candidate; they differ only in the label the caller prints. A
    candidate whose article already has an earnings event or an earnings quarantine row is skipped,
    so a re-run writes nothing new. `unreadable` counts the quarantine rows this run wrote.
    """
    if mode not in MODES:
        raise ValueError(f"derive mode must be one of {MODES}, got {mode!r}")
    records: list[dict] = []
    quarantines: list[tuple[dict, Exception, str]] = []
    with eventsdb.get_events_conn(events_db_path) as conn:
        candidates = _read_candidates(conn, since_epoch)
        done = _already_done_article_ids(conn)
        for news in candidates:
            article_id = int(news["article_id"])
            if article_id in done:
                continue
            headline = news["headline"]
            try:
                fields = parse_headline(headline)
                symbols = news.get("symbols") or []
                if len(symbols) != 1:
                    raise EarningsNotOneSymbol(
                        f"not one symbol: the news row carries {len(symbols)} ({symbols!r}): {headline!r}")
                ticker = symbols[0]
                try:
                    cik = benzinga_map.resolve_ticker_to_cik(conn, ticker)
                except TickerNotFoundInBridge as exc:
                    raise EarningsNoCik(f"no CIK for {ticker}") from exc
            except EarningsHeadlineUnreadable as exc:
                quarantines.append(_quarantine_entry(article_id, headline, exc))
                continue
            released = int(news["created_at"])                 # the release time: the news publish instant
            records.append({
                "event_type": "earnings",
                "natural_key": str(article_id),
                "producer_id": str(article_id),
                "knowledge_at": released,
                "occurred_at": released,
                "source": "alpaca_news",
                "origin": "fetched",
                "payload_schema_version": earnings_type.PAYLOAD_SCHEMA_VERSION,
                "raw_input": json.dumps({"article_id": article_id, "headline": headline}, sort_keys=True),
                "payload": {
                    "article_id": article_id, "raw_ticker": ticker, **fields,
                    "release_at": released, "headline": headline,
                },
                "companies": [{"cik": cik, "role": "primary_issuer", "cusip": None}],
            })
    lookups_failed = _attach_filing_times(records, edgar_client)
    if not records and not quarantines:
        return len(candidates), 0, 0, lookups_failed
    result = write_core.write_event_batch(
        events_db_path, source="alpaca_news", kind="fetch", policy="quarantine",
        records=records,
        source_descriptor=json.dumps({"source": SOURCE_DESCRIPTOR_NAME, "mode": mode}, sort_keys=True),
        parse_quarantines_collector=quarantines,
        research_db_path=research_db_path,
    )
    if result.status not in ("succeeded", "succeeded_with_quarantines"):
        raise RuntimeError(
            f"earnings batch {result.batch_id} closed as {result.status!r} "
            f"(failure_class {result.failure_class!r})")
    return len(candidates), result.event_count, result.quarantine_count, lookups_failed
