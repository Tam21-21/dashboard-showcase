import { useState } from "react";
import { serverDetail } from "../../../lib/errorText";
import type { CalendarEvent } from "../../../types";
import { DatePick, State, TimePick, Window } from "../../kit";
import { kindOf } from "./accent";
import { eventDay, hhmm, isTimed, minToStamp, timeRange, useDeleteEvent, useMoveEvent } from "./data";

const short = (t: string) => t.replace(/\s+-\s+[A-Z]?\d+.*$/, "").replace(/\s*\(.*\)\s*$/, "");
const dur = (m: number) => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m / 60} h`);
const longDay = (iso: string) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric" });
const lengthOf = (e: CalendarEvent) => (e.end_dt.includes("T") ? dur(mins(hhmm(e.end_dt)) - mins(hhmm(e.start_dt))) : "All day");

const mins = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));

// One event. DELETE asks twice: the first press turns it into CONFIRM DELETE. EDIT TIME (timed events) changes the date
// and the start and end through the same route the drag uses; the title is not editable (no route renames an event).

export default function EventDetail({
  event, onClose, onError,
}: {
  event: CalendarEvent;
  onClose: () => void;
  onError: (text: string | null) => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const del = useDeleteEvent();
  const move = useMoveEvent();
  const [editing, setEditing] = useState(false);
  const [date, setDate] = useState(eventDay(event));
  const [start, setStart] = useState(hhmm(event.start_dt));
  const [end, setEnd] = useState(event.end_dt.includes("T") ? hhmm(event.end_dt) : hhmm(event.start_dt));
  const [problem, setProblem] = useState<string | null>(null);
  const save = () => {
    if (mins(end) <= mins(start)) {
      setProblem("The end must be after the start.");
      return;
    }
    setProblem(null);
    onError(null);
    move.mutate(
      { event_id: event.event_id, start_dt: minToStamp(date, mins(start)), end_dt: minToStamp(date, mins(end)) },
      { onSuccess: onClose, onError: (err) => setProblem(serverDetail(err)) },
    );
  };
  const remove = () => {
    onError(null);
    del.mutate(event.event_id, {
      onSuccess: onClose,
      onError: (err) => {
        onError(serverDetail(err));
        onClose();
      },
    });
  };
  const phase = Date.now();
  const over = Date.parse(event.end_dt.includes("T") ? event.end_dt : `${event.end_dt}T23:59:59`) < phase;
  const on = !over && isTimed(event) && Date.parse(event.start_dt) <= phase;
  const kind = kindOf(event.title);
  return (
    <Window label="Event" title={short(event.title)} size="l" className="cwin" onClose={onClose}>
        <div className="cw-lead">
          <State tone={over ? "dim" : on ? "good" : "run"}>{over ? "Over" : on ? "On now" : "Coming"}</State>
          <span>{longDay(eventDay(event))}</span>
        </div>
        <div className="cw-kv">
          <span><i>When</i><b>{isTimed(event) ? timeRange(event) : "All day"}</b></span>
          <span><i>How long</i><b>{isTimed(event) ? lengthOf(event) : "All day"}</b></span>
          <span><i>Kind</i><b><i className="cw-dot" style={{ background: kind.hex }} />{kind.name}</b></span>
          <span><i>Name in Google</i><b>{event.title}</b></span>
          {event.location && <span><i>Place</i><b>{event.location}</b></span>}
        </div>
        {event.description && <div className="mrow desc">{event.description}</div>}
        {editing && (
          <div className="medit">
            <DatePick value={date} onChange={setDate} ariaLabel="Date" />
            <div className="ftimes">
              <TimePick value={start} onChange={setStart} ariaLabel="Start" />
              <span className="to">to</span>
              <TimePick value={end} onChange={setEnd} ariaLabel="End" after={start} />
            </div>
            {problem && <div className="ferr" role="alert">{problem}</div>}
          </div>
        )}
        <div className="cw-acts">
          {isTimed(event) && !editing && (
            <button type="button" className="btn" onClick={() => setEditing(true)}>Edit time</button>
          )}
          {editing && (
            <button type="button" className="btn" disabled={move.isPending} onClick={save}>Save</button>
          )}
          <button
            type="button"
            className="btn"
            disabled={del.isPending}
            onClick={() => (confirm ? remove() : setConfirm(true))}
          >
            {confirm ? "Confirm delete" : "Delete"}
          </button>
          {event.html_link && (
            <a className="btn" href={event.html_link} target="_blank" rel="noopener noreferrer">Open in Google</a>
          )}
        </div>
    </Window>
  );
}
