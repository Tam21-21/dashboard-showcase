"""The figure each judge check graded, and its bar, for the Judge page (lane UI8b-0).

Everything here is read from what the judge stored: the quantities in a verdict's check_outcomes and
the frozen threshold rows of the verdict's own battery version. Nothing is recomputed from anything
else, and nothing is invented: a value or a bar that is not stored is None, and the page shows a dash.
The one derived figure is check 4's flatness, which is the same ratio the check compares to its bar.
"""
from __future__ import annotations

import re

from . import judge_checks, judgedb

# (key, label, threshold quantity key or None, comparison for a constant bar, units for a constant bar)
_MEASURES: dict[int, tuple] = {
    1: (
        ("rdf_percent", "degrees of freedom left", None, "at_or_above", "percent"),
        ("kappa", "fat tails (kappa)", None, "at_or_below", "ratio"),
    ),
    2: (("wfe_percent", "walk-forward efficiency", "wfe_pass_bar"),),
    3: (("dsr", "deflated Sharpe", "dsr_pass_bar"),),
    4: (("flatness", "peak above its neighbours, in spreads", "plateau_flatness_bar"),),
    5: (("pbo", "chance the best is overfit", "pbo_threshold"),),
    6: (("bound", "pessimistic mean return", "mc_bound_pass_bar"),),
    7: (("p_value", "permutation p-value", "mcpt_pvalue_threshold"),),
    8: (("prom_minus_biggest_run", "profit without the best run", "prom_minus_biggest_run_pass_bar"),),
    9: (
        ("sharpe_margin", "Sharpe above buy and hold", "buy_hold_arm_pass_bar"),
        ("p_value", "random-entry p-value", "random_arm_pass_bar"),
    ),
}

_CONSTANT_BARS = {
    "rdf_percent": judge_checks._RDF_FLOOR_PERCENT,
    "kappa": judge_checks._KAPPA_GATE,
}


# Why a check's figure is missing, in plain words, for a not-evaluable outcome only (lane UI35-0).
# Key rule: a stored reason that opens with a lowercase-hyphen code and then ":" or the end is keyed
# on that code; any other reason is keyed on its full text, with each part the check filled in at run
# time written "{}", and at lookup "{}" matches any text. The sentences are ours; the reasons are the
# checks' own words, which this page never shows.
_B = "The pass mark for this check is not set for this version of the checks."
_G = "The run this verdict grades is no longer in the research store."
_Y = "The run does not record how many bars make a year, so its results cannot be scaled to a year."
_NO_TRADES = "The run made no trades."
_BAR_RETURNS = "The run's bar by bar returns were not stored."
_WF_NOT_RUN = "The test on data it was not tuned on has not run yet."
_DEGREES = "The number of settings the strategy tunes is not recorded, so the freedom left cannot be counted."
_CELLS_LACK_RETURNS = "The run's results lack the return figures this test needs."
_NO_WARMUP_BARS = "The number of bars after the warm-up is not recorded."
_WRONG_COUNT_ARM = "The random entry comparison ran the wrong number of tries."

_WHY: dict[tuple[int, str], str] = {
    (1, "fitted-run-not-found"): "The tuned run this exam run came from is not recorded.",
    (1, "trade_count is the missing input: the trade record is empty"): _NO_TRADES,
    (1, "kappa is the missing input: fewer than 4 trades"):
        "Too few trades to measure how heavy the losing tail is (it needs 4).",
    (1, "kappa is the missing input: fewer than 2 non-overlapping pairs"):
        "Too few trades to measure how heavy the losing tail is (it needs 2 pairs).",
    (1, "kappa is the missing input: M(1) or M(2) is zero (a degenerate series)"):
        "The trade results do not vary, so how heavy the losing tail is cannot be measured.",
    (1, "kappa is the missing input: M(2) equals M(1), so equation 10.2's log ratio is zero"):
        "The trade results scale in a way that makes the tail measure undefined.",
    (1, "kappa is the missing input: {}"):
        "How heavy the losing tail is could not be measured from these trades.",
    (1, "bars-total-absent"): "The number of bars the run was tested on is not recorded.",
    (1, "bars-total-ambiguous"): "More than one bar count matches this run.",
    (1, "run {} not found"): _G,
    (1, "used-degrees-of-freedom-undeclared"): _DEGREES,
    (1, "degrees-of-freedom-remaining is the missing input: Pardo's Rdf (pages 130-131) needs the strategy's used degrees of freedom and the test window's bar count, neither reachable from a trade record alone"): _DEGREES,
    (2, "wfe_pass_bar is the missing input: not frozen for this battery_version"): _B,
    (2, "in_sample_profit and out_of_sample_profit are the missing inputs: this run has no research_run_stats row at all"): _WF_NOT_RUN,
    (2, "in_sample_profit and out_of_sample_profit are the missing inputs: this run's own research_run_stats row has not recorded a walk-forward write yet"): _WF_NOT_RUN,
    (3, "graded-cell-absent"): "The one setting this step grades is not recorded.",
    (3, "dsr_pass_bar is the missing input: not frozen for this battery_version"): _B,
    (3, "run {} not found"): _G,
    (3, "annualization_factor is the missing input: research_runs carries no positive observed-bars-per-year factor for this run, so the stored Sharpe cannot be de-annualised into the book's own original sampling frequency"): _Y,
    (3, "annualization_factor is the missing input: research_runs carries no positive observed-bars-per-year factor for this run (an event or legacy run stores NULL here), so the stored Sharpe cannot be de-annualised into the book's own original sampling frequency"): _Y,
    (3, "member_dataset_sha256 is the missing input: the cell's replay carries no member set sha256, so its trial family cannot be found"):
        "The tickers the run used are not recorded, so its group of tries cannot be found.",
    (3, "screen-window-unresolved"): "The run's screening dates are missing or recorded twice.",
    (3, "trial family is the missing input: no search recorded a family on this member set and screen window, so the trial-multiplicity correction is not computable"):
        "No search recorded the group of tries this strategy belongs to.",
    (3, "family-union-pending"): "Several theories tried this data; their combined group of tries is not worked out yet.",
    (3, "family-union-failed"): "Several theories tried this data; working out their combined group of tries failed.",
    (3, "too-few-trials"):"Too few strategies were tried to count the real tries.",
    (3, "k_effective is {}, below 2: the trial-multiplicity correction needs at least 2 independent trials"):
        "Fewer than 2 really different tries were found; the luck test needs 2.",
    (3, "v is {}, not positive: the variance across the family's trials is needed for the benchmark"):
        "The tries' results do not vary, so the luck test has nothing to measure.",
    (3, "research_run_cells is the missing input: the graded cell carries no Sharpe, skew and kurtosis together"): _CELLS_LACK_RETURNS,
    (3, "research_run_cells is the missing input: no member's row carries a Sharpe, skew and kurtosis together (skew/kurtosis are only written by a sweep cell, spec brief section 3's own basket-breadth requirement)"): _CELLS_LACK_RETURNS,
    (3, "bars_after_warmup is the missing input: research_run_funnel carries no row for the graded cell"): _NO_WARMUP_BARS,
    (3, "bars_after_warmup is the missing input: research_run_funnel carries no cell row for this run (a bespoke strategy that bypasses the shared portfolio helper writes no funnel row), so the observed-return count is not derivable"): _NO_WARMUP_BARS,
    (3, "bar-return count too small (T < 2) to compute a standard error"): "Fewer than 2 bar returns, too few to measure.",
    (3, "PSR's own denominator is not positive for this candidate's skew, kurtosis and Sharpe together; DSR is not computable"):
        "This mix of return figures gives no valid luck-adjusted score.",
    (3, "trial-count-scope-owed"): "How to count tries for this kind of run is not decided yet.",
    (3, "research_trials is the missing input: fewer than 2 trial rows for this dataset/strategy, so the trial-multiplicity correction is not computable"):
        "Fewer than 2 tries are recorded for this strategy and data.",
    (4, "plateau_bandwidth is the missing input: not frozen for this battery_version (judge_spec_brief.md section 7)"):
        "How far around the chosen setting to look is not set yet.",
    (4, "plateau_flatness_bar is the missing input: not frozen for this battery_version"): _B,
    (4, "research_run_cells is the missing input: no cell carries a Sharpe ratio for this run"):
        "No setting of this run has a recorded result.",
    (4, "no graded cell at the replayed centre (variation_id={}, ticker={})"): "The chosen setting has no graded result.",
    (4, "fewer neighbouring cells than plateau_bandwidth needs on at least one side of the peak"):
        "Too few nearby settings were tested on one side of the chosen one.",
    (5, "cscv-artifact-over-cap"): "The stored results for this test are too large to read.",
    (5, "the cscv artifact is the missing input: this run carries no stored per-period performance matrix (a sweep stored before this build, or one whose worker upload of the block-sums artifact never landed)"):
        "The results per period this test needs were not stored for this run.",
    (5, "pbo_threshold is the missing input: not frozen for this battery_version"): _B,
    (5, "the cscv artifact carries too few trials ({}) to run PBO: never run PBO on a single split (AFML section 11.6)"):
        "Too few settings were tried to measure the fluke chance.",
    (5, "the cscv artifact's own blocks carry too few bars ({}) to run PBO: each block needs at least 2 bars for a defined Sharpe"):
        "The data pieces are too short; each needs 2 bars.",
    (5, "cscv_split_count is the missing input: CSCV's own split count has no book-sourced candidate value (judge_spec_brief.md section 7), and the per-period performance matrix CSCV needs is not a shape this schema carries today (researchdb.py, where it would need a new column and write path, is outside this lane's own area fence)"):
        "This version of the checks could not run this test yet.",
    (6, "mc_bound_pass_bar is the missing input: not frozen for this battery_version"): _B,
    (6, "bound_percentile is the missing input: not frozen for this battery_version (owed, the percentile at which #213's pessimistic lower bound is read, judge_spec_brief.md section 7)"):
        "How cautious the shuffled-trades test should be is not set yet.",
    (6, "the candidate's bar-level return series is the missing input: equity.parquet is absent or empty"): _BAR_RETURNS,
    (6, "block-length-undefined"): "The run has too few bars, or too flat a record, to choose the bootstrap block length.",
    (7, "mcpt_pvalue_threshold is the missing input: not frozen for this battery_version"): _B,
    (7, "the candidate's bar-level return series is the missing input: equity.parquet is absent or empty"): _BAR_RETURNS,
    (7, "the re-run used {} fake markets but the frozen replication count is {} (#376 decision 2, D7): a different count is not graded"):
        "The shuffled-prices test ran the wrong number of fake markets.",
    (7, "check 7's re-run on shuffled price bars could not be made: {} (#451: never the bar-return shuffle instead)"):
        "The shuffled-prices test could not be run for this strategy.",
    (7, "the MCPT shuffle flavor for a multi-bar hold is the missing input: #213's own 'What is deliberately left to spec time' paragraph reserves which permutation scheme survives a multi-day hold (judge_spec_brief.md), and this build does not invent one"):
        "How to shuffle prices for trades held several days is not decided yet.",
    (8, "trade record is empty"): _NO_TRADES,
    (8, "no winning trades in the record: the biggest winning run cannot be identified"):
        "The run has no winning trades, so the biggest win cannot be found.",
    (9, "the following are missing inputs for check 9, read together rather than stopping at the first (#193 safeguard 3 requires reasoning about both control arms): {}"):
        "The buy and hold and random entry comparisons are not set yet.",
    (9, "the buy-and-hold arm's own computation failed inside the container: {}"): "The buy and hold comparison failed while it ran.",
    (9, "the random-entry arm used {} permutations but the frozen replication count is {} (#421 item 1): a different count is not graded"): _WRONG_COUNT_ARM,
    (9, "the buy-and-hold arm is absent for this replay; replay the cell again. The random-entry arm's own comparison is likewise not implemented in this build (#193): both control arms' comparisons are unbuilt until Tamer ratifies the random-entry arm's own statistic"):
        "The buy and hold comparison was not stored for this run.",
    (9, "the candidate's or the buy-and-hold arm's own bar dates are missing, so the two daily return series cannot be paired by date"):
        "The dates that line up the strategy and buy and hold are missing.",
    (9, "the candidate's and the buy-and-hold arm's own daily return series share fewer than two overlapping dates ({}); a Sharpe ratio needs at least two paired observations"):
        "The strategy and buy and hold share fewer than 2 dates.",
    (9, "annualization_factor is the missing input: research_runs carries no positive observed-bars-per-year factor for this run, so neither Sharpe ratio can be annualised"): _Y,
    (9, "the candidate's or the buy-and-hold arm's own daily return series has a zero standard deviation over the shared dates; a Sharpe ratio is not computable"):
        "One of the two return series never moves, so they cannot be compared.",
    (9, "the buy-and-hold arm passed (Sharpe margin {} above the buy_hold_arm_pass_bar of {}), but check 9 remains not-evaluable: the random-entry arm alone is unbuilt (#193 safeguard 3 requires both control arms to pass before check 9 can clear)"):
        "It beat buy and hold, but the random entry comparison is not built yet.",
}

_NOT_EVALUABLE = judge_checks.NOT_EVALUABLE
_WHY_GENERIC = "This check could not be graded yet: an input it needs is missing."
_WHY_STORED_WITHOUT = "This verdict was stored without this check's figures."
_CODE_RULE = re.compile(r"^([a-z]+(?:-[a-z]+)+)(?::|$)")


def _wildcard(key: str) -> "re.Pattern[str]":
    return re.compile("".join(".*" if part == "{}" else re.escape(part) for part in re.split(r"(\{\})", key)), re.DOTALL)


# per check: the keys with a "{}" in them, as full-match patterns, tried after the exact keys
_WILDCARDS: dict[int, list[tuple["re.Pattern[str]", str]]] = {}
for (_check, _key), _sentence in _WHY.items():
    if "{}" in _key:
        _WILDCARDS.setdefault(_check, []).append((_wildcard(_key), _sentence))


def _why(entry) -> "str | None":
    """The plain sentence for a check's missing figure, or None when there is nothing to explain.

    None for a check that was not run, and for a cleared or failed check that stored a reason (that
    reason is a pass or fail reason, not a missing figure).
    """
    if entry is None or entry.get("outcome") is None:
        return None
    reason = entry.get("reason")
    if not reason and not entry.get("quantities"):
        return _WHY_STORED_WITHOUT
    if entry["outcome"] != _NOT_EVALUABLE:
        return None
    check = entry["check"]
    if not isinstance(reason, str) or not reason:
        return _WHY_GENERIC
    match = _CODE_RULE.match(reason)
    key = match.group(1) if match else reason
    sentence = _WHY.get((check, key))
    if sentence is not None:
        return sentence
    for pattern, wildcard_sentence in _WILDCARDS.get(check, ()):
        if pattern.fullmatch(reason):
            return wildcard_sentence
    return _WHY_GENERIC


def _number(quantities: dict, key: str):
    value = quantities.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _value(check: int, key: str, quantities: dict):
    if check == 4 and key == "flatness":
        peak = _number(quantities, "peak_sharpe")
        mean = _number(quantities, "neighbourhood_mean")
        std = _number(quantities, "neighbourhood_std")
        if peak is None or mean is None or std is None or std == 0:
            return None
        return abs(peak - mean) / std
    return _number(quantities, key)


def check_measures(check_outcomes, conn, battery_version) -> list[dict]:
    """Nine entries in check order: {check, outcome, measures: [{key, label, value, bar, comparison, units}], why}."""
    by_check = {entry.get("check"): entry for entry in (check_outcomes or [])}
    result = []
    for check in range(1, 10):
        entry = by_check.get(check)
        outcome = entry.get("outcome") if entry is not None else None
        quantities = (entry.get("quantities") if entry is not None else None) or {}
        measures = []
        for spec in _MEASURES[check]:
            key, label, threshold_key = spec[0], spec[1], spec[2]
            if threshold_key is None:
                bar, comparison, units = _CONSTANT_BARS[key], spec[3], spec[4]
            else:
                bar = comparison = units = None
                if battery_version is not None:
                    row = judgedb.read_threshold(conn, check, threshold_key, battery_version)
                    if row is not None:
                        bar, comparison, units = row["value_numeric"], row["comparison"], row["units"]
            measures.append({
                "key": key, "label": label, "value": _value(check, key, quantities),
                "bar": bar, "comparison": comparison, "units": units,
            })
        result.append({"check": check, "outcome": outcome, "measures": measures, "why": _why(entry)})
    return result
