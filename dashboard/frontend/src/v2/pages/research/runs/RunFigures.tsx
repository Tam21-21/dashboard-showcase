import { useRef, useState } from "react";
import type { MouseEvent } from "react";
import { useUnveil } from "../../../kit";
import { useWidth } from "./kinds";

// The picked run in full: its money line, point by point from where it started, with the money written at four levels.
// A log scale once it grew more than tenfold, so the early part still shows.

const money = (v: number) => Math.round(v).toLocaleString("en-GB");

/** A signed percent; a figure past 1,000% drops its decimals and groups its digits. */
export function pct(x: number, dp = 1): string {
  const v = x * 100;
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return Math.abs(v) >= 1000 ? `${sign}${Math.round(Math.abs(v)).toLocaleString("en-GB")}%` : `${sign}${Math.abs(v).toFixed(dp)}%`;
}

// A smooth line through the points that never overshoots between them (a monotone cubic curve).
function smooth(P: Array<[number, number]>): string {
  const n = P.length;
  const d: number[] = [];
  for (let i = 0; i < n - 1; i++) d.push((P[i + 1][1] - P[i][1]) / (P[i + 1][0] - P[i][0] || 1));
  const m: number[] = [];
  for (let i = 0; i < n; i++) m.push(i === 0 ? d[0] : i === n - 1 ? d[n - 2] : d[i - 1] * d[i] <= 0 ? 0 : (2 * d[i - 1] * d[i]) / (d[i - 1] + d[i]));
  let out = `M${P[0][0].toFixed(1)} ${P[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = (P[i + 1][0] - P[i][0]) / 3;
    out += `C${(P[i][0] + h).toFixed(1)} ${(P[i][1] + m[i] * h).toFixed(1)} ${(P[i + 1][0] - h).toFixed(1)} ${(P[i + 1][1] - m[i + 1] * h).toFixed(1)} ${P[i + 1][0].toFixed(1)} ${P[i + 1][1].toFixed(1)}`;
  }
  return out;
}

export function MoneyLine({ values }: { values: number[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const W = useWidth(ref);
  const [hx, setHx] = useState(-1);
  useUnveil(ref, "x", { dur: 900 });
  const H = 320;
  const PAD = 64;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const logy = lo > 0 && hi / lo > 10;
  const f = (v: number) => (logy ? Math.log(v) : v);
  const up = values[values.length - 1] >= values[0];
  const colour = up ? "var(--g)" : "var(--r)";
  const Y = (v: number) => 14 + (1 - (f(v) - f(lo)) / (f(hi) - f(lo) || 1)) * (H - 28);
  const X = (i: number) => PAD + (i / Math.max(1, values.length - 1)) * (W - PAD);
  const levels = [0, 1, 2, 3].map((j) => (logy ? Math.exp(f(lo) + ((f(hi) - f(lo)) * j) / 3) : lo + ((hi - lo) * j) / 3));
  const P: Array<[number, number]> = values.map((v, i) => [X(i), Y(v)]);
  const line = smooth(P);
  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const b = e.currentTarget.getBoundingClientRect();
    const i = Math.round(((e.clientX - b.left - PAD) / Math.max(1, b.width - PAD)) * (values.length - 1));
    setHx(Math.max(0, Math.min(values.length - 1, i)));
  };
  return (
    <div className="rf-line" data-testid="money-line">
      <h3 className="rf-h">Account value</h3>
      <div ref={ref} className="rf-plot">
        {W > 0 && (
          <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="The run's money, point by point" onMouseMove={onMove} onMouseLeave={() => setHx(-1)}>
            <defs>
              <linearGradient id="rf-fill" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0" stopColor={colour} stopOpacity=".28" />
                <stop offset="1" stopColor={colour} stopOpacity="0" />
              </linearGradient>
            </defs>
            {levels.map((v, j) => (
              <g key={j}>
                <line x1={PAD} x2={W} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} className="wk-grid" />
                <text x={PAD - 12} y={Y(v) + 4} textAnchor="end" className="wk-t">{money(v)}</text>
              </g>
            ))}
            <path d={`${line}L${W} ${H}L${PAD} ${H}Z`} fill="url(#rf-fill)" />
            <line x1={PAD} x2={W} y1={Y(values[0])} y2={Y(values[0])} className="rf-start" />
            <path d={line} className={`rf-path ${up ? "up" : "down"}`} />
            {hx >= 0 && (
              <g>
                <line x1={X(hx)} x2={X(hx)} y1={0} y2={H} className="rf-cross" />
                <circle cx={X(hx)} cy={Y(values[hx])} r={5} fill={colour} stroke="#0b0f14" strokeWidth={2.5} />
              </g>
            )}
            <title>{hx >= 0 ? `Point ${hx + 1} of ${values.length}: ${money(values[hx])}, ${pct(values[hx] / values[0] - 1)} since the start` : "The money the run started with, point by point along the run"}</title>
          </svg>
        )}
      </div>
    </div>
  );
}
