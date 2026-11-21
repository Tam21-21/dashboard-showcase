"""Unit suite for the VBT-native indicator reference parser (TERM F3).

catalog.py imports only stdlib + the contract packages (no VBT, no pandas), so this runs anywhere —
it is the backend's no-VBT guarantee that makes the parse a pure function over a committed file.
Covers: the real committed reference parses to the full suite; a well-formed fixture maps fields
correctly (including the `none` params and empty-inputs cases); a missing file fails loud; the boot
cache parses once and the test-only reset clears it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dashboard.backend import catalog  # noqa: E402 (conftest hoists the repo root)


def test_committed_reference_parses_to_full_suite() -> None:
    """The real committed file parses: 36 indicators, the pinned version, every field populated sanely."""
    parsed = catalog.parse_vbt_indicators_reference()  # the default committed path
    assert parsed["version"] == "2026.4.7"  # the pinned build the file was generated from
    inds = parsed["indicators"]
    assert len(inds) == 36  # the full VBT-native suite on the pinned build
    for ind in inds:  # every entry is shaped and human-readable, no empty name/description
        assert ind["name"] and isinstance(ind["name"], str)
        assert ind["description"] and isinstance(ind["description"], str)
        assert "Class representing" not in ind["description"]  # the boilerplate wrapper was stripped
        assert isinstance(ind["inputs"], list)
        assert isinstance(ind["parameters"], dict)
        assert isinstance(ind["outputs"], list)
    by_name = {i["name"]: i for i in inds}  # spot-check a few known indicators
    assert by_name["ADX"]["inputs"] == ["high", "low", "close"]
    assert by_name["ADX"]["parameters"] == {"window": "14", "wtype": "wilder"}
    assert by_name["ADX"]["outputs"] == ["plus_di", "minus_di", "dx", "adx"]
    assert by_name["RAND"]["inputs"] == []  # a signal generator with no price input
    assert by_name["OBV"]["parameters"] == {}  # an indicator with no tunable parameters


def test_fixture_field_mapping(tmp_path: Path) -> None:
    """A hand-written fixture maps each field, including the `none` parameters and empty-inputs cases."""
    fixture = tmp_path / "vbt_ref.md"
    fixture.write_text(
        "# VBT indicator reference\n\n"
        "Generated from VBT 9.9.9 by research_lab/tools/generate_vbt_indicator_reference.py.\n\n"
        "## FOO\n"
        "**What it does:** Does a thing in plain English.\n"
        "**Inputs:** high, low, close\n"
        "**Parameters:** window=14, wtype=wilder\n"
        "**Outputs:** foo, bar\n\n"
        "## BAR\n"
        "**What it does:** A signal generator.\n"
        "**Inputs:** none\n"
        "**Parameters:** none\n"
        "**Outputs:** entries\n",
        encoding="utf-8",
    )
    parsed = catalog.parse_vbt_indicators_reference(fixture)
    assert parsed["version"] == "9.9.9"  # read from the header line
    foo, bar = parsed["indicators"]
    assert foo == {
        "name": "FOO",
        "description": "Does a thing in plain English.",
        "inputs": ["high", "low", "close"],
        "parameters": {"window": "14", "wtype": "wilder"},
        "outputs": ["foo", "bar"],
        "look_ahead": False,
    }
    assert bar["inputs"] == []  # the "none" inputs sentinel -> empty list
    assert bar["parameters"] == {}  # the "none" parameters sentinel -> empty dict
    assert bar["outputs"] == ["entries"]


def test_missing_file_fails_loud(tmp_path: Path) -> None:
    """An absent reference raises the build-dependency error (fail loud at boot, not an empty catalog)."""
    with pytest.raises(catalog.VbtIndicatorReferenceMissing):
        catalog.parse_vbt_indicators_reference(tmp_path / "does_not_exist.md")


def test_load_caches_and_reset_clears() -> None:
    """load_vbt_indicators parses once and caches; the test-only reset clears the singleton."""
    catalog._reset_vbt_indicators_cache()  # start clean
    first = catalog.load_vbt_indicators()
    second = catalog.load_vbt_indicators()
    assert first is second  # same cached object, parsed once
    catalog._reset_vbt_indicators_cache()
    third = catalog.load_vbt_indicators()
    assert third is not first  # a fresh parse after the reset
