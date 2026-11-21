"""Shared, strategy-author-facing helpers (M4 R15).

The home for genuinely reusable patterns a strategy author reaches for, kept distinct from
``strategies.base`` (the locked ``run``/settings/``RunResult`` CONTRACT every strategy must satisfy).
This package is deliberately MINIMAL: shared logic lives here only as real duplication appears, never as
speculative scaffolding. The M4 neutral-cost read (``default_costs``) was the first — the one pattern that
repeats across the price and event cost legs. The M4.5 R16 long-only portfolio build
(``long_only_signal_result``) is the second: the five price templates all need ma_cross's ``from_signals``
tail, so it is extracted here as their single vbt home (R-TMPL3 — a templated price strategy hands its
pandas signals to this helper and never writes a vbt call itself). The catalog (R13) introspects whatever
this package exports.

Baked into ``research-runner`` alongside ``strategies.base`` (the narrowed Dockerfile COPY, R1/R15) so a
mounted strategy can ``from strategies.helpers import ...`` at run time.
"""
from strategies.helpers.costs import default_costs  # the M4 neutral-key cost read (D2)
from strategies.helpers.annualization import resolve_annualization  # Phase-2 Unit 1: index-derived Sharpe annualization
from strategies.helpers.funnel import compute_signal_funnel, classify_zero_reason  # Phase-2 Unit 3: the production signal funnel
from strategies.helpers.portfolio import long_only_signal_result  # M4.5 R16: the shared price-template build
from strategies.helpers.sweep_portfolio import sweep_portfolio_cells  # M5 1B: the shared WIDE sweep-batch build

__all__ = [
    "default_costs",
    "resolve_annualization",
    "compute_signal_funnel",
    "classify_zero_reason",
    "long_only_signal_result",
    "sweep_portfolio_cells",  # M5 1B: one wide from_signals over a sweep batch -> per-column cells
]
