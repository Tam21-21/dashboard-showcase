import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import type { CalendarEvent, Todo } from "../../types";
import Today from "../pages/calendar/Today";

const ev = (id: string, title: string, start: string, end: string, extra: Partial<CalendarEvent> = {}): CalendarEvent => ({
  event_id: id, calendar_id: "primary", title, start_dt: start, end_dt: end, all_day: 0, location: null,
  description: null, color_id: null, html_link: null, synced_at: "2026-11-02T08:00:00", local_only: 0, ...extra,
});
const todo = (id: number, text: string, date: string, done: 0 | 1 = 0, done_at: string | null = null): Todo => ({
  id, text, done, position: id, date, done_at, created_at: "x", updated_at: "x",
});

const EVENTS = [
  ev("b", "Lecture", "2026-11-02T11:00:00", "2026-11-02T12:30:00", { location: "Room 4" }),
  ev("a", "Standup", "2026-11-02T09:00:00", "2026-11-02T09:30:00"),
];
const LATE = todo(1, "Pay the phone bill", "2026-10-30");
const TODAY_ITEMS = [
  todo(2, "Book the dentist", "2026-11-02", 1),
  todo(3, "Read Carver", "2026-11-02"),
  todo(4, "Buy a bike tube", "2026-11-02"),
];

let dayItems: Todo[];
let openItems: Todo[];

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Today />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 10, 2, 10, 20));
  dayItems = TODAY_ITEMS;
  openItems = [LATE];
  vi.spyOn(api.life, "calendarRange").mockResolvedValue(EVENTS);
  vi.spyOn(api.life, "getTodos").mockImplementation(async () => dayItems);
  vi.spyOn(api.life, "openTodos").mockImplementation(async () => openItems);
  vi.spyOn(api.life, "doneLateTodos").mockResolvedValue([]);
  vi.spyOn(api.life, "createTodo").mockResolvedValue(todo(9, "x", "2026-11-02"));
  vi.spyOn(api.life, "patchTodo").mockResolvedValue(todo(1, "x", "2026-11-02"));
  vi.spyOn(api.life, "deleteTodo").mockResolvedValue({ ok: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Today", () => {
  it("d1 lists today's events in time order as a table: when, what, kind, how long, and Over or Next", async () => {
    mount();
    await screen.findByText("Lecture");
    const rows = Array.from(document.querySelectorAll(".tdt tbody tr"));
    expect(rows.map((r) => r.querySelector("td:nth-child(2)")?.textContent)).toEqual(["Standup", "Lecture"]);
    expect(rows[0].querySelector("td")?.textContent).toBe("09:00 to 09:30");
    expect(rows[0].querySelector("td:nth-child(4)")?.textContent).toBe("30 min");
    expect(rows[0].textContent).toContain("Over");
    expect(rows[1].textContent).toContain("Next");
    expect(rows[0].textContent).not.toContain("Next");
  });

  it("d2 shows the late item first with its day and the note counts it", async () => {
    mount();
    await screen.findByText("Pay the phone bill");
    const late = screen.getByText("LATE");
    expect(late.closest("[title]")!.getAttribute("title")).toBe("Late since 30 OCT");
    expect(screen.queryByText("LATE, 30 OCT")).toBeNull();
    expect(screen.getByText(/1 of 4 done/).textContent).toBe("1 of 4 done, 1 late.");
    const rows = Array.from(document.querySelectorAll(".td")).map((r) => r.querySelector(".tx")?.textContent);
    expect(rows).toEqual(["Pay the phone bill", "Read Carver", "Buy a bike tube", "Book the dentist"]);
  });

  it("d3 typing then Enter posts the day and the text", async () => {
    mount();
    await screen.findByText("Read Carver");
    const input = screen.getByPlaceholderText("Add a to-do");
    fireEvent.change(input, { target: { value: "Call mum" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(api.life.createTodo).toHaveBeenCalledWith({ date: "2026-11-02", text: "Call mum" }));
  });

  it("d4 ticking the late item keeps its own day", async () => {
    mount();
    await screen.findByText("Pay the phone bill");
    fireEvent.click(screen.getByRole("button", { name: "Mark done: Pay the phone bill" }));
    await waitFor(() => expect(api.life.patchTodo).toHaveBeenCalledWith(1, { done: true }));
  });

  it("d5 a click on a text, a new text and Enter saves the text", async () => {
    mount();
    fireEvent.click(await screen.findByText("Read Carver"));
    const input = screen.getByDisplayValue("Read Carver");
    fireEvent.change(input, { target: { value: "Read Carver 7" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(api.life.patchTodo).toHaveBeenCalledWith(3, { text: "Read Carver 7" }));
  });

  it("d5b Escape cancels an edit", async () => {
    mount();
    fireEvent.click(await screen.findByText("Read Carver"));
    fireEvent.keyDown(screen.getByDisplayValue("Read Carver"), { key: "Escape" });
    expect(screen.queryByDisplayValue("Read Carver")).toBeNull();
    expect(api.life.patchTodo).not.toHaveBeenCalled();
  });

  it("d6 the cross deletes", async () => {
    mount();
    await screen.findByText("Read Carver");
    fireEvent.click(screen.getByRole("button", { name: "Delete: Read Carver" }));
    await waitFor(() => expect(api.life.deleteTodo).toHaveBeenCalledWith(3));
  });

  it("d7 shows the server's words when it refuses", async () => {
    vi.spyOn(api.life, "createTodo").mockRejectedValue(
      new ApiError("422 x", 422, { detail: "The to-do needs some text." }),
    );
    mount();
    await screen.findByText("Read Carver");
    const input = screen.getByPlaceholderText("Add a to-do");
    fireEvent.change(input, { target: { value: "a" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByText("The to-do needs some text.")).toBeTruthy();
  });

  it("d8 says so when there is nothing to do", async () => {
    dayItems = [];
    openItems = [];
    mount();
    expect(await screen.findByText("Nothing to do today.")).toBeTruthy();
    expect(screen.getByText("All done")).toBeTruthy();
  });

  it("d9 NEW DATE on a late item opens on its own month and moves only the date", async () => {
    mount();
    await screen.findByText("Pay the phone bill");
    fireEvent.click(screen.getByRole("button", { name: "New date: Pay the phone bill" }));
    const dlg = screen.getByRole("dialog", { name: "Choose a date" });
    expect(within(dlg).getByText("October 2026")).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("button", { name: "Next month" }));
    fireEvent.click(within(dlg).getByRole("button", { name: "20" }));
    await waitFor(() => expect(api.life.patchTodo).toHaveBeenCalledWith(1, { date: "2026-11-20" }));
    expect(api.life.patchTodo).toHaveBeenCalledTimes(1);
  });

  it("d10 today's open items and done items show no NEW DATE button", async () => {
    mount();
    await screen.findByText("Read Carver");
    expect(screen.queryByRole("button", { name: "New date: Read Carver" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New date: Book the dentist" })).toBeNull();
    expect(screen.getAllByRole("button", { name: /^New date:/ })).toHaveLength(1);
  });

  it("d11 the cross of the late row still deletes", async () => {
    mount();
    await screen.findByText("Pay the phone bill");
    fireEvent.click(screen.getByRole("button", { name: "Delete: Pay the phone bill" }));
    await waitFor(() => expect(api.life.deleteTodo).toHaveBeenCalledWith(1));
  });

  it("d12 a late item ticked today stays on the list, greyed, counted as done, with no LATE word", async () => {
    vi.spyOn(api.life, "doneLateTodos").mockResolvedValue([todo(5, "Renew the pass", "2026-10-29", 1, "2026-11-02 09:00:00")]);
    mount();
    await screen.findByText("Renew the pass");
    const rows = Array.from(document.querySelectorAll(".td"));
    const last = rows[rows.length - 1];
    expect(last.textContent).toContain("Renew the pass");
    expect(last.classList.contains("done")).toBe(true);
    expect(last.textContent).not.toContain("LATE, 29 OCT");
    expect(screen.queryByRole("button", { name: "New date: Renew the pass" })).toBeNull();
    expect(document.body.textContent).toContain("2 of 5 done, 1 late.");
  });

  it("d13 unticking a late item ticked today patches done false", async () => {
    vi.spyOn(api.life, "doneLateTodos").mockResolvedValue([todo(5, "Renew the pass", "2026-10-29", 1, "2026-11-02 09:00:00")]);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Mark not done: Renew the pass" }));
    await waitFor(() => expect(api.life.patchTodo).toHaveBeenCalledWith(5, { done: false }));
  });
});
