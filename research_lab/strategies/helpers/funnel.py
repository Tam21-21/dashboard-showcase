"""The production signal funnel for the price path (Phase 2 Unit 3, audit concern #1).

The single home for turning a finished price-path portfolio plus its raw entry/exit signals into
the per-run signal funnel: bars -> bars after warmup -> raw entry signals -> entries kept (after the
in-position / same-bar-conflict drop) -> raw exit signals -> fills -> trades -> exit-reason split,
and, for a zero-trade run, the WHY-ZERO classification.

This productionizes ``research_lab/probes/signal_funnel_diagnostic.py`` (the audit's A2c deliverable).
The prototype RE-SIMULATES dedup/fills because it reconstructs a wiped run from on-disk artifacts and
never has the live portfolio; here we have the finished ``pf``, so the post-dedup counts come from
``pf.orders`` / ``pf.trades`` (the exact records vbt produced) — exact by construction, no recovery of
strategy parameters. Only the pre-dedup raw-signal counts come from the boolean Series the strategy
computed before ``from_signals``.

The audit property this preserves (signal_funnel_diagnostic.py:16-18): a valid-zero (the threshold was
just too tight) and a no-data zero look DIFFERENT through the funnel. ``classify_zero_reason`` is that
distinction, transcribed verbatim from the prototype (signal_funnel_diagnostic.py:191-204).

Pure and pandas-only at module load (no vbt import here, mirroring ``annualization.py`` / ``costs.py``);
``compute_signal_funnel`` receives the already-built ``pf`` from the caller, which owns the vbt import.

Trade-count semantics (verified via the VBT Pro MCP, pinned build b1727ac6, 2026-06-19): ``pf.trades``
records ALL trades, an end-of-data open position included as one trade with status Open. So
``total_trades = pf.trades.count()`` equals the stats ``num_trades`` and includes the EOD-open position;
the invariant is ``total_trades == exits_by_signal + exits_by_eod`` (closed-on-signal + still-open).
"""
from __future__ import annotations  # lazy annotations: import stays light, no eager typing eval

from typing import TYPE_CHECKING  # keep the pandas import type-check-only

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.Series referenced in the annotations below


# The canonical funnel count keys, in funnel order. These are the column names of research_run_funnel
# (minus zero_reason, which is derived) and the keys of meta["funnel"]; compute_signal_funnel returns
# exactly these. Kept here as the single source the entrypoint + the probe agree on.
# Cross-deployable contract (research_lab here vs the dashboard's researchdb._RUN_FUNNEL_COLUMNS): the
# two halves cannot import each other (separate deployables), but drift is caught fail-loud, not silent —
# the entrypoint builds meta["funnel"] = these keys + zero_reason + lead_in_bars (P2g: the lead-in bars the
# run's clip used, added by the entrypoints, never by compute_signal_funnel), and complete_run's partial-dict
# guard raises ValueError on the FIRST run if that set != _RUN_FUNNEL_COLUMNS (or that set minus lead_in_bars,
# an image from before P2g). So `_RUN_FUNNEL_COLUMNS` must equal
# `FUNNEL_COUNT_KEYS + ("zero_reason", "lead_in_bars")`; the guard is the machine-check.
FUNNEL_COUNT_KEYS = (
    "bars_total",            # bars in the price series
    "bars_after_warmup",     # bars where every warmup indicator is non-NaN
    "raw_entries",           # True count in the raw entry Series (before the in-position / conflict drop)
    "entries_after_dedup",   # buy orders vbt actually placed (positions opened) = pf.orders.side_buy
    "raw_exits",             # True count in the raw exit Series
    "fills",                 # filled orders (buys + sells) = pf.orders.count()
    "total_trades",          # every trade vbt recorded, EOD-open included = pf.trades.count() = num_trades
    "exits_by_signal",       # trades that closed on an exit signal = pf.trades.status_closed
    "exits_by_eod",          # trades still open at end of data = pf.trades.status_open
)

# The six WHY-ZERO buckets (signal_funnel_diagnostic.py:53-60). "N/A" carries when there are trades.
ZERO_REASONS = (
    "NO-DATA",
    "ALL-NaN-WARMUP",
    "THRESHOLD-NEVER-CROSSED",
    "SIGNALS-BUT-NO-FILLS",
    "ENTRIES-DEDUPED-AWAY",
    "N/A",
)


def compute_signal_funnel(pf, entries: "pd.Series", exits: "pd.Series", indicators: dict) -> dict:
    """Build the numeric signal-funnel counts for one finished price-path run.

    Parameters
    ----------
    pf          : the finished ``vbt.Portfolio`` (the caller built it and owns the vbt import).
    entries     : the raw boolean entry-signal Series (before the in-position / same-bar-conflict drop).
    exits       : the raw boolean exit-signal Series.
    indicators  : the strategy's overlay-series dict (the warmup-defining indicators); ``{}`` for an
                  indicator-less rule, in which case there is no warmup region.

    Returns a flat dict keyed by ``FUNNEL_COUNT_KEYS`` (all Python ``int``). The categorical
    ``zero_reason`` is NOT computed here — it is derived from these counts by ``classify_zero_reason``
    at the point the record is assembled (the entrypoint), so it can also be tested independently.
    """
    import pandas as pd  # lazy: only when a funnel is actually computed, never at module import

    # bars_total: the run's bar count, read off the portfolio's own index (the authoritative length).
    bars_total = int(len(pf.wrapper.index))

    # bars_after_warmup: bars where EVERY warmup indicator is non-NaN. No indicators -> no warmup, so all
    # bars are post-warmup (mirrors signal_funnel_diagnostic.py:128-132). A raw-price rule has no warmup.
    if indicators:
        any_nan = pd.concat(list(indicators.values()), axis=1).isna().any(axis=1)  # True where any indicator is NaN
        bars_after_warmup = int((~any_nan).sum())  # count of fully-valid bars
    else:
        bars_after_warmup = bars_total  # no indicator -> no warmup region

    # Raw signal counts: the True count in each boolean Series BEFORE vbt's dedup/conflict handling.
    raw_entries = int(entries.sum())
    raw_exits = int(exits.sum())

    # Post-dedup counts from the AUTHORITATIVE order/trade records vbt produced (not a re-simulation):
    #  - fills          : every filled order (buys + sells).
    #  - entries_after_dedup : buy orders = positions actually opened (side==0; verified vbt API).
    #  - total_trades   : all trades incl. an EOD-open position (pf.trades.count() == num_trades).
    #  - exits_by_signal/exits_by_eod : closed-on-signal vs still-open at end of data.
    fills = int(pf.orders.count())
    entries_after_dedup = int(pf.orders.side_buy.count())  # side==0 (buy); the kept entries
    total_trades = int(pf.trades.count())
    exits_by_signal = int(pf.trades.status_closed.count())  # closed trades = exited on a signal
    exits_by_eod = int(pf.trades.status_open.count())       # open at end of data = EOD-marked

    return {
        "bars_total": bars_total,
        "bars_after_warmup": bars_after_warmup,
        "raw_entries": raw_entries,
        "entries_after_dedup": entries_after_dedup,
        "raw_exits": raw_exits,
        "fills": fills,
        "total_trades": total_trades,
        "exits_by_signal": exits_by_signal,
        "exits_by_eod": exits_by_eod,
    }


def classify_zero_reason(counts: dict) -> str:
    """Classify WHY a run produced its trade count into one of ``ZERO_REASONS``.

    A pure function of the numeric ``counts`` (the ``compute_signal_funnel`` dict). The bucket order is
    the prototype's (signal_funnel_diagnostic.py:191-204), with the N/A gate updated to use
    ``total_trades`` (which includes an EOD-open position, per the §2 trade-count semantics) where the
    prototype keyed on its ``closed_trades`` field: a run WITH trades is ``N/A``; otherwise the first
    matching cause wins, in order — empty series (NO-DATA), all bars in warmup (ALL-NaN-WARMUP), the
    threshold never fired (THRESHOLD-NEVER-CROSSED, the valid-zero), raw entries all suppressed
    (ENTRIES-DEDUPED-AWAY), else entries survived but nothing filled (SIGNALS-BUT-NO-FILLS).

    The ordering is the whole point of the audit: THRESHOLD-NEVER-CROSSED (data present, signal silent)
    and NO-DATA (no series at all) land in different buckets, so a valid-zero never reads as a failure.
    """
    if counts["total_trades"] > 0:
        return "N/A"
    if counts["bars_total"] == 0:
        return "NO-DATA"
    if counts["bars_after_warmup"] == 0:
        return "ALL-NaN-WARMUP"
    if counts["raw_entries"] == 0:
        return "THRESHOLD-NEVER-CROSSED"  # the valid-zero: bars present, threshold too tight
    if counts["entries_after_dedup"] == 0:
        return "ENTRIES-DEDUPED-AWAY"  # raw entries existed but none opened a position
    return "SIGNALS-BUT-NO-FILLS"  # entries opened nothing that closed (defensive; rare on the long-only path)
