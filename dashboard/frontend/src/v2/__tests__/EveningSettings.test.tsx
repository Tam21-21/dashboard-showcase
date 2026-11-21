import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import Evening from "../pages/settings/Evening";

// SYNTHETIC stand-ins: the phone's Notification and pushManager are replaced, no real subscription is made.
const KEY_BYTES = Uint8Array.from({ length: 65 }, (_, i) => (i * 7 + 3) % 256);
const KEY_TEXT = btoa(String.fromCharCode(...KEY_BYTES)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const SUB = { endpoint: "https://push.example/abc", p256dh: "P256DH_KEY", auth: "AUTH_KEY" };
const EM_DASH = String.fromCharCode(0x2014);

function sub() {
  return {
    endpoint: SUB.endpoint,
    toJSON: () => ({ endpoint: SUB.endpoint, keys: { p256dh: SUB.p256dh, auth: SUB.auth } }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
}
type Stand = { permission: string; asked: string; existing: ReturnType<typeof sub> | null };
function stand(s: Partial<Stand> = {}) {
  const o: Stand = { permission: "default", asked: "granted", existing: null, ...s };
  const made = sub();
  const subscribe = vi.fn().mockResolvedValue(made);
  const getSubscription = vi.fn().mockImplementation(() => Promise.resolve(o.existing));
  Object.defineProperty(window, "pushManager", { configurable: true, value: { subscribe, getSubscription } });
  const N = {
    permission: o.permission,
    requestPermission: vi.fn().mockImplementation(() => { N.permission = o.asked; return Promise.resolve(o.asked); }),
  };
  Object.defineProperty(window, "Notification", { configurable: true, value: N });
  return { subscribe, getSubscription, N, made };
}

const life = api.life;
beforeEach(() => {
  vi.spyOn(life, "pushKey").mockResolvedValue({ key: KEY_TEXT });
  vi.spyOn(life, "pushSubscribe").mockResolvedValue({ ok: true });
  vi.spyOn(life, "pushUnsubscribe").mockResolvedValue({ ok: true });
  vi.spyOn(life, "pushState").mockResolvedValue({ known: true });
  vi.spyOn(life, "pushTest").mockResolvedValue({ sent: 1, gone: 0, failed: 0 });
  vi.spyOn(life, "summarySettings").mockResolvedValue({ steps_goal: 10000, monthly_budget: 500 });
  vi.spyOn(life, "saveSummarySettings").mockResolvedValue({ steps_goal: 12000 });
  vi.spyOn(life, "getBudgetConfig").mockResolvedValue({ id: 1, monthly_budget: 500, fixed_expenses: 1234.5, manual_monthly_spent: 77, updated_at: "2026-11-01T00:00:00Z" });
  vi.spyOn(life, "updateBudgetConfig").mockResolvedValue({ id: 1, monthly_budget: 450, fixed_expenses: 1234.5, manual_monthly_spent: 77, updated_at: "2026-11-19T00:00:00Z" });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Reflect.deleteProperty(window, "pushManager");
  Reflect.deleteProperty(window, "Notification");
});

const NOT_SET = "The evening notification is not set up on the server yet.";

describe("Evening summary: the button", () => {
  it("e1 a press asks the phone, subscribes with the server's key and tells the server", async () => {
    const s = stand();
    render(<Evening />);
    await screen.findByText("Off");
    fireEvent.click(await screen.findByRole("button", { name: "Turn on evening summary" }));
    await screen.findByText("On");
    expect(s.N.requestPermission).toHaveBeenCalledTimes(1);
    expect(s.subscribe).toHaveBeenCalledTimes(1);
    const arg = s.subscribe.mock.calls[0][0];
    expect(arg.userVisibleOnly).toBe(true);
    expect(Array.from(arg.applicationServerKey as Uint8Array)).toEqual(Array.from(KEY_BYTES));
    expect(life.pushSubscribe).toHaveBeenCalledWith({ endpoint: SUB.endpoint, keys: { p256dh: SUB.p256dh, auth: SUB.auth } });
    expect(screen.getByRole("button", { name: /Turn off/ })).toBeTruthy();
  });

  it("e2 a refusal shows how to allow it and never subscribes", async () => {
    const s = stand({ permission: "denied" });
    render(<Evening />);
    await screen.findByText("Refused");
    expect(screen.getByText(/open the iPhone's Settings, tap Notifications, pick this app and switch on Allow Notifications/)).toBeTruthy();
    expect(s.subscribe).not.toHaveBeenCalled();
  });

  it("e2b a phone that says no to the question ends as refused", async () => {
    const s = stand({ asked: "denied" });
    render(<Evening />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn on evening summary" }));
    await screen.findByText("Refused");
    expect(s.subscribe).not.toHaveBeenCalled();
    expect(life.pushSubscribe).not.toHaveBeenCalled();
  });

  it("e3 a subscription the server knows reads On; Send a test and Turn off say what happened", async () => {
    const existing = sub();
    stand({ permission: "granted", existing });
    render(<Evening />);
    await screen.findByText("On");
    expect(life.pushSubscribe).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Send a test" }));
    await screen.findByText("Sent. It should arrive in a few seconds.");
    expect(life.pushTest).toHaveBeenCalledTimes(1);
    vi.spyOn(life, "pushTest").mockRejectedValue(new ApiError("POST /x 503", 503, { detail: NOT_SET }));
    fireEvent.click(screen.getByRole("button", { name: "Send a test" }));
    await screen.findByText(NOT_SET);
    fireEvent.click(screen.getByRole("button", { name: /Turn off/ }));
    await screen.findByText("Off");
    expect(existing.unsubscribe).toHaveBeenCalledTimes(1);
    expect(life.pushUnsubscribe).toHaveBeenCalledWith({ endpoint: SUB.endpoint });
  });

  it("e4 a subscription the server does not know is sent again by itself", async () => {
    stand({ permission: "granted", existing: sub() });
    vi.spyOn(life, "pushState").mockResolvedValue({ known: false });
    render(<Evening />);
    await screen.findByText("On");
    await waitFor(() => expect(life.pushSubscribe).toHaveBeenCalledWith({ endpoint: SUB.endpoint, keys: { p256dh: SUB.p256dh, auth: SUB.auth } }));
  });

  it("e5 a browser with no pushManager says how to open the app and the button is off", async () => {
    render(<Evening />);
    await screen.findByText("Off");
    expect(screen.getByText("Open the app from its Home Screen icon on your iPhone to turn this on.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Turn on evening summary" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("e6 a server with no keys shows its words and the button is off", async () => {
    stand();
    vi.spyOn(life, "pushKey").mockRejectedValue(new ApiError("GET /k 503", 503, { detail: NOT_SET }));
    render(<Evening />);
    await screen.findByText(NOT_SET);
    expect((screen.getByRole("button", { name: "Turn on evening summary" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("Evening summary: the goal and the budget", () => {
  it("e7 the step goal saves, the server's refusal shows under it, the budget keeps the other two values", async () => {
    stand();
    render(<Evening />);
    const goal = (await screen.findByLabelText("Daily step goal")) as HTMLInputElement;
    await waitFor(() => expect(goal.value).toBe("10000"));
    fireEvent.change(goal, { target: { value: "12000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the step goal" }));
    await waitFor(() => expect(life.saveSummarySettings).toHaveBeenCalledWith({ steps_goal: 12000 }));
    const refused = "The step goal must be between 1000 and 60000.";
    vi.spyOn(life, "saveSummarySettings").mockRejectedValue(new ApiError("PUT /s 422", 422, { detail: refused }));
    fireEvent.change(goal, { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the step goal" }));
    await screen.findByText(refused);

    const budget = (await screen.findByLabelText("Monthly budget")) as HTMLInputElement;
    await waitFor(() => expect(budget.value).toBe("500"));
    fireEvent.change(budget, { target: { value: "450" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the monthly budget" }));
    await waitFor(() =>
      expect(life.updateBudgetConfig).toHaveBeenCalledWith({ monthly_budget: 450, fixed_expenses: 1234.5, manual_monthly_spent: 77 }),
    );
  });
});

describe("Evening summary: the words", () => {
  it("e8 the table holds the seven lines of the mockup and the banner shows the first", async () => {
    stand();
    const { container } = render(<Evening />);
    await screen.findByText("Off");
    const rows = Array.from(container.querySelectorAll("tbody tr")).map((r) => Array.from(r.querySelectorAll("td")).map((c) => c.textContent));
    const TAIL = "Open the app for all the details.";
    expect(rows).toEqual([
      ["Budget kept, step goal reached", `On track with your budget and your steps. ${TAIL}`],
      ["Over budget, step goal reached", `On track with your steps, over on your budget. ${TAIL}`],
      ["Budget kept, step goal missed", `On track with your budget, short on your steps. ${TAIL}`],
      ["Over budget, step goal missed", `Over on your budget and short on your steps. ${TAIL}`],
      ["Watch not synced", `On track with your budget. Your watch has not synced. ${TAIL}`],
      ["Bank not read", `On track with your steps. The bank was not read. ${TAIL}`],
      ["Nothing read", `Nothing could be read tonight. ${TAIL}`],
    ]);
    const banner = container.querySelector(".sm-note") as HTMLElement;
    expect(banner.textContent).toContain("Evening Summary");
    expect(banner.textContent).toContain(`On track with your budget and your steps. ${TAIL}`);
  });

  it("e9 no visible text holds an em dash or an underscore", async () => {
    stand();
    const { container } = render(<Evening />);
    await screen.findByText("Off");
    const text = container.textContent ?? "";
    expect(text.includes(EM_DASH)).toBe(false);
    expect(text.includes("_")).toBe(false);
  });
});

describe("Evening summary: only a plain number is saved", () => {
  it("f1 a goal or budget with a sign or a letter is refused with words and nothing is sent", async () => {
    stand();
    render(<Evening />);
    const goal = (await screen.findByLabelText("Daily step goal")) as HTMLInputElement;
    const budget = (await screen.findByLabelText("Monthly budget")) as HTMLInputElement;
    await waitFor(() => expect(goal.value).toBe("10000"));
    await waitFor(() => expect(budget.value).toBe("500"));

    fireEvent.change(goal, { target: { value: "-15000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the step goal" }));
    await screen.findByText("Type the steps as a whole number, with digits only.");
    fireEvent.change(goal, { target: { value: "12a" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the step goal" }));
    await screen.findByText("Type the steps as a whole number, with digits only.");
    expect(life.saveSummarySettings).not.toHaveBeenCalled();

    fireEvent.change(budget, { target: { value: "-450" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the monthly budget" }));
    await screen.findByText("Type the budget in euros, with digits and at most one dot.");
    fireEvent.change(budget, { target: { value: "12a" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the monthly budget" }));
    await screen.findByText("Type the budget in euros, with digits and at most one dot.");
    expect(life.updateBudgetConfig).not.toHaveBeenCalled();
  });

  it("f2 a plain goal and budget still save, the budget keeping the other two values", async () => {
    stand();
    render(<Evening />);
    const goal = (await screen.findByLabelText("Daily step goal")) as HTMLInputElement;
    const budget = (await screen.findByLabelText("Monthly budget")) as HTMLInputElement;
    await waitFor(() => expect(budget.value).toBe("500"));
    fireEvent.change(goal, { target: { value: "12000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the step goal" }));
    await waitFor(() => expect(life.saveSummarySettings).toHaveBeenCalledWith({ steps_goal: 12000 }));
    fireEvent.change(budget, { target: { value: "450" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the monthly budget" }));
    await waitFor(() =>
      expect(life.updateBudgetConfig).toHaveBeenCalledWith({ monthly_budget: 450, fixed_expenses: 1234.5, manual_monthly_spent: 77 }),
    );
  });
});
