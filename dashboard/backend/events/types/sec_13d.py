"""Spec for the `sec_13d` event type. Spec brief §4.1 / MA1.

Payload shape (3 fields after MA1):
  percent_owned: float|None  Item 4 "Percent of class" or equivalent (OPTIONAL/nullable
                             since the 13D-parser fix — a 13D filing is a valid event even
                             when the cover page's % cannot be extracted; the only consumer
                             is a nullable float64 materializer column, so null is safe)
  filer_name:    str      Item 2(a) "Name of filing person" (REQUIRED)
  subject_name:  str      Issuer-name field (REQUIRED)

Fields NOT in the payload — derived elsewhere (per MA1):
  filer_cik:          int(natural_key.split('_', 1)[0])         (natural_key spine)
  accession_number:   natural_key.split('_', 1)[1]              (natural_key spine)
  subject_cusip:      research_event_companies.cusip            (join table)
  filing_url:         derived from filer_cik + accession_number (materializer §9.3)

The §13.2 step 6 / brief A fixture-finalization substituted `subject_name` for
the spec-brief-cited `subject_cusip` validate-failure target. That substitution
is purely about the locked validate-failure record in the probe-N fixture;
the validator itself simply enforces the three REQUIRED_FIELDS above.
"""
from __future__ import annotations

from ..errors import (
    InvalidEnumValue,
    MissingRequiredField,
    UnknownField,
    WrongFieldType,
)


PAYLOAD_SCHEMA_VERSION = 1

# Each value is the Python type the field MUST hold post-validation.
REQUIRED_FIELDS: dict[str, type] = {
    "filer_name": str,
    "subject_name": str,
}

# 13D's underlying-event date (Item 2(e)) populates the spine's `occurred_at`
# column, NOT a payload field. percent_owned is OPTIONAL/nullable (the parser fix):
# the cover-page % extraction misses many real filings (table layouts, wording
# variants), and discarding the whole event over one field is the wrong tradeoff —
# the filing happened, the filer/subject/date are known. An optional field present
# but unextractable is stored as None (its materializer column is nullable float64).
OPTIONAL_FIELDS: dict[str, type] = {
    "percent_owned": float,
}


def validate(payload: dict) -> dict:
    """Validate a sec_13d payload. Return the normalized dict.

    Strict: unknown fields raise UnknownField. Required fields absent (or
    None) raise MissingRequiredField. Wrong types raise WrongFieldType. The
    shared core's per-record catch (`policy='quarantine'`) maps any
    PayloadValidationError subclass to a quarantine row; under
    `policy='abort_on_first_error'` the same exception escalates to
    RecordValidationAbort.

    Returns a fresh dict (not the input) so a caller mutation cannot
    retroactively affect a stored row.
    """
    if not isinstance(payload, dict):
        raise WrongFieldType(
            f"sec_13d payload must be a dict, got {type(payload).__name__}"
        )

    # Unknown fields: every key must be in the union of required + optional sets.
    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    extra = set(payload) - allowed
    if extra:
        # Sorted for deterministic error messages across Python versions.
        raise UnknownField(
            f"sec_13d payload has unknown field(s): {sorted(extra)}"
        )

    normalized: dict = {}

    # Required fields: must be present AND non-None (None as a value is treated
    # as "absent" for the purposes of MissingRequiredField).
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in payload or payload[field] is None:
            raise MissingRequiredField(
                f"sec_13d payload missing required field: {field}"
            )
        normalized[field] = _coerce_type(field, payload[field], expected_type)

    # Optional/nullable fields: a present, non-None value is type-checked and stored;
    # absent OR explicit None is stored as None (the field is nullable, e.g. percent_owned
    # when the cover page's % could not be extracted). The materializer's column is nullable.
    for field, expected_type in OPTIONAL_FIELDS.items():
        value = payload.get(field)
        normalized[field] = None if value is None else _coerce_type(field, value, expected_type)

    # No enum-valued fields on sec_13d as of payload_schema_version=1; the
    # InvalidEnumValue import keeps the class reachable so a future v2 can
    # add an enum field without churning imports across modules.
    _ = InvalidEnumValue

    return normalized


def _coerce_type(field: str, value: object, expected_type: type) -> object:
    """Type-check one payload value against its declared type; return the normalized value.

    Shared by the required + optional loops. int → float promotion is fine for percent_owned
    (JSON often serializes 5 without a decimal); booleans are ints in Python, guard them out.
    """
    if expected_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WrongFieldType(
                f"sec_13d payload field {field!r} expected float, got {type(value).__name__}"
            )
        return float(value)
    if expected_type is str:
        if not isinstance(value, str):
            raise WrongFieldType(
                f"sec_13d payload field {field!r} expected str, got {type(value).__name__}"
            )
        return value
    # Unreachable given the only declared types above; a future type must extend this ladder.
    raise WrongFieldType(
        f"sec_13d validator does not know how to check type "
        f"{expected_type.__name__} (validator bug)"
    )
