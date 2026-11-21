"""Permutation test on price bars for judge check 7 (lane P4, DECISIONS #422 and #445 item 4).

The strategy is re-run in full on fake markets made by shuffling the real price bars, and the real result is
compared with the fake ones. Written from Timothy Masters, Testing and Tuning Market Trading Systems, sections
7.7.1 (Fisher-Yates), 7.7.3 (every market with the same shuffle), 7.7.4 (permuting bars) and 7.4 (the loop and
p = (k + 1) / (m + 1)). No code was taken from any other implementation.

How a fake market is made (all in log prices):
  every bar from window_start on is cut into two pieces: the gap (previous close to this open) and the in-bar piece
  (open to high, low and close, with the bar's volume). The gaps are shuffled on their own and the in-bar pieces
  are shuffled on their own (two independent Fisher-Yates shuffles); the same two orders are used for every ticker.
  Bars are then rebuilt one after the other from the close before them. Bars before window_start, and the close
  just before it, stay as they are, so a lookback is never permuted (Masters 7.2). The last close is kept because
  the sum of all the pieces does not depend on their order.

The library picks no statistic. The caller passes perform(bars) returning (statistic, trade_count); higher is
better; a run with trade_count 0 scores 0.0 (no position, no return); any other NaN raises MCPTError.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["date", "open", "high", "low", "close", "volume"]
ARTIFACT_COLUMNS = ["k", "m", "p_value", "original_performance", "seed", "statistic", "dropped_dates"]


class MCPTError(Exception):
    """A permutation test that cannot be run or scored, with the reason in the message."""


def _check_frame(ticker: str, frame: "pd.DataFrame") -> None:
    if not isinstance(frame, pd.DataFrame):
        raise MCPTError(f"{ticker}: expected a DataFrame, got {type(frame).__name__}")
    missing = [c for c in COLUMNS if c not in frame.columns]
    if missing:
        raise MCPTError(f"{ticker}: columns missing: {missing}; need lowercase {COLUMNS}")
    prices = frame[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise MCPTError(f"{ticker}: open, high, low and close must all be finite and above zero (logs are taken)")
    dates = pd.to_datetime(frame["date"])
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise MCPTError(f"{ticker}: dates must be strictly increasing with no repeats")


def _align(ohlcv_by_ticker: dict) -> "tuple[dict, int]":
    """Masters 7.7.3 step 1: keep only the dates every ticker has. Returns the aligned frames and how many dates
    (counted over the union of all tickers) were dropped."""
    if not ohlcv_by_ticker:
        raise MCPTError("no tickers given")
    for ticker, frame in ohlcv_by_ticker.items():
        _check_frame(ticker, frame)
    date_sets = [set(f["date"]) for f in ohlcv_by_ticker.values()]
    common = set.intersection(*date_sets)
    dropped = len(set.union(*date_sets)) - len(common)
    aligned = {t: f.loc[f["date"].isin(common), COLUMNS].reset_index(drop=True) for t, f in ohlcv_by_ticker.items()}
    return aligned, dropped


def _window_position(aligned: dict, window_start: "int | str") -> int:
    """window_start is a position in the aligned bars (int) or a date, which names the first bar on or after it."""
    dates = pd.to_datetime(next(iter(aligned.values()))["date"])
    n = len(dates)
    if isinstance(window_start, str) or isinstance(window_start, pd.Timestamp):
        hits = np.flatnonzero((dates >= pd.Timestamp(window_start)).to_numpy())
        if len(hits) == 0:
            raise MCPTError(f"window_start {window_start}: no bar on or after it (last bar {dates.iloc[-1]})")
        pos = int(hits[0])
    else:
        pos = int(window_start)
    if pos < 1:
        raise MCPTError(f"window_start resolves to bar {pos}: the window needs at least one bar before it to start from")
    if n - pos < 2:
        raise MCPTError(f"window_start resolves to bar {pos} of {n}: the window needs at least 2 bars to shuffle")
    return pos


def _fisher_yates(n: int, rng: "np.random.Generator") -> "np.ndarray":
    """Masters 7.7.1: i = n; while i > 1: j = floor(u * i) with 0 <= u < 1; i -= 1; swap order[i] and order[j]."""
    order = np.arange(n)
    draws = rng.random(n)
    i = n
    step = 0
    while i > 1:
        j = int(draws[step] * i)
        step += 1
        i -= 1
        order[i], order[j] = order[j], order[i]
    return order


def _draw_orders(width: int, rng: "np.random.Generator") -> "tuple[np.ndarray, np.ndarray]":
    """The two shuffle orders of one fake market, drawn in today's sequence: the gap order first, then the bar order."""
    gap_order = _fisher_yates(width, rng)
    bar_order = _fisher_yates(width, rng)
    return gap_order, bar_order


def _build_fake(aligned: dict, pos: int, gap_order: "np.ndarray", bar_order: "np.ndarray") -> dict:
    """The fake market that two given orders make from the aligned bars."""
    out = {}
    for ticker, frame in aligned.items():
        o = np.log(frame["open"].to_numpy(dtype=float))
        h = np.log(frame["high"].to_numpy(dtype=float))
        lo = np.log(frame["low"].to_numpy(dtype=float))
        c = np.log(frame["close"].to_numpy(dtype=float))
        gap = (o[pos:] - c[pos - 1:-1])[gap_order]
        rel_high = (h[pos:] - o[pos:])[bar_order]
        rel_low = (lo[pos:] - o[pos:])[bar_order]
        rel_close = (c[pos:] - o[pos:])[bar_order]
        volume = frame["volume"].to_numpy()[pos:][bar_order]
        prev_close = c[pos - 1] + np.concatenate([[0.0], np.cumsum(gap + rel_close)[:-1]])
        new_open = prev_close + gap
        built = pd.DataFrame({
            "date": frame["date"].to_numpy()[pos:],
            "open": np.exp(new_open),
            "high": np.exp(new_open + rel_high),
            "low": np.exp(new_open + rel_low),
            "close": np.exp(new_open + rel_close),
            "volume": volume,
        })
        out[ticker] = pd.concat([frame.iloc[:pos], built], ignore_index=True)[COLUMNS]
    return out


def _permute_aligned(aligned: dict, pos: int, rng: "np.random.Generator") -> dict:
    n = len(next(iter(aligned.values())))
    gap_order, bar_order = _draw_orders(n - pos, rng)
    return _build_fake(aligned, pos, gap_order, bar_order)


def permute_bars(ohlcv_by_ticker: dict, window_start: "int | str", rng: "np.random.Generator") -> "tuple[dict, int]":
    """One fake market. Takes ticker -> DataFrame (lowercase date, open, high, low, close, volume) and returns
    (ticker -> DataFrame with a RangeIndex, number of dates dropped to align the tickers). The tickers are aligned
    on the dates all of them have first (Masters 7.7.3 step 1), so the dates out are the common dates."""
    aligned, dropped = _align(ohlcv_by_ticker)
    pos = _window_position(aligned, window_start)
    return _permute_aligned(aligned, pos, rng), dropped


def _score(result: object, label: str) -> float:
    try:
        statistic, trade_count = result
    except (TypeError, ValueError) as err:
        raise MCPTError(f"{label}: perform must return (statistic, trade_count), got {result!r}") from err
    if isinstance(trade_count, bool) or not isinstance(trade_count, (int, np.integer)) or trade_count < 0:
        raise MCPTError(f"{label}: trade_count must be a whole number of at least 0, got {trade_count!r}")
    if trade_count == 0:
        return 0.0  # no position, no return
    value = float(statistic)
    if np.isnan(value):
        raise MCPTError(f"{label}: perform returned a NaN statistic with {trade_count} trades")
    return value


# What the fork pool's workers read: set in the parent before the pool starts, so a fork copies it and nothing is pickled.
_POOL_STATE: dict = {}


def _score_reps(lo: int, hi: int) -> "list[float]":
    """The scores of reps lo up to hi (in order), from the state the parent set. Any error goes to the caller as it is."""
    state = _POOL_STATE
    orders = state["orders"]
    scores = []
    for rep in range(lo, hi):
        fake = _build_fake(state["aligned"], state["pos"], orders[rep, 0], orders[rep, 1])
        scores.append(_score(state["perform"](fake), f"rep {rep}"))
    return scores


def _rep_ranges(m: int, workers: int) -> "list[tuple[int, int]]":
    """Contiguous rep ranges, one per worker (fewer when m is small), in rep order."""
    size = -(-m // workers)
    return [(lo, min(lo + size, m)) for lo in range(0, m, size)]


def _score_in_pool(m: int, workers: int) -> "list[float]":
    import multiprocessing  # noqa: PLC0415 - only the parallel path needs it
    ranges = _rep_ranges(m, workers)
    pool = multiprocessing.get_context("fork").Pool(processes=len(ranges))
    try:
        pending = [pool.apply_async(_score_reps, (lo, hi)) for lo, hi in ranges]
        scores: "list[float]" = []
        for job in pending:
            scores.extend(job.get())
    except BaseException:
        pool.terminate()
        pool.join()
        raise
    pool.close()
    pool.join()
    return scores


def mcpt(perform, ohlcv_by_ticker: dict, window_start: "int | str", m: int, seed: int,
         statistic_name: "str | None" = None, workers: int = 1) -> dict:
    """Masters 7.4 over a caller's re-run. original = the statistic of perform on the real bars (aligned the same way
    as the fakes); then m times perform on a fake market drawn with numpy default_rng(seed); k counts the fakes
    whose statistic is at least the original's; p = (k + 1) / (m + 1).

    workers above 1 score the reps in a fork process pool over contiguous rep ranges. Every shuffle order is drawn first,
    in today's order, from the one default_rng(seed), so k is the same for any workers. An error from perform or the scoring
    reaches the caller unchanged, and the pool is closed and joined (or terminated on an error) before this returns or raises."""
    if not isinstance(m, int) or isinstance(m, bool) or m < 1:
        raise MCPTError(f"m must be a whole number of at least 1, got {m!r}")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise MCPTError(f"workers must be a whole number of at least 1, got {workers!r}")
    name = statistic_name if statistic_name is not None else getattr(perform, "__name__", None)
    if not name:
        raise MCPTError("the statistic needs a name: give statistic_name or a named perform function")
    aligned, dropped = _align(ohlcv_by_ticker)
    pos = _window_position(aligned, window_start)
    original = _score(perform({t: f.copy() for t, f in aligned.items()}), "the original run")
    rng = np.random.default_rng(seed)
    width = len(next(iter(aligned.values()))) - pos
    orders = np.empty((m, 2, width), dtype=np.int32)
    for rep in range(m):
        orders[rep, 0], orders[rep, 1] = _draw_orders(width, rng)
    _POOL_STATE.update(perform=perform, aligned=aligned, pos=pos, orders=orders)
    try:
        scores = _score_reps(0, m) if workers == 1 else _score_in_pool(m, workers)
    finally:
        _POOL_STATE.clear()
    k = sum(1 for score in scores if score >= original)
    return {"k": k, "m": m, "p_value": (k + 1) / (m + 1), "original": original, "seed": seed,
            "statistic": name, "dropped_dates": dropped}


def write_mcpt_artifact(result: dict, path: "str | Path") -> None:
    """The result as a one-row parquet: k, m, p_value, original_performance, seed, statistic, dropped_dates."""
    row = {
        "k": int(result["k"]), "m": int(result["m"]), "p_value": float(result["p_value"]),
        "original_performance": float(result["original"]), "seed": int(result["seed"]),
        "statistic": str(result["statistic"]), "dropped_dates": int(result["dropped_dates"]),
    }
    pd.DataFrame([row], columns=ARTIFACT_COLUMNS).to_parquet(path, index=False)
