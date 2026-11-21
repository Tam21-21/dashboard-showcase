"""Per-(source, timeframe) earliest floor + latest end for the Add-dataset "Full" option.

Dashboard-improvements W8 / decisions D8-2 + D8-4. The single, server-authoritative source of
truth for "Full / earliest-to-latest" fetches and for "Update to latest" (it computes the new end).
Kept here (not a frontend constant) so the now-relative branches use the server clock and the values
match the `data_sources.*` parameter-provenance entry exactly.

The floor is keyed by (source, timeframe) because the Alpaca free tier has TWO histories
(research_lab_architectural_decisions.md:389-390): daily reaches 2016-01-04, but minute/intraday
bars are a rolling ~5-year window. A too-early floor is harmless (vbt clamps to the real earliest and
W3's coverage capture records the truth); a too-late floor would truncate, so the floors err early.

Stdlib-only (datetime math) so both routes and researchdb can import it with no circular dependency.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# --- Fixed measured / sentinel floors (provenance: data_sources.* in parameter_provenance.md) ---
# Alpaca SIP daily history floor, verified 2026-05-18 (arch §389: SPY 5 rows for 2016-01-04..10).
_ALPACA_DAILY_FLOOR = "2016-01-04T00:00:00Z"
# Binance spot exchange launched 2017-07; a pre-launch sentinel vbt clamps to the per-symbol listing.
_BINANCE_FLOOR = "2017-07-01T00:00:00Z"
# Alpaca free-tier minute/intraday history is a rolling window of this many years (arch §390).
_ALPACA_INTRADAY_ROLLING_YEARS = 5
# End cutoffs: Alpaca free SIP has a 15-min delay; use 30 min for margin (matches the UI defaultEnd).
_ALPACA_END_LAG_SEC = 30 * 60
# Binance has no delay; back off one minute to avoid the in-progress current bar.
_BINANCE_END_LAG_SEC = 60
# EODHD daily is a pre-downloaded static store reaching back to listing (AAPL daily to 1980). A
# pre-history sentinel earlier than any US listing; the eodhd adapter clamps to the file's real first
# row and records the truth via covered_range, so a too-early floor is harmless (M5 data-store).
_EODHD_FLOOR = "1960-01-01T00:00:00Z"

# Lane GV2-4a: the made-up stocks (research_lab/synthetic) are read from a host store, window-clamped to the
# file by the worker's host conversion (research_lab/worker/synthetic_fetch.py), exactly as the eodhd adapter
# clamps to its file. So the synthetic floor equals the eodhd floor: a pre-history sentinel, harmless when too early.
_SYNTHETIC_FLOOR = _EODHD_FLOOR
# A symbol is synthetic exactly when it matches this (GV2-1 writes SYN_VOL_01 .. SYN_FLT_06, SYN_TWN_01 .. 06).
SYNTHETIC_SYMBOL_PATTERN = re.compile(r"^SYN_[A-Z]{3}_[0-9]{2}$")

_DAILY_TIMEFRAMES = {"1d"}                                      # everything else (1m/5m/15m/1h/4h) is intraday


def price_source_for_ticker(ticker: str) -> str:
    """The data source a bare price ticker is fetched from: "synthetic" for the made-up pattern, else "eodhd"."""
    return "synthetic" if SYNTHETIC_SYMBOL_PATTERN.match(ticker) else "eodhd"


def _iso_to_epoch(iso: str) -> int:
    """ISO-8601 'Z' -> unix seconds UTC."""
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _epoch_to_iso(epoch: int) -> str:
    """unix seconds UTC -> ISO-8601 'Z' (seconds precision, the fetch.py contract form)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minus_years(now_epoch: int, years: int) -> int:
    """now - `years` calendar years, in unix seconds UTC (Feb 29 -> Feb 28 guard)."""
    dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    try:
        return int(dt.replace(year=dt.year - years).timestamp())
    except ValueError:                                          # Feb 29 in a leap year with a non-leap target
        return int(dt.replace(year=dt.year - years, day=28).timestamp())


def earliest_floor_epoch(source: str, timeframe: str, now_epoch: int) -> int:
    """Earliest fetchable start (unix seconds UTC) for a "Full" fetch of (source, timeframe).

    now_epoch is injected (never read from the wall clock here) so the result is deterministic
    and testable. Raises ValueError on an unknown source.
    """
    if source == "alpaca_sip":
        if timeframe in _DAILY_TIMEFRAMES:
            return _iso_to_epoch(_ALPACA_DAILY_FLOOR)           # fixed measured daily floor
        return _minus_years(now_epoch, _ALPACA_INTRADAY_ROLLING_YEARS)  # rolling intraday window
    if source == "binance":
        return _iso_to_epoch(_BINANCE_FLOOR)                    # pre-launch sentinel, vbt clamps per symbol
    if source == "eodhd":
        return _iso_to_epoch(_EODHD_FLOOR)                      # pre-history sentinel, the adapter clamps to file coverage
    if source == "synthetic":
        return _iso_to_epoch(_SYNTHETIC_FLOOR)                  # same sentinel; the host conversion windows to the file
    raise ValueError(f"unknown source {source!r}; expected 'alpaca_sip', 'binance', 'eodhd', or 'synthetic'")


def latest_end_epoch(source: str, now_epoch: int) -> int:
    """Latest fetchable end (unix seconds UTC) for a "Full"/"Update to latest" fetch.

    now_epoch is injected for determinism. Raises ValueError on an unknown source.
    """
    if source == "alpaca_sip":
        return now_epoch - _ALPACA_END_LAG_SEC                  # clears the 15-min free-tier delay with margin
    if source == "binance":
        return now_epoch - _BINANCE_END_LAG_SEC                 # avoids the in-progress current bar
    if source == "eodhd":
        return now_epoch                                        # static local file, no real-time delay; the adapter clamps to the file's last bar
    if source == "synthetic":
        return now_epoch                                        # static local file, like eodhd; the host conversion windows to the file
    raise ValueError(f"unknown source {source!r}; expected 'alpaca_sip', 'binance', 'eodhd', or 'synthetic'")


def full_window_iso(source: str, timeframe: str, now_epoch: int) -> dict[str, str]:
    """The {start, end} ISO-8601 'Z' window for a "Full" fetch of (source, timeframe)."""
    return {
        "start": _epoch_to_iso(earliest_floor_epoch(source, timeframe, now_epoch)),
        "end": _epoch_to_iso(latest_end_epoch(source, now_epoch)),
    }
