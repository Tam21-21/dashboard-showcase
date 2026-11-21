import type { ReactNode } from "react";
import type { BudgetCategory, BudgetMonth, LastMonthCategory } from "../../data/moneyApi";

// The Money and Budget pages' drawings and formatters. Charts are SVG with a hover on every mark
// (an SVG <title>), after docs/design/rail_mockup_2026-10-10.html (bBullets, bSankey, b12, kzTrack, kzBars).
// Each figure's method is in docs/research_lab/display_methods.md, "Portfolio: Money and Budget".

export const GREEN = "#2fe08a";
export const AMBER = "#ffb020";
export const RED = "#ff5a63";
export const GREY = "#c3cbd6";
const TEXT = "#8d98a8";
const INK = "#d4dbe6";
const THIN = ",";
const MONO = "Azeret Mono,monospace";
const SANS = "Instrument Sans,sans-serif";

// ------------------------------------------------------------------ formatters

/** A whole-euro amount with the sign and thin-space thousands: €1 413, -€320. */
export function eur(n: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (n == null || Number.isNaN(n)) return "-";
  const r = Math.round(Math.abs(n));
  const body = String(r).replace(/\B(?=(\d{3})+(?!\d))/g, THIN);
  const sign = n < 0 && r > 0 ? "−" : opts.sign && r > 0 ? "+" : "";
  return `${sign}€${body}`;
}

/**
 * The y-axis text of the net worth line. It rebuilds the tick step LineChart's lineTicks() will pick
 * for these values, then shows every tick with as many digits as the step needs, so two ticks never read the same.
 */
export function netAxisFormat(values: number[]): (v: number) => string {
  const finite = values.filter((v) => Number.isFinite(v));
  const span = finite.length >= 2 ? Math.max(...finite) - Math.min(...finite) : 0;
  const padded = span === 0 ? 2 : span * 1.24;
  const s0 = padded / 4;
  const p = 10 ** Math.floor(Math.log10(s0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * p).find((x) => x >= s0 - 1e-9) as number;
  if (step >= 1000) {
    return (v) => {
      const k = Math.round(Math.abs(v) / 1000);
      return `${v < 0 && k > 0 ? "-" : ""}€${k}k`;
    };
  }
  const decimals = Math.max(0, Math.ceil(-Math.log10(+step.toFixed(8))));
  return (v) => {
    const fixed = Math.abs(v).toFixed(decimals);
    const [whole, frac] = fixed.split(".");
    const body = whole.replace(/\B(?=(\d{3})+(?!\d))/g, THIN) + (frac ? `.${frac}` : "");
    const sign = v < 0 && Number(fixed) !== 0 ? "-" : "";
    return `${sign}€${body}`;
  };
}

export const pct0 = (f: number): string => `${Math.round(f * 100)}%`;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09" to "Sep"; the text is the month part only. */
export function monthName(month: string): string {
  const n = Number(month.slice(5, 7));
  return MONTHS[n - 1] ?? month;
}

function parisParts(iso: string): Record<string, string> {
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Paris", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const out: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date(iso))) out[p.type] = p.value;
  return out;
}

/** "22 Oct, 20:00" in Paris time. */
export function parisStamp(iso: string): string {
  const p = parisParts(iso);
  return `${p.day} ${p.month}, ${p.hour}:${p.minute}`;
}

/** The Paris calendar month of an instant, as "2026-10". */
export function parisMonth(iso: string): string {
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Paris", year: "numeric", month: "2-digit" });
  const parts: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date(iso))) parts[p.type] = p.value;
  return `${parts.year}-${parts.month}`;
}

/** The hover of a card: where the figure comes from and when it was read. */
export function readTitle(source: string, readAt: string | null): string {
  return readAt ? `${source}. Read from Finary ${parisStamp(readAt)}` : source;
}

export const categoryLabel = (label: string | null): string => (label && label.trim() ? label : "Unnamed category");

// ------------------------------------------------------------------ card pictures

const KW = 260;
const KH = 36;

function Kz({ children }: { children: ReactNode }) {
  return (
    <div className="kz">
      <svg viewBox={`0 0 ${KW} ${KH}`} fontFamily={MONO}>{children}</svg>
    </div>
  );
}

/** A value on a track, with named marks (the plan) and a hover. */
export function KzTrack({
  value, max, colour, marks = [], title,
}: { value: number; max: number; colour: string; marks?: Array<[number, string]>; title: string }) {
  const top = max > 0 ? max : 1;
  const X = (v: number) => 1 + Math.max(0, Math.min(1, v / top)) * (KW - 2);
  return (
    <Kz>
      <g>
        <title>{title}</title>
        <rect x={1} y={7} width={KW - 2} height={6} rx={3} fill="rgba(255,255,255,.08)" />
        <rect x={1} y={7} width={Math.max(4, X(value) - 1)} height={6} rx={3} fill={colour} />
        {marks.map(([v, l], i) => (
          <g key={i}>
            <line x1={X(v)} x2={X(v)} y1={3} y2={17} stroke="#eef2f7" strokeWidth={1.6} />
            <text x={Math.min(KW - 24, X(v) - 4)} y={30} fontSize={10} fill={TEXT} letterSpacing={0.6}>{l}</text>
          </g>
        ))}
      </g>
    </Kz>
  );
}

/** One bar per item, up for a gain and down for a loss, each with its hover. */
export function KzBars({ bars }: { bars: Array<{ v: number; label: string; title: string }> }) {
  const n = bars.length;
  const hasNeg = bars.some((b) => b.v < 0);
  const base = hasNeg ? 20 : 30;
  const up = hasNeg ? 16 : 26;
  const down = 14;
  const mx = Math.max(1, ...bars.map((b) => Math.abs(b.v)));
  const bw = KW / n;
  return (
    <Kz>
      {bars.map((b, i) => {
        const h = Math.max(1.5, (Math.abs(b.v) / mx) * (b.v < 0 ? down : up));
        return (
          <g key={i}>
            <title>{b.title}</title>
            <rect x={i * bw + 2} y={b.v < 0 ? base : base - h} width={Math.max(2, bw - 4)} height={h} rx={2} fill={b.v < 0 ? RED : GREEN} />
          </g>
        );
      })}
    </Kz>
  );
}

// ------------------------------------------------------------------ this month against the plan

const BW = 880;
const BL = 190;
const BR = 200;
const LABEL_MAX = 24;

/** A label cut into lines of at most 24 characters: at the last space within them, else hard at 24. Nothing is dropped. */
export function wrapLabel(label: string): string[] {
  const lines: string[] = [];
  let rest = label.trim();
  while (rest.length > LABEL_MAX) {
    const at = rest.lastIndexOf(" ", LABEL_MAX - 1);
    const cut = at > 0 ? at : LABEL_MAX;
    lines.push(rest.slice(0, cut).trimEnd());
    rest = rest.slice(cut).trimStart();
  }
  lines.push(rest);
  return lines;
}

/** A row is 38 high for one or two lines, 14 more for each further line. */
const rowHeight = (lines: number): number => 38 + Math.max(0, lines - 2) * 14;

/** Every number and word of one plan row, so the wide picture and the phone list can never disagree. */
interface PlanFacts {
  label: string;
  lines: string[];
  colour: string;
  hasTarget: boolean;
  spent: string;
  of: string | null;
  second: { text: string; colour: string } | null;
  hover: string;
  fSpent: number;
  fPlan: number | null;
  fPace: number | null;
}

function planFacts(c: BudgetCategory, elapsed: number): PlanFacts {
  const label = categoryLabel(c.label);
  const hasTarget = c.target !== null;
  const scale = (hasTarget ? (c.target as number) * 1.25 : Math.max(c.spent, c.suggestion ?? 0, 1) * 1.25) || 1;
  const F = (v: number) => Math.max(0, Math.min(1, v / scale));
  const secondText = hasTarget ? (c.warn ? (c.ahead === null ? "AHEAD" : `AHEAD ${Math.round(c.ahead * 100)}%`) : "") : null;
  const hover = hasTarget
    ? `${label}: ${eur(c.spent)} spent of ${eur(c.target)} planned, by day ${elapsed} the pace is ${eur(c.pace)}` +
      (c.warn ? (c.ahead === null ? ", ahead of pace" : `, ${Math.round(c.ahead * 100)}% ahead of pace`) : "")
    : `${label}: ${eur(c.spent)} spent, no plan set` +
      (c.suggestion !== null ? `, the 12-month average is ${eur(c.suggestion)}` : ", no 12-month average yet");
  return {
    label,
    lines: wrapLabel(label),
    colour: c.warn ? RED : hasTarget ? GREEN : GREY,
    hasTarget,
    spent: eur(c.spent),
    of: hasTarget ? ` of ${eur(c.target)}` : null,
    second: secondText
      ? { text: secondText, colour: RED }
      : !hasTarget
        ? { text: c.suggestion !== null ? `average ${eur(c.suggestion)}` : "average -", colour: GREY }
        : null,
    hover: c.label && c.label.trim() ? hover : `${hover} (${c.slug})`,
    fSpent: F(c.spent),
    fPlan: hasTarget ? F(c.target as number) : null,
    fPace: hasTarget && c.pace !== null ? F(c.pace) : null,
  };
}

function PlanKey({ categories, elapsed, total }: { categories: BudgetCategory[]; elapsed: number; total: number }) {
  const onPace = categories.filter((c) => c.target !== null && !c.warn).length;
  const ahead = categories.filter((c) => c.warn).length;
  const noPlan = categories.filter((c) => c.target === null).length;
  const sw = (style: Record<string, string>) => <i className="mn-sw" style={style} />;
  return (
    <div className="mn-key">
      <span>{sw({ width: "2px", background: "#eef2f7" })}Plan</span>
      <span>{sw({ width: "0", borderLeft: "1.4px dashed #eef2f7" })}{`Where day ${elapsed} of ${total} should be`}</span>
      <span>{sw({ background: GREEN })}{`On pace ${onPace}`}</span>
      <span>{sw({ background: RED })}{`Ahead of pace ${ahead}`}</span>
      <span>{sw({ background: GREY })}{`No plan ${noPlan}`}</span>
    </div>
  );
}

/** Spent bar, solid plan mark, dashed pace mark, and AHEAD in red when a category runs more than 10% ahead. */
export function PlanBullets({
  categories, elapsed, total,
}: { categories: BudgetCategory[]; elapsed: number; total: number }) {
  const T = BW - BL - BR;
  let y = 0;
  const rows = categories.map((c) => {
    const f = planFacts(c, elapsed);
    const top = y;
    y += rowHeight(f.lines.length);
    const X = (frac: number) => BL + frac * T;
    return (
      <g key={c.slug} data-slug={c.slug} transform={`translate(0 ${top})`}>
        <title>{f.hover}</title>
        <text className="pb-lab" x={0} y={f.lines.length > 1 ? 10 : 16} fontSize={13} fill={INK} fontFamily={SANS}>
          {f.lines.map((line, k) => (
            <tspan key={k} x={0} y={f.lines.length > 1 ? 10 + k * 14 : 16}>{line}</tspan>
          ))}
        </text>
        <rect x={BL} y={6} width={T} height={12} rx={4} fill="rgba(255,255,255,.06)" />
        <rect x={BL} y={6} width={Math.max(4, X(f.fSpent) - BL)} height={12} rx={4} fill={f.colour} opacity={0.9} />
        {f.fPace !== null && (
          <line className="pace-mark" x1={X(f.fPace)} x2={X(f.fPace)} y1={2} y2={22} stroke="#eef2f7" strokeWidth={1.4} strokeDasharray="2 2" />
        )}
        {f.fPlan !== null && <line className="plan-mark" x1={X(f.fPlan)} x2={X(f.fPlan)} y1={1} y2={23} stroke="#eef2f7" strokeWidth={2} />}
        <text x={BW - BR + 14} y={13} fontSize={12} fill={INK} fontFamily={MONO}>
          {f.spent}
          {f.of !== null && <tspan fill={TEXT}>{f.of}</tspan>}
        </text>
        {f.second && <text x={BW - BR + 14} y={29} fontSize={11} fill={f.second.colour} fontFamily={MONO}>{f.second.text}</text>}
      </g>
    );
  });
  return (
    <>
      <svg viewBox={`0 0 ${BW} ${categories.length === 0 ? 38 : y}`} style={{ width: "100%", height: "auto", display: "block" }}>
        {rows}
      </svg>
      <PlanKey categories={categories} elapsed={elapsed} total={total} />
    </>
  );
}

/** The same rows as PlanBullets for a phone: one block per category, the label and amount on one line, the bar under it. */
export function PlanRows({
  categories, elapsed, total,
}: { categories: BudgetCategory[]; elapsed: number; total: number }) {
  if (categories.length === 0) return <p className="empty">No spending categories this month.</p>;
  const pc = (f: number) => `${f * 100}%`;
  return (
    <>
      <div className="pr-rows">
        {categories.map((c) => {
          const f = planFacts(c, elapsed);
          return (
            <div className="pr-row" key={c.slug} data-slug={c.slug} title={f.hover}>
              <div className="pr-top">
                <span className="pr-lab">{f.label}</span>
                <span className="pr-amt">
                  {f.spent}
                  {f.of !== null && <span className="pr-of">{f.of}</span>}
                </span>
              </div>
              <div className="pr-track">
                <i className="pr-fill" style={{ width: pc(f.fSpent), background: f.colour }} />
                {f.fPace !== null && <i className="pr-pace" style={{ left: pc(f.fPace) }} />}
                {f.fPlan !== null && <i className="pr-plan" style={{ left: pc(f.fPlan) }} />}
              </div>
              {f.second && <div className="pr-sec" style={{ color: f.second.colour }}>{f.second.text}</div>}
            </div>
          );
        })}
      </div>
      <PlanKey categories={categories} elapsed={elapsed} total={total} />
    </>
  );
}

// ------------------------------------------------------------------ where last month's money went

const HUES = ["#29c6ff", "#a78bfa", "#ff8fd0", "#e8d49a", "#ff9a3d", "#7fd9a8", "#4d9bff", "#f0959a"];

interface FlowNode {
  n: string;
  v: number;
  y: number;
  h: number;
  off: number;
  colour: string;
  slug: string;
  kind: "income" | "spend" | "saved";
}

/** Income categories flow into spending categories in proportion to their size; Saved is what is left. */
export function MoneyFlow({
  categories, income, expenses,
}: { categories: LastMonthCategory[]; income: number; expenses: number }) {
  const inc = categories.filter((c) => c.income > 0).sort((a, b) => b.income - a.income);
  const out = categories.filter((c) => c.spent > 0).sort((a, b) => b.spent - a.spent);
  if (inc.length === 0 && out.length === 0) {
    return <div className="empty" data-testid="flow">No money in or out was recorded for last month.</div>;
  }
  const saved = income - expenses;
  const hasSaved = saved > 0;
  const sumIn = inc.reduce((a, c) => a + c.income, 0);
  const sumOut = out.reduce((a, c) => a + c.spent, 0);
  const T = Math.max(sumIn, sumOut + (hasSaved ? saved : 0), 1);
  const W = 600;
  const H = 300;
  const gap = 6;
  const LX = 150;
  const RX = W - 170;
  const NW = 10;
  const rightCount = out.length + (hasSaved ? 1 : 0);
  const k = (H - gap * (Math.max(inc.length, rightCount) - 1)) / T;

  let yl = 0;
  const left: FlowNode[] = inc.map((c) => {
    const node = { n: categoryLabel(c.label), v: c.income, y: yl, h: c.income * k, off: 0, colour: GREY, slug: c.slug, kind: "income" as const };
    yl += node.h + gap;
    return node;
  });
  let yr = 0;
  const right: FlowNode[] = out.map((c, i) => {
    const node = { n: categoryLabel(c.label), v: c.spent, y: yr, h: c.spent * k, off: 0, colour: HUES[i % HUES.length], slug: c.slug, kind: "spend" as const };
    yr += node.h + gap;
    return node;
  });
  if (hasSaved) right.push({ n: "Saved", v: saved, y: yr, h: saved * k, off: 0, colour: GREEN, slug: "saved", kind: "saved" });

  const flows: ReactNode[] = [];
  const mx = (LX + NW + RX) / 2;
  right.forEach((r) => {
    left.forEach((l) => {
      const amount = (l.v * r.v) / T;
      const hh = amount * k;
      if (hh < 0.2) return;
      const y0 = l.y + l.off;
      const y1 = r.y + r.off;
      l.off += hh;
      r.off += hh;
      flows.push(
        <path
          key={`${l.slug}-${r.slug}`}
          d={`M${LX + NW},${y0} C${mx},${y0} ${mx},${y1} ${RX},${y1} L${RX},${y1 + hh} C${mx},${y1 + hh} ${mx},${y0 + hh} ${LX + NW},${y0 + hh}Z`}
          fill={r.colour}
          opacity={0.28}
        >
          <title>{`${l.n} to ${r.n}: ${eur(amount)}`}</title>
        </path>,
      );
    });
  });

  let lastY = -99;
  const rightLabels = right.map((r) => {
    const y = Math.max(lastY + 15, r.y + Math.max(r.h, 2) / 2 + 4);
    lastY = y;
    return (
      <g key={`rl-${r.slug}`}>
        <text x={RX + 18} y={y} fontSize={12} fill={INK}>
          {r.n}
          <tspan fill={TEXT}>{` ${eur(r.v)}`}</tspan>
        </text>
      </g>
    );
  });

  return (
    <div data-testid="flow">
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} fontFamily={MONO}>
        {flows}
        {left.map((l) => (
          <g key={`l-${l.slug}`}>
            <rect data-node={l.kind} x={LX} y={l.y} width={NW} height={Math.max(2, l.h)} rx={2} fill={GREY}>
              <title>{`${l.n}: ${eur(l.v)} came in`}</title>
            </rect>
            <text x={LX - 10} y={l.y + l.h / 2 + 4} textAnchor="end" fontSize={12} fill={INK}>{l.n}</text>
            <text x={LX - 10} y={l.y + l.h / 2 + 19} textAnchor="end" fontSize={11} fill={TEXT}>{eur(l.v)}</text>
          </g>
        ))}
        {right.map((r) => (
          <rect
            key={`r-${r.slug}`}
            data-node={r.kind}
            x={RX}
            y={r.y}
            width={NW}
            height={Math.max(2, r.h)}
            rx={2}
            fill={r.colour}
          >
            <title>{`${r.n}: ${eur(r.v)}${r.kind === "saved" ? " kept" : " spent"}`}</title>
          </rect>
        ))}
        {rightLabels}
      </svg>
      {inc.length === 0 && <div className="mn-note">No income categories last month</div>}
      {!hasSaved && saved < 0 && <div className="mn-note rd">Spending was larger than income</div>}
    </div>
  );
}

// ------------------------------------------------------------------ twelve months in and out

function niceStep(raw: number): number {
  const p = 10 ** Math.floor(Math.log10(raw));
  const m = raw / p;
  return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p;
}

/** Each bar is a month's income: grey the part spent, green the part kept, red when more went out than came in. */
export function InOutBars({ months }: { months: BudgetMonth[] }) {
  if (months.length === 0) return <div className="empty">No months were recorded yet.</div>;
  const W = 600;
  const H = 250;
  const B = 26;
  const LEFT = 40;
  const mx = Math.max(1, ...months.map((m) => Math.max(m.income, m.expenses)));
  const step = niceStep(mx / 4);
  const Y = (v: number) => H - B - (v / mx) * (H - B - 14);
  const ticks: number[] = [];
  for (let v = step; v <= mx * 1.0001; v += step) ticks.push(v);
  const bw = (W - LEFT) / months.length;
  const tick = (v: number) => (v >= 1000 ? `${+(v / 1000).toFixed(1)}k` : String(v));
  const over = months.filter((m) => m.expenses > m.income).length;
  const spentSum = months.reduce((a, m) => a + Math.min(m.income, m.expenses), 0);
  const keptSum = months.reduce((a, m) => a + Math.max(0, m.income - m.expenses), 0);
  return (
    <>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} fontFamily={MONO}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={LEFT} x2={W} y1={Y(v)} y2={Y(v)} stroke="rgba(255,255,255,.06)" />
            <text x={LEFT - 6} y={Y(v) + 4} textAnchor="end" fontSize={10} fill={TEXT}>{tick(v)}</text>
          </g>
        ))}
        {months.map((m, j) => {
          const x = LEFT + j * bw + bw * 0.18;
          const w = bw * 0.64;
          const kept = m.income - m.expenses;
          const low = Math.min(m.income, m.expenses);
          const name = monthName(m.month);
          const hover =
            `${name}${m.complete ? "" : " so far"}: in ${eur(m.income)}, out ${eur(m.expenses)}, ` +
            (kept >= 0 ? `kept ${eur(kept)}${m.income > 0 ? ` (${pct0(kept / m.income)})` : ""}` : `spent ${eur(-kept)} more than came in`);
          return (
            <g key={m.month} opacity={m.complete ? 1 : 0.55} data-month={m.month}>
              <title>{hover}</title>
              <rect x={x} y={Y(low)} width={w} height={Math.max(0, H - B - Y(low))} rx={3} fill={GREY} opacity={0.5} />
              {kept >= 0 ? (
                <rect x={x} y={Y(m.income)} width={w} height={Math.max(0, Y(m.expenses) - Y(m.income) - 2)} rx={3} fill={GREEN} opacity={0.85} />
              ) : (
                <rect x={x} y={Y(m.expenses)} width={w} height={Math.max(0, Y(m.income) - Y(m.expenses) - 2)} rx={3} fill={RED} />
              )}
              <text x={x + w / 2} y={H - 8} textAnchor="middle" fontSize={10} fill={TEXT}>{name.toUpperCase()}</text>
            </g>
          );
        })}
      </svg>
      <div className="mn-key">
        <span><i className="mn-sw" style={{ background: GREY }} />{`Spent ${eur(spentSum)}`}</span>
        <span><i className="mn-sw" style={{ background: GREEN }} />{`Kept ${eur(keptSum)}`}</span>
        <span><i className="mn-sw" style={{ background: RED }} />{`Spent more than came in ${over}`}</span>
      </div>
    </>
  );
}
