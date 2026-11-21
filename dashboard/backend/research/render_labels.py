"""Render labels, formatters, and metric explanations for K.5 (M3 spec §5.3, L28).

The single source of truth for translating canonical snake_case field names into
human-readable labels, formatting numbers per L28's kind-specific rules, and
emitting one-line plain-English explanations of each metric. The JSON renderer
never touches this module (machine output keeps snake_case); the MD and HTML
renderers consume LABELS, format_number, and METRIC_EXPLANATIONS to build the
human-facing surface.

Every name that appears as a *renderer-generated label* (table headers, stat
panel rows) must have a LABELS entry; the K.5 local probe asserts coverage
against the three canonical column sets (stats / trades / drawdowns).
"""
from __future__ import annotations  # postponed evaluation for type hints

import datetime as _dt  # only for timestamp formatting
from typing import Literal  # for the format_number kind argument

# Note: render_labels is consumed by render_md and render_html (and by the K.5
# probes for coverage assertions). It does NOT itself iterate the canonical
# trade/drawdown column lists from strategies.base.vbt_columns — those are
# referenced only by the probe layer, which manages its own sys.path (the
# server dashboard process runs from /home/newsbot/trading and does not have
# research_lab on its path). Importing them here would break server startup
# and would be unused code locally. Keep this module dependency-free.

# Stats keys exposed in the bundle's "stats" slot (mirrors
# dashboard/backend/research/export.py:_STATS_KEYS — kept in sync by hand).
STATS_KEYS: tuple[str, ...] = (
    "total_return",  # fraction (e.g. 0.266 -> 26.6%)
    "sharpe_ratio",  # ratio
    "max_drawdown",  # fraction
    "num_trades",  # integer count
    "win_rate",  # fraction
    "total_fees_paid",  # currency
    "total_slippage_cost",  # currency
    "profit_factor",  # ratio
)


# --- Labels -----------------------------------------------------------------

# Renderer-generated labels for stats, trades, drawdowns, dataset, environment,
# and the run-meta block. Keep entries sorted alphabetically within each section
# to make missing-key audits cheap (the K.5 probe diffs against three canonical
# column sets and prints the missing names).
LABELS: dict[str, str] = {
    # Stats (§3.5) — surfaced in MD/HTML summary table.
    "max_drawdown": "Maximum drawdown",
    "num_trades": "Number of trades",
    "profit_factor": "Profit factor",
    "sharpe_ratio": "Sharpe ratio",
    "total_fees_paid": "Total fees paid",
    "total_return": "Total return",
    "total_slippage_cost": "Total slippage cost",
    "win_rate": "Win rate",
    # Trades (§3.8.1) — surfaced as MD table column headers.
    "avg_entry_price": "Average entry price",
    "avg_exit_price": "Average exit price",
    "column": "Column",
    "direction": "Direction",
    "entry_fees": "Entry fees",
    "entry_index": "Entry bar",
    "entry_order_id": "Entry order id",
    "exit_fees": "Exit fees",
    "exit_index": "Exit bar",
    "exit_order_id": "Exit order id",
    "mae": "Maximum adverse excursion",
    "mae_return_pct": "Maximum adverse excursion (return)",
    "mfe": "Maximum favourable excursion",
    "mfe_return_pct": "Maximum favourable excursion (return)",
    "pnl": "Profit / loss",
    "position_id": "Position id",
    "return_pct": "Return",
    "size": "Position size",
    "status": "Status",
    "trade_id": "Trade id",
    # Drawdowns (§3.8.2) — overlap on "column" / "status" with trades is fine,
    # same human label fits both contexts.
    "drawdown_id": "Drawdown id",
    "end_index": "End bar",
    "end_value": "End value",
    "start_index": "Start bar",
    "start_value": "Start value",
    "valley_index": "Valley bar",
    "valley_value": "Valley value",
    # Run + dataset + environment metadata — surfaced in MD/HTML headers.
    "build_provenance": "Build provenance",
    # DBG-4 part B: the run's sizing and cost provenance, shown in the Run environment section.
    "cost_model": "Cost model",
    "sizing_method": "Sizing method",
    "sizing_spec": "Sizing settings",
    "created_at": "Created",
    "dataset_sha256": "Dataset sha256",
    "finished_at": "Finished",
    "hostname": "Hostname",
    "python_version": "Python version",
    "source": "Source",
    "started_at": "Started",
    "symbol": "Symbol",
    "timeframe": "Timeframe",
    "vbt_version": "vbt version",
    # Lane WB (6.2c, AC-3): the walk-forward slices table (render_md.py / render_html.py). A
    # separate small block rather than folding into the trades/drawdowns section above: these
    # column names belong to research_walkforward_slices, a different table with a different
    # grain, and mixing them into the trade/drawdown block would make an audit of "does every
    # trades column have a label" harder to read.
    "slice_index": "Slice",
    "in_sample_start_ts": "In-sample start",
    "in_sample_end_ts": "In-sample end",
    "out_of_sample_start_ts": "Out-of-sample start",
    "out_of_sample_end_ts": "Out-of-sample end",
    "in_sample_profit": "In-sample profit",
    "out_of_sample_profit": "Out-of-sample profit",
    "trade_count": "Trades",
    "param_config_hash": "Parameter combo",
    "wfa_boundary_buffer_bars": "Boundary buffer bars",
    # M2.5 brief F (§7.4) — event-store quality-report labels. Distinct namespace
    # from the M3 K.5 stats / trades / drawdowns set; collisions across labels
    # are intentional where the human label fits both contexts (e.g. "source",
    # "status", "created_at" carry the same meaning whether the row is a price
    # run or an event batch).
    #
    # report shapes
    "record_level": "Quality report",
    "batch_level_failure": "Batch failure",
    "correction_summary": "Correction",
    "rollback_summary": "Rollback",
    "cancelled_partial_stub": "Cancelled batch, partial state",
    # totals
    "records_seen": "Records seen",
    "events_written": "Events written",
    "dedup_hits": "Duplicate records skipped",
    "dedup_collisions_with_changed_payload": "Re-fetched records whose payload differs from stored",
    "quarantines": "Records quarantined",
    "provisional_fields_populated": "Rows touching provisional payload fields",
    # batch row fields
    "batch_id": "Batch id",
    "kind": "Kind",
    "event_type": "Event type",
    "event_count": "Events written",  # mirrors totals.events_written
    "quarantine_count": "Records quarantined",
    "content_hash": "Content hash (SHA-256)",
    "policy": "Failure policy",
    "source_descriptor": "Source descriptor",
    "request_id": "Request id",
    "parent_batch_id": "Parent batch id",
    "realized_start_at": "Earliest event time",
    "realized_end_at": "Latest event time",
    "last_heartbeat_at": "Last heartbeat",
    "failure_class": "Failure class",
    "failure_context": "Failure context",
    "underlying_class": "Underlying class",
    "record_index_in_batch": "Record index in batch",
    "natural_key": "Natural key",
    "failure_message": "Failure message",
    "raw_input_excerpt": "Raw input excerpt",
    "rolled_back_event_count": "Events rolled back",
    "swept_at": "Swept at",
    "swept_by": "Swept by",
    # failure step + class labels (failure_step enum)
    "failure_step.parse": "Parse failure",
    "failure_step.validate": "Validation failure",
    # batch-level + per-record failure classes
    "EdgarHttp5xx": "EDGAR server error",
    "EdgarHttp429Persistent": "EDGAR rate limit, persistent",
    "EdgarNetworkTimeout": "EDGAR network timeout",
    "EdgarParseFatal": "EDGAR returned an unexpected response",
    "OrphanedRunningBatch": "Batch swept by reaper (no recent heartbeat)",
    "Cancelled": "Batch cancelled",
    "RecordValidationAbort": "Record validation failure aborted the batch",
    "LoaderFileUnreadable": "Canonical file unreadable (whole-file structural failure)",
    "LoaderEnvelopeInvalid": "Canonical-file line envelope invalid",
    "LoaderJsonDecodeError": "Canonical-file line was not valid JSON",
    "TickerNotFoundInBridge": "Ticker not found in CIK bridge",
    "MissingRequiredField": "Required field missing",
    "WrongFieldType": "Field type mismatch",
    "UnknownField": "Unknown field in payload",
    "InvalidEnumValue": "Invalid enum value",
    # correction + rollback summary fields
    "event_id": "Event id",
    "prev_version": "Prior version",
    "new_version": "New version",
    "trigger_note": "Operator trigger note",
    "payload_diff_keys": "Payload fields changed",
    "target_batch_id": "Target batch id",
    # status enum values for the batch row
    "succeeded": "Succeeded",
    "succeeded_with_quarantines": "Succeeded with quarantines",
    "failed": "Failed",
    "rolled_back": "Rolled back",
    "cancelled_partial": "Cancelled (partial)",
    "running": "Running",
    # finished_at — overlaps M3 K.5 "Finished" (already in this dict)
}


# --- Format number ----------------------------------------------------------

# Mapping from stats-key name -> the kind format_number expects. The MD and HTML
# renderers iterate STATS_KEYS, look up the label and the formatter kind, and
# render one row per metric.
STATS_FORMAT: dict[str, Literal["percent", "currency", "ratio", "count"]] = {
    "total_return": "percent",  # fraction shown as XX.X%
    "sharpe_ratio": "ratio",  # two-decimal ratio
    "max_drawdown": "percent",  # fraction shown as XX.X%
    "num_trades": "count",  # integer with thousands sep
    "win_rate": "percent",  # fraction shown as XX.X%
    "total_fees_paid": "currency",  # $X.XX with thousands sep
    "total_slippage_cost": "currency",  # $X.XX with thousands sep
    "profit_factor": "ratio",  # two-decimal ratio
}


def format_number(
    value: float | int | None,
    kind: Literal["percent", "currency", "ratio", "count", "timestamp"],
) -> str:
    """Format a number per the L28 kind-specific rule.

    Returns "—" for None (so missing nullable metrics render as a placeholder
    rather than a broken string). Locked rules from L28:
        percent   -> "18.3%"  (one decimal; fraction in, percent out)
        currency  -> "$10,000.00" (always two decimals; thousands separators;
                                   L28 says "$X.XX" always — wins over arch §21's
                                   inconsistent "$10,000" example)
        ratio     -> "0.42" (two decimals, no separators)
        count     -> "8,784" (integer with thousands separators)
        timestamp -> "2024-01-01 00:00 UTC" (UNIX seconds in, UTC out)
    """
    # Honest "no value" placeholder rather than "0" or "nan" or "None".
    if value is None:
        return "—"
    # Each kind has one strict format; no flexibility-for-flexibility's-sake.
    if kind == "percent":
        return f"{value * 100:.1f}%"  # fraction -> single decimal percent
    if kind == "currency":
        return f"${value:,.2f}"  # always two decimals + thousands separators
    if kind == "ratio":
        return f"{value:.2f}"  # two decimals, no symbol
    if kind == "count":
        return f"{int(value):,}"  # integer cast guards against accidental floats
    if kind == "timestamp":
        return _dt.datetime.fromtimestamp(int(value), _dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    # Defensive: an unknown kind is a programming bug, not user error.
    raise ValueError(f"unknown format kind: {kind!r}")


# --- Metric explanations ----------------------------------------------------

# One-line plain-English explanation per stats metric. HTML renders as tooltip;
# MD renders as inline glossary footnote on first appearance (L28).
METRIC_EXPLANATIONS: dict[str, str] = {
    "total_return": "The strategy's total profit or loss over the run, as a percentage of the starting cash.",
    "sharpe_ratio": "Return per unit of risk. Higher is better; 1.0 is decent, 2.0 is excellent.",
    "max_drawdown": "The worst peak-to-trough decline in equity during the run.",
    "num_trades": "Total number of completed round-trip trades.",
    "win_rate": "Fraction of trades that closed profitably.",
    "total_fees_paid": "Sum of trading fees (commissions) across every trade.",
    "total_slippage_cost": "What price slippage cost the strategy, totalled across every fill: for each fill, the gap between the price it got and the clean price with no slippage, times the size filled.",
    "profit_factor": "Sum of winning trades divided by sum of losing trades. Above 1.0 is profitable.",
}
