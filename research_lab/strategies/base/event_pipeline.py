"""Event-run equity / RunResult assembly (M3.5 brief J1).

The "vbt-as-accountant" core. Brief E (``triple_barrier.label_event``) owns each event's
exit bar and exit kind; THIS module turns a set of labelled events into the locked six-slot
``RunResult`` by:

  1. resolving the realized FILL PRICE for each event from the engine's exit level, worsened
     by settings-driven slippage per exit kind and trade side (the #59 fill model, §6.2);
  2. selecting which events get a position (the causal first-by-knowledge_at concurrency cap
     and the one-order-per-(symbol, bar) rule, §8/§9);
  3. placing those as explicit per-order prices into ``vbt.Portfolio.from_orders`` on a daily
     mark-to-market grid (cash-shared across symbols, §7); and
  4. deriving all six slots through the SAME ``build_run_result`` the price run uses, so the
     event run and the price run share ONE extraction surface (EL61 / DDIA single source).

We NEVER let vbt re-derive an exit (no ``sl_stop``/``tp_stop``): brief E is the sole exit
authority. We hand vbt the price we computed; vbt only keeps the books. Slippage is baked into
our fill price (read from the ``scope='slippage'`` settings rows) and the engine is told
``slippage=0``, so vbt never sees it. ``total_slippage_cost`` is the direct per-fill execution
shortfall (DECISIONS #66/#67): for each order the one run actually made, the filled size times
the gap between the executed price and the clean reference the fill model produced (the bar open
for an entry, ``clean_exit_reference`` for an exit), summed once here — there is no shadow pass.

Imports vbt; kept OUT of the import-light ``strategies.base`` contract surface (imported
function-locally by ``EventStrategy.run``, the same rule ``vbt_extraction`` follows).
"""
from __future__ import annotations  # lazy annotations, consistent with base/

from dataclasses import dataclass  # the placed-trade record is a plain frozen value
from typing import TYPE_CHECKING, Protocol  # Protocol = the pluggable sizer seam (§8)

import numpy as np  # vectorized grid construction
import pandas as pd  # the daily grid + per-symbol close frames
import vectorbtpro as vbt  # the accounting engine; only backtest-running modules import it

from strategies.base.triple_barrier import (  # the exit-kind constants the fill model reads (BN-J1-1)
    EXIT_LOWER_BARRIER,
    EXIT_UPPER_BARRIER,
)
from strategies.base.slippage import per_fill_slippage_cost  # the one per-fill slippage formula (shared with the price path)
from strategies.base.vbt_extraction import build_run_result  # pf -> six-slot RunResult (the one extractor)

if TYPE_CHECKING:  # readers / type checker only; no runtime import cost
    from strategies.base.run_result import RunResult  # the locked output type
    from strategies.base.triple_barrier import BarrierLabel  # the per-event label (brief E)

INIT_CASH = 10_000  # starting cash in $ — the locked reference-run convention (ma_cross.py:42, L14); not a cost

LONG, SHORT = 1, -1  # trade-side encoding (+1 long, -1 short); the side comes from the strategy, NOT the label sign

# The event path's daily mark-to-market annualization convention (BN-J1-3). The grid is one bar per
# trading day, annualized on the 252-trading-day year (Carver's ~256 sqrt-time base,
# carver-systematic-trading-tibot.md:421-422); vbt computes ann_factor = year_freq / freq = 252.
# Exposed as module constants (not inline literals) so run-finalize records the SAME annualization on
# an event run's research_runs columns — one source of truth, no drift (Phase-2 Unit 7).
EVENT_ANNUALIZATION_FREQ = "1d"             # the daily-grid bar period passed to from_orders
EVENT_ANNUALIZATION_YEAR_FREQ = "252 days"  # the trading-day-per-year convention passed to from_orders


def event_annualization() -> dict:
    """The event path's fixed annualization, keyed by the research_runs annualization column names.

    The event path annualizes its Sharpe on the fixed daily 252-trading-day convention (the from_orders
    freq/year_freq below), NOT an index-derived factor: this returns exactly what vbt applied
    (ann_factor = year_freq / freq = 252), so an event run records a faithful annualization, uniform with
    a price run's columns (Phase-2 Unit 7, closing the Unit-1 event carve-out). freq/year_freq are
    normalized through pd.Timedelta so they match the price path's str(Timedelta) recording form exactly
    (run_backtest_entrypoint.py records the price run's resolve_annualization Timedeltas via str()).
    """
    freq = pd.Timedelta(EVENT_ANNUALIZATION_FREQ)            # "1d" -> a 1-day Timedelta
    year_freq = pd.Timedelta(EVENT_ANNUALIZATION_YEAR_FREQ)  # "252 days" -> a 252-day Timedelta
    return {
        "annualization_factor": float(year_freq / freq),    # = 252.0, the ann_factor vbt applied
        "annualization_freq": str(freq),                    # str(Timedelta) — matches the price path's form
        "annualization_year_freq": str(year_freq),          # str(Timedelta) — matches the price path's form
    }


# --------------------------------------------------------------------------- #
# The position-sizer seam (§8). A Sizer turns an admitted event into a share
# count off CAUSAL, SLIPPAGE-INVARIANT inputs: the requested share count does not
# depend on the slippage rate (it sizes off the un-slippaged open), so the
# per-fill slippage measure isolates the price effect. M5 vol-targeting and M10
# ML bet-sizing plug in here.
# --------------------------------------------------------------------------- #
class Sizer(Protocol):
    """The pluggable sizing interface (the §8 seam)."""

    def shares(self, entry_reference_price: float, init_cash: float) -> float:
        """Return the share count for one admitted event.

        ``entry_reference_price`` is the UN-slippaged entry-bar open (slippage-invariant on
        purpose, so the requested share count does not depend on the slippage rate). Future
        sizers may take more context (current portfolio state, an ML forecast) as keyword
        arguments; the M3.5 default needs only the reference price and the starting cash.
        """
        ...


@dataclass(frozen=True)
class FixedFractionSizer:
    """The M3.5 default: a fixed notional ``f * init_cash`` per trade (§8, OD-J1-3).

    ``f`` is the per-trade fraction (``f = 1/N`` at the concurrency cap ``N``, so the book is
    fully invested and never levered at the cap). The share count is computed off the
    un-slippaged entry open, so it is slippage-invariant: the requested share count does not
    depend on the slippage rate. This is a fixed notional off the run's starting cash (NOT
    compounding off live equity), the concrete reading of "pre-slippage gross equity"; a
    compounding / vol-target sizer is a future seam plug-in.
    """

    f: float  # the per-trade fraction of init_cash (provenance + value owed at J2, §13)

    def shares(self, entry_reference_price: float, init_cash: float) -> float:
        """``f * init_cash`` dollars of notional, in shares, off the un-slippaged open."""
        if not (0.0 < self.f <= 1.0):  # a fraction outside (0, 1] is a config error, never silent
            raise ValueError(f"position_fraction f must be in (0, 1], got {self.f}")
        if not np.isfinite(entry_reference_price) or entry_reference_price <= 0:  # corrupt price, not a coverage gap
            raise ValueError(f"entry_reference_price must be a positive finite price, got {entry_reference_price}")
        return (self.f * init_cash) / entry_reference_price  # fractional shares are fine in simulation


@dataclass(frozen=True)
class EventCandidate:
    """One surviving, covered, labelled event, ready for fill + sizing + placement.

    Assembled by ``EventStrategy.run`` after the membership gate, feature/signal filter, side
    resolution, and labelling. ``side`` is the strategy's trade direction (LONG/SHORT), NOT
    the label sign (AFML meta-labeling: the primary model gives the side, §6.3).
    """

    event_id: int  # the event's id (the spine key; also the trades-frame linkage)
    symbol: str  # the trade_time_symbol (the price-frame key and the vbt column)
    side: int  # LONG (+1) or SHORT (-1); the strategy's choice
    knowledge_at: int  # epoch s; the causal ordering key for selection (§8)
    label: "BarrierLabel"  # brief E's label: entry_ts / exit_ts / outcome_return / exit_kind


@dataclass(frozen=True)
class PlacedTrade:
    """A trade admitted to the book, with its realized fills and booked bars (§6, §7)."""

    event_id: int  # the originating event
    symbol: str  # the vbt column
    side: int  # LONG / SHORT
    shares: float  # the slippage-invariant share count (from the sizer)
    entry_ts: int  # epoch s; the entry bar (the un-slippaged-open bar)
    entry_fill: float  # the realized entry price (open worsened by entry slippage)
    entry_reference: float  # the un-slippaged entry basis (the bar open); the per-fill slippage reference (DECISIONS #67)
    booked_exit_ts: int  # epoch s; the exit bar AS BOOKED (next bar for a same-bar touch, §7.1)
    exit_fill: float  # the realized exit price (the #59 fill model output)
    exit_reference: float  # the un-slippaged exit basis (clean_exit_reference); the per-fill slippage reference (DECISIONS #67)


def clean_exit_reference(entry_open: float, label: "BarrierLabel") -> float:
    """The un-slippaged exit reference for one labelled event (the engine's exit LEVEL price).

    For every exit kind the clean exit basis is ``entry_open * (1 + label.outcome_return)``: a
    horizontal touch resolves at the barrier level, a time-out / truncation at the resolving close.
    This is the ONE place that formula lives — both ``realized_fills`` (which worsens it into the
    realized fill) and the placement (which carries it as the per-fill slippage reference) call this
    helper, so the slippage measure reads the SAME authority the fill came from, never an
    independent second opinion on the exit (SN-C4).
    """
    return entry_open * (1.0 + label.outcome_return)  # the barrier level / resolving close, un-slippaged


def realized_fills(
    label: "BarrierLabel",  # brief E's label for this event (carries outcome_return + exit_kind)
    side: int,  # LONG (+1) / SHORT (-1); worsening is applied AGAINST this side
    entry_open: float,  # the un-slippaged open at the entry bar (the engine's entry price)
    entry_slippage: float,  # the entry market-order slippage rate (settings, scope='slippage')
    exit_slippage: float,  # the exit market-order slippage rate (settings, scope='slippage')
    limit_miss_rate: float = 0.0,  # OD-J1-1: 0 = every touched profit-take fills (the optimistic bound)
) -> "tuple[float, float]":  # (entry_fill, exit_fill)
    """Compute the honest realized entry and exit fills for one labelled event (the #59 fix).

    The engine's exit LEVEL price is ``entry_open * (1 + label.outcome_return)`` for every exit
    kind (a horizontal touch resolves at the barrier level, a time-out/truncation at the
    resolving close). The fill model then worsens it per exit kind and side:

      - Entry is a market order: it fills WORSE than the open against the side (a long pays up,
        a short sells low): ``entry_open * (1 + side * entry_slippage)``.
      - The FAVORABLE barrier is a limit order: it fills AT the level, never worse (a limit
        fills at its level or better, Pardo §5.3). Favorable = the upper barrier for a long, the
        lower barrier for a short. ``limit_miss_rate`` would model the ~30% of limit touches that
        do not fill; M3.5 pins it to 0 (the optimistic bound) and rejects a non-zero value as
        out of scope (it needs a fall-through-exit engine).
      - The ADVERSE barrier is a market-on-touch (stop) order, and a time-out / truncated exit
        is a market-at-close order: both fill WORSE than the level against the side
        (``level * (1 - side * exit_slippage)``: a long sell fills low, a short buy fills high).
    """
    for rate in (entry_slippage, exit_slippage):  # negative slippage would IMPROVE a fill: a config error, fail loud
        if not np.isfinite(rate) or rate < 0:
            raise ValueError(f"slippage rate must be a finite, non-negative fraction, got {rate}")
    if limit_miss_rate != 0.0:  # OD-J1-1: the non-zero limit-miss model is out of M3.5 scope (needs a fall-through engine)
        raise NotImplementedError(
            "limit_miss_rate != 0 needs a fall-through-exit engine (a missed limit does not exit at the level); "
            "M3.5 pins it to 0 as the optimistic bound (OD-J1-1)"
        )
    if side not in (LONG, SHORT):  # an unknown side would silently mis-direct the worsening
        raise ValueError(f"side must be LONG (1) or SHORT (-1), got {side}")
    if not np.isfinite(entry_open) or entry_open <= 0:  # a 0/NaN open is corrupt data, not a coverage gap
        raise ValueError(f"entry_open must be a positive finite price, got {entry_open}")

    entry_fill = entry_open * (1.0 + side * entry_slippage)  # market entry: worse than the open against the side
    level = clean_exit_reference(entry_open, label)  # the engine's exit level price (the ONE place that formula lives)

    favorable = (side == LONG and label.exit_kind == EXIT_UPPER_BARRIER) or (
        side == SHORT and label.exit_kind == EXIT_LOWER_BARRIER
    )  # the limit barrier for this side (fills at the level, never worse)
    if favorable:  # a take-profit limit: fills AT the level (limit_miss_rate is 0, so no miss)
        exit_fill = level
    else:  # an adverse stop, OR a time-out / truncated market exit: worse than the level against the side
        exit_fill = level * (1.0 - side * exit_slippage)
    return float(entry_fill), float(exit_fill)


def _scalar_knowledge_epoch(value: "int | float | pd.Timestamp") -> int:
    """Normalize one event's knowledge_at scalar to int epoch seconds (Timestamp / datetime64 / int in).

    Mirrors ``features._scalar_epoch_seconds`` (each base/ module keeps its own normalizer): a NaT or
    a tz-naive Timestamp or a non-finite epoch is a producer bug and fails loud, never a silent
    garbage epoch (the DECISIONS #59 silent-cast class).
    """
    if isinstance(value, pd.Timestamp):  # itertuples / iterrows on a datetime64[UTC] column yields Timestamps
        if pd.isnull(value):  # NaT.value is INT64_MIN -> a finite garbage epoch; fail loud
            raise ValueError("knowledge_at scalar is NaT; timestamps must be non-null")
        if value.tz is None:  # a tz-naive Timestamp is ambiguous; the materializer emits UTC-aware
            raise ValueError(f"knowledge_at Timestamp is tz-naive; expected UTC-aware, got {value!r}")
        return int(value.value // 1_000_000_000)  # int64 ns since epoch (UTC) -> seconds
    if isinstance(value, np.datetime64):  # a numpy datetime64 scalar (treated as UTC)
        if np.isnat(value):  # a NaT would cast to a garbage epoch; fail loud
            raise ValueError(f"knowledge_at scalar is NaT (np.datetime64), got {value!r}")
        return int(value.astype("datetime64[s]").astype(np.int64))  # truncate to epoch seconds
    as_float = float(value)  # an int / float epoch-second scalar
    if not np.isfinite(as_float):  # a non-finite trigger time is a producer bug
        raise ValueError(f"knowledge_at scalar must be a finite epoch second, got {value!r}")
    return int(as_float)  # finite -> int epoch seconds


def _merge_coverage(stats: "dict", coverage: "dict[str, int]") -> "dict":
    """Fold the run's counted exclusions into the stats dict under ``coverage_*`` keys (§5).

    The coverage figure (membership-excluded, uncovered, cap-excluded, same-bar/collision drops)
    is how the run reports "counted, never silently dropped." These keys are NOT
    ``research_run_stats`` columns, so the finalize INSERT (which reads only its eight known
    columns) ignores them; they ride in the run's ``stats.json`` artifact for the report.
    """
    merged = dict(stats)  # copy; do not mutate the frozen result's dict
    for key, value in coverage.items():  # one counted figure at a time
        merged[f"coverage_{key}"] = int(value)  # namespaced so it cannot collide with a real stat key
    return merged


def _epoch_index(price_frame: "pd.DataFrame") -> np.ndarray:
    """Epoch-second positions of a per-symbol price frame's index (mirrors triple_barrier._epoch_seconds)."""
    idx = price_frame.index  # the per-symbol price frame is indexed by a tz-aware DatetimeIndex (or epoch ints)
    if isinstance(idx, pd.DatetimeIndex):  # the real provisioned price carries a tz-aware DatetimeIndex
        return idx.asi8 // 1_000_000_000  # int64 ns (UTC) -> seconds
    return np.asarray(idx, dtype=np.int64)  # already epoch seconds (a synthetic test frame)


def select_and_place(
    candidates: "list[EventCandidate]",  # surviving covered events (label is not None)
    event_prices: "dict[str, pd.DataFrame]",  # per-symbol OHLCV frames (for the entry open + the next-bar booking)
    sizer: Sizer,  # the position sizer (the §8 seam)
    max_concurrent: int,  # N: the causal concurrency cap (settings, value owed at J2)
    entry_slippage: float,  # the entry slippage rate (settings)
    exit_slippage: float,  # the exit slippage rate (settings)
) -> "tuple[list[PlacedTrade], dict[str, int]]":  # (placed trades, the counted-exclusion figures)
    """Apply selection, the concurrency cap, the same-(symbol,bar) rule, and the same-bar booking.

    Causal and deterministic: candidates are processed in ``(knowledge_at, event_id)`` order, so
    every admit/exclude decision depends only on events known by that time (no look-ahead), and
    the run fingerprint is reproducible. Every exclusion is COUNTED (never a silent drop), keyed
    by reason so the run reports an honest coverage breakdown (§5/§9).
    """
    if max_concurrent < 1:  # a non-positive cap admits nothing meaningful; a config error
        raise ValueError(f"max_concurrent (N) must be >= 1, got {max_concurrent}")
    ordered = sorted(candidates, key=lambda c: (c.knowledge_at, c.event_id))  # causal priority order
    placed: list[PlacedTrade] = []  # the admitted trades
    counts = {"cap_excluded": 0, "same_symbol_bar_excluded": 0, "same_bar_last_bar_dropped": 0}  # counted exclusions
    taken_cells: set[tuple[str, int]] = set()  # (symbol, booked epoch bar) already carrying an order (vbt: one/bar/col)

    for cand in ordered:  # one candidate at a time, in causal order
        sym, lab = cand.symbol, cand.label  # the symbol's price frame + brief E's label
        epochs = _epoch_index(event_prices[sym])  # the symbol's bar timestamps (epoch s)
        entry_pos = int(np.searchsorted(epochs, lab.entry_ts, side="left"))  # the entry bar position
        if entry_pos >= len(epochs) or int(epochs[entry_pos]) != int(lab.entry_ts):  # the entry bar must exist in the frame
            raise ValueError(f"event {cand.event_id}: entry_ts {lab.entry_ts} not found in {sym!r} price index")
        entry_open = float(event_prices[sym]["Open"].iloc[entry_pos])  # the un-slippaged entry reference price

        # The booked exit bar: the engine's exit_ts, EXCEPT a same-bar touch (exit_ts == entry_ts)
        # books on the NEXT bar (§7.1; from_orders holds one order per bar per column). The realized
        # FILL stays the barrier price; only the booking bar shifts (one-day MTM artifact).
        if lab.exit_ts == lab.entry_ts:  # same-bar touch: book the exit on the next available bar
            if entry_pos + 1 >= len(epochs):  # no next bar (the entry bar is the symbol's last) -> counted drop (§7.1)
                counts["same_bar_last_bar_dropped"] += 1
                continue
            booked_exit_ts = int(epochs[entry_pos + 1])  # the next bar carries the exit order
        else:
            booked_exit_ts = int(lab.exit_ts)  # the engine's exit bar

        entry_cell = (sym, int(lab.entry_ts))  # the entry order's (symbol, bar) cell
        exit_cell = (sym, booked_exit_ts)  # the exit order's cell
        if entry_cell in taken_cells or exit_cell in taken_cells:  # one order per (symbol, bar): the §9 collision rule
            counts["same_symbol_bar_excluded"] += 1  # count-and-exclude the colliding trade (deterministic, never silent)
            continue

        # O(len(placed)) scan per candidate -> O(candidates * placed) overall; negligible at M3.5 event
        # counts (< ~500). A high-frequency strategy would replace this with a sorted active-set (the
        # forward-looking optimization, like the brief-G purge note); flagged so it is not a silent O(n^2).
        concurrent = sum(  # admitted trades open AT this entry bar (entered at/before, exit after): the §8 cap test
            1 for p in placed if p.entry_ts <= lab.entry_ts < p.booked_exit_ts
        )
        if concurrent >= max_concurrent:  # the cap binds: no free slot at this entry time -> counted exclusion (§8)
            counts["cap_excluded"] += 1
            continue

        entry_fill, exit_fill = realized_fills(lab, cand.side, entry_open, entry_slippage, exit_slippage)  # the #59 fills
        shares = sizer.shares(entry_open, INIT_CASH)  # slippage-invariant share count (off the un-slippaged open)
        placed.append(PlacedTrade(  # admit the trade
            event_id=cand.event_id, symbol=sym, side=cand.side, shares=shares,
            entry_ts=int(lab.entry_ts), entry_fill=entry_fill, entry_reference=entry_open,  # entry reference = the un-slippaged open
            booked_exit_ts=booked_exit_ts, exit_fill=exit_fill, exit_reference=clean_exit_reference(entry_open, lab),  # exit reference from the ONE authority
        ))
        taken_cells.add(entry_cell)  # reserve both cells so a later trade cannot collide on them
        taken_cells.add(exit_cell)
    return placed, counts


def _daily_grid(placed: "list[PlacedTrade]", event_prices: "dict[str, pd.DataFrame]") -> "pd.DatetimeIndex":
    """The union of the traded symbols' bar timestamps spanning the placed trades (the MTM grid, §7).

    The union of the per-symbol TRADING days (not a calendar date_range), so vbt infers a
    business-daily frequency and annualizes the Sharpe correctly (Carver sqrt-time); bounded to
    the first entry .. last booked exit so the grid never spans the whole price superset.
    """
    symbols = {p.symbol for p in placed}  # only symbols that carry a position need a column
    lo = min(p.entry_ts for p in placed)  # first entry epoch
    hi = max(p.booked_exit_ts for p in placed)  # last booked-exit epoch
    stamps: set[int] = set()  # the union of in-range bar epochs across the traded symbols
    for sym in symbols:
        ep = _epoch_index(event_prices[sym])  # the symbol's bar epochs
        stamps.update(int(e) for e in ep if lo <= int(e) <= hi)  # keep bars within the span
    ordered = sorted(stamps)  # ascending epochs
    return pd.DatetimeIndex(pd.to_datetime(ordered, unit="s", utc=True))  # the tz-aware daily grid


def _event_fixed_fees(placed: "list[PlacedTrade]", grid: "pd.DatetimeIndex", symbols: "list[str]") -> "pd.DataFrame":
    """Alpaca's fee for every order of an event run, as a (grid by symbols) frame, NaN where no order (FEE1b, #459 item 4).

    The event path is ONE cash-shared account across its symbol columns, so Alpaca's daily rounding spans the columns: every entry
    and exit is listed as (grid day, side, shares, fill price) and ``daily_charged_fees`` is called once over the whole list.
    An entry is a buy for a long (side +1) and a sell for a short (-1); an exit is the opposite. Each order's share of its day's
    rounded fees lands in its own (bar, symbol) cell; ``select_and_place`` reserves one order per cell, so no cell is written twice.
    """
    from strategies.helpers.alpaca_fees import daily_charged_fees  # function-local, matching assemble_event_run's own imports

    entry_ix = {int(ts.value // 1_000_000_000): ts for ts in grid}  # epoch -> grid Timestamp for placement
    orders, cells = [], []  # the aligned (day, side, shares, price) list and the (Timestamp, symbol) cell of each
    for p in placed:
        entry_side, exit_side = ("buy", "sell") if p.side > 0 else ("sell", "buy")
        entry_day, exit_day = entry_ix[p.entry_ts], entry_ix[p.booked_exit_ts]
        orders.append((entry_day, entry_side, p.shares, p.entry_fill))
        cells.append((entry_day, p.symbol))
        orders.append((exit_day, exit_side, p.shares, p.exit_fill))
        cells.append((exit_day, p.symbol))
    if len(set(cells)) != len(cells):  # two orders in one (bar, symbol) cell would silently overwrite a fee
        raise ValueError("two orders land in one (bar, symbol) cell; select_and_place must reserve one order per cell")
    fixed_fees = pd.DataFrame(index=grid, columns=symbols, dtype=float)  # NaN = no order this bar/column
    for (day, symbol), fee in zip(cells, daily_charged_fees(orders)):
        fixed_fees.loc[day, symbol] = fee
    return fixed_fees


def assemble_event_run(
    placed: "list[PlacedTrade]",  # the admitted trades (from select_and_place)
    event_prices: "dict[str, pd.DataFrame]",  # per-symbol OHLCV frames (for the MTM close grid)
    settings: list,  # the run settings (fees read here; slippage already baked into the fills)
    regimes: "dict[str, list[tuple[int, int, str]]]",  # the §11 regimes slot (already built by the pipeline)
    window_start: int,  # epoch s; the run window start (for the empty-run regimes band)
    window_end: int,  # epoch s; the run window end
    coverage: "dict[str, int]",  # the counted exclusions (membership / uncovered / cap / collision), §5
) -> "RunResult":
    """Build the daily mark-to-market portfolio via ``from_orders`` and derive the six slots.

    No placed trade -> a clean empty six-slot RunResult (the empty-case helper). Otherwise build
    the union daily close grid (forward-filled so a held position holds its last mark across a
    missing bar), place the explicit entry/exit orders at our computed fill prices, run
    ``from_orders`` (cash-shared, NO native stops -> brief E stays the sole exit authority,
    ``slippage=0`` because we baked it), and pass the finished portfolio to ``build_run_result``.
    """
    from strategies.base.settings import get_setting  # function-local: keep this module's import surface honest

    if not placed:  # empty universe / zero events / all uncovered / zero survivors -> clean empty result
        return empty_run_result(window_start, window_end, regimes, coverage)

    grid = _daily_grid(placed, event_prices)  # the regular-enough daily MTM index
    symbols = sorted({p.symbol for p in placed})  # the portfolio columns

    # close: each symbol's close on the grid, forward-filled so a held position holds its last
    # mark across a halt / non-trading day; a leading gap (before the symbol's first bar) stays
    # NaN and carries no position, so it is never valued (§7 missing-close rule).
    close = pd.DataFrame(index=grid, columns=symbols, dtype=float)  # the MTM valuation frame
    for sym in symbols:
        pf_idx = pd.DatetimeIndex(pd.to_datetime(_epoch_index(event_prices[sym]), unit="s", utc=True))  # the symbol's bars
        s = pd.Series(event_prices[sym]["Close"].to_numpy(dtype=float), index=pf_idx)  # its close series
        close[sym] = s.reindex(grid).ffill()  # align to the grid + forward-fill (no bfill: that would look ahead)

    # size / price: one explicit order per (symbol, bar). Entry: +shares for a long, -shares for
    # a short, at our entry fill. Exit: the opposite, at our exit fill. size_type stays 'amount'
    # (shares), so vbt fills exactly at our computed price; the per-fill slippage cost is then read
    # back off the order records against the un-slippaged references (no shadow pass).
    size = pd.DataFrame(index=grid, columns=symbols, dtype=float)  # NaN = no order this bar/column
    price = pd.DataFrame(index=grid, columns=symbols, dtype=float)  # the explicit fill price per order
    entry_ix = {int(ts.value // 1_000_000_000): ts for ts in grid}  # epoch -> grid Timestamp for placement
    for p in placed:
        size.loc[entry_ix[p.entry_ts], p.symbol] = p.side * p.shares  # open the position
        price.loc[entry_ix[p.entry_ts], p.symbol] = p.entry_fill
        size.loc[entry_ix[p.booked_exit_ts], p.symbol] = -p.side * p.shares  # close it (opposite sign, same shares)
        price.loc[entry_ix[p.booked_exit_ts], p.symbol] = p.exit_fill

    fixed_fees = _event_fixed_fees(placed, grid, symbols)  # FEE1b: Alpaca's per-day, cash-shared fees, one cell per (bar, symbol) order
    fees = get_setting(settings, "fees", "default")  # commission fraction; the neutral key dispatch routed by asset class (M4 D2)
    pf = vbt.Portfolio.from_orders(  # the accountant: our prices, vbt keeps the books
        close=close,  # daily mark-to-market valuation
        size=size,  # explicit per-order share counts (NaN where no order)
        price=price,  # OUR fill prices (the #59 model); vbt never re-derives an exit
        size_type="amount",  # shares, so the price arg is the literal fill price
        fees=fees,  # commission from settings (a separate stat; the slippage measure counts only the per-fill shortfall)
        fixed_fees=fixed_fees,  # FEE1b: per-order Alpaca fee (NaN where no order, mirroring size/price)
        slippage=0.0,  # slippage is already baked into `price`; do NOT double-apply it
        init_cash=INIT_CASH,  # the locked starting cash
        cash_sharing=True,  # one shared account across symbols (a real multi-name equity)
        group_by=True,  # collapse all columns into one group -> one pf.value series
        direction="both",  # long and short both allowed; the order sign sets the direction
        # Annualization (BN-J1-3): the grid is a DAILY mark-to-market grid (one bar per trading day),
        # so the bar period IS 1 day and the year is the 252-trading-day convention (Carver's ~256
        # sqrt-time base, carver-systematic-trading-tibot.md:421-422). vbt cannot 'auto'-infer a freq
        # on a halt-gapped trading-day index (it raises "Index frequency is None"), and the default
        # year (365 calendar) would mis-annualize a trading-day series, so both are set explicitly:
        # ann_factor = year_freq / freq = 252.
        freq=EVENT_ANNUALIZATION_FREQ,  # the daily-grid bar period (a known cadence, not a tuned parameter)
        year_freq=EVENT_ANNUALIZATION_YEAR_FREQ,  # the trading-day-per-year annualization convention (Carver)
    )
    indicators: dict[str, "pd.Series"] = {}  # event runs supply no overlay series at M3.5 (slot stays empty)

    # The direct per-fill slippage cost (DECISIONS #66/#67): read the ACTUAL filled size and the
    # executed price off the one run's order records (SR2 — a partial fill contributes only its
    # filled portion), and join each order to the clean reference the fill model produced, keyed by
    # (symbol, bar-epoch). select_and_place reserves one order per (symbol, bar), so the key is
    # unique; the references come from the placed legs (the bar open / clean_exit_reference — the one
    # fill authority, SN-C4), never re-derived from vbt (which saw slippage=0 on this path).
    reference_by_cell: dict[tuple[str, int], float] = {}  # (symbol, bar-epoch) -> the un-slippaged reference
    for p in placed:  # each placed trade contributes an entry leg and an exit leg
        reference_by_cell[(p.symbol, p.entry_ts)] = p.entry_reference  # the entry order's reference (the bar open)
        reference_by_cell[(p.symbol, p.booked_exit_ts)] = p.exit_reference  # the exit order's reference (the level)
    orders = pf.orders.records_readable  # the order records (the same readable accessor vbt_extraction uses, :64/:72)
    order_symbols = orders["Column"].tolist()  # each order's symbol (the vbt column label)
    order_stamps = orders["Index"].tolist()  # each order's bar Timestamp
    order_sizes = orders["Size"].tolist()  # each order's FILLED size (SR2)
    order_prices = orders["Price"].tolist()  # each order's executed (post-slippage) price
    filled_sizes, executed_prices, reference_prices = [], [], []  # the three aligned per-fill sequences
    for symbol, stamp, size, executed in zip(order_symbols, order_stamps, order_sizes, order_prices):  # one order at a time
        bar_epoch = int(pd.Timestamp(stamp).value // 1_000_000_000)  # the order's bar -> epoch s (the placement keying)
        cell = (symbol, bar_epoch)  # the (symbol, bar) join key
        if cell not in reference_by_cell:  # a filled order with no placed leg means the (symbol, bar) keying drifted
            raise ValueError(f"slippage join: order at {cell!r} has no placed leg (symbol/bar keying drift)")  # legible, not a bare KeyError
        filled_sizes.append(size)  # the actual filled size
        executed_prices.append(executed)  # the executed price
        reference_prices.append(reference_by_cell[cell])  # the un-slippaged reference from the fill authority
    slippage_cost = per_fill_slippage_cost(filled_sizes, executed_prices, reference_prices)  # the one shared formula

    rr = build_run_result(pf, indicators=indicators, regimes=regimes, total_slippage_cost=slippage_cost)  # THE one extraction surface (EL61)
    from strategies.base.run_result import RunResult  # rebuild to fold coverage into stats (frozen dataclass)
    return RunResult(  # same six slots, stats augmented with the namespaced coverage figure (§5)
        equity=rr.equity, drawdowns=rr.drawdowns, trades=rr.trades,
        stats=_merge_coverage(rr.stats, coverage), indicators=rr.indicators, regimes=rr.regimes,
    )


def empty_run_result(
    window_start: int,  # epoch s; the run window start
    window_end: int,  # epoch s; the run window end
    regimes: "dict[str, list[tuple[int, int, str]]]",  # the rule-based regimes band for the window (§11)
    coverage: "dict[str, int] | None" = None,  # the counted exclusions, folded into stats (§5)
) -> "RunResult":
    """A clean empty six-slot RunResult for a no-trade run (empty universe / zero / all-uncovered).

    A two-bar flat equity at ``INIT_CASH`` on a tz-aware daily index (so ``equity.iloc[-1]`` and
    ``equity.index.asi8`` both work for the entrypoint's equity write, not a crash). The stats carry
    an EXPLICIT ``total_slippage_cost`` of 0.0 (no fills) and other no-trade values, never NaN: 0 for
    the NOT-NULL counts/sums
    and None for the nullable ratios a flat equity leaves undefined (so a NaN never reaches the
    research_run_stats write).
    """
    from strategies.base.run_result import RunResult  # function-local import (the locked output type)
    from strategies.base.vbt_columns import CANONICAL_DRAWDOWN_COLUMNS, CANONICAL_TRADE_COLUMNS  # the empty-frame schemas

    start = pd.Timestamp(window_start, unit="s", tz="UTC")  # the flat-equity index endpoints
    end = pd.Timestamp(max(window_end, window_start + 1), unit="s", tz="UTC")  # ensure end > start (>= 2 distinct bars)
    equity = pd.Series([float(INIT_CASH), float(INIT_CASH)], index=pd.DatetimeIndex([start, end]))  # flat, no trades
    trades = pd.DataFrame(columns=CANONICAL_TRADE_COLUMNS)  # empty, canonical 20-column shape
    drawdowns = pd.DataFrame(columns=CANONICAL_DRAWDOWN_COLUMNS)  # empty, canonical 9-column shape
    stats = {  # explicit no-trade values; None (not NaN) for the nullable ratios (researchdb.py:265-271)
        "total_return": 0.0,  # NOT NULL
        "sharpe_ratio": None,  # nullable: undefined with zero variance
        "max_drawdown": 0.0,  # no drawdown on a flat curve
        "num_trades": 0,  # NOT NULL
        "win_rate": None,  # nullable: undefined with zero trades
        "profit_factor": None,  # nullable: undefined with zero trades
        "total_fees_paid": 0.0,  # NOT NULL: no orders, no fees
        "total_slippage_cost": 0.0,  # NOT NULL: no fills -> zero shortfall (the entrypoint no longer back-fills it, DECISIONS #67)
    }
    stats = _merge_coverage(stats, coverage or {})  # fold the counted exclusions in (empty for a bare empty result)
    return RunResult(equity=equity, drawdowns=drawdowns, trades=trades, stats=stats, indicators={}, regimes=regimes)
