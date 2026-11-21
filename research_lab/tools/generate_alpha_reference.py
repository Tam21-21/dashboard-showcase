"""Generate the committed WQA101 alpha reference the dashboard catalog parses (U.4, lane GP,
criterion 6). Same idiom as generate_vbt_indicator_reference.py beside it: run this ONCE inside
the pinned VBT environment (the research-runner image, pinned build b1727ac6 / 2026.4.7) with the
repo mounted, and it writes a static, committed reference file. The dashboard backend never
imports vectorbtpro (AD-F3.1's same rule extended to the alphas): it only parses the file this
script writes.

Regenerate when the VBT pin changes: re-run here, re-commit
docs/reference/wqa101_alphas_reference.md.
"""
from __future__ import annotations  # lazy annotations, so this module imports cheaply

from pathlib import Path  # locate the committed reference file relative to the repo root

import vectorbtpro as vbt  # the pinned VBT build; the ONLY place vbt is imported for this file

# The committed reference file the catalog parses at boot. __file__ is
# research_lab/tools/generate_alpha_reference.py; three parents up is the repo root (the same
# depth generate_vbt_indicator_reference.py's own _OUT_PATH climbs).
_OUT_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "reference" / "wqa101_alphas_reference.md"


def build_reference_text() -> str:
    """Introspect the real wqa101_expr_config dict and return the full reference file content.

    Confirmed live inside research-runner:m3-latest before this script was written (never assumed
    from documentation, DECISIONS #308 ruling 3): `vbt.indicators.expr.wqa101_expr_config` is a
    `HybridConfig` (a dict subclass) with exactly 101 entries, keyed by the plain integers 1
    through 101 with no gap, every value a single-line expression string (no embedded newline,
    longest observed 282 characters). So the parse this writes for is: one `## alpha_N` heading
    per key, in ascending numeric order (never dict-insertion order, which is not guaranteed to be
    sorted), one `**Expression:**` field carrying that key's value VERBATIM, never reformatted and
    never re-derived, so a reader comparing this file against the live config sees the identical
    text vbt itself would evaluate.
    """
    version = vbt.__version__  # the pinned build version; recorded in the header so staleness is visible
    config = vbt.indicators.expr.wqa101_expr_config  # the real, live alpha expression dict
    keys = sorted(config.keys())  # ascending 1..101; dict order is not itself a sorted guarantee
    lines: list[str] = []  # accumulate the markdown lines
    lines.append("# WQA101 alpha reference")  # the document title (the parser ignores it, humans read it)
    lines.append("")  # blank line for readability
    lines.append(f"Generated from VBT {version} by research_lab/tools/generate_alpha_reference.py.")  # provenance
    lines.append("Regenerate when the VBT pin changes; do not edit by hand.")  # the regen instruction
    lines.append("")  # blank line before the first alpha block
    for key in keys:  # one `## alpha_N` block per alpha, in ascending numeric order
        expression = config[key]  # the raw expression text, verbatim
        lines.append(f"## alpha_{key}")  # the block heading (the parser splits on `## `)
        lines.append(f"**Expression:** {expression}")  # the one field this file carries per alpha
        lines.append("")  # blank line between blocks
    return "\n".join(lines).rstrip() + "\n"  # one trailing newline, no trailing blank run


def main() -> None:
    """Write the reference file and print a one-line confirmation."""
    text = build_reference_text()  # build the full content from the live, pinned config
    _OUT_PATH.write_text(text, encoding="utf-8")  # write the committed reference file
    count = text.count("\n## ")  # count the alpha blocks (each starts with a `## ` heading)
    print(f"wrote {_OUT_PATH}, VBT {vbt.__version__}, {count} alphas")  # confirmation for the operator


if __name__ == "__main__":  # only run when invoked directly, never on import
    main()  # do the work
