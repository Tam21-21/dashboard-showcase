"""Position sizing: the shared home for the ruled sizing rule (Lane A, 2026-09-01 night-five campaign).

Both shared ``from_signals`` call sites (``strategies/helpers/portfolio.py`` and
``strategies/helpers/sweep_portfolio.py``) read a position size from exactly this module (AC-1), so
the single-run path and the wide sweep path can never drift apart in a separately hand-written size
calculation. Before this unit neither call site passed ``size``/``size_type`` at all, so vbt's own
default (unbounded amount, effectively "spend all available cash on every entry") governed every run
silently (docs/briefs/sizing_unit_brief.md, ruling #292).

**The ruled rule, in its ruled order** (docs/briefs/sizing_derivation_2026-09-01.md section 2, ruled
by docs/handoffs/RULINGS_2026-09-01_sizing_review.md ruling 1): at signal time, measure volatility
from bars that closed BEFORE the entry only; turn that into a stop distance (a multiple of the
measured volatility); decide the risk budget (a fixed fraction of equity); divide the risk budget by
the STOP DISTANCE, not the price (derivation 3.4: the joint step neither book prints, argued and
adopted there); cap the position inside the slice; round down to whole shares (derivation gap 2: vbt
and NautilusTrader both refuse fractional share orders, a tool constraint, not a choice); and refuse
loudly, never silently shrink, when the answer is unaffordable, capped, or rounds to zero (AC-6).

**Bar-agnostic by construction (AC-5).** Every calculation below reads only bar ORDER and bar COUNT
(``.shift()``, ``.rolling()``, ``.ewm()``, integer positions via ``Index.get_loc``); nothing here ever
reads a timestamp's own calendar spacing, so the identical code prices an hourly bar and a daily bar
the same way. Verified empirically against the real EODHD daily and hourly AAPL stores by
``research_lab/probes/sizing/s06_ac5_bar_agnostic_real_daily_hourly.probe.py``.

Pure and pandas-only at module load, no vbt import anywhere in this file, mirroring
``annualization.py`` / ``costs.py`` / ``funnel.py`` in this same package: the catalog introspects
``strategies.helpers`` without needing the engine installed, so pandas itself is imported lazily
inside each function rather than at module load.
"""
from __future__ import annotations  # lazy annotations: import stays light, no eager typing eval

import math  # stdlib, zero import cost: math.floor is the whole-share rounding step (AC-3 step 6)
from dataclasses import dataclass  # SizingSpec/SizingResult are plain frozen dataclasses, like RunResult
from typing import TYPE_CHECKING  # keep the pandas import type-check-only, matching this package's own style

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.Series referenced in the annotations below
    from typing import Callable  # AC-9's fixed_fee_func annotation; __future__ annotations makes this string-only


# Per-process counters for the ops routing law (docs/briefs/sizing_unit_brief.md "what can break").
# Incremented in-memory ONLY, deliberately: no I/O, no print, no logging call, so incrementing these
# can never add a byte to a probe's captured stdout/stderr and can never turn a PASS into a DRIFT
# against a committed `.expected` file (scripts/run_probes.sh diffs the two exactly). A future
# ops-wiring unit reads these three counters from the worker process, the only place research_lab/
# can report anything from, since this area has no database access (research_lab/CLAUDE.md), and
# forwards them into docs/ops/ops_monitor_registry.md's rows, which this unit's Amendment 1 explicitly
# leaves for the lead to add on `main` after both night-five lanes merge. The two OTHER declared
# failure modes (sweep_param_coverage, sizing_stop_multiple_agreement) are not runtime counters: see
# this unit's builder report for why each is covered a different way.
SIZING_MONITOR_COUNTERS: dict[str, int] = {
    "sizing_undefined_warmup": 0,  # an entry with no valid volatility measurement or no next-bar fill
    "sizing_affordability_resizes": 0,  # a final (capped, rounded) share count that costs more than equity after fees
    "sizing_cap_limited_entries": 0,  # a raw share count that would exceed the hard cap
    # Lane LB (5.8e, DECISIONS #406 ruling 2): every path that removes a TRUE entry signal sitting on
    # its own frame's own last bar increments this ONE shared, in-process counter, whether that removal
    # ends in the sheet's item 2(i) fallback (no admissible reach: opens nothing, counted) or in a
    # priced reach (a recorded fill). This is the ONLY signal for a path with no per-run database row to
    # carry a count at all (the sweep modes, sweep_portfolio.py's own "what can break" section): every
    # OTHER path (the single run, walk-forward) ALSO increments it here, so the two signals (this
    # counter and research_run_stats.entries_masked_at_end) always agree on which class of event
    # happened, even where only one of them can durably record how often.
    "entries_masked_at_end": 0,
}


class SizingError(Exception):
    """Base class for every refusal this module raises (AC-6): never silently shrink a position,
    always name which reason fired. Every subclass's message embeds a literal ``reason=<token>``
    substring so a caller can grep the reason without a separate structured attribute, matching this
    codebase's own established convention (``strategies/helpers/annualization.py``'s
    ``AnnualizationError`` embeds ``no_dominant_gap`` the same way). Catching this base class (as
    ``research_lab/probes/sizing/d03_ac9_real_artifact_daily_hourly_execution_impl.py`` does) catches
    every refusal this module can ever raise.
    """


def refusal_reason(message: str) -> str:
    """Lane D1: the ``reason=<token>`` a SizingError message carries (this module's own convention, see
    SizingError), or ``"unknown"`` for a message without one, so a recorded refusal always has a reason."""
    marker = "reason="
    if marker not in message:
        return "unknown"
    return message.rsplit(marker, 1)[1].split()[0].strip(";,.")


class InvalidSizingParameterError(SizingError):
    """A ``SizingSpec`` field is the wrong type, or names an estimator that does not exist
    (reason=invalid_parameter_type). Raised at CONSTRUCTION time, before any bar is ever read."""


class InvalidRiskFractionError(SizingError):
    """A well-typed ``risk_fraction`` outside its valid range, ``(0, 1.0]``
    (reason=invalid_risk_fraction). Raised at CONSTRUCTION time."""


class UndefinedVolatilityError(SizingError):
    """No defined volatility estimate exists at an entry (not enough warmed-up history, a
    degenerate zero-volatility window, or no next bar to fill the entry on: the t-zero-adjacent
    edge), so no stop distance and no share count can be computed (reason=undefined_warmup)."""


class ZeroShareSizeError(SizingError):
    """The risk budget divided by the stop distance rounds down to zero whole shares
    (reason=zero_shares). Refused rather than silently sizing a zero (no-op) position."""


class CapLimitedSizeError(SizingError):
    """The raw (uncapped) share count would exceed the hard cap on the slice's own equity
    (reason=cap_limited). The cap REFUSES the trade; it never silently shrinks the position to fit
    (derivation 3.1's own warning about calm-then-crazy markets is exactly the risk this refusal
    exists to surface rather than paper over)."""


class UnaffordableSizeError(SizingError):
    """The final (already capped, already rounded) share count costs more than the available
    equity once fees are applied (reason=unaffordable)."""


class UndefinedFactorError(SizingError):
    """Lane D1 (real-share sizing): the per-day adjustment factor at an entry's fill bar is missing,
    NaN or not positive, so the as-traded price (adjusted price / factor) is unknown and no real share
    count can be computed (reason=undefined_factor)."""


class InsufficientLeadInError(SizingError):
    """Lane SW (5.8), the sizing warmup lead-in (`DECISIONS.md` #383 decision 2, condition 1): fewer
    than ``estimator_window`` bars exist before ``window_start_ts`` in the member's own series. The run
    refuses ALL OR NOTHING here: never a shorter lead-in, never a silent fallback to the old
    ``[window_start, window_end]``-only clip (which would quietly load a lead-in of zero rather than
    raising). Beside ``UndefinedVolatilityError`` in the same house style, this module's own base class
    (``reason=insufficient_lead_in``); its message names the member, the window start, the bars wanted
    and the bars available, so a caller can widen the universe or the window without re-deriving any of
    the four facts from a stack trace."""


# The two alternative volatility estimators ruling 6 requires, each testable and selectable (AC-4).
# Nothing else is ever a valid `SizingSpec.estimator`.
VALID_ESTIMATORS = ("ewma", "atr")

# Lane DV (set 16b, `#424`, docs/units/DV/criteria.md AC-6): the plain OHLCV column labels a wide
# frame's own `close` series can carry when NO per-ticker slice has happened yet (vbt's own
# `Data.close` accessor names its column "Close", never a ticker). Compared case-insensitively so
# a real ticker that happens to be spelled "close" in some feed is still an edge this set catches
# defensively, though no real feed uses a bare OHLCV word as a ticker symbol.
_OHLCV_COLUMN_LABELS = {"open", "high", "low", "close", "volume"}


def _require_real_number(field_name: str, value: object) -> None:
    """Refuse a non-numeric (or boolean) value for a field that must be a real number (AC-6)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):  # bool IS an int subclass; reject it too
        raise InvalidSizingParameterError(
            f"{field_name} must be a real number (int or float), got {type(value).__name__} "
            f"({value!r}); reason=invalid_parameter_type"
        )


@dataclass(frozen=True)  # frozen: a spec is read fresh per call and never mutated (AC-4/s05: no shared state)
class SizingSpec:
    """One sizing configuration. Every field defaults to the derivation sheet's own ruled value
    (docs/briefs/sizing_derivation_2026-09-01.md, docs/handoffs/RULINGS_2026-09-01_sizing_review.md),
    so a caller that asks for sizing with no further detail gets the ruled rule exactly as written.
    Read FRESH on every ``compute_position_sizes`` call, with no module-level or process-level state
    feeding the computation itself (AC-4): a sweep grid can vary any field per combo with no leakage
    between combos, proven by ``research_lab/probes/sizing/s05_ac4_spec_not_global_per_call.probe.py``.

    risk_fraction: the fraction of a run's equity risked per trade (derivation 3.3). Ruled 3 percent
        (0.03), Tamer's own number, not a book's; no book prints a range for this method (derivation
        3.3, `#292`). Valid range ``(0, 1.0]``; 1.0 is itself a valid value (docs/briefs/
        sizing_unit_brief.md AC-6 / s07's own comment: "the maximum valid value").
    stop_multiple: the volatility multiple that BOTH places the stop AND sizes the position (AC-10):
        the number sizing divides by is the same number the stop sits at, by construction (read once
        per entry below, never duplicated). Ruled default 4.0 (Carver p.211's own "I recommend X = 4",
        ruling 2).
    estimator: "ewma" or "atr" (AC-4): the two alternative volatility measures ruling 6 requires.
    estimator_window: the look-back window in BARS, never a calendar span (AC-5). Ruled default 278
        (`DECISIONS.md` #304 ruling 4: Carver p.156's footnote 113 gives his EXPONENTIALLY weighted
        look-back as 36 days, not his 25-day SIMPLE one; 36 days converted to hourly bars at the
        measured 7.71 bars per trading date rounds to 278, replacing the old 25-bar simple anchor).
    hard_cap_fraction: the fraction of equity the position may use before the cap REFUSES the trade
        (derivation 3.1's own warning: calm markets, then crazy overnight, is the risk this cap
        exists to catch). Default 1.0 (the whole slice): a permissive ceiling that only bites when
        risk_fraction/stop_multiple alone would already push the position past all available capital;
        argued in docs/research_lab/parameter_provenance.md `sizing.hard_cap_fraction` rather than
        printed by either book.
    """

    risk_fraction: float = 0.03  # AC-8/provenance: sizing.risk_fraction
    stop_multiple: float = 4.0  # AC-8/provenance: sizing.stop_multiple; AC-10: the injected, not constant, multiple
    estimator: str = "atr"  # AC-8/provenance: sizing.estimator (the selector, argued default in the provenance entry)
    estimator_window: int = 278  # AC-8/provenance: sizing.estimator_window; DECISIONS #304 ruling 4 (36-day exponential anchor)
    hard_cap_fraction: float = 1.0  # AC-8/provenance: sizing.hard_cap_fraction

    def __post_init__(self) -> None:
        """Refuse a bad parameter at CONSTRUCTION time (AC-6), before any bar is ever read. Type is
        checked before range on every field, so a wrong-typed risk_fraction (e.g. a string) reports
        as invalid_parameter_type rather than being coerced and then misjudged as an out-of-range
        number (docs/briefs/sizing_unit_brief.md AC-6 / s07's own ordering)."""
        _require_real_number("risk_fraction", self.risk_fraction)  # type first, per AC-6's own order
        if not (0.0 < self.risk_fraction <= 1.0):  # 0 excluded (no risk = no trade), 1.0 included (the max valid)
            raise InvalidRiskFractionError(
                f"risk_fraction must be in the range (0, 1.0], got {self.risk_fraction!r}; "
                "reason=invalid_risk_fraction"
            )
        _require_real_number("stop_multiple", self.stop_multiple)  # a multiple of volatility, must be numeric
        if self.stop_multiple <= 0.0:  # a zero or negative multiple places no meaningful stop at all
            raise InvalidSizingParameterError(
                f"stop_multiple must be positive, got {self.stop_multiple!r}; reason=invalid_parameter_type"
            )
        if self.estimator not in VALID_ESTIMATORS:  # only the two ruling-6 alternatives are ever valid (AC-4)
            raise InvalidSizingParameterError(
                f"estimator must be one of {VALID_ESTIMATORS!r}, got {self.estimator!r}; "
                "reason=invalid_parameter_type"
            )
        if isinstance(self.estimator_window, bool) or not isinstance(self.estimator_window, int):
            raise InvalidSizingParameterError(  # the window is a bar COUNT (AC-5): must be a plain int
                f"estimator_window must be an int, got {type(self.estimator_window).__name__} "
                f"({self.estimator_window!r}); reason=invalid_parameter_type"
            )
        if self.estimator_window < 2:  # fewer than 2 bars cannot measure even one change
            raise InvalidSizingParameterError(
                f"estimator_window must be at least 2, got {self.estimator_window!r}; "
                "reason=invalid_parameter_type"
            )
        _require_real_number("hard_cap_fraction", self.hard_cap_fraction)  # type check before range, as above
        if not (0.0 < self.hard_cap_fraction <= 1.0):  # long-only, unleveraged: never more than 100% of equity
            raise InvalidSizingParameterError(
                f"hard_cap_fraction must be in the range (0, 1.0], got {self.hard_cap_fraction!r}; "
                "reason=invalid_parameter_type"
            )


def ewma_volatility(close: "pd.Series", window: int) -> "pd.Series":
    """Carver's exponentially-weighted volatility estimate (derivation 3.1, p.156): the standard
    deviation of bar-to-bar percentage returns, exponentially weighted with ``span=window`` so this
    estimator and ``atr_volatility`` share one anchored default window. Ruling 6's original text,
    "Carver's 25-bar window remains the anchored default window", is superseded by `DECISIONS.md`
    #304 ruling 4: the anchor moved from Carver's 25-day SIMPLE look-back to his 36-day EXPONENTIAL
    one (footnote 113, same page), which converts to 278 hourly bars, not needing a second,
    separately-sourced span parameter neither ruling document names.

    Returns are undefined at bar 0 (no prior bar to difference against), so the first bar contributes
    no observation; the first DEFINED estimate lands at index ``window``, one bar LATER than
    ``atr_volatility``'s own warmup, because true range (below) needs no prior close to start from.
    This one-bar difference between the two estimators is real and expected, not a defect in either
    (verified against the real EODHD stores: s06_ac5_bar_agnostic_real_daily_hourly.probe.py).
    """
    import pandas as pd  # lazy: only when an estimate is actually computed, never at module import

    returns = close.pct_change()  # bar-to-bar percentage change; index 0 is NaN (no prior bar to diff)
    return returns.ewm(span=window, min_periods=window).std()  # min_periods counts non-null RETURNS, not price bars


def ewma_price_volatility(close: "pd.Series", window: int) -> "pd.Series":
    """The ewma volatility in PRICE POINTS: the percentage reading times the same bar's close.

    ``ewma_volatility`` is a percentage (a fraction near 0.01); a stop distance and a share count need
    a price distance. Carver, Systematic Trading, ch. 13, "Volatility units" (printed p. 211, PDF page
    228): "The volatility in price points is equal to the percentage point volatility (price volatility
    as defined in chapter ten, 'Position sizing', on "Price volatility" on page 155), multiplied by the
    current price." ``atr_volatility`` is already a price distance and needs no conversion.
    """
    return ewma_volatility(close, window) * close


def atr_volatility(high: "pd.Series", low: "pd.Series", close: "pd.Series", window: int) -> "pd.Series":
    """The exponentially smoothed true range, the second of ruling 6's two alternative estimators: an
    exponentially weighted mean of the true range (the larger of the bar's own high-low span and its
    gap from the PRIOR close), reweighted by `DECISIONS.md` #304 ruling 3 to share ``ewma_volatility``'s
    own exponential speed rather than a plain trailing average. True range needs no prior bar to be
    defined at bar 0 (high-low alone is enough there), so this estimator's own first defined value
    lands one bar EARLIER than ``ewma_volatility``'s, at index ``window - 1`` rather than ``window``,
    the same real, expected one-bar difference noted there, from the other side.
    """
    import pandas as pd  # lazy: only when an estimate is actually computed, never at module import

    prev_close = close.shift(1)  # the PRIOR bar's close; NaN at bar 0 (no prior bar exists)
    high_low = high - low  # the bar's own trading range, always defined, even at bar 0
    high_prev_close = (high - prev_close).abs()  # this bar's high vs the prior close; NaN at bar 0
    low_prev_close = (low - prev_close).abs()  # this bar's low vs the prior close; NaN at bar 0
    # True range is the LARGEST of the three; DataFrame.max(axis=1) skips NaN by default, so bar 0's
    # true range is simply high-low (no prior bar is needed to define it there).
    true_range = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    # Exponentially weighted mean at the SAME span as ewma_volatility (ruling 3): min_periods still
    # gates the first defined value on `window` true-range observations, but every observation ever
    # seen keeps a fading, never-zero weight, unlike a plain trailing window that forgets a spike
    # completely exactly `window` bars later.
    return true_range.ewm(span=window, min_periods=window).mean()


# Lane P2d (Tamer 2026-10-16 ruling 2 (a), confirmed 2026-10-23): on bars shorter than a day the
# volatility that sizes a position and places its stop is measured on DAILY bars (Carver ch. 13,
# p. 211: sigma is daily price volatility), over 36 daily bars (footnote 113, "36 days for
# exponentially weighted"). Daily runs keep the spec's own window until lane P2e.
INTRADAY_ESTIMATOR_WINDOW = 36  # daily bars; docs/research_lab/parameter_provenance.md sizing.estimator_window
# Lane P2e1 (DECISIONS #476 item 2): the lead-in on intraday bars counts New York sessions. 37 = 36 + 1: the
# daily volatility reads the previous completed day, and the exponentially weighted close-to-close estimator
# (the later of the two to be defined) has its first value on daily bar 37, so an entry in session 38 is the
# first it can size, which needs 37 completed sessions before it. docs/research_lab/parameter_provenance.md
# sizing.estimator_window, "The intraday rule".
INTRADAY_LEAD_IN_SESSIONS = INTRADAY_ESTIMATOR_WINDOW + 1
_NEW_YORK = "America/New_York"  # the market's own calendar: a US session day is one New York date


def session_days(index: "pd.Index") -> "pd.Series | None":
    """The day label of every bar: the bar's New York date, as a Series aligned to ``index``.

    Returns ``None`` when no New York date holds more than one bar (a daily run): the callers then
    take the bar-count path exactly as before this lane. A tz-naive index with more than one bar on
    some calendar date is intraday data whose New York date cannot be known, so it raises
    ``ValueError("intraday bars need a timezone-aware index")``; a tz-naive index with one bar per
    date gives ``None``. The labels are handed to ``compute_position_sizes`` / ``compute_exit_lines``
    by the caller, so the volatility machinery below never reads a clock time itself (AC-5).
    """
    import pandas as pd  # lazy: only when labels are actually computed, never at module import

    if len(index) == 0:
        return None
    stamps = pd.DatetimeIndex(index)
    if stamps.tz is None:
        if stamps.normalize().has_duplicates:
            raise ValueError("intraday bars need a timezone-aware index")
        return None
    labels = pd.Series(stamps.tz_convert(_NEW_YORK).date, index=index, dtype="object")
    if not labels.duplicated().any():
        return None
    return labels


def daily_volatility(open_: "pd.Series", high: "pd.Series", low: "pd.Series", close: "pd.Series",
                     session_day: "pd.Series", spec: "SizingSpec") -> "pd.Series":
    """The volatility of the PREVIOUS completed day, on every bar, measured on daily bars.

    The bars are folded into one daily bar per label (first open, highest high, lowest low, last
    close; labels in order of first appearance), the estimator ``spec.estimator`` runs on the daily
    bars with window ``INTRADAY_ESTIMATOR_WINDOW`` (36), and each bar reads the value of the last
    label that ended before its own: the daily values are shifted one label. A bar of the first
    label, or of any label before the window is full, reads NaN, which the callers refuse as the
    warmup case. Only completed days are read, so nothing a bar's own day does can reach its own
    value (no look-ahead). Labels must be in time order; ``session_days`` gives them so.
    """
    import pandas as pd  # lazy: only when an estimate is actually computed, never at module import

    if len(session_day) != len(close):
        raise ValueError(f"session_day has {len(session_day)} labels for {len(close)} bars; one label per bar")
    labels = pd.Series(list(session_day), index=close.index, dtype="object")
    bars = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close})
    folded = bars.groupby(labels, sort=False).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    if spec.estimator == "ewma":  # SizingSpec.__post_init__ already refused any other value
        per_day = ewma_price_volatility(folded["close"], INTRADAY_ESTIMATOR_WINDOW)  # day d's reading x day d's close
    else:
        per_day = atr_volatility(folded["high"], folded["low"], folded["close"], INTRADAY_ESTIMATOR_WINDOW)
    previous_day = per_day.shift(1)  # day d reads the value measured on day d - 1, the last completed day
    return pd.Series(labels.map(previous_day).to_numpy(dtype="float64"), index=close.index)


@dataclass(frozen=True)  # frozen: a finished result is never mutated, matching RunResult's own contract
class SizingResult:
    """The output of ``compute_position_sizes``: four series, each indexed exactly like the inputs.
    ``volatility``/``stop_distance`` are the full VECTORIZED estimator output (defined wherever the
    estimator itself is warmed up, whether or not that bar is an entry); ``stop_price``/``shares`` are
    defined ONLY at entry bars and ``NaN`` everywhere else (every probe that reads them confirms this:
    e.g. s01's "every other bar carries no share count (NaN)").
    """

    volatility: "pd.Series"  # the estimator's own output, full series (AC-5's bar-agnosticism is provable here)
    stop_distance: "pd.Series"  # volatility * spec.stop_multiple, full series (AC-10: read from ONE spec field)
    stop_price: "pd.Series"  # entry_price - stop_distance, defined ONLY at entry bars
    shares: "pd.Series"  # the position in the PRICE SERIES' units (adjusted shares), defined ONLY at entry bars
    real_shares: "pd.Series"  # lane D1: the whole as-traded share count, defined ONLY at entry bars;
    # equals `shares` exactly when no factor is given (factor 1)


def compute_position_sizes(
    close: "pd.Series",  # the run's close series (also the estimator input for the EWMA path)
    high: "pd.Series",  # the run's high series (the ATR path; also unused-but-required for a uniform signature)
    low: "pd.Series",  # the run's low series (the ATR path)
    open_: "pd.Series",  # the run's open series: entries fill at the NEXT bar's open (L14, this project's own model)
    entries: "pd.Series",  # boolean entry-signal series, True only on the bar the signal closed on
    spec: SizingSpec,  # the sizing configuration for THIS call only (AC-4: never shared, never cached)
    *,
    equity: float,  # the slice's equity used to size EVERY entry in this call (see this unit's builder report
    # for why this is the run's static starting capital rather than a compounding, trade-by-trade balance:
    # compute_position_sizes takes exactly one equity value per call, by every probe's own call shape, and
    # inter-trade compounding is a caller-level policy question no probe in this suite tests either way)
    fee_rate: float = 0.0,  # the commission fraction applied when checking affordability (AC-6); 0.0 by default
    slippage_rate: float = 0.0,  # AC-9 (KA round 4, SF2): moves the affordability check's own entry price
    # the SAME way the run's real fill moves it (executed = entry_price * (1.0 + slippage_rate), the s=+1
    # buy-side convention `strategies/base/slippage.py` and `_compounding_shares` both already use). 0.0
    # (the default) leaves the price unslipped, byte-identical to every call site that predates this
    # parameter (paperdb.py included): this default is never changed by this fix, only ADDED to the signature.
    factor: "pd.Series | None" = None,  # lane D1: the per-day adjustment factor (adjusted / as-traded),
    # indexed like `close`. With it, the share count, the cap, the zero-share and affordability checks and
    # the fixed fee all use REAL shares at the price actually traded (adjusted price / factor), so a stock
    # whose old adjusted price is huge after reverse splits (HOTR: traded $1.84, adjusted $147,347) sizes
    # as the stock it was. None (the default) means factor 1, byte-identical to every caller before D1
    # (paperdb.py's paper path, Alpaca and Binance data, hourly bars, old run artifacts).
    fixed_fee_func: "Callable[[float, float], float] | None" = None,  # AC-9: the ONE shared per-fill fee
    # function (strategies.helpers.costs.ibkr_fixed_fee), CALLED on (whole_shares, executed_entry_price) and
    # added to the affordability cost -- never restated as a second formula here. None (the default) charges
    # no fixed fee in this check at all, byte-identical to every call site that predates this parameter.
    session_day: "pd.Series | None" = None,  # lane P2d: one day label per bar (``session_days``). None (the
    # default) is today's path byte for byte: the estimator runs on the bars as given, window
    # ``spec.estimator_window``. With labels, the volatility is the previous completed day's, measured on
    # daily bars folded from these (``daily_volatility``, window 36).
) -> SizingResult:
    """The ruled rule, in its ruled order (docs/briefs/sizing_derivation_2026-09-01.md section 2,
    docs/handoffs/RULINGS_2026-09-01_sizing_review.md ruling 1): for every entry, in order:

      1. measure volatility from bars that closed BEFORE the entry only (AC-3's no-look-ahead half:
         the estimator's own rolling/ewm window never reads a bar after the one it is evaluated at);
      2. turn that into a stop distance (volatility * spec.stop_multiple, read ONCE and reused for
         both the share count and the stop price: AC-10's own agreement requirement, enforced by
         construction rather than by a separate check);
      3. decide the risk budget (equity * spec.risk_fraction);
      4. divide the risk budget by the STOP DISTANCE, never the price (derivation 3.4: the joint step
         neither book prints; dividing by price answers a different question, "what if the company
         disappears overnight", and would make every position absurdly small);
      5. cap: refuse loudly if the raw share count would exceed the hard cap, rather than shrinking to
         fit it (derivation 3.1's calm-then-crazy warning is the risk the cap exists to surface);
      6. round DOWN to whole shares (derivation gap 2: forced by the execution tooling, not chosen);
      7. refuse loudly, never silently shrink, on a zero-share or unaffordable answer (AC-6).

    Every refusal is a named ``SizingError`` subclass whose message embeds ``reason=<token>`` (AC-6).

    Lane D1, real shares: with ``factor`` given, steps 4 to 7 run in as-traded terms at the fill bar
    (f = factor at the bar after the signal): real price = entry_price / f, real stop = stop distance / f,
    real shares = floor(risk budget / real stop). The returned ``shares`` is real shares / f, the position
    in the adjusted series' own units (fractional), so a portfolio built on adjusted prices books exactly
    the money the real position makes; ``real_shares`` carries the whole count. ``stop_price`` stays in
    adjusted terms, matching the series the exit lines read.
    No clock-time assumption is baked in anywhere here (AC-5): only ``Index.get_loc`` (an integer bar
    POSITION) and bar-to-bar arithmetic are used, never a timestamp's own calendar spacing.
    """
    import pandas as pd  # lazy: only when sizes are actually computed, never at module import

    # Lane DV (AC-6): both real callers (sweep_portfolio.py, portfolio.py) slice a wide OHLC
    # frame by ticker BEFORE calling in, so `close.name` already carries the real ticker symbol
    # by construction (frame[ticker].name == ticker) wherever this call is a real per-stock run.
    # A `close` series still carrying a bare OHLCV label (the single-run path's own vbt-native
    # column name) means no real ticker is known here, so the message stays silent rather than
    # printing "Close: ..." as if that were a stock. ticker_prefix is prepended to all four
    # UndefinedVolatilityError messages below, so a crashed run's own error says which stock to
    # drop (AC-6's own words), wherever a caller can tell it.
    ticker_label = (
        close.name if close.name is not None and str(close.name).lower() not in _OHLCV_COLUMN_LABELS
        else None
    )
    ticker_prefix = f"{ticker_label}: " if ticker_label is not None else ""

    if session_day is not None:  # lane P2d: bars shorter than a day, the daily volatility of the last completed day
        volatility = daily_volatility(open_, high, low, close, session_day, spec)
        warmup_window, warmup_unit = INTRADAY_ESTIMATOR_WINDOW, "daily bars"
    else:
        warmup_window, warmup_unit = spec.estimator_window, "bars"
        if spec.estimator == "ewma":  # SizingSpec.__post_init__ already refused any value outside VALID_ESTIMATORS
            volatility = ewma_price_volatility(close, spec.estimator_window)  # price points (Carver ch. 13)
        else:  # spec.estimator == "atr", the only other possibility once construction-time validation has passed
            volatility = atr_volatility(high, low, close, spec.estimator_window)

    stop_distance = volatility * spec.stop_multiple  # AC-10: read spec.stop_multiple exactly ONCE, reused below

    shares = pd.Series(float("nan"), index=close.index, dtype="float64")  # NaN everywhere until an entry fills it
    real_shares = pd.Series(float("nan"), index=close.index, dtype="float64")  # lane D1: the as-traded whole count
    stop_price = pd.Series(float("nan"), index=close.index, dtype="float64")  # NaN everywhere until an entry fills it

    n_bars = len(close)  # the series length, used below to detect the t-zero-adjacent "no next bar" edge
    risk_amount = equity * spec.risk_fraction  # AC-3 step 3: the dollar risk budget, fixed across this call
    cap_value = equity * spec.hard_cap_fraction  # the dollar ceiling step 5 (the hard cap) enforces

    for ts in entries[entries].index:  # walk every TRUE entry timestamp, in chronological (index) order
        pos = close.index.get_loc(ts)  # this entry's integer bar POSITION (never its calendar timestamp)

        vol_at_entry = volatility.iloc[pos]  # this entry's own volatility reading, from pre-entry bars only
        if pd.isna(vol_at_entry):  # not enough warmed-up history precedes this entry (AC-3/AC-7's own case)
            SIZING_MONITOR_COUNTERS["sizing_undefined_warmup"] += 1  # in-memory only; see the module docstring
            raise UndefinedVolatilityError(
                f"{ticker_prefix}volatility is undefined at entry {ts} ({spec.estimator} needs "
                f"{warmup_window} warmed-up {warmup_unit}; not enough history precedes this entry); "
                "reason=undefined_warmup"
            )

        if pos + 1 >= n_bars:  # the t-zero-adjacent edge: this IS the series' own last bar, no next bar exists
            SIZING_MONITOR_COUNTERS["sizing_undefined_warmup"] += 1  # in-memory only; see the module docstring
            raise UndefinedVolatilityError(
                f"{ticker_prefix}entry at {ts} is the series' own last bar; no next-bar open exists "
                "to fill this position on; reason=undefined_warmup"
            )

        distance_at_entry = stop_distance.iloc[pos]  # == vol_at_entry * spec.stop_multiple, read once (AC-10)
        if pd.isna(distance_at_entry) or distance_at_entry <= 0.0:  # a degenerate (flat, zero-volatility) window
            SIZING_MONITOR_COUNTERS["sizing_undefined_warmup"] += 1  # treated as undefined, not a bare crash (F1)
            raise UndefinedVolatilityError(
                f"{ticker_prefix}stop_distance is undefined or non-positive at entry {ts} "
                f"(volatility={vol_at_entry!r}, stop_multiple={spec.stop_multiple!r}); "
                "reason=undefined_warmup"
            )

        entry_price = open_.iloc[pos + 1]  # L14: signal fires at this bar's close, fills at the NEXT bar's open
        if pd.isna(entry_price):  # a missing next-bar open (e.g. a ragged, not-yet-listed sweep column)
            SIZING_MONITOR_COUNTERS["sizing_undefined_warmup"] += 1  # in-memory only; see the module docstring
            raise UndefinedVolatilityError(
                f"{ticker_prefix}entry at {ts}: the next bar's open is missing (NaN); no fill price "
                "to size against; reason=undefined_warmup"
            )

        if factor is None:
            fill_factor = 1.0  # no factor: the price series IS the as-traded price (every pre-D1 caller)
        else:
            fill_factor = factor.iloc[pos + 1]  # lane D1: the fill bar's own factor, the day the shares are bought
            if pd.isna(fill_factor) or fill_factor <= 0.0:
                raise UndefinedFactorError(
                    f"{ticker_prefix}entry at {ts}: the adjustment factor at the fill bar is {fill_factor!r}; "
                    "the as-traded price is unknown; reason=undefined_factor"
                )
            fill_factor = float(fill_factor)
        real_entry_price = entry_price / fill_factor  # lane D1: the price actually traded (== entry_price at factor 1)
        real_distance = distance_at_entry / fill_factor  # the same stop, in as-traded dollars per real share

        raw_shares = risk_amount / real_distance  # AC-3 step 4: risk budget divided by the STOP, not price
        max_shares_by_cap = cap_value / real_entry_price  # AC-3 step 5: the hard cap, expressed as a share count
        if raw_shares > max_shares_by_cap:  # strictly over: exactly-at-the-cap is valid, matching s07's own boundary
            SIZING_MONITOR_COUNTERS["sizing_cap_limited_entries"] += 1  # in-memory only; see the module docstring
            raise CapLimitedSizeError(
                f"{ticker_prefix}entry at {ts}: {raw_shares!r} raw shares exceeds the hard cap of {max_shares_by_cap!r} "
                f"shares (hard_cap_fraction={spec.hard_cap_fraction!r} of equity={equity!r}); refusing "
                "rather than shrinking to the cap; reason=cap_limited"
            )

        whole_shares = math.floor(raw_shares)  # AC-3 step 6: round DOWN to whole shares (derivation gap 2)
        if whole_shares <= 0:  # rounds down to nothing: refuse rather than size a zero (no-op) position
            raise ZeroShareSizeError(
                f"{ticker_prefix}entry at {ts}: risk_amount={risk_amount!r} / stop_distance={real_distance!r} = "
                f"{raw_shares!r} raw shares, rounding down to 0; refusing rather than sizing a zero "
                "position; reason=zero_shares"
            )

        # AC-9 (KA round 4, SF2): the affordability check reads the EXECUTED entry price (the same
        # slippage-moved price the run's own fill uses), never the raw next-open alone, so a trade that
        # is genuinely unaffordable once slippage and the real fee are counted is refused HERE, before
        # compute_position_sizes ever approves it -- not silently caught only by the sizing walk's own,
        # separate equity accounting after the fact. slippage_rate=0.0/fixed_fee_func=None (both
        # defaults) reduce this to exactly today's formula, unchanged for every caller that predates
        # this parameter pair (paperdb.py included).
        executed_entry_price = real_entry_price * (1.0 + slippage_rate)  # s=+1 buy-side convention (slippage.py); as traded (D1)
        cost = whole_shares * executed_entry_price * (1.0 + fee_rate)  # AC-6: affordability on the FINAL share count
        if fixed_fee_func is not None:  # AC-9: the one shared fee function, CALLED, never restated
            cost += fixed_fee_func(whole_shares, executed_entry_price)
        if cost > equity:  # strictly over: an exact-equity cost (fee_rate=0.0, no fixed fee) is affordable
            SIZING_MONITOR_COUNTERS["sizing_affordability_resizes"] += 1  # in-memory only; see the module docstring
            raise UnaffordableSizeError(
                f"{ticker_prefix}entry at {ts}: {whole_shares} shares at {executed_entry_price!r} (raw {real_entry_price!r}) "
                f"with fee_rate={fee_rate!r} costs {cost!r}, more than the available equity {equity!r}; "
                "reason=unaffordable"
            )

        shares.iloc[pos] = float(whole_shares) / fill_factor  # lane D1: in the price series' units (exact at factor 1)
        real_shares.iloc[pos] = float(whole_shares)  # this entry's final, validated as-traded share count
        stop_price.iloc[pos] = entry_price - distance_at_entry  # AC-10: the SAME distance that sized the position

    return SizingResult(volatility=volatility, stop_distance=stop_distance, stop_price=stop_price, shares=shares,
                        real_shares=real_shares)


# ============================================================
# Lane SW (5.8), the sizing warmup lead-in (DECISIONS #383 decision 2). A test window's own indicators
# and volatility estimator need bars BEFORE window_start_ts to warm up on -- the old [window_start,
# window_end]-only clip fed the estimator nothing but in-window bars, so a window whose first crossing
# sits fewer than estimator_window bars after window_start_ts always raised UndefinedVolatilityError,
# empirically documented for the real AAPL 2020-10-12..2026-06-19 window by
# research_lab/probes/sizing/d03_ac9_real_artifact_daily_hourly_execution.probe.py's own docstring.
# These three functions live here (never in a docker-runner entrypoint) so the vbt-free / pandas-lazy
# property this whole module's docstring states stays true of the fix too: no new vbt import anywhere.
# ============================================================


def clip_frame_with_lead_in(frame: "pd.DataFrame", window_start_ts: "int | None",
                            window_end_ts: "int | None", estimator_window: int,
                            member: str, *, lead_in_bars: "int | None" = None
                            ) -> "tuple[pd.DataFrame, pd.Timestamp | None]":
    """Clip ``frame`` to ``[window_start_ts - lead-in, window_end_ts]``, all or nothing (condition 1):
    either the returned frame starts the full lead-in before ``window_start_ts``, or this raises
    ``InsufficientLeadInError`` -- never a shorter lead-in, never a fallback to the old window-only
    clip. ``window_end_ts`` stays exactly as permissive as the old clip (an inclusive upper bound, or
    open when ``None``); only the LOWER bound changes shape.

    The lead-in is, in this order of precedence:
      - ``lead_in_bars`` when given: exactly that many bars before the window's first bar, on any bar
        size (the replay path: a run replays at the lead-in it recorded, lane P2e1);
      - on an intraday frame (``session_days(frame.index)`` is not ``None``): the first bar of the New
        York session ``INTRADAY_LEAD_IN_SESSIONS`` (37) sessions before the session holding the
        window's first bar, so that session's own bars before the window count as lead-in too;
        ``estimator_window`` is NOT read on an intraday frame (lane P2e1);
      - otherwise (a daily frame): exactly ``estimator_window`` bars, byte for byte as before.

    ``window_start_ts is None`` has no anchor to lead into (a lead-in is defined relative to
    ``window_start_ts`` alone), so this preserves the pre-lane clip exactly on that side -- an open
    lower bound, ``window_end_ts`` still honoured if set -- and returns ``entries_not_before=None``:
    nothing was extended backward, so nothing needs excluding forward either (AC-SW6's own "no window at
    all" case, matching the pre-lane "full history" behaviour byte for byte).

    Returns ``(clipped_frame, entries_not_before)``: ``entries_not_before`` is ``window_start_ts`` itself,
    as a tz-aware UTC ``pd.Timestamp`` (matching ``frame``'s own index dtype, so a caller never rebuilds
    it), meant to be passed straight to ``mask_entries_before`` so no entry is ever taken on a lead-in
    bar; it is ``None`` exactly when ``window_start_ts`` is ``None``.
    """
    import pandas as pd  # lazy, matching every other function in this module

    if window_start_ts is None:  # no anchor to lead into: the pre-lane clip, unchanged, on this side
        hi = None if window_end_ts is None else pd.Timestamp(window_end_ts, unit="s", tz="UTC")
        return frame.loc[:hi], None

    start_boundary = pd.Timestamp(window_start_ts, unit="s", tz="UTC")  # the window's own first instant
    # side="left": the first bar POSITION at or after start_boundary, i.e. the window's own first bar.
    window_start_pos = frame.index.searchsorted(start_boundary, side="left")
    labels = None if lead_in_bars is not None else session_days(frame.index)
    if labels is None:  # a bar count: the caller's exact count, or a daily frame's estimator_window
        wanted = estimator_window if lead_in_bars is None else lead_in_bars
        lead_in_start_pos = window_start_pos - wanted  # exactly that many bars earlier
        if lead_in_start_pos < 0:  # fewer than that many bars precede window_start_ts in this series
            raise InsufficientLeadInError(
                f"{member}: window_start_ts={window_start_ts} wants {wanted} lead-in bars but "
                f"only {window_start_pos} precede it in this series; reason=insufficient_lead_in"
            )
    else:  # an intraday frame: count New York sessions, the first bar of each marks where it starts
        import numpy as np  # lazy, like pandas above

        session_first_pos = np.flatnonzero((labels != labels.shift()).to_numpy())  # frame labels are in time order
        if window_start_pos >= len(frame):  # the window lies past the data: every session precedes it
            reference = len(session_first_pos)
        else:  # the session holding the window's first bar (a mid-session window start lands here)
            reference = int(np.searchsorted(session_first_pos, window_start_pos, side="right")) - 1
        if reference < INTRADAY_LEAD_IN_SESSIONS:  # sessions strictly before the reference session
            raise InsufficientLeadInError(
                f"{member}: window_start_ts={window_start_ts} wants {INTRADAY_LEAD_IN_SESSIONS} lead-in "
                f"New York sessions but only {reference} precede it in this series; "
                "reason=insufficient_lead_in"
            )
        lead_in_start_pos = int(session_first_pos[reference - INTRADAY_LEAD_IN_SESSIONS])

    if window_end_ts is None:  # open upper bound: run to the frame's own end (AC-SW6's own scenario)
        clipped = frame.iloc[lead_in_start_pos:]
    else:
        end_boundary = pd.Timestamp(window_end_ts, unit="s", tz="UTC")
        # side="right": one PAST the last bar at or before end_boundary, so .iloc[:window_end_pos]
        # includes that bar -- an INCLUSIVE upper bound, matching the old _clip_frame_to_window's own
        # .loc[lo:hi] label-slice convention (research_lab/docker/research-runner/run_sweep_entrypoint.py).
        window_end_pos = frame.index.searchsorted(end_boundary, side="right")
        clipped = frame.iloc[lead_in_start_pos:window_end_pos]

    return clipped, start_boundary


def mask_entries_before(entries: "pd.Series | pd.DataFrame",
                        entries_not_before: "pd.Timestamp | None") -> "pd.Series | pd.DataFrame":
    """Mask out every ``True`` entry signal strictly before ``entries_not_before`` (condition 1: "a
    lead-in is not a draw" -- a lead-in bar is loaded so an estimator can warm up on it, never so a
    trade can be taken on it). Masking ENTRIES rather than the frame's own bars is what keeps the
    estimator's warmup input intact while still refusing a trade on it.

    ``entries`` is a boolean ``Series`` (the single-run path) or a boolean ``DataFrame``, one column per
    (combo, ticker) (the wide sweep path); both are handled identically, because wrapping the boundary
    condition in a ``Series`` of the SAME index makes pandas align it row-wise against either shape
    (verified empirically: a bare boolean array does not broadcast against a DataFrame and raises
    ``ValueError: Array conditional must be same shape as self``, which is why this is not simply
    ``entries.index >= entries_not_before``). ``entries_not_before`` is the exact ``pd.Timestamp``
    ``clip_frame_with_lead_in`` returns, or ``None`` (no boundary at all: every entry passes through
    unchanged, the pre-lane behaviour). Returns a NEW object; ``entries`` itself is never mutated
    (AC-4/s05's own no-shared-state precedent this module already keeps).
    """
    if entries_not_before is None:  # no window at all: nothing to mask, pass through unchanged
        return entries
    import pandas as pd  # lazy, matching every other function in this module

    # An index-aligned Series condition, not a bare array: DataFrame.where broadcasts a Series condition
    # across every column row-wise, but requires an exact-shape match for a plain numpy/Index array.
    cond = pd.Series(entries.index >= entries_not_before, index=entries.index)
    return entries.where(cond, other=False)  # any True strictly before the boundary becomes False


def traded_bar_count(index: "pd.Index", entries_not_before: "pd.Timestamp | None") -> int:
    """The number of bars in ``index`` at or after ``entries_not_before`` (AC-SW6): the traded-window
    bar count alone, EXCLUDING every lead-in bar, so ``research_run_funnel.bars_total`` (the whole
    clipped frame's length) minus this equals ``estimator_window`` exactly on a lead-in run.

    ``entries_not_before`` is ``None`` exactly when ``clip_frame_with_lead_in`` was given no
    ``window_start_ts`` (the pre-lane "full history" case): every bar counts as traded, matching the
    funnel's own pre-lane ``bars_total`` meaning exactly (AC-SW6's "no window at all" scenario). This
    function replaces the bare ``len(close.index) < 2`` / ``len(ohlc.index) < 2`` empty-window checks at
    every call site a lead-in can now reach: once the frame is lead-in-extended, ``len(index) >= 2`` can
    be satisfied by lead-in bars alone even when the TRADED window itself is empty, so those checks must
    read this count instead of the frame's own raw length (this lane's own builder report names every
    call site this replaces).
    """
    if entries_not_before is None:  # no window at all: every bar counts as traded (pre-lane behaviour)
        return len(index)
    pos = index.searchsorted(entries_not_before, side="left")  # the first traded bar's own position
    return len(index) - pos  # everything from that position onward is traded; everything before is lead-in


# ============================================================
# Lane LB (5.8e, the last-bar fill reaches one bar, for the fill only). DECISIONS.md #406 ruling 2
# overrules the lead's own lean (mask a last-bar entry the same way a lead-in entry is masked): a
# signal on a windowed frame's own last bar really would have been filled on the next real bar, and
# that bar exists in the store, so the frame reaches ONE bar past clip_frame_with_lead_in's own upper
# bound to price that one fill, never to feed an indicator, a signal, a statistic or a funnel count
# (guarantee (i), proved in portfolio.py by running the real pipeline twice, once with this function
# patched to always return None). clip_frame_with_lead_in's own contract is untouched by this lane
# (criterion 1): nothing here edits that function or reads past the reach's own two fields.
# ============================================================


@dataclass(frozen=True)  # frozen, two fields only (criterion 1): a static probe pins the field count so
# no later change can smuggle a close, high, low or volume figure onto the one bar this reach may read
class FillReach:
    """The one bar clip_frame_with_lead_in's own clip cannot see, reached ONLY to price a fill on a
    last-bar entry. ``ts`` is that bar's own timestamp (a tz-aware UTC ``pd.Timestamp``, matching every
    other boundary this module hands a caller); ``open`` is that bar's own opening price, the exact
    figure a next-bar-open fill (L14, this project's own execution model) would have used. Nothing
    else about that bar is ever read: the dataclass holds exactly these two fields, so a caller cannot
    accidentally pull a close, high, low or volume off a bar the granted window never covered."""
    ts: "pd.Timestamp"
    open: float


def fill_reach_after(source: "pd.DataFrame | pd.Series", clipped_frame: "pd.DataFrame | pd.Series",
                      reach_before_ts: "int | None") -> "FillReach | None":
    """The first bar of ``source`` (the UNCLIPPED series a clip was drawn from) strictly after
    ``clipped_frame``'s own last bar, returned as a ``FillReach`` only when that next bar's timestamp is
    STRICTLY BEFORE ``reach_before_ts`` (the granted window's own end, in the same unix-seconds shape
    ``clip_frame_with_lead_in``'s own ``window_end_ts`` takes); ``None`` in every other case.

    ``reach_before_ts`` alone is what makes guarantees (ii) and (iii) of ruling 2 provable rather than
    asserted (`DECISIONS.md` #406): the tracker's own ``request_draw`` already refuses any non-reserve
    draw whose window ends at or after the reserve cut (`tracker.py`'s ``reserve_cut_crossed``, `#318`),
    so a bar strictly before the granted window's own end can never be reserve data, and a bar this
    function refuses to reach for (at or past that same end) is undrawn data the chronology gate has
    not granted -- the SAME strict inequality closes both guarantees at once.

    Returns ``None`` when: ``clipped_frame`` is empty (nothing to reach past); ``clipped_frame``'s own
    last bar IS ``source``'s own last bar (no next bar exists in the data at all, the sheet's item 2(i)
    "no admissible bar" case); ``reach_before_ts`` is ``None`` (an unwindowed frame: only a walk-forward
    slice ever passes a real boundary here, criterion 3); the next bar's own timestamp is at or after
    ``reach_before_ts``; or the next bar's own open is undefined (``NaN``, nothing tradeable to price a
    fill against). ``source`` may be a DataFrame carrying a title-case ``"Open"`` column (the walk-forward
    entrypoint's own OHLC frame shape) or a plain Series of opens; both are handled identically.
    """
    import pandas as pd  # lazy, matching every other function in this module

    if reach_before_ts is None or len(clipped_frame.index) == 0:  # no boundary, or nothing was clipped at all
        return None
    open_series = source["Open"] if hasattr(source, "columns") else source  # a DataFrame or a bare Series
    last_ts = clipped_frame.index[-1]  # the clipped frame's own last bar, the one sizing.py's own last-bar
    # refusal (compute_position_sizes's "pos + 1 >= n_bars" check) fires on when it carries a TRUE entry
    source_pos = open_series.index.get_loc(last_ts)  # this bar's own position in the UNCLIPPED source series
    next_pos = source_pos + 1
    if next_pos >= len(open_series.index):  # the clipped frame's last bar IS the source's own last bar
        return None
    next_ts = open_series.index[next_pos]
    boundary = pd.Timestamp(int(reach_before_ts), unit="s", tz="UTC")
    if open_series.index.tz is None:  # a tz-naive source index: compare against the naive UTC wall time
        boundary = boundary.tz_localize(None)
    if next_ts >= boundary:  # at or past the granted window's own end: undrawn data, guarantees (ii)/(iii)
        return None
    next_open = open_series.iloc[next_pos]
    if pd.isna(next_open):  # nothing tradeable to price this fill against (a mid-history gap, a halt)
        return None
    return FillReach(ts=next_ts, open=float(next_open))
