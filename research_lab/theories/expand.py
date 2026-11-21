"""Expanding a template into theories (lane GV3-T, DECISIONS #467 item 1).

A template is a theory form with slots; each slot lists choices and each choice fills some operands. Every
combination of one choice per slot is one theory. `expand` returns one `Expanded` per combination, slots in
file order and choices in list order (the first slot changes slowest), and checks that each one loads with
`form.loads`. The command line writes one loading theory file per combination:

    python -m research_lab.theories.expand <template> --out <folder>

Standard library plus form.py only. Nothing here runs a theory. README.md, "Templates", explains the form.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import math
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from research_lab.theories import form

_SECTIONS = ("operand", "entry", "exit", "stocks", "risk", "money", "parameter_picks", "search")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_THEORY_DROPPED = ("name", "display_name", "kind")


@dataclass(frozen=True)
class Expanded:
    """One theory made from a template: its name, display name, the choice taken in each slot, its data and its TOML text."""
    name: str
    display_name: str
    choices: dict
    data: dict
    text: str


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def _value(value) -> str:
    """One TOML value: text, a whole number, a decimal, a boolean, a list or an inline table."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cannot write the number %r as TOML" % (value,))
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join("%s = %s" % (_key(k), _value(v)) for k, v in value.items()) + " }"
    raise ValueError("cannot write %r as TOML" % (value,))


def _assignments(table: dict, skip: tuple = ()) -> list:
    return ["%s = %s" % (_key(k), _value(v)) for k, v in table.items() if k not in skip]


def _write(data: dict, source: str) -> str:
    """The TOML text of an expanded theory, in the order of the form."""
    lines = ["# made by research_lab.theories.expand from %s; edit the template" % source, "", "[theory]"]
    lines += _assignments(data["theory"])
    for section in _SECTIONS:
        if section not in data:
            continue
        table = data[section]
        if section in ("operand", "parameter_picks"):
            for name, sub in table.items():
                lines += ["", "[%s.%s]" % (section, _key(name))] + _assignments(sub)
            continue
        has_groups = section in ("entry", "exit")
        head = _assignments(table, skip=("group",) if has_groups else ())
        if head:
            lines += ["", "[%s]" % section] + head
        for group in table.get("group", ()) if has_groups else ():
            lines += ["", "[[%s.group]]" % section]
            for k, v in group.items():
                if k == "conditions":
                    lines.append("conditions = [")
                    lines += ["  %s," % _value(cond) for cond in v]
                    lines.append("]")
                else:
                    lines.append("%s = %s" % (_key(k), _value(v)))
    return "\n".join(lines) + "\n"


def _letters_used(data: dict) -> set:
    """The strings of an expanded theory that stand where a parameter pick letter may stand."""
    used = set()

    def add(value):
        if isinstance(value, str):
            used.add(value)

    for op in data.get("operand", {}).values():
        for key in ("bar", "offset", "span", "value"):
            add(op.get(key))
        for value in op.get("params", {}).values():
            add(value)
    for which in ("entry", "exit"):
        table = data.get(which, {})
        for group in table.get("group", ()):
            for cond in group.get("conditions", ()):
                add(cond.get("left"))
                add(cond.get("right"))
    for key in ("stop", "target", "max_hold"):
        add(data.get("exit", {}).get(key))
    return used


def _combination(template: form.Template, slot_choices: dict, source: str) -> Expanded:
    base = template.data
    head = base["theory"]
    name = "__".join([head["name"]] + list(slot_choices.values()))
    display = ", ".join([head["display_name"]] + [template.choices[c]["words"] for c in slot_choices.values()])
    if len(display) > form.DISPLAY_NAME_MAX:
        raise form.TheoryFormError(
            "bad_template", "theory.display_name",
            "combination %s: its display name '%s' is %d characters; the most is %d" % (name, display, len(display), form.DISPLAY_NAME_MAX))
    data = {"theory": {"name": name, "display_name": display, "template": head["name"], "choices": dict(slot_choices)}}
    data["theory"].update({k: copy.deepcopy(v) for k, v in head.items() if k not in _THEORY_DROPPED})
    for section in _SECTIONS:
        if section in base and section not in ("parameter_picks",):
            data[section] = copy.deepcopy(base[section])
    operands = data.setdefault("operand", {})
    for choice in slot_choices.values():
        for operand, table in template.choices[choice]["operand"].items():
            operands[operand] = copy.deepcopy(table)
    if "parameter_picks" in base:
        used = _letters_used(data)
        data["parameter_picks"] = {k: copy.deepcopy(v) for k, v in base["parameter_picks"].items() if k in used}
    text = _write(data, source)
    if tomllib.loads(text) != data:
        raise form.TheoryFormError("bad_template", "theory", "combination %s: the text written from it does not read back as the same data" % name)
    return Expanded(name, display, dict(slot_choices), data, text)


def expand_template(template: form.Template, ranges_path=None) -> list:
    """Every combination of a checked template, each loaded with form.loads. A fault names its combination."""
    source = Path(template.path).name
    slots = list(template.slots)
    out = []
    for taken in itertools.product(*(template.slots[s] for s in slots)):
        expanded = _combination(template, dict(zip(slots, taken)), source)
        try:
            form.loads(expanded.text, expanded.name, ranges_path)
        except form.TheoryFormError as exc:
            raise form.TheoryFormError(exc.code, exc.part, "combination %s: %s" % (expanded.name, exc.detail)) from exc
        out.append(expanded)
    return out


def expand(template_path, ranges_path=None) -> list:
    """Read a template and return one Expanded per combination of its choices. Raises form.TheoryFormError."""
    return expand_template(form.read_template(template_path), ranges_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m research_lab.theories.expand", description="Write one theory file per combination of a template's choices.")
    parser.add_argument("template", help="the template file")
    parser.add_argument("--out", required=True, help="the folder to write <name>.toml files into; it must be empty or new")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print("the out folder is not empty: %s" % out, file=sys.stderr)
        return 2
    try:
        expanded = expand(args.template)
    except form.TheoryFormError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    for item in expanded:
        (out / (item.name + ".toml")).write_text(item.text, encoding="utf-8")
    for item in expanded:
        print("%s  %s" % (item.name, item.display_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
