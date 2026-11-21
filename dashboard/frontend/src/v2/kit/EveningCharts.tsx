import { useEffect, useRef } from "react";
import { useBoxSize } from "./charts";
import { useUnveil } from "./motion";

// The two small charts of the evening Summary (mockup 2026-11-19, lab-summary.js burnChart and stepRing), drawn as SVG so
// their words can be read and tested. Colours come from the look's variables; each comes in once, on first sight.

export type BurnDay = { d: string; rest: number | null; move: number | null };

const LONG = (d: string) => new Date(Date.parse(`${d}T12:00:00Z`)).toLocaleDateString("en-GB", { timeZone: "UTC", weekday: "long", day: "numeric", month: "long" }).replace(/,/g, "");
const WHOLE = (n: number) => Math.round(n).toLocaleString("en-US");
const DRAWN_WIDTH = 300, DRAWN_HEIGHT = 148; // the size a column set is laid out for before the box has been measured

/** Calories: one column a day, the last one (the day itself) in white. The pale part is burned at rest, the bright part by moving; the dashed line is the average of the days before. */
export function BurnColumns({ days, avg }: { days: BurnDay[]; avg: number | null }) {
  const ref = useRef<HTMLDivElement>(null);
  useUnveil(ref, "y");
  const box = useBoxSize(ref);
  const n = days.length;
  const W = box.w || DRAWN_WIDTH, H = box.h || DRAWN_HEIGHT;
  const top = Math.max(...days.map((d) => (d.rest ?? 0) + (d.move ?? 0)), avg ?? 0) * 1.06 || 1;
  const slot = W / n, bw = Math.max(8, Math.min(28, slot * 0.6)), base = H - 1;
  const Y = (v: number) => base - (v / top) * (base - 4);
  return (
    <div className="kz-burn" ref={ref}>
      <svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Calories burned, one column a day">
        <defs>
          {days.map((d, i) => {
            if (d.rest == null || d.move == null) return null;
            const yTop = Y(d.rest + d.move), cut = (Y(d.rest) - yTop) / Math.max(1, base - yTop);
            const col = i === n - 1 ? "var(--bright)" : "var(--b)";
            return (
              <linearGradient key={d.d} id={`kzb-${i}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset={0} stopColor={col} stopOpacity={0.98} />
                <stop offset={Math.max(0, cut - 0.05)} stopColor={col} stopOpacity={0.9} />
                <stop offset={Math.min(1, cut + 0.05)} stopColor={col} stopOpacity={0.46} />
                <stop offset={1} stopColor={col} stopOpacity={0.34} />
              </linearGradient>
            );
          })}
        </defs>
        {days.map((d, i) => {
          const x = i * slot + (slot - bw) / 2, r = Math.min(6, bw / 2);
          const today = i === n - 1 ? { "data-today": "" } : {};
          if (d.rest == null || d.move == null) {
            return (
              <g key={d.d} className="kz-col" data-col={d.d} {...today}>
                <title>{`${LONG(d.d)}: the watch has not sent this day`}</title>
                <rect x={x + 0.5} y={base - 30.5} width={bw - 1} height={30} rx={4} fill="none" stroke="var(--soft)" strokeOpacity={0.4} strokeDasharray="3 3" />
              </g>
            );
          }
          const yTop = Y(d.rest + d.move), bot = base, h = bot - yTop;
          const path = `M${x},${bot} V${yTop + r} Q${x},${yTop} ${x + r},${yTop} H${x + bw - r} Q${x + bw},${yTop} ${x + bw},${yTop + r} V${bot} Z`;
          return (
            <g key={d.d} className="kz-col" data-col={d.d} {...today}>
              <title>{`${LONG(d.d)}: ${WHOLE(d.rest + d.move)} kcal burned, ${WHOLE(d.rest)} at rest, ${WHOLE(d.move)} by moving`}</title>
              <path d={path} fill={`url(#kzb-${i})`} data-h={Math.round(h)} />
            </g>
          );
        })}
        {avg != null && (
          <line className="kz-avg" data-avg={Math.round(avg)} x1={0} x2={W} y1={Math.round(Y(avg)) + 0.5} y2={Math.round(Y(avg)) + 0.5}>
            <title>{`Average of the days before: ${WHOLE(avg)} kcal`}</title>
          </line>
        )}
      </svg>
    </div>
  );
}

/** Steps: one large ring. A full ring is the goal; past it a second, lighter lap starts, so a big day still shows. */
export function StepRing({ steps, goal }: { steps: number; goal: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useUnveil(ref, "fade");
  const share = steps / goal, reached = steps >= goal;
  const S = 150, lw = 16, r = S / 2 - lw / 2 - 2, c = S / 2;
  return (
    <div className="kz-ring" ref={ref}>
      <svg viewBox={`0 0 ${S} ${S}`} role="img" aria-label={`${WHOLE(steps)} steps of ${WHOLE(goal)}`}>
        <circle className="kz-ring-track" cx={c} cy={c} r={r} fill="none" strokeWidth={lw} />
        {share > 0.004 && (
          <circle className={`kz-ring-arc ${reached ? "good" : "warn"}`} cx={c} cy={c} r={r} fill="none" strokeWidth={lw} strokeLinecap="round"
            pathLength={1} strokeDasharray={`${Math.min(1, share)} 1`} transform={`rotate(-90 ${c} ${c})`} />
        )}
        {share > 1.004 && (
          <circle className={`kz-ring-lap ${reached ? "good" : "warn"}`} cx={c} cy={c} r={r} fill="none" strokeWidth={lw} strokeLinecap="round"
            pathLength={1} strokeDasharray={`${Math.min(1, share - 1)} 1`} transform={`rotate(-90 ${c} ${c})`} />
        )}
        <text className="kz-ring-pct" x={c} y={c - 8} textAnchor="middle" dominantBaseline="middle">{`${Math.round(share * 100)}%`}</text>
        <text className="kz-ring-of" x={c} y={c + 21} textAnchor="middle" dominantBaseline="middle">of your goal</text>
      </svg>
    </div>
  );
}

const EURO = (n: number) => `${n < 0 ? "−" : ""}€${Math.abs(n).toFixed(2)}`;
const STAIR_HEIGHT = 46;
const DATA_RGB = [77, 155, 255]; // the look's --b-c (look.css), the colour of data

/**
 * The heights of the staircase: the start value and each running total, scaled from the lowest of them (H - 4)
 * to the highest (8); all equal draws in the middle. A month summing to zero or less stays inside the canvas.
 */
export function stairYs(days: number[], total: number, H: number): number[] {
  const start = total - days.reduce((t, v) => t + v, 0);
  const vals = [start];
  days.reduce((t, v) => { vals.push(t + v); return t + v; }, start);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) return vals.map(() => (H - 4 + 8) / 2);
  return vals.map((v) => H - 4 - ((v - lo) / (hi - lo)) * (H - 12));
}

/** Round-ups: the account's total as a small staircase, one step a day this month (mockup lab-summary.js stair). */
export function Stair({ days, total }: { days: number[]; total: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const cv = useRef<HTMLCanvasElement>(null);
  useUnveil(ref, "x");
  const box = useBoxSize(ref);
  const n = days.length, W = box.w || DRAWN_WIDTH;
  const month = days.reduce((t, v) => t + v, 0);
  useEffect(() => {
    const c = cv.current;
    if (!c || n === 0) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(W * dpr); c.height = Math.round(STAIR_HEIGHT * dpr);
    const g = c.getContext("2d");
    if (!g) throw new Error("This browser gave no 2d canvas for the round-ups chart.");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const H = STAIR_HEIGHT, ys = stairYs(days, total, H);
    const X = (i: number) => 2 + (i / n) * (W - 8);
    const pts: Array<[number, number]> = [[X(0), ys[0]]];
    for (let i = 0; i < n; i++) pts.push([X(i + 0.5), pts[pts.length - 1][1]], [X(i + 0.5), ys[i + 1]]);
    pts.push([X(n), ys[n]]);
    const rgba = (a: number) => `rgba(${DATA_RGB.join(",")},${a})`;
    const trace = () => { g.beginPath(); pts.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); };
    g.clearRect(0, 0, W, H);
    trace(); g.lineTo(X(n), H); g.lineTo(X(0), H); g.closePath();
    const fade = g.createLinearGradient(0, 0, 0, H); fade.addColorStop(0, rgba(0.3)); fade.addColorStop(1, rgba(0.02));
    g.fillStyle = fade; g.fill();
    trace(); g.strokeStyle = rgba(1); g.lineWidth = 2.2; g.lineJoin = "round"; g.shadowColor = rgba(0.5); g.shadowBlur = 8; g.stroke(); g.shadowBlur = 0;
    g.fillStyle = "#fff"; g.beginPath(); g.arc(X(n), ys[n], 4, 0, 7); g.fill();
  }, [days, total, n, month, W]);
  if (n === 0) return null;
  return (
    <div className="kz-stair" ref={ref} title={`Round-ups this month. Invested this month: ${EURO(month)}. In total: ${EURO(total)}`}>
      <canvas ref={cv} role="img" aria-label="Round-ups this month, the total one step a day" style={{ width: "100%", height: STAIR_HEIGHT }} />
    </div>
  );
}
