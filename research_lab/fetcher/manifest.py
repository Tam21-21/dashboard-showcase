"""The SQLite manifest: the resume + idempotence spine (brief §6).

One table `fetch_unit` keyed by the concatenated primary key (ticker, interval,
window_start, window_end), a DDIA Ch 4 point-lookup / ordered-range-scan index
over the flat parquet. Plus `coverage` (per-ticker 1-min floor), `meta` (the
verify-endpoints gate and the run-completion stamp), and `priority` (the tier
+ position `set_priority` writes once per run, so `pending_units`'s join can
order every drain by what the fetcher's own queue judged most useful, per the
priority-seed-fix brief). WAL for crash safety; a single async writer serialises
writes, so no write-contention design is needed.

Job 11 (docs/handoffs/2026-09-04_fetcher/11_job_download_preconditions.md, AC-11) adds two
additive, nullable columns, and the DDIA reasoning is read from kleppmann_canon.md before either
is built. Chapter 5 (line 1305: "relational DBs add nullable columns cheaply, schema evolution
fills missing fields with nulls") is why both are plain ALTER TABLE ADD COLUMN, never a rebuild:
`fetch_unit.folded_rows` (NULL on every row written before this unit; a real fetch_unit primary
key already exists so there is nothing to backfill) and `priority.exchange` (NULL on a seed-tier
row, which is exactly AC-8's fail-open case). Chapter 3 (line 448, prefer an id over free text) and
chapter 4 (line 1075, a concatenated key fixes a prefix) argued for keying the exchange onto
`priority` by ticker rather than inventing a second lookup: `priority`'s primary key is already the
ticker, so no new index is needed to read it back. `priority.exchange` lives on `priority`, not on
`fetch_unit`, because the venue is a per-ticker fact and `fetch_unit`'s key is per UNIT (up to seven
grains per ticker); copying the venue onto every grain's row would restate the same fact up to seven
times per ticker and let the copies drift. Both migrations run inside the manifest's existing WAL
journal (chapter 4's write-ahead log; see `connect` below), so an ALTER that lands mid-crash replays
cleanly rather than leaving a half-migrated schema.
"""

import re  # job 13: pull an HTTP status class out of a stored error string
import sqlite3  # the embedded store; WAL gives crash safety
import time  # fetched_at timestamps
from collections import defaultdict  # tally status counts per interval

from .config import ALL_INTERVALS  # the fixed interval enum, to scan done units per interval (index-friendly)
# Job 13's error-cause classifier reuses the validator's own reason-code constants rather than
# retyping them as literals (the ibkr_costs precedent this map's own night-ten note describes: a
# probe or classifier that restates a rule loses the ability to catch that rule changing under
# it). validate.py imports only from .config, never from this module, so this import creates no
# cycle.
from .validate import REASON_DUPLICATE_DISAGREES, REASON_NON_MONOTONIC

# The DDL is idempotent (IF NOT EXISTS) so init is safe to call on every launch.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_unit (
  ticker        TEXT    NOT NULL,            -- a real symbol, or '__BULK_US__' for bulk units
  interval      TEXT    NOT NULL,            -- daily | 1min | 1h | 5min | bulk_eod | bulk_splits | bulk_dividends
  window_start  INTEGER NOT NULL,            -- epoch s; 0 for whole-history units; day epoch for bulk
  window_end    INTEGER NOT NULL,            -- epoch s; 0 for whole-history units; = window_start for bulk
  status        TEXT    NOT NULL,            -- pending | done | empty | error | skipped
  row_count     INTEGER,                     -- rows validated + written
  path          TEXT,                        -- parquet path relative to the staging root
  error         TEXT,                        -- last redacted error context, or the skip reason; NULL when done/empty
  attempts      INTEGER NOT NULL DEFAULT 0,  -- retry counter
  fetched_at    INTEGER,                     -- epoch s of the validated write
  folded_rows   INTEGER,                     -- job 11: rows folded on write; NULL = written before the fold existed
  PRIMARY KEY (ticker, interval, window_start, window_end)
);
CREATE INDEX IF NOT EXISTS ix_fetch_unit_status ON fetch_unit (interval, status);
CREATE TABLE IF NOT EXISTS coverage (
  ticker        TEXT PRIMARY KEY,            -- the symbol
  min_1m_ts     INTEGER,                     -- earliest 1-min epoch; NULL = no coverage (skip 1-min)
  discovered_at INTEGER                      -- when discovery ran
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,                    -- e.g. 'verify_endpoints_passed'
  value TEXT                                 -- the recorded value (JSON or scalar)
);
CREATE TABLE IF NOT EXISTS priority (
  ticker     TEXT PRIMARY KEY NOT NULL,  -- the symbol, matching fetch_unit.ticker exactly
  tier       INTEGER NOT NULL,   -- 0 seed, 1 active major, 2 active other, 3 delisted
  position   INTEGER NOT NULL,   -- rank within the queue, so within-tier order is stable across runs
  updated_at INTEGER NOT NULL,   -- epoch of the write, so a stale mapping is visible rather than assumed
  exchange   TEXT                -- job 11: the venue build_priority_queue derived; NULL for a seed-tier row
);
"""

# The two additive columns job 11 owns (AC-11), checked LIVE via PRAGMA rather than trusted from a
# stored flag: a version this function reports can never disagree with what the schema actually
# holds, even if a future migration changes shape. (table, column) pairs, not a bare column list,
# because `folded_rows` and `exchange` sit on two different tables.
_SCHEMA_V2_COLUMNS = (("fetch_unit", "folded_rows"), ("priority", "exchange"))


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, column_def: str) -> None:
    """Add a column via ALTER TABLE; no-op if it already exists.

    The manifest's own version of researchdb._add_column_if_missing's shape (the fetcher package
    does not import the dashboard, so the shape is rewritten here rather than shared). SQLite's
    ALTER TABLE ADD COLUMN errors if the column exists, so PRAGMA table_info is checked first,
    which is what makes calling this on every `connect()` safe (idempotent by construction, proved
    twice on a copy of the real manifest by AC-11's probe).
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # row[1] = column name
    if column not in existing:  # only the first call on a given manifest file actually alters anything
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the manifest's schema version, derived live from PRAGMA table_info (what-can-break
    row 4's own signal: "the schema version read and reported at startup"). Version 1 is the
    original three-table-plus-priority schema; version 2 adds both job-11 columns. A manifest that
    somehow gained only one of the two (should never happen, since both migrate in the same
    `connect()` call) still reports 1, because "version 2" means the WHOLE job-11 migration landed,
    not a partial slice of it.
    """
    have = set()
    for table, column in _SCHEMA_V2_COLUMNS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in cols:
            have.add((table, column))
    return 2 if have == set(_SCHEMA_V2_COLUMNS) else 1


def connect(db_path: str) -> sqlite3.Connection:
    """Open the manifest in WAL mode and ensure the schema exists.

    WAL = the write-ahead-log primitive (DDIA Ch 4): a crash mid-write replays
    cleanly rather than corrupting the page file.
    """
    conn = sqlite3.connect(db_path)  # one connection per process (asyncio is single-threaded)
    conn.execute("PRAGMA journal_mode=WAL")  # crash-safe journaling
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe durability with good throughput
    conn.executescript(_SCHEMA)  # idempotent DDL (a FRESH manifest gets both job-11 columns here)
    # An EXISTING manifest predates the two job-11 columns: CREATE TABLE IF NOT EXISTS above is a
    # no-op on a table that already exists, so the columns are migrated in explicitly, on every
    # connect (idempotent: a no-op once already migrated, per _add_column_if_missing above).
    _add_column_if_missing(conn, "fetch_unit", "folded_rows", "INTEGER")
    _add_column_if_missing(conn, "priority", "exchange", "TEXT")
    conn.commit()  # persist the schema
    return conn  # caller owns the connection lifetime


def enqueue(conn: sqlite3.Connection, ticker: str, interval: str,
            window_start: int, window_end: int, path: str) -> None:
    """Create a 'pending' unit if it does not already exist; never reset a finished one.

    Idempotent: a unit already done/empty/error keeps its state (ON CONFLICT DO NOTHING),
    so re-enqueuing the whole plan only adds genuinely new work.
    """
    conn.execute(
        "INSERT INTO fetch_unit (ticker, interval, window_start, window_end, status, path) "
        "VALUES (?, ?, ?, ?, 'pending', ?) "
        "ON CONFLICT (ticker, interval, window_start, window_end) DO NOTHING",
        (ticker, interval, window_start, window_end, path),
    )


def mark_done(conn: sqlite3.Connection, ticker: str, interval: str,
              window_start: int, window_end: int, row_count: int, path: str,
              folded_rows: int = 0) -> None:
    """Upsert a unit to 'done' after a validated atomic write. Clears any prior error.

    folded_rows (job 11, AC-1) is the count of repeated-but-identical rows the validator dropped
    before this write; it defaults to 0 for bulk products and any caller that has not been
    updated to pass it, never NULL, because "written under the new code, zero folds" and "written
    before the fold existed" are genuinely different facts and the column's own NULL is reserved
    for the latter (existing rows keep NULL through the additive migration; this function never
    writes NULL for a fresh write).
    """
    conn.execute(
        "INSERT INTO fetch_unit (ticker, interval, window_start, window_end, status, "
        "row_count, path, error, fetched_at, folded_rows) VALUES (?, ?, ?, ?, 'done', ?, ?, NULL, ?, ?) "
        "ON CONFLICT (ticker, interval, window_start, window_end) DO UPDATE SET "
        "status='done', row_count=excluded.row_count, path=excluded.path, "
        "error=NULL, fetched_at=excluded.fetched_at, folded_rows=excluded.folded_rows",
        (ticker, interval, window_start, window_end, row_count, path, int(time.time()), folded_rows),
    )
    conn.commit()  # commit immediately so a crash right after never loses a done


def mark_empty(conn: sqlite3.Connection, ticker: str, interval: str,
               window_start: int, window_end: int) -> None:
    """Mark a unit 'empty' — a real coverage gap (no data), distinct from an error."""
    conn.execute(
        "INSERT INTO fetch_unit (ticker, interval, window_start, window_end, status, "
        "row_count, error, fetched_at) VALUES (?, ?, ?, ?, 'empty', 0, NULL, ?) "
        "ON CONFLICT (ticker, interval, window_start, window_end) DO UPDATE SET "
        "status='empty', row_count=0, error=NULL, fetched_at=excluded.fetched_at",
        (ticker, interval, window_start, window_end, int(time.time())),
    )
    conn.commit()  # persist the terminal empty state


def mark_skipped(conn: sqlite3.Connection, ticker: str, interval: str,
                 window_start: int, window_end: int, reason: str) -> None:
    """Mark a unit 'skipped' — deliberately never requested, because it cannot hold this ticker's bars.

    Distinct from 'empty' on purpose. 'empty' means EODHD was asked and had nothing; 'skipped'
    means the daily history already on disk proves the requested window lies outside the ticker's
    trading life, so asking would spend calls and (for a reused symbol) import another company's
    bars. Terminal like 'empty': pending_units never re-selects it. The reason is stored so the
    decision is auditable rather than invisible.
    """
    conn.execute(
        "INSERT INTO fetch_unit (ticker, interval, window_start, window_end, status, "
        "row_count, error, fetched_at) VALUES (?, ?, ?, ?, 'skipped', 0, ?, ?) "
        "ON CONFLICT (ticker, interval, window_start, window_end) DO UPDATE SET "
        "status='skipped', row_count=0, error=excluded.error, fetched_at=excluded.fetched_at",
        (ticker, interval, window_start, window_end, reason[:500], int(time.time())),
    )
    conn.commit()  # persist the terminal skipped state


def mark_error(conn: sqlite3.Connection, ticker: str, interval: str,
               window_start: int, window_end: int, error: str) -> None:
    """Mark a unit 'error', increment attempts, store the redacted context for re-queue."""
    conn.execute(
        "INSERT INTO fetch_unit (ticker, interval, window_start, window_end, status, "
        "error, attempts) VALUES (?, ?, ?, ?, 'error', ?, 1) "
        "ON CONFLICT (ticker, interval, window_start, window_end) DO UPDATE SET "
        "status='error', error=excluded.error, attempts=fetch_unit.attempts+1",
        (ticker, interval, window_start, window_end, error),
    )
    conn.commit()  # persist so the attempt count survives a crash


def is_done(conn: sqlite3.Connection, ticker: str, interval: str,
            window_start: int, window_end: int) -> bool:
    """True iff the unit is already done, empty OR skipped. All three mean 'no re-fetch needed'.

    'skipped' joins the terminal set here (brief S3.4 item 2): mark_skipped's own docstring already
    claimed this ("Terminal like 'empty': pending_units never re-selects it"), which was true of
    pending_units and false of this function until now.
    """
    row = conn.execute(
        "SELECT status FROM fetch_unit WHERE ticker=? AND interval=? "
        "AND window_start=? AND window_end=?",
        (ticker, interval, window_start, window_end),
    ).fetchone()
    return row is not None and row[0] in ("done", "empty", "skipped")  # terminal-success states


# The one definition of "outstanding": status 'pending', plus 'error' still under the retry ceiling.
# Hoisted to a module-level constant (SF4 fold, priority-seed-fix brief) so `pending_units` and
# `run_reached_tier` share the SAME string rather than each maintaining its own copy that happens to
# match today. `run_reached_tier`'s docstring claims "'Outstanding' means exactly what `pending_units`
# selects"; before this fold that claim was true only by coincidence between two independently edited
# literals, and a later change to one (a new terminal status, say) would have silently falsified it
# with nothing to catch the drift.
_OUTSTANDING_PREDICATE = "(f.status='pending' OR (f.status='error' AND f.attempts < ?))"


def pending_units(conn: sqlite3.Connection, max_attempts: int,
                  intervals=None, tickers=None):
    """Yield (ticker, interval, window_start, window_end, path, attempts) needing a fetch.

    Pending = status 'pending', plus 'error' units still under the retry ceiling. Done, empty and
    skipped are all terminal and excluded. Optional `intervals` / `tickers` push the filter into
    SQL (the PK leads with ticker, so a ticker IN clause is an index range scan, not a
    full-manifest Python scan). This keeps the deep-1-min batch loop fast as the manifest grows to
    millions of rows.

    Ordered by interval, then the priority tier (0..3; a ticker with no `priority` row sorts last
    via the sentinel 99, from `LEFT JOIN priority` + `COALESCE`, so an unmapped unit is drained
    rather than dropped), then position within the tier (the queue's own order, from `set_priority`),
    then ticker as the deterministic total-order tie-break, then window_start so one ticker's own
    pending windows still drain oldest first (priority-seed-fix brief section 3.2). Every column
    reference is qualified with its table alias, including inside the two string-built filter
    clauses below: `priority` declares its own `ticker`, so an unqualified reference is ambiguous
    once the join exists, and SQLite raises `sqlite3.OperationalError: ambiguous column name` on
    every call shape if it is left bare.
    """
    clauses = [_OUTSTANDING_PREDICATE]  # the resumable predicate, qualified, shared with run_reached_tier
    params = [max_attempts]  # bound params in order
    if intervals is not None:  # restrict to the phase's intervals (uses ix_fetch_unit_status)
        if not intervals:  # an empty interval set matches nothing — never silently fall through to ALL
            return
        clauses.append(f"f.interval IN ({','.join('?' * len(intervals))})")
        params.extend(intervals)
    if tickers is not None:  # restrict to a batch of symbols (uses the PK's leading ticker column)
        if not tickers:  # an empty batch matches nothing — avoid the SQL 'IN ()' syntax error
            return
        clauses.append(f"f.ticker IN ({','.join('?' * len(tickers))})")
        params.extend(tickers)
    sql = ("SELECT f.ticker, f.interval, f.window_start, f.window_end, f.path, f.attempts "
           "FROM fetch_unit f LEFT JOIN priority p ON p.ticker = f.ticker "
           "WHERE " + " AND ".join(clauses) +
           " ORDER BY f.interval, COALESCE(p.tier, 99), COALESCE(p.position, 0), f.ticker, f.window_start")
    cur = conn.execute(sql, params)  # run it
    yield from cur  # a generator; the caller materialises a bounded slice per phase/batch


def set_priority(conn: sqlite3.Connection, queue) -> None:
    """Upsert the whole priority queue in ONE transaction (priority-seed-fix brief section 3.1).

    `queue` is the list of dicts `build_priority_queue` returns: {symbol, tier, type, exchange,
    delisted}. Each entry's `position` is its own zero-based index in `queue`, so tier-then-position
    reproduces the queue's exact order and within-tier order is never silently degraded to ticker
    order. `updated_at` is stamped with one epoch for the whole call, so every row this call touches
    carries the same write instant. `exchange` (job 11, AC-6) is written straight from the value
    `build_priority_queue` already computed, via `row.get("exchange")` so a queue row with no
    exchange key at all (an older caller, a hand-built test row) binds NULL rather than raising: a
    seed-tier row's `exchange` is genuinely `None` (AC-8's fail-open case), and a missing key must
    read exactly the same as an explicit `None`, never differently.

    An EMPTY queue is a zero-iteration loop: nothing is written, nothing is deleted. This is
    deliberate. A universe fetch that returned nothing (an API failure, an empty symbol list) must
    never wipe the ordering for the whole store as a side effect of an unrelated fault. A symbol
    absent from `queue` simply keeps whatever row it already had (advisory, possibly stale, never
    deleted): the row can misorder a later fetch and can never cause one.

    A queue row missing EITHER `symbol` or `tier` (row.get(...) is None for that field) binds NULL
    and raises `sqlite3.IntegrityError` from the table's own NOT NULL constraint, rather than
    silently defaulting one field (a silently defaulted tier is the inert-priority defect wearing a
    different hat) and rather than raising a bare `KeyError` on the other (SF1 fold, priority-seed-fix
    brief: `symbol` and `tier` are now read the same way, `row.get(...)`, so the guarantee below
    holds for either shape of malformed row, not only a missing tier). The whole upsert runs inside
    `with conn:` (priority-seed-fix brief, transaction-handling fold), so the guarantee below is
    unconditional rather than scoped to one exception type: on ANY exception, not only
    `sqlite3.IntegrityError`, the WHOLE call rolls back and the exception propagates unchanged, so a
    bad row never half-writes the map. Every upsert this call made is undone, and every row from an
    earlier, already-committed call is untouched (they live in a separate transaction).
    """
    if not queue:  # a zero-length list or tuple, see the empty-queue contract above
        return  # nothing to iterate, so nothing is touched; existing rows are left exactly as they were
    now = int(time.time())  # one write epoch for this whole batch (this run's instant, not per-row)
    # row.get('symbol') and row.get('tier'), never row['symbol'] or row['tier']: EITHER missing key
    # must reach the table's own NOT NULL constraint as a NULL bind value, not raise a KeyError
    # before the constraint gets a chance to fire (SF1 fold). A generator, not a list, so the queue
    # is never held twice in memory at once.
    rows = ((row.get("symbol"), row.get("tier"), position, now, row.get("exchange"))
            for position, row in enumerate(queue))  # position = the entry's zero-based index in the queue
    # `with conn:` commits this whole batch on clean exit and rolls back on ANY exception raised
    # inside it (priority-seed-fix brief, transaction-handling fold). This replaces a hand-rolled
    # try/except/rollback/raise/commit that only caught `sqlite3.IntegrityError`: any other failure
    # inside `executemany` (an `OperationalError` from a locked or I/O-erroring manifest, an
    # `InterfaceError` from an unbindable value) used to leave the connection holding an open,
    # partially-written transaction with no rollback at all. The context manager never closes the
    # connection, only commits or rolls back its own transaction, so `conn` stays usable afterward.
    with conn:
        # executemany prepares the INSERT statement ONCE for the whole batch (SF3 fold): a per-row
        # conn.execute re-prepares it roughly 59,756 times against the real manifest, which is wasted
        # work for a statement whose shape never changes across rows.
        conn.executemany(
            "INSERT INTO priority (ticker, tier, position, updated_at, exchange) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (ticker) DO UPDATE SET tier=excluded.tier, position=excluded.position, "
            "updated_at=excluded.updated_at, exchange=excluded.exchange",
            rows,
        )


def get_priority_exchange(conn: sqlite3.Connection, ticker: str):
    """Return the ticker's stored venue from `priority` (job 11, AC-6/AC-8), or None.

    None covers BOTH "no priority row at all" (never queued, or queued from a batch older than
    this column) and "a priority row exists but its exchange is NULL" (the seed tier, tier 0,
    which build_priority_queue marks with exchange=None on purpose). Both are the SAME fail-open
    case for a caller deciding whether to skip discovery (AC-8): a ticker this function cannot
    place in a known venue is never silently dropped, only a ticker POSITIVELY placed outside the
    six-venue set is.
    """
    row = conn.execute("SELECT exchange FROM priority WHERE ticker=?", (ticker,)).fetchone()
    return row[0] if row else None  # no row -> None; a row with a NULL exchange -> None either way


LAST_BACKFILL_META_KEY = "last_backfill_finished_at"  # the meta key run_backfill stamps on every exit


def priority_map_age(conn: sqlite3.Connection, reference_epoch: int) -> str | int:
    """Return the age in seconds of the OLDEST `priority.updated_at`, measured against `reference_epoch`.

    Returns the exact string 'NOT YET WRITTEN' while the table is empty (ops registry row
    `eodhd_priority_map_age`, priority-seed-fix brief section 4). `MIN(updated_at)` over zero rows
    is SQL NULL, and rendering that as a large or alarming age would flag every fresh install as
    stale, which is the same failure class as rendering a NULL state as a colour. Once at least one
    row exists this always returns an `int`, never a NULL and never a number standing in for absence.
    """
    row = conn.execute("SELECT MIN(updated_at) FROM priority").fetchone()  # NULL iff the table is empty
    oldest = row[0]  # the earliest write instant recorded, or None
    if oldest is None:  # no run has ever written the map
        return "NOT YET WRITTEN"  # words, not a number and not a null (per DECISIONS #211's state rule)
    return int(reference_epoch) - int(oldest)  # the age, in seconds, of the staleest mapped row


def run_reached_tier(conn: sqlite3.Connection, max_attempts: int) -> str | int:
    """Return the LOWEST-NUMBERED tier still holding an outstanding unit (ops registry row
    `eodhd_run_reached_tier`, priority-seed-fix brief section 4, failure mode 4).

    'Outstanding' means exactly what `pending_units` selects: status 'pending', plus 'error' still
    under `max_attempts`. 'done', 'empty' and 'skipped' are all terminal and never outstanding.

    Evaluated in this order, and no other:
      1. 'NO RUN YET': no backfill has ever completed (LAST_BACKFILL_META_KEY is unset).
      2. 'ALL TIERS REACHED': a run has completed and nothing is outstanding.
      3. 'NO PRIORITY MAPPING': every outstanding unit's ticker has no priority row, so the query's
         own `COALESCE(p.tier, 99)` sentinel is the minimum; 99 is never a tier and is never rendered
         as one, because that would put a meaningless number on the page.
      4. the `int` MIN(tier) over the outstanding set: the highest-priority tier not yet finished.
         This is deliberately MIN, not MAX: tier 3 is enqueued alongside every other tier and holds
         pending units in any run that has not finished, so MAX would read 3 for a run that cleared
         tiers 0-2 and for a run that barely started tier 0 alike, and could never tell them apart.
    """
    if get_meta(conn, LAST_BACKFILL_META_KEY) is None:  # no backfill pass has ever completed
        return "NO RUN YET"  # words: a run genuinely has not happened, not merely "unknown"
    row = conn.execute(  # the SAME _OUTSTANDING_PREDICATE constant pending_units uses, joined the same way
        "SELECT MIN(COALESCE(p.tier, 99)) FROM fetch_unit f LEFT JOIN priority p ON p.ticker = f.ticker "
        "WHERE " + _OUTSTANDING_PREDICATE,
        (max_attempts,),
    ).fetchone()
    lowest = row[0]  # NULL iff no unit anywhere is outstanding
    if lowest is None:  # nothing pending or retryable, anywhere in the manifest
        return "ALL TIERS REACHED"  # words: no tier number can express "nothing is left"
    if lowest == 99:  # every outstanding unit's ticker is unmapped: the sentinel, never a real tier
        return "NO PRIORITY MAPPING"  # a stale or missing map showing up in the progress monitor
    return int(lowest)  # the lowest tier still holding outstanding work, as recorded; 0-3 is the
                         # convention build_priority_queue follows, not a range this schema enforces


def counts(conn: sqlite3.Connection) -> dict:
    """Return {interval: {status: n}} for the status subcommand."""
    out: dict = defaultdict(lambda: defaultdict(int))  # interval -> status -> count
    for interval, status, n in conn.execute(
        "SELECT interval, status, COUNT(*) FROM fetch_unit GROUP BY interval, status"
    ):
        out[interval][status] = n  # fill the tally
    return {k: dict(v) for k, v in out.items()}  # plain dicts for printing


def coverage_counts(conn: sqlite3.Connection) -> tuple:
    """Return (discovered, with_1min_data) for the status report. Keeps the schema in this module."""
    row = conn.execute("SELECT COUNT(*), COUNT(min_1m_ts) FROM coverage").fetchone()  # total vs non-NULL floor
    return row[0], row[1]  # discovered, with-data (discovered - with-data = empty-coverage names)


def iter_done(conn: sqlite3.Connection):
    """Yield (ticker, interval, window_start, window_end, path, row_count) for every 'done' unit.

    A streaming generator (not a materialised list) so the audit stays O(1) memory as the manifest
    grows to millions of rows. Scans per interval so each query uses ix_fetch_unit_status
    (interval, status) as an index range scan, not a full-table scan on status alone. The caller
    stats each path against the staging root and reconciles the recorded row_count against the
    parquet's actual row count (the content audit). row_count is read from the existing column
    (no schema change); mark_done always writes it, so it is non-NULL for a done unit.
    """
    for interval in ALL_INTERVALS:  # one indexed range scan per interval (the composite index leads with interval)
        cur = conn.execute(
            "SELECT ticker, interval, window_start, window_end, path, row_count FROM fetch_unit "
            "WHERE interval=? AND status='done'", (interval,)
        )
        yield from cur  # streamed; bounded memory


def set_coverage(conn: sqlite3.Connection, ticker: str, min_1m_ts) -> None:
    """Record a ticker's discovered earliest 1-min epoch (None = no coverage)."""
    conn.execute(
        "INSERT INTO coverage (ticker, min_1m_ts, discovered_at) VALUES (?, ?, ?) "
        "ON CONFLICT (ticker) DO UPDATE SET min_1m_ts=excluded.min_1m_ts, "
        "discovered_at=excluded.discovered_at",
        (ticker, min_1m_ts, int(time.time())),
    )
    conn.commit()  # persist the discovered floor


def get_coverage(conn: sqlite3.Connection, ticker: str):
    """Return (discovered: bool, min_1m_ts: int|None). discovered=False if never probed."""
    row = conn.execute(
        "SELECT min_1m_ts FROM coverage WHERE ticker=?", (ticker,)
    ).fetchone()
    if row is None:  # never discovered
        return False, None
    return True, row[0]  # discovered; min_1m_ts may be NULL (no coverage)


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write a meta key (e.g. the verify-endpoints pass record)."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()  # persist immediately


def get_meta(conn: sqlite3.Connection, key: str):
    """Read a meta key, or None if absent."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None  # the stored value or None


VERIFY_META_KEY = "verify_endpoints_passed"  # the gate key backfill checks (brief §9, M2)


def verify_passed(conn: sqlite3.Connection) -> bool:
    """True iff verify-endpoints recorded a pass — the gate backfill refuses to start without."""
    return get_meta(conn, VERIFY_META_KEY) is not None  # presence = passed


# The OLD, pre-fold free-text message (validate.py wrote this before job 11's fold existed). The
# reset below targets exactly this population: units abandoned by the bug this unit fixes, never
# a unit that failed for any other reason. Matches the plan's own literal SQL (night-two PLAN.md
# section 7), so a reader comparing this constant against that document sees the same string.
_DUPLICATE_CLASS_LIKE = "%duplicate timestamp%"


def duplicate_class_tickers(conn: sqlite3.Connection) -> set:
    """Return the set of tickers holding at least one 'error' unit from the OLD duplicate-timestamp
    bug (job 11). Read-only: used by the reset-folded CLI wiring to size the #242 guard check
    before anything is written.
    """
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT ticker FROM fetch_unit WHERE status='error' AND error LIKE ?",
        (_DUPLICATE_CLASS_LIKE,),
    )}


def reset_folded_units(conn: sqlite3.Connection, refuse_tickers: frozenset = frozenset()) -> dict:
    """Reset every OLD duplicate-timestamp 'error' unit to 'pending' (job 11, AC-4), except a
    ticker in `refuse_tickers` (the #242 retired-ticker guard: a condemned ticker with no daily
    file must never be reset, job 11 STOP IF 4 / what-can-break item 2, the same shape already
    written into the #242 quarantine unit). `refuse_tickers` is computed by the CALLER (the CLI
    wiring in __main__.py, which reads the condemned CSV and the daily file through
    research_lab.fetcher.ops.quarantine_242's own functions) rather than here, so this function
    never touches the filesystem, never imports research_lab.fetcher.client, and can never open a
    socket (AC-5): it reads and writes the manifest connection it is given and nothing else.

    Never touches a unit whose error is anything else (AC-4's vendor-404 case): the WHERE clause
    below is identical in shape to duplicate_class_tickers's own, so the two can never drift apart
    on which population is "fold-able".

    Runs as ONE transaction (`with conn:`, set_priority's own idiom): a snapshot of the candidate
    (ticker, interval) pairs is read first so the per-grain tally and the refused list are both
    derived from the SAME rows the single UPDATE statement below acts on, then one UPDATE resets
    every candidate whose ticker is not refused. Returns {'reset_by_interval': {interval: n},
    'refused_count': n, 'refused_tickers': [sorted names]}.
    """
    with conn:  # commit on clean exit, rollback on any exception (nothing half-resets)
        candidates = conn.execute(
            "SELECT ticker, interval FROM fetch_unit WHERE status='error' AND error LIKE ?",
            (_DUPLICATE_CLASS_LIKE,),
        ).fetchall()
        reset_by_interval: dict = defaultdict(int)  # per-grain count of units this call actually resets
        refused_tickers = set()  # tickers the #242 guard is protecting this call
        for ticker, interval in candidates:
            if ticker in refuse_tickers:
                refused_tickers.add(ticker)
            else:
                reset_by_interval[interval] += 1
        if refused_tickers:  # widen the WHERE by one clause only when there is something to exclude
            placeholders = ",".join("?" * len(refused_tickers))
            conn.execute(
                "UPDATE fetch_unit SET status='pending', attempts=0, error=NULL "
                f"WHERE status='error' AND error LIKE ? AND ticker NOT IN ({placeholders})",
                (_DUPLICATE_CLASS_LIKE, *sorted(refused_tickers)),
            )
        else:  # the plan's own literal SQL, unwidened, when nothing needs excluding
            conn.execute(
                "UPDATE fetch_unit SET status='pending', attempts=0, error=NULL "
                "WHERE status='error' AND error LIKE ?",
                (_DUPLICATE_CLASS_LIKE,),
            )
    return {
        "reset_by_interval": dict(reset_by_interval),
        "refused_count": len(refused_tickers),
        "refused_tickers": sorted(refused_tickers),
    }


# ---------------------------------------------------------------------------------------------
# Job 13 (docs/handoffs/2026-09-04_fetcher/13_job_run_visibility.md): run visibility. Everything
# below only reads fetch_unit; none of it opens a socket or writes a row, so a probe calling one
# of these functions directly (never through __main__.py, whose top level imports .client for
# every OTHER subcommand) proves the same no-network shape reset_folded_units already proves.

# The five statuses the DDL comment above (_SCHEMA's fetch_unit.status line) declares. A single
# named tuple rather than a magic set re-typed at each call site, so a sixth status added to the
# schema comment is a ONE-place edit every reader of this constant sees (job 13 what-can-break
# item 5: "the report enumerates every status the manifest can hold").
ALL_STATUSES = ("pending", "done", "empty", "error", "skipped")


class UnknownStatusError(RuntimeError):
    """fetch_unit holds a status value outside ALL_STATUSES. Raised rather than silently folded
    into an existing bucket or dropped, so a status this module has never heard of is IMPOSSIBLE
    to report on invisibly (job 13 what-can-break item 5), not merely unlikely to occur.
    """


def assert_known_statuses(conn: sqlite3.Connection) -> None:
    """Raise UnknownStatusError naming any fetch_unit.status value outside ALL_STATUSES.

    Cheap regardless of table size: DISTINCT status returns at most five rows. Called by
    error_report below before it counts anything, so a future status added to the schema without
    updating ALL_STATUSES is caught the first time a report is asked for.
    """
    found = {row[0] for row in conn.execute("SELECT DISTINCT status FROM fetch_unit").fetchall()}
    unknown = found - set(ALL_STATUSES)
    if unknown:  # a status value this module was never told about
        raise UnknownStatusError(
            f"fetch_unit holds status value(s) outside the known set {ALL_STATUSES}: {sorted(unknown)}"
        )


def outstanding_counts_by_interval(conn: sqlite3.Connection, max_attempts: int) -> dict:
    """Return {interval: n} of units still outstanding (job 13 AC-4's remaining-days model input).

    "Outstanding" is exactly what pending_units already selects: the SAME _OUTSTANDING_PREDICATE
    this module uses for the drain order and for run_reached_tier, so this can never quietly
    define the word a second, different way. One GROUP BY query, never a materialised row list,
    because the remaining-days figure only needs a count per interval.

    DDIA (kleppmann_canon.md, chapter 4, the B-tree section, line 101's own worked lookup, and the
    concatenated-index entry at line 39: "composite key joining multiple columns; the lexicographic
    index from a phone book"): grouping by `interval` reuses the EXISTING composite index
    `ix_fetch_unit_status (interval, status)` as a leading-column range scan, one page-tree descent
    per interval value, rather than a full table scan filtered afterward, because `interval` is
    that index's leading column (the phone-book analogy: scanning by last name, the index's first
    column, is cheap; scanning by first name alone, its second column, is not). No new index is
    added: this is the SAME shape `counts()` and `iter_done()` already exploit over the same
    composite index, never a second query pattern the schema would have to serve.
    """
    out = {interval: 0 for interval in ALL_INTERVALS}  # zero-fill every known interval up front
    for interval, n in conn.execute(
        "SELECT f.interval, COUNT(*) FROM fetch_unit f WHERE " + _OUTSTANDING_PREDICATE +
        " GROUP BY f.interval", (max_attempts,),
    ):
        out[interval] = n
    return out


# Reason-class matchers for classify_error_reason, checked in this exact order (first match
# wins). The two validator codes are imported by NAME above, never re-typed as literals here.
_OLD_DUPLICATE_TEXT = "duplicate timestamp"  # the pre-fold free-text bug (job 11); the same
                                              # substring _DUPLICATE_CLASS_LIKE's LIKE pattern
                                              # targets, without the SQL wildcard characters
_TRADING_LIFE_TEXT = "cannot read daily history at"  # TradingLifeUnreadable's own message shape
                                                      # (tradinglife.py); that exception carries
                                                      # no separate reason code of its own
_STORAGE_PREFIX = "storage:"  # _process_unit's own OSError-wrap prefix (backfill.py)
_HTTP_STATUS_RE = re.compile(r"HTTP (\d)\d\d")  # the leading digit of any HTTP status client.py
                                                 # embeds in an error string (client.py's own
                                                 # f"... HTTP {status} ..." shape)
_NETWORK_TEXT = "network failure after"  # fetch_json's own wording when a timeout/transport
                                          # error exhausts retries without ever getting a status
REASON_OTHER = "other"  # the honest catch-all: an error shape this classifier has never seen


def classify_error_reason(error_text) -> str:
    """Map one stored fetch_unit.error string to a DERIVED reason class (job 13 AC-6).

    Pure (no I/O), so a probe exercises every branch with a plain string fixture. Order matters:
    checked top to bottom, first match wins. A missing/empty error text is answered by name
    rather than raised on, since a status='error' row could in principle carry no stored text.
    """
    if not error_text:  # defensive: every real mark_error call writes a string, but never assume
        return "no_reason_recorded"
    if REASON_DUPLICATE_DISAGREES in error_text:  # the current validator's own reason code
        return REASON_DUPLICATE_DISAGREES
    if REASON_NON_MONOTONIC in error_text:  # the current validator's own reason code
        return REASON_NON_MONOTONIC
    if _OLD_DUPLICATE_TEXT in error_text:  # the OLD pre-fold free-text bug, job 11's own target
        return "duplicate_timestamp_old"
    if _TRADING_LIFE_TEXT in error_text:  # a corrupt daily parquet blocked the life narrowing
        return "trading_life_unreadable"
    if error_text.startswith(_STORAGE_PREFIX):  # an OSError writing the parquet
        return "storage_error"
    m = _HTTP_STATUS_RE.search(error_text)  # any client.py exception carrying an HTTP status
    if m:
        return f"http_{m.group(1)}xx"  # bucketed by status CLASS (4xx, 5xx, ...), never the code
    if _NETWORK_TEXT in error_text:  # a timeout/transport failure that never reached a status
        return "network_transient"
    return REASON_OTHER  # an error shape this classifier does not recognise; counted, not dropped


def error_report(conn: sqlite3.Connection, max_attempts: int) -> dict:
    """Group every 'error' unit by DERIVED reason and by retry-ceiling standing (job 13 AC-6),
    touching only `conn`: no socket is ever opened (AC-7), matching reset_folded_units's own
    no-network guarantee.

    Returns {'status_counts': {status: n, one entry per ALL_STATUSES, zero-filled},
             'total_error': n, 'by_reason': {reason: {'retryable': n, 'abandoned': n}}}.
    The two per-reason sub-counts are guaranteed to sum to 'total_error' below (AC-4's
    reconciliation): every error row is classified into exactly one reason and exactly one
    ceiling bucket, so a mismatch here can only mean a bug in this function, and it raises rather
    than reporting a total nobody can trust.
    """
    assert_known_statuses(conn)  # job 13 what-can-break item 5, before anything else is counted
    status_counts = {s: 0 for s in ALL_STATUSES}  # zero-fill every known status up front
    for interval_counts in counts(conn).values():  # {interval: {status: n}} -> flatten to {status: n}
        for status, n in interval_counts.items():
            status_counts[status] = status_counts.get(status, 0) + n
    by_reason: dict = defaultdict(lambda: {"retryable": 0, "abandoned": 0})  # reason -> bucket -> n
    total = 0
    for error_text, attempts in conn.execute("SELECT error, attempts FROM fetch_unit WHERE status='error'"):
        reason = classify_error_reason(error_text)  # the derived class, never the raw free text
        bucket = "retryable" if attempts < max_attempts else "abandoned"  # AC-6's own distinction
        by_reason[reason][bucket] += 1
        total += 1
    reconciled = sum(b["retryable"] + b["abandoned"] for b in by_reason.values())
    if reconciled != status_counts["error"]:  # AC-4: never report a total the manifest disagrees with
        raise RuntimeError(
            f"error report miscounted: classified {reconciled} rows but fetch_unit holds "
            f"{status_counts['error']} status='error' rows"
        )
    return {"status_counts": status_counts, "total_error": total, "by_reason": dict(by_reason)}
