import { useEffect, useRef, useState } from "react";
import type { CSSProperties, DragEvent } from "react";
import { eventAccent, kindOf, toAccent } from "./accent";
import type { CalendarEvent, WeatherForecastDay } from "../../../types";
import { WxIcon, forecastFor } from "./WeatherCard";
import { serverDetail } from "../../../lib/errorText";
import {
  PX_PER_HOUR, gridHours, WEEKDAYS, dayNum, dropMinute, eventDay, eventHover,
  evDuration, groupByDay, isDraggable, isTimed, hhmm, layoutGroups, minToStamp, waveBackground, resizeEnd, sameDayLength, timeRange,
  shortTitle, toMinutes, useCreateLocal, useMoveEvent, usePatchEvent, weekdayIndex,
} from "./data";
import type { Drag, EventGroup } from "./data";

// The day and week views: a sticky day header, a sticky ALL DAY row, then the hours of gridHours at 48px an hour.
// With fit (full screen) the hour height is the one that shows those hours without scrolling.
// The smallest fitted hour: a 30 minute event still shows one line of its title.
const MIN_FIT_PX_PER_HOUR = 36;
// A block shorter than this (a 30 minute event at a fitted height) drops its padding so the one title line fits.
const SHORT_PX = 26;
const accent = (e: CalendarEvent) => ({ "--e": eventAccent(e) }) as CSSProperties;

type Resize = { id: string; day: string; startY: number; start: number; base: number };
type Ghost = { day: string; start: number; length: number };

function Block({
  ev, pxh, h0, onOpen, endMin, onDragStart, onDragEnd, onResizeStart,
}: {
  ev: CalendarEvent; pxh: number; h0: number; onOpen: (e: CalendarEvent) => void; endMin: number | null;
  onDragStart: (e: CalendarEvent, de: DragEvent<HTMLDivElement>) => void; onDragEnd: () => void;
  onResizeStart: (e: CalendarEvent, clientY: number) => void;
}) {
  const movable = isDraggable(ev);
  const startMin = toMinutes(ev.start_dt);
  const top = (Math.max(startMin, h0 * 60) - h0 * 60) * (pxh / 60) + 1;
  const minutes = endMin === null ? evDuration(ev) : endMin - startMin;
  const height = minutes * (pxh / 60) - 3;
  const showTime = height >= 44;
  const lines = showTime ? Math.max(1, Math.floor((height - 10 - 16) / 17)) : 1;
  return (
    <div
      className={`ev${height < SHORT_PX ? " short" : ""}`}
      data-kind={kindOf(ev.title).key}
      role="button"
      tabIndex={0}
      draggable={movable ? true : undefined}
      title={eventHover(ev)}
      style={{ ...accent(ev), top, height, left: 4, right: 4 }}
      onClick={() => onOpen(ev)}
      onKeyDown={(k) => k.key === "Enter" && onOpen(ev)}
      onDragStart={movable ? (de) => onDragStart(ev, de) : undefined}
      onDragEnd={movable ? onDragEnd : undefined}
    >
      <b style={{ WebkitLineClamp: lines }}>{shortTitle(ev.title)}</b>
      {showTime && <span className="num ev-t">{timeRange(ev)}</span>}
      {showTime && <span className="num ev-s">{hhmm(ev.start_dt)}</span>}
      {movable && (
        <div
          className="ev-h"
          data-testid={`resize-${ev.event_id}`}
          onMouseDown={(m) => {
            m.preventDefault();
            m.stopPropagation();
            onResizeStart(ev, m.clientY);
          }}
          onClick={(c) => c.stopPropagation()}
        />
      )}
    </div>
  );
}

// Events at the same time: one card from the first start to the last end, one line per event.
// Each line opens or drags its own event; the card has no resize handle.
function GroupBlock({
  group, pxh, h0, onOpen, onDragStart, onDragEnd,
}: {
  group: EventGroup; pxh: number; h0: number; onOpen: (e: CalendarEvent) => void;
  onDragStart: (e: CalendarEvent, de: DragEvent<HTMLDivElement>) => void; onDragEnd: () => void;
}) {
  const { events } = group;
  const top = (Math.max(group.start, h0 * 60) - h0 * 60) * (pxh / 60) + 1;
  const height = (group.end - group.start) * (pxh / 60) - 3;
  const fit = Math.max(1, Math.floor((height - 10) / 17));
  const shown = fit >= events.length ? events : events.slice(0, fit - 1);
  const rest = events.length - shown.length;
  const showTime = rest === 0 && height - 10 - 17 * events.length >= 16;
  const colours = events.map((e) => eventAccent(e));
  const clock = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const span = `${clock(group.start)} to ${clock(group.end)}`;
  return (
    <div
      className={`ev grp${height < SHORT_PX ? " short" : ""}`}
      title={events.map(eventHover).join("\n")}
      style={{
        top, height, left: 4, right: 4,
        backgroundImage: waveBackground(colours),
        "--bar": `linear-gradient(to bottom,${colours.join(",")})`,
      } as CSSProperties}
    >
      {shown.map((e) => {
        const movable = isDraggable(e);
        return (
          <div
            key={e.event_id}
            className="gl"
            role="button"
            tabIndex={0}
            draggable={movable ? true : undefined}
            title={eventHover(e)}
            style={accent(e)}
            onClick={() => onOpen(e)}
            onKeyDown={(k) => k.key === "Enter" && onOpen(e)}
            onDragStart={movable ? (de) => onDragStart(e, de) : undefined}
            onDragEnd={movable ? onDragEnd : undefined}
          >
            <i />
            <span className="gn">{shortTitle(e.title)}</span>
          </div>
        );
      })}
      {rest > 0 && (
        <div className="gl more">
          {shown.length > 0 ? `${rest} more` : `${events[0].title} and ${events.length - 1} more`}
        </div>
      )}
      {showTime && <span className="num ev-t">{span}</span>}
      {showTime && <span className="num ev-s">{span.slice(0, 5)}</span>}
    </div>
  );
}

export default function Grid({
  days, today, events, forecast, onOpen, drag, setDrag, onError, fit = false,
}: {
  days: string[];
  today: string;
  events: CalendarEvent[];
  forecast: WeatherForecastDay[] | undefined;
  onOpen: (e: CalendarEvent) => void;
  drag: Drag | null;
  setDrag: (d: Drag | null) => void;
  onError: (text: string | null) => void;
  fit?: boolean;
}) {
  const byDay = groupByDay(events);
  const { start: h0, end: h1 } = gridHours(days.flatMap((d) => byDay.get(d) ?? []));
  const HOURS = Array.from({ length: h1 - h0 }, (_, i) => h0 + i);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [fitPx, setFitPx] = useState<number | null>(null);
  const pxh = fit && fitPx !== null ? fitPx : PX_PER_HOUR;

  // Full screen: the hour height follows the hour area's own height (the screen, not the events), whole pixels.
  useEffect(() => {
    const el = scrollRef.current;
    if (!fit || !el) {
      setFitPx(null);
      return;
    }
    const measure = () => setFitPx(Math.max(MIN_FIT_PX_PER_HOUR, Math.floor(el.clientHeight / HOURS.length)));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [fit, HOURS.length]);
  const move = useMoveEvent();
  const patch = usePatchEvent();
  const createLocal = useCreateLocal();
  const [ghost, setGhost] = useState<Ghost | null>(null);
  const [resize, setResize] = useState<Resize | null>(null);
  const [liveEnd, setLiveEnd] = useState<number | null>(null);
  const endRef = useRef(0);
  const fail = (err: unknown) => onError(serverDetail(err));

  // Stretching: document listeners live only while the handle is held, and go on release or unmount.
  useEffect(() => {
    if (!resize) return;
    const onMove = (m: MouseEvent) => {
      endRef.current = resizeEnd(resize.start, resize.base, m.clientY - resize.startY, pxh, h1);
      setLiveEnd(endRef.current);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      const end = endRef.current;
      setResize(null);
      setLiveEnd(null);
      if (end !== resize.base) {
        onError(null);
        patch.mutate({ event_id: resize.id, end_dt: minToStamp(resize.day, end) }, { onError: fail });
      }
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, [resize, pxh, h1]);

  const startResize = (e: CalendarEvent, clientY: number) => {
    const base = toMinutes(e.end_dt);
    endRef.current = base;
    setResize({ id: e.event_id, day: eventDay(e), startY: clientY, start: toMinutes(e.start_dt), base });
  };
  const startDrag = (e: CalendarEvent, de: DragEvent<HTMLDivElement>) => {
    de.dataTransfer.setData("event-id", e.event_id);
    de.dataTransfer.effectAllowed = "move";
    setDrag({ kind: "event", id: e.event_id, length: sameDayLength(e) ?? 0 });
  };
  const endDrag = () => {
    setDrag(null);
    setGhost(null);
  };
  const minuteAt = (de: DragEvent<HTMLDivElement>, length: number) =>
    dropMinute(de.clientY, de.currentTarget.getBoundingClientRect().top, length, pxh, h0, h1);
  const onOver = (day: string) => (de: DragEvent<HTMLDivElement>) => {
    if (!drag) return;
    de.preventDefault();
    const start = minuteAt(de, drag.length);
    setGhost((g) => (g && g.day === day && g.start === start ? g : { day, start, length: drag.length }));
  };
  const onDropDay = (day: string) => (de: DragEvent<HTMLDivElement>) => {
    if (!drag) return;
    de.preventDefault();
    const start = minuteAt(de, drag.length);
    const startDt = minToStamp(day, start);
    const endDt = minToStamp(day, start + drag.length);
    endDrag();
    onError(null);
    if (drag.kind === "event") move.mutate({ event_id: drag.id, start_dt: startDt, end_dt: endDt }, { onError: fail });
    else createLocal.mutate({ title: drag.title, color_id: drag.color, start_dt: startDt, end_dt: endDt }, { onError: fail });
  };
  const cols = { gridTemplateColumns: `56px repeat(${days.length}, minmax(0, 1fr))` };
  const hourCols = { ...cols, "--pxh": `${pxh}px` } as CSSProperties;
  return (
    <>
    <div className="chead" style={cols}>
      <div className="chd" />
      {days.map((d) => {
        const f = forecastFor(forecast, d);
        return (
          <div key={d} data-testid={`dayhead-${d}`} className={`chd${d === today ? " today" : ""}`}>
            <div className="dn">{WEEKDAYS[weekdayIndex(d)]}</div>
            <div className="dd num">{dayNum(d)}</div>
            <div className="dw">
              {f ? (
                <>
                  <WxIcon icon={f.icon} />
                  <span className="num">{Math.round(f.temp_max)}°</span>
                  <span className="num lo">{Math.round(f.temp_min)}°</span>
                </>
              ) : (
                <span className="nf" title="No forecast for this day">-</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
    <div className="callday" style={cols}>
      <div className="cad lbl">All day</div>
      {days.map((d) => (
        <div key={d} data-testid={`allday-${d}`} className={`cad${d === today ? " today" : ""}`}>
          {(byDay.get(d) ?? []).filter((e) => !isTimed(e)).map((e) => (
            <div
              key={e.event_id}
              className="ev all"
              data-kind={kindOf(e.title).key}
              role="button"
              tabIndex={0}
              title={eventHover(e)}
              style={accent(e)}
              onClick={() => onOpen(e)}
              onKeyDown={(k) => k.key === "Enter" && onOpen(e)}
            >
              {shortTitle(e.title)}
            </div>
          ))}
        </div>
      ))}
    </div>
    <div className="chours" data-testid="hours-scroll" ref={scrollRef}>
    <div className="cgrid" style={hourCols}>
      <div className="hrs">
        {HOURS.map((h) => (
          <div key={h} className="num" style={{ height: pxh }}>{String(h).padStart(2, "0")}:00</div>
        ))}
      </div>
      {days.map((d) => (
        <div
          key={d}
          data-testid={`hours-${d}`}
          className={`cday${d === today ? " today" : ""}`}
          style={{ height: HOURS.length * pxh }}
          onDragOver={onOver(d)}
          onDrop={onDropDay(d)}
        >
          {layoutGroups((byDay.get(d) ?? []).filter(isTimed)).map((g) =>
            g.events.length === 1 ? (
              <Block
                key={g.events[0].event_id} ev={g.events[0]} pxh={pxh} h0={h0} onOpen={onOpen}
                endMin={resize && resize.id === g.events[0].event_id ? liveEnd : null}
                onDragStart={startDrag} onDragEnd={endDrag} onResizeStart={startResize}
              />
            ) : (
              <GroupBlock
                key={g.events.map((e) => e.event_id).join("+")} group={g} pxh={pxh} h0={h0} onOpen={onOpen}
                onDragStart={startDrag} onDragEnd={endDrag}
              />
            ),
          )}
          {ghost && ghost.day === d && drag && (
            <div
              className="ev ghost"
              data-testid="drop-ghost"
              style={{
                top: (ghost.start - h0 * 60) * (pxh / 60) + 1,
                height: ghost.length * (pxh / 60) - 3,
                left: 4, width: "calc(100% - 8px)",
                ...(drag.kind === "template" ? { "--e": toAccent(drag.color) } : {}),
              } as CSSProperties}
            />
          )}
        </div>
      ))}
    </div>
    </div>
    </>
  );
}
