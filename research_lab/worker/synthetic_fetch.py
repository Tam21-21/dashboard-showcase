"""Host-side fetch for the made-up stocks (lane GV2-4a, DECISIONS #440).

A made-up stock is a file <store>/<code>.parquet written by `python -m research_lab.synthetic make`
(columns date, open, high, low, close, adjusted_close, volume), beside a manifest.json that says
"synthetic": true. This module turns one such file into the dataset file the research runner reads:
/data/synthetic/<code>/1d/<start>_<end>.parquet with Open, High, Low, Close, Volume, Factor and a UTC
date index, the same shape fetch.py writes for eodhd. It runs on the host, in the worker process: fetch.py
is baked into the m2 image and is not touched by this lane.

Pandas only (no vbt, no network), so a plain probe interpreter can import it. Every refusal is a
ValueError whose message names the cause; nothing is written when a refusal is raised.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

_COLUMNS_IN = ("date", "open", "high", "low", "close", "volume")
_CONTAINER_DATA = "/data"  # the path the runner container sees RESEARCH_DATA_ROOT under


def _check_store(store_root: Path, symbol: str) -> None:
    manifest_path = store_root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"synthetic store {store_root} has no manifest.json; refusing to read it as a synthetic store")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("synthetic") is not True:
        raise ValueError(f"synthetic store {store_root} is not marked synthetic in its manifest.json; refusing")
    if symbol not in manifest.get("codes", {}):
        raise ValueError(f"symbol {symbol!r} is not among the store's codes in {manifest_path}")


def _durable_write_parquet(frame: pd.DataFrame, out_path: Path) -> None:
    """Temp file in the same folder, fsync, os.replace, fsync the folder (the order durable_write.py uses)."""
    tmp_path = out_path.with_name(f".{out_path.name}.tmp")
    frame.to_parquet(tmp_path)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, out_path)
    dirfd = os.open(out_path.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def fetch_to_store(symbol: str, timeframe: str, start_iso: str, end_iso: str,
                   store_root: Path, data_root: Path, mode: str) -> dict:
    """Convert <store_root>/<symbol>.parquet into <data_root>/synthetic/<symbol>/1d/<start>_<end>.parquet.

    Rows are those with start <= date < end. Returns the result dict fetch.py returns: rows, sha256,
    file_path (the /data/... form), file_bytes, covered_start_ts, covered_end_ts.
    """
    store_root, data_root = Path(store_root), Path(data_root)
    if timeframe != "1d":
        raise ValueError(f"synthetic data is daily only; timeframe {timeframe!r} is not available")
    if mode != "fetch":
        raise ValueError(f"synthetic fetch does not support mode {mode!r}; only a whole-window fetch")
    _check_store(store_root, symbol)
    source_path = store_root / f"{symbol}.parquet"
    if not source_path.is_file():
        raise ValueError(f"synthetic store {store_root} has no file for {symbol!r}")
    raw = pd.read_parquet(source_path)
    missing = [c for c in _COLUMNS_IN if c not in raw.columns]
    if missing:
        raise ValueError(f"synthetic file {source_path} is missing columns {missing}; got {list(raw.columns)}")
    start_ts = pd.Timestamp(start_iso, tz="UTC")
    end_ts = pd.Timestamp(end_iso, tz="UTC")
    stamps = pd.to_datetime(raw["date"], utc=True)
    windowed = raw[(stamps >= start_ts) & (stamps < end_ts)]
    if len(windowed) == 0:
        raise ValueError(f"synthetic file for {symbol!r} has no rows in [{start_iso}, {end_iso})")
    frame = pd.DataFrame({
        "Open": windowed["open"], "High": windowed["high"], "Low": windowed["low"],
        "Close": windowed["close"], "Volume": windowed["volume"], "Factor": 1.0,
    })
    frame.index = pd.to_datetime(windowed["date"], utc=True)
    frame.index.name = "date"
    out_dir = data_root / "synthetic" / symbol / timeframe
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(start_ts.timestamp())}_{int(end_ts.timestamp())}.parquet"
    out_path = out_dir / name
    _durable_write_parquet(frame, out_path)
    return {
        "rows": int(len(frame)),
        "sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
        "file_path": f"{_CONTAINER_DATA}/synthetic/{symbol}/{timeframe}/{name}",
        "file_bytes": out_path.stat().st_size,
        "covered_start_ts": int(frame.index[0].timestamp()),
        "covered_end_ts": int(frame.index[-1].timestamp()),
    }
