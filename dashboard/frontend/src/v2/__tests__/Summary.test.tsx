import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { EveningSummary } from "../../types";
import Summary from "../pages/home/summary/Summary";

// Every figure here is SYNTHETIC: the mockup's own example figures (ES1a's e1 answer for 2026-11-14), never a real read.
const LONG_DASH = String.fromCharCode(0x2014);
const STEPS = [10177, 15265, 12365, 20163, 9840, 13120, 8760, 11460];
const MOVE = [634, 700, 546, 1160, 498, 667, 431, 742];
const FULL: EveningSummary = {
  day: "2026-11-14", made_at: "2026-11-14T21:00:00Z", goal: 10000, watch_last: "2026-11-14",
  body: {
    days: STEPS.map((steps, i) => ({ d: `2026-11-${String(7 + i).padStart(2, "0")}`, steps, rest: 1878, move: MOVE[i] })),
    steps: 11460, goal: 10000, reached: true, short: -1460, share: 1.146, prev: 8760, avg_steps: 12812.86, avg_kcal: 2540.29, kcal: 2620, rest: 1878, move: 742,
  },
  money: {
    today: 12.4, in: 0, budget: 500, before: 225.6, spent: 238, left: 17, share: 16.14, gap: 3.74, under: true, pace: 233.33, st: "ok",
    since: null, read_at: "2026-11-14T18:07:00Z", bank_as_of: "2026-11-14T04:00:00Z",
  },
  plan: {
    events: [{ title: "Swim", all_day: false, start: "2026-11-15T09:00:00+01:00", end: "2026-11-15T10:00:00+01:00" }],
    first: "Swim", todos: ["Review calculus notes", "Wash the running kit"], late: ["Reply to the landlord"],
  },
  verdict: { budget: true, steps: true },
  round: { today: 0.87, month: 8.25, total: 64.35, days: [0.42, 1.1, 0, 0.65, 0.38, 0.91, 0.24, 0.73, 0.55, 0, 1.3, 0.62, 0.48, 0.87] },
};
// jsdom cannot draw on a canvas: this stand-in takes every drawing call and ignores it.
const pen: unknown = new Proxy(function () {}, { get: () => pen, set: () => true, apply: () => pen });
function stubCanvas() { vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((() => pen) as never); }
const clone = (): EveningSummary => JSON.parse(JSON.stringify(FULL)) as EveningSummary;

const EVENING = "2026-11-14T21:05:00Z"; // 22:05 in Paris
const NEXT_MORNING = "2026-11-15T09:00:00Z"; // 10:00 in Paris

function mount(summary: EveningSummary | null, at = EVENING) {
  vi.setSystemTime(new Date(at));
  vi.spyOn(api.life, "eveningSummary").mockResolvedValue({ summary });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><Summary /></MemoryRouter></QueryClientProvider>);
}
const names = () => Array.from(document.querySelectorAll(".sm-card .t-name")).map((e) => e.textContent);
const cardOf = (k: string) => document.querySelector(`[data-k="${k}"]`) as HTMLElement;
const say = () => (document.querySelector(".sm-say") as HTMLElement).textContent as string;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  stubCanvas();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("Summary block", () => {
  it("s1: made this evening it is open, with the five cards, the words and the figures", async () => {
    mount(clone());
    await screen.findByText("Saturday 14 November");
    expect(screen.getByText("Updated 22:00")).toBeTruthy();
    expect(say()).toBe("You stayed on budget and moved enough.");
    expect(names()).toEqual(["Out of your accounts", "Out this month", "Calories burned", "Steps", "Tomorrow's plan", "Round-ups"]);
    for (const w of ["€3.74 left", "On pace", "Above usual", "Goal reached", "1 late", "€12.40", "of €16.14", "€238", "of €500", "2,620", "11,460", "of 10,000", "115%", "+€0.87", "today", "Total €64.35"]) {
      expect(screen.getAllByText(w).length, w).toBeGreaterThan(0);
    }
    expect(screen.getByRole("button", { name: "Hide the cards" })).toBeTruthy();
  });

  it("s2: made on an earlier day it is folded, and one press opens the cards", async () => {
    mount(clone(), NEXT_MORNING);
    await screen.findByText("Updated yesterday, 22:00");
    expect(say().startsWith("On Saturday you")).toBe(true);
    expect(Array.from(document.querySelectorAll(".sm-cell .t-name")).map((e) => e.textContent)).toEqual(["Out", "This month", "Burned", "Steps"]);
    expect(document.querySelectorAll(".sm-card").length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Show the cards" }));
    expect(document.querySelectorAll(".sm-card").length).toBe(6);
    expect(screen.getByRole("button", { name: "Hide the cards" })).toBeTruthy();
  });

  it("s3: nothing read says why in the mockup's sentences", async () => {
    const s = clone(); s.body = null; s.money = null; s.watch_last = "2026-11-10"; s.verdict = { budget: null, steps: null };
    mount(s);
    await screen.findByText("Saturday 14 November");
    expect(say()).toBe("Nothing to sum up tonight. No bank was read and your watch has sent nothing since 10 Nov.");
    for (const k of ["today", "month", "burn", "steps"]) expect(within(cardOf(k)).getByText("–")).toBeTruthy();
    expect(within(cardOf("today")).getByText("The bank was not read today, so no spending is shown.")).toBeTruthy();
    expect(within(cardOf("month")).getByText("The bank was not read, so the month's spending is not known.")).toBeTruthy();
    expect(within(cardOf("burn")).getByText(/since 10 Nov/)).toBeTruthy();
    expect(within(cardOf("today")).getByText("Not read")).toBeTruthy();
    expect(within(cardOf("steps")).getByText("Watch behind")).toBeTruthy();
  });

  it("s4: the money words", async () => {
    const s = clone(); s.money!.under = false; s.money!.gap = -4.2; s.money!.today = 20.34;
    mount(s);
    await screen.findByText("Saturday 14 November");
    expect(say()).toContain("went €4.20 over budget");
    expect(within(cardOf("today")).getByText("€4.20 over")).toBeTruthy();
    cleanup();
    const a = clone(); a.money!.st = "ahead";
    mount(a);
    expect(await screen.findByText("Ahead of pace")).toBeTruthy();
    cleanup();
    const o = clone(); o.money!.st = "over";
    mount(o);
    expect(await screen.findByText("Over budget")).toBeTruthy();
    cleanup();
    const sn = clone(); sn.money!.since = "2026-11-16";
    mount(sn);
    await screen.findByText("Saturday 14 November");
    expect(within(cardOf("month")).getByText("since 16 Nov")).toBeTruthy();
    cleanup();
    const inn = clone(); inn.money!.today = 0; inn.money!.in = 300;
    mount(inn);
    expect(await screen.findByText("Money in")).toBeTruthy();
  });

  it("s5: a step goal missed", async () => {
    const s = clone(); s.body!.steps = 8760; s.body!.reached = false; s.body!.short = 1240; s.body!.share = 0.876;
    mount(s);
    await screen.findByText("Saturday 14 November");
    expect(say()).toContain("moved less than your goal");
    expect(screen.getByText("1,240 short")).toBeTruthy();
  });

  it("s6: no Summary stored, and a failed load", async () => {
    mount(null);
    expect(await screen.findByText("The first Summary is made tonight at 22:00.")).toBeTruthy();
    cleanup();
    vi.spyOn(api.life, "eveningSummary").mockRejectedValue(new ApiError("503 x - locked", 503, { detail: "The summary store is locked" }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Summary /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("The summary store is locked")).toBeTruthy();
  });

  it("s7: a press on a card opens the numbers, Escape closes them", async () => {
    mount(clone());
    await screen.findByText("Saturday 14 November");
    fireEvent.click(cardOf("steps"));
    const win = await screen.findByRole("dialog", { name: "Steps" });
    expect(win.textContent).toContain("Your goal");
    expect(win.textContent).toContain("10,000");
    expect(win.textContent).toContain("12,813");
    expect(win.textContent).toContain("8,760");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("r2: no round-up read says so in the mockup's two sentences", async () => {
    const s = clone(); s.round = null;
    mount(s);
    await screen.findByText("Saturday 14 November");
    const c = cardOf("round");
    expect(within(c).getByText("–")).toBeTruthy();
    expect(within(c).getByText("Not read")).toBeTruthy();
    expect(within(c).getByText("No round-up was read today.")).toBeTruthy();
    cleanup();
    const n = clone(); n.round = null;
    mount(n, NEXT_MORNING);
    await screen.findByText("Updated yesterday, 22:00");
    fireEvent.click(screen.getByRole("button", { name: "Show the cards" }));
    expect(within(cardOf("round")).getByText("No round-up was read on Saturday.")).toBeTruthy();
  });

  it("r3: the day's change is written with its sign, and no sign on zero", async () => {
    const s = clone(); s.round!.today = -7.17;
    mount(s);
    await screen.findByText("Saturday 14 November");
    expect(within(cardOf("round")).getByText("\u2212\u20AC7.17")).toBeTruthy();
    cleanup();
    const z = clone(); z.round!.today = 0;
    mount(z);
    await screen.findByText("Saturday 14 November");
    expect(within(cardOf("round")).getByText("\u20AC0.00")).toBeTruthy();
  });

  it("r4: a press on the card opens the numbers behind it", async () => {
    mount(clone());
    await screen.findByText("Saturday 14 November");
    fireEvent.click(cardOf("round"));
    const win = await screen.findByRole("dialog", { name: "Round-ups" });
    expect(win.textContent).toContain("Invested this month");
    expect(win.textContent).toContain("\u20AC8.25");
    expect(win.textContent).toContain("In total");
    expect(win.textContent).toContain("\u20AC64.35");
  });

  it("r5: the folded strip still has exactly four results (guard)", async () => {
    mount(clone(), NEXT_MORNING);
    await screen.findByText("Updated yesterday, 22:00");
    expect(document.querySelectorAll(".sm-cell").length).toBe(4);
    expect(document.querySelector('[data-k="round"]')).toBeNull();
  });

  it("s8: no visible text holds an em dash or an underscore", async () => {
    mount(clone());
    await screen.findByText("Saturday 14 November");
    const text = document.body.textContent ?? "";
    expect(text.includes(LONG_DASH)).toBe(false);
    expect(text.includes("_")).toBe(false);
    cleanup();
    const s = clone(); s.body = null; s.money = null; s.watch_last = null;
    mount(s);
    await screen.findByText("Saturday 14 November");
    expect((document.body.textContent ?? "").includes(LONG_DASH)).toBe(false);
    expect((document.body.textContent ?? "").includes("_")).toBe(false);
  });
});
