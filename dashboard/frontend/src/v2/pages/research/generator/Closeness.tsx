import { useRef } from "react";
import { useUnveil } from "../../../kit";
import type { GenTheory, TrialsDay } from "../../../data/generatorApi";
import { dateToEpoch, fmtDmy } from "../../../../lib/format";
import { useWidth } from "../runs/RunRiver";
import { niceTicks } from "../runs/kinds";

// Round six (Tamer 2026-11-14), drawn first in the look trial (Desktop/overview_trial, lab-p-generator.js). Two questions:
// how close did each theory's search come to the bars, and how much of each search space was tried? Everything here is read
// from the batch trees: the search's quality answer names the nearest strategy of its pool, its five numbers and the bars
// it failed. The bars' own values are not served, so a cell says passed or failed (the server's word) and shows the number;
// it does not draw a distance to a bar.

export const NUMS = [
  ["closed", "Closed trades", (v: number) => Math.round(v).toLocaleString("en-GB")],
  ["win_share", "Winning trades", (v: number) => `${Math.round(v * 100)}%`],
  ["max_drawdown", "Deepest drop", (v: number) => `${(v * 100).toFixed(1)}%`],
  ["avg_month", "Average month", (v: number) => `${(v * 100).toFixed(2)}%`],
  ["recovery", "Gain per drop", (v: number) => v.toFixed(1)],
] as const;
type NumKey = (typeof NUMS)[number][0];

export type Nearest = { pool: number; answer: string; sharpe: number; met: number; failed: string[]; m: Record<NumKey, number | null> };

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** The search's quality answer, read defensively: null when there is none yet, "odd" when its shape is not the one this page knows. */
export function nearestOf(t: GenTheory): Nearest | null | "odd" {
  const q = t.search?.rounds?.quality as Record<string, unknown> | null | undefined;
  if (q == null) return null;
  const c = q.closest as Record<string, unknown> | null | undefined;
  if (typeof q.answer !== "string" || !isNum(q.pool)) return "odd";
  if (c == null) return { pool: q.pool, answer: q.answer, sharpe: NaN, met: 0, failed: [], m: { closed: null, win_share: null, max_drawdown: null, avg_month: null, recovery: null } };
  const m = c.metrics as Record<string, unknown> | undefined;
  if (!isNum(c.sharpe) || !isNum(c.met) || !Array.isArray(c.failed) || m == null) return "odd";
  const val = (k: NumKey) => (isNum(m[k]) ? (m[k] as number) : null);
  return { pool: q.pool, answer: q.answer, sharpe: c.sharpe, met: c.met, failed: c.failed.map(String), m: { closed: val("closed"), win_share: val("win_share"), max_drawdown: val("max_drawdown"), avg_month: val("avg_month"), recovery: val("recovery") } };
}

/** Trials per day, the last 30 days the server lists, one bar a day on a scale with its numbers; an empty day is a short grey mark. */
export function TrialDays({ days }: { days: TrialsDay[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const W = useWidth(ref);
  useUnveil(ref, "y", { dur: 900 });
  const shown = days.slice(-30);
  const H = W > 0 && W < 720 ? 200 : 250;
  const L = 44;
  const base = H - 30;
  const topPad = 12;
  const hi = Math.max(1, ...shown.map((d) => d.trials)) * 1.1;
  const ticks = niceTicks(0, hi);
  const Y = (v: number) => base - (v / hi) * (base - topPad);
  const slot = shown.length ? (W - L - 8) / shown.length : 0;
  const bw = Math.max(6, Math.min(34, slot * 0.6));
  const X = (i: number) => L + slot * (i + 0.5);
  const word = (d: TrialsDay) => fmtDmy(dateToEpoch(d.day));
  const short = (d: TrialsDay) => new Date(`${d.day}T12:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  return (
    <div className="gn-days" data-testid="trial-days">
      <div ref={ref} className="gn-dplot" style={{ minHeight: H }}>
        {W > 0 && shown.length > 0 && (
          <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Trials per day">
            <defs><linearGradient id="gn-fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="var(--b)" stopOpacity=".95" /><stop offset="1" stopColor="var(--b)" stopOpacity=".5" /></linearGradient></defs>
            {ticks.map((v) => (
              <g key={v}>
                <line x1={L} x2={W - 8} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} className="gn-grid" />
                <text x={L - 10} y={Y(v) + 4} textAnchor="end" className="gn-t">{(v || 0).toLocaleString("en-GB")}</text>
              </g>
            ))}
            {shown.map((d, i) => {
              const h = d.trials ? Math.max(4, base - Y(d.trials)) : 3;
              return (
                <rect key={d.day} data-bar={d.day} x={X(i) - bw / 2} y={base - h} width={bw} height={h} rx={d.trials ? 3 : 1.5} fill={d.trials ? "url(#gn-fade)" : undefined} className={d.trials ? "on" : "off"} data-empty={d.trials ? undefined : ""}>
                  <title>{`${word(d)}: ${d.trials.toLocaleString("en-GB")} trials, ${d.searches} searches`}</title>
                </rect>
              );
            })}
            <line x1={L} x2={W - 8} y1={base + 0.5} y2={base + 0.5} className="gn-base" />
            {shown.map((d, i) => (i % (W < 720 ? 4 : 2) === 0 ? <text key={d.day} x={X(i)} y={base + 20} textAnchor="middle" className="gn-t">{short(d)}</text> : null))}
          </svg>
        )}
      </div>
    </div>
  );
}
