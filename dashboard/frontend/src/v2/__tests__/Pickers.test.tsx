import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../lib/api";
import { DatePick, TimePick } from "../kit";
import NewEvent from "../pages/calendar/NewEvent";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const days = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".dpg button"));
const options = () => within(screen.getByRole("listbox")).getAllByRole("option");

describe("DatePick", () => {
  it("(a) shows the date, opens a month grid, marks the value and today, and a click picks", () => {
    const f = vi.fn();
    render(<DatePick value="2026-10-08" onChange={f} ariaLabel="Date" />);
    const btn = screen.getByRole("button", { name: "Date" });
    expect(btn).toHaveTextContent("08/10/2026");
    fireEvent.click(btn);
    const dlg = screen.getByRole("dialog", { name: "Choose a date" });
    expect(within(dlg).getByText("October 2026")).toBeInTheDocument();
    const heads = Array.from(dlg.querySelectorAll(".dpg .hd")).map((n) => n.textContent);
    expect(heads).toEqual(["MO", "TU", "WE", "TH", "FR", "SA", "SU"]);
    const d = days();
    expect(d).toHaveLength(42);
    expect(d[0].textContent).toBe("28");
    expect(d[0]).toHaveClass("out");
    expect(d[41].textContent).toBe("8");
    const eighth = d[10];
    expect(eighth.textContent).toBe("8");
    expect(eighth).toHaveClass("sel");
    expect(eighth).toHaveAttribute("aria-pressed", "true");
    expect(d[9]).toHaveClass("tdy");
    fireEvent.click(d[16]);
    expect(f).toHaveBeenCalledWith("2026-10-14");
    expect(screen.queryByRole("dialog", { name: "Choose a date" })).toBeNull();
  });

  it("(b) steps months and TODAY picks today", () => {
    const f = vi.fn();
    render(<DatePick value="2026-10-08" onChange={f} ariaLabel="Date" />);
    fireEvent.click(screen.getByRole("button", { name: "Date" }));
    fireEvent.click(screen.getByRole("button", { name: "Next month" }));
    expect(screen.getByText("November 2026")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous month" }));
    fireEvent.click(screen.getByRole("button", { name: "Previous month" }));
    expect(screen.getByText("September 2026")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "TODAY" }));
    expect(f).toHaveBeenCalledWith("2026-10-07");
  });

  it("(c) Escape closes it and goes no further; a press outside closes it", () => {
    const seen = vi.fn();
    render(
      <div onKeyDown={seen}>
        <DatePick value="2026-10-08" onChange={vi.fn()} ariaLabel="Date" />
      </div>,
    );
    const btn = screen.getByRole("button", { name: "Date" });
    fireEvent.click(btn);
    fireEvent.keyDown(btn, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(seen).not.toHaveBeenCalled();
    fireEvent.click(btn);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("TimePick", () => {
  it("(d) is a text field with a list of quarter hours", () => {
    const g = vi.fn();
    render(<TimePick value="09:00" onChange={g} ariaLabel="Start" />);
    const input = screen.getByRole("textbox", { name: "Start" });
    expect(input).toHaveValue("09:00");
    fireEvent.click(input);
    const o = options();
    expect(o).toHaveLength(95);
    expect(o[0].textContent).toBe("00:00");
    expect(o[94].textContent).toBe("23:30");
    expect(screen.getByRole("option", { name: "09:00" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("option", { name: "10:30" }));
    expect(g).toHaveBeenCalledWith("10:30");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("(e) typing is read and rounded to the quarter hour", () => {
    const g = vi.fn();
    render(<TimePick value="09:00" onChange={g} ariaLabel="Start" />);
    const input = screen.getByRole("textbox", { name: "Start" });
    const type = (v: string) => fireEvent.change(input, { target: { value: v } });
    type("930");
    expect(g).toHaveBeenLastCalledWith("09:30");
    type("21h");
    expect(g).toHaveBeenLastCalledWith("21:00");
    type("9:07");
    expect(g).toHaveBeenLastCalledWith("09:00");
    type("9:08");
    expect(g).toHaveBeenLastCalledWith("09:15");
    g.mockClear();
    type("25:00");
    type("abc");
    type("23:55");
    expect(g).not.toHaveBeenCalled();
    fireEvent.blur(input);
    expect(input).toHaveValue("09:00");
  });

  it("(f) an end list starts a quarter hour after the start and shows each length", () => {
    render(<TimePick value="10:00" after="09:00" onChange={vi.fn()} ariaLabel="End" />);
    fireEvent.click(screen.getByRole("textbox", { name: "End" }));
    const o = options();
    expect(o).toHaveLength(59);
    expect(o[0].textContent).toBe("09:15");
    expect(o[58].textContent).toBe("23:45");
    const title = (t: string) => screen.getByRole("option", { name: t }).getAttribute("title");
    expect(title("09:15")).toBe("15 minutes");
    expect(title("10:00")).toBe("1 hour");
    expect(title("10:30")).toBe("1 h 30");
    expect(title("11:00")).toBe("2 hours");
  });
});

describe("NewEvent with the pickers", () => {
  function open() {
    const create = vi.spyOn(api.life, "calendarCreate").mockResolvedValue({} as never);
    const onClose = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <NewEvent startDay="2026-10-08" onClose={onClose} />
      </QueryClientProvider>,
    );
    return { create, onClose, form: screen.getByRole("dialog", { name: "New event" }) };
  }

  it("(g) the start keeps the length, the end list follows, no native inputs", async () => {
    const { create, form } = open();
    const start = within(form).getByRole("textbox", { name: "Start" });
    const end = within(form).getByRole("textbox", { name: "End" });
    expect(start).toHaveValue("09:00");
    expect(end).toHaveValue("10:00");
    expect(form.querySelector('input[type="time"]')).toBeNull();
    expect(form.querySelector('input[type="date"]')).toBeNull();

    fireEvent.click(start);
    expect(options()[options().length - 1].textContent).toBe("23:30");
    fireEvent.click(screen.getByRole("option", { name: "14:00" }));
    expect(end).toHaveValue("15:00");

    fireEvent.click(start);
    fireEvent.click(screen.getByRole("option", { name: "23:30" }));
    expect(end).toHaveValue("23:45");
    fireEvent.click(start);
    fireEvent.click(screen.getByRole("option", { name: "14:00" }));
    fireEvent.change(end, { target: { value: "1630" } });
    fireEvent.change(within(form).getByLabelText("Title"), { target: { value: "Dentist" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({ title: "Dentist", start_dt: "2026-10-08T14:00:00", end_dt: "2026-10-08T16:30:00" }),
    );
  });

  it("(g) Escape closes an open list first, then the dialog", () => {
    const { onClose, form } = open();
    const start = within(form).getByRole("textbox", { name: "Start" });
    fireEvent.click(start);
    fireEvent.keyDown(start, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(start, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
