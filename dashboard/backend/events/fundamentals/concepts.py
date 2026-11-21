"""The normalized fundamental-concept vocabulary (M3.75 FUND-1 §2.1, requirements R2).

This is the single code-side owner of "which fundamental concepts exist, which XBRL tags map
to each, what unit each carries, and whether each is a flow (period-duration) or an instant
(balance-sheet point) concept." It is CODE, deliberately not a database table and not a
``CHECK`` enum on the store: concept is stored as DATA in research_fundamental_facts.concept
(no ``CHECK``, §1.1), so adding a concept later is an INSERT of rows, never a schema
migration (NFR-11, the concept-as-data guarantee). The vocabulary here is what decides which
raw ``(taxonomy, tag)`` pairs become which normalized concept name; the parser (parse.py)
emits only names from this vocabulary, which is why the store needs no concept ``CHECK``.

The seed is the verified Newsbot v3 §6.5 core (Q7 settled = these four only, §12): net income,
shareholders' equity, basic EPS, and shares outstanding. The shares-outstanding tag has the
dei-preferred / us-gaap-fallback order the live spike adopted after finding the dei tag absent
on several mid-caps (verify_edgar_xbrl_v2.py:103-113; confirmed again at the FUND-1 #77 gate,
where ROKU/PLTR fell back to us-gaap and Wayfair carried neither — a real coverage gap the R6
report surfaces, never a silent miss).

Adding a fifth concept later is appending one ``ConceptSpec`` to ``FUNDAMENTAL_CONCEPTS`` (or
passing a custom tuple to the parser): the store, the index, the materializer, and the channel
all absorb it as data. The §13.1 fifth-concept probe proves exactly that — zero store rework.
"""
from __future__ import annotations

from dataclasses import dataclass

# The two period-semantics kinds. A FLOW concept is reported over a duration (net income, EPS
# accrue across a fiscal period) and carries an XBRL ``start``/``end`` pair; the M3.75 read
# takes the latest FY annual figure (lagged-annual, Q8). An INSTANT concept is a point value
# (a balance-sheet stock: equity, share count) with only an ``end``; the read takes the latest
# period's value as of the date, regardless of fiscal-period scope (§2.5 / §5.2).
PERIOD_SEMANTICS = ("flow", "instant")

# The expected XBRL units keys for the core concepts. Used by the parser's units boundary
# (§2.4 step 1): a fact under any other units key (e.g. EUR, CNY) is dropped-and-reported as
# non-USD/uncovered, never converted. Listed here so the vocabulary owns the unit expectation.
EXPECTED_UNITS = ("USD", "shares", "USD/shares")


@dataclass(frozen=True)
class ConceptSpec:
    """One fundamental concept: its internal name, its XBRL tag preference, unit, and period kind.

    Frozen (immutable) because the vocabulary is a constant: a strategy or the parser reads it,
    never mutates it. Validation runs at construction (``__post_init__``) so a malformed entry
    fails loud at import time, not at the first parse.
    """

    name: str                                   # the internal concept name (the value stored in research_fundamental_facts.concept)
    tags: tuple[tuple[str, str], ...]           # ordered (taxonomy, xbrl_tag) preference; the first present in a doc wins
    unit: str                                   # the expected XBRL units key (one of EXPECTED_UNITS)
    period_semantics: str                       # 'flow' (duration) or 'instant' (point); decides the §5.2 read scope
    merge_tags: bool = False                    # False: the first present tag serves the whole document (the eight original concepts).
                                                # True (FA1b-2): every listed tag is read and, per period and filing, the earliest-listed tag present wins.
    parts: tuple[tuple[str, str], ...] = ()     # LK1e: (taxonomy, tag) pairs that are the children of the total in ``tags``. When a filing
                                                # reports none of ``tags`` but reports parts, the stored value is the sum of the parts that
                                                # filing reports (parse.py). Empty: the concept has no parts.

    def __post_init__(self) -> None:
        """Fail loud on a malformed spec (a typo in the vocabulary is a programming error)."""
        if not self.name:                                                    # an empty concept name is a bug
            raise ValueError("ConceptSpec.name must be non-empty")
        if not self.tags:                                                    # a concept with no tags can never resolve
            raise ValueError(f"ConceptSpec {self.name!r} has no (taxonomy, tag) pairs")
        for pair in self.tags + self.parts:                                  # every tag and part must be a (taxonomy, tag) 2-tuple
            if len(pair) != 2 or not all(isinstance(x, str) and x for x in pair):
                raise ValueError(f"ConceptSpec {self.name!r} has a malformed tag pair {pair!r}; expected (taxonomy, tag)")
        if self.unit not in EXPECTED_UNITS:                                  # an unexpected unit would silently never match
            raise ValueError(f"ConceptSpec {self.name!r} unit {self.unit!r} not in {EXPECTED_UNITS}")
        if self.period_semantics not in PERIOD_SEMANTICS:                    # flow vs instant drives the read scope
            raise ValueError(f"ConceptSpec {self.name!r} period_semantics {self.period_semantics!r} not in {PERIOD_SEMANTICS}")


# The seed vocabulary: the verified Newsbot v3 §6.5 four-concept core (Q7 = exactly these).
# A new concept ships later as one appended ConceptSpec — pure data, no store/index/channel rework.
FUNDAMENTAL_CONCEPTS: tuple[ConceptSpec, ...] = (
    # Net income: the bottom-line earnings flow. Single us-gaap tag (verify_edgar_xbrl_v2.py:104).
    ConceptSpec("net_income", (("us-gaap", "NetIncomeLoss"),), "USD", "flow"),
    # Shareholders' equity: a balance-sheet stock (instant) — total equity. Single us-gaap tag.
    ConceptSpec("shareholders_equity", (("us-gaap", "StockholdersEquity"),), "USD", "instant"),
    # Basic earnings per share: a per-share flow. Single us-gaap tag; unit is USD-per-share.
    ConceptSpec("eps_basic", (("us-gaap", "EarningsPerShareBasic"),), "USD/shares", "flow"),
    # Shares outstanding: a count (instant). dei cover-page tag preferred (more current), us-gaap
    # balance-sheet tag the fallback — the §6.5 order the spike proved necessary (the #77 gate
    # confirmed ROKU/PLTR need the fallback and Wayfair has neither).
    ConceptSpec(
        "shares_outstanding",
        (("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding")),
        "shares",
        "instant",
    ),
    # --- ANA-4 concept expansion (#100 spec §4.1): four appended concepts, pure data, no schema
    # change (NFR-11, the concept-as-data guarantee, now exercised four times). The tag-preference
    # lists are taxonomy-grounded CANDIDATES verified against real filings by the §8.2 real-host
    # probe (verify_companyfacts_realhost.py captures the fuller fixtures; a tag failure on a spike
    # company is a fold to this spec, never a ship-unverified). ---
    # Revenue: the top-line period flow. ASC-606 (effective 2018 for public filers) moved revenue
    # tagging from us-gaap:Revenues / SalesRevenueNet to RevenueFromContractWithCustomerExcludingAssessedTax.
    # The post-606 tag is preferred (recent filers); the two pre-606 tags are fallbacks so an older
    # filer still resolves. The parser resolves ONE tag per company (first present in the doc, parse.py
    # _resolve loop) — for a company that has switched, only the post-606 records ship; pre-606-only
    # periods are honest-missing (the §8.2 probe verifies which era each spike company resolves). USD.
    ConceptSpec("revenue",
        (("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
         ("us-gaap", "Revenues"),
         ("us-gaap", "SalesRevenueNet")), "USD", "flow"),
    # Total assets: a balance-sheet stock (instant). Single stable us-gaap tag. USD.
    ConceptSpec("total_assets", (("us-gaap", "Assets"),), "USD", "instant"),
    # Total liabilities: a balance-sheet stock (instant). us-gaap:Liabilities is total liabilities
    # (NOT LiabilitiesAndStockholdersEquity, which is liabilities + equity). USD.
    ConceptSpec("total_liabilities", (("us-gaap", "Liabilities"),), "USD", "instant"),
    # Operating cash flow: a period flow. The standard tag, with the continuing-operations variant
    # as a fallback for filers that split discontinued operations out. USD.
    ConceptSpec("operating_cash_flow",
        (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
         ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")), "USD", "flow"),
    # --- FA1b-2: 28 more SEC figures (handoff 2026-10-29 sections 1 and 1b, #480 item 1). Each lists its alternative tags,
    # checked on 20 real companies; merge_tags=True so a company that changed tag keeps its whole history (Tamer 2026-10-30).
    # The eight concepts above keep the one-tag-per-document rule, so none of their stored rows move. ---
    ConceptSpec("cost_of_revenue",
        (("us-gaap", "CostOfRevenue"),
         ("us-gaap", "CostOfGoodsAndServicesSold"),
         ("us-gaap", "CostOfGoodsSold"),), "USD", "flow", merge_tags=True),
    ConceptSpec("gross_profit",
        (("us-gaap", "GrossProfit"),), "USD", "flow", merge_tags=True),
    ConceptSpec("research_development",
        (("us-gaap", "ResearchAndDevelopmentExpense"),
         ("us-gaap", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),), "USD", "flow", merge_tags=True),
    ConceptSpec("sga",
        (("us-gaap", "SellingGeneralAndAdministrativeExpense"),), "USD", "flow", merge_tags=True),
    ConceptSpec("operating_income",
        (("us-gaap", "OperatingIncomeLoss"),), "USD", "flow", merge_tags=True),
    ConceptSpec("depreciation_amortization",
        (("us-gaap", "DepreciationDepletionAndAmortization"),
         ("us-gaap", "DepreciationAndAmortization"),), "USD", "flow", merge_tags=True),
    ConceptSpec("interest_expense",
        (("us-gaap", "InterestExpense"),
         ("us-gaap", "InterestExpenseNonoperating"),), "USD", "flow", merge_tags=True),
    ConceptSpec("pretax_income",
        (("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
         ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),), "USD", "flow", merge_tags=True),
    ConceptSpec("income_tax",
        (("us-gaap", "IncomeTaxExpenseBenefit"),), "USD", "flow", merge_tags=True),
    ConceptSpec("capex",
        (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
         ("us-gaap", "PaymentsToAcquireProductiveAssets"),), "USD", "flow", merge_tags=True),
    ConceptSpec("dividends_paid",
        (("us-gaap", "PaymentsOfDividends"),
         ("us-gaap", "PaymentsOfDividendsCommonStock"),), "USD", "flow", merge_tags=True),
    ConceptSpec("buybacks",
        (("us-gaap", "PaymentsForRepurchaseOfCommonStock"),), "USD", "flow", merge_tags=True),
    ConceptSpec("stock_based_comp",
        (("us-gaap", "ShareBasedCompensation"),), "USD", "flow", merge_tags=True),
    ConceptSpec("investing_cash_flow",
        (("us-gaap", "NetCashProvidedByUsedInInvestingActivities"),
         ("us-gaap", "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"),), "USD", "flow", merge_tags=True),
    ConceptSpec("financing_cash_flow",
        (("us-gaap", "NetCashProvidedByUsedInFinancingActivities"),
         ("us-gaap", "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"),), "USD", "flow", merge_tags=True),
    ConceptSpec("change_in_cash",
        (("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect"),
         ("us-gaap", "CashAndCashEquivalentsPeriodIncreaseDecrease"),), "USD", "flow", merge_tags=True),
    ConceptSpec("eps_diluted",
        (("us-gaap", "EarningsPerShareDiluted"),), "USD/shares", "flow", merge_tags=True),
    ConceptSpec("weighted_shares_basic",
        (("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),), "shares", "flow", merge_tags=True),
    ConceptSpec("weighted_shares_diluted",
        (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),), "shares", "flow", merge_tags=True),
    ConceptSpec("cash",
        (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),), "USD", "instant", merge_tags=True),
    ConceptSpec("short_term_investments",
        (("us-gaap", "ShortTermInvestments"),
         ("us-gaap", "MarketableSecuritiesCurrent"),), "USD", "instant", merge_tags=True),
    ConceptSpec("receivables",
        (("us-gaap", "AccountsReceivableNetCurrent"),
         ("us-gaap", "ReceivablesNetCurrent"),), "USD", "instant", merge_tags=True),
    ConceptSpec("inventory",
        (("us-gaap", "InventoryNet"),), "USD", "instant", merge_tags=True),
    ConceptSpec("current_assets",
        (("us-gaap", "AssetsCurrent"),), "USD", "instant", merge_tags=True),
    ConceptSpec("current_liabilities",
        (("us-gaap", "LiabilitiesCurrent"),), "USD", "instant", merge_tags=True),
    ConceptSpec("short_term_debt",
        (("us-gaap", "DebtCurrent"),
         ("us-gaap", "LongTermDebtCurrent"),), "USD", "instant", merge_tags=True),
    ConceptSpec("long_term_debt",
        (("us-gaap", "LongTermDebtNoncurrent"),
         ("us-gaap", "LongTermDebt"),), "USD", "instant", merge_tags=True),
    ConceptSpec("retained_earnings",
        (("us-gaap", "RetainedEarningsAccumulatedDeficit"),), "USD", "instant", merge_tags=True),
    # --- LK1e: the two short-term loan figures DebtCurrent leaves out when a company files its loans apart (Apple files
    # commercial paper on its own line). Each is the total tag when filed, else the sum of the parts one filing reports
    # (FASB us-gaap-stm-sfp-cls-cal-2026.xml: DebtCurrent = ShortTermBorrowings + LongTermDebtAndCapitalLeaseObligationsCurrent;
    # the parts are the children of those two totals). Tamer, 2026-11-16, ruling "Accounting tree". ---
    ConceptSpec("short_term_borrowings",
        (("us-gaap", "ShortTermBorrowings"),), "USD", "instant", merge_tags=True,
        parts=(
         ("us-gaap", "BankOverdrafts"),
         ("us-gaap", "CommercialPaper"),
         ("us-gaap", "BridgeLoan"),
         ("us-gaap", "ConstructionLoan"),
         ("us-gaap", "ShortTermBankLoansAndNotesPayable"),
         ("us-gaap", "ShortTermNonBankLoansAndNotesPayable"),
         ("us-gaap", "SecuritiesSoldUnderAgreementsToRepurchase"),
         ("us-gaap", "FederalHomeLoanBankAdvancesMaturitiesSummaryDueWithinOneYearOfBalanceSheetDate"),
         ("us-gaap", "WarehouseAgreementBorrowings"),
         ("us-gaap", "OtherShortTermBorrowings"),
        )),
    ConceptSpec("current_long_term_debt",
        (("us-gaap", "LongTermDebtCurrent"),), "USD", "instant", merge_tags=True,
        parts=(
         ("us-gaap", "SecuredDebtCurrent"),
         ("us-gaap", "ConvertibleDebtCurrent"),
         ("us-gaap", "UnsecuredDebtCurrent"),
         ("us-gaap", "SubordinatedDebtCurrent"),
         ("us-gaap", "ConvertibleSubordinatedDebtCurrent"),
         ("us-gaap", "LongTermCommercialPaperCurrent"),
         ("us-gaap", "LongTermConstructionLoanCurrent"),
         ("us-gaap", "LongtermTransitionBondCurrent"),
         ("us-gaap", "LongtermPollutionControlBondCurrent"),
         ("us-gaap", "OtherLongTermDebtCurrent"),
         ("us-gaap", "LinesOfCreditCurrent"),
         ("us-gaap", "NotesAndLoansPayableCurrent"),
         ("us-gaap", "SpecialAssessmentBondCurrent"),
         ("us-gaap", "FederalHomeLoanBankAdvancesCurrent"),
        )),
)


def concept_by_name(concepts: tuple[ConceptSpec, ...] = FUNDAMENTAL_CONCEPTS) -> dict[str, ConceptSpec]:
    """Return a {name: ConceptSpec} lookup for a vocabulary (defaults to the shipped core).

    A helper for the parser / reader / channel that need to look a concept up by its stored
    name. Takes the vocabulary as a parameter so the §13.1 fifth-concept probe can pass a
    custom tuple without touching the production constant.
    """
    return {c.name: c for c in concepts}
