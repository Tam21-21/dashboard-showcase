"""Map a raw Benzinga rating to a canonical analyst_rating v2 event RECORD, and resolve its
raw ticker to a CIK. The mapper is pure (no I/O); resolution reads research_cik_ticker_map.

Field mapping (verified against the live Benzinga shape + the v2 schema, §0 of the spec brief):
  id            -> producer_id / natural_key (the stable dedup key)
  updated       -> knowledge_at AND occurred_at (the authoritative UTC instant; the Eastern
                   date/time fields are NEVER used — Benzinga itself flags them unusable)
  ticker        -> payload.raw_ticker (cik + resolved_at injected at resolution)
  action_company-> payload.action_company (the upgrade/downgrade signal)
  rating_current-> payload.new_grade
  rating_prior  -> payload.old_grade (omitted when blank — an initiation)
  analyst       -> payload.analyst_firm (the issuing firm; the required identity)
  analyst_name  -> payload.analyst_person (the named analyst; optional)
  pt_current    -> payload.pt_current (parsed float; '' -> omitted)
  pt_prior      -> payload.pt_prior
  importance    -> payload.importance (int)
  exchange      -> payload.exchange
The full raw rating rides research_events.raw_input as a JSON string (audit / re-map, Q3).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from ..errors import TickerNotFoundInBridge
from ..tickers import normalize_ticker                          # canonical bridge-key lookup (item 1 / B1)

# The source/version stamp recorded on every Benzinga-sourced event (Q3).
PRODUCER_MODEL_VERSION = "benzinga-calendar/v2.1"


class BenzingaRatingUnmappable(ValueError):
    """A raw Benzinga rating lacks a field required to build a canonical record (-> quarantine)."""


def _opt_float(value: object) -> Optional[float]:
    """Parse a Benzinga price-target value to float; None / '' -> None (the field is omitted)."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError as exc:
        raise BenzingaRatingUnmappable(f"price target not a number: {value!r}") from exc


def _opt_str(value: object) -> Optional[str]:
    """A trimmed non-empty string, or None (so a blank Benzinga field becomes an absent key)."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def map_rating(raw: dict) -> dict:
    """Build a canonical analyst_rating v2 RECORD dict from a raw Benzinga rating.

    Raises BenzingaRatingUnmappable when a required field is missing/blank. The resolved CIK
    and resolved_at are injected later by resolve_record, so companies[0]['cik'] is a None
    placeholder here and payload has no resolved_at until resolution.
    """
    def req(key: str) -> object:
        val = raw.get(key)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            raise BenzingaRatingUnmappable(f"missing required Benzinga field: {key!r}")
        return val

    rating_id = str(req("id"))                                 # the stable dedup key
    ticker = str(req("ticker")).strip()
    try:
        updated_epoch = int(req("updated"))                    # the authoritative UTC instant
    except (TypeError, ValueError) as exc:
        raise BenzingaRatingUnmappable(f"updated not an int epoch: {raw.get('updated')!r}") from exc

    payload: dict = {
        "raw_ticker": ticker,
        "action_company": str(req("action_company")).strip(),  # the signal
        "new_grade": str(req("rating_current")).strip(),
        "analyst_firm": str(req("analyst")).strip(),           # the firm name (present on every row)
        "producer_model_version": PRODUCER_MODEL_VERSION,
        # resolved_at is injected by resolve_record (loader-injected, per the v2 contract)
    }
    old_grade = _opt_str(raw.get("rating_prior"))              # absent on an initiation
    if old_grade is not None:
        payload["old_grade"] = old_grade
    person = _opt_str(raw.get("analyst_name"))                 # the named analyst (optional)
    if person is not None:
        payload["analyst_person"] = person
    pt_current = _opt_float(raw.get("pt_current"))
    if pt_current is not None:
        payload["pt_current"] = pt_current
    pt_prior = _opt_float(raw.get("pt_prior"))
    if pt_prior is not None:
        payload["pt_prior"] = pt_prior
    importance = raw.get("importance")
    if importance is not None and importance != "":
        try:
            payload["importance"] = int(importance)
        except (TypeError, ValueError) as exc:
            raise BenzingaRatingUnmappable(f"importance not an int: {importance!r}") from exc
    exchange = _opt_str(raw.get("exchange"))
    if exchange is not None:
        payload["exchange"] = exchange

    return {
        "event_type": "analyst_rating",
        "producer_id": rating_id,
        "natural_key": rating_id,                              # EL13: analyst natural_key == producer_id
        "knowledge_at": updated_epoch,                         # when we could know it
        "occurred_at": updated_epoch,                          # the rating's occurrence == its update instant
        "source": "benzinga",
        "origin": "fetched",
        "payload_schema_version": 2,
        "raw_input": json.dumps(raw, sort_keys=True),          # the audit copy (rides research_events.raw_input)
        "payload": payload,
        "companies": [{"cik": None, "role": "primary_issuer", "cusip": None}],  # cik filled by resolve_record
    }


def resolve_ticker_to_cik(conn: sqlite3.Connection, raw_ticker: str) -> int:
    """Resolve a raw ticker to a CIK against research_cik_ticker_map (current snapshot).

    Mirrors jsonl_canonical._resolve_company_ciks's lookup (effective_from IS NULL, newest
    loaded_at). Raises TickerNotFoundInBridge when no row matches (the caller quarantines).
    """
    row = conn.execute(
        "SELECT cik FROM research_cik_ticker_map "
        "WHERE ticker = ? AND effective_from IS NULL "
        "ORDER BY loaded_at DESC LIMIT 1",
        (normalize_ticker(raw_ticker),),                       # canonical bridge key (item 1 / B1)
    ).fetchone()
    if row is None:
        raise TickerNotFoundInBridge(
            f"raw_ticker={raw_ticker!r} has no row in research_cik_ticker_map "
            f"(current-snapshot lookup; effective_from IS NULL)"
        )
    return row["cik"]


def resolve_record(conn: sqlite3.Connection, record: dict, *, now_epoch: int) -> dict:
    """Inject the resolved CIK + resolved_at into a mapped record (mutates it) and return it.

    Raises TickerNotFoundInBridge if the ticker is unresolvable (the caller quarantines the row).
    """
    cik = resolve_ticker_to_cik(conn, record["payload"]["raw_ticker"])
    record["companies"][0]["cik"] = cik                        # fill the primary-issuer link
    record["payload"]["resolved_at"] = now_epoch               # the loader-injected resolution instant
    return record
