"""The kept-back third of a stock group: the stocks a theory's search never sees (GV3-W1, DECISIONS #467).

One function, stdlib and numpy only, so the generator store (dashboard/backend/gendb.py) and the final test (lane GV3-F) draw
the same stocks from the same seed.
"""
from __future__ import annotations

import numpy as np

MINIMUM_MEMBERS = 3  # below three a third is zero stocks, and nothing would be kept back


def draw_kept_back(members, seed: int) -> list:
    """The stocks to keep back from a group: len(members) // 3 of them, drawn with the seed, returned sorted.

    The members are sorted first, so the same group in any order and the same seed always give the same stocks.
    Raises ValueError for a group of fewer than 3 stocks, or one that lists a stock twice.
    """
    ordered = sorted(members)
    if len(ordered) < MINIMUM_MEMBERS:
        raise ValueError("a group needs at least %d stocks to keep a third back, got %d" % (MINIMUM_MEMBERS, len(ordered)))
    if len(set(ordered)) != len(ordered):
        raise ValueError("a group lists a stock twice: %s" % ", ".join(sorted({m for m in ordered if ordered.count(m) > 1})))
    picked = np.random.default_rng(seed).choice(len(ordered), size=len(ordered) // 3, replace=False)
    return sorted(ordered[int(i)] for i in picked)
