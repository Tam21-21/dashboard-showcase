"""The three point-in-time reads over the kept-every-vintage fundamentals store (M3.75 FUND-1 §5).

The store keeps every filing of every fact (the vintage axis is filing_date). Over that history
there are three distinct reads (R4), each a predicate over the same table and the same
(cik, concept, fiscal_period_code, period_end, filing_date) index (§1.2):

  (a) as-originally-reported, censored to D — within the latest reporting period public by D,
      that period's EARLIEST filing (its publication filing). Never overwritten by a restatement.
  (b) as-known-on-D — within the latest reporting period public by D, the LATEST filing with
      filing_date <= D. Equals (a) before any restatement; returns the restated figure once a
      restatement is itself public by D. The backtest DEFAULT (Q2, §12).
  (c) latest-restated — the most recent filing for each period regardless of filing_date.
      Leaks later restatements into earlier dates; a research read, never a backtest default.

THE correctness rule (§5.2, the arch §6 amendment, DECISIONS #92): the reporting period is
chosen by its ``period_end`` — the greatest ``period_end`` among the periods whose PUBLICATION
date (the period's earliest filing_date) is <= D — NOT by the latest filing and NOT by the
latest publication date. Selecting by publication date fails on an out-of-order/late first
filing (a delinquent filer files an OLDER period AFTER a newer one is public); selecting by
``period_end`` defeats both that hazard and the restatement trap with one rule. Publication date
is used ONLY as the public-by-D gate (a period counts only if its earliest filing is <= D) and
as read (a)'s vintage.

Scope: a FLOW concept (net income, EPS) reads the FY annual figure (fiscal_period_code='FY',
lagged-annual, Q8); an INSTANT concept (equity, shares) reads the latest period of ANY scope as
of D (the most-current balance-sheet value). The scope comes from the concept vocabulary's
period_semantics (concepts.py).

This is the DB/SQL form of the read (used by the inspector §10, the screen §9, and the §13.1
probe). The in-container vectorized form over a materialized parquet slice is FUND-2 §6.3.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .concepts import FUNDAMENTAL_CONCEPTS, ConceptSpec, concept_by_name


@dataclass(frozen=True)
class FactValue:
    """One resolved fact: its value plus the provenance a consumer needs to judge it.

    ``filing_date`` is carried back so a consumer can see how old the still-most-current figure
    is (R14) and apply a staleness rule (Q5); the read never presents an arbitrarily old figure
    as current without its age.
    """

    value: float                                # the concept's value as of the read
    concept: str                                # the concept name
    fiscal_period_code: str                     # the chosen period's fp ('FY' for flows; any for instants)
    fiscal_year: int                            # the chosen filing's fy
    period_end: int                             # epoch s UTC, the chosen reporting period's end
    period_start: int | None                    # epoch s UTC, the duration start (None for instants)
    filing_date: int                            # epoch s UTC, the chosen filing's date (the figure's age axis)
    source_form: str                            # the chosen filing's form
    accession_number: str | None                # the chosen filing's accession
    xbrl_tag: str | None = None                 # the raw XBRL tag stored with the figure (the debt rule reads it; lane LK1e).
                                                # A part sum is its part tags joined by "+". None when a caller builds one without it.


# The three vintage modes selectable within the chosen period.
VINTAGE_AS_KNOWN = "as_known_on_date"           # read (b), the default
VINTAGE_AS_REPORTED = "as_originally_reported"  # read (a)
_VINTAGES = (VINTAGE_AS_KNOWN, VINTAGE_AS_REPORTED)

# Seconds per UTC day. The same-day rule (M3.75 hotfix Fix 4): filing_date is day-granular (midnight
# UTC, parse._iso_to_epoch), so a filing made the SAME calendar day as an intraday decision date D
# cannot be assumed public at D (the XBRL 'filed' carries no time). A filing is known at D iff it is
# filed STRICTLY BEFORE D's UTC day — i.e. filing_date < start_of_day(D). Floor D to its UTC midnight.
_SECONDS_PER_DAY = 86_400


def _scope_clause(concept_spec: ConceptSpec) -> tuple[str, tuple]:
    """Return the SQL fiscal-period-scope predicate + params for a concept.

    FLOW -> fiscal_period_code = 'FY' (the annual figure). INSTANT -> no fp restriction (the
    latest period of any scope). The predicate is appended to a WHERE that already fixes
    (cik, concept), so it rides the (cik, concept, fiscal_period_code, ...) index prefix.
    """
    if concept_spec.period_semantics == "flow":
        return " AND fiscal_period_code = ?", ("FY",)
    return "", ()                                # instant: any periodic scope


def _row_to_factvalue(row: sqlite3.Row) -> FactValue:
    """Map a research_fundamental_facts row to a FactValue."""
    return FactValue(
        value=row["value"], concept=row["concept"], fiscal_period_code=row["fiscal_period_code"],
        fiscal_year=row["fiscal_year"], period_end=row["period_end"], period_start=row["period_start"],
        filing_date=row["filing_date"], source_form=row["source_form"], accession_number=row["accession_number"],
        xbrl_tag=row["xbrl_tag"],
    )


def _resolve_spec(concept: str, concepts: tuple[ConceptSpec, ...]) -> ConceptSpec:
    """Look a concept name up in the vocabulary; fail loud on an unknown concept (a caller bug)."""
    spec = concept_by_name(concepts).get(concept)
    if spec is None:
        raise ValueError(f"unknown concept {concept!r}; known: {sorted(concept_by_name(concepts))}")
    return spec


def read_asof(
    conn: sqlite3.Connection,
    cik: int,
    concept: str,
    decision_date: int,
    vintage: str = VINTAGE_AS_KNOWN,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> FactValue | None:
    """Read a fundamental as of ``decision_date`` (epoch s), no lookahead. Reads (a) and (b).

    Step 1 (period by period_end, gated public-by-D): among (cik, concept) rows in scope, find
    the greatest ``period_end`` whose period is public by D — i.e. whose EARLIEST filing_date is
    STRICTLY BEFORE D's UTC day (the Fix 4 same-day rule: filing_date < start_of_day(D), since a
    filing made the same calendar day as an intraday D cannot be assumed public at D). Step 2
    (vintage within the period): for ``as_known_on_date`` the latest filing with filing_date <
    start_of_day(D); for ``as_originally_reported`` that period's earliest filing. Returns None
    when the company has no in-scope fact public by D (an honest empty, never a fake zero).

    ``conn.row_factory`` must be ``sqlite3.Row`` (the caller sets it); the read is index-served
    by the §1.2 (cik, concept, fiscal_period_code, period_end, filing_date) index.
    """
    if vintage not in _VINTAGES:                                # a bad vintage is a caller bug; fail loud
        raise ValueError(f"vintage must be one of {_VINTAGES}; got {vintage!r}")
    spec = _resolve_spec(concept, concepts)                     # the concept's scope/semantics
    scope_sql, scope_params = _scope_clause(spec)               # '' or " AND fiscal_period_code='FY'"
    dod = decision_date - decision_date % _SECONDS_PER_DAY      # start of D's UTC day; the same-day gate (Fix 4)

    # Step 1: choose the period. Group the in-scope rows by period_end, keep periods whose
    # earliest filing is STRICTLY BEFORE D's day (public by D, Fix 4 same-day rule), take the
    # greatest period_end among them. This is the Rule-C selection (period_end key), not
    # publication date and not latest filing.
    period_row = conn.execute(
        f"""SELECT period_end
              FROM research_fundamental_facts
             WHERE cik = ? AND concept = ?{scope_sql}
             GROUP BY period_end
            HAVING MIN(filing_date) < ?
             ORDER BY period_end DESC
             LIMIT 1""",
        (cik, concept, *scope_params, dod),
    ).fetchone()
    if period_row is None:                                      # no in-scope period public by D
        return None
    chosen_period_end = period_row["period_end"]

    # Step 2: choose the vintage within the chosen period.
    if vintage == VINTAGE_AS_KNOWN:                             # read (b): latest filing strictly before D's day (Fix 4)
        order = "ORDER BY filing_date DESC"
        vintage_sql, vintage_params = " AND filing_date < ?", (dod,)
    else:                                                       # read (a): the period's earliest (publication) filing
        order = "ORDER BY filing_date ASC"
        vintage_sql, vintage_params = "", ()
    fact_row = conn.execute(
        f"""SELECT value, concept, fiscal_period_code, fiscal_year, period_end, period_start,
                   filing_date, source_form, accession_number, xbrl_tag
              FROM research_fundamental_facts
             WHERE cik = ? AND concept = ?{scope_sql} AND period_end = ?{vintage_sql}
             {order}
             LIMIT 1""",
        (cik, concept, *scope_params, chosen_period_end, *vintage_params),
    ).fetchone()
    return _row_to_factvalue(fact_row) if fact_row is not None else None


def read_latest_restated(
    conn: sqlite3.Connection,
    cik: int,
    concept: str,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> FactValue | None:
    """Read (c) latest-restated: the greatest ``period_end`` overall, its most recent filing.

    No decision-date gate — this deliberately uses the latest restatement regardless of when it
    was filed, so it leaks future information into the past. A RESEARCH read only, never a
    backtest default (NFR-1). Provided so the §13.1 probe can assert (c) differs from (a)/(b).
    """
    spec = _resolve_spec(concept, concepts)
    scope_sql, scope_params = _scope_clause(spec)
    # Greatest period_end overall (no public-by-D gate), then the latest filing for it overall.
    row = conn.execute(
        f"""SELECT value, concept, fiscal_period_code, fiscal_year, period_end, period_start,
                   filing_date, source_form, accession_number, xbrl_tag
              FROM research_fundamental_facts
             WHERE cik = ? AND concept = ?{scope_sql}
               AND period_end = (SELECT MAX(period_end) FROM research_fundamental_facts
                                  WHERE cik = ? AND concept = ?{scope_sql})
             ORDER BY filing_date DESC
             LIMIT 1""",
        (cik, concept, *scope_params, cik, concept, *scope_params),
    ).fetchone()
    return _row_to_factvalue(row) if row is not None else None


# The year before a balance sheet: an instant 330 to 400 days earlier (a 52 or 53 week year and a calendar year
# both fit), the one nearest 365 days. A display and ratio-input rule (lane LK1c), not a backtest parameter.
_PRIOR_MIN_DAYS = 330
_PRIOR_MAX_DAYS = 400
_PRIOR_NEAREST_DAYS = 365
# The balance-sheet figures the ratios average with the year before.
_PRIOR_CONCEPTS = ("shareholders_equity", "total_assets")


def read_prior_year_instant(
    conn: sqlite3.Connection,
    cik: int,
    concept: str,
    period_end: int,
    decision_date: int,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> FactValue | None:
    """The same instant concept one year before ``period_end`` (epoch s), as known on ``decision_date``.

    Among the concept's periods ending 330 to 400 days before ``period_end`` and public by D (earliest
    filing strictly before D's UTC day, the same gate read_asof uses), the one nearest 365 days back (a tie
    goes to the newer period end); its latest filing strictly before D's day. None when there is none, an
    honest empty. ``conn.row_factory`` must be ``sqlite3.Row``.
    """
    spec = _resolve_spec(concept, concepts)
    scope_sql, scope_params = _scope_clause(spec)
    dod = decision_date - decision_date % _SECONDS_PER_DAY
    low = period_end - _PRIOR_MAX_DAYS * _SECONDS_PER_DAY
    high = period_end - _PRIOR_MIN_DAYS * _SECONDS_PER_DAY
    candidates = conn.execute(
        f"""SELECT period_end
              FROM research_fundamental_facts
             WHERE cik = ? AND concept = ?{scope_sql} AND period_end BETWEEN ? AND ?
             GROUP BY period_end
            HAVING MIN(filing_date) < ?""",
        (cik, concept, *scope_params, low, high, dod),
    ).fetchall()
    if not candidates:
        return None
    target = period_end - _PRIOR_NEAREST_DAYS * _SECONDS_PER_DAY
    chosen = min((row["period_end"] for row in candidates), key=lambda end: (abs(end - target), -end))
    fact_row = conn.execute(
        f"""SELECT value, concept, fiscal_period_code, fiscal_year, period_end, period_start,
                   filing_date, source_form, accession_number, xbrl_tag
              FROM research_fundamental_facts
             WHERE cik = ? AND concept = ?{scope_sql} AND period_end = ? AND filing_date < ?
             ORDER BY filing_date DESC
             LIMIT 1""",
        (cik, concept, *scope_params, chosen, dod),
    ).fetchone()
    return _row_to_factvalue(fact_row) if fact_row is not None else None


# --- Lane LK1: the annual history read -------------------------------------------------------------

# Plain names for the concepts FIGURE_DEFINITIONS (the eight core figures) does not cover, so the lookup
# window never shows a snake_case word. Every concept in FUNDAMENTAL_CONCEPTS is labelled here or there;
# annual_label fails loud (KeyError) on one that is neither.
_EXTRA_LABELS: dict[str, str] = {
    "cost_of_revenue": "Cost of revenue",
    "gross_profit": "Gross profit",
    "research_development": "Research and development",
    "sga": "Selling, general and administrative costs",
    "operating_income": "Operating income",
    "depreciation_amortization": "Depreciation and amortization",
    "interest_expense": "Interest expense",
    "pretax_income": "Income before tax",
    "income_tax": "Income tax",
    "capex": "Capital spending",
    "dividends_paid": "Dividends paid",
    "buybacks": "Share buybacks",
    "stock_based_comp": "Stock-based pay",
    "investing_cash_flow": "Cash from investing",
    "financing_cash_flow": "Cash from financing",
    "change_in_cash": "Change in cash",
    "eps_diluted": "Diluted earnings per share",
    "weighted_shares_basic": "Average shares, basic",
    "weighted_shares_diluted": "Average shares, diluted",
    "cash": "Cash",
    "short_term_investments": "Short-term investments",
    "receivables": "Receivables",
    "inventory": "Inventory",
    "current_assets": "Current assets",
    "current_liabilities": "Current liabilities",
    "short_term_debt": "Short-term debt",
    "long_term_debt": "Long-term debt",
    "retained_earnings": "Retained earnings",
    "short_term_borrowings": "Short-term borrowings",
    "current_long_term_debt": "Long-term debt due within a year",
}

# A full-year flow lasts 350 to 380 days: a 52 or 53 week year and a calendar year both fit; a quarter or
# a year-to-date figure that a filing labels "FY" does not. A display rule (the same kind of dial as the
# 12 hour cache), not a backtest parameter.
_ANNUAL_MIN_DAYS = 350
_ANNUAL_MAX_DAYS = 380


def annual_label(concept: str) -> str:
    """The plain name of a concept (FIGURE_DEFINITIONS first, then _EXTRA_LABELS); KeyError if neither."""
    from .definitions import FIGURE_DEFINITIONS

    entry = FIGURE_DEFINITIONS.get(concept)
    return entry["label"] if entry is not None else _EXTRA_LABELS[concept]


def _epoch_to_day(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()


def read_annual_history(
    conn: sqlite3.Connection,
    cik: int,
    years: int,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> dict:
    """Each concept and ratio by fiscal year for one company (lane LK1), newest year first.

    Only rows filed as fiscal_period_code 'FY' count. A flow counts only when its period is 350 to 380
    days long. An instant counts at an annual period end of that company's flows (a company with no
    flows has any FY instant counted). Per (concept, period end) the latest filing wins, a tie going to
    the larger fact_id, so a restatement replaces the first figure. ``years`` keeps that many newest
    period ends. Ratios are computed per year by ratios.compute_ratios over that year's figures.
    ``conn.row_factory`` must be ``sqlite3.Row``. A company with nothing on record gives empty lists.
    Returns {"periods": [YYYY-MM-DD], "concepts": [...], "ratios": [...]}.
    """
    from .definitions import RATIO_DEFINITIONS
    from .ratios import RATIO_SPECS, compute_ratios  # lazy: ratios imports this module

    by_name = concept_by_name(concepts)
    rows = conn.execute(
        """SELECT fact_id, value, concept, fiscal_period_code, fiscal_year, period_end, period_start,
                  filing_date, source_form, accession_number, xbrl_tag
             FROM research_fundamental_facts
            WHERE cik = ? AND fiscal_period_code = 'FY'""",
        (cik,),
    ).fetchall()

    latest: dict[tuple[str, int], sqlite3.Row] = {}          # (concept, period_end) -> the winning row
    for row in rows:
        spec = by_name.get(row["concept"])
        if spec is None:
            continue                                         # a stored concept outside the vocabulary asked for
        if spec.period_semantics == "flow":
            if row["period_start"] is None:
                continue
            length_days = (row["period_end"] - row["period_start"]) / _SECONDS_PER_DAY
            if not _ANNUAL_MIN_DAYS <= length_days <= _ANNUAL_MAX_DAYS:
                continue
        key = (row["concept"], row["period_end"])
        held = latest.get(key)
        if held is None or (row["filing_date"], row["fact_id"]) > (held["filing_date"], held["fact_id"]):
            latest[key] = row

    flow_ends = {end for (name, end) in latest if by_name[name].period_semantics == "flow"}
    kept = {
        key: row for key, row in latest.items()
        if by_name[key[0]].period_semantics == "flow" or not flow_ends or key[1] in flow_ends
    }
    all_ends = sorted({end for (_, end) in kept}, reverse=True)
    period_ends = all_ends[:years]
    if not period_ends:
        return {"periods": [], "concepts": [], "ratios": []}

    concept_entries = []
    for spec in concepts:
        values = [
            {"period_end": _epoch_to_day(end), "value": kept[(spec.name, end)]["value"],
             "filing_date": _epoch_to_day(kept[(spec.name, end)]["filing_date"])}
            for end in period_ends if (spec.name, end) in kept
        ]
        if values:
            concept_entries.append(
                {"concept": spec.name, "label": annual_label(spec.name), "unit": spec.unit, "values": values}
            )

    ratio_values: dict[str, list[dict]] = {spec.name: [] for spec in RATIO_SPECS}
    for end in period_ends:
        facts = {
            spec.name: _row_to_factvalue(kept[(spec.name, end)])
            for spec in concepts if (spec.name, end) in kept
        }
        # The year before: the next older kept period end (taken before the years cut), when it lies 330 to
        # 400 days back. Each balance-sheet figure then reads its own value at that end, or none.
        older = all_ends[all_ends.index(end) + 1] if all_ends.index(end) + 1 < len(all_ends) else None
        prior_end = older if older is not None and _PRIOR_MIN_DAYS <= (end - older) / _SECONDS_PER_DAY <= _PRIOR_MAX_DAYS else None
        prior = {
            name: _row_to_factvalue(kept[(name, prior_end)])
            for name in _PRIOR_CONCEPTS if prior_end is not None and (name, prior_end) in kept
        }
        for ratio in compute_ratios(facts, prior):
            cell = {
                "period_end": _epoch_to_day(end),
                "value": ratio["value"] if ratio["available"] else None,
                "reason": None if ratio["available"] else ratio["reason"],
            }
            if "debt" in ratio:                                  # debt to equity: the debt it divided (lane LK1e)
                cell["debt"] = ratio["debt"]
            ratio_values[ratio["name"]].append(cell)
    ratio_entries = [
        {"name": spec.name, "label": spec.label, "definition": spec.definition, "formula": spec.formula,
         "source": RATIO_DEFINITIONS[spec.name]["source"], "values": ratio_values[spec.name]}
        for spec in RATIO_SPECS
    ]
    return {"periods": [_epoch_to_day(end) for end in period_ends], "concepts": concept_entries,
            "ratios": ratio_entries}
