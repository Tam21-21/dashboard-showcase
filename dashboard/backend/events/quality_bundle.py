"""M2.5 brief F — build_quality_report_bundle.

Spec brief v4 §7.3 / EL45. The renderer input shape: a dict carrying the
batch row, the parsed quality_report JSON, an optional `_links` block the
/export route populates per the M3 K.5 retrofit pattern, and the
render-labels translation table render_md / render_html consume.

Public surface
--------------
build_quality_report_bundle(batch_id, *, events_db_path=None,
                            include_source=True,  # for share-toggle gating
                            include_settings=True,
                            include_logs=False) -> dict

Bundle shape:
  {
    "batch":    { the §6.3 row dict, minus quality_report and large blobs },
    "report":   { the §7.1 / §7.2 / §7.5 / §7.6 / §7.7 shape parsed from JSON },
    "labels":   { the render_labels.LABELS dict (the renderers reference) },
    "_links":   { populated by the /export route, not by this builder },
  }

Exceptions
----------
BundleError       — batch_id not found (the route maps to 404)
BundleStillRunning — batch is still status='running' (the route maps to 409)

The builder does NOT decide whether the share-flag gates run (the route
extracts them from the share row and passes the bool kwargs in); it merely
omits fields from the `batch` slot when the corresponding flag is False so
the share renderer sees a smaller shape.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from .. import eventsdb
from ..research import render_labels


class BundleError(LookupError):
    """No research_event_batches row with the given batch_id."""


class BundleStillRunning(RuntimeError):
    """Batch is still status='running'; quality_report is NULL.

    The §6.5 /quality endpoint maps to 409 batch_still_running per the
    M2 K.5-style "fixture not yet available" pattern.
    """


# Batch-row columns surfaced in the bundle's `batch` slot. quality_report is
# intentionally EXCLUDED from this list because the parsed object goes in the
# bundle's `report` slot instead — a renderer that read both would risk drift.
_BATCH_COLS_ALL: tuple[str, ...] = (
    "batch_id",
    "kind",
    "source",
    "policy",
    "parent_batch_id",
    "request_id",
    "source_descriptor",
    "content_hash",
    "started_at",
    "finished_at",
    "last_heartbeat_at",
    "status",
    "event_count",
    "quarantine_count",
    "failure_class",
    "failure_context",
    "realized_start_at",
    "realized_end_at",
)


def build_quality_report_bundle(
    batch_id: int,
    *,
    events_db_path: Union[str, Path, None] = None,
    include_source: bool = True,
    include_settings: bool = True,
    include_logs: bool = False,
) -> dict:
    """Return the renderer input dict for a batch's quality report.

    Reads the batch row + parsed quality_report from events.db. The share-flag
    kwargs (include_source / include_settings / include_logs) gate which
    fields appear in the `batch` slot for the share-bundle path; the
    owner-side /quality + /export routes pass all True so they see the
    full shape.

    Raises BundleError on missing batch_id; BundleStillRunning when the batch
    is still status='running' (quality_report is NULL on the row by design
    per §6.5 — the close UPDATE is the only path that writes it).
    """
    with eventsdb.get_events_conn(events_db_path) as conn:
        row = conn.execute(
            f"""SELECT {', '.join(_BATCH_COLS_ALL)}, quality_report
                  FROM research_event_batches
                 WHERE batch_id = ?""",
            (batch_id,),
        ).fetchone()
    if row is None:
        raise BundleError(f"batch_id={batch_id} not found")
    if row["status"] == "running":
        raise BundleStillRunning(f"batch_id={batch_id} is still running")

    # Build the batch slot. Gate fields by the share-flag kwargs per spec §6.7's
    # repurposed flag semantics:
    #   include_source   → include `source_descriptor`
    #   include_settings → include `policy` + `request_id` (the request criteria
    #                       proxy; the full request row is the fetcher's
    #                       responsibility, not the share bundle's)
    #   include_logs     → include full quality_report (otherwise omit the
    #                       quarantines_samples + dedup_collisions arrays from
    #                       the report slot for the share path; owner /quality
    #                       always sees the full set)
    batch: dict = {col: row[col] for col in _BATCH_COLS_ALL}
    if not include_source:
        batch["source_descriptor"] = None
    if not include_settings:
        batch["policy"] = None
        batch["request_id"] = None

    # Parse the quality_report JSON. The batch row may carry quality_report=NULL
    # on a `cancelled_partial` batch whose secondary sweep has not yet run per
    # §7.0.2 — the renderer is expected to handle that case (the renderers below
    # branch on `shape` and render an "incomplete" stub when shape is None).
    if row["quality_report"] is None:
        report: dict = {"shape": None, "reason":
                        "quality_report not yet written (cancelled_partial pre-sweep)"}
    else:
        report = json.loads(row["quality_report"])
        # Trim the verbose sample arrays under the share-flag gate (include_logs).
        # The owner /quality + /export paths pass include_logs=True so they see
        # the full set; shared bundles default to logs=False per L25.
        if not include_logs and report.get("shape") == "record_level":
            # Mutate a shallow copy so the cached canonical_json bytes in the DB
            # are not affected.
            report = {**report}
            report["quarantines_samples"] = []
            report["dedup_collisions"] = []

    return {
        "batch": batch,
        "report": report,
        "labels": dict(render_labels.LABELS),
        "_links": {},   # populated by the /export route per K.5 retrofit pattern
    }
