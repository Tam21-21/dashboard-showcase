"""Running a stock group as one portfolio with shared cash (lane GV2-5a, DECISIONS #445 item 1, #440, #442, #323).

`run_group(frames, entries, exits, money, costs, window)` takes the group's bars (one DataFrame per field, tickers as
columns), a boolean frame of entry signals seen at the close, the exit rules, the money method, the costs and the window,
and returns a `GroupResult`.

The rules, in plain words:
  Fills. A signal seen at the close of bar s fills at the open of the stock's next bar. Long only. A stock has at most one
    open trade; a signal on a bar where the stock is still in a trade (up to and including the bar it leaves on) opens
    nothing. A signal before the window's first bar is never traded; neither is one with no next bar inside the window.
  Money, "equal_slots". Each entry is sized to at most the group's value at the fill bar's open divided by the number of
    stocks in the group; positions are valued at that bar's open. Fractional shares. If the cash left is smaller than that,
    the order takes the cash that is left (vbt allow_partial); an entry refused for lack of cash altogether is counted in
    `entries_refused`, never hidden. A sale at the close, or at a level inside the bar, never pays that day's buys, which are made at the
    open: vbt is given the order of the day's orders (the call sequence), the sales at the open first, then the buys, then the later sales,
    column order inside each group. (vbt's own "auto" order would put every sale before every buy, and a sale at the close cannot fund
    a purchase at the open.)
  Money, "fixed_fraction" (lane TH1, DECISIONS #323, #489). Each entry is sized by one strategies.helpers.sizing.compute_position_sizes call, with
    the SizingSpec given in money["sizing"] (exactly its five fields, never a default): the risk fraction of the group's value just before that
    buy (cash plus the held shares at the fill bar's open, so sizes compound) divided by the stop distance, whole real shares. A sizing.py
    refusal skips the entry and is counted by reason in `GroupResult.sizing_refusals`; an entry costing more than the cash left is skipped,
    never partly filled, and counted in `entries_refused` only. The frames must hold at least the spec's estimator_window bars before the
    window's first bar. The sizes come from a walk over the orders in vbt's call order (fixed_fraction.Sizer.walk) with the same fee table
    as the vbt run; vbt buys exactly those sizes (size_type "amount") or the run raises.
  Exits. From the bar after the fill bar (the fill bar's own range is not checked, because vbt cannot hold an entry and an exit on one bar),
    each bar k is read in this order, p being the bar before it:
    1. a sell signal (an exit group) seen at the close of p, the fill bar included, never one before the fill bar: out at the open of k;
    1b. only under "fixed_fraction" (lane TH1c, DECISIONS #489 items 3 D and 15): the risk stop, the stop the size was worked out from, as Carver's
       trailing stop decided by strategies/helpers/exits.compute_exit_lines (through fixed_fraction.risk_stop_breach, never a second copy of its
       rule): its level is the highest high from the fill bar to p (seeded at the fill's open) minus the spec's stop_multiple times the volatility
       at the signal bar, frozen for the trade; unlike the rules below it reads the fill bar's own range too. A bar p whose low is at or below the
       level is seen there and the trade leaves at the open of k, the stock's next bar (#292): out at the open of k, reason "risk_stop". A breach on the
       window's last bar leaves the trade open. "equal_slots" has no such stop;
    2. the stop, a fraction of the executed entry price (a number, or a DataFrame of fractions by signal bar, NaN meaning none): if the low
       of k reaches the level, out at the level, or at the open when the open is already below it;
    3. the trailing stop, if set (`trail` times ATR from `atr`): its level for bar k is the highest close from the fill bar to p minus `trail`
       times the ATR at p, and never lower than the level for the bar before; if the low of k reaches it, out at the level, or at the open
       when the open is below it. The ATR must be known at the fill bar, or the run raises;
    4. the target, a fraction like the stop: if the high of k reaches the level, out at the level, or at the open when it gaps above it;
    5. `max_hold`: out at the close of the bar that completes that many bars counting the fill bar.
    A bar reaching several levels takes the first in this order (the order inside a bar is unknowable, so the stop comes first). A trade still
    open on the window's last bar stays open and is marked at that bar's close. `GroupResult.exit_reasons` counts the closed trades by the
    rule that ended them (sell, stop, trail, target, max_hold, risk_stop; the last is always 0 under "equal_slots"); an exit planned for an entry that was refused for cash counts nowhere.
  Costs. `fees` "none": no fee. `fees` "ibkr_fixed": the fee of each order is strategies/helpers/costs.ibkr_fixed_fee on its
    executed size and price. Stored prices are adjusted (ADJ1), so size and price are in adjusted units; when `frames` holds a
    "factor" frame (adjusted / as-traded, from load_stored) each fee is charged on the real shares at the real price, (size x f,
    price / f), f the factor at that order's bar and stock, under "ibkr_fixed" and "alpaca" alike. Orders, trades and equity stay
    in adjusted units. With no "factor" frame the fees are charged on the sizes and prices as they are. `fees` "alpaca": the fees are Alpaca's, strategies/helpers/alpaca_fees.daily_charged_fees (SEC fee on
    sells, CAT fee on buys and sells, summed per day for the whole group as one account, rounded up to the cent, and shared among
    the day's orders in proportion to each order's unrounded fee of that type); a theory bar is one session, so the day of an order
    is its bar. In both models the fee changes the cash and so the next sizes, and the size changes the fee, so the fixed fee of
    every order is solved by repeating the run until the fees stop changing. Measured 2026-10-17 (docs/log/rerun_profile_2026-10-17.md,
    six SYN_VOL stocks, theory_stage _spec.py's three variations): "ibkr_fixed" takes 8, 8 and 10 passes, the largest gap between the
    fees wanted and the fees used falling 20 to 70 times a pass (217, 10.8, 0.25, 0.0036, 3.6e-5 ... down to 9.5e-12 dollars); "alpaca" takes
    3 (0.615, 5.2e-7, 1.1e-16 dollars).
    Under "alpaca" the round-up can make the fees flip between two states; `_settle` then ends the loop by the rule in its docstring
    and `fee_cycles` is 1. `slippage` is a fraction on every executed price (buys pay more, sells get less).
  All timing is decided here, from the bars, and handed to one vbt.Portfolio.from_signals over all columns as one group
  with shared cash: entries on the fill bar, exits on the exit bar, an explicit price per order, no callback.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategies.base.vbt_columns import CANONICAL_TRADE_COLUMNS, VBT_TO_CANONICAL_TRADES
from strategies.helpers.alpaca_fees import daily_charged_fees
from strategies.helpers.costs import ibkr_fixed_fee
from strategies.helpers.exits import UndefinedExitVolatilityError

from research_lab.theory_run import fixed_fraction

FEE_MODELS = ("none", "ibkr_fixed", "alpaca")
FEE_TOLERANCE = 1e-11   # the fixed-fee passes stop when no order's fee moves by more than this (dollars)
MAX_FEE_PASSES = 60     # each pass shrinks the gap 20 to 70 times (8 to 10 passes measured under "ibkr_fixed", 3 under "alpaca"), so a run that needs more is broken, not slow
TRADING_DAYS = 252      # the Sharpe ratio's annualisation, daily bars (plan step 4)


@dataclass(frozen=True)
class ExitRules:
    """max_hold: bars to hold counting the fill bar (2 or more), or None. stop, target: None, a fraction, or a DataFrame.
    sell: None or a boolean DataFrame, True where a sell signal is seen at the close. trail: None or a number above 0, the trailing stop's
    distance in ATRs; atr: the ATR DataFrame it reads. trail and atr are given together or not at all."""
    max_hold: "int | None" = None
    stop: object = None
    target: object = None
    sell: object = None
    trail: object = None
    atr: object = None


@dataclass
class GroupResult:
    equity: pd.Series
    trades: pd.DataFrame
    orders: pd.DataFrame
    bar_returns: pd.Series
    sharpe: float
    sharpe_per_bar: float
    num_trades: int
    num_open_at_end: int
    max_open_at_once: int
    total_return: float
    max_drawdown: float
    fees_paid: float
    slippage_paid: float
    entries_planned: int = 0
    entries_refused: int = 0
    fee_passes: int = 0
    fee_cycles: int = 0   # 1 when the "alpaca" fees flipped between two states and _settle ended the loop (counted, never hidden)
    exit_reasons: dict = field(default_factory=dict)   # closed trades by the rule that ended them: sell, stop, trail, target, max_hold, risk_stop (always 0 under equal_slots)
    sizing_refusals: dict = field(default_factory=dict)   # fixed_fraction: entries sizing.py refused, counted by reason (refusal_reason); {} under equal_slots
    window: tuple = field(default_factory=tuple)


def _vbt():
    import vectorbtpro as vbt  # noqa: PLC0415 - only the research-runner images carry vbt
    # GV2-8a: vbt's cache registry keeps every run's Index and ArrayWrapper alive (about 0.37 MB a strategy, measured
    # 2026-10-14), which ran the size-10,000 search out of memory at the 4 GiB cap. The theory path never reuses a cached
    # result, so the machinery is off from the first vbt call; results are bit-identical on and off (probe r12).
    vbt.settings["caching"]["disable_machinery"] = True
    return vbt


def _check_inputs(frames, entries, exits, money, costs, window):
    for name in ("open", "high", "low", "close"):
        if name not in frames:
            raise ValueError("frames has no %r frame" % name)
    like = frames["close"]
    for name in ("open", "high", "low"):
        if not (frames[name].index.equals(like.index) and list(frames[name].columns) == list(like.columns)):
            raise ValueError("frames[%r] does not share the close frame's dates and tickers" % name)
    if "factor" in frames and not (frames["factor"].index.equals(like.index) and list(frames["factor"].columns) == list(like.columns)):
        raise ValueError("frames['factor'] does not share the close frame's dates and tickers")
    if not (entries.index.equals(like.index) and list(entries.columns) == list(like.columns)):
        raise ValueError("entries does not share the frames' dates and tickers")
    if not like.index.is_monotonic_increasing or like.index.has_duplicates:
        raise ValueError("the frames' dates must be strictly increasing")
    if not isinstance(exits, ExitRules):
        raise ValueError("exits must be an ExitRules")
    if exits.max_hold is not None and (isinstance(exits.max_hold, bool) or int(exits.max_hold) != exits.max_hold or exits.max_hold < 2):
        raise ValueError("max_hold must be a whole number of at least 2 (the entry fills at the open, so a 1-bar hold cannot "
                         "also leave at that bar's close), got %r" % (exits.max_hold,))
    if exits.sell is not None:
        if not (isinstance(exits.sell, pd.DataFrame) and exits.sell.index.equals(like.index) and list(exits.sell.columns) == list(like.columns)):
            raise ValueError("exits.sell must be a DataFrame sharing the frames' dates and tickers")
        if not all(pd.api.types.is_bool_dtype(t) for t in exits.sell.dtypes):
            raise ValueError("exits.sell must hold booleans")
    if (exits.trail is None) != (exits.atr is None):
        raise ValueError("a trailing stop needs both exits.trail and exits.atr")
    if exits.trail is not None:
        if isinstance(exits.trail, bool) or not isinstance(exits.trail, (int, float)) or not math.isfinite(exits.trail) or exits.trail <= 0:
            raise ValueError("exits.trail must be a number above 0, got %r" % (exits.trail,))
        if not (isinstance(exits.atr, pd.DataFrame) and exits.atr.index.equals(like.index) and list(exits.atr.columns) == list(like.columns)):
            raise ValueError("exits.atr must be a DataFrame sharing the frames' dates and tickers")
    if money.get("method") not in ("equal_slots", "fixed_fraction"):
        raise ValueError("money method %r is not supported here; run_group does equal_slots and fixed_fraction" % (money.get("method"),))
    if money["method"] == "fixed_fraction":
        fixed_fraction.spec_from(money)
    cash = money.get("init_cash")
    if not isinstance(cash, (int, float)) or isinstance(cash, bool) or not math.isfinite(cash) or cash <= 0:
        raise ValueError("money needs init_cash, a positive number, got %r" % (cash,))
    if costs.get("fees") not in FEE_MODELS:
        raise ValueError('costs["fees"] must be "none", "ibkr_fixed" or "alpaca", got %r' % (costs.get("fees"),))
    slip = costs.get("slippage")
    if not isinstance(slip, (int, float)) or isinstance(slip, bool) or not (0 <= slip < 1):
        raise ValueError('costs["slippage"] must be a fraction from 0 up to 1, got %r' % (slip,))
    if len(window) != 2:
        raise ValueError("window is (start, end)")


def _fractions(rule, like: pd.DataFrame, mask: np.ndarray, what: str):
    """A stop or target rule as a (bars, stocks) float array of fractions by signal bar, NaN for none, or None."""
    if rule is None:
        return None
    if isinstance(rule, pd.DataFrame):
        if not (rule.index.equals(like.index) and list(rule.columns) == list(like.columns)):
            raise ValueError("%s frame does not share the frames' dates and tickers" % what)
        arr = rule.to_numpy(dtype=float)[mask]
    else:
        if isinstance(rule, bool) or not isinstance(rule, (int, float)):
            raise ValueError("%s must be None, a fraction or a DataFrame of fractions, got %r" % (what, rule))
        arr = np.full((int(mask.sum()), like.shape[1]), float(rule))
    if np.any(arr[~np.isnan(arr)] <= 0):
        raise ValueError("%s fractions must be above 0" % what)
    if what == "stop" and np.any(arr[~np.isnan(arr)] >= 1):
        raise ValueError("stop fractions must be below 1")
    return arr


def _plan_column(o, h, l, c, sig, stop, target, max_hold, slip, sell=None, trail=None, atr=None, ticker="", dates=None, risk=None):
    """The trades of one stock, decided from its bars. Returns (fills, exits): [(bar, nominal price)] and
    [(bar, price, reason, at_open)]: reason is sell, stop, trail, target, max_hold or risk_stop; at_open is True when the sale is made at that bar's
    open (a sell, a risk stop, or a level the bar's open had already gone through) and False for a level inside the bar or the close.
    `risk` (lane TH1c) is None or a callable (fill bar, last bar) -> the first bar from the fill bar to the last bar whose low breaches the risk
    stop, or None, all in window positions. Given, it is asked for each trade after the trade's own exit is set, up to the stock's valid bar before
    that exit (up to its second-to-last valid bar when the trade has no own exit, so a breach on the window's last bar, which has no next open to
    leave at, leaves the trade open); a breach sends the trade out at the open of the stock's next valid bar, unless the trade's own exit is a sell
    on that same bar, which comes first."""
    valid = np.flatnonzero(~(np.isnan(o) | np.isnan(h) | np.isnan(l) | np.isnan(c)))
    fills, leaves = [], []
    last_exit = -1
    for s in np.flatnonzero(sig):
        if s <= last_exit:
            continue
        j = int(np.searchsorted(valid, s, side="right"))
        if j >= len(valid):
            continue
        f = int(valid[j])
        paid = o[f] * (1.0 + slip)
        stop_level = paid * (1.0 - stop[s]) if stop is not None and not np.isnan(stop[s]) else None
        target_level = paid * (1.0 + target[s]) if target is not None and not np.isnan(target[s]) else None
        if trail is not None and np.isnan(atr[f]):
            raise ValueError("%s %s: ATR unknown at the fill bar" % (ticker, dates[f].date()))
        fills.append((f, o[f]))
        exit_bar, exit_price, reason, at_open = None, None, None, False
        top_close, level = -np.inf, None
        for t in range(1, len(valid) - j):
            k = int(valid[j + t])
            p = int(valid[j + t - 1])
            top_close = max(top_close, c[p])
            if sell is not None and sell[p]:
                exit_bar, exit_price, reason, at_open = k, o[k], "sell", True
                break
            if stop_level is not None and l[k] <= stop_level:
                exit_bar, exit_price, reason, at_open = k, min(o[k], stop_level), "stop", bool(o[k] <= stop_level)
                break
            if trail is not None:
                if np.isnan(atr[p]):
                    raise ValueError("%s %s: ATR unknown at the bar the trailing stop reads" % (ticker, dates[p].date()))
                raw = top_close - trail * atr[p]
                level = raw if level is None else max(level, raw)
                if l[k] <= level:
                    exit_bar, exit_price, reason, at_open = k, min(o[k], level), "trail", bool(o[k] <= level)
                    break
            if target_level is not None and h[k] >= target_level:
                exit_bar, exit_price, reason, at_open = k, max(o[k], target_level), "target", bool(o[k] >= target_level)
                break
            if max_hold is not None and t == max_hold - 1:
                exit_bar, exit_price, reason, at_open = k, c[k], "max_hold", False
                break
        if risk is not None:
            # The stock's valid bar before the own exit, or its second-to-last valid bar when the trade has no own exit.
            if exit_bar is None:
                before_own = len(valid) - 2
            else:
                before_own = int(np.searchsorted(valid, exit_bar)) - 1
            last = int(valid[before_own]) if before_own >= 0 else -1
            if last >= f:
                breach = risk(f, last)
                if breach is not None:
                    leave = int(valid[int(np.searchsorted(valid, breach, side="right"))])   # the next valid bar after the breach
                    if not (exit_bar == leave and reason == "sell"):
                        exit_bar, exit_price, reason, at_open = leave, o[leave], "risk_stop", True
        if exit_bar is None:
            last_exit = len(o)   # open to the window's end: every later signal is in a trade
        else:
            leaves.append((exit_bar, exit_price, reason, at_open))
            last_exit = exit_bar
    return fills, leaves


def _risk_function(bars: pd.DataFrame, spec, win_index, ci: int, unknown: set):
    """The `risk` callable _plan_column takes, for one stock (lane TH1c): (fill bar, last bar) in window positions -> the breach bar, in window
    positions, or None. `bars` is the stock's full loaded open, high, low, close with the rows holding a NaN left out (the lead-in included).
    The one error caught is the warm-up one (a stock with too few bars before its signal): the trade gets no risk stop and its
    (fill bar, stock) goes into `unknown`; run_group raises if such a cell is ever sized above 0. Any other error goes up."""
    position = {ts: i for i, ts in enumerate(win_index)}

    def risk(fill: int, last: int):
        try:
            date = fixed_fraction.risk_stop_breach(bars, spec, win_index[fill], win_index[last])
        except UndefinedExitVolatilityError as exc:
            if "reason=undefined_warmup" not in str(exc):
                raise
            unknown.add((fill, ci))
            return None
        return None if date is None else position[date]
    return risk


def _call_sequence(entry_arr: np.ndarray, exit_arr: np.ndarray, open_arr: np.ndarray) -> np.ndarray:
    """vbt's call sequence, a (bars, stocks) integer array: on each bar the sales at the open, then the buys, then the sales made later
    in the bar (a level inside it, or the close), the stocks in column order inside each group; each row lists every column once.
    Stable, so a tie keeps column order."""
    rank = np.where(exit_arr & open_arr, 0, np.where(entry_arr, 1, np.where(exit_arr, 2, 3)))
    return np.argsort(rank, axis=1, kind="stable").astype(np.int64)


def frame_ffill(arr: np.ndarray) -> np.ndarray:
    """A copy of a (bars, stocks) array with each NaN replaced by the stock's last earlier value (NaN before its first)."""
    return pd.DataFrame(arr).ffill().to_numpy(dtype=float)


def _run_vbt(vbt, o, h, l, c, entries, exits, price, mark_open, size, size_type, allow_partial, init_cash, slip, fixed, call_seq):
    return vbt.Portfolio.from_signals(
        close=c, open=o, high=h, low=l,
        entries=entries, exits=exits, price=price, val_price=mark_open,
        size=size, size_type=size_type,
        init_cash=float(init_cash), fees=0.0, fixed_fees=fixed, slippage=float(slip),
        direction="longonly", accumulate=False, allow_partial=allow_partial,
        cash_sharing=True, group_by=True, freq="1D", call_seq=call_seq,
    )


ORDER_COLUMNS = ["ticker", "date", "side", "size", "price", "fee", "nominal_price"]


def _order_arrays(pf) -> dict:
    """The orders of a vbt portfolio as arrays, read from the raw order records (fields col, idx, size, price, fees, side; side 0 Buy, 1 Sell),
    in the order the records hold them."""
    rec = pf.orders.values
    return {"idx": rec["idx"].astype(np.int64), "col": rec["col"].astype(np.int64), "size": rec["size"].astype(float),
            "price": rec["price"].astype(float), "fees": rec["fees"].astype(float), "side": rec["side"].astype(np.int64)}


def _sorted_positions(arr: dict, tickers) -> np.ndarray:
    """The positions that put the orders in (date, ticker) order, the ticker compared as text."""
    rank = np.empty(len(tickers), dtype=np.int64)
    rank[np.argsort(np.array([str(t) for t in tickers]), kind="stable")] = np.arange(len(tickers))
    return np.lexsort((rank[arr["col"]], arr["idx"]))


def _orders_frame(arr: dict, price: np.ndarray, win_index, tickers) -> pd.DataFrame:
    """The orders table (ORDER_COLUMNS) from the arrays of _order_arrays; `price` is the (bars, stocks) nominal price array."""
    if len(arr["idx"]) == 0:   # no order at all: an empty table that keeps its columns
        return pd.DataFrame({c: pd.Series(dtype=object if c in ("ticker", "side") else float) for c in ORDER_COLUMNS})
    dates = win_index[arr["idx"]]
    out = pd.DataFrame({
        "ticker": np.array(tickers, dtype=object)[arr["col"]],
        "date": dates.to_numpy() if win_index.tz is None else dates,
        "side": np.where(arr["side"] == 0, "buy", "sell").astype(object),
        "size": arr["size"],
        "price": arr["price"],
        "fee": arr["fees"],
        "nominal_price": price[arr["idx"], arr["col"]].astype(float),
    })
    return out.sort_values(["date", "ticker"]).reset_index(drop=True)


def _settle(before, fixed, wanted) -> str:
    """What the fee loop does after a run. `fixed` is the fee table the run just used, `wanted` the table that run's orders call for,
    `before` the table the run before used (None at the first run); each a (bars, stocks) array or DataFrame.
      "done"         wanted is within FEE_TOLERANCE of fixed: the run is settled.
      "cycle_keep"   wanted is within FEE_TOLERANCE of before, so the fees flip between the two tables, and fixed's total is at
                     least before's: keep the last run (the dearer of the two states).
      "cycle_rerun"  the same flip with fixed's total below before's: run once more with before and keep that run.
      "continue"     neither: another pass (the caller raises when MAX_FEE_PASSES is spent)."""
    fixed, wanted = np.asarray(fixed, dtype=float), np.asarray(wanted, dtype=float)
    if float(np.abs(wanted - fixed).max()) <= FEE_TOLERANCE:
        return "done"
    if before is not None:
        before = np.asarray(before, dtype=float)
        if float(np.abs(wanted - before).max()) <= FEE_TOLERANCE:
            return "cycle_keep" if float(fixed.sum()) >= float(before.sum()) else "cycle_rerun"
    return "continue"


def _wanted_fees(model: str, arr: dict, win_index, tickers, factor=None) -> np.ndarray:
    """The fee each order calls for, as a (bars, stocks) array; one order to a (date, ticker) cell, or it raises.
    `factor` is the (bars, stocks) array of adjusted / as-traded, or None. Given, each fee is charged on the real shares at the real price,
    (size x f, price / f), the rule of strategies/helpers/portfolio._real_fee_inputs (not imported: that file is the backtest path's);
    an f that is NaN or not above 0 at an order's cell raises. None leaves size and price as they are."""
    wanted = np.zeros((len(win_index), len(tickers)), dtype=float)
    if len(arr["idx"]) == 0:
        return wanted
    pos = _sorted_positions(arr, tickers)
    idx, col, size, price, side = (arr[k][pos] for k in ("idx", "col", "size", "price", "side"))
    keys = idx * len(tickers) + col
    _, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    shared = counts[inverse] > 1
    if shared.any():
        first = int(np.flatnonzero(shared)[0])
        raise RuntimeError("two orders share one (date, ticker) cell, %s %s: a fixed fee cannot be set per order" % (win_index[idx[first]], tickers[col[first]]))
    if factor is not None:
        f = factor[idx, col]
        bad = ~(f > 0.0)   # NaN compares False, so it is caught too
        if bad.any():
            first = int(np.flatnonzero(bad)[0])
            raise RuntimeError("%s on %s: the factor is %r, not a number above 0; the real shares and price of the order are unknown"
                               % (tickers[col[first]], win_index[idx[first]], float(f[first])))
        size, price = size * f, price / f
    if model == "alpaca":
        dates = win_index[idx]
        sides = np.where(side == 0, "buy", "sell")
        charged = daily_charged_fees(list(zip(dates, sides.tolist(), size.tolist(), price.tolist())))
    else:
        charged = [ibkr_fixed_fee(sz, pr) for sz, pr in zip(size.tolist(), price.tolist())]
    wanted[idx, col] = charged
    return wanted


def _trades_frame(pf) -> pd.DataFrame:
    readable = pf.trades.records_readable   # built once: vbt rebuilds the table on every call
    if len(readable) == 0:   # no trade: an empty table with the canonical columns and ticker
        return pd.DataFrame({c: pd.Series(dtype=object) for c in CANONICAL_TRADE_COLUMNS + ["ticker"]})
    trades = readable.rename(columns=VBT_TO_CANONICAL_TRADES)
    trades["mae"] = pf.trades.mae.values
    trades["mfe"] = pf.trades.mfe.values
    trades["mae_return_pct"] = pf.trades.mae_returns.values
    trades["mfe_return_pct"] = pf.trades.mfe_returns.values
    trades = trades[CANONICAL_TRADE_COLUMNS].copy()
    trades["ticker"] = trades["column"]
    return trades.reset_index(drop=True)


def run_group(frames: dict, entries: pd.DataFrame, exits: ExitRules, money: dict, costs: dict, window: tuple) -> GroupResult:
    _check_inputs(frames, entries, exits, money, costs, window)
    vbt = _vbt()
    like = frames["close"]
    index = like.index
    start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    if index.tz is not None:
        start, end = start.tz_localize(index.tz), end.tz_localize(index.tz)
    mask = np.asarray((index >= start) & (index <= end))
    if not mask.any():
        raise ValueError("the window %s to %s holds no bar of the frames (%s to %s)" % (window[0], window[1], index[0], index[-1]))
    tickers = list(like.columns)
    n_stocks = len(tickers)
    slip = float(costs["slippage"])
    first = int(np.flatnonzero(mask)[0])
    sizer = None
    if money["method"] == "fixed_fraction":
        spec = fixed_fraction.spec_from(money)
        if first < spec.estimator_window:
            raise ValueError("fixed_fraction sizing needs %d bars before the window's first bar (the sizing estimator_window); the frames hold %d"
                             % (spec.estimator_window, first))
        sizer = fixed_fraction.Sizer({k: frames[k] for k in ("open", "high", "low", "close")}, frames.get("factor"), tickers, first, spec, slip,
                                     costs["fees"])

    raw = {name: frames[name].to_numpy(dtype=float)[mask] for name in ("open", "high", "low", "close")}
    sig = entries.to_numpy(dtype=bool)[mask]
    stop = _fractions(exits.stop, like, mask, "stop")
    target = _fractions(exits.target, like, mask, "target")
    sell = None if exits.sell is None else exits.sell.to_numpy(dtype=bool)[mask]
    atr = None if exits.atr is None else exits.atr.to_numpy(dtype=float)[mask]
    factor = frames["factor"].to_numpy(dtype=float)[mask] if "factor" in frames else None
    win_index = index[mask]

    entry_arr = np.zeros(sig.shape, dtype=bool)
    exit_arr = np.zeros(sig.shape, dtype=bool)
    open_arr = np.zeros(sig.shape, dtype=bool)   # True where the planned sale is made at the bar's open
    reasons = {}                                  # (bar, stock) of a planned sale -> the rule that ends the trade
    price = frame_ffill(raw["open"])
    no_risk_stop = set()   # fixed_fraction: (fill bar, stock) of trades whose risk stop could not be worked out (the warm-up refusal)
    for ci in range(n_stocks):
        risk = None
        if sizer is not None:
            bars = pd.DataFrame({k: frames[k][tickers[ci]] for k in ("open", "high", "low", "close")}).dropna()
            risk = _risk_function(bars, spec, win_index, ci, no_risk_stop)
        fills, leaves = _plan_column(
            raw["open"][:, ci], raw["high"][:, ci], raw["low"][:, ci], raw["close"][:, ci], sig[:, ci],
            None if stop is None else stop[:, ci], None if target is None else target[:, ci], exits.max_hold, slip,
            None if sell is None else sell[:, ci], exits.trail, None if atr is None else atr[:, ci], tickers[ci], win_index, risk)
        for bar, p in fills:
            entry_arr[bar, ci] = True
            price[bar, ci] = p
        for bar, p, reason, at_open in leaves:
            exit_arr[bar, ci] = True
            open_arr[bar, ci] = at_open
            reasons[(bar, ci)] = reason
            price[bar, ci] = p
    call_seq = _call_sequence(entry_arr, exit_arr, open_arr)

    def frame(arr):
        return pd.DataFrame(arr, index=win_index, columns=tickers)

    # Marking a held stock on a session it lacks uses its last known bar; no signal and no fill ever uses a carried bar.
    carried = {name: frame(raw[name]).ffill() for name in raw}
    price_frame = frame(price)
    planned = int(entry_arr.sum())
    init_cash = float(money["init_cash"])
    fixed = np.zeros(sig.shape, dtype=float)
    before = None
    passes = 0
    cycles = 0
    carried_open = carried["open"].to_numpy(dtype=float)
    refusals = {}

    def vbt_pass(table):
        """One vbt run with the fee table `table`; under fixed_fraction the sizes come from the walk made with that same table, and vbt's buys must equal them."""
        nonlocal refusals
        if sizer is None:
            return _run_vbt(vbt, carried["open"], carried["high"], carried["low"], carried["close"], frame(entry_arr), frame(exit_arr),
                            price_frame, carried["open"], 1.0 / n_stocks, "valuepercent", True, init_cash, slip, frame(table), call_seq)
        sizes, refusals = sizer.walk(entry_arr, exit_arr, price, carried_open, call_seq, np.asarray(table, dtype=float), init_cash)
        for bar, ci in sorted(no_risk_stop):
            if sizes[bar, ci] > 0.0:   # sizing.py refuses the same entry (the same warm-up rule), so this fires only if the two ever disagree
                raise RuntimeError("%s %s: sized %r shares, but its risk stop is undefined (reason=undefined_warmup)" % (tickers[ci], win_index[bar].date(),
                                                                                                                         float(sizes[bar, ci])))
        run =_run_vbt(vbt, carried["open"], carried["high"], carried["low"], carried["close"], frame(sizes > 0.0), frame(exit_arr),
                       price_frame, carried["open"], frame(sizes), "amount", False, init_cash, slip, frame(table), call_seq)
        got = _order_arrays(run)
        bought = {(int(i), int(c)): float(z) for i, c, z, sd in zip(got["idx"], got["col"], got["size"], got["side"]) if sd == 0}
        for bar, ci in sorted(set(bought) | {(int(b), int(c)) for b, c in zip(*np.nonzero(sizes))}):
            if bought.get((bar, ci), 0.0) != float(sizes[bar, ci]):
                raise RuntimeError("%s %s: vbt bought %r shares, the sizing walk sized %r" % (tickers[ci], win_index[bar].date(), bought.get((bar, ci), 0.0),
                                                                                                float(sizes[bar, ci])))
        return run

    while True:
        passes += 1
        pf = vbt_pass(fixed)
        arr = _order_arrays(pf)
        if costs["fees"] == "none":
            break
        wanted = _wanted_fees(costs["fees"], arr, win_index, tickers, factor)
        # Only "alpaca" can flip between two states (the round-up is a step); "ibkr_fixed" keeps its single-state loop.
        verdict = _settle(before if costs["fees"] == "alpaca" else None, fixed, wanted)
        if verdict == "done":
            break
        if verdict == "cycle_keep":
            cycles = 1
            break
        if verdict == "cycle_rerun":
            cycles = 1
            passes += 1
            pf = vbt_pass(before)
            arr = _order_arrays(pf)
            break
        if passes >= MAX_FEE_PASSES:
            raise RuntimeError("the fixed fees did not settle after %d passes" % passes)
        before, fixed = fixed, wanted
    orders = _orders_frame(arr, price, win_index, tickers)

    equity = pf.value
    equity = pd.Series(equity.to_numpy(dtype=float), index=win_index, name="equity")
    bar_returns = equity.pct_change()
    bar_returns.iloc[0] = equity.iloc[0] / init_cash - 1.0
    bar_returns.name = "bar_return"
    sd = float(bar_returns.std(ddof=1)) if len(bar_returns) > 1 else float("nan")
    per_bar = float(bar_returns.mean()) / sd if sd and math.isfinite(sd) else float("nan")
    trades = _trades_frame(pf)
    closed = trades["status"].str.lower() == "closed"
    held = np.zeros(len(win_index), dtype=int)
    pos = {ts: i for i, ts in enumerate(win_index)}
    column = {name: ci for ci, name in enumerate(tickers)}
    exit_reasons = {"sell": 0, "stop": 0, "trail": 0, "target": 0, "max_hold": 0, "risk_stop": 0}
    for _, t in trades.iterrows():
        lo = pos[pd.Timestamp(t["entry_index"])]
        if t["status"].lower() == "closed":
            hi = pos[pd.Timestamp(t["exit_index"])]
            key = (hi, column[t["ticker"]])
            if key not in reasons:
                raise RuntimeError("%s: a trade closed on %s that no planned exit explains" % (t["ticker"], t["exit_index"]))
            exit_reasons[reasons[key]] += 1
        else:
            hi = len(win_index) - 1
        held[lo:hi + 1] += 1
    drawdown = float((equity / equity.cummax() - 1.0).min())
    slippage_paid = float((orders["size"] * (orders["price"] - orders["nominal_price"]).abs()).sum())
    buys = int((orders["side"] == "buy").sum())
    return GroupResult(
        equity=equity, trades=trades, orders=orders, bar_returns=bar_returns,
        sharpe=per_bar * math.sqrt(TRADING_DAYS), sharpe_per_bar=per_bar,
        num_trades=int(closed.sum()), num_open_at_end=int((~closed).sum()), max_open_at_once=int(held.max()),
        total_return=float(equity.iloc[-1] / init_cash - 1.0), max_drawdown=drawdown,
        fees_paid=float(orders["fee"].sum()), slippage_paid=slippage_paid,
        entries_planned=planned, entries_refused=planned - buys, fee_passes=passes, fee_cycles=cycles,
        exit_reasons=exit_reasons, sizing_refusals=dict(refusals),
        window=(str(window[0]), str(window[1])),
    )
