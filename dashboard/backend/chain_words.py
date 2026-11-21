"""The Chains page's closed word maps (lane P7-0, criteria AC-1 and AC-2 of docs/units/CU).

Every word a chain row can carry is mapped here to what the page shows: the state words (S-1), the
partition into groups, the step a chain sits at or ended at (T-1), the live notes (N-1) and one
plain sentence for every reason a chain can end with (R-1, R-2 and P-1 of
docs/briefs/chain_runner_spec_2026-09-24.md section 5). The sentences live on the server so the page
and any later reader share one map. A reason word this map does not know is never shown raw: it
gets a fixed fallback sentence and is counted in UNKNOWN_REASON_WORDS, so a missing sentence shows
up in a count instead of on the page.
"""
from __future__ import annotations

from collections import Counter

# S-1: the stored state and the word the page shows for it.
STATE_WORDS = {
    "waiting": "WAITING TO START",
    "screening": "SCREENING",
    "optimising": "OPTIMISING",
    "choosing-cell": "CHOOSE A CELL",
    "replaying": "REPLAYING",
    "walking-forward": "WALKING FORWARD",
    "judging": "JUDGING",
    "done": "DONE",
    "refused": "REFUSED",
    "failed": "FAILED",
    "stopped": "STOPPED",
}

# S-1's partition: active (the runner acts on it), waiting-on-you (needs Tamer's pick), ended
# (finished for good) and retryable (ended, but a retry is allowed).
STATE_GROUPS = {
    "waiting": "active", "screening": "active", "optimising": "active", "replaying": "active",
    "walking-forward": "active", "judging": "active",
    "choosing-cell": "waiting-on-you",
    "done": "ended", "stopped": "ended",
    "refused": "retryable", "failed": "retryable",
}

# N-1: live notes, derived at read time, never stored.
NOTE_WORDS = {
    "no-worker-online": "WORKER OFFLINE",
    "step-stalled": "STALLED",
    "stop-requested": "STOPPING",
}

# T-1: the step each running state sits at. A chain that is done was last at the judge.
STEP_FOR_STATE = {
    "waiting": "screen", "screening": "screen", "optimising": "optimise", "choosing-cell": "choose",
    "replaying": "replay", "walking-forward": "walkforward", "judging": "judge",
    "done": "judge",
}

# States whose step is read from the row's ended_at_step instead of the map above.
STATES_WITH_ENDED_STEP = ("refused", "failed", "stopped")

UNKNOWN_REASON_SENTENCE = "This chain ended for a reason the page has no sentence for yet."

# How many times each unknown reason word was asked for, so a gap in the map is counted.
UNKNOWN_REASON_WORDS: Counter = Counter()

REASON_SENTENCES = {
    # R-1: what the code a step calls can refuse with.
    "candidate-version-moved": "The strategy was edited after this chain started, so the chain no longer tests the version it began with.",
    "daily-store-not-configured": "The daily price store is not set up on this machine, so no price window can be served.",
    "lead-in-unavailable": "The price store does not hold enough earlier bars before the window starts.",
    "warmup-unsatisfied": "The strategy needs more earlier bars than the price store holds before the window starts.",
    "quantile-constant-ambiguous": "The strategy holds a quantile number that cannot be read in one clear way.",
    "walkforward-window-too-small": "The walk forward window is too short for the walk forward settings.",
    "walkforward-chosen-not-qualifying": "The stock picked for the walk forward does not qualify in the walk forward window.",
    "walkforward-lookback-undeclared": "The strategy does not say how many bars it looks back, which the walk forward needs.",
    "stage-word-unknown": "The runner asked for a test stage that does not exist.",
    "walkforward-parameter-absent": "One of the three walk forward settings has no value yet.",
    "quantile-out-of-range": "A quantile in the parameter grid is outside its allowed range.",
    "generated-source-drift": "The strategy's code no longer matches the record made when it was generated.",
    "strategy-not-found": "The strategy this chain tests can no longer be found.",
    "strategy-deleted": "The strategy this chain tests was deleted.",
    "price-universe-not-found": "The stock list this step needs was not found.",
    "price-universe-empty": "The stock list this step needs holds no stocks.",
    "universe-name-taken": "A stock list with this name already exists for a different window.",
    "price-universe-window-invalid": "The window given for the stock list is not valid.",
    "frozen-sweep-parameter": "The parameter grid changes a setting that is frozen.",
    "undeclared-sweep-parameter": "The parameter grid names a setting the strategy does not declare.",
    "sweep-parameter-missing": "The parameter grid leaves out a setting the strategy declares.",
    "sweep-grid-too-large": "The parameter grid has more combinations than one sweep may run.",
    "unknown-asset-class": "The strategy names an asset class the system does not know.",
    "asset-class-scope-mismatch": "The stock list holds an asset class the strategy does not trade.",
    "sweep-request-invalid": "The sweep request built from this chain was not valid.",
    "chronology-refused": "The test windows would break the order of the data use record, so the draw was refused.",
    "draw-window-missing": "The data use record has no window for this step.",
    "reserve-cut-crossed": "The window reaches past the reserve cut date, which no test may touch.",
    "run-link-failed": "The run could not be linked to its data draw.",
    "universe-parameter-owed": "A setting the stock list rule needs has no value yet.",
    "contamination-list-unavailable": "The list of contaminated stocks is not available.",
    "contamination-list-empty": "The list of contaminated stocks is empty.",
    "contamination-list-inert": "The list of contaminated stocks has no effect on the stock list.",
    "contamination-list-unreadable": "The list of contaminated stocks could not be read.",
    "too-few-qualifying-names": "Too few stocks qualify in this window to run the test.",
    "holdout-not-drawn": "The hold out window was not drawn before it was needed.",
    # R-2: the runner's own reasons.
    "stage-draw-orphaned": "A data draw for this step exists but no run is linked to it.",
    "stage-draw-duplicated": "More than one live data draw exists for this step.",
    "stage-link-refused": "The data use record refused to link the run to its draw.",
    "stage-run-failed": "The run for this step failed.",
    "stage-run-cancelled": "The run for this step was cancelled.",
    "stage-run-deleted": "The run for this step was deleted.",
    "duel-inputs-differ": "The two chains of this duel were not given the same inputs.",
    "route-answer-unworded": "A check answered with a refusal the runner has no name for.",
    "step-crashed": "The runner hit an unexpected error on this step. The error was logged.",
    # P-1: replay and judge answers that fail or refuse the chain.
    "replay-failed": "The replay of the chosen cell failed.",
    "replay-not-reproduced": "The replay did not reproduce the numbers of the chosen cell.",
    "cell-key-incomplete": "The chosen cell is missing part of its key.",
    "single-run-not-a-replay": "The run found is a single run but not a replay of the chosen cell.",
    "sweep-not-optimise": "The sweep found is not the optimise sweep of this chain.",
    "walkforward-run-not-gradable": "The walk forward run cannot be graded.",
    "source-link-refused": "The link from the replay to its source sweep was refused.",
    "sweep-needs-a-cell": "The sweep needs a chosen cell before it can be replayed or graded.",
    "cell-not-found": "The chosen cell was not found in the sweep.",
    "replay-mode-unknown": "The replay was made in a mode the judge does not know.",
    "replay-source-incomplete": "The replay's source sweep is missing information.",
    "replay-absent": "The judge found no replay of the chosen cell.",
    "replay-unverifiable": "The replay of the chosen cell cannot be checked against its sweep.",
}


def reason_sentence(reason_word: str) -> str:
    """The plain sentence for a reason word. A word not in the map gets the fixed fallback
    sentence and adds one to UNKNOWN_REASON_WORDS for that word, so the gap is counted."""
    sentence = REASON_SENTENCES.get(reason_word)
    if sentence is None:
        UNKNOWN_REASON_WORDS[reason_word] += 1
        return UNKNOWN_REASON_SENTENCE
    return sentence
