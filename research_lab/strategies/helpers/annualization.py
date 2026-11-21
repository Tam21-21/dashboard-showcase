"""Index-derived Sharpe annualization for the price path (Phase 2 Unit 1, A2b-001/A2b-002).

The single home for resolving how a price-path run's Sharpe is annualized. The audit (A2b-001)
found the price build passed no ``freq``/``year_freq`` to ``vbt.Portfolio.from_signals``, so vbt
annualized with factor 24x365 = 8760 (the 24/7 crypto assumption) on gappy equity data, inflating
every stored intraday-equity Sharpe. The event path is correct because it sets both explicitly
(event_pipeline.py freq="1d"/year_freq="252 days" on its daily grid).

The fix follows AFML Ch 14 (Sharpe annualization, afml_SKILL.md:714): annualize by sqrt(a), where a
is the average number of return observations per year. ``a`` is read from the observed price index
itself, not from a flat calendar constant: the index encodes the bar size, the gaps (overnight,
weekend, halts), and the span, so the same function is correct for daily and intraday, equities and
crypto, with no asset-class input. On real daily equity this yields ~252 (matching the trading-day
convention); on continuous crypto ~365 (daily) or ~8760 (hourly); on gappy intraday equity the true
gap-aware count. This also folds A2b-002: the bar period is derived from the observed gaps between
bars (the intra-session spacing), so ``pd.infer_freq`` (which returns None on a halt-gapped index) is
never used.

**Job B bar-period fix (hourly-pivot campaign, 2026-08-25).** A2b-002's original rule took the
SMALLEST positive gap as the bar period. On the real AAPL hourly store
(``/home/tamer/eodhd/1h/AAPL.US.parquet``, 11,013 rows, measured 2026-08-24) that rule returns 8
minutes 10 seconds, because the file carries two off-grid bars (one at 10:06 New York, one at 13:21)
among an overwhelming one-hour mode, and the smallest gap is always the most fragile statistic in a
series like this: a single stray bar decides it. The rule below replaces "smallest gap" with "the
gap value that accounts for a strict majority of all observed gaps", which is robust to a handful of
off-grid bars by construction. Source, mechanism, sensitivity and bounds (this parameter has no book
page, so it carries this argument instead, per the job brief):

- Source: measured 2026-08-24 against the real stores. Hourly (AAPL, full file): of 11,012
  consecutive gaps, 8,506 (77.24 percent) are exactly one hour; the rest are session-boundary jumps
  (the 30-minute early-close gap, the overnight 17:30/18:00 gaps, weekend multi-day spans) and the two
  off-grid gaps contribute two of the 23 distinct gap values seen, each with a single-digit count.
  Daily (AAPL, 2020-10-12 onward): of 1,427 gaps, 1,116 (78.21 percent) are exactly one day, the rest
  weekend or holiday jumps. In both real stores one grid value clears three-quarters of all gaps.
- Mechanism: take the most frequent (modal) positive gap value; accept it as the bar period whenever
  its share of all positive gaps is AT LEAST half, share >= 0.5. This is derived from the observed
  distribution of gaps, not a hardcoded list of known bar sizes (a lookup table of "1 minute, 5
  minutes, 1 hour, 1 day" would resolve any grain not on the list, such as a 17-minute bar, to the
  nearest listed entry instead of its true period, which is exactly the wrong answer). Findings
  dispatch F2 (2026-08-24, SHOULD-FIX) widened this from a strict majority (share > 0.5) because a
  short sliced window -- routine under DECISIONS #179's per-ragged-member own-window annualization --
  can tie exactly two-and-two, and the old strict rule raised on that tie where the smallest-gap rule
  it replaced answered correctly. Two different gap values can never each hold MORE than half of the
  same total, so a share of exactly 0.5 admits at most a TWO-way tie, resolved by taking the smaller
  of the two tied values (matching the old smallest-gap rule's answer on the same data).
- Sensitivity: the real hourly store's modal share (77.24 percent) and the real daily store's (78.21
  percent) both sit far above the 0.5 cutoff, so the resolved period would be unchanged for either
  store at any threshold from roughly 0.1 up to about 0.75. A synthetic grid with 4 off-grid gaps out
  of 50 (92 percent modal share) still resolves to its true period rather than the nearest guess; a
  fully scattered index where no gap value exceeds 10 percent still fails loud rather than choosing.
  0.5 (plain majority) sits in the middle of that wide safe band, which is why it is the round number
  chosen rather than a value tuned to just clear either store.
- Bounds: undefined for fewer than 2 bars (guarded separately, unchanged). The rule assumes the grid's
  gap repeats as the exact same ``Timedelta`` bar to bar; a store whose spacing jitters by even a
  second between bars would fragment the mode across many near-identical values and could legitimately
  raise ``no_dominant_gap`` on data that a human would still call gridded. That is the accepted cost of
  reading the grid from the data instead of a fixed lookup, and it fails loud rather than guessing
  wrong.

Pure and pandas-only (no vbt); the import stays light, mirroring ``costs.py``, so importing
``strategies.helpers`` does not drag the engine at catalog-introspection time.
"""
from __future__ import annotations  # lazy annotations: import stays light, no eager typing eval

from typing import TYPE_CHECKING  # keep the pandas import type-check-only

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.DatetimeIndex / pd.Timedelta used in annotations


class AnnualizationError(ValueError):
    """An index that cannot yield an annualization factor: too short, all one instant, or with no
    single gap value covering at least half of the observed gaps (``no_dominant_gap``, job B)."""


def resolve_annualization(index: "pd.DatetimeIndex") -> dict:
    """Resolve ``(factor, freq, year_freq)`` for a price index from the data itself (AFML Ch 14).

    Returns a dict with:
      - ``factor``: the annualization factor (float) = the observed number of bars per year,
        ``(len - 1) / span_years``. This is the comparable quantity (``sqrt(factor)`` annualizes the
        per-bar Sharpe), recorded per run so cross-run Sharpe comparison is valid by construction.
      - ``freq``: the bar period (``pd.Timedelta``) = the positive gap value that accounts for AT
        LEAST HALF of all observed bar-to-bar gaps (ties at exactly half broken toward the smaller
        value, findings F2), the actual bar size, robust to a handful of off-grid bars and to
        overnight/weekend/halt gaps (folds A2b-002, fixed by job B: see the module docstring's
        source/mechanism/sensitivity/bounds for why "at least half of gaps" replaced the old "smallest
        gap" rule).
      - ``year_freq``: ``freq * factor`` (``pd.Timedelta``). vbt computes ``ann_factor =
        year_freq / freq`` (ReturnsAccessor.get_ann_factor), so this pairing makes vbt's annualization
        exactly ``factor``.

    Fail-loud (AnnualizationError) on fewer than two bars, on an index with no positive gap at all, or
    on an index with no dominant gap (message contains ``no_dominant_gap``): none of these can be
    annualized honestly, and a silent fallback or a best guess would hide it (the audit's
    no-papering-over discipline).
    """
    import pandas as pd  # lazy: only when an annualization is actually resolved, never at module import

    if len(index) < 2:  # need at least two bars to have a gap (the bar period) and a span
        raise AnnualizationError(
            f"cannot resolve annualization from a {len(index)}-bar index; need at least 2 bars"
        )

    deltas = index.to_series().diff().dropna()           # consecutive bar-to-bar gaps (first is NaT, dropped)
    positive = deltas[deltas > pd.Timedelta(0)]          # guard against any duplicate/zero-gap timestamps
    if positive.empty:                                    # every gap was zero, a degenerate (single-instant) index
        raise AnnualizationError("cannot resolve annualization: no positive gap between bars")

    # Job B bar-period fix: the bar size is the gap value covering AT LEAST HALF of all observed
    # gaps, not the smallest gap. A single off-grid bar (or a handful) creates a couple of rare gap
    # values whose count never comes close to the top, which is exactly why this rule survives them
    # where "take the minimum" did not (see the module docstring for the measured gap distribution
    # this is derived from).
    #
    # Findings dispatch F2 (2026-08-24, SHOULD-FIX): the original rule required a STRICT majority
    # (share > 0.5) and raised on an exact 50/50 tie. DECISIONS #179 makes a short tied window
    # routine, not hypothetical: each ragged sweep member annualizes on its own sliced index
    # (sweep_portfolio.py), so a 4-gap own-window that splits two-and-two is real data, not an edge
    # case to reject. Two distinct gap values can never each hold MORE than half of the same total
    # (an arithmetic fact), so accepting a share of exactly 0.5 can never admit a three-way or worse
    # ambiguity: either one value alone reaches exactly half (nothing to tie-break, it is simply
    # chosen) or exactly two values are tied at the same top count. The tie is broken by taking the
    # SMALLER of the values sharing the top count, which matches what the pre-Job-B smallest-gap
    # rule answered on the same data, so a real tied window keeps its old, correct answer.
    gap_counts = positive.value_counts()                     # distinct gap values -> counts (any order for ties)
    max_count = gap_counts.max()                              # the largest count reached by any single gap value
    dominant_share = max_count / len(positive)                # the top count's share of every observed gap
    if dominant_share < 0.5:                                  # not even a plain majority: no defensible grid
        raise AnnualizationError(
            "cannot resolve annualization: no_dominant_gap "
            f"(largest single gap value covers {dominant_share:.1%} of {len(positive)} observed "
            "gaps; need at least half to call it the grid)"
        )
    # Every gap value that reached the top count (usually exactly one; exactly two only on a genuine
    # 0.5 tie, per the arithmetic fact above). `.min()` on that set is a no-op when there is only one
    # value, so this line changes nothing for every case this fix does not target (b1/b2/b3/b6).
    tied_at_max = gap_counts[gap_counts == max_count].index
    bar_period = tied_at_max.min()                            # the bar size = the (smallest, if tied) top value

    span_years = (index[-1] - index[0]) / pd.Timedelta(days=365.25)  # float ratio; avoids int64-ns overflow
    factor = (len(index) - 1) / span_years               # AFML: observed return-observations per year
    year_freq = bar_period * factor                      # a Timedelta whose ratio to bar_period is `factor`

    return {"factor": float(factor), "freq": bar_period, "year_freq": year_freq}
