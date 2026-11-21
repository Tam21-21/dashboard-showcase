"""Server-side OHLC downsampling for the dataset-preview chart (Brief B, R10).

Pure (pandas/numpy, no I/O), so the probe's B-D legs test it without a route. Reduces a fetched
price frame (the fetch.py vbt output: Title-case Open/High/Low/Close/Volume + a UTC DatetimeIndex)
to at most PREVIEW_MAX_BUCKETS OHLC candles, preserving per-bucket extremes (a spike is never
smoothed away, NFR-5) so the candlestick wicks stay honest, and bounding the browser payload
independent of the input row count (arch §3b; DDIA reduced view, kleppmann_canon.md:1061-1070).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dashboard.backend.config import PREVIEW_MAX_BUCKETS


def reduce_ohlc(df: pd.DataFrame, max_buckets: int = PREVIEW_MAX_BUCKETS) -> list[dict]:
    """Reduce a Title-case OHLC frame to <= max_buckets candles {time, open, high, low, close}.

    `time` is the bucket's first-bar timestamp as epoch SECONDS (the lightweight-charts UTCTimestamp
    PriceChart consumes, matching export.py's price serialization). rows <= max_buckets returns the
    bars unreduced (candle count == rows). Otherwise the series is split into EXACTLY max_buckets
    contiguous near-equal-count buckets (numpy.array_split: floor/ceil sizes differing by at most
    one), so the candle count is exactly max_buckets for any rows > max_buckets, independent of the
    row count — a ceil(rows/max_buckets)-per-bucket split would instead yield fewer than max_buckets
    buckets whenever rows is not a multiple. Per bucket: open = first bar open, high = max high,
    low = min low, close = last bar close.
    """
    rows = len(df)
    if rows == 0:
        return []  # guard: an empty frame has no df.index[0] (a true zero-row fetch can't report ready)
    # Epoch seconds, resolution-safe (force ns then ns->s); asi8 on a tz-aware index is UTC-based,
    # so this is tz-agnostic and matches export.py:69.
    times = df.index.as_unit("ns").asi8 // 10**9
    o = df["Open"].to_numpy()
    h = df["High"].to_numpy()
    low = df["Low"].to_numpy()
    c = df["Close"].to_numpy()
    if rows <= max_buckets:
        return [
            {"time": int(times[i]), "open": float(o[i]), "high": float(h[i]),
             "low": float(low[i]), "close": float(c[i])}
            for i in range(rows)
        ]
    # Exactly max_buckets contiguous buckets; array_split over a contiguous range keeps each bucket's
    # positions contiguous, so first/last index the bucket's open/close and the slice its extremes.
    out: list[dict] = []
    for idx in np.array_split(np.arange(rows), max_buckets):
        s, e = int(idx[0]), int(idx[-1])
        out.append({
            "time": int(times[s]),               # the bucket's first-bar timestamp
            "open": float(o[s]),                  # the bucket's first-bar open
            "high": float(h[s:e + 1].max()),      # the bucket's true high (a spike is preserved)
            "low": float(low[s:e + 1].min()),     # the bucket's true low
            "close": float(c[e]),                 # the bucket's last-bar close
        })
    return out
