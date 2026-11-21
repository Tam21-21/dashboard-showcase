"""Pytest unit suite for render_json (A6).

Stdlib-only. Asserts strict-NaN, round-trip, pretty-printing, and size cap.
"""
from __future__ import annotations

import json
import math

import pytest

from dashboard.backend.research import render_json
from dashboard.backend.tests._synthetic import synthetic_bundle


def test_round_trips_synthetic_bundle():
    bundle = synthetic_bundle()
    out = render_json.render_json(bundle)
    assert json.loads(out) == bundle


def test_strict_nan_rejected_by_strict_parser():
    # render_json uses allow_nan=False; a downstream strict parse must succeed.
    bundle = synthetic_bundle()
    out = render_json.render_json(bundle)

    def _strict(constant: str) -> None:
        raise ValueError(f"non-strict JSON token: {constant}")

    json.loads(out, parse_constant=_strict)  # must not raise


def test_nan_in_bundle_raises_loudly():
    # If the bundle is malformed (NaN slipped through normalisation), render_json
    # raises rather than silently emit invalid JSON. The probe's discipline.
    bundle = synthetic_bundle()
    bundle["stats"]["sharpe_ratio"] = math.nan
    with pytest.raises(ValueError):
        render_json.render_json(bundle)


def test_size_under_4mb():
    out = render_json.render_json(synthetic_bundle())
    assert len(out) <= 4_000_000


def test_is_pretty_printed():
    # §5.2 says pretty-printed; we check for at least one newline + indent.
    out = render_json.render_json(synthetic_bundle()).decode("utf-8")
    assert "\n  " in out, "expected pretty-printed JSON with indent"
