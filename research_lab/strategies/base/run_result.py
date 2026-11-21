"""The ``RunResult`` container: the locked output contract every strategy returns.

Six slots, fixed for all time (M3 spec L8, arch §5). The shape never changes
across ``Strategy`` / ``EventStrategy`` (M3.5) / ``MLStrategy`` (M10); future
milestones extend the data flowing through the slots, never the slot set.

Engine-neutral on purpose: every slot is plain pandas or plain Python, never a
vbt object. A strategy builds whatever vbt objects it needs inside ``run()`` and
hands back only project-owned types, so swapping the backtest engine later does
not touch this contract.
"""
# Lazy annotations: field types are stored as strings, so importing this module
# does NOT require pandas at runtime. Keeps the contract importable anywhere.
from __future__ import annotations

from dataclasses import dataclass  # the container is a plain frozen dataclass
from typing import TYPE_CHECKING  # guard the pandas import to type-checking only

if TYPE_CHECKING:  # only the type checker / reader needs the real pandas symbols
    import pandas as pd  # pd.Series / pd.DataFrame referenced in the annotations


@dataclass(frozen=True)  # frozen: a completed run result is immutable
class RunResult:
    """The complete, canonical result of one strategy run (all six slots filled)."""

    equity: "pd.Series"  # equity curve indexed by bar timestamp (account value in $)
    drawdowns: "pd.DataFrame"  # drawdown records, canonical columns (see vbt_columns)
    trades: "pd.DataFrame"  # trade records with MAE/MFE, canonical snake_case columns
    stats: dict[str, float | None]  # flat per-run metrics (keys match research_run_stats); nullable ratios (sharpe/win_rate/profit_factor) are None on a no-trade run, matching the nullable schema columns
    indicators: dict[str, "pd.Series"]  # named series for price-chart overlay
    regimes: dict[str, list[tuple[int, int, str]]]  # regime intervals (start, end, label)
