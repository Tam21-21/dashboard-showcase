import { useState } from "react";
import type { CSSProperties } from "react";
import type { CalendarEvent } from "../../../types";
import { State } from "../../kit";
import { addDays } from "./sports/derive";

// Home's calendar half, from the mockup (lab-p-home.js): the picked day as a list (when, what, how long, and for today
// whether it is over, on now or next), and the next seven days as one capsule a day, as tall as the hours booked, in the
// colour of each kind of event. Pressing a day lists it.

export const two = (n: number) => String(n).padStart(2, "0");
export const hm = (m: number) => `${two(Math.floor(m / 60))}:${two(m % 60)}`;
const minutesOf = (dt: string) => Number(dt.slice(11, 13)) * 60 + Number(dt.slice(14, 16));
// The calendar's titles often carry a code or a room after the name ("Gym - B12", "Call (Zoom)"); the sentence keeps the name.
export const short = (t: string) => t.replace(/\s+-\s+[A-Z]?\d+.*$/, "").replace(/\s*\(.*\)\s*$/, "");

// The kinds of calendar event and their colours (the mockup's own list). The read holds no colour and no calendar name, so the
// kind is worked out from the event's name: CM is a lecture and TD a tutorial in the university's timetable.
const KINDS = [
  ["lecture", "Lecture", "77 155 255"], ["tutorial", "Tutorial", "236 110 205"], ["class", "Class", "150 128 255"],
  ["run", "Run", "255 146 110"], ["swim", "Swim", "40 200 235"], ["football", "Football", "190 225 95"], ["other", "Other", "150 160 180"],
] as const;
export function kindOf(t: string): (typeof KINDS)[number] {
  const k = /^CM\b/.test(t) ? "lecture" : /^TD\b/.test(t) ? "tutorial" : /\brun\b/i.test(t) ? "run" : /\bswim/i.test(t) ? "swim"
    : /\bfootball\b/i.test(t) ? "football" : /\s-\s[A-Z]\d+\s*$/.test(t) ? "class" : "other";
  return KINDS.find((x) => x[0] === k) as (typeof KINDS)[number];
}

// a and b place the block (at least 15 minutes wide, cut at 24:00); when is the event's own time, as the calendar holds it.
export type Ev = { title: string; day: string; a: number; b: number; when: string };
export const isAllDay = (e: CalendarEvent) => e.all_day === 1 || !e.start_dt.includes("T");
/** Timed events as minutes of their day, earliest first. All-day events are counted apart (allDay below). */
export function timed(list: CalendarEvent[]): Ev[] {
  return list.filter((e) => !isAllDay(e)).map((e) => {
    const day = e.start_dt.slice(0, 10), a = minutesOf(e.start_dt);
    const later = !e.end_dt.includes("T") || e.end_dt.slice(0, 10) > day;
    const end = later ? (e.end_dt.includes("T") ? `${hm(minutesOf(e.end_dt))} the next day` : "the end of the day") : hm(minutesOf(e.end_dt));
    return { title: e.title, day, a, b: Math.min(1440, Math.max(later ? 1440 : minutesOf(e.end_dt), a + 15)), when: `${hm(a)} to ${end}` };
  }).sort((x, y) => x.day.localeCompare(y.day) || x.a - y.a);
}
/** The all-day events of one day, placed on their start day as the Calendar page places them. */
export const allDay = (list: CalendarEvent[], day: string) => list.filter((e) => isAllDay(e) && e.start_dt.slice(0, 10) === day);

const dur = (m: number) => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${two(m % 60)}` : `${m / 60} h`);
/** Minutes booked in a day: events that overlap are counted once. */
function booked(list: Ev[]): number {
  let total = 0, end = -1;
  for (const e of list) { const a = Math.max(e.a, end); if (e.b > a) { total += e.b - a; end = e.b; } }
  return total;
}
const longDay = (iso: string) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });
const vars = (v: Record<string, number | string>) => v as CSSProperties;
const AHEAD = 7;
const CAPSULE = 150;

/** The picked day's events as a list, and the seven capsules that pick it. */
export function DayParts({ events, all, today, now }: { events: Ev[]; all: CalendarEvent[]; today: string; now: number }) {
  const [at, setAt] = useState(0);
  const days = Array.from({ length: AHEAD }, (_, i) => {
    const iso = addDays(today, i);
    const list = events.filter((e) => e.day === iso);
    const whole = allDay(all, iso);
    const byKind = KINDS.map((k) => [k, list.filter((e) => kindOf(e.title)[0] === k[0]).reduce((t, e) => t + e.b - e.a, 0)] as const).filter((x) => x[1] > 0);
    return { i, iso, list, whole, mins: booked(list), byKind, wd: new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" }), n: Number(iso.slice(8, 10)) };
  });
  const top = Math.max(1, ...days.map((d) => d.mins));
  const d = days[at];
  const isToday = at === 0;
  const next = isToday ? d.list.find((e) => e.a > now) : undefined;
  const count = d.list.length + d.whole.length;
  const rows = [
    ...d.whole.map((e) => ({ key: e.event_id, when: "All day", name: short(e.title), full: e.title, len: "-", st: null as null | [Parameters<typeof State>[0]["tone"], string] })),
    ...d.list.map((e, k) => ({
      key: `${k}`, when: e.when, name: short(e.title), full: `${e.title}, ${e.when}`, len: dur(e.b - e.a),
      st: !isToday ? null : e.b <= now ? (["dim", "Over"] as const) : e.a <= now ? (["good", "On now"] as const) : e === next ? (["run", "Next"] as const) : null,
    })),
  ];
  return (
    <>
      <div className="nl-blk hm-today" data-testid="day">
        <div className="nl-blk-h hm-hd"><h2>{isToday ? "Today" : longDay(d.iso)}</h2><State tone="run">{`${count} ${count === 1 ? "event" : "events"}`}</State></div>
        {rows.length === 0 ? <p className="empty">Nothing is on the calendar this day.</p> : (
          <table className="hm-tbl">
            <thead><tr><th>When</th><th>What</th><th className="r">How long</th><th /></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key} className={r.st?.[1] === "Over" ? "over" : undefined}>
                  <td className="num">{r.when.split(" to ")[0]}<span className="hm-to">{r.when.slice(r.when.split(" to ")[0].length)}</span></td>
                  <td title={r.full}>{r.name}</td>
                  <td className="num r">{r.len}</td>
                  <td className="r">{r.st && <State tone={r.st[0]}>{r.st[1]}</State>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="nl-blk hm-next" data-testid="week">
        <div className="nl-blk-h hm-hd"><h2 title="Hours booked each day, in the colour of each kind of event. Press a day to list it.">Next 7 days</h2></div>
        <div className="hm-caps">
          {days.map((x) => {
            const n = x.list.length + x.whole.length;
            return (
              <button key={x.iso} type="button" className={`hm-cap${x.i === 0 ? " today" : ""}`} aria-pressed={x.i === at} style={vars({ "--k": x.i })}
                title={`${longDay(x.iso)}${x.whole.map((e) => `\n${short(e.title)}, all day`).join("")}${x.list.map((e) => `\n${short(e.title)}, ${e.when}`).join("") || (x.whole.length ? "" : "\nNothing on the calendar")}`}
                onClick={() => setAt(x.i)}>
                <span className="hm-db">
                <b className="num">{x.mins ? dur(x.mins) : n ? "All day" : "Free"}</b>
                {x.mins ? (
                  <span className="hm-cp" style={{ height: Math.max(10, Math.round((x.mins / top) * CAPSULE)) }}>
                    {x.byKind.map(([k, m]) => <i key={k[0]} title={k[1]} style={vars({ "--c": k[2], flex: `${m} 1 0` })} />)}
                  </span>
                ) : <span className="hm-cp none" />}
                </span>
                <span className="hm-dn">{`${x.wd} ${x.n}`}</span>
              </button>
            );
          })}
        </div>
      </div>
    </>
  );
}
