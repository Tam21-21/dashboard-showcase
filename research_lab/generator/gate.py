"""The two structural gates a generated expression passes before it can become a
research_strategies row (AD5). Both run on the HOST, never inside the vbt container:
neither gate needs a compiled indicator, only a resolved NAME or a plain formula string.

Gate 1, the allow-list (AD5.1 layer 2, R-G17/R-G18): tokenises the emitted expression and
refuses any token that names something outside the theory's own two lists plus the fixed
input/parameter vocabulary the facts file reports for that filler. Construction alone
(the loop only ever concatenating list words, layer 1) is not trusted on its own, because
vbt's own formula reader executes an expr string as Python (DECISIONS #329 ruling 1).

Gate 2, the look-ahead re-check (AD5.2 site 2, R-G21): the same refusal point theory.py
already ran at load time, run again right before the write, so a bug in the loop cannot
bypass it. Both gates return a small result object naming the refusal reason; neither
raises for an ordinary refusal, because a refusal here becomes a durable candidate row,
never an exception the caller has to catch (R-G19/R-G23).
"""
from __future__ import annotations

import importlib.util
import tokenize
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

# research_lab/generator/gate.py -> parents[0]=generator, [1]=research_lab, [2]=repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOOKAHEAD_PATH = _REPO_ROOT / "research_lab" / "strategies" / "helpers" / "lookahead.py"


def _load_lookahead_module():
    """Load lookahead.py BY PATH, never through strategies.helpers (whose __init__ eagerly
    imports portfolio.py, which imports strategies.base and pulls vbt in as a side effect).
    This is the same by-path route lookahead.py's own docstring prescribes for a process
    without vectorbtpro (AD5.2), and the host generator process is exactly such a process."""
    spec = importlib.util.spec_from_file_location("lookahead_by_path", _LOOKAHEAD_PATH)
    if spec is None or spec.loader is None:  # malformed path / loader: fail loud, no fallback
        raise ImportError(f"cannot load a module spec from {_LOOKAHEAD_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_filler_lookahead_name(filler_id: str) -> str:
    """The name a filler id presents to assert_not_lookahead (R-G21).

    A wqa101 filler's name is a synthetic, deterministic id built from its own filler id
    (':' replaced by '_'): the authority list only ever names VBT-native indicators
    (docs/reference/lookahead_indicators.md), so this name can never collide with one of
    the nine flagged names. That is exactly R-G24's residual, stated here rather than
    hidden: the gate always returns normally for a wqa101 filler, whatever that alpha does.

    An expr filler's name is the filler's OWN declared name (the text after 'expr:'),
    because that name is the theory author's own choice of what the filler computes. A
    theory that deliberately names an expr filler after one of the nine flagged indicators
    (e.g. 'expr:FIXLB') is exactly the case this gate exists to catch, and doing so gives
    the operator full, direct control over what name this gate checks for that filler.
    """
    if filler_id.startswith("wqa101:"):
        return filler_id.replace(":", "_")
    if filler_id.startswith("expr:"):
        _, _, name = filler_id.partition(":")
        return name
    raise ValueError(f"unresolvable filler id: {filler_id!r}")


@dataclass(frozen=True)
class LookaheadGateResult:
    ok: bool
    reason: str | None = None  # e.g. "lookahead:FIXLB"


def check_lookahead(filler_id: str, *, lookahead_module=None) -> LookaheadGateResult:
    """R-G21/AD5.2: run the resolved filler name through assert_not_lookahead.

    allow_lookahead is NEVER passed (R-G22): there is no parameter on this function that
    could carry it, so a grep for 'allow_lookahead=True' across this unit's own source
    finds nothing (AC-35)."""
    lookahead = lookahead_module or _load_lookahead_module()
    name = resolve_filler_lookahead_name(filler_id)
    try:
        lookahead.assert_not_lookahead(name)
    except lookahead.LookaheadViolation:
        return LookaheadGateResult(ok=False, reason=f"lookahead:{name}")
    return LookaheadGateResult(ok=True)


def _allowed_names(theory, facts_entry: dict) -> set[str]:
    """The allow-list AD5.1 describes: the theory's own two lists, the theory's own
    Expressions names, and the fixed input/parameter vocabulary the facts file reports
    for THIS filler (the facts are the only source of a compiled class's real input and
    parameter names, since the host never compiles anything itself, section 5.2)."""
    allowed = set(theory.skeletons) | set(theory.fillers) | set(theory.expressions.keys())
    allowed |= set(facts_entry.get("input_names", []))
    allowed |= set(facts_entry.get("param_names", []))
    return allowed


@dataclass(frozen=True)
class AllowlistGateResult:
    ok: bool
    reason: str | None = None  # e.g. "allowlist:os"


def check_allowlist(expression: str, theory, facts_entry: dict) -> AllowlistGateResult:
    """R-G17/R-G18, AD5.1 layer 2: tokenise the emitted expression and refuse the first
    NAME token that names anything outside the allow-list. Numbers, operators and
    punctuation are always permitted (the fixed operator vocabulary); only a bare
    identifier can name something dangerous (DECISIONS #329's own measured example,
    evaluate("__import__('os').name"))."""
    allowed = _allowed_names(theory, facts_entry)
    for tok in tokenize.generate_tokens(StringIO(expression).readline):
        if tok.type != tokenize.NAME:
            continue
        name = tok.string
        if name in allowed:
            continue
        return AllowlistGateResult(ok=False, reason=f"allowlist:{name}")
    return AllowlistGateResult(ok=True)
