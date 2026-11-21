import { useRef, useState } from "react";
import { useUnveil } from "../../../kit";
import { KIND_ORDER, KIND_WORD, niceTicks, useWidth } from "./kinds";
import type { RunKind } from "./kinds";

// Runs by week (the look trial's Runs page): one bar a week, split by how the runs ended, so thousands of runs draw the
// same as sixty. The y marks are run counts; the x names mark every fourth week, counted back from the newest.

export type Week = { start: number; label: string; full: string; n: number; by: Partial<Record<RunKind, number>> };

const COLOUR: Record<RunKind, string> = {
  good: "var(--g)", warn: "var(--a)", soft: "rgba(141,152,168,.75)", bad: "var(--r)", run: "var(--b)", off: "rgba(141,152,168,.4)",
};

export function WeekBars({ weeks }: { weeks: Week[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const W = useWidth(ref);
  const [hot, setHot] = useState(-1);
  useUnveil(ref, "x", { dur: 1100 });
  const H = W > 0 && W < 720 ? 210 : 280;
  const L = 40;
  const base = H - 30;
  const top = 12;
  const hi = Math.max(1, ...weeks.map((w) => w.n)) * 1.1;
  const ticks = niceTicks(0, hi);
  const Y = (v: number) => base - (v / hi) * (base - top);
  const slot = weeks.length ? (W - L - 8) / weeks.length : 0;
  const bw = Math.max(5, Math.min(44, slot * 0.66));
  const X = (i: number) => L + slot * (i + 0.5);
  return (
    <div ref={ref} className="wk" data-testid="river">
      {W > 0 && (
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Runs by week" onMouseLeave={() => setHot(-1)}>
          {ticks.map((v) => (
            <g key={v}>
              <line x1={L} x2={W - 8} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} className="wk-grid" />
              <text x={L - 10} y={Y(v) + 4} textAnchor="end" className="wk-t">{v}</text>
            </g>
          ))}
          {weeks.map((w, i) => {
            let y = base;
            const tip = `Week of ${w.full}\n${w.n ? `${w.n} runs\n${KIND_ORDER.filter((k) => w.by[k]).map((k) => `${KIND_WORD[k]}: ${w.by[k]}`).join("\n")}` : "No run this week"}`;
            return (
              <g key={w.start} data-week={w.start} className={hot >= 0 && hot !== i ? "dim" : undefined} onMouseEnter={() => setHot(i)}>
                <title>{tip}</title>
                <rect x={L + slot * i} y={top} width={slot} height={base - top} fill="transparent" />
                {KIND_ORDER.map((k) => {
                  const c = w.by[k];
                  if (!c) return null;
                  const h = base - Y(c);
                  y -= h;
                  return <rect key={k} data-kind={k} x={X(i) - bw / 2} y={y} width={bw} height={Math.max(1, h - 1.5)} rx={2} fill={COLOUR[k]} />;
                })}
              </g>
            );
          })}
          <line x1={L} x2={W - 8} y1={base + 0.5} y2={base + 0.5} className="wk-base" />
          {weeks.map((w, i) => ((weeks.length - 1 - i) % 4 === 0 ? <text key={w.start} x={X(i)} y={base + 20} textAnchor="middle" className="wk-t">{w.label}</text> : null))}
        </svg>
      )}
    </div>
  );
}
