import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../lib/api";
import { usePageMemory } from "../kit";
import AskClaude from "../pages/calendar/AskClaude";
import Calendar from "../pages/calendar/Calendar";
import Markets from "../pages/research/Markets";
import Paper from "../pages/trading/Paper";
import Runs from "../pages/research/Runs";
import { SettingsProvider } from "../settings";
import { APPS } from "../shell/sections";

const mocks = vi.hoisted(() => ({
  getQuotes: vi.fn(),
  getHistory: vi.fn(),
  getYieldCurve: vi.fn(),
  getCommodities: vi.fn(),
  getMarketNews: vi.fn(),
  search: vi.fn(),
  listRuns: vi.fn(),
  runsSummary: vi.fn(),
  listStrategies: vi.fn(),
  getRunningRuns: vi.fn(),
  getSummary: vi.fn(),
  getRunFills: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  return {
    ...real,
    api: {
      ...real.api,
      display: {
        getQuotes: mocks.getQuotes, getHistory: mocks.getHistory, getYieldCurve: mocks.getYieldCurve,
        getCommodities: mocks.getCommodities, getMarketNews: mocks.getMarketNews, search: mocks.search,
      },
      research: {
        ...real.api.research,
        listRuns: (...a: unknown[]) => mocks.listRuns(...a),
        runsSummary: (...a: unknown[]) => mocks.runsSummary(...a),
        listStrategies: (...a: unknown[]) => mocks.listStrategies(...a),
      },
    },
  };
});
vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  const names = [
    { id: 1, name: "alpha_gap", display_name: "Alpha gap", status: "active" },
    { id: 2, name: "beta_drift", display_name: "Beta drift", status: "active" },
  ];
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => Promise.resolve(names) }) };
});
vi.mock("../data/paperApi", () => ({
  getRunningRuns: () => mocks.getRunningRuns(),
  getSummary: (w: string) => mocks.getSummary(w),
  getRunFills: (id: number) => mocks.getRunFills(id),
  getHoldings: () => new Promise(() => {}),
  getPaperLimits: () => new Promise(() => {}),
  putPaperLimits: () => new Promise(() => {}),
}));

const MIN = 60 * 1000;
const KEY = "v2-memory:t/a";

function Probe({ k = "t/a" }: { k?: string }) {
  const [v, set] = usePageMemory<{ text: string }>(k, { text: "" });
  return <input aria-label="probe" value={v.text} onChange={(e) => set({ text: e.target.value })} />;
}
const probe = () => (screen.getByLabelText("probe") as HTMLInputElement).value;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Object.values(mocks).forEach((m) => m.mockReset());
});

describe("usePageMemory", () => {
  it("(f) keeps a value for 15 minutes after leaving and drops it after", () => {
    const a = render(<Probe />);
    fireEvent.change(screen.getByLabelText("probe"), { target: { value: "hello" } });
    a.unmount();
    vi.setSystemTime(Date.now() + 14 * MIN + 59_000);
    const b = render(<Probe />);
    expect(probe()).toBe("hello");
    b.unmount();
    vi.setSystemTime(Date.now() + 15 * MIN + 1_000);
    render(<Probe />);
    expect(probe()).toBe("");
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it("(g) the 15 minutes run from leaving, an unstamped value from its last change, and pagehide stamps", () => {
    const t0 = Date.now();
    const a = render(<Probe />);
    fireEvent.change(screen.getByLabelText("probe"), { target: { value: "kept" } });
    vi.setSystemTime(t0 + 10 * MIN);
    a.unmount();
    vi.setSystemTime(t0 + 24 * MIN);
    const b = render(<Probe />);
    expect(probe()).toBe("kept");
    b.unmount();
    cleanup();
    sessionStorage.clear();

    // a value never stamped (a reload): 15 minutes after its last change
    sessionStorage.setItem(KEY, JSON.stringify({ v: { text: "raw" }, at: Date.now() - 14 * MIN, leftAt: null }));
    const c = render(<Probe />);
    expect(probe()).toBe("raw");
    c.unmount();
    sessionStorage.setItem(KEY, JSON.stringify({ v: { text: "raw" }, at: Date.now() - 16 * MIN, leftAt: null }));
    render(<Probe />);
    expect(probe()).toBe("");
    cleanup();
    sessionStorage.clear();

    // pagehide stamps while still mounted
    render(<Probe />);
    fireEvent.change(screen.getByLabelText("probe"), { target: { value: "bye" } });
    expect(JSON.parse(sessionStorage.getItem(KEY) as string).leftAt).toBeNull();
    const stamp = Date.now() + 5 * MIN;
    vi.setSystemTime(stamp);
    window.dispatchEvent(new Event("pagehide"));
    expect(JSON.parse(sessionStorage.getItem(KEY) as string).leftAt).toBe(stamp);
  });

  it("(h) a storage error keeps the field working and logs once with the key", () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    render(<Probe />);
    const box = screen.getByLabelText("probe");
    fireEvent.change(box, { target: { value: "a" } });
    fireEvent.change(box, { target: { value: "ab" } });
    expect(probe()).toBe("ab");
    expect(err).toHaveBeenCalledTimes(1);
    expect(String(err.mock.calls[0][0])).toContain("page memory t/a");
  });
});

const calApp = APPS.find((a) => a.id === "calendar")!;
const calSection = calApp.sections[0];

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsProvider>
        <MemoryRouter>{node}</MemoryRouter>
      </SettingsProvider>
    </QueryClientProvider>,
  );
}
function stubLife() {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "calendarRange").mockResolvedValue([]);
  vi.spyOn(api.life, "weather").mockRejectedValue(new Error("none"));
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([]);
  vi.spyOn(api.life, "getTemplates").mockResolvedValue([]);
}
const GYM = { kind: "add", title: "Gym", date: "2026-10-14", start: "18:00", end: "19:00", all_day: false };

describe("Ask Claude memory", () => {
  it("(i) the conversation comes back through the window after a remount, with no second ask, and Done stays", async () => {
    stubLife();
    const ask = vi.spyOn(api.life as unknown as { ask: (q: string, h: unknown[]) => Promise<unknown> }, "ask")
      .mockResolvedValue({ answer: "Try this.", proposals: [GYM] });
    vi.spyOn(api.life, "calendarCreate").mockResolvedValue({ sync_error: null } as never);
    wrap(<AskClaude />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Claude" }));
    fireEvent.change(screen.getByLabelText("Question for Claude"), { target: { value: "Plan my week" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(await screen.findByRole("button", { name: "Apply" }));
    await screen.findByText("Done");
    cleanup();
    wrap(<AskClaude />);
    fireEvent.click(screen.getByRole("button", { name: "Ask Claude" }));
    expect(await screen.findByText("Try this.")).toBeInTheDocument();
    expect(screen.getByText("Plan my week")).toBeInTheDocument();
    expect(screen.getByText("Add: Gym, Wed 14 Oct 18:00 to 19:00")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    expect(ask).toHaveBeenCalledTimes(1);
  });
});

describe("Calendar memory", () => {
  const mountCal = () => wrap(<Calendar app={calApp} section={calSection} />);

  it("(j) an open NEW EVENT form comes back with its title, and a create clears it", async () => {
    stubLife();
    const create = vi.spyOn(api.life, "calendarCreate").mockResolvedValue({} as never);
    mountCal();
    fireEvent.click(screen.getByRole("button", { name: "New event" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Dentist" } });
    cleanup();
    mountCal();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Dentist");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    cleanup();
    mountCal();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "New event" }));
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("");
  });

  it("(j) the template add form comes back and a save clears it", async () => {
    stubLife();
    const add = vi.spyOn(api.life, "createTemplate").mockResolvedValue({} as never);
    mountCal();
    fireEvent.click(screen.getByRole("button", { name: /Templates/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add template" }));
    fireEvent.change(screen.getByLabelText("Template title"), { target: { value: "Gym" } });
    cleanup();
    mountCal();
    expect((screen.getByLabelText("Template title") as HTMLInputElement).value).toBe("Gym");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByLabelText("Template title")).toBeNull());
    cleanup();
    mountCal();
    expect(screen.queryByLabelText("Template title")).toBeNull();
    expect(sessionStorage.getItem("v2-memory:calendar/template")).toBeNull();
  });

  it("(k) the view and the anchor come back", async () => {
    stubLife();
    mountCal();
    fireEvent.click(screen.getByRole("button", { name: "Month" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    const title = document.querySelector(".cmt h2")!.textContent;
    expect(title).toContain("November");
    cleanup();
    mountCal();
    expect(document.querySelector(".cmt h2")!.textContent).toBe(title);
  });
});

describe("Markets, Runs and Paper memory", () => {
  it("(k) Markets: the chart window comes back with its target, period and search", async () => {
    const day = (i: number) => new Date(Date.UTC(2025, 0, 1) + i * 86400000);
    const bars = Array.from({ length: 300 }, (_, i) => ({
      time: day(i).toISOString().slice(0, 10), epoch_seconds: day(i).getTime() / 1000, open: 100 + i, high: 101 + i, low: 99 + i, close: 100 + i, volume: 1000,
    }));
    mocks.getHistory.mockImplementation(async (symbol: string, period: string) => ({ symbol, period, bars, time_form: "daily" }));
    mocks.getYieldCurve.mockImplementation(() => new Promise(() => undefined));
    mocks.getCommodities.mockImplementation(() => new Promise(() => undefined));
    mocks.getMarketNews.mockImplementation(() => new Promise(() => undefined));
    const app = APPS.find((a) => a.id === "research")!;
    const section = app.sections.find((s) => s.id === "data-library")!;
    const mountMk = () => wrap(<Markets app={app} section={section} />);
    mountMk();
    // The chart window opens from a wall tile; the wall draws every market at once, with or without its bars.
    fireEvent.click((await screen.findAllByTestId("wall-tile")).find((t) => t.getAttribute("data-name") === "S&P 500")!);
    let dlg = await screen.findByRole("dialog");
    fireEvent.click(await within(dlg).findByRole("button", { name: "1M" }));
    fireEvent.change(within(dlg).getByLabelText("Search a ticker, a company or an ISIN"), { target: { value: "spdr" } });
    cleanup();
    mountMk();
    dlg = await screen.findByRole("dialog", { name: "S&P 500 chart" });
    expect(await within(dlg).findByRole("button", { name: "1M" })).toHaveClass("on");
    expect((within(dlg).getByLabelText("Search a ticker, a company or an ISIN") as HTMLInputElement).value).toBe("spdr");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    cleanup();
    mountMk();
    await screen.findAllByTestId("wall-tile");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("(k) Runs: the range and SHOW ALL come back", async () => {
    vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
    } as DOMRect);
    const base = Date.parse("2026-10-05T09:00:00Z") / 1000;
    const runs = Array.from({ length: 14 }, (_, i) => ({
      id: i + 1, job_id: i + 1, name: null, strategy_id: 11, strategy_version: 1, dataset_id: 1,
      created_at: base + i * 3600, started_at: base + i * 3600 + 60, finished_at: base + i * 3600 + 700,
      status: "succeeded", error: null, total_return: 0.1, sharpe_ratio: 1 + i / 10, max_drawdown: 0.1,
      num_trades: 10, equity_sparkline: [100, 101, 103, 110], kind: "standard",
    }));
    mocks.listRuns.mockResolvedValue(runs);
    mocks.runsSummary.mockResolvedValue({ in_flight_count: 0, in_flight: null });
    const app = APPS.find((a) => a.id === "research")!;
    const section = app.sections.find((s) => s.id === "runs")!;
    const mountRuns = () => wrap(<Runs app={app} section={section} />);
    mountRuns();
    fireEvent.click(await screen.findByRole("button", { name: "This month" }));
    await screen.findByTestId("run-card");
    fireEvent.click(screen.getByRole("button", { name: /^Show all/ }));
    cleanup();
    mountRuns();
    await screen.findByTestId("run-card");
    expect(screen.getByRole("button", { name: "This month" })).toHaveClass("on");
    expect(screen.queryByRole("button", { name: /^Show all/ })).toBeNull();
  });

  it("(k) Paper: the picked strategy comes back", async () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
    } as DOMRect);
    const series = (n: number, s: number) => Array.from({ length: n }, (_, i) => [`2026-10-${String(i + 1).padStart(2, "0")}T20:00:00Z`, s + i * 10]);
    const run = (id: number, name: string) => ({
      paper_run_id: id, strategy_id: id, strategy_name: name, strategy_version: 1, source_run_id: 1,
      slice_opening_cash: 10000, expected_bounds: null, started_at: "2026-10-01T00:00:00Z", ended_at: null,
      end_reason: null, kill_started_at: null, kill_step: null,
      slice: { cash: 5000, position_value: 5500, equity: 10500, realised_pnl: 120, committed_capital: 5500, updated_at: "x" },
      equity_series: series(5, 10000), realised_result_counts: { win_count: 3, loss_count: 2 },
    });
    const SUM = { total_equity: 21000, total_opening_cash: 20000, delta_fraction: 0.05, running_count: 2,
      promotions_requested_count: 0, trades_in_window: 9, fees_paid_total: 0, window: "since_start" };
    mocks.getRunningRuns.mockResolvedValue([run(1, "Alpha gap"), run(2, "Beta drift")]);
    mocks.getSummary.mockResolvedValue(SUM);
    mocks.getRunFills.mockResolvedValue({ fills: [], trade_stats: { win_count: 0, loss_count: 0 } });
    const app = APPS.find((a) => a.id === "trading")!;
    const section = app.sections.find((s) => s.id === "paper")!;
    const mountPaper = () => wrap(<Paper app={app} section={section} />);
    mountPaper();
    await waitFor(() => expect(document.querySelectorAll("button.tp-card")).toHaveLength(2));
    const beta = () => Array.from(document.querySelectorAll("button.tp-card")).find((c) => c.querySelector(".tp-name")?.textContent === "Beta drift")!;
    fireEvent.click(beta());
    expect(screen.getByTestId("main-title").textContent).toContain("Beta drift");
    cleanup();
    mountPaper();
    await waitFor(() => expect(document.querySelectorAll("button.tp-card")).toHaveLength(2));
    expect(screen.getByTestId("main-title").textContent).toContain("Beta drift");
  });
});
