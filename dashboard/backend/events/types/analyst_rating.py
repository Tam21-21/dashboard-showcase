"""Spec for the `analyst_rating` event type. Spec brief §4.2 / MN13; evolved to
payload schema version 2 by the Benzinga ingestion unit (the multi-source channel).

Payload shape (version 2, post-loader):
  raw_ticker:             str   (REQUIRED — kept per R6 even after resolution)
  action_company:         str   (REQUIRED — the rating action: Upgrades/Downgrades/
                                 Maintains/Reiterates/Initiates/… kept a free non-empty
                                 string, not a hard enum, so an unseen Benzinga action
                                 ingests rather than spuriously quarantining)
  new_grade:              str   (REQUIRED — the current rating)
  analyst_firm:           str   (REQUIRED — the issuing firm, the signal)
  producer_model_version: str   (REQUIRED — the source/version stamp, e.g.
                                 "benzinga-calendar/v2.1")
  resolved_at:            int   (REQUIRED, LOADER-INJECTED, not producer-supplied)
  old_grade:              str   (OPTIONAL — absent for an initiation, no prior grade)
  analyst_person:         str   (OPTIONAL — the named analyst; occasionally blank)
  pt_current:             float (OPTIONAL — current price target)
  pt_prior:               float (OPTIONAL — prior price target)
  importance:             int   (OPTIONAL — 0..5)
  exchange:               str   (OPTIONAL — listing exchange)

`resolved_at` is the wall-clock instant the loader resolved `raw_ticker → CIK`
against `research_cik_ticker_map`. Producer-supplied `resolved_at` is rejected at
the envelope; by the time the validator sees a record, the loader has injected it.

`producer_id` is NOT in the payload — it is the natural_key (EL13) and lives at
`research_events.producer_id` (for Benzinga, the stable Benzinga rating `id`).

Version evolution (DDIA Ch 5): wholesale v2, validated as a single version. There
are zero v1 records in production, so the v1 `analyst_name` field is redefined to
`analyst_firm` (+ the new `analyst_person`) with no alias needed, and the new
required `action_company` does not break a backward compatibility that does not
exist. The version-aware DISPATCH (per-record `payload_schema_version` branching)
is deferred until a real future version must coexist with v2.
"""
from __future__ import annotations

from ..errors import (
    MissingRequiredField,
    UnknownField,
    WrongFieldType,
)


PAYLOAD_SCHEMA_VERSION = 2


REQUIRED_FIELDS: dict[str, type] = {
    "raw_ticker": str,
    "action_company": str,
    "new_grade": str,
    "analyst_firm": str,
    "producer_model_version": str,
    "resolved_at": int,
}

OPTIONAL_FIELDS: dict[str, type] = {
    "old_grade": str,        # absent for an analyst-initiation event (no prior grade)
    "analyst_person": str,   # the named analyst; occasionally blank at the source
    "pt_current": float,     # current price target
    "pt_prior": float,       # prior price target
    "importance": int,       # 0..5
    "exchange": str,         # listing exchange
}


def _coerce_typed(field: str, value: object, expected_type: type) -> object:
    """Type-check one field value against expected_type, returning the stored form.

    Handles str, int, and float (the v2 set). Booleans are rejected for the numeric
    types (bool is an int subclass in Python, so a stray True/False would otherwise
    pass an int check and silently store 1/0). An int passed where a float is expected
    is accepted and widened to float, so a whole-number price target survives a JSON
    round-trip (json.loads of "400" yields int) without a spurious type error.
    """
    if expected_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WrongFieldType(
                f"analyst_rating payload field {field!r} expected int, "
                f"got {type(value).__name__}"
            )
        return value
    if expected_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WrongFieldType(
                f"analyst_rating payload field {field!r} expected float, "
                f"got {type(value).__name__}"
            )
        return float(value)
    if expected_type is str:
        if not isinstance(value, str):
            raise WrongFieldType(
                f"analyst_rating payload field {field!r} expected str, "
                f"got {type(value).__name__}"
            )
        return value
    raise WrongFieldType(
        f"analyst_rating validator does not know how to check type "
        f"{expected_type.__name__} (validator bug)"
    )


def validate(payload: dict) -> dict:
    """Validate an analyst_rating payload (version 2). Same contract as sec_13d.validate."""
    if not isinstance(payload, dict):
        raise WrongFieldType(
            f"analyst_rating payload must be a dict, got {type(payload).__name__}"
        )

    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    extra = set(payload) - allowed
    if extra:
        raise UnknownField(
            f"analyst_rating payload has unknown field(s): {sorted(extra)}"
        )

    normalized: dict = {}

    # Required: present AND non-None.
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in payload or payload[field] is None:
            raise MissingRequiredField(
                f"analyst_rating payload missing required field: {field}"
            )
        normalized[field] = _coerce_typed(field, payload[field], expected_type)

    # Optional: only type-check if present and non-None. Absent / None passes.
    for field, expected_type in OPTIONAL_FIELDS.items():
        if field in payload and payload[field] is not None:
            normalized[field] = _coerce_typed(field, payload[field], expected_type)

    return normalized
