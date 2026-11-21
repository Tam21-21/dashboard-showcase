"""The single source of truth for what each fundamental figure and ratio MEANS (ANA-2 §2.1).

Fixed, human-readable definition content — no per-data computation, no XBRL tags, no internal
vocabulary names in the prose. One place says what a figure or ratio is, so the text shown on hover
in the dashboard and (for ratios) the formula computed by the ratio module (§5, ratios.py imports
RATIO_DEFINITIONS from here) can never drift apart.

Two maps, each keyed by the internal concept/ratio name:
  FIGURE_DEFINITIONS  name -> {name, label, definition, source}   (the figures, the ratios' inputs included)
  RATIO_DEFINITIONS   name -> {name, label, definition, formula, source}  (the four ratios, lane LK1c)

`source` names the SEC figure in plain terms (the statement it comes from), never the raw us-gaap
tag. `formula` is the plain-text formula a human reads. The route GET /fundamentals/definitions
(§2.2) serves definitions_payload() as static JSON the frontend caches forever.
"""
from __future__ import annotations

# The eight figures. Keys are the concept names in concepts.py FUNDAMENTAL_CONCEPTS (the 4 seed) plus
# the 4 appended in §4 (revenue, total_assets, total_liabilities, operating_cash_flow). Defining all
# eight here now (pure content) is intended: ANA-4 wires the concepts, this module already names them.
FIGURE_DEFINITIONS: dict[str, dict[str, str]] = {
    "net_income": {
        "name": "net_income",
        "label": "Net income",
        "definition": "The company's total profit for the period, after every cost, expense, "
                      "interest payment, and tax. The bottom line.",
        "source": "The bottom line of the income statement.",
    },
    "shareholders_equity": {
        "name": "shareholders_equity",
        "label": "Shareholders' equity",
        "definition": "What would be left for shareholders if the company sold everything it owns "
                      "and paid off everything it owes (assets minus liabilities).",
        "source": "Total stockholders' equity on the balance sheet.",
    },
    "eps_basic": {
        "name": "eps_basic",
        "label": "Basic earnings per share",
        "definition": "Net income divided by the average number of shares, before any dilution from "
                      "options or convertible securities.",
        "source": "Basic earnings per share on the income statement.",
    },
    "shares_outstanding": {
        "name": "shares_outstanding",
        "label": "Shares outstanding",
        "definition": "The number of the company's shares currently held by all its shareholders.",
        "source": "Common shares outstanding (the filing cover page or balance sheet).",
    },
    "revenue": {
        "name": "revenue",
        "label": "Revenue",
        "definition": "The total money the company earned from selling its products or services "
                      "during the period. The top line.",
        "source": "Total revenue on the income statement.",
    },
    "total_assets": {
        "name": "total_assets",
        "label": "Total assets",
        "definition": "Everything the company owns that has value: cash, inventory, property, "
                      "equipment, and more.",
        "source": "Total assets on the balance sheet.",
    },
    "total_liabilities": {
        "name": "total_liabilities",
        "label": "Total liabilities",
        "definition": "Everything the company owes: debt, money owed to suppliers, and other "
                      "obligations.",
        "source": "Total liabilities on the balance sheet.",
    },
    "operating_cash_flow": {
        "name": "operating_cash_flow",
        "label": "Operating cash flow",
        "definition": "The actual cash the company generated from running its core business during "
                      "the period, set apart from accounting profit.",
        "source": "Net cash from operating activities on the cash flow statement.",
    },
    "operating_income": {
        "name": "operating_income",
        "label": "Operating income",
        "definition": "The profit from running the business itself: revenue minus the costs of making "
                      "and selling, before interest and tax.",
        "source": "Operating income on the income statement.",
    },
    "income_tax": {
        "name": "income_tax",
        "label": "Income tax",
        "definition": "The income tax the company charged against the period's profit.",
        "source": "Income tax expense on the income statement.",
    },
    "pretax_income": {
        "name": "pretax_income",
        "label": "Income before tax",
        "definition": "The company's profit for the period before income tax is taken off.",
        "source": "Income from continuing operations before income taxes on the income statement.",
    },
    "short_term_debt": {
        "name": "short_term_debt",
        "label": "Short-term debt",
        "definition": "Borrowed money the company has to pay back within a year.",
        "source": "Short-term debt on the balance sheet.",
    },
    "long_term_debt": {
        "name": "long_term_debt",
        "label": "Long-term debt",
        "definition": "Borrowed money the company can pay back after more than a year.",
        "source": "Long-term debt on the balance sheet.",
    },
    "short_term_borrowings": {
        "name": "short_term_borrowings",
        "label": "Short-term borrowings",
        "definition": "Money the company borrowed for a short time and has to pay back within a year, "
                      "such as commercial paper and bank loans.",
        "source": "The balance sheet line, or the sum of its parts in one filing.",
    },
    "current_long_term_debt": {
        "name": "current_long_term_debt",
        "label": "Long-term debt due within a year",
        "definition": "The part of the company's long-term loans that falls due within the next year.",
        "source": "The balance sheet line, or the sum of its parts in one filing.",
    },
}

# The four ratios (lane LK1c, DECISIONS #493). Each follows Damodaran, Investment Valuation, chapter 3 (the
# profitability and debt ratios) and, for the tax rate, chapter 10 "Effective versus Marginal Tax rate".
# Keys are the ratio names ratios.py uses; that module imports this map for the label/definition/formula.
RATIO_DEFINITIONS: dict[str, dict[str, str]] = {
    "ROE": {
        "name": "ROE",
        "label": "Return on equity",
        "definition": "How much profit the company makes for each dollar of shareholders' equity, "
                      "counting the equity as the average of the year's start and end. Higher is generally better.",
        "formula": "Return on equity = net income / average shareholders' equity",
        "source": "Damodaran, Investment Valuation, ch. 3, \"Measuring Profitability\".",
    },
    "ROA": {
        "name": "ROA",
        "label": "Return on assets",
        "definition": "How much profit the business earns for each dollar of assets, using the profit from "
                      "operations after tax, and the average of the year's start and end assets.",
        "formula": "Return on assets = operating income x (1 - income tax / income before tax) / average total assets",
        "source": "Damodaran, Investment Valuation, ch. 3, \"Measuring Profitability\"; the tax rate from "
                  "ch. 10, \"Effective versus Marginal Tax rate\".",
    },
    "net_profit_margin": {
        "name": "net_profit_margin",
        "label": "Net profit margin",
        "definition": "The share of every revenue dollar that becomes profit after all costs.",
        "formula": "Net profit margin = net income / revenue",
        "source": "Damodaran, Investment Valuation, ch. 3, \"Measuring Profitability\".",
    },
    "debt_to_equity": {
        "name": "debt_to_equity",
        "label": "Debt to equity",
        "definition": "How much borrowed money the company has compared with shareholders' equity. This counts "
                      "borrowed money (every short-term loan and the long-term loans), not all liabilities.",
        "formula": "Debt to equity = (short-term debt, including short-term borrowings and long-term debt due within "
                   "a year + long-term debt due after a year) / shareholders' equity",
        "source": "Damodaran, Investment Valuation, ch. 3, \"Debt Ratios\".",
    },
}


def definitions_payload() -> dict[str, dict[str, dict[str, str]]]:
    """The full definitions map the route serves (§2.2): figures + ratios, each keyed by name.

    Static content (no per-data computation), so the route can serve it directly and the frontend
    caches it forever (staleTime: Infinity). Returns copies so a caller cannot mutate the module
    constants.
    """
    return {
        # dict(entry) is a full copy here, not merely shallow: every value in an entry is a str
        # (immutable), so a caller mutating the returned map cannot reach the module constants.
        "figures": {name: dict(entry) for name, entry in FIGURE_DEFINITIONS.items()},
        "ratios": {name: dict(entry) for name, entry in RATIO_DEFINITIONS.items()},
    }
