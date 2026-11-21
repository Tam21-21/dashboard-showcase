import { cleanup, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import { KIT_ACCENTS } from "../pages/calendar/accent";
import type { CalendarEvent, EventTemplate } from "../../types";
import Calendar from "../pages/calendar/Calendar";
import { APPS } from "../shell/sections";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };

const app = APPS.find((a) => a.id === "calendar")!;
const section = app.sections.find((s) => s.id === "calendar")!;

const ev = (id: string, title: string, start: string, end: string, extra: Partial<CalendarEvent> = {}): CalendarEvent => ({
  event_id: id, calendar_id: "primary", title, start_dt: start, end_dt: end, all_day: 0, location: null,
  description: null, color_id: null, html_link: null, synced_at: "2026-10-07T08:00:00", local_only: 0, ...extra,
});
const tpl = (id: number, title: string, color: string): EventTemplate => ({ id, title, color, created_at: "2026-10-01T00:00:00" });

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Calendar app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// jsdom has no layout: every hours column starts at the top of the screen.
function stubTops() {
  document.querySelectorAll('[data-testid^="hours-"]').forEach((el) => {
    if (el.getAttribute("data-testid") === "hours-scroll") return;
    (el as HTMLElement).getBoundingClientRect = () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON() {} });
  });
}
const dt = (kind: string, id: string) => ({
  dataTransfer: { setData: vi.fn(), getData: (k: string) => (k === kind ? id : ""), effectAllowed: "", dropEffect: "" },
});
// jsdom has no DragEvent, so the generic event drops clientY from its init: set it by hand.
const fire = (name: "dragOver" | "drop", el: Element, kind: string, id: string, clientY: number) => {
  const event = createEvent[name](el, dt(kind, id));
  Object.defineProperty(event, "clientY", { value: clientY });
  fireEvent(el, event);
};
const dropOn = (testid: string, kind: string, id: string, clientY: number) => {
  const el = screen.getByTestId(testid);
  fire("dragOver", el, kind, id, clientY);
  fire("drop", el, kind, id, clientY);
};
const openTemplates = () => fireEvent.click(screen.getByRole("button", { name: /Templates/ }));
const blockOf = (title: string) => screen.getByText(title).closest(".ev") as HTMLElement;
const FRI = ev("m1", "Standup", "2026-10-09T09:00:00", "2026-10-09T10:30:00");

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "calendarRange").mockResolvedValue([FRI]);
  vi.spyOn(api.life, "weather").mockResolvedValue({} as never);
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([]);
  vi.spyOn(api.life, "getTemplates").mockResolvedValue([tpl(1, "Run", "#3ddbb4"), tpl(2, "Study", "#ffb000")]);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Calendar drag, templates and delete", () => {
  it("(a) a drop on Wednesday moves the event to the drop minute and keeps its length", async () => {
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue(FRI);
    mount();
    await screen.findByText("Standup");
    stubTops();
    expect(blockOf("Standup")).toHaveAttribute("draggable", "true");
    fireEvent.dragStart(blockOf("Standup"), dt("event-id", "m1"));
    dropOn("hours-2026-10-07", "event-id", "m1", 144);
    await waitFor(() => expect(move).toHaveBeenCalledWith("m1", { start_dt: "2026-10-07T11:00:00", end_dt: "2026-10-07T12:30:00" }));
  });

  it("(b) a 15 minute event keeps 15 minutes", async () => {
    vi.spyOn(api.life, "calendarRange").mockResolvedValue([ev("s1", "Quick", "2026-10-09T09:00:00", "2026-10-09T09:15:00")]);
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue(FRI);
    mount();
    await screen.findByText("Quick");
    stubTops();
    fireEvent.dragStart(blockOf("Quick"), dt("event-id", "s1"));
    dropOn("hours-2026-10-07", "event-id", "s1", 144);
    await waitFor(() => expect(move).toHaveBeenCalledWith("s1", { start_dt: "2026-10-07T11:00:00", end_dt: "2026-10-07T11:15:00" }));
  });

  it("(c) a drop near the bottom is clamped so the end stays at 20:00", async () => {
    vi.spyOn(api.life, "calendarRange").mockResolvedValue([ev("h1", "Hour", "2026-10-09T09:00:00", "2026-10-09T10:00:00")]);
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue(FRI);
    mount();
    await screen.findByText("Hour");
    stubTops();
    fireEvent.dragStart(blockOf("Hour"), dt("event-id", "h1"));
    dropOn("hours-2026-10-07", "event-id", "h1", 880);
    await waitFor(() => expect(move).toHaveBeenCalledWith("h1", { start_dt: "2026-10-07T19:00:00", end_dt: "2026-10-07T20:00:00" }));
  });

  it("(d) the bottom handle stretches the end in 15 minute steps and the release patches it", async () => {
    const patch = vi.spyOn(api.life, "calendarPatchEvent").mockResolvedValue(FRI);
    mount();
    await screen.findByText("Standup");
    const handle = screen.getByTestId("resize-m1");
    fireEvent.mouseDown(handle, { clientY: 300 });
    fireEvent.mouseMove(document, { clientY: 326 });
    fireEvent.mouseUp(document);
    await waitFor(() => expect(patch).toHaveBeenCalledWith("m1", { end_dt: "2026-10-09T11:00:00" }));
    expect(patch).toHaveBeenCalledTimes(1);
    fireEvent.mouseMove(document, { clientY: 400 });
    fireEvent.mouseUp(document);
    expect(patch).toHaveBeenCalledTimes(1);
  });

  it("(e) the templates list shows both rows, adds one and deletes one", async () => {
    const create = vi.spyOn(api.life, "createTemplate").mockResolvedValue(tpl(3, "Gym", KIT_ACCENTS[1]));
    const del = vi.spyOn(api.life, "deleteTemplate").mockResolvedValue({ ok: true });
    mount();
    expect(screen.queryByText("Run")).toBeNull();
    openTemplates();
    expect(await screen.findByText("Run")).toBeInTheDocument();
    expect(screen.getByText("Study")).toBeInTheDocument();
    expect(screen.queryByText("Drag one onto the calendar")).toBeNull();
    expect(screen.getByText("Templates")).toHaveAttribute("title", "Drag one onto the calendar");
    fireEvent.click(screen.getByRole("button", { name: "Add template" }));
    fireEvent.change(screen.getByLabelText("Template title"), { target: { value: "Gym" } });
    fireEvent.click(screen.getByRole("button", { name: `Colour ${KIT_ACCENTS[1]}` }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith({ title: "Gym", color: KIT_ACCENTS[1] }));
    const delBtn = screen.getByRole("button", { name: "Delete Study" });
    expect(delBtn).toHaveTextContent("Delete");
    fireEvent.click(delBtn);
    await waitFor(() => expect(del).toHaveBeenCalledWith(2));
  });

  it("(e2) the delete button shows only on hover or focus, by the css", () => {
    const css = fs.readFileSync("src/v2/pages/calendar/calendar.css", "utf-8");
    const block = (sel: string) => {
      const esc = sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const m = new RegExp(`(?:^|\\n)${esc} *\\{([^}]*)\\}`).exec(css);
      expect(m, sel).not.toBeNull();
      return m![1];
    };
    expect(block(".v2 .tdel")).toContain("opacity: 0");
    expect(block(".v2 .tdel")).toContain("color: var(--rt)");
    expect(block(".v2 .tp:hover .tdel, .v2 .tdel:focus-visible")).toContain("opacity: 1");
    expect(css).not.toContain(".v2 .tdel:hover");
    expect(css).not.toContain(".tnote");
  });

  it("(f) a template dropped on Thursday creates a 60 minute event at the drop minute", async () => {
    const create = vi.spyOn(api.life, "calendarCreateLocal").mockResolvedValue(FRI);
    mount();
    openTemplates();
    const row = (await screen.findByText("Study")).closest(".tp") as HTMLElement;
    expect(row).toHaveAttribute("draggable", "true");
    stubTops();
    fireEvent.dragStart(row, dt("template-id", "2"));
    dropOn("hours-2026-10-08", "template-id", "2", 72);
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({ title: "Study", color_id: "#ffb000", start_dt: "2026-10-08T09:30:00", end_dt: "2026-10-08T10:30:00" }),
    );
  });

  it("(g) DELETE needs a second press to confirm", async () => {
    const del = vi.spyOn(api.life, "deleteCalendarEvent").mockResolvedValue({ ok: true });
    mount();
    fireEvent.click(await screen.findByText("Standup"));
    const d = screen.getByRole("dialog", { name: "Event" });
    fireEvent.click(within(d).getByRole("button", { name: "Delete" }));
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(within(d).getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(del).toHaveBeenCalledTimes(1));
    expect(del).toHaveBeenCalledWith("m1");
  });

  it("(h) a refused move shows the server's words in the error line", async () => {
    vi.spyOn(api.life, "calendarMoveEvent").mockRejectedValue(
      new ApiError("403 x - /api/life/calendar/events/m1/move", 403, { detail: "event is read only" }),
    );
    mount();
    await screen.findByText("Standup");
    stubTops();
    fireEvent.dragStart(blockOf("Standup"), dt("event-id", "m1"));
    dropOn("hours-2026-10-07", "event-id", "m1", 260);
    expect(await screen.findByText("event is read only")).toBeInTheDocument();
  });

  it("(i) the ALL DAY row and month view take no drops, and a two day event is not draggable", async () => {
    vi.spyOn(api.life, "calendarRange").mockResolvedValue([FRI, ev("l1", "Night", "2026-10-09T22:00:00", "2026-10-10T16:00:00")]);
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue(FRI);
    const create = vi.spyOn(api.life, "calendarCreateLocal").mockResolvedValue(FRI);
    mount();
    await screen.findByText("Standup");
    stubTops();
    expect(blockOf("Night")).not.toHaveAttribute("draggable");
    fireEvent.dragStart(blockOf("Standup"), dt("event-id", "m1"));
    dropOn("allday-2026-10-07", "event-id", "m1", 10);
    fireEvent.click(screen.getByRole("button", { name: "Month" }));
    const cell = await screen.findByTestId("cell-2026-10-20");
    fire("drop", cell, "event-id", "m1", 10);
    expect(move).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
  });
});
