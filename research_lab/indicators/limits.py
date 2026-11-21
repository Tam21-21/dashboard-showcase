"""Hard limits of the indicator library (lane GV2-2, DECISIONS #440 items 4 and 7).

Pure Python, standard library only: no pandas, no vbt. For every indicator this module declares its
parameters (name, type, hard minimum, whether the minimum itself is allowed), its logic rules and the range
of each output, so a theory can never ask for an impossible indicator (an RSI of window 1, a MACD whose fast
window is not below its slow window, an RSI line of 105).

There are no upper limits on parameters: sensible ranges belong to the ranges file (lane GV2-3).
"""
from __future__ import annotations

import math


class IndicatorParamError(ValueError):
    """A parameter set no indicator can honour. The message names the indicator, the parameter, the value and the limit."""


class IndicatorLogicRuleError(IndicatorParamError):
    """A broken logic rule between two parameters (macd: fast must be below slow). A grid drops such pairs; it is no range fault."""


class IndicatorThresholdError(ValueError):
    """A threshold outside what an indicator output can ever be. The message names the indicator, the output, the value and the range."""


# A parameter spec: (kind, minimum, inclusive). kind is "int" or "float"; minimum None means no limit.
_WINDOW2 = ("int", 2, True)
_WINDOW1 = ("int", 1, True)

PARAMS = {
    "sma": {"n": _WINDOW2},
    "ema": {"n": _WINDOW2},
    "rsi": {"n": _WINDOW2},
    "bollinger": {"n": _WINDOW2, "w": ("float", 0, False)},
    "atr": {"n": _WINDOW2},
    "stochastic": {"k": _WINDOW2, "d": _WINDOW1},
    "macd": {"fast": _WINDOW2, "slow": _WINDOW2, "signal": _WINDOW2},
    "roc": {"n": _WINDOW1},
    "donchian": {"n": _WINDOW1},
    "volume_ratio": {"n": _WINDOW1},
    "constant": {"value": ("float", None, True)},
}

# Logic rules between parameters: (indicator, smaller parameter, larger parameter), the smaller strictly below.
RULES = {
    "macd": [("fast", "slow")],
}

# Output ranges: (low, high), None meaning unbounded on that side. Both ends are inclusive.
OUTPUTS = {
    "sma": {"value": (None, None)},
    "ema": {"value": (None, None)},
    "rsi": {"rsi": (0, 100)},
    "bollinger": {
        "middle": (None, None), "upper": (None, None), "lower": (None, None),
        "pct_b": (None, None), "width": (0, None),
    },
    "atr": {"value": (None, None)},
    "stochastic": {"k": (0, 100), "d": (0, 100)},
    "macd": {"line": (None, None), "signal": (None, None), "hist": (None, None)},
    "roc": {"value": (None, None)},
    "donchian": {"upper": (None, None), "lower": (None, None)},
    "volume_ratio": {"value": (0, None)},
    "constant": {"value": (None, None)},
}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_real(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _known(name: str, table: dict, error):
    if name not in table:
        raise error("indicator '%s' is not in the library; known indicators: %s" % (name, ", ".join(sorted(table))))
    return table[name]


def check_params(name: str, params: dict) -> None:
    """Refuse a parameter set that breaks a hard limit or a logic rule of indicator `name`."""
    spec = _known(name, PARAMS, IndicatorParamError)
    for pname in params:
        if pname not in spec:
            raise IndicatorParamError("%s: unknown parameter '%s'; it takes: %s" % (name, pname, ", ".join(spec)))
    for pname, (kind, minimum, inclusive) in spec.items():
        if pname not in params:
            raise IndicatorParamError("%s: parameter '%s' is missing" % (name, pname))
        value = params[pname]
        if kind == "int" and not _is_int(value):
            raise IndicatorParamError("%s: parameter '%s' = %r must be a whole number (int)" % (name, pname, value))
        if kind == "float" and not _is_real(value):
            raise IndicatorParamError("%s: parameter '%s' = %r must be a number" % (name, pname, value))
        if isinstance(value, float) and not math.isfinite(value):
            raise IndicatorParamError("%s: parameter '%s' = %r must be a finite number" % (name, pname, value))
        if minimum is None:
            continue
        if inclusive and value < minimum:
            raise IndicatorParamError(
                "%s: parameter '%s' = %r is below the hard minimum, it must be at least %s" % (name, pname, value, minimum))
        if not inclusive and value <= minimum:
            raise IndicatorParamError(
                "%s: parameter '%s' = %r is at or below the hard limit, it must be above %s" % (name, pname, value, minimum))
    for small, large in RULES.get(name, []):
        if not params[small] < params[large]:
            raise IndicatorLogicRuleError(
                "%s: logic rule broken, %s = %r must be below %s = %r" % (name, small, params[small], large, params[large]))


def check_threshold(name: str, output: str, value) -> None:
    """Refuse a threshold `value` that output `output` of indicator `name` can never reach."""
    outputs = _known(name, OUTPUTS, IndicatorThresholdError)
    if output not in outputs:
        raise IndicatorThresholdError(
            "%s: unknown output '%s'; it has: %s" % (name, output, ", ".join(outputs)))
    if not _is_real(value) or not math.isfinite(value):
        raise IndicatorThresholdError("%s: output '%s' threshold %r must be a finite number" % (name, output, value))
    low, high = outputs[output]
    if low is not None and value < low:
        raise IndicatorThresholdError(
            "%s: output '%s' threshold %r is below the lowest value it can take, %s (range %s to %s)"
            % (name, output, value, low, low, "no limit" if high is None else high))
    if high is not None and value > high:
        raise IndicatorThresholdError(
            "%s: output '%s' threshold %r is above the highest value it can take, %s (range %s to %s)"
            % (name, output, value, high, "no limit" if low is None else low, high))


_LOOKBACK = {
    "sma": lambda p: p["n"],
    "ema": lambda p: p["n"],
    "rsi": lambda p: p["n"] + 1,
    "bollinger": lambda p: p["n"],
    "atr": lambda p: p["n"],
    "stochastic": lambda p: p["k"] + p["d"] - 1,
    "macd": lambda p: p["slow"] + p["signal"] - 1,
    "roc": lambda p: p["n"] + 1,
    "donchian": lambda p: p["n"] + 1,
    "volume_ratio": lambda p: p["n"] + 1,
    "constant": lambda p: 1,
}


def lookback_bars(name: str, params: dict) -> int:
    """The bars indicator `name` needs, at `params`, before its first value: the position of its first non-NaN bar, plus 1.

    One closed formula per indicator, each read off library.py's own computation (probe g9 of lane GV2-7a holds every formula to
    the library's own count of leading NaN bars on a synthetic series, so the table cannot drift). For an indicator with several
    outputs this is the longest one. A parameter set check_params refuses raises IndicatorParamError.
      sma, ema:      n                   vbt MA: the first value comes once n bars are in.
      rsi:           n + 1               vbt RSI, wilder: the first change needs 2 bars, then n changes.
      bollinger:     n                   close.rolling(n) mean and standard deviation.
      atr:           n                   true range exists from bar 1 (high - low); ewm with min_periods=n.
      stochastic:    k + d - 1           %K is over a rolling(k) range; %D is rolling(d) of %K.
      macd:          slow + signal - 1   the line needs slow bars; the signal line is an ewm of it with min_periods=signal.
      roc:           n + 1               close.shift(n) is NaN on the first n bars.
      donchian:      n + 1               high.shift(1).rolling(n): the shift loses 1 bar.
      volume_ratio:  n + 1               volume.shift(1).rolling(n): the shift loses 1 bar.
      constant:      1                   a value from the first bar.
    """
    check_params(name, params)
    if set(_LOOKBACK) != set(PARAMS):
        raise IndicatorParamError("lookback_bars has no formula for: %s" % ", ".join(sorted(set(PARAMS) - set(_LOOKBACK))))
    return int(_LOOKBACK[name](params))
