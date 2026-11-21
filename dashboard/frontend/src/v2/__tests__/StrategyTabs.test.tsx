import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Strategies from "../pages/research/Strategies";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const fetchOverview = vi.fn();
const fetchVerdicts = vi.fn();
const fetchParams = vi.fn();
const fetchVersions = vi.fn();
const fetchSettings = vi.fn();
const fetchSource = vi.fn();
const fetchRuns = vi.fn();

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
  fetchStrategyParameters: (...a: unknown[]) => fetchParams(...a),
  fetchStrategyVersions: (...a: unknown[]) => fetchVersions(...a),
  fetchStrategySettings: (...a: unknown[]) => fetchSettings(...a),
  fetchStrategyRuns: (...a: unknown[]) => fetchRuns(...a),
  fetchStrategySource: (...a: unknown[]) => fetchSource(...a),
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
  const rows = [1, 2].map((id) => ({ id, name: `s${id}`, display_name: `S${id}`, status: "active" }));
  return { ...real, useStrategyNames: () => ({ data: rows, isError: false, error: null }) };
});
vi.mock("../data/judgeApi", () => ({
  fetchJudgeVerdicts: (...a: unknown[]) => fetchVerdicts(...a),
  approveVerdictForPaper: vi.fn(),
  rejectVerdict: vi.fn(),
}));
vi.mock("../data/paperApi", () => ({ getRunningRuns: () => Promise.resolve([]) }));
vi.mock("../data/runsApi", () => ({
  runsKeys: { list: ["runs", "list"], summary: ["runs", "summary"], strategies: ["strategies", "list"] },
  sendToJudge: vi.fn(),
}));

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "strategies")!;
const EM_DASH = String.fromCharCode(8212);

const STOCKS_WHY = "The server does not record which stocks a strategy trades yet";
const WINDOWS_WHY = "The server does not record a strategy's windows yet";
const MONEY_WHY = "The server does not report how money is split yet";
const THEORY_RULES_WHY = "The server does not send a theory's rules in words yet";

const SPARK = [1, 2, 3, 2, 4];
const OVERVIEW = [
  {
    id: 1, name: "strategy_runs_ma_cross", display_name: "Moving average cross", version: 2,
    description: "Buys when two averages cross",
    run: { id: 101, created_at: "2026-10-01T00:00:00Z", strategy_version: 2, equity_sparkline: SPARK, sharpe_ratio: 1.2, num_trades: 40, total_return: 0.1, max_drawdown: -0.1 },
  },
  {
    id: 2, name: "theory__gap_and_go__v0", display_name: "Theory gap and go", version: 1, description: null,
    run: { id: 102, created_at: "2026-10-01T00:00:00Z", strategy_version: 1, equity_sparkline: SPARK, sharpe_ratio: 0.4, num_trades: 12, total_return: 0.02, max_drawdown: -0.1 },
  },
];
const VERDICT = {
  id: 1, run_id: 101, strategy_id: 1, verdict: "pass", is_advisory: false, graded_at: 1761868800, current: true, rejected: false,
  check_outcomes: Array.from({ length: 9 }, (_, i) => ({ check: i + 1, outcome: "cleared" })),
  strategy_name: "x", measures: [], judged_capital: 25000,
};
const VERSIONS = [
  { version: 2, description: "Added the volume filter", edited_at: 1761955200 },
  { version: 1, description: "First cut", edited_at: 1760054400 },
];
function setting(scope: string, key: string, value: number, label: string) {
  return { scope, key, value, value_type: "number", unit: "", label, source: "inherited", global_value: value, overridable: true, source_note: null };
}
const SETTINGS = [
  setting("fees", "alpaca.sec_fee_rate", 0.0000206, "Alpaca SEC fee"),
  setting("fees", "alpaca.taf_per_share", 0, "Alpaca FINRA TAF"),
  setting("fees", "alpaca.cat_per_share", 0.000003, "Alpaca CAT fee"),
  setting("slippage", "equities.default", 0.0003, "Equity slippage"),
  setting("slippage", "crypto.default", 0.0005, "Crypto slippage"),
];
const SOURCE = "class Strategy(BaseStrategy):\n    sizing_method = \"vol_target\"\n";

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/app/research/strategies"]}>
        <Routes>
          <Route path="/app/research/strategies" element={<Strategies app={app} section={section} />} />
          <Route path="/app/research/runs" element={<div>RUNS PAGE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  fetchOverview.mockResolvedValue(OVERVIEW);
  fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 1 }, rows: [VERDICT] });
  fetchParams.mockImplementation((id: number) =>
    Promise.resolve({
      strategy_id: id, kind: id === 2 ? "theory" : "standard", run_id: null,
      parameters: id === 2 ? [] : ["fast", "slow", "band"].map((name) => ({ name, range: null, value: 5, source: null })),
    }));
  fetchVersions.mockResolvedValue(VERSIONS);
  fetchSettings.mockResolvedValue(SETTINGS);
  fetchSource.mockResolvedValue({ source_code: SOURCE, sizing_method: "vol_target" });
  fetchRuns.mockResolvedValue(Array.from({ length: 14 }, (_, i) => ({
    id: i, created_at: 1761000000, status: "succeeded", sharpe_ratio: 1, num_trades: 3, total_return: 0.1, equity_sparkline: null,
  })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [fetchOverview, fetchVerdicts, fetchParams, fetchVersions, fetchSettings, fetchSource, fetchRuns]) m.mockReset();
});

const detail = () => screen.getByTestId("strategy-detail");
const tabNames = () => Array.from(detail().querySelectorAll(".stb > span")).map((t) => t.querySelector("b")!.textContent);
async function open(id = 1) {
  mount();
  await waitFor(() => expect(document.querySelector(`[data-strategy="${id}"]`)).not.toBeNull());
  fireEvent.click(document.querySelector(`[data-strategy="${id}"]`)!);
}
async function goTab(label: string) {
  fireEvent.click(within(detail()).getByText(label).closest("span")!);
}
const tileOf = (label: string) => within(detail()).getByText(label).closest(".gct") as HTMLElement;

describe("Strategy detail tabs", () => {
  it("t1 the five tabs in order with their subs", async () => {
    await open();
    await waitFor(() => expect(tabNames()).toEqual(["OVERVIEW", "RULES", "PARAMETERS", "WHERE IT TRADES", "HISTORY"]));
    await waitFor(() => {
      const subs = Array.from(detail().querySelectorAll(".stb > span i")).map((i) => i.textContent);
      expect(subs).toEqual(["Sharpe 1.20", "by hand", "3 values", "-", "2 versions"]);
    });
    const whereSub = detail().querySelectorAll(".stb > span i")[3];
    expect(whereSub.getAttribute("title")).toBe(STOCKS_WHY);
  });

  it("t2 RULES: the description, the pill, VIEW CODE once, and the theory's dashes", async () => {
    await open();
    await goTab("RULES");
    await waitFor(() => expect(detail().querySelector(".grule")).not.toBeNull());
    const box = detail().querySelector(".grule") as HTMLElement;
    expect(box.textContent).toBe("Buys when two averages cross");
    expect(within(detail()).getByText("WRITTEN BY HAND")).toBeTruthy();
    fireEvent.click(within(detail()).getByRole("button", { name: "VIEW CODE" }));
    const dlg = await screen.findByRole("dialog", { name: "Code" });
    await waitFor(() => expect(dlg.querySelector("pre")!.textContent).toBe(SOURCE));
    expect(fetchSource).toHaveBeenCalledTimes(1);
    fireEvent.click(within(dlg).getByRole("button", { name: "CLOSE" }));
    expect(screen.queryByRole("dialog", { name: "Code" })).toBeNull();
    cleanup();

    fetchOverview.mockResolvedValue([{ ...OVERVIEW[0], description: null }, OVERVIEW[1]]);
    await open(1);
    await goTab("RULES");
    await waitFor(() => expect(detail().querySelector(".grule")).not.toBeNull());
    const nobox = detail().querySelector(".grule") as HTMLElement;
    expect(nobox.textContent).toBe("-");
    expect(nobox.querySelector("[title]")!.getAttribute("title")).toBe("No description in the code");
    cleanup();

    await open(2);
    await goTab("RULES");
    const pill = await within(detail()).findByText("THEORY");
    expect(pill.getAttribute("title")).toBe("This strategy is a generator theory; its rules are its theory file");
    const boxes = Array.from(detail().querySelectorAll(".grule")) as HTMLElement[];
    expect(boxes.map((b) => b.querySelector(".kl")!.textContent)).toEqual(["ENTRY", "EXIT"]);
    for (const b of boxes) {
      const d = within(b).getByText("-");
      expect(d.getAttribute("title")).toBe(THEORY_RULES_WHY);
    }
  });

  it("t3 WHERE IT TRADES: dashes, fees, slippage, size, money, a refused read", async () => {
    await open();
    await goTab("WHERE IT TRADES");
    await waitFor(() => expect(tileOf("FEES").textContent).toContain("Alpaca"));
    expect(within(detail()).getByText("STOCKS").closest(".sc-block")!.querySelector("[title]")!.getAttribute("title")).toBe(STOCKS_WHY);
    expect(within(detail()).getByText("WINDOWS").closest(".sc-block")!.querySelector("[title]")!.getAttribute("title")).toBe(WINDOWS_WHY);
    expect(tileOf("FEES").getAttribute("title")).toBe(
      ["Alpaca SEC fee: 0.0000206", "Alpaca FINRA TAF: 0", "Alpaca CAT fee: 0.000003"].join("\n"),
    );
    expect(tileOf("SLIPPAGE").querySelector("b")!.textContent).toBe("0.03%");
    expect(tileOf("SLIPPAGE").getAttribute("title")).toBe("Crypto: 0.05%");
    await waitFor(() => expect(tileOf("SIZE").querySelector("b")!.textContent).toBe("Vol target"));
    expect(tileOf("SIZE").getAttribute("title")).toBe("Stored as vol_target");
    const money = tileOf("MONEY").querySelector("b")!;
    expect(money.textContent).toBe("-");
    expect(money.querySelector("[title]")!.getAttribute("title")).toBe(MONEY_WHY);
    cleanup();

    fetchSettings.mockRejectedValue(new ApiError("404 /x", 404, { detail: "strategy 1 not found" }));
    await open();
    await goTab("WHERE IT TRADES");
    await screen.findByText("strategy 1 not found");
  });

  it("t4 HISTORY: the timeline with versions, runs and judge", async () => {
    await open();
    await goTab("HISTORY");
    await waitFor(() => expect(detail().querySelectorAll(".gte").length).toBe(5));
    const rows = Array.from(detail().querySelectorAll(".gte")).map((e) => Array.from(e.children).filter((c) => c.tagName !== "I").map((c) => c.textContent));
    expect(rows[0]).toEqual(["THEORY", "-"]);
    expect(rows[1]).toEqual(["VERSION 1", "", "10/10/2025"]);
    expect(detail().querySelectorAll(".gte")[2].getAttribute("title")).toBe("Added the volume filter");
    expect(rows[2]).toEqual(["VERSION 2", "", "01/11/2025"]);
    await waitFor(() => expect(detail().querySelectorAll(".gte")[3].textContent).toContain("14 runs"));
    const judge = detail().querySelectorAll(".gte")[4];
    expect(judge.textContent).toContain("PASSED");
    expect(judge.textContent).toContain("31/10/2025");
    expect(detail().querySelectorAll(".gte")[0].querySelector("[title]")!.getAttribute("title")).toBe("Written by hand, not from a theory");
    fireEvent.click(detail().querySelectorAll(".gte")[3]);
    await screen.findByText("RUNS PAGE");
  });

  it("t4b HISTORY: no verdict, 500 runs, a theory's name, a refused read", async () => {
    fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 0 }, rows: [] });
    fetchRuns.mockResolvedValue(Array.from({ length: 500 }, (_, i) => ({ id: i, created_at: 1, status: "succeeded", sharpe_ratio: 1, num_trades: 1, total_return: 0, equity_sparkline: null })));
    await open(2);
    await goTab("HISTORY");
    await waitFor(() => expect(detail().querySelectorAll(".gte").length).toBe(5));
    const tl = Array.from(detail().querySelectorAll(".gte")) as HTMLElement[];
    expect(tl[0].textContent).toContain("Theory gap and go");
    await waitFor(() => expect(tl[3].textContent).toContain("500+ runs"));
    const dash = within(tl[4]).getByText("-");
    expect(dash.getAttribute("title")).toBe("Not sent to the judge yet");
    cleanup();

    fetchVersions.mockRejectedValue(new ApiError("404 /x", 404, { detail: "strategy not found" }));
    await open();
    await goTab("HISTORY");
    await screen.findByText("strategy not found");
  });

  it("t5 no snake_case and no em dash on any tab", async () => {
    await open();
    for (const label of ["RULES", "PARAMETERS", "WHERE IT TRADES", "HISTORY"]) {
      await goTab(label);
      await waitFor(() => expect(detail().querySelector(".stb span.on b")!.textContent).toBe(label));
      if (label === "WHERE IT TRADES") await waitFor(() => expect(tileOf("SIZE").querySelector("b")!.textContent).toBe("Vol target"));
      if (label === "HISTORY") await waitFor(() => expect(detail().querySelectorAll(".gte").length).toBe(5));
      const text = detail().textContent ?? "";
      expect(text).not.toMatch(/[a-z]+_[a-z]+/);
      expect(text).not.toContain(EM_DASH);
      for (const el of Array.from(detail().querySelectorAll("[title]"))) {
        expect(el.getAttribute("title")!).not.toContain(EM_DASH);
      }
    }
  });
});
