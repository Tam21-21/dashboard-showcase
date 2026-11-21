"""Markdown renderer for K.5 (M3 spec §5.2 / L23 / L28; arch §21).

Hand-rolled MD output. Summary-only per L23 (top-N rows in trades and
drawdowns, full settings rows, full description), keeping the file size
small enough to paste into chat or include in a doc. No third-party MD
library — the output is plain markdown that any MD viewer renders.

L28 style rules applied to renderer-generated labels only (table headers,
metric names): no snake_case in those. Body content (the strategy
description, settings keys, trade cells) is passed through verbatim — a
docstring or a stored setting key is the author's content, not a renderer
label, and the L28 rule does not target it.

Layout (one H1, then H2 sections):
    # <strategy name> v<version> — Run #<run_id>
    <description, raw markdown passthrough>
    ## Summary             stats table (8 metrics)
    ## Dataset             dataset card
    ## Run environment     vbt/python/build/host card
    ## Trades (first 20)   trade table
    ## Drawdowns (worst 10 by depth)
    ## Settings at run time
    <plain-text footer pointing at JSON/HTML for full data>
"""
from __future__ import annotations  # postponed type-hint evaluation

import json  # DBG-4: sizing_spec and cost_model are rendered as sorted JSON text

from dashboard.backend.research.render_labels import (
    LABELS,  # snake_case -> human label
    METRIC_EXPLANATIONS,  # one-line plain-English per metric
    STATS_FORMAT,  # stats key -> format_number kind
    STATS_KEYS,  # canonical stats order
    format_number,  # the kind-aware formatter
)

# Trade-table columns shown in MD (subset of CANONICAL_TRADE_COLUMNS; the full
# 20 is too wide to fit a markdown table comfortably). Audience is humans
# skimming — the high-leverage columns are direction, prices, P&L, MAE/MFE.
_TRADE_TABLE_COLS: tuple[str, ...] = (
    "trade_id",
    "direction",
    "entry_index",
    "avg_entry_price",
    "exit_index",
    "avg_exit_price",
    "pnl",
    "return_pct",
    "mae",
    "mfe",
)
# Drawdown-table columns shown in MD (subset of CANONICAL_DRAWDOWN_COLUMNS;
# valley_index + start/end values are the high-leverage ones).
_DRAWDOWN_TABLE_COLS: tuple[str, ...] = (
    "drawdown_id",
    "start_index",
    "valley_index",
    "end_index",
    "start_value",
    "valley_value",
    "end_value",
    "status",
)
# Cap rows in the two tables. L23 says MD is summary-only, not a data archive.
_TRADES_TOP_N = 20  # show the first 20 trades; reader gets the rest from JSON/HTML
_DRAWDOWNS_TOP_N = 10  # show the 10 worst drawdowns by depth

# Lane WB (6.2c, AC-3): the walk-forward slices table columns, in the order research_walkforward_
# slices' own DDL declares them (skipping param_config_hash's raw sha to keep the table narrow; a
# reader who wants the exact combo has the JSON export).
_WALKFORWARD_SLICE_TABLE_COLS: tuple[str, ...] = (
    "slice_index",
    "in_sample_start_ts",
    "in_sample_end_ts",
    "out_of_sample_start_ts",
    "out_of_sample_end_ts",
    "in_sample_profit",
    "out_of_sample_profit",
    "trade_count",
)


def render_md(bundle: dict) -> bytes:
    """Build the MD export for a run bundle. Returns UTF-8 bytes."""
    # Use a list of lines and join at the end — fewer string allocations than
    # building one growing string, and trivial to keep readable.
    out: list[str] = []
    out.append(_h1(bundle))  # title with strategy name + version + run id
    out.append("")  # blank line after H1
    desc = (bundle.get("strategy") or {}).get("description")
    if desc:  # description is markdown; embed verbatim so MD viewers render it
        out.append(desc.rstrip())
        out.append("")
    out.extend(_section_summary(bundle))  # ## Summary — stats table
    out.extend(_section_walkforward_slices(bundle))  # ## Walk-forward slices (lane WB, 6.2c, AC-3); [] for every other run
    out.extend(_section_dataset(bundle))  # ## Dataset
    out.extend(_section_environment(bundle))  # ## Run environment
    out.extend(_section_trades(bundle))  # ## Trades (first N)
    out.extend(_section_drawdowns(bundle))  # ## Drawdowns (worst N)
    out.extend(_section_settings(bundle))  # ## Settings at run time
    out.extend(_footer(bundle.get("_links")))  # K.6: links appended when present
    return ("\n".join(out) + "\n").encode("utf-8")


# --- Section builders -------------------------------------------------------


def _h1(bundle: dict) -> str:
    """The top-level header: '# <strategy> v<version> — Run #<run_id>'."""
    strat = bundle.get("strategy") or {}
    run = bundle.get("run") or {}
    name = strat.get("name") or "Unknown strategy"  # honest fallback if bundle malformed
    version = strat.get("version")
    run_id = run.get("id")
    parts = [f"# {name}"]
    if version is not None:
        parts.append(f"v{version}")  # version number after the name
    if run_id is not None:
        parts.append(f"— Run #{run_id}")  # run identity
    return " ".join(parts)


def _section_summary(bundle: dict) -> list[str]:
    """## Summary — 2-column stats table with formatted values and a glossary footnote."""
    stats = bundle.get("stats") or {}
    lines: list[str] = ["## Summary", ""]
    # Two-column table: metric label + formatted value. Header row uses canonical
    # human labels (passes the K.5 snake_case probe).
    lines.append("| Metric | Value |")  # MD table header row
    lines.append("|---|---|")  # separator that turns the prior row into a header
    for key in STATS_KEYS:  # iterate in canonical order, not stats.keys()
        label = LABELS.get(key, key)  # missing label -> fall back to raw key (caught by K5L-7)
        value = stats.get(key)
        kind = STATS_FORMAT.get(key, "ratio")  # ratio is the safest default for unknown
        lines.append(f"| {label} | {format_number(value, kind)} |")
    lines.append("")
    # Glossary footnote section per L28 ("inline glossary footnote on first
    # appearance"). One block, all explanations at once, keeps MD scannable.
    lines.append("### Metric explanations")
    lines.append("")
    for key in STATS_KEYS:
        explanation = METRIC_EXPLANATIONS.get(key)
        if explanation:
            lines.append(f"- **{LABELS.get(key, key)}** — {explanation}")
    lines.append("")
    return lines


def _section_dataset(bundle: dict) -> list[str]:
    """## Dataset — source / symbol / timeframe / time range / checksum."""
    dataset = bundle.get("dataset") or {}
    lines: list[str] = ["## Dataset", ""]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    # Iterate explicit fields rather than dataset.items() — the K.5 snake_case
    # probe checks header rows, and the label column must be human-readable.
    fields: tuple[tuple[str, str, str], ...] = (
        ("source", "Source", "text"),
        ("symbol", "Symbol", "text"),
        ("timeframe", "Timeframe", "text"),
        ("start_ts", "Start", "timestamp"),
        ("end_ts", "End", "timestamp"),
        ("sha256", "Dataset sha256", "sha"),
    )
    for key, label, kind in fields:
        value = dataset.get(key)
        if kind == "timestamp" and value is not None:
            rendered = format_number(value, "timestamp")
        elif kind == "sha" and value:
            rendered = f"`{value[:12]}…`"  # truncate sha to first 12 hex chars for readability
        else:
            rendered = "—" if value is None else str(value)
        lines.append(f"| {label} | {rendered} |")
    lines.append("")
    return lines


def _section_walkforward_slices(bundle: dict) -> list[str]:
    """## Walk-forward slices -- lane WB (6.2c), AC-3. Rendered ONLY when the bundle carries a
    "walkforward" block (an ordinary run's bundle has none, so this returns [] and the whole
    section is skipped, matching every other conditional section in this file, e.g. _section_trades
    when include_trades=False). One row per slice (AC-2's own slices array), the run's three
    headline walk-forward figures above the table (wfa_boundary_buffer_bars through format_number's
    own "count" kind, so a genuinely NULL value reads as the honest "-" placeholder rather than the
    raw text "None" leaking through, AC-6(e)), and a plain-language line naming why the drawdowns/
    price/trades sections above read empty for this run rather than leaving that unexplained."""
    wf = bundle.get("walkforward")
    if wf is None:
        return []  # not a walk-forward run
    lines: list[str] = ["## Walk-forward slices", ""]
    lines.append(
        "In-sample profit: " + format_number(wf.get("in_sample_profit"), "percent") + ". "
        "Out-of-sample profit: " + format_number(wf.get("out_of_sample_profit"), "percent") + ". "
        "Boundary buffer bars: " + format_number(wf.get("wfa_boundary_buffer_bars"), "count") + "."
    )
    lines.append("")
    slices = wf.get("slices") or []
    lines.append(f"{len(slices)} slice(s) recorded.")
    lines.append("")
    if slices:
        header = "| " + " | ".join(LABELS.get(c, c) for c in _WALKFORWARD_SLICE_TABLE_COLS) + " |"
        sep = "| " + " | ".join("---" for _ in _WALKFORWARD_SLICE_TABLE_COLS) + " |"
        lines.append(header)
        lines.append(sep)
        for s in slices:
            cells = [_format_walkforward_slice_cell(s, c) for c in _WALKFORWARD_SLICE_TABLE_COLS]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    absent = wf.get("absent_artifacts") or {}
    if absent:  # always true for a real walk-forward bundle (AC-2 names all four); guarded anyway
        named = "; ".join(f"{LABELS.get(name, name)}: {reason}" for name, reason in absent.items())
        lines.append(
            "_This is a walk-forward run. The sections above read empty on purpose, not from a "
            f"pipeline gap: {named}._"
        )
        lines.append("")
    return lines


def _section_environment(bundle: dict) -> list[str]:
    """## Run environment — Python / vbt / build provenance / hostname card."""
    env = bundle.get("environment") or {}
    lines: list[str] = ["## Run environment", ""]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    for key in ("python_version", "vbt_version", "build_provenance", "hostname",
                "sizing_method", "sizing_spec", "cost_model"):
        value = env.get(key)
        if value is None:
            rendered = "—"  # a NULL renders as the dash, as every row here does
        elif key in ("sizing_spec", "cost_model"):  # parsed JSON objects: one sorted, inline-code text
            rendered = f"`{json.dumps(value, sort_keys=True)}`"
        else:
            rendered = f"`{value}`"  # inline-code for machine strings
        lines.append(f"| {LABELS.get(key, key)} | {rendered} |")
    lines.append("")
    return lines


def _section_trades(bundle: dict) -> list[str]:
    """## Trades (first 20) — short trade table, with overflow note."""
    trades = bundle.get("trades")
    lines: list[str] = []
    # When include_trades=False, the bundle has no 'trades' key — render an
    # explanatory section header rather than a broken table.
    if trades is None:
        lines.append("## Trades")
        lines.append("")
        lines.append("_Trades omitted from this bundle._")
        lines.append("")
        return lines
    total = len(trades)
    shown = min(total, _TRADES_TOP_N)
    lines.append(f"## Trades (first {shown} of {total})")
    lines.append("")
    if total == 0:
        lines.append("_No trades._")
        lines.append("")
        return lines
    # Header row — every cell label is from LABELS (renderer-generated, so
    # subject to the L28 no-snake_case rule the probe enforces).
    header = "| " + " | ".join(LABELS.get(col, col) for col in _TRADE_TABLE_COLS) + " |"
    sep = "| " + " | ".join("---" for _ in _TRADE_TABLE_COLS) + " |"
    lines.append(header)
    lines.append(sep)
    for trade in trades[:shown]:
        cells = [_format_trade_cell(trade, col) for col in _TRADE_TABLE_COLS]
        lines.append("| " + " | ".join(cells) + " |")
    if total > shown:
        lines.append("")
        lines.append(f"_…and {total - shown} more trades. Full data in the JSON or HTML export._")
    lines.append("")
    return lines


def _section_drawdowns(bundle: dict) -> list[str]:
    """## Drawdowns (worst N by depth) — short drawdowns table."""
    drawdowns = bundle.get("drawdowns") or []
    lines: list[str] = []
    total = len(drawdowns)
    # Sort by depth (start_value - valley_value); largest drop first. Tolerant of
    # missing values so the renderer never crashes on a malformed row.
    sorted_dds = sorted(
        drawdowns,
        key=lambda d: (d.get("start_value") or 0) - (d.get("valley_value") or 0),
        reverse=True,
    )
    shown = min(total, _DRAWDOWNS_TOP_N)
    lines.append(f"## Drawdowns (worst {shown} of {total} by depth)")
    lines.append("")
    if total == 0:
        lines.append("_No drawdowns recorded._")
        lines.append("")
        return lines
    header = "| " + " | ".join(LABELS.get(col, col) for col in _DRAWDOWN_TABLE_COLS) + " |"
    sep = "| " + " | ".join("---" for _ in _DRAWDOWN_TABLE_COLS) + " |"
    lines.append(header)
    lines.append(sep)
    for dd in sorted_dds[:shown]:
        cells = [_format_drawdown_cell(dd, col) for col in _DRAWDOWN_TABLE_COLS]
        lines.append("| " + " | ".join(cells) + " |")
    if total > shown:
        lines.append("")
        lines.append(f"_…and {total - shown} more drawdowns. Full data in the JSON or HTML export._")
    lines.append("")
    return lines


def _section_settings(bundle: dict) -> list[str]:
    """## Settings at run time — flat list of scope.key = value for every setting."""
    snapshot = bundle.get("settings_snapshot")
    lines: list[str] = ["## Settings at run time", ""]
    # When include_settings=False, the bundle has no settings_snapshot key.
    if snapshot is None:
        lines.append("_Settings snapshot omitted from this bundle._")
        lines.append("")
        return lines
    # Snapshot is a list of {scope, key, value, value_type} rows (the canonical
    # form persisted in research_runs.settings_snapshot). Group by scope for
    # readability; render as a flat code block (settings keys may contain dots
    # and underscores — those are stored verbatim, not renderer labels).
    if not snapshot:
        lines.append("_No settings recorded._")
        lines.append("")
        return lines
    # Bullet-list grouped under the scope: keeps it scannable without snake_case
    # appearing in renderer-generated labels (the bullet body is content).
    by_scope: dict[str, list[dict]] = {}
    for row in snapshot:
        by_scope.setdefault(row.get("scope", "?"), []).append(row)
    for scope in sorted(by_scope.keys()):
        lines.append(f"**Scope: {scope}**")
        lines.append("")
        for row in sorted(by_scope[scope], key=lambda r: r.get("key", "")):
            key = row.get("key", "?")
            value = row.get("value", "")
            lines.append(f"- `{scope}.{key}` = `{value}`")
        lines.append("")
    return lines


def _footer(links: dict | None = None) -> list[str]:
    """Footer block. K.5 callers pass no links (plain-text only). K.6 owner and
    public routes both populate bundle["_links"] = {"json": url, "md": url, "html": url}
    before rendering; when present, those URLs are appended as a "Same export in"
    list so a reader of the MD can navigate to the JSON or HTML version of the
    same bundle.

    The same renderer serves both K.5 (owner) and K.6 (public) — the URLs differ
    per context (owner: /runs/{id}/export?format=...; public: /share/{uuid}?format=...)
    but the renderer just emits whatever URLs the caller passed.
    """
    out = [
        "---",
        "",
        "_Full data, including every bar of the equity curve and every trade, is available in"
        " the JSON and interactive HTML exports of this run._",
        "",
    ]
    if links:
        out.append("**Same export in other formats:**")
        out.append("")
        # Render in a stable order so two runs of the same bundle produce identical MD.
        for fmt in ("json", "md", "html"):
            url = links.get(fmt)
            if url:
                out.append(f"- [{fmt.upper()}]({url})")
        out.append("")
    return out


# --- Cell formatters --------------------------------------------------------


def _format_trade_cell(trade: dict, col: str) -> str:
    """Format one trade cell; column-aware so prices come out as currency, etc."""
    value = trade.get(col)
    if value is None:
        return "—"
    # Currency columns
    if col in ("avg_entry_price", "avg_exit_price", "pnl", "mae", "mfe", "entry_fees", "exit_fees"):
        return format_number(value, "currency")
    # Percent columns (return is stored as a fraction)
    if col in ("return_pct", "mae_return_pct", "mfe_return_pct"):
        return format_number(value, "percent")
    # Integer columns
    if col in ("trade_id", "entry_index", "exit_index", "column", "entry_order_id",
               "exit_order_id", "position_id"):
        return format_number(value, "count")
    # Text columns (direction, status) pass through.
    return str(value)


def _format_walkforward_slice_cell(s: dict, col: str) -> str:
    """Format one walk-forward slice cell; column-aware for timestamps vs profits vs counts."""
    value = s.get(col)
    if value is None:
        return "-"
    if col in ("in_sample_start_ts", "in_sample_end_ts", "out_of_sample_start_ts", "out_of_sample_end_ts"):
        return format_number(value, "timestamp")
    if col in ("in_sample_profit", "out_of_sample_profit"):
        return format_number(value, "percent")
    if col in ("slice_index", "trade_count"):
        return format_number(value, "count")
    return str(value)


def _format_drawdown_cell(dd: dict, col: str) -> str:
    """Format one drawdown cell; column-aware for values vs indices vs status."""
    value = dd.get(col)
    if value is None:
        return "—"
    if col in ("start_value", "valley_value", "end_value"):
        return format_number(value, "currency")
    if col in ("drawdown_id", "start_index", "valley_index", "end_index", "column"):
        return format_number(value, "count")
    return str(value)
