"""Pytest unit suite for render_labels (A6).

Stdlib-only — exercises the formatter and the label table without touching
researchdb or any server-tier dep. Mirrors the corresponding K.5 probe steps so
a regression that the probe would catch in 5s is caught here in 50ms.
"""
from __future__ import annotations

import pytest

from dashboard.backend.research import render_labels


class TestFormatNumber:
    """L28 number-formatting rules: percent, currency, ratio, count, timestamp, None."""

    def test_percent_one_decimal(self):
        assert render_labels.format_number(0.183, "percent") == "18.3%"

    def test_currency_always_two_decimals(self):
        assert render_labels.format_number(8.45, "currency") == "$8.45"
        # L28 wins over arch §21's inconsistent example: always two decimals + commas.
        assert render_labels.format_number(10000, "currency") == "$10,000.00"

    def test_ratio_two_decimals(self):
        assert render_labels.format_number(0.42, "ratio") == "0.42"

    def test_count_thousands_separator(self):
        assert render_labels.format_number(8784, "count") == "8,784"

    def test_timestamp_utc_human(self):
        assert render_labels.format_number(1_704_067_200, "timestamp") == "2024-01-01 00:00 UTC"

    def test_none_renders_as_em_dash(self):
        # Every kind: a None input renders as the placeholder, not the literal "None".
        for kind in ("percent", "currency", "ratio", "count", "timestamp"):
            assert render_labels.format_number(None, kind) == "—"

    def test_unknown_kind_raises(self):
        # Programming bug should fail loud, not silently fall through.
        with pytest.raises(ValueError, match="unknown format kind"):
            render_labels.format_number(1.0, "nonsense")  # type: ignore[arg-type]


class TestLabelCoverage:
    """LABELS must cover every key the renderers emit as a human-facing label."""

    def test_labels_cover_stats_keys(self):
        missing = set(render_labels.STATS_KEYS) - set(render_labels.LABELS)
        assert not missing, f"missing stats labels: {missing}"

    def test_explanations_cover_stats_keys(self):
        missing = set(render_labels.STATS_KEYS) - set(render_labels.METRIC_EXPLANATIONS)
        assert not missing, f"missing explanations: {missing}"

    def test_stats_format_covers_stats_keys(self):
        missing = set(render_labels.STATS_KEYS) - set(render_labels.STATS_FORMAT)
        assert not missing, f"missing format kinds: {missing}"

    def test_no_snake_case_in_labels(self):
        # Renderer-generated labels must not contain snake_case (L28). A stat
        # like "max_drawdown" maps to "Maximum drawdown", never "max_drawdown".
        offenders = [(key, label) for key, label in render_labels.LABELS.items()
                     if "_" in label and label.replace("_", "").islower()]
        assert not offenders, f"snake_case offenders: {offenders}"
