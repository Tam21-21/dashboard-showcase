import type { CSSProperties, ReactNode } from "react";
import { Block, State, usePageMemory } from "../../kit";
import type { NlTone } from "../../kit";
import "./parts.css";

// The parts the money pages share (Paper, Capital, Money, Positions, Budget), after the mockup's R2 helpers
// (trading_lab_new_ui.html, lab-r2.js): a share bar, a summary card, and the "Needs you" rows.

export type Hue = NlTone | "";

/** One part of a share bar: its tone, its size, and how strong the colour is (0 to 1). */
export type SharePart = [tone: "good" | "bad" | "warn" | "data" | "soft", size: number, level?: number];

/** A bar split into parts, each as wide as its size; with no size at all it is an empty outline. */
export function Share({ parts, className, label }: { parts: SharePart[]; className?: string; label?: string }) {
  const live = parts.filter((p) => p[1] > 0);
  return (
    <span className={`tp-share${live.length ? "" : " none"}${className ? ` ${className}` : ""}`} role={label ? "img" : undefined} aria-label={label}>
      {live.map(([tone, size, level], i) => (
        <i key={i} className={tone} style={{ flex: size, opacity: level ?? (tone === "soft" ? 0.3 : 0.85) } as CSSProperties} />
      ))}
    </span>
  );
}

export type CardFact = [name: string, value: ReactNode, tone?: "good" | "bad" | "warn" | ""];

/** A summary card: its name with a state word, one written number, a line of words, a picture, and up to four facts. */
export function Card({ name, hue = "", chip, fig, sub, pic, facts, onClick, pressed, hover, testid, word }: {
  name: string; hue?: Hue; chip?: [NlTone, string]; fig: ReactNode; sub?: ReactNode; pic?: ReactNode; facts?: CardFact[];
  onClick?: () => void; pressed?: boolean; hover?: string; testid?: string; word?: boolean;
}) {
  const body = (
    <>
      <span className="tp-top"><span className="tp-name">{name}</span>{chip && <State tone={chip[0]}>{chip[1]}</State>}</span>
      <span className={word ? "tp-fig word" : "tp-fig num"}>{fig}</span>
      {sub != null && <span className="tp-sub">{sub}</span>}
      {pic}
      {facts && facts.length > 0 && (
        <span className="tp-facts">
          {facts.map(([k, v, t]) => <span key={k}><i>{k}</i><b className={t ? `num ${t}` : "num"}>{v}</b></span>)}
        </span>
      )}
    </>
  );
  const cls = `tp-card${hue ? ` h-${hue}` : ""}`;
  return onClick ? (
    <button type="button" className={cls} title={hover} onClick={onClick} aria-pressed={pressed} data-testid={testid}>{body}</button>
  ) : (
    <div className={cls} title={hover} data-testid={testid}>{body}</div>
  );
}

/** The cards of one Summary block. */
export function Wall({ children, n = 4 }: { children: ReactNode; n?: number }) {
  return <div className={`tp-wall n${n}`}>{children}</div>;
}

export type Stop = {
  key: string; tone: "good" | "bad" | "warn" | "dim"; word: string; title: string; detail?: string;
  bar?: ReactNode; cap?: string; btn?: string; act?: () => void;
};

/** What needs Tamer (amber), what is broken (red), what is not wired (grey): one row each, with its button. */
export function Stops({ stops, empty }: { stops: Stop[]; empty: string }) {
  if (stops.length === 0) return <p className="tp-empty">{empty}</p>;
  return (
    <div className="tp-stops" data-testid="needs">
      {stops.map((s) => (
        <div className="tp-stop" key={s.key}>
          <State tone={s.tone}>{s.word}</State>
          <span className="tp-st-t"><b>{s.title}</b>{s.detail && <small>{s.detail}</small>}</span>
          <span className="tp-st-bar">{s.bar}{s.cap && <small>{s.cap}</small>}</span>
          <span className="tp-st-go">{s.btn && <button type="button" className="btn" onClick={s.act}>{s.btn}</button>}</span>
        </div>
      ))}
    </div>
  );
}

/** The Needs you block: the stops, and one state word over them all. */
export function NeedsBlock({ stops, empty, notRead }: { stops: Stop[]; empty: string; notRead?: boolean }) {
  const waits = stops.filter((s) => s.tone !== "dim").length;
  const tone: NlTone = notRead ? "dim" : stops.some((s) => s.tone === "bad") ? "bad" : waits ? "warn" : "good";
  return (
    <Block title="Needs you" state={<State tone={tone}>{notRead ? "Not read" : waits ? `${waits} waiting` : "All clear"}</State>}>
      <Stops stops={stops} empty={empty} />
    </Block>
  );
}

/** Name, big written value, bar: one row of the "against the plan" style lists. */
export function BarRow({ children }: { children: ReactNode }) {
  return <div className="tp-barrow">{children}</div>;
}

export const dotStyle = (rgb: string): CSSProperties => ({ ["--k" as string]: rgb });

/** Hide amounts: one switch for the three Portfolio pages, kept in the tab for a quarter of an hour. */
export function useHide(): [boolean, () => void] {
  const [hide, set] = usePageMemory<boolean>("portfolio/hide", false);
  return [hide, () => set(!hide)];
}

export type GainRow = { label: string; value: number; text: string; hover?: string };

/** One column per period, up for a gain and down for a loss around a zero line, each with its amount written on it. */
export function GainBars({ rows, none }: { rows: GainRow[]; none: string }) {
  if (rows.length === 0) return <p className="tp-empty">{none}</p>;
  const top = Math.max(1, ...rows.map((r) => Math.abs(r.value)));
  return (
    <div className="tp-gb" data-testid="gain-bars">
      {rows.map((r) => {
        const h = Math.max(2, (Math.abs(r.value) / top) * 100);
        return (
          <div key={r.label} className="tp-gb-c" title={r.hover}>
            <span className="tp-gb-up">{r.value >= 0 && <><b className="num">{r.text}</b><i className="good" style={{ height: `${h}%` }} /></>}</span>
            <span className="tp-gb-dn">{r.value < 0 && <><i className="bad" style={{ height: `${h}%` }} /><b className="num">{r.text}</b></>}</span>
            <small>{r.label}</small>
          </div>
        );
      })}
    </div>
  );
}

const STROKE = { good: "#2fe08a", bad: "#ff5a63", data: "#4d9bff", warn: "#ffb020" } as const;

/** A small line with a soft area under it; with fewer than two values it says why there is none. */
export function Pic({ values, tone = "data", none }: { values: number[]; tone?: keyof typeof STROKE; none: string }) {
  if (values.length < 2) return <span className="tp-pic"><span className="none">{none}</span></span>;
  const lo = Math.min(...values), hi = Math.max(...values), pad = (hi - lo) * 0.12 || 1;
  const W = 200, H = 56;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * W, 4 + (1 - (v - (lo - pad)) / (hi + pad - (lo - pad))) * (H - 8)] as const);
  const line = pts.map(([x, y]) => `${x},${y}`).join(" ");
  return (
    <span className="tp-pic" aria-hidden="true">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <polygon points={`0,${H} ${line} ${W},${H}`} fill={STROKE[tone]} opacity=".18" />
        <polyline points={line} stroke={STROKE[tone]} />
      </svg>
    </span>
  );
}

/** Bars around a zero line, green up and red down, one per value. */
export function ZeroBars({ values, none, title }: { values: number[]; none: string; title?: (i: number) => string }) {
  if (values.length === 0) return <span className="tp-pic"><span className="none">{none}</span></span>;
  const W = 200, H = 56, mid = H / 2, top = Math.max(1, ...values.map((v) => Math.abs(v)));
  const slot = W / values.length, bw = Math.min(14, slot * 0.6);
  return (
    <span className="tp-pic">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <line x1="0" x2={W} y1={mid} y2={mid} stroke="#fff" opacity=".2" vectorEffect="non-scaling-stroke" />
        {values.map((v, i) => {
          const h = Math.max(2, (Math.abs(v) / top) * (mid - 3));
          return (
            <rect key={i} x={i * slot + (slot - bw) / 2} width={bw} y={v >= 0 ? mid - h : mid} height={h} rx="2" fill={v >= 0 ? STROKE.good : STROKE.bad} opacity=".9">
              {title && <title>{title(i)}</title>}
            </rect>
          );
        })}
      </svg>
    </span>
  );
}

/** One line for each strategy, drawn from its own start, with the zero line; a line that ends up is green, down is red. */
export function LinesPic({ lines, none }: { lines: Array<{ values: number[]; up: boolean }>; none: string }) {
  const usable = lines.filter((l) => l.values.length >= 2);
  if (usable.length === 0) return <span className="tp-pic"><span className="none">{none}</span></span>;
  const W = 200, H = 56;
  const all = [...usable.flatMap((l) => l.values), 0];
  const lo = Math.min(...all), hi = Math.max(...all), pad = (hi - lo) * 0.12 || 1;
  const Y = (v: number) => 4 + (1 - (v - (lo - pad)) / (hi + pad - (lo - pad))) * (H - 8);
  const span = Math.max(...usable.map((l) => l.values.length));
  return (
    <span className="tp-pic" aria-hidden="true">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <line x1="0" x2={W} y1={Y(0)} y2={Y(0)} stroke="#fff" opacity=".25" vectorEffect="non-scaling-stroke" />
        {usable.map((l, k) => {
          const off = span - l.values.length;
          return <polyline key={k} points={l.values.map((v, i) => `${((i + off) / Math.max(1, span - 1)) * W},${Y(v)}`).join(" ")} stroke={l.up ? STROKE.good : STROKE.bad} opacity=".95" />;
        })}
      </svg>
    </span>
  );
}

/** One bar for each item, as long as its value; the tone says how it stands. */
export function RowsPic({ rows, none }: { rows: Array<{ value: number; tone: "good" | "warn" | "data"; title?: string }>; none: string }) {
  if (rows.length === 0) return <span className="tp-pic"><span className="none">{none}</span></span>;
  const W = 200, H = 56, top = Math.max(1, ...rows.map((r) => r.value));
  const rowH = H / rows.length, h = Math.min(12, rowH - 8);
  return (
    <span className="tp-pic">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {rows.map((r, i) => {
          const y = i * rowH + (rowH - h) / 2;
          return (
            <g key={i}>
              <rect x="0" y={y} width={W} height={h} rx={h / 2} fill="#fff" opacity=".07" />
              <rect x="0" y={y} width={Math.max(h, (r.value / top) * W)} height={h} rx={h / 2} fill={STROKE[r.tone]} opacity=".9">{r.title && <title>{r.title}</title>}</rect>
            </g>
          );
        })}
      </svg>
    </span>
  );
}

/** One bar a day: the part that was refused is amber, a day with nothing is a thin line. */
export function DaysPic({ days, none }: { days: Array<{ ok: number; all: number; title?: string }>; none: string }) {
  if (days.length === 0) return <span className="tp-pic"><span className="none">{none}</span></span>;
  const W = 200, H = 56, top = Math.max(1, ...days.map((d) => d.all)), slot = W / days.length, bw = Math.min(22, slot * 0.56);
  return (
    <span className="tp-pic">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {days.map((d, i) => {
          const x = i * slot + (slot - bw) / 2, h = (d.all / top) * (H - 6);
          if (!d.all) return <rect key={i} x={x} y={H - 2} width={bw} height="2" fill="#fff" opacity=".14" />;
          const refused = (h * (d.all - d.ok)) / d.all;
          return (
            <g key={i}>
              <rect x={x} y={H - h} width={bw} height={h} rx="3" fill={STROKE.good} opacity=".9"><title>{d.title}</title></rect>
              {d.all > d.ok && <rect x={x} y={H - h} width={bw} height={refused} rx="3" fill={STROKE.warn} opacity=".95" />}
            </g>
          );
        })}
      </svg>
    </span>
  );
}

/** The figures of one strategy, written plainly: a name over a value, with an optional line of words under it. */
export function PlainFacts({ facts, sans }: { facts: Array<[name: string, value: ReactNode, tone?: "good" | "bad" | "warn" | "", note?: string]>; sans?: boolean }) {
  return (
    <div className={sans ? "tp-plain sans" : "tp-plain"} data-testid="side-table">
      {facts.map(([k, v, t, n]) => <span key={k}><i>{k}</i><b className={`num${t ? ` ${t}` : ""}`}>{v}</b>{n && <small>{n}</small>}</span>)}
    </div>
  );
}
