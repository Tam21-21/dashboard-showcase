"""Counting a theory at two levels (lane GV2-3, DECISIONS #441 item 4; words changed by lane GV3-T, #467).

A combination is one choice of variations (every operand that has variations keeps itself or takes one
of its alternatives). Its strategies are one per set of parameter picks, after the indicators' logic rules
(a MACD's fast window below its slow window) have dropped the sets that cannot exist. A sub-theory is now a
search round (#467 item 1); a combination of slot choices makes a theory, in templates (expand.py).

Only parameter picks count. A calibrated stop or target, or none, adds nothing: the exit calibration chooses
those on generated paths (DECISIONS #440 item 5). Nothing here runs a theory.
"""
from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass

from research_lab.indicators import limits
from research_lab.theories import form


@dataclass(frozen=True)
class Combination:
    """One combination of variations: `choices` maps each varied operand to the one used, `variables` are the letters it
    uses in form order, `count` is its number of strategies."""
    choices: tuple
    variables: tuple
    count: int


def _formula_names(expr: str) -> set:
    return {node.id for node in ast.walk(ast.parse(expr, mode="eval")) if isinstance(node, ast.Name)}


def _used_operands(theory: form.Theory, choice: dict) -> list:
    """The operands this combination reads, in the order they are first reached, variations applied."""
    start = []
    for rows in theory.entry_groups + theory.exit_groups:
        for left, _op, right, _part in rows:
            for side in (left, right):
                if side[0] == "operand":
                    start.append(side[1])
    expected = theory.data["exit"].get("expected_price")
    if expected is not None:
        start.append(expected)
    seen = []
    queue = [choice.get(n, n) for n in start]
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.append(name)
        op = theory.operands[name]
        if op["kind"] == "formula":
            queue.extend(choice.get(n, n) for n in sorted(_formula_names(op["expr"])))
    return seen


def _letters(theory: form.Theory, choice: dict, used: list) -> set:
    letters = set()

    def add(value):
        if isinstance(value, str) and value in theory.picks:
            letters.add(value)

    for name in used:
        op = theory.operands[name]
        for key in ("bar", "offset", "span", "value"):
            add(op.get(key))
        for value in op.get("params", {}).values():
            add(value)
    for rows in theory.entry_groups + theory.exit_groups:
        for left, _op, right, _part in rows:
            for side in (left, right):
                if side[0] == "letter":
                    letters.add(side[1])
    for key in ("stop", "target", "trail", "trail_atr", "max_hold"):
        add(theory.data["exit"].get(key))
    return letters


def _constraints(theory: form.Theory, used: list) -> list:
    """Logic rules of the used indicators as (smaller term, larger term); a term is a letter or a number."""
    pairs = []
    for name in used:
        op = theory.operands[name]
        if op["kind"] != "indicator":
            continue
        params = op.get("params", {})
        for small, large in limits.RULES.get(op["indicator"], []):
            pairs.append((params[small], params[large]))
    return pairs


def _count(theory: form.Theory, letters: list, pairs: list) -> int:
    """The product of the letters' value counts, with the pairs that break a logic rule dropped."""
    linked = {t for pair in pairs for t in pair if isinstance(t, str)}
    free = [l for l in letters if l not in linked]
    count = 1
    for letter in free:
        count *= len(theory.picks[letter].values)
    group = [l for l in letters if l in linked]
    if not group:
        return count
    # Letters tied together by a rule are enumerated together, in groups that share a rule.
    parent = {l: l for l in group}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for small, large in pairs:
        if isinstance(small, str) and isinstance(large, str):
            parent[find(small)] = find(large)
    components = {}
    for letter in group:
        components.setdefault(find(letter), []).append(letter)
    for members in components.values():
        total = 0
        for combo in itertools.product(*(theory.picks[m].values for m in members)):
            assignment = dict(zip(members, combo))
            ok = True
            for small, large in pairs:
                a = assignment.get(small, small) if isinstance(small, str) else small
                b = assignment.get(large, large) if isinstance(large, str) else large
                if (isinstance(small, str) and small not in assignment) or (isinstance(large, str) and large not in assignment):
                    continue
                if not a < b:
                    ok = False
                    break
            if ok:
                total += 1
        count *= total
    return count


def combinations(theory: form.Theory) -> list:
    """One Combination per choice of variations, in form order; a theory without variations is one."""
    keys = list(theory.variations)
    options = [[key] + list(theory.variations[key]) for key in keys]
    out = []
    for combo in itertools.product(*options):
        choice = dict(zip(keys, combo))
        used = _used_operands(theory, choice)
        found = _letters(theory, choice, used)
        variables = [letter for letter in theory.picks if letter in found]
        out.append(Combination(tuple(choice.items()), tuple(variables), _count(theory, variables, _constraints(theory, used))))
    return out


def total(theory: form.Theory) -> int:
    """The strategies of all combinations together."""
    return sum(sub.count for sub in combinations(theory))
