"""The buy-and-hold arm for a group (lane GV2-7a, plan step 4; judge check 9 compares the candidate with it).

Rule: at the window's first bar the starting cash is split in equal parts, one for each member; each part buys whole shares at that
bar's open, at the executed price open * (1 + slippage); the largest number of shares s with s * price + fee <= part is taken, the
fee being strategies/helpers/costs.ibkr_fixed_fee(s, price) when costs.fees is "ibkr_fixed" and 0 when "none". When costs.fees is
"alpaca" (FEE1a) a buy owes only the CAT fee, 0.000003 a share, and Alpaca rounds a day's total up to the cent and shares it among the
day's orders (strategies/helpers/alpaca_fees.daily_charged_fees): each member takes the largest whole s with
s * price + (s * 0.000003 rounded up to the cent) <= part. The day's rounded total never exceeds the sum of those per-member
roundings, so the cash stays at or above 0; the fees charged are daily_charged_fees over the buys. Any other fee model raises. The shares are held
to the window's last bar and never sold. The equity of a bar is the cash left plus each member's shares times its close (a close a
member lacks takes its last earlier close). Every member must have an open and a close on the window's first bar, or the arm raises.

Real shares (ADJ1). Stored prices are adjusted, so the open is not the price paid. When `frames` holds a "factor" frame (adjusted /
as-traded, from theory_run.frames.load_stored), f0 is each member's factor on the window's first bar and the rule above runs on the
real price, open * (1 + slippage) / f0: the largest whole n with n * real price + fee(n, real price) <= part is bought, cash falls by
n * real price plus the fee, and the member holds n / f0 adjusted shares, so the equity (cash + closes x adjusted shares) is in adjusted
units like the closes. Under "alpaca" the buys are (day, "buy", n, real price). A first-bar factor that is NaN or not above 0 raises,
naming the members. With no "factor" frame f0 is 1.0 and the arm is what it was.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, Decimal

import numpy as np
import pandas as pd

from strategies.helpers.alpaca_fees import CAT_PER_SHARE, CENT, daily_charged_fees
from strategies.helpers.costs import ibkr_fixed_fee

FEE_MODELS = ("none", "ibkr_fixed", "alpaca")


def _cat_rounded_up(n: int) -> float:
    """The CAT fee of n shares by itself, rounded up to the cent: the most one member's buy can be charged."""
    return float((Decimal(n) * CAT_PER_SHARE).quantize(CENT, rounding=ROUND_CEILING))


def _member_fee(model: str, n: int, price: float) -> float:
    """The fee a member's part must leave room for: IBKR's fixed fee, the member's own CAT fee rounded up, or none."""
    if n <= 0 or model == "none":
        return 0.0
    return ibkr_fixed_fee(n, price) if model == "ibkr_fixed" else _cat_rounded_up(n)


def buy_hold_equity(frames: dict, window: tuple, init_cash: float, costs: dict) -> pd.Series:
    if costs["fees"] not in FEE_MODELS:
        raise ValueError('costs["fees"] must be "none", "ibkr_fixed" or "alpaca", got %r' % (costs["fees"],))
    index = frames["close"].index
    start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    if index.tz is not None:
        start, end = start.tz_localize(index.tz), end.tz_localize(index.tz)
    mask = np.asarray((index >= start) & (index <= end))
    if not mask.any():
        raise ValueError("the window %s to %s holds no bar of the frames" % (window[0], window[1]))
    tickers = list(frames["close"].columns)
    first_open = frames["open"].to_numpy(dtype=float)[mask][0]
    first_close = frames["close"].to_numpy(dtype=float)[mask][0]
    lacking = [t for t, o, c in zip(tickers, first_open, first_close) if not (math.isfinite(o) and math.isfinite(c))]
    if lacking:
        raise ValueError("buy and hold needs every member priced on the window's first bar (%s); no open or close for: %s"
                         % (index[mask][0].date(), ", ".join(lacking)))
    if "factor" in frames:
        first_factor = frames["factor"].to_numpy(dtype=float)[mask][0]
        unknown = [t for t, f in zip(tickers, first_factor) if not (f > 0.0)]   # NaN compares False
        if unknown:
            raise ValueError("buy and hold needs every member's factor above 0 on the window's first bar (%s); no such factor for: %s"
                             % (index[mask][0].date(), ", ".join(unknown)))
    else:
        first_factor = np.ones(len(tickers))
    part = float(init_cash) / len(tickers)
    slip = float(costs["slippage"])
    cash = float(init_cash)
    shares = []    # adjusted shares held: whole real shares n divided by f0
    prices = []    # the real price paid
    real_counts = []
    for ticker, open_price, f0 in zip(tickers, first_open, first_factor):
        price = open_price * (1.0 + slip) / f0
        n = int(part // price)
        fee = _member_fee(costs["fees"], n, price)
        while n > 0 and n * price + fee > part:
            n -= 1
            fee = _member_fee(costs["fees"], n, price)
        real_counts.append(n)
        shares.append(n / f0)
        prices.append(price)
        cash -= n * price + (fee if costs["fees"] != "alpaca" else 0.0)
    if costs["fees"] == "alpaca":
        # The members' buys are one day's orders of one account: the day's CAT total is rounded up once and shared.
        buys = [(index[mask][0], "buy", n, price) for n, price in zip(real_counts, prices) if n > 0]
        cash -= sum(daily_charged_fees(buys))
    closes = frames["close"].ffill().to_numpy(dtype=float)[mask]
    equity = cash + closes @ np.array(shares, dtype=float)
    return pd.Series(equity, index=index[mask], name="equity")
