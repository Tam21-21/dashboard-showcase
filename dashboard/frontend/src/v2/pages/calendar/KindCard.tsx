import type { CSSProperties } from "react";
import { Block } from "../../kit";
import type { CalendarEvent } from "../../../types";
import { KINDS, kindOf } from "./accent";
import { isTimed } from "./data";

// Time by kind (mockup: lab-p-calendar.js): how the booked time of the days on show splits across the kinds of event, each kind
// in its one colour, here and in the calendar. Press a kind to dim the others.

const minutesOf = (dt: string) => Number(dt.slice(11, 13)) * 60 + Number(dt.slice(14, 16));
const dur = (m: number) => (m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m / 60} h`);

/** The minutes an event books: its end minus its start, cut at the end of its day; an all-day event books none. */
export function bookedMinutes(e: CalendarEvent): number {
  if (!isTimed(e)) return 0;
  const a = minutesOf(e.start_dt);
  const later = !e.end_dt.includes("T") || e.end_dt.slice(0, 10) > e.start_dt.slice(0, 10);
  return Math.max(0, (later ? 1440 : minutesOf(e.end_dt)) - a);
}

export default function KindCard({ events, span, only, onOnly }: { events: CalendarEvent[]; span: string; only: string | null; onOnly: (k: string | null) => void }) {
  const kinds = KINDS.map((k) => {
    const list = events.filter((e) => kindOf(e.title).key === k.key);
    return { ...k, cnt: list.length, min: list.reduce((t, e) => t + bookedMinutes(e), 0) };
  }).filter((k) => k.cnt > 0).sort((a, b) => b.min - a.min);
  const total = kinds.reduce((t, k) => t + k.min, 0);
  const vars = (hex: string, extra: Record<string, string | number> = {}) => ({ "--k": hex, ...extra }) as CSSProperties;
  return (
    <Block id="calKind" title={<span title={`${span}: ${events.length} ${events.length === 1 ? "event" : "events"}. Each kind of event has one colour, here and in the calendar. The kind is worked out from the event's name: CM is a lecture, TD a tutorial, a name that ends in a room number a class. Press a kind to show only that kind.`}>Time by kind</span>}
      right={total > 0 ? <b className="kt-tot num">{dur(total)}</b> : undefined}>
      <div data-testid="kinds">
        {kinds.length === 0 ? <p className="xn">{`Nothing in the calendar for ${span.toLowerCase()}.`}</p> : (
          <>
            <div className="kt-bar">
              {kinds.filter((k) => k.min > 0).map((k) => <i key={k.key} className={only && only !== k.key ? "off" : undefined} style={vars(k.hex, { flex: `${k.min} 1 0` })} title={`${k.name}: ${dur(k.min)}, ${Math.round((k.min / total) * 100)}% of the time`} />)}
            </div>
            <div className="kt-list">
              {kinds.map((k) => (
                <button key={k.key} type="button" className={`kt-k${only === k.key ? " on" : only ? " off" : ""}`} style={vars(k.hex)} aria-pressed={only === k.key}
                  title={`${k.name}: ${k.cnt} ${k.cnt === 1 ? "event" : "events"}`} onClick={() => onOnly(only === k.key ? null : k.key)}>
                  <i /><span>{k.name}</span><b className="num">{k.min ? dur(k.min) : "All day"}</b>
                </button>
              ))}
            </div>
          </>
        )}
      </div>
    </Block>
  );
}
