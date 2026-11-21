"""Pure window-generation functions (brief §5.2, §5.3, §5.1).

No I/O, no state — every function maps inputs to a list of (epoch, epoch) spans,
so the probe can assert tiling/cap/year-alignment without a network. Two grains:
- 1-min DEEP-era windows are MANIFEST units (one parquet file each, year-aligned).
- recent-era 5m/1h spans are INTERNAL sub-requests of a single whole-history unit.
All API bounds are inclusive (EODHD `from`/`to` include the endpoints), so an
exclusive tiling boundary is converted with `api_to = boundary - 1`.
"""

from datetime import datetime, timezone  # derive year boundaries from an epoch

from .config import CAP_1M_DAYS, CAP_5M_DAYS, SECONDS_PER_DAY  # the published span caps


def _year_start_epoch(year: int) -> int:
    """Epoch seconds of Jan 1 00:00:00 UTC of `year` (the calendar-year boundary)."""
    return int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())  # UTC midnight Jan 1


def year_of(ts: int) -> int:
    """The UTC calendar year a given epoch falls in (used to place a 1-min file)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).year  # UTC year


def year_aligned_1m_windows(floor_ts: int, end_ts: int):
    """Tile [floor_ts, end_ts) into 1-min fetch windows, each a SEPARATE manifest unit/file.

    Each window is at most CAP_1M_DAYS (120 days, the hard cap) AND never crosses a
    calendar-year boundary, so every window's bars live in exactly one year directory
    (`1min/{ticker}/{year}/{window_start}.parquet`) — one atomic file, no straddle, no merge.
    Returns a list of (window_start, window_end_exclusive). The caller derives the API
    inclusive bound as window_end_exclusive - 1 and the year as year_of(window_start).
    """
    if floor_ts is None or floor_ts >= end_ts:  # no coverage, or floor at/after the deep-era end
        return []  # nothing to fetch
    windows = []  # accumulate (start, end_exclusive)
    cursor = floor_ts  # the lower edge of the next window
    cap = CAP_1M_DAYS * SECONDS_PER_DAY  # 120 days in seconds
    while cursor < end_ts:  # walk forward to the deep-era end
        next_year_boundary = _year_start_epoch(year_of(cursor) + 1)  # start of the next calendar year
        boundary = min(cursor + cap, next_year_boundary, end_ts)  # the smallest of: cap, year-end, end
        windows.append((cursor, boundary))  # half-open [cursor, boundary); all bars in year_of(cursor)
        cursor = boundary  # the next window begins exactly where this one ends (no gap, no overlap)
    return windows  # one entry per (manifest unit / parquet file)


def recent_5m_subwindows(start_ts: int, now_ts: int):
    """Internal ≤600-day sub-requests for the single recent-era 5-min unit.

    5-min is a whole-history manifest unit written to ONE file `5min/{ticker}.parquet`;
    this only splits the fetch into API-cap-sized requests that are concatenated before
    the single atomic write. Returns inclusive (from_ts, to_ts) pairs.
    """
    if start_ts >= now_ts:  # nothing recent to fetch
        return []
    spans = []  # accumulate inclusive (from, to)
    cursor = start_ts  # lower edge
    cap = CAP_5M_DAYS * SECONDS_PER_DAY  # 600 days in seconds
    while cursor < now_ts:  # tile up to now
        to = min(cursor + cap, now_ts)  # inclusive upper edge, capped at now
        spans.append((cursor, to))  # one sub-request
        cursor = to + 1  # next sub-request starts one second later (no overlap on the inclusive API)
    return spans  # internal sub-requests for one 5m unit


def recent_1h_span(start_ts: int, now_ts: int):
    """The single recent-era 1-hour request span (the 7,200-day cap covers 2020->now).

    Returns inclusive (from_ts, to_ts), or None if there is no recent range. The whole
    recent 1h history fits one request, so the 1h unit is a single file, one request.
    """
    if start_ts >= now_ts:  # no recent range
        return None
    return (start_ts, now_ts)  # one inclusive span (the 7200-day cap is not exceeded by 2020->now)


def anchored_probe_windows(now_ts: int, anchors_years_back, probe_days: int = CAP_1M_DAYS):
    """Coverage-discovery probe windows: a few 120-day windows at fixed look-back anchors.

    For each anchor (years before now) we test one 120-day window; the earliest one that
    returns bars brackets the ticker's 1-min floor (the smoke findings' method). Returns
    inclusive (from_ts, to_ts) pairs, deepest anchor first (so the walk finds the floor fast).
    """
    window = probe_days * SECONDS_PER_DAY  # the 120-day probe width in seconds
    spans = []  # accumulate inclusive (from, to)
    for years_back in sorted(anchors_years_back, reverse=True):  # deepest first
        to = now_ts - int(years_back * 365.25 * SECONDS_PER_DAY)  # the anchor's upper edge
        frm = to - window  # the 120-day window below it
        if to <= 0:  # an anchor older than the epoch is meaningless; skip
            continue
        spans.append((frm, to))  # one probe window
    return spans  # deepest-first probe windows for coverage discovery
