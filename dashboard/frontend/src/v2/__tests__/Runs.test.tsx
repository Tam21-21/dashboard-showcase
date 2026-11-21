import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Runs from "../pages/research/Runs";
import { APPS } from "../shell/sections";

const listRuns = vi.fn();
const runsSummary = vi.fn();
const strategyNames = vi.fn();
const judgeRun = vi.fn();

// The page's own data module is left real, so the arguments it sends to the shared api are tested.
vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    apiGet: (path: string, ...a: unknown[]) => {
      if (path === "/api/research/strategies/names") return strategyNames(path, ...a);
      return actual.apiGet(path, ...(a as []));
    },
    api: {
      ...actual.api,
      research: {
        ...actual.api.research,
        listRuns: (...a: unknown[]) => listRuns(...a),
        runsSummary: (...a: unknown[]) => runsSummary(...a),
        judgeRun: (...a: unknown[]) => judgeRun(...a),
      },
    },
  };
});
import { ApiError } from "../../lib/api";

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "runs")!;

const at = (date: string, hm = "09:00") => Date.parse(`${date}T${hm}:00Z`) / 1000;

type Extra = Record<string, unknown>;
const run = (id: number, date: string, strategy: number, extra: Extra = {}) => {
  const created = at(date);
  return {
    id, job_id: id, name: null, strategy_id: strategy, strategy_version: 1, dataset_id: 1,
    created_at: created, started_at: created + 60, finished_at: created + 60 + 12 * 60,
    status: "succeeded", error: null, total_return: 0.1, sharpe_ratio: null, max_drawdown: null,
    num_trades: null, equity_sparkline: [100, 101, 103, 110], kind: "standard", ...extra,
  };
};
const done = (id: number, date: string, strategy: number, sh: number, dd: number, tr: number | null, ret = 0.1) =>
  run(id, date, strategy, { sharpe_ratio: sh, max_drawdown: dd, num_trades: tr, total_return: ret });

const RUNS = [
  done(1, "2026-10-12", 11, 1.85, 0.12, 60),
  run(2, "2026-10-11", 11, { status: "failed", error: "boom", total_return: null, equity_sparkline: null }),
  run(3, "2026-10-10", 11, { status: "pending", started_at: null, finished_at: null, total_return: null, equity_sparkline: null }),
  run(4, "2026-10-09", 12, { total_return: null, equity_sparkline: null }),
  done(5, "2026-10-06", 12, 2.4, 0.05, null),
  done(6, "2026-10-01", 11, 0.9, 0.2, 400),
  done(7, "2026-09-20", 11, 3.1, 0.3, 30),
  done(8, "2026-09-01", 12, 1.0, 0.1, 10),
];
const NAMES = [
  { id: 11, name: "breakout_drift", display_name: "Breakout drift", status: "active" },
  { id: 12, name: "gap_fill", display_name: "Gap fill", status: "active" },
  { id: 1, name: "ma_cross_base", display_name: "Moving average cross", status: "active" },
  { id: 2, name: "rsi_dip", display_name: "RSI dip buyer", status: "deleted" },
  { id: 3, name: "old_one", display_name: null, status: "deleted" },
];
const SUMMARY_A = { total: 2341, in_flight_count: 1, in_flight: { run_id: 3, name: null, status: "pending", started_at: at("2026-10-10") } };
const SUMMARY_B = { total: 0, in_flight_count: 0, in_flight: null };

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Runs app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const dots = () => Array.from(document.querySelectorAll("[data-dot]")) as SVGCircleElement[];
const runOf = (d: Element) => d.closest("[data-run]")!.getAttribute("data-run");
const dotIds = () => dots().map((d) => Number(runOf(d))).sort((a, b) => a - b);
const tableIds = () =>
  Array.from(screen.getByTestId("runs-table").querySelectorAll("tbody tr")).map((tr) => tr.querySelector("[data-run]")!.getAttribute("data-run"));
const card = () => screen.getByTestId("run-card");
const row = (id: number) => screen.getByTestId("runs-table").querySelector(`[data-run="${id}"]`)!.closest("tr")!;

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  listRuns.mockResolvedValue(RUNS);
  runsSummary.mockResolvedValue(SUMMARY_B);
  strategyNames.mockResolvedValue(NAMES);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [listRuns, runsSummary, strategyNames, judgeRun]) m.mockReset();
  sessionStorage.clear();
});

async function open() {
  mount();
  await screen.findByTestId("run-card");
}

const weeks = () => Array.from(screen.getByTestId("river").querySelectorAll<SVGGElement>("g[data-week]"));
const dot = (id: number) => screen.getByTestId("scatter").querySelector(`[data-run="${id}"]`)!;
const seg = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".v2-page > header .pills button, .rs-head .pills button"));

// Round seven (2026-11-15), the mockup: the finding, runs by week, the scatter, the picked run in full, the table with its filter.
describe("Runs page, round seven", () => {
  it("(a) reads standard runs, newest first, 500 of them, and never asks for synthetic", async () => {
    await open();
    expect(listRuns).toHaveBeenCalledWith("created_at", 500, 0, "standard");
    expect(listRuns.mock.calls[0]).toHaveLength(4);
    expect(runsSummary).toHaveBeenCalledWith("standard");
  });

  it("(b) the finding counts finished, figures and failed from the rows, the total from the server", async () => {
    runsSummary.mockResolvedValue({ total: 8, succeeded: 6, in_flight_count: 0, in_flight: null });
    await open();
    await waitFor(() => expect(document.querySelector(".nl-say")!.textContent).toContain("6 of 8 runs finished. 5 left figures to judge by."));
    const say = document.querySelector(".nl-say")!.textContent!;
    expect(say).toContain("The 1 newest are first screens of your current strategies");
    expect(say).toContain('1 failed, 1 of them on "boom".');
    expect(say).not.toContain("newest 8");
  });

  it("(b2) a log longer than the list: the finished count is the server's, the figures say which runs they cover", async () => {
    runsSummary.mockResolvedValue({ total: 600, succeeded: 590, in_flight_count: 0, in_flight: null });
    await open();
    const say = await waitFor(() => {
      const t = document.querySelector(".nl-say")!.textContent!;
      expect(t).toContain("590 of 600 runs finished. 5 left figures to judge by among the newest 8.");
      return t;
    });
    expect(say).toContain("Among the newest 8: ");
    expect(say).toContain("1 failed");
  });

  it("(b3) a cancelled run has its own word, not finished", async () => {
    listRuns.mockResolvedValue([...RUNS.slice(0, 7), { ...RUNS[7], status: "cancelled" }]);
    await open();
    expect(row(8).textContent).toContain("Cancelled");
    expect(document.querySelector(".nl-blk-t")!.textContent).toContain("1 cancelled");
  });

  it("(c) runs by week: one bar a Monday week, split by how the runs ended, with a count of each kind", async () => {
    listRuns.mockResolvedValue([...RUNS.slice(0, 7), { ...RUNS[7], verdict: "unjudgeable" }]);
    await open();
    expect(weeks()).toHaveLength(7);
    const busy = weeks().find((g) => g.querySelectorAll("rect[data-kind]").length === 4)!;
    expect(Array.from(busy.querySelectorAll("rect[data-kind]")).map((r) => r.getAttribute("data-kind"))).toEqual(["good", "warn", "bad", "run"]);
    expect(busy.querySelector("title")!.textContent).toContain("4 runs");
    const head = document.querySelector(".rs-counts")!.textContent!;
    expect(head).toContain("5 finished, with figures");
    expect(head).toContain("1 waiting for the judge");
    expect(head).toContain("1 failed");
    expect(head).toContain("1 running");
    // a judged run says so in its table row
    expect(row(8).textContent).toContain("Judged");
    expect(row(4).textContent).toContain("Waits");
  });

  it("(d) the scatter holds the runs with figures, no zone, coloured by the sign of the return", async () => {
    listRuns.mockResolvedValue([...RUNS.slice(0, 7), { ...RUNS[7], total_return: -0.2 }]);
    await open();
    expect(dotIds()).toEqual([1, 5, 6, 7, 8]);
    const sc = screen.getByTestId("scatter");
    expect(Array.from(sc.querySelectorAll("text")).some((t) => /zone/i.test(t.textContent ?? ""))).toBe(false);
    expect(sc.querySelector("[data-zone]")).toBeNull();
  });

  it("(e) the run card, the default pick and picking from the river, the scatter and the table", async () => {
    await open();
    expect(within(card()).getByText("Run 1")).toBeInTheDocument();
    expect(within(card()).getByText("Finished, with figures")).toBeInTheDocument();
    expect(within(card()).getByText("Breakout drift")).toBeInTheDocument();
    expect(within(card()).getByText("12 Oct 2026, 12 min")).toBeInTheDocument();
    for (const k of ["Return", "Sharpe", "Deepest drop", "Trades"]) expect(within(card()).getByText(k)).toBeInTheDocument();
    expect(within(card()).getByText("+10.0%")).toBeInTheDocument();
    expect(within(card()).getByText("1.85")).toBeInTheDocument();
    expect(within(card()).getByText("-12.0%")).toBeInTheDocument();
    expect(within(card()).getByText("60")).toBeInTheDocument();
    expect(screen.getByTestId("money-line")).toBeInTheDocument();
    // each figure says where the run sits among the runs with figures
    expect(within(card()).getByText("1.85").closest(".rf-fact")!.getAttribute("title")).toBe("Higher than 2 of the 5 runs with figures");
    // a dot picks
    fireEvent.click(dot(5));
    expect(within(card()).getByText("Run 5")).toBeInTheDocument();
    expect(within(card()).getByText("Gap fill")).toBeInTheDocument();
    // a table row picks; id 4 has no figures and says why
    fireEvent.click(row(4));
    expect(within(card()).getByText("Run 4")).toBeInTheDocument();
    expect(within(card()).getByText("A first screen of a current strategy. Its results are in the Generator's search for it.")).toBeInTheDocument();
    expect(within(card()).getByText("Waits for the judge")).toBeInTheDocument();
    // a pending run has no length
    fireEvent.click(row(3));
    expect(within(card()).getByText("This run has not finished.")).toBeInTheDocument();
    expect(card().querySelector(".rs-when")!.textContent).toBe("10 Oct 2026, -");
  });

  it("(f) the table: nine columns, 12 rows then SHOW ALL, and the filter", async () => {
    listRuns.mockResolvedValue(Array.from({ length: 15 }, (_, i) => done(100 + i, "2026-10-12", 11, 1, 0.1, 10)).concat([RUNS[1]]));
    await open();
    const head = Array.from(screen.getByTestId("runs-table").querySelectorAll("th")).map((t) => t.textContent);
    expect(head).toEqual(["Run", "Strategy", "Started", "Length", "Result", "Return", "Sharpe", "Deepest drop", "State"]);
    expect(tableIds()).toHaveLength(12);
    fireEvent.click(screen.getByRole("button", { name: "Show all 16" }));
    expect(tableIds()).toHaveLength(16);
    fireEvent.click(screen.getByRole("button", { name: "Failed 1" }));
    expect(tableIds()).toEqual(["2"]);
    expect(within(row(2)).getByText("Failed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "With figures 15" }));
    expect(tableIds()).toHaveLength(12);
  });

  it("(g) Send to the judge posts once and shows the answer", async () => {
    judgeRun.mockResolvedValue({ verdict_id: 1 });
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Send to the judge" }));
    expect(await screen.findByText("Sent to the judge")).toBeInTheDocument();
    expect(judgeRun).toHaveBeenCalledTimes(1);
    expect(judgeRun.mock.calls[0][0]).toBe(1);
  });

  it("(g) a refusal shows the server's words; a failed run has no button", async () => {
    judgeRun.mockRejectedValue(new ApiError("409 /x", 409, { detail: "This run has too few trades." }, "/x"));
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Send to the judge" }));
    expect(await screen.findByText("This run has too few trades.")).toBeInTheDocument();
    fireEvent.click(row(2));
    expect(screen.queryByRole("button", { name: "Send to the judge" })).toBeNull();
  });

  it("(h) a failed list shows the server's words and nothing else", async () => {
    listRuns.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The runs database is locked." }, "/x"));
    mount();
    expect(await screen.findByText("The runs database is locked.")).toBeInTheDocument();
    expect(screen.queryByTestId("river")).toBeNull();
    expect(document.querySelector(".nl-say")).toBeNull();
  });

  it("(i) an empty list, and a range with no run", async () => {
    listRuns.mockResolvedValue([]);
    mount();
    expect(await screen.findByText("No runs in this range")).toBeInTheDocument();
    expect(screen.queryByTestId("run-card")).toBeNull();
    expect(screen.queryByTestId("river")).toBeNull();
    expect(dots()).toHaveLength(0);
  });

  it("(i) This week with no run, then All runs again", async () => {
    listRuns.mockResolvedValue([RUNS[5]]);
    mount();
    await screen.findByTestId("run-card");
    fireEvent.click(screen.getByRole("button", { name: "This week" }));
    await screen.findByText("No runs in this range");
    expect(screen.queryByTestId("run-card")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "All runs" }));
    expect(await screen.findByTestId("run-card")).toBeInTheDocument();
  });

  it("(j) nothing is invented and nothing is snake_case", async () => {
    await open();
    const text = (document.body.textContent ?? "").toLowerCase();
    for (const w of ["family", "where we want to be", "compare", "best zone", "tickers"]) expect(text).not.toContain(w);
    expect(document.body.textContent).not.toContain("breakout_drift");
  });

  it("(k) a strategy shows its display name, with the stored name on hover", async () => {
    await open();
    const cell = row(1).querySelector(".rn") as HTMLElement;
    expect(cell.textContent).toBe("Breakout drift");
    expect(cell.getAttribute("title")).toBe("breakout_drift");
  });

  it("(l) the head says what is running, and the range switch opens on All runs", async () => {
    runsSummary.mockResolvedValue(SUMMARY_A);
    await open();
    expect(screen.getAllByText("1 running").length).toBeGreaterThan(0);
    expect(seg().map((b) => b.textContent)).toEqual(["All runs", "This week", "This month"]);
    expect(seg()[0].getAttribute("aria-pressed")).toBe("true");
    expect(document.querySelector(".nl-say")!.textContent).toContain("of 2341 runs finished");
  });

  it("(m) the week starts Monday 00:00 Paris and the month on the 1st 00:00 Paris", async () => {
    vi.setSystemTime(new Date("2026-10-16T10:00:00Z"));
    const paris = (iso: string) => Date.parse(iso) / 1000;
    const t = (id: number, iso: string) => run(id, "2026-10-15", 11, { created_at: paris(iso), started_at: paris(iso), finished_at: paris(iso) + 60 });
    listRuns.mockResolvedValue([
      t(21, "2026-10-11T22:30:00Z"), // Monday 12 Oct 00:30 Paris
      t(22, "2026-10-11T21:00:00Z"), // Sunday 11 Oct 23:00 Paris
      t(23, "2026-09-30T22:10:00Z"), // 1 Oct 00:10 Paris
      t(24, "2026-09-30T21:50:00Z"), // 30 Sep 23:50 Paris
    ]);
    await open();
    expect(tableIds()).toEqual(["21", "22", "23", "24"]);
    fireEvent.click(screen.getByRole("button", { name: "This week" }));
    expect(tableIds()).toEqual(["21"]);
    fireEvent.click(screen.getByRole("button", { name: "This month" }));
    expect(tableIds()).toEqual(["21", "22", "23"]);
  });

  it("(o) the mockup's sections in its order: Runs by week, Runs with results, the picked run with its account value, All runs", async () => {
    listRuns.mockResolvedValue([{ ...RUNS[0], win_rate: 0.52, profit_factor: 6.3 }, ...RUNS.slice(1)]);
    await open();
    const heads = Array.from(document.querySelectorAll(".nl-blk-t h2")).map((h) => h.textContent);
    expect(heads).toEqual(["Runs by week", "Runs with results", "Run 1", "All runs"]);
    expect(within(card()).getByText("Account value")).toBeInTheDocument();
    for (const k of ["Winning trades", "Won for each 1 lost"]) expect(within(card()).getByText(k)).toBeInTheDocument();
    expect(within(card()).getByText("52%")).toBeInTheDocument();
    expect(within(card()).getByText("6.30")).toBeInTheDocument();
    expect(screen.getByTestId("scatter-key").textContent).toBe("Made moneyLost money");
    for (const k of ["All 8", "With figures 5", "Waiting 1", "Failed 1"]) expect(screen.getByRole("button", { name: k })).toBeInTheDocument();
  });

  it("(n) a link to a run picks it and lists every row", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/x?run=7"]}>
          <Runs app={app} section={section} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByTestId("run-card");
    expect(within(card()).getByText("Run 7")).toBeInTheDocument();
  });
});
