"""Pytest unit suite for render_html (A6).

Server-tier: requires markdown-it-py. Skipped on machines without the dep
(laptop, by convention — markdown-it-py is installed in the server venv via
the rich -> markdown-it-py transitive). The HTTP probe is the end-to-end
gate; these tests cover renderer-local invariants on the synthetic bundle.
"""
from __future__ import annotations

import pytest

# Skip the whole file when the server-tier dep is missing. Per CLAUDE.md the
# renderer has no fallback, so importorskip is the correct gate.
pytest.importorskip("markdown_it")

from dashboard.backend.research import render_html  # noqa: E402
from dashboard.backend.tests._synthetic import synthetic_bundle  # noqa: E402

pytestmark = pytest.mark.server


def _render(bundle: dict | None = None) -> str:
    return render_html.render_html(bundle or synthetic_bundle()).decode("utf-8")


def test_starts_with_doctype():
    assert _render().startswith("<!DOCTYPE html>")


def test_single_bundle_script_tag():
    # The K.5 HTTP probe asserts exactly one; the unit test mirrors the invariant.
    assert _render().count('<script id="bundle"') == 1


def test_v5_chart_api_used_not_v4():
    out = _render()
    assert "LightweightCharts.createChart(" in out
    assert "chart.addSeries(LightweightCharts.CandlestickSeries" in out
    assert "chart.addSeries(LightweightCharts.LineSeries" in out
    # Negative assertion: v4 type-specific methods were removed in v5.
    assert "addLineSeries(" not in out


def test_attribution_logo_disabled():
    # UI #3: TradingView attribution mark removed (attributionLogo: false on
    # every chart options block).
    assert "attributionLogo: false" in _render()


def test_h1_uses_run_name_when_present():
    # UI #2: bundle.run.name leads the title; the strategy name is the fallback.
    bundle = synthetic_bundle()
    bundle["run"]["name"] = "M-A-baseline"
    out = render_html.render_html(bundle).decode("utf-8")
    # The hero H1 contains the run name (after html.escape).
    assert "M-A-baseline" in out
    # Title tag also includes it.
    assert "<title>M-A-baseline" in out


def test_h1_falls_back_to_strategy_name_when_no_run_name():
    bundle = synthetic_bundle()
    bundle["run"]["name"] = None
    out = render_html.render_html(bundle).decode("utf-8")
    assert "synthetic-strategy" in out  # strategy name appears in H1


def test_run_id_demoted_to_meta_chip():
    # No "Run #N" in the H1 itself; it lives in the meta chips below.
    bundle = synthetic_bundle()
    bundle["run"]["name"] = "named-run"
    out = render_html.render_html(bundle).decode("utf-8")
    # H1 line contains the name only — find the <h1 ...>...</h1> block.
    import re
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", out, re.DOTALL)
    assert h1_match, "no H1 found"
    assert "Run #" not in h1_match.group(1), f"H1 leaked Run #: {h1_match.group(1)!r}"
    # But the run id IS present elsewhere (in the meta chip and the title).
    assert "Run #" in out or "Run&nbsp;#" in out


def test_xss_in_description_is_escaped():
    # markdown-it-py is configured with html=False; raw HTML in description must
    # render as escaped text, not as live tags. The probe limits itself to the
    # rendered description region (the inlined bundle JSON contains the literal
    # description as a string, but the `</` is escaped to `<\/` so the parent
    # script tag cannot be broken out of).
    import re
    bundle = synthetic_bundle()
    bundle["strategy"]["description"] = "<script>alert(1)</script>"
    out = render_html.render_html(bundle).decode("utf-8")
    # Extract the description card body. The test renderer wraps it in
    # <div class="card-body prose">...</div>; find that block.
    desc_match = re.search(r'<div class="card-body prose">(.*?)</div>', out, re.DOTALL)
    assert desc_match, "description card not found in output"
    desc_html = desc_match.group(1)
    # The rendered prose must not contain a live <script> tag.
    assert "<script" not in desc_html, f"rendered description contains live <script>: {desc_html!r}"
    # And the escaped form should be present (markdown-it escapes < to &lt;).
    assert "&lt;script" in desc_html
    # The inlined bundle JSON must have escaped </ to <\/ so it cannot terminate
    # the parent <script> tag.
    bundle_match = re.search(r'<script id="bundle" type="application/json">(.*?)</script>', out, re.DOTALL)
    assert bundle_match, "bundle script not found"
    assert "</script>" not in bundle_match.group(1)


def test_drift_chip_appears_when_drift_present():
    bundle = synthetic_bundle()
    bundle["settings_drift"] = {
        "drifted": True,
        "changes": [
            {"scope": "fees", "key": "crypto.default", "run_value": "0.001", "current_value": "0.005"},
        ],
    }
    out = render_html.render_html(bundle).decode("utf-8")
    # Plain-English copy ("setting(s) changed since this run") + the table title.
    assert "setting changed since this run" in out
    assert "Trading settings changed since this run" in out
    assert "fees.crypto.default" in out


def test_drift_chip_absent_when_no_drift():
    # "drift-chip" appears in the CSS rule definitions regardless; the assertion
    # checks that the rendered <span class="drift-chip"> markup is not emitted.
    bundle = synthetic_bundle()
    bundle["settings_drift"] = {"drifted": False, "changes": []}
    out = render_html.render_html(bundle).decode("utf-8")
    assert "setting changed since this run" not in out
    assert "Trading settings changed since this run" not in out
    assert '<span class="drift-chip"' not in out


def test_size_under_4mb_with_synthetic_bundle():
    # The synthetic bundle is tiny (20 trades, 100 equity points); HTML stays small.
    assert len(_render().encode("utf-8")) <= 4_000_000
