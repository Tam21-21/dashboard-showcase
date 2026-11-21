"""The quantile pre-check, moved here from dashboard/backend/routes/research.py (lane CN, set
16c, `#427`, spec finding F-4: "the quantile pre-check lives in the ROUTE, not in dispatch_stage
... the build lane moves it to a module both callers import rather than copying it").

Why this module exists: the chain runner (dashboard/backend/chain_runner.py) must run the exact
same pre-draw quantile refusal the stage-runner HTTP route already runs (routes/research.py's
`dispatch_stage_route`), so that a chain dispatched straight through `stage_runner.dispatch_stage`
(never through that HTTP route) still refuses a bad grid BEFORE any draw is requested, matching
spec section 1.1 step 3. Two callers, one function: this is a MOVE, not a copy, so the two can
never quietly drift apart (lane DV's own #424 founding case, run 50's grid of raw values 10 to 80
instead of fractions, is exactly the bug a second, drifted copy could reintroduce).

Every name below is re-exported, unchanged, from dashboard/backend/routes/research.py, so any
existing probe or document that reaches it as `routes.research.<name>` (a plain Python import
binds the SAME object, so a shared counter dict still increments as one shared count) keeps
working without edit.
"""
from __future__ import annotations

import ast  # reads a strategy's own baked quantile constants from stored TEXT, never imports or executes it (matches stage_runner._read_quantile_min_bars's own OPEN-4 discipline)

# Lane DV (set 16b, `#424`, docs/units/DV/criteria.md AC-4): a generated strategy's entry_quantile
# or exit_quantile is a FRACTION (a quantile), never a raw threshold like 10 or 80 -- run 50's own
# grid used values 10 to 80, which is why it crashed instead of refusing cleanly beforehand.
class QuantileOutOfRange(RuntimeError):
    """Raised before any draw is requested (AC-4): an entry_quantile or exit_quantile value is not
    strictly between 0 and 1. The message starts with the reason word `quantile_out_of_range`, the
    same "<reason_word>: ..." shape frozen_sweep_parameter/undeclared_sweep_parameter/
    sweep_grid_too_large/price_universe_empty already use in routes/research.py, and names both
    the offending parameter and its value so a caller can fix the request without re-deriving
    either from a stack trace."""

    def __init__(self, param_name: str, value: object) -> None:
        self.param_name = param_name  # "entry_quantile" or "exit_quantile", named for the caller
        self.value = value  # the offending value itself, embedded so the message is self-contained
        # Lane RM (set 16b, plan amendment 7, AC-4f): a value that is not a number at all (a
        # string like 'abc') and a value that IS a number but outside (0, 1) are two different
        # mistakes a caller can make; naming which one it was is more useful than one shared
        # sentence for both, and reads honestly either way (never a guess about the caller's
        # intent).
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            reason = "must be strictly between 0 and 1 (a quantile is a fraction of the sample, never a raw threshold)"
        else:
            reason = "is not a valid number at all (a quantile must be a float strictly between 0 and 1)"
        super().__init__(
            f"quantile_out_of_range: {param_name}={value!r} {reason}; refusing before any draw is requested"
        )


# The one counter both quantile pre-checks below share (AC-7): a refusal here fires BEFORE any
# draw is ever requested, so there is no durable research_data_draws row this signal could query
# instead, matching LEAD_IN_PRECHECK_MONITOR_COUNTERS's own established idiom for the same reason.
QUANTILE_PRECHECK_MONITOR_COUNTERS: dict[str, int] = {"quantile_precheck_refusals": 0}

# The two quantile parameter names AC-4 names, restated here as a single tuple so both the
# grid-shaped check (the sweep and stage-runner routes, and now the chain runner) and the
# baked-constant check (the single-run route) name them identically.
_QUANTILE_PARAM_NAMES = ("entry_quantile", "exit_quantile")

# The single-run route's own baked constant names (research_lab/generator/emit.py's own template),
# mapped to the PARAMETER name they carry, mirroring stage_runner._read_quantile_min_bars's own
# two-name mapping for a different baked constant.
_BAKED_QUANTILE_CONSTANTS = {
    "DEFAULT_ENTRY_QUANTILE": "entry_quantile",
    "DEFAULT_EXIT_QUANTILE": "exit_quantile",
}


def _quantile_in_range(value: object) -> bool:
    """True only for a real number (never a bool, which is an int subclass) strictly between 0
    and 1, matching AC-4's own words ("not strictly between 0 and 1")."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1


def _refuse_bad_quantiles_in_grid(param_grid: dict) -> None:
    """AC-4, the sweep-shaped callers (the universe sweep route, the stage-runner dispatch route,
    and the chain runner's own direct stage_runner.dispatch_stage call): refuse the FIRST
    out-of-range entry_quantile/exit_quantile value found in `param_grid`, before any draw is ever
    requested. Only the two named quantile keys are checked; every other grid key is a different
    check's business (the frozen/undeclared/too-large refusals in routes/research.py)."""
    for param_name in _QUANTILE_PARAM_NAMES:
        for value in param_grid.get(param_name, []):
            if not _quantile_in_range(value):
                QUANTILE_PRECHECK_MONITOR_COUNTERS["quantile_precheck_refusals"] += 1
                raise QuantileOutOfRange(param_name, value)


def _read_baked_quantile_defaults(source_code: str) -> "dict[str, object]":
    """AC-4, the single-run route: a single run carries no param_grid at all, so its
    entry_quantile/exit_quantile are strategy-declared defaults read at container run time via
    `self.param(...)` from the two module-level constants research_lab/generator/emit.py's own
    template bakes, DEFAULT_ENTRY_QUANTILE and DEFAULT_EXIT_QUANTILE. Reads the strategy's STORED
    SOURCE TEXT with `ast`, never imports or executes it, the SAME discipline
    stage_runner._read_quantile_min_bars already uses for a different baked constant. Returns a
    dict keyed by the PARAMETER name (never the source token) holding whichever of the two
    constants the source actually declares; a hand-authored strategy declaring neither returns an
    empty dict and is therefore left unchecked, matching stage_runner's own hand-authored
    fallthrough for its own baked constant."""
    tree = ast.parse(source_code)  # parse only; never exec, matching the OPEN-4 boundary
    found: "dict[str, object]" = {}
    for node in ast.walk(tree):
        is_candidate = (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _BAKED_QUANTILE_CONSTANTS
        )
        if is_candidate and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)) \
                and not isinstance(node.value.value, bool):
            found[_BAKED_QUANTILE_CONSTANTS[node.targets[0].id]] = node.value.value
    return found


def _refuse_bad_baked_quantiles_in_source(source_code: str) -> None:
    """AC-4, the single-run route's own call: refuse the FIRST out-of-range baked quantile
    constant a strategy's stored source declares, before any draw is ever requested."""
    for param_name, value in _read_baked_quantile_defaults(source_code).items():
        if not _quantile_in_range(value):
            QUANTILE_PRECHECK_MONITOR_COUNTERS["quantile_precheck_refusals"] += 1
            raise QuantileOutOfRange(param_name, value)
