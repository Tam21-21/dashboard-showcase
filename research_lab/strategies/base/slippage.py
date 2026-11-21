"""The direct per-fill execution-shortfall measure (M3.5 slippage-measurement brief).

ONE definition of ``total_slippage_cost``: the gross sum, over every fill the run actually
made, of the filled size times the absolute gap between the executed price and the clean
reference price (the un-slippaged basis the run used for that order). This replaces the M3
two-pass terminal-equity difference (DECISIONS #66/#67): there is one run, so there is nothing
to diverge, and a partial fill contributes only its filled portion (SR1, SR2, SC1, SC4).

Two reference SOURCES feed the one formula, because slippage lives in different places on the
two run paths:

  - PRICE path: the engine applied the slippage, so the order records carry the post-slippage
    executed price and the clean reference is recovered exactly from the rate
    (``price_path_slippage_cost`` below).
  - EVENT path: the engine saw ``slippage=0`` (the fill is baked into an explicit price), so the
    clean reference lives only in the fill model; the event path builds its (size, executed,
    reference) sequences itself and calls ``per_fill_slippage_cost`` directly (event_pipeline.py).

Import-light: pure arithmetic over sequences, no top-level vbt or pandas import (it reads only
attributes off a ``pf`` passed in), so it stays importable wherever a backtest runs — matching the
``vbt_extraction`` discipline. SF1 (a size-dependent square-root impact model, Bouchaud §6.6) and
SF2 (a per-asset/per-venue rate) plug in by changing how one fill's cost or reference is computed,
NOT by changing where the run sums it: the summation and the reference source stay.
"""
from __future__ import annotations  # lazy annotations, consistent with the rest of base/

import math  # finite-check the rate without importing numpy (keeps this module import-light)


def per_fill_slippage_cost(
    filled_sizes,  # per-fill filled size magnitudes (>= 0); a sequence aligned to the other two
    executed_prices,  # per-fill executed (post-slippage) prices, same order
    reference_prices,  # per-fill clean reference prices (the un-slippaged basis), same order
) -> float:
    """The one formula: sum over fills of ``filled_size * abs(executed - reference)``.

    Both run paths call this with their own three sequences; neither restates the formula (the
    single source of truth for what the metric means). A buy that fills above its reference and a
    sell that fills below its reference both contribute a POSITIVE cost (the ``abs`` makes the
    direction-agnostic shortfall, SR1). Returns a plain Python float (0.0 for no fills, SR7), so it
    serializes cleanly to stats.json and binds to the ``REAL NOT NULL`` column.
    """
    n = len(filled_sizes)  # the fill count drives the equal-length contract
    if len(executed_prices) != n or len(reference_prices) != n:  # a mismatch would silently truncate under zip
        raise ValueError(  # no silent truncation — fail loud (the no-fallback rule)
            f"per-fill sequences must be equal length, got sizes={n} "
            f"executed={len(executed_prices)} references={len(reference_prices)}"
        )
    total = 0.0  # accumulate the gross per-fill shortfall
    for size, executed, reference in zip(filled_sizes, executed_prices, reference_prices):  # one fill at a time
        total += float(size) * abs(float(executed) - float(reference))  # filled size * per-share shortfall
    return total  # the run's total slippage cost (a plain float; 0.0 when there are no fills)


def price_path_slippage_cost(pf, rate: float) -> float:
    """The PRICE-path cost: recover each order's clean reference from the engine rate, then sum.

    The engine applied slippage as a uniform multiplier, so ``executed = reference * (1 + s*rate)``
    with the side sign ``s`` (+1 for a buy, -1 for a sell, DECISIONS #67). The order records expose
    the filled size (``pf.orders.size``, a positive magnitude), the executed price
    (``pf.orders.price``), and the side as a 0/1 int (``pf.orders.side``: 0 = Buy, 1 = Sell — NOT
    +/-1), so the clean reference is recovered exactly as ``executed / (1 + s*rate)`` where
    ``s = 1 - 2*side``. At a zero rate the recovery collapses to ``reference == executed`` and the
    cost is 0.0 (SC1). ``rate`` is the run's slippage rate, passed in by the strategy (read from its
    settings), so this helper does not guess the scope/key and a later per-asset/per-venue split
    (SF2) passes a per-fill rate without changing the formula.
    """
    r = float(rate)  # coerce any numeric (Python float, numpy float32/64, int) to a plain float, then validate
    if not (math.isfinite(r) and 0.0 <= r < 1.0):  # NaN/inf or a slippage >= 100% breaks the recovery; fail loud
        raise ValueError(f"slippage rate must be a finite fraction in [0, 1), got {rate!r}")
    sizes = pf.orders.size.values  # the filled-size magnitudes (SR2: what actually filled, not requested)
    executed = pf.orders.price.values  # the post-slippage executed prices
    sides = pf.orders.side.values  # 0 = Buy, 1 = Sell (the engine's OrderSide encoding)
    # Recover the clean reference per order: s = +1 for a buy (side 0), -1 for a sell (side 1).
    references = [float(e) / (1.0 + (1.0 - 2.0 * float(sd)) * r) for e, sd in zip(executed, sides)]
    return per_fill_slippage_cost(sizes, executed, references)  # the one formula does the summation
