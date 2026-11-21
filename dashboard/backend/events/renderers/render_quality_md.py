"""Render the quality-report bundle as plain-English markdown bytes.

Spec brief §7.4 + §12 brief F probe row: "MD with no snake_case keys in body text."
The renderer reads labels from `bundle['labels']` so every column header /
field label is plain-English. Numbers come through `format_number` (M3
K.5). Footer emits `_links` lines when present (the /export route populates
`bundle["_links"]` before calling this).

Per the F brief's gating semantics: `include_logs` False shrinks the
quarantines_samples + dedup_collisions arrays to empty inside
quality_bundle; the renderer just emits the section headers (which will be
empty bodies).
"""
from __future__ import annotations

from datetime import datetime as _dt, timezone as _tz

from ...research import render_labels


def _t(labels: dict, key: str) -> str:
    """Plain-English label lookup with a snake_case fallback for unknown keys.

    A KeyError here would be a coverage gap in render_labels.LABELS; the
    fallback (` `-joined Title Case) is a graceful degradation so a missed
    label still produces readable output rather than a 500.
    """
    if key in labels:
        return labels[key]
    return key.replace("_", " ").title()


def _ts(value) -> str:
    """Format an epoch-seconds int as YYYY-MM-DD HH:MM:SS UTC, or '—' for None."""
    if value is None or value == "" or value == 0:
        return "—"
    try:
        return _dt.fromtimestamp(int(value), _tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return str(value)


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Return MD-table lines: header row, separator, then body rows."""
    out: list[str] = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return out


def render_quality_md(bundle: dict) -> bytes:
    """Render the bundle to UTF-8 MD bytes. Plain-English throughout (§7.4)."""
    batch = bundle["batch"]
    report = bundle["report"]
    labels = bundle.get("labels") or render_labels.LABELS
    links = bundle.get("_links") or {}

    lines: list[str] = []

    # H1 — the batch name surface. We prefer the source descriptor's basename if
    # the loader path supplied a JSON source_descriptor with that key; else fall
    # back to a "Batch #N" label.
    batch_label = f"Batch #{batch['batch_id']}"
    desc = batch.get("source_descriptor")
    if desc and isinstance(desc, str):
        try:
            import json as _json
            parsed = _json.loads(desc)
            bn = parsed.get("source_file_basename")
            if bn:
                batch_label = f"{bn} (Batch #{batch['batch_id']})"
        except Exception:
            pass  # source_descriptor is sometimes plain text (correction trigger note)

    lines.append(f"# {batch_label}")
    lines.append("")

    # Batch metadata table
    meta_rows = [
        [_t(labels, "kind"), str(batch.get("kind") or "—")],
        [_t(labels, "source"), str(batch.get("source") or "—")],
        [_t(labels, "status"), _t(labels, batch.get("status") or "") or "—"],
        [_t(labels, "started_at"), _ts(batch.get("started_at"))],
        [_t(labels, "finished_at"), _ts(batch.get("finished_at"))],
        [_t(labels, "last_heartbeat_at"), _ts(batch.get("last_heartbeat_at"))],
        [_t(labels, "event_count"), str(batch.get("event_count") or 0)],
        [_t(labels, "quarantine_count"), str(batch.get("quarantine_count") or 0)],
        [_t(labels, "content_hash"), str(batch.get("content_hash") or "—")],
        [_t(labels, "policy"), str(batch.get("policy") or "—")],
    ]
    if batch.get("failure_class"):
        meta_rows.append([_t(labels, "failure_class"),
                          _t(labels, batch["failure_class"]) or batch["failure_class"]])
    lines.append("## Batch summary")
    lines.append("")
    lines.extend(_md_table(["Field", "Value"], meta_rows))
    lines.append("")

    # Render the report-shape-specific body
    shape = report.get("shape")
    if shape == "record_level":
        lines.extend(_render_record_level(report, labels))
    elif shape == "batch_level_failure":
        lines.extend(_render_batch_level_failure(report, labels))
    elif shape == "correction_summary":
        lines.extend(_render_correction_summary(report, labels))
    elif shape == "rollback_summary":
        lines.extend(_render_rollback_summary(report, labels))
    elif shape == "cancelled_partial_stub":
        lines.extend(_render_cancelled_partial_stub(report, labels))
    elif shape is None:
        lines.append("## Quality report")
        lines.append("")
        lines.append(f"_The quality report is not yet available_: "
                     f"`{report.get('reason') or 'not yet written'}`")
        lines.append("")

    # Footer with _links (M3 K.5 retrofit pattern)
    if links:
        lines.append("---")
        lines.append("")
        lines.append("## Other formats")
        lines.append("")
        for fmt, url in sorted(links.items()):
            lines.append(f"- [{fmt.upper()}]({url})")
        lines.append("")

    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Per-shape body renderers
# ---------------------------------------------------------------------------


def _render_record_level(report: dict, labels: dict) -> list[str]:
    """Render the §7.1 record_level shape body."""
    out: list[str] = []
    totals = report.get("totals") or {}
    out.append("## " + _t(labels, "record_level"))
    out.append("")
    out.append("### Totals")
    out.append("")
    rows = []
    for key in ("records_seen", "events_written", "dedup_hits",
                "dedup_collisions_with_changed_payload",
                "quarantines", "provisional_fields_populated"):
        rows.append([_t(labels, key), str(totals.get(key, 0))])
    out.extend(_md_table(["Metric", "Count"], rows))
    out.append("")

    by_class = report.get("quarantines_summary_by_failure_class") or {}
    if by_class:
        out.append("### Quarantines by failure class")
        out.append("")
        rows = [[_t(labels, cls), str(n)] for cls, n in sorted(by_class.items())]
        out.extend(_md_table(["Failure class", "Count"], rows))
        out.append("")

    samples = report.get("quarantines_samples") or []
    if samples:
        out.append("### Quarantine samples")
        out.append("")
        rows = []
        for s in samples:
            rows.append([
                str(s.get("event_type") or "—"),
                _t(labels, s.get("failure_class") or "") or str(s.get("failure_class") or "—"),
                _t(labels, f"failure_step.{s.get('failure_step')}")
                    if s.get('failure_step') else "—",
                str(s.get("failure_message") or "—")[:200],
            ])
        out.extend(_md_table(
            ["Event type", "Failure class", "Step", "Message"], rows,
        ))
        out.append("")

    dedup_col = report.get("dedup_collisions") or []
    if dedup_col:
        out.append("### Dedup collisions (changed payload)")
        out.append("")
        rows = []
        for d in dedup_col:
            rows.append([
                str(d.get("natural_key") or "—"),
                str(d.get("stored_payload_canonical_sha256_prefix") or "—"),
                str(d.get("new_payload_canonical_sha256_prefix") or "—"),
            ])
        out.extend(_md_table(
            ["Natural key", "Stored payload (sha prefix)", "New payload (sha prefix)"],
            rows,
        ))
        out.append("")

    nkprefix = report.get("dedup_summary_by_natural_key_prefix") or {}
    if nkprefix:
        out.append("### Duplicate-records-skipped grouped by natural-key prefix")
        out.append("")
        rows = [[k, str(v)] for k, v in sorted(nkprefix.items(),
                                               key=lambda kv: kv[1], reverse=True)]
        out.extend(_md_table(["Prefix", "Count"], rows))
        out.append("")

    tf = report.get("terminal_failure")
    if tf:
        out.append("### Terminal failure")
        out.append("")
        rows = []
        for key in ("failure_class", "underlying_class", "record_index_in_batch",
                    "natural_key", "rolled_back_event_count"):
            val = tf.get(key)
            if key in ("failure_class", "underlying_class") and val:
                val = _t(labels, val) or val
            rows.append([_t(labels, key), str(val) if val is not None else "—"])
        out.extend(_md_table(["Field", "Value"], rows))
        out.append("")
        if tf.get("failure_message"):
            out.append(f"_Message_: `{tf['failure_message']}`")
            out.append("")

    return out


def _render_batch_level_failure(report: dict, labels: dict) -> list[str]:
    """Render the §7.2 batch_level_failure shape body."""
    out: list[str] = []
    out.append("## " + _t(labels, "batch_level_failure"))
    out.append("")
    cls = report.get("failure_class")
    if cls:
        out.append(f"**{_t(labels, cls) or cls}**")
        out.append("")
    ctx = report.get("failure_context") or {}
    if ctx:
        out.append("### Context")
        out.append("")
        rows = []
        for key, value in sorted(ctx.items()):
            rows.append([_t(labels, key), str(value)[:200]])
        out.extend(_md_table(["Field", "Value"], rows))
        out.append("")
    return out


def _render_correction_summary(report: dict, labels: dict) -> list[str]:
    """Render the §7.5 correction_summary shape body."""
    out: list[str] = []
    out.append("## " + _t(labels, "correction_summary"))
    out.append("")
    rows = []
    for key in ("event_id", "natural_key", "prev_version", "new_version",
                "parent_batch_id"):
        rows.append([_t(labels, key), str(report.get(key)) if report.get(key) is not None else "—"])
    out.extend(_md_table(["Field", "Value"], rows))
    out.append("")
    diff = report.get("payload_diff_keys") or []
    if diff:
        out.append(f"_{_t(labels, 'payload_diff_keys')}_: " + ", ".join(f"`{k}`" for k in diff))
        out.append("")
    if report.get("trigger_note"):
        out.append(f"_{_t(labels, 'trigger_note')}_: {report['trigger_note']}")
        out.append("")
    return out


def _render_rollback_summary(report: dict, labels: dict) -> list[str]:
    """Render the §7.6 rollback_summary shape body."""
    out: list[str] = []
    out.append("## " + _t(labels, "rollback_summary"))
    out.append("")
    rows = []
    for key in ("target_batch_id", "rolled_back_event_count"):
        rows.append([_t(labels, key), str(report.get(key)) if report.get(key) is not None else "—"])
    out.extend(_md_table(["Field", "Value"], rows))
    out.append("")
    if report.get("trigger_note"):
        out.append(f"_{_t(labels, 'trigger_note')}_: {report['trigger_note']}")
        out.append("")
    return out


def _render_cancelled_partial_stub(report: dict, labels: dict) -> list[str]:
    """Render the §7.7 cancelled_partial_stub shape body."""
    out: list[str] = []
    out.append("## " + _t(labels, "cancelled_partial_stub"))
    out.append("")
    rows = [
        [_t(labels, "rolled_back_event_count"), str(report.get("rolled_back_event_count") or 0)],
        [_t(labels, "swept_at"), _ts(report.get("swept_at"))],
        [_t(labels, "swept_by"), str(report.get("swept_by") or "—")],
    ]
    out.extend(_md_table(["Field", "Value"], rows))
    out.append("")
    return out
