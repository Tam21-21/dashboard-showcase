"""Interactive HTML renderer for K.5 (M3 spec §5.2/§5.4; arch §21).

Self-contained HTML5 file: inline CSS dark theme matched to the dashboard
chrome, inline JSON bundle, inline lightweight-charts v5 standalone (committed
at backend/research/static/), inline rendering JS. Zero external requests; opens
offline; frozen artifact per arch §21 ("An export made in 2026 must render
identically in 2030").

Visual contract: matches the dashboard's RunExplorer surface — `#0a0a0a`
background with two subtle radial gradients + a hairline grid, Space Grotesk
display + IBM Plex Mono numerics if available (system fallbacks otherwise so
the export still looks clean on a machine without the fonts installed), eyebrow
labels (10px tracking 0.14em uppercase), card panels (`#111111` over `#1e1e1e`
border), and the dashboard's `#00C853 / #FF2D2D / #5AA2FF` palette in charts
(no TradingView colours, no TradingView attribution logo — `attributionLogo:
false` is set on every chart).

Title precedence: `bundle.run.name` if the user gave the run a name, otherwise
the strategy name. The visible H1 reflects this; "Run #N" is demoted to a meta
chip in the eyebrow row so the human-given name leads.

The renderer requires markdown-it-py for description -> HTML conversion. No
fallback: if the dependency is missing, the module import raises ImportError
loudly (CLAUDE.md "no fallbacks unless explicitly requested"). The dependency
is on the server venv (Appendix A, transitive of rich); the renderer is
server-tier only — laptop imports of this module are expected to fail.

lightweight-charts v5 API (locked 2026-05-24, verified against the migration
docs): use ``chart.addSeries(LightweightCharts.LineSeries, opts)`` and
``chart.addSeries(LightweightCharts.CandlestickSeries, opts)``. Do NOT use the
v4 type-specific methods (addLineSeries, addCandlestickSeries) — they were
removed in v5.
"""
from __future__ import annotations  # postponed type-hint evaluation

import html  # for escaping arbitrary text into HTML attributes / inline strings
import json  # for inlining the bundle as JSON
from pathlib import Path  # for lazy reads of the static chart-lib file

from markdown_it import MarkdownIt  # required server-tier dep; no fallback

from dashboard.backend.research.render_labels import (
    LABELS,  # snake_case -> human label
    METRIC_EXPLANATIONS,  # one-line plain-English per metric (tooltip text)
    STATS_FORMAT,  # stats key -> format_number kind
    STATS_KEYS,  # canonical stats order
    format_number,  # the kind-aware formatter
)

# Module-level markdown-it instance: html=False is the XSS guard for K.6's
# public path. Per markdown-it-py docs (security.html), html=False causes any
# raw HTML in the input to be escaped as plain text rather than passed through —
# so a description containing <script>alert(1)</script> renders as the literal
# escaped text, not as an executable tag. Verified live 2026-05-24.
_MD = MarkdownIt("commonmark", {"html": False})

# Path to the pinned lightweight-charts standalone bundle. Read lazily inside
# render_html() so importing this module has no I/O side effects.
_CHART_LIB_PATH = Path(__file__).parent / "static" / "lightweight-charts.standalone.production.js"

# Markers around the chart-lib inline so the K.5 probe can confirm byte-for-byte
# match against the committed file. Keep them on dedicated lines so a substring
# extract is trivial in the probe.
_CHART_LIB_BEGIN = "/* lightweight-charts v5 standalone BEGIN */"
_CHART_LIB_END = "/* lightweight-charts v5 standalone END */"


def render_html(bundle: dict) -> bytes:
    """Build the interactive HTML export. Returns UTF-8 bytes.

    Reads the pinned chart-lib bundle from disk on each call (the file size is
    ~196 KB; one read per export is fine). markdown-it-py converts the strategy
    description to HTML with html=False (raw HTML escaped, XSS-safe).
    """
    # Read the committed chart-lib file each call. Lazy (not at module import)
    # so render_html.py imports cleanly even on machines without the static
    # asset, surfacing the missing file as a clear runtime error when invoked.
    chart_lib_js = _CHART_LIB_PATH.read_text(encoding="utf-8")
    # JSON-serialise the bundle for the inline <script id="bundle"> block.
    # allow_nan=False keeps the embedded JSON strict-parseable too (mirroring
    # render_json's discipline). ensure_ascii=False writes characters as-is.
    # Escape any literal "</" inside JSON strings (e.g. a description containing
    # "</script>") as "<\/" so the embedded JSON cannot terminate the parent
    # script tag. Standard browsers and JSON.parse both accept "<\/" as "</".
    bundle_json = json.dumps(bundle, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    # Description: markdown -> HTML, html=False meaning raw HTML in source is
    # escaped. Empty description renders as an empty paragraph; safe either way.
    strategy = bundle.get("strategy") or {}
    desc_html = _MD.render(strategy.get("description") or "")
    # Header — the user-given run name wins; fall back to strategy name; final
    # fallback to a placeholder so the title is never empty.
    run = bundle.get("run") or {}
    raw_title = run.get("name") or strategy.get("name") or "Untitled run"
    title_text = html.escape(str(raw_title))
    # Meta chips beneath the H1: strategy + version, dataset symbol + timeframe,
    # and the run id (demoted to a chip per the no-"Run #N"-leading rule).
    dataset = bundle.get("dataset") or {}
    strategy_name = html.escape(str(strategy.get("name") or "—"))
    strategy_version = strategy.get("version")
    strategy_chip = f"{strategy_name}" + (f" <span class=\"num\">v{strategy_version}</span>" if strategy_version is not None else "")
    sym = dataset.get("symbol")
    tf = dataset.get("timeframe")
    dataset_chip = f"{html.escape(str(sym))} <span class=\"num\">{html.escape(str(tf))}</span>" if sym and tf else None
    run_id = run.get("id")
    run_chip = f"Run <span class=\"num\">#{run_id}</span>" if run_id is not None else None
    meta_chips = [chip for chip in (strategy_chip, dataset_chip, run_chip) if chip]
    meta_html = "<span class=\"dot\">·</span>".join(f"<span>{chip}</span>" for chip in meta_chips)
    # Settings drift chip (A4) — only shown when drift was computed (the bundle
    # has a settings_drift slot) and at least one key changed. Plain-English copy
    # over the original "drifted" wording so a first-time reader gets the meaning
    # at a glance.
    drift = bundle.get("settings_drift") or {}
    drift_chip_html = ""
    if drift.get("drifted"):
        n = len(drift.get("changes") or [])
        plural = "" if n == 1 else "s"
        drift_chip_html = (
            f'<span class="drift-chip" '
            f'title="{n} trading setting{plural} changed since this run. A new run today would not match this one.">'
            f'{n} setting{plural} changed since this run'
            f'</span>'
        )
    # Stats grid rendered server-side (cheap; no JS needed for the static values).
    stats_html = _build_stats_grid(bundle.get("stats") or {})
    # Lane WB (6.2c, AC-3): the walk-forward slices card, server-rendered like the stats grid and
    # the drift table above; "" for every non-walk-forward bundle (the walkforward key is absent).
    walkforward_html = _build_walkforward_slices_section(bundle)
    # Settings-drift table (server-rendered, hidden when there is no drift).
    drift_table_html = _build_drift_section(drift)
    # The HTML title — same precedence as the H1, kept short for tab titles.
    page_title = title_text
    if run_id is not None:
        page_title = f"{title_text} · Run #{run_id}"
    # Description card only appears when the strategy actually has a description.
    desc_section = (
        f'<section class="card"><div class="card-title"><span class="eyebrow">Description</span></div>'
        f'<div class="card-body prose">{desc_html}</div></section>'
        if (strategy.get("description") or "").strip()
        else ""
    )
    # Assemble the document. One <style> block, one bundle script, one chart-lib
    # script, one rendering script — keeps the K.5 probe's "script#bundle count
    # == 1" and chart-lib byte-match assertions trivial.
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{page_title}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<div class=\"backdrop\"></div>",  # pseudo-element holder for the dashboard's radial gradients
        "<main>",
        "<header class=\"hero\">",
        "<div class=\"eyebrow\">Research · Run</div>",
        f"<h1 class=\"display\">{title_text}</h1>",
        f"<div class=\"meta\">{meta_html}{drift_chip_html}</div>",
        "</header>",
        desc_section,
        stats_html,
        walkforward_html,
        '<section class="card flush"><div class="card-title"><span class="eyebrow">Price &amp; indicators</span></div><div class="chart-host"><div id="price-chart"></div></div></section>',
        '<div class="two-up">',
        '<section class="card flush"><div class="card-title"><span class="eyebrow">Equity</span></div><div class="chart-host"><div id="equity-chart"></div></div></section>',
        '<section class="card flush"><div class="card-title"><span class="eyebrow">Drawdown</span></div><div class="chart-host"><div id="drawdown-chart"></div></div></section>',
        '</div>',
        drift_table_html,
        '<section class="card flush"><div class="card-title"><span class="eyebrow">Trades</span></div><div id="trades-table"></div></section>',
        "</main>",
        f'<script id="bundle" type="application/json">{bundle_json}</script>',
        f"<script>{_CHART_LIB_BEGIN}\n{chart_lib_js}\n{_CHART_LIB_END}</script>",
        f"<script>{_RENDER_JS}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts).encode("utf-8")


def _build_stats_grid(stats: dict) -> str:
    """Build the 8-tile stats grid (matches the dashboard's StatsPanel layout)."""
    tiles: list[str] = []
    for key in STATS_KEYS:
        label = html.escape(LABELS.get(key, key))
        kind = STATS_FORMAT.get(key, "ratio")
        value_text = html.escape(format_number(stats.get(key), kind))
        explanation = html.escape(METRIC_EXPLANATIONS.get(key, ""))
        tiles.append(
            f'<div class="stat-tile" title="{explanation}">'
            f'<div class="stat-label eyebrow">{label}</div>'
            f'<div class="stat-value num">{value_text}</div>'
            f'</div>'
        )
    return (
        '<section class="card flush stats-card">'
        '<div class="card-title"><span class="eyebrow">Statistics</span></div>'
        f'<div class="stats-grid">{"".join(tiles)}</div>'
        '</section>'
    )


def _build_walkforward_slices_section(bundle: dict) -> str:
    """Server-render the "Walk-forward slices" card (lane WB, 6.2c, AC-3). Empty string for every
    bundle that carries no "walkforward" block, matching _build_drift_section's own empty-when-
    absent shape below (so an ordinary run's HTML export is unaffected: an empty string in the
    `parts` list joins to nothing)."""
    wf = bundle.get("walkforward")
    if not wf:
        return ""
    rows: list[str] = []
    for s in wf.get("slices") or []:
        rows.append(
            "<tr>"
            f'<td class="mono num">{html.escape(format_number(s.get("slice_index"), "count"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("in_sample_start_ts"), "timestamp"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("in_sample_end_ts"), "timestamp"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("out_of_sample_start_ts"), "timestamp"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("out_of_sample_end_ts"), "timestamp"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("in_sample_profit"), "percent"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("out_of_sample_profit"), "percent"))}</td>'
            f'<td class="mono num">{html.escape(format_number(s.get("trade_count"), "count"))}</td>'
            "</tr>"
        )
    # format_number(None, ...) returns the honest "-" placeholder (never the raw text "None"),
    # so a NULL wfa_boundary_buffer_bars (AC-6(e)) renders safely here too.
    summary_html = (
        '<p style="margin:0;padding:14px 18px;color:var(--c-muted);font-size:12px;line-height:1.55;">'
        f'In-sample profit {html.escape(format_number(wf.get("in_sample_profit"), "percent"))}, '
        f'out-of-sample profit {html.escape(format_number(wf.get("out_of_sample_profit"), "percent"))}, '
        f'boundary buffer {html.escape(format_number(wf.get("wfa_boundary_buffer_bars"), "count"))} bars.'
        '</p>'
    )
    absent = wf.get("absent_artifacts") or {}
    absent_html = ""
    if absent:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in absent.items())
        absent_html = (
            '<p style="margin:0;padding:0 18px 14px;color:var(--c-muted);font-size:12px;'
            'line-height:1.55;border-bottom:1px solid var(--c-border);">'
            f'This is a walk-forward run. The price, drawdown and trades sections above are empty on '
            f'purpose, not from a pipeline gap: {html.escape(reasons)}.</p>'
        )
    return (
        '<section class="card flush">'
        '<div class="card-title"><span class="eyebrow">Walk-forward slices</span></div>'
        f'{summary_html}{absent_html}'
        '<table class="data-table"><thead><tr>'
        '<th>Slice</th><th class="num">In-sample start</th><th class="num">In-sample end</th>'
        '<th class="num">Out-of-sample start</th><th class="num">Out-of-sample end</th>'
        '<th class="num">In-sample profit</th><th class="num">Out-of-sample profit</th>'
        '<th class="num">Trades</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        '</section>'
    )


def _build_drift_section(drift: dict) -> str:
    """Server-render the settings-changed table; empty string when no changes to show.

    Plain-language framing: "Trading settings changed since this run" + a one-line
    explanation so a reader who has never seen the dashboard before knows why this
    matters (a re-run today would not reproduce these numbers).
    """
    changes = drift.get("changes") or []
    if not changes:
        return ""  # nothing to render; the chip in the hero already flags the change
    rows: list[str] = []
    for change in changes:
        scope = html.escape(str(change.get("scope", "")))
        key = html.escape(str(change.get("key", "")))
        run_value = html.escape(str(change.get("run_value", "")))
        current_value = html.escape(str(change.get("current_value", "")))
        rows.append(
            f'<tr><td class="mono">{scope}.{key}</td>'
            f'<td class="mono num">{run_value}</td>'
            f'<td class="mono num">{current_value}</td></tr>'
        )
    return (
        '<section class="card flush">'
        '<div class="card-title">'
        '<span class="eyebrow">Trading settings changed since this run</span>'
        '</div>'
        '<p style="margin:0;padding:14px 18px;color:var(--c-muted);font-size:12px;'
        'line-height:1.55;border-bottom:1px solid var(--c-border);">'
        'These trading settings were changed after this run finished. '
        'A new run today would use the newer values, so the numbers above would not match.'
        '</p>'
        '<table class="data-table"><thead><tr>'
        '<th>Setting</th><th class="num">When this ran</th><th class="num">Today</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        '</section>'
    )


# --- CSS (dark theme matched to dashboard's RunExplorer surface) ------------

# The dashboard's CSS variables (--c-bg etc) collapsed to literal values so the
# export is self-contained. Fonts default to Space Grotesk / IBM Plex Mono if
# the viewer has them locally (likely on Tamer's machine; @fontsource ships
# them with the dashboard), with a clean system fallback otherwise. No external
# requests per arch §21 ("Zero external requests").
_CSS = """
:root {
  --c-bg: #0a0a0a; --c-surface: #111111; --c-surface2: #161616;
  --c-border: #1e1e1e; --c-hair: rgba(255,255,255,0.06);
  --c-fg: #ffffff; --c-muted: #888888; --c-subtle: #555555;
  --c-pos: #00C853; --c-neg: #FF2D2D; --c-warn: #F5A524; --c-info: #5AA2FF;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--c-bg); color: var(--c-fg); }
body {
  font-family: 'Space Grotesk', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  font-feature-settings: "ss01", "cv11";
  font-weight: 400;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  min-height: 100vh; position: relative;
}
/* Dashboard backdrop: two radial gradients (red top, blue bottom) over a hairline grid.
   Lives on a fixed pseudo-element so scrolling doesn't reveal the seams. */
.backdrop {
  position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background-image:
    radial-gradient(ellipse 120% 80% at 50% -10%, rgba(255,45,45,0.04) 0%, transparent 55%),
    radial-gradient(ellipse 100% 60% at 50% 110%, rgba(90,162,255,0.025) 0%, transparent 60%),
    linear-gradient(to right, rgba(255,255,255,0.012) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255,255,255,0.012) 1px, transparent 1px);
  background-size: auto, auto, 80px 80px, 80px 80px;
}
main { max-width: 1200px; margin: 0 auto; padding: 28px 24px 60px; }
/* Hero */
.hero { margin-bottom: 28px; }
.eyebrow {
  font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--c-muted); font-weight: 500;
}
.display {
  font-family: 'Space Grotesk', ui-sans-serif, system-ui, sans-serif;
  font-size: 36px; font-weight: 300; letter-spacing: -0.02em;
  margin: 6px 0 10px; line-height: 1.05; color: var(--c-fg);
  /* Slight gradient on the title for a subtle glow that matches the dashboard's red accent. */
  background: linear-gradient(180deg, #ffffff 0%, #cccccc 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: transparent;
}
.meta {
  font-size: 12px; color: var(--c-muted); display: flex; flex-wrap: wrap;
  align-items: center; gap: 8px;
}
.meta .dot { color: var(--c-subtle); margin: 0 2px; }
.meta .num { font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; color: var(--c-fg); }
.drift-chip {
  display: inline-block; margin-left: 10px; padding: 3px 8px;
  font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase;
  background: rgba(245,165,36,0.10); color: var(--c-warn);
  border: 1px solid rgba(245,165,36,0.30); border-radius: 2px;
  cursor: help;
}
/* Card surface */
.card {
  background: var(--c-surface); border: 1px solid var(--c-border);
  border-radius: 2px; margin-bottom: 24px;
}
.card-title {
  padding: 14px 18px; border-bottom: 1px solid var(--c-border);
}
.card-body { padding: 18px; }
.card.flush .card-body { padding: 0; }
.two-up { display: grid; grid-template-columns: 1fr; gap: 24px; margin-bottom: 24px; }
@media (min-width: 900px) { .two-up { grid-template-columns: 1fr 1fr; } }
.two-up > .card { margin-bottom: 0; }
/* Stats grid (4-wide on desktop, 2 on small). */
.stats-grid { display: grid; grid-template-columns: repeat(2, 1fr); }
@media (min-width: 900px) { .stats-grid { grid-template-columns: repeat(4, 1fr); } }
.stat-tile {
  padding: 16px 20px; border-right: 1px solid rgba(255,255,255,0.04);
  border-bottom: 1px solid rgba(255,255,255,0.04); cursor: help;
}
.stat-tile:hover { background: rgba(255,255,255,0.015); }
.stat-label { color: var(--c-muted); }
.stat-value {
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 18px; color: var(--c-fg); margin-top: 6px;
  font-variant-numeric: tabular-nums;
}
/* Chart hosts */
.chart-host { padding: 12px; }
#price-chart   { width: 100%; height: 360px; }
#equity-chart  { width: 100%; height: 280px; }
#drawdown-chart{ width: 100%; height: 220px; }
/* Description card (Markdown content) */
.prose { color: rgba(255,255,255,0.92); font-size: 13px; line-height: 1.55; }
.prose h1, .prose h2, .prose h3 { color: var(--c-fg); margin: 14px 0 6px; }
.prose h2 { font-size: 16px; font-weight: 500; }
.prose h3 { font-size: 14px; font-weight: 500; }
.prose p { margin: 0 0 10px; }
.prose code, .prose pre {
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--c-surface2); padding: 1px 5px; border-radius: 2px;
  font-size: 12px; color: var(--c-info);
}
.prose pre { padding: 10px 12px; overflow-x: auto; }
.prose ul, .prose ol { padding-left: 20px; margin: 0 0 10px; }
/* Tables (trades + drift) */
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table thead th {
  text-align: left; padding: 10px 16px; color: var(--c-muted);
  font-weight: 500; font-size: 10px; letter-spacing: 0.12em;
  text-transform: uppercase; border-bottom: 1px solid var(--c-border);
  background: transparent; user-select: none; position: sticky; top: 0;
  cursor: default;
}
.data-table thead th.sortable { cursor: pointer; }
.data-table thead th.sortable:hover { color: var(--c-fg); }
.data-table thead th.num, .data-table tbody td.num { text-align: right; }
.data-table tbody td {
  padding: 8px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);
  color: var(--c-fg);
}
.data-table tbody td.mono, .data-table tbody td.num {
  font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}
.data-table tbody tr:hover { background: rgba(255,255,255,0.02); }
""".strip()


# --- Rendering JS -----------------------------------------------------------

# The inline rendering script. Reads the bundle from #bundle, builds three
# charts (price+indicators, equity+benchmark, drawdown), renders the trades
# table with sortable headers. Uses the v5 unified addSeries API; v4 type-
# specific addLineSeries/addCandlestickSeries are removed.
#
# `attributionLogo: false` is set on every chart options block — kills the
# TradingView attribution mark that v5 shows by default in the bottom-left.
_RENDER_JS = r"""
(function () {
  // Read the bundle from the inline JSON script tag.
  var bundle = JSON.parse(document.getElementById('bundle').textContent);

  // v5 API reference (locked 2026-05-24): chart.addSeries(LightweightCharts.CandlestickSeries, opts)
  // and chart.addSeries(LightweightCharts.LineSeries, opts). The v4 type-specific methods
  // (addLineSeries, addCandlestickSeries) were removed in v5 — do not use them.

  // Dashboard-matched layout: transparent background so the body backdrop shows through,
  // muted text, hairline grid, IBM Plex Mono for axis labels.
  var BASE_LAYOUT = {
    background: { type: 'solid', color: 'transparent' },
    textColor: '#8a8a8a',
    fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
    fontSize: 11,
    attributionLogo: false
  };
  var BASE_GRID = {
    vertLines: { color: 'rgba(255,255,255,0.04)' },
    horzLines: { color: 'rgba(255,255,255,0.04)' }
  };
  var BASE_PRICE_SCALE = { borderColor: 'rgba(255,255,255,0.08)' };
  var BASE_TIME_SCALE = { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: false };

  // --- Price chart with SMA indicator overlays ---
  var priceChart = LightweightCharts.createChart(document.getElementById('price-chart'), {
    autoSize: true,
    layout: BASE_LAYOUT, grid: BASE_GRID,
    rightPriceScale: BASE_PRICE_SCALE, timeScale: BASE_TIME_SCALE
  });
  var priceSeries = priceChart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: '#00C853', downColor: '#FF2D2D', borderVisible: false,
    wickUpColor: '#00C853', wickDownColor: '#FF2D2D'
  });
  // price.parquet rows: {timestamp, open, high, low, close, volume}.
  var priceData = (bundle.price || []).map(function (p) {
    return { time: p.timestamp, open: p.open, high: p.high, low: p.low, close: p.close };
  });
  priceSeries.setData(priceData);

  // Indicator overlays — colours match the dashboard's INDICATOR_COLORS palette.
  var indicators = bundle.indicators || {};
  var indicatorColors = ['#5AA2FF', '#F5A524', '#9b8cff', '#36c5b0', '#f06292'];
  var indicatorIdx = 0;
  Object.keys(indicators).forEach(function (key) {
    var color = indicatorColors[indicatorIdx % indicatorColors.length];
    indicatorIdx += 1;
    var series = priceChart.addSeries(LightweightCharts.LineSeries, {
      color: color, lineWidth: 1, priceLineVisible: false, title: key
    });
    var data = indicators[key]
      .filter(function (pair) { return pair[1] !== null && pair[1] !== undefined; })
      .map(function (pair) { return { time: pair[0], value: pair[1] }; });
    series.setData(data);
  });
  priceChart.timeScale().fitContent();

  // --- Equity chart (strategy + buy-and-hold benchmark) ---
  var equityChart = LightweightCharts.createChart(document.getElementById('equity-chart'), {
    autoSize: true,
    layout: BASE_LAYOUT, grid: BASE_GRID,
    rightPriceScale: BASE_PRICE_SCALE, timeScale: BASE_TIME_SCALE
  });
  // Strategy equity as an area series, blue (matches the dashboard EquityChart).
  var equityArea = equityChart.addSeries(LightweightCharts.AreaSeries, {
    lineColor: '#5AA2FF',
    topColor: 'rgba(90,162,255,0.25)', bottomColor: 'rgba(90,162,255,0.02)',
    lineWidth: 2, priceLineVisible: false, title: 'Strategy'
  });
  equityArea.setData((bundle.equity || []).map(function (pair) {
    return { time: pair[0], value: pair[1] };
  }));
  // Buy-and-hold benchmark as a dimmer line on the same scale.
  var benchmark = bundle.benchmark || [];
  if (benchmark.length) {
    var benchSeries = equityChart.addSeries(LightweightCharts.LineSeries, {
      color: 'rgba(255,255,255,0.35)', lineWidth: 1,
      priceLineVisible: false, title: 'Buy & hold'
    });
    benchSeries.setData(benchmark.map(function (pair) {
      return { time: pair[0], value: pair[1] };
    }));
  }
  equityChart.timeScale().fitContent();

  // --- Drawdown chart (derived from equity) ---
  var drawdownChart = LightweightCharts.createChart(document.getElementById('drawdown-chart'), {
    autoSize: true,
    layout: BASE_LAYOUT, grid: BASE_GRID,
    rightPriceScale: BASE_PRICE_SCALE, timeScale: BASE_TIME_SCALE
  });
  var ddArea = drawdownChart.addSeries(LightweightCharts.AreaSeries, {
    lineColor: '#FF2D2D',
    topColor: 'rgba(255,45,45,0.02)', bottomColor: 'rgba(255,45,45,0.28)',
    lineWidth: 1, priceLineVisible: false
  });
  var peak = -Infinity;
  var ddData = (bundle.equity || []).map(function (pair) {
    peak = Math.max(peak, pair[1]);
    return { time: pair[0], value: peak > 0 ? pair[1] / peak - 1 : 0 };
  });
  ddArea.setData(ddData);
  drawdownChart.timeScale().fitContent();

  // --- Trades table with sortable headers ---
  var tradeCols = [
    { key: 'trade_id', label: 'Trade id', kind: 'count' },
    { key: 'direction', label: 'Direction', kind: 'text' },
    { key: 'entry_index', label: 'Entry bar', kind: 'count' },
    { key: 'avg_entry_price', label: 'Average entry price', kind: 'currency' },
    { key: 'exit_index', label: 'Exit bar', kind: 'count' },
    { key: 'avg_exit_price', label: 'Average exit price', kind: 'currency' },
    { key: 'pnl', label: 'Profit / loss', kind: 'currency' },
    { key: 'return_pct', label: 'Return', kind: 'percent' },
    { key: 'mae', label: 'Max adverse excursion', kind: 'currency' },
    { key: 'mfe', label: 'Max favourable excursion', kind: 'currency' }
  ];
  var trades = bundle.trades || [];

  function fmt(value, kind) {
    if (value === null || value === undefined) { return '—'; }
    if (kind === 'percent')  { return (value * 100).toFixed(1) + '%'; }
    if (kind === 'currency') { return '$' + Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    if (kind === 'count')    { return Number(value).toLocaleString('en-US'); }
    return String(value);
  }

  function renderTradesTable(sortedBy, ascending) {
    var sorted = trades.slice();
    if (sortedBy) {
      sorted.sort(function (a, b) {
        var av = a[sortedBy], bv = b[sortedBy];
        if (av === null || av === undefined) { return 1; }
        if (bv === null || bv === undefined) { return -1; }
        if (av < bv) { return ascending ? -1 : 1; }
        if (av > bv) { return ascending ? 1 : -1; }
        return 0;
      });
    }
    var head = tradeCols.map(function (c) {
      var alignClass = (c.kind === 'currency' || c.kind === 'percent' || c.kind === 'count') ? ' class="sortable num"' : ' class="sortable"';
      return '<th' + alignClass + ' data-key="' + c.key + '">' + c.label + '</th>';
    }).join('');
    var body = sorted.map(function (t) {
      return '<tr>' + tradeCols.map(function (c) {
        var alignClass = (c.kind === 'currency' || c.kind === 'percent' || c.kind === 'count') ? ' class="mono num"' : ' class="mono"';
        return '<td' + alignClass + '>' + fmt(t[c.key], c.kind) + '</td>';
      }).join('') + '</tr>';
    }).join('');
    var host = document.getElementById('trades-table');
    if (trades.length === 0) {
      host.innerHTML = '<div style="padding:24px;color:var(--c-muted);font-size:12px;text-align:center;">No trades recorded.</div>';
      return;
    }
    host.innerHTML =
      '<div style="overflow-x:auto;">' +
      '<table class="data-table"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table>' +
      '</div>';
    // Re-wire click handlers after re-render.
    Array.prototype.forEach.call(host.querySelectorAll('th.sortable'), function (th) {
      th.addEventListener('click', function () {
        var key = th.getAttribute('data-key');
        var newAsc = !(sortedBy === key && ascending);
        renderTradesTable(key, newAsc);
      });
    });
  }
  renderTradesTable(null, true);
})();
""".strip()
