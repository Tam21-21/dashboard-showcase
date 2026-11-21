import { useLayoutEffect, useRef, useState } from "react";
import type { MouseEvent, ReactNode, RefObject } from "react";

// The charts of the Sports page. Each draws as SVG at the real size of its box and gives a hover box per day,
// week or night. A value that is null leaves a gap; nothing is drawn between two real points across it.

const GRID = "rgba(255,255,255,.05)";
const AXIS = "#c3cbd6";
const LINE_PAD: Pad = [10, 70, 24, 6];
const BAR_PAD: Pad = [10, 10, 24, 40];

export type Pad = [top: number, right: number, bottom: number, left: number];
export interface Tip {
  /** The whole hover as one sentence, for the screen reader and the tests. */
  label: string;
  head: string;
  rows: Array<[string, string]>;
}

function useBox(ref: RefObject<HTMLElement | null>) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.round(r.width), h: Math.round(r.height) });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

export function niceTicks(lo: number, hi: number, want = 4): number[] {
  const raw = (hi - lo) / want || 1;
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  const step = (m < 1.5 ? 1 : m < 3 ? 2 : m < 7 ? 5 : 10) * p;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(+v.toFixed(8));
  return out;
}

export interface Geo { w: number; h: number; pad: Pad }

export function ChartBox({
  testid, n, mode, pad, minHeight, tip, children,
}: {
  testid: string;
  n: number;
  mode: "line" | "bars";
  pad: Pad;
  minHeight: number;
  tip: (i: number) => Tip;
  children: (g: Geo, hover: number | null) => ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const { w, h } = useBox(ref);
  const [hover, setHover] = useState<number | null>(null);
  const pr = pad[1];
  const pl = pad[3];
  const onMove = (e: MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left - pl;
    const span = w - pr - pl;
    const k = mode === "line" ? Math.round((x / span) * (n - 1)) : Math.floor((x / span) * n);
    setHover(k < 0 || k > n - 1 ? null : k);
  };
  const t = hover === null ? null : tip(hover);
  const xAt = hover === null ? 0 : mode === "line" ? pl + (n > 1 ? (hover / (n - 1)) * (w - pr - pl) : 0) : pl + ((hover + 0.5) * (w - pr - pl)) / n;
  return (
    <div ref={ref} className="lcbox" data-testid={testid} style={{ minHeight, height: minHeight }} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      {w > 0 && h > 0 && (
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block", overflow: "visible", position: "absolute", left: 0, top: 0 }}>
          {children({ w, h, pad }, hover)}
          {hover !== null && (
            <line x1={xAt} x2={xAt} y1={pad[0]} y2={h - pad[2]} stroke="#eef2f7" strokeOpacity={0.35} style={{ pointerEvents: "none" }} />
          )}
        </svg>
      )}
      {t && (
        <div className="tip" aria-label={t.label} style={{ left: xAt + 14 + 240 > w - pr ? xAt - 14 - 240 : xAt + 14, top: 8 }}>
          <b>{t.head}</b>
          {t.rows.map(([a, b]) => (
            <div key={a}><span>{a}</span><span>{b}</span></div>
          ))}
        </div>
      )}
    </div>
  );
}

export function LineSeriesChart({
  testid, values, colour, yfmt, xlabels, tip, guides = [], fill = 0.12, minHeight = 150, pad = LINE_PAD,
}: {
  testid: string;
  values: Array<number | null>;
  colour: string;
  yfmt: (v: number) => string;
  /** [index, text] along the bottom */
  xlabels: Array<[number, string]>;
  tip: (i: number) => Tip;
  /** Dashed reference lines at fixed values (Garmin's balanced range). */
  guides?: number[];
  fill?: number;
  minHeight?: number;
  pad?: Pad;
}) {
  const n = values.length;
  const have = [...values.filter((v): v is number => v !== null), ...guides];
  let lo = Math.min(...have);
  let hi = Math.max(...have);
  const room = (hi - lo) * 0.08 || 1;
  lo -= room;
  hi += room;
  return (
    <ChartBox testid={testid} n={n} mode="line" pad={pad} minHeight={minHeight} tip={tip}>
      {({ w, h }, hover) => {
        const [pt, pr, pb, pl] = pad;
        const X = (i: number) => pl + (n > 1 ? (i / (n - 1)) * (w - pr - pl) : 0);
        const Y = (v: number) => h - pb - ((v - lo) / (hi - lo || 1)) * (h - pb - pt);
        // Each run of consecutive days with a value is one line.
        const runs: number[][] = [];
        values.forEach((v, i) => {
          if (v === null) return;
          const last = runs[runs.length - 1];
          if (last && last[last.length - 1] === i - 1) last.push(i);
          else runs.push([i]);
        });
        return (
          <>
            {niceTicks(lo, hi).map((v) => (
              <g key={v}>
                <line x1={pl} x2={w - pr} y1={Y(v)} y2={Y(v)} stroke={GRID} />
                <text x={w - pr + 8} y={Y(v) + 4} fill={AXIS} fontSize={11}>{yfmt(v)}</text>
              </g>
            ))}
            {guides.map((v) => (
              <path key={v} className="bal" d={`M${X(0)},${Y(v)}L${X(n - 1)},${Y(v)}`} stroke="#c3cbd6" strokeWidth={1} strokeDasharray="4 4" opacity={0.7} fill="none" />
            ))}
            {runs.map((r) => {
              if (r.length === 1) return <circle key={r[0]} className="pt" cx={X(r[0])} cy={Y(values[r[0]] as number)} r={2} fill={colour} />;
              const d = r.map((i, j) => `${j ? "L" : "M"}${X(i).toFixed(1)},${Y(values[i] as number).toFixed(1)}`).join("");
              return (
                <g key={r[0]}>
                  <path d={`${d}L${X(r[r.length - 1]).toFixed(1)},${h - pb}L${X(r[0]).toFixed(1)},${h - pb}Z`} fill={colour} opacity={fill} />
                  <path className="ln" d={d} fill="none" stroke={colour} strokeWidth={2} strokeLinejoin="round" />
                </g>
              );
            })}
            {xlabels.map(([i, text]) => (
              <text key={i} x={X(i)} y={h - pb + 17} fill={AXIS} fontSize={11}>{text}</text>
            ))}
            {hover !== null && values[hover] !== null && (
              <circle cx={X(hover)} cy={Y(values[hover] as number)} r={4} fill={colour} stroke="#060809" strokeWidth={2} style={{ pointerEvents: "none" }} />
            )}
          </>
        );
      }}
    </ChartBox>
  );
}

export interface StackPart { value: number; colour: string }

/** Hour ticks: a step of 2, 5 or 10 and a top that is a multiple of it. */
export function hourAxis(max: number): { top: number; ticks: number[] } {
  const step = max <= 10 ? 2 : max <= 20 ? 5 : 10;
  const top = Math.max(step, Math.ceil(max / step) * step);
  return { top, ticks: Array.from({ length: top / step + 1 }, (_, i) => i * step) };
}

/** One stacked bar per slot; a null slot is a gap. Values are in hours. */
export function StackedBars({
  testid, attr, stacks, tip, xlabels, minHeight = 230, min = 0, refLine,
}: {
  testid: string;
  /** The data attribute set on each bar's group, so a test can count the bars. */
  attr: "data-night" | "data-week";
  stacks: Array<StackPart[] | null>;
  tip: (i: number) => Tip;
  xlabels: Array<[number, string]>;
  minHeight?: number;
  min?: number;
  /** A dashed horizontal line at a value in hours, with its name (Garmin's sleep need). */
  refLine?: { hours: number; label: string };
}) {
  const n = stacks.length;
  const top = Math.max(min, refLine?.hours ?? 0, ...stacks.map((s) => (s ? s.reduce((a, p) => a + p.value, 0) : 0)));
  const { top: yTop, ticks } = hourAxis(top);
  return (
    <ChartBox testid={testid} n={n} mode="bars" pad={BAR_PAD} minHeight={minHeight} tip={tip}>
      {({ w, h }, hover) => {
        const [pt, pr, pb, pl] = BAR_PAD;
        const cw = (w - pr - pl) / n;
        const Y = (v: number) => h - pb - (v / yTop) * (h - pb - pt);
        return (
          <>
            {ticks.map((v) => (
              <g key={v}>
                <line x1={pl} x2={w - pr} y1={Y(v)} y2={Y(v)} stroke={GRID} />
                <text x={0} y={Y(v) + 4} fill={AXIS} fontSize={11}>{`${v} h`}</text>
              </g>
            ))}
            {stacks.map((s, i) => {
              if (s === null) return null;
              let base = 0;
              return (
                <g key={i} {...{ [attr]: i }} opacity={hover === null || hover === i ? 1 : 0.6}>
                  {s.map((p, k) => {
                    const y0 = Y(base);
                    base += p.value;
                    return p.value > 0 ? (
                      <rect key={k} x={pl + i * cw + cw * 0.2} y={Y(base)} width={cw * 0.6} height={Math.max(0, y0 - Y(base))} fill={p.colour} />
                    ) : null;
                  })}
                </g>
              );
            })}
            {xlabels.map(([i, text]) => (
              <text key={i} x={pl + (i + 0.5) * cw} y={h - 6} fill={AXIS} fontSize={11} textAnchor="middle">{text}</text>
            ))}
            {refLine && (
              <g style={{ pointerEvents: "none" }}>
                <path className="need" d={`M${pl},${Y(refLine.hours)}L${w - pr},${Y(refLine.hours)}`} stroke="#eef2f7" strokeWidth={1} strokeDasharray="5 4" fill="none" />
                <text x={w - pr} y={Y(refLine.hours) - 5} fill="#eef2f7" fontSize={11} textAnchor="end">{refLine.label}</text>
              </g>
            )}
          </>
        );
      }}
    </ChartBox>
  );
}
