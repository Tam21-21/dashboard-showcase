"""Alpaca's fees on US equities, exactly as Alpaca charges them (lane FEE1a, DECISIONS #456 and #459 item 4).

Alpaca takes no commission. It passes on three regulatory fees (Alpaca Brokerage Fee Schedule revised 2026-10-12, sections "Pass-Through
Regulatory and Exchange Fees" and "How fees are charged"; Tamer 2026-10-16: "make it match alpaca exactly"):
  SEC fee   0.0000206 of the trade value, on sells only.
  FINRA TAF 0 a share (the schedule's figure for this account type), on sells only.
  CAT fee   0.000003 USD a share, on buys and sells.
Each fee type is summed per day for the account, rounded UP to the cent, and shared among that day's orders in proportion to each
order's unrounded fee of that type, so the orders add up to Alpaca's daily figure. One account here is one call of
`daily_charged_fees` (run_group, or buy_hold_equity, is one account).

The arithmetic is in Decimal: a float ceiling turns 10000 * 0.000003 = 0.030000000000000002 into 0.04, where Alpaca's 0.03 stays 0.03.
Each float input enters as Decimal(repr(float)), its shortest exact decimal text.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, Decimal, localcontext

# The three rates are frozen at Alpaca's published figures; provenance in docs/research_lab/parameter_provenance.md under the
# matching `fees.alpaca.*` heading, and the settings rows in dashboard/backend/researchdb.py carry the same values.
SEC_FEE_RATE = Decimal("0.0000206")  # fees.alpaca.sec_fee_rate: fraction of the trade value, sells only
TAF_PER_SHARE = Decimal("0")         # fees.alpaca.taf_per_share: USD a share, sells only
CAT_PER_SHARE = Decimal("0.000003")  # fees.alpaca.cat_per_share: USD a share, buys and sells

CENT = Decimal("0.01")
FEE_TYPES = ("sec", "taf", "cat")
PRECISION = 60  # digits: two 17-digit floats multiplied stay exact


def _number(value, what: str) -> Decimal:
    """The float as the Decimal of its shortest text; ValueError naming the value when it is not a finite number of 0 or more."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) and not hasattr(value, "__float__"):
        raise ValueError("%s must be a finite number of 0 or more, got %r" % (what, value))
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("%s must be a finite number of 0 or more, got %r" % (what, number))
    return Decimal(repr(number))


def order_fee_parts(side: str, shares: float, price: float) -> dict:
    """The unrounded fee of one order by type: {"sec", "taf", "cat"} in Decimal. side is "buy" or "sell"."""
    if side not in ("buy", "sell"):
        raise ValueError('side must be "buy" or "sell", got %r' % (side,))
    n = _number(shares, "shares")
    p = _number(price, "price")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        if side == "sell":
            return {"sec": n * p * SEC_FEE_RATE, "taf": n * TAF_PER_SHARE, "cat": n * CAT_PER_SHARE}
        return {"sec": Decimal(0), "taf": Decimal(0), "cat": n * CAT_PER_SHARE}


def daily_charged_fees(orders: list) -> list:
    """The fee each order is charged, as floats in the orders' order. `orders` is a list of (day, side, shares, price).

    Per day and fee type the unrounded parts are summed, the total is rounded up to the cent, and that rounded total is shared among
    the day's orders in proportion to their unrounded parts (a total of zero shares nothing). An order's fee is its three shares added.
    """
    parts = [order_fee_parts(side, shares, price) for _, side, shares, price in orders]
    by_day: dict = {}
    for i, (day, _, _, _) in enumerate(orders):
        by_day.setdefault(day, []).append(i)
    charged = [Decimal(0)] * len(orders)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for members in by_day.values():
            for kind in FEE_TYPES:
                total = sum((parts[i][kind] for i in members), Decimal(0))
                if total == 0:
                    continue
                rounded = total.quantize(CENT, rounding=ROUND_CEILING)
                for i in members:
                    charged[i] += rounded * parts[i][kind] / total
    return [float(c) for c in charged]


def buy_order_fee(shares: float, price: float) -> float:
    """The charged fee of one buy order alone on its day in its account (FEE1b): the old engines hold at most one order per bar and column."""
    return daily_charged_fees([(0, "buy", shares, price)])[0]


def sell_order_fee(shares: float, price: float) -> float:
    """The charged fee of one sell order alone on its day in its account (FEE1b)."""
    return daily_charged_fees([(0, "sell", shares, price)])[0]
