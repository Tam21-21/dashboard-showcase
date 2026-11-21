"""The feature layer (M3.5, EL67-EL69): peer-event features over the materialized slice.

Brief F (spec brief §6; 2a §4, §5; R6, R7, Q3, Q8). Three things live here:

  - ``FeatureDef``: the strategy's frozen feature declaration (what the
    ``EventStrategy.feature_defs`` hook returns). One declaration covers both the
    peer-event case (non-empty ``peer_ciks``) and the single-event case (empty).
  - ``peer_event_feature``: the library-level peer primitive (R6). For one trigger
    event it returns the fraction of the strategy's peer set that had an event of the
    same type in a window around the trigger, with a strategy-chosen missingness mode.
    This is the reusable, hard-to-get-right part the M2.5 read path named (the read is
    "this event plus other events over a window in company space", not single-event
    fetch).
  - ``provide_features``: the single localized seam (the F1-flip containment). Under
    the recompute-per-run default (EL67, Q8) it computes each peer feature in-run from
    the materialized slice and returns the per-event frames; it stores nothing. If
    probe F1 ever overturns the default, the ONLY edit point is the stored-mode branch
    inside this one function — write/read a ``dataset_type='feature'`` parquet under the
    run's data area instead of recomputing — and nothing else in the framework changes.

Recompute, not store (EL67, 2a §4). A stored materialized view buys read speed but
adds a maintenance-on-write obligation (``kleppmann_canon.md:1064``): a store correction
(M2.5 corrections are real, DECISIONS #43) makes every derived feature stale. Recompute
has no staleness surface — it is always consistent with the current point-in-time view —
and matches the event parquet's own derived-follower pattern (R19). The warehouse keeps
the raw data and uses a cube only as an accelerator (``kleppmann_canon.md:1070``); here
the recompute default IS keeping the raw and deriving on read. Probe F1 measured the
recompute fraction and ratified this default (DECISIONS #55).

The peer read pattern (EL68, Q3; 2a §5). It runs container-side over the mounted slice,
never against the store (the container has no network, R18; the materializer already
returns every universe peer's events of the type for the window, so every peer event is a
row in the same parquet). It decomposes into the trigger's bounded peer set crossed with
one time-range scan per peer (``subject_cik = peer, knowledge_at in window``), the
prefix-plus-range case (``kleppmann_canon.md:1075``), NOT a single two-range query (the
multidimensional-index case the canon warns against, ``kleppmann_canon.md:1078``). At
M3.5 volumes (megabytes) the slice is read into memory and filtered in-memory; no sort,
no event-store index. The forward-looking large-slice optimization (sort the slice by
``(subject_cik, knowledge_at)`` so each peer's events are contiguous and the window prunes
by Parquet row-group min/max, ``kleppmann_canon.md:1035``) is the framework's own in-run
sort, adding no event-store change.

Modeling choices this layer makes that sit below the spec's resolution (surfaced for
Cowork per CLAUDE.md "name the decision, don't bury it"; the §17.2 brief F probe pins each):

  1. No-lookahead window cap (R10). The locked ``peer_event_feature`` signature (§6)
     carries no price index, so it cannot resolve the exact entry-bar decision time (the
     next-bar open strictly after ``knowledge_at``, EL52). It therefore caps the peer
     window's upper edge at the trigger's own ``knowledge_at`` — the conservative,
     price-index-free bound: the decision time is strictly later, so capping at
     ``knowledge_at`` can only EXCLUDE peer events that became public after the trigger,
     never leak a future-known one (R10). A consequence: a positive ``window_after`` is
     fully dominated by this cap in the as-built primitive (the effective upper edge is
     ``min(knowledge_at + window_after, knowledge_at) == knowledge_at``). Honoring the
     sub-bar ``(knowledge_at, decision_time]`` sliver later is a SIGNATURE CHANGE, not a
     free extension: it needs each event's price index (to resolve the next-bar-open
     decision time) threaded through ``provide_features`` into this primitive, which the
     locked §6 signature does not carry. Until then ``window_after`` is the declared
     intent, clipped. Cowork ruling (brief F close, DECISIONS #60): ``peer_window_after``
     is EXEMPT from the §12 provenance obligation until that seam exists — an inert knob
     has no sensitivity to record, and gating it to a fixed value would contradict §6.

  2. Missingness semantics (EL69), the literal §6 reading. The numerator is the count of
     peers with at least one event in the window; the denominator depends on the mode:
       - ``'zero'``  : a peer with no event in the window contributes 0 to the numerator
                       and stays in the denominator -> present / total.
       - ``'exclude'``: a peer with no event in the window is dropped from the
                       denominator -> present / present. For this presence-count feature
                       that collapses to 1.0 whenever any peer is present (and to a null
                       feature when none is). The mode is well-defined and offered because
                       a future value-weighted feature (where present peers contribute
                       distinct magnitudes) makes it non-degenerate; for the M3.5 presence
                       primitive its 1.0 collapse is the spec's literal consequence, not a
                       silent reinterpretation. Surfaced for Cowork.
       - ``'null'``  : any peer with no event in the window nullifies the feature -> NaN
                       (the strict "all peers observed or I do not trust it" mode).
     The framework bakes no single policy (2a §5 carried this to the spec brief); the mode
     is a per-strategy categorical setting (``peer_missingness``) owing a provenance entry
     at brief J2, not here (§12, EL57).

  3. Single-event features are not computed here (empty ``peer_ciks``). §6 specifies only
     the peer primitive; a genuine single-event feature reads the trigger event's own
     payload (a rating magnitude, a filing lag), which is event-type-specific and so the
     strategy's own computation (brief J2), not a framework primitive. ``FeatureDef``
     expresses the single-event shape (so the type is forward-proof), but a single-event
     def routed through ``provide_features`` fails loud with an actionable message rather
     than returning an invented generic value. Surfaced for Cowork.

  4. The peer set is treated as a set: duplicate ``peer_ciks`` are collapsed before the
     fraction so a repeated CIK cannot double-count the denominator (the parameter is "the
     peer set"). The trigger's own CIK is NOT auto-removed from its peer set — whether a
     company is its own peer is the strategy's choice, so no silent self-exclusion.

Input contract. ``slice_events`` is a materialized event slice (the §3.2 columns); this
layer reads ``subject_cik`` and ``knowledge_at`` (and ``event_id`` in ``provide_features``
to key the output). ``knowledge_at`` is normalized to epoch seconds from either an integer
epoch column or a datetime64/Timestamp, the same axis the store and the labeling engine use.
A datetime ``knowledge_at`` must be UTC-aware and non-null (the materializer writes
``tz='UTC'``, ``output_schemas.py:148``): a NaT would cast to a finite garbage epoch and a
tz-naive value is ambiguous, so both are rejected loudly rather than silently mis-dated.
"""
from __future__ import annotations  # lazy annotations, consistent with the rest of base/

from dataclasses import dataclass  # the feature declaration is a plain frozen record
from typing import TYPE_CHECKING  # keep the Path import type-check-only (no runtime cost)

import numpy as np  # vectorized peer membership + the fraction; engine-side, not the light surface
import pandas as pd  # the materialized-slice frames + the knowledge_at normalization

if TYPE_CHECKING:  # readers / type checker only; the annotation below is lazily a string
    from pathlib import Path  # provide_features' run_data_dir type (the stored-mode write target)

# The catalog dataset type for a stored feature dataset (EL75, §3.3). Registered (named)
# now so the F1-flip stored branch lands feature rows under an already-known type with no
# catalog migration; UNUSED under the recompute default (no feature rows are written per
# run). The dashboard-side type registry follows the same one-constant-per-type pattern
# (events/universe.py: DATASET_TYPE_UNIVERSE; events/catalog.py: 'event_series').
DATASET_TYPE_FEATURE = "feature"  # research_datasets.dataset_type for a stored feature dataset

_MISSINGNESS_MODES = ("zero", "null", "exclude")  # the EL69 modes; anything else is a config error


@dataclass(frozen=True)
class FeatureDef:
    """A strategy's declaration of one feature (what ``EventStrategy.feature_defs`` returns).

    Single-event and peer-event features share this one declaration: a non-empty
    ``peer_ciks`` is a peer-event feature; an empty ``peer_ciks`` is the single-event case
    (computed in the strategy, not this framework layer — modeling choice 3).
    """

    name: str  # the feature's name; the key it lands under in StrategyInput.features
    event_type: str  # which materialized slice it reads (e.g. 'analyst_rating')
    peer_ciks: "list[int]"  # the peer set; empty -> single-event, non-empty -> peer-event
    window_before: int  # seconds before the trigger's knowledge_at (>= 0)
    window_after: int  # seconds after; dominated by the no-lookahead cap (>= 0; modeling choice 1)
    missingness: str  # 'zero' | 'null' | 'exclude' (EL69, modeling choice 2)


def _to_epoch_seconds(values: "pd.Series | np.ndarray") -> np.ndarray:
    """Normalize a knowledge_at column to int64 epoch seconds (datetime64 or int epoch in).

    The materialized parquet carries knowledge_at as a datetime64[ns]; a synthetic test
    slice may carry plain int epoch seconds. Mirrors triple_barrier._epoch_seconds so the
    feature axis matches the store's knowledge_at and the labeling engine's spans.
    """
    series = pd.Series(values)  # uniform handling for ndarray or Series input
    if pd.api.types.is_datetime64_any_dtype(series):  # the materializer writes tz='UTC' (output_schemas.py:148)
        dti = pd.DatetimeIndex(series)  # construct once for the two guards below
        if dti.hasnans:  # a NaT casts to INT64_MIN via asi8 -> a finite GARBAGE epoch, never NaN (the brief G / DECISIONS #59 silent-cast class)
            raise ValueError("knowledge_at has a NaT value; timestamps must be non-null datetime64")
        if dti.tz is None:  # a tz-naive datetime is ambiguous (could be local time); the store/materializer emit UTC-aware, so reject rather than assume
            raise ValueError("knowledge_at datetime64 column is tz-naive; expected UTC-aware (the materializer writes tz='UTC', output_schemas.py:148)")
        return dti.asi8 // 1_000_000_000  # asi8 is int64 ns (UTC) -> seconds (triple_barrier:87)
    if pd.api.types.is_integer_dtype(series) or pd.api.types.is_float_dtype(series):  # epoch-second column
        as_float = series.to_numpy(dtype=float)  # check finiteness before the int cast (no silent INT64_MIN)
        if not np.isfinite(as_float).all():  # a NaN/inf epoch is a producer bug, not a coverage gap
            raise ValueError("knowledge_at has a non-finite value (NaN/inf); timestamps must be finite epoch seconds")
        return as_float.astype(np.int64)  # finite -> safe int64 epoch seconds
    raise ValueError(f"knowledge_at must be epoch-second ints or datetime64, got dtype {series.dtype!r}")


def _scalar_epoch_seconds(value: "int | float | pd.Timestamp") -> int:
    """Normalize one trigger's knowledge_at scalar to int epoch seconds (Timestamp or int in)."""
    if isinstance(value, pd.Timestamp):  # a Timestamp trigger time (itertuples on a datetime64 column yields these)
        if pd.isnull(value):  # NaT.value is INT64_MIN -> a finite garbage epoch; fail loud (the same silent-cast class as the column path)
            raise ValueError("knowledge_at scalar is NaT; timestamps must be non-null")
        if value.tz is None:  # a tz-naive Timestamp is ambiguous; the materializer emits UTC-aware (output_schemas.py:148)
            raise ValueError(f"knowledge_at Timestamp is tz-naive; expected UTC-aware, got {value!r}")
        return int(value.value // 1_000_000_000)  # .value is int64 ns since epoch (UTC for a tz-aware Timestamp)
    if isinstance(value, np.datetime64):  # a numpy datetime64 scalar (no tz concept in numpy; treated as UTC)
        if np.isnat(value):  # a NaT would cast to a garbage epoch; fail loud, never a silent value
            raise ValueError(f"knowledge_at scalar is NaT (np.datetime64), got {value!r}")
        return int(value.astype("datetime64[s]").astype(np.int64))  # truncate to epoch seconds
    as_float = float(value)  # an int/float epoch-second scalar
    if not np.isfinite(as_float):  # a non-finite trigger time is a producer bug
        raise ValueError(f"knowledge_at scalar must be a finite epoch second, got {value!r}")
    return int(as_float)  # finite -> int epoch seconds


def peer_event_feature(
    trigger_cik: int,  # the trigger event's subject company
    knowledge_at: int,  # the trigger event's public-knowledge time (epoch s)
    slice_events: "pd.DataFrame",  # the materialized slice for the event type (all universe peers)
    peer_ciks: "list[int]",  # the strategy's peer set (non-empty; collapsed to a set, choice 4)
    window_before: int,  # seconds before knowledge_at (>= 0)
    window_after: int,  # seconds after; dominated by the no-lookahead cap (>= 0, choice 1)
    missingness: str,  # 'zero' | 'null' | 'exclude' (EL69, choice 2)
) -> float:
    """The fraction of the trigger's peer set with >= 1 event in the (no-lookahead) window.

    The window is ``[knowledge_at - window_before, knowledge_at]`` — the upper edge is
    capped at ``knowledge_at`` so no peer that became public after the trigger's trade
    decision can leak in, regardless of ``window_after`` (R10, modeling choice 1). The
    ``missingness`` mode governs peers with no event in that window (choice 2). Returns a
    float in [0, 1] (or NaN under the null/empty-denominator cases). A peer feature over no
    peers, an unknown missingness mode, a negative window, or a slice missing the columns it
    reads are all config errors and raise ``ValueError`` (fail loud, no silent value).
    """
    # Fail-loud config guards (CLAUDE.md: a misconfigured caller must never get a silent value).
    if missingness not in _MISSINGNESS_MODES:  # an unknown mode would otherwise pick a default silently
        raise ValueError(f"missingness must be one of {_MISSINGNESS_MODES}, got {missingness!r}")
    if window_before < 0:  # a negative lookback is meaningless
        raise ValueError(f"window_before must be >= 0 seconds, got {window_before}")
    if window_after < 0:  # negative is meaningless even though the cap dominates a positive value
        raise ValueError(f"window_after must be >= 0 seconds, got {window_after}")
    peer_set = set(int(c) for c in peer_ciks)  # collapse duplicates -> the peer SET (choice 4)
    if not peer_set:  # the peer primitive needs peers; an empty set is a single-event def misrouted here
        raise ValueError(
            "peer_event_feature requires a non-empty peer_ciks (it is the peer-event primitive); "
            "an empty peer set is the single-event case, computed in the strategy, not here (§6, choice 3)"
        )
    for col in ("subject_cik", "knowledge_at"):  # the only slice columns this primitive reads
        if col not in slice_events.columns:  # a malformed slice is a programming error, not a coverage gap
            raise ValueError(f"slice_events is missing the required column {col!r}")

    # The no-lookahead window (choice 1): lower edge in the past, upper edge capped at knowledge_at.
    lower = knowledge_at - window_before  # window start (epoch s)
    upper = min(knowledge_at + window_after, knowledge_at)  # == knowledge_at; the cap dominates window_after

    if len(slice_events) == 0:  # an empty slice -> no peer has any event in the window
        present_count = 0  # nothing present; the missingness branch below handles the denominator
    else:
        ciks = slice_events["subject_cik"].to_numpy(dtype=np.int64)  # int64 company column; the materializer types subject_cik non-nullable (output_schemas.py:151), so a null cannot occur — were one ever present (a float column carrying NaN) the int64 cast would raise, not silently coerce
        ka = _to_epoch_seconds(slice_events["knowledge_at"])  # normalized to epoch seconds
        in_window = (ka >= lower) & (ka <= upper)  # rows inside the no-lookahead window
        peer_arr = np.fromiter(peer_set, dtype=np.int64)  # the peer set as an int64 array for np.isin
        is_peer = np.isin(ciks, peer_arr)  # rows whose company is in the peer set
        present_ciks = set(int(c) for c in ciks[in_window & is_peer])  # distinct peers WITH an event in window
        present_count = len(present_ciks)  # the numerator: peers present in the window

    total = len(peer_set)  # the full peer-set size (the 'zero' denominator)
    absent_count = total - present_count  # peers with NO event in the window

    if missingness == "zero":  # absent peers count as 0, stay in the denominator
        return present_count / total  # present / total
    if missingness == "exclude":  # absent peers dropped from the denominator
        return present_count / present_count if present_count > 0 else float("nan")  # present/present = 1.0, or NaN
    # missingness == "null": any absent peer nullifies the feature
    return float("nan") if absent_count > 0 else present_count / total  # NaN if any peer missing, else the fraction


def provide_features(
    feature_defs: "list[FeatureDef]",  # the strategy's feature definitions (peer windows, missingness, etc.)
    events: "dict[str, pd.DataFrame]",  # the materialized slices, keyed by event type (StrategyInput.events)
    prices: "pd.DataFrame",  # the base price frame (locked seam signature; see the note below)
    run_data_dir: "Path",  # the run's data area, for the stored-mode write/read (the F1-flip branch)
) -> "dict[str, pd.DataFrame]":  # feature_name -> per-event feature frame (-> StrategyInput.features)
    """Compute each strategy feature over the materialized slices (recompute-per-run default).

    The single localized seam (EL67, the F1-flip containment): under the recompute default
    this computes every peer feature in-run and returns the frames, writing NOTHING. The
    stored-dataset branch (an F1 overturn) is the ONLY edit point — write/read a
    ``dataset_type='feature'`` parquet under ``run_data_dir`` here instead of recomputing —
    and nothing else in the framework changes (2a §4; the type is registered as
    ``DATASET_TYPE_FEATURE``, §3.3).

    Returns ``{feature_name: frame}`` where each frame has one row per trigger event of the
    feature's ``event_type``, indexed by that event's ``event_id``, with a single ``value``
    column (the ``peer_event_feature`` result). An empty slice yields an empty frame (zero
    triggers), which is the EL71 empty-slice contract, not an error.

    ``prices`` is part of the locked §6 seam signature for a future price-conditioned feature;
    the M3.5 presence-based peer primitive does not read price, and for an event run the base
    ``prices`` frame is empty anyway (per-symbol price lives in ``StrategyInput.event_prices``).
    A price-conditioned feature would thread ``event_prices`` through this same seam later;
    keeping the parameter holds the contract stable for that extension. ``run_data_dir`` is
    likewise unused under the recompute default (it is the stored-mode write target).
    """
    features: dict[str, pd.DataFrame] = {}  # feature_name -> per-event frame (the return)
    for fd in feature_defs:  # one frame per declared feature
        if fd.missingness not in _MISSINGNESS_MODES:  # validate eagerly so a typo'd mode fails even on an EMPTY slice, not only on the first non-empty trigger row (peer_event_feature is never called for a zero-row slice)
            raise ValueError(f"FeatureDef {fd.name!r} has missingness {fd.missingness!r}; must be one of {_MISSINGNESS_MODES}")
        if not fd.peer_ciks:  # empty peer set -> a single-event def, not computed at the framework layer
            raise ValueError(
                f"FeatureDef {fd.name!r} declares an empty peer_ciks (a single-event feature). The M3.5 "
                f"framework feature layer provides the peer-event primitive only (§6); compute single-event "
                f"payload features in the strategy itself (brief J2). See modeling choice 3."
            )
        if fd.event_type not in events:  # the declared slice was never materialized/mounted -> wiring bug
            raise ValueError(
                f"FeatureDef {fd.name!r} reads event_type {fd.event_type!r}, which is absent from the "
                f"materialized slices {sorted(events)} (it must be a declared EventStrategy.event_type, §8)"
            )
        slice_df = events[fd.event_type]  # the materialized slice this feature reads (all universe peers)
        for col in ("event_id", "subject_cik", "knowledge_at"):  # the columns the per-trigger loop needs
            if col not in slice_df.columns:  # a malformed slice is a programming error, fail loud
                raise ValueError(f"slice for {fd.event_type!r} is missing the required column {col!r}")

        event_ids: list = []  # the spine id per trigger (the output index)
        values: list[float] = []  # the feature value per trigger
        for row in slice_df.itertuples(index=False):  # one trigger event at a time (the recompute scan, 2a §5)
            values.append(peer_event_feature(  # the per-peer prefix-plus-range read for this trigger
                trigger_cik=int(row.subject_cik),  # the trigger's company
                knowledge_at=_scalar_epoch_seconds(row.knowledge_at),  # normalized trigger time (epoch s)
                slice_events=slice_df,  # every universe peer's events of this type are rows here
                peer_ciks=fd.peer_ciks,  # the strategy's peer set
                window_before=fd.window_before,  # the lookback
                window_after=fd.window_after,  # the (capped) look-forward
                missingness=fd.missingness,  # the strategy's missingness mode
            ))
            event_ids.append(row.event_id)  # key the value by the trigger's event_id
        features[fd.name] = pd.DataFrame(  # the per-event feature frame, indexed by event_id
            {"value": values}, index=pd.Index(event_ids, name="event_id"),
        )
    return features  # StrategyInput.features <- this dict (the J1 pipeline assigns it, EL47 step 3)
