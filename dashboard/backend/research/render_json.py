"""JSON renderer for K.5 (M3 spec §5.2, arch §21 — full-fidelity, snake_case).

The machine-readable export: takes the canonical bundle dict from
build_run_bundle and serialises it pretty-printed with strict JSON semantics
(allow_nan=False, ensure_ascii=False) — invalid floats raise rather than emit
the non-standard "NaN"/"Infinity" tokens that lenient parsers swallow but the
spec rejects. Round-trips cleanly through any strict JSON parser.

Per L23, JSON is the full-fidelity machine-readable artifact; per L28, JSON
keeps snake_case (the renderer applies no label translation — that is the MD/
HTML renderers' job).
"""
from __future__ import annotations  # postponed type-hint evaluation

import json  # the standard library encoder; no third-party JSON lib


def render_json(bundle: dict) -> bytes:
    """Serialize the canonical run bundle as pretty-printed strict UTF-8 JSON.

    allow_nan=False raises on NaN/Infinity (invalid per the JSON spec); the
    bundle upstream is expected to have normalised NaN to None already, so this
    is the loud guard if normalisation ever regresses. indent=2 matches §5.2's
    "pretty-printed" requirement.
    """
    # sort_keys=False preserves the bundle's insertion order, which is the order
    # build_run_bundle uses to organise the dict (run, stats, strategy, dataset,
    # equity, drawdowns, price, ...). Sorting would shuffle that organisation.
    return json.dumps(
        bundle,
        indent=2,  # pretty-printed per §5.2
        ensure_ascii=False,  # write UTF-8 characters as-is, smaller and human-readable
        allow_nan=False,  # strict: NaN/Inf raise ValueError rather than emit invalid JSON
        sort_keys=False,  # preserve the build_run_bundle dict ordering
    ).encode("utf-8")
