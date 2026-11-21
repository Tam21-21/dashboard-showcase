import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LookupWindow from "../pages/research/lookup/LookupWindow";
import type { Company } from "../pages/research/lookup/names";
import { LayoutContext } from "../kit";
import { ApiError } from "../../lib/api";

const mocks = vi.hoisted(() => ({
  getHistory: vi.fn(), getNews: vi.fn(), search: vi.fn(),
  fundamentalsHistory: vi.fn(), fundamentalsStatus: vi.fn(), fundamentalsBackfill: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const { fundamentalsHistory, fundamentalsStatus, fundamentalsBackfill } = mocks;
  return {
    ...real,
    api: {
      ...real.api,
      display: { getHistory: mocks.getHistory, getNews: mocks.getNews, search: mocks.search },
      research: { ...real.api.research, fundamentalsHistory, fundamentalsStatus, fundamentalsBackfill },
    },
  };
});

const APPLE: Company = { ticker: "AAPL", name: "Apple Inc.", cik: 320193, symbol: "AAPL.US" };
const NEVER = () => new Promise(() => undefined);

// Synthetic: three fiscal years of a made-up company shaped like the answer of GET /api/research/fundamentals/history.
const P = ["2026-09-26", "2025-09-27", "2024-09-28"];
const FILED = ["2026-11-03", "2025-10-31", "2024-11-01"];
type V = { period_end: string; value: number; filing_date: string; as_filed?: number };
function series(concept: string, label: string, vals: Array<number | null>, unit = "USD") {
  const values: V[] = [];
  vals.forEach((value, i) => { if (value !== null) values.push({ period_end: P[i], value, filing_date: FILED[i] }); });
  return { concept, label, unit, values };
}
function accounts() {
  const eps = series("eps_diluted", "Diluted earnings per share", [6.5, 4.8, null], "USD/shares");
  eps.values[0] = { ...eps.values[0], as_filed: 26 };
  const ratio = (name: string, label: string, v: Array<number | null>, extra: Record<string, unknown> = {}) => ({
    name, label, definition: `${label}, in words.`, formula: `${label} formula`, source: "Damodaran, ch. 3",
    values: P.map((p, i) => ({ period_end: p, value: v[i], reason: v[i] === null ? (name === "debt_to_equity" ? "Short-term debt not reported as of this date." : "Equity for the year before is not held.") : null, ...(i === 0 ? extra : {}) })),
  });
  return {
    cik: 320193, on_record: true, periods: P,
    concepts: [
      series("revenue", "Revenue", [400e9, 320e9, 300e9]),
      series("cost_of_revenue", "Cost of revenue", [240e9, 200e9, null]),
      series("gross_profit", "Gross profit", [160e9, 120e9, null]),
      series("research_development", "Research and development", [30e9, 25e9, null]),
      series("sga", "Selling, general and administrative", [25e9, 22e9, null]),
      series("operating_income", "Operating income", [120e9, 90e9, null]),
      series("pretax_income", "Income before tax", [120e9, 90e9, null]),
      series("income_tax", "Income tax", [20e9, 15e9, null]),
      series("net_income", "Net income", [100e9, 75e9, null]),
      eps,
      series("total_assets", "Total assets", [500e9, 450e9, null]),
      series("total_liabilities", "Total liabilities", [300e9, 280e9, null]),
      series("shareholders_equity", "Shareholders' equity", [200e9, 170e9, null]),
      series("cash", "Cash", [50e9, 40e9, null]),
      series("receivables", "Receivables", [30e9, 25e9, null]),
      series("short_term_borrowings", "Short-term borrowings", [10e9, 8e9, null]),
      series("current_long_term_debt", "Long-term debt due within a year", [5e9, 4e9, null]),
      series("long_term_debt", "Long-term debt", [65e9, 60e9, null]),
      series("operating_cash_flow", "Cash from operations", [130e9, 110e9, null]),
      series("capex", "Capital spending", [12e9, 11e9, null]),
      series("dividends_paid", "Dividends paid", [15e9, 14e9, null]),
      series("buybacks", "Share buybacks", [90e9, 80e9, null]),
    ],
    ratios: [
      ratio("ROE", "Return on equity", [0.5, 0.4, null]),
      ratio("ROA", "Return on assets", [0.2, 0.15, null]),
      ratio("net_profit_margin", "Net profit margin", [0.25, 0.234, null]),
      ratio("debt_to_equity", "Debt to equity", [0.7147, 1.338, null], { debt: 80111000000 }),
    ],
    splits: [{ ex_date: "2026-12-01", ratio: 4 }], split_error: null,
  };
}
const NOT_HELD = { cik: 320193, on_record: false, periods: [], concepts: [], ratios: [], splits: null, split_error: null };

function wrap(ui: React.ReactElement, layout: "phone" | "desktop" | null = null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><LayoutContext.Provider value={layout}>{ui}</LayoutContext.Provider></MemoryRouter>
    </QueryClientProvider>,
  );
}
const open = () => wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />);
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const flush = async () => { for (let i = 0; i < 30; i++) await tick(1); };
const dlg = () => screen.getByRole("dialog");
const card = (name: string) => dlg().querySelector<HTMLElement>(`[data-name="${name}"]`)!;
const panel = (title: string) => within(dlg()).getByRole("heading", { name: title }).closest("section")!;
const rowNames = (s: HTMLElement) => Array.from(s.querySelectorAll(".lk-lv:not(.head) .lk-lv-n")).map((n) => n.textContent);
const rowOf = (s: HTMLElement, name: string) => Array.from(s.querySelectorAll<HTMLElement>(".lk-lv:not(.head)")).find((r) => r.querySelector(".lk-lv-n")!.textContent === name)!;
const ratioCard = (name: string) => within(dlg()).getByText(name).closest(".lk-r") as HTMLElement;

beforeEach(() => {
  vi.useFakeTimers();
  Object.values(mocks).forEach((m) => m.mockReset());
  mocks.getHistory.mockImplementation(NEVER);
  mocks.getNews.mockImplementation(NEVER);
  mocks.fundamentalsHistory.mockImplementation(async () => accounts());
  mocks.fundamentalsStatus.mockImplementation(async () => ({ source: "sec_edgar", recent_batches: [], latest_filing_date: null, total_facts_on_record: 0, per_year: [] }));
  mocks.fundamentalsBackfill.mockResolvedValue({ dispatched: true, job: "x", kind: "fetch", cik_count: 1 });
});
afterEach(() => { vi.useRealTimers(); });

describe("a1 a held company: the card and the sentence", () => {
  it("asks for the history of AAPL, orders the cards, and fills the Revenue card and the sentence", async () => {
    open(); await flush();
    expect(mocks.fundamentalsHistory).toHaveBeenCalledWith(320193, "AAPL");
    expect(Array.from(dlg().querySelectorAll(".lc-wall [data-name]")).map((c) => c.getAttribute("data-name"))).toEqual(["Share price", "Revenue", "News"]);
    const c = card("Revenue");
    expect(c.querySelector(".lc-fig .num")!.textContent).toBe("$400.0bn");
    expect(within(c).getByText("Up 25.0% on 2025")).toBeTruthy();
    const facts = Object.fromEntries(Array.from(c.querySelectorAll(".lc-facts > span")).map((s) => [s.querySelector("i")!.textContent, s.querySelector("b")!.textContent]));
    expect(facts).toEqual({ "Gross profit": "$160.0bn", "Operating income": "$120.0bn", "Net income": "$100.0bn" });
    expect(dlg().querySelector(".nl-say .more")!.textContent).toContain("Its last yearly report, filed 3 Nov 2026, shows revenue of $400.0bn, up 25.0% on the year before.");
  });
});

describe("a2 the income statement", () => {
  it("lists the years, the rows and the earnings per share, bold on the bigger amount and colour only where it means something", async () => {
    open(); await flush();
    const s = panel("Income statement");
    expect(Array.from(s.querySelectorAll(".lk-lv.head > *")).map((n) => n.textContent)).toEqual(["", "", "2025", "2026", "Change"]);
    expect(rowNames(s)).toEqual(["Revenue", "Cost of revenue", "Gross profit", "Research and development", "Selling, general and administrative", "Operating income", "Income tax", "Net income", "Diluted earnings per share"].map((n) => (n.startsWith("Diluted") ? "Earnings per share" : n)));
    const rev = rowOf(s, "Revenue");
    expect(rev.querySelector("b.num")!.textContent).toBe("$400.0bn");
    expect(rev.querySelector("b.num")!.classList.contains("top")).toBe(true);
    expect(rev.querySelector(".was")!.textContent).toBe("$320.0bn");
    expect(rev.querySelector(".was")!.classList.contains("top")).toBe(false);
    const chg = (r: HTMLElement) => r.lastElementChild as HTMLElement;
    expect(chg(rev).textContent).toBe("+25.0%");
    expect(chg(rev).classList.contains("good")).toBe(true);
    const cost = chg(rowOf(s, "Cost of revenue"));
    expect(cost.textContent).toBe("+20.0%");
    expect(cost.classList.contains("good") || cost.classList.contains("bad")).toBe(false);
    const eps = rowOf(s, "Earnings per share");
    expect(eps.querySelector("b.num")!.textContent).toBe("$6.50");
    expect(eps.getAttribute("title")).toContain("filed as $26.00, before the 4 for 1 split of 1 Dec 2026");
  });
});

describe("a3 the balance sheet", () => {
  it("is dated, lists what it owes under total liabilities and leaves out a figure it does not have", async () => {
    open(); await flush();
    const s = panel("Balance sheet");
    expect(within(s).getByText("At 26 Sep 2026")).toBeTruthy();
    const names = rowNames(s);
    const at = names.indexOf("Total liabilities");
    expect(names.slice(at, at + 4)).toEqual(["Total liabilities", "Short-term borrowings", "Long-term debt due within a year", "Long-term debt"]);
    expect(names).not.toContain("Inventory");
    expect(names).toContain("Cash");
  });
});

describe("a4 a year in focus", () => {
  it("moves every panel and leaves the card alone", async () => {
    open(); await flush();
    const group = within(dlg()).getByRole("group", { name: "Fiscal year" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["2024", "2025", "2026"]);
    fireEvent.click(within(group).getByRole("button", { name: "2025" }));
    await flush();
    expect(Array.from(panel("Income statement").querySelectorAll(".lk-lv.head > *")).map((n) => n.textContent)).toEqual(["", "", "2024", "2025", "Change"]);
    expect(within(panel("Balance sheet")).getByText("At 27 Sep 2025")).toBeTruthy();
    expect(ratioCard("Return on equity").querySelector("b")!.textContent).toBe("40.0%");
    expect(card("Revenue").querySelector(".lc-fig .num")!.textContent).toBe("$400.0bn");
  });

  it("reads the years as '24 '25 '26 on a phone", async () => {
    wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />, "phone"); await flush();
    const group = within(dlg()).getByRole("group", { name: "Fiscal year" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["'24", "'25", "'26"]);
  });
});

describe("a5 the four ratios", () => {
  it("shows them in the answer's order with the server's value, words and reasons", async () => {
    open(); await flush();
    const names = Array.from(dlg().querySelectorAll(".lk-r .lk-rn")).map((n) => n.textContent);
    expect(names).toEqual(["Return on equity", "Return on assets", "Net profit margin", "Debt to equity"]);
    const roe = ratioCard("Return on equity");
    expect(roe.querySelector("b")!.textContent).toBe("50.0%");
    expect(within(roe).getByText("Up from 40.0% the year before")).toBeTruthy();
    const de = ratioCard("Debt to equity");
    expect(de.querySelector("b")!.textContent).toBe("0.71×");
    expect(de.getAttribute("title")).toContain("$80.1bn");
    expect(de.getAttribute("title")).toContain("Debt to equity formula");
    fireEvent.click(within(within(dlg()).getByRole("group", { name: "Fiscal year" })).getByRole("button", { name: "2024" }));
    await flush();
    expect(within(ratioCard("Debt to equity")).getByText("Short-term debt not reported as of this date.")).toBeTruthy();
    expect(ratioCard("Debt to equity").querySelector("b")!.textContent).toBe("-");
  });
});

describe("a6 a company that is not held", () => {
  const batch = (id: number, status: string, failure_class: string | null = null) => ({
    batch_id: id, kind: "fetch", source: "sec_edgar", started_at: 1, finished_at: null, status, cik_count: 1, fact_count: 0,
    quarantine_count: 0, quality_report: null, failure_class,
  });
  const srv = { held: false, batches: [] as ReturnType<typeof batch>[] };
  beforeEach(() => {
    srv.held = false; srv.batches = [];
    mocks.fundamentalsHistory.mockImplementation(async () => (srv.held ? accounts() : NOT_HELD));
    mocks.fundamentalsStatus.mockImplementation(async () => ({ source: "sec_edgar", recent_batches: srv.batches, latest_filing_date: null, total_facts_on_record: 0, per_year: [] }));
  });

  it("starts one fetch, says Fetching, and fills when the batch ends", async () => {
    mocks.fundamentalsBackfill.mockImplementation(async () => { srv.batches = [batch(7, "running")]; return { dispatched: true, job: "x", kind: "fetch", cik_count: 1 }; });
    open(); await flush();
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(1);
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledWith({ ciks: [320193] });
    expect(within(card("Revenue")).getByText("Fetching")).toBeTruthy();
    await tick(10_000); await flush();
    expect(within(card("Revenue")).getByText("Fetching")).toBeTruthy();
    srv.batches = [batch(7, "succeeded")]; srv.held = true;
    await tick(10_000); await flush();
    expect(card("Revenue").querySelector(".lc-fig .num")!.textContent).toBe("$400.0bn");
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(1);
  });

  it("follows a running batch when the start is refused, then starts once", async () => {
    let first = true;
    mocks.fundamentalsBackfill.mockImplementation(async () => {
      if (first) { first = false; srv.batches = [batch(5, "running")]; throw new ApiError("x", 409, { detail: "A fundamentals fetch is already running." }); }
      srv.batches = [batch(6, "running"), ...srv.batches];
      return { dispatched: true, job: "x", kind: "fetch", cik_count: 1 };
    });
    open(); await flush();
    expect(screen.queryByText("Not read")).toBeNull();
    expect(within(card("Revenue")).getByText("Fetching")).toBeTruthy();
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(1);
    srv.batches = [batch(5, "succeeded")];
    await tick(10_000); await flush();
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(2);
    srv.batches = [batch(6, "succeeded"), batch(5, "succeeded")]; srv.held = true;
    await tick(10_000); await flush();
    expect(card("Revenue").querySelector(".lc-fig .num")!.textContent).toBe("$400.0bn");
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(2);
  });

  it("starts nothing while another batch runs at opening, then one when it ends", async () => {
    srv.batches = [batch(3, "running")];
    mocks.fundamentalsBackfill.mockImplementation(async () => { srv.batches = [batch(4, "running"), ...srv.batches]; return { dispatched: true, job: "x", kind: "fetch", cik_count: 1 }; });
    open(); await flush();
    expect(mocks.fundamentalsBackfill).not.toHaveBeenCalled();
    srv.batches = [batch(3, "succeeded")];
    await tick(10_000); await flush();
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(1);
  });

  it("says why when its own batch ends without figures", async () => {
    mocks.fundamentalsBackfill.mockImplementation(async () => { srv.batches = [batch(8, "running")]; return { dispatched: true, job: "x", kind: "fetch", cik_count: 1 }; });
    open(); await flush();
    srv.batches = [batch(8, "failed", "sec_unreachable")];
    await tick(10_000); await flush();
    const words = within(dlg()).getByText("The fetch from the SEC ended without figures for Apple.");
    expect(words.getAttribute("title")).toBe("Sec unreachable");
    expect(mocks.fundamentalsBackfill).toHaveBeenCalledTimes(1);
  });
});

describe("a7 on record with no yearly report", () => {
  it("says so in the card and the panel", async () => {
    mocks.fundamentalsHistory.mockImplementation(async () => ({ ...NOT_HELD, on_record: true }));
    open(); await flush();
    expect(within(card("Revenue")).getByText("No yearly report")).toBeTruthy();
    expect(within(dlg()).getByText("No yearly report of Apple is held.")).toBeTruthy();
    expect(mocks.fundamentalsBackfill).not.toHaveBeenCalled();
  });
});
