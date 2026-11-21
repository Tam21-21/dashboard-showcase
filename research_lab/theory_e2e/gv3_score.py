"""Lane GV3-R1 (DECISIONS #467 items 9 and 11, #489 item 21): puts each theory's batch result beside its answer sheet verdict.

Pure functions on the three files of a made-up (SYNTHETIC, data version 2) batch folder: gv3_result.json (gv3_batch.py), theory_summary.json
(synthetic/answer.py) and manifest.json. A row shows both sides and decides nothing: no field says pass or fail for the run.
Imports only the standard library and theory_search.quality (for the book's bars).
"""
from __future__ import annotations

from research_lab.theory_search import quality

_LOW_LINES = {"bollinger_lower": "bollinger", "donchian_low": "donchian"}
_OSCILLATORS = ("rsi", "stochastic")
_AVERAGES = ("sma", "ema")
_SHEET_KEYS = ("verdict", "trades", "win_rate", "mean_return", "six_fewest_trades")
_SIDES = ("kept_back", "later_years")


def sheet_theory(choices: dict) -> str:
    """The answer sheet's theory name for a tree theory's choices."""
    if isinstance(choices, dict):
        if set(choices) == {"low_line", "oscillator"} and choices["low_line"] in _LOW_LINES and choices["oscillator"] in _OSCILLATORS:
            return _LOW_LINES[choices["low_line"]] + "_" + choices["oscillator"]
        if set(choices) == {"average"} and choices["average"] in _AVERAGES:
            return choices["average"] + "_cross"
    raise ValueError("no answer sheet for the choices %r" % (choices,))


def sheet_group(manifest: dict, group: str) -> str:
    """The sheet's group word (volatile, twin, ...) of the made-up stocks whose code starts with `group` + "_"."""
    words = {entry.get("group") for code, entry in manifest["codes"].items() if code.startswith(group + "_")}
    if not words:
        raise ValueError("the manifest has no made-up stocks for group %s." % group)
    if len(words) > 1:
        raise ValueError("the manifest gives group %s more than one sheet group: %s." % (group, sorted(map(str, words))))
    return words.pop()


def _refuse(sentence: str):
    raise ValueError(sentence)


def _check_inputs(result: dict, summary: dict):
    if result.get("data_version") != 2:
        _refuse("the result is not on data version 2 (it says %r)." % (result.get("data_version"),))
    if result.get("synthetic") is not True:
        _refuse("the result is not marked synthetic.")
    tree = result.get("tree")
    if not isinstance(tree, dict) or not isinstance(tree.get("theories"), list):
        _refuse("the result has no tree with a list of theories.")
    states = result.get("states")
    if not isinstance(states, list) or len(states) != len(tree["theories"]):
        _refuse("the result's states and the tree's theories are not the same length.")
    for i, theory in enumerate(tree["theories"]):
        if states[i].get("theory") != theory.get("display_name"):
            _refuse("state %d names %r but the tree's theory is %r." % (i, states[i].get("theory"), theory.get("display_name")))
    if not isinstance(summary.get("theories"), dict):
        _refuse("the summary has no theories.")


def _failed_entries(side: dict) -> list:
    five = side.get("five")
    out = []
    for name in side["failed"]:
        if not isinstance(five, dict) or name not in five:
            _refuse("a side failed %r but its five numbers do not hold it." % (name,))
        out.append({"name": name, "value": five[name], "bar": getattr(quality.BOOK, name)})
    return out


def _why_not(strategies: list) -> list:
    entries = []
    for strategy in strategies:
        if strategy["held_up"]:
            continue
        rank = strategy["rank"]
        if "reason" in strategy:
            entries.append({"rank": rank, "side": None, "failed": [], "error": None, "reason": strategy["reason"]})
            continue
        for side_name in _SIDES:
            side = strategy[side_name]
            if side.get("meets") is not False:
                continue
            if "error" in side:
                entries.append({"rank": rank, "side": side_name, "failed": [], "error": side["error"], "reason": None})
            else:
                entries.append({"rank": rank, "side": side_name, "failed": _failed_entries(side), "error": None, "reason": None})
    return entries


def _check_strategies(theory: dict) -> list:
    final = theory.get("final")
    if final == "not run":
        return []
    if not isinstance(final, dict) or not isinstance(final.get("strategies"), list):
        _refuse("the final of %r is neither \"not run\" nor a dict with a strategies list." % (theory.get("display_name"),))
    for strategy in final["strategies"]:
        if not isinstance(strategy, dict) or "rank" not in strategy or "held_up" not in strategy:
            _refuse("a strategy of %r has no rank or held_up." % (theory.get("display_name"),))
        if "reason" not in strategy and not all(isinstance(strategy.get(s), dict) for s in _SIDES):
            _refuse("a strategy of %r has no reason and not both sides as dicts." % (theory.get("display_name"),))
    return final["strategies"]


def score(result: dict, summary: dict, manifest: dict) -> dict:
    """One row per theory, in the tree's order: the sheet's numbers beside what the run did. Decides nothing."""
    _check_inputs(result, summary)
    group = result["group"]
    sheet_word = sheet_group(manifest, group)
    rows = []
    for theory, state in zip(result["tree"]["theories"], result["states"]):
        name = sheet_theory(theory.get("choices"))
        if name not in summary["theories"]:
            _refuse("the summary has no theory %s." % name)
        try:
            search = summary["theories"][name][sheet_word]["search"]
        except KeyError:
            _refuse("the summary has no search entry for %s on %s." % (name, sheet_word))
        missing = [k for k in _SHEET_KEYS if k not in search]
        if missing:
            _refuse("the summary's search entry for %s lacks %s." % (name, ", ".join(missing)))
        strategies = _check_strategies(theory)
        stop = theory.get("stop")
        saved = theory.get("saved") or []
        rows.append({
            "theory": name,
            "display_name": theory["display_name"],
            "sheet": {k: search[k] for k in _SHEET_KEYS},
            "run": {
                "state": state["state"],
                "stop": None if stop is None else {"reason": stop["reason"]},
                "kept": len(strategies),
                "held_up": sum(1 for s in strategies if s["held_up"]),
                "why_not": _why_not(strategies),
                "judge": [{"rank": s["rank"], "word": (s["verdict"] or {}).get("word"), "refusal": s["refusal"]}
                          for s in sorted(saved, key=lambda s: s["rank"])],
            },
        })
    template = result["tree"].get("template")
    if not isinstance(template, dict) or "display_name" not in template:
        _refuse("the tree has no template display name.")
    return {"synthetic": True, "data_version": result["data_version"], "group": group, "sheet_group": sheet_word,
            "template": template["display_name"], "seed": result.get("seed"), "seconds_total": result.get("seconds_total"),
            "spans": summary.get("spans"), "rows": rows}
