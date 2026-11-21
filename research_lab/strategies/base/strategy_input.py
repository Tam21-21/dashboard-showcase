"""The ``StrategyInput`` container: the locked input contract every strategy receives.

The mirror of ``RunResult`` on the input side, and engine-neutral for the same
reason: a strategy reads project-owned data, never a vbt data class. It carries the
OHLCV price frame plus three M3.5 event-run fields (``events``, ``features``,
``event_prices``); being a dataclass let M3.5 add those fields without breaking the
locked type or the existing price strategies (DECISIONS #12).

The convenience properties (``close`` etc.) wall off the on-disk column names so
strategies never string-index ``prices`` directly; if the data source ever
renames columns, only this file changes.
"""
# Lazy annotations so importing the contract does not require pandas at runtime.
from __future__ import annotations

from dataclasses import dataclass, field  # frozen container; field() gives each dict its own default
from typing import TYPE_CHECKING  # keep the pandas import type-check-only

if TYPE_CHECKING:  # readers / type checker only
    import pandas as pd  # pd.DataFrame / pd.Series used in annotations


def _empty_frame() -> "pd.DataFrame":
    """An empty DataFrame default for the additive ``fundamentals`` field, importing pandas LAZILY.

    Keeps the module import-light (importing the contract needs no pandas, per the module docstring):
    pandas loads only when a StrategyInput is actually constructed without ``fundamentals`` — and
    constructing one already needs pandas (``prices`` is a DataFrame), so this adds no new dependency.
    A plain ``default_factory=lambda: pd.DataFrame()`` would NameError, because ``pd`` is TYPE_CHECKING
    only.
    """
    import pandas as pd  # lazy: only on a default-constructed StrategyInput, never at module import

    return pd.DataFrame()


@dataclass(frozen=True)  # frozen: the input to a run is immutable
class StrategyInput:
    """Input container for one strategy run.

    A price-only run carries just ``prices`` (an OHLCV frame for one instrument, indexed by a
    tz-aware DatetimeIndex). An event run additionally fills the three M3.5 fields below (the
    event slices, the computed features, and the per-symbol price context); all three default
    empty so price strategies are unaffected.
    """

    prices: "pd.DataFrame"  # at least Open/High/Low/Close/Volume columns

    # M3.5 additive event-driven fields (EL48). All default empty so price strategies
    # (ma_cross) construct StrategyInput(prices=...) byte-unchanged; an event run fills
    # these three and leaves `prices` empty (it has N per-symbol frames, not one).
    events: "dict[str, pd.DataFrame]" = field(default_factory=dict)
    # event_type -> the materialized slice (the §3.2 columns incl. trade_time_symbol,
    # symbol_coverage, membership_valid). Empty for a price-only run.
    features: "dict[str, pd.DataFrame]" = field(default_factory=dict)
    # feature_name -> per-event feature frame. Empty until provide_features (brief F) fills it.
    event_prices: "dict[str, pd.DataFrame]" = field(default_factory=dict)
    # trade_time_symbol -> OHLCV frame: the per-symbol price context (2a §6 / 2b §2). The
    # engine reads event_prices[trade_time_symbol] over each event's window; the base
    # single-frame `prices` is unused for an event run. A third additive field (OPEN-7).
    window: "tuple[int, int] | None" = None
    # (knowledge_from, knowledge_to) epoch-second run window (BN-J1-2, brief J1). The entrypoint
    # fills it from run_spec.json for an event run; the pipeline uses it for the regimes-slot band
    # and the empty-run band (which has no events to derive a span from). None for a price run.
    fundamentals: "pd.DataFrame" = field(default_factory=_empty_frame)
    # The materialized fundamentals fact slice (M3.75 FUND-2 §6) for an event run, or empty. The
    # feature layer recomputes the as-of join over it (provide_fundamental_features, §8.2). A single
    # pd.DataFrame (one logical fact table the as-of helper does merge_asof over, §6.3), not a dict.
    # Empty for price-only and event-only runs, so those StrategyInput(...) calls are byte-unchanged.
    # A fifth additive M3.5/M3.75 field alongside events / features / event_prices / window (the
    # DECISIONS #12 additive-extension discipline; the locked run(data, settings) signature is untouched).

    @property
    def close(self) -> "pd.Series":  # closing price per bar
        return self.prices["Close"]  # title-case column name as written by M2 fetch

    @property
    def open(self) -> "pd.Series":  # opening price per bar (needed for next-open fills)
        return self.prices["Open"]

    @property
    def high(self) -> "pd.Series":  # bar high (used for faithful MFE/MAE)
        return self.prices["High"]

    @property
    def low(self) -> "pd.Series":  # bar low (used for faithful MFE/MAE)
        return self.prices["Low"]

    @property
    def volume(self) -> "pd.Series":  # bar volume
        return self.prices["Volume"]

    @property
    def factor(self) -> "pd.Series | None":  # lane D1: per-day adjustment factor (adjusted / as-traded)
        # Present on EODHD daily datasets fetched from lane D1 on (fetch.py writes it); None for data
        # with no as-traded price (Alpaca, Binance, hourly bars), which sizing reads as factor 1.
        return self.prices["Factor"] if "Factor" in self.prices.columns else None
