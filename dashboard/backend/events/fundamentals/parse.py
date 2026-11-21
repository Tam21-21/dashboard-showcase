"""CompanyFacts JSON -> normalized FactRow parser + per-company coverage (M3.75 FUND-1 §2.4/§2.5).

The normalization layer (requirements R2). It turns one company's CompanyFacts document into
normalized core facts (one row per kept filing) plus a coverage contribution (R6). The parser
is the single owner of the rules that guarantee the store's five-tuple uniqueness (§1.2):
which forms count as periodic, which fiscal periods are core, the units boundary, and — the
load-bearing one — the duration rule that collapses a flow concept's multiple per-filing
durations to exactly ONE canonical-annual row per (cik, concept, fiscal_period_code,
period_end, filing_date).

A note on the "dimensional drop" the brief (§2.5) names, reconciled against the real data
this build grounded against (the FUND-1 #77 gate's six real companies): the CompanyFacts API
returns ONLY the undimensioned, entity-level consolidated facts — a companyfacts record carries
no XBRL segment/dimension qualifier field at all (the verified record keys are exactly
{start, end, val, accn, fy, fp, form, filed, frame}). So there is no dimensional member to
detect-and-drop from this source; the dimensional_dropped counter is retained in the coverage
report (structurally 0 from companyfacts) for a future dimensional source, and the REAL
multiple-records-per-period problem — duration ambiguity for flows — is what the §2.5 rule
disambiguates, with the five-tuple collision quarantine as the fail-loud backstop on any
genuinely indistinguishable pair. This is the honest reading of §2.5 against the source; it is
documented here per the contract's deviation-documentation rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .concepts import ConceptSpec, FUNDAMENTAL_CONCEPTS

# The periodic forms the store KEEPS (§2.4 step 2): the base annual/quarterly forms AND their
# amendments. The base forms are the spike's verified periodic set; the /A amendment forms are
# kept on the authority of requirements R4 (amendment-borne restatements live in the vintage
# history). Any other form (8-K, S-1, ...) is dropped-and-reported.
PERIODIC_FORMS: frozenset[str] = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

# The core fiscal-period codes (§2.3). XBRL also emits CY / half-year / cover-page edge codes;
# any fp outside this set is dropped-and-reported, so the store CHECK never rejects a fact that
# reached the write path.
CORE_FISCAL_PERIODS: frozenset[str] = frozenset({"Q1", "Q2", "Q3", "FY"})

# The canonical length in days of each flow duration class. A filing can carry several
# durations under one (end, filed) - the annual figure plus a quarter or a year-to-date stub; the
# parser keeps the record whose duration is nearest its class's length. A 52-vs-53-week fiscal year
# (364-371 days) lands well inside the nearest-to-365 tie-break against any quarterly (~90d) or YTD
# (~180-270d) duration carried under the same end.
_CANONICAL_ANNUAL_DAYS = 365
_CANONICAL_QUARTER_DAYS = 91
_CANONICAL_HALF_DAYS = 182
_CANONICAL_NINE_MONTH_DAYS = 273
_SECONDS_PER_DAY = 86_400

# The annual-duration band (days) that identifies a flow concept's ANNUAL figure.
#
# CRITICAL XBRL fact, verified against the real CompanyFacts data this build grounded against
# (Apple's 10-K, the #77 gate): the XBRL ``fp`` field is the FILING's fiscal period, NOT the
# individual fact's period. A 10-K (fp='FY') carries the annual figure AND every quarterly
# breakdown AND prior-year comparatives, and ALL of them are tagged fp='FY' (the filing's
# period). So ``fp == 'FY'`` alone does NOT mean "this fact is the annual figure" - Apple's
# 2010 10-K carries ~90-day quarterly net-income facts all tagged fp='FY'. The ONLY reliable
# signal that a flow fact is the 12-month annual figure is its DURATION. The brief's §2.5 rule
# ("nearest 365 among records sharing a period_end") misses this because the quarterly
# breakdowns sit at DIFFERENT period_ends; the duration band below is the data-grounded
# refinement that keeps only the true annual record. A normal fiscal year is 52-53 weeks
# (364-371 days) or 365-366 calendar days. Lane FA1b-3 replaced the old margin band [340, 380] with
# the band measured on the 401 held companies' real filings: [362, 370] days (end minus start;
# 363 for a 52-week year to 370 for a 53-week year, the start moved to 362 for a 52-week year counted
# Monday to Saturday). It excludes the 9-month YTD (~273d), half-year (~182d) and quarterly (~91d)
# sub-annual records carried under fp='FY'. A genuinely sub-annual transition-stub year is
# conservatively deferred-scope (reported, never emitted as an annual).
# Provenance (NFR-5): docs/research_lab/parameter_provenance.md `fundamentals.annual_duration_band`
# (DECISIONS #479 item 10 and #481 item 8; frozen, a data-shape classifier, not a tunable backtest knob).
_ANNUAL_DURATION_MIN_DAYS = 362
_ANNUAL_DURATION_MAX_DAYS = 370

# The three sub-annual bands (days) a 10-Q flow record may fall in (lane FA1c, re-set by lane FA1b-3).
# Each is a data-shape classifier like the annual band, measured on the 401 held companies' real
# filings (end minus start) and frozen by DECISIONS #479 item 10 and #481 item 8: quarters 83 to 111
# (the 12 to 16 week calendars, Kroger's 16-week first quarter at 111), halves 167 to 195, nine months
# 251 to 279. Provenance: docs/research_lab/parameter_provenance.md `fundamentals.quarter_duration_band`,
# `fundamentals.half_duration_band` and `fundamentals.nine_month_duration_band`.
_QUARTER_DURATION_MIN_DAYS = 83
_QUARTER_DURATION_MAX_DAYS = 111
_HALF_DURATION_MIN_DAYS = 167
_HALF_DURATION_MAX_DAYS = 195
_NINE_MONTH_DURATION_MIN_DAYS = 251
_NINE_MONTH_DURATION_MAX_DAYS = 279

# class name -> (band min, band max, canonical length, label used in a quarantine message)
_DURATION_CLASSES: dict[str, tuple[int, int, int, str]] = {
    "annual": (_ANNUAL_DURATION_MIN_DAYS, _ANNUAL_DURATION_MAX_DAYS, _CANONICAL_ANNUAL_DAYS, "FY"),
    "quarter": (_QUARTER_DURATION_MIN_DAYS, _QUARTER_DURATION_MAX_DAYS, _CANONICAL_QUARTER_DAYS, "quarter"),
    "half": (_HALF_DURATION_MIN_DAYS, _HALF_DURATION_MAX_DAYS, _CANONICAL_HALF_DAYS, "half-year"),
    "nine_month": (_NINE_MONTH_DURATION_MIN_DAYS, _NINE_MONTH_DURATION_MAX_DAYS, _CANONICAL_NINE_MONTH_DAYS, "nine-month"),
}

# The classes each filing's fiscal period may carry for a flow. A flow outside its filing's classes
# (such as a 10-K's quarter) is deferred_scope_dropped; Q4 is derived later (FA2), never parsed.
_FLOW_CLASSES_BY_FP: dict[str, tuple[str, ...]] = {
    "FY": ("annual",),
    "Q1": ("quarter",),
    "Q2": ("quarter", "half"),
    "Q3": ("quarter", "nine_month"),
}


def _iso_to_epoch(s: str) -> int:
    """Convert an XBRL 'YYYY-MM-DD' date string to epoch seconds UTC (midnight).

    XBRL dates are calendar dates (no time, no offset). The store keys on integer epoch
    seconds (the spike compared ISO strings lexicographically, verify_edgar_xbrl_v2.py:167;
    the store replaces that with integer comparison). Midnight UTC is the canonical instant for
    a calendar date here — consistent across every fact, so the < / max comparisons the as-of
    read relies on are exact. Raises ValueError on a non-ISO string (caller quarantines).
    """
    d = datetime.fromisoformat(s)                                 # 'YYYY-MM-DD' -> naive datetime at 00:00
    return int(d.replace(tzinfo=timezone.utc).timestamp())        # pin to UTC midnight -> epoch seconds


@dataclass(frozen=True)
class FactRow:
    """One normalized core fact, ready to write into research_fundamental_facts.

    Carries every column the store needs except the surrogate fact_id, the batch_id (the write
    path stamps it), and ingested_at (the write path's wall-clock). period_start is None for an
    instant concept (a balance-sheet stock has no duration).
    """

    cik: int                                    # the SEC company id
    concept: str                                # the normalized internal concept name (a value, NFR-11)
    taxonomy: str                               # the XBRL taxonomy the tag came from ('us-gaap' | 'dei')
    xbrl_tag: str                               # the raw XBRL tag (records the shares fallback choice)
    unit: str                                   # the XBRL units key ('USD' | 'shares' | 'USD/shares')
    fiscal_year: int                            # the XBRL 'fy'
    fiscal_period_code: str                     # the XBRL 'fp' (one of CORE_FISCAL_PERIODS)
    period_start: int | None                    # epoch s UTC, the XBRL 'start'; None for instant concepts
    period_end: int                             # epoch s UTC, the XBRL 'end' (the reporting-period end)
    filing_date: int                            # epoch s UTC, the XBRL 'filed' — the knowledge/version axis
    value: float                                # the XBRL 'val'
    source_form: str                            # the XBRL 'form' as-seen
    accession_number: str | None                # the XBRL 'accn', for lineage


@dataclass
class CoverageContribution:
    """One company's contribution to the §3.4 coverage report (R6).

    The write path (write.py) aggregates these across all CIKs in a batch into the
    batch-level quality_report. ``normalize_quarantines`` carries the genuine-ambiguity cases
    the write path turns into research_fundamental_quarantines rows.
    """

    cik: int
    concepts_found: list[str] = field(default_factory=list)       # concepts whose tag resolved in the doc
    concepts_missing: list[str] = field(default_factory=list)     # concepts with neither preferred nor fallback tag
    reports_ifrs_only: bool = False                              # the doc carries ifrs-full tags and NO us-gaap (a foreign
                                                                 # private issuer our us-gaap concepts cannot resolve, ANA-A)
    shares_tag: str = "none"                                      # 'dei:...' | 'us-gaap:...' | 'none' (the R6 fallback signal)
    non_usd_units_seen: list[str] = field(default_factory=list)   # foreign-currency unit keys seen (the units boundary)
    counts: dict[str, int] = field(default_factory=lambda: {      # the per-company drop accounting
        "facts_emitted": 0,             # core rows produced
        "non_usd_dropped": 0,           # records under a non-matching unit key (e.g. EUR)
        "non_periodic_dropped": 0,      # records on a non-periodic form (8-K, S-1, ...)
        "non_core_fp_dropped": 0,       # records with an fp outside {Q1,Q2,Q3,FY}
        "deferred_scope_dropped": 0,    # flow records outside their filing's duration classes (a 10-K's quarter, a stub)
        "dimensional_dropped": 0,       # structurally 0 from companyfacts (undimensioned source); retained for a future source
    })
    # genuine-ambiguity cases (two indistinguishable core values for one five-tuple) -> quarantine rows
    normalize_quarantines: list[dict] = field(default_factory=list)


def _parsed_records(records: list[dict], spec: ConceptSpec, cov: CoverageContribution) -> list[dict]:
    """Apply the form / fp / scope filters to a tag's unit records; return the survivors.

    Each survivor is a small dict with the parsed epoch fields. Drops are counted into
    ``cov.counts``. For a FLOW concept a record survives only in a duration class its filing carries
    (FY: annual; Q1: quarter; Q2: quarter, half; Q3: quarter, nine months); any other flow is
    counted as deferred_scope_dropped. For an INSTANT concept every periodic fp
    survives with period_start=None.
    """
    survivors: list[dict] = []                                    # the kept, parsed records
    is_flow = spec.period_semantics == "flow"                     # flow -> FY annual-only; instant -> all periodic fps
    for rec in records:                                           # walk every fact under the selected unit
        form = rec.get("form")                                    # the filing form
        if form not in PERIODIC_FORMS:                            # keep only periodic forms + their amendments
            cov.counts["non_periodic_dropped"] += 1
            continue
        fp = rec.get("fp")                                        # the FILING's fiscal-period code (see the band note)
        if fp not in CORE_FISCAL_PERIODS:                         # keep only the four core periodic codes
            cov.counts["non_core_fp_dropped"] += 1
            continue
        # Parse the dates + value; a malformed record is an integrity issue, quarantine it.
        try:
            period_end = _iso_to_epoch(rec["end"])               # the reporting-period end
            filing_date = _iso_to_epoch(rec["filed"])            # the knowledge axis
            # period_start is present for durations (flows), absent for instants -> None
            raw_start = rec.get("start")
            period_start = _iso_to_epoch(raw_start) if raw_start is not None else None
            value = float(rec["val"])                            # the numeric value
            fiscal_year = int(rec["fy"])                         # the XBRL 'fy' (filing-relative; recorded, not trusted for scoping)
        except (KeyError, ValueError, TypeError) as exc:         # a record missing a required field / non-numeric val
            cov.normalize_quarantines.append({
                "concept": spec.name, "period_end": rec.get("end"), "filing_date": rec.get("filed"),
                "failure_message": f"malformed record for {spec.name}: {type(exc).__name__}: {exc}",
            })
            continue
        duration_class: str | None = None                        # the flow's duration class; None for an instant
        if is_flow:
            # The duration gate (the band notes above): the fact's DURATION, not the filing's fp, says
            # which figure it is. It must fall in a class its filing carries (FY: annual; Q1: quarter;
            # Q2: quarter or half; Q3: quarter or nine months). Anything else is a flow outside its
            # filing's classes (such as a 10-K's quarter) and is deferred-scope, reported.
            if period_start is None:                              # a flow with no duration has no class
                cov.counts["deferred_scope_dropped"] += 1
                continue
            duration_days = (period_end - period_start) // _SECONDS_PER_DAY
            duration_class = next(
                (c for c in _FLOW_CLASSES_BY_FP[fp]
                 if _DURATION_CLASSES[c][0] <= duration_days <= _DURATION_CLASSES[c][1]), None)
            if duration_class is None:
                cov.counts["deferred_scope_dropped"] += 1        # a flow outside its filing's classes (a 10-K's quarter, a stub)
                continue
        survivors.append({                                       # a clean, parsed, in-scope record
            "fp": fp, "fiscal_year": fiscal_year, "period_start": period_start,
            "period_end": period_end, "filing_date": filing_date, "value": value,
            "form": form, "accn": rec.get("accn"), "klass": duration_class,
        })
    return survivors


def _select_canonical(group: list[dict], spec: ConceptSpec, cov: CoverageContribution) -> dict | None:
    """Collapse a (fp, class, period_end, filing_date) group to ONE canonical record, or quarantine.

    The §2.5 uniqueness guarantee. For a FLOW concept the canonical record is the one whose
    duration is nearest its duration class's length (91, 182, 273 or 365); a tie between records with DIFFERENT values
    is a genuine ambiguity -> quarantine (fail loud, never silently pick). For an INSTANT concept
    there is no duration, so the group should hold one value; multiple DIFFERENT values is a
    genuine ambiguity -> quarantine. Records with the same value are deduped to one.
    """
    if len(group) == 1:                                          # the common case: exactly one record
        return group[0]
    distinct_values = {round(r["value"], 9) for r in group}      # values present in this five-tuple group
    if len(distinct_values) == 1:                                # several records, identical value -> dedup to one
        return group[0]
    if spec.period_semantics == "flow":                          # flow: disambiguate by duration nearest the class length
        klass = group[0]["klass"]                                # every record of a group shares one class
        _lo, _hi, class_days, label = _DURATION_CLASSES[klass]

        def _class_distance(r: dict) -> int:                     # |duration_days - class length| for a record
            days = (r["period_end"] - r["period_start"]) // _SECONDS_PER_DAY
            return abs(days - class_days)
        best = min(group, key=_class_distance)                   # the record nearest the class length
        best_distance = _class_distance(best)
        # Records equally near the class length AS the winner, but with a different value, are
        # a genuine ambiguity (two undimensioned values for one period/filing) -> quarantine.
        contenders = [r for r in group if _class_distance(r) == best_distance]
        if len({round(r["value"], 9) for r in contenders}) > 1:  # the tie is on value, not just count
            cov.normalize_quarantines.append({
                "concept": spec.name, "period_end": group[0]["period_end"], "filing_date": group[0]["filing_date"],
                "failure_message": (
                    f"ambiguous {label} {spec.name}: {len(contenders)} undimensioned records equally near the "
                    f"{label} length with different values {sorted(round(r['value'], 2) for r in contenders)}"
                ),
            })
            return None
        return best                                              # the unambiguous canonical record
    # instant: no duration to disambiguate; different values for one (fp, end, filed) is ambiguous
    cov.normalize_quarantines.append({
        "concept": spec.name, "period_end": group[0]["period_end"], "filing_date": group[0]["filing_date"],
        "failure_message": (
            f"ambiguous instant {spec.name}: {len(group)} records for one (period_end, filing_date) "
            f"with different values {sorted(round(v, 2) for v in distinct_values)}"
        ),
    })
    return None


def _sum_parts(group: list[dict], spec: ConceptSpec, cov: CoverageContribution) -> dict | None:
    """The sum of the parts one filing reports for one period (lane LK1e), or None when the group is quarantined.

    ``group`` holds only part records (no total tag was filed). Each part tag collapses to one record by
    _select_canonical (its own quarantine, then the whole group is skipped). Then the group is quarantined, never
    summed, when the parts come from different filings (their accessions differ: the sum would mix two filings) or
    when two parts carry the same non-zero amount (one balance-sheet line may be tagged twice, which would count
    it twice). The sum's tag is the part tags joined by "+" in the spec's parts order; fiscal year, form and
    accession are the first part's in that order.
    """
    by_tag: dict[str, list[dict]] = {}
    for r in group:
        by_tag.setdefault(r["tag"], []).append(r)
    picked: list[dict] = []
    for records in by_tag.values():
        record = _select_canonical(records, spec, cov)
        if record is None:                                       # _select_canonical quarantined this part
            return None
        picked.append(record)
    picked.sort(key=lambda r: r["rank"])                         # the spec's parts order
    first = picked[0]
    quarantine = {"concept": spec.name, "period_end": first["period_end"], "filing_date": first["filing_date"]}
    if len({r["accn"] for r in picked}) > 1:
        cov.normalize_quarantines.append({**quarantine, "failure_message": f"parts of {spec.name} from different filings"})
        return None
    amounts = [round(r["value"], 9) for r in picked if r["value"] != 0]
    if len(set(amounts)) < len(amounts):
        cov.normalize_quarantines.append({**quarantine, "failure_message": (
            f"two parts of {spec.name} report the same amount; one line may be tagged twice")})
        return None
    return {"value": sum(r["value"] for r in picked), "tag": "+".join(r["tag"] for r in picked),
            "taxonomy": first["taxonomy"], "fiscal_year": first["fiscal_year"], "form": first["form"],
            "accn": first["accn"]}


def parse_companyfacts(
    cik: int,
    doc: dict,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> tuple[list[FactRow], CoverageContribution]:
    """Parse one company's CompanyFacts document into normalized FactRows + a coverage contribution.

    ``concepts`` is injectable (defaults to the shipped core) so the §13.1 fifth-concept probe can
    pass a five-ConceptSpec tuple to exercise the additive path WITHOUT mutating the production
    constant (NFR-11). For each concept: resolve the first present (taxonomy, tag), select the
    matching unit (other units -> non_usd_dropped + non_usd_units_seen), filter to periodic forms
    and core fiscal periods, apply the flow/instant scope, then collapse each
    (fp, period_end, filing_date) group to one canonical row (or quarantine the genuine ambiguity).
    Every surviving filing is kept as its own FactRow (the vintage axis is filing_date, §5.1).
    """
    cov = CoverageContribution(cik=cik)                          # this company's coverage accumulator
    facts: list[FactRow] = []                                    # the emitted core rows
    all_facts_tree = doc.get("facts", {})                        # taxonomy -> tag -> {units: {unit: [records]}}

    # IFRS-only detection (ANA-A): a foreign private issuer (e.g. Taiwan Semiconductor, CIK 1046179)
    # files its CompanyFacts under the `ifrs-full` taxonomy with NO `us-gaap` namespace. Our concepts
    # resolve us-gaap tags, so EVERY concept goes honest-missing and zero facts are emitted — the same
    # surface as a never-fetched company, which is misleading. Record the signal here (the only place
    # that sees the taxonomy tree) so the fetch outcome can say "reports under IFRS — not supported
    # yet" honestly, instead of a generic "fetch this company first". Verified live against
    # data.sec.gov: TSM carries ifrs-full (334 tags) and no us-gaap. We do NOT map IFRS tags here (a
    # future concept-spec unit, OPEN.md) — this is detection only, never a fabricated value.
    cov.reports_ifrs_only = ("ifrs-full" in all_facts_tree) and ("us-gaap" not in all_facts_tree)

    for spec in concepts:                                        # one concept at a time
        # Which tags to read. One-tag concepts (merge_tags False): the first present (taxonomy, tag) in the
        # preference order serves the whole document (the shares fallback lives here). merge_tags concepts
        # (FA1b-2): every listed tag present is read, and the pick below runs per period and filing.
        present = [(taxonomy, tag) for taxonomy, tag in spec.tags if tag in all_facts_tree.get(taxonomy, {})]
        # LK1e: the parts of a total (spec.parts) present in the document, in the spec's parts order. They rank after
        # every tag, so a filing that reports a total never has its parts read for it (see the group loop below).
        parts_present = [(taxonomy, tag) for taxonomy, tag in spec.parts if tag in all_facts_tree.get(taxonomy, {})]
        if not present and not parts_present:                    # neither preferred, fallback tag nor part present
            cov.concepts_missing.append(spec.name)              # R6: concept missing for this company (not an error)
            if spec.name == "shares_outstanding":               # record the shares fallback signal as 'none'
                cov.shares_tag = "none"
            continue
        to_read = present if spec.merge_tags else present[:1]    # a one-tag concept reads only the first present tag
        n_tags = len(to_read)                                    # ranks below this are tags, ranks from it are parts
        to_read = to_read + parts_present
        cov.concepts_found.append(spec.name)                    # the concept resolved (any tag or part present)
        if spec.name == "shares_outstanding" and present:       # record which shares tag won (the R6 fallback log)
            cov.shares_tag = f"{present[0][0]}:{present[0][1]}"

        survivors: list[dict] = []                              # kept records of every tag read, each marked with its tag
        for rank, (taxonomy, tag) in enumerate(to_read):        # rank 0 is the earliest-listed tag present
            units_block = all_facts_tree[taxonomy][tag].get("units", {})  # unit -> [records]
            # Units boundary (§2.4 step 1): select the records under the expected unit; any other unit
            # key is a non-USD / uncovered drop, reported, never converted.
            for unit_key, recs in units_block.items():
                if unit_key != spec.unit:
                    cov.counts["non_usd_dropped"] += len(recs)   # count the dropped foreign-unit records
                    if unit_key not in cov.non_usd_units_seen:
                        cov.non_usd_units_seen.append(unit_key)  # surface the foreign unit (e.g. EUR) for R6
            records = units_block.get(spec.unit, [])            # the in-unit records (may be empty)
            for r in _parsed_records(records, spec, cov):       # form/fp/scope filters
                r["taxonomy"], r["tag"], r["rank"] = taxonomy, tag, rank
                survivors.append(r)
        # Group by (fp, duration class, period_end, filing_date); the class is None for an instant.
        groups: dict[tuple[str, str | None, int, int], list[dict]] = {}
        for r in survivors:
            groups.setdefault((r["fp"], r["klass"], r["period_end"], r["filing_date"]), []).append(r)
        for (fp, _klass, period_end, filing_date), group in groups.items():
            tag_records = [r for r in group if r["rank"] < n_tags]
            if tag_records:                                      # a filing that reports a total: its parts are ignored
                group = tag_records
            elif spec.parts:                                     # only parts: the sum of the parts this filing reports
                summed = _sum_parts(group, spec, cov)
                if summed is not None:
                    facts.append(FactRow(
                        cik=cik, concept=spec.name, taxonomy=summed["taxonomy"], xbrl_tag=summed["tag"], unit=spec.unit,
                        fiscal_year=summed["fiscal_year"], fiscal_period_code=fp, period_start=None,
                        period_end=period_end, filing_date=filing_date, value=summed["value"],
                        source_form=summed["form"], accession_number=summed["accn"],
                    ))
                    cov.counts["facts_emitted"] += 1
                continue
            if spec.merge_tags:                                  # keep only the earliest-listed tag present in this group
                first = min(r["rank"] for r in group)
                group = [r for r in group if r["rank"] == first]
            chosen = _select_canonical(group, spec, cov)        # one canonical row or None (quarantined)
            if chosen is None:
                continue
            # Instant concepts store period_start = NULL (no duration); flows keep the chosen start.
            period_start = None if spec.period_semantics == "instant" else chosen["period_start"]
            facts.append(FactRow(
                cik=cik, concept=spec.name, taxonomy=chosen["taxonomy"], xbrl_tag=chosen["tag"], unit=spec.unit,
                fiscal_year=chosen["fiscal_year"], fiscal_period_code=fp, period_start=period_start,
                period_end=period_end, filing_date=filing_date, value=chosen["value"],
                source_form=chosen["form"], accession_number=chosen["accn"],
            ))
            cov.counts["facts_emitted"] += 1

    return facts, cov
