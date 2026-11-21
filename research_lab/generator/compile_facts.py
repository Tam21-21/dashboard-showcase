"""Image-side generator step (section 5.2 step 1, AD4.1 step 1-2). The ONLY file in this
whole unit that imports vectorbtpro. Run as:

    docker run --rm -v <repo>/research_lab:/work:ro -w /work research-runner:m3-latest \\
        python generator/compile_facts.py <theory.md>

Reads a theory file's '## Fillers' and '## Expressions' lists directly, with a minimal,
standalone parse: it does NOT import research_lab.generator.theory, because that loader
also runs the look-ahead gate by loading strategies/helpers/lookahead.py from a path
resolved relative to the repo root, and this container mounts research_lab/ ALONE at
/work, so that repo-root-relative resolution would fail here. The full theory shape
(the mechanism/counterparty/kill-rule fields, the fail-loud checks, the look-ahead gate)
is the host loader's job; this script only needs to know which fillers to compile.

Resolves every filler id to a compiled vbt indicator class and prints one JSON object of
the facts the host-side generation step needs: which vbt build produced them, and each
filler's class name, parameter names, input names, output names, and expression text.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np  # only for the deterministic synthetic warmup-probe series, never vbt's own math
import pandas as pd  # the Series shape every filler class's run() expects for close/open/high/low
import vectorbtpro as vbt  # the one and only vbt import in this entire generator unit

# AD3.2's warmup dimension (job block criterion 2, `3.2b`): the synthetic series used to
# MEASURE a filler's own leading-undefined-row count. A sinusoid riding a linear trend: the
# trend term (0.05 per bar) dominates the sinusoid's own bounded per-bar step (at most 6.0
# in magnitude here, from the 3.0 amplitude), so two bars are never numerically tied and a
# bar-to-bar return is never exactly zero in practice, without needing any random seed (the
# measurement stays reproducible byte for byte on every run). The 0.37 phase offset and the
# 6.0 divisor keep the sinusoid's own period off any integer multiple of the trend's own
# scale, so the two never fall into lockstep. Recorded verbatim in every filler's own facts
# entry so the emitted module's comment can quote exactly what measured its own warmup.
_WARMUP_SERIES_FORMULA = "close[t] = 100.0 + 0.05*t + 3.0*sin(t/6.0 + 0.37), t = 0 .. length-1"
_WARMUP_PROBE_START_BARS = 64  # a short first try: most fillers resolve well inside this
_WARMUP_PROBE_MAX_BARS = 131072  # ARGUED ceiling: 2**17 bars is far past any realistic indicator's own window; past this the filler is refused rather than probed forever


def _synthetic_ohlc(length: int) -> "tuple[pd.Series, pd.Series, pd.Series, pd.Series]":
    """Builds (close, open, high, low) for `_WARMUP_SERIES_FORMULA` at the given length.
    open is close shifted one bar (the same synthesis the p1 image-side probe already uses
    for its own future-shock series), so a filler reading open never sees a value that did
    not exist yet; high/low bracket close by a fixed half-point band, wide enough that no
    filler reading (high - low) ever divides by zero."""
    t = np.arange(length, dtype=float)
    close = pd.Series(100.0 + 0.05 * t + 3.0 * np.sin(t / 6.0 + 0.37))
    open_ = close.shift(1).fillna(close.iloc[0])
    high = close + 0.5
    low = close - 0.5
    return close, open_, high, low


def _leading_undefined_count(values) -> "int | None":
    """The count of leading NaN entries in a 1-D array-like, before the first defined
    (non-NaN) value. None means every entry is still undefined: the series was too short
    for this filler to produce even one real number yet, the doubling loop's own signal to
    try a longer one."""
    arr = np.asarray(values, dtype=float)
    defined = ~np.isnan(arr)
    if not defined.any():
        return None  # not even one defined value at this length
    return int(np.argmax(defined))  # position of the first True is exactly the leading-NaN count


def measure_warmup_bars(cls) -> "tuple[int, int]":
    """AD3.2's warmup dimension: runs the compiled filler class over `_synthetic_ohlc` at
    growing lengths, doubling from `_WARMUP_PROBE_START_BARS`, until its FIRST output
    (R-G13: 'its first output is out') leaves at least one row defined. Returns
    (warmup_bars, series_length_used) so the caller can record both facts. Refuses by name
    ('filler-lookback-unmeasurable') past `_WARMUP_PROBE_MAX_BARS`, which the job block
    requires rather than letting an unmeasurable filler hang the compile step forever."""
    length = _WARMUP_PROBE_START_BARS
    while length <= _WARMUP_PROBE_MAX_BARS:
        close, open_, high, low = _synthetic_ohlc(length)
        ind = cls.run(close=close, open=open_, high=high, low=low)
        out = getattr(ind, cls.output_names[0])
        warmup_bars = _leading_undefined_count(out)
        if warmup_bars is not None:
            return warmup_bars, length
        length *= 2
    raise ValueError(
        f"filler-lookback-unmeasurable: {cls.__name__}'s first output never leaves NaN "
        f"within {_WARMUP_PROBE_MAX_BARS} bars of the synthetic warmup-probe series"
    )


def parse_theory_lists(text: str) -> "tuple[list[str], dict[str, str]]":
    """Read only the '## Fillers' and '## Expressions' headings from a theory file's raw
    text. Never refuses on a missing Mechanism/Counterparty/Kill-rule/Skeletons field:
    compiling facts does not need them, and refusing on them is the host loader's job
    (research_lab.generator.theory.load_theory), which runs before OR after this script
    depending on which the caller runs first; either order is safe because this script
    writes no state and the host loader is the one fail-loud gate for the full file shape.
    """
    blocks = re.split(r"(?m)^## ", text)
    headings: dict[str, str] = {}
    for block in blocks[1:]:
        heading, _, body = block.partition("\n")
        headings[heading.strip()] = body.strip("\n")
    fillers = [ln.strip() for ln in headings.get("Fillers", "").splitlines() if ln.strip()]
    expressions: dict[str, str] = {}
    for line in headings.get("Expressions", "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, sep, expr_text = line.partition(":")
        if sep:
            expressions[name.strip()] = expr_text.strip()
    return fillers, expressions


def compile_filler_facts(filler_id: str, expressions: "dict[str, str]") -> dict:
    """Compile one filler id to its vbt facts (AD4.1 step 1-2): resolve to an indicator
    class (from_wqa101 for a wqa101 alpha, from_expr for a named expression template),
    then read the class's own param_names/input_names/output_names off it -- never
    guessed, always read from the compiled class, so a frozen-build difference from
    documentation is caught here rather than assumed (DECISIONS #308 ruling 3)."""
    if filler_id.startswith("wqa101:"):
        _, _, n = filler_id.partition(":")
        alpha_idx = int(n)
        cls = vbt.IF.from_wqa101(alpha_idx)
        # the frozen build's own expression text for this alpha, never vbt's documentation
        expression = vbt.indicators.expr.wqa101_expr_config[alpha_idx]
    elif filler_id.startswith("expr:"):
        _, _, name = filler_id.partition(":")
        if name not in expressions:
            raise ValueError(f"expr filler {filler_id!r} has no matching '## Expressions' line")
        expression = expressions[name]
        cls = vbt.IF.from_expr(expression, use_pd_eval=False)  # R-G15: never the default
    else:
        raise ValueError(f"unresolvable filler id: {filler_id!r}")
    # AD3.2's warmup dimension (job block criterion 2): measured here, not guessed, so a
    # generated candidate's own lookback_bars() declaration is honest rather than assumed.
    warmup_bars, warmup_series_length = measure_warmup_bars(cls)
    return {
        "class": cls.__name__,
        "param_names": list(cls.param_names),
        "input_names": list(cls.input_names),
        "output_names": list(cls.output_names),
        "expression": expression,
        "warmup_bars": warmup_bars,
        "warmup_series_length": warmup_series_length,
        "warmup_series_formula": _WARMUP_SERIES_FORMULA,
    }


def main(argv: "list[str]") -> None:
    if len(argv) != 2:
        raise SystemExit(f"usage: {argv[0]} <theory.md>")
    theory_path = Path(argv[1])
    text = theory_path.read_text(encoding="utf-8")
    fillers, expressions = parse_theory_lists(text)
    # AC-7 (job block, lane GR): compile each filler ON ITS OWN, so one that cannot compile
    # (measured: wqa101:41, whose compiled class needs a 'volume' input this generator never
    # hands cls.run) no longer fails the WHOLE theory's compile step, PREVIEW included. Caught
    # narrowly: TypeError is the measured shape (a required cls.run(...) keyword argument this
    # probe's fixed close/open/high/low call never supplies); ValueError is the shape
    # compile_filler_facts and measure_warmup_bars themselves already raise BY NAME for an
    # unresolvable filler id or an unmeasurable warmup ceiling, both equally "this filler
    # cannot compile" refusals. Recorded as a refused entry naming the reason in plain words,
    # using the exception's own message (which, for the measured case, already names the
    # missing input by name), never a guessed or generic sentence; every OTHER filler in the
    # same theory still compiles and reaches this dict.
    filler_facts: dict = {}
    for filler_id in fillers:
        try:
            filler_facts[filler_id] = compile_filler_facts(filler_id, expressions)
        except (TypeError, ValueError) as exc:
            filler_facts[filler_id] = {
                "decision": "refused",
                "reason": f"filler_compile_failed:{exc}",
            }
    facts = {
        "image": "research-runner:m3-latest",
        "vbt_version": vbt.__version__,  # read from the running build, never hardcoded
        "fillers": filler_facts,
    }
    print(json.dumps(facts))


if __name__ == "__main__":
    main(sys.argv)
