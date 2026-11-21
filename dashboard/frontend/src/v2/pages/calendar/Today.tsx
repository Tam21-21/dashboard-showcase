import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { Block, DatePick, ShareBar, State, StateWord } from "../../kit";
import { AMBER, GREEN } from "../../kit/parts";
import { useLayout } from "../../kit/phone";
import { serverDetail } from "../../../lib/errorText";
import type { CalendarEvent } from "../../../types";
import { kindOf } from "./accent";
import {
  MONTHS, dayNum, hhmm, isTimed, timeRange, todayIso, todoNote, useAddTodo, useCalendarRange,
  useDeleteTodo, usePatchTodo, useTodoList,
} from "./data";
import type { TodoItem } from "./data";
import "./today.css";

// TODAY: the day's events, then the to-do list under them (never side by side).
// Drawn after docs/design/rail_mockup_2026-10-10.html, round 10 (agenda and todoPanel).

const GREY = "rgba(255,255,255,.14)";

/** The small bar of a to-do list: one part per item, green ticked, amber late, grey open. */
export function TodoBar({ items }: { items: TodoItem[] }) {
  const parts: Array<[number, string, string]> = [
    ...items.filter((i) => i.todo.done).map((): [number, string, string] => [1, GREEN, "Done"]),
    ...items.filter((i) => !i.todo.done && i.late).map((): [number, string, string] => [1, AMBER, "Late"]),
    ...items.filter((i) => !i.todo.done && !i.late).map((): [number, string, string] => [1, GREY, "Still to do"]),
  ];
  return <ShareBar parts={parts} />;
}

/** The note beside the TO DO title: "D of N done", with " · K LATE" in amber when K is above 0. */
export function TodoNote({ done, total, late }: { done: number; total: number; late: number }) {
  return (
    <span className="num" title="Ticked today out of today's list; late items are open ones from earlier days">
      {todoNote(done, total)}
      {late > 0 && <> · <b className="tdlate">{late} LATE</b></>}
    </span>
  );
}

/** "30 OCT" from an ISO day. */
export const shortDay = (iso: string) => `${dayNum(iso)} ${MONTHS[Number(iso.slice(5, 7)) - 1].slice(0, 3).toUpperCase()}`;

const minutesNow = (d: Date) => d.getHours() * 60 + d.getMinutes();
const startMin = (e: CalendarEvent) => (isTimed(e) ? Number(e.start_dt.slice(11, 13)) * 60 + Number(e.start_dt.slice(14, 16)) : -1);
const endMin = (e: CalendarEvent) =>
  isTimed(e) && e.end_dt.includes("T") ? Number(e.end_dt.slice(11, 13)) * 60 + Number(e.end_dt.slice(14, 16)) : null;

const short = (t: string) => t.replace(/\s+-\s+[A-Z]?\d+.*$/, "").replace(/\s*\(.*\)\s*$/, "");
const dur = (m: number) => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m / 60} h`);

// TODAY as the mockup draws it: a table of when, what (with the kind's dot), which kind, how long, and whether it is over,
// on now or next. The to-do list sits above the calendar, never in this table.
export default function Today({ onOpen, todo = true }: { onOpen?: (e: CalendarEvent) => void; todo?: boolean }) {
  const today = todayIso();
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60000);
    return () => clearInterval(id);
  }, []);

  const phone = useLayout() === "phone";
  const rangeQ = useCalendarRange(today, today);
  const events = [...(rangeQ.data ?? [])].sort((a, b) => startMin(a) - startMin(b) || a.start_dt.localeCompare(b.start_dt));
  const nowMin = minutesNow(now);
  const next = events.find((e) => isTimed(e) && startMin(e) > nowMin);

  return (
    <div className="tdy">
      {rangeQ.isError ? (
        <p className="xn">{serverDetail(rangeQ.error)}</p>
      ) : events.length === 0 ? (
        <p className="xn">{rangeQ.isPending ? "Loading" : "Nothing in the calendar today."}</p>
      ) : (
        <table className="tdt">
          <thead><tr><th>{phone ? "From" : "When"}</th><th>What</th><th className="k">Kind</th>{!phone && <th className="r">How long</th>}<th /></tr></thead>
          <tbody>
            {events.map((e) => {
              const end = endMin(e);
              const timed = isTimed(e);
              const past = timed && end !== null && end <= nowMin;
              const on = timed && !past && startMin(e) <= nowMin;
              const k = kindOf(e.title);
              return (
                <tr key={e.event_id} className="ar" role="button" tabIndex={0} style={{ "--e": k.hex } as CSSProperties} onClick={() => onOpen?.(e)} onKeyDown={(k2) => k2.key === "Enter" && onOpen?.(e)}>
                  <td className="num">{timed ? (phone ? hhmm(e.start_dt) : timeRange(e)) : "All day"}</td>
                  <td title={e.title}><i className="kd" />{short(e.title)}</td>
                  <td className="k">{k.name}</td>
                  {!phone && <td className="num r">{timed && end !== null ? dur(end - startMin(e)) : "-"}</td>}
                  <td className="r">{past ? <State tone="dim">Over</State> : on ? <State tone="good">On now</State> : e === next ? <State tone="run">Next</State> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {todo && <TodoPanel today={today} />}
    </div>
  );
}

export function TodoPanel({ today }: { today: string }) {
  const list = useTodoList(today);
  const add = useAddTodo();
  const patch = usePatchTodo();
  const del = useDeleteTodo();
  const [text, setText] = useState("");
  const [day, setDay] = useState(today);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ id: number; text: string } | null>(null);

  const fail = (err: unknown) => setError(serverDetail(err));
  const submit = () => {
    if (add.isPending) return;
    setError(null);
    add.mutate({ date: day, text }, { onSuccess: () => setText(""), onError: fail });
  };
  const tick = (i: TodoItem) => {
    setError(null);
    // A ticked late item keeps its own day (done late), DECISIONS #487 item 4.
    patch.mutate({ id: i.todo.id, body: { done: !i.todo.done } }, { onError: fail });
  };
  const save = () => {
    if (!editing) return;
    const { id, text: next } = editing;
    setEditing(null);
    setError(null);
    patch.mutate({ id, body: { text: next } }, { onError: fail });
  };

  const loadError = list.dayQ.isError ? list.dayQ.error : list.lateQ.isError ? list.lateQ.error : list.doneLateQ.isError ? list.doneLateQ.error : null;

  const open = list.items.filter((i) => !i.todo.done).length;
  const stateTone = list.lateCount > 0 ? "warn" : open > 0 ? "run" : "good";
  const stateText = list.lateCount > 0 ? `${list.lateCount} late` : open > 0 ? `${open} open` : "All done";
  return (
    <Block id="calTodo" className="cal-todo" title="To do" state={<State tone={stateTone}>{stateText}</State>}>
      {list.total > 0 && (
        <>
          <div className="tdbarw"><TodoBar items={list.items} /></div>
          <p className="xn" title="Ticked today out of today's list; late items are open ones from earlier days">{`${list.done} of ${list.total} done${list.lateCount ? `, ${list.lateCount} late` : ""}.`}</p>
        </>
      )}
      {error && <div className="tderr">{error}</div>}
      {loadError ? (
        <div className="empty">{serverDetail(loadError)}</div>
      ) : list.total === 0 ? (
        <p className="xn">{list.dayQ.isPending || list.lateQ.isPending || list.doneLateQ.isPending ? "Loading" : "Nothing to do today."}</p>
      ) : (
        list.items.map((i) => (
          <div key={i.todo.id} className={`td${i.todo.done ? " done" : ""}`}>
            <button
              type="button"
              className="ck"
              aria-label={`${i.todo.done ? "Mark not done" : "Mark done"}: ${i.todo.text}`}
              title={i.todo.done ? "Not done" : "Done"}
              onClick={() => tick(i)}
            >
              {i.todo.done ? <svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10" /></svg> : null}
            </button>
            {editing?.id === i.todo.id ? (
              <input
                className="tdedit"
                autoFocus
                value={editing.text}
                maxLength={200}
                aria-label="Edit the to-do"
                onChange={(e) => setEditing({ id: i.todo.id, text: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") save();
                  if (e.key === "Escape") setEditing(null);
                }}
              />
            ) : (
              <span className="tx" title="Click to edit" onClick={() => setEditing({ id: i.todo.id, text: i.todo.text })}>
                {i.todo.text}
              </span>
            )}
            <span className="tdend">
              {i.late && !i.todo.done && <span title={`Late since ${shortDay(i.todo.date)}`}><StateWord word="LATE" tone="a" /></span>}
              {i.late && !i.todo.done && (
                <DatePick
                  value={i.todo.date}
                  ariaLabel={`New date: ${i.todo.text}`}
                  onChange={(d) => { setError(null); patch.mutate({ id: i.todo.id, body: { date: d } }, { onError: fail }); }}
                />
              )}
              <button type="button" className="x" aria-label={`Delete: ${i.todo.text}`} title="Delete" onClick={() => del.mutate(i.todo.id, { onError: fail })}>
                ×
              </button>
            </span>
          </div>
        ))
      )}
      <div className="tdadd">
        <input
          type="text"
          placeholder="Add a to-do"
          aria-label="Add a to-do"
          value={text}
          maxLength={200}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        />
        <DatePick value={day} onChange={setDay} ariaLabel="Day of the to-do" />
        <button type="button" className="btn" onClick={submit}>Add</button>
      </div>
    </Block>
  );
}


