"""The three exit lines: the shared home for the ruled risk-control rule (lane B, the exits build,
round 2, 2026-09-01/2026-09-03 nights). Mirrors ``sizing.py``'s own shape member for member
(``docs/briefs/engine_unit_brief.md`` section 2.3, added 2026-09-03 evening by the lead): a frozen
config dataclass (``ExitSpec``), a named-error hierarchy (``ExitError``), a per-process counter dict
(``EXITS_MONITOR_COUNTERS``) and a pure compute function (``compute_exit_lines``). That is not a
fresh convention invented for exits; it is ``sizing.py``'s own shape re-used, which is what makes it
the sustainable choice.

**The three lines, in the ruled order** (``docs/briefs/exits_derivation_addendum_2026-09-01.md``
section 2): a profit line ``x`` volatility units above entry; a trailing stop ``z`` volatility units
below the highest price seen since entry, moving up only, never down; a time limit ``y`` bars after
entry. Whichever is touched first closes the trade; if the time limit arrives with neither price line
touched, the time limit closes it. A fourth, non-rule outcome exists too: the price series can simply
end with none of the three touched, recorded as the distinct reason ``end_of_data`` (addendum section
2.2), because AFML's own answer (drop the observation) would remove exactly the trades that were never
stopped out, which for a trend-following shape is where the winners sit.

**Where the lines go, settled, not this module's business.** ``docs/briefs/engine_unit_brief.md``
section 2.1 settles that these lines are computed here, in pandas, into a boolean ``exits`` series
that both ``from_signals`` call sites already pass; they are NEVER handed to vbt as its own
``sl_stop``/``tp_stop``/``tsl_stop`` keywords (``strategies/base/event_pipeline.py``'s own words: "We
NEVER let vbt re-derive an exit"). This module only computes the series; ``portfolio.py`` and
``sweep_portfolio.py`` are the two places that read it.

**``z`` is read from the one place sizing keeps it (AC-5), never a second time.** ``ExitSpec`` below
carries no ``stop_multiple`` of its own; ``compute_exit_lines`` takes a
``strategies.helpers.sizing.SizingSpec`` and reads ``spec.stop_multiple``/``spec.estimator``/
``spec.estimator_window`` from it, the exact same values that size the position, so a trade meant to
risk 3 percent can never quietly risk something else (``DECISIONS.md`` #314). The two volatility
estimators (``ewma_volatility``/``atr_volatility``) are imported from ``sizing.py``, not
re-implemented, for the same reason: one formula, read from one place.

**The volatility-update rule is DEFERRED (``DECISIONS.md`` #304 Amendment 3), so the stop distance is
frozen at its entry-time value for the whole trade.** The estimator is read ONCE, at entry, and never
re-evaluated mid-trade; only the running HIGH moves the stop level, never a fresh volatility reading.
"Moving up only" binds the stop LEVEL, not the estimate feeding it, so a mid-trade rise in volatility
cannot widen an already-drawn stop (``docs/research_lab/parameter_provenance.md``
``search.trailing_stop_multiple`` Mechanism).

**Causal by construction (the look-ahead trap named in the unit brief's "what can break" item 5).**
The running high since entry is built bar by bar, forward, inside the walk below; a bar's own high can
only ever be compared against the running high AS IT STOOD AT THAT BAR, never against a later bar's
high. The walk below is also where the profit line, the stop breach and the time limit are checked IN
THE SAME PASS, at the SAME bar, in the ruled priority order (profit, then trailing stop, then time
limit, then end-of-data), which a running-extreme computed separately from that walk could not do
without a second, harder-to-read alignment step back onto the same bars.

Pure and pandas-only at module load, no vbt import anywhere in this file, mirroring ``sizing.py``
itself: the catalog introspects ``strategies.helpers`` without needing the engine installed.
"""
from __future__ import annotations  # lazy annotations: import stays light, no eager typing eval

from dataclasses import dataclass  # ExitSpec/ExitResult are plain frozen dataclasses, like sizing.py's own
from typing import TYPE_CHECKING  # keep the pandas import type-check-only, matching sizing.py's own style

from strategies.helpers.sizing import (  # the ONE place both estimators and SizingSpec live (AC-5)
    SizingSpec,  # referenced only for its type in annotations/docs; the value itself is a caller-built instance
    atr_volatility,  # the same ATR formula sizing.py uses, never re-implemented here
    daily_volatility,  # lane P2d: the previous completed day's volatility on bars shorter than a day
    INTRADAY_ESTIMATOR_WINDOW,  # lane P2d: 36 daily bars, named in the warmup refusal below
    ewma_price_volatility,  # the same EWMA formula in price points that sizing.py uses, never re-implemented here
)

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.Series referenced in the annotations below


# Per-process counters for the ops routing law (AC-8/AC-D7). Incremented in-memory ONLY, mirroring
# sizing.py's own SIZING_MONITOR_COUNTERS discipline: no I/O, no print, no logging call, so
# incrementing these can never add a byte to a probe's captured stdout and can never turn a PASS into
# a DRIFT against a committed `.expected` file. Both pairs are reported, count beside population,
# because a counter that found nothing wrong and a counter that examined nothing both read zero
# (exits derivation addendum section 2.2 item 4).
EXITS_MONITOR_COUNTERS: dict[str, int] = {
    "exits_end_of_data": 0,  # a trade whose exit reason was end_of_data (the series ran out, no line fired)
    "exits_end_of_data_population": 0,  # every trade this call closed, end_of_data or not (the denominator)
    "exits_volatility_undefined_bars": 0,  # an entry refused for undefined volatility, no next-bar fill,
    # or (lane E round 4, E-SF6) an undefined price bar reached mid-trade during the forward walk
    "exits_volatility_undefined_bars_population": 0,  # every entry examined for that refusal (the denominator)
}


class ExitError(Exception):
    """Base class for every refusal this module raises, mirroring ``sizing.py``'s ``SizingError``:
    never silently drop a trade, always name which reason fired. Every subclass's message embeds a
    literal ``reason=<token>`` substring, matching this codebase's established convention."""


class InvalidExitParameterError(ExitError):
    """``profit_ratio`` or ``time_limit_bars`` is the wrong type or out of its valid range
    (reason=invalid_parameter). Raised eagerly, before any bar is ever read."""


class UndefinedExitVolatilityError(ExitError):
    """No defined volatility estimate exists at an entry, or the entry has no next bar to fill on
    (reason=undefined_warmup): the same two edges ``sizing.py``'s own ``UndefinedVolatilityError``
    refuses, refused here the same way so a trade exits.py cannot compute is a trade sizing.py could
    never have sized either.

    A THIRD edge shares this class (lane E round 4, E-SF6): a bar partway through an open trade's
    forward walk with an undefined (NaN) high or low (reason=undefined_price_bar). Reachable because
    the one real sweep caller, ``sweep_portfolio.py``'s ``sweep_portfolio_cells`` (lane E round 5,
    E-SF8: ``cell_subportfolio`` never calls this function at all, and takes no ``exit_spec``
    parameter to call it with), passes the FULL, unsliced price column into this walk; only the
    RETURNED sub-portfolio is sliced to a ragged member's own history afterwards
    (``_own_history_bounds``), so a still-open trade can walk into a mid-history data gap or a
    member's own delisting tail before that slicing ever happens. Every comparison against NaN is
    False in Python/pandas, so leaving that bar unchecked would silently treat "we cannot see what
    happened on this bar" as "nothing happened on this bar": the profit line would never touch, the
    trailing stop would never breach and the running high would never move, with no record that
    anything was skipped, and a trade walking into a delisted member's trailing NaN tail would ride
    all the way to a fabricated exit bar whose own price is itself NaN. Refused here instead, the
    same class and counter the entry-time checks above already use, rather than silently proceeding
    on data this walk cannot see. Never reachable from the ordinary LEADING NaN run before a ragged
    member is listed: an entry cannot fire there in the first place (its own volatility or next-bar
    open is undefined, raising this same error from the entry check below first), so this third edge
    only ever fires on a genuine gap or tail reached AFTER a real, validated entry -- never turning an
    ordinary sweep's normal pre-listing padding into a refusal storm.
    """


def _validate_exit_ratios(profit_ratio: float, time_limit_bars: int) -> None:
    """Refuse a bad profit ratio or time limit before any bar is read. Shared by ``ExitSpec``'s own
    ``__post_init__`` and ``compute_exit_lines`` (which takes the two as plain kwargs, per the unit
    brief section 2.3's signature), so the one check is written once, not twice."""
    if isinstance(profit_ratio, bool) or not isinstance(profit_ratio, (int, float)):  # bool is an int subclass
        raise InvalidExitParameterError(
            f"profit_ratio must be a real number (int or float), got {type(profit_ratio).__name__} "
            f"({profit_ratio!r}); reason=invalid_parameter"
        )
    if profit_ratio < 0.0:  # 0 is valid and DISABLES the line (AC-4); negative has no meaning for a long
        raise InvalidExitParameterError(
            f"profit_ratio must be non-negative (0 disables the profit line), got {profit_ratio!r}; "
            "reason=invalid_parameter"
        )
    if isinstance(time_limit_bars, bool) or not isinstance(time_limit_bars, int):  # a bar COUNT, must be int
        raise InvalidExitParameterError(
            f"time_limit_bars must be an int, got {type(time_limit_bars).__name__} "
            f"({time_limit_bars!r}); reason=invalid_parameter"
        )
    if time_limit_bars < 1:  # addendum 2.1: y=0 would close the trade on the bar that opened it, not a trade
        raise InvalidExitParameterError(
            f"time_limit_bars must be at least 1 (0 would close a trade on its own entry bar), got "
            f"{time_limit_bars!r}; reason=invalid_parameter"
        )


@dataclass(frozen=True)  # frozen: a spec is read fresh per call and never mutated, matching SizingSpec
class ExitSpec:
    """One exit-line configuration: ``profit_ratio`` and ``time_limit_bars`` and NOTHING ELSE (AC-5).
    Carries no ``stop_multiple``, no estimator and no estimator window: those live in exactly one
    place, the ``SizingSpec`` a caller also holds, and a second copy here would be the exact defect
    AC-5 forbids. This dataclass is the call-site-level config object (mirroring how a caller builds a
    ``SizingSpec`` and hands it to ``long_only_signal_result``); ``compute_exit_lines`` itself takes
    the two fields as plain keyword arguments rather than this object, so the pure compute function
    stays trivially callable from a probe with no dataclass construction required.

    profit_ratio: the ratio R such that the profit line sits ``R`` STOP DISTANCES above entry
        (``docs/research_lab/parameter_provenance.md`` ``search.profit_take_ratio`` Mechanism: "x = R
        * z"). Default 1.0, the provenance entry's own argued default. 0.0 disables the line.
    time_limit_bars: the time limit ``y`` in BARS, never a calendar span (addendum section 2.1 item
        3). Default 40, the provenance entry's own argued default (``search.time_limit_bars``
        Bounds), which the addendum itself flags as owed a calibration sweep, not a derived number.
    """

    profit_ratio: float = 1.0  # provenance: search.profit_take_ratio, default 1 (R=1: profit line == stop distance)
    time_limit_bars: int = 40  # provenance: search.time_limit_bars, default 40 (owed a calibration sweep)

    def __post_init__(self) -> None:
        """Refuse a bad parameter at CONSTRUCTION time, before any bar is ever read, the same
        discipline ``SizingSpec.__post_init__`` uses."""
        _validate_exit_ratios(self.profit_ratio, self.time_limit_bars)


@dataclass(frozen=True)  # frozen: a finished result is never mutated, matching SizingResult's own contract
class ExitResult:
    """The output of ``compute_exit_lines``: two series, each indexed exactly like the inputs, both
    defined ONLY at the bar a trade actually exits on (elsewhere ``exits`` is False and
    ``exit_reason`` is ``None``). The reason rides beside the boolean series because AC-6 requires an
    end-of-data exit to be distinguishable from a rule-fired one, and a bare boolean cannot carry
    that.
    """

    exits: "pd.Series"  # True only at each trade's own exit bar, False everywhere else
    exit_reason: "pd.Series"  # one of "profit"/"trailing_stop"/"time_limit"/"end_of_data" at an exit bar, else None


def compute_exit_lines(
    close: "pd.Series",  # the run's close series (also the estimator input for the EWMA path)
    high: "pd.Series",  # the run's high series (the ATR path; also read for the profit-line and stop-breach checks)
    low: "pd.Series",  # the run's low series (the stop-breach check reads this)
    open_: "pd.Series",  # the run's open series: a trade fills at the NEXT bar's open (addendum 2.1 item 2)
    entries: "pd.Series",  # boolean entry-signal series, True only on the bar the signal closed on
    sizing_spec: SizingSpec,  # AC-5: z (stop_multiple), the estimator and its window are read from HERE, never a second copy
    *,
    profit_ratio: float,  # the ratio R such that the profit line sits R stop distances above entry (0 disables it)
    time_limit_bars: int,  # the time limit y, in bars, counted from the entry bar (bar zero, addendum 2.1)
    session_day: "pd.Series | None" = None,  # lane P2d: one day label per bar (sizing.session_days). None (the
    # default) is today's path byte for byte; with labels the stop distance is the previous completed
    # day's volatility on daily bars (sizing.daily_volatility), the SAME reading sizing uses.
) -> ExitResult:
    """The ruled rule, in its ruled order (exits derivation addendum section 2), for every entry:

      1. measure volatility from bars that closed BEFORE entry only (the same estimator sizing.py
         uses, read from ``sizing_spec``), and refuse loudly if it is undefined or if no next bar
         exists to fill the entry on (mirroring ``sizing.py``'s own two "undefined_warmup" edges);
      2. freeze the stop distance at its entry-time value for the whole trade (``DECISIONS.md`` #304
         Amendment 3: the volatility-update rule is DEFERRED, so nothing after entry perturbs it);
      3. seed the running high since entry at the entry price itself (addendum section 2.3);
      4. walk forward bar by bar from the fill bar to the series' last bar: update the running high
         CAUSALLY (this bar's own high only, never a later bar's), redraw the stop level from that
         running high (moving up only, since the running high only ever rises), then check in the
         ruled priority order -- profit line touched, then trailing stop breached, then time limit
         reached, then (only if the series itself has run out) end_of_data.

    Every refusal is a named ``ExitError`` subclass whose message embeds ``reason=<token>`` (mirroring
    ``sizing.py``'s AC-6 discipline). No clock-time assumption is baked in anywhere here: only integer
    bar positions and bar-to-bar arithmetic are used, matching ``sizing.py``'s own bar-agnosticism.
    """
    import pandas as pd  # lazy: only when exits are actually computed, never at module import

    _validate_exit_ratios(profit_ratio, time_limit_bars)  # refuse a bad parameter before any bar is read

    # The SAME estimator sizing.py uses, off the SAME SizingSpec (AC-5): one volatility reading, not two.
    if session_day is not None:  # lane P2d: bars shorter than a day, the daily volatility of the last completed day
        volatility = daily_volatility(open_, high, low, close, session_day, sizing_spec)
        warmup_window, warmup_unit = INTRADAY_ESTIMATOR_WINDOW, "daily bars"
    else:
        warmup_window, warmup_unit = sizing_spec.estimator_window, "bars"
        if sizing_spec.estimator == "ewma":  # SizingSpec.__post_init__ already refused any other value
            volatility = ewma_price_volatility(close, sizing_spec.estimator_window)  # price points, as sizing.py
        else:  # sizing_spec.estimator == "atr", the only other possibility once construction-time validation has passed
            volatility = atr_volatility(high, low, close, sizing_spec.estimator_window)

    n_bars = len(close)  # the series length, used below to detect the "no next bar" and "series end" edges
    exits = pd.Series(False, index=close.index, dtype=bool)  # True only at each trade's own exit bar
    exit_reason = pd.Series(None, index=close.index, dtype="object")  # the reason at an exit bar, None elsewhere

    for ts in entries[entries].index:  # walk every TRUE entry timestamp, in chronological (index) order
        pos = close.index.get_loc(ts)  # this entry's integer bar POSITION (never its calendar timestamp)

        EXITS_MONITOR_COUNTERS["exits_volatility_undefined_bars_population"] += 1  # this entry was examined

        vol_at_entry = volatility.iloc[pos]  # this entry's own volatility reading, from pre-entry bars only
        no_next_bar = pos + 1 >= n_bars  # the t-zero-adjacent edge: no next bar exists to fill the entry on
        if pd.isna(vol_at_entry) or no_next_bar:
            EXITS_MONITOR_COUNTERS["exits_volatility_undefined_bars"] += 1
            raise UndefinedExitVolatilityError(
                f"entry at {ts}: "
                + (f"volatility is undefined ({sizing_spec.estimator} needs {warmup_window} "
                   f"warmed-up {warmup_unit}; not enough history precedes this entry)" if pd.isna(vol_at_entry)
                   else "this is the series' own last bar; no next-bar open exists to fill this position on")
                + "; reason=undefined_warmup"
            )

        entry_price = open_.iloc[pos + 1]  # addendum 2.1 item 2: fires at this bar's close, fills at next open
        if pd.isna(entry_price):  # a missing next-bar open (e.g. a ragged, not-yet-listed sweep column)
            EXITS_MONITOR_COUNTERS["exits_volatility_undefined_bars"] += 1
            raise UndefinedExitVolatilityError(
                f"entry at {ts}: the next bar's open is missing (NaN); no fill price to draw the exit "
                "lines from; reason=undefined_warmup"
            )

        # Frozen for the whole trade (Amendment 3, DEFERRED): read ONCE here, never redrawn mid-trade.
        stop_distance = vol_at_entry * sizing_spec.stop_multiple  # AC-5: the SAME multiple sizing divides by
        # AC-4: profit_ratio=0.0 DISABLES the line entirely (never a barrier sitting at zero distance).
        profit_line = entry_price + profit_ratio * stop_distance if profit_ratio > 0.0 else None

        running_high = entry_price  # addendum 2.3: seeded at the entry price itself, nothing carried over
        fill_pos = pos + 1  # the bar the position actually opens on; the walk below starts here (inclusive)

        exit_pos = None  # set by whichever branch below fires; the loop always sets it by its last iteration
        reason = None
        for bar in range(fill_pos, n_bars):  # walk forward, one bar at a time, from the fill bar to the series' end
            bar_high = high.iloc[bar]  # this bar's own high; never a later bar's (the look-ahead trap, p09)
            bar_low = low.iloc[bar]  # this bar's own low; the stop-breach check reads it

            if pd.isna(bar_high) or pd.isna(bar_low):  # E-SF6: a mid-history data gap or the member's own
                # delisting tail (the one sweep caller, sweep_portfolio_cells, passes the FULL, unsliced
                # column into this walk; see UndefinedExitVolatilityError's own docstring for the full
                # argument). Every comparison
                # against NaN below is silently False, so left unchecked this bar would look exactly like
                # an ordinary quiet bar instead of a bar we cannot see -- refuse loudly here instead.
                EXITS_MONITOR_COUNTERS["exits_volatility_undefined_bars"] += 1
                raise UndefinedExitVolatilityError(
                    f"entry at {ts}: bar {bar} has an undefined high or low (high={bar_high!r}, "
                    f"low={bar_low!r}) partway through this trade's forward walk; cannot evaluate the "
                    "profit line, trailing stop or time limit without a real price on this bar; "
                    "reason=undefined_price_bar"
                )

            if bar_high > running_high:  # CAUSAL update: only this bar's own high can raise the running high
                running_high = bar_high
            stop_level = running_high - stop_distance  # moving up only, since running_high only ever rises

            profit_touched = profit_line is not None and bar_high >= profit_line  # a disabled line never touches
            stop_breached = bar_low <= stop_level  # the trailing stop, redrawn from the (possibly just-updated) high
            time_limit_reached = (bar - pos) >= time_limit_bars  # bar zero is the entry bar (addendum 2.1)

            if profit_touched:  # ruled priority: profit line checked first (job file "checks profit, then trailing_stop")
                exit_pos, reason = bar, "profit"
                break
            elif stop_breached:  # checked second, so a same-bar profit touch always wins over a same-bar breach
                exit_pos, reason = bar, "trailing_stop"
                break
            elif time_limit_reached:  # "if the time limit arrives with neither price line touched" (addendum section 2)
                exit_pos, reason = bar, "time_limit"
                break
            elif bar == n_bars - 1:  # the series itself has run out: no line fired, ever, on this trade
                exit_pos, reason = bar, "end_of_data"
                break
            # else: none of the four fired on this bar; continue walking forward to the next one

        EXITS_MONITOR_COUNTERS["exits_end_of_data_population"] += 1  # this trade closed, one way or another
        if reason == "end_of_data":
            EXITS_MONITOR_COUNTERS["exits_end_of_data"] += 1  # counted on its own, never folded into time_limit

        exits.iloc[exit_pos] = True  # this trade's own exit bar, and no other
        exit_reason.iloc[exit_pos] = reason  # the reason beside the boolean, so end_of_data stays distinguishable

    return ExitResult(exits=exits, exit_reason=exit_reason)
