import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../lib/api";
import type { CalendarEvent } from "../../types";
import Calendar from "../pages/calendar/Calendar";
import { SettingsProvider } from "../settings";
import { APPS } from "../shell/sections";

// FULL SCREEN: the browser's full screen on the page with its own background, 06:00 to 23:00 fitted to the height.

const app = APPS.find((a) => a.id === "calendar")!;
const section = app.sections.find((s) => s.id === "calendar")!;

const ev = (id: string, title: string, start: string, end: string): CalendarEvent => ({
  event_id: id, calendar_id: "primary", title, start_dt: start, end_dt: end, all_day: 0, location: null,
  description: null, color_id: null, html_link: null, synced_at: "2026-10-07T08:00:00", local_only: 0,
});

let fsEl: Element | null = null;
let hoursHeight = 0;
const requestFullscreen = vi.fn();
const exitFullscreen = vi.fn();

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SettingsProvider>
          <Calendar app={app} section={section} />
        </SettingsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const page = () => document.querySelector(".v2-page") as HTMLElement;
const enterFull = () => {
  fsEl = page();
  fireEvent(document, new Event("fullscreenchange"));
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  fsEl = null;
  hoursHeight = 0;
  requestFullscreen.mockReset().mockResolvedValue(undefined);
  exitFullscreen.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => fsEl });
  Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exitFullscreen });
  Object.defineProperty(HTMLElement.prototype, "requestFullscreen", { configurable: true, value: requestFullscreen });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) { return this.getAttribute("data-testid") === "hours-scroll" ? hoursHeight : 0; },
  });
  const ctx = { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "calendarRange").mockResolvedValue([ev("1", "Standup", "2026-10-07T09:00:00", "2026-10-07T10:00:00")]);
  vi.spyOn(api.life, "weather").mockResolvedValue({} as never);
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (document as unknown as Record<string, unknown>).fullscreenElement;
  delete (document as unknown as Record<string, unknown>).exitFullscreen;
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).requestFullscreen;
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientHeight;
});

describe("Calendar full screen", () => {
  it("(a) FULL SCREEN asks the browser to show the page alone", async () => {
    mount();
    await screen.findByText("Standup");
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(requestFullscreen).toHaveBeenCalledTimes(1);
    expect(requestFullscreen.mock.contexts[0]).toBe(page());
  });

  it("(b) in full screen the week's 12 hours fit the hour area's height", async () => {
    hoursHeight = 850;
    mount();
    await screen.findByText("Standup");
    expect(screen.getByTestId("hours-2026-10-07")).toHaveStyle({ height: `${12 * 48}px` });
    expect(page().querySelector(".aur")).toBeNull();
    enterFull();
    await waitFor(() => expect(page()).toHaveClass("calfull"));
    // The app's background is drawn inside the page, since full screen shows only the page.
    expect(page().querySelector(".aur")).not.toBeNull();
    // The hours run 08:00 to 20:00. 850 / 12 = 70 px an hour: the column is 840 tall, 09:00 to 10:00 starts 1 hour down.
    await waitFor(() => expect(screen.getByTestId("hours-2026-10-07")).toHaveStyle({ height: "840px" }));
    const block = screen.getByText("Standup").closest(".ev") as HTMLElement;
    expect(block).toHaveStyle({ top: "71px", height: "67px" });
    expect(document.querySelectorAll('[data-testid^="dayhead-"]')).toHaveLength(7);
    expect(screen.getByRole("button", { name: "Exit full screen" })).toBeInTheDocument();
  });

  it("(c) a very short screen keeps 36 px an hour and scrolls", async () => {
    hoursHeight = 300;
    mount();
    await screen.findByText("Standup");
    enterFull();
    await waitFor(() => expect(screen.getByTestId("hours-2026-10-07")).toHaveStyle({ height: `${12 * 36}px` }));
  });

  it("(d) EXIT FULL SCREEN leaves it, and leaving by Esc brings the normal page back", async () => {
    hoursHeight = 850;
    mount();
    await screen.findByText("Standup");
    enterFull();
    fireEvent.click(await screen.findByRole("button", { name: "Exit full screen" }));
    expect(exitFullscreen).toHaveBeenCalledTimes(1);
    fsEl = null;
    fireEvent(document, new Event("fullscreenchange"));
    await waitFor(() => expect(page()).not.toHaveClass("calfull"));
    expect(screen.getByTestId("hours-2026-10-07")).toHaveStyle({ height: `${12 * 48}px` });
    expect(screen.getByRole("button", { name: "Full screen" })).toBeInTheDocument();
  });

  it("(e) a browser that refuses full screen says so in the header", async () => {
    requestFullscreen.mockRejectedValue(new Error("Permissions check failed"));
    mount();
    await screen.findByText("Standup");
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(await screen.findByText("The browser refused full screen: Permissions check failed")).toBeInTheDocument();
  });
});
