"""Indicator library of generator v2 (lane GV2-2, DECISIONS #440 items 4 and 7).

Every function calls limits.check_params first, takes pandas objects and returns pandas objects on the
input's index, NaN until enough bars. Nothing here reads a later bar: each value uses bars up to and
including its own, except donchian and volume_ratio, which by definition use only the bars before today.

Where the frozen vbt (vectorbtpro, b1727ac6, inside the research-runner images only) reproduces a formula
with explicit arguments, vbt computes it: sma, ema, rsi, macd. The rest is own pandas code: stochastic (vbt's
%K came out 3e-14 below 0 on the fixture, outside its 0 to 100 range), bollinger (vbt's rolling standard deviation leaves a noise of
about 1e-5 on a flat window, so its bands never meet and the zero-width rule cannot fire), atr (vbt's first
bar differs), roc, donchian, volume_ratio, constant. vbt is imported inside the functions that need it,
so this module imports on any machine with pandas.

Series in, Series out for single-output indicators (sma, ema, rsi, atr, roc, volume_ratio also take a
DataFrame, column by column). Indicators with several outputs (bollinger, stochastic, macd, donchian)
take Series and return a DataFrame whose columns are the output names in limits.OUTPUTS.
"""
from __future__ import annotations

import pandas as pd

from . import limits


def _vbt():
    import vectorbtpro as vbt  # noqa: PLC0415 - only the research-runner images carry vbt
    # GV2-8a: vbt's cache registry keeps every run's Index and ArrayWrapper alive (about 0.37 MB a strategy, measured
    # 2026-10-14), which ran the size-10,000 search out of memory at the 4 GiB cap. The theory path never reuses a cached
    # result, so the machinery is off from the first vbt call; results are bit-identical on and off (probe r12).
    vbt.settings["caching"]["disable_machinery"] = True
    return vbt


def _need_series(indicator: str, **named) -> None:
    for pname, obj in named.items():
        if not isinstance(obj, pd.Series):
            raise TypeError("%s: input '%s' must be a pandas Series, got %s" % (indicator, pname, type(obj).__name__))


def _same_index(indicator: str, **named) -> None:
    first_name, first = next(iter(named.items()))
    for pname, obj in named.items():
        if not obj.index.equals(first.index):
            raise ValueError("%s: inputs '%s' and '%s' do not share one index" % (indicator, first_name, pname))


def _back(out, like):
    """Give a vbt result the input's shape and labels: a Series for a Series, the input's columns for a DataFrame."""
    if isinstance(like, pd.Series):
        if isinstance(out, pd.DataFrame):
            if out.shape[1] != 1:
                raise ValueError("expected one column from vbt, got %d" % out.shape[1])
            out = out.iloc[:, 0]
        return pd.Series(out.to_numpy(), index=like.index, name=like.name)
    return pd.DataFrame(out.to_numpy(), index=like.index, columns=like.columns)


def sma(x, n: int):
    limits.check_params("sma", {"n": n})
    return _back(_vbt().MA.run(x, window=n, wtype="simple").ma, x)


def ema(x, n: int):
    limits.check_params("ema", {"n": n})
    return _back(_vbt().MA.run(x, window=n, wtype="exp").ma, x)


def rsi(close, n: int):
    limits.check_params("rsi", {"n": n})
    # Planning ruling: G and L both 0 gives NaN, L = 0 with G above 0 gives 100; vbt does exactly that.
    return _back(_vbt().RSI.run(close, window=n, wtype="wilder").rsi, close)


def bollinger(close, n: int, w):
    limits.check_params("bollinger", {"n": n, "w": w})
    _need_series("bollinger", close=close)
    middle = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    upper = middle + w * sd
    lower = middle - w * sd
    span = upper - lower
    pct_b = ((close - lower) / span).where(upper != lower)
    width = (span / middle).where(middle != 0)
    return pd.DataFrame({"middle": middle, "upper": upper, "lower": lower, "pct_b": pct_b, "width": width})


def atr(high, low, close, n: int):
    limits.check_params("atr", {"n": n})
    _need_series("atr", high=high, low=low, close=close)
    _same_index("atr", high=high, low=low, close=close)
    prev = close.shift(1)
    parts = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1)
    true_range = parts.max(axis=1, skipna=True).where(parts.iloc[:, 0].notna())
    return true_range.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def stochastic(high, low, close, k: int, d: int):
    limits.check_params("stochastic", {"k": k, "d": d})
    _need_series("stochastic", high=high, low=low, close=close)
    _same_index("stochastic", high=high, low=low, close=close)
    lowest = low.rolling(k).min()
    highest = high.rolling(k).max()
    span = highest - lowest
    pct_k = (100.0 * (close - lowest) / span).where(span != 0)
    return pd.DataFrame({"k": pct_k, "d": pct_k.rolling(d).mean()})


def macd(close, fast: int, slow: int, signal: int):
    limits.check_params("macd", {"fast": fast, "slow": slow, "signal": signal})
    _need_series("macd", close=close)
    res = _vbt().MACD.run(close, fast_window=fast, slow_window=slow, signal_window=signal,
                          macd_wtype="exp", signal_wtype="exp")
    line = _back(res.macd, close)
    sig = _back(res.signal, close)
    return pd.DataFrame({"line": line, "signal": sig, "hist": line - sig})


def roc(close, n: int):
    limits.check_params("roc", {"n": n})
    return 100.0 * (close / close.shift(n) - 1.0)


def donchian(high, low, n: int):
    limits.check_params("donchian", {"n": n})
    _need_series("donchian", high=high, low=low)
    _same_index("donchian", high=high, low=low)
    return pd.DataFrame({
        "upper": high.shift(1).rolling(n).max(),
        "lower": low.shift(1).rolling(n).min(),
    })


def volume_ratio(volume, n: int):
    limits.check_params("volume_ratio", {"n": n})
    mean_before = volume.shift(1).rolling(n).mean()
    return volume / mean_before.where(mean_before != 0)


def constant(index, value):
    limits.check_params("constant", {"value": value})
    return pd.Series(float(value), index=index)
