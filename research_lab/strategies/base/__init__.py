"""The strategy contract surface.

Re-exports the engine-neutral contract types and settings helpers so consumers
write ``from strategies.base import Strategy, RunResult`` (M3 spec L15). Every
symbol here is pure Python or lazily-annotated, so importing ``strategies.base``
pulls in neither vbt nor pandas. The vbt-dependent extraction helper
(``vbt_extraction.build_run_result``) is intentionally NOT re-exported — import
it directly from the strategy code that runs a backtest.
"""
from strategies.base.event_strategy import EventStrategy  # the M3.5 event-driven base (EL47)
from strategies.base.run_result import RunResult  # the locked 6-slot output type
from strategies.base.settings import (  # typed settings rows + read/shadow helpers
    SettingRow,
    clone_with_zero_slippage,
    get_setting,
)
from strategies.base.strategy import Strategy, StrategyInputError  # the abstract base + the V7 input-rejection error
from strategies.base.strategy_input import StrategyInput  # the locked input type
from strategies.base.sweep import (  # M5 1B (§4): the engine-neutral wide-sweep contract types + canonical helpers
    EmptyWindowedUniverse,
    SweepInput,
    SweepNotSupported,
    SweepParam,
    canonical_combo_order,
    canonical_param_json,
    coerce_param_value,
)

# Explicit public surface of the contract package.
__all__ = [
    "Strategy",  # ABC every strategy subclasses
    "StrategyInputError",  # raised by validate_input to reject a run's input (V7)
    "EventStrategy",  # M3.5 event-driven base (subclass of Strategy)
    "RunResult",  # output container
    "StrategyInput",  # input container
    "SettingRow",  # one typed setting
    "get_setting",  # read a setting value by (scope, key)
    "clone_with_zero_slippage",  # zero numeric slippage rows for the shadow run
    "SweepInput",  # M5 1B: wide-frame sweep input (close/open/high/low, columns = tickers)
    "SweepParam",  # M5 1B: one sweepable-parameter declaration (name + type)
    "SweepNotSupported",  # M5 1B: raised when a non-sweepable strategy is swept
    "EmptyWindowedUniverse",  # M5 B4: raised when every member has <2 bars in the test window (AD-UU-3 P-3)
    "canonical_combo_order",  # M5 1B: the deterministic combo sort (variation_id source)
    "canonical_param_json",  # M5 1B: the canonical per-combo JSON (param_config_hash source)
    "coerce_param_value",  # M5 1B: grid-value -> declared kind coercion
]
