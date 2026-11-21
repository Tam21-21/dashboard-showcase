"""The money method "fixed_fraction" for run_group (lane TH1, DECISIONS #488 item 5, #489 items 3 and 20, #323).

Each entry is sized by one strategies.helpers.sizing.compute_position_sizes call: the risk fraction of the group's equity just before
that buy, divided by the stop distance (stop_multiple volatility units), rounded down to whole real shares. The group's equity is its
cash plus the held shares at the open of the fill bar, so the sizes compound. A sizing.py refusal skips the entry and is counted by
reason; an entry whose cost is above the cash left is skipped too (never partly filled), and counted only in entries_refused.

`Sizer.walk` replays the group's orders in vbt's call order (the sales at the open, the buys, the later sales, columns in order inside each
group) with the fee table vbt will be run with, and returns the size of every buy. run_group hands those sizes to vbt (size_type
"amount") and checks that vbt buys exactly them.

`risk_stop_breach` (lane TH1c) says on which bar a filled trade's risk stop, the stop the size was worked out from, is breached; simulate.py makes
the trade leave at the next open with the reason "risk_stop".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.helpers.alpaca_fees import buy_order_fee
from strategies.helpers.costs import ibkr_fixed_fee
from strategies.helpers import exits, sizing

SIZING_FIELDS = tuple(f for f in sizing.SizingSpec.__dataclass_fields__)


def spec_from(money: dict) -> "sizing.SizingSpec":
    """The SizingSpec of money["sizing"], a dict of exactly SizingSpec's five fields; anything else raises ValueError."""
    given = money.get("sizing")
    if not isinstance(given, dict):
        raise ValueError('money method "fixed_fraction" needs money["sizing"], a dict of %s, got %r' % (", ".join(SIZING_FIELDS), given))
    missing = [f for f in SIZING_FIELDS if f not in given]
    extra = [k for k in given if k not in SIZING_FIELDS]
    if missing or extra:
        raise ValueError('money["sizing"] must hold exactly %s; missing %s, extra %s' % (", ".join(SIZING_FIELDS), missing, extra))
    return sizing.SizingSpec(**given)


def risk_stop_breach(bars: "pd.DataFrame", spec, fill_date, last_date):
    """The date of the first bar at or before `last_date` that breaches the risk stop of a trade filled on `fill_date`, or None (lane TH1c).

    The rule is Carver's trailing stop as strategies.helpers.exits.compute_exit_lines draws it (DECISIONS #314, #304 Amendment 3): from the fill bar
    on, the level is the highest high since the entry (seeded at the entry's open) minus spec.stop_multiple times the volatility at the signal bar, the
    bar before the fill bar; the first bar whose low is at or below it is the breach. This function never works the rule out a second time: it asks
    compute_exit_lines with no profit line and a time limit as long as the bars, so only the stop, or the end of the bars, can end the walk.

    `bars` is one stock's full loaded open, high, low, close as a DataFrame by date, cut to its valid bars (none NaN), the lead-in included. A fill
    on the stock's first valid bar raises exits.UndefinedExitVolatilityError (no bar before it), and so does compute_exit_lines when the volatility at
    the signal bar is undefined; both texts hold "reason=undefined_warmup". Nothing is caught here.
    """
    bars = bars.loc[bars.index <= last_date]
    if fill_date not in bars.index:
        raise ValueError("risk_stop_breach: the fill date %s is not a bar at or before %s" % (fill_date, last_date))
    i = int(bars.index.get_loc(fill_date))
    if i == 0:
        raise exits.UndefinedExitVolatilityError("no bar before the fill; reason=undefined_warmup")
    entries = pd.Series(False, index=bars.index)
    entries.iloc[i - 1] = True
    got = exits.compute_exit_lines(bars["close"], bars["high"], bars["low"], bars["open"], entries, spec, profit_ratio=0.0,
                                   time_limit_bars=len(bars))
    where = np.flatnonzero(got.exits.to_numpy(dtype=bool))
    if len(where) != 1:
        raise RuntimeError("risk_stop_breach: compute_exit_lines closed %d trades for one entry (fill %s)" % (len(where), fill_date))
    reason = got.exit_reason.iloc[where[0]]
    if reason == "trailing_stop":
        return bars.index[where[0]]
    if reason == "end_of_data":
        return None
    raise RuntimeError("risk_stop_breach: compute_exit_lines ended the trade with reason %r, only trailing_stop or end_of_data are expected "
                       "(fill %s)" % (reason, fill_date))


def _fee_func(model: str):
    return {"ibkr_fixed": ibkr_fixed_fee, "alpaca": buy_order_fee, "none": None}[model]


class Sizer:
    """The sizes of one run_group call. `full` holds the whole loaded frames (open, high, low, close as DataFrames, every bar, not only the
    window), `factor` the factor DataFrame or None, `first` the position in them of the window's first bar. Sizes asked for twice at the
    same (stock, bar, equity) are answered from a dict, so the fee passes of one run repeat nothing."""

    def __init__(self, full: dict, factor, tickers: list, first: int, spec, slip: float, fee_model: str):
        self.tickers = tickers
        self.first = first
        self.spec = spec
        self.slip = slip
        self.fee_func = _fee_func(fee_model)
        self.cache = {}
        self.cols = []
        for t in tickers:
            ohlc = {k: full[k][t] for k in ("open", "high", "low", "close")}
            ok = ~(ohlc["open"].isna() | ohlc["high"].isna() | ohlc["low"].isna() | ohlc["close"].isna()).to_numpy()
            valid = np.flatnonzero(ok)
            cut = {k: v.iloc[valid] for k, v in ohlc.items()}
            cut["close"] = cut["close"].rename(t)
            self.cols.append({"valid": valid, "cut": cut, "factor": None if factor is None else factor[t].iloc[valid]})

    def _size(self, ci: int, fill: int, equity: float):
        """(size in the series' units, None) or (0.0, reason). `fill` is the window bar the entry fills on."""
        key = (ci, fill, equity)
        if key in self.cache:
            return self.cache[key]
        col = self.cols[ci]
        k = int(np.searchsorted(col["valid"], self.first + fill))   # the fill bar's place among the stock's valid bars
        if k == 0:
            out = (0.0, "undefined_warmup")
        else:
            cut = col["cut"]
            entries = pd.Series(False, index=cut["close"].index)
            entries.iloc[k - 1] = True
            try:
                res = sizing.compute_position_sizes(
                    cut["close"], cut["high"], cut["low"], cut["open"], entries, self.spec, equity=equity,
                    slippage_rate=self.slip, factor=col["factor"], fixed_fee_func=self.fee_func)
            except sizing.SizingError as exc:
                out = (0.0, sizing.refusal_reason(str(exc)))
            else:
                out = (float(res.shares.iloc[k - 1]), None)
        self.cache[key] = out
        return out

    def walk(self, entry_arr, exit_arr, price, carried_open, call_seq, fixed, init_cash: float):
        """Returns (sizes, refusals): sizes a (bars, stocks) float array, 0 where nothing is bought; refusals the sizing.py reasons counted."""
        n_bars, n_stocks = entry_arr.shape
        sizes = np.zeros(entry_arr.shape, dtype=float)
        refusals = {}
        held = np.zeros(n_stocks, dtype=float)
        cash = float(init_cash)
        for bar in range(n_bars):
            for ci in call_seq[bar]:
                if exit_arr[bar, ci]:
                    if held[ci] > 0.0:
                        cash += held[ci] * price[bar, ci] * (1.0 - self.slip) - fixed[bar, ci]
                        held[ci] = 0.0
                elif entry_arr[bar, ci]:
                    in_hand = held > 0.0   # a stock not held is not valued: its open may still be NaN
                    equity = cash + float(np.dot(held[in_hand], carried_open[bar][in_hand]))
                    size, reason = self._size(int(ci), bar, equity)
                    if reason is not None:
                        refusals[reason] = refusals.get(reason, 0) + 1
                        continue
                    cost = size * price[bar, ci] * (1.0 + self.slip) + fixed[bar, ci]
                    if cost > cash:
                        continue
                    cash -= cost
                    held[ci] = size
                    sizes[bar, ci] = size
        return sizes, refusals
