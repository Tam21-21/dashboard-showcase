"""The fundamentals batch write path + the orphan-batch reaper (M3.75 FUND-1 §3).

Modelled on the event write path's batch-as-lineage discipline (write_core.write_event_batch)
but fundamentals-shaped:

  write_fundamental_batch(...)
      Opens a research_fundamental_batches row (status='running'), then for each CIK fetches the
      CompanyFacts document (via an injected fetch_fn), parses it (parse_companyfacts), and
      INSERTs each FactRow IDEMPOTENTLY on the five-tuple key (§3.3 — value-checked, never a
      silent overwrite, never an unhandled IntegrityError). A fetch/parse failure for ONE CIK
      quarantines that CIK and the batch CONTINUES (the policy='quarantine' analogue). On close
      it writes the §3.4 coverage report, sets the batch status, and best-effort refreshes the
      §4 catalog handle.

  _reap_orphaned_fundamental_batches(...)
      CAS-flips a stale-heartbeat 'running' batch to 'failed' (failure_class=
      'OrphanedRunningBatch'), so a backfill killed by process death stops 409-blocking every
      future backfill. SIMPLER than the event reaper: NO event-rollback sweep, because
      fundamentals are append-only vintage-kept — a dead batch's already-written facts are valid
      filings, just incomplete coverage, so they stay.

Idempotency is the correctness spine: re-fetching a company re-presents its full CompanyFacts
history, so the only collision risk is across fetches; the per-FactRow SELECT-then-act on the
five-tuple is the §3.3 dedup (no row -> INSERT; same value -> skip; different value -> record a
source_contradiction and do NOT overwrite). No content-hash column is owed (the five-tuple pins
the filing).

The raw CompanyFacts document is parsed and then discarded — never stored (the §1 DDL has no
raw-document column; only the normalized core facts persist, the NFR-10 store-size decision).
"""
from __future__ import annotations

import json                                                      # serialize the §3.4 coverage report into quality_report
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

from ... import eventsdb
from ..errors import EdgarHttp5xx, EdgarHttp429Persistent, EdgarNetworkTimeout, EdgarParseFatal
from ..errors import EventStoreError, FundamentalParseError
from .catalog import refresh_fundamentals_catalog
from .concepts import FUNDAMENTAL_CONCEPTS, ConceptSpec
from .parse import CoverageContribution, FactRow, parse_companyfacts

log = logging.getLogger(__name__)

# The default stale-heartbeat threshold for the reaper (§3.3): a 'running' batch whose last
# heartbeat is older than this is presumed dead (process killed before it could close). 300s
# matches the event reaper's DEFAULT_HEARTBEAT_STALE_SECONDS so the two recovery paths agree.
DEFAULT_HEARTBEAT_STALE_SECONDS = 300

# A bounded cap on how many quarantine samples the coverage report embeds (the raw_excerpt /
# normalize_quarantines lists can grow with a bad batch; the full rows live in the quarantine
# table, the report carries only a readable sample). Mirrors the event report's bounded samples.
_MAX_QUARANTINE_SAMPLES = 20

# The fetch-time exception classes the fetch_fn may raise (the EdgarClient transport classes,
# §3.1). A fetch failure for one CIK quarantines that CIK with failure_step='fetch'; a parse
# failure raises FundamentalParseError (failure_step='parse'). These are SPECIFIC classes — no
# catch-all (CLAUDE.md): an unexpected class propagates and fails the batch loud.
_FETCH_FAILURE_CLASSES = (EdgarHttp5xx, EdgarHttp429Persistent, EdgarNetworkTimeout, EdgarParseFatal)


# Lane FA1b-1 report words (DECISIONS #479 item 10).
ACCEPTANCE_NOT_FETCHED = "acceptance times not fetched"
ACCEPTANCE_LABEL = "a figure with no acceptance time counts as known from the day after its filing date"


@dataclass
class FundamentalBatchResult:
    """The outcome of one write_fundamental_batch call (the batch-as-lineage summary)."""

    batch_id: int                                   # the research_fundamental_batches row id
    status: str                                     # 'succeeded' | 'succeeded_with_quarantines'
    fact_count: int                                 # core FactRows actually INSERTed (new vintages)
    cik_count: int                                  # CIKs attempted
    quarantine_count: int                           # research_fundamental_quarantines rows written
    quality_report: dict = field(default_factory=dict)  # the §3.4 coverage report JSON


def _now(now_epoch: Optional[int]) -> int:
    """Resolve the wall-clock for this write (injectable so the smoke test is deterministic)."""
    return now_epoch if now_epoch is not None else int(time.time())


def _open_batch(conn, *, kind: str, now_epoch: int) -> int:
    """INSERT a 'running' research_fundamental_batches row; return its batch_id.

    The lineage anchor every FactRow's batch_id FK points at (R6). source is the one fundamentals
    source ('sec_edgar', the F1 batch table's CHECK enum). last_heartbeat_at is seeded to
    started_at so the reaper's staleness window starts from the open, not from epoch 0.
    """
    cur = conn.execute(
        """INSERT INTO research_fundamental_batches
               (kind, source, started_at, last_heartbeat_at, status,
                cik_count, fact_count, quarantine_count)
           VALUES (?, 'sec_edgar', ?, ?, 'running', 0, 0, 0)""",
        (kind, now_epoch, now_epoch),
    )
    return int(cur.lastrowid)


def _heartbeat(conn, batch_id: int, now_epoch: int) -> None:
    """UPDATE last_heartbeat_at to now() for the open batch (the per-CIK liveness write, §3.2).

    This is what makes the §3.3 reaper able to tell a live multi-hundred-CIK backfill from a dead
    one: a live writer touches this column on every CIK, so its heartbeat stays fresh; a killed
    writer's heartbeat goes stale and the reaper can flip it.
    """
    conn.execute(
        "UPDATE research_fundamental_batches SET last_heartbeat_at = ? WHERE batch_id = ?",
        (now_epoch, batch_id),
    )


def _quarantine(conn, *, batch_id: int, cik: Optional[int], concept: Optional[str],
                failure_step: str, failure_class: str, failure_message: str,
                raw_excerpt: Optional[str], now_epoch: int) -> None:
    """INSERT one research_fundamental_quarantines row (failure_step in fetch/parse/normalize)."""
    conn.execute(
        """INSERT INTO research_fundamental_quarantines
               (batch_id, cik, concept, failure_step, failure_class,
                failure_message, raw_excerpt, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, cik, concept, failure_step, failure_class, failure_message,
         raw_excerpt, now_epoch),
    )


def _store_fact(conn, *, cik: int, batch_id: int, fact: FactRow, now_epoch: int,
                accepted_at: Optional[int] = None) -> tuple[str, Optional[dict]]:
    """Idempotently insert one FactRow on the six-tuple key (§3.3). Returns (outcome, contradiction).

    The correctness rule: SELECT value on the six-tuple
    (cik, concept, fiscal_period_code, period_end, filing_date, period_start; NULL start matches NULL,
    the schema 9 key of lane FA1a), then —
      - no row            -> INSERT, outcome 'inserted'
      - row, SAME value   -> skip (the idempotent re-fetch case), outcome 'skipped'; except that a stored
                             NULL accepted_at is filled when ``accepted_at`` is given, outcome
                             'accepted_at_filled' (a stored time is never changed; lane FA1b-1); a stored
                             NULL with no time to give is 'skipped_null'
      - row, DIFFERENT    -> do NOT overwrite; outcome 'contradiction' + the detail dict for the
                             coverage report (a genuine SEC self-contradiction to surface, arch §8;
                             never a silent overwrite, never an unhandled IntegrityError).
    NO content-hash column — the five-tuple already pins the filing.
    """
    existing = conn.execute(
        """SELECT fact_id, value, accepted_at FROM research_fundamental_facts
            WHERE cik = ? AND concept = ? AND fiscal_period_code = ?
              AND period_end = ? AND filing_date = ? AND period_start IS ?""",
        (cik, fact.concept, fact.fiscal_period_code, fact.period_end, fact.filing_date,
         fact.period_start),
    ).fetchone()

    if existing is None:                                            # a new vintage row -> INSERT
        conn.execute(
            """INSERT INTO research_fundamental_facts
                   (cik, concept, taxonomy, xbrl_tag, unit, fiscal_year, fiscal_period_code,
                    period_start, period_end, filing_date, value, source_form, accession_number,
                    batch_id, ingested_at, accepted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cik, fact.concept, fact.taxonomy, fact.xbrl_tag, fact.unit, fact.fiscal_year,
             fact.fiscal_period_code, fact.period_start, fact.period_end, fact.filing_date,
             fact.value, fact.source_form, fact.accession_number, batch_id, now_epoch, accepted_at),
        )
        return "inserted", None

    # A row already exists under the same five-tuple. Compare values (rounded to dodge float
    # representation noise, the parser's _select_canonical convention) to tell idempotent
    # re-fetch from a genuine source contradiction.
    if round(existing["value"], 9) == round(fact.value, 9):        # same filing, same value -> idempotent skip
        if existing["accepted_at"] is None and accepted_at is not None:   # fill a NULL, never change a time
            conn.execute("UPDATE research_fundamental_facts SET accepted_at = ? "
                         "WHERE fact_id = ? AND accepted_at IS NULL", (accepted_at, existing["fact_id"]))
            return "accepted_at_filled", None
        if existing["accepted_at"] is None:                          # same value, still no time to give
            return "skipped_null", None
        return "skipped", None

    # Same five-tuple, different value: a genuine SEC self-contradiction. Record it, do NOT
    # overwrite (the stored vintage stays). The detail is surfaced in the §3.4 coverage report.
    contradiction = {
        "cik": cik, "concept": fact.concept, "fiscal_period_code": fact.fiscal_period_code,
        "period_end": fact.period_end, "filing_date": fact.filing_date,
        "stored_value": existing["value"], "refetched_value": fact.value,
    }
    return "contradiction", contradiction


def _insert_fact_idempotent(conn, *, cik: int, batch_id: int, fact: FactRow, now_epoch: int,
                            accepted_at: Optional[int] = None) -> tuple[str, Optional[dict]]:
    """The §3.3 contract as FA1a's six-tuple probe reads it: _store_fact with 'skipped_null' folded into 'skipped'.

    The batch loop calls _store_fact, which tells a stored NULL acceptance time apart (for the missing count).
    """
    outcome, contradiction = _store_fact(conn, cik=cik, batch_id=batch_id, fact=fact, now_epoch=now_epoch,
                                         accepted_at=accepted_at)
    return ("skipped" if outcome == "skipped_null" else outcome), contradiction


def _build_coverage_report(
    *,
    contributions: list[CoverageContribution],
    ciks_attempted: int,
    ciks_resolved: int,
    facts_written: int,
    quarantine_count: int,
    source_contradictions: list[dict],
    quarantines_by_class: dict[str, int],
    quarantine_samples: list[dict],
    accepted_at_filled: int = 0,
    accepted_at_missing_by_cik: Optional[dict[int, int]] = None,
    acceptance_fetched: bool = True,
) -> dict:
    """Aggregate the per-company CoverageContributions into the §3.4 coverage report JSON.

    Per-concept and per-company (R6): the totals roll the per-company drop accounting up, the
    per_company list carries each company's found/missing concepts, shares-fallback tag, and
    foreign-currency units seen, and the quarantine accounting names the failure classes and a
    bounded sample. This is the "is this data correct?" signal the R18 inspector / coverage view
    assume exists — it makes the precision-vs-coverage iteration visible, never silent.
    """
    # Sum the per-company drop counters into the batch totals (the §3.4 shape).
    totals_non_usd = sum(c.counts["non_usd_dropped"] for c in contributions)
    totals_non_periodic = sum(c.counts["non_periodic_dropped"] for c in contributions)
    totals_non_core_fp = sum(c.counts["non_core_fp_dropped"] for c in contributions)
    totals_deferred = sum(c.counts["deferred_scope_dropped"] for c in contributions)
    totals_dimensional = sum(c.counts["dimensional_dropped"] for c in contributions)
    # The shares-fallback signal: count companies that used the us-gaap fallback (not the dei tag).
    shares_fallback_used = sum(1 for c in contributions if c.shares_tag.startswith("us-gaap:"))

    missing_by_cik = accepted_at_missing_by_cik or {}
    per_company = [
        {
            "cik": c.cik,
            "accepted_at_missing": missing_by_cik.get(c.cik, 0),
            "concepts_found": c.concepts_found,
            "concepts_missing": c.concepts_missing,
            "shares_tag": c.shares_tag,
            "non_usd_units_seen": c.non_usd_units_seen,
            # ANA-A: a foreign private issuer that files only under ifrs-full (no us-gaap), so our
            # concepts cannot resolve it. Surfaced in the fetch outcome as an honest "reports under
            # IFRS — not supported yet", told apart from a genuine no-data result.
            "ifrs_only": c.reports_ifrs_only,
        }
        for c in contributions
    ]

    return {
        "shape": "fundamentals_coverage",
        "totals": {
            "ciks_attempted": ciks_attempted,
            "ciks_resolved": ciks_resolved,
            "facts_written": facts_written,
            "quarantines": quarantine_count,
            "non_usd_dropped": totals_non_usd,
            "non_periodic_dropped": totals_non_periodic,
            "non_core_fp_dropped": totals_non_core_fp,
            "deferred_scope_dropped": totals_deferred,
            "dimensional_dropped": totals_dimensional,
            "shares_fallback_used": shares_fallback_used,
            "source_contradictions": len(source_contradictions),
            "accepted_at_missing": sum(missing_by_cik.values()),
            "accepted_at_filled": accepted_at_filled,
        },
        # Lane FA1b-1: what the acceptance column means in this batch. A figure with no acceptance time is
        # not dropped; reads treat it as known from the day after its filing date.
        "acceptance_status": "acceptance times fetched" if acceptance_fetched else ACCEPTANCE_NOT_FETCHED,
        "acceptance_label": ACCEPTANCE_LABEL,
        "per_company": per_company,
        "quarantines_by_class": quarantines_by_class,
        "quarantines_samples": quarantine_samples[:_MAX_QUARANTINE_SAMPLES],
        # The genuine SEC self-contradictions (same five-tuple, different value) — a bounded
        # sample; surfaced so a data-integrity signal is visible, never a silent no-op (§3.3).
        "source_contradictions_samples": source_contradictions[:_MAX_QUARANTINE_SAMPLES],
    }


def write_fundamental_batch(
    events_db_path: Union[str, Path, None] = None,
    *,
    kind: str,
    ciks: Sequence[int],
    fetch_fn: Callable[[int], dict],
    now_epoch: Optional[int] = None,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
    research_db_path: Union[str, Path, None] = None,
    acceptance_fn: Optional[Callable[[int, dict], dict]] = None,
) -> FundamentalBatchResult:
    """Fetch, parse, and idempotently store the fundamentals for ``ciks`` as one batch (§3.2).

    ``fetch_fn(cik) -> dict`` is INJECTED so the caller supplies the transport: the dispatch job
    wires it to EdgarClient.companyfacts_url + client.get + json.loads, the probe / smoke test
    wires it to the cached fixture loader. This keeps the write path transport-agnostic and
    testable without a network.

    Per CIK: fetch -> parse_companyfacts -> idempotent INSERT of each FactRow. A fetch/parse
    failure for ONE CIK quarantines that CIK (failure_step in fetch/parse) and the batch
    CONTINUES — never aborts the whole batch on one company. The parser's own genuine-ambiguity
    cases (CoverageContribution.normalize_quarantines) are also written as quarantine rows
    (failure_step='normalize'). On clean close the batch status is 'succeeded' (no quarantines)
    or 'succeeded_with_quarantines', the §3.4 coverage report lands in quality_report, and the §4
    catalog handle is best-effort refreshed (a failed refresh is logged-not-fatal; the reconcile
    pass repairs drift).

    ``acceptance_fn(cik, needed) -> {accession: epoch or None}`` (lane FA1b-1) gives each filing's SEC
    acceptance time; ``needed`` maps each accession of the parsed facts to its filing date. It is called per
    CIK after the parse and before any write for that CIK. A failure of the fetch classes quarantines the CIK
    like a failed fetch (step 'fetch'), so a read failure never writes NULL. None stores NULL on every row
    and the report says "acceptance times not fetched". On the same six-tuple and value a stored NULL is
    filled with a given time; a stored time is never changed.

    ``kind`` must be 'fetch' or 'refresh'(the F1 batch table's CHECK enum; fundamentals have no
    correction/rollback at M3.75). A bad kind fails loud here before any row is written.
    """
    if kind not in ("fetch", "refresh"):                            # fail loud on a bad kind (the CHECK enum)
        raise ValueError(f"write_fundamental_batch requires kind in (fetch, refresh); got {kind!r}")

    open_epoch = _now(now_epoch)                                    # the batch-open clock

    # Open ONE connection for the whole batch (not one per CIK — the §3.2 scalability point a
    # multi-hundred-CIK backfill needs). The write lock is held ONLY inside each transaction_on()
    # block and released between CIKs, so the slow network fetches (which happen OUTSIDE
    # transaction_on) never hold the lock — a single held BEGIN across the fetches would block
    # every other writer for the whole backfill. The connection is closed when this `with` exits.
    contributions: list[CoverageContribution] = []                 # one per CIK that fetched+parsed
    source_contradictions: list[dict] = []                         # same-five-tuple-different-value cases (§3.3)
    quarantines_by_class: dict[str, int] = {}                      # failure_class -> count (§3.4)
    quarantine_samples: list[dict] = []                           # a bounded readable sample
    facts_written = 0                                             # new vintage rows actually INSERTed
    quarantine_count = 0                                          # quarantine rows written this batch
    ciks_resolved = 0                                            # CIKs whose document fetched+parsed
    accepted_at_filled = 0                                       # stored NULL times filled this batch (lane FA1b-1)
    accepted_at_missing_by_cik: dict[int, int] = {}              # presented facts left with no acceptance time

    def _record_quarantine_class(failure_class: str, sample: dict) -> None:
        """Tally a quarantine by class and keep a bounded sample (§3.4 accounting)."""
        quarantines_by_class[failure_class] = quarantines_by_class.get(failure_class, 0) + 1
        if len(quarantine_samples) < _MAX_QUARANTINE_SAMPLES:
            quarantine_samples.append(sample)

    with eventsdb.get_events_conn(events_db_path) as conn:          # the one held connection for the batch
        # Open the batch row in its own short transaction so the batch_id exists for any subsequent
        # quarantine row even if a later step fails (the write_core._open_batch discipline).
        with eventsdb.transaction_on(conn):
            batch_id = _open_batch(conn, kind=kind, now_epoch=open_epoch)

        # The per-CIK loop. Each CIK gets its OWN short transaction (on the held connection) so a
        # fetch/parse failure on one company quarantines that company without rolling back the prior
        # companies' committed facts (the batch-continues-on-one-failure rule, §3.2).
        for cik in ciks:
            cik_epoch = _now(now_epoch)                            # the per-CIK clock (also the heartbeat)
            # 1. Fetch the CompanyFacts document via the injected transport — OUTSIDE any
            #    transaction, so the write lock is released during the slow fetch. A fetch failure
            #    (the Edgar* classes) quarantines this CIK with failure_step='fetch' and continues.
            try:
                doc = fetch_fn(cik)
            except _FETCH_FAILURE_CLASSES as exc:                  # a SPECIFIC fetch failure (not a catch-all)
                failure_class = type(exc).__name__
                with eventsdb.transaction_on(conn):
                    _quarantine(
                        conn, batch_id=batch_id, cik=cik, concept=None,
                        failure_step="fetch", failure_class=failure_class,
                        failure_message=f"fetch failed for cik {cik}: {exc}",
                        raw_excerpt=None, now_epoch=cik_epoch,
                    )
                    _heartbeat(conn, batch_id, cik_epoch)         # liveness even on a failed CIK
                quarantine_count += 1
                _record_quarantine_class(failure_class, {
                    "cik": cik, "failure_step": "fetch", "failure_class": failure_class,
                    "failure_message": f"fetch failed for cik {cik}: {exc}",
                })
                log.warning("fundamentals fetch failed for cik=%s class=%s", cik, failure_class)
                continue

            # 2. Parse the document into normalized FactRows + a coverage contribution. A structural
            #    parse failure raises FundamentalParseError (failure_step='parse') and continues.
            try:
                if not isinstance(doc, dict):                      # a non-dict document is structurally unusable
                    raise FundamentalParseError(
                        f"CompanyFacts for cik {cik} is not a dict (got {type(doc).__name__})")
                facts, cov = parse_companyfacts(cik, doc, concepts=concepts)
            except FundamentalParseError as exc:                  # a SPECIFIC parse failure (not a catch-all)
                failure_class = type(exc).__name__
                with eventsdb.transaction_on(conn):
                    _quarantine(
                        conn, batch_id=batch_id, cik=cik, concept=None,
                        failure_step="parse", failure_class=failure_class,
                        failure_message=f"parse failed for cik {cik}: {exc}",
                        raw_excerpt=None, now_epoch=cik_epoch,
                    )
                    _heartbeat(conn, batch_id, cik_epoch)
                quarantine_count += 1
                _record_quarantine_class(failure_class, {
                    "cik": cik, "failure_step": "parse", "failure_class": failure_class,
                    "failure_message": f"parse failed for cik {cik}: {exc}",
                })
                log.warning("fundamentals parse failed for cik=%s class=%s", cik, failure_class)
                continue

            # 2b. Acceptance times (lane FA1b-1), OUTSIDE any transaction like the fetch. A failure of the
            #     fetch classes quarantines this CIK as a fetch failure and writes none of its rows: a read
            #     failure must never store NULL.
            times: dict = {}
            if acceptance_fn is not None:
                needed: dict[str, int] = {}
                for fact in facts:
                    if fact.accession_number is not None:
                        needed[fact.accession_number] = min(
                            fact.filing_date, needed.get(fact.accession_number, fact.filing_date))
                try:
                    times = acceptance_fn(cik, needed)
                except _FETCH_FAILURE_CLASSES as exc:            # a SPECIFIC read failure (not a catch-all)
                    failure_class = type(exc).__name__
                    with eventsdb.transaction_on(conn):
                        _quarantine(
                            conn, batch_id=batch_id, cik=cik, concept=None,
                            failure_step="fetch", failure_class=failure_class,
                            failure_message=f"acceptance times failed for cik {cik}: {exc}",
                            raw_excerpt=None, now_epoch=cik_epoch,
                        )
                        _heartbeat(conn, batch_id, cik_epoch)
                    quarantine_count += 1
                    _record_quarantine_class(failure_class, {
                        "cik": cik, "failure_step": "fetch", "failure_class": failure_class,
                        "failure_message": f"acceptance times failed for cik {cik}: {exc}",
                    })
                    log.warning("fundamentals acceptance times failed for cik=%s class=%s", cik, failure_class)
                    continue

            ciks_resolved += 1                                    # this CIK fetched+parsed cleanly
            contributions.append(cov)                             # roll its coverage into the close

            # 3. Idempotently INSERT each FactRow + write the parser's normalize-quarantines, all in
            #    this CIK's transaction. The heartbeat is updated inside it too (per-CIK liveness).
            with eventsdb.transaction_on(conn):
                for fact in facts:
                    given = times.get(fact.accession_number) if fact.accession_number is not None else None
                    outcome, contradiction = _store_fact(
                        conn, cik=cik, batch_id=batch_id, fact=fact, now_epoch=cik_epoch, accepted_at=given)
                    if outcome == "inserted":                     # a new vintage row landed
                        facts_written += 1
                        if given is None:
                            accepted_at_missing_by_cik[cik] = accepted_at_missing_by_cik.get(cik, 0) + 1
                    elif outcome == "accepted_at_filled":         # a stored NULL got its time
                        accepted_at_filled += 1
                    elif outcome == "skipped_null":               # same value, still no time to give
                        accepted_at_missing_by_cik[cik] = accepted_at_missing_by_cik.get(cik, 0) + 1
                    elif outcome == "contradiction":              # same five-tuple, different value (§3.3)
                        source_contradictions.append(contradiction)
                    # 'skipped' (idempotent re-fetch) is silent by design — it is the success case.

                # The parser's genuine-ambiguity cases become normalize quarantine rows (§3.2). They
                # carry the concept + period_end/filing_date the parser captured.
                for nq in cov.normalize_quarantines:
                    _quarantine(
                        conn, batch_id=batch_id, cik=cik, concept=nq.get("concept"),
                        failure_step="normalize", failure_class="FundamentalParseError",
                        failure_message=nq.get("failure_message", "normalize ambiguity"),
                        raw_excerpt=None, now_epoch=cik_epoch,
                    )
                    quarantine_count += 1
                    _record_quarantine_class("FundamentalParseError", {
                        "cik": cik, "failure_step": "normalize",
                        "failure_class": "FundamentalParseError",
                        "failure_message": nq.get("failure_message", "normalize ambiguity"),
                    })

                _heartbeat(conn, batch_id, cik_epoch)             # the per-CIK liveness write

        # Close the batch. Status is succeeded_with_quarantines when any quarantine was written
        # (fetch/parse/normalize), else succeeded. The coverage report lands in quality_report.
        close_epoch = _now(now_epoch)
        status = "succeeded_with_quarantines" if quarantine_count > 0 else "succeeded"
        report = _build_coverage_report(
            contributions=contributions,
            ciks_attempted=len(ciks),
            ciks_resolved=ciks_resolved,
            facts_written=facts_written,
            quarantine_count=quarantine_count,
            source_contradictions=source_contradictions,
            quarantines_by_class=quarantines_by_class,
            quarantine_samples=quarantine_samples,
            accepted_at_filled=accepted_at_filled,
            accepted_at_missing_by_cik=accepted_at_missing_by_cik,
            acceptance_fetched=acceptance_fn is not None,
        )
        with eventsdb.transaction_on(conn):
            conn.execute(
                """UPDATE research_fundamental_batches
                      SET status = ?, finished_at = ?, last_heartbeat_at = ?,
                          cik_count = ?, fact_count = ?, quarantine_count = ?,
                          quality_report = ?
                    WHERE batch_id = ?""",
                (status, close_epoch, close_epoch, len(ciks), facts_written, quarantine_count,
                 json.dumps(report), batch_id),
            )

    # Best-effort catalog refresh after a clean close (§3.2 step 4 / §4.3). A failed refresh is
    # logged-not-fatal: the batch already committed its facts, and reconcile_fundamentals_catalog
    # at startup repairs any drift. The catalog refresh is NOT inside the batch transaction (it
    # opens its own research.db connection + ATTACHes events.db).
    try:
        refresh_fundamentals_catalog(
            events_db_path=events_db_path, research_db_path=research_db_path)
    except EventStoreError:
        # An event-store-originated failure (e.g. an unregistered dataset_type guard) — log it,
        # do not fail the batch; reconcile is the backstop.
        log.warning("refresh_fundamentals_catalog failed after batch %d close; "
                    "reconcile at startup will repair", batch_id, exc_info=True)
    except (OSError, ValueError) as exc:
        # A research.db path / ATTACH-literal problem (OSError) or a risky-path refusal
        # (ValueError from _safe_attach_path). Same logged-not-fatal treatment (§4.3).
        log.warning("refresh_fundamentals_catalog failed after batch %d close: %s; "
                    "reconcile at startup will repair", batch_id, exc, exc_info=True)

    return FundamentalBatchResult(
        batch_id=batch_id, status=status, fact_count=facts_written,
        cik_count=len(ciks), quarantine_count=quarantine_count, quality_report=report,
    )


def _reap_orphaned_fundamental_batches(
    events_db_path: Union[str, Path, None] = None,
    *,
    heartbeat_stale_seconds: int = DEFAULT_HEARTBEAT_STALE_SECONDS,
) -> dict:
    """CAS-flip stale-heartbeat 'running' fundamentals batches to 'failed' (§3.3).

    A fundamentals batch killed by process death (deploy SIGTERM, OOM, crash) before the write
    path closes it leaves status='running', and the 409 guard then blocks every future backfill
    with no recovery — exactly the bug the event reaper exists for. This flips a 'running' batch
    whose last_heartbeat_at is older than ``heartbeat_stale_seconds`` to 'failed'
    (failure_class='OrphanedRunningBatch') via a CAS UPDATE on status='running', so a
    genuinely-live backfill with a recent heartbeat is NEVER touched.

    SIMPLER than the event reaper: NO secondary event-rollback sweep, because fundamentals are
    append-only vintage-kept — a dead batch's already-written facts are valid filings (just
    incomplete coverage), so they stay. Idempotent: a re-run finds nothing stale and is a no-op.
    """
    cutoff = int(time.time()) - heartbeat_stale_seconds            # heartbeats older than this are stale
    now_epoch = int(time.time())
    with eventsdb.events_transaction(events_db_path) as conn:
        # The CAS UPDATE: only rows still 'running' AND stale are flipped. RETURNING gives the
        # rows the UPDATE actually touched, so the reap is atomic at the row level (the
        # sweeps.reap_orphaned_running_batches pattern, minus the event rollback).
        rows = conn.execute(
            """UPDATE research_fundamental_batches
                  SET status        = 'failed',
                      failure_class = 'OrphanedRunningBatch',
                      finished_at   = ?
                WHERE status = 'running'
                  AND last_heartbeat_at < ?
            RETURNING batch_id""",
            (now_epoch, cutoff),
        ).fetchall()
        reaped_ids = [int(r["batch_id"]) for r in rows]

    if reaped_ids:                                                 # only log when something was actually reaped
        log.warning(
            "reap_orphaned_fundamental_batches: flipped %d batch(es) to failed/"
            "OrphanedRunningBatch (stale_seconds=%d)",
            len(reaped_ids), heartbeat_stale_seconds,
        )
    return {"batches_reaped": reaped_ids}
