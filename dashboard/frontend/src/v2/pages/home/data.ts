import { useQuery } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { serverDetail } from "../../../lib/errorText";
import { fetchJudgeSummary, fetchJudgeVerdicts } from "../../data/judgeApi";
import type { JudgeVerdictRow } from "../../data/judgeApi";
import { fetchStrategiesOverview, strategiesKeys } from "../../data/strategiesApi";
import { getRunningRuns, getSummary } from "../../data/paperApi";
import type { SummaryWindow } from "../../data/paperTypes";
import type { CalendarEvent, GarminActivity } from "../../../types";

// One query per route, each read once by the page and shared by every place that shows it.
// The keys are the ones the older pages use where they exist.

export const useJudgeSummary = () => useQuery({ queryKey: ["judge", "summary"], queryFn: () => fetchJudgeSummary() });
export const useRunningRuns = () => useQuery({ queryKey: ["paper", "runs"], queryFn: getRunningRuns });
export const usePaperSummary = (window: SummaryWindow) =>
  useQuery({ queryKey: ["paper", "summary", window], queryFn: () => getSummary(window) });
export const useStrategiesOverview = () =>
  useQuery({ queryKey: strategiesKeys.overview, queryFn: () => fetchStrategiesOverview() });
// The same key and size as the Strategies page, so the two share one read.
export const useJudgeVerdicts = () =>
  useQuery({ queryKey: ["judge", "verdicts"], queryFn: () => fetchJudgeVerdicts(500, 0) });
export const useCalendarToday = () => useQuery({ queryKey: ["calendar-today"], queryFn: api.life.calendarToday });
export const useGarminToday = () => useQuery({ queryKey: ["garmin-today"], queryFn: api.life.garminToday });
export const useGarminActivities = () =>
  useQuery({ queryKey: ["garmin-activities", 5], queryFn: () => api.life.garminActivities(5) });

/** The newest stored evening Summary (null before the first one is made); read again every minute so 22:00 shows by itself. */
export const useEveningSummary = () =>
  useQuery({ queryKey: ["evening-summary"], queryFn: api.life.eveningSummary, refetchInterval: 60_000 });

/** The cached Garmin days of the last n calendar days, oldest first. */
export const useGarminDays = (n: number) =>
  useQuery({ queryKey: ["garmin-days", n], queryFn: () => api.life.garminDays(n) });
/** The newest n cached Garmin activities. */
export const useGarminActivityList = (n: number) =>
  useQuery({ queryKey: ["garmin-activities", n], queryFn: () => api.life.garminActivities(n) });

export type Sport = "Running" | "Cycling" | "Swimming" | "Gym and other";
const RUN_TYPES = new Set(["running", "treadmill_running", "trail_running"]);
const RIDE_TYPES = new Set(["cycling", "indoor_cycling", "road_biking", "mountain_biking", "gravel_cycling", "virtual_ride"]);
const SWIM_TYPES = new Set(["lap_swimming", "open_water_swimming"]);

/** The sport group of an activity type: gym and other is every type that is not running, cycling or swimming. */
export function sportOf(typeKey: string | null): Sport {
  if (typeKey !== null) {
    if (RUN_TYPES.has(typeKey)) return "Running";
    if (RIDE_TYPES.has(typeKey)) return "Cycling";
    if (SWIM_TYPES.has(typeKey)) return "Swimming";
  }
  return "Gym and other";
}

export const LOADING = "Loading";
export const NOT_CONNECTED = "Garmin is not connected";
export const NO_VALUE = "Garmin gave no value for this";
/** The hover of a dash when no day in the cached window has the field. */
export const NO_DATA = "No data from the watch yet";

/** Why a call has no figure yet: its own words when it failed, "Loading" while it runs, else null. */
export function whyNot(q: Pick<UseQueryResult, "isError" | "error" | "isPending">): string | null {
  if (q.isError) return serverDetail(q.error);
  if (q.isPending) return LOADING;
  return null;
}

const two = (n: number) => String(n).padStart(2, "0");

/** Local "YYYY-MM-DDThh:mm", the same naive form the calendar store keeps. */
export function localMinute(d: Date): string {
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}T${two(d.getHours())}:${two(d.getMinutes())}`;
}

export const isTimed = (e: CalendarEvent) => e.all_day !== 1 && e.start_dt.includes("T");
export const hhmm = (s: string) => s.slice(11, 16);

/** Timed events that have not started yet, earliest first. All-day events are left out. */
export function upcoming(events: CalendarEvent[], now: Date): CalendarEvent[] {
  const at = localMinute(now);
  return events
    .filter((e) => isTimed(e) && e.start_dt.slice(0, 16) > at)
    .sort((a, b) => a.start_dt.localeCompare(b.start_dt));
}

/** Seconds of sleep as "7 h 12". */
export function sleepText(sec: number): string {
  const min = Math.round(sec / 60);
  return `${Math.floor(min / 60)} h ${two(min % 60)}`;
}

/** The activity with the latest start, as "<name>, <km to one decimal> km"; null when it lacks a name or a distance. */
export function lastActivity(list: GarminActivity[]): string | null {
  if (list.length === 0) return null;
  const a = list.reduce((best, x) => (x.start_time_local > best.start_time_local ? x : best));
  if (a.name == null || a.distance_m == null) return null;
  return `${a.name}, ${(a.distance_m / 1000).toFixed(1)} km`;
}

/** Each strategy's own newest verdict (current, not advisory, not rejected, as the Strategies page takes it); of those that passed, the newest. Null when none. */
export function newestPassed(rows: JudgeVerdictRow[]): JudgeVerdictRow | null {
  const newest = new Map<number, JudgeVerdictRow>();
  for (const r of rows) {
    if (!r.current || r.is_advisory || r.rejected) continue;
    const best = newest.get(r.strategy_id);
    if (!best || r.graded_at > best.graded_at || (r.graded_at === best.graded_at && r.id > best.id)) newest.set(r.strategy_id, r);
  }
  const passed = [...newest.values()].filter((r) => r.verdict === "pass");
  passed.sort((a, b) => b.graded_at - a.graded_at || b.id - a.id);
  return passed[0] ?? null;
}

const WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
/** The three-letter weekday of a YYYY-MM-DD date, in capitals. */
export const weekdayOf = (iso: string): string => {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
};
