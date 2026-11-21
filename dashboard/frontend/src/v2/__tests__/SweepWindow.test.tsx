import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SweepWindow from "../pages/research/sweep/SweepWindow";
import { ApiError } from "../../lib/api";

const m = vi.hoisted(() => ({
  listDatasets: vi.fn(),
  getStrategyStatus: vi.fn(),
  updatePriceUniverseWindow: vi.fn(),
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const { apiGet, apiPost, ...research } = m;
  return { ...real, apiGet, apiPost, api: { ...real.api, research: { ...real.api.research, ...research } } };
});

const EM_DASH = String.fromCharCode(8212);
const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const universe = (id: number, name: string, count: number, from: string | null, to: string | null, made: string) => ({
  id, name: name.toLowerCase().replace(/ /g, "_"), display_name: name, source: "eodhd", symbol: "", timeframe: "1d",
  start_ts: 0, end_ts: 0, status: "ready", rows: null,
  sha256: null, file_path: null, file_bytes: null, last_error: null, created_at: day(made), last_fetched_at: null,
  dataset_type: "price_universe", member_count: count,
  window_start_ts: from ? day(from) : null, window_end_ts: to ? day(to) : null,
});
const refused = (status: number, body: unknown) => new ApiError(`${status} /x`, status, body, "/x");

const STARTED = { id: 5, run_id: 9, status: "queued", registered_n: 6, member_count: 5, all_members_ready: true, finalized: false };
const GRID_URL = "/api/research/price-universes/7/dispatch";

function mount(props: { strategyId?: number; universeId?: number } = {}) {
  const onClose = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const spy = vi.spyOn(qc, "invalidateQueries");
  render(
    <QueryClientProvider client={qc}>
      <SweepWindow {...props} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose, spy };
}
const dlg = () => screen.getByRole("dialog", { name: "Run a sweep" });
const field = (label: string) => within(dlg()).getByLabelText(label) as HTMLInputElement;
const press = (name: string) => fireEvent.click(within(dlg()).getByRole("button", { name }));
const type = (label: string, value: string) => fireEvent.change(field(label), { target: { value } });
const start = () => within(dlg()).getByRole("button", { name: "START" }) as HTMLButtonElement;
async function ready(props: { strategyId?: number; universeId?: number } = { strategyId: 3, universeId: 7 }) {
  const out = mount(props);
  await screen.findByRole("dialog", { name: "Run a sweep" });
  if (props.strategyId === 3) await within(dlg()).findByLabelText("Fast window");
  if (props.strategyId === 4) await within(dlg()).findByTitle("This strategy declares no parameters");
  return out;
}
async function fillTwo() {
  type("Fast window", "10, 20");
  type("Exit z", "1.5, 2, 3");
}

beforeEach(() => {
  m.listDatasets.mockResolvedValue([
    { ...universe(1, "AAPL daily", 0, null, null, "2026-10-05"), dataset_type: "price_dataset", symbol: "AAPL" },
    universe(7, "Tech five", 5, "2015-01-01", "2024-12-31", "2026-10-01"),
    universe(9, "Banks", 2, null, null, "2026-10-10"),
    { ...universe(11, "Gone list", 3, null, null, "2026-10-12"), status: "deleted" },
  ]);
  m.apiGet.mockResolvedValue([
    { id: 3, name: "mom_v1", display_name: "Momentum", status: "active" },
    { id: 4, name: "rev_v1", display_name: "Reversal", status: "active" },
    { id: 5, name: "old_v1", display_name: "Old", status: "deleted" },
  ]);
  m.getStrategyStatus.mockImplementation(async (id: number) =>
    id === 3
      ? { sweep_params: [{ name: "fast_window", type: "int" }, { name: "exit_z", type: "float" }], param_mode: "vectorized" }
      : { sweep_params: [], param_mode: "zero" });
  m.updatePriceUniverseWindow.mockResolvedValue({ universe_id: 9 });
  m.apiPost.mockResolvedValue(STARTED);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Run a sweep window", () => {
  it("w1 the strategy and stock options, and the presets", async () => {
    mount();
    await screen.findByRole("dialog", { name: "Run a sweep" });
    await waitFor(() => expect(within(field("Stocks")).getAllByRole("option").length).toBeGreaterThan(0));
    const strategies = Array.from(field("Strategy").querySelectorAll("option")).map((o) => o.textContent);
    expect(strategies).toEqual(["Pick a strategy", "Momentum", "Reversal"]);
    const stocks = Array.from(field("Stocks").querySelectorAll("option")).map((o) => o.textContent);
    expect(stocks).toEqual(["Banks, 2 stocks", "Tech five, 5 stocks"]);
    expect(field("Stocks").value).toBe("9");
    cleanup();
    mount({ strategyId: 3, universeId: 7 });
    await waitFor(() => expect((field("Strategy") as unknown as HTMLSelectElement).value).toBe("3"));
    await waitFor(() => expect((field("Stocks") as unknown as HTMLSelectElement).value).toBe("7"));
  });

  it("w1 empty sets say so", async () => {
    m.listDatasets.mockResolvedValue([]);
    m.apiGet.mockResolvedValue([]);
    mount();
    await within(dlg()).findByText("No stock list yet");
    expect(within(dlg()).getByText("No strategy yet")).toBeInTheDocument();
  });

  it("w2 the window text, a dash for a list with none, and START off", async () => {
    await ready({ universeId: 7 });
    await within(dlg()).findByText("01/01/2015 to 31/12/2024");
    fireEvent.change(field("Stocks"), { target: { value: "9" } });
    const dash = within(dlg()).getByTitle("This stock list has no window yet");
    expect(dash.textContent).toBe("-");
    expect(start()).toBeDisabled();
  });

  it("w3 EDIT WINDOW saves once, asks for both dates, and shows a refusal in words", async () => {
    await ready({ universeId: 9 });
    await within(dlg()).findByText("Banks, 2 stocks");
    press("EDIT WINDOW");
    fireEvent.click(within(dlg()).getByRole("button", { name: "From" }));
    fireEvent.click(within(dlg()).getByRole("button", { name: "TODAY" }));
    press("SAVE");
    expect(await within(dlg()).findByText("Choose both dates.")).toBeInTheDocument();
    expect(m.updatePriceUniverseWindow).not.toHaveBeenCalled();
    fireEvent.click(within(dlg()).getByRole("button", { name: "To" }));
    fireEvent.click(within(dlg()).getByRole("button", { name: "TODAY" }));
    m.updatePriceUniverseWindow.mockRejectedValueOnce(refused(422, { detail: "Start must not be after end" }));
    press("SAVE");
    expect(await within(dlg()).findByText("Start must not be after end")).toBeInTheDocument();
    expect(m.updatePriceUniverseWindow).toHaveBeenCalledTimes(1);
    press("SAVE");
    await waitFor(() => expect(m.updatePriceUniverseWindow).toHaveBeenCalledTimes(2));
    const [id, from, to] = m.updatePriceUniverseWindow.mock.calls[1];
    expect(id).toBe(9);
    expect(from).toBe(to);
    expect(typeof from).toBe("number");
  });

  it("w4 counts, type checks, the total line and the zero case", async () => {
    await ready();
    const row = (label: string) => field(label).closest(".sw-vrow") as HTMLElement;
    type("Fast window", "10, 20, 20, ");
    expect(row("Fast window").querySelector(".sw-n")!.textContent).toBe("2");
    type("Exit z", "1.5, 2, 3");
    expect(dlg().textContent).toContain("6 combinations × 5 stocks = 30 backtests");
    const time = within(dlg()).getByTitle("The server does not estimate run time yet");
    expect(time.textContent).toBe("-");
    type("Fast window", "1.5");
    expect(within(dlg()).getByText("1.5 is not a whole number")).toBeInTheDocument();
    expect(row("Fast window").querySelector(".sw-n")!.textContent).toBe("-");
    type("Exit z", "abc");
    expect(within(dlg()).getByText("abc is not a number")).toBeInTheDocument();
    type("Fast window", "10");
    type("Exit z", "2");
    expect(dlg().textContent).toContain("1 combination × 5 stocks = 5 backtests");
    cleanup();
    await ready({ strategyId: 4, universeId: 7 });
    expect(within(dlg()).getByTitle("This strategy declares no parameters").textContent).toBe("-");
    expect(dlg().textContent).toContain("1 combination × 5 stocks = 5 backtests");
  });

  it("w4 another type cannot be swept, a strategy change empties the rows, a failed read shows the server's words", async () => {
    m.getStrategyStatus.mockImplementation(async (id: number) => {
      if (id === 4) throw refused(404, { detail: "No such strategy status" });
      return { sweep_params: [{ name: "label", type: "str" }], param_mode: "inline" };
    });
    mount({ strategyId: 3, universeId: 7 });
    await within(await screen.findByRole("dialog", { name: "Run a sweep" })).findByLabelText("Label");
    type("Label", "a");
    expect(within(dlg()).getByText("This type cannot be swept: str")).toBeInTheDocument();
    fireEvent.change(field("Strategy"), { target: { value: "4" } });
    expect(await within(dlg()).findByText("No such strategy status")).toBeInTheDocument();
    expect(within(dlg()).queryByLabelText("Label")).toBeNull();
  });

  it("w5 four stages, Reserve off with its hover, and the name follows the stage until typed in", async () => {
    await ready();
    const seg = dlg().querySelector(".seg") as HTMLElement;
    expect(Array.from(seg.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Screen", "Optimise", "Walk-forward", "Reserve"]);
    const reserve = within(seg).getByRole("button", { name: "Reserve" });
    expect(reserve).toBeDisabled();
    expect(reserve.getAttribute("title")).toBe("The reserve is used only by the reserve exam, on the strategy page");
    expect(field("Name").value).toBe("Momentum, screen");
    fireEvent.click(within(seg).getByRole("button", { name: "Optimise" }));
    expect(field("Name").value).toBe("Momentum, optimise");
    fireEvent.click(within(seg).getByRole("button", { name: "Walk-forward" }));
    expect(field("Name").value).toBe("Momentum, walk-forward");
    fireEvent.click(within(seg).getByRole("button", { name: "Screen" }));
    type("Name", "Mine");
    fireEvent.click(within(seg).getByRole("button", { name: "Optimise" }));
    expect(field("Name").value).toBe("Mine");
  });

  it("w5 a clicked Reserve never reaches a body", async () => {
    await ready();
    await fillTwo();
    fireEvent.click(within(dlg().querySelector(".seg") as HTMLElement).getByRole("button", { name: "Reserve" }));
    press("START");
    await waitFor(() => expect(m.apiPost).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(m.apiPost.mock.calls)).not.toContain("reserve");
    expect(m.apiPost.mock.calls[0][1].stage).toBe("screen");
  });

  it("w6 START on screen posts once with the exact body and says STARTED", async () => {
    const { spy } = await ready();
    expect(start()).toBeDisabled();
    await fillTwo();
    expect(start()).toBeEnabled();
    press("START");
    expect(await within(dlg()).findByText("STARTED")).toBeInTheDocument();
    expect(m.apiPost).toHaveBeenCalledTimes(1);
    expect(m.apiPost).toHaveBeenCalledWith(GRID_URL, {
      strategy_id: 3, param_grid: { fast_window: [10, 20], exit_z: [1.5, 2, 3] }, name: "Momentum, screen", stage: "screen",
    });
    expect(within(dlg()).getByText("Sweep started: Momentum, screen")).toBeInTheDocument();
    expect(within(dlg()).queryByRole("button", { name: "START" })).toBeNull();
    expect(within(dlg()).getByRole("button", { name: "CLOSE" })).toBeInTheDocument();
    expect(within(dlg()).queryByRole("button", { name: "CANCEL" })).toBeNull();
    const keys = spy.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey));
    expect(keys).toEqual(expect.arrayContaining(['["universes"]', '["strategies"]', '["runs"]']));
  });

  it("w6 START is held, with its reason, until the form is whole", async () => {
    await ready({ universeId: 7 });
    expect(start().getAttribute("title")).toBe("Pick a strategy first");
    await within(dlg()).findByRole("option", { name: "Momentum" });
    fireEvent.change(field("Strategy"), { target: { value: "3" } });
    await within(dlg()).findByLabelText("Fast window");
    expect(start().getAttribute("title")).toBe("Type valid values for Fast window");
    type("Name", "  ");
    await fillTwo();
    expect(start()).toBeDisabled();
    expect(start().getAttribute("title")).toBe("Type a name");
  });

  it("w7 Walk-forward posts to the stage runner with no name, and the Name is off", async () => {
    await ready();
    await fillTwo();
    fireEvent.click(within(dlg().querySelector(".seg") as HTMLElement).getByRole("button", { name: "Walk-forward" }));
    expect(field("Name")).toBeDisabled();
    expect(field("Name").getAttribute("title")).toBe("The server names a walk-forward run");
    press("START");
    expect(await within(dlg()).findByText("Walk-forward started")).toBeInTheDocument();
    expect(m.apiPost).toHaveBeenCalledTimes(1);
    expect(m.apiPost).toHaveBeenCalledWith("/api/research/stage-runner/dispatch", {
      strategy_id: 3, universe_id: 7, stage: "walkforward", calendar_id: "real",
      param_grid: { fast_window: [10, 20], exit_z: [1.5, 2, 3] },
    });
  });

  it("w8 each refusal shows its word and its text, START stays, a second press posts again", async () => {
    await ready();
    await fillTwo();
    const cases: Array<[unknown, string, string]> = [
      [refused(422, { detail: "sweep_grid_too_large: the server takes at most 200 combinations" }), "GRID TOO LARGE", "The server takes at most 200 combinations"],
      [refused(422, { detail: "frozen_sweep_parameter: fast window is frozen" }), "PARAMETER FROZEN", "Fast window is frozen"],
      [refused(422, { detail: "undeclared_sweep_parameter: exit z is not declared" }), "NOT DECLARED", "Exit z is not declared"],
      [refused(409, { detail: "price_universe_empty: this list has no stocks" }), "EMPTY LIST", "This list has no stocks"],
      [refused(409, { detail: "the sizing lead-in cannot be satisfied for ABNB" }), "NOT ENOUGH HISTORY", "the sizing lead-in cannot be satisfied for ABNB"],
      [refused(409, { detail: "the grid has a quantile warmup needs 60 bars" }), "NOT ENOUGH HISTORY", "the grid has a quantile warmup needs 60 bars"],
      [refused(409, { detail: "Some other words" }), "REFUSED", "Some other words"],
      [refused(409, { error: "This test window does not come after the last one" }), "REFUSED", "This test window does not come after the last one"],
      [refused(500, {}), "REFUSED", "The server failed on this request and sent no reason."],
      [refused(422, {}), "REFUSED", "The server refused this request."],
    ];
    let posts = 0;
    for (const [err, word, text] of cases) {
      m.apiPost.mockRejectedValueOnce(err);
      press("START");
      posts += 1;
      const box = await screen.findByRole("alert");
      await waitFor(() => expect(within(box).getByText(word)).toBeInTheDocument());
      expect(within(box).getByText(text)).toBeInTheDocument();
      expect(m.apiPost).toHaveBeenCalledTimes(posts);
      await waitFor(() => expect(start()).toBeEnabled());
    }
  });

  it("w9 no snake_case and no em dash on the window", async () => {
    await ready();
    await fillTwo();
    const text = dlg().textContent ?? "";
    expect(text).not.toContain(EM_DASH);
    expect(text).not.toMatch(/[a-z]+_[a-z]+/);
  });
});
