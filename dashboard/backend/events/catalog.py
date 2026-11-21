"""M2.5 brief H — research_datasets catalog refresh + reconciliation.

Spec brief v4 §9 / EL20 / EL28 / arch v6 §5.6.

Two public functions:

  refresh_event_catalog_for(event_type, source, ...)
      UPSERT one `(dataset_type='event_series', event_type, source)` row in
      research.db.research_datasets, recomputing start_at / end_at /
      row_count from events.db via ATTACH. Idempotent; safe to call after
      every successful batch close.

  reconcile_event_catalog(...)
      Walk every event-series catalog row and every distinct (event_type,
      source) present in events.db. Refresh any row that needs it; surface
      `orphan_catalog_rows` (catalog row exists but events.db has no
      surviving active rows for the pair) and `orphan_batches` (events.db
      has active rows for a pair but no catalog row exists). Backstop for
      the eventually-consistent refresh path per arch §5.6 BLv4-2.

Both call sites use SQLite's `INSERT ... ON CONFLICT DO UPDATE` keyed on
the partial unique index `uq_research_datasets_event_series`
(`(event_type, source) WHERE dataset_type='event_series'`) brief A
shipped in researchdb.init_db(). Price-dataset rows are not in that
index, so the UPSERT cannot collide with M2 rows.

Why this lives in events/catalog.py and not eventsdb.py: eventsdb.py
holds the events.db schema + per-connection lifecycle and nothing else.
The catalog refresh ATTACHes events.db onto a research.db connection;
it operates at the boundary between both DBs. Parallel structure with
events/sweeps.py (which also operates at boundaries).
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .. import eventsdb
from .. import researchdb

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogRefreshResult:
    """One per (event_type, source) pair the refresh touched."""
    event_type: str
    source: str
    row_count: int
    # `start_at` / `end_at` are conceptual arch §4.8 names; live columns are
    # `start_ts` / `end_ts`. The values stored are integer epoch seconds (UTC),
    # 0 for an empty series per §9.0 fact 4's COALESCE(..., 0) convention.
    start_at: Optional[int]
    end_at: Optional[int]
    inserted: bool         # True on INSERT (no prior row), False on UPDATE
    changed: bool          # True iff (row_count, start_at, end_at) actually moved
    name: str              # the synthetic series name written into research_datasets.name


@dataclass
class ReconcileResult:
    """Return value from reconcile_event_catalog(). Matches the §6.11 admin
    endpoint response body shape (plus duration_ms which the endpoint adds).
    """
    series_walked: int = 0
    series_updated: int = 0
    refreshes: list[CatalogRefreshResult] = field(default_factory=list)
    # (event_type, source) pairs whose catalog row exists with
    # dataset_type='event_series' but events.db has no surviving active rows
    # behind them.
    orphan_catalog_rows: list[tuple[str, str]] = field(default_factory=list)
    # batch_ids whose events.db `(event_type, source)` has no catalog row.
    # Reconcile creates the catalog row in the same call so this list is
    # the "rows the reconcile job just created" diagnostic.
    orphan_batches: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Series-name derivation per spec brief §9.1
# ---------------------------------------------------------------------------


def _ts_to_date(ts: int) -> str:
    """Convert an epoch-seconds integer to a UTC YYYY-MM-DD string.

    Used inside the series-name builder; the name is UI-facing only and
    never an identifier (EL28 / arch §5.6 SFv5-1 keys on the stable
    `(dataset_type, event_type, source)` tuple).
    """
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _derive_series_name(event_type: str, source: str,
                        start_at: Optional[int],
                        end_at: Optional[int]) -> str:
    """Build the synthetic `research_datasets.name` value for an event series.

    Empty-series sentinel: start_at == end_at == 0 (post-COALESCE in §9.0 /
    fact 4); the function returns `<event_type>_<source>_empty` in that case so
    `1970-01-01_1970-01-01` never appears in a UI label. Spec brief §9.1.
    """
    if start_at is None or end_at is None or (start_at == 0 and end_at == 0):
        return f"{event_type}_{source}_empty"
    return f"{event_type}_{source}_{_ts_to_date(start_at)}_{_ts_to_date(end_at)}"


# ---------------------------------------------------------------------------
# refresh_event_catalog_for — single-pair UPSERT
# ---------------------------------------------------------------------------


# Path validation: SQLite does not accept bind parameters on ATTACH paths,
# so the path is interpolated into the SQL string. We refuse any path
# containing a single-quote or backslash so the interpolation cannot escape
# its own quoting. The default events.db path is a project-owned absolute
# path and never contains these characters.
def _safe_attach_path(path: Union[str, Path]) -> str:
    """Return a single-quoted SQL literal for the ATTACH path; refuse risky chars.

    Per §9 brief text: "shell-escapes the path (no apostrophes or backslashes
    in any project-owned path) and validates that it points to a .db file
    that exists." A future revision that needs an apostrophe in the path
    must add a doubled-quote escape here.
    """
    s = str(path)
    if "'" in s or "\\" in s:
        raise ValueError(
            f"refresh_event_catalog_for: events_db_path {s!r} contains a "
            f"character (single-quote or backslash) that cannot be safely "
            f"interpolated into ATTACH DATABASE"
        )
    return f"'{s}'"


# Sentinel placeholders per spec brief §9.0 fact 4 (the four NOT NULL columns
# `symbol`, `timeframe`, `start_ts`, `end_ts` on research_datasets that don't
# fit an event-series row in any honest sense; brief takes the placeholder
# approach to leave price-dataset code unchanged).
_EVENT_SERIES_SYMBOL = "__event_series__"
_EVENT_SERIES_TIMEFRAME = "event"


def refresh_event_catalog_for(
    *,
    event_type: str,
    source: str,
    now_epoch: Optional[int] = None,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
) -> CatalogRefreshResult:
    """Idempotent UPSERT of one event-series row in research.db.research_datasets.

    Opens a fresh connection on research.db (per researchdb.py's per-call
    lifecycle) and ATTACHes events.db onto it for the MIN/MAX/COUNT
    subqueries. Runs the UPSERT inside one BEGIN IMMEDIATE; DETACHes on
    clean exit OR rollback.

    Spec brief §9 / EL28: the UPSERT's ON CONFLICT key is the partial unique
    index `uq_research_datasets_event_series` (`event_type, source` WHERE
    `dataset_type='event_series'`). Price-dataset rows have
    `dataset_type='price_dataset'` so they cannot collide.

    For an empty series (MIN/MAX return NULL because no active rows exist)
    the COALESCE inside the SQL writes 0 for both bounds so the NOT NULL
    columns stay populated; _derive_series_name reads the two zeros as the
    "empty" sentinel.

    The function is best-effort from the caller's perspective: the caller
    catches any exception and treats it as a logged-not-fatal degradation
    (arch §5.6 BLv4-2's eventually-consistent guarantee). The
    reconcile_event_catalog() backstop at dashboard startup re-runs the
    refresh for every catalog row whose values differ from the events.db
    truth.
    """
    if now_epoch is None:
        now_epoch = int(time.time())

    # Resolve paths so the ATTACH literal is stable across the call. The
    # research_db_path default routes through researchdb.DB_PATH so this
    # function works the same way the rest of the dashboard does.
    epath = events_db_path if events_db_path is not None else eventsdb.DB_PATH
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH
    attach_literal = _safe_attach_path(epath)

    # Open research.db with per-connection pragmas matching researchdb.py.
    conn = sqlite3.connect(str(rpath), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.row_factory = sqlite3.Row
        # ATTACH outside the transaction so a re-entrant call from inside an
        # open transaction would also be safe (SQLite forbids ATTACH inside
        # a transaction). Spec brief §9 fragment uses the same shape.
        conn.execute(f"ATTACH DATABASE {attach_literal} AS events")
        try:
            # First: detect inserted-vs-updated + capture the pre-state for
            # the `changed` flag. UPSERT cannot tell us inserted-vs-updated
            # in SQLite (RETURNING from UPSERT is not portable across all
            # SQLite versions we care about); cheap SELECT first.
            prior = conn.execute(
                """SELECT rows AS row_count, start_ts AS start_at, end_ts AS end_at
                     FROM research_datasets
                    WHERE dataset_type='event_series'
                      AND event_type = ?
                      AND source     = ?""",
                (event_type, source),
            ).fetchone()

            # Recompute from events.db inside the same transaction (the
            # ATTACH'd database is read-aside).
            conn.execute("BEGIN IMMEDIATE;")
            try:
                agg = conn.execute(
                    """SELECT MIN(knowledge_at) AS start_at,
                              MAX(knowledge_at) AS end_at,
                              COUNT(*)          AS row_count
                         FROM events.research_events
                        WHERE event_type = ?
                          AND source     = ?
                          AND status     = 'active'""",
                    (event_type, source),
                ).fetchone()
                row_count = int(agg["row_count"] or 0)
                # COALESCE(..., 0) per §9.0 — both NOT NULL on the live table.
                start_at = int(agg["start_at"]) if agg["start_at"] is not None else 0
                end_at = int(agg["end_at"]) if agg["end_at"] is not None else 0

                series_name = _derive_series_name(event_type, source, start_at, end_at)

                # H4 (pre-M4 A1): gate the type through the registry before the UPSERT, and
                # bind the value from the named constant (not the bare 'event_series' literal in
                # the VALUES below — now a ? placeholder). The ON CONFLICT predicate KEEPS its
                # literal: it is the partial-index DDL predicate, not a writer, and the registry
                # governs writers, not the index.
                researchdb.dataset_storage_kind(researchdb.DATASET_TYPE_EVENT_SERIES)
                # UPSERT keyed on the partial unique index. Live column names
                # per §9.0 verified premise: start_ts / end_ts / rows /
                # last_fetched_at (not the conceptual arch §4.8 names).
                conn.execute(
                    """
                    INSERT INTO research_datasets
                           (name, dataset_type, event_type, source,
                            symbol, timeframe, status,
                            start_ts, end_ts, rows,
                            file_path, sha256, file_bytes,
                            created_at, last_fetched_at)
                    VALUES (?, ?, ?, ?,
                            ?, ?, 'ready',
                            ?, ?, ?,
                            NULL, NULL, NULL,
                            ?, ?)
                    ON CONFLICT (event_type, source) WHERE dataset_type = 'event_series'
                    DO UPDATE SET
                        name            = excluded.name,
                        start_ts        = excluded.start_ts,
                        end_ts          = excluded.end_ts,
                        rows            = excluded.rows,
                        last_fetched_at = excluded.last_fetched_at,
                        -- Lane UI36-0: a row stuck on 'failed' (production id 12, a June
                        -- "docker exited -7") heals to 'ready' only when it holds active
                        -- events AND the newest finished fetch/load batch of its source
                        -- succeeded. Any other status, and last_error, are left alone.
                        status          = CASE
                            WHEN research_datasets.status = 'failed'
                             AND excluded.rows > 0
                             AND (SELECT b.status FROM events.research_event_batches b
                                   WHERE b.source = excluded.source
                                     AND b.kind IN ('fetch', 'load')
                                     AND b.status != 'running'
                                   ORDER BY b.batch_id DESC LIMIT 1)
                                 IN ('succeeded', 'succeeded_with_quarantines')
                            THEN 'ready'
                            ELSE research_datasets.status END
                    """,
                    (series_name, researchdb.DATASET_TYPE_EVENT_SERIES, event_type, source,
                     _EVENT_SERIES_SYMBOL, _EVENT_SERIES_TIMEFRAME,
                     start_at, end_at, row_count,
                     now_epoch, now_epoch),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            inserted = prior is None
            if inserted:
                changed = True
            else:
                changed = (
                    int(prior["row_count"]) != row_count
                    or int(prior["start_at"]) != start_at
                    or int(prior["end_at"]) != end_at
                )
            return CatalogRefreshResult(
                event_type=event_type,
                source=source,
                row_count=row_count,
                start_at=start_at,
                end_at=end_at,
                inserted=inserted,
                changed=changed,
                name=series_name,
            )
        finally:
            # DETACH outside the BEGIN/COMMIT (same reason as ATTACH above).
            # Best-effort: a DETACH failure shouldn't mask the actual return.
            try:
                conn.execute("DETACH DATABASE events")
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# refresh_catalog_for_batch — convenience for write_event_batch close path
# ---------------------------------------------------------------------------


def refresh_catalog_for_batch(
    batch_id: int,
    *,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
) -> list[CatalogRefreshResult]:
    """Enumerate distinct (event_type, source) under batch_id and refresh each.

    Called by write_event_batch on clean close per MO6: "the post-commit
    catalog refresh (write_event_batch step 4) calls refresh_event_catalog_for".
    Returns the per-pair results; the caller logs counts and surfaces via SSE.

    Why distinct over the batch's rows rather than (source, single
    event_type): a load batch can mix event types in principle (the
    canonical R23 loader could read one file with both sec_13d-rejected
    and analyst_rating lines, though the spec brief rejects sec_13d at
    envelope time, leaving analyst-only in practice). Walking distinct
    handles the general case without assuming brief E's per-line policy.
    """
    epath = events_db_path if events_db_path is not None else eventsdb.DB_PATH
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH
    pairs: list[tuple[str, str]] = []
    with eventsdb.get_events_conn(epath) as conn_e:
        rows = conn_e.execute(
            """SELECT DISTINCT event_type, source
                 FROM research_events
                WHERE batch_id = ? AND status = 'active'""",
            (batch_id,),
        ).fetchall()
        pairs = [(r["event_type"], r["source"]) for r in rows]

    results: list[CatalogRefreshResult] = []
    now = int(time.time())
    for event_type, source in pairs:
        try:
            results.append(refresh_event_catalog_for(
                event_type=event_type,
                source=source,
                now_epoch=now,
                events_db_path=epath,
                research_db_path=rpath,
            ))
        except Exception:
            # Per arch §5.6 BLv4-2: catalog refresh is eventually consistent.
            # A failure here does not fail the batch's clean close; the
            # reconcile_event_catalog backstop at dashboard startup picks up
            # any drift.
            log.warning(
                "refresh_event_catalog_for failed for (%s, %s) batch_id=%d; "
                "reconcile at startup will reconcile",
                event_type, source, batch_id, exc_info=True,
            )
    return results


# ---------------------------------------------------------------------------
# reconcile_event_catalog — the eventually-consistent backstop
# ---------------------------------------------------------------------------


def reconcile_event_catalog(
    *,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
) -> ReconcileResult:
    """Walk every event-series catalog row + every (event_type, source) in
    events.db. Refresh any row whose values differ; surface orphans.

    Per spec brief §6.11 + arch §5.6 BLv4-2: the catalog refresh on every
    batch close is best-effort. This function is the load-bearing backstop
    that brings research.db.research_datasets back into agreement with the
    events.db active-row state.

    Idempotent: a reconcile run that finds no drift writes zero changes
    and returns series_updated=0. Brief I's startup wiring calls this once
    on dashboard boot; the §6.11 admin endpoint exposes a manual trigger.
    """
    epath = events_db_path if events_db_path is not None else eventsdb.DB_PATH
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH
    result = ReconcileResult()
    now = int(time.time())

    # 1. Enumerate the union of (event_type, source) pairs from both sides:
    #    catalog rows AND events.db distinct active pairs. The reconcile
    #    function calls refresh_event_catalog_for on every pair so:
    #      - drift on existing catalog rows is corrected
    #      - missing catalog rows (orphan batches' pairs) are inserted
    #    A catalog row with no events.db active rows behind it still gets
    #    a refresh call which writes row_count=0, start_at=0, end_at=0; the
    #    pair lands in orphan_catalog_rows.

    # Catalog side
    catalog_pairs: set[tuple[str, str]] = set()
    catalog_conn = sqlite3.connect(str(rpath), isolation_level=None)
    try:
        catalog_conn.row_factory = sqlite3.Row
        catalog_conn.execute("PRAGMA foreign_keys = ON;")
        for r in catalog_conn.execute(
            """SELECT event_type, source FROM research_datasets
                WHERE dataset_type = 'event_series'""",
        ):
            catalog_pairs.add((r["event_type"], r["source"]))
    finally:
        catalog_conn.close()

    # Events side
    events_pairs: set[tuple[str, str]] = set()
    events_pairs_with_batches: dict[tuple[str, str], list[int]] = {}
    with eventsdb.get_events_conn(epath) as conn_e:
        for r in conn_e.execute(
            """SELECT DISTINCT event_type, source FROM research_events
                WHERE status = 'active'""",
        ):
            events_pairs.add((r["event_type"], r["source"]))
        # Capture batch_ids whose pair has no catalog row pre-reconcile so
        # the diagnostic is honest about what was orphaned BEFORE we fixed it.
        orphan_pairs_pre = events_pairs - catalog_pairs
        if orphan_pairs_pre:
            placeholders = ",".join(["(?, ?)"] * len(orphan_pairs_pre))
            params: list = []
            for et, src in orphan_pairs_pre:
                params.extend([et, src])
            for r in conn_e.execute(
                f"""SELECT DISTINCT batch_id, event_type, source
                      FROM research_events
                     WHERE status = 'active'
                       AND (event_type, source) IN (VALUES {placeholders})""",
                params,
            ):
                key = (r["event_type"], r["source"])
                events_pairs_with_batches.setdefault(key, []).append(int(r["batch_id"]))

    # 2. Refresh every pair in the union. The function is idempotent so we
    #    can call it on the same pair twice cheaply.
    all_pairs = sorted(catalog_pairs | events_pairs)
    result.series_walked = len(all_pairs)
    for event_type, source in all_pairs:
        try:
            r = refresh_event_catalog_for(
                event_type=event_type,
                source=source,
                now_epoch=now,
                events_db_path=epath,
                research_db_path=rpath,
            )
        except Exception:
            log.warning(
                "reconcile_event_catalog: refresh for (%s, %s) failed; "
                "leaving prior catalog row in place",
                event_type, source, exc_info=True,
            )
            continue
        result.refreshes.append(r)
        if r.changed:
            result.series_updated += 1

    # 3. Surface orphans for the response body.
    #    orphan_catalog_rows: catalog row exists but events.db has zero
    #    active rows for the pair (the refresh wrote row_count=0).
    for r in result.refreshes:
        if r.row_count == 0:
            result.orphan_catalog_rows.append((r.event_type, r.source))
    #    orphan_batches: batch_ids whose pair had no catalog row pre-reconcile
    #    (those rows were just CREATED in the refresh loop above; the
    #    diagnostic is honest about what reconcile created).
    for pair, batch_ids in events_pairs_with_batches.items():
        result.orphan_batches.extend(batch_ids)
    # Stable order for the response
    result.orphan_catalog_rows.sort()
    result.orphan_batches.sort()

    return result
