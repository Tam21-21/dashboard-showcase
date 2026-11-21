"""Moving-average crossover (MA-cross)

The M3 reference strategy: a long-only moving-average crossover on a single
instrument. It exists to exercise the full strategy contract end to end against
real data, not to make money.

## Rules

- **Indicators.** A fast and a slow simple moving average of the close, computed
  with vbt's native `MA` indicator (not a pandas rolling mean).
- **Entry.** Go long when the fast average crosses *above* the slow average. The
  crossover fires only on the bar where the relationship flips, so a long stretch
  of "fast above slow" is one entry, not one per bar.
- **Exit.** Close the position when the fast average crosses *below* the slow
  average.
- **Sizing.** Commit 100% of available cash on entry. Starting cash is $10,000.
- **Execution.** Signals are computed at the bar close and executed at the next
  bar's open.
- **Costs.** Fees and slippage are read from the lab settings, never hardcoded;
  the slippage cost is the direct per-fill execution shortfall, recovered from the
  same rate the engine applied (DECISIONS #66/#67), not a two-pass shadow diff.

## Parameters

Fast window 20, slow window 50. These are per-strategy parameters fixed in code
for M3; they become editable settings in M4 (M3 reads only global settings, L11).

## Scope

Class-agnostic from M4 on. A moving-average cross is a price rule with no
asset-class assumption, so it runs cost-correctly on crypto or equities: it reads
the neutral `default` fee and slippage keys, which dispatch routes to the right
profile per the dataset's asset class (M4 D2). The M3 reference instrument was
BTC/USDT 1h; the rule itself is unchanged.
"""
from __future__ import annotations  # lazy annotations

import vectorbtpro as vbt  # the backtest engine; only strategy code (not the contract) imports it

from strategies.base import (  # contract surface
    RunResult,
    SettingRow,
    Strategy,
    StrategyInput,
    StrategyInputError,
    SweepInput,        # M5 1B: the wide-frame sweep input
    SweepParam,        # M5 1B: the sweepable-parameter declaration
)
from strategies.helpers import default_costs, long_only_signal_result  # M4 R15 cost read + M4.5 R16 shared price build

FAST_WINDOW = 20  # fast SMA length (per-strategy param; becomes a setting in M4)
SLOW_WINDOW = 50  # slow SMA length
INIT_CASH = 10_000  # starting cash in $ (locked by L14; not a cost, so not a settings lookup)


class MaCross(Strategy):
    """Long-only fast/slow moving-average crossover."""

    # M4 D1/D7: class-agnostic by design (see the Scope docstring) — a moving-average cross reads the
    # neutral cost keys and runs cost-correctly on either class, so it declares BOTH. Declaring the honest
    # scope means its dispatch guard never falsely refuses a legitimate equities (or crypto) MA-cross run.
    asset_class_scope = ("crypto", "equities")

    # M5 1B (§4): the sweep contract. ma_cross is the reference implementation — the two fixed windows
    # (FAST_WINDOW/SLOW_WINDOW above) become the two swept parameters. The engine reads sweep_params to
    # coerce the grid values and applies sweep_conditions (fast < slow) at grid build to drop invalid combos
    # (R-SWEEP). The single-run `run` path below is unchanged — only the additive `sweep_signals` is new.
    sweep_params = (SweepParam("fast_window", "int"), SweepParam("slow_window", "int"))  # both integer windows
    sweep_conditions = ("fast_window < slow_window",)  # a cross needs the fast window shorter than the slow

    def validate_input(self, data: StrategyInput, settings: list[SettingRow]) -> None:
        """V7 (brief A3): reject a price series too short for the slow SMA before the run starts,
        with a clear error, instead of vbt silently producing all-NaN SMAs (and zero trades)
        downstream. The optional illustrative override; the base default is a no-op.

        Lane WP round 3 (finding WP-F2): reads slow_window through self.param, the SAME way run()
        does, rather than the bare module constant. The walk-forward per-combo engine calls
        _set_active_combo(combo) BEFORE validate_input on every dispatch path (run_sweep_entrypoint.
        py's own comment on that line: "the V7 gate; may key off a combo value (run after the
        inject)"), so a swept combo asking for a slow window the guard never measured used to pass
        this gate silently and reach run() with too few bars for the slow SMA, the exact failure
        this gate exists to catch. On an ORDINARY single run or a vectorized sweep _active_combo
        stays None, so self.param returns its default (SLOW_WINDOW unchanged) and this check is
        byte-identical to before. The error message names the value actually checked (the combo's
        own slow_window when one is injected), not the module constant, since that is the number
        this gate is really testing against now.
        """
        slow_window = self.param("slow_window", SLOW_WINDOW, type="int")  # the SAME read run() does
        if len(data.close) < slow_window:  # the slow SMA needs at least slow_window bars to exist
            raise StrategyInputError(
                f"MaCross needs at least slow_window={slow_window} bars; got {len(data.close)}"
            )

    def run(self, data: StrategyInput, settings: list[SettingRow]) -> RunResult:
        """Run the crossover backtest and return a complete RunResult."""
        close = data.close  # closing price series the SMAs are built from

        # Lane WP round 2 (6.5, finding WP-F1): read the two windows through self.param rather
        # than the bare module constants, so the walk-forward per-combo engine can inject a swept
        # combo here via _set_active_combo. On an ORDINARY single run or a vectorized sweep
        # _active_combo stays None, so self.param returns its default (FAST_WINDOW/SLOW_WINDOW
        # unchanged), and this class also keeps declaring class sweep_params plus overriding
        # sweep_signals below, so the wide vectorized path is untouched: it never calls run() at
        # all, let alone self.param. single_run_params() below keeps reading the SAME module
        # constants, so a single run and its recorded combo can never drift apart.
        fast_window = self.param("fast_window", FAST_WINDOW, type="int")
        slow_window = self.param("slow_window", SLOW_WINDOW, type="int")

        # Fast and slow simple moving averages via vbt's native indicator.
        fast_sma = vbt.MA.run(close, window=fast_window).ma  # fast SMA series
        slow_sma = vbt.MA.run(close, window=slow_window).ma  # slow SMA series

        # Crossover signals: True only on the bar where the relationship flips.
        entries = fast_sma.vbt.crossed_above(slow_sma)  # enter long on upward cross
        exits = fast_sma.vbt.crossed_below(slow_sma)  # exit on downward cross

        # Costs come from the lab settings via the neutral key dispatch routed by asset class (M4 D2):
        # a crypto dataset resolves 'default' to the crypto profile, an equities dataset to the equities
        # profile, so the same MA-cross runs cost-correctly on either class. Read via the shared helper
        # (M4 R15) — the extracted form of this two-key read.
        fees, slippage = default_costs(settings)  # (fees, slippage) from the neutral 'default' keys

        # Phase-2 Unit 1: delegate the portfolio build to the shared helper — the verbatim extraction of
        # this method's own tail (the from_signals build + the per-fill slippage cost + the RunResult).
        # That single home now also carries the index-derived Sharpe annualization fix (A2b-001/A2b-002),
        # so ma_cross no longer duplicates the from_signals call (which had the same missing-freq bug).
        return long_only_signal_result(
            data, entries, exits, fees, slippage,  # signals computed above + the neutral-key costs
            indicators={"fast_sma": fast_sma, "slow_sma": slow_sma},  # the two SMAs for the price-chart overlay (L4)
            init_cash=INIT_CASH,  # $10,000 starting cash (L14)
        )

    def single_run_params(self) -> dict:
        """Lane DG (`6.1g`): the ONE combo `run()` above actually uses on a single backtest, read
        from the SAME module constants `run()` itself reads (`FAST_WINDOW`, `SLOW_WINDOW`), so
        this can never drift from what a single run actually does; never a second, re-typed copy
        of those constants. Keyed by the same names `sweep_params` declares, so the returned dict
        feeds straight into `lookback_bars` and `used_degrees_of_freedom` below unchanged."""
        return {"fast_window": FAST_WINDOW, "slow_window": SLOW_WINDOW}

    def lookback_bars(self, params: dict) -> int:
        """Fold item 3: this combo's own longest SMA window, the bars its slow (or fast, if a swept
        grid ever inverted the two) average needs to exist before it produces a real value. `max`
        rather than assuming slow_window is always the larger: sweep_conditions enforces
        fast_window < slow_window at the VECTORIZED grid build (R-SWEEP), but this method also runs
        for the walk-forward per-combo path, which does not apply that condition, so taking the max
        of the two stays correct either way."""
        return max(params["fast_window"], params["slow_window"])

    def used_degrees_of_freedom(self, params: dict) -> int:
        """Lane DF2 (`6.1e`): this combo's own used degrees of freedom, Udf. The count below follows
        the PLAN's own reading of Pardo's chapter 13 worked example (PDF page 326, book page 293,
        quoted in full on the base class above), where each moving average is charged one degree "as
        a rule" and each of the example's other named trading rules costs one degree more. Under
        that reading, this strategy owes: the longest declared window (the same value `lookback_bars`
        above returns, one degree per data point that window reads), PLUS one degree for each of the
        two moving averages this strategy computes (the fast SMA and the slow SMA), PLUS one degree
        for each of this strategy's own two trading rules taken as further rules beyond the two
        per-average charges: the ENTRY rule (go long when the fast SMA crosses above the slow SMA)
        and the EXIT rule (close the position when the fast SMA crosses below the slow SMA). Four
        extra degrees total (2 averages plus 2 rules), on top of the window.

        A second reading of the same worked example exists and is recorded here rather than
        silently discarded: it reads the crossover's entry and exit as ALREADY priced by the two
        per-average charges above, so the two-rule charge would double count them, and that reading
        would return the window plus two degrees, two fewer than the count below. The plan's reading
        (the one this method implements) is the STRICTER of the two: it consumes more degrees, which
        makes Rdf (Pardo's remaining-degrees-of-freedom percentage) lower, so it errs toward
        rejecting a strategy rather than toward passing one. This choice is recorded here for
        Tamer's ruling in the ledger, entered by the lead; the return value below is unchanged by
        that recording and stays pinned at the plan's own reading until a ruling says otherwise.

        A pure function of `params` (this combo's own fast_window/slow_window) and this module's
        own literals (2 averages, 2 rules): the same combo always returns the same count, and the
        count scales with whichever window is the LONGEST (`max`, matching `lookback_bars` above,
        never assuming slow_window is the larger one, since the walk-forward per-combo path never
        applies `sweep_conditions`)."""
        return max(params["fast_window"], params["slow_window"]) + 2 + 2  # longest window + 2 averages + 2 rules

    def sweep_signals(self, data: SweepInput, params: dict[str, list]) -> tuple:
        """M5 1B (§4): the WIDE sweep signals — the same crossover rule as `run`, as a vbt.Param function.

        Given the wide universe close (columns = tickers) and the swept fast/slow window value lists, return
        wide boolean `(entries, exits)` and the `{name: wide indicator frame}` dict, all indexed by the
        `(fast_window, slow_window, ticker)` product. Built with `@vbt.parameterized(merge_func="column_stack")`:
        the decorator generates the SIGNALS for each conditioned combo and column-stacks them (the partial-
        parameterization idiom verified against the pinned build), so the engine runs ONE wide `from_signals`
        over all stacked columns — the R-PROD one-pass, NOT a per-combo simulation loop (which §4 rejects).
        The `condition` drops invalid combos (fast >= slow) at grid build (R-SWEEP). The signal lines are the
        SAME two as `run` (crossed_above / crossed_below on the two SMAs), with the windows as vbt.Param sets
        instead of the fixed scalars — so each cell equals the single `run` cell-for-cell.
        """
        close = data.close  # the wide universe close (columns = tickers); the rule reads only close

        # The per-combo signal generator. The decorator iterates the conditioned (fast, slow) product and
        # column-stacks each combo's per-ticker signals, naming the column levels after these arg names
        # (so the result columns carry fast_window/slow_window/ticker — the engine reads the combo off them).
        @vbt.parameterized(merge_func="column_stack")  # partial parameterization: signals wide, one sim later
        def _gen(close, fast_window, slow_window):
            fast_ma = vbt.MA.run(close, window=fast_window, hide_params=True).ma  # fast SMA per (combo, ticker)
            slow_ma = vbt.MA.run(close, window=slow_window, hide_params=True).ma  # slow SMA per (combo, ticker)
            entries = fast_ma.vbt.crossed_above(slow_ma)  # enter on upward cross (same rule as run)
            exits = fast_ma.vbt.crossed_below(slow_ma)     # exit on downward cross
            return entries, exits, fast_ma, slow_ma  # signals + the two warmup indicators (for the per-cell funnel)

        # Build the vbt.Param sets from the grid: the fast param carries the class's declared sweep_conditions
        # (joined with `and` if several), so the decorator drops invalid combos at build (R-SWEEP). Reading
        # the class attr — not a literal — keeps ONE source of the condition, so 1C (which freezes the grid
        # from sweep_conditions) and the engine produce the SAME conditioned combo set (no drift).
        condition = " and ".join(self.sweep_conditions) or None  # None when a strategy declares no condition
        entries, exits, fast_ma, slow_ma = _gen(
            close,
            vbt.Param(params["fast_window"], condition=condition),  # conditioned fast windows (fast < slow)
            vbt.Param(params["slow_window"]),                       # slow windows
        )
        # indicators keyed by name, wide frames at the same (combo x ticker) columns — the engine slices
        # them per column for compute_signal_funnel (so bars_after_warmup is exact, matching the single path).
        return entries, exits, {"fast_sma": fast_ma, "slow_sma": slow_ma}
