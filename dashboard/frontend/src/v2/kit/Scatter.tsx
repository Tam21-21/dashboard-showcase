// The scatter: items compared on two measures. Drawn after the Runs page's runScatter
// (docs/design/rail_mockup_2026-10-10.html, round 9). A marked zone can show the best corner;
// a press on a point picks it, and the picked point is drawn heavier with an outer ring. It draws at its box's own width, so
// its words keep their size on a phone (ui2, round six: scaled from 700 wide they were about 6 px at 390).
import { useLayoutEffect, useRef, useState } from "react";

export interface ScatterAxis {
  lo: number;
  hi: number;
  ticks: number[];
  text: (v: number) => string;
  name: string;
}

export interface ScatterPoint {
  id: number | string;
  x: number;
  y: number;
  r: number;
  colour: string;
  title: string;
}

interface Zone {
  x0: number;
  x1: number;
  y0: number;
  y1: number;
  colour: string;
  title: string;
}

const WIDE = 700;
const NARROW_BELOW = 560;
const PAD = { t: 16, r: 18, b: 40, l: 48 };
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

export function Scatter({ testid, label, x, y, zone, points, picked, pointAttr, onPick }: {
  testid: string;
  label: string;
  x: ScatterAxis;
  y: ScatterAxis;
  zone?: Zone;
  points: ScatterPoint[];
  picked: number | string | null;
  pointAttr: string;
  onPick: (id: number | string) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setW(Math.round(el.getBoundingClientRect().width));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const W = w > 0 ? w : WIDE;
  const H = W < NARROW_BELOW ? 280 : 340;
  const sx = (v: number) => PAD.l + ((clamp(v, x.lo, x.hi) - x.lo) / (x.hi - x.lo)) * (W - PAD.l - PAD.r);
  const sy = (v: number) => H - PAD.b - ((clamp(v, y.lo, y.hi) - y.lo) / (y.hi - y.lo)) * (H - PAD.b - PAD.t);
  return (
    <div data-testid={testid} className="scat" ref={box}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={label}>
        <defs>
          <filter id="scat-glow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="2.4" />
          </filter>
        </defs>
        {y.ticks.map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} stroke={`rgba(255,255,255,${v === 0 ? 0.14 : 0.05})`} />
            <text x={PAD.l - 8} y={sy(v) + 4} fontSize="11" fill="#8d98a8" textAnchor="end">{y.text(v)}</text>
          </g>
        ))}
        {x.ticks.map((v) => (
          <text key={v} x={sx(v)} y={H - PAD.b + 17} fontSize="11" fill="#8d98a8" textAnchor="middle">
            {x.text(v)}
          </text>
        ))}
        <text x={(PAD.l + W - PAD.r) / 2} y={H - 4} fontSize="11.5" fill="#8d98a8" textAnchor="middle">{x.name}</text>
        <text transform={`translate(12,${(PAD.t + H - PAD.b) / 2}) rotate(-90)`} fontSize="11.5" fill="#8d98a8" textAnchor="middle">{y.name}</text>
        {zone && (
          <g>
            <rect x={sx(zone.x0)} y={sy(zone.y1)} width={sx(zone.x1) - sx(zone.x0)} height={sy(zone.y0) - sy(zone.y1)} fill={zone.colour} opacity={0.09} rx={8} />
            <title>{zone.title}</title>
          </g>
        )}
        {points.map((p) => {
          const cx = sx(p.x);
          const cy = sy(p.y);
          const sel = p.id === picked;
          return (
            <g key={p.id} {...{ [pointAttr]: p.id }} style={{ cursor: "pointer" }} onClick={() => onPick(p.id)}>
              <circle cx={cx} cy={cy} r={p.r + 1.5} fill={p.colour} opacity={sel ? 0.55 : 0.28} filter="url(#scat-glow)" />
              <circle data-dot cx={cx} cy={cy} r={p.r} fill={p.colour} fillOpacity={0.15} stroke={p.colour} strokeWidth={sel ? 2.4 : 1.5} />
              {sel && <circle cx={cx} cy={cy} r={p.r + 5} fill="none" stroke={p.colour} strokeOpacity={0.6} strokeWidth={1} />}
              <title>{p.title}</title>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
