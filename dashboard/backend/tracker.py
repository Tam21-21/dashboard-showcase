"""The data-use tracker (lane T, night two, docs/plans/night/2026-09-08/PLAN.md section 6;
generator_requirements_2026-09-05.md section 6 R-T1 to R-T16; generator_architecture_2026-09-05.md
AD6 to AD8, AD11.2, AD11.3).

Every draw of price bars by a stage, for a candidate, must be recorded BEFORE the bars are
read, and a draw whose window is not strictly later than that candidate's own high-water
mark must be refused rather than silently allowed (R-T1, R-T2). This module is the gate that
makes that true. Standard library plus dashboard.backend.researchdb.transaction()/get_conn()
only: no vectorbtpro, no research_lab import, matching generated.py's own import discipline
(the module that already sits beside this one, gating dispatch on a different question).

Three transactions per AD7, never one, because dispatch_sweep (and dispatch_run) cannot be
called from inside another connection's already-open transaction (AD7's own G2-B2: each of
those functions opens its own `with transaction() as conn:` block, and a second BEGIN
IMMEDIATE against the same file fails at once with no busy_timeout configured anywhere in
dashboard/backend/). So: transaction 1 (request_draw) is the chronology gate itself and
commits BEFORE any dispatch is attempted (R-T3); the dispatch that follows runs entirely
outside any transaction this module opens; transaction 3 (link_run) is a SEPARATE, later,
APPENDED event, because the run it links to does not exist until that dispatch has returned
(R-T14). A fourth, independent transaction (record_bars_observed) appends the bars-consumed
observation once the run has actually reported, and a fifth kind (record_names_drawn) is the
held-out name draw, which belongs to no candidate at all (AD8, AD9).
"""
from __future__ import annotations

import contextvars  # the reserve exam's own call-scoped seed channel (see reserve_exam_seed below)
import sqlite3
from contextlib import contextmanager

from dashboard.backend import researchdb, reservedb

# The one operational dial this unit introduces (R-T12's own words: a signal with no stated
# value is a guess a builder has to make). granted_draws_without_run_link (B14) only means
# something once a genuinely stuck draw has had time to resolve: a normal dispatch finishes
# in seconds to a few minutes, and the worker polls every two seconds (dashboard/backend/
# CLAUDE.md, "How work flows"), so an hour of silence is well past any legitimate in-flight
# window while still surfacing a same-day orphan. This is an operational dial, not a number a
# backtest can see (#204): it changes what the monitor reports, never what a strategy computes,
# so it carries this inline justification and no entry in parameter_provenance.md.
UNLINKED_DRAW_STALE_AGE_SECONDS = 3600  # one hour

# Lane RE (6.4), AC-22: the reserve exam's own call-scoped seed channel. stage_runner.dispatch_stage
# (frozen for this lane) calls request_draw below with exactly six positional arguments and no
# room for a seventh; the ONLY way a caller upstream of dispatch_stage can still make its own
# draw_requested row carry a seed, without widening that frozen call site, is an ambient value
# request_draw itself reads at write time. Defaults to None, so every OTHER call to request_draw
# (every existing caller, every existing probe) is completely unaffected: the seed/seed_purpose
# columns stay NULL exactly as before unless reserve_exam_seed below is explicitly entered.
_PENDING_RESERVE_SEED: "contextvars.ContextVar[tuple[int, str] | None]" = contextvars.ContextVar(
    "_PENDING_RESERVE_SEED", default=None,
)


@contextmanager
def reserve_exam_seed(seed: int, seed_purpose: str):
    """Lane RE (6.4): sets the (seed, seed_purpose) pair the NEXT request_draw call inside this
    block records on its own draw_requested row, on both the granted and the refused shapes.
    Cleared on exit even if request_draw is never reached at all (an earlier stage_runner refusal,
    for instance), so a seed can never leak into an unrelated, later draw. reserve_exam.py is the
    one intended caller (AC-22's own seed_purpose word, 'reserve_exam_draw'); no store constraint
    enforces that limitation, exactly as AD8 already states of the reserve draw in general and as
    ac23_seeded_draws_record_seed_and_purpose already proves by hand-inserting a seeded row of
    stage other than reserve without the database ever objecting."""
    token = _PENDING_RESERVE_SEED.set((seed, seed_purpose))
    try:
        yield
    finally:
        _PENDING_RESERVE_SEED.reset(token)


class ChronologyRefused(RuntimeError):
    """R-T2/AD7.1 step 5: a draw whose window is not strictly later than the candidate's own
    high-water mark. Also raised when a concurrent dispatch wins the write lock first (AD13
    failure A6, R-T3a): the loser gets this NAMED refusal, never an opaque
    sqlite3.OperationalError, per the plan's own section 6.6 table ("the loser gets
    ChronologyRefused naming the contention")."""


class DrawWindowMissing(RuntimeError):
    """R-T15: the price universe/dataset row named by the dispatch carries a NULL window bound
    on the named side (or does not exist at all, which reads the same way: no real window to
    grant against). AD11.3's NULL/ABSENT answer forbids the gate from ever reading a
    missing window, so a NULL bound is refused rather than resolved."""


class RunLinkFailed(RuntimeError):
    """AC-49/R-T3a applied to transaction 3 (AD7.3): the run_linked append itself failed. The
    one cause this module can observe and name is a write-lock loss against another
    research.db writer (no busy_timeout is configured anywhere in dashboard/backend/, so a
    contended BEGIN IMMEDIATE fails at once rather than waiting); the other cause AD7.3 names,
    the process dying between the dispatch's own commit and this transaction's, has no caller
    left to raise to, which is exactly why the runs_without_draw_link monitor exists. Carries
    both ids so a human can find the orphaned run and the draw it belongs to without
    re-deriving either from a stack trace."""

    def __init__(self, draw_id: int, run_id: int, detail: str):
        self.draw_id = draw_id
        self.run_id = run_id
        super().__init__(
            f"run_linked append failed for draw {draw_id} / run {run_id}: {detail}; the run "
            "exists and its bars may already have been read (AD7.3, AC-49)"
        )


class ReserveCutCrossed(RuntimeError):
    """Lane RE (6.4), criterion 7, option (a) as ruled (`#399` item 4): a draw at a stage OTHER
    THAN 'reserve' whose window reaches at or past the reserve cut (`#318`: "Everything from
    2023-01-01 onwards is reserve"). Everything from the cut date forward may only be touched
    through the reserve exam (`#302`: "nothing ever optimised on it, only final forms taking the
    exam"), so a screen, optimise or walk-forward draw reaching that far is refused, on EVERY
    calendar including a dry run's own compressed one (ruling text: "on every calendar"),
    because the judge's own guarantee that a verdict never rests on reserve bars has to hold for
    every leg that can produce a verdict, not only the real one."""

    def __init__(self, stage: str, window_end_ts: int, cut_ts: int):
        from datetime import datetime, timezone  # local import: only this message needs date arithmetic (reserve_exam.py's own enumerate_windows idiom)

        self.stage = stage  # the offending stage word, named for the caller
        self.window_end_ts = window_end_ts  # the draw's own window end, so the message is self-contained
        self.cut_ts = cut_ts  # the reserve cut this window crossed, read live from the register
        # Round 5 fold, integration-review finding C2: the human-readable date is DERIVED from
        # cut_ts itself, never restated as a literal, so the register's own value (reserve.cut_date)
        # is the single source for both the seconds and the date shown in this message; an amendment
        # to that register entry changes both automatically instead of leaving one stale.
        cut_date_iso = datetime.fromtimestamp(cut_ts, tz=timezone.utc).date().isoformat()
        super().__init__(
            f"reserve_cut_crossed: a {stage!r}-stage draw's window ends at {window_end_ts} "
            f"(unix seconds UTC), at or after the reserve cut {cut_ts} ({cut_date_iso}, "
            "reserve.cut_date); refusing to let a non-reserve stage touch reserve data. Create a "
            "dataset or universe window ending before the cut, or use the reserve exam instead."
        )


# Lane DV (set 16b, #424, docs/units/DV/criteria.md AC-1): a granted draw whose run crashed
# before storing anything gives its window back, so a later draw for the same candidate is not
# refused for chronology reasons a real result never earned. This is ONE shared SQL fragment
# (a correlated EXISTS test on the outer query's own draw alias "d"), reused by BOTH
# _high_water_mark below (to EXCLUDE such a draw from the candidate's mark) and
# query_draws_given_back (to SELECT exactly such draws for the monitor), so the two can never
# quietly disagree on what "given back" means. "Stored nothing" is read as: the linked run's own
# status is 'failed', AND no research_run_cells row names it, AND no research_full_metrics_cache
# row names it, AND every one of its six artifact path columns is NULL (AC-1's own exact words).
# No schema change: this is a read-time predicate over existing tables, never a new column or
# table, and the draw rows themselves are never edited or deleted (research_data_draws stays
# append-only, per its own DDIA comment in researchdb.py).
_GIVEN_BACK_PREDICATE = """
    EXISTS (
        SELECT 1 FROM research_data_draws l
        JOIN research_runs r ON r.id = l.run_id
        WHERE l.event_kind = 'run_linked' AND l.draw_id = d.id
          AND r.status = 'failed'
          AND NOT EXISTS (SELECT 1 FROM research_run_cells c WHERE c.run_id = r.id)
          AND NOT EXISTS (SELECT 1 FROM research_full_metrics_cache m WHERE m.run_id = r.id)
          AND r.equity_path IS NULL AND r.drawdowns_path IS NULL AND r.trades_path IS NULL
          AND r.stats_path IS NULL AND r.indicators_path IS NULL AND r.meta_path IS NULL
    )
"""


def _high_water_mark(conn: "sqlite3.Connection", strategy_id: int, strategy_version: int,
                     calendar_id: str) -> "int | None":
    """AD6.3's own high-water query, read inside the CALLER's already-open transaction so the
    read and the later append share one BEGIN IMMEDIATE (R-T3: a read taken before the
    transaction opens is a stale read no locking mode can rescue). event_kind is fixed to
    'draw_requested' on purpose (AD6.3's own propagation note): a run_linked row also carries
    a decision and copies the same three grouping columns from the draw it links, and this
    clause alone is what keeps it out of the candidate's mark.

    Lane DV (AC-1): a draw matching _GIVEN_BACK_PREDICATE above is excluded from the MAX() here,
    the give-back itself -- a candidate whose only granted draw crashed with nothing stored reads
    the SAME "no prior draw" NULL a fresh candidate reads, so a later window that draw's own
    (uselessly large) window_end_ts would otherwise have refused is granted instead."""
    row = conn.execute(
        f"SELECT MAX(window_end_ts) AS mark FROM research_data_draws d "
        " WHERE d.event_kind = 'draw_requested' AND d.strategy_id = ? AND d.strategy_version = ? "
        "   AND d.calendar_id = ? AND d.decision = 'granted' "
        f"   AND NOT ({_GIVEN_BACK_PREDICATE});",
        (strategy_id, strategy_version, calendar_id),
    ).fetchone()
    return row["mark"]  # NULL over zero rows, an all-refused set, or an all-given-back set: all read "no prior draw" (8.2 FRESH/EMPTY)


def request_draw(strategy_id: int, strategy_version: int, calendar_id: str, stage: str,
                 universe_id: int, now: int) -> int:
    """Transaction 1 of AD7.1, verbatim. Resolves the requested window from the price
    universe/dataset row `universe_id` names, reads the candidate's high-water mark inside the
    SAME transaction, grants or refuses, and appends the draw_requested row EITHER WAY before
    ever raising (R-T4/AC-19: the refusal itself must be a durable, visible row, not a
    swallowed exception). Returns the new row's id on a grant. Raises DrawWindowMissing (a
    NULL window bound) or ChronologyRefused (the window is not strictly later than the mark,
    or a concurrent dispatch for this same candidate won the write lock first).

    universe_id serves BOTH call sites this milestone has: a price_universe row for the sweep
    dispatch and a plain price_dataset row for the single-backtest dispatch. No dataset_type
    filter is applied here because both kinds keep their test window in the same two columns
    of the same research_datasets table, and the caller (not this gate) is responsible for
    resolving which row it means.
    """
    # Lane RE (6.4), AC-22: the reserve exam's own ambient seed, read ONCE here (never re-read
    # inside the transaction below, since a context manager's value cannot change mid-block) so
    # every INSERT this call might reach carries the SAME pair or the same NULL/NULL absence.
    pending_seed = _PENDING_RESERVE_SEED.get()
    seed_value = pending_seed[0] if pending_seed is not None else None
    seed_purpose_value = pending_seed[1] if pending_seed is not None else None

    try:
        with researchdb.transaction() as conn:  # BEGIN IMMEDIATE; the lock is held from here, not from the append (closes B4's write-skew hazard)
            # AD7.1 step 3: read the SAME two columns dispatch_sweep/dispatch_run will read
            # later, from the same row, so the granted window and the eventual registration
            # compare one stored value against itself (AD7.4) rather than two independently
            # derived ones. A missing row reads as "both bounds absent", the same conclusion a
            # NULL row would produce.
            urow = conn.execute(
                "SELECT window_start_ts, window_end_ts FROM research_datasets WHERE id = ?;",
                (universe_id,),
            ).fetchone()
            window_start_ts = urow["window_start_ts"] if urow is not None else None
            window_end_ts = urow["window_end_ts"] if urow is not None else None

            if window_start_ts is None or window_end_ts is None:
                # R-T15: AD11.3 forbids the gate from ever assuming a NULL bound means full
                # history; a missing bound is refused instead. Name which side was missing (start
                # is checked first, matching the requirement's own left-to-right reading).
                missing_side = "start" if window_start_ts is None else "end"
                reason = f"window_missing:{missing_side}"
                conn.execute(
                    "INSERT INTO research_data_draws "
                    "    (event_kind, strategy_id, strategy_version, calendar_id, "
                    "     counts_against_budget, stage, decision, refusal_reason, seed, "
                    "     seed_purpose, created_at) "
                    "VALUES ('draw_requested', ?, ?, ?, 0, ?, 'refused', ?, ?, ?, ?);",
                    (strategy_id, strategy_version, calendar_id, stage, reason,
                     seed_value, seed_purpose_value, now),
                )
                # The refusal row above is written inside this `with` block, so it commits
                # normally when the block exits; the outcome is captured here and raised only
                # AFTER that commit (below, outside the `with`), so AC-19 holds: a refusal is
                # a durable record even though the exception carries no id back to the caller.
                outcome = ("window_missing", missing_side)
            elif stage != "reserve" and window_end_ts >= reservedb.read_reserve_cut_date_ts():
                # Lane RE (6.4), criterion 7, option (a) (`#399` item 4): a non-reserve draw
                # whose window reaches at or past the reserve cut is refused, on EVERY calendar,
                # before the chronology check below ever runs (a window this far forward would
                # almost always clear that check anyway, and the reserve boundary binds first).
                # The refusal row commits durably before the exception is raised, the SAME R-T4
                # shape the two branches above and below already use.
                cut_ts = reservedb.read_reserve_cut_date_ts()
                conn.execute(
                    "INSERT INTO research_data_draws "
                    "    (event_kind, strategy_id, strategy_version, calendar_id, "
                    "     counts_against_budget, stage, window_start_ts, window_end_ts, "
                    "     decision, refusal_reason, seed, seed_purpose, created_at) "
                    "VALUES ('draw_requested', ?, ?, ?, 0, ?, ?, ?, 'refused', "
                    "        'reserve_cut_crossed', ?, ?, ?);",
                    (strategy_id, strategy_version, calendar_id, stage,
                     window_start_ts, window_end_ts, seed_value, seed_purpose_value, now),
                )
                outcome = ("reserve_cut_crossed", window_end_ts, cut_ts)
            else:
                # AD7.1 step 4: the high-water mark, read inside this same transaction.
                mark = _high_water_mark(conn, strategy_id, strategy_version, calendar_id)
                # AD7.1 step 5: grant on no prior mark, or on a window starting no earlier than
                # it; refuse otherwise.
                granted = mark is None or window_start_ts >= mark
                if granted:
                    # AD6.3's per-kind rule: counts_against_budget is 1 on exactly a GRANTED
                    # draw_requested row under the real calendar, 0 on every dry-run row (R-T9).
                    counts = 1 if calendar_id == "real" else 0
                    row = conn.execute(
                        "INSERT INTO research_data_draws "
                        "    (event_kind, strategy_id, strategy_version, calendar_id, "
                        "     counts_against_budget, stage, window_start_ts, window_end_ts, "
                        "     decision, seed, seed_purpose, created_at) "
                        "VALUES ('draw_requested', ?, ?, ?, ?, ?, ?, ?, 'granted', ?, ?, ?) "
                        "RETURNING id;",
                        (strategy_id, strategy_version, calendar_id, counts, stage,
                         window_start_ts, window_end_ts, seed_value, seed_purpose_value, now),
                    ).fetchone()
                    outcome = ("granted", row["id"])
                else:
                    conn.execute(
                        "INSERT INTO research_data_draws "
                        "    (event_kind, strategy_id, strategy_version, calendar_id, "
                        "     counts_against_budget, stage, window_start_ts, window_end_ts, "
                        "     decision, refusal_reason, seed, seed_purpose, created_at) "
                        "VALUES ('draw_requested', ?, ?, ?, 0, ?, ?, ?, 'refused', "
                        "        'window_not_strictly_later', ?, ?, ?);",
                        (strategy_id, strategy_version, calendar_id, stage,
                         window_start_ts, window_end_ts, seed_value, seed_purpose_value, now),
                    )
                    outcome = ("window_not_strictly_later", window_start_ts, mark)
    except sqlite3.OperationalError as exc:
        # Fold finding 3 (docs/units/T/reads/read_1.md, catch-all-exception-relabelled): this
        # used to relabel EVERY OperationalError from the whole transaction body above as lock
        # contention -- a missing table, a corrupt file, a disk error all came out looking like
        # a losing race, rendered as a 409 no monitor ever counted. Narrowed to the two message
        # shapes SQLite itself raises for a lost BEGIN IMMEDIATE (AD13 A6/R-T3a): two dispatches
        # raced for this candidate's write lock and this one lost. No busy_timeout is configured
        # anywhere in dashboard/backend/ (grep confirms it), so the BEGIN IMMEDIATE above failed
        # at once rather than waiting; nothing was read or written on this attempt, so there is
        # no row to point at, only the contention itself. Named per the plan's own table: "the
        # loser gets ChronologyRefused naming the contention, never an opaque sqlite error."
        # Every OTHER OperationalError re-raises AS ITSELF below, so it surfaces for what it is.
        if "database is locked" in str(exc) or "database table is locked" in str(exc):
            raise ChronologyRefused(
                f"a concurrent draw for candidate ({strategy_id}, v{strategy_version}) under "
                f"calendar {calendar_id!r} is being processed right now (write-lock contention, "
                f"no busy_timeout configured); retry the dispatch (AD13 A6, R-T3a): {exc}"
            ) from exc
        raise

    # Everything below runs AFTER the transaction above has already committed (or, for the
    # granted case, returns before any exception is ever considered).
    if outcome[0] == "granted":
        return outcome[1]
    if outcome[0] == "window_missing":
        raise DrawWindowMissing(
            f"universe/dataset {universe_id} has a NULL window bound on the {outcome[1]} side; "
            f"refusing to treat it as full history for candidate ({strategy_id}, "
            f"v{strategy_version}) under calendar {calendar_id!r} (R-T15)"
        )
    if outcome[0] == "reserve_cut_crossed":
        raise ReserveCutCrossed(stage, outcome[1], outcome[2])
    # outcome[0] == "window_not_strictly_later"
    raise ChronologyRefused(
        f"candidate ({strategy_id}, v{strategy_version}) under calendar {calendar_id!r}: "
        f"requested window_start_ts={outcome[1]} is not strictly later than its high-water "
        f"mark {outcome[2]!r}; draw refused and recorded (R-T2)"
    )


def link_run(draw_id: int, run_id: int, now: int) -> None:
    """Transaction 3 of AD7.3: an APPENDED run_linked event, never an update to the draw row
    that already exists (R-T14). Compares the run's ACTUAL window (the frozen
    research_sweep_registrations row when one exists, matching AD7.3's own words; otherwise
    the run's own dataset's current window, for the single-backtest site, which registers
    nothing) against the window the draw was granted, and records the outcome: 'granted' on
    equality, 'refused' with reason 'registered_window_mismatch' otherwise. A NULL on either
    side of that comparison is treated as a mismatch, never as equality (AD7.3's own reading):
    for a run this gate actually dispatched neither bound can legitimately be NULL, since
    request_draw already refused a NULL universe window before any dispatch happened.

    Raises RunLinkFailed, naming both ids, when the append itself loses a write-lock race
    (AC-49): the run already exists at that point and its bars may already be unrecoverable
    from this draw's side, so the caller must see a named, debuggable error rather than an
    opaque sqlite3.OperationalError (R-T3a, which binds every transaction this unit opens).
    """
    try:
        with researchdb.transaction() as conn:  # BEGIN IMMEDIATE; a THIRD, independent transaction (AD7.3)
            draw = conn.execute(
                "SELECT strategy_id, strategy_version, calendar_id, window_start_ts, window_end_ts "
                "  FROM research_data_draws WHERE id = ? AND event_kind = 'draw_requested';",
                (draw_id,),
            ).fetchone()
            if draw is None:
                # A caller-contract violation, not one of AD7.3's own residuals: draw_id must
                # name a real draw_requested row, since only request_draw ever returns one.
                # Plain ValueError (not RunLinkFailed, which is reserved for the write-lock
                # failure AC-49 names), and nothing has been written yet, so the transaction()
                # helper's own rollback is the correct, harmless outcome here.
                raise ValueError(f"draw {draw_id} is not a draw_requested row; link_run cannot run against it")

            # AD7.3: prefer the FROZEN sweep registration window, because dispatch_sweep
            # freezes window_start_ts/window_end_ts onto research_sweep_registrations at write
            # time specifically so a later edit of the mutable research_datasets row cannot
            # change what a completed sweep is compared against (researchdb.py's own comment
            # on that table). The single-backtest site writes no registration at all (section
            # 6.2's own words), so it falls back to the run's own dataset's CURRENT window,
            # read via research_runs.dataset_id -> research_datasets.
            registration = conn.execute(
                "SELECT window_start_ts, window_end_ts FROM research_sweep_registrations WHERE run_id = ?;",
                (run_id,),
            ).fetchone()
            if registration is not None:
                run_window_start_ts = registration["window_start_ts"]
                run_window_end_ts = registration["window_end_ts"]
            else:
                run_row = conn.execute(
                    "SELECT ds.window_start_ts, ds.window_end_ts FROM research_runs r "
                    "JOIN research_datasets ds ON ds.id = r.dataset_id WHERE r.id = ?;",
                    (run_id,),
                ).fetchone()
                run_window_start_ts = run_row["window_start_ts"] if run_row is not None else None
                run_window_end_ts = run_row["window_end_ts"] if run_row is not None else None

            equal = (
                run_window_start_ts is not None and run_window_end_ts is not None
                and run_window_start_ts == draw["window_start_ts"]
                and run_window_end_ts == draw["window_end_ts"]
            )
            decision = "granted" if equal else "refused"
            refusal_reason = None if equal else "registered_window_mismatch"
            conn.execute(
                "INSERT INTO research_data_draws "
                "    (event_kind, strategy_id, strategy_version, calendar_id, "
                "     counts_against_budget, draw_id, run_id, decision, refusal_reason, created_at) "
                "VALUES ('run_linked', ?, ?, ?, 0, ?, ?, ?, ?, ?);",
                (draw["strategy_id"], draw["strategy_version"], draw["calendar_id"],
                 draw_id, run_id, decision, refusal_reason, now),
            )
    except sqlite3.OperationalError as exc:
        # Fold finding 3 (docs/units/T/reads/read_1.md): the same narrowing as request_draw's
        # own handler above, and for the same reason -- before this fold ANY OperationalError
        # here, not only a lost write-lock race, came out as a RunLinkFailed naming "write-lock
        # contention" even when the real cause was a missing table or a corrupt file. Only the
        # two message shapes SQLite itself uses for a lost BEGIN IMMEDIATE are relabelled; every
        # other OperationalError re-raises unchanged.
        if "database is locked" in str(exc) or "database table is locked" in str(exc):
            raise RunLinkFailed(draw_id, run_id, f"write-lock contention appending run_linked: {exc}") from exc
        raise


def draw_id_for_run(run_id: int) -> "int | None":
    """Resolve a run back to the draw it was granted for, via its run_linked row (R-T14: this
    is the ONLY path from a run back to its draw; draw_requested itself never carries run_id
    and bars_observed cannot either). Returns None when no run_linked row names this run,
    which is exactly the runs_without_draw_link population (B17) and the caller's own signal
    to write no observation at all rather than guess a draw_id. Reads by run_id alone,
    regardless of the link's own decision: a 'refused' registered_window_mismatch link still
    names a run whose bars were genuinely read (AD7.4's own point: that failure is NOT
    conservative), so the observation must still be recorded against it."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT draw_id FROM research_data_draws WHERE event_kind = 'run_linked' AND run_id = ?;",
            (run_id,),
        ).fetchone()
    return row["draw_id"] if row is not None else None


def record_bars_observed(draw_id: int, bars_observed: int, now: int) -> None:
    """W8 (AD10.1): append a bars_observed event for a granted draw, copying the candidate
    columns from the draw_requested row it observes (run_id stays NULL on this kind and
    cannot be otherwise, per the CHECK set on research_data_draws). R-T6: bars_observed=0 is a
    real, distinct value from "never observed" (no row at all), so a caller must call this
    with the true count INCLUDING zero, never skip the call to avoid writing a zero."""
    with researchdb.transaction() as conn:
        draw = conn.execute(
            "SELECT strategy_id, strategy_version, calendar_id FROM research_data_draws "
            " WHERE id = ? AND event_kind = 'draw_requested';",
            (draw_id,),
        ).fetchone()
        if draw is None:
            raise ValueError(f"draw {draw_id} is not a draw_requested row; cannot record an observation for it")
        conn.execute(
            "INSERT INTO research_data_draws "
            "    (event_kind, strategy_id, strategy_version, calendar_id, "
            "     counts_against_budget, draw_id, bars_observed, created_at) "
            "VALUES ('bars_observed', ?, ?, ?, 0, ?, ?, ?);",
            (draw["strategy_id"], draw["strategy_version"], draw["calendar_id"],
             draw_id, bars_observed, now),
        )


def record_names_drawn(calendar_id: str, seed: int, member_count: int, now: int) -> None:
    """W9 (AD8, AD9): the held-out name draw, a store-wide event belonging to no candidate
    (strategy_id/strategy_version both left NULL, forced by the table's own CHECK). At most
    one per calendar is enforced by ux_draws_names_drawn_once, a partial UNIQUE index: a
    second call for a calendar that already has one raises sqlite3.IntegrityError, which this
    function lets propagate rather than swallowing, since "at most one per calendar" IS the
    guarantee the caller needs (AC-43) and a swallowed violation would hide a real bug."""
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_data_draws "
            "    (event_kind, calendar_id, counts_against_budget, seed, seed_purpose, "
            "     member_count, created_at) "
            "VALUES ('names_drawn', ?, 0, ?, 'held_out_name_draw', ?, ?);",
            (calendar_id, seed, member_count, now),
        )


def trial_count(dataset_sha256: str, strategy_id: int) -> int:
    """R-T7/AC-22: read research_trials at its OWN existing index scope
    (ix_trials_count_scope: dataset_sha256, strategy_id, created_at), never a second counter.
    research_trials is already the durable trial ledger; duplicating its count here would be a
    second source of truth that can only ever disagree with the first."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM research_trials WHERE dataset_sha256 = ? AND strategy_id = ?;",
            (dataset_sha256, strategy_id),
        ).fetchone()
    return row["n"]


# ============================================================
# The five monitor queries: the four plan section 6.6 names, plus query_draws_without_observation
# (fold finding 2, docs/units/T/reads/read_1.md -- generator_requirements section 11 names this
# signal explicitly and the plan's own 6.6 table omitted it, which is the plan's gap, not a
# reason to skip it). Each is a QUERY over durable rows (R-T12: "every such signal is a QUERY
# over a durably recorded row, never a process-local integer"). These are read by an
# ops-monitor tick, which supplies its own connection, matching
# generated.query_generated_rows_without_candidate's own convention.
# ============================================================

def query_chronology_refusals(conn: "sqlite3.Connection") -> "dict[str, int]":
    """granted_draws_without_run_link's sibling row, chronology_refusals: every draw_requested
    refusal, grouped by its own reason string. Grouping by reason (rather than one bare count)
    is what R-T12 asks for: window_not_strictly_later and the two window_missing:<side> reasons
    are three different causes, and a signal states the causes its population holds."""
    rows = conn.execute(
        "SELECT refusal_reason, COUNT(*) AS n FROM research_data_draws "
        " WHERE event_kind = 'draw_requested' AND decision = 'refused' "
        " GROUP BY refusal_reason;"
    ).fetchall()
    return {r["refusal_reason"]: r["n"] for r in rows}


def query_granted_draws_without_run_link(conn: "sqlite3.Connection", now: int,
                                         max_age_seconds: int = UNLINKED_DRAW_STALE_AGE_SECONDS) -> "list[int]":
    """B14/A8 (the draw side, R-T16): granted draw_requested rows older than max_age_seconds
    with no run_linked row naming them. This population CONFLATES A8 (the dispatch never
    produced a run: conservative, no bars read) with A9 (a run exists but its link never
    committed: not conservative, bars were read) by construction, exactly as R-T16 states;
    query_runs_without_draw_link is the second signal that tells them apart."""
    rows = conn.execute(
        "SELECT d.id FROM research_data_draws d "
        " WHERE d.event_kind = 'draw_requested' AND d.decision = 'granted' "
        "   AND d.created_at <= ? "
        "   AND NOT EXISTS (SELECT 1 FROM research_data_draws l "
        "                    WHERE l.event_kind = 'run_linked' AND l.draw_id = d.id);",
        (now - max_age_seconds,),
    ).fetchall()
    return [r["id"] for r in rows]


def query_runs_without_draw_link(conn: "sqlite3.Connection") -> "list[int]":
    """B17/A9 (the run side, R-T16): research_runs rows for a candidate holding a granted draw
    that already existed by the time the run was created, that no run_linked event names, read
    from the RUN side rather than the draw side. A count of 0 here (alongside a nonzero
    granted_draws_without_run_link) means every unlinked-looking draw is A8's conservative
    case; any row here is A9's, where the bars were genuinely read and nothing checked the
    window (AC-48 never ran for that run).

    Bounded and deduplicated (fold finding 4, docs/units/T/reads/read_1.md,
    monitor-query-unbounded-population): the original join matched on strategy_id and
    strategy_version alone with NO time bound, so on the real store every run of a candidate
    dispatched BEFORE this table ever existed became a permanent hit the moment that candidate
    later earned one granted draw, and a candidate with SEVERAL granted draws returned the same
    run id once per draw. The join below requires a granted draw created at or before the run's
    own created_at, so a run OLDER than every granted draw for its candidate is OUTSIDE this
    population BY CONSTRUCTION -- it could never have been gated by a draw that did not exist
    yet -- and SELECT DISTINCT collapses the once-per-qualifying-draw duplicate. research_runs
    carries no calendar_id column of its own (calendar_id lives only on research_data_draws
    rows, and a run with no run_linked row has none recorded anywhere), so this ties on
    candidate identity (strategy_id, strategy_version) and time only, never on calendar; that
    is a real, schema-shaped limit on this query's precision, not an oversight.
    """
    rows = conn.execute(
        "SELECT DISTINCT r.id FROM research_runs r "
        "JOIN research_data_draws d "
        "  ON d.event_kind = 'draw_requested' AND d.decision = 'granted' "
        "  AND d.strategy_id = r.strategy_id AND d.strategy_version = r.strategy_version "
        "  AND d.created_at <= r.created_at "
        " WHERE NOT EXISTS (SELECT 1 FROM research_data_draws l "
        "                    WHERE l.event_kind = 'run_linked' AND l.run_id = r.id);"
    ).fetchall()
    return [r["id"] for r in rows]


def query_draw_window_mismatches(conn: "sqlite3.Connection") -> "list[int]":
    """A7/B15: run_linked rows refused for 'registered_window_mismatch' -- a run whose actual
    window differed from the window its own draw was granted, caught by link_run's own
    read-back (AD7.4's residual, closed by detection rather than prevention)."""
    rows = conn.execute(
        "SELECT id FROM research_data_draws "
        " WHERE event_kind = 'run_linked' AND decision = 'refused' "
        "   AND refusal_reason = 'registered_window_mismatch';"
    ).fetchall()
    return [r["id"] for r in rows]


def query_draws_without_observation(conn: "sqlite3.Connection") -> "list[int]":
    """generator_requirements_2026-09-05.md section 11's own named signal (fold finding 2,
    docs/units/T/reads/read_1.md, declared-signal-not-built): a granted draw_requested row
    whose run SUCCEEDED (research_runs.status = 'succeeded') via a run_linked row, with no
    bars_observed row naming the draw. A finished run that never got an observation means the
    result-callback wiring itself failed to record what was actually read -- a genuine gap,
    distinct from B14/B17's still-in-flight or lost-link populations, neither of which says
    anything about whether the run FINISHED. SELECT DISTINCT guards against a draw somehow
    reached by more than one run_linked row (never expected under AD7.3's one-link-per-run
    design, but cheap insurance rather than an assumption the query leans on).

    Scoped to status = 'succeeded', never merely finished_at IS NOT NULL (fold T-R2-3,
    docs/units/T/reviews/code_quality_round2.md, draws-without-observation-conflates-terminal-
    status): finished_at is set by FOUR call sites, not two -- complete_run/complete_sweep on
    success, but ALSO fail_run and cancel_run, on a failure or a cancellation, which are both
    routine, un-remarkable outcomes. record_bars_observed is only ever called from the two
    success-path worker callbacks (worker_run_result, worker_sweep_result), so a finished-but-
    failed or finished-but-cancelled linked run would otherwise sit in this population forever
    with no way to clear it, even though nothing about the tracker's own wiring is broken."""
    rows = conn.execute(
        "SELECT DISTINCT d.id FROM research_data_draws d "
        "JOIN research_data_draws l "
        "  ON l.event_kind = 'run_linked' AND l.draw_id = d.id "
        "JOIN research_runs r ON r.id = l.run_id "
        " WHERE d.event_kind = 'draw_requested' AND d.decision = 'granted' "
        "   AND r.status = 'succeeded' "
        "   AND NOT EXISTS (SELECT 1 FROM research_data_draws o "
        "                    WHERE o.event_kind = 'bars_observed' AND o.draw_id = d.id);"
    ).fetchall()
    return [r["id"] for r in rows]


def query_draws_given_back(conn: "sqlite3.Connection", draw_ids: "list[int] | None" = None) -> "list[int]":
    """Lane DV (set 16b, `#424`, docs/units/DV/criteria.md AC-7): every granted draw_requested
    row matching AC-1's own give-back shape (see _GIVEN_BACK_PREDICATE above), a durable QUERY
    over already-recorded rows rather than a process-local counter (R-T12's own rule, matching
    every sibling monitor query in this file): a given-back draw IS a real, committed row, unlike
    a pre-draw refusal (AC-4/AC-5's own two counters, which have no row to query because they
    fire before any draw is ever requested). Registry row `draws_given_back`.

    Round-2 finding SF1 (code-quality gate 1): `draw_ids`, when given, scopes the scan to exactly
    those draw ids (`AND d.id IN (...)`) instead of the whole table, so a run-page chain view
    (`_build_run_chain`'s own bounded set of at most len(STAGE_WORDS) draws) never pays a full
    `research_data_draws` scan that grows with total draw volume. `None` (the default) keeps the
    UNSCOPED form, which is what the monitor itself needs: the whole store's own given-back
    population, not one run's slice of it. An empty list is a real, distinct input (a caller that
    genuinely has zero draw ids to ask about) and returns an empty result without ever touching
    the unscoped form, rather than being treated the same as "no scoping requested."
    """
    if draw_ids is None:
        rows = conn.execute(
            f"SELECT d.id FROM research_data_draws d "
            " WHERE d.event_kind = 'draw_requested' AND d.decision = 'granted' "
            f"   AND ({_GIVEN_BACK_PREDICATE});"
        ).fetchall()
        return [r["id"] for r in rows]
    if not draw_ids:  # the empty-list case: nothing to scan, nothing to ask the database at all
        return []
    placeholders = ",".join("?" for _ in draw_ids)
    rows = conn.execute(
        f"SELECT d.id FROM research_data_draws d "
        " WHERE d.event_kind = 'draw_requested' AND d.decision = 'granted' "
        f"   AND d.id IN ({placeholders}) "
        f"   AND ({_GIVEN_BACK_PREDICATE});",
        draw_ids,
    ).fetchall()
    return [r["id"] for r in rows]


def query_draws_past_reserve_cut(conn: "sqlite3.Connection") -> "list[int]":
    """Lane RE (6.4), criterion 7's own monitor row (`draws_past_reserve_cut`, docs/units/RE/
    monitor_rows.md): every GRANTED, non-reserve draw_requested row whose window reaches at or
    past the reserve cut. Under option (a) as ruled (`#399` item 4), request_draw's own guard
    above refuses every such draw before it can ever be granted, so this population is EXPECTED
    ZERO from the moment this build is live; a nonzero reading names either a row written before
    this guard existed (the historical backlog) or a defect in the guard itself, and the two are
    told apart by `created_at` against this build's own deploy time, which this query leaves to
    its reader rather than hardcoding a deploy timestamp here."""
    cut_ts = reservedb.read_reserve_cut_date_ts()  # read live, matching request_draw's own guard above
    rows = conn.execute(
        "SELECT id FROM research_data_draws WHERE event_kind = 'draw_requested' "
        "  AND decision = 'granted' AND stage != 'reserve' AND window_end_ts >= ?;",
        (cut_ts,),
    ).fetchall()
    return [r["id"] for r in rows]


def query_walkforward_fill_reaches_outside_draw(conn: "sqlite3.Connection") -> "list[int]":
    """Lane LB (5.8e), criterion 6's own monitor row (`walkforward_fill_reaches_outside_draw`,
    docs/units/LB/monitor_rows.md): guarantee (ii) says every slice's fill_reach_ts is NULL or
    strictly between its own leg's last bar and the run's granted draw window_end_ts. This query
    checks the half of that guarantee a steady-state read CAN check from the stored columns alone:
    a non-null fill_reach_ts never falls before the slice's own in_sample_start_ts (the loosest
    honest lower bound, since fill_reach_after only ever returns a bar strictly after whichever
    leg's own clipped frame produced it, and the table does not record WHICH leg that was) and
    never reaches at or past the run's own draw window_end_ts (the reserve-safety half of the
    guarantee, and the one #406 ruling 2 exists to protect). The tighter lower bound, one leg's own
    exact end rather than the slice's earliest possible start, is proved once at build time by the
    criterion-6 probe reading both legs' own stats dicts directly, not by this query, because the
    table's own columns cannot tell which leg's reach a stored value came from.

    Same join shape as query_draws_past_reserve_cut and researchdb.query_walkforward_slices_out_of_
    window: from the slice to its run's own run_linked row to the draw_requested row that granted
    the window, the only path from a run to its own draw. Expected EMPTY on a healthy pipeline; any
    row returned names either a fill_reach_after defect or a reach computed against the wrong bound
    (a slice's own end rather than the granted window's end, the exact bug row 5.8e's guarantee
    (iii) argues against)."""
    rows = conn.execute(
        "SELECT s.run_id FROM research_walkforward_slices s "
        "JOIN research_data_draws rl ON rl.event_kind = 'run_linked' AND rl.run_id = s.run_id "
        "JOIN research_data_draws dr ON dr.id = rl.draw_id AND dr.event_kind = 'draw_requested' "
        "WHERE s.fill_reach_ts IS NOT NULL "
        "  AND (s.fill_reach_ts < s.in_sample_start_ts OR s.fill_reach_ts >= dr.window_end_ts);"
    ).fetchall()
    return [r["run_id"] for r in rows]
