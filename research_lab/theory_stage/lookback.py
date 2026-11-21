"""A theory's lookback: the walk-forward embargo of a group (lane GV2-7a, plan step 5b).

`_process_walkforward` (run_sweep_entrypoint.py) sets the embargo to the strategy's own lookback, the largest one over the grid's
combinations. A group has no strategy object, so the lookback is read from the theory: for each operand an entry or exit condition
reads, the bars its indicator needs before its first value (indicators/limits.py `lookback_bars`), plus the operand's offset and
span (offset shifts the series back, span makes the condition read that many earlier bars); a formula takes the largest of the
operands it names; a price field and a constant need 1. The theory's lookback is the largest over those operands, and a group's
is the largest over its variations. Never measured from member data.

Degrees of freedom (lane GV2-8b, DECISIONS #459 item 1): what judge check 1 asks of a cell as `used_degrees_of_freedom`, the #399
rule read for a theory: the theory's lookback, plus the number of distinct indicator operands the entry and exit conditions read
(directly or through a formula; each counts once, however many times it is read), plus the number of conditions. Example, the
two-average crossover at fast 20 and slow 50: lookback 50 (sma 50), 2 indicators (the fast and the slow average), 2 conditions
(fast above slow to enter, fast below slow to leave): 50 + 2 + 2 = 54. A price field and a constant are not indicators.
"""
from __future__ import annotations

import ast

from research_lab.indicators import limits


def _number(value, picks: dict, what: str):
    if isinstance(value, str):
        if value not in picks:
            raise ValueError("pick %r is missing: %s needs a value for it (have: %s)" % (value, what, ", ".join(sorted(picks))))
        return picks[value]
    return value


def _whole(value, what: str) -> int:
    if isinstance(value, bool) or float(value) != int(value):
        raise ValueError("%s must be a whole number, got %r" % (what, value))
    return int(value)


def operand_lookback(theory, name: str, picks: dict, _seen=()) -> int:
    if name in _seen:
        raise ValueError("operand %s names itself through its formula" % name)
    table = theory.operands[name]
    what = "operand %s" % name
    kind = table["kind"]
    if kind == "indicator":
        params = {}
        for pname, raw in table.get("params", {}).items():
            value = _number(raw, picks, what + " param " + pname)
            params[pname] = _whole(value, "%s param %s" % (what, pname)) if limits.PARAMS[table["indicator"]][pname][0] == "int" else float(value)
        base = limits.lookback_bars(table["indicator"], params)
    elif kind == "formula":
        names = [n.id for n in ast.walk(ast.parse(table["expr"], mode="eval")) if isinstance(n, ast.Name)]
        base = max([operand_lookback(theory, n, picks, _seen + (name,)) for n in names] or [1])
    else:
        base = 1
    offset = _whole(_number(table.get("offset", 0), picks, what + " offset"), what + " offset")
    span = _whole(_number(table.get("span", 0), picks, what + " span"), what + " span")
    return base + offset + span


def used_operands(theory) -> list:
    """The names of the operands the theory's entry and exit conditions read, sorted."""
    used = set()
    for group in list(theory.entry_groups) + list(theory.exit_groups):
        for left, _op, right, _part in group:
            for kind, value in (left, right):
                if kind == "operand":
                    used.add(value)
    return sorted(used)


def theory_lookback(theory, picks: dict) -> int:
    """The largest lookback over the operands the theory's entry and exit conditions read, at `picks`.

    A trailing stop reads the ATR over its `trail_atr` bars (GV3-S2): when the theory has one, that window's bars
    (limits.lookback_bars("atr", {"n": trail_atr})) count too, and the larger of the two is the theory's lookback.
    """
    used = used_operands(theory)
    if not used:
        raise ValueError("the theory's conditions read no operand, so it has no lookback")
    found = max(operand_lookback(theory, name, picks) for name in used)
    table = theory.data["exit"]
    if "trail" in table:
        window = _whole(_number(table["trail_atr"], picks, "exit.trail_atr"), "exit.trail_atr")
        found = max(found, limits.lookback_bars("atr", {"n": window}))
    return found


def group_lookback(theory, variations: list) -> int:
    """The largest theory lookback over the variations' picks (the way `_process_walkforward` takes the largest over combos)."""
    if not variations:
        raise ValueError("a group needs at least one variation")
    return max(theory_lookback(theory, picks) for picks in variations)


def _indicators_read(theory, name: str, found: set, _seen=()) -> None:
    """Add to `found` the indicator operands that operand `name` is or reads through its formula (the walk operand_lookback does)."""
    if name in _seen:
        raise ValueError("operand %s names itself through its formula" % name)
    table = theory.operands[name]
    if table["kind"] == "indicator":
        found.add(name)
    elif table["kind"] == "formula":
        for node in ast.walk(ast.parse(table["expr"], mode="eval")):
            if isinstance(node, ast.Name):
                _indicators_read(theory, node.id, found, _seen + (name,))


def theory_degrees_of_freedom(theory, picks: dict) -> int:
    """The theory's lookback at `picks`, plus its distinct indicator operands, plus its conditions (see the module docstring)."""
    found: set = set()
    for name in used_operands(theory):
        _indicators_read(theory, name, found)
    conditions = sum(len(group) for group in list(theory.entry_groups) + list(theory.exit_groups))
    return theory_lookback(theory, picks) + len(found) + conditions
