import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { CalendarEvent, GarminActivity, GarminSnapshot, Todo } from "../../types";
import Home from "../pages/home/Home";
import { APPS, pageFor } from "../shell/sections";

const getRunningRuns = vi.fn();
const getSummary = vi.fn();
const fetchJudgeSummary = vi.fn();
const fetchNames = vi.fn();
const fetchOverview = vi.fn();
const fetchVerdicts = vi.fn();
const fetchDatasets = vi.fn();
const fetchStrategies = vi.fn();
const fetchRuns = vi.fn();
const fetchRunsSummary = vi.fn();

vi.mock("../data/paperApi", () => ({
  getRunningRuns: () => getRunningRuns(),
  getSummary: (w: string) => getSummary(w),
}));
vi.mock("../data/judgeApi", () => ({
  fetchJudgeSummary: () => fetchJudgeSummary(),
  fetchJudgeVerdicts: (...a: unknown[]) => fetchVerdicts(...a),
}));
vi.mock("../data/strategiesApi", async (orig) => {
  const real = await orig<typeof import("../data/strategiesApi")>();
  return { ...real, fetchStrategiesOverview: () => fetchOverview() };
});

vi.mock("../data/overviewApi", async (orig) => {
  const real = await orig<typeof import("../data/overviewApi")>();
  return { ...real, fetchDatasets: () => fetchDatasets(), fetchStrategies: () => fetchStrategies(), fetchRuns: () => fetchRuns(), fetchRunsSummary: () => fetchRunsSummary() };
});

vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => fetchNames() }) };
});

const app = APPS.find((a) => a.id === "home")!;
const section = app.sections.find((s) => s.id === "overview")!;
const LONG_DASH = String.fromCharCode(0x2014);

const ev = (id: string, title: string, start: string, allDay: 0 | 1 = 0, end = start): CalendarEvent => ({
  event_id: id, calendar_id: "primary", title, start_dt: start, end_dt: end, all_day: allDay, location: null,
  description: null, color_id: null, html_link: null, synced_at: "2026-10-11T08:00:00", local_only: 0,
});
const DAY_EVENTS = [
  ev("a", "Standup", "2026-10-11T09:00:00", 0, "2026-10-11T09:30:00"),
  ev("b", "Groceries (Lidl)", "2026-10-11T11:00:00", 0, "2026-10-11T12:00:00"),
  ev("c", "Dentist", "2026-10-11T15:00:00", 0, "2026-10-11T16:00:00"),
  ev("d", "Holiday", "2026-10-11", 1),
  ev("e", "Long run", "2026-10-13T18:30:00", 0, "2026-10-13T20:00:00"),
];

const series = (n: number, start: number): Array<[string, number]> =>
  Array.from({ length: n }, (_, i) => [`2026-10-${String(i + 1).padStart(2, "0")}T20:00:00Z`, start + i * 10]);
const run = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  paper_run_id: id, strategy_id: id, strategy_name: name, strategy_version: 1, source_run_id: 1,
  slice_opening_cash: 10000, expected_bounds: null, started_at: "2026-10-01T00:00:00Z", ended_at: null,
  end_reason: null, kill_started_at: null, kill_step: null,
  slice: { cash: 5000, position_value: 5500, equity: 10500, realised_pnl: 120, committed_capital: 5500, updated_at: "x" },
  equity_series: series(5, 10000), latest_promotion: null, ...extra,
});
const promo = { promotion_id: 1, requested_at: "x", state: "requested", decided_at: null, decision_ledger_entry: null };

const JUDGE = { store: { exists: true }, graded: 40, survivors: 5, awaiting_ok: 1, in_paper: 2, unjudgeable: 0 };
const SUMMARY = (window: string, extra: Record<string, unknown> = {}) => ({
  total_equity: 21000, total_opening_cash: 20000, delta_fraction: window === "today" ? 0.012 : 0.05,
  running_count: 2, promotions_requested_count: 1, trades_in_window: window === "today" ? 4 : 99,
  fees_paid_total: 0, window, ...extra,
});
const snap = (extra: Partial<GarminSnapshot> = {}) =>
  ({ date: "2026-10-11", sleep_duration_sec: 25920, sleep_deep_sec: 5000, sleep_rem_sec: 4000, resting_hr: 52, steps: 8123, ...extra }) as GarminSnapshot;
const act = (id: string, name: string, at: string, m: number | null, type_key = "running") =>
  ({ activity_id: id, name, start_time_local: at, distance_m: m, type_key }) as GarminActivity;
type Bar = { time: string; epoch_seconds: number; open: number; high: number; low: number; close: number; volume: number };
const bars2 = (prev: number, last: number): Bar[] =>
  [["2026-10-08", prev], ["2026-10-09", last]].map(([t, c]) => ({
    time: t as string, epoch_seconds: Date.parse(`${t}T00:00:00Z`) / 1000, open: c as number, high: c as number, low: c as number, close: c as number, volume: 1,
  }));
const BARS: Record<string, Bar[] | undefined> = { "GSPC.INDX": bars2(4000, 4040), "DXY.INDX": bars2(100, 99) };

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>;
}
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/app/home/overview"]}>
        <Routes><Route path="*" element={<><Home app={app} section={section} /><Where /></>} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
// Found by reading the attribute: jsdom's selector engine does not match "&" inside an attribute value.
const tile = (name: string) => Array.from(document.querySelectorAll("[data-tile]")).find((t) => t.getAttribute("data-tile") === name) as HTMLElement;
const card = (title: string) => screen.getByRole("heading", { name: title }).closest("section") as HTMLElement;
// 28 days to 11 Oct: read nights 1 to 8 Oct (resting heart rate 50 + day), a night the watch did not read on 9 Oct, nothing after.
const DAYS = Array.from({ length: 9 }, (_, i) => snap({
  date: `2026-10-0${i + 1}`, sleep_duration_sec: 25200 + i * 600, sleep_deep_sec: i === 8 ? 0 : 5000, sleep_rem_sec: i === 8 ? 0 : 4000,
  resting_hr: i === 8 ? null : 50 + i, sleep_score: 80,
}));
const td = (id: number, text: string, date: string, done: 0 | 1 = 0): Todo => ({
  id, text, done, position: id, date, done_at: null, created_at: "x", updated_at: "x",
});
const titles = (el: Element) => Array.from(el.querySelectorAll("[title]")).map((e) => e.getAttribute("title"));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 11, 10, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  fetchOverview.mockResolvedValue([
    { id: 1, name: "alpha_gap", version: 1, description: null, run: null },
    { id: 2, name: "beta_drift", version: 1, description: null, run: null },
    { id: 3, name: "gamma", version: 1, description: null, run: null },
  ]);
  fetchVerdicts.mockResolvedValue({ rows: [] });
  fetchDatasets.mockResolvedValue([{ status: "ok", dataset_type: "price_dataset", covered_end_ts: Math.floor(Date.UTC(2026, 9, 9) / 1000) }, { status: "ok", dataset_type: "price_dataset", covered_end_ts: Math.floor(Date.UTC(2026, 8, 1) / 1000) }]);
  fetchStrategies.mockResolvedValue([{ id: 1 }, { id: 2 }]);
  fetchRuns.mockResolvedValue([
    { id: 11, strategy_id: 1, created_at: 1760000000, status: "succeeded", verdict: "pass", error: null },
    { id: 12, strategy_id: 2, created_at: 1760001000, status: "succeeded", verdict: null, error: null },
  ]);
  fetchRunsSummary.mockResolvedValue({ total: 2 });
  vi.spyOn(api.life, "garminDays").mockResolvedValue({ configured: true, snapshots: DAYS });
  vi.spyOn(api.display, "getHistory").mockImplementation(async (symbol: string, period = "2y") => {
    const bars = BARS[symbol];
    if (!bars) throw new ApiError(`503 x - ${symbol}`, 503, { detail: `no data for ${symbol}` });
    return { symbol, period, bars, time_form: "daily" } as never;
  });
  vi.spyOn(api.display, "getYieldCurve").mockResolvedValue({
    tenors: [{ tenor: "10Y", level: 4.5, change_bps: 3, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" }],
  } as never);
  vi.spyOn(api.display, "getCommodities").mockResolvedValue({
    commodities: [{ name: "Crude Oil Prices: Brent", level: 90, change: 3, grain: "daily", observation_date: "2026-10-09", unit: "Dollars per Barrel" }],
  } as never);
  vi.spyOn(api.life, "calendarRange").mockResolvedValue(DAY_EVENTS);
  getRunningRuns.mockResolvedValue([run(1, "Alpha gap", { latest_promotion: promo }), run(2, "Beta drift")]);
  getSummary.mockImplementation((w: string) => Promise.resolve(SUMMARY(w)));
  fetchJudgeSummary.mockResolvedValue(JUDGE);
  fetchNames.mockResolvedValue([
    { id: 1, name: "alpha_gap", display_name: "Alpha gap", status: "active" },
    { id: 2, name: "beta_drift", display_name: "Beta drift", status: "active" },
  ]);
  vi.spyOn(api.life, "eveningSummary").mockResolvedValue({ summary: null });
  vi.spyOn(api.life, "calendarToday").mockResolvedValue(DAY_EVENTS);
  vi.spyOn(api.life, "getTodos").mockResolvedValue([
    td(2, "Book the dentist", "2026-10-11", 1), td(3, "Read Carver", "2026-10-11"),
    td(4, "Send the application", "2026-10-11"), td(5, "Buy a tube", "2026-10-11"), td(6, "Water the plants", "2026-10-11"),
  ]);
  vi.spyOn(api.life, "openTodos").mockResolvedValue([td(1, "Pay the phone bill", "2026-10-08")]);
  vi.spyOn(api.life, "doneLateTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "garminToday").mockResolvedValue({ configured: true, snapshot: snap({ vo2max: 57, vo2max_as_of: "2026-10-06", race_5k_sec: 1288, race_10k_sec: 2811, race_half_sec: null, race_marathon_sec: 14474 }) });
  vi.spyOn(api.life, "garminActivities").mockResolvedValue({
    configured: true,
    activities: [
      act("1", "Old run", "2026-10-01T07:00:00", 5000), act("2", "Morning run", "2026-10-10T07:00:00", 8250),
      act("3", "Swim", "2026-10-07T07:00:00", 1500, "lap_swimming"), act("4", "No distance", "2026-10-06T07:00:00", null),
    ],
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  getRunningRuns.mockReset();
  getSummary.mockReset();
  fetchJudgeSummary.mockReset();
  fetchNames.mockReset();
  fetchOverview.mockReset();
  fetchVerdicts.mockReset();
  fetchDatasets.mockReset();
  fetchStrategies.mockReset();
  fetchRuns.mockReset();
  fetchRunsSummary.mockReset();
});

describe("Home overview, round six", () => {
  it("(a) is registered for home/overview", () => {
    expect(pageFor(app, section)).toBe(Home);
  });

  it("s0 the Summary block is the first child of the page after the header, above the opening sentence", async () => {
    mount();
    await screen.findByText("Nothing needs you today.").catch(() => null);
    const page = document.querySelector(".hm-page") as HTMLElement;
    const kids = Array.from(page.children);
    const at = kids.findIndex((k) => k.classList.contains("ph"));
    expect(kids[at + 1].getAttribute("aria-label")).toBe("Summary");
    expect(kids[at + 2].classList.contains("nl-say")).toBe(true);
  });

  it("n1 the finding counts what waits for you (the judge's approvals plus today's asks to leave paper), and each row opens its page", async () => {
    mount();
    expect(await screen.findByText("2 things need you today.")).toBeTruthy();
    const needs = screen.getByTestId("needs");
    const cells = Array.from(needs.querySelectorAll(".hm-n3 .hm-nk"));
    expect(cells.length).toBe(3);
    expect(cells.slice(0, 2).map((r) => r.textContent)).toEqual(["Verdicts to approve1Open", "Asking to leave paper1Open"]);
    expect(cells.slice(0, 2).map((r) => r.getAttribute("title"))).toEqual(["Runs that passed the judge and wait for your yes", "Paper strategies that ask to trade real money"]);
    expect(needs.querySelector("small")).toBeNull();
    const ask = await within(needs).findByText("Alpha gap");
    expect(ask.closest("p")!.textContent).toBe("Alpha gap asks to leave paper");
    expect(ask.closest(".hm-nopen")).not.toBeNull();
    expect(ask.getAttribute("title")).toContain("alpha_gap");
    fireEvent.click(within(needs).getAllByRole("link", { name: "Open" })[0]);
    expect(screen.getByTestId("where").textContent).toBe("/app/research/judge");
  });

  it("n2 nothing waits: says so, all clear, no Open link", async () => {
    fetchJudgeSummary.mockResolvedValue({ ...JUDGE, awaiting_ok: 0 });
    getSummary.mockImplementation((w: string) => Promise.resolve(SUMMARY(w, { promotions_requested_count: 0 })));
    mount();
    expect(await screen.findByText("Nothing needs you today.")).toBeTruthy();
    expect(await screen.findByText("1 late")).toBeTruthy();
    expect(within(screen.getByTestId("needs")).queryByRole("link", { name: "Open" })).toBeNull();
  });

  it("n3 a failed judge read says Home could not read what waits, in the server's words", async () => {
    fetchJudgeSummary.mockRejectedValue(new ApiError("500 /api/judge/summary", 500, { detail: "the judge store is locked" }));
    mount();
    expect(await screen.findByText("Home could not read what waits for you.")).toBeTruthy();
    expect(document.querySelector(".nl-say")!.textContent).toMatch(/the judge store is locked/i);
  });

  it("c1 today's events: named in the sentence without their room, listed as when, what and how long, the past one marked Over and the next one Next", async () => {
    mount();
    const day = await screen.findByTestId("day");
    expect(document.querySelector(".nl-say")!.textContent).toContain("Four things on the calendar: Holiday all day, Standup at 09:00, Groceries at 11:00 and Dentist at 15:00.");
    const rows = Array.from(day.querySelectorAll("tbody tr"));
    expect(rows.map((r) => r.textContent)).toEqual(["All dayHoliday-", "09:00 to 09:30Standup30 minOver", "11:00 to 12:00Groceries1 hNext", "15:00 to 16:00Dentist1 h"]);
    expect(rows[2].querySelector("td:nth-child(2)")!.getAttribute("title")).toBe("Groceries (Lidl), 11:00 to 12:00");
    expect(day.querySelector(".nl-blk-t, .hm-hd .nl-st")!.textContent).toBe("4 events");
  });

  it("c2 the week: seven capsules from today, as tall as the hours booked, a day without any says Free; pressing a day lists it", async () => {
    mount();
    const week = await screen.findByTestId("week");
    const caps = Array.from(week.querySelectorAll(".hm-cap"));
    expect(caps).toHaveLength(7);
    expect(caps[0].textContent).toBe("2 h 30Sun 11");
    expect(caps[1].textContent).toBe("FreeMon 12");
    expect(caps[2].textContent).toBe("1 h 30Tue 13");
    expect(caps[2].getAttribute("title")).toContain("Long run, 18:30 to 20:00");
    fireEvent.click(caps[2]);
    const rows = Array.from(screen.getByTestId("day").querySelectorAll("tbody tr"));
    expect(rows.map((r) => r.textContent)).toEqual(["18:30 to 20:00Long run1 h 30"]);
    fireEvent.click(caps[1]);
    expect(screen.getByTestId("day").textContent).toContain("Nothing is on the calendar this day.");
  });

  it("c3 an event keeps its own times in its label: a 5-minute call, and one that ends after midnight", async () => {
    vi.spyOn(api.life, "calendarRange").mockResolvedValue([
      ev("a", "Call", "2026-10-11T10:00:00", 0, "2026-10-11T10:05:00"), ev("b", "Night drive", "2026-10-11T23:00:00", 0, "2026-10-12T00:30:00"),
    ]);
    mount();
    const day = await screen.findByTestId("day");
    const rows = Array.from(day.querySelectorAll("tbody tr"));
    expect(rows.map((r) => r.querySelector("td")!.textContent)).toEqual(["10:00 to 10:05", "23:00 to 00:30 the next day"]);
  });

  it("w1 the watch: sleep from the newest read night, the unread and missing nights outlined with their reason, the quiet watch said once", async () => {
    mount();
    const bars = await screen.findByTestId("sleep");
    expect(card("Sleep").querySelector(".hm-fig")!.textContent).toBe("8h10");
    expect(card("Sleep").textContent).toContain("on 8 Oct, 7 h 35 on average");
    const all = Array.from(bars.querySelectorAll("i"));
    expect(all).toHaveLength(28);
    expect(all[25].className).toBe("none");
    expect(all[25].getAttribute("title")).toBe("9 Oct: the watch did not read this night");
    expect(all[27].getAttribute("title")).toBe("11 Oct: the watch has not sent this night");
    expect(all[24].getAttribute("title")).toBe("Night to 8 Oct: 8 h 10, sleep score 80");
    expect(within(card("Sleep")).getByText("Watch quiet since 9 Oct")).toBeTruthy();
    expect(document.querySelector(".nl-say")!.textContent).toContain("Your watch has sent nothing since 9 Oct.");
    expect(card("Resting heart rate").textContent).toContain("57bpm on 8 Oct, 54 on average");
  });

  it("w2 Garmin not connected: each watch card says so, and the sentence says nothing about the watch", async () => {
    vi.spyOn(api.life, "garminDays").mockResolvedValue({ configured: false, snapshots: [] });
    vi.spyOn(api.life, "garminToday").mockResolvedValue({ configured: false, snapshot: null });
    vi.spyOn(api.life, "garminActivities").mockResolvedValue({ configured: false, activities: [] });
    mount();
    await screen.findByTestId("day");
    await waitFor(() => expect(screen.getAllByText("Garmin is not connected")).toHaveLength(4));
    expect(document.querySelector(".nl-say")!.textContent).not.toContain("watch");
  });

  it("r1 running: twelve weeks Monday to Sunday, kilometres from the runs that have a distance, this week drawn white", async () => {
    mount();
    const bars = await screen.findByTestId("running");
    const all = Array.from(bars.querySelectorAll("i"));
    expect(all).toHaveLength(12);
    expect(all[11].className).toBe("now");
    expect(all[11].getAttribute("title")).toBe("Week of 5 Oct: 8.3 km in 2 runs (1 with no distance), 1 other session");
    expect(all[10].getAttribute("title")).toBe("Week of 28 Sep: 5.0 km in 1 run, 0 other sessions");
    expect(card("Running").textContent).toContain("8.3km this week, 5.0 the same days last week");
  });

  it("r3 last week is counted to the same weekday, as the Sports page does; the bar keeps the whole week", async () => {
    vi.setSystemTime(new Date(2026, 9, 10, 10, 0));
    vi.spyOn(api.life, "garminActivities").mockResolvedValue({
      configured: true, activities: [act("1", "Midweek", "2026-10-01T07:00:00", 5000), act("2", "Sunday long", "2026-10-04T07:00:00", 6000)],
    });
    mount();
    const bars = await screen.findByTestId("running");
    expect(card("Running").textContent).toContain("0.0km this week, 5.0 the same days last week");
    expect(Array.from(bars.querySelectorAll("i"))[10].getAttribute("title")).toBe("Week of 28 Sep: 11.0 km in 2 runs, 0 other sessions");
  });

  it("r2 race pace: VO2 max and each race the watch predicts, with its pace; a race it does not predict is left out", async () => {
    mount();
    const race = await screen.findByTestId("race");
    expect(Array.from(race.querySelectorAll("tbody tr")).map((r) => r.textContent)).toEqual(["5 km21:284:18", "10 km46:514:41", "Marathon4:01:145:43"]);
    expect(card("Race pace").textContent).toContain("57the watch's fitness score (VO2 max), 6 Oct");
  });

  it("l1 the lab: the six stations by the shared rule, each opening its page", async () => {
    mount();
    const lab = await screen.findByTestId("lab");
    await waitFor(() => expect(lab.querySelector('[data-station="data"]')!.className).toContain("s-warn"));
    expect(Array.from(lab.querySelectorAll(".hm-lname")).map((e) => e.textContent)).toEqual(["Data", "Strategies", "Runs", "Judge", "Paper", "Capital"]);
    expect(lab.querySelector('[data-station="data"]')!.getAttribute("title")).toContain("1 of 2 price series are behind");
    expect(lab.querySelector('[data-station="strategies"]')!.className).toContain("s-warn");
    expect(lab.querySelector('[data-station="paper"]')!.className).toContain("s-good");
    expect((lab.querySelector(".hm-lt i") as HTMLElement).style.width).toBe("100%");
    fireEvent.click(lab.querySelector('[data-station="judge"]')!);
    expect(screen.getByTestId("where").textContent).toBe("/app/research/judge");
  });

  it("t1 to do: the open count, done of total, the next three with the late one marked, and the late count", async () => {
    mount();
    const todo = await screen.findByTestId("todo");
    expect(todo.classList.contains("hm-nk")).toBe(true);
    expect(todo.querySelector("i")!.textContent).toBe("To do today");
    expect(todo.querySelector("b")!.textContent).toBe("5");
    expect(todo.getAttribute("title")).toBe("1 of 6 done, 1 late");
    const items = Array.from(screen.getByTestId("needs").querySelectorAll(".hm-nopen .hm-xn"));
    expect(items.map((i) => i.textContent)).toEqual(["Late: Pay the phone bill", "Read Carver", "Send the application"]);
    expect(items[0].querySelector(".warn")!.textContent).toBe("Late: ");
    expect(todo.getAttribute("href")).toBe("/app/calendar/calendar?view=today");
  });

  it("t2 a failed read of the late items ticked today says so instead of a count that leaves them out", async () => {
    vi.spyOn(api.life, "doneLateTodos").mockRejectedValue(new ApiError("500 /x", 500, { detail: "the to-do store is locked" }));
    mount();
    expect(await within(await screen.findByRole("heading", { name: "Needs you" }).then((h) => h.closest("section") as HTMLElement)).findByText(/the to-do store is locked/i)).toBeTruthy();
    expect(screen.queryByTestId("todo")).toBeNull();
  });

  it("r4 a pace is rounded as a whole, never to 60 seconds", async () => {
    vi.spyOn(api.life, "garminToday").mockResolvedValue({ configured: true, snapshot: snap({ vo2max: 57, race_5k_sec: 1498, race_10k_sec: null, race_half_sec: null, race_marathon_sec: null }) });
    mount();
    const race = await screen.findByTestId("race");
    expect(race.querySelector("tbody")!.textContent).toBe("5 km24:585:00");
  });

  it("m1 markets: the four tiles with last and day change, the source on hover; a failed series says why with no figure", async () => {
    BARS["DXY.INDX"] = undefined;
    try {
      mount();
      await waitFor(() => expect(tile("S&P 500").textContent).toContain("4,040.00"));
      expect(Array.from(document.querySelectorAll("[data-tile]")).map((t) => t.getAttribute("data-tile"))).toEqual(["S&P 500", "US 10-year yield", "Oil Brent", "Dollar index"]);
      expect(tile("S&P 500").textContent).toContain("+1.00%");
      expect(titles(tile("S&P 500"))).toContain("EODHD, close of 09/10/2026");
      expect(tile("S&P 500").querySelector("svg.hm-sp.up polyline")).not.toBeNull();
      await waitFor(() => expect(tile("Dollar index").textContent).toContain("no data for DXY.INDX"));
      expect(tile("Dollar index").textContent).not.toMatch(/[0-9]{2}\.[0-9]/);
    } finally {
      BARS["DXY.INDX"] = bars2(100, 99);
    }
  });

  it("s1 the mockup's sections, in order: Summary, Today, Next 7 days, Needs you, Pipeline overview, the four watch cards and Markets", async () => {
    mount();
    await screen.findByTestId("week");
    const heads = Array.from(document.querySelectorAll("h2")).map((h) => h.textContent);
    expect(heads).toEqual(["Summary", "Today", "Next 7 days", "Needs you", "Pipeline overview", "Sleep", "Resting heart rate", "Running", "Race pace", "Markets"]);
    expect(document.querySelector("h1")!.textContent).toBe("Home");
  });

  it("g1 guard: no long dash and no stored snake_case name on the page", async () => {
    mount();
    await screen.findByTestId("lab");
    await waitFor(() => expect(tile("S&P 500").textContent).toContain("4,040.00"));
    expect(document.body.textContent).not.toContain(LONG_DASH);
    expect(document.body.textContent).not.toMatch(/[a-z]+_[a-z]+/);
  });
});
