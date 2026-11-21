import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import type { CalendarEvent } from "../../../types";

// Queries, mutations and the pure date helpers for the Calendar page.

export const REFETCH_MS = 300000;

export const useCalendarStatus = () =>
  useQuery({ queryKey: ["calendar", "status"], queryFn: () => api.life.calendarStatus() });

// The range is inclusive and by start date (calendar_sync.py get_events).
export const rangeQueryOptions = (from: string, to: string) => ({
  queryKey: ["calendar", "range", from, to] as const,
  queryFn: () => api.life.calendarRange(from, to),
  refetchInterval: REFETCH_MS,
});
export const useCalendarRange = (from: string, to: string) => useQuery(rangeQueryOptions(from, to));

export const useWeather = () => useQuery({ queryKey: ["weather"], queryFn: () => api.life.weather() });
export const useForecast = () =>
  useQuery({ queryKey: ["weather", "forecast"], queryFn: () => api.life.weatherForecast() });

export function useCreateEvent(onDone: () => void) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Parameters<typeof api.life.calendarCreate>[0]) => api.life.calendarCreate(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["calendar", "range"] });
      onDone();
    },
  });
}

// Every write refreshes the calendar range; template writes refresh the templates list.
export const TEMPLATES_KEY = ["calendar", "templates"] as const;
const RANGE_KEY = ["calendar", "range"] as const;

export const useTemplates = () => useQuery({ queryKey: TEMPLATES_KEY, queryFn: () => api.life.getTemplates() });

function useRangeWrite<V>(fn: (v: V) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => void qc.invalidateQueries({ queryKey: RANGE_KEY }) });
}
export const useMoveEvent = () =>
  useRangeWrite((v: { event_id: string; start_dt: string; end_dt: string }) =>
    api.life.calendarMoveEvent(v.event_id, { start_dt: v.start_dt, end_dt: v.end_dt }));
export const usePatchEvent = () =>
  useRangeWrite((v: { event_id: string; end_dt: string }) => api.life.calendarPatchEvent(v.event_id, { end_dt: v.end_dt }));
export const useCreateLocal = () =>
  useRangeWrite((body: Parameters<typeof api.life.calendarCreateLocal>[0]) => api.life.calendarCreateLocal(body));
export const useDeleteEvent = () => useRangeWrite((event_id: string) => api.life.deleteCalendarEvent(event_id));

function useTemplateWrite<V>(fn: (v: V) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => void qc.invalidateQueries({ queryKey: TEMPLATES_KEY }) });
}
export const useCreateTemplate = () => useTemplateWrite((body: { title: string; color: string }) => api.life.createTemplate(body));
export const useDeleteTemplate = () => useTemplateWrite((id: number) => api.life.deleteTemplate(id));

// The to-do list. Every key starts with "todos" so the live feed refreshes them all (shell/live.ts).
export const todosDayKey = (date: string) => ["todos", "day", date] as const;
export const todosOpenKey = (before: string) => ["todos", "open", before] as const;
export const todosDoneLateKey = (day: string) => ["todos", "doneLate", day] as const;
export const useTodosDay = (date: string) =>
  useQuery({ queryKey: todosDayKey(date), queryFn: () => api.life.getTodos(date) });
export const useOpenTodos = (before: string) =>
  useQuery({ queryKey: todosOpenKey(before), queryFn: () => api.life.openTodos(before) });

export const useDoneLateTodos = (day: string) =>
  useQuery({ queryKey: todosDoneLateKey(day), queryFn: () => api.life.doneLateTodos(day) });

function useTodoWrite<V>(fn: (v: V) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => void qc.invalidateQueries({ queryKey: ["todos"] }) });
}
/** Today's list: late (open, from earlier days, oldest first), today's open ones by position, then the ticked ones, then late items ticked today (they keep their own day). */
export function useTodoList(today: string) {
  const dayQ = useTodosDay(today);
  const lateQ = useOpenTodos(today);
  const doneLateQ = useDoneLateTodos(today);
  const doneLate = doneLateQ.data ?? [];
  const day = dayQ.data ?? [];
  const late = lateQ.data ?? [];
  const items = [
    ...late.map((t) => ({ todo: t, late: true })),
    ...day.filter((t) => !t.done).map((t) => ({ todo: t, late: false })),
    ...day.filter((t) => t.done).map((t) => ({ todo: t, late: false })),
    ...doneLate.map((t) => ({ todo: t, late: true })),
  ];
  const total = items.length;
  const done = day.filter((t) => t.done).length + doneLate.length;
  return { dayQ, lateQ, doneLateQ, items, total, done, lateCount: late.length };
}
export type TodoItem = ReturnType<typeof useTodoList>["items"][number];

/** "D of N done" plus " · K LATE" text parts. */
export const todoNote = (done: number, total: number) => `${done} of ${total} done`;

export const useAddTodo = () => useTodoWrite((b: { date: string; text: string }) => api.life.createTodo(b));
export const usePatchTodo = () =>
  useTodoWrite((v: { id: number; body: { text?: string; done?: boolean; date?: string } }) => api.life.patchTodo(v.id, v.body));
export const useDeleteTodo = () => useTodoWrite((id: number) => api.life.deleteTodo(id));

// ISO day strings ("YYYY-MM-DD"). All arithmetic is UTC so a day never drifts with the zone.
export const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
export const WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

const pad = (n: number) => String(n).padStart(2, "0");
const utc = (day: string) => {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
};
const toIso = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;

export const todayIso = (): string => {
  const n = new Date();
  return `${n.getFullYear()}-${pad(n.getMonth() + 1)}-${pad(n.getDate())}`;
};
export const addDays = (day: string, n: number): string => {
  const d = utc(day);
  d.setUTCDate(d.getUTCDate() + n);
  return toIso(d);
};
export const weekdayIndex = (day: string) => (utc(day).getUTCDay() + 6) % 7;
export const mondayOf = (day: string): string => addDays(day, -weekdayIndex(day));
export const sundayOf = (day: string): string => addDays(mondayOf(day), 6);
export const monthStart = (day: string): string => `${day.slice(0, 8)}01`;
export const shiftMonth = (day: string, n: number): string => {
  const d = utc(monthStart(day));
  d.setUTCMonth(d.getUTCMonth() + n);
  return toIso(d);
};
export const monthEnd = (day: string): string => addDays(shiftMonth(day, 1), -1);
export const dayNum = (day: string) => Number(day.slice(8));
export const monthName = (day: string) => MONTHS[Number(day.slice(5, 7)) - 1];

export type View = "TODAY" | "DAY" | "WEEK" | "MONTH";

// The week view is seven days starting on the day in view (today when the page opens), not on Monday
// (Tamer, 2026-10-30); the month grid keeps its Monday to Sunday rows.
export function viewDays(view: View, anchor: string): string[] {
  if (view === "TODAY") return [todayIso()];
  if (view === "DAY") return [anchor];
  if (view === "WEEK") return Array.from({ length: 7 }, (_, i) => addDays(anchor, i));
  const first = mondayOf(monthStart(anchor));
  const last = sundayOf(monthEnd(anchor));
  const count = Math.round((utc(last).getTime() - utc(first).getTime()) / 86400000) + 1;
  return Array.from({ length: count }, (_, i) => addDays(first, i));
}

const LONG_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function viewTitle(view: View, anchor: string): string {
  if (view === "TODAY") {
    const t = todayIso();
    return `${LONG_WEEKDAYS[weekdayIndex(t)]} ${dayNum(t)} ${monthName(t)}`;
  }
  if (view === "DAY") return `${dayNum(anchor)} ${monthName(anchor)} ${anchor.slice(0, 4)}`;
  if (view === "MONTH") return `${monthName(anchor)} ${anchor.slice(0, 4)}`;
  const a = anchor;
  const b = addDays(anchor, 6);
  if (a.slice(0, 4) !== b.slice(0, 4)) {
    return `${dayNum(a)} ${monthName(a)} ${a.slice(0, 4)} to ${dayNum(b)} ${monthName(b)} ${b.slice(0, 4)}`;
  }
  if (a.slice(5, 7) !== b.slice(5, 7)) return `${dayNum(a)} ${monthName(a)} to ${dayNum(b)} ${monthName(b)} ${b.slice(0, 4)}`;
  return `${dayNum(a)} to ${dayNum(b)} ${monthName(b)} ${b.slice(0, 4)}`;
}

export function stepAnchor(view: View, anchor: string, dir: 1 | -1): string {
  if (view === "TODAY") return anchor;
  if (view === "DAY") return addDays(anchor, dir);
  if (view === "WEEK") return addDays(anchor, 7 * dir);
  return shiftMonth(anchor, dir);
}

// Time helpers, adapted from src/routes/calendar/CalendarPage.tsx (toMinutes, fmtTime, evDuration).
// The store keeps naive local strings ("2026-10-05T09:00:00"), so they are read as text.
export const GRID_START_HOUR = 6;
export const GRID_END_HOUR = 23;
export const PX_PER_HOUR = 48;
// The hours a grid draws, as the mockup: 08:00 to 20:00, wider when an event in view starts earlier or ends later
// (06:00 to 23:00 at most).
export function gridHours(events: CalendarEvent[]): { start: number; end: number } {
  const timed = events.filter(isTimed);
  const starts = timed.map((e) => Math.floor(toMinutes(e.start_dt) / 60));
  const ends = timed.map((e) => Math.ceil((toMinutes(e.start_dt) + evDuration(e)) / 60));
  return { start: Math.max(GRID_START_HOUR, Math.min(8, ...starts)), end: Math.min(GRID_END_HOUR, Math.max(20, ...ends)) };
}

export const isTimed = (e: CalendarEvent) => e.all_day !== 1 && e.start_dt.includes("T");
export const eventDay = (e: CalendarEvent) => e.start_dt.slice(0, 10);
export const toMinutes = (s: string): number => Number(s.slice(11, 13)) * 60 + Number(s.slice(14, 16));
export const hhmm = (s: string): string => s.slice(11, 16);
export const evDuration = (e: CalendarEvent): number =>
  e.end_dt.includes("T") ? Math.max(toMinutes(e.end_dt) - toMinutes(e.start_dt), 30) : 30;
export const timeRange = (e: CalendarEvent): string => `${hhmm(e.start_dt)} to ${hhmm(e.end_dt)}`;
export const eventHover = (e: CalendarEvent): string =>
  [e.title, isTimed(e) ? timeRange(e) : "All day", e.location].filter(Boolean).join(", ");

export function groupByDay(events: CalendarEvent[]): Map<string, CalendarEvent[]> {
  const m = new Map<string, CalendarEvent[]>();
  for (const e of events) {
    const k = eventDay(e);
    m.set(k, [...(m.get(k) ?? []), e]);
  }
  for (const list of m.values()) list.sort((a, b) => a.start_dt.localeCompare(b.start_dt));
  return m;
}

// Events that overlap in time are one group (a chain counts: A with B, B with C). Each group keeps its
// events in start order, with the group's start and end in minutes of the day.
export type EventGroup = { events: CalendarEvent[]; start: number; end: number };
export function layoutGroups(events: CalendarEvent[]): EventGroup[] {
  const sorted = [...events].sort(
    (p, q) => toMinutes(p.start_dt) - toMinutes(q.start_dt) || evDuration(q) - evDuration(p),
  );
  const out: EventGroup[] = [];
  for (const ev of sorted) {
    const s = toMinutes(ev.start_dt);
    const e = s + evDuration(ev);
    const open = out[out.length - 1];
    if (open && s < open.end) {
      open.events.push(ev);
      open.end = Math.max(open.end, e);
    } else {
      out.push({ events: [ev], start: s, end: e });
    }
  }
  return out;
}

// A colour at a share over the page background (#0a0d11), as the mockup's mixHex.
const mixHex = (hex: string, share: number): string => {
  const n = parseInt(hex.slice(1), 16);
  const bg = [10, 13, 17];
  const rgb = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c, i) => Math.round(c * share + bg[i] * (1 - share)));
  return `rgb(${rgb.join(",")})`;
};

// The card's fill: the first colour at 20% over the page, each later one laid on as a diagonal wave
// with a 1.5px line, as the mockup's waveBg. Returns a CSS url() value.
export function waveBackground(colours: string[]): string {
  let svg = `<rect width="100" height="100" fill="${mixHex(colours[0], 0.2)}"/>`;
  for (let i = 1; i < colours.length; i++) {
    const y = (i * 100) / colours.length;
    const a = y + 22;
    const b = y - 22;
    const d = `M0 ${a} C35 ${a + 14} 65 ${b - 14} 100 ${b}`;
    svg += `<path d="${d} L100 100 L0 100Z" fill="${mixHex(colours[i], 0.2)}"/>`;
    svg += `<path d="${d}" fill="none" stroke="${colours[i]}" stroke-opacity=".75" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
  }
  const doc = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" preserveAspectRatio="none">${svg}</svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(doc)}")`;
}

// Drag and resize maths. The grid runs the hours gridHours gives, at 48px an hour (full screen: the height that fits the
// screen, passed as pxPerHour); all times are minutes of the day.
export const SNAP_MIN = 15;
export const MAX_DRAG_MIN = 17 * 60;
const snap = (m: number) => Math.round(m / SNAP_MIN) * SNAP_MIN;
const pxToMin = (px: number, pxPerHour: number) => (px * 60) / pxPerHour;

// The start minute for a drop: 06:00 plus the pointer offset from the column top, snapped to 15 minutes,
// and held so the block of `duration` minutes ends at or before 23:00 and starts at or after 06:00.
export function dropMinute(
  clientY: number, columnTop: number, duration: number, pxPerHour = PX_PER_HOUR, startHour = GRID_START_HOUR, endHour = GRID_END_HOUR,
): number {
  const raw = snap(startHour * 60 + pxToMin(clientY - columnTop, pxPerHour));
  const latest = endHour * 60 - duration;
  return Math.max(startHour * 60, Math.min(raw, latest));
}
// The end minute while stretching: moved by whole 15 minute steps, never under 15 minutes long.
export function resizeEnd(startMin: number, baseEnd: number, deltaPx: number, pxPerHour = PX_PER_HOUR, endHour = GRID_END_HOUR): number {
  const end = baseEnd + snap(pxToMin(deltaPx, pxPerHour));
  return Math.min(Math.max(end, startMin + SNAP_MIN), Math.max(endHour * 60, baseEnd));
}
export const minToStamp = (day: string, m: number): string =>
  `${day}T${pad(Math.floor(m / 60))}:${pad(m % 60)}:00`;
// Real length in minutes of an event that starts and ends on one day, else null.
export function sameDayLength(e: CalendarEvent): number | null {
  if (!isTimed(e) || !e.end_dt.includes("T") || e.end_dt.slice(0, 10) !== eventDay(e)) return null;
  const len = toMinutes(e.end_dt) - toMinutes(e.start_dt);
  return len > 0 ? len : null;
}
export const isDraggable = (e: CalendarEvent): boolean => {
  const len = sameDayLength(e);
  return len !== null && len <= MAX_DRAG_MIN;
};

// What is being dragged: held in React state from dragStart, because browsers answer "" from getData during dragover.
export type Drag =
  | { kind: "event"; id: string; length: number }
  | { kind: "template"; id: number; title: string; color: string; length: number };
export const TEMPLATE_LENGTH = 60;

/** An event's name without the code or room the university's timetable adds ("Gym - B12", "Call (Zoom)"); the hover keeps the full title. */
export const shortTitle = (t: string) => t.replace(/\s+-\s+[A-Z]?\d+.*$/, "").replace(/\s*\(.*\)\s*$/, "");
