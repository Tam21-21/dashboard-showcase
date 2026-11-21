import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import Capital from "../pages/trading/Capital";
import Paper from "../pages/trading/Paper";
import { APPS } from "../shell/sections";
import { STORAGE_KEY } from "../settings";
import { ApiError } from "../../lib/api";

// Lane PH4 (DECISIONS #484): Trading's Paper and Capital pages on a phone. The phone draws only
// STOP, HALT and FLATTEN, each behind a 1.5 second hold, and sends the requests the laptop sends.
const getRealAccount = vi.fn();
const getReadiness = vi.fn();
const getRealLimits = vi.fn();
const getKillState = vi.fn();
const armKill = vi.fn();
const verifyKill = vi.fn();
const disarmKill = vi.fn();
const putRealLimits = vi.fn();

vi.mock("../data/realApi", () => ({
  getRealAccount: () => getRealAccount(),
  getReadiness: () => getReadiness(),
  getRealLimits: () => getRealLimits(),
  putRealLimits: (b: unknown) => putRealLimits(b),
  getKillState: (id: string) => getKillState(id),
  armKill: (b: unknown) => armKill(b),
  verifyKill: (b: unknown) => verifyKill(b),
  disarmKill: (b: unknown) => disarmKill(b),
}));

const getRunningRuns = vi.fn();
const getSummary = vi.fn();
const getRunFills = vi.fn();
const getHoldings = vi.fn();
const getPaperLimits = vi.fn();
const killRun = vi.fn();
const promoteRun = vi.fn();
const fetchNames = vi.fn();

vi.mock("../data/paperApi", () => ({
  getRunningRuns: () => getRunningRuns(),
  getSummary: (w: string) => getSummary(w),
  getRunFills: (id: number) => getRunFills(id),
  getHoldings: (id: number) => getHoldings(id),
  getPaperLimits: () => getPaperLimits(),
  putPaperLimits: vi.fn(),
  killRun: (id: number) => killRun(id),
  promoteRun: (id: number) => promoteRun(id),
}));
vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => fetchNames() }) };
});

const app = APPS.find((a) => a.id === "trading")!;
const capSection = app.sections.find((s) => s.id === "capital")!;
const paperSection = app.sections.find((s) => s.id === "paper")!;

const KILL = (state: string) => ({ account_id: "ACC1", state, commanded_action: null, last_verified_at: null,
  last_verified_outcome: null, drill_state: "never-drilled" });
const err = (status: number, detail: string) => new ApiError(`${status} x`, status, { detail }, "/api/real/x");

const setLayout = (layout: "phone" | "desktop") => localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout }));

function mount(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}
const mountCapital = () => mount(<Capital app={app} section={capSection} />);
const mountPaper = () => mount(<Paper app={app} section={paperSection} />);

const isKillPanel = (c: Element) => c.querySelector("h2")?.textContent === "Kill switch";
const killPanel = () => {
  const el = Array.from(document.querySelectorAll(".nl-blk")).find(isKillPanel);
  if (!el) throw new Error("no kill switch panel");
  return within(el as HTMLElement);
};
const btn = (name: string) => screen.queryByRole("button", { name });

async function hold(name: string, ms: number) {
  vi.useFakeTimers();
  const b = screen.getByRole("button", { name });
  fireEvent.pointerDown(b);
  await act(async () => { vi.advanceTimersByTime(ms); });
  fireEvent.pointerUp(b);
  vi.useRealTimers();
}

const run = (id: number) => ({
  paper_run_id: id, strategy_id: id, strategy_name: "Alpha gap", strategy_version: 1, source_run_id: 1,
  slice_opening_cash: 10000, expected_bounds: null, started_at: "2026-10-01T00:00:00Z", ended_at: null,
  end_reason: null, kill_started_at: null, kill_step: null,
  slice: { cash: 5000, position_value: 5500, equity: 10500, realised_pnl: 120, committed_capital: 5500, updated_at: "x" },
  equity_series: [["2026-10-01T20:00:00Z", 10000], ["2026-10-02T20:00:00Z", 10100]],
  realised_result_counts: { win_count: 3, loss_count: 2 },
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  getRealAccount.mockResolvedValue({ account_id: "ACC1" });
  getReadiness.mockResolvedValue({ account_id: "ACC1", ready_count: 0, items: [] });
  getRealLimits.mockResolvedValue({ limits: [] });
  getKillState.mockResolvedValue(KILL("never-armed"));
  armKill.mockResolvedValue({});
  getRunningRuns.mockResolvedValue([run(7)]);
  getSummary.mockResolvedValue({ total_equity: 1, total_opening_cash: 1, delta_fraction: 0, running_count: 1,
    promotions_requested_count: 0, trades_in_window: 0, fees_paid_total: 0, window: "since_start" });
  getRunFills.mockResolvedValue({ fills: [], trade_stats: { win_count: 0, loss_count: 0 } });
  getHoldings.mockResolvedValue({ paper_run_id: 7, cash: 1, marked_equity: 1, rows: [], share_unavailable_reason: null,
    reconcile: [], alpaca_positions_unavailable_reason: null });
  getPaperLimits.mockResolvedValue({ limits: [{ key: "paper.per_order_notional_cap", value: 1000, unit: "usd", meaning: "x" }] });
  killRun.mockResolvedValue({ end_reason: "killed" });
  fetchNames.mockResolvedValue([{ id: 7, name: "alpha_gap", display_name: "Alpha gap", status: "active" }]);
});
afterEach(() => {
  vi.useRealTimers();
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [armKill, verifyKill, disarmKill, killRun, promoteRun, getKillState, getRealAccount]) m.mockReset();
});

describe("Trading on the phone", () => {
  it("k1 Capital: the KILL SWITCH panel is first and shows only the two hold buttons", async () => {
    setLayout("phone");
    mountCapital();
    const p = killPanel();
    await p.findByRole("button", { name: "Hold to halt" });
    expect(p.getByRole("button", { name: "Hold to flatten" })).toBeTruthy();
    for (const n of ["Arm", "Disarm", "Check", "Edit"]) expect(btn(n)).toBeNull();
    const first = document.querySelector(".tp-stack")!.querySelector(".nl-blk")!;
    expect(first.querySelector("h2")?.textContent).toContain("Kill switch");
    expect(Array.from(document.querySelectorAll(".nl-blk")).filter(isKillPanel)).toHaveLength(1);
  });

  it("k2 holding HALT 1500 ms posts halt once; 1000 ms posts nothing", async () => {
    setLayout("phone");
    mountCapital();
    await killPanel().findByRole("button", { name: "Hold to halt" });
    await hold("Hold to halt", 1000);
    expect(armKill).not.toHaveBeenCalled();
    await hold("Hold to halt", 1500);
    await waitFor(() => expect(armKill).toHaveBeenCalledTimes(1));
    expect(armKill).toHaveBeenCalledWith({ account_id: "ACC1", action: "halt" });
  });

  it("k3 holding FLATTEN posts flatten", async () => {
    setLayout("phone");
    mountCapital();
    await killPanel().findByRole("button", { name: "Hold to flatten" });
    await hold("Hold to flatten", 1500);
    await waitFor(() => expect(armKill).toHaveBeenCalledTimes(1));
    expect(armKill).toHaveBeenCalledWith({ account_id: "ACC1", action: "flatten" });
  });

  it("k4 an armed switch draws no hold button and says to disarm on the laptop", async () => {
    setLayout("phone");
    getKillState.mockResolvedValue(KILL("armed-verified-effective"));
    mountCapital();
    await killPanel().findByText("Already armed: disarm it on the laptop.");
    expect(btn("Hold to halt")).toBeNull();
    expect(btn("Hold to flatten")).toBeNull();
    expect(killPanel().getAllByText("Armed and working").length).toBeGreaterThan(0);
  });

  it("k5 a refused arm shows the server's words", async () => {
    setLayout("phone");
    armKill.mockRejectedValue(err(409, "The switch is busy with another command."));
    mountCapital();
    await killPanel().findByRole("button", { name: "Hold to halt" });
    await hold("Hold to halt", 1500);
    await killPanel().findByText("The switch is busy with another command.");
  });

  it("k6 Paper: HOLD TO STOP kills the selected run after 1500 ms; PROMOTE and EDIT are absent", async () => {
    setLayout("phone");
    mountPaper();
    await screen.findByRole("button", { name: "Hold to stop" });
    await screen.findByText("Largest order");
    expect(btn("Promote")).toBeNull();
    expect(btn("Edit")).toBeNull();
    expect(btn("Stop")).toBeNull();
    await hold("Hold to stop", 1000);
    expect(killRun).not.toHaveBeenCalled();
    await hold("Hold to stop", 1500);
    await waitFor(() => expect(killRun).toHaveBeenCalledTimes(1));
    expect(killRun).toHaveBeenCalledWith(7);
  });

  it("k6b a part-done stop keeps the laptop sentence", async () => {
    setLayout("phone");
    killRun.mockResolvedValue({ end_reason: null });
    mountPaper();
    await screen.findByRole("button", { name: "Hold to stop" });
    await hold("Hold to stop", 1500);
    await screen.findByText("The stop is only part done. Press Stop again to finish it.");
  });

  it("k8 while the account loads and with no account, no hold button is drawn", async () => {
    setLayout("phone");
    getRealAccount.mockReturnValue(new Promise(() => {}));
    mountCapital();
    expect(btn("Hold to halt")).toBeNull();
    expect(btn("Hold to flatten")).toBeNull();
    cleanup();
    getRealAccount.mockReset();
    getRealAccount.mockResolvedValue({ account_id: null });
    mountCapital();
    await killPanel().findAllByText("No real account configured");
    expect(btn("Hold to halt")).toBeNull();
    expect(btn("Hold to flatten")).toBeNull();
  });

  it("k7 guard: on the desktop the laptop buttons are drawn and no hold button is", async () => {
    setLayout("desktop");
    mountCapital();
    await killPanel().findByRole("button", { name: "Arm" });
    expect(btn("Edit")).not.toBeNull();
    expect(btn("Hold to halt")).toBeNull();
    cleanup();
    getKillState.mockResolvedValue(KILL("armed-verified-effective"));
    mountCapital();
    await killPanel().findByRole("button", { name: "Check" });
    expect(killPanel().getByRole("button", { name: "Disarm" })).toBeTruthy();
    cleanup();
    mountPaper();
    await screen.findByRole("button", { name: "Stop" });
    expect(btn("Promote")).not.toBeNull();
    expect(btn("Edit")).not.toBeNull();
    expect(btn("Hold to stop")).toBeNull();
  });
});
