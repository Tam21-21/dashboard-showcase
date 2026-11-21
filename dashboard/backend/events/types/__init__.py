"""Per-event-type validator registry.

Each module under this package declares one `Spec` exposing:
  PAYLOAD_SCHEMA_VERSION: int
  REQUIRED_FIELDS:        dict[str, type]
  OPTIONAL_FIELDS:        dict[str, type]
  validate(payload: dict) -> dict

The shared write core (`events/write_core.py`) dispatches on
`record.event_type` to the right module via `SPEC_REGISTRY` below.
Adding a future event type is one new module here plus one registry entry;
no schema migration on `research_events.event_type`'s CHECK enum (the enum
lives in `eventsdb.py` and stays bounded by spec brief §3.1).
"""
from __future__ import annotations

from typing import Callable

from . import analyst_rating, earnings, news, sec_13d


# (PAYLOAD_SCHEMA_VERSION, REQUIRED_FIELDS, OPTIONAL_FIELDS, validate)
# Tuples not dicts so misnamed indices fail fast on usage.
SPEC_REGISTRY: dict[str, tuple[int, dict, dict, Callable[[dict], dict]]] = {
    "sec_13d": (
        sec_13d.PAYLOAD_SCHEMA_VERSION,
        sec_13d.REQUIRED_FIELDS,
        sec_13d.OPTIONAL_FIELDS,
        sec_13d.validate,
    ),
    "analyst_rating": (
        analyst_rating.PAYLOAD_SCHEMA_VERSION,
        analyst_rating.REQUIRED_FIELDS,
        analyst_rating.OPTIONAL_FIELDS,
        analyst_rating.validate,
    ),
    # F7: news is admissible + ingest-validatable (here), but held OUT of the backtest
    # registries (output_schemas / loader gate) — the structural not-backtest-grade marker.
    "news": (
        news.PAYLOAD_SCHEMA_VERSION,
        news.REQUIRED_FIELDS,
        news.OPTIONAL_FIELDS,
        news.validate,
    ),
    # EA1: earnings is admissible + ingest-validatable (here), but NOT backtest-grade yet; lane EA2
    # grades it. Held out of the backtest registries like news.
    "earnings": (
        earnings.PAYLOAD_SCHEMA_VERSION,
        earnings.REQUIRED_FIELDS,
        earnings.OPTIONAL_FIELDS,
        earnings.validate,
    ),
}


def get_spec(event_type: str) -> tuple[int, dict, dict, Callable[[dict], dict]]:
    """Look up the per-type spec; KeyError if event_type is unknown.

    The events.research_events CHECK enum forbids unknown event_type at the
    DB layer; this lookup is a code-side mirror so a typo in the shared core
    fails fast with a clear KeyError rather than silently writing under the
    wrong validator.
    """
    return SPEC_REGISTRY[event_type]
