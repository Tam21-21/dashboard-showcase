"""The five quality numbers of a strategy and the book's quality condition (lane GV3-S1, DECISIONS #467 item 4 and #470).

Dolzhenko, "Algorithmic Trading Systems and Strategies", pdfp 88 to 89 (docs/books/algorithmic_trading_systems_deep/part_B03.md):
after the first round a quality condition says whether the theory is suitable, so whether to go on narrowing. Figure 3-8 is her
example: the input is the pool of strategies, the scope "5% of best strategies", and a strategy of the pool must have
    amount of deals > 100, percentage of profitable trades > 60%, maxDrawdown < 2%, avg performance per month > 0.3%, recovery factor > 6.
The output is yes or no. The theory is suitable (yes) when a strategy of the pool meets every number.

The bars are Tamer's since DECISIONS #485 (2026-11-03): winning trades over 25% (the book's example says 60%), and the recovery factor
is shown and is not a bar. The other three bars (over 100 deals, drawdown under 2%, over 0.3% a month) are the book's Figure 3-8 example.

Plain words: five numbers describe how a strategy behaved on the screen window, and four of them are bars. A theory field or a spec key never changes them. `bars` on `meets`, `suitable` and the search is a test hook for the probes.
"""
from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
import pandas as pd

# Figure 3-8, pdfp 89 (the book's example condition), and #467 item 4.
MIN_DEALS = 100         # amount of deals > 100
MIN_WIN_SHARE = 0.25    # winning trades > 25% (Tamer, #485; the book's example says 60%)
MAX_DRAWDOWN = 0.02     # maxDrawdown < 2%
MIN_MONTH = 0.003       # avg performance per month > 0.3%
POOL_PERCENT = 5        # scope: 5% of best strategies

NAMES = ("closed", "win_share", "max_drawdown", "avg_month", "recovery")   # the five numbers, stored in the tables


class Metrics(NamedTuple):
    """The five numbers; recovery is shown, not a bar (#485). None is a number that does not exist (a share of no trades, a recovery with no fall)."""
    closed: int
    win_share: float | None
    max_drawdown: float | None
    avg_month: float | None
    recovery: float | None


class Bars(NamedTuple):
    """The four bars of the condition, in the same order and names as the first four of Metrics. A strategy must be past every one."""
    closed: float
    win_share: float
    max_drawdown: float
    avg_month: float


BOOK = Bars(MIN_DEALS, MIN_WIN_SHARE, MAX_DRAWDOWN, MIN_MONTH)
BARRED = Bars._fields   # the numbers that are bars; recovery is not one


def _finite(value) -> float | None:
    return float(value) if value is not None and math.isfinite(value) else None


def metrics(res, init_cash: float) -> Metrics:
    """The five numbers of one strategy's GroupResult on its window.

    closed         the closed trades, res.num_trades (an open trade at the end is not a deal)
    win_share      closed trades with pnl above 0, over closed; None when none closed
    max_drawdown   the largest fall of equity from its running high, as a fraction: -res.max_drawdown (0.0 when equity never fell)
    avg_month      the mean over the calendar months of the window of month-end equity / the previous month-end equity - 1,
                   the first month against init_cash (Dolzhenko pdfp 86: month profit = (end - open) / open balance)
    recovery       |last equity - init_cash| / the largest fall of equity from its running high, in money (pdfp 20); None when
                   equity never fell

    Hand values (the probe s15): init cash 100, equity of 12 bars in three months ending 102, 101 and 105, running high 103 and
    lowest point 98 after it, closed pnl +3, -1, +2, +1 (and one open trade): closed 4, win_share 0.75,
    max_drawdown 5 / 103 = 0.04854368932038835, avg_month (0.02 + (101 / 102 - 1) + (105 / 101 - 1)) / 3 = 0.01660001294247074,
    recovery |105 - 100| / 5 = 1.0.
    """
    closed = int(res.num_trades)
    trades = res.trades
    if closed == 0:
        win_share = None
    else:
        done = trades[trades["status"].str.lower() == "closed"]
        win_share = float((done["pnl"].astype(float) > 0).sum()) / closed
    equity = res.equity.to_numpy(dtype=float)
    month_end = pd.Series(equity).groupby([res.equity.index.year, res.equity.index.month]).last().to_numpy(dtype=float)
    before = np.concatenate([[float(init_cash)], month_end[:-1]])
    avg_month = _finite(float(np.mean(month_end / before - 1.0)))
    fall = float(np.max(np.maximum.accumulate(equity) - equity))
    recovery = abs(float(equity[-1]) - float(init_cash)) / fall if fall > 0 else None
    return Metrics(closed, win_share, _finite(0.0 - float(res.max_drawdown)), avg_month, _finite(recovery))


def meets(m: Metrics, bars: Bars = BOOK) -> tuple:
    """(True, ()) when every barred number is strictly past its bar, else (False, the names that failed, in the book's order).

    Past means above, except max_drawdown, which must be below. A number that is None fails. Recovery is shown, not a bar:
    it is never checked, so a recovery of None, 0 or 1 never fails.
    """
    failed = []
    for name in BARRED:
        value, bar = getattr(m, name), getattr(bars, name)
        past = value is not None and (value < bar if name == "max_drawdown" else value > bar)
        if not past:
            failed.append(name)
    return (not failed, tuple(failed))


def _row_metrics(row) -> Metrics:
    def number(name):
        value = row[name]
        return None if value is None or pd.isna(value) else float(value)

    return Metrics(int(row["closed"]), number("win_share"), number("max_drawdown"), number("avg_month"), number("recovery"))


def suitable(table: pd.DataFrame, bars: Bars = BOOK) -> dict:
    """The quality condition on a round's trials: `table` has sharpe, num_trades and the five, one row per trial, the trial id as index.

    The pool is the best POOL_PERCENT of the rows clearing the 30-trade bar (search.best_percent). The answer is "yes" when a
    pool row meets every bar, with the ids of those rows; else "no", with the closest pool row: the one meeting most bars, ties
    to the higher Sharpe, then the lower id. An empty pool is "no" with no closest row.
    """
    from research_lab.theory_search import search  # search.py imports this module, so the import is made when used

    pool = search.best_percent(table, percent=POOL_PERCENT)
    answers = []
    for trial_id, row in pool.iterrows():
        m = _row_metrics(row)
        ok, failed = meets(m, bars)
        answers.append((int(trial_id), float(row["sharpe"]), ok, failed, m))
    ids = [a[0] for a in answers if a[2]]
    out = {"answer": "yes" if ids else "no", "ids": ids, "pool": len(answers), "closest": None}
    if not ids and answers:
        best = sorted(answers, key=lambda a: (len(a[3]), -a[1], a[0]))[0]
        out["closest"] = {"trial_id": best[0], "sharpe": best[1], "met": len(BARRED) - len(best[3]), "failed": list(best[3]), "metrics": best[4]._asdict()}
    return out


def select(trials: list, keep: int, bars: Bars = BOOK) -> list:
    """The kept strategies (lane GV3-S2, DECISIONS #467 items 5 and 6, #470).

    Dolzhenko pdfp 93: "followed a list of strategies sorted in descending order ... if any of them fulfilled the suitability condition
    ... until either the required number of strategies is collected or the pool ... is exhausted".
    `trials` are the search.Trial objects of every round on the screen window. They are walked best Sharpe first, ties to the lower
    trial id; a trial with no Sharpe (NaN) is never ranked, so never kept. Each one meeting every bar of `meets` is kept, until `keep`
    are kept or the list is used up. `bars` is the test hook of this module.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a whole number of at least 1, got %r" % (keep,))
    ranked = sorted((t for t in trials if not math.isnan(t.sharpe)), key=lambda t: (-t.sharpe, t.trial_id))
    kept = []
    for t in ranked:
        if meets(Metrics(t.closed, t.win_share, t.max_drawdown, t.avg_month, t.recovery), bars)[0]:
            kept.append(t)
            if len(kept) == keep:
                break
    return kept
