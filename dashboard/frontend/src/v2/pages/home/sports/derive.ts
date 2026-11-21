import type { GarminActivity, GarminSnapshot } from "../../../../types";
import { NO_VALUE, sleepText } from "../data";

// Pure helpers for the Sports page: dates, the week window, and the text of a number.
// Every figure here is a sum, a mean or a window over Garmin's own fields; nothing is estimated.

export const BAD_VALUE = "Garmin's value could not be read";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const two = (n: number) => String(n).padStart(2, "0");

/** "2026-10-11" as "11 Oct". */
export const dayMonth = (iso: string) => `${Number(iso.slice(8, 10))} ${MONTHS[Number(iso.slice(5, 7)) - 1]}`;

/** The local calendar date of a Date as YYYY-MM-DD. */
export const localIso = (d: Date) => `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;

const utc = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
};
export const addDays = (iso: string, n: number) => new Date(utc(iso) + n * 86400000).toISOString().slice(0, 10);
export const daysBetween = (from: string, to: string) => Math.round((utc(to) - utc(from)) / 86400000);

/** The Monday on or before a date. */
export const mondayOf = (iso: string) => addDays(iso, -((new Date(utc(iso)).getUTCDay() + 6) % 7));

/** The last n dates up to and including `today`, oldest first. */
export const lastDates = (today: string, n: number) => Array.from({ length: n }, (_, i) => addDays(today, i - (n - 1)));

export const actDay = (a: GarminActivity) => a.start_time_local.slice(0, 10);
export const sum = (v: number[]) => v.reduce((a, b) => a + b, 0);
export const mean = (v: number[]) => sum(v) / v.length;
/** The mean of the days that have a reading, or null when fewer than `need` of the `days` do; n is how many did. */
export function averageOf(values: number[], days: number, need: number): { n: number; days: number; mean: number | null } {
  return { n: values.length, days, mean: values.length >= need ? mean(values) : null };
}
export const nonNull = (v: Array<number | null | undefined>): number[] => v.filter((x): x is number => x != null);

export const kmText = (m: number) => (m / 1000).toFixed(1);

/** Seconds as "6 h 00". */
export const hm = sleepText;

/** Seconds as "N min" under an hour, else "h mm". */
export function minOrHm(sec: number): string {
  const min = Math.round(sec / 60);
  return min >= 60 ? `${Math.floor(min / 60)} h ${two(min % 60)}` : `${min} min`;
}

/** Seconds as m:ss under an hour, h:mm:ss from an hour. */
export function clock(sec: number): string {
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}:${two(m)}:${two(s % 60)}` : `${m}:${two(s % 60)}`;
}

/** A pace in minutes per km as m:ss. */
export function pace(minPerKm: number): string {
  const s = Math.round(minPerKm * 60);
  return `${Math.floor(s / 60)}:${two(s % 60)}`;
}

/** "2026-10-12T23:40" as "23:40". */
export const hhmm = (local: string) => local.slice(11, 16);

const minutesOf = (text: string) => Number(text.slice(0, 2)) * 60 + Number(text.slice(3, 5));
/** Minutes of a bedtime, counted from noon so that 00:30 is 24:30 and the mean of 23:30 and 00:30 is 24:00. */
export const bedMinutes = (local: string) => {
  const m = minutesOf(hhmm(local));
  return m < 720 ? m + 1440 : m;
};
export const wakeMinutes = (local: string) => minutesOf(hhmm(local));
export function clockOfMinutes(min: number): string {
  const m = Math.round(min) % 1440;
  return `${two(Math.floor(m / 60))}:${two(m % 60)}`;
}

/** The people-facing form of a Garmin class or word: "WellTrained" and "WELL_TRAINED" both give "WELL TRAINED". */
export const plainWord = (w: string) => w.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/_/g, " ").toUpperCase();

/** "strength_training" as "Strength training". */
export function plainType(key: string): string {
  const t = key.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
export const activityName = (a: GarminActivity) => a.name ?? (a.type_key ? plainType(a.type_key) : "Activity");

export type Parsed<T> = { v: T | null; why: string | null };

/** Reads one stored JSON column. A missing column is "no value"; text that is not the expected shape says so. */
export function readJson<T>(text: string | null | undefined, ok: (x: unknown) => x is T): Parsed<T> {
  if (text == null) return { v: null, why: NO_VALUE };
  let x: unknown;
  try {
    x = JSON.parse(text);
  } catch (e) {
    if (e instanceof SyntaxError) return { v: null, why: BAD_VALUE };
    throw e;
  }
  return ok(x) ? { v: x, why: null } : { v: null, why: BAD_VALUE };
}

const isNum = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);
const isObj = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null;

export interface LoadFocus { name: string; value: number; target_min: number; target_max: number }
export const isFocusList = (x: unknown): x is LoadFocus[] =>
  Array.isArray(x) && x.every((f) => isObj(f) && typeof f.name === "string" && isNum(f.value) && isNum(f.target_min) && isNum(f.target_max));

export interface Factor { name: string; percent: number; feedback: string | null }
export const isFactorList = (x: unknown): x is Factor[] =>
  Array.isArray(x) && x.every((f) => isObj(f) && typeof f.name === "string" && isNum(f.percent));

export interface EnduranceLimits { gauge_lower: number | null; gauge_upper: number | null; classes: Array<{ name: string; lower: number }> }
export const isLimits = (x: unknown): x is EnduranceLimits =>
  isObj(x) && Array.isArray(x.classes) && x.classes.every((c) => isObj(c) && typeof c.name === "string" && isNum(c.lower));

export const isSeries = (x: unknown): x is Array<[number, number]> =>
  Array.isArray(x) && x.every((p) => Array.isArray(p) && p.length >= 2 && isNum(p[0]) && isNum(p[1]));

export const byDate = (rows: GarminSnapshot[]) => new Map(rows.map((r) => [r.date, r]));

/** The newest row (rows are oldest first) whose field `pick` gives is not null, or null when no row has it. */
export function newestWith(rows: GarminSnapshot[], pick: (r: GarminSnapshot) => unknown): GarminSnapshot | null {
  for (let i = rows.length - 1; i >= 0; i--) if (pick(rows[i]) != null) return rows[i];
  return null;
}

/**
 * True when the watch read the night: a sleep duration, and some deep or REM sleep. A night with a duration but no deep and no REM
 * is a night the watch did not read (planner ruling in DECISIONS #484), so it is left out of every average, spark and need line.
 */
export const readNight = (r: GarminSnapshot): boolean =>
  r.sleep_duration_sec != null && (r.sleep_deep_sec ?? 0) + (r.sleep_rem_sec ?? 0) > 0;

/** A row that holds sleep the watch did not read. */
export const unreadNight = (r: GarminSnapshot): boolean => r.sleep_duration_sec != null && !readNight(r);

/** "" for a row of today, else "as of 14 Oct". */
export const asOfLabel = (date: string, today: string) => (date === today ? "" : `as of ${dayMonth(date)}`);

const PARIS = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Paris", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

/** A stored UTC time ("2026-10-14T23:00:00", no zone mark) as "15 Oct 01:00" in Paris time. */
export function parisClock(utcIso: string): string {
  const t = new Date(/Z$|[+-]\d\d:\d\d$/.test(utcIso) ? utcIso : `${utcIso}Z`);
  if (Number.isNaN(t.getTime())) throw new Error(`parisClock: "${utcIso}" is not a time`);
  const p = Object.fromEntries(PARIS.formatToParts(t).map((x) => [x.type, x.value]));
  return `${Number(p.day)} ${MONTHS[Number(p.month) - 1]} ${p.hour}:${p.minute}`;
}

const WORDS: Record<string, string> = {
  soccer: "Football", strength_training: "Strength", lap_swimming: "Pool swim", open_water_swimming: "Open water swim", running: "Running",
  treadmill_running: "Treadmill run", cycling: "Cycling", walking: "Walking", paddelball: "Padel", table_tennis: "Table tennis",
  stair_climbing: "Stair climbing", multi_sport: "Multisport", meditation: "Meditation", other: "Other",
};
/** The short word for an activity type; a type not in the list is its key with spaces and a capital. */
export const activityWord = (typeKey: string) => WORDS[typeKey] ?? plainType(typeKey);

/** Activity types that have a pace (Garmin's keys); every other type is left without a Pace row. */
export const PACE_TYPES = ["running", "treadmill_running", "walking", "hiking"];

/** The population standard deviation (divide by n). */
export function stdev(v: number[]): number {
  const m = mean(v);
  return Math.sqrt(mean(v.map((x) => (x - m) ** 2)));
}
