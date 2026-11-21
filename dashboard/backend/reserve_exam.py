"""dashboard/backend/reserve_exam.py: lane RE (6.4), the reserve exam's own rule.

Governing text: docs/plans/night/2026-09-15_set9_night/PLAN.md section 8, AC-21 to AC-27;
docs/briefs/dry_run_spec_brief.md section 7. This file is the ONE place that decides which
enumerated reserve window a candidate draws, whether that candidate is a final form allowed to sit
the exam at all, and how the exam's single parameter set reaches the real, UNCHANGED stage runner.
It owns no schema of its own (reservedb.py does) and it is imported by routes/reserve.py, never the
reverse.

Every window bound and count here comes from reservedb's two provenance readers
(read_reserve_cut_date_ts, read_reserve_exam_window_months), never an in-code literal: "No digit
restated in code without its register name" (this lane's own criterion 1).

Three frozen files this module calls into but never edits, per this lane's own stop conditions:
researchdb.py (through its public get_conn()/transaction() only), stage_runner.py
(build_screen_universe and dispatch_stage, called exactly as any other caller would), and
judgedb.py (only to read its own _PASS constant, the survivor word compose_verdict returns, the
same "import a private-looking name across a module boundary rather than restate its value"
precedent researchdb.py already sets for provenance_parse._REQUIRED_PROVENANCE_SECTIONS).
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass

from . import researchdb, reservedb, stage_runner, tracker
from .judgedb import _PASS as SURVIVOR_VERDICT_WORD  # compose_verdict's own survivor word, read rather than restated (AC-23)

# Round 3 fold, code-quality finding B1: the lead-in's own reach into earlier reserve windows,
# approximated from a reserve window's own trading-day count rather than restated as a fixed
# "one window back". This codebase's OWN established trading-year convention is 252 days
# (research_lab/strategies/base/event_pipeline.py's EVENT_ANNUALIZATION_YEAR_FREQ, Carver's ~256
# sqrt-time base, carver-systematic-trading-tibot.md:421-422); dividing by 12 months gives an
# approximate bars-per-month figure. This is an APPROXIMATION only, adequate for deciding which
# enumerated reserve window a fixed-bar lead-in reaches back into, never a bar-exact calendar
# (real month lengths and market holidays vary window to window).
_TRADING_DAYS_PER_MONTH_APPROX = 252 / 12  # = 21.0

# The "what can break" section's own reserve_exam_refusals_by_reason row (docs/units/RE/
# monitor_rows.md), a process-local, in-memory dict, mirroring stage_runner.py's own
# STAGE_RUNNER_MONITOR_COUNTERS["walkforward_refusals_by_reason"] idiom: a reason-string key,
# created on its own first occurrence rather than pre-declared, reset on every backend restart,
# never a durable row (this lane's own refusals fire BEFORE any tracker row is written, so there
# is no database population this counter could read instead). Every ReserveExamError subclass this
# module raises bumps its own bucket in its own __init__, so an increment can never be missed at a
# call site the way a raise-site increment could be if a new raise were added later without one.
RESERVE_EXAM_MONITOR_COUNTERS = {"refusals_by_reason": {}}


def _record_refusal(reason: str) -> None:
    """Bump the named reason bucket by one, creating it on first occurrence (stage_runner.py's
    own _record_walkforward_refusal, copied rather than imported: that function is private to its
    own module and this lane may not add a cross-module call into a frozen file)."""
    bucket = RESERVE_EXAM_MONITOR_COUNTERS["refusals_by_reason"]
    bucket[reason] = bucket.get(reason, 0) + 1


@dataclass(frozen=True)
class ReserveWindow:
    """One enumerated reserve half-year, 1-based index (section 7's own numbering) plus its
    half-open [start_ts, end_ts) bounds in unix seconds UTC, the same convention every window this
    codebase stores already uses (AD7.5)."""
    index: int
    start_ts: int
    end_ts: int


class ReserveExamError(RuntimeError):
    """Base class for every refusal this module raises. Never raised directly."""


class ReserveSupplyExhausted(ReserveExamError):
    """AC-25's own EMPTY SET: every enumerated reserve window is already spent by some
    candidate's verdict (#302: "windows are disjoint while supply lasts"). Names the count spent
    against the count enumerated, so a caller can see exactly how tight the supply is without a
    second query."""

    def __init__(self, spent: int, enumerated: int) -> None:
        self.spent = spent
        self.enumerated = enumerated
        _record_refusal("reserve-supply-exhausted")
        super().__init__(
            f"reserve-supply-exhausted: {spent} of {enumerated} enumerated reserve windows are "
            "already spent by a verdict; no unspent window remains for a new exam"
        )


class FinalFormVerdictAbsent(ReserveExamError):
    """AC-23's first refusal shape: this strategy_id has never been graded at all (no
    research_judge_verdicts row of any outcome), so there is no verdict for "final form" to be
    read against."""

    def __init__(self, strategy_id: int) -> None:
        self.strategy_id = strategy_id
        _record_refusal("final-form-verdict-absent")
        super().__init__(
            f"final-form-verdict-absent: strategy {strategy_id} has never been graded by the "
            "judge; the reserve exam admits only a candidate with a recorded verdict"
        )


class FinalFormNotSurvivor(ReserveExamError):
    """AC-23's second refusal shape: the candidate's newest verdict exists but is not a survivor
    (compose_verdict's own word 'pass'); names the word actually stored so a caller can see
    whether it failed outright or came back unjudgeable."""

    def __init__(self, strategy_id: int, verdict: str) -> None:
        self.strategy_id = strategy_id
        self.verdict = verdict
        _record_refusal("final-form-not-survivor")
        super().__init__(
            f"final-form-not-survivor: strategy {strategy_id}'s newest verdict is {verdict!r}, "
            f"not {SURVIVOR_VERDICT_WORD!r}; the reserve exam admits only a survivor"
        )


class FinalFormParamsUnrecorded(ReserveExamError):
    """AC-23's third refusal shape: the survivor's own graded run carries no
    research_run_variations row at the verdict's own variation_id (lane JC, section 7 row B-8:
    this is the verdict's own combination, never a hardcoded row 0), so there is no single
    parameter set this exam could dispatch. A single-run candidate needs lane DG's own writer
    (6.1g); a sweep-graded one already has this row today."""

    def __init__(self, strategy_id: int, run_id: int, variation_id: int) -> None:
        self.strategy_id = strategy_id
        self.run_id = run_id
        self.variation_id = variation_id
        _record_refusal("final-form-params-unrecorded")
        super().__init__(
            f"final-form-params-unrecorded: strategy {strategy_id}'s graded run {run_id} carries "
            f"no research_run_variations row at its own verdict's variation_id ({variation_id}); "
            "the reserve exam has no single parameter set to dispatch"
        )


class ReserveGridForbidden(ReserveExamError):
    """AC-24: nothing is ever optimised on the reserve window. Raised BEFORE any draw is
    requested when the assembled param_grid's own product exceeds 1, naming the product so a
    caller can see how far over the limit it was; this build never constructs such a grid itself
    (every value list holds exactly one element by construction), so reaching this branch would
    mean the survivor's own recorded param_json was not a flat, single combo."""

    def __init__(self, strategy_id: int, grid_product: int) -> None:
        self.strategy_id = strategy_id
        self.grid_product = grid_product
        _record_refusal("reserve-grid-forbidden")
        super().__init__(
            f"reserve-grid-forbidden: strategy {strategy_id}'s assembled param_grid has a product "
            f"of {grid_product}, not 1; nothing is ever optimised on the reserve window"
        )


class SizingSpecNotRecorded(ReserveExamError):
    """P2h: the graded run recorded a sizing method but no sizing spec, so the exam cannot re-test the
    candidate with the sizing it was graded under. Refused before any universe or draw is written."""

    def __init__(self, strategy_id: int, run_id: int, sizing_method: str) -> None:
        self.strategy_id = strategy_id
        self.run_id = run_id
        _record_refusal("sizing-spec-not-recorded")
        super().__init__(
            f"sizing-spec-not-recorded: strategy {strategy_id}'s graded run {run_id} recorded sizing method "
            f"{sizing_method!r} but no sizing spec; the reserve exam cannot test it with the sizing it was graded under"
        )


class SizingNotRecordedForExam(ReserveExamError):
    """P2j (DECISIONS #479 item 9, #488 item 9, #489 item 4): the graded run's row is missing, or both its
    sizing columns are NULL, so the exam cannot test the candidate with the sizing it was graded under and no
    default sizing is used. Refused before any universe or draw is written."""

    def __init__(self, strategy_id: int, run_id: int) -> None:
        self.strategy_id = strategy_id
        self.run_id = run_id
        _record_refusal("sizing-not-recorded")
        super().__init__(
            f"sizing-not-recorded: strategy {strategy_id} was graded on run {run_id}, which has no recorded "
            "sizing, so the reserve exam cannot test it with the sizing it was graded under; "
            "no default sizing is used"
        )


class RecordedSizingSpecUnreadableForExam(ReserveExamError):
    """P2h: the graded run's recorded sizing spec cannot be read as SizingSpec's five fields
    (researchdb.parse_recorded_sizing_spec refused it). `detail` is that refusal's own message, which
    already starts "recorded-sizing-spec-unreadable:" and names the run. Refused before any universe or
    draw is written."""

    def __init__(self, strategy_id: int, run_id: int, detail: str) -> None:
        self.strategy_id = strategy_id
        self.run_id = run_id
        _record_refusal("recorded-sizing-spec-unreadable")
        super().__init__(detail)


class ReserveSeedAbsent(ReserveExamError):
    """AC-22's own second half: "the runner refuses to proceed when the seed is absent even
    though the store would accept the row." This is dispatch_exam's OWN refusal, checked before
    anything else runs, never a store-level constraint (AD8's own words: research_data_draws would
    happily accept a seedless reserve row)."""

    def __init__(self, strategy_id: int) -> None:
        self.strategy_id = strategy_id
        _record_refusal("reserve-seed-absent")
        super().__init__(
            f"reserve-seed-absent: strategy {strategy_id}'s exam dispatch carried no seed; "
            "the reserve exam always draws its window by a caller-supplied seed"
        )


def _add_months(dt, months: int):
    """Advance a UTC-midnight datetime by a whole number of months, rolling the year over
    correctly (Python's own datetime has no month-arithmetic operator, and pulling in
    dateutil.relativedelta for one call is a heavier dependency than this three-line rollover)."""
    total_months = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(total_months, 12)
    return dt.replace(year=year, month=month0 + 1)


def enumerate_windows(now_ts: int) -> "list[ReserveWindow]":
    """AC-21's own rule: every CLOSED reserve window from reservedb.read_reserve_cut_date_ts()
    forward, each reservedb.read_reserve_exam_window_months() months long, half-open
    [start_ts, end_ts). A window is CLOSED when its own end_ts is at or before now_ts; the first
    window whose end has not yet arrived stops the enumeration (#318: "the reserve grows", so the
    supply is whatever has closed as of now, never a fixed roster this function hardcodes). At
    2026-01, section 7's own six windows are exactly this rule's own output; the count grows with
    the calendar, which is why no probe ever pins a literal count here, only derives one from the
    date it ran on.
    """
    from datetime import datetime, timezone  # local import: only this function needs date arithmetic

    cut_ts = reservedb.read_reserve_cut_date_ts()
    window_months = reservedb.read_reserve_exam_window_months()
    cut_date = datetime.fromtimestamp(cut_ts, tz=timezone.utc)

    windows: "list[ReserveWindow]" = []
    start = cut_date
    index = 1
    while True:
        end = _add_months(start, window_months)
        end_ts = int(end.timestamp())
        if end_ts > now_ts:  # this window has not closed yet: the enumeration stops here
            break
        windows.append(ReserveWindow(index=index, start_ts=int(start.timestamp()), end_ts=end_ts))
        start = end
        index += 1
    return windows


def draw_window(seed: int, now_ts: int) -> ReserveWindow:
    """AC-21: the same seed reproduces the same window, drawn from the UNSPENT supply only
    (criterion 2). Raises ReserveSupplyExhausted when nothing unspent remains."""
    with researchdb.get_conn() as conn:
        supply = reservedb.query_reserve_supply(conn, now_ts)
    unspent = sorted((w for w in supply["windows"] if not w["spent"]), key=lambda w: w["index"])
    if not unspent:
        raise ReserveSupplyExhausted(spent=supply["spent"], enumerated=supply["enumerated"])
    chosen = random.Random(seed).choice(unspent)  # deterministic: the same seed over the same sorted unspent list always picks the same entry
    return ReserveWindow(index=chosen["index"], start_ts=chosen["start_ts"], end_ts=chosen["end_ts"])


def _lead_in_reach_index(window_index: int, window_months: int, lead_in_bars: int) -> "int | None":
    """AC-27's own reach, corrected (round 3 fold, B1): walk BACK from the drawn window by however
    many enumerated reserve windows lead_in_bars (the pinned spec's estimator_window) worth of trading bars spans,
    using _TRADING_DAYS_PER_MONTH_APPROX x window_months as one window's own approximate bar
    count, rather than a fixed "one window back" (which under-reports from window index 3 upward,
    since 278 bars is more than two six-month windows' worth). Returns the EARLIEST reserve window
    index the lead-in reaches, clamped to 1: index 1 is the earliest reserve window that exists, so
    a lead-in reaching further back is reading PRE-CUT TRAINING bars, which are not reserve data
    and are not recorded by this function. Returns None only for window index 1 itself, which has
    no earlier reserve window to reach into at all (AC-27's own first case).
    """
    if window_index <= 1:
        return None
    bars_per_window = _TRADING_DAYS_PER_MONTH_APPROX * window_months
    windows_back = math.ceil(lead_in_bars / bars_per_window)
    return max(1, window_index - windows_back)


def dispatch_exam(strategy_id: int, seed, calendar_id: str, now_ts: int) -> dict:
    """Criterion 4, in order: refuse a missing seed (AC-22); admit only a final form (AC-23);
    assemble the one-combo grid and refuse a wider one (AC-24); draw the window among the unspent
    supply (AC-21/AC-25); dispatch through the REAL, unchanged stage_runner exactly as any other
    caller would; record the exam (AC-26/AC-27). Returns the exam record's own reportable fields.
    """
    if seed is None:  # AC-22's own second half: the runner's own refusal, never a store constraint
        raise ReserveSeedAbsent(strategy_id)

    # AC-23: only a final form takes the exam. "The newest verdict for this strategy_id" spans
    # every version it has ever carried, because a candidate can be re-graded after an edit; the
    # GRADED version travelling with that verdict, not the caller's own guess, is what dispatch_stage
    # is pinned to below, so an edit made after grading surfaces as CandidateVersionMoved rather
    # than silently running the exam against source the judge never saw.
    with researchdb.get_conn() as conn:
        verdict_row = conn.execute(
            "SELECT run_id, strategy_version, verdict, variation_id FROM research_judge_verdicts "
            " WHERE strategy_id = ? AND is_advisory = 0 ORDER BY graded_at DESC, id DESC LIMIT 1;",
            (strategy_id,),
        ).fetchone()
    if verdict_row is None:
        raise FinalFormVerdictAbsent(strategy_id)
    if verdict_row["verdict"] != SURVIVOR_VERDICT_WORD:
        raise FinalFormNotSurvivor(strategy_id, verdict_row["verdict"])
    graded_version = verdict_row["strategy_version"]
    verdict_run_id = verdict_row["run_id"]
    # Lane JC (candidate chain link spec, section 7 row B-8): the newest verdict's OWN
    # variation_id, never a hardcoded 0. A single-run verdict is always graded at variation_id=0
    # (complete_run's own single-run-as-cell write), so this is unchanged for that case; a cell
    # verdict on a sweep's replayed combination now reads the RIGHT combo instead of silently
    # reading whatever sits at variation_id=0 (a different, wrong combination).
    verdict_variation_id = verdict_row["variation_id"]

    # P2h (DECISIONS #302, #305; planner ruling 2026-10-29): the exam re-tests the candidate with the sizing
    # its graded run recorded, never the registry's current default. Read here, before any universe or
    # draw, so an unreadable record refuses with nothing written. Both columns NULL (an image older than
    # #376 ruling 4), or no run row, refuses too (P2j): no default sizing is used.
    with researchdb.get_conn() as conn:
        sizing_row = conn.execute(
            "SELECT sizing_method, sizing_spec FROM research_runs WHERE id = ?;", (verdict_run_id,),
        ).fetchone()
    pinned_kwargs: dict = {}
    if sizing_row is None or (sizing_row["sizing_method"] is None and sizing_row["sizing_spec"] is None):
        raise SizingNotRecordedForExam(strategy_id, verdict_run_id)  # P2j: no default sizing is used
    if sizing_row["sizing_method"] is None and sizing_row["sizing_spec"] is not None:
        # A spec with no method cannot be pinned (the method id picks the registry entry), and running it as a
        # legacy candidate would size with the registry default, not the graded spec: refused, nothing written.
        raise RecordedSizingSpecUnreadableForExam(
            strategy_id, verdict_run_id,
            f"recorded-sizing-spec-unreadable: run {verdict_run_id} recorded a sizing_spec but no sizing_method; "
            "the method is missing while the spec is set, so the exam cannot pin the sizing it was graded under")
    if sizing_row["sizing_method"] is not None:
        if sizing_row["sizing_spec"] is None:
            raise SizingSpecNotRecorded(strategy_id, verdict_run_id, sizing_row["sizing_method"])
        try:
            researchdb.parse_recorded_sizing_spec(verdict_run_id, sizing_row["sizing_spec"])
        except researchdb.RecordedSizingSpecUnreadable as exc:
            raise RecordedSizingSpecUnreadableForExam(strategy_id, verdict_run_id, str(exc)) from exc
        pinned_kwargs = {"pinned_sizing_method": sizing_row["sizing_method"],
                         "pinned_sizing_spec": sizing_row["sizing_spec"]}

    with researchdb.get_conn() as conn:
        params_row = conn.execute(
            "SELECT param_json FROM research_run_variations WHERE run_id = ? AND variation_id = ?;",
            (verdict_run_id, verdict_variation_id),
        ).fetchone()
    if params_row is None:
        raise FinalFormParamsUnrecorded(strategy_id, verdict_run_id, verdict_variation_id)
    combo = json.loads(params_row["param_json"])  # the survivor's own one recorded combo, never introspected or recomputed
    param_grid = {key: [value] for key, value in combo.items()}  # AC-24: one value per parameter, by construction
    grid_product = 1
    for values in param_grid.values():
        grid_product *= len(values)
    if grid_product > 1:  # defensive: this build never constructs a wider grid, but the refusal is named per AC-24's own words
        raise ReserveGridForbidden(strategy_id, grid_product)

    # AC-21/AC-25: the seeded draw among the unspent supply.
    window = draw_window(seed, now_ts)

    # Criterion 4: the universe, then the dispatch, through the real, UNCHANGED stage runner.
    # `now_ts` rides in the name (never only strategy_id/window.index/seed) because a genuine
    # RE-DELIVERED dispatch (AC-9's own lifecycle answer: "the same window, the same draw refused
    # by the chronology gate as a second look") must reach that gate at all: create_price_universe
    # raises PriceUniverseNameExists on a repeated name (researchdb.py's own documented 409), which
    # would otherwise fire on the SECOND call before tracker.request_draw is ever reached, masking
    # the named ChronologyRefused this lane's own criterion 9 requires with an unrelated cataloguing
    # collision. A fresh, cheap catalog row per attempt is the right cost for that: the real
    # protection against double-spending the window is the tracker's own chronology gate below,
    # never this name's own uniqueness.
    universe_name = f"reserve_exam_{strategy_id}_{window.index}_{seed}_{now_ts}"
    universe = stage_runner.build_screen_universe(
        universe_name, window.start_ts, window.end_ts, calendar_id,
    )

    # AC-22: the tracker's own draw row carries this seed and seed_purpose. dispatch_stage's own
    # six-argument call to tracker.request_draw has no room for a seventh fact and stage_runner.py
    # is frozen for this lane (stop condition, section 8), so this call-scoped context is the one
    # channel that can reach request_draw without touching that file; see tracker.py's own
    # reserve_exam_seed docstring for the full reasoning.
    with tracker.reserve_exam_seed(seed, reservedb.RESERVE_EXAM_SEED_PURPOSE):
        result = stage_runner.dispatch_stage(
            strategy_id, universe["universe_id"], "reserve", calendar_id, param_grid, now_ts,
            pinned_version=graded_version, **pinned_kwargs,
        )

    run_id = result["run_id"]
    draw_id = tracker.draw_id_for_run(run_id)  # the UNCHANGED, already-tested reader: the run_linked row dispatch_stage's own link_run just appended

    # AC-27: the lead-in's own reach, reported by name rather than refused, corrected at round 3
    # (code-quality finding B1): a fixed "one window back" under-reported the reach from window
    # index 3 upward, since stage_runner.SIZING_LEAD_IN_BARS (278) trading bars spans MORE than
    # two six-month reserve windows' worth (roughly 126 bars each), not merely one.
    # _lead_in_reach_index walks back however many enumerated windows the lead-in actually spans
    # and reports the EARLIEST reserve window index it reaches; window_months is read live from
    # the same provenance entry enumerate_windows itself reads, never a restated digit. Any bars
    # the lead-in reads BEFORE the reserve cut (window index 1's own start) are pre-cut TRAINING
    # bars, not reserve data, and this function does not record them: the reach is clamped to 1,
    # the earliest reserve window that exists.
    window_months = reservedb.read_reserve_exam_window_months()
    lead_in_reaches_window_index = _lead_in_reach_index(
        window.index, window_months, int(json.loads(sizing_row["sizing_spec"])["estimator_window"]))

    # AC-26/AC-27: the record, as its own transaction immediately following stage_runner's three
    # (see reservedb.record_exam's own docstring for why this is not literally nested inside
    # link_run's).
    # AC-26/AC-27 plus lane EF (6.1h): fitted_run_id is verdict_run_id, the SAME run this
    # function already read to admit the candidate (FinalFormVerdictAbsent/NotSurvivor) and to
    # fetch its one recorded combo (FinalFormParamsUnrecorded). Passed straight through rather
    # than re-derived, freezing the link at the moment it was actually used (this lane's own DDIA
    # note, on the DDL comment in reservedb.py): a later verdict on the same strategy must never
    # change which run a past exam's check 1 reads.
    exam_id = reservedb.record_exam(
        draw_id=draw_id,
        strategy_id=strategy_id,
        strategy_version=graded_version,
        window_index=window.index,
        window_start_ts=window.start_ts,
        window_end_ts=window.end_ts,
        seed=seed,
        lead_in_reaches_window_index=lead_in_reaches_window_index,
        run_id=run_id,
        fitted_run_id=verdict_run_id,
        created_at=now_ts,
    )
    return {
        "exam_id": exam_id,
        "run_id": run_id,
        "window_index": window.index,
        "window_start_ts": window.start_ts,
        "window_end_ts": window.end_ts,
        "lead_in_reaches_window_index": lead_in_reaches_window_index,
    }
