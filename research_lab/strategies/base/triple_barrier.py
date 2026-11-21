"""The triple-barrier labeling engine (M3.5, EL50-EL53).

One library-level engine, shared by every event strategy (§10; 2b §3, R3, R12). It
is path-dependent and first-touch (AFML §3.4): given an event's price path it walks
the bars forward from the entry and labels the observation by which barrier the path
touches first, never by comparing only the window's start and end (R12).

AFML cross-check (CLAUDE.md books rule; the PDF is laptop-only / gitignored):
  - §3.4 "The Triple-Barrier Method" (PDF p.77): "labels an observation according to
    the first barrier touched out of three"; two horizontal barriers (profit-taking,
    stop-loss) that are "a dynamic function of estimated volatility", one vertical
    barrier "in terms of number of bars elapsed". Upper first -> +1, lower first -> -1,
    vertical first -> the sign of the return (the author's preferred choice over 0).
    Path-dependent over [t_{i,0}, t_{i,0}+h].
  - §3.3 + Snippet 3.1 "Daily Volatility Estimates" (PDF p.76): the volatility estimate
    "applies a span of span0 days to an exponentially weighted moving standard deviation"
    of returns. We elect this EWMA-std-of-returns form (EL53), with the only tunable the
    span (`vol_window`, in bars here rather than days, since the framework's bars set the
    cadence). Electing this specific estimator form is OPEN-5 for Cowork (§22).

Modeling choices this engine makes that sit below the spec's resolution (surfaced for
Cowork, documented here per CLAUDE.md "name the decision, don't bury it"):
  1. Intrabar high/low first-touch. A barrier is "touched" when a bar's High reaches the
     upper barrier or its Low reaches the lower barrier. This is more faithful to §10's
     "walks the intra-window price path" (the OHLC frame is passed in for exactly this)
     and to how a real stop-loss / profit-take order fills (intrabar), and it is a
     documented deviation from AFML's reference Snippet 3.2, which tests close prices only
     because that snippet carries only a close series.
  2. Same-bar tie-break: if one bar's High reaches the upper barrier AND its Low reaches
     the lower barrier, the stop-loss resolves first. Intrabar order is unknowable from
     OHLC, so a risk system assumes the adverse touch came first (conservative).
  3. No-lookahead volatility. Entry is the next bar's open after knowledge_at (EL52), so
     the volatility known at the decision point is the EWMA std as of the bar BEFORE entry
     (its close is the last price known when the entry order is placed). Using the entry
     bar's own close return would peek at a price that forms after the fill.
  4. Horizontal-touch outcome return is the barrier level itself (+/- mult * vol): the
     exit fills at the barrier. Vertical-timeout and truncated outcomes use the resolving
     bar's close.
  5. A disabled barrier (its multiple is 0) is never touched, matching AFML's "If 0, there
     will not be an upper/lower barrier" (the [pt, sl, t1] configurations).

Truncated vs zero-bar (EL51, R16, Q10): two distinct outcomes, neither ever labelled as
if it completed and neither ever silently dropped:
  - A truncated path (>= 1 real bar after entry but the series ends before any barrier) is
    a forced exit at the last available bar, labelled by the sign of the realized return
    there, with `truncated=True` — the same exit a real delisting forces, fabricating no
    barrier touch.
  - A zero-bar uncovered event (no price series, or no bar strictly after knowledge_at to
    enter on, or no usable run-up volatility — both a non-estimable NaN vol AND a degenerate
    zero vol from a flat run-up, which would otherwise collapse the barriers and fabricate a
    same-bar tie) returns None; the engine drops it from the labelled set and counts it in the
    run's coverage figure (§5). There is no real bar to force-exit at (or no usable barrier to
    place), so it cannot be labelled honestly.

Input contract: `price_path` is an OHLC frame (columns Open/High/Low/Close) indexed by
epoch seconds (UTC) — either an integer epoch index or a tz-aware DatetimeIndex, both
normalized internally. Returning epoch-second timestamps keeps the label spans in the
same axis as the store's `knowledge_at` and the purge/embargo primitive (brief G, §11).
"""
from __future__ import annotations  # lazy annotations, consistent with base/

from dataclasses import dataclass  # the label is a plain frozen record

import numpy as np  # vectorized returns + sign; engine-side, not the light contract surface
import pandas as pd  # the EWMA std (AFML Snippet 3.1 form) + the OHLC frame


@dataclass(frozen=True)
class BarrierLabel:
    """The outcome of labeling one event (the label plus the span purge/embargo consumes)."""

    label: int  # +1 upper first / -1 lower first / sign(realized return) on time-out
    outcome_return: float  # realized return at the resolving bar (fraction)
    entry_ts: int  # epoch s of the entry bar (the next-bar open after knowledge_at, EL52)
    exit_ts: int  # epoch s of the resolving bar
    label_start: int  # = entry_ts; the label span start fed to purge/embargo (EL54, §11)
    label_end: int  # = exit_ts; the label span end
    truncated: bool  # True = forced exit on a path that ended before any barrier (EL51)
    exit_kind: str  # which barrier resolved the label (BN-J1-1); the J1 fill model reads it to pick fill mechanics


_OHLC_COLUMNS = ("Open", "High", "Low", "Close")  # the columns label_event reads

# The exit kind the J1 fill model (brief J1 §6.2) reads to choose the realized-fill mechanics
# (BN-J1-1). Named SIDE-NEUTRAL by the barrier the path touched, NOT long-centric stop/take-profit:
# a short flips which barrier is favorable, so the fill model combines this with the trade side.
EXIT_UPPER_BARRIER = "upper_barrier"  # the +1 touch: upper barrier first (favorable for a long, adverse for a short)
EXIT_LOWER_BARRIER = "lower_barrier"  # the -1 touch: lower barrier first (adverse for a long, favorable for a short)
EXIT_TIMEOUT = "timeout"              # vertical barrier reached without a horizontal touch (resolving-close exit)
EXIT_TRUNCATED = "truncated"          # series ended before any barrier (forced last-available-close exit, EL51)


def _epoch_seconds(index: "pd.Index") -> np.ndarray:
    """Normalize a price-path index (epoch-int or tz-aware datetime) to epoch seconds (int64)."""
    if isinstance(index, pd.DatetimeIndex):  # real OHLCV frames carry a tz-aware DatetimeIndex
        return index.asi8 // 1_000_000_000  # asi8 is int64 nanoseconds (UTC); -> seconds
    return np.asarray(index, dtype=np.int64)  # already epoch seconds


def label_event(
    price_path: "pd.DataFrame",  # OHLC bars for the event's trade_time_symbol, run-up through the horizon
    knowledge_at: int,  # the event's public-knowledge time (epoch s)
    profit_take_mult: float,  # upper-barrier multiple of the vol estimate (0 disables it; EL53, EL56)
    stop_loss_mult: float,  # lower-barrier multiple of the vol estimate (0 disables it)
    vertical_horizon: int,  # max holding, in bars after entry (EL56)
    vol_window: int,  # EWMA-std span, in bars (the AFML Snippet 3.1 form, EL53, EL56)
) -> "BarrierLabel | None":  # None = zero-bar uncovered event (drop-and-count, EL51)
    """Label one event by the first of three barriers its price path touches.

    Returns a ``BarrierLabel`` for a labelable event (a real entry bar plus an estimable
    volatility), or ``None`` for a zero-bar uncovered event (no series, no entry bar after
    ``knowledge_at``, or no estimable run-up volatility), which the engine counts (§5).
    """
    # Parameter validation: a misconfigured caller must fail loudly, never silently mislabel
    # (CLAUDE.md "fail with a clear error"). A 0 multiple disables that barrier (AFML config);
    # a negative one is invalid. vertical_horizon < 1 has no holding period; vol_window < 2
    # cannot form an EWMA std (it needs >= 2 returns). These are config errors, not coverage gaps.
    if profit_take_mult < 0:
        raise ValueError(f"profit_take_mult must be >= 0 (0 disables the barrier), got {profit_take_mult}")
    if stop_loss_mult < 0:
        raise ValueError(f"stop_loss_mult must be >= 0 (0 disables the barrier), got {stop_loss_mult}")
    if vertical_horizon < 1:
        raise ValueError(f"vertical_horizon must be >= 1 bar, got {vertical_horizon}")
    if vol_window < 2:
        raise ValueError(f"vol_window must be >= 2 bars to form an EWMA std, got {vol_window}")

    missing = [c for c in _OHLC_COLUMNS if c not in price_path.columns]  # OHLC is the contract
    if missing:  # a malformed frame is a programming error, not a coverage gap -> fail loudly
        raise ValueError(f"price_path is missing required OHLC columns: {missing}")

    if len(price_path) == 0:  # no price series at all -> zero-bar uncovered (EL51)
        return None

    epoch = _epoch_seconds(price_path.index)  # bar timestamps as epoch seconds
    if epoch.size > 1 and not bool((np.diff(epoch) > 0).all()):  # searchsorted below assumes ascending bars
        raise ValueError("price_path index must be strictly increasing epoch seconds; got a non-monotonic index")
    open_ = price_path["Open"].to_numpy(dtype=float)  # fill price for the entry
    high = price_path["High"].to_numpy(dtype=float)  # intrabar upper-barrier touch test
    low = price_path["Low"].to_numpy(dtype=float)  # intrabar lower-barrier touch test
    close = price_path["Close"].to_numpy(dtype=float)  # returns (vol), time-out and truncated outcome

    # Entry: the first bar strictly after knowledge_at (the next-bar open, EL52). searchsorted
    # with side="right" gives the first position whose timestamp is > knowledge_at.
    entry_pos = int(np.searchsorted(epoch, knowledge_at, side="right"))
    if entry_pos >= len(epoch):  # no tradeable bar after the event -> zero-bar uncovered (EL51)
        return None

    # No-lookahead volatility (choice 3): the EWMA std of close-to-close returns as of the bar
    # BEFORE entry, the last price known when the entry order is placed at the next open.
    if entry_pos < 1:  # entry is the very first bar -> no run-up -> no estimable volatility
        return None
    returns = pd.Series(close).pct_change()  # bar-to-bar simple returns (first is NaN)
    vol_series = returns.ewm(span=vol_window).std()  # AFML Snippet 3.1 EWMA-std-of-returns form
    vol = vol_series.iloc[entry_pos - 1]  # vol as of the last fully-known bar before entry
    # A zero vol is finite but degenerate: it collapses both barriers onto the entry price, so the
    # entry bar trips touched_up AND touched_dn and the same-bar tie-break fabricates an instant -1
    # on a name that never moved. Treat zero vol as unusable -> uncovered (drop-and-count), the same
    # survivorship-safe handling as a NaN vol; never a fabricated label.
    if not np.isfinite(vol) or vol <= 0.0:  # no usable volatility to scale barriers by -> uncovered (counted)
        return None

    entry_price = open_[entry_pos]  # fill at the entry bar's open
    if not np.isfinite(entry_price) or entry_price <= 0:  # a 0/NaN open is corrupt data, not a coverage gap
        raise ValueError(f"entry bar open is not a valid price: {entry_price} at epoch {int(epoch[entry_pos])}")
    upper = entry_price * (1.0 + profit_take_mult * vol)  # profit-take barrier (active iff mult > 0)
    lower = entry_price * (1.0 - stop_loss_mult * vol)  # stop-loss barrier (active iff mult > 0)

    last_pos = len(epoch) - 1  # last available real bar
    vertical_pos = entry_pos + vertical_horizon  # the time-out bar (may lie beyond the data)
    walk_end = min(vertical_pos, last_pos)  # walk only over bars that exist

    # First-touch walk, intrabar high/low (choice 1), from the entry bar through the horizon.
    for pos in range(entry_pos, walk_end + 1):
        touched_up = profit_take_mult > 0 and high[pos] >= upper  # disabled barrier never touches
        touched_dn = stop_loss_mult > 0 and low[pos] <= lower
        if touched_dn:  # stop-loss; also wins a same-bar tie with the profit-take (choice 2)
            return BarrierLabel(
                label=-1,
                outcome_return=-stop_loss_mult * vol,  # exit at the lower barrier (choice 4)
                entry_ts=int(epoch[entry_pos]),
                exit_ts=int(epoch[pos]),
                label_start=int(epoch[entry_pos]),
                label_end=int(epoch[pos]),
                truncated=False,
                exit_kind=EXIT_LOWER_BARRIER,  # lower barrier touched first (BN-J1-1)
            )
        if touched_up:  # profit-take touched first (no stop-loss this bar)
            return BarrierLabel(
                label=1,
                outcome_return=profit_take_mult * vol,  # exit at the upper barrier (choice 4)
                entry_ts=int(epoch[entry_pos]),
                exit_ts=int(epoch[pos]),
                label_start=int(epoch[entry_pos]),
                label_end=int(epoch[pos]),
                truncated=False,
                exit_kind=EXIT_UPPER_BARRIER,  # upper barrier touched first (BN-J1-1)
            )

    # No horizontal touch within the available bars.
    if vertical_pos <= last_pos:  # the vertical barrier exists in the data -> time-out
        ret = close[vertical_pos] / entry_price - 1.0  # realized return at the time-out bar
        return BarrierLabel(
            label=int(np.sign(ret)),  # sign of the realized return (AFML §3.4 preferred choice)
            outcome_return=ret,
            entry_ts=int(epoch[entry_pos]),
            exit_ts=int(epoch[vertical_pos]),
            label_start=int(epoch[entry_pos]),
            label_end=int(epoch[vertical_pos]),
            truncated=False,
            exit_kind=EXIT_TIMEOUT,  # vertical barrier reached, no horizontal touch (BN-J1-1)
        )

    # The series ended before the vertical barrier and before any horizontal touch, but with at
    # least one real bar after entry -> forced exit at the last available bar (truncated, EL51).
    ret = close[last_pos] / entry_price - 1.0  # realized return at the last real bar
    return BarrierLabel(
        label=int(np.sign(ret)),  # labelled by the sign of the realized return to that bar
        outcome_return=ret,
        entry_ts=int(epoch[entry_pos]),
        exit_ts=int(epoch[last_pos]),
        label_start=int(epoch[entry_pos]),
        label_end=int(epoch[last_pos]),
        truncated=True,  # the visible flag a study filters on to exclude forced exits
        exit_kind=EXIT_TRUNCATED,  # series ended before any barrier (BN-J1-1)
    )
