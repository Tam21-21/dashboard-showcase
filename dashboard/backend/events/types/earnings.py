"""Spec for the `earnings` event type (lane EA1). Mirrors analyst_rating's Spec contract.

One event is one Benzinga Newsdesk earnings headline, read into numbers. It is admissible and
ingest-validated and, since lane EA2, backtest-grade (it is in eventsdb.BACKTEST_GRADE_EVENT_TYPES),
so a strategy can declare event_types = ("earnings",).

Payload shape (version 2; version 1 rows from lane EA1 stay valid, they lack the two optional fields):
  article_id:     int    (REQUIRED, the news article id; also the natural_key, as a str)
  raw_ticker:     str    (REQUIRED, the one symbol the news row carries)
  fiscal_quarter: int    (REQUIRED, 1 to 4, as the headline writes it)
  adjusted:       bool   (REQUIRED, true when the headline says "Adj. EPS")
  eps_actual:     float  (REQUIRED, US dollars per share; "(x)" in the headline is -x)
  eps_estimate:   float  (REQUIRED)
  eps_word:       str    (REQUIRED, BEAT, MISS or INLINE, the headline's word for EPS)
  sales_actual:   float  (REQUIRED, US dollars; B, M, K read as 1e9, 1e6, 1e3)
  sales_estimate: float  (REQUIRED)
  sales_word:     str    (REQUIRED, BEAT, MISS or INLINE, the headline's word for sales)
  release_at:     int    (REQUIRED, epoch seconds UTC: the news row's created_at)
  headline:       str    (REQUIRED, the headline as stored)
  is_update:      bool   (REQUIRED, true for an "Update:" or "Correction:" headline)
  filing_accepted_at: int (OPTIONAL, epoch seconds UTC: the SEC acceptance time of the 8-K with Item 2.02
                           accepted within 24 hours of release_at; absent when none was found)
  accession_number:   str (OPTIONAL, that 8-K's accession number)
"""
from __future__ import annotations

from ..errors import (
    MissingRequiredField,
    UnknownField,
    WrongFieldType,
)


PAYLOAD_SCHEMA_VERSION = 2


REQUIRED_FIELDS: dict[str, type] = {
    "article_id": int,
    "raw_ticker": str,
    "fiscal_quarter": int,
    "adjusted": bool,
    "eps_actual": float,
    "eps_estimate": float,
    "eps_word": str,
    "sales_actual": float,
    "sales_estimate": float,
    "sales_word": str,
    "release_at": int,
    "headline": str,
    "is_update": bool,
}

OPTIONAL_FIELDS: dict[str, type] = {
    "filing_accepted_at": int,
    "accession_number": str,
}

_WORDS = ("BEAT", "MISS", "INLINE")


def _coerce_typed(field: str, value: object, expected_type: type) -> object:
    """Type-check one field value against expected_type, returning the stored form.

    A bool is refused where an int or float is expected (bool is an int subclass in Python, so a
    stray True would otherwise store 1). An int where a float is expected is widened to float, so a
    whole-number value survives a JSON round trip.
    """
    if expected_type is bool:
        if not isinstance(value, bool):
            raise WrongFieldType(
                f"earnings payload field {field!r} expected bool, got {type(value).__name__}"
            )
        return value
    if expected_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WrongFieldType(
                f"earnings payload field {field!r} expected int, got {type(value).__name__}"
            )
        return value
    if expected_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WrongFieldType(
                f"earnings payload field {field!r} expected float, got {type(value).__name__}"
            )
        return float(value)
    if expected_type is str:
        if not isinstance(value, str):
            raise WrongFieldType(
                f"earnings payload field {field!r} expected str, got {type(value).__name__}"
            )
        return value
    raise WrongFieldType(
        f"earnings validator does not know how to check type "
        f"{expected_type.__name__} (validator bug)"
    )


def validate(payload: dict) -> dict:
    """Validate an earnings payload (version 1). Same contract as analyst_rating.validate."""
    if not isinstance(payload, dict):
        raise WrongFieldType(
            f"earnings payload must be a dict, got {type(payload).__name__}"
        )

    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    extra = set(payload) - allowed
    if extra:
        raise UnknownField(
            f"earnings payload has unknown field(s): {sorted(extra)}"
        )

    normalized: dict = {}
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in payload or payload[field] is None:
            raise MissingRequiredField(
                f"earnings payload missing required field: {field}"
            )
        normalized[field] = _coerce_typed(field, payload[field], expected_type)
    for field, expected_type in OPTIONAL_FIELDS.items():    # absent stays absent; a present value is type-checked
        if field in payload and payload[field] is not None:
            normalized[field] = _coerce_typed(field, payload[field], expected_type)

    if not 1 <= normalized["fiscal_quarter"] <= 4:
        raise WrongFieldType(
            f"earnings payload field 'fiscal_quarter' must be 1 to 4, got {normalized['fiscal_quarter']}"
        )
    for word_field in ("eps_word", "sales_word"):
        if normalized[word_field] not in _WORDS:
            raise WrongFieldType(
                f"earnings payload field {word_field!r} must be one of {list(_WORDS)}, "
                f"got {normalized[word_field]!r}"
            )

    return normalized
