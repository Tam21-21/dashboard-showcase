"""Moving-average crossover (inline-parameter reference, M5 C)

The reference INLINE-parameter strategy: identical MA-cross logic to `ma_cross`, but the two windows are marked
sweepable INLINE with `self.param` instead of declared as a class `sweep_params` tuple plus a `sweep_signals`
override. It is the M5 C reference and the reproduce-probe subject. Dispatched over a universe, the engine runs
`run()` once per (combination x ticker); run as a plain single backtest (or at the default combo), `self.param`
returns the defaults (fast 5, slow 20), so it is an ordinary single-run strategy when not swept.

## Rules

Identical to `ma_cross`: go long when the fast SMA crosses above the slow SMA, exit when it crosses back, execute
at the next bar's open, read the neutral `default` fee and slippage keys, start with $10,000. The ONLY difference
is HOW the windows are declared: inline, where they are used, rather than as a separate vectorized contract.

## Parameters

`fast_window` (default 5), `slow_window` (default 20), both marked sweepable with `self.param`. No class
`sweep_params` and no `sweep_signals` override, so the engine runs the inline per-combo path (M5 C).

## Scope

Class-agnostic (crypto or equities), like `ma_cross`: a moving-average cross reads the neutral cost keys and runs
cost-correctly on either class.
"""
from __future__ import annotations  # lazy annotations

import vectorbtpro as vbt  # the backtest engine; only strategy code imports it

from strategies.base import RunResult, SettingRow, Strategy, StrategyInput  # contract surface
from strategies.helpers import default_costs, long_only_signal_result  # neutral-key cost read + the shared price build

INIT_CASH = 10_000  # starting cash in $ (L14; not a cost, so not a settings lookup)


class MaCrossInline(Strategy):
    """Long-only fast/slow moving-average crossover with INLINE-swept windows (M5 C reference)."""

    # Class-agnostic by design, like ma_cross: it reads the neutral cost keys and runs cost-correctly on either
    # class, so it declares BOTH (the dispatch guard then never falsely refuses a legitimate run).
    asset_class_scope = ("crypto", "equities")

    def run(self, data: StrategyInput, settings: list[SettingRow]) -> RunResult:
        """Run the crossover backtest and return a complete RunResult.

        The windows are marked sweepable INLINE: ``self.param`` returns the swept combo's value when this run is
        part of an inline sweep, else the default (fast 5, slow 20). Everything else is identical to
        ``ma_cross.run`` — so a single-combo inline run reproduces a hardcoded 5/20 run exactly (the reproduce gate)."""
        fast_window = self.param("fast_window", 5, type="int")   # sweepable inline; defaults to 5 on a normal run
        slow_window = self.param("slow_window", 20, type="int")  # sweepable inline; defaults to 20 on a normal run
        close = data.close  # closing price series the SMAs are built from

        fast_sma = vbt.MA.run(close, window=fast_window).ma  # fast SMA series
        slow_sma = vbt.MA.run(close, window=slow_window).ma  # slow SMA series
        entries = fast_sma.vbt.crossed_above(slow_sma)  # enter long on the upward cross
        exits = fast_sma.vbt.crossed_below(slow_sma)  # exit on the downward cross

        fees, slippage = default_costs(settings)  # (fees, slippage) from the neutral 'default' keys (M4 D2)
        return long_only_signal_result(
            data, entries, exits, fees, slippage,  # signals + the neutral-key costs
            indicators={"fast_sma": fast_sma, "slow_sma": slow_sma},  # the two SMAs for the chart overlay (L4)
            init_cash=INIT_CASH,  # $10,000 starting cash (L14)
        )
