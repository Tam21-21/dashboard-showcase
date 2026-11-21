"""The strategy row of a generator v2 theory (lane GV2-7b, DECISIONS #445).

A theory is searched, optimised and walked forward by the jobs run_theory_search and run_theory_stage, never by
the ordinary single-run or sweep engine. Its research_strategies row exists so that a run, a draw, a trial and a
verdict have a strategy id and a version to point at. The row's source is a small module that holds the theory's
TOML text as a constant and one Strategy subclass whose run() raises TheoryRunsOnlyAsJobs, so a theory handed to
any other job fails by name instead of running as something it is not.

Standard library only. The source never runs in the backend: researchdb.validate_strategy_source reads it as text.
"""
from __future__ import annotations

import re

NAME_PREFIX = "theory__"  # the reserved start of a theory strategy's name; apart from the generator v1 prefix "gen__"

# The name goes into a docstring and a string constant, so it holds plain characters only: a quote, a backslash or a
# line break in it would break out of the module's text.
_PLAIN_NAME = re.compile(r"^[A-Za-z0-9_.\- ]+$")

_MODULE_TEMPLATE = '''"""Generator v2 theory: {theory_name}.

This strategy row stands for a theory file of generator v2 (DECISIONS #445). The theory's TOML text is the
constant THEORY_TOML below. The search, the optimise stage and the walk-forward stage run it through the jobs
run_theory_search and run_theory_stage; run() here refuses every other use by name.
"""
from __future__ import annotations

from strategies.base import Strategy

THEORY_NAME = {theory_name!r}
THEORY_TOML = {theory_text!r}


class TheoryRunsOnlyAsJobs(RuntimeError):
    """A theory strategy was handed to an engine that is not a theory job."""


class TheoryStrategy(Strategy):
    """The row of one generator v2 theory; it does not run as an ordinary strategy."""

    asset_class_scope = ("equities",)

    def run(self, data, settings):
        raise TheoryRunsOnlyAsJobs(
            "theory " + THEORY_NAME + " runs only as the jobs run_theory_search and run_theory_stage, never as an ordinary run or sweep"
        )
'''


def _check_name(theory_name) -> None:
    if not isinstance(theory_name, str) or not theory_name.strip():
        raise ValueError("a theory needs a non-empty name, got %r" % (theory_name,))
    if not _PLAIN_NAME.match(theory_name):
        raise ValueError("a theory's name may hold letters, digits, space, dot, dash and underscore only, got %r" % (theory_name,))


def strategy_name(theory_name: str) -> str:
    """The research_strategies name of a theory: the reserved prefix and the theory's own name."""
    _check_name(theory_name)
    return NAME_PREFIX + theory_name.strip()


def emit_theory_strategy(theory_name: str, theory_text: str) -> str:
    """The module source of a theory's strategy row. The same name and text give the same source, so an upsert is a no-op."""
    _check_name(theory_name)
    if not isinstance(theory_text, str) or not theory_text.strip():
        raise ValueError("a theory needs its TOML text")
    return _MODULE_TEMPLATE.format(theory_name=theory_name.strip(), theory_text=theory_text)
