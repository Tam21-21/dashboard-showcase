import { Fragment, useId, useLayoutEffect, useRef, useState } from "react";
import type { MouseEvent, ReactNode, RefObject } from "react";

// Charts ported from docs/design/rail_mockup_2026-10-10.html: spark, line, mchart and hovers.
// Both draw as SVG at the real pixel size of their box and redraw when the box resizes.

const GRID = "rgba(227,231,237,.07)";
const GRID_ZERO = "rgba(227,231,237,.24)";
const AXIS = "rgba(141,152,168,.95)";
const HOVER_LINE = "#e3e7ed";

const AXIS_FONT = { fontFamily: "var(--sans)", fontSize: 11.5, fontWeight: 500 };

const lin = (d0: number, d1: number, r0: number, r1: number) => (v: number) =>
  r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);

function extent(arrs: number[][]): [number, number] {
  let a = Infinity;
  let b = -Infinity;
  for (const s of arrs) {
    for (const v of s) {
      if (v < a) a = v;
      if (v > b) b = v;
    }
  }
  return [a, b];
}

/** The line chart's ticks, the mockup's way: a step of 1, 2, 2.5, 5 or 10 times a power of ten, never finer than n lines. */
export function lineTicks(lo: number, hi: number, n = 4): number[] {
  const raw = (hi - lo || 1) / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((x) => x >= raw - 1e-9) as number;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(8));
  return out;
}

const path = (a: number[], X: (i: number) => number, Y: (v: number) => number) =>
  a.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");

/** Measures a box with getBoundingClientRect and again whenever it resizes; 0 by 0 until measured. */
export function useBoxSize(ref: RefObject<HTMLElement | null>): { w: number; h: number } {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      setSize((p) => (p.w === w && p.h === h ? p : { w, h }));
    };
    measure();
    // Where there is no ResizeObserver (a test page), the first measure stands.
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return size;
}

export function Spark({
  values, colour, fill, height = 44, strokeWidth = 1.3, dot = true,
}: { values: number[]; colour: string; fill?: number; height?: number; strokeWidth?: number; dot?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const { w } = useBoxSize(ref);
  const h = height;
  let svg: ReactNode = null;
  if (w > 0 && values.length > 0) {
    const [mn, mx] = extent([values]);
    const X = lin(0, values.length - 1, 1, w - 3);
    const Y = lin(mn, mx, h - 2, 2);
    const d = path(values, X, Y);
    const last = values.length - 1;
    svg = (
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block", overflow: "visible" }}>
        {fill != null && <path d={`${d}L${X(last)},${h}L${X(0)},${h}Z`} fill={colour} opacity={fill} />}
        <path d={d} fill="none" stroke={colour} strokeWidth={strokeWidth} strokeLinejoin="round" />
        {dot && <circle cx={X(last)} cy={Y(values[last])} r={2.2} fill={colour} />}
      </svg>
    );
  }
  return (
    <div ref={ref} style={{ height: h, width: "100%" }}>
      {svg}
    </div>
  );
}

export interface ChartSeries {
  v: number[];
  c: string;
  /** stroke width, default 1.5 */
  sw?: number;
  /** top opacity of a fade fill under the line */
  fill?: number;
  dash?: string;
  op?: number;
  /** The mockup lights the main line with a soft glow; false turns it off. */
  glow?: boolean;
}

/** A row of the hover box: the first entry is the heading, the rest are [label, value, tone class]. */
export type HoverRows = Array<string | [string, string, string?]>;
type Pad = [number, number, number, number];
type Layer = (X: (i: number) => number, Y: (v: number) => number, w: number, h: number, pad: Pad, n0: number) => ReactNode;

export interface LineChartProps {
  series: ChartSeries[];
  /** What the hover box shows for the point at index i (an index into the full series). */
  rows: (i: number) => HoverRows;
  yfmt: (v: number) => string;
  /** Labels along the bottom, as [index into the full series, text]. */
  xlabels?: Array<[number, string]>;
  /** First point drawn; earlier points are left out. */
  n0?: number;
  /** Drawn first, behind the lines. */
  under?: Layer;
  /** Drawn after the lines. */
  extra?: Layer;
  /** A tag on the y axis at the first series' last value. */
  endTag?: { colour: string; width?: number; format?: (v: number) => string };
  /** Space round the plot: top, right, bottom, left. The default is the mockup's. */
  pad?: Pad;
  minHeight?: number;
}

export function LineChart({
  series, rows, yfmt, xlabels, n0 = 0, under, extra, endTag, pad, minHeight = 320,
}: LineChartProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const { w, h } = useBoxSize(boxRef);
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [hover, setHover] = useState<number | null>(null);
  const [tipW, setTipW] = useState(0);

  useLayoutEffect(() => {
    if (hover !== null && tipRef.current) setTipW(tipRef.current.offsetWidth);
  }, [hover]);

  const len = series.length ? series[0].v.length : 0;
  const n = len - n0;
  const hasData = series.length > 0 && n > 0;

  const boxStyle = { position: "relative" as const, flex: 1, minHeight, height: "100%" };

  if (!hasData) {
    return (
      <div ref={boxRef} className="lcbox" style={boxStyle}>
        <div className="empty">No data</div>
      </div>
    );
  }

  // The mockup's plot: values on the left (40 px on a narrow chart, else 52), 10 above, 24 for dates below.
  const pd: Pad = pad ?? [10, 6, xlabels && xlabels.length ? 24 : 6, w < 520 ? 40 : 52];
  const [pt, pr, pb, pl] = pd;
  const X = n > 1 ? lin(0, n - 1, pl, w - pr) : () => pl + (w - pl - pr) / 2;
  let [mn, mx] = extent(series.map((s) => s.v.slice(n0)));
  const pv = (mx - mn || Math.abs(mx) || 1) * 0.12;
  mn -= pv;
  mx += pv;
  const Y = lin(mn, mx, h - pb, pt);
  const tk = lineTicks(mn, mx, 4);

  const onMove = (e: MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const k = Math.round(((e.clientX - r.left - pl) / (w - pr - pl)) * (n - 1));
    setHover(k < 0 || k > n - 1 ? null : k);
  };

  const sliced = series.map((s) => s.v.slice(n0));
  const endY = endTag ? Y(sliced[0][n - 1]) : -99;
  // Dates along the bottom, each centred on its point and kept inside the plot; one that would crowd the last is left out.
  const xTexts: ReactNode[] = [];
  let edge = -1e9;
  for (const [i, t] of xlabels ?? []) {
    if (i < n0) continue;
    const half = t.length * 3.2;
    const x = Math.max(pl + half, Math.min(w - pr - half, X(i - n0)));
    if (x - half < edge + 14) continue;
    xTexts.push(
      <text key={`x${i}`} x={x} y={h - 5} textAnchor="middle" fill={AXIS} style={AXIS_FONT}>
        {t}
      </text>,
    );
    edge = x + half;
  }
  let svg: ReactNode = null;
  if (w > 0 && h > 0) {
    svg = (
      <svg
        width={w}
        height={h}
        viewBox={`0 0 ${w} ${h}`}
        style={{ display: "block", overflow: "visible", position: "absolute", left: 0, top: 0 }}
      >
        {under?.(X, Y, w, h, pd, n0)}
        {tk.map((v) => (
          <line key={`g${v}`} x1={pl} x2={w - pr} y1={Math.round(Y(v)) + 0.5} y2={Math.round(Y(v)) + 0.5} stroke={v === 0 && mn < 0 ? GRID_ZERO : GRID} />
        ))}
        {tk
          .filter((v) => !endTag || Math.abs(Y(v) - endY) > 13)
          .map((v) => (
            <text key={`t${v}`} x={pl - 8} y={Y(v) + 0.5} textAnchor="end" dominantBaseline="central" fill={AXIS} style={AXIS_FONT}>
              {yfmt(v)}
            </text>
          ))}
        {series.map((q, k) => {
          const d = path(sliced[k], X, Y);
          return (
            <g key={k}>
              {q.fill != null && (
                <>
                  <defs>
                    <linearGradient id={`${uid}g${k}`} gradientUnits="userSpaceOnUse" x1="0" y1={Y(Math.max(...sliced[k]))} x2="0" y2={h - pb}>
                      <stop offset="0" style={{ stopColor: q.c }} stopOpacity={q.fill} />
                      <stop offset="1" style={{ stopColor: q.c }} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <path d={`${d}L${X(n - 1)},${h - pb}L${X(0)},${h - pb}Z`} fill={`url(#${uid}g${k})`} />
                </>
              )}
              <path
                className="ln"
                d={d}
                fill="none"
                stroke={q.c}
                strokeWidth={q.sw ?? 1.5}
                strokeDasharray={q.dash}
                strokeLinejoin="round"
                strokeLinecap="round"
                opacity={q.op ?? 1}
                style={{ stroke: q.c, filter: q.glow === false ? undefined : `drop-shadow(0 0 4px color-mix(in srgb, ${q.c} 50%, transparent))` }}
              />
            </g>
          );
        })}
        {endTag && (
          <>
            <line x1={pl} x2={w - pr} y1={endY} y2={endY} stroke={endTag.colour} strokeDasharray="2 3" opacity={0.7} />
            <rect x={w - pr + 2} y={endY - 9} width={endTag.width ?? pr - 4} height={18} fill={endTag.colour} />
            <text x={w - pr + 6} y={endY + 4} fontSize={11} fill="#000" fontWeight={600}>
              {(endTag.format ?? yfmt)(sliced[0][n - 1])}
            </text>
          </>
        )}
        {xTexts}
        {extra?.(X, Y, w, h, pd, n0)}
        {hover !== null && (
          <g style={{ pointerEvents: "none" }}>
            <line className="hvl" x1={Math.round(X(hover)) + 0.5} x2={Math.round(X(hover)) + 0.5} y1={pt} y2={h - pb} stroke={HOVER_LINE} strokeOpacity={0.3} />
          </g>
        )}
      </svg>
    );
  }

  let tip: ReactNode = null;
  if (hover !== null) {
    const rr = rows(n0 + hover);
    const x = X(hover);
    const left = x + 14 + tipW > w - pr ? x - 14 - tipW : x + 14;
    tip = (
      <div
        ref={tipRef}
        className="tip"
        style={{ left, top: Math.max(4, Y(series[0].v[n0 + hover]) - 70) }}
      >
        <b>{rr[0] as string}</b>
        {rr.slice(1).map((row, i) => {
          const [a, b, c] = row as [string, string, string?];
          return (
            <div key={i}>
              <span>{a}</span>
              <span className={c}>{b}</span>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div
      ref={boxRef}
      className="lcbox"
      style={boxStyle}
      onMouseMove={onMove}
      onMouseLeave={() => setHover(null)}
    >
      {svg}
      {tip}
    </div>
  );
}

// Thin card visuals ported from the mockup's kzSeg and kzSteps.
const KW = 260;
const KH = 36;
const KZ_TEXT = "#8d98a8";
const KZ_GREEN = "#2fe08a";

function KzFrame({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="kz">
      <svg viewBox={`0 0 ${KW} ${KH}`} fontFamily="Azeret Mono,monospace">
        {title != null && <title>{title}</title>}
        {children}
      </svg>
    </div>
  );
}

/** The parts of a whole, in order; each part is labelled when wide enough. */
export function KzSeg({ parts }: { parts: Array<[number, string, string]> }) {
  const tot = parts.reduce((a, p) => a + p[0], 0) || 1;
  let x = 0;
  const out: ReactNode[] = [];
  parts.forEach(([v, c, l], i) => {
    const w = (v / tot) * KW;
    if (w <= 0) return;
    out.push(
      <rect key={`r${i}`} x={x + 1} y={7} width={Math.max(2, w - 2)} height={6} rx={3} fill={c}>
        <title>{`${l}: ${v.toLocaleString("en-US")}`}</title>
      </rect>,
    );
    if (w > l.length * 6.6 + 4) {
      out.push(
        <text key={`t${i}`} x={x + 2} y={28} fontSize={10} fill={KZ_TEXT} letterSpacing={0.6}>{l.toUpperCase()}</text>,
      );
    }
    x += w;
  });
  return <KzFrame>{out}</KzFrame>;
}

/** One bar per step; the words say how many are green. */
export function KzSteps({ items, word = "READY" }: { items: Array<[string, string]>; word?: string }) {
  const n = items.length;
  const bw = KW / n;
  const done = items.filter((it) => it[1] === KZ_GREEN).length;
  return (
    <KzFrame>
      {items.map(([l, c], i) => (
        <rect key={i} x={i * bw + 1} y={7} width={bw - 2} height={6} rx={3} fill={c}>
          <title>{l}</title>
        </rect>
      ))}
      <text x={0} y={28} fontSize={10} fill={KZ_TEXT} letterSpacing={0.6}>{`${done} OF ${n} ${word}`}</text>
    </KzFrame>
  );
}

const KZ_GREY_TRACK = "rgba(255,255,255,.08)";

export interface DayBar {
  /** Stacked from the bottom in this order. */
  parts: Array<{ value: number; colour: string; name?: string }>;
  /** The hover word of the bar (card size). */
  hover?: string;
  /** The day was not read (card size). */
  unread?: boolean;
}

/**
 * One bar per day. Size "card" is the thin card strip, the last bar lit; size "tile" fills the box
 * it sits in and measures it.
 */
export function DayBars({
  bars, size, title, opacity = 0.85,
}: { bars: DayBar[]; size: "card" | "tile"; title?: string; opacity?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const { w } = useBoxSize(ref);
  const sum = (b: DayBar) => b.parts.reduce((a, p) => a + p.value, 0);
  const n = bars.length;
  if (size === "card") {
    const mx = Math.max(1, ...bars.map(sum));
    const bw = KW / n;
    const base = KH - 4;
    const unit = KH - 8;
    return (
      <KzFrame title={title}>
        {bars.map((b, i) => {
          const op = i === n - 1 ? 1 : 0.55;
          let y = base;
          return (
            <g key={i} data-bar={i} data-unread={b.unread ? "1" : undefined}>
              {b.parts.map((p, k) => {
                const h = (p.value / mx) * unit;
                y -= h;
                return h > 0 ? (
                  <rect key={k} data-part={p.name} x={i * bw + bw * 0.16} y={y} width={bw * 0.68} height={h} rx={1.5} fill={p.colour} opacity={op} />
                ) : null;
              })}
              {b.hover != null && (
                <rect x={i * bw} y={0} width={bw} height={KH} fill="transparent">
                  <title>{b.hover}</title>
                </rect>
              )}
            </g>
          );
        })}
      </KzFrame>
    );
  }
  const h = 44;
  const top = Math.max(0, ...bars.map(sum));
  const slot = w / Math.max(n, 1);
  return (
    <div ref={ref} style={{ height: h, width: "100%" }}>
      {w > 0 && n > 0 && (
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: "block" }}>
          {bars.map((b, i) => {
            let y = h;
            return (
              <Fragment key={i}>
                {b.parts.map((p, k) => {
                  const bh = top > 0 && p.value > 0 ? Math.max(2, (p.value / top) * (h - 6)) : 0;
                  y -= bh;
                  return <rect key={k} x={i * slot + slot * 0.15} y={y} width={slot * 0.7} height={bh} fill={p.colour} opacity={opacity} />;
                })}
              </Fragment>
            );
          })}
        </svg>
      )}
    </div>
  );
}

/** The parts of a whole on one thin bar, each with its word on hover. */
export function ShareBar({ parts }: { parts: Array<[number, string, string]> }) {
  const tot = parts.reduce((a, p) => a + p[0], 0);
  let x = 0;
  return (
    <KzFrame>
      <rect x={0} y={15} width={KW} height={6} rx={3} fill={KZ_GREY_TRACK} />
      {parts.map(([v, c, l], i) => {
        const w = tot > 0 ? (v / tot) * KW : 0;
        if (w <= 0) return null;
        const at = x;
        x += w;
        return (
          <rect key={i} data-share={l} x={at} y={15} width={Math.max(2, w - 2)} height={6} rx={3} fill={c}>
            <title>{`${l}: ${v.toLocaleString("en-US")}`}</title>
          </rect>
        );
      })}
    </KzFrame>
  );
}
