"""Render the quality-report bundle as a standalone HTML page.

Spec brief §12 brief F probe: "HTML render opens offline with the same
dashboard look-and-feel as M3 K.5; embeds the BatchQualityReport JSON
inline (escape `</` → `<\\/` per K.5 hardening)."

The page is self-contained (no external JS / CSS / fonts) so it works the
same in a browser-tab, an email attachment, or behind the Caddy bypass on
the public share path. Styling mirrors M3 K.5's dashboard skin
(`#0a0a0a` backdrop, Space Grotesk + IBM Plex Mono with system fallbacks,
4-tile stats grid, table-driven content).

The MD renderer's plain-English logic is duplicated rather than reused
because the two surfaces have different layout obligations (HTML stat
tiles + tables; MD heading sections). Same M3 K.5 pattern.
"""
from __future__ import annotations

import html
import json
from datetime import datetime as _dt, timezone as _tz

from ...research import render_labels


_CSS = """
:root {
  --bg: #0a0a0a;
  --panel: #15151a;
  --panel-border: #25252e;
  --text: #ededf2;
  --muted: #8b8b95;
  --accent: #6acfff;
  --ok: #6dd99c;
  --warn: #ffba6a;
  --bad: #ff7373;
  --mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --sans: 'Space Grotesk', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  background-image:
    radial-gradient(at 12% 0%, rgba(106, 207, 255, 0.06), transparent 40%),
    radial-gradient(at 88% 0%, rgba(255, 186, 106, 0.04), transparent 40%);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  min-height: 100vh;
}
.container { max-width: 1080px; margin: 0 auto; padding: 40px 32px 80px; }
h1 { font-size: 26px; margin: 0 0 4px; font-weight: 600; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 32px 0 12px; font-weight: 600; }
h3 { font-size: 14px; margin: 20px 0 8px; font-weight: 600; color: var(--muted);
     text-transform: uppercase; letter-spacing: 0.06em; }
.meta-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 24px; }
.chip {
  background: var(--panel); border: 1px solid var(--panel-border);
  border-radius: 999px; padding: 4px 12px; font-size: 12px; color: var(--muted);
  font-family: var(--mono);
}
.chip.ok { color: var(--ok); border-color: rgba(109, 217, 156, 0.3); }
.chip.warn { color: var(--warn); border-color: rgba(255, 186, 106, 0.3); }
.chip.bad { color: var(--bad); border-color: rgba(255, 115, 115, 0.3); }
.grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0 24px;
}
.tile {
  background: var(--panel); border: 1px solid var(--panel-border);
  border-radius: 8px; padding: 16px 18px;
}
.tile .k { font-size: 11px; color: var(--muted); text-transform: uppercase;
            letter-spacing: 0.08em; margin-bottom: 6px; }
.tile .v { font-size: 22px; font-family: var(--mono); }
table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12.5px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--panel-border); }
th { color: var(--muted); font-weight: 500; text-transform: uppercase;
     letter-spacing: 0.05em; font-size: 11px; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; }
.panel { background: var(--panel); border: 1px solid var(--panel-border);
         border-radius: 8px; padding: 20px 24px; margin: 16px 0; }
.muted { color: var(--muted); }
.mono { font-family: var(--mono); font-size: 12.5px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: none; border-top: 1px solid var(--panel-border); margin: 32px 0; }
.links { margin: 32px 0 0; padding-top: 24px; border-top: 1px solid var(--panel-border); }
.links li { font-family: var(--mono); font-size: 12.5px; }
"""


def _t(labels: dict, key: str) -> str:
    """Label lookup with snake_case fallback."""
    return labels.get(key) or key.replace("_", " ").title()


def _ts(value) -> str:
    if value is None or value == "" or value == 0:
        return "—"
    try:
        return _dt.fromtimestamp(int(value), _tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def _safe_json_inline(obj) -> str:
    """JSON-encode for inline embed; escape `</` so a payload containing
    `</script>` cannot terminate the parent script tag (K.5 hardening
    discipline carried into brief F)."""
    text = json.dumps(obj, ensure_ascii=False)
    return text.replace("</", "<\\/")


def _h(value) -> str:
    """HTML-escape a value, coercing to str. Used in body interpolations so
    no user-provided text reaches the document without going through
    html.escape first."""
    if value is None:
        return "—"
    return html.escape(str(value))


def _table_rows(labels: dict, items: list[tuple[str, object]]) -> str:
    """Render a 2-column key/value table body from (key, value) tuples.

    Labels are translated via render_labels.LABELS; values are HTML-escaped.
    """
    out = ["<table><tbody>"]
    for k, v in items:
        label = _t(labels, k)
        out.append(f"<tr><th scope='row'>{_h(label)}</th><td>{_h(v)}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def _status_chip(labels: dict, status: str | None) -> str:
    cls = "chip"
    if status in ("succeeded",):
        cls += " ok"
    elif status in ("succeeded_with_quarantines",):
        cls += " warn"
    elif status in ("failed", "rolled_back", "cancelled_partial"):
        cls += " bad"
    return f"<span class='{cls}'>{_h(_t(labels, status or 'unknown'))}</span>"


def render_quality_html(bundle: dict) -> bytes:
    """Render the bundle to UTF-8 HTML bytes."""
    batch = bundle["batch"]
    report = bundle["report"]
    labels = bundle.get("labels") or render_labels.LABELS
    links = bundle.get("_links") or {}

    # Page title — prefer the source-descriptor basename for the loader path.
    batch_label = f"Batch #{batch['batch_id']}"
    desc = batch.get("source_descriptor")
    if desc and isinstance(desc, str):
        try:
            parsed = json.loads(desc)
            bn = parsed.get("source_file_basename")
            if bn:
                batch_label = f"{bn} · Batch #{batch['batch_id']}"
        except Exception:
            pass

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html lang='en'><head>")
    parts.append("<meta charset='utf-8'>")
    parts.append(f"<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append(f"<title>{_h(batch_label)} · Quality report</title>")
    parts.append("<meta name='robots' content='noindex,nofollow'>")
    parts.append(f"<style>{_CSS}</style>")
    parts.append("</head><body>")
    parts.append("<div class='container'>")
    parts.append(f"<h1>{_h(batch_label)}</h1>")

    # Meta chips
    chips = [
        f"<span class='chip'>{_h(_t(labels, 'kind'))}: {_h(batch.get('kind') or '—')}</span>",
        f"<span class='chip'>{_h(_t(labels, 'source'))}: {_h(batch.get('source') or '—')}</span>",
        _status_chip(labels, batch.get("status")),
    ]
    parts.append(f"<div class='meta-chips'>{''.join(chips)}</div>")

    # Stats grid (4 tiles)
    parts.append("<div class='grid'>")
    for k in ("event_count", "quarantine_count", "started_at", "finished_at"):
        val = batch.get(k)
        if k in ("started_at", "finished_at"):
            val_str = _ts(val)
        else:
            val_str = _h(val or 0)
        parts.append(
            f"<div class='tile'><div class='k'>{_h(_t(labels, k))}</div>"
            f"<div class='v'>{val_str}</div></div>"
        )
    parts.append("</div>")

    # Batch panel
    parts.append("<h2>Batch summary</h2>")
    parts.append("<div class='panel'>")
    items = [
        ("kind", batch.get("kind")),
        ("source", batch.get("source")),
        ("policy", batch.get("policy")),
        ("status", _t(labels, batch.get("status") or "")),
        ("started_at", _ts(batch.get("started_at"))),
        ("finished_at", _ts(batch.get("finished_at"))),
        ("last_heartbeat_at", _ts(batch.get("last_heartbeat_at"))),
        ("content_hash", batch.get("content_hash")),
        ("parent_batch_id", batch.get("parent_batch_id")),
        ("request_id", batch.get("request_id")),
        ("event_count", batch.get("event_count")),
        ("quarantine_count", batch.get("quarantine_count")),
    ]
    if batch.get("failure_class"):
        items.append(("failure_class",
                      _t(labels, batch["failure_class"]) or batch["failure_class"]))
    parts.append(_table_rows(labels, items))
    parts.append("</div>")

    # Per-shape body
    shape = report.get("shape")
    if shape == "record_level":
        parts.append(_render_record_level_html(report, labels))
    elif shape == "batch_level_failure":
        parts.append(_render_batch_level_failure_html(report, labels))
    elif shape == "correction_summary":
        parts.append(_render_correction_summary_html(report, labels))
    elif shape == "rollback_summary":
        parts.append(_render_rollback_summary_html(report, labels))
    elif shape == "cancelled_partial_stub":
        parts.append(_render_cancelled_partial_stub_html(report, labels))
    elif shape is None:
        parts.append("<h2>Quality report</h2>")
        parts.append(f"<p class='muted'>The quality report is not yet available: "
                     f"<span class='mono'>{_h(report.get('reason') or '—')}</span></p>")

    # Inline JSON dump for forensics (escaped to avoid </script> termination)
    parts.append("<hr>")
    parts.append("<h2>Raw report (JSON)</h2>")
    parts.append("<details><summary class='muted'>Show JSON</summary>")
    parts.append(f"<pre class='mono'>{_h(json.dumps(report, indent=2, ensure_ascii=False))}</pre>")
    parts.append("</details>")
    # Hidden inline JSON for downstream scrapers (K.5-style hardening).
    parts.append(f"<script type='application/json' id='bundle-report'>{_safe_json_inline(report)}</script>")

    # _links footer
    if links:
        parts.append("<div class='links'>")
        parts.append("<h3>Other formats</h3>")
        parts.append("<ul>")
        for fmt, url in sorted(links.items()):
            parts.append(f"<li><a href='{_h(url)}'>{_h(fmt.upper())}</a></li>")
        parts.append("</ul></div>")

    parts.append("</div></body></html>")
    return "\n".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# Per-shape body renderers (HTML)
# ---------------------------------------------------------------------------


def _render_record_level_html(report: dict, labels: dict) -> str:
    out: list[str] = []
    totals = report.get("totals") or {}
    out.append("<h2>" + _h(_t(labels, "record_level")) + "</h2>")

    # Totals as a 6-tile sub-grid for visual scan
    out.append("<div class='grid' style='grid-template-columns: repeat(3, 1fr)'>")
    for k in ("records_seen", "events_written", "dedup_hits",
              "dedup_collisions_with_changed_payload", "quarantines",
              "provisional_fields_populated"):
        out.append(
            f"<div class='tile'><div class='k'>{_h(_t(labels, k))}</div>"
            f"<div class='v'>{_h(totals.get(k, 0))}</div></div>"
        )
    out.append("</div>")

    by_class = report.get("quarantines_summary_by_failure_class") or {}
    if by_class:
        out.append("<h3>Quarantines by failure class</h3>")
        out.append("<div class='panel'><table><thead><tr><th>Failure class</th>"
                   "<th class='num'>Count</th></tr></thead><tbody>")
        for cls, n in sorted(by_class.items()):
            out.append(f"<tr><td>{_h(_t(labels, cls))}</td>"
                       f"<td class='num'>{_h(n)}</td></tr>")
        out.append("</tbody></table></div>")

    samples = report.get("quarantines_samples") or []
    if samples:
        out.append("<h3>Quarantine samples</h3>")
        out.append("<div class='panel'><table><thead><tr>"
                   "<th>Event type</th><th>Failure class</th><th>Step</th><th>Message</th>"
                   "</tr></thead><tbody>")
        for s in samples:
            step_key = f"failure_step.{s.get('failure_step')}"
            out.append(
                f"<tr>"
                f"<td>{_h(s.get('event_type'))}</td>"
                f"<td>{_h(_t(labels, s.get('failure_class') or '') or s.get('failure_class'))}</td>"
                f"<td>{_h(_t(labels, step_key) if s.get('failure_step') else '—')}</td>"
                f"<td>{_h(str(s.get('failure_message') or '')[:200])}</td>"
                f"</tr>"
            )
        out.append("</tbody></table></div>")

    dedup_col = report.get("dedup_collisions") or []
    if dedup_col:
        out.append("<h3>Dedup collisions (changed payload)</h3>")
        out.append("<div class='panel'><table><thead><tr>"
                   "<th>Natural key</th><th>Stored payload (sha prefix)</th>"
                   "<th>New payload (sha prefix)</th></tr></thead><tbody>")
        for d in dedup_col:
            out.append(
                f"<tr>"
                f"<td>{_h(d.get('natural_key'))}</td>"
                f"<td>{_h(d.get('stored_payload_canonical_sha256_prefix'))}</td>"
                f"<td>{_h(d.get('new_payload_canonical_sha256_prefix'))}</td>"
                f"</tr>"
            )
        out.append("</tbody></table></div>")

    nkprefix = report.get("dedup_summary_by_natural_key_prefix") or {}
    if nkprefix:
        out.append("<h3>Duplicate records grouped by natural-key prefix</h3>")
        out.append("<div class='panel'><table><thead><tr>"
                   "<th>Prefix</th><th class='num'>Count</th></tr></thead><tbody>")
        for k, v in sorted(nkprefix.items(), key=lambda kv: kv[1], reverse=True):
            out.append(f"<tr><td class='mono'>{_h(k)}</td>"
                       f"<td class='num'>{_h(v)}</td></tr>")
        out.append("</tbody></table></div>")

    tf = report.get("terminal_failure")
    if tf:
        out.append("<h3>Terminal failure</h3>")
        items = [
            ("failure_class",
             _t(labels, tf.get("failure_class") or "") or tf.get("failure_class")),
            ("underlying_class",
             _t(labels, tf.get("underlying_class") or "") or tf.get("underlying_class")),
            ("record_index_in_batch", tf.get("record_index_in_batch")),
            ("natural_key", tf.get("natural_key")),
            ("rolled_back_event_count", tf.get("rolled_back_event_count")),
            ("failure_message", tf.get("failure_message")),
        ]
        out.append("<div class='panel'>" + _table_rows(labels, items) + "</div>")

    return "\n".join(out)


def _render_batch_level_failure_html(report: dict, labels: dict) -> str:
    out = ["<h2>" + _h(_t(labels, "batch_level_failure")) + "</h2>"]
    cls = report.get("failure_class")
    if cls:
        out.append(f"<p><strong>{_h(_t(labels, cls) or cls)}</strong></p>")
    ctx = report.get("failure_context") or {}
    if ctx:
        items = [(k, str(v)[:200]) for k, v in sorted(ctx.items())]
        out.append("<div class='panel'>" + _table_rows(labels, items) + "</div>")
    return "\n".join(out)


def _render_correction_summary_html(report: dict, labels: dict) -> str:
    out = ["<h2>" + _h(_t(labels, "correction_summary")) + "</h2>"]
    items = [
        ("event_id", report.get("event_id")),
        ("natural_key", report.get("natural_key")),
        ("prev_version", report.get("prev_version")),
        ("new_version", report.get("new_version")),
        ("parent_batch_id", report.get("parent_batch_id")),
    ]
    out.append("<div class='panel'>" + _table_rows(labels, items) + "</div>")
    diff = report.get("payload_diff_keys") or []
    if diff:
        keys = ", ".join(f"<code>{_h(k)}</code>" for k in diff)
        out.append(f"<p><em>{_h(_t(labels, 'payload_diff_keys'))}</em>: {keys}</p>")
    if report.get("trigger_note"):
        out.append(f"<p><em>{_h(_t(labels, 'trigger_note'))}</em>: "
                   f"{_h(report['trigger_note'])}</p>")
    return "\n".join(out)


def _render_rollback_summary_html(report: dict, labels: dict) -> str:
    out = ["<h2>" + _h(_t(labels, "rollback_summary")) + "</h2>"]
    items = [
        ("target_batch_id", report.get("target_batch_id")),
        ("rolled_back_event_count", report.get("rolled_back_event_count")),
    ]
    out.append("<div class='panel'>" + _table_rows(labels, items) + "</div>")
    if report.get("trigger_note"):
        out.append(f"<p><em>{_h(_t(labels, 'trigger_note'))}</em>: "
                   f"{_h(report['trigger_note'])}</p>")
    return "\n".join(out)


def _render_cancelled_partial_stub_html(report: dict, labels: dict) -> str:
    out = ["<h2>" + _h(_t(labels, "cancelled_partial_stub")) + "</h2>"]
    items = [
        ("rolled_back_event_count", report.get("rolled_back_event_count")),
        ("swept_at", _ts(report.get("swept_at"))),
        ("swept_by", report.get("swept_by")),
    ]
    out.append("<div class='panel'>" + _table_rows(labels, items) + "</div>")
    return "\n".join(out)
