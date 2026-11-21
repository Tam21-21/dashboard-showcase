"""Entry signals of a theory over a stock group (lane GV2-5a, DECISIONS #440 items 4 and 7).

`entry_signals(theory, picks, frames)` reads only the theory's operands, conditions and entry groups; `exit_signals` the exit groups the same
way (GV3-X); `atr_frame(frames, n)` is ATR(n) per stock for the trailing stop. Each operand is
computed per stock, on the stock's own bars, so a stock never borrows another stock's numbers. A condition is false
wherever either side is NaN. The result is a boolean DataFrame (dates by tickers) shaped like the close frame: True on
the bar where the signal is seen at the close; the order goes in at the next open (simulate.py).

An entry part may hold several groups: any group is enough (OR); inside a group every condition must hold (AND). An
operand's `span` makes a condition that reads it hold on that many extra earlier bars too (CandleInterval in
Dolzhenko's book); a condition's span is the largest span of the operands it reads. The theory's variations are not
applied here: this runs the base operands, one strategy at a time.
"""
from __future__ import annotations

import ast

import pandas as pd

from research_lab.indicators import library, limits
from research_lab.theories.form import EARNINGS_FIELDS

_COMPARE = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def _number(value, picks: dict, what: str):
    """A letter becomes its pick, a number stays. A letter with no pick raises."""
    if isinstance(value, str):
        if value not in picks:
            raise ValueError("pick %r is missing: %s needs a value for it in picks (have: %s)" % (value, what, ", ".join(sorted(picks))))
        return picks[value]
    return value


def _whole(value, what: str) -> int:
    if isinstance(value, bool) or float(value) != int(value):
        raise ValueError("%s must be a whole number, got %r" % (what, value))
    return int(value)


class _Run:
    def __init__(self, theory, picks: dict, frames: dict):
        for field in ("open", "high", "low", "close", "volume"):
            if field not in frames:
                raise ValueError("frames has no %r frame" % field)
        self.theory, self.picks, self.frames = theory, picks, frames
        self.like = frames["close"]
        self.cache: dict = {}
        self.bar = theory.data["theory"].get("bar", "1d")

    def _filled(self, value) -> pd.DataFrame:
        return pd.DataFrame(float(value), index=self.like.index, columns=self.like.columns)

    def operand(self, name: str) -> pd.DataFrame:
        if name in self.cache:
            return self.cache[name]
        table = self.theory.operands[name]
        what = "operand %s" % name
        bar = table.get("bar")
        if bar is not None and _number(bar, self.picks, what) != self.bar:
            raise ValueError("%s reads bar %r but the theory runs on %r: other bar sizes are not supported yet" % (what, bar, self.bar))
        kind = table["kind"]
        if kind == "field":
            if table["field"] not in self.frames:
                if table["field"] in EARNINGS_FIELDS:
                    raise ValueError("%s reads the earnings field %r but the frames have none: the spec needs the key \"earnings\"" % (what, table["field"]))
                raise ValueError("%s reads the field %r but the frames have none" % (what, table["field"]))
            out = self.frames[table["field"]].astype(float)
        elif kind == "constant":
            out = self._filled(_number(table["value"], self.picks, what))
        elif kind == "indicator":
            out = self._indicator(name, table)
        else:
            out = self._formula(name, table["expr"])
        offset = _whole(_number(table.get("offset", 0), self.picks, what + " offset"), what + " offset")
        if offset:
            out = out.shift(offset)
        self.cache[name] = out
        return out

    def _indicator(self, name: str, table: dict) -> pd.DataFrame:
        what = "operand %s" % name
        indicator = table["indicator"]
        params = {}
        for pname, raw in table.get("params", {}).items():
            value = _number(raw, self.picks, what + " param " + pname)
            kind = limits.PARAMS[indicator][pname][0]
            params[pname] = _whole(value, "%s param %s" % (what, pname)) if kind == "int" else float(value)
        output = table.get("output")
        columns = {}
        for ticker in self.like.columns:
            close = self.frames["close"][ticker]
            first, last = close.first_valid_index(), close.last_valid_index()
            if first is None:
                columns[ticker] = pd.Series(float("nan"), index=self.like.index)
                continue
            part = {f: self.frames[f][ticker].loc[first:last] for f in ("open", "high", "low", "close", "volume")}
            result = self._call(indicator, part, params)
            if isinstance(result, pd.DataFrame):
                result = result[output]
            columns[ticker] = result.reindex(self.like.index)
        return pd.DataFrame(columns, index=self.like.index, columns=self.like.columns).astype(float)

    @staticmethod
    def _call(indicator: str, part: dict, params: dict):
        if indicator in ("sma", "ema", "rsi", "roc"):
            return getattr(library, indicator)(part["close"], **params)
        if indicator == "bollinger":
            return library.bollinger(part["close"], **params)
        if indicator == "macd":
            return library.macd(part["close"], **params)
        if indicator == "atr":
            return library.atr(part["high"], part["low"], part["close"], **params)
        if indicator == "stochastic":
            return library.stochastic(part["high"], part["low"], part["close"], **params)
        if indicator == "donchian":
            return library.donchian(part["high"], part["low"], **params)
        if indicator == "volume_ratio":
            return library.volume_ratio(part["volume"], **params)
        if indicator == "constant":
            return library.constant(part["close"].index, **params)
        raise ValueError("indicator %r has no input rule in theory_run" % indicator)

    def _formula(self, name: str, expr: str) -> pd.DataFrame:
        def walk(node):
            if isinstance(node, ast.Expression):
                return walk(node.body)
            if isinstance(node, ast.BinOp):
                left, right = walk(node.left), walk(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
            if isinstance(node, ast.UnaryOp):
                inner = walk(node.operand)
                return -inner if isinstance(node.op, ast.USub) else inner
            if isinstance(node, ast.Constant):
                return float(node.value)
            if isinstance(node, ast.Name):
                return self.operand(node.id)
            raise ValueError("operand %s: formula %r uses something other than numbers, operand names and + - * /" % (name, expr))

        out = walk(ast.parse(expr, mode="eval"))
        if not isinstance(out, pd.DataFrame):
            out = self._filled(out)
        return out.replace([float("inf"), float("-inf")], float("nan"))

    def side(self, side):
        kind, value = side
        if kind == "operand":
            what = "operand %s span" % value
            span = _whole(_number(self.theory.operands[value].get("span", 0), self.picks, what), what)
            return self.operand(value), span
        if kind == "letter":
            return float(_number(value, self.picks, "a condition")), 0
        return float(value), 0

    def condition(self, row) -> pd.DataFrame:
        left_side, op, right_side, _part = row
        left, span_l = self.side(left_side)
        right, span_r = self.side(right_side)
        if op in _COMPARE:
            hit = _COMPARE[op](left, right)
        else:
            lo = left if isinstance(left, pd.DataFrame) else self._filled(left)
            ro = right if isinstance(right, pd.DataFrame) else self._filled(right)
            now = (lo > ro) if op == "crosses_above" else (lo < ro)
            before = (lo.shift(1) <= ro.shift(1)) if op == "crosses_above" else (lo.shift(1) >= ro.shift(1))
            hit = now & before
        known = pd.DataFrame(True, index=self.like.index, columns=self.like.columns)
        for side in (left, right):
            if isinstance(side, pd.DataFrame):
                known &= side.notna()
        hit = (hit & known).astype(bool)
        out = hit.copy()
        for back in range(1, max(span_l, span_r) + 1):
            out &= hit.shift(back, fill_value=False)
        return out


def group_signals(theory, picks: dict, frames: dict, groups: list) -> pd.DataFrame:
    """Boolean DataFrame (dates by tickers): True where any of the groups has every one of its conditions true."""
    run = _Run(theory, picks, frames)
    result = pd.DataFrame(False, index=run.like.index, columns=run.like.columns)
    for group in groups:
        inside = pd.DataFrame(True, index=run.like.index, columns=run.like.columns)
        for row in group:
            inside &= run.condition(row)
        result |= inside
    return result.astype(bool)


def entry_signals(theory, picks: dict, frames: dict) -> pd.DataFrame:
    """Boolean DataFrame (dates by tickers): True where any entry group has every one of its conditions true."""
    return group_signals(theory, picks, frames, theory.entry_groups)


def exit_signals(theory, picks: dict, frames: dict) -> pd.DataFrame:
    """The same for the exit groups: True on the bar where a sell is seen at the close (the sale is made at the next open, simulate.py)."""
    return group_signals(theory, picks, frames, theory.exit_groups)


def atr_frame(frames: dict, n: int) -> pd.DataFrame:
    """ATR(n) of every stock (dates by tickers), computed per stock on its own bars as _Run._indicator cuts them: from its first to its last close."""
    for field in ("high", "low", "close"):
        if field not in frames:
            raise ValueError("frames has no %r frame" % field)
    like = frames["close"]
    columns = {}
    for ticker in like.columns:
        close = frames["close"][ticker]
        first, last = close.first_valid_index(), close.last_valid_index()
        if first is None:
            columns[ticker] = pd.Series(float("nan"), index=like.index)
            continue
        part = {f: frames[f][ticker].loc[first:last] for f in ("high", "low", "close")}
        columns[ticker] = library.atr(part["high"], part["low"], part["close"], n=n).reindex(like.index)
    return pd.DataFrame(columns, index=like.index, columns=like.columns).astype(float)
