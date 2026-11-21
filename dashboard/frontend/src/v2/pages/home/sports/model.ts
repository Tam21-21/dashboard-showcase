import type { GarminActivity, GarminSnapshot } from "../../../../types";
import { sportOf } from "../data";
import type { Sport } from "../data";
import { actDay, addDays, daysBetween, mondayOf, readNight } from "./derive";

// The numbers the Sports page works out itself, from the watch's own rows and sessions (mockup: lab-p-sports.js, model()).
// It never fills a gap: a number with nothing behind it is null.
//   load of a session   Banister's training impulse: minutes x r x 0.64 x e^(1.92 x r), r = (average - resting) / (max - resting) heart rate
//   fitness, fatigue    a slow and a fast average of daily load (42 and 7 days); form = fitness - fatigue
//   normal HRV range    the average of up to 60 nights, plus and minus half their standard deviation, against a 7-night average
//   doing enough        Garmin's own active minutes (a hard minute counts twice) against the watch's weekly goal

export const SPORTS: Sport[] = ["Running", "Cycling", "Swimming", "Gym and other"];
export const WEEKS = 26;

const have = (a: Array<number | null | undefined>): number[] => a.filter((v): v is number => v != null);
export const avg = (a: number[]): number | null => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : null);
const sdev = (a: number[]): number | null => {
  if (a.length < 2) return null;
  const m = avg(a) as number;
  return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1));
};

export interface Sess { a: GarminActivity; day: string; sport: Sport; min: number; km: number; load: number | null }
export interface Line {
  d: string; row: GarminSnapshot | null; sess: Sess[]; min: number; load: number;
  fit: number | null; tir: number | null; form: number | null;
  sleep: number | null; need: number | null; hrv: number | null; rhr: number | null;
  hrv7: number | null; rhr7: number | null; band: [number, number, number] | null;
}
export interface Week { w: string; n: number; minAll: number; min: Record<Sport, number>; km: Record<Sport, number>; cnt: Record<Sport, number>; load: number; im: number | null; strength: number }

export interface Model {
  sess: Sess[]; line: Line[]; weeks: Week[]; thisMon: string; wk: Week; hasLoad: boolean; hrMax: number | null;
  lwSame: { min: number; n: number };
  fit: { fit: number; tir: number; form: number; pct: number | null; d28: number | null; zone: [string, string] } | null;
  rec: { row: Line; hrv: number; hrv7: number | null; band: [number, number, number] | null; rhr7: number | null; rhr28: number | null; bb: number | null; stress: number | null; side: -1 | 0 | 1 | null; streak: number; quiet: number } | null;
  slp: { row: Line; last: number; avg: number | null; n: number; need: number | null; owed: number | null; bed: string | null } | null;
  /** Calories burned a day: means over the last 7 finished days that hold both parts. */
  burn: { avg: number; move: number; rest: number; prev: number | null; yest: number; today: number | null; days: number[] } | null;
  goal: number | null; vo2max: number | null; strengthGoal: number;
  /** Days at the end of the window the watch sent nothing for. */
  missing: number;
}

const bedTime = (r: GarminSnapshot): number | null => {
  if (!r.sleep_start_local) return null;
  const m = Number(r.sleep_start_local.slice(11, 13)) * 60 + Number(r.sleep_start_local.slice(14, 16));
  return m < 12 * 60 ? m + 1440 : m;
};
const clockOf = (m: number) => { const x = ((Math.round(m) % 1440) + 1440) % 1440; return `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(x % 60).padStart(2, "0")}`; };

export function buildModel(rows: GarminSnapshot[], acts: GarminActivity[], today: string): Model {
  const byD = new Map(rows.map((r) => [r.date.slice(0, 10), r]));
  const hrMaxVals = have(acts.map((a) => a.max_hr));
  const hrMax = hrMaxVals.length ? Math.max(...hrMaxVals) : null;
  const restAvg = avg(have(rows.map((r) => r.resting_hr)));
  const sess: Sess[] = acts.map((a) => {
    const day = actDay(a);
    const rest = byD.get(day)?.resting_hr ?? restAvg;
    const min = (a.duration_sec ?? 0) / 60;
    const r = a.avg_hr != null && hrMax != null && rest != null && hrMax > rest ? Math.max(0, Math.min(1, (a.avg_hr - rest) / (hrMax - rest))) : null;
    return { a, day, sport: sportOf(a.type_key), min, km: (a.distance_m ?? 0) / 1000, load: r == null ? null : min * r * 0.64 * Math.exp(1.92 * r) };
  }).sort((x, y) => x.a.start_time_local.localeCompare(y.a.start_time_local));
  const hasLoad = sess.some((s) => s.load != null);

  // The rows stop at the last day the watch sent anything: a day it has not synced is unknown, not a rest day.
  const sentRows = rows.filter((r) => r.sleep_duration_sec != null || r.hrv_overnight != null || r.resting_hr != null);
  const lastRow = sentRows.length ? sentRows[sentRows.length - 1].date.slice(0, 10) : null;
  const lastAct = sess.length ? sess[sess.length - 1].day : null;
  const end = [lastRow, lastAct].filter((x): x is string => x != null).sort().pop() ?? today;
  const first = [rows[0]?.date.slice(0, 10), sess[0]?.day, end].filter((x): x is string => x != null).sort()[0];
  const line: Line[] = [];
  for (let d = first; d <= end; d = addDays(d, 1)) {
    const row = byD.get(d) ?? null;
    line.push({ d, row, sess: [], min: 0, load: 0, fit: null, tir: null, form: null,
      sleep: row && readNight(row) ? row.sleep_duration_sec : null, need: row?.sleep_need_min != null ? row.sleep_need_min * 60 : null,
      hrv: row?.hrv_overnight ?? null, rhr: row?.resting_hr ?? null, hrv7: null, rhr7: null, band: null });
  }
  const at = new Map(line.map((r, i) => [r.d, i]));
  for (const s of sess) {
    const r = line[at.get(s.day) ?? -1];
    if (!r) continue;
    r.sess.push(s); r.min += s.min; if (s.load != null) r.load += s.load;
  }
  if (hasLoad) {
    const seed = avg(line.slice(0, 42).map((r) => r.load)) as number;
    let f = seed, t = seed;
    for (const r of line) { f += (r.load - f) / 42; t += (r.load - t) / 7; r.fit = f; r.tir = t; r.form = f - t; }
  }
  const avg7 = (i: number, k: "hrv" | "rhr") => { const v = have(line.slice(Math.max(0, i - 6), i + 1).map((r) => r[k])); return v.length >= 4 ? avg(v) : null; };
  line.forEach((r, i) => {
    r.hrv7 = avg7(i, "hrv"); r.rhr7 = avg7(i, "rhr");
    const v = have(line.slice(Math.max(0, i - 59), i + 1).map((q) => q.hrv));
    const s = sdev(v);
    r.band = v.length >= 14 && s != null ? [(avg(v) as number) - s / 2, (avg(v) as number) + s / 2, v.length] : null;
  });
  const lastOf = (k: "hrv" | "sleep"): Line | null => { for (let i = line.length - 1; i >= 0; i--) if (line[i][k] != null) return line[i]; return null; };
  const last7 = (r: Line | null, k: "sleep"): Line[] => { if (!r) return []; const i = at.get(r.d) as number; return line.slice(Math.max(0, i - 6), i + 1).filter((q) => q[k] != null); };

  // Weeks, Monday to Sunday, the newest 26.
  const thisMon = mondayOf(today);
  const weeks: Week[] = [];
  for (let k = WEEKS - 1; k >= 0; k--) {
    const w = addDays(thisMon, -7 * k), end7 = addDays(w, 6);
    const wk: Week = { w, n: 0, minAll: 0, min: { Running: 0, Cycling: 0, Swimming: 0, "Gym and other": 0 }, km: { Running: 0, Cycling: 0, Swimming: 0, "Gym and other": 0 }, cnt: { Running: 0, Cycling: 0, Swimming: 0, "Gym and other": 0 }, load: 0, im: null, strength: 0 };
    const strengthDays = new Set<string>();
    for (const s of sess) {
      if (s.day < w || s.day > end7) continue;
      wk.n++; wk.minAll += s.min; wk.min[s.sport] += s.min; wk.cnt[s.sport]++; wk.km[s.sport] += s.km;
      if (s.load != null) wk.load += s.load;
      if (/strength/i.test(s.a.type_key ?? "")) strengthDays.add(s.day);
    }
    wk.strength = strengthDays.size;
    const ims = rows.filter((r) => r.date.slice(0, 10) >= w && r.date.slice(0, 10) <= end7 && (r.intensity_moderate_min != null || r.intensity_vigorous_min != null));
    wk.im = ims.length ? ims.reduce((t, r) => t + (r.intensity_moderate_min ?? 0) + 2 * (r.intensity_vigorous_min ?? 0), 0) : null;
    weeks.push(wk);
  }
  const wk = weeks[WEEKS - 1];
  const lwEnd = addDays(today, -7);
  const lwSess = sess.filter((s) => s.day >= addDays(thisMon, -7) && s.day <= lwEnd);
  const lwSame = { min: lwSess.reduce((t, s) => t + s.min, 0), n: lwSess.length };

  const T = line[line.length - 1];
  let fit: Model["fit"] = null;
  if (hasLoad && T && T.fit != null && T.tir != null && T.form != null) {
    const pct = T.fit ? (T.form / T.fit) * 100 : null;
    const b28 = line.length > 28 ? line[line.length - 29].fit : null, b7 = line.length > 7 ? line[line.length - 8].fit : null;
    void b7;
    fit = { fit: T.fit, tir: T.tir, form: T.form, pct, d28: b28 == null ? null : T.fit - b28,
      zone: pct == null ? ["dim", "No reading"] : pct < -30 ? ["bad", "Too much"] : pct < -10 ? ["good", "Building"] : pct <= 5 ? ["dim", "Holding"] : ["run", "Fresh"] };
  }
  const hr = lastOf("hrv");
  let rec: Model["rec"] = null;
  if (hr && hr.hrv != null) {
    const i = at.get(hr.d) as number;
    const side: -1 | 0 | 1 | null = hr.band && hr.hrv7 != null ? (hr.hrv7 < hr.band[0] ? -1 : hr.hrv7 > hr.band[1] ? 1 : 0) : null;
    let streak = 0;
    if (side) for (let j = i; j >= 0; j--) { const r = line[j]; if (r.band && r.hrv7 != null && (side < 0 ? r.hrv7 < r.band[0] : r.hrv7 > r.band[1])) streak++; else break; }
    rec = { row: hr, hrv: hr.hrv, hrv7: hr.hrv7, band: hr.band, rhr7: hr.rhr7, rhr28: avg(have(line.slice(Math.max(0, i - 27), i + 1).map((r) => r.rhr))),
      bb: hr.row?.body_battery_at_wake ?? hr.row?.body_battery_high ?? null, stress: hr.row?.stress ?? null, side, streak,
      quiet: daysBetween(hr.d, today) };
  }
  const sr = lastOf("sleep");
  const s7 = last7(sr, "sleep");
  const slp: Model["slp"] = sr && sr.sleep != null ? {
    row: sr, last: sr.sleep, avg: s7.length >= 4 ? avg(s7.map((r) => r.sleep as number)) : null, n: s7.length, need: avg(have(s7.map((r) => r.need))),
    owed: s7.some((r) => r.need != null) ? s7.reduce((t, r) => t + (r.need != null ? Math.max(0, r.need - (r.sleep as number)) : 0), 0) : null,
    bed: (() => { const b = avg(have(s7.map((r) => (r.row ? bedTime(r.row) : null)))); return b == null ? null : clockOf(b); })(),
  } : null;
  const bothParts = (r: GarminSnapshot | null) => (r != null && r.resting_calories != null && r.active_calories != null ? { rest: r.resting_calories, move: r.active_calories } : null);
  const full = line.flatMap((l) => { const p = l.d < today ? bothParts(l.row) : null; return p ? [p] : []; });
  const b7 = full.slice(-7), prev7 = full.slice(-14, -7);
  const sum = (p: { rest: number; move: number }) => p.rest + p.move;
  const todayParts = bothParts(line.find((l) => l.d === today)?.row ?? null);
  const burn: Model["burn"] = b7.length ? {
    avg: avg(b7.map(sum)) as number, move: avg(b7.map((p) => p.move)) as number, rest: avg(b7.map((p) => p.rest)) as number,
    prev: prev7.length === 7 ? (avg(prev7.map(sum)) as number) : null, yest: sum(b7[b7.length - 1]),
    today: todayParts ? sum(todayParts) : null, days: full.slice(-14).map(sum),
  } : null;
  const newest = [...rows].reverse();
  const goal = newest.find((r) => r.intensity_goal != null)?.intensity_goal ?? null;
  const vo2max = newest.find((r) => r.vo2max != null)?.vo2max ?? null;
  return { sess, line, weeks, thisMon, wk, hasLoad, hrMax, lwSame, fit, rec, slp, burn, goal, vo2max, strengthGoal: 2, missing: Math.max(0, daysBetween(end, today)) };
}
