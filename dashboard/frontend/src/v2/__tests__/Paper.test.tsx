import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Paper from "../pages/trading/Paper";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";
import { fmtTime } from "../../lib/format";
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";

const fsw = nodeFs as unknown as {
  readFileSync: (p: string, enc: string) => string;
  readdirSync: (p: string) => string[];
  statSync: (p: string) => { isDirectory: () => boolean };
};

const getRunningRuns = vi.fn();
const getSummary = vi.fn();
const getRunFills = vi.fn();
const fetchNames = vi.fn();
const getHoldings = vi.fn();
const getPaperLimits = vi.fn();
const putPaperLimits = vi.fn();
const killRun = vi.fn();
const promoteRun = vi.fn();
const approveForRealMoney = vi.fn();

vi.mock("../data/paperApi", () => ({
  getRunningRuns: () => getRunningRuns(),
  getSummary: (w: string) => getSummary(w),
  getRunFills: (id: number) => getRunFills(id),
  getHoldings: (id: number) => getHoldings(id),
  getPaperLimits: () => getPaperLimits(),
  putPaperLimits: (b: unknown) => putPaperLimits(b),
  killRun: (id: number) => killRun(id),
  promoteRun: (id: number) => promoteRun(id),
  approveForRealMoney: (id: number) => approveForRealMoney(id),
}));

vi.mock("../data/opsApi", () => ({ getSystems: () => Promise.resolve({ systems: [] }) }));

vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => fetchNames() }) };
});

const app = APPS.find((a) => a.id === "trading")!;
const section = app.sections.find((s) => s.id === "paper")!;

const series = (n: number, start: number): Array<[string, number]> =>
  Array.from({ length: n }, (_, i) => [`2026-10-${String(i + 1).padStart(2, "0")}T20:00:00Z`, start + i * 10]);

const run = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  paper_run_id: id,
  strategy_id: id,
  strategy_name: name,
  strategy_version: 1,
  source_run_id: 1,
  slice_opening_cash: 10000,
  expected_bounds: null,
  started_at: "2026-10-01T00:00:00Z",
  ended_at: null,
  end_reason: null,
  kill_started_at: null,
  kill_step: null,
  slice: { cash: 5000, position_value: 5500, equity: 10500, realised_pnl: 120, committed_capital: 5500, updated_at: "x" },
  equity_series: series(5, 10000),
  realised_result_counts: { win_count: 3, loss_count: 2 },
  ...extra,
});

const fill = (id: number, sym: string, side: "buy" | "sell", px: number | null, at: string, reason: string | null = null) => ({
  fill_id: id, paper_run_id: 1, symbol: sym, side, intended_quantity: 1, intended_notional: 1,
  submitted_quantity: 1, filled_quantity: px === null ? null : 1, fill_price: px, fees: 0, venue_order_id: null,
  recorded_at: at, submitted_at: at, filled_at: px === null ? null : at, refusal_reason: reason,
  realised_gain: null, position_effect: null,
});

const SINCE = { total_equity: 21000, total_opening_cash: 20000, delta_fraction: 0.05, running_count: 2,
  promotions_requested_count: 0, trades_in_window: 99, fees_paid_total: 0, window: "since_start" };
const WEEK = { ...SINCE, trades_in_window: 17, window: "this_week" };

const KEYS = [
  "paper.per_order_notional_cap",
  "paper.ticker_net_cap",
  "paper.total_net_cap",
  "paper.loss_limit_window",
  "paper.loss_limit_since_go",
  "paper.order_rate_cap",
];
const limitsBody = (values: Array<number | null>) => ({
  limits: KEYS.map((key, i) => ({ key, value: values[i], unit: "usd", meaning: "x" })),
});
const ALL_SET = [1000, 5000, 20000, 300, 1500, 10];

const hrow = (symbol: string, extra: Record<string, unknown> = {}) => ({
  symbol, quantity: 10, average_cost: 100, mark_price: 300, mark_source: "alpaca latest trade, IEX",
  mark_time: "2026-10-29T15:30:00Z", value: 3000, open_pnl: 200, share: 0.3, mark_unavailable_reason: null,
  ...extra,
});
const holdings = (extra: Record<string, unknown> = {}) => ({
  paper_run_id: 1, cash: 5750, marked_equity: 10000,
  rows: [hrow("AAPL"), hrow("MSFT", { value: 1250, open_pnl: -40, share: 0.125 })],
  share_unavailable_reason: null,
  reconcile: [
    { symbol: "AAPL", ours_quantity: 10, alpaca_quantity: 10, mismatch: false },
    { symbol: "MSFT", ours_quantity: 10, alpaca_quantity: 10, mismatch: false },
  ],
  alpaca_positions_unavailable_reason: null,
  ...extra,
});

const refusal = (word: string) => new ApiError("422 Unprocessable", 422, { detail: word }, "/api/paper/limits");

const runCards = () => Array.from(document.querySelectorAll("button.tp-card"));
const runCard = (name: string) => {
  const el = runCards().find((c) => c.querySelector(".tp-name")?.textContent === name);
  if (!el) throw new Error(`no strategy card ${name}`);
  return el as HTMLElement;
};
const sumCard = (name: string) => {
  const el = Array.from(document.querySelectorAll(".tp-card")).find((c) => c.querySelector(".tp-name")?.textContent === name && c.tagName !== "BUTTON");
  if (!el) throw new Error(`no summary card ${name}`);
  return el as HTMLElement;
};
const ready = async (name = "Alpha gap") => { await waitFor(() => runCard(name)); };

const holdingsPanel = async () => within(await screen.findByTestId("holdings"));
const limitsPanel = async () => within(await screen.findByTestId("limits"));

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Paper app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  getRunningRuns.mockResolvedValue([run(1, "Alpha gap"), run(2, "Beta drift", { equity_series: series(7, 9000) })]);
  getSummary.mockImplementation((w: string) => Promise.resolve(w === "this_week" ? WEEK : SINCE));
  getRunFills.mockResolvedValue({ fills: [], trade_stats: { win_count: 0, loss_count: 0 } });
  getHoldings.mockResolvedValue({ ...holdings(), rows: [], reconcile: [], cash: 10000 });
  getPaperLimits.mockResolvedValue(limitsBody(ALL_SET));
  putPaperLimits.mockResolvedValue(limitsBody(ALL_SET));
  fetchNames.mockResolvedValue([
    { id: 1, name: "alpha_gap", display_name: "Alpha gap", status: "active" },
    { id: 2, name: "beta_drift", display_name: "Beta drift", status: "active" },
  ]);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  getRunningRuns.mockReset();
  getSummary.mockReset();
  getRunFills.mockReset();
  fetchNames.mockReset();
  getHoldings.mockReset();
  getPaperLimits.mockReset();
  putPaperLimits.mockReset();
  killRun.mockReset();
  promoteRun.mockReset();
});

describe("Paper page", () => {
  it("renders one card per running strategy with its name", async () => {
    mount();
    await ready();
    expect(runCard("Beta drift")).toBeInTheDocument();
    expect(runCards()).toHaveLength(2);
  });

  it("has the mockup's sections: needs you, summary, strategies, the picked strategy, holdings, orders, limits", async () => {
    mount();
    await ready();
    for (const h of ["Needs you", "Summary", "Strategies on paper", "Holdings, Alpha gap", "Orders", "Limits"]) {
      expect(await screen.findByRole("heading", { name: h })).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { name: /^Account value, Alpha gap/ })).toBeInTheDocument();
  });

  it("reads the four summary cards from the two summaries", async () => {
    mount();
    await ready();
    expect(sumCard("Account value").textContent).toContain("$21,000");
    expect(sumCard("Account value").textContent).toContain("$20,000");
    expect(sumCard("Return").textContent).toContain("+5.00%");
    expect(sumCard("Strategies").querySelector(".tp-fig")!.textContent).toBe("2");
    expect(sumCard("Orders").querySelector(".tp-fig")!.textContent).toBe("17");
    expect(sumCard("Orders").textContent).not.toContain("99");
  });

  it("draws the picked strategy in its block and switches on click", async () => {
    mount();
    await ready();
    expect(screen.getByTestId("main-title").textContent).toContain("Alpha gap");
    fireEvent.click(runCard("Beta drift"));
    expect(screen.getByTestId("main-title").textContent).toContain("Beta drift");
    expect(document.querySelectorAll(".lcbox path.ln").length).toBeGreaterThan(0);
    expect(screen.getByTestId("side-table").textContent).toContain("$10,500");
  });

  it("says too few points for a one-point series", async () => {
    getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { equity_series: series(1, 10000) })]);
    mount();
    expect(await screen.findByText("Too few points to draw yet.")).toBeInTheDocument();
    expect(document.querySelector(".lcbox path.ln")).toBeNull();
  });

  it("says too few points when the series field is absent", async () => {
    const r = run(1, "Alpha gap") as Record<string, unknown>;
    delete r.equity_series;
    getRunningRuns.mockResolvedValue([r]);
    mount();
    expect(await screen.findByText("Too few points to draw yet.")).toBeInTheDocument();
  });

  it("shows the newest orders and a refusal with its reason as the hover", async () => {
    getRunningRuns.mockResolvedValue([run(1, "Alpha gap")]);
    getRunFills.mockResolvedValue({
      fills: [
        fill(1, "OLDX", "buy", 1.11, "2026-10-01T10:00:00Z"),
        fill(2, "AAPL", "buy", 190.25, "2026-10-02T10:00:00Z"),
        fill(3, "MSFT", "sell", 410.5, "2026-10-03T10:00:00Z"),
        fill(4, "TSLA", "buy", null, "2026-10-04T10:00:00Z", "Not enough cash in the slice"),
      ],
      trade_stats: { win_count: 3, loss_count: 2 },
    });
    mount();
    const refused = await waitFor(() => { const e = screen.getAllByText("Refused").find((x) => x.classList.contains("nl-st")); if (!e) throw new Error("no refusal yet"); return e; });
    expect(refused).toHaveAttribute("title", "Not enough cash in the slice");
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
    expect(screen.getByText("$190.25")).toBeInTheDocument();
    expect(screen.getByText("$410.50")).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.getAllByText("Filled").filter((e) => e.classList.contains("nl-st"))).toHaveLength(3);
    expect(screen.getByText("Sell")).toBeInTheDocument();
  });

  it("shows a dash with a hover when the return since start is missing", async () => {
    getSummary.mockImplementation((w: string) =>
      Promise.resolve(w === "this_week" ? WEEK : { ...SINCE, delta_fraction: null }),
    );
    mount();
    await ready();
    await waitFor(() => expect(screen.getByTitle("No starting value yet").textContent).toBe("-"));
    expect(sumCard("Return").querySelector(".tp-fig")!.textContent).toBe("-");
  });

  it("with no running strategy it says so, shows no cards and no summary", async () => {
    getRunningRuns.mockResolvedValue([]);
    mount();
    expect((await screen.findAllByText("No strategy is on paper right now.")).length).toBeGreaterThan(0);
    expect(document.querySelectorAll(".tp-card")).toHaveLength(0);
    expect(screen.queryByRole("heading", { name: "Summary" })).toBeNull();
    expect(screen.getByRole("button", { name: "Open Forward testing" })).toBeInTheDocument();
  });

  it("gives the card dashes their reasons when a run has no figures", async () => {
    const r = run(1, "Alpha gap") as Record<string, unknown>;
    delete r.slice;
    delete r.realised_result_counts;
    getRunningRuns.mockResolvedValue([r]);
    mount();
    await ready();
    const c = runCard("Alpha gap");
    expect(c.querySelector(".tp-fig span")).toHaveAttribute("title", "No account figures for this strategy yet");
    const dashes = Array.from(c.querySelectorAll(".tp-facts b span[title]"));
    expect(dashes).toHaveLength(3);
    expect(dashes[0]).toHaveAttribute("title", "No closed trades counted yet");
  });

  it("pf2 the empty card has the note and no second empty line (the finding above it says it, as in the mockup)", async () => {
    getRunningRuns.mockResolvedValue([]);
    mount();
    await screen.findAllByText("No strategy is on paper right now.");
    expect(document.querySelector("p.empty.pp-empty")).toBeNull();
    expect(document.querySelector("p.pp-note")!.textContent).toContain("A strategy gets here after the judge passes it");
  });

  it("has no link to the old page", async () => {
    mount();
    await ready();
    await waitFor(() => expect(getRunFills).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: "Open the current page" })).toBeNull();
  });

  it("shows the display name, with the stored name on hover, on the card and in the account title", async () => {
    getRunningRuns.mockResolvedValue([run(1, "strategy_runs_ma_cross")]);
    fetchNames.mockResolvedValue([{ id: 1, name: "strategy_runs_ma_cross", display_name: "Moving average cross", status: "active" }]);
    mount();
    const card = await waitFor(() => runCard("Moving average cross"));
    expect(card.getAttribute("title")).toBe("strategy_runs_ma_cross");
    expect(screen.getByTestId("main-title").textContent).toBe("Account value, Moving average cross");
    expect(document.body.textContent).not.toContain("strategy_runs_ma_cross");
  });

  it("w7 a strategy with no display name shows a dash with 'No display name'", async () => {
    getRunningRuns.mockResolvedValue([run(1, "bare_one")]);
    fetchNames.mockResolvedValue([{ id: 1, name: "bare_one", display_name: null, status: "active" }]);
    mount();
    const card = await waitFor(() => {
      const el = document.querySelector("button.tp-card[title='No display name']");
      expect(el).not.toBeNull();
      return el!;
    });
    expect(card.querySelector(".tp-name")!.textContent).toBe("-");
    expect(screen.getByTestId("main-title").querySelector("[title='No display name']")).not.toBeNull();
    expect(document.body.textContent).not.toContain("Bare one");
  });

  it("h16 with no strategy on paper the Limits block, its state and Edit still show", async () => {
    getRunningRuns.mockResolvedValue([]);
    getPaperLimits.mockResolvedValue(limitsBody([null, null, null, null, null, null]));
    mount();
    expect((await screen.findAllByText("No strategy is on paper right now.")).length).toBeGreaterThan(0);
    const l = await limitsPanel();
    expect((await l.findByText("Orders blocked")).closest("[title]")).toHaveAttribute("title", "6 of 6 not set");
    expect(l.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("h1 draws the holdings table with shares, values, open profit and a Cash row", async () => {
    getHoldings.mockResolvedValue(holdings());
    mount();
    const h = await holdingsPanel();
    await h.findByText("AAPL");
    expect(screen.getByTestId("holdings").textContent).toContain("Holdings, Alpha gap");
    expect(h.getByText("42.5% invested")).toBeInTheDocument();
    expect(h.getAllByText("Long")).toHaveLength(2);
    expect(h.getByText("30.0%")).toBeInTheDocument();
    expect(h.getByText("12.5%")).toBeInTheDocument();
    expect(h.getByText("$3,000.00")).toBeInTheDocument();
    expect(h.getByText("+$200.00")).toHaveClass("good");
    expect(h.getByText("-$40.00")).toHaveClass("bad");
    expect(document.querySelectorAll("[data-testid='holdings'] .tp-share").length).toBe(4);
    const cash = h.getByText("Cash").closest("tr") as HTMLElement;
    expect(cash.textContent).toContain("57.5%");
    expect(cash.textContent).toContain("$5,750.00");
  });

  it("h2 a row without a mark shows no price three times and a dash note, with the reason", async () => {
    getHoldings.mockResolvedValue(holdings({
      rows: [hrow("AAPL", { mark_price: null, value: null, open_pnl: null, share: null, mark_unavailable_reason: "Alpaca is down" })],
      marked_equity: null, share_unavailable_reason: "a mark is unavailable: AAPL",
    }));
    mount();
    const h = await holdingsPanel();
    const cells = await h.findAllByText("no price");
    expect(cells).toHaveLength(3);
    cells.forEach((c) => expect(c).toHaveAttribute("title", "Alpaca is down"));
    const note = document.querySelector("[data-testid='holdings'] .nl-blk-t [title='Alpaca is down']");
    expect(note?.textContent).toBe("-");
  });

  it("h3 a mismatch shows the amber word with both quantities, or says Alpaca could not be read", async () => {
    getHoldings.mockResolvedValue(holdings({
      reconcile: [
        { symbol: "AAPL", ours_quantity: 10, alpaca_quantity: 7, mismatch: true },
        { symbol: "MSFT", ours_quantity: 10, alpaca_quantity: null, mismatch: true },
      ],
      alpaca_positions_unavailable_reason: "no keys",
    }));
    mount();
    const h = await holdingsPanel();
    const words = await h.findAllByText("Differs");
    expect(words).toHaveLength(2);
    expect(words[0]).toHaveAttribute("title", "differs from Alpaca: ours 10, Alpaca 7");
    expect(words[0]).toHaveClass("a");
    expect(words[1].getAttribute("title")).toBe("ours 10, Alpaca could not be read, no keys");
  });

  it("h4 an empty list reads the empty sentence and a failed read the failure sentence", async () => {
    mount();
    const h = await holdingsPanel();
    expect(await h.findByText("No holdings for this strategy yet.")).toBeInTheDocument();
    getHoldings.mockRejectedValue(new Error("boom"));
    fireEvent.click(runCard("Beta drift"));
    expect(await h.findByText("Could not load the holdings.")).toHaveAttribute("title", "boom");
  });

  it("h5 the limits note says orders blocked with the count of unset limits, or all set", async () => {
    getPaperLimits.mockResolvedValue(limitsBody([null, 5000, null, 300, null, 10]));
    mount();
    const l = await limitsPanel();
    expect(await l.findAllByText("Not set")).toHaveLength(3);
    const word = l.getByText("Orders blocked");
    expect(word.closest("[title]")).toHaveAttribute("title", "3 of 6 not set");
    expect(word).toHaveClass("warn");
    cleanup();
    getPaperLimits.mockResolvedValue(limitsBody(ALL_SET));
    mount();
    const l2 = await limitsPanel();
    expect(await l2.findByText("All set")).toHaveClass("good");
  });

  it("h6 the six names in order, orders per minute as a whole number", async () => {
    mount();
    const l = await limitsPanel();
    await l.findByText("Largest order");
    const rowsOf = () => Array.from(document.querySelectorAll("[data-testid='limits'] .pp-lim"));
    expect(rowsOf().map((r) => r.children[0].textContent)).toEqual([
      "Largest order", "Largest position in one stock", "Total invested",
      "Largest loss in a day", "Largest loss since start", "Orders per minute",
    ]);
    expect(rowsOf()[5].children[2].textContent).toBe("10 orders");
    expect(rowsOf()[0].children[2].textContent).toBe("$1,000");
  });

  it("h7 EDIT opens the dialog, SAVE sends only the changed key and the dialog closes", async () => {
    mount();
    const l = await limitsPanel();
    fireEvent.click(await l.findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Paper limits" }));
    const input = dlg.getByLabelText("Largest order") as HTMLInputElement;
    expect(input.value).toBe("1000");
    fireEvent.change(input, { target: { value: "1200" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putPaperLimits).toHaveBeenCalledWith({ "paper.per_order_notional_cap": 1200 }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("h8 a refused value keeps the dialog open and shows the sentence", async () => {
    putPaperLimits.mockRejectedValue(refusal("paper-limit-value-invalid"));
    mount();
    const l = await limitsPanel();
    fireEvent.click(await l.findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Paper limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "-5" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(await dlg.findByText("Each limit must be a number above 0, and orders per minute a whole number.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("h9 hover on a Value reads the Alpaca IEX trade time", async () => {
    getHoldings.mockResolvedValue(holdings());
    mount();
    const h = await holdingsPanel();
    const v = await h.findByText("$3,000.00");
    expect(v).toHaveAttribute("title", `Alpaca IEX latest trade, ${fmtTime("2026-10-29T15:30:00Z")}`);
  });

  it("h10 the unknown-key word and an unknown word each read their sentence", async () => {
    putPaperLimits.mockRejectedValue(refusal("paper-limit-unknown-key"));
    mount();
    const l = await limitsPanel();
    fireEvent.click(await l.findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Paper limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "1200" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(await dlg.findByText("That limit is not one the server knows.")).toBeInTheDocument();
    putPaperLimits.mockRejectedValue(refusal("paper-limit-something-new"));
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(await dlg.findByText("The server refused: paper limit something new")).toBeInTheDocument();
  });

  it("h11 CANCEL closes the dialog and sends nothing", async () => {
    mount();
    const l = await limitsPanel();
    fireEvent.click(await l.findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Paper limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "1200" } });
    fireEvent.click(dlg.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(putPaperLimits).not.toHaveBeenCalled();
  });

  it("h12 an emptied field and a non-number each block SAVE with the sentence", async () => {
    mount();
    const l = await limitsPanel();
    fireEvent.click(await l.findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Paper limits" }));
    const sentence = "Each limit needs a number; a limit cannot be emptied once set.";
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(await dlg.findByText(sentence)).toBeInTheDocument();
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "abc" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(await dlg.findByText(sentence)).toBeInTheDocument();
    expect(putPaperLimits).not.toHaveBeenCalled();
  });

  it("h13 after a successful SAVE the limits are read again", async () => {
    mount();
    const l = await limitsPanel();
    fireEvent.click(await l.findByRole("button", { name: "Edit" }));
    expect(getPaperLimits).toHaveBeenCalledTimes(1);
    const dlg = within(screen.getByRole("dialog", { name: "Paper limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "1200" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(getPaperLimits).toHaveBeenCalledTimes(2));
  });

  it("h14 without marked equity the Cash share is a dash with the reason", async () => {
    getHoldings.mockResolvedValue(holdings({
      marked_equity: null, share_unavailable_reason: "a mark is unavailable: AAPL",
      rows: [hrow("AAPL", { mark_price: null, value: null, open_pnl: null, share: null, mark_unavailable_reason: "x" })],
    }));
    mount();
    const h = await holdingsPanel();
    const cash = (await h.findByText("Cash")).closest("tr") as HTMLElement;
    const dash = cash.querySelector("[title='a mark is unavailable: AAPL']");
    expect(dash?.textContent).toBe("-");
  });

  it("h15 both panels say they are loading, then that they failed", async () => {
    getHoldings.mockReturnValue(new Promise(() => {}));
    getPaperLimits.mockReturnValue(new Promise(() => {}));
    mount();
    expect(await screen.findByText("Loading the holdings.")).toBeInTheDocument();
    expect(screen.getByText("Loading the limits.")).toBeInTheDocument();
    cleanup();
    getHoldings.mockRejectedValue(new Error("h boom"));
    getPaperLimits.mockRejectedValue(new Error("l boom"));
    mount();
    expect(await screen.findByText("Could not load the holdings.")).toHaveAttribute("title", "h boom");
    expect(await screen.findByText("Could not load the limits.")).toHaveAttribute("title", "l boom");
  });
});

const strategyPanel = async () => {
  await ready();
  const title = await screen.findByTestId("main-title");
  return within(title.closest(".nl-blk") as HTMLElement);
};
const promo = (state: string) => ({ promotion_id: 7, requested_at: "2026-10-02T00:00:00Z", state, decided_at: null, decision_ledger_entry: null });
const apiErr = (status: number, detail: string) => new ApiError(`${status} x`, status, { detail }, "/api/paper/runs/1/x");

describe("Paper page, stop and promote (UI41)", () => {
  beforeEach(() => {
    getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { latest_promotion: null }), run(2, "Beta drift")]);
  });

  it("p1 shows PROMOTE then STOP, STOP last and red", async () => {
    mount();
    const p = await strategyPanel();
    const names = p.getAllByRole("button").map((b) => b.textContent);
    expect(names).toEqual(["Promote", "Stop"]);
    expect(p.getByRole("button", { name: "Stop" }).className).toContain("danger");
  });

  it("p2 STOP opens a dialog, CANCEL calls nothing, STOP calls killRun once and reads the runs again", async () => {
    killRun.mockResolvedValue({ ...run(1, "Alpha gap"), end_reason: "killed", venue: "stub" });
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: "Stop" }));
    let dlg = within(screen.getByRole("dialog"));
    expect(dlg.getByText("Stop Alpha gap on paper")).toBeTruthy();
    fireEvent.click(dlg.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(killRun).not.toHaveBeenCalled();
    fireEvent.click(p.getByRole("button", { name: "Stop" }));
    dlg = within(screen.getByRole("dialog"));
    const before = getRunningRuns.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(killRun).toHaveBeenCalledTimes(1));
    expect(killRun).toHaveBeenCalledWith(1);
    await waitFor(() => expect(getRunningRuns.mock.calls.length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("p3 the PROMOTE dialog calls promoteRun once and reads the runs again", async () => {
    promoteRun.mockResolvedValue({ promotion_id: 7, state: "requested" });
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: "Promote" }));
    const dlg = within(screen.getByRole("dialog"));
    expect(dlg.getByText("Promote Alpha gap")).toBeTruthy();
    const before = getRunningRuns.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Promote" }));
    await waitFor(() => expect(promoteRun).toHaveBeenCalledTimes(1));
    expect(promoteRun).toHaveBeenCalledWith(1);
    await waitFor(() => expect(getRunningRuns.mock.calls.length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("p4 requested shows PROMOTION ASKED and granted shows PROMOTED, neither with PROMOTE", async () => {
    getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { latest_promotion: promo("requested") })]);
    mount();
    let p = await strategyPanel();
    expect(p.getByText("Promotion asked")).toBeTruthy();
    expect(p.queryByRole("button", { name: "Promote" })).toBeNull();
    cleanup();
    getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { latest_promotion: promo("granted") })]);
    mount();
    p = await strategyPanel();
    expect(p.getByText("Promoted")).toBeTruthy();
    expect(p.queryByRole("button", { name: "Promote" })).toBeNull();
    cleanup();
    getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { latest_promotion: promo("declined") })]);
    mount();
    p = await strategyPanel();
    expect(p.getByRole("button", { name: "Promote" })).toBeTruthy();
  });

  it("p5 a killRun refusal shows its detail and reads nothing again", async () => {
    killRun.mockRejectedValue(apiErr(404, "no such paper run"));
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: "Stop" }));
    const dlg = within(screen.getByRole("dialog"));
    const before = getRunningRuns.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Stop" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("no such paper run");
    expect(getRunningRuns.mock.calls.length).toBe(before);
  });

  it("p6 a stop that only advanced a step keeps the dialog with the part-done sentence", async () => {
    killRun.mockResolvedValue({ ...run(1, "Alpha gap"), end_reason: null, kill_step: "orders_cancelled", venue: "stub" });
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: "Stop" }));
    const dlg = within(screen.getByRole("dialog"));
    const before = getRunningRuns.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Stop" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("The stop is only part done. Press Stop again to finish it.");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(getRunningRuns.mock.calls.length).toBe(before);
  });

  it("p7 a promoteRun refusal shows its detail and reads nothing again", async () => {
    promoteRun.mockRejectedValue(apiErr(409, "a promotion is already asked"));
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: "Promote" }));
    const dlg = within(screen.getByRole("dialog"));
    const before = getRunningRuns.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Promote" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("a promotion is already asked");
    expect(getRunningRuns.mock.calls.length).toBe(before);
  });

  it("p8 a non-ApiError rejection of killRun shows the plain sentence", async () => {
    killRun.mockRejectedValue(new Error("boom"));
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: "Stop" }));
    const dlg = within(screen.getByRole("dialog"));
    fireEvent.click(dlg.getByRole("button", { name: "Stop" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("Could not stop this strategy.");
  });
});

const approveBtn = "Approve for real money";
const onlyRun = (promotion: unknown) => getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { latest_promotion: promotion })]);
const SECOND_LINE = "The paper broker is not connected yet, so no paper shares are actually sold.";

describe("Paper page, approve for real money (PRM1)", () => {
  it("ap1 a requested promotion shows PROMOTION ASKED, APPROVE FOR REAL MONEY, then STOP last and red", async () => {
    onlyRun(promo("requested"));
    mount();
    const p = await strategyPanel();
    expect(p.getByText("Promotion asked")).toBeTruthy();
    const names = p.getAllByRole("button").map((b) => b.textContent);
    expect(names).toEqual([approveBtn, "Stop"]);
    expect(p.getByRole("button", { name: "Stop" }).className).toContain("danger");
    expect(p.getByRole("button", { name: approveBtn }).className).toContain("btn");
  });

  it("ap2 no live request shows no APPROVE FOR REAL MONEY", async () => {
    for (const promotion of [null, promo("granted"), promo("declined"), promo("withdrawn")]) {
      onlyRun(promotion);
      mount();
      const p = await strategyPanel();
      expect(p.queryByRole("button", { name: approveBtn })).toBeNull();
      cleanup();
    }
  });

  it("ap3 the window shows both lines; CANCEL calls nothing; APPROVE calls once and reads the runs again", async () => {
    approveForRealMoney.mockResolvedValue({ paper_run_id: 1, end_reason: "promoted", ended_at: "x", kill_step: "done", venue: "stub" });
    onlyRun(promo("requested"));
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: approveBtn }));
    let dlg = within(screen.getByRole("dialog"));
    expect(dlg.getByText("Approve Alpha gap for real money")).toBeTruthy();
    expect(dlg.getByText("Its paper positions are closed as Stop closes them, then its paper run ends as approved. No real order is sent; real money stays off until your go is recorded in the decisions log.")).toBeTruthy();
    expect(dlg.getByText(SECOND_LINE)).toBeTruthy();
    fireEvent.click(dlg.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(approveForRealMoney).not.toHaveBeenCalled();
    fireEvent.click(p.getByRole("button", { name: approveBtn }));
    dlg = within(screen.getByRole("dialog"));
    const before = getRunningRuns.mock.calls.length;
    const go = dlg.getByRole("button", { name: "Approve" });
    fireEvent.click(go);
    fireEvent.click(go);
    await waitFor(() => expect(approveForRealMoney).toHaveBeenCalledTimes(1));
    expect(approveForRealMoney).toHaveBeenCalledWith(1);
    await waitFor(() => expect(getRunningRuns.mock.calls.length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(approveForRealMoney).toHaveBeenCalledTimes(1);
  });

  it("ap4 a refusal shows the server's sentence and reads nothing again", async () => {
    approveForRealMoney.mockRejectedValue(apiErr(409, "this strategy has no live promotion request"));
    onlyRun(promo("requested"));
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: approveBtn }));
    const dlg = within(screen.getByRole("dialog"));
    const before = getRunningRuns.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Approve" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("this strategy has no live promotion request");
    expect(getRunningRuns.mock.calls.length).toBe(before);
  });

  it("ap5 a non-ApiError rejection shows the plain sentence", async () => {
    approveForRealMoney.mockRejectedValue(new Error("boom"));
    onlyRun(promo("requested"));
    mount();
    const p = await strategyPanel();
    fireEvent.click(p.getByRole("button", { name: approveBtn }));
    const dlg = within(screen.getByRole("dialog"));
    fireEvent.click(dlg.getByRole("button", { name: "Approve" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("Could not approve this strategy.");
  });

  it("ap6 outside the tests, the call appears twice in src and the route path once", () => {
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of fsw.readdirSync(dir)) {
        const full = `${dir}/${name}`;
        if (fsw.statSync(full).isDirectory()) {
          if (name !== "__tests__") walk(full);
        } else if (/\.(ts|tsx)$/.test(name)) files.push(full);
      }
    };
    walk("src");
    const count = (needle: string) =>
      files.reduce((n, f) => n + fsw.readFileSync(f, "utf-8").split(needle).length - 1, 0);
    expect(count("approveForRealMoney(")).toBe(2);
    expect(count("approve-real-money")).toBe(1);
  });
});
