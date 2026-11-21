"""The multi-source fundamental feature channel (M3.75 FUND-3, §6.3 / §8).

The engine-side half of M3.75: a SECOND feature family that reads point-in-time fundamentals
(materialized as a fact-table slice by FUND-2, §6) as-of each trigger event's decision date, with
NO lookahead. It is accepted ALONGSIDE the locked ``FeatureDef`` (arch §7, Q3) — never a
discriminator bolted onto it — so the J1 pipeline stays frozen and a price/event-only strategy is
byte-unchanged (NFR-6). Three things live here:

  - ``as_of_select`` — the in-container two-stage VECTORIZED as-of join (§6.3). The single correctness
    spine, shared by the feature path here and (in its SQL twin ``reads.read_asof``) the server-side
    screen. It implements the DECISIONS #92 rule: choose the reporting period by its ``period_end``
    (the greatest period_end whose PUBLICATION date is on or before the decision date), then the
    vintage within that period. NOT a plain ``merge_asof`` on publication date (the Rule-P bug #92
    corrected, which returns a stale older period on a late/out-of-order first filing).
  - ``FundamentalFeatureDef`` — a strategy's frozen declaration of one fundamental feature (§8.1).
  - ``provide_fundamental_features`` — the as-of reader, the sibling of ``features.provide_features``
    (§8.2). Its per-event output reuses the locked features-dict contract (one full-length frame per
    feature, keyed by ``event_id``, single ``value`` column), so ``signal_filter`` is untouched.

Why ``as_of_select`` carries NO explicit flow-vs-instant scope (the §6.3 ``scope`` arg): the concept
vocabulary that classifies a concept as flow or instant lives in the DASHBOARD BACKEND
(``events/fundamentals/concepts.py``), NOT importable from this strategies package. It does not need
to be: the store's §2.5 parse-time scope rule already produced the correct in-scope rows per concept
— a FLOW concept's slice rows are FY-only (quarterly flows were dropped as deferred-scope), an
INSTANT concept's are every period — so selecting the period by greatest ``period_end`` over the
concept's rows yields FY-for-flow / latest-for-instant AUTOMATICALLY, with no fp filter. The SQL twin
``reads.read_asof`` keeps an explicit FY guard because it HAS the vocabulary; this side relies on the
store invariant instead. Both are correct; this is the documented deviation from the literal §6.3
``scope`` parameter (the deviation-documentation rule), made because the vocabulary is not importable
across the dashboard-backend / strategies-package boundary.

Pure pandas/numpy at import — no vbt. ``provide_fundamental_features`` and ``as_of_select`` run
inside the no-network research-runner container over the mounted slice (R11/NFR-4), the same place
``features.provide_features`` runs.
"""
from __future__ import annotations  # lazy annotations, consistent with the rest of base/

from dataclasses import dataclass  # the feature declaration is a plain frozen record
from typing import Callable, Optional  # the FundamentalFeatureDef.combine / staleness annotations

import numpy as np  # the vectorized as-of merges + the present/stale masks
import pandas as pd  # the materialized-slice frames + merge_asof

# Reuse the event feature layer's knowledge_at -> epoch-seconds normalizer VERBATIM (reuse over
# reinvention, CLAUDE.md): its NaT / tz-naive guards (features.py) are load-bearing and must match
# the store/labeling axis exactly; duplicating them here would risk drift. Same package, same axis.
from strategies.base.features import _to_epoch_seconds  # column normalizer (datetime64 or int epoch -> int64 s)

# The two no-lookahead vintage modes selectable within the chosen period (the §5.2 reads a/b). Mirror
# the reads.py names exactly so the SQL form and the vectorized form name the same concept.
VINTAGE_AS_KNOWN = "as_known_on_date"            # read (b), the backtest default (Q2)
VINTAGE_AS_REPORTED = "as_originally_reported"   # read (a), the publication vintage
_VINTAGES = (VINTAGE_AS_KNOWN, VINTAGE_AS_REPORTED)

# The slice columns the as-of join reads. A slice missing any of these is a programming error (a
# malformed mount), not a coverage gap -> fail loud.
_SLICE_READ_COLUMNS = ("cik", "concept", "period_end", "filing_date", "value")

# The fundamentals-specific missingness vocabulary: a STRICT SUBSET of the event feature layer's
# _MISSINGNESS_MODES (features.py, ("zero","null","exclude")). 'zero' is REJECTED because a reported
# zero (zero equity, zero EPS) is a real, meaningful fundamental value, distinct from "not reported";
# substituting zero would itself be a silent-wrong-number (Q6, §8.1).
_FUNDAMENTAL_MISSINGNESS_MODES = ("null", "exclude")

# The fiscal-scope vocabulary on the declaration. 'auto' is the only live value at M3.75 (FY for a
# flow concept, the latest period for an instant — resolved by the store invariant, see the module
# docstring); 'ttm' is RESERVED for the deferred trailing-twelve-month extension (Q8, §12) and is
# rejected at construction so a strategy cannot silently declare an unbuilt read.
_FISCAL_SCOPES_LIVE = ("auto",)
_FISCAL_SCOPES_RESERVED = ("ttm",)

_SECONDS_PER_DAY = 86_400  # staleness is declared in days; the slice carries epoch seconds


def as_of_select(
    slice_df: "pd.DataFrame",      # the materialized fundamentals fact-table slice (all CIKs, concepts, vintages)
    queries: "pd.DataFrame",       # one row per (company, decision date): columns ['cik', 'decision_date'] (epoch s)
    concept: str,                  # which concept to read
    vintage: str = VINTAGE_AS_KNOWN,
) -> "pd.DataFrame":
    """Vectorized point-in-time read of one ``concept`` as-of each query's decision date (§6.3).

    The §6.3 ``as_of_select(slice_df, decision_dates, concept, scope, vintage)`` made concrete: the
    decision-date set is carried as a ``(cik, decision_date)`` frame so ONE grouped ``merge_asof``
    (``by='cik'``) serves every company at once — no per-decision-date Python loop (the §6.3
    quadratic guard) and no per-CIK loop either. Returns a frame aligned to ``queries.index`` with
    columns ``['value', 'filing_date']``; both are NaN where the company has no in-scope fact public
    by its decision date (an honest missing-as-of, never a fake zero, Q6). The ``scope`` arg is
    elided on purpose — the store invariant supplies it (module docstring).

    Two vectorized stages (the DECISIONS #92 correctness spine):
      1. Per ``(cik, period_end)`` reduce to its PUBLICATION date (the earliest ``filing_date``).
         Then per cik carry a RUNNING ARGMAX of ``period_end`` (``cummax``) down publication-date
         order; a backward ``merge_asof`` of each decision date against publication date reads that
         running-argmax period. The periods public by D are exactly the publication-date prefix
         STRICTLY BEFORE D's UTC day (the Fix 4 same-day rule), and the running max of ``period_end``
         over that prefix is the §5.2 chosen period — defeating BOTH the restatement trap (an older
         period's restatement does not raise its ``period_end``) and the late/out-of-order first
         filing (a more-recently-published but economically-staler period has a smaller
         ``period_end``). The right side is sorted ``(publication_date, chosen_period_end)`` so a
         backward merge on an equal-publication_date tie reads the GREATEST period (Fix 2), not
         whatever a non-stable single-key sort left last.
      2. Choose the vintage within the chosen period: ``as_known_on_date`` -> the latest filing
         strictly before D's day (a second backward ``merge_asof`` on ``filing_date``, Fix 4);
         ``as_originally_reported`` -> that period's earliest filing (the publication vintage).
    """
    if vintage not in _VINTAGES:                                   # a bad vintage is a caller bug; fail loud
        raise ValueError(f"vintage must be one of {_VINTAGES}; got {vintage!r}")
    for col in ("cik", "decision_date"):                           # the query frame's required columns
        if col not in queries.columns:
            raise ValueError(f"queries is missing the required column {col!r}")

    # The aligned all-NaN template (the empty / missing result). Float columns so a missing read is
    # NaN, never a coerced 0 or a garbage epoch. Returned as-is for the empty / concept-absent cases.
    result = pd.DataFrame(
        {"value": np.full(len(queries), np.nan), "filing_date": np.full(len(queries), np.nan)},
        index=queries.index,
    )
    if len(queries) == 0:                                          # no triggers -> nothing to read
        return result

    for col in _SLICE_READ_COLUMNS:                                # a malformed slice is a programming error
        if col not in slice_df.columns:
            raise ValueError(f"slice_df is missing the required column {col!r}")

    conc = slice_df.loc[slice_df["concept"] == concept, ["cik", "period_end", "filing_date", "value"]]
    if len(conc) == 0:                                             # this concept is not in the slice -> all missing
        return result
    conc = conc.copy()
    conc["cik"] = conc["cik"].astype("int64")                      # the merge keys are epoch-second / id ints
    conc["period_end"] = conc["period_end"].astype("int64")
    conc["filing_date"] = conc["filing_date"].astype("int64")

    # Carry a stable 0..n-1 position so the result realigns to queries.index after the sorts the
    # merges require (merge_asof needs both sides globally sorted by the on-key).
    q = queries[["cik", "decision_date"]].copy()
    q["cik"] = q["cik"].astype("int64")
    q["decision_date"] = q["decision_date"].astype("int64")
    # The same-day gate (Fix 4): floor each decision date to its UTC day ONCE here; both merges below
    # key on this `dod` column (with allow_exact_matches=False) so a filing made the SAME calendar day
    # as an intraday D is treated as not-yet-known. `dod` is monotonic in decision_date, so a
    # decision-date sort still satisfies merge_asof's sorted-on-key precondition.
    q["dod"] = q["decision_date"] - q["decision_date"] % _SECONDS_PER_DAY
    q["__row"] = np.arange(len(q), dtype="int64")                 # position in the original queries order

    # --- Stage 1: one row per (cik, period_end) carrying its publication date (earliest filing). ---
    periods = (
        conc.groupby(["cik", "period_end"], as_index=False)["filing_date"].min()
        .rename(columns={"filing_date": "publication_date"})
    )
    # --- Stage 2: per cik, running argmax of period_end down publication-date order, then asof. ---
    periods = periods.sort_values(["cik", "publication_date", "period_end"]).reset_index(drop=True)
    periods["chosen_period_end"] = periods.groupby("cik")["period_end"].cummax()  # max period_end among pubs so far
    q_sorted = q.sort_values("dod").reset_index(drop=True)                         # left side sorted by the on-key (the floored date, Fix 4)
    periods_asof = (                                                             # right side sorted by the on-key
        periods[["cik", "publication_date", "chosen_period_end"]]
        # Fix 2: order ties on publication_date by chosen_period_end (ascending) so the backward merge's
        # last-match is the GREATEST period (Rule-C), not whatever numpy's non-stable single-key sort
        # happened to leave last on an equal-publication_date run.
        .sort_values(["publication_date", "chosen_period_end"]).reset_index(drop=True)
    )
    matched = pd.merge_asof(
        q_sorted, periods_asof,
        left_on="dod", right_on="publication_date",
        by="cik", direction="backward", allow_exact_matches=False,  # last period published STRICTLY before D's day (Fix 4)
    )
    # matched.chosen_period_end is NaN where the cik has no period public by D (no facts, or all
    # published after D) -> that query stays missing.

    # --- Stage 3: choose the vintage within the chosen period. ---
    has_period = matched["chosen_period_end"].notna()              # the queries that found an as-of period
    if not has_period.any():                                      # nobody had a fact public by their date
        return result
    sel = matched.loc[has_period].copy()                          # only the resolvable queries go through stage 3
    sel["chosen_period_end"] = sel["chosen_period_end"].astype("int64")

    if vintage == VINTAGE_AS_KNOWN:                               # read (b): latest filing strictly before D's day (Fix 4)
        # A second backward asof on filing_date, grouped by (cik, period_end). The chosen period was
        # selected because its publication (min filing) is strictly before D's day, so a vintage
        # satisfying filing_date < start_of_day(D) always exists here.
        vintages = (
            conc.sort_values("filing_date")[["cik", "period_end", "filing_date", "value"]]
            .reset_index(drop=True)
        )
        sel = sel.sort_values("dod").reset_index(drop=True)
        picked = pd.merge_asof(
            sel, vintages,
            left_on="dod", right_on="filing_date",
            left_by=["cik", "chosen_period_end"], right_by=["cik", "period_end"],
            direction="backward", allow_exact_matches=False,       # latest filing STRICTLY before D's day (Fix 4)
        )
    else:                                                        # read (a): the period's earliest (publication) filing
        # The publication vintage is D-independent: the min-filing record per (cik, period_end). A
        # plain merge on the chosen period key (no asof) reads it.
        publication = (
            conc.sort_values("filing_date")
            .groupby(["cik", "period_end"], as_index=False)
            .first()[["cik", "period_end", "filing_date", "value"]]
        )
        picked = sel.merge(
            publication,
            left_on=["cik", "chosen_period_end"], right_on=["cik", "period_end"],
            how="left",
        )

    # Scatter the picked (value, filing_date) back to the original queries order via __row.
    out_value = np.full(len(queries), np.nan)
    out_filed = np.full(len(queries), np.nan)
    rows = picked["__row"].to_numpy(dtype="int64")               # original positions of the resolved queries
    out_value[rows] = picked["value"].to_numpy(dtype="float64")  # the as-of value
    out_filed[rows] = picked["filing_date"].to_numpy(dtype="float64")  # its filing_date (the age axis, R14)
    return pd.DataFrame({"value": out_value, "filing_date": out_filed}, index=queries.index)


@dataclass(frozen=True)
class FundamentalFeatureDef:
    """A strategy's declaration of one fundamental feature (read as-of a decision date, no lookahead).

    Accepted ALONGSIDE ``FeatureDef`` (arch §7, Q3): ``EventStrategy.feature_defs`` returns a union
    list and the feature layer dispatches by type. Its per-event output reuses the locked
    features-dict contract (``features.provide_features`` shape), so ``signal_filter`` is untouched.
    Frozen + validated at construction (``__post_init__``) so a malformed declaration fails loud at
    import time, the ``FeatureDef`` / ``ConceptSpec`` precedent.
    """

    name: str                                    # the feature's key in StrategyInput.features
    concepts: tuple[str, ...]                     # one or more concept names, each read as-of D independently
    combine: "Optional[Callable[[dict[str, float]], float]]" = None
    # combine: applied to {concept_name: as_of_value} -> the feature value (a ratio / expression).
    # None requires exactly one concept (the raw single-concept as-of value); a multi-concept def
    # MUST supply a combine. The most general mechanism (R13, §8.6): any multi-concept quantity is a
    # declared feature, evaluated in-container (never serialized across the mount boundary), not
    # strategy-side reconstruction.
    fiscal_scope: str = "auto"                    # 'auto' only at M3.75; 'ttm' reserved (Q8), rejected at construction
    missingness: str = "exclude"                  # 'null' | 'exclude' ONLY (NOT 'zero'); default 'exclude' (Q6)
    staleness_max_days: "Optional[int]" = None    # None = no cap, age exposed (Q5); when set, an as-of fact older than
    #                                               this many days before D is treated as missing -> the missingness mode

    def __post_init__(self) -> None:
        """Fail loud on a malformed declaration (a strategy-authoring footgun caught at construction)."""
        if not self.name:                                                    # an empty feature name is a bug
            raise ValueError("FundamentalFeatureDef.name must be non-empty")
        if not self.concepts:                                                # a feature must read at least one concept
            raise ValueError(f"FundamentalFeatureDef {self.name!r} declares no concepts")
        if self.combine is None and len(self.concepts) != 1:                 # None combine -> single concept only (§8.1)
            raise ValueError(
                f"FundamentalFeatureDef {self.name!r} has {len(self.concepts)} concepts but no combine; "
                f"a multi-concept feature must supply combine (None means the single raw concept value)"
            )
        if self.missingness not in _FUNDAMENTAL_MISSINGNESS_MODES:           # 'zero' (and any typo) rejected (Q6)
            raise ValueError(
                f"FundamentalFeatureDef {self.name!r} missingness {self.missingness!r} must be one of "
                f"{_FUNDAMENTAL_MISSINGNESS_MODES} (a reported zero is a real value, so 'zero' is forbidden, Q6)"
            )
        if self.fiscal_scope in _FISCAL_SCOPES_RESERVED:                     # 'ttm' is declared-but-unbuilt at M3.75
            raise ValueError(
                f"FundamentalFeatureDef {self.name!r} fiscal_scope {self.fiscal_scope!r} is reserved for the "
                f"deferred trailing-twelve-month extension (Q8); only {_FISCAL_SCOPES_LIVE} ships at M3.75"
            )
        if self.fiscal_scope not in _FISCAL_SCOPES_LIVE:                     # any other value is a typo
            raise ValueError(
                f"FundamentalFeatureDef {self.name!r} fiscal_scope {self.fiscal_scope!r} must be one of "
                f"{_FISCAL_SCOPES_LIVE} at M3.75"
            )
        if self.staleness_max_days is not None and self.staleness_max_days <= 0:  # a non-positive cap is meaningless
            raise ValueError(
                f"FundamentalFeatureDef {self.name!r} staleness_max_days must be a positive int or None; "
                f"got {self.staleness_max_days!r}"
            )


def _trigger_queries(events: "dict[str, pd.DataFrame]") -> "pd.DataFrame":
    """Build the trigger-event query frame: every row of every slice, unioned by ``event_id`` (§8.2).

    The fundamental feature has NO ``event_type`` field (unlike ``FeatureDef``), so it spans ALL the
    strategy's declared trigger events across every type. Reads ``event_id``, ``subject_cik``, and
    ``knowledge_at`` from each materialized slice (the slices carry ``subject_cik`` — the
    materializer's join column, ``output_schemas`` — which the assembled ``events_frame`` does NOT,
    so the read is from the slices, not the frame). Returns a frame indexed by ``event_id`` with
    ``['cik', 'decision_date']``, deduped by ``event_id`` (the spine surrogate is globally unique, so
    a duplicate across types cannot occur; the dedup is a belt-and-suspenders guard).
    """
    rows: list[pd.DataFrame] = []                                  # one mini-frame per event-type slice
    for slice_df in events.values():                              # union across every declared event type
        if slice_df is None or len(slice_df) == 0:               # an empty / absent slice contributes no triggers
            continue
        for col in ("event_id", "subject_cik", "knowledge_at"):  # the columns the query needs
            if col not in slice_df.columns:                      # a malformed slice is a programming error
                raise ValueError(f"event slice is missing the required column {col!r} (the fundamental reader needs it)")
        rows.append(pd.DataFrame({
            "event_id": slice_df["event_id"].to_numpy(),
            "cik": slice_df["subject_cik"].to_numpy(dtype="int64"),          # the trigger company (the as-of CIK)
            "decision_date": _to_epoch_seconds(slice_df["knowledge_at"]),    # D = the event's public-knowledge time (epoch s)
        }))
    if not rows:                                                  # no events at all -> an empty query frame
        return pd.DataFrame(columns=["cik", "decision_date"], index=pd.Index([], name="event_id"))
    allq = pd.concat(rows, ignore_index=True)                    # the superset of trigger events
    allq = allq.drop_duplicates(subset="event_id")              # event_id is unique; guard a degenerate duplicate
    return allq.set_index("event_id")[["cik", "decision_date"]]  # keyed by event_id, the output spine


def provide_fundamental_features(
    fundamental_defs: "list[FundamentalFeatureDef]",
    events: "dict[str, pd.DataFrame]",          # the materialized event slices, keyed by event type (carry subject_cik + knowledge_at)
    fundamentals: "pd.DataFrame",               # the materialized fundamentals fact slice (StrategyInput.fundamentals, §6)
) -> "tuple[dict[str, pd.DataFrame], set[int]]":
    """Read each declared fundamental feature as-of every trigger event, no lookahead (§8.2).

    Returns ``(features, exclude_ids)``:
      - ``features`` -> ``{feature_name: per-event frame}``, each FULL-LENGTH (one row per trigger
        event, keyed by ``event_id``, a single ``value`` column), with a missing-as-of value written
        as NaN under BOTH missingness modes — byte-identical in SHAPE to ``provide_features`` output
        (the contract that keeps ``signal_filter`` untouched). "Same shape" means the same per-event,
        ``event_id``-keyed, single-``value``-column frame, not an identical row count: a fundamental
        frame spans all trigger events across every type, so it can have a different row count than an
        event-feature frame (safe because everything downstream aligns by the ``event_id`` index,
        never positionally, §8.2/§8.5).
      - ``exclude_ids`` -> the ``event_id``s missing-as-of under an ``exclude``-mode def, for the
        pipeline drop-and-count (§8.5). Empty when no def uses ``exclude``.

    Per def, per concept: the §6.3 two-stage as-of read at D (vintage = as-known-on-D, Q2), capturing
    the value AND its ``filing_date``. An event's value is MISSING if any required concept has no
    as-of value, OR (when ``staleness_max_days`` is set) any concept's fact is older than that many
    days before D. Otherwise the value is ``combine({concept: value})`` (or the single concept's value
    when ``combine`` is None). ``combine`` runs ONLY when all concepts are present, so it never sees a
    partial / None input (§8.6).
    """
    if not fundamental_defs:                                      # the strict NFR-6 no-op: a strategy that declares no
        return {}, set()                                         # fundamental feature never touches the as-of path at all
    queries = _trigger_queries(events)                            # the event_id-keyed (cik, decision_date) superset
    features: dict[str, pd.DataFrame] = {}                        # feature_name -> full-length per-event frame
    exclude_ids: set[int] = set()                                # event_ids dropped under an exclude-mode def

    seen_names: set[str] = set()                                  # guard a duplicate feature name across defs
    for fd in fundamental_defs:                                   # one frame per declared fundamental feature
        # Re-validate the live invariants even though __post_init__ already did (a hand-built frozen
        # instance could in principle be constructed via object.__setattr__; cheap belt-and-suspenders).
        if fd.missingness not in _FUNDAMENTAL_MISSINGNESS_MODES:  # 'zero'/typo rejected (Q6)
            raise ValueError(f"FundamentalFeatureDef {fd.name!r} has missingness {fd.missingness!r}; must be one of {_FUNDAMENTAL_MISSINGNESS_MODES}")
        if fd.name in seen_names:                                # two defs with the same name would silently overwrite
            raise ValueError(f"duplicate FundamentalFeatureDef name {fd.name!r}")
        seen_names.add(fd.name)

        # Read each required concept as-of D over the slice (vectorized; no per-event loop here).
        per_concept_value: dict[str, np.ndarray] = {}            # concept -> as-of value per query (NaN = missing)
        per_concept_filed: dict[str, np.ndarray] = {}           # concept -> the chosen fact's filing_date per query
        for concept in fd.concepts:
            asof = as_of_select(fundamentals, queries, concept, vintage=VINTAGE_AS_KNOWN)
            per_concept_value[concept] = asof["value"].to_numpy(dtype="float64")
            per_concept_filed[concept] = asof["filing_date"].to_numpy(dtype="float64")

        n = len(queries)                                         # the full trigger count (the frame length)
        decision = queries["decision_date"].to_numpy(dtype="float64") if n else np.empty(0)

        # present_mask: every required concept has an as-of value (not NaN). Start all-True, AND each.
        present = np.ones(n, dtype=bool)
        for concept in fd.concepts:
            present &= ~np.isnan(per_concept_value[concept])
        # staleness: when a bound is set, ANY concept whose fact is older than the bound before D
        # makes the event missing (the as-of read already exposed the age via filing_date, R14/Q5).
        if fd.staleness_max_days is not None:
            max_age_s = fd.staleness_max_days * _SECONDS_PER_DAY  # the cap, in seconds
            for concept in fd.concepts:
                filed = per_concept_filed[concept]
                # age = D - filing_date (seconds); NaN filing_date is already not-present, so guard nan.
                age = decision - filed
                too_old = np.where(np.isnan(age), False, age > max_age_s)  # over-age (NaN -> handled by present)
                present &= ~too_old

        # Compute the per-event value: combine for present events, NaN for missing. combine never sees
        # a missing concept (present gates it), so it cannot receive a partial / None input (§8.6).
        value = np.full(n, np.nan)                               # missing -> NaN under both modes (§8.2)
        present_idx = np.nonzero(present)[0]                     # positions of the resolvable events
        if fd.combine is None:                                   # single concept (validated) -> the raw as-of value
            only = fd.concepts[0]
            value[present_idx] = per_concept_value[only][present_idx]
        else:                                                    # multi-concept (or declared) -> apply combine per event
            for i in present_idx:
                inputs = {c: float(per_concept_value[c][i]) for c in fd.concepts}  # all present (gated)
                # Fix 3: a combine that divides by a REPORTED zero (a real Q6 value) is genuinely
                # undefined for that event -> honest-missing, not a crash. Catch ONLY ZeroDivisionError
                # (the data condition); any other exception is an author bug in the expression and must
                # fail loud (CLAUDE.md). np.isfinite also folds a numpy inf/nan (np.divide-by-zero,
                # overflow) into missing. present[i]=False routes the event through the existing missing
                # path below (value stays NaN; excluded under exclude mode). present_idx was materialized
                # before this loop, so mutating present[i] here is safe.
                try:
                    result = float(fd.combine(inputs))          # the strategy's expression, evaluated in-container
                except ZeroDivisionError:                       # divide by a reported zero -> missing (Q6), not a crash
                    present[i] = False
                    continue
                if not np.isfinite(result):                     # inf/nan is "not a real value" -> missing
                    present[i] = False
                    continue
                value[i] = result                               # a finite ratio -> the feature value
        frame = pd.DataFrame({"value": value}, index=queries.index)  # full-length, event_id-keyed, single 'value' col
        features[fd.name] = frame

        # exclude-mode: the missing event_ids feed the pipeline drop-and-count (§8.5). null-mode leaves
        # the NaN in place for signal_filter (no event dropped here).
        if fd.missingness == "exclude":
            missing_idx = np.nonzero(~present)[0]               # the missing-as-of events for THIS def
            exclude_ids.update(int(eid) for eid in queries.index.to_numpy()[missing_idx])

    return features, exclude_ids
