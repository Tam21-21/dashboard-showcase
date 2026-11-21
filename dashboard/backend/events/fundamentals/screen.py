"""The universe screen on a fundamental (M3.75 FUND-4 §9, requirements R17).

The arch §5 calendar-keyed form of the as-of join, run server-side at universe resolution. It
post-filters a candidate CIK set (the output of `resolve_universe_ciks`, universe.py) down to the
companies whose as-of fundamental value satisfies a comparison at a single decision date D — the
calendar-keyed sibling of the per-event as-of read.

The screen is feasible precisely because the fundamentals fact table lives in `events.db` too
(§1): it is a single-connection, `events.db`-local read, NOT the cross-database
`research.db`->`events.db` join DECISIONS #86 found un-runnable. It does one indexed point lookup
per candidate CIK (N reads against the §1.2 (cik, concept, fiscal_period_code, period_end,
filing_date) index), reusing `read_asof` — the SAME as-known-on-D / period-by-`period_end`
correctness (DECISIONS #92 Rule-C) the per-event feature read uses, never a second selection rule
that could disagree. At the stated few-hundred-CIK scale N indexed point reads are fine; a single
SQL JOIN is not needed (and would re-introduce the cross-DB join the arch ruled out).

A candidate with NO as-of fundamental is EXCLUDED (Q6 default, §12): a company we cannot evaluate
point-in-time is not asserted into the screened set — symmetric with the feature path's `exclude`
default. The output is a filtered CIK list, the SAME shape `resolve_universe_ciks` returns, so the
screen is an additive predicate at the resolution site, not a new consumer surface.

The membership date it screens at inherits M3.5's flagged not-point-in-time membership limitation
(DECISIONS #54, the per-event gate a no-op on real runs); the screen neither fixes nor worsens it
(R17 honesty). M3.75 proves the calendar-keyed read through this screen; the rebalance-schedule
form of the same read (R14) is first driven by a real rebalance loop at the factor milestones.
"""
from __future__ import annotations

import operator

from ... import eventsdb                                    # the events.db connection owner (sets row_factory=Row)

from .concepts import FUNDAMENTAL_CONCEPTS, ConceptSpec, concept_by_name
from .reads import VINTAGE_AS_KNOWN, read_asof


# The closed comparator set the screen accepts. CHECK-style validation (the parse.py/write.py
# frozenset idiom): a typo or an unsupported operator fails loud at the call boundary, never a
# silent mis-screen. Mapped to the stdlib operator functions so the comparison is data, not a
# branch ladder. `==`/`!=` are deliberately excluded — exact float equality on a reported magnitude
# is not a meaningful fundamental screen.
_COMPARATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def screen_ciks_by_fundamental(
    *,
    ciks: list[int],                                         # the candidate set (from resolve_universe_ciks)
    membership_date: int,                                    # the decision date D (epoch s UTC) — the calendar-keyed form of §5.2
    concept: str,                                            # the fundamental to screen on (a vocabulary name)
    op: str,                                                 # one of _COMPARATORS ('>','>=','<','<='), CHECK-style validated
    threshold: float,                                        # the value the as-of fundamental is compared against
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,  # the vocabulary (overridable for the NFR-11 probe)
    events_db_path=None,                                     # events.db override (the probe points it at a throwaway)
) -> list[int]:
    """Keep the candidate CIKs whose as-known-on-D ``concept`` value satisfies ``op threshold``.

    For each candidate CIK it applies the §5.2 as-of predicate via ``read_asof`` (the period of
    ``concept`` with the greatest ``period_end`` whose publication date is on or before
    ``membership_date``, in the concept's scope, vintage = as-known-on-D — the DECISIONS #92 Rule-C
    selection), then keeps the candidate iff its as-of value satisfies ``op threshold``. A candidate
    with NO as-of fundamental (``read_asof`` returns None) is EXCLUDED (Q6 default). Returns the
    kept CIKs in the input order (the candidate list is already sorted/deduped by
    resolve_universe_ciks); an empty result is a valid empty screen, never an error.

    Fails loud (ValueError) on a bad ``op`` or an unknown ``concept`` — both are caller bugs, not
    data conditions, so they must not silently pass an unfiltered universe through.
    """
    if op not in _COMPARATORS:                               # a bad comparator is a caller bug; fail loud
        raise ValueError(f"op must be one of {sorted(_COMPARATORS)}; got {op!r}")
    if concept_by_name(concepts).get(concept) is None:       # an unknown concept can never resolve; fail loud
        raise ValueError(f"unknown concept {concept!r}; known: {sorted(concept_by_name(concepts))}")
    if not ciks:                                             # no candidates -> empty screen, no DB touch
        return []

    compare = _COMPARATORS[op]                               # the comparison is data (op -> operator function)
    kept: list[int] = []                                     # the candidates that pass the as-of threshold
    # ONE events.db connection for the whole screen (read-only, Row factory set by get_events_conn).
    # The per-CIK point reads each ride the §1.2 index prefix; the connection is reused across the
    # loop (not reopened per CIK) so the screen is one connection lifecycle, not N.
    with eventsdb.get_events_conn(events_db_path) as conn:
        for cik in ciks:                                     # one indexed point lookup per candidate
            fact = read_asof(conn, cik, concept, membership_date, VINTAGE_AS_KNOWN, concepts)
            if fact is None:                                 # no point-in-time fact public by D -> exclude (Q6)
                continue
            if compare(fact.value, threshold):               # as-of value satisfies op threshold -> keep
                kept.append(cik)
    return kept
