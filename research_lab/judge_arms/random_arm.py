"""Random-entry control arm for judge check 9 (lane P5, DECISIONS #213 row 9, #376, #421 item 1, #445 item 4).

Written from David Aronson, Evidence-Based Technical Analysis: "The fix: detrend the market data", "Look-ahead bias"
(return = POS_0 x log(O+2 / O+1) - ALR), "Monte Carlo permutation method (MCP)" steps 1 to 7 (folios 240 and 243) and
"Data-mining-aware MCP" step 2 (one permutation for all series); the p-value form (k + 1) / (m + 1) is Masters'
(Testing and Tuning Market Trading Systems, 7.4, frozen as random_arm_comparison_statistic).

The idea in plain words: the strategy's own positions are paired with market moves put in a random order. If the real
pairing earns more than chance pairings almost always, the strategy has something. A move is the log of the next open
over this open, minus that ticker's average move over the window (so a rising market earns nothing on its own).

weights: a table, one row per session, one column per ticker. weights[t, ticker] is the share of equity held in that
ticker over the interval that starts at the open of session t (a position decided at a close is held from the next open
to the open after: Aronson's look-ahead form). opens: the same tickers, one row per session, a superset of the weights
sessions, so that every window session has a next open when the data has one.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ARTIFACT_COLUMNS = ["k", "m", "p_value", "original_statistic", "seed"]


class RandomArmError(Exception):
    """A random-entry arm that cannot be computed, with the case named in the message."""


def window_returns(weights: "pd.DataFrame", opens: "pd.DataFrame", window: "tuple[str, str]") -> "tuple[np.ndarray, np.ndarray, list]":
    """The weights and detrended returns the arm is graded on: (weights, returns, sessions).

    Sessions are the weights rows with start <= session <= end (window, both ends inclusive, labels compared as
    sorted text, so ISO dates). Each keeps only if opens has a row after it. returns[t, j] = log(open[t+1] / open[t])
    for ticker j, minus ticker j's mean over the kept sessions. Raises RandomArmError for a ticker with fewer than 2
    opens, an open that is not finite and above zero, a session missing from opens, or a window with no usable session.
    """
    absent = sorted(set(weights.columns) - set(opens.columns))
    if absent:
        raise RandomArmError(f"opens has no column for ticker(s) {absent}")
    tickers = list(weights.columns)
    prices = opens[tickers]
    for ticker in tickers:
        column = prices[ticker]
        if int(column.notna().sum()) < 2:
            raise RandomArmError(f"{ticker}: fewer than 2 opens, a return cannot be formed")
        finite = column.dropna().to_numpy(dtype=float)
        if not np.isfinite(finite).all() or (finite <= 0).any():
            raise RandomArmError(f"{ticker}: every open must be finite and above zero (logs are taken)")
    start, end = window
    in_window = [s for s in weights.index if start <= s <= end]
    if not in_window:
        raise RandomArmError(f"no session of the weights table falls in the window {start} to {end}")
    positions = prices.index.get_indexer(in_window)
    if (positions < 0).any():
        missing = [s for s, p in zip(in_window, positions) if p < 0][:3]
        raise RandomArmError(f"opens has no row for window session(s) {missing}")
    usable = positions + 1 < len(prices)
    kept_sessions = [s for s, ok in zip(in_window, usable) if ok]
    if not kept_sessions:
        raise RandomArmError("no window session has a next open")
    kept_positions = positions[usable]
    now = prices.to_numpy(dtype=float)[kept_positions]
    after = prices.to_numpy(dtype=float)[kept_positions + 1]
    if np.isnan(now).any() or np.isnan(after).any():
        bad = [t for j, t in enumerate(tickers) if np.isnan(now[:, j]).any() or np.isnan(after[:, j]).any()]
        raise RandomArmError(f"{bad[0]}: an open is missing inside the window, a return cannot be formed")
    raw = np.log(after / now)
    returns = raw - raw.mean(axis=0)
    kept_weights = weights.loc[kept_sessions].to_numpy(dtype=float)
    if not np.isfinite(kept_weights).all():
        raise RandomArmError("weights must be finite numbers")
    if not (kept_weights != 0.0).any():
        raise RandomArmError("no trade in the window: every weight is zero over the sessions that have a next open")
    return kept_weights, returns, kept_sessions


def statistic(weights: "np.ndarray", returns: "np.ndarray") -> float:
    """Mean over sessions of the sum over tickers of weight times return (Aronson's mean rule return)."""
    return float((weights * returns).sum(axis=1).mean())


def shuffle_returns(returns: "np.ndarray", rng: "np.random.Generator") -> "tuple[np.ndarray, np.ndarray]":
    """One permutation of the session rows, the same order for every ticker (Aronson, data-mining-aware MCP, step 2).
    Returns (the permuted copy, the order). The input is not changed."""
    order = rng.permutation(returns.shape[0])
    return returns[order], order


def random_arm(weights: "pd.DataFrame", opens: "pd.DataFrame", window: "tuple[str, str]", m: int, seed: int) -> dict:
    """Aronson's Monte Carlo permutation on the real pairing of weights and returns.

    original = statistic on the real pairing. m times: permute the session rows of the returns with one numpy
    default_rng(seed) permutation shared by all tickers, recompute, and count it in k when it is >= original. The
    weights are never permuted. Returns k, m, p_value = (k + 1) / (m + 1), original_statistic and seed.
    """
    if not isinstance(m, int) or isinstance(m, bool) or m < 1:
        raise RandomArmError(f"m must be a whole number of at least 1, got {m!r}")
    w, r, _ = window_returns(weights, opens, window)
    original = statistic(w, r)
    rng = np.random.default_rng(seed)
    k = 0
    for _ in range(m):
        shuffled, _order = shuffle_returns(r, rng)
        if statistic(w, shuffled) >= original:
            k += 1
    return {"k": k, "m": m, "p_value": (k + 1) / (m + 1), "original_statistic": original, "seed": seed}


def weights_from_trades(trades: list, sessions: list, tickers: list) -> "pd.DataFrame":
    """The weights table from a trade list. Each trade is a dict: ticker, entry_index, exit_index, weight. Its weight
    sits on rows entry_index through exit_index inclusive of `sessions` (entered at the entry open; the exit at the exit
    session's close is approximated by the next open, as Aronson's POS_0 x log(O+2 / O+1) applies a position from the
    open after the decision). Trades that overlap on one ticker add. Rows outside the table are cut off."""
    table = pd.DataFrame(0.0, index=list(sessions), columns=list(tickers))
    n = len(table)
    for trade in trades:
        ticker = trade["ticker"]
        if ticker not in table.columns:
            raise RandomArmError(f"a trade names ticker {ticker!r}, which is not in the table's tickers")
        first, last = int(trade["entry_index"]), int(trade["exit_index"])
        if last < first:
            raise RandomArmError(f"{ticker}: a trade exits (row {last}) before it enters (row {first})")
        lo, hi = max(first, 0), min(last, n - 1)
        if lo <= hi:
            table.iloc[lo:hi + 1, table.columns.get_loc(ticker)] += float(trade["weight"])
    return table


def write_random_arm_artifact(result: dict, path: "str | Path") -> None:
    """The result as a one-row parquet: k, m, p_value, original_statistic, seed."""
    row = {c: result[c] for c in ARTIFACT_COLUMNS}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row], columns=ARTIFACT_COLUMNS).to_parquet(path, index=False)
