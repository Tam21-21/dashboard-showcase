import { useState } from "react";
import type { FormEvent } from "react";
import { serverDetail } from "../../../lib/errorText";
import { DatePick, Segmented, TimePick, Window, usePageMemory } from "../../kit";
import { useCreateEvent } from "./data";

const mins = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
const clock = (m: number) => String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");

// The form for one new event. The write goes to Google through the server; a refusal is shown here only.

export default function NewEvent({ startDay, onClose }: { startDay: string; onClose: () => void }) {
  const [form, setForm, clearForm] = usePageMemory("calendar/new-event/form", {
    title: "", date: startDay, start: "09:00", end: "10:00", allDay: false,
  });
  const { title, date, start, end, allDay } = form;
  const setTitle = (v: string) => setForm((f) => ({ ...f, title: v }));
  const setDate = (v: string) => setForm((f) => ({ ...f, date: v }));
  const setEnd = (v: string) => setForm((f) => ({ ...f, end: v }));
  const setAllDay = (v: boolean) => setForm((f) => ({ ...f, allDay: v }));
  const [problem, setProblem] = useState<string | null>(null);
  const [pk, setPk] = useState<"d" | "a" | "b" | null>(null);
  // A created event clears the form; a cancel keeps what was typed for the next visit.
  const create = useCreateEvent(() => { clearForm(); onClose(); });

  // Moving the start moves the end with it: the event keeps its length, cut to 23:45.
  function moveStart(next: string) {
    const length = Math.max(mins(end) - mins(start), 15);
    setForm((f) => ({ ...f, start: next, end: clock(Math.min(mins(next) + length, 23 * 60 + 45)) }));
  }

  // Escape closes an open list first (the list stops the key itself), then the window.
  function submit(e: FormEvent) {
    e.preventDefault();
    setProblem(null);
    if (!title.trim()) return setProblem("Give the event a title.");
    if (!date) return setProblem("Pick a date.");
    if (allDay) {
      create.mutate({ title, start_dt: date, end_dt: date, all_day: true }, { onError: (err) => setProblem(serverDetail(err)) });
      return;
    }
    if (!start || !end || end <= start) return setProblem("The end must be after the start.");
    create.mutate(
      { title, start_dt: `${date}T${start}:00`, end_dt: `${date}T${end}:00` },
      { onError: (err) => setProblem(serverDetail(err)) },
    );
  }

  return (
    <Window label="New event" title="New event" size="l" className="cwin" onClose={onClose}>
      <form className="cw-form" onSubmit={submit}>
        <label className="cw-field">
          <span>Title</span>
          <input className="cw-in" aria-label="Title" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        </label>
        <div className="cw-two">
          <div className="cw-field">
            <span>Date</span>
            <DatePick value={date} onChange={setDate} ariaLabel="Date" open={pk === "d"} onOpenChange={(o) => setPk(o ? "d" : null)} />
          </div>
          <div className="cw-field">
            <span>All day</span>
            <span>
              <Segmented variant="seg" value={allDay ? "yes" : "no"} onChange={(v) => setAllDay(v === "yes")}
                options={[{ label: "Timed", value: "no" }, { label: "All day", value: "yes" }]} />
            </span>
          </div>
          <div className="cw-field">
            <span>Start</span>
            <div className={allDay ? "cw-off" : undefined}>
              <TimePick value={start} onChange={moveStart} ariaLabel="Start" open={pk === "a"} onOpenChange={(o) => setPk(o ? "a" : null)} />
            </div>
          </div>
          <div className="cw-field">
            <span>End</span>
            <div className={allDay ? "cw-off" : undefined}>
              <TimePick value={end} onChange={setEnd} ariaLabel="End" after={start} open={pk === "b"} onOpenChange={(o) => setPk(o ? "b" : null)} />
            </div>
          </div>
        </div>
        {problem && <div className="ferr" role="alert">{problem}</div>}
        <div className="cw-acts">
          <button type="submit" className="btn" disabled={create.isPending}>Create</button>
          <span className="cw-note">The event is created in Google Calendar.</span>
        </div>
      </form>
    </Window>
  );
}
