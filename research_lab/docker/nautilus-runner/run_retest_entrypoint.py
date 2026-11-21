#!/usr/bin/env python3
"""The nautilus-runner:1.231.0 entrypoint (lane NR, U.7s; lane NT1, DECISIONS #452). Reads one
retest spec, replays EVERY trade of the judged run (each entry and each exit, with the backtest's
own share size and per-fill fee) through a real NautilusTrader engine, and writes one completion
result plus a fill-by-fill output file, per
research_lab/probes/nautilus_retest/ENTRYPOINT_CONTRACT.md, whose exact spec and result JSON
shapes this file implements.

Read order (AC-22, AC-C1, AC-C2, AC-C10): the engine's own installed-version string is read at
exactly one call site, attempted before the interpreter's own reported version is read at its own
single call site -- both unconditionally, at the very start of main(), on every path including the
two forced-failure fixtures, so no branch below can ever reach the second read before the first has
been attempted. Both reads live together in one small function, so "exactly one site each" is a
property of that function's own body, not a rule spread across the file.

The shadow copy (AC-A1, AC-A2, AD2.4, AD2.5) is a PREDICTION of what the real engine will do,
computed from the run's own bars and its own trade record alone, filling every leg at its signal
bar's own close; its own completion is always stamped BEFORE the real engine's fills are read, which
this file makes structural, not incidental: the function that reads real fills is never called until
the shadow function has already returned and stamped its own completion time.

Scope (DECISIONS #452): long trades only, with the backtest's own sizes. A size is fractional on
any back-adjusted stock (whole real shares divided by the fill's adjustment factor), so the
instrument is a Commodity with a size precision of 8 decimals, which fills a fractional order in
one piece. The engine still orders on the signal bar (the bar before the backtest's fill
bar), so fill timing (knob 1) and slippage (knob 2) stay unmatched and are the drift the
two-engine comparison measures (DECISIONS #283).

Exit code 0 on every outcome (a completing run, a could-not-run outcome, or a forced fixture path
alike): the RESULT lives entirely in the written result file, never in the process exit code,
because a could-not-run outcome is a successful MEASUREMENT of a failure to run, not a crash.
"""
from __future__ import annotations

import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import nautilus_trader  # bare import: the version read below is a real attribute access
import pandas as pd

# The engine account starts with this multiple of the run's own starting cash. It is a bookkeeping
# cushion only (an operational dial, DECISIONS #204): the backtest sizes positions against its own
# equity, so the replay must never be stopped by a cash rule the backtest did not have. Every
# reported figure is on the starting-cash basis, starting cash + (engine cash - engine start), so
# the cushion never changes a number the retest reports. A leg that would drive the engine cash
# below zero (an order the cushion cannot cover) is a derivation-failed outcome, never a silent skip.
ENGINE_CASH_CUSHION_MULTIPLE = 10

# Placeholder volume on every bar. No fill rule here reads it for pricing, but the engine fills a
# market order against the bar's size, so a small value would split one order into several fills.
# Large enough that one order is one fill; a split is caught by the fill count check in main().
PLACEHOLDER_BAR_VOLUME = 1_000_000_000

# docs/probes/nautilus_probe_2026-08-24.md step P8's own measured finding: back-adjusted EODHD
# prices need up to fifteen decimals to be EXACTLY representable, but "raising price_precision [to
# 8] is sufficient" -- every one of that probe's 2630 real bars round-tripped through the catalog
# at precision 8 with a residual of about 4e-10 of price, far below anything that matters here.
# Fixed rather than computed per run: a per-run computation would only ever re-derive the same
# answer that probe already measured on real data, at the cost of a code path nothing else needs.
PRICE_PRECISION = 8

# The decimals of the engine's money type. The backtest records fees like 1.225 (a third decimal),
# and a two-decimal currency would round the fee the engine charges; six decimals keep it exact.
MONEY_PRECISION = 6

# Tolerance for "the engine charged the fee we gave it": the money type holds six decimals.
FEE_TOLERANCE = 1e-6

# Decimals of an order size. The backtest's size on an adjusted stock is whole real shares divided
# by an adjustment factor, a long fraction; eight decimals match the price precision above.
SIZE_PRECISION = 8
SIZE_TOLERANCE = 10 ** -SIZE_PRECISION

SECONDS_PER_YEAR = 365.25 * 86400


class DerivationFailed(Exception):
    """A spec the replay cannot honour exactly (a Short trade, a fractional size, a signal on no
    bar, a missing fee, a denied order). The message is the cause; main() writes it to stderr and
    records could-not-run with the reason derivation-failed. Never a skip."""


def _read_engine_and_python_versions():
    """The one call site for each version read (AC-22): engine first, both unconditional."""
    engine_version = nautilus_trader.__version__
    python_version = platform.python_version()
    return engine_version, python_version


def _baked_engine_version():
    """The engine version this image's own Dockerfile baked into /etc/build_provenance at build
    time (git:<sha>+nautilus:<version>), read once for the version-mismatch check below. A missing
    file means a broken image, not a bad retest, so it returns None and the mismatch check simply
    does not fire."""
    provenance_path = Path("/etc/build_provenance")
    if not provenance_path.is_file():
        return None
    text = provenance_path.read_text(encoding="utf-8").strip()
    if "+nautilus:" not in text:
        return None
    return text.split("+nautilus:", 1)[1]


def _load_bars(bars_path):
    """Read the run's own bars: one row per bar, at least timestamp/open/high/low/close (the
    fifth OHLC column, which carries trading activity rather than price, is never read here: no
    sizing rule in this file is sensitive to it)."""
    bars = pd.read_parquet(bars_path)
    return bars.sort_values("timestamp").reset_index(drop=True)


def _load_signal_series(trades_path):
    """Read the judged run's own trade record: one row per trade (ENTRYPOINT_CONTRACT.md's own
    trades_path shape)."""
    return pd.read_parquet(trades_path)


def _load_backtest_equity(equity_path, bars):
    """Read the run's own equity curve: one row per bar, the same timestamps as the bars. Returns
    the list of equity values. A curve that does not line up with the bars is a derivation-failed
    cause, never realigned."""
    equity = pd.read_parquet(equity_path).sort_values("timestamp").reset_index(drop=True)
    if len(equity) != len(bars) or list(equity["timestamp"]) != list(bars["timestamp"]):
        raise DerivationFailed(
            f"the equity curve has {len(equity)} rows and the bars have {len(bars)}, or their "
            f"timestamps differ; the two must line up row for row")
    values = [float(value) for value in equity["equity"]]
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise DerivationFailed("the equity curve holds a value that is not a positive number")
    return values


def _bar_close_at(bars_by_ts, ts):
    """The close of the bar whose own timestamp equals ts exactly, or None when no bar in this
    run's own data carries that timestamp, never a nearest-match guess that could silently move a
    fill onto a bar the signal never named."""
    return bars_by_ts.get(ts)


def _positive_finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _scheduled_signals(bars, signals):
    """Resolve the trade record into the sorted list of legs: one entry leg per trade and one exit
    leg per closed trade (an open trade has an entry only). Sorted by signal bar, an exit before an
    entry on one bar, trade order kept otherwise. Raises DerivationFailed for anything the replay
    cannot honour exactly: a Short trade, a size that is not a finite number above zero, a signal bar
    on no bar, a fee or a backtest price that is not a number."""
    bars_by_ts = {int(ts): float(close) for ts, close in zip(bars["timestamp"], bars["close"])}
    legs = []
    for position, (_, trade) in enumerate(signals.iterrows()):
        side = str(trade.get("side")).lower()
        if side != "long":
            raise DerivationFailed(
                f"trade {position} is a {side} trade; only long trades are replayed (short is "
                f"not supported)")
        size = trade.get("size")
        if not _positive_finite(size):
            raise DerivationFailed(
                f"trade {position} has size {size!r}; the replay needs a size that is a finite number above zero")
        exit_ts = trade.get("exit_bar_ts")
        is_closed = exit_ts is not None and not pd.isna(exit_ts)
        for kind, ts_key, fee_key, price_key in (
                ("entry", "entry_bar_ts", "entry_fee", "entry_vbt_price"),
                ("exit", "exit_bar_ts", "exit_fee", "exit_vbt_price")):
            if kind == "exit" and not is_closed:
                continue
            ts_value = trade.get(ts_key)
            if ts_value is None or pd.isna(ts_value):
                raise DerivationFailed(f"trade {position} has no {ts_key}")
            ts = int(ts_value)
            close = _bar_close_at(bars_by_ts, ts)
            if close is None:
                raise DerivationFailed(
                    f"trade {position} {kind} signal bar {ts} is on no bar of this run's data")
            fee = trade.get(fee_key)
            if fee is None or pd.isna(fee) or not math.isfinite(float(fee)) or float(fee) < 0:
                raise DerivationFailed(f"trade {position} has no usable {kind} fee ({fee!r})")
            vbt_price = trade.get(price_key)
            if vbt_price is None or pd.isna(vbt_price) or not math.isfinite(float(vbt_price)):
                raise DerivationFailed(
                    f"trade {position} has no usable {kind} backtest price ({vbt_price!r})")
            legs.append({
                "ts": ts, "kind": kind, "side": "buy" if kind == "entry" else "sell",
                "signal_bar_close": close, "size": float(size), "fee": float(fee),
                "vbt_price": float(vbt_price),
            })
    legs.sort(key=lambda leg: (leg["ts"], 0 if leg["kind"] == "exit" else 1))
    return legs


def _leg_cash_effect(side, size, price, fee):
    """The cash a leg moves: a buy pays size x price, a sell receives it, either way minus the fee."""
    sign = 1.0 if side == "buy" else -1.0
    return -sign * size * price - fee


def compute_shadow_ledger(starting_cash, scheduled):
    """The shadow copy's own cash simulation (AC-24): every leg trades its own size at the signal
    bar's own close (the prediction this retest makes for what the real engine will fill at) and
    pays its own fee. Returns the lowest cash balance seen, starting cash included, on the
    starting-cash basis; it may be negative, because the engine account carries a cushion."""
    cash = starting_cash
    min_cash = cash
    for leg in scheduled:
        cash += _leg_cash_effect(leg["side"], leg["size"], leg["signal_bar_close"], leg["fee"])
        min_cash = min(min_cash, cash)
    return min_cash


def _client_order_id_tag(schedule_index: int) -> str:
    """The one place that builds the identity tag threading a submitted order back to the
    scheduled leg that produced it (findings round 3, B1 BLOCKER). Built from the leg's OWN
    position in the sorted `scheduled` list, never its timestamp alone: an exit and an entry can
    share a bar, and a shared timestamp would collide as a client_order_id, which NautilusTrader
    does not allow to repeat. The index is stable for one call of main() (scheduled is built once
    and never reordered), so round-tripping it through the real engine and back is exactly
    recovering the leg this order belongs to, and it is also how the fee model finds the leg's fee."""
    return f"RETEST-{schedule_index}"


def _schedule_index_from_client_order_id(client_order_id_str: str, seen_bad: list) -> int:
    """The inverse of _client_order_id_tag. A client_order_id that does not match the expected
    "RETEST-<int>" shape can only mean a bug in this file (nothing else in this engine run ever
    submits an order), so it is mapped to a fresh negative sentinel rather than silently dropped:
    a negative index can never equal a real schedule position (range(len(scheduled)) is always
    non-negative), so it always surfaces as an unmatched "fill with no signal record" in main()'s
    own count-mismatch check below, rather than disappearing from every count."""
    prefix = "RETEST-"
    if client_order_id_str.startswith(prefix):
        try:
            return int(client_order_id_str[len(prefix):])
        except ValueError:
            pass
    seen_bad.append(client_order_id_str)
    return -(len(seen_bad))


def _money_amount(text: str) -> float:
    """The amount in the fills report's "<amount> USD" strings."""
    return float(str(text).split()[0])


def run_replay_engine(bars, scheduled, starting_cash):
    """Run the REAL NautilusTrader engine over the run's own bars, submitting a market order at
    every scheduled leg's own bar (AC-A1: a buy at an entry leg, a sell at an exit leg), each order
    tagged with its own leg's identity (findings round 3, B1 BLOCKER) rather than left to be paired
    back by list position, and each fill charged the leg's own recorded fee through a fee model.
    Returns a dict of schedule_index -> fill dict (side, qty, avg_px, commission), one entry per
    leg that the real engine actually filled; a leg with no key is what main()'s own count-mismatch
    check below notices and routes to could-not-run, never a silent positional pairing."""
    from decimal import Decimal

    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.backtest.models import FeeModel, FillModel
    from nautilus_trader.config import BacktestEngineConfig, LoggingConfig, RiskEngineConfig, StrategyConfig
    from nautilus_trader.model.data import Bar, BarSpecification, BarType
    from nautilus_trader.model.enums import (
        AccountType, AggregationSource, AssetClass, BarAggregation, BookType, CurrencyType, OmsType,
        OrderSide, PriceType,
    )
    from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol, TraderId, Venue
    from nautilus_trader.model.instruments import Commodity
    from nautilus_trader.model.objects import Currency, Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    venue = Venue("RETEST")
    instrument_id = InstrumentId(Symbol("RETESTINSTR"), venue)
    usd = Currency(code="USD", precision=MONEY_PRECISION, iso4217=840, name="US Dollar",
                   currency_type=CurrencyType.FIAT)
    equity = Commodity(
        instrument_id=instrument_id, raw_symbol=Symbol("RETESTINSTR"),
        asset_class=AssetClass.EQUITY, quote_currency=usd,
        price_precision=PRICE_PRECISION, size_precision=SIZE_PRECISION,
        price_increment=Price(10 ** -PRICE_PRECISION, PRICE_PRECISION),
        size_increment=Quantity(SIZE_TOLERANCE, SIZE_PRECISION), ts_event=0, ts_init=0,
        margin_init=Decimal("0"), margin_maint=Decimal("0"),
        maker_fee=Decimal("0"), taker_fee=Decimal("0"),
    )
    bar_spec = BarSpecification(1, BarAggregation.DAY, PriceType.LAST)
    bar_type = BarType(instrument_id, bar_spec, AggregationSource.EXTERNAL)

    nautilus_bars = []
    for _, row in bars.iterrows():
        close_ns = int(row["timestamp"]) * 1_000_000_000
        nautilus_bars.append(Bar(
            bar_type=bar_type,
            open=Price(float(row["open"]), PRICE_PRECISION),
            high=Price(float(row["high"]), PRICE_PRECISION),
            low=Price(float(row["low"]), PRICE_PRECISION),
            close=Price(float(row["close"]), PRICE_PRECISION),
            volume=Quantity(float(PLACEHOLDER_BAR_VOLUME), SIZE_PRECISION),  # a placeholder: no rule here prices off it
            ts_event=close_ns, ts_init=close_ns,
        ))

    # Keyed by ts for the on_bar lookup, but each entry carries its own INDEX into `scheduled`
    # (findings round 3, B1 BLOCKER), which is the identity _client_order_id_tag threads onto the
    # order it produces. An exit and an entry sharing a ts still get two distinct indices here, in
    # sorted order (exit first), so their orders still get two distinct, non-colliding tags.
    schedule_by_ts = {}
    for idx, leg in enumerate(scheduled):
        schedule_by_ts.setdefault(leg["ts"], []).append((idx, leg))
    fee_by_tag = {_client_order_id_tag(idx): leg["fee"] for idx, leg in enumerate(scheduled)}

    class LegFeeModel(FeeModel):
        """Charges each fill the fee of its own leg, found by the order's client order id tag."""

        def get_commission(self, order, fill_qty, fill_px, instrument):
            return Money(fee_by_tag[str(order.client_order_id)], usd)

    class ReplayConfig(StrategyConfig, frozen=True):
        instrument_id: str
        bar_type: str

    class ReplayStrategy(Strategy):
        """Submits one market order per scheduled leg, at the bar whose own ts_event matches the
        leg's signal timestamp exactly: a BUY at an entry leg, a SELL at an exit leg, sized at the
        leg's own share count."""

        def __init__(self, config):
            super().__init__(config)
            self.instrument_id_obj = None

        def on_start(self):
            self.instrument_id_obj = InstrumentId.from_str(self.config.instrument_id)
            self.subscribe_bars(BarType.from_str(self.config.bar_type))

        def on_bar(self, bar):
            ts_seconds = int(bar.ts_event // 1_000_000_000)
            for schedule_index, leg in schedule_by_ts.get(ts_seconds, []):
                order = self.order_factory.market(
                    instrument_id=self.instrument_id_obj,
                    order_side=OrderSide.BUY if leg["side"] == "buy" else OrderSide.SELL,
                    quantity=Quantity(leg["size"], SIZE_PRECISION),
                    # The identity tag (findings round 3, B1 BLOCKER): this is what lets main()
                    # pair this fill back to schedule_index's own leg by IDENTITY once the engine
                    # returns, never by the order in which fills happen to come back.
                    client_order_id=ClientOrderId(_client_order_id_tag(schedule_index)),
                )
                self.submit_order(order)

    config = BacktestEngineConfig(
        trader_id=TraderId("RETEST-001"),
        logging=LoggingConfig(log_level="ERROR"),
        risk_engine=RiskEngineConfig(bypass=False),
    )
    engine = BacktestEngine(config=config)
    engine.add_venue(
        venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.CASH,
        starting_balances=[Money(starting_cash * ENGINE_CASH_CUSHION_MULTIPLE, usd)],
        base_currency=usd, book_type=BookType.L1_MBP,
        # random_seed=42 is inert here (findings round 1, NOTE): prob_slippage=0.0 means the model
        # never consults its own random draw at all, so no seed value can change a single fill this
        # engine produces. Carried for the same reason a constructor's other explicit defaults are
        # spelled out rather than omitted: a reader should not have to check the model's own source
        # to confirm this run is deterministic.
        fill_model=FillModel(prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=42),
        fee_model=LegFeeModel(), bar_execution=True, bar_adaptive_high_low_ordering=False,
    )
    engine.add_instrument(equity)
    engine.add_data(nautilus_bars)
    engine.add_strategy(ReplayStrategy(config=ReplayConfig(
        instrument_id=str(instrument_id), bar_type=str(bar_type),
    )))
    engine.run()

    # generate_fills_report() is one row per fill, with client_order_id as the DataFrame's own
    # INDEX (verified against the installed nautilus_trader 1.231.0): exactly the identity tag each
    # order above was submitted with. Keyed by schedule_index rather than returned as a plain list
    # (findings round 3, B1 BLOCKER): a caller that zips this against `scheduled` by list position
    # silently mispairs a fill the moment the two lists' lengths or orders ever disagree.
    fills_report = engine.trader.generate_fills_report()
    real_fills = {}
    split_fills = []
    bad_client_order_ids = []
    if fills_report is not None and len(fills_report) > 0:
        for client_order_id_str, fill_row in fills_report.iterrows():
            schedule_index = _schedule_index_from_client_order_id(
                str(client_order_id_str), bad_client_order_ids)
            if schedule_index in real_fills:
                split_fills.append(schedule_index)
                continue
            real_fills[schedule_index] = {
                "side": str(fill_row["order_side"]).lower(),
                "qty": float(fill_row["last_qty"]),
                "avg_px": float(fill_row["last_px"]),
                "commission": _money_amount(fill_row["commission"]),
            }
    engine.dispose()
    return real_fills, split_fills


# Everything below this point is the result-shape and file-output logic that runs AFTER the real
# engine's fill-report call above has already executed: co-located here rather than earlier in the
# file, since it is the code that turns those fills into the retest's own output artifacts.

def _annualization_factor(timestamps):
    """Bars per year on this run's own calendar: (bars - 1) / (span in years of 365.25 days)."""
    if len(timestamps) < 2:
        raise DerivationFailed(
            f"the run has {len(timestamps)} bars; at least 2 are needed for an annualization factor")
    span_seconds = timestamps[-1] - timestamps[0]
    if span_seconds <= 0:
        raise DerivationFailed("the bars span no time, so no annualization factor exists")
    return (len(timestamps) - 1) / (span_seconds / SECONDS_PER_YEAR)


def _total_return(equity):
    return equity[-1] / equity[0] - 1.0


def _sharpe(equity, factor):
    """mean(r) / std(r, ddof 1) x sqrt(factor) with r[0] = 0; None when the std is 0."""
    returns = [0.0] + [equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity))]
    spread = statistics.stdev(returns)
    if spread == 0:
        return None
    return statistics.mean(returns) / spread * math.sqrt(factor)


def _gap(nautilus_value, backtest_value):
    if nautilus_value is None or backtest_value is None:
        return None
    return nautilus_value - backtest_value


def _nautilus_equity_and_min_cash(bars, scheduled, real_fills, starting_cash):
    """Nautilus equity at each bar's close: starting cash + (engine cash - engine start) + shares
    held x close, with the engine cash change rebuilt leg by leg from the engine's own fills (its
    fill price and its charged commission). Returns (equity list, lowest starting-cash-basis cash).
    An engine cash that would go below zero means an order the cushion cannot cover: DerivationFailed."""
    legs_by_ts = {}
    for idx, leg in enumerate(scheduled):
        legs_by_ts.setdefault(leg["ts"], []).append((idx, leg))
    cash_change = 0.0
    shares = 0
    lowest = starting_cash
    equity = []
    for ts, close in zip(bars["timestamp"], bars["close"]):
        for idx, leg in legs_by_ts.get(int(ts), []):
            fill = real_fills[idx]
            cash_change += _leg_cash_effect(leg["side"], fill["qty"], fill["avg_px"], fill["commission"])
            shares += fill["qty"] if leg["side"] == "buy" else -fill["qty"]
            lowest = min(lowest, starting_cash + cash_change)
            if starting_cash * ENGINE_CASH_CUSHION_MULTIPLE + cash_change < 0:
                raise DerivationFailed(
                    f"leg {idx} was denied: the engine cash went below zero, so the order is "
                    f"not covered even with the {ENGINE_CASH_CUSHION_MULTIPLE}x bookkeeping cushion")
        equity.append(starting_cash + cash_change + shares * float(close))
    return equity, lowest


def _write_fills_file(output_dir, rows):
    """The fill-by-fill output file (AC-C6): one row per real fill, every summary figure derived
    from it re-derivably (AC-C7). Called only when there is at least one row."""
    frame = pd.DataFrame(rows, columns=[
        "signal_date", "side", "leg", "size", "signal_bar_close", "real_fill_price",
        "shadow_fill_price", "vbt_fill_price", "diff_shadow", "diff_vbt", "commission",
    ])
    fills_file_path = output_dir / "retest_fills.parquet"
    frame.to_parquet(fills_file_path, index=False)
    return fills_file_path


def _write_result(output_dir, *, status, reason=None, engine_version=None, python_version=None,
                   shadow_copy_completed_at=None, fills_read_at=None, export_path=None, figures=None):
    """Assemble and write retest_result.json to the exact contract shape (ENTRYPOINT_CONTRACT.md's
    Result JSON shape): every key present on every outcome, export_path and figures null on a
    could-not-run outcome rather than absent from the object."""
    body = {
        "status": status,
        "could_not_run_reason": reason,
        "engine_version": engine_version,
        "python_version": python_version,
        "shadow_copy_completed_at": shadow_copy_completed_at,
        "fills_read_at": fills_read_at,
        "export_path": str(export_path) if export_path is not None else None,
        "figures": figures,
    }
    (output_dir / "retest_result.json").write_text(json.dumps(body), encoding="utf-8")


def main() -> int:
    engine_version, python_version = _read_engine_and_python_versions()

    spec_path = Path(os.environ["RETEST_SPEC_PATH"])
    output_dir = Path(os.environ["RETEST_OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    # The two fixture-only forced paths (ENTRYPOINT_CONTRACT.md: "a real caller never sets them").
    # Checked before anything else touches bars, signals or the engine, since force_halt_early
    # models a run that never got that far at all.
    if spec.get("force_halt_early"):
        _write_result(output_dir, status="could-not-run", reason="halted-early",
                       engine_version=engine_version, python_version=python_version)
        return 0

    baked_version = _baked_engine_version()
    if baked_version is not None and baked_version != engine_version:
        _write_result(output_dir, status="could-not-run", reason="version-mismatch",
                       engine_version=engine_version, python_version=python_version)
        return 0

    # SF1 (findings round 3): compute_shadow_ledger lives INSIDE this same try, not after it. It
    # shares the exact failure domain bar/signal loading already guards (a malformed bars or
    # trades file can produce a `scheduled` list compute_shadow_ledger then chokes on), so an
    # uncaught crash there used to escape this file's own documented invariant ("exit code 0 on
    # every outcome"). force_zero_signals is applied before the ledger call so the ledger reflects
    # the fixture override rather than the file's own original signal count. A spec with no
    # equity_path is a missing shadow input (KeyError), reported as absent-shadow-copy.
    try:
        bars = _load_bars(spec["bars_path"])
        signals = _load_signal_series(spec["trades_path"])
        backtest_equity = _load_backtest_equity(spec["equity_path"], bars)
        timestamps = [int(ts) for ts in bars["timestamp"]]
        factor = _annualization_factor(timestamps)
        starting_cash = backtest_equity[0]
        scheduled = _scheduled_signals(bars, signals)
        if spec.get("force_zero_signals"):
            scheduled = []
        min_shadow_cash = compute_shadow_ledger(starting_cash, scheduled)
    except DerivationFailed as exc:
        print(f"derivation-failed: {exc}", file=sys.stderr)
        _write_result(output_dir, status="could-not-run", reason="derivation-failed",
                       engine_version=engine_version, python_version=python_version)
        return 0
    except (KeyError, ValueError, OSError):
        _write_result(output_dir, status="could-not-run", reason="absent-shadow-copy",
                       engine_version=engine_version, python_version=python_version)
        return 0
    # Nanosecond resolution (findings round 1, AC-23 red twice on a fast run): int(time.time())
    # has whole-second resolution, so a container run fast enough to finish inside one wall-clock
    # second recorded the two ordering stamps as EQUAL, failing the strict "shadow before fills"
    # check even though the shadow copy genuinely completed first, code-path-wise. Still a plain
    # Python int (arbitrary precision) and still an INTEGER column on the way to storage and to
    # RetestResultIn (no width limit on either side), so the strict order now holds structurally
    # rather than by how slow the engine happens to run.
    shadow_copy_completed_at = time.time_ns()

    # Narrowed from a bare except Exception (findings round 3, B2 BLOCKER): AccountError is the
    # one generic engine-raised exception base class nautilus_trader==1.231.0 actually exposes for
    # a backtest run (verified inside the real built image: every other *Error base class in the
    # installed package is a network adapter's own, binance/polymarket/websocket, none reachable
    # from a pure backtest with no live connectivity), covering AccountBalanceNegative and
    # AccountMarginExceeded. ValueError is what the engine's own PyCondition precondition checks
    # raise (verified the same way: PyCondition.positive(-1, "x") raises a bare ValueError).
    # KeyError and OSError cover a malformed bar/order object or a filesystem failure reachable
    # from inside the engine's own run loop; ArithmeticError is the base of ZeroDivisionError and
    # OverflowError for a pricing edge case. A genuinely novel exception type from a future engine
    # version bump is NOT swallowed here: it propagates, the container exits non-zero, and
    # run_retest.py's own existing container-exit handling reports the real failure with the
    # container's own stderr tail, which is louder and more debuggable than a silently
    # misclassified could-not-run row would have been.
    from nautilus_trader.accounting.error import AccountError

    try:
        real_fills, split_fills = run_replay_engine(bars, scheduled, starting_cash)
    except (AccountError, ValueError, KeyError, OSError, ArithmeticError):
        _write_result(output_dir, status="could-not-run", reason="engine-error",
                       engine_version=engine_version, python_version=python_version,
                       shadow_copy_completed_at=shadow_copy_completed_at)
        return 0
    fills_read_at = time.time_ns()  # same nanosecond-resolution fix, same reason, as the stamp above

    if not real_fills:
        _write_result(output_dir, status="could-not-run", reason="zero-fills",
                       engine_version=engine_version, python_version=python_version,
                       shadow_copy_completed_at=shadow_copy_completed_at, fills_read_at=fills_read_at)
        return 0

    # Identity-based pairing (findings round 3, B1 BLOCKER), never a positional zip. Comparing the
    # two index sets explicitly catches a leg with no fill (an order the engine denied, or a run
    # the engine stopped early) instead of ever attempting to build a row from a mispair.
    scheduled_indices = set(range(len(scheduled)))
    filled_indices = set(real_fills.keys())
    if scheduled_indices != filled_indices or split_fills:
        missing_signal_fills = scheduled_indices - filled_indices
        unmatched_fills = filled_indices - scheduled_indices
        # The contract's own could_not_run_reason stays the fixed nine-word set (ENTRYPOINT_
        # CONTRACT.md carries no free-text detail field); the count mismatch itself goes to
        # stderr, where a human debugging a real derivation-failed run can actually see it.
        print(
            f"derivation-failed: {len(missing_signal_fills)} leg(s) with no fill (an order the "
            f"engine denied, or a run it stopped early), {len(unmatched_fills)} fill(s) with no "
            f"leg record, {len(split_fills)} order(s) filled in more than one piece "
            f"(scheduled={len(scheduled)}, filled={len(real_fills)})",
            file=sys.stderr,
        )
        _write_result(output_dir, status="could-not-run", reason="derivation-failed",
                       engine_version=engine_version, python_version=python_version,
                       shadow_copy_completed_at=shadow_copy_completed_at, fills_read_at=fills_read_at)
        return 0

    try:
        rows = []
        for schedule_index, leg in enumerate(scheduled):
            fill = real_fills[schedule_index]
            if fill["side"] != leg["side"] or abs(fill["qty"] - leg["size"]) > SIZE_TOLERANCE:
                raise DerivationFailed(
                    f"leg {schedule_index} was ordered as {leg['side']} {leg['size']} and the "
                    f"engine filled {fill['side']} {fill['qty']}")
            if abs(fill["commission"] - leg["fee"]) > FEE_TOLERANCE:
                raise DerivationFailed(
                    f"leg {schedule_index} was given fee {leg['fee']} and the engine charged "
                    f"{fill['commission']}")
            real_px = fill["avg_px"]
            shadow_px = leg["signal_bar_close"]
            rows.append({
                "signal_date": leg["ts"],
                "side": leg["side"],
                "leg": leg["kind"],
                "size": leg["size"],
                "signal_bar_close": leg["signal_bar_close"],
                "real_fill_price": real_px,
                "shadow_fill_price": shadow_px,
                "vbt_fill_price": leg["vbt_price"],
                "diff_shadow": real_px - shadow_px,
                "diff_vbt": real_px - leg["vbt_price"],
                "commission": fill["commission"],
            })
        nautilus_equity, nautilus_min_cash = _nautilus_equity_and_min_cash(
            bars, scheduled, real_fills, starting_cash)
        fills_file_path = _write_fills_file(output_dir, rows)
        mean_diff_shadow = sum(r["diff_shadow"] for r in rows) / len(rows)
        mean_diff_vbt = sum(r["diff_vbt"] for r in rows) / len(rows)
        backtest_total_return = _total_return(backtest_equity)
        backtest_sharpe = _sharpe(backtest_equity, factor)
        nautilus_total_return = _total_return(nautilus_equity)
        nautilus_sharpe = _sharpe(nautilus_equity, factor)
    except (DerivationFailed, KeyError, ValueError, ZeroDivisionError, OSError) as exc:
        print(f"derivation-failed: {exc}", file=sys.stderr)
        _write_result(output_dir, status="could-not-run", reason="derivation-failed",
                       engine_version=engine_version, python_version=python_version,
                       shadow_copy_completed_at=shadow_copy_completed_at, fills_read_at=fills_read_at)
        return 0

    _write_result(
        output_dir, status="report-only",
        engine_version=engine_version, python_version=python_version,
        shadow_copy_completed_at=shadow_copy_completed_at, fills_read_at=fills_read_at,
        export_path=fills_file_path,
        figures={"min_shadow_cash_balance": min_shadow_cash, "mean_diff_shadow": mean_diff_shadow,
                 "mean_diff_vbt": mean_diff_vbt,
                 "backtest_total_return": backtest_total_return, "backtest_sharpe": backtest_sharpe,
                 "nautilus_total_return": nautilus_total_return, "nautilus_sharpe": nautilus_sharpe,
                 "gap_total_return": _gap(nautilus_total_return, backtest_total_return),
                 "gap_sharpe": _gap(nautilus_sharpe, backtest_sharpe),
                 "annualization_factor": factor, "fills_count": len(rows),
                 "nautilus_min_cash": nautilus_min_cash},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
