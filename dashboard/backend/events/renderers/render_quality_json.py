"""Render the quality-report bundle as canonical JSON bytes.

Machine-readable shape: keep snake_case verbatim, never apply the labels
translation table. Mirrors M3 K.5's `render_json.py` discipline: stable
two-space pretty-print, allow_nan=False so a NaN regression surfaces as a
ValueError rather than producing a JSON file no other parser accepts.

The /export route adds `bundle["_links"]` before calling this; the bundle's
`labels` slot is intentionally INCLUDED in JSON output so a downstream
consumer that wants to render its own UI off the JSON has the same
translation table the dashboard's MD/HTML renderers use.
"""
from __future__ import annotations

import json


def render_quality_json(bundle: dict) -> bytes:
    """Serialize the bundle dict to canonical JSON bytes.

    Two-space indent for human-readability; sort_keys=False so the spec's
    intended ordering (batch / report / labels / _links) survives; allow_nan
    OFF to fail loud on a regression.
    """
    return json.dumps(
        bundle,
        indent=2,
        sort_keys=False,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
