"""The walk-forward slicing primitive (lane WF, 6.2b): where the split itself actually runs.

`#329` ruling 1 puts the split inside the sweep entrypoint (`run_sweep_entrypoint.py`), never in
the backend runner (`dashboard/backend/stage_runner.py` imports nothing from this module or from
any other engine path, AC-10/SR-16). Two pure functions live here, each proven directly by
`research_lab/probes/walk_forward/wf04_purge_embargo_derived_never_chosen*` and
`wf05_boundary_holds_after_purge_embargo*`:

  - `derive_purge_and_embargo` answers AC-16's own words: the purge horizon is the strategy's
    longest hold in bars (its own declared time-limit where one is declared, else the maximum
    realized hold of an in-sample trade record), and the embargo is the strategy's own lookback.
    Neither is ever CHOSEN.
  - `compute_slice_bounds` answers WHERE each in-sample/out-of-sample boundary sits, for both the
    `rolling` (fixed-length, sliding) and `anchored` (fixed start, growing) schemes, after the
    purge/embargo pullback is applied.

A DELIBERATE DEVIATION, disclosed rather than silent (the failure-stop rule): `#329` ruling 1's own
words are "vbt is used as shipped for grids, walk-forward, sizing, stops, fills and statistics...
add only what it does not have, keep each added piece small." `vbt.Splitter`'s own walk-forward
constructor, `from_purged_walkforward`, is FOLD-COUNT parameterized (`n_folds`, `n_test_folds`,
`min_train_folds`), never BAR-COUNT parameterized, and its purge/embargo are time-deltas
(`purge_td`), not the bar-count values `#392` ruling 4 froze (`in_sample_bars=84`,
`out_of_sample_bars=25`). Translating one parameterization into the other, or reaching deep enough
into `vectorbtpro`'s pinned build to verify `Splitter.take`'s exact return shape against the
byte-for-byte contract this lane's OWN probes already commit to (`compute_slice_bounds`'s plain
`(bar_dates, window_start_ts, window_end_ts, in_sample_bars, out_of_sample_bars, anchoring,
purge_bars, embargo_bars) -> list[dict]` shape, reviewed and committed by the lead before this
build started), is not something this dispatch can do without either fabricating an unverified
signature (the hard prohibition: "verify or say unknown") or a live MCP/vbt-source read this
dispatch was not given. This module is therefore a SMALL, book-cited, hand-verified arithmetic
primitive rather than a `vbt.Splitter` wrapper -- exactly the "add only what it does not have"
half of the same ruling, sized to the one thing vbt's own stock constructors do not offer in this
exact bar-count parameterization.

Two book sentences, read live from their PDFs at build time (`#216`), quoted byte for byte below:

AFML section 7.4.1 (Lopez de Prado, PDF p.149; the purge_embargo.py precedent's own page number,
re-confirmed against the PDF for this build):
    "purge from the training set all observations whose labels overlapped in time with those
  labels included in the testing set." (the two-space indent on the continuation line above is
  the exact byte this module's own probe, wf04, checks for; do not reflow this quote)

Masters, Testing and Tuning Market Trading Systems, "The General Walkforward Algorithm" section,
PDF page 141 (rendered as a PNG via `pdftoppm -r 150` and read directly; NOT the summary notes
file `docs/books/masters_trading_systems.md`, whose own paraphrase at line 522 is NOT the book's
text and was the round-2 fold's own finding on this lane):
    "1) We must set OMIT = min (LOOKAHEAD, LOOKBACK) – 1 to avoid
     deadly optimistic bias. This is crucial." (the dash before "1" is an EN DASH, kept exactly as
  the PDF has it; OMIT is the book's own name for what this module calls the embargo/purge pull-
  back, min(lookahead, lookback) minus one training case omitted at the fold boundary)
"""
from __future__ import annotations  # defers every annotation to a string


def derive_purge_and_embargo(time_limit_bars: "int | None", trade_holds: list, lookback_bars: int
                             ) -> "tuple[int, int]":
    """AC-16: purge and embargo, DERIVED, never chosen.

    The purge horizon is the strategy's own longest hold in bars: its declared `time_limit_bars`
    (a maximum-holding-period exit the strategy itself enforces) when one is declared, else the
    MAXIMUM realized hold across `trade_holds` (a plain list of per-trade bar counts from an
    in-sample run); an empty trade record with no declared limit derives a purge of zero (a real,
    meaningful absence, never a guessed positive number -- AFML's own purge exists to remove
    training bars a trade's OWN hold could still overlap the test set with, and a strategy that
    never held a trade in-sample overlaps nothing).

    The embargo is always the strategy's own declared lookback (`lookback_bars`, the strategy's own
    `Strategy.lookback_bars(combo)` method -- e.g. a moving-average crossover's own longest SMA
    window for that combo; fold item 3, round-1 review finding U2, corrected FROM an earlier build
    that passed the sizing estimator's own window here instead, which answers a different question),
    regardless of which purge branch fired: it answers a DIFFERENT question (AFML §7.4.2, the
    serial-correlation buffer after the test set; Masters' own OMIT, the lookahead/lookback
    minimum), never derived from the trade record at all.

    Raises ValueError on a negative declared `time_limit_bars` (a caller contract violation, never
    silently turned into zero or a positive number by sign-flipping).
    """
    if time_limit_bars is not None and time_limit_bars < 0:
        raise ValueError(
            f"time_limit_bars must be non-negative when declared, got {time_limit_bars}; "
            "refusing to derive a negative purge horizon"
        )
    if time_limit_bars is not None:
        purge_bars = time_limit_bars  # the strategy's own declared maximum hold wins outright
    elif trade_holds:
        purge_bars = max(trade_holds)  # the maximum REALIZED hold, never a chosen constant
    else:
        purge_bars = 0  # no declared limit and no trade at all: a real, meaningful absence
    return purge_bars, lookback_bars


def compute_slice_bounds(bar_dates: list, window_start_ts: int, window_end_ts: int,
                         in_sample_bars: int, out_of_sample_bars: int, anchoring: str,
                         purge_bars: int, embargo_bars: int) -> list:
    """WF-1/AC-18/AC-20: every in-sample/out-of-sample slice this scheme produces over one
    ticker's own bar series, entirely inside the half-open granted window `[window_start_ts,
    window_end_ts)` (the convention this whole build uses, matching `dispatch_stage`'s own
    window semantics).

    `bar_dates` is `[(label, epoch_ts), ...]`, every bar this ticker's own store file holds, in
    ascending time order (the ENTRYPOINT builds this from a member's loaded OHLC index; `label` is
    carried only for a caller's own convenience and this function never reads it). `anchoring` is
    `'rolling'` (a FIXED-length in-sample window that SLIDES forward each slice) or `'anchored'`
    (a window with a FIXED start that GROWS each slice) -- the ruled `#392` value is `'rolling'`.

    Each slice's in-sample block is `in_sample_bars` long (before any purge), starting where the
    scheme places it; its out-of-sample block is `out_of_sample_bars` long, starting `embargo_bars`
    bars after the in-sample block's own UNPULLED end (embargo pulls the out-of-sample START
    forward, never touches the in-sample end); the in-sample block's own END is then pulled BACK by
    `purge_bars`, clamped so it never precedes the in-sample block's own start (a real, non-empty
    slice, never a negative-length one). Both pullbacks are derived ONCE (by the caller, from slice
    0's own unpulled bounds -- `derive_purge_and_embargo`'s own docstring) and applied identically
    to every slice this function computes, so the boundary math here never re-derives either value.

    A slice is included only when its WHOLE out-of-sample leg still fits inside the window (its own
    out-of-sample end bar's timestamp is strictly less than `window_end_ts`); the scheme stops
    advancing the moment the next slice would not fit, rather than truncating one. Returns `[]`
    when even the first slice cannot fit (AC-36's own refusal is the caller's job, checked BEFORE
    this function ever runs, against the same `in_sample_bars + 2 * out_of_sample_bars` floor).
    """
    if anchoring not in ("rolling", "anchored"):
        raise ValueError(f"anchoring must be 'rolling' or 'anchored', got {anchoring!r}")

    n = len(bar_dates)
    start_idx = None  # the first bar at or after window_start_ts
    for i in range(n):
        if bar_dates[i][1] >= window_start_ts:
            start_idx = i
            break
    if start_idx is None:
        return []  # the window starts after every bar this series holds

    end_idx_exclusive = n  # default: the window's own end reaches past every bar this series holds
    for i in range(start_idx, n):
        if bar_dates[i][1] >= window_end_ts:
            end_idx_exclusive = i
            break
    last_usable_idx = end_idx_exclusive - 1  # the last bar strictly inside [window_start_ts, window_end_ts)
    if last_usable_idx < start_idx:
        return []  # no bar at all inside the window

    slices = []
    i = 0
    while True:
        if anchoring == "rolling":
            is_start_idx = start_idx + i * out_of_sample_bars  # the fixed-length window slides forward
        else:  # anchored: the in-sample block always starts at the window's own start
            is_start_idx = start_idx
        # anchored grows the in-sample block by out_of_sample_bars each slice; rolling keeps it fixed.
        raw_is_end_idx = is_start_idx + in_sample_bars - 1 if anchoring == "rolling" else \
            start_idx + i * out_of_sample_bars + in_sample_bars - 1
        oos_start_idx = raw_is_end_idx + 1 + embargo_bars  # embargo pulls the OOS start forward, off the UNPULLED IS end
        oos_end_idx = oos_start_idx + out_of_sample_bars - 1
        if oos_end_idx > last_usable_idx:
            break  # this slice's own out-of-sample leg no longer fits inside the granted window; stop, never truncate
        is_end_idx = max(is_start_idx, raw_is_end_idx - purge_bars)  # the purge pullback, clamped to a real, non-empty slice
        slices.append({
            "in_sample_start_ts": bar_dates[is_start_idx][1],
            "in_sample_end_ts": bar_dates[is_end_idx][1],
            "out_of_sample_start_ts": bar_dates[oos_start_idx][1],
            "out_of_sample_end_ts": bar_dates[oos_end_idx][1],
            "slice_index": i,  # a caller convenience; the probes read the four *_ts keys directly
        })
        i += 1
    return slices
