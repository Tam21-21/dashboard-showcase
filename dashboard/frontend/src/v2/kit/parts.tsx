import { Fragment, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Icon } from "../shell/icons";
import { GROUP } from "../shell/groups";
import { useStations } from "../data/useStations";
import type { StationId } from "../data/stations";
import { Window } from "./Window";
import { State } from "./Block";
import { PageArrows } from "./pageNav";
import { PhoneNav, useLayout, type PhoneNavValue } from "./phone";

// The pieces every new page is built from. Ported from the kit in
// docs/design/rail_mockup_2026-10-10.html (functions hd, kpis, card, tbl, isNum, track, lc, cols, f4, rws).

export type Tone = "g" | "rd" | "a" | "bl" | "none";

export const GREEN = "#2fe08a";
export const AMBER = "#ffb020";
export const RED = "#ff5a63";

/** A value is a number when it has a digit and no word longer than a unit (of, km, min). */
export function isNum(v: unknown): boolean {
  if (typeof v !== "string" && typeof v !== "number") return false;
  const t = String(v).replace(/<[^>]+>/g, "").replace(/&amp;/g, "&");
  return /\d/.test(t) && !/[A-Za-z]{3,}/.test(t.replace(/\b(of|km|min)\b/g, ""));
}

const join = (...c: Array<string | false | undefined>) => c.filter(Boolean).join(" ");
const toneClass = (t?: Tone) => (t && t !== "none" ? t : "");
const numClass = (v: unknown) => (isNum(v) ? "num" : "");

// The open app's pages as a strip of pills above the title, on a phone only (the mockup's .subnav). When they do not fit on
// one line (Research's nine), the strip is one button with the open page's name; it opens the list of all of them in the
// rail's groups, each with its station word (Tamer 2026-11-15, "lets do A"). A second press on the open app in the bottom
// bar opens the same list (the shell sends "v2-pages").
function PhoneStrip({ nav }: { nav: PhoneNavValue }) {
  const navigate = useNavigate();
  const ref = useRef<HTMLElement>(null);
  const [pick, setPick] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && el.scrollWidth > el.clientWidth + 1) setPick(true);
  }, []);
  useEffect(() => {
    if (!pick) return;
    const onPages = (e: Event) => { e.preventDefault(); setOpen(true); };
    window.addEventListener("v2-pages", onPages);
    return () => window.removeEventListener("v2-pages", onPages);
  }, [pick]);
  const cur = nav.sections.find((s) => s.id === nav.section) ?? nav.sections[0];
  return (
    <>
      <nav ref={ref} className={`subnav${pick ? " picking" : ""}`} aria-label="Pages">
        {pick ? (
          <button type="button" className="pick" aria-haspopup="dialog" onClick={() => setOpen(true)}>
            <Icon name={cur.icon} /><span>{cur.label}</span><Icon name="down" />
          </button>
        ) : nav.sections.map((s) => (
          <button type="button" key={s.id} aria-current={s.id === nav.section ? "page" : undefined}
            onClick={() => navigate(`/app/${nav.app}/${s.id}`)}>
            {s.label}
          </button>
        ))}
      </nav>
      {open && <PagesWindow nav={nav} onClose={() => setOpen(false)} />}
    </>
  );
}

const STATION_OF: Record<string, StationId> = { "data-library": "data", strategies: "strategies", runs: "runs", judge: "judge" };

function PagesWindow({ nav, onClose }: { nav: PhoneNavValue; onClose: () => void }) {
  const navigate = useNavigate();
  const st = new Map(useStations().map((s) => [s.id, s]));
  const name = nav.name;
  return (
    <Window label={`${name} pages`} title={name} size="m" onClose={onClose}>
      <nav className="pg-list" aria-label={`${name} pages`}>
        {nav.sections.map((s) => {
          const station = STATION_OF[s.id] ? st.get(STATION_OF[s.id]) : undefined;
          return (
            <Fragment key={s.id}>
              {GROUP[s.id] && <p className="pg-grp">{GROUP[s.id]}</p>}
              <button type="button" aria-current={s.id === nav.section ? "page" : undefined}
                onClick={() => { onClose(); navigate(`/app/${nav.app}/${s.id}`); }}>
                <Icon name={s.icon} /><span>{s.label}</span>
                {station?.tone && <State tone={station.tone}>{station.word}</State>}
              </button>
            </Fragment>
          );
        })}
      </nav>
    </Window>
  );
}

export function PageHeader({ crumb, title, right, gear = true }: { crumb: string; title: string; right?: ReactNode; gear?: boolean }) {
  const phone = useLayout() === "phone";
  const nav = useContext(PhoneNav);
  return (
    <>
      {phone && nav !== null && <PhoneStrip key={nav.app} nav={nav} />}
      <div className="ph">
        <div>
          <div className="cr">{crumb.toUpperCase()}</div>
          <div className="ttl">
            <h1>{title}</h1>
            {!phone && <PageArrows />}
            {/* The phone has no rail, so Settings stays in reach here (kept from before the match; the mockup has no way there). */}
            {phone && gear && (
              <Link className="phgear" to="/app/settings" aria-label="Settings">
                <Icon name="gear" />
              </Link>
            )}
          </div>
        </div>
        {right}
      </div>
    </>
  );
}

export function Panel({
  title, note, children, style,
}: { title?: ReactNode; note?: ReactNode; children?: ReactNode; style?: CSSProperties }) {
  return (
    <div className="cd" style={style}>
      {title != null && (
        <div className="h2">
          {title}
          {note != null && <span>{note}</span>}
        </div>
      )}
      {children}
    </div>
  );
}

export interface KpiItem {
  label: ReactNode;
  value: string;
  tone?: Tone;
  pill?: string;
  pillTone?: Tone;
  /** A reason shown on hover of the pill. */
  pillHover?: string;
  /** A sparkline or other small chart for the card's bottom strip. */
  spark?: ReactNode;
  /** A reason shown on hover of the value, for a dash. */
  hover?: string;
  /** A state word on the right of the line under the number. */
  state?: string;
  stateTone?: Tone;
  /** A thin visual (KzSeg, KzSteps) for the card; it replaces the spark. */
  viz?: ReactNode;
  /** The footer: label, value and an optional tone for the value. */
  foot?: Array<[string, string, Tone?]>;
}

/** A label in plain case: first letter upper, the rest lower, then Sharpe, S&P and VIX restored. */
function kcase(s: string): string {
  return (s.charAt(0).toUpperCase() + s.slice(1).toLowerCase())
    .replace(/\bsharpe\b/gi, "Sharpe")
    .replace(/\bs&p\b/gi, "S&P")
    .replace(/\bvix\b/gi, "VIX");
}

export function Kpis({ items }: { items: KpiItem[] }) {
  return (
    <div className="kp">
      {items.map((k, i) => (
        <div className="cd ktl" key={i}>
          <div className="kl">{typeof k.label === "string" ? kcase(k.label) : k.label}</div>
          <div className="kv">
            <span className={numClass(k.value) || undefined} title={k.hover}>{k.value}</span>
          </div>
          <div className="mch">
            {k.pill != null ? (
              <span className={join("pill", toneClass(k.pillTone ?? k.tone), numClass(k.pill))} title={k.pillHover}>{k.pill}</span>
            ) : (
              <span />
            )}
            {k.state != null && <span className={join("state", toneClass(k.stateTone))}>{k.state}</span>}
          </div>
          {k.viz != null ? (
            <div className="kviz">{k.viz}</div>
          ) : k.spark != null ? (
            <div className="sp">{k.spark}</div>
          ) : (
            <div style={{ height: 16 }} />
          )}
          {k.foot != null ? (
            <div className="mft num">
              {k.foot.map(([l, v, c], j) => (
                <span key={j}>{l} <b className={toneClass(c) || undefined}>{v}</b></span>
              ))}
            </div>
          ) : (
            <div style={{ height: 10 }} />
          )}
        </div>
      ))}
    </div>
  );
}

export type Cell = ReactNode | { v: ReactNode; cls?: string };

function asCell(c: Cell): { v: ReactNode; cls?: string } {
  if (typeof c === "object" && c !== null && "v" in c) return c as { v: ReactNode; cls?: string };
  return { v: c as ReactNode };
}

export function Table({ head, rows }: { head: ReactNode[]; rows: Cell[][] }) {
  return (
    <table>
      <thead>
        <tr>
          {head.map((h, i) => (
            <th key={i}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((raw, j) => {
              const c = asCell(raw);
              return (
                <td key={j} className={join(c.cls, j > 0 && numClass(c.v)) || undefined}>
                  {c.v}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Facts({ items }: { items: Array<[ReactNode, string, string?, string?]> }) {
  return (
    <div className="f4">
      {items.map(([k, v, c, hover], i) => (
        <div key={i}>
          <span>{k}</span>
          <b style={{ fontWeight: 400 }} className={join(c, numClass(v)) || undefined} title={hover}>
            {v}
          </b>
        </div>
      ))}
    </div>
  );
}

export function Rows({ items }: { items: Cell[][] }) {
  return (
    <div className="rows">
      {items.map((r, i) => (
        <div key={i}>
          {r.map((raw, j) => {
            const c = asCell(raw);
            return (
              <span key={j} className={join(c.cls, numClass(c.v)) || undefined}>
                {c.v}
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

export interface ColItem {
  /** Colour of the thin cap on top of the column. */
  cap: string;
  name: ReactNode;
  state?: string;
  stateTone?: Tone;
  big?: string;
  bigTone?: Tone;
  /** A reason shown on hover of the big value, for a dash. */
  bigHover?: string;
  sub?: ReactNode;
  body?: ReactNode;
  /** When set, the whole column is a link to this path. */
  to?: string;
}

export function Cols({ items }: { items: ColItem[] }) {
  return (
    <div className="cols">
      {items.map((x, i) => {
        const inner = (
          <>
            <div className="cap" style={{ background: x.cap }} />
            <div className="in">
              <div className="nm">
                {x.name}
                {x.state != null && <span className={toneClass(x.stateTone) || undefined}>{x.state}</span>}
              </div>
              {x.big != null && <div className={join("r", toneClass(x.bigTone), numClass(x.big))} title={x.bigHover}>{x.big}</div>}
              {x.sub != null && <div className="p">{x.sub}</div>}
              {x.body}
            </div>
          </>
        );
        return x.to ? (
          <Link className="cd col" key={i} to={x.to} style={{ display: "block", color: "inherit", textDecoration: "none" }}>{inner}</Link>
        ) : (
          <div className="cd col" key={i}>{inner}</div>
        );
      })}
    </div>
  );
}

/** The mockup's lc: green under 60 percent, amber to 80, red above. */
export function loadColour(f: number): string {
  return f > 0.8 ? RED : f > 0.6 ? AMBER : GREEN;
}

export function Track({ fraction, colour }: { fraction: number; colour?: string }) {
  const pct = Math.max(2, Math.min(100, fraction * 100));
  return (
    <span className="track">
      <b style={{ width: `${pct}%`, background: colour ?? loadColour(fraction) }} />
    </span>
  );
}

export function StateWord({ word, tone }: { word: string; tone: Tone }) {
  return <span className={join("state", toneClass(tone))}>{word}</span>;
}

export function Segmented({
  options, value, onChange, variant = "pills",
}: {
  options: Array<{ label: string; value: string; disabled?: boolean; hover?: string }>;
  value: string;
  onChange: (value: string) => void;
  variant?: "pills" | "seg";
}) {
  return (
    <div className={variant}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={o.value === value ? "on" : undefined}
          aria-pressed={o.value === value}
          disabled={o.disabled}
          title={o.hover}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A bar around zero: it grows right (green) or left (red) from the middle; empty with no value. */
export function ZeroBar({ value, widest }: { value: number | null; widest: number }) {
  return (
    <span className="dbar">
      {value !== null && widest > 0 && <b className={value < 0 ? "neg" : "pos"} style={{ width: `${(Math.abs(value) / widest) * 50}%` }} />}
    </span>
  );
}

/** What an item of a counted, pressable Legend adds: a key to pick it by, a count and a percent. */
export interface LegendExtra { key?: string; count?: number; percent?: string }

/**
 * The colour key of a chart: a swatch and a name per item. An item may carry a count and a percent (drawn as
 * two number spans, the box then gets the class lgd-n); with onPick each item is a button with data-key, and
 * the one whose key is active has the class on.
 */
export function Legend({ items, active = null, onPick }: {
  items: Array<[string, string] | [string, string, LegendExtra]>;
  active?: string | null;
  onPick?: (key: string) => void;
}) {
  const counted = items.some((it) => it[2]?.count !== undefined);
  return (
    <div className={counted ? "lgd lgd-n" : "lgd"}>
      {items.map(([name, colour, extra]) => {
        const key = extra?.key ?? name;
        const inner = (
          <>
            <i style={{ background: colour }} />
            {counted ? <span>{name}</span> : name}
            {extra?.count !== undefined && <span className="num">{extra.count.toLocaleString("en-US")}</span>}
            {extra?.percent !== undefined && <span className="num">{extra.percent}</span>}
          </>
        );
        if (!onPick) return <span key={key}>{inner}</span>;
        return (
          <button key={key} type="button" data-key={key} className={active === key ? "on" : undefined} onClick={() => onPick(key)}>
            {inner}
          </button>
        );
      })}
    </div>
  );
}
