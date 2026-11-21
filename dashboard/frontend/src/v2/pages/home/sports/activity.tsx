import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Legend, Panel, Rows, Spark, Table, Track } from "../../../kit";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { GarminActivity } from "../../../../types";
import { useSettings } from "../../../settings";
import { LOADING, NO_VALUE, sleepText, sportOf } from "../data";
import { StackedBars } from "./charts";
import type { StackPart, Tip } from "./charts";
import { SPORT_COLOUR } from "./tiles";
import { PACE_TYPES, actDay, activityWord, addDays, dayMonth, kmText, minOrHm, mondayOf, pace, sum } from "./derive";

// The cards built from the activity list, and the activity window.

const Dash = ({ why }: { why: string }) => <span title={why}>-</span>;
const SPORTS = ["Running", "Cycling", "Swimming", "Gym and other"] as const;

export function Weekly({ acts, today }: { acts: GarminActivity[]; today: string }) {
  const monday = mondayOf(today);
  const weeks = Array.from({ length: 12 }, (_, i) => addDays(monday, (i - 11) * 7));
  const secs = weeks.map((w) =>
    SPORTS.map((s) => sum(acts.filter((a) => actDay(a) >= w && actDay(a) <= addDays(w, 6) && sportOf(a.type_key) === s).map((a) => a.duration_sec ?? 0))),
  );
  const stacks: Array<StackPart[] | null> = secs.map((row) => row.map((v, k) => ({ value: v / 3600, colour: SPORT_COLOUR[SPORTS[k]] })));
  return (
    <Panel title={<b style={{ fontWeight: "inherit" }} title="Hours by sport, Monday to Sunday">WEEKLY TRAINING, 12 WEEKS</b>}>
      <Legend items={SPORTS.map((s): [string, string] => [s, SPORT_COLOUR[s]])} />
      <StackedBars
        testid="weekly-chart" attr="data-week" stacks={stacks} minHeight={230}
        xlabels={weeks.map((w, i): [number, string] => [i, dayMonth(w)]).filter(([i]) => (11 - i) % 3 === 0)}
        tip={(i): Tip => {
          const parts = SPORTS.map((s, k): [string, string] => [s, sleepText(secs[i][k])]);
          return { label: `Week of ${dayMonth(weeks[i])}: ${parts.map(([a, b]) => `${a} ${b}`).join(", ")}`, head: `Week of ${dayMonth(weeks[i])}`, rows: parts };
        }}
      />
    </Panel>
  );
}

export function ActivityList({ acts, onOpen }: { acts: GarminActivity[]; onOpen: (a: GarminActivity) => void }) {
  const newest = [...acts].sort((a, b) => b.start_time_local.localeCompare(a.start_time_local)).slice(0, 8);
  return (
    <Panel title="LAST ACTIVITIES">
      <table className="acts">
        <tbody>
          {newest.map((a) => (
            <tr
              key={a.activity_id} data-act={a.activity_id} role="button" tabIndex={0}
              onClick={() => onOpen(a)}
              onKeyDown={(e) => { if (e.key === "Enter") onOpen(a); }}
            >
              <td>
                <i style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: SPORT_COLOUR[sportOf(a.type_key)], marginRight: 8 }} />
                <span title={a.name ?? undefined}>{a.type_key ? activityWord(a.type_key) : "Activity"}, {dayMonth(actDay(a))}</span>
              </td>
              <td className="num" style={{ textAlign: "right" }}>{a.distance_m == null || a.distance_m === 0 ? <Dash why={NO_VALUE} /> : `${kmText(a.distance_m)} km`}</td>
              <td className="num" style={{ textAlign: "right" }}>{a.duration_sec == null ? <Dash why={NO_VALUE} /> : minOrHm(a.duration_sec)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

const ZONE_COLOURS = ["#c3cbd6", "#4d9bff", "#2fe08a", "#ffb020", "#ff5a63"];

export function ActivityWindow({ activity: a, onClose }: { activity: GarminActivity; onClose: () => void }) {
  const q = useQuery({ queryKey: ["garmin-activity", a.activity_id], queryFn: () => api.life.garminActivityDetail(a.activity_id) });
  const closeRef = useRef<HTMLButtonElement>(null);
  const { settings } = useSettings();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => closeRef.current?.focus(), []);

  const sport = sportOf(a.type_key);
  const stat = (name: string, v: number | null, f: (v: number) => string): [string, ReactNode] => [name, v == null ? <Dash why={NO_VALUE} /> : f(v)];
  // Distance is left out when there is none, Pace when the type has no pace: a row that cannot apply is not a dash.
  const stats: Array<[string, ReactNode]> = [
    ...(a.distance_m != null && a.distance_m > 0 ? [stat("Distance", a.distance_m, (v) => `${kmText(v)} km`)] : []),
    stat("Time", a.duration_sec, minOrHm),
    ...(a.type_key != null && PACE_TYPES.includes(a.type_key) ? [stat("Pace", a.avg_pace_min_per_km, (v) => `${pace(v)} /km`)] : []),
    stat("Average HR", a.avg_hr, (v) => `${Math.round(v)} bpm`),
    stat("Max HR", a.max_hr, (v) => `${Math.round(v)} bpm`),
    stat("Training effect", a.aerobic_te, (v) => `${v.toFixed(1)} aerobic`),
    stat("Load", a.training_load, (v) => String(Math.round(v))),
    stat("Climb", a.elevation_gain_m, (v) => `${Math.round(v)} m`),
    stat("Calories", a.total_calories, (v) => `${Math.round(v)} kcal`),
  ];

  let lower: ReactNode;
  if (q.isError) lower = <div className="empty">{serverDetail(q.error)}</div>;
  else if (q.isPending) lower = <div className="empty">{LOADING}</div>;
  else {
    const d = q.data.detail;
    const hr = d.samples.map((s) => s.directHeartRate).filter((v): v is number => typeof v === "number");
    const zones = [...d.hr_zones].sort((x, y) => (x.zone ?? 0) - (y.zone ?? 0));
    const top = Math.max(...zones.map((z) => z.seconds ?? 0), 1);
    const splits = sport === "Running" ? d.splits : [];
    const w = d.weather;
    lower = (
      <>
        <div className="kl" style={{ marginTop: 6 }}>HEART RATE</div>
        <div data-testid="act-hr" style={{ minHeight: 120 }}>
          {hr.length > 1 ? <Spark values={hr} colour="#ff5a63" fill={0.12} height={120} dot={false} /> : <Dash why={NO_VALUE} />}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 22, marginTop: 6 }}>
          <div>
            <div className="kl">TIME IN HEART RATE ZONES</div>
            {zones.length === 0 ? (
              <div className="empty"><Dash why={NO_VALUE} /></div>
            ) : (
              <table className="cot">
                <tbody>
                  {zones.map((z, i) => (
                    <tr key={z.zone ?? i} data-zone={z.zone ?? i + 1}>
                      <td>Zone {z.zone ?? i + 1}</td>
                      <td style={{ width: "55%" }}>
                        {z.seconds == null ? <Dash why={NO_VALUE} /> : <Track fraction={z.seconds / top} colour={ZONE_COLOURS[i % ZONE_COLOURS.length]} />}
                      </td>
                      <td className="num" style={{ textAlign: "right" }}>{z.seconds == null ? <Dash why={NO_VALUE} /> : `${Math.round(z.seconds / 60)} min`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {splits.length > 0 ? (
            <div data-testid="splits">
              <div className="kl">SPLITS, PER KM</div>
              <Table
                head={["Km", "Pace", "HR"]}
                rows={splits.map((s, i) => [
                  String(s.lap ?? i + 1),
                  s.pace_min_per_km == null ? <Dash why={NO_VALUE} /> : pace(s.pace_min_per_km),
                  s.avg_hr == null ? <Dash why={NO_VALUE} /> : `${Math.round(s.avg_hr)} bpm`,
                ])}
              />
            </div>
          ) : w ? (
            <div>
              <div className="kl">WEATHER</div>
              <Rows
                items={[
                  ["Temperature", w.temp_c == null ? <Dash why={NO_VALUE} /> : `${w.temp_c} °C`],
                  ["Wind", w.wind_kph == null ? <Dash why={NO_VALUE} /> : `${w.wind_kph} km/h`],
                  ["Humidity", w.humidity == null ? <Dash why={NO_VALUE} /> : `${w.humidity}%`],
                ]}
              />
            </div>
          ) : null}
        </div>
      </>
    );
  }
  const title = `${a.type_key ? activityWord(a.type_key) : "Activity"}, ${dayMonth(actDay(a))}`.toUpperCase();
  // The window sits on the body, outside the page's own wrapper, so it carries the page-wide class and the tone itself.
  const cls = ["v2", settings.tone === "vivid" ? "vivid" : "", "spov"].filter(Boolean).join(" ");
  return createPortal(
    <div className={cls} onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cd spovb" role="dialog" aria-modal="true" aria-label={title}>
        <div className="spovt">
          <div className="h2" style={{ flex: "1 1 200px", minWidth: 0, margin: 0 }}>
            <span>
              <i style={{ display: "inline-block", width: 9, height: 9, borderRadius: 2, background: SPORT_COLOUR[sport], marginRight: 8 }} />
              <span title={a.name ?? undefined}>{title}</span>
            </span>
          </div>
          <button type="button" ref={closeRef} className="btn q" onClick={onClose}>CLOSE</button>
        </div>
        <div className="spstats">
          {stats.map(([k, v]) => (
            <div key={k}>
              <div className="kl">{k.toUpperCase()}</div>
              <div className="num" style={{ fontSize: 20, color: "var(--hi)", marginTop: 4 }}>{v}</div>
            </div>
          ))}
        </div>
        {lower}
      </div>
    </div>,
    document.body,
  );
}
