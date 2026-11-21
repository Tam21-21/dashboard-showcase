"""Canonical artifact column names and the vbt-readable → canonical rename maps.

vbt's ``records_readable`` returns title-case, space-separated labels
(``'Exit Trade Id'``) that can shift with the vbt version. We rename to fixed
snake_case ONCE, inside ``run()`` (via ``vbt_extraction``), so ``RunResult`` and
every downstream consumer (parquet on disk, RunExplorer, export bundles) speak a
single canonical vocabulary that does not depend on vbt internals.

Locked schemas: trades = M3 spec §3.8.1 (20 columns), drawdowns = §3.8.2 (9
columns). The vbt-readable label sets were verified against the pinned build
(vbt 2026.4.7) on 2026-05-21. This module lives in ``strategies/base/`` (not
``worker/lib/``) because the rename happens in strategy code; it is pure data
(no pandas, no vbt import).
"""

# --- Trades -----------------------------------------------------------------

# vbt's pf.trades.records_readable carries these 16 labels (verified 2026.4.7).
# Columns 17–20 (MAE/MFE) come from separate attributes and are joined in
# vbt_extraction, so they are NOT in this rename map — only in the column list.
VBT_TO_CANONICAL_TRADES: dict[str, str] = {
    "Exit Trade Id": "trade_id",  # vbt's per-trade id (keyed on the exit leg)
    "Column": "column",  # asset/column index within the portfolio
    "Size": "size",  # position size of the trade
    "Entry Order Id": "entry_order_id",  # order id that opened the trade
    "Entry Index": "entry_index",  # bar index of entry
    "Avg Entry Price": "avg_entry_price",  # average fill price on entry
    "Entry Fees": "entry_fees",  # fees paid on entry
    "Exit Order Id": "exit_order_id",  # order id that closed the trade
    "Exit Index": "exit_index",  # bar index of exit
    "Avg Exit Price": "avg_exit_price",  # average fill price on exit
    "Exit Fees": "exit_fees",  # fees paid on exit
    "PnL": "pnl",  # realised profit/loss in currency
    "Return": "return_pct",  # realised return as a fraction
    "Direction": "direction",  # 'long' / 'short'
    "Status": "status",  # 'open' / 'closed'
    "Position Id": "position_id",  # id of the position the trade belongs to
}

# The 20 canonical trade columns, in the exact §3.8.1 order. The last four are
# joined from pf.trades.mae / .mfe / .mae_returns / .mfe_returns in vbt_extraction.
CANONICAL_TRADE_COLUMNS: list[str] = [
    "trade_id",  # 1
    "column",  # 2
    "size",  # 3
    "entry_order_id",  # 4
    "entry_index",  # 5
    "avg_entry_price",  # 6
    "entry_fees",  # 7
    "exit_order_id",  # 8
    "exit_index",  # 9
    "avg_exit_price",  # 10
    "exit_fees",  # 11
    "pnl",  # 12
    "return_pct",  # 13
    "direction",  # 14
    "status",  # 15
    "position_id",  # 16
    "mae",  # 17 — joined from pf.trades.mae
    "mfe",  # 18 — joined from pf.trades.mfe
    "mae_return_pct",  # 19 — joined from pf.trades.mae_returns
    "mfe_return_pct",  # 20 — joined from pf.trades.mfe_returns
]

# --- Drawdowns --------------------------------------------------------------

# vbt's pf.drawdowns.records_readable carries these 9 labels (verified 2026.4.7).
# No merge step: the readable frame is already the full canonical content.
VBT_TO_CANONICAL_DRAWDOWNS: dict[str, str] = {
    "Drawdown Id": "drawdown_id",  # per-drawdown id
    "Column": "column",  # asset/column index
    "Start Index": "start_index",  # bar where the drawdown began
    "Valley Index": "valley_index",  # bar of the lowest point
    "End Index": "end_index",  # bar where it recovered (or last bar if active)
    "Start Value": "start_value",  # equity at the start (peak)
    "Valley Value": "valley_value",  # equity at the valley (trough)
    "End Value": "end_value",  # equity at the end
    "Status": "status",  # 'recovered' / 'active'
}

# The 9 canonical drawdown columns, in the exact §3.8.2 order.
CANONICAL_DRAWDOWN_COLUMNS: list[str] = [
    "drawdown_id",  # 1
    "column",  # 2
    "start_index",  # 3
    "valley_index",  # 4
    "end_index",  # 5
    "start_value",  # 6
    "valley_value",  # 7
    "end_value",  # 8
    "status",  # 9
]
