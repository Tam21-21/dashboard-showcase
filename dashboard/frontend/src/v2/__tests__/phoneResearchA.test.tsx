import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import Runs from "../pages/research/Runs";
import Strategies from "../pages/research/Strategies";
import Judge from "../pages/research/Judge";
import { APPS } from "../shell/sections";
import { STORAGE_KEY } from "../settings";

// Phase 3a of the phone frame (DECISIONS #484): the research result pages on a phone. Layout is
// forced through the stored setting, the way phone.test.tsx does it.
const listRuns = vi.fn();
const runsSummary = vi.fn();
const fetchOverview = vi.fn();
const fetchVerdicts = vi.fn();
const approve = vi.fn();
const sendToJudge = vi.fn();

vi.mock("../data/runsApi", () => ({
  RUNS_LIMIT: 500,
  runsKeys: { list: ["runs", "list"], summary: ["runs", "summary"], strategies: ["strategies", "list"] },
  fetchRuns: (...a: unknown[]) => listRuns(...a),
  fetchRunsSummary: (...a: unknown[]) => runsSummary(...a),
  sendToJudge: (...a: unknown[]) => sendToJudge(...a),
}));
vi.mock("../data/strategiesApi", () => ({
  strategiesKeys: {
    overview: ["strategies", "overview"],
    parameters: (id: number) => ["strategies", "parameters", id],
    runs: (id: number) => ["strategies", "runs", id],
    versions: (id: number) => ["strategies", "versions", id],
    settings: (id: number) => ["strategies", "settings", id],
    source: (id: number) => ["strategies", "source", id],
    supply: ["strategies", "reserve-supply"],
    retest: (runId: number) => ["strategies", "retest", runId],
    fills: (id: number) => ["strategies", "retest-fills", id],
  },
  RUNS_AT_MOST: 500,
  fetchStrategiesOverview: (...a: unknown[]) => fetchOverview(...a),
  fetchStrategyParameters: (id: number) => Promise.resolve({ strategy_id: id, kind: "standard", run_id: null, parameters: [] }),
  fetchStrategyRuns: () => Promise.resolve([]),
  fetchStrategyVersions: () => Promise.resolve([]),
  fetchStrategySettings: () => Promise.resolve([]),
  fetchStrategyVersion: vi.fn(),
  setStrategySetting: vi.fn(),
  clearStrategySetting: vi.fn(),
  fetchStrategySource: () => Promise.resolve({ source_code: "", sizing_method: "vol_target" }),
  createStrategy: vi.fn(),
  renameStrategy: vi.fn(),
  deleteStrategy: vi.fn(),
  fetchReserveSupply: () => Promise.resolve({ enumerated: 0, spent: 0, windows: [] }),
  takeReserveExam: vi.fn(),
  fetchRetest: () => Promise.resolve({ state: "never-retested", retests: [] }),
  startRetest: vi.fn(),
  fetchRetestFills: () => Promise.resolve({ fills: [] }),
  replayResult: vi.fn(),
}));
vi.mock("../data/strategyNames", async () => {
  const real = await vi.importActual<typeof import("../data/strategyNames")>("../data/strategyNames");
  const rows = [1, 2, 11].map((id) => ({ id, name: `s${id}`, display_name: `S${id}`, status: "active" }));
  return { ...real, useStrategyNames: () => ({ data: rows, isError: false, error: null, isPending: false }) };
});
vi.mock("../data/judgeApi", () => ({
  fetchJudgeVerdicts: (...a: unknown[]) => fetchVerdicts(...a),
  approveVerdictForPaper: (...a: unknown[]) => approve(...a),
  rejectVerdict: vi.fn(),
  fetchJudgeThresholds: () => Promise.resolve({ battery_version: "b", checks: {} }),
}));
vi.mock("../data/paperApi", () => ({ getRunningRuns: () => Promise.resolve([]) }));

const app = APPS.find((a) => a.id === "research")!;
const sectionOf = (id: string) => app.sections.find((s) => s.id === id)!;

const NOW = Date.parse("2026-10-12T12:00:00Z") / 1000;
const RUN = {
  id: 1, job_id: 1, name: null, strategy_id: 11, strategy_version: 1, dataset_id: 1,
  created_at: NOW - 86400, started_at: NOW - 86000, finished_at: NOW - 85000,
  status: "succeeded", error: null, total_return: 0.1, sharpe_ratio: 1.85, max_drawdown: 0.12,
  num_trades: 60, equity_sparkline: [100, 101, 103, 110], kind: "standard",
};
const MEASURES = Array.from({ length: 9 }, (_, i) => ({
  check: i + 1, outcome: "cleared", why: null,
  measures: [{ key: "k", label: "the figure", value: 0.9, bar: 0.5, comparison: "at_or_above", units: "ratio" }],
}));
const VERDICT = {
  id: 10, run_id: 1, judging_pass_id: "p", variation_id: 1, ticker: "AAA", graded_formula_version: "v", strategy_id: 1,
  strategy_version: 1, dataset_sha256: "x", verdict: "pass", is_advisory: false, battery_version: "b", evidence_floor_outcome: "x",
  check_outcomes: Array.from({ length: 9 }, (_, i) => ({ check: i + 1, outcome: "cleared" })), decided_by: "judge",
  warnings: [], graded_at: 1010, current: true, strategy_name: "Strategy 1", trials: 1, total_return: null,
  max_drawdown: null, num_trades: null, sharpe_ratio: null, rejected: false, rejected_at: null,
  measures: MEASURES, judged_capital: 25000,
};
const OVERVIEW = [
  {
    id: 1, name: "strategy_runs_ma_cross", display_name: "Moving average cross", version: 1, description: "Buys on a cross",
    run: { id: 101, created_at: "2026-10-01T00:00:00Z", strategy_version: 1, equity_sparkline: [1, 2, 3, 2, 4], sharpe_ratio: 1.2, num_trades: 40, total_return: 0.1, max_drawdown: -0.1 },
  },
];

function mount(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}
const setLayout = (layout: "phone" | "desktop") => localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout }));
const btn = (name: string) => screen.queryByRole("button", { name });

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  listRuns.mockResolvedValue([RUN]);
  runsSummary.mockResolvedValue({ total: 1, in_flight_count: 0, in_flight: null });
  fetchOverview.mockResolvedValue(OVERVIEW);
  fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 1 }, rows: [VERDICT] });
  approve.mockResolvedValue({ paper_run_id: 1, verdict_id: 10 });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [listRuns, runsSummary, fetchOverview, fetchVerdicts, approve, sendToJudge]) m.mockReset();
});

const PHONE_HIDDEN = [
  "New strategy", "RENAME", "EDIT CODE", "DELETE", "REPLAY", "REJECT", "RETEST",
  "APPROVE FOR PAPER", "TAKE THE NEXT EXAM", "RUN A SWEEP", "SEND TO THE JUDGE",
];

async function openRuns() {
  mount(<Runs app={app} section={sectionOf("runs")} />);
  await screen.findByTestId("run-card");
}
async function openStrategies() {
  mount(<Strategies app={app} section={sectionOf("strategies")} />);
  fireEvent.click(await screen.findByTestId("road").then((t) => t.querySelector("tr[data-strategy]") as HTMLElement));
  await screen.findByTestId("strategy-detail");
  await screen.findByTestId("road-to-paper");
  fireEvent.click(screen.getByRole("tab", { name: /RULES/ }));
  await screen.findByRole("button", { name: "VIEW CODE" });
}

describe("phone, research results", () => {
  it("r1 Runs on phone shows the run's figures and no SEND TO THE JUDGE", async () => {
    setLayout("phone");
    await openRuns();
    const t = screen.getByTestId("run-card").textContent!;
    for (const w of ["Return", "Sharpe", "Deepest drop", "Trades", "1.85", "60"]) expect(t).toContain(w);
    expect(btn("Send to the judge")).toBeNull();
  });

  it("r2 Strategies on phone shows the detail and VIEW CODE, none of the write buttons", async () => {
    setLayout("phone");
    await openStrategies();
    expect(btn("FILLS")).not.toBeNull();
    expect(btn("OPEN RUNS")).not.toBeNull();
    for (const n of PHONE_HIDDEN) expect(btn(n), n).toBeNull();
  });

  it("r3 Judge on phone keeps APPROVE FOR PAPER and it posts the approve call", async () => {
    setLayout("phone");
    mount(<Judge app={app} section={sectionOf("judge")} />);
    await screen.findByTestId("sheet");
    fireEvent.click(screen.getByRole("button", { name: "Approve for paper" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith(10, 25000));
  });

  it("r4 guard: on desktop every button of r1 and r2 is present", async () => {
    setLayout("desktop");
    await openRuns();
    expect(btn("Send to the judge")).not.toBeNull();
    cleanup();
    await openStrategies();
    for (const n of ["New strategy", "RENAME", "EDIT CODE", "DELETE", "REPLAY", "REJECT", "RETEST", "FILLS", "APPROVE FOR PAPER"]) {
      expect(btn(n), n).not.toBeNull();
    }
  });
});
