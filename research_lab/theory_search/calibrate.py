"""A finalist's stop and target, calibrated by AFML chapter 13 (lane GV2-5b, plan step 6; the tools are GV2-6's research_lab.exits.otr).

Plain words: each finalist's own past entries show how its price behaves after the entry. We fit the O-U process (a price that
is pulled back to a level at a steady rate) to them, simulate paths from it, and take the stop and target with the best Sharpe
on the simulated paths. The pull-back level is the theory's expected price, so each entry has its own gap: how far the price
must travel to reach it. The book fits one gap at a time; here the gaps are bucketed to the nearest half sigma and one mesh
is run per bucket, a stated simplification of the book's per-opportunity initial conditions.

Units are log prices: a stop of s and a target of t are the prices entry * exp(-s) and entry * exp(t); run_group takes them as
fractions of the executed entry price, 1 - exp(-s) and exp(t) - 1.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research_lab.exits import otr

BUCKET_SIGMA = 0.5  # the gap bucket width in sigma units (plan step 6)


def gap_bucket(gap: float, sigma: float) -> float:
    """The gap in sigma units rounded to the nearest 0.5 (an exact half rounds up), as a multiple of 0.5."""
    if not (math.isfinite(gap) and math.isfinite(sigma) and sigma > 0):
        raise ValueError("gap_bucket needs a finite gap and a positive sigma, got gap %r and sigma %r" % (gap, sigma))
    return math.floor(gap / (BUCKET_SIGMA * sigma) + 0.5) * BUCKET_SIGMA


def exit_fractions(stop: float, target: float) -> tuple:
    """(stop fraction, target fraction) of the entry price for a stop and target in log units."""
    for name, value in (("stop", stop), ("target", target)):
        if not (math.isfinite(value) and value > 0):
            raise ValueError("the %s must be a positive finite log distance, got %r" % (name, value))
    return 1.0 - math.exp(-stop), math.exp(target) - 1.0


def opportunities(frames: dict, expected: pd.DataFrame, entries: list, hold: int, last_day: str) -> tuple:
    """The finalist's opportunities from its entries. `entries` are (ticker, fill date). Returns (list of (path, gap), skipped).

    path = [0] then the log close of each of the `hold` bars from the fill bar on, minus the log open of the fill bar;
    gap = log of the expected price at the signal bar (the stock's last bar before the fill) minus the log open of the fill bar.
    `last_day` ("YYYY-MM-DD", the screen window's last day) bounds what is read: the path never reaches a bar after the last bar on
    or before it, so no price after the window the entries were found in enters the calibration (lane SL1).
    An entry is skipped, and counted, when its last path bar (the fill bar plus `hold` - 1) lies after that last bar, there is no
    earlier bar, or a price is missing.
    """
    opens, closes = frames["open"], frames["close"]
    index = closes.index
    end = pd.Timestamp(last_day)
    if index.tz is not None:  # as exit_frames does, for a stored-shape dataset's UTC index
        end = end.tz_localize(index.tz)
    last_pos = int(index.searchsorted(end, side="right")) - 1
    found, skipped = [], 0
    for ticker, fill in entries:
        pos = index.get_loc(pd.Timestamp(fill))
        if pos + hold - 1 > last_pos:
            skipped += 1
            continue
        before = np.flatnonzero(closes[ticker].iloc[:pos].notna().to_numpy())
        if len(before) == 0:
            skipped += 1
            continue
        signal = int(before[-1])
        entry_price = float(opens[ticker].iloc[pos])
        exp = float(expected[ticker].iloc[signal])
        window = closes[ticker].iloc[pos:pos + hold].to_numpy(dtype=float)
        if not (math.isfinite(entry_price) and entry_price > 0 and math.isfinite(exp) and exp > 0
                and np.isfinite(window).all() and (window > 0).all()):
            skipped += 1
            continue
        path = [0.0] + [float(math.log(c) - math.log(entry_price)) for c in window]
        found.append((path, math.log(exp) - math.log(entry_price)))
    return found, skipped


class Calibrator:
    """One mesh per gap bucket: pair(gap) gives the best stop and target (log units) of the gap's bucket."""

    def __init__(self, fit: otr.OUFit, hold: int, n_iter: int, seed: int):
        self.fit, self.hold, self.n_iter, self.seed = fit, int(hold), int(n_iter), int(seed)
        self.cache: dict = {}

    def pair(self, gap: float) -> dict:
        bucket = gap_bucket(gap, self.fit.sigma)
        if bucket not in self.cache:
            nodes = otr.mesh(self.fit, bucket * self.fit.sigma, self.hold, self.n_iter, self.seed)
            rule = otr.optimal_rule(nodes)
            self.cache[bucket] = {"bucket": bucket, "stop": rule["stop"], "target": rule["target"],
                                  "stop_sigma": rule["stop_sigma"], "target_sigma": rule["target_sigma"], "mesh_sharpe": rule["sharpe"]}
        return dict(self.cache[bucket])


def exit_frames(frames: dict, expected: pd.DataFrame, entries: pd.DataFrame, calibrator: Calibrator, window: tuple) -> tuple:
    """Stop and target fraction frames (by signal bar) for the entry signals inside the window; NaN elsewhere.

    Returns (stops, targets). A signal with no later bar, or whose fill bar lies after the window's last bar, has no fill in the
    window (run_group never trades it), so it gets no pair and the bar after the window is never read (lane SL1). A signal whose gap cannot be
    computed (no expected price) raises: a trade must never open without its calibrated exit.
    """
    opens, closes = frames["open"], frames["close"]
    index = closes.index
    start, end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    if index.tz is not None:  # as simulate.run_group localizes the window to a UTC index
        start, end = start.tz_localize(index.tz), end.tz_localize(index.tz)
    stops = pd.DataFrame(np.nan, index=index, columns=closes.columns)
    targets = pd.DataFrame(np.nan, index=index, columns=closes.columns)
    inside = (index >= start) & (index <= end)
    for ticker in closes.columns:
        valid = np.flatnonzero(closes[ticker].notna().to_numpy())
        col = entries[ticker].to_numpy(dtype=bool)
        for s in np.flatnonzero(col & inside):
            j = int(np.searchsorted(valid, s, side="right"))
            if j >= len(valid):
                continue
            fill = int(valid[j])
            if index[fill] > end:  # the fill bar lies after the window: run_group never trades this signal, so it needs no exit
                continue
            entry_price = float(opens[ticker].iloc[fill])
            exp = float(expected[ticker].iloc[s])
            if not (math.isfinite(entry_price) and entry_price > 0 and math.isfinite(exp) and exp > 0):
                raise ValueError("%s: the entry signal on %s has no usable expected price (%r) or entry price (%r)"
                                 % (ticker, index[s].date(), exp, entry_price))
            pair = calibrator.pair(math.log(exp) - math.log(entry_price))
            stop_fraction, target_fraction = exit_fractions(pair["stop"], pair["target"])
            stops.iloc[s, stops.columns.get_loc(ticker)] = stop_fraction
            targets.iloc[s, targets.columns.get_loc(ticker)] = target_fraction
    return stops, targets
