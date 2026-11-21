"""The theory form of generator v2 (lane GV2-3, DECISIONS #440 items 5 and 7, #441; templates by lane GV3-T, #467).

A theory is one TOML file: an entry signal and an exit signal built from operands (indicators, price fields,
constants or formulas), a risk and a money method, parameter picks that take their numbers from the ranges
file, and variations that turn one theory into combinations. `load` reads a theory and its ranges file and
refuses a bad form with `TheoryFormError`, which carries a code and the part it names. A template is the
same form with slots, each a list of choices; `load_template` checks it and every combination of its
choices, and expand.py writes one theory file per combination.

Standard library plus research_lab/indicators/limits.py only: no pandas, no vbt. Running a theory, the search
and the exit calibration are other lanes. README.md explains every part; TEMPLATE.toml holds every field.
"""
from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from research_lab.indicators import limits

CODES = (
    "missing_part",
    "unknown_indicator",
    "unknown_operand",
    "unknown_range",
    "wider_than_range",
    "outside_hard_limits",
    "missing_why",
    "expected_price_missing",
    "bad_time",
    "bad_op",
    "bad_risk",
    "bad_money",
    "bad_template",
    "bad_search",
)

DEFAULT_RANGES_PATH = Path(__file__).parent / "ranges.toml"

OPS = ("<", "<=", ">", ">=", "crosses_above", "crosses_below")
KINDS = ("indicator", "field", "constant", "formula")
FIELD_NAMES = ("open", "high", "low", "close", "volume")
# Earnings fields (lane EA4-0, DECISIONS #480 item 8): built by theory_run/earnings.py from an earnings slice, known only from the report's own time.
EARNINGS_FIELDS = ("eps_surprise_pct", "eps_beat_miss", "revenue_surprise_pct", "days_since_report", "report_move_pct")
SIDES = ("long",)
STATUSES = ("test", "research")
STOCK_GROUPS = ("trending", "volatile", "flat", "any")
# Hardcoded lists (DECISIONS #442): today one risk method and one money method, together DECISIONS #323.
RISK_METHODS = ("volatility_stop",)
MONEY_METHODS = ("fixed_fraction", "equal_slots")  # equal_slots: the generator v2 test's method (DECISIONS #445 item 1)
CALIBRATED = "calibrated"
NONE = "none"

# Every field path this module reads in a theory file. "*" stands for any operand name, letter or variation
# name. TEMPLATE.toml holds each of these with a comment line, and README.md has a heading for each name.
FIELDS = (
    "theory.name",
    "theory.bar",
    "theory.side",
    "theory.entry_time",
    "theory.status",
    "theory.kind",
    "theory.template",
    "theory.choices",
    "operand.*.kind",
    "operand.*.indicator",
    "operand.*.params",
    "operand.*.output",
    "operand.*.field",
    "operand.*.value",
    "operand.*.expr",
    "operand.*.bar",
    "operand.*.offset",
    "operand.*.span",
    "entry.group.conditions",
    "exit.group.conditions",
    "exit.stop",
    "exit.target",
    "exit.trail",
    "exit.trail_atr",
    "exit.max_hold",
    "exit.expected_price",
    "stocks.group",
    "stocks.universe",
    "risk.method",
    "money.method",
    "parameter_picks.*.range",
    "parameter_picks.*.from",
    "parameter_picks.*.to",
    "parameter_picks.*.step",
    "variations.*.alternatives",
    "slot.*.choices",
    "choice.*.words",
    "choice.*.operand",
    "search.values_per_number",
    "search.round_size",
    "search.best",
    "search.max_rounds",
    "search.all_below",
    "search.keep",
)

# The search settings of a theory's optional [search] table, in the order of DECISIONS #470 (lane GV3-S1): six whole numbers.
SEARCH_KEYS = ("values_per_number", "round_size", "best", "max_rounds", "all_below", "keep")

# The longest display name a theory may carry, as DN1 rules it (README.md, [theory]).
DISPLAY_NAME_MAX = 60
TEMPLATE_KIND = "template"
# Names of a template, a slot and a choice go into theory names and file names.
_NAME_PART = re.compile(r"^[a-z][a-z0-9_]*$")

# Fields of a ranges.toml table.
RANGE_FIELDS = ("from", "to", "step", "values", "why")

_OPERAND_FIELDS = {
    "indicator": ("kind", "indicator", "params", "output", "bar", "offset", "span"),
    "field": ("kind", "field", "bar", "offset", "span"),
    "constant": ("kind", "value", "bar", "offset", "span"),
    "formula": ("kind", "expr", "bar", "offset", "span"),
}


class TheoryFormError(ValueError):
    """A theory form or ranges file that breaks the form. `code` is one of CODES, `part` names the part."""

    def __init__(self, code: str, part: str, message: str):
        if code not in CODES:
            raise ValueError("unknown theory form error code %r" % code)
        super().__init__("%s [%s]: %s" % (code, part, message))
        self.code = code
        self.part = part
        self.detail = message


@dataclass(frozen=True)
class Range:
    """One ranges.toml table: from, to and step (lo, hi, step), or a list of values, and its one-line why."""
    key: str
    lo: Decimal | None
    hi: Decimal | None
    step: Decimal | None
    listed: tuple | None
    why: str
    int_valued: bool


@dataclass(frozen=True)
class Pick:
    """One parameter pick: a letter, the range it takes numbers from, and the values after any narrowing."""
    letter: str
    range_key: str
    lo: Decimal | None
    hi: Decimal | None
    step: Decimal | None
    listed: tuple | None
    values: tuple
    int_valued: bool


@dataclass
class Theory:
    path: str
    data: dict
    ranges: dict
    picks: dict
    operands: dict
    variations: dict
    entry_groups: list = field(default_factory=list)
    exit_groups: list = field(default_factory=list)


@dataclass
class Template:
    """A checked template: `slots` maps each slot to its choice names in list order, `choices` maps each choice
    name to its table (`words`, `operand`). `data` is the whole file."""
    path: str
    data: dict
    slots: dict
    choices: dict


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _dec(value) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _expect_table(value, part: str) -> dict:
    if not isinstance(value, dict):
        raise TheoryFormError("missing_part", part, "must be a table")
    return value


def _only_fields(table: dict, allowed: tuple, part: str) -> None:
    for name in table:
        if name not in allowed:
            raise TheoryFormError("missing_part", part, "unknown field '%s'; this part has: %s" % (name, ", ".join(allowed)))


def _parse_toml(text: str, label) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TheoryFormError("missing_part", str(label), "not valid TOML: %s" % exc) from exc


def _read_toml(path) -> dict:
    with open(path, "rb") as handle:
        return _parse_toml(handle.read().decode("utf-8"), path)


def _grid_values(lo: Decimal, hi: Decimal, step: Decimal, int_valued: bool) -> tuple:
    out = []
    k = 0
    while True:
        value = lo + step * k
        if value > hi:
            break
        out.append(int(value) if int_valued else float(value))
        k += 1
    return tuple(out)


def load_ranges(path=None) -> dict:
    """Read and check a ranges file. None means the ranges.toml beside this module, never the working directory."""
    data = _read_toml(DEFAULT_RANGES_PATH if path is None else path)
    ranges = {}
    for key, table in data.items():
        part = "ranges." + key
        table = _expect_table(table, part)
        _only_fields(table, RANGE_FIELDS, part)
        why = table.get("why")
        if not isinstance(why, str) or not why.strip() or "\n" in why.strip():
            raise TheoryFormError("missing_why", part, "every range needs a 'why': one non-empty line saying where it comes from")
        if "values" in table:
            if any(name in table for name in ("from", "to", "step")):
                raise TheoryFormError("missing_part", part, "a range is either from, to and step, or values, not both")
            values = table["values"]
            if not isinstance(values, list) or not values:
                raise TheoryFormError("missing_part", part + ".values", "values must be a non-empty list")
            if not (all(_is_number(v) for v in values) or all(isinstance(v, str) for v in values)):
                raise TheoryFormError("missing_part", part + ".values", "values must be all numbers or all text")
            ranges[key] = Range(key, None, None, None, tuple(values), why.strip(), all(_is_int(v) for v in values))
            continue
        for name in ("from", "to", "step"):
            if not _is_number(table.get(name)):
                raise TheoryFormError("missing_part", part + "." + name, "'%s' is missing or not a number" % name)
        lo, hi, step = _dec(table["from"]), _dec(table["to"]), _dec(table["step"])
        if step <= 0 or lo > hi:
            raise TheoryFormError("missing_part", part, "needs step above 0 and from not above to")
        int_valued = all(_is_int(table[name]) for name in ("from", "to", "step"))
        ranges[key] = Range(key, lo, hi, step, None, why.strip(), int_valued)
    return ranges


def _narrow(letter: str, table: dict, ranges: dict) -> Pick:
    part = "parameter_picks." + letter
    table = _expect_table(table, part)
    _only_fields(table, ("range", "from", "to", "step"), part)
    key = table.get("range")
    if not isinstance(key, str) or not key:
        raise TheoryFormError("missing_part", part + ".range", "a pick names the ranges.toml key it takes numbers from")
    if key not in ranges:
        raise TheoryFormError("unknown_range", part, "range '%s' is not in the ranges file; it has: %s" % (key, ", ".join(sorted(ranges))))
    rng = ranges[key]
    given = [name for name in ("from", "to", "step") if name in table]
    if rng.listed is not None:
        if given:
            raise TheoryFormError("wider_than_range", part, "range '%s' is a list; a pick cannot narrow it with %s" % (key, ", ".join(given)))
        return Pick(letter, key, None, None, None, rng.listed, rng.listed, rng.int_valued)
    for name in given:
        if not _is_number(table[name]):
            raise TheoryFormError("missing_part", part + "." + name, "'%s' must be a number" % name)
    lo = _dec(table["from"]) if "from" in table else rng.lo
    hi = _dec(table["to"]) if "to" in table else rng.hi
    step = _dec(table["step"]) if "step" in table else rng.step
    if lo < rng.lo or hi > rng.hi:
        raise TheoryFormError(
            "wider_than_range", part, "%s to %s is outside range '%s' (%s to %s); a theory may narrow a range, never widen it"
            % (lo, hi, key, rng.lo, rng.hi))
    if lo > hi:
        raise TheoryFormError("wider_than_range", part, "from %s is above to %s" % (lo, hi))
    if (lo - rng.lo) % rng.step != 0 or (hi - rng.lo) % rng.step != 0:
        raise TheoryFormError(
            "wider_than_range", part, "%s to %s is off the step %s of range '%s' (which starts at %s)" % (lo, hi, rng.step, key, rng.lo))
    if step <= 0 or step % rng.step != 0:
        raise TheoryFormError(
            "wider_than_range", part, "step %s is not a whole multiple of step %s of range '%s'" % (step, rng.step, key))
    int_valued = rng.int_valued and all(_is_int(table[name]) for name in given)
    return Pick(letter, key, lo, hi, step, None, _grid_values(lo, hi, step, int_valued), int_valued)


def _parse_entry_time(value) -> None:
    part = "theory.entry_time"
    if value in ("next_open", "same_close"):
        return
    if isinstance(value, str) and value.startswith("after_open:"):
        minutes = value.split(":", 1)[1]
        if minutes.isdigit() and int(minutes) > 0:
            return
    raise TheoryFormError(
        "bad_time", part, "%r is not a time; use next_open, same_close or after_open:<minutes> with minutes above 0" % (value,))


def _letter_or_number(value, part: str, picks: dict, integer: bool = False, minimum=None, strict_minimum=False) -> None:
    """A number or a letter (a parameter pick) in a place that takes a number."""
    if isinstance(value, str):
        if value not in picks:
            raise TheoryFormError("missing_part", "parameter_picks." + value, "'%s' is used in %s but is not a parameter pick" % (value, part))
        return
    if not _is_number(value):
        raise TheoryFormError("missing_part", part, "must be a number or a letter (a parameter pick), got %r" % (value,))
    if integer and not _is_int(value):
        raise TheoryFormError("missing_part", part, "must be a whole number or a letter, got %r" % (value,))
    _minimum_check(value, part, minimum, strict_minimum)


def _minimum_check(value, part: str, minimum, strict: bool) -> None:
    if minimum is None:
        return
    if (value <= minimum) if strict else (value < minimum):
        raise TheoryFormError(
            "outside_hard_limits", part, "%r must be %s %s" % (value, "above" if strict else "at least", minimum))


def _parse_formula(name: str, expr, operands: dict) -> set:
    part = "operand.%s.expr" % name
    if not isinstance(expr, str) or not expr.strip():
        raise TheoryFormError("missing_part", part, "a formula needs 'expr', a text over operand names, numbers, + - * /")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise TheoryFormError("bad_op", part, "'%s' is not a formula: %s" % (expr, exc.msg)) from exc
    names = set()

    def walk(node):
        if isinstance(node, ast.Expression):
            walk(node.body)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            walk(node.operand)
        elif isinstance(node, ast.Constant) and _is_number(node.value):
            return
        elif isinstance(node, ast.Name):
            if node.id not in operands:
                raise TheoryFormError("unknown_operand", part, "'%s' is not an operand; the operands are: %s" % (node.id, ", ".join(operands)))
            names.add(node.id)
        else:
            raise TheoryFormError("bad_op", part, "'%s' uses something other than numbers, operand names and + - * /" % expr)

    walk(tree)
    return names


def _check_formula_cycles(formulas: dict) -> None:
    for start in formulas:
        stack = [(start, [start])]
        while stack:
            current, trail = stack.pop()
            for nxt in formulas.get(current, ()):
                if nxt == start:
                    raise TheoryFormError(
                        "unknown_operand", "operand.%s.expr" % start, "formula refers back to itself: %s" % " -> ".join(trail + [start]))
                if nxt in formulas and nxt not in trail:
                    stack.append((nxt, trail + [nxt]))


def _parse_operand(name: str, table, picks: dict) -> dict:
    part = "operand." + name
    table = _expect_table(table, part)
    kind = table.get("kind")
    if kind not in KINDS:
        raise TheoryFormError("missing_part", part + ".kind", "kind must be one of %s, got %r" % (", ".join(KINDS), kind))
    _only_fields(table, _OPERAND_FIELDS[kind], part)
    if "bar" in table:
        bar = table["bar"]
        if not isinstance(bar, str) or not bar:
            raise TheoryFormError("missing_part", part + ".bar", "bar must be a bar size such as \"1d\" or a letter")
    for key in ("offset", "span"):
        if key in table:
            _letter_or_number(table[key], "%s.%s" % (part, key), picks, integer=True, minimum=0)
    if kind == "indicator":
        indicator = table.get("indicator")
        if indicator not in limits.PARAMS:
            raise TheoryFormError(
                "unknown_indicator", part + ".indicator",
                "indicator %r is not in the library; known indicators: %s" % (indicator, ", ".join(sorted(limits.PARAMS))))
        params = _expect_table(table.get("params", {}), part + ".params")
        for pname, value in params.items():
            _letter_or_number(value, "%s.params.%s" % (part, pname), picks)
        outputs = limits.OUTPUTS[indicator]
        output = table.get("output")
        if output is None:
            if len(outputs) != 1:
                raise TheoryFormError(
                    "missing_part", part + ".output", "%s has several outputs, so 'output' is required: %s" % (indicator, ", ".join(outputs)))
        elif output not in outputs:
            raise TheoryFormError(
                "unknown_indicator", part + ".output", "%s has no output %r; it has: %s" % (indicator, output, ", ".join(outputs)))
    elif kind == "field":
        if table.get("field") not in FIELD_NAMES + EARNINGS_FIELDS:
            raise TheoryFormError(
                "missing_part", part + ".field",
                "field must be one of %s (price fields) or %s (earnings fields), got %r" % (
                    ", ".join(FIELD_NAMES), ", ".join(EARNINGS_FIELDS), table.get("field")))
    elif kind == "constant":
        if "value" not in table:
            raise TheoryFormError("missing_part", part + ".value", "a constant needs 'value', a number or a letter")
        _letter_or_number(table["value"], part + ".value", picks)
    return dict(table)


def _side(value, part: str, operands: dict, picks: dict):
    """Classify one side of a condition: ("operand", name), ("letter", name) or ("number", value)."""
    if isinstance(value, bool):
        raise TheoryFormError("missing_part", part, "a condition side is an operand name, a number or a letter, got %r" % (value,))
    if _is_number(value):
        return ("number", value)
    if isinstance(value, str):
        if value in operands and value in picks:
            raise TheoryFormError("unknown_operand", part, "'%s' is both an operand and a parameter pick; rename one" % value)
        if value in operands:
            return ("operand", value)
        if value in picks:
            return ("letter", value)
        raise TheoryFormError(
            "unknown_operand", part, "'%s' is neither an operand nor a parameter pick; operands: %s" % (value, ", ".join(operands)))
    raise TheoryFormError("missing_part", part, "a condition side is an operand name, a number or a letter, got %r" % (value,))


def _parse_groups(data: dict, which: str, operands: dict, picks: dict, required: bool) -> list:
    part = which + ".group"
    table = data.get(which)
    if table is None:
        if required:
            raise TheoryFormError("missing_part", which, "the %s part is missing" % which)
        return []
    table = _expect_table(table, which)
    groups = table.get("group")
    if groups is None:
        if required:
            raise TheoryFormError("missing_part", part, "at least one [[%s]] group of conditions is required" % part)
        return []
    if not isinstance(groups, list) or not groups:
        raise TheoryFormError("missing_part", part, "must be a list of groups: use [[%s]]" % part)
    parsed = []
    for gi, group in enumerate(groups):
        gpart = "%s[%d]" % (part, gi)
        group = _expect_table(group, gpart)
        _only_fields(group, ("conditions",), gpart)
        conditions = group.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise TheoryFormError("missing_part", gpart + ".conditions", "a group needs a non-empty list of conditions")
        rows = []
        for ci, cond in enumerate(conditions):
            cpart = "%s.conditions[%d]" % (gpart, ci)
            cond = _expect_table(cond, cpart)
            _only_fields(cond, ("left", "op", "right"), cpart)
            for name in ("left", "op", "right"):
                if name not in cond:
                    raise TheoryFormError("missing_part", cpart + "." + name, "a condition needs left, op and right")
            if cond["op"] not in OPS:
                raise TheoryFormError("bad_op", cpart, "operator %r is not allowed; use one of %s" % (cond["op"], ", ".join(OPS)))
            rows.append((_side(cond["left"], cpart + ".left", operands, picks), cond["op"],
                         _side(cond["right"], cpart + ".right", operands, picks), cpart))
        parsed.append(rows)
    return parsed


def _check_ends(letter_values: dict, name: str, params: dict, part: str) -> None:
    """Run limits.check_params at each end of every lettered parameter of one indicator operand."""
    for target, values in letter_values.items():
        for end in (min(values), max(values)):
            trial = {}
            for pname, value in params.items():
                if pname == target:
                    trial[pname] = end
                elif pname in letter_values:
                    trial[pname] = min(letter_values[pname])
                else:
                    trial[pname] = value
            try:
                limits.check_params(name, trial)
            except limits.IndicatorLogicRuleError:
                # A logic rule between two lettered parameters (fast below slow) is not a range fault: the
                # grid drops the pairs that break it. Every other limit is checked first inside check_params.
                continue
            except limits.IndicatorParamError as exc:
                raise TheoryFormError("outside_hard_limits", part, str(exc)) from exc


def _value_ends(term, operands: dict, picks: dict):
    """The numbers a value-like condition side can take, or None when the side is not value-like."""
    kind, content = term
    if kind == "number":
        return [content]
    if kind == "letter":
        values = picks[content].values
        return list(values) if all(_is_number(v) for v in values) else None
    op = operands[content]
    if op["kind"] == "constant":
        value = op["value"]
        if isinstance(value, str):
            values = picks[value].values
            return list(values) if all(_is_number(v) for v in values) else None
        return [value]
    return None


def _check_hard_limits(theory_operands: dict, picks: dict, variations: dict, groups: list, exit_table: dict) -> None:
    for name, op in theory_operands.items():
        part = "operand.%s.params" % name
        if op["kind"] == "indicator":
            params = op.get("params", {})
            letter_values = {p: picks[v].values for p, v in params.items() if isinstance(v, str)}
            for pname, values in letter_values.items():
                if not all(_is_number(v) for v in values):
                    raise TheoryFormError("outside_hard_limits", part, "parameter '%s' takes a list of text, not numbers" % pname)
            if letter_values:
                _check_ends(letter_values, op["indicator"], params, part)
            else:
                try:
                    limits.check_params(op["indicator"], params)
                except limits.IndicatorParamError as exc:
                    raise TheoryFormError("outside_hard_limits", part, str(exc)) from exc
        for key in ("offset", "span"):
            if isinstance(op.get(key), str):
                for value in picks[op[key]].values:
                    _minimum_check(value, "operand.%s.%s" % (name, key), 0, False)
                    if not _is_int(value):
                        raise TheoryFormError("outside_hard_limits", "operand.%s.%s" % (name, key), "%r must be a whole number" % (value,))
    # key: (smallest allowed value, whether the value must be above it, whether it must be whole)
    exit_limits = {"max_hold": (1, False, False), "stop": (0, True, False), "target": (0, True, False),
                   "trail": (0, True, False), "trail_atr": (2, False, True)}
    for key, (minimum, strict, whole) in exit_limits.items():
        value = exit_table.get(key)
        if isinstance(value, str) and value in picks:
            numbers = picks[value].values
        elif _is_number(value):
            numbers = [value]
        else:
            continue
        for number in numbers:
            if not _is_number(number):
                raise TheoryFormError("outside_hard_limits", "exit." + key, "%r is not a number" % (number,))
            _minimum_check(number, "exit." + key, minimum, strict)
            if whole and not _is_int(number):
                raise TheoryFormError("outside_hard_limits", "exit." + key, "%r must be a whole number" % (number,))
    # A letter or a number compared with an indicator output must be a value that output can take, for the
    # indicator and for every indicator that a variation may put in its place.
    for rows in groups:
        for left, _op, right, cpart in rows:
            for first, second in ((left, right), (right, left)):
                if first[0] != "operand":
                    continue
                variants = [first[1]] + list(variations.get(first[1], ()))
                for variant in variants:
                    op = theory_operands[variant]
                    if op["kind"] != "indicator":
                        continue
                    outputs = limits.OUTPUTS[op["indicator"]]
                    output = op.get("output") or next(iter(outputs))
                    if second[0] == "operand" and theory_operands[second[1]]["kind"] == "indicator":
                        continue
                    values = _value_ends(second, theory_operands, picks)
                    if values is None:
                        continue
                    for value in {min(values), max(values)}:
                        try:
                            limits.check_threshold(op["indicator"], output, value)
                        except limits.IndicatorThresholdError as exc:
                            raise TheoryFormError("outside_hard_limits", cpart, str(exc)) from exc


def earnings_fields(theory: Theory) -> tuple:
    """The sorted earnings field names the theory's operands read; () when it reads none."""
    return tuple(sorted({t["field"] for t in theory.operands.values() if t["kind"] == "field" and t["field"] in EARNINGS_FIELDS}))


def load(path, ranges_path=None) -> Theory:
    """Read a theory file and check it against the form and the ranges file. Raises TheoryFormError."""
    with open(path, "rb") as handle:
        text = handle.read().decode("utf-8")
    return loads(text, path, ranges_path)


def _check_search(data: dict) -> None:
    """The optional [search] table (DECISIONS #470): all six keys, each a whole number (a boolean is not one).

    values_per_number is at least 2 (a box of one value per number cannot narrow), every other key at least 1, and best is
    not above round_size (a round cannot narrow around more strategies than it ran).
    """
    if "search" not in data:
        return
    table = _expect_table(data["search"], "search")
    _only_fields(table, SEARCH_KEYS, "search")
    for key in SEARCH_KEYS:
        if key not in table:
            raise TheoryFormError("missing_part", "search." + key, "[search] holds all of %s; '%s' is missing" % (", ".join(SEARCH_KEYS), key))
    for key in SEARCH_KEYS:
        value = table[key]
        least = 2 if key == "values_per_number" else 1
        if not _is_int(value) or value < least:
            raise TheoryFormError("bad_search", "search." + key, "%s must be a whole number of at least %d, got %r" % (key, least, value))
    if table["best"] > table["round_size"]:
        raise TheoryFormError("bad_search", "search.best", "best (%d) cannot be above round_size (%d)" % (table["best"], table["round_size"]))


def loads(text: str, label, ranges_path=None) -> Theory:
    """Check theory text against the form and the ranges file; `label` names it in errors. Raises TheoryFormError."""
    data = _parse_toml(text, label)
    ranges = load_ranges(ranges_path)
    head = data.get("theory")
    if isinstance(head, dict) and "kind" in head:
        raise TheoryFormError(
            "bad_template", "theory.kind",
            "kind is %r: %s" % (head["kind"], "this file is a template, expand it first with python -m research_lab.theories.expand"
                                if head["kind"] == TEMPLATE_KIND else "the only kind is \"template\""))
    for table in ("slot", "choice"):
        if table in data:
            raise TheoryFormError("bad_template", table, "[%s] belongs in a template; a theory has none (expand the template first)" % table)
    _only_fields(data, ("theory", "operand", "entry", "exit", "stocks", "risk", "money", "parameter_picks", "variations", "search"), "file")

    # [theory]
    if "theory" not in data:
        raise TheoryFormError("missing_part", "theory", "the [theory] part is missing")
    head = _expect_table(data["theory"], "theory")
    _only_fields(head, ("name", "bar", "side", "entry_time", "status", "display_name", "template", "choices"), "theory")
    if ("template" in head) != ("choices" in head):
        raise TheoryFormError("missing_part", "theory.template", "template and choices come together: an expanded theory names its template and its choices")
    if "template" in head:
        if not isinstance(head["template"], str) or not head["template"].strip():
            raise TheoryFormError("missing_part", "theory.template", "template must be a non-empty text, the template's name")
        chosen = head["choices"]
        if not isinstance(chosen, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in chosen.items()):
            raise TheoryFormError("missing_part", "theory.choices", "choices must be a table of text to text: slot name to the choice taken")
    if "display_name" in head and (not isinstance(head["display_name"], str) or not head["display_name"].strip()):
        raise TheoryFormError("missing_part", "theory.display_name", "display_name, when given, must be a non-empty text")
    if not isinstance(head.get("name"), str) or not head["name"].strip():
        raise TheoryFormError("missing_part", "theory.name", "a theory needs a non-empty name")
    if not isinstance(head.get("bar", "1d"), str) or not head.get("bar", "1d"):
        raise TheoryFormError("missing_part", "theory.bar", "bar must be a bar size such as \"1d\"")
    if head.get("side", "long") not in SIDES:
        raise TheoryFormError("missing_part", "theory.side", "side must be one of %s, got %r" % (", ".join(SIDES), head.get("side")))
    if head.get("status") not in STATUSES:
        raise TheoryFormError("missing_part", "theory.status", "status must be one of %s, got %r" % (", ".join(STATUSES), head.get("status")))
    if "entry_time" not in head:
        raise TheoryFormError("missing_part", "theory.entry_time", "a theory needs an entry_time")
    _parse_entry_time(head["entry_time"])

    # [parameter_picks.<letter>]
    picks = {}
    for letter, table in _expect_table(data.get("parameter_picks", {}), "parameter_picks").items():
        picks[letter] = _narrow(letter, table, ranges)

    # [operand.<name>]
    if not data.get("operand"):
        raise TheoryFormError("missing_part", "operand", "a theory needs at least one [operand.<name>]")
    raw_operands = _expect_table(data["operand"], "operand")
    operands = {name: _parse_operand(name, table, picks) for name, table in raw_operands.items()}
    formulas = {name: _parse_formula(name, op.get("expr"), operands) for name, op in operands.items() if op["kind"] == "formula"}
    _check_formula_cycles(formulas)

    # [[entry.group]], [[exit.group]], [exit]
    entry_groups = _parse_groups(data, "entry", operands, picks, required=True)
    exit_table = _expect_table(data.get("exit", {}), "exit") if "exit" in data else None
    if exit_table is None:
        raise TheoryFormError("missing_part", "exit", "the [exit] part is missing")
    exit_groups = _parse_groups(data, "exit", operands, picks, required=False)
    _only_fields(exit_table, ("group", "stop", "target", "trail", "trail_atr", "max_hold", "expected_price"), "exit")
    # A trailing stop needs both its multiple and the ATR window it reads: both or neither.
    for given, other in (("trail", "trail_atr"), ("trail_atr", "trail")):
        if given in exit_table and other not in exit_table:
            raise TheoryFormError("missing_part", "exit." + other, "a trailing stop needs both trail and trail_atr")
    if "trail" in exit_table:
        _letter_or_number(exit_table["trail"], "exit.trail", picks)
        _letter_or_number(exit_table["trail_atr"], "exit.trail_atr", picks, integer=True)
    for key in ("stop", "target"):
        value = exit_table.get(key, NONE)
        if value in (CALIBRATED, NONE):
            continue
        if isinstance(value, bool) or not (isinstance(value, str) or _is_number(value)):
            raise TheoryFormError("missing_part", "exit." + key, "%s must be \"calibrated\", \"none\", a letter or a number" % key)
        _letter_or_number(value, "exit." + key, picks)
    if "max_hold" in exit_table:
        _letter_or_number(exit_table["max_hold"], "exit.max_hold", picks, integer=True)
    calibrated = CALIBRATED in (exit_table.get("stop"), exit_table.get("target"))
    if calibrated and "expected_price" not in exit_table:
        raise TheoryFormError(
            "expected_price_missing", "exit.expected_price", "a calibrated stop or target needs expected_price, the operand that is the price expected after the move")
    if "expected_price" in exit_table and exit_table["expected_price"] not in operands:
        raise TheoryFormError(
            "unknown_operand", "exit.expected_price", "'%s' is not an operand; the operands are: %s" % (exit_table["expected_price"], ", ".join(operands)))
    way_out = exit_groups or "max_hold" in exit_table or "trail" in exit_table or any(exit_table.get(k, NONE) != NONE for k in ("stop", "target"))
    if not way_out:
        raise TheoryFormError("missing_part", "exit", "a theory needs a way out: an exit group, a stop, a target, a trail or max_hold")

    # [stocks], [risk], [money]
    if "stocks" not in data:
        raise TheoryFormError("missing_part", "stocks", "the [stocks] part is missing")
    stocks = _expect_table(data["stocks"], "stocks")
    _only_fields(stocks, ("group", "universe"), "stocks")
    if ("group" in stocks) == ("universe" in stocks):
        raise TheoryFormError("missing_part", "stocks", "give exactly one of group and universe")
    if "group" in stocks and stocks["group"] not in STOCK_GROUPS:
        raise TheoryFormError("missing_part", "stocks.group", "group must be one of %s, got %r" % (", ".join(STOCK_GROUPS), stocks["group"]))
    if "universe" in stocks and (not isinstance(stocks["universe"], str) or not stocks["universe"].strip()):
        raise TheoryFormError("missing_part", "stocks.universe", "universe must be a non-empty text")
    for part, lst, code in (("risk", RISK_METHODS, "bad_risk"), ("money", MONEY_METHODS, "bad_money")):
        if part not in data:
            raise TheoryFormError("missing_part", part, "the [%s] part is missing" % part)
        table = _expect_table(data[part], part)
        _only_fields(table, ("method",), part)
        if "method" not in table:
            raise TheoryFormError("missing_part", part + ".method", "%s needs a method" % part)
        if table["method"] not in lst:
            raise TheoryFormError(code, part + ".method", "%s method %r is not allowed; the list is: %s" % (part, table["method"], ", ".join(lst)))

    # [variations.<operand>]
    variations = {}
    for key, table in _expect_table(data.get("variations", {}), "variations").items():
        part = "variations." + key
        table = _expect_table(table, part)
        _only_fields(table, ("alternatives",), part)
        if key not in operands:
            raise TheoryFormError("unknown_operand", part, "'%s' is not an operand" % key)
        alts = table.get("alternatives")
        if not isinstance(alts, list) or not alts or not all(isinstance(a, str) for a in alts):
            raise TheoryFormError("missing_part", part + ".alternatives", "alternatives must be a non-empty list of operand names")
        for alt in alts:
            if alt not in operands:
                raise TheoryFormError("unknown_operand", part + ".alternatives", "'%s' is not an operand" % alt)
        variations[key] = tuple(alts)

    _check_search(data)
    _check_hard_limits(operands, picks, variations, entry_groups + exit_groups, exit_table)
    return Theory(str(label), data, ranges, picks, operands, variations, entry_groups, exit_groups)


# ---------------------------------------------------------------------------------------------------------
# Templates (lane GV3-T, DECISIONS #467 item 1): a theory form with slots. Each slot has a list of choices,
# each choice fills some operands, and each combination of choices is one theory (expand.py writes them).

_TEMPLATE_PARTS = ("theory", "operand", "entry", "exit", "stocks", "risk", "money", "parameter_picks", "slot", "choice", "search")


def _bad_template(part: str, message: str) -> TheoryFormError:
    return TheoryFormError("bad_template", part, message)


def _check_name(name, part: str) -> None:
    if not isinstance(name, str) or not _NAME_PART.match(name) or "__" in name:
        raise _bad_template(part, "%r must be lower case letters, digits and single underscores, starting with a letter" % (name,))


def _condition_names(data: dict) -> set:
    """Every name a condition, the expected price or a formula of the template text reads."""
    names = set()
    for which in ("entry", "exit"):
        table = data.get(which)
        groups = table.get("group") if isinstance(table, dict) else None
        for group in groups if isinstance(groups, list) else ():
            for cond in group.get("conditions", ()) if isinstance(group, dict) else ():
                if isinstance(cond, dict):
                    names.update(v for k, v in cond.items() if k in ("left", "right") and isinstance(v, str))
    exit_table = data.get("exit")
    if isinstance(exit_table, dict) and isinstance(exit_table.get("expected_price"), str):
        names.add(exit_table["expected_price"])
    tables = list(data.get("operand", {}).values()) if isinstance(data.get("operand"), dict) else []
    for choice in data.get("choice", {}).values() if isinstance(data.get("choice"), dict) else ():
        if isinstance(choice, dict) and isinstance(choice.get("operand"), dict):
            tables.extend(choice["operand"].values())
    for table in tables:
        if isinstance(table, dict) and table.get("kind") == "formula" and isinstance(table.get("expr"), str):
            try:
                names.update(node.id for node in ast.walk(ast.parse(table["expr"], mode="eval")) if isinstance(node, ast.Name))
            except SyntaxError:
                continue  # the expanded theory's own load names the bad formula
    return names


def read_template(path) -> Template:
    """Read a template file and check its structure: its slots and choices, not the theories they make.

    `load_template` also expands and loads every combination. Raises TheoryFormError, code bad_template for a
    fault of the template form."""
    data = _read_toml(path)
    if "theory" not in data:
        raise TheoryFormError("missing_part", "theory", "the [theory] part is missing")
    head = _expect_table(data["theory"], "theory")
    if head.get("kind") != TEMPLATE_KIND:
        raise _bad_template("theory.kind", "a template has kind = \"template\" in [theory], got %r" % (head.get("kind"),))
    if "variations" in data:
        raise _bad_template("variations", "a template has no [variations]: its slots are the choices")
    _only_fields(data, _TEMPLATE_PARTS, "file")
    _only_fields(head, ("name", "display_name", "kind", "bar", "side", "entry_time", "status"), "theory")
    _check_name(head.get("name"), "theory.name")
    display = head.get("display_name")
    if not isinstance(display, str) or not display.strip():
        raise _bad_template("theory.display_name", "a template needs a display_name, a non-empty text")
    if len(display) > DISPLAY_NAME_MAX:
        raise _bad_template("theory.display_name", "display_name is %d characters; the most is %d" % (len(display), DISPLAY_NAME_MAX))

    raw_choices = _expect_table(data.get("choice", {}), "choice")
    slots = {}
    listed = {}
    for slot, table in _expect_table(data.get("slot", {}), "slot").items():
        _check_name(slot, "slot." + slot)
        part = "slot.%s.choices" % slot
        table = _expect_table(table, "slot." + slot)
        _only_fields(table, ("choices",), "slot." + slot)
        names = table.get("choices")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise _bad_template(part, "choices must be a non-empty list of choice names")
        for name in names:
            if name not in raw_choices:
                raise _bad_template(part, "choice '%s' has no [choice.%s] table" % (name, name))
            if name in listed:
                raise _bad_template(part, "choice '%s' is already listed in slot '%s'; a choice belongs to one slot, once" % (name, listed[name]))
            listed[name] = slot
        slots[slot] = tuple(names)

    choices = {}
    for name, table in raw_choices.items():
        part = "choice." + name
        _check_name(name, part)
        table = _expect_table(table, part)
        _only_fields(table, ("words", "operand"), part)
        if name not in listed:
            raise _bad_template(part, "choice '%s' is in no slot's choices list" % name)
        words = table.get("words")
        if not isinstance(words, str) or not words.strip():
            raise _bad_template(part + ".words", "a choice needs words, the plain words its display name uses")
        operands = _expect_table(table.get("operand", {}), part + ".operand")
        if not operands:
            raise _bad_template(part + ".operand", "a choice fills at least one operand")
        choices[name] = table

    read = _condition_names(data)
    own = _expect_table(data.get("operand", {}), "operand")
    owner = {}
    for slot, names in slots.items():
        filled = {}
        for name in names:
            for operand in choices[name]["operand"]:
                part = "choice.%s.operand.%s" % (name, operand)
                if operand in own:
                    raise _bad_template(part, "the template already has [operand.%s]; a choice may not fill it too" % operand)
                if owner.setdefault(operand, slot) != slot:
                    raise _bad_template(part, "slot '%s' already fills operand '%s'; one operand has one slot" % (owner[operand], operand))
                if operand not in read:
                    raise _bad_template(part, "no condition, expected_price or formula reads operand '%s'" % operand)
            filled[name] = set(choices[name]["operand"])
        first = filled[names[0]]
        for name in names[1:]:
            if filled[name] != first:
                raise _bad_template(
                    "choice.%s.operand" % name,
                    "slot '%s' needs every choice to fill the same operands: '%s' fills %s, '%s' fills %s"
                    % (slot, names[0], sorted(first), name, sorted(filled[name])))
    return Template(str(path), data, slots, choices)


def load_template(path, ranges_path=None) -> Template:
    """Read a template, check its structure, then expand it and load every combination (expand.py).

    A fault in a combination is raised with the combination's name in the message. Raises TheoryFormError."""
    from research_lab.theories import expand  # expand.py imports this module, so the import is made when used

    template = read_template(path)
    expand.expand_template(template, ranges_path)
    return template
