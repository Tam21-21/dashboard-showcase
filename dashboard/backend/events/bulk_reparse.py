"""M2.5 brief G — bulk re-parse: company-change-aware correction batch dispatch.

Spec brief v4 §11 brief G row + arch v6 §5.4 round-5 correction 2.

The operator's workflow:
  1. A parser bug is discovered in dashboard/backend/events/parsers/sec_13d_cover.py
     (or any other event-type parser).
  2. After fixing the parser, the operator wants to re-process every active
     event in some affected batch under the new parser. For each event:
       - If the re-parsed (cik, role, cusip) set MATCHES the stored set,
         the change is a payload-only fix → apply_correction is the right
         tool (preserves the natural_key / company link / content-hash chain).
       - If the re-parsed set DIFFERS from the stored set, the parser bug
         had cross-cutting effects on the entity link too. apply_correction's
         CorrectionAltersCompanyLinks guard would refuse it; the right tool
         is rollback-and-re-ingest. The operator confirms before re-ingest.

The bulk re-parser:
  - Reads active events under target_batch_id
  - For each event with source='sec_edgar', reads its corresponding
    pre-staged .txt file from `reparse_dir` (file named by accession_number),
    parses it via parsers/sec_13d_cover.parse_13d_submission, builds the
    new (cik, role, cusip) set
  - Compares to stored set from research_event_companies
  - Routes correction-candidates vs rollback-candidates
  - If apply=True, walks the correction-candidates calling apply_correction
    one-at-a-time (each one opens its own correction-batch row); the
    rollback-candidates are NOT applied here — the operator runs the
    rollback admin endpoint separately so they can confirm the
    typed-confirmation phrase per EL42.

Why pre-staged .txt files rather than live EDGAR fetching: the operator's
re-fetch step is best done as a separate workflow (rate-limited, retriable,
auditable). The CLI is the "compare + correction" engine; rate-limited
fetching is brief D's territory and would duplicate machinery here.

Why a separate module (not inline in cli.py): the probe layer needs to
unit-test the routing logic without invoking the CLI's argparse layer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .. import eventsdb
from . import write_core
from .errors import CorrectionAgainstRunningBatch, CorrectionAltersCompanyLinks
from .parsers import sec_13d_cover

log = logging.getLogger(__name__)


@dataclass
class BulkReparseResult:
    """Diagnostic + outcome shape returned from `bulk_reparse`."""
    scanned: int = 0
    skipped_missing_file: int = 0
    skipped_parse_failure: int = 0
    correction_candidates: list[dict] = field(default_factory=list)
    rollback_candidates: list[dict] = field(default_factory=list)
    corrections_applied: int = 0
    correction_batch_ids: list[int] = field(default_factory=list)
    correction_failures: list[dict] = field(default_factory=list)


def _stored_companies_set(conn, event_id: int) -> set[tuple[int, str, Optional[str]]]:
    rows = conn.execute(
        "SELECT cik, role, cusip FROM research_event_companies WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    return {(int(r["cik"]), r["role"], r["cusip"]) for r in rows}


def _new_companies_set(companies: list[dict]) -> set[tuple[int, str, Optional[str]]]:
    return {(int(c["cik"]), c["role"], c.get("cusip")) for c in companies}


def _payload_diff_keys(stored_payload: dict, new_payload: dict) -> list[str]:
    """Top-level keys whose values differ between stored and new payload."""
    return sorted(
        k for k in set(stored_payload) | set(new_payload)
        if stored_payload.get(k) != new_payload.get(k)
    )


def bulk_reparse(
    *,
    batch_id: int,
    reparse_dir: Union[str, Path],
    events_db_path: Union[str, Path, None] = None,
    apply: bool = False,
    max_events: Optional[int] = None,
    trigger_note_prefix: str = "bulk_reparse",
) -> BulkReparseResult:
    """Walk the batch's active events; route correction vs rollback candidates.

    `reparse_dir` is expected to contain `<accession_number>.txt` files
    (one per event the operator wants to re-parse). Events whose file is
    absent are SKIPPED with `skipped_missing_file` bumped; the operator
    can do a follow-up run for the missing ones.

    `apply=False` (default): dry-run; nothing is written; the result's
    `correction_candidates` and `rollback_candidates` lists give the
    operator a preview.

    `apply=True`: for each correction_candidate, calls apply_correction
    (which opens a new correction-batch row, writes a history snapshot,
    updates the current event row in-place). Each call is independent;
    a failure on one event is logged in `correction_failures` and the
    loop continues. The rollback_candidates list is NEVER applied here —
    the operator must run POST /event-batches/{id}/rollback with the
    typed-confirmation phrase per EL42.

    Only `event_type='sec_13d'` is supported at M2.5 (the only event type
    with a brief-D parser); analyst_rating events are skipped with a
    note.
    """
    reparse_dir = Path(reparse_dir)
    if not reparse_dir.is_dir():
        raise ValueError(f"bulk_reparse: reparse_dir {reparse_dir} is not a directory")

    result = BulkReparseResult()

    # 1. Enumerate active events under the batch.
    with eventsdb.get_events_conn(events_db_path) as conn:
        rows = conn.execute(
            """SELECT event_id, event_type, natural_key, payload, source,
                      raw_input
                 FROM research_events
                WHERE batch_id = ? AND status = 'active'
                ORDER BY event_id ASC""",
            (batch_id,),
        ).fetchall()

    for row in rows:
        if max_events is not None and result.scanned >= max_events:
            break
        result.scanned += 1

        event_type = row["event_type"]
        natural_key = row["natural_key"]
        event_id = row["event_id"]

        if event_type != "sec_13d":
            # Only 13D has a brief D parser; analyst_rating re-parse needs
            # the classifier (out of M2.5 scope per DECISIONS #31).
            continue

        # natural_key is `filer_cik_accession_number` per EL12; the file the
        # operator pre-staged is keyed by accession_number alone (one filing
        # per file, regardless of which filer claims it).
        try:
            filer_cik_str, accession_number = natural_key.split("_", 1)
            filer_cik = int(filer_cik_str)
        except ValueError:
            log.warning("bulk_reparse: event_id=%d natural_key=%r is not in "
                        "<filer_cik>_<accession_number> form; skipping",
                        event_id, natural_key)
            result.skipped_parse_failure += 1
            continue

        staged = reparse_dir / f"{accession_number}.txt"
        if not staged.exists():
            result.skipped_missing_file += 1
            continue

        # 2. Re-parse the staged file using the brief-D parser.
        try:
            submission_text = staged.read_text(encoding="utf-8")
            record = sec_13d_cover.parse_13d_submission(
                submission_text,
                expected_filer_cik=filer_cik,
                expected_accession_number=accession_number,
            )
        except Exception as exc:
            # Parser raised; log + count + skip. The operator's re-fetch
            # may have grabbed a wrong file or the parser still has a bug
            # against this filing's shape.
            log.warning("bulk_reparse: event_id=%d re-parse failed: %s",
                        event_id, exc)
            result.skipped_parse_failure += 1
            continue

        # 3. Compare company sets.
        new_companies = record.get("companies") or []
        new_set = _new_companies_set(new_companies)
        with eventsdb.get_events_conn(events_db_path) as conn:
            stored_set = _stored_companies_set(conn, event_id)

        if stored_set != new_set:
            # Cross-cutting parser bug: company links would change. Route
            # OUT of the correction batch and into the operator's rollback
            # queue. The operator runs POST /event-batches/{id}/rollback
            # then re-ingests under a fresh fetch batch.
            result.rollback_candidates.append({
                "event_id": event_id,
                "natural_key": natural_key,
                "stored_companies": [
                    {"cik": c, "role": r, "cusip": u}
                    for (c, r, u) in sorted(stored_set)
                ],
                "new_companies": [
                    {"cik": c, "role": r, "cusip": u}
                    for (c, r, u) in sorted(new_set)
                ],
            })
            continue

        # 4. Company set unchanged → correction candidate. Compute the
        # payload diff for the operator preview.
        stored_payload = json.loads(row["payload"])
        new_payload = record.get("payload") or {}
        diff_keys = _payload_diff_keys(stored_payload, new_payload)
        if not diff_keys:
            # Re-parse produced the same payload; no correction needed.
            continue

        result.correction_candidates.append({
            "event_id": event_id,
            "natural_key": natural_key,
            "payload_diff_keys": diff_keys,
            "new_payload": new_payload,
            "stored_payload": stored_payload,
            # The companies list is the same as stored (we verified above).
            "companies": [
                {"cik": c, "role": r, "cusip": u}
                for (c, r, u) in sorted(stored_set)
            ],
        })

    # 5. Apply (optional). Each call opens its own correction-batch row.
    if apply:
        trigger_note = f"{trigger_note_prefix}: re-parse against batch_id={batch_id}"
        for cand in result.correction_candidates:
            try:
                br = write_core.apply_correction(
                    events_db_path=events_db_path,
                    event_id=cand["event_id"],
                    corrected_payload=cand["new_payload"],
                    companies=cand["companies"],
                    trigger_note=trigger_note,
                )
            except CorrectionAgainstRunningBatch as exc:
                result.correction_failures.append({
                    "event_id": cand["event_id"],
                    "natural_key": cand["natural_key"],
                    "class": "CorrectionAgainstRunningBatch",
                    "message": str(exc),
                })
                continue
            except CorrectionAltersCompanyLinks as exc:
                # Should not happen — we already verified set equality. But
                # if a concurrent write modified the companies between our
                # SELECT and apply_correction, surface honestly.
                result.correction_failures.append({
                    "event_id": cand["event_id"],
                    "natural_key": cand["natural_key"],
                    "class": "CorrectionAltersCompanyLinks",
                    "added": exc.added,
                    "removed": exc.removed,
                })
                continue
            except Exception as exc:
                result.correction_failures.append({
                    "event_id": cand["event_id"],
                    "natural_key": cand["natural_key"],
                    "class": type(exc).__name__,
                    "message": str(exc)[:512],
                })
                continue
            result.correction_batch_ids.append(br.batch_id)
            result.corrections_applied += 1

    return result
