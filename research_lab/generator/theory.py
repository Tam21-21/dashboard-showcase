"""The theory file loader (AD1, AD2). One file per theory, five mandatory '## ' headings
at column 0 in a fixed order (Mechanism, Counterparty, Kill rule, Skeletons, Fillers), plus
two optional ones (Expressions, Display name). Fail-loud on any missing heading, empty list, or
unresolvable skeleton/filler id, naming the file and the offending heading or id (R-G1,
R-G2). Runs the look-ahead gate over every resolved filler before the theory is accepted
(R-G21 site 1, AD2): a theory naming a flagged filler never loads at all.

Runs on the HOST only, never inside the vbt container: it needs no compiled indicator,
only the file's own text plus the look-ahead helper (which itself imports neither vbt nor
the dashboard package, loaded by path per gate.py's own docstring).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from research_lab.generator.gate import check_lookahead

# The one skeleton this build defines (AD3). A theory naming any other skeleton id is
# refused at load: R-G2's "a theory file missing any field never reaches data" reading
# extended to "names an unknown field never reaches data" (the loader's own fail-loud rule).
ADMISSIBLE_SKELETONS = ("band_cap",)

_REQUIRED_HEADINGS = ("Mechanism", "Counterparty", "Kill rule", "Skeletons", "Fillers")


class TheoryLoadError(ValueError):
    """Raised by load_theory on any refusal. Always names the theory file and the
    offending heading or id, matching the look-ahead loader's own fail-loud discipline
    (dashboard/backend/CLAUDE.md trap: 'fail-loud on purpose because an absent or empty
    list would otherwise read as nothing is flagged')."""


@dataclass(frozen=True)
class Theory:
    """One loaded, validated theory file (AD1's "Theory" layer)."""

    theory_id: str  # the file's stem: the theory's identity everywhere else in this unit
    path: Path
    theory_sha256: str  # content identity of the file's raw bytes (AD2)
    mechanism: str
    counterparty: str
    kill_rule: str
    skeletons: tuple[str, ...]
    fillers: tuple[str, ...]
    expressions: dict[str, str]  # name -> from_expr text, from the optional Expressions heading
    display_name: "str | None" = None  # DN1: the optional '## Display name' heading's text; a write run needs it


def _parse_headings(text: str) -> dict[str, str]:
    """Split the file on '## ' headings at column 0 into {heading: body}, the same
    structural shape lookahead_indicators.md and the guardrail's own loader use."""
    blocks = re.split(r"(?m)^## ", text)  # blocks[0] is whatever precedes the first heading
    out: dict[str, str] = {}
    for block in blocks[1:]:
        heading, _, body = block.partition("\n")
        out[heading.strip()] = body.strip("\n")
    return out


def _parse_filler_expression_names(headings: dict[str, str]) -> dict[str, str]:
    """Parse the optional '## Expressions' block into {name: from_expr text}. Each line is
    'name: text'; the FIRST colon splits the two, so a from_expr text containing its own
    colon (unlikely for arithmetic, but not assumed away) is preserved whole."""
    expressions: dict[str, str] = {}
    for line in headings.get("Expressions", "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, sep, expr_text = line.partition(":")
        if not sep:
            raise TheoryLoadError(f"malformed '## Expressions' line (no ':'): {line!r}")
        name = name.strip()
        expr_text = expr_text.strip()
        if not name or not expr_text:
            raise TheoryLoadError(f"malformed '## Expressions' line (empty name or text): {line!r}")
        expressions[name] = expr_text
    return expressions


def _validate_filler_id(filler_id: str, expressions: dict[str, str], path: Path) -> None:
    """A filler id resolves to exactly one of: a wqa101 alpha index 1-101, or a named
    from_expr template the theory's own Expressions block carries (AD2: 'the two lists ARE
    the allow-list ... an unresolvable id is a load-time refusal, not a runtime one')."""
    if filler_id.startswith("wqa101:"):
        _, _, n = filler_id.partition(":")
        if not n.isdigit() or not (1 <= int(n) <= 101):
            raise TheoryLoadError(f"theory file {path} names unresolvable filler id {filler_id!r}")
        return
    if filler_id.startswith("expr:"):
        _, _, name = filler_id.partition(":")
        if name not in expressions:
            raise TheoryLoadError(
                f"theory file {path} names filler {filler_id!r} with no matching "
                f"'## Expressions' line for {name!r}"
            )
        return
    raise TheoryLoadError(f"theory file {path} names unresolvable filler id {filler_id!r}")


def load_theory(path: "str | Path", *, lookahead_module=None) -> Theory:
    """Parse, validate and gate a theory file. Raises TheoryLoadError on any refusal.

    lookahead_module lets a probe substitute a fixture authority list; a production caller
    never passes it, so the real committed authority file is what every real load reads."""
    path = Path(path)
    if not path.exists():
        raise TheoryLoadError(f"theory file not found: {path}")
    raw = path.read_bytes()
    theory_sha256 = hashlib.sha256(raw).hexdigest()  # content identity (AD2)
    text = raw.decode("utf-8")
    theory_id = path.stem
    headings = _parse_headings(text)

    missing = [h for h in _REQUIRED_HEADINGS if h not in headings]
    if missing:
        raise TheoryLoadError(f"theory file {path} is missing required heading(s): {missing}")

    mechanism = headings["Mechanism"].strip()
    counterparty = headings["Counterparty"].strip()
    kill_rule = headings["Kill rule"].strip()
    for field_name, value in (("Mechanism", mechanism), ("Counterparty", counterparty), ("Kill rule", kill_rule)):
        if not value:
            raise TheoryLoadError(f"theory file {path} has an empty '## {field_name}' field")

    skeletons = tuple(ln.strip() for ln in headings["Skeletons"].splitlines() if ln.strip())
    fillers = tuple(ln.strip() for ln in headings["Fillers"].splitlines() if ln.strip())
    if not skeletons:
        raise TheoryLoadError(f"theory file {path} has an empty '## Skeletons' list")
    if not fillers:
        raise TheoryLoadError(f"theory file {path} has an empty '## Fillers' list")

    for skeleton_id in skeletons:
        if skeleton_id not in ADMISSIBLE_SKELETONS:
            raise TheoryLoadError(f"theory file {path} names unknown skeleton id {skeleton_id!r}")

    display_name = None
    if "Display name" in headings:
        display_name = headings["Display name"].strip()
        if not display_name:
            raise TheoryLoadError(f"theory file {path} has an empty '## Display name' heading")

    expressions = _parse_filler_expression_names(headings)
    for filler_id in fillers:
        _validate_filler_id(filler_id, expressions, path)

    # R-G21 site 1: every filler's resolved indicator name goes through the look-ahead
    # refusal point before the theory is accepted (AD2). A flagged name means the WHOLE
    # theory refuses to load; nothing is generated from it (AC-36 shape one).
    for filler_id in fillers:
        result = check_lookahead(filler_id, lookahead_module=lookahead_module)
        if not result.ok:
            raise TheoryLoadError(
                f"theory file {path} names flagged filler {filler_id!r}: refused by the "
                f"look-ahead gate ({result.reason})"
            )

    return Theory(
        theory_id=theory_id,
        path=path,
        theory_sha256=theory_sha256,
        mechanism=mechanism,
        counterparty=counterparty,
        kill_rule=kill_rule,
        skeletons=skeletons,
        fillers=fillers,
        expressions=expressions,
        display_name=display_name,
    )
