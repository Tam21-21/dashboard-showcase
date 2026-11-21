import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../lib/api";
import { CataloguePanel, MoneyPanel, ParametersPanel, QuickTestDash, RiskPanel } from "../pages/research/strategies/EditorPanels";

const fetchCatalogue = vi.fn();
const fetchSizing = vi.fn();
const fetchStrategy = vi.fn();
const fetchStatus = vi.fn();

vi.mock("../data/editorPanelsApi", () => ({
  editorPanelsKeys: {
    catalogue: ["editor", "catalogue"],
    sizingMethods: ["editor", "sizing-methods"],
    strategy: (id: number, v: number | null) => ["editor", "strategy", id, v],
    status: (id: number, v: number | null) => ["editor", "status", id, v],
  },
  fetchEditorCatalogue: (...a: unknown[]) => fetchCatalogue(...a),
  fetchSizingMethods: (...a: unknown[]) => fetchSizing(...a),
  fetchEditorStrategy: (...a: unknown[]) => fetchStrategy(...a),
  fetchEditorStatus: (...a: unknown[]) => fetchStatus(...a),
}));

const EM_DASH = String.fromCharCode(8212);
const CATALOGUE = {
  helpers: [
    { name: "default_costs", kind: "function", signature: "()", doc: "The cost model.\nSecond line." },
    { name: "EVENT_COLUMNS", kind: "constant", signature: null, doc: null },
  ],
  settings: [{ scope: "event_strategy", key: "vol_window", value: 20, value_type: "int" }],
  event_types: ["earnings", "split"],
  vbt_indicators: {
    indicators: [
      { name: "ATR", description: "Average true range.", inputs: ["high", "low", "close"], parameters: { window: "14" }, look_ahead: false },
      { name: "BOLB", description: "Bollinger band.", inputs: ["close"], parameters: {}, look_ahead: true },
    ],
  },
};
const SIZING = {
  methods: [{
    id: "vol_target_atr", description: "Sizes by volatility.",
    spec: { risk_fraction: 0.03, stop_multiple: 4, estimator: "atr", estimator_window: 278, hard_cap_fraction: 1 },
  }],
};

function wrap(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  fetchCatalogue.mockResolvedValue(CATALOGUE);
  fetchSizing.mockResolvedValue(SIZING);
  fetchStrategy.mockResolvedValue({ sizing_method: "vol_target_atr" });
  fetchStatus.mockResolvedValue({
    db_version: 3, param_mode: "x",
    sweep_params: [{ name: "lookback", type: "int" }, { name: "volume_x", type: "float" }, { name: "flag", type: "bool" }],
  });
});
afterEach(() => {
  cleanup();
  for (const m of [fetchCatalogue, fetchSizing, fetchStrategy, fetchStatus]) m.mockReset();
});

const row = (name: string | RegExp) => screen.findByRole("button", { name });

describe("Editor side panels", () => {
  it("p1 the catalogue lists four groups with counts and inserts the right text", async () => {
    const onInsert = vi.fn();
    const { container } = wrap(<CataloguePanel ready onInsert={onInsert} />);
    await row("ATR");
    const heads = Array.from(container.querySelectorAll(".ep-group")).map((e) => e.textContent);
    expect(heads).toEqual(["Indicators2", "Helpers2", "Settings1", "Event types2"]);
    fireEvent.click(await row("ATR"));
    expect(onInsert).toHaveBeenCalledTimes(1);
    expect(onInsert).toHaveBeenLastCalledWith("vbt.ATR.run(high, low, close)");
    expect(within(await row(/^BOLB/)).getByText("Look-ahead")).toBeInTheDocument();
    expect((await row("ATR")).getAttribute("title")).toBe("Average true range. Defaults: window=14");
    fireEvent.click(await row("default_costs"));
    expect(onInsert).toHaveBeenLastCalledWith("default_costs()");
    fireEvent.click(await row("EVENT_COLUMNS"));
    expect(onInsert).toHaveBeenLastCalledWith("EVENT_COLUMNS");
    fireEvent.click(await row(/^event_strategy\.vol_window/));
    expect(onInsert).toHaveBeenLastCalledWith('get_setting(settings, "event_strategy", "vol_window")');
    fireEvent.click(await row("earnings"));
    expect(onInsert).toHaveBeenLastCalledWith('"earnings"');
  });

  it("p2 search keeps matches, hides empty groups, says NO MATCH", async () => {
    wrap(<CataloguePanel ready onInsert={vi.fn()} />);
    await row("ATR");
    const box = screen.getByLabelText("Find in the catalogue");
    fireEvent.change(box, { target: { value: "atr" } });
    expect(screen.getByRole("button", { name: "ATR" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^BOLB/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "earnings" })).toBeNull();
    fireEvent.change(box, { target: { value: "zzz" } });
    expect(screen.getByText("NO MATCH")).toBeInTheDocument();
  });

  it("p3 rows are disabled while the editor is not ready", async () => {
    const onInsert = vi.fn();
    wrap(<CataloguePanel ready={false} onInsert={onInsert} />);
    const b = await row("ATR");
    expect(b).toBeDisabled();
    fireEvent.click(b);
    expect(onInsert).not.toHaveBeenCalled();
  });

  it("p4 the volatility target row shows its values, IN, and inserts on click and drag", async () => {
    const onMoney = vi.fn();
    wrap(<MoneyPanel strategyId={2} version={3} ready onMoney={onMoney} />);
    const b = await row(/^Volatility target/);
    await waitFor(() => expect(within(b).getByText("In the code")).toBeInTheDocument());
    for (const t of ["3%", "4 × volatility", "true range", "278 bars", "100% of the account"]) {
      expect(screen.getByText(t)).toBeInTheDocument();
    }
    expect(b.getAttribute("title")).toBe("Sizes by volatility.");
    fireEvent.click(b);
    expect(onMoney).toHaveBeenCalledTimes(1);
    expect(onMoney).toHaveBeenCalledWith("vol_target_atr");
    const setData = vi.fn();
    fireEvent.dragStart(b, { dataTransfer: { setData } });
    expect(setData).toHaveBeenCalledWith("application/x-sizing-method", "vol_target_atr");
    expect(setData).not.toHaveBeenCalledWith("text/plain", expect.anything());
  });

  it("p5 fixed fraction and equal slots are dashes with their words", async () => {
    wrap(<MoneyPanel strategyId={null} version={null} ready onMoney={vi.fn()} />);
    await row(/^Volatility target/);
    expect(screen.queryByText("In the code")).toBeNull();
    const ff = screen.getByText("Fixed fraction").closest("[title]") as HTMLElement;
    expect(ff.getAttribute("title")).toBe("Not on the server's sizing list, so a strategy cannot pick it yet. Event strategies size this way from their settings.");
    const es = screen.getByText("Equal slots").closest("[title]") as HTMLElement;
    expect(es.getAttribute("title")).toBe("Not on the server's sizing list, so a strategy cannot pick it yet. Generator theories size this way.");
    expect(screen.queryByRole("button", { name: /Fixed fraction/ })).toBeNull();
  });

  it("p6 six risk dashes, none draggable", () => {
    const { container } = render(<RiskPanel />);
    const names = ["Volatility stop", "Profit target", "Trailing stop", "Time limit", "Triple barrier", "Computed best exit"];
    const rows = Array.from(container.querySelectorAll(".ep-risk"));
    expect(rows.map((r) => r.querySelector("span")?.textContent)).toEqual(names);
    for (const r of rows) {
      expect(r.getAttribute("draggable")).toBeNull();
      expect(r.getAttribute("title")?.startsWith("No server list of exits yet, so a strategy cannot pick this here. Today ")).toBe(true);
      expect(r.querySelector("b")?.textContent).not.toBe("");
    }
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(rows[0].getAttribute("title")).toContain("the stop distance is set by the volatility target money method.");
  });

  it("p7 parameters show names, types, version and the sweep dash", async () => {
    wrap(<ParametersPanel strategyId={2} version={3} />);
    await screen.findByText("lookback");
    expect(screen.getByText("Version 3")).toBeInTheDocument();
    const tr = (n: string) => screen.getByText(n).closest("tr") as HTMLElement;
    expect(within(tr("lookback")).getByText("whole number")).toBeInTheDocument();
    expect(within(tr("volume_x")).getByText("number")).toBeInTheDocument();
    const bad = within(tr("flag")).getByText("UNKNOWN TYPE");
    expect(bad.closest("[title]")?.getAttribute("title")).toBe("The sweep takes int or float; this code declares bool");
    const dash = within(tr("lookback")).getByText("-");
    expect(dash.closest("[title]")?.getAttribute("title")).toBe("The code declares a name and a type; the values to try are set in the sweep window");
  });

  it("p7b parameters for a new strategy and for an empty list", async () => {
    wrap(<ParametersPanel strategyId={null} version={null} />);
    expect(screen.getByText("Save once to read the parameters the code declares")).toBeInTheDocument();
    expect(fetchStatus).not.toHaveBeenCalled();
    cleanup();
    fetchStatus.mockResolvedValue({ db_version: 1, param_mode: "x", sweep_params: [] });
    wrap(<ParametersPanel strategyId={5} version={1} />);
    expect(await screen.findByText("The code declares no parameters to sweep")).toBeInTheDocument();
  });

  it("p8 a failed catalogue read is a dash with the server's words", async () => {
    fetchCatalogue.mockRejectedValue(new ApiError("500 x /u", 500, { detail: "The catalogue is down." }, "/u"));
    wrap(<CataloguePanel ready onInsert={vi.fn()} />);
    const dash = await screen.findByText("-");
    expect(dash.getAttribute("title")).toBe("The catalogue is down.");
  });

  it("p9 quick test is disabled with its reason and calls nothing", () => {
    render(<QuickTestDash />);
    const b = screen.getByRole("button", { name: "Quick test" });
    expect(b).toBeDisabled();
    expect(b.parentElement?.getAttribute("title")).toBe("The server cannot run a quick test on one stock yet");
    expect(fetchCatalogue).not.toHaveBeenCalled();
  });

  it("p10 no snake_case outside code names, no em dash", async () => {
    const { container } = wrap(
      <div>
        <CataloguePanel ready onInsert={vi.fn()} />
        <MoneyPanel strategyId={2} version={3} ready onMoney={vi.fn()} />
        <RiskPanel />
        <ParametersPanel strategyId={2} version={3} />
      </div>,
    );
    await screen.findByText("lookback");
    await row(/^Volatility target/);
    await waitFor(() => expect(within(container).getByText("In the code")).toBeInTheDocument());
    const clone = container.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("[data-code]").forEach((e) => e.remove());
    const hovers = Array.from(clone.querySelectorAll("[title]")).map((e) => e.getAttribute("title") ?? "");
    const text = [clone.textContent ?? "", ...hovers].join(" ");
    expect(text).not.toMatch(/[a-z0-9]+_[a-z0-9_]+/);
    expect(text).not.toContain(EM_DASH);
    expect(container.textContent).not.toContain(EM_DASH);
  });
});
