"""The hourly research adapter (M5.x hourly-pivot campaign, job A).

This module is DELIBERATELY separate from fetch.py: fetch.py imports vectorbtpro at module level
(research_lab/CLAUDE.md trap 1), so nothing that imports fetch.py can load on a bare interpreter.
This module stays pandas-only and host-importable on purpose, so the build's own probes (which run
on research_lab/worker/.venv, which carries pandas/pyarrow/numpy but no vectorbtpro) can exercise it
directly. fetch.py routes timeframe '1h' to fetch_eodhd_hourly() from inside main(), never by
touching fetch_eodhd itself (AC-7: fetch_eodhd stays byte-unchanged).

Read a pre-downloaded EODHD hourly parquet and produce a total-return OHLCV frame, corrected for
corporate actions and folded to remove the phantom session-closing bar.

The correction reuses fetch_eodhd's own proven arithmetic: each trading day carries one total-return
factor (that day's adjusted_close / close, read from the SAME EODHD daily store, keyed on the NEW
YORK trading date -- not the UTC date, since a bar near UTC midnight can sit on a different calendar
day in New York than in UTC). Open, High, Low and Close are all multiplied by that day's factor.
Volume passes through as EODHD's RAW as-traded volume, unadjusted -- never multiplied by the factor,
exactly like fetch_eodhd's own docstring flags for the daily file (cross-source volume differs and
there is no split history here either to split-adjust it).

The hourly store carries a stitching artefact the daily file does not: most trading days end with an
extra bar whose open, high, low and close are all equal and whose volume is null -- not a real trade,
but (measured 2026-08-24 against the real AAPL file) its close matches that day's own official daily
close on 99.8-100 pct of days. Three cases, all folded or dropped by ONE pass (AC-3's fold pass,
never a separate alignment step):

  1. The session-close phantom (this flat/null-volume bar sits AFTER every real bar of its day): its
     close is folded onto the last real bar of that day (taking the phantom's own close as that real
     bar's new close), and the phantom row is then removed. It is never simply dropped, because it
     carries the day's real, official closing information.
  2. A mid-session stub (a flat/null-volume bar with at least one REAL bar still to come that day,
     e.g. AAPL's own 10:06 and 13:21 New York bars): folding this one the naive way would OVERWRITE a
     real, volume-backed bar's close with an unproven stub value -- exactly the "wrong results...
     data corruption" risk this whole build exists to avoid. So it is REMOVED instead, without
     touching any neighbouring bar's close.
  3. A lone-stub day (the day's ONLY row is itself a flat/null-volume stub, with no real bar at all
     that day to fold into -- measured 2026-08-24: AAPL carries exactly four such days,
     2025-10-28 through 2025-10-31): the day is DROPPED whole, since there is nothing real to fold
     into and nothing real to keep.

This same AC-3 fold pass answers AC-4's off-grid question too: AAPL's two measured off-grid bars
(10:06 and 13:21 New York) are both mid-session stubs under case 2 above, so they are REMOVED, not
aligned to the one-hour grid -- there is no separate alignment step anywhere in this module, because
case 2 already strips every off-grid bar that is not a real trade.

Every failure below is loud and specific, never a silent skip or an empty frame: no daily twin for
the symbol; a non-positive OR NaN daily close/adjusted_close (reason=non_positive_close) making the
factor undefined; no daily rows overlapping the requested window; a requested window entirely
outside THAT TICKER's own hourly file span (reason=window_outside_ticker_span, finding F below); an
hourly file missing any of the eight expected columns.

FINDINGS-DISPATCH AMENDMENTS (job A, lane A of the hourly-pivot campaign, 2026-08-24), folded into
this same module rather than listed separately, because each is a correction to the design above:

  A. An EODHD feed gap (open, high, low, close AND volume all NaN -- distinct from the flat/null
     -volume stub, since NaN == NaN is False and the stub rule needs real equal numbers) is DROPPED
     unconditionally, never folded (a NaN close carries no information to fold forward) and never
     raised on (AAPL alone carries 19 such rows; refusing on them would refuse the whole store).
     The drop is reported per call, printed only when the count is non-zero, naming the symbol, the
     exact count and the literal reason=all_nan_bar, matching this module's existing
     `eodhd_hourly: {symbol} ...` prefix and `(reason=X)` idiom.
  B. The AC-3 fold pass must see the FULL trading day even when the requested window truncates that
     day mid-session, or a genuine mid-session stub can be misread as the day's own closing phantom
     and its unproven close can overwrite a real, volume-backed bar's close (reproduced on the real
     AAPL store, 2023-08-29). So the window clamp now runs AFTER the fold pass groups by trading
     day, not before; the OUTPUT is still clamped to the requested window, only the fold pass's own
     day-grouping sees the unclamped file.
  C. Four operational monitors, all silent unless there is something to report: `hourly_adapter_
     refusals` (every raise above records its reason into a process-wide counter silently;
     `report_refusals()` prints the grouped-by-reason total, called explicitly rather than on the
     raising call itself, so this suite's own already-committed refusal probes -- which trigger a
     raise directly without capturing stdout -- are not drifted); `hourly_phantom_bars_remaining`
     (a defensive re-scan of the OUTPUT for the fold rule's own flat/null-volume shape, auto-printed
     per call, fire-only-when-non-zero, expected 0); `hourly_bar_period_observed` (the smallest gap
     between consecutive output bars, auto-printed per call, fire-only-when-non-zero, silent at
     exactly one hour or the half-day's own legitimate 30 minutes); `hourly_uncorrected_jumps` (any
     post-correction bar-to-bar move whose absolute log-return exceeds 0.25, counted silently per
     call -- an auto-print here would drift ac4_smallest_gap_one_hour_after_fold.probe.py's own
     888.0 marker-close fixture -- and reported only via the explicit `report_uncorrected_jumps()`
     call). The 0.25 threshold is derived, not a round number picked by hand: run against AAPL's
     and NVDA's full real hourly store, the largest clean (already-corrected) bar-to-bar move
     measured was 7.15 pct and the 99.99th percentile was 6.88 pct, so 0.25 sits about 3.5x above
     the largest clean move actually seen and well below a 3:2 stock split's own roughly 40 pct
     jump -- comfortably separating a genuine uncorrected corporate action from ordinary price
     noise.
  D. round 3, closing the `nan-reaches-output-unguarded` class for good: a row with SOME but not
     ALL of its four PRICE columns (open, high, low, close) NaN matched neither finding A's all-
     five-NaN drop nor the flat-bar stub test below (both rely on exact equality, and NaN == NaN is
     False), so it used to survive the fold pass, get multiplied by its day's factor, and reach the
     output as a NaN-priced row nothing raised or counted on. DROPPED unconditionally, same position
     and reasoning as finding A (before the fold pass's day-grouping), reported under the distinct
     reason=partial_nan_bar. The mask checks ONLY the four price columns, deliberately never volume:
     a non-flat bar with real prices but a NaN volume is a genuine, tradeable price bar (finding
     C's own AC-5 reading: a NaN volume is EODHD's own raw value for that hour) and must SURVIVE,
     with Volume still NaN -- a mask that also caught NaN volume would silently discard real price
     bars, the over-correction hazard this finding's own dispatch named by name.
  E. root-cause dispatch, 2026-08-25, closing the `silent-declared-failure-mode` class this module
     kept re-growing one instance at a time: finding A's all-NaN drop and finding D's partial-NaN
     drop each report their own count when they fire, but the AC-3 fold pass beside them (the
     session-close fold, the mid-session-stub removal and the lone-stub-day drop) never gained a
     monitor of its own, so a window whose every bar was a stub returned an empty frame with every
     existing signal silent -- a caller could not tell "checked N bars, all clean" from "checked
     nothing". Rather than give the fold pass its own counter (which would only re-open the same
     hole the next time a fold pass gains a new drop mechanism), this closes it structurally: a
     count of the bars this call actually found inside the requested window, taken BEFORE any drop
     pass runs, is compared against the returned frame's own row count. `hourly_window_fully_
     dropped` fires once, fire-only-when-non-zero like every other monitor in this module, exactly
     when that count is positive and the returned frame is empty -- whichever combination of drop
     passes consumed the window, named or not yet named. Silent when the window genuinely held no
     bars to begin with, which is a different and unremarkable case from this one.
  F. M5 fetcher job 10 (lane R, docs/handoffs/2026-09-04_fetcher/10_job_reader_and_provenance.md,
     2026-09-04), replacing the store-wide span check above with a per-ticker one. THE OLD DEFECT:
     `_STORE_SPAN_START`/`_STORE_SPAN_END` were a single date range measured once against one real
     file (AAPL's), then applied to every ticker alike -- hiding real data a DIFFERENT ticker's own
     file genuinely held (a ticker whose own history runs past the date the constant happened to be
     measured on, or before it) and going stale again by construction every time the store grew.
     THE FIX asks the question per ticker instead, from the SAME read already performed above to
     load and validate the hourly file -- no second read of the file, since `raw` already holds
     every one of this ticker's own rows in memory: the ticker's own file spans
     [ticker_min_ts, ticker_max_ts] (its ROWS' own timestamp min and max), and a requested window
     entirely outside that range is refused, reason=window_outside_ticker_span, naming both the
     ticker and the reason by code, never a silent empty frame. A window that merely holds zero raw
     bars WHILE STILL sitting inside the ticker's own file span (two real bars straddle an empty gap
     between them, for instance) is a different, unremarkable case this check does not refuse on --
     `hourly_window_fully_dropped` (finding E) is what reports that one, silently when
     n_raw_in_window is zero, exactly as before this change.
  G. RA-5 gate review of finding F, two fixes (lane R findings build, 2026-09-04). BLOCKER B2: a
     ticker file that EXISTS and holds ZERO rows made finding F's own guard inert -- pandas' NaN/NaT
     comparison rules (the same class finding F's neighbour, the non_positive_close guard, already
     hit and fixed) made `ticker_min_ts`/`ticker_max_ts` both NaT for an empty file, and every NaT
     comparison is False, so the guard never fired and an empty file silently returned an empty
     frame. Fixed by checking `len(raw) == 0` explicitly, before the NaT-producing min/max calls,
     reason=ticker_file_empty. SHOULD-FIX 1: the lower bound `start_ts >= ticker_max_ts` refused a
     window starting exactly on the ticker's own last bar, even though that bar genuinely sits
     inside the half-open window this function's own later filter serves -- the shape an incremental
     "everything since the last known bar" fetch takes. Changed to strict `start_ts > ticker_max_ts`
     to match that same half-open convention.
"""
import math  # math.log for the uncorrected-jump check; stdlib only, keeps this module pandas-only
import os  # path joins and env var reads, call-time, matching fetch_eodhd's own convention
import time  # wall-clock timing for the reader-latency ops monitor (M5 fetcher job 10, mode 3)

import pandas as pd  # the only third-party import: this module must stay importable without vbt

# Process-wide, silently-accumulated operational state (ops monitors C above). Neither of these is
# ever touched by a print statement outside report_refusals()/report_uncorrected_jumps() themselves,
# so a call that raises or folds a large marker value never leaks a line into a caller's own
# captured stdout -- the exact drift this suite's own probes already measured and pinned against.
_refusal_counts: dict = {}  # reason string -> count, across every fetch_eodhd_hourly call this run
_jump_count = 0  # total post-correction bar-to-bar moves past the threshold, across every call

# M5 fetcher job 10 what-can-break mode 3 ("the per-ticker span answer costs a second file read on a
# hot path... a reader latency counter, so the cost is visible rather than argued about"). Three
# bounded scalars, never a per-call list, so a long-running container never grows this state without
# bound. report_reader_latency() is the only thing that ever prints it, same idiom as every other
# monitor above.
_reader_latency_count = 0  # calls to fetch_eodhd_hourly completed this process, success or refusal alike
_reader_latency_total_s = 0.0  # their summed wall-clock seconds
_reader_latency_max_s = 0.0  # the single slowest call seen this process

# The post-correction jump threshold (ops monitor hourly_uncorrected_jumps). Derived 2026-08-24 from
# the adapter's own corrected output over AAPL's and NVDA's full real hourly store: the largest
# clean bar-to-bar move measured was 7.15 pct and the 99.99th percentile was 6.88 pct, so 0.25 (25
# pct) sits about 3.5x above the largest clean move actually seen and far below a 3:2 split's own
# roughly 40 pct jump. The book prints no value for this and no ledger entry does either, so this is
# an argument from the measured distribution, not a page citation (docs/research_lab/
# parameter_provenance.md entry 2).
_UNCORRECTED_JUMP_THRESHOLD = 0.25


def _record_refusal(reason: str) -> None:
    """Silently increments the process-wide refusal counter for `reason` (ops monitor hourly_
    adapter_refusals). Never prints: printing on the raising call itself would auto-fire on every
    one of this suite's already-committed AC-6 refusal probes, none of which capture stdout, and
    drift their .expected files. report_refusals() is the only thing that ever prints this state."""
    _refusal_counts[reason] = _refusal_counts.get(reason, 0) + 1  # item assignment, no rebind needed


def _record_jump() -> None:
    """Silently increments the process-wide uncorrected-jump counter (ops monitor hourly_
    uncorrected_jumps). Never prints: printing here would auto-fire on ac4_smallest_gap_one_hour_
    after_fold.probe.py's own 888.0 marker-close fixture, a deliberately huge fold value unrelated
    to real price continuity, and drift that already-committed probe's .expected file.
    report_uncorrected_jumps() is the only thing that ever prints this state."""
    global _jump_count  # rebinding the module-level int requires this, unlike the dict above
    _jump_count += 1


def report_refusals() -> None:
    """Ops monitor hourly_adapter_refusals: 'Counter of adapter refusals by reason, per run.' A
    fire-only-when-non-zero summary of every refusal recorded silently since process start, grouped
    by reason. Prints nothing if nothing has been refused yet this process."""
    if not _refusal_counts:  # nothing refused yet: stay silent, matching this module's own idiom
        return
    for reason, count in sorted(_refusal_counts.items()):  # sorted for a deterministic report order
        print(f"eodhd_hourly: hourly_adapter_refusals reason={reason} count={count}", flush=True)


def report_uncorrected_jumps() -> None:
    """Ops monitor hourly_uncorrected_jumps: 'Post-correction jump check: any bar-to-bar move above
    a declared threshold is counted and named.' A fire-only-when-non-zero summary of every jump
    counted silently since process start. Prints nothing if none has been counted yet."""
    if _jump_count == 0:  # nothing counted yet: stay silent, matching this module's own idiom
        return
    print(f"eodhd_hourly: hourly_uncorrected_jumps threshold={_UNCORRECTED_JUMP_THRESHOLD} "
          f"count={_jump_count}", flush=True)


def _record_reader_latency(elapsed_s: float) -> None:
    """Silently accumulates the reader-latency ops monitor (M5 fetcher job 10 what-can-break mode 3).
    Never prints: printing here would fire on every one of this suite's already-committed probes,
    none of which capture stdout, and drift their .expected files, the same reasoning
    _record_refusal/_record_jump already give for staying silent. report_reader_latency() is the only
    thing that ever prints this state."""
    global _reader_latency_count, _reader_latency_total_s, _reader_latency_max_s
    _reader_latency_count += 1
    _reader_latency_total_s += elapsed_s
    if elapsed_s > _reader_latency_max_s:
        _reader_latency_max_s = elapsed_s


def report_reader_latency() -> None:
    """Ops monitor reader_latency_counter: wall-clock cost of fetch_eodhd_hourly calls this process
    (count, average and max seconds), so the per-ticker span check's own cost -- or any future cost a
    later change adds to this reader -- is visible rather than argued about. Fire-only-when-non-zero:
    silent if no call has completed yet this process."""
    if _reader_latency_count == 0:  # nothing timed yet: stay silent, matching this module's own idiom
        return
    avg_s = _reader_latency_total_s / _reader_latency_count
    print(f"eodhd_hourly: reader_latency_counter calls={_reader_latency_count} "
          f"avg_s={avg_s:.4f} max_s={_reader_latency_max_s:.4f}", flush=True)


# The container's default bind-mount roots (matches fetch_eodhd's own _EODHD_DAILY_ROOT_DEFAULT
# convention exactly). Both are overridable by env var, read at CALL time below so a probe pointing
# at a temp fixture directory just sets the env var before calling.
_EODHD_HOURLY_ROOT_DEFAULT = "/eodhd-hourly"
_EODHD_DAILY_ROOT_DEFAULT = "/eodhd-daily"  # the daily store is SHARED with fetch_eodhd; same root

# Lane D2c: the joined root (env EODHD_HOURLY_JOINED_ROOT, no default: unset means native only) holds
# <SYM>.US.parquet with a source column and _gaps.parquet, as research_lab/fetcher/join_1h.py writes them
# (SOURCE_BUILT and GAPS_FILE there; this module is baked into the image alone, so the two names are repeated).
_BUILT_SOURCE = "built_from_1min"
_GAPS_FILE = "_gaps.parquet"

# The eight columns the real hourly store carries (research_lab/probes/hourly_adapter/_fixtures.py,
# verified 2026-08-24 against /home/tamer/eodhd/1h/AAPL.US.parquet). AC-6 mode 5: a file missing any
# of these is refused loud rather than failing later with a cryptic KeyError.
_HOURLY_COLS = ("timestamp", "gmtoffset", "datetime", "open", "high", "low", "close", "volume")

# The seven columns the daily store carries (same as fetch_eodhd's own missing_cols check).
_DAILY_COLS = ("date", "open", "high", "low", "close", "adjusted_close", "volume")

# NOTE: this module used to carry a STORE-level span bound here (_STORE_SPAN_START/_STORE_SPAN_END,
# a single date range measured once against one real file). M5 fetcher job 10 (finding F in the
# module docstring above) replaced it with a PER-TICKER check, computed from each call's own already
# -loaded hourly file further down in fetch_eodhd_hourly, so no store-wide constant lives here any
# more.


def fetch_eodhd_hourly(symbol: str, timeframe: str,
                       start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """Same calling convention as every fetcher in fetch.py's SOURCE_DISPATCH. Returns a frame with
    columns Open, High, Low, Close, Volume on a tz-aware UTC DatetimeIndex, corrected for corporate
    actions and with the phantom bar folded away (see the module docstring for the full design).

    A thin timing wrapper around _fetch_eodhd_hourly_impl (ops monitor mode 3): every call is timed,
    success or refusal alike, via try/finally, so the reader-latency counter sees every call this
    process makes, not only the ones that return a frame.
    """
    start_time = time.perf_counter()
    try:
        return _fetch_eodhd_hourly_impl(symbol, timeframe, start_ts, end_ts)
    finally:
        _record_reader_latency(time.perf_counter() - start_time)


def _fetch_eodhd_hourly_impl(symbol: str, timeframe: str,
                             start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    """The real implementation of fetch_eodhd_hourly; see that function's own docstring for the
    calling convention and the module docstring for the full design. Split into its own function only
    so the reader-latency wrapper above can time the WHOLE call, success or refusal alike, without
    reindenting this function's own body.
    """
    if timeframe != "1h":  # this adapter serves the EODHD hourly store only (AC-1)
        raise ValueError(f"eodhd_hourly adapter supports timeframe '1h' only, got {timeframe!r}")

    # call-time env reads, so a probe pointing at a temp fixture dir just sets the env first
    hourly_root = os.environ.get("EODHD_HOURLY_ROOT", _EODHD_HOURLY_ROOT_DEFAULT)
    daily_root = os.environ.get("EODHD_DAILY_ROOT", _EODHD_DAILY_ROOT_DEFAULT)

    # lane D2c (DECISIONS #472 item 3): the joined root, when set and holding a file for this ticker, adds
    # the deeper history built from the 1-minute store, joined to the native rows here at read time
    joined_root = os.environ.get("EODHD_HOURLY_JOINED_ROOT")
    joined_path = os.path.join(joined_root, f"{symbol}.US.parquet") if joined_root else None
    use_joined = joined_path is not None and os.path.exists(joined_path)

    # locate and read the hourly file: fail loud, never a silent empty frame, if it is absent. A ticker
    # with a joined file and no native file (delisted before 2020) is read from its built rows alone.
    hourly_path = os.path.join(hourly_root, f"{symbol}.US.parquet")
    have_native = os.path.exists(hourly_path)
    if not have_native and not use_joined:
        _record_refusal("no_hourly_file")  # silent; report_refusals() prints the total
        raise FileNotFoundError(
            f"eodhd_hourly: no hourly file for symbol {symbol!r} at {hourly_path} "
            f"(EODHD_HOURLY_ROOT={hourly_root})"
        )
    if have_native:
        raw = pd.read_parquet(hourly_path)  # columns: timestamp, gmtoffset, datetime, o, h, l, c, volume
        missing_hourly = [c for c in _HOURLY_COLS if c not in raw.columns]
        if missing_hourly:  # AC-6 mode 5: a malformed hourly parquet -> fail loud with the missing names
            _record_refusal("missing_columns")  # silent; report_refusals() prints the total
            raise ValueError(
                f"eodhd_hourly: {symbol} hourly parquet at {hourly_path} missing columns "
                f"{missing_hourly}; got {list(raw.columns)}"
            )
    else:
        raw = pd.DataFrame(columns=list(_HOURLY_COLS))
    raw = raw.assign(_is_built=False)  # the phantom-bar fold below skips built rows (D2a: no 16:00 bar)

    gap_rows = []
    if use_joined:
        joined = pd.read_parquet(joined_path)
        missing_joined = [c for c in (*_HOURLY_COLS, "source") if c not in joined.columns]
        if missing_joined:
            _record_refusal("missing_columns")  # silent; report_refusals() prints the total
            raise ValueError(
                f"eodhd_hourly: {symbol} joined parquet at {joined_path} missing columns "
                f"{missing_joined}; got {list(joined.columns)}"
            )
        built = joined.loc[joined["source"] == _BUILT_SOURCE, list(_HOURLY_COLS)].assign(_is_built=True)
        overlap = set(built["timestamp"]) & set(raw["timestamp"])
        if overlap:  # the native rows and the built rows must never share a bar
            _record_refusal("duplicate_timestamp")  # silent; report_refusals() prints the total
            raise ValueError(
                f"eodhd_hourly: {symbol} has {len(overlap)} bar(s) in both the built rows of {joined_path} "
                f"and the native file {hourly_path}, first timestamp {min(overlap)} "
                "(reason=duplicate_timestamp); refusing rather than choosing one"
            )
        raw = pd.concat([built, raw], ignore_index=True) if len(raw) else built.reset_index(drop=True)
        # the hole's rows for this ticker; its absence is a stop, because a hole cannot be told from a quiet market
        gaps_path = os.path.join(joined_root, _GAPS_FILE)
        if not os.path.exists(gaps_path):
            _record_refusal("no_gaps_file")  # silent; report_refusals() prints the total
            raise FileNotFoundError(
                f"eodhd_hourly: {symbol} has a joined file at {joined_path} but there is no {_GAPS_FILE} "
                f"beside it (EODHD_HOURLY_JOINED_ROOT={joined_root}); the hole in the series cannot be checked "
                "(reason=no_gaps_file)"
            )
        gaps = pd.read_parquet(gaps_path)
        missing_gap_cols = [c for c in ("ticker", "start", "end") if c not in gaps.columns]
        if missing_gap_cols:
            _record_refusal("missing_columns")  # silent; report_refusals() prints the total
            raise ValueError(
                f"eodhd_hourly: gaps parquet at {gaps_path} missing columns {missing_gap_cols}; "
                f"got {list(gaps.columns)}"
            )
        mine = gaps[gaps["ticker"] == symbol]
        gap_rows = [(pd.Timestamp(r.start).normalize(), pd.Timestamp(r.end).normalize())
                    for r in mine.sort_values("start").itertuples()]

    # AC-6 mode 4, PER-TICKER now (finding F, M5 fetcher job 10): refuse a window entirely outside
    # THIS ticker's own file, using the read already performed just above -- 'raw' already holds
    # every one of this ticker's own rows, so no second read of the file is needed to answer the
    # question. Replaces the old store-wide constant, which refused (or silently admitted) every
    # ticker alike regardless of what that ticker's own file actually covered.
    #
    # RA-5 gate-review BLOCKER B2 (2026-09-04): a ticker file that EXISTS and holds ZERO rows makes
    # the span guard below inert. MEASURED by the lead by running it, pandas 3.0.5:
    # raw["timestamp"].min() on an empty Series is nan, pd.to_datetime(nan, ...) is NaT, and every
    # NaT comparison is False -- so "end_ts <= ticker_min_ts or start_ts > ticker_max_ts" would
    # collapse to "False or False" and never fire, letting a genuinely empty file fall through to an
    # empty result frame with nothing refused and nothing recorded. Checked explicitly here, BEFORE
    # the NaT-producing min/max calls below, so the refusal never depends on NaT's own comparison
    # behaviour at all. reason=ticker_file_empty, a distinct code from window_outside_ticker_span
    # below: a zero-row file is a different condition (no span to compare against) from a window
    # that misses a real, non-empty span.
    if len(raw) == 0:
        _record_refusal("ticker_file_empty")  # silent; report_refusals() prints the total
        raise ValueError(
            f"eodhd_hourly: {symbol} hourly file at {hourly_path} exists but holds zero rows "
            f"(reason=ticker_file_empty); refusing rather than silently returning an empty frame"
        )
    ticker_min_ts = pd.to_datetime(raw["timestamp"].min(), unit="s", utc=True)
    ticker_max_ts = pd.to_datetime(raw["timestamp"].max(), unit="s", utc=True)
    # RA-5 gate-review SHOULD-FIX 1 (2026-09-04): the lower bound was `start_ts >= ticker_max_ts`,
    # which refuses a window that STARTS exactly on the ticker's own last bar even though that bar
    # genuinely sits inside the half-open [start_ts, end_ts) window the later filter uses
    # (`raw["_idx"] >= start_ts) & (raw["_idx"] < end_ts)`). Changed to strict `>` to match that same
    # half-open convention: a window starting exactly at the ticker's own last bar is NOT refused,
    # exactly the shape an incremental "everything since the last known bar" fetch takes.
    if end_ts <= ticker_min_ts or start_ts > ticker_max_ts:
        _record_refusal("window_outside_ticker_span")  # silent; report_refusals() prints the total
        raise ValueError(
            f"eodhd_hourly: {symbol} requested window {start_ts.isoformat()} to {end_ts.isoformat()} "
            f"is entirely outside {symbol}'s own hourly file span, {ticker_min_ts.isoformat()} to "
            f"{ticker_max_ts.isoformat()} (reason=window_outside_ticker_span); refusing rather than "
            "silently returning an empty frame"
        )

    # lane D2c: a window that overlaps a gap row is refused, never returned with a jump between two bars
    # (one bar's return would span the hole and corrupt every hourly figure). Each gap row's start and
    # end are New York dates, both inclusive (join_1h writes end = the day before the first native bar).
    # The window's dates are read as the calendar dates fetch.py gives (UTC midnights): its first date
    # is start_ts's, its last is the day of the last instant before the exclusive end_ts.
    first_day = start_ts.tz_convert("UTC").normalize().tz_localize(None)
    last_day = (end_ts - pd.Timedelta(nanoseconds=1)).tz_convert("UTC").normalize().tz_localize(None)
    for gap_start, gap_end in gap_rows:
        if first_day <= gap_end and last_day >= gap_start:
            _record_refusal("window_crosses_hourly_gap")  # silent; report_refusals() prints the total
            before_gap = gap_start.strftime("%Y-%m-%d")
            after_gap = gap_end.strftime("%Y-%m-%d")
            raise ValueError(
                f"eodhd_hourly: {symbol} requested window {start_ts.isoformat()} to {end_ts.isoformat()} "
                f"crosses the hourly gap {before_gap} to {after_gap} (reason=window_crosses_hourly_gap); "
                f"EODHD has no hourly bars in the gap. Ask for a window that ends before {before_gap} "
                f"(its end on or before {before_gap}) or one that starts after {after_gap} "
                f"(its start on {(gap_end + pd.Timedelta(days=1)).strftime('%Y-%m-%d')} or later)"
            )

    # locate and read the daily twin: every one of the 41,400 real hourly tickers has one, so a
    # missing file here means a fixture or a data-store defect, and either way it is a stop (AC-6
    # mode 1), because the total-return factor cannot be built without it.
    daily_path = os.path.join(daily_root, f"{symbol}.US.parquet")
    if not os.path.exists(daily_path):
        _record_refusal("no_daily_twin")  # silent; report_refusals() prints the total
        raise FileNotFoundError(
            f"eodhd_hourly: no daily twin for symbol {symbol!r} at {daily_path} "
            f"(EODHD_DAILY_ROOT={daily_root}); the total-return factor cannot be built without it"
        )
    daily_raw = pd.read_parquet(daily_path)  # columns: date, o, h, l, c, adjusted_close, volume
    missing_daily = [c for c in _DAILY_COLS if c not in daily_raw.columns]
    if missing_daily:  # same malformed-file guard as the hourly file, mirrored for the daily twin
        _record_refusal("missing_columns")  # silent; report_refusals() prints the total
        raise ValueError(
            f"eodhd_hourly: {symbol} daily parquet at {daily_path} missing columns "
            f"{missing_daily}; got {list(daily_raw.columns)}"
        )

    # AC-6 mode 2: a non-positive close makes adjusted_close/close undefined (or a division by a
    # sign-flipped number, equally meaningless). FINDING (2026-08-24): pandas' own NaN-comparison
    # rules make "<= 0" blind to NaN (NaN <= 0 is False), so a NaN close or a NaN adjusted_close
    # used to pass this guard silently, the factor came out NaN, and every price that day went out
    # NaN with nothing raised. isna() checks are ORed in explicitly to close that hole. The brief's
    # own machine-readable reason string, non_positive_close, is named literally in this message so
    # a caller can match on it by name; the NaN case gets the SAME token, since the brief supplies
    # no separate one and the underlying defect (an undefined factor) is identical either way.
    bad_close_mask = (
        (daily_raw["close"] <= 0) | daily_raw["close"].isna() | daily_raw["adjusted_close"].isna()
    )
    if bad_close_mask.any():
        _record_refusal("non_positive_close")  # silent; report_refusals() prints the total
        bad_dates = daily_raw.loc[bad_close_mask, "date"].tolist()[:5]  # name a few for debugging
        raise ValueError(
            f"eodhd_hourly: {symbol} has non-positive or NaN daily close/adjusted_close on "
            f"{bad_dates} (reason=non_positive_close); cannot back-adjust, refusing"
        )

    # AC-6 mode 3: the daily twin exists and is well-formed, but does it actually cover the
    # requested window? The daily 'date' column already represents the New York trading day (the
    # same convention fetch_eodhd relies on), so we compare it against the window's own NY-date
    # span, padded by one day on each side to absorb the UTC/NY offset at the window's own edges (a
    # UTC-midnight boundary can land on either side of the NY trading day). The pad never HIDES a
    # real "no overlap" case: this brief's own fixture uses a 2023-vs-2024 gap, far outside any
    # one-day pad.
    window_start_ny = start_ts.tz_convert("America/New_York").normalize().tz_localize(None)
    window_end_ny = end_ts.tz_convert("America/New_York").normalize().tz_localize(None)
    daily_dates = pd.to_datetime(daily_raw["date"])  # naive calendar-day timestamps, one per daily row
    pad = pd.Timedelta(days=1)
    overlap = daily_dates[(daily_dates >= window_start_ny - pad) & (daily_dates <= window_end_ny + pad)]
    if len(overlap) == 0:
        _record_refusal("no_daily_overlap")  # silent; report_refusals() prints the total
        raise ValueError(
            f"eodhd_hourly: {symbol} daily twin has no rows overlapping the requested window "
            f"{start_ts.isoformat()} to {end_ts.isoformat()} (overlap check failed)"
        )

    # the NY-date -> total-return-factor lookup, built from the WHOLE daily file (mirrors
    # fetch_eodhd's own whole-file factor derivation rather than restricting to the window, so the
    # non-positive-close guard above and this lookup stay consistent with each other).
    daily_factor = dict(zip(daily_raw["date"], daily_raw["adjusted_close"] / daily_raw["close"]))

    # AC-1: the index is built from the 'timestamp' column (unix epoch seconds), never parsed from
    # the 'datetime' string column, which the real store's own gmtoffset=0 on every row makes the
    # simpler and safer source (verified 2026-08-24).
    idx = pd.to_datetime(raw["timestamp"], unit="s", utc=True)
    raw = raw.assign(_idx=idx)  # attach as a working column so sort/filter/group keep every raw field
    raw = raw.sort_values("_idx").reset_index(drop=True)  # chronological order the fold logic needs

    # FINDING B (2026-08-24): the window clamp used to run HERE, before the AC-3 fold pass's own
    # day-grouping. That let a genuine mid-session stub near a mid-day end_ts be misread as the
    # day's own closing phantom (it looked like the LAST row of the truncated window), and its
    # unproven close overwrote a real, volume-backed bar's close -- reproduced on the real store,
    # AAPL 2023-08-29. So the clamp is deliberately NOT here any more: the fold pass below sees every
    # bar of every trading day the file holds, and the requested window is applied to the OUTPUT
    # only, after folding, further down this function.

    # the New York calendar date each bar belongs to, used both for grouping the AC-3 fold pass and
    # for keying the per-day total-return factor (AC-2's own "New York trading date, not UTC" rule).
    raw = raw.assign(_ny_date=raw["_idx"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d"))

    # ROOT-CAUSE DISPATCH FINDING E (2026-08-25, see module docstring): the count of raw bars this
    # call actually found inside the requested window, taken BEFORE any drop pass below runs. Used
    # only by the hourly_window_fully_dropped monitor further down, to tell "checked N bars, all
    # clean" apart from "checked nothing" on an empty returned frame.
    n_raw_in_window = int(((raw["_idx"] >= start_ts) & (raw["_idx"] < end_ts)).sum())

    # FINDING A (2026-08-24): an EODHD feed gap -- open, high, low, close AND volume all NaN, not
    # the already-handled flat/null-volume stub (NaN == NaN is False, so the stub's own equality
    # check below never matches this shape). Dropped unconditionally, never folded (a NaN close
    # carries no information to fold forward, unlike a real phantom's own genuine closing print) and
    # never raised on (AAPL alone carries 19 such rows across its whole file; refusing on them would
    # refuse essentially the whole store). Removed from `raw` BEFORE the fold pass below, so a
    # gap bar can never be mistaken for a day's "last real bar" and never receives or supplies a
    # close-override. The reported count is scoped to the requested window (what THIS call actually
    # returned), even though the drop itself happens across the whole file.
    is_all_nan = (raw["open"].isna() & raw["high"].isna() & raw["low"].isna() &
                  raw["close"].isna() & raw["volume"].isna())
    n_all_nan_in_window = int(
        (is_all_nan & (raw["_idx"] >= start_ts) & (raw["_idx"] < end_ts)).sum()
    )
    raw = raw.loc[~is_all_nan].reset_index(drop=True)  # drop every all-NaN gap bar, in or out of window
    if n_all_nan_in_window:  # fire-only-when-non-zero: silent when this call dropped nothing
        print(f"eodhd_hourly: {symbol} dropped {n_all_nan_in_window} bar(s) (reason=all_nan_bar)",
              flush=True)

    # FINDING (2026-08-24, round 3): a row with SOME but not all of its four PRICE columns NaN
    # (e.g. close NaN while open/high/low are real) matches neither the all-NaN drop above (which
    # needs ALL FIVE of open/high/low/close/volume NaN) nor the flat-bar stub test below (which
    # needs EXACT EQUALITY among open/high/low/close, and NaN == NaN is False under pandas). Such a
    # row used to survive the fold pass untouched, get multiplied by its day's factor (NaN * factor
    # == NaN), and reach the returned frame as a NaN-priced row with nothing raised and nothing
    # counted -- the same nan-reaches-output-unguarded class as the all-NaN gap bar above. The mask
    # below checks ONLY the four PRICE columns (open, high, low, close), deliberately never volume:
    # a non-flat bar with real prices but a NaN volume is a genuine, tradeable price bar (AC-5: a
    # NaN volume IS EODHD's own raw as-traded value for that hour) and must SURVIVE untouched --
    # checking volume here would over-correct and silently discard real price bars (the exact
    # hazard the dispatch calls out). This mask also naturally catches the case of all FOUR price
    # columns NaN with a real volume (e.g. a trade print with no OHLC at all): that row is not the
    # all-five-NaN case (volume survives) but still has at least one, in fact all four, price cells
    # NaN, so "at least one price column NaN" already covers it without a separate rule.
    price_cols = ["open", "high", "low", "close"]  # the four columns this guard inspects, never volume
    is_partial_nan = raw[price_cols].isna().any(axis=1)  # True iff >=1 of the 4 price cells is NaN
    n_partial_nan_in_window = int(
        (is_partial_nan & (raw["_idx"] >= start_ts) & (raw["_idx"] < end_ts)).sum()
    )
    raw = raw.loc[~is_partial_nan].reset_index(drop=True)  # drop before the fold pass sees any of it
    if n_partial_nan_in_window:  # fire-only-when-non-zero: silent when this call dropped nothing
        print(f"eodhd_hourly: {symbol} dropped {n_partial_nan_in_window} bar(s) "
              "(reason=partial_nan_bar)", flush=True)

    # the flat-bar detection rule (provenance entry 1): open == high == low == close, exact
    # equality, and volume is null. Measured 2026-08-24: 100 pct exact match on 33,302 sampled bars,
    # so no tolerance band is applied. The all-NaN drop above runs first, so this can never match a
    # NaN-priced gap bar as if it were a flat, tradeable-looking stub.
    # lane D2c: built rows are never stubs (D2a: they carry no 16:00 bar), so the fold sees native rows only
    is_stub = ((raw["open"] == raw["high"]) & (raw["high"] == raw["low"]) &
               (raw["low"] == raw["close"]) & raw["volume"].isna() & ~raw["_is_built"])

    keep_mask = pd.Series(True, index=raw.index)  # rows to keep; stubs get flipped False below
    close_override = {}  # row label -> the raw close a folded real bar should take instead of its own

    for _ny_date, group in raw.groupby(raw["_ny_date"], sort=False):  # one pass per trading day
        real_rows = group.index[~is_stub.loc[group.index]]  # this day's real (non-stub) bars, in time order
        stub_rows = group.index[is_stub.loc[group.index]]  # this day's stub bars, in time order
        if len(real_rows) == 0:
            # AC-3's third pinned case: a lone-stub day, nothing real that day to fold into or keep.
            keep_mask.loc[group.index] = False
            continue
        last_real_idx = real_rows[-1]  # the day's last real bar (real_rows is chronological)
        last_real_ts = raw.loc[last_real_idx, "_idx"]
        for s in stub_rows:  # stub_rows is chronological, so the LAST trailing match wins below
            stub_ts = raw.loc[s, "_idx"]
            if stub_ts > last_real_ts:
                # the session-close phantom (or any trailing stub after the day's last real bar):
                # folded, never dropped outright -- its close becomes the last real bar's new close.
                # If more than one trailing stub existed, the chronologically LAST one is the day's
                # true final print, so a later trailing stub always overwrites an earlier one here.
                close_override[last_real_idx] = raw.loc[s, "close"]
                keep_mask.loc[s] = False
            else:
                # the pinned mid-session-stub decision: a real bar still comes later that day, so
                # this stub carries no "official close" information the way the session closer does
                # -- it is simply removed, WITHOUT overwriting any neighbouring bar's close (naive
                # folding here would overwrite a real, volume-backed close with an unproven value).
                keep_mask.loc[s] = False

    for row_label, new_close in close_override.items():  # apply folds before dropping the stub rows
        raw.loc[row_label, "close"] = new_close
    raw = raw.loc[keep_mask].reset_index(drop=True)  # drop every stub row decided above

    # FINDING B, continued: the fold pass above has now seen every bar of every trading day the
    # file holds. The window clamp is applied HERE, to the OUTPUT, after folding -- never before.
    raw = raw[(raw["_idx"] >= start_ts) & (raw["_idx"] < end_ts)].reset_index(drop=True)

    # AC-2: every surviving bar's price is multiplied by its own trading day's total-return factor.
    # A survivor whose day has no matching daily row (should not happen given the overlap guard
    # above, but checked explicitly rather than assumed) is a fail-loud condition, not a silent skip.
    missing_factor_dates = sorted(set(raw["_ny_date"]) - set(daily_factor))
    if missing_factor_dates:
        _record_refusal("missing_daily_factor")  # silent; report_refusals() prints the total
        raise ValueError(
            f"eodhd_hourly: {symbol} has hourly bars on dates with no matching daily factor row: "
            f"{missing_factor_dates[:5]} (cannot back-adjust)"
        )
    factor = raw["_ny_date"].map(daily_factor)  # one factor value per surviving row

    out = pd.DataFrame({
        "Open": raw["open"] * factor,    # back-adjusted to the total-return basis, same as fetch_eodhd
        "High": raw["high"] * factor,    # same per-day factor
        "Low": raw["low"] * factor,      # same per-day factor
        "Close": raw["close"] * factor,  # includes any AC-3 fold override applied above
        "Volume": raw["volume"],         # AC-5: EODHD RAW as-traded volume, unadjusted, never scaled
    })
    out.index = raw["_idx"]  # AC-1: the tz-aware UTC DatetimeIndex built from 'timestamp'
    out.index.name = None  # cosmetic only, matches no particular caller expectation
    out = out.sort_index()  # defensive: the fold pass preserves order, but sort explicitly regardless

    # ops monitor hourly_window_fully_dropped (ROOT-CAUSE DISPATCH FINDING E, 2026-08-25): fires
    # only when this call's window held at least one raw bar (n_raw_in_window, captured above
    # before any drop pass ran) and NONE of them survived into the returned frame -- whatever
    # combination of the all-NaN drop, the partial-NaN drop and the AC-3 stub fold consumed all of
    # them. Structural, not per-mechanism: it does not name which pass did the dropping (each of
    # those already names itself when it fires), so a future drop pass this module gains in a
    # later fold is covered automatically, with no new monitor to remember to add. Silent whenever
    # the window genuinely held nothing to begin with (n_raw_in_window == 0), a different and
    # unremarkable case from this one.
    if n_raw_in_window > 0 and len(out) == 0:
        print(f"eodhd_hourly: {symbol} hourly_window_fully_dropped raw_bars_in_window="
              f"{n_raw_in_window} (every bar in the requested window was dropped; none survived "
              "to the returned frame)", flush=True)

    # ops monitor hourly_phantom_bars_remaining: "Assertion counter: flat null-volume bars remaining
    # after the fold, expected 0." A defensive re-scan of the OUTPUT for the fold pass's own
    # flat-bar shape (same equality rule as is_stub above), auto-printed per call, fire-only-when
    # -non-zero -- the fold pass already removes every detected stub by construction, so a correct
    # call always reads 0 here and stays silent; a non-zero reading means the phantom-bar rule has
    # stopped matching the real store's own stitching pattern.
    phantom_mask = ((out["Open"] == out["High"]) & (out["High"] == out["Low"]) &
                    (out["Low"] == out["Close"]) & out["Volume"].isna())
    n_remaining_phantom = int(phantom_mask.sum())
    if n_remaining_phantom:  # fire-only-when-non-zero: silent on every correct call
        print(f"eodhd_hourly: {symbol} hourly_phantom_bars_remaining={n_remaining_phantom} "
              "(expected=0)", flush=True)

    # ops monitor hourly_bar_period_observed: "Bar-period check recorded per run against an expected
    # one hour." The only ALREADY-ACCEPTED exception is a half-day session's own legitimate final
    # 30-minute interval (AC-4's "on a normal full session" qualifier). Auto-printed per call, fire
    # -only-when-non-zero (silent at exactly one hour or exactly the half-day's own 30 minutes;
    # anything smaller signals the bar grid has drifted off the expected one-hour spacing).
    if len(out) > 1:
        gaps = out.index.to_series().diff().dropna()
        min_gap = gaps.min()
        if min_gap not in (pd.Timedelta(hours=1), pd.Timedelta(minutes=30)):
            print(f"eodhd_hourly: {symbol} hourly_bar_period_observed={min_gap} (expected one hour, "
                  "or the half-day's own thirty minutes)", flush=True)

    # ops monitor hourly_uncorrected_jumps: "Post-correction jump check: any bar-to-bar move above a
    # declared threshold is counted and named." Threshold 0.25 (25 pct) absolute log-return, derived
    # from the adapter's own corrected output over AAPL's and NVDA's full real hourly store: the
    # largest clean bar-to-bar move measured was 7.15 pct, the 99.99th percentile was 6.88 pct, so
    # 0.25 sits about 3.5x above the largest clean move seen and well below a 3:2 split's own roughly
    # 40 pct jump. Counted SILENTLY here (an auto-print would drift ac4_smallest_gap_one_hour_after_
    # fold.probe.py's own 888.0 marker-close fixture, a deliberately huge fold value unrelated to
    # real price continuity); report_uncorrected_jumps() is the only thing that ever prints this.
    closes = out["Close"]
    for i in range(1, len(closes)):  # one comparison per consecutive pair of output bars
        prev_close = closes.iloc[i - 1]
        curr_close = closes.iloc[i]
        if prev_close > 0 and curr_close > 0:  # log-return is undefined for a non-positive price
            log_return = math.log(curr_close / prev_close)
            if abs(log_return) > _UNCORRECTED_JUMP_THRESHOLD:
                _record_jump()

    return out
