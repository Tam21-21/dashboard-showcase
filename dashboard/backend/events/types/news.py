"""Spec for the `news` event type (F7). Mirrors analyst_rating's Spec contract.

News is a DISPLAY-AND-RECORD event type, NOT backtest-grade: it is held out of the
materializer/loader registries (the structural marker — eventsdb.BACKTEST_GRADE_EVENT_TYPES),
so a strategy cannot consume it. It is ingested honestly now; a future unit decides its
event-study semantics before it becomes strategy-readable.

Payload shape (version 1, headlines-keep-all — no article body stored):
  article_id:  int       (REQUIRED — the Benzinga article id; also the natural_key, as str)
  headline:    str        (REQUIRED — the title, HTML entities decoded at map time)
  created_at:  int        (REQUIRED — epoch s UTC, the publish instant; mirrors knowledge_at)
  updated_at:  int        (REQUIRED — epoch s UTC, the revision signal a re-fetch compares on)
  url:         str        (REQUIRED — the source permalink, the row's click-through)
  news_source: str        (REQUIRED — the article's own source string, e.g. "benzinga";
                           named news_source to avoid colliding with research_events.source,
                           which is the ROW-level integration value 'alpaca_news')
  symbols:     list[str]  (REQUIRED — the tagged symbol set; the display surface reads this.
                           Multi-symbol; crypto comes un-slashed e.g. "BTCUSD". May be empty.)
  summary:     str        (OPTIONAL — short abstract, HTML entities decoded; absent when blank)
  author:      str        (OPTIONAL — byline; absent when blank)

`companies` is always empty for news (no CIK links — the symbol set lives in the payload),
so news never trips the correction-time company-link guard. See the F7 arch note §4.
"""
from __future__ import annotations

from ..errors import (
    MissingRequiredField,
    UnknownField,
    WrongFieldType,
)


PAYLOAD_SCHEMA_VERSION = 1


REQUIRED_FIELDS: dict[str, type] = {
    "article_id": int,
    "headline": str,
    "created_at": int,
    "updated_at": int,
    "url": str,
    "news_source": str,
    "symbols": list,
}

OPTIONAL_FIELDS: dict[str, type] = {
    "summary": str,   # short abstract; absent (not None) when the source field is blank
    "author": str,    # byline; absent when blank at the source
}


def _coerce_typed(field: str, value: object, expected_type: type) -> object:
    """Type-check one field value against expected_type, returning the stored form.

    Handles int, str, and list (the v1 set). Booleans are rejected for int (bool is an int
    subclass in Python, so a stray True/False would otherwise pass an int check and silently
    store 1/0). A list field must contain only strings (symbols are ticker strings); a
    non-string element is a WrongFieldType, never silently dropped.
    """
    if expected_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise WrongFieldType(
                f"news payload field {field!r} expected int, got {type(value).__name__}"
            )
        return value
    if expected_type is str:
        if not isinstance(value, str):
            raise WrongFieldType(
                f"news payload field {field!r} expected str, got {type(value).__name__}"
            )
        return value
    if expected_type is list:
        if not isinstance(value, list):
            raise WrongFieldType(
                f"news payload field {field!r} expected list, got {type(value).__name__}"
            )
        for i, elem in enumerate(value):                       # every element must be a string
            if not isinstance(elem, str):
                raise WrongFieldType(
                    f"news payload field {field!r}[{i}] expected str, "
                    f"got {type(elem).__name__}"
                )
        return list(value)
    raise WrongFieldType(
        f"news validator does not know how to check type "
        f"{expected_type.__name__} (validator bug)"
    )


def validate(payload: dict) -> dict:
    """Validate a news payload (version 1). Same contract as analyst_rating.validate."""
    if not isinstance(payload, dict):
        raise WrongFieldType(
            f"news payload must be a dict, got {type(payload).__name__}"
        )

    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    extra = set(payload) - allowed
    if extra:
        raise UnknownField(
            f"news payload has unknown field(s): {sorted(extra)}"
        )

    normalized: dict = {}

    # Required: present AND non-None.
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in payload or payload[field] is None:
            raise MissingRequiredField(
                f"news payload missing required field: {field}"
            )
        normalized[field] = _coerce_typed(field, payload[field], expected_type)

    # Optional: only type-check if present and non-None. Absent / None passes.
    for field, expected_type in OPTIONAL_FIELDS.items():
        if field in payload and payload[field] is not None:
            normalized[field] = _coerce_typed(field, payload[field], expected_type)

    return normalized
