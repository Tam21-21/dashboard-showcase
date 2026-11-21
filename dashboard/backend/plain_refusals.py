"""Plain sentences for refusals the browser shows (lane DBG-21).

The routes put a reason word first and one sentence after it. The sentence is built here from the
exception's own fields, so no code word, class name or spec tag reaches the page. The full
exception text goes to the server log at the route (DECISIONS #432).
"""
from __future__ import annotations

from research_lab import universe as universe_rule

from . import tracker

# The five universe-rule refusals whose own message fails the plain rule. TooFewQualifyingNames is
# already plain and is not listed: the route keeps its text.
PLAIN_UNIVERSE_REFUSALS = (
    universe_rule.ContaminationListUnavailable,
    universe_rule.ContaminationListEmpty,
    universe_rule.ContaminationListInert,
    universe_rule.UniverseParameterOwed,
    universe_rule.HoldoutNotDrawnYet,
)

_BARRED = "The list of stocks barred from research (DECISIONS #242)"


def sentence(exc: Exception) -> str:
    """One plain sentence for a refusal. Any class not listed raises TypeError naming it."""
    if isinstance(exc, tracker.ChronologyRefused):
        return (
            "This test window does not come after the last window this strategy already used "
            "on its calendar; each new test must move forward in time."
        )
    if isinstance(exc, tracker.DrawWindowMissing):
        return "This stock group has no start or end date for its test window, so no data can be granted for it."
    if isinstance(exc, tracker.RunLinkFailed):
        return (
            f"Run {exc.run_id} could not be linked to its data draw {exc.draw_id}; "
            "the run exists, retry the link."
        )
    if isinstance(exc, universe_rule.ContaminationListUnavailable):
        return (
            f"{_BARRED} is missing on this server, so the screen cannot remove them; "
            f"{exc.would_have_qualified_count} stocks were waiting for that check."
        )
    if isinstance(exc, universe_rule.ContaminationListEmpty):
        return (
            f"{_BARRED} is empty, so the screen cannot remove them; "
            f"{exc.would_have_qualified_count} stocks were waiting for that check."
        )
    if isinstance(exc, universe_rule.ContaminationListInert):
        return (
            f"{_BARRED} holds {exc.actual_row_count} stocks instead of "
            f"{universe_rule.CONDEMNED_LIST_PROVENANCE_ROW_COUNT}, so it looks cut short "
            "and the screen will not use it."
        )
    if isinstance(exc, universe_rule.UniverseParameterOwed):
        return (
            "A setting the stock screen needs has no approved value in the parameter register "
            "yet, and no default is used."
        )
    if isinstance(exc, universe_rule.HoldoutNotDrawnYet):
        return (
            "This calendar has not drawn its held-out stocks yet; draw them once before "
            "screening any window on it."
        )
    raise TypeError(f"no plain sentence for {type(exc).__name__}")
