import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Strategies, { axisDecimals } from "../pages/research/Strategies";
import { whereOf } from "../pages/research/strategies/Road";
import { strategyHue } from "../data/strategyNames";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const fetchOverview = vi.fn();
const fetchVerdicts = vi.fn();
const runningRuns = vi.fn();
const sendToJudge = vi.fn();
const fetchParams = vi.fn();
const fetchBatches = vi.fn();
const fetchTree = vi.fn();

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
  const rows = [1, 2, 3, 4, 5, 6].map((id) => ({ id, name: `s${id}`, display_name: `S${id}`, status: "active" }));
  return { ...real, useStrategyNames: () => ({ data: rows, isError: false, error: null }) };
});
vi.mock("../data/judgeApi", () => ({
  fetchJudgeVerdicts: (...a: unknown[]) => fetchVerdicts(...a),
  approveVerdictForPaper: vi.fn(),
  rejectVerdict: vi.fn(),
}));
vi.mock("../data/paperApi", () => ({
  getRunningRuns: (...a: unknown[]) => runningRuns(...a),
}));
vi.mock("../data/generatorApi", () => ({
  generatorKeys: { batches: ["generator", "batches"], tree: (id: number) => ["generator", "tree", id] },
  fetchBatches: (...a: unknown[]) => fetchBatches(...a),
  fetchTree: (...a: unknown[]) => fetchTree(...a),
}));
vi.mock("../data/runsApi", () => ({
  runsKeys: { list: ["runs", "list"], summary: ["runs", "summary"], strategies: ["strategies", "list"] },
  sendToJudge: (...a: unknown[]) => sendToJudge(...a),
}));

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "strategies")!;
const EM_DASH = String.fromCharCode(8212);

const SPARK = [1, 2, 3, 2, 4];
function run(id: number, sharpe: number | null, extra: Record<string, unknown> = {}) {
  return {
    id, created_at: "2026-10-01T00:00:00Z", strategy_version: 1, equity_sparkline: SPARK, sharpe_ratio: sharpe,
    num_trades: 40, total_return: 0.1, max_drawdown: -0.1, ...extra,
  };
}
const OVERVIEW = [
  { id: 1, name: "strategy_runs_ma_cross", display_name: "Moving average cross", version: 1, description: "Buys when two averages cross", run: run(101, 1.2) },
  { id: 2, name: "theory__capitulation_rebound", display_name: "Capitulation rebound", version: 1, description: null, run: run(102, -0.3) },
  { id: 3, name: "gap_fill", display_name: "Opening gap fill", version: 1, description: null, run: null },
  { id: 4, name: "trend_follow", display_name: "Trend follow", version: 1, description: null, run: run(104, 0.5) },
  { id: 5, name: "theory__gap_and_go__v0", display_name: "Gap and go", version: 1, description: null, run: run(105, 0.1, { equity_sparkline: null }) },
];
const UNNAMED = { id: 6, name: "zz_unnamed", display_name: null, version: 1, description: null, run: run(106, 0.2) };
function verdict(id: number, strategyId: number, v: string, extra: Record<string, unknown> = {}, cleared = 9) {
  return {
    id, run_id: id, strategy_id: strategyId, verdict: v, is_advisory: false, graded_at: 1000 + id, current: true,
    rejected: false,
    check_outcomes: Array.from({ length: 9 }, (_, i) => ({ check: i + 1, outcome: i < cleared ? "cleared" : "failed" })),
    strategy_name: "x", measures: [], judged_capital: 25000, ...extra,
  };
}
const VERDICTS = [
  verdict(1, 1, "pass", { graded_at: 1000 }),
  verdict(2, 1, "fail", { graded_at: 500 }, 4),
  verdict(3, 1, "fail", { graded_at: 2000, is_advisory: true }, 2),
  verdict(4, 2, "unjudgeable", { graded_at: 1000 }, 5),
  verdict(5, 4, "fail", { graded_at: 1000 }, 3),
  verdict(6, 5, "pass", { graded_at: 1500, rejected: true }),
  verdict(7, 5, "fail", { graded_at: 1600, current: false }),
];
const PAPER = [{ paper_run_id: 1, strategy_id: 4, strategy_name: "trend_follow" }];

// The generator's trees: strategy 5 is the theory gap_and_go__v0 (its screen stopped it), strategy 2 the theory capitulation_rebound.
const QUALITY = {
  answer: "no", pool: 8, ids: [],
  closest: { sharpe: 1.25, met: 3, failed: ["win_share", "max_drawdown"], trial_id: 9,
    metrics: { closed: 252, win_share: 0.58, max_drawdown: 0.147, avg_month: 0.0122, recovery: 8.1 } },
};
function theory(id: number, name: string, display: string, extra: Record<string, unknown> = {}) {
  return {
    id, name, display_name: display, choices: {}, final: "not run", saved: [], stop: null,
    runs: [{ stage: "screen", run_id: 77 + id, status: "succeeded" }],
    search: {
      rounds: {
        rounds: [{ round: 1, valid_strategies: 144, ran: "all", strategies: 144, new_trials: 144, best_trial_ids: [],
          box: { N: { from: 10, to: 30, step: 10, values: [10, 20, 30] }, M: { from: 1, to: 2, step: 1, values: [1, 2] } } }],
        quality: QUALITY, stop: "not suitable",
      },
      finalists: [], pick: null,
    },
    ...extra,
  };
}
const TREE = {
  batch: { id: 1 },
  template: { name: "gap", display_name: "Gap plays", sha256: "a" },
  theories: [theory(1, "gap_and_go__v0", "Gap plays, Gap and go")],
};
const TREE2 = {
  batch: { id: 2 },
  template: { name: "rebound", display_name: "Rebound", sha256: "b" },
  theories: [theory(2, "capitulation_rebound", "Rebound, Capitulation rebound")],
};

function RunsStub() {
  const { search } = useLocation();
  return <div>{`RUNS PAGE${search}`}</div>;
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/app/research/strategies"]}>
        <Routes>
          <Route path="/app/research/strategies" element={<Strategies app={app} section={section} />} />
          <Route path="/app/research/runs" element={<RunsStub />} />
          <Route path="/app/research/generator" element={<div>GENERATOR PAGE</div>} />
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
  fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 7 }, rows: VERDICTS });
  runningRuns.mockResolvedValue(PAPER);
  fetchParams.mockImplementation((id: number) => Promise.resolve({ strategy_id: id, kind: "standard", run_id: null, parameters: [] }));
  fetchBatches.mockResolvedValue([{ id: 1 }, { id: 2 }]);
  fetchTree.mockImplementation((id: number) => Promise.resolve(id === 1 ? TREE : TREE2));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [fetchOverview, fetchVerdicts, runningRuns, sendToJudge, fetchParams, fetchBatches, fetchTree]) m.mockReset();
});

const list = () => screen.getByTestId("road");
const detail = () => screen.getByTestId("strategy-detail");
const ids = () => Array.from(list().querySelectorAll("[data-strategy]")).map((e) => e.getAttribute("data-strategy"));
const rowOf = (id: number) => list().querySelector(`[data-strategy="${id}"]`) as HTMLElement;
const stateOf = (id: number) => rowOf(id).querySelector(".st") as HTMLElement;
const stateTitle = (id: number) => stateOf(id).querySelector("[title]")!.getAttribute("title");
async function open() {
  mount();
  await waitFor(() => expect(ids().length).toBeGreaterThan(0));
  await waitFor(() => expect(stateOf(4).textContent).toBe("On paper"));
  await waitFor(() => expect(stateOf(5).textContent).toBe("Stopped"));
}

describe("Strategies page", () => {
  it("(a) renders at research/strategies and the old stub text is gone", async () => {
    await open();
    for (const h of ["Strategies by stage", "Strategy families", "All strategies"]) expect(screen.getByRole("heading", { name: h })).toBeInTheDocument();
    expect(screen.queryByText("This page is not rebuilt yet.")).toBeNull();
    expect(fetchOverview).toHaveBeenCalledWith();
    expect(fetchVerdicts).toHaveBeenCalledWith(500, 0);
  });

  it("(b) where each stands: the judge's newest verdict, paper over a verdict, not run, stopped at the screen", async () => {
    await open();
    expect(stateOf(1).textContent).toBe("Passed the judge");
    expect(stateTitle(1)).toBe("9 of 9 checks cleared");
    expect(stateOf(2).textContent).toBe("The judge could not grade it");
    expect(stateOf(3).textContent).toBe("Not run yet");
    expect(stateTitle(3)).toBe("Written by hand and not backtested yet");
    expect(stateTitle(5)).toBe("Its search stopped after round 1: none of its best 8 attempts met every target");
    const stage = (id: number) => rowOf(id).querySelectorAll("td")[2].textContent;
    expect([1, 2, 3, 4, 5].map(stage)).toEqual(["Judge", "Judge", "Screen", "Paper", "Screen"]);
    expect(stateOf(5).querySelector(".nl-st")!.className).toContain("bad");
  });

  it("(c) display names with the stored name on hover, no Family column, sorted by display name", async () => {
    await open();
    for (const [shown, stored] of [
      ["Moving average cross", "strategy_runs_ma_cross"],
      ["Capitulation rebound", "theory__capitulation_rebound"],
      ["Gap and go", "theory__gap_and_go__v0"],
    ]) {
      expect(within(list()).getByText(shown).getAttribute("title")).toContain(`Stored as ${stored}`);
    }
    expect(within(list()).getByText("Moving average cross").getAttribute("title")).toBe(
      "Buys when two averages cross Stored as strategy_runs_ma_cross",
    );
    expect([5, 2, 1].map((id) => rowOf(id).querySelectorAll("td")[1].textContent)).toEqual(["Gap plays", "Rebound", "Written by hand"]);
    expect(ids()).toEqual(["4", "2", "1", "5", "3"]);
    expect(screen.getByTestId("families").querySelectorAll(".sx-tile")).toHaveLength(3);
  });

  it("(d) the search filters by display name", async () => {
    await open();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "GAP" } });
    expect(ids()).toEqual(["5", "3"]);
  });

  it("(e) the detail card, the buttons and the honest lines", async () => {
    sendToJudge.mockResolvedValue({});
    await open();
    fireEvent.click(rowOf(5));
    expect(detail().textContent).toContain("GAP AND GO");
    fireEvent.click(rowOf(1));
    await waitFor(() => expect(detail().textContent).toContain("MOVING AVERAGE CROSS"));
    expect(detail().textContent).toContain("9 of 9");
    fireEvent.click(screen.getByRole("button", { name: "SEND TO THE JUDGE" }));
    await screen.findByText("Sent to the judge");
    expect(sendToJudge).toHaveBeenCalledWith(101);

    sendToJudge.mockRejectedValue(new ApiError("409 /x", 409, { detail: "The run is still going" }));
    fireEvent.click(screen.getByRole("button", { name: "SEND TO THE JUDGE" }));
    await screen.findByText("The run is still going");

    fireEvent.click(rowOf(3));
    const btn = screen.getByRole("button", { name: "SEND TO THE JUDGE" });
    expect(btn).toBeDisabled();
    expect(btn.getAttribute("title")).toBe("No real run to send");

    fireEvent.click(rowOf(5));
    expect(detail().textContent).toContain("No curve stored for this strategy");
  });

  it("(e2) pressing a row opens its window, and OPEN RUNS goes to the runs page", async () => {
    await open();
    expect(screen.queryByTestId("strategy-detail")).toBeNull();
    fireEvent.click(rowOf(5));
    fireEvent.click(screen.getByRole("button", { name: "OPEN RUNS" }));
    expect(screen.getByRole("dialog", { name: "This strategy's runs" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "OPEN THE RUNS PAGE" }));
    await screen.findByText("RUNS PAGE");
  });

  it("(f) Close shuts the window", async () => {
    await open();
    fireEvent.click(rowOf(4));
    const win = screen.getByRole("dialog");
    fireEvent.click(within(win).getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("strategy-detail")).toBeNull();
  });

  it("(g) a failed source shows its message and the rest still draws", async () => {
    fetchVerdicts.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The judge store is unreadable" }));
    mount();
    await screen.findByText("The judge store is unreadable");
    await waitFor(() => expect(ids().length).toBe(5));
    await waitFor(() => expect(stateTitle(1)).toBe("Judge verdicts could not be read"));
    cleanup();

    fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 7 }, rows: VERDICTS });
    runningRuns.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The paper store is unreadable" }));
    mount();
    await screen.findByText("The paper store is unreadable");
    await waitFor(() => expect(stateTitle(1)).toBe("Paper runs could not be read"));
    cleanup();

    runningRuns.mockResolvedValue([]);
    fetchOverview.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The strategy store is unreadable" }));
    mount();
    await screen.findByText("The strategy store is unreadable");
    cleanup();

    fetchOverview.mockResolvedValue([]);
    mount();
    await screen.findByText(/No strategies yet/);
  });

  it("(h) no snake_case text and no em dash", async () => {
    await open();
    for (const id of [1, 2, 3, 5]) {
      fireEvent.click(rowOf(id));
      const text = Array.from(document.body.querySelectorAll("*"))
        .filter((e) => e.children.length === 0)
        .map((e) => e.textContent)
        .join(" ");
      expect(text).not.toMatch(/[a-z]+_[a-z]+/);
      expect(text).not.toContain(EM_DASH);
    }
  });

  it("n1 names: display names only, unnamed last with a dash and its hover", async () => {
    fetchOverview.mockResolvedValue([...OVERVIEW, UNNAMED]);
    await open();
    expect(ids()).toHaveLength(6);
    const text = list().textContent ?? "";
    for (const stored of ["strategy_runs_ma_cross", "gap_fill", "zz_unnamed", "trend_follow"]) expect(text).not.toContain(stored);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "gap" } });
    expect(ids()).toEqual(["5", "3"]);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    expect(rowOf(6).querySelector("td")!.textContent).toBe("-");
    expect(within(rowOf(6)).getAllByTitle("No display name").length).toBeGreaterThan(0);
    fireEvent.click(rowOf(6));
    await waitFor(() => expect(within(detail()).getAllByTitle("No display name").length).toBeGreaterThan(0));
  });

  it("n2 a description shows only in the title's hover, no line under it", async () => {
    await open();
    fireEvent.click(rowOf(1));
    await waitFor(() => expect(detail().textContent).toContain("MOVING AVERAGE CROSS"));
    expect(detail().textContent).not.toContain("Buys when two averages cross");
    const title = within(detail()).getByText("MOVING AVERAGE CROSS").getAttribute("title");
    expect(title).toBe("Buys when two averages cross Stored as strategy_runs_ma_cross");
  });

  it("n3 the placeholder, the stations and how many reached each", async () => {
    await open();
    expect(screen.getByRole("searchbox").getAttribute("placeholder")).toBe("Find a strategy by name");
    const stops = Array.from(document.querySelectorAll(".sx-stop"));
    expect(stops.map((h) => h.querySelector(".sx-sname")!.textContent)).toEqual(["Screen", "Optimise", "Walk-forward", "Judge", "Reserve exam", "Second engine", "Paper"]);
    expect(stops.map((h) => h.querySelector(".sx-fig")!.textContent)).toEqual(["2", "0", "0", "2", "0", "0", "1"]);
    expect(stops[4].getAttribute("title")).toBe("Reserve exam: One last run on years kept back. Here now: 0. Stopped here: 0. Got this far or further: 1");
    // a stage narrows the table below
    fireEvent.click(stops[3]);
    expect(ids().sort()).toEqual(["1", "2"]);
  });

  it("n4 the detail chart's fill", async () => {
    await open();
    fireEvent.click(rowOf(2));
    await waitFor(() => expect(detail().querySelector("stop")).not.toBeNull());
    expect(Number(detail().querySelector("stop")!.getAttribute("stop-opacity"))).toBeCloseTo(0.16);
  });

  it("n5 axisDecimals", () => {
    expect(axisDecimals([9999.5, 10000, 10001])).toBe(2);
    expect(axisDecimals([100, 102, 104])).toBe(0);
  });

  it("n6 five tabs with their subs, OVERVIEW first, RETURN, the hue, OPEN RUNS as a btn", async () => {
    await open();
    fireEvent.click(rowOf(2));
    await waitFor(() => expect(detail().textContent).toContain("CAPITULATION REBOUND"));
    const tabs = Array.from(detail().querySelectorAll(".stb > span"));
    expect(tabs.map((t) => t.querySelector("b")!.textContent)).toEqual(["OVERVIEW", "RULES", "PARAMETERS", "WHERE IT TRADES", "HISTORY"]);
    expect(tabs[0].querySelector("i")!.textContent).toBe("Sharpe -0.30");
    expect(tabs[0].className).toContain("on");
    expect((detail().querySelector(".stb") as HTMLElement).style.getPropertyValue("--c")).toBe(strategyHue(2, [1, 2, 3, 4, 5, 6]));
    expect(detail().textContent).toContain("RETURN");
    expect(detail().textContent).toContain("+10.00%");
    const open1 = screen.getByRole("button", { name: "OPEN RUNS" });
    expect(open1.className).toContain("btn");
    expect(open1.className).not.toMatch(/\bq\b/);
    fireEvent.click(rowOf(3));
    await waitFor(() => expect(detail().querySelector(".stb > span i")!.textContent).toBe("no run"));
  });

  function paramsFor(id: number, kind: string, parameters: unknown[]) {
    fetchParams.mockImplementation((i: number) =>
      Promise.resolve(i === id ? { strategy_id: id, kind, run_id: null, parameters } : { strategy_id: i, kind: "standard", run_id: null, parameters: [] }));
  }
  const parametersTab = () => screen.getByText("PARAMETERS").closest("span")!;

  it("q1 a standard strategy's values and sources", async () => {
    paramsFor(1, "standard", [
      { name: "fast_window", range: null, value: 12, source: { file: "docs/research_lab/parameter_provenance.md", key: "fast_window", line: 7 } },
      { name: "slow_window", range: null, value: 40, source: null },
    ]);
    await open();
    fireEvent.click(rowOf(1));
    await waitFor(() => expect(parametersTab().querySelector("i")!.textContent).toBe("2 values"));
    fireEvent.click(parametersTab());
    const row = (await screen.findByText("Fast window")).closest(".gpr") as HTMLElement;
    expect(row.querySelector(".gbig")!.textContent).toBe("12");
    const src = within(row).getByText("provenance, line 7");
    expect(src.getAttribute("title")).toContain("fast_window");
    const row2 = screen.getByText("Slow window").closest(".gpr") as HTMLElement;
    expect(row2.querySelector(".gbig")!.textContent).toBe("40");
    expect(within(row2).getByTitle("No line in the provenance file for this parameter").textContent).toBe("-");
  });

  it("q2 a theory pick shows its range and its ranges.toml line", async () => {
    paramsFor(2, "theory", [
      { name: "N", range: "rebound_bb_window", value: { from: 15, to: 30, step: 5 },
        source: { file: "research_lab/theories/ranges.toml", key: "rebound_bb_window", line: 12, why: "Bollinger window" } },
    ]);
    await open();
    fireEvent.click(rowOf(2));
    await waitFor(() => expect(parametersTab().querySelector("i")!.textContent).toBe("1 pick"));
    fireEvent.click(parametersTab());
    expect(await screen.findByText("15 to 30, step 5")).toBeInTheDocument();
    const src = screen.getByText("ranges.toml, line 12");
    expect(src.getAttribute("title")).toContain("Bollinger window");
    expect(within(detail()).getByText("Rebound bb window")).toBeInTheDocument();
  });

  it("q3 a listed value is joined", async () => {
    paramsFor(1, "standard", [{ name: "bars", range: null, value: { values: ["1min", "5min", "1h"] }, source: null }]);
    await open();
    fireEvent.click(rowOf(1));
    await waitFor(() => expect(parametersTab().querySelector("i")!.textContent).toBe("1 value"));
    fireEvent.click(parametersTab());
    expect(await screen.findByText("1min, 5min, 1h")).toBeInTheDocument();
  });

  it("q4 no parameters says so, and the tab sub is none", async () => {
    await open();
    fireEvent.click(rowOf(3));
    await waitFor(() => expect(parametersTab().querySelector("i")!.textContent).toBe("none"));
    fireEvent.click(parametersTab());
    expect(await screen.findByText("No parameters recorded: this strategy has no single real run yet.")).toBeInTheDocument();
  });

  it("q5 a failed read shows its message", async () => {
    fetchParams.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The parameters could not be read" }));
    await open();
    fireEvent.click(rowOf(5));
    fireEvent.click(parametersTab());
    expect(await screen.findByText("The parameters could not be read")).toBeInTheDocument();
  });

  it("q6 the old not served yet text is gone", async () => {
    await open();
    fireEvent.click(rowOf(5));
    expect(document.body.textContent).not.toContain("not served yet");
    fireEvent.click(parametersTab());
    expect(document.body.textContent).not.toContain("not served yet");
  });

  it("q7 words, yes and no, and dotted names without underscores", async () => {
    paramsFor(1, "standard", [
      { name: "price_field", range: null, value: "close_adj", source: null },
      { name: "use_stop", range: null, value: true, source: null },
      { name: "open.bbw.n", range: null, value: 3, source: null },
    ]);
    await open();
    fireEvent.click(rowOf(1));
    await waitFor(() => expect(parametersTab().querySelector("i")!.textContent).toBe("3 values"));
    fireEvent.click(parametersTab());
    const v = await screen.findByText("Close adj");
    expect(v.getAttribute("title")).toBe("close_adj");
    expect(screen.getByText("yes")).toBeInTheDocument();
    expect(screen.getByText("Open bbw n")).toBeInTheDocument();
    expect(detail().textContent).not.toContain("_");
  });

  it("q8 the picked tab survives picking another strategy", async () => {
    await open();
    fireEvent.click(rowOf(5));
    fireEvent.click(parametersTab());
    fireEvent.click(rowOf(4));
    await waitFor(() => expect(detail().textContent).toContain("TREND FOLLOW"));
    expect(parametersTab().className).toContain("on");
    expect(sessionStorage.getItem("v2-memory:research/strategies/tab")).toContain("par");
  });

  it("r1 the finding: how many, how far, why the searches stopped", async () => {
    await open();
    const say = document.querySelector(".nl-say")!;
    expect(say.querySelector("b")!.textContent).toBe("5 strategies. 1 is on paper.");
    expect(say.querySelector(".more")!.textContent).toBe("1 stopped at the first test: the best attempt of each missed at least one target. ");
    expect(say.querySelector(".more em.bad")!.textContent).toBe("1 stopped at the first test");
  });

  it("r2 the picked theory's search: what it tried, its first screen, the buttons", async () => {
    fetchParams.mockImplementation((i: number) => Promise.resolve(i === 5
      ? { strategy_id: 5, kind: "theory", run_id: null, parameters: [
        { name: "N", range: "gap_window", value: { from: 10, to: 30, step: 10 }, source: { file: "research_lab/theories/ranges.toml", key: "gap_window", line: 3, why: "a book's gap rule" } }] }
      : { strategy_id: i, kind: "standard", run_id: null, parameters: [] }));
    await open();
    fireEvent.click(rowOf(5));
    const card = await screen.findByTestId("search-card");
        expect(screen.getByRole("dialog").textContent).toContain("Gap plays family, version 1");
    expect(card.textContent).toContain("144 of 144");
    const n = await waitFor(() => {
      const row = card.querySelector('[data-k="N"]') as HTMLElement;
      expect(row.querySelector(".sr-bxn")!.textContent).toBe("Gap window");
      return row;
    });
    expect([...n.querySelectorAll("td")].map((c) => c.textContent)).toEqual(["Gap window", "10", "30", "10", "a book's gap rule"]);
    expect(card.querySelector('[data-k="M"] .sr-bxn')!.textContent).toBe("M");
    const res = (k: string) => card.querySelector(`[data-testid="search-targets"] [data-k="${k}"] td:last-child`)!.textContent;
    expect(["closed", "win_share", "max_drawdown", "avg_month", "recovery"].map(res)).toEqual(["Met", "Missed", "Missed", "Met", "Met"]);
    expect(card.querySelector('[data-testid="search-targets"] [data-k="win_share"]')!.getAttribute("title")).toBe("Winning trades: 58%. Short of its bar");
    expect(card.textContent).toContain("Its best attempt met 3 of 5 targets, Sharpe 1.25, so the search stopped.");
    fireEvent.click(screen.getByRole("button", { name: "Open its run" }));
    await screen.findByText("RUNS PAGE?run=78");
  });

  it("r3 Open its search goes to the generator; a strategy written by hand has no search card", async () => {
    await open();
    fireEvent.click(rowOf(1));
    await waitFor(() => expect(detail().textContent).toContain("MOVING AVERAGE CROSS"));
    expect(screen.queryByTestId("search-card")).toBeNull();
    fireEvent.click(rowOf(5));
    fireEvent.click(await screen.findByRole("button", { name: "Open its search" }));
    await screen.findByText("GENERATOR PAGE");
  });

  it("r4 a theory with no search, and searches that could not be read", async () => {
    fetchOverview.mockResolvedValue([...OVERVIEW, { id: 7, name: "theory__lost", display_name: "Lost theory", version: 1, description: null, run: null }]);
    await open();
    expect(stateOf(7).textContent).toBe("No search found");
    cleanup();
    fetchTree.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The tree store is unreadable" }));
    mount();
    await screen.findByText("The tree store is unreadable");
    await waitFor(() => expect(stateTitle(5)).toBe("The generator's searches could not be read"));
    expect(stateOf(1).textContent).toBe("Passed the judge");
    expect(document.querySelector(".nl-say")).toBeNull();
  });

  it("r5 where a theory stands from its furthest run", () => {
    const s = { id: 9, name: "theory__x", display_name: "X", version: 1, description: null, run: null };
    const at = (runs: unknown[], extra: Record<string, unknown> = {}) => {
      const th = { ...theory(9, "x", "X"), runs, ...extra } as never;
      const w = whereOf(s, { theory: th, family: "F", approvedAt: null }, null, false);
      return [w.at, w.tone, w.word];
    };
    expect(at([])).toEqual([0, "dim", "Not started"]);
    expect(at([{ stage: "screen", run_id: 1, status: "running" }])).toEqual([0, "run", "Screen running"]);
    expect(at([{ stage: "screen", run_id: 1, status: "failed" }])).toEqual([0, "bad", "Screen failed"]);
    expect(at([{ stage: "screen", run_id: 1, status: "succeeded" }, { stage: "optimise", run_id: 2, status: "succeeded" }, { stage: "walkforward", run_id: 3, status: "succeeded" }]))
      .toEqual([2, "dim", "Walk-forward done"]);
    expect(at([{ stage: "screen", run_id: 1, status: "succeeded" }, { stage: "optimise", run_id: 2, status: "succeeded" }], { stop: { step: "optimise", reason: "no_finalist" } }))
      .toEqual([1, "bad", "Stopped while tuning"]);
    // the stop record names its own step, even when no run of that step was queued
    expect(at([{ stage: "screen", run_id: 1, status: "succeeded" }], { stop: { step: "optimise", reason: "The optimise run for X was refused: no stocks." } }))
      .toEqual([1, "bad", "Stopped while tuning"]);
    expect(at([{ stage: "screen", run_id: 1, status: "succeeded" }, { stage: "walkforward", run_id: 3, status: "succeeded" }], { stop: { step: "judge", reason: "The judge gave no usable answer" } }))
      .toEqual([3, "bad", "Stopped at the judge"]);
    // a stop by you, and the runs it cancelled
    expect(at([{ stage: "screen", run_id: 1, status: "succeeded" }, { stage: "optimise", run_id: 2, status: "cancelled" }], { stop: { step: "optimise", reason: "Stopped by you on 3 November." } }))
      .toEqual([1, "dim", "Stopped by you"]);
    expect(at([{ stage: "screen", run_id: 1, status: "succeeded" }, { stage: "optimise", run_id: 2, status: "cancelled" }]))
      .toEqual([1, "dim", "Optimise cancelled"]);
    // on paper: the reserve exam and the second engine are not read here, so they are not drawn as done
    expect(whereOf(s, null, null, true).unread).toEqual([4, 5]);
  });
});
