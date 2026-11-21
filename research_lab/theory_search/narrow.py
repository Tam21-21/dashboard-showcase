"""Step 2 of the search: narrowing each letter's interval around the best strategies (lane GV2-5b, DECISIONS #440).

Dolzhenko, "Algorithmic Trading Systems and Strategies", pdfp 91 to 93 (docs/books/algorithmic_trading_systems_deep/part_B03.md,
"Second step in detail"): the values of the five best strategies show where the best region is. The new interval runs from
the lowest of them to the highest, widened by one old step each way (the optimum may sit just outside, "since this is not the
final solution, we will add one step in each direction"). The new step is the number of values the interval would hold at the
parameter's own finest step, divided by the user's limit (20 in the book's example), rounded up, times that finest step.

Table 3-3 worked through: best 91, 16, 196, 166, 16; range 1 to 300; old step 15; finest step 1 gives 1 to 211 and a step of
ceil(211 / 20) * 1 = 11.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from research_lab.theory_search import sample


@dataclass(frozen=True)
class Axis:
    """One letter's interval after narrowing: lo, hi, step and the values lo, lo + step, ... up to hi."""
    lo: object
    hi: object
    step: object
    values: tuple


def _decimal(value) -> Decimal:
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def first_axis(lo, hi, finest, limit: int) -> tuple:
    """Returns (lo, hi, step) for round 1 (lane GV3-S1, DECISIONS #470): narrow's own step rule on the whole range.

    The step is the number of values the range would hold at the finest step, divided by the limit, rounded up, times the
    finest step. Table 3-2: 1 to 300 by 1 with a limit of 20 gives ceil(300 / 20) = 15. A range already within the limit keeps
    its finest step. The ends are Decimals.
    """
    lo, hi, finest = _decimal(lo), _decimal(hi), _decimal(finest)
    if finest <= 0:
        raise ValueError("the finest step must be above 0, got %r" % (finest,))
    if lo > hi:
        raise ValueError("the range runs from %r to %r: from is above to" % (lo, hi))
    if isinstance(limit, bool) or limit != int(limit) or limit < 1:
        raise ValueError("limit must be a whole number of at least 1, got %r" % (limit,))
    count = (hi - lo) / finest + 1
    return lo, hi, math.ceil(count / int(limit)) * finest


def narrow(best_values, lo, hi, old_step, min_step, limit: int = 20) -> tuple:
    """Returns (lo', hi', step'): the new interval and step, from the best strategies' values of one letter."""
    best = list(best_values)
    if not best:
        raise ValueError("narrow needs at least one best value")
    if min_step <= 0 or old_step <= 0:
        raise ValueError("steps must be above 0, got old step %r and finest step %r" % (old_step, min_step))
    if limit < 1:
        raise ValueError("limit must be at least 1, got %r" % (limit,))
    new_lo = max(lo, min(best) - old_step)
    new_hi = min(hi, max(best) + old_step)
    if new_lo > new_hi:
        raise ValueError("the best values %r lie outside the interval %r to %r" % (best, lo, hi))
    count = (new_hi - new_lo) / min_step + 1
    return new_lo, new_hi, math.ceil(count / limit) * min_step


def _values(lo, hi, step, int_valued: bool) -> tuple:
    out, v = [], lo
    while v <= hi:
        out.append(sample.number(v, int_valued))
        v = v + step
    return tuple(out)


def first_box(theory, limit: int) -> dict:
    """Round 1's box: {letter: Axis} over each pick's own range (its from and to), at the step `first_axis` gives.

    A letter with listed (not numeric) values is refused by name, as in step2_box.
    """
    letters, _rules = sample.letters_and_rules(theory)
    box = {}
    for letter in letters:
        sample.axis_values(theory, letter)  # refuses a listed letter by name
        pick = theory.picks[letter]
        lo, hi, step = first_axis(pick.lo, pick.hi, pick.step, limit)
        box[letter] = Axis(lo, hi, step, _values(lo, hi, step, pick.int_valued))
    return box


def step2_box(theory, best_five: list, previous: dict | None = None, limit: int = 20) -> dict:
    """The box of the next round: {letter: Axis}. `best_five` are {letter: number} sets.

    The first round narrows each letter's range as the theory gives it (its own step is the old step and the finest step).
    A later round passes the box before it as `previous`: it narrows inside that box, with that box's step as the old step.
    `limit` is the most values a letter's new interval is given (narrow's rule); the search passes the theory's values_per_number.
    A letter with listed (not numeric) values is refused: narrowing an interval needs numbers.
    """
    letters, _rules = sample.letters_and_rules(theory)
    box = {}
    for letter in letters:
        sample.axis_values(theory, letter)  # refuses a listed letter by name
        pick = theory.picks[letter]
        best = [Decimal(str(row[letter])) for row in best_five]
        if previous is None:
            lo, hi, old = pick.lo, pick.hi, pick.step
        else:
            lo, hi, old = (Decimal(str(previous[letter].lo)), Decimal(str(previous[letter].hi)), Decimal(str(previous[letter].step)))
        new_lo, new_hi, new_step = narrow(best, lo, hi, old, pick.step, limit)
        box[letter] = Axis(new_lo, new_hi, new_step, _values(new_lo, new_hi, new_step, pick.int_valued))
    return box


def box_size(box: dict) -> int:
    """How many strategies the box holds before any logic rule drops some: the product of its letters' value counts."""
    size = 1
    for axis in box.values():
        size *= len(axis.values)
    return size
