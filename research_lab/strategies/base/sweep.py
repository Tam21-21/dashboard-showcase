"""The sweep contract: how a strategy exposes its sweepable parameters and wide signals (M5 1B §4).

The additive, opt-in companion to the locked ``Strategy.run`` contract. A single run is
``run(data, settings) -> RunResult`` (one ticker, one parameter set); a *sweep* runs the same rule
across a whole universe (columns = tickers) and a whole parameter grid (columns = combos) in ONE wide
``vbt.Portfolio.from_signals`` pass (R-PROD), so the engine needs the rule expressed as a function of
``vbt.Param`` parameters over a wide universe close. This module holds the engine-neutral pieces of that
contract:

  - ``SweepParam`` — one sweepable parameter's declaration (name + type), so the engine can coerce grid
    values and 1C can build/freeze the grid.
  - ``SweepInput`` — the wide-frame input (close/open/high/low, columns = tickers), the sweep analogue of
    ``StrategyInput``.
  - ``SweepNotSupported`` — raised when a sweep is dispatched against a strategy that has not implemented
    the contract, so the engine refuses it with a clear error (never a silent single-run fallback, §4).
  - ``canonical_combo_order`` / ``canonical_param_json`` — the ONE deterministic combo sort and the ONE
    canonical per-combo JSON. ``variation_id`` is a combo's index in this sort (§6.4), and the backend
    hashes ``canonical_param_json`` into ``param_config_hash``. Pure functions so the container engine
    (here) and 1C's dispatch (the backend, a separate deployable) can produce IDENTICAL combo ids and
    json — the same cross-deployable contract discipline ``funnel.FUNNEL_COUNT_KEYS`` keeps. 1C MUST use
    these helpers when it freezes the registered grid so the registered ``variation_id`` set matches what
    the engine writes.

Pure Python (stdlib only): importing this module pulls in neither pandas nor vbt, so ``strategies.base``
stays import-light at catalog-introspection time (the same rule the rest of ``base/`` keeps).
"""
from __future__ import annotations  # lazy annotations: import stays light, no eager typing eval

import json  # the canonical param JSON the backend hashes into param_config_hash
from dataclasses import dataclass, field  # frozen contract containers
from typing import TYPE_CHECKING  # keep the pandas import type-check-only

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    import pandas as pd  # pd.DataFrame referenced in the SweepInput annotations


class SweepNotSupported(NotImplementedError):
    """Raised when a sweep is dispatched against a strategy that has not implemented the sweep contract.

    A ``NotImplementedError`` subclass so the engine's refusal reads as the clear "this strategy is not
    sweepable" signal (§4 — never a silent fall-through to a single run). A strategy is sweepable iff it
    declares non-empty ``sweep_params`` AND overrides ``sweep_signals``; the base ``sweep_signals`` raises
    this so a half-declared strategy fails loud rather than silently producing nothing."""


class EmptyWindowedUniverse(ValueError):
    """EVERY universe member has fewer than two bars inside the test window (M5 AD-UU-3, B4 P-3).

    A ``ValueError`` subclass with a SPECIFIC type so the sweep entrypoint can raise it BEFORE
    ``resolve_annualization`` would raise the generic ``AnnualizationError`` on the clipped index (SF-A):
    an empty windowed universe is a user error (the window excludes all data) worth a clear message, never
    a silent zero-cell success and never the confusing generic annualization error. A window where at
    least one member has >=2 in-window bars does NOT raise — per-member empties surface honestly (logged,
    excluded from the grid), the no-silent-subset contract."""


@dataclass(frozen=True)  # one sweepable parameter's declaration; immutable
class SweepParam:
    """A strategy's declaration of one sweepable parameter (§4).

    ``name`` is the parameter name as the strategy's ``sweep_signals`` inner function names it (so the wide
    result's column-index level carries the same name — the engine reads the combo off those levels).
    ``kind`` is ``'int'`` or ``'float'``, the type the engine coerces each grid value to before building
    the ``vbt.Param`` set (a JSON grid carries numbers untyped; the declaration makes ``20`` an int window,
    not ``20.0``). For ``ma_cross`` these are ``fast_window`` and ``slow_window`` (both ``'int'``)."""

    name: str  # the parameter name (matches the sweep_signals inner-function arg + the result column level)
    kind: str  # 'int' | 'float' — the value type the engine coerces grid values to


@dataclass(frozen=True)  # the wide-frame sweep input; immutable, mirrors StrategyInput
class SweepInput:
    """Wide-frame input to a strategy's ``sweep_signals`` (the sweep analogue of ``StrategyInput``).

    Every field is a wide DataFrame whose columns are the universe tickers (a single shared, union-index,
    NaN-padded calendar — R-CAL, §5.1), so the strategy computes signals for the whole universe at once.
    The engine builds these frames (aligning every member on the union index) and owns the portfolio build
    (the per-column costs + the partition annualization, §5.2/§5.3); the strategy only needs the price
    frames its rule reads (``ma_cross`` reads ``close``; a breakout rule would read ``high``/``low``)."""

    close: "pd.DataFrame"  # wide close, columns = tickers (the union-index, NaN-padded universe frame)
    open: "pd.DataFrame"   # wide open  (next-open fills) — same columns/index as close
    high: "pd.DataFrame"   # wide high  (MFE/MAE + breakout rules)
    low: "pd.DataFrame"    # wide low   (MFE/MAE + breakout rules)


def coerce_param_value(value, kind: str):
    """Coerce one grid value to the SweepParam ``kind`` (a JSON grid carries numbers untyped).

    ``'int'`` -> Python ``int`` (so a window is ``20``, not ``20.0`` — vbt's MA window must be integer and
    the canonical JSON must read ``20``); ``'float'`` -> Python ``float``. Fail-loud on an unknown kind: a
    typo in a strategy's ``SweepParam`` declaration is a contract bug, not a value to pass through."""
    if kind == "int":  # integer parameter (e.g. an indicator window)
        return int(value)  # int("20")/int(20.0)/int(20) all -> 20
    if kind == "float":  # real-valued parameter (e.g. a threshold)
        return float(value)  # normalize to a Python float
    raise ValueError(f"unknown SweepParam kind {kind!r} (expected 'int' or 'float')")  # contract bug, fail loud


def canonical_param_json(combo: dict) -> str:
    """The ONE canonical JSON string for a parameter combo (the backend hashes this into param_config_hash).

    Sorted keys + compact separators make it deterministic and whitespace-free, so the same combo always
    yields the same string (hence the same ``param_config_hash``) regardless of dict insertion order. The
    values must already be coerced (``coerce_param_value``) so ``20`` reads as ``20`` (an int), not ``20.0``.
    Used by the engine to write ``research_run_variations.param_json`` and by the backend's hash; 1C reuses
    it when freezing the grid so the registered combos hash identically to the executed ones."""
    return json.dumps(combo, sort_keys=True, separators=(",", ":"))  # deterministic, compact, sorted-key


def canonical_combo_order(combos: list[dict], param_names: tuple[str, ...]) -> list[dict]:
    """Sort parameter combos into the ONE canonical order; the list index is each combo's ``variation_id``.

    The sort key is the tuple of values in ``param_names`` order (the strategy's ``sweep_params`` order), so
    the ordering is stable and reproducible: combo ``{"fast_window":5,"slow_window":20}`` always precedes
    ``{"fast_window":10,"slow_window":20}``. The engine assigns ``variation_id = canonical_combo_order(...)``
    index (§6.4), and 1C MUST use this same sort when it freezes the registered grid, so the registered
    ``variation_id`` set matches the executed one (the exactly-once gate, §6, depends on stable ids)."""
    return sorted(combos, key=lambda c: tuple(c[name] for name in param_names))  # by the declared param order
