"""CLI entrypoint for the EODHD raw fetcher (brief §9).

  python -m research_lab.fetcher verify-endpoints   # probe + record the universe shapes (run first)
  python -m research_lab.fetcher backfill            # the resumable run (gated on a verify pass)
  python -m research_lab.fetcher status              # manifest progress + bytes on disk
  python -m research_lab.fetcher reset-folded        # job 11: reset OLD duplicate-timestamp errors
  python -m research_lab.fetcher errors               # job 13: error units grouped by cause, no network

The secret is read from ~/.research-worker.env and never logged. The staging root defaults to
the locked /mnt/ssd/eodhd (override with EODHD_STAGING_ROOT for a test).
"""

import argparse  # subcommand parsing
import asyncio  # run the async flows
import fcntl  # advisory file lock for the single-instance backfill guard
import json  # print the verify record
import os  # paths + byte counting
import signal  # job 13: catch SIGTERM so an interrupted run still leaves its summary (AC-8)
import sys  # exit codes
import time  # epoch stamps for the last-run meta + the heartbeat line age
from collections import defaultdict  # tally audit failures by reason
from datetime import datetime, timezone  # the UTC stamp leading each heartbeat line

import httpx  # the async client

from . import audit, manifest, universe  # the content audit + the store + the universe sourcing
from .backfill import build_run_summary_line, ended_label, run_backfill  # the orchestrator + job 13's summary bits
from .client import (CostLedger, EodhdAuthError, EodhdCapError, EodhdShapeError,
                     EodhdTransientError, RateLimiter, read_api_key, read_usage)
from .config import (ALL_INTERVALS, COST_BY_INTERVAL, DEFAULT_CONCURRENCY, DEFAULT_DAILY_MARGIN,
                     DEFAULT_HTTP_TIMEOUT_S, DEFAULT_RETRY_MAX, MAJOR_VENUES_ONLY,
                     TIMESERIES_INTERVALS,
                     StagingNotMountedError, mountpoint, require_staging_mounted, staging_root)
# job 11's reset-folded subcommand reuses the #242 quarantine unit's own condemned-list reader and
# daily-file check (composed, not copied): quarantine_242.py imports manifest/storage/tradinglife,
# never this module, so importing it here creates no import cycle. The reset logic itself lives in
# manifest.reset_folded_units, which imports neither this module nor .client, so a probe that
# calls it directly (bypassing this CLI file entirely) can prove AC-5's no-network-call guarantee
# without ever pulling in the client import this file's own top level carries for every OTHER
# subcommand.
from .ops import quarantine_242


def _manifest_path(root: str) -> str:
    """The manifest lives at <staging_root>/manifest.db."""
    return os.path.join(root, "manifest.db")


def _open_manifest(root: str):
    """Guard the SSD mount, then ensure the staging root exists and open the manifest connection."""
    require_staging_mounted()  # refuse if the SSD is not mounted (never makedirs/write on the root disk)
    os.makedirs(root, exist_ok=True)  # the SSD eodhd dir
    return manifest.connect(_manifest_path(root))  # WAL connection + schema


async def _cmd_verify(args) -> int:
    """Probe the universe/index endpoint shapes and record the pass (the backfill gate)."""
    root = staging_root()  # the SSD eodhd dir
    conn = _open_manifest(root)  # the manifest
    try:
        key = read_api_key()  # fail loud if absent
        async with httpx.AsyncClient() as http:  # one client for the probe
            record = await universe.verify_endpoints(http, key, DEFAULT_HTTP_TIMEOUT_S, conn)  # the live probe
        print("verify-endpoints PASSED:")  # human summary
        print(json.dumps(record, indent=2))  # the recorded fields + the index-availability fact
        return 0
    finally:
        conn.close()  # release the WAL connection so the sidecar checkpoints cleanly


def _run_log_path() -> str:
    """job 13: the per-run exit-summary log on the ROOT disk (readable when the SSD is detached,
    the same reasoning _report_log_path already carries for the daily heartbeat). Overridable via
    EODHD_RUN_LOG for a test.
    """
    return os.environ.get("EODHD_RUN_LOG", os.path.expanduser("~/eodhd_runs.log"))


async def _cmd_backfill(args) -> int:
    """Run the resumable phased backfill (refuses to start without a recorded verify pass).

    Job 13 (docs/handoffs/2026-09-04_fetcher/13_job_run_visibility.md): a per-unit line reaches
    stdout as each unit lands (AC-1/AC-2, via `run_stats` threaded into `run_backfill`), and an
    exit summary (AC-3/AC-4/AC-5) is written from exactly ONE call site, `_emit_summary` below,
    reached alike by a clean return, any caught exception, and a SIGTERM (AC-8): the three paths
    can never disagree about how the summary is composed because they all call the same closure.
    """
    root = staging_root()  # the SSD eodhd dir
    require_staging_mounted()  # the mount guard, before any file op on the staging root
    os.makedirs(root, exist_ok=True)  # ensure the staging dir exists (guard passed -> on the SSD, not root)
    lock_path = os.path.join(root, "backfill.lock")  # single-instance lock on the SSD (mount guarded above)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)  # the lock file descriptor
    conn = None  # the manifest is opened only AFTER the lock is held (no second WAL connection on a refused start)
    # job 13's own run-level state, created here (the OUTERMOST scope) so it survives whatever
    # happens below: a clean return, a caught exception, or a SIGTERM landing mid-run.
    run_stats = {"done": 0, "empty": 0, "error": 0, "skipped": 0, "coverage_rows_added": 0}
    start_time = time.time()  # AC-3/what-can-break item 6: elapsed time in the exit summary
    ledger = None  # created below; None here means _emit_summary has nothing yet to report
    summary_emitted = False  # guards against the outer finally double-writing after a clean exit
    sigterm_installed = False  # so the finally only removes a handler this call actually installed
    loop = None  # bound once ledger exists, below

    def _emit_summary(ended: str) -> None:
        """The ONE composition + write site (job 13 AC-3, AC-8): reads whatever run_stats/ledger
        hold RIGHT NOW, so a partial run reports exactly what it completed, never a guess."""
        nonlocal summary_emitted
        if ledger is None or conn is None:  # never got far enough to spend anything -> nothing to report
            return
        elapsed = time.time() - start_time
        outstanding = manifest.outstanding_counts_by_interval(conn, DEFAULT_RETRY_MAX)  # AC-4's model input
        line = build_run_summary_line(run_stats, ledger.used, ledger.effective_cap,
                                      outstanding, COST_BY_INTERVAL, ended, elapsed)
        print(line)  # stdout: the timer path's journal captures this line exactly as it does the per-unit ones
        _append_report_line(_run_log_path(), line)  # the root-disk run log, readable with the SSD detached
        summary_emitted = True

    def _on_sigterm() -> None:
        """job 13 AC-8: an interrupted run still leaves the summary of what it completed. Runs
        as an ordinary asyncio loop callback (via loop.add_signal_handler), so it fires between
        two coroutine steps, never while a manifest write is actually in flight."""
        print("SIGTERM received: writing the partial-run summary before exit", file=sys.stderr)
        try:
            _emit_summary("interrupted (SIGTERM)")
        finally:
            sys.stdout.flush()  # os._exit skips normal interpreter cleanup, so flush explicitly first
            sys.stderr.flush()
            os._exit(143)  # 128 + SIGTERM(15): the conventional signal exit code

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # non-blocking exclusive lock
        except BlockingIOError:  # the LOCK_NB found the lock held -> another backfill is running
            print(f"another backfill is already running (lock held on {lock_path})", file=sys.stderr)
            return 5  # distinct rc: concurrent backfill refused
        conn = _open_manifest(root)  # safe now: we hold the lock (the guard re-runs harmlessly, idempotent)
        # job 11 what-can-break row 4's own signal, printed at backfill's own start too (job 13's lean),
        # not only by `status`: a migration that silently failed to land shows up here as well.
        print(f"schema version: {manifest.schema_version(conn)}")
        if not manifest.verify_passed(conn):  # the §9 gate (M2)
            print("REFUSING to backfill: run `python -m research_lab.fetcher verify-endpoints` first.",
                  file=sys.stderr)
            return 2
        key = read_api_key()  # fail loud if absent
        ledger = CostLedger(daily_cap=args.daily_cap, margin=args.margin)  # the cap accounting
        limiter = RateLimiter(req_per_min=args.req_per_min)  # the per-minute guard
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)  # job 13 AC-8
        sigterm_installed = True
        async with httpx.AsyncClient() as http:  # the shared client (per-request timeouts govern)
            try:
                # Source the universe (verified live) and build the priority queue.
                active = await universe.fetch_symbol_rows(http, key, DEFAULT_HTTP_TIMEOUT_S, delisted=False)
                delisted = await universe.fetch_symbol_rows(http, key, DEFAULT_HTTP_TIMEOUT_S, delisted=True)
                seed = universe.load_seed(root)  # optional user watchlist
                queue = universe.build_priority_queue(active, delisted, seed)  # tiers 0..3
                if args.limit:  # an optional cap on the queue (for a first smoke launch)
                    queue = queue[:args.limit]
                print(f"universe: {len(active)} active, {len(delisted)} delisted; "
                      f"queue={len(queue)} symbols (seed={len(seed)}, "
                      f"major_venues_only={MAJOR_VENUES_ONLY})")  # the venue scope in force, not assumed
                result = await run_backfill(http, key, ledger, limiter, DEFAULT_HTTP_TIMEOUT_S,
                                            conn, root, queue, args.concurrency, force=args.force,
                                            run_stats=run_stats)
            except EodhdAuthError as exc:  # bad key on the universe fetch OR mid-run -> fail loud, rc=3
                print(f"AUTH FAILURE: {exc}", file=sys.stderr)
                return 3
            except (EodhdShapeError, EodhdTransientError, EodhdCapError) as exc:  # universe / initial /user read failed
                # The universe fetch (or the pre-spend /user read in run_backfill) failed before any backfill
                # work. Surface a clean message + a distinct rc instead of letting a stack trace escape main().
                print(f"UNIVERSE FETCH FAILURE: {exc}", file=sys.stderr)
                return 7  # distinct rc: the universe/symbol-list (or /user) fetch failed before backfill work
        print(f"backfill returned: {result} (used={ledger.used}/{ledger.cap})")  # progress summary
        # Record this run's outcome for the daily heartbeat (P6). Additive meta, exactly the pattern of
        # the verify-endpoints gate write; no engine logic changes. The ExecStartPost `report` reads it.
        manifest.set_meta(conn, "last_run", json.dumps({
            "rc": 0,  # ExecStartPost only fires on a successful (rc=0) ExecStart, so this is always 0 here
            "stopped_at": result.get("stopped_at"),  # which phase the cap-pause stopped in (or None)
            "used": ledger.used,  # local end-of-run counter
            "cap": ledger.cap,  # the effective daily cap in force
            "ts": int(time.time()),  # epoch of this run, to age the line
        }))
        _emit_summary(ended_label(result))  # AC-5: cap-reached (naming the phase) vs queue-drained, distinctly
        return 0
    finally:
        if not summary_emitted:  # every early return above (lock refusal aside) lands here uncredited
            _emit_summary("interrupted (exception or early exit)")
        if sigterm_installed and loop is not None:
            loop.remove_signal_handler(signal.SIGTERM)  # never leave the process-wide handler installed past this run
        if conn is not None:  # only if we opened it (a lock-refused start never did)
            conn.close()  # release the WAL connection so the sidecar checkpoints cleanly
        os.close(lock_fd)  # release the single-instance lock (flock also auto-releases on process death)


def _dir_bytes(path: str) -> int:
    """Total bytes under a directory (best-effort; missing dir -> 0)."""
    total = 0  # accumulate
    for root, _dirs, files in os.walk(path):  # walk the tree
        for f in files:  # sum file sizes
            try:
                total += os.path.getsize(os.path.join(root, f))
            except (FileNotFoundError, PermissionError):  # a vanished or unreadable file -> skip it in the sum
                pass
    return total  # bytes on disk


async def _cmd_status(args) -> int:
    """Print manifest progress (per interval) + coverage + bytes on disk."""
    root = staging_root()  # the SSD eodhd dir
    conn = _open_manifest(root)  # the manifest
    try:
        print("verify-endpoints:", "PASSED" if manifest.verify_passed(conn) else "NOT RUN")
        # what-can-break row 4's own signal: the schema version, read live and reported at startup
        # (job 11 AC-11), so a migration that silently failed to land shows up here rather than
        # only being discoverable by reading the file with a separate tool.
        print(f"  schema version: {manifest.schema_version(conn)}")
        tally = manifest.counts(conn)  # {interval: {status: n}}
        for interval in sorted(tally):  # one line per interval
            print(f"  {interval:16s} {tally[interval]}")
        discovered, with_data = manifest.coverage_counts(conn)  # via the manifest API (schema stays in one module)
        print(f"  coverage: {discovered} discovered, {with_data} with 1-min data, {discovered-with_data} empty")
        raw = os.path.join(root, "raw")  # the raw staging tree
        print(f"  bytes on disk (raw/): {_dir_bytes(raw)/1e9:.2f} GB")  # rough footprint
        # live quota (best-effort: status must still report the offline manifest progress above)
        try:
            key = read_api_key()  # the secret (raises EodhdAuthError if absent -> caught below)
            async with httpx.AsyncClient() as http:  # one-shot client for the free /user read
                used, limit, _extra, qdate = await read_usage(http, key, DEFAULT_HTTP_TIMEOUT_S)  # counter + its date
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # the UTC date the count must match to be live
            stale = " (STALE prior-day count; real quota is fresh until the first request resets it)" \
                if qdate != today else ""  # surface the lag so a near-cap stale number is not misread
            print(f"  quota: {used}/{limit} used on {qdate}{stale} "
                  f"(effective stop at {limit - DEFAULT_DAILY_MARGIN})")
        except (EodhdAuthError, EodhdCapError, EodhdShapeError, EodhdTransientError) as exc:  # honest degrade (httpx timeout/transport are wrapped into EodhdTransientError by fetch_json)
            print(f"  quota: unavailable ({exc})")  # the manifest progress above is unaffected
        return 0
    finally:
        conn.close()  # release the WAL connection


async def _cmd_reset_folded(args) -> int:
    """Reset OLD duplicate-timestamp 'error' units to 'pending' (job 11), refusing any ticker the
    #242 retired-ticker guard would not protect (on the condemned list AND missing a daily file,
    what-can-break item 2). Touches only the manifest connection: no HTTP client is ever
    constructed on this path (AC-5), matching manifest.reset_folded_units's own guarantee.
    """
    root = staging_root()  # the SSD eodhd dir
    conn = _open_manifest(root)  # the manifest
    try:
        candidates = manifest.duplicate_class_tickers(conn)  # tickers with at least one fold-able error
        condemned = quarantine_242.load_condemned_csv(args.csv)  # the 801-style list, reused not re-parsed
        # The #242 guard's own trigger, reused rather than reimplemented (daily_file_missing is a
        # one-line wrapper around tradinglife.daily_date_span, the SAME function the shared bottom
        # trim itself calls): a ticker only refuses if it is BOTH condemned AND has no daily file,
        # never on condemned-list membership alone (most condemned tickers DO have a daily record
        # that already lets the ordinary trim protect them).
        refuse = frozenset(
            t for t in candidates
            if t in condemned and quarantine_242.daily_file_missing(root, t)
        )
        result = manifest.reset_folded_units(conn, refuse)
        print(f"reset-folded: reset {result['reset_by_interval']}; "
              f"refused {result['refused_count']} (no daily evidence): {result['refused_tickers']}; "
              f"calls_spent=0")  # what-can-break item 6's own signal: this path never spends a call
        return 0
    finally:
        conn.close()  # release the WAL connection


async def _cmd_errors(args) -> int:
    """job 13 (AC-6/AC-7/AC-9): group 'error' units by DERIVED cause and by retry-ceiling
    standing, and enumerate every status the manifest can hold, WITHOUT issuing a network call.

    Declared async only to match the handler-dispatch table in main() (asyncio.run per command);
    the whole body is a synchronous manifest read. This function never reads EODHD_API_KEY and
    never constructs an httpx client, matching reset-folded's own no-network shape.
    """
    root = staging_root()  # the SSD eodhd dir
    conn = _open_manifest(root)  # the manifest (mount guard runs inside _open_manifest)
    try:
        report = manifest.error_report(conn, DEFAULT_RETRY_MAX)
        print(f"errors: {report['total_error']} 'error' unit(s), grouped by cause "
              f"(retry ceiling = {DEFAULT_RETRY_MAX} attempts)")
        for reason in sorted(report["by_reason"]):  # a stable, readable order
            bucket = report["by_reason"][reason]
            print(f"  {reason:24s} retryable={bucket['retryable']:6d}  abandoned={bucket['abandoned']:6d}")
        # job 13 what-can-break item 5: every status this schema can hold, named, even at zero,
        # so a status absent from this list is impossible rather than merely unlikely.
        print("status enumeration (every status fetch_unit can hold):")
        for status in manifest.ALL_STATUSES:
            print(f"  {status:10s} {report['status_counts'].get(status, 0)}")
        return 0
    finally:
        conn.close()  # release the WAL connection


async def _cmd_audit(args) -> int:
    """Content-verify the manifest's 'done' parquets on disk (read-back + schema + row-count + structure).

    Strengthened from the existence-only reconcile: each done unit is re-opened and proven to carry
    the expected schema, the recorded row count, a monotonic de-duplicated time axis, and a non-empty
    non-all-null key column (audit.verify_unit). Returns rc=6 if ANY unit fails (a distinct code from
    rc=4 mount / rc=5 lock, so a scheduled run can alarm on content drift). --interval scopes the run
    to one interval (e.g. 'daily') so a run need not re-read the larger intraday trees.

    Declared async only to match the handler-dispatch table in main() (asyncio.run per command); its I/O
    (the manifest scan + the per-unit parquet reads) is synchronous and blocks for the audit's duration.
    """
    root = staging_root()  # the SSD eodhd dir
    conn = _open_manifest(root)  # the manifest (mount guard runs inside _open_manifest)
    try:
        total = good = 0  # counters over the checked done units
        failures = []  # (reason, interval, ticker, path) for every unit that failed a content check
        by_reason = defaultdict(int)  # reason-prefix -> count, for the summary tally
        for ticker, interval, _ws, _we, path, row_count in manifest.iter_done(conn):  # stream done units (O(1) memory)
            if args.interval and interval != args.interval:  # optional scope: skip other intervals (no file read)
                continue
            total += 1  # count this checked unit
            ok, reason = audit.verify_unit(root, interval, path, row_count)  # the content verification
            if ok:
                good += 1  # verified-good
            else:
                failures.append((reason, interval, ticker, path))  # named failure for the report
                by_reason[reason.split(":", 1)[0]] += 1  # tally by reason prefix (e.g. 'schema_drift')
        scope = f" (interval={args.interval})" if args.interval else ""  # echo the scope in the header
        print(f"audit{scope}: {total} checked, {good} verified-good, {len(failures)} failed")
        for prefix in sorted(by_reason):  # the per-reason tally
            print(f"  {prefix:14s} {by_reason[prefix]}")
        for reason, interval, ticker, path in failures[:args.show]:  # first N named failures for triage
            print(f"  FAIL  {interval:12s} {ticker:14s} {path}  [{reason}]")
        if len(failures) > args.show:  # never silently truncate -> say what was dropped
            print(f"  ... and {len(failures) - args.show} more (raise --show to list them)")
        return 6 if failures else 0  # rc=6 on any content failure (distinct from rc=4 mount / rc=5 lock)
    finally:
        conn.close()  # release the WAL connection


def _report_log_path() -> str:
    """The daily heartbeat log on the ROOT disk (must stay readable when the SSD is detached).

    Defaults to ~/eodhd_daily_report.log; a test points EODHD_REPORT_LOG at a temp file.
    """
    return os.environ.get("EODHD_REPORT_LOG", os.path.expanduser("~/eodhd_daily_report.log"))


def _append_report_line(path: str, line: str) -> None:
    """Append one line to the report log, creating it if absent (append-only daily history)."""
    with open(path, "a", encoding="utf-8") as fh:  # append mode: never truncate prior days' lines
        fh.write(line + "\n")  # one heartbeat per line


def _last_report_date(path: str):
    """Return the UTC date (YYYY-MM-DD) leading the log's last non-blank line, or None if absent/empty.

    Used by --if-absent: the daily backstop timer no-ops when today already has a line.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:  # read the existing history
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]  # drop blank lines
    except FileNotFoundError:  # no log yet -> treat as "today absent" so the first line gets written
        return None
    if not lines:  # an empty file -> also absent
        return None
    leading = lines[-1].split(" ", 1)[0]  # the 'YYYY-MM-DDThh:mmZ' stamp token of the last line
    return leading[:10] if len(leading) >= 10 else None  # the date part, or None if the line is malformed


def _parse_last_run(raw):
    """Parse the manifest 'last_run' meta JSON into {rc, stopped_at, ts}, or None if absent/malformed."""
    if not raw:  # no run recorded yet
        return None
    try:
        d = json.loads(raw)  # the JSON the backfill wrote
        return {"rc": int(d["rc"]), "stopped_at": d["stopped_at"], "ts": int(d["ts"])}  # the fields the line needs
    except (ValueError, KeyError, TypeError) as exc:  # malformed meta -> degrade to none, but never bury it
        # The backfill writes this meta as well-formed JSON, so a parse failure is a real signal (truncated
        # write / corruption / schema drift), not the same as "no run yet". Warn to stderr (-> journal) so a
        # reader can tell a corrupt meta from an absent one; still return None so the report never crashes.
        print(f"last_run meta malformed (ignored, reported as last_run=none): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def build_report_line(ts_iso: str, mounted: bool, quota, store_gb, interval_counts,
                      errors_total: int, last_run, now_epoch: int, note=None) -> str:
    """Compose one daily-heartbeat line. Pure (no I/O) so the probe asserts the format offline.

    ts_iso         : the UTC stamp leading the line (its date is the dedup key for --if-absent).
    mounted        : SSD mount state at report time (the headline).
    quota          : (used, limit, apiRequestsDate) from the live /user read, or None when unmounted/unreadable.
                     apiRequestsDate is the UTC date the count is FOR; a prior date means the count is stale
                     (the real quota is fresh until the first request of the day resets it).
    store_gb       : raw/ footprint in GB, or None when unmounted.
    interval_counts: {interval: {status: n}} from the manifest, or None when unmounted.
    errors_total   : sum of 'error' units across intervals (surfaced only when > 0, never hidden).
    last_run       : {"rc", "stopped_at", "ts"} of the last backfill, or None.
    now_epoch      : current epoch, to age the last_run stamp.
    note           : free-text trailing field for a degraded/skip reason (the WHY); omitted when None.
    """
    parts = [ts_iso]  # every line starts with the UTC stamp
    parts.append("mount=ok" if mounted else "mount=absent")  # the headline mount state
    # The budget answer is the live global counter. 'na' when there is no SSD to even try; 'unavailable'
    # when the SSD is present but the /user read failed (the note then says why).
    if quota is None:
        parts.append("quota=na" if not mounted else "quota=unavailable")
    else:
        used, limit, qdate = quota  # the live triple (count, cap, the date the count is FOR)
        parts.append(f"quota={used}/{limit}")  # used / the real daily cap
        parts.append(f"qdate={qdate}")  # the UTC date the count is FOR; a prior date = stale (real quota fresh)
        parts.append(f"effstop={limit - DEFAULT_DAILY_MARGIN}")  # the margin-adjusted stop, for context
    parts.append(f"store={store_gb:.2f}GB" if store_gb is not None else "store=na")  # the footprint
    if interval_counts is not None:  # per-interval done/empty/pending/skipped for the four spend targets
        for interval in TIMESERIES_INTERVALS:  # daily, 1min, 1h, 5min
            c = interval_counts.get(interval, {})  # missing statuses default to 0
            # Fourth field: units the trading-life guard skipped without ever requesting them (brief
            # S3.3 item 6). Zero-fills on a fresh manifest exactly as the other three already do.
            parts.append(f"{interval}={c.get('done', 0)}/{c.get('empty', 0)}/"
                        f"{c.get('pending', 0)}/{c.get('skipped', 0)}")
        if errors_total:  # surface error units only when there are any (never silently hide them)
            parts.append(f"errors={errors_total}")
    if last_run is None:  # the last backfill's outcome (secondary to the live quota)
        parts.append("last_run=none")
    else:
        age_h = max(0.0, (now_epoch - int(last_run["ts"])) / 3600.0)  # hours since the last run
        parts.append(f"last_run=rc{last_run['rc']}/{last_run['stopped_at']}/{age_h:.1f}h")
    if note:  # the trailing free-text reason, always last so the consumer reads it as the line remainder
        parts.append(f"note={note}")
    return " ".join(parts)  # one space-delimited key=value line


async def _cmd_report(args) -> int:
    """Append one daily-heartbeat line to the root-disk report log (the P6 budget-spend push).

    Mounted: read the manifest counts + store bytes + the live /user quota and emit the full line.
    Unmounted: emit the 'SSD not mounted — no spend' line to the SAME root-disk log WITHOUT touching
    the SSD (the manifest lives on the SSD; the log must stay readable when the SSD is detached). The
    absence of a line for a day is itself a signal the chain broke, so the skip-day line states the WHY.
    --if-absent makes the daily backstop a no-op on days the per-run ExecStartPost already wrote a line.
    """
    log_path = _report_log_path()  # the root-disk log (never the SSD)
    now_epoch = int(time.time())  # for the line stamp + the last-run age
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")  # UTC stamp; its date is the dedup key
    today = ts_iso[:10]  # the UTC date for --if-absent
    if args.if_absent and _last_report_date(log_path) == today:  # today already reported
        return 0  # backstop no-op: the per-run report covered today

    if not os.path.ismount(mountpoint()):  # the SSD is detached -> report the WHY, touch nothing on the SSD
        line = build_report_line(
            ts_iso, False, None, None, None, 0, None, now_epoch,
            note=f"SSD not mounted at {mountpoint()} — no spend today; remount to resume",
        )
        _append_report_line(log_path, line)  # to the root disk
        return 0

    # Mounted: gather the offline manifest facts first (a mount race here falls back to the skip line).
    try:
        root = staging_root()  # the SSD eodhd dir
        conn = _open_manifest(root)  # opens with the mount guard; raises if the SSD vanished just now
    except StagingNotMountedError:  # the mount dropped between the ismount check and the open
        line = build_report_line(
            ts_iso, False, None, None, None, 0, None, now_epoch,
            note=f"SSD unmounted mid-report at {mountpoint()} — no spend recorded",
        )
        _append_report_line(log_path, line)
        return 0
    try:
        tally = manifest.counts(conn)  # {interval: {status: n}}
        errors_total = sum(s.get("error", 0) for s in tally.values())  # any error units, surfaced if > 0
        store_gb = _dir_bytes(os.path.join(root, "raw")) / 1e9  # the raw footprint in GB
        last_run = _parse_last_run(manifest.get_meta(conn, "last_run"))  # rc/stopped_at/ts of the last run
    finally:
        conn.close()  # release the WAL connection before the (networked) quota read

    quota = None  # the live budget answer; stays None if the read fails
    note = None  # only set on a degraded read, to say why the number is missing
    try:
        key = read_api_key()  # the secret (raises EodhdAuthError if absent)
        async with httpx.AsyncClient() as http:  # one-shot client for the free /user read
            used, limit, _extra, qdate = await read_usage(http, key, DEFAULT_HTTP_TIMEOUT_S)  # counter + its date
        quota = (used, limit, qdate)  # the headline budget triple (the date flags a stale prior-day count)
    except (EodhdAuthError, EodhdCapError, EodhdShapeError, EodhdTransientError) as exc:  # honest degrade
        note = f"quota read failed: {exc}"  # the budget number is the whole point, so record why it's absent

    line = build_report_line(ts_iso, True, quota, store_gb, tally, errors_total, last_run, now_epoch, note=note)
    _append_report_line(log_path, line)  # to the root disk
    return 0


def main(argv=None) -> int:
    """Parse subcommands and dispatch to the async handlers."""
    parser = argparse.ArgumentParser(prog="research_lab.fetcher", description="EODHD raw fetcher")
    sub = parser.add_subparsers(dest="cmd", required=True)  # one of verify-endpoints/backfill/status

    sub.add_parser("verify-endpoints", help="probe + record the universe endpoint shapes (run first)")

    bf = sub.add_parser("backfill", help="run the resumable phased backfill")
    bf.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)  # in-flight requests
    bf.add_argument("--daily-cap", type=int, default=100000)  # the daily call cap
    bf.add_argument("--margin", type=int, default=2000)  # safety headroom below the cap
    bf.add_argument("--req-per-min", type=int, default=1000)  # the per-minute rate ceiling
    bf.add_argument("--limit", type=int, default=0)  # optional: cap the queue (0 = whole universe)
    bf.add_argument("--force", action="store_true",  # run-now: ignore the displayed /user count at the seed
                    help="ignore the displayed /user count at the initial seed (the run-now path): a stale "
                         "near-cap display never false-stalls. Cap safety still rests on local charges + the "
                         "periodic reconcile + the margin.")

    sub.add_parser("status", help="print manifest progress + bytes on disk")

    rf = sub.add_parser("reset-folded", help="job 11: reset OLD duplicate-timestamp 'error' units to pending")
    rf.add_argument("--csv", default=quarantine_242.DEFAULT_CONDEMNED_CSV,  # the #242 guard's own condemned list
                    help=f"the 801-style condemned-ticker CSV the #242 guard checks (default {quarantine_242.DEFAULT_CONDEMNED_CSV})")

    au = sub.add_parser("audit", help="content-verify manifest 'done' parquets on disk (read-back + schema + row-count)")
    au.add_argument("--show", type=int, default=20)  # how many failed units to list
    au.add_argument("--interval", default=None, choices=list(ALL_INTERVALS),  # optional: scope to one interval
                    help="only audit this interval (e.g. daily); default audits every interval")

    rp = sub.add_parser("report", help="append one daily-heartbeat line to the root-disk report log (P6)")
    rp.add_argument("--if-absent", action="store_true",  # the daily backstop timer uses this
                    help="no-op when today's UTC date already has a line (so the daily timer adds nothing "
                         "on days the per-run ExecStartPost already reported)")

    sub.add_parser("errors", help="job 13: group 'error' units by cause and retry-ceiling standing, no network call")

    args = parser.parse_args(argv)  # parse
    handler = {"verify-endpoints": _cmd_verify, "backfill": _cmd_backfill, "status": _cmd_status,
               "reset-folded": _cmd_reset_folded, "audit": _cmd_audit, "report": _cmd_report,
               "errors": _cmd_errors}[args.cmd]
    try:
        return asyncio.run(handler(args))  # run the chosen async flow
    except StagingNotMountedError as exc:  # the SSD is not mounted -> refuse loudly, write nothing on root
        print(str(exc), file=sys.stderr)  # the clear remount instruction
        return 4  # distinct rc: SSD not mounted


if __name__ == "__main__":
    sys.exit(main())  # propagate the exit code
