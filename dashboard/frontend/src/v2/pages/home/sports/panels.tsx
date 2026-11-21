import { useState } from "react";
import type { ReactNode } from "react";
import { Gauge, Legend, Panel, Rows, Segmented, StateWord, Table, Track } from "../../../kit";
import type { Cell } from "../../../kit";
import type { GarminSnapshot } from "../../../../types";
import { NO_DATA, NO_VALUE, sleepText } from "../data";
import { LineSeriesChart, StackedBars } from "./charts";
import type { StackPart, Tip } from "./charts";
import {
  addDays, asOfLabel, averageOf, bedMinutes, byDate, clock, clockOfMinutes, dayMonth, hhmm, isFactorList, isFocusList, isLimits, isSeries, lastDates, mean,
  mondayOf, newestWith, nonNull, parisClock, plainWord, readJson, readNight, stdev, two, wakeMinutes,
} from "./derive";

// The cards built from Garmin's daily rows: training load, today, the nights, the sleep week, the endurance score and the race estimates.

const GREEN = "#2fe08a";
const AMBER = "#ffb020";
const RED = "#ff5a63";
const BLUE = "#4d9bff";
const STAGES: Array<[string, string]> = [["Deep", "#5b3fd6"], ["Light", "#9d7bff"], ["REM", "#c9b8ff"], ["Awake", "rgba(255,255,255,.25)"]];
/** The one grey bar of a night the watch did not read. */
const UNREAD = "#6b7480";
const CLASS_COLOURS = ["#5b6b80", "#4d9bff", "#29c6ff", "#2fe08a", "#e8d49a", "#ffb020", "#ff5a63"];

const Dash = ({ why }: { why: string }) => <span title={why}>-</span>;
const Empty = ({ why }: { why: string }) => <div className="empty"><Dash why={why} /></div>;
const hint = (title: string, text: string) => <b style={{ fontWeight: "inherit" }} title={title}>{text}</b>;
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const noValue = "no value";

interface DayProps { rows: GarminSnapshot[]; today: string }

/** "as of 14 Oct" (hover: the date) when a value comes from a day that is not today; nothing for today. */
export function AsOf({ date, today }: { date: string; today: string }) {
  const text = asOfLabel(date, today);
  return text === "" ? null : <span className="asof" title={date}>{text}</span>;
}

export function TrainingLoad({ rows, today }: DayProps) {
  const dated = byDate(rows);
  const statusRow = newestWith(rows, (r) => r.training_status);
  const focusRow = newestWith(rows, (r) => r.load_focus_json);
  const focus = focusRow === null ? { v: null, why: NO_DATA } : readJson(focusRow.load_focus_json, isFocusList);
  const dates = lastDates(today, 56);
  const values = dates.map((d) => dated.get(d)?.acute_load ?? null);
  return (
    <Panel
      title={hint("Garmin's training status and its load focus over the last 4 weeks, each against Garmin's own target range (the two marks)", "TRAINING LOAD")}
      note={
        statusRow === null ? <Dash why={NO_DATA} /> : (
          <>
            <StateWord word={plainWord(statusRow.training_status as string)} tone="none" />
            <AsOf date={statusRow.date} today={today} />
          </>
        )
      }
    >
      {focus.v === null || focusRow === null ? (
        <Empty why={focus.why ?? NO_DATA} />
      ) : (
        <>
        {focusRow.date !== today && <div style={{ textAlign: "right" }}><AsOf date={focusRow.date} today={today} /></div>}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10, margin: "4px 0 6px" }}>
          {focus.v.map((f, i) => {
            const mx = f.target_max * 1.5;
            return (
              <div key={f.name}>
                <Gauge
                  id={`lf${i}`} value={f.value} min={0} max={mx} sw={8} foot={20} top={16} side={10}
                  label={f.value < f.target_min ? "BELOW TARGET" : f.value > f.target_max ? "ABOVE TARGET" : "IN TARGET"}
                  bands={[[0, f.target_min, BLUE], [f.target_min, f.target_max, GREEN], [f.target_max, mx, AMBER]]}
                  marks={[f.target_min, f.target_max]}
                  title={`${f.name}: ${f.value} over 4 weeks; Garmin's target ${f.target_min} to ${f.target_max}`}
                />
                <div style={{ textAlign: "center", fontSize: 12.5, marginTop: 2 }}>{f.name}</div>
              </div>
            );
          })}
        </div>
        </>
      )}
      <div className="kl" style={{ marginTop: 12 }} title="Garmin's acute load, from the morning readiness of each day">ACUTE LOAD, 8 WEEKS</div>
      {nonNull(values).length === 0 ? (
        <Empty why={NO_DATA} />
      ) : (
        <LineSeriesChart
          testid="acute-chart" values={values} colour="#ff9a3d" yfmt={(v) => String(Math.round(v))}
          xlabels={dates.map((d, i): [number, string] => [i, dayMonth(d)]).filter(([i]) => (dates.length - 1 - i) % 14 === 0)}
          tip={(i): Tip => {
            const v = values[i];
            const text = v === null ? noValue : String(v);
            return { label: `${dayMonth(dates[i])}: acute load ${text}`, head: dayMonth(dates[i]), rows: [["Acute load", text]] };
          }}
        />
      )}
    </Panel>
  );
}

const factorColour = (pct: number) => (pct >= 70 ? GREEN : pct >= 40 ? AMBER : RED);

export function Today({ rows, today }: DayProps) {
  const t = byDate(rows).get(today);
  if (!t || t.body_battery_series_json == null) {
    // The watch has sent nothing for today: say when it last did, by the newest day with steps or a night of sleep.
    const last = [...rows].reverse().find((r) => (r.steps ?? 0) > 0 || r.sleep_duration_sec != null);
    return (
      <Panel title="TODAY">
        <div className="empty">{last ? `Nothing from the watch since ${parisClock(last.fetched_at)}` : NO_DATA}</div>
      </Panel>
    );
  }
  const factorsRow = newestWith(rows, (r) => r.readiness_factors_json);
  const series = readJson(t.body_battery_series_json, isSeries);
  const factors = factorsRow === null ? { v: null, why: NO_DATA } : readJson(factorsRow.readiness_factors_json, isFactorList);
  const clockOf = (ts: number) => {
    const d = new Date(ts);
    return `${two(d.getHours())}:${two(d.getMinutes())}`;
  };
  const pts = series.v ?? [];
  return (
    <Panel title="TODAY">
      <div className="kl">BODY BATTERY SINCE MIDNIGHT</div>
      <div style={{ margin: "6px 0 14px" }}>
        {pts.length === 0 ? (
          <Empty why={series.why ?? NO_VALUE} />
        ) : (
          <LineSeriesChart
            testid="battery-chart" values={pts.map((p) => p[1])} colour="#29c6ff" fill={0.16} minHeight={90} pad={[8, 40, 20, 6]}
            yfmt={String}
            xlabels={[[0, clockOf(pts[0][0])], [pts.length - 1, clockOf(pts[pts.length - 1][0])]]}
            tip={(i): Tip => ({
              label: `${clockOf(pts[i][0])}: body battery ${pts[i][1]}`,
              head: clockOf(pts[i][0]),
              rows: [["Body battery", String(pts[i][1])]],
            })}
          />
        )}
      </div>
      <div className="kl">WHAT READINESS {factorsRow?.readiness ?? ""} IS MADE OF{factorsRow && <AsOf date={factorsRow.date} today={today} />}</div>
      {factors.v === null ? (
        <Empty why={factors.why ?? NO_DATA} />
      ) : (
        <table className="cot">
          <tbody>
            {factors.v.map((f) => (
              <tr key={f.name} data-factor={f.name} title={f.feedback ? plainWord(f.feedback).toLowerCase() : undefined}>
                <td>{cap(f.name)}</td>
                <td style={{ width: "38%" }}><Track fraction={f.percent / 100} colour={factorColour(f.percent)} /></td>
                <td className="num" style={{ textAlign: "right" }}>{f.percent}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

type NightsMode = "SLEEP" | "RHR" | "HRV";
const durText = (sec: number) => (sec < 3600 ? `${Math.round(sec / 60)} min` : sleepText(sec));

export function Nights({ rows, today }: DayProps) {
  const [mode, setMode] = useState<NightsMode>("SLEEP");
  const dates = lastDates(today, 28);
  const dated = byDate(rows);
  const balance = newestWith(rows, (r) => (r.hrv_balanced_low != null && r.hrv_balanced_high != null ? 1 : null));
  const at = (i: number) => dated.get(dates[i]);
  const needs = nonNull(dates.map((_, i) => { const r = at(i); return r && readNight(r) ? r.sleep_need_min : null; }));
  const xlabels = dates.map((d, i): [number, string] => [i, dayMonth(d)]).filter(([i]) => i % 7 === 0);
  const dm = (i: number) => dayMonth(dates[i]);
  const txt = (v: number | null | undefined, f: (v: number) => string) => (v == null ? noValue : f(v));

  let body: ReactNode;
  if (mode === "SLEEP") {
    const stacks: Array<StackPart[] | null> = dates.map((_, i) => {
      const r = at(i);
      if (!r || r.sleep_duration_sec == null) return null;
      const hrs = (s: number | null) => (s ?? 0) / 3600;
      if (!readNight(r)) return [{ value: hrs(r.sleep_duration_sec), colour: UNREAD }];
      return [
        { value: hrs(r.sleep_deep_sec), colour: STAGES[0][1] }, { value: hrs(r.sleep_light_sec), colour: STAGES[1][1] },
        { value: hrs(r.sleep_rem_sec), colour: STAGES[2][1] }, { value: hrs(r.sleep_awake_sec), colour: STAGES[3][1] },
      ];
    });
    body = stacks.every((s) => s === null) ? (
      <Empty why="No night recorded in the last 28 days" />
    ) : (
      <>
        <Legend items={STAGES} />
        <StackedBars
          testid="nights-chart" attr="data-night" stacks={stacks} xlabels={xlabels} minHeight={250} min={8}
          refLine={needs.length === 0 ? undefined : { hours: mean(needs) / 60, label: `Garmin's sleep need ${sleepText(mean(needs) * 60)}` }}
          tip={(i): Tip => {
            const r = at(i);
            if (!r || r.sleep_duration_sec == null) return { label: `${dm(i)}: no sleep recorded`, head: dm(i), rows: [] };
            if (!readNight(r)) {
              return {
                label: `${dm(i)}: Not read by the watch, ${durText(r.sleep_duration_sec)} asleep`,
                head: dm(i),
                rows: [["Asleep", durText(r.sleep_duration_sec)], ["Status", "Not read by the watch"]] as Array<[string, string]>,
              };
            }
            const stage = (s: number | null) => txt(s, durText);
            const score = r.sleep_score == null ? noValue : `${r.sleep_score}${r.sleep_score_word ? ` ${r.sleep_score_word}` : ""}`;
            const bed = r.sleep_start_local == null || r.sleep_end_local == null ? noValue : `${hhmm(r.sleep_start_local)} to ${hhmm(r.sleep_end_local)}`;
            const rowsOut: Array<[string, string]> = [
              ["Asleep", durText(r.sleep_duration_sec)], ["Score", score], ["Deep", stage(r.sleep_deep_sec)], ["Light", stage(r.sleep_light_sec)],
              ["REM", stage(r.sleep_rem_sec)], ["Awake", txt(r.sleep_awake_sec, (s) => `${Math.round(s / 60)} min`)], ["In bed", bed],
            ];
            return {
              label: `${dm(i)}: ${durText(r.sleep_duration_sec)}, score ${score}, deep ${stage(r.sleep_deep_sec)}, light ${stage(r.sleep_light_sec)}, REM ${stage(r.sleep_rem_sec)}, awake ${txt(r.sleep_awake_sec, (s) => `${Math.round(s / 60)} min`)}, ${bed}`,
              head: dm(i),
              rows: rowsOut,
            };
          }}
        />
      </>
    );
  } else {
    const hrv = mode === "HRV";
    const values = dates.map((_, i) => (hrv ? at(i)?.hrv_overnight : at(i)?.resting_hr) ?? null);
    const lo = balance?.hrv_balanced_low ?? null;
    const hi = balance?.hrv_balanced_high ?? null;
    body = nonNull(values).length === 0 ? (
      <Empty why={NO_DATA} />
    ) : (
      <LineSeriesChart
        testid="nights-chart" values={values} colour={hrv ? "#7fd9a8" : "#ff8fd0"} minHeight={250}
        yfmt={(v) => `${Math.round(v)} ${hrv ? "ms" : "bpm"}`} xlabels={xlabels}
        guides={hrv && lo != null && hi != null ? [lo, hi] : []}
        tip={(i): Tip => {
          if (!hrv) {
            const t = txt(values[i], (v) => `${v} bpm`);
            return { label: `${dm(i)}: resting heart rate ${t}`, head: dm(i), rows: [["Resting heart rate", t]] };
          }
          const t = txt(values[i], (v) => `${v} ms`);
          const word = at(i)?.hrv_status ?? null;
          const range = lo != null && hi != null ? `${lo} to ${hi} ms` : null;
          const parts = [`HRV ${t}`, ...(word ? [word] : []), ...(range ? [`balanced range ${range}`] : [])];
          return {
            label: `${dm(i)}: ${parts.join(", ")}`,
            head: dm(i),
            rows: [["HRV", t], ...(word ? [["Garmin's word", word] as [string, string]] : []), ...(range ? [["Balanced range", range] as [string, string]] : [])],
          };
        }}
      />
    );
  }
  return (
    <Panel
      title="LAST 28 NIGHTS"
      note={
        <Segmented
          variant="seg" value={mode} onChange={(v) => setMode(v as NightsMode)}
          options={[{ label: "Sleep", value: "SLEEP" }, { label: "Resting HR", value: "RHR" }, { label: "HRV", value: "HRV" }]}
        />
      }
    >
      {body}
    </Panel>
  );
}

type Kind = "dur" | "score" | "bed" | "wake" | "bpm" | "ms";
const sign = (d: number) => (d > 0 ? "+" : "-");

function show(v: number, kind: Kind): string {
  switch (kind) {
    case "dur": return durText(v);
    case "score": return String(Math.round(v));
    case "bed": return clockOfMinutes(v);
    case "wake": return clockOfMinutes(v);
    case "bpm": return `${Math.round(v)} bpm`;
    case "ms": return `${Math.round(v)} ms`;
  }
}
/** The signed difference of two means, for the rows that have one: "0 min" when it rounds to nothing. */
function change(d: number, kind: "dur" | "score"): string {
  if (kind === "dur") return Math.round(Math.abs(d) / 60) === 0 ? "0 min" : `${sign(d)}${durText(Math.abs(d))}`;
  const r = Math.round(Math.abs(d));
  return r === 0 ? "0" : `${sign(d)}${r}`;
}

export function SleepWeek({ rows, today }: DayProps) {
  const dated = byDate(rows);
  const d28 = lastDates(today, 28);
  const d7 = d28.slice(-7);
  // The sleep rows average the nights the watch read; Resting HR and HRV belong to the day, so they take every day.
  const get = (f: (r: GarminSnapshot) => number | null, nightsOnly = true) => (dates: string[]) =>
    nonNull(dates.map((d) => { const r = dated.get(d); return r && (!nightsOnly || readNight(r)) ? f(r) : null; }));
  const nightCount = (dates: string[]) => dates.filter((d) => { const r = dated.get(d); return r !== undefined && readNight(r); }).length;
  const readHead = (text: string, dates: string[]) => {
    const n = nightCount(dates);
    return hint(`${n} ${n === 1 ? "night" : "nights"} read`, text);
  };
  const start = (r: GarminSnapshot) => (r.sleep_start_local == null ? null : bedMinutes(r.sleep_start_local));
  const end = (r: GarminSnapshot) => (r.sleep_end_local == null ? null : wakeMinutes(r.sleep_end_local));
  // [label, the field, how it reads, whether more is better (null: no verdict, no colour)]
  const spec: Array<[string, (r: GarminSnapshot) => number | null, Kind, boolean | null]> = [
    ["Need", (r) => (r.sleep_need_min == null ? null : r.sleep_need_min * 60), "dur", null],
    ["Asleep", (r) => r.sleep_duration_sec, "dur", true],
    ["Deep", (r) => r.sleep_deep_sec, "dur", true],
    ["REM", (r) => r.sleep_rem_sec, "dur", true],
    ["Score", (r) => r.sleep_score, "score", true],
    ["Bedtime", start, "bed", null],
    ["Waking", end, "wake", null],
    ["Resting HR", (r) => r.resting_hr, "bpm", null],
    ["HRV", (r) => r.hrv_overnight, "ms", null],
  ];
  const table: Cell[][] = spec.map(([label, f, kind, upGood]) => {
    const own = kind !== "bpm" && kind !== "ms";
    const a = get(f, own)(d7);
    const b = get(f, own)(d28);
    const dash = <Dash why={NO_VALUE} />;
    // Resting HR and HRV average the days with a reading, show how many on hover, and a dash below the bar (4 of 7, 16 of 28).
    const counted = (v: number[], days: number, need: number): ReactNode => {
      const r = averageOf(v, days, need);
      if (r.n === 0) return <Dash why={NO_DATA} />;
      if (r.mean === null) return <Dash why={`Only ${r.n} of ${days} days have a reading; the average needs ${need}`} />;
      return <span title={`${r.n} of ${days} days have a reading`}>{show(r.mean, kind)}</span>;
    };
    const dayRow = kind === "bpm" || kind === "ms";
    const last7 = dayRow ? counted(a, 7, 4) : a.length > 0 ? show(mean(a), kind) : dash;
    const last28 = dayRow ? counted(b, 28, 16) : b.length > 0 ? show(mean(b), kind) : dash;
    // Resting HR and HRV carry no Change; Bedtime and Waking carry the spread of the last 7 nights (population standard deviation).
    if (kind === "bpm" || kind === "ms") return [label, last7, last28, ""];
    if (kind === "bed" || kind === "wake") return [label, last7, last28, a.length > 0 ? `\u00b1 ${Math.round(stdev(a))} min` : dash];
    if (a.length === 0 || b.length === 0) return [label, last7, last28, dash];
    const d = mean(a) - mean(b);
    const flat = change(d, kind).startsWith("0");
    const cls = flat || upGood === null ? "" : (d > 0) === upGood ? "g" : "a";
    return [label, last7, last28, { v: change(d, kind), cls }];
  });

  const nights = get((r) => r.sleep_duration_sec)(d7);
  const count = `${nights.length} ${nights.length === 1 ? "night" : "nights"}`;
  return (
    <Panel title="SLEEP">
      <div className="kv" style={{ margin: "2px 0 12px" }} data-testid="sleep-average">
        <span className="num" style={{ fontSize: 26 }} title={nights.length > 0 ? count : undefined}>
          {nights.length > 0 ? sleepText(mean(nights)) : <Dash why="No night recorded in the last 7 days" />}
        </span>
        {nights.length > 0 && nights.length < 7 && <span className="pill">{count}</span>}
      </div>
      <Table head={["", readHead("Last 7 days", d7), readHead("Last 28 days", d28), "Change"]} rows={table} />
    </Panel>
  );
}

export function Endurance({ rows, today }: DayProps) {
  const scoreRow = newestWith(rows, (r) => r.endurance_score);
  const limitsRow = newestWith(rows, (r) => r.endurance_limits_json);
  const limits = limitsRow === null ? { v: null, why: NO_DATA } : readJson(limitsRow.endurance_limits_json, isLimits);
  const score = scoreRow?.endurance_score ?? null;
  const lim = limits.v;
  const classes = lim ? [...lim.classes].sort((a, b) => a.lower - b.lower) : [];
  const lower = lim?.gauge_lower ?? null;
  const upper = lim?.gauge_upper ?? null;

  let gauge: ReactNode;
  if (score == null) gauge = <Empty why={NO_DATA} />;
  else if (lim === null) gauge = <Empty why={limits.why ?? NO_DATA} />;
  else if (lower === null || upper === null || classes.length === 0) gauge = <Empty why={NO_DATA} />;
  else {
    const top = (i: number) => (i + 1 < classes.length ? classes[i + 1].lower : upper);
    let at = -1;
    classes.forEach((c, i) => { if (c.lower <= score) at = i; });
    gauge = (
      <Gauge
        id="endurance" value={score} min={lower} max={upper} big sw={11} w={260} h={190}
        bands={classes.map((c, i): [number, number, string] => [c.lower, top(i), CLASS_COLOURS[i % CLASS_COLOURS.length]])}
        marks={classes.slice(1).map((c) => c.lower)}
        label={at < 0 ? undefined : `${plainWord(classes[at].name)} (${classes[at].lower} to ${top(at)})`}
        title={classes.map((c) => `${plainWord(c.name).toLowerCase()} from ${c.lower}`).join(", ")}
        text={String(score)}
      />
    );
  }

  // One point per Monday to Sunday week: the last score Garmin gave in it.
  const monday = mondayOf(today);
  const weeks = Array.from({ length: 12 }, (_, i) => addDays(monday, (i - 11) * 7));
  const values = weeks.map((w) => {
    const inWeek = rows.filter((r) => r.date >= w && r.date <= addDays(w, 6) && r.endurance_score != null);
    return inWeek.length === 0 ? null : (inWeek[inWeek.length - 1].endurance_score as number);
  });
  return (
    <Panel title={hint("Garmin's endurance score and its classes (Garmin's own limits)", "ENDURANCE SCORE")}>
      <div style={{ display: "grid", gridTemplateColumns: "260px minmax(0, 1fr)", gap: 18, alignItems: "center" }}>
        <div>
          {gauge}
          {scoreRow && scoreRow.date !== today && <div style={{ textAlign: "center" }}><AsOf date={scoreRow.date} today={today} /></div>}
        </div>
        {nonNull(values).length === 0 ? (
          <Empty why={NO_DATA} />
        ) : (
          <LineSeriesChart
            testid="endurance-chart" values={values} colour="#29c6ff" minHeight={200} yfmt={(v) => String(Math.round(v))}
            xlabels={weeks.map((w, i): [number, string] => [i, dayMonth(w)]).filter(([i]) => i % 3 === 0)}
            tip={(i): Tip => {
              const t = values[i] === null ? noValue : String(values[i]);
              return { label: `Week of ${dayMonth(weeks[i])}: endurance score ${t}`, head: `Week of ${dayMonth(weeks[i])}`, rows: [["Endurance score", t]] };
            }}
          />
        )}
      </div>
    </Panel>
  );
}

export function Race({ rows, today }: DayProps) {
  // Each figure comes from the newest day that has it, and says which day when that is not today.
  const field = (pick: (r: GarminSnapshot) => number | null): { v: number; date: string } | null => {
    const r = newestWith(rows, pick);
    return r === null ? null : { v: pick(r) as number, date: r.date };
  };
  const show = (pick: (r: GarminSnapshot) => number | null, f: (v: number) => string): ReactNode => {
    const x = field(pick);
    return x === null ? <Dash why={NO_DATA} /> : <>{f(x.v)}<AsOf date={x.date} today={today} /></>;
  };
  const races: Array<[string, (r: GarminSnapshot) => number | null]> = [
    ["5 km", (r) => r.race_5k_sec], ["10 km", (r) => r.race_10k_sec], ["Half marathon", (r) => r.race_half_sec], ["Marathon", (r) => r.race_marathon_sec],
  ];
  return (
    <Panel title="RACE ESTIMATES AND FITNESS">
      <table className="cot">
        <tbody>
          {races.map(([k, pick]) => (
            <tr key={k}><td>{k}</td><td className="num" style={{ textAlign: "right" }}>{show(pick, clock)}</td></tr>
          ))}
        </tbody>
      </table>
      <div style={{ height: 14 }} />
      <Rows
        items={[
          ["VO2 max", show((r) => r.vo2max_garmin, String)], ["Fitness age", show((r) => r.fitness_age, String)], ["Hill score", show((r) => r.hill_score, String)],
          ["Lactate threshold", show((r) => r.lactate_threshold_hr, (v) => `${v} bpm`)], ["Running power at threshold", show((r) => r.threshold_power_w, (v) => `${v} W`)],
        ]}
      />
    </Panel>
  );
}
