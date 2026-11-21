"""dashboard/backend/stage_runner.py: lane SR (6.2a), the stage runner's first half.

Governing text: docs/plans/night/2026-09-12_set6a_night/PLAN.md section 8; docs/briefs/
dry_run_spec_brief.md section 5 and its acceptance criteria AC-1 to AC-15 (lines 850 to 896).
This file orchestrates ONE stage of the dry-run chain (screen, optimise or walkforward) over the
SAME tracker calls and the SAME dispatch_sweep the sweep route (dashboard/backend/routes/
research.py's dispatch_price_universe_sweep_route) already runs, reusing rather than
reimplementing either, per section 5.1's own reuse-not-reimplement instruction.

Runs in the backend process. Imports no research_lab ENGINE module (AC-10, SR-16) and never
vectorbtpro: the one named exception is research_lab.universe, a pure function with no engine
import of its own, used only by build_screen_universe below for AC-15's own screen-stage draw.

This file adds no table and takes no schema decision of its own: the DDIA read section 5.1 of
the brief owes is explicitly NOT discharged here (the brief's own words: "The DDIA read this
choice owes is NOT discharged here"), because docs/books/kleppmann_canon.md is gitignored and a
worktree cannot see it. docs/units/SR/reads/read_1.md is where the lead states the chapter and
the verdict, against the canon, in the main checkout, per section 8's own instruction.
"""
from __future__ import annotations  # defers every annotation to a string, so "int | None" reads fine on any pinned Python

import json  # reads the pinned sizing spec text (P2j) for its estimator_window
import ast  # reads a strategy's own baked quantile constant (QUANTILE_WINDOW_BARS from 2026-09-20 onward, or the retired QUANTILE_MIN_BARS a pre-lane row still carries) from its stored TEXT, never by importing or executing it
import os  # reads the daily-store environment key live, on every call, never cached at import time
import tomllib  # GV2-7b: reads a theory file's name from its text (standard library; the form itself is checked by the container)
from datetime import datetime, timezone  # converts an epoch-second window bound to the store's own plain date form
from pathlib import Path  # the daily store's own directory, and every per-ticker parquet file inside it

import pandas as pd  # reads a ticker's own 'date' column; the same library research_lab.universe already uses for parquet

from dashboard.backend import researchdb, theory_strategy, tracker  # the SAME two modules the sweep route already calls; no second implementation
from research_lab import universe as universe_rule  # the ONE permitted research_lab import (AC-10/SR-16); called via the module attribute so a caller's own patch is honoured

# The four words research_data_draws.stage's own CHECK constraint allows (researchdb.py's DDL),
# restated here so an unknown word is refused with a named, readable error before any row is
# written, rather than surfacing later as an opaque sqlite3.IntegrityError (AC-2).
STAGE_WORDS = ("screen", "optimise", "walkforward", "reserve")

# The fixed sizing lead-in, in bars: research_lab/strategies/helpers/sizing.py's own ruled
# default (DECISIONS #304 ruling 4, the 36-day exponential anchor), already sourced in
# docs/research_lab/parameter_provenance.md under sizing.estimator_window. This file may never
# import sizing.py (an engine module, AC-10/SR-16 forbid it), so the number is restated as a
# plain constant here rather than read off the class, matching the brief's own section 5.6
# instruction: "278 is stated as its own figure."
SIZING_LEAD_IN_BARS = 278

# The daily store's own environment key (SR-17). Named as a constant so every reader of this
# file, and the refusal message itself, uses one spelling.
_DAILY_STORE_ENV_KEY = "EODHD_DAILY_STORE"

# The store's own per-ticker filename suffix, matching research_lab.universe's identical
# convention (a small literal, not worth a cross-module import for).
_DAILY_PARQUET_SUFFIX = ".US.parquet"

# One named failure mode from the plan's own "what can break" list (section 8): how many times
# this process has refused a stage for want of its fixed lead-in (AC-13). A process-local
# counter, not a database row, because a LeadInUnavailable refusal fires BEFORE any bar of the
# stage's own draw could be recorded against it: the draw itself was already granted by
# request_draw, and research_data_draws has no event kind for "refused after the gate, before
# dispatch, for a warmup reason", so there is no durable row this signal could query instead.
# Matches researchdb.py's own SWEEP_GUARD_REFUSAL_COUNTERS idiom: a plain dict, read by an
# ops-monitor tick that is itself owed to the operations page (October, #300).
STAGE_RUNNER_MONITOR_COUNTERS = {
    "lead_in_refusals": 0,
    # Lane WF (6.2b), registry row walkforward_refusals_by_reason: a reason-string keyed dict
    # (never a single scalar), since the plan names two distinct refusal classes together (AC-36's
    # window-too-small refusal and a NULL walkforward.* parameter) plus this lane's own
    # empty-universe refusal; a nested dict lets one registry row report the whole breakdown.
    #
    # Fold item 5 (round 1, SHOULD-FIX LC4): this dict is PROCESS-LOCAL and in-memory, exactly like
    # every other counter in this module -- it resets on every backend restart and holds no history
    # across processes. An ABSENT reason key means zero refusals for that reason SINCE PROCESS
    # START, never "never checked" or "cannot happen": a reason bucket is created on its own first
    # occurrence (_record_walkforward_refusal below), so a key genuinely missing from this dict is
    # the same fact as a key present with value 0, just not yet observed this process. A reader
    # should expect (not assume closed to) FIVE keys today: window_too_small (AC-36, dispatch_stage,
    # and also the container's own post-buffer refusal, round 3 fold, counted the same way since
    # both name the same underlying fact), parameter_absent (a NULL walkforward.* setting,
    # dispatch_stage), empty_universe (the screen stage's own draw, dispatch_stage),
    # lookback_undeclared (a strategy with no lookback_bars override, checked BOTH in-process in
    # dispatch_stage, round 2 fold, and from the container's own guard via
    # routes/research.py's worker_run_failed, fold item 3, since that refusal fires inside a
    # separate process this dict cannot reach directly), and no_out_of_sample_leg (round 4 fold,
    # B1(a): the container's chained curve came out empty or non-finite, counted the same way).
    "walkforward_refusals_by_reason": {},
}


def _record_walkforward_refusal(reason: str) -> None:
    """Lane WF (6.2b): bump the named reason bucket in walkforward_refusals_by_reason by one,
    creating the bucket on its first occurrence rather than requiring every reason word to be
    pre-declared here (a fresh, unanticipated reason still counts, never silently dropped)."""
    bucket = STAGE_RUNNER_MONITOR_COUNTERS["walkforward_refusals_by_reason"]
    bucket[reason] = bucket.get(reason, 0) + 1


class UnknownStageWord(ValueError):
    """AC-2: a stage word outside STAGE_WORDS is refused before any row is written. A ValueError
    subclass on purpose: this is a caller-contract violation, the same family as a malformed
    param_grid, never a database-layer surprise."""

    def __init__(self, stage: str) -> None:
        self.stage = stage  # the offending word, named for the caller
        super().__init__(
            f"{stage!r} is not one of the four stage words this chain accepts: {STAGE_WORDS}"
        )


class CandidateVersionMoved(RuntimeError):
    """AC-11: this chain pins ONE (strategy_id, strategy_version) pair for its whole run. A
    caller that already pinned a version and later finds the candidate has moved (an editor
    save bumped research_strategies.version) refuses to continue against the stale pin, rather
    than silently running a later stage against a strategy the earlier stages never tested."""

    def __init__(self, strategy_id: int, pinned_version: int, current_version: int) -> None:
        self.strategy_id = strategy_id  # the candidate this pin was for
        self.pinned_version = pinned_version  # the version the caller was still expecting
        self.current_version = current_version  # what research_strategies.version actually holds now
        super().__init__(
            f"strategy {strategy_id}'s pinned version {pinned_version} no longer matches its "
            f"current version {current_version}; refusing to continue the chain against a stale pin"
        )


class DailyStoreNotConfigured(RuntimeError):
    """SR-17: the daily EODHD store's own environment key is unset, blank, or does not resolve
    to a real directory. Never a substituted default: an unnamed store is exactly the state a
    silent fallback would hide."""


class LeadInUnavailable(RuntimeError):
    """AC-13: the fixed sizing lead-in cannot be satisfied for a ticker from the store this
    runner was pointed at. Refused BEFORE any dispatch, naming the ticker and the bars actually
    available, rather than letting the run abort inside the worker's own container on
    sizing.py's UndefinedVolatilityError (whose four trigger sites this refusal answers only the
    first of; AC-13's own second half is a regression guard on that file, left unchanged here)."""

    def __init__(self, ticker: str, bars_available: int, needed: int = SIZING_LEAD_IN_BARS) -> None:
        self.ticker = ticker  # the member whose own history came up short
        self.bars_available = bars_available  # the true count, so the message is self-contained
        super().__init__(
            f"the sizing lead-in cannot be satisfied for {ticker!r}: only {bars_available} bars "
            f"are available in the store before this stage's window, short of the "
            f"{needed}-bar lead-in the estimator needs; refusing before dispatch"
        )


class ChosenTickerNotQualifying(RuntimeError):
    """DECISIONS #434: a chain's walk-forward tests the chosen stock only, and only when the universe
    rule admits that stock for the walk-forward window (the same rule every stage's universe passes
    through: data check flags, lead-in coverage, held-out names). Refused before any draw is
    requested, naming the stock, the window and how many names the rule did admit."""

    def __init__(self, ticker: str, window_start_date: str, window_end_date: str, qualifying_count: int) -> None:
        self.ticker = ticker
        super().__init__(
            f"the chosen stock {ticker!r} is not among the {qualifying_count} names the universe rule "
            f"admits for the walk-forward window [{window_start_date}, {window_end_date}); "
            "refusing before any draw is requested"
        )


class WindowTooSmallForScheme(RuntimeError):
    """AC-36/WF-3: the walkforward stage's own window check. A granted window must hold at least
    one full slice under the configured scheme, in-sample plus at least two out-of-sample legs
    (AC-36's own words), or there is nothing for the entrypoint's own compute_slice_bounds to
    produce. Refused BEFORE any draw is requested (WF-3), naming both the true bar count the
    window actually holds and the floor the scheme needs, never a widened window substituted in
    its place."""

    def __init__(self, bars_held: int, bars_needed: int) -> None:
        self.bars_held = bars_held  # the true count, read from the store, never guessed
        self.bars_needed = bars_needed  # in_sample_bars + 2 * out_of_sample_bars, the scheme's own floor
        super().__init__(
            f"the granted window holds {bars_held} bars, short of the {bars_needed}-bar floor "
            "(in-sample plus at least two out-of-sample slices) this walkforward scheme needs; "
            "refusing before any draw is requested"
        )


class WarmupUnsatisfied(RuntimeError):
    """AC-14: the strategy's own baked derived.generator_quantile_min_bars is the BINDING
    warmup (it exceeds the fixed sizing lead-in), and the granted window does not hold enough
    bars to satisfy it before the first admissible entry."""

    def __init__(self, ticker: str, bars_available: int, bars_required: int) -> None:
        self.ticker = ticker  # the member whose own window came up short
        self.bars_available = bars_available  # what the window actually holds for this ticker
        self.bars_required = bars_required  # the strategy's own baked requirement
        super().__init__(
            f"the window granted for {ticker!r} holds {bars_available} bars, short of the "
            f"{bars_required} its own quantile warmup needs before the first admissible entry"
        )


class LookbackNotDeclared(RuntimeError):
    """Round-2 fold (n7-WF-4a1c), AC-16: the walkforward stage's own pre-dispatch lookback check.
    Plan section 7 item 4's own words: "the strategy's own lookback, the largest declared window
    parameter." A strategy's STORED SOURCE TEXT is parsed here (round 4 fold, code-quality pass 1
    finding SF1: an ast walk via _declares_lookback_bars below, never a plain substring search,
    which a docstring or comment quoting the method's own signature could satisfy; never imported,
    never executed: this file may never touch an engine module, AC-10/SR-16), so a strategy that
    never declared the method refuses BY NAME before any draw is requested, rather than reaching
    the container and refusing only after a slice's own compute has already started. The
    container's own guard (run_sweep_entrypoint.py's own NotImplementedError-to-ValueError
    translation) stays in place as the second line of defense, for whatever this ast walk cannot
    see (e.g. the method declared dynamically rather than as a plain `def`/`async def` line)."""

    def __init__(self, strategy_id: int, detail: "str | None" = None) -> None:
        self.strategy_id = strategy_id  # the candidate whose stored source names no lookback_bars
        message = (
            f"walkforward-lookback-undeclared: strategy {strategy_id}'s stored source declares no "
            "lookback_bars method; refusing before any draw is requested"
        )
        if detail is not None:  # round 4 fold SF1: a source that failed to even parse appends WHY
            message = f"{message} ({detail})"
        super().__init__(message)


def daily_store_path() -> Path:
    """SR-17: the daily EODHD store this file reads bars from, resolved strictly from its own
    named environment key, read live on every call. Never falls back to any other project
    default: config.py is lane RT's own file tonight (the plan's own section 2 lean), so this
    key is this file's own, independent knob until a later change moves it."""
    raw_value = os.environ.get(_DAILY_STORE_ENV_KEY)  # read live: a test that changes this mid-process must be honoured
    if not raw_value:  # unset, or set to an empty string: both read as "not configured"
        raise DailyStoreNotConfigured(
            f"the environment key {_DAILY_STORE_ENV_KEY} is not set; refusing to guess a daily "
            "store path rather than silently default to one"
        )
    resolved = Path(raw_value)  # the exact path named, no join and no rewrite
    if not resolved.is_dir():  # set, but not a real directory: the same refusal, a different reason
        raise DailyStoreNotConfigured(
            f"the environment key {_DAILY_STORE_ENV_KEY} names {raw_value!r}, which is not a "
            "directory; refusing to read bars from a path that is not a real store"
        )
    return resolved  # the exact, resolved directory


def _epoch_to_date_str(ts: int) -> str:
    """An epoch-second timestamp's own UTC calendar date, as the same YYYY-MM-DD string the
    daily store's own 'date' column carries, so the two can be compared directly as strings."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _bar_dates_for_ticker(store: Path, ticker: str) -> "list[str]":
    """Every ISO date string this ticker's own daily parquet file holds. A ticker the store
    does not hold at all reads as zero bars, never a crash: an absent file is exactly the state
    AC-13's own refusal exists to catch, not a reason to skip the check."""
    path = store / f"{ticker}{_DAILY_PARQUET_SUFFIX}"  # the store's own per-ticker filename convention
    if not path.is_file():  # no file for this ticker at all
        return []  # zero bars, not an error: the caller's own count-based checks handle this correctly
    frame = pd.read_parquet(path, columns=["date"])  # only the date column: this check never touches price data
    return sorted(frame["date"].tolist())  # a plain, sorted list of date strings


class AmbiguousBakedQuantileConstant(RuntimeError):
    """SK-F1 fold (lane SK round 2, 2026-09-20): a strategy's stored source defines BOTH the
    retired QUANTILE_MIN_BARS constant and the current QUANTILE_WINDOW_BARS constant (see
    _read_quantile_min_bars below). This should never happen from a single emit.py run (the
    template writes exactly one of the two names, never both), so a source carrying both is
    either hand-edited or hand-authored to imitate the generator's own shape. Refusing by name,
    carrying both baked values, is the honest response: picking one silently would be a guess
    about which the strategy actually means."""

    def __init__(self, min_bars_value: int, window_bars_value: int, strategy_id: "int | None" = None) -> None:
        self.min_bars_value = min_bars_value  # the retired name's own baked value
        self.window_bars_value = window_bars_value  # the current name's own baked value
        self.strategy_id = strategy_id  # the candidate this source came from, when the caller has it
        subject = f"strategy {strategy_id}" if strategy_id is not None else "this strategy's stored source"
        super().__init__(
            f"{subject} defines both the retired QUANTILE_MIN_BARS={min_bars_value} and the "
            f"current QUANTILE_WINDOW_BARS={window_bars_value}; refusing to guess which one is "
            "the strategy's real baked warmup floor rather than silently picking one"
        )


def _read_quantile_min_bars(source_code: str, strategy_id: "int | None" = None) -> "int | None":
    """AC-14's second warmup dimension: derived.generator_quantile_window_bars (renamed from
    derived.generator_quantile_min_bars by the SK-F1 fold, 2026-09-20, DECISIONS #396 ruling 1:
    the generator's own quantile threshold moved from an expanding window to a rolling one, and
    the module-level constant that carries its size was renamed to match). From lane SK (3.2b)
    onward, research_lab/generator/emit.py's own template bakes this as a module-level
    QUANTILE_WINDOW_BARS constant. A strategy row the generator emitted BEFORE that lane still
    carries the retired name QUANTILE_MIN_BARS instead: criterion 6's own lifecycle state, an
    already-generated candidate is never silently rewritten just because the template changed.
    That module is an engine module this file may never import (AC-10/SR-16), so whichever name
    is present is read by parsing the strategy's own stored source TEXT with ast, exactly like
    generated.py already reads a different constant the same way, never by importing or
    executing anything. Returns whichever ONE of the two names the source actually carries, or
    None for a hand-authored strategy that carries neither constant at all (AC-14 scenario A). A
    source carrying BOTH names raises AmbiguousBakedQuantileConstant by name rather than
    silently picking one; the function keeps its pre-fold name because the value it returns
    still plays the same AC-14 role (the caller's own quantile_min_bars variable, and the return
    dict's own "quantile_min_bars" key, name the ROLE, never the literal source-code token, so
    the role name did not need to change with the token search space)."""
    tree = ast.parse(source_code)  # parse only; never exec, matching the OPEN-4 boundary
    found_by_name: "dict[str, int]" = {}  # whichever of the two candidate constant names this source actually defines
    for node in ast.walk(tree):  # walk every node looking for either top-level assignment that matters
        is_a_candidate_constant = (
            isinstance(node, ast.Assign)  # a plain assignment statement
            and len(node.targets) == 1  # a single target, never a tuple/chained assignment
            and isinstance(node.targets[0], ast.Name)  # the target is a bare name, not an attribute or subscript
            and node.targets[0].id in ("QUANTILE_WINDOW_BARS", "QUANTILE_MIN_BARS")  # the current name, or the retired one
        )
        if is_a_candidate_constant and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            found_by_name[node.targets[0].id] = node.value.value  # record this name's own baked integer value
    if "QUANTILE_WINDOW_BARS" in found_by_name and "QUANTILE_MIN_BARS" in found_by_name:
        raise AmbiguousBakedQuantileConstant(
            found_by_name["QUANTILE_MIN_BARS"], found_by_name["QUANTILE_WINDOW_BARS"], strategy_id
        )
    if "QUANTILE_WINDOW_BARS" in found_by_name:
        return found_by_name["QUANTILE_WINDOW_BARS"]  # the current name, lane SK (3.2b) onward
    if "QUANTILE_MIN_BARS" in found_by_name:
        return found_by_name["QUANTILE_MIN_BARS"]  # the retired name, a row emitted before that lane
    return None  # neither constant: a hand-authored strategy, or one the generator never touched


def _declares_lookback_bars(source_code: str) -> bool:
    """Round-4 fold (n7-WF-4a1c), code-quality pass 1 finding SF1: dispatch_stage's own
    pre-dispatch lookback check used to be a plain substring test, `"def lookback_bars(" not in
    source_code`, which a DOCSTRING OR COMMENT quoting the method's own signature would satisfy
    even though the class never actually declares it. Reads the strategy's stored source TEXT with
    ast instead, the SAME idiom _read_quantile_min_bars above already uses (never imports or
    executes it, AC-10/SR-16): True only when some ast.ClassDef in the parsed tree contains an
    ast.FunctionDef or ast.AsyncFunctionDef named lookback_bars in its own body. Lets a plain
    SyntaxError propagate on a source that fails to parse at all: dispatch_stage folds that into
    its own LookbackNotDeclared refusal (a strategy this file cannot even parse certainly does not
    PROVABLY declare the method), appending the parser's own text so the reason is visible rather
    than swallowed."""
    tree = ast.parse(source_code)  # parse only; never exec. SyntaxError propagates to the caller.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:  # a method must be a DIRECT member of the class body, never a nested def
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "lookback_bars":
                    return True
    return False


def _check_warmups(strat_row: dict, universe_id: int, *, lead_in_bars: int = SIZING_LEAD_IN_BARS) -> dict:
    """AC-13 (the fixed lead-in) and AC-14 (the strategy's own baked quantile floor) together:
    reads every member's own dates ONCE, checks the lead-in for each unconditionally (AC-13's
    own wording), then checks the window's own bar count against the baked constant only when
    that constant actually exceeds the fixed lead-in (AC-14's four scenarios).

    Round-2 finding 1 (docs/units/SR/reads read_1.md, DDIA chapter 8 atomicity): this now runs
    BEFORE tracker.request_draw, not after, so a lead-in/quantile refusal never leaves behind a
    granted draw that already consumed the candidate's data budget for a stage that never
    dispatched (a plain read over research_datasets and the store needs the draw to exist for
    nothing). Round-1's own docstring here leaned on request_draw having already proven the
    window's two bounds non-NULL; with the order reversed that is no longer true, so THIS
    function checks for a missing universe row or a NULL bound itself, by name, rather than
    crash on None. It does NOT raise its own error for that case: it returns a neutral result
    and lets request_draw (called right after this function returns, unchanged) be the one,
    already-tested source of DrawWindowMissing AND the durable refusal row AC-9 asserts on. Two
    independent reasons rule out raising here instead: (1) request_draw's own row-write is the
    ONLY code that can produce that row, so raising here would leave AC-9 with no row to find;
    (2) AC-9's own fixtures never configure EODHD_DAILY_STORE at all, so calling
    daily_store_path() before confirming the window is real would raise DailyStoreNotConfigured
    in place of DrawWindowMissing even before the None-bound question arises. daily_store_path()
    is therefore called ONLY once the window is confirmed real, below."""
    with researchdb.get_conn() as conn:  # a plain read, no lock: the window this stage would be granted
        window_row = conn.execute(
            "SELECT window_start_ts, window_end_ts FROM research_datasets WHERE id = ?;",
            (universe_id,),
        ).fetchone()
    window_missing = (  # a nonexistent universe row, or a real row with either bound NULL
        window_row is None
        or window_row["window_start_ts"] is None
        or window_row["window_end_ts"] is None
    )
    if window_missing:
        # Not this function's own refusal to raise (see the docstring above): a neutral,
        # never-consumed placeholder, since request_draw (called next) already raises
        # DrawWindowMissing, with its own durable row, for exactly this case.
        return {"quantile_min_bars": None, "binding": None}

    window_start_date = _epoch_to_date_str(window_row["window_start_ts"])  # the granted window's own start, as a date
    window_end_date = _epoch_to_date_str(window_row["window_end_ts"])  # and its own end, half-open on this side

    store = daily_store_path()  # SR-17: only reached once the window above is confirmed real
    members = researchdb.list_price_universe_members(universe_id)  # every ticker this stage's own universe names
    dates_by_ticker = {t: _bar_dates_for_ticker(store, t) for t in members}  # read the store ONCE per ticker, reused below
    quantile_min_bars = _read_quantile_min_bars(strat_row["source_code"], strat_row.get("id"))  # the baked constant (either name), or None for a hand-authored strategy

    for ticker, dates in dates_by_ticker.items():  # AC-13: the fixed lead-in binds unless the baked quantile is stricter
        bars_before_window = sum(1 for d in dates if d < window_start_date)  # strictly earlier than the window
        if bars_before_window < lead_in_bars:
            STAGE_RUNNER_MONITOR_COUNTERS["lead_in_refusals"] += 1  # the registry's own stage_runner_lead_in_refusals row reads this
            raise LeadInUnavailable(ticker, bars_before_window, lead_in_bars)  # naming the ticker and the true count (AC-13)

    binding = "lead_in"  # the fixed figure binds unless the strategy's own constant below is stricter
    if quantile_min_bars is not None and quantile_min_bars > lead_in_bars:
        binding = "quantile"  # the baked constant is now the larger, controlling requirement
        for ticker, dates in dates_by_ticker.items():  # AC-14: the window itself must hold enough bars for it
            bars_in_window = sum(1 for d in dates if window_start_date <= d < window_end_date)  # half-open, matching filter 4's own convention
            if bars_in_window < quantile_min_bars:
                raise WarmupUnsatisfied(ticker, bars_in_window, quantile_min_bars)  # naming the ticker, what the window holds and needs

    return {"quantile_min_bars": quantile_min_bars, "binding": binding}  # AC-14's own report shape


def _check_walkforward_window(universe_id: int, wf_params: dict) -> int:
    """AC-36/WF-3: the walkforward stage's own two checks, run AFTER _check_warmups (whose own
    per-ticker loop is already a harmless no-op on an empty universe, by its own docstring) and
    BEFORE tracker.request_draw, so either refusal here writes no draw row at all.

    First, an empty universe refuses by the SAME name dispatch_sweep would raise anyway deep
    inside its own transaction (researchdb.PriceUniverseEmptyError): raised here instead so it
    fires before any draw is granted, never a second exception class for one condition.

    Second, the window itself must hold at least one full slice under the configured scheme
    (in_sample_bars plus at least two out_of_sample_bars, AC-36's own floor). Bars are counted the
    same half-open way _check_warmups already counts them, over the SAME store; the worst-case
    member (the one holding the fewest in-window bars) is what the scheme must actually work for,
    so that member's count is what gets compared against the floor.

    A missing or NULL-bound window is left alone, exactly like _check_warmups's own precedent: it
    returns a neutral zero rather than raising, so request_draw's own DrawWindowMissing stays the
    one, already-tested source of that refusal and its durable row.

    Returns the true bars-held count, for a caller that wants to report it; it is never used to
    widen or default the window itself.
    """
    members = researchdb.list_price_universe_members(universe_id)  # every ticker this stage's own universe names
    if not members:  # no-fallback: an empty universe cannot be walked forward either
        with researchdb.get_conn() as conn:
            urow = conn.execute(
                "SELECT name FROM research_datasets WHERE id = ?;", (universe_id,),
            ).fetchone()
        universe_label = urow["name"] if urow is not None else universe_id  # a real name when the row exists, the bare id otherwise
        _record_walkforward_refusal("empty_universe")  # the registry's own walkforward_refusals_by_reason row reads this
        raise researchdb.PriceUniverseEmptyError(
            f"price universe {universe_id} ({universe_label!r}) has no members"
        )

    with researchdb.get_conn() as conn:  # a plain read, no lock: the same window _check_warmups already reads
        window_row = conn.execute(
            "SELECT window_start_ts, window_end_ts FROM research_datasets WHERE id = ?;",
            (universe_id,),
        ).fetchone()
    window_missing = (
        window_row is None
        or window_row["window_start_ts"] is None
        or window_row["window_end_ts"] is None
    )
    if window_missing:  # left for request_draw's own DrawWindowMissing refusal, never raised here
        return 0

    window_start_date = _epoch_to_date_str(window_row["window_start_ts"])  # the granted window's own start, as a date
    window_end_date = _epoch_to_date_str(window_row["window_end_ts"])  # and its own end, half-open on this side
    store = daily_store_path()  # SR-17: only reached once the window above is confirmed real

    bars_needed = wf_params["in_sample_bars"] + 2 * wf_params["out_of_sample_bars"]  # AC-36's own floor, never less
    bars_held = None  # the worst-case member's own in-window bar count, computed across every member
    for ticker in members:
        dates = _bar_dates_for_ticker(store, ticker)  # this ticker's own store file, read once
        count = sum(1 for d in dates if window_start_date <= d < window_end_date)  # half-open, matching every other window check in this file
        if bars_held is None or count < bars_held:
            bars_held = count  # keep the smallest count seen so far: the scheme must work for every member
    if bars_held < bars_needed:
        _record_walkforward_refusal("window_too_small")  # AC-36's own refusal, counted by reason
        raise WindowTooSmallForScheme(bars_held, bars_needed)  # refused before any draw is requested
    return bars_held


def dispatch_stage(strategy_id: int, universe_id: int, stage: str, calendar_id: str,
                   param_grid: dict, now: int, *, pinned_version: "int | None" = None,
                   pinned_sizing_method: "str | None" = None, pinned_sizing_spec: "str | None" = None) -> dict:
    """Orchestrates ONE stage of the dry-run chain over an already-resolved price-universe row
    (build_screen_universe below creates the screen stage's own row; a later stage's own row is
    any caller's to create). Reuses the exact three tracker calls and the exact dispatch_sweep
    the sweep route already runs (section 5.1's own reuse-not-reimplement instruction), never a
    second implementation of either. record_bars_observed is deliberately never called here: it
    already fires from the existing worker result callbacks once link_run has named the run
    (AC-6), via tracker.draw_id_for_run.

    Round-2 finding 1 (DDIA chapter 8 atomicity): the warmup checks run BEFORE request_draw, not
    after. tracker.request_draw's own granted row is a committed transaction the instant it
    returns, so a lead-in/quantile refusal raised AFTER it would leave a granted, budget-
    consuming draw behind for a stage that never actually dispatched. _check_warmups is a pure
    read over research_datasets and the daily store; nothing in it needs the draw to exist, so
    running it first means such a refusal now writes no row at all. The remaining gap between a
    granted draw and dispatch_sweep (any OTHER reason dispatch_sweep might fail) is the
    pre-existing shape the sweep route already has, watched by the tracker's own
    granted_draws_without_run_link query (docs/units/SR/monitor_rows.md)."""
    if stage not in STAGE_WORDS:  # AC-2: refused before any row is written, a plain input check
        raise UnknownStageWord(stage)
    if (pinned_sizing_method is None) != (pinned_sizing_spec is None):  # P2h: the sizing pin is both keywords or neither
        raise ValueError("pinned_sizing_method and pinned_sizing_spec are given together or not at all")
    # P2h: the reserve exam's pin (a graded run's recorded sizing); empty for every other caller, so their
    # dispatch_sweep calls below are exactly today's.
    pin = ({} if pinned_sizing_method is None
           else {"pinned_sizing_method": pinned_sizing_method, "pinned_sizing_spec": pinned_sizing_spec})

    strat_row = researchdb.get_strategy(strategy_id)  # a plain read; not a bar, and not a lock
    if strat_row is None:  # an unknown strategy id is not this function's own error to invent
        return researchdb.dispatch_sweep(strategy_id, universe_id, param_grid, **pin)  # dispatch_sweep raises its own named StrategyNotFound/StrategyDeleted
    current_version = strat_row["version"]  # the candidate's own current version, pinned for the whole chain (AC-11)
    if pinned_version is not None and pinned_version != current_version:  # AC-11: a caller pinned an earlier version and it has since moved
        raise CandidateVersionMoved(strategy_id, pinned_version, current_version)

    # RS1: every refusal dispatch_sweep raises before its first write (grid keys, a missing declared
    # parameter, the size cap, the universe, the asset class) is raised here too, before the warmup
    # reads and before request_draw, so a refused grid burns no data window. Reads no store.
    researchdb.check_sweep(strategy_id, universe_id, param_grid)

    # Both warmup requirements (AC-13, AC-14), checked BEFORE any draw is requested (round-2
    # finding 1): a refusal here writes no row at all. A window this read cannot confirm as real
    # (missing row, or a NULL bound) is deliberately left for request_draw's own gate, next.
    # P2j: a pinned spec's own estimator_window is the lead-in this exam's container will load, so it is the floor
    # (unpinned callers keep SIZING_LEAD_IN_BARS).
    warmup_kwargs = ({} if pinned_sizing_spec is None
                     else {"lead_in_bars": int(json.loads(pinned_sizing_spec)["estimator_window"])})
    warmup = _check_warmups(strat_row, universe_id, **warmup_kwargs)

    # WF-1/AC-36/WF-3 (lane WF, 6.2b): the walkforward stage's own ruled parameters and window
    # checks, run only for that one stage word, still strictly before any draw is requested. The
    # ruled values live in research_settings (WalkforwardParameterAbsent's own refusal if they are
    # ever blanked), never re-typed as a literal here.
    wf_params = None  # every other stage passes this straight through as None: no walkforward block at all
    if stage == "walkforward":
        try:
            wf_params = researchdb.read_walkforward_parameters()  # the three ruled #392 values, read live
        except researchdb.WalkforwardParameterAbsent:
            _record_walkforward_refusal("parameter_absent")  # counted here, in this lane's own file, then re-raised unchanged
            raise
        _check_walkforward_window(universe_id, wf_params)  # empty-universe and window-floor refusals, before any draw
        # Round-2 fold (n7-WF-4a1c), AC-16: the strategy's own declared lookback, read from its
        # STORED SOURCE TEXT (strat_row, already held above), never imported (AC-10/SR-16 forbid
        # this file touching any engine module). Round-4 fold, code-quality pass 1 finding SF1:
        # an ast walk (_declares_lookback_bars) rather than a plain substring search, which a
        # docstring or comment quoting the method's own signature could satisfy. Refusing before
        # any draw is requested rather than only once the container has already started a slice.
        # Checked LAST of the three walkforward-only gates (parameters, window/universe, lookback)
        # so an empty-universe or too-small-window dispatch still refuses with ITS OWN named
        # reason, never masked by this one.
        try:
            declares_lookback = _declares_lookback_bars(strat_row["source_code"])
        except SyntaxError as exc:  # SF1: a source that does not even parse cannot PROVABLY declare the method
            _record_walkforward_refusal("lookback_undeclared")
            raise LookbackNotDeclared(strategy_id, detail=f"source failed to parse: {exc}") from exc
        if not declares_lookback:
            _record_walkforward_refusal("lookback_undeclared")
            raise LookbackNotDeclared(strategy_id)

    # Transaction 1 (AD7.1): the chronology gate, strictly before any bar of this stage is read (AC-1).
    draw_id = tracker.request_draw(strategy_id, current_version, calendar_id, stage, universe_id, now)

    # Transaction 2: the dispatch itself, entirely outside any transaction this module opens
    # (tracker.py's own docstring: dispatch_sweep cannot run inside another already-open transaction).
    # The walkforward block rides along only for that one stage (AC-18): the sweep entrypoint is the
    # ONLY place that actually runs the split (#329 ruling 1), so this is a single, ordinary dispatch
    # carrying one extra field, never one dispatch per slice.
    if wf_params is not None:
        result = researchdb.dispatch_sweep(strategy_id, universe_id, param_grid, walkforward=wf_params, **pin)
    else:
        result = researchdb.dispatch_sweep(strategy_id, universe_id, param_grid, **pin)

    # Transaction 3 (AD7.3): an appended, later event, now that the run this draw was granted for exists.
    tracker.link_run(draw_id, result["run_id"], now)

    result["warmup"] = warmup  # AC-14: report which dimension bound, alongside dispatch_sweep's own fields
    return result


def build_screen_universe(name: str, window_start_ts: int, window_end_ts: int, calendar_id: str) -> dict:
    """AC-15's own screen-stage draw: the universe rule decides which names qualify for this
    window, then the qualifying set becomes a real price-universe row through the SAME write
    path every hand-built universe already uses (researchdb.create_price_universe), so
    dispatch_stage can run over its result exactly like any other universe_id. Kept SEPARATE
    from dispatch_stage (the plan's own interface note) so this function's own FL-gated
    real-store proof (AC-15's REAL half) never blocks AC-1 to AC-14's fixture proofs, none of
    which ever call this function. Propagates every universe_rule refusal (including
    UniverseParameterOwed) UNCHANGED: no default is ever substituted for an OWED register value
    (AC-15's own fixture half, R-U6)."""
    window_start_date = _epoch_to_date_str(window_start_ts)  # the universe rule's own date-string contract
    window_end_date = _epoch_to_date_str(window_end_ts)
    store = daily_store_path()  # SR-17: the same named, no-fallback store this whole file reads
    qualifying = universe_rule.universe(  # called through the MODULE attribute, so a test's own patch of universe_rule.universe is honoured
        window_start_date, window_end_date, store, calendar_id=calendar_id,
    )
    return researchdb.create_price_universe(  # the same write path every hand-built universe already uses
        name, sorted(qualifying), window_start_ts=window_start_ts, window_end_ts=window_end_ts,
        # 6.6: these names are the store's own filename spelling (universe_rule reads them off
        # _store_codes, verbatim), never a typed guess, so they must be validated and stored
        # EXACT, byte for byte, or this file's own _bar_dates_for_ticker below will rebuild a
        # path that does not exist on a case-sensitive filesystem.
        store_derived=True,
    )


def build_walkforward_universe(name: str, window_start_ts: int, window_end_ts: int, calendar_id: str,
                               ticker: str) -> dict:
    """DECISIONS #434: a chain's walk-forward universe is the chosen cell's stock alone. The walk-forward
    engine (run_sweep_entrypoint._process_walkforward) chains every member's out-of-sample curve into
    one equity curve, which is right for one stock and wrong for many, and the judge grades one cell.
    The universe rule still decides admission, over the walk-forward window, exactly as
    build_screen_universe applies it; a chosen stock it does not admit refuses by name
    (ChosenTickerNotQualifying), never a substitute. Written through the same create_price_universe
    path, so dispatch_stage runs over it like any other universe_id."""
    window_start_date = _epoch_to_date_str(window_start_ts)
    window_end_date = _epoch_to_date_str(window_end_ts)
    store = daily_store_path()
    qualifying = universe_rule.universe(  # the MODULE attribute, as build_screen_universe calls it
        window_start_date, window_end_date, store, calendar_id=calendar_id,
    )
    if ticker not in qualifying:
        raise ChosenTickerNotQualifying(ticker, window_start_date, window_end_date, len(qualifying))
    return researchdb.create_price_universe(
        name, [ticker], window_start_ts=window_start_ts, window_end_ts=window_end_ts,
        store_derived=True,  # the chosen ticker came off the store's own filename spelling (6.6)
    )


def dispatch_theory_stage(stage: str, universe_id: int, now: int, *, search_run_id: "int | None" = None,
                          variations: "list | None" = None, theory_text: "str | None" = None, calendar_id: "str | None" = None,
                          group_label: "str | None" = None, data_start: "str | None" = None, search: "dict | None" = None,
                          costs: "dict | None" = None, init_cash: "float | None" = None) -> dict:
    """Lane GV2-7b (DECISIONS #445): ONE stage of a generator v2 theory run, through the same three tracker calls as dispatch_stage:
    request_draw, then researchdb.dispatch_theory_run, then link_run.

    stage 'screen' is the search: the caller gives `theory_text`, `calendar_id` (no default, as StageDispatchIn requires),
    `group_label`, `data_start`, `search`, `costs` and `init_cash`. It makes the theory's strategy row ONCE (theory_strategy), and
    that row's id and version and the calendar id ride the search run's payload. Stages 'optimise' and 'walkforward' give
    `search_run_id` and the `variations` and nothing else: the strategy, its version, the calendar id and the theory's settings are
    read back from the search run's payload, so all stages of one theory run share one strategy row, one version and one calendar.
    A theory edited since the search (the strategy's version moved) is CandidateVersionMoved. A re-run of a theory needs a new
    calendar id: the draw gate refuses an earlier window on the same strategy, version and calendar.

    Every refusal that needs no write (researchdb.check_theory_dispatch: the arguments, S, the members' datasets; the walk-forward
    parameters) is raised BEFORE request_draw, so it leaves no granted draw behind. The warmup and window checks of dispatch_stage read
    the EODHD daily store: a universe of real stocks (research_datasets.source not 'synthetic') gets them here too, after
    check_theory_dispatch and before the strategy row and the draw (GV3-W1), so a refusal writes nothing. A made-up universe has no
    store files and skips them; the stage's container refuses a window too small for its scheme.
    """
    if stage not in STAGE_WORDS:
        raise UnknownStageWord(stage)
    if stage == "reserve":
        raise UnknownStageWord("reserve: a theory run has no reserve stage here")
    with researchdb.get_conn() as conn:  # GV3-W1: a universe of real stocks gets the store checks below; a made-up one has no store files
        universe_row = conn.execute("SELECT source FROM research_datasets WHERE id = ?;", (universe_id,)).fetchone()
    real_stocks = universe_row is not None and universe_row["source"] != "synthetic"  # a missing universe is left to request_draw's own refusal
    if stage == "screen":
        if search_run_id is not None or variations is not None:
            raise researchdb.TheoryRunInvalid("the search (stage screen) takes no search_run_id and no variations")
        given = {"theory_text": theory_text, "calendar_id": calendar_id, "group_label": group_label, "data_start": data_start,
                 "search": search, "costs": costs, "init_cash": init_cash}
        missing = [k for k, v in given.items() if v is None]
        if missing:
            raise researchdb.TheoryRunInvalid(f"the search needs {missing}")
        try:
            theory_head = tomllib.loads(theory_text)["theory"]
            theory_name = theory_head["name"]
        except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise researchdb.TheoryRunInvalid(f"the theory text has no readable [theory] name: {exc!r}") from exc
        theory_display_name = theory_head.get("display_name")
        if not isinstance(theory_display_name, str) or not theory_display_name.strip():
            raise researchdb.TheoryRunInvalid("the theory needs a display_name in [theory]")
        source = theory_strategy.emit_theory_strategy(theory_name, theory_text)
        researchdb.check_theory_dispatch(stage, universe_id, calendar_id, group_label, data_start, costs, search=search)
        ciks = researchdb.theory_earnings_ciks(theory_text, universe_id)  # EA4: a refusal needing no write, so before the draw
        earnings = None if ciks is None else {"ciks": ciks, "knowledge_to": now}  # the search sees reports up to the moment it is dispatched
        if real_stocks:  # the same lead-in check dispatch_stage makes, before the strategy row and the draw exist
            _check_warmups({"source_code": source, "id": None}, universe_id)
        strategy_id, strategy_version, _action = researchdb.upsert_strategy(
            theory_strategy.strategy_name(theory_name), source, now, display_name=theory_display_name)
        run_args = {"theory_text": theory_text, "group_label": group_label, "data_start": data_start, "costs": costs,
                    "init_cash": init_cash, "search": search}
    else:
        stray = {"theory_text": theory_text, "calendar_id": calendar_id, "group_label": group_label, "data_start": data_start,
                 "search": search, "costs": costs, "init_cash": init_cash}
        given = [k for k, v in stray.items() if v is not None]
        if search_run_id is None or given:
            raise researchdb.TheoryRunInvalid(
                f"stage {stage!r} takes search_run_id and variations only; {given} are read from the search run's payload")
        p = researchdb.read_theory_search_payload(search_run_id)
        strategy_id, strategy_version, calendar_id = p["strategy_id"], p["strategy_version"], p["calendar_id"]
        strat_row = researchdb.get_strategy(strategy_id)
        if strat_row is None:
            raise researchdb.StrategyNotFound(f"strategy {strategy_id} not found")
        if strat_row["version"] != strategy_version:
            raise CandidateVersionMoved(strategy_id, strategy_version, strat_row["version"])
        run_args = {"theory_text": p["theory_toml"], "group_label": p["group_label"], "data_start": p["data_start"],
                    "costs": p["costs"], "init_cash": p["init_cash"], "variations": variations, "seed": p["seed"]}
        if stage == "walkforward":
            try:
                run_args["walkforward"] = researchdb.read_walkforward_parameters()
            except researchdb.WalkforwardParameterAbsent:
                _record_walkforward_refusal("parameter_absent")
                raise
        researchdb.check_theory_dispatch(stage, universe_id, calendar_id, run_args["group_label"], run_args["data_start"],
                                         run_args["costs"], variations=variations, walkforward=run_args.get("walkforward"), seed=p["seed"])
        ciks = researchdb.theory_earnings_ciks(run_args["theory_text"], universe_id)  # EA4
        earnings = None
        if ciks is not None:
            if "earnings" not in p:
                raise researchdb.TheoryRunInvalid(
                    f"the theory reads an earnings field but search run {search_run_id} has no earnings block to take the report cut-off from")
            earnings = {"ciks": ciks, "knowledge_to": p["earnings"]["knowledge_to"]}  # one set of reports for all stages
        if real_stocks:  # the lead-in check and, for the walk-forward, the window check dispatch_stage makes, before any draw
            _check_warmups({"source_code": strat_row["source_code"], "id": strategy_id}, universe_id)
            if stage == "walkforward":
                _check_walkforward_window(universe_id, run_args["walkforward"])

    draw_id = tracker.request_draw(strategy_id, strategy_version, calendar_id, stage, universe_id, now)
    result = researchdb.dispatch_theory_run(stage, strategy_id, universe_id, calendar_id, now=now, earnings=earnings, **run_args)
    tracker.link_run(draw_id, result["run_id"], now)
    result["draw_id"] = draw_id
    return result


def query_chain_rows_on_real_calendar(conn) -> "list[int]":
    """The registry's own stage_runner_chain_rows_on_real_calendar row, read by an ops-monitor
    tick that supplies its own connection (tracker.py's own query convention). Counts granted
    draw_requested rows for optimise, walkforward or reserve -- three stage words that, as this
    codebase stands, reach research_data_draws ONLY through this file's own dispatch_stage --
    carrying calendar_id = 'real'. 'screen' is deliberately excluded: it is ALSO the milestone's
    own real, single-run/sweep screen-stage draw (brief section 5.1), so a screen row under
    'real' is the expected, ordinary case, never a signal of anything wrong. Expected zero:
    nothing in this build ever dispatches an optimise/walkforward/reserve stage under the real
    calendar, so a nonzero reading here is either a caller error or a future milestone decision
    this row would need revisiting for."""
    rows = conn.execute(
        "SELECT id FROM research_data_draws WHERE event_kind = 'draw_requested' "
        "  AND decision = 'granted' AND calendar_id = 'real' "
        "  AND stage IN ('optimise', 'walkforward', 'reserve');"
    ).fetchall()
    return [r["id"] for r in rows]
