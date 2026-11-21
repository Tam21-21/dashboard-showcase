"""Look-ahead guardrail: the ONE place a strategy or the worker asks "does this VBT indicator name
read the future". Source of truth: docs/reference/lookahead_indicators.md, the committed, hand-authored
authority list (docs/briefs/lookahead_guardrail_spec_brief.md section 4, layer 1).

This module imports neither vectorbtpro nor the dashboard package, on purpose, so both the worker and
the backend can use it (the brief's own words). That is also why it is a standalone file rather than a
name added to strategies/helpers/__init__.py: that package's own __init__.py eagerly imports portfolio.py,
which imports strategies.base at module level (research_lab/CLAUDE.md names strategies/base/ as carrying
"VBT extraction"), so any normal "import strategies.helpers.lookahead" would still run that package's
init first and pull vbt in as a side effect. A caller that genuinely needs the "no VBT" guarantee loads
this file directly by path, exactly as this module's own probes do
(scripts/probes/lookahead_guardrail/ac5_helper_imports_neither_vbt_nor_dashboard.probe.py).

The parser below is a SEPARATE, independently written copy of the same strict row format
dashboard/backend/catalog.py::parse_lookahead_indicators enforces. Layer 1 states plainly that "three
separately written parsers must agree on it"; duplicating the small parse here, rather than importing
catalog.py, is what keeps this file's own import list empty of the dashboard package.
"""
from __future__ import annotations  # lazy annotations, matching the rest of the tree's style

import re  # the heading-structural block parse: split on "## ", read two field lines per block
from pathlib import Path  # __file__-relative repo-root resolution needs no environment assumption

# research_lab/strategies/helpers/lookahead.py -> parents[3] is the repo root: parents[0]=helpers,
# [1]=strategies, [2]=research_lab, [3]=the checkout root. Works the same way whether this file is
# imported normally or loaded directly by path (Python sets __file__ to the real file location either
# way), so the path this resolves to is correct under both loading styles.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUTHORITY_PATH = _REPO_ROOT / "docs" / "reference" / "lookahead_indicators.md"


class LookaheadAuthorityMissing(FileNotFoundError):
    """The authority list is absent, or present but carries zero rows.

    Folded into one class on purpose, mirroring dashboard/backend/catalog.py's own
    LookaheadReferenceMissing: the brief's own words are "we know nine exist; a file that says none do
    is a corrupted file, not good news," so an empty file is exactly as loud a failure as a missing one.
    """


class LookaheadAuthorityError(RuntimeError):
    """The authority list exists and is non-empty but breaks layer 1's strict row format.

    Raised on a repeated name, a heading carrying anything but one name, a missing **Reads:** or
    **Evidence:** field line, a field out of order, or either field's value being empty after
    trimming. No row is ever silently skipped: a parser that drops what it cannot read would shrink
    the flagged set while still reporting success, which is this guardrail failing open (layer 2's
    own words for exactly this failure class).
    """


class LookaheadViolation(RuntimeError):
    """Raised by assert_not_lookahead() when a caller reaches for a future-reading indicator by name
    without the explicit opt-in. This is the refusal point every future consumer calls, the generator
    first among them (docs/briefs/lookahead_guardrail_spec_brief.md section 4, layer 2).
    """


def _parse_authority_file(path: Path) -> dict[str, dict[str, str]]:
    """Parse the authority list at `path` into {NAME: {"reads": ..., "evidence": ...}}, strictly.

    Kept separate from the cached entry point below so a test could exercise a fresh parse in
    isolation, without touching the module-level cache.
    """
    if not path.exists():  # absence is a build error, never read as "nothing is flagged"
        raise LookaheadAuthorityMissing(
            f"look-ahead authority list not found at {path}; it is the committed, hand-authored file "
            f"docs/briefs/lookahead_guardrail_spec_brief.md section 4 requires."
        )
    text = path.read_text(encoding="utf-8")  # the whole small file, read once per parse
    blocks = re.split(r"^## ", text, flags=re.M)[1:]  # drop the header prose before the first heading
    if not blocks:  # present but zero rows: still a corrupted-file state, never "nothing is look-ahead"
        raise LookaheadAuthorityMissing(
            f"look-ahead authority list at {path} carries zero rows; the nine known future-reading "
            f"indicators would all silently stop being flagged."
        )
    rows: dict[str, dict[str, str]] = {}
    for block in blocks:  # one block per candidate future-reading indicator name
        heading, _, body = block.partition("\n")  # the first line is the "NAME" half of "## NAME"
        name = heading.strip()
        if not name or re.search(r"\s", name):  # empty heading, or a heading carrying more than one name
            raise LookaheadAuthorityError(
                f"look-ahead authority list at {path} carries a block with an empty or multi-word "
                f"heading (raw heading text: {heading!r})"
            )
        if name in rows:  # the name is the block's key; a repeat is malformed, never a merge of rows
            raise LookaheadAuthorityError(
                f"look-ahead authority list at {path} repeats the name {name!r}; a name must be "
                f"unique across the file"
            )
        lines = [ln for ln in body.splitlines() if ln.strip()]  # this block's non-blank lines, in order
        if len(lines) != 2:  # exactly two field lines are required, nothing more, nothing fewer
            raise LookaheadAuthorityError(
                f"look-ahead authority list block {name!r} at {path} carries {len(lines)} non-blank "
                f"line(s); exactly two are required (**Reads:** then **Evidence:**)"
            )
        reads_match = re.match(r"^\*\*Reads:\*\*\s*(.*)$", lines[0])  # the first field, in order
        evidence_match = re.match(r"^\*\*Evidence:\*\*\s*(.*)$", lines[1])  # the second field, in order
        if not reads_match or not evidence_match:  # wrong label text or wrong order both land here
            raise LookaheadAuthorityError(
                f"look-ahead authority list block {name!r} at {path} does not carry its two field "
                f"lines in the required order (**Reads:** then **Evidence:**)"
            )
        reads_value = reads_match.group(1).strip()  # the plain-English "what future info it reads"
        evidence_value = evidence_match.group(1).strip()  # the plain-English "how we know" sentence
        if not reads_value or not evidence_value:  # a present field with an empty value is still broken
            raise LookaheadAuthorityError(
                f"look-ahead authority list block {name!r} at {path} carries an empty **Reads:** or "
                f"**Evidence:** value after trimming; every row needs a non-empty reason"
            )
        rows[name] = {"reads": reads_value, "evidence": evidence_value}  # a fully valid row: keep it
    return rows


# Populated once on first call, then read-only for the life of the process (mirrors
# dashboard/backend/catalog.py's own process-cache pattern): the file is committed and does not change
# while a worker or backend process is alive.
_AUTHORITY_CACHE: dict[str, dict[str, str]] | None = None


def _load_authority_names() -> dict[str, dict[str, str]]:
    """Parse-and-cache the authority list on first call; every later call reuses the cached result."""
    global _AUTHORITY_CACHE
    if _AUTHORITY_CACHE is None:  # first call in this process: parse now, fail loud if the file is bad
        _AUTHORITY_CACHE = _parse_authority_file(_AUTHORITY_PATH)
    return _AUTHORITY_CACHE


def is_lookahead(name: str) -> bool:
    """True iff `name` is one of the authority list's future-reading VBT indicator names.

    Answers exactly one question: is this name on the authority list today. It never guesses from the
    name's own spelling and it never reads the exemption file or the reviewed-names baseline: neither
    one carries the look_ahead answer, by design (docs/briefs/lookahead_guardrail_spec_brief.md section
    4). A name that is empty or None is a caller bug, not a safe answer, so it raises rather than
    quietly returning False for it.
    """
    if not name:  # covers both None and "" without a separate isinstance check
        raise ValueError(f"is_lookahead() requires a non-empty indicator name, got: {name!r}")
    return name in _load_authority_names()  # membership in the parsed, cached authority rows


def assert_not_lookahead(name: str, allow_lookahead: bool = False) -> None:
    """Raise unless `name` is safe to use, or the caller has explicitly opted in. This is the refusal
    point every future consumer calls (the generator first among them).

    Raises LookaheadViolation for any name on the authority list, UNLESS `allow_lookahead=True` is
    passed explicitly (the brief's own words: "It takes one explicit opt-in argument for the case
    where a caller genuinely wants a label generator"). Returns normally, regardless of the opt-in,
    for a name absent from the authority list: the opt-in only ever widens what a caller may
    deliberately choose, it never narrows what is already considered safe. Whether the generator is
    ever allowed to pass the opt-in is that project's own design decision (brief section 8), not
    decided here.
    """
    if not name:  # same caller-bug rule as is_lookahead(): never treat an empty/None name as safe
        raise ValueError(f"assert_not_lookahead() requires a non-empty indicator name, got: {name!r}")
    if is_lookahead(name) and not allow_lookahead:  # flagged, and nobody explicitly opted in: refuse
        reads = _load_authority_names()[name]["reads"]  # surface WHY it is refused, not just that it is
        raise LookaheadViolation(
            f"'{name}' is a future-reading VBT indicator ({reads}); refused by default. Pass "
            f"allow_lookahead=True only when the caller genuinely wants a label generator "
            f"(meta-labeling), never for a live trading signal."
        )
    # either the name is not flagged, or the caller explicitly opted in: nothing to refuse here
