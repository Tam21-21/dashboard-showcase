"""Running one variation of a theory over the group (lane GV2-7a, plan step 3).

A variation is one set of picks with its stop and target rule. `prepare` turns it into the entry signals and the exit rules for a
window; `run` hands them to GV2-5a's run_group. A calibrated theory (exit.stop and exit.target both "calibrated") takes its stop and
target from the variation's calibration table: for each entry signal the gap between the theory's expected price and the entry
price, in sigma units rounded to the half sigma (research_lab.theory_search.calibrate.gap_bucket), picks the bucket, and the bucket's
stop_sigma and target_sigma times the fit's sigma are the stop and target in log units (the same arithmetic GV2-5b's search used).
"""
from __future__ import annotations

import dataclasses
import math

import pandas as pd

from research_lab.exits import otr
from research_lab.theories import form
from research_lab.theory_run import one, signals, simulate
from research_lab.theory_search import calibrate
from research_lab.theory_stage import lookback


class MissingCalibration(ValueError):
    """The theory's exit is calibrated and the variation brings no usable stop and target rule."""


FIT_FIELDS = ("phi", "half_life", "n_obs", "max_steps", "n_iter")


class TableCalibrator:
    """pair(gap) -> the stop and target (log units) of the gap's bucket.

    The table is only a cache of AFML chapter 13's mesh per gap. A bucket the table lacks is computed here exactly as GV2-5b does:
    calibrate.Calibrator(OUFit(phi, sigma, half_life, n_obs), max_steps, n_iter, seed).pair(gap), from the finalist's stored fit
    (fitted on the optimise window only) and the search spec's seed. A missing fit field or seed raises MissingCalibration.
    Each bucket computed this way is kept in `computed` (variation, bucket, stop_sigma, target_sigma).
    """

    def __init__(self, variation_id: int, calibration: dict, seed=None):
        self.variation_id = variation_id
        if not isinstance(calibration, dict) or "sigma" not in calibration or not isinstance(calibration.get("buckets"), dict):
            raise MissingCalibration('variation %s: exits.calibration must hold "sigma" and "buckets"' % variation_id)
        sigma = calibration["sigma"]
        if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or not (math.isfinite(sigma) and sigma > 0):
            raise MissingCalibration("variation %s: calibration sigma must be a positive finite number, got %r" % (variation_id, sigma))
        self.sigma = float(sigma)
        self.buckets = calibration["buckets"]
        self.calibration = calibration
        self.seed = seed
        self.computed: list = []
        self._mesh = None

    def _calibrator(self, gap: float, key: str):
        missing = [f for f in FIT_FIELDS if f not in self.calibration]
        if self.seed is None:
            missing.append("seed (the search spec's, in the stage spec)")
        if missing:
            raise MissingCalibration("variation %s: the calibration table has no gap bucket %s (the gap %.6g is %.6g sigma; its buckets are %s) "
                                     "and it cannot be computed: missing %s" % (self.variation_id, key, gap, gap / self.sigma, ", ".join(sorted(self.buckets)), ", ".join(missing)))
        if self._mesh is None:
            c = self.calibration
            fit = otr.OUFit(phi=float(c["phi"]), sigma=self.sigma, half_life=float(c["half_life"]), n_obs=int(c["n_obs"]))
            self._mesh = calibrate.Calibrator(fit, int(c["max_steps"]), int(c["n_iter"]), int(self.seed))
        return self._mesh

    def pair(self, gap: float) -> dict:
        bucket = calibrate.gap_bucket(gap, self.sigma)
        key = "%g" % bucket
        if key not in self.buckets:
            rule = self._calibrator(gap, key).pair(gap)
            self.buckets[key] = {"stop_sigma": rule["stop_sigma"], "target_sigma": rule["target_sigma"]}
            self.computed.append({"variation": self.variation_id, "bucket": key, "stop_sigma": rule["stop_sigma"], "target_sigma": rule["target_sigma"]})
        row = self.buckets[key]
        for name in ("stop_sigma", "target_sigma"):
            if name not in row:
                raise MissingCalibration("variation %s: gap bucket %s has no %s" % (self.variation_id, key, name))
        return {"stop": float(row["stop_sigma"]) * self.sigma, "target": float(row["target_sigma"]) * self.sigma}


@dataclasses.dataclass
class Prepared:
    variation_id: int
    picks: dict
    entries: pd.DataFrame
    rules: object
    bars_ok: pd.Series      # True on a bar where every operand the conditions read is known for every stock that has a price
    computed: list = dataclasses.field(default_factory=list)   # gap buckets computed in the stage, not read from the table


def is_calibrated(theory) -> bool:
    table = theory.data["exit"]
    flags = [table.get(k, form.NONE) == form.CALIBRATED for k in ("stop", "target")]
    if any(flags) and not all(flags):
        raise MissingCalibration('the theory calibrates only one of stop and target; the stage needs both "calibrated" or neither')
    return all(flags)


def prepare(ctx, variation: dict, window: tuple) -> Prepared:
    """The entry signals (on the whole loaded frames) and the exit rules of one variation for entries inside `window`."""
    theory, frames = ctx.theory, ctx.frames
    vid, picks = variation["id"], variation["picks"]
    entries = signals.entry_signals(theory, picks, frames)
    spec_exits = variation.get("exits") or {}
    computed = []
    if is_calibrated(theory):
        if "calibration" not in spec_exits:
            raise MissingCalibration('variation %s: the theory\'s exit is "calibrated", so the variation needs exits.calibration '
                                     "(sigma and the gap buckets of finalists.json); it has none" % vid)
        rules = one.exit_rules(theory, picks, {"stop": None, "target": None})
        expected = signals._Run(theory, picks, frames).operand(theory.data["exit"]["expected_price"])
        calibrator = TableCalibrator(vid, spec_exits["calibration"], ctx.spec.get("seed"))
        stops, targets = calibrate.exit_frames(frames, expected, entries, calibrator, window)
        computed = calibrator.computed
        rules = dataclasses.replace(rules, stop=stops, target=targets)
    else:
        if "calibration" in spec_exits:
            raise MissingCalibration("variation %s: exits.calibration is given but the theory's exit is not calibrated" % vid)
        rules = one.exit_rules(theory, picks, spec_exits, frames=frames)
    run = signals._Run(theory, picks, frames)
    known = pd.DataFrame(True, index=frames["close"].index, columns=frames["close"].columns)
    for name in lookback.used_operands(theory):
        known &= run.operand(name).notna()
    bars_ok = (known | frames["close"].isna()).all(axis=1)
    return Prepared(vid, picks, entries, rules, bars_ok, computed)


def run(ctx, prepared: Prepared, window: tuple) -> "simulate.GroupResult":
    money = {"method": ctx.theory.data["money"]["method"], "init_cash": ctx.spec["init_cash"]}
    return simulate.run_group(ctx.frames, prepared.entries, prepared.rules, money, ctx.spec["costs"], tuple(window))
