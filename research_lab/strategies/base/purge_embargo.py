"""The purge/embargo primitive (M3.5, EL54-EL55): cross-validation hygiene over label spans.

A single pure function over a generic label-span series plus an embargo size (§11; 2b §7;
R9, R13, Q7). It takes NO barrier-specific or event-specific arguments, so the labeling
engine (brief E) is a producer of label spans and the cross-validation layers (M3.5's own
labeling-driven validation and M6's combinatorial purged cross-validation) are consumers;
neither reaches into the other's internals. This is the shared infrastructure DECISIONS #49
item 3 pulled into M3.5 — built once, used by the engine M3.5 ships and by the M6 CPCV.

Concurrent (time-overlapping) labels are, per AFML §4.2 (Overlapping Outcomes), the single
biggest reason machine learning fails in finance; without the purge and embargo of AFML Ch 7
a validation score is fiction (R13).

AFML cross-check (CLAUDE.md books rule; the PDF is laptop-only / gitignored):
  - §7.4.1 "Purging the Training Set" (PDF p.149): "purge from the training set all
    observations whose labels overlapped in time with those labels included in the testing
    set." A train label [t_{i,0}, t_{i,1}] overlaps a test label [t_{j,0}, t_{j,1}] if any of
    three sufficient conditions hold — t_{j,0} <= t_{i,0} <= t_{j,1}, or t_{j,0} <= t_{i,1}
    <= t_{j,1}, or t_{i,0} <= t_{j,0} <= t_{j,1} <= t_{i,1}. Their union is exactly the
    closed-interval overlap predicate (start_i <= end_j AND end_i >= start_j), which is what
    this function tests.
  - §7.4.2 Embargo (PDF p.149): "eliminate from the training set observations that
    immediately follow an observation in the testing set", because serially-correlated
    features leak from test to the training labels that begin just after the test set ends.
    Here a train observation is embargoed when its label_start falls within `embargo_size`
    after a test observation's label_end (the forward buffer).

Embargo size (EL55, R17): `embargo_size` is supplied by the caller in epoch seconds; it is
derived from the label horizon (the vertical-barrier holding horizon plus the feature
lookback — the span over which a label's information reach persists), NOT the AFML
`h ~= .01T` rule of thumb, which is the placeholder the project contract bans. This
primitive does not compute the size; it applies it. The size's provenance entry
(`derived.embargo_size`) is owed at brief J2 (§12, §21).

Masks are NOT complementary: a test observation is in `test_mask` only; a purged or
embargoed observation is False in BOTH masks (excluded from training, and not test) — that
exclusion is the whole point. Scale (DDIA workflow, CLAUDE.md): the overlap/embargo tests are
pairwise (every observation against every test observation), in-memory on a single node, which
is comfortable at M3.5 / M6 single-split scale (hundreds to low thousands of observations) —
below the working-set size where DDIA's in-memory aggregation gives way to a sort that spills to
disk and scales beyond memory (kleppmann_canon.md:2801-2802, "Sorting vs in-memory aggregation");
a sort-based sweep is the forward-looking optimization if a split's observation count ever grows large.
"""
from __future__ import annotations  # lazy annotations, consistent with base/

import numpy as np  # boolean masks + vectorized interval tests
import pandas as pd  # the label_spans frame (columns label_start, label_end)


def purge_embargo_masks(
    label_spans: "pd.DataFrame",  # columns label_start, label_end (epoch s); one row per labelled obs
    test_indices: "np.ndarray",  # positional indices of the test set within label_spans
    embargo_size: int,  # epoch seconds; the label-horizon-derived forward buffer (EL55), >= 0
) -> "tuple[np.ndarray, np.ndarray]":  # (train_mask, test_mask): boolean arrays over every observation
    """Return the (train_mask, test_mask) for one train/test split, purged and embargoed.

    Purge: drop training observations whose ``[label_start, label_end]`` overlaps any test
    observation's span in time (AFML §7.4.1). Embargo: additionally drop training
    observations whose ``label_start`` falls within ``embargo_size`` seconds after a test
    observation's ``label_end`` (AFML §7.4.2). Both masks span every observation; a purged or
    embargoed observation is False in both.
    """
    for col in ("label_start", "label_end"):  # the only columns this primitive reads (Q7)
        if col not in label_spans.columns:  # a malformed span frame is a programming error
            raise ValueError(f"label_spans is missing the required column {col!r}")
    if embargo_size < 0:  # a negative buffer is meaningless; fail loudly, no silent clamp
        raise ValueError(f"embargo_size must be non-negative, got {embargo_size}")

    # A NaN/inf epoch silently casts to a garbage int64 (numpy's unchecked float->int gives
    # INT64_MIN with only a RuntimeWarning), which would corrupt every split. A non-finite or
    # inverted span is a producer bug, not a coverage gap, so fail loudly before the cast.
    for col in ("label_start", "label_end"):
        if not np.isfinite(label_spans[col].to_numpy(dtype=float)).all():
            raise ValueError(f"label_spans column {col!r} has a non-finite value (NaN/inf); spans must be finite epoch seconds")
    inverted = (label_spans["label_start"] > label_spans["label_end"]).to_numpy()
    if inverted.any():  # start after end is a malformed span; under-purges silently if let through
        first_bad = np.flatnonzero(inverted)[:5].tolist()
        raise ValueError(f"label_spans has {int(inverted.sum())} row(s) with label_start > label_end (first positional rows: {first_bad})")

    n = len(label_spans)  # total observation count; both masks span all n
    starts = label_spans["label_start"].to_numpy(dtype=np.int64)  # per-obs span starts (epoch s)
    ends = label_spans["label_end"].to_numpy(dtype=np.int64)  # per-obs span ends (epoch s)

    test_mask = np.zeros(n, dtype=bool)  # False everywhere; True at the test observations
    test_pos = np.asarray(test_indices, dtype=np.int64)  # positional, within [0, n)
    oob = test_pos[(test_pos < 0) | (test_pos >= n)]  # catch out-of-range indices with context
    if oob.size:  # a bare numpy IndexError below would name neither n nor the offending values
        raise ValueError(f"test_indices has {oob.size} value(s) out of range for label_spans of length {n}: {oob.tolist()}")
    test_mask[test_pos] = True

    if test_pos.size == 0:  # no test set -> nothing to purge or embargo; every obs trains
        return ~test_mask, test_mask

    test_starts = starts[test_pos]  # the test spans we purge/embargo against
    test_ends = ends[test_pos]

    # Purge: obs i overlaps test obs j (closed intervals) iff start_i <= end_j AND end_i >= start_j.
    # Pairwise over (all obs) x (test obs); .any over the test axis flags an obs overlapping any test.
    overlaps = (starts[:, None] <= test_ends[None, :]) & (ends[:, None] >= test_starts[None, :])
    # purged includes the test observations themselves (every span overlaps itself); harmless
    # because train_mask gates them out via ~test_mask below. It is "training obs to drop" only
    # after that gate, not on its own.
    purged = overlaps.any(axis=1)  # True where obs i overlaps at least one test span

    # Embargo: obs i is embargoed iff its start falls in (test_end_j, test_end_j + embargo_size].
    after = (starts[:, None] > test_ends[None, :]) & (
        starts[:, None] <= test_ends[None, :] + embargo_size
    )
    embargoed = after.any(axis=1)  # True where obs i starts within the forward buffer of any test

    # Train: every non-test observation that is neither purged nor embargoed.
    train_mask = (~test_mask) & (~purged) & (~embargoed)
    return train_mask, test_mask
