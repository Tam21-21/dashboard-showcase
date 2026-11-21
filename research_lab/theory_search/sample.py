"""The balanced sample of step 1 (lane GV2-5b, DECISIONS #440: a balanced sample on the screen window).

Plain words: a theory's parameter picks make a grid of strategies, too many to run all of them. A balanced sample draws
`size` strategies so that each value of each letter is used the same number of times (give or take one), and no strategy
is drawn twice. That way no part of a letter's range is left out by chance, and the later narrowing sees all of it.

How (plan step 2): per letter, its values are repeated to `size` rows and shuffled with numpy default_rng((seed, letter
index)); the columns are zipped into rows. A row that repeats an earlier row, or breaks one of the indicators' logic rules
(a MACD's fast window below its slow window), is fixed by swapping that letter's values between the bad row and another
row. A swap keeps every column's value counts, so the balance survives the fixing. A size at or above the number of valid
strategies gives the whole grid, in form order.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import numpy as np

from research_lab.theories import grid

SWAP_BUDGET_PER_ROW = 5000  # a size close to the grid can need many swaps to find the last free cells; past this it is refused, never looped


def number(value, int_valued: bool):
    """A pick value as a plain number: a whole number for an integer letter, a float otherwise."""
    return int(value) if int_valued else float(value)


def letters_and_rules(theory) -> tuple:
    """(letters in form order, logic rules as (smaller term, larger term) pairs) of the theory's base strategy.

    The base strategy is the combination that keeps every operand as written; variations are not searched here, as in
    theory_run (GV2-5a).
    """
    base = grid.combinations(theory)[0]
    used = grid._used_operands(theory, dict(base.choices))
    return list(base.variables), grid._constraints(theory, used)


def row_is_valid(row: dict, rules: list) -> bool:
    """True when the row breaks no logic rule (a rule says its smaller term must be below its larger term)."""
    for small, large in rules:
        a = row[small] if isinstance(small, str) else small
        b = row[large] if isinstance(large, str) else large
        if not a < b:
            return False
    return True


def axis_values(theory, letter: str) -> tuple:
    pick = theory.picks[letter]
    if pick.listed is not None:
        raise ValueError("letter %r takes listed values %r; the search draws numbers only" % (letter, pick.listed))
    return tuple(number(v, pick.int_valued) for v in pick.values)


def grid_rows(letters: list, axes: dict, rules: list) -> list:
    """Every valid strategy of the grid, in form order (the first letter changes slowest)."""
    rows = []
    for combo in itertools.product(*(axes[l] for l in letters)):
        row = dict(zip(letters, combo))
        if row_is_valid(row, rules):
            rows.append(row)
    return rows


def grid_count(letters: list, axes: dict, rules: list) -> int:
    """The number of valid strategies over the given value lists, counted without listing them (grid._count)."""
    shim = SimpleNamespace(picks={l: SimpleNamespace(values=axes[l]) for l in letters})
    return grid._count(shim, letters, rules)


def sample_axes(letters: list, axes: dict, rules: list, size: int, seed: int, salt: tuple = ()) -> list:
    """`size` distinct valid strategies over the given value lists, each value used floor or ceil of size / count times."""
    if isinstance(size, bool) or int(size) != size or size < 1:
        raise ValueError("size must be a whole number of at least 1, got %r" % (size,))
    size = int(size)
    valid = grid_count(letters, axes, rules)
    if valid == 0:
        raise ValueError("the grid holds no valid strategy")
    if size >= valid:
        return grid_rows(letters, axes, rules)
    if 2 * size > valid:
        # Close to the whole grid, finding the last free cells by swaps takes too long: draw the strategies to leave out instead
        # (a balanced sample of the few, so each value is left out floor or ceil of the same share) and shuffle the rest.
        left_out = sample_axes(letters, axes, rules, valid - size, seed, salt + (999,))
        gone = {tuple(row[l] for l in letters) for row in left_out}
        kept = [row for row in grid_rows(letters, axes, rules) if tuple(row[l] for l in letters) not in gone]
        order = np.random.default_rng((seed,) + tuple(salt) + (1000,)).permutation(len(kept))
        return [kept[int(i)] for i in order]

    columns = []
    for index, letter in enumerate(letters):
        values = axes[letter]
        indexes = np.resize(np.arange(len(values)), size)
        np.random.default_rng((seed,) + tuple(salt) + (index,)).shuffle(indexes)
        columns.append(indexes)
    swaps = np.random.default_rng((seed,) + tuple(salt) + (len(letters),))

    def key_of(i):
        return tuple(int(columns[c][i]) for c in range(len(letters)))

    def valid_row(i):
        return row_is_valid({l: axes[l][int(columns[c][i])] for c, l in enumerate(letters)}, rules)

    counts: dict = {}
    for i in range(size):
        k = key_of(i)
        counts[k] = counts.get(k, 0) + 1
    bad_rows = [i for i in range(size) if counts[key_of(i)] > 1 or not valid_row(i)]
    budget = SWAP_BUDGET_PER_ROW * size
    spent = 0
    # A row is bad when another row has its key or when it breaks a rule. A swap is kept when it does not make the two rows worse.
    while bad_rows:
        still = []
        for i in bad_rows:
            if counts[key_of(i)] <= 1 and valid_row(i):
                continue
            while True:
                spent += 1
                if spent > budget:
                    raise ValueError("balanced_sample: size %d of a grid of %d valid strategies could not be made distinct and valid "
                                     "within %d swaps; ask for a smaller size" % (size, valid, budget))
                c = int(swaps.integers(len(letters)))
                j = int(swaps.integers(size))
                if j == i or columns[c][i] == columns[c][j]:
                    continue
                before = (counts[key_of(i)] > 1) + (counts[key_of(j)] > 1) + (not valid_row(i)) + (not valid_row(j))
                ki, kj = key_of(i), key_of(j)
                columns[c][i], columns[c][j] = columns[c][j], columns[c][i]
                counts[ki] -= 1
                counts[kj] -= 1
                ni, nj = key_of(i), key_of(j)
                counts[ni] = counts.get(ni, 0) + 1
                counts[nj] = counts.get(nj, 0) + 1
                after = (counts[ni] > 1) + (counts[nj] > 1) + (not valid_row(i)) + (not valid_row(j))
                if after < before or (after == before and counts[ni] <= 1 and valid_row(i)):
                    break
                counts[ni] -= 1
                counts[nj] -= 1
                columns[c][i], columns[c][j] = columns[c][j], columns[c][i]
                counts[ki] += 1
                counts[kj] += 1
            if counts[key_of(i)] > 1 or not valid_row(i):
                still.append(i)
        bad_rows = [i for i in range(size) if counts[key_of(i)] > 1 or not valid_row(i)]
    return [{l: axes[l][int(columns[c][i])] for c, l in enumerate(letters)} for i in range(size)]


def balanced_sample(theory, size: int, seed: int) -> list:
    """The step 1 sample: `size` strategies of the theory as {letter: number}, in drawn order."""
    letters, rules = letters_and_rules(theory)
    axes = {l: axis_values(theory, l) for l in letters}
    return sample_axes(letters, axes, rules, size, seed)
