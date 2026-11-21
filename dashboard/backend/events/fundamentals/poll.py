"""The daily SEC poll for followed companies (lane FA3, DECISIONS #480 item 4, #488 item 13).

Once a day the server reads the SEC submissions feed of every followed company, records each new 10-K or 10-Q (or
an amendment of one), fetches that company's figures through the existing write path, and records the pass (when it
ran, what it found, what failed, when the next one is due). The poll computes no figure: the figures come from
``write_fundamental_batch`` and nothing here changes them.

A company is followed when the facts table holds at least one row for it. The list is derived at each pass, never
kept (DDIA ch. 1, systems of record and derived data).

A filing is new when its accession number has no row in ``research_fundamental_filings``. Its time is the SEC
acceptance time, or the filing date plus one day when the feed gives none (the same rule the store applies to a
figure with no acceptance time). If that time is at or before the newest time among the company's stored figures,
the store already holds everything the filing could have given: the filing is recorded as known. Otherwise it is
pending, and the company is refreshed once for all its pending filings. A pending filing is fetched when its
accession number shows up in the company's refreshed document; it stays pending while it does not, and after
GIVE_UP_AFTER_SECONDS it is recorded as not_in_facts with a failure, so the miss is visible and not retried for ever.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from ... import eventsdb
from ..errors import EdgarHttp429Persistent, EdgarHttp5xx, EdgarNetworkTimeout, EdgarParseFatal
from . import write
from .acceptance import _parse_time
from .parse import PERIODIC_FORMS

# Operational dials, not backtest parameters (a change cannot change a backtest's output). 04:30 UTC is after
# EDGAR's 10 p.m. Eastern filing close and clear of the 02:00 backup and the 03:00 13D fetch.
POLL_HOUR_UTC = 4
POLL_MINUTE_UTC = 30
LATE_AFTER_SECONDS = 26 * 3600     # a daily poll more than two hours overdue is late
GIVE_UP_AFTER_SECONDS = 7 * 86400  # a filing the company's facts still lack after a week is recorded as missing

DAY = 86_400
_SUBMISSION_FAILURES = (EdgarHttp5xx, EdgarNetworkTimeout, EdgarParseFatal)
_RUN_FAILED = "the pass did not finish"


@dataclass
class PollResult:
    """The outcome of one pass, the same facts the poll row holds."""

    poll_id: int
    status: str
    ciks_checked: int = 0
    filings_new: int = 0
    filings_fetched: int = 0
    refresh_batch_id: Optional[int] = None
    failures: list = field(default_factory=list)
    failure_message: Optional[str] = None


def followed_ciks(events_db_path: Union[str, Path, None] = None) -> list[int]:
    """The companies the store holds figures for, in CIK order."""
    with eventsdb.get_events_conn(events_db_path) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT cik FROM research_fundamental_facts ORDER BY cik")]


def _now(now_epoch: Optional[int]) -> int:
    return int(now_epoch) if now_epoch is not None else int(time.time())


def next_poll_at(now_epoch: int) -> int:
    """The next POLL_HOUR_UTC:POLL_MINUTE_UTC strictly after ``now_epoch``, in epoch seconds UTC."""
    now = dt.datetime.fromtimestamp(now_epoch, tz=dt.timezone.utc)
    today = now.replace(hour=POLL_HOUR_UTC, minute=POLL_MINUTE_UTC, second=0, microsecond=0)
    nxt = today if today > now else today + dt.timedelta(days=1)
    return int(nxt.timestamp())


def _failure(cik: Optional[int], step: str, cls: str, message: str) -> dict:
    return {"cik": cik, "step": step, "class": cls, "message": message}


def _recent_block(submissions: object, cik: int) -> list[tuple[str, str, int, Optional[int]]]:
    """The (form, accession, filing_date epoch, acceptance epoch or None) rows of filings.recent for periodic forms.

    Raises EdgarParseFatal when the block lacks form, accessionNumber, acceptanceDateTime or filingDate arrays of
    equal length, so a changed feed is a visible failure and never a silent empty list.
    """
    filings = submissions.get("filings") if isinstance(submissions, dict) else None
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise EdgarParseFatal(f"submissions for CIK {cik} has no filings.recent block")
    names = ("form", "accessionNumber", "acceptanceDateTime", "filingDate")
    arrays = [recent.get(n) for n in names]
    if not all(isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
        raise EdgarParseFatal(
            f"submissions filings.recent of CIK {cik} lacks equal-length form, accessionNumber, "
            f"acceptanceDateTime and filingDate arrays")
    rows = []
    for form, accession, accepted, filed in zip(*arrays):
        if form not in PERIODIC_FORMS:
            continue
        try:
            filed_epoch = int(dt.datetime.strptime(filed, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())
        except (TypeError, ValueError) as exc:
            raise EdgarParseFatal(f"filingDate for {accession} is not YYYY-MM-DD: {filed!r}") from exc
        rows.append((form, accession, filed_epoch, _parse_time(accepted, accession)))
    return rows


def _accessions_in(doc: object) -> set[str]:
    """The accession numbers of periodic-form figures in a CompanyFacts document."""
    found: set[str] = set()
    facts = doc.get("facts") if isinstance(doc, dict) else None
    if not isinstance(facts, dict):
        return found
    for taxonomy in facts.values():
        if not isinstance(taxonomy, dict):
            continue
        for concept in taxonomy.values():
            units = concept.get("units") if isinstance(concept, dict) else None
            if not isinstance(units, dict):
                continue
            for items in units.values():
                for item in items if isinstance(items, list) else ():
                    if isinstance(item, dict) and item.get("form") in PERIODIC_FORMS and item.get("accn"):
                        found.add(item["accn"])
    return found


def _close(conn, poll_id: int, *, now: int, status: str, result: PollResult) -> None:
    result.status = status
    with eventsdb.transaction_on(conn):
        conn.execute(
            """UPDATE research_fundamental_polls
                  SET finished_at = ?, status = ?, ciks_checked = ?, filings_new = ?, filings_fetched = ?,
                      refresh_batch_id = ?, failures = ?, failure_message = ?
                WHERE poll_id = ?""",
            (now, status, result.ciks_checked, result.filings_new, result.filings_fetched,
             result.refresh_batch_id, json.dumps(result.failures), result.failure_message, poll_id))


def poll_once(
    client,
    *,
    fetch_fn: Callable[[int], dict],
    acceptance_fn: Callable[[int, dict], dict],
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
    now_epoch: Optional[int] = None,
) -> PollResult:
    """Run one pass. ``client`` needs ``get_submissions_json(cik)``; ``fetch_fn`` and ``acceptance_fn`` are the
    same transports ``write_fundamental_batch`` takes. A persistent 429 closes the pass failed and returns it. Any
    other error after the pass row opens closes it failed with the error message and propagates."""
    now = _now(now_epoch)
    with eventsdb.get_events_conn(events_db_path) as conn:
        with eventsdb.transaction_on(conn):
            conn.execute(
                "UPDATE research_fundamental_polls SET status = 'failed', finished_at = ?, failure_message = ? "
                "WHERE status = 'running'", (now, _RUN_FAILED))
            poll_id = conn.execute(
                "INSERT INTO research_fundamental_polls (started_at, status) VALUES (?, 'running')",
                (now,)).lastrowid
        result = PollResult(poll_id=poll_id, status="running")
        try:
            return _run_pass(conn, result, now, client=client, fetch_fn=fetch_fn, acceptance_fn=acceptance_fn,
                             events_db_path=events_db_path, research_db_path=research_db_path)
        except BaseException as exc:
            # A fence, not a swallow: whatever dies after the pass row opened, the row must not stay 'running'.
            # Its message is stored and the error is re-raised unchanged.
            result.failure_message = f"{type(exc).__name__}: {exc}"
            _close(conn, poll_id, now=now, status="failed", result=result)
            raise


def _run_pass(conn, result: PollResult, now: int, *, client, fetch_fn, acceptance_fn,
              events_db_path, research_db_path) -> PollResult:
    """The body of one pass, after the running row is open. Closes the row itself on every normal exit."""
    poll_id = result.poll_id
    ciks = [r[0] for r in conn.execute("SELECT DISTINCT cik FROM research_fundamental_facts ORDER BY cik")]
    for cik in ciks:
        try:
            rows = _recent_block(client.get_submissions_json(cik), cik)
        except EdgarHttp429Persistent as exc:
            result.failure_message = f"SEC rate limit persisted at CIK {cik}: {exc}"
            _close(conn, poll_id, now=now, status="failed", result=result)
            return result
        except _SUBMISSION_FAILURES as exc:
            result.failures.append(_failure(cik, "submissions", type(exc).__name__, str(exc)))
            continue
        result.ciks_checked += 1
        newest = conn.execute(
            "SELECT MAX(COALESCE(accepted_at, filing_date + ?)) FROM research_fundamental_facts WHERE cik = ?",
            (DAY, cik)).fetchone()[0]
        with eventsdb.transaction_on(conn):
            for form, accession, filed, accepted in rows:
                if conn.execute("SELECT 1 FROM research_fundamental_filings WHERE accession_number = ?",
                                (accession,)).fetchone():
                    continue
                moment = accepted if accepted is not None else filed + DAY
                status = "known" if moment <= newest else "pending"
                conn.execute(
                    "INSERT INTO research_fundamental_filings "
                    "(accession_number, cik, form, accepted_at, filing_date, status, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (accession, cik, form, accepted, filed, status, now))
                if status == "pending":
                    result.filings_new += 1

    pending_ciks = [r[0] for r in conn.execute(
        "SELECT DISTINCT cik FROM research_fundamental_filings WHERE status = 'pending' ORDER BY cik")]
    in_document: dict[int, set[str]] = {}
    quarantined: dict[int, str] = {}
    if pending_ciks:
        def recording_fetch(cik: int) -> dict:
            doc = fetch_fn(cik)
            in_document[cik] = _accessions_in(doc)
            return doc

        batch = write.write_fundamental_batch(
            events_db_path, kind="refresh", ciks=pending_ciks, fetch_fn=recording_fetch,
            now_epoch=now, research_db_path=research_db_path, acceptance_fn=acceptance_fn)
        result.refresh_batch_id = batch.batch_id
        for q in conn.execute(
                "SELECT cik, failure_class FROM research_fundamental_quarantines "
                "WHERE batch_id = ? AND failure_step IN ('fetch','parse') AND cik IS NOT NULL",
                (batch.batch_id,)):
            quarantined[q["cik"]] = q["failure_class"]

        pending = [dict(r) for r in conn.execute(
            "SELECT accession_number, cik, seen_at FROM research_fundamental_filings "
            "WHERE status = 'pending' ORDER BY cik, accession_number")]
        with eventsdb.transaction_on(conn):
            for row in pending:
                cik, accession = row["cik"], row["accession_number"]
                if cik in quarantined:
                    result.failures.append(_failure(
                        cik, "refresh", quarantined[cik],
                        f"the refresh of CIK {cik} was quarantined; {accession} stays pending"))
                elif accession in in_document.get(cik, set()):
                    conn.execute(
                        "UPDATE research_fundamental_filings SET status = 'fetched', batch_id = ?, settled_at = ? "
                        "WHERE accession_number = ?", (batch.batch_id, now, accession))
                    result.filings_fetched += 1
                elif now - row["seen_at"] > GIVE_UP_AFTER_SECONDS:
                    conn.execute(
                        "UPDATE research_fundamental_filings SET status = 'not_in_facts', settled_at = ? "
                        "WHERE accession_number = ?", (now, accession))
                    result.failures.append(_failure(
                        cik, "refresh", "AccessionNotInFacts",
                        f"{accession} is still not in the facts of CIK {cik} after "
                        f"{GIVE_UP_AFTER_SECONDS // DAY} days"))

    _close(conn, poll_id, now=now, result=result,
           status="succeeded_with_failures" if result.failures else "succeeded")
    return result


def poll_status(events_db_path: Union[str, Path, None] = None, now_epoch: Optional[int] = None) -> dict:
    """What the status page shows: the newest pass, the last success, the next due time (epoch seconds UTC),
    whether the poll is late, and the counts."""
    now = _now(now_epoch)
    with eventsdb.get_events_conn(events_db_path) as conn:
        last = conn.execute("SELECT * FROM research_fundamental_polls ORDER BY poll_id DESC LIMIT 1").fetchone()
        last_success = conn.execute(
            "SELECT MAX(finished_at) FROM research_fundamental_polls "
            "WHERE status IN ('succeeded','succeeded_with_failures')").fetchone()[0]
        followed = conn.execute("SELECT COUNT(DISTINCT cik) FROM research_fundamental_facts").fetchone()[0]
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM research_fundamental_filings GROUP BY status")}
    last_poll = None
    if last is not None:
        last_poll = dict(last)
        last_poll["failures"] = json.loads(last_poll["failures"])
    return {
        "last_poll": last_poll,
        "last_success_at": last_success,
        "next_poll_at": next_poll_at(now),
        "late": last_success is None or now - last_success > LATE_AFTER_SECONDS,
        "companies_followed": followed,
        "filings_pending": counts.get("pending", 0),
        "filings_not_in_facts": counts.get("not_in_facts", 0),
    }
