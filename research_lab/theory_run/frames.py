"""Loading a stock group's bars as one DataFrame per field (lane GV2-5a, plan step 2).

A frame has the session dates as its index and the tickers as its columns. Sessions are aligned across the stocks; a
session one stock lacks is NaN there and is never filled, so no indicator, signal or fill is ever made from a made-up bar.

Two loaders, chosen by the spec's `source_shape`:
  "stored": dataset parquet files in the stored shape (Open, High, Low, Close, Volume, Factor). Stored prices are adjusted at
            fetch (fetch_eodhd: Open, High, Low = as-traded x Factor, Close = adjusted_close, Volume as traded) and are read as
            stored, never multiplied or divided. Factor (adjusted / as-traded, below 1 before a later dividend or split) comes back
            as a sixth frame "factor" and serves only real-share fees (simulate.run_group, theory_stage.buyhold). A Factor that is
            not a number above 0 on any row raises: the as-traded price would be unknown.
  "raw_synthetic": GV2-1's files (date, open, high, low, close, adjusted_close, volume) in a folder whose manifest.json
            says "synthetic": true; a folder without that is refused.
Both return (frames, shas): the field frames and {ticker: sha256 of the file's bytes}.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

FIELDS = ("open", "high", "low", "close", "volume")
STORED_COLUMNS = ("Open", "High", "Low", "Close", "Volume", "Factor")
RAW_COLUMNS = ("date", "open", "high", "low", "close", "adjusted_close", "volume")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _need_columns(path: Path, frame: pd.DataFrame, wanted: tuple) -> None:
    missing = [c for c in wanted if c not in frame.columns]
    extra = [c for c in frame.columns if c not in wanted]
    if missing or extra:
        raise ValueError("%s: expected columns %s, missing %s, unexpected %s" % (path, list(wanted), missing, extra))


def _assemble(per_ticker: dict) -> dict:
    """per_ticker: {ticker: DataFrame with lower-case field columns on a sorted unique index} -> {field: DataFrame}."""
    if not per_ticker:
        raise ValueError("a group needs at least one stock")
    out = {}
    for field in FIELDS:
        out[field] = pd.DataFrame({t: f[field].astype(float) for t, f in per_ticker.items()}).sort_index()
    return out


def load_raw_synthetic(folder, codes) -> tuple:
    folder = Path(folder)
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("%s has no manifest.json: refusing a folder that does not say it is synthetic" % folder)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True:
        raise ValueError('%s: manifest.json does not say "synthetic": true; raw_synthetic reads made-up data only' % folder)
    per_ticker, shas = {}, {}
    for code in codes:
        path = folder / ("%s.parquet" % code)
        if not path.is_file():
            raise ValueError("%s is missing from %s" % (path.name, folder))
        frame = pd.read_parquet(path)
        _need_columns(path, frame, RAW_COLUMNS)
        frame = frame.set_index(pd.DatetimeIndex(pd.to_datetime(frame["date"])))
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError("%s: dates must be strictly increasing" % path)
        per_ticker[code] = frame
        shas[code] = _sha256_of(path)
    return _assemble(per_ticker), shas


def load_stored(files) -> tuple:
    """files: {ticker: path of a dataset parquet in the stored shape}."""
    per_ticker, shas = {}, {}
    for ticker, name in files.items():
        path = Path(name)
        if not path.is_file():
            raise ValueError("%s: dataset file not found" % path)
        frame = pd.read_parquet(path)
        _need_columns(path, frame, STORED_COLUMNS)
        good = frame["Factor"] > 0.0   # NaN compares False, so a NaN Factor is counted as bad too
        if not good.all():
            bad = frame["Factor"][~good]
            raise ValueError("%s: Factor is not a number above 0 on %d rows, first %s (%r); the as-traded price is unknown"
                             % (path, len(bad), pd.Timestamp(bad.index[0]).date(), float(bad.iloc[0])))
        if not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
            raise ValueError("%s: the index must be strictly increasing" % path)
        per_ticker[ticker] = frame.rename(columns=str.lower)
        shas[ticker] = _sha256_of(path)
    out = _assemble(per_ticker)
    out["factor"] = pd.DataFrame({t: f["factor"].astype(float) for t, f in per_ticker.items()}).sort_index()
    return out, shas
