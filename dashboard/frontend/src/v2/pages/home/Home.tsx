import type { CSSProperties, ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Block, PageHeader, Say, State } from "../../kit";
import type { PageProps } from "../../shell/sections";
import type { GarminActivity, GarminSnapshot } from "../../../types";
import { serverDetail } from "../../../lib/errorText";
import { TILES, signedPct } from "../research/markets/data";
import { dayChange } from "../research/markets/series";
import { figFrom } from "../research/markets/useBars";
import { viewOf } from "../research/markets/Tiles";
import { useTileSources } from "../research/markets/sources";
import { useCalendarRange, useTodoList } from "../calendar/data";
import { stations } from "../../data/stations";
import { strategyLabel, useStrategyNames } from "../../data/strategyNames";
import type { Station } from "../../data/stations";
import { fetchDatasets, fetchRuns, fetchRunsSummary, fetchStrategies, overviewKeys } from "../../data/overviewApi";
import {
  LOADING, NOT_CONNECTED, NO_VALUE, sleepText, sportOf, whyNot,
  useGarminActivityList, useGarminDays, useGarminToday, useJudgeSummary, usePaperSummary, useRunningRuns,
} from "./data";
import { actDay, addDays, dayMonth, daysBetween, lastDates, localIso, mean, mondayOf, readNight } from "./sports/derive";
import { DayParts, allDay, hm, short, timed, two } from "./DayParts";
import Summary from "./summary/Summary";
import "./home.css";

// Round six (Tamer 2026-11-14), from the look trial's Home page (Desktop overview_trial, lab-p-home.js). The question: what
// does today hold, does anything need me, and how are the lab, my body and the markets doing? Read-only; every figure comes
// from a route that exists today, and where none does the page says why.

const NIGHTS = 28;
const WEEKS = 12;
const AHEAD = 7;
const ACTIVITY_WINDOW = 300;

// The four markets, by the Markets tiles' own names. The euro dollar row has no method, so the dollar index takes its place
// (Tamer, 2026-10-16).
const MARKET_NAMES = ["S&P 500", "US 10-year yield", "Oil Brent", "Dollar index"];
const MARKET_TILES = MARKET_NAMES.map((n) => {
  const def = TILES.find((t) => t.name === n);
  if (!def) throw new Error(`Home: the Markets tiles have no entry named ${n}`);
  return def;
});
const MARKET_SYMBOLS = MARKET_TILES.flatMap((t) => (t.kind === "bars" ? [t.symbol] : []));

const vars = (v: Record<string, number | string>) => v as CSSProperties;
const COUNT_WORD = ["No", "One", "Two", "Three", "Four", "Five"];
const race = (s: number) => (s >= 3600 ? `${Math.floor(s / 3600)}:${two(Math.floor((s % 3600) / 60))}:${two(s % 60)}` : `${Math.floor(s / 60)}:${two(s % 60)}`);
const mmss = (s: number) => { const r = Math.round(s); return `${Math.floor(r / 60)}:${two(r % 60)}`; };

/** Bars with a dashed average; a day with no value is an outlined stub, and every bar names itself on hover. */
function Bars({ list, avg, testid }: { list: Array<{ v: number | null; title: string; now?: boolean }>; avg: number | null; testid: string }) {
  const top = Math.max(1, ...list.map((x) => x.v ?? 0)) * 1.12;
  return (
    <div className="hm-bars" data-testid={testid}>
      {list.map((x, i) => (
        <i key={i} className={x.v == null ? "none" : x.now ? "now" : undefined} style={vars({ "--k": i, height: x.v == null ? "16%" : `${Math.max(2, (x.v / top) * 100)}%` })} title={x.title} />
      ))}
      {avg != null && <span className="hm-avg" style={{ bottom: `${(avg / top) * 100}%` }} />}
    </div>
  );
}

/** A line through the days that have a value, broken where the watch sent nothing; the dashed line is its average. */
function Line({ list, avg }: { list: Array<{ v: number | null; title: string }>; avg: number | null }) {
  const vs = list.flatMap((x) => (x.v == null ? [] : [x.v]));
  const lo = Math.min(...vs) - 2, hi = Math.max(...vs) + 2;
  const x = (i: number) => (list.length === 1 ? 50 : (i / (list.length - 1)) * 100);
  const y = (v: number) => 6 + (1 - (v - lo) / (hi - lo)) * 88;
  const runs: Array<Array<[number, number]>> = [];
  let cur: Array<[number, number]> = [];
  list.forEach((p, i) => { if (p.v == null) { if (cur.length) runs.push(cur); cur = []; } else cur.push([x(i), y(p.v)]); });
  if (cur.length) runs.push(cur);
  const lastI = list.map((p, i) => (p.v == null ? -1 : i)).filter((i) => i >= 0).pop() ?? -1;
  return (
    <div className="hm-line" data-testid="rhr">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        <defs><linearGradient id="hm-rhr-g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#fff" stopOpacity=".14" /><stop offset="1" stopColor="#fff" stopOpacity="0" /></linearGradient></defs>
        {avg != null && <line className="hm-l-avg" x1={0} x2={100} y1={y(avg)} y2={y(avg)} />}
        {runs.map((r, k) => (
          <g key={k}>
            <polygon className="hm-l-area" points={`${r.map(([a, b]) => `${a},${b}`).join(" ")} ${r[r.length - 1][0]},100 ${r[0][0]},100`} />
            <polyline className="hm-l-line" points={r.map(([a, b]) => `${a},${b}`).join(" ")} />
          </g>
        ))}
      </svg>
      {lastI >= 0 && <span className="hm-l-dot" style={{ left: `${x(lastI)}%`, top: `${y(list[lastI].v as number)}%` }} />}
      <div className="hm-l-hit">{list.map((p, i) => <span key={i} title={p.title} className={p.v == null ? "none" : undefined} />)}</div>
    </div>
  );
}

function Axis({ from, to }: { from: string; to: string }) {
  return <div className="hm-axis"><span>{from}</span><span>{to}</span></div>;
}

/** The six stations from data to capital, by the one shared rule (data/stations.ts), lit up to the first broken one. */
function Lab({ list }: { list: Station[] }) {
  const navigate = useNavigate();
  // Lit from data up to the first broken station; with none broken, all the way.
  const stop = list.findIndex((s) => s.tone === "bad");
  return (
    <div className="hm-lab" data-testid="lab">
      <div className="hm-lt"><i style={{ width: `${((stop === -1 ? list.length - 1 : stop) / (list.length - 1)) * 100}%` }} /></div>
      {list.map((s, k) => (
        <button type="button" key={s.id} className={`hm-ls s-${s.tone ?? "load"}`} style={vars({ "--k": k })} data-station={s.id}
          title={`${s.name}: ${s.word}. ${s.why}${s.rule ? `\n${s.rule}` : ""}`} onClick={() => navigate(s.path)}>
          <span className="hm-ln" /><span className="hm-lname">{s.name}</span><span className="num hm-lf">{s.figure}</span><span className="hm-lx">{s.fact}</span><span className="hm-lp"><State tone={s.tone ?? "dim"}>{s.word}</State></span>
        </button>
      ))}
    </div>
  );
}

export default function Home({ app }: PageProps) {
  const navigate = useNavigate();
  const nowD = new Date();
  const today = localIso(nowD);
  const now = nowD.getHours() * 60 + nowD.getMinutes();
  const judgeQ = useJudgeSummary();
  const paperQ = usePaperSummary("today");
  const runsOnPaperQ = useRunningRuns();
  const namesQ = useStrategyNames();
  const rangeQ = useCalendarRange(today, addDays(today, AHEAD));
  const daysQ = useGarminDays(NIGHTS);
  const garminQ = useGarminToday();
  const actsQ = useGarminActivityList(ACTIVITY_WINDOW);
  const todo = useTodoList(today);
  const datasetsQ = useQuery({ queryKey: overviewKeys.datasets, queryFn: fetchDatasets });
  const strategiesQ = useQuery({ queryKey: overviewKeys.strategies, queryFn: fetchStrategies });
  const runsQ = useQuery({ queryKey: overviewKeys.runs, queryFn: fetchRuns });
  const runsSumQ = useQuery({ queryKey: overviewKeys.runsSummary, queryFn: fetchRunsSummary });
  const sources = useTileSources(MARKET_SYMBOLS);

  // ---- What waits for Tamer: verdicts to approve and strategies asking to leave paper.
  const awaiting = judgeQ.data?.awaiting_ok ?? null;
  const asking = paperQ.data?.promotions_requested_count ?? null;
  const needs = awaiting != null && asking != null ? awaiting + asking : null;
  const needsWhy = judgeQ.isError ? serverDetail(judgeQ.error) : paperQ.isError ? serverDetail(paperQ.error) : null;

  // ---- The calendar
  const events = rangeQ.data ? timed(rangeQ.data) : [];
  const todays = events.filter((e) => e.day === today);
  const todaysAll = rangeQ.data ? allDay(rangeQ.data, today) : [];
  const nToday = todays.length + todaysAll.length;

  // ---- The watch: the nights and mornings it sent, newest last; a day it did not send stays a gap.
  const garminWhy = whyNot(daysQ) ?? (daysQ.data && !daysQ.data.configured ? NOT_CONNECTED : null);
  const byDay = new Map((daysQ.data?.snapshots ?? []).map((s) => [s.date.slice(0, 10), s]));
  const dates = lastDates(today, NIGHTS);
  const rows: Array<GarminSnapshot | null> = dates.map((d) => byDay.get(d) ?? null);
  const sentRows = rows.filter((r): r is GarminSnapshot => r !== null && r.sleep_duration_sec != null);
  const lastSent = sentRows[sentRows.length - 1] ?? null;
  const quiet = lastSent !== null && lastSent.date.slice(0, 10) < addDays(today, -1) ? dayMonth(lastSent.date.slice(0, 10)) : null;
  const read = rows.filter((r): r is GarminSnapshot => r !== null && readNight(r));
  const lastRead = read[read.length - 1] ?? null;
  const sleepAvg = read.length ? mean(read.map((r) => r.sleep_duration_sec as number)) : null;
  const rhrRows = rows.filter((r): r is GarminSnapshot => r !== null && r.resting_hr != null);
  const lastRhr = rhrRows[rhrRows.length - 1] ?? null;
  const rhrAvg = rhrRows.length ? mean(rhrRows.map((r) => r.resting_hr as number)) : null;

  // ---- Running, by week (Monday to Sunday, the watch's local dates), the newest twelve weeks.
  const thisMon = mondayOf(today);
  const weeks = Array.from({ length: WEEKS }, (_, i) => ({ w: addDays(thisMon, -7 * (WEEKS - 1 - i)), km: 0, runs: 0, blind: 0, other: 0 }));
  const byWeek = new Map(weeks.map((w) => [w.w, w]));
  const acts: GarminActivity[] = actsQ.data?.activities ?? [];
  for (const a of acts) {
    const w = byWeek.get(mondayOf(actDay(a)));
    if (!w) continue;
    // A run the watch gave no distance for is still a run: counted, named on hover, but adds no kilometres.
    if (sportOf(a.type_key) !== "Running") w.other += 1;
    else if (a.distance_m == null) { w.runs += 1; w.blind += 1; } else { w.km += a.distance_m / 1000; w.runs += 1; }
  }
  // Last week up to the same weekday, as the Sports page compares it, so the two pages give one figure.
  const sameDaysEnd = addDays(thisMon, daysBetween(thisMon, today) - 7);
  const lastWeekSoFar = acts.filter((a) => sportOf(a.type_key) === "Running" && a.distance_m != null && actDay(a) >= addDays(thisMon, -7) && actDay(a) <= sameDaysEnd)
    .reduce((t, a) => t + (a.distance_m as number) / 1000, 0);
  const actsWhy = whyNot(actsQ) ?? (actsQ.data && !actsQ.data.configured ? NOT_CONNECTED : null);
  const snap = garminQ.data?.snapshot ?? null;
  const races = snap ? ([["5 km", 5, snap.race_5k_sec], ["10 km", 10, snap.race_10k_sec], ["Half marathon", 21.0975, snap.race_half_sec], ["Marathon", 42.195, snap.race_marathon_sec]] as const)
    .flatMap(([n, km, s]) => (s == null ? [] : [{ n, km, s, pace: s / km }])) : [];
  const fitWhy = whyNot(garminQ) ?? (garminQ.data && !garminQ.data.configured ? NOT_CONNECTED : null);

  // ---- The lab, by the shared station rule.
  const failedRead: Partial<Record<Station["id"], unknown>> = {
    data: datasetsQ.error, strategies: strategiesQ.error ?? runsQ.error, runs: runsQ.error, judge: judgeQ.error, paper: paperQ.error,
  };
  const lab = stations({
    datasets: datasetsQ.data, strategies: strategiesQ.data, runs: runsQ.data, runsTotal: runsSumQ.data?.total, judge: judgeQ.data,
    paperRunning: paperQ.data?.running_count, now: Math.floor(Date.now() / 1000),
  }).map((s) => (failedRead[s.id] ? { ...s, word: "Could not read", why: serverDetail(failedRead[s.id]) } : s));

  // ---- The finding
  const lead = needsWhy ? "Home could not read what waits for you." : needs === null ? "Reading what waits for you." : needs > 0 ? `${needs} ${needs === 1 ? "thing needs" : "things need"} you today.` : "Nothing needs you today.";
  const named = [...todaysAll.map((e) => `${short(e.title)} all day`), ...todays.map((e) => `${short(e.title)} at ${hm(e.a)}`)];
  const calSaid: ReactNode = !rangeQ.data ? null : nToday === 0 ? "Nothing is on the calendar today. " : (
    <>{`${COUNT_WORD[nToday] ?? nToday} ${nToday === 1 ? "thing" : "things"} on the calendar: `}
      {named.map((t, k) => <span key={k}>{k > 0 && (k === named.length - 1 ? " and " : ", ")}<em className="run">{t}</em></span>)}. </>
  );
  const more = <>{needsWhy && <>{needsWhy} </>}{calSaid}{quiet && <em className="warn">{`Your watch has sent nothing since ${quiet}.`}</em>}</>;

  const todoOpen = todo.items.filter((i) => !i.todo.done);
  const todoErr = [todo.dayQ, todo.lateQ, todo.doneLateQ].find((q) => q.isError);
  const todoWhy = todoErr ? serverDetail(todoErr.error) : null;
  const todoPending = todo.dayQ.isPending || todo.lateQ.isPending || todo.doneLateQ.isPending;
  const todoTitle = (todo.total ? `${todo.done} of ${todo.total} done${todo.lateCount ? `, ${todo.lateCount} late` : ""}` : "Nothing on today's list");
  const asked = (runsOnPaperQ.data ?? []).filter((r) => r.latest_promotion?.state === "requested");
  const todoShown = todoWhy || todoPending ? [] : todoOpen.slice(0, 3);
  const drew = needs === null || asked.length > 0 || todoWhy !== null || todoPending || todoShown.length > 0;
  const notWatch = (why: string) => <p className="empty">{why}</p>;

  const dateText = new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  const sleepFig = (sec: number) => { const m = Math.round(sec / 60); return <>{Math.floor(m / 60)}<small>h</small>{two(m % 60)}</>; };
  const needRows = [["Verdicts to approve", awaiting, "/app/research/judge", "Runs that passed the judge and wait for your yes"],
    ["Asking to leave paper", asking, "/app/trading/paper", "Paper strategies that ask to trade real money"]] as const;

  return (
    <div className="v2-page hm-page">
      <PageHeader crumb={app.name} title="Home"
        right={<div className="hm-side"><span className="hm-date">{dateText}</span><button type="button" className="btn hm-cal" onClick={() => navigate("/app/calendar/calendar")}>Open the calendar</button></div>} />
      <Summary />
      <Say lead={lead} more={more} />
      <div className="hm-stack">
        {rangeQ.isError ? <Block title="Today"><p className="empty">{serverDetail(rangeQ.error)}</p></Block>
          : !rangeQ.data ? <Block title="Today"><p className="empty">{LOADING}</p></Block>
          : <div className="hm-row2"><DayParts events={events} all={rangeQ.data} today={today} now={now} /></div>}

        <Block title="Needs you" state={needs === null ? undefined : <State tone={needs || todo.lateCount > 0 ? "warn" : "good"}>{needs ? `${needs} waiting` : todo.lateCount > 0 ? `${todo.lateCount} late` : "All clear"}</State>}>
          <div className="hm-needs" data-testid="needs">
            <div className="hm-n3">
              {needRows.map(([t, n, to, what]) => (
                <div key={t} className="hm-nk" title={what}>
                  <i>{t}</i><b className={n ? "num warn" : "num"}>{n ?? NO_VALUE}</b>
                  {n ? <Link className="hm-open" to={to}>Open</Link> : null}
                </div>
              ))}
              {todoWhy || todoPending ? null : (
                <Link className="hm-nk hm-todo" data-testid="todo" to="/app/calendar/calendar?view=today" title={todoTitle}>
                  <i>To do today</i><b className={todo.lateCount > 0 ? "num warn" : "num"}>{todoOpen.length}</b>
                </Link>
              )}
            </div>
            <div className="hm-nopen">
              {needs === null && <p className="empty">{needsWhy ?? LOADING}</p>}
              {asked.map((r) => {
                const name = strategyLabel(namesQ, r.strategy_id);
                return <p key={r.paper_run_id} className="hm-ask"><span title={name.hover}>{name.text}</span> asks to leave paper</p>;
              })}
              {todoWhy ? <p className="empty">{todoWhy}</p> : todoPending ? <p className="empty">{LOADING}</p> : null}
              {todoShown.map((i) => <span key={i.todo.id} className="hm-xn" title={i.late ? "Late: its day has passed" : undefined}>{i.late && <span className="warn">Late: </span>}{i.todo.text}</span>)}
              {drew ? null : <p className="empty">No to-do is open.</p>}
            </div>
          </div>
        </Block>

        <Block title="Pipeline overview" right={<button type="button" className="btn" onClick={() => navigate("/app/research/overview")}>Open Research</button>}>
          <Lab list={lab} />
        </Block>

        <div className="hm-c4">
          <Block title="Sleep" state={quiet ? <State tone="warn">{`Watch quiet since ${quiet}`}</State> : undefined}>
            {garminWhy ? notWatch(garminWhy) : !lastRead ? notWatch(NO_VALUE) : (
              <>
                <p className="hm-lead"><span className="hm-fig fig" title={sleepText(lastRead.sleep_duration_sec as number)}>{sleepFig(lastRead.sleep_duration_sec as number)}</span>
                  <span>{`on ${dayMonth(lastRead.date.slice(0, 10))}${sleepAvg != null ? `, ${sleepText(sleepAvg)} on average` : ""}`}</span></p>
                <Bars testid="sleep" avg={sleepAvg} list={rows.map((r, i) => {
                  const d = dayMonth(dates[i]);
                  if (r === null || r.sleep_duration_sec == null) return { v: null, title: `${d}: the watch has not sent this night` };
                  if (!readNight(r)) return { v: null, title: `${d}: the watch did not read this night` };
                  return { v: r.sleep_duration_sec, title: `Night to ${d}: ${sleepText(r.sleep_duration_sec)}${r.sleep_score != null ? `, sleep score ${r.sleep_score}` : ""}` };
                })} />
                <Axis from={dayMonth(dates[0])} to={dayMonth(dates[dates.length - 1])} />
                <p className="hm-xn">{`Each bar is one night. The dashed line is your average${sleepAvg != null ? `, ${sleepText(sleepAvg)}` : ""}.`}</p>
              </>
            )}
          </Block>
          <Block title="Resting heart rate">
            {garminWhy ? notWatch(garminWhy) : !lastRhr ? notWatch(NO_VALUE) : (
              <>
                <p className="hm-lead"><span className="hm-fig fig">{lastRhr.resting_hr}</span>
                  <span>{`bpm on ${dayMonth(lastRhr.date.slice(0, 10))}${rhrAvg != null ? `, ${Math.round(rhrAvg)} on average` : ""}`}</span></p>
                <Line avg={rhrAvg} list={rows.map((r, i) => (r?.resting_hr == null
                  ? { v: null, title: `${dayMonth(dates[i])}: the watch has not sent this morning` }
                  : { v: r.resting_hr, title: `${dayMonth(dates[i])}: ${r.resting_hr} bpm${r.hrv_overnight != null ? `, overnight heart rate variability ${r.hrv_overnight} ms` : ""}` }))} />
                <Axis from={dayMonth(dates[0])} to={dayMonth(dates[dates.length - 1])} />
                <p className="hm-xn">{`Beats a minute at rest, each morning. The dashed line is your average${rhrAvg != null ? `, ${Math.round(rhrAvg)}` : ""}. Lower is calmer.`}</p>
              </>
            )}
          </Block>
          <Block title="Running">
            {actsWhy ? notWatch(actsWhy) : (
              <>
                <p className="hm-lead"><span className="hm-fig fig">{weeks[WEEKS - 1].km.toFixed(1)}</span><span>{`km this week, ${lastWeekSoFar.toFixed(1)} the same days last week`}</span></p>
                <Bars testid="running" avg={null} list={weeks.map((w) => ({
                  v: w.km, now: w.w === thisMon,
                  title: `Week of ${dayMonth(w.w)}: ${w.km.toFixed(1)} km in ${w.runs} ${w.runs === 1 ? "run" : "runs"}${w.blind ? ` (${w.blind} with no distance)` : ""}, ${w.other} other ${w.other === 1 ? "session" : "sessions"}`,
                }))} />
                <Axis from={dayMonth(weeks[0].w)} to="this week" />
                <p className="hm-xn">Each bar is one week of running, in kilometres. This week is the white one.</p>
              </>
            )}
          </Block>
          <Block title="Race pace">
            {fitWhy ? notWatch(fitWhy) : (
              <>
                <p className="hm-lead"><span className="hm-fig fig" title={snap?.vo2max == null ? NO_VALUE : undefined}>{snap?.vo2max ?? "-"}</span>
                  <span>{`the watch's fitness score (VO2 max)${snap?.vo2max_as_of ? `, ${dayMonth(snap.vo2max_as_of.slice(0, 10))}` : ""}`}</span></p>
                {races.length === 0 ? <p className="empty">The watch predicts no race times yet.</p> : (
                  <table className="hm-tbl hm-race" data-testid="race">
                    <thead><tr><th>Race</th><th className="r">Time</th><th className="r">Each km</th></tr></thead>
                    <tbody>
                      {races.map((r) => (
                        <tr key={r.n} title={`${r.n}, as the watch predicts it: ${race(r.s)}, ${mmss(r.pace)} per km`}>
                          <td>{r.n}</td><td className="num r">{race(r.s)}</td><td className="num r">{mmss(r.pace)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {races.length > 0 && <p className="hm-xn">The times your watch predicts if you raced today.</p>}
              </>
            )}
          </Block>
        </div>

        <Block title="Markets" right={<button type="button" className="btn" onClick={() => navigate("/app/research/data-library")}>Open Markets</button>}>
          <div className="hm-mk" data-testid="markets">
            {MARKET_TILES.map((d, k) => {
              const v = viewOf(d, sources);
              // The mockup's Home writes a day move with two decimals (the Markets page keeps one).
              const dayText = (def: typeof d, text: string) => {
                if (def.kind !== "bars") return text;
                const f = figFrom(sources.bars.get(def.symbol), dayChange);
                return f.value === null ? text : signedPct(f.value, 2);
              };
              const c = v.spark?.closes ?? [];
              const lo = Math.min(...c), hi = Math.max(...c);
              const up = v.day.tone !== "rd";
              const src = v.date === null ? v.last.title : `EODHD, close of ${v.date.slice(0, 10).split("-").reverse().join("/")}`;
              const hue = v.last.text === "-" ? "" : v.day.tone === "rd" ? " rd" : v.day.tone === "g" ? " g" : "";
              const pts = c.map((p, i) => `${(i / (c.length - 1)) * 100},${4 + (1 - (p - lo) / (hi - lo || 1)) * 32}`).join(" ");
              return (
                <div key={v.name} className={`hm-t${hue}`} style={vars({ "--k": k })} data-tile={v.name}>
                  <span className="hm-mn" title={src}>{v.name}</span>
                  <b className={v.last.text === "-" ? "hm-mf num dim" : "hm-mf num"} title={v.last.text === "-" ? v.last.title : src}>{v.last.text}</b>
                  {c.length > 1 ? (
                    <svg className={up ? "hm-sp up" : "hm-sp down"} viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">
                      <defs><linearGradient id={`hm-g-${k}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={up ? "rgb(47 224 138)" : "rgb(255 90 99)"} stopOpacity=".22" /><stop offset="1" stopColor={up ? "rgb(47 224 138)" : "rgb(255 90 99)"} stopOpacity="0" /></linearGradient></defs>
                      <polygon className="hm-sa" fill={`url(#hm-g-${k})`} points={`0,40 ${pts} 100,40`} />
                      <polyline points={pts} />
                    </svg>
                  ) : <span className="hm-none">{v.last.text === "-" ? (v.last.title ?? NO_VALUE) : "No chart yet"}</span>}
                  {v.last.text !== "-" && <span className="hm-dy"><i>Day</i><b className={`num ${v.day.tone}`.trim()} title={v.day.title}>{dayText(d, v.day.text)}</b></span>}
                </div>
              );
            })}
          </div>
        </Block>
      </div>
    </div>
  );
}
