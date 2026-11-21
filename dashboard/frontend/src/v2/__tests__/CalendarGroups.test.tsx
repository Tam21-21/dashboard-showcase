import { cleanup, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../lib/api";
import type { CalendarEvent } from "../../types";
import Calendar from "../pages/calendar/Calendar";
import { APPS } from "../shell/sections";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };

const app = APPS.find((a) => a.id === "calendar")!;
const section = app.sections.find((s) => s.id === "calendar")!;

const ev = (id: string, title: string, start: string, end: string, extra: Partial<CalendarEvent> = {}): CalendarEvent => ({
  event_id: id, calendar_id: "primary", title, start_dt: start, end_dt: end, all_day: 0, location: null,
  description: null, color_id: null, html_link: null, synced_at: "2026-10-07T08:00:00", local_only: 0, ...extra,
});

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
function stubTops() {
  document.querySelectorAll('[data-testid^="hours-"]').forEach((el) => {
    if (el.getAttribute("data-testid") === "hours-scroll") return;
    (el as HTMLElement).getBoundingClientRect = () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON() {} });
  });
}
const dt = (kind: string, id: string) => ({
  dataTransfer: { setData: vi.fn(), getData: (k: string) => (k === kind ? id : ""), effectAllowed: "", dropEffect: "" },
});
const fire = (name: "dragOver" | "drop", el: Element, kind: string, id: string, clientY: number) => {
  const event = createEvent[name](el, dt(kind, id));
  Object.defineProperty(event, "clientY", { value: clientY });
  fireEvent(el, event);
};

const BU = ev("bu", "BU", "2026-10-07T16:00:00", "2026-10-07T18:30:00", { color_id: "#ffb000" });
const FB = ev("fb", "Football", "2026-10-07T17:30:00", "2026-10-07T19:30:00", { color_id: "#3ddbb4" });

const setRange = (list: CalendarEvent[]) => vi.spyOn(api.life, "calendarRange").mockResolvedValue(list);
const groups = () => Array.from(document.querySelectorAll(".ev.grp")) as HTMLElement[];
const lines = (g: HTMLElement) => Array.from(g.querySelectorAll(".gl")).map((l) => l.textContent);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  setRange([BU, FB]);
  vi.spyOn(api.life, "weather").mockResolvedValue({} as never);
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([]);
  vi.spyOn(api.life, "getTemplates").mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Calendar overlap cards", () => {
  it("(a) two overlapping events are one card from the first start to the last end", async () => {
    mount();
    await screen.findByText("BU");
    expect(groups()).toHaveLength(1);
    const g = groups()[0];
    expect(g.style.top).toBe("385px");
    expect(g.style.height).toBe("165px");
    expect(lines(g)).toEqual(["BU", "Football"]);
    expect(screen.queryByTestId("resize-bu")).toBeNull();
    expect(screen.queryByTestId("resize-fb")).toBeNull();
  });

  it("(b) a click on the Football line opens Football's detail", async () => {
    mount();
    const line = (await screen.findAllByText("Football")).map((n) => n.closest(".gl")).find((n) => n !== null) as HTMLElement;
    expect(line).not.toBeNull();
    expect(line).toHaveAttribute("role", "button");
    fireEvent.click(line);
    const d = screen.getByRole("dialog", { name: "Event" });
    expect(within(d).getByRole("heading", { name: "Football" })).toBeInTheDocument();
    expect(within(d).queryByText("BU")).toBeNull();
  });

  it("(c) dragging the BU line moves BU alone", async () => {
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue(BU);
    mount();
    const line = (await screen.findByText("BU")).closest(".gl") as HTMLElement;
    expect(line).not.toBeNull();
    stubTops();
    expect(line).toHaveAttribute("draggable", "true");
    fireEvent.dragStart(line, dt("event-id", "bu"));
    const col = screen.getByTestId("hours-2026-10-08");
    fire("dragOver", col, "event-id", "bu", 144);
    fire("drop", col, "event-id", "bu", 144);
    await waitFor(() => expect(move).toHaveBeenCalledWith("bu", { start_dt: "2026-10-08T11:00:00", end_dt: "2026-10-08T13:30:00" }));
  });

  it("(d) a chain counts: A, B and C are one group from 09:00 to 11:30", async () => {
    setRange([
      ev("a", "Alpha", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
      ev("b", "Bravo", "2026-10-07T09:30:00", "2026-10-07T11:00:00"),
      ev("c", "Charlie", "2026-10-07T10:30:00", "2026-10-07T11:30:00"),
    ]);
    mount();
    await screen.findByText("Alpha");
    expect(groups()).toHaveLength(1);
    expect(groups()[0].style.top).toBe("49px");
    expect(groups()[0].style.height).toBe("117px");
  });

  it("(e) three events at once: the first title and 2 more, the title lists all three", async () => {
    setRange([
      ev("a", "Alpha", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
      ev("b", "Bravo", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
      ev("c", "Charlie", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
    ]);
    mount();
    await screen.findByText("Alpha");
    const g = groups()[0];
    expect(lines(g)).toEqual(["Alpha", "2 more"]);
    expect(g.getAttribute("title")!.split("\n")).toHaveLength(3);
  });

  it("(f) the card background is a generated svg", async () => {
    mount();
    await screen.findByText("BU");
    const bg = groups()[0].style.backgroundImage;
    expect(bg.startsWith("url(")).toBe(true);
    expect(bg).toContain("data:image/svg+xml");
  });

  it("(g) guard: back-to-back events are two blocks, no card", async () => {
    setRange([
      ev("a", "Alpha", "2026-10-07T09:00:00", "2026-10-07T10:00:00"),
      ev("b", "Bravo", "2026-10-07T10:00:00", "2026-10-07T11:00:00"),
    ]);
    mount();
    await screen.findByText("Alpha");
    expect(groups()).toHaveLength(0);
    expect(document.querySelectorAll(".ev:not(.all)")).toHaveLength(2);
  });

  it("(h) guard: a lone event keeps its resize handle and its time line", async () => {
    setRange([ev("a", "Alpha", "2026-10-07T09:00:00", "2026-10-07T10:00:00")]);
    mount();
    await screen.findByText("Alpha");
    expect(screen.getByTestId("resize-a")).toBeInTheDocument();
    expect(screen.getAllByText("09:00 to 10:00").length).toBeGreaterThan(0);
  });

  it("(j) the span line shows only when every line fits with 16px to spare", async () => {
    mount();
    await screen.findByText("BU");
    expect(groups()[0].querySelector(".ev-t")?.textContent).toBe("16:00 to 19:30");
    cleanup();
    setRange([
      ev("a", "Alpha", "2026-10-07T09:00:00", "2026-10-07T09:30:00"),
      ev("b", "Bravo", "2026-10-07T09:15:00", "2026-10-07T09:45:00"),
    ]);
    mount();
    await screen.findByText("Alpha and 1 more");
    expect(groups()).toHaveLength(1);
    expect(groups()[0].querySelector(".ev-t")).toBeNull();
  });

  it("(i) the button rules live in kit.css and calendar.css has none", () => {
    const kit = fs.readFileSync("src/v2/kit/kit.css", "utf-8");
    const cal = fs.readFileSync("src/v2/pages/calendar/calendar.css", "utf-8");
    // One button colour (the Buttons choice), the quiet one looks the same; only the red one keeps its edge.
    const rules = [
      ".v2 .btn { white-space: nowrap; font: inherit; font-size: 14.5px; font-weight: 600; color: var(--btn-ink); background: var(--btn); border: 0; box-shadow: inset 0 0 0 1px var(--btn-edge); border-radius: 11px; height: 40px; padding: 0 18px; gap: 8px; cursor: pointer; display: inline-flex; align-items: center; text-decoration: none; transition: transform .14s var(--ease), background-color .15s ease; }",
      ".v2 .btn:disabled { opacity: .45; cursor: default; transform: none; }",
      ".v2 .btn.danger { color: var(--rt); background: none; box-shadow: inset 0 0 0 1px var(--rt); }",
    ];
    for (const r of rules) expect(kit).toContain(r);
    expect(cal).not.toContain(".btn");
  });
});
