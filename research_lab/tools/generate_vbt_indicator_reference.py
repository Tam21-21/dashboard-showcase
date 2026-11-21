"""Generate the committed VBT-native indicator reference the dashboard catalog parses (TERM F3).

Run this inside the pinned VBT environment (the `research-runner` image, which carries the pinned
build b1727ac6 / 2026.4.7) with the repo mounted, OR confirm the same pinned build via the VBT Pro
MCP server. It introspects VBT's native indicator suite once and writes a static, committed reference
file. The dashboard backend never imports VBT — it only parses the file this script emits (AD-F3.1).

Regenerate when the VBT pin changes: re-run here, re-commit `docs/reference/vbt_indicators_reference.md`.
"""
from __future__ import annotations  # lazy annotations, so the module imports cheaply

import inspect  # read each indicator class's docstring (the human-readable description)
import re  # collapse whitespace in the extracted description paragraph
from pathlib import Path  # locate the committed reference file relative to the repo root

import vectorbtpro as vbt  # the pinned VBT build; the ONLY place VBT is imported for F3

# The committed reference file the catalog parses at boot (config.PROJECT_ROOT / docs / reference / ...).
# __file__ is research_lab/tools/...; three parents up is the repo root.
_OUT_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "reference" / "vbt_indicators_reference.md"


def _clean_first_line(p0: str) -> str:
    """Turn the boilerplate `Class representing the X indicator.` first line into a clean human phrase.

    The VBT docstrings ALWAYS open with this sentence, and for many indicators it is the cleanest
    one-liner there is (e.g. "look-ahead breakout label generator"). We strip the wrapper and present
    it as a sentence, so even indicators whose body paragraph is a doc-reference still read human.
    """
    s = re.sub(r"^Class representing (?:the|a|an) ", "", p0)  # drop the "Class representing the/a/an " wrapper
    s = re.sub(r"^Class representing ", "", s)  # drop the bare "Class representing " wrapper (no article)
    s = re.sub(r"\s*indicator\.?$", "", s).strip()  # drop a trailing " indicator." so the phrase stands alone
    s = s.rstrip(".").strip()  # drop any remaining trailing period before we re-punctuate
    if not s:  # nothing left (should not happen) — never emit an empty description
        return "No description available."
    return s[0].upper() + s[1:] + "."  # capitalize and end as a sentence, e.g. "Look-ahead breakout label generator."


def _is_good_prose(p1: str) -> bool:
    """True when the second docstring paragraph is a real plain-English sentence worth showing.

    Rejects VBT's doc-reference and admonition paragraphs (`See: * …`, `!!! tip …`, StackOverflow
    pointers), truncated lead-ins ending in a colon, code-identifier jargon (e.g. `new_entries`), and
    anything too short to be a description. When this returns False we fall back to the clean first line.
    """
    if p1.startswith(("See:", "!!!", "*", "For additional details")):  # doc-reference / admonition noise
        return False
    if p1.endswith(":"):  # a truncated lead-in (the real text is a list below it, which we don't pull)
        return False
    if re.search(r"\w+_\w+", p1):  # a code identifier like new_entries / entry_ts — not human prose
        return False
    if len(p1) < 30:  # too terse to be a useful description
        return False
    return True  # a genuine sentence


def _human_description(doc: str | None) -> str:
    """Turn an indicator class docstring into one plain-English line (no robot dumps, F3 hard requirement).

    Prefer the richer body paragraph when it is genuine prose; otherwise fall back to the cleaned
    boilerplate first line (which is always a readable noun phrase). Backticks are stripped from the
    chosen prose so no code formatting leaks into the catalog.
    """
    text = doc or ""  # guard against a missing docstring
    paras = [p.strip().replace("\n", " ") for p in text.split("\n\n") if p.strip()]  # non-empty paragraphs
    if not paras:  # no docstring at all — never emit an empty description
        return "No description available."
    p0 = re.sub(r"\s+", " ", paras[0]).strip()  # the boilerplate first line, whitespace-collapsed
    if len(paras) > 1:  # there is a body paragraph to consider
        p1 = re.sub(r"\s+", " ", paras[1]).replace("`", "").strip()  # collapse whitespace, strip backticks
        if _is_good_prose(p1):  # use the richer sentence only when it is genuine prose
            return p1
    return _clean_first_line(p0)  # otherwise the clean first line is the human description


def _render_param_value(value: object) -> str:
    """Render a default parameter value human-readably (spell out the infinities; strings verbatim)."""
    if isinstance(value, float) and value == float("inf"):  # BOLB et al. use +inf thresholds
        return "infinity"  # the word reads better than "inf" in the catalog
    if isinstance(value, float) and value == float("-inf"):  # the symmetric -inf case
        return "-infinity"  # spelled out for the same reason
    return str(value)  # numbers and string param values (e.g. wtype="wilder") render as-is


def _params_line(param_names: tuple[str, ...], defaults: dict) -> str:
    """Build the `key=value, ...` parameters line, or the literal `none` when there are no params."""
    if not param_names:  # an indicator with no tunable parameters (rare) says so explicitly
        return "none"
    parts = []  # accumulate "key=value" tokens in declared order
    for name in param_names:  # iterate the indicator's own parameter order, not dict order
        parts.append(f"{name}={_render_param_value(defaults.get(name))}")  # default from the param_defaults map
    return ", ".join(parts)  # comma-separated, the parser splits on commas then the first '='


def build_reference_text() -> str:
    """Introspect the VBT-native suite and return the full reference file content as one string."""
    version = vbt.__version__  # the pinned build version; recorded in the header so staleness is visible
    names = sorted(vbt.IF.list_indicators("vbt"))  # the native suite, in a stable alphabetical order
    lines: list[str] = []  # accumulate the markdown lines
    lines.append("# VBT indicator reference")  # the document title (the parser ignores it, humans read it)
    lines.append("")  # blank line for readability
    lines.append(f"Generated from VBT {version} by research_lab/tools/generate_vbt_indicator_reference.py.")  # provenance
    lines.append("Regenerate when the VBT pin changes; do not edit by hand.")  # the regen instruction
    lines.append("")  # blank line before the first indicator block
    for name in names:  # one `## NAME` block per native indicator
        ind = vbt.indicator(f"vbt:{name}")  # the parsed indicator class for this name
        description = _human_description(inspect.getdoc(ind))  # the cleaned plain-English description
        inputs = ", ".join(ind.input_names) or "none"  # the input series (e.g. "high, low, close"); "none" if it takes no price input
        outputs = ", ".join(ind.output_names) or "none"  # the output series the indicator produces
        params = _params_line(ind.param_names, getattr(ind, "param_defaults", {}) or {})  # the defaults line
        lines.append(f"## {name}")  # the block heading = the indicator name (the parser splits on `## `)
        lines.append(f"**What it does:** {description}")  # the human description field
        lines.append(f"**Inputs:** {inputs}")  # the inputs field
        lines.append(f"**Parameters:** {params}")  # the parameters field
        lines.append(f"**Outputs:** {outputs}")  # the outputs field
        lines.append("")  # blank line between blocks
    return "\n".join(lines).rstrip() + "\n"  # one trailing newline, no trailing blank run


def main() -> None:
    """Write the reference file and print a one-line confirmation."""
    text = build_reference_text()  # build the full content from the pinned build
    _OUT_PATH.write_text(text, encoding="utf-8")  # write the committed reference file
    count = text.count("\n## ")  # count the indicator blocks (each starts with a `## ` heading)
    print(f"wrote {_OUT_PATH} — VBT {vbt.__version__}, {count} indicators")  # confirmation for the operator


if __name__ == "__main__":  # only run when invoked directly, never on import
    main()  # do the work
