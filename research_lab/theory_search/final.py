"""The final test of a search's kept strategies (lane GV3-F, DECISIONS #467 item 5, #472 item 8 and #473).

Dolzhenko, "Algorithmic Trading Systems and Strategies", pdfp 93 to 94 (docs/books/algorithmic_trading_systems_deep/part_B03.md,
"Selection and Forward Testing"): a strategy is tested "on stocks that were not included in the search process"; only if all results meet
the suitability condition is the strategy final (Figure 3-13).

Plain words: a third of the stock group is kept back and the search never sees it (draw_kept_back, GV3-W1). After the search, each kept
strategy is run twice more, with the exits it was made with: on the kept-back stocks over the screen years, and on the searched stocks
over the later years (#473: the newest years, after the walk-forward). It "held up" when both runs meet the book's quality condition
(quality.py), the same five numbers and bars the search used. Only strategies that held up go to the judge.

The spec's "final" is {"kept_back": [tickers], "later_window": [start, end]}. check_final refuses a bad one by name.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from research_lab.exits import otr
from research_lab.theory_run import signals, simulate
from research_lab.theory_search import calibrate, quality, search

FINAL_KEYS = ("kept_back", "later_window")


def group_tickers(spec: dict) -> list:
    """The stocks of the spec's group, in the spec's order."""
    shape = spec["source_shape"]
    if shape == "raw_synthetic":
        return list(spec["codes"])
    if shape == "stored":
        return list(spec["files"])
    raise ValueError('source_shape must be "raw_synthetic" or "stored", got %r' % (shape,))


def _day(value) -> "datetime":
    if not isinstance(value, str):
        raise ValueError("not a date: %r" % (value,))
    return datetime.strptime(value, "%Y-%m-%d")


def check_final(spec: dict) -> None:
    """Raises ValueError, naming the fault, unless spec["final"] is a good final for the spec's group and screen window."""
    final = spec["final"]
    if not isinstance(final, dict):
        raise ValueError("final must be an object with kept_back and later_window, got %r" % (final,))
    missing = [k for k in FINAL_KEYS if k not in final]
    unknown = [k for k in final if k not in FINAL_KEYS]
    if missing or unknown:
        raise ValueError("final: missing %s, unknown %s" % (missing, unknown))
    kept, group = final["kept_back"], group_tickers(spec)
    if not isinstance(kept, list) or not kept or not all(isinstance(t, str) for t in kept):
        raise ValueError("final kept_back must be a non-empty list of tickers, got %r" % (kept,))
    if len(set(kept)) != len(kept):
        raise ValueError("final kept_back lists a stock twice: %s" % ", ".join(sorted({t for t in kept if kept.count(t) > 1})))
    absent = [t for t in kept if t not in group]
    if absent:
        raise ValueError("final kept_back names %s, not in the group %s" % (", ".join(absent), ", ".join(group)))
    if len(kept) >= len(group):
        raise ValueError("final kept_back is the whole group (%d stocks): the search would have nothing to read" % len(group))
    window = final["later_window"]
    if not (isinstance(window, (list, tuple)) and len(window) == 2):
        raise ValueError("final later_window must be two dates [start, end], got %r" % (window,))
    try:
        start, end = _day(window[0]), _day(window[1])
    except ValueError as exc:
        raise ValueError("final later_window must be two dates YYYY-MM-DD, got %r (%s)" % (list(window), exc)) from exc
    if end < start:
        raise ValueError("final later_window ends before it starts: %s to %s" % (window[0], window[1]))
    screen_end = spec["screen_window"][1]
    if start <= _day(screen_end):
        raise ValueError("final later_window starts on %s, on or before the screen window's end %s: the later years must come after the search years"
                         % (window[0], screen_end))


def split_spec(spec: dict) -> tuple:
    """(the search spec, the kept spec): the same spec with the group cut in two, the kept-back stocks on one side and the others on the other.

    The search spec has no "final": the search never learns of the kept-back stocks. Order within each side follows the spec.
    """
    check_final(spec)
    kept = set(spec["final"]["kept_back"])
    search_spec = {k: v for k, v in spec.items() if k != "final"}
    kept_spec = dict(search_spec)
    if spec["source_shape"] == "raw_synthetic":
        search_spec["codes"] = [c for c in spec["codes"] if c not in kept]
        kept_spec["codes"] = [c for c in spec["codes"] if c in kept]
    else:
        search_spec["files"] = {t: p for t, p in spec["files"].items() if t not in kept}
        kept_spec["files"] = {t: p for t, p in spec["files"].items() if t in kept}
    return search_spec, kept_spec


def _calibrated_exits(ctx: "search.Context", picks: dict, entry: dict, window, seed: int) -> "simulate.ExitRules":
    """The exits a calibrated finalist was made with, rebuilt for another context: its O-U fit and its mesh, from finalists.json."""
    cal = entry["calibration"]
    fit = otr.OUFit(phi=cal["phi"], sigma=cal["sigma"], half_life=cal["half_life"], n_obs=cal["n_obs"])
    hold = int(cal["max_steps"])
    calibrator = calibrate.Calibrator(fit, hold, int(cal["n_iter"]), seed)
    entries = signals.entry_signals(ctx.theory, picks, ctx.frames)
    expected = signals._Run(ctx.theory, picks, ctx.frames).operand(ctx.theory.data["exit"]["expected_price"])
    stops, targets = calibrate.exit_frames(ctx.frames, expected, entries, calibrator, tuple(window))
    return simulate.ExitRules(max_hold=hold, stop=stops, target=targets)


def _side(spec: dict, ctx: "search.Context", entry: dict, window, bars: quality.Bars) -> dict:
    """One forward run: the entry's picks on `ctx` over `window` with the exits its finalists.json entry was made with."""
    picks = entry["picks"]
    try:
        exits = _calibrated_exits(ctx, picks, entry, window, int(spec["seed"])) if search.is_calibrated(ctx.theory) else None
        res = search.run_strategy(ctx, picks, window, exits)
    except (otr.OTRError, ValueError) as exc:
        return {"five": None, "meets": False, "failed": [], "error": "%s: %s" % (type(exc).__name__, exc)}
    m = quality.metrics(res, spec["init_cash"])
    ok, failed = quality.meets(m, bars)
    return {"five": m._asdict(), "meets": ok, "failed": list(failed)}


def final_entry(spec: dict, ctx_kept: "search.Context", ctx_searched: "search.Context", entry: dict,
                bars: quality.Bars = quality.BOOK) -> dict:
    """One kept strategy's final.json entry. `entry` is its finalists.json entry; `spec` is the whole spec, final included.

    held_up is true when it meets `bars` on the kept-back stocks over the screen window AND on the searched stocks over the later window.
    A finalist whose calibration failed has no stop and target to run with: no forward run, held_up false, and the reason.
    `bars` is the TEST HOOK of quality.py.
    """
    head = {"rank": entry["rank"], "trial_id": entry["trial_id"], "picks": entry["picks"]}
    if "calibration_error" in entry:
        return {**head, "kept_back": None, "later_years": None, "held_up": False,
                "reason": "no calibrated stop and target: %s" % entry["calibration_error"]}
    kept = _side(spec, ctx_kept, entry, spec["screen_window"], bars)
    later = _side(spec, ctx_searched, entry, spec["final"]["later_window"], bars)
    return {**head, "kept_back": kept, "later_years": later, "held_up": bool(kept["meets"] and later["meets"])}


def bars_count(frames: dict, end: str) -> dict:
    """{ticker: close bars that are not NaN, from the frames' first bar to the last bar on or before `end`}."""
    close = frames["close"]
    stop = pd.Timestamp(end)
    if close.index.tz is not None:  # a stored-shape dataset has a UTC index; read the date in it, as search._fillable does
        stop = stop.tz_localize(close.index.tz)
    return {str(t): int(n) for t, n in close.loc[:stop].notna().sum().items()}

