import { useState } from "react";
import type { ReactNode } from "react";
import { Block, Say, State } from "../../../kit";
import type { NlTone } from "../../../kit";
import type { GarminActivity } from "../../../../types";
import { ChartBox, niceTicks } from "./charts";
import type { Tip } from "./charts";
import { activityName, dayMonth, minOrHm, pace } from "./derive";
import { SPORTS, avg } from "./model";
import type { Line, Model, Sess } from "./model";

// The Sports page as the mockup draws it (lab-p-sports.js): the finding, what needs you, the six summary tiles, fitness against
// fatigue, training by week, recovery by night and every session. Every figure comes from the model (model.ts), which works from
// the watch's own rows and sessions; where the watch holds nothing the element says so.

const PINK = "#ec6ecd", GOOD = "#2fe08a", WARN = "#ffb020", BAD = "#ff5a63", INK = "#eef2f7", SOFT = "#8d98a8";
const DASH = "-";
const BURN_HOVER = "What your body burned a day, on average over the last 7 finished days, as the watch counts it: at rest plus by moving. A day counts once it is over, so today is not in the average";
const whole = (n: number) => Math.round(n).toLocaleString("en-GB");
const tens = (n: number) => (Math.round(n / 10) * 10).toLocaleString("en-GB");
const WAIT = "The watch sync holds none yet";

const hmS = (sec: number) => { const m = Math.round(sec / 60); return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`; };
const minT = (min: number) => (min < 60 ? `${Math.round(min)} min` : hmS(min * 60));
const figH = (sec: number) => { const m = Math.round(sec / 60); return <>{Math.floor(m / 60)}<small>h</small>{String(m % 60).padStart(2, "0")}</>; };
const figMin = (min: number) => (min < 60 ? <>{Math.round(min)}<small>min</small></> : figH(min * 60));
const signed = (n: number, dp = 0) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(dp)}`;
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
const have = (a: Array<number | null>): number[] => a.filter((v): v is number => v != null);
const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
const tone = (t: string): NlTone => (t === "data" ? "run" : (t as NlTone));

// ---------------------------------------------------------------- charts

interface Q { X: (i: number) => number; Y: (v: number) => number; base: number; slot: number; hover: number | null; left: number; right: number }
function Plot({ testid, n, h = 260, lo, hi, ticks, yf, xl, tip, bars = false, draw }: {
  testid: string; n: number; h?: number; lo: number; hi: number; ticks: number[]; yf: (v: number) => string; xl: Array<[number, string]>;
  tip: (i: number) => Tip; bars?: boolean; draw: (q: Q) => ReactNode;
}) {
  const pad: [number, number, number, number] = [10, 10, 26, 54];
  return (
    <ChartBox testid={testid} n={n} mode={bars ? "bars" : "line"} pad={pad} minHeight={h} tip={tip}>
      {({ w, h: hh }, hover) => {
        const [pt, pr, pb, pl] = pad, span = w - pl - pr, slot = span / n;
        const X = (i: number) => (bars ? pl + (i + 0.5) * slot : pl + (n > 1 ? (i / (n - 1)) * span : 0));
        const Y = (v: number) => hh - pb - ((v - lo) / (hi - lo || 1)) * (hh - pb - pt);
        return (
          <>
            {ticks.map((v) => (
              <g key={v}>
                <line x1={pl} x2={w - pr} y1={Y(v)} y2={Y(v)} stroke="rgba(255,255,255,.06)" />
                <text x={pl - 8} y={Y(v) + 4} fill="#a3adbb" fontSize={11} textAnchor="end">{yf(v)}</text>
              </g>
            ))}
            {draw({ X, Y, base: hh - pb, slot, hover, left: pl, right: w - pr })}
            {xl.map(([i, text]) => <text key={i} x={X(i)} y={hh - 6} fill="#a3adbb" fontSize={11} textAnchor="middle">{text}</text>)}
          </>
        );
      }}
    </ChartBox>
  );
}
/** Runs of consecutive points as separate paths, so a night with no reading breaks the line. */
function runs(pts: Array<[number, number] | null>): Array<Array<[number, number]>> {
  const out: Array<Array<[number, number]>> = [];
  let cur: Array<[number, number]> = [];
  for (const p of pts) { if (p == null) { if (cur.length) out.push(cur); cur = []; } else cur.push(p); }
  if (cur.length) out.push(cur);
  return out;
}
const d = (r: Array<[number, number]>) => r.map((p, j) => `${j ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
function Line2({ pts, base, colour, width, opacity = 1, area = 0 }: { pts: Array<[number, number] | null>; base: number; colour: string; width: number; opacity?: number; area?: number }) {
  return (
    <>
      {runs(pts).map((r, k) => (
        <g key={k}>
          {area > 0 && r.length > 1 && <path d={`${d(r)}L${r[r.length - 1][0]},${base}L${r[0][0]},${base}Z`} fill={colour} opacity={area} />}
          {r.length > 1 ? <path d={d(r)} fill="none" stroke={colour} strokeWidth={width} strokeOpacity={opacity} strokeLinejoin="round" /> : <circle cx={r[0][0]} cy={r[0][1]} r={2} fill={colour} />}
        </g>
      ))}
    </>
  );
}
const range = (v: Array<number | null>, room: number, zero = false): [number, number] => {
  const a = have(v);
  if (!a.length) return [0, 1];
  let lo = zero ? 0 : Math.min(...a), hi = Math.max(...a);
  const r = (hi - lo || 1) * room;
  if (!zero) lo -= r;
  hi += r;
  return [lo, hi];
};
const every = (rows: Line[], step: number): Array<[number, string]> => rows.map((r, i) => [i, dayMonth(r.d)] as [number, string]).filter(([i]) => (rows.length - 1 - i) % step === 0);

/** A small chart inside a tile: bars or a line with an optional band, no axis. */
function Spark({ vals, kind, colour, band, empty }: { vals: Array<number | null>; kind: "bars" | "line"; colour: string | ((i: number) => string); band?: Array<[number, number, number] | null>; empty: string }) {
  const v = have(vals);
  if (kind === "bars" ? !v.some((q) => q > 0) : v.length < 2) return <span className="sp-none">{v.length ? "One reading so far" : empty}</span>;
  const W = 300, H = 84;
  const [lo, hi] = range(band ? [...vals, ...band.flatMap((b) => (b ? [b[0], b[1]] : []))] : vals, 0.12, kind === "bars");
  const n = vals.length, Y = (q: number) => 4 + (1 - (q - lo) / (hi - lo || 1)) * (H - 8), X = (i: number) => (i / (n - 1)) * W;
  if (kind === "bars") {
    const slot = W / n, bw = Math.max(2, Math.min(16, slot * 0.66));
    return (
      <svg className="sp-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        {vals.map((q, i) => (q != null && q > 0 ? <rect key={i} x={i * slot + (slot - bw) / 2} y={Y(q)} width={bw} height={H - Y(q)} rx={2} fill={typeof colour === "function" ? colour(i) : colour} /> : null))}
      </svg>
    );
  }
  const top = band?.map((b, i) => (b ? [X(i), Y(b[1])] as [number, number] : null)).filter((p): p is [number, number] => p != null) ?? [];
  const bot = band?.map((b, i) => (b ? [X(i), Y(b[0])] as [number, number] : null)).filter((p): p is [number, number] => p != null).reverse() ?? [];
  const pts = vals.map((q, i) => (q == null ? null : ([X(i), Y(q)] as [number, number])));
  return (
    <svg className="sp-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      {top.length > 1 && <path d={`${d([...top, ...bot])}Z`} fill={GOOD} opacity={0.14} />}
      <Line2 pts={pts} base={H} colour={colour as string} width={1.8} area={0.2} />
    </svg>
  );
}

// ---------------------------------------------------------------- tiles

function Tile({ k, name, chip, fig, unit, hue, spark, facts, hover }: {
  k: string; name: string; chip: [string, string] | null; fig: ReactNode; unit?: string; hue?: string; spark?: ReactNode;
  facts: Array<[string, ReactNode, string?]>; hover: string;
}) {
  return (
    <div className={`sp-tile${hue ? ` ${hue}` : ""}`} data-tile={k} title={hover}>
      <div className="sp-th"><span className="sp-tn">{name}</span>{chip && <State tone={tone(chip[0])}>{chip[1]}</State>}</div>
      <div className="sp-fig num">{fig}{unit && <span className="sp-unit">{unit}</span>}</div>
      <div className="sp-sk">{spark}</div>
      <div className="sp-facts">{facts.map(([a, b, t]) => <span key={a}><i>{a}</i><b className={`num ${t ?? ""}`.trim()}>{b}</b></span>)}</div>
    </div>
  );
}

// ---------------------------------------------------------------- the page

export function SportsView({ m, onOpen }: { m: Model; onOpen: (a: GarminActivity) => void }) {
  const { line, wk, fit, rec, slp } = m;
  const stale = rec != null && rec.quiet > 1;
  const shown = line.slice(-182);

  // The headline: rest, go easy or train, and why. Each reason is one of the page's own findings.
  const flags: Array<{ tone: string; pre: string; em: string; post: string }> = [];
  if (rec?.side) flags.push({ tone: "warn", pre: "your ", em: `heart rate variability has been ${rec.side < 0 ? "below" : "above"} your normal range for ${plural(rec.streak, "day")}`, post: "" });
  if (fit?.pct != null && fit.pct < -30) flags.push({ tone: "bad", pre: "", em: "fatigue is far above your fitness", post: "" });
  if (slp && slp.avg != null && slp.need != null && slp.avg < slp.need) flags.push({ tone: "warn", pre: "you slept ", em: `${minT((slp.need - slp.avg) / 60)} less than you need`, post: ` each night ${stale ? "over the last 7 nights it read" : "this week"}` });
  const verdict = !rec ? "No night from the watch yet." : stale ? `Your watch has sent nothing since ${dayMonth(rec.row.d)}.` : flags.some((f) => f.tone === "bad") ? "Rest today." : flags.length ? "Go easy today." : "You are good to train today.";
  const d28r = fit && fit.d28 != null ? Math.round(fit.d28) : null;
  const flagNode = (f: (typeof flags)[number], i: number) => (
    <span key={i}>{i > 0 && (i === flags.length - 1 ? " and " : ", ")}{i === 0 && f.pre ? cap(f.pre) : f.pre}<em className={f.tone}>{i === 0 && !f.pre ? cap(f.em) : f.em}</em>{f.post}</span>
  );
  const why: ReactNode = flags.length ? <>{flags.map(flagNode)}.</>
    : rec && rec.side === 0 ? <>Your heart rate variability {stale ? "was" : "is"} <em className="good">inside your normal range</em>{stale ? " on the last night it read" : ""}.</>
    : rec && !rec.band ? "A normal range for your heart rate variability needs 14 nights." : null;
  const more = (
    <>
      {why}
      {fit && <>{" Fitness is "}<em className={d28r != null && d28r > 0 ? "good" : d28r != null && d28r < 0 ? "warn" : "pk"}>{`${Math.round(fit.fit)}${d28r == null ? "" : d28r === 0 ? ", level over four weeks" : `, ${d28r > 0 ? "up" : "down"} ${Math.abs(d28r)} in four weeks`}`}</em>.</>}
      {" This week: "}<em className="pk">{wk.n ? `${minT(wk.minAll)} in ${plural(wk.n, "session")}` : "no session yet"}</em>.
    </>
  );

  // What needs you: amber waits for Tamer; the share bar says how much.
  const stops: Array<{ title: string; detail: string; word: string; good: number; warn: number; cap: string }> = [];
  if (rec && rec.quiet > 1) {
    const q = Math.min(28, rec.quiet);
    stops.push({ word: "Behind", title: `The watch has sent nothing since ${dayMonth(rec.row.d)}`, detail: "Open Garmin Connect on your phone so the watch can sync. The numbers below stop at that day", good: 28 - q, warn: q, cap: `${rec.quiet} of the last 28 days missing` });
  }
  stops.push({ word: "Waits for you", title: "Water and weight are not logged", detail: "This page holds no water and no weight: the watch sync does not carry them. Weight comes from a Garmin scale or a typed weigh-in", good: 0, warn: 2, cap: "0 of 2 parts of this page" });

  const tiles = (
    <div className="sp-wall" data-testid="wall">
      {fit ? (
        <Tile k="fit" name="Fitness" hue={fit.d28 != null && fit.d28 > 0 ? "g" : fit.d28 != null && fit.d28 < 0 ? "w" : ""} fig={Math.round(fit.fit)} unit="load a day"
          chip={Math.round(fit.d28 ?? 0) > 0 ? ["good", "Rising"] : Math.round(fit.d28 ?? 0) < 0 ? ["warn", "Falling"] : ["dim", "Holding"]}
          spark={<Spark vals={line.slice(-84).map((r) => r.fit)} kind="line" colour={PINK} empty={WAIT} />}
          facts={[["Fatigue", Math.round(fit.tir)], ["Form", signed(Math.round(fit.form)), fit.zone[0] === "good" || fit.zone[0] === "bad" ? fit.zone[0] : ""], ["In 4 weeks", fit.d28 == null ? DASH : signed(Math.round(fit.d28)), fit.d28 != null && fit.d28 > 0 ? "good" : fit.d28 != null && fit.d28 < 0 ? "warn" : ""], ["VO2 max", m.vo2max ?? DASH]]}
          hover="Your training of the last six weeks as one number. It climbs slowly when you train more than you are used to. Fatigue is the same for the last week. Form is fitness minus fatigue: under zero you are carrying tiredness" />
      ) : (
        <Tile k="fit" name="Fitness" chip={["dim", "Nothing yet"]} fig={DASH} facts={[["VO2 max", m.vo2max ?? DASH]]} hover="Fitness is worked out from the heart rate of every session. None of the sessions read has one" />
      )}
      <Tile k="week" name="This week" hue={wk.im != null && m.goal != null && wk.im >= m.goal ? "g" : ""} fig={wk.n ? figMin(wk.minAll) : "0"} unit="trained"
        chip={wk.im == null || m.goal == null ? null : wk.im >= m.goal ? ["good", "Target met"] : ["data", `${m.goal - wk.im} min to go`]}
        spark={<Spark vals={Array.from({ length: 7 }, (_, i) => line.find((r) => r.d === `${addIso(m.thisMon, i)}`)?.min ?? null)} kind="bars" colour={PINK} empty="No session this week yet" />}
        facts={[["Sessions", wk.n], ["Active minutes", wk.im == null ? DASH : m.goal == null ? `${wk.im}` : `${wk.im} of ${m.goal}`], ["Strength days", `${wk.strength} of ${m.strengthGoal}`], ["Same days last week", m.lwSame.n ? minT(m.lwSame.min) : "none"]]}
        hover="Active minutes count a hard minute twice, as the watch does. The World Health Organization asks for 150 to 300 a week, and strength work on 2 days" />
      {rec && rec.hrv7 != null ? (
        <Tile k="rec" name="Recovery" hue={rec.side == null ? "" : rec.side === 0 ? "g" : "w"} fig={Math.round(rec.hrv7)} unit="ms"
          chip={rec.side == null ? ["dim", "No range yet"] : rec.side === 0 ? ["good", "In your range"] : ["warn", rec.side < 0 ? "Below your range" : "Above your range"]}
          spark={<Spark vals={line.slice(-28).map((r) => r.hrv7)} kind="line" colour={INK} band={line.slice(-28).map((r) => r.band)} empty={WAIT} />}
          facts={[["Your range", rec.band ? `${Math.round(rec.band[0])} to ${Math.round(rec.band[1])}` : DASH], ["Resting heart rate", rec.rhr7 == null ? DASH : Math.round(rec.rhr7)], ["Body battery", rec.bb ?? DASH], ["Stress", rec.stress ?? DASH]]}
          hover="Heart rate variability is the small change in time between heartbeats while you sleep. Near your own normal means rested. The figure is the average of the last 7 nights. Your range comes from the last 60" />
      ) : <Tile k="rec" name="Recovery" chip={["dim", "Nothing yet"]} fig={DASH} facts={[]} hover="Recovery needs four nights of heart rate variability from the watch" />}
      {slp ? (
        <Tile k="sleep" name="Sleep" hue={slp.need == null || slp.avg == null ? "" : slp.avg >= slp.need ? "g" : "w"} fig={figH(slp.last)} unit={stale ? `on ${dayMonth(slp.row.d)}` : "last night"}
          chip={slp.need == null || slp.avg == null ? null : slp.avg >= slp.need ? ["good", "Enough"] : ["warn", "Short"]}
          spark={<Spark vals={line.slice(-14).map((r) => (r.sleep == null ? null : r.sleep))} kind="bars" colour={(i) => { const r = line.slice(-14)[i]; return r.need == null || r.sleep == null ? PINK : r.sleep >= r.need ? GOOD : WARN; }} empty={WAIT} />}
          facts={[["7-night average", slp.avg == null ? DASH : hmS(slp.avg)], ["You need", slp.need == null ? DASH : hmS(slp.need)], ["Owed this week", slp.owed == null ? DASH : hmS(slp.owed), slp.owed != null && slp.owed > 0 ? "warn" : ""], ["Bedtime", slp.bed ?? DASH]]}
          hover="How long you slept, against what the watch says you need. Owed is what the last 7 nights fell short by" />
      ) : <Tile k="sleep" name="Sleep" chip={["dim", "Nothing yet"]} fig={DASH} facts={[]} hover="No night the watch read yet" />}
      {m.burn ? (
        <Tile k="burn" name="Calories burned" fig={tens(m.burn.avg)} unit="kcal a day"
          chip={m.burn.prev == null ? null : ["data", `${m.burn.avg >= m.burn.prev ? "Up" : "Down"} from ${tens(m.burn.prev)}`]}
          spark={<Spark vals={m.burn.days} kind="bars" colour={PINK} empty={WAIT} />}
          facts={[["Today so far", m.burn.today == null ? DASH : whole(m.burn.today)], ["Yesterday", whole(m.burn.yest)], ["By moving", tens(m.burn.move)], ["At rest", tens(m.burn.rest)]]}
          hover={BURN_HOVER} />
      ) : <Tile k="burn" name="Calories burned" chip={["dim", "Nothing yet"]} fig={DASH} facts={[]} hover={BURN_HOVER} />}
      <Tile k="body" name="Body" chip={["dim", "Nothing logged"]} fig={DASH} facts={[]} hover="The watch sync holds no weight, body fat or water. They come from a Garmin scale or a typed weigh-in" />
    </div>
  );

  return (
    <>
      <Say lead={verdict} more={more} />
      <div className="sp-stack">
        <Block title="Needs you" state={<State tone="warn">{`${stops.length} waiting`}</State>}>
          <div className="sp-stops" data-testid="stops">
            {stops.map((s) => (
              <div key={s.title} className="sp-stop">
                <span className="sp-sw"><State tone="warn">{s.word}</State></span>
                <span className="sp-sx"><b>{s.title}</b><span>{s.detail}</span></span>
                <span className="sp-sb">
                  <span className="sp-share">{s.good > 0 && <i style={{ flex: `${s.good} 1 0`, background: GOOD }} />}{s.warn > 0 && <i style={{ flex: `${s.warn} 1 0`, background: WARN }} />}</span>
                  <small>{s.cap}</small>
                </span>
              </div>
            ))}
          </div>
        </Block>
        <Block title="Summary">{tiles}</Block>
        <FitBlock m={m} shown={shown} />
        <WeeksBlock m={m} />
        <NightsBlock m={m} />
        <SessionsBlock m={m} onOpen={onOpen} />
      </div>
    </>
  );
}

function addIso(iso: string, n: number): string {
  return new Date(Date.parse(`${iso}T12:00:00Z`) + n * 86400000).toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- fitness, fatigue and form

function FitBlock({ m, shown }: { m: Model; shown: Line[] }) {
  const { fit } = m;
  if (!fit) {
    return <Block title="Fitness, fatigue and form"><p className="empty">Fitness is worked out from the heart rate of every session, and none of the sessions read has one.</p></Block>;
  }
  const n = shown.length;
  const [lo, hi] = range([...shown.map((r) => r.fit), ...shown.map((r) => r.tir)], 0.1);
  const [flo, fhi] = range([...shown.map((r) => r.form), 0], 0.1);
  const tipF = (i: number): Tip => {
    const r = shown[i];
    const rows: Array<[string, string]> = [["Fitness", String(Math.round(r.fit as number))], ["Fatigue", String(Math.round(r.tir as number))], ["Form", signed(Math.round(r.form as number))],
      ["Load that day", r.sess.length ? String(Math.round(r.load)) : "rest day"], ...r.sess.map((s): [string, string] => [activityName(s.a), minT(s.min)])];
    return { label: `${dayMonth(r.d)}: ${rows.map((x) => x.join(" ")).join(", ")}`, head: dayMonth(r.d), rows };
  };
  const zc = (r: Line) => { const p = r.fit ? ((r.form as number) / r.fit) * 100 : 0; return p < -30 ? BAD : p < -10 ? GOOD : p <= 5 ? SOFT : PINK; };
  return (
    <Block title="Fitness, fatigue and form" state={<State tone={tone(fit.zone[0])}>{fit.zone[1]}</State>}>
      <Plot testid="fit-chart" n={n} h={300} lo={lo} hi={hi} ticks={niceTicks(lo, hi)} yf={(v) => String(v)} xl={every(shown, 28)} tip={tipF}
        draw={(q) => (
          <>
            <Line2 pts={shown.map((r, i) => [q.X(i), q.Y(r.fit as number)])} base={q.base} colour={PINK} width={2.6} area={0.2} />
            <Line2 pts={shown.map((r, i) => [q.X(i), q.Y(r.tir as number)])} base={q.base} colour={INK} width={1.2} opacity={0.6} />
          </>
        )} />
      <div className="sp-key"><span><i style={{ background: PINK }} />Fitness, the last six weeks</span><span><i style={{ background: INK }} />Fatigue, the last week</span></div>
      <h3 className="sp-h3">Form</h3>
      <Plot testid="form-chart" n={n} h={150} bars lo={flo} hi={fhi} ticks={niceTicks(flo, fhi, 3)} yf={(v) => signed(v)} xl={every(shown, 28)} tip={tipF}
        draw={(q) => {
          const y0 = q.Y(0), bw = Math.max(1.5, q.slot * 0.72);
          return <>{shown.map((r, i) => { const y = q.Y(r.form as number); return <rect key={i} x={q.X(i) - bw / 2} y={Math.min(y, y0)} width={bw} height={Math.max(1, Math.abs(y - y0))} fill={zc(r)} opacity={q.hover !== null && q.hover !== i ? 0.4 : 0.85} />; })}</>;
        }} />
      <div className="sp-key"><span><i style={{ background: PINK }} />Fresh</span><span><i style={{ background: SOFT }} />Holding</span><span><i style={{ background: GOOD }} />Building</span><span><i style={{ background: BAD }} />Too much</span></div>
      <p className="sp-note">Fatigue above fitness means you are training harder than you are used to. That is how fitness grows, as long as form does not stay deep in the red.</p>
    </Block>
  );
}

// ---------------------------------------------------------------- training by week

const WK = { all: "All sports", Running: "Running", Cycling: "Cycling", Swimming: "Swimming", "Gym and other": "Gym and other" } as const;
type WKey = keyof typeof WK;

function Chips<T extends string>({ items, value, onPick, label }: { items: Array<[T, string]>; value: T; onPick: (v: T) => void; label: string }) {
  return (
    <span className="sp-chips" role="group" aria-label={label}>
      {items.map(([k, t]) => <button key={k} type="button" className={k === value ? "on" : undefined} aria-pressed={k === value} onClick={() => onPick(k)}>{t}</button>)}
    </span>
  );
}

function WeeksBlock({ m }: { m: Model }) {
  const [pick, setPick] = useState<WKey>("all");
  const { weeks } = m;
  const unit = pick === "Running" || pick === "Cycling" || pick === "Swimming" ? "km" : "h";
  const val = (w: (typeof weeks)[number]) => (pick === "all" ? w.minAll / 60 : unit === "km" ? w.km[pick as "Running"] : w.min[pick as "Gym and other"] / 60);
  const say = (v: number) => (unit === "km" ? `${v.toFixed(1)} km` : hmS(v * 3600));
  const cnt = (w: (typeof weeks)[number]) => (pick === "all" ? w.n : w.cnt[pick as "Running"]);
  const vals = weeks.map(val);
  const hi = Math.max(...vals, 0) * 1.1 || 1;
  const none = !weeks.some((w) => cnt(w));
  const full = weeks.slice(0, -1).map(val);
  const a4 = avg(full.slice(-4)), p4 = avg(full.slice(-8, -4));
  return (
    <Block title="Training by week" right={<Chips items={Object.entries(WK) as Array<[WKey, string]>} value={pick} onPick={setPick} label="Sport" />}>
      {none ? <p className="empty">{`No ${pick === "all" ? "session" : WK[pick].toLowerCase()} in the last ${weeks.length} weeks.`}</p> : (
        <div>
          <Plot testid="weeks-chart" n={weeks.length} h={280} bars lo={0} hi={hi} ticks={niceTicks(0, hi)} yf={(v) => `${v} ${unit}`}
            xl={weeks.map((w, i) => [i, dayMonth(w.w)] as [number, string]).filter(([i]) => (weeks.length - 1 - i) % 4 === 0)}
            tip={(i): Tip => {
              const w = weeks[i];
              const rows: Array<[string, string]> = [[pick === "all" ? "All sports" : WK[pick], say(vals[i])], ["Sessions", String(cnt(w))],
                ...(pick === "all" ? SPORTS.filter((s) => w.cnt[s]).map((s): [string, string] => [s, `${minT(w.min[s])}${w.km[s] ? `, ${w.km[s].toFixed(1)} km` : ""}`]) : []),
                ...(w.load ? ([["Load", String(Math.round(w.load))]] as Array<[string, string]>) : [])];
              const head = `Week of ${dayMonth(w.w)}${w.w === m.thisMon ? ", so far" : ""}`;
              return { label: `${head}: ${rows.map((x) => x.join(" ")).join(", ")}`, head, rows };
            }}
            draw={(q) => {
              const bw = Math.max(4, Math.min(34, q.slot * 0.66));
              return <><defs><linearGradient id="sp-wk-p" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={PINK} stopOpacity=".95" /><stop offset="1" stopColor={PINK} stopOpacity=".45" /></linearGradient><linearGradient id="sp-wk-w" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={INK} stopOpacity=".95" /><stop offset="1" stopColor={INK} stopOpacity=".4" /></linearGradient></defs>{weeks.map((w, i) => { const y = q.Y(vals[i]); return <rect key={w.w} data-week={i} x={q.X(i) - bw / 2} y={y} width={bw} height={Math.max(0, q.base - y)} rx={4} fill={w.w === m.thisMon ? "url(#sp-wk-w)" : "url(#sp-wk-p)"} opacity={q.hover !== null && q.hover !== i ? 0.4 : 1} />; })}</>;
            }} />
          <p className="sp-note">{`Each bar is one week, Monday to Sunday. The white one is this week so far. The last four full weeks averaged ${a4 == null ? DASH : say(a4)}, the four before them ${p4 == null ? DASH : say(p4)}.`}</p>
        </div>
      )}
    </Block>
  );
}

// ---------------------------------------------------------------- recovery by night

const NV = { hrv: "Heart rate variability", rhr: "Resting heart rate", sleep: "Sleep" } as const;
type NKey = keyof typeof NV;

function NightsBlock({ m }: { m: Model }) {
  const [pick, setPick] = useState<NKey>("hrv");
  const { rec } = m;
  const all60 = m.line.slice(-60);
  const key = pick === "sleep" ? "sleep" : pick;
  const firstI = all60.findIndex((r) => r[key] != null);
  const nights = firstI < 0 ? [] : all60.slice(firstI);
  const n = nights.length;
  const quiet = (r: Line): Tip => ({ label: `${dayMonth(r.d)}: the watch sent nothing for this night`, head: dayMonth(r.d), rows: [["", "The watch sent nothing for this night"]] });
  const step = n > 40 ? 14 : 7;
  let body: ReactNode, keyRow: ReactNode = null, note = "";
  if (firstI < 0) body = <p className="empty">The watch has sent no night yet.</p>;
  else if (pick === "hrv") {
    const [lo, hi] = range([...nights.map((r) => r.hrv), ...nights.flatMap((r) => (r.band ? [r.band[0], r.band[1]] : []))], 0.1);
    body = (
      <Plot testid="nights-chart" n={n} h={300} lo={lo} hi={hi} ticks={niceTicks(lo, hi)} yf={(v) => `${v} ms`} xl={every(nights, step)}
        tip={(i): Tip => {
          const r = nights[i];
          if (r.hrv == null) return quiet(r);
          const rows: Array<[string, string]> = [["That night", `${r.hrv} ms`], ...(r.hrv7 != null ? ([["7-night average", `${Math.round(r.hrv7)} ms`]] as Array<[string, string]>) : []),
            ["Your normal range", r.band ? `${Math.round(r.band[0])} to ${Math.round(r.band[1])} ms` : "needs 14 nights"]];
          return { label: `Night to ${dayMonth(r.d)}: ${rows.map((x) => x.join(" ")).join(", ")}`, head: `Night to ${dayMonth(r.d)}`, rows };
        }}
        draw={(q) => {
          const top: Array<[number, number]> = [], bot: Array<[number, number]> = [];
          nights.forEach((r, i) => { if (r.band) { top.push([q.X(i), q.Y(r.band[1])]); bot.push([q.X(i), q.Y(r.band[0])]); } });
          return (
            <>
              {top.length > 1 && <path d={`${d([...top, ...bot.reverse()])}Z`} fill={GOOD} opacity={0.13} />}
              <Line2 pts={nights.map((r, i) => (r.hrv == null ? null : [q.X(i), q.Y(r.hrv)]))} base={q.base} colour={PINK} width={1.4} opacity={0.7} />
              <Line2 pts={nights.map((r, i) => (r.hrv7 == null ? null : [q.X(i), q.Y(r.hrv7)]))} base={q.base} colour={INK} width={2.4} />
            </>
          );
        }} />
    );
    keyRow = <><span><i style={{ background: PINK }} />One night</span><span><i style={{ background: INK }} />7-night average</span><span><i style={{ background: GOOD }} />Your normal range</span></>;
    const b = rec?.band;
    note = b ? `The green band is your own normal: the average of the last ${b[2]} nights, give or take half their spread${b[2] < 60 ? `. It firms up at 60 nights, and ${b[2]} are in` : ""}. When the white line leaves the band, low or high, take an easy day.` : "A normal range needs 14 nights of readings.";
  } else if (pick === "rhr") {
    const [lo, hi] = range(nights.map((r) => r.rhr), 0.15);
    body = (
      <Plot testid="nights-chart" n={n} h={300} lo={lo} hi={hi} ticks={niceTicks(lo, hi)} yf={(v) => `${v} bpm`} xl={every(nights, step)}
        tip={(i): Tip => {
          const r = nights[i];
          if (r.rhr == null) return quiet(r);
          const rows: Array<[string, string]> = [["Resting heart rate", `${r.rhr} bpm`], ...(r.rhr7 != null ? ([["7-day average", `${Math.round(r.rhr7)} bpm`]] as Array<[string, string]>) : [])];
          return { label: `${dayMonth(r.d)}: ${rows.map((x) => x.join(" ")).join(", ")}`, head: dayMonth(r.d), rows };
        }}
        draw={(q) => (
          <>
            <Line2 pts={nights.map((r, i) => (r.rhr == null ? null : [q.X(i), q.Y(r.rhr)]))} base={q.base} colour={PINK} width={1.4} opacity={0.7} />
            <Line2 pts={nights.map((r, i) => (r.rhr7 == null ? null : [q.X(i), q.Y(r.rhr7)]))} base={q.base} colour={INK} width={2.4} />
          </>
        )} />
    );
    keyRow = <><span><i style={{ background: PINK }} />One morning</span><span><i style={{ background: INK }} />7-day average</span></>;
    note = `Beats a minute at rest. Lower is calmer. A rise of several beats that lasts, together with low heart rate variability, is an early sign of tiredness or illness.${rec && rec.rhr7 != null && rec.rhr28 != null ? ` Yours: ${Math.round(rec.rhr7)} over the last 7 days, ${Math.round(rec.rhr28)} over the last 28.` : ""}`;
  } else {
    const hrs = nights.map((r) => (r.sleep == null ? null : r.sleep / 3600)), needs = nights.map((r) => (r.need == null ? null : r.need / 3600));
    const hi = Math.max(...have(hrs), ...have(needs)) * 1.08;
    body = (
      <Plot testid="nights-chart" n={n} h={300} bars lo={0} hi={hi} ticks={niceTicks(0, hi)} yf={(v) => `${v} h`} xl={every(nights, step)}
        tip={(i): Tip => {
          const r = nights[i];
          if (r.sleep == null) return quiet(r);
          const rows: Array<[string, string]> = [["Slept", hmS(r.sleep)], ...(r.need != null ? ([["You need", hmS(r.need)]] as Array<[string, string]>) : [])];
          return { label: `Night to ${dayMonth(r.d)}: ${rows.map((x) => x.join(" ")).join(", ")}`, head: `Night to ${dayMonth(r.d)}`, rows };
        }}
        draw={(q) => {
          const bw = Math.max(3, Math.min(18, q.slot * 0.66));
          return (
            <>
              {nights.map((r, i) => (hrs[i] == null ? null : <rect key={r.d} x={q.X(i) - bw / 2} y={q.Y(hrs[i] as number)} width={bw} height={Math.max(0, q.base - q.Y(hrs[i] as number))} rx={2} fill={r.need == null ? PINK : (r.sleep as number) >= r.need ? GOOD : WARN} opacity={q.hover !== null && q.hover !== i ? 0.4 : 0.9} />))}
              <Line2 pts={nights.map((_r, i) => (needs[i] == null ? null : [q.X(i), q.Y(needs[i] as number)]))} base={q.base} colour={INK} width={1.2} opacity={0.6} />
            </>
          );
        }} />
    );
    keyRow = <><span><i style={{ background: GOOD }} />Enough</span><span><i style={{ background: WARN }} />Short</span><span><i style={{ background: INK }} />What you need</span></>;
    note = "Each bar is one night the watch read. The white line is the sleep the watch says you need.";
  }
  return (
    <Block title="Recovery by night" right={<Chips items={Object.entries(NV) as Array<[NKey, string]>} value={pick} onPick={setPick} label="Reading" />}>
      <div>{body}</div>
      {keyRow && <div className="sp-key">{keyRow}</div>}
      {note && <p className="sp-note">{note}</p>}
    </Block>
  );
}

// ---------------------------------------------------------------- every session

const GROUP = ["All", ...SPORTS] as const;
function paceOf(s: Sess): string | null {
  const a = s.a;
  if (!a.distance_m || !a.duration_sec) return null;
  if (s.sport === "Running" || /walking|hiking/.test(a.type_key ?? "")) return `${pace(a.duration_sec / 60 / (a.distance_m / 1000))} /km`;
  if (s.sport === "Swimming") return `${pace(a.duration_sec / 60 / (a.distance_m / 100))} /100 m`;
  if (s.sport === "Cycling") return `${(a.distance_m / 1000 / (a.duration_sec / 3600)).toFixed(1)} km/h`;
  return null;
}

function SessionsBlock({ m, onOpen }: { m: Model; onOpen: (a: GarminActivity) => void }) {
  const [find, setFind] = useState("");
  const [sport, setSport] = useState<(typeof GROUP)[number]>("All");
  const [limit, setLimit] = useState(12);
  const all = [...m.sess].reverse();
  const q = find.trim().toLowerCase();
  const rows = all.filter((s) => (sport === "All" || s.sport === sport) && (!q || activityName(s.a).toLowerCase().includes(q) || s.sport.toLowerCase().includes(q)));
  const shownRows = rows.slice(0, limit);
  const total = rows.reduce((t, s) => t + s.min * 60, 0);
  return (
    <Block title="All sessions" state={<State tone="run">{String(all.length)}</State>}>
      <div className="sp-bar">
        <input className="sp-find" type="search" placeholder="Search sessions" aria-label="Search sessions" value={find} onChange={(e) => { setFind(e.target.value); setLimit(12); }} />
        <Chips items={GROUP.map((g) => [g, g] as [(typeof GROUP)[number], string])} value={sport} onPick={(v) => { setSport(v); setLimit(12); }} label="Sport" />
      </div>
      {rows.length === 0 ? <p className="empty">No session matches.</p> : (
        <table className="sp-tbl" data-testid="sessions">
          <thead><tr><th>Session</th><th className="x-w">Sport</th><th>Day</th><th className="r">Distance</th><th className="r">Time</th><th className="r x-w">Pace or speed</th><th className="r x-w">Heart rate</th><th className="r x-w">Hard minutes</th><th className="r x-w">Load</th></tr></thead>
          <tbody>
            {shownRows.map((s) => (
              <tr key={s.a.activity_id} data-act={s.a.activity_id} tabIndex={0} onClick={() => onOpen(s.a)} onKeyDown={(e) => { if (e.key === "Enter") onOpen(s.a); }}>
                <td title={s.a.name ?? undefined}>{activityName(s.a)}</td>
                <td className="x-w">{s.sport}</td>
                <td className="num">{`${Number(s.day.slice(8, 10))} ${dayMonth(s.day).split(" ")[1]} ${s.day.slice(0, 4)}`}</td>
                <td className="num r">{s.km > 0 ? `${s.km.toFixed(2)} km` : <span title="No distance for this session">-</span>}</td>
                <td className="num r">{s.a.duration_sec == null ? "-" : minOrHm(s.a.duration_sec)}</td>
                <td className="num r x-w">{paceOf(s) ?? <span title="No pace for this kind of session">-</span>}</td>
                <td className="num r x-w">{s.a.avg_hr == null ? <span title="The watch gave no heart rate">-</span> : `${Math.round(s.a.avg_hr)} bpm`}</td>
                <td className="num r x-w"><span title="The watch sync holds no time in each heart rate zone">-</span></td>
                <td className="num r x-w">{s.load == null ? <span title="A load needs the session's average heart rate">-</span> : Math.round(s.load)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="sp-more">
        <span className="num">{`${Math.min(limit, rows.length)} of ${rows.length} shown, ${hmS(total)} in all`}</span>
        {rows.length > limit && <button type="button" className="btn" onClick={() => setLimit(limit + 48)}>{`Show ${Math.min(48, rows.length - limit)} more`}</button>}
      </div>
    </Block>
  );
}

