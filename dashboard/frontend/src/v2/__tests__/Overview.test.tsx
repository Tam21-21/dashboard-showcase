import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Overview, { failReasons, weekCounts } from "../pages/research/Overview";
import { APPS } from "../shell/sections";
import { SettingsProvider } from "../settings";

const listDatasets = vi.fn();
const listRuns = vi.fn();
const runsSummary = vi.fn();
const listStrategies = vi.fn();
const judgeRun = vi.fn();
const fetchJudgeSummary = vi.fn();
const fetchJudgeVerdicts = vi.fn();
const getSummary = vi.fn();
const getRunningRuns = vi.fn();
const fetchNames = vi.fn();
const getSystems = vi.fn();

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      research: {
        ...actual.api.research,
        listDatasets: (...a: unknown[]) => listDatasets(...a),
        listRuns: (...a: unknown[]) => listRuns(...a),
        runsSummary: (...a: unknown[]) => runsSummary(...a),
        listStrategies: (...a: unknown[]) => listStrategies(...a),
        judgeRun: (...a: unknown[]) => judgeRun(...a),
      },
    },
  };
});
vi.mock("../data/judgeApi", () => ({
  fetchJudgeSummary: () => fetchJudgeSummary(),
  fetchJudgeVerdicts: (...a: unknown[]) => fetchJudgeVerdicts(...a),
}));
vi.mock("../data/opsApi", () => ({ getSystems: () => getSystems() }));
vi.mock("../data/paperApi", () => ({
  getSummary: (w: string) => getSummary(w),
  getRunningRuns: () => getRunningRuns(),
}));
vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => fetchNames() }) };
});
import { ApiError } from "../../lib/api";

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "overview")!;

const at = (date: string, hm = "09:00") => Date.parse(`${date}T${hm}:00Z`) / 1000;

const dataset = (id: number, status: string, extra: Record<string, unknown> = {}) => ({
  id, name: `Set ${id}`, status, dataset_type: "price_dataset", last_error: null, ...extra,
});
const DATASETS = [dataset(1, "ready"), dataset(2, "ready"), dataset(3, "ready"), dataset(4, "ready"), dataset(5, "failed")];
const STRATS = [
  { id: 1, name: "Old one", version: 1, description: null },
  { id: 2, name: "Middle one", version: 1, description: null },
  { id: 3, name: "Newest one", version: 1, description: null },
];
const NAMES = [
  { id: 1, name: "old_one", display_name: "Old one", status: "active" },
  { id: 2, name: "middle_one", display_name: "Middle one", status: "active" },
  { id: 3, name: "newest_one", display_name: "Newest one", status: "active" },
];
const run = (id: number, date: string, verdict: string | null, extra: Record<string, unknown> = {}) => ({
  id, name: null, strategy_id: 1, status: "succeeded", created_at: at(date), sharpe_ratio: 0.5, verdict, kind: "standard", ...extra,
});
const RUNS = [
  run(1, "2026-10-06", "pass"), run(2, "2026-10-07", "pass"), run(3, "2026-10-08", "pass", { sharpe_ratio: 1.234 }),
  run(4, "2026-10-09", "fail"), run(5, "2026-10-10", "fail"), run(6, "2026-10-11", null),
  run(7, "2026-10-08", "pass", { status: "failed" }),
  run(8, "2026-07-20", "pass"),
];
const SUMMARY = { total: 52, by_status: { succeeded: 44, failed: 7, running: 1 }, in_flight_count: 1, in_flight: { run_id: 9, name: "Momentum reversal", status: "running", started_at: at("2026-10-12") } };
const JSUM = { store: { table_present: true, row_count: 16 }, graded: 16, survivors: 5, awaiting_ok: 2, in_paper: 0, unjudgeable: 0 };
const PSUM = { running_count: 4 };

const outcomes = (o: Record<number, string>) => Object.entries(o).map(([c, outcome]) => ({ check: Number(c), outcome }));
let vid = 0;
const verdict = (check2: string | null, current = true, v = "pass", extra: Record<string, unknown> = {}) => ({
  id: ++vid, current, verdict: v, strategy_name: `Strat ${vid}`, strategy_id: 1, graded_at: 1000 + vid,
  check_outcomes: check2 === null ? [] : outcomes({ 1: "cleared", 2: check2 }), measures: [], rejected: false, ...extra,
});
const VERDICTS = () => {
  vid = 0;
  return [
    verdict("cleared"), verdict("cleared"), verdict("cleared"), verdict("failed"),
    verdict("not-evaluable"), verdict("not-evaluable"), verdict(null),
    verdict("cleared", false),
  ];
};
const vresp = (rows: unknown[], present = true) => ({ store: { table_present: present, row_count: rows.length }, rows });

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsProvider>
        <MemoryRouter initialEntries={["/app/research/overview"]}>
          <Overview app={app} section={section} />
          <Where />
        </MemoryRouter>
      </SettingsProvider>
    </QueryClientProvider>,
  );
}
const stage = (id: string) => document.querySelector(`[data-stage="${id}"]`) as HTMLButtonElement;
const where = () => screen.getByTestId("where").textContent;

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  listDatasets.mockResolvedValue(DATASETS);
  listRuns.mockResolvedValue(RUNS);
  runsSummary.mockResolvedValue(SUMMARY);
  listStrategies.mockResolvedValue(STRATS);
  fetchJudgeSummary.mockResolvedValue(JSUM);
  fetchJudgeVerdicts.mockResolvedValue(vresp(VERDICTS()));
  getSummary.mockResolvedValue(PSUM);
  getRunningRuns.mockResolvedValue([]);
  fetchNames.mockResolvedValue(NAMES);
  getSystems.mockResolvedValue({ systems: [] });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [listDatasets, listRuns, runsSummary, listStrategies, judgeRun, fetchJudgeSummary, fetchJudgeVerdicts, getSummary, getRunningRuns, fetchNames, getSystems]) m.mockReset();
});

async function open() {
  mount();
  await waitFor(() => expect(stage("judge").textContent).toContain("16"));
}

describe("Research overview, new look", () => {
  it("(a) each station shows its figure, its fact and its state word, coloured by the rule", async () => {
    await open();
    const want: Record<string, [string, string, string]> = {
      data: ["5", "Broken", "t-bad"],
      strategies: ["3", "Waits for you", "t-warn"],
      runs: ["52", "Works", "t-good"],
      judge: ["16", "Waits for you", "t-warn"],
      paper: ["4", "Works", "t-good"],
      capital: ["-", "Not reached", "t-dim"],
    };
    for (const [id, [fig, word, cls]] of Object.entries(want)) {
      const s = stage(id);
      expect(s.querySelector(".fig")!.textContent).toBe(fig);
      expect(s.textContent).toContain(word);
      expect(s.className).toContain(cls);
    }
    // The run counts are the whole log's (the summary), not the runs list's.
    expect(stage("runs").textContent).toContain("7 failed");
    expect(stage("runs").title).toContain("7 of 52 failed");
    expect(stage("judge").textContent).toContain("5 passed");
  });

  it("(a2) the mockup's sections are all there, in order", async () => {
    await open();
    await waitFor(() => expect(document.querySelector(".ov7-stops")).toBeTruthy());
    const heads = [...document.querySelectorAll("h2")].map((h) => h.textContent);
    expect(heads).toEqual(["Pipeline overview", "Needs you", "Judge summary", "Needs approval", "Runs summary", "Reasons of failure"]);
  });

  it("(b) a station's hover says why it has its colour and states the rule", async () => {
    await open();
    expect(stage("data").title).toContain("1 dataset failed the last fetch.");
    expect(stage("data").title).toContain("Red if a dataset failed its last fetch");
    expect(stage("strategies").title).toContain("1 of 3 strategies have a finished run that waits");
    expect(stage("judge").title).toContain("2 runs passed and wait for your approval.");
  });

  it("(c) clicking a station opens its page", async () => {
    await open();
    fireEvent.click(stage("runs"));
    expect(where()).toBe("/app/research/runs");
    fireEvent.click(stage("paper"));
    expect(where()).toBe("/app/research/forward-testing");
  });

  it("(d) the finding says how many passed and how many are on paper", async () => {
    await open();
    const say = document.querySelector(".nl-say")!;
    expect(say.textContent).toContain("stop the lab.");
    expect(say.textContent).toContain("4 on paper.");
    expect(document.querySelector('[data-stopper="waiting"]')!.textContent).toContain("1 run waits for the judge");
  });

  it("(e) nothing on paper and no verdict: the page says so", async () => {
    fetchJudgeSummary.mockResolvedValue({ ...JSUM, survivors: 0, awaiting_ok: 0, unjudgeable: 16 });
    getSummary.mockResolvedValue({ running_count: 0 });
    vid = 0;
    fetchJudgeVerdicts.mockResolvedValue(vresp([verdict(null, true, "unjudgeable"), verdict(null, true, "unjudgeable")]));
    await open();
    await waitFor(() => expect(document.querySelector(".nl-say")?.textContent).toContain("Nothing has reached paper yet."));
    expect(stage("judge").className).toContain("t-bad");
    expect(stage("paper").className).toContain("t-dim");
  });

  it("(f) the judge summary shows the current verdicts only, a row a check, a bar of the runs graded", async () => {
    await open();
    await waitFor(() => expect(document.querySelector(".ov7-checks")).toBeTruthy());
    expect(document.querySelectorAll(".ov7-check")).toHaveLength(9);
    const walk = document.querySelector('.ov7-check[data-check="2"]')!;
    expect(walk.textContent).toContain("3 cleared, 1 failed, 3 could not run");
    expect(screen.getByText("Judge summary")).toBeTruthy();
    expect(screen.getByText("Each bar is the 7 runs the judge has looked at")).toBeTruthy();
  });

  it("(g) the waiting list names each strategy by its display name and sends its run to the judge", async () => {
    judgeRun.mockResolvedValue({});
    await open();
    const wait = document.querySelector(".ov6-wait") as HTMLElement;
    await waitFor(() => expect(within(wait).getByText("Old one")).toBeTruthy());
    const stop = () => document.querySelector('[data-stopper="waiting"]') as HTMLElement;
    expect(wait.textContent).toContain("run 6, 11 Oct");
    expect(document.body.textContent).not.toContain("old_one");
    const calls = listRuns.mock.calls.length;
    let reread!: (v: unknown) => void;
    listRuns.mockImplementation(() => new Promise((r) => { reread = r; }));
    fireEvent.click(within(stop()).getByRole("button", { name: "Send 1 run to the judge" }));
    await waitFor(() => expect(judgeRun).toHaveBeenCalledWith(6));
    await waitFor(() => expect(listRuns.mock.calls.length).toBeGreaterThan(calls));
    // Until the list is read again the button stays pressed, so a second press cannot send run 6 twice.
    expect((within(stop()).getByRole("button", { name: /^Sending/ }) as HTMLButtonElement).disabled).toBe(true);
    reread(RUNS.map((r) => (r.id === 6 ? { ...r, verdict: "pass" } : r)));
    await waitFor(() => expect(document.querySelector('[data-stopper="waiting"]')).toBeNull());
    expect(judgeRun).toHaveBeenCalledTimes(1);
  });

  it("(h) a refused send shows the server's words and keeps the button", async () => {
    judgeRun.mockRejectedValue(new ApiError("409 /api/judge/runs/6/judge", 409, { detail: "This run is already being judged" }, "/api/judge/runs/6/judge"));
    await open();
    const stop = () => document.querySelector('[data-stopper="waiting"]') as HTMLElement;
    await waitFor(() => expect(stop()).toBeTruthy());
    await waitFor(() => expect(within(stop()).getByRole("button", { name: "Send 1 run to the judge" })).toBeTruthy());
    let reread!: (v: unknown) => void;
    listRuns.mockImplementation(() => new Promise((r) => { reread = r; }));
    fireEvent.click(within(stop()).getByRole("button", { name: "Send 1 run to the judge" }));
    await waitFor(() => expect(judgeRun).toHaveBeenCalledWith(6));
    // The words and the button come back only after the list is read again.
    await waitFor(() => expect(reread).toBeTypeOf("function"));
    expect(screen.queryByRole("alert")).toBeNull();
    expect((within(stop()).getByRole("button", { name: /^Sending/ }) as HTMLButtonElement).disabled).toBe(true);
    reread(RUNS);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Run 6 was not sent: This run is already being judged"));
    expect(within(stop()).getByRole("button", { name: "Send 1 run to the judge" })).toBeTruthy();
  });

  it("(i) Open the judge opens the judge", async () => {
    await open();
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Open the judge" }).length).toBeGreaterThan(0));
    fireEvent.click(screen.getAllByRole("button", { name: "Open the judge" })[0]);
    expect(where()).toBe("/app/research/judge");
  });

  it("(j) a route that fails says so on its station, in the server's words", async () => {
    listDatasets.mockRejectedValue(new ApiError("500 /api/research/datasets", 500, { detail: "The datasets table is locked" }, "/api/research/datasets"));
    await open();
    await waitFor(() => expect(stage("data").textContent).toContain("Could not read"));
    expect(stage("data").title).toContain("The datasets table is locked");
  });

  it("(k) runs by week and failures by reason", async () => {
    await open();
    await waitFor(() => expect(document.querySelector(".ov6-weeks")).toBeTruthy());
    expect(screen.getByText("Reasons of failure")).toBeTruthy();
    expect(screen.getByText("44 finished").className).toContain("good");
    expect([...document.querySelectorAll(".nl-st.bad")].map((e) => e.textContent)).toContain("7 failed");
    expect(screen.getByText("No error recorded")).toBeTruthy();
    expect(screen.queryByText("Newest 500")).toBeNull();
  });

  it("(l) when the runs list stops at its newest 500, both run cards say so on hover", async () => {
    listRuns.mockResolvedValue(Array.from({ length: 500 }, (_, k) => run(1000 + k, "2026-10-05", "pass", k % 50 === 0 ? { status: "failed" } : {})));
    runsSummary.mockResolvedValue({ ...SUMMARY, total: 812, by_status: { succeeded: 700, failed: 112 } });
    await open();
    await waitFor(() => expect(screen.getAllByText("Newest 500")).toHaveLength(2));
    expect(screen.getAllByText("Newest 500")[0].getAttribute("title")).toBe("These count the newest 500 runs of the 812 in the log.");
    expect(screen.getByText("700 finished")).toBeTruthy();
    expect(stage("runs").querySelector(".fig")!.textContent).toBe("812");
  });
});

describe("Research overview on a fresh database", () => {
  it("every card says what is missing and nothing breaks", async () => {
    listDatasets.mockResolvedValue([]);
    listRuns.mockResolvedValue([]);
    runsSummary.mockResolvedValue({ total: 0, by_status: {}, in_flight_count: 0, in_flight: null });
    listStrategies.mockResolvedValue([]);
    fetchJudgeSummary.mockResolvedValue({ ...JSUM, graded: 0, survivors: 0, awaiting_ok: 0, unjudgeable: 0 });
    fetchJudgeVerdicts.mockResolvedValue(vresp([]));
    getSummary.mockResolvedValue({ running_count: 0 });
    fetchNames.mockResolvedValue([]);
    mount();
    await waitFor(() => expect(screen.getByText("No run has been judged yet.")).toBeTruthy());
    expect(screen.getByText("No finished run waits for the judge.")).toBeTruthy();
    expect(screen.getByText("Every part is running, the data is fresh and the judge can decide.")).toBeTruthy();
    expect(screen.getByText("No run has finished yet.")).toBeTruthy();
    expect(screen.getByText("No run has failed.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Send/ })).toBeNull();
    for (const id of ["data", "strategies", "runs", "judge", "paper", "capital"]) expect(stage(id).className).toContain("t-dim");
    expect(document.querySelector(".nl-say")!.textContent).toBe("Nothing stops the lab.Nothing has reached paper yet.");
  });
});

describe("weekCounts and failReasons", () => {
  const r = (id: number, date: string, status: string, error: string | null = null) =>
    ({ ...run(id, date, null), status, error }) as unknown as Parameters<typeof weekCounts>[0][number];
  it("counts finished and failed runs per Monday week up to this week, with empty weeks between", () => {
    const now = Date.parse("2026-10-12T12:00:00Z");
    const w = weekCounts([r(1, "2026-09-28", "succeeded"), r(2, "2026-10-04", "failed"), r(3, "2026-10-12", "succeeded"), r(4, "2026-10-12", "running")], now);
    expect(w.map((x) => [new Date(x.start).toISOString().slice(0, 10), x.ok, x.failed])).toEqual([
      ["2026-09-28", 1, 1],
      ["2026-10-05", 0, 0],
      ["2026-10-12", 1, 0],
    ]);
    expect(weekCounts([r(5, "2026-10-12", "running")], now)).toEqual([]);
  });
  it("keeps at most 52 weeks", () => {
    const now = Date.parse("2026-10-12T12:00:00Z");
    expect(weekCounts([r(1, "2025-01-06", "succeeded")], now)).toHaveLength(52);
  });
  it("says the lab's own error lines in plain words", () => {
    const f = failReasons([r(1, "2026-10-01", "failed", "volatility is undefined at entry (atr needs 278 bars); reason=undefined_warmup")]);
    expect(f[0].text).toBe("Volatility undefined at entry");
  });
  it("groups failures by the error's class, or by the first line when it names none, most first", () => {
    const f = failReasons([
      r(1, "2026-10-01", "failed", "KeyError: (10, 30, 'AAPL')\ntrace"), r(2, "2026-10-02", "failed", "KeyError: 'MSFT'"),
      r(3, "2026-10-03", "failed", null), r(4, "2026-10-04", "succeeded"), r(5, "2026-10-05", "failed", "Dataset missing"),
    ]);
    expect(f).toEqual([
      { key: "KeyError", text: "'MSFT'", n: 2, lines: ["KeyError: (10, 30, 'AAPL')", "KeyError: 'MSFT'"] },
      { key: "Dataset missing", text: "Dataset missing", n: 1, lines: ["Dataset missing"] },
      { key: "No error recorded", text: "No error recorded", n: 1, lines: ["No error recorded"] },
    ]);
  });
});
