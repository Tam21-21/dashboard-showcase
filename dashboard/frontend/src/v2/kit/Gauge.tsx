import type { ReactNode } from "react";

// The mockup's sgauge (docs/design/rail_mockup_2026-10-10.html): a half circle in bands, the part up to
// the value lit and the rest dim, optional marks, a glowing dot at the value, the value and a label in the middle.

export interface GaugeProps {
  id: string;
  value: number;
  min: number;
  max: number;
  /** [from, to, colour] */
  bands: Array<[number, number, string]>;
  marks?: number[];
  label?: string;
  big?: boolean;
  sw?: number;
  foot?: number;
  top?: number;
  side?: number;
  w?: number;
  h?: number;
  title?: string;
  text?: ReactNode;
}

const STEPS = 96;
const f4 = (n: number) => n.toFixed(2);

export function Gauge({ id, value, min, max, bands, marks = [], label, big, sw = 10, foot = 30, top = 30, side = 16, w = 240, h = 118, title, text }: GaugeProps) {
  const cx = w / 2;
  const cy = h - foot;
  const r = Math.min(w / 2 - side, h - top - foot);
  const frac = (x: number) => Math.max(0, Math.min(1, (x - min) / (max - min)));
  const ang = (x: number) => Math.PI * (1 - frac(x));
  const at = (x: number, rr: number): [number, number] => [cx + rr * Math.cos(ang(x)), cy - rr * Math.sin(ang(x))];
  const colour = (x: number) => {
    for (const [a, b, c] of bands) if (x >= a && x <= b) return c;
    return bands[bands.length - 1][2];
  };

  const arcs = Array.from({ length: STEPS }, (_, k) => {
    const a = min + ((max - min) * k) / STEPS;
    const b = min + (max - min) * Math.min(1, (k + 1.2) / STEPS);
    const [x0, y0] = at(a, r);
    const [x1, y1] = at(b, r);
    return (
      <path
        key={k}
        d={`M${f4(x0)},${f4(y0)}A${r},${r} 0 0 1 ${f4(x1)},${f4(y1)}`}
        stroke={colour(a)}
        strokeOpacity={a <= value ? 0.95 : 0.16}
        strokeWidth={sw}
        fill="none"
      />
    );
  });
  const [ex, ey] = at(value, r);
  const c = colour(value);
  return (
    <svg data-gauge={id} role="img" viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: h, display: "block", fontFamily: "var(--mono)" }}>
      {title != null && <title>{title}</title>}
      <defs>
        <filter id={`gg${id}`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" />
        </filter>
      </defs>
      {arcs}
      {marks.map((x) => {
        const [x1, y1] = at(x, r - sw / 2 - 4);
        const [x2, y2] = at(x, r + sw / 2 + 4);
        return <line key={x} x1={f4(x1)} y1={f4(y1)} x2={f4(x2)} y2={f4(y2)} stroke="#eef2f7" strokeOpacity={0.6} strokeWidth={1.5} />;
      })}
      <circle cx={f4(ex)} cy={f4(ey)} r={sw * 0.9} fill={c} opacity={0.55} filter={`url(#gg${id})`} />
      <circle cx={f4(ex)} cy={f4(ey)} r={sw * 0.62} fill="#060809" stroke={c} strokeWidth={3} />
      <text x={cx} y={cy - (big ? 10 : 4)} textAnchor="middle" fontSize={big ? 30 : 20} fill="#eef2f7">{text ?? value}</text>
      {label != null && (
        <text x={cx} y={cy + 14} textAnchor="middle" fontSize={10.5} letterSpacing={1.4} fill="#c3cbd6">{label}</text>
      )}
    </svg>
  );
}
