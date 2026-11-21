"""Research lab routes.

Two routers in one file:
- browser_router: behind Caddy basic auth, used by the dashboard frontend
- worker_router: protected by X-Worker-API-Key header, used by the laptop worker
"""
from __future__ import annotations
import ast                                                     # lane DV (#424): reads a strategy's baked quantile constants from stored TEXT, never imports or executes it (AC-4)
import asyncio
import hmac
import itertools                                               # lane RM (set 16b, plan amendment 7): the full cartesian product of a param_grid, for the no-surviving-combination pre-check
import json
import logging                                                # stranded-job cleaner step 1: structured ops-monitor log lines (below)
import os
import re                                                    # EDGAR quarter-range validation (mirrors the fetcher's _QUARTER_RANGE_RE)
import secrets                                                # mint the fundamentals-backfill systemd instance token (§3.3)
from dashboard.backend.events import company_profiles  # lane UNI1b: wake the SEC company-record reads after members are added
import sqlite3                                                # narrow the reap route's transient DB-locked failure to a 503
import subprocess                                            # benzinga dispatch via `sudo systemctl start`
import time
import urllib.error                                            # the cik-ticker-map route maps SEC fetch failures
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse  # StreamingResponse: M5 Track C worker artifact download
from pydantic import BaseModel, Field, field_validator, model_validator

from dashboard.backend import market_display
from dashboard.backend.routes import display as display_route
from dashboard.backend import artifact_store, catalog, dataset_sources, eventsdb, generated, plain_refusals, researchdb, stage_runner, strategy_mirror, tracker
from dashboard.backend.config import (
    EDGAR_BACKFILL_JOB_DIR,
    EVENT_LOADER_SOURCE_PREFIX,
    FUNDAMENTALS_JOB_DIR,
    RESEARCH_DASHBOARD_BASE_URL,
)
from dashboard.backend.events import bulk_reparse as event_bulk_reparse
from dashboard.backend.events import catalog as event_catalog
from dashboard.backend.events import cik_ticker_map as event_cik_ticker_map
from dashboard.backend.events import feeds as event_feeds
from dashboard.backend.events import universe as event_universe
from dashboard.backend.events import sweeps as event_sweeps
from dashboard.backend.events import quality_bundle as event_quality_bundle
from dashboard.backend.events import write_core as event_write_core
from dashboard.backend.events.fundamentals import concepts as fundamentals_concepts
from dashboard.backend.events.fundamentals import definitions as fundamentals_definitions
from dashboard.backend.events.fundamentals import inspect as fundamentals_inspect
from dashboard.backend.events.fundamentals import materializer as fundamentals_materializer
from dashboard.backend.events.fundamentals import poll as fundamentals_poll
from dashboard.backend.events.fundamentals import reads as fundamentals_reads
from dashboard.backend.events.fundamentals import screen as fundamentals_screen
from dashboard.backend.events.fundamentals import write as fundamentals_write
from dashboard.backend.events.materializer import build as event_materializer
from dashboard.backend.events.materializer import output_schemas as event_output_schemas
from dashboard.backend.events.errors import (
    CorrectionAgainstRunningBatch as _CorrectionAgainstRunningBatch,
    CorrectionAltersCompanyLinks as _CorrectionAltersCompanyLinks,
    LoaderEnvelopeInvalid as _LoaderEnvelopeInvalid,
    LoaderFileUnreadable as _LoaderFileUnreadable,
    LoaderJsonDecodeError as _LoaderJsonDecodeError,
    PayloadValidationError as _PayloadValidationError,
    TickerNotFoundInBridge as _TickerNotFoundInBridge,
)
from dashboard.backend.events.loaders import jsonl_canonical as event_loader_jsonl
from dashboard.backend.events.renderers import (
    render_quality_html as event_render_quality_html,
    render_quality_json as event_render_quality_json,
    render_quality_md as event_render_quality_md,
)
from dashboard.backend.research import earnings_reaction, events_browse, events_read, export, preview, render_html, render_json, render_md
from dashboard.backend.sse import broadcaster

# Per-format dispatch table for K.5 /export: renderer + Content-Type + filename
# extension. Keeps the route handler short and the format set easy to audit.
_EXPORT_FORMATS: dict[str, tuple[Any, str, str]] = {
    "json": (render_json.render_json, "application/json; charset=utf-8", "json"),
    "md":   (render_md.render_md,     "text/markdown; charset=utf-8",   "md"),
    "html": (render_html.render_html, "text/html; charset=utf-8",       "html"),
}

# M2.5 brief F per-format dispatch for the event-batch /quality + /export +
# public-share routes. Separate from _EXPORT_FORMATS because the renderer
# function signatures differ (quality renderers take a `bundle` dict, K.5
# renderers take a richer run-bundle shape); a single dict would conflate
# the two contracts.
_QUALITY_EXPORT_FORMATS: dict[str, tuple[Any, str, str]] = {
    "json": (event_render_quality_json.render_quality_json,
             "application/json; charset=utf-8", "json"),
    "md":   (event_render_quality_md.render_quality_md,
             "text/markdown; charset=utf-8", "md"),
    "html": (event_render_quality_html.render_quality_html,
             "text/html; charset=utf-8", "html"),
}


def _export_url(scope: str, identifier: str | int, fmt: str) -> str:
    """Build an absolute export URL for the given scope.

    Scopes:
      'runs'           — M3 K.5 owner-side run export route
      'share'          — M3 K.6 public share dispatch route (works for both
                          runs and batches; the route dispatches internally)
      'event-batches'  — M2.5 brief F owner-side batch export route

    Used to populate bundle["_links"] before rendering so render_md / HTML
    footers can point readers at the other formats of the same export. The
    origin comes from RESEARCH_DASHBOARD_BASE_URL (env-derived; see
    config.py) — not request.base_url (which is the loopback origin without
    proxy headers trusted).
    """
    if scope == "runs":
        return f"{RESEARCH_DASHBOARD_BASE_URL}/api/research/runs/{identifier}/export?format={fmt}"
    if scope == "share":
        return f"{RESEARCH_DASHBOARD_BASE_URL}/api/research/share/{identifier}?format={fmt}"
    if scope == "event-batches":
        return f"{RESEARCH_DASHBOARD_BASE_URL}/api/research/event-batches/{identifier}/export?format={fmt}"
    raise ValueError(f"unknown export scope: {scope!r}")  # programming bug, not user input


# -------- Auth dependency for worker routes --------

def require_worker_api_key(x_worker_api_key: str = Header(default="")):
    """Constant-time comparison to avoid timing-attack side channels."""
    expected = os.environ.get("RESEARCH_WORKER_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="server API key not configured")
    if not hmac.compare_digest(x_worker_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid worker API key")


# -------- Tracker refusal mapping (L1-F1) --------

def _handle_tracker_refusal(exc: Exception) -> None:
    """Map every tracker refusal class to a 409 response with a reason word and detail.

    Raises HTTPException with status 409 for all tracker exceptions. Each exception is
    mapped to a response detail that begins with a reason word (hyphenated for exceptions
    that don't already have one) followed by the exception message. This centralizes the
    mapping used by _dispatch_run_backtest, dispatch_price_universe_sweep_route, and
    dispatch_stage_route, so every refusal class the tracker grows cannot miss a route.

    Raises:
        HTTPException(409) for tracker.ReserveCutCrossed, tracker.ChronologyRefused,
                        tracker.DrawWindowMissing, tracker.RunLinkFailed
    """
    if isinstance(exc, tracker.ReserveCutCrossed):
        # ReserveCutCrossed already formats its message with the reason word prefix
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, tracker.ChronologyRefused):
        log.warning("chronology-refused: %s", exc)
        raise HTTPException(
            status_code=409, detail=f"chronology-refused: {plain_refusals.sentence(exc)}"
        ) from exc
    if isinstance(exc, tracker.DrawWindowMissing):
        log.warning("draw-window-missing: %s", exc)
        raise HTTPException(
            status_code=409, detail=f"draw-window-missing: {plain_refusals.sentence(exc)}"
        ) from exc
    if isinstance(exc, tracker.RunLinkFailed):
        log.warning("run-link-failed: %s", exc)
        raise HTTPException(
            status_code=409, detail=f"run-link-failed: {plain_refusals.sentence(exc)}"
        ) from exc
    # Not a tracker refusal; let the caller decide what to do
    raise TypeError(f"not a tracker refusal: {type(exc).__name__}")


# Module-level tuple of all tracker refusal classes. Every dispatch route that maps tracker
# exceptions must catch this tuple; a new refusal class the tracker grows will redden the
# enumeration probe until it is added here, ensuring closure over every refusal type that
# exists at build time. This constant is the source of truth for the tuple all except
# clauses use, avoiding the hand-enumeration the lead's L1-F1 finding required fixing.
_TRACKER_REFUSALS = (
    tracker.ChronologyRefused,
    tracker.DrawWindowMissing,
    tracker.RunLinkFailed,
    tracker.ReserveCutCrossed,
)


# Lane CN (set 16c, `#427`, spec finding F-4): the quantile pre-check (QuantileOutOfRange,
# QUANTILE_PRECHECK_MONITOR_COUNTERS, and every helper below it) MOVED to
# dashboard/backend/research_prechecks.py, a module both this route and the new chain runner
# import, never a second copy. Re-imported here by name so every existing reference in this file
# (and every probe reaching it as `routes.research.<name>`) keeps working unchanged: a plain
# Python import binds the SAME dict/class object, so `routes.research.QUANTILE_PRECHECK_MONITOR_COUNTERS`
# is still the one shared counter research_prechecks.py itself increments.
from dashboard.backend.research_prechecks import (  # noqa: E402
    QuantileOutOfRange,
    QUANTILE_PRECHECK_MONITOR_COUNTERS,
    _refuse_bad_quantiles_in_grid,
    _refuse_bad_baked_quantiles_in_source,
)


# Lane RM (set 16b, plan amendment 7, docs/units/RM/criteria.md AC-4b/AC-4d): two more grid
# refusals the survey found were missing, both root-caused by reading rather than guessed (once
# root cause 1's lead-in fix stopped masking them behind an unrelated crash, the real, distinct
# failure each one produces was read straight off the worker's own job error text).
GRID_SHAPE_MONITOR_COUNTERS: dict[str, int] = {
    "no_surviving_combination_refusals": 0,
    "non_positive_holding_cap_refusals": 0,
    # Lane RM round 3 (docs/units/RM/reads/read_1.md, R2, BLOCKER): a condition whose ast the
    # restricted evaluator below refuses (anything outside Compare/BoolOp/Not/Name/numeric
    # Constant). The pre-check is skipped for that strategy and dispatch proceeds as before; this
    # counts how often that happens so a strategy nobody can pre-check is visible, not silent.
    "unsafe_condition_precheck_skips": 0,
    # Lane GC (set 16c, docs/units/GC/criteria.md AC-1): a grid whose combination count is
    # counted (never walked) above _GRID_PRECHECK_WALK_CAP_COMBINATIONS. Dispatch proceeds exactly
    # as it does for the unsafe-condition skip just above; this counts how often that happens so a
    # grid nobody pre-checked here stays visible, not silent.
    "grid_precheck_walk_skipped_above_cap": 0,
}


# #204 operational dial, not a backtest parameter: it only decides when THIS request-time
# pre-check declines to run, never what any backtest computes, so it carries no
# parameter_provenance entry (the same class researchdb.py's own _SWEEP_SIZE_CAP_CELLS comment
# names for a job-config number). The per-combination walk below runs SYNCHRONOUSLY inside the
# request handler (one hand-written ast interpreter call per combination, `_eval_safe_condition`),
# measured with a throwaway benchmark during this build at about 0.75 microseconds/combination
# (2,002,225 combinations walked in 1.488s on this box). Walking the full 50,000,000-cell sweep
# size cap (`researchdb._SWEEP_SIZE_CAP_CELLS`) here would therefore block a request for roughly
# 35-40 seconds; 1,000,000 keeps the worst case near one second, comfortably at or below that cap,
# while still catching the oversized grids this pre-check exists to refuse early. Raising it only
# costs request latency, never correctness.
_GRID_PRECHECK_WALK_CAP_COMBINATIONS = 1_000_000


_ALLOWED_COMPARE_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)  # <, <=, >, >=, ==, !=


def _condition_is_safe(node: "ast.AST", grid_keys: "set[str]") -> bool:
    """R2 fix (lane RM round 3, BLOCKER): a strict, hand-written STRUCTURAL check over an
    already-parsed condition's ast, run once before any combination is evaluated. Returns True
    only when every node in the tree is one of: the Expression wrapper `ast.parse(..., mode=
    "eval")` produces, a Compare using <, <=, >, >=, == or != , a BoolOp And/Or, a UnaryOp Not, a
    Name that is a key of the caller's own grid, or a numeric Constant (int or float, never a bool
    and never a string). Anything else -- a Call, an Attribute, a Subscript, a Lambda, a
    comprehension, or a Name that is not one of the grid's own parameters -- returns False.

    This is why it is safe where `eval(condition, {"__builtins__": {}}, local_ns)` (the previous
    code here) was not: stripped builtins are not a sandbox in CPython (a hostile string can still
    reach a live class through plain attribute-and-subscript chains, e.g. `().__class__.__bases__
    [0].__subclasses__()`, with zero builtins in scope), and a stored strategy's source is meant to
    be PARSED, never EXECUTED, on the backend (the OPEN-4 discipline `_read_sweep_conditions`'s own
    docstring already cites for this same file's source text). This function never calls eval,
    exec or compile; it only inspects node TYPES, so nothing a condition string names can ever run
    as code here, whatever it says."""
    if isinstance(node, ast.Expression):
        return _condition_is_safe(node.body, grid_keys)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        return all(_condition_is_safe(value, grid_keys) for value in node.values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _condition_is_safe(node.operand, grid_keys)
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1 and len(node.comparators) == 1
        and isinstance(node.ops[0], _ALLOWED_COMPARE_OPS)
    ):
        return _condition_is_safe(node.left, grid_keys) and _condition_is_safe(node.comparators[0], grid_keys)
    if isinstance(node, ast.Name):
        return node.id in grid_keys  # only a name the caller's OWN grid declares ever resolves
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return True
    return False  # every other node shape (Call, Attribute, Subscript, Lambda, ...) is refused


def _eval_safe_condition(node: "ast.AST", local_ns: dict) -> bool:
    """Evaluates an ast tree ALREADY proven safe by `_condition_is_safe` above (the caller never
    invokes this on an unchecked tree). A hand-written recursive interpreter over the SAME five
    node shapes `_condition_is_safe` allows, never Python's own eval/exec/compile, so nothing
    outside this function's own explicit branches can ever run regardless of what the condition
    string says."""
    if isinstance(node, ast.Expression):
        return _eval_safe_condition(node.body, local_ns)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_safe_condition(value, local_ns) for value in node.values)
        return any(_eval_safe_condition(value, local_ns) for value in node.values)  # ast.Or
    if isinstance(node, ast.UnaryOp):  # ast.Not is the only unary op _condition_is_safe allows
        return not _eval_safe_condition(node.operand, local_ns)
    if isinstance(node, ast.Compare):
        left = _eval_safe_operand(node.left, local_ns)
        right = _eval_safe_operand(node.comparators[0], local_ns)
        op = node.ops[0]
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.Eq):
            return left == right
        return left != right  # ast.NotEq, the last of the six allowed compare ops
    raise AssertionError(f"unreachable: {ast.dump(node)} passed _condition_is_safe but no branch handles it")


def _eval_safe_operand(node: "ast.AST", local_ns: dict) -> "int | float":
    """The leaf case for `_eval_safe_condition`'s own Compare branch: only a Name (already proven
    to be a grid key by `_condition_is_safe`) or a numeric Constant ever reaches here."""
    if isinstance(node, ast.Name):
        return local_ns[node.id]
    return node.value  # ast.Constant, the only other shape _condition_is_safe allows as an operand


class NoSurvivingCombination(RuntimeError):
    """AC-4(b): a strategy's own declared `sweep_conditions` (band_cap's
    `exit_quantile < entry_quantile`, research_lab/generator/emit.py) filters out EVERY
    combination in the caller's grid. Left unchecked, this dispatches successfully and then
    crashes deep inside vbt.Param's own condition machinery with `ValueError: Set of parameter
    combinations is empty` (docs/units/RM/reads/probe_report_2026-09-25_1.md's own follow-up
    finding), a message that never reaches this route's own exception map and never names the
    real cause."""

    def __init__(self, conditions: "tuple[str, ...]") -> None:
        self.conditions = conditions
        super().__init__(
            "no_surviving_combination: no combination in this grid satisfies "
            f"{' and '.join(conditions)!r}; refusing before any draw is requested"
        )


class NonPositiveHoldingCap(RuntimeError):
    """AC-4(d): a holding_cap of zero (or less) exits every position the very next bar after it
    opens (research_lab/generator/emit.py's own `_apply_holding_cap`: `bars_open >= holding_cap`
    is already true the first bar a position is held), which is a degenerate, never-useful
    trading rule rather than a crash, so nothing downstream ever refuses it on its own; the
    criterion's own text asks for it to be named and refused before any draw is requested."""

    def __init__(self, value: object) -> None:
        self.value = value
        super().__init__(
            f"non_positive_holding_cap: holding_cap={value!r} must be a positive number of bars "
            "(a cap of zero or less holds no position at all); refusing before any draw is requested"
        )


def _refuse_non_positive_holding_cap(param_grid: dict) -> None:
    """AC-4(d): refuse the FIRST holding_cap value that is not a positive number, before any
    draw is requested. Generic by grid key, like _refuse_bad_quantiles_in_grid above: fires only
    when the caller's own grid actually carries a holding_cap key, so a hand-authored strategy
    with no such parameter (ma_cross) is never touched by this check."""
    for value in param_grid.get("holding_cap", []):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            GRID_SHAPE_MONITOR_COUNTERS["non_positive_holding_cap_refusals"] += 1
            raise NonPositiveHoldingCap(value)


def _read_sweep_conditions(source_code: str) -> "tuple[str, ...]":
    """AC-4(b): reads the strategy's own declared `sweep_conditions = (...)` class attribute from
    its stored source TEXT via `ast`, never imported or executed (the same OPEN-4 discipline
    stage_runner._read_quantile_min_bars already uses for a different baked constant on this
    file's neighbour module). Returns the tuple of condition strings verbatim (each one a plain
    boolean expression over the grid's own parameter names, e.g. "exit_quantile < entry_quantile",
    the literal text research_lab/generator/emit.py's own template bakes), or an empty tuple for a
    strategy that declares no such attribute at all (a hand-authored strategy, e.g. ma_cross,
    which therefore never has anything for this check to enforce)."""
    tree = ast.parse(source_code)
    for node in ast.walk(tree):
        is_conditions_assign = (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "sweep_conditions"
        )
        if is_conditions_assign and isinstance(node.value, (ast.Tuple, ast.List)):
            return tuple(
                elt.value for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            )
    return ()  # no declared conditions: nothing for AC-4(b) to check


def _refuse_no_surviving_combination(strat_row: "dict | None", param_grid: dict) -> None:
    """AC-4(b): refuse before any draw is requested when NO combination in the full cartesian
    product of `param_grid`'s own values satisfies every one of the strategy's own declared
    `sweep_conditions`. Each condition is evaluated over only that one combination's named values
    (mirroring what vbt.Param's own `condition=` keyword does internally, research_lab/generator/
    emit.py's own `sweep_signals`), through `_eval_safe_condition`'s own hand-written interpreter,
    never Python's `eval` (R2 fix, lane RM round 3, BLOCKER: a condition string is generator-
    authored, stored TEXT, but stripped builtins are not a sandbox, and stored source is meant to
    be PARSED, never EXECUTED, on the backend; `_read_sweep_conditions`'s own docstring already
    cites the same OPEN-4 boundary this fix now actually holds to).

    A strategy row that does not resolve (None), one with no declared conditions, or an empty
    grid are all left alone: this check has nothing to enforce for any of those, and each is
    already another check's own business (StrategyNotFound, an empty/malformed grid's own
    ValueError) further down the same call path. A condition whose ast is NOT provably safe (any
    node `_condition_is_safe` refuses) means the pre-check cannot run for this strategy at all:
    dispatch proceeds exactly as it did before this check existed, and the skip is counted rather
    than silently absorbed, because a strategy nobody can pre-check is a fact worth seeing.

    Lane GC (set 16c, docs/units/GC/criteria.md AC-1): a grid whose own combination count exceeds
    `_GRID_PRECHECK_WALK_CAP_COMBINATIONS` is never walked at all, counted first, before any walk
    and before the conditions are even parsed. Dispatch proceeds exactly as the unsafe-condition
    skip above does; the skip is counted separately."""
    if strat_row is None:
        return
    conditions = _read_sweep_conditions(strat_row["source_code"])
    if not conditions:
        return
    names = sorted(n for n in param_grid if param_grid.get(n))
    if not names:
        return  # an empty/malformed grid: a different check's business, not this one's to guess at
    grid_keys = set(names)

    # Lane GC (set 16c, docs/units/GC/criteria.md AC-1): count the full cartesian product's size
    # BEFORE any walk, the same way researchdb.dispatch_sweep already counts a sweep's own total
    # cells before dispatching it. Above the bound this pre-check cannot afford to walk (see
    # _GRID_PRECHECK_WALK_CAP_COMBINATIONS's own comment for why), so it steps aside exactly as
    # the unsafe-condition skip below does: dispatch proceeds, and the skip is counted so a grid
    # nobody pre-checked here stays visible rather than silent.
    combo_count = 1
    for n in names:
        combo_count *= len(param_grid[n])
    if combo_count > _GRID_PRECHECK_WALK_CAP_COMBINATIONS:
        GRID_SHAPE_MONITOR_COUNTERS["grid_precheck_walk_skipped_above_cap"] += 1
        return

    trees: "list[ast.AST]" = []
    for condition in conditions:
        try:
            tree = ast.parse(condition, mode="eval")
        except SyntaxError:
            tree = None
        if tree is None or not _condition_is_safe(tree, grid_keys):
            # The condition's shape is not one this restricted evaluator can prove safe (or it is
            # not even valid Python): skip the pre-check for this strategy entirely, never run
            # eval, and let dispatch proceed as it would with no sweep_conditions at all.
            GRID_SHAPE_MONITOR_COUNTERS["unsafe_condition_precheck_skips"] += 1
            return
        trees.append(tree)
    for combo in itertools.product(*(param_grid[n] for n in names)):
        local_ns = dict(zip(names, combo))
        if all(_eval_safe_condition(tree, local_ns) for tree in trees):
            return  # at least one combination survives: nothing to refuse
    GRID_SHAPE_MONITOR_COUNTERS["no_surviving_combination_refusals"] += 1
    raise NoSurvivingCombination(conditions)


# _read_baked_quantile_defaults and _refuse_bad_baked_quantiles_in_source (the single-run route's
# own baked-constant half of the quantile pre-check) also MOVED to research_prechecks.py in the
# same lane CN move (F-4); both are imported back above, beside QuantileOutOfRange.


# -------- Datasets helpers & constants (M2) --------

def parse_iso_to_unix(s: str) -> int:
    """Parse ISO 8601 with required timezone -> unix seconds UTC.

    Python 3.11+ datetime.fromisoformat handles 'Z' natively.
    Naive (no-tz) strings are rejected: .timestamp() on a naive datetime
    interprets it as LOCAL time, which silently corrupts the stored ts.
    Wire format is locked to ISO 8601 with 'Z' per M2 spec section 6 P0.12.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"ISO 8601 must include timezone (use 'Z'): {s!r}")
    return int(dt.timestamp())


_WHERE_BY_STATUS = {
    "active":  "WHERE status != 'deleted'",
    "deleted": "WHERE status = 'deleted'",
    "all":     "",
}


# -------- Settings helpers (M2) --------

def validate_value(value: str, value_type: str) -> None:
    """Validate that `value` (TEXT form) parses according to `value_type`.

    Raises HTTPException(400) with a clear message on user-input failure.
    Raises HTTPException(500) on unknown stored value_type (schema bug).
    Used by PUT /settings/{scope}/{key} after serializing the typed input.
    """
    if value_type == "string":
        return
    if value_type == "number":
        try:
            float(value)
        except (ValueError, TypeError):
            raise HTTPException(
                400, f"value_type=number requires numeric input, got {value!r}"
            )
        return
    if value_type == "boolean":
        if value not in ("true", "false"):
            raise HTTPException(
                400, f"value_type=boolean requires 'true' or 'false', got {value!r}"
            )
        return
    if value_type == "json":
        try:
            json.loads(value)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"value_type=json requires valid JSON: {e}")
        return
    # Unknown stored value_type — schema bug, not user error.
    raise HTTPException(500, f"unsupported value_type stored: {value_type!r}")


def coerce_to_text(value: Any, value_type: str) -> str:
    """Coerce a typed JSON body value to the TEXT form stored in research_settings.value.

    Mirrors researchdb.serialize_value but raises HTTPException(400) instead
    of TypeError, since this runs against user input.
    """
    if value_type == "string":
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)
    if value_type == "number":
        if isinstance(value, bool):
            raise HTTPException(400, f"value_type=number requires numeric input, got bool {value!r}")
        if isinstance(value, (int, float)):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return repr(value) if isinstance(value, float) else str(value)
        if isinstance(value, str):
            return value  # validate_value will check it's numeric
        raise HTTPException(400, f"value_type=number requires numeric input, got {value!r}")
    if value_type == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        if value in ("true", "false"):
            return value
        raise HTTPException(400, f"value_type=boolean requires bool or 'true'/'false', got {value!r}")
    if value_type == "json":
        if isinstance(value, str):
            return value  # validate_value will check it's valid JSON
        return json.dumps(value)
    raise HTTPException(500, f"unsupported value_type stored: {value_type!r}")


# Child logger under main.py's root "dashboard" config (logging.basicConfig runs there); creating
# this object here never depends on that call having run yet, since a Logger is only a handle and
# handlers attach to the root at basicConfig time. Stranded-job cleaner step 1 uses it for the ops
# monitor rows this build owes (docs/ops/ops_monitor_registry.md) -- see worker_heartbeat and
# worker_poll below. No prior logger existed in this file; this is the first.
log = logging.getLogger("dashboard.research")

# worker_poll_latency sampling (stranded-job cleaner step 1). 30 polls is about one sample a minute
# at the worker's 2-second cadence. A mutable single-slot list rather than a module-level int
# because the counter is rebound on every poll and a bare `global` would be the only alternative;
# the route is single-process and the count is a sampling aid, so a lost increment under concurrency
# costs a sample and nothing else.
_POLL_SAMPLE_EVERY = 30
_poll_counter = [0]

# -------- Two routers --------

# Browser: no auth dep here (Caddy basic auth handles humans at the edge).
browser_router = APIRouter(prefix="/api/research", tags=["research"])

# Worker: every route under this router requires the API key.
worker_router = APIRouter(
    prefix="/api/research/worker",
    tags=["research-worker"],
    dependencies=[Depends(require_worker_api_key)],
)


# -------- Request bodies --------

class CreateJobBody(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # run_backtest dispatch (brief E) carries strategy_id + dataset_id instead of a raw
    # payload; the backend assembles the worker payload itself. Optional so other kinds
    # (fake, fetch_dataset) keep the generic payload path.
    strategy_id: Optional[int] = None
    dataset_id: Optional[int] = None
    # Optional user-supplied label for the run; stored on research_runs.name, never sent
    # to the worker. Trim whitespace; empty string is normalized to NULL at dispatch.
    name: Optional[str] = Field(default=None, max_length=200)
    # M4 R8/D5/MD2: a test run from the editor. True -> the run is tagged research_runs.kind='scratch'
    # and goes through the SAME dispatch pipeline (never a side channel); False -> a standard run.
    scratch: bool = False
    # Lane SR (night 2026-09-12, 6.2a, AC-3/DH13): which pipeline stage and which calendar this
    # single-run dispatch belongs to. Both default to the exact literal pair this route used to
    # hard-code ("screen"/"real"), so no existing caller's behaviour changes just because these
    # fields now exist: a client that posts neither still gets the milestone's screen-stage real
    # draw it always got.
    stage: Literal["screen", "optimise", "walkforward", "reserve"] = "screen"
    calendar_id: str = Field(default="real", min_length=1)


class CreateEventJobBody(BaseModel):
    """Dispatch an event-driven backtest (M3.5 brief I, OPEN-6 sibling surface).

    The event-run analogue of the run_backtest branch of CreateJobBody: a strategy plus a
    run-level universe and window. There is NO price dataset_id (an event run has none) and NO
    event_types field — event_types is read off the strategy at dispatch (OPEN-4), kept in one
    place. The window is epoch seconds UTC; knowledge_to is the no-lookahead bound (§4 / R10).
    """
    strategy_id: int
    universe_id: int
    knowledge_from: int = Field(..., ge=0)   # epoch s; window start (inclusive)
    knowledge_to: int = Field(..., ge=1)     # epoch s; window end / no-lookahead bound (exclusive)
    name: Optional[str] = Field(default=None, max_length=200)  # UI label only; never crosses to the worker
    # M4 R8/D5/MD2: a test run from the editor -> research_runs.kind='scratch', same dispatch pipeline.
    scratch: bool = False

    @model_validator(mode="after")
    def _window_ordered(self) -> "CreateEventJobBody":
        if self.knowledge_to <= self.knowledge_from:  # an empty/inverted window is a client bug -> 422
            raise ValueError("knowledge_to must be > knowledge_from")
        return self


class WorkerIdBody(BaseModel):
    worker_id: str


class HeartbeatBody(BaseModel):
    worker_id: str


class LogBody(BaseModel):
    level: str = "info"
    line: str


class CompleteBody(BaseModel):
    # Pydantic validates Literal types automatically — invalid status → 422.
    # 'cancelled' added in brief I so all three terminal outcomes finalize the job the same way
    # (run-callback + /jobs/{id}/complete). No research_jobs DDL — that column has no CHECK.
    status: Literal["succeeded", "failed", "cancelled"]
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class CreateDatasetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source: Literal["binance", "alpaca_sip", "eodhd", "synthetic"]  # eodhd = pre-downloaded EODHD daily (M5 data-store); synthetic = GV2-4a made-up stocks
    symbol: str = Field(..., min_length=1, max_length=50)
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"]
    start: str                                                   # ISO 8601 with Z
    end: str                                                     # ISO 8601 with Z

    @field_validator("start", "end")
    @classmethod
    def _iso_with_tz(cls, v: str) -> str:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            raise ValueError(f"ISO 8601 must include timezone (use 'Z'): {v!r}")
        return v

    @model_validator(mode="after")
    def _end_after_start(self) -> "CreateDatasetIn":
        if parse_iso_to_unix(self.end) <= parse_iso_to_unix(self.start):
            raise ValueError("end must be > start")
        return self


class StoreIdentityIn(BaseModel):
    """Night four lane S (job 2.4, decision 1 ruled (a)): the worker's own store-identity shape
    (research_lab/worker/store_identity.py's compute_store_identity return value: {"store_id":
    <str>, "store_fetched_at": <str>}). Carried as a NESTED object because fetch_dataset.py
    attaches it as result["store_identity"] = {...} (line 162), never as flat top-level result
    keys the way covered_start_ts/covered_end_ts are; the two fields are required WITHIN this
    submodel (compute_store_identity always returns both together, never one alone), while the
    submodel itself is optional on DatasetWorkerResultIn below (a non-eodhd fetch never computes
    one)."""
    store_id: str = Field(..., min_length=1)
    store_fetched_at: str = Field(..., min_length=1)


class DatasetWorkerResultIn(BaseModel):
    rows: int = Field(..., ge=1)
    sha256: str = Field(..., min_length=64, max_length=64)
    file_path: str = Field(..., min_length=1)
    file_bytes: int = Field(..., ge=1)
    # Dashboard-improvements W3 (true coverage): the ACTUAL fetched bar range (unix s UTC),
    # optional so an older worker that does not emit them still validates. The field names
    # MUST match the fetch.py result keys AND the research_datasets columns exactly — a
    # plain BaseModel silently drops unknown keys, so a name drift would write NULL forever.
    # No ge constraint: these are unix-second epochs the fetcher controls; rejecting an unexpected
    # value with a 422 would mark a successful fetch as failed. None (older worker / empty) -> NULL.
    covered_start_ts: int | None = None
    covered_end_ts: int | None = None
    # Night four lane S (job 2.4): the EODHD store identity an eodhd fetch stamps onto its own
    # result (fetch_dataset.py, AC-7). Optional so an older worker, or a non-eodhd fetch (alpaca_sip
    # / binance, which never reads this store), still validates with this left None, persisted as
    # NULL on both research_datasets columns below.
    store_identity: StoreIdentityIn | None = None


class DatasetWorkerFailedIn(BaseModel):
    error: str = Field(..., min_length=1, max_length=10_000)


# -------- Run worker-callback bodies (M3 brief E + I) --------

class RunResultIn(BaseModel):
    """Worker reports a succeeded run (brief D's report_run_result payload).

    Mirrors run_backtest.py's result dict. run_id is redundant with the path param;
    the path is authoritative. Extra container fields are ignored.
    """
    status: Literal["succeeded"]
    artifacts: dict[str, Any]      # six container-relative paths (§3.8)
    stats: dict[str, Any]          # eight research_run_stats keys (§3.5)
    environment: dict[str, Any]    # four meta.json env fields (§3.4)
    annualization: dict[str, Any] | None = None  # Phase-2 Unit 1: index-derived Sharpe annualization (price runs); None for event runs. Declared so RunResultIn does NOT silently drop it.
    funnel: dict[str, Any] | None = None  # Phase-2 Unit 3: the price-run signal funnel (counts + zero_reason); None for event/bespoke runs. Declared so Pydantic does NOT silently drop it.
    variation: dict[str, Any] | None = None  # lane DG (`6.1g`): the single run's own combo (param_json, lookback_bars, used_degrees_of_freedom); None for an undeclared strategy. Declared so Pydantic does NOT silently drop it.


class RunFailedIn(BaseModel):
    error: str = Field(..., min_length=1, max_length=10_000)


class RunCancelledIn(BaseModel):
    # Carries the terminal status so M5/M6 'cancelled_partial' reuse this endpoint unchanged.
    status: str = "cancelled"


class SweepChunkIn(BaseModel):
    """M5 1B (§6.1): one sweep chunk's cells, POSTed by the worker as it streams the container.

    Each cell is a free-form dict carrying variation_id, ticker, param_json, dataset_sha256, the eight
    metric keys, and the nine funnel counts + zero_reason — exactly what the engine emits on stdout. The
    backend (complete_sweep_chunk) reads the known keys; extra keys are ignored. A plain dict (not a typed
    sub-model) so the engine's stdout shape and the write path are the single contract, not duplicated here.
    """
    cells: list[dict[str, Any]]


class CellRefusalsIn(BaseModel):
    """Lane D1: refused sweep cells, POSTed by the worker from the engine's {"cell_refusals": [...]} lines.
    Each item carries variation_id, ticker, param_json, error, reason and message (record_cell_refusals
    checks every key)."""
    refusals: list[dict[str, Any]]


class SweepResultIn(BaseModel):
    """M5 1B (§7): the sweep finalize body (the container's sweep-done line), POSTed once at the end.

    A sweep writes no per-run stats/artifacts; this records the run-level annualization (one factor for a
    single-asset-class sweep, None for a mixed one, §15.1) + the environment + the total cell count.
    """
    annualization: dict[str, Any] | None = None  # run-level Sharpe annualization, or None (mixed-calendar, §15.1)
    environment: dict[str, Any]                   # the four meta.json env fields
    total_cells: int = 0                          # the engine's emitted cell count (for the SSE / operator)
    # Lane GV2-8b (DECISIONS #459 item 2): the bars a theory search or walk-forward run read, sent by the worker because
    # those runs write no funnel rows to total them from. None = the sum over the run's funnel rows, as before. 0 is a real count.
    bars_observed: int | None = Field(default=None, ge=0)


class WalkforwardResultIn(BaseModel):
    """Lane WF (6.2b, AC-19): the walk-forward relay body, POSTed once after sweep-result, carrying
    every {"walkforward_slice": ...} line the entrypoint emitted plus the run-level summary its
    final sweep-done line carried under its own "walkforward" key. A free-form dict per slice (not
    a typed sub-model), matching SweepChunkIn's own precedent immediately above: the engine's
    stdout shape and the write path are the single contract, not duplicated here.
    """
    slices: list[dict[str, Any]]           # one dict per slice_index, the entrypoint's own emitted shape
    in_sample_profit: float | None = None  # the run-level annualised figure (AC-17), or None (no slice ever ran)
    out_of_sample_profit: float | None = None
    wfa_boundary_buffer_bars: int = 0      # purge_bars + embargo_bars (guard G1's own boundary buffer)
    # Fold item 2c (round 1, BLOCKER LC2 / the lead's read): the five general research_run_stats
    # columns, computed by the entrypoint from the chained out-of-sample equity curve, replacing the
    # old hardcoded 0/0.0/0 first-insert. Defaulted so a stale worker image (predating this fold)
    # still relays successfully, writing the same honest zeros the OLD code wrote outright.
    total_return: float | None = None
    max_drawdown: float | None = None
    num_trades: int = 0
    total_fees_paid: float | None = None
    total_slippage_cost: float | None = None
    # Fold item 1 (round 1, BLOCKER LC1): the two slice-usage counts. Accepted here so the field is
    # explicit rather than silently dropped, but NOT persisted as a research_run_stats column (two
    # more nullable columns are NOT wanted for this) -- the route below only logs them.
    slices_used: int = 0
    slices_with_absent_profit: int = 0


class PutStrategyIn(BaseModel):
    source_code: str = Field(..., min_length=1)  # full .py source; description derived from its docstring
    display_name: str | None = None  # DN1: optional on an edit; the same source with a new name renames


class CreateStrategyIn(BaseModel):
    """POST /strategies — Add Strategy (D5). name is new (409 on an existing name); the
    description is derived from the source docstring (DECISIONS #18), never passed in."""
    name: str = Field(..., min_length=1)
    source_code: str = Field(..., min_length=1)  # full .py source
    display_name: str  # DN1: required; the name pages show (blank or over 60 characters is a 400)


class ValidateStrategyIn(BaseModel):
    """POST /strategies/validate — M4 R6 Validate button. Checks a source WITHOUT writing it."""
    source_code: str = Field(..., min_length=1)  # full .py source to validate (no write)


class UpdateSettingIn(BaseModel):
    # Accepts any JSON scalar (string, number, bool) or object — the route serializes
    # it to TEXT for storage based on the stored value_type.
    # Empty string is a legitimate value (e.g. unset path override).
    value: Any = Field(...)


class UpdateOverrideIn(BaseModel):
    """PUT /strategies/{id}/settings/{scope}/{key} — a per-strategy override (M4.5 3b B2.2). value is the
    override (typed + bounds-checked); source_note is the optional D4 justification (NULL when omitted)."""
    value: Any = Field(...)
    source_note: str | None = Field(default=None)  # the D4 override justification, optional


class PutProvenanceIn(BaseModel):
    """PUT /provenance/{dotted_key} — author/edit a USER-AUTHORED setting's provenance (M4.5 3b B1.4).
    All four prose fields must be non-empty and bounds_spec must be a well-formed structured spec; the
    route checks both and returns a specific 400 (defaults here let the route own the messages, not 422)."""
    source: str = Field(default="")
    mechanism: str = Field(default="")
    sensitivity: str = Field(default="")
    bounds: str = Field(default="")
    bounds_spec: Any = Field(default=None)  # validated by researchdb.validate_bounds_spec in the route


# -------- pre-M4 Brief A1 / D4 reference-data loader bodies --------

class UniverseCreateIn(BaseModel):
    """POST /universes — Add Universe (D4). tickers is the raw textarea lines; the route
    trims blanks and #-comments server-side (defense in depth, not trusting the client)."""
    name: str = Field(..., min_length=1)
    tickers: list[str] = Field(..., min_length=1)   # at least one line; cleaned in the route
    source_label: str = Field(..., min_length=1)


class PriceUniverseCreateIn(BaseModel):
    """POST /price-universes — Add a price universe (M5 U1, R-UNIV). tickers is the raw textarea/chip
    list; the create helper normalizes (bare-upper, strip .US), de-dups, and validates each against the
    EODHD availability index (NOT the CIK bridge). Mirrors UniverseCreateIn minus source_label (the
    price-universe source is always the local eodhd store)."""
    name: str = Field(..., min_length=1)
    tickers: list[str] = Field(..., min_length=1)   # at least one typed ticker; validated in the helper
    # AD-UU-3 / B1.3: the test window, sent in the SAME create call (atomic). Epoch seconds UTC. Typed
    # Optional here (so a missing field maps to the helper's own named PriceUniverseWindowRequired 422
    # rather than a generic FastAPI validation error), but item 5b makes BOTH bounds required in
    # practice: the helper floors each to UTC midnight, validates ordering, and refuses a NULL side.
    window_start_ts: Optional[int] = None
    window_end_ts: Optional[int] = None
    display_name: Optional[str] = None               # UNI1: the typed name a page shows; NULL when none is given


class PriceUniverseMembersIn(BaseModel):
    tickers: list[str] = Field(..., min_length=1)


class PriceUniverseRenameIn(BaseModel):
    """PATCH /price-universes/{id} — rename a price universe's catalog row and/or set its display name (UNI1)."""
    name: Optional[str] = Field(None, min_length=1)
    display_name: Optional[str] = None


class PriceUniverseWindowIn(BaseModel):
    """PATCH /price-universes/{id}/window — set or clear the test window (AD-UU-3, B1.3). A distinct
    sub-route from the rename PATCH (which requires name); either bound may be null here (the admin
    correction path), never read as full history, only as unknown (R-T15 refuses a draw against it)."""
    window_start_ts: Optional[int] = None
    window_end_ts: Optional[int] = None


class PriceUniverseDispatchIn(BaseModel):
    """POST /price-universes/{id}/dispatch — kick off a universe sweep (M5 U2, AD-M5-13; B3 contract).

    strategy_id is the strategy run over the universe. param_grid is {param_name: [values]}: REQUIRED + non-empty
    for a strategy that declares sweep_params; EMPTY ({}) for a zero-parameter strategy (the backend reads the
    strategy's declared params and runs the single-combo path — AD-UU-4, B3.1). selection_scope is NOT in the
    request: the independent loop is always stamped 'in_isolation' server-side (F3 — the field was vestigial;
    the worker never read it and the metrics are identical either way). The backend freezes+hashes the grid and
    registers the sweep; the worker expands the grid (engine-neutral)."""
    strategy_id: int
    param_grid: dict[str, list]                       # {param_name: [values]}; non-empty for a sweep, {} for a zero-param run
    name: Optional[str] = None
    # Lane T (night two, data-use tracker, generator_architecture_2026-09-05.md AD6.4): which
    # pipeline stage this draw belongs to, and which calendar (the real one, or a named
    # dry-run calendar the tracker never counts against the real budget, R-T9). Both optional
    # so every existing caller (which names neither) keeps behaving exactly as it does today:
    # a plain screen-stage sweep under the real calendar. A value outside AD6.4's four-stage
    # vocabulary is a 422 from Pydantic's own Literal validation, before any row is touched.
    stage: Literal["screen", "optimise", "walkforward", "reserve"] = "screen"
    calendar_id: str = Field(default="real", min_length=1)


class CikTickerMapLoadIn(BaseModel):
    """POST /cik-ticker-map — Top up the company→ticker map (D4). source_file omitted
    means fetch the live SEC URL."""
    source_file: Optional[str] = None


class BulkReparseIn(BaseModel):
    """POST /event-batches/{id}/reparse — re-parse 13D events from operator-staged files (D4).
    apply=false is the preview; max_events bounds the scan."""
    reparse_dir: str = Field(..., min_length=1)
    apply: bool = False
    max_events: Optional[int] = Field(default=None, ge=1)


# =============================================================
# Browser endpoints
# =============================================================

# Lane WU (5.8c, `DECISIONS.md` #383 decision 2, set 12 plan section 6's own "what can break"
# line): the lead-in pre-dispatch gate's two live signals, module-level, one increment site each,
# the same shape PRICE_UNIVERSE_DELETE_MONITOR_COUNTERS above already uses.
# `lead_in_precheck_refusals` is the declared failure mode: a client that ignores the 409 and keeps
# retrying the same short dataset. `lead_in_precheck_dataset_unreadable` is NOT a failure mode this
# brief declares, but is wired anyway: this backend process cannot always reach the parquet a run
# will actually open (research_lab/CLAUDE.md, "On disk": "price parquets the worker writes which
# the backend only ever reads by path"), so when that read fails the check falls through to
# LETTING THE JOB DISPATCH (the pre-existing behaviour, unchanged) rather than refusing on a file
# it never actually inspected -- and this counter is what keeps that fallthrough from being silent.
LEAD_IN_PRECHECK_MONITOR_COUNTERS: dict[str, int] = {
    "lead_in_precheck_refusals": 0,
    "lead_in_precheck_dataset_unreadable": 0,
    # Lane DV (set 16b, `#424`, AC-5): the universe sweep route's OWN lead-in refusal count,
    # added BESIDE the single-run route's existing key above rather than renaming or sharing it,
    # so the two routes' own refusal rates can still be told apart.
    "lead_in_precheck_refusals_sweep": 0,
}


def _refuse_insufficient_sweep_lead_in(universe_id: int) -> None:
    """AC-5 (docs/units/DV/criteria.md): the universe sweep route's own pre-dispatch lead-in
    check. dispatch_stage_route already runs this exact check through stage_runner._check_warmups
    for the dry-run chain; the universe sweep route dispatches a run DIRECTLY, never through that
    chain, so it never ran the check at all until this lane. Reuses stage_runner's own
    daily_store_path(), SIZING_LEAD_IN_BARS, _bar_dates_for_ticker and LeadInUnavailable rather
    than a second copy of any of the four, so the two routes can never quietly disagree on what
    "enough lead-in" means.

    Where the backend host carries no configured daily store (stage_runner.
    DailyStoreNotConfigured), this check is SKIPPED rather than refusing on a guess (AC-5's own
    text: production's own case, FACT LOOKED UP in docs/units/DV/report_probes_1.md): the run
    proceeds, and AC-1's own give-back takes over if it later crashes for real.
    """
    try:
        store = stage_runner.daily_store_path()
    except stage_runner.DailyStoreNotConfigured:
        return  # cannot count bars on this host: fall through, never refuse on a guess (AC-5)
    with researchdb.get_conn() as conn:
        window_row = conn.execute(
            "SELECT window_start_ts FROM research_datasets WHERE id = ?;",
            (universe_id,),
        ).fetchone()
    if window_row is None or window_row["window_start_ts"] is None:
        # No real window yet (a missing row, or a NULL bound): request_draw's own DrawWindowMissing
        # is the correct, already-tested refusal for this state; this check stays silent so that
        # refusal is the one the caller actually sees, matching stage_runner._check_warmups's own
        # documented reasoning for the identical branch.
        return
    window_start_date = stage_runner._epoch_to_date_str(window_row["window_start_ts"])
    for ticker in researchdb.list_price_universe_members(universe_id):
        dates = stage_runner._bar_dates_for_ticker(store, ticker)
        bars_before_window = sum(1 for d in dates if d < window_start_date)  # strictly earlier than the window
        if bars_before_window < stage_runner.SIZING_LEAD_IN_BARS:
            LEAD_IN_PRECHECK_MONITOR_COUNTERS["lead_in_precheck_refusals_sweep"] += 1
            raise stage_runner.LeadInUnavailable(ticker, bars_before_window)  # names the ticker and the true count


def _dataset_bar_count_for_lead_in_precheck(dataset_id: int) -> "int | None":
    """The exact bar count of the parquet a run_backtest dispatch on ``dataset_id`` will open,
    resolved the SAME way ``researchdb.dispatch_run`` resolves a dataset's own file for its own
    no-parquet check: a fresh read of ``research_datasets.file_path`` (``researchdb.get_dataset``,
    the same column dispatch_run's own SELECT reads), never ``research_datasets.rows`` -- a column
    written once at fetch completion and never re-derived from the file again, so it can go stale
    relative to what the file on disk actually holds by the time a run dispatches against it.

    Returns ``None``, not a refusal, when the dataset row does not exist, carries no file yet
    (``dispatch_run``'s own ``DatasetNotFound``/``DatasetNotRunnable`` already cover both), or when
    the file cannot be opened on THIS host: the dashboard backend and the laptop worker are not
    always the same machine (root ``CLAUDE.md``'s Tools table: "Hetzner VPS: dashboard ... Docker
    Desktop + WSL2: worker sandbox"), so an unreachable container-relative path is a KNOWN,
    already-accepted architecture gap (lane-list row `2.20`'s own "no stage-runner dispatch runs on
    production without the daily store on the backend host"), not evidence the dataset is short.
    """
    ds = researchdb.get_dataset(dataset_id)
    if ds is None or ds.get("file_path") is None:
        return None  # nothing to count; dispatch_run's own checks refuse this state by name already
    # Lazy, bare import: the SAME precedent dashboard/backend/paperdb.py already sets for reaching
    # research_lab from this backend (its own "from research_lab.strategies.helpers import sizing",
    # zero sys.path code, because production's own PYTHONPATH already carries the repo root).
    # pathmap.py is import-light on purpose (pathlib only, its own module docstring), so this import
    # carries no vbt / pandas cost of its own.
    from research_lab.worker.pathmap import to_host_path
    # The SAME default the worker itself computes (research_lab/worker/jobs/fetch_dataset.py's own
    # DATA_ROOT), read from the SAME named environment key, so this check and the worker always
    # agree on which physical file a container-relative "/data/..." path names.
    repo_root = Path(__file__).resolve().parents[3]  # routes/research.py -> routes, backend, dashboard, repo root
    data_root = Path(os.environ.get("RESEARCH_DATA_ROOT", str(repo_root / "research_lab" / "data")))
    host_path = to_host_path(ds["file_path"], data_root)
    try:
        import pyarrow.parquet as pq  # lazy: only when a run_backtest dispatch actually needs this count
        return pq.ParquetFile(host_path).metadata.num_rows  # metadata only: never reads a single price cell
    except (FileNotFoundError, OSError):
        LEAD_IN_PRECHECK_MONITOR_COUNTERS["lead_in_precheck_dataset_unreadable"] += 1
        return None


async def _dispatch_run_backtest(body: CreateJobBody):
    """Dispatch a run_backtest job + pending run row (brief E). Thin over researchdb.dispatch_run."""
    if body.strategy_id is None or body.dataset_id is None:
        raise HTTPException(400, "run_backtest requires strategy_id and dataset_id")
    # Normalize: trim, collapse "" -> None so the DB stores NULL when the user typed nothing.
    name = body.name.strip() if body.name is not None else None
    if name == "":
        name = None
    try:
        # Round-3 finding CQ1 (code_quality_round2.md SF1): the sweep dispatch route already
        # carried this re-verification but this single-backtest path did not, so a generated
        # strategy whose source had drifted from its own recorded expression (PUT
        # /strategies/{id} lets an operator edit it) could still run a single backtest with
        # no refusal. Same call, same place in the flow (before the dispatch that would
        # otherwise run unchecked), matching the sweep route's own comment: a strategy with
        # no generation record at all is hand-authored and passes through unchanged.
        generated.assert_generation_record_matches(body.strategy_id)
        # Lane T (night two, data-use tracker, R-T3/AD7): the chronology gate's second named
        # call site (section 6.1). Lane SR (6.2a, AC-3/DH13) closed the gap this comment used to
        # describe: CreateJobBody now carries stage/calendar_id explicitly, defaulting to the
        # same screen-stage real draw every existing caller already got, so no caller's own
        # behaviour changes just because the fields now exist. A strategy that no longer exists
        # skips the gate and falls through to dispatch_run's own StrategyNotFound, leaving that
        # 404 unchanged.
        strat_row = researchdb.get_strategy(body.strategy_id)
        # Lane DV (set 16b, `#424`, AC-4): the single-run route's own quantile pre-check, before
        # any draw is granted, reading the strategy's baked DEFAULT_ENTRY_QUANTILE/
        # DEFAULT_EXIT_QUANTILE constants from its stored source text (no param_grid exists on
        # this route at all). A strategy that no longer exists is left to dispatch_run's own
        # StrategyNotFound below, unchanged.
        if strat_row is not None:
            _refuse_bad_baked_quantiles_in_source(strat_row["source_code"])
        # Lane WU (5.8c, criterion 3): refuse BEFORE any draw is granted, the same order rule
        # stage_runner._check_warmups follows for the stage-runner paths (row 2.20's own text: a
        # lead-in/quantile refusal must never leave behind a granted draw for a dispatch that never
        # happened). stage_runner.SIZING_LEAD_IN_BARS by import, never a second literal 278 -- that
        # constant exists so this file need not import sizing.py itself.
        bar_count = _dataset_bar_count_for_lead_in_precheck(body.dataset_id)
        if bar_count is not None and bar_count <= stage_runner.SIZING_LEAD_IN_BARS:
            LEAD_IN_PRECHECK_MONITOR_COUNTERS["lead_in_precheck_refusals"] += 1
            raise HTTPException(
                409,
                detail=(
                    f"insufficient-lead-in: dataset {body.dataset_id} holds {bar_count} bars, at or "
                    f"below the {stage_runner.SIZING_LEAD_IN_BARS}-bar lead-in every sized run needs "
                    f"(bars-have={bar_count}, bars-need={stage_runner.SIZING_LEAD_IN_BARS}); "
                    "refusing before dispatch"
                ),
            )
        draw_id = None
        if strat_row is not None:
            draw_id = tracker.request_draw(
                body.strategy_id, strat_row["version"], body.calendar_id, body.stage,
                body.dataset_id, int(time.time()),
            )
        # M4 R8/D5: a scratch test run is tagged kind='scratch'; a standard run leaves kind NULL.
        d = researchdb.dispatch_run(
            body.strategy_id, body.dataset_id, name=name,
            kind="scratch" if body.scratch else None,
        )
        if draw_id is not None:
            # Transaction 3 (AD7.3): the run this draw was granted for now exists. A
            # tracker.RunLinkFailed here means the run itself DID dispatch; let it propagate
            # rather than swallowing it, so the caller still learns the link failed.
            tracker.link_run(draw_id, d["run_id"], int(time.time()))
    except generated.GeneratedSourceDrift as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except QuantileOutOfRange as exc:  # AC-4: a baked quantile constant outside (0, 1)
        raise HTTPException(409, detail=str(exc)) from exc
    except _TRACKER_REFUSALS as exc:
        _handle_tracker_refusal(exc)
    except researchdb.StrategyNotFound:
        raise HTTPException(404, "strategy not found")
    except researchdb.StrategyDeleted as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except researchdb.DatasetNotFound:
        raise HTTPException(404, "dataset not found")
    except researchdb.DatasetNotRunnable as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except researchdb.UnknownAssetClass as exc:
        # M4 D1/D7: the dataset's (source, dataset_type) names data with no cost contract. A
        # contract-bound refusal (the request asks for data the system cannot cost), so 422 — the
        # same family as StrategyNotEventDriven. The exception message carries the offending pair.
        raise HTTPException(422, str(exc))
    except researchdb.AssetClassScopeMismatch as exc:
        # M4 R2c/D7: the strategy's declared asset_class_scope excludes this dataset's class. A
        # contract-bound violation (the declared contract forbids the request), 422 — not a 409, since
        # the dataset's class is an immutable fact, not a transient state. The message carries context.
        raise HTTPException(422, str(exc))

    await broadcaster.publish("research.run_created", {
        "run_id": d["run_id"], "job_id": d["job_id"], "strategy_id": d["strategy_id"],
        "strategy_version": d["strategy_version"], "dataset_id": d["dataset_id"],
        "name": d["name"], "created_at": d["created_at"],  # Unit-4 D3: resolved name, not the pre-dispatch None
        # M4 R8/D5: normalize None -> 'standard' so the SSE payload matches the COALESCE the REST list
        # returns (the RunKind contract is 'standard' | 'scratch', never null).
        "kind": d["kind"] or "standard",
    })
    return {"id": d["job_id"], "run_id": d["run_id"], "status": "pending"}


@browser_router.post("/jobs", status_code=201)
async def create_job(body: CreateJobBody):
    """Create a new pending job. Emits research.job_created.

    run_backtest is dispatched specially (brief E): the body carries strategy_id +
    dataset_id, and the backend inserts the research_jobs + research_runs rows atomically
    via researchdb.dispatch_run, then fires research.run_created. Other kinds keep the
    generic payload path.
    """
    if body.kind == "run_backtest":
        return await _dispatch_run_backtest(body)

    now = int(time.time())
    with researchdb.transaction() as conn:
        # Insert the job and grab its assigned id
        row = conn.execute(
            "INSERT INTO research_jobs(kind, payload_json, created_at, status) "
            "VALUES (?, ?, ?, 'pending') RETURNING id;",
            (body.kind, json.dumps(body.payload), now),
        ).fetchone()
        job_id = row["id"]
        # Write the audit event for the lifecycle log
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, 'created', ?);",
            (job_id, now, json.dumps({"kind": body.kind})),
        )
    await broadcaster.publish(
        "research.job_created",
        {"id": job_id, "kind": body.kind, "status": "pending"},
    )
    return {"id": job_id, "status": "pending"}


@browser_router.post("/event-runs", status_code=201)
async def create_event_run(body: CreateEventJobBody):
    """Dispatch an event-driven backtest + pending run row (M3.5 brief I, OPEN-6).

    The sibling of the run_backtest dispatch (POST /jobs kind=run_backtest), on its own path so
    the M3 price-run dispatch is byte-unchanged (R19). event_types is read off the strategy at
    dispatch (OPEN-4); the universe and window are run-level. The job kind stays 'run_backtest'
    (same worker handler; run() branches on the event payload fields, §8). Emits research.run_created
    so the runs list refreshes. The dashboard UI that drives this is the deferred D8 backlog item;
    brief I ships the backend + worker surface it will call.
    """
    # Normalize the optional label: trim, collapse "" -> None (same as _dispatch_run_backtest).
    name = body.name.strip() if body.name is not None else None
    if name == "":
        name = None
    try:
        d = researchdb.dispatch_event_run(
            body.strategy_id, body.universe_id, body.knowledge_from, body.knowledge_to, name=name,
            kind="scratch" if body.scratch else None,  # M4 R8/D5: tag the editor test run scratch
        )
    except researchdb.StrategyNotFound:
        raise HTTPException(404, "strategy not found")
    except researchdb.StrategyDeleted as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except researchdb.UniverseNotFound:
        raise HTTPException(404, "universe not found")
    except researchdb.StrategyNotEventDriven as exc:
        raise HTTPException(422, str(exc))
    except researchdb.UnknownAssetClass as exc:
        # M4 D1/D7: same 422 contract as the price handler. An event run resolves its class via the
        # universe invariant (always equities), so this arm does not fire in normal operation; it maps the
        # full exception surface of dispatch_event_run's asset_class_of call.
        raise HTTPException(422, str(exc))
    except researchdb.AssetClassScopeMismatch as exc:
        # M4 R2c/D7: the strategy's declared asset_class_scope excludes equities (it never does for a
        # correctly-declared event strategy — a universe is always equities — so this is defense in depth).
        # A contract-bound violation, 422, mirroring the StrategyNotEventDriven arm above.
        raise HTTPException(422, str(exc))
    except researchdb.StrategyValidationError as exc:
        # The stored source has a malformed event_types declaration. validate_strategy_source
        # precludes this for sources written after brief I, but a pre-contract stored source can
        # still trip the dispatch-time AST read; surface it as a 422, not an unhandled 500.
        raise HTTPException(422, f"strategy source has a malformed event_types declaration: {exc}")

    await broadcaster.publish("research.run_created", {
        "run_id": d["run_id"], "job_id": d["job_id"], "strategy_id": d["strategy_id"],
        "strategy_version": d["strategy_version"], "universe_id": d["universe_id"],
        "knowledge_from": d["knowledge_from"], "knowledge_to": d["knowledge_to"],
        "event_types": d["event_types"], "name": d["name"], "created_at": d["created_at"],  # Unit-4 D3: resolved name
        "kind": d["kind"] or "standard",  # M4 R8/D5: normalize None -> 'standard' (the RunKind contract)
    })
    return {"id": d["job_id"], "run_id": d["run_id"], "status": "pending"}


@browser_router.get("/jobs")
async def list_jobs(limit: int = 50, offset: int = 0):
    """List jobs, newest first. Pagination via limit/offset."""
    limit = max(1, min(limit, 500))
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, kind, status, created_at, claimed_at, finished_at, "
            "       worker_id, error "
            "FROM research_jobs "
            "ORDER BY id DESC LIMIT ? OFFSET ?;",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


@browser_router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    """Return one job's full record."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM research_jobs WHERE id = ?;",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    d = dict(row)
    # Parse the JSON columns for the client
    d["payload"] = json.loads(d.pop("payload_json"))
    if d.get("result_json"):
        d["result"] = json.loads(d.pop("result_json"))
    else:
        d.pop("result_json", None)
        d["result"] = None
    return d


@browser_router.get("/jobs/{job_id}/events")
async def get_job_events(job_id: int):
    """Audit trail for one job, chronological."""
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, event, payload_json FROM research_job_events "
            "WHERE job_id = ? ORDER BY ts ASC, id ASC;",
            (job_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("payload_json"):
            d["payload"] = json.loads(d.pop("payload_json"))
        else:
            d.pop("payload_json", None)
            d["payload"] = None
        out.append(d)
    return out


@browser_router.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: int, since_id: int = 0, limit: int = 500):
    """Log lines for a job. Use since_id to fetch only new ones."""
    limit = max(1, min(limit, 5000))
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, level, line FROM research_job_logs "
            "WHERE job_id = ? AND id > ? ORDER BY id ASC LIMIT ?;",
            (job_id, since_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@browser_router.delete("/jobs/{job_id}")
async def cancel_job(job_id: int):
    """Cancel a job (brief I, arch §20). Branches on status via researchdb.request_cancel (EI-pre).

    - pending run_backtest (unclaimed): backend finalizes job + run to cancelled now, returns 200.
    - running run_backtest: sets cancellation_requested=1, returns 202 (worker hard-kills).
    - already cancelled: idempotent 200; other terminal / running non-backtest: 409.
    - missing: 404.
    """
    try:
        d = researchdb.request_cancel(job_id)
    except researchdb.JobNotFound:
        raise HTTPException(status_code=404, detail="job not found")

    if d["action"] == "cancelled":
        if d["run_id"] is not None:  # run_backtest: the run row was finalized too (§5.8)
            await broadcaster.publish("research.run_cancelled", {
                "run_id": d["run_id"], "job_id": job_id, "status": "cancelled",
            })
        return {"cancelled": True, "id": job_id, "run_id": d["run_id"]}

    if d["action"] == "requested":
        # Worker will hard-kill and call /worker/runs/{id}/cancelled; that callback fires
        # research.run_cancelled. 202 = accepted, cancellation in progress.
        return JSONResponse(
            {"status": "cancelling", "id": job_id, "run_id": d["run_id"]}, status_code=202)

    if d["action"] == "already" and d["status"] == "cancelled":
        return {"cancelled": True, "id": job_id}  # idempotent
    raise HTTPException(status_code=409, detail=f"job is in '{d['status']}' state; cannot cancel")


# =============================================================================
# RS (U.6, set 5 day plan, docs/plans/night/2026-09-12_set5b_day/PLAN.md section 10): the verdict
# word a run's own list/detail row gains, plus THE CHAIN read below. research.db and the judge
# store (judgedb.DB_PATH) are the SAME sqlite file, so a plain SELECT joined against
# research_judge_verdicts works with no cross-database query and no new store.
# =============================================================================

# "Is this verdict row the CURRENT one for its candidate" -- the SAME rule
# routes/judge.py's own _IS_CURRENT_EXPR computes (judge_architecture_2026-08-31.md section 2b
# items 3/4: the newest non-advisory row sharing a candidate key). Duplicated here rather than
# imported from routes/judge.py ON PURPOSE: that file is a DIFFERENT lane's own area for this
# night set (routes/judge.py is job JP's, not RS's, per the plan's lane table), and importing one
# lane's private, underscore-prefixed name into another file would couple the two lanes' modules
# at import time while both are being built concurrently. The text is schema-derived (the
# candidate-key definition itself, not a design choice this route makes), so the two copies can
# only drift if the schema changes -- a review event on both files either way.
_RUNS_VERDICT_IS_CURRENT_EXPR = """(
    v.is_advisory = 0 AND NOT EXISTS (
        SELECT 1 FROM research_judge_verdicts AS newer
         WHERE newer.strategy_id      = v.strategy_id
           AND newer.strategy_version = v.strategy_version
           AND newer.variation_id     = v.variation_id
           AND newer.ticker           = v.ticker
           AND newer.dataset_sha256   = v.dataset_sha256
           AND newer.is_advisory      = 0
           AND (newer.graded_at > v.graded_at
                OR (newer.graded_at = v.graded_at AND newer.id > v.id))
    )
)"""


def _judge_verdicts_table_present(conn) -> bool:
    """True when research_judge_verdicts exists in the file this connection opened. Mirrors
    routes/judge.py's own _table_present check (the same underlying fact: research.db and the
    judge store are literally the same file, judgedb.DB_PATH == RESEARCH_DB_PATH), duplicated
    here for the same lane-disjointness reason _RUNS_VERDICT_IS_CURRENT_EXPR is. A fresh store,
    or a probe fixture that never ran judgedb.init_judge_db(), reads False here rather than
    raising: the runs list and run explorer must keep working even when the judge half of the
    same file has never been initialised, because a backtest can succeed with no judge ever run.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'research_judge_verdicts'"
    ).fetchone() is not None


def _verdicts_for_run_ids(conn, run_ids: "list[int]") -> "dict[int, str | None]":
    """One additional query (never a per-run_id fetch, plan section 10 criterion 3) mapping a set
    of run ids to their CURRENT judge verdict word, or None when the run has no graded cell, or
    when research_judge_verdicts has never been created.

    Joins research_run_cells (variation_id=0, row_scope='cell' -- the single run's own cell, the
    EXACT grain routes/judge.py's judge_run route reads to grade a run: "SELECT variation_id,
    ticker FROM research_run_cells WHERE run_id = ? AND variation_id = 0 AND row_scope = 'cell'")
    against research_judge_verdicts on the five-column candidate key, filtered to the current row.
    So "verdict" here is always the SAME verdict that route would report for that run, never a
    re-derived approximation. A sweep run (whose cells are keyed by a real variation_id, not 0)
    naturally finds no match and reads None, which is correct: JUDGE THIS RUN grades a single
    run's own saved replay, never a sweep as a whole.
    """
    if not run_ids:  # an empty page's worth of runs asks no query at all
        return {}
    result: "dict[int, str | None]" = {run_id: None for run_id in run_ids}  # every id defaults to "not graded"
    with_verdicts = _judge_verdicts_table_present(conn)  # a fresh store: every id stays None, no query needed
    if not with_verdicts:
        return result
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"""
        SELECT c.run_id AS run_id, v.verdict AS verdict
          FROM research_run_cells AS c
          JOIN research_runs AS r ON r.id = c.run_id
          LEFT JOIN research_judge_verdicts AS v
            ON v.strategy_id      = r.strategy_id
           AND v.strategy_version = r.strategy_version
           AND v.variation_id     = c.variation_id
           AND v.ticker           = c.ticker
           AND v.dataset_sha256   = r.dataset_sha256
           AND {_RUNS_VERDICT_IS_CURRENT_EXPR}
         WHERE c.run_id IN ({placeholders})
           AND c.variation_id = 0
           AND c.row_scope = 'cell';
        """,
        run_ids,
    ).fetchall()
    for row in rows:
        result[row["run_id"]] = row["verdict"]  # NULL over an ungraded cell stays a Python None, the honest "not graded yet"
    return result


def _chain_stage_order_violations(conn) -> "list[int]":
    """The registry's own chain_stage_order_violations row (plan section 10, "what can break"):
    a granted draw_requested row whose window_start_ts is BEFORE an earlier-granted draw's
    window_end_ts for the SAME (strategy_id, strategy_version, calendar_id) candidate. Expected
    zero: tracker.request_draw's own chronology gate (its high-water-mark check) refuses exactly
    this shape of draw before it is ever granted, so a nonzero reading here means either a bug in
    that gate or a write to research_data_draws that bypassed it entirely -- never an ordinary
    operational event. Lives here (not in tracker.py, the module that owns the table) because
    tracker.py is a DIFFERENT lane's area for this night set; see _RUNS_VERDICT_IS_CURRENT_EXPR's
    own comment for why a monitor query is duplicated across lane boundaries rather than shared.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT d.id
          FROM research_data_draws AS d
          JOIN research_data_draws AS prior
            ON prior.event_kind      = 'draw_requested'
           AND prior.decision        = 'granted'
           AND prior.strategy_id     = d.strategy_id
           AND prior.strategy_version = d.strategy_version
           AND prior.calendar_id     = d.calendar_id
           AND prior.id              < d.id
         WHERE d.event_kind = 'draw_requested'
           AND d.decision   = 'granted'
           AND d.window_start_ts < prior.window_end_ts;
        """
    ).fetchall()
    return [r["id"] for r in rows]


@browser_router.get("/runs")
async def list_runs(limit: int = 50, offset: int = 0, sort: str = "created_at",
                    kind: str | None = None, include_synthetic: bool = False):
    """Paginated run list with stats joined (brief F). sort: created_at|name|sharpe|return|drawdown|trades.

    GV2-4a: runs on made-up (synthetic) datasets are left out unless ?include_synthetic=true.

    created_at and name list ALL runs (created_at newest-first; name A->Z, NULLs-last, U3); a metric
    sort lists only completed runs that carry that metric, index-backed per §3.5. M4 D5: the optional
    `kind` filter (scratch | standard) narrows to editor test runs or ordinary/legacy runs; absent =
    every run.
    """
    # U3 (pre-M4 Brief A1): 'name' is a NON-metric sort key — it must NOT join researchdb._RUN_SORT
    # (every entry there is a research_run_stats column routed through the dropping INNER-JOIN path);
    # runs_list_sql handles 'name' over research_runs with a LEFT JOIN so every run is kept.
    if sort not in ("created_at", "name") and sort not in researchdb._RUN_SORT:
        raise HTTPException(
            400, f"unknown sort {sort!r}; use created_at|name|sharpe|return|drawdown|trades")
    # M4 D5: validate the kind filter to the closed set before it reaches the SQL builder (fail-loud 400,
    # the same contract as the sort key) — never an unbounded string near the query.
    if kind is not None and kind not in researchdb._RUN_KIND_FILTERS:
        raise HTTPException(400, f"unknown kind filter {kind!r}; use scratch|standard")
    rows = researchdb.list_runs(limit=limit, offset=offset, sort=sort, kind_filter=kind,
                                include_synthetic=include_synthetic)
    # RS (U.6): the list gains a verdict word per run through ONE additional query over this
    # page's own run ids (plan section 10 criterion 3), never a per-row fetch.
    run_ids = [r["id"] for r in rows]
    with researchdb.get_conn() as conn:
        verdicts = _verdicts_for_run_ids(conn, run_ids)
    for r in rows:
        r["verdict"] = verdicts.get(r["id"])
    return rows


@browser_router.get("/runs/summary")
async def runs_summary(kind: str | None = None, include_synthetic: bool = False):
    """Global aggregate over the run log for the Runs-list summary grid (TERM Runs rebuild).

    The list (GET /runs) is paginated, so its header totals must come from an aggregate, not a page
    (DECISIONS #143). The optional `kind` filter (scratch | standard) scopes the summary to the same
    slice the list shows; absent = every run. Validated to the closed set first (the same fail-loud
    contract as GET /runs). Declared ABOVE /runs/{run_id} so the literal path is matched first.
    """
    if kind is not None and kind not in researchdb._RUN_KIND_FILTERS:
        raise HTTPException(400, f"unknown kind filter {kind!r}; use scratch|standard")
    return researchdb.runs_summary(kind_filter=kind, include_synthetic=include_synthetic)


@browser_router.get("/runs/{run_id}")
async def get_run(run_id: int):
    """One run's full row plus its stats (brief F). 404 if unknown.

    M5 U2: also reports `is_sweep` (the run's dataset is a price_universe) so RunExplorer routes a universe
    sweep to the cells/grid read instead of the single-run bundle (which has no artifacts to open). For an
    in-flight sweep it also attaches `sweep_progress` (cell done/total + per-member status) so the loop's IN
    FLIGHT panel rides the SAME 2.5s run poll RunExplorer already runs — no second endpoint, one round-trip.
    """
    run = researchdb.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    try:
        run["is_sweep"] = await asyncio.to_thread(researchdb.run_is_sweep, run_id)  # the universe-sweep discriminator
    except researchdb.RunNotFound:  # get_run found the row but the dataset FK is broken (corrupt DB) -> 404, not 500
        raise HTTPException(404, "run not found")
    # Lane WV (AC-1, `#417`/`#420`): the SAME test the bundle route already uses to admit a
    # walk-forward run (export.run_is_walkforward: a granted, linked draw at stage='walkforward'),
    # read here too, so the run page and the bundle route can never disagree about what kind of
    # run this is. Never raises for a known run row (see the function's own docstring), so no
    # extra try/except is needed beyond the one already wrapping is_sweep above.
    run["is_walkforward"] = await asyncio.to_thread(export.run_is_walkforward, run_id)
    # The IN-FLIGHT loop surface: only a not-yet-finished sweep needs progress (a succeeded sweep reads /cells;
    # a single run never has it). Attaching it here keeps it on the existing poll instead of a parallel endpoint.
    if run["is_sweep"] and run["status"] in ("pending", "running"):
        run["sweep_progress"] = await asyncio.to_thread(researchdb.get_sweep_progress, run_id)

    # RS (U.6): the single-run route gains the same verdict word list_runs gains, off the event
    # loop like every other blocking sqlite read in this function.
    def _read_verdict() -> "str | None":
        with researchdb.get_conn() as conn:
            return _verdicts_for_run_ids(conn, [run_id]).get(run_id)
    run["verdict"] = await asyncio.to_thread(_read_verdict)
    return run


@browser_router.get("/runs/{run_id}/chain")
async def get_run_chain(run_id: int):
    """GET /api/research/runs/{run_id}/chain (RS, U.6, plan section 10 criterion 1): THE CHAIN --
    every stage of the dry-run pipeline this run's own candidate has drawn bars for, in time
    order, so the run explorer can draw one box per stage across its top.

    Resolves the run's own draw via tracker.draw_id_for_run (R-T14: the ONLY path from a run back
    to its draw), then reads that draw's (strategy_id, strategy_version, calendar_id) and every
    OTHER granted draw_requested row sharing that same key, ordered by window_start_ts. Grant order
    already equals window_start_ts order for a healthy chain -- tracker.request_draw's own
    high-water-mark gate refuses any draw whose window does not start no earlier than the
    candidate's prior granted mark -- so this read never needs to re-derive the ordering itself.

    A run with no draw at all (dispatched before the tracker existed, or through a path this
    milestone's stage_runner never touched) answers 200 with {"chain": null, "reason": ...}, NEVER
    a 404: the run itself is real, only its chain is absent (the plan's own words), and a 404 here
    would read as "no such run" to a caller that already loaded the run successfully.
    """
    draw_id = await asyncio.to_thread(tracker.draw_id_for_run, run_id)
    if draw_id is None:
        return {"chain": None, "reason": "no draw recorded for this run"}
    return await asyncio.to_thread(_build_run_chain, draw_id)


def _build_run_chain(draw_id: int) -> dict:
    """The synchronous body of get_run_chain, run off the event loop (asyncio.to_thread) like
    every other blocking sqlite read in this file."""
    with researchdb.get_conn() as conn:
        anchor = conn.execute(
            "SELECT strategy_id, strategy_version, calendar_id FROM research_data_draws "
            " WHERE id = ? AND event_kind = 'draw_requested';",
            (draw_id,),
        ).fetchone()
        if anchor is None:
            # tracker.draw_id_for_run only ever returns an id it read off a real run_linked row
            # (R-T14), and research_data_draws is append-only by its own DDIA comment ("no UPDATE,
            # no OR REPLACE, ever, with no exception at all"), so this branch is unreachable
            # through this build's own write paths. Refused by name rather than swallowed, per the
            # failure-stop discipline: a caller seeing this has found a real write-path bug.
            raise HTTPException(
                status_code=500,
                detail=f"draw {draw_id}, named by a run_linked row, no longer exists as a draw_requested row",
            )

        # Every OTHER granted draw for the same candidate + calendar, in window order (AC-1's own
        # chain definition, plan section 10 criterion 1).
        stage_rows = conn.execute(
            "SELECT id, stage, window_start_ts, window_end_ts FROM research_data_draws "
            " WHERE event_kind = 'draw_requested' AND decision = 'granted' "
            "   AND strategy_id = ? AND strategy_version = ? AND calendar_id = ? "
            " ORDER BY window_start_ts ASC;",
            (anchor["strategy_id"], anchor["strategy_version"], anchor["calendar_id"]),
        ).fetchall()

        chain: "list[dict]" = []
        linked_run_ids: "list[int]" = []
        # bars_observed + the linked run id, per stage draw: bounded, at most len(STAGE_WORDS)
        # rows (four today), so this is NOT the "never a per-row fetch" list-route rule -- that
        # rule is about the up-to-500-row runs list, not a chain this short.
        for row in stage_rows:
            observed = conn.execute(
                "SELECT bars_observed FROM research_data_draws "
                " WHERE event_kind = 'bars_observed' AND draw_id = ? "
                " ORDER BY id DESC LIMIT 1;",
                (row["id"],),
            ).fetchone()
            # The latest run_linked row for this draw, regardless of its own decision: AD7.3's own
            # reading is that even a 'refused' (registered_window_mismatch) link still names a run
            # whose bars were genuinely read, so the chain shows that run rather than hiding it.
            linked = conn.execute(
                "SELECT run_id FROM research_data_draws "
                " WHERE event_kind = 'run_linked' AND draw_id = ? "
                " ORDER BY id DESC LIMIT 1;",
                (row["id"],),
            ).fetchone()
            linked_run_id = linked["run_id"] if linked is not None else None
            if linked_run_id is not None:
                linked_run_ids.append(linked_run_id)
            chain.append({
                "draw_id": row["id"],
                "stage": row["stage"],
                "window_start_ts": row["window_start_ts"],
                "window_end_ts": row["window_end_ts"],
                "bars_observed": observed["bars_observed"] if observed is not None else None,
                "run_id": linked_run_id,
                "run_status": None,   # filled below, one more query over the whole linked-run set
                "verdict": None,
                "given_back": False,  # filled below; always a plain boolean, never null/absent (AC-3)
            })

        # Lane DV (set 16b, `#424`, AC-3): mark every stage box whose own granted draw matches
        # AC-1's exact give-back shape, reusing tracker.query_draws_given_back over this SAME
        # connection (never a second copy of the predicate in this file), so the chain route and
        # the tracker's own monitor query can never quietly disagree on what "given back" means.
        # Round-2 finding SF1 (code-quality gate 1): SCOPED to this chain's own bounded draw id
        # set (`stage_rows`, at most len(STAGE_WORDS) rows), never the whole table -- a run-page
        # view must not scan every granted draw in the store to answer one run's own question.
        # The unscoped form (draw_ids=None) stays the monitor's own, unchanged: it needs the
        # store-wide given-back population, not one run's slice of it.
        chain_draw_ids = [entry["draw_id"] for entry in chain]
        given_back_draw_ids = set(tracker.query_draws_given_back(conn, draw_ids=chain_draw_ids))
        for entry in chain:
            entry["given_back"] = entry["draw_id"] in given_back_draw_ids

        # The linked runs' status + verdict: one more query over the (bounded) run id set, never
        # one query per stage box.
        if linked_run_ids:
            placeholders = ",".join("?" for _ in linked_run_ids)
            statuses = {
                r["id"]: r["status"]
                for r in conn.execute(
                    f"SELECT id, status FROM research_runs WHERE id IN ({placeholders});",
                    linked_run_ids,
                ).fetchall()
            }
            verdicts = _verdicts_for_run_ids(conn, linked_run_ids)
            for entry in chain:
                rid = entry["run_id"]
                if rid is not None:
                    # A run_id named by a run_linked row but absent from research_runs is a
                    # deleted sibling run (soft references, no cascade, per research_data_draws'
                    # own DDIA comment): run_status stays None, which the frontend reads as
                    # "run deleted" rather than crashing on a missing key.
                    entry["run_status"] = statuses.get(rid)
                    entry["verdict"] = verdicts.get(rid)

    return {"chain": chain}


async def _bundle_or_refuse(
    run_id: int,
    *,
    admit_by_stage: bool,
    include_source: bool = True,
    include_settings: bool = True,
    include_indicators: bool = True,
    include_trades: bool = True,
    include_logs: bool = True,
):
    """Lane WB round 2 (CQ-1 / CQ-2, code_quality_pass1.md SF1 + SF2), folded again round 3
    (SF1 / NF1, code_quality_pass2.md): the one shared read path for all three build_run_bundle
    callers (get_run_bundle, export_run, and the public share route).

    Placed ABOVE get_run_cells on purpose (round 2, second fold): the s1 regression guard reads
    get_run_cells as a symbol slice from its own decorator to the NEXT top-level route decorator,
    so a plain (undecorated) helper sitting in that gap silently widens what the guard captures
    even though get_run_cells's own text never changed. Defined here, before any of its three
    callers, which is fine in Python since none of them run at import time.

    What this function does NOT do, on purpose: it never converts a refusal into an HTTPException or
    JSONResponse. The three callers disagree on response shape (get_run_bundle raises a plain
    HTTPException; export_run returns the locked spec section 5.5 JSONResponse body with a "status"
    field on BundleNotAvailable; the share route returns JSONResponse with the section 5.7 hardening
    headers and 410 instead of 409 on BundleNotAvailable), so this helper stays a pure read-plus-count
    and each caller keeps its own except block mapping the same four exception types to its own body.
    The ONE thing this helper centralises is WHEN a monitor counter fires, so the three call sites can
    never drift on that the way the code-quality gate found them starting to.

    Round 3, NF1: is_walkforward is now determined the SAME cheap way (run_is_sweep first, then
    run_is_walkforward only for a run that IS sweep-kind) for every caller, not only when
    admit_by_stage=True. Before this fold the share route (admit_by_stage=False) hard-coded
    is_walkforward=False, so its not-succeeded and malformed-indicators counters could never fire for
    a walk-forward run's own failure on a public share, an undercount on a declared monitor row.
    admit_by_stage now controls ONLY whether a non-walk-forward sweep is refused with a 409 (True) or
    let through unchanged to whatever build_run_bundle already did with it (False, the share route's
    own pre-existing, unmodified behaviour).

    admit_by_stage=True (get_run_bundle, export_run): a universe sweep that is not also a walk-forward
    run is refused here with HTTPException(409, ...) before build_run_bundle ever runs, matching
    get_run_bundle's own behaviour today; export_run never had this admission gate before round 2 and
    gained it then, which also fixed a latent, untested crash on an ordinary sweep's /export (no probe
    covers that edge; noted honestly in the build report, not hidden here). An unknown run surfaces as
    HTTPException(404, ...) from run_is_sweep's own RunNotFound, unchanged.

    admit_by_stage=False (the share route): no 409 admission refusal, same as before this lane ever
    touched this route (F1, docs/units/WB/reads/read_1.md on main, said to keep this route's existing
    behaviour otherwise, so a walk-forward-vs-sweep distinction never risks leaking a 409 sentence
    naming "another run's nature" on a public URL). An unknown run is NOT surfaced as a 404 here
    either (this route never called run_is_sweep for an unknown run before this lane existed): a
    RunNotFound from run_is_sweep is caught and treated as not-sweep, and build_run_bundle's own
    BundleError below is what the caller's existing except block already turns into "share not
    found", exactly as before.

    SF1 (round 3): the is_walkforward this helper computes is now PASSED to build_run_bundle through
    its own is_walkforward keyword, so that function no longer pays a second, unconditional
    research_data_draws self-join on every request now that the caller already has the answer.

    CQ-1 / SF1 (round 2): run_is_sweep is checked FIRST, and the research_data_draws self-join inside
    export.run_is_walkforward runs ONLY for a run that run_is_sweep already says is sweep-kind, so an
    ordinary price or event run (the overwhelming majority of /bundle and /export traffic) never pays
    that unindexed join. The index itself, leading with run_id on research_data_draws, is owed to
    researchdb.py and is not this file's to add (AC-7's own stop; logged here, not silently skipped).

    Returns (bundle, is_walkforward) on success. Raises HTTPException(404) or HTTPException(409)
    directly for an admission refusal when admit_by_stage=True; never raises either for
    admit_by_stage=False. Raises export.BundleError, export.BundleNotAvailable,
    export.BundleArtifactMissing or export.IndicatorsArtifactError UNCHANGED (after incrementing the
    matching AC-4 monitor counter) so each caller's own except block still decides the response
    shape, exactly as it does today.
    """
    is_walkforward = False
    # Round 3, NF1: run_is_sweep now runs for EVERY caller, admit_by_stage or not, because the cheap
    # sweep check is what lets is_walkforward be determined honestly for the share route too.
    try:
        is_sweep = await asyncio.to_thread(researchdb.run_is_sweep, run_id)
    except researchdb.RunNotFound:
        if admit_by_stage:
            raise HTTPException(404, "run not found")
        # admit_by_stage=False (the share route): preserve its pre-lane behaviour exactly. Treat the
        # run as not-sweep here; build_run_bundle raises BundleError for the unknown run below, which
        # the caller's own except block already turns into "share not found".
        is_sweep = False
    if is_sweep:
        # Only a sweep-kind run ever pays the draws self-join; an ordinary run never reaches here.
        is_walkforward = await asyncio.to_thread(export.run_is_walkforward, run_id)
        if not is_walkforward and admit_by_stage:
            raise HTTPException(409, "this run is a universe sweep; read its per-ticker results at /runs/{id}/cells")
        # admit_by_stage=False and not is_walkforward: an ordinary universe sweep shared publicly is
        # let through unchanged, to whatever build_run_bundle already did with it before this lane
        # added any admission concept to this route.
    # else: an ordinary (non-sweep) run is never walk-forward; is_walkforward stays False, no join paid.
    try:
        bundle = export.build_run_bundle(
            run_id,
            include_source=include_source,
            include_settings=include_settings,
            include_indicators=include_indicators,
            include_trades=include_trades,
            include_logs=include_logs,
            is_walkforward=is_walkforward,
        )
    except export.BundleNotAvailable:
        if is_walkforward:  # registry row walkforward_bundle_reads_failed_by_reason
            export.WALKFORWARD_BUNDLE_MONITOR_COUNTERS["not-succeeded"] += 1
        raise
    except export.BundleArtifactMissing:
        # AC-4: unconditional, because export.py only ever raises this for a walk-forward run's own
        # equity relay (build_run_bundle computes its own is_walkforward before deciding to raise it).
        export.WALKFORWARD_BUNDLE_MONITOR_COUNTERS["artifact-missing"] += 1
        raise
    except export.IndicatorsArtifactError:
        if is_walkforward:  # registry row walkforward_bundle_reads_failed_by_reason
            export.WALKFORWARD_BUNDLE_MONITOR_COUNTERS["malformed-indicators"] += 1
        raise
    return bundle, is_walkforward


@browser_router.get("/runs/{run_id}/cells")
async def get_run_cells(run_id: int):
    """The universe-sweep results read (M5 U2, R-GRID + R-SUMMARY, AD-M5-15): the per-ticker cell grid plus
    the distribution-led aggregate. 404 if the run is unknown; 409 if the run is NOT a universe sweep (a
    single run has no per-cell grid — read /bundle instead); 409 if the sweep has not finished (status is
    not 'succeeded' — its cells/distribution are partial and would mislead; the IN-FLIGHT progress read is
    where a running sweep is observed). The frontend calls this for a succeeded is_sweep run instead of
    /bundle (a sweep writes per-cell rows + no single-run artifacts)."""
    run = researchdb.get_run(run_id)  # the run row (status, dataset, ...) or None for an unknown id
    if run is None:
        raise HTTPException(404, "run not found")
    try:
        is_sweep = await asyncio.to_thread(researchdb.run_is_sweep, run_id)  # raises RunNotFound for an unknown run
    except researchdb.RunNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if not is_sweep:  # a single run has no cell grid — refuse cleanly (symmetric with the /bundle 409 guard)
        raise HTTPException(409, "this run is not a universe sweep; read its results at /runs/{id}/bundle")
    if run["status"] != "succeeded":  # partial cells -> a misleading distribution; results are a terminal read
        raise HTTPException(409, f"this universe sweep is not finished (status={run['status']}); its per-ticker "
                                 "results are available once it succeeds")
    return await asyncio.to_thread(researchdb.get_run_cells, run_id)


@browser_router.get("/runs/{run_id}/refusals")
async def get_run_refusals(run_id: int):
    """Lane D1: the sweep cells position sizing refused for this run (ticker, combo, reason, message), so a
    refused cell is visible rather than silently missing from /cells. 404 for an unknown run; an empty list
    for a run with none."""
    try:
        refusals = await asyncio.to_thread(researchdb.get_cell_refusals, run_id)
    except researchdb.RunNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"run_id": run_id, "count": len(refusals), "refusals": refusals}


@browser_router.get("/runs/{run_id}/bundle")
async def get_run_bundle(run_id: int):
    """The canonical run bundle for RunExplorer (brief K, read path = build_run_bundle).

    All-inclusive (the owner's own view). 404 if the run is unknown; 409 if it is not
    `succeeded` (no artifacts to bundle, spec §5.5) — RunExplorer renders a placeholder then.

    M5 U2: a universe sweep has per-cell rows and NO single-run artifacts (no equity.parquet), so the bundle
    read would fail loud on a missing artifact. Refuse it cleanly with a 409 pointing at /cells, rather than
    letting build_run_bundle raise an opaque artifact error.

    Lane WB (6.2c, AC-1, DECISIONS #394 ruling 1's own "by its STAGE"): a walk-forward run is ALSO
    dispatched under job kind 'run_sweep' (the same kind an ordinary universe sweep uses), so it
    would otherwise trip the universe-sweep 409 below and never reach build_run_bundle at all. Its
    own granted, linked draw at stage='walkforward' is checked FIRST and, when true, skips that
    sweep guard entirely; every OTHER run whose job kind is 'run_sweep' keeps the existing 409,
    byte-identical, naming "universe sweep".

    Round 2 (CQ-1 / CQ-2): the admission check and the build_run_bundle call both moved into the
    shared _bundle_or_refuse helper (see its own docstring); this function keeps only ITS OWN
    response-shape mapping of the four refusal exceptions, which is why the except blocks below are
    still here even though the counter increments and the admission logic are not.
    """
    try:
        bundle, _is_walkforward = await _bundle_or_refuse(run_id, admit_by_stage=True)
        return bundle
    except export.CorruptRunRecord as e:
        # DBG-15: the run exists but a stored field is broken, so it is a 500 with the specific reason,
        # never the 404 "run not found" a missing run gets. Written before the BundleError clause below.
        raise HTTPException(500, str(e))
    except export.BundleError:
        raise HTTPException(404, "run not found")
    except export.BundleNotAvailable as e:
        raise HTTPException(409, str(e))
    except export.BundleArtifactMissing as e:
        # AC-4: a walk-forward run's own equity relay can fail after the run already succeeded
        # (run_sweep.py's own disclosed failure mode); surface the specific artifact and run id
        # rather than an opaque or fabricated 200.
        raise HTTPException(500, str(e))
    except export.IndicatorsArtifactError as e:
        # A malformed STORED artifact, not a client error: 500, but with the SPECIFIC reason in the
        # body (detail) so RunExplorer surfaces what is wrong instead of a blank page. The empty-frame
        # case never reaches here (it returns {} in _indicators); this is only a real malformation.
        raise HTTPException(500, str(e))


@browser_router.get("/runs/{run_id}/full-metrics")
async def get_run_full_metrics(run_id: int, variation_id: Optional[int] = None,
                               ticker: Optional[str] = None, retry: bool = False,
                               scope: Optional[str] = None):
    """The full-metrics ORCHESTRATION read (M5 C-metrics §2.1): cache-lookup -> dispatch -> honest states.

    vbt-free: the WORKER computes the metrics (C2 §1); this route serves the immutable cache, or dispatches
    the compute job and reports the queued/offline/failed state. THREE read scopes: a SINGLE run passes no
    variation_id/ticker; a universe CELL passes BOTH; the universe AGGREGATE passes scope=aggregate + variation_id
    (no ticker — it is across tickers, the aggregate spec brief §5). States (C2 §4): 'ready' (the payload),
    'computing' (queued/running), 'unavailable' (worker offline / source gone / a failed compute) -- never a
    fabricated number. `retry=1` re-dispatches after a failed compute (the frontend's try-again; without it a
    failed compute stays honest 'unavailable' rather than re-looping forever on a permanently-broken cell).

    404 unknown run; 409 a cell read on a non-sweep, a single/aggregate read on the wrong run kind, or a
    not-succeeded run (symmetric with the /cells <-> /bundle guards); 422 a half-specified cell key, a bad scope,
    or an aggregate read missing variation_id / carrying a ticker.
    """
    run = await asyncio.to_thread(researchdb.get_run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    try:
        is_sweep = await asyncio.to_thread(researchdb.run_is_sweep, run_id)
    except researchdb.RunNotFound:
        raise HTTPException(404, "run not found")

    is_aggregate = scope == "aggregate"
    if scope is not None and not is_aggregate:  # the only defined scope is 'aggregate' (omit it for single/cell)
        raise HTTPException(422, "scope must be 'aggregate' (or omitted for a single run or a cell)")
    is_cell = (variation_id is not None) and (ticker is not None)

    if is_aggregate:
        # An aggregate read is variation_id set + ticker omitted, so it MUST be recognized BEFORE the cell/single
        # key-shape guards below (the half-key 422 + the sweep-not-cell 409), both of which assume the single/cell
        # shape and would otherwise reject it (aggregate spec brief §5).
        if variation_id is None:
            raise HTTPException(422, "scope=aggregate requires a combination to aggregate across the universe")
        if ticker is not None:
            raise HTTPException(422, "scope=aggregate takes a combination only, no ticker (the aggregate is across every member)")
        if not is_sweep:
            raise HTTPException(409, "the universe aggregate is available only for a universe sweep, not a single run")
    else:
        if (variation_id is None) != (ticker is None):  # a cell read needs BOTH halves of the key, or neither
            raise HTTPException(422, "a cell read needs BOTH a combination and a ticker (or neither, for a single run)")
        if is_cell and not is_sweep:
            raise HTTPException(409, "this run is not a universe sweep; read its single-run metrics at /runs/{id}/full-metrics")
        if (not is_cell) and is_sweep:
            raise HTTPException(409, "this run is a universe sweep; read a cell's metrics with a combination and a ticker (or the per-ticker grid at /runs/{id}/cells)")
    if run["status"] != "succeeded":  # partial/failed runs have no terminal artifacts/cells to compute from
        raise HTTPException(409, f"this run has not finished (status={run['status']}); full metrics are available only for a succeeded run")

    # The 3-part cache key: the formula identity + the run's strategy_version + its engine provenance. resolve_
    # formula_version is a vbt-free string fn in full_metrics.py (lazy import; research_lab is on sys.path via
    # the catalog path helper) so this route never drags vbt (C2 §1).
    catalog._ensure_strategies_on_path()  # idempotent
    from strategies.helpers.full_metrics import resolve_formula_version  # vbt-free string fn (no engine import)
    formula_version = resolve_formula_version(run["strategy_version"], run["build_provenance"] or "")
    if is_aggregate:  # the aggregate sentinel key (run_id, variation_id, "*"); the payload builder fails loud on a bad variation
        key = (run_id, variation_id, researchdb._FM_AGGREGATE_TICKER)
    elif is_cell:
        cell = await asyncio.to_thread(researchdb.get_run_cell, run_id, variation_id, ticker)
        if cell is None:
            raise HTTPException(404, f"no cell for combination {variation_id}, ticker {ticker!r}")
        key = (run_id, variation_id, ticker)
    else:
        key = (run_id, researchdb._FM_SINGLE_VARIATION, researchdb._FM_SINGLE_TICKER)

    cached = await asyncio.to_thread(researchdb.get_full_metrics_cache, key[0], key[1], key[2], formula_version)
    if cached is not None:  # hit -> the immutable payload (warehouse-not-cube accelerator, C2 §3)
        return {"status": "ready", "payload": cached["payload"], "computed_at": cached["computed_at"],
                "formula_version": formula_version}

    # Cache miss: a prior FAILED compute is honest absence (don't re-loop) unless retry; an in-flight job is
    # 'computing'; otherwise dispatch (worker-online-gated).
    prior = await asyncio.to_thread(researchdb.latest_full_metrics_job, key[0], key[1], key[2], formula_version)
    if prior is not None and prior["status"] == "failed" and not retry:
        return {"status": "unavailable", "reason": prior["error"] or "the metrics compute failed",
                "formula_version": formula_version}
    if prior is not None and prior["status"] in ("pending", "picked_up"):
        return {"status": "computing", "formula_version": formula_version}
    if not await asyncio.to_thread(researchdb.any_worker_online):
        return {"status": "unavailable",
                "reason": "the research worker is offline; metrics cannot be computed right now",
                "formula_version": formula_version}
    try:
        if is_aggregate:
            payload = await asyncio.to_thread(
                researchdb.build_aggregate_metrics_compute_payload, run_id, variation_id, formula_version)
        elif is_cell:
            payload = await asyncio.to_thread(
                researchdb.build_cell_metrics_compute_payload, run_id, variation_id, ticker, formula_version)
        else:
            payload = await asyncio.to_thread(
                researchdb.build_single_metrics_compute_payload, run_id, formula_version)
    except (researchdb.RunNotFound, researchdb.SizingSpecNotRecordedError,  # P2f: no readable recorded sizing
            researchdb.RecordedSizingSpecUnreadable) as e:  # a source the re-run needs is gone -> honest absence, never a number
        return {"status": "unavailable", "reason": str(e), "formula_version": formula_version}
    await asyncio.to_thread(researchdb.enqueue_full_metrics_job, payload)
    return {"status": "computing", "formula_version": formula_version}


@browser_router.get("/runs/{run_id}/export")
async def export_run(run_id: int, format: str = "json"):
    """K.5 export endpoint: serve the run bundle as JSON, MD, or HTML (M3 spec §5.5).

    Owner-only (behind Caddy basic auth). 404 if the run is unknown; 409 if it is not
    `succeeded` with the locked body shape from §5.5; 400 on unknown format. The HTML
    download is intentionally also delivered as an attachment here — the owner export
    pattern is uniformly downloadable; K.6 public shares serve HTML inline instead.

    Round 2: also 409 (plain body, not the locked shape above) if the run is a universe sweep
    that is not also a walk-forward run, same admission rule /bundle already applies (see
    _bundle_or_refuse); this route never refused a sweep before this fold.
    """
    # Format validation up front — fail loud on an unknown format with a clear list.
    chosen = _EXPORT_FORMATS.get(format)
    if chosen is None:
        raise HTTPException(400, "format must be json|md|html")
    renderer, content_type, ext = chosen
    # Build the bundle with all-inclusive flags (owner sees their own data fully); the
    # exception mapping mirrors /bundle so RunExplorer and /export disagree on neither
    # the status code nor the error body shape (§5.5 + DECISIONS #18 single-write-path
    # discipline applied to the read side).
    #
    # Round 2 (CQ-1 / CQ-2): both the admission check and the build_run_bundle call now go through
    # the shared _bundle_or_refuse helper (admit_by_stage=True), the same as get_run_bundle. This is
    # a genuine behaviour change worth naming plainly: /export never refused a universe sweep before
    # this fold, so it could hit build_run_bundle on a run with no single-run artifacts and fail with
    # whatever exception that read happened to raise, unhandled. It now gets the same clean 404/409
    # admission gate /bundle already had. No probe exercises that edge case either before or after.
    try:
        bundle, _is_walkforward = await _bundle_or_refuse(run_id, admit_by_stage=True)
    except export.CorruptRunRecord as exc:
        # DBG-15: a broken stored field on an existing run is a 500 with the specific reason, not a 404.
        raise HTTPException(500, str(exc))
    except export.BundleError:
        raise HTTPException(404, "run not found")
    except export.BundleNotAvailable as exc:
        # Locked 409 body from §5.5 — the "status" field surfaces the actual run state
        # so callers can react without re-querying the run.
        run = researchdb.get_run(run_id)
        status = (run or {}).get("status", "unknown")
        return JSONResponse(
            status_code=409,
            content={"error": str(exc), "status": status},
        )
    except export.BundleArtifactMissing as exc:
        # Mirror /bundle (AC-4): a walk-forward run's own equity relay can fail after the run
        # already succeeded; surface the specific artifact and run id, never an empty curve.
        raise HTTPException(500, str(exc))
    except export.IndicatorsArtifactError as exc:
        # Mirror /bundle: a malformed stored artifact is a 500 carrying the specific reason, so the
        # export path and RunExplorer never disagree on the status or the error body.
        raise HTTPException(500, str(exc))
    # K.6 retrofit: populate _links so render_md can emit cross-format links in
    # its footer. Owner context uses the /runs/{id}/export URL; the public share
    # endpoint populates _links with /share/{uuid}/?format=... URLs instead.
    bundle["_links"] = {
        "json": _export_url("runs", run_id, "json"),
        "md":   _export_url("runs", run_id, "md"),
        "html": _export_url("runs", run_id, "html"),
    }
    body = renderer(bundle)
    return Response(
        content=body,
        media_type=content_type,
        headers={
            # Attachment disposition so a browser navigation triggers a download
            # rather than rendering inline; per-format extension keeps tools happy.
            "Content-Disposition": f'attachment; filename="run_{run_id}.{ext}"',
        },
    )


# =================================================================
# K.6 — permalink share endpoints (spec §5.6; DECISIONS #29)
# =================================================================
#
# Five routes: 4 owner-only (behind Caddy basic auth on /api/research/shares/...,
# plural) + 1 public (Caddy bypass on /api/research/share/..., singular). The
# Caddy bypass is the auth boundary; FastAPI itself does NOT enforce basic auth.

import uuid as _uuid  # for UUID4 validation on the public path


class _CreateShareBody(BaseModel):
    """POST body for share creation. Each field optional; defaults per L25/L27.

    `view_limit` is special-cased: omitted -> default 10 (L27); explicit null ->
    unlimited; integer -> that integer. Pydantic's default_factory pattern
    distinguishes "absent" from "null" cleanly.
    """
    include_source: bool = True
    include_settings: bool = True
    include_indicators: bool = True
    include_trades: bool = True
    include_logs: bool = False
    view_limit: int | None = 10  # API default per L27 (NOT a SQL DEFAULT)


class _PatchShareBody(BaseModel):
    """PATCH body for share updates. Every field optional; ... means 'unchanged'.

    Pydantic does not distinguish "absent" from "null" for Optional[int] cleanly
    without `model_config['extra']` tricks. We use the model_dump exclude_unset
    pattern at the route layer instead.
    """
    view_limit: int | None = None
    include_source: bool | None = None
    include_settings: bool | None = None
    include_indicators: bool | None = None
    include_trades: bool | None = None
    include_logs: bool | None = None


@browser_router.get("/runs/{run_id}/shares")
async def list_run_shares(run_id: int):
    """Active (non-revoked) shares for a run, newest first.

    Backs the K.6 share-management section in RunExplorer. NOT in spec §5.6 (which
    only lists per-uuid owner endpoints) — needed because the management UI lists
    every active share, not just one by uuid. Uses the partial index
    ix_shared_runs_active for cheap lookup.
    """
    run = researchdb.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return researchdb.list_active_shares_for_run(run_id)


@browser_router.post("/runs/{run_id}/share")
async def create_share(run_id: int, body: _CreateShareBody):
    """Create a share for a succeeded run (M3 spec §5.6 POST).

    404 if the run is unknown; 409 if it is not `succeeded` with the locked
    §5.5 body (a non-succeeded run has no bundle, so no share). Returns
    {uuid, url} on success and fires research.share_created (spec §5.8).
    """
    run = researchdb.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    if run["status"] != "succeeded":
        # Mirror the /export 409 body so callers see one shape for both.
        return JSONResponse(
            status_code=409,
            content={
                "error": f"run {run_id} status is {run['status']}; "
                         "bundles available only for succeeded runs",
                "status": run["status"],
            },
        )
    share = researchdb.create_share(
        run_id,
        include_source=body.include_source,
        include_settings=body.include_settings,
        include_indicators=body.include_indicators,
        include_trades=body.include_trades,
        include_logs=body.include_logs,
        view_limit=body.view_limit,
    )
    # Public URL — uses the singular /share/{uuid} path Caddy bypasses; default
    # format=html means a browser click renders inline (Fork K6-B).
    share_url = _export_url("share", share["uuid"], "html").replace("?format=html", "")
    await broadcaster.publish("research.share_created", {
        "uuid": share["uuid"], "run_id": share["run_id"],
        "view_limit": share["view_limit"], "created_at": share["created_at"],
    })
    return {"uuid": share["uuid"], "url": share_url}


@browser_router.get("/shares/{share_uuid}")
async def get_share_owner(share_uuid: str):
    """Owner introspection for a share (Fork K6-D-1): share metadata + run summary."""
    share = researchdb.get_share(share_uuid)
    if share is None:
        raise HTTPException(404, "share not found")
    # Run summary attached for the management-section UI — saves a second roundtrip.
    run = researchdb.get_run(share["run_id"]) or {}
    strat = researchdb.get_strategy(run.get("strategy_id")) if run.get("strategy_id") else None
    return {
        "share": share,
        "run": {"id": run.get("id"), "name": run.get("name"),
                "created_at": run.get("created_at"), "status": run.get("status"),
                "strategy_name": (strat or {}).get("name")},
    }


@browser_router.patch("/shares/{share_uuid}")
async def patch_share(share_uuid: str, body: _PatchShareBody):
    """Update view_limit and/or content flags. Refuses on a revoked share (Fork K6-M).

    Uses model_dump(exclude_unset=True) so the caller distinguishes "do not
    change" from "set to null" (Pydantic preserves the field-presence info).
    """
    changes = body.model_dump(exclude_unset=True)  # only keys the caller set
    # Translate "field present" into the helper's per-arg API.
    try:
        if "view_limit" in changes:
            share = researchdb.update_share(share_uuid, view_limit=changes["view_limit"])
        else:
            share = researchdb.update_share(share_uuid)  # no-op view_limit, may still flip flags
        for col in ("include_source", "include_settings", "include_indicators",
                    "include_trades", "include_logs"):
            if col in changes:
                share = researchdb.update_share(share_uuid, **{col: changes[col]})
    except researchdb.ShareNotFound:
        raise HTTPException(404, "share not found")
    except researchdb.ShareRevoked:
        # Fork K6-M: revoke is one-way; PATCH on a revoked share returns 409.
        raise HTTPException(409, "share is revoked; create a new share to re-enable")
    return share


@browser_router.delete("/shares/{share_uuid}")
async def delete_share(share_uuid: str):
    """Manual revoke. Idempotent. Fires research.share_revoked with reason='manual'."""
    try:
        share = researchdb.revoke_share(share_uuid)
    except researchdb.ShareNotFound:
        raise HTTPException(404, "share not found")
    await broadcaster.publish("research.share_revoked", {
        "uuid": share["uuid"], "run_id": share["run_id"],
        "revoked_at": share["revoked_at"], "reason": "manual",
    })
    return share


@browser_router.get("/share/{share_uuid}")
async def get_share_public(share_uuid: str, format: str = "html"):
    """Public share consumer endpoint (Caddy bypass; spec §5.6).

    Default format=html (Fork K6-N): a browser click renders inline. JSON/MD
    return as attachments (Fork K6-B). 404 missing / malformed uuid (per
    critique item 9 fold: a UUID that does not parse is just one we cannot
    find); 410 dead share (revoked OR over-limit); 400 unknown format.
    Hardening headers per §5.7 (X-Robots-Tag, Referrer-Policy) on EVERY
    response, including errors.
    """
    hardening_headers = {
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
    }
    # Malformed uuid path-param -> 404 (per spec §5.6 "missing -> 404"). Avoids
    # the FastAPI auto-422 trap that would expose internal validation details.
    try:
        _uuid.UUID(share_uuid)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "share not found"},
                            headers=hardening_headers)
    # M2.5 brief F: the public share route now dispatches on share.run_id vs
    # share.batch_id. The format-dispatch table differs between the two paths
    # (K.5 renderers vs quality renderers), so the lookup is done AFTER we
    # know which kind of bundle we are rendering.

    # Existence first: distinguishes 404 (no such uuid) from 410 (revoked/over-limit).
    share = researchdb.get_share(share_uuid)
    if share is None:
        return JSONResponse(status_code=404, content={"error": "share not found"},
                            headers=hardening_headers)
    # Atomic claim: increment view + auto-revoke at limit. Returns None for dead
    # share; the get_share above already proved existence, so None now = dead.
    pre_claim_revoked = share["revoked_at"] is not None
    claimed = researchdb.try_claim_share_view(share_uuid)
    if claimed is None:
        return JSONResponse(status_code=410, content={"error": "share is revoked or over view limit"},
                            headers=hardening_headers)

    # Brief F dispatch: pick the renderer table + bundle builder by which target
    # the share row points at. The table-level CHECK ((run_id IS NULL) !=
    # (batch_id IS NULL)) guarantees exactly one is set.
    if claimed.get("batch_id") is not None:
        # ---- Batch path ----
        chosen = _QUALITY_EXPORT_FORMATS.get(format)
        if chosen is None:
            return JSONResponse(status_code=400,
                                content={"error": "format must be json|md|html"},
                                headers=hardening_headers)
        renderer, content_type, ext = chosen
        # If the UPDATE's CASE set revoked_at on THIS call, the limit was hit.
        # Same SSE event shape as the run path, with batch_id surfaced too so
        # the owner UI can route updates correctly.
        if claimed["revoked_at"] is not None and not pre_claim_revoked:
            await broadcaster.publish("research.share_revoked", {
                "uuid": claimed["uuid"], "batch_id": claimed["batch_id"],
                "revoked_at": claimed["revoked_at"], "reason": "view_limit_reached",
            })
        try:
            bundle = event_quality_bundle.build_quality_report_bundle(
                claimed["batch_id"],
                include_source=bool(claimed["include_source"]),
                include_settings=bool(claimed["include_settings"]),
                include_logs=bool(claimed["include_logs"]),
            )
        except event_quality_bundle.BundleError:
            # The batch referenced by the share row has vanished. We have no FK
            # to enforce cross-DB cascade, so this branch is the honest 404.
            return JSONResponse(status_code=404,
                                content={"error": "share not found"},
                                headers=hardening_headers)
        except event_quality_bundle.BundleStillRunning:
            # A share targeting a still-running batch is rare (the operator
            # would normally share after close), but possible. Surface as 410
            # so the receiver sees a non-200 the same way the run path treats
            # a not-yet-available run.
            return JSONResponse(status_code=410,
                                content={"error": "batch is still running"},
                                headers=hardening_headers)
        # Public _links point at the share URL, same pattern as the run path.
        bundle["_links"] = {
            "json": _export_url("share", share_uuid, "json"),
            "md":   _export_url("share", share_uuid, "md"),
            "html": _export_url("share", share_uuid, "html"),
        }
        body = renderer(bundle)
        response_headers = dict(hardening_headers)
        response_headers["Content-Disposition"] = (
            "inline" if format == "html"
            else f'attachment; filename="share_{share_uuid[:8]}.{ext}"'
        )
        return Response(content=body, media_type=content_type, headers=response_headers)

    # ---- Run path (M3 K.6 original; round 2 (F1) adds the BundleArtifactMissing branch below,
    # through the shared _bundle_or_refuse helper called with admit_by_stage=False. Round 3 (NF1):
    # the helper now DETERMINES is_walkforward here too (the cheap run_is_sweep-first path), so the
    # not-succeeded and malformed-indicators counters can fire correctly for a shared walk-forward
    # run's own failure; admit_by_stage=False still means that value is never used to REFUSE anything,
    # so a 409 sentence naming another run's nature still can never leak on this public route, and an
    # unknown run still surfaces as "share not found" exactly as it always did) ----
    chosen = _EXPORT_FORMATS.get(format)
    if chosen is None:
        return JSONResponse(status_code=400, content={"error": "format must be json|md|html"},
                            headers=hardening_headers)
    renderer, content_type, ext = chosen
    # If the UPDATE's CASE set revoked_at on THIS call, the limit was just hit.
    # Fire the auto-revoke SSE event so the owner UI updates in real time.
    if claimed["revoked_at"] is not None and not pre_claim_revoked:
        await broadcaster.publish("research.share_revoked", {
            "uuid": claimed["uuid"], "run_id": claimed["run_id"],
            "revoked_at": claimed["revoked_at"], "reason": "view_limit_reached",
        })
    # Build the bundle using the share's content flags (different shares of the
    # same run can show different content per L25).
    try:
        bundle, _is_walkforward = await _bundle_or_refuse(
            claimed["run_id"],
            admit_by_stage=False,
            include_source=bool(claimed["include_source"]),
            include_settings=bool(claimed["include_settings"]),
            include_indicators=bool(claimed["include_indicators"]),
            include_trades=bool(claimed["include_trades"]),
            include_logs=bool(claimed["include_logs"]),
        )
    except export.CorruptRunRecord as exc:
        # DBG-15: the run exists but a stored field is broken: a 500 with the specific reason, never
        # the 404 "share not found" a dangling share gets. Still carries the public hardening headers.
        return JSONResponse(status_code=500, content={"error": str(exc)},
                            headers=hardening_headers)
    except export.BundleError:
        # The share row references a run that vanished — should not happen since
        # CASCADE deletes shares with their run, but be honest if it does.
        return JSONResponse(status_code=404, content={"error": "share not found"},
                            headers=hardening_headers)
    except export.BundleNotAvailable as exc:
        return JSONResponse(status_code=410, content={"error": str(exc)},
                            headers=hardening_headers)
    except export.BundleArtifactMissing as exc:
        # F1 (lane WB round 2, docs/units/WB/reads/read_1.md on main): a walk-forward run shared
        # publicly with its equity.parquet missing used to fall through with no handler here and
        # surface as an unnamed 500 on a public URL. Same 500-with-reason shape as /bundle and
        # /export, still carrying the public hardening headers.
        return JSONResponse(status_code=500, content={"error": str(exc)},
                            headers=hardening_headers)
    except export.IndicatorsArtifactError as exc:
        # A malformed stored artifact: 500 with the specific reason, consistent with /bundle and
        # /export (the empty-frame case returns {} in _indicators and never reaches here).
        return JSONResponse(status_code=500, content={"error": str(exc)},
                            headers=hardening_headers)
    # Public _links point at the share URL (not the owner export URL) — receivers
    # of the share do not have basic-auth credentials for the owner path.
    bundle["_links"] = {
        "json": _export_url("share", share_uuid, "json"),
        "md":   _export_url("share", share_uuid, "md"),
        "html": _export_url("share", share_uuid, "html"),
    }
    body = renderer(bundle)
    # Per Fork K6-B: HTML inline (no attachment), JSON / MD download. Filename
    # uses the first 8 chars of the uuid so file-system-friendly without leaking
    # more secret than the URL already does.
    response_headers = dict(hardening_headers)
    response_headers["Content-Disposition"] = (
        "inline" if format == "html"
        else f'attachment; filename="share_{share_uuid[:8]}.{ext}"'
    )
    return Response(content=body, media_type=content_type, headers=response_headers)


# ============================================================================
# M2.5 brief E — Canonical loader endpoint (POST /api/research/event-batches/load)
# ============================================================================


class _LoadBatchBody(BaseModel):
    """Request body for the canonical loader endpoint per spec brief §6.1.

    The endpoint validates the source_file_path prefix against
    config.EVENT_LOADER_SOURCE_PREFIX so a path outside the staging directory
    is rejected as 400 before any file IO. Default policy is `quarantine`
    matching the EL7 working default for ingest paths.
    """
    source_file_path: str = Field(..., min_length=1)
    policy: Literal["quarantine", "abort_on_first_error"] = "quarantine"


def _loader_source_descriptor(path: Path, file_bytes: int,
                              sha256_hex: str) -> str:
    """Serialize the loader's source_descriptor JSON per spec brief §3.5 / RB2.

    Loader batches carry `request_id = NULL` per arch §4.4 verbatim; the file
    lineage lives entirely in `research_event_batches.source_descriptor`. The
    operator's "have I loaded this file before" query at the bottom of §3.5
    matches against `json_extract(source_descriptor, '$.source_file_basename')`,
    so the basename + content hash + bytes are the locked shape.
    """
    return json.dumps({
        "kind": "canonical_loader",
        "source_file_basename": path.name,
        "source_file_path": str(path),
        "source_file_bytes": file_bytes,
        "source_file_sha256": sha256_hex,
    }, sort_keys=True, separators=(",", ":"))


def _sha256_of_file(path: Path) -> tuple[int, str]:
    """Return (file_bytes, sha256_hex) by streaming the file once.

    Streaming keeps peak memory bounded by the chunk size; the file may be
    larger than RAM for a future analyst classifier run. Used both for the
    source_descriptor lineage and for an operator-side "already loaded"
    check in journal logs.
    """
    import hashlib
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
            total += len(chunk)
    return total, h.hexdigest()


def _detect_running_loader(events_db_path: Optional[Path] = None) -> Optional[int]:
    """Return the batch_id of an already-running canonical_loader batch, or None.

    Per spec brief MN3 / §6.1: "another loader is currently writing" is
    detected via a single SELECT on research_event_batches with
    source='canonical_loader' AND status='running'. The endpoint serializes
    on a row check immediately before opening the new batch; the check runs
    inside the same connection but does NOT need to be inside a transaction
    because the FastAPI request handler is single-threaded per request, and
    concurrent POST /load requests serialize on uvicorn's worker queue.

    A future revision that runs uvicorn with multiple workers would need to
    upgrade the check to a SELECT inside the new-batch open transaction (the
    detection-vs-INSERT race becomes real with multiple writers). M2.5
    ships single-worker uvicorn per system_reference §15.
    """
    with eventsdb.get_events_conn(events_db_path) as conn:
        row = conn.execute(
            """SELECT batch_id FROM research_event_batches
                WHERE source='canonical_loader' AND status='running'
                LIMIT 1"""
        ).fetchone()
    return int(row["batch_id"]) if row is not None else None


@browser_router.post("/event-batches/load")
async def load_event_batch(body: _LoadBatchBody):
    """Run the canonical JSON-lines loader synchronously inside the request handler.

    Spec brief §6.1 (endpoint) + §4.4 (R23 envelope) + §4.4.1 (per-line flow).
    Returns the batch's terminal state in the response body, not a 202 +
    job handle: M2.5 loader volumes fit inside the request timeout.

    Errors per §6.1:
      400 invalid_source_file_path      — path not under EVENT_LOADER_SOURCE_PREFIX
      404 source_file_not_found
      409 loader_already_running        — another canonical_loader batch is running
      422 loader_file_unreadable        — whole-file structural failure (RB1)
      500 <exception_class_snake_case>  — any other shared-core class

    Per arch §11 §20 the loader's cancellation checkpoint is between lines.
    M2.5 runs the loader synchronously inside the request handler so the
    only path through "cancellation" is uvicorn process shutdown; a half-
    loaded batch is left `running` for the secondary sweep at next startup
    to roll back. No async-task indirection needed at M2.5 volumes.
    """
    started = time.time()

    # 1. Path-prefix validation (400 invalid_source_file_path)
    try:
        candidate = Path(body.source_file_path).resolve(strict=False)
    except (OSError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_source_file_path",
                     "path": body.source_file_path},
        )
    prefix = EVENT_LOADER_SOURCE_PREFIX.resolve()
    # Use Path.is_relative_to (Python 3.9+) so a path that looks like the
    # prefix string but escapes via .. or symlink is rejected. relative_to
    # raises ValueError on non-prefix; is_relative_to returns False.
    if not candidate.is_relative_to(prefix):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_source_file_path",
                     "path": body.source_file_path},
        )

    # 2. File-existence (404 source_file_not_found)
    if not candidate.exists() or not candidate.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": "source_file_not_found",
                     "path": str(candidate)},
        )

    # 3. Already-running detection (409 loader_already_running)
    existing = _detect_running_loader()
    if existing is not None:
        return JSONResponse(
            status_code=409,
            content={"error": "loader_already_running",
                     "batch_id": existing},
        )

    # 4. Whole-file structural check (422 loader_file_unreadable per RB1)
    # We do this BEFORE opening a batch row: under whole-file failure the spec
    # brief §6.1 422 body is the terminal response; no batch row is created.
    # This deviates from §6.1's worded description ("Batch row is opened then
    # immediately closed status='failed' with failure_class='LoaderFileUnreadable'")
    # by NOT writing a batch row for the whole-file failure. Rationale: writing
    # a failed batch row for every accidentally-misformatted file pollutes the
    # batch list with one row per typo, which is far more painful for operators
    # to clean up than the small loss of "everywhere a file was attempted" audit
    # trail. Per-line failures still produce a batch row (the standard path).
    # Logged here as a deviation; the journal already records the 422 response
    # which is sufficient operator audit for the no-batch path.
    try:
        event_loader_jsonl.assert_loader_file_readable(candidate)
    except _LoaderFileUnreadable as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "loader_file_unreadable",
                     "reason": str(exc)},
        )

    # 5. Compute source-descriptor lineage (file basename + sha256 + bytes).
    file_bytes, sha256_hex = await asyncio.to_thread(_sha256_of_file, candidate)
    source_descriptor = _loader_source_descriptor(
        candidate, file_bytes=file_bytes, sha256_hex=sha256_hex,
    )

    # 6. Iterate + write_event_batch. Synchronous via asyncio.to_thread so the
    # uvicorn loop is not blocked while the loader iterates a multi-thousand
    # line file. parse_quarantines_collector is the by-reference list brief D
    # introduced; the loader appends per-line failures to it under quarantine
    # policy. write_event_batch ingests them at close as quarantine rows.
    parse_quarantines: list = []

    now_epoch = int(time.time())

    def _run_load() -> event_write_core.BatchResult:
        records_iter = event_loader_jsonl.iter_canonical_records(
            candidate,
            policy=body.policy,
            parse_quarantines_collector=parse_quarantines,
            now_epoch=now_epoch,
        )
        return event_write_core.write_event_batch(
            source="canonical_loader",
            kind="load",
            policy=body.policy,
            records=records_iter,
            source_descriptor=source_descriptor,
            # Brief E loader cadence: smaller chunks than the default 1000.
            # The loader's per-line cost is dominated by the ticker resolution
            # SQL, not the write — a 200-line chunk keeps the writer-lock
            # duration bounded under heavy ticker-lookup load.
            commit_chunk_size=200,
            # Brief H: the loader runs inside the dashboard process so the
            # research.db path is the default; the catalog refresh on clean
            # close fires automatically.
            research_db_path=researchdb.DB_PATH,
            parse_quarantines_collector=parse_quarantines,
        )

    try:
        result = await asyncio.to_thread(_run_load)
    except event_write_core._ReaperFlippedBatch as exc:
        # The reaper flipped the loader's batch mid-run (long-lived loader,
        # heartbeat went stale). Surface as 500; the secondary sweep at next
        # dashboard startup will roll back any committed events.
        return JSONResponse(
            status_code=500,
            content={"error": "orphaned_running_batch",
                     "batch_id": exc.batch_id},
        )
    except Exception as exc:
        # Last-resort 500 with the exception class name in snake_case per
        # §6.1's 500 contract. Any specific class would have been caught
        # inside write_event_batch's policy-aware path; reaching here means
        # an unexpected shape we want loud in journald.
        return JSONResponse(
            status_code=500,
            content={"error": _snake_case(type(exc).__name__),
                     "message": str(exc)[:1024]},
        )

    # 7. Broadcast SSE events in-process (the loader runs inside the dashboard
    # process; the spec brief §6.12 cross-process notify endpoint is the
    # fetcher's path, not the loader's). Same payload shape so any UI sees
    # the loader's batch alongside the fetcher's.
    await broadcaster.publish(
        "research.event_batch_completed",
        {
            "batch_id": result.batch_id,
            "event_type": "analyst_rating",  # canonical loader's primary type
            "source": "canonical_loader",
            "status": result.status,
            "content_hash": result.content_hash,
            "event_count": result.event_count,
            "quarantine_count": result.quarantine_count,
            "catalog_refresh_status":
                "succeeded" if result.catalog_refreshes else "no_pairs",
        },
    )

    duration_ms = int((time.time() - started) * 1000)
    return {
        "batch_id": result.batch_id,
        "status": result.status,
        "event_count": result.event_count,
        "quarantine_count": result.quarantine_count,
        "content_hash": result.content_hash,
        "duration_ms": duration_ms,
        # Surface failure_class on the response when the batch closed failed
        # (abort path) so the operator sees the cause without a separate
        # /quality fetch.
        "failure_class": result.failure_class,
    }


# =============================================================
# pre-M4 Brief A1 / D4 — reference-data loader routes (CLI-only until now).
# All three run their (seconds-scale) loader off the event loop via asyncio.to_thread (the
# load_event_batch precedent: "no async-task indirection needed at these volumes"). R2's
# "must not freeze the dashboard" is met because the event loop is never blocked; the
# triggering request waits for the terminal result (RATIFIED to_thread, DECISIONS #74 item #2).
# =============================================================

@browser_router.post("/universes")
async def create_universe(body: UniverseCreateIn):
    """Add a today's-list universe (D4). Wraps events.universe.load_universe, whose catalog
    write goes through the now registry-gated find_or_create_universe (§A1.1). The tickers
    array is the raw textarea content; blanks and #-comment lines are trimmed HERE (mirroring
    the CLI parse loop) so the API does not trust the client to clean its input. Fast (one
    bridge SELECT per ticker + one membership replace), so synchronous via to_thread."""
    # Clean the ticker list server-side: strip each, drop blanks and #-comments (cli.py parity).
    tickers = [t.strip() for t in body.tickers]
    tickers = [t for t in tickers if t and not t.startswith("#")]
    if not tickers:
        raise HTTPException(400, "no tickers after trimming blanks and comments")

    def _load():
        return event_universe.load_universe(
            universe_name=body.name,
            constituent_list=tickers,
            source_label=body.source_label,
        )

    try:
        result = await asyncio.to_thread(_load)
    except event_universe.UniverseEmptyError as exc:
        # Cowork S1 / D2: zero constituents resolved, so no usable universe was persisted
        # (a brand-new catalog row was compensate-deleted; an existing universe kept its
        # members). Surface a plain-English 400 instead of returning a zombie empty universe.
        raise HTTPException(
            400,
            detail=(
                f"No tickers resolved to a company: 0 of {exc.constituents_in} matched the "
                f"company-to-ticker map ({len(exc.unresolvable)} unknown, "
                f"{len(exc.ambiguous)} ambiguous). Check the tickers, or sync the "
                f"company-to-ticker map first."
            ),
        ) from exc
    return {
        "universe_id": result.universe_id,
        "created": result.created,
        "loaded": result.loaded,
        "constituents_in": result.constituents_in,
        "unresolvable": result.unresolvable,
        "ambiguous": result.ambiguous,
    }


@browser_router.post("/cik-ticker-map")
async def load_cik_ticker_map_route(body: CikTickerMapLoadIn):
    """Top up the CIK↔ticker bridge from SEC's company_tickers_exchange.json (D4). Wraps
    events.cik_ticker_map.load_cik_ticker_map (the shared impl the CLI also calls). The live
    fetch is network-bound (~10k rows + one GET), so it runs off the event loop via to_thread.
    A bad source file → 400; an upstream SEC fetch failure → 502."""
    def _load():
        return event_cik_ticker_map.load_cik_ticker_map(source_file=body.source_file)

    try:
        result = await asyncio.to_thread(_load)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        # Operator-provided bad source file, or a payload missing the required fields.
        raise HTTPException(400, f"cik_ticker_map_source_invalid: {exc}")
    except urllib.error.URLError as exc:
        # The live SEC fetch failed (HTTPError is a URLError subclass, caught here too).
        raise HTTPException(502, f"cik_ticker_map_fetch_failed: {exc}")
    return {
        "rows_in_payload": result.rows_in_payload,
        "inserted": result.inserted,
        "skipped_existing": result.skipped_existing,
        "names_backfilled": result.names_backfilled,
    }


@browser_router.get("/cik-ticker-map/search")
async def cik_ticker_map_search(
    q: str = Query(..., min_length=1),                       # the search term (required, prefix match)
    limit: int = Query(25, ge=1, le=100),                    # row cap (default 25, hard-capped at 100)
):
    """Search the CIK↔ticker bridge by ticker or company name (ANA-1 §1.3, the catalog entry point).

    Case-insensitive PREFIX match of `q` against ticker AND name, returning one row per distinct
    (cik, ticker) ordered exact-ticker-first then alphabetically by name. The bridge can hold the
    same (cik, ticker) under multiple sources and the name is backfilled only on sec_company_tickers
    rows (§1.2), so a manual/openfigi duplicate carries a NULL name; the de-dup takes the
    sec_company_tickers row first, so the null-name duplicate is never surfaced. Read-only.
    """
    term = q.strip()                                         # trim so a stray space is not part of the prefix
    if not term:                                             # whitespace-only q is an empty search, reject it
        raise HTTPException(422, "cik_ticker_search_empty_query: q is blank after trimming")

    def _search():
        with eventsdb.get_events_conn() as conn:             # Row factory set by get_events_conn
            return event_cik_ticker_map.search(conn, term, limit)

    results = await asyncio.to_thread(_search)
    return {"q": term, "results": results}


@browser_router.get("/eodhd-tickers/search")
async def eodhd_tickers_search(
    # min_length=1 validates the RAW length, so a whitespace-only q passes this outer guard and is
    # caught by the trim check below (the two-stage guard the cik precedent uses). Keep both halves.
    q: str = Query(..., min_length=1),                       # the search term (required, prefix match)
    limit: int = Query(25, ge=1, le=100),                    # row cap (default 25, hard-capped at 100)
):
    """Search the EODHD daily availability index by bare ticker (M5 U0, AD-M5-14).

    Case-insensitive PREFIX match of `q` against the bare ticker, exact-ticker-first then
    alphabetical. Mirrors cik_ticker_map_search's shape so the future F5 alpaca/binance merge sees
    two parallel helpers. Read-only over research_eodhd_tickers (NOT the CIK bridge in eventsdb).
    """
    term = q.strip()                                         # trim so a stray space is not part of the prefix
    if not term:                                             # whitespace-only q is an empty search, reject it
        raise HTTPException(422, "eodhd_ticker_search_empty_query: q is blank after trimming")
    rows = await asyncio.to_thread(researchdb.search_eodhd_tickers, term, limit)
    return {"q": term, "results": [{"ticker": t} for t in rows]}


@browser_router.post("/price-universes")
async def create_price_universe_route(body: PriceUniverseCreateIn):
    """Create a price universe (M5 U1, R-UNIV): validate typed tickers against the EODHD availability
    index, persist the catalog row + a membership row per resolved ticker, report the excluded.

    Mirrors POST /universes but validates against AVAILABILITY (research_eodhd_tickers, U0), not the CIK
    bridge. Fast (one batched index read + a membership executemany), so synchronous via to_thread. An
    all-excluded body → 422 (price_universe_empty, fail loud — no zombie empty universe). A duplicate
    name → 409.
    """
    def _create():
        return researchdb.create_price_universe(
            body.name, body.tickers, body.window_start_ts, body.window_end_ts,
            display_name=body.display_name,
        )

    try:
        result = await asyncio.to_thread(_create)
    except researchdb.StrategyValidationError as exc:
        raise HTTPException(422, detail=f"price_universe_display_name_invalid: {exc}") from exc
    except researchdb.PriceUniverseEmptyError as exc:
        raise HTTPException(
            422,
            detail=f"price_universe_empty: no typed ticker is in the EODHD availability index. {exc}",
        ) from exc
    except researchdb.PriceUniverseMixedSource as exc:
        # GV2-4a: made-up and real tickers cannot share one universe; both lists are named.
        raise HTTPException(
            422,
            detail=f"price_universe_mixed_source: {exc}",
        ) from exc
    except researchdb.PriceUniverseWindowInvalid as exc:
        raise HTTPException(
            422,
            detail=f"price_universe_window_invalid: {exc}",
        ) from exc
    except researchdb.PriceUniverseWindowRequired as exc:
        # Item 5b: a universe with a missing bound would only fail later, at dispatch time,
        # naming R-T15 with no obvious cause. Refused here instead, at creation, naming the
        # missing side plainly.
        raise HTTPException(
            422,
            detail=f"price_universe_window_required: {exc}",
        ) from exc
    except researchdb.PriceUniverseNameExists as exc:
        raise HTTPException(
            409,
            detail=f"price_universe_name_exists: a price universe named {body.name!r} already exists "
                   f"(id={exc.existing_id}).",
        ) from exc
    company_profiles.wake()  # UNI1b: the reading loop picks up the new members' SEC company records
    return result


@browser_router.get("/price-universes/sector-mix")
async def price_universe_sector_mix_route(include_synthetic: bool = False):
    """UNI1: per price universe, today's members counted by sector (the colour key of the Universes tab).

    Declared above every /price-universes/{universe_id} route so the literal segment is not read as an id.
    """
    universes = await asyncio.to_thread(researchdb.price_universe_sector_mix, include_synthetic)
    return {"universes": universes}


@browser_router.get("/price-universes/{universe_id}/members")
async def list_price_universe_members_route(universe_id: int):
    """The members (bare tickers) of a price universe, sorted (the prefix read, AD-M5-11)."""
    members = await asyncio.to_thread(researchdb.list_price_universe_members, universe_id)
    return {"universe_id": universe_id, "members": members}


@browser_router.post("/price-universes/{universe_id}/members")
async def add_price_universe_members_route(universe_id: int, body: PriceUniverseMembersIn):
    """DL1 (DECISIONS #455): add tickers to a price universe. Reports added, excluded (not in the EODHD
    index) and already. 404 unknown or deleted universe; 422 mixed made-up and real tickers, or nothing to add."""
    try:
        result = await asyncio.to_thread(
            researchdb.add_price_universe_members, universe_id, body.tickers, int(time.time()))
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, detail=f"price_universe_not_found: {exc}") from exc
    except researchdb.PriceUniverseMixedSource as exc:
        raise HTTPException(422, detail=f"price_universe_mixed_source: {exc}") from exc
    except researchdb.PriceUniverseAddNothing as exc:
        raise HTTPException(
            422,
            detail=f"price_universe_add_nothing: no ticker was added. Not in the EODHD index: "
                   f"{exc.excluded}. Already members: {exc.already}.",
        ) from exc
    company_profiles.wake()  # UNI1b: the reading loop picks up the new members' SEC company records
    return result


@browser_router.delete("/price-universes/{universe_id}/members/{ticker}")
async def remove_price_universe_member_route(universe_id: int, ticker: str):
    """DL1 (DECISIONS #455): remove one ticker; the universe keeps the dated record of it. 404 unknown
    universe or a ticker that is not a member; 422 for the last member."""
    try:
        return await asyncio.to_thread(
            researchdb.remove_price_universe_member, universe_id, ticker, int(time.time()))
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, detail=f"price_universe_not_found: {exc}") from exc
    except researchdb.MemberNotFound as exc:
        raise HTTPException(404, detail=f"member_not_found: {exc}") from exc
    except researchdb.PriceUniverseLastMember as exc:
        raise HTTPException(422, detail=f"price_universe_last_member: {exc}") from exc


@browser_router.get("/price-universes/{universe_id}/members/detail")
async def price_universe_member_detail_route(universe_id: int, as_of: Optional[int] = None):
    """DL1: each member at `as_of` (unix seconds, default now) with company name, added date and daily price coverage."""
    try:
        return await asyncio.to_thread(researchdb.price_universe_member_detail, universe_id, as_of)
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, detail=f"price_universe_not_found: {exc}") from exc


@browser_router.get("/price-universes/{universe_id}/runs")
async def list_price_universe_runs_route(universe_id: int):
    """DL1: the runs on a price universe, newest first (at most 500)."""
    def _read():
        with researchdb.get_conn() as conn:
            known = conn.execute(
                "SELECT 1 FROM research_datasets WHERE id = ? AND dataset_type = ?;",
                (universe_id, researchdb.DATASET_TYPE_PRICE_UNIVERSE)).fetchone()
        if known is None:
            raise researchdb.PriceUniverseNotFound(f"price universe {universe_id} not found")
        return researchdb.list_runs_for_universe(universe_id)

    try:
        runs = await asyncio.to_thread(_read)
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, detail=f"price_universe_not_found: {exc}") from exc
    return {"universe_id": universe_id, "runs": runs}


@browser_router.patch("/price-universes/{universe_id}")
async def rename_price_universe_route(universe_id: int, body: PriceUniverseRenameIn):
    """Edit a price universe (M5 U1, UNI1): its name and/or its display name. 404 if no price_universe row with
    that id; 409 if another price universe already holds the new name (the rename keeps name == symbol, the
    UNIQUE discriminator); 422 when neither is sent or the display name is blank or too long."""
    if body.name is None and body.display_name is None:
        raise HTTPException(422, "price_universe_edit_empty: send a name or a display name")

    def _edit():
        if body.display_name is not None:
            researchdb._clean_display_name(body.display_name)   # refuse a bad text before the rename writes
        if body.name is not None and not researchdb.rename_price_universe(universe_id, body.name):
            return False
        if body.display_name is not None and not researchdb.set_price_universe_display_name(universe_id, body.display_name):
            return False
        with researchdb.get_conn() as conn:
            return conn.execute(
                "SELECT name, display_name FROM research_datasets WHERE id = ? AND dataset_type = ?;",
                (universe_id, researchdb.DATASET_TYPE_PRICE_UNIVERSE)).fetchone()

    try:
        row = await asyncio.to_thread(_edit)
    except researchdb.StrategyValidationError as exc:
        raise HTTPException(422, detail=f"price_universe_display_name_invalid: {exc}") from exc
    except researchdb.PriceUniverseNameExists as exc:
        raise HTTPException(
            409,
            detail=f"price_universe_name_exists: a price universe named {body.name!r} already exists "
                   f"(id={exc.existing_id}).",
        ) from exc
    if not row:
        raise HTTPException(404, f"no price universe with id {universe_id}")
    return {"universe_id": universe_id, "name": row["name"], "display_name": row["display_name"]}


@browser_router.patch("/price-universes/{universe_id}/window")
async def update_price_universe_window_route(universe_id: int, body: PriceUniverseWindowIn):
    """Set or clear a price universe's test window (AD-UU-3, B1.3). A DISTINCT sub-route from the rename
    PATCH (/price-universes/{id}, which requires name) so the two do not collide. 404 if no price_universe
    row with that id (fail loud, no silent no-op); 422 if start > end."""
    def _update():
        return researchdb.update_price_universe_window(
            universe_id, body.window_start_ts, body.window_end_ts,
        )

    try:
        return await asyncio.to_thread(_update)
    except researchdb.PriceUniverseWindowInvalid as exc:
        raise HTTPException(422, detail=f"price_universe_window_invalid: {exc}") from exc
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, detail=f"no price universe with id {universe_id}") from exc


# Lane UD (U.3b, set 6 day plan section 9's own "what can break" line): the one live counter this
# build's failure mode needs. A universe refused because a run still names it is a silent-failure
# risk (a caller could otherwise assume the delete "just didn't happen" for no visible reason), so
# every such refusal is counted here, module-level, one increment site, the same shape
# DISPLAY_MONITOR_COUNTERS already uses in routes/display.py. The OTHER named signal,
# `price_universes_deleted`, is a durable database FACT (how many price universes are currently
# soft-deleted), not a process-local event count, so it is a query function below instead
# (tracker.py's own query_* convention), never a counter that would reset on every restart.
PRICE_UNIVERSE_DELETE_MONITOR_COUNTERS: dict[str, int] = {"universe_delete_refused_in_use": 0}


def query_price_universes_deleted() -> int:
    """Ops monitor `price_universes_deleted`: how many price_universe rows currently carry
    status='deleted', a plain COUNT over the same table DELETE /price-universes/{id} writes.
    REPORT-IN shape (a database fact, read on demand), not a counter, because a soft-delete's own
    row survives a restart and this signal must too."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM research_datasets WHERE dataset_type=? AND status='deleted';",
            (researchdb.DATASET_TYPE_PRICE_UNIVERSE,),
        ).fetchone()
        return int(row[0])


@browser_router.delete("/price-universes/{universe_id}")
async def delete_price_universe_route(universe_id: int):
    """Soft-delete a price universe (lane UD, U.3b, set 6 day plan section 9). Inline SQL in the
    route, the same shape delete_dataset (above) already uses, plus one added refusal this route
    needs and delete_dataset does not: a universe still named by a run's own dataset_id is never
    deleted out from under that run, refused by NAME so the caller knows which universe and by the
    exact run ids so they know which runs to resolve first.

    404  no price_universe row at this id (an unknown id, or an id naming a different dataset_type
         -- both read the same so this route never leaks which kind of row it found)
    409  currently fetching, or still referenced by one or more research_runs
    200  {ok: True} on a real soft-delete, and again, idempotently, on a repeat call
    """
    with researchdb.transaction() as conn:  # one transaction: the precondition reads and the UPDATE stay consistent
        row = conn.execute(
            "SELECT status, dataset_type, name FROM research_datasets WHERE id=?;",
            (universe_id,),
        ).fetchone()
        if row is None or row["dataset_type"] != researchdb.DATASET_TYPE_PRICE_UNIVERSE:
            raise HTTPException(404, f"no price universe with id {universe_id}")
        current = row["status"]
        if current == "fetching":
            # Mirrors delete_dataset's own in-flight refusal above, worded for a universe.
            raise HTTPException(409, "cannot delete universe while fetching")
        if current == "deleted":
            # Idempotent: already in the target state. No UPDATE, no broadcast, same as
            # delete_dataset's own no-op branch.
            return {"ok": True}
        # The exact count query request_hard_delete already counts referencing runs with
        # (researchdb.py's own request_hard_delete: "SELECT COUNT(*) FROM research_runs WHERE
        # dataset_id = ?"), so this refusal agrees with the hard-delete path's own notion of "in
        # use". A second, LIMIT-ed query names up to ten of those runs for the caller; the count
        # above always carries the TRUE total even when more than ten runs reference the universe.
        n_runs = conn.execute(
            "SELECT COUNT(*) FROM research_runs WHERE dataset_id=?;",
            (universe_id,),
        ).fetchone()[0]
        if n_runs > 0:
            run_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM research_runs WHERE dataset_id=? ORDER BY id LIMIT 10;",
                    (universe_id,),
                ).fetchall()
            ]
            # The one increment site for PRICE_UNIVERSE_DELETE_MONITOR_COUNTERS: every in-use
            # refusal this route ever returns, before the HTTPException is raised.
            PRICE_UNIVERSE_DELETE_MONITOR_COUNTERS["universe_delete_refused_in_use"] += 1
            raise HTTPException(
                409,
                detail=f"refused: universe {row['name']} is used by {n_runs} run(s): "
                       f"{', '.join(str(i) for i in run_ids)}",
            )
        conn.execute(
            "UPDATE research_datasets SET status='deleted' WHERE id=?;",
            (universe_id,),
        )

    # Broadcast only on the real transition (ready -> deleted), mirroring delete_dataset above.
    await broadcaster.publish("research.dataset_status_changed", {
        "id": universe_id, "status": "deleted",
    })
    return {"ok": True}


@browser_router.post("/price-universes/{universe_id}/dispatch", status_code=201)
async def dispatch_price_universe_sweep_route(universe_id: int, body: PriceUniverseDispatchIn):
    """Dispatch a universe sweep over a saved price universe (M5 U2, AD-M5-13). Two-phase: the call registers
    the sweep + the run atomically and resolves/queues each member's eodhd dataset; the worker runs the sweep
    once every member is 'ready' (synchronously here on the all-already-ready fast path, else via the
    ready-trigger). Returns the run id + the registered combination count. Mirrors _dispatch_run_backtest's
    exception->HTTP mapping. The specific ValueError subclasses (empty universe / scope mismatch) are caught
    BEFORE the bare ValueError (malformed grid / scope), since they subclass it."""
    def _dispatch():
        # R-G25 (generator unit 1): re-verify a generated strategy against its own
        # generation record BEFORE the sweep is registered. A strategy with no record at
        # all is hand-authored and passes through unchanged; this call is the one place
        # requirements section 4's "at dispatch" phrase can mean without touching an
        # existing researchdb.py function (decision sheet lean 6).
        generated.assert_generation_record_matches(body.strategy_id)
        # Lane DV (set 16b, `#424`, AC-4 and AC-5): both pre-dispatch checks run BEFORE any
        # draw is ever requested, unconditionally (they read body.param_grid / universe_id, not
        # strat_row, so they fire whether or not the strategy still exists -- consistent with the
        # generation-record check just above, which already runs unconditionally too).
        _refuse_bad_quantiles_in_grid(body.param_grid)
        _refuse_insufficient_sweep_lead_in(universe_id)
        # Lane RM (set 16b, plan amendment 7, AC-4b/AC-4d): holding_cap is grid-generic, so it
        # runs unconditionally like the two DV checks just above; the no-surviving-combination
        # check needs the strategy's own declared sweep_conditions, so it reads strat_row a line
        # early (the chronology gate's own pre-existing read, moved up rather than duplicated).
        _refuse_non_positive_holding_cap(body.param_grid)
        # Lane T (night two, data-use tracker, R-T3/AD7): the chronology gate, one of the
        # two named call sites (section 6.1). Its own transaction must commit BEFORE
        # dispatch_sweep is ever called (dispatch_sweep cannot be nested inside another
        # open transaction, AD7's G2-B2), so the candidate's CURRENT version is resolved
        # here, independently, rather than borrowed from dispatch_sweep's own later read.
        # A strategy that no longer exists skips the gate and falls through to
        # dispatch_sweep's own StrategyNotFound, leaving that 404 unchanged.
        strat_row = researchdb.get_strategy(body.strategy_id)
        _refuse_no_surviving_combination(strat_row, body.param_grid)
        # Lane RS1: every refusal dispatch_sweep would raise before its first write is raised here,
        # before request_draw, so a refused sweep leaves research_data_draws unchanged.
        researchdb.check_sweep(body.strategy_id, universe_id, body.param_grid)
        if strat_row is None:
            return researchdb.dispatch_sweep(
                body.strategy_id, universe_id, body.param_grid, name=body.name,
            )
        draw_id = tracker.request_draw(
            body.strategy_id, strat_row["version"], body.calendar_id, body.stage,
            universe_id, int(time.time()),
        )
        # selection_scope is no longer a request field (F3): dispatch_sweep defaults it to 'in_isolation'.
        result = researchdb.dispatch_sweep(
            body.strategy_id, universe_id, body.param_grid, name=body.name,
        )
        # Transaction 3 (AD7.3): an appended run_linked event, now that the run this draw
        # was granted for actually exists. Any exception here (tracker.RunLinkFailed) means
        # the sweep itself DID dispatch successfully; the caller must still learn that the
        # link failed, so it is allowed to propagate rather than being swallowed.
        tracker.link_run(draw_id, result["run_id"], int(time.time()))
        return result

    try:
        d = await asyncio.to_thread(_dispatch)
    except generated.GeneratedSourceDrift as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except QuantileOutOfRange as exc:  # AC-4: an out-of-(0,1) entry_quantile/exit_quantile grid value
        raise HTTPException(409, detail=str(exc)) from exc
    except NonPositiveHoldingCap as exc:  # lane RM AC-4(d): a holding_cap that is zero or less
        raise HTTPException(409, detail=str(exc)) from exc
    except NoSurvivingCombination as exc:  # lane RM AC-4(b): no grid combination survives the strategy's own declared conditions
        raise HTTPException(409, detail=str(exc)) from exc
    except stage_runner.LeadInUnavailable as exc:  # AC-5: a member's own bar count is short of the fixed lead-in
        raise HTTPException(409, detail=str(exc)) from exc
    except _TRACKER_REFUSALS as exc:
        _handle_tracker_refusal(exc)
    except researchdb.StrategyNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except researchdb.StrategyDeleted as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except researchdb.PriceUniverseEmptyError as exc:
        raise HTTPException(422, f"price_universe_empty: {exc}") from exc
    except researchdb.FrozenSweepParameterError as exc:  # night-six Lane C, DECISIONS #304: a frozen-tier grid key (group B, C, E or F, or an entry with no readable Group) -> a readable 422 (before any row written)
        raise HTTPException(422, f"frozen_sweep_parameter: {exc}") from exc
    except researchdb.UndeclaredSweepParameterError as exc:  # night four allow-list unit, DECISIONS #324: a grid key that is neither provenance-tiered nor a declared strategy knob -> a readable 422, key and declared names in detail (before any row written)
        raise HTTPException(422, f"undeclared_sweep_parameter: {exc}") from exc
    except researchdb.SweepParameterMissing as exc:  # lane RS1: a declared parameter is absent from the grid -> a readable 422 (before any row written)
        raise HTTPException(422, f"sweep_parameter_missing: {exc}") from exc
    except researchdb.SweepGridTooLarge as exc:  # B3.2: combinations x members over the cap -> a readable 422 (before any row written)
        raise HTTPException(422, f"sweep_grid_too_large: {exc}") from exc
    except (researchdb.UnknownAssetClass, researchdb.AssetClassScopeMismatch) as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:  # malformed param_grid, a grid sent for a zero-param strategy, or a bad scope (fail loud)
        raise HTTPException(400, str(exc)) from exc

    await broadcaster.publish("research.run_created", {
        "run_id": d["run_id"], "job_id": d["job_id"], "universe_id": universe_id,
    })
    return {
        "id": d["job_id"], "run_id": d["run_id"], "status": "pending",
        "registered_n": d["registered_n"], "member_count": d["member_count"],
        "all_members_ready": d["all_members_ready"], "finalized": d["finalized"],
    }


class StageDispatchIn(BaseModel):
    """Lane SR (6.2a): the stage-chain dispatch body. Mirrors PriceUniverseDispatchIn's own
    stage/calendar_id fields (section 8's own reuse), but requires BOTH explicitly, with no
    default: a caller reaching this endpoint is choosing to run one stage of the dry-run chain,
    and a forgotten field silently defaulting to the real calendar is exactly the mistake AC-3
    exists to prevent."""
    strategy_id: int
    universe_id: int
    stage: Literal["screen", "optimise", "walkforward", "reserve"]
    calendar_id: str = Field(..., min_length=1)
    param_grid: dict[str, list]                        # {} for a zero-parameter strategy, same contract as PriceUniverseDispatchIn
    pinned_version: Optional[int] = None                # AC-11: the caller's own pin, carried across every stage of one chain


@browser_router.post("/stage-runner/dispatch", status_code=201)
async def dispatch_stage_route(body: StageDispatchIn):
    """Lane SR (6.2a): dispatch one stage of the dry-run chain over an already-resolved
    price-universe row (build_screen_universe, or any other caller, creates that row first).
    Thin over stage_runner.dispatch_stage, mirroring dispatch_price_universe_sweep_route's own
    exception -> HTTP mapping (section 5.1's reuse-not-reimplement instruction). Registered on
    the EXISTING browser_router (SR-18): no new router, and dashboard/backend/main.py is
    untouched by this lane."""
    def _dispatch():
        # Lane DV (set 16b, `#424`, AC-4): the stage-runner route's own quantile pre-check, run
        # before stage_runner.dispatch_stage ever requests a draw. AC-5's own sweep-lead-in check
        # does NOT repeat here: stage_runner.dispatch_stage already runs its own equivalent
        # (_check_warmups, AC-13/AC-14, existing before this lane), so adding a second copy here
        # would only duplicate a check this route already has.
        _refuse_bad_quantiles_in_grid(body.param_grid)
        # Lane RM (set 16b, plan amendment 7, AC-4b/AC-4d): holding_cap is grid-generic, so it
        # runs unconditionally like the quantile check above. The no-surviving-combination check
        # needs the strategy's own declared sweep_conditions, so it reads strat_row itself here
        # (a plain, lockless read, the SAME one dispatch_price_universe_sweep_route's own
        # chronology gate already takes for a different reason; stage_runner.dispatch_stage takes
        # its own second read further in, and the two are never nested in one transaction so a
        # second plain SELECT here costs nothing beyond one extra query).
        _refuse_non_positive_holding_cap(body.param_grid)
        _refuse_no_surviving_combination(researchdb.get_strategy(body.strategy_id), body.param_grid)
        return stage_runner.dispatch_stage(
            body.strategy_id, body.universe_id, body.stage, body.calendar_id,
            body.param_grid, int(time.time()), pinned_version=body.pinned_version,
        )

    try:
        d = await asyncio.to_thread(_dispatch)
    except QuantileOutOfRange as exc:  # AC-4: an out-of-(0,1) entry_quantile/exit_quantile grid value
        raise HTTPException(409, detail=str(exc)) from exc
    except NonPositiveHoldingCap as exc:  # lane RM AC-4(d): a holding_cap that is zero or less
        raise HTTPException(409, detail=str(exc)) from exc
    except NoSurvivingCombination as exc:  # lane RM AC-4(b): no grid combination survives the strategy's own declared conditions
        raise HTTPException(409, detail=str(exc)) from exc
    except stage_runner.UnknownStageWord as exc:  # a caller-contract violation: an input the four-word vocabulary rejects
        raise HTTPException(422, detail=str(exc)) from exc
    except stage_runner.CandidateVersionMoved as exc:  # AC-11: the pinned candidate has moved since the caller pinned it
        raise HTTPException(409, detail=str(exc)) from exc
    except stage_runner.DailyStoreNotConfigured as exc:  # SR-17: this environment has no daily store named
        raise HTTPException(503, detail=str(exc)) from exc
    except stage_runner.LeadInUnavailable as exc:  # AC-13: the fixed sizing lead-in cannot be satisfied from the store
        raise HTTPException(409, detail=str(exc)) from exc
    except stage_runner.WarmupUnsatisfied as exc:  # AC-14: the strategy's own baked quantile floor cannot be satisfied
        raise HTTPException(409, detail=str(exc)) from exc
    # Lane RS1: three refusals read from the strategy's source text or a blank setting, which had no
    # arm and answered a bare 500. 409 for source-text refusals as WarmupUnsatisfied, 503 for a blank
    # setting as DailyStoreNotConfigured; the detail is the exception's own words.
    except stage_runner.LookbackNotDeclared as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except stage_runner.AmbiguousBakedQuantileConstant as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except researchdb.WalkforwardParameterAbsent as exc:
        raise HTTPException(503, detail=str(exc)) from exc
    # Lane RM (set 16b, plan amendment 7, root cause 2, docs/units/RM/reads/probe_report_2026-09-25_1.md):
    # WindowTooSmallForScheme is a RuntimeError, so with no named arm of its own it fell through
    # every except clause below (including the bare ValueError one, which it is not a subclass of)
    # to FastAPI's default 500. The check itself already fires correctly, before any draw is
    # requested; only its own route never translated the refusal into a plain word.
    except stage_runner.WindowTooSmallForScheme as exc:  # AC-36/WF-3: the granted window is short of the walkforward scheme's own bar floor
        raise HTTPException(409, detail=str(exc)) from exc
    except _TRACKER_REFUSALS as exc:
        _handle_tracker_refusal(exc)
    except researchdb.StrategyNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except researchdb.StrategyDeleted as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except researchdb.PriceUniverseNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except researchdb.PriceUniverseEmptyError as exc:
        raise HTTPException(422, f"price_universe_empty: {exc}") from exc
    except researchdb.FrozenSweepParameterError as exc:  # a frozen-tier grid key (DECISIONS #304), same mapping as the sweep route
        raise HTTPException(422, f"frozen_sweep_parameter: {exc}") from exc
    except researchdb.UndeclaredSweepParameterError as exc:  # a grid key neither tiered nor declared (DECISIONS #324), same mapping
        raise HTTPException(422, f"undeclared_sweep_parameter: {exc}") from exc
    except researchdb.SweepParameterMissing as exc:  # lane RS1: a declared parameter is absent from the grid, same mapping
        raise HTTPException(422, f"sweep_parameter_missing: {exc}") from exc
    except researchdb.SweepGridTooLarge as exc:  # combinations x members over the cap, before any row is written
        raise HTTPException(422, f"sweep_grid_too_large: {exc}") from exc
    except (researchdb.UnknownAssetClass, researchdb.AssetClassScopeMismatch) as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:  # malformed param_grid, a grid sent for a zero-param strategy, or a bad scope (fail loud)
        raise HTTPException(400, str(exc)) from exc

    await broadcaster.publish("research.run_created", {
        "run_id": d["run_id"], "job_id": d["job_id"], "universe_id": body.universe_id,
    })
    return {
        "id": d["job_id"], "run_id": d["run_id"], "status": "pending",
        "registered_n": d["registered_n"], "member_count": d["member_count"],
        "all_members_ready": d["all_members_ready"], "finalized": d["finalized"],
        "warmup": d["warmup"],
    }


@browser_router.post("/event-batches/{batch_id}/reparse")
async def reparse_event_batch(batch_id: int, body: BulkReparseIn):
    """Re-parse a batch's active 13D events from operator-staged <accession>.txt files (D4).
    Wraps events.bulk_reparse.bulk_reparse. Two honest constraints carried verbatim from the
    engine: (a) only event_type='sec_13d' is handled, other types are silently skipped; (b) the
    operator must pre-stage the .txt files in reparse_dir (a separate fetch workflow, out of
    scope here) — a reparse_dir that is not a directory → 400. apply=false is the preview."""
    def _run():
        return event_bulk_reparse.bulk_reparse(
            batch_id=batch_id,
            reparse_dir=body.reparse_dir,
            apply=body.apply,
            max_events=body.max_events,
        )

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as exc:
        # reparse_dir is not a directory (mirrors cmd_bulk_reparse's exit-2 mapping).
        raise HTTPException(400, f"reparse_dir_invalid: {exc}")
    return asdict(result)


def _snake_case(camel: str) -> str:
    """Convert a CamelCase class name to snake_case for the §6.1 500 error body."""
    out: list[str] = []
    for i, ch in enumerate(camel):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


# M2.5 brief J — §6.3 / §6.4 surface support.
# Column list returned by the §6.3 detail endpoint and the §6.4 list endpoint.
# quality_report is intentionally omitted per spec brief §6.3 (keeps the
# payload small; clients fetch /quality for the full report). The constant
# is module-level so the detail handler and the list helper share one source.
_BATCH_COLUMNS_NO_QR = (
    "batch_id, request_id, kind, parent_batch_id, source, policy, "
    "realized_start_at, realized_end_at, source_descriptor, content_hash, "
    "started_at, finished_at, last_heartbeat_at, status, "
    "quarantine_count, event_count, failure_class, failure_context"
)


def _build_list_batches_where(
    source: Optional[str],
    status: Optional[str],
    since: Optional[int],
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause shared between the list SELECT and COUNT.

    Returns (where_sql, params). where_sql includes the literal "WHERE"
    keyword when at least one filter is set, else is the empty string.
    Filters are by-equality (source, status) or `started_at >= since`;
    unknown source/status values still parse to a syntactically valid
    SQL with zero matches (permissive design fork locked by the probe).
    """
    parts: list[str] = []
    params: list[Any] = []
    if source is not None:
        parts.append("source = ?")
        params.append(source)
    if status is not None:
        parts.append("status = ?")
        params.append(status)
    if since is not None:
        parts.append("started_at >= ?")
        params.append(since)
    where_sql = ("WHERE " + " AND ".join(parts)) if parts else ""
    return where_sql, params


def _build_list_batches_sql(
    source: Optional[str],
    status: Optional[str],
    since: Optional[int],
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    """Return (sql, params) for the §6.4 list SELECT — the single source of
    truth for the list-endpoint SQL.

    Locked by the brief J probe spec (V7): the route handler MUST call this
    helper and pass the tuple straight to conn.execute(sql, params); no
    duplicate SQL assembly inside the route. The probe imports this helper
    directly and runs EXPLAIN QUERY PLAN against the returned tuple to
    assert the ix_batches_source_started access path on the source-keyed
    shape and the absence of that index on the source-less shape.

    Sort: (started_at DESC, batch_id DESC). The batch_id tiebreaker makes
    paginated slices stable across re-runs when multiple batches land in
    the same epoch-second (realistic under brief D's per-quarter cadence).
    """
    where_sql, params = _build_list_batches_where(source, status, since)
    sql = (
        f"SELECT {_BATCH_COLUMNS_NO_QR} "
        f"FROM research_event_batches "
        f"{where_sql} "
        f"ORDER BY started_at DESC, batch_id DESC "
        f"LIMIT ? OFFSET ?"
    )
    return sql, [*params, limit, offset]


# W7 "Fetch all" plan (decision D7-2 + the locked fail-closed cap). The earliest SEC quarterly
# full-index is 1994 Q3 (events/clients/edgar.py:281-282). _FETCH_ALL_MAX_FILINGS is the single
# backend constant matching the event_counts.sec_13d_backfill_cap provenance entry; the dashboard
# never sends an uncapped run. Declared BEFORE GET /event-batches/{batch_id} so the static
# 'fetch-all-plan' segment is not swallowed by the int path param.
_FETCH_ALL_EARLIEST_QUARTER = "1994Q3"
_FETCH_ALL_MAX_FILINGS = 1_000_000


@browser_router.get("/event-batches/fetch-all-plan")
async def edgar_fetch_all_plan():
    """W7: the server-authoritative "Fetch all" plan for SEC 13D — the quarter range from the
    earliest SEC index to the current quarter, plus the locked fail-closed cap. The UI posts this
    verbatim to POST /event-batches/backfill (max_filings stays mandatory ge=1, so the dashboard
    cannot start an unbounded run). The current quarter is computed server-side so the UI and the
    fetcher agree on the calendar boundary.
    """
    g = time.gmtime()
    quarter = (g.tm_mon - 1) // 3 + 1                            # 1..4 from the UTC month
    current = f"{g.tm_year}Q{quarter}"
    return {
        "quarter_range": f"{_FETCH_ALL_EARLIEST_QUARTER}-{current}",
        "max_filings": _FETCH_ALL_MAX_FILINGS,
        "current_quarter": current,
    }


@browser_router.get("/event-batches/{batch_id}")
async def get_event_batch(batch_id: int):
    """Return one batch's row (spec brief §6.3, owner auth at Caddy edge).

    Returns every research_event_batches column EXCEPT quality_report
    (use §6.5 /quality for the full report; keeps this payload small).

    Status mapping:
      200 — JSON object with the column set above
      404 — {"error": "batch_not_found", "batch_id": <int>}
    """
    def _fetch():
        with eventsdb.get_events_conn() as conn:
            row = conn.execute(
                f"SELECT {_BATCH_COLUMNS_NO_QR} "
                f"FROM research_event_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": "batch_not_found", "batch_id": batch_id},
        )
    return row


@browser_router.get("/event-batches")
async def list_event_batches(
    source: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Operational batch list (spec brief §6.4, owner auth at Caddy edge).

    Pagination + index-backed sort. Filters are by-equality (source,
    status) or `started_at >= since`. Unknown enum values return 200 with
    an empty `batches` list (permissive on unknown values — locked by the
    probe to avoid coupling route validation to the §3.4 CHECK enum,
    which can grow without the route changing).

    Response:
      {
        "batches": [ §6.3 row shape, ... ],
        "total":   <COUNT(*) over the filter, NOT the page>,
        "next_offset": <offset + limit when more rows remain, else null>
      }
    """
    # Server-side clamp on limit (spec brief §6.4: default 50, max 200).
    # offset clamps to >= 0 for the same paranoia (negative offsets would
    # SQL-parse but yield surprises; the §6.4 contract says default 0).
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    sql, params = _build_list_batches_sql(source, status, since, limit, offset)
    where_sql, count_params = _build_list_batches_where(source, status, since)
    count_sql = f"SELECT COUNT(*) FROM research_event_batches {where_sql}"

    def _fetch():
        with eventsdb.get_events_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            total = conn.execute(count_sql, count_params).fetchone()[0]
        return [dict(r) for r in rows], total

    batches, total = await asyncio.to_thread(_fetch)
    # next_offset is integer when more rows remain after this page, else None.
    # The locked convention (§6.4 example shows integer at offset 50 of 137):
    # integer-when-more, null-at-end. Matches the probe step-4 assertion.
    next_offset: Optional[int] = offset + limit if (offset + limit) < total else None
    return {"batches": batches, "total": total, "next_offset": next_offset}


# ---------------------------------------------------------------------------
# Benzinga analyst-ratings ingestion controls (brief BZ4.3). Per the complete-control-
# surface principle (#58), the backfill + poll are triggerable + observable from the
# dashboard. browser_router => Caddy owner basic-auth at the edge (no per-route dep).
# ---------------------------------------------------------------------------


def _detect_running_benzinga(events_db_path: Optional[Path] = None) -> Optional[int]:
    """Return the batch_id of an already-running benzinga batch, or None.

    The route-level 409 mechanism (mirrors `_detect_running_loader`): refuse a new dispatch
    while a `source='benzinga'`, `status='running'` batch exists. The fetcher unit's flock is
    the lower-level backstop, NOT this check.
    """
    with eventsdb.get_events_conn(events_db_path) as conn:
        row = conn.execute(
            """SELECT batch_id FROM research_event_batches
                WHERE source='benzinga' AND status='running'
                LIMIT 1"""
        ).fetchone()
    return int(row["batch_id"]) if row is not None else None


_NO_BLOCK_UNIT_RE = re.compile(r"^newsbot-(benzinga|edgar|fundamentals)-backfill@[^@/\s]+\.service$")


def _dispatch_systemctl(unit: str) -> None:
    """Start a systemd unit via the scoped sudoers carve-out; raise RuntimeError on failure.

    Module-level so the route probe can monkeypatch it (no real systemctl in the probe). The
    command is a fixed argv list (no shell), and `unit` is built from a validated value, so
    there is no injection surface.

    The three backfill units (benzinga, edgar, fundamentals) are oneshot jobs that run for
    minutes; a plain `systemctl start` waits for them and hit the 30 s timeout while the job ran
    fine, so the route answered 500. They are started with `--no-block`: systemctl returns as soon
    as the job is queued. Any other unit (the Benzinga poll) keeps the plain form and waits.
    `sudo -n` means a missing sudoers rule fails at once with a message instead of waiting for a
    password prompt that nobody can answer.
    """
    if _NO_BLOCK_UNIT_RE.match(unit):
        argv = ["sudo", "-n", "systemctl", "start", "--no-block", unit]
    else:
        argv = ["sudo", "-n", "systemctl", "start", unit]
    cmd = " ".join(argv[1:])
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{cmd} did not answer within 30 s") from exc
    except OSError as exc:
        raise RuntimeError(f"{cmd} could not run: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd} failed (rc={result.returncode}): {result.stderr.strip()}. "
            "If this says a password is required, the sudoers rule for this exact command is "
            "not installed on the server (dashboard/deploy/*/newsbot-*.sudoers)"
        )


class BenzingaBackfillIn(BaseModel):
    """POST /benzinga/backfill body. max_lookback_months is the integer %i for the templated unit."""
    max_lookback_months: int = Field(..., ge=1, le=60)


@browser_router.post("/benzinga/backfill", status_code=202)
async def benzinga_backfill(body: BenzingaBackfillIn):
    """Dispatch a Benzinga historical backfill (BZ4.3). 202 on dispatch; 409 if one is running.

    The integer max_lookback_months becomes the systemd template instance
    `newsbot-benzinga-backfill@<n>.service`, started via the scoped sudoers carve-out.
    """
    running = await asyncio.to_thread(_detect_running_benzinga)
    if running is not None:
        raise HTTPException(status_code=409, detail="backfill_already_running")
    unit = f"newsbot-benzinga-backfill@{body.max_lookback_months}.service"
    try:
        await asyncio.to_thread(_dispatch_systemctl, unit)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"dispatch_failed: {exc}") from exc
    return {"dispatched": True, "max_lookback_months": body.max_lookback_months, "unit": unit}


@browser_router.post("/benzinga/poll", status_code=202)
async def benzinga_poll():
    """Dispatch a one-shot Benzinga poll (BZ4.3). 202 on dispatch; 409 if a benzinga batch runs."""
    running = await asyncio.to_thread(_detect_running_benzinga)
    if running is not None:
        raise HTTPException(status_code=409, detail="poll_already_running")
    try:
        await asyncio.to_thread(_dispatch_systemctl, "newsbot-benzinga-poll.service")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"dispatch_failed: {exc}") from exc
    return {"dispatched": True}


@browser_router.get("/benzinga/status")
async def benzinga_status():
    """The most recent Benzinga batches' summary (BZ4.3) — what the dashboard shows the operator."""
    def _fetch():
        with eventsdb.get_events_conn() as conn:
            rows = conn.execute(
                """SELECT batch_id, kind, status, started_at, finished_at,
                          event_count, quarantine_count
                     FROM research_event_batches
                    WHERE source='benzinga'
                    ORDER BY batch_id DESC LIMIT 10"""
            ).fetchall()
            return [dict(r) for r in rows]
    batches = await asyncio.to_thread(_fetch)
    return {"source": "benzinga", "recent_batches": batches}


# ---------------------------------------------------------------------------
# Dashboard-triggered EDGAR 13D backfill (Brief A2 / D3, spec §A2.2). Server-side
# dispatch via the existing supervised systemd unit — reuses _dispatch_systemctl +
# the _detect_running pattern proven in prod by the Benzinga ingestion (#76). The cap
# is MANDATORY and the fetcher fails CLOSED, so a dashboard trigger is always bounded.
# ---------------------------------------------------------------------------

# Mirror of the fetcher's quarter-range guard (`fetch-edgar-13d.py` _QUARTER_RANGE_RE); the
# script can't be imported (hyphenated filename). The route uses it for a fast 400; the fetcher
# re-validates server-side as the security authority the unit's no-injection comment relies on.
_QUARTER_RANGE_RE = re.compile(r"^[0-9]{4}Q[1-4]-[0-9]{4}Q[1-4]$")


def _detect_running_edgar(events_db_path: Optional[Path] = None) -> Optional[int]:
    """Return the batch_id of an already-running EDGAR backfill batch, or None.

    Mirrors `_detect_running_benzinga`: refuse a new dispatch while a `source='sec_edgar'`,
    `status='running'` batch exists (the route-level 409). The fetcher unit's flock is the
    lower-level backstop, NOT this check.
    """
    with eventsdb.get_events_conn(events_db_path) as conn:
        row = conn.execute(
            """SELECT batch_id FROM research_event_batches
                WHERE source='sec_edgar' AND status='running'
                LIMIT 1"""
        ).fetchone()
    return int(row["batch_id"]) if row is not None else None


class EdgarBackfillIn(BaseModel):
    """POST /event-batches/backfill body (A2 §A2.2). The cap is MANDATORY (H10): a dashboard
    backfill always carries one, and the fetcher fails CLOSED without it, so no request shape can
    start an unbounded backfill. quarter_range is the systemd template instance, e.g. 2024Q1-2024Q1."""
    quarter_range: str = Field(..., description="single-token quarter range, e.g. 2024Q1-2024Q1")
    max_filings: int = Field(..., ge=1, description="filing cap, required (H10); the fetcher fails closed without it")


@browser_router.post("/event-batches/backfill", status_code=202)
async def edgar_backfill(body: EdgarBackfillIn):
    """Dispatch a dashboard-triggered EDGAR 13D backfill (D3 / §A2.2), ALWAYS capped.

    The cap (max_filings, required, ge=1) is written to a per-instance env file the templated unit
    loads (<EDGAR_BACKFILL_JOB_DIR>/backfill-<range>.env -> EDGAR_MAX_FILINGS), then the unit is
    started via the scoped sudoers carve-out. The fetcher fails CLOSED if the cap signal is absent
    (the Cowork amendment), so a propagation bug refuses rather than runs unbounded. Uncapped is
    reachable only by the operator's explicit --max-filings=0 / EDGAR_MAX_FILINGS=0 (the systemd-unit
    operator path), never from this route. 202 on dispatch; 409 if a backfill is running; 400 on a
    malformed quarter range.
    """
    if not _QUARTER_RANGE_RE.match(body.quarter_range):
        raise HTTPException(status_code=400, detail="invalid_quarter_range")
    running = await asyncio.to_thread(_detect_running_edgar)
    if running is not None:
        raise HTTPException(status_code=409, detail="backfill_already_running")

    def _write_cap() -> None:
        # Write the per-instance cap file BEFORE dispatch so the unit's EnvironmentFile=- loads it.
        # EDGAR_BACKFILL_JOB_DIR must match the unit's EnvironmentFile= path (set in the server .env);
        # newsbot owns both, so the write and the unit read agree. A mismatch is safe: the unit finds
        # no file and the fetcher fails closed (refuses), never an uncapped run.
        EDGAR_BACKFILL_JOB_DIR.mkdir(parents=True, exist_ok=True)
        env_file = EDGAR_BACKFILL_JOB_DIR / f"backfill-{body.quarter_range}.env"
        env_file.write_text(f"EDGAR_MAX_FILINGS={body.max_filings}\n", encoding="utf-8")

    # Two distinct failure surfaces, each surfaced with its own operator-facing detail: the cap-file
    # write (OSError — permissions / disk full; raised before any dispatch, so nothing was started)
    # and the systemctl dispatch (RuntimeError — _dispatch_systemctl on a non-zero systemctl return).
    try:
        await asyncio.to_thread(_write_cap)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cap_file_write_failed: {exc}") from exc
    unit = f"newsbot-edgar-backfill@{body.quarter_range}.service"
    try:
        await asyncio.to_thread(_dispatch_systemctl, unit)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"dispatch_failed: {exc}") from exc
    return {"dispatched": True, "quarter_range": body.quarter_range, "max_filings": body.max_filings, "unit": unit}


@browser_router.post("/event-batches/reap-orphaned")
async def reap_orphaned_batches():
    """Clear a stuck `running` batch (item 5a / Cowork S3): one orphaned by PROCESS DEATH —
    a deploy SIGTERM, OOM, or crash before the fetcher's except handler could close it — which
    otherwise 409s every future dispatch ("backfill already running").

    This reuses the existing startup recovery path, not a new bespoke flip: the heartbeat reaper
    (reap_orphaned_running_batches) only touches a `running` batch whose last heartbeat is older
    than the stale threshold (300s) via a CAS UPDATE, so a genuinely-live backfill (recent
    heartbeat) is NEVER cleared — it is safe to invoke while a real run is in flight. Then the
    secondary sweep rolls back any active events stranded behind a now-failed batch. Both are
    idempotent: with nothing stuck, this is a no-op returning empty lists. Synchronous (a couple of
    indexed writes) via to_thread.
    """
    def _reap() -> dict:
        reap = event_sweeps.reap_orphaned_running_batches()          # flip stale-heartbeat running -> failed
        sweep = event_sweeps.secondary_sweep()                       # roll back events stranded behind them
        return {
            "batches_reaped": reap.batches_reaped,                   # ids flipped failed/OrphanedRunningBatch
            "events_rolled_back": reap.events_rolled_back + sweep.events_rolled_back,
            "batches_swept": sweep.batches_swept,                    # already-failed batches whose events were rolled back
        }

    # A locked events.db (a concurrent writer holds BEGIN IMMEDIATE) is transient: surface it as a
    # 503 with detail so the operator knows to retry, not a blank 500 (CQ-SF1). Other sqlite3 errors
    # are real faults and propagate as a 500 (the honest "something is wrong, not just busy" signal).
    try:
        return await asyncio.to_thread(_reap)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"reaper_db_busy: {exc}") from exc


# ---------------------------------------------------------------------------
# Dashboard-triggered SEC XBRL fundamentals backfill (M3.75 FUND-1, spec §3.3). Server-side
# dispatch via the supervised systemd unit `newsbot-fundamentals-backfill@<token>.service` —
# reuses _dispatch_systemctl + the _detect_running pattern proven in prod by EDGAR / Benzinga.
# UNLIKE EDGAR (where the systemd instance %i IS the quarter-range value passed on the command
# line), the fundamentals instance is a freshly minted server TOKEN; the REAL scope (the CIK set)
# is ALWAYS written to the per-instance env file, never onto the command line — the injection-safety
# property (§3.3). A CIK list cannot be a bounded, injection-safe systemd instance the way EDGAR's
# regex-validated quarter-range can.
# ---------------------------------------------------------------------------

# The systemd instance token grammar (§3.3): the route mints `secrets.token_hex(16)` (32 hex chars)
# and re-validates it against this regex before dispatch — defense in depth, exactly as EDGAR
# re-validates `quarter_range` against `_QUARTER_RANGE_RE` (routes/research.py:1630). The sudoers
# carve-out is scoped to `newsbot-fundamentals-backfill@*.service`, so a token that cannot match
# this grammar never reaches systemctl. The 1..32 lower bound is intentionally permissive (the route
# always mints exactly 32, but a shorter hand-supplied token for an operator/probe `systemctl start`
# stays valid); the upper bound + hex-only set are what bound the systemd instance.
_FUNDAMENTALS_TOKEN_RE = re.compile(r"^[0-9a-f]{1,32}$")


def _detect_running_fundamentals(events_db_path: Optional[Path] = None) -> Optional[int]:
    """Return the batch_id of an already-running fundamentals backfill batch, or None.

    Mirrors `_detect_running_edgar` exactly: refuse a new dispatch while a `status='running'`
    fundamentals batch exists (the route-level 409). The fetcher unit's flock is the lower-level
    TOCTOU backstop, NOT this check (the 409 is check-then-act, §3.3).
    """
    with eventsdb.get_events_conn(events_db_path) as conn:
        row = conn.execute(
            """SELECT batch_id FROM research_fundamental_batches
                WHERE status='running'
                LIMIT 1"""
        ).fetchone()
    return int(row["batch_id"]) if row is not None else None


class FundamentalsBackfillIn(BaseModel):
    """POST /fundamentals/backfill body (§3.3). One of `ciks` (an explicit list, written verbatim)
    or `universe_id` (resolved server-side via resolve_universe_ciks over the window) supplies the
    scope; `kind` is the batch kind. A universe-scoped request MUST carry the knowledge window the
    membership read needs (resolve_universe_ciks is a half-open `[from, to)` read, universe.py:384);
    supplying neither scope, or both, or a universe with no window, is a 422 (validated below)."""
    ciks: Optional[list[int]] = Field(default=None, description="an explicit CIK list, written verbatim to the scope file")
    universe_id: Optional[int] = Field(default=None, description="resolve the CIK set from this universe's membership over the window")
    knowledge_from: Optional[int] = Field(default=None, description="(universe-scoped) inclusive window lower bound, epoch s")
    knowledge_to: Optional[int] = Field(default=None, description="(universe-scoped) exclusive window upper bound, epoch s")
    kind: Literal["fetch", "refresh"] = Field(..., description="the batch kind; fundamentals are fetch-and-refresh only (§1.4)")


@browser_router.post("/fundamentals/backfill", status_code=202)
async def fundamentals_backfill(body: FundamentalsBackfillIn):
    """Dispatch a dashboard-triggered SEC XBRL fundamentals backfill (§3.3).

    The REAL scope (the resolved CIK set) is written to a per-instance env file the templated unit
    loads (<FUNDAMENTALS_JOB_DIR>/backfill-<token>.env -> FUND_KIND + FUND_CIKS), then the unit
    `newsbot-fundamentals-backfill@<token>.service` is started via the scoped sudoers carve-out. The
    systemd instance is a freshly minted, regex-validated TOKEN — never the CIK list — so the
    wildcard sudoers rule has no injection surface (§3.3). 202 on dispatch; 409 if a fundamentals
    backfill is already running; 422 on a malformed scope (the Pydantic Literal handles a bad kind).
    """
    # 1. Scope validity (§3.3) first — cheap, no DB. Exactly one of {ciks, universe_id}, and a
    #    universe-scoped request needs the knowledge window resolve_universe_ciks reads (the
    #    half-open `[from, to)` membership read, universe.py:384). A bad shape is a named 422, not a
    #    raw Pydantic 422, mirroring the `window_required` 422 the universe-resolve route uses.
    has_ciks = body.ciks is not None
    has_universe = body.universe_id is not None
    if has_ciks == has_universe:                                # neither OR both supplied
        raise HTTPException(status_code=422, detail="scope_required_exactly_one")
    if has_ciks and len(body.ciks) == 0:                        # an explicit empty CIK list is no scope
        raise HTTPException(status_code=422, detail="ciks_empty")
    if has_universe:
        kf, kt = body.knowledge_from, body.knowledge_to        # the membership window (§3.3 / §7)
        if kf is None or kt is None or kf <= 0 or kt <= 0 or kf >= kt:
            raise HTTPException(status_code=422, detail="window_required")

    # 2. The 409 guard: refuse while a fundamentals batch is 'running' (check-then-act; the unit's
    #    flock is the lower backstop, §3.3). Sync SQLite off the event loop (the materialize/EDGAR
    #    precedent).
    running = await asyncio.to_thread(_detect_running_fundamentals)
    if running is not None:
        raise HTTPException(status_code=409, detail="fundamentals_already_running")

    # 3. Resolve the CIK scope. A universe-scoped request reads the membership union over the window
    #    (§7); an explicit list is taken verbatim. Off the event loop (sync SQLite).
    if has_universe:
        ciks = await asyncio.to_thread(
            event_universe.resolve_universe_ciks,
            universe_id=body.universe_id,
            knowledge_from=body.knowledge_from,
            knowledge_to=body.knowledge_to,
        )
        if not ciks:                                           # an empty union is no work — refuse, do not dispatch
            raise HTTPException(status_code=422, detail="universe_empty")
    else:
        ciks = list(body.ciks)                                 # the explicit list, written verbatim (§3.3)

    # 4. Mint the systemd instance token and re-validate it (defense in depth, §3.3): 16 random
    #    bytes -> 32 hex chars, which the strict grammar must accept. A token that somehow failed
    #    the grammar would be a hard internal error (token_hex cannot produce a non-hex string), so
    #    this is a belt-and-suspenders guard, never a request-driven 422.
    token = secrets.token_hex(16)
    if not _FUNDAMENTALS_TOKEN_RE.match(token):
        raise HTTPException(status_code=500, detail="token_mint_failed")

    def _write_scope() -> None:
        # Write the per-instance scope file BEFORE dispatch so the unit's EnvironmentFile=- loads it.
        # FUNDAMENTALS_JOB_DIR must match the unit's EnvironmentFile= path (set in the server .env);
        # newsbot owns both, so the write and the unit read agree. A mismatch is SAFE: the unit finds
        # no scope file and the fetcher fails CLOSED (refuses), never a default-scope run. The dir is
        # 0o700 and the file 0o600 — the CIK set is the cap-file secret, readable only by newsbot.
        FUNDAMENTALS_JOB_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        env_file = FUNDAMENTALS_JOB_DIR / f"backfill-{token}.env"
        # FUND_KIND + FUND_CIKS (comma-separated CIK ints) is the env-var convention the fetcher
        # parses (fetch-fundamentals.py); both sides agree on these names. The CIK list lives ONLY
        # here, never on the command line (the injection-safety property, §3.3).
        body_text = f"FUND_KIND={body.kind}\nFUND_CIKS={','.join(str(c) for c in ciks)}\n"
        env_file.write_text(body_text, encoding="utf-8")
        env_file.chmod(0o600)                                  # tighten in case the umask left it wider

    # 5. Two distinct failure surfaces, each surfaced with its own operator-facing detail, mirroring
    #    edgar_backfill: the scope-file write (OSError — permissions / disk full; raised before any
    #    dispatch, so nothing was started) and the systemctl dispatch (RuntimeError — _dispatch_systemctl
    #    on a non-zero systemctl return). Both map to 500, the same status edgar_backfill uses.
    try:
        await asyncio.to_thread(_write_scope)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"scope_file_write_failed: {exc}") from exc
    unit = f"newsbot-fundamentals-backfill@{token}.service"
    try:
        await asyncio.to_thread(_dispatch_systemctl, unit)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"dispatch_failed: {exc}") from exc
    return {"dispatched": True, "job": token, "kind": body.kind, "cik_count": len(ciks), "unit": unit}


@browser_router.post("/fundamentals/reap-orphaned")
async def reap_orphaned_fundamental_batches():
    """Clear a stuck `running` fundamentals batch (§3.3): one orphaned by PROCESS DEATH — a deploy
    SIGTERM, OOM, or crash before the write path could close it — which otherwise 409s every future
    backfill ("fundamentals already running").

    Reuses the orphan reaper's on-demand path, not a new bespoke flip: the heartbeat reaper
    (_reap_orphaned_fundamental_batches) only touches a `running` batch whose last heartbeat is older
    than the stale threshold (300s) via a CAS UPDATE, so a genuinely-live backfill (recent heartbeat)
    is NEVER cleared — it is safe to invoke while a real run is in flight. SIMPLER than the event
    reaper: NO secondary event-rollback sweep, because fundamentals are append-only vintage-kept (a
    dead batch's already-written facts are valid filings, just incomplete coverage, so they stay,
    §3.3). Idempotent: with nothing stale, this is a no-op returning an empty list. Synchronous (a
    single CAS write) via to_thread.
    """
    # A locked events.db (a concurrent writer holds BEGIN IMMEDIATE) is transient: surface it as a
    # 503 with detail so the operator knows to retry, not a blank 500 — the same narrowing
    # reap_orphaned_batches uses. Other sqlite3 errors are real faults and propagate as a 500.
    try:
        return await asyncio.to_thread(fundamentals_write._reap_orphaned_fundamental_batches)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"reaper_db_busy: {exc}") from exc


@browser_router.get("/fundamentals/definitions")
async def fundamentals_definitions_route():
    """The plain-English definitions of every figure and ratio (ANA-2 §2.2). Static content (no
    per-data computation), so the frontend caches it forever (staleTime: Infinity) rather than
    re-attaching it to every fact/ratio read. Returns {figures: {...}, ratios: {...}}, each keyed
    by the internal name, each entry carrying its human label + definition (+ source for figures,
    + formula for ratios)."""
    return fundamentals_definitions.definitions_payload()


@browser_router.get("/fundamentals/status")
async def fundamentals_status():
    """The most recent fundamentals batches' summary + the store's newest filing date (FUND-4 §10).

    Backs the fundamentals control panel's status line: the recent `research_fundamental_batches`
    rows (counts, the latest's quality_report for the coverage view) and `latest_filing_date` =
    MAX(filing_date) across the fact store (the newest filing the store knows about — a one-number
    coverage horizon for the operator). Mirrors benzinga_status, pointed at the dedicated
    fundamentals lineage table (§1.4), NOT research_event_batches. Read-only; an empty store returns
    an empty list + a null latest_filing_date (an honest empty, never a fake zero — NFR-7).
    Also `per_year`: [{year, count}] ascending, facts by UTC year of filing_date ([] when empty).
    """
    def _fetch():
        with eventsdb.get_events_conn() as conn:
            batches = conn.execute(
                """SELECT batch_id, kind, source, started_at, finished_at, status,
                          cik_count, fact_count, quarantine_count, quality_report,
                          failure_class
                     FROM research_fundamental_batches
                    ORDER BY batch_id DESC LIMIT 10"""
            ).fetchall()
            # The newest filing the store holds (the coverage horizon). MAX over an empty table is
            # one row with a NULL value, so .fetchone() never returns None here.
            latest = conn.execute(
                "SELECT MAX(filing_date) AS m FROM research_fundamental_facts"
            ).fetchone()
            # ANA-3 §3.1: the total facts the store holds, computed AT SERVE TIME (not baked into the
            # batch-time quality_report JSON, which would go stale). Lets the frontend tell "no new
            # figures since the last fetch" (a re-fetch wrote 0 but the store holds N) apart from a
            # genuinely empty store.
            # Scalability note (DDIA Ch-3, the same trap the MAX(filing_date) index comment names):
            # a bare COUNT(*) with no WHERE is a full scan SQLite cannot serve from a B-tree in O(1).
            # This is bounded and acceptable HERE because (a) the status page is an operator surface
            # loaded occasionally, not a hot path, and (b) the store is in the low millions of rows for
            # a long time (8 concepts x 4 periods x ~20 years x the fetched-company set), where a scan
            # is tens of milliseconds. REVISIT with a batch-close-maintained counter (kept serve-time
            # accurate, not the stale quality_report value) if the store reaches tens of millions or
            # this page becomes hot.
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM research_fundamental_facts"
            ).fetchone()
            # Facts per UTC year of filing_date (the Data library's per-year strip); a GROUP BY
            # over ix_fundamental_facts_filing_date. [] on an empty store.
            years = conn.execute(
                "SELECT CAST(strftime('%Y', filing_date, 'unixepoch') AS INTEGER) AS year, "
                "       COUNT(*) AS count FROM research_fundamental_facts "
                "GROUP BY year ORDER BY year"
            ).fetchall()
            per_year = [{"year": y["year"], "count": y["count"]} for y in years]
            return [dict(r) for r in batches], latest["m"], total["c"], per_year
    recent_batches, latest_filing_date, total_facts_on_record, per_year = await asyncio.to_thread(_fetch)
    return {
        "source": "sec_edgar",
        "recent_batches": recent_batches,
        "latest_filing_date": latest_filing_date,  # epoch s UTC, or null when the store is empty
        "total_facts_on_record": total_facts_on_record,  # serve-time COUNT(*), never the stale batch value
        "per_year": per_year,  # [{year, count}] ascending, UTC year of filing_date; [] when empty
    }


@browser_router.get("/fundamentals/poll-status")
async def fundamentals_poll_status():
    """The daily SEC poll for followed companies (lane FA3): the newest pass with its failures, the last
    success, the next due time and whether the poll is late (times are epoch seconds UTC), and the counts of
    companies followed and filings still waiting or missing. Read-only; an empty file answers with no pass,
    late true."""
    return await asyncio.to_thread(fundamentals_poll.poll_status)


@browser_router.get("/fundamentals/facts")
async def fundamentals_facts(cik: int, as_of: int):
    """The point-in-time fact inspector (FUND-4 §10): the as-known-on-D read for each core concept.

    Returns one entry per shipped concept (§2.1) — the as-of value the public record showed for
    `cik` on `as_of` (epoch s UTC), read no-lookahead via read_asof (vintage = as-known-on-D, the
    Q2 default; as-originally-reported / latest-restated are research-only and NOT UI-facing). A
    concept with no fact public by the date carries `found: false` and a null value (an honest
    empty per-concept state, never a fake zero — NFR-7); a company with no facts at all yields every
    entry `found: false`, which the frontend renders as an honest empty inspector. Read-only.
    """
    def _read():
        with eventsdb.get_events_conn() as conn:                    # Row factory set by get_events_conn
            # The shared cell read (ANA-5 §5.2 / ANA-6 §6.1): per-concept as-known-on-D facts + the
            # divide-safe ratios over those same FactValues. The compare route reuses this exact
            # function, so a compare cell is byte-identical to this inspector read by construction.
            return fundamentals_inspect.read_company_asof(conn, cik, as_of)
    cell = await asyncio.to_thread(_read)
    return {"cik": cik, "as_of": as_of, **cell}


@browser_router.get("/fundamentals/history")
async def fundamentals_history(
    cik: int,
    years: int = Query(10, ge=1, le=30),
    ticker: Optional[str] = Query(None, pattern=r"^[A-Z0-9.\-]{1,10}$"),
):
    """Lane LK1: each fundamental concept and ratio by fiscal year for one company, newest year first
    (the ticker lookup window's study). Full fiscal years only, the latest filing per year; see
    reads.read_annual_history. A company never fetched answers on_record false with empty lists.

    Lane LK1c: with `ticker`, each per-share figure is counted in today's shares: divided by every later
    split read from Alpaca (market_display.fetch_alpaca_splits), the filed number kept as `as_filed`.
    `splits` is null without a ticker and [] when nothing per-share needs adjusting (no call made);
    `split_error` is null, or plain words saying why the figures stay as filed (the answer is still 200).
    """
    def _read():
        with eventsdb.get_events_conn() as conn:                    # Row factory set by get_events_conn
            history = fundamentals_reads.read_annual_history(conn, cik, years)
            return {"cik": cik, "on_record": fundamentals_inspect.company_on_record(conn, cik), **history}
    answer = await asyncio.to_thread(_read)
    answer["splits"] = None
    answer["split_error"] = None
    if ticker is None:
        return answer
    filed = [v["filing_date"] for entry in answer["concepts"] if entry["unit"] == "USD/shares"
             for v in entry["values"]]
    if not filed:
        answer["splits"] = []
        return answer
    try:
        key_id, secret_key = display_route._read_credentials()
        splits = await market_display.fetch_alpaca_splits(ticker, min(filed), key_id, secret_key)
    except (display_route.DisplayUpstreamError, market_display.MarketUpstreamError,
            market_display.MarketBudgetExhausted) as exc:
        answer["split_error"] = str(exc)                            # plain words, never a key
        return answer
    answer["splits"] = [{"ex_date": s["ex_date"], "ratio": s["ratio"]} for s in splits]
    answer["concepts"] = market_display.per_share_in_todays_shares(answer["concepts"], splits)
    return answer


class FundamentalsCompareIn(BaseModel):
    """POST /fundamentals/compare body (ANA-6 §6.1). A bounded grid: every (cik, as_of) cell is the
    same no-lookahead inspector read. The company-AND-date PRODUCT is capped (reject-don't-truncate),
    so a too-large grid is a 422 `compare_too_large`, never a silently-incomplete answer."""
    ciks: list[int] = Field(..., description="the companies to compare (SEC CIKs); per-axis capped")
    as_ofs: list[int] = Field(..., description="the as-of dates (epoch s UTC); per-axis capped")


@browser_router.post("/fundamentals/compare")
async def fundamentals_compare(body: FundamentalsCompareIn):
    """The bounded fundamentals compare grid (ANA-6 §6.1): {facts, ratios} per (cik, as_of) cell.

    Each cell is the SAME no-lookahead read the inspector produces (fundamentals_inspect reuses
    read_company_asof), so a compare cell is byte-identical to the single-company inspector read.
    The company-and-date PRODUCT is bounded (not companies alone — the SF1 correctness fix): an
    over-cap request is a 422 `compare_too_large` (reject-don't-truncate), so the answer is never
    silently incomplete. Read-only. Returns `{cells: [{cik, as_of, facts, ratios}, ...]}`.
    """
    # The cap check first (cheap, no DB): a plain over-cap reason -> 422 compare_too_large. The
    # bound logic lives in the loader module (laptop-testable without fastapi, the §8.5 gotcha).
    bound_error = fundamentals_inspect.compare_bound_error(body.ciks, body.as_ofs)
    if bound_error is not None:
        raise HTTPException(status_code=422, detail={"code": "compare_too_large", "message": bound_error})

    def _read():
        with eventsdb.get_events_conn() as conn:                    # Row factory set by get_events_conn
            return fundamentals_inspect.read_compare(conn, body.ciks, body.as_ofs)
    cells = await asyncio.to_thread(_read)
    return {"cells": cells}


class CompareRunsIn(BaseModel):
    """POST /runs/compare body (Unit-4 D1). The run ids to overlay, capped at 6 and rejected (not
    truncated) over-cap, so the answer is never silently incomplete (the ANA-6 house pattern)."""
    run_ids: list[int] = Field(..., description="the runs to compare (run ids), in display order; capped at 6")


# Unit-4 D1: a presentation + payload bound, NOT a backtest parameter (no parameter_provenance
# entry). Six overlaid equity curves stay visually distinguishable, and the headline use case
# (the M-A/B/C/D cost-sensitivity rows) is four. The cap bounds the run COUNT; each run carries
# its full single-run equity payload (the C3 acceptance — payload = 6x the single-run bundle).
_RUN_COMPARE_CAP = 6


@browser_router.post("/runs/compare")
async def compare_runs(body: CompareRunsIn):
    """Overlay N completed runs (Unit-4 D1): {runs: [{id, name, stats, equity}]} in REQUESTED order.

    Capped at 6 (reject-don't-truncate: over-cap -> 422 `compare_too_large`, mirroring the
    fundamentals compare). Only succeeded runs are comparable; a missing or non-succeeded id is
    refused with a 422 `run_not_comparable` naming the id + status (no partial answer). Event runs
    compare normally (equity + stats exist; the price-only field is not part of the response). The
    bundle build is read-heavy (a parquet read per run), so it runs off the event loop.
    """
    # Cap check first (cheap, no DB / no file): over-cap -> 422 compare_too_large.
    if len(body.run_ids) > _RUN_COMPARE_CAP:
        raise HTTPException(status_code=422, detail={"code": "compare_too_large",
            "message": f"compare accepts at most {_RUN_COMPARE_CAP} runs; got {len(body.run_ids)}"})
    try:
        runs = await asyncio.to_thread(export.build_run_comparison, body.run_ids)
    except export.RunNotComparable as exc:
        # 409 Conflict: the request is well-formed, but a named run is in a state that cannot be
        # compared (missing, or not succeeded). Names the offending id + status, reject-don't-
        # truncate (no partial result). 409 over 422 because the body is valid; the conflict is
        # with the run's current state. (The over-cap case above stays 422 — that IS a bad body.)
        raise HTTPException(status_code=409, detail={"code": "run_not_comparable",
            "message": str(exc), "run_id": exc.run_id, "status": exc.status})
    return {"runs": runs}


@browser_router.get("/event-feeds")
async def list_event_feeds(recent_n: int = 5):
    """Per-event-type live-feed summary (spec brief §14 / brief K, owner auth at Caddy edge).

    Backs the Live Feeds page: for EVERY canonical event type (even ones with no
    data, for an honest empty state), the active-event count, the latest
    knowledge_at, and the most recent `recent_n` events. Reads research_events
    directly — the batch endpoints above are batch-grain and expose no per-type
    counts (§16 build-time check), so this thin read is the sanctioned path.

    Response: a BARE ARRAY, one entry per canonical event type (sorted), each
      {event_type, total_count, latest_knowledge_at, recent_events: [...]}.
    """
    # Clamp recent_n the way the event-batches limit is clamped (default 5, the
    # "most recent few" of §14; min 1, max 25). No 422 — clamp, like list_event_batches.
    if recent_n < 1:
        recent_n = 1
    if recent_n > 25:
        recent_n = 25

    def _fetch():
        # Open the connection INSIDE the thread target: sqlite3 connections default
        # to check_same_thread=True, so the conn must be created AND used on the same
        # (worker) thread. Mirrors get_event_batch / list_event_batches above.
        with eventsdb.get_events_conn() as conn:
            return event_feeds.summarize_event_feeds(conn, recent_n)

    return await asyncio.to_thread(_fetch)


@browser_router.get("/events/search")
async def search_events(
    types: str = Query(...),
    q: str | None = None,
    ticker: str | None = None,
    action: str | None = None,
    after_cursor: str | None = None,
    limit: int = 40,
):
    """Events tab feed (lane UI38-0): active events of the listed types, newest first, filtered by
    words, ticker and analyst action, with the count of every match. Keyset cursor as GET /events."""
    try:
        type_list = events_browse.parse_types(types)
    except events_browse.BadTypes as exc:
        raise HTTPException(422, str(exc))
    limit = max(1, min(limit, 200))

    def _fetch() -> dict:
        with eventsdb.get_events_conn() as conn:
            return events_browse.search_events(conn, type_list, q, ticker, action, after_cursor, limit)

    try:
        return await asyncio.to_thread(_fetch)
    except events_browse.MalformedCursor as exc:
        raise HTTPException(400, str(exc))


@browser_router.get("/events/news-top")
async def events_news_top(days: int = Query(30, ge=1, le=365), limit: int = Query(12, ge=1, le=50)):
    """Tickers most in the news over the last `days` days, each counted once per event."""
    def _fetch() -> list:
        with eventsdb.get_events_conn() as conn:
            return events_browse.news_top(conn, int(time.time()), days, limit)

    return await asyncio.to_thread(_fetch)


@browser_router.get("/events/rating-actions")
async def events_rating_actions():
    """Analyst rating events grouped by action, most common first."""
    def _fetch() -> dict:
        with eventsdb.get_events_conn() as conn:
            return events_browse.rating_actions(conn)

    return await asyncio.to_thread(_fetch)


@browser_router.get("/events/earnings")
async def events_earnings(limit: int = Query(60, ge=1, le=200)):
    """The newest earnings releases with surprise and price reaction (lane EA3-0)."""
    def _fetch() -> dict:
        with eventsdb.get_events_conn() as conn:
            return events_browse.earnings(conn, limit)

    try:
        return await asyncio.to_thread(_fetch)
    except earnings_reaction.ClosesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@browser_router.get("/events")
async def list_events(
    event_type: str | None = None,
    batch_id: int | None = None,
    after_cursor: str | None = None,
    limit: int = 100,
):
    """Paginated, filterable read of research_events for the D2 event-data table (Brief B, R11).

    Keyset pagination (not OFFSET) so concurrent ingestion never skips or repeats a row. The order
    key follows the index per filter, so every path is index-served, no temp B-tree sort (arch §4b):
      - event_type set -> newest-first by (knowledge_at, event_id) via ix_events_type_knowledge;
                          order_key='knowledge_at', cursor '<knowledge_at>_<event_id>'.
      - batch_id only  -> newest-first by event_id DESC, the leaf order of ix_events_batch;
                          order_key='event_id', cursor '<event_id>'.
      - neither        -> newest-first by event_id DESC (the rowid); order_key='event_id'.
      - both           -> rides ix_events_type_knowledge with batch_id as a residual filter.
    status='active' only (a rolled-back row is not live; the column is not indexed, so it filters as a
    residual). The full parsed payload is returned via a bare json.loads that fails loud on a
    store-corrupted payload (the json_valid CHECK backs it). Reads the server events.db -> no transport.
    """
    limit = max(1, min(limit, 500))  # bound the page (default 100), clamp like the other event reads

    def _fetch() -> dict:
        # Open + use the conn inside the thread target (sqlite check_same_thread=True; the live-feeds
        # precedent). The keyset query lives in events_read so the probe exercises the real code (B-E*).
        with eventsdb.get_events_conn() as conn:
            return events_read.read_events_page(conn, event_type, batch_id, after_cursor, limit)

    try:
        return await asyncio.to_thread(_fetch)
    except events_read.MalformedCursor as exc:  # ONLY a bad cursor -> 400; a corrupt-payload JSONDecodeError (also a ValueError) still 500s
        raise HTTPException(400, str(exc))


@browser_router.get("/event-batches/{batch_id}/quality")
async def get_event_batch_quality(batch_id: int):
    """Return the parsed quality_report JSON for a batch (spec brief §6.5).

    Status mapping:
      200  — quality_report present (any closed-batch shape) or
              `{"quality_report": null, "reason": "..."}` for a cancelled_partial
              whose secondary sweep has not yet run.
      404  — batch_not_found
      409  — batch_still_running (the close UPDATE is the only path that
              writes quality_report; running batches have it NULL by design)
    """
    # Owner endpoint: always pass include_logs=True so the full quality_report
    # (with quarantines_samples + dedup_collisions arrays) comes through. The
    # share path explicitly sets these to False when the share row's flag is
    # False; this owner endpoint sees everything.
    def _build():
        return event_quality_bundle.build_quality_report_bundle(
            batch_id, include_logs=True,
        )
    try:
        bundle = await asyncio.to_thread(_build)
    except event_quality_bundle.BundleError:
        return JSONResponse(status_code=404,
                            content={"error": "batch_not_found",
                                     "batch_id": batch_id})
    except event_quality_bundle.BundleStillRunning:
        return JSONResponse(status_code=409,
                            content={"error": "batch_still_running",
                                     "batch_id": batch_id})

    # The §6.5 contract returns the raw quality_report JSON; the bundle's
    # `report` slot is exactly that (already parsed). Surface as-is, with a
    # `null`-quality_report shape when the batch is a cancelled_partial that
    # has not yet been swept.
    if bundle["report"].get("shape") is None:
        return {"quality_report": None,
                "reason": bundle["report"].get("reason") or "not yet written"}
    return {"quality_report": bundle["report"]}


@browser_router.get("/event-batches/{batch_id}/export")
async def export_event_batch(batch_id: int, format: str = "html"):
    """Export the quality report bundle as json|md|html (spec brief §6.6).

    Mirrors M3 K.5 /runs/{id}/export semantics: same render contract; same
    error bodies; same Content-Disposition pattern. _links populated via the
    `_export_url` helper (brief F adds the 'event-batches' scope).

    Status mapping:
      200 — bytes, with Content-Type + Content-Disposition headers
      400 invalid_format
      404 batch_not_found
      409 batch_still_running (mirrors K.5)
    """
    chosen = _QUALITY_EXPORT_FORMATS.get(format)
    if chosen is None:
        return JSONResponse(status_code=400,
                            content={"error": "invalid_format",
                                     "format": format})
    renderer, content_type, ext = chosen
    # Owner endpoint: same include_logs=True policy as /quality above.
    def _build():
        return event_quality_bundle.build_quality_report_bundle(
            batch_id, include_logs=True,
        )
    try:
        bundle = await asyncio.to_thread(_build)
    except event_quality_bundle.BundleError:
        return JSONResponse(status_code=404,
                            content={"error": "batch_not_found",
                                     "batch_id": batch_id})
    except event_quality_bundle.BundleStillRunning:
        return JSONResponse(status_code=409,
                            content={"error": "batch_still_running",
                                     "batch_id": batch_id})
    # K.5 retrofit pattern: populate _links so the MD / HTML footers point
    # readers at the sibling formats.
    bundle["_links"] = {
        "json": _export_url("event-batches", batch_id, "json"),
        "md":   _export_url("event-batches", batch_id, "md"),
        "html": _export_url("event-batches", batch_id, "html"),
    }
    body = renderer(bundle)
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Content-Disposition":
                f'attachment; filename="batch_{batch_id}_quality.{ext}"',
        },
    )


class _CreateBatchShareBody(BaseModel):
    """Request body for POST /event-batches/{batch_id}/share (brief F).

    Mirrors the M3 K.6 _CreateShareBody shape but for a batch target. The five
    content flags carry the §6.7 repurposed semantics (source = batch
    source_descriptor; settings = policy + request criteria; logs = full
    quality_report including quarantines_samples + dedup_collisions arrays;
    indicators/trades unused on batch shares).
    """
    include_source: bool = True
    include_settings: bool = True
    include_indicators: bool = True
    include_trades: bool = True
    include_logs: bool = False
    view_limit: Optional[int] = 10  # nullable; explicit None = unlimited


@browser_router.get("/event-batches/{batch_id}/shares")
async def list_event_batch_shares(batch_id: int):
    """List active (non-revoked) share rows for a batch.

    Mirrors M3 K.6's /runs/{run_id}/shares additive endpoint. Backs the
    admin UI's batch-share-management section (the brief F equivalent of
    RunExplorer's ShareSection).
    """
    return researchdb.list_active_shares_for_batch(batch_id)


@browser_router.post("/event-batches/{batch_id}/share")
async def create_event_batch_share(batch_id: int, body: _CreateBatchShareBody):
    """Create a polymorphic share row pointing at an events.db batch_id.

    The batch must exist in events.db; if it does not, return 404 before
    inserting a dangling share row (the schema cannot enforce this via FK
    because the share table lives in research.db and the batch lives in
    events.db; lineage is enforced here in code per spec brief §3.3 of arch).

    Response 201:
      {"uuid": "...", "url": "https://.../api/research/share/<uuid>"}

    Errors:
      404 — batch_not_found
    """
    # Existence check against events.db
    def _check_batch_exists():
        with eventsdb.get_events_conn() as conn:
            return conn.execute(
                "SELECT batch_id FROM research_event_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone() is not None

    exists = await asyncio.to_thread(_check_batch_exists)
    if not exists:
        return JSONResponse(status_code=404,
                            content={"error": "batch_not_found",
                                     "batch_id": batch_id})

    share = await asyncio.to_thread(
        researchdb.create_batch_share,
        batch_id,
        include_source=body.include_source,
        include_settings=body.include_settings,
        include_indicators=body.include_indicators,
        include_trades=body.include_trades,
        include_logs=body.include_logs,
        view_limit=body.view_limit,
    )
    # M3 K.6 same shape: the public-route URL the share resolves to. Brief F
    # reuses the M3 K.6 public route (/api/research/share/{uuid}) — the route
    # dispatches internally on share.run_id vs share.batch_id.
    share_url = _export_url("share", share["uuid"], "html").replace("?format=html", "")
    await broadcaster.publish("research.batch_share_created", {
        "uuid": share["uuid"], "batch_id": share["batch_id"],
        "created_at": share["created_at"],
    })
    return JSONResponse(
        status_code=201,
        content={"uuid": share["uuid"], "url": share_url, "share": share},
    )


# ============================================================================
# M2.5 brief G — Correction + Rollback admin endpoints
# ============================================================================


class _CorrectionBody(BaseModel):
    """Request body for POST /event-batches/correction per spec brief §6.8."""
    event_id: int
    corrected_payload: dict
    companies: list[dict]
    trigger_note: str = Field(..., min_length=1)


@browser_router.post("/event-batches/correction")
async def post_event_batch_correction(body: _CorrectionBody):
    """Apply a single-row correction to an active event.

    Spec brief §6.8. Calls events.write_core.apply_correction which enforces
    the EL23 guards (CorrectionAgainstRunningBatch + CorrectionAltersCompanyLinks)
    and writes a correction-batch with the §7.5 correction_summary report
    shape on the batch row.

    Errors:
      404 event_not_found
      409 correction_against_running_batch  — EL23 / SFv4-1
      409 correction_alters_company_links   — EL23 / round-5 correction 2
                                              (body carries the (cik, role, cusip)
                                              set diff so the admin UI can render
                                              the per-row delta)
      422 payload_validation_failed         — per-type validator rejected the
                                              corrected payload
    """
    def _run() -> event_write_core.BatchResult:
        return event_write_core.apply_correction(
            event_id=body.event_id,
            corrected_payload=body.corrected_payload,
            companies=body.companies,
            trigger_note=body.trigger_note,
        )

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as exc:
        # ValueError from apply_correction means "event_id not found" or
        # "parent batch missing"; the latter would be a data-integrity bug.
        # Per §6.8 we surface as 404.
        return JSONResponse(
            status_code=404,
            content={"error": "event_not_found", "event_id": body.event_id,
                     "detail": str(exc)},
        )
    except _CorrectionAgainstRunningBatch as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "correction_against_running_batch",
                     "event_id": exc.event_id,
                     "running_batch_id": exc.batch_id},
        )
    except _CorrectionAltersCompanyLinks as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "correction_alters_company_links",
                     "event_id": exc.event_id,
                     "diff": {"added": exc.added, "removed": exc.removed}},
        )
    except _PayloadValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "payload_validation_failed",
                     "class": type(exc).__name__,
                     "message": str(exc)},
        )

    # Broadcast for the in-process SSE channel; the admin UI can watch
    # research.event_batch_completed and tie correction-batches to their
    # parent rows.
    await broadcaster.publish(
        "research.event_batch_completed",
        {
            "batch_id": result.batch_id,
            "event_type": None,   # correction batches inherit type via parent event
            "source": None,
            "status": result.status,
            "content_hash": result.content_hash,
            "event_count": result.event_count,
            "quarantine_count": result.quarantine_count,
            "kind": "correction",
        },
    )

    return {
        "batch_id": result.batch_id,
        "event_id": body.event_id,
        "new_version": result.quality_report.get("new_version"),
        "content_hash": result.content_hash,
    }


class _RollbackBody(BaseModel):
    """Request body for POST /event-batches/{batch_id}/rollback per §6.9."""
    confirm: str = Field(..., min_length=1)
    trigger_note: str = Field(..., min_length=1)


def _rollback_eligibility_error(target: dict, batch_id: int) -> Optional[dict]:
    """Return None if the target batch is rollback-eligible, else the 409 body.

    Per §6.9 / RN2 eligibility is BOTH:
      - status IN ('succeeded', 'succeeded_with_quarantines')
      - kind IN ('fetch', 'load')

    Already-rolled-back, failed, cancelled_partial, running, correction, and
    rollback batches are all rejected.
    """
    if target["status"] not in ("succeeded", "succeeded_with_quarantines"):
        return {"error": "batch_not_eligible",
                "batch_id": batch_id,
                "kind": target["kind"],
                "status": target["status"]}
    if target["kind"] not in ("fetch", "load"):
        return {"error": "batch_not_eligible",
                "batch_id": batch_id,
                "kind": target["kind"],
                "status": target["status"]}
    return None


@browser_router.post("/event-batches/{batch_id}/rollback")
async def post_event_batch_rollback(batch_id: int, body: _RollbackBody):
    """Roll back every active event in batch_id per spec brief §6.9 / EL42.

    The `confirm` body field must EXACTLY equal `"ROLLBACK BATCH <batch_id>"`
    (case-sensitive). Mismatch returns 400 `confirm_phrase_mismatch` with the
    expected phrase echoed back so the admin UI can render the right hint.

    Eligibility: target batch must be in
    `status IN ('succeeded', 'succeeded_with_quarantines')` AND
    `kind IN ('fetch', 'load')` per RN2. Already-rolled-back, failed,
    cancelled_partial, running, correction, and rollback batches are
    rejected with 409 `batch_not_eligible`.

    Returns 200 `{rollback_batch_id, rolled_back_event_count, content_hash}`
    on success.
    """
    expected = f"ROLLBACK BATCH {batch_id}"
    if body.confirm != expected:
        return JSONResponse(
            status_code=400,
            content={"error": "confirm_phrase_mismatch", "expected": expected},
        )

    # Pre-check existence + eligibility against the batch row. apply_rollback
    # has its own ValueError raises for the same conditions; surfacing here
    # gives a richer 409 body.
    def _read_target():
        with eventsdb.get_events_conn() as conn:
            return conn.execute(
                "SELECT batch_id, kind, source, status FROM research_event_batches "
                "WHERE batch_id = ?", (batch_id,),
            ).fetchone()

    target = await asyncio.to_thread(_read_target)
    if target is None:
        return JSONResponse(
            status_code=404,
            content={"error": "batch_not_found", "batch_id": batch_id},
        )
    eligibility = _rollback_eligibility_error(target, batch_id)
    if eligibility is not None:
        return JSONResponse(status_code=409, content=eligibility)

    def _run() -> event_write_core.BatchResult:
        return event_write_core.apply_rollback(
            target_batch_id=batch_id,
            trigger_note=body.trigger_note,
        )

    try:
        result = await asyncio.to_thread(_run)
    except ValueError as exc:
        # apply_rollback also raises ValueError for missing / wrong-kind; we
        # already covered both above, so reaching here means a race (e.g. the
        # batch was deleted between the pre-check and the rollback). Surface
        # honestly as 409.
        return JSONResponse(
            status_code=409,
            content={"error": "batch_not_eligible",
                     "batch_id": batch_id, "detail": str(exc)},
        )

    await broadcaster.publish(
        "research.event_batch_completed",
        {
            "batch_id": result.batch_id,
            "event_type": None,
            "source": target["source"],
            "status": result.status,
            "content_hash": result.content_hash,
            "event_count": result.event_count,
            "quarantine_count": 0,
            "kind": "rollback",
            "target_batch_id": batch_id,
        },
    )

    return {
        "rollback_batch_id": result.batch_id,
        "rolled_back_event_count": result.event_count,
        "content_hash": result.content_hash,
    }


@browser_router.post("/admin/reconcile-event-catalog")
async def reconcile_event_catalog_admin():
    """Manual trigger for the event-catalog reconciliation backstop (brief H, §6.11).

    Walks every event-series catalog row + every distinct (event_type, source)
    in events.db; refreshes any row whose values drifted; surfaces orphans both
    directions. Runs synchronously inside the request handler at M2.5 volumes
    (one UPSERT per pair; pairs are O(event_types × sources) ≈ a handful in
    M2.5). Empty body on request; no parameters.

    Response: 200 with {series_walked, series_updated, orphan_catalog_rows,
    orphan_batches, duration_ms}. Errors are propagated as 500 (the function
    catches per-pair failures internally; only a wholesale crash escapes).
    """
    started = time.time()
    result = await asyncio.to_thread(event_catalog.reconcile_event_catalog)
    duration_ms = int((time.time() - started) * 1000)
    return {
        "series_walked": result.series_walked,
        "series_updated": result.series_updated,
        "orphan_catalog_rows": [list(p) for p in result.orphan_catalog_rows],
        "orphan_batches": result.orphan_batches,
        "duration_ms": duration_ms,
    }


@browser_router.get("/strategies")
async def list_strategies():
    """Current-state strategy list for the new-run dropdown (brief J)."""
    return researchdb.list_strategies()


@browser_router.get("/strategies/catalog")
async def strategy_catalog():
    """The in-editor reference catalog (M4 R13/R14, §6): the contract surface, helpers, settings, the
    registered event types + fundamental definitions, and the parsed indicator reference — all a virtual
    view computed on read from the live modules/registries/reference (never a stored snapshot, D4).

    Declared BEFORE GET /strategies/{strategy_id} so the literal 'catalog' is not parsed as an int id.
    Offloaded to a worker thread: it imports + introspects the contract packages and reads the settings
    rows + the (cached) indicator parse — never block the event loop.
    """
    return await asyncio.to_thread(catalog.build_catalog)


@browser_router.get("/strategies/overview")
async def strategies_overview(include_synthetic: bool = False):
    """UI17-0: every active strategy with its newest real run's sparkline and stats, in one read.

    Declared BEFORE GET /strategies/{strategy_id} so the literal 'overview' is not parsed as an int id.
    """
    return researchdb.list_strategies_overview(include_synthetic)


@browser_router.get("/strategies/names")
async def strategy_names():
    """DN1: every strategy, active and deleted, as [{id, name, display_name, status}] ordered by id.

    One read for the pages to map a strategy id to its display name; a deleted strategy is included so its
    orphaned runs stay listed. Declared BEFORE GET /strategies/{strategy_id} so 'names' is not parsed as an id.
    """
    return researchdb.list_strategy_names()


@browser_router.get("/strategies/{strategy_id}/parameters")
async def strategy_parameters(strategy_id: int, include_synthetic: bool = False):
    """UI17b-0: every parameter of one strategy with its value and its provenance or ranges line."""
    result = await asyncio.to_thread(researchdb.strategy_parameters, strategy_id, include_synthetic)
    if result is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return result


@browser_router.get("/sizing-methods")
async def list_sizing_methods():
    """The sizing-method registry (#376 ruling 4, the dashboard UI plan's 2.3.3 item 2 option (a)):
    id, description, and the five SizingSpec fields, for every registered method.

    Lazy import inside the handler (the paperdb.py:1583 precedent): strategies.helpers.sizing_methods
    imports only the standard library, so this route never pulls pandas or vbt into the backend's
    boot; a probe asserts the module is absent from sys.modules right after the app itself is
    imported, to keep that true.

    The bare `strategies.X` form, never `research_lab.strategies.X`: every file inside
    sizing_methods/ (vol_target_atr.py's own `from strategies.helpers.sizing_methods import
    SizingMethod`, matching every entrypoint) uses that bare form at runtime, because it is the one
    a container's `sys.path[0] = /app` resolves. Importing the SAME package here under the
    `research_lab.`-qualified name would load it under a SECOND, different sys.modules key, so the
    SizingMethod class this route's own isinstance check would see is a different object than the
    one vol_target_atr.py's METHOD is built from -- an isinstance mismatch discovered by AC-E7's
    probe, not assumed. `_ensure_strategies_on_path` (catalog.py) must already have put research_lab/
    on sys.path for the bare form to resolve at all; main.py's boot calls it via
    catalog.load_strategy_templates() before any request is served.
    """
    catalog._ensure_strategies_on_path()  # idempotent; the same pre-condition boot already established
    # E-R1-N4: a synchronous import inside an async handler, acknowledged rather than fixed. It blocks
    # the event loop only on the FIRST call (module import + a handful of small file reads under
    # sizing_methods/); every later call is a sys.modules cache hit, matching every other lazy-import
    # route in this file (paperdb.py:1583's own precedent, cited above).
    from strategies.helpers.sizing_methods import REGISTRY  # lazy, and the SAME bare form every caller uses

    return {
        "methods": [
            {
                "id": method.id,
                "description": method.description,
                "spec": asdict(method.spec),
            }
            for method in REGISTRY.values()
        ]
    }


@browser_router.get("/provenance/{dotted_key:path}")
async def get_provenance(dotted_key: str):
    """Provenance for a setting key '{scope}.{key}' (brief L tooltip): source / mechanism /
    sensitivity / bounds as markdown. 404 if the key has no documented entry."""
    entry = researchdb.provenance_entry(dotted_key)
    if entry is None:
        raise HTTPException(404, f"no provenance entry for {dotted_key!r}")
    return entry


@browser_router.put("/provenance/{dotted_key:path}")
async def put_provenance(dotted_key: str, body: PutProvenanceIn):
    """Author/edit a USER-AUTHORED setting's provenance (M4.5 3b B1.4 / D5, R-PROV2).

    The SAME single-:path-param form as GET /provenance/{dotted_key:path}: Starlette cannot split one
    segment into two params on a literal dot, and built-in keys carry dots inside the key half (e.g.
    fees.crypto.default). scope = the first dot-segment, key = the remainder (the join provenance_entry
    uses). Refuses fail-loud (specific 400, no catch-all) when: any of the four prose fields is empty;
    the bounds_spec is missing or malformed; or the scope collides with a built-in scope name (AD7
    disjointness — built-in provenance is committed markdown, never DB-authored)."""
    scope, _, key = dotted_key.partition(".")  # scope = first segment, key = remainder (built-in dot rule)
    if not key:
        raise HTTPException(
            400, f"provenance key {dotted_key!r} must be '{{scope}}.{{key}}' (a scope and a key)",
        )
    # The four-field rule (R-PROV2): every prose field present and non-empty after trimming.
    prose = {"source": body.source, "mechanism": body.mechanism,
             "sensitivity": body.sensitivity, "bounds": body.bounds}
    missing = [name for name, val in prose.items() if not (val or "").strip()]
    if missing:
        raise HTTPException(400, f"provenance requires non-empty {', '.join(missing)}")
    # The scope-namespace guard (AD7 disjointness): an authored scope may not shadow a built-in scope.
    if scope in researchdb.builtin_scopes():
        raise HTTPException(
            400, f"scope {scope!r} is a built-in scope; built-in provenance is markdown, not DB-authored",
        )
    # A well-formed bounds_spec is required, so B2.3's "the authored value satisfies its own bounds_spec"
    # always has a bound to check (no silent no-op on NULL). Malformed -> a specific 400, not a catch-all.
    if body.bounds_spec is None:
        raise HTTPException(400, "provenance requires a structured bounds_spec")
    try:
        researchdb.validate_bounds_spec(body.bounds_spec)
    except ValueError as exc:
        raise HTTPException(400, f"malformed bounds_spec: {exc}")
    researchdb.set_setting_provenance(
        scope, key, body.source.strip(), body.mechanism.strip(), body.sensitivity.strip(),
        body.bounds.strip(), body.bounds_spec, int(time.time()),
    )
    return {"ok": True}


@browser_router.put("/strategies/{strategy_id}")
async def put_strategy(strategy_id: int, body: PutStrategyIn):
    """Edit a strategy's source from the API (brief G). Same upsert_strategy() as the CLI.

    DB write is the source of truth; the disk mirror + git hook are best-effort and never fail
    the request (L9). 404 if the id is unknown; 400 if the source is invalid.
    """
    strat = researchdb.get_strategy(strategy_id)
    if strat is None:
        raise HTTPException(404, "strategy not found")
    if strat["status"] == 'deleted':  # TERM F2: a PUT must not resurrect a tombstoned strategy
        raise HTTPException(409, "strategy is deleted; it cannot be edited")
    try:
        sid, version, action = researchdb.upsert_strategy(strat["name"], body.source_code, display_name=body.display_name)
    except researchdb.StrategyValidationError as exc:
        raise HTTPException(400, str(exc))

    # Best-effort mirror + git hook, then persist the outcome (derived data, never blocks the 2xx).
    status, path, error = strategy_mirror.mirror_strategy(strat["name"], body.source_code)
    researchdb.set_strategy_mirror(sid, path, status, error)

    if action != "unchanged":
        await broadcaster.publish("research.strategy_registered", {
            "strategy_id": sid, "name": strat["name"], "version": version, "action": action,
        })
    return {"id": sid, "name": strat["name"], "display_name": researchdb.get_strategy(sid)["display_name"],
            "version": version, "action": action, "mirror_status": status}


class RenameStrategyIn(BaseModel):
    display_name: str


@browser_router.patch("/strategies/{strategy_id}/display-name")
async def patch_strategy_display_name(strategy_id: int, body: RenameStrategyIn):
    """NM1a: a person renames any strategy, active or deleted. 404 unknown id; 400 with the reason for a bad name."""
    try:
        await asyncio.to_thread(researchdb.rename_strategy, strategy_id, body.display_name)
    except researchdb.StrategyNotFound:
        raise HTTPException(404, "strategy not found")
    except researchdb.StrategyValidationError as exc:
        raise HTTPException(400, str(exc))
    return {"id": strategy_id, "display_name": researchdb.get_strategy(strategy_id)["display_name"]}


@browser_router.post("/strategies", status_code=201)
async def create_strategy(body: CreateStrategyIn):
    """Add a NEW strategy (D5, pre-M4 Brief A1). Create-only: 409 if the name already exists
    (editing is the separate PUT screen). No new domain function — upsert_strategy already
    create-branches on a new name and validates the source at the top, so create already validates.

    The refusal is RACE-FREE: upsert_strategy(create_only=True) does the name check and the refusal
    INSIDE its write transaction (BEGIN IMMEDIATE) and raises StrategyNameExists rather than silently
    updating, so there is no check-then-write TOCTOU even if the calls were ever made concurrent.
    409 echoes the existing strategy id so the UI can route the operator to the edit screen. Mirrors
    put_strategy's validation-error mapping, best-effort disk mirror, and SSE publish.
    """
    try:
        sid, version, action = researchdb.upsert_strategy(
            body.name, body.source_code, create_only=True, display_name=body.display_name)
    except researchdb.StrategyNameExists as exc:
        raise HTTPException(
            status_code=409,
            detail=f"strategy_name_exists: '{body.name}' already exists (id={exc.strategy_id}); "
                   f"edit it via PUT /strategies/{exc.strategy_id}",
        )
    except researchdb.StrategyValidationError as exc:
        raise HTTPException(400, str(exc))

    # Best-effort mirror + git hook, then persist the outcome (derived data, never blocks the 2xx).
    status, path, error = strategy_mirror.mirror_strategy(body.name, body.source_code)
    researchdb.set_strategy_mirror(sid, path, status, error)

    if action != "unchanged":  # a fresh create is always 'created', never 'unchanged' — published.
        await broadcaster.publish("research.strategy_registered", {
            "strategy_id": sid, "name": body.name, "version": version, "action": action,
        })
    return {"id": sid, "name": body.name, "display_name": researchdb.get_strategy(sid)["display_name"],
            "version": version, "action": action, "mirror_status": status}


@browser_router.post("/strategies/validate")
async def validate_strategy(body: ValidateStrategyIn):
    """Validate a strategy source WITHOUT writing it (M4 R6, the editor Validate button).

    Runs the EXACT same researchdb.validate_strategy_source the save path runs (upsert calls it
    first), so Validate and Save can never disagree (R6, quality bar 3). On success returns
    {ok, description}; a StrategyValidationError maps to 400 with the message — byte-identical to the
    PUT/POST save mapping, so the editor renders ONE error surface for both validate and save.
    """
    try:
        description = researchdb.validate_strategy_source(body.source_code)
    except researchdb.StrategyValidationError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "description": description}


@browser_router.get("/strategies/{strategy_id}")
async def get_strategy(strategy_id: int):
    """One strategy's full current state including source_code, for the editor to load (M4 R3).

    404 if the id is unknown. The list endpoint omits source_code (the dropdown does not need it); the
    editor needs the source to populate Monaco, so this read returns the full current-state row.

    #376 ruling 4: the row also carries `sizing_method`, the strategy's declared registry id
    (its own override or the registry's base default), read by AST off the stored source, never
    executed, so the workspace's SETTINGS tab (U.4) has something to show beside the sizing dials.
    """
    strat = researchdb.get_strategy(strategy_id)
    if strat is None:
        raise HTTPException(404, "strategy not found")
    if strat["status"] == 'deleted':  # TERM F2: a tombstone is gone to the editor (a stale deep-link 404s)
        raise HTTPException(404, "strategy not found")
    strat = dict(strat)  # never mutate the researchdb-returned row in place
    strat["sizing_method"] = researchdb.extract_sizing_method(strat["source_code"])
    return strat


@browser_router.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: int):
    """Delete a strategy under the keep+orphan policy (TERM F2, the strategy-delete arch note).

    Destroys the strategy's editable identity (its live source + visibility) but KEEPS its runs
    (orphaned, never destroyed), their artifacts, and their share links. A strategy WITH runs becomes
    a tombstone (hidden from the rail/pickers, source blanked, version history kept so orphaned runs
    still resolve their name and source); a zero-run strategy is fully removed. 404 if the id is
    unknown; idempotent — re-deleting a tombstone is a 200 no-op.
    """
    try:
        result = researchdb.soft_delete_strategy(strategy_id)
    except researchdb.StrategyNotFound:
        raise HTTPException(404, "strategy not found")
    if result["action"] != "noop":  # broadcast only on a real state transition (rail/list refetch)
        await broadcaster.publish("research.strategy_deleted", {
            "strategy_id": strategy_id, "action": result["action"],
            "orphaned_runs": result.get("orphaned_runs", 0),
        })
    return result


@browser_router.get("/strategies/{strategy_id}/versions")
async def list_strategy_versions(strategy_id: int):
    """The version history of one strategy, newest first (M4 R7). {version, description, edited_at}.

    404 if the id is unknown (an empty list would be indistinguishable from a genuine no-history case
    that upsert_strategy makes impossible — every strategy has >= 1 version — so a 404 is the honest
    answer for a bad id).
    """
    if researchdb.get_strategy(strategy_id) is None:
        raise HTTPException(404, "strategy not found")
    return researchdb.list_strategy_versions(strategy_id)


@browser_router.get("/strategies/{strategy_id}/versions/{version}")
async def get_strategy_version(strategy_id: int, version: int):
    """One historical version's source + description, for the Monaco diff editor (M4 R7).

    404 if that (strategy_id, version) was never recorded.
    """
    snap = researchdb.get_strategy_version(strategy_id, version)
    if snap is None:
        raise HTTPException(404, f"no version {version} recorded for strategy {strategy_id}")
    return snap


@browser_router.get("/strategies/{strategy_id}/status")
async def strategy_status(strategy_id: int):
    """The R11 status badge read: which version the DB holds, which the last run used, mirror match.

    - db_version:       the current source-of-truth version (research_strategies.version).
    - last_run_version: the strategy_version the most recent run used, or null if it never ran.
    - mirror_status:    the last best-effort disk-mirror outcome (synced/failed/pending/stale).
    - mirror_present / mirror_matches: whether the derived on-disk mirror exists and is byte-identical
      to the DB source (arch §8: the DB is truth, the mirror is derived; a mismatch is a stale mirror,
      not a wrong DB). 404 if the id is unknown.
    """
    # This route compounds I/O: two SQLite reads plus a disk read of the mirror file (the source can be
    # up to 1 MiB), then a full-string compare. Offload the whole bundle to a worker thread so it never
    # blocks the event loop — the file's convention for compounded reads (the bundle / comparison /
    # event-batch routes all use this _fetch idiom; the trivial single-row reads above stay sync).
    def _fetch():
        strat = researchdb.get_strategy(strategy_id)
        if strat is None:
            return None  # 404 raised in the handler, outside the thread
        mirror = strategy_mirror.read_mirror(strat["name"])  # None if the file is absent
        # M4 R8/R9: AST-read the declared event_types + asset_class_scope off the stored source (the same
        # no-exec reads dispatch uses, one shared parse), so the editor's test-run panel knows the
        # dispatch path (event vs price) and the declared scope without a second round trip. A
        # contract-valid stored source always parses (validate_strategy_source enforces it at write time).
        meta = researchdb.extract_strategy_dispatch_metadata(strat["source_code"])
        return {
            "db_version": strat["version"],
            "last_run_version": researchdb.last_run_strategy_version(strategy_id),
            "mirror_status": strat["last_mirror_status"],
            "mirror_present": mirror is not None,
            "mirror_matches": mirror is not None and mirror == strat["source_code"],
            "event_types": list(meta["event_types"]),               # () for a price strategy
            "asset_class_scope": list(meta["asset_class_scope"]),   # M4 D1: the declared {crypto, equities} subset
            "sweep_params": meta["sweep_params"],                   # M5 U2/C: the universe-sweep grid fields ([] if none)
            "param_mode": meta["param_mode"],                       # M5 C: 'zero' | 'vectorized' | 'inline' (drives the inline cost note)
        }

    result = await asyncio.to_thread(_fetch)
    if result is None:
        raise HTTPException(404, "strategy not found")
    return result


@browser_router.get("/strategies/{strategy_id}/runs")
async def strategy_runs(strategy_id: int, limit: int = 50, offset: int = 0, include_synthetic: bool = False):
    """One strategy's run history, newest first (M4 R12, served by ix_runs_strategy). 404 on bad id.

    Same Run shape GET /runs returns (each row carries strategy_version — the version that run used).
    """
    # Compounded read (existence check + a join that decodes up to 500 sparkline JSON columns): offload
    # to a worker thread, same idiom as the other multi-read routes; never block the event loop.
    def _fetch():
        if researchdb.get_strategy(strategy_id) is None:
            return None  # 404 raised in the handler, outside the thread
        return researchdb.list_runs_for_strategy(strategy_id, limit, offset, include_synthetic=include_synthetic)

    result = await asyncio.to_thread(_fetch)
    if result is None:
        raise HTTPException(404, "strategy not found")
    return result


class TestRunPreflightIn(BaseModel):
    """M4 R8 (§9 friction 3): the daily-price preflight for an editor test run.

    `symbols` is the set the test run will need daily price for — a single ticker for the one-symbol-
    universe shortcut (§9 friction 4), or the member tickers of a chosen universe. The preflight
    answers "no daily price for symbol X" BEFORE dispatch, so the author sees a missing price up front
    instead of as a mid-run uncovered-events failure (the event-run black-screen lesson, R8).
    """
    symbols: list[str] = Field(..., min_length=1)


@browser_router.post("/test-run/preflight")
async def test_run_preflight(body: TestRunPreflightIn):
    """Resolve each symbol to a ready daily price dataset; report which are missing (M4 R8, friction 3).

    Thin over researchdb.resolve_price_slices (the same resolver the worker uses at run time), so the
    preflight and the actual run agree on what "has daily price" means. Returns {resolved, unresolved};
    the editor blocks the test run when `unresolved` is non-empty and names the missing symbols.
    """
    # Resolve is a small indexed read over research_datasets; offload off the event loop for consistency
    # with the other multi-read routes (it touches the same DB the dispatch path reads).
    result = await asyncio.to_thread(researchdb.resolve_price_slices, body.symbols)
    return result  # {"resolved": {symbol: file_path}, "unresolved": [symbol, ...]}


@browser_router.get("/workers")
async def list_workers():
    """List known workers with their current status."""
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, status, last_seen, online_since FROM research_workers;"
        ).fetchall()
    return [dict(r) for r in rows]


# -------- Dataset browser endpoints (M2) --------

@browser_router.post("/datasets", status_code=201)
async def create_dataset(body: CreateDatasetIn):
    """Create a dataset row + queue fetch_dataset job atomically.

    Returns 201 on fresh insert, 200 with existed=true on uniqueness conflict.
    """
    start_ts = parse_iso_to_unix(body.start)
    end_ts   = parse_iso_to_unix(body.end)
    now      = int(time.time())

    # GV2-4a: a symbol is synthetic exactly when it matches the made-up pattern, and the source must say so.
    symbol_is_synthetic = dataset_sources.SYNTHETIC_SYMBOL_PATTERN.match(body.symbol) is not None
    if (body.source == "synthetic") != symbol_is_synthetic:
        raise HTTPException(
            422,
            f"synthetic-symbol-mismatch: source {body.source!r} with symbol {body.symbol!r}; a symbol is "
            f"synthetic only when it looks like SYN_VOL_01, and a synthetic source takes only such symbols.",
        )

    # Alpaca SIP free-tier validation — saves a worker round-trip per spec §6 line 450.
    if body.source == "alpaca_sip":
        if end_ts > now - 15 * 60:
            raise HTTPException(400, "alpaca_sip free tier requires end <= now()-15min")

    # H4 (pre-M4 A1): a price dataset's type is sourced from the named registry constant,
    # never a bare literal, and gated through dataset_storage_kind before the INSERT — the
    # all-at-once retrofit (Q1). This also closes the prior hole where create_dataset omitted
    # dataset_type entirely and leaned on the init-time backfill to type a row made after boot.
    dataset_type = researchdb.DATASET_TYPE_PRICE
    researchdb.dataset_storage_kind(dataset_type)  # raises UnknownDatasetTypeError on an unregistered type

    with researchdb.transaction() as conn:
        # ON CONFLICT DO NOTHING + RETURNING: row on insert, empty on conflict.
        # lastrowid is "left unchanged" on ON CONFLICT DO NOTHING (Python sqlite3
        # docs); RETURNING + fetchone() is the only reliable insert-vs-conflict signal.
        row = conn.execute(
            "INSERT INTO research_datasets "
            "    (name, source, symbol, timeframe, start_ts, end_ts, dataset_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, symbol, timeframe, start_ts, end_ts) DO NOTHING "
            "RETURNING id, status;",
            (body.name, body.source, body.symbol, body.timeframe,
             start_ts, end_ts, dataset_type, now),
        ).fetchone()

        if row is None:
            # Conflict path — return existing dataset id, 200 not 201.
            existing = conn.execute(
                "SELECT id, status FROM research_datasets "
                "WHERE source=? AND symbol=? AND timeframe=? "
                "  AND start_ts=? AND end_ts=?;",
                (body.source, body.symbol, body.timeframe, start_ts, end_ts),
            ).fetchone()
            return JSONResponse(
                {"id": existing["id"], "status": existing["status"], "existed": True},
                status_code=200,
            )

        # Fresh insert — queue the fetch_dataset job atomically in the same transaction.
        dataset_id = row["id"]
        conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at) "
            "VALUES ('fetch_dataset', ?, ?);",
            (json.dumps({
                "source": body.source, "symbol": body.symbol,
                "timeframe": body.timeframe,
                "start": body.start, "end": body.end,
                "dataset_id": dataset_id,
            }), now),
        )

    # SSE broadcast outside the transaction. Event + payload per spec §10 line 1045.
    await broadcaster.publish("research.dataset_created", {
        "id": dataset_id, "name": body.name, "source": body.source,
        "symbol": body.symbol, "timeframe": body.timeframe, "status": "pending",
    })
    return {"id": dataset_id, "status": "pending"}                # 201 from decorator


@browser_router.get("/datasets")
async def list_datasets(status: Literal["active", "deleted", "all"] = "active", include_synthetic: bool = False):
    """List datasets. ?status=active (default, excludes deleted) | deleted | all.

    GV2-4a: made-up (synthetic) datasets are left out unless ?include_synthetic=true.
    """
    # Safe f-string: `where` from a module-level dict; `status` is Literal-validated upstream.
    where = _WHERE_BY_STATUS[status]
    if not include_synthetic:
        where += (" AND " if where.strip() else "WHERE ") + "source != 'synthetic' "
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, name, source, symbol, timeframe, start_ts, end_ts, status, "
            f"       rows, sha256, file_path, file_bytes, last_error, "
            f"       created_at, last_fetched_at, covered_start_ts, covered_end_ts, "
            f"       store_id, store_fetched_at, "  # night four lane S: the EODHD store's own comparable identity, informational only
            f"       dataset_type, event_type, "  # Brief B.3.1: the inspector branches on dataset_type; D8 enumerates universes
            f"       window_start_ts, window_end_ts, "  # AD-UU-3 / B1.4: the price-universe test window, so the row renders it
            f"       display_name, display_name_from_name "  # UNI1: the typed name a page shows (NULL where none was typed); NM1a: 1 when made up from the name
            f"FROM research_datasets "
            f"{where} "
            f"ORDER BY created_at DESC;"
        ).fetchall()
    datasets = [dict(r) for r in rows]
    # Item 1 / D2 visibility: attach member_count to universe rows so an empty universe is
    # visibly empty, not indistinguishable from absent. ONE aggregate over events.db merged
    # in Python (member_counts), never a per-row COUNT — and only when a universe is present.
    # research_universe_membership is in events.db; the listing above is research.db, so the
    # two cannot share one connection (Cowork S1/S2 round; member_counts owns the events read).
    # Two parallel member-count sources, each fetched only when its type is present in the list:
    #   - CIK universes (research_universe_membership, events.db) via member_counts().
    #   - price universes (research_price_universe_membership, research.db) via the M5 U1 branch — the
    #     CIK member_counts() does not see them (different table, different DB), so this is a real
    #     second branch, not free reuse (U1 brief Step 4).
    cik_counts = (
        event_universe.member_counts()
        if any(d["dataset_type"] == event_universe.DATASET_TYPE_UNIVERSE for d in datasets)
        else {}
    )
    price_counts = (
        researchdb.price_universe_member_counts()
        if any(d["dataset_type"] == researchdb.DATASET_TYPE_PRICE_UNIVERSE for d in datasets)
        else {}
    )
    for d in datasets:
        flagged = bool(d.pop("display_name_from_name"))
        if d["dataset_type"] == researchdb.DATASET_TYPE_PRICE_UNIVERSE:
            d["needs_name"] = d["display_name"] is None or flagged   # NM1a: price universes only
        if d["dataset_type"] == event_universe.DATASET_TYPE_UNIVERSE:
            d["member_count"] = cik_counts.get(d["id"], 0)      # CIK universe: absent from aggregate -> zero
        elif d["dataset_type"] == researchdb.DATASET_TYPE_PRICE_UNIVERSE:
            d["member_count"] = price_counts.get(d["id"], 0)    # price universe: the M5 U1 count
        else:
            d["member_count"] = None                            # price / event-series: not applicable
    return datasets


@browser_router.get("/datasets/source-floor")
async def dataset_source_floor(
    source: Literal["binance", "alpaca_sip", "eodhd", "synthetic"],  # eodhd floors via dataset_sources (M5 data-store); synthetic = GV2-4a
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"],
):
    """W8 "Full / earliest-to-latest": the {start, end} ISO-8601 window for (source, timeframe).

    Server-authoritative (the now-relative branches use the server clock), the single source of
    truth for the per-(source, timeframe) earliest floors (decisions D8-2/D8-4, dataset_sources.py +
    the data_sources.* provenance entries). Declared BEFORE GET /datasets/{dataset_id} so the static
    'source-floor' segment is not swallowed by the int path param. FastAPI 422s an unknown source or
    timeframe via the Literal validation.
    """
    return dataset_sources.full_window_iso(source, timeframe, int(time.time()))


@browser_router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: int):
    """Return any dataset including soft-deleted ones (shared getter, also used by the bundle)."""
    row = researchdb.get_dataset(dataset_id)
    if row is None:
        raise HTTPException(404)
    return row


@browser_router.get("/datasets/{dataset_id}/preview")
async def get_dataset_preview(dataset_id: int):
    """Bounded OHLC candle view of a price dataset for the D2 inspector chart (Brief B, R10).

    Reduces the SERVER-HELD preview parquet (uploaded by the worker on every fetch, B.1) to at most
    PREVIEW_MAX_BUCKETS candles via reduce_ohlc, so the browser payload is bounded independent of the
    dataset's row count (arch §3b). 404 if the dataset row is absent; an honest available:false state
    if no preview object has been uploaded yet (a pre-Brief-B dataset, or an upload that failed) — a
    re-fetch (D7) regenerates it, no silent blank chart. The parquet read + reduce runs in a thread
    (CPU/IO, off the event loop). No transport: the parquet is already server-local in the store.
    """
    if researchdb.get_dataset(dataset_id) is None:
        raise HTTPException(404, "dataset not found")
    if not artifact_store.store.exists_dataset_preview(dataset_id, "preview.parquet"):
        return {"dataset_id": dataset_id, "available": False, "reason": "preview_not_generated"}

    def _read_and_reduce() -> dict:
        import pandas as pd  # server-side read (the K.0 read precedent); pandas is a server dependency
        with artifact_store.store.open_dataset_preview(dataset_id, "preview.parquet") as fh:
            df = pd.read_parquet(fh)
        candles = preview.reduce_ohlc(df)
        return {
            "dataset_id": dataset_id,
            "source_rows": len(df),
            "bucket_count": len(candles),
            "candles": candles,
        }

    return await asyncio.to_thread(_read_and_reduce)


@browser_router.delete("/datasets/{dataset_id}")
async def delete_dataset(dataset_id: int):
    """Soft-delete a dataset. Idempotent — re-deleting deleted returns 200 no-op.

    Returns 404 if not found, 409 if currently fetching, 200 otherwise.
    File on disk is NOT removed (M2 spec §3: soft-delete only; hard-delete is M3+).
    """
    with researchdb.transaction() as conn:
        row = conn.execute(
            "SELECT status FROM research_datasets WHERE id=?;",
            (dataset_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        current = row["status"]
        if current == "fetching":
            raise HTTPException(409, "cannot delete dataset while fetching")
        if current == "deleted":
            # Idempotent: already in target state. No UPDATE, no SSE broadcast.
            return {"ok": True}
        conn.execute(
            "UPDATE research_datasets SET status='deleted' WHERE id=?;",
            (dataset_id,),
        )

    # Broadcast only on real state transition (ready/pending/failed -> deleted).
    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "deleted",
    })
    return {"ok": True}


@browser_router.post("/datasets/{dataset_id}/restore")
async def restore_dataset_route(dataset_id: int):
    """Restore a soft-deleted dataset to 'ready' (D7, pre-M4 Brief A1). Branches on the registry
    storage kind (§A1.1/§A1.5): a row-backed universe/event-series flips; a file-backed price
    dataset requires a present file. Idempotent no-op if already non-deleted.

    200 {id, status:'ready'}     on a real restore
    200 {id, status:<current>}   idempotent no-op (already non-deleted)
    404 dataset_not_found
    409 cannot_restore           file-backed dataset whose file is gone/never-completed
    """
    try:
        result = researchdb.restore_dataset(dataset_id)
    except researchdb.DatasetNotFound:
        raise HTTPException(404, "dataset_not_found")
    except researchdb.DatasetFileMissing as exc:
        raise HTTPException(409, f"cannot_restore: {exc}")
    if result["action"] == "restored":
        # Broadcast only on a real transition (deleted -> ready), mirroring delete_dataset.
        await broadcaster.publish("research.dataset_status_changed", {
            "id": dataset_id, "status": "ready",
        })
    return {"id": dataset_id, "status": result["status"]}


@browser_router.post("/datasets/{dataset_id}/refetch")
async def refetch_dataset_route(dataset_id: int):
    """Re-queue a fetch for an existing dataset (D7, pre-M4 Brief A1). create_dataset's
    ON CONFLICT DO NOTHING no-ops on an existing row, so this is the only re-fetch path. The
    worker writes via the §A1.2 durable atomic write, so a partial re-fetch cannot corrupt the
    prior file.

    202 {id, status:'pending', job_id}   re-queue accepted
    404 dataset_not_found
    409 cannot_refetch_while_fetching     status already 'fetching'
    """
    try:
        result = researchdb.refetch_dataset(dataset_id)
    except researchdb.DatasetNotFound:
        raise HTTPException(404, "dataset_not_found")
    except researchdb.DatasetAlreadyFetching:
        raise HTTPException(409, "cannot_refetch_while_fetching")
    # A fetch job is queued and the row is now 'pending'; broadcast the change (mirror create).
    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "pending",
    })
    return JSONResponse(
        {"id": dataset_id, "status": "pending", "job_id": result["job_id"]},
        status_code=202,
    )


@browser_router.post("/datasets/{dataset_id}/update-to-latest")
async def update_dataset_to_latest_route(dataset_id: int):
    """W8 "Update to latest" (D8-1): EXTEND the window to the source's latest end and queue a fetch
    of the extended window (vs refetch, which repairs the same window). The worker re-cuts the whole
    window for a source that re-bases past prices (eodhd, alpaca_sip; PX4) and downloads only the new
    bars for one that does not (binance); the durable atomic write + the D8-3 fetcher-owned cleanup
    repoint file_path.

    202 {id, status:'pending', job_id, new_end_ts}   incremental update accepted
    200 {id, status, action:'noop', reason:'already_current'}   window already reaches the latest end
    404 dataset_not_found
    409 cannot_update_while_fetching      status already 'fetching'
    409 no_existing_file                  no file to append onto (full-fetch first)
    409 window_conflict                   the extended window collides with another dataset
    """
    try:
        result = researchdb.update_dataset_to_latest(dataset_id)
    except researchdb.DatasetNotFound:
        raise HTTPException(404, "dataset_not_found")
    except researchdb.DatasetAlreadyFetching:
        raise HTTPException(409, "cannot_update_while_fetching")
    except researchdb.DatasetFileMissing:
        raise HTTPException(409, "no_existing_file")
    except researchdb.DatasetWindowConflict:
        raise HTTPException(409, "window_conflict")
    except ValueError as exc:
        # The row's source is outside {binance, alpaca_sip} (dataset_sources has no floor for it).
        # research_datasets carries no DB CHECK on source, so surface a clear 400 rather than a raw 500.
        raise HTTPException(400, f"unsupported_source: {exc}")
    if result["action"] == "noop":
        # Nothing to fetch — the requested window already reaches the source's latest end.
        return JSONResponse(
            {"id": dataset_id, "status": result["status"], "action": "noop",
             "reason": result["reason"]},
            status_code=200,
        )
    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "pending",
    })
    return JSONResponse(
        {"id": dataset_id, "status": "pending", "job_id": result["job_id"],
         "new_end_ts": result["new_end_ts"]},
        status_code=202,
    )


@browser_router.delete("/datasets/{dataset_id}/file")
async def hard_delete_dataset_file(dataset_id: int):
    """Hard-delete a soft-deleted dataset's parquet (async, worker-mediated; DECISIONS #16).

    The file is laptop-side and this backend is server-side, so we queue a
    hard_delete_dataset job and the worker removes it, then calls back. Thin wrapper
    over researchdb.request_hard_delete — all decisioning lives there (DECISIONS #17).

    202 {status:"deleting", job_id}  — job queued (or an in-flight one reused)
    200 {ok:true}                    — already clean (file_path IS NULL); nothing queued
    404                              — dataset not found
    409 {error:...}                  — not soft-deleted, or still referenced by runs
    """
    try:
        result = researchdb.request_hard_delete(dataset_id)
    except researchdb.DatasetNotFound:
        raise HTTPException(status_code=404, detail="dataset not found")
    except researchdb.DatasetNotSoftDeleted:
        return JSONResponse({"error": "dataset must be soft-deleted first"}, status_code=409)
    except researchdb.DatasetHasRuns as exc:
        return JSONResponse(
            {"error": f"dataset has {exc.count} runs; cannot hard-delete"}, status_code=409
        )

    if result["action"] == "noop":
        # Already in the cleaned terminal state — no file, no job. Idempotent 200.
        return {"id": dataset_id, "status": "deleted", "ok": True}

    # A cleanup job is in flight. "deleting" is a response-only operation status; the
    # stored research_datasets.status stays 'deleted' until the worker callback lands.
    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "deleting", "job_id": result["job_id"],
    })
    return JSONResponse(
        {"id": dataset_id, "status": "deleting", "job_id": result["job_id"]},
        status_code=202,
    )


# -------- Settings endpoints (M2) --------

def _row_to_setting_dict(row) -> dict:
    """Build the API response shape for one settings row.

    Aliases DB columns to the frontend contract:
      value_type → type
      updated_at → changed_at
    Parses `value` from TEXT to the typed Python value.
    Joins `default_value` from _DEFAULT_SETTINGS_LAYOUT (defaults live in code).
    Joins `ruled_by` the same way (lane RV, AC-2): read from the real layout through
    researchdb.setting_ruled_by, never a second hand-typed list of which rows are ruled. None for a
    row with no ruling, which the wire form carries as JSON null.
    """
    d = dict(row)
    value_type = d["value_type"]
    return {
        "scope": d["scope"],
        "key": d["key"],
        "value": researchdb.parse_value(d["value"], value_type),
        "type": value_type,
        "unit": d.get("unit"),
        "label": d.get("label"),
        "description": d.get("description"),
        "default_value": researchdb.get_default_value(d["scope"], d["key"]),
        "changed_at": d["updated_at"],
        "ruled_by": researchdb.setting_ruled_by(d["scope"], d["key"]),
    }


@browser_router.get("/settings")
async def list_settings():
    """List all settings ordered by (scope, key) with full presentation metadata.

    Returned shape per row matches the frontend Setting type — typed `value`,
    `unit`/`label`/`description`, and `default_value` from the layout constant.
    """
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT scope, key, value, value_type, unit, label, description, updated_at "
            "FROM research_settings "
            "ORDER BY scope, key;"
        ).fetchall()
    return [_row_to_setting_dict(r) for r in rows]


@browser_router.get("/settings/scopes")
async def list_setting_scopes():
    """List scope metadata in display order (M2 brief F.1)."""
    with researchdb.get_conn() as conn:
        rows = conn.execute(
            "SELECT scope, title, description, display_order "
            "FROM research_setting_scopes "
            "ORDER BY display_order ASC, scope ASC;"
        ).fetchall()
    return [dict(r) for r in rows]


@browser_router.get("/settings/history")
async def list_settings_history(
    scope: Optional[str] = None,
    key: Optional[str] = None,
    limit: int = 200,
):
    """Return setting change history, ordered most-recent-first.

    Optional filters: ?scope=, ?key=. Both can be combined.
    Default limit 200; max 1000 (rejects values out of [1, 1000]).
    """
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")

    conditions = []
    params: list = []
    if scope is not None:
        conditions.append("scope = ?")
        params.append(scope)
    if key is not None:
        conditions.append("key = ?")
        params.append(key)
    # Safe f-string: conditions are hardcoded SQL fragments; user input goes
    # through `params` only. Same pattern as list_datasets.
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)

    with researchdb.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, scope, key, old_value, new_value, changed_at, changed_by "
            f"FROM research_settings_history "
            f"{where} "
            f"ORDER BY changed_at DESC, id DESC "
            f"LIMIT ?;",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@browser_router.get("/settings/{scope}/{key}")
async def get_setting(scope: str, key: str):
    """Return a single setting or 404. Response shape matches GET /settings rows."""
    with researchdb.get_conn() as conn:
        row = conn.execute(
            "SELECT scope, key, value, value_type, unit, label, description, updated_at "
            "FROM research_settings WHERE scope=? AND key=?;",
            (scope, key),
        ).fetchone()
    if row is None:
        raise HTTPException(404)
    return _row_to_setting_dict(row)


@browser_router.put("/settings/{scope}/{key}")
async def update_setting(scope: str, key: str, body: UpdateSettingIn):
    """Update a setting value. Accepts typed JSON (number/bool/string/object/etc.).

    The body value is coerced to TEXT (matching the column format) using the
    stored value_type, then validated. Strict audit log: inserts a history row
    for every PUT, even when the new value equals the old. Broadcasts
    research.setting_changed every time.
    """
    now = int(time.time())

    with researchdb.transaction() as conn:
        cur_row = conn.execute(
            "SELECT value, value_type FROM research_settings "
            "WHERE scope=? AND key=?;",
            (scope, key),
        ).fetchone()
        if cur_row is None:
            raise HTTPException(404)

        value_type = cur_row["value_type"]
        new_text = coerce_to_text(body.value, value_type)
        # Defence-in-depth: re-validate the serialized string against the type.
        validate_value(new_text, value_type)

        conn.execute(
            "UPDATE research_settings "
            "   SET value=?, updated_at=? "
            " WHERE scope=? AND key=?;",
            (new_text, now, scope, key),
        )

        conn.execute(
            "INSERT INTO research_settings_history "
            "    (scope, key, old_value, new_value, changed_at, changed_by) "
            "VALUES (?, ?, ?, ?, ?, 'user');",
            (scope, key, cur_row["value"], new_text, now),
        )

    # Broadcast outside the transaction. Event + payload per spec §10 line 1047.
    # Wire format unchanged from Brief B — old_value/new_value as TEXT strings.
    # Frontend uses cached value_type to parse on receive.
    await broadcaster.publish("research.setting_changed", {
        "scope": scope, "key": key,
        "old_value": cur_row["value"], "new_value": new_text,
    })
    return {"ok": True}


# =============================================================
# Per-strategy setting overrides (TERM M4.5 brief 3a B2)
# =============================================================

@browser_router.get("/strategies/{strategy_id}/settings")
async def get_strategy_settings(strategy_id: int):
    """Resolved per-strategy settings (override -> global -> code default) for the workspace editor.

    Each (scope, key) carries: the resolved value, source ('override'|'inherited'), the inherited global
    value (reference), value_type/unit/label, overridable, and source_note (3b). A status='deleted'
    strategy 404s (the AD8.1 guard pattern).
    """
    strat = researchdb.get_strategy(strategy_id)
    if strat is None or strat.get("status") == "deleted":
        raise HTTPException(404, f"strategy {strategy_id} not found")
    return researchdb.strategy_settings_resolved(strategy_id)


@browser_router.put("/strategies/{strategy_id}/settings/{scope}/{key}")
async def set_strategy_override(strategy_id: int, scope: str, key: str, body: UpdateOverrideIn):
    """Set a per-strategy override (M4.5 3b B2.2). The value_type is sourced from the target GLOBAL (never
    guessed from the request), so the NOT NULL value_type column is authoritative. An unknown global key
    -> 400; a non-overridable key -> 400; a deleted strategy -> 404. The value is type-validated AND
    bounds-checked against the setting's structured `bounds` (numeric range + categorical allowed-set,
    fail-loud 400). source_note (the optional D4 override justification) is written; omitted -> NULL.
    """
    now = int(time.time())
    with researchdb.transaction() as conn:
        strat = conn.execute(
            "SELECT status FROM research_strategies WHERE id=?;", (strategy_id,),
        ).fetchone()
        if strat is None or strat["status"] == "deleted":
            raise HTTPException(404, f"strategy {strategy_id} not found")
        glob = conn.execute(
            "SELECT value_type FROM research_settings WHERE scope=? AND key=?;", (scope, key),
        ).fetchone()
        if glob is None:
            raise HTTPException(
                400, f"unknown setting {scope}.{key}: an override must target an existing global setting",
            )
        if not researchdb.setting_overridable(scope, key):
            raise HTTPException(400, f"setting {scope}.{key} is not per-strategy overridable")
        value_type = glob["value_type"]
        new_text = coerce_to_text(body.value, value_type)
        validate_value(new_text, value_type)  # type validity first (3a)
        # 3b B2.2: bounds enforcement. Every overridable setting carries a structured `bounds` (the B2.1
        # boot invariant), so this never hits a no-bounds case at runtime. Check the TYPED value (what a
        # run resolves) against the bound, fail-loud on a range/allowed-set violation.
        bounds = researchdb.setting_bounds(scope, key)
        if bounds is not None:
            typed_value = researchdb.parse_value(new_text, value_type)
            err = researchdb.check_value_against_bounds(typed_value, bounds)
            if err is not None:
                raise HTTPException(400, f"override out of bounds for {scope}.{key}: {err}")
        # source_note: the D4 justification, optional. An empty string collapses to NULL (no justification).
        note = body.source_note.strip() if body.source_note and body.source_note.strip() else None
        conn.execute(
            "INSERT INTO research_strategy_settings "
            "    (strategy_id, scope, key, value, value_type, source_note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(strategy_id, scope, key) DO UPDATE SET "
            "    value=excluded.value, value_type=excluded.value_type, "
            "    source_note=excluded.source_note, updated_at=excluded.updated_at;",
            (strategy_id, scope, key, new_text, value_type, note, now),
        )
    return {"ok": True}


@browser_router.delete("/strategies/{strategy_id}/settings/{scope}/{key}")
async def clear_strategy_override(strategy_id: int, scope: str, key: str):
    """Clear a per-strategy override (revert to the inherited global): delete the row. Idempotent."""
    with researchdb.transaction() as conn:
        conn.execute(
            "DELETE FROM research_strategy_settings WHERE strategy_id=? AND scope=? AND key=?;",
            (strategy_id, scope, key),
        )
    return {"ok": True}


# =============================================================
# Worker endpoints (every route here requires the API key)
# =============================================================

@worker_router.post("/online")
async def worker_online(body: WorkerIdBody):
    """Worker just started up. Upsert it as online."""
    now = int(time.time())
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_workers (id, status, last_seen, online_since) "
            "VALUES (?, 'online', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "    status = 'online', "
            "    last_seen = excluded.last_seen, "
            "    online_since = excluded.online_since;",
            (body.worker_id, now, now),
        )
    await broadcaster.publish(
        "research.worker_online",
        {"worker_id": body.worker_id},
    )
    return {"ok": True}


@worker_router.post("/offline")
async def worker_offline(body: WorkerIdBody):
    """Worker shutting down gracefully. Mark offline only if currently online."""
    now = int(time.time())
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "UPDATE research_workers SET status = 'offline', last_seen = ? "
            "WHERE id = ? AND status = 'online';",
            (now, body.worker_id),
        )
        # rowcount is set by execute() before commit. > 0 means real transition.
        transitioned = cur.rowcount > 0
    # Only broadcast on real state change; avoids spamming clients on repeat calls
    if transitioned:
        await broadcaster.publish(
            "research.worker_offline",
            {"worker_id": body.worker_id, "reason": "graceful"},
        )
    return {"ok": True}


@worker_router.get("/poll")
async def worker_poll(worker_id: str):
    """Atomic poll-and-claim. Returns a claimed job, or 204 if queue is empty.

    Every poll also serves as the worker's idle heartbeat (updates last_seen).

    Stranded-job cleaner step 1 (spec brief §3.1, §5.4 dial 1 sensitivity): this step takes the
    heartbeating job kinds from three to six on the same single-writer database this route's own
    transaction writes to, so this route -- the hottest one in the system -- is where write-lock
    contention from the new heartbeat volume would show up first. `_poll_started` below times the
    transaction for the `worker_poll_latency` ops monitor (docs/ops/ops_monitor_registry.md, NEW row
    owed by this build); the SQL inside the `with` block is otherwise untouched.
    """
    _poll_started = time.monotonic()  # a duration measurement: the monotonic clock, not wall time
    now = int(time.time())
    with researchdb.transaction() as conn:
        # Atomic claim: pick the oldest pending job and flip it to picked_up
        row = conn.execute(
            "UPDATE research_jobs "
            "SET status = 'picked_up', claimed_at = ?, "
            "    worker_id = ?, last_heartbeat = ? "
            "WHERE id = ("
            "    SELECT id FROM research_jobs "
            "    WHERE status = 'pending' AND cancellation_requested = 0 "  # EI-pre: never hand out a cancel-requested job
            "    ORDER BY id LIMIT 1"
            ") RETURNING id, kind, payload_json;",
            (now, worker_id, now),
        ).fetchone()
        # Liveness: poll itself counts as a heartbeat for the worker
        conn.execute(
            "INSERT INTO research_workers (id, status, last_seen, online_since) "
            "VALUES (?, 'online', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "    status = 'online', last_seen = excluded.last_seen;",
            (worker_id, now, now),
        )
        # If we got a job, write the audit event
        run_started = None  # set below if this claim flips a run_backtest run pending -> running
        if row is not None:
            conn.execute(
                "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
                "VALUES (?, ?, 'picked_up', ?);",
                (row["id"], now, json.dumps({"worker_id": worker_id})),
            )
            # EI-3: a run_backtest claim flips its linked run pending -> running, in the SAME
            # transaction as the claim, so a cancel landing mid-claim cannot race (DDIA Ch 8).
            if row["kind"] == "run_backtest":
                flipped = conn.execute(
                    "UPDATE research_runs SET status = 'running', started_at = ? "
                    "WHERE job_id = ? AND status = 'pending' RETURNING id;",
                    (now, row["id"]),
                ).fetchone()
                if flipped is not None:
                    run_started = {"run_id": flipped["id"], "job_id": row["id"], "started_at": now}

    # ops monitor: worker_poll_latency, SAMPLED rather than logged every call.
    #
    # The first version logged unconditionally. At the worker's 2-second poll cadence that is about
    # 43,000 lines a day, per worker, forever, on the hottest route in the system -- and
    # `StreamHandler.emit` is synchronous I/O, so it was blocking the event loop on every poll. That
    # is the same handler whose DATABASE transaction the brief moves onto `asyncio.to_thread` in
    # step 2 for exactly this reason; adding blocking log I/O to it in step 1 worked against that.
    #
    # Sampling rather than a slow-poll threshold, deliberately: a threshold needs a latency number
    # nobody has measured, and inventing one would be the placeholder value the contract forbids. A
    # sampling rate claims nothing about how the system behaves -- it only trades volume for
    # fidelity -- so it needs no measurement to justify. One sample per _POLL_SAMPLE_EVERY polls is
    # roughly one a minute at the current cadence, which still yields a distribution to read a p99
    # from while cutting both the line count and the blocking-I/O rate by the same factor.
    _poll_counter[0] += 1
    if _poll_counter[0] % _POLL_SAMPLE_EVERY == 0:
        log.info("worker_poll_latency",
                 extra={"worker_id": worker_id,
                        "duration_ms": round((time.monotonic() - _poll_started) * 1000.0, 1),
                        "sampled_every": _POLL_SAMPLE_EVERY})
    if row is None:
        return Response(status_code=204)
    await broadcaster.publish(
        "research.job_picked_up",
        {"id": row["id"], "worker_id": worker_id},
    )
    if run_started is not None:
        await broadcaster.publish("research.run_started", run_started)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "payload": json.loads(row["payload_json"]),
    }


@worker_router.post("/jobs/{job_id}/heartbeat")
async def worker_heartbeat(job_id: int, body: HeartbeatBody):
    """Heartbeat from a long-running job. Updates liveness, repairs the worker's online status if the
    90-second sweeper had flipped it offline mid-job, AND carries the cancel signal (EI-2).

    The worker has no DB access, so the cancellation flag rides back on this existing channel
    (no separate poll endpoint). run_backtest reads `cancellation_requested` from the response
    and hard-kills its container when it flips true.

    Stranded-job cleaner step 1 (spec brief §3.1): every one of the six job kinds now reaches this
    route (three of them never called it before this step, and `fake` called it only when its log
    loop happened to fire), so `research_jobs.last_heartbeat` now means the same thing for every
    row. This step also repairs the worker-shows-offline defect the brief measured: a network outage
    longer than 90 seconds mid-job flips the worker row offline with no path back, because nothing
    except `worker_poll` and `worker_online` used to set the status back to online.

    THREE write statements in this function, of which the WORKER-ROW repair is the last TWO. The two
    counts are the same design read at different scopes, and both appear in the record: the brief's
    §3.1 says "it is TWO statements" counting only the repair (the unconditional `last_seen` touch
    plus the separate guarded status flip), while this docstring counts every write the function
    makes, the job's own heartbeat included. Neither is wrong; naming both here is what stops the
    next reader finding a contradiction between the code and the brief that is not there.

    Read the brief's §3.1 before touching this: statements 1 and 2 are byte-identical to the
    pre-step-1 route on purpose, and statement 2 in particular MUST stay unconditional -- adding
    `AND status = 'offline'` to it would stop refreshing `last_seen` while the worker is online, the
    90-second sweeper would then flip it offline, the next heartbeat would flip it back, and the
    dashboard would flap for the whole job.
    """
    now = int(time.time())
    with researchdb.transaction() as conn:
        # Statement 1 (unchanged): the job's own liveness touch.
        conn.execute(
            "UPDATE research_jobs SET last_heartbeat = ? WHERE id = ?;",
            (now, job_id),
        )
        # Statement 2 (unchanged, and unconditional by requirement -- see the docstring above).
        conn.execute(
            "UPDATE research_workers SET last_seen = ? WHERE id = ?;",
            (now, body.worker_id),
        )
        # Statement 3 (NEW): a SEPARATE compare-and-set, never folded into statement 2. Only flips a
        # worker the sweeper marked offline back to online; the broadcast after the `with` block below
        # is what actually repairs the dashboard, since a bare row write is invisible to a browser
        # that last heard "offline" over SSE from the sweeper.
        online_cas = conn.execute(
            "UPDATE research_workers SET status = 'online' WHERE id = ? AND status = 'offline';",
            (body.worker_id,),
        )
        # rowcount is set by execute() before commit. > 0 means a real offline -> online transition,
        # mirroring worker_offline's own transition-only idiom above.
        transitioned = online_cas.rowcount > 0
        # Read after every write above, inside the same transaction: status + kind feed the two ops
        # monitors below; cancellation_requested is the existing EI-2 contract this route answers.
        row = conn.execute(
            "SELECT status, kind, cancellation_requested FROM research_jobs WHERE id = ?;", (job_id,)
        ).fetchone()

    # --- ops monitor: worker_heartbeat_after_terminal (docs/ops/ops_monitor_registry.md, NEW row
    # owed by this build -- see the handoff's "what can break" table). A heartbeat for a job that is
    # already succeeded/failed/cancelled means a heartbeat task outlived the handler it was watching;
    # the expected count is zero. Logged rather than raised: a late heartbeat cannot resurrect
    # anything, it never touches `status`, so this row is visibility only.
    if row is not None and row["status"] in ("succeeded", "failed", "cancelled"):
        log.warning("worker_heartbeat_after_terminal",
                    extra={"job_id": job_id, "status": row["status"],
                           "kind": row["kind"], "worker_id": body.worker_id})
    # --- ops monitor: worker_job_heartbeat_arrivals (same registry, NEW). This route only ever emits
    # the raw per-kind arrival signal; grading it (alarming on a claimed job with zero arrivals after
    # one cadence) is a periodic read across the jobs table and is not this request handler's job.
    #
    # UNCONDITIONAL, and deliberately so, unlike `worker_poll_latency` ninety lines above which was
    # rewritten to sample for the volume reason. Two arguments, and the first one is decisive.
    #
    # SAMPLING THIS LINE WOULD BREAK THE MONITOR IT FEEDS. The graded fact is "a claimed job reported
    # ZERO arrivals within one cadence". Log one heartbeat in thirty and a perfectly healthy job that
    # heartbeats twenty times emits nothing at all, which the grader cannot distinguish from a job
    # that died -- the false positive that kills healthy work, which is the exact failure this whole
    # build exists to prevent. A latency sample loses fidelity when it is sampled; an arrival signal
    # loses its meaning.
    #
    # AND THE VOLUME IS A DIFFERENT ORDER ANYWAY. `worker_poll` fires every 2 seconds forever, idle or
    # not: about 43,000 lines a day per worker, unconditionally. A heartbeat fires every 10 seconds
    # and ONLY while a job is actually running, so its ceiling is about 8,600 lines a day per
    # concurrently-running job at a 100% duty cycle, and the real duty cycle is far below that. Five
    # times slower and gated on work existing.
    if row is not None:
        log.info("worker_job_heartbeat_arrival",
                 extra={"job_id": job_id, "kind": row["kind"],
                        "status": row["status"], "worker_id": body.worker_id})
    # --- ops monitor: worker_online_transition_publishes (same registry, NEW). Publish OUTSIDE the
    # transaction, mirroring worker_offline's own idiom, and only on a real transition -- without this
    # broadcast the row flips in the database while the dashboard, which last heard "offline" from the
    # sweeper, would show offline for the rest of the job (the defect this step repairs).
    if transitioned:
        log.info("worker_online_transition_publish",
                 extra={"worker_id": body.worker_id, "via": "heartbeat"})
        await broadcaster.publish(
            "research.worker_online",
            {"worker_id": body.worker_id},
        )
    return {"ok": True, "cancellation_requested": bool(row["cancellation_requested"]) if row else False}


@worker_router.post("/jobs/{job_id}/log")
async def worker_log(job_id: int, body: LogBody):
    """Append a single log line for a job and broadcast it via SSE."""
    now = int(time.time())
    with researchdb.transaction() as conn:
        row = conn.execute(
            "INSERT INTO research_job_logs(job_id, ts, level, line) "
            "VALUES (?, ?, ?, ?) RETURNING id;",
            (job_id, now, body.level, body.line),
        ).fetchone()
        log_id = row["id"]
    await broadcaster.publish(
        "research.job_log",
        {"id": job_id, "log_id": log_id, "level": body.level, "line": body.line},
    )
    return {"ok": True}


@worker_router.post("/jobs/{job_id}/complete")
async def worker_complete(job_id: int, body: CompleteBody):
    """Finalize a job. Pydantic guarantees body.status is 'succeeded' or 'failed'."""
    now = int(time.time())
    result_json = json.dumps(body.result) if body.result is not None else None

    with researchdb.transaction() as conn:
        conn.execute(
            "UPDATE research_jobs SET status = ?, finished_at = ?, "
            "    result_json = ?, error = ? "
            "WHERE id = ?;",
            (body.status, now, result_json, body.error, job_id),
        )
        conn.execute(
            "INSERT INTO research_job_events(job_id, ts, event, payload_json) "
            "VALUES (?, ?, ?, ?);",
            (job_id, now, body.status,
             json.dumps({"result": body.result, "error": body.error})),
        )

    if body.status == "succeeded":
        await broadcaster.publish(
            "research.job_completed",
            {"id": job_id, "status": "succeeded", "result": body.result},
        )
    elif body.status == "failed":
        await broadcaster.publish(
            "research.job_failed",
            {"id": job_id, "error": body.error},
        )
    # status == 'cancelled' (brief I): the run-level research.run_cancelled already fired from
    # the /worker/runs/{id}/cancelled callback, so no extra job-level event here.
    return {"ok": True}


# -------- Dataset worker endpoints (M2) --------

@worker_router.post("/datasets/{dataset_id}/result")
async def worker_dataset_result(dataset_id: int, body: DatasetWorkerResultIn):
    """Worker reports successful fetch. Auth enforced by worker_router dependency."""
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "UPDATE research_datasets "
            "   SET status='ready', "
            "       rows=?, sha256=?, file_path=?, file_bytes=?, "
            "       covered_start_ts=?, covered_end_ts=?, "
            "       window_start_ts=?, window_end_ts=?, "
            "       store_id=?, store_fetched_at=?, "
            "       last_fetched_at=? "
            " WHERE id=?;",
            # W3: persist the real fetched range alongside the row count (None for an
            # older worker / empty fetch -> NULL, the frontend falls back to start_ts/end_ts).
            # Set 9 night lane DX (U.10 item 5a): the test window a price dataset is graded
            # against is now the SAME value as the coverage the runner actually measured from the
            # file it wrote, set in this SAME statement so a dataset is never briefly "ready" with
            # a coverage fact but no window (an atomic write, DDIA Ch 7's own single-transaction
            # argument: two separate statements would let a reader between them see a
            # ready-but-windowless row, which is exactly the state R-T15 refuses). A worker that
            # sends no coverage (older worker, empty fetch) writes NULL on both columns here too,
            # so the row stays refused by the tracker rather than silently reading as "full
            # history" -- there is no other, better window to fall back to, since the coverage IS
            # the ground truth this window is built from.
            # Night four lane S: store_id/store_fetched_at NULL when body.store_identity is None
            # (a non-eodhd fetch, or a worker built before this migration), matching the covered_*
            # NULL-on-absence idiom immediately above.
            (body.rows, body.sha256, body.file_path, body.file_bytes,
             body.covered_start_ts, body.covered_end_ts,
             body.covered_start_ts, body.covered_end_ts,
             body.store_identity.store_id if body.store_identity is not None else None,
             body.store_identity.store_fetched_at if body.store_identity is not None else None,
             int(time.time()), dataset_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404)

    # M5 U2 (AD-M5-13 phase-2 ready-trigger): this dataset just became 'ready'. Outside the txn above (the
    # broadcast pattern), finalize every materializing sweep this dataset completes — assemble the member list
    # + enqueue the run_sweep job, but ONLY when ALL of a sweep's members are now ready (so a multi-member
    # sweep enqueues exactly once, when its last member readies). Idempotent and off the event loop (to_thread).
    await asyncio.to_thread(researchdb.finalize_materializing_sweeps_for, dataset_id)

    # M5 Track C C-bench (§3 phase-2): if this dataset is a frozen BENCHMARK (not a sweep member, so the
    # finalize above does not cover it), pin its content sha onto the registrations that froze it — the
    # benchmark analogue of universe_sha256. Idempotent (fills only a NULL benchmark_sha256).
    await asyncio.to_thread(researchdb.freeze_benchmark_sha_on_ready, dataset_id)

    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "ready",
        "rows": body.rows, "sha256": body.sha256,
        "file_path": body.file_path, "file_bytes": body.file_bytes,
    })
    return {"ok": True}


@worker_router.post("/datasets/{dataset_id}/failed")
async def worker_dataset_failed(dataset_id: int, body: DatasetWorkerFailedIn):
    """Worker reports failed fetch. Auth enforced by worker_router dependency."""
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "UPDATE research_datasets "
            "   SET status='failed', last_error=?, last_fetched_at=? "
            " WHERE id=?;",
            (body.error, int(time.time()), dataset_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404)

    # M5 U2 (AD-M5-13 partial-materialize-failure, no-fallback): a member dataset failed to fetch. Outside the
    # txn above, fail every materializing sweep that has this dataset as a member (recording the failing
    # ticker), enqueuing NOTHING — never a silent subset over whoever happened to be ready. Off the event loop.
    await asyncio.to_thread(researchdb.fail_materializing_sweeps_for, dataset_id, body.error)

    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "failed", "last_error": body.error,
    })
    return {"ok": True}


@worker_router.post("/datasets/{dataset_id}/hard_deleted")
async def worker_dataset_hard_deleted(dataset_id: int):
    """Worker reports the parquet removed; write the cleaned terminal state (B.5, DECISIONS #16).

    Thin wrapper over researchdb.mark_hard_deleted (file_path NULL, file_bytes 0,
    sha256 NULL; status stays 'deleted'). Auth enforced by the worker_router dependency.
    Only called after a successful unlink — a failed unlink fails the job instead.
    """
    try:
        researchdb.mark_hard_deleted(dataset_id)
    except researchdb.DatasetNotFound:
        raise HTTPException(status_code=404, detail="dataset not found")

    # The dataset's chart preview (Brief B) is a derived, regenerable object; remove it now the
    # dataset is permanently gone. Runs after mark_hard_deleted (the row is the system of record and
    # is already clean) and is idempotent (a never-previewed dataset is a clean no-op), so it cannot
    # leave the row inconsistent. Server-side because the worker cannot reach this store (the same
    # asymmetry the preview transport exists for). NOT the M5 retention lever — that caps total
    # preview storage; this is per-dataset symmetry (a preview dies with its dataset).
    artifact_store.store.delete_dataset_preview(dataset_id, "preview.parquet")

    await broadcaster.publish("research.dataset_status_changed", {
        "id": dataset_id, "status": "deleted", "hard_deleted": True,
    })
    return {"ok": True}


# -------- Run worker endpoints (M3 brief E + I + K.0) --------

# The artifacts the server store must hold before a run may flip to 'succeeded'
# (K.0 §4.6). price.parquet (DECISIONS #23 P2) is mandatory — the entrypoint always writes it
# (every strategy runs on OHLCV) and RunExplorer's on-price overlay needs it. indicators.parquet
# is optional (spec §3.4: NULL if the strategy emitted none), so it is stored when sent but NOT
# gate-required. meta.json (C2.5 D2) is mandatory in BOTH sets — the entrypoint writes it
# unconditionally (§12) and it carries the regime bands + an event run's universe window/types the
# bundle reads, so a run must not finalize without it (the gap C2.5 closed). These are addressed by
# run_id + fixed filename, so they need no DB column.
_MANDATORY_ARTIFACTS = ("equity.parquet", "drawdowns.parquet", "trades.parquet", "price.parquet", "meta.json")
# An event run writes no price.parquet (it has no single base OHLCV frame; entrypoint §3 guard,
# DECISIONS #23 P2), so its mandatory set drops it. The other three parquets plus meta.json are
# still mandatory (the K.0 no-succeeded-without-bytes invariant holds for the artifacts an event
# run does produce; meta.json is exactly where its regime bands + window/types live).
_MANDATORY_ARTIFACTS_EVENT = ("equity.parquet", "drawdowns.parquet", "trades.parquet", "meta.json")


@worker_router.post("/runs/{run_id}/artifacts")
async def worker_run_artifacts(
    run_id: int,
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Worker uploads a succeeded run's parquet artifacts (brief K.0, called before /result).

    Each file is streamed into the ArtifactStore and verified against its declared sha256
    (store.put raises on mismatch). Idempotent: a re-upload overwrites. Touches no DB row;
    /result is what finalizes the run, and only after this has landed the mandatory parquets.
    """
    try:
        digests = json.loads(manifest)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="manifest must be JSON {filename: sha256}")
    if not isinstance(digests, dict):
        raise HTTPException(status_code=400, detail="manifest must be a JSON object")

    stored: list[str] = []
    for f in files:
        name = f.filename or ""
        sha = digests.get(name)
        if sha is None:
            raise HTTPException(status_code=400, detail=f"manifest missing sha256 for {name!r}")
        try:
            artifact_store.store.put(run_id, name, f.file, sha256=sha)
        except artifact_store.ArtifactIntegrityError as e:
            raise HTTPException(status_code=422, detail=f"artifact integrity check failed: {e}")
        except ValueError as e:  # illegal filename (path traversal guard in the store)
            raise HTTPException(status_code=400, detail=str(e))
        stored.append(name)
    return {"ok": True, "run_id": run_id, "stored": stored}


@worker_router.get("/runs/{run_id}/artifacts/{name}")
async def worker_run_artifact_download(run_id: int, name: str):
    """Stream a stored run artifact back to the worker (M5 Track C single-run DERIVE, C2 §2.1).

    The compute-on-demand worker fetches equity.parquet + trades.parquet over HTTP rather than depending on a
    stale local run_artifacts/ copy, so a run produced on any worker (or before a worker restart) still derives
    its full metrics. 400 on an illegal name (path-traversal guard, belt-and-suspenders over the store's own);
    404 when the artifact is not in the store (a purged run -> the route renders honest absence)."""
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="illegal artifact name")
    try:
        fh = artifact_store.store.open(run_id, name)
    except artifact_store.ArtifactNotFound:
        raise HTTPException(status_code=404, detail=f"artifact {name!r} not in store for run {run_id}")
    return StreamingResponse(fh, media_type="application/octet-stream")


class FullMetricsCacheIn(BaseModel):
    """The worker's computed full-metrics payload for the cache (M5 C-metrics §2.2)."""
    variation_id: int   # the cell's variation_id, or -1 for a single run (the sentinel)
    ticker: str         # the cell's ticker, or '' for a single run (the sentinel)
    formula_version: str  # the 3-part identity the route computed + echoed in the job payload
    payload: dict[str, Any]  # the full-metric set the worker computed


@worker_router.post("/runs/{run_id}/full-metrics-cache")
async def worker_full_metrics_cache(run_id: int, body: FullMetricsCacheIn):
    """The worker POSTs a computed full-metrics payload; the backend caches it immutably (M5 C-metrics §2.2).

    INSERT OR REPLACE on the (run_id, variation_id, ticker, formula_version) PK, so a retried compute is
    idempotent. The payload is stored verbatim (the worker is the sole computer); the route never computes.

    Lane JC (candidate chain link spec 2026-09-23, section 1.4 step 4 and the overwrite rule):
    routed through put_full_metrics_cache_guarded rather than the bare put, so a payload carrying
    a `replay` object is re-checked before it is ever written (409 replay-recheck-failed on any
    disagreement, no row written), and a PLAIN payload can never clobber a stored row that already
    carries a real replay object.
    """
    try:
        await asyncio.to_thread(researchdb.put_full_metrics_cache_guarded, run_id, body.variation_id,
                                body.ticker, body.formula_version, body.payload)
    except researchdb.ReplayRecheckFailed as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "run_id": run_id}


@worker_router.post("/datasets/{dataset_id}/preview")
async def worker_dataset_preview(
    dataset_id: int,
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Worker uploads a fetched dataset's full parquet as its chart preview (Brief B, R9).

    Mirrors worker_run_artifacts under a dataset-scoped key (store.put_dataset_preview): each file
    is streamed into the preview namespace and verified against its declared sha256 (raises on
    mismatch -> 422, illegal filename -> 400). Idempotent: a re-upload overwrites, so every fetch /
    re-fetch / update-to-latest refreshes the preview. Touches no DB row; the read endpoint
    (GET /datasets/{id}/preview) is what serves it.
    """
    try:
        digests = json.loads(manifest)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="manifest must be JSON {filename: sha256}")
    if not isinstance(digests, dict):
        raise HTTPException(status_code=400, detail="manifest must be a JSON object")

    stored: list[str] = []
    for f in files:
        name = f.filename or ""
        sha = digests.get(name)
        if sha is None:
            raise HTTPException(status_code=400, detail=f"manifest missing sha256 for {name!r}")
        try:
            artifact_store.store.put_dataset_preview(dataset_id, name, f.file, sha256=sha)
        except artifact_store.ArtifactIntegrityError as e:
            raise HTTPException(status_code=422, detail=f"preview integrity check failed: {e}")
        except ValueError as e:  # illegal filename (path traversal guard in the store)
            raise HTTPException(status_code=400, detail=str(e))
        stored.append(name)
    return {"ok": True, "dataset_id": dataset_id, "stored": stored}


@worker_router.post("/runs/{run_id}/result")
async def worker_run_result(run_id: int, body: RunResultIn):
    """Worker reports a succeeded run. Backend writes stats + finalizes the run (brief E).

    K.0 gate: refuse to flip the run to 'succeeded' unless the mandatory parquet artifacts are
    already in the server store (the invariant — no succeeded-without-bytes). The worker uploads
    via /artifacts first; a missing artifact means that upload did not land, so this returns 409
    and the run stays unfinalized (retryable). The presence check is enough — integrity was
    verified at write time by store.put (K.0 §4.5), so we do not re-hash here.
    """
    # Event runs produce no price.parquet (DECISIONS #23 P2 / entrypoint §3 guard), so the gate
    # must use the event-aware mandatory set or it would 409 forever on a legitimately-absent file.
    try:
        mandatory = _MANDATORY_ARTIFACTS_EVENT if researchdb.run_is_event(run_id) else _MANDATORY_ARTIFACTS
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    missing = [n for n in mandatory if not artifact_store.store.exists(run_id, n)]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"artifacts not in store for run {run_id}: {missing}; upload before /result",
        )
    # Unit-4 D2: compute the equity sparkline from the stored equity.parquet (present + integrity-
    # checked by the mandatory-artifact gate above) via the single artifact-layer helper, then hand
    # it to complete_run to store in the same finalize transaction as the stats. The read is once
    # per run at finalize, never on a list read (the materialized-view guarantee). A read failure
    # here is a genuine corrupt-artifact problem and fails loud (the result callback is retryable),
    # never papered over.
    sparkline = export.equity_sparkline_from_artifact(run_id)
    try:
        d = researchdb.complete_run(run_id, body.artifacts, body.stats, body.environment,
                                    equity_sparkline=sparkline, annualization=body.annualization,
                                    funnel=body.funnel, variation=body.variation)
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")

    # Lane T (night two, data-use tracker, R-T5/W8): record the bars this run actually
    # consumed, AFTER complete_run has committed the run's own funnel row, in its own
    # separate transaction (never inside complete_run itself: section 6.1 forbids changing
    # an existing researchdb.py function). draw_id_for_run resolves the draw via the run's
    # run_linked row (R-T14, the only path back); a run with none (an unlinked run, or one
    # dispatched before this gate existed) writes no observation, which is exactly the
    # runs_without_draw_link population (B17) and is never guessed at here.
    draw_id = tracker.draw_id_for_run(run_id)
    if draw_id is not None:
        with researchdb.get_conn() as _conn:
            bars_row = _conn.execute(
                "SELECT SUM(bars_total) AS total FROM research_run_funnel WHERE run_id = ?;",
                (run_id,),
            ).fetchone()
        # SUM over zero rows is a SQL NULL, not zero: a run with no funnel row at all (an
        # event/legacy/bespoke run, per complete_run's own docstring) has NO EVIDENCE of how
        # many bars it read, which is a different fact from "it read zero" (R-T6). Writing 0
        # here would fabricate a number this route never observed, so it is skipped instead.
        if bars_row is not None and bars_row["total"] is not None:
            tracker.record_bars_observed(draw_id, int(bars_row["total"]), int(time.time()))

    await broadcaster.publish("research.run_completed", {
        "run_id": d["run_id"], "job_id": d["job_id"], "finished_at": d["finished_at"],
        "status": "succeeded", "total_return": d["total_return"],
        "sharpe_ratio": d["sharpe_ratio"], "num_trades": d["num_trades"],
    })
    return {"ok": True}


@worker_router.post("/runs/{run_id}/failed")
async def worker_run_failed(run_id: int, body: RunFailedIn):
    """Worker reports a failed run. Backend flips the run to failed (brief E).

    Fold item 3 (round 1, BLOCKER-class U2/claims): a walk-forward container refuses BY NAME, in
    process, before any slice runs, when the strategy has not declared its own lookback_bars
    (run_sweep_entrypoint.py's own walkforward-lookback-undeclared ValueError). That refusal can
    only surface HERE: the container has no access to the backend's in-memory
    STAGE_RUNNER_MONITOR_COUNTERS dict (a separate process, and stage_runner.py itself may never
    import engine code, AC-10/SR-16), so this route recognises the named error text and counts it
    under the SAME walkforward_refusals_by_reason bucket every other walk-forward refusal already
    uses, rather than leaving this one reason silently uncounted.

    Round-3 fold (n7-WF-4a1c, the lead's read): the SAME container also refuses, by the separate
    name walkforward-window-too-small-after-buffer, when a window that cleared stage_runner.py's
    own pre-draw floor (in_sample_bars + 2*out_of_sample_bars at ZERO buffer, the earliest check
    that can run) turns out too small once the REAL purge/embargo buffer is known -- that buffer
    cannot be computed before the grid is read, so it is only ever discovered here, in the
    container. This reason is counted under the EXISTING window_too_small bucket
    (stage_runner.py's own pre-draw WindowTooSmallForScheme uses the same key), never a new one,
    since both name the same underlying fact: the window is too small for the scheme.

    Round-4 fold (n7-WF-4a1c), code-quality pass 1 finding B1(a): the container also refuses, by
    the name walkforward-no-out-of-sample-leg, when the chained out-of-sample curve came out empty
    or non-finite (so total_return/max_drawdown would otherwise be NULL against the NOT NULL
    research_run_stats columns). Counted under a NEW bucket, no_out_of_sample_leg, since this is a
    genuinely distinct failure mode from a window or a parameter refusal: the window and the grid
    were both fine, the run simply produced nothing usable to report.

    Lane WV (AC-12, `#417`/`#420`, Amendment 1): a FOURTH named refusal,
    walkforward-declared-params-unread, fires when a generated strategy declares sweep knobs in
    `sweep_params` but its own `run()` never reads one of them through `self.param(...)` -- the
    walk-forward engine injects each combo's value via that call, so an unread name would silently
    replay the identical backtest for every combo in the grid rather than sweeping it. This was
    already loud on the failed run itself (the stored `error` text), but before this lane it fell
    through every `if` below uncounted, so its own failure mode had a name but no counter. Bucketed
    under its OWN reason key, never folded into `lookback_undeclared` (a different root cause: a
    knob the strategy never declared at all, not one it declared and then ignored).
    """
    try:
        d = researchdb.fail_run(run_id, body.error)
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")

    if "walkforward-lookback-undeclared" in body.error:
        stage_runner._record_walkforward_refusal("lookback_undeclared")
    if "walkforward-window-too-small-after-buffer" in body.error:
        stage_runner._record_walkforward_refusal("window_too_small")
    if "walkforward-no-out-of-sample-leg" in body.error:
        stage_runner._record_walkforward_refusal("no_out_of_sample_leg")
    if "walkforward-declared-params-unread" in body.error:
        stage_runner._record_walkforward_refusal("declared_params_unread")

    await broadcaster.publish("research.run_failed", {
        "run_id": d["run_id"], "job_id": d["job_id"], "finished_at": d["finished_at"],
        "status": "failed", "error": d["error"],
    })
    return {"ok": True}


@worker_router.post("/runs/{run_id}/cancelled")
async def worker_run_cancelled(run_id: int, body: RunCancelledIn):
    """Worker reports a cancelled run after hard-killing the container (brief I, EI-1).

    The worker has no DB; the backend does the UPDATE (status from the body, artifact paths
    NULL, finished_at set, error NULL). The body's status is used, not hardcoded, so M5/M6
    'cancelled_partial' reuse this endpoint unchanged.
    """
    try:
        d = researchdb.cancel_run(run_id, body.status)
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")

    await broadcaster.publish("research.run_cancelled", {
        "run_id": d["run_id"], "job_id": d["job_id"], "finished_at": d["finished_at"],
        "status": d["status"],
    })
    return {"ok": True}


class TheoryFamilyIn(BaseModel):
    """Lane GV2-7b: the body of POST /runs/{run_id}/theory-family, the search's family.json. Every field of the table is required
    (a field that can be null is still required, so a missing one is a 422); extra keys of family.json are ignored."""
    theory_name: str
    group_label: str
    member_set_sha256: str
    window_start: str          # YYYY-MM-DD, both ends included; the key's window_start_ts is this day at 00:00 UTC
    window_end: str
    n_trials: int
    n_in_onc: int | None
    k_effective: int | None
    onc_cap: int | None
    cap_bound: bool | None
    v: float | None
    selected_frequency: float
    reason: str | None
    trials_sha256: str
    returns_sha256: str


@worker_router.post("/runs/{run_id}/theory-family")
async def worker_theory_family(run_id: int, body: TheoryFamilyIn):
    """Lane GV2-7b (DECISIONS #445): the worker records the trial family one theory search made. 404 for an unknown run; 409 when the
    run already has its row (the answer names that row, so a repeat POST after a restart is harmless); a second theory on the same
    key is recorded too and enqueues the merge job (GV3-FU2, call 37); 422 for a run that is not a theory search, a bad date, or numbers
    that disagree with the reason."""
    try:
        return await asyncio.to_thread(researchdb.record_trial_family, run_id, body.model_dump())
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except researchdb.TrialFamilyAlreadyRecorded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class FamilyUnionIn(BaseModel):
    """Lane GV3-FU2: the body of POST /family-unions, GV3-FU1's union.json plus job_id. Every field of the table is required (a field
    that can be null is still required); extra keys of union.json (onc_cap_asked, seed, selected_frequency) are ignored."""
    job_id: int
    run_ids: list[int]
    member_set_sha256: str
    window_start: str          # YYYY-MM-DD, both ends included
    window_end: str
    n_trials: int
    n_in_onc: int | None
    k_effective: int | None
    onc_cap: int | None        # family_summary's own cap (null under too-few-trials), not the cap the job asked for
    cap_bound: bool | None
    v: float | None
    reason: str | None


@worker_router.post("/family-unions")
async def worker_family_union(body: FamilyUnionIn):
    """Lane GV3-FU2 (call 37): the worker records the merged trial family of a run_family_union job. 404 for a job that is not a
    run_family_union job; 409 when the job already has its row (the answer names it); 422 when the run ids are not the job's, the member
    set or window differs, a date is bad, or the numbers disagree with the reason."""
    data = body.model_dump()
    job_id = data.pop("job_id")
    try:
        return await asyncio.to_thread(researchdb.record_family_union, job_id, data)
    except researchdb.FamilyUnionJobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except researchdb.FamilyUnionAlreadyRecorded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@worker_router.get("/runs/{run_id}/committed-cells")
async def worker_sweep_committed_cells(run_id: int):
    """M5 1B (§6.2): the sweep's already-committed (variation_id, ticker) cells — the resume skip-set.

    The worker reads this before driving a sweep so the container does not recompute committed cells; the
    backend idempotence gate is the correctness backstop regardless. Empty on a fresh sweep.
    """
    return {"cells": researchdb.get_sweep_committed_cells(run_id)}


@worker_router.post("/runs/{run_id}/sweep-chunk")
async def worker_sweep_chunk(run_id: int, body: SweepChunkIn):
    """M5 1B (§6.1): write one sweep chunk atomically + exactly-once. Returns {written, skipped, done, total}.

    complete_sweep_chunk opens one transaction, skips cells already in the cube (the exactly-once gate),
    writes the trial+cell+funnel+variation for the rest, and advances progress_json. A 404 for an unknown
    run; a 409 if the sweep has no frozen registration (R-PREREG — 1C must register before the engine writes).
    """
    try:
        d = researchdb.complete_sweep_chunk(run_id, body.cells)
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as e:  # no sweep registration (R-PREREG) — the engine must not write unregistered cells
        raise HTTPException(status_code=409, detail=str(e))
    return d


@worker_router.post("/runs/{run_id}/cell-refusals")
async def worker_cell_refusals(run_id: int, body: CellRefusalsIn):
    """Lane D1: record refused sweep cells (sizing refused one (combo, ticker); the sweep carried on).
    Returns {written, skipped}; 404 for an unknown run, 422 for a refusal missing a key."""
    try:
        return await asyncio.to_thread(researchdb.record_cell_refusals, run_id, body.refusals)
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@worker_router.post("/runs/{run_id}/sweep-result")
async def worker_sweep_result(run_id: int, body: SweepResultIn):
    """M5 1B (§7): finalize a succeeded sweep run (status + run-level annualization; no stats row).

    Lane GV2-8b (DECISIONS #459 item 2): a body that carries bars_observed (a theory search or walk-forward run, which writes no funnel
    rows) records that count for the run's draw. It is refused with 422 when the run has funnel rows (the sum over them is the count)
    and with 409 when the draw already holds a bars_observed row, both before the run is finalised.
    """
    if body.bars_observed is not None:
        with researchdb.get_conn() as _conn:
            funnel = _conn.execute("SELECT 1 FROM research_run_funnel WHERE run_id = ? LIMIT 1;", (run_id,)).fetchone()
        if funnel is not None:
            raise HTTPException(status_code=422, detail=f"run {run_id} has funnel rows; its bars observed come from them")
        given_draw = tracker.draw_id_for_run(run_id)
        if given_draw is not None:
            with researchdb.get_conn() as _conn:
                seen = _conn.execute(
                    "SELECT 1 FROM research_data_draws WHERE event_kind = 'bars_observed' AND draw_id = ? LIMIT 1;", (given_draw,),
                ).fetchone()
            if seen is not None:
                raise HTTPException(status_code=409, detail=f"draw {given_draw} already records bars observed")
    try:
        d = researchdb.complete_sweep(run_id, annualization=body.annualization, environment=body.environment)
    except researchdb.RunNotFound:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as e:  # a partial annualization dict (the fail-loud guard)
        raise HTTPException(status_code=422, detail=str(e))

    # Fold finding 1 (docs/units/T/reads/read_1.md, observation-unwired-on-main-path): a sweep
    # run -- the milestone's OWN dispatch path -- used to get NO bars_observed row at all,
    # because this accounting only ever ran from worker_run_result (the single-backtest site).
    # Wired here, after complete_sweep has committed the run's own finalize transaction, in its
    # own SEPARATE transaction (never inside complete_sweep itself: section 6.1 forbids editing
    # an existing researchdb.py function), mirroring worker_run_result's own comment and shape
    # exactly. draw_id_for_run resolves the draw via the run's run_linked row; a run with none
    # writes no observation, which is exactly the runs_without_draw_link population (B17) and is
    # never guessed at here. SUM(bars_total) is read across EVERY funnel row this run's chunks
    # wrote (the sweep grain is one funnel row per (run_id, variation_id, ticker, row_scope),
    # never one row per run, unlike a single backtest's exactly-one funnel row), which is the
    # only place a sweep's total bars-read can honestly be totalled.
    draw_id = tracker.draw_id_for_run(run_id)
    if draw_id is not None and body.bars_observed is not None:
        tracker.record_bars_observed(draw_id, body.bars_observed, int(time.time()))  # GV2-8b: the worker's count; zero stays zero
    elif draw_id is not None:
        with researchdb.get_conn() as _conn:
            bars_row = _conn.execute(
                "SELECT SUM(bars_total) AS total FROM research_run_funnel WHERE run_id = ?;",
                (run_id,),
            ).fetchone()
        # Same NULL-sum rule as the single-run site (R-T6): SUM over zero funnel rows is a SQL
        # NULL, a different fact from an observed zero, so the write is skipped rather than
        # fabricating a 0 this route never actually measured (an event/bespoke sweep, if one
        # ever exists, would otherwise get a false zero here).
        if bars_row is not None and bars_row["total"] is not None:
            tracker.record_bars_observed(draw_id, int(bars_row["total"]), int(time.time()))

    # A sweep has no single run-level metric tuple, so the SSE carries the terminal status only (the cube
    # cells are read per-cell by the Phase-2 grid); total_return/sharpe/num_trades are honestly null here.
    await broadcaster.publish("research.run_completed", {
        "run_id": d["run_id"], "job_id": d["job_id"], "finished_at": d["finished_at"],
        "status": "succeeded", "total_return": None, "sharpe_ratio": None, "num_trades": None,
    })
    return {"ok": True}


@worker_router.post("/runs/{run_id}/walkforward-result")
async def worker_walkforward_result(run_id: int, body: WalkforwardResultIn):
    """Lane WF (6.2b, AC-19): the walk-forward relay, POSTed once after sweep-result finalizes the
    run's own status. Writes every slice and the run-level figures through record_walkforward_result
    (round 4 fold, n7-WF-4a1c, code-quality pass 1 finding B1(b): ONE transaction for both, so a
    refused stats write leaves no half-written record; before this fold the two writes ran as
    separate transactions and a refusal on the second one left the slice rows committed with no
    matching research_run_stats row, invisible to query_runs_missing_walkforward_record). Both
    writers treat run_id as a SOFT reference (the table's own DDL comment: "a run row always exists
    first, this table is additive-only"), matching research_walkforward_slices's own schema
    decision, so neither raises for an unknown run_id; the sweep-result route above is what already
    404s an unknown run before this route would ever be reached in the real relay's own call order.

    Fold item 2b (round 1, BLOCKER LC2 / the lead's read): after the stats write, set
    research_runs.equity_path to this run's own conventional path, gated on the artifact ACTUALLY
    being in the server store (the same K.0 exists() gate /result uses above) -- the worker uploads
    equity.parquet via /artifacts before this call lands, so a run with zero usable slices (which
    never uploads a file) correctly leaves equity_path NULL rather than pointing at absent bytes.
    """
    try:
        researchdb.record_walkforward_result(run_id, body.slices, {
            "in_sample_profit": body.in_sample_profit, "out_of_sample_profit": body.out_of_sample_profit,
            "wfa_boundary_buffer_bars": body.wfa_boundary_buffer_bars, "total_return": body.total_return,
            "max_drawdown": body.max_drawdown, "num_trades": body.num_trades,
            "total_fees_paid": body.total_fees_paid, "total_slippage_cost": body.total_slippage_cost,
        })
    except ValueError as exc:  # B1(b): the NOT NULL trio came in incomplete (walkforward-run-stats-incomplete)
        raise HTTPException(422, str(exc)) from exc
    if artifact_store.store.exists(run_id, "equity.parquet"):
        researchdb.record_run_equity_path(run_id)
    return {"ok": True, "slices_written": len(body.slices)}


# M2.5 brief J — §6.12.1 cross-process notification endpoint.
# Fetcher process POSTs here; the dashboard re-broadcasts on the in-process
# SSE channel after prefixing the event name with 'research.' in exactly one
# place. Auth via the router-level X-Worker-API-Key dependency (V1).

# Four-value enum from spec brief §6.12.2. Enforced as an in-handler check
# (not a Pydantic Literal) so an unknown value returns 400 with
# {"error": "unknown_kind", "kind": <echoed>} for an operator-friendly
# diagnostic, instead of Pydantic's 422 with the {"detail": [...]} shape.
# Locked by the probe spec design fork.
_NOTIFY_ALLOWED_KINDS = frozenset({
    "event_batch_created",
    "event_batch_progress",
    "event_batch_completed",
    "event_catalog_refreshed",
})


class _EventsNotifyBody(BaseModel):
    """POST /worker/events/notify request body (spec brief §6.12.1).

    kind is plain str (validated in-handler against _NOTIFY_ALLOWED_KINDS) so
    unknown values return 400 with the locked echoed shape. payload is a
    free-form dict — the endpoint is a pure pipe (auth + prefix-adding) and
    does not reshape it; any payload-shape contract lives on the caller.
    Per §6.12.2 RN4/M9 `kind` is the unprefixed stem; the handler prefixes
    with 'research.' once before calling broadcaster.publish.
    """
    kind: str
    payload: dict[str, Any]


@worker_router.post("/events/notify")
async def worker_events_notify(body: _EventsNotifyBody):
    """Cross-process notify → SSE re-broadcast (spec brief §6.12.1).

    The fetcher script POSTs here at each emission point (V4); the in-
    process loader broadcasts directly per DECISIONS #41. Both paths
    converge on broadcaster.publish so the on-the-wire event format is
    uniform across producers (the broadcaster prefixes with 'research.'
    in exactly one place — see V2 / sse.py:40).

    No DB work; only side effect is the broadcaster.publish call.

    Status mapping:
      204 — broadcast accepted; no body
      400 unknown_kind — kind not in the four-value enum (§6.12.2)
      400 payload_contains_reserved_type_key — payload would silently
           overwrite the broadcaster's `type` field on the fan-out
           message (sse.py:40 builds `{"type": event_type, **payload}`);
           rejecting at the door keeps the wire-format invariant
           subscribers depend on
      401 — router-level dependency rejected the worker auth header
      422 — Pydantic schema mismatch (missing field, wrong type)
    """
    # 1. Four-value enum check on the unprefixed stem.
    if body.kind not in _NOTIFY_ALLOWED_KINDS:
        return JSONResponse(
            status_code=400,
            content={"error": "unknown_kind", "kind": body.kind},
        )
    # 2. Reserved-key check on payload. A payload-carried `type` would shadow
    # the broadcaster's prefixed event name (subscribers would see SSE
    # `event:` and the `type` field of `data:` disagree); reject before any
    # broadcast happens so no inconsistent message ever leaves this process.
    if "type" in body.payload:
        return JSONResponse(
            status_code=400,
            content={
                "error": "payload_contains_reserved_type_key",
                "kind": body.kind,
            },
        )
    # 3. Re-broadcast with the 'research.' prefix added in exactly one place.
    await broadcaster.publish(f"research.{body.kind}", body.payload)
    return Response(status_code=204)


# M2.5 brief K — §6.10 container-boundary materializer (worker-only).
# Reads events.db, builds a denormalized per-(event,cik) parquet via the §8.4
# output-schema registry, returns the bytes + four headers. The M3.5 backtest
# container mounts the parquet read-only (EL44). DDIA Ch 12: events.db is the
# single source of truth; the parquet is a re-derived follower (read-only here).

class _MaterializeBody(BaseModel):
    """POST /worker/events/materialize request body (spec brief §6.10).

    event_type / ciks are validated in-handler (not via Pydantic) so they return
    the §6.10-named 400 shapes (`unsupported_event_type` / `ciks_empty`) instead
    of Pydantic's 422 — the J notify endpoint set this in-handler-400 precedent.
    include_status is a Pydantic Literal list (a malformed value is a worker-code
    bug → 422; §6.10 names no include_status 400) with a non-empty default.
    """
    event_type: str
    ciks: list[int]
    knowledge_from: int
    knowledge_to: int
    include_status: list[Literal["active", "rolled_back"]] = Field(
        default_factory=lambda: ["active"], min_length=1)
    # M3.5 brief B (EL65, DECISIONS #54): an event run binds the resolved universe so
    # the materializer can stamp the per-event membership_valid flag. Absent for M2.5
    # callers (and the brief-K/N probes) -> the membership join matches nothing ->
    # membership_valid is NULL on every row. Optional, so existing callers are unchanged.
    universe_id: Optional[int] = None


@worker_router.post("/events/materialize")
async def worker_events_materialize(body: _MaterializeBody):
    """Materialize one (event_type, cik-universe, window) to a parquet stream (§6.10).

    Status mapping:
      200 — parquet body + X-Event-Count / X-Content-SHA256 / X-Source-Series /
            Content-Disposition (§6.10)
      400 unsupported_event_type — event_type not in the §8.4 registry
      400 ciks_empty — empty ciks array
      401 — router-level worker-auth rejection (require_worker_api_key)
      422 — Pydantic schema mismatch (missing field; bad include_status value)
      500 {"error":"<class_snake_case>"} — any unexpected engine/pyarrow path
    """
    # 1. Registry membership → the §6.10 400 (echoes the offending type), not 422.
    if body.event_type not in event_output_schemas.OUTPUT_SCHEMAS:
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported_event_type", "event_type": body.event_type},
        )
    # 2. Empty universe → 400 (json_each('[]') would otherwise 0-row silently).
    if not body.ciks:
        return JSONResponse(status_code=400, content={"error": "ciks_empty"})

    # 3. Sync SQLite + pyarrow build off the event loop (the loader/to_thread precedent).
    try:
        result = await asyncio.to_thread(
            event_materializer.materialize_event_slice,
            event_type=body.event_type,
            ciks=body.ciks,
            knowledge_from=body.knowledge_from,
            knowledge_to=body.knowledge_to,
            include_status=body.include_status,
            universe_id=body.universe_id,                  # EL65: None for M2.5 callers -> NULL membership flag
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberate broad catch: §6.10 mandates 500 {"error":"<class_snake_case>"}
        # for any unexpected shared-core/pyarrow path, and turning the unknown
        # class name into that body requires catching it. Same contract-driven
        # exception to CLAUDE.md's catch-all rule the brief-E loader 500-mapper
        # uses (routes/research.py:1072-1081). The named input failures
        # (unsupported_event_type, ciks_empty, the 422s) are handled above.
        return JSONResponse(status_code=500, content={"error": _snake_case(type(exc).__name__)})

    # 4. 200 with the parquet body + the four §6.10 headers.
    filename = f"{body.event_type}_{body.knowledge_from}_{body.knowledge_to}.parquet"
    return Response(
        content=result.parquet_bytes,
        media_type="application/octet-stream",
        headers={
            "X-Event-Count": str(result.event_count),
            "X-Content-SHA256": result.content_sha256,
            "X-Source-Series": result.source_series,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# M3.75 FUND-2 — §6.2 container-boundary fundamentals materializer (worker-only).
# Reads events.db, builds the denormalized fact-table parquet slice via
# fundamentals_materializer.materialize_fundamentals_slice (§6.1), returns the bytes + two headers.
# The backtest container mounts the parquet read-only at /data/fundamentals/facts (§6.2) and
# recomputes the as-of join over it (§6.3). A SEPARATE route from /events/materialize (not a
# discriminator on it) because the body + output schema differ: no event_type, a concept set, a
# fact-table column set (arch §11 fork settled in §6.2). Same single-source-of-truth as events.db:
# the parquet is a re-derived follower (DDIA Ch 1; read-only here).

class _FundamentalsMaterializeBody(BaseModel):
    """POST /worker/fundamentals/materialize request body (§6.2).

    `ciks` is validated in-handler (not via Pydantic) so it returns the §6.2-named 400 `ciks_empty`
    shape (mirroring /events/materialize) rather than Pydantic's 422. `concepts` is optional and
    defaults empty -> "all four core concepts" (the materializer's `concepts=()` semantics, §6.1); a
    non-empty list restricts the slice to those concept names.
    """
    ciks: list[int]
    knowledge_from: int
    knowledge_to: int
    concepts: list[str] = Field(default_factory=list)  # empty -> all FUNDAMENTAL_CONCEPTS (§6.1)


@worker_router.post("/fundamentals/materialize")
async def worker_fundamentals_materialize(body: _FundamentalsMaterializeBody):
    """Materialize the fundamentals fact-table slice for (ciks, window, concepts) to parquet (§6.2).

    Status mapping (mirrors /events/materialize):
      200 — parquet body + X-Fact-Count / X-Content-SHA256 / Content-Disposition
      400 ciks_empty — empty ciks array (json_each('[]') would otherwise 0-row silently)
      401 — router-level worker-auth rejection (require_worker_api_key)
      422 — Pydantic schema mismatch (missing field; bad type)
      500 {"error":"<class_snake_case>"} — any unexpected engine/pyarrow path
    """
    # 1. Empty universe -> the §6.2-named 400, not 422 (the /events/materialize ciks_empty precedent).
    if not body.ciks:
        return JSONResponse(status_code=400, content={"error": "ciks_empty"})

    # 2. Sync SQLite + pyarrow build off the event loop (the materialize/to_thread precedent).
    try:
        result = await asyncio.to_thread(
            fundamentals_materializer.materialize_fundamentals_slice,
            ciks=body.ciks,
            knowledge_from=body.knowledge_from,
            knowledge_to=body.knowledge_to,
            concepts=tuple(body.concepts),                 # empty tuple -> all core concepts (§6.1)
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberate broad catch: §6.2 mandates 500 {"error":"<class_snake_case>"} for any unexpected
        # engine/pyarrow path, the SAME contract-driven CLAUDE.md carve-out the /events/materialize
        # route documents (turning an unknown class name into the mandated 500 body needs catching it).
        # The named input failure (ciks_empty) is handled above; the 422s are Pydantic.
        return JSONResponse(status_code=500, content={"error": _snake_case(type(exc).__name__)})

    # 3. 200 with the fact-table parquet body + the two §6.2 headers + Content-Disposition.
    filename = f"fundamentals_{body.knowledge_from}_{body.knowledge_to}.parquet"
    return Response(
        content=result.parquet_bytes,
        media_type="application/octet-stream",
        headers={
            "X-Fact-Count": str(result.fact_count),
            "X-Content-SHA256": result.content_sha256,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


class _FundamentalScreenSpec(BaseModel):
    """The optional fundamental screen on POST /worker/universes/resolve (M3.75 FUND-4 §9).

    Keep only the resolved CIKs whose as-known-on-D `concept` value satisfies `op threshold` at the
    window's no-lookahead horizon (the calendar-keyed as-of join, §5.2). The strategy supplies the
    declaration (read off the source at dispatch, the analogue of event_types) and the worker passes
    it. `op` is the closed comparator set (a Literal -> a clean Pydantic 422 on a bad operator,
    defense in depth; screen_ciks_by_fundamental re-validates op AND the concept's vocabulary
    membership and fails loud)."""
    concept: str = Field(..., description="the fundamental to screen on (a vocabulary name)")
    op: Literal[">", ">=", "<", "<="] = Field(..., description="the comparator (closed set, §9)")
    threshold: float = Field(..., description="the value the as-of fundamental is compared against")


class _ResolveUniverseBody(BaseModel):
    """POST /worker/universes/resolve request body (spec brief §4, EL63).

    The window bounds are Optional so a missing bound returns the §4-named
    422 {"error": "window_required"} from the handler rather than Pydantic's
    default 422 body — the materialize route set the in-handler-validation
    precedent. universe_id stays required (a missing id is a worker-code bug
    → Pydantic 422; §4 names no missing-id error). `fundamental_screen` is the
    optional FUND-4 §9 post-filter — absent for every price/event run, so those
    resolutions are byte-unchanged (NFR-6).
    """
    universe_id: int
    knowledge_from: Optional[int] = None
    knowledge_to: Optional[int] = None
    fundamental_screen: Optional[_FundamentalScreenSpec] = None
    members_as_of: Optional[int] = None  # DL1: a price universe's list as of this unix second; echoed back


@worker_router.post("/universes/resolve")
async def worker_universes_resolve(body: _ResolveUniverseBody):
    """Resolve (universe_id, window) to the UNION-over-window CIK list (§4, EL63).

    The spec instantiation of 2a §7's "the worker resolves the data declaration
    into slices": the worker (laptop) cannot read events.db, so this server-side
    read turns a universe id + window into the explicit `ciks` list the
    materializer's locked parameter takes.

    Status mapping:
      200 — {universe_id, knowledge_from, knowledge_to, ciks, member_count}
      404 universe_not_found — no research_datasets row (dataset_type='universe') with that id
      422 window_required — a window bound missing, not a positive integer, or from >= to
      401 — router-level worker-auth rejection (require_worker_api_key)

    Read-only: opens no batch row, writes nothing. An empty union (no member
    overlaps the window) returns ciks=[] with member_count=0 — a valid response,
    not an error (the §8 wiring short-circuits it before the materializer, which
    would otherwise 400 ciks_empty).
    """
    # 1. Window validity (§4) first — cheap, no DB. A missing/non-positive bound
    #    or from >= to → the §4-named 422, not a raw Pydantic 422.
    kf, kt = body.knowledge_from, body.knowledge_to
    if kf is None or kt is None or kf <= 0 or kt <= 0 or kf >= kt:
        return JSONResponse(status_code=422, content={"error": "window_required"})
    # 2. Universe existence → 404 (echoes the offending id, like materialize's 400s).
    #    Sync SQLite off the event loop (the materialize/to_thread precedent).
    if not await asyncio.to_thread(event_universe.universe_exists, body.universe_id):
        return JSONResponse(
            status_code=404,
            content={"error": "universe_not_found", "universe_id": body.universe_id},
        )
    # 3. The union-over-window read (§3.1 step 1), also off the event loop.
    ciks = await asyncio.to_thread(
        event_universe.resolve_universe_ciks,
        universe_id=body.universe_id,
        knowledge_from=kf,
        knowledge_to=kt,
        members_as_of=body.members_as_of,
    )
    # 3b. Optional fundamental screen (FUND-4 §9): keep only the resolved CIKs whose as-known-on-D
    #     `concept` value satisfies `op threshold`. The screen is events.db-local (the SAME store
    #     the membership read used), one indexed point lookup per candidate — never the cross-DB
    #     join DECISIONS #86 found un-runnable. The decision date D is the window's exclusive upper
    #     bound `kt` (the run's no-lookahead knowledge horizon): read_asof reads only filings public
    #     by D (`filing_date <= D`), so the screen introduces NO lookahead beyond the already-flagged
    #     static-membership limitation (DECISIONS #54; the screen neither fixes nor worsens it, §9
    #     R17 honesty). A candidate with no as-of fundamental is excluded (Q6). Off the event loop
    #     (sync SQLite), symmetric with the resolve read above. Absent for every existing run
    #     (NFR-6); the union list is returned unchanged when no screen is supplied.
    if body.fundamental_screen is not None:
        fs = body.fundamental_screen
        ciks = await asyncio.to_thread(
            fundamentals_screen.screen_ciks_by_fundamental,
            ciks=ciks, membership_date=kt, concept=fs.concept, op=fs.op, threshold=fs.threshold,
        )
    # 4. 200 with the (possibly screened) union list and its count (empty union → ciks=[], count 0).
    resolved = {
        "universe_id": body.universe_id,
        "knowledge_from": kf,
        "knowledge_to": kt,
        "ciks": ciks,
        "member_count": len(ciks),
    }
    if body.members_as_of is not None:  # DL1: echoed so the worker can tell this server read the dated list
        resolved["members_as_of"] = body.members_as_of
    return resolved


class _ResolvePriceSlicesBody(BaseModel):
    """POST /worker/price-slices/resolve request body (M3.5 price-provisioning brief, §3.1).

    `symbols` is the worker's distinct resolved trade_time_symbols read off the staged event slices;
    a missing/empty list is a worker-code bug -> Pydantic 422. Daily is the locked M3.5 granularity,
    so there is no timeframe field (a knob with no consumer would be a latent placeholder).
    """
    symbols: list[str] = Field(..., min_length=1)


@worker_router.post("/price-slices/resolve")
async def worker_price_slices_resolve(body: _ResolvePriceSlicesBody):
    """Resolve each symbol to its ready daily price dataset's container path (price brief §3.1, EL70).

    The worker (laptop) cannot read research.db, so this server-side read turns a symbol list into the
    {resolved: {symbol: file_path}, unresolved: [...]} map the worker mounts from. Read-only: opens no
    batch row, writes nothing. A symbol with no ready dataset_type='price_dataset' daily row goes to
    `unresolved` — a valid response, not an error (the worker mounts none and the event rides the
    price-coverage figure, never a silent skip). 401 is the router-level worker-auth rejection.

    Sync SQLite off the event loop (the materialize / universes-resolve to_thread precedent).
    """
    return await asyncio.to_thread(researchdb.resolve_price_slices, body.symbols)
