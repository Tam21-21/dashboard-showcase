import { useState } from "react";
import type { ReactNode } from "react";
import { DayBars, Spark } from "../../../kit";
import { useSettings } from "../../../settings";
import { useLayout } from "../../../kit/phone";
import type { GarminActivity, GarminSnapshot } from "../../../../types";
import { NO_DATA, NO_VALUE, sleepText, sportOf } from "../data";
import type { Sport } from "../data";
import { StateWord } from "../../../kit";
import { AsOf } from "./panels";
import {
  actDay, addDays, asOfLabel, averageOf, byDate, daysBetween, dayMonth, hhmm, isSeries, kmText, lastDates, mean, mondayOf, newestWith, nonNull, readJson, readNight, sum, unreadNight,
} from "./derive";

// The moving row of 13 tiles (mockup: home/SPORTS, tiles). Every value is a Garmin field, a sum, a mean or the week window.

export const SPORT_COLOUR: Record<Sport, string> = {
  Running: "#29c6ff",
  Cycling: "#4d9bff",
  Swimming: "#a78bfa",
  "Gym and other": "#ff9a3d",
};
const NO_DAY = "No Garmin day synced yet";
const NO_ACTIVITY = "No activity synced yet";

const Dash = ({ why }: { why: string }) => <span title={why}>-</span>;
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const bpm = (n: number) => `${n} bpm`;
const group = (n: number) => n.toLocaleString("en-US");

export interface TileInput {
  today: string;
  rows: GarminSnapshot[] | null;
  rowsWhy: string | null;
  acts: GarminActivity[] | null;
  actsWhy: string | null;
}
interface Tile {
  name: string;
  hover: string;
  value: ReactNode;
  sub?: ReactNode;
  foot?: ReactNode;
  spark?: ReactNode;
  /** "as of 14 Oct" when the main value comes from an earlier day. */
  asOf?: ReactNode;
}

/** Reads one field at a time from the newest day that has it. */
interface Fields {
  /** The field as text; a dash with the reason when no day has it; a hover with the day when it is not today's. */
  val: <T>(pick: (r: GarminSnapshot) => T | null | undefined, f: (v: T) => string) => ReactNode;
  /** The raw value, or null when no day has it. */
  raw: <T>(pick: (r: GarminSnapshot) => T | null | undefined) => T | null;
  /** The "as of" label of the day the field was read from. */
  asOf: (pick: (r: GarminSnapshot) => unknown) => ReactNode;
}

interface Tally { n: number; sec: number; m: number }
function tally(acts: GarminActivity[], from: string, to: string, sport?: Sport): Tally {
  const hit = acts.filter((a) => actDay(a) >= from && actDay(a) <= to && (sport === undefined || sportOf(a.type_key) === sport));
  return { n: hit.length, sec: sum(hit.map((a) => a.duration_sec ?? 0)), m: sum(hit.map((a) => a.distance_m ?? 0)) };
}
const dailySum = (acts: GarminActivity[], dates: string[], pick: (a: GarminActivity) => number, sport?: Sport) =>
  dates.map((d) => sum(acts.filter((a) => actDay(a) === d && (sport === undefined || sportOf(a.type_key) === sport)).map(pick)));

export function buildTiles({ today, rows, rowsWhy, acts, actsWhy }: TileInput): Tile[] {
  const wk0 = mondayOf(today);
  const nDays = daysBetween(wk0, today) + 1;
  const lw0 = addDays(wk0, -7);
  const lwEnd = addDays(lw0, nDays - 1);
  const week = lastDates(today, nDays);
  const month = lastDates(today, 28);

  const actDash = actsWhy ?? (acts !== null && acts.length === 0 ? NO_ACTIVITY : null);
  const list = acts ?? [];
  const sportTile = (name: string, hover: string, sport: Sport | undefined, kind: "time" | "km", noun: [string, string], daily: number[], colour: string): Tile => {
    if (actDash !== null) return { name, hover, value: <Dash why={actDash} /> };
    const t = tally(list, wk0, today, sport);
    const l = tally(list, lw0, lwEnd, sport);
    const spark = <DayBars size="tile" bars={daily.map((value) => ({ parts: [{ value, colour }] }))} opacity={0.85} />;
    if (kind === "km") {
      return {
        name, hover, spark,
        value: `${kmText(t.m)} km`,
        sub: `${sleepText(t.sec)}, ${plural(t.n, noun[0], noun[1])}`,
        foot: `last week ${kmText(l.m)} km`,
      };
    }
    return {
      name, hover, spark,
      value: sleepText(t.sec),
      sub: plural(t.n, noun[0], noun[1]),
      foot: sport === undefined ? `same days last week ${sleepText(l.sec)}` : `last week ${plural(l.n, noun[0], noun[1])}`,
    };
  };

  const out: Tile[] = [
    sportTile("ALL SPORT, THIS WEEK", "Monday to today, every activity Garmin recorded", undefined, "time", ["workout", "workouts"],
      dailySum(list, week, (a) => (a.duration_sec ?? 0) / 60), "#eef2f7"),
    sportTile("RUNNING", "Running, treadmill running and trail running, Monday to today", "Running", "km", ["run", "runs"],
      dailySum(list, week, (a) => (a.distance_m ?? 0) / 1000, "Running"), SPORT_COLOUR.Running),
    sportTile("CYCLING", "Every kind of cycling and virtual ride (indoor rides count time only)", "Cycling", "km", ["ride", "rides"],
      dailySum(list, month, (a) => (a.distance_m ?? 0) / 1000, "Cycling"), SPORT_COLOUR.Cycling),
    sportTile("SWIMMING", "Pool and open water swimming", "Swimming", "km", ["swim", "swims"],
      dailySum(list, month, (a) => (a.distance_m ?? 0) / 1000, "Swimming"), SPORT_COLOUR.Swimming),
    sportTile("GYM AND OTHER", "Every activity that is not running, cycling or swimming: time and count, no distance", "Gym and other", "time", ["session", "sessions"],
      dailySum(list, week, (a) => (a.duration_sec ?? 0) / 60, "Gym and other"), SPORT_COLOUR["Gym and other"]),
  ];

  // The day tiles read each field from the newest Garmin day that has it.
  const dayWhy = rowsWhy ?? (rows !== null && rows.length === 0 ? NO_DAY : null);
  const all = rows ?? [];
  const dated = byDate(all);
  const series = (pick: (r: GarminSnapshot) => number | null | undefined, dates: string[]) =>
    nonNull(dates.map((d) => { const r = dated.get(d); return r ? pick(r) : null; }));
  const fields: Fields = {
    val: (pick, f) => {
      const r = newestWith(all, pick);
      if (r === null) return <Dash why={NO_DATA} />;
      const text = f(pick(r) as never);
      return r.date === today ? text : <span title={asOfLabel(r.date, today)}>{text}</span>;
    },
    raw: (pick) => {
      const r = newestWith(all, pick);
      return r === null ? null : (pick(r) as never);
    },
    asOf: (pick) => {
      const r = newestWith(all, pick);
      return r === null ? null : <AsOf date={r.date} today={today} />;
    },
  };
  const dayTile = (name: string, hover: string, build: (g: Fields) => Omit<Tile, "name" | "hover">): Tile =>
    dayWhy !== null ? { name, hover, value: <Dash why={dayWhy} /> } : { name, hover, ...build(fields) };

  const last28 = (pick: (r: GarminSnapshot) => number | null | undefined) => series(pick, month);
  const spark = (values: number[], colour: string) => (values.length > 0 ? <Spark values={values} colour={colour} fill={0.14} /> : undefined);

  out.push(
    dayTile("SLEEP, LAST NIGHT", "Garmin's sleep score and its word, all from the newest night the watch read", () => {
      // One row, so duration, score, word, bed and wake always belong to the same night.
      const night = newestWith(all, (r) => (readNight(r) ? 1 : null));
      const newer = newestWith(all, (r) => (unreadNight(r) ? 1 : null));
      const notRead = newer !== null && (night === null || newer.date > night.date) ? newer : null;
      const part = <T,>(v: T | null | undefined, f: (x: T) => string) => (v == null ? <Dash why={NO_VALUE} /> : f(v));
      return {
        value: night === null ? <Dash why={NO_DATA} /> : sleepText(night.sleep_duration_sec as number),
        sub: night === null ? undefined : <>score {part(night.sleep_score, String)} {part(night.sleep_score_word, String)}</>,
        foot: (
          <>
            {notRead !== null && (
              <span title={`The watch did not read the night of ${dayMonth(notRead.date)}: ${sleepText(notRead.sleep_duration_sec as number)} asleep, no deep or REM sleep`}>
                <StateWord word="LAST NIGHT NOT READ" tone="a" />{" "}
              </span>
            )}
            {night !== null && <>bed {part(night.sleep_start_local, hhmm)}, up {part(night.sleep_end_local, hhmm)}</>}
          </>
        ),
        spark: spark(last28((r) => (readNight(r) ? r.sleep_duration_sec : null)), "#9d7bff"),
        asOf: night === null ? null : <AsOf date={night.date} today={today} />,
      };
    }),
    dayTile("HRV, LAST NIGHT", "Overnight HRV average; the range and the word are Garmin's own baseline", (g) => ({
      value: g.val((r) => r.hrv_overnight, (v) => `${v} ms`),
      sub: g.val((r) => r.hrv_status, String),
      foot: <>Garmin's balanced range {g.val((r) => r.hrv_balanced_low, String)} to {g.val((r) => r.hrv_balanced_high, String)}</>,
      spark: spark(last28((r) => r.hrv_overnight), "#7fd9a8"),
      asOf: g.asOf((r) => r.hrv_overnight),
    })),
    dayTile("TRAINING READINESS", "Garmin's morning readiness score and level", (g) => ({
      value: g.val((r) => r.readiness, String),
      sub: g.val((r) => r.readiness_level, String),
      foot: <>recovery {g.val((r) => r.recovery_minutes, (m) => sleepText(m * 60))} left</>,
      spark: spark(last28((r) => r.readiness), "#e8d49a"),
      asOf: g.asOf((r) => r.readiness),
    })),
    dayTile("BODY BATTERY", "Garmin's body battery, today", (g) => {
      const s = readJson(g.raw((r) => r.body_battery_series_json), isSeries).v;
      return {
        value: g.val((r) => r.body_battery_pct, String),
        sub: <>woke at {g.val((r) => r.body_battery_at_wake, String)}</>,
        foot: <>high {g.val((r) => r.body_battery_high, String)}, low {g.val((r) => r.body_battery_low, String)}</>,
        spark: s && s.length > 0 ? spark(s.map((p) => p[1]), "#29c6ff") : undefined,
        asOf: g.asOf((r) => r.body_battery_pct),
      };
    }),
    dayTile("RESTING HEART RATE", "Garmin's resting heart rate", (g) => {
      const w7 = series((r) => r.resting_hr, lastDates(today, 7));
      const avg7 = averageOf(w7, 7, 4);
      const ago = dated.get(addDays(today, -28))?.resting_hr;
      return {
        value: g.val((r) => r.resting_hr, bpm),
        sub: <>7-day average {avg7.n === 0 ? <Dash why={NO_DATA} /> : avg7.mean === null
          ? <Dash why={`Only ${avg7.n} of 7 days have a reading; the average needs 4`} />
          : <span title={`${avg7.n} of 7 days have a reading`}>{Math.round(avg7.mean)}</span>}</>,
        foot: <>4 weeks ago {ago == null ? <Dash why={NO_DATA} /> : ago}</>,
        spark: spark(last28((r) => r.resting_hr), "#ff8fd0"),
        asOf: g.asOf((r) => r.resting_hr),
      };
    }),
    dayTile("STEPS, TODAY", "The goal is Garmin's own daily step goal", (g) => {
      const w7 = series((r) => r.steps, lastDates(today, 7));
      return {
        value: g.val((r) => r.steps, group),
        sub: <>goal {g.val((r) => r.steps_goal, group)}</>,
        foot: <>week average {w7.length > 0 ? group(Math.round(mean(w7))) : <Dash why={NO_DATA} />}</>,
        spark: w7.length > 0 ? <DayBars size="tile" bars={w7.map((value) => ({ parts: [{ value, colour: "#e8d49a" }] }))} opacity={0.8} /> : undefined,
        asOf: g.asOf((r) => r.steps),
      };
    }),
    dayTile("INTENSITY MINUTES", "Garmin's intensity minutes, vigorous minutes counted twice; the weekly goal is Garmin's", (g) => {
      const minutes = (d: string) => {
        const r = dated.get(d);
        return r && (r.intensity_moderate_min != null || r.intensity_vigorous_min != null)
          ? (r.intensity_moderate_min ?? 0) + 2 * (r.intensity_vigorous_min ?? 0)
          : null;
      };
      const mine = nonNull(week.map(minutes));
      const lastWeek = nonNull(lastDates(addDays(lw0, 6), 7).map(minutes));
      const cum = mine.map((_, i) => sum(mine.slice(0, i + 1)));
      return {
        value: mine.length > 0 ? String(sum(mine)) : <Dash why={NO_DATA} />,
        sub: <>goal {g.val((r) => r.intensity_goal, String)} this week</>,
        foot: <>last week {lastWeek.length > 0 ? sum(lastWeek) : <Dash why={NO_DATA} />}</>,
        spark: spark(cum, "#ff8fd0"),
      };
    }),
    dayTile("VO2 MAX", "Garmin's VO2 max estimate from your runs", (g) => {
      const weekly = nonNull(Array.from({ length: 12 }, (_, i) => dated.get(addDays(today, -77 + 7 * i))?.vo2max_garmin));
      const before = dated.get(addDays(today, -77))?.vo2max_garmin;
      return {
        value: g.val((r) => r.vo2max_garmin, String),
        sub: "running",
        foot: <>11 weeks ago {before == null ? <Dash why={NO_DATA} /> : before}</>,
        spark: spark(weekly, "#7fd9a8"),
        asOf: g.asOf((r) => r.vo2max_garmin),
      };
    }),
  );
  return out;
}

function TileCard({ t, copy }: { t: Tile; copy: boolean }) {
  return (
    <div className="cd mtl stl" title={t.hover} aria-hidden={copy ? true : undefined}>
      <div className="mn">{t.name}</div>
      <div className="mv num">{t.value}</div>
      <div className="mch"><span>{t.sub}</span>{t.asOf}</div>
      <div className="msp2">{t.spark}</div>
      <div className="sft">{t.foot}</div>
    </div>
  );
}

/** The tiles in a row that moves slowly and stops under the pointer; settings can turn the motion off. */
export function TileRow({ tiles }: { tiles: Tile[] }) {
  const { settings } = useSettings();
  const [paused, setPaused] = useState(false);
  // The phone shows the tiles two across and still, with no second copy.
  const desktop = useLayout() === "desktop";
  const moving = settings.motion === "on" && desktop;
  return (
    <div className="mq" data-testid="tiles-row" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className={`mqt${moving ? " moving" : ""}${paused ? " paused" : ""}`} data-testid="tiles-track">
        {tiles.map((t) => <TileCard key={t.name} t={t} copy={false} />)}
        {moving && tiles.map((t) => <TileCard key={`copy-${t.name}`} t={t} copy />)}
      </div>
    </div>
  );
}
