"""Writing the stage's files so the same spec gives the same bytes (lane GV2-7a, Done (4))."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def clean(value):
    """JSON-safe: NaN and infinity become null, numpy numbers and arrays become plain ones."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [clean(v) for v in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(clean(data), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def write_parquet(path: Path, frame: pd.DataFrame) -> str:
    """Write a frame without its index and return the sha256 of the file's bytes."""
    frame.to_parquet(path, index=False, engine="pyarrow", compression="snappy")
    return sha256_of(path)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unix_seconds(index: pd.DatetimeIndex) -> np.ndarray:
    """Whole seconds since 1970-01-01 UTC of each date (a date without a zone is read as UTC)."""
    naive = index.tz_convert("UTC").tz_localize(None) if index.tz is not None else index
    return ((naive - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


def equity_frame(equity: pd.Series) -> pd.DataFrame:
    """timestamp (unix seconds) and equity, the shape the judge reads (`_dated_bar_returns_from_equity`)."""
    return pd.DataFrame({"timestamp": unix_seconds(equity.index), "equity": equity.to_numpy(dtype=float)})
