import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RoadToPaper from "../pages/research/strategies/RoadToPaper";
import { strategyNamesKey } from "../data/strategyNames";
import { ApiError } from "../../lib/api";

const fetchRuns = vi.fn();
const fetchSource = vi.fn();
const renameStrategy = vi.fn();
const deleteStrategy = vi.fn();
const fetchSupply = vi.fn();
const takeExam = vi.fn();
const fetchRetest = vi.fn();
const startRetest = vi.fn();
const fetchFills = vi.fn();
const replayResult = vi.fn();
const rejectVerdict = vi.fn();
const approve = vi.fn();
const sendToJudge = vi.fn();
const onEditCode = vi.fn();

vi.mock("../data/strategiesApi", () => ({
  RUNS_AT_MOST: 500,
  strategiesKeys: {
    overview: ["strategies", "overview"],
    parameters: (id: number) => ["strategies", "parameters", id],
    runs: (id: number) => ["strategies", "runs", id],
    supply: ["strategies", "reserve-supply"],
    retest: (runId: number) => ["strategies", "retest", runId],
    fills: (id: number) => ["strategies", "retest-fills", id],
  },
  fetchStrategyRuns: (...a: unknown[]) => fetchRuns(...a),
  fetchStrategySource: (...a: unknown[]) => fetchSource(...a),
  renameStrategy: (...a: unknown[]) => renameStrategy(...a),
  deleteStrategy: (...a: unknown[]) => deleteStrategy(...a),
  fetchReserveSupply: (...a: unknown[]) => fetchSupply(...a),
  takeReserveExam: (...a: unknown[]) => takeExam(...a),
  fetchRetest: (...a: unknown[]) => fetchRetest(...a),
  startRetest: (...a: unknown[]) => startRetest(...a),
  fetchRetestFills: (...a: unknown[]) => fetchFills(...a),
  replayResult: (...a: unknown[]) => replayResult(...a),
}));
vi.mock("../data/judgeApi", () => ({
  rejectVerdict: (...a: unknown[]) => rejectVerdict(...a),
  approveVerdictForPaper: (...a: unknown[]) => approve(...a),
}));
vi.mock("../data/runsApi", () => ({
  runsKeys: { list: ["runs", "list"], summary: ["runs", "summary"], strategies: ["strategies", "list"] },
  sendToJudge: (...a: unknown[]) => sendToJudge(...a),
}));

vi.mock("../data/sweepApi", () => ({
  sweepKeys: { lists: ["sweep", "lists"], params: (id: number) => ["sweep", "params", id] },
  fetchStockLists: () => Promise.resolve([]),
  fetchSweepParams: () => Promise.resolve({ sweep_params: [], param_mode: "zero" }),
  saveListWindow: vi.fn(),
  startSweep: vi.fn(),
  startWalkForward: vi.fn(),
}));
vi.mock("../data/strategyNames", async () => {
  const real = await vi.importActual<typeof import("../data/strategyNames")>("../data/strategyNames");
  const rows = [{ id: 1, name: "strategy_runs_ma_cross", display_name: "Moving average cross", status: "active" }];
  return { ...real, useStrategyNames: () => ({ data: rows, isError: false, error: null }) };
});

const EM_DASH = String.fromCharCode(8212);
const JUL_2023 = 1688169600;
const JAN_2024 = 1704067200;

const S = {
  id: 1, name: "strategy_runs_ma_cross", display_name: "Moving average cross", version: 3, description: null,
  run: {
    id: 101, created_at: "2026-10-01T00:00:00Z", strategy_version: 3, equity_sparkline: [1, 2, 3], sharpe_ratio: 1.2,
    num_trades: 40, total_return: 0.1, max_drawdown: -0.1,
  },
};
function verdict(extra: Record<string, unknown> = {}, cleared = 8) {
  return {
    id: 7, run_id: 101, variation_id: 12, ticker: "AAPL", strategy_id: 1, verdict: "pass", is_advisory: false,
    graded_at: 1000, current: true, rejected: false, judged_capital: 25000,
    check_outcomes: Array.from({ length: 9 }, (_, i) => ({ check: i + 1, outcome: i < cleared ? "cleared" : "failed" })),
    ...extra,
  };
}
const WINDOWS = [0, 1, 2, 3].map((i) => ({
  index: i, name: `w${i}`, start_ts: JUL_2023, end_ts: JAN_2024, spent: i === 0,
}));
const RUNS = [
  { id: 11, created_at: JUL_2023, status: "succeeded", sharpe_ratio: 1.1, num_trades: 30, total_return: 0.12, equity_sparkline: [1, 2, 3] },
  { id: 12, created_at: JAN_2024, status: "failed", sharpe_ratio: null, num_trades: null, total_return: null, equity_sparkline: null },
  { id: 13, created_at: JAN_2024, status: "running", sharpe_ratio: null, num_trades: null, total_return: null, equity_sparkline: null },
];
const RETEST_ROW = { id: 5, status: "report-only", could_not_run_reason: null, export_path: "x.parquet", gap_final_equity: 0.0125, gap_sharpe: 0.31 };

let qc: QueryClient;
let invalidate: { mock: { calls: unknown[][] } };
function mount(v: unknown = verdict(), s: unknown = S) {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  invalidate = vi.spyOn(qc, "invalidateQueries");
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/here"]}>
        <Routes>
          <Route path="/here" element={<RoadToPaper s={s as never} verdict={v as never} onEditCode={onEditCode} />} />
          <Route path="/app/research/runs" element={<div>RUNS PAGE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const col = (id: string) => screen.getByTestId(id);
const dialog = () => screen.getByRole("dialog");
const press = (name: string, root: HTMLElement | Document = document.body) =>
  fireEvent.click(within(root as HTMLElement).getByRole("button", { name }));
const invalidated = () => invalidate.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey));
async function ready(v: unknown = verdict()) {
  mount(v);
  await waitFor(() => expect(col("rp-exam").textContent).toContain("LEFT"));
  await waitFor(() => expect(screen.getByTestId("rp-foot").textContent).toContain("RUNS"));
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  fetchRuns.mockResolvedValue(RUNS);
  fetchSource.mockResolvedValue({ source_code: "SRC" });
  renameStrategy.mockResolvedValue({ id: 1, display_name: "New name", version: 3, action: "unchanged" });
  deleteStrategy.mockResolvedValue({ action: "tombstoned", id: 1, orphaned_runs: 3 });
  fetchSupply.mockResolvedValue({ enumerated: 4, spent: 1, windows: WINDOWS });
  takeExam.mockResolvedValue({ exam: { run_id: 9, window_start_ts: JUL_2023, window_end_ts: JAN_2024 }, window_name: "x" });
  fetchRetest.mockResolvedValue({ state: "never-retested", retests: [] });
  startRetest.mockResolvedValue({ job_id: 4 });
  fetchFills.mockResolvedValue({
    fills: [
      { signal_date: JUL_2023, side: "buy", real_fill_price: 10.5, shadow_fill_price: 10.4, vbt_fill_price: 10.45, diff_vbt: 0.05 },
      { signal_date: JAN_2024, side: "sell", real_fill_price: 11, shadow_fill_price: 11, vbt_fill_price: null, diff_vbt: null },
    ],
  });
  replayResult.mockResolvedValue({ status: "replay-queued", run_id: 101, variation_id: 12, ticker: "AAPL" });
  rejectVerdict.mockResolvedValue({ rejection_id: 1, verdict_id: 7 });
  approve.mockResolvedValue({ paper_run_id: 2, verdict_id: 7 });
  sendToJudge.mockResolvedValue({});
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [fetchRuns, fetchSource, renameStrategy, deleteStrategy, fetchSupply, takeExam, fetchRetest, startRetest,
    fetchFills, replayResult, rejectVerdict, approve, sendToJudge, onEditCode]) m.mockReset();
});

describe("RoadToPaper card", () => {
  it("r1 the head: name, version, the four buttons in order, RUN A SWEEP opens the window on this strategy", async () => {
    await ready();
    const head = screen.getByTestId("rp-head");
    expect(head.textContent).toContain("Moving average cross");
    expect(head.textContent).toContain("version 3");
    const buttons = Array.from(head.querySelectorAll("button"));
    expect(buttons.map((b) => b.textContent)).toEqual(["RUN A SWEEP", "RENAME", "EDIT CODE", "DELETE"]);
    expect(buttons[0]).toBeEnabled();
    expect(buttons[0].title).toBe("");
    expect(buttons[2]).toBeEnabled();
    expect(buttons[2].title).toBe("");
    fireEvent.click(buttons[0]);
    const win = screen.getByRole("dialog", { name: "Run a sweep" });
    expect((within(win).getByLabelText("Strategy") as HTMLSelectElement).value).toBe("1");
    expect(within(win).getByRole("option", { name: "Moving average cross" })).toBeInTheDocument();
    fireEvent.click(within(win).getByRole("button", { name: "CANCEL" }));
    fireEvent.click(buttons[2]);
    expect(onEditCode).toHaveBeenCalledTimes(1);
    expect(onEditCode).toHaveBeenCalledWith(1);
    expect(buttons[3].className).toContain("danger");
    expect(buttons[1].className).toContain("q");
  });

  it("r2 RENAME sends the fetched source and the typed name once; a refusal shows in the server's words", async () => {
    await ready();
    press("RENAME");
    const input = within(dialog()).getByLabelText("Display name") as HTMLInputElement;
    expect(input.value).toBe("Moving average cross");
    fireEvent.change(input, { target: { value: "New name" } });
    press("SAVE", dialog());
    await waitFor(() => expect(renameStrategy).toHaveBeenCalledTimes(1));
    expect(fetchSource).toHaveBeenCalledTimes(1);
    expect(renameStrategy).toHaveBeenCalledWith(1, "SRC", "New name");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(invalidated()).toContain(JSON.stringify(["strategies", "overview"]));
    expect(invalidated()).toContain(JSON.stringify(strategyNamesKey));

    renameStrategy.mockRejectedValue(new ApiError("400 /x", 400, { detail: "the display name cannot be blank" }));
    press("RENAME");
    press("SAVE", dialog());
    await within(dialog()).findByText("the display name cannot be blank");
  });

  it("r3 DELETE asks first, then says what it did with the runs", async () => {
    await ready();
    press("DELETE", screen.getByTestId("rp-head"));
    expect(within(dialog()).getByText("Delete Moving average cross?")).toBeInTheDocument();
    expect(deleteStrategy).not.toHaveBeenCalled();
    press("DELETE", dialog());
    await within(dialog()).findByText("Deleted; its 3 runs are kept");
    expect(deleteStrategy).toHaveBeenCalledTimes(1);
    expect(deleteStrategy).toHaveBeenCalledWith(1);
    expect(invalidated()).toContain(JSON.stringify(["strategies", "overview"]));
    expect(invalidated()).toContain(JSON.stringify(strategyNamesKey));
    cleanup();

    deleteStrategy.mockResolvedValue({ action: "removed", id: 1 });
    await ready();
    press("DELETE", screen.getByTestId("rp-head"));
    press("DELETE", dialog());
    await within(dialog()).findByText("Deleted");
    cleanup();

    deleteStrategy.mockResolvedValue({ action: "noop", id: 1 });
    await ready();
    press("DELETE", screen.getByTestId("rp-head"));
    press("DELETE", dialog());
    await within(dialog()).findByText("Already deleted");
  });

  it("r4 the judge column: 8 OF 9 CLEARED and PASSED; no verdict gives dashes and held buttons", async () => {
    await ready();
    const j = col("rp-judge");
    expect(j.textContent).toContain("8 OF 9 CLEARED");
    expect(j.textContent).toContain("PASSED");
    expect(Array.from(j.querySelectorAll("title")).map((t) => t.textContent)).toContain("Check 9: failed");
    cleanup();

    fetchRetest.mockClear();
    mount(null);
    await waitFor(() => expect(col("rp-exam").textContent).toContain("LEFT"));
    const j2 = col("rp-judge");
    expect(j2.textContent).not.toContain("CLEARED");
    expect(j2.textContent).not.toContain("PASSED");
    expect(within(j2).getAllByTitle("Not judged yet").length).toBeGreaterThan(0);
    expect(within(j2).getByRole("button", { name: "REPLAY" })).toBeDisabled();
    expect(within(j2).getByRole("button", { name: "REJECT" })).toBeDisabled();
    expect(within(col("rp-engine")).getByRole("button", { name: "RETEST" })).toBeDisabled();
    expect(within(col("rp-engine")).getByRole("button", { name: "FILLS" })).toBeDisabled();
    expect(within(col("rp-engine")).getByRole("button", { name: "RETEST" })).toHaveAttribute("title", "Not judged yet");
    expect(screen.getByRole("button", { name: "APPROVE FOR PAPER" })).toBeDisabled();
    expect(fetchRetest).not.toHaveBeenCalled();
  });

  it("r5 REPLAY sends the cell once; an existing replay and a refusal both speak plainly", async () => {
    await ready();
    press("REPLAY", col("rp-judge"));
    press("REPLAY", dialog());
    await within(dialog()).findByText("Replay queued");
    expect(replayResult).toHaveBeenCalledTimes(1);
    expect(replayResult).toHaveBeenCalledWith(101, 12, "AAPL");
    cleanup();

    replayResult.mockResolvedValue({ status: "replay-exists", run_id: 101, variation_id: 12, ticker: "AAPL" });
    await ready();
    press("REPLAY", col("rp-judge"));
    press("REPLAY", dialog());
    await within(dialog()).findByText("A saved replay of this result already exists");
    cleanup();

    replayResult.mockRejectedValue(new ApiError("409 /x", 409, { detail: "worker-offline: no research worker has reported online recently" }));
    await ready();
    press("REPLAY", col("rp-judge"));
    press("REPLAY", dialog());
    await within(dialog()).findByText(/^No research worker/);
  });

  it("r6 REJECT sends the verdict id and the reason once, and refreshes the verdicts", async () => {
    await ready();
    press("REJECT", col("rp-judge"));
    fireEvent.change(within(dialog()).getByLabelText("Reason"), { target: { value: "too thin" } });
    press("REJECT", dialog());
    await waitFor(() => expect(rejectVerdict).toHaveBeenCalledTimes(1));
    expect(rejectVerdict).toHaveBeenCalledWith(7, "too thin");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(invalidated().some((k) => k.includes("judge"))).toBe(true);
    cleanup();

    await ready();
    press("REJECT", col("rp-judge"));
    press("REJECT", dialog());
    await waitFor(() => expect(rejectVerdict).toHaveBeenLastCalledWith(7, undefined));
  });

  it("r7 the reserve exam: 3 OF 4 LEFT, hovers, a seed shown and sent once", async () => {
    await ready();
    const e = col("rp-exam");
    expect(e.textContent).toContain("3 OF 4 LEFT");
    const titles = Array.from(e.querySelectorAll("title")).map((t) => t.textContent);
    expect(titles).toContain("01/07/2023 to 01/01/2024: untouched");
    expect(titles).toContain("01/07/2023 to 01/01/2024: used");
    expect(within(e).getByTitle("The server does not report a strategy's exam result yet").textContent).toBe("-");
    const rnd = vi.spyOn(globalThis.crypto, "getRandomValues").mockImplementation(((a: Uint32Array) => {
      a[0] = 4000000000;
      return a;
    }) as never);
    press("TAKE THE NEXT EXAM", e);
    const d = dialog();
    expect(d.textContent).toContain("Seed 2000000000");
    expect(rnd).toHaveBeenCalledTimes(1);
    press("START", d);
    await within(dialog()).findByText("Exam started on 01/07/2023 to 01/01/2024");
    expect(takeExam).toHaveBeenCalledTimes(1);
    expect(takeExam).toHaveBeenCalledWith(1, 2000000000);
    expect(invalidated()).toContain(JSON.stringify(["strategies", "reserve-supply"]));
    cleanup();

    fetchSupply.mockResolvedValue({ enumerated: 4, spent: 4, windows: WINDOWS.map((w) => ({ ...w, spent: true })) });
    await ready();
    const btn = within(col("rp-exam")).getByRole("button", { name: "TAKE THE NEXT EXAM" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "No untouched stretch is left");
  });

  it("r8 each retest state in words, the gaps, the bar, and the refusals", async () => {
    const cases: Array<[string, string, string | undefined, Record<string, unknown>]> = [
      ["never-retested", "NOT RETESTED", undefined, {}],
      ["passed", "PASSED", undefined, {}],
      ["report-only", "NOT PASSED", "It ran; its gaps are outside the allowed gap, or no allowed gap is set", {}],
      ["could-not-run", "COULD NOT RUN", "Reason: missing bars", { status: "could-not-run", could_not_run_reason: "missing-bars" }],
      ["unreadable-record", "UNREADABLE", undefined, {}],
      ["odd", "-", "Unknown retest state", {}],
    ];
    for (const [state, word, why, row] of cases) {
      fetchRetest.mockResolvedValue({ state, retests: state === "never-retested" ? [] : [{ ...RETEST_ROW, ...row }] });
      await ready();
      const eng = col("rp-engine");
      if (word !== "-") await waitFor(() => expect(within(eng).getByText(word)).toBeInTheDocument());
      if (why) await waitFor(() => expect(within(eng).getAllByTitle(why).length).toBeGreaterThan(0));
      cleanup();
    }
    fetchRetest.mockResolvedValue({ state: "report-only", retests: [RETEST_ROW] });
    await ready();
    const eng = col("rp-engine");
    await waitFor(() => expect(eng.textContent).toContain("+1.25%"));
    expect(eng.textContent).toContain("+0.31");
    expect(within(eng).getByTitle("The server does not send the allowed gap yet").textContent).toBe("-");
    cleanup();

    fetchRetest.mockResolvedValue({ state: "report-only", retests: [{ ...RETEST_ROW, gap_final_equity: null, gap_sharpe: null }] });
    await ready();
    await waitFor(() => expect(within(col("rp-engine")).getAllByTitle("No gap recorded for this retest").length).toBe(2));
    cleanup();

    const refusals: Array<[string, string]> = [
      ["refused: ungraded-candidate", "This run has no judge verdict yet"],
      ["refused: unsupported-timeframe", "Only daily runs can be retested"],
      ["refused: missing-bars", "The run's price file is not stored"],
      ["refused: missing-configuration-statement", "The run's settings were not recorded"],
    ];
    for (const [detail, words] of refusals) {
      startRetest.mockRejectedValue(new ApiError("409 /x", 409, { detail }));
      await ready();
      press("RETEST", col("rp-engine"));
      press("RETEST", dialog());
      await within(dialog()).findByText(words);
      cleanup();
    }
    startRetest.mockReset();
    startRetest.mockResolvedValue({ job_id: 4 });
    await ready();
    press("RETEST", col("rp-engine"));
    press("RETEST", dialog());
    await within(dialog()).findByText("Retest queued");
    expect(startRetest).toHaveBeenCalledTimes(1);
    expect(startRetest).toHaveBeenCalledWith(101);
  });

  it("r9 FILLS lists the rows; none recorded holds the button", async () => {
    fetchRetest.mockResolvedValue({ state: "report-only", retests: [{ ...RETEST_ROW, id: 4 }, RETEST_ROW] });
    await ready();
    await waitFor(() => expect(within(col("rp-engine")).getByRole("button", { name: "FILLS" })).toBeEnabled());
    press("FILLS", col("rp-engine"));
    const d = dialog();
    await within(d).findByText("01/07/2023");
    expect(fetchFills).toHaveBeenCalledWith(5);
    expect(Array.from(d.querySelectorAll("th")).map((h) => h.textContent)).toEqual(["DATE", "SIDE", "BACKTEST", "NAUTILUS", "SHADOW", "GAP"]);
    expect(within(d).getByText("BUY")).toBeInTheDocument();
    expect(within(d).getByText("SELL")).toBeInTheDocument();
    expect(d.querySelectorAll(".dbar").length).toBe(1);
    cleanup();

    fetchRetest.mockResolvedValue({ state: "report-only", retests: [{ ...RETEST_ROW, export_path: null }] });
    await ready();
    await waitFor(() => expect(within(col("rp-engine")).getByRole("button", { name: "FILLS" })).toHaveAttribute("title", "No fills recorded for this retest"));
    expect(within(col("rp-engine")).getByRole("button", { name: "FILLS" })).toBeDisabled();
  });

  it("r10 the foot: RUNS, the run window, the judge and paper buttons once each", async () => {
    await ready();
    const foot = screen.getByTestId("rp-foot");
    expect(foot.textContent).toContain("RUNS");
    expect(foot.textContent).toContain("3");
    press("OPEN RUNS", foot);
    const d = dialog();
    expect(Array.from(d.querySelectorAll("th")).map((h) => h.textContent)).toEqual(["DATE", "STATE", "SHARPE", "TRADES", "RETURN", "CURVE"]);
    expect(within(d).getByText("DONE")).toBeInTheDocument();
    expect(within(d).getByText("FAILED")).toBeInTheDocument();
    expect(within(d).getByText("RUNNING")).toBeInTheDocument();
    press("OPEN THE RUNS PAGE", d);
    await screen.findByText("RUNS PAGE");
    cleanup();

    await ready();
    press("SEND TO THE JUDGE");
    await screen.findByText("Sent to the judge");
    expect(sendToJudge).toHaveBeenCalledTimes(1);
    expect(sendToJudge).toHaveBeenCalledWith(101);

    press("APPROVE FOR PAPER", screen.getByTestId("rp-foot"));
    press("APPROVE FOR PAPER", dialog());
    await screen.findByText("Sent to paper");
    expect(approve).toHaveBeenCalledTimes(1);
    expect(approve).toHaveBeenCalledWith(7, 25000);
    cleanup();

    fetchRuns.mockResolvedValue(Array.from({ length: 500 }, (_, i) => ({ ...RUNS[0], id: i + 1 })));
    await ready();
    expect(within(screen.getByTestId("rp-foot")).getByTitle("The server sends at most 500 runs at a time").textContent).toBe("500+");
    cleanup();

    mount(verdict({ verdict: "fail" }, 3), { ...S, run: null });
    await waitFor(() => expect(col("rp-exam").textContent).toContain("LEFT"));
    const approveBtn = screen.getByRole("button", { name: "APPROVE FOR PAPER" });
    expect(approveBtn).toBeDisabled();
    expect(approveBtn).toHaveAttribute("title", "Only a passed, current verdict can go to paper");
    expect(screen.getByRole("button", { name: "SEND TO THE JUDGE" })).toHaveAttribute("title", "No real run to send");
    cleanup();

    mount(verdict({ rejected: true }));
    await waitFor(() => expect(col("rp-exam").textContent).toContain("LEFT"));
    expect(screen.getByRole("button", { name: "APPROVE FOR PAPER" })).toBeDisabled();
  });

  it("r11 no snake_case and no em dash, windows open or not", async () => {
    fetchRetest.mockResolvedValue({ state: "could-not-run", retests: [{ ...RETEST_ROW, status: "could-not-run", could_not_run_reason: "missing-bars" }] });
    await ready();
    const text = () =>
      Array.from(document.body.querySelectorAll("*")).filter((e) => e.children.length === 0).map((e) => e.textContent).join(" ");
    const titles = () => Array.from(document.body.querySelectorAll("[title]")).map((e) => e.getAttribute("title")).join(" ");
    for (const open of ["RENAME", "DELETE", "OPEN RUNS"]) {
      expect(text() + titles()).not.toMatch(/[a-z]+_[a-z]+/);
      expect(text() + titles()).not.toContain(EM_DASH);
      press(open, screen.getByTestId(open === "OPEN RUNS" ? "rp-foot" : "rp-head"));
      expect(text() + titles()).not.toMatch(/[a-z]+_[a-z]+/);
      expect(text() + titles()).not.toContain(EM_DASH);
      press("CANCEL", dialog());
    }
  });
});
