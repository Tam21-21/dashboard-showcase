"""research-runner fetch entrypoint.

CLI for pulling OHLCV from a single source (binance, alpaca_sip), writing
parquet to a deterministic path, and reporting result as a final JSON
line on stdout.

Reconstructed from M2 Brief C constraints; spec m2_spec_v5.md §8 not
available on the laptop at write time. Behavioral contracts honored:
  - arch (e): pd.Timestamp(iso_str, tz='UTC') at the vbt boundary
  - arch (i): ALPACA_API_KEY / ALPACA_SECRET_KEY env names
  - arch (k): /data/{source}/{symbol_safe}/{timeframe}/{start_ts}_{end_ts}.parquet
  - v4 fix gamma: feed="sip" passed explicitly to AlpacaData.pull
  - Phase-2 Unit 2 (A1b-007 / DECISIONS #164 ruling 2): adjustment="all" (total return) is the
    LOCKED equity representation, superseding C1's split-only convention. Splits AND dividends are
    adjusted at the SOURCE, so a multi-year equity backtest measures total return, not price return
    (split-only excluded dividends, understating long-horizon equity performance). Like C1 this is a
    correctness invariant (an adjustment convention, not a provenanced parameter); the per-source
    policy is declared in _ADJUSTMENT_BY_SOURCE so a new equity source cannot silently fetch raw
    (A1b-003). Adjustment is retroactive (a later split/dividend rewrites prior closes), so a
    dataset's bars are "total-return-adjusted as of the fetch date" — re-fetches propagate new
    corporate actions, which is why dataset_sha256 can change across re-fetches.
  - validate_alpaca_end raises before any network call; exit code 4
  - every print uses flush=True (Probe 4 live-log requirement)
  - final stdout line is json.dumps(result) with flush=True
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
# vectorbtpro is imported inside fetch_binance and fetch_alpaca_sip only (lane W1): importing it costs
# about 5.8 s per container (measured 2026-10-08) and the eodhd paths never use it, so a per-stock
# eodhd fetch no longer pays it.

# pre-M4 Brief A1: the durable atomic parquet write (sibling temp -> fsync -> os.replace ->
# fsync dir). Baked alongside fetch.py at /app/durable_write.py in the research-runner image,
# so this resolves off /app on sys.path when fetch.py runs in the container.
from durable_write import durable_write_parquet


ALPACA_FREE_TIER_CUTOFF = timedelta(minutes=15)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _to_vbt_ts(s: str) -> pd.Timestamp:
    """Convert an ISO-8601 string to a UTC-aware pd.Timestamp.

    Uses pd.Timestamp(s, tz='UTC') per arch (e) — NOT
    datetime.fromisoformat, which mishandles the 'Z' suffix on Python
    < 3.11 and produces tz-naive Timestamps that vbt would coerce
    inconsistently.
    """
    return pd.Timestamp(s, tz="UTC")


def _symbol_safe(symbol: str) -> str:
    """Filesystem-safe form of a symbol (BTC/USDT -> BTC_USDT)."""
    return symbol.replace("/", "_")


def _symbol_for_binance(symbol: str) -> str:
    """Binance API form (BTC/USDT -> BTCUSDT)."""
    return symbol.replace("/", "")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tf_delta(timeframe: str) -> pd.Timedelta:
    """One bar of `timeframe` as a Timedelta, for the incremental next-bar anchor."""
    table = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}
    if timeframe not in table:
        raise RuntimeError(f"unknown timeframe {timeframe!r} for incremental delta")
    return pd.Timedelta(table[timeframe])


def incremental_combine(existing_path: str, fetcher, symbol: str, timeframe: str,
                        end_ts: pd.Timestamp) -> pd.DataFrame:
    """Read the existing parquet, fetch only the gap after its last bar, return the combined frame.

    Dashboard-improvements W8 (incremental "Update to latest"). Gap-safe: it anchors on the
    EXISTING last timestamp (not a computed expectation), pulls from last + one timeframe unit to
    `end_ts`, concatenates, and de-duplicates on the index (keep last) so the boundary bar is never
    duplicated or skipped. Pure read + fetch; the caller owns the durable write and the old-file
    cleanup. Raises RuntimeError on an empty/missing existing frame.
    """
    existing = pd.read_parquet(existing_path)
    if len(existing) == 0:
        raise RuntimeError(f"existing parquet {existing_path!r} is empty; cannot append incrementally")
    last_ts = existing.index[-1]
    if last_ts.tzinfo is None:                                   # be defensive: pin to UTC if a naive index slipped in
        last_ts = last_ts.tz_localize("UTC")
    delta_start = last_ts + _tf_delta(timeframe)                # the first bar NOT already held
    if delta_start <= end_ts:
        _log(f"[fetch] incremental: existing last={last_ts.isoformat()} -> "
             f"fetching delta {delta_start.isoformat()} -> {end_ts.isoformat()}")
        delta = fetcher(symbol, timeframe, delta_start, end_ts)
    else:
        _log(f"[fetch] incremental: existing already covers up to {last_ts.isoformat()} "
             f">= end {end_ts.isoformat()}; no delta to fetch")
        delta = existing.iloc[0:0]                              # empty, same columns/dtypes
    if len(delta) and set(delta.columns) != set(existing.columns):
        # Lane D1: an eodhd daily file written before the Factor column, extended by a delta that has
        # one, would leave Factor NaN on every old row and size those bars wrongly. Refuse instead.
        raise RuntimeError(
            f"existing parquet {existing_path!r} has columns {sorted(existing.columns)} but the new bars "
            f"have {sorted(delta.columns)}; re-fetch the whole window instead of appending"
        )
    combined = pd.concat([existing, delta])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()  # drop any boundary overlap
    _log(f"[fetch] incremental: combined {len(existing)} existing + {len(delta)} delta "
         f"-> {len(combined)} rows")
    return combined


def covered_range(df: pd.DataFrame) -> tuple[int | None, int | None]:
    """The ACTUAL first-to-last bar range as (unix seconds UTC, unix seconds UTC).

    Dashboard-improvements W3 (true coverage). Returns (None, None) for an empty frame
    (df.index[0] on an empty index raises IndexError, so guard it). df.index is tz-aware
    UTC (the pull passes tz="UTC"), so .timestamp() yields correct unix seconds. Pure: no
    IO, so it is unit-testable in the research-runner image where pandas/vbt are present.
    """
    if len(df) == 0:
        return None, None
    return int(df.index[0].timestamp()), int(df.index[-1].timestamp())


def validate_alpaca_end(end_ts: pd.Timestamp) -> None:
    """Raise RuntimeError if end > now()-15min (Alpaca free tier limit).

    Runs BEFORE any auth or network call, so a bad job fails fast and
    cheaply.
    """
    now = pd.Timestamp(datetime.now(tz=timezone.utc))
    cutoff = now - ALPACA_FREE_TIER_CUTOFF
    if end_ts > cutoff:
        raise RuntimeError(
            "alpaca_sip free tier requires end <= now()-15min; "
            f"end={end_ts.isoformat()} now={now.isoformat()} "
            f"cutoff={cutoff.isoformat()}"
        )


def fetch_binance(symbol: str, timeframe: str,
                  start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    import vectorbtpro as vbt  # lazy, lane W1: only the binance and alpaca paths use vbt

    api_symbol = _symbol_for_binance(symbol)
    _log(f"[fetch] binance: pulling {api_symbol} {timeframe} "
         f"{start_ts.isoformat()} -> {end_ts.isoformat()}")
    data = vbt.BinanceData.pull(
        api_symbol,
        timeframe=timeframe,
        start=start_ts,
        end=end_ts,
        tz="UTC",
    )
    df = data.get()
    _log(f"[fetch] binance: got {len(df)} rows")
    return df


def fetch_alpaca_sip(symbol: str, timeframe: str,
                     start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    import vectorbtpro as vbt  # lazy, lane W1: only the binance and alpaca paths use vbt

    validate_alpaca_end(end_ts)

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        raise RuntimeError(
            "alpaca_sip requires ALPACA_API_KEY and ALPACA_SECRET_KEY env"
        )

    _log("[fetch] alpaca_sip: configuring auth")
    vbt.AlpacaData.set_custom_settings(
        client_config=dict(api_key=api_key, secret_key=secret_key),
    )

    _log(f"[fetch] alpaca_sip: pulling {symbol} {timeframe} "
         f"{start_ts.isoformat()} -> {end_ts.isoformat()}")
    data = vbt.AlpacaData.pull(
        symbol,                              # the raw equity ticker, e.g. 'AAPL'
        timeframe=timeframe,                 # '1d' for the locked daily granularity
        start=start_ts,                      # inclusive window start (UTC)
        end=end_ts,                          # exclusive window end (UTC)
        feed="sip",                          # the paid SIP consolidated feed (v4 fix gamma)
        adjustment=_ADJUSTMENT_BY_SOURCE["alpaca_sip"],  # Phase-2 Unit 2: "all" = total return (splits +
                                             # dividends) at the SOURCE, the locked equity default (#164
                                             # ruling 2 / A1b-007). Read from the per-source policy map (not
                                             # a bare literal) so the policy and the call cannot drift; vbt's
                                             # default None would let the Alpaca raw default govern.
        tz="UTC",                            # localize the returned index to UTC
    )
    df = data.get()
    _log(f"[fetch] alpaca_sip: got {len(df)} rows")
    return df


# M5 data-store: the default container path where the worker bind-mounts the EODHD daily tree (one
# parquet per ticker), read-only. The EODHD_DAILY_ROOT env var overrides it, read at CALL time inside
# fetch_eodhd so the override is authoritative (a probe pointing at a temp fixture dir just sets the env).
_EODHD_DAILY_ROOT_DEFAULT = "/eodhd-daily"


_XNYS_SESSIONS = None  # lane D1b: the XNYS session dates, built once per process on first use


def _xnys_session_dates() -> pd.DatetimeIndex:
    """Every New York Stock Exchange session date from 1900-01-01 to exchange_calendars' last known
    session, as naive midnight timestamps (restates research_lab/universe/data_check._session_dates)."""
    global _XNYS_SESSIONS
    if _XNYS_SESSIONS is None:
        import exchange_calendars  # lazy: only the eodhd daily path needs it

        calendar = exchange_calendars.get_calendar("XNYS", start="1900-01-01")
        _XNYS_SESSIONS = pd.DatetimeIndex(calendar.sessions).tz_localize(None).normalize()
    return _XNYS_SESSIONS


def _data_check_flags(raw: pd.DataFrame) -> "dict[str, list[str]]":
    """The data check's four fact flags on raw EODHD daily rows, as {flag: [dates]} for each flag
    present. Restates research_lab/universe/data_check.py (flag names, the 999999.9999 ceiling, the
    comparisons, the XNYS calendar) because this image cannot import it; research_lab/probes/data_check
    pins the two equal. A bar date outside the calendar's known range raises rather than reading as
    open or closed."""
    dates = pd.to_datetime(raw["date"])
    if dates.dt.tz is not None:
        dates = dates.dt.tz_localize(None)
    dates = dates.dt.normalize()
    sessions = _xnys_session_dates()
    if dates.isna().any():  # a missing date is not a closed day; refuse rather than give the wrong reason
        raise ValueError(f"eodhd: {int(dates.isna().sum())} bar(s) carry no date; cannot judge them")
    if len(dates) and (dates.min() < sessions[0] or dates.max() > sessions[-1]):
        raise ValueError(
            f"eodhd: bar dates {dates.min().date()} to {dates.max().date()} fall outside the XNYS calendar's "
            f"known range {sessions[0].date()} to {sessions[-1].date()}; cannot tell open from closed days"
        )
    masks = {
        "adjusted_close_at_ceiling": raw["adjusted_close"] >= 999999.9999,
        "non_positive_close": raw["close"] <= 0,
        "high_below_low": raw["high"] < raw["low"],
        "bar_on_closed_day": ~dates.isin(sessions),  # lane D1b: a bar on a day the NYSE held no session
    }
    return {flag: raw.loc[mask, "date"].astype(str).tolist() for flag, mask in masks.items() if bool(mask.any())}


def fetch_eodhd(symbol: str, timeframe: str,
                start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """Read a pre-downloaded EODHD daily parquet and produce a total-return OHLCV frame.

    EODHD daily files carry RAW as-traded open/high/low/close PLUS a total-return adjusted_close.
    A naive Close=adjusted_close with raw open/high/low would yield internally inconsistent bars (a
    4x discontinuity at a split). So we back-adjust open/high/low by the per-day total-return factor
    (adjusted_close/close) — within one day all four prices share one factor — and set Close to the
    adjusted_close directly. This reconstruction matches alpaca_sip adjustment="all" OHLC to ~0.003-
    0.005% (verified). Volume is EODHD's RAW as-traded volume, kept as-is: cross-source volume differs
    and the daily file lacks the split history to split-adjust it (flagged; the split-adjusted-volume
    unit closes this). Daily-only (the EODHD daily store); no network — a local file read.
    Row `3.4d` (DECISIONS #428/#429): a non-positive close refuses this call only when it falls
    INSIDE the requested [start_ts, end_ts) window, named by date, never for a bad row anywhere
    else in the ticker's own full history. No stored bar is ever repaired, dropped, forward-filled
    or rewritten by this change; the window the call returns is byte-identical to before whenever
    that window itself carries no bad close.

    Lane D1 (2026-10-07): the window is refused on ANY of the data check's three fact flags, named by
    flag and date: adjusted_close at or above EODHD's 999999.9999 ceiling, close at or below zero, high
    below low. The definitions live in research_lab/universe/data_check.py; this image cannot import it,
    so the three conditions are restated in _data_check_flags below and research_lab/probes/data_check
    pins that both give the same answer on one fixture. Lane D1b (2026-10-08) adds the fourth flag,
    bar_on_closed_day: a bar dated on a day the NYSE held no session, from exchange_calendars' XNYS. The output also carries `Factor`, the per-day
    adjustment factor adjusted_close / close, so sizing can count real shares at the price traded
    (Close / Factor is the as-traded close).
    """
    if timeframe != "1d":  # this adapter serves the EODHD daily store only (intraday is a separate unit)
        raise ValueError(f"eodhd adapter supports timeframe '1d' only, got {timeframe!r}")
    root = os.environ.get("EODHD_DAILY_ROOT", _EODHD_DAILY_ROOT_DEFAULT)  # call-time read; env is authoritative
    path = os.path.join(root, f"{symbol}.US.parquet")  # per-ticker file; the path IS the index
    if not os.path.exists(path):  # fail loud: the ticker is not in the EODHD daily store (never a silent empty)
        raise FileNotFoundError(
            f"eodhd: no daily file for symbol {symbol!r} at {path} (EODHD_DAILY_ROOT={root})"
        )
    _log(f"[fetch] eodhd: reading {path}")
    raw = pd.read_parquet(path)  # columns: date, open, high, low, close, adjusted_close, volume
    missing_cols = [c for c in ("date", "open", "high", "low", "close", "adjusted_close", "volume") if c not in raw.columns]
    if missing_cols:  # a malformed parquet (e.g. date as the index) -> fail loud with context, not a cryptic KeyError
        raise ValueError(f"eodhd: {symbol} parquet at {path} missing columns {missing_cols}; got {list(raw.columns)}")
    # Row `3.4d` (DECISIONS #428/#429): clamp to the requested [start, end) window BEFORE testing
    # for a non-positive close, never after. Before this fix the whole file was tested first, so
    # one bad row ANYWHERE in a ticker's history (e.g. AEL's own 2024-05-13/14 rows) refused every
    # window for that ticker, even a window years earlier that never reads those rows at all. Now
    # a bad close outside the requested window is simply never looked at by this guard, because the
    # rows it could poison are already excluded from `windowed` below -- no stored bar is repaired,
    # dropped or rewritten, only the ORDER of "slice" and "check" changed.
    raw_ts = pd.to_datetime(raw["date"], utc=True)  # the file's own dates as a tz-aware UTC Series, for the window test only
    windowed = raw[(raw_ts >= start_ts) & (raw_ts < end_ts)]  # the exact rows this call will ever return
    flags = _data_check_flags(windowed)
    if flags:  # lane D1: any data check flag INSIDE the requested window refuses the fetch, named
        detail = "; ".join(f"{flag} on {dates[:5]}" + (f" and {len(dates) - 5} more" if len(dates) > 5 else "")
                           for flag, dates in flags.items())
        if "non_positive_close" in flags:  # kept word for word: callers and probes grep this phrase
            detail += " (non-positive close: cannot back-adjust)"
        raise ValueError(f"eodhd: {symbol} window fails the data check: {detail}; refusing")
    factor = windowed["adjusted_close"] / windowed["close"]  # the day's cumulative total-return (split x dividend) factor, computed only over the window now returned
    out = pd.DataFrame({
        "Open": windowed["open"] * factor,    # back-adjusted to the total-return basis (matches alpaca adjustment=all)
        "High": windowed["high"] * factor,    # same per-day factor
        "Low": windowed["low"] * factor,      # same per-day factor
        "Close": windowed["adjusted_close"],  # the total-return close directly (#164 ruling 2 representation)
        "Volume": windowed["volume"],         # EODHD raw as-traded volume (flagged; not split-adjusted)
        "Factor": factor,                     # lane D1: adjusted / as-traded, so sizing can count real shares
    })
    out.index = pd.to_datetime(windowed["date"], utc=True)  # tz-aware UTC DatetimeIndex (date string -> midnight UTC)
    out.index.name = "date"  # the time axis the worker read contract consumes (name is cosmetic)
    _log(f"[fetch] eodhd: {len(out)} rows in window")
    return out


SOURCE_DISPATCH = {
    "binance": fetch_binance,
    "alpaca_sip": fetch_alpaca_sip,
    "eodhd": fetch_eodhd,
}

# Phase-2 Unit 2 (A1b-003): the per-source corporate-action adjustment policy. EVERY source must
# declare its adjustment here — "all" = total return (splits + dividends, the locked equity default),
# None = no adjustment (correct for crypto, which has no splits or dividends). fetch_alpaca_sip reads
# its value from this map rather than a bare literal, so the policy and the fetch call cannot drift.
_ADJUSTMENT_BY_SOURCE = {
    "binance": None,          # crypto: no corporate actions, so no adjustment concept applies
    "alpaca_sip": "all",      # US equities: total return (splits + dividends) — #164 ruling 2 / A1b-007
    "eodhd": "all",           # US equities: total return — fetch_eodhd produces it from adjusted_close by construction
}

# The guard A1b-003 owes: a new source added to SOURCE_DISPATCH without declaring its adjustment policy
# fails the module import LOUD (naming the offending source), so a future equity source can never
# silently fetch raw. The two key sets must be identical.
_undeclared = set(SOURCE_DISPATCH) - set(_ADJUSTMENT_BY_SOURCE)
_unknown = set(_ADJUSTMENT_BY_SOURCE) - set(SOURCE_DISPATCH)
if _undeclared or _unknown:
    raise RuntimeError(
        "fetch.py adjustment-policy mismatch: every SOURCE_DISPATCH source must declare an "
        f"_ADJUSTMENT_BY_SOURCE policy. Sources missing a policy: {sorted(_undeclared)}; "
        f"policies for unknown sources: {sorted(_unknown)}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fetch.py")
    parser.add_argument("--source", required=True, choices=sorted(SOURCE_DISPATCH))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--start", required=True, help="ISO-8601 UTC")
    parser.add_argument("--end", required=True, help="ISO-8601 UTC")
    parser.add_argument("--out-root", required=True, help="container path, e.g. /data")
    # W8 "Update to latest": extend an existing parquet to a later end. --existing is the container
    # path of the current file. PX4: a source that re-bases past prices (eodhd, alpaca_sip) gets the
    # whole window re-cut; one that does not (binance) gets only the gap appended.
    parser.add_argument("--existing", required=False, default=None,
                        help="container path to the existing parquet (required for --mode append)")
    parser.add_argument("--mode", required=False, default="fetch", choices=("fetch", "append"),
                        help="fetch = whole window (default); append = extend --existing: the whole window "
                             "re-cut for a source that adjusts past prices, only the gap appended otherwise")
    args = parser.parse_args(argv)

    _log(f"[fetch] start: source={args.source} symbol={args.symbol} "
         f"timeframe={args.timeframe} start={args.start} end={args.end}")

    # Set only if the '1h' hourly branch below actually runs this process (never on a daily,
    # binance or alpaca run), so the finally block at the end of this function can report the
    # hourly adapter's two per-run monitors without ever importing eodhd_hourly on a run that
    # never touched it.
    hourly_module = None

    try:
        start_ts = _to_vbt_ts(args.start)
        end_ts = _to_vbt_ts(args.end)

        if args.source == "eodhd" and args.timeframe == "1h":
            # M5.x hourly-pivot job A: the hourly research adapter lives in its own pandas-only
            # module, eodhd_hourly.py, because this file once imported vectorbtpro at module level and
            # the adapter must ALSO be importable on the bare probe interpreter (research_lab/
            # CLAUDE.md trap 1). fetch_eodhd itself (the '1d' path, just above) stays byte-unchanged
            # (AC-7): this branch is the ONLY place '1h' routing lives in fetch.py.
            import eodhd_hourly  # local import: keeps the pandas-only module decoupled from vbt
            hourly_module = eodhd_hourly  # remember it so the finally block can report its counters
            fetcher = eodhd_hourly.fetch_eodhd_hourly
        else:
            fetcher = SOURCE_DISPATCH[args.source]
        if args.mode == "append":
            # W8 "Update to latest": --start/--end are the NEW requested window (original start, extended
            # end); --existing is the current file, removed below once the new one is durable.
            if not args.existing:
                raise RuntimeError("--mode append requires --existing")
            if _ADJUSTMENT_BY_SOURCE[args.source] is None:
                # No adjustment (binance): past bars never change, so only the gap is fetched and
                # appended. Combined in-container (the worker venv is bare).
                df = incremental_combine(args.existing, fetcher, args.symbol, args.timeframe, end_ts)
            else:
                # PX4 (Tamer 2026-11-09): this source re-bases past prices after dividends and splits, so
                # the whole window is re-cut, never appended: new bars on an old copy show a false jump.
                df = fetcher(args.symbol, args.timeframe, start_ts, end_ts)
        else:
            df = fetcher(args.symbol, args.timeframe, start_ts, end_ts)

        out_dir = (Path(args.out_root) / args.source
                   / _symbol_safe(args.symbol) / args.timeframe)
        out_dir.mkdir(parents=True, exist_ok=True)
        start_epoch = int(start_ts.timestamp())
        end_epoch = int(end_ts.timestamp())
        out_path = out_dir / f"{start_epoch}_{end_epoch}.parquet"

        _log(f"[fetch] writing parquet to {out_path} (durable atomic replace)")
        # pre-M4 Brief A1 / arch §5: durable atomic write replaces the prior in-place
        # df.to_parquet(out_path). An interrupted/power-lost re-fetch can no longer leave a
        # partial file at out_path while the catalog row still points at it — restore's
        # presence check (researchdb.restore_dataset) stays honest because a non-null
        # file_path now always denotes a complete file.
        durable_write_parquet(df, out_path)

        # W8 / D8-3 window-named-rewrite cleanup, fetcher-owned, in this exact order so no window
        # ever points at a deleted or half-written file: the new file is durably written ABOVE;
        # confirm it exists with size > 0; only THEN remove the OLD file (when the extended end
        # gave it a new name). Fail the job loudly if the unlink raises — the new file is already
        # durable, so a left-behind old file is harmless dead weight, never silent data loss.
        if args.mode == "append" and args.existing and str(out_path) != args.existing:
            if not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError(
                    f"new file {out_path} missing or empty after durable write; "
                    "refusing to remove the old file"
                )
            try:
                Path(args.existing).unlink()
                _log(f"[fetch] incremental: removed superseded old file {args.existing}")
            except FileNotFoundError:
                pass                                            # already gone; idempotent, harmless
            except OSError as e:
                raise RuntimeError(
                    f"failed to remove old file {args.existing} after writing {out_path}: {e}"
                )

        _log("[fetch] hashing parquet")
        sha256 = _sha256_file(out_path)
        file_bytes = out_path.stat().st_size

        result = {
            "rows": int(len(df)),
            "sha256": sha256,
            "file_path": str(out_path),
            "file_bytes": file_bytes,
        }
        # Dashboard-improvements W3 (true coverage): capture the ACTUAL first-to-last bar
        # range so the catalog can show real coverage, not the requested window. The keys
        # MUST be exactly covered_start_ts/covered_end_ts — the worker posts this dict
        # verbatim and DatasetWorkerResultIn silently drops any unknown key (a name drift
        # would write NULL forever with no error). Omit the keys on an empty frame (NULL).
        covered_start_ts, covered_end_ts = covered_range(df)
        if covered_start_ts is not None:
            result["covered_start_ts"] = covered_start_ts
            result["covered_end_ts"] = covered_end_ts
        print(json.dumps(result), flush=True)
        return 0

    except RuntimeError as e:
        # 15-min cutoff and other contract violations
        msg = f"RuntimeError: {e}"
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": msg}), flush=True)
        if "alpaca_sip free tier requires" in str(e):
            return 4
        return 1
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": msg}), flush=True)
        return 1

    finally:
        # Job A tail fix, extended by M5 fetcher job 10 (lane R, gate-review fix B1): report the
        # hourly adapter's THREE per-run operational monitors, hourly_adapter_refusals,
        # hourly_uncorrected_jumps and reader_latency_counter, at the true end of this run,
        # whichever branch above returned or raised. A refusal always raises before the success
        # return above, so the failure path is the only place a refusal count is ever seen; the
        # finally clause runs on every exit from this function, so all three are covered. Every
        # one of the three functions already prints nothing when its own counter is still zero,
        # so calling them unconditionally here adds no noise to a clean run. Only runs when the
        # '1h' branch above actually executed this process (hourly_module set), so a daily,
        # binance or alpaca run never imports or touches eodhd_hourly at all.
        #
        # Round 3, root-cause dispatch: a bare call here is dangerous. A `finally` block that
        # raises DISCARDS the return value Python already computed above, so a fully successful
        # run whose data is written and whose result JSON is already on stdout would crash
        # instead of returning 0. And if the try block above had already raised a real,
        # debuggable error, that error would be silently replaced by an unrelated crash from a
        # MONITOR: code that exists only to REPORT on the run, never to decide its outcome. So
        # each call below sits in its own try/except, deliberately not sharing one with the
        # other: one monitor failing must not also stop the second monitor's own report from
        # running. OSError is the specific type caught: both functions do nothing but format a
        # string and print it, so once the module's own counting logic is trusted, the only
        # realistic failure left is the print call's own I/O failing (for example a broken pipe
        # if the worker reading this process's stdout has already gone while the container tears
        # down). The diagnostic below goes to stderr, not through _log() (which prints to the
        # same stdout that may be exactly what just failed).
        #
        # Round 5, correcting round 3's own claim here: round 3 said stderr is "a separate
        # stream" from stdout, so the diagnostic write below could not itself fail from the same
        # cause. That is FALSE for the only real invocation path: research_lab/worker/jobs/
        # fetch_dataset.py spawns this container with stderr=asyncio.subprocess.STDOUT, so the
        # worker MERGES this process's stderr onto the very same pipe as its stdout. So the exact
        # broken-pipe scenario that made a reporter's own call raise OSError above breaks this
        # diagnostic print too, on the same pipe, and an uncaught OSError here would still escape
        # this `finally` block and skip the other monitor -- recreating the very bug this fix
        # exists to close, from inside its own diagnostic path. Each diagnostic print below is
        # therefore wrapped in its own further try/except OSError, and swallows silently on
        # failure: once the only sink for the diagnostic is itself broken, there is no channel
        # left to report through, and crashing an otherwise-successful run (data already durably
        # written, result JSON already on stdout) just to signal that one monitor line could not
        # be printed would be strictly worse than staying quiet.
        if hourly_module is not None:
            try:
                hourly_module.report_refusals()
            except OSError as e:
                try:
                    print(f"[fetch] hourly_adapter_refusals monitor failed to report: "
                          f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                except OSError:
                    # The diagnostic write itself failed too (see the comment above this block):
                    # nothing further can be done, so this is swallowed on purpose.
                    pass
            try:
                hourly_module.report_uncorrected_jumps()
            except OSError as e:
                try:
                    print(f"[fetch] hourly_uncorrected_jumps monitor failed to report: "
                          f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                except OSError:
                    # Same reasoning as the report_refusals() branch above: the sink is broken
                    # too, so there is nothing left to report through.
                    pass
            # M5 fetcher job 10 (lane R), gate-review BLOCKER B1: report_reader_latency() was
            # defined and its own counter wired since job 10's own build, but nothing ever called
            # the reporter itself, so the timing it faithfully accumulated in-process was thrown
            # away the instant this process exited -- "a declared failure mode with no monitor,"
            # the sprint contract's own BLOCKER wording, on this file's third end-of-run reporter.
            # Wired here in its OWN try/except OSError, matching the two siblings above exactly
            # (never sharing one try with either of them): a bare, unprotected third call would
            # reopen the very escape the round-3/round-5 fix above closed for the first two, since
            # this reporter's own print call can fail from the identical broken-pipe cause (the
            # worker merges this process's stderr onto the same pipe as stdout, so a reader going
            # away breaks every print on this path alike, not only the first two).
            try:
                hourly_module.report_reader_latency()
            except OSError as e:
                try:
                    print(f"[fetch] reader_latency_counter monitor failed to report: "
                          f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
                except OSError:
                    # Same reasoning as the two branches above: the sink is broken too, so there
                    # is nothing left to report through.
                    pass


if __name__ == "__main__":
    sys.exit(main())
