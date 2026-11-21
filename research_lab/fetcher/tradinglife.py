"""A ticker's real trading life, derived from the daily history already on disk.

Why this exists. A recent-era intraday unit always asks EODHD for the same fixed window
(2020-01-01 to now), split into 600-day sub-requests at 5 calls each, so a 5-minute unit
costs 25 calls whether or not the ticker traded in that window at all. The daily end-of-day
history for every ticker is already on the SSD and cost nothing extra, and it says exactly
when the ticker first and last traded. Asking for intraday bars outside that span buys
nothing.

It is not only a cost question. EODHD's intraday endpoint does NOT honour the retired-symbol
suffix that its end-of-day endpoint honours: `/eod/MSII_old.US` returns the company that died
in 2014, while `/intraday/MSII_old.US` returns the company trading under MSII today. Measured
2026-08-17 on the store: `MSII_old.US` daily ends 2014-03-24, yet its 5-minute file opens at
2025-06-04 13:30:00, the same first bar as live `MSII.US`. So a request whose window cannot
overlap the ticker's own trading life does not come back empty, it comes back with a DIFFERENT
COMPANY'S BARS filed under this ticker's name. Refusing those requests keeps that contamination
out of the store as well as saving the calls.

Everything here reads; nothing writes.
"""

import os                                          # locate the per-ticker daily parquet
from datetime import datetime, timedelta, timezone  # epoch to UTC date, and the finality margin

import pyarrow.lib                                 # the parquet read error type
import pyarrow.parquet as pq                       # read only the date column

from .storage import relative_path                 # the one place raw paths are defined

# A ticker still trading when we recorded its daily history has a last bar within a few days of
# that fetch (weekends and holidays). A last bar older than this margin means the ticker was
# already dead when we looked, so its daily record is FINAL and the top of the window can be
# trimmed. Without this test a stale daily store (ours is frozen at 2026-06-18) would look like
# every live ticker had stopped trading.
FINALITY_MARGIN_DAYS = 7  # backtest parameter (DECISIONS #204/#251): decides whether a window's top is trimmed; data_sources.eodhd_intraday_finality_margin_days


class TradingLifeUnreadable(RuntimeError):
    """The daily parquet exists but could not be read. Surfaced, never silently ignored."""


class NoTradingOverlap(Exception):
    """The requested window lies wholly outside the ticker's trading life, so it is never requested."""


def _utc_date(ts: int) -> str:
    """Render an epoch second as the 'YYYY-MM-DD' UTC date string the daily rows use."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")  # match EODHD's date form


def daily_date_span(staging_root: str, ticker: str):
    """Return (first_date, last_date) from the ticker's daily parquet, or None when there is none.

    Touches only the filesystem, never the manifest, so the caller can run it in a worker thread.
    That matters: one daily file costs 200-450 ms on the USB SSD, and doing that on the event loop
    serialises the whole 16-worker pool behind a single read.
    """
    path = os.path.join(staging_root, relative_path("daily", ticker=ticker))  # the per-ticker EOD file
    if not os.path.exists(path):                              # no daily history recorded for this name
        return None                                           # no evidence -> caller fetches everything
    try:
        table = pq.read_table(path, columns=["date"])         # only the date column leaves the disk
    except (OSError, pyarrow.lib.ArrowInvalid) as exc:        # corrupt or unreadable file
        raise TradingLifeUnreadable(f"{ticker}: cannot read daily history at {path}: {exc!r}") from exc
    if table.num_rows == 0:                                   # a written-but-empty daily table
        return None                                           # treat as no evidence
    dates = table.column("date").to_pylist()                  # 'YYYY-MM-DD' strings, sortable as text
    return min(dates), max(dates)                             # the recorded trading life


def daily_fetched_at(conn, ticker: str):
    """Return the epoch at which the ticker's daily unit was written, or None. Manifest only."""
    row = conn.execute(                                       # cheap: the primary key leads with ticker
        "SELECT fetched_at FROM fetch_unit WHERE ticker=? AND interval='daily'", (ticker,)).fetchone()
    return row[0] if row and row[0] else None                 # None = no stamp, so finality is unprovable


def trading_life_from(span, fetched_at):
    """Combine a date span with its fetch stamp into (first, last, last_is_final). Pure."""
    if span is None:                                          # no daily evidence at all
        return None                                           # caller requests the full window
    first, last = span                                        # the recorded trading life
    if fetched_at is None:                                    # no fetch stamp -> cannot claim finality
        return first, last, False                             # bottom trim still applies, top does not
    fetched = datetime.fromtimestamp(fetched_at, timezone.utc)  # the day we recorded the daily history
    cutoff = (fetched - timedelta(days=FINALITY_MARGIN_DAYS)).strftime("%Y-%m-%d")  # the finality test
    return first, last, last < cutoff                         # dead well before we looked = final


def trading_life(staging_root: str, conn, ticker: str):
    """Blocking convenience wrapper over the two halves above (probes, scripts, tests)."""
    return trading_life_from(daily_date_span(staging_root, ticker), daily_fetched_at(conn, ticker))


def _drop_before_first(spans, first):
    """The BOTTOM rule alone: drop a span that closes before the ticker's first recorded bar.

    Written once here so both public functions below share it rather than each re-implementing
    the same predicate (brief S3.2 step 3: composed, not copied).
    """
    return [(frm, to) for frm, to in spans if _utc_date(to) >= first]  # keep only spans that could still hold bars


def _drop_after_final_end(spans, last, last_is_final):
    """The TOP rule alone: drop a span that opens after a FINAL last bar. A stale (non-final) last
    bar drops nothing, because finality is unprovable for it (see trading_life_from).

    Written once here so both public functions below share it (brief S3.2 step 3: composed, not
    copied): this is the contamination guard's top half, and a second copy of it anywhere in the
    tree is exactly the drift this split exists to prevent.
    """
    if not last_is_final:                                     # stale record: never trim the top
        return list(spans)
    return [(frm, to) for frm, to in spans if _utc_date(frm) <= last]  # drop spans opening after death


def spans_overlapping_life(spans, life):
    """Keep only the (from_ts, to_ts) spans that can contain bars belonging to THIS ticker.

    `life` is the tuple from `trading_life`, or None for 'no evidence, keep everything'. Applies
    BOTH trims (bottom then top): a span is dropped when it closes before the ticker's first bar,
    or (only when the last bar is final) when it opens after the ticker's last bar. This is the
    RECENT-phase rule, over continuous tiling windows where both ends are safe to trim.
    """
    if life is None:                                          # no daily evidence -> request as before
        return list(spans)
    first, last, last_is_final = life                         # the recorded trading life
    return _drop_after_final_end(_drop_before_first(spans, first), last, last_is_final)  # bottom then top


def spans_before_final_end(spans, life):
    """Keep only the (from_ts, to_ts) spans that open at or before a FINAL last bar (TOP rule only).

    Used by deep discovery (coverage.py -> discover_floor), which must NOT apply the bottom rule:
    discovery's anchors are sparse probe windows, and the anchor immediately deeper than the first
    hit, already probed and found EMPTY, is the buffer the returned floor rests on (brief S3.2).
    Dropping anchors before the walk would delete that buffer. `life` of None means no evidence,
    so every anchor survives, matching `spans_overlapping_life`'s no-evidence behaviour.
    """
    if life is None:                                          # no daily evidence -> probe every anchor
        return list(spans)
    _first, last, last_is_final = life                        # first is deliberately unused: no bottom trim here
    return _drop_after_final_end(spans, last, last_is_final)   # top rule alone


def no_overlap_reason(ticker: str, interval: str, frm: int, to: int, life) -> str:
    """Build the fixed NoTradingOverlap reason string (brief S3.3 item 5), every date YYYY-MM-DD UTC.

    Applies to all three intraday branches (1h, 5min, 1min) so a skipped unit can be re-judged
    later without re-deriving anything. Only called once a span has already been emptied by the
    narrowing above, so `life` is never None here: an empty result requires daily evidence to have
    produced it.
    """
    first, last, last_is_final = life                         # the trading life that emptied this span
    final_word = "true" if last_is_final else "false"          # the template's literal true/false token
    return (f"{ticker} {interval}: requested {_utc_date(frm)}..{_utc_date(to)} "
            f"lies outside trading life {first}..{last} (final={final_word})")
