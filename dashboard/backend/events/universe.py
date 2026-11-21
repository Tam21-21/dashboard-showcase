"""M3.5 brief A: the reusable universe layer (loader + point-in-time read).

Spec brief §3.1 / §3.3 / §4 / §18 brief A row. A "universe" is the set of
companies a backtest may trade. It is stored as two coordinated rows:

  - one research.db `research_datasets` catalog row with `dataset_type='universe'`
    (the metadata that NAMES the universe; its `id` is the `universe_id`
    everything else references), and
  - one events.db `research_universe_membership` row per member, with an
    `entered_at`/`left_at` validity interval (the slowly-changing-dimension
    join table, eventsdb.py `_DDL_UNIVERSE_MEMBERSHIP`).

This module owns three things every other brief-A surface calls:

  - `load_universe(...)`        — write the catalog row + membership rows
                                  (the CLI `load-universe` subcommand and the
                                  probe both call this).
  - `resolve_universe_ciks(...)`— the UNION-over-window read (the
                                  `POST /universes/resolve` endpoint calls this).
  - `universe_exists(...)`      — the research.db existence check that backs the
                                  endpoint's 404.

Registration note (EL75, the "code-side type registry"). M2.5 did not build a
literal type-registry data structure: `dataset_type` is a free TEXT column on
`research_datasets` with no whitelist, and `'event_series'` is "registered" only
by its loader (events/catalog.py) plus typed sentinel constants. Brief A
registers `'universe'` the same way — the `DATASET_TYPE_UNIVERSE` constant below
plus this loader — rather than inventing a structure existing M2.5 code would not
consult. No `research_datasets` table migration (the column exists since M2.5).

Cross-database honesty. The catalog row lives in research.db; the membership
rows live in events.db. Both run in WAL mode, where a single transaction
spanning two attached database files is not atomic across the files, so this
module does NOT pretend to one atomic write. Instead it is ordered and
idempotent: the catalog row is found-or-created first (so its `id` is stable),
then the membership set is replaced wholesale (DELETE then INSERT). A partial
failure is recovered by simply re-running the loader — find-or-create reuses the
same `id` and the membership replace is wholesale. This mirrors M2.5's
best-effort cross-DB catalog refresh (events/catalog.py).
"""
from __future__ import annotations

import logging                                                  # structured warning when the bridge can't resolve a ticker (B2)
import sqlite3                                                   # the only DB driver; both DBs are SQLite
import time                                                      # epoch wall-clock for loaded_at / last_fetched_at
from dataclasses import dataclass, field                         # the structured loader result
from pathlib import Path                                         # path typing for the override params
from typing import Optional, Union                               # optional overrides, like catalog.py

from .. import eventsdb                                          # events.db: membership table + the bridge + path default
from .. import researchdb                                        # research.db: the catalog table + path default
from .tickers import normalize_ticker                            # canonical bridge-key lookup (item 1 / B1)

log = logging.getLogger(__name__)                               # module logger (the bridge's report-not-drop warning)

# The code-side dataset-type name for a universe catalog row (EL75). Re-pointed at
# the single registry owner (pre-M4 Brief A1 / H4): researchdb.DATASET_TYPE_REGISTRY
# is now THE authority for which dataset types exist, so this module references that
# one constant instead of carrying its own literal. The module docstring's
# "registration note" predates the registry; registration is now the registry, gated
# at every writer via dataset_storage_kind (below, before the INSERT).
DATASET_TYPE_UNIVERSE = researchdb.DATASET_TYPE_UNIVERSE
# B2 (AD-UU-2, the read-time event bridge): the ticker-keyed price universe. An event run
# may target a price_universe; resolve_universe_ciks bridges its bare tickers -> CIKs on read.
DATASET_TYPE_PRICE_UNIVERSE = researchdb.DATASET_TYPE_PRICE_UNIVERSE

# Sentinel for the catalog row's price-only NOT NULL `timeframe` column (§3.3),
# the universe analogue of catalog.py's `_EVENT_SERIES_TIMEFRAME`. A universe row
# has no real timeframe; this keeps the NOT NULL column populated honestly.
_UNIVERSE_TIMEFRAME = "universe"


@dataclass
class LoadUniverseResult:
    """Outcome of one `load_universe` call (what the CLI prints, what the probe asserts)."""

    universe_id: int                                             # the research_datasets.id (found or created)
    created: bool                                                # True if the catalog row was newly inserted this call
    loaded: int                                                  # distinct CIKs written to research_universe_membership
    constituents_in: int                                         # length of the input constituent list (pre-dedup)
    # Constituents that could NOT be loaded — reported and counted, never silently
    # dropped (the EL66 / R14 honesty stance). Two distinct reasons, kept distinct:
    unresolvable: list[str] = field(default_factory=list)        # ticker with zero bridge rows (no CIK known)
    ambiguous: list[str] = field(default_factory=list)           # ticker resolving to >1 distinct CIK (dual-class / reused)

    @property
    def excluded(self) -> int:
        """Total constituents excluded (counted-not-dropped) across both reasons."""
        return len(self.unresolvable) + len(self.ambiguous)      # the honest exclusion total


class UniverseEmptyError(Exception):
    """No constituent resolved to a CIK, so the universe would have zero members.

    Raised by `load_universe` instead of persisting an unusable zero-member universe
    (Cowork S1 / D2). A zero-member universe cannot back a run (it would resolve to an
    empty CIK list), so aborting is honest — it is NOT a silent drop of resolvable
    members (a partial resolve still succeeds and reports the excluded tickers). Carries
    the counts so the route can return a 400 the operator can act on.
    """

    def __init__(self, *, universe_id: int, created: bool, constituents_in: int,
                 unresolvable: list[str], ambiguous: list[str]) -> None:
        self.universe_id = universe_id                           # the (possibly just-deleted) catalog id
        self.created = created                                   # True if this call created + then compensated the row
        self.constituents_in = constituents_in                  # how many tickers were given
        self.unresolvable = unresolvable                         # tickers with no bridge row (reported, not dropped)
        self.ambiguous = ambiguous                               # tickers resolving to >1 CIK
        super().__init__(
            f"universe id={universe_id} resolved 0 of {constituents_in} constituents "
            f"({len(unresolvable)} unresolvable, {len(ambiguous)} ambiguous); "
            f"refusing to persist a zero-member universe"
        )


def _connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open an autocommit SQLite connection with the project's per-connection pragmas.

    Mirrors researchdb._connect / eventsdb._connect_events so an override path
    behaves exactly like the default path. We own transactions explicitly via
    BEGIN IMMEDIATE / COMMIT / ROLLBACK (isolation_level=None = autocommit).
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)   # autocommit; we drive BEGIN/COMMIT ourselves
    conn.execute("PRAGMA foreign_keys = ON;")                    # per-connection; FK enforcement off by default
    conn.execute("PRAGMA journal_mode = WAL;")                   # persistent at the file level; cheap to re-set
    conn.row_factory = sqlite3.Row                               # dict-like rows so we read row["id"] not row[0]
    return conn


def find_or_create_universe(
    *,
    universe_name: str,
    source_label: str,
    research_db_path: Union[str, Path, None] = None,
    now_epoch: Optional[int] = None,
) -> tuple[int, bool]:
    """Find (or create) the `dataset_type='universe'` catalog row; return (id, created).

    Idempotency is SELECT-first by `(name, dataset_type='universe')` (§3.3): a
    re-load of the same universe name reuses the existing `id` rather than
    inserting a duplicate, so the membership it then replaces stays attached to
    one stable `universe_id`. The base `research_datasets` UNIQUE constraint is
    `(source, symbol, timeframe, start_ts, end_ts)`; keying `symbol` to the
    universe name (not a fixed literal) makes two universes under the same
    `source_label` distinct rows without a new index (§3.3).
    """
    if now_epoch is None:                                        # default to now; tests pin it for determinism
        now_epoch = int(time.time())
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH  # override or module default
    conn = _connect(rpath)                                       # research.db holds the catalog
    try:
        conn.execute("BEGIN IMMEDIATE;")                         # one write transaction (find-or-create is a write)
        try:
            # SELECT-first by (name, dataset_type) per §3.3 — the find half.
            row = conn.execute(
                "SELECT id FROM research_datasets WHERE name = ? AND dataset_type = ?",
                (universe_name, DATASET_TYPE_UNIVERSE),
            ).fetchone()
            if row is not None:                                  # found: reuse the id, refresh last_fetched_at honestly
                universe_id = int(row["id"])                     # the stable universe_id
                conn.execute(
                    "UPDATE research_datasets SET last_fetched_at = ? WHERE id = ?",
                    (now_epoch, universe_id),
                )
                created = False                                  # reused, not created
            else:                                               # not found: create the row with the §3.3 sentinels
                # H4 (pre-M4 A1): gate the type through the registry before the INSERT, so an
                # unregistered dataset_type fails here at the write site (UnknownDatasetTypeError),
                # never as a silent typo'd row. DATASET_TYPE_UNIVERSE is the registry-owned constant.
                researchdb.dataset_storage_kind(DATASET_TYPE_UNIVERSE)
                cur = conn.execute(
                    """INSERT INTO research_datasets
                          (name, dataset_type, event_type, source,
                           symbol, timeframe, status,
                           start_ts, end_ts, rows,
                           sha256, file_path, file_bytes,
                           created_at, last_fetched_at)
                       VALUES (?, ?, NULL, ?,
                               ?, ?, 'ready',
                               0, 0, NULL,
                               NULL, NULL, NULL,
                               ?, ?)""",
                    (universe_name, DATASET_TYPE_UNIVERSE, source_label,
                     universe_name, _UNIVERSE_TIMEFRAME,
                     now_epoch, now_epoch),
                )
                universe_id = int(cur.lastrowid)                 # the new universe_id
                created = True                                   # newly inserted this call
            conn.execute("COMMIT;")                              # commit the find-or-create
        except (sqlite3.Error, researchdb.UnknownDatasetTypeError):
            # The only failures inside this block: a sqlite3 driver error from the
            # SELECT/UPDATE/INSERT/COMMIT, or UnknownDatasetTypeError from the registry
            # gate (dataset_storage_kind). Roll back the open transaction, then re-raise
            # so the caller sees the real error and can re-run (find-or-create is idempotent).
            conn.execute("ROLLBACK;")                            # undo on failure; the caller re-runs
            raise
        return universe_id, created
    finally:
        conn.close()                                            # per-call lifecycle, no pooling


def resolve_tickers_to_ciks(
    tickers: list[str],
    conn: sqlite3.Connection,
) -> tuple[list[int], list[str], list[str]]:
    """Resolve bare tickers to CIKs via `research_cik_ticker_map` on the given events.db connection.

    The single owner of the bridge-key resolution rule (B2 minor): normalize each ticker
    (`normalize_ticker`, e.g. `BRK.B`->`BRK-B`) then apply the exactly-one-CIK rule. Reused by
    `load_universe` (the write path) AND `resolve_universe_ciks`'s price-universe branch (the read
    path), so the two cannot drift on what "resolvable" means (DDIA: one canonical form, one owner).

    `conn` is an events.db connection (where `research_cik_ticker_map` lives); the caller owns its
    lifecycle (a write transaction for load_universe, a read connection for the bridge).

    Returns (resolved_ciks, unresolvable, ambiguous):
      - resolved_ciks: ordered (input order), de-duplicated list[int] — tickers resolving to exactly one CIK
      - unresolvable:  list[str] — tickers with zero bridge rows (reported in their ORIGINAL form, never dropped)
      - ambiguous:     list[str] — tickers resolving to >1 distinct CIK (dual-class / reused, OPEN-2)

    The DISTINCT-with-no-effective_from lookup is intentional (it detects >1 CIK as ambiguous, the
    EL66/R14 count-not-drop branch); do NOT harmonize it with the Benzinga/canonical effective_from reader.
    """
    resolved_ciks: list[int] = []                               # ordered, de-duplicated CIKs (input order)
    seen_ciks: set[int] = set()                                 # dedup guard (two tickers can map to one CIK)
    unresolvable: list[str] = []                                # tickers with zero bridge rows
    ambiguous: list[str] = []                                   # tickers resolving to >1 distinct CIK
    for ticker in tickers:                                       # preserve input order for a stable listing
        ciks = [
            int(r["cik"])                                       # named access; matches the codebase row convention
            for r in conn.execute(
                "SELECT DISTINCT cik FROM research_cik_ticker_map WHERE ticker = ?",
                (normalize_ticker(ticker),),                    # canonical key: a dotted/lowercase paste must normalize or it misses
            ).fetchall()
        ]
        if len(ciks) == 1:                                      # exactly one CIK: resolvable
            cik = ciks[0]
            if cik not in seen_ciks:                            # dedup: one membership row per CIK
                seen_ciks.add(cik)
                resolved_ciks.append(cik)
        elif not ciks:                                          # zero bridge rows: unknown ticker (report the ORIGINAL form)
            unresolvable.append(ticker)
        else:                                                  # >1 distinct CIK: ambiguous (dual-class / reused)
            ambiguous.append(ticker)
    return resolved_ciks, unresolvable, ambiguous


def load_universe(
    *,
    universe_name: str,
    constituent_list: list[str],
    source_label: str,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
    now_epoch: Optional[int] = None,
) -> LoadUniverseResult:
    """Load a today's-list universe: catalog row + one membership row per resolved CIK.

    Reusable (any universe, any source) per §4. Each constituent ticker resolves
    to a `subject_cik` via the existing `research_cik_ticker_map` bridge; a
    ticker with zero bridge rows (unknown) or more than one distinct CIK
    (dual-class / reused, OPEN-2) is reported, counted, and excluded — never
    silently dropped (EL66 / R14). Membership rows are back-dated to inception
    (`entered_at=0`, `left_at=NULL`) so the union-over-window read returns the
    full current list for any historical window (DECISIONS #54; the today's-list
    limitation, §4). Idempotent: re-running for the same `universe_name` reuses
    the catalog id and REPLACES the membership set (DELETE then INSERT), which
    preserves the §3.1 non-overlap invariant.
    """
    if now_epoch is None:                                        # default to now; tests pin it for determinism
        now_epoch = int(time.time())

    # 1. Catalog row first, so its id is the stable universe_id (research.db).
    universe_id, created = find_or_create_universe(
        universe_name=universe_name,
        source_label=source_label,
        research_db_path=research_db_path,
        now_epoch=now_epoch,
    )

    # 2. Resolve every constituent via the bridge, then replace the membership
    #    set. The resolution READ and the membership WRITE share one events.db
    #    transaction, but the DELETE-then-INSERT runs ONLY when at least one
    #    ticker resolved (Cowork S1): re-loading an EXISTING universe with a
    #    zero-resolving list must NOT wipe its current members. Zero-resolve is
    #    handled after the transaction as an honest abort, never a silent empty save.
    with eventsdb.events_transaction(events_db_path) as conn:    # BEGIN IMMEDIATE on events.db
        # The bridge resolution lives in one shared owner (resolve_tickers_to_ciks) so the write
        # path here and the read-time price-universe bridge cannot drift (B2 minor). It reads the
        # cik bridge on THIS events.db transaction connection (the read + the write below are one txn).
        resolved_ciks, unresolvable, ambiguous = resolve_tickers_to_ciks(constituent_list, conn)

        # Idempotent replace, but ONLY when something resolved (Cowork S1). On a
        # zero-resolve we touch NO membership rows here — an existing universe keeps
        # its current members — and abort after the transaction below.
        if resolved_ciks:
            conn.execute(
                "DELETE FROM research_universe_membership WHERE universe_id = ?",
                (universe_id,),
            )
            for cik in resolved_ciks:                           # one row per resolved, de-duplicated CIK
                conn.execute(
                    """INSERT INTO research_universe_membership
                          (universe_id, subject_cik, entered_at, left_at, source, loaded_at)
                       VALUES (?, ?, 0, NULL, ?, ?)""",
                    (universe_id, cik, source_label, now_epoch),  # entered_at=0 (inception), left_at=NULL (open-ended)
                )

    # Zero-resolve abort (Cowork S1 / D2). A universe with no members is unusable, so
    # do NOT leave a zombie. If THIS call created the catalog row, compensate-delete it
    # on research.db (its own transaction — the catalog row was committed by
    # find_or_create_universe before the events.db transaction opened). Then raise so
    # the route returns 400 with the counts. A re-load of an existing universe that
    # zero-resolves keeps its prior members (the DELETE above was skipped) and also
    # raises, telling the operator the new list resolved nothing.
    if not resolved_ciks:
        if created:
            _delete_universe_catalog_row(universe_id, research_db_path)
        raise UniverseEmptyError(
            universe_id=universe_id,
            created=created,
            constituents_in=len(constituent_list),
            unresolvable=unresolvable,
            ambiguous=ambiguous,
        )

    return LoadUniverseResult(
        universe_id=universe_id,
        created=created,
        loaded=len(resolved_ciks),
        constituents_in=len(constituent_list),
        unresolvable=unresolvable,
        ambiguous=ambiguous,
    )


def _delete_universe_catalog_row(universe_id: int,
                                 research_db_path: Union[str, Path, None]) -> None:
    """Compensate-delete a just-created universe catalog row on research.db.

    Used only on a zero-resolve abort when load_universe created the catalog row this
    call (Cowork S1): find_or_create_universe commits the catalog row on research.db
    before the events.db membership step, so on abort it must be removed here in its own
    research.db transaction or it persists as a zombie zero-member universe. Guarded to
    the universe id AND `dataset_type='universe'` so it can never touch a price-dataset
    row; a brand-new universe has no membership rows and no run referencing it, so the
    delete is safe.
    """
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH  # override or default
    conn = _connect(rpath)                                       # research.db holds the catalog row
    try:
        conn.execute("BEGIN IMMEDIATE;")                         # one small write transaction
        try:
            conn.execute(
                "DELETE FROM research_datasets WHERE id = ? AND dataset_type = ?",
                (universe_id, DATASET_TYPE_UNIVERSE),            # type-guarded: never a price dataset
            )
            conn.execute("COMMIT;")
        except sqlite3.Error:
            # Only a sqlite3 driver error can arise from the BEGIN/DELETE/COMMIT here;
            # roll back and re-raise so a half-applied delete never persists silently.
            conn.execute("ROLLBACK;")                            # leave the row rather than half-delete on error
            raise
    finally:
        conn.close()                                            # per-call lifecycle, no pooling


def member_counts(events_db_path: Union[str, Path, None] = None) -> dict[int, int]:
    """Return {universe_id: current member count} for every universe, in ONE aggregate query.

    member_count = COUNT(DISTINCT subject_cik) of CURRENT members (left_at IS NULL) — the
    same set the resolve read returns today (every membership row is inserted open-ended).
    The Data Library listing (GET /datasets) calls this ONCE and merges the counts into its
    research.db rows in Python (a universe absent from the result has zero members), so the
    listing stays a fixed number of queries regardless of how many universes exist — never a
    per-row COUNT (no N+1). research_universe_membership lives in events.db, so this read
    belongs here, not folded into the research.db listing query (the two databases cannot be
    joined on one connection without an ATTACH).
    """
    with eventsdb.get_events_conn(events_db_path) as conn:       # read connection on events.db (Row factory)
        rows = conn.execute(
            """SELECT universe_id, COUNT(DISTINCT subject_cik) AS member_count
                 FROM research_universe_membership
                WHERE left_at IS NULL
             GROUP BY universe_id"""
        ).fetchall()
    return {int(r["universe_id"]): int(r["member_count"]) for r in rows}  # absent universe -> caller defaults 0


def universe_exists(
    universe_id: int,
    *,
    research_db_path: Union[str, Path, None] = None,
) -> bool:
    """True iff a `research_datasets` row with this id AND a universe `dataset_type` exists.

    Backs the `POST /universes/resolve` 404 (`universe_not_found`). B2 (AD-UU-2): the type
    filter accepts BOTH `'universe'` (the CIK-keyed universe) and `'price_universe'` (the
    ticker-keyed price universe an event run may now target via the read-time bridge), so an
    event run over a price universe is not 404'd before `resolve_universe_ciks` can bridge it.
    A price-DATASET id (a single ticker, not a universe) is still treated as "not a universe".
    """
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH  # override or default
    conn = _connect(rpath)
    try:
        row = conn.execute(
            "SELECT 1 FROM research_datasets WHERE id = ? AND dataset_type IN (?, ?)",
            (universe_id, DATASET_TYPE_UNIVERSE, DATASET_TYPE_PRICE_UNIVERSE),
        ).fetchone()
        return row is not None                                   # existence, not the row contents
    finally:
        conn.close()


def resolve_universe_ciks(
    *,
    universe_id: int,
    knowledge_from: int,
    knowledge_to: int,
    events_db_path: Union[str, Path, None] = None,
    research_db_path: Union[str, Path, None] = None,
    members_as_of: Optional[int] = None,
) -> list[int]:
    """Resolve a universe id + window to its CIK list, branching on the universe's physical type (AD-UU-2).

    Two physically distinct universe kinds, one read seam (the read-time event bridge):

    - `'universe'` (the legacy CIK-keyed universe): the UNION-over-window membership read on
      events.db (§3.1 step 1). Interval-overlap predicate: an episode `[entered_at, left_at)`
      overlaps `[knowledge_from, knowledge_to)` iff it started before window-end
      (`entered_at < knowledge_to`) and ended after window-start (`left_at IS NULL OR left_at >
      knowledge_from`). Survivorship-safe (DECISIONS #54). UNCHANGED by B2.

    - `'price_universe'` (the ticker-keyed price universe, B2): read the bare tickers from
      research.db `research_price_universe_membership`, then bridge them to CIKs via the shared
      `resolve_tickers_to_ciks` over an events.db read connection. The window is IGNORED — the
      price membership is a flat current list with no `entered_at`/`left_at` validity interval
      (F5: the price-window scopes price sweeps only, not the event run's knowledge window). The
      bridge holds NO transaction across the two databases and writes nothing (a pure read;
      DELETE/INSERT into `research_universe_membership` is the load_universe write path only).
      Unresolvable/ambiguous tickers are reported (logged with counts), never silently dropped.

    DL1 (DECISIONS #455): for a price universe, `members_as_of` (unix seconds, pinned by the run at dispatch)
    makes the read use the list as it stood then, from the dated spans; without it the read is today's list,
    as before. A legacy 'universe' ignores it.

    Returns a sorted, de-duplicated CIK list either way (the materializer's deterministic `ciks`
    contract). A universe with no resolvable member returns `[]` (a valid empty universe, not an
    error — the §8 wiring short-circuits an empty union before the materializer).
    """
    # Branch on the universe's physical type, read from research.db once (a price_universe is
    # ticker-keyed there; a legacy universe's catalog row is also there but its membership is in
    # events.db). The price branch also reads its bare tickers from this same research.db connection.
    rpath = research_db_path if research_db_path is not None else researchdb.DB_PATH  # override or default
    rconn = _connect(rpath)                                      # research.db: the catalog + price membership
    try:
        trow = rconn.execute(
            "SELECT dataset_type FROM research_datasets WHERE id = ?",
            (universe_id,),
        ).fetchone()
        dataset_type = trow["dataset_type"] if trow is not None else None  # None = no such row (caller's 404 already guards)
        price_tickers: Optional[list[str]] = None               # the bare-ticker list, only for a price_universe
        if dataset_type == DATASET_TYPE_PRICE_UNIVERSE:
            if members_as_of is None:
                price_tickers = [
                    r["ticker"]                                  # the bare ticker (AD-M5-14), bridged to a CIK below
                    for r in rconn.execute(
                        "SELECT ticker FROM research_price_universe_membership WHERE universe_id = ? ORDER BY ticker",
                        (universe_id,),
                    ).fetchall()
                ]
            else:                                                # DL1: the list as it stood at the run's own date
                price_tickers = researchdb.price_universe_members_as_of_conn(rconn, universe_id, members_as_of)
    finally:
        rconn.close()                                           # per-call lifecycle, no pooling (the cross-DB read is scoped)

    epath = events_db_path if events_db_path is not None else eventsdb.DB_PATH  # override or default

    if dataset_type == DATASET_TYPE_PRICE_UNIVERSE:
        # The read-time bridge: ticker -> CIK over events.db, the window deliberately ignored (F5).
        with eventsdb.get_events_conn(epath) as conn:           # read-only per-call connection (the cik bridge lives here)
            resolved, unresolvable, ambiguous = resolve_tickers_to_ciks(price_tickers or [], conn)
        if unresolvable or ambiguous:
            # Report-not-drop (EL66 / R14): the run proceeds over the resolvable members, but the
            # exclusion is recorded, never silent. Counts are exact; the ticker lists are capped to a
            # sample (a pasted index can leave hundreds unresolvable in an early bridge-map state, and a
            # multi-KB log event risks backend truncation) — the count carries the full magnitude.
            _LOG_SAMPLE = 20                                     # how many offending tickers to name in the log
            log.warning(
                "price-universe event bridge could not resolve every ticker to a CIK",
                extra={
                    "universe_id": universe_id,
                    "resolved_count": len(resolved),
                    "unresolvable_count": len(unresolvable),
                    "ambiguous_count": len(ambiguous),
                    "unresolvable_sample": unresolvable[:_LOG_SAMPLE],
                    "ambiguous_sample": ambiguous[:_LOG_SAMPLE],
                },
            )
        return sorted(resolved)                                  # deterministic, de-duplicated (the materializer contract)

    # Legacy 'universe' path — the survivorship-safe union-over-window read on events.db (unchanged).
    with eventsdb.get_events_conn(epath) as conn:               # read-only per-call connection
        rows = conn.execute(
            """SELECT DISTINCT subject_cik
                 FROM research_universe_membership
                WHERE universe_id = ?
                  AND entered_at < ?
                  AND (left_at IS NULL OR left_at > ?)
                ORDER BY subject_cik""",
            (universe_id, knowledge_to, knowledge_from),         # note the cross-binding: < to, > from (overlap)
        ).fetchall()
    return [int(r["subject_cik"]) for r in rows]                 # the union CIK list
