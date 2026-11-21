import { useRef } from "react";
import type { YieldTenor } from "../../../../types/display";
import { Block, useBoxSize } from "../../../kit";
import { DASH, spreadOf } from "./data";

const H = 250;
// The page colour (the mockup draws the curve in it).
const BLUE = "rgb(var(--b-c))";
const bpsText = (b: number) => `${b > 0 ? "+" : ""}${b.toFixed(1)} basis points`;

/** The level at each tenor as one line, each tenor's move since its previous print as a bar under it, and the 10 year minus 2 year spread (mockup: lab-markets.js, US interest rates). */
export default function Rates({ tenors, error }: { tenors: YieldTenor[] | undefined; error: string | null }) {
  const box = useRef<HTMLDivElement>(null);
  const { w } = useBoxSize(box);
  const W = w > 0 ? w : 320;
  const spread = error ? { points: null, bps: null, reason: error } : spreadOf(tenors ?? []);
  const rows = tenors ?? [];
  const levels = rows.map((t) => t.level);
  const lo = Math.floor(Math.min(...levels) * 4) / 4;
  const hi = Math.ceil(Math.max(...levels) * 4) / 4;
  const slot = W / Math.max(rows.length, 1);
  const x = (i: number) => (i + 0.5) * slot;
  const mid = H - 46;
  const zero = H - 22;
  const y = (v: number) => 26 + (1 - (v - lo) / (hi - lo || 1)) * (mid - 26 - 12);
  const bmax = Math.max(1, ...rows.map((t) => Math.abs(t.change_bps)));
  const bh = (b: number) => (Math.abs(b) / bmax) * 18;
  const line = rows.map((t, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(t.level).toFixed(1)}`).join("");

  return (
    <Block title="US interest rates">
      {error && <div className="empty">{error}</div>}
      {!error && tenors !== undefined && rows.length === 0 && <div className="empty">The rates feed returned no rows.</div>}
      <div ref={box}>{rows.length > 0 && (
        <svg className="mk-rates" viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label="US interest rates by tenor">
          <defs><linearGradient id="mk-rates-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style={{ stopColor: BLUE }} stopOpacity=".34" /><stop offset="1" style={{ stopColor: BLUE }} stopOpacity="0" /></linearGradient></defs>
          <path d={`${line}L${x(rows.length - 1).toFixed(1)},${mid}L${x(0).toFixed(1)},${mid}Z`} fill="url(#mk-rates-fill)" />
          <path d={line} fill="none" style={{ stroke: BLUE }} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
          {rows.map((t, i) => {
            const bx = Math.round((x(i) - 8) / 4) * 4;
            const h = bh(t.change_bps);
            return (
              <rect
                key={`b-${t.tenor}`} data-testid="rates-change" x={bx} width={16}
                y={t.change_bps > 0 ? zero - h : t.change_bps < 0 ? zero + 4 : zero} height={t.change_bps === 0 ? 4 : h} rx={t.change_bps === 0 ? 2 : 3}
                fill={t.change_bps > 0 ? "#2fe08a" : t.change_bps < 0 ? "#ff5a63" : "rgba(141,152,168,.4)"}
              >
                <title>{`${t.tenor}: ${bpsText(t.change_bps)} from ${t.previous_observation_date} to ${t.observation_date}`}</title>
              </rect>
            );
          })}
          {rows.map((t, i) => {
            const key = t.tenor === "2Y" || t.tenor === "10Y";
            return (
              <g key={t.tenor}>
                {key && <text x={x(i) - 4} y={y(t.level) - 12} fontSize="12" fill="#e3e7ed" textAnchor="end" fontFamily="var(--mono)">{`${t.level.toFixed(2)}%`}</text>}
                <circle data-testid="rates-point" cx={x(i)} cy={y(t.level)} r={key ? 5 : 3} fill={key ? "#e3e7ed" : undefined} style={key ? undefined : { fill: BLUE }}>
                  <title>{`${t.tenor}: ${t.level.toFixed(2)}%, ${t.change_bps >= 0 ? "+" : ""}${t.change_bps.toFixed(1)} basis points`}</title>
                </circle>
              </g>
            );
          })}
        </svg>
      )}</div>
      {rows.length > 0 && <div className="t-labels">{rows.map((t) => <span key={t.tenor}>{t.tenor}</span>)}</div>}
      {(tenors !== undefined || error !== null) && (
        <p className="spread" title={spread.bps != null ? `Change today: ${spread.bps >= 0 ? "+" : ""}${spread.bps.toFixed(1)} basis points` : undefined}>
          <span>10 year minus 2 year</span>
          <b className="num">
            <span data-testid="rates-spread" title={spread.reason ?? undefined}>{spread.points != null ? spread.points.toFixed(2) : DASH}</span>
            {spread.points != null && " points"}
            <span data-testid="rates-spread-change" hidden>{spread.bps != null ? `${spread.bps >= 0 ? "+" : ""}${spread.bps.toFixed(1)} basis points` : DASH}</span>
          </b>
        </p>
      )}
    </Block>
  );
}
