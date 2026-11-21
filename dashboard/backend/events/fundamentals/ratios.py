"""Named fundamental ratios, computed backend-side on read (ANA-5 §5.1; lane LK1c, DECISIONS #493).

Four ratios, each following Damodaran's text (Investment Valuation, ch. 3 "Measuring Profitability" and
"Debt Ratios"; the tax rate from ch. 10 "Effective versus Marginal Tax rate"):

  ROE               = net_income / ((shareholders_equity previous year + shareholders_equity) / 2)
  ROA               = operating_income x (1 - income_tax / pretax_income)
                      / ((total_assets previous year + total_assets) / 2), the three flows from one period end
  net_profit_margin = net_income / revenue
  debt_to_equity    = (short-term debt + long-term debt) / shareholders_equity, the debt counted by _debt below

The label, definition and formula text come from definitions.py RATIO_DEFINITIONS, the single source, so the
formula shown on hover and the one computed here cannot drift apart.

`compute_ratios` takes the per-concept as-of FactValues for the year and, separately, the year-before
balance-sheet FactValues (``prior_by_concept``). Each ratio reads its own year-before figure, so one missing
figure affects only its ratio. The divide-safe rule MATCHES the backtest (DECISIONS #97): only
ZeroDivisionError is caught (a reported-zero denominator is a real value, genuinely undefined, so honest-missing),
the result must be finite, and a missing figure is honest-missing. Any other exception (a typo in a ratio
spec) fails loud. This is a small PARALLEL implementation of that rule, deliberately not an import of the
strategies-package `combine` (the #95 boundary).

Debt to equity (lane LK1e, Tamer 2026-11-16 "Accounting tree"): which figure holds what depends on the XBRL tag
stored with it, so the debt is built by _debt, not by a plain sum. The FASB balance-sheet tree: DebtCurrent =
ShortTermBorrowings + LongTermDebtCurrent; LongTermDebtNoncurrent leaves out the part due within a year,
LongTermDebt holds it. Its legs are all five figures, each carrying its own date, so the page can show every input of
the debt; the generic missing-leg rule does not apply to the three short-term ones (a missing one counts as zero).

Every leg rides along as backend-owned data: concept, value, filing_date, period_end and "year" ("current" or
"previous"), so the page can show which period each figure came from.
"""
from __future__ import annotations

import math                                                        # math.isfinite, the finite-result guard (matches #97)
from dataclasses import dataclass

from .concepts import concept_by_name                             # the vocabulary actually read from the store
from .definitions import FIGURE_DEFINITIONS, RATIO_DEFINITIONS     # the single source for figure + ratio human content
from .reads import FactValue                                       # the per-concept as-of value the read produced

CURRENT = "current"                                                # a leg read from the year itself
PREVIOUS = "previous"                                              # a leg read from the year before


def _figure_label(concept: str) -> str:
    """The human label for a figure concept (KeyError if undefined, which the import-time check below prevents)."""
    return FIGURE_DEFINITIONS[concept]["label"]


@dataclass(frozen=True)
class RatioSpec:
    """One ratio: its name, its input legs (concept, year), and its human content (from RATIO_DEFINITIONS).

    ``legs`` lists every input as (concept, "current" | "previous"). ``positive`` names the legs (by index)
    that must be above zero for the ratio to be defined (Damodaran: a ratio over a zero or negative equity,
    assets, revenue or pretax income is not meaningful). ``same_period`` names concepts whose current legs
    must come from one period end.
    """

    name: str
    legs: tuple[tuple[str, str], ...]
    positive: tuple[int, ...]
    same_period: tuple[str, ...]
    label: str
    definition: str
    formula: str


def _spec(name: str, legs, positive=(), same_period=()) -> RatioSpec:
    """Build a RatioSpec, pulling the human content from RATIO_DEFINITIONS (KeyError at import if absent)."""
    d = RATIO_DEFINITIONS[name]
    return RatioSpec(name=name, legs=tuple(legs), positive=tuple(positive), same_period=tuple(same_period),
                     label=d["label"], definition=d["definition"], formula=d["formula"])


RATIO_SPECS: tuple[RatioSpec, ...] = (
    _spec("ROE", [("net_income", CURRENT), ("shareholders_equity", PREVIOUS), ("shareholders_equity", CURRENT)],
          positive=(1, 2)),
    _spec("ROA", [("operating_income", CURRENT), ("income_tax", CURRENT), ("pretax_income", CURRENT),
                  ("total_assets", PREVIOUS), ("total_assets", CURRENT)],
          positive=(2, 3, 4), same_period=("operating_income", "income_tax", "pretax_income")),
    _spec("net_profit_margin", [("net_income", CURRENT), ("revenue", CURRENT)], positive=(1,)),
    _spec("debt_to_equity", [("short_term_debt", CURRENT), ("short_term_borrowings", CURRENT),
                             ("current_long_term_debt", CURRENT), ("long_term_debt", CURRENT),
                             ("shareholders_equity", CURRENT)], positive=(4,)),
)

# Fail loud AT IMPORT if a leg names a figure with no definition or a concept the store never reads, so a typo is
# caught the moment the module loads, never as a raw snake_case name in a serve-time reason.
_CONCEPT_NAMES = frozenset(concept_by_name())
for _s in RATIO_SPECS:
    for _concept, _year in _s.legs:
        if _concept not in FIGURE_DEFINITIONS:
            raise ValueError(
                f"ratio {_s.name!r} leg {_concept!r} has no entry in FIGURE_DEFINITIONS; "
                f"every ratio leg must be a defined figure (add it to definitions.py)")
        if _concept not in _CONCEPT_NAMES:
            raise ValueError(
                f"ratio {_s.name!r} leg {_concept!r} is not a concept in FUNDAMENTAL_CONCEPTS; "
                f"every ratio leg must be a read concept (add a ConceptSpec to concepts.py)")
        if _year not in (CURRENT, PREVIOUS):
            raise ValueError(f"ratio {_s.name!r} leg {_concept!r} has year {_year!r}; expected current or previous")


def _leg(concept: str, year: str, fv: FactValue | None) -> dict:
    """One ratio leg as backend-owned data: concept, value, both dates (or nulls) and which year it is."""
    if fv is None:
        return {"concept": concept, "value": None, "filing_date": None, "period_end": None, "year": year}
    return {"concept": concept, "value": fv.value, "filing_date": fv.filing_date, "period_end": fv.period_end,
            "year": year}


def _value(name: str, values: list[float]) -> float:
    """The ratio's number from its leg values (in leg order). May raise ZeroDivisionError (caught by the caller)."""
    if name == "ROE":
        net_income, equity_prev, equity = values
        return net_income / ((equity_prev + equity) / 2)
    if name == "ROA":
        operating_income, income_tax, pretax_income, assets_prev, assets = values
        return operating_income * (1 - income_tax / pretax_income) / ((assets_prev + assets) / 2)
    if name == "net_profit_margin":
        net_income, revenue = values
        return net_income / revenue
    raise ValueError(f"no formula for ratio {name!r}")


_DEBT_CURRENT = "DebtCurrent"                                      # S holds the whole short-term total
_LTD_CURRENT = "LongTermDebtCurrent"                               # S holds only the part due within a year
_LTD_NONCURRENT = "LongTermDebtNoncurrent"                         # L leaves out the part due within a year
_LTD_ALL = "LongTermDebt"                                          # L holds the part due within a year too
_SPLIT_REASON = ("Long-term debt includes the part due within a year, which the company does not report on its "
                 "own, so it cannot be split from short-term debt.")


def _check_tag(concept: str, fv: FactValue, allowed: tuple[str, ...]) -> None:
    """Fail loud when a debt figure carries a tag the rule does not know (a typo or a new tag), naming both."""
    if fv.xbrl_tag not in allowed:
        shown = f"tag {fv.xbrl_tag!r}" if fv.xbrl_tag else "no tag"
        raise ValueError(f"{concept} carries {shown}; the debt to equity rule reads only {', '.join(allowed)}")


def _debt(short: FactValue | None, borrowings: FactValue | None, due_within_year: FactValue | None,
          long: FactValue | None, equity: FactValue) -> tuple[float | None, str | None]:
    """The borrowed money behind debt to equity, as (debt, None), or (None, why it cannot be split).

    Short-term part: S tagged DebtCurrent is the whole total; S tagged LongTermDebtCurrent is only the part due
    within a year, so the short-term borrowings B are added; with no S it is B plus the long-term debt due within
    a year C. Long-term part: L tagged LongTermDebtNoncurrent as is; tagged LongTermDebt it holds the part due
    within a year, so S (when tagged LongTermDebtCurrent) or else C is taken off, and L stays as is when neither
    exists; S tagged DebtCurrent with no C cannot be split. B and C count only when dated at the equity's period
    end, otherwise they count as zero. S or L with another tag, or none, is a ValueError.
    """
    if short is not None:
        _check_tag("short_term_debt", short, (_DEBT_CURRENT, _LTD_CURRENT))
    if long is not None:
        _check_tag("long_term_debt", long, (_LTD_NONCURRENT, _LTD_ALL))
    b = borrowings.value if borrowings is not None and borrowings.period_end == equity.period_end else 0.0
    c = due_within_year.value if due_within_year is not None and due_within_year.period_end == equity.period_end else None
    if short is None:
        short_part = b + (c or 0.0)
    elif short.xbrl_tag == _DEBT_CURRENT:
        short_part = short.value
    else:
        short_part = short.value + b
    if long.xbrl_tag == _LTD_NONCURRENT:
        long_part = long.value
    elif short is not None and short.xbrl_tag == _LTD_CURRENT:
        long_part = long.value - short.value
    elif c is not None:
        long_part = long.value - c
    elif short is not None:                                        # S tagged DebtCurrent, no part due within a year
        return None, _SPLIT_REASON
    else:
        long_part = long.value
    return short_part + long_part, None


def _debt_to_equity(base: dict, facts_by_concept: dict[str, FactValue]) -> dict:
    """The debt to equity result dict (the generic missing-leg rule does not apply to the short-term figures)."""
    short = facts_by_concept.get("short_term_debt")
    borrowings = facts_by_concept.get("short_term_borrowings")
    due_within_year = facts_by_concept.get("current_long_term_debt")
    long = facts_by_concept.get("long_term_debt")
    equity = facts_by_concept.get("shareholders_equity")
    missing = []
    if short is None and borrowings is None and due_within_year is None:
        missing.append(_figure_label("short_term_debt"))
    if long is None:
        missing.append(_figure_label("long_term_debt"))
    if equity is None:
        missing.append(_figure_label("shareholders_equity"))
    if missing:
        return {**base, "available": False, "debt": None,
                "reason": f"{' and '.join(missing)} not reported as of this date."}
    debt, split_reason = _debt(short, borrowings, due_within_year, long, equity)
    if equity.value <= 0:
        return {**base, "available": False, "debt": debt,
                "reason": f"{_figure_label('shareholders_equity')} is at or below zero, so {base['label']} is not defined."}
    if debt is None:
        return {**base, "available": False, "debt": None, "reason": split_reason}
    result = debt / equity.value
    if not math.isfinite(result):
        return {**base, "available": False, "debt": debt, "reason": "The ratio is not a finite number."}
    return {**base, "available": True, "value": result, "debt": debt}


def compute_ratios(facts_by_concept: dict[str, FactValue],
                   prior_by_concept: dict[str, FactValue] | None = None) -> list[dict]:
    """Compute every ratio, divide-safe (the #97 rule), one dict per ratio in RATIO_SPECS order.

    ``facts_by_concept`` holds the year's FactValues; ``prior_by_concept`` the year-before balance-sheet ones
    (shareholders_equity, total_assets), or None. Each dict is {name, label, available, value | reason, legs}.
    Order of the reasons: a figure missing from the year; no year before; a figure at or below zero; flows from
    different years; a non-finite result. A ratio-spec typo (any other exception) propagates.
    """
    prior = prior_by_concept or {}
    out: list[dict] = []
    for spec in RATIO_SPECS:
        facts = [(prior if year == PREVIOUS else facts_by_concept).get(concept) for concept, year in spec.legs]
        legs = [_leg(concept, year, fv) for (concept, year), fv in zip(spec.legs, facts)]
        base = {"name": spec.name, "label": spec.label, "legs": legs}

        if spec.name == "debt_to_equity":                          # its debt depends on the stored tags (lane LK1e)
            out.append(_debt_to_equity(base, facts_by_concept))
            continue
        missing = [_figure_label(concept) for (concept, year), fv in zip(spec.legs, facts)
                   if year == CURRENT and fv is None]
        if missing:
            out.append({**base, "available": False,
                        "reason": f"{' and '.join(missing)} not reported as of this date."})
            continue
        if any(year == PREVIOUS and fv is None for (concept, year), fv in zip(spec.legs, facts)):
            out.append({**base, "available": False, "reason": "No balance sheet a year earlier, so no average."})
            continue
        low = next((spec.legs[i][0] for i in spec.positive if facts[i].value <= 0), None)
        if low is not None:
            out.append({**base, "available": False,
                        "reason": f"{_figure_label(low)} is at or below zero, so {spec.label} is not defined."})
            continue
        if spec.same_period:
            ends = {fv.period_end for (concept, year), fv in zip(spec.legs, facts) if concept in spec.same_period}
            if len(ends) > 1:
                out.append({**base, "available": False,
                            "reason": "The tax figures are from another year than operating income."})
                continue
        try:
            result = _value(spec.name, [fv.value for fv in facts])
        except ZeroDivisionError:                                  # a reported zero: undefined, not a crash (#97)
            out.append({**base, "available": False, "reason": f"{spec.label} divides by zero, so it is undefined."})
            continue
        if not math.isfinite(result):
            out.append({**base, "available": False, "reason": "The ratio is not a finite number."})
            continue
        out.append({**base, "available": True, "value": result})
    return out
