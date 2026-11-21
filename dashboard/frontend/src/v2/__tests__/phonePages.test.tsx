import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import { api } from "../../lib/api";
import Calendar from "../pages/calendar/Calendar";
import EventDetail from "../pages/calendar/EventDetail";
import type { CalendarEvent } from "../../types";
import Home from "../pages/home/Home";
import Budget from "../pages/portfolio/Budget";
import Settings from "../pages/Settings";
import { SettingsProvider, STORAGE_KEY } from "../settings";
import { APPS } from "../shell/sections";

// The life pages on a phone (DECISIONS #484 (1), (3), (5)). Layout is forced through the stored setting,
// the way phone.test.tsx does it.
const getBudget = vi.fn();
vi.mock("../data/moneyApi", () => ({
  getBudget: () => getBudget(),
  putTarget: vi.fn(),
  deleteTarget: vi.fn(),
}));
vi.mock("../data/paperApi", () => ({
  getRunningRuns: () => Promise.resolve([]),
  getSummary: () => Promise.resolve({
    total_equity: 0, total_opening_cash: 0, delta_fraction: 0, running_count: 0,
    promotions_requested_count: 0, trades_in_window: 0, fees_paid_total: 0, window: "today",
  }),
}));
vi.mock("../data/judgeApi", () => ({
  fetchJudgeSummary: () => Promise.resolve({ store: { exists: true }, graded: 0, survivors: 0, awaiting_ok: 0, in_paper: 0, unjudgeable: 0 }),
  fetchJudgeVerdicts: () => Promise.resolve({ rows: [] }),
}));
vi.mock("../data/strategiesApi", async (orig) => {
  const real = await orig<typeof import("../data/strategiesApi")>();
  return { ...real, fetchStrategiesOverview: () => Promise.resolve([]) };
});
vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  return { ...real, useStrategyNames: () => ({ data: [], isError: false, error: null, isPending: false }) };
});

const calApp = APPS.find((a) => a.id === "calendar")!;
const calSection = calApp.sections.find((s) => s.id === "calendar")!;
const homeApp = APPS.find((a) => a.id === "home")!;
const homeSection = homeApp.sections.find((s) => s.id === "overview")!;
const pfApp = APPS.find((a) => a.id === "portfolio")!;
const budgetSection = pfApp.sections.find((s) => s.id === "budget")!;

function mount(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SettingsProvider>{ui}</SettingsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const setLayout = (layout: "phone" | "desktop") => localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout }));
const btn = (name: string) => screen.queryByRole("button", { name });

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "calendarRange").mockResolvedValue([]);
  vi.spyOn(api.life, "calendarToday").mockResolvedValue([]);
  vi.spyOn(api.life, "getTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "openTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "doneLateTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "weather").mockResolvedValue({} as never);
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([]);
  vi.spyOn(api.life, "garminToday").mockResolvedValue({ configured: false, snapshot: null } as never);
  vi.spyOn(api.life, "garminActivities").mockResolvedValue({ configured: false, activities: [] } as never);
  vi.spyOn(api.settings, "connections").mockResolvedValue({ alpaca: false, eodhd: false, garmin: false });
  vi.spyOn(api.settings, "connectionsLive").mockResolvedValue({
    checked_at: "2026-10-16T09:30:00Z",
    garmin: { configured: false, last_sync_at: null, last_sync_error: null, watch_data_until: null },
    alpaca: { configured: false, paper: true, status: null, trading_blocked: null, error: null },
    eodhd: { configured: false, requests_today: null, daily_limit: null, as_of: null, error: null },
  });
  vi.spyOn(api.display, "getHistory").mockRejectedValue(new Error("no data"));
  vi.spyOn(api.display, "getYieldCurve").mockResolvedValue({ tenors: [] } as never);
  vi.spyOn(api.display, "getCommodities").mockResolvedValue({ commodities: [] } as never);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ({ fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() })) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
  getBudget.mockResolvedValue({
    synced: false, read_at: null, stale: null, budget: null, reason: null, savings_eur: null,
    cushion_months: null, months: [], last_month: null, recurring: null,
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  getBudget.mockReset();
});

const BUDGET_WITH_PLAN = {
  synced: true, read_at: "2026-10-22T18:00:00Z", stale: false, reason: null,
  budget: {
    from: "2026-10-01", to: "2026-10-31", is_partial: true, elapsed_days: 22, total_days: 31,
    income: 3000, expenses: 1000, net_cashflow: 2000, savings_rate_percent: 60,
    categories: [{ slug: "transport", label: "Transport", income: 0, spent: 50, target: 200, pace: 100, ahead: -0.5, warn: false, suggestion: null }],
  },
  savings_eur: 40000, cushion_months: 8,
  months: [], last_month: null, recurring: null,
};

const pcat = (slug: string, label: string, spent: number, target: number | null, extra: Record<string, unknown> = {}) => ({
  slug, label, income: 0, spent, target, pace: target === null ? null : target / 2, ahead: null, warn: false, suggestion: null, ...extra,
});
const BUDGET_PHONE_ROWS = {
  ...BUDGET_WITH_PLAN,
  budget: {
    ...BUDGET_WITH_PLAN.budget,
    categories: [
      ...BUDGET_WITH_PLAN.budget.categories,
      pcat("leisure", "Leisure", 130, 100, { pace: 70, ahead: 0.25, warn: true }),
      pcat("coffee", "Coffee", 40, null, { suggestion: 120 }),
    ],
  },
};

describe("phone, life pages", () => {
  it("q1 Calendar on phone opens on TODAY even when the laptop's remembered view is WEEK, and offers TODAY, DAY, MONTH", async () => {
    sessionStorage.setItem("v2-memory:calendar/view", JSON.stringify({ v: { view: "WEEK", anchor: "2026-10-07" }, at: Date.now(), leftAt: null }));
    setLayout("phone");
    mount(<Calendar app={calApp} section={calSection} />);
    expect(await screen.findByText("To do")).toBeInTheDocument();
    for (const n of ["Today", "Day", "Month"]) expect(btn(n), n).not.toBeNull();
    expect(btn("Week")).toBeNull();
    expect(JSON.parse(sessionStorage.getItem("v2-memory:calendar/view")!).v.view).toBe("WEEK");
  });

  it("q2 Calendar on phone drops Templates and Ask Claude and keeps NEW EVENT", async () => {
    setLayout("phone");
    mount(<Calendar app={calApp} section={calSection} />);
    await screen.findByText("To do");
    expect(btn("New event")).not.toBeNull();
    expect(screen.queryByText("Templates")).toBeNull();
    expect(btn("Ask Claude")).toBeNull();
  });

  it("q3 guard: Calendar on desktop keeps Templates, Ask Claude and WEEK", async () => {
    setLayout("desktop");
    mount(<Calendar app={calApp} section={calSection} />);
    expect(await screen.findByText("Templates")).toBeInTheDocument();
    expect(btn("Ask Claude")).not.toBeNull();
    expect(btn("Week")).not.toBeNull();
  });

  it("q4 Budget on phone has no Edit plan button, on desktop it has", async () => {
    getBudget.mockResolvedValue(BUDGET_WITH_PLAN);
    setLayout("phone");
    mount(<Budget app={pfApp} section={budgetSection} />);
    await screen.findByTestId("plan-rows");
    expect(btn("Edit plan")).toBeNull();
    cleanup();
    setLayout("desktop");
    mount(<Budget app={pfApp} section={budgetSection} />);
    await screen.findByTestId("plan-rows");
    expect(btn("Edit plan")).not.toBeNull();
  });

  it("q5 guard: Home on phone renders the Needs you card with the to-do row, the week's seven days and the six lab stations", async () => {
    setLayout("phone");
    mount(<Home app={homeApp} section={homeSection} />);
    expect(await screen.findByRole("heading", { name: "Needs you" })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelectorAll(".hm-caps .hm-cap").length).toBe(7));
    expect(document.querySelectorAll(".hm-lab .hm-ls").length).toBe(6);
  });

  it("q6 guard: Settings on phone keeps the LAYOUT control", async () => {
    setLayout("phone");
    mount(<Settings />);
    expect(await screen.findByText("Layout")).toBeInTheDocument();
    expect(btn("Automatic")).not.toBeNull();
  });

  const EV: CalendarEvent = {
    event_id: "e1", calendar_id: "primary", title: "Standup", start_dt: "2026-10-07T09:00:00", end_dt: "2026-10-07T10:00:00",
    all_day: 0, location: null, description: null, color_id: null, html_link: null, synced_at: "2026-10-07T08:00:00", local_only: 0,
  };

  it("q7 EventDetail: EDIT TIME saves the drag's format, refuses an end before the start, none for all-day", async () => {
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue({} as never);
    const onError = vi.fn();
    mount(<EventDetail event={EV} onClose={vi.fn()} onError={onError} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit time" }));
    fireEvent.change(screen.getByLabelText("End"), { target: { value: "08:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByText("The end must be after the start.")).toBeInTheDocument();
    expect(move).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Start"), { target: { value: "14:00" } });
    fireEvent.change(screen.getByLabelText("End"), { target: { value: "15:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(move).toHaveBeenCalledWith("e1", { start_dt: "2026-10-07T14:00:00", end_dt: "2026-10-07T15:30:00" }));
    cleanup();
    mount(<EventDetail event={{ ...EV, all_day: 1, start_dt: "2026-10-07", end_dt: "2026-10-07" }} onClose={vi.fn()} onError={vi.fn()} />);
    expect(btn("Edit time")).toBeNull();
  });

  it("q8 on phone a click on a TODAY event row opens its EventDetail with DELETE and EDIT TIME", async () => {
    vi.spyOn(api.life, "calendarRange").mockResolvedValue([EV]);
    setLayout("phone");
    mount(<Calendar app={calApp} section={calSection} />);
    fireEvent.click(await screen.findByRole("button", { name: /Standup/ }));
    expect(await screen.findByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(btn("Edit time")).not.toBeNull();
  });

  it("pf5 q5 Budget on phone lists each category as a row with its amount and its state", async () => {
    getBudget.mockResolvedValue(BUDGET_PHONE_ROWS);
    setLayout("phone");
    mount(<Budget app={pfApp} section={budgetSection} />);
    const box = await screen.findByTestId("plan-rows");
    const rows = Array.from(box.querySelectorAll(".tp-barrow")) as HTMLElement[];
    expect(rows.map((r) => r.querySelector(".tp-n")!.textContent)).toEqual(["Transport", "Leisure", "Coffee"]);
    expect(rows[0].textContent).toContain("€50 of €200");
    expect(rows[1].textContent).toContain("€130 of €100");
    expect(rows[1].textContent).toContain("Over the plan");
    expect(rows[2].textContent).toContain("€40");
    expect(rows[2].textContent).toContain("average €120");
  });

  it("pf5 q6 guard: Budget on desktop draws the same rows", async () => {
    getBudget.mockResolvedValue(BUDGET_PHONE_ROWS);
    setLayout("desktop");
    mount(<Budget app={pfApp} section={budgetSection} />);
    const box = await screen.findByTestId("plan-rows");
    expect(box.querySelectorAll(".tp-barrow")).toHaveLength(3);
  });

  it("pf5 q7 Budget on phone with no categories says so", async () => {
    getBudget.mockResolvedValue({ ...BUDGET_WITH_PLAN, budget: { ...BUDGET_WITH_PLAN.budget, categories: [] } });
    setLayout("phone");
    mount(<Budget app={pfApp} section={budgetSection} />);
    expect(await screen.findByText("No spending categories this month.")).toBeInTheDocument();
  });
});
