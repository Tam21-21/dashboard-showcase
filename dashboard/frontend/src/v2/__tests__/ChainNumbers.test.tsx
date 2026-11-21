import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Chains from "../pages/research/Chains";
import { Scatter } from "../kit";
import { stepSharpes } from "../pages/research/chain/stepSharpe";
import { APPS } from "../shell/sections";

const chains = vi.fn();
const chain = vi.fn();
const getRunCells = vi.fn();
const listStrategies = vi.fn();
const getStrategyStatus = vi.fn();

vi.mock("../../lib/api", async (orig) => {
  const actual = await orig<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      research: {
        ...actual.api.research,
        chains: (...a: unknown[]) => chains(...a),
        chain: (...a: unknown[]) => chain(...a),
        getRunCells: (...a: unknown[]) => getRunCells(...a),
        listStrategies: (...a: unknown[]) => listStrategies(...a),
        getStrategyStatus: (...a: unknown[]) => getStrategyStatus(...a),
      },
    },
  };
});

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "chains")!;

const ts = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const WORDS: Record<string, [string, string, string]> = {
  screening: ["SCREENING", "active", "screen"],
  optimising: ["OPTIMISING", "active", "optimise"],
  "walking-forward": ["WALKING FORWARD", "active", "walkforward"],
  "choosing-cell": ["CHOOSE A CELL", "waiting-on-you", "choose"],
  replaying: ["REPLAYING", "active", "replay"],
};
const mk = (id: number, state: string, extra: Record<string, unknown> = {}) => ({
  id, strategy_id: 10 + id, strategy_version: 1, calendar_id: `chain-${id}`,
  screen_start_ts: ts("2004-01-01"), optimise_start_ts: ts("2009-01-01"),
  walkforward_start_ts: ts("2014-01-01"), walkforward_end_ts: ts("2018-01-01"),
  state, state_word: WORDS[state][0], group: WORDS[state][1], step: WORDS[state][2],
  ended_at_step: null, stop_requested: 0, note: null, note_word: null,
  reason_word: null, reason_sentence: null, display_name: `Chain ${id}`,
  created_at: ts("2026-10-20"), updated_at: ts("2026-10-23"), duel_chain_id: null,
  chosen_variation_id: null, chosen_ticker: null, ...extra,
});
const trans = (id: number, chainId: number, from: string | null, to: string, iso: string) => ({
  id, chain_id: chainId, from_state: from, to_state: to, actor: "runner", created_at: Date.parse(iso) / 1000,
  reason_word: null, reason_sentence: null, to_word: WORDS[to]?.[0] ?? to.toUpperCase(),
});
const story = (chainId: number, path: string[]) =>
  path.map((to, i) => trans(i + 1, chainId, i === 0 ? null : path[i - 1], to, `2026-10-23T0${i + 1}:00:00Z`));

const D9 = {
  ...mk(9, "replaying", { chosen_variation_id: 14, chosen_ticker: "AAPL" }), optimise_run_id: 50, walkforward_run_id: null,
  transitions: story(9, ["screening", "optimising", "choosing-cell", "replaying"]),
};
const D7 = { ...mk(7, "walking-forward"), optimise_run_id: 50, walkforward_run_id: 60, transitions: [] };
const D5 = { ...mk(5, "choosing-cell"), optimise_run_id: 50, walkforward_run_id: null, transitions: story(5, ["screening", "optimising", "choosing-cell"]) };
const D8 = { ...mk(8, "screening"), optimise_run_id: null, walkforward_run_id: null, transitions: [] };
const DETAILS: Record<number, unknown> = { 9: D9, 7: D7, 5: D5, 8: D8 };
const LIST = {
  chains: [D9, D8, D7, D5].map((d) => {
    const { optimise_run_id: _o, walkforward_run_id: _w, transitions: _t, ...c } = d;
    return c;
  }),
  reserve_cut_ts: 1672531200,
};

const cell = (variation_id: number, ticker: string, sharpe: number | null) => ({
  variation_id, ticker, param_json: null, total_return: 0.31, sharpe_ratio: sharpe, max_drawdown: -0.18, num_trades: 61,
  win_rate: 0.56, total_fees_paid: 12.5, total_slippage_cost: 4.25, profit_factor: 1.4, skew: null, kurtosis: null,
});
// 25 cells: best Sharpe 2.50 (v101 NVDA), v14 AAPL 1.42 is the chosen one, one cell has no Sharpe.
const CELLS = [
  cell(30, "ZZZ", null),
  cell(101, "NVDA", 2.5),
  ...Array.from({ length: 22 }, (_, i) => cell(200 + i, "MSFT", 1.3 - i * 0.01)),
  cell(14, "AAPL", 1.42),
];
const CELLS25 = { run_id: 50, cell_count: 25, member_count: 5, variation_count: 5, summary: {}, cells: CELLS };

const SCREEN_WHY = "The server does not send the screen run's Sharpe yet";
const REPLAY_WHY = "The server does not send the replay run's Sharpe yet";
const WF_WHY = "The walk-forward run stores no Sharpe yet";
const JUDGE_WHY = "The judge step has no Sharpe of its own";

function Where() {
  const l = useLocation();
  return <span data-testid="where">{l.pathname + l.search}</span>;
}
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/app/research/chains"]}>
        <Chains app={app} section={section} />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const open = async (id: number) => {
  mount();
  await waitFor(() => expect(screen.getByTestId("chain-list").querySelector("[data-chain]")).not.toBeNull());
  fireEvent.click(screen.getByTestId("chain-list").querySelector(`[data-chain="${id}"]`) as HTMLElement);
  await waitFor(() => expect(chain).toHaveBeenCalledWith(id));
};
const where = () => screen.getByTestId("where").textContent;
const svgTitle = (el: Element) => el.querySelector("title")!.textContent;
const openRun = () => within(screen.getByTestId("chain-actions")).getByRole("button", { name: "OPEN RUN" });

beforeEach(() => {
  sessionStorage.clear();
  chains.mockResolvedValue(LIST);
  chain.mockImplementation((id: number) => Promise.resolve(DETAILS[id]));
  getRunCells.mockResolvedValue(CELLS25);
  listStrategies.mockResolvedValue([]);
  getStrategyStatus.mockResolvedValue({ sweep_params: [] });
});
afterEach(() => {
  vi.restoreAllMocks();
  for (const m of [chains, chain, getRunCells, listStrategies, getStrategyStatus]) m.mockReset();
  sessionStorage.clear();
});

describe("Chain numbers", () => {
  it("(n1) stepSharpes gives each of the six steps a value or its reason", () => {
    const s = stepSharpes(D9 as never, CELLS);
    expect(s.map((x) => x.step)).toEqual(["screen", "optimise", "choose", "replay", "walkforward", "judge"]);
    expect(s.map((x) => x.value)).toEqual([null, 2.5, 1.42, null, null, null]);
    expect(s[0].why).toBe(SCREEN_WHY);
    expect(s[3].why).toBe(REPLAY_WHY);
    expect(s[4].why).toBe(WF_WHY);
    expect(s[5].why).toBe(JUDGE_WHY);
    expect(s[1].change).toBeNull();
    expect(s[2].change).toBeCloseTo(-1.08, 6);
    expect(stepSharpes(D8 as never, null)[1].why).toBe("This chain has no optimise run yet");
    expect(stepSharpes(D9 as never, CELLS.map((c) => ({ ...c, sharpe_ratio: null })))[1].why).toBe("The optimise run saved no Sharpe");
    expect(stepSharpes(D5 as never, CELLS)[2].why).toBe("No cell has been chosen yet");
    expect(stepSharpes({ ...D9, chosen_ticker: "GONE" } as never, CELLS)[2].why).toBe("No Sharpe was saved for the chosen cell");
  });

  it("(n2) the Sharpe line draws known steps as dots and unknown steps as titled dashes", async () => {
    await open(9);
    const chart = await screen.findByTestId("chain-sharpe");
    await waitFor(() => expect(chart.querySelector('circle[data-step="optimise"]')).not.toBeNull());
    const dot = (step: string) => chart.querySelector(`circle[data-step="${step}"]`)!;
    expect(dot("optimise").getAttribute("fill")).toBe("var(--hi)");
    expect(dot("choose").getAttribute("fill")).toBe("var(--r)");
    expect(svgTitle(dot("choose"))).toBe("Choose: Sharpe 1.42, change -1.08");
    expect(chart.textContent).toContain("2.50");
    expect(chart.textContent).toContain("1.42");
    const dashes = Array.from(chart.querySelectorAll("text[data-unknown]"));
    expect(dashes.map((d) => d.getAttribute("data-step"))).toEqual(["screen", "replay", "walkforward", "judge"]);
    expect(dashes.map((d) => d.firstChild!.textContent)).toEqual(["-", "-", "-", "-"]);
    expect(dashes.map((d) => svgTitle(d))).toEqual([SCREEN_WHY, REPLAY_WHY, WF_WHY, JUDGE_WHY]);
  });

  it("(n3) the story rows leaving a step carry before, after and the change", async () => {
    await open(9);
    const rows = () => Array.from(screen.getByTestId("chain-story").querySelectorAll("[data-transition]"));
    await waitFor(() => expect(rows()).toHaveLength(4));
    // A row is named by the word it arrives at: REPLAYING leaves choosing-cell, CHOOSE A CELL leaves optimising.
    const rowFor = (word: string) => rows().find((r) => r.querySelector("b")!.textContent === word)!;
    await waitFor(() => expect(rowFor("REPLAYING").querySelector("[data-figure]")).not.toBeNull());
    const leavingChoose = rowFor("REPLAYING").querySelector("[data-figure]")!;
    expect(leavingChoose.textContent).toContain("2.50 → 1.42");
    const change = leavingChoose.querySelector("[data-change]")!;
    expect(change.textContent).toBe("▼ -1.08");
    expect(change.className).toContain("rd");
    expect(rowFor("CHOOSE A CELL").querySelector("[data-figure]")!.textContent).toBe("2.50");
    const leavingScreen = rowFor("OPTIMISING").querySelector("[data-figure]")!;
    expect(leavingScreen.textContent).toBe("-");
    expect(leavingScreen.querySelector("span")!.getAttribute("title")).toBe(SCREEN_WHY);
  });

  it("(n4) OPTIONS AT CHOOSE draws 24 points and a press opens nothing once a cell is chosen", async () => {
    await open(9);
    const chart = await screen.findByTestId("chain-options");
    await waitFor(() => expect(chart.querySelectorAll("[data-cell]")).toHaveLength(24));
    const count = screen.getByTestId("chain-options-count");
    expect(count.textContent).toBe("24");
    expect(count.getAttribute("title")).toBe("1 cell has no Sharpe or no deepest drop and is not drawn");
    const aapl = chart.querySelector('[data-cell="14-AAPL"]')!;
    expect(aapl.querySelectorAll("circle")).toHaveLength(3);
    expect(svgTitle(aapl)).toBe("AAPL, v14: Sharpe 1.42, deepest drop -18%");
    fireEvent.click(aapl);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("(n4b) on a chain waiting for a cell, a point press opens the CHOOSE window", async () => {
    await open(5);
    const chart = await screen.findByTestId("chain-options");
    await waitFor(() => expect(chart.querySelectorAll("[data-cell]")).toHaveLength(24));
    fireEvent.click(chart.querySelector('[data-cell="14-AAPL"]')!);
    const win = await screen.findByRole("dialog", { name: "Choose this cell" });
    expect(within(win).getByText("AAPL")).toBeInTheDocument();
  });

  it("(n5) OPEN RUN on a walk-forward chain opens the walk-forward run", async () => {
    await open(7);
    await waitFor(() => expect(openRun()).not.toBeDisabled());
    expect(openRun().getAttribute("title")).toBe("Open the walk-forward run");
    fireEvent.click(openRun());
    expect(where()).toBe("/app/research/runs?run=60");
  });

  it("(n5b) OPEN RUN on a chain with an optimise run only opens that run", async () => {
    await open(5);
    await waitFor(() => expect(openRun().getAttribute("title")).toBe("Open the optimise run"));
    fireEvent.click(openRun());
    expect(where()).toBe("/app/research/runs?run=50");
  });

  it("(n5c) a chain with no run has OPEN RUN disabled and titled", async () => {
    await open(8);
    await waitFor(() => expect(openRun()).toBeDisabled());
    expect(openRun().getAttribute("title")).toBe("This chain has no run yet");
  });

  it("(n5d) the OPTIMISE mark of chain 9 opens run 50", async () => {
    await open(9);
    const mark = await waitFor(() => {
      const m = screen.getByTestId("chain-marks").querySelector('button[data-step="optimise"]') as HTMLElement;
      expect(m).not.toBeNull();
      return m;
    });
    expect(mark.getAttribute("title")).toBe("Optimise: done; open its run");
    fireEvent.click(mark);
    expect(where()).toBe("/app/research/runs?run=50");
  });

  it("(n6) a Scatter given no zone draws no zone rect", () => {
    const axis = { lo: 0, hi: 1, ticks: [0, 1], text: (v: number) => String(v), name: "a" };
    const { container } = render(
      <Scatter testid="s" label="s" x={axis} y={axis} points={[]} picked={null} pointAttr="data-p" onPick={() => {}} />,
    );
    expect(container.querySelector("rect")).toBeNull();
  });
});
