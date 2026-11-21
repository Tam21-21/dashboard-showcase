"""The search, round by round (lanes GV2-5b, DECISIONS #440 and #445; GV3-S1, #467 items 3 and 4, #470).

The theory's [search] table says how: values_per_number, round_size, best, max_rounds, all_below, keep.
Round 1: the box is the theory's ranges at about values_per_number values per number (narrow.first_box). A box with fewer valid
strategies than all_below is run whole; a bigger one is sampled, round_size strategies, balanced. Each strategy runs over the
whole stock group on the screen window (GV2-5a's run_group, exits at max hold H); those under 30 trades are dropped; the rest are
ranked by annualised Sharpe after costs. After round 1 the book's quality condition (quality.py) says whether the theory is suitable;
if not, the search stops there. Otherwise round r narrows around the best strategies of round r - 1 (Dolzhenko's rule, narrow.py)
and runs its box the same way, until a box was run whole, max_rounds is reached, or no strategy cleared 30 trades.
Every strategy of every round is a trial of the screen family; a strategy a box shares with an earlier round is the same trial and
is not run or counted twice.
Finalists (GV3-S2, #467 items 5 and 6, #470): the best `keep` strategies of all rounds that meet the quality condition (quality.select,
Dolzhenko pdfp 93). A calibrated theory gives each its stop and target from the O-U calibration (calibrate.py); a plain theory (its own
stop, target, max hold, sell signal or trailing stop) runs them as the theory says (one.exit_rules). Each is re-run on the optimise
window; those runs are trials of the optimise family. The pick is the best optimise-window Sharpe.

Strategies run one by one in a process or in a pool of worker processes; the results come back in the order asked, so the
files do not depend on the number of workers.
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import NamedTuple
from multiprocessing import get_context

import numpy as np
import pandas as pd

from research_lab.exits import otr
from research_lab.theories import form
from research_lab.theory_run import one, signals, simulate
from research_lab.theory_search import calibrate, narrow, quality, sample

MIN_TRADES = 30            # the coarse bar of step 1 (DECISIONS #440)
BEST_PERCENT = 5           # the share of the strategies clearing the bar that step 1 keeps (DECISIONS #440)
STEP_SCREEN_1, STEP_SCREEN_2, STEP_FINALIST = 1, 2, 3


class StepTimeout(RuntimeError):
    """A step ran past its deadline."""


def best_percent(table: pd.DataFrame, min_trades: int = MIN_TRADES, percent: int = BEST_PERCENT) -> pd.DataFrame:
    """The rows clearing `min_trades` trades, ranked by Sharpe, best first; the best ceil(percent of them) are kept.

    Ties go to the lower row. A row with no Sharpe (NaN) is never ranked. The count is whole-number arithmetic, never a float
    percent (0.05 * 60 is 3.0000000000000004 and would round up to 4).
    """
    rest = table[(table["num_trades"] >= min_trades) & table["sharpe"].notna()]
    count = -(-len(rest) * percent // 100)
    ranked = rest.assign(_row=np.arange(len(rest))).sort_values(["sharpe", "_row"], ascending=[False, True], kind="mergesort")
    return ranked.drop(columns="_row").iloc[:count]


def max_hold(theory, picks: dict) -> int:
    hold = theory.data["exit"].get("max_hold")
    if hold is None:
        raise ValueError("the search calibrates the stop and target over the max hold: the theory has no exit.max_hold")
    value = picks[hold] if isinstance(hold, str) else hold
    if int(value) != value:
        raise ValueError("max hold must be a whole number, got %r" % (value,))
    return int(value)


def is_calibrated(theory) -> bool:
    """True when the theory's exit.stop and exit.target are both "calibrated", False when neither is (the rule of
    research_lab.theory_stage.variation.is_calibrated); one of the two is refused."""
    table = theory.data["exit"]
    flags = [table.get(k, form.NONE) == form.CALIBRATED for k in ("stop", "target")]
    if any(flags) and not all(flags):
        raise ValueError('the theory calibrates only one of stop and target; the search needs both "calibrated" or neither')
    return all(flags)


def require_calibrated(theory) -> None:
    table = theory.data["exit"]
    for key in ("stop", "target"):
        if table.get(key) != form.CALIBRATED:
            raise ValueError('the search needs a theory whose exit.%s is "calibrated", got %r' % (key, table.get(key)))
    if table.get("expected_price") is None:
        raise ValueError("the search calibrates towards the theory's exit.expected_price; the theory names none")


# ---- running one strategy ------------------------------------------------------------------------------------------------

@dataclass
class Context:
    spec: dict
    theory: object
    frames: dict
    shas: dict


def load_context(spec: dict) -> Context:
    theory = form.load(spec["theory"])
    if is_calibrated(theory):
        require_calibrated(theory)
    frames, shas = one.load_group(spec)
    return Context(spec, theory, frames, shas)


def _fillable(entries: pd.DataFrame, frames: dict, window) -> bool:
    """True when some entry signal inside the window has a later bar of its stock to fill on (the fill is at the next open)."""
    index = frames["close"].index
    lo, hi = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    if index.tz is not None:  # a stored-shape dataset has a UTC index; read the window dates in it, as simulate.run_group does
        lo, hi = lo.tz_localize(index.tz), hi.tz_localize(index.tz)
    mask = np.asarray((index >= lo) & (index <= hi))
    sig = entries.to_numpy(dtype=bool)[mask]
    bars = np.stack([frames[f].to_numpy(dtype=float)[mask] for f in ("open", "high", "low", "close")])
    valid = ~np.isnan(bars).any(axis=0)
    later = valid.sum(axis=0) - np.cumsum(valid, axis=0)        # valid bars of the stock after each bar
    return bool((sig & (later > 0)).any())


def _no_trade_result(frames: dict, window, init_cash: float) -> "simulate.GroupResult":
    """What a strategy that never trades in the window is: cash flat, no trades, a Sharpe of NaN (zero variance).

    Since GV2-7a run_group handles zero trades itself. This path stays for a strategy with no fillable entry in the window
    (no entry signal with a later bar to fill on): it skips vbt altogether, which is cheaper, and answers from the same
    definitions (equity = the starting cash, bar returns 0, no Sharpe).
    """
    index = frames["close"].index
    lo, hi = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    if index.tz is not None:  # a stored-shape dataset has a UTC index; read the window dates in it, as simulate.run_group does
        lo, hi = lo.tz_localize(index.tz), hi.tz_localize(index.tz)
    mask = np.asarray((index >= lo) & (index <= hi))
    if not mask.any():
        raise ValueError("the window %s to %s holds no bar of the frames" % (window[0], window[1]))
    dates = index[mask]
    equity = pd.Series(float(init_cash), index=dates, name="equity")
    bar_returns = pd.Series(0.0, index=dates, name="bar_return")
    empty = pd.DataFrame()
    return simulate.GroupResult(
        equity=equity, trades=empty, orders=empty, bar_returns=bar_returns, sharpe=float("nan"), sharpe_per_bar=float("nan"),
        num_trades=0, num_open_at_end=0, max_open_at_once=0, total_return=0.0, max_drawdown=0.0, fees_paid=0.0, slippage_paid=0.0,
        window=(str(window[0]), str(window[1])))


def run_strategy(ctx: Context, picks: dict, window, exits=None) -> "simulate.GroupResult":
    entries = signals.entry_signals(ctx.theory, picks, ctx.frames)
    if exits is None:
        if is_calibrated(ctx.theory):
            exits = simulate.ExitRules(max_hold=max_hold(ctx.theory, picks))
        else:
            exits = one.exit_rules(ctx.theory, picks, {}, frames=ctx.frames)
    if not _fillable(entries, ctx.frames, window):
        return _no_trade_result(ctx.frames, window, ctx.spec["init_cash"])
    money = {"method": ctx.theory.data["money"]["method"], "init_cash": ctx.spec["init_cash"]}
    return simulate.run_group(ctx.frames, entries, exits, money, ctx.spec["costs"], tuple(window))


def _summary(res, init_cash: float) -> dict:
    sharpe = res.sharpe
    return {"sharpe": float(sharpe) if math.isfinite(sharpe) else float("nan"), "num_trades": int(res.num_trades),
            "bar_returns": res.bar_returns.to_numpy(dtype=np.float32), "dates": res.bar_returns.index,
            "metrics": quality.metrics(res, init_cash)}


_WORKER: dict = {}


def _worker_init(spec: dict) -> None:
    _WORKER["ctx"] = load_context(spec)


def _worker_run(job: tuple) -> dict:
    picks, window = job
    return _summary(run_strategy(_WORKER["ctx"], picks, window), _WORKER["ctx"].spec["init_cash"])


class Evaluator:
    """Runs strategies on a window and remembers every result, so a strategy asked twice is run once."""

    def __init__(self, spec: dict, ctx: Context):
        self.spec, self.ctx = spec, ctx
        self.workers = int(spec.get("workers", 1))
        if self.workers < 1:
            raise ValueError("workers must be at least 1, got %r" % (spec.get("workers"),))
        self.letters, _rules = sample.letters_and_rules(ctx.theory)
        self.cache: dict = {}
        self._pool = None

    def key(self, picks: dict, window) -> tuple:
        return (tuple(window), tuple(picks[l] for l in self.letters))

    def _pool_get(self):
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=get_context("spawn"),
                                             initializer=_worker_init, initargs=(self.spec,))
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def evaluate(self, rows: list, window, deadline=None) -> list:
        """The result of each row (a picks dict) on the window, in the order asked."""
        window = tuple(window)
        todo, seen = [], set()
        for row in rows:
            k = self.key(row, window)
            if k not in self.cache and k not in seen:
                seen.add(k)
                todo.append(row)
        if self.workers == 1:
            for done, row in enumerate(todo):
                if deadline is not None and time.monotonic() >= deadline:
                    raise StepTimeout("passed the deadline after %d of %d strategies" % (done, len(todo)))
                self.cache[self.key(row, window)] = _summary(run_strategy(self.ctx, row, window), self.spec["init_cash"])
        elif todo:
            pool = self._pool_get()
            futures = [pool.submit(_worker_run, (row, window)) for row in todo]
            for row, future in zip(todo, futures):
                try:
                    timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                    self.cache[self.key(row, window)] = future.result(timeout=timeout)
                except FutureTimeout:
                    for f in futures:
                        f.cancel()
                    self.close()
                    raise StepTimeout("passed the deadline with %d of %d strategies done" % (sum(1 for f in futures if f.done()), len(todo))) from None
        return [self.cache[self.key(row, window)] for row in rows]


# ---- the screen family: steps 1 and 2 ------------------------------------------------------------------------------------

@dataclass
class Trial:
    trial_id: int
    picks: dict
    window: str
    step: int
    sharpe: float
    num_trades: int
    bar_returns: np.ndarray
    dates: pd.DatetimeIndex
    round: int = 0                        # the search round that first ran it; 0 for an optimise-window trial
    closed: int = 0                       # the five quality numbers (quality.py); None where the number does not exist
    win_share: float | None = None
    max_drawdown: float | None = None
    avg_month: float | None = None
    recovery: float | None = None


def make_trial(trial_id: int, picks: dict, window: str, step: int, round_no: int, summary: dict) -> Trial:
    m = summary["metrics"]
    return Trial(trial_id, dict(picks), window, step, summary["sharpe"], summary["num_trades"], summary["bar_returns"], summary["dates"],
                 round_no, m.closed, m.win_share, m.max_drawdown, m.avg_month, m.recovery)


@dataclass
class Screen:
    trials: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    by_key: dict = field(default_factory=dict)


class Settings(NamedTuple):
    """A theory's [search] table, in the order of DECISIONS #470."""
    values_per_number: int
    round_size: int
    best: int
    max_rounds: int
    all_below: int
    keep: int


def settings(theory) -> Settings:
    table = theory.data.get("search")
    if table is None:
        raise ValueError("the search needs the theory's [search] table (#470); %s has none" % theory.path)
    return Settings(*(int(table[k]) for k in form.SEARCH_KEYS))


def _table(trials: list) -> pd.DataFrame:
    return pd.DataFrame({"sharpe": [t.sharpe for t in trials], "num_trades": [t.num_trades for t in trials],
                         "closed": [t.closed for t in trials], "win_share": [t.win_share for t in trials],
                         "max_drawdown": [t.max_drawdown for t in trials], "avg_month": [t.avg_month for t in trials],
                         "recovery": [t.recovery for t in trials]},
                        index=[t.trial_id for t in trials])


def _best_rows(results_rows: list, results: list, count: int) -> list:
    table = pd.DataFrame({"sharpe": [r["sharpe"] for r in results], "num_trades": [r["num_trades"] for r in results]})
    ranked = best_percent(table, percent=100).iloc[:count]
    return [results_rows[i] for i in ranked.index]


def _box_note(box: dict, theory, letters: list) -> dict:
    out = {}
    for letter in letters:
        int_valued = theory.picks[letter].int_valued
        axis = box[letter]
        out[letter] = {"from": sample.number(axis.lo, int_valued), "to": sample.number(axis.hi, int_valued),
                       "step": sample.number(axis.step, int_valued), "values": [sample.number(v, int_valued) for v in axis.values]}
    return out


def run_rounds(spec: dict, evaluator: Evaluator, deadline=None, bars: quality.Bars = quality.BOOK) -> Screen:
    """The rounds on the screen window. `bars` is a TEST HOOK (quality.py): the search itself never passes it.

    After round r the first of these that holds stops the search: r is 1 and the theory is not suitable ("not suitable"); the
    round ran every combination of its box ("ran every combination", round 1 too); r is max_rounds ("max rounds"); no strategy of
    the round cleared 30 trades ("no strategy cleared 30 trades"). screen.notes holds every round, the quality answer and the stop.
    """
    theory = evaluator.ctx.theory
    st = settings(theory)
    window = tuple(spec["screen_window"])
    seed = int(spec["seed"])
    letters, rules = sample.letters_and_rules(theory)
    screen = Screen()
    screen.notes = {"rounds": [], "quality": None, "stop": None}
    box, best = None, []
    for round_no in range(1, st.max_rounds + 1):
        if round_no == 1:
            box = narrow.first_box(theory, st.values_per_number)
        else:
            box = narrow.step2_box(theory, best, previous=box, limit=st.values_per_number)
        axes = {l: box[l].values for l in letters}
        valid = sample.grid_count(letters, axes, rules)
        if valid == 0:
            raise ValueError("round %d: the box holds no valid strategy" % round_no)
        if valid < st.all_below:
            rows = sample.grid_rows(letters, axes, rules)
        else:
            rows = sample.sample_axes(letters, axes, rules, st.round_size, seed, salt=(round_no,))
        whole = len(rows) == valid
        results = evaluator.evaluate(rows, window, deadline)
        step = STEP_SCREEN_1 if round_no == 1 else STEP_SCREEN_2
        before = len(screen.trials)
        for row, res in zip(rows, results):
            k = evaluator.key(row, window)
            if k not in screen.by_key:
                screen.by_key[k] = make_trial(len(screen.trials), row, "screen", step, round_no, res)
                screen.trials.append(screen.by_key[k])
        best = _best_rows(rows, results, st.best)
        screen.notes["rounds"].append({
            "round": round_no, "box": _box_note(box, theory, letters), "valid_strategies": int(valid),
            "ran": "all" if whole else "sample of %d" % len(rows), "strategies": len(rows), "new_trials": len(screen.trials) - before,
            "best_trial_ids": [screen.by_key[evaluator.key(row, window)].trial_id for row in best]})
        if round_no == 1:
            screen.notes["quality"] = quality.suitable(_table(screen.trials), bars)
        if round_no == 1 and screen.notes["quality"]["answer"] == "no":
            screen.notes["stop"] = "not suitable"
        elif whole:
            screen.notes["stop"] = "ran every combination"
        elif round_no == st.max_rounds:
            screen.notes["stop"] = "max rounds"
        elif not best:
            screen.notes["stop"] = "no strategy cleared %d trades" % MIN_TRADES
        if screen.notes["stop"] is not None:
            break
    return screen


# ---- finalists: the kept strategies, calibration and the optimise window ------------------------------------------------------

def _finite(value):
    return value if value is not None and math.isfinite(value) else None


def _entry_head(finalist: Trial, rank: int) -> dict:
    """What a kept strategy's finalists.json entry says of its screen run: rank, trial id, picks, step, round, Sharpe, trades and the five."""
    entry = {"rank": rank, "trial_id": finalist.trial_id, "picks": finalist.picks, "step": finalist.step, "round": finalist.round,
             "screen_sharpe": _finite(finalist.sharpe), "screen_num_trades": finalist.num_trades}
    for name in quality.NAMES:
        entry[name] = getattr(finalist, name)
    return entry


def _check_rerun(ctx: Context, spec: dict, finalist: Trial) -> "simulate.GroupResult":
    res = run_strategy(ctx, finalist.picks, spec["screen_window"])
    if not (res.sharpe == finalist.sharpe or (math.isnan(res.sharpe) and math.isnan(finalist.sharpe))):
        raise RuntimeError("finalist %d: re-running it on the screen window gave Sharpe %r, the search had %r"
                           % (finalist.trial_id, res.sharpe, finalist.sharpe))
    return res


def _optimise_result(opt) -> dict:
    return {"sharpe": _finite(opt.sharpe), "num_trades": int(opt.num_trades), "total_return": _finite(opt.total_return),
            "max_drawdown": _finite(opt.max_drawdown), "fees_paid": _finite(opt.fees_paid)}


def plain_finalist(spec: dict, ctx: Context, finalist: Trial, rank: int, optimise_id: int) -> tuple:
    """A kept strategy of a plain theory (no calibrated exit): returns (its finalists.json entry, its optimise Trial).

    The screen run is repeated and must give the search's Sharpe; then the strategy runs on the optimise window with the theory's own
    exits (stop, target, max hold, sell signal, trailing stop). The entry has no calibration and no opportunities.
    """
    _check_rerun(ctx, spec, finalist)
    opt = run_strategy(ctx, finalist.picks, spec["optimise_window"])
    entry = _entry_head(finalist, rank)
    entry["optimise"] = _optimise_result(opt)
    return entry, make_trial(optimise_id, finalist.picks, "optimise", STEP_FINALIST, 0, _summary(opt, spec["init_cash"]))


def calibrate_finalist(spec: dict, ctx: Context, finalist: Trial, rank: int, optimise_id: int) -> tuple:
    """Returns (the finalist's finalists.json entry, its optimise Trial or None)."""
    theory, frames = ctx.theory, ctx.frames
    picks = finalist.picks
    hold = max_hold(theory, picks)
    entry = _entry_head(finalist, rank)
    entries = signals.entry_signals(theory, picks, frames)
    res = _check_rerun(ctx, spec, finalist)
    fills = [(t["ticker"], pd.Timestamp(t["entry_index"])) for _, t in res.trades.iterrows()]
    expected = signals._Run(theory, picks, frames).operand(theory.data["exit"]["expected_price"])
    opps, skipped = calibrate.opportunities(frames, expected, fills, hold, spec["screen_window"][1])
    entry["opportunities"] = len(opps)
    entry["opportunities_skipped"] = skipped
    try:
        fit = otr.fit_ou(opps)
        calibrator = calibrate.Calibrator(fit, hold, int(spec["n_iter"]), int(spec["seed"]))
        stops, targets = calibrate.exit_frames(frames, expected, entries, calibrator, spec["optimise_window"])
        exits = simulate.ExitRules(max_hold=hold, stop=stops, target=targets)
        opt = run_strategy(ctx, picks, spec["optimise_window"], exits)
    except (otr.OTRError, ValueError) as exc:
        entry["calibration_error"] = "%s: %s" % (type(exc).__name__, exc)
        return entry, None
    buckets = {("%g" % b): {"stop_sigma": p["stop_sigma"], "target_sigma": p["target_sigma"], "mesh_sharpe": p["mesh_sharpe"]}
               for b, p in sorted(calibrator.cache.items())}
    entry["calibration"] = {"phi": fit.phi, "sigma": fit.sigma, "half_life": fit.half_life, "n_obs": fit.n_obs, "max_steps": hold,
                            "n_iter": calibrator.n_iter, "buckets": buckets}
    entry["optimise"] = _optimise_result(opt)
    trial = make_trial(optimise_id, picks, "optimise", STEP_FINALIST, 0, _summary(opt, spec["init_cash"]))
    return entry, trial


def choose_pick(entries: list, optimise_trials: dict) -> tuple:
    """The finalist with the highest optimise-window Sharpe (ties to the better screen rank). Returns (entry, trial) or (None, None)."""
    best = None
    for entry in entries:
        trial = optimise_trials.get(entry["rank"])
        if trial is None or not math.isfinite(trial.sharpe):
            continue
        if best is None or trial.sharpe > best[1].sharpe:
            best = (entry, trial)
    return best if best is not None else (None, None)
