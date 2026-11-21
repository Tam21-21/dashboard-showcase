import { useEffect, useReducer, useRef, useState } from "react";
import type { CSSProperties, ComponentProps, ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../../../../lib/api";
import { plainDetail, serverDetail } from "../../../../lib/errorText";
import type { FundamentalsHistory } from "../../../../types/research";
import { fetchHistory, fetchStatus, fundamentalsKeys, startFetch } from "../../../data/fundamentalsApi";
import { Block, Segmented, State, useBoxSize, useLayout } from "../../../kit";
import { lineTicks } from "../../../kit/charts";
import { LibCard } from "../markets/LibCard";
import { dayWord } from "./names";

// The company accounts of the ticker lookup (mockup: the frozen mockup of 2026-11-18, accountsParts and fillAccounts; display_methods.md,
// "Ticker lookup window" and "The company, from its filings"). Every amount is a figure the company filed and every ratio is the
// server's; the page works out only the year-on-year change. A figure the answer does not hold is a dash, never an error.
// Also here: the one SEC fetch a company not held starts, and the following of it until the figures are in.

const DASH = "-";
const POLL_MS = 10_000;
const ENDED = new Set(["succeeded", "succeeded_with_quarantines", "failed", "cancelled_partial"]);

// ---- The fetch of a company not held ----

export type Failure = { words: string; hover?: string };
type Drive = { k: "idle" } | { k: "follow"; id: number } | { k: "starting" } | { k: "ours" } | { k: "settling" } | { k: "done" };

/**
 * The history of one company and, when it is not held, the single SEC fetch that fills it. Once per opening (refs, never a
 * re-render): a batch that is already running is followed, else one fetch is started and its own batch is the one whose id was
 * not there before. Status and history are read again every 10 s while waiting, and stop when the window closes.
 */
export function useAccountsAnswer(cik: number, ticker: string, short: string) {
  const qc = useQueryClient();
  const stopped = useRef(false);
  const drive = useRef<Drive>({ k: "idle" });
  const kept = useRef<Set<number>>(new Set());
  const startedOurs = useRef(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [nudged, nudge] = useReducer((n: number) => n + 1, 0);
  const history = useQuery({
    queryKey: fundamentalsKeys.history(cik),
    queryFn: () => fetchHistory(cik, ticker),
    retry: false,
    refetchInterval: (q) => (q.state.data && !q.state.data.on_record && !stopped.current ? POLL_MS : false),
  });
  const notHeld = history.data?.on_record === false;
  const status = useQuery({ queryKey: fundamentalsKeys.status, queryFn: fetchStatus, enabled: notHeld && !failure, refetchInterval: POLL_MS, retry: false });
  const statusWords = notHeld && status.isError ? serverDetail(status.error) : null;

  useEffect(() => {
    if (!notHeld || failure || !status.data) return;
    const rows = status.data.recent_batches;
    const giveUp = (f: Failure) => { stopped.current = true; drive.current = { k: "done" }; setFailure(f); };
    const startOurs = (seen: typeof rows) => {
      if (startedOurs.current) return;
      startedOurs.current = true;
      kept.current = new Set(seen.map((r) => r.batch_id));
      drive.current = { k: "starting" };
      startFetch({ ciks: [cik] }).then(
        () => {
          drive.current = { k: "ours" };
          void qc.invalidateQueries({ queryKey: fundamentalsKeys.status });
          nudge();
        },
        (err: unknown) => {
          if (!(err instanceof ApiError && err.status === 409)) { giveUp({ words: serverDetail(err) }); return; }
          startedOurs.current = false;
          qc.fetchQuery({ queryKey: fundamentalsKeys.status, queryFn: fetchStatus, staleTime: 0 }).then(
            (s) => {
              const running = s.recent_batches.find((r) => r.status === "running");
              if (running) { drive.current = { k: "follow", id: running.batch_id }; nudge(); } else giveUp({ words: serverDetail(err) });
            },
            (e: unknown) => giveUp({ words: serverDetail(e) }),
          );
        },
      );
    };
    const settle = async (seen: typeof rows, ours: boolean, failureClass: string | null) => {
      try {
        const h = await qc.fetchQuery({ queryKey: fundamentalsKeys.history(cik), queryFn: () => fetchHistory(cik, ticker), staleTime: 0 });
        if (h.on_record) { drive.current = { k: "done" }; return; }
      } catch (err: unknown) {
        giveUp({ words: serverDetail(err) });
        return;
      }
      if (ours) giveUp({ words: `The fetch from the SEC ended without figures for ${short}.`, hover: failureClass ? plainDetail(failureClass) : undefined });
      else { drive.current = { k: "idle" }; startOurs(seen); }
    };
    const d = drive.current;
    if (d.k === "idle") {
      const running = rows.find((r) => r.status === "running");
      if (running) drive.current = { k: "follow", id: running.batch_id };
      else startOurs(rows);
    } else if (d.k === "follow") {
      const row = rows.find((r) => r.batch_id === d.id);
      if (row && ENDED.has(row.status)) { drive.current = { k: "settling" }; void settle(rows, false, row.failure_class); }
    } else if (d.k === "ours") {
      const row = rows.find((r) => !kept.current.has(r.batch_id));
      if (row && ENDED.has(row.status)) { drive.current = { k: "settling" }; void settle(rows, true, row.failure_class); }
    }
  }, [status.data, notHeld, failure, nudged, cik, ticker, short, qc]);

  return { history, notHeld, failure: failure ?? (statusWords ? { words: statusWords } : null) };
}

// ---- The figures ----

export const usd = (v: number | null, unit = "USD"): string => {
  if (v === null) return DASH;
  const s = v < 0 ? "−" : "";
  const a = Math.abs(v);
  if (unit.startsWith("USD/")) return `${s}$${a.toFixed(2)}`;
  return a >= 1e9 ? `${s}$${(a / 1e9).toFixed(1)}bn` : `${s}$${(a / 1e6).toFixed(0)}m`;
};
const pct = (c: number) => `${c > 0 ? "+" : ""}${c.toFixed(1)}%`;
const fy = (d: string) => d.slice(0, 4);
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

type Model = {
  h: FundamentalsHistory;
  per: string[];
  np: number;
  F: (k: string) => Array<number | null>;
  unit: (k: string) => string;
  nameOf: (k: string) => string;
  filedOn: (k: string, i: number) => string | null;
  valueAt: (k: string, i: number) => FundamentalsHistory["concepts"][number]["values"][number] | undefined;
  filedLast: string;
};

function buildModel(h: FundamentalsHistory): Model {
  const per = [...h.periods].reverse();
  const by = Object.fromEntries(h.concepts.map((c) => [c.concept, c]));
  const valueAt = (k: string, i: number) => by[k]?.values.find((v) => v.period_end === per[i]);
  const filedLast = h.concepts.reduce((m, c) => (c.values.length && c.values[0].filing_date > m ? c.values[0].filing_date : m), "");
  return {
    h, per, np: per.length,
    F: (k) => per.map((_p, i) => valueAt(k, i)?.value ?? null),
    unit: (k) => by[k]?.unit ?? "USD",
    nameOf: (k) => (k === "eps_diluted" ? "Earnings per share" : by[k]?.label ?? k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " ")),
    filedOn: (k, i) => valueAt(k, i)?.filing_date ?? null,
    valueAt, filedLast,
  };
}

/** Year-on-year change in percent: only against a positive year before. */
const yoy = (v: Array<number | null>, i: number) => (i > 0 && v[i] !== null && v[i - 1] !== null && (v[i - 1] as number) > 0 ? ((v[i] as number) / (v[i - 1] as number) - 1) * 100 : null);

const SRC = (m: Model) => `Source: the company's yearly reports to the SEC (10-K), the latest filing for each fiscal year; newest filed ${m.filedLast ? dayWord(m.filedLast) : DASH}.`;

const INCOME: Row[] = [["revenue", "in"], ["gross_profit", "in", ["cost_of_revenue"], "Less"], ["operating_income", "in", ["research_development", "sga"], "Less"], ["net_income", "in", ["income_tax"], "Less"]];
const CASH: Row[] = [["operating_cash_flow", "in"], ["capex", "out"], ["dividends_paid", "out"], ["buybacks", "out"]];
const SHEET: Row[] = [
  ["total_assets", "in", ["cash", "short_term_investments", "receivables", "inventory"], "Of which"],
  ["total_liabilities", "out", ["short_term_borrowings", "current_long_term_debt", "long_term_debt"], "Of which"],
  ["shareholders_equity", "in"],
];
type Row = [string, "in" | "out", string[]?, string?];
// A change is green or red only where its direction says something by itself (Tamer 2026-11-16: "debt can be a good thing or a bad
// thing ... have the color be gray/white ish, since we dont know"); every other figure's change is plain.
const JUDGED = new Set(["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted", "operating_cash_flow"]);
const judge = (k: string, c: number | null) => (c === null ? "dim" : JUDGED.has(k) ? (c > 0 ? "good" : c < 0 ? "bad" : "dim") : "plain");
const KEYS = {
  income: "A coloured bar is what is kept at each step; where it runs on in grey, that is what was taken away at that step, and the grey rows are those costs.",
  cash: "The coloured bar is the cash the business brought in; grey bars are cash spent or paid out.",
  sheet: "Coloured bars are what it holds and what is left for shareholders; grey bars are what it owes.",
};
const YEARS2 = "The brighter bar is the year in focus, the fainter one the year before.";

// ---- The card and the sentence ----

type CardProps = ComponentProps<typeof LibCard>;

/** The sentence's second part and the Revenue card, from the newest fiscal year held. */
export function accountsParts(h: FundamentalsHistory): { more: ReactNode; card: CardProps } {
  const m = buildModel(h);
  const i = m.np - 1;
  const rev = m.F("revenue");
  const c = yoy(rev, i);
  const top = Math.max(0, ...rev.filter((v): v is number => v !== null));
  const bn = (v: number | null) => usd(v);
  const cls = c === null ? "dim" : c > 0 ? "good" : c < 0 ? "bad" : "dim";
  const move = c === null ? null : c > 0 ? `up ${c.toFixed(1)}%` : c < 0 ? `down ${Math.abs(c).toFixed(1)}%` : "unchanged";
  const filed = m.filedLast ? <>, filed {dayWord(m.filedLast)},</> : null;
  const more = rev[i] === null
    ? <>Its last yearly report{filed} does not show revenue.</>
    : <>Its last yearly report{filed} shows revenue of {bn(rev[i])}{move && <>, <em className={cls}>{move}</em> on the year before</>}.</>;
  const chip = c === null
    ? { tone: "dim" as const, text: m.np === 1 ? "One year held" : rev[i] === null ? "No revenue filed" : "No change to show" }
    : { tone: cls as "good" | "bad" | "dim", text: `${c > 0 ? "Up" : c < 0 ? "Down" : "Unchanged"}${c ? ` ${Math.abs(c).toFixed(1)}%` : ""} on ${fy(m.per[i - 1])}` };
  const card: CardProps = {
    name: "Revenue",
    hue: "data",
    chip,
    fig: bn(rev[i]),
    unit: `fiscal year to ${dayWord(m.per[i])}`,
    pic: (
      <span className="lk-mini">
        {rev.map((x, j) => <i key={j} className={j === i ? "on" : ""} style={{ height: `${x === null || top === 0 ? 0 : Math.max(3, (x / top) * 100).toFixed(1)}%` }} />)}
      </span>
    ),
    facts: [["Gross profit", "gross_profit"], ["Operating income", "operating_income"], ["Net income", "net_income"]].map(([label, k]) => ({ label, value: bn(m.F(k)[i]) })),
    hover: `Revenue, fiscal ${fy(m.per[0])} to ${fy(m.per[i])}. One bar for each fiscal year, the newest lit. ${SRC(m)}`,
    testId: "lk-card-filings",
  };
  return { more, card };
}

// ---- The panels ----

const money = (v: number, tight: boolean) => {
  const bn = Math.abs(v) >= 1e9 || v === 0;
  return tight ? `${+(v / (bn ? 1e9 : 1e6)).toFixed(1)}${bn ? "" : "m"}` : bn ? `$${+(v / 1e9).toFixed(1)}bn` : `$${Math.round(v / 1e6)}m`;
};

/** A bar with its top corners rounded (up to 4) and its foot nearly square, as the mockup draws it. */
const barPath = (x: number, y: number, w: number, h: number): string => {
  if (h <= 0) return "";
  const a = Math.min(4, w / 2);
  return `M${x} ${y + a}Q${x} ${y} ${x + a} ${y}H${x + w - a}Q${x + w} ${y} ${x + w} ${y + a}V${y + h - 1}Q${x + w} ${y + h} ${x + w - 1} ${y + h}H${x + 1}Q${x} ${y + h} ${x} ${y + h - 1}Z`;
};

/** Revenue (bright) and net income (faint) for each fiscal year, one band behind the year in focus. SVG; a press on a year puts it in focus. */
function YearsPlot({ m, yi, onYear, phone }: { m: Model; yi: number; onYear: (i: number) => void; phone: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const { w } = useBoxSize(host);
  const [hot, setHot] = useState<number | null>(null);
  const W = w > 0 ? w : 640;
  const tight = W < 520;
  const H = phone ? 220 : 300;
  const L = tight ? 40 : 52;
  const R = 6;
  const T = 10;
  const B = 28;
  const ih = H - T - B;
  const rev = m.F("revenue");
  const net = m.F("net_income");
  const hi = Math.max(0, ...rev.filter((v): v is number => v !== null)) * 1.08 || 1;
  const ticks = lineTicks(0, hi, 4);
  const Y = (v: number) => T + ih * (1 - v / hi);
  const slot = (W - L - R) / m.np;
  const X = (i: number) => L + slot * (i + 0.5);
  const bw = Math.min(34, slot * 0.34);
  const label = (i: number) => (phone ? `'${fy(m.per[i]).slice(2)}` : fy(m.per[i]));
  return (
    <>
      <div className={`lk-plot${tight ? " tight" : ""}`} ref={host}>
        <svg className="lk-yrs" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Revenue and net income by fiscal year">
          <defs>
            <linearGradient id="lk-yfade" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" className="lk-fa" />
              <stop offset="1" className="lk-fb" />
            </linearGradient>
          </defs>
          <g className="lk-gl">
            {ticks.map((t) => <line key={t} x1={L} x2={W - R} y1={Y(t)} y2={Y(t)} className={t === 0 ? "base" : undefined} />)}
          </g>
          <g className="lk-ys">{ticks.map((t) => <text key={t} x={L - 8} y={Y(t) + 4} textAnchor="end">{money(t, tight)}</text>)}</g>
          <rect className="lk-band" x={L + slot * yi + 3} y={T - 4} width={slot - 6} height={ih + 4} rx="8" />
          {m.per.map((p, i) => {
            const on = i === yi || i === hot;
            const r = rev[i];
            const n = net[i];
            return (
              <g key={p} className="lk-slot" onClick={() => onYear(i)} onMouseEnter={() => setHot(i)} onMouseLeave={() => setHot(null)}>
                <title>{`Fiscal year to ${dayWord(p)}. Revenue ${usd(r)}, net income ${usd(n)}, filed ${m.filedOn("revenue", i) ? dayWord(m.filedOn("revenue", i) as string) : DASH}`}</title>
                <rect x={L + slot * i} y={0} width={slot} height={H} fill="transparent" />
                {r !== null && r > 0 && <path className="lk-yb rev" d={barPath(X(i) - bw - 1, Y(r), bw, Y(0) - Y(r))} opacity={on ? 1 : 0.8} />}
                {n !== null && n > 0 && <path className="lk-yb net" d={barPath(X(i) + 1, Y(n), bw, Y(0) - Y(n))} opacity={on ? 0.5 : 0.38} />}
              </g>
            );
          })}
          <g className="lk-xs">{m.per.map((p, i) => <text key={p} x={X(i)} y={H - 6} textAnchor="middle">{label(i)}</text>)}</g>
        </svg>
      </div>
      <p className="lk-unit">In billions of US dollars</p>
    </>
  );
}

/** The four ratios as the server worked them out, each with its line over the years and the year in focus marked. */
function Ratios({ m, yi }: { m: Model; yi: number }) {
  return (
    <div className="lk-rat">
      {m.h.ratios.map((r) => {
        const cell = (p: string) => r.values.find((x) => x.period_end === p);
        const v = m.per.map((p) => cell(p)?.value ?? null);
        const now = v[yi];
        const was = yi ? v[yi - 1] : null;
        const kind = r.name === "debt_to_equity" ? "x" : "pct";
        const show = (x: number) => (kind === "pct" ? `${(x * 100).toFixed(1)}%` : `${x.toFixed(2)}×`);
        const have = v.filter((x): x is number => x !== null);
        const lo = Math.min(...have);
        const hi = Math.max(...have);
        const Yp = (x: number) => 4 + (1 - (x - lo) / (hi - lo || 1)) * 28;
        const Xp = (i: number) => (m.np > 1 ? (i / (m.np - 1)) * 100 : 50);
        let pen = false;
        const line = v.map((x, i) => {
          if (x === null) { pen = false; return ""; }
          const c = `${pen ? "L" : "M"}${Xp(i).toFixed(2)} ${Yp(x).toFixed(1)}`;
          pen = true;
          return c;
        }).join("");
        const said = now === null ? (cell(m.per[yi])?.reason ?? "Not filed for this year")
          : was === null ? `Fiscal ${fy(m.per[yi])}`
            : show(now) === show(was) ? "Same as the year before"
              : `${now > was ? "Up" : "Down"} from ${show(was)} the year before`;
        const debt = cell(m.per[yi])?.debt;
        const hover = [r.label, debt == null ? null : `Borrowed money, ${dayWord(m.per[yi])}: ${usd(debt)}`, `${r.formula}. ${r.definition} ${r.source}`].filter(Boolean).join("\n");
        return (
          <div className="lk-r" key={r.name} title={hover}>
            <span className="lk-rn">{r.label}</span>
            <b className="num">{now === null ? DASH : show(now)}</b>
            <span className="lk-spark">
              <svg viewBox="0 0 100 36" preserveAspectRatio="none" aria-hidden="true"><path d={line} /></svg>
              {now !== null && <i style={{ left: `${Xp(yi).toFixed(2)}%`, top: `${((Yp(now) / 36) * 100).toFixed(1)}%` }} />}
            </span>
            <span className="lk-said">{said}</span>
          </div>
        );
      })}
    </div>
  );
}

/** One statement as rows on one scale: the year before and the year in focus as two bars, two amounts and the change. */
function Statement({ m, yi, list, top, mix, why, last }: { m: Model; yi: number; list: Row[]; top: (j: number) => number; mix: boolean; why: string; last?: ReactNode }) {
  const i = yi;
  const scale = Math.max(top(i), i ? top(i - 1) : 0) || 1;
  const share = (v: number) => Math.max(0, Math.min(100, (v / scale) * 100));
  const yearOf = (j: number) => fy(m.per[j]);
  const bigger = (now: number | null, was: number | null, text: (x: number) => string) => (now === null || was === null || text(now) === text(was) ? 0 : now > was ? 1 : -1);
  const bar = (j: number, k: string, cls: string, cut: ((j: number) => number) | null) => {
    const v = m.F(k)[j];
    if (v === null) return null;
    const kept = share(v);
    const c = cut ? share(cut(j)) : 0;
    const prev = j === i ? "" : " prev";
    if (c > 0) return <u key={j} className={`mix${prev}`} style={{ "--w": `${Math.min(100, kept + c).toFixed(2)}%`, "--k": `${((kept / (kept + c || 1)) * 100).toFixed(2)}%` } as CSSProperties} />;
    return <u key={j} className={`${cls}${prev}`} style={{ "--w": `${kept.toFixed(2)}%` } as CSSProperties} />;
  };
  const one = (k: string, sub: boolean, cls: string, cut: ((j: number) => number) | null, extra: string[] = []) => {
    const f = m.F(k);
    const c = yoy(f, i);
    const hi = bigger(f[i], i ? f[i - 1] : null, usd);
    const tip = [
      m.nameOf(k), `Fiscal year to ${dayWord(m.per[i])}: ${usd(f[i])}`,
      m.filedOn(k, i) ? `Filed: ${dayWord(m.filedOn(k, i) as string)}` : null,
      i ? `Fiscal year to ${dayWord(m.per[i - 1])}: ${usd(f[i - 1])}` : null,
      ...extra, "", `${why}${i ? ` ${YEARS2}` : ""}`,
    ].filter((x) => x !== null).join("\n");
    return (
      <div className={`lk-lv${sub ? " sub" : ""}`} key={k} title={tip}>
        <span className="lk-lv-n">{m.nameOf(k)}</span>
        <span className="lk-lv-t">{bar(i, k, cls, cut)}{i > 0 && bar(i - 1, k, cls, cut)}</span>
        <span className={`num was${i ? "" : " none"}${hi < 0 ? " top" : ""}`} data-y={i ? yearOf(i - 1) : ""}>{i ? usd(f[i - 1]) : DASH}</span>
        <b className={`num${hi > 0 ? " top" : ""}`} data-y={yearOf(i)}>{usd(f[i])}</b>
        <span className={`num ${judge(k, c)}`}>{c === null ? DASH : pct(c)}</span>
      </div>
    );
  };
  return (
    <div>
      <div className="lk-lv head"><span /><span /><span>{i ? yearOf(i - 1) : ""}</span><b>{yearOf(i)}</b><span>{i ? "Change" : ""}</span></div>
      {list.flatMap(([k, tone, under = [], word]) => {
        const parts = under.filter((q) => m.F(q)[i] !== null);
        const cut = mix && parts.length ? (j: number) => parts.reduce((t, q) => t + Math.max(0, m.F(q)[j] ?? 0), 0) : null;
        const pre = m.F("pretax_income")[i];
        const main = one(k, false, tone, cut, k === "net_income" && pre !== null ? [`${m.nameOf("pretax_income")}: ${usd(pre)}`] : []);
        const subs = parts.map((q) => one(q, true, word === "Less" ? "out" : `${tone} part`, null));
        return word === "Less" ? [...subs, main] : [main, ...subs];
      })}
      {last}
    </div>
  );
}

/** The panels: revenue and net income with the year picker and the four ratios, then the three statements. */
export function AccountsPanels({ h }: { h: FundamentalsHistory }) {
  const m = buildModel(h);
  const phone = useLayout() === "phone";
  const [yi, setYi] = useState(m.np - 1);
  const year = Math.min(yi, m.np - 1);
  const label = (i: number) => (phone ? `'${fy(m.per[i]).slice(2)}` : fy(m.per[i]));
  const src = SRC(m);
  const title = (name: string, key: string) => <span title={`${key} ${YEARS2} ${src}`}>{name}</span>;
  const eps = m.F("eps_diluted");
  const epsTop = Math.max(0, ...eps.filter((x): x is number => x !== null));
  const epsC = yoy(eps, year);
  const epsHi = (year ? (eps[year] !== null && eps[year - 1] !== null && usd(eps[year], "USD/shares") !== usd(eps[year - 1], "USD/shares") ? (eps[year] as number) > (eps[year - 1] as number) ? 1 : -1 : 0) : 0);
  const cur = m.valueAt("eps_diluted", year);
  const later = cur?.as_filed != null ? (h.splits ?? []).filter((s) => s.ex_date > cur.filing_date).map((s) => `the ${s.ratio} for 1 split of ${dayWord(s.ex_date)}`) : [];
  const epsTip = [
    `${m.nameOf("eps_diluted")}, fiscal year to ${dayWord(m.per[year])}`,
    "Net income for each share, counting the shares that options and convertible notes would add (diluted). One bar for each fiscal year, the year in focus lit.",
    cur?.as_filed != null ? `filed as $${cur.as_filed.toFixed(2)}${later.length ? `, before ${later.join(" and ")}` : ""}` : null,
    cur?.note ?? null,
    h.split_error,
  ].filter((x) => x !== null && x !== undefined).join("\n");
  const epsRow = (
    <div className="lk-lv eps" title={epsTip}>
      <span className="lk-lv-n">{m.nameOf("eps_diluted")}</span>
      <span className="lk-mini">{eps.map((x, j) => <i key={j} className={j === year ? "on" : ""} style={{ height: `${x === null || epsTop === 0 ? 0 : Math.max(3, (x / epsTop) * 100).toFixed(1)}%` }} />)}</span>
      <span className={`num was${year ? "" : " none"}${epsHi < 0 ? " top" : ""}`} data-y={year ? fy(m.per[year - 1]) : ""}>{year ? usd(eps[year - 1], "USD/shares") : DASH}</span>
      <b className={`num${epsHi > 0 ? " top" : ""}`} data-y={fy(m.per[year])}>{usd(eps[year], "USD/shares")}</b>
      <span className={`num ${judge("eps_diluted", epsC)}`}>{epsC === null ? DASH : pct(epsC)}</span>
    </div>
  );
  const cashTop = (j: number) => Math.max(...CASH.map(([k]) => m.F(k)[j] ?? 0));
  const to = `Fiscal year to ${dayWord(m.per[year])}`;
  return (
    <>
      <Block
        id="lk-filings"
        title={<span title={`For each fiscal year the brighter bar is revenue and the fainter one beside it net income. Press a year to put it in focus. ${src}`}>Revenue and net income</span>}
        state={<State tone="run">{plural(m.np, "fiscal year")}</State>}
        right={
          <span role="group" aria-label="Fiscal year">
            <Segmented variant="seg" options={m.per.map((_p, i) => ({ label: label(i), value: String(i) }))} value={String(year)} onChange={(v) => setYi(Number(v))} />
          </span>
        }
      >
        <div className="lk-two">
          <div><YearsPlot m={m} yi={year} onYear={setYi} phone={phone} /></div>
          <Ratios m={m} yi={year} />
        </div>
      </Block>
      <Block title={title("Income statement", KEYS.income)} state={<State tone="dim">{to}</State>}>
        <Statement m={m} yi={year} list={INCOME} top={(j) => m.F("revenue")[j] ?? 0} mix why={KEYS.income} last={epsRow} />
      </Block>
      <Block title={title("Balance sheet", KEYS.sheet)} state={<State tone="dim">{`At ${dayWord(m.per[year])}`}</State>}>
        <Statement m={m} yi={year} list={SHEET} top={(j) => m.F("total_assets")[j] ?? 0} mix={false} why={KEYS.sheet} />
      </Block>
      <Block title={title("Cash flow", KEYS.cash)} state={<State tone="dim">{to}</State>}>
        <Statement m={m} yi={year} list={CASH} top={cashTop} mix={false} why={KEYS.cash} />
      </Block>
    </>
  );
}
