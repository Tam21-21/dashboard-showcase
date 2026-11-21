import { memo, useMemo, useRef, useState } from "react";
import { lineTicks } from "../../../kit/charts";
import { useBoxSize } from "../../../kit";
import { fx } from "../markets/data";
import { hourLabel } from "./names";

// One line, large, on its two scales (mockup: lkLine and timeTicks, frozen mockup of 2026-11-18): the price on the right, the time
// along the bottom. The closes (or the hourly prices, hours outside the regular session drawn faint), the thin 200-day average
// on the daily views, a dot at the newest value. The pointer moves only the cross, its dot and the two tags on the scales.
// Display data only. SVG, no canvas.

const GOOD = "#2fe08a";
const BAD = "#ff5a63";

export type LinePoint = { time: string; close: number; regular: boolean };

const NY_DAY = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" });
const PARIS_END = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", weekday: "short", day: "numeric", hour: "2-digit", hourCycle: "h23" });

type Tick = [number, string];

/** The time scale: the finest step whose labels do not touch. An hourly view starts a new day at a new New York trading date,
 * so a Friday's late hours (past midnight in Paris) stay under that Friday. */
export function timeTicks(points: LinePoint[], hourly: boolean, X: (i: number) => number, PW: number): Tick[] {
  const pickAt = (keep: (i: number) => boolean, label: (i: number) => string): Tick[] =>
    points.map((_p, i): Tick | null => (i > 0 && keep(i) ? [i, label(i)] : null)).filter((t): t is Tick => t !== null);
  let levels: Array<[(i: number) => boolean, (i: number) => string]>;
  if (hourly) {
    const ends = points.map((p) => Object.fromEntries(PARIS_END.formatToParts(new Date(Date.parse(p.time) + 3_600_000)).map((x) => [x.type, x.value])));
    const days = points.map((p) => NY_DAY.format(new Date(p.time)));
    const newDay = (i: number) => days[i] !== days[i - 1];
    const dayLabel = (i: number) => `${ends[i].weekday} ${ends[i].day}`;
    levels = [
      ...[1, 2, 3, 6].map((k): [(i: number) => boolean, (i: number) => string] => [
        (i) => newDay(i) || Number(ends[i].hour) % k === 0,
        (i) => (newDay(i) ? dayLabel(i) : `${Number(ends[i].hour)}:00`),
      ]),
      [newDay, dayLabel],
    ];
  } else {
    const d = points.map((p) => new Date(`${p.time}T12:00:00Z`));
    const y = (i: number) => d[i].getUTCFullYear();
    const m = (i: number) => d[i].getUTCMonth();
    const newYear = (i: number) => y(i) !== y(i - 1);
    const newMonth = (i: number) => m(i) !== m(i - 1);
    const yl = (i: number) => String(y(i));
    const mon = (i: number) => (m(i) === 0 ? yl(i) : d[i].toLocaleString("en-GB", { month: "short", timeZone: "UTC" }));
    levels = [
      [(i) => d[i].getUTCDay() < d[i - 1].getUTCDay(), (i) => d[i].toLocaleString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })],
      [newMonth, mon],
      [(i) => newMonth(i) && m(i) % 3 === 0, mon],
      [newYear, yl],
      ...[2, 5, 10].map((k): [(i: number) => boolean, (i: number) => string] => [(i) => newYear(i) && y(i) % k === 0, yl]),
    ];
  }
  const room = (a: Tick, b: Tick) => X(b[0]) - X(a[0]) >= ((a[1].length + b[1].length) / 2) * 7.4 + 18;
  for (const [keep, label] of levels) {
    const t = pickAt(keep, label).filter(([i]) => X(i) > 18 && X(i) < PW - 18);
    if (t.every((c, j) => j === 0 || room(t[j - 1], c))) return t;
  }
  return [];
}

/** The digits a price scale needs so its step reads exactly (a step of 2.5 needs one, a step of 5 none). */
function decimalsOf(step: number): number {
  const e = Math.floor(Math.log10(step));
  return Math.max(0, -e + (Number.isInteger(Math.round((step / 10 ** e) * 1e6) / 1e6) ? 0 : 1));
}

const BOTTOM = 30;

type Frame = { W: number; PW: number; H: number; X: (i: number) => number; Y: (v: number) => number };

const Still = memo(function Still({ f, points, avg, hourly, tone }: { f: Frame; points: LinePoint[]; avg: Array<number | null> | undefined; hourly: boolean; tone: string }) {
  const { W, PW, H, X, Y } = f;
  const n = points.length;
  const v = points.map((p) => p.close);
  const path = (arr: Array<number | null>, keep?: (i: number) => boolean) => {
    let d = "";
    let pen = false;
    arr.forEach((x, i) => {
      if (x == null || (keep && !keep(i))) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)} ${Y(x).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const all = [...v, ...(avg ?? []).filter((x): x is number => x != null)];
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const pad = (hi - lo || 1) * 0.06;
  const ys = lineTicks(lo - pad, hi + pad, 4);
  const dec = ys.length > 1 ? decimalsOf(ys[1] - ys[0]) : 2;
  const ticks = timeTicks(points, hourly, X, PW);
  const main = path(v, hourly ? (i) => points[i].regular : undefined);
  const fill = `${main || path(v)}L${X(n - 1).toFixed(1)} ${H}L${X(0).toFixed(1)} ${H}Z`;
  return (
    <>
      <defs>
        <linearGradient id="lk-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={tone} stopOpacity=".4" /><stop offset="1" stopColor={tone} stopOpacity="0" /></linearGradient>
      </defs>
      <g className="lk-gl">
        {ys.map((t) => <line key={t} x1="0" x2={PW} y1={Y(t).toFixed(1)} y2={Y(t).toFixed(1)} />)}
        <line className="base" x1="0" x2={PW} y1={H} y2={H} />
        {ticks.map(([i]) => <line key={i} className="tk" x1={X(i).toFixed(1)} x2={X(i).toFixed(1)} y1={H} y2={H + 5} />)}
      </g>
      {!hourly && <path d={fill} fill="url(#lk-fill)" stroke="none" />}
      {avg && <path className="avg" d={path(avg)} />}
      {hourly && <path className="ln lk-x" d={path(v)} style={{ color: tone }} />}
      <path className="ln lk-main" d={main} style={{ color: tone }} />
      <circle className="end" cx={X(n - 1)} cy={Y(v[n - 1])} r="4.5" style={{ color: tone }} />
      <g className="lk-ys">{ys.map((t) => <text key={t} x={W - 4} y={Y(t) + 4} textAnchor="end">{fx(t, dec)}</text>)}</g>
      <g className="lk-xs">{ticks.map(([i, l]) => <text key={i} x={X(i)} y={H + 22} textAnchor="middle">{l}</text>)}</g>
    </>
  );
});

export default function PriceLine({ points, avg, hourly, height }: { points: LinePoint[]; avg?: Array<number | null>; hourly: boolean; height: number }) {
  const box = useRef<HTMLDivElement>(null);
  const { w } = useBoxSize(box);
  const W = w > 0 ? w : 600;
  const G = W < 720 ? 52 : 64;
  const PW = W - G;
  const H = height;
  const n = points.length;
  const [hov, setHov] = useState<number | null>(null);
  const first = points[0].close;
  const last = points[n - 1].close;
  const tone = last >= first ? GOOD : BAD;
  const frame = useMemo<Frame>(() => {
    const vals = [...points.map((p) => p.close), ...(avg ?? []).filter((x): x is number => x != null)];
    const lo0 = Math.min(...vals);
    const hi0 = Math.max(...vals);
    const pad = (hi0 - lo0 || 1) * 0.06;
    const lo = lo0 - pad;
    const hi = hi0 + pad;
    return { W, PW, H, X: (i) => (n > 1 ? (i / (n - 1)) * PW : PW), Y: (x) => 6 + (1 - (x - lo) / (hi - lo)) * (H - 12) };
  }, [points, avg, W, PW, H, n]);

  const at = hov !== null ? points[hov] : null;
  const x = hov !== null ? frame.X(hov) : 0;
  const y = at ? frame.Y(at.close) : 0;
  const when = at ? (hourly ? hourLabel(at.time, at.regular) : at.time) : "";
  const tagW = when.length * 7.4 + 14;
  return (
    <div
      ref={box} className="lk-chart" data-testid="lk-chart" style={{ height: H + BOTTOM }}
      onPointerMove={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        setHov(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / PW) * (n - 1)))));
      }}
      onPointerLeave={() => setHov(null)}
    >
      <svg className="lk-svg" viewBox={`0 0 ${W} ${H + BOTTOM}`} width={W} height={H + BOTTOM} aria-hidden="true">
        <Still f={frame} points={points} avg={avg} hourly={hourly} tone={tone} />
        {at && (
          <g className="lk-hv">
            <line className="lk-vl" x1={x} x2={x} y1="0" y2={H} />
            <circle className="lk-dt" cx={x} cy={y} r="5.5" style={{ fill: tone }} />
            <rect className="lk-tag" x={W - G + 2} y={y - 10} width={G - 2} height="20" rx="6" />
            <text className="lk-tagt" x={W - G + 2 + (G - 2) / 2} y={y + 4} textAnchor="middle">{fx(at.close, 2)}</text>
            <rect className="lk-tag" x={Math.max(0, Math.min(PW - tagW, x - tagW / 2))} y={H + 4} width={tagW} height="20" rx="6" />
            <text className="lk-tagt" x={Math.max(0, Math.min(PW - tagW, x - tagW / 2)) + tagW / 2} y={H + 18} textAnchor="middle">{when}</text>
          </g>
        )}
      </svg>
    </div>
  );
}
