import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ForwardTesting from "../pages/research/ForwardTesting";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const verdicts = vi.fn();
const summary = vi.fn();
const runs = vi.fn();
const history = vi.fn();
const retired = vi.fn();
const limits = vi.fn();
const paperSum = vi.fn();
const systems = vi.fn();
const names = vi.fn();

vi.mock("../data/judgeApi", async (orig) => {
  const real = await orig<typeof import("../data/judgeApi")>();
  return { ...real, fetchJudgeVerdicts: () => verdicts(), fetchJudgeSummary: () => summary() };
});
vi.mock("../data/paperApi", async (orig) => {
  const real = await orig<typeof import("../data/paperApi")>();
  return { ...real, getRunningRuns: () => runs(), getHistory: () => history(), getRetired: () => retired(), getPaperLimits: () => limits(), getSummary: (w: string) => paperSum(w) };
});
vi.mock("../data/opsApi", async (orig) => {
  const real = await orig<typeof import("../data/opsApi")>();
  return { ...real, getSystems: () => systems() };
});
vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => names() }) };
});

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "forward-testing")!;

const ALL = Array(9).fill("cleared");
function verdict(id: number, v: string, outcomes: string[], extra: Record<string, unknown> = {}) {
  return {
    id, run_id: id, strategy_id: id, verdict: v, current: true, rejected: false, graded_at: 1000 + id,
    check_outcomes: outcomes.map((o, i) => ({ check: i + 1, outcome: o })), measures: [], ...extra,
  };
}
const LIMIT = (key: string, value: number | null) => ({ key, value, unit: "dollars", meaning: `the most for ${key}` });
const SIX = ["a", "b", "c", "d", "e", "f"].map((k) => LIMIT(`paper.${k}`, null));
const SUM = (w: string, extra: Record<string, unknown> = {}) => ({
  total_equity: 0, total_opening_cash: 0, delta_fraction: null, running_count: 0, promotions_requested_count: 0, trades_in_window: 0, fees_paid_total: 0, window: w, ...extra,
});
const BROKER = { name: "Paper broker", where: "Alpaca", word: "NOT CONNECTED", tone: "off", reason: "The Alpaca paper broker is not connected yet", last_proven_at: null, marks: [] };

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/app/research/forward-testing"]}>
        <Routes><Route path="*" element={<><ForwardTesting app={app} section={section} /><Where /></>} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const road = () => screen.findByTestId("road");
const stop = (r: HTMLElement, k: string) => r.querySelector(`[data-stop="${k}"]`) as HTMLElement;

beforeEach(() => {
  localStorage.clear();
  verdicts.mockResolvedValue({ store: { table_present: true, row_count: 2 }, rows: [
    verdict(1, "unjudgeable", ALL.map((o, i) => (i ? "not-evaluable" : o))),
    verdict(2, "fail", ALL.map((o, i) => (i === 7 ? "failed" : o))),
    verdict(3, "pass", ALL, { current: false }),
  ] });
  summary.mockResolvedValue({ store: { table_present: true, row_count: 2 }, graded: 2, survivors: 0, awaiting_ok: 0, in_paper: 0, unjudgeable: 1 });
  runs.mockResolvedValue([]);
  history.mockResolvedValue([]);
  retired.mockResolvedValue([]);
  limits.mockResolvedValue({ limits: SIX });
  paperSum.mockImplementation((w: string) => Promise.resolve(SUM(w)));
  systems.mockResolvedValue({ systems: [BROKER] });
  names.mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Forward testing page", () => {
  it("f1 nothing on paper: says so, and what stands in the way, from the live figures", async () => {
    mount();
    await road();
    const say = document.querySelector(".nl-say")!.textContent!;
    expect(say).toContain("Nothing is on paper yet.");
    expect(say).toContain("0 of 2 judged runs passed");
    expect(say).toContain("6 of 6 safety limits have no value");
    expect(await screen.findByText(/the broker is not connected/)).toBeTruthy();
  });

  it("f2 the steps: judge red, approval grey, limits amber, broker from the systems read, paper grey; lit up to the first stop that is not green", async () => {
    mount();
    const r = await road();
    expect(stop(r, "judge").className).toContain("t-bad");
    expect(stop(r, "ok").className).toContain("t-dim");
    expect(stop(r, "limits").className).toContain("t-warn");
    expect(stop(r, "limits").querySelectorAll(".ov7-share i.warn")).toHaveLength(1);
    expect(within(stop(r, "limits")).getByText("Waits for you")).toBeTruthy();
    await within(r).findByText("Not connected");
    expect(stop(r, "broker").getAttribute("title")).toContain("The Alpaca paper broker is not connected yet");
    expect(stop(r, "paper").className).toContain("t-dim");
  });

  it("f3 a stop opens its page; the approval stop with nothing to approve does nothing", async () => {
    mount();
    const r = await road();
    fireEvent.click(stop(r, "ok"));
    expect(screen.getByTestId("where").textContent).toBe("/app/research/forward-testing");
    fireEvent.click(stop(r, "limits"));
    expect(screen.getByTestId("where").textContent).toBe("/app/trading/paper");
  });

  it("f4 the account reads the window picked, and with nothing on paper it says so", async () => {
    paperSum.mockImplementation((w: string) => Promise.resolve(SUM(w, w === "today" ? { trades_in_window: 3, fees_paid_total: 4.4 } : {})));
    mount();
    await road();
    const figs = await screen.findByTestId("figs");
    expect(figs.textContent).toContain("Trades0");
    expect(figs.textContent).toContain("No strategy is on paper");
    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    await waitFor(() => expect(screen.getByTestId("figs").textContent).toContain("Trades3"));
    expect(screen.getByTestId("figs").textContent).toContain("Fees paid$4");
    expect(paperSum).toHaveBeenCalledWith("today");
  });

  it("f5 the limits in a table; a set one shows its value, an unset one says Not set", async () => {
    limits.mockResolvedValue({ limits: [LIMIT("paper.a", 2500), ...SIX.slice(1)] });
    mount();
    const l = await screen.findByTestId("limits");
    const tiles = Array.from(l.querySelectorAll(".lim"));
    expect(tiles[0].textContent).toContain("Set");
    expect(tiles[0].textContent).toContain("2,500");
    expect(tiles[1].textContent).toContain("Not set");
    expect(tiles[1].textContent).toContain("Waits for you");
    expect(tiles[1].getAttribute("title")).toContain("Until it has a value, no paper order is sent");
    expect(tiles[0].textContent).toContain("The most for paper.a");
  });

  it("f6 with a strategy on paper: says how many, the paper stop is green and its line is drawn against its opening cash", async () => {
    runs.mockResolvedValue([{ paper_run_id: 7, strategy_id: 2, strategy_name: "s_2", slice_opening_cash: 10000, started_at: "2026-11-01T00:00:00Z",
      equity_series: [["2026-11-01T00:00:00Z", 10000], ["2026-11-02T00:00:00Z", 10100], ["2026-11-03T00:00:00Z", 10250]] }]);
    paperSum.mockImplementation((w: string) => Promise.resolve(SUM(w, { running_count: 1, total_equity: 10250, total_opening_cash: 10000, delta_fraction: 0.025 })));
    names.mockResolvedValue([{ id: 2, name: "s_2", display_name: "Strategy two", status: "active" }]);
    mount();
    const r = await road();
    expect(document.querySelector(".nl-say")!.textContent).toContain("1 strategy is on paper.");
    expect(stop(r, "paper").className).toContain("t-good");
    const tiles = await screen.findByTestId("tiles");
    await waitFor(() => expect(tiles.textContent).toContain("Strategy two"));
    expect(tiles.querySelector("svg.fw7-line.up")).not.toBeNull();
    expect(screen.getByTestId("figs").textContent).toContain("+2.50%");
  });

  it("f8 a systems read with no paper broker row says the broker is not watched, not that it is still reading", async () => {
    systems.mockResolvedValue({ systems: [] });
    mount();
    const r = await road();
    await within(r).findByText("Not watched");
    expect(stop(r, "broker").getAttribute("title")).toContain("The systems read lists no paper broker");
  });

  it("f9 the mockup's sections are all there, and Example shows made-up strategies that say so", async () => {
    mount();
    await road();
    for (const h of ["Running strategies", "Steps to paper", "Running strategies, total", "Past strategies", "Safety limits"]) expect(screen.getByRole("heading", { name: h })).toBeTruthy();
    expect(screen.getByText("No strategy has left paper yet. One that stops or moves on to real money is listed here with its result.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Example" }));
    const tiles = await screen.findByTestId("tiles");
    expect(tiles.querySelectorAll(".fw7-tile")).toHaveLength(3);
    expect(screen.getAllByText("Example data, made up").length).toBeGreaterThan(0);
  });

  it("f7 a route that fails shows the server's words", async () => {
    limits.mockRejectedValue(new ApiError("500 /api/paper/limits", 500, { detail: "the paper store is locked" }));
    mount();
    expect(await screen.findByText(/the paper store is locked/i)).toBeTruthy();
  });
});
