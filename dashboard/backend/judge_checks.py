"""The judge battery's first half (night four, lane J1, 6.1a): four pure check functions, each
built to the exact book text `docs/briefs/judge_spec_brief.md` derives it from, plus the checks'
own comment citing the folio and the PDF page the number was read from (the book protocol, #216).

This module builds checks 1 (evidence floor), 3 (trial-count correction, DSR/PSR), 4 (plateau)
and 8 (windfall, PROM). SET 5 NIGHT (lane JB, 6.1b) ADDS checks 2 (walk-forward efficiency), 5
(CSCV/PBO), 6 (Monte Carlo bounds), 7 (MCPT) and 9 (controls) below, in the same pure-function
shape: each takes a run's own already-fetched data (never opens a database connection itself
beyond the `conn` a caller passes in) and returns a `CheckResult`. Which of the nine checks can
return a real (`cleared`/`failed`) outcome as merged tonight, and why, is answered in full in this
lane's own report, `docs/units/JB/report_0.md`, not restated here.

DESIGN DECISIONS this module makes that the plan's section 8.2 did not spell out, named here
rather than left for a reader to guess (the failure-stop rule: an ambiguity is resolved and
logged, never silently invented and never hidden):

1. Degrees-of-freedom-remaining (Pardo pages 130-131, `Rdf = 100 x [1 - (Udf/Odf)]`) is read from
   the run's own record (lane DF2, `6.1e`, `#395` rulings 1 and 2, set 9a night):
   `check_1_evidence_floor` carries the signature `(run_id, conn, trades)`. Udf is
   `research_run_variations.used_degrees_of_freedom` for the graded `(run_id, variation_id=0)`
   row, written per combo by the vectorized sweep entrypoint from the strategy's own declared
   `Strategy.used_degrees_of_freedom(params)`. Odf is `research_run_funnel.bars_total` for the
   graded cell (chapter 6's own reading, the test window's bar count) minus the run's own recorded
   lead-in (`researchdb.get_run_lead_in_bars`, zero for an unsized run). A run whose strategy has
   not declared `used_degrees_of_freedom`, or whose funnel or lead-in inputs are absent, still
   reads `not-evaluable`, named by exactly which input is missing, never a guessed number.
2. Check 1's kappa is Taleb's kappa_1 = kappa(n0=1, n=2) (Definition 8.1, book page 156, Table
   8.1's own "Kappa for 2 summands, kappa_1" label, and the empirical estimator of section 10.2.3,
   equation 10.2, book page 198: `kappa(n0, n) = 2 - [log(n) - log(n0)] / log(M(n)/M(n0))`), the
   practical single-number kappa the page-159 confidence-interval callout discusses.
   A finite sample can give a negative estimate. Taleb's kappa lies in [0, 1] and is 0 for the
   Gaussian (2.2.6); it is negative only when the distribution is bimodal (8.4.4). DECISIONS #479
   item 7 reads a negative estimate as 0 (thin tails, under the 0.15 gate); the raw estimate stays
   in the quantities as `kappa_estimate`. No outcome changes.
3. Check 3's "breadth" (the spec brief's own words, "one variation across the basket members in
   one window") is read as every `row_scope = 'cell'` row research_run_cells carries for the
   given `run_id`, because the function's own signature, `(run_id, conn, thresholds)`, carries no
   `variation_id`. For the single-run grade the route builds tonight (`POST
   /api/judge/runs/{run_id}/judge`, no cell parameters in the path) this resolves to exactly the
   one cell `complete_run`'s own single-run-as-cell write leaves behind. A future sweep-grading
   caller that needs one variation among several is out of tonight's scope and is named as such.
4. Check 4's "the candidate" (the peak the neighbourhood is graded around) is the run's own
   highest-Sharpe cell among the same ticker's cells, for the same reason: no variation_id
   parameter exists to name it any other way, and Pardo's own framing, "grades the neighborhood
   rather than the peak," names the peak as the thing under test.
5. `thresholds` in `check_3_trial_count(run_id, conn, thresholds)` and
   `check_4_plateau(run_id, conn, thresholds)` is the `battery_version` string, passed straight
   into `judgedb.read_threshold`, which is that module's own existing reader over
   `research_judge_thresholds`.
6. Lane EF (`6.1h`, night `2026-09-19`, Tamer's ruling): decision 1's "the run's own record" now
   has an exception. When `run_id` names a reserve exam run (`reservedb.reserve_exam_fitted_run_id`
   finds a `research_reserve_exams` row for it), Odf and Udf are read from that row's own
   `fitted_run_id` instead, the run where the graded combo was actually fitted, never the exam's
   own short window; every other check still grades the exam run itself. An exam run whose
   `fitted_run_id` was never recorded (every exam row written before that column existed) refuses
   check 1 by name (`fitted-run-not-found`) rather than reading a wrong or absent Odf/Udf. This
   changes no other check's signature or behaviour and touches no other file this module's own
   build-gate guard (`scripts/probes/judge_quantities/ac8_...`) protects.
"""
from __future__ import annotations

import itertools  # lane C5 (set 16c, #427): compute_pbo's own C(S, S/2) combinations (AFML 11.6)
import math
import random  # used by check 7's shuffle test (Fisher-Yates reordering) and check 6's bootstrap
from dataclasses import dataclass, field
from statistics import NormalDist

from . import judgedb, researchdb, reservedb

# The three outcomes every check returns (spec brief section 2, "kept apart deliberately").
CLEARED = "cleared"
FAILED = "failed"
NOT_EVALUABLE = "not-evaluable"

# Taleb's own gate, book page 159 boxed callout, quoted in the spec brief: "Any value of kappa
# above .15 effectively indicates a high degree of unreliability of the 'normal approximation'."
_KAPPA_GATE = 0.15
# Pardo's own degrees-of-freedom floor (lane DF2, `6.1e`, chapter 13, PDF page 326, book page
# 293), quoted verbatim: "Generally, less than 90 percent remaining degrees of freedom is
# considered too few." Rdf at or above this clears; strictly below, including a negative Rdf when
# Udf exceeds Odf, fails (never clamped to zero). Not a `research_judge_thresholds` row: it is the
# book's own fixed number, the same reasoning `_KAPPA_GATE` above already follows.
_RDF_FLOOR_PERCENT = 90.0
# Pardo's trade-count caution, book page 129: below this the outcome still clears (a soft floor,
# never a reject) but carries a caveat.
_TRADE_COUNT_CAUTION = 30
# The Euler-Mascheroni constant AFML's SR* formula uses (PDF page 275), the same value the spec
# brief's own worked example uses.
_GAMMA_EULER_MASCHERONI = 0.5772156649015329


@dataclass
class CheckResult:
    """One check's own outcome, quantities and reason (plan section 8.2's own shape).

    `warnings` is additional to the plan's own three named fields, carrying check 1's caveat
    entries (spec brief, "Check 1: evidence floor", the caveat subsection): empty unless the
    check has something to say beyond its outcome.
    """

    check_id: int
    outcome: str
    quantities: dict
    reason: str
    warnings: list = field(default_factory=list)


def _taleb_kappa_1(returns: list) -> "tuple[float | None, str | None]":
    """kappa(1, 2): Definition 8.1 (book page 156) specialised to n0=1, n=2 via equation 10.2
    (book page 198): kappa(n0, n) = 2 - [log(n) - log(n0)] / log(M(n)/M(n0)), where M(n) is the
    expected mean absolute deviation from the mean of n-summed, non-overlapping blocks.

    Needs at least 2 non-overlapping pairs (4 observations) so M(2) is not trivially a single,
    zero-deviation point. Returns (kappa, None), or (None, reason) with one reason per case where
    the log ratio does not exist: too few trades, too few pairs, M(1) or M(2) zero, or M(2) equal
    to M(1) (the log of the ratio is then zero and the equation divides by it; lane DBG-7a).

    The value returned is equation 10.2's raw estimate, which a finite sample can make negative.
    Taleb's kappa lies in [0, 1], 0 for the Gaussian (2.2.6), negative only when bimodal (8.4.4);
    check 1 reads a negative estimate as 0 (DECISIONS #479 item 7).
    """
    n = len(returns)
    if n < 4:
        return None, "fewer than 4 trades"
    mean1 = sum(returns) / n
    m1 = sum(abs(r - mean1) for r in returns) / n
    pairs = [returns[i] + returns[i + 1] for i in range(0, n - 1, 2)]  # non-overlapping consecutive pairs
    if len(pairs) < 2:
        return None, "fewer than 2 non-overlapping pairs"
    mean2 = sum(pairs) / len(pairs)
    m2 = sum(abs(p - mean2) for p in pairs) / len(pairs)
    if m1 <= 0.0 or m2 <= 0.0:
        return None, "M(1) or M(2) is zero (a degenerate series)"
    log_ratio = math.log(m2 / m1)
    if log_ratio == 0.0:
        return None, "M(2) equals M(1), so equation 10.2's log ratio is zero"
    return 2.0 - math.log(2.0) / log_ratio, None


# The fitted-run-not-found monitor (lane EF, 6.1h, docs/plans/night/2026-09-19/PLAN.md section 7's
# ops routing law: every declared failure mode ships its counter). Every reserve exam run graded
# before its own fitted_run_id column existed, or dispatched by a caller that never recorded one,
# turns its WHOLE verdict unjudgeable through this one refusal; this counter makes that visible as
# a number rather than a silent drop in the pass rate nobody can trace to a cause. Expected to
# decay toward zero as pre-migration exam rows are superseded by new ones carrying the link;
# nonzero readings after that point name a real, ongoing linkage gap worth investigating. See
# docs/units/EF/monitor_rows.md for the registry wiring this counter feeds, mirroring
# RESAMPLING_MONITOR_COUNTERS's own module-level, process-local, reset-on-restart shape below.
CHECK1_FITTED_RUN_MONITOR_COUNTERS = {"fitted_run_not_found_count": 0}

# W-2, each written as a literal EXACTLY ONCE (JC-17): the dict below and every return site in
# routes/judge.py's _resolve_check2_walkforward_run, plus check_3_trial_count's own short-circuit,
# all reference these constants rather than retyping the strings.
WORD_WALKFORWARD_RUN_NOT_FOUND = "walkforward-run-not-found"
WORD_WALKFORWARD_RUN_AMBIGUOUS = "walkforward-run-ambiguous"
WORD_WALKFORWARD_LINK_REFUSED = "walkforward-link-refused"
WORD_TRIAL_COUNT_SCOPE_OWED = "trial-count-scope-owed"

# Lane JC (candidate chain link spec, section 7 row B-7): one key per W-2 word, incremented by
# routes/judge.py wherever _resolve_check2_walkforward_run returns one of the three check-2 link
# words, and by check_3_trial_count's own short-circuit below for trial-count-scope-owed. Declared
# here, beside CHECK1_FITTED_RUN_MONITOR_COUNTERS, per docs/units/JC/monitor_rows.md.
JC_CHECK2_CHECK3_MONITOR_COUNTERS = {
    WORD_WALKFORWARD_RUN_NOT_FOUND: 0,
    WORD_WALKFORWARD_RUN_AMBIGUOUS: 0,
    WORD_WALKFORWARD_LINK_REFUSED: 0,
    WORD_TRIAL_COUNT_SCOPE_OWED: 0,
}


def check_1_evidence_floor(run_id: int, conn, trades, *, ticker: str | None = None,
                            variation_id: int = 0) -> CheckResult:
    """The evidence floor (spec brief, "Check 1: evidence floor", lines 560-671): coordinate A of
    the composition rule, an unconditional gate. Reads trade count and kappa from `trades` (a
    DataFrame in the trades.parquet shape, `research_lab/strategies/base/vbt_columns.py`'s
    `CANONICAL_TRADE_COLUMNS`); past kappa, reads the run's own degrees-of-freedom record (lane
    DF2, `6.1e`, `#395` rulings 1 and 2, module docstring decision 1).

    Odf, Pardo's own denominator (chapter 6, PDF page 163, book page 130, quoted verbatim on the
    shelf: "Assume a trading strategy uses a single moving average with a length of 30 days and a
    test window size of 100 days. It is said that one degree of freedom is consumed by each data
    point that is used in its calculation." And: "Rdf = 100 x [1 - (Udf/Odf)]"):
    `research_run_funnel.bars_total` for the graded `(run_id, variation_id=0, row_scope='cell')`
    row, minus the run's own recorded lead-in (`researchdb.get_run_lead_in_bars`, zero for an
    unsized run, `#395` ruling 1: the lead-in bars are by construction never traded, so counting
    them would make a run look better tested than it was). Udf:
    `research_run_variations.used_degrees_of_freedom` for the same graded combo, written by the
    strategy's own declared `used_degrees_of_freedom(params)` at sweep time. `Rdf = 100 x (1 -
    Udf/Odf)` against `_RDF_FLOOR_PERCENT` (chapter 13, PDF page 326, book page 293, quoted
    verbatim: "Generally, less than 90 percent remaining degrees of freedom is considered too
    few"): `cleared` at or above the floor, `failed` strictly below it, including a negative Rdf
    when Udf exceeds Odf, never clamped to zero.

    Fold items 2 and 3 (code-quality pass 1, lane DF2 round 2). `ticker` is keyword-only and
    defaults to `None`. research_run_funnel's own primary key is `(run_id, variation_id, ticker,
    row_scope)`, so a multi-ticker run carries one funnel row per ticker: pass the graded cell's
    own ticker (routes/judge.py already resolves it beside variation_id, from research_run_cells)
    to read exactly that ticker's row. `ticker=None` means "no ticker was resolved for this call":
    the query still carries no ticker filter, but it now fetches every matching row instead of an
    arbitrary one, and refuses `not-evaluable` named `bars-total-ambiguous` when more than one row
    comes back, naming the row count and the tickers, rather than silently grading whichever row
    SQLite happens to return first; exactly one matching row still grades exactly as before.
    `quantities` carries `ticker` whenever the caller passed one in. The lead-in read
    (`researchdb.get_run_lead_in_bars`) now takes `conn=conn`, so it rides this function's own
    connection instead of opening a second one, which is what keeps this module's own docstring
    promise, that no check opens a connection beyond the one it is passed, true in fact and not
    only in words.

    Lane EF (6.1h, docs/plans/night/2026-09-19/PLAN.md section 7, Tamer's ruling 2026-09-19): when
    run_id is a reserve exam run, Odf and Udf are read from its FITTED run's own record (the run
    where the parameters were fitted), never from the exam's own short window; every other check
    still grades the exam run itself. `reservedb.reserve_exam_fitted_run_id(conn, run_id)` tells an
    ordinary run apart from an exam run; an exam run whose fitted run was never recorded refuses
    this check by name (`fitted-run-not-found`) rather than skipping it, which alone makes the
    whole verdict `unjudgeable` (`judgedb.compose_verdict`, UNTOUCHED by this lane: coordinate A
    not `cleared` always composes to `unjudgeable`, "silence from a check is never consent").

    Lane JC (candidate chain link spec, L-5 row C1): `variation_id` is keyword-only and defaults
    to `0`, so every caller written before this lane (which never passed it) grades the same
    single cube cell as before. The judge route now passes the REPLAYED cell's own variation_id
    for a cell grade, so Odf and Udf are read for the combination that was actually replayed and
    graded, never an assumed variation 0.

    Lane DBG-7b (DECISIONS #479 item 7): a finite sample can give a negative kappa estimate.
    Taleb's kappa lies in [0, 1], 0 for the Gaussian (2.2.6), negative only when bimodal (8.4.4),
    so the gate reads `max(estimate, 0)`. `quantities["kappa"]` is that read value; a negative
    estimate is kept as `quantities["kappa_estimate"]`. No outcome changes.
    """
    # Lane EF: resolve which run's own record grades Odf/Udf BEFORE anything else runs, so this
    # refusal is never shadowed by a coincidental trades/kappa issue below it (both would compose
    # to the same unjudgeable verdict either way, but naming the real reason matters for a human
    # reading the stored check_outcomes_json later).
    is_exam_run, fitted_run_id = reservedb.reserve_exam_fitted_run_id(conn, run_id)
    if is_exam_run and fitted_run_id is None:
        # Tamer's own refusal condition: an exam run whose fitted run cannot be found refuses BY
        # NAME rather than skipping the check quietly. Every exam row written before reservedb's
        # own fitted_run_id column existed reads NULL here, so every one of them grades this way
        # from the day this merges (this lane's own t-zero lifecycle answer). Counted live so the
        # ops routing law's own promise holds: a declared failure mode is visible, never silent.
        CHECK1_FITTED_RUN_MONITOR_COUNTERS["fitted_run_not_found_count"] += 1
        return CheckResult(
            1, NOT_EVALUABLE, {},
            f"fitted-run-not-found: reserve exam run {run_id} carries no fitted_run_id; the "
            "evidence floor cannot be read from an exam whose fitted run link is unresolved",
        )
    # dof_run_id is the run whose funnel/sizing/variation rows Odf and Udf are read from below:
    # run_id itself for an ordinary run (is_exam_run False, unchanged path), or the fitted run for
    # an admitted exam run (is_exam_run True, fitted_run_id already proven not None above).
    dof_run_id = fitted_run_id if is_exam_run else run_id

    if trades is None or len(trades) == 0:
        return CheckResult(1, NOT_EVALUABLE, {}, "trade_count is the missing input: the trade record is empty")

    trade_count = int(len(trades))
    returns = [float(r) for r in trades["return_pct"].tolist() if r is not None]
    kappa, kappa_reason = _taleb_kappa_1(returns)
    if kappa is None:
        return CheckResult(
            1, NOT_EVALUABLE, {"trade_count": trade_count},
            f"kappa is the missing input: {kappa_reason}",
        )

    kappa_read = 0.0 if kappa < 0.0 else kappa  # #479 item 7: a negative estimate reads as 0
    quantities = {"trade_count": trade_count, "kappa": kappa_read}
    if kappa < 0.0:
        quantities["kappa_estimate"] = kappa
    if ticker is not None:  # fold item 2: name which ticker's row this grade reads, when one was resolved
        quantities["ticker"] = ticker
    if is_exam_run:  # lane EF: name which run's own record graded Odf/Udf, for a later reader's audit trail
        quantities["fitted_run_id"] = dof_run_id
    # Taleb's own gate, book page 159: "Any value of kappa above .15 ... a high degree of
    # unreliability." A hard gate, independent of degrees-of-freedom-remaining (the derivation's
    # own OR: either hard gate alone fails the floor).
    if kappa_read > _KAPPA_GATE:
        return CheckResult(
            1, FAILED, quantities,
            f"kappa {kappa_read:.4f} exceeds Taleb's 0.15 gate (Statistical Consequences of Fat "
            f"Tails, book page 159)",
        )

    # Odf's own bar count: the graded cell's own funnel row. variation_id=0 is the ONE cube cell
    # complete_run's own single-run write leaves behind, and the one routes/judge.py always grades
    # (its own comment: "this is where this route reads the (variation_id, ticker) grain the
    # verdict row itself is keyed on"). research_run_funnel's own primary key also carries ticker
    # (fold item 2): with a ticker the query filters on it and reads exactly one row by construction;
    # with ticker=None it still carries no ticker filter, so it fetches EVERY matching row rather than
    # `.fetchone()`'s arbitrary first one, and more than one row is refused below rather than graded
    # blind. A missing or zero-bar funnel row means the test window's own bar count is not derivable:
    # not-evaluable, never a division-by-zero crash below.
    #
    # Lane EF: `ticker` is the EXAM run's own graded cell (routes/judge.py resolves it from run_id,
    # not from dof_run_id), which carries no guarantee of appearing on a DIFFERENT run's (the
    # fitted run's) funnel at all -- a sweep-graded fitted run can hold many tickers, none of which
    # need match the reserve window's own universe. So the ticker filter applies only while still
    # reading run_id's OWN record (dof_run_id == run_id, the unchanged path); redirected reads use
    # no ticker filter and rely on the ambiguity guard below exactly as an untickered call already
    # does, rather than this function inventing a cross-run ticker-matching rule the brief never
    # specified.
    funnel_ticker = ticker if dof_run_id == run_id else None
    if funnel_ticker is not None:
        funnel_rows = conn.execute(
            "SELECT bars_total, lead_in_bars, ticker FROM research_run_funnel"
            " WHERE run_id = ? AND variation_id = ? AND row_scope = 'cell' AND ticker = ?;",
            (dof_run_id, variation_id, funnel_ticker),
        ).fetchall()
    else:
        funnel_rows = conn.execute(
            "SELECT bars_total, lead_in_bars, ticker FROM research_run_funnel"
            " WHERE run_id = ? AND variation_id = ? AND row_scope = 'cell';",
            (dof_run_id, variation_id),
        ).fetchall()
    if len(funnel_rows) == 0 or funnel_rows[0]["bars_total"] is None:
        return CheckResult(
            1, NOT_EVALUABLE, quantities,
            "bars-total-absent: research_run_funnel carries no graded cell row for this run, so "
            "Odf's own test-window bar count is not derivable",
        )
    if len(funnel_rows) > 1:
        # No ticker was given and more than one funnel row matched: grading any one of them would be
        # arbitrary (the old `.fetchone()` defect this fold fixes), so refuse by name instead, naming
        # the count and every ticker seen so a caller can re-dispatch with the right one.
        seen_tickers = sorted({row["ticker"] for row in funnel_rows})
        return CheckResult(
            1, NOT_EVALUABLE, quantities,
            f"bars-total-ambiguous: {len(funnel_rows)} research_run_funnel rows match run_id="
            f"{dof_run_id} with no ticker to choose among them (tickers: {', '.join(seen_tickers)})",
        )
    bars_total = int(funnel_rows[0]["bars_total"])

    # The lead-in (#395 ruling 1): zero for an unsized run (research_runs.sizing_method IS NULL,
    # trading starts at bar zero, nothing to subtract); the run's own recorded
    # sizing_spec.estimator_window for a sized one. A sized run with no recorded sizing_spec
    # refuses rather than guessing the sizing registry's CURRENT default, which this run may not
    # actually have dispatched with. Lane EF: reads dof_run_id (the fitted run for an exam run,
    # run_id itself otherwise), since the lead-in is a property of how the GRADED combo was sized
    # when it was fitted, not of the exam window that later re-ran it.
    run_row = conn.execute("SELECT sizing_method FROM research_runs WHERE id = ?;", (dof_run_id,)).fetchone()
    if run_row is None:
        return CheckResult(1, NOT_EVALUABLE, quantities, f"run {dof_run_id} not found")
    recorded_lead_in = funnel_rows[0]["lead_in_bars"]  # P2g: the lead-in the run itself recorded; NULL for a run stored before P2g
    if recorded_lead_in is not None:
        lead_in_bars = int(recorded_lead_in)  # the bars its clip actually led in, never the sizing spec's guess
    elif run_row["sizing_method"] is None:
        lead_in_bars = 0  # unsized: nothing traded before window_start_ts, so nothing to subtract
    else:
        try:
            # Fold item 3: pass this function's own connection through so the read rides it instead
            # of opening a second one (the module docstring's promise, kept in fact now, not just in words).
            lead_in_bars = researchdb.get_run_lead_in_bars(dof_run_id, conn=conn)
        except researchdb.SizingSpecNotRecordedError:
            return CheckResult(
                1, NOT_EVALUABLE, quantities,
                "bars-total-absent: this sized run's own sizing_spec is NULL, so the lead-in bar "
                "count cannot be read and a guessed default is refused",
            )

    odf = bars_total - lead_in_bars
    quantities.update({"bars_total": bars_total, "lead_in_bars": lead_in_bars, "odf": odf})
    if odf <= 0:
        return CheckResult(
            1, NOT_EVALUABLE, quantities,
            "bars-total-absent: the test window's own bar count (bars_total minus the lead-in) "
            "is zero or negative",
        )

    # Udf: the graded combo's own recorded used_degrees_of_freedom. NULL/absent means the
    # strategy never declared the contract method, or this run predates this lane's migration.
    # Lane EF: dof_run_id again, the same reasoning as the funnel read above.
    variation_row = conn.execute(
        "SELECT used_degrees_of_freedom, lookback_bars FROM research_run_variations "
        " WHERE run_id = ? AND variation_id = ?;",
        (dof_run_id, variation_id),
    ).fetchone()
    if variation_row is None or variation_row["used_degrees_of_freedom"] is None:
        return CheckResult(
            1, NOT_EVALUABLE, quantities,
            "used-degrees-of-freedom-undeclared: research_run_variations carries no recorded "
            "used_degrees_of_freedom for this run's graded combo",
        )
    udf = int(variation_row["used_degrees_of_freedom"])
    lookback_bars = variation_row["lookback_bars"]
    quantities.update({"udf": udf, "lookback_bars": lookback_bars})

    rdf_percent = 100.0 * (1.0 - (udf / odf))
    quantities["rdf_percent"] = rdf_percent
    # The reading word: which chapter's Odf definition graded this run (#395 ruling 1, chapter 6,
    # the test window's own bar count), so a later reader can redo the arithmetic without
    # re-deriving which denominator this build used.
    quantities["reading"] = "test-window"

    if rdf_percent < _RDF_FLOOR_PERCENT:
        return CheckResult(
            1, FAILED, quantities,
            f"Rdf {rdf_percent:.1f} percent falls below Pardo's 90 percent floor (Udf={udf}, "
            f"Odf={odf}, Degrees of Freedom, Sample Size and Startup Overhead, book page 293)",
        )

    warnings = []
    if trade_count < _TRADE_COUNT_CAUTION:
        # Pardo's own soft floor, book page 129: "extra caution", never "reject". The outcome
        # still clears; the caveat rides along so a reader knows the sample is thin.
        warnings.append({
            "code": "thin-trade-count-caveat",
            "detail": f"{trade_count} trades is below Pardo's 30-trade caution (book page 129); "
                      f"the evidence floor still clears, read the result with extra caution",
        })
    return CheckResult(
        1, CLEARED, quantities,
        f"Rdf {rdf_percent:.1f} percent clears Pardo's 90 percent floor (Udf={udf}, Odf={odf})",
        warnings=warnings,
    )


def _check_3_family(run_id: int, conn, battery_version: str, variation_id: "int | None",
                    ticker: "str | None") -> CheckResult:
    """Check 3 for a generator v2 group run (lane GV2-5c, DECISIONS #445 item 2): DSR against the effective number of
    trials K (k_effective) and the variance V of the trial family the search recorded, instead of a raw row count.

    Same formulas as check_3_trial_count (AFML p. 274 for PSR, p. 275 for DSR), with N = K and sqrt(V) = sqrt(v) from the
    family row (AFML p. 275: V is the variance across the trials' Sharpe ratios, N the number of independent trials;
    Lopez de Prado and Lewis 2018 sections 8.1 and 8.2 give K and V from the ONC clustering). SR_hat, skew, kurtosis and T
    come from the graded cell alone (variation_id and the group label in `ticker`). The family is found by
    researchdb.family_for(member_set_sha256, screen window start, screen window end): the member set sha is the replay's
    member_dataset_sha256, the window is the search's screen window (the granted screen draw of the optimise draw's
    strategy, version and calendar).
    """
    if variation_id is None or ticker is None:
        return CheckResult(3, NOT_EVALUABLE, {}, "graded-cell-absent: a theory stage run is graded at one cell (variation id "
                                                "and group label), none was given")
    threshold = judgedb.read_threshold(conn, 3, "dsr_pass_bar", battery_version)
    if threshold is None:
        return CheckResult(3, NOT_EVALUABLE, {}, "dsr_pass_bar is the missing input: not frozen for this battery_version")
    dsr_bar = threshold["value_numeric"]
    run_row = conn.execute("SELECT annualization_factor FROM research_runs WHERE id = ?;", (run_id,)).fetchone()
    if run_row is None:
        return CheckResult(3, NOT_EVALUABLE, {}, f"run {run_id} not found")
    ann_factor = run_row["annualization_factor"]
    if ann_factor is None or ann_factor <= 0.0:
        return CheckResult(
            3, NOT_EVALUABLE, {},
            "annualization_factor is the missing input: research_runs carries no positive observed-bars-per-year factor "
            "for this run, so the stored Sharpe cannot be de-annualised into the book's own original sampling frequency")

    cache = researchdb.get_full_metrics_cache_by_cell(run_id, variation_id, ticker)
    replay = ((cache or {}).get("payload") or {}).get("replay")
    member_sha = replay.get("member_dataset_sha256") if isinstance(replay, dict) else None
    if not member_sha:
        return CheckResult(3, NOT_EVALUABLE, {}, "member_dataset_sha256 is the missing input: the cell's replay carries no "
                                                "member set sha256, so its trial family cannot be found")
    screen_rows = conn.execute(
        "SELECT DISTINCT s.window_start_ts AS window_start_ts, s.window_end_ts AS window_end_ts "
        "FROM research_data_draws rl "
        "JOIN research_data_draws o ON o.id = rl.draw_id AND o.event_kind = 'draw_requested' AND o.stage = 'optimise' "
        "JOIN research_data_draws s ON s.event_kind = 'draw_requested' AND s.stage = 'screen' AND s.decision = 'granted' "
        "  AND s.strategy_id = o.strategy_id AND s.strategy_version = o.strategy_version AND s.calendar_id = o.calendar_id "
        "WHERE rl.event_kind = 'run_linked' AND rl.run_id = ? AND rl.decision = 'granted';",
        (run_id,)).fetchall()
    if len(screen_rows) != 1:
        return CheckResult(
            3, NOT_EVALUABLE, {"screen_windows": len(screen_rows)},
            "screen-window-unresolved: the run's optimise draw has " +
            ("no granted screen draw" if not screen_rows else "granted screen draws with different windows") +
            " of the same strategy, version and calendar, so the trial family key is not known")
    start_ts, end_ts = screen_rows[0]["window_start_ts"], screen_rows[0]["window_end_ts"]
    try:
        family = researchdb.family_for(member_sha, start_ts, end_ts)
    except researchdb.FamilyUnionPending as pending:
        # call 37, AFML p. 275: several theories searched this data, so N, K and V are the merged family's, not one theory's
        if pending.job_id is not None:
            return CheckResult(3, NOT_EVALUABLE, {"member_set_sha256": member_sha},
                               f"family-union-failed: job {pending.job_id}: {pending.error}")
        return CheckResult(3, NOT_EVALUABLE, {"member_set_sha256": member_sha},
                           f"family-union-pending: {len(pending.theories)} theories searched this data and "
                           f"their merged family is not computed yet")
    if family is None:
        return CheckResult(3, NOT_EVALUABLE, {"member_set_sha256": member_sha},
                            "trial family is the missing input: no search recorded a family on this member set and screen "
                            "window, so the trial-multiplicity correction is not computable")
    quantities = {("union_id" if family.get("union") else "family_id"): family["union_id" if family.get("union") else "id"],
                  "n_trials": family["n_trials"], "k_effective": family["k_effective"], "v": family["v"]}
    if family["reason"] is not None:
        return CheckResult(3, NOT_EVALUABLE, quantities,
                            f"{family['reason']}: the search tried too few strategies to cluster, so K and V are not known")
    k, v = family["k_effective"], family["v"]
    if k is None or k < 2:
        return CheckResult(3, NOT_EVALUABLE, quantities,
                            f"k_effective is {k}, below 2: the trial-multiplicity correction needs at least 2 independent trials")
    if v is None or not v > 0.0:
        return CheckResult(3, NOT_EVALUABLE, quantities,
                            f"v is {v}, not positive: the variance across the family's trials is needed for the benchmark")

    cell = conn.execute(
        "SELECT sharpe_ratio, skew, kurtosis FROM research_run_cells "
        " WHERE run_id = ? AND variation_id = ? AND ticker = ? AND row_scope = 'cell';",
        (run_id, variation_id, ticker)).fetchone()
    if cell is None or cell["sharpe_ratio"] is None or cell["skew"] is None or cell["kurtosis"] is None:
        return CheckResult(3, NOT_EVALUABLE, quantities,
                            "research_run_cells is the missing input: the graded cell carries no Sharpe, skew and kurtosis together")
    funnel = conn.execute(
        "SELECT bars_after_warmup FROM research_run_funnel "
        " WHERE run_id = ? AND variation_id = ? AND ticker = ? AND row_scope = 'cell';",
        (run_id, variation_id, ticker)).fetchone()
    if funnel is None or funnel["bars_after_warmup"] is None:
        return CheckResult(3, NOT_EVALUABLE, quantities,
                            "bars_after_warmup is the missing input: research_run_funnel carries no row for the graded cell")
    sr_hat_annualized = cell["sharpe_ratio"]
    sr_hat = sr_hat_annualized / math.sqrt(ann_factor)
    gamma3 = cell["skew"]
    gamma4 = cell["kurtosis"] + 3.0
    t = funnel["bars_after_warmup"]
    if t < 2:
        return CheckResult(3, NOT_EVALUABLE, quantities, "bar-return count too small (T < 2) to compute a standard error")
    z = NormalDist()
    sr_star = math.sqrt(v) * (
        (1.0 - _GAMMA_EULER_MASCHERONI) * z.inv_cdf(1.0 - 1.0 / k)
        + _GAMMA_EULER_MASCHERONI * z.inv_cdf(1.0 - (1.0 / k) * math.exp(-1.0)))
    denom = 1.0 - gamma3 * sr_hat + ((gamma4 - 1.0) / 4.0) * sr_hat ** 2
    if denom <= 0.0:
        return CheckResult(3, NOT_EVALUABLE, quantities,
                            "PSR's own denominator is not positive for this candidate's skew, kurtosis "
                            "and Sharpe together; DSR is not computable")
    psr_stat = (sr_hat - sr_star) * math.sqrt(t - 1.0) / math.sqrt(denom)
    dsr = z.cdf(psr_stat)
    quantities.update({"annualization_factor": ann_factor, "sr_hat_annualized": sr_hat_annualized, "sr_hat": sr_hat,
                       "gamma3": gamma3, "gamma4": gamma4, "t": t, "sr_star": sr_star, "psr_statistic": psr_stat,
                       "dsr": dsr, "dsr_pass_bar": dsr_bar})
    if dsr > dsr_bar:
        return CheckResult(3, CLEARED, quantities, f"DSR {dsr:.4f} exceeds the {dsr_bar} bar (AFML 14.7.4, K={k} trials from the family)")
    return CheckResult(3, FAILED, quantities, f"DSR {dsr:.4f} does not exceed the {dsr_bar} bar (AFML 14.7.4, K={k} trials from the family)")


def check_3_trial_count(run_id: int, conn, battery_version: str, *,
                         variation_id: int | None = None, ticker: "str | None" = None) -> CheckResult:
    """Trial-count correction, DSR against the frozen 0.95 bar (spec brief section 3, "Check 3,
    the deflated Sharpe ratio (DSR) and its rider, the probabilistic Sharpe ratio (PSR)").

    PSR[SR*] = Z[(SR_hat - SR*) * sqrt(T-1) / sqrt(1 - gamma3*SR_hat + ((gamma4-1)/4)*SR_hat^2)]
    (AFML PDF page 274), DSR = PSR[SR*] with SR* the trial-multiplicity benchmark (AFML PDF page
    275): SR* = sqrt(V) * ((1-gamma_EM)*Phi^-1[1-1/N] + gamma_EM*Phi^-1[1-(1/N)*e^-1]), sqrt(V)
    approximated by the same per-trial standard error PSR's own denominator uses under the null
    (spec brief section 3, step 1/2: "sqrt(V) is the step-2 standard error").

    Stored kurtosis (research_run_cells.kurtosis) is EXCESS kurtosis (the schema's own comment);
    AFML's gamma4 is RAW kurtosis (Gaussian = 3), so gamma4 = stored_kurtosis + 3.0 here.

    UNITS (fixed in the read_1 fold, J1-R1-B1). The book's own sentence, quoted verbatim, spec
    brief lines 928-933 (PDF page 274): "T is the number of observed returns ... For a given SR*,
    PSR increases with greater SR (in the original sampling frequency, i.e. non-annualized)". So
    SR_hat here MUST be the per-bar Sharpe, never the annualised one, and T MUST be a bar-return
    count, never a trade count. `research_run_cells.sharpe_ratio` is vbt's `acc.sharpe_ratio()`
    under the run's own `year_freq` (ANNUALISED; `full_metrics.py` line 198's own call, copied into
    the cell verbatim by the sweep entrypoint's `_run_result_cell`), so it is de-annualised here by
    dividing by `sqrt(annualization_factor)` (`research_runs.annualization_factor`, the same
    observed-bars-per-year number vbt's own `get_ann_factor` used to inflate it in the first
    place). T is read from `research_run_funnel.bars_after_warmup` for the same cells (the count of
    bars carrying a defined return, the funnel's own definition), never from `num_trades`, which is
    a different and much smaller count. A run with no stored annualization_factor (an event or
    legacy run) or no funnel rows (a bespoke strategy that skips the shared portfolio helper,
    `run_sweep_entrypoint.py`'s own comment on `_run_result_cell`) cannot be de-annualised or bar-
    counted honestly, so this returns not-evaluable naming the missing input rather than assuming a
    factor of 1 or falling back to the trade count.
    """
    job_kind = conn.execute(
        "SELECT j.kind AS kind FROM research_runs r JOIN research_jobs j ON r.job_id = j.id WHERE r.id = ?;",
        (run_id,)).fetchone()
    if job_kind is not None and job_kind["kind"] == researchdb.JOB_KIND_RUN_THEORY_STAGE:
        return _check_3_family(run_id, conn, battery_version, variation_id, ticker)
    if variation_id is not None:
        # Lane JC (candidate chain link spec, section 3's own finding): called with a sweep's
        # replayed cell, this check's own trial count reads research_runs.dataset_sha256, which
        # for a sweep is the DECLARATION fingerprint, never the member content sha the sweep's own
        # trial rows carry (dispatch_sweep vs complete_sweep_chunk). Computing DSR against that
        # mismatched scope would always read "fewer than 2 trial rows", a reason that blames the
        # ledger when the fault is the scope. The correlated-basket trial-count scheme itself is
        # OWED to the judge brief (section 3), so this refuses by name rather than inventing one.
        JC_CHECK2_CHECK3_MONITOR_COUNTERS[WORD_TRIAL_COUNT_SCOPE_OWED] += 1
        return CheckResult(
            3, NOT_EVALUABLE, {"variation_id": variation_id},
            WORD_TRIAL_COUNT_SCOPE_OWED,
        )
    threshold = judgedb.read_threshold(conn, 3, "dsr_pass_bar", battery_version)
    if threshold is None:
        return CheckResult(3, NOT_EVALUABLE, {}, "dsr_pass_bar is the missing input: not frozen for this battery_version")
    dsr_bar = threshold["value_numeric"]

    run_row = conn.execute(
        "SELECT dataset_sha256, strategy_id, annualization_factor FROM research_runs WHERE id = ?;",
        (run_id,),
    ).fetchone()
    if run_row is None:
        return CheckResult(3, NOT_EVALUABLE, {}, f"run {run_id} not found")

    ann_factor = run_row["annualization_factor"]
    if ann_factor is None or ann_factor <= 0.0:
        return CheckResult(
            3, NOT_EVALUABLE, {},
            "annualization_factor is the missing input: research_runs carries no positive "
            "observed-bars-per-year factor for this run (an event or legacy run stores NULL "
            "here), so the stored Sharpe cannot be de-annualised into the book's own original "
            "sampling frequency",
        )

    n_trials = conn.execute(
        "SELECT COUNT(*) AS n FROM research_trials WHERE dataset_sha256 = ? AND strategy_id = ?;",
        (run_row["dataset_sha256"], run_row["strategy_id"]),
    ).fetchone()["n"]
    if n_trials < 2:
        return CheckResult(3, NOT_EVALUABLE, {"n_trials": n_trials},
                            "research_trials is the missing input: fewer than 2 trial rows for this "
                            "dataset/strategy, so the trial-multiplicity correction is not computable")

    cells = conn.execute(
        "SELECT sharpe_ratio, skew, kurtosis FROM research_run_cells "
        " WHERE run_id = ? AND row_scope = 'cell';",
        (run_id,),
    ).fetchall()
    sharpes = [c["sharpe_ratio"] for c in cells if c["sharpe_ratio"] is not None]
    skews = [c["skew"] for c in cells if c["skew"] is not None]
    kurts = [c["kurtosis"] for c in cells if c["kurtosis"] is not None]
    if not sharpes or not skews or not kurts:
        return CheckResult(
            3, NOT_EVALUABLE, {},
            "research_run_cells is the missing input: no member's row carries a Sharpe, skew and "
            "kurtosis together (skew/kurtosis are only written by a sweep cell, spec brief section "
            "3's own basket-breadth requirement)",
        )

    # T, the book's own "number of observed returns" in the ORIGINAL frequency: the funnel's own
    # bar-after-warmup count for the same breadth of cells, never a trade count.
    funnel_rows = conn.execute(
        "SELECT bars_after_warmup FROM research_run_funnel WHERE run_id = ? AND row_scope = 'cell';",
        (run_id,),
    ).fetchall()
    bar_counts = [f["bars_after_warmup"] for f in funnel_rows if f["bars_after_warmup"] is not None]
    if not bar_counts:
        return CheckResult(
            3, NOT_EVALUABLE, {},
            "bars_after_warmup is the missing input: research_run_funnel carries no cell row for "
            "this run (a bespoke strategy that bypasses the shared portfolio helper writes no "
            "funnel row), so the observed-return count is not derivable",
        )

    sr_hat_annualized = sum(sharpes) / len(sharpes)
    sr_hat = sr_hat_annualized / math.sqrt(ann_factor)  # de-annualise: vbt's own sqrt(factor) scaling, inverted
    gamma3 = sum(skews) / len(skews)
    gamma4 = (sum(kurts) / len(kurts)) + 3.0  # excess -> raw
    t = sum(bar_counts) / len(bar_counts)
    if t < 2:
        return CheckResult(3, NOT_EVALUABLE, {}, "bar-return count too small (T < 2) to compute a standard error")

    z = NormalDist()
    sqrt_v = 1.0 / math.sqrt(t - 1.0)  # the trial's own standard error (spec brief section 3, step 1/2)
    sr_star = sqrt_v * (
        (1.0 - _GAMMA_EULER_MASCHERONI) * z.inv_cdf(1.0 - 1.0 / n_trials)
        + _GAMMA_EULER_MASCHERONI * z.inv_cdf(1.0 - (1.0 / n_trials) * math.exp(-1.0))
    )
    denom = 1.0 - gamma3 * sr_hat + ((gamma4 - 1.0) / 4.0) * sr_hat ** 2
    if denom <= 0.0:
        return CheckResult(3, NOT_EVALUABLE, {},
                            "PSR's own denominator is not positive for this candidate's skew, kurtosis "
                            "and Sharpe together; DSR is not computable")
    psr_stat = (sr_hat - sr_star) * math.sqrt(t - 1.0) / math.sqrt(denom)
    dsr = z.cdf(psr_stat)

    quantities = {"n_trials": n_trials, "annualization_factor": ann_factor,
                  "sr_hat_annualized": sr_hat_annualized, "sr_hat": sr_hat, "gamma3": gamma3,
                  "gamma4": gamma4, "t": t, "sr_star": sr_star, "dsr": dsr, "dsr_pass_bar": dsr_bar}
    if dsr > dsr_bar:
        return CheckResult(3, CLEARED, quantities, f"DSR {dsr:.4f} exceeds the {dsr_bar} bar (AFML 14.7.4)")
    return CheckResult(3, FAILED, quantities, f"DSR {dsr:.4f} does not exceed the {dsr_bar} bar (AFML 14.7.4)")


def check_4_plateau(run_id: int, conn, battery_version: str, *,
                     centre: "tuple[int, str] | None" = None) -> CheckResult:
    """The plateau check (spec brief, checks 4/8 subsection; Pardo chapter 10, page 226/230):
    grades the neighbourhood around the run's own peak-Sharpe cell rather than the peak alone.
    Neighbourhood = `plateau_bandwidth` cells on each side of the peak's own variation_id, same
    ticker, ordered by variation_id (module docstring, decision 4).

    Lane JC (candidate chain link spec, L-5 row C4): `centre` is keyword-only, `None` by default
    (every existing caller unchanged). When given as `(variation_id, ticker)`, the neighbourhood
    centres on THAT cell -- the one the judge actually replayed and graded -- rather than on
    whichever cell happens to carry the run's own highest Sharpe. A centre naming a cell absent
    from this run's own graded cells is not-evaluable, named.
    """
    bandwidth_row = judgedb.read_threshold(conn, 4, "plateau_bandwidth", battery_version)
    if bandwidth_row is None:
        return CheckResult(4, NOT_EVALUABLE, {},
                            "plateau_bandwidth is the missing input: not frozen for this battery_version "
                            "(judge_spec_brief.md section 7)")
    flatness_row = judgedb.read_threshold(conn, 4, "plateau_flatness_bar", battery_version)
    if flatness_row is None:
        return CheckResult(4, NOT_EVALUABLE, {}, "plateau_flatness_bar is the missing input: not frozen for this battery_version")

    bandwidth = int(bandwidth_row["value_numeric"])
    flatness_bar = flatness_row["value_numeric"]

    cells = conn.execute(
        "SELECT variation_id, ticker, sharpe_ratio FROM research_run_cells "
        " WHERE run_id = ? AND row_scope = 'cell' AND sharpe_ratio IS NOT NULL;",
        (run_id,),
    ).fetchall()
    if not cells:
        return CheckResult(4, NOT_EVALUABLE, {}, "research_run_cells is the missing input: no cell carries a Sharpe ratio for this run")

    if centre is not None:
        centre_variation_id, centre_ticker = centre
        same_ticker = sorted((c for c in cells if c["ticker"] == centre_ticker), key=lambda c: c["variation_id"])
        centred = [c for c in same_ticker if c["variation_id"] == centre_variation_id]
        if not centred:
            return CheckResult(
                4, NOT_EVALUABLE, {"centre_variation_id": centre_variation_id, "centre_ticker": centre_ticker},
                f"no graded cell at the replayed centre (variation_id={centre_variation_id}, "
                f"ticker={centre_ticker!r})",
            )
        peak = centred[0]
    else:
        peak = max(cells, key=lambda c: c["sharpe_ratio"])
        same_ticker = sorted((c for c in cells if c["ticker"] == peak["ticker"]), key=lambda c: c["variation_id"])
    peak_pos = next(i for i, c in enumerate(same_ticker) if c["variation_id"] == peak["variation_id"])
    before = same_ticker[max(0, peak_pos - bandwidth):peak_pos]
    after = same_ticker[peak_pos + 1: peak_pos + 1 + bandwidth]
    if len(before) < bandwidth or len(after) < bandwidth:
        return CheckResult(
            4, NOT_EVALUABLE,
            {"peak_variation_id": peak["variation_id"], "bandwidth": bandwidth,
             "neighbours_before": len(before), "neighbours_after": len(after)},
            "fewer neighbouring cells than plateau_bandwidth needs on at least one side of the peak",
        )

    neighbours = before + after
    n_vals = [c["sharpe_ratio"] for c in neighbours]
    mean_n = sum(n_vals) / len(n_vals)
    variance = sum((v - mean_n) ** 2 for v in n_vals) / len(n_vals)
    std_n = math.sqrt(variance)
    deviation = abs(peak["sharpe_ratio"] - mean_n)
    quantities = {"peak_sharpe": peak["sharpe_ratio"], "neighbourhood_mean": mean_n,
                  "neighbourhood_std": std_n, "bandwidth": bandwidth, "flatness_bar_std": flatness_bar}
    if std_n == 0.0:
        # DBG-2: a zero spread makes "within one standard deviation" hold only for a peak equal to
        # the mean. Any other peak is outside the bar, never cleared by a zero division or a 0 bar.
        if deviation == 0.0:
            return CheckResult(4, CLEARED, quantities, "the peak sits within the flatness bar of its own neighbourhood (Pardo ch10 p226/230)")
        return CheckResult(
            4, FAILED, quantities,
            "the neighbours' Sharpe ratios have zero spread and the peak differs from their mean, "
            "so it is not within one standard deviation (Pardo ch10 p226/230)",
        )
    if deviation <= flatness_bar * std_n:
        return CheckResult(4, CLEARED, quantities, "the peak sits within the flatness bar of its own neighbourhood (Pardo ch10 p226/230)")
    return CheckResult(4, FAILED, quantities, "the peak sits outside the flatness bar of its own neighbourhood (Pardo ch10 p226/230)")


def check_8_windfall(trades) -> CheckResult:
    """The windfall check (spec brief, checks 4/8 subsection; Pardo chapter 9, pages 205-209):
    PROM minus the biggest winning run, gated at strictly above 0 (#376 roster row 13, the most
    stringent variant, Pardo folio 208).

    PROM = {[AW x (#WT - sqrt(#WT))] - [AL x (#LT + sqrt(#LT))]} / Margin (book page 206). "PROM
    minus the biggest winning run" substitutes gross profit minus the largest run's own profit
    for gross profit before this same formula (book page 208): "removes the gross profit of the
    largest run of winning trades from the gross profit". Margin is fixed at 1: this system has
    no margin/collateral figure, and the frozen bar is "strictly above 0", a comparison whose
    sign is unchanged by division by any positive Margin, so Margin=1 changes no gate outcome.
    """
    if trades is None or len(trades) == 0:
        return CheckResult(8, NOT_EVALUABLE, {}, "trade record is empty")

    ordered = trades.sort_values("entry_index", kind="mergesort")  # stable: equal entry_index keeps the stored order
    pnls = [float(p) for p in ordered["pnl"].tolist()]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    wt, lt = len(wins), len(losses)
    if wt == 0:
        return CheckResult(8, NOT_EVALUABLE, {"winning_trade_count": 0},
                            "no winning trades in the record: the biggest winning run cannot be identified")

    # The biggest run of consecutive winning trades, chronological (entry_index) order. Its first and last positions are
    # kept so the profit outside it can be summed exactly (math.fsum); an equal run later does not replace the first.
    best_sum = 0.0
    best_first = best_last = -1
    run_first = -1
    for position, p in enumerate(pnls):
        if p > 0:
            if run_first < 0:
                run_first = position
            run_sum = math.fsum(pnls[run_first:position + 1])
            if run_sum > best_sum:
                best_sum, best_first, best_last = run_sum, run_first, position
        else:
            run_first = -1

    gross_profit = math.fsum(wins)
    gross_loss = math.fsum(losses)
    best_run = math.fsum(pnls[best_first:best_last + 1])
    adjusted_gross_profit = math.fsum(p for position, p in enumerate(pnls)
                                      if p > 0 and not best_first <= position <= best_last)
    a_wt = wt - math.sqrt(wt)
    a_lt = (lt + math.sqrt(lt)) if lt > 0 else 0.0
    aw_adjusted = adjusted_gross_profit / wt
    al = (gross_loss / lt) if lt > 0 else 0.0
    prom_minus_run = (a_wt * aw_adjusted) - (a_lt * al)  # / Margin(=1)

    quantities = {"gross_profit": gross_profit, "gross_loss": gross_loss,
                  "biggest_winning_run_profit": best_run, "winning_trade_count": wt,
                  "losing_trade_count": lt, "prom_minus_biggest_run": prom_minus_run}
    if prom_minus_run > 0.0:
        return CheckResult(8, CLEARED, quantities, "PROM minus the biggest winning run is strictly above 0 (#376 roster row 13)")
    return CheckResult(8, FAILED, quantities, "PROM minus the biggest winning run is at or below 0 (#376 roster row 13)")


# =============================================================================
# Set 5 night, lane JB (6.1b): checks 2, 5, 6, 7, 9. docs/plans/night/2026-09-10_set5a_night/
# PLAN.md section 7. Built to docs/briefs/judge_spec_brief.md's derivations: the DH9 amendment
# block (check 2), section 3's full derivation (checks 6 and 9), section 4 and #376 decision 2's
# D7 (check 7, MCPT sample count).
#
# A DELIBERATE SCOPE LIMIT (scripts/probes/judge_battery_2/PROBES.manifest, quoted in this lane's
# report): checks 2 and 5 each needed a load-bearing input (check 2's in-sample profit, check 5's
# per-period performance matrix) that existed nowhere in this schema at the time this comment was
# written, and researchdb.py, where either would need a new column and write path, was outside
# this lane's own area fence. Check 2's own scope limit is CLOSED as of lane WF (6.2b): that
# lane's own area fence covered exactly the researchdb.py write path this comment named as
# missing, and record_walkforward_run_stats now writes in_sample_profit, out_of_sample_profit and
# wfa_boundary_buffer_bars for real, so check_2_walk_forward below CAN and DOES clear or fail on a
# real walk-forward run today (scripts/probes/judge_battery_2's own wf08 proves both outcomes over
# the real function). Check 5 still always escalates to not-evaluable: its own per-period
# performance matrix remains unbuilt. Check 2's own DH9 arithmetic is proven correct through a
# private, pure helper that never has to invent a walk-forward split mechanism the brief does not
# specify, whether or not a real input happens to be present on a given run.
#
# SEVERAL NUMBERS BELOW ARE THIS BUILD'S OWN OPERATIONAL CHOICE, NOT A BOOK-STATED OR
# TAMER-RULED THRESHOLD, named plainly here and in the report rather than left to look like a
# sourced constant: the block-bootstrap
# block length's cube-root heuristic (check 6), and the MCPT's own "performance" statistic, the
# negative of the path's own maximum drawdown (check 7). judge_spec_brief.md itself licenses this:
# "check 6's and check 7's exact numeric statistics, are left to the builder's own book-justified
# choice. The probes test form, never an unstated method" (this lane's own report_0.md, section
# 1). None of these three touches `research_judge_thresholds` (the seed table stays at exactly
# nine frozen and six owed rows, scripts/probes/judge_battery_1/ac_j2_frozen_bars_and_owed_rows's
# own regression-guarded count), matching how `_MCPT_REPLICATION_COUNT` below and `_KAPPA_GATE`
# above already sit as plain module constants rather than register rows: a formula detail, never
# a pass bar Tamer might later amend under #193.
# =============================================================================


def _wfe_from_profits(oos_profit: float, is_profit: float, pass_bar: float,
                       buffer_bars: int) -> "tuple[str, str, dict]":
    """Walk-forward efficiency's own arithmetic (Pardo folio 239, quoted in judgedb.py's own seed
    row: "out-of-sample profit over in-sample profit"), carrying the DH9 amendment block's four
    rules (judge_spec_brief.md, the check-2 amendment) in isolation, as a private pure helper, so
    this arithmetic is proven correct on its own terms whether or not `check_2_walk_forward` below
    has a real input to hand it on a given run (as of lane WF, 6.2b, it now often does; module
    comment above).

    Returns (outcome, reason, quantities). `quantities` always carries `buffer_bars` (unchanged
    from the input) and `series_level` = "bar" (guard G3: check 2 always grades bar-level data),
    so the same shape `_resampling_descriptor_violation` reads can be built from it later.
    """
    # DH9 rule 3 (the boundary buffer, guard G1, never shortened or dropped): a real buffer is a
    # POSITIVE bar count. Zero or falsy reads as "no sourced length", the same way a NULL
    # threshold row reads as absent elsewhere in this module.
    if not buffer_bars or buffer_bars <= 0:
        return (
            NOT_EVALUABLE,
            "wfa_boundary_buffer_bars is the missing input: guard G1's boundary buffer has no "
            "positive bar length sourced for it (judge_spec_brief.md section 7)",
            {"buffer_bars": buffer_bars, "series_level": "bar"},
        )
    # DH9 rule 4: an in-sample profit at or below zero is never a divide and never a sign flip.
    if is_profit <= 0.0:
        return (
            NOT_EVALUABLE,
            "in_sample_profit is the missing input: it is at or below zero, so the walk-forward "
            "efficiency ratio has no valid denominator (DH9 amendment point 4: never a divide, "
            "never a sign flip)",
            {"buffer_bars": buffer_bars, "series_level": "bar", "in_sample_profit": is_profit},
        )
    # DH9 rule 1 (the graded statistic is out-of-sample profit, the numerator; never blended with
    # the denominator). wfe_pass_bar's own seed row is expressed as a PERCENT ("at least 50
    # percent"), so the ratio is scaled by 100 to compare on the same units, the units bug this
    # lane's own probe-writer caught during its throwaway pass (report_0.md section 2).
    wfe_percent = (oos_profit / is_profit) * 100.0
    quantities = {
        "buffer_bars": buffer_bars, "series_level": "bar", "out_of_sample_profit": oos_profit,
        "in_sample_profit": is_profit, "wfe_percent": wfe_percent, "pass_bar": pass_bar,
    }
    # wfe_pass_bar's own comparison is at_or_above (judgedb.py seed row): DH9 rule 2 (no
    # clamp, no substitute) is already satisfied above by refusing a non-positive denominator.
    if wfe_percent >= pass_bar:
        return CLEARED, f"walk-forward efficiency {wfe_percent:.2f}% is at or above the {pass_bar}% bar (Pardo folio 239)", quantities
    return FAILED, f"walk-forward efficiency {wfe_percent:.2f}% is below the {pass_bar}% bar (Pardo folio 239)", quantities


def check_2_walk_forward(run_id: int, conn, battery_version: str) -> CheckResult:
    """Walk-forward efficiency (spec brief roster row 1; DH9 amendment block). Reads
    `wfe_pass_bar` (already frozen at 50.0 by #376 decision 1), then the three walk-forward
    inputs lane WF (6.2b) now writes onto a run's own `research_run_stats` row through
    `record_walkforward_run_stats` (`in_sample_profit`, `out_of_sample_profit`,
    `wfa_boundary_buffer_bars`), and grades them through the pre-existing, unchanged
    `_wfe_from_profits` above (the DH9 arithmetic, proven correct independently of this check by
    `wf07`'s own guard). Three gates, checked in order, each naming its own missing input rather
    than a bundled reason: an unfrozen `battery_version`; no `research_run_stats` row at all for
    this `run_id` (a fresh store that never completed a run, WF-3's own lifecycle answer); a row
    that exists but has never recorded a walk-forward write (either profit still NULL, the
    ordinary state for every non-walkforward run this battery also grades). Past this point
    `_wfe_from_profits` owns every further refusal (a non-positive in-sample profit, a
    non-positive boundary buffer), so this function does not re-check what that helper already
    checks.
    """
    pass_bar_row = judgedb.read_threshold(conn, 2, "wfe_pass_bar", battery_version)
    if pass_bar_row is None:
        return CheckResult(2, NOT_EVALUABLE, {}, "wfe_pass_bar is the missing input: not frozen for this battery_version")
    stats_row = conn.execute(
        "SELECT in_sample_profit, out_of_sample_profit, wfa_boundary_buffer_bars "
        "FROM research_run_stats WHERE run_id = ?;",
        (run_id,),
    ).fetchone()
    if stats_row is None:  # no research_run_stats row at all: a fresh store, or a run_id that never completed
        return CheckResult(
            2, NOT_EVALUABLE, {},
            "in_sample_profit and out_of_sample_profit are the missing inputs: this run has no "
            "research_run_stats row at all",
        )
    is_profit = stats_row["in_sample_profit"]
    oos_profit = stats_row["out_of_sample_profit"]
    if is_profit is None or oos_profit is None:  # a row exists but no walk-forward write ever landed on it
        return CheckResult(
            2, NOT_EVALUABLE, {},
            "in_sample_profit and out_of_sample_profit are the missing inputs: this run's own "
            "research_run_stats row has not recorded a walk-forward write yet",
        )
    outcome, reason, quantities = _wfe_from_profits(
        oos_profit=oos_profit, is_profit=is_profit, pass_bar=pass_bar_row["value_numeric"],
        buffer_bars=stats_row["wfa_boundary_buffer_bars"],
    )
    return CheckResult(2, outcome, quantities, reason)


# --- Lane C5 (set 16c, #427, docs/units/C5/criteria.md): check 5's own per-run artifact ---------
#
# AFML section 11.6 (PDF pages 207-209, quoted in docs/units/C5/criteria.md, book protocol #216):
# the sweep's per-trial performance MATRIX M, one column per trial, partitioned into S equal
# contiguous blocks, C(S, S/2) train/test combinations, PBO the share of combinations whose
# out-of-sample rank logit is at or below zero. An ordinary sweep never stored this matrix (it
# computed each cell's own returns and threw them away, run_sweep_entrypoint.py's own
# _run_result_cell for the zero/inline paths, strategies/helpers/sweep_portfolio.py's
# sweep_portfolio_cells for the vectorized path); this section is check 5's own reader/grader over
# the new per-run artifact those two files now write (AC-1).

# AC-1: one module constant names the artifact file, read/written by both sides so a rename can
# never happen in only one place. Ends .parquet (export._read_parquet's own contract, the C9
# idiom) and differs from every sibling per-run artifact name already in use.
CSCV_ARTIFACT_FILENAME = "cscv_blocks.parquet"

# AC-8 (plan amendment 4, the lead's own resolution of a gap the amendment's text left open: how
# a caller tells "never attempted" apart from "skipped, over the size cap" once both look
# identical to a plain store lookup, no schema change available to carry the distinction). A
# SEPARATE, tiny marker artifact, uploaded by run_sweep.py IN PLACE of the real one (never
# alongside it: exactly one of the two exists for a given run, "never written half" still stands)
# whenever the container's own trial count would have pushed CSCV_ARTIFACT_FILENAME over the
# named size cap. routes/judge.py checks for this file FIRST, before ever calling the reader
# below, so a genuinely-missing sweep's own counter is never spuriously incremented for an
# over-cap one. Duplicated as a plain string literal in research_lab/worker/jobs/run_sweep.py
# (the engine/backend boundary: research_lab must never import dashboard.backend, the same reason
# run_sweep_entrypoint.py's own hashlib/param_config_hash comment already states); keep both
# literals identical by hand if this name ever changes.
CSCV_ARTIFACT_OVER_CAP_MARKER_FILENAME = "cscv_blocks_over_cap.marker.parquet"


def parse_cscv_artifact(df) -> "tuple[dict, int]":
    """AC-1's reader: a pandas DataFrame with columns (variation_id, ticker, block_index, count,
    sum, sumsq, bars_dropped) -> ({(variation_id, ticker): [(count, sum, sumsq), ...] ordered by
    block_index ascending}, bars_dropped). Raises KeyError when a required column is absent
    (never a silent partial read: AC-1's own file shape is a build contract, not user input).
    `bars_dropped` is read once (the same value for every row of one run's own artifact, the
    leftover the write side dropped from the START of its shared bar index before splitting into
    S equal blocks); a mixed-asset-class sweep whose partitions disagree on it is a known,
    documented simplification (see run_sweep_entrypoint.py's own comment at the write site).
    """
    required = ("variation_id", "ticker", "block_index", "count", "sum", "sumsq", "bars_dropped")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise KeyError(f"cscv artifact is missing required column(s) {missing}: build a fresh one")
    if len(df) == 0:
        # AC-4's own lifecycle clause: a zero-cell sweep writes a REAL, present, empty artifact
        # (never skipped), so check 5 reads this as "too few trials", never "missing artifact"
        # (AC-5). `bars_dropped` has no row to read here, so 0 (an honest "nothing was dropped
        # because nothing was ever summed", never a guessed leftover count).
        return {}, 0
    by_key: dict = {}
    for _, row in df.iterrows():  # order does not matter here; each key's own list is sorted below
        key = (int(row["variation_id"]), str(row["ticker"]))
        by_key.setdefault(key, []).append(
            (int(row["block_index"]), int(row["count"]), float(row["sum"]), float(row["sumsq"])))
    block_sums = {}
    for key, items in by_key.items():
        items.sort(key=lambda t: t[0])  # block_index ascending, whatever order the file rows were in
        block_sums[key] = [(count, total, totalsq) for (_, count, total, totalsq) in items]
    bars_dropped = int(df["bars_dropped"].iloc[0])  # one value per run's own artifact (see docstring)
    return block_sums, bars_dropped


def build_cscv_block_sums(bar_returns: list, s_blocks: int) -> "tuple[list, int]":
    """AC-2's writer-side aggregation, over ONE trial's own per-bar return sequence aligned to the
    calendar the whole sweep shares. A bar the member has no price for is `None` in `bar_returns`
    (AC-2: "a bar a member has no price for contributes a return of 0") and contributes 0 to that
    block's sum and sumsq while still counting toward the block's own count (round 2, C5-P2): the
    block partition is defined over the SHARED bar index, never over a member's own present bars,
    so every trial's blocks carry the same count and the matrix stays synchronous (AFML's own
    requirement 1). Leftover bars when the total is not a multiple of `s_blocks` are dropped from
    the START of the window (AC-1: "the book asks for equal dimensions").

    Called both by the pure math this module tests (this probe suite's own c10) and, in a
    necessarily DUPLICATED form (never imported: the engine package must never import
    dashboard.backend, the same boundary run_sweep_entrypoint.py's own hashlib/param_config_hash
    comment already states), by strategies/helpers/sweep_portfolio.py and
    run_sweep_entrypoint.py's own zero/inline paths, which build the real artifact inside the
    container.
    """
    if s_blocks < 1:
        raise ValueError(f"s_blocks must be a positive integer, got {s_blocks!r}")
    total_bars = len(bar_returns)
    bars_dropped = total_bars % s_blocks  # AC-1: dropped from the START, so every block is equal
    kept = bar_returns[bars_dropped:]
    block_width = len(kept) // s_blocks
    blocks = []
    for i in range(s_blocks):
        chunk = kept[i * block_width:(i + 1) * block_width]
        count = len(chunk)  # AC-2: a missing-price bar (None) still counts toward the block's count
        total = 0.0
        totalsq = 0.0
        for r in chunk:
            if r is not None:  # None (no price for this bar) contributes 0 to both sums, never skipped
                total += r
                totalsq += r * r
        blocks.append((count, total, totalsq))
    return blocks, bars_dropped


def cscv_combined_sharpe(count: int, total: float, totalsq: float) -> "float | None":
    """AC-2/AC-3: the Sharpe ratio of a combined block-sum, SAMPLE standard deviation (ddof=1,
    AFML's own convention for a subsample statistic, never population), no annualisation (a
    constant factor never changes a rank, AC-2). `None` when undefined: fewer than 2 bars, or a
    non-positive sample variance (a flat combined block, never a division by zero)."""
    if count < 2:
        return None
    mean = total / count
    variance = (totalsq - count * mean * mean) / (count - 1)
    if variance <= 0.0:
        return None
    return mean / math.sqrt(variance)


def cscv_combination_count(s_blocks: int) -> int:
    """AC-3: C(S, S/2), `math.comb`, never the book's own printed "12,780" for S=16 (a digit swap
    of the true math.comb(16, 8) = 12870, criteria.md's own derivation)."""
    return math.comb(s_blocks, s_blocks // 2)


def _cscv_rank_with_ties(values_by_key: dict, target_key) -> float:
    """AC-3's own rank rule: 1 = lowest. A `None` (undefined Sharpe) ranks below every defined
    value, grouped as one tied block at the bottom (AC-3: "a trial whose Sharpe is undefined on a
    half... ranks below every defined one"). Ties (equal defined values, or the whole undefined
    group) share the AVERAGE of the ranks they would occupy sorted ascending."""
    def sort_key(k):
        v = values_by_key[k]
        return (0,) if v is None else (1, v)

    ordered = sorted(values_by_key.keys(), key=sort_key)
    n = len(ordered)
    ranks = {}
    i = 0
    while i < n:
        j = i
        while j < n and sort_key(ordered[j]) == sort_key(ordered[i]):
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in ordered[i:j]:
            ranks[k] = avg_rank
        i = j
    return ranks[target_key]


def compute_pbo(block_sums: dict) -> dict:
    """AC-3: PBO to the book (AFML section 11.6). `block_sums` maps a trial key to an ORDERED list
    of (count, sum, sumsq) per block (AC-1's own artifact shape, `parse_cscv_artifact`'s return).
    Iteration order of `block_sums.keys()` is the tie-break's own "lowest column index" (criteria
    AC-3): Python 3.7+ dict order is insertion order, so the FIRST trial inserted wins an arg-max
    tie on the training statistic.

    Per combination of S/2 training blocks (`itertools.combinations`, count `math.comb(S, S/2)`):
    R from the training blocks' combined Sharpe, n* the arg-max (ties: lowest column index),
    R-bar from the testing blocks, omega-bar = (rank of R-bar_n* among R-bar, 1 = lowest, ties
    averaged) / (N + 1) (criteria AC-3: the lead's own derivation from two properties the book
    states, never a printed formula), lambda = log(omega-bar / (1 - omega-bar)), and PBO = the
    share of combinations with lambda at or below 0.

    Raises ValueError on fewer than 2 trials (AFML: "never run PBO on a single split"); the CALLER
    (check_5_cscv_pbo) is responsible for reading that boundary before calling this, so the reason
    a caller sees names trials or bars, never a bare exception.
    """
    keys = list(block_sums.keys())
    n_trials = len(keys)
    if n_trials < 2:
        raise ValueError("compute_pbo needs at least 2 trials (AFML: never run PBO on a single split)")
    s_blocks = len(next(iter(block_sums.values())))
    half = s_blocks // 2
    combination_count = cscv_combination_count(s_blocks)
    lambda_le_zero = 0
    for train_idx in itertools.combinations(range(s_blocks), half):
        test_idx = tuple(i for i in range(s_blocks) if i not in train_idx)
        r_train, r_test = {}, {}
        for k in keys:
            blocks = block_sums[k]
            tr = [sum(blocks[i][c] for i in train_idx) for c in range(3)]  # (count, sum, sumsq)
            te = [sum(blocks[i][c] for i in test_idx) for c in range(3)]
            r_train[k] = cscv_combined_sharpe(*tr)
            r_test[k] = cscv_combined_sharpe(*te)
        best = None  # arg-max over the TRAINING statistic; first-inserted key wins a tie (AC-3)
        for k in keys:
            v = r_train[k]
            if v is None:
                continue
            if best is None or v > r_train[best]:
                best = k
        # AFML gives no rule for "every training Sharpe is undefined"; the lead's own reading
        # (criteria AC-3's "ties: the lowest column index") extends to this case the same way:
        # the first-inserted key stands in for n*.
        n_star = best if best is not None else keys[0]
        rank = _cscv_rank_with_ties(r_test, n_star)
        omega_bar = rank / (n_trials + 1)
        lam = math.log(omega_bar / (1 - omega_bar))
        if lam <= 0:
            lambda_le_zero += 1
    return {
        "pbo": lambda_le_zero / combination_count,
        "n_trials": n_trials,
        "s_blocks": s_blocks,
        "combination_count": combination_count,
    }


# AC-5/AC-8: the ops routing law's two declared failure modes for check 5's own artifact.
# `cscv_artifact_missing` is incremented by routes/judge.py's `_read_cscv_artifact_for_run`, the
# only place that knows a real store lookup actually missed (the same `buy_hold_artifact_missing`
# idiom CHECK9_BUY_HOLD_MONITOR_COUNTERS above already uses beside it); this function never
# increments it itself; it only reports the reason the caller's own read already found. Plan
# amendment 3: `cscv_artifact_over_cap` IS incremented here, the one counter this function owns
# itself, because "over cap" is a fact this function's OWN caller flag carries on every grading
# attempt, not a fact discovered by a store lookup.
CHECK5_CSCV_MONITOR_COUNTERS = {
    "cscv_artifact_missing": 0,
    "cscv_artifact_over_cap": 0,
}


def check_5_cscv_pbo(run_id: int, conn, battery_version: str,
                      cscv_block_sums: "dict | None" = None,
                      cscv_bars_dropped: "int | None" = None,
                      cscv_artifact_missing: bool = False,
                      cscv_artifact_over_cap: bool = False) -> CheckResult:
    """CSCV (combinatorially symmetric cross-validation) and PBO (the probability of backtest
    overfitting), AFML section 11.6 (docs/units/C5/criteria.md, book protocol #216). Reads
    `pbo_threshold` (frozen at 0.5, #376 decision 2, D3) and `cscv_split_count` (frozen at 16,
    #421) through `judgedb.read_threshold`, per `battery_version`, never a literal.

    `run_id` is accepted for interface parity with every sibling check's own signature; this
    function's own logic never queries the database by it (the caller already resolved the
    artifact before calling in, AC-6: the caller's job, never this pure function's).

    Escalation order (AC-4/AC-5/AC-8), each branch a `not-evaluable` naming its own gap:
    1. `cscv_artifact_over_cap`: the container skipped writing the artifact because this sweep's
       own trial count would have pushed it over the named size cap (plan amendment 3); the
       sweep's other results are still valid. Counted here (the one counter this function owns).
    2. `cscv_block_sums is None`: no artifact was ever read for this run, whether because the
       caller explicitly found one missing (`cscv_artifact_missing=True`, AC-5: a sweep stored
       before this build, or one whose worker upload never landed) or because the caller passed
       nothing at all (the pre-this-build 3-argument calling shape every OTHER check's own
       regression-guard probe still uses, `scripts/probes/judge_battery_2`'s `ac_j14` and
       `scripts/probes/judge_values`'s `ac7`): both read the SAME reason, naming both the artifact
       and the per-period performance matrix it would have carried, matching this check's own
       pre-this-lane escalation wording closely enough that neither regression guard's own
       "no longer names cscv_split_count" and "still names the per-period matrix" assertions break.
    3. Too few trials (fewer than 2 keys in `cscv_block_sums`, e.g. a real, present-but-empty
       artifact from a zero-cell sweep, AC-4/lifecycle: "empty set... reads AC-4's too-few-trials
       word, not a missing artifact"): names trials, never a single split silently mis-worded.
    4. Any block narrower than 2 bars (AFML: never run PBO on a single split, applied per block
       here since the CRITERIA reads "too few bars to fill S blocks of at least 2 bars"): names
       bars.
    5. Otherwise: `compute_pbo`, graded against the frozen `pbo_threshold` by its own `comparison`.
    """
    if cscv_artifact_over_cap:
        CHECK5_CSCV_MONITOR_COUNTERS["cscv_artifact_over_cap"] += 1
        return CheckResult(
            5, NOT_EVALUABLE, {},
            "cscv-artifact-over-cap: this sweep's own trial count would have made the per-trial "
            "cscv block-sums artifact exceed the named size cap, so the container never wrote one "
            "for this run; the sweep's other results are unaffected",
        )

    if cscv_block_sums is None:
        return CheckResult(
            5, NOT_EVALUABLE, {},
            "the cscv artifact is the missing input: this run carries no stored per-period "
            "performance matrix (a sweep stored before this build, or one whose worker upload of "
            "the block-sums artifact never landed)",
        )

    threshold_row = judgedb.read_threshold(conn, 5, "pbo_threshold", battery_version)
    if threshold_row is None:
        return CheckResult(5, NOT_EVALUABLE, {}, "pbo_threshold is the missing input: not frozen for this battery_version")

    n_trials = len(cscv_block_sums)
    if n_trials < 2:
        return CheckResult(
            5, NOT_EVALUABLE, {},
            f"the cscv artifact carries too few trials ({n_trials}) to run PBO: never run PBO on "
            "a single split (AFML section 11.6)",
        )
    min_block_count = min(block[0] for blocks in cscv_block_sums.values() for block in blocks)
    if min_block_count < 2:
        return CheckResult(
            5, NOT_EVALUABLE, {},
            f"the cscv artifact's own blocks carry too few bars ({min_block_count}) to run PBO: "
            "each block needs at least 2 bars for a defined Sharpe",
        )

    pbo_result = compute_pbo(cscv_block_sums)
    comparison = threshold_row["comparison"]
    bar = threshold_row["value_numeric"]
    if comparison != "at_or_below":  # #376's own frozen row is at_or_below; fail loud on any other
        raise ValueError(f"pbo_threshold's comparison {comparison!r} is not one this check grades")
    cleared = pbo_result["pbo"] <= bar
    outcome = CLEARED if cleared else FAILED
    quantities = dict(pbo_result)
    quantities["bars_dropped"] = cscv_bars_dropped
    return CheckResult(
        5, outcome, quantities,
        f"PBO {pbo_result['pbo']:.4f} over {pbo_result['n_trials']} trials and "
        f"{pbo_result['s_blocks']} blocks ({pbo_result['combination_count']} combinations) is "
        f"{'at or below' if cleared else 'above'} the frozen bar {bar}",
    )


# --- Shared reproducibility machinery for checks 6 and 7 (round 2 finding) -------------------
#
# ROUND 2 FINDING: checks 6 and 7 used to draw from the shared, process-wide `random` module with
# no seed. Two gradings of the exact same stored run (the same equity.parquet and trades.parquet,
# never rewritten once a run has succeeded) could then return a different bootstrap bound or a
# different MCPT p-value, and near a bar, a different cleared/failed outcome -- for check 7 this
# was live rather than theoretical, since its own threshold is already frozen. `#193` freezes a
# threshold precisely so a verdict is reproducible; a frozen bar compared against a number that
# moves between runs of the SAME grading defeats the reason the bar was frozen at all.


def _seeded_random(run_id: int, check_id: int) -> "tuple[random.Random, int]":
    """A PRIVATE, seeded `random.Random` instance, never the shared `random` module's own global
    state, following `research_lab/universe/__init__.py`'s own shape (merged the same night:
    "a PRIVATE, seeded generator: never touches the shared random module's own global state").

    The seed is derived ONLY from `run_id` (the run's own identity, part of what is being graded)
    and `check_id` (so two different checks grading the SAME run do not draw from identical
    sequences); nothing about wall-clock time, process id or any other source outside the stored
    record enters it. A seed built from anything else would not fix the finding: the whole point
    is that grading the SAME run_id twice, from the SAME stored equity and trades, must draw the
    SAME sequence of "random" numbers both times, so the bound or p-value it produces is
    reproducible from the record alone, which is what a frozen threshold is compared against.

    Plain arithmetic, not Python's built-in `hash()`: `hash()` on a tuple is not a documented,
    guaranteed-stable value across interpreter versions, and this seed must be exactly reproducible
    forever, not merely within one process. The multiplier is large enough that two check ids for
    the same run never collide with two different run ids for another check in the range this
    system's own row counts reach.

    Returns (rng, seed) so a caller can both draw from `rng` and record `seed` in its own
    `quantities`, so a later reader can reproduce the exact number rather than take it on trust.
    """
    seed = run_id * 1_000_003 + check_id  # deterministic, stored-record-only; never wall clock or PID
    return random.Random(seed), seed


# --- Check 6, Monte Carlo bounds: shared routing and resampling machinery --------------------

# The bootstrap's own replication count: a FORMULA DETAIL (module comment above), not a
# Tamer-ruled pass bar. Masters' own MCPT chapter states "nreps typically several hundred to a few
# thousand" (docs/books/masters_trading_systems.md line 1134) for the same kind of Monte Carlo
# procedure; this build applies the same order of magnitude here.
_CHECK6_BOOTSTRAP_REPLICATIONS = 2000

# The descriptor-violation monitor (docs/plans/night/2026-09-10_set5a_night/PLAN.md section 7's
# own "what can break": "a bound comes out too narrow because a resampling guard was dropped").
# Every resampling descriptor this module writes into a CheckResult's own warnings list passes
# through `_resampling_descriptor_violation` first (via `_attach_resampling_descriptor` below),
# and a True reading increments this counter live. Expected zero on every real, correctly-routed
# computation: a nonzero reading is a genuine monitor firing, never a value this code path can
# reach by design. See docs/units/JB/monitor_rows.md for the registry wiring this counter feeds.
RESAMPLING_MONITOR_COUNTERS = {"resampling_descriptor_violation_count": 0}


def _has_multi_bar_hold(trades) -> bool:
    """True when the trade record proves at least one hold spans more than one bar: its own
    `entry_index` differs from its own `exit_index` on some row (AC-J15's own reading of the
    phrase, since neither the brief nor the ruling gives a numeric threshold). Absent or empty
    trade evidence is NOT read as proof of a multi-bar hold (AC-J16): the plain, single-bar-hold
    methods the books do state stay licensed whenever nothing contradicts them, never the reverse.
    A trade record that carries no `exit_index` column at all (an older artifact shape,
    scripts/probes/judge_battery_1's own fixture predates checks 6/7/9) is the SAME "cannot prove
    it" case as an absent record, never a crash: the fact is not readable from it either way.
    Shared by check 6 (guard G2's own routing) and check 7 (the same rider's own G2 analog).
    """
    if trades is None or len(trades) == 0:
        return False
    if "entry_index" not in trades.columns or "exit_index" not in trades.columns:
        return False
    return bool((trades["entry_index"] != trades["exit_index"]).any())


class BlockLengthUndefined(ValueError):
    """The automatic block length cannot be read from this series (too short, a 0/0 correlation,
    or a non-positive long-run variance). Check 6 turns this into a not-evaluable result."""


def _pw_mean(values: list) -> float:
    return sum(values) / len(values)


def _pw_cov(a: list, b: list) -> float:
    """Sample covariance, divisor length-1, of two equal-length slices."""
    ma, mb = _pw_mean(a), _pw_mean(b)
    return sum((u - ma) * (v - mb) for u, v in zip(a, b)) / (len(a) - 1)


def _pw_autocorrelations(x: list, mmax: int) -> list:
    """rho(k), k = 1..mmax: the Pearson correlation of x[mmax:] with x[mmax-k:n-k] (Patton's
    opt_block_length_REV_dec07). A slice with zero variance gives nan (a 0/0 correlation)."""
    n = len(x)
    head = x[mmax:]
    var_head = _pw_cov(head, head)
    rho = []
    for k in range(1, mmax + 1):
        lagged = x[mmax - k:n - k]
        denom = math.sqrt(var_head * _pw_cov(lagged, lagged))
        rho.append(_pw_cov(head, lagged) / denom if denom > 0.0 else float("nan"))
    return rho


def _pw_lag_window(rho: list, n: int) -> "tuple[int, int]":
    """(mhat, M) from Patton's code: rho[0] is the lag-1 correlation and len(rho) == mmax. mhat is
    the smallest j with rho(j)..rho(j+KN-1) all strictly inside the band 2 sqrt(log10(n)/n), else
    the largest lag with |rho| above the band; M = mmax if 2 mhat > mmax else 2 mhat."""
    mmax = len(rho)
    kn = max(5, math.ceil(math.sqrt(math.log10(n))))
    band = 2.0 * math.sqrt(math.log10(n) / n)
    mhat = None
    for j in range(1, mmax - kn + 2):
        if all(abs(r) < band for r in rho[j - 1:j - 1 + kn]):
            mhat = j
            break
    if mhat is None:
        above = [k for k in range(1, mmax + 1) if abs(rho[k - 1]) > band]
        if not above:
            raise BlockLengthUndefined(f"n = {n}: no lag window found in the correlations")
        mhat = max(above)
    return mhat, (mmax if 2 * mhat > mmax else 2 * mhat)


def _pw_autocovariances(x: list, M: int) -> dict:
    """acv(k), k = 0..M: the sample covariance (divisor length-1) of x[M:] with x[M-k:n-k]."""
    n = len(x)
    head = x[M:]
    return {k: _pw_cov(head, x[M - k:n - k]) for k in range(M + 1)}


def _pw_block_lengths(acv: dict, M: int, n: int) -> "tuple[float, float]":
    """(b_SB, b_CB), unrounded, capped at Bmax = ceil(min(3 sqrt n, n/3)): Patton, Politis and
    White (2009), D_SB = 2 g(0)^2 and D_CB = (4/3) g(0)^2 with g(0) = s2 here."""
    def lam(u: float) -> float:
        u = abs(u)
        return 1.0 if u < 0.5 else 2.0 * (1.0 - u)

    g = 0.0
    s2 = 0.0
    for k in range(-M, M + 1):
        w = lam(k / M)
        if w == 0.0:
            continue  # lag M has weight zero: its covariance is not read
        g += w * abs(k) * acv[abs(k)]
        s2 += w * acv[abs(k)]
    if not s2 > 0.0:
        raise BlockLengthUndefined(f"n = {n}: s2 (the long-run variance) is {s2}, not above zero")
    bmax = math.ceil(min(3.0 * math.sqrt(n), n / 3.0))
    scale = n ** (1.0 / 3.0)
    b_sb = min((2.0 * g * g / (2.0 * s2 * s2)) ** (1.0 / 3.0) * scale, bmax)
    b_cb = min((2.0 * g * g / ((4.0 / 3.0) * s2 * s2)) ** (1.0 / 3.0) * scale, bmax)
    return b_sb, b_cb


def _block_length_for(series: list) -> int:
    """The circular block bootstrap's block length, chosen from the series' own correlation:
    Politis and White's automatic rule as corrected by Patton, Politis and White (2009,
    Econometric Reviews 28(4): 372-375), following Patton's opt_block_length_REV_dec07 (DECISIONS
    #478 item 4; parameter_provenance.md judge.check6_block_length). It replaces the cube-root rule.
    A constant series (zero sample variance) gives 1: every block of it is the same, so the bound
    is unchanged. A series shorter than mmax + 2 bars, a 0/0 correlation, or a non-positive s2
    raises BlockLengthUndefined. Otherwise max(1, ceil(b_CB)): our resampler uses fixed blocks with
    a wrapped start, which is the circular bootstrap, and Patton rounds up.
    """
    n = len(series)
    if n >= 2 and _pw_cov(series, series) == 0.0:
        return 1
    mmax = math.ceil(math.sqrt(n)) + max(5, math.ceil(math.sqrt(math.log10(n)))) if n >= 2 else 0
    if n < mmax + 2:
        raise BlockLengthUndefined(f"n = {n} bars, fewer than mmax + 2 = {mmax + 2} needed for the correlations")
    rho = _pw_autocorrelations(series, mmax)
    if not all(math.isfinite(r) for r in rho):
        raise BlockLengthUndefined(f"n = {n}: a correlation is not finite (0/0)")
    _, M = _pw_lag_window(rho, n)
    acv = _pw_autocovariances(series, M)
    if not all(math.isfinite(v) for v in acv.values()):
        raise BlockLengthUndefined(f"n = {n}: an autocovariance is not finite")
    _, b_cb = _pw_block_lengths(acv, M, n)
    return max(1, math.ceil(b_cb))


def _block_bootstrap_resample(series: list, block_length: int, rng: "random.Random") -> list:
    """One block-bootstrap resample of `series`: draw consecutive BLOCKS of `block_length`
    elements, with replacement, wrapping the start index, until the resample is as long as the
    original (TSSB section 12.1's own block bootstrap, #213's guard G2: a block of consecutive bar
    returns, never an independent per-observation draw). `rng` is a PRIVATE, seeded generator
    (`_seeded_random` above, round 2 finding): never the shared `random` module's own global
    state, so the same run graded twice draws the same blocks both times.
    """
    n = len(series)
    if block_length >= n:
        return list(series)  # a block as long as the series itself: the only "block" is the whole series
    resample = []
    while len(resample) < n:
        start = rng.randrange(0, n)  # every position has an equal chance of starting a block
        for offset in range(block_length):
            resample.append(series[(start + offset) % n])  # wrap around the end of the series
    return resample[:n]


def _bootstrap_bound(bar_returns: list, percentile: float, rng: "random.Random") -> "tuple[float, dict]":
    """The block-bootstrap lower confidence bound on the mean return (Masters pages 222-273;
    #213 guard G2). `_CHECK6_BOOTSTRAP_REPLICATIONS` block-bootstrap resamples of `bar_returns`
    are drawn, each resample's own mean is computed, and the bound is read off the `percentile`-th
    value of the sorted distribution of those means (#213's own "pessimistic lower bound"). `rng`
    is the caller's own private, seeded generator (`_seeded_random`, round 2 finding), threaded
    through rather than read from the shared `random` module, so the bound this function returns
    is reproducible from the same stored `bar_returns` and the same seed alone.

    DETERMINISM ON A CONSTANT SERIES (this suite's own probes pin no seed value, only that the
    build draws from SOME seeded generator): every element of `series` being equal means every
    block, drawn from any start position, is the SAME sequence of that one value, so every
    resample's own mean equals that value exactly, and any percentile of an all-equal distribution
    is that same value, whatever the seed -- proven by this suite's own probes rather than assumed.
    """
    n = len(bar_returns)
    block_length = _block_length_for(bar_returns)
    means = []
    for _ in range(_CHECK6_BOOTSTRAP_REPLICATIONS):
        resample = _block_bootstrap_resample(bar_returns, block_length, rng)
        means.append(sum(resample) / len(resample))
    means.sort()
    index = min(len(means) - 1, max(0, int(percentile * len(means))))
    bound = means[index]
    descriptor = {"resampling_unit": "block", "series_level": "bar", "block_length": block_length,
                  "block_length_rule": "politis-white-cb"}
    return bound, descriptor


def _resampling_descriptor_violation(descriptor: dict) -> bool:
    """`judge_spec_brief.md` section 5's own monitor rule (AC-J18), turned into one testable
    function: True when `resampling_unit` reads 'iid' (never a naive, independent-draw resample),
    when `series_level` reads 'trade' (never a trade-level series, guard G3), or when a
    `buffer_bars` entry is present and is None or at or below zero (a real split buffer is always
    a positive bar count). False otherwise.
    """
    if descriptor.get("resampling_unit") == "iid":
        return True
    if descriptor.get("series_level") == "trade":
        return True
    if "buffer_bars" in descriptor:
        value = descriptor["buffer_bars"]
        if value is None or value <= 0:
            return True
    return False


def _attach_resampling_descriptor(warnings: list, descriptor: dict) -> None:
    """Append a `resampling_descriptor` warning entry, counting a live violation first
    (RESAMPLING_MONITOR_COUNTERS above) if the monitor's own detection logic ever finds one:
    expected zero on every computation this build produces, the plan's own monitor row for this
    lane (docs/units/JB/monitor_rows.md)."""
    if _resampling_descriptor_violation(descriptor):
        RESAMPLING_MONITOR_COUNTERS["resampling_descriptor_violation_count"] += 1
    warnings.append({"code": "resampling_descriptor", "detail": descriptor})


def check_6_monte_carlo_bounds(run_id: int, conn, battery_version: str, bar_returns, trades) -> CheckResult:
    """Monte Carlo bounds (spec brief section 3, "Check 6, Monte Carlo bounds"; #213 guards G2/G3).
    Reads `mc_bound_pass_bar` (frozen at 0.0, strictly above, by #376 decision 2, D1(a)) and
    `bound_percentile`. Once both are present and a bar-level return series exists, it ALWAYS
    computes the bound with the block bootstrap (DECISIONS #450: the resample happens every run;
    2000 resamples, the 10th percentile, Masters folios 245, 246, 261), seeded from the run's own
    identity, and grades it against the frozen bar. `trades` stays a parameter because the route
    passes it; check 6 no longer reads it.
    """
    pass_bar_row = judgedb.read_threshold(conn, 6, "mc_bound_pass_bar", battery_version)
    if pass_bar_row is None:
        return CheckResult(6, NOT_EVALUABLE, {}, "mc_bound_pass_bar is the missing input: not frozen for this battery_version")
    percentile_row = judgedb.read_threshold(conn, 6, "bound_percentile", battery_version)
    if percentile_row is None:
        return CheckResult(
            6, NOT_EVALUABLE, {},
            "bound_percentile is the missing input: not frozen for this battery_version (owed, "
            "the percentile at which #213's pessimistic lower bound is read, judge_spec_brief.md "
            "section 7)",
        )
    if bar_returns is None or len(bar_returns) == 0:
        return CheckResult(
            6, NOT_EVALUABLE, {},
            "the candidate's bar-level return series is the missing input: equity.parquet is "
            "absent or empty",
        )

    pass_bar = pass_bar_row["value_numeric"]
    percentile = percentile_row["value_numeric"]

    # A private, seeded generator derived only from this run's own identity (never the shared
    # `random` module), so grading the same run_id twice from the same stored record draws the same
    # blocks both times and returns the same bound. The seed is recorded so a later reader can
    # reproduce the number rather than take it on trust.
    rng, seed = _seeded_random(run_id, 6)
    try:
        bound, descriptor = _bootstrap_bound(bar_returns, percentile, rng)
    except BlockLengthUndefined as err:
        return CheckResult(6, NOT_EVALUABLE, {}, f"block-length-undefined: {err}")
    quantities = {
        "method": "bootstrap", "percentile": percentile, "pass_bar": pass_bar,
        "bound": bound, "seed": seed,
    }

    warnings = []
    _attach_resampling_descriptor(warnings, descriptor)
    # mc_bound_pass_bar's own comparison is strictly_above (judgedb.py seed row).
    if bound > pass_bar:
        return CheckResult(6, CLEARED, quantities,
                            f"the pessimistic lower bound {bound:.6f} is strictly above the "
                            f"{pass_bar} bar (Masters ch6, #376 decision 2 D1(a))", warnings)
    return CheckResult(6, FAILED, quantities,
                        f"the pessimistic lower bound {bound:.6f} is not strictly above the "
                        f"{pass_bar} bar (Masters ch6, #376 decision 2 D1(a))", warnings)


# --- Check 7, MCPT (Monte Carlo permutation test) --------------------------------------------

# #376 decision 2, D7: Masters' form, (k+1)/(m+1), with 4,999 replications so the p-value times
# 5,000 is an integer. A FORMULA DETAIL (D7 rules how the count and the +1/+1 form combine), not a
# Tamer-ruled pass bar: sits beside `_KAPPA_GATE` and `_TRADE_COUNT_CAUTION` above on the same
# footing, a plain module constant rather than a `research_judge_thresholds` row (module comment
# above; scripts/probes/judge_battery_2/ac_j16's own docstring makes the same argument).
_MCPT_REPLICATION_COUNT = 4999


def _mcpt_pvalue(k: int, m: int) -> float:
    """Masters' own MCPT p-value form (Testing and Tuning Market Trading Systems, section 7.4,
    quoted verbatim in docs/books/masters_trading_systems.md line 1134: "The formula (k + 1) /
    (m + 1) ... is the standard MCPT p-value formula"), verified in the book's own notes at build
    time. `k` is the count of shuffled reorderings whose own performance tied or beat the
    original's; `m` is the replication (shuffle) count. The `+1` on each side is Masters' own
    convention for including the original result alongside its own shuffles, which is also why a
    p-value of exactly zero can never be produced by this form.
    """
    return (k + 1.0) / (m + 1.0)


def _mcpt_verdict(p_value: float, pass_bar: float, comparison: str) -> str:
    """Grade a p-value against its bar using the threshold row's own comparison word.

    `at_or_below` (batteries 2026-09-09.a and 2026-09-24.a, kept as graded then) clears on
    `p <= bar`. `strictly_below` (DBG-2b, battery 2026-10-10.a, DECISIONS #436 item 7) clears only on
    `p < bar`. Any other word is refused by name, never read as a pass or a fail.
    """
    if comparison == "at_or_below":
        return CLEARED if p_value <= pass_bar else FAILED
    if comparison == "strictly_below":
        return CLEARED if p_value < pass_bar else FAILED
    raise ValueError(
        f"p-value verdict has no rule for comparison {comparison!r}; the rules are "
        f"'at_or_below' and 'strictly_below'"
    )


def _mcpt_performance(series: list) -> float:
    """The order-sensitive 'performance' statistic Masters section 7.4 asks the MCPT loop to
    compute ("Performance must be measured the same way for all reps"). The exact MCPT 'flavor'
    (which statistic) is explicitly OWED by `#213` for a multi-day hold (judge_spec_brief.md:
    "This brief does not pick a flavor... Owed, section 7"), and this suite's own probes state
    that proving the WIRING here "does NOT, and does not need to, pin the MCPT flavor" for the
    single-bar-hold case this function serves either (ac_j16's own docstring).

    A plain sum or mean cannot serve this role: BOTH are invariant under any full reordering of
    the same values (permutation never changes a set's own total or average), so every shuffle
    would always tie the original for EVERY input, not only the determinism fixture, which would
    make the test vacuous rather than merely undecided. This build's own choice, a scope decision
    named in the report rather than presented as book-sourced, is the negative of the path's own
    maximum drawdown: the worst peak-to-trough dip of the cumulative return path, an order-
    sensitive quantity of exactly the kind this same spec brief already treats as a first-class
    bar-level concept ("the drawdown distribution #215 compares paper results against"). Higher
    (a smaller, less-negative drawdown) is better, matching ">= original" meaning "at least as
    good."
    """
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for r in series:
        cumulative += r                       # the running total of bar-level returns so far
        peak = max(peak, cumulative)           # the highest point the path has reached so far
        max_drawdown = max(max_drawdown, peak - cumulative)  # how far below that peak we are now
    return -max_drawdown


def check_7_mcpt(run_id: int, conn, battery_version: str, bar_returns, trades=None, rerun_result=None, rerun_error=None) -> CheckResult:
    """MCPT, shortlist only (spec brief, "Checks 2, 5, 7: procedural, not distributional"; #213).
    Masters' (k+1)/(m+1) form, `_MCPT_REPLICATION_COUNT` reorderings via a Fisher-Yates shuffle
    (Masters section 7.7.1), graded against the frozen `mcpt_pvalue_threshold`. Guard G3 (bar
    level, never trade level) is structural here: this function's own input is already a
    bar-level series. The G2 analog (this build's own derivation, since G2 itself names a
    resampling scheme this check's plain shuffle does not use word for word): a proven multi-bar
    hold leaves the correct shuffle flavor genuinely undecided (`#213`'s own "What is deliberately
    left to spec time" paragraph), so this escalates rather than silently permuting bar-level
    returns as though every hold were single-bar. Absent trade evidence is not read as proof of a
    multi-bar hold (`_has_multi_bar_hold` above): the plain shuffle stays licensed unless something
    proves otherwise.

    Lane P4 (DECISIONS #422, #445 item 4): `rerun_result`, when the route passes one, is the outcome of a
    permutation test that already re-ran the whole strategy on shuffled price bars
    (research_lab/judge_arms/mcpt_bars.py `mcpt`, read from the replay's mcpt artifact). It carries k, m,
    p_value, seed, statistic and original_performance. Check 7 then only grades it, by Masters' (k + 1) / (m + 1)
    against the frozen bar; a result graded from its re-run never reads bar_returns or trades. Without it
    (None) everything below runs exactly as before.

    Lane GV3-J7 (#451, #488 item 8): `rerun_error` is the reason the stage could not make that re-run for a theory strategy
    (replay.mcpt_error). Check 7 is then not evaluable: it never falls back to the bar-return shuffle. Giving both
    `rerun_result` and `rerun_error` is a caller bug and raises ValueError.
    """
    threshold_row = judgedb.read_threshold(conn, 7, "mcpt_pvalue_threshold", battery_version)
    if threshold_row is None:
        return CheckResult(7, NOT_EVALUABLE, {}, "mcpt_pvalue_threshold is the missing input: not frozen for this battery_version")
    pass_bar = threshold_row["value_numeric"]
    bar_comparison = threshold_row["comparison"]

    if rerun_result is not None and rerun_error is not None:
        raise ValueError("check 7 was given both a re-run result and a re-run error; a replay carries one or the other")
    if rerun_error is not None:
        return CheckResult(
            7, NOT_EVALUABLE, {},
            f"check 7's re-run on shuffled price bars could not be made: {rerun_error} "
            f"(#451: never the bar-return shuffle instead)",
        )

    if rerun_result is not None:
        k, m = int(rerun_result["k"]), int(rerun_result["m"])
        if m != _MCPT_REPLICATION_COUNT:
            return CheckResult(
                7, NOT_EVALUABLE, {},
                f"the re-run used {m} fake markets but the frozen replication count is "
                f"{_MCPT_REPLICATION_COUNT} (#376 decision 2, D7): a different count is not graded",
            )
        p_value = _mcpt_pvalue(k, m)
        verdict = _mcpt_verdict(p_value, pass_bar, bar_comparison)
        quantities = {"k": k, "m": m, "p_value": p_value, "pass_bar": pass_bar,
                      "original_performance": rerun_result.get("original_performance"),
                      "seed": rerun_result.get("seed"), "statistic": rerun_result.get("statistic")}
        reason = (f"MCPT p-value {p_value:.6f} ({k} of {m} re-runs on shuffled price bars tied or beat the "
                  f"original, Masters' (k+1)/(m+1) form) against the {pass_bar} bar (#376 decision 2, D2/D7; "
                  f"#422 re-run of the whole strategy)")
        return CheckResult(7, verdict, quantities, reason)

    if bar_returns is None or len(bar_returns) == 0:
        return CheckResult(
            7, NOT_EVALUABLE, {},
            "the candidate's bar-level return series is the missing input: equity.parquet is "
            "absent or empty",
        )

    if _has_multi_bar_hold(trades):
        return CheckResult(
            7, NOT_EVALUABLE, {},
            "the MCPT shuffle flavor for a multi-bar hold is the missing input: #213's own "
            "'What is deliberately left to spec time' paragraph reserves which permutation scheme "
            "survives a multi-day hold (judge_spec_brief.md), and this build does not invent one",
        )

    # Round 2 finding: this loop used to shuffle through the shared `random` module's own global
    # state, so grading the same run twice could shuffle differently and return a different
    # p-value, and near the frozen bar a different cleared/failed outcome, from the SAME stored
    # record. A private generator seeded only from this run's own identity (`_seeded_random`,
    # never wall-clock time or anything outside the record) makes the whole loop reproducible: the
    # same run_id draws the same 4,999 shuffles every time it is graded.
    rng, seed = _seeded_random(run_id, 7)
    m = _MCPT_REPLICATION_COUNT
    series = list(bar_returns)
    original_performance = _mcpt_performance(series)
    k = 0
    for _ in range(m):
        shuffled = list(series)
        rng.shuffle(shuffled)  # Fisher-Yates, Masters section 7.7.1: every ordering equally likely
        if _mcpt_performance(shuffled) >= original_performance:
            k += 1
    p_value = _mcpt_pvalue(k, m)
    verdict = _mcpt_verdict(p_value, pass_bar, bar_comparison)
    quantities = {"k": k, "m": m, "p_value": p_value, "pass_bar": pass_bar,
                  "original_performance": original_performance, "seed": seed}
    reason = (f"MCPT p-value {p_value:.6f} ({k} of {m} shuffles tied or beat the original, "
              f"Masters' (k+1)/(m+1) form) against the {pass_bar} bar (#376 decision 2, D2/D7)")
    return CheckResult(7, verdict, quantities, reason)


# --- Check 9, controls -------------------------------------------------------------------------

def _is_net_short(trades) -> bool:
    """True when the trade record's own `direction` column ('long'/'short',
    `research_lab/strategies/base/vbt_columns.py`'s own two values) counts more short trades than
    long ones (AC-J17's own documented convention: neither the brief nor the ruling gives one, and
    TSSB's own sell-short-and-hold definition is a naive long/short baseline pair, not a
    net-exposure calculation). A trade record with no `direction` column at all (an older artifact
    shape, scripts/probes/judge_battery_1's own fixture predates check 9) reads as "not proven net
    short", the same absent-evidence rule `_has_multi_bar_hold` above states, never a crash.
    """
    if trades is None or len(trades) == 0:
        return False
    if "direction" not in trades.columns:
        return False
    shorts = int((trades["direction"] == "short").sum())
    longs = int((trades["direction"] == "long").sum())
    return shorts > longs


# Lane C9 (set 16b, `#421`/`#423`, docs/units/C9/criteria.md, AC-6): the buy-and-hold control
# arm's own three declared failure modes get one counter each, the ops routing law's own
# requirement, declared here beside check_9_controls the same way RESAMPLING_MONITOR_COUNTERS
# sits beside check_6/7 above. `buy_hold_arm_compute_failed` and `buy_hold_series_no_overlap` are
# incremented by check_9_controls itself, below, every time it returns not-evaluable for that
# reason. `buy_hold_artifact_missing` is incremented by routes/judge.py, at GRADING time, when the
# replay's own stored object carries neither a buy_hold_error nor a readable arm artifact (an old
# replay computed before this build, or one whose worker upload never wrote one): that is a
# route-level fact about what was found on disk, never something this pure function can observe on
# its own, so it lives beside the two counters this function does own rather than inside this
# function's own increment sites (docs/units/C9/monitor_rows.md carries the reader/registry wiring
# for all three).
CHECK9_BUY_HOLD_MONITOR_COUNTERS = {
    "buy_hold_arm_compute_failed": 0,
    "buy_hold_artifact_missing": 0,
    "buy_hold_series_no_overlap": 0,
}


def _population_stdev(values: list) -> float:
    """Population standard deviation (ddof=0): divides the sum of squared deviations from the
    mean by the FULL observation count, never count-1. Sourced from the book protocol (`#216`):
    Timothy Masters, *Testing and Tuning Market Trading Systems: Algorithms in C++*, page 181,
    `sharpe_ratio()`'s own C code (`docs/units/C9/book_reads.md` quotes it verbatim and re-derives
    the index arithmetic that settles population over sample). Never called on an empty list by
    this module's own caller, `_sharpe_ratio`, which checks length first.
    """
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


def _sharpe_ratio(returns: list, annualization_factor: float) -> "float | None":
    """The ordinary Sharpe ratio, `docs/units/C9/book_reads.md`'s own formula (Masters page 181):
    mean divided by the population standard deviation, annualised by `sqrt(annualization_factor)`,
    no risk-free term subtracted (`#421` item 3, `#423` sheet item 3: a nonzero risk-free rate
    would shift both arms' Sharpe by the same amount and cancel out of the margin, so this build
    never subtracts one). Returns None, never raises and never a guessed value, when the series has
    fewer than two points or a zero standard deviation (AC-4: never a division by zero).
    """
    if len(returns) < 2:
        return None
    stdev = _population_stdev(returns)
    if stdev <= 0.0:
        return None
    return (sum(returns) / len(returns)) / stdev * math.sqrt(annualization_factor)


def _dated_series_intersection(dates_a: list, values_a: list, dates_b: list, values_b: list):
    """Pairs each series with its own bar dates and returns the two value lists restricted to the
    shared dates only, in date order (F4, the lead's meaning review round 1, finding 4:
    "overlapping days" means a real date intersection, never plain min-length pairing by list
    position). Returns `(values_a_over_intersection, values_b_over_intersection,
    overlapping_day_count)`. A date repeated within one series collapses to its last value, the
    same behaviour `dict(zip(...))` already gives every other reader of a dated series in this
    codebase; the replay writer never repeats a bar date in practice.
    """
    by_date_a = dict(zip(dates_a, values_a))
    by_date_b = dict(zip(dates_b, values_b))
    shared_dates = sorted(set(by_date_a) & set(by_date_b))
    return (
        [by_date_a[d] for d in shared_dates],
        [by_date_b[d] for d in shared_dates],
        len(shared_dates),
    )


def check_9_controls(run_id: int, conn, battery_version: str, bar_returns, trades,
                      buy_hold_bar_returns=None, buy_hold_error: "str | None" = None,
                      bar_returns_dates: "list | None" = None,
                      buy_hold_bar_returns_dates: "list | None" = None,
                      random_arm_result: "dict | None" = None) -> CheckResult:
    """Controls (spec brief section 3, "Check 9, controls"; `#193` safeguard 3): the random-entry
    arm and the buy-and-hold arm. Reads all six of check 9's own registered quantities before
    escalating (AC-J17: "never stopping silently at the first one it happens to read"), so a
    not-evaluable reason names every genuinely-missing input from BOTH arms together. The D4
    sell-short-and-hold warning (`#376` decision 2, D4) is computed from `trades` BEFORE any
    escalation, so it attaches whatever the check's own final outcome is: D4 is a warning,
    decoupled from gating.

    `bar_returns` is the candidate's own daily return series, used by this check ONLY (never the
    same series checks 6/7/8 read): `#426` (Tamer's ruling, DECISIONS.md, on Masters page 181's own
    "the price array must actually be the log of the real prices") requires it to be the NATURAL-LOG
    bar-to-bar change of the candidate's stored equity curve, `ln(e_i / e_i-1)`, produced by
    `routes/judge.py`'s own `_dated_bar_returns_from_equity`, never `_bar_returns_from_equity`'s
    undated PERCENT change (that function's own output stays checks 6/7/8's series, unchanged).

    Lane C9 (set 16b, `#421`/`#423`, docs/units/C9/criteria.md): the buy-and-hold arm itself.
    `buy_hold_bar_returns` is the arm's own daily return series, computed inside the replay's own
    container job and read by the caller (routes/judge.py) off the stored replay artifact;
    `buy_hold_error` is a non-None string exactly when that container-side computation failed
    (read off the stored replay payload's own `buy_hold_error` key). `bar_returns_dates` /
    `buy_hold_bar_returns_dates` are the two series' own PARALLEL bar-date lists (unix seconds,
    `_write_replay_series`'s own shape), read by the caller from the two stored replay artifacts.
    When both series and both date lists are real, the two Sharpes below are computed over the
    DATE INTERSECTION only (F4), never plain min-length pairing, and `overlapping_day_count` is
    the intersection's own size.

    AC-3's own escalation: the arm's own verdict word is `"passed"` when its Sharpe margin
    (candidate minus buy-and-hold) is strictly above `buy_hold_arm_pass_bar` (`0.0`, frozen),
    `"failed"` otherwise, INCLUDING an exact tie (never passed on a tie). Check 9's own outcome is
    `FAILED` when the arm fails (`#193` safeguard 3: both control arms must pass, so no single
    arm's own pass can rescue the whole check); otherwise `NOT_EVALUABLE`, naming the random-entry
    arm alone as the remaining unbuilt piece (the lead's own lean, `docs/units/C9/criteria.md`
    AC-3: a failed buy-and-hold arm is a real answer and is never hidden behind the unbuilt
    random arm).

    Lane P5 (DECISIONS #213 row 9, #421 item 1, #445 item 4): `random_arm_result`, when the route passes one, is
    the stored outcome of Aronson's Monte Carlo permutation (research_lab/judge_arms/random_arm.py `random_arm`,
    read from the replay's random_arm artifact): k, m, p_value, original_statistic, seed. The arm passes when
    (k + 1) / (m + 1) beats the frozen `random_arm_pass_bar` by that row's own comparison word. Order: missing
    threshold, buy_hold_error, a wrong m (NOT_EVALUABLE), a failing random arm (FAILED whatever the buy-and-hold
    arm shows), then the buy-and-hold arm's own exits and failure as before, and CLEARED when both pass. With
    `random_arm_result` None every line below behaves exactly as before this lane.
    """
    warnings = []
    if _is_net_short(trades):
        warnings.append({
            "code": "sell-short-and-hold-baseline",
            "detail": "the candidate's trade record is net short: the sell-short-and-hold "
                       "baseline is attached as a warning, never a second gate (#376 decision 2, D4)",
        })

    # Both arms' own frozen pass bars, plus the four owed quantities (judgedb.py's own seed table,
    # all still status='owed' today), read together in this fixed order and ALL accumulated into
    # one reason, never stopping at the first (AC-J17): the check reasons about both arms.
    required_keys = (
        "random_arm_pass_bar", "buy_hold_arm_pass_bar",
        "random_arm_comparison_statistic", "random_arm_replication_count",
        "buy_hold_arm_construction", "buy_hold_arm_comparison_statistic",
    )
    missing = [key for key in required_keys if judgedb.read_threshold(conn, 9, key, battery_version) is None]

    if missing:
        return CheckResult(
            9, NOT_EVALUABLE, {},
            "the following are missing inputs for check 9, read together rather than stopping at "
            "the first (#193 safeguard 3 requires reasoning about both control arms): "
            + ", ".join(missing),
            warnings,
        )

    # --- Lane C9: the buy-and-hold arm's own lifecycle (AC-4), read before any arithmetic --------
    if buy_hold_error is not None:
        # The container's own arm computation failed; the route already folded that failure into
        # the stored replay object rather than losing the candidate's own already-reproduced
        # replay (AC-1). Named by exactly the failure text the caller passed, never guessed.
        CHECK9_BUY_HOLD_MONITOR_COUNTERS["buy_hold_arm_compute_failed"] += 1
        return CheckResult(
            9, NOT_EVALUABLE, {},
            f"the buy-and-hold arm's own computation failed inside the container: {buy_hold_error}",
            warnings,
        )

    random_arm_quantities = None
    if random_arm_result is not None:
        bar_row = judgedb.read_threshold(conn, 9, "random_arm_pass_bar", battery_version)
        count_row = judgedb.read_threshold(conn, 9, "random_arm_replication_count", battery_version)
        frozen_m = int(count_row["value_numeric"])
        arm_k, arm_m = int(random_arm_result["k"]), int(random_arm_result["m"])
        if arm_m != frozen_m:
            return CheckResult(
                9, NOT_EVALUABLE, {},
                f"the random-entry arm used {arm_m} permutations but the frozen replication count is "
                f"{frozen_m} (#421 item 1): a different count is not graded",
                warnings,
            )
        arm_p = _mcpt_pvalue(arm_k, arm_m)
        arm_bar = bar_row["value_numeric"]
        arm_verdict_word = _mcpt_verdict(arm_p, arm_bar, bar_row["comparison"])
        random_arm_quantities = {
            "k": arm_k, "m": arm_m, "p_value": arm_p, "random_arm_pass_bar": arm_bar,
            "original_statistic": random_arm_result.get("original_statistic"),
            "seed": random_arm_result.get("seed"),
            "random_arm_verdict": "passed" if arm_verdict_word == CLEARED else "failed",
        }
        if arm_verdict_word != CLEARED:
            return CheckResult(
                9, FAILED, random_arm_quantities,
                f"the random-entry arm's p-value {arm_p:.6f} ({arm_k} of {arm_m} random pairings of the "
                f"candidate's positions with detrended next-day moves tied or beat the real pairing, "
                f"Masters' (k+1)/(m+1) form) does not beat the random_arm_pass_bar of {arm_bar} "
                "(#421 item 1, strictly below): check 9 fails when either control arm fails (#193 safeguard 3)",
                warnings,
            )

    if buy_hold_bar_returns is None:
        # A replay computed before this build (or whose own upload otherwise never carried the
        # arm's series) has no arm artifact at all: never a guessed value (AC-4). Also reached by
        # every caller that supplies no buy-and-hold information at all (the reserve-exam path,
        # which has no replay concept to begin with, and every pre-C9 caller/probe that still
        # calls this function with its original five-argument shape): the SAME regression guards
        # this lane must keep green (judge_values, judge_battery_2) read this exact reason for
        # that shape, so both arms are named here, matching the pre-C9 escalation's own wording
        # ("not implemented") alongside AC-4's own exact required phrase.
        return CheckResult(
            9, NOT_EVALUABLE, {},
            "the buy-and-hold arm is absent for this replay; replay the cell again. The "
            "random-entry arm's own comparison is likewise not implemented in this build (#193): "
            "both control arms' comparisons are unbuilt until Tamer ratifies the random-entry "
            "arm's own statistic",
            warnings,
        )

    if bar_returns_dates is None or buy_hold_bar_returns_dates is None:
        # Both series exist but at least one carries no dates to pair by: the caller's own contract
        # is violated (routes/judge.py always reads both date lists off the stored artifacts
        # alongside the returns), never silently paired by list position (F4).
        return CheckResult(
            9, NOT_EVALUABLE, {},
            "the candidate's or the buy-and-hold arm's own bar dates are missing, so the two "
            "daily return series cannot be paired by date",
            warnings,
        )

    candidate_over, arm_over, overlapping_day_count = _dated_series_intersection(
        bar_returns_dates, bar_returns, buy_hold_bar_returns_dates, buy_hold_bar_returns)

    if overlapping_day_count < 2:
        CHECK9_BUY_HOLD_MONITOR_COUNTERS["buy_hold_series_no_overlap"] += 1
        return CheckResult(
            9, NOT_EVALUABLE, {},
            "the candidate's and the buy-and-hold arm's own daily return series share fewer than "
            f"two overlapping dates ({overlapping_day_count}); a Sharpe ratio needs at least two "
            "paired observations",
            warnings,
        )

    run_row = conn.execute(
        "SELECT annualization_factor FROM research_runs WHERE id = ?;", (run_id,),
    ).fetchone()
    ann_factor = run_row["annualization_factor"] if run_row is not None else None
    if ann_factor is None or ann_factor <= 0.0:
        return CheckResult(
            9, NOT_EVALUABLE, {},
            "annualization_factor is the missing input: research_runs carries no positive "
            "observed-bars-per-year factor for this run, so neither Sharpe ratio can be "
            "annualised",
            warnings,
        )

    candidate_sharpe = _sharpe_ratio(candidate_over, ann_factor)
    buy_hold_sharpe = _sharpe_ratio(arm_over, ann_factor)
    if candidate_sharpe is None or buy_hold_sharpe is None:
        # A zero standard deviation in either series over the shared dates: never a division by
        # zero (AC-4), named by exactly which condition fired.
        return CheckResult(
            9, NOT_EVALUABLE, {},
            "the candidate's or the buy-and-hold arm's own daily return series has a zero "
            "standard deviation over the shared dates; a Sharpe ratio is not computable",
            warnings,
        )

    sharpe_margin = candidate_sharpe - buy_hold_sharpe
    buy_hold_pass_bar = judgedb.read_threshold(
        conn, 9, "buy_hold_arm_pass_bar", battery_version)["value_numeric"]
    buy_hold_verdict = "passed" if sharpe_margin > buy_hold_pass_bar else "failed"
    quantities = {
        "candidate_sharpe": candidate_sharpe,
        "buy_hold_sharpe": buy_hold_sharpe,
        "sharpe_margin": sharpe_margin,
        "overlapping_day_count": overlapping_day_count,
        "buy_hold_verdict": buy_hold_verdict,
    }
    if random_arm_quantities is not None:
        quantities.update(random_arm_quantities)

    if buy_hold_verdict == "failed":
        # #193 safeguard 3: both control arms must pass, so a failing buy-and-hold arm fails the
        # whole check outright, whatever the still-unbuilt random-entry arm might have said.
        return CheckResult(
            9, FAILED, quantities,
            f"the buy-and-hold arm's own Sharpe margin ({sharpe_margin:.6f}) is at or below the "
            f"buy_hold_arm_pass_bar of {buy_hold_pass_bar} (#421 item 3): check 9 fails when "
            "either control arm fails (#193 safeguard 3)",
            warnings,
        )
    if random_arm_quantities is not None:
        return CheckResult(
            9, CLEARED, quantities,
            f"both control arms pass: the buy-and-hold arm's Sharpe margin {sharpe_margin:.6f} is above the "
            f"buy_hold_arm_pass_bar of {buy_hold_pass_bar}, and the random-entry arm's p-value "
            f"{random_arm_quantities['p_value']:.6f} beats the random_arm_pass_bar of "
            f"{random_arm_quantities['random_arm_pass_bar']} (#193 safeguard 3, #421)",
            warnings,
        )
    # The buy-and-hold arm passed, but the random-entry arm is still unbuilt (its own two
    # thresholds are frozen but its comparison logic is not implemented in this build): the lead's
    # own lean, logged in the criteria (AC-3), is that a single passing arm never clears the whole
    # check on its own, so this stays not-evaluable rather than cleared.
    return CheckResult(
        9, NOT_EVALUABLE, quantities,
        f"the buy-and-hold arm passed (Sharpe margin {sharpe_margin:.6f} above the "
        f"buy_hold_arm_pass_bar of {buy_hold_pass_bar}), but check 9 remains not-evaluable: "
        "the random-entry arm alone is unbuilt (#193 safeguard 3 requires both control arms to "
        "pass before check 9 can clear)",
        warnings,
    )
