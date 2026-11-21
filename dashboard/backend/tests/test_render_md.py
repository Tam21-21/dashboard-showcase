"""Pytest unit suite for render_md (A6).

Stdlib-only. Asserts H1 leads, no snake_case in table headers (L28), the
optional-keys "omitted" branches, links footer.
"""
from __future__ import annotations

import re

from dashboard.backend.research import render_md
from dashboard.backend.tests._synthetic import synthetic_bundle


def test_starts_with_h1():
    out = render_md.render_md(synthetic_bundle()).decode("utf-8")
    first = next(line for line in out.splitlines() if line.strip())
    assert first.startswith("# ")


def test_no_snake_case_in_table_headers():
    # L28 rule, mirrors the K.5 probe assertion.
    out = render_md.render_md(synthetic_bundle()).decode("utf-8")
    lines = out.splitlines()
    offenders: list[str] = []
    for i, line in enumerate(lines[:-1]):
        if re.match(r"^\|.*\|\s*$", line) and re.match(r"^\|---", lines[i + 1]):
            for cell in (c.strip() for c in line.strip("|").split("|")):
                if re.search(r"\b[a-z]+_[a-z]+\b", cell):
                    offenders.append(cell)
    assert not offenders, f"snake_case in MD table headers: {offenders}"


def test_size_under_100kb():
    out = render_md.render_md(synthetic_bundle())
    assert len(out) <= 100_000


def test_trades_omitted_branch_renders_marker():
    out = render_md.render_md(synthetic_bundle(optional_keys=False)).decode("utf-8")
    assert "_Trades omitted from this bundle._" in out


def test_settings_omitted_branch_renders_marker():
    out = render_md.render_md(synthetic_bundle(optional_keys=False)).decode("utf-8")
    assert "_Settings snapshot omitted from this bundle._" in out


def test_links_footer_appears_when_links_present():
    # K.6 retrofit: bundle["_links"] populates the cross-format footer.
    bundle = synthetic_bundle()
    bundle["_links"] = {
        "json": "https://example.com/run.json",
        "md": "https://example.com/run.md",
        "html": "https://example.com/run.html",
    }
    out = render_md.render_md(bundle).decode("utf-8")
    assert "Same export in other formats" in out
    assert "https://example.com/run.json" in out
    assert "https://example.com/run.md" in out
    assert "https://example.com/run.html" in out


def test_links_footer_absent_by_default():
    out = render_md.render_md(synthetic_bundle()).decode("utf-8")
    assert "Same export in other formats" not in out
