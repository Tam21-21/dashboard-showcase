import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Markets from "../pages/research/Markets";
import { SettingsProvider } from "../settings";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const m = vi.hoisted(() => ({
  fetchStatus: vi.fn(),
  fetchPollStatus: vi.fn(),
  fetchDefinitions: vi.fn(),
  fetchFacts: vi.fn(),
  fetchCompare: vi.fn(),
  reapStuck: vi.fn(),
  startFetch: vi.fn(),
  startRefresh: vi.fn(),
  fetchUniverses: vi.fn(),
}));
vi.mock("../data/fundamentalsApi", async (orig) => ({ ...(await orig<typeof import("../data/fundamentalsApi")>()), ...m }));
// The other tabs are not under test: the Markets tab's own calls never answer.
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const wait = () => new Promise(() => undefined);
  return { ...real, api: { ...real.api, display: { getQuotes: wait, getHistory: wait, getYieldCurve: wait, getCommodities: wait, getMarketNews: wait, search: wait } } };
});

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "data-library")!;
const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;

const STATUS = {
  source: "sec", latest_filing_date: day("2026-10-09"), total_facts_on_record: 870645,
  per_year: [{ year: 2009, count: 20000 }, { year: 2025, count: 400000 }, { year: 2026, count: 450645 }],
  recent_batches: [
    { batch_id: 3, kind: "refresh", source: "sec_edgar", started_at: day("2026-10-05"), finished_at: null, status: "running", cik_count: 6, fact_count: 0, quarantine_count: 0, quality_report: null, failure_class: null },
    { batch_id: 2, kind: "refresh", source: "sec_edgar", started_at: day("2026-10-01"), finished_at: day("2026-10-01"), status: "succeeded", cik_count: 6, fact_count: 214, quarantine_count: 0, quality_report: null, failure_class: null },
  ],
};
const POLL = {
  last_poll: null, last_success_at: day("2026-10-11") + 3600, next_poll_at: day("2026-10-12") + 3600, late: false,
  companies_followed: 12, filings_pending: 1, filings_not_in_facts: 0,
};
const DEFS = {
  figures: { revenue: { name: "revenue", label: "Revenue", definition: "Everything the company sold in the year.", source: "income statement" } },
  ratios: { gross_margin: { name: "gross_margin", label: "Gross margin", definition: "Gross profit as a share of revenue.", formula: "Gross profit / Revenue" } },
};

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsProvider>
        <MemoryRouter>
          <Markets app={app} section={section} />
        </MemoryRouter>
      </SettingsProvider>
    </QueryClientProvider>,
  );
}
async function openTab() {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Fundamentals" }));
  await screen.findByTestId("fd-years");
}

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  m.fetchStatus.mockResolvedValue(STATUS);
  m.fetchPollStatus.mockResolvedValue(POLL);
  m.fetchDefinitions.mockResolvedValue(DEFS);
  m.fetchUniverses.mockResolvedValue([]);
  m.reapStuck.mockResolvedValue({ batches_reaped: [3] });
  m.startRefresh.mockResolvedValue({ dispatched: true, job: "x", kind: "refresh", cik_count: 2 });
  m.startFetch.mockResolvedValue({ dispatched: true, job: "x", kind: "fetch", cik_count: 2 });
  m.fetchFacts.mockResolvedValue({
    cik: 320193, as_of: 0, on_record: true,
    facts: [{ concept: "revenue", found: true, value: 3.9e11, fiscal_year: 2025, filing_date: day("2025-10-31") }],
    ratios: [{ name: "gross_margin", label: "Gross margin", available: true, value: 0.46, legs: [] }],
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const f of Object.values(m)) f.mockReset();
});

describe("Data library, Fundamentals tab", () => {
  it("the tab row has Fundamentals, and the page holds the mockup's sections", async () => {
    await openTab();
    expect(screen.getByRole("button", { name: "Fundamentals" })).toHaveAttribute("aria-pressed", "true");
    const heads = Array.from(document.querySelectorAll(".fd .nl-blk h2")).map((h) => h.textContent);
    expect(heads).toEqual(["Needs you", "Summary", "One company, as it was known", "Facts by filing year", "What each figure means", "Fetches"]);
    expect(document.querySelector(".nl-say")!.textContent).toContain("870,645 company figures are stored.");
  });

  it("the summary cards come from the server: facts held, companies, newest filing, daily SEC check", async () => {
    await openTab();
    const cards = Array.from(document.querySelectorAll<HTMLElement>('[data-testid="fd-card"]'));
    expect(cards.map((c) => c.getAttribute("data-name"))).toEqual(["Facts held", "Companies", "Newest filing", "Daily SEC check"]);
    expect(cards[0].querySelector(".lc-fig .num")!.textContent).toBe("871k");
    expect(cards[1].querySelector(".lc-fig .num")!.textContent).toBe("12");
    expect(cards[2].querySelector(".lc-fig .num")!.textContent).toBe("3 days");
    expect(cards[2].querySelector(".nl-st")!.textContent).toBe("Working");
    expect(cards[3].querySelector(".nl-st")!.textContent).toBe("On time");
  });

  it("a fetch running over a day is Needs you with Clear it, which asks the server once", async () => {
    await openTab();
    expect(screen.getByText("1 thing")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear it" }));
    await waitFor(() => expect(m.reapStuck).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Cleared 1 stuck fetch")).toBeInTheDocument();
  });

  it("facts by filing year draws one bar a year with its count on hover, and Fetches lists the batches", async () => {
    await openTab();
    const bars = Array.from(screen.getByTestId("fd-years").querySelectorAll("i"));
    expect(bars).toHaveLength(3);
    expect(bars[2].getAttribute("title")).toContain("Filed in 2026: 450,645 figures");
    const rows = Array.from(screen.getByTestId("fd-batches").querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("Refresh");
    expect(rows[0].textContent).toContain("Running");
    expect(rows[1].textContent).toContain("Finished");
  });

  it("What each figure means lists the server's definitions", async () => {
    await openTab();
    expect(await screen.findByText("Everything the company sold in the year.")).toBeInTheDocument();
    expect(screen.getByText("Gross profit / Revenue")).toBeInTheDocument();
  });

  it("One company looks a company up as known on a date and shows its figures and ratios", async () => {
    await openTab();
    fireEvent.change(screen.getByLabelText("SEC company numbers"), { target: { value: "320193" } });
    fireEvent.click(screen.getByRole("button", { name: "Look up" }));
    const tbl = await screen.findByTestId("fd-facts");
    expect(within(tbl).getByText("Revenue")).toBeInTheDocument();
    expect(within(tbl).getByText("$390.00bn")).toBeInTheDocument();
    expect(within(tbl).getByText("0.460")).toBeInTheDocument();
    expect(m.fetchFacts).toHaveBeenCalledWith(320193, expect.any(Number));
  });

  it("Fetch or refresh sends the company numbers and says what started; a refusal stays in the window", async () => {
    m.fetchStatus.mockResolvedValue({ ...STATUS, recent_batches: [STATUS.recent_batches[1]] });
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Fetch or refresh" }));
    const dlg = screen.getByRole("dialog", { name: "Fetch or refresh company figures" });
    fireEvent.click(within(dlg).getByRole("button", { name: "Company numbers" }));
    fireEvent.change(within(dlg).getByLabelText("SEC company numbers"), { target: { value: "320193, 789019" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(m.startRefresh).toHaveBeenCalledWith({ ciks: [320193, 789019] }));
    expect(await screen.findByText("Started: 2 companies")).toBeInTheDocument();
  });

  it("a refused start shows the server's words and keeps the window", async () => {
    m.fetchStatus.mockResolvedValue({ ...STATUS, recent_batches: [STATUS.recent_batches[1]] });
    m.startRefresh.mockRejectedValue(new ApiError("409 /x", 409, { detail: "A fundamentals fetch is already running." }, "/x"));
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Fetch or refresh" }));
    const dlg = screen.getByRole("dialog", { name: "Fetch or refresh company figures" });
    fireEvent.click(within(dlg).getByRole("button", { name: "Company numbers" }));
    fireEvent.change(within(dlg).getByLabelText("SEC company numbers"), { target: { value: "1" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Start" }));
    expect(await within(dlg).findByText("A fundamentals fetch is already running.")).toBeInTheDocument();
  });

  it("nothing held says so instead of a number, and a failed read shows the server's words", async () => {
    m.fetchStatus.mockResolvedValue({ source: "sec", recent_batches: [], latest_filing_date: null, total_facts_on_record: 0, per_year: [] });
    m.fetchPollStatus.mockRejectedValue(new Error("The daily SEC check could not be read: 500 Server Error"));
    await openTab().catch(() => undefined);
    expect(await screen.findByText("No company figures are stored.")).toBeInTheDocument();
    expect(screen.getByText("No filing year is held yet.")).toBeInTheDocument();
    expect(screen.getByText("No fetch yet.")).toBeInTheDocument();
  });

  it("uses no em dash and no snake_case in anything it shows", async () => {
    await openTab();
    const text = document.querySelector(".mk")!.textContent!;
    expect(text).not.toMatch(/[a-z]+_[a-z0-9_]+/);
    expect(text).not.toContain(String.fromCharCode(8212));
  });
});
