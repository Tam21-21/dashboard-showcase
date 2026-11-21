import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../lib/api";
import Calendar from "../pages/calendar/Calendar";
import AskClaude from "../pages/calendar/AskClaude";
import { APPS } from "../shell/sections";

const app = APPS.find((a) => a.id === "calendar")!;
const section = app.sections.find((s) => s.id === "calendar")!;

const ask = () => vi.spyOn(api.life as unknown as { ask: (q: string, h: unknown[]) => Promise<unknown> }, "ask");
const ADD = { kind: "add", title: "Football", date: "2026-10-14", start: "18:30", end: "21:30", all_day: false };
const SERIES = {
  kind: "series", title: "BU", days: ["MO", "TU", "WE", "TH", "FR"], start: "08:30", end: "12:00",
  first_date: "2026-10-14", until: "",
};
const REMOVE = { kind: "remove", event_ids: ["e1", "e2", "e3"], titles: ["Easy run", "Gym", "Long run"] };
const MOVE = { kind: "move", event_id: "r1", title: "RACE", date: "2026-10-25", start: "00:00", end: "00:00", all_day: true };
const refused = (words: string) => new ApiError("503 x - /api/life/calendar", 503, { detail: words });

function mountCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const ui = (
    <QueryClientProvider client={qc}>
      <AskClaude />
    </QueryClientProvider>
  );
  const r = render(ui);
  return { invalidate, remount: () => { r.unmount(); render(ui); } };
}
const openWindow = () => fireEvent.click(screen.getByRole("button", { name: "Ask Claude" }));
const box = () => screen.getByLabelText("Question for Claude");
const send = (text: string) => {
  fireEvent.change(box(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
};
async function askOnce(answer: { answer: string; proposals: unknown[] }, text = "Do it") {
  ask().mockResolvedValue(answer);
  mountCard();
  openWindow();
  send(text);
  await screen.findByText(answer.answer);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "calendarRange").mockResolvedValue([]);
  vi.spyOn(api.life, "weather").mockRejectedValue(new Error("none"));
  vi.spyOn(api.life, "weatherForecast").mockResolvedValue([]);
  vi.spyOn(api.life, "getTemplates").mockResolvedValue([]);
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Ask Claude window", () => {
  it("(a) the card holds one button and no text box; the button opens a dialog; Escape closes it", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <Calendar app={app} section={section} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByText("Templates");
    expect(screen.queryByPlaceholderText("Plan my week")).toBeNull();
    expect(screen.queryByLabelText("Question for Claude")).toBeNull();
    openWindow();
    expect(screen.getByRole("dialog", { name: "Ask Claude" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Ask Claude" })).toBeNull();
  });

  it("(b) Send asks once; the second question carries the history; turns show in order with line breaks", async () => {
    const spy = ask()
      .mockResolvedValueOnce({ answer: "First.\nSecond line.", proposals: [] })
      .mockResolvedValueOnce({ answer: "Again.", proposals: [] });
    mountCard();
    openWindow();
    send("Plan my week");
    await screen.findByText(/First\./);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("Plan my week", []);
    expect(screen.getByText(/First\./).textContent).toBe("First.\nSecond line.");
    send("And Friday?");
    await screen.findByText("Again.");
    expect(spy).toHaveBeenLastCalledWith("And Friday?", [
      { role: "user", text: "Plan my week" },
      { role: "assistant", text: "First.\nSecond line." },
    ]);
    const log = screen.getByRole("dialog").textContent ?? "";
    expect(log.indexOf("Plan my week")).toBeLessThan(log.indexOf("First."));
    expect(log.indexOf("First.")).toBeLessThan(log.indexOf("And Friday?"));
    expect(log.indexOf("And Friday?")).toBeLessThan(log.indexOf("Again."));
  });

  it("(b) Enter sends", async () => {
    const spy = ask().mockResolvedValue({ answer: "ok", proposals: [] });
    mountCard();
    openWindow();
    fireEvent.change(box(), { target: { value: "Hi" } });
    fireEvent.keyDown(box(), { key: "Enter" });
    await screen.findByText("ok");
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("(c) the four kinds read in plain words", async () => {
    await askOnce({
      answer: "Here.",
      proposals: [ADD, SERIES, { ...SERIES, title: "Gym", days: ["MO"], until: "2026-12-19" }, REMOVE, MOVE],
    });
    expect(screen.getByText("Add: Football, Wed 14 Oct 18:30 to 21:30")).toBeInTheDocument();
    expect(screen.getByText("Every Mon, Tue, Wed, Thu, Fri: BU 08:30 to 12:00, from Wed 14 Oct")).toBeInTheDocument();
    expect(screen.getByText("Every Mon: Gym 08:30 to 12:00, from Wed 14 Oct, until Sat 19 Dec")).toBeInTheDocument();
    expect(screen.getByText("Remove 3 events: Easy run, Gym, Long run")).toBeInTheDocument();
    expect(screen.getByText("Move: RACE to Sun 25 Oct, all day")).toBeInTheDocument();
  });

  it("(d) Apply on each kind calls its route, the row reads Done, the range is invalidated", async () => {
    const create = vi.spyOn(api.life, "calendarCreate").mockResolvedValue({} as never);
    const del = vi.spyOn(api.life, "deleteCalendarEvent").mockResolvedValue({ ok: true });
    const move = vi.spyOn(api.life, "calendarMoveEvent").mockResolvedValue({} as never);
    ask().mockResolvedValue({
      answer: "Here.",
      proposals: [ADD, SERIES, { ...SERIES, title: "Gym", days: ["MO"], until: "2026-12-19" }, REMOVE, MOVE],
    });
    const { invalidate } = mountCard();
    openWindow();
    send("Do it");
    await screen.findByText("Here.");
    const apply = () => screen.getAllByRole("button", { name: "Apply" })[0];
    fireEvent.click(apply());
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenLastCalledWith({
      title: "Football", start_dt: "2026-10-14T18:30:00", end_dt: "2026-10-14T21:30:00", all_day: false,
    });
    await screen.findAllByText("Done");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["calendar", "range"] });
    fireEvent.click(apply());
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create).toHaveBeenLastCalledWith({
      title: "BU", start_dt: "2026-10-14T08:30:00", end_dt: "2026-10-14T12:00:00", all_day: false,
      recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"],
    });
    await waitFor(() => expect(screen.getAllByText("Done")).toHaveLength(2));
    fireEvent.click(apply());
    await waitFor(() => expect(create).toHaveBeenCalledTimes(3));
    expect(create).toHaveBeenLastCalledWith(expect.objectContaining({
      recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261219T235959Z"],
    }));
    await waitFor(() => expect(screen.getAllByText("Done")).toHaveLength(3));
    fireEvent.click(apply());
    await waitFor(() => expect(del).toHaveBeenCalledTimes(3));
    expect(del.mock.calls.map((c) => c[0])).toEqual(["e1", "e2", "e3"]);
    await waitFor(() => expect(screen.getAllByText("Done")).toHaveLength(4));
    fireEvent.click(apply());
    await waitFor(() => expect(move).toHaveBeenCalledTimes(1));
    expect(move).toHaveBeenCalledWith("r1", { start_dt: "2026-10-25T00:00:00", end_dt: "2026-10-26T00:00:00" });
    await waitFor(() => expect(screen.getAllByText("Done")).toHaveLength(5));
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  });

  it("(d) a create answered with sync_error is Done with the note", async () => {
    vi.spyOn(api.life, "calendarCreate").mockResolvedValue({ sync_error: "Google is slow" } as never);
    await askOnce({ answer: "Here.", proposals: [SERIES] });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Added; calendar refresh failed: Google is slow")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  it("(e) a refused call shows the server's words on that row", async () => {
    vi.spyOn(api.life, "calendarCreate").mockRejectedValue(refused("Google Calendar not connected"));
    await askOnce({ answer: "Here.", proposals: [ADD] });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Google Calendar not connected")).toBeInTheDocument();
    expect(screen.queryByText("Done")).toBeNull();
  });

  it("(e) a remove failing on its second id reads Removed 1 of 3", async () => {
    const del = vi.spyOn(api.life, "deleteCalendarEvent")
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(refused("Google refused the delete: nope"));
    await askOnce({ answer: "Here.", proposals: [REMOVE] });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText("Removed 1 of 3: Google refused the delete: nope")).toBeInTheDocument();
    expect(del).toHaveBeenCalledTimes(2);
  });

  it("(f) Apply all applies the rows not done, in order, and stops at the first failure", async () => {
    const create = vi.spyOn(api.life, "calendarCreate")
      .mockResolvedValueOnce({} as never)
      .mockRejectedValueOnce(refused("Google refused the create: no"));
    const del = vi.spyOn(api.life, "deleteCalendarEvent").mockResolvedValue({ ok: true });
    await askOnce({ answer: "Here.", proposals: [ADD, SERIES, REMOVE] });
    fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
    expect(await screen.findByText("Google refused the create: no")).toBeInTheDocument();
    expect(create).toHaveBeenCalledTimes(2);
    expect(del).not.toHaveBeenCalled();
    expect(screen.getAllByText("Done")).toHaveLength(1);
    // A second APPLY ALL skips the done row and retries from the failed one.
    create.mockResolvedValue({} as never);
    fireEvent.click(screen.getByRole("button", { name: "Apply all" }));
    await waitFor(() => expect(screen.getAllByText("Done")).toHaveLength(3));
    expect(create).toHaveBeenCalledTimes(3);
    expect(del).toHaveBeenCalledTimes(3);
  });

  it("(g) close and reopen, or remount, keeps turns, rows and Done; New conversation empties them", async () => {
    vi.spyOn(api.life, "calendarCreate").mockResolvedValue({} as never);
    ask().mockResolvedValue({ answer: "Here.", proposals: [ADD] });
    const { remount } = mountCard();
    openWindow();
    send("Football?");
    await screen.findByText("Here.");
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByText("Done");
    fireEvent.keyDown(document, { key: "Escape" });
    openWindow();
    expect(screen.getByText("Football?")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    remount();
    openWindow();
    expect(screen.getByText("Here.")).toBeInTheDocument();
    expect(screen.getByText("Football?")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.queryByText("Here.")).toBeNull();
    expect(screen.queryByText("Football?")).toBeNull();
    expect(screen.queryByText("Done")).toBeNull();
  });

  it("(h) a failed ask shows the server's words in the window", async () => {
    ask().mockRejectedValue(
      new ApiError("503 x - /api/life/ask", 503, { detail: "ANTHROPIC_API_KEY is not set on the server" }),
    );
    mountCard();
    openWindow();
    send("Hi");
    const win = screen.getByRole("dialog", { name: "Ask Claude" });
    await waitFor(() => expect(within(win).getByText("ANTHROPIC_API_KEY is not set on the server")).toBeInTheDocument());
  });
});
