"""The point-in-time company read: per-concept as-of facts + the computed ratios (ANA-5 / ANA-6).

One place assembles a company's as-of "cell" — the per-concept no-lookahead figures (read_asof,
vintage = as-known-on-D) plus the divide-safe ratios (ratios.compute_ratios) over those same
FactValues. The inspector route (one company, one date, §5.2) and the compare route (a bounded
grid of companies × dates, §6.1) BOTH call read_company_asof, so a compare cell is byte-identical
to the inspector read by construction (the §8.5 cell-agreement is structural, not a coincidence).

This module lives below the route layer so the fastapi-less laptop probe can import and exercise the
cell read AND the compare bound check directly (the session-46 testability gotcha — the route layer
needs fastapi, this module does not). It imports reads + ratios + concepts; none import it (no cycle).

The compare bound (§6.1, D-operational): the read is companies × dates × concepts indexed read_asof
calls. The cap bounds the company-AND-date PRODUCT (not companies alone — the SF1 correctness fix),
reject-don't-truncate. These are operational read bounds (they shape a dashboard read, not a
backtest), so they carry their rationale here, not a parameter_provenance.md entry (spec §7).
"""
from __future__ import annotations

import sqlite3

from .concepts import ConceptSpec, FUNDAMENTAL_CONCEPTS
from .ratios import compute_ratios
from .reads import _PRIOR_CONCEPTS, read_asof, read_prior_year_instant

# The compare bound (§6.1). COMPARE_MAX_CELLS caps the company-AND-date product (12 companies × 4
# dates, or 6 × 8 — a "handful"). The per-axis sub-caps bound a single huge axis even within the
# product cap. Operational read bounds, not backtest parameters (spec §7): a larger grid is a
# rejected request, never a truncated-and-silently-incomplete answer.
COMPARE_MAX_CELLS = 48                                            # the company × date product ceiling
COMPARE_MAX_CIKS = 12                                            # the per-axis company ceiling
COMPARE_MAX_ASOFS = 8                                           # the per-axis date ceiling


def company_on_record(conn: sqlite3.Connection, cik: int) -> bool:
    """True if this company has ANY fundamental fact in the store (any concept, any vintage).

    The honest distinction the compare/inspector UI needs: a cell with every concept ``found: false``
    is AMBIGUOUS — it can mean "the company was fetched, but nothing it filed is public by this as-of
    date" (honest-missing, keep the dash) OR "this company has never been fetched at all" (fetch it
    first). This EXISTS (cik is the leading column of the as-of concatenated index, so it is a single
    indexed point lookup) resolves it: on_record False -> never fetched; True -> fetched, the per-cell
    dashes are genuine no-data-by-this-date. No as-of filter — the question is "fetched ever", not
    "public by D".
    """
    row = conn.execute(
        "SELECT 1 FROM research_fundamental_facts WHERE cik = ? LIMIT 1", (cik,)
    ).fetchone()
    return row is not None


def read_company_asof(
    conn: sqlite3.Connection,
    cik: int,
    as_of: int,
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> dict:
    """One company's as-of cell: the per-concept facts list + the computed ratios + on_record.

    For each concept, the as-known-on-D figure (read_asof, the Q2 default vintage; no lookahead). A
    concept with no fact public by the date carries ``found: false`` and a null value (the honest
    empty per-concept state, never a fake zero — NFR-7). The ratios are computed over the resolved
    FactValues (the SAME values, no second read path), divide-safe, both legs' dates carried.
    ``on_record`` (company_on_record) tells the UI whether an all-empty cell means "never fetched"
    (False) versus "fetched, nothing public by this date" (True). Returns
    ``{"facts": [...], "ratios": [...], "on_record": bool}``; the caller adds cik/as_of.
    """
    rows: list[dict] = []                                        # one entry per concept, in vocabulary order
    facts_by_concept: dict = {}                                 # concept -> FactValue, for the ratio computation
    for spec in concepts:
        fact = read_asof(conn, cik, spec.name, as_of)           # as-known-on-D (Q2 default vintage)
        if fact is None:                                        # no in-scope fact public by as_of -> honest empty
            rows.append({"concept": spec.name, "found": False, "value": None})
            continue
        facts_by_concept[fact.concept] = fact                   # the SAME no-lookahead value the ratios reuse
        rows.append({
            "concept": fact.concept, "found": True, "value": fact.value,
            "fiscal_period_code": fact.fiscal_period_code, "fiscal_year": fact.fiscal_year,
            "period_end": fact.period_end, "period_start": fact.period_start,
            "filing_date": fact.filing_date, "source_form": fact.source_form,
            "accession_number": fact.accession_number,
        })
    # The year-before equity and assets, read as known on the same date (no lookahead), for the averages.
    prior_by_concept: dict = {}
    for name in _PRIOR_CONCEPTS:
        current = facts_by_concept.get(name)
        if current is None:
            continue
        prior_fact = read_prior_year_instant(conn, cik, name, current.period_end, as_of, concepts)
        if prior_fact is not None:
            prior_by_concept[name] = prior_fact
    ratios = compute_ratios(facts_by_concept, prior_by_concept)  # the divide-safe virtual view over those facts
    return {"facts": rows, "ratios": ratios, "on_record": company_on_record(conn, cik)}


def compare_bound_error(ciks: list[int], as_ofs: list[int]) -> str | None:
    """Return a plain over-cap reason if the compare request exceeds a bound, else None.

    Pure (no DB, no fastapi) so the laptop probe tests the cap arithmetic directly and the route
    raises a 422 ``compare_too_large`` on a non-None return. Checks BOTH per-axis sub-caps AND the
    company-and-date product (the SF1 fix: a 6×8 grid is 48 cells = the cap, a 13×1 grid is rejected
    by the per-axis cap even though its product is under 48). An empty axis is itself a bad request.
    """
    if not ciks or not as_ofs:                                  # an empty axis cannot form a grid
        return "Pick at least one company and one date."
    if len(ciks) > COMPARE_MAX_CIKS:
        return f"Too many companies: {len(ciks)} (max {COMPARE_MAX_CIKS})."
    if len(as_ofs) > COMPARE_MAX_ASOFS:
        return f"Too many dates: {len(as_ofs)} (max {COMPARE_MAX_ASOFS})."
    cells = len(ciks) * len(as_ofs)                             # the company × date product
    if cells > COMPARE_MAX_CELLS:
        return (f"Too many cells: {len(ciks)} companies × {len(as_ofs)} dates = {cells} "
                f"(max {COMPARE_MAX_CELLS}). Narrow the companies or the dates.")
    return None                                                 # within every bound


def read_compare(
    conn: sqlite3.Connection,
    ciks: list[int],
    as_ofs: list[int],
    concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS,
) -> list[dict]:
    """The bounded compare grid: one cell per (cik, as_of), each the inspector read (§6.1).

    The caller MUST have cleared compare_bound_error first (the route raises 422 otherwise). Each
    cell reuses read_company_asof, so a compare cell equals the single-company inspector read for the
    same (cik, as_of) by construction. The loop is companies × dates indexed reads; at the cap (48
    cells × 8 concepts = 384 indexed point lookups on the as-of concatenated index) this is trivially
    fast, so the indexed-read loop is chosen over a set-based per-date query — measured at build
    (§8.5), the set-based query is not worth the added SQL + the correctness re-proof at this bound.
    """
    return [
        {"cik": cik, "as_of": as_of, **read_company_asof(conn, cik, as_of, concepts)}
        for as_of in as_ofs                                     # date-major so a set-based per-date batch could slot in later
        for cik in ciks
    ]
