"""The backfill orchestrator (brief §5, §8): the concurrency pool, the phased plan,
the cap-aware drain, and the periodic /user reconcile.

Phases run in "useful data first" order: BULK (validate the bulk path + seed the top-up),
DAILY (cheap, makes every name usable + total-return-adjustable), RECENT (native 1h + 5m),
then DEEP 1-min (per-ticker coverage discovery, then the year-aligned window walk). Every
phase is resumable: enqueue is idempotent and the drain skips done/empty units. Reaching the
daily cap stops the pool cleanly with the rest left 'pending' for the next launch.
"""

import asyncio  # the concurrency pool
import sys  # stderr for the non-fatal reconcile-failure trace
import time  # 'now' for spans + the UTC day for bulk
from datetime import datetime, timezone  # the bulk day epoch + label

import httpx  # the async client

from . import manifest  # the resume/idempotence store
from .client import (CapReached, EodhdAuthError, EodhdCapError, EodhdShapeError,
                     EodhdTransientError, fetch_json, read_usage)
from .config import (BULK_INTERVALS, BULK_TICKER, BULK_URL, COST_BULK, COST_EOD,
                     COST_INTRADAY, DEEP_1M_END, DEFAULT_RECONCILE_EVERY, EOD_URL,
                     EXCHANGE, INTRADAY_URL, RECENT_START, TRIM_INTRADAY_TO_TRADING_LIFE)
from .coverage import discover_floor
from .storage import relative_path, write_atomic
from .tradinglife import (NoTradingOverlap, TradingLifeUnreadable, daily_date_span,
                          daily_fetched_at, no_overlap_reason, spans_overlapping_life,
                          trading_life_from)
from .universe import MAJOR_EXCHANGES  # job 11 AC-9: the ONE six-venue definition, read not restated
from .validate import ValidationError, validate
from .windows import recent_1h_span, recent_5m_subwindows, year_aligned_1m_windows, year_of

# Per-process counters for the venue pre-filter (job 11 what-can-break item 3), mirroring
# quarantine_242.SPAN_AUDIT_COUNTERS: in-memory only, no I/O, so incrementing this can never add a
# byte to a probe's captured stdout. A reader for these two (the operations page, parked to
# October) samples this from the running process; until then the registry row documents where it
# lives (docs/units/V/monitor_rows.md, pasted at merge).
VENUE_FILTER_COUNTERS = {
    "skipped_by_venue": 0,      # a stored exchange outside MAJOR_EXCHANGES -> discovery skipped it (AC-7)
    "probed_unknown_venue": 0,  # no priority row, or a NULL exchange -> fail-open, still probed (AC-8)
}


def _chunks(seq, size):
    """Yield successive `size`-length chunks of `seq` (for batched deep-1-min processing)."""
    for i in range(0, len(seq), size):  # step through in blocks
        yield seq[i:i + size]  # one batch


def _utc_day():
    """Return (day_epoch, 'YYYY-MM-DD') for today's UTC midnight — the bulk unit grain."""
    now = datetime.now(timezone.utc)  # current UTC time
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)  # today 00:00 UTC
    return int(midnight.timestamp()), midnight.strftime("%Y-%m-%d")  # epoch + label


def _effective_seed(used: int, requests_date: str, today_utc: str, force: bool) -> int:
    """Return the count to seed the cost ledger with, treating a stale or force-ignored display as 0.

    EODHD's /user apiRequests count carries the PRIOR UTC day's value (with that day's apiRequestsDate)
    until the first request of a new day resets it (verified live; see the budget-staleness decision
    note). So a count whose date is not today is stale and MUST seed 0 — otherwise a near-cap stale
    display false-stalls a quota that is actually fresh (the pre-stop refuses to spend). force=True
    ignores the display entirely (the run-now path Tamer triggers).

    force is passed ONLY at the initial seed in run_backfill; _reconcile always calls with force=False,
    so the periodic reconcile stays the honest safety net the force path relies on.
    """
    if force or requests_date != today_utc:  # explicit force OR a prior-day (stale) date -> do not trust it
        return 0  # treat the displayed count as zero; the real spend accrues from local charges
    return used  # same-day date -> the count is the real spend so far, use it


async def _guarded_get(http, key, ledger, limiter, timeout, url, params, ctx, cost):
    """Cap-check, rate-limit, issue one GET, charge the cost. Raise CapReached before issuing."""
    if not ledger.can_spend(cost):  # the hard stop (brief §8) — never overshoot the cap
        raise CapReached(f"daily cap reached before {ctx}")
    await limiter.acquire()  # respect the per-minute rate
    data = await fetch_json(http, url, params, ctx, timeout)  # the network call (with retry taxonomy)
    ledger.charge(cost)  # account the calls locally
    return data  # parsed JSON


async def _cached_life(life_cache, conn, staging_root, ticker):
    """Read a ticker's trading life through the per-run cache (brief S3.3 item 1).

    `life_cache` is a plain dict the caller creates ONCE in run_backfill and threads through every
    phase, so a ticker's daily file is read at most once across the whole run: the RECENT phase's
    "1h"/"5min" units and the DEEP phase's discovery step share the same cache and never re-read
    the same file. Scope and lifetime are the run: never persisted, so a stale entry cannot outlive
    the process. Two workers can race on the same miss (both read, one write wins); that costs one
    duplicate read and stays correct because the value is deterministic for the run, so the cache
    is never used to make a unit skip a read it needs.

    Only a SUCCESSFUL read is cached: TradingLifeUnreadable propagates uncached (never stored here),
    so a corrupt daily file fails only the unit that hit it, retried under the existing retry
    ceiling, rather than poisoning every remaining unit for this ticker for the rest of the run.

    The blocking read runs off the event loop (#229's second fault: an inline 200-450 ms read on
    the USB SSD serialised the whole 16-worker pool to about one unit per second). The manifest
    lookup for the fetch stamp stays on the loop: it is a primary-key hit and costs nothing.
    """
    if ticker in life_cache:  # a hit: already read this run, by either phase
        return life_cache[ticker]
    span = await asyncio.to_thread(daily_date_span, staging_root, ticker)  # blocking read, off the loop
    life = trading_life_from(span, daily_fetched_at(conn, ticker))  # combine with the fetch stamp
    life_cache[ticker] = life  # cache the successful result (life may itself be None: no evidence)
    return life


async def _useful_spans(conn, staging_root, ticker, spans, life_cache):
    """Narrow intraday spans (1h, 5min AND 1min) to the ticker's real trading life (config-gated).

    Returns (kept_spans, life): the spans still worth requesting, and the trading-life tuple that
    produced the narrowing (or None when the switch is off or there is no evidence), so a caller
    whose result is empty can build the NoTradingOverlap reason without a second read. With the
    trim switched off, or with no daily evidence on disk, every span is returned and behaviour is
    exactly as before.
    """
    if not TRIM_INTRADAY_TO_TRADING_LIFE:  # the one-line revert switch
        return list(spans), None  # unchanged behaviour: request the whole window, no life read at all
    life = await _cached_life(life_cache, conn, staging_root, ticker)  # cached across the whole run
    return spans_overlapping_life(spans, life), life  # drop what cannot hold this ticker's own bars


async def _fetch_rows_for_unit(http, key, ledger, limiter, timeout, conn, staging_root, unit, now, life_cache):
    """Fetch a unit's raw rows and compute its parquet path. Returns (interval, rows, rel_path).

    Every intraday unit (1h, 5min, 1min) is first narrowed to the ticker's real trading life,
    read for free from the daily history already on disk (tradinglife.py), through the shared
    `_useful_spans` helper -- ONE narrowing implementation, not one per interval. Sub-requests that
    cannot contain this ticker's own bars are never issued, and a unit with nothing left to ask
    raises NoTradingOverlap so the caller records it as 'skipped' rather than spending the calls.
    """
    ticker, interval, ws, we, _path, _attempts = unit  # unpack the manifest row

    if interval == "daily":  # per-ticker EOD full history (one ~1-call request)
        params = {"api_token": key, "fmt": "json", "period": "d", "order": "a"}  # ascending full history
        rows = await _guarded_get(http, key, ledger, limiter, timeout,
                                  EOD_URL.format(symbol=ticker), params, f"{ticker} eod", COST_EOD)
        return interval, rows, relative_path("daily", ticker=ticker)

    if interval == "1h":  # recent-era native hourly in one request (7200-day cap covers it)
        span = recent_1h_span(RECENT_START, now)  # (from, to) inclusive
        if span is None:  # RECENT_START is not in the past, so there is no recent era to request
            raise ValueError(f"{ticker} 1h: no recent-era span exists for RECENT_START={RECENT_START} "
                             f"against now={now}; RECENT_START must be in the past")
        spans, life = await _useful_spans(conn, staging_root, ticker, [span], life_cache)  # narrow via the cache
        if not spans:  # outside its trading life
            raise NoTradingOverlap(no_overlap_reason(ticker, "1h", span[0], span[1], life))
        params = {"api_token": key, "interval": "1h", "from": span[0], "to": span[1], "fmt": "json"}
        rows = await _guarded_get(http, key, ledger, limiter, timeout,
                                  INTRADAY_URL.format(symbol=ticker), params, f"{ticker} 1h", COST_INTRADAY)
        return interval, rows, relative_path("1h", ticker=ticker)

    if interval == "5min":  # recent-era native 5-min: walk <=600-day sub-windows, concat, write once
        all_spans = recent_5m_subwindows(RECENT_START, now)  # the full tiled sub-window set before narrowing
        if not all_spans:  # same precondition as the 1h branch, stated rather than left to IndexError
            raise ValueError(f"{ticker} 5min: no recent-era sub-windows exist for "
                             f"RECENT_START={RECENT_START} against now={now}; RECENT_START must be in the past")
        spans, life = await _useful_spans(conn, staging_root, ticker, all_spans, life_cache)  # drop dead sub-requests
        if not spans:  # nothing in the whole window can be this ticker's data
            raise NoTradingOverlap(no_overlap_reason(ticker, "5min", all_spans[0][0], all_spans[-1][1], life))
        rows = []  # accumulate all sub-windows for the single 5m file
        for frm, to in spans:  # only the sub-requests that can hold this ticker's bars
            params = {"api_token": key, "interval": "5m", "from": frm, "to": to, "fmt": "json"}
            part = await _guarded_get(http, key, ledger, limiter, timeout,
                                      INTRADAY_URL.format(symbol=ticker), params,
                                      f"{ticker} 5m [{frm}..{to}]", COST_INTRADAY)
            if not isinstance(part, list):  # a non-array 200 is a real shape error, never a silent gap
                raise EodhdShapeError(
                    f"{ticker} 5m [{frm}..{to}]: expected list, got {type(part).__name__}: {str(part)[:100]!r}")
            rows.extend(part)  # this sub-window's bars (concatenated into the single 5m file)
        return interval, rows, relative_path("5min", ticker=ticker)

    if interval == "1min":  # deep-era 1-min: one year-aligned window -> one file
        year = year_of(ws)  # the calendar year this window lives in
        api_to = we - 1  # exclusive tiling boundary -> inclusive API bound
        span = (ws, api_to)  # the single window this manifest unit covers
        spans, life = await _useful_spans(conn, staging_root, ticker, [span], life_cache)  # SAME helper as 5min
        if not spans:  # outside its trading life -- the third, previously-unguarded call site (brief S3.1)
            raise NoTradingOverlap(no_overlap_reason(ticker, "1min", span[0], span[1], life))
        params = {"api_token": key, "interval": "1m", "from": ws, "to": api_to, "fmt": "json"}
        rows = await _guarded_get(http, key, ledger, limiter, timeout,
                                  INTRADAY_URL.format(symbol=ticker), params,
                                  f"{ticker} 1m [{ws}..{api_to}]", COST_INTRADAY)
        return interval, rows, relative_path("1min", ticker=ticker, window_start=ws, year=year)

    if interval in BULK_INTERVALS:  # whole-exchange EOD / splits / dividends for one day
        _day_epoch, day_str = _utc_day()  # the day label for the path
        params = {"api_token": key, "fmt": "json"}  # base bulk params
        if interval == "bulk_splits":  # the actions variants take a type
            params["type"] = "splits"
        elif interval == "bulk_dividends":
            params["type"] = "dividends"
        rows = await _guarded_get(http, key, ledger, limiter, timeout,
                                  BULK_URL.format(exchange=EXCHANGE), params, f"bulk {interval}", COST_BULK)
        return interval, rows, relative_path(interval, day_str=day_str)

    raise ValueError(f"unknown interval {interval!r}")  # programming guard


def _record_unit_outcome(run_stats, ticker: str, interval: str, outcome: str, row_count: int) -> None:
    """Print job 13's per-unit line and tally the outcome (AC-1, AC-3), but ONLY when the caller
    opted in by passing a real `run_stats` dict.

    The real CLI (`_cmd_backfill` in __main__.py) always passes one, so every genuine backfill
    run prints and tallies. The many existing probes in this repo that call `_process_unit` or
    `_discover_batch` directly, bypassing the CLI (`research_lab/probes/eodhd_1min_p*.probe.py`,
    `eodhd_priority_q10.probe.py`, `fetcher_preconditions/p07`/`p08`), pass no `run_stats` (it
    defaults to None on every one of those functions) and so see no new output at all: this keeps
    every one of their committed `.expected` files byte-identical, since none of them is inside
    this unit's own area (`research_lab/fetcher/**` and its own probe directory).
    """
    if run_stats is None:  # a direct, non-CLI caller: stay silent, exactly as before this unit
        return
    print(f"unit ticker={ticker} interval={interval} outcome={outcome} rows={row_count}")  # AC-1's line shape
    run_stats[outcome] = run_stats.get(outcome, 0) + 1  # AC-3: this run's own live tally


async def _process_unit(http, key, ledger, limiter, timeout, conn, staging_root, unit, now, life_cache,
                        run_stats=None):
    """Fetch -> validate -> atomically write -> mark the manifest. Terminal-error -> mark_error.

    CapReached and EodhdAuthError propagate (the pool stops / the run fails). Every other fetch
    or validation failure marks the unit 'error' (re-queued, bounded retries); an empty payload
    marks 'empty' (a real coverage gap, not an error); a window that lies outside the ticker's
    trading life marks 'skipped' without issuing a request at all.

    `run_stats` (job 13, added as a trailing keyword-default parameter so every existing caller
    of this function keeps working unchanged): a mutable dict the CLI creates once per run and
    threads down through run_backfill; see `_record_unit_outcome` for what passing one does.
    """
    ticker, interval, ws, we, _path, _attempts = unit  # unpack
    try:
        interval, rows, rel = await _fetch_rows_for_unit(http, key, ledger, limiter, timeout,
                                                         conn, staging_root, unit, now, life_cache)
    except (CapReached, EodhdAuthError):  # control / fatal -> propagate to the pool
        raise
    except NoTradingOverlap as exc:  # never requested: the window cannot hold this ticker's bars
        # ONE truncation, at the store boundary (manifest.mark_skipped applies reason[:500] itself);
        # this is the single call site that stops pre-truncating (brief S3.4 item 1).
        manifest.mark_skipped(conn, ticker, interval, ws, we, str(exc))
        _record_unit_outcome(run_stats, ticker, interval, "skipped", 0)
        return
    except TradingLifeUnreadable as exc:  # a corrupt daily file must be visible, not silently ignored
        manifest.mark_error(conn, ticker, interval, ws, we, str(exc)[:500])
        _record_unit_outcome(run_stats, ticker, interval, "error", 0)
        return
    except (EodhdCapError, EodhdShapeError, EodhdTransientError) as exc:  # fetch failure -> re-queue
        manifest.mark_error(conn, ticker, interval, ws, we, str(exc)[:500])
        _record_unit_outcome(run_stats, ticker, interval, "error", 0)
        return
    try:
        # kept_rows is the ALREADY-FOLDED payload (job 11 AC-1): a repeated timestamp whose rows
        # agree contributes one entry. Writing kept_rows (never the raw `rows`) is what keeps the
        # parquet file and the manifest's recorded row_count from ever disagreeing about how many
        # rows this unit holds (validate.py's own docstring explains why the fold cannot be a bare
        # count: writing the unfolded rows while recording the folded count would be exactly the
        # mismatch audit.py's content check exists to catch).
        kept_rows, fold_count = validate(interval, rows)  # structural validation + the fold, on land
    except ValidationError as exc:  # bad shape, a genuine disagreement, or a backward step -> re-queue
        manifest.mark_error(conn, ticker, interval, ws, we, str(exc)[:500])
        _record_unit_outcome(run_stats, ticker, interval, "error", 0)
        return
    if not kept_rows:  # empty payload -> real coverage gap (not an error)
        manifest.mark_empty(conn, ticker, interval, ws, we)
        _record_unit_outcome(run_stats, ticker, interval, "empty", 0)
        return
    try:
        abs_path = write_atomic(staging_root, rel, kept_rows)  # atomic temp->fsync->rename, folded rows only
    except OSError as exc:  # disk full / SSD unmounted / permission -> re-queue, keep the pool alive
        manifest.mark_error(conn, ticker, interval, ws, we, f"storage: {exc!r}"[:500])
        _record_unit_outcome(run_stats, ticker, interval, "error", 0)
        return
    rel = abs_path[len(staging_root):].lstrip("/") if abs_path.startswith(staging_root) else abs_path  # store relative
    manifest.mark_done(conn, ticker, interval, ws, we, len(kept_rows), rel, folded_rows=fold_count)  # commit
    _record_unit_outcome(run_stats, ticker, interval, "done", len(kept_rows))


async def _reconcile(http, key, ledger, timeout, lock):
    """Re-seed the cost ledger from the live /user counter (brief §8 periodic reconcile).

    Always honest (force=False): the reconcile adopts the real same-day count once it is accurate, so it
    is the safety net the force path leans on. A still-stale (prior-day) count re-seeds 0 and self-
    corrects on the next reconcile after the run's first spend triggers EODHD's reset.
    """
    async with lock:  # only one reconcile at a time
        used, limit, _extra, requests_date = await read_usage(http, key, timeout)  # live global counter + its date
        ledger.cap = min(ledger.cap, limit)  # never exceed the account's real daily limit
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # the UTC date to compare the count against
        ledger.seed(_effective_seed(used, requests_date, today_utc, force=False))  # honest seed (stale -> 0)


async def run_units(http, key, ledger, limiter, timeout, conn, staging_root, units,
                    concurrency, life_cache, reconcile_every=DEFAULT_RECONCILE_EVERY, run_stats=None):
    """Drain a materialised list of pending units through an N-worker pool. Return cap_reached.

    Workers pull from a bounded queue; a CapReached sets the stop flag (the rest stay 'pending');
    an EodhdAuthError is captured and re-raised after the pool unwinds (the whole run fails). The
    ledger is reconciled to /user every `reconcile_every` processed units so local drift cannot
    accumulate. `units` is a list (materialised before draining) so the manifest cursor never
    interleaves with worker writes on the single connection.

    `life_cache` is the per-run trading-life cache (brief S3.3 item 1), created ONCE in
    run_backfill and passed in here rather than created locally: this function is called once per
    phase (and again per deep batch), so a cache created inside it would be per-phase, not per-run.

    `run_stats` (job 13, trailing keyword-default so every existing caller is unaffected): forwarded
    straight through to `_process_unit`, which is the only place that reads it.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 2)  # backpressure-bounded feed
    stop = asyncio.Event()  # set on cap or fatal
    fatal: dict = {}  # captured auth error, re-raised after unwind
    now = int(time.time())  # one 'now' for all spans this drain
    reconcile_lock = asyncio.Lock()  # serialise reconciles
    processed = {"n": 0}  # shared counter for the reconcile cadence

    async def worker():
        while True:  # pull until the sentinel
            unit = await q.get()
            try:
                if unit is None:  # sentinel -> this worker exits
                    return
                if stop.is_set():  # draining without processing after a stop
                    continue
                try:
                    await _process_unit(http, key, ledger, limiter, timeout, conn, staging_root,
                                        unit, now, life_cache, run_stats=run_stats)
                except CapReached:  # cap hit -> stop the pool cleanly
                    stop.set()
                except EodhdAuthError as exc:  # bad key -> fail the whole run
                    fatal["e"] = exc
                    stop.set()
                else:
                    processed["n"] += 1  # count a successfully-handled unit
                    if processed["n"] % reconcile_every == 0:  # periodic drift correction
                        try:
                            await _reconcile(http, key, ledger, timeout, reconcile_lock)
                        except EodhdAuthError as exc:  # a bad key during reconcile -> same channel as a fetch auth error
                            fatal["e"] = exc  # route through the single fatal capture (not a raw task crash)
                            stop.set()  # stop the pool; _cmd_backfill returns rc=3
                        except (EodhdTransientError, EodhdShapeError, httpx.TimeoutException,
                                httpx.TransportError) as exc:  # a transient reconcile failure must not kill the pool
                            # The pool continues on the local counter until the next reconcile, but trace the
                            # failure: under a force run the reconcile is the safety net, so a run of silent
                            # reconcile failures (letting the local counter drift) must be visible, not buried.
                            print(f"reconcile failed (non-fatal, local counter stands): "
                                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                q.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]  # the pool
    for unit in units:  # feed the queue (stops early if the pool stopped)
        if stop.is_set():
            break
        await q.put(unit)
    for _ in workers:  # one sentinel per worker
        await q.put(None)
    await asyncio.gather(*workers)  # wait for the pool to unwind
    if fatal:  # an auth error during the drain -> fail the run
        raise fatal["e"]
    return stop.is_set()  # True = cap reached (caller stops further phases)


def _pending_list(conn, intervals, symbols=None, max_attempts=4):
    """Materialise the pending units for the given intervals (and optional symbol batch).

    The interval/ticker filters are pushed into SQL (manifest.pending_units) so a deep-1-min batch
    scans only its own rows via the PK, not the whole (millions-of-rows) manifest. Materialised to a
    list so worker writes never interleave a live cursor on the single connection.
    """
    tickers = list(symbols) if symbols is not None else None  # SQL-side ticker filter (a batch)
    return list(manifest.pending_units(conn, max_attempts, intervals=tuple(intervals), tickers=tickers))


async def _discover_batch(http, key, ledger, limiter, timeout, conn, staging_root, symbols,
                          concurrency, life_cache, now: int = None, run_stats=None):
    """Discover the 1-min floor for a batch of symbols concurrently; record coverage. Cap-aware.

    `run_stats` (job 13, trailing keyword-default so every existing caller is unaffected): when a
    real dict is passed, every successful `manifest.set_coverage` call this batch makes increments
    `run_stats['coverage_rows_added']` (AC-3's own field); a direct, non-CLI caller passing none
    sees no change to anything, exactly as before this unit.

    Per-symbol pass (brief S3.2), each step inside `one(sym)`'s own try so no symbol's failure can
    take the batch down, at any failure count including all symbols failing:
      1. Resume check FIRST: a symbol already carrying a coverage row (floor or NULL) is skipped
         and its row is left exactly as recorded (a wrong floor does not self-heal here; that is
         DECISIONS #242's audit unit, not this one).
      2. Daily life read, through the shared per-run cache, reached only for a not-yet-discovered
         symbol. TradingLifeUnreadable is caught here: no coverage row is written, and the symbol
         is retried on the next launch (a symbol with no row is re-evaluated every run).
      3. Anchor filtering + probing lives inside discover_floor (top-only trim). NoTradingOverlap
         means no anchor survived the filter: no row is written (outcome c), also retried next run.
      4. A readable probe records its floor (bars found) or NULL (probed, found nothing) -- both
         terminal, exactly as before this brief.

    `now` is threaded straight through to discover_floor (default None -> the live clock, unchanged
    production behaviour). It exists so a probe can pin the look-back reference the same way P6 does
    for discover_floor called directly; without it, a probe driving this function would drift one
    day per day the way S3.3 item 6 already flags for a substring assertion.
    """
    stop = asyncio.Event()  # set on cap
    sem = asyncio.Semaphore(concurrency)  # bound concurrency

    async def one(sym):
        async with sem:  # limit in-flight discoveries
            if stop.is_set():  # cap already hit -> skip
                return
            discovered, _floor = manifest.get_coverage(conn, sym)  # resume: skip if already discovered
            if discovered:
                return
            # Job 11 AC-7/AC-8: the venue pre-filter, cheaper than a daily-life read so it runs
            # first. A stored exchange outside the six-venue set is skipped with a recorded reason
            # (never silently dropped); a ticker with NO priority row, or a NULL exchange (the
            # seed tier), fails OPEN and is still probed, because a filter this cheap is exactly
            # the kind that must never quietly cost real coverage on an unmapped symbol.
            exchange = manifest.get_priority_exchange(conn, sym)  # the venue the queue derived, or None
            if exchange is None:  # AC-8: no venue row, or an explicit NULL -> fail open, still probed
                VENUE_FILTER_COUNTERS["probed_unknown_venue"] += 1
            elif exchange not in MAJOR_EXCHANGES:  # AC-7: a positively-known venue outside the six-venue set
                VENUE_FILTER_COUNTERS["skipped_by_venue"] += 1
                print(f"discovery: skipped venue={exchange!r} symbol={sym}", file=sys.stderr)  # the recorded reason
                return  # never asked; no coverage row written, so this is re-judged if the venue map changes
            life = None  # no evidence -> discover_floor probes every anchor (matches the switch-off path)
            if TRIM_INTRADAY_TO_TRADING_LIFE:  # the same revert switch that gates the fetch path (brief S3.2)
                try:
                    life = await _cached_life(life_cache, conn, staging_root, sym)  # cached across the run
                except TradingLifeUnreadable as exc:  # a corrupt daily file for THIS symbol only
                    # SURFACED, not swallowed. The exception's own contract says so
                    # (tradinglife.TradingLifeUnreadable: "Surfaced, never silently ignored"), and the
                    # fetch path honours it by writing mark_error, which the manifest shows and the
                    # heartbeat's errors= term counts. Discovery has no unit row to mark, so stderr is
                    # the channel it does have. Without this a permanently corrupt daily file blocks
                    # one-minute discovery for that ticker forever and reads exactly like "not reached
                    # yet" in every place an operator looks.
                    print(f"discovery: daily history unreadable, coverage left unrecorded "
                          f"symbol={sym} error={type(exc).__name__}: {exc}", file=sys.stderr)
                    return  # this symbol is retried on the next launch (the pool stays alive)
            try:
                floor = await discover_floor(http, key, ledger, limiter, timeout, sym, life=life, now=now)  # probe
            except CapReached:  # cap during discovery -> stop the batch cleanly (resume next launch)
                stop.set()
                return
            except NoTradingOverlap:  # no anchor survives the top trim -> outcome (c), no row written
                return  # retried on the next launch, at the cost of one cached local file read
            except (EodhdTransientError, EodhdShapeError, EodhdCapError):  # one symbol failed -> skip it
                return  # leave coverage unrecorded; this symbol is retried on the next launch (pool stays alive)
            manifest.set_coverage(conn, sym, floor)  # record floor (None = no coverage)
            if run_stats is not None:  # job 13 AC-3: this run's own coverage-rows-added tally
                run_stats["coverage_rows_added"] = run_stats.get("coverage_rows_added", 0) + 1

    await asyncio.gather(*(asyncio.create_task(one(s)) for s in symbols))  # the discovery pool
    return stop.is_set()  # True = cap reached


async def _run_backfill_phases(http, key, ledger, limiter, timeout, conn, staging_root, queue,
                               concurrency, deep_batch, force, run_stats=None):
    """Do the actual phased-backfill work and return the result dict. Never stamps completion itself.

    This is `run_backfill`'s real body, split out (SF2 fold, priority-seed-fix brief) so the public
    function below can wrap it in exactly ONE completion-stamp site instead of the six early-return
    paths each carrying their own copy of the stamp call. Any exception raised anywhere in here
    (an auth failure, a fatal universe-fetch error) propagates straight out of this function, past
    the wrapper's stamp line, so a run that never completed can never claim that it did.

    `run_stats` (job 13, trailing keyword-default so every existing caller is unaffected): the
    caller's own mutable tally dict, forwarded unchanged into every `run_units`/`_discover_batch`
    call below so it reflects everything this run has completed, even if this function later
    raises or the process is interrupted (the caller created it BEFORE calling here, so it
    outlives any exception raised inside this function).
    """
    # Seed the ledger from the live counter before spending anything (brief §8), treating a stale
    # (prior-day) count or an explicit force as zero so a lagging /user display cannot false-stall.
    used, limit, _extra, requests_date = await read_usage(http, key, timeout)  # global daily usage + its date
    ledger.cap = min(ledger.cap, limit)  # honour the account's real cap
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # the UTC date to test the count's freshness
    ledger.seed(_effective_seed(used, requests_date, today_utc, force))  # fresh same-day count, else 0
    symbols = [row["symbol"] for row in queue]  # priority-ordered symbol list
    # Carry the tier across the layer boundary (priority-seed-fix brief section 3.1), BEFORE the BULK
    # phase below spends a single call, so every drain this run makes can honour it from the start.
    manifest.set_priority(conn, queue)  # one upsert per symbol, in one transaction; set_priority itself commits
    # The per-run trading-life cache (brief S3.3 item 1), created ONCE here (never inside run_units,
    # which runs once per phase and again per deep batch) so RECENT and DEEP share every read.
    life_cache: dict = {}

    # --- Phase BULK: today's whole-exchange EOD + splits + dividends (validate + seed top-up) ---
    day_epoch, _day_str = _utc_day()  # the bulk unit grain (one row per UTC day)
    for interval in BULK_INTERVALS:  # enqueue the three bulk units for today
        manifest.enqueue(conn, BULK_TICKER, interval, day_epoch, day_epoch,
                         relative_path(interval, day_str=_day_str))
    conn.commit()  # persist the enqueue
    cap = await run_units(http, key, ledger, limiter, timeout, conn, staging_root,
                          _pending_list(conn, BULK_INTERVALS), concurrency, life_cache, run_stats=run_stats)
    if cap:
        return {"stopped_at": "bulk", "symbols": len(symbols)}

    # --- Phase DAILY: per-ticker EOD full history for every priority name (cheap, makes usable) ---
    for sym in symbols:  # enqueue one daily unit per symbol
        manifest.enqueue(conn, sym, "daily", 0, 0, relative_path("daily", ticker=sym))
    conn.commit()
    cap = await run_units(http, key, ledger, limiter, timeout, conn, staging_root,
                          _pending_list(conn, ("daily",)), concurrency, life_cache, run_stats=run_stats)
    if cap:
        return {"stopped_at": "daily", "symbols": len(symbols)}

    # --- Phase RECENT: native 1h (one request) + native 5m (sub-window concat) per symbol ---
    for sym in symbols:  # enqueue recent intraday units
        manifest.enqueue(conn, sym, "1h", 0, 0, relative_path("1h", ticker=sym))
        manifest.enqueue(conn, sym, "5min", 0, 0, relative_path("5min", ticker=sym))
    conn.commit()
    cap = await run_units(http, key, ledger, limiter, timeout, conn, staging_root,
                          _pending_list(conn, ("1h", "5min")), concurrency, life_cache, run_stats=run_stats)
    if cap:
        return {"stopped_at": "recent", "symbols": len(symbols)}

    # --- Phase DEEP: per-ticker coverage discovery, then the year-aligned 1-min walk, batched ---
    for batch in _chunks(symbols, deep_batch):  # bound memory + interleave discovery/fetch
        cap = await _discover_batch(http, key, ledger, limiter, timeout, conn, staging_root,
                                    batch, concurrency, life_cache, run_stats=run_stats)
        if cap:
            return {"stopped_at": "deep-discovery", "symbols": len(symbols)}
        for sym in batch:  # enqueue 1-min windows for names with coverage
            _discovered, floor = manifest.get_coverage(conn, sym)  # the recorded floor
            if floor is None:  # no 1-min coverage -> skip (already recorded as such)
                continue
            for ws, we in year_aligned_1m_windows(floor, DEEP_1M_END):  # one unit per window/file
                manifest.enqueue(conn, sym, "1min", ws, we,
                                 relative_path("1min", ticker=sym, window_start=ws, year=year_of(ws)))
        conn.commit()
        cap = await run_units(http, key, ledger, limiter, timeout, conn, staging_root,
                              _pending_list(conn, ("1min",), symbols=batch), concurrency, life_cache,
                              run_stats=run_stats)
        if cap:
            return {"stopped_at": "deep-1min", "symbols": len(symbols)}

    return {"stopped_at": None, "symbols": len(symbols)}  # full pass complete (or nothing left)


async def run_backfill(http, key, ledger, limiter, timeout, conn, staging_root, queue,
                       concurrency, deep_batch=300, force=False, run_stats=None):
    """Run the phased backfill over the priority `queue` (list of {symbol,...}). Resumable.

    `run_stats` (job 13, trailing keyword-default so every existing caller is unaffected): the
    CLI's own tally dict, created and owned by the caller so it survives whatever this call does
    (return normally, raise, or the process receiving SIGTERM mid-run); forwarded unchanged into
    `_run_backfill_phases`.

    Returns a short dict of what ran. Stops early (leaving 'pending') if the daily cap is hit in
    any phase. Before any phase spends anything, `queue` is written into the manifest through
    `manifest.set_priority`, so `pending_units`'s join genuinely orders every phase's drain by tier
    (0..3) then position: every phase walks the most useful names first because the manifest itself
    now carries that fact, not because the caller happened to enqueue in that order (priority-seed-
    fix brief, closing the gap this docstring used to claim was already closed). force=True ignores
    the displayed /user count at the initial seed (the run-now path): a stale near-cap display never
    false-stalls; cap safety then rests on local charges + the periodic reconcile + the margin (the
    reconcile is always honest. See _reconcile).

    The completion stamp lives HERE and only here (SF2 fold, priority-seed-fix brief section 4): every
    one of `_run_backfill_phases`'s early returns (cap reached in any phase) and its full-pass return
    alike land back here and hit this ONE stamp line, so a later phase added to the plan needs no new
    stamp call of its own to stay monitored. A raised exception (auth failure, a fatal universe-fetch
    error) propagates straight past this line, so a run that never completed never claims that it did;
    this is why the stamp is a plain statement after the await and not inside a try/finally, which
    would stamp on the exception path too.
    """
    result = await _run_backfill_phases(http, key, ledger, limiter, timeout, conn, staging_root,
                                        queue, concurrency, deep_batch, force,
                                        run_stats=run_stats)  # the real work
    manifest.set_meta(conn, manifest.LAST_BACKFILL_META_KEY, str(int(time.time())))  # the one stamp site
    return result  # unchanged: the same dict the phases returned


def ended_label(result: dict) -> str:
    """Job 13 AC-5: state cap-reached (naming the phase) DISTINCTLY from a fully drained queue.

    Pure (no I/O), so a probe asserts both branches without running a backfill at all. `result`
    is exactly what `run_backfill` returns: {'stopped_at': <phase name> | None, 'symbols': n}.
    """
    stopped_at = result.get("stopped_at")  # the phase name the cap-pause stopped in, or None
    return "queue_drained" if stopped_at is None else f"cap_reached:phase={stopped_at}"


def build_run_summary_line(run_stats: dict, calls_spent: int, effective_cap: int,
                           outstanding_by_interval: dict, cost_by_interval: dict,
                           ended: str, elapsed_s: float) -> str:
    """Compose job 13's exit summary (AC-3, AC-4, AC-5, AC-8, and what-can-break item 6's own
    elapsed/calls-spent signal). Pure (no I/O), so a probe asserts the exact text offline, the
    __main__.build_report_line precedent this mirrors.

    run_stats            : {'done', 'empty', 'error', 'skipped', 'coverage_rows_added'}, tallied
                            live as THIS run's own code already saw each outcome (never re-queried
                            from the manifest, which has no run-id column to filter by).
    calls_spent          : the ledger's own running total at the moment this is composed.
    effective_cap        : the ledger's enforced daily ceiling (cap - margin) for this run.
    outstanding_by_interval, cost_by_interval: the remaining-days model's two inputs (AC-4):
                            manifest.outstanding_counts_by_interval's own count, and config's own
                            per-grain call cost.
    ended                : 'cap_reached:phase=<x>' | 'queue_drained' | 'interrupted (SIGTERM)' |
                            'interrupted (exception or early exit)' -- AC-5's own distinct states.
    elapsed_s             : wall time since this run started.
    """
    parts = [
        f"done={run_stats.get('done', 0)}",
        f"empty={run_stats.get('empty', 0)}",
        f"error={run_stats.get('error', 0)}",
        f"skipped={run_stats.get('skipped', 0)}",
        f"calls_spent={calls_spent}",
        f"coverage_rows_added={run_stats.get('coverage_rows_added', 0)}",
    ]
    total_outstanding_calls = sum(  # AC-4's model: outstanding units per grain times that grain's cost
        outstanding_by_interval.get(interval, 0) * cost_by_interval.get(interval, 0)
        for interval in cost_by_interval
    )
    if effective_cap > 0:  # the honest case: a real, positive daily ceiling to divide by
        remaining_days = total_outstanding_calls / effective_cap
        parts.append(f"remaining_days={remaining_days:.2f} "
                     f"(model=outstanding_units_per_grain*cost_per_grain/effective_daily_cap)")
    else:  # STOP IF 2's logged default: never a guess, name the gap instead of dividing by zero
        parts.append(f"remaining_days=unavailable (reason=effective_daily_cap_non_positive:{effective_cap})")
    parts.append(f"ended={ended}")  # AC-5: cap-reached (with its phase) is reported distinctly from queue-drained
    parts.append(f"elapsed_s={elapsed_s:.1f}")  # what-can-break item 6: a slowdown is visible run over run
    return "RUN SUMMARY: " + " ".join(parts)
