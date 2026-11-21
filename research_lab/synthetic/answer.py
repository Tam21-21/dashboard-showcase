"""The answer sheet (lane GV2-1, DECISIONS #440 item 8): which days signal and what each trade returns.

Written before any search runs, from the data files and manifest.json alone. This module is plain pandas on
purpose: it shares no code with the pipeline or with research_lab/indicators (lane GV2-2), so a mistake in
one cannot hide a mistake in the other.

Rules (plan docs/lanes/GV2-1.md, step 7):
  middle = mean of the last N closes including today; sd = population standard deviation (ddof 0) of the same closes;
  lower = middle - W * sd; volume ratio = today's volume / mean volume of the N sessions before today;
  RSI = 100 - 100 / (1 + G / L) with G and L the ewm(alpha=1/R, adjust=False, min_periods=R) means of the
  close-to-close gains and losses (L = 0 gives 100).
  A signal day is close < lower, volume ratio >= k and RSI < L_RSI together.
  Entry at the next session's open; the trade spans H sessions counting the entry session and exits at the close of
  its last; one trade per code at a time; return = exit / entry - 1, no costs.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

WINDOWS = {
    "screen": ("2004-01-02", "2016-12-30"),
    "optimise": ("2017-01-03", "2019-12-31"),
    "walk_forward": ("2020-01-02", "2022-12-30"),
    "reserve": ("2023-01-03", "2026-06-30"),
}
TRAP_KINDS = ("quiet_slide", "up_spike", "slow_grind")
SHEET_COLUMNS = ["code", "group", "signal_date", "kind", "entry_date", "entry_price",
                 "exit_date", "exit_price", "return", "skipped"]


def lower_band(close: pd.Series, n: int, w: float) -> pd.Series:
    middle = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return middle - w * sd


def volume_ratio(volume: pd.Series, n: int) -> pd.Series:
    return volume / volume.shift(1).rolling(n).mean()


def rsi(close: pd.Series, r: int) -> pd.Series:
    change = close.diff()
    gains = change.clip(lower=0)
    losses = (-change).clip(lower=0)
    g = gains.ewm(alpha=1.0 / r, adjust=False, min_periods=r).mean()
    lo = losses.ewm(alpha=1.0 / r, adjust=False, min_periods=r).mean()
    out = 100.0 - 100.0 / (1.0 + g / lo)
    out[(lo == 0) & g.notna()] = 100.0
    return out


def signal_days(frame: pd.DataFrame, n: int, w: float, k: float, r: int, l_rsi: float) -> pd.Series:
    """True on every session where close < lower band, volume ratio >= k and RSI < l_rsi (NaN counts as not met)."""
    close = frame["close"].astype(float).reset_index(drop=True)
    volume = frame["volume"].astype(float).reset_index(drop=True)
    return (close < lower_band(close, n, w)) & (volume_ratio(volume, n) >= k) & (rsi(close, r) < l_rsi)


def trades_for(frame: pd.DataFrame, flags: pd.Series, hold: int) -> list:
    """One dict per signal day: a trade, or a skipped note. One trade per code at a time."""
    rows = []
    last_exit = -1
    dates = list(frame["date"])
    opens = frame["open"].astype(float).tolist()
    closes = frame["close"].astype(float).tolist()
    for s in np.flatnonzero(flags.to_numpy()):
        s = int(s)
        row = {"signal_index": s, "signal_date": dates[s]}
        if s <= last_exit:
            row["skipped"] = "skipped: in trade"
        elif s + hold >= len(dates):
            row["skipped"] = "skipped: data ends before the exit"
        else:
            entry, exit_ = s + 1, s + hold
            row.update(entry_date=dates[entry], entry_price=opens[entry], exit_date=dates[exit_],
                       exit_price=closes[exit_], ret=closes[exit_] / opens[entry] - 1.0, skipped="")
            last_exit = exit_
        rows.append(row)
    return rows


def _kind_for(index: int, events: list) -> str:
    for ev in events:
        if ev["kind"] == "dropped":
            continue
        if ev["start_index"] <= index <= ev["end_index"]:
            return ev["kind"]
    return "stray"


def _window_of(date: str) -> "str | None":
    for name, (lo, hi) in WINDOWS.items():
        if lo <= date <= hi:
            return name
    return None


def build_sheet(data_dir: Path) -> "tuple[pd.DataFrame, dict]":
    manifest = json.loads((data_dir / "manifest.json").read_text())
    d = manifest["design"]
    records = []
    for code in sorted(manifest["codes"]):
        info = manifest["codes"][code]
        frame = pd.read_parquet(data_dir / f"{code}.parquet")
        flags = signal_days(frame, d["N"], d["W"], d["k"], d["R"], d["L"])
        for t in trades_for(frame, flags, d["H"]):
            records.append({
                "code": code, "group": info["group"], "signal_date": t["signal_date"],
                "kind": _kind_for(t["signal_index"], info["events"]),
                "entry_date": t.get("entry_date", ""), "entry_price": t.get("entry_price", ""),
                "exit_date": t.get("exit_date", ""), "exit_price": t.get("exit_price", ""),
                "return": t.get("ret", ""), "skipped": t["skipped"],
            })
    sheet = pd.DataFrame(records, columns=SHEET_COLUMNS)
    groups = sorted({info["group"] for info in manifest["codes"].values()})
    return sheet, _summary(sheet, groups)


def _summary(sheet: pd.DataFrame, groups: list) -> dict:
    out: dict = {}
    windowed = sheet.assign(window=sheet["signal_date"].map(_window_of))
    for group in groups:
        out[group] = {}
        for window in WINDOWS:
            part = windowed[(windowed["group"] == group) & (windowed["window"] == window)]
            done = part[part["skipped"] == ""]
            ret = pd.to_numeric(done["return"])
            planted = ret[done["kind"] == "planted"]
            others = ret[done["kind"] != "planted"]
            entry = {
                "signals": int(len(part)), "trades": int(len(done)),
                "planted": int((part["kind"] == "planted").sum()),
                "twin_drop": int((part["kind"] == "twin_drop").sum()),
                "strays": int((part["kind"] == "stray").sum()),
                "mean_return_planted": None if planted.empty else float(planted.mean()),
                "mean_return_others": None if others.empty else float(others.mean()),
            }
            for kind in TRAP_KINDS:
                entry[kind] = int((part["kind"] == kind).sum())
            out[group][window] = entry
    return out


def write_answer(data_dir: Path) -> None:
    sheet, summary = build_sheet(data_dir)
    sheet.to_csv(data_dir / "answer.csv", index=False)
    (data_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


# ---- Version 2 (lane GV3-D1, DECISIONS #467 item 9): the four rebound theories, costs, verdicts ----------------------

THEORIES = ("bollinger_rsi", "donchian_rsi", "bollinger_stochastic", "donchian_stochastic")
THEORY_COLUMNS = ["theory", "code", "group", "signal_date", "kind", "entry_date", "entry_price",
                  "exit_date", "exit_price", "return", "skipped"]
SPANS = {"search": ("2004-01-02", "2019-12-31"), "later": ("2020-01-02", "2026-06-30")}
# Alpaca fees plus 0.03% slippage each way (DECISIONS #467 item 7; the fee constants are #460's, copied).
SLIPPAGE = 0.0003
BUY_FEE = 0.000003
SELL_FEE_RATE = 0.0000206
SELL_FEE_FLAT = 0.000003
# Dolzhenko pdfp 89: over 100 trades and over 60% winners.
VERDICT_MIN_TRADES = 100
VERDICT_MIN_WIN_RATE = 0.60
SIX = 6
# Lane GV3-D2 (DECISIONS #467 item 8): the average-crossover template, fast average above slow in, out on the cross back.
TREND_THEORIES = ("sma_cross", "ema_cross")


def donchian_low(low: pd.Series, n: int) -> pd.Series:
    """Lowest low of the n sessions before today (the library reads only bars before today)."""
    return low.shift(1).rolling(n).min()


def stochastic_k(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """%K = 100 (close - lowest low of n) / (highest high of n - lowest low of n), today included; NaN when the span is 0."""
    lowest = low.rolling(n).min()
    span = high.rolling(n).max() - lowest
    return 100.0 * (close - lowest) / span.where(span != 0)


def trade_return(entry_open: float, exit_close: float) -> float:
    """Return after slippage and the Alpaca fees. No per-day cent round-up: it needs every order of the day."""
    sell = exit_close * (1.0 - SLIPPAGE)
    sell_fee = SELL_FEE_RATE * sell + SELL_FEE_FLAT
    return (sell - sell_fee) / (entry_open * (1.0 + SLIPPAGE) + BUY_FEE) - 1.0


def average(close: pd.Series, n: int, kind: str) -> pd.Series:
    """Simple (rolling mean) or exponential (ewm span n, adjust False, min_periods n) average of the close."""
    if kind == "sma":
        return close.rolling(n).mean()
    if kind == "ema":
        return close.ewm(span=n, adjust=False, min_periods=n).mean()
    raise ValueError(f"unknown average kind {kind!r}")


def cross_flags(frame: pd.DataFrame, kind: str, f: int, ts: int) -> "tuple[pd.Series, pd.Series]":
    """(entry days, exit days): fast crosses above slow, and fast crosses below slow (NaN counts as not met)."""
    close = frame["close"].astype(float).reset_index(drop=True)
    fast, slow = average(close, f, kind), average(close, ts, kind)
    entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exits = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    return entries, exits


def cross_trades(frame: pd.DataFrame, entries: pd.Series, exits: pd.Series) -> list:
    """One dict per entry day: a trade, or a skipped note. The exit day is the first exit day after the entry day; both
    trades fill at the next session's open (entry at the open after the entry day, exit at the open after the exit day)."""
    rows = []
    last_exit = -1
    dates = list(frame["date"])
    opens = frame["open"].astype(float).tolist()
    exit_days = [int(i) for i in np.flatnonzero(exits.to_numpy())]
    for s in np.flatnonzero(entries.to_numpy()):
        s = int(s)
        row = {"signal_index": s, "signal_date": dates[s]}
        later = [i for i in exit_days if i > s]
        if s <= last_exit:
            row["skipped"] = "skipped: in trade"
        elif not later or later[0] + 1 >= len(dates):
            row["skipped"] = "skipped: data ends before the exit"
        else:
            exit_day = later[0]
            entry, exit_ = s + 1, exit_day + 1
            row.update(exit_signal_index=exit_day, entry_date=dates[entry], entry_price=opens[entry],
                       exit_date=dates[exit_], exit_price=opens[exit_], ret=trade_return(opens[entry], opens[exit_]),
                       skipped="")
            last_exit = exit_
        rows.append(row)
    return rows


def theory_flags(frame: pd.DataFrame, theory: str, d: dict) -> pd.Series:
    """Signal days of one theory: low line (Bollinger lower or Donchian low), oscillator (RSI or %K) and volume ratio."""
    close = frame["close"].astype(float).reset_index(drop=True)
    low = frame["low"].astype(float).reset_index(drop=True)
    high = frame["high"].astype(float).reset_index(drop=True)
    volume = frame["volume"].astype(float).reset_index(drop=True)
    line, osc = theory.split("_")
    below = close < (lower_band(close, d["N"], d["W"]) if line == "bollinger" else donchian_low(low, d["donchian_window"]))
    quiet = (rsi(close, d["R"]) < d["L"]) if osc == "rsi" else (stochastic_k(high, low, close, d["stoch_window"]) < d["stoch_line"])
    return below & quiet & (volume_ratio(volume, d["N"]) >= d["k"])


def build_theory_sheet(data_dir: Path) -> "tuple[pd.DataFrame, dict]":
    manifest = json.loads((data_dir / "manifest.json").read_text())
    d = manifest["DESIGN_V2"]
    hold = d["H"]
    records = []
    trend = {"sma_cross": "sma", "ema_cross": "ema"}
    for code in sorted(manifest["codes"]):
        info = manifest["codes"][code]
        frame = pd.read_parquet(data_dir / f"{code}.parquet")
        for theory in THEORIES:
            for t in trades_for(frame, theory_flags(frame, theory, d), hold):
                done = t["skipped"] == ""
                records.append({
                    "theory": theory, "code": code, "group": info["group"], "signal_date": t["signal_date"],
                    "kind": _kind_for(t["signal_index"], info["events"]),
                    "entry_date": t.get("entry_date", ""), "entry_price": t.get("entry_price", ""),
                    "exit_date": t.get("exit_date", ""), "exit_price": t.get("exit_price", ""),
                    "return": trade_return(t["entry_price"], t["exit_price"]) if done else "", "skipped": t["skipped"],
                })
        for theory in TREND_THEORIES:
            entries, exits = cross_flags(frame, trend[theory], d["trend_fast"], d["trend_slow"])
            for t in cross_trades(frame, entries, exits):
                done = t["skipped"] == ""
                records.append({
                    "theory": theory, "code": code, "group": info["group"], "signal_date": t["signal_date"],
                    "kind": _kind_for(t["signal_index"], info["events"]),
                    "entry_date": t.get("entry_date", ""), "entry_price": t.get("entry_price", ""),
                    "exit_date": t.get("exit_date", ""), "exit_price": t.get("exit_price", ""),
                    "return": t["ret"] if done else "", "skipped": t["skipped"],
                })
    sheet = pd.DataFrame(records, columns=THEORY_COLUMNS)
    groups = sorted({info["group"] for info in manifest["codes"].values()})
    codes = {g: sorted(c for c, i in manifest["codes"].items() if i["group"] == g) for g in groups}
    return sheet, _theory_summary(sheet, groups, codes, hold, d["trend_fast"], d["trend_slow"])


def _theory_summary(sheet: pd.DataFrame, groups: list, codes: dict, hold: int, fast: int, slow: int) -> dict:
    done = sheet[sheet["skipped"] == ""].copy()
    done["return"] = pd.to_numeric(done["return"])
    out: dict = {}
    for theory in THEORIES + TREND_THEORIES:
        out[theory] = {}
        for group in groups:
            out[theory][group] = {}
            for span, (lo, hi) in SPANS.items():
                part = done[(done["theory"] == theory) & (done["group"] == group)
                            & (done["signal_date"] >= lo) & (done["signal_date"] <= hi)]
                per_code = sorted(int((part["code"] == c).sum()) for c in codes[group])
                entry = {
                    "trades": int(len(part)),
                    "win_rate": None if part.empty else float((part["return"] > 0).mean()),
                    "mean_return": None if part.empty else float(part["return"].mean()),
                    "six_fewest_trades": int(sum(per_code[:SIX])),
                }
                if span == "search":
                    entry["verdict"] = ("PASS" if entry["six_fewest_trades"] > VERDICT_MIN_TRADES
                                        and entry["win_rate"] is not None and entry["win_rate"] > VERDICT_MIN_WIN_RATE
                                        and entry["mean_return"] > 0 else "FAIL")
                out[theory][group][span] = entry
    return {"H": hold, "F": fast, "TS": slow, "spans": SPANS, "theories": out}


def write_theory_answer(data_dir: Path) -> None:
    sheet, summary = build_theory_sheet(data_dir)
    sheet.to_csv(data_dir / "theory_answer.csv", index=False)
    (data_dir / "theory_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def folder_version(data_dir: Path) -> int:
    """The made-up stocks version a folder was built as (manifest.json 'version'; absent means 1)."""
    return int(json.loads((data_dir / "manifest.json").read_text()).get("version", 1))
