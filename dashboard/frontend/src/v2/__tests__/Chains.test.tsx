import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Chains from "../pages/research/Chains";
import { LIVE_EVENTS } from "../shell/live";
import { APPS, pageFor } from "../shell/sections";

const chains = vi.fn();
const chain = vi.fn();
const createChain = vi.fn();
const chooseChainCell = vi.fn();
const stopChain = vi.fn();
const retryChain = vi.fn();
const getRunCells = vi.fn();
const listStrategies = vi.fn();
const getStrategyStatus = vi.fn();
const apiGet = vi.fn();

// The page's own data module is left real, so the calls it makes on the shared api are tested.
vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    apiGet: (...a: unknown[]) => apiGet(...a),
    api: {
      ...actual.api,
      research: {
        ...actual.api.research,
        chains: (...a: unknown[]) => chains(...a),
        chain: (...a: unknown[]) => chain(...a),
        createChain: (...a: unknown[]) => createChain(...a),
        chooseChainCell: (...a: unknown[]) => chooseChainCell(...a),
        stopChain: (...a: unknown[]) => stopChain(...a),
        retryChain: (...a: unknown[]) => retryChain(...a),
        getRunCells: (...a: unknown[]) => getRunCells(...a),
        listStrategies: (...a: unknown[]) => listStrategies(...a),
        getStrategyStatus: (...a: unknown[]) => getStrategyStatus(...a),
      },
    },
  };
});
import { ApiError } from "../../lib/api";

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "chains")!;

const ts = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const RESERVE_CUT = 1672531200; // 2023-01-01
const EM_DASH = String.fromCharCode(8212);

type Extra = Record<string, unknown>;
const WORDS: Record<string, [string, string, string]> = {
  screening: ["SCREENING", "active", "screen"],
  optimising: ["OPTIMISING", "active", "optimise"],
  "walking-forward": ["WALKING FORWARD", "active", "walkforward"],
  "choosing-cell": ["CHOOSE A CELL", "waiting-on-you", "choose"],
  done: ["DONE", "ended", "judge"],
  refused: ["REFUSED", "retryable", "walkforward"],
  failed: ["FAILED", "retryable", "replay"],
  replaying: ["REPLAYING", "active", "replay"],
};
const mk = (id: number, strategy: number, name: string | null, state: string, extra: Extra = {}) => ({
  id, strategy_id: strategy, strategy_version: 1, calendar_id: `chain-${id}`,
  screen_start_ts: ts("2004-01-01"), optimise_start_ts: ts("2009-01-01"),
  walkforward_start_ts: ts("2014-01-01"), walkforward_end_ts: ts("2018-01-01"),
  state, state_word: WORDS[state][0], group: WORDS[state][1], step: WORDS[state][2],
  ended_at_step: null, stop_requested: 0, note: null, note_word: null,
  reason_word: null, reason_sentence: null, display_name: name,
  created_at: ts("2026-10-20"), updated_at: Date.parse("2026-10-23T09:00:00Z") / 1000, duel_chain_id: null, ...extra,
});
const REFUSED_SENTENCE = "The walk forward window is too short for the walk forward settings.";
const CHAINS = [
  mk(9, 19, "Pull back", "replaying", { stop_requested: 1, note: "stop-requested", note_word: "STOPPING", chosen_variation_id: 14, chosen_ticker: "AAPL" }),
  mk(8, 11, "Breakout drift", "screening"),
  mk(7, 12, "Gap fill", "walking-forward", { screen_start_ts: ts("2006-01-01") }),
  mk(6, 14, "Trend follow", "optimising"),
  mk(5, 15, "Liquidity gap", "choosing-cell", { updated_at: Date.parse("2026-10-23T09:00:00Z") / 1000 }),
  mk(4, 16, "Mean revert", "done"),
  mk(3, 13, null, "done"),
  mk(2, 17, "Gap fade", "refused", { ended_at_step: "walkforward", reason_word: "walkforward-window-too-small", reason_sentence: REFUSED_SENTENCE }),
  mk(1, 18, "Volatility squeeze", "failed", { ended_at_step: "replay", reason_word: "replay-failed", reason_sentence: "The replay of the chosen cell failed." }),
];
const LIST = { chains: CHAINS, reserve_cut_ts: RESERVE_CUT };

const trans = (id: number, from: string | null, to: string, actor: string, iso: string) => ({
  id, chain_id: 5, from_state: from, to_state: to, actor, created_at: Date.parse(iso) / 1000,
  reason_word: null, reason_sentence: null, to_word: WORDS[to]?.[0] ?? to.toUpperCase(),
});
const DETAIL5 = {
  ...CHAINS[4], optimise_run_id: 50, walkforward_run_id: null,
  transitions: [
    trans(1, null, "waiting", "page", "2026-10-23T09:00:00Z"),
    trans(2, "waiting", "screening", "runner", "2026-10-23T09:05:00Z"),
    trans(3, "screening", "optimising", "runner", "2026-10-23T09:40:00Z"),
    trans(4, "optimising", "choosing-cell", "runner", "2026-10-23T11:00:00Z"),
  ],
};
const cell = (variation_id: number, ticker: string, sharpe: number | null) => ({
  variation_id, ticker, param_json: null, total_return: 0.31, sharpe_ratio: sharpe, max_drawdown: -0.18, num_trades: 61,
  win_rate: 0.56, total_fees_paid: 12.5, total_slippage_cost: 4.25, profit_factor: 1.4, skew: null, kurtosis: null,
});
// 25 cells: v14 AAPL is the best (1.42), one cell has no Sharpe, the rest fall from 1.30 by 0.01 a cell.
const CELLS25 = {
  run_id: 50, cell_count: 25, member_count: 5, variation_count: 5, summary: {},
  cells: [
    cell(30, "ZZZ", null),
    ...Array.from({ length: 23 }, (_, i) => cell(100 + i, "MSFT", 1.3 - i * 0.01)),
    cell(14, "AAPL", 1.42),
  ],
};
const detailFor = (c: (typeof CHAINS)[number]) => ({
  ...c, optimise_run_id: c.id === 5 || c.id === 9 ? 50 : null, walkforward_run_id: null,
  transitions: c.id === 5 ? DETAIL5.transitions : [],
});
const STRATEGIES = [
  { id: 11, name: "breakout_drift", display_name: "Breakout drift", version: 1, description: null },
  { id: 12, name: "gap_fill", display_name: "Gap fill", version: 2, description: null },
];
const STATUS = { sweep_params: [{ name: "fast_window", type: "int" }, { name: "entry_band", type: "float" }] };
const SUPPLY = {
  enumerated: 2, spent: 1,
  windows: [
    { index: 1, name: "2023-01-01 to 2023-07-01", start_ts: ts("2023-01-01"), end_ts: ts("2023-07-01"), spent: true },
    { index: 2, name: "2023-07-01 to 2024-01-01", start_ts: ts("2023-07-01"), end_ts: ts("2024-01-01"), spent: false },
  ],
};


function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Chains app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const open = async () => {
  mount();
  await waitFor(() => expect(screen.getByTestId("chain-list").querySelector("[data-chain]")).not.toBeNull());
};
const item = (id: number) => screen.getByTestId("chain-list").querySelector(`[data-chain="${id}"]`) as HTMLElement;
const itemIds = () => Array.from(screen.getByTestId("chain-list").querySelectorAll("[data-chain]")).map((b) => Number(b.getAttribute("data-chain")));
const pickChain = async (id: number) => {
  fireEvent.click(item(id));
  await waitFor(() => expect(chain).toHaveBeenCalledWith(id));
};
const marks = () => Array.from(screen.getByTestId("chain-marks").querySelectorAll("[data-mark]"));
const dialog = (name: string) => screen.getByRole("dialog", { name });

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-12T12:00:00Z"));
  chains.mockResolvedValue(LIST);
  chain.mockImplementation((id: number) => Promise.resolve(detailFor(CHAINS.find((c) => c.id === id)!)));
  getRunCells.mockResolvedValue(CELLS25);
  listStrategies.mockResolvedValue(STRATEGIES);
  getStrategyStatus.mockResolvedValue(STATUS);
  createChain.mockResolvedValue({ status: "chain-started", chain_id: 9, duel_chain_id: null });
  chooseChainCell.mockResolvedValue({ status: "cell-chosen", chain_id: 5 });
  stopChain.mockResolvedValue({ status: "chain-stop-requested", chain_id: 7 });
  retryChain.mockResolvedValue({ status: "chain-retry-accepted", chain_id: 2 });
  apiGet.mockImplementation((path: string) => (path === "/api/research/reserve-exam/supply"
    ? Promise.resolve(SUPPLY)
    : path.endsWith("/batches") ? Promise.resolve([{ id: 1, data_start: "2009-01-01" }])
      : Promise.reject(new Error(`no fixture for ${path}`))));
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  for (const m of [chains, chain, createChain, chooseChainCell, stopChain, retryChain, getRunCells, listStrategies, getStrategyStatus, apiGet]) m.mockReset();
  sessionStorage.clear();
});

describe("Chain page", () => {
  it("(c1) the rail and the header read Chain and the picked chain holds the stage colour key", async () => {
    expect(app.sections.map((s) => s.label).slice(4, 6)).toEqual(["Runs", "Chain"]);
    expect(section.id).toBe("chains");
    expect(pageFor(app, section)).toBe(Chains);
    await open();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Chain");
    const legend = document.querySelector(".lgd")!;
    expect(Array.from(legend.querySelectorAll("span")).map((s) => s.textContent)).toEqual(["Screen", "Optimise", "Walk-forward", "Later"]);
  });

  it("(c2) the group chips count every group and filter the list", async () => {
    await open();
    for (const name of ["All 9", "Running 4", "Waiting on you 1", "Can retry 2", "Ended 2"]) expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(itemIds()).toEqual([9, 8, 7, 6, 5, 4, 3, 2, 1]);
    fireEvent.click(screen.getByRole("button", { name: "Can retry 2" }));
    expect(itemIds()).toEqual([2, 1]);
    fireEvent.click(screen.getByRole("button", { name: "Running 4" }));
    expect(itemIds()).toEqual([9, 8, 7, 6]);
  });

  it("(c3) each chain shows its name, state word, note word and step title", async () => {
    await open();
    const nine = within(item(9).closest("tr") as HTMLElement);
    expect(nine.getByText("Pull back")).toBeInTheDocument();
    expect(nine.getByText("Replay")).toBeInTheDocument();
    expect(nine.getByText("REPLAYING")).toBeInTheDocument();
    expect(nine.getByText("STOPPING")).toBeInTheDocument();
    expect(item(9).getAttribute("title")).toBe("Step 4 of 6, REPLAY");
    expect(within(item(3).closest("tr") as HTMLElement).getByText("Unnamed strategy").getAttribute("title")).toBe("This strategy has no display name");
    expect(item(7).getAttribute("title")).toBe("Step 5 of 6, WALK-FORWARD");
  });

  it("(c4) six step marks: done before the step, on at it, ahead after", async () => {
    await open();
    await pickChain(7);
    expect(marks().map((m) => m.getAttribute("data-mark"))).toEqual(["done", "done", "done", "done", "on", "ahead"]);
    expect(marks()[4].getAttribute("data-tone")).toBe("bl");
    expect(marks()[0].getAttribute("title")).toBe("Screen: done");
    expect(marks()[4].getAttribute("title")).toBe("Walk-forward: walking forward");
    expect(marks()[5].getAttribute("title")).toBe("Judge: not reached");
    await pickChain(4);
    expect(marks().map((m) => m.getAttribute("data-mark"))).toEqual(Array(6).fill("done"));
    await pickChain(1);
    expect(marks().map((m) => m.getAttribute("data-mark"))).toEqual(["done", "done", "done", "on", "ahead", "ahead"]);
    expect(marks()[3].getAttribute("data-tone")).toBe("rd");
  });

  it("(c5) the years band draws the three stretches, the reserve cut and the year marks", async () => {
    await open();
    await pickChain(8);
    const band = screen.getByTestId("chain-years");
    expect(Array.from(band.querySelectorAll("[data-stretch]")).map((s) => s.getAttribute("title"))).toEqual([
      "Screen: 01/01/2004 to 01/01/2009",
      "Optimise: 01/01/2009 to 01/01/2014",
      "Walk-forward: 01/01/2014 to 01/01/2018",
    ]);
    expect(band.querySelector("[data-cut]")!.getAttribute("title")).toBe("Reserve from 01/01/2023");
    expect(Array.from(band.querySelectorAll("[data-tick]")).map((t) => t.textContent)).toEqual(["2000", "2005", "2010", "2015", "2020"]);
  });

  it("(c6) the story lists the steps newest first with the date and who did it", async () => {
    await open();
    await pickChain(5);
    const story = screen.getByTestId("chain-story");
    await waitFor(() => expect(within(story).getByText("CHOOSE A CELL")).toBeInTheDocument());
    const rows = Array.from(story.querySelectorAll("[data-transition]"));
    expect(rows.map((r) => r.querySelector("b")!.textContent)).toEqual(["CHOOSE A CELL", "OPTIMISING", "SCREENING", "WAITING"]);
    expect(rows[0].textContent).toContain("23/10/2026");
    expect(rows[0].querySelector("[data-date]")!.getAttribute("title")).toBe("23/10/2026 13:00");
    expect(rows.map((r) => r.textContent!.includes("YOU"))).toEqual([false, false, false, true]);
  });

  it("(c7) the ending, its sentence and the buttons that fit it", async () => {
    await open();
    const ending = () => screen.getByTestId("chain-ending").textContent;
    const buttons = () => Array.from(screen.getByTestId("chain-actions").querySelectorAll("button")).map((b) => b.textContent);
    await pickChain(1);
    expect(ending()).toContain("FAILED AT REPLAY");
    expect(await screen.findByText("The replay of the chosen cell failed.")).toBeInTheDocument();
    expect(buttons()).toEqual(["OPEN RUN", "RETRY"]);
    await pickChain(2);
    expect(ending()).toContain("REFUSED AT WALK-FORWARD");
    expect(buttons()).toEqual(["OPEN RUN", "RETRY"]);
    await pickChain(4);
    expect(ending()).toContain("DONE");
    expect(buttons()).toEqual(["OPEN RUN"]);
    await pickChain(5);
    expect(ending()).toContain("WAITING ON YOU");
    expect(buttons()).toEqual(["OPEN RUN", "STOP"]);
    await pickChain(7);
    expect(ending()).toContain("STILL MOVING");
    expect(buttons()).toEqual(["OPEN RUN", "STOP"]);
    await pickChain(9);
    expect(within(screen.getByTestId("chain-ending")).getByText("STOPPING")).toBeInTheDocument();
    const stop = within(screen.getByTestId("chain-actions")).getByRole("button", { name: "STOP" });
    expect(stop).toBeDisabled();
    expect(stop.getAttribute("title")).toBe("A stop is already asked for");
  });

  it("(c8) the cells: 20 best first, SHOW ALL, every metric, dashes with a reason, CHOSEN", async () => {
    await open();
    await pickChain(5);
    const table = await screen.findByTestId("chain-cells");
    expect(getRunCells).toHaveBeenCalledWith(50);
    const head = Array.from(table.querySelectorAll("th")).map((h) => h.textContent);
    expect(head).toEqual(["Stock", "Variation", "Sharpe", "Return", "Deepest drop", "Trades", "Win rate", "Fees", "Slippage", ""]);
    const rows = () => Array.from(table.querySelectorAll("tbody tr"));
    expect(rows()).toHaveLength(20);
    expect(rows()[0].textContent).toContain("AAPL");
    expect(rows()[0].textContent).toContain("v14");
    expect(rows()[0].textContent).toContain("1.42");
    expect(rows()[0].textContent).toContain("31%");
    expect(rows()[0].textContent).toContain("-18%");
    expect(rows()[0].textContent).toContain("56%");
    expect(rows()[0].textContent).toContain("12.50");
    expect(rows()[0].textContent).toContain("4.25");
    expect(within(rows()[0] as HTMLElement).getByRole("button", { name: "CHOOSE" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "SHOW ALL 25" }));
    expect(rows()).toHaveLength(25);
    const last = rows()[24] as HTMLElement;
    expect(last.textContent).toContain("ZZZ");
    const dash = within(last).getByTitle("No sharpe was saved for this cell");
    expect(dash.textContent).toBe("-");
    expect(screen.getByTestId("chain-cells-title").textContent).toContain("25");

    await pickChain(9);
    const done = await screen.findByTestId("chain-cells");
    await waitFor(() => expect(within(done).getByText("CHOSEN")).toBeInTheDocument());
    expect(within(within(done).getByText("CHOSEN").closest("tr") as HTMLElement).getByText("AAPL")).toBeInTheDocument();
    expect(within(done).queryByRole("button", { name: "CHOOSE" })).toBeNull();
  });

  it("(c8) no run and no cells are worded", async () => {
    await open();
    await pickChain(7);
    const figure = () => within(screen.getByTestId("chain-cells-panel"));
    expect(figure().getByTitle("This chain has no optimise run yet").textContent).toBe("-");
    getRunCells.mockResolvedValue({ ...CELLS25, cell_count: 0, cells: [] });
    await pickChain(5);
    await waitFor(() => expect(figure().getByTitle("The optimise run saved no cells").textContent).toBe("-"));
  });

  it("(c9) CHOOSE opens a window and its CHOOSE, pressed twice, sends once", async () => {
    let release: (v: unknown) => void = () => {};
    chooseChainCell.mockImplementation(() => new Promise((res) => { release = res; }));
    await open();
    await pickChain(5);
    const table = await screen.findByTestId("chain-cells");
    fireEvent.click(within(table.querySelector("tbody tr") as HTMLElement).getByRole("button", { name: "CHOOSE" }));
    const win = dialog("Choose this cell");
    expect(win.textContent).toContain("AAPL");
    expect(win.textContent).toContain("v14");
    expect(win.textContent).toContain("1.42");
    const verb = within(win).getByRole("button", { name: "CHOOSE" });
    fireEvent.click(verb);
    fireEvent.click(verb);
    expect(chooseChainCell).toHaveBeenCalledTimes(1);
    expect(chooseChainCell).toHaveBeenCalledWith(5, { variation_id: 14, ticker: "AAPL" });
    expect(verb).toBeDisabled();
    release({ status: "cell-chosen", chain_id: 5 });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose this cell" })).toBeNull());
    expect(await screen.findByText("Cell chosen.")).toBeInTheDocument();
  });

  it("(c10) STOP and RETRY each open a window and call once; a refusal shows in the window", async () => {
    await open();
    await pickChain(2);
    fireEvent.click(within(screen.getByTestId("chain-actions")).getByRole("button", { name: "RETRY" }));
    const retryWin = dialog("Retry the chain");
    fireEvent.click(within(retryWin).getByRole("button", { name: "RETRY" }));
    await waitFor(() => expect(retryChain).toHaveBeenCalledTimes(1));
    expect(retryChain).toHaveBeenCalledWith(2);
    expect(await screen.findByText("Retry accepted.")).toBeInTheDocument();

    await pickChain(7);
    stopChain.mockRejectedValueOnce(new ApiError("409 x - /api/research/chains/7/stop", 409, { detail: "chain-already-ended: chain 7 is already 'done'" }));
    fireEvent.click(within(screen.getByTestId("chain-actions")).getByRole("button", { name: "STOP" }));
    const stopWin = dialog("Stop the chain");
    const verb = within(stopWin).getByRole("button", { name: "STOP" });
    fireEvent.click(verb);
    fireEvent.click(verb);
    await waitFor(() => expect(stopChain).toHaveBeenCalledTimes(1));
    expect(stopChain).toHaveBeenCalledWith(7);
    expect(await within(dialog("Stop the chain")).findByText("Chain 7 is already 'done'")).toBeInTheDocument();
    expect(screen.queryByText(/chain-already-ended/)).toBeNull();
    fireEvent.click(within(dialog("Stop the chain")).getByRole("button", { name: "CANCEL" }));
    expect(screen.queryByRole("dialog", { name: "Stop the chain" })).toBeNull();
  });

  it("(c11) START opens a window that posts the strategy, four epoch dates and a cast grid once", async () => {
    vi.setSystemTime(new Date("2015-06-12T12:00:00Z")); // the date picker opens on the current month
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Start a chain" }));
    const win = dialog("Start a chain");
    await within(win).findByRole("option", { name: "Breakout drift" });
    fireEvent.change(within(win).getByLabelText("Strategy"), { target: { value: "11" } });
    await within(win).findByLabelText("Fast window");
    expect(getStrategyStatus).toHaveBeenCalledWith(11);
    const pick = (label: string, monthsBack: number, day: number) => {
      fireEvent.click(within(win).getByRole("button", { name: label }));
      for (let i = 0; i < monthsBack; i++) fireEvent.click(screen.getByRole("button", { name: "Previous month" }));
      const pop = screen.getByRole("dialog", { name: "Choose a date" });
      const button = Array.from(pop.querySelectorAll(".dpg button")).find((b) => !b.classList.contains("out") && b.textContent === String(day))!;
      fireEvent.click(button);
    };
    pick("Screen from", 5, 2);
    pick("Optimise from", 4, 2);
    pick("Walk forward from", 3, 2);
    pick("Walk forward to", 2, 1);
    expect(within(win).getByText("Walk forward to").getAttribute("title")).toMatch(/^Must end before the reserve cut, \d\d\/\d\d\/\d{4}$/);
    fireEvent.change(within(win).getByLabelText("Fast window"), { target: { value: "10, 20,30" } });
    fireEvent.change(within(win).getByLabelText("Entry band"), { target: { value: "1.5, 2" } });
    const verb = within(win).getByRole("button", { name: "START THE CHAIN" });
    fireEvent.click(verb);
    fireEvent.click(verb);
    await waitFor(() => expect(createChain).toHaveBeenCalledTimes(1));
    expect(createChain).toHaveBeenCalledWith({
      strategy_id: 11,
      screen_start_ts: 1420156800,
      optimise_start_ts: ts("2015-02-02"),
      walkforward_start_ts: ts("2015-03-02"),
      walkforward_end_ts: ts("2015-04-01"),
      param_grid: { fast_window: [10, 20, 30], entry_band: [1.5, 2] },
    });
    expect(await screen.findByText("Chain started.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Start a chain" })).toBeNull();
  }, 30000); // four date pickers are driven by clicks; the full suite runs this beside every other page test

  it("(c11) a value that is not a number sends nothing", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Start a chain" }));
    const win = dialog("Start a chain");
    await within(win).findByRole("option", { name: "Breakout drift" });
    fireEvent.change(within(win).getByLabelText("Strategy"), { target: { value: "11" } });
    fireEvent.change(await within(win).findByLabelText("Fast window"), { target: { value: "ten" } });
    fireEvent.click(within(win).getByRole("button", { name: "START THE CHAIN" }));
    expect(createChain).not.toHaveBeenCalled();
    expect(within(win).getByRole("status")).toBeInTheDocument();
  });

  it("(c11) no strategies: one sentence, no select, START THE CHAIN disabled with the reason", async () => {
    listStrategies.mockResolvedValue([]);
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Start a chain" }));
    const win = dialog("Start a chain");
    expect(await within(win).findByText("There are no strategies to start a chain from.")).toBeInTheDocument();
    expect(within(win).queryByLabelText("Strategy")).toBeNull();
    const verb = within(win).getByRole("button", { name: "START THE CHAIN" });
    expect(verb).toBeDisabled();
    expect(verb.getAttribute("title")).toBe("There are no strategies to start a chain from.");
  });

  it("(c12) FIRST AGAINST LAST is a dash with the reason on hover", async () => {
    await open();
    const panel = screen.getByTestId("chain-duel");
    expect(panel.textContent).toContain("FIRST AGAINST LAST");
    expect(within(panel).getByText("-").getAttribute("title")).toBe("The server does not pair a chain's first and last equity curves yet");
    expect(within(panel).getByText("FIRST AGAINST LAST").getAttribute("title")).toBe("The server does not pair a chain's first and last equity curves yet");
  });

  it("(c12) an empty store shows every chip at 0, the words and START", async () => {
    chains.mockResolvedValue({ chains: [], reserve_cut_ts: RESERVE_CUT });
    mount();
    expect(screen.getByText("Loading the chains.")).toBeInTheDocument();
    expect(await screen.findByText("No chains yet. Start one and it shows here.")).toBeInTheDocument();
    for (const name of ["All 0", "Running 0", "Waiting on you 0", "Can retry 0", "Ended 0"]) expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Start a chain" })).toHaveLength(2);
  });

  it("(c12) a failed read shows the server's detail", async () => {
    chains.mockRejectedValue(new ApiError("503 x - /api/research/chains", 503, { detail: "reserve-cut-owed: the reserve cut date has no value yet" }));
    mount();
    expect(await screen.findByText("The reserve cut date has no value yet")).toBeInTheDocument();
  });

  it("(c12) the page holds no code word and no em dash, and the live event maps to the chains", async () => {
    await open();
    await pickChain(5);
    await screen.findByTestId("chain-cells");
    fireEvent.click(screen.getByRole("button", { name: "Start a chain" }));
    await within(dialog("Start a chain")).findByRole("option", { name: "Breakout drift" });
    fireEvent.change(within(dialog("Start a chain")).getByLabelText("Strategy"), { target: { value: "11" } });
    await within(dialog("Start a chain")).findByLabelText("Fast window");
    const text = document.body.textContent!;
    expect(text).not.toMatch(/[a-z]+_[a-z]+/);
    expect(text).not.toContain(EM_DASH);
    expect(LIVE_EVENTS["research.chain_changed"]).toEqual([["chains"]]);
  });

  it("(r1) the finding: how many chains, who waits, how many strategies declare settings", async () => {
    await open();
    const say = await waitFor(() => {
      const el = document.querySelector(".nl-say") as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(say.querySelector("b")!.textContent).toBe("9 chains. 1 waits for you.");
    expect(say.querySelector(".more")!.textContent).toBe(
      "A chain takes one strategy through six steps by itself and stops once, at the step where you pick which result to carry on with. 2 of your 2 strategies list settings to try.",
    );
    expect(say.querySelector("em.warn")!.textContent).toBe("you pick which result to carry on with");
  });

  it("(r2) no chain yet, and no strategy declares settings", async () => {
    chains.mockResolvedValue({ chains: [], reserve_cut_ts: RESERVE_CUT });
    getStrategyStatus.mockResolvedValue({ sweep_params: [] });
    mount();
    const say = await waitFor(() => {
      const el = document.querySelector(".nl-say") as HTMLElement;
      expect(el).not.toBeNull();
      return el;
    });
    expect(say.querySelector("b")!.textContent).toBe("No chain has run yet.");
    expect(say.textContent).toContain("None of your 2 strategies lists settings for a chain to try.");
    expect(document.querySelector(".cn-pair .cn-none")).not.toBeNull();
  });

  it("(m1) the mockup's sections: the finding, Chain steps, Years used by a chain, All chains, and the Start a chain button", async () => {
    await open();
    const heads = Array.from(document.querySelectorAll(".nl-blk-t h2")).map((h) => h.textContent);
    expect(heads.slice(0, 3)).toEqual(["Chain steps", "Years used by a chain", "All chains"]);
    expect(screen.getAllByRole("button", { name: "Start a chain" })).toHaveLength(1);
    const table = screen.getByTestId("chain-list").querySelector("table")!;
    expect(Array.from(table.querySelectorAll("th")).map((t) => t.textContent)).toEqual(["Chain", "Step", "State"]);
  });

  it("(r3) the six steps: who moves each on and how many chains stand at it now", async () => {
    await open();
    const steps = Array.from(screen.getByTestId("chain-steps").querySelectorAll("[data-step]"));
    expect(steps.map((s) => s.querySelector(".cn-nm")!.textContent)).toEqual(["1. Screen", "2. Optimise", "3. Choose", "4. Replay", "5. Walk-forward", "6. Judge"]);
    expect(steps.map((s) => s.querySelector(".cn-big")!.textContent)).toEqual(["1", "1", "1", "2", "2", "0"]);
    expect(steps.map((s) => s.querySelector(".nl-st")!.textContent)).toEqual(["Runs by itself", "Runs by itself", "Waits for you", "Runs by itself", "Runs by itself", "Runs by itself"]);
    expect(steps[2].className).toContain("you");
  });

  it("(r5) while the exam stretches load the years say so; a failed searches read says why the years are missing", async () => {
    apiGet.mockImplementation((path: string) => (path === "/api/research/reserve-exam/supply"
      ? new Promise(() => {})
      : Promise.reject(new ApiError("500 x", 500, { detail: "The batches cannot be read" }))));
    await open();
    await waitFor(() => expect(document.querySelector(".cn-facts")!.textContent).toContain("Loading"));
    expect(document.querySelector(".cn-facts")!.textContent).not.toContain("None set");
    expect(await screen.findByText(/The generator's searches could not be read, so the years before the cut are drawn without dates/)).toBeInTheDocument();
    expect(document.querySelector(".cn-ya span")!.textContent).toBe("2023");
  });

  it("(r4) the years a chain may use: the cut, the exam stretches, the years from the first search day", async () => {
    await open();
    const bar = await screen.findByTestId("chain-allowed-years");
    await waitFor(() => expect(bar.querySelectorAll("[data-exam]").length).toBe(2));
    expect(bar.querySelector(".cn-open")!.getAttribute("title")).toBe("Before the cut: every chain window must end by 01/01/2023");
    expect(bar.textContent).toContain("Open to chains");
    expect(bar.textContent).toContain("Kept for the exam");
    expect(bar.querySelector('[data-exam="1"]')!.className).toContain("spent");
    expect(bar.querySelector('[data-exam="2"]')!.getAttribute("title")).toBe("Exam stretch 2: 01/07/2023 to 01/01/2024, not used yet");
    expect(document.querySelector(".cn-ya span")!.textContent).toBe("2005");
    expect(document.querySelector(".cn-facts")!.textContent).toContain("1 of 2");
    expect(document.querySelector(".cn-facts")!.textContent).toContain("1 Jan 2023");
  });
});
