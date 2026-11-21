"""The 2024-02-05 regime branch and the regimes-slot fill (M3.5, EL58, EL59).

Brief H (spec brief §13; 2b §8, §9; R11, Q4, Q6). The regime concern is entirely
consumption-side: the M2.5 store records every event's public-knowledge time
(``knowledge_at``) faithfully and unchanged; this layer reads that time and tags each
labelled event by which side of a known SEC rule-change boundary it falls on, so a
strategy whose features assume the pre-2024 reporting timing is made visibly wrong on
post-2024 events rather than silently averaged across the break (R11).

The boundary is a fixed, verified fact, not a tunable parameter: SEC Release 33-11253
shortened the Schedule 13D filing deadline, effective 2024-02-05 (requirements §11). The
only per-strategy choice is the regime MODE (how the run treats events on each side).

Three things live here:

  - ``BOUNDARY``: the boundary as a UTC epoch second, computed ONCE at import from the
    Eastern-zone wall-clock date (see "Boundary timezone" below).
  - ``regime_of`` / ``tag_regimes``: the tagging primitive — scalar and vectorized — that
    maps a ``knowledge_at`` to ``PRE`` or ``POST``.
  - ``apply_regime_mode``: the three-mode logic (``split`` / ``restrict`` / ``mark``) over
    a set of labelled events, returning a ``RegimeOutcome`` the pipeline acts on.
  - ``regimes_slot``: builds the ``RunResult.regimes`` band intervals for the run window.

Boundary timezone (pinned to America/New_York, spec brief §13). The rule's effective date
is 2024-02-05 in US Eastern, but ``knowledge_at`` is a UTC epoch, so the boundary must be
the UTC epoch of ``2024-02-05 00:00:00 America/New_York``, NOT of ``2024-02-05 00:00:00
UTC``. The two differ by the Eastern offset (five hours on that date, EST), and an event
whose ``knowledge_at`` falls in that five-hour window would tag to the WRONG regime if the
boundary were left at bare midnight UTC. ``BOUNDARY`` is therefore computed from
``datetime(2024, 2, 5, tzinfo=ZoneInfo("America/New_York"))`` converted to a UTC epoch,
reusing the Eastern-zone convention brief D established (``ZoneInfo("America/New_York")``,
``dashboard/backend/events/clients/edgar.py:72``; DST-aware, no pytz). This module is
worker-side (``research_lab``), so it uses the stdlib ``zoneinfo`` with that same IANA zone
directly rather than importing the server-side ``edgar.py`` helper across the package
boundary; the boundary is a computed constant, never a hand-typed UTC epoch.

Modeling choices this layer makes that sit below the spec's resolution (surfaced for Cowork
per CLAUDE.md "name the decision, don't bury it"; the §17.2 brief H probe pins each):

  1. The ``restrict`` target regime is an explicit parameter, NOT a settings key — and the
     locked §12 settings table is missing the key that would supply it. ``restrict`` is
     spec'd (§13) as "declare one regime, drop events outside it, counted", but §12 lists
     only ``regime_mode`` (split/restrict/mark); there is no ``regime_select`` (verified:
     the brief contains no such key). So this primitive takes the target as an explicit
     ``target_regime`` argument and the wiring (brief J1) must source it; brief J2 owes the
     companion per-strategy setting AND a §12 settings-table amendment to add it. Until that
     setting exists, ``restrict`` is callable but unreachable from a pure-settings run. This
     is the brief E / brief F precedent (surface the gap, build the seam, do not invent a
     silent default). Flagged for Cowork at the brief H close.

  2. The boundary comparison is half-open at the boundary: ``knowledge_at < BOUNDARY`` is
     PRE, ``knowledge_at >= BOUNDARY`` is POST. An event exactly at the boundary epoch is
     POST (the new rule is in force at and after its effective instant). Matches §13's
     literal "pre-boundary if ``knowledge_at < BOUNDARY``, post-boundary otherwise".

  3. ``regimes_slot`` emits a band ONLY for a regime the window actually touches: a window
     entirely on one side yields a single band; a boundary-spanning window yields two bands
     split AT the boundary; a degenerate empty/zero-length window yields no band. The bands
     are the visible record the export chart overlays (no export change, §13).

Input contract. ``knowledge_at`` is normalized to int epoch seconds from either an integer
epoch column/scalar or a datetime64/Timestamp — the same axis the store, the labeling
engine, and the feature layer use. A datetime ``knowledge_at`` must be UTC-aware and
non-null (the materializer writes ``tz='UTC'``, ``output_schemas.py:148``): a NaT would
cast to a finite garbage epoch (the brief G / DECISIONS #59 silent-cast class) and a
tz-naive value is ambiguous, so both are rejected loudly. The normalizer mirrors
``features._to_epoch_seconds`` deliberately; each ``base/`` module stays independently
importable (the M3.5 base-module convention) rather than cross-importing a private helper.
"""
from __future__ import annotations  # lazy annotations, consistent with the rest of base/

from dataclasses import dataclass  # the regime outcome is a plain frozen record
from datetime import datetime  # build the Eastern wall-clock boundary instant
from zoneinfo import ZoneInfo  # stdlib, DST-aware IANA zones (no pytz); the §13 convention

import numpy as np  # vectorized tagging + the keep/drop masks
import pandas as pd  # the knowledge_at series + the per-event tag/keep Series

# The boundary as a UTC epoch second, computed ONCE at import from the Eastern wall-clock
# date (the §13 timezone pin). .timestamp() on a tz-AWARE datetime returns the correct UTC
# epoch (here 2024-02-05 00:00 EST == 2024-02-05 05:00 UTC), so this is the Eastern-pinned
# instant, never bare midnight UTC. int() drops the always-zero fractional part.
BOUNDARY: int = int(datetime(2024, 2, 5, tzinfo=ZoneInfo("America/New_York")).timestamp())

PRE = "pre_2024_02_05"  # regime label for events known strictly before the boundary
POST = "post_2024_02_05"  # regime label for events known at or after the boundary
_REGIME_LABELS = (PRE, POST)  # the only two regime labels this rule-based branch produces

# The key the rule-based regime occupies in the RunResult.regimes dict. M6 adds DETECTED
# regimes under their own keys additively (EL59); this one key is M3.5's whole contribution.
REGIME_KIND = "rule_based_13d_2024_02_05"  # research_lab/strategies/base/run_result.py:32

REGIME_MODES = ("split", "restrict", "mark")  # the EL58 modes; anything else is a config error


@dataclass(frozen=True)
class RegimeOutcome:
    """The result of applying a regime mode to one run's labelled events.

    Every field is keyed to / aligned with the input ``knowledge_at`` index (the per-event
    ``event_id`` spine the pipeline carries), so the caller (brief J1) can filter the
    labelled set by ``keep`` and report ``dropped_count`` in the run's coverage figure.
    """

    tags: "pd.Series"  # per-event regime label (PRE/POST), indexed like the input
    keep: "pd.Series"  # boolean mask of events the mode keeps (restrict drops some; else all True)
    dropped_count: int  # events dropped by restrict (counted, never silently dropped); 0 for split/mark
    cross_regime: bool  # True iff the KEPT set spans the boundary (the mark/split cross-regime flag)
    per_regime: "dict[str, pd.Index]"  # split: kept event ids grouped by regime; one group for restrict; both for mark


def _to_epoch_seconds(values: "pd.Series | np.ndarray | list[int]") -> np.ndarray:
    """Normalize a knowledge_at column to int64 epoch seconds (datetime64 or int epoch in).

    Mirrors ``features._to_epoch_seconds`` (kept local so this module imports no sibling
    base helper), extended with a ``list[int]`` input because ``tag_regimes`` accepts a raw
    list (the ``pd.Series(values)`` coercion handles all three forms). A datetime64 column
    must be UTC-aware and non-null; an int/float column must be finite. Both guards fail loud
    rather than silently mis-date an event (the DECISIONS #59 silent-cast class).
    """
    series = pd.Series(values)  # uniform handling for ndarray / list / Series input
    if pd.api.types.is_datetime64_any_dtype(series):  # the materializer writes tz='UTC' (output_schemas.py:148)
        dti = pd.DatetimeIndex(series)  # construct once for the two guards below
        if dti.hasnans:  # a NaT casts to INT64_MIN via asi8 -> a finite GARBAGE epoch (DECISIONS #59 class)
            raise ValueError("knowledge_at has a NaT value; timestamps must be non-null datetime64")
        if dti.tz is None:  # a tz-naive datetime is ambiguous; the store/materializer emit UTC-aware
            raise ValueError(
                "knowledge_at datetime64 column is tz-naive; expected UTC-aware "
                "(the materializer writes tz='UTC', output_schemas.py:148)"
            )
        return dti.asi8 // 1_000_000_000  # asi8 is int64 ns (UTC) -> seconds (triple_barrier:87)
    if pd.api.types.is_integer_dtype(series) or pd.api.types.is_float_dtype(series):  # epoch-second column
        as_float = series.to_numpy(dtype=float)  # check finiteness before the int cast (no silent INT64_MIN)
        if not np.isfinite(as_float).all():  # a NaN/inf epoch is a producer bug, not a coverage gap
            raise ValueError("knowledge_at has a non-finite value (NaN/inf); timestamps must be finite epoch seconds")
        return as_float.astype(np.int64)  # finite -> safe int64 epoch seconds
    raise ValueError(f"knowledge_at must be epoch-second ints or datetime64, got dtype {series.dtype!r}")


def regime_of(knowledge_at: int) -> str:
    """Tag one event by the boundary: ``PRE`` if ``knowledge_at < BOUNDARY``, else ``POST``.

    Half-open at the boundary (modeling choice 2): an event exactly at the boundary epoch is
    ``POST`` (the new rule is in force at and after its effective instant).
    """
    return PRE if int(knowledge_at) < BOUNDARY else POST  # the §13 pre/post rule, scalar form


def tag_regimes(knowledge_at: "pd.Series | np.ndarray | list[int]") -> "pd.Series":
    """Vectorized ``regime_of`` over a column of event knowledge times.

    Returns a string Series (``PRE``/``POST``) aligned to the input's index when given a
    Series (so the tags stay keyed by ``event_id``), or a fresh RangeIndex otherwise.
    """
    index = knowledge_at.index if isinstance(knowledge_at, pd.Series) else None  # preserve event_id keying
    epoch = _to_epoch_seconds(knowledge_at)  # normalized int64 epoch seconds (fail-loud on bad input)
    labels = np.where(epoch < BOUNDARY, PRE, POST)  # vectorized half-open boundary test
    return pd.Series(labels, index=index, dtype=object)  # PRE/POST per event, keyed like the input


def apply_regime_mode(
    knowledge_at: "pd.Series",  # one row per labelled event, indexed by event_id; the values are knowledge times
    mode: str,  # 'split' | 'restrict' | 'mark' (EL58, the per-strategy regime_mode setting)
    target_regime: "str | None" = None,  # required iff mode == 'restrict'; PRE or POST (modeling choice 1)
) -> RegimeOutcome:
    """Apply the regime mode to a run's labelled events and return what the pipeline acts on.

    - ``mark`` (default): keep every event, tag each, flag the run cross-regime if the kept
      set spans the boundary. Runs across the break but never hides that it did (§13).
    - ``restrict``: keep only events in ``target_regime``; drop the rest and COUNT them (the
      coverage figure, never a silent drop). Single regime by construction, so never
      cross-regime.
    - ``split``: keep every event, tag each, and group the kept ids per regime so the caller
      can report results per regime; cross-regime iff both sides are present.

    Fails loud on a bad mode, a missing/extra ``target_regime``, or a bad target value — a
    misconfigured caller must never get a silently wrong partition (CLAUDE.md).
    """
    if mode not in REGIME_MODES:  # an unknown mode would otherwise fall through to a silent branch
        raise ValueError(f"mode must be one of {REGIME_MODES}, got {mode!r}")
    if not isinstance(knowledge_at, pd.Series):  # the per-event spine must carry the event_id index
        raise ValueError(f"knowledge_at must be a pd.Series indexed by event_id, got {type(knowledge_at).__name__}")
    if mode == "restrict":  # restrict needs to know WHICH regime to keep (modeling choice 1)
        if target_regime is None:  # no companion setting supplies it (§12 gap) -> the caller must pass it
            raise ValueError("mode 'restrict' requires target_regime (PRE or POST); §12 has no setting for it yet (J2 owes one)")
        if target_regime not in _REGIME_LABELS:  # only the two rule-based regimes exist in M3.5
            raise ValueError(f"target_regime must be one of {_REGIME_LABELS}, got {target_regime!r}")
    elif target_regime is not None:  # target_regime is meaningless for split/mark -> reject, do not ignore
        raise ValueError(f"target_regime is only valid for mode 'restrict', not {mode!r}")

    tags = tag_regimes(knowledge_at)  # PRE/POST per event, keyed by event_id (fail-loud on bad knowledge_at)

    if mode == "restrict":  # keep one regime, drop-and-count the rest
        keep = tags == target_regime  # boolean mask: the kept regime only
        dropped_count = int((~keep).sum())  # the dropped (counted) events outside the target regime
        kept_ids = tags.index[keep]  # the surviving event ids
        per_regime = {target_regime: kept_ids}  # one group by construction
        return RegimeOutcome(tags=tags, keep=keep, dropped_count=dropped_count, cross_regime=False, per_regime=per_regime)

    # split and mark both keep every event; they differ only in how the caller reports them.
    keep = pd.Series(True, index=tags.index, dtype=bool)  # keep all
    present = set(tags.unique())  # which regimes the kept set actually contains
    cross_regime = PRE in present and POST in present  # spans the boundary -> cross-regime flag
    per_regime = {label: tags.index[tags == label] for label in _REGIME_LABELS if label in present}  # group present regimes
    return RegimeOutcome(tags=tags, keep=keep, dropped_count=0, cross_regime=cross_regime, per_regime=per_regime)


def regimes_slot(window_start: int, window_end: int) -> "dict[str, list[tuple[int, int, str]]]":
    """Build the ``RunResult.regimes`` band intervals for a run window (EL59, the slot fill).

    Returns ``{REGIME_KIND: [(start, end, label), ...]}`` — the band(s) the export chart
    overlays. A window entirely on one side of the boundary yields one band; a
    boundary-spanning window yields two bands split AT the boundary (modeling choice 3). An
    inverted window (``window_end < window_start``) is a caller bug and raises ``ValueError``.
    A zero-length window (``window_end == window_start``) yields an empty band list under the
    key, never a fabricated zero-width band.
    """
    if window_end < window_start:  # an inverted window is a caller bug, not an empty run
        raise ValueError(f"window_end ({window_end}) must be >= window_start ({window_start})")
    start, end = int(window_start), int(window_end)  # int epoch seconds, the regimes-slot axis
    if end == start:  # a zero-length window covers no regime -> empty band list (not a 0-width band)
        return {REGIME_KIND: []}
    if end <= BOUNDARY:  # the whole window precedes (or ends exactly at) the boundary -> one PRE band
        return {REGIME_KIND: [(start, end, PRE)]}
    if start >= BOUNDARY:  # the whole window is at/after the boundary -> one POST band
        return {REGIME_KIND: [(start, end, POST)]}
    # The window straddles the boundary -> two bands split at BOUNDARY (the cross-regime case).
    return {REGIME_KIND: [(start, BOUNDARY, PRE), (BOUNDARY, end, POST)]}
