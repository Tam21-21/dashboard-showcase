"""The ``Strategy`` abstract base class: the one method every strategy implements.

A strategy is a pure function of its inputs: ``run(data, settings) -> RunResult``.
The ABC fixes that signature for all strategy kinds (``Strategy`` now,
``EventStrategy`` in M3.5, ``MLStrategy`` in M10).

Purity pre-conditions (M3 spec §3.5), enforced by code review at brief B and PUT
validation at brief G, and relied on by the two-pass slippage-cost method:

  - ``run`` must be a pure function of ``(data, settings)``: no hidden state, no
    global mutation, no random seeding unless the seed comes from ``settings``.
  - No hardcoded fee or slippage constants in strategy code. Every cost parameter
    comes from ``settings`` (read via ``get_setting``), so a zero-slippage shadow
    run actually has zero slippage.

Pure Python: importing this module pulls in neither pandas nor vbt.
"""
from __future__ import annotations  # lazy annotations → import stays light

from abc import ABC, abstractmethod  # standard abstract-base-class machinery
from dataclasses import dataclass  # #376 ruling 4 round 2 (E-R1-B1): SizedStrategyInput's own decorator
from typing import TYPE_CHECKING  # keep the contract-type imports type-check-only

# StrategyInput is now a REAL (not TYPE_CHECKING-only) import: SizedStrategyInput below subclasses it,
# which needs the actual class object at class-definition time, not just a string annotation. This adds
# no pandas/vbt cost (strategy_input.py's own module-level imports are `dataclasses` plus a TYPE_CHECKING
# pandas reference; its one pandas use is inside a lazily-imported default factory), so the module
# docstring's "pulls in neither pandas nor vbt" claim still holds.
from strategies.base.strategy_input import StrategyInput  # the locked input type; also the new subclass's base

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    from strategies.base.run_result import RunResult  # the locked output type
    from strategies.base.settings import SettingRow  # the typed settings rows
    from strategies.base.sweep import SweepInput, SweepParam  # M5 1B: the wide-sweep contract types
    from strategies.helpers.sizing import SizingSpec  # type-only: no base -> helpers runtime dependency


class StrategyInputError(ValueError):
    """Raised by a strategy's ``validate_input`` when it rejects the run's input (V7, brief A3).

    A ``ValueError`` subclass so any existing ``except ValueError`` path still catches it; the
    container entrypoint maps it to the clean machine-readable error line it already emits, so a
    bad input fails clearly at the gate instead of as a confusing downstream traceback."""


@dataclass(frozen=True)  # frozen: matches StrategyInput's own immutable contract
class SizedStrategyInput(StrategyInput):
    """``StrategyInput`` plus ONE additive field: the registry-resolved ``SizingSpec`` a container
    entrypoint already looked up before calling ``strat.run()`` (#376 ruling 4 round 2, finding E-R1-B1).

    A strategy's own ``run()`` (``ma_cross.py`` and every generated strategy -- none of them lane-E
    files) calls ``strategies.helpers.portfolio.long_only_signal_result`` with no ``sizing=`` kwarg at
    all, so without this field that helper's bare ``SizingSpec()`` default silently governs the ACTUAL
    position sizing while ``meta.json``'s environment records the registry's id: a real, if currently
    invisible, mismatch, since the id recorded and the spec applied agree only because
    ``vol_target_atr``'s spec equals ``SizingSpec()`` field for field today (#312/#313), never because
    anything wires them together. Subclassing here rather than editing ``strategy_input.py`` (outside
    lane E's declared areas, ``docs/plans/night/2026-09-08_set3a_night/PLAN.md``'s lane table) keeps
    this fix inside the files this unit owns. ``resolved_sizing`` defaults to ``None`` so a plain
    ``StrategyInput`` (built by any host-side caller or probe that never heard of this subclass) is
    unaffected -- ``long_only_signal_result`` reads it with ``getattr(data, "resolved_sizing", None)``,
    never an ``isinstance`` check, so a plain ``StrategyInput`` and this subclass both work at the same
    call site, matching AC-E5's field-for-field default.
    """
    resolved_sizing: "SizingSpec | None" = None


class Strategy(ABC):
    """Base class for every Research Lab strategy.

    Implement ``run(self, data, settings) -> RunResult``. To make a value SWEEPABLE over a universe you have two
    additive, opt-in choices, and the engine picks the mode from what you declare (declare only one):
      - INLINE (M5 C, simplest): mark the value where you use it in ``run()`` with
        ``period = self.param("period", 14, type="int")``. The dispatch grid renders it automatically and the
        engine runs ``run()`` once per (combination x ticker). Best for a small grid; on a normal single run
        ``self.param`` returns the default.
      - VECTORIZED (for a large grid): declare a class ``sweep_params`` tuple AND override ``sweep_signals`` to
        compute the wide signal frame in one parameterized pass. More to write; the scalable path.
    """

    # The asset classes this strategy is allowed to run on (M4 D1/D7). A literal tuple drawn from the
    # closed vocabulary {crypto, equities}, read at dispatch by AST (extract_asset_class_scope) the same
    # no-exec way event_types is. The base default () means UNCONSTRAINED — the dispatch guard does not
    # fire, exactly as event_types=() means "not event-driven". A concrete strategy declares its real
    # scope (e.g. ma_cross=("crypto","equities")); the guard then refuses a run whose dataset resolves to
    # a class outside the scope (the run #21 lesson). Declared on Strategy (not only EventStrategy) so a
    # price strategy that subclasses Strategy directly (ma_cross) can carry it too.
    asset_class_scope: tuple[str, ...] = ()

    # M5 1B (§4): the sweep contract — additive and OPT-IN. A strategy is sweepable iff it declares
    # non-empty `sweep_params` (the swept parameters, name + type) AND overrides `sweep_signals` (the wide
    # signal method). The base defaults below make an un-migrated strategy NOT sweepable: the engine reads
    # `sweep_params` to build/validate the grid and calls `sweep_signals`, which raises SweepNotSupported on
    # the base, so the engine refuses the sweep with a clear error (never a silent single-run fallback). The
    # locked `run` path is untouched — single and event runs do not see any of this.
    sweep_params: tuple["SweepParam", ...] = ()  # empty -> not sweepable; ma_cross declares fast/slow window
    # Optional vbt.Param condition strings the engine applies at grid build to drop invalid combos (R-SWEEP),
    # e.g. ("fast_window < slow_window",). Each references sweep_params names. Empty -> the full product runs.
    sweep_conditions: tuple[str, ...] = ()

    # #376 ruling 4 (the engine-and-sizing-provenance unit, night three): the id this strategy's
    # positions are sized by, resolved through the registry at
    # ``strategies.helpers.sizing_methods`` before any entrypoint reads a bar. The base default,
    # ``vol_target_atr``, is the ONE method the registry carries today and its spec equals
    # ``SizingSpec()`` field for field (the exponentially smoothed true range every un-migrated
    # strategy already runs, sizing.py's own default), so declaring nothing here changes no saved
    # number. An id the registry does not carry fails the run at the container gate, before a bar
    # is read, never a silent fall-back to this default.
    sizing_method: str = "vol_target_atr"

    # M5 C (the inline-parameter contract): the per-combo injection slot. The per-combo sweep engine sets this
    # to the current combo {name: value} immediately before each run() and clears it after; for a NORMAL run (a
    # single backtest, an event run, or the zero-param path) it stays None, so self.param returns its default.
    # A third additive opt-in beside sweep_params/sweep_signals: a strategy marks a value sweepable INLINE with
    # self.param(name, default, type) in run(), and the engine sweeps it by running run() per (combo x ticker).
    _active_combo: "dict | None" = None

    @abstractmethod  # subclasses MUST implement run(); Strategy() itself is not instantiable
    def run(self, data: "StrategyInput", settings: "list[SettingRow]") -> "RunResult":
        """Run the strategy and return a complete RunResult (all six slots filled)."""
        raise NotImplementedError  # unreachable for subclasses; explicit for clarity

    def sweep_signals(self, data: "SweepInput", params: "dict[str, list]") -> tuple:
        """Compute WIDE entry/exit signals across the universe and the parameter grid (M5 1B §4).

        Additive, opt-in. ``data`` is the wide-frame ``SweepInput`` (close/open/high/low, columns =
        tickers, one shared union-index calendar). ``params`` maps each ``sweep_params`` name to its grid
        value list (already coerced to the declared kind). The method returns a 3-tuple
        ``(entries, exits, indicators)`` where ``entries``/``exits`` are wide boolean DataFrames whose
        column index carries the parameter levels (named by ``sweep_params``) plus a ``ticker`` level — the
        ``(combo x ticker)`` product (R-PROD) — and ``indicators`` is a ``{name: wide DataFrame}`` dict of
        the warmup-defining series at the SAME columns (so the engine can compute each cell's funnel via the
        shared ``compute_signal_funnel``, §5.5). The engine owns the portfolio build (per-column costs,
        per-partition annualization) and reads the combo off the result columns.

        The base raises SweepNotSupported so a non-sweepable strategy fails loud at the engine boundary
        (never a silent single-run fallback). A sweepable strategy overrides this AND declares
        ``sweep_params`` (the engine checks both)."""
        from strategies.base.sweep import SweepNotSupported  # function-local: keep the contract import light

        raise SweepNotSupported(
            f"{type(self).__name__} does not implement the sweep contract (no sweep_signals override)"
        )

    def single_run_params(self) -> dict:
        """Declare the ONE parameter combo this strategy's own ``run()`` uses when it is not
        swept (lane DG, `6.1g`, the contract method a single backtest needs beside
        ``lookback_bars`` and ``used_degrees_of_freedom`` below).

        A single backtest (dispatched through neither the wide vectorized sweep nor the
        per-combo grid) has no combo anywhere in today's schema: ``run()`` just reads whatever
        module-level constants or settings it likes. So ``used_degrees_of_freedom(params)``
        cannot be called for a single run at all, because it needs a ``params`` dict keyed by
        this strategy's own ``sweep_params`` names, and a single run never builds one.

        Return the SAME dict ``run()`` actually uses, keyed by the same names ``sweep_params``
        declares, so the same combo can be handed unchanged to ``lookback_bars`` and
        ``used_degrees_of_freedom``. Read the values from the SAME module constants (or
        equivalent) ``run()`` itself reads, never re-derive or re-type them: a caller-side
        introspection of a class attribute by name would be a second copy of the strategy's own
        default that can silently drift from what ``run()`` actually runs.

        The base raises ``NotImplementedError`` by name (``single-run-params-undeclared``),
        matching ``lookback_bars``'s and ``used_degrees_of_freedom``'s own opt-in pattern below:
        a strategy that has not declared its own single-run combo fails loud, never a guessed or
        introspected value. The single-run entrypoint reads the raised exception by name and
        proceeds without a combo (no row is written), exactly as a single run behaves today."""
        raise NotImplementedError(
            f"{type(self).__name__} does not declare its single-run parameter combo "
            f"(single-run-params-undeclared: no single_run_params override)"
        )

    def lookback_bars(self, params: dict) -> int:
        """Declare this combo's own lookback window, in bars (fold item 3, round-1 review, finding U2).

        The walk-forward stage's embargo (``strategies.base.walkforward.derive_purge_and_embargo``)
        needs the longest window any of THIS strategy's own indicators reads for a given parameter
        combo, so a held-out out-of-sample slice never starts inside a window the in-sample fit could
        already see. ``params`` is one combo dict, keyed by this strategy's own ``sweep_params``
        names -- the SAME shape ``run_sweep_entrypoint.py`` builds from the grid. Return the number
        of bars as a plain int, never a window object, never a name.

        The base raises NotImplementedError, matching ``sweep_signals``'s own opt-in pattern above:
        a strategy that has not declared its own lookback fails loud, by name
        (``walkforward-lookback-undeclared``), before any walk-forward slice runs, rather than
        silently borrowing an unrelated number (the round-1 finding: the build had been passing the
        sizing estimator's own window here, which answers a different question)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not declare a walk-forward lookback (no lookback_bars override)"
        )

    def used_degrees_of_freedom(self, params: dict) -> int:
        """Declare this combo's own used degrees of freedom, Udf (lane DF2, `6.1e`, `#395`
        rulings 1 and 2): the judge's check 1 reads this alongside the test window's own bar
        count (Odf) to compute Pardo's `Rdf = 100 x [1 - (Udf/Odf)]` (the evidence-floor check).

        Pardo's own worked example, quoted verbatim (chapter 13, "Degrees of Freedom, Sample Size
        and Startup Overhead", PDF page 326, book page 293). One unbroken quote, kept on one line
        here on purpose so no line wrap can split a phrase this method's own docstring is checked
        against word for word:

        "The trading system uses a 10-day and a 50-day close moving average and has 5 trading rules. It uses 57 degrees of freedom calculated as follows. The long moving average uses 51 degrees of freedom: 50 closes to calculate it and 1 as a rule. The short moving average only uses 1 degree of freedom because it is counted as a rule even though it uses the same data as the long average. The other 5 trading rules each use 1 degree of freedom."

        So the strategy's LONGEST window contributes one degree per data point it reads PLUS one
        for the rule the average itself is, while every OTHER indicator or rule that reads the
        same price data (a second moving average, an entry rule, an exit rule) contributes exactly
        one degree each, never its own window length again.

        ``params`` is one combo dict, keyed by this strategy's own ``sweep_params`` names, the
        SAME shape ``lookback_bars`` above already takes and ``run_sweep_entrypoint.py`` already
        builds from the grid. Return the count as a plain int, never a fraction, never a name.

        The base raises NotImplementedError, matching ``lookback_bars``'s own opt-in pattern
        above: a strategy that has not declared its own count fails loud, by name
        (``used-degrees-of-freedom-undeclared``), before check 1 ever grades it, rather than
        silently fabricating a number (`#395` ruling 2: an honest declaration for a strategy whose
        indicator reads every prior bar would fail every one of its own candidates, and a smaller,
        invented number would be exactly the fabrication this method refuses to guess)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not declare its used degrees of freedom "
            f"(used-degrees-of-freedom-undeclared: no used_degrees_of_freedom override)"
        )

    def validate_input(self, data: "StrategyInput", settings: "list[SettingRow]") -> None:
        """Optional input-validation hook (V7, brief A3). Default no-op: a strategy that does not
        override this behaves exactly as before (additive to the locked contract, DECISIONS #12).

        A strategy MAY override it to reject an input it cannot run (e.g. a price series shorter than
        its longest window) by raising ``StrategyInputError`` with a specific message, so the run
        fails clearly at the gate instead of failing confusingly downstream. Reads, never mutates
        (``StrategyInput`` is frozen, strategy_input.py:23). The entrypoint calls it on the SAME
        instance that runs, after the inputs are assembled and before ``run``."""
        return None  # default: accept every input

    def param(self, name: str, default, type: "str | None" = None):
        """Declare a sweepable INLINE parameter (M5 C). Returns the swept combo's value when this run is part
        of an inline-param sweep, else ``default``.

        Call it where you USE the value in ``run()``, e.g. ``period = self.param("period", 14, type="int")``.
        The backend AST-reads these calls at dispatch (no exec) so the dispatch grid renders one input per
        inline param; the per-combo engine injects the current combo's value via ``_set_active_combo`` before
        each ``run()``. ``type`` is ``'int'`` | ``'float'`` (the SweepParam kind vocabulary); when omitted it is
        inferred from ``default``'s Python type (``int`` not ``bool`` -> ``'int'``, else ``'float'``), so both
        ``self.param("period", 14)`` and ``self.param("period", 14, type="int")`` work. The returned value is
        coerced to that kind by the same ``coerce_param_value`` the wide sweep path uses, so an inline param is
        typed identically to a ``sweep_params`` value. On a normal single run (``_active_combo`` is None) it
        returns the default -> an inline-param strategy is an ordinary single-run strategy when not swept."""
        from strategies.base.sweep import coerce_param_value  # function-local: keep the base import light
        # bool is a subclass of int; exclude it so a True/False default is not mis-typed as an int window
        kind = type or ("int" if isinstance(default, int) and not isinstance(default, bool) else "float")
        if self._active_combo is None or name not in self._active_combo:  # not swept -> the author's default
            return coerce_param_value(default, kind)
        return coerce_param_value(self._active_combo[name], kind)  # swept -> the injected combo value

    def _set_active_combo(self, combo: "dict | None") -> None:
        """Engine hook (the per-combo sweep path only): set the combo injected into the NEXT run() (M5 C).

        The per-combo engine calls this immediately before each ``run()`` with the current combo {name: value}
        (and never on the single/event/zero/vectorized paths, where it stays None so ``param`` returns defaults).
        A plain attribute set: the container runs combos x members sequentially on one instance, so there is no
        race and no stale state (the next call overwrites it)."""
        self._active_combo = combo
