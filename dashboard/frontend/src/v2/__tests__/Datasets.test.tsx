import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Markets from "../pages/research/Markets";
import { SettingsProvider } from "../settings";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";
import { isStale } from "../pages/research/datasets/stale";

const m = vi.hoisted(() => ({
  fetchDatasets: vi.fn(),
  fetchEventFeeds: vi.fn(),
  fetchFundamentalsStatus: vi.fn(),
  fetchUniverseMembers: vi.fn(),
  createSeries: vi.fn(),
  fetchToLatest: vi.fn(),
  deleteSeries: vi.fn(),
  restoreSeries: vi.fn(),
}));
// The keys stay real; every call to the server is a stand-in.
vi.mock("../data/datasetsApi", async (orig) => ({ ...(await orig<typeof import("../data/datasetsApi")>()), ...m }));
// The Markets tab is not under test here: its own calls never answer, so it stays in its loading state.
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const wait = () => new Promise(() => undefined);
  return {
    ...real,
    api: { ...real.api, display: { getQuotes: wait, getHistory: wait, getYieldCurve: wait, getCommodities: wait, getMarketNews: wait, search: wait } },
  };
});

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "data-library")!;

const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
let nextId = 1;
const price = (symbol: string, source: string, timeframe: string, status: string, extra: Record<string, unknown> = {}) => ({
  id: nextId++, name: `${symbol} ${source} ${timeframe}`, source, symbol, timeframe, start_ts: 0, end_ts: 0, status,
  rows: null, covered_start_ts: null, covered_end_ts: null, last_fetched_at: null, dataset_type: "price_dataset", ...extra,
});
const base = () => {
  nextId = 1;
  return [
    price("AAPL", "eodhd", "1d", "ready", { rows: 11000, covered_start_ts: day("1980-12-12"), covered_end_ts: day("2026-10-10"), last_fetched_at: day("2026-10-11") }),
    price("MSFT", "eodhd", "1d", "fetching"),
    price("KO", "eodhd", "1d", "ready"),
    price("NVDA", "alpaca_sip", "15m", "ready", { rows: 79470, covered_start_ts: day("2021-01-04"), covered_end_ts: day("2026-08-20") }),
    price("TSLA", "alpaca_sip", "15m", "failed"),
    price("SPY", "alpaca_sip", "1d", "ready", { rows: 2043, covered_start_ts: day("2018-01-02"), covered_end_ts: day("2026-07-29") }),
    { ...price("My list", "eodhd", "1d", "ready"), id: 90, name: "My list", symbol: "", dataset_type: "price_universe" },
    { ...price("Earnings", "eodhd", "1d", "failed"), id: 91, name: "Earnings", symbol: "", dataset_type: "event_series", event_type: "analyst_rating", last_error: "docker exited -7" },
  ];
};
const FEEDS = [
  { event_type: "news", total_count: 1200, latest_knowledge_at: day("2026-10-09"), recent_events: [], per_year: [{ year: 2025, count: 700 }, { year: 2026, count: 500 }] },
  { event_type: "analyst_rating", total_count: 2560, latest_knowledge_at: day("2026-10-08"), recent_events: [], per_year: [{ year: 2026, count: 2560 }] },
];
const FUND = {
  source: "sec", recent_batches: [], latest_filing_date: day("2026-08-26"), total_facts_on_record: 50000,
  per_year: [{ year: 2009, count: 20000 }, { year: 2026, count: 30000 }],
};
const refused = (detail: string) => new ApiError("409 /x", 409, { detail }, "/x");
const deferred = () => {
  let resolve!: (v: unknown) => void;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
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
const card = (name: string) => document.querySelector<HTMLElement>(`[data-testid="kind-card"][data-name="${name}"]`)!;
const factOf = (name: string, label: string) =>
  Array.from(card(name).querySelectorAll<HTMLElement>(".lc-facts > span")).find((f) => f.querySelector("i")?.textContent === label)!.querySelector("b")!.textContent;
async function openTab() {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
  await screen.findAllByTestId("kind-card");
  await waitFor(() => expect(card("Daily prices, US").querySelector(".lc-fig .num")!.textContent).not.toBe("-"));
}
// A kind opened in its window; the table inside lists that kind's datasets.
async function openKind(name = "Daily prices, US") {
  await openTab();
  fireEvent.click(card(name));
  await screen.findByTestId("ds-table");
}
const rows = () => Array.from(screen.getByTestId("ds-table").querySelectorAll("tbody tr")) as HTMLElement[];
const rowOf = (symbol: string) => rows().find((r) => within(r).queryByText(symbol))!;
const cells = (r: HTMLElement) => Array.from(r.querySelectorAll("td")).map((c) => c.textContent);
const typeSearch = (v: string) => fireEvent.change(screen.getByLabelText("Find any dataset by name"), { target: { value: v } });
const say = () => document.querySelector(".nl-say")!.textContent;

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  m.fetchDatasets.mockResolvedValue(base());
  m.fetchEventFeeds.mockResolvedValue(FEEDS);
  m.fetchFundamentalsStatus.mockResolvedValue(FUND);
  m.fetchUniverseMembers.mockResolvedValue({ universe_id: 90, members: ["AAPL", "XOM"] });
  m.createSeries.mockResolvedValue({ id: 500, status: "pending" });
  m.fetchToLatest.mockResolvedValue({ id: 1, status: "fetching" });
  m.deleteSeries.mockResolvedValue({ ok: true });
  m.restoreSeries.mockResolvedValue({});
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const f of Object.values(m)) f.mockReset();
});

describe("Data library, Datasets tab", () => {
  it("(a) the tab row has all five tabs, and the choice survives a remount", async () => {
    const first = mount();
    for (const n of ["Markets", "Universes", "Events", "Fundamentals"]) expect(screen.getByRole("button", { name: n })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
    await screen.findAllByTestId("kind-card");
    first.unmount();
    mount();
    await screen.findAllByTestId("kind-card");
    expect(screen.getByRole("button", { name: "Datasets" })).toHaveAttribute("aria-pressed", "true");
  });

  it("(b) the mockup's sections render: headline, Datasets by kind, Price history, Last update", async () => {
    await openTab();
    expect(say()).toContain("2 of 3 price series are behind.");
    for (const h of ["Datasets by kind", "Price history", "Last update"]) expect(screen.getByRole("heading", { name: h })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add data" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh 2 behind" })).toBeInTheDocument();
  });

  it("(c) one card for each kind in the mockup's order, planned kinds saying none yet", async () => {
    await openTab();
    const names = Array.from(document.querySelectorAll('[data-testid="kind-card"]')).map((t) => t.getAttribute("data-name"));
    expect(names).toEqual([
      "Daily prices, US", "Daily prices, Alpaca", "15-minute prices", "Stock baskets", "Events", "Company accounts",
      "1-minute prices", "5-minute prices", "Dollar bars", "Futures prices", "Company filings",
    ]);
    const chipOf = (n: string) => card(n).querySelector(".nl-st")!.textContent;
    expect(chipOf("Daily prices, US")).toBe("1 fetching");
    expect(chipOf("Daily prices, Alpaca")).toBe("1 behind");
    expect(chipOf("15-minute prices")).toBe("1 failed");
    expect(chipOf("Dollar bars")).toBe("None yet");
    expect(card("Daily prices, US").querySelector(".lc-fig .num")!.textContent).toBe("3");
    expect(factOf("15-minute prices", "Rows")).toBe("79 k");
    expect(factOf("Daily prices, Alpaca", "Prices from")).toBe("2018");
    expect(factOf("Daily prices, Alpaca", "Newest price")).toBe("29 Jul");
    expect(card("Events").querySelector(".lc-fig .num")!.textContent).toBe("2");
    expect(factOf("Events", "Rows")).toBe("3,760");
    expect(card("Company accounts").querySelector(".lc-fig .num")!.textContent).toBe("1");
    expect(card("Stock baskets").querySelector(".lc-fig .num")!.textContent).toBe("1");
  });

  it("(d) pressing a kind lists its datasets, the most behind first, with their states and dashes", async () => {
    await openKind();
    expect(rows().map((r) => cells(r)[0])).toEqual(["AAPL", "KO", "MSFT"]);
    expect(rows().map((r) => cells(r)[6])).toEqual(["WORKING", "-", "FETCHING"]);
    const aapl = cells(rowOf("AAPL"));
    expect(aapl[2]).toBe("Daily");
    expect(aapl[4]).toBe("11,000");
    expect(aapl[5]).toBe("10 Oct");
    const ko = cells(rowOf("KO"));
    expect([ko[3], ko[4], ko[5]]).toEqual(["-", "-", "-"]);
  });

  it("(d2) the 15-minute kind lists its two series; the search inside a kind narrows it", async () => {
    await openKind("15-minute prices");
    expect(rows().map((r) => cells(r)[0])).toEqual(["NVDA", "TSLA"]);
    expect(cells(rowOf("NVDA"))[2]).toBe("15 minutes");
    fireEvent.change(screen.getByLabelText("Find in this kind"), { target: { value: "ts" } });
    expect(rows().map((r) => cells(r)[0])).toEqual(["TSLA"]);
  });

  it("(d3) the search box finds a dataset across every kind, and Clear the search closes the results", async () => {
    await openTab();
    typeSearch("a");
    await screen.findByRole("heading", { name: "Search results" });
    expect(rows().map((r) => cells(r)[0])).toEqual(["NVDA", "AAPL", "TSLA"]);
    fireEvent.click(screen.getByRole("button", { name: "Clear the search" }));
    expect(screen.queryByRole("heading", { name: "Search results" })).toBeNull();
  });

  it("(e) FETCH, DELETE and the toast's UNDO call the server by id", async () => {
    await openKind();
    fireEvent.click(within(rowOf("AAPL")).getByRole("button", { name: "FETCH" }));
    await waitFor(() => expect(m.fetchToLatest).toHaveBeenCalledWith(1));
    fireEvent.click(within(rowOf("AAPL")).getByRole("button", { name: "DELETE" }));
    const dlg = screen.getByRole("dialog", { name: "Delete AAPL?" });
    expect(within(dlg).getByText("Runs that used it keep their results.")).toBeInTheDocument();
    expect(m.deleteSeries).not.toHaveBeenCalled();
    fireEvent.click(within(dlg).getByRole("button", { name: "DELETE" }));
    await waitFor(() => expect(m.deleteSeries).toHaveBeenCalledWith(1));
    fireEvent.click(await screen.findByRole("button", { name: "UNDO" }));
    await waitFor(() => expect(m.restoreSeries).toHaveBeenCalledWith(1));
  });

  it("(f) Add data skips what is held, sends one series, and shows a refusal by its ticker", async () => {
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Add data" }));
    const dlg = screen.getByRole("dialog", { name: "Add data" });
    fireEvent.change(within(dlg).getByLabelText("Tickers"), { target: { value: "aapl, goog" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Fetch" }));
    await waitFor(() => expect(m.createSeries).toHaveBeenCalledTimes(1));
    expect(m.createSeries).toHaveBeenCalledWith({
      name: "GOOG eodhd 1d", source: "eodhd", symbol: "GOOG", timeframe: "1d",
      start: "2000-01-01T00:00:00Z", end: "2026-10-12T00:00:00Z",
    });
    expect(await screen.findByText("Fetching 1 series")).toBeInTheDocument();
  });

  it("(f) a refused ticker shows the server's words and the dialog stays", async () => {
    m.createSeries.mockRejectedValue(new ApiError("409 /x", 409, { detail: "No bars before 2004." }, "/x"));
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Add data" }));
    const dlg = screen.getByRole("dialog", { name: "Add data" });
    fireEvent.change(within(dlg).getByLabelText("Tickers"), { target: { value: "GOOG" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Fetch" }));
    expect(await within(dlg).findByText("GOOG: No bars before 2004.")).toBeInTheDocument();
    expect(screen.queryByText(/Fetching \d+ series/)).toBeNull();
  });

  it("(f) a universe adds every member not already held", async () => {
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Add data" }));
    const dlg = screen.getByRole("dialog", { name: "Add data" });
    fireEvent.click(within(dlg).getByRole("button", { name: "15 minutes" }));
    fireEvent.click(within(dlg).getByRole("button", { name: "Alpaca" }));
    fireEvent.change(within(dlg).getByLabelText("Universe"), { target: { value: "90" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Fetch" }));
    await waitFor(() => expect(m.createSeries).toHaveBeenCalledTimes(2));
    expect(m.createSeries.mock.calls.map((c) => c[0].name)).toEqual(["AAPL alpaca_sip 15m", "XOM alpaca_sip 15m"]);
  });

  it("w11 a universe shows in plain words with its id as value", async () => {
    m.fetchDatasets.mockResolvedValue([
      ...base(),
      { ...price("x", "eodhd", "1d", "ready"), id: 95, name: "sp500_constituents", symbol: "", dataset_type: "price_universe" },
    ]);
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Add data" }));
    const dlg = screen.getByRole("dialog", { name: "Add data" });
    const opt = within(dlg).getByRole("option", { name: "Sp500 constituents" }) as HTMLOptionElement;
    expect(opt.value).toBe("95");
  });

  it("w11 an unknown source and bars fall back to plain words with the codes as title; known ones carry no title", async () => {
    m.fetchDatasets.mockResolvedValue([
      price("ZZZ", "polygon_flat", "4h-x", "ready"),
      price("AAPL", "eodhd", "1d", "ready"),
    ]);
    await openTab();
    typeSearch("zzz");
    await screen.findByTestId("ds-table");
    const odd = rowOf("ZZZ");
    const src = within(odd).getByText("Polygon flat");
    expect(src.closest("[title]")!.getAttribute("title")).toBe("polygon_flat");
    expect(within(odd).getByText("4h x").getAttribute("title")).toBe("4h-x");
    typeSearch("aapl");
    const known = rowOf("AAPL");
    expect(within(known).getByText("EODHD").closest("[title]")).toBeNull();
    expect(within(known).getByText("Daily").getAttribute("title")).toBeNull();
  });

  it("(g) 35 rows show 30 and SHOW ALL 35 shows the rest", async () => {
    const many = Array.from({ length: 35 }, (_, i) => price(`T${String(i).padStart(2, "0")}`, "eodhd", "1d", "ready"));
    m.fetchDatasets.mockResolvedValue(many);
    await openKind();
    expect(rows()).toHaveLength(30);
    fireEvent.click(screen.getByRole("button", { name: "SHOW ALL 35" }));
    expect(rows()).toHaveLength(35);
  });

  it("(h) a failing event source shows its words on its own card only", async () => {
    m.fetchEventFeeds.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The events store is locked." }, "/x"));
    await openTab();
    await waitFor(() => expect(card("Events").querySelector(".nl-st")!.getAttribute("title")).toBe("The events store is locked."));
    expect(card("Daily prices, US").querySelector(".lc-fig .num")!.textContent).toBe("3");
    expect(card("Daily prices, US").querySelector(".nl-st")!.getAttribute("title")).toBeNull();
  });

  it("(h) an empty price list says so in the headline", async () => {
    m.fetchDatasets.mockResolvedValue([]);
    await openTab();
    expect(screen.getByText("No price series yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add data" })).toBeInTheDocument();
    expect(screen.getByText("No price series has dates yet.")).toBeInTheDocument();
  });

  it("(i) no snake_case and no em dash on the tab", async () => {
    await openKind();
    const text = document.querySelector(".mk")!.textContent!;
    expect(text).not.toMatch(/[a-z]+_[a-z0-9_]+/);
    expect(text).not.toContain(String.fromCharCode(8212));
  });

  it("(j) guard: the Markets tab still shows its wall", async () => {
    mount();
    expect(screen.getByRole("button", { name: "Markets" })).toHaveAttribute("aria-pressed", "true");
    expect(document.querySelectorAll('[data-testid="wall-tile"]').length).toBeGreaterThan(0);
    expect(screen.queryByTestId("ds-table")).toBeNull();
  });

  it("k1 a series is behind only past 7 days", () => {
    const now = day("2026-10-12");
    expect(isStale(now - 7 * 86400, now)).toBe(false);
    expect(isStale(now - 7 * 86400 - 1, now)).toBe(true);
  });

  it("k2 price history: one bar for each year, its title the count of series with prices that year", async () => {
    await openTab();
    const bars = Array.from(screen.getByTestId("price-history").querySelectorAll("i"));
    expect(bars).toHaveLength(47);
    expect(bars[0].getAttribute("title")).toBe("1980: 1 price series have prices that year");
    expect(bars[bars.length - 1].getAttribute("title")).toBe("2026: 3 price series have prices that year");
  });

  it("k3 last update: one row for each day a series stopped, the newest first, the stale ones amber", async () => {
    await openTab();
    const list = Array.from(screen.getByTestId("last-update").children) as HTMLElement[];
    expect(list).toHaveLength(3);
    expect(list[0].querySelector(".top span")!.textContent).toBe("10 Oct 2026, 3 days ago");
    expect(list[0].querySelector("b")!.className).toBe("good");
    expect(list[1].querySelector("b")!.className).toBe("warn");
    expect(list[2].querySelector("b")!.className).toBe("warn");
  });

  it("k5 Refresh asks first, then fetches one series at a time and says one thing", async () => {
    const first = deferred();
    m.fetchToLatest.mockReset();
    m.fetchToLatest.mockReturnValueOnce(first.promise).mockResolvedValue({ id: 6, status: "fetching" });
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Refresh 2 behind" }));
    const dlg = screen.getByRole("dialog", { name: "Fetch new bars for 2 stale series?" });
    expect(m.fetchToLatest).not.toHaveBeenCalled();
    fireEvent.click(within(dlg).getByRole("button", { name: "FETCH" }));
    await waitFor(() => expect(m.fetchToLatest).toHaveBeenCalledTimes(1));
    expect(m.fetchToLatest).toHaveBeenCalledWith(6);
    expect(screen.getByRole("button", { name: "Refresh 2 behind" })).toBeDisabled();
    first.resolve({ id: 4, status: "fetching" });
    await waitFor(() => expect(m.fetchToLatest).toHaveBeenCalledTimes(2));
    expect(m.fetchToLatest).toHaveBeenLastCalledWith(4);
    expect(await screen.findByText("Fetching new bars for 2 stale series")).toBeInTheDocument();
  });

  it("k7 a refusal is counted and named", async () => {
    m.fetchToLatest.mockReset();
    m.fetchToLatest.mockResolvedValueOnce({ id: 4, status: "fetching" }).mockRejectedValueOnce(refused("Another fetch is running."));
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Refresh 2 behind" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "FETCH" }));
    expect(await screen.findByText("Fetching new bars for 1 stale series, 1 refused, NVDA: Another fetch is running.")).toBeInTheDocument();
  });

  it("k7 when every series is refused the toast says only that", async () => {
    m.fetchToLatest.mockReset();
    m.fetchToLatest.mockRejectedValue(refused("Busy."));
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Refresh 2 behind" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "FETCH" }));
    expect(await screen.findByText("2 stale series refused, SPY: Busy.; NVDA: Busy.")).toBeInTheDocument();
  });

  it("k8 with nothing behind, Refresh is off and says why", async () => {
    m.fetchDatasets.mockResolvedValue([base()[0]]);
    await openTab();
    const b = screen.getByRole("button", { name: "Refresh 0 behind" });
    expect(b).toBeDisabled();
    expect(b.getAttribute("title")).toBe("No series is behind");
  });

  it("k9 CANCEL on the refresh confirm calls nothing", async () => {
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "Refresh 2 behind" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "CANCEL" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(m.fetchToLatest).not.toHaveBeenCalled();
  });

  it("k10 tabbing onto FETCH shows the row's buttons", async () => {
    await openKind();
    const acts = rowOf("AAPL").querySelector(".ds-acts")!;
    expect(acts.classList.contains("on")).toBe(false);
    act(() => within(rowOf("AAPL")).getByRole("button", { name: "FETCH" }).focus());
    expect(acts.classList.contains("on")).toBe(true);
    act(() => (document.activeElement as HTMLElement).blur());
    expect(acts.classList.contains("on")).toBe(false);
  });

  it("k12 a million bars or more read as 4.45 M, ten thousand up as 79 k", async () => {
    m.fetchDatasets.mockResolvedValue([price("AAPL", "eodhd", "1d", "ready", { rows: 4450000, covered_start_ts: day("2010-01-04"), covered_end_ts: day("2026-10-10") })]);
    await openTab();
    expect(factOf("Daily prices, US", "Rows")).toBe("4.45 M");
  });

  it("k13 a feed the server does not list is not counted", async () => {
    m.fetchEventFeeds.mockResolvedValue([FEEDS[1]]);
    await openTab();
    await waitFor(() => expect(card("Events").querySelector(".lc-fig .num")!.textContent).toBe("1"));
  });

  it("never asks for synthetic datasets", async () => {
    await openTab();
    expect(m.fetchDatasets).toHaveBeenCalled();
    expect(m.fetchDatasets.mock.calls[0]).toHaveLength(0);
  });
});
