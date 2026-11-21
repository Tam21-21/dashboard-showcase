import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Capital from "../pages/trading/Capital";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const getRealAccount = vi.fn();
const getReadiness = vi.fn();
const getRealLimits = vi.fn();
const putRealLimits = vi.fn();
const getKillState = vi.fn();
const armKill = vi.fn();
const verifyKill = vi.fn();
const disarmKill = vi.fn();

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

const app = APPS.find((a) => a.id === "trading")!;
const section = app.sections.find((s) => s.id === "capital")!;

const BAD_CHARS = new RegExp(`${String.fromCharCode(8212)}|_`);

const KEYS = [
  "per_order_notional_cap", "ticker_net_cap", "total_net_cap", "loss_limit_window", "loss_limit_since_go",
  "order_rate_cap", "reconciliation_max_age", "account_value_max_age", "kill_drill_max_age",
].map((k) => `real_trading.${k}`);
const UNITS = ["dollars", "dollars", "dollars", "dollars", "dollars", "orders per 60 seconds", "seconds", "seconds", "seconds"];
const limitsBody = (values: Array<number | null>) => ({
  limits: KEYS.map((key, i) => ({ key, value: values[i], unit: UNITS[i], meaning: `meaning of ${i}` })),
});
const NONE_SET = Array<number | null>(9).fill(null);

const FRESH = {
  account_id: "ACC1",
  ready_count: 0,
  approved_count: 0,
  items: [
    { item: "go", state: "not-yet", evidence: "No go is recorded for this account." },
    { item: "kill_switch", state: "not-ready", evidence: "Last drill never run; switch never armed." },
    { item: "limits", state: "not-set", evidence: "0 of 9 set." },
    { item: "broker", state: "not-connected", evidence: "No broker connection is set up yet." },
    { item: "approved_strategy", state: "none", evidence: "No strategy is approved for real money yet." },
  ],
};
const KILL = { account_id: "ACC1", state: "never-armed", commanded_action: null, last_verified_at: null,
  last_verified_outcome: null, drill_state: "never-drilled" };

const err = (status: number, detail: string) => new ApiError(`${status} x`, status, { detail }, "/api/real/x");

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Capital app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const card = (label: string) => {
  const el = Array.from(document.querySelectorAll(".tp-card")).find((c) => c.querySelector(".tp-name")?.textContent === label);
  if (!el) throw new Error(`no card ${label}`);
  return el as HTMLElement;
};
const fig = (c: HTMLElement) => c.querySelector(".tp-fig")!.textContent;
const panel = (title: string) => {
  const el = Array.from(document.querySelectorAll(".nl-blk")).find((c) => c.querySelector("h2")?.textContent?.toLowerCase().startsWith(title.toLowerCase()));
  if (!el) throw new Error(`no panel ${title}`);
  return within(el as HTMLElement);
};

beforeEach(() => {
  getRealAccount.mockResolvedValue({ account_id: "ACC1" });
  getReadiness.mockResolvedValue(FRESH);
  getRealLimits.mockResolvedValue(limitsBody(NONE_SET));
  getKillState.mockResolvedValue(KILL);
  putRealLimits.mockReset();
  armKill.mockReset();
  verifyKill.mockReset();
  disarmKill.mockReset();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Capital page", () => {
  it("c1 heading, the summary and its four cards on a fresh store", async () => {
    mount();
    expect(screen.getByRole("heading", { name: "Capital" })).toBeTruthy();
    await waitFor(() => expect(fig(card("Steps done"))).toBe("0"));
    expect(card("Steps done").textContent).toContain("of the 5 steps");
    await waitFor(() => expect(card("Server limits").textContent).toContain("9 not set"));
    expect(fig(card("Server limits"))).toBe("0");
    expect(document.querySelectorAll(".tp-card")).toHaveLength(4);
    for (const h of ["Needs you", "Summary", "Before the first real order", "Kill switch", "Server limits"]) panel(h);
  });

  it("c2 five checklist rows in order with state words and the evidence as title", async () => {
    mount();
    await panel("Before the first real order").findByText("Not yet");
    const p = panel("Before the first real order");
    const rows = Array.from(document.querySelectorAll(".cap-steps > div"));
    expect(rows).toHaveLength(5);
    expect(rows.map((r) => r.querySelector(".cap-t b")!.textContent)).toEqual([
      "Your go, recorded in the decisions log",
      "Kill switch on this dashboard",
      "Hard limits held by the server",
      "Broker account (Alpaca)",
      "A strategy approved for capital",
    ]);
    expect(rows.map((r) => r.querySelector(".nl-st")!.textContent)).toEqual(["Not yet", "Not ready", "Not set", "Not connected", "None"]);
    expect(p.getByTitle("No go is recorded for this account.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("IBKR");
    expect(rows[0].querySelector(".nl-st")!.className).toContain("warn");
    expect(rows[4].querySelector(".nl-st")!.className).toContain("dim");
  });

  it("c3 limits: nine rows in two groups, not set for nulls", async () => {
    mount();
    const p = panel("Server limits");
    await p.findByText("Order limits");
    expect(p.getByText("Freshness")).toBeTruthy();
    for (const n of ["Largest order", "Largest position in one stock", "Total invested", "Largest loss in a window",
      "Largest loss since go", "Orders per window", "Broker check at most", "Account value at most", "Kill drill at most"]) {
      expect(p.getByText(n)).toBeTruthy();
    }
    expect(p.getAllByText("Not set")).toHaveLength(9);
  });

  it("c4 EDIT then SAVE sends only the changed key, then refetches", async () => {
    const after = [250, ...NONE_SET.slice(1)];
    putRealLimits.mockResolvedValue({ ...limitsBody(after), enabled: false, enabled_reason: "x", superseded_enabling_record_ids: [] });
    mount();
    const p = panel("Server limits");
    fireEvent.click(await p.findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Real money limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "250" } });
    getRealLimits.mockResolvedValue(limitsBody(after));
    const before = getRealLimits.mock.calls.length;
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putRealLimits).toHaveBeenCalledWith({ "real_trading.per_order_notional_cap": 250 }));
    await waitFor(() => expect(getRealLimits.mock.calls.length).toBeGreaterThan(before));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect((await screen.findAllByText("1 of 9 set")).length).toBeGreaterThan(0);
  });

  it("c4b SAVE with no change sends no PUT, an emptied limit is blocked", async () => {
    getRealLimits.mockResolvedValue(limitsBody([100, ...NONE_SET.slice(1)]));
    mount();
    fireEvent.click(await panel("Server limits").findByRole("button", { name: "Edit" }));
    let dlg = within(screen.getByRole("dialog", { name: "Real money limits" }));
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(putRealLimits).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(panel("Server limits").getByRole("button", { name: "Edit" }));
    dlg = within(screen.getByRole("dialog", { name: "Real money limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    expect(dlg.getByRole("alert").textContent).toContain("cannot be emptied");
    expect(putRealLimits).not.toHaveBeenCalled();
  });

  it("c5 a superseded enabling record shows the go must be given again", async () => {
    putRealLimits.mockResolvedValue({ ...limitsBody([250, ...NONE_SET.slice(1)]), enabled: false,
      enabled_reason: "A limit changed after the go, so the go must be given again.", superseded_enabling_record_ids: [3] });
    mount();
    fireEvent.click(await panel("Server limits").findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Real money limits" }));
    fireEvent.change(dlg.getByLabelText("Largest order"), { target: { value: "250" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    const word = await screen.findByText("Your go must be given again");
    expect(word.className).toContain("warn");
    expect(word.closest("[title]")?.getAttribute("title")).toBe("A limit changed after the go, so the go must be given again.");
  });

  it("c6 a 422 keeps the dialog open with its sentence", async () => {
    putRealLimits.mockRejectedValue(err(422, "real-limit-value-invalid: real_trading.order_rate_cap"));
    mount();
    fireEvent.click(await panel("Server limits").findByRole("button", { name: "Edit" }));
    const dlg = within(screen.getByRole("dialog", { name: "Real money limits" }));
    fireEvent.change(dlg.getByLabelText("Orders per window"), { target: { value: "2.5" } });
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    const alert = await dlg.findByRole("alert");
    expect(alert.textContent).toBe("Each limit must be a number above 0, and orders per window a whole number.");
    expect(screen.getByRole("dialog", { name: "Real money limits" })).toBeTruthy();
    fireEvent.change(dlg.getByLabelText("Orders per window"), { target: { value: "3" } });
    putRealLimits.mockRejectedValue(err(422, "real-limit-unknown-key: x"));
    fireEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(dlg.getByRole("alert").textContent).toBe("That limit is not one the server knows."));
  });

  it("c7 ARM opens the confirm dialog, HALT posts, a 503 shows its detail and changes nothing", async () => {
    armKill.mockRejectedValue(err(503, "no execution adapter is configured for the real-trading router"));
    mount();
    const p = panel("Kill switch");
    await p.findAllByText("Never armed");
    const arm = p.getByRole("button", { name: "Arm" });
    expect(arm.className).toContain("danger");
    fireEvent.click(arm);
    const dlg = within(screen.getByRole("dialog"));
    fireEvent.click(dlg.getByRole("button", { name: "Halt" }));
    await waitFor(() => expect(armKill).toHaveBeenCalledWith({ account_id: "ACC1", action: "halt" }));
    expect((await dlg.findByRole("alert")).textContent).toBe("No broker is connected yet, so the switch cannot act.");
    expect(getKillState).toHaveBeenCalledTimes(1);
    expect(p.getAllByText("Never armed").length).toBeGreaterThan(0);
  });

  it("c10 FLATTEN posts flatten and CANCEL posts nothing", async () => {
    armKill.mockResolvedValue({ account_id: "ACC1", state: "armed-unverified", commanded_action: "flatten" });
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Arm" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(armKill).not.toHaveBeenCalled();
    fireEvent.click(p.getByRole("button", { name: "Arm" }));
    const flat = within(screen.getByRole("dialog")).getByRole("button", { name: "Flatten" });
    expect(flat.getAttribute("title")).toContain("closes every position");
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Halt" }).getAttribute("title")).toContain("cancels open ones");
    fireEvent.click(flat);
    await waitFor(() => expect(armKill).toHaveBeenCalledWith({ account_id: "ACC1", action: "flatten" }));
  });

  it("c11 after a 200 the kill state is read again and shown", async () => {
    armKill.mockResolvedValue({ account_id: "ACC1", state: "armed-unverified", commanded_action: "halt" });
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Arm" }));
    getKillState.mockResolvedValue({ ...KILL, state: "armed-unverified", commanded_action: "halt" });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Halt" }));
    await p.findAllByText("Armed, not checked");
    expect(getKillState).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("c8 no em dash or underscore on the page or in a title", async () => {
    mount();
    await panel("Server limits").findByText("Order limits");
    await panel("Kill switch").findAllByText("Never armed");
    fireEvent.click(panel("Server limits").getByRole("button", { name: "Edit" }));
    const titles = Array.from(document.querySelectorAll("[title], title")).map((e) => e.getAttribute("title") ?? e.textContent ?? "");
    const all = [document.body.textContent ?? "", ...titles].join("\n");
    expect(all).not.toMatch(BAD_CHARS);
  });

  it("c12 a limits row shows its unit and has its meaning as title", async () => {
    getRealLimits.mockResolvedValue(limitsBody([1000, 5000, 20000, 300, 1500, 10, 60, 120, 3600]));
    mount();
    const p = panel("Server limits");
    const row = (await p.findByText("Orders per window")).closest(".pp-lim")!;
    expect(row.textContent).toContain("10");
    expect(row.textContent).toContain("orders per 60 seconds");
    expect(row.querySelector("[title]")?.getAttribute("title")).toBe("meaning of 5");
    expect(p.queryByText("Not set")).toBeNull();
    await waitFor(() => expect(fig(card("Server limits"))).toBe("9"));
    expect(card("Server limits").textContent).not.toContain("not set");
    expect(card("Server limits").textContent).toContain("All set");
  });

  it("c13 a 503 on readiness gives dashes with the sentence as title", async () => {
    getReadiness.mockRejectedValue(err(503, "the real-trading store has no tables"));
    mount();
    expect((await screen.findAllByText(/could not read the checklist: the real-trading store has no tables/)).length).toBeGreaterThan(0);
    for (const label of ["Steps done", "Kill switch", "Strategies"]) {
      expect(fig(card(label))).toBe("-");
      expect(card(label).getAttribute("title")).toContain("the real-trading store has no tables");
    }
    await waitFor(() => expect(fig(card("Server limits"))).toBe("0"));
  });

  it("c14 kill switch steps and the strategies count", async () => {
    getReadiness.mockResolvedValue({
      account_id: "ACC1", ready_count: 3, approved_count: 2,
      items: [
        { item: "go", state: "recorded", evidence: "Your go is recorded and in force." },
        { item: "kill_switch", state: "not-ready", evidence: "x" },
        { item: "limits", state: "set", evidence: "9 of 9 set." },
        { item: "broker", state: "not-connected", evidence: "b" },
        { item: "approved_strategy", state: "approved", evidence: "2 approved." },
      ],
    });
    mount();
    await waitFor(() => expect(fig(card("Steps done"))).toBe("3"));
    expect(card("Steps done").textContent).toContain("Open2");
    expect(fig(card("Strategies"))).toBe("2");
    expect(document.querySelectorAll(".tp-card")).toHaveLength(4);
    expect(screen.getByText("2 approved").className).toContain("good");
  });

  it("c16 the strategies card: none yet at 0, approved at 1 or more, a dash when unknown", async () => {
    mount();
    await waitFor(() => expect(card("Strategies").textContent).toContain("None yet"));
    expect(fig(card("Strategies"))).toBe("0");
    cleanup();
    getReadiness.mockResolvedValue({
      ...FRESH,
      approved_count: 3,
      items: FRESH.items.map((i) => (i.item === "approved_strategy" ? { ...i, state: "approved", evidence: "3 approved." } : i)),
    });
    mount();
    await waitFor(() => expect(fig(card("Strategies"))).toBe("3"));
    expect(card("Strategies").textContent).toContain("Approved");
    cleanup();
    const { approved_count: _gone, ...noCount } = FRESH;
    getReadiness.mockResolvedValue(noCount);
    mount();
    await waitFor(() => expect(card("Strategies").getAttribute("title")).toBe("The checklist gave no count"));
    expect(fig(card("Strategies"))).toBe("-");
  });

  it("c18 the count comes from approved_count, never from the evidence text", async () => {
    getReadiness.mockResolvedValue({
      ...FRESH,
      approved_count: 1,
      items: FRESH.items.map((i) => (i.item === "approved_strategy" ? { ...i, state: "approved", evidence: "odd" } : i)),
    });
    mount();
    await waitFor(() => expect(fig(card("Strategies"))).toBe("1"));
    expect(screen.getByText("1 approved")).toBeTruthy();
  });

  it("c19 no account and the checklist unreadable: a dash with the reason, the other cards say no account", async () => {
    getRealAccount.mockResolvedValue({ account_id: null });
    getReadiness.mockRejectedValue(err(503, "the real-trading store could not be opened"));
    mount();
    await waitFor(() =>
      expect(card("Strategies").getAttribute("title")).toBe(
        "could not read the checklist: the real-trading store could not be opened",
      ),
    );
    expect(fig(card("Strategies"))).toBe("-");
    for (const label of ["Steps done", "Kill switch"]) {
      expect(card(label).textContent).toContain("No account");
    }
  });

  it("c15 loading and could not read states", async () => {
    getRealAccount.mockReturnValue(new Promise(() => {}));
    getRealLimits.mockReturnValue(new Promise(() => {}));
    mount();
    expect((await screen.findAllByText("Loading.")).length).toBeGreaterThan(0);
    cleanup();
    getRealAccount.mockRejectedValue(err(500, "boom"));
    getRealLimits.mockRejectedValue(err(503, "the real-trading store could not be opened"));
    mount();
    await screen.findAllByText(/could not read the account: boom/);
    await screen.findAllByText(/could not read the limits: the real-trading store could not be opened/);
    cleanup();
    getRealAccount.mockResolvedValue({ account_id: "ACC1" });
    getRealLimits.mockResolvedValue(limitsBody(NONE_SET));
    getKillState.mockRejectedValue(err(503, "the real-trading store answered with an error"));
    mount();
    expect((await screen.findAllByText(/could not read the kill switch: the real-trading store answered with an error/)).length).toBeGreaterThan(0);
  });

  it("c9 no account: plain state words, no routes, no ARM, limits still editable", async () => {
    getRealAccount.mockResolvedValue({ account_id: null });
    getReadiness.mockResolvedValue({ ...FRESH, account_id: null, approved_count: 1 });
    getRealLimits.mockResolvedValue(limitsBody([1, 2, 3, 4, 5, 6, 7, 8, 9]));
    mount();
    const lim = panel("Server limits");
    await lim.findByText("Freshness");
    expect(lim.getByRole("button", { name: "Edit" })).toBeTruthy();
    expect(lim.getByText("Largest order").closest(".pp-lim")!.textContent).toContain("1");
    for (const label of ["Steps done", "Kill switch"]) {
      expect(card(label).textContent).toContain("No account");
    }
    await waitFor(() => expect(fig(card("Strategies"))).toBe("1"));
    expect(card("Strategies").textContent).not.toContain("No account");
    expect(panel("Kill switch").getByText("No real account configured")).toBeTruthy();
    expect(panel("Before the first real order").getByText("No real account configured")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Arm" })).toBeNull();
    expect(getReadiness).toHaveBeenCalled();
    expect(getKillState.mock.calls.every((c) => c[0] !== "example-account")).toBe(true);
    expect(armKill).not.toHaveBeenCalled();
  });
});

const NO_BROKER = "No broker is connected yet, so the switch cannot act.";
const armed = (state: string) => ({ ...KILL, state, commanded_action: "halt" });

describe("Capital page, verify and disarm (UI41)", () => {
  it("UI41 c11 armed-verified-effective shows VERIFY and DISARM enabled before ARM", async () => {
    getKillState.mockResolvedValue(armed("armed-verified-effective"));
    mount();
    const p = panel("Kill switch");
    await p.findAllByText("Armed and working");
    const names = p.getAllByRole("button").map((b) => b.textContent);
    expect(names).toEqual(["Check", "Disarm", "Arm"]);
    expect((p.getByRole("button", { name: "Disarm" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("UI41 c12 armed-unverified shows DISARM disabled with a title", async () => {
    getKillState.mockResolvedValue(armed("armed-unverified"));
    mount();
    const p = panel("Kill switch");
    await p.findAllByText("Armed, not checked");
    const d = p.getByRole("button", { name: "Disarm" }) as HTMLButtonElement;
    expect(d.disabled).toBe(true);
    expect(d.getAttribute("title")).toBe("Only after a check has seen the switch work");
    expect((p.getByRole("button", { name: "Check" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("UI41 c13 never-armed and disarmed-verified-clear show neither", async () => {
    mount();
    const p = panel("Kill switch");
    await p.findAllByText("Never armed");
    expect(p.queryByRole("button", { name: "Check" })).toBeNull();
    expect(p.queryByRole("button", { name: "Disarm" })).toBeNull();
    cleanup();
    getKillState.mockResolvedValue(armed("disarmed-verified-clear"));
    mount();
    const q = panel("Kill switch");
    await q.findAllByText("Off, checked clear");
    expect(q.queryByRole("button", { name: "Check" })).toBeNull();
    expect(q.queryByRole("button", { name: "Disarm" })).toBeNull();
  });

  it("UI41 c14 DISARM dialog calls disarmKill and reads the kill state again; CANCEL calls nothing", async () => {
    getKillState.mockResolvedValue(armed("armed-verified-effective"));
    disarmKill.mockResolvedValue({ account_id: "ACC1", state: "disarmed-verified-clear" });
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Disarm" }));
    const dlg = within(screen.getByRole("dialog"));
    expect(dlg.getByText("Disarm the kill switch")).toBeTruthy();
    fireEvent.click(dlg.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(disarmKill).not.toHaveBeenCalled();
    fireEvent.click(p.getByRole("button", { name: "Disarm" }));
    getKillState.mockResolvedValue({ ...KILL, state: "disarmed-verified-clear" });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disarm" }));
    await waitFor(() => expect(disarmKill).toHaveBeenCalledWith({ account_id: "ACC1" }));
    await p.findAllByText("Off, checked clear");
    expect(getKillState).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("UI41 c15 VERIFY rejecting with a 503 shows the no-broker sentence under the panel and refetches nothing", async () => {
    getKillState.mockResolvedValue(armed("armed-unverified"));
    verifyKill.mockRejectedValue(err(503, "no execution adapter is configured for the real-trading router"));
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Check" }));
    await waitFor(() => expect(verifyKill).toHaveBeenCalledWith({ account_id: "ACC1" }));
    expect((await p.findByRole("alert")).textContent).toBe(NO_BROKER);
    expect(getKillState).toHaveBeenCalledTimes(1);
  });

  it("UI41 c16 VERIFY resolving reads the kill state again", async () => {
    getKillState.mockResolvedValue(armed("armed-unverified"));
    verifyKill.mockResolvedValue({ account_id: "ACC1", state: "armed-verified-effective" });
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Check" }));
    getKillState.mockResolvedValue(armed("armed-verified-effective"));
    await p.findAllByText("Armed and working");
    expect(getKillState).toHaveBeenCalledTimes(2);
  });

  it("UI41 c17 a non-ApiError rejection of disarmKill shows the plain sentence", async () => {
    getKillState.mockResolvedValue(armed("armed-verified-effective"));
    disarmKill.mockRejectedValue(new Error("boom"));
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Disarm" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disarm" }));
    expect((await within(screen.getByRole("dialog")).findByRole("alert")).textContent).toBe("Could not disarm the kill switch.");
    expect(getKillState).toHaveBeenCalledTimes(1);
  });

  it("UI41 a string detail from DISARM is shown as the server wrote it", async () => {
    getKillState.mockResolvedValue(armed("armed-verified-effective"));
    disarmKill.mockRejectedValue(err(409, "disarm needs a verified switch"));
    mount();
    const p = panel("Kill switch");
    fireEvent.click(await p.findByRole("button", { name: "Disarm" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disarm" }));
    expect((await within(screen.getByRole("dialog")).findByRole("alert")).textContent).toBe("disarm needs a verified switch");
  });
});
