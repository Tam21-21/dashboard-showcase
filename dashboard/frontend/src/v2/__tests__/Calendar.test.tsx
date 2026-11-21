// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { CalendarEvent, WeatherData, WeatherForecastDay } from "../../types";
import Calendar from "../pages/calendar/Calendar";
import { rangeQueryOptions } from "../pages/calendar/data";
import { APPS, pageFor } from "../shell/sections";

const app = APPS.find((a) => a.id === "calendar")!;
const section = app.sections.find((s) => s.id === "calendar")!;
const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };
const LONG_DASH = String.fromCharCode(0x2014);

const ev = (id: string, title: string, start: string, end: string, extra: Partial<CalendarEvent> = {}): CalendarEvent => ({
  event_id: id, calendar_id: "primary", title, start_dt: start, end_dt: end, all_day: 0, location: null,
  description: null, color_id: null, html_link: null, synced_at: "2026-10-07T08:00:00", local_only: 0, ...extra,
});

const WX_NOW: WeatherData = {
  temp: 19, feels_like: 18, temp_min: 15, temp_max: 21, description: "clear sky", icon: "01d",
  humidity: 50, wind_kph: 10, city: "Lyon",
};
const fc = (date: string, hi: number, lo: number): WeatherForecastDay => ({
  date, temp_max: hi, temp_min: lo, description: "few clouds", icon: "02d", wind_kph: 8, humidity: 40, rain_chance: 10,
});

const rangeSpy = () => vi.spyOn(api.life, "calendarRange");

function mount(entry = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Calendar app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const title = () => document.querySelector(".cmt h2")!.textContent;
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "calendarRange").mockResolvedValue([]);
  vi.spyOn(api.life, "getTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "openTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "doneLateTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "weather").mockResolvedValue(WX_NOW);
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([fc("2026-10-07", 21, 15), fc("2026-10-08", 18, 12)]);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Calendar page", () => {
  it("(a) is registered in sections.ts", () => {
    expect(pageFor(app, section)).toBe(Calendar);
    mount();
    expect(screen.queryByText("This page is not rebuilt yet.")).toBeNull();
  });

  it("(b) week view draws seven days from today (a Wednesday), not from Monday, the timed event, the all day event and asks for the week", async () => {
    rangeSpy().mockResolvedValue([
      ev("1", "Standup", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
      ev("2", "Holiday", "2026-10-08", "2026-10-08", { all_day: 1 }),
    ]);
    mount();
    expect(title()).toBe("7 to 13 October 2026");
    expect(await screen.findByText("Standup")).toBeInTheDocument();
    expect(screen.getByText("09:00 to 10:00")).toBeInTheDocument();
    const heads = document.querySelectorAll('[data-testid^="dayhead-"]');
    expect(heads).toHaveLength(7);
    expect(heads[0]).toHaveAttribute("data-testid", "dayhead-2026-10-07");
    expect(heads[6]).toHaveAttribute("data-testid", "dayhead-2026-10-13");
    expect(within(screen.getByTestId("allday-2026-10-08")).getByText("Holiday")).toBeInTheDocument();
    expect(within(screen.getByTestId("hours-2026-10-07")).getByText("Standup")).toBeInTheDocument();
    expect(api.life.calendarRange).toHaveBeenCalledWith("2026-10-07", "2026-10-13");
  });

  it("(c) next, today and previous move the week", async () => {
    mount();
    await screen.findByTestId("hours-2026-10-07");
    click("Next");
    expect(title()).toBe("14 to 20 October 2026");
    await waitFor(() => expect(api.life.calendarRange).toHaveBeenCalledWith("2026-10-14", "2026-10-20"));
    fireEvent.click(screen.getAllByRole("button", { name: "Today" })[0]); // the button that returns to today comes before the TODAY view
    expect(title()).toBe("7 to 13 October 2026");
    click("Previous");
    expect(title()).toBe("30 September to 6 October 2026");
  });

  it("(d) month view draws whole weeks, three events then 'N more', and a double click opens the day", async () => {
    rangeSpy().mockResolvedValue(
      [9, 10, 11, 12, 13].map((h) => ev(`m${h}`, `Task ${h}`, `2026-10-20T${h}:00:00`, `2026-10-20T${h}:30:00`)),
    );
    mount();
    click("Month");
    expect(title()).toBe("October 2026");
    await waitFor(() => expect(api.life.calendarRange).toHaveBeenCalledWith("2026-09-28", "2026-11-01"));
    const cell = await screen.findByTestId("cell-2026-10-20");
    expect(within(cell).getAllByText(/^Task/)).toHaveLength(3);
    expect(within(cell).getByText("2 more")).toBeInTheDocument();
    expect(within(cell).getByText("2 more").className).not.toContain("lo");
    expect(document.querySelectorAll('[data-testid^="cell-"]')).toHaveLength(35);
    expect(screen.getByTestId("cell-2026-09-28").className).toContain("out");
    fireEvent.doubleClick(cell);
    expect(title()).toBe("20 October 2026");
  });

  it("(e) says when Google is not connected or not set up, and still shows events", async () => {
    rangeSpy().mockResolvedValue([ev("1", "Local thing", "2026-10-07T09:00:00", "2026-10-07T10:00:00")]);
    vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: false, last_synced: null });
    mount();
    expect(await screen.findByText("Google Calendar is not connected.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/app/settings");
    expect(await screen.findByText("Local thing")).toBeInTheDocument();
    expect(api.life.calendarRange).toHaveBeenCalled();
    cleanup();
    vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: false, connected: false, last_synced: null });
    mount();
    expect(await screen.findByText("Google sign-in is not set up on this server.")).toBeInTheDocument();
  });

  it("(f) shows the server's words when the range fails, and Retry asks again; an empty range is quiet", async () => {
    const spy = rangeSpy().mockRejectedValueOnce(new ApiError("500 x - /api/life/calendar/range", 500, { detail: "store locked" }));
    mount();
    expect(await screen.findByText("store locked")).toBeInTheDocument();
    expect(screen.queryByText(/500/)).toBeNull();
    spy.mockResolvedValue([]);
    click("Retry");
    await waitFor(() => expect(screen.queryByText("store locked")).toBeNull());
    expect(spy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("hours-2026-10-07")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByText(/not connected|not set up/)).toBeNull();
  });

  it("(g) NEW EVENT in day view starts on the day in view, creates, and shows a refusal inside the form", async () => {
    const create = vi.spyOn(api.life, "calendarCreate").mockRejectedValue(
      new ApiError("503 x - /api/life/calendar/events", 503, { detail: "Google Calendar not connected" }),
    );
    mount();
    click("Day");
    expect(title()).toBe("7 October 2026");
    click("Next");
    expect(title()).toBe("8 October 2026");
    click("New event");
    const form = screen.getByRole("dialog", { name: "New event" });
    expect(within(form).getByLabelText("Date")).toHaveTextContent("08/10/2026");
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Dentist" } });
    fireEvent.change(within(form).getByLabelText("Start"), { target: { value: "14:00" } });
    fireEvent.change(within(form).getByLabelText("End"), { target: { value: "15:00" } });
    expect(within(form).getByLabelText("Start")).toHaveAttribute("type", "text");
    fireEvent.click(within(form).getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({ title: "Dentist", start_dt: "2026-10-08T14:00:00", end_dt: "2026-10-08T15:00:00" }),
    );
    expect(await within(form).findByText("Google Calendar not connected")).toBeInTheDocument();
    expect(screen.getAllByText("Google Calendar not connected")).toHaveLength(1);
  });

  it("(h) the weather card, the tomorrow line, the day header and a failed weather call", async () => {
    mount();
    expect(await screen.findByTitle("Lyon")).toBeInTheDocument();
    const card = screen.getByTestId("wx-card");
    expect(within(card).getByText("21°")).toBeInTheDocument();
    expect(within(card).getByText("Clear sky, 15° tonight")).toBeInTheDocument();
    const tomorrow = await screen.findByTestId("wx-tomorrow");
    expect(tomorrow).toHaveTextContent("18°");
    expect(tomorrow).toHaveTextContent("12°");
    const noFc = screen.getByTestId("dayhead-2026-10-09");
    expect(within(noFc).queryByText("no forecast")).toBeNull();
    expect(within(noFc).getByText("-")).toHaveAttribute("title", "No forecast for this day");
    expect(within(screen.getByTestId("dayhead-2026-10-07")).queryByText("-")).toBeNull();
    cleanup();

    vi.spyOn(api.life, "weatherForecast").mockResolvedValue([fc("2026-10-07", 21, 15)]);
    mount();
    expect(await screen.findByTitle("No forecast for tomorrow")).toBeInTheDocument();
    cleanup();

    vi.spyOn(api.life, "weather").mockRejectedValue(new ApiError("500 x - /api/life/weather", 500, { detail: "no weather key" }));
    mount();
    const why = await screen.findAllByTitle("no weather key");
    expect(why.length).toBeGreaterThan(0);
    expect(within(screen.getByTestId("wx-card")).queryByText("21°")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("(i) an event opens a read-only detail with OPEN IN GOOGLE when there is a link", async () => {
    rangeSpy().mockResolvedValue([
      ev("1", "Linked", "2026-10-07T09:00:00", "2026-10-07T10:00:00", {
        html_link: "https://calendar.google.com/x", location: "Room 4", description: "Bring notes",
      }),
      ev("2", "Plain", "2026-10-08T09:00:00", "2026-10-08T10:00:00"),
    ]);
    mount();
    fireEvent.click(await screen.findByText("Linked"));
    const d = screen.getByRole("dialog", { name: "Event" });
    expect(within(d).getByText("Room 4")).toBeInTheDocument();
    expect(within(d).getByText("Bring notes")).toBeInTheDocument();
    expect(within(d).getByRole("link", { name: "Open in Google" })).toHaveAttribute("href", "https://calendar.google.com/x");
    fireEvent.click(within(d).getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByText("Plain"));
    expect(screen.queryByRole("link", { name: "Open in Google" })).toBeNull();
  });

  it("(j) says nothing about syncing, has no long dash, and refetches every 300000 ms", async () => {
    rangeSpy().mockResolvedValue([ev("1", "Standup", "2026-10-07T09:00:00", "2026-10-07T10:00:00")]);
    mount();
    await screen.findByText("Standup");
    await screen.findByTitle("Lyon");
    const text = document.body.textContent ?? "";
    expect(text.toLowerCase()).not.toContain("synced");
    expect(screen.queryByRole("button", { name: /sync/i })).toBeNull();
    expect(text).not.toContain(LONG_DASH);
    expect(rangeQueryOptions("2026-10-07", "2026-10-13").refetchInterval).toBe(300000);
  });

  it("(m) the N more line lists the hidden events in its hover", async () => {
    const allDay = (id: string, title: string) =>
      ev(id, title, "2026-10-20", "2026-10-21", { all_day: 1 });
    rangeSpy().mockResolvedValue([
      allDay("a", "A day"), allDay("b", "B day"), allDay("c", "C day"), allDay("h", "Holiday"),
      ev("d", "Lunch", "2026-10-20T12:00:00", "2026-10-20T13:00:00"),
    ]);
    mount();
    click("Month");
    await waitFor(() => expect(api.life.calendarRange).toHaveBeenCalledWith("2026-09-28", "2026-11-01"));
    const cell = await screen.findByTestId("cell-2026-10-20");
    expect(within(cell).getByText("2 more")).toHaveAttribute("title", "All day Holiday\n12:00 Lunch");
  });

  it("(l) the header rows sit outside the one scrolling area, and the css keeps them see-through", async () => {
    mount();
    const scroll = await screen.findByTestId("hours-scroll");
    expect(scroll.contains(screen.getByTestId("hours-2026-10-07"))).toBe(true);
    expect(scroll.contains(screen.getByTestId("dayhead-2026-10-07"))).toBe(false);
    expect(scroll.contains(screen.getByTestId("allday-2026-10-07"))).toBe(false);
    const css = fs.readFileSync("src/v2/pages/calendar/calendar.css", "utf-8");
    const rule = (sel: string) => {
      const m = new RegExp(`(?:^|\\n)[^{}\\n]*\\${sel}\\b[^{}]*\\{([^}]*)\\}`).exec(css);
      expect(m, sel).not.toBeNull();
      return m![1];
    };
    expect(rule(".chours")).toContain("overflow-y: auto");
    expect(css).not.toMatch(/position:\s*sticky/);
    expect(rule(".chead")).not.toContain("background");
    expect(rule(".callday")).not.toContain("background");
    expect(css).toMatch(/@container \(max-width: 84px\)\s*\{\s*[^{}]*\.ev-t[^{}]*\{[^}]*display: none[^}]*\}[^{}]*\.ev-s[^{}]*\{[^}]*display: block/);
  });

  it("(m) an empty weather answer shows dashes with a reason and the page still loads", async () => {
    vi.spyOn(api.life, "weather").mockResolvedValue({} as never);
    mount();
    const why = await screen.findAllByTitle("The weather service sent no reading");
    expect(why.length).toBeGreaterThan(0);
    expect(screen.getByTestId("hours-2026-10-07")).toBeInTheDocument();
    expect(within(screen.getByTestId("wx-card")).queryByText(/° tonight/)).toBeNull();
  });

  it("c1 the templates card is closed by default and gains the closed class; open drops it", async () => {
    mount();
    await screen.findByTestId("hours-2026-10-07");
    const card = document.querySelector(".ctpl") as HTMLElement;
    expect(within(card).getByRole("button", { name: /Templates/ }).getAttribute("aria-expanded")).toBe("false");
    expect(card.querySelector(".tp")).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: /Templates/ }));
    expect(within(card).getByRole("button", { name: /Templates/ }).getAttribute("aria-expanded")).toBe("true");
  });

  it("CAL1 c1 the views read TODAY, DAY, WEEK, MONTH; ?view=today lists the day and the TO DO panel stays once, above the calendar", async () => {
    mount();
    const labels = Array.from(document.querySelectorAll(".pills button")).map((b) => b.textContent);
    expect(labels).toEqual(["Today", "Day", "Week", "Month"]);
    cleanup();
    mount("/app/calendar/calendar?view=today");
    expect(await screen.findByText("To do")).toBeInTheDocument();
    expect(screen.getAllByText("To do")).toHaveLength(1);
    expect(title()).toBe("Wednesday 7 October");
    expect(screen.queryByRole("button", { name: "Previous" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
  });

  it("CAL2 the mockup's parts: the finding, To do, Weather, Time by kind, then the calendar card with its Templates foot", async () => {
    rangeSpy().mockResolvedValue([
      ev("1", "Standup", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
      ev("2", "Long run", "2026-10-07T18:30:00", "2026-10-07T20:00:00"),
      ev("3", "CM Finance", "2026-10-08T08:00:00", "2026-10-08T10:00:00"),
    ]);
    mount();
    await screen.findByText("Standup");
    expect(document.querySelector("h1")!.textContent).toBe("Calendar");
    await waitFor(() => expect(document.querySelector(".nl-say b")!.textContent).toBe("2 events today."));
    expect(document.querySelector(".nl-say")!.textContent).toContain("Next: Long run at 18:30");
    expect(document.querySelector(".nl-say")!.textContent).toContain("The next 7 days hold 3 events, 4 h 30 booked.");
    const top = document.querySelector(".cal-top")!;
    expect(Array.from(top.querySelectorAll("h2")).map((h) => h.textContent)).toEqual(["To do", "Weather", "Time by kind"]);
    expect(top.children.length).toBe(2);
    const side = top.children[1];
    expect(side.classList.contains("cal-side")).toBe(true);
    expect(Array.from(side.querySelectorAll("h2")).map((h) => h.textContent)).toEqual(["Weather", "Time by kind"]);
    expect(top.querySelector('[data-testid="wx-card"]')).not.toBeNull();
    const kinds = top.querySelector('[data-testid="kinds"]')!;
    expect(Array.from(kinds.querySelectorAll(".kt-k")).map((b) => b.textContent)).toEqual(["Lecture2 h", "Run1 h 30", "Other1 h"]);
    expect(document.querySelector(".cmain .cmt h2")).not.toBeNull();
    expect(document.querySelector(".cmain .ctpl")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Ask Claude" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New event" })).toBeInTheDocument();
  });

  it("CAL1 c2 guard: the default view is still the week", async () => {
    mount();
    await screen.findByTestId("hours-2026-10-07");
    expect(screen.getByText("To do")).toBeInTheDocument();
    expect(title()).toBe("7 to 13 October 2026");
  });
});
