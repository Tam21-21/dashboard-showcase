"""The spec of a stage run and the loaded group (lane GV2-7a, plan step 2).

The spec is a JSON object:
  theory_toml      the theory file's text
  group_label      the name of the stock group, GROUP_ then capitals, digits or underscores; it sits in the ticker slot
  stage            "optimise" or "walkforward"
  source_shape, data + codes (raw_synthetic) or files (stored)   as in research_lab.theory_run.one
  member_sha256    optional {ticker: sha256}: each must equal the loaded file's sha256
  data_start       first bar loaded; bars from here warm the indicators
  window_start, window_end   both included; entries are made only inside it (the walk-forward slices are cut from it)
  variations       a list of {"id": whole number, "picks": {letter: number}, "exits": {...}}; exits is read as in
                   research_lab.theory_run.one for a stop or target the theory leaves "calibrated", but for a calibrated
                   theory it must hold "calibration": {"sigma": number, "buckets": {"<gap bucket>": {"stop_sigma": n,
                   "target_sigma": n}}}, the fit's sigma and the buckets of GV2-5b's finalists.json
  costs            {"fees": "none", "ibkr_fixed" or "alpaca", "slippage": fraction}
  init_cash        starting cash of the group
  cscv_s           optimise: the number of blocks S of check 5
  mcpt             optimise: {"m": whole number >= 1, "workers": whole number >= 1}, check 7's re-run on m shuffled-bar markets and the
                   processes that score them (lane GV3-J7); a walkforward spec may carry it, it is ignored
  in_sample_bars, out_of_sample_bars, anchoring ("rolling" or "anchored")   walkforward: the ruled #392 figures, read by the caller
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from research_lab.theories import form
from research_lab.theory_run import identity, one

LABEL = re.compile(r"^GROUP_[A-Z0-9_]+$")
STAGES = ("optimise", "walkforward")
COMMON_KEYS = ("theory_toml", "group_label", "stage", "source_shape", "data_start", "window_start", "window_end",
               "variations", "costs", "init_cash")
STAGE_KEYS = {"optimise": ("cscv_s", "mcpt"), "walkforward": ("in_sample_bars", "out_of_sample_bars", "anchoring")}


@dataclass
class Context:
    spec: dict
    theory: object
    frames: dict
    shas: dict
    label: str
    member_set_sha256: str
    window: tuple
    variations: list


def check_spec(spec: dict) -> None:
    missing = [k for k in COMMON_KEYS if k not in spec]
    if missing:
        raise ValueError("the spec lacks %s" % ", ".join(missing))
    if spec["stage"] not in STAGES:
        raise ValueError("stage must be one of %s, got %r" % (", ".join(STAGES), spec["stage"]))
    missing = [k for k in STAGE_KEYS[spec["stage"]] if k not in spec]
    if missing:
        raise ValueError("stage %r: the spec lacks %s" % (spec["stage"], ", ".join(missing)))
    if not isinstance(spec["group_label"], str) or not LABEL.match(spec["group_label"]):
        raise ValueError("group_label %r must match ^GROUP_[A-Z0-9_]+$: the ticker slot holds a group's name, never a stock's" % (spec["group_label"],))
    start, first, last = pd.Timestamp(spec["data_start"]), pd.Timestamp(spec["window_start"]), pd.Timestamp(spec["window_end"])
    if not (start <= first <= last):
        raise ValueError("the dates must satisfy data_start <= window_start <= window_end, got %s, %s, %s" % (start.date(), first.date(), last.date()))
    variations = spec["variations"]
    if not isinstance(variations, list) or not variations:
        raise ValueError("variations must be a non-empty list")
    ids = []
    for v in variations:
        if not isinstance(v, dict) or "id" not in v or "picks" not in v:
            raise ValueError('each variation needs "id" and "picks", got %r' % (v,))
        if isinstance(v["id"], bool) or not isinstance(v["id"], int) or v["id"] < 0:
            raise ValueError("a variation id must be a whole number from 0, got %r" % (v["id"],))
        ids.append(v["id"])
    if len(set(ids)) != len(ids):
        raise ValueError("variation ids must be distinct, got %s" % ids)
    if spec["stage"] == "optimise":
        s = spec["cscv_s"]
        if isinstance(s, bool) or not isinstance(s, int) or s < 2:
            raise ValueError("cscv_s must be a whole number of at least 2, got %r" % (s,))
        mcpt = spec["mcpt"]
        if not isinstance(mcpt, dict) or set(mcpt) != {"m", "workers"} or any(
                isinstance(mcpt[k], bool) or not isinstance(mcpt[k], int) or mcpt[k] < 1 for k in ("m", "workers")):
            raise ValueError('mcpt must be {"m": whole number >= 1, "workers": whole number >= 1}, got %r' % (mcpt,))
    else:
        for key in ("in_sample_bars", "out_of_sample_bars"):
            if isinstance(spec[key], bool) or not isinstance(spec[key], int) or spec[key] < 1:
                raise ValueError("%s must be a whole number of at least 1, got %r" % (key, spec[key]))
        if spec["anchoring"] not in ("rolling", "anchored"):
            raise ValueError('anchoring must be "rolling" or "anchored", got %r' % (spec["anchoring"],))


def load_context(spec: dict) -> Context:
    check_spec(spec)
    with tempfile.TemporaryDirectory(prefix="theory-stage-") as folder:
        path = Path(folder) / "theory.toml"
        path.write_text(spec["theory_toml"], encoding="utf-8")
        theory = form.load(path)
    if theory.data["theory"]["entry_time"] != "next_open":
        raise ValueError("this runner fills at the next open; the theory's entry_time is %r" % theory.data["theory"]["entry_time"])
    frames, shas = one.load_group(spec)
    for ticker, want in (spec.get("member_sha256") or {}).items():
        if shas.get(ticker) != want:
            raise ValueError("member %s: the file's sha256 is %s, the spec says %s" % (ticker, shas.get(ticker), want))
    start, end = pd.Timestamp(spec["data_start"]), pd.Timestamp(spec["window_end"])
    index = frames["close"].index
    if index.tz is not None:
        start, end = start.tz_localize(index.tz), end.tz_localize(index.tz)
    cut = {name: frame.loc[start:end] for name, frame in frames.items()}
    if len(cut["close"]) == 0:
        raise ValueError("no bar between data_start %s and window_end %s" % (spec["data_start"], spec["window_end"]))
    return Context(spec, theory, cut, shas, spec["group_label"], identity.member_set_sha256(shas.values()),
                   (spec["window_start"], spec["window_end"]), sorted(spec["variations"], key=lambda v: v["id"]))
