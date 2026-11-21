import type { CSSProperties } from "react";
import { eventAccent, kindOf } from "./accent";
import type { CalendarEvent } from "../../../types";
import { WEEKDAYS, dayNum, shortTitle, eventHover, groupByDay, hhmm, isTimed } from "./data";

// Month view: whole weeks, up to three events a day, then "N more". A double click opens the day.

export default function Month({
  days, month, today, events, onOpenDay, onOpen,
}: {
  days: string[];
  month: string;
  today: string;
  events: CalendarEvent[];
  onOpenDay: (day: string) => void;
  onOpen: (e: CalendarEvent) => void;
}) {
  const byDay = groupByDay(events);
  return (
    <div className="cmon">
      <div className="mhd">
        {WEEKDAYS.map((w) => (
          <div key={w}>{w}</div>
        ))}
      </div>
      <div className="mgrid" style={{ gridTemplateRows: `repeat(${days.length / 7}, minmax(0, 1fr))` }}>
        {days.map((d) => {
          const list = byDay.get(d) ?? [];
          const out = d.slice(0, 7) !== month.slice(0, 7);
          return (
            <div
              key={d}
              data-testid={`cell-${d}`}
              className={`mc${out ? " out" : ""}${d === today ? " today" : ""}`}
              onDoubleClick={() => onOpenDay(d)}
            >
              <div className="mt">
                <span className="num">{dayNum(d)}</span>
              </div>
              {list.slice(0, 3).map((e) => (
                <div
                  key={e.event_id}
                  className="mev"
                  data-kind={kindOf(e.title).key}
                  role="button"
                  tabIndex={0}
                  title={eventHover(e)}
                  style={{ "--e": eventAccent(e) } as CSSProperties}
                  onClick={() => onOpen(e)}
                  onKeyDown={(k) => k.key === "Enter" && onOpen(e)}
                >
                  <i />
                  {isTimed(e) && <span className="num">{hhmm(e.start_dt)}</span>}
                  <span className="mtitle">{shortTitle(e.title)}</span>
                </div>
              ))}
              {list.length > 3 && (
                <div
                  className="more"
                  title={list.slice(3).map((e) => `${isTimed(e) ? hhmm(e.start_dt) : "All day"} ${e.title}`).join("\n")}
                >
                  {list.length - 3} more
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
