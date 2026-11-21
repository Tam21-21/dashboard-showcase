"""Shared synthetic-bundle fixture for the renderer tests.

Mirrors the K.5 local probe's _synthetic_bundle so the unit tests exercise the
same shape the probe asserts against. Kept here (not in conftest) so it's
explicit which tests import the bundle vs which run pure-function checks.
"""
from __future__ import annotations


def synthetic_bundle(*, optional_keys: bool = True) -> dict:
    """Build a synthetic bundle exercising every renderer code path."""
    base_ts = 1_704_067_200  # 2024-01-01 00:00 UTC
    bundle: dict = {
        "schema_version": 1,
        "run": {"id": 999, "job_id": 999, "name": "synthetic-probe-run",
                "status": "succeeded", "created_at": base_ts, "started_at": base_ts,
                "finished_at": base_ts + 3600, "strategy_id": 1, "strategy_version": 1,
                "dataset_id": 1, "dataset_sha256": "abc123def456", "error": None},
        "stats": {"total_return": 0.123, "sharpe_ratio": 0.42, "max_drawdown": 0.18,
                  "num_trades": 20, "win_rate": 0.55, "total_fees_paid": 8.45,
                  "total_slippage_cost": 12.34, "profit_factor": 1.15},
        "strategy": {"name": "synthetic-strategy", "version": 1,
                     "description": "## Rules\n\nA tiny strategy for probe purposes.\n"},
        "dataset": {"id": 1, "source": "binance", "symbol": "BTC/USDT",
                    "timeframe": "1h", "start_ts": base_ts,
                    "end_ts": base_ts + 100 * 3600, "sha256": "abc123def456"},
        "equity": [[base_ts + i * 3600, 10000.0 + i] for i in range(100)],
        "benchmark": [[base_ts + i * 3600, 10000.0 + i * 0.5] for i in range(100)],
        "drawdowns": [
            {"drawdown_id": i, "column": 0, "start_index": i * 10,
             "valley_index": i * 10 + 5, "end_index": i * 10 + 9,
             "start_value": 10100.0 + i, "valley_value": 10000.0 + i,
             "end_value": 10100.0 + i, "status": "recovered"}
            for i in range(5)
        ],
        "price": [
            {"timestamp": base_ts + i * 3600, "open": 100.0 + i, "high": 105.0 + i,
             "low": 95.0 + i, "close": 102.0 + i, "volume": 1000.0 + i}
            for i in range(5)
        ],
        "regimes": [],
        "environment": {"vbt_version": "2026.4.7+b1727ac6", "python_version": "3.12.13",
                        "build_provenance": "git:abc+vbt:b1727ac6", "hostname": "test"},
    }
    if optional_keys:
        bundle["strategy"]["source"] = "def run(): pass"
        bundle["settings_snapshot"] = [
            {"scope": "fees", "key": "crypto.default", "value": "0.001", "value_type": "number"},
            {"scope": "slippage", "key": "crypto.default", "value": "0.0005", "value_type": "number"},
        ]
        bundle["settings_drift"] = {"drifted": False, "changes": []}
        bundle["trades"] = [
            {"trade_id": i, "column": 0, "size": 0.1, "entry_order_id": i * 2,
             "entry_index": i * 5, "avg_entry_price": 100.0 + i, "entry_fees": 0.5,
             "exit_order_id": i * 2 + 1, "exit_index": i * 5 + 3,
             "avg_exit_price": 102.0 + i, "exit_fees": 0.5, "pnl": 0.5 * (i % 3 - 1),
             "return_pct": 0.005 * (i % 3 - 1), "direction": "long", "status": "closed",
             "position_id": i, "mae": -0.5, "mfe": 1.0,
             "mae_return_pct": -0.005, "mfe_return_pct": 0.01}
            for i in range(20)
        ]
        bundle["indicators"] = {
            "fast_sma": [[base_ts + i * 3600, 100.0 + i] for i in range(100)],
            "slow_sma": [[base_ts + i * 3600, 99.0 + i] for i in range(100)],
        }
        bundle["logs"] = ["log line 1", "log line 2"]
    return bundle
