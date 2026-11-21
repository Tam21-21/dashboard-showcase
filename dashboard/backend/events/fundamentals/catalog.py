"""The fundamentals catalog handle: one row-backed research_datasets row (M3.75 FUND-1 §4).

Mirrors events/catalog.py's event-series functions. The facts live in events.db (§1); this is
the discoverable / listable / restorable handle in research.db. There is exactly ONE
fundamentals catalog row (one source, sec_edgar), kept unique by the partial index
``uq_research_datasets_fundamentals`` (``(source) WHERE dataset_type='fundamentals'``,
researchdb.init_db) and maintained by an idempotent UPSERT.

Two public functions, the SAME best-effort-refresh + reconcile-backstop pair the event series
catalog uses (§4.3):

  refresh_fundamentals_catalog(...)
      UPSERT the one fundamentals row, recomputing rows / start_ts / end_ts (= fact count and
      MIN/MAX filing_date) from events.db via ATTACH. Best-effort: a failed refresh after a
      batch wrote facts must not fail the batch (write.py logs-not-fatal); the reconcile pass
      repairs any drift.

  reconcile_fundamentals_catalog(...)
      Re-derive the one fundamentals row from the live facts at dashboard startup, closing the
      best-effort refresh's silent-drift hazard (the event-series reconcile_event_catalog
      analogue).

Why this lives here and not in researchdb.py: like events/catalog.py, it ATTACHes events.db
onto a research.db connection — it operates at the boundary between both DBs, which is the
events/ package's job, not researchdb.py's (which owns the research.db schema alone).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from ... import eventsdb
from ... import researchdb
# Reuse the event-series module's ATTACH-path validator verbatim (same risky-char refusal).
from ..catalog import _safe_attach_path
from .concepts import FUNDAMENTAL_CONCEPTS

log = logging.getLogger(__name__)


# Sentinel placeholders for the four NOT NULL research_datasets columns (symbol, timeframe,
# start_ts, end_ts) that have no honest meaning for a fundamentals store, mirroring the
# event-series sentinels (catalog._EVENT_SERIES_SYMBOL / _EVENT_SERIES_TIMEFRAME). The
# `(source, symbol, timeframe, start_ts, end_ts)` table UNIQUE constraint needs them populated.
_FUNDAMENTALS_SYMBOL = "__fundamentals__"        # §4.3 sentinel symbol
_FUNDAMENTALS_TIMEFRAME = "fundamentals"         # §4.3 sentinel timeframe

# The fixed human label for the one fundamentals dataset row (§4: a plain UI name, never an id).
_FUNDAMENTALS_NAME = "SEC XBRL fundamentals"
# The one source the fundamentals store has (the F1 batch table's source CHECK enum).
_FUNDAMENTALS_SOURCE = "sec_edgar"


@dataclass(frozen=True)
class FundamentalsCatalogResult:
    """The outcome of one refresh / reconcile of the fundamentals catalog row."""
    source: str            # the fundamentals source (sec_edgar)
    row_count: int         # the fact count behind the catalog row
    start_at: int          # MIN(filing_date) over the facts, 0 when the store is empty
    end_at: int            # MAX(filing_date) over the facts, 0 when the store is empty
    inserted: bool         # True on INSERT (no prior row), False on UPDATE
    changed: bool          # True iff (row_count, start_at, end_at) actually moved


def refresh_fundamentals_catalog(
    *,
    source: str = _FUNDAMENTALS_SOURCE,
    now_epoch: Optional[int] = None,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
) -> FundamentalsCatalogResult:
    """Idempotent UPSERT of the one fundamentals row in research.db.research_datasets (§4.3).

    ATTACHes events.db onto a research.db connection, recomputes rows = COUNT(*) and
    start_ts / end_ts = MIN/MAX(filing_date) over research_fundamental_facts (COALESCEd to 0
    for an empty store, the event-series convention), and UPSERTs the one row keyed on the
    partial index ``(source) WHERE dataset_type='fundamentals'``. file_path / sha256 /
    file_bytes are NULL (row-backed); event_type is NULL (a fundamentals source has no event
    type, §4.3).

    Best-effort from the caller's view: write.py catches any exception and treats a failed
    refresh as logged-not-fatal; reconcile_fundamentals_catalog at startup is the backstop.
    """
    if now_epoch is None:                                            # default the UPSERT clock to now
        now_epoch = int(time.time())

    # Resolve both paths so the ATTACH literal is stable across the call (the catalog.py shape).
    epath = events_db_path if events_db_path is not None else eventsdb.DB_PATH
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH
    attach_literal = _safe_attach_path(epath)                        # single-quoted, risky-char-refused

    # Open research.db with the per-connection pragmas matching researchdb.py / catalog.py.
    conn = sqlite3.connect(str(rpath), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.row_factory = sqlite3.Row
        # ATTACH outside the transaction (SQLite forbids ATTACH inside a transaction).
        conn.execute(f"ATTACH DATABASE {attach_literal} AS events")
        try:
            # Capture the pre-state so the `changed` flag is honest (UPSERT cannot tell us
            # inserted-vs-updated portably; a cheap SELECT first, the catalog.py pattern).
            prior = conn.execute(
                """SELECT rows AS row_count, start_ts AS start_at, end_ts AS end_at
                     FROM research_datasets
                    WHERE dataset_type='fundamentals'
                      AND source = ?""",
                (source,),
            ).fetchone()

            conn.execute("BEGIN IMMEDIATE;")                         # the recompute + UPSERT are one transaction
            try:
                # Count what research can read: the annual figure for a flow concept, every row for an
                # instant (the reads.py _scope_clause rule; a quarterly flow row, lane FA1a, is stored
                # but not counted here). The status route keeps counting the whole store.
                flow_concepts = [s.name for s in FUNDAMENTAL_CONCEPTS if s.period_semantics == "flow"]
                agg = conn.execute(
                    """SELECT MIN(filing_date) AS start_at,
                              MAX(filing_date) AS end_at,
                              COUNT(*)         AS row_count
                         FROM events.research_fundamental_facts
                        WHERE fiscal_period_code = 'FY'
                           OR concept NOT IN (SELECT value FROM json_each(?))""",
                    (json.dumps(flow_concepts),),
                ).fetchone()
                row_count = int(agg["row_count"] or 0)              # 0 on an empty store
                # COALESCE(..., 0): both start_ts / end_ts are NOT NULL on research_datasets.
                start_at = int(agg["start_at"]) if agg["start_at"] is not None else 0
                end_at = int(agg["end_at"]) if agg["end_at"] is not None else 0

                # Gate the type through the registry before the write (the catalog.py H4 gate):
                # a typo'd dataset_type fails loud here, not as a silent unregistered row.
                researchdb.dataset_storage_kind(researchdb.DATASET_TYPE_FUNDAMENTALS)
                # UPSERT keyed on the partial unique index uq_research_datasets_fundamentals.
                # event_type is NULL (§4.3); the row is row-backed so file_* are NULL.
                conn.execute(
                    """
                    INSERT INTO research_datasets
                           (name, dataset_type, event_type, source,
                            symbol, timeframe, status,
                            start_ts, end_ts, rows,
                            file_path, sha256, file_bytes,
                            created_at, last_fetched_at)
                    VALUES (?, ?, NULL, ?,
                            ?, ?, 'ready',
                            ?, ?, ?,
                            NULL, NULL, NULL,
                            ?, ?)
                    ON CONFLICT (source) WHERE dataset_type = 'fundamentals'
                    DO UPDATE SET
                        name            = excluded.name,
                        start_ts        = excluded.start_ts,
                        end_ts          = excluded.end_ts,
                        rows            = excluded.rows,
                        last_fetched_at = excluded.last_fetched_at
                    """,
                    (_FUNDAMENTALS_NAME, researchdb.DATASET_TYPE_FUNDAMENTALS, source,
                     _FUNDAMENTALS_SYMBOL, _FUNDAMENTALS_TIMEFRAME,
                     start_at, end_at, row_count,
                     now_epoch, now_epoch),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            inserted = prior is None                                # no prior row -> this was an INSERT
            if inserted:
                changed = True                                      # a brand-new row is always a change
            else:
                changed = (                                         # did the derived values actually move?
                    int(prior["row_count"]) != row_count
                    or int(prior["start_at"]) != start_at
                    or int(prior["end_at"]) != end_at
                )
            return FundamentalsCatalogResult(
                source=source, row_count=row_count,
                start_at=start_at, end_at=end_at,
                inserted=inserted, changed=changed,
            )
        finally:
            # DETACH outside the BEGIN/COMMIT; best-effort so it can't mask the real return.
            try:
                conn.execute("DETACH DATABASE events")
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()


def reconcile_fundamentals_catalog(
    *,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
) -> FundamentalsCatalogResult:
    """Re-derive the one fundamentals catalog row from the live facts at startup (§4.3).

    The load-bearing backstop to the best-effort refresh: a refresh that died after a batch's
    facts landed could leave the catalog row's rows / start_ts / end_ts permanently drifted
    from the events.db truth. This runs at dashboard boot (mirroring reconcile_event_catalog)
    and re-runs the refresh, which recomputes from the facts — so the catalog converges on the
    store. Idempotent: a reconcile that finds no drift returns changed=False.

    There is exactly one fundamentals row (one source), so unlike reconcile_event_catalog there
    is no pair-walk to do — reconcile is a single refresh of that one row.
    """
    return refresh_fundamentals_catalog(
        events_db_path=events_db_path,
        research_db_path=research_db_path,
    )
