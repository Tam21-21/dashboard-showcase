import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode, RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PhoneOff } from "../../kit/phone";
import { Block, PageHeader, Say, Segmented, State, usePageMemory } from "../../kit";
import type { NlTone } from "../../kit";
import type { PageProps } from "../../shell/sections";
import { approveVerdictForPaper, fetchJudgeThresholds, fetchJudgeVerdicts } from "../../data/judgeApi";
import type { CheckOutcomeEntry, JudgeMeasure, JudgeThresholdsResponse, JudgeVerdictRow, JudgeVerdictsResponse } from "../../data/judgeApi";
import { fetchStrategiesOverview, strategiesKeys } from "../../data/strategiesApi";
import type { StrategyOverviewRow } from "../../data/strategiesApi";
import { sendToJudge } from "../../data/runsApi";
import { serverDetail } from "../../../lib/errorText";
import { strategyLabel, useStrategyNames } from "../../data/strategyNames";
import "./judge.css";

// Round six (Tamer 2026-11-14), from the look trial's Judge page (Desktop overview_trial, lab-p-judge.js): the finding said
// once; the nine checks as a dial (every check for every graded run) or as their pass bars with the book each comes from;
// the graded runs with the picked one's sheet; the strategies waiting for the judge, with the button that sends them; and
// what keeps the checks from running, in the judge's own plain words.

// One page of verdicts: the route's own largest page (routes/judge.py, verdicts, limit le=500).
const LIMIT = 500;

// The names and questions are the mockup's CHECKS and JQ, in check order.
export const CHECKS: ReadonlyArray<{ name: string; question: string }> = [
  { name: "Evidence floor", question: "Enough trades to trust the result?" },
  { name: "Walk forward", question: "Does it still work on data it was not tuned on?" },
  { name: "Trial count", question: "Is it more than the luck of many tries?" },
  { name: "Plateau", question: "Do nearby settings work too?" },
  { name: "Overfitting test", question: "How likely is the chosen setting a fluke?" },
  { name: "Monte Carlo bounds", question: "Does it survive the trades in a shuffled order?" },
  { name: "Permutation test", question: "Does it beat the same rules on shuffled prices?" },
  { name: "Windfall", question: "Is it carried by one lucky trade?" },
  { name: "Beats buy and hold", question: "Does it beat simply holding?" },
];

const SENTENCE: Record<JudgeVerdictRow["verdict"], string> = {
  pass: "Passed all nine checks. It waits for your approval before it goes on paper.",
  fail: "Failed at least one check, so it stops here. It can come back only as a new idea, never with tuned numbers.",
  unjudgeable: "Some checks could not be computed from what this run stored, so the judge cannot decide.",
};
const VERDICT: Record<JudgeVerdictRow["verdict"], [NlTone, string]> = {
  pass: ["good", "Passed"], fail: ["bad", "Failed"], unjudgeable: ["bad", "No verdict"],
};
type Comparison = NonNullable<JudgeMeasure["comparison"]>;
// The target's hover says what the comparison means.
const PASSES: Record<Comparison, (bar: string) => string> = {
  at_or_above: (b) => `passes at ${b} or more`,
  strictly_above: (b) => `passes above ${b}`,
  at_or_below: (b) => `passes at ${b} or less`,
  strictly_below: (b) => `passes below ${b}`,
};

export const judgeKeys = { verdicts: ["judge", "verdicts"] as const, thresholds: ["judge", "thresholds"] as const };
/**
 * Every verdict row the store holds, read a page at a time until the store's own row count is covered, so a current
 * verdict older than one page is never missed. Forward testing reads the same under judgeKeys.verdicts.
 */
export async function fetchPageVerdicts(): Promise<JudgeVerdictsResponse> {
  const first = await fetchJudgeVerdicts(LIMIT, 0);
  const rows = [...first.rows];
  while (rows.length < first.store.row_count) {
    const next = await fetchJudgeVerdicts(LIMIT, rows.length);
    if (next.rows.length === 0) break; // rows were removed between the reads: every row there is has been read
    rows.push(...next.rows);
  }
  return { store: first.store, rows };
}

const NO_BAR = "No target is stored for this run";
// Older verdicts stored no figures at all; their checks are left out of "what keeps the checks from running".
const OLD = "This verdict was stored without this check's figures.";
// The judge's own words "not set yet" or "not decided yet" mean a setting nobody has chosen; anything else is an input.
const isSetting = (why: string) => /not (set|decided) yet/i.test(why);

export type Outcome = "cleared" | "failed" | "other";
const outcomeOf = (raw: string | null | undefined): Outcome => (raw === "cleared" ? "cleared" : raw === "failed" ? "failed" : "other");
const WORD: Record<Outcome, [NlTone, string]> = { cleared: ["good", "Cleared"], failed: ["bad", "Failed"], other: ["dim", "Could not run"] };

export function outcomes(row: JudgeVerdictRow): Outcome[] {
  const byCheck = new Map(row.check_outcomes.map((c: CheckOutcomeEntry) => [c.check, c.outcome]));
  return CHECKS.map((_, i) => outcomeOf(byCheck.get(i + 1)));
}
const clearedCount = (o: Outcome[]) => o.filter((x) => x === "cleared").length;
// The browser's own short month name varies ("Sept" in some), so the page spells the months itself, as the mockup does.
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const parisDate = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "numeric", year: "numeric", timeZone: "Europe/Paris" });
const dayWords = (ms: number, withYear = false): string => {
  const part = (t: string) => Number(parisDate.formatToParts(ms).find((x) => x.type === t)?.value);
  return `${part("day")} ${MONTHS[part("month") - 1]}${withYear ? ` ${part("year")}` : ""}`;
};
const gradedDay = (t: number) => dayWords(t * 1000);
const vars = (v: Record<string, number>) => v as CSSProperties;

const WINDFALL = 8;

// One figure as the page prints it. Check 8 is dollars, whole; a percent has one decimal; a small
// number is a plain decimal with two significant figures, never an exponent. A zero goes through
// the same rules as any other value of its unit.
/**
 * A figure in its check's own form. Given the bar it is read against, it takes as many more decimals as it needs (up to
 * six) for a figure that differs from its bar never to print the same as the bar.
 */
function figure(v: number, units: string | null, check: number, against?: number | null): string {
  const text = (x: number, more: number): string => {
    if (check === WINDFALL) {
      const t = Math.abs(x).toFixed(more);
      return `${x < 0 && Number(t) !== 0 ? "-" : ""}$${t}`;
    }
    if (units === "percent") return `${x.toFixed(1 + more)}%`;
    const a = Math.abs(x);
    if (a > 0 && a < 0.01) return x.toFixed(Math.min(100, 1 - Math.floor(Math.log10(a)) + more));
    return x.toFixed(2 + more);
  };
  if (against == null || v === against) return text(v, 0);
  for (let more = 0; more < 6; more++) if (text(v, more) !== text(against, more)) return text(v, more);
  return text(v, 6);
}

// A measure fails only when the value and the bar are both stored and the value misses the bar.
function misses(m: JudgeMeasure): boolean {
  if (m.value == null || m.bar == null || m.comparison == null) return false;
  switch (m.comparison) {
    case "at_or_above": return m.value < m.bar;
    case "strictly_above": return m.value <= m.bar;
    case "at_or_below": return m.value > m.bar;
    case "strictly_below": return m.value >= m.bar;
  }
}

// The text after the server's "code: ", first letter capitalised. The code itself is never shown.
function refusalSentence(e: unknown): string {
  const detail = serverDetail(e);
  const at = detail.indexOf(": ");
  const text = at >= 0 ? detail.slice(at + 2) : detail;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const pt = (c: number, r: number, a: number) => `${(c + r * Math.sin(a)).toFixed(2)},${(c - r * Math.cos(a)).toFixed(2)}`;
function arc(c: number, r0: number, r1: number, a0: number, a1: number): string {
  const big = a1 - a0 > Math.PI ? 1 : 0;
  return `M${pt(c, r1, a0)}A${r1},${r1} 0 ${big} 1 ${pt(c, r1, a1)}L${pt(c, r0, a1)}A${r0},${r0} 0 ${big} 0 ${pt(c, r0, a0)}Z`;
}

/** Nine arcs, one per check: green cleared, red failed, grey could not run; outlines only while a run waits for its verdict. */
export function Ring({ cells, size, empty = false }: { cells: Outcome[]; size: number; empty?: boolean }) {
  const c = size / 2;
  const gap = size > 80 ? 0.035 : 0.06;
  return (
    <svg className={size > 80 ? "jd-ring big" : "jd-ring"} width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      {CHECKS.map((_, i) => (
        <path key={i} className={empty ? "o-empty" : `o-${cells[i]}`} style={vars({ "--k": i })}
          d={arc(c, c - Math.max(5, size * 0.13), c - 2, (i / 9) * 2 * Math.PI + gap, ((i + 1) / 9) * 2 * Math.PI - gap)} />
      ))}
    </svg>
  );
}

/** True once the element has first shown, so its parts come in (transform and opacity only, see judge.css). */
export function useSeen<T extends HTMLElement>(): [RefObject<T>, boolean] {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    if (typeof IntersectionObserver === "undefined") { setSeen(true); return; }
    const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { io.disconnect(); setSeen(true); } }, { rootMargin: "0px 0px -8% 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);
  return [ref, seen];
}

export type CheckSum = { cleared: number; failed: number; other: number; whys: Map<string, number>; top: string | null; kind: "runs" | "setting" | "input" };
const KIND: Record<CheckSum["kind"], [NlTone, string]> = { runs: ["good", "Gives a figure"], setting: ["warn", "Setting not chosen"], input: ["dim", "Input missing"] };

/** How the graded runs did on each check, and whether the check can give a figure today. */
export function sumChecks(rows: JudgeVerdictRow[], outs: Outcome[][]): CheckSum[] {
  return CHECKS.map((_, i) => {
    const n = { cleared: 0, failed: 0, other: 0 };
    outs.forEach((o) => { n[o[i]] += 1; });
    const whys = new Map<string, number>();
    rows.forEach((r, k) => {
      const why = r.measures.find((m) => m.check === i + 1)?.why;
      if (outs[k][i] === "other" && why && why !== OLD) whys.set(why, (whys.get(why) ?? 0) + 1);
    });
    const top = [...whys.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
    // A check gives a figure when a graded run stored a value for it; a fail with no value (a run with no trades) does not count.
    const figured = rows.some((r) => (r.measures.find((m) => m.check === i + 1)?.measures ?? []).some((m) => m.value != null));
    return { ...n, whys, top, kind: figured ? "runs" : top !== null && isSetting(top) ? "setting" : "input" };
  });
}

function Dial({ rows, outs, names, sums, onPick }: {
  rows: JudgeVerdictRow[]; outs: Outcome[][]; names: (id: number) => string; sums: CheckSum[]; onPick: () => void;
}) {
  const [hot, setHot] = useState<[number, number | null] | null>(null);
  const [ref, seen] = useSeen<HTMLDivElement>();
  const S = 440;
  const R = S / 2 - 24;
  const r0 = R * 0.44;
  const band = (R - r0) / Math.max(1, rows.length);
  const passed = rows.filter((r) => r.verdict === "pass").length;
  const on = (i: number, k: number) => !hot || (hot[0] === i && (hot[1] == null || hot[1] === k));
  const tip = (i: number, k: number) => {
    const r = rows[k];
    const m = r.measures.find((x) => x.check === i + 1);
    const v = m?.measures.find((x) => x.value != null);
    const why = outs[k][i] === "other" && m?.why ? `\n${m.why}` : "";
    return `${CHECKS[i].name}, ${names(r.strategy_id)}\n${WORD[outs[k][i]][1]}${v && v.value != null ? `: ${v.label} ${figure(v.value, v.units, i + 1)}` : ""}${why}`;
  };
  return (
    <div className={seen ? "jd-dial in" : "jd-dial"} ref={ref} data-testid="dial">
      <div className="jd-dial-cv">
        <svg viewBox={`0 0 ${S} ${S}`} className="jd-dial-svg" role="img" aria-label="Every check for every graded run">
          {CHECKS.map((_, i) => rows.map((r, k) => {
            const a0 = (i / 9) * 2 * Math.PI + 0.03;
            const a1 = ((i + 1) / 9) * 2 * Math.PI - 0.03;
            return (
              <path key={`${i}-${r.id}`} className={`o-${outs[k][i]}${on(i, k) ? "" : " off"}`} style={vars({ "--k": i })}
                d={arc(S / 2, R - (k + 1) * band + 1.5, R - k * band - 1.5, a0, a1)}
                onMouseEnter={() => setHot([i, k])} onMouseLeave={() => setHot(null)}>
                <title>{tip(i, k)}</title>
              </path>
            );
          }))}
          {CHECKS.map((_, i) => {
            const a = ((i + 0.5) / 9) * 2 * Math.PI;
            const [x, y] = pt(S / 2, R + 14, a).split(",");
            return <text key={i} x={x} y={y} className={hot && hot[0] === i ? "jd-n hot" : "jd-n"} textAnchor="middle" dominantBaseline="middle">{i + 1}</text>;
          })}
        </svg>
        <div className="jd-mid"><span className="num">{passed}</span><span>of {rows.length} passed</span></div>
      </div>
      <div className={hot ? "jd-checks hov" : "jd-checks"}>
        {CHECKS.map((c, i) => {
          const s = sums[i];
          return (
            <button key={c.name} type="button" className={hot && hot[0] === i ? "jd-ck hot" : "jd-ck"} style={vars({ "--k": i })}
              onMouseEnter={() => setHot([i, null])} onMouseLeave={() => setHot(null)} onClick={onPick}
              title={`${c.question}\nCleared ${s.cleared}, failed ${s.failed}, could not run ${s.other}${s.top ? `\n${s.top}` : ""}`}>
              <span className="num jd-n">{String(i + 1).padStart(2, "0")}</span>
              <span className="jd-nm">{c.name}</span>
              <span className="jd-stack">
                {([["good", s.cleared], ["bad", s.failed], ["soft", s.other]] as const).filter(([, n]) => n > 0).map(([t, n]) => <i key={t} className={t} style={{ flex: n }} />)}
              </span>
              <State tone={KIND[s.kind][0]}>{KIND[s.kind][1]}</State>
            </button>
          );
        })}
      </div>
    </div>
  );
}

const CMP: Record<Comparison, string> = { at_or_above: "at least", strictly_above: "above", at_or_below: "at most", strictly_below: "below" };

/** Each check's pass bar in words, with the book and page it comes from; the book's own sentence on hover. */
function PassBars({ thresholds, rows, sums }: { thresholds: JudgeThresholdsResponse | undefined; rows: JudgeVerdictRow[]; sums: CheckSum[] }) {
  const [ref, seen] = useSeen<HTMLDivElement>();
  return (
    <div className={seen ? "jd-bars in" : "jd-bars"} ref={ref} data-testid="bars">
      {CHECKS.map((c, i) => {
        const bars = thresholds?.checks[String(i + 1)]?.bars ?? [];
        // Check 1 keeps its bars in the judge's code, not in the bar table; a stored verdict's figures carry them.
        const own = rows[0]?.measures.find((m) => m.check === i + 1)?.measures.filter((m) => m.bar != null && m.comparison != null) ?? [];
        const lines: Array<[string, string, string | undefined]> = bars.length > 0
          ? bars.map((b) => [b.value_text, /^\d/.test(b.source_folio) ? `${b.source_book}, page ${b.source_folio}` : b.source_book, b.source_quote])
          : own.map((m) => [`${m.label} ${CMP[m.comparison as Comparison]} ${figure(m.bar as number, m.units, i + 1)}`, "No book is stored with this target", undefined]);
        const s = sums[i];
        return (
          <div key={c.name} className={`jd-bar k-${s.kind}`} style={vars({ "--k": i })}>
            <div className="jd-bar-top"><span className="num jd-bar-i">{String(i + 1).padStart(2, "0")}</span><State tone={KIND[s.kind][0]}>{KIND[s.kind][1]}</State></div>
            <p className="jd-bar-n">{c.name}</p>
            <p className="jd-bar-q">{c.question}</p>
            {lines.map(([t, src, q]) => (
              <div key={t}>
                <p className="jd-bar-t" title={q ? `"${q}"` : undefined}>{t.charAt(0).toUpperCase() + t.slice(1)}</p>
                <p className="jd-bar-s">{src}</p>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

function barHover(m: JudgeMeasure, check: number): string | undefined {
  if (m.bar == null) return NO_BAR;
  return m.comparison == null ? undefined : PASSES[m.comparison](figure(m.bar, m.units, check));
}

/** One check of the picked run: its figure on a track against the bar (the white tick), or the plain reason it could not run. */
function CheckRow({ index, row, outcome }: { index: number; row: JudgeVerdictRow; outcome: Outcome }) {
  const info = CHECKS[index];
  const check = index + 1;
  const entry = row.measures.find((x) => x.check === check);
  const measures = entry?.measures ?? [];
  const shown = measures.find(misses) ?? measures.find((m) => m.value != null) ?? measures[0] ?? null;
  const absent = entry?.outcome == null;
  const [tone, word] = absent ? (["dim", "Not run yet"] as [NlTone, string]) : WORD[outcome];
  const fmt = (v: number | null, m: JudgeMeasure, other: number | null) => (v == null ? "-" : figure(v, m.units, check, other));
  const hover = measures.map((x) => `${x.label}: ${fmt(x.value, x, x.bar)}, target ${fmt(x.bar, x, x.value)}`).join("\n") || undefined;

  // Words only: what the check measured and what it needs, or the plain reason it could not run.
  let middle: ReactNode;
  if (shown && shown.value != null && shown.bar != null) {
    middle = (
      <span className="jd-lo jd-fig">
        <b className={`num ${tone}`}>{figure(shown.value, shown.units, check, shown.bar)}</b>
        <span title={barHover(shown, check)}>{`${shown.label}. It needs ${shown.comparison ? CMP[shown.comparison] : "to reach"} ${figure(shown.bar, shown.units, check, shown.value)}.`}</span>
      </span>
    );
  } else if (shown && shown.value != null) {
    middle = <span className="jd-lo jd-fig"><b className="num">{figure(shown.value, shown.units, check)}</b><span title={NO_BAR}>{shown.label}</span></span>;
  } else if (absent) {
    middle = <span className="jd-lo">Not run yet</span>;
  } else {
    middle = <span className="jd-lo">{entry?.why ?? (outcome === "other" ? "Could not run" : "No figure stored")}</span>;
  }
  const reason = row.check_outcomes.find((c) => c.check === check)?.reason;
  return (
    <div className="jd-row vrow" data-check={check} title={[hover, outcome !== "other" ? reason : undefined].filter(Boolean).join("\n") || undefined} style={vars({ "--k": index })}>
      <span className="num jd-n">{String(check).padStart(2, "0")}</span>
      <span className="jd-rn"><b className="vk">{info.name}</b><small className="vq">{info.question}</small></span>
      <div>{middle}</div>
      <span className="jd-w8">{absent ? <span title="Not run yet"><State tone={tone}>{word}</State></span> : <State tone={tone}>{word}</State>}</span>
    </div>
  );
}

function Sheet({ row }: { row: JudgeVerdictRow }) {
  const qc = useQueryClient();
  const names = strategyLabel(useStrategyNames(), row.strategy_id);
  const [sent, setSent] = useState<{ id: number; ok: boolean; text: string } | null>(null);
  const approve = useMutation({
    mutationFn: (r: JudgeVerdictRow) => approveVerdictForPaper(r.id, r.judged_capital),
    onSuccess: (_d, r) => {
      setSent({ id: r.id, ok: true, text: "Sent to paper" });
      void qc.invalidateQueries({ queryKey: ["judge"] });
    },
    onError: (e, r) => setSent({ id: r.id, ok: false, text: refusalSentence(e) }),
  });
  const cells = outcomes(row);
  const [tone, word] = VERDICT[row.verdict];
  const msg = sent && sent.id === row.id ? sent : null;
  const canApprove = row.verdict === "pass" && row.current && !row.rejected;
  const graded = dayWords(row.graded_at * 1000, true);
  return (
    <Block className="jd-sheet">
      <div data-testid="sheet" key={row.id} className="jd-sheet-in">
        <div className="jd-sh-top">
          <div className="jd-sh-ring"><Ring cells={cells} size={116} /><span className="num vb">{clearedCount(cells)}<small>of 9</small></span></div>
          <div className="jd-sh-id">
            <p className="jd-sh-n vn" title={names.hover}>{names.text}</p>
            <p className="jd-sh-s"><State tone={tone}>{word}</State>{names.hover.endsWith(", deleted") && <State tone="dim">Strategy deleted</State>}<span>{row.ticker}, run {row.run_id}, graded {graded}</span></p>
            <p className="jd-sh-say vt">{SENTENCE[row.verdict]}</p>
          </div>
          <div className="jd-ap">
            <span title={canApprove ? undefined : "Only a passed, current verdict can go to paper"}>
              <button type="button" className="btn" disabled={!canApprove || approve.isPending} onClick={() => approve.mutate(row)}>Approve for paper</button>
            </span>
            {msg && <span className={msg.ok ? "g" : "rd"} role="status">{msg.text}</span>}
          </div>
        </div>
        <div className="jd-rows">{cells.map((o, i) => <CheckRow key={i} index={i} row={row} outcome={o} />)}</div>
      </div>
    </Block>
  );
}

/** Each plain reason a check could not run, once, with how many graded runs it held back; amber when a setting is not chosen. */
function WhyList({ rows, sums }: { rows: JudgeVerdictRow[]; sums: CheckSum[] }) {
  const [ref, seen] = useSeen<HTMLDivElement>();
  const items = sums.flatMap((s, i) => [...s.whys.entries()].map(([why, n]) => ({ i, why, n, set: isSetting(why) })))
    .sort((a, b) => Number(b.set) - Number(a.set) || b.n - a.n || a.i - b.i);
  if (items.length === 0) return <p className="empty">Every check ran on every graded run.</p>;
  return (
    <div className={seen ? "jd-why in" : "jd-why"} ref={ref} data-testid="why">
      {items.map((x, k) => (
        <div key={`${x.i}-${x.why}`} className="jd-w" style={vars({ "--k": k })}
          title={x.set ? "A setting nobody has chosen: with a value from a book, the check can run" : "An input the lab does not make yet: the run did not store what this check reads"}>
          <span className="jd-w-c"><span className="num">{String(x.i + 1).padStart(2, "0")}</span>{CHECKS[x.i].name}</span>
          <span className="jd-w-t">{x.why}</span>
          <span className="jd-w-bar"><i className={x.set ? "warn" : "soft"} style={{ width: `${(x.n / rows.length) * 100}%` }} /></span>
          <span className="num jd-w-n">{x.n} of {rows.length}</span>
        </div>
      ))}
    </div>
  );
}

type SendResult = { name: string; ok: boolean; text: string };

const runSecs = (v: unknown): number => (typeof v === "number" ? v : Date.parse(String(v)) / 1000) || 0;
// created_at is served as Unix seconds although its type says text; both forms are read.
const runDay = (v: unknown): string | null => {
  const ms = typeof v === "number" ? v * 1000 : typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isFinite(ms) ? dayWords(ms) : null;
};

type Ran = StrategyOverviewRow & { run: NonNullable<StrategyOverviewRow["run"]> };

/** The one send: every waiting run goes to the judge in turn, and each answer is kept. The header and the list share it. */
function useSend(names: (id: number) => string) {
  const qc = useQueryClient();
  const [results, setResults] = useState<SendResult[] | null>(null);
  const send = useMutation({
    mutationFn: async (list: Ran[]) => {
      const out: SendResult[] = [];
      for (const s of list) {
        try { await sendToJudge(s.run.id); out.push({ name: names(s.id), ok: true, text: "Judged" }); }
        catch (e) { out.push({ name: names(s.id), ok: false, text: refusalSentence(e) }); }
      }
      return out;
    },
    onSuccess: (out) => {
      setResults(out);
      void qc.invalidateQueries({ queryKey: ["judge"] });
    },
  });
  return { send, results };
}
type Sender = ReturnType<typeof useSend>;

const sendWords = (n: number) => `Send ${n} ${n === 1 ? "run" : "runs"} to the judge`;

/** The current strategies whose newest run has no verdict yet, and the button that sends those runs to the judge. */
function Waiting({ waiting, names, sender }: { waiting: Ran[]; names: (id: number) => string; sender: Sender }) {
  const [ref, seen] = useSeen<HTMLDivElement>();
  const { send, results } = sender;
  if (waiting.length === 0) return <p className="empty">Every current strategy's newest run has a verdict.</p>;
  const judged = results?.filter((r) => r.ok).length ?? 0;
  const refused = results?.filter((r) => !r.ok) ?? [];
  return (
    <div className={seen ? "jd-wait in" : "jd-wait"} ref={ref} data-testid="waiting">
      {waiting.map((s, k) => (
        <div key={s.id} className="jd-wt" style={vars({ "--k": k })} title="Its ring fills once the judge grades its run">
          <Ring cells={[]} size={26} empty />
          <span className="jd-wt-n">{names(s.id)}</span>
          <span className="jd-wt-d">{`ran ${runDay(s.run.created_at) ?? "-"}`}</span>
        </div>
      ))}
      <div className="jd-go">
        <button type="button" className="btn" disabled={send.isPending} onClick={() => send.mutate(waiting)}>
          {sendWords(waiting.length)}
        </button>
        {results && (
          <span className={refused.length ? "rd" : "g"} role="status" title={refused.map((r) => `${r.name}: ${r.text}`).join("\n") || undefined}>
            {`${judged} judged${refused.length ? `, ${refused.length} refused` : ""}`}
          </span>
        )}
      </div>
    </div>
  );
}

export default function Judge({ app, section }: PageProps) {
  const [view, setView] = usePageMemory<{ pickedId: number | null; checks: "dial" | "bars" }>("research/judge", { pickedId: null, checks: "dial" });
  const q = useQuery({ queryKey: judgeKeys.verdicts, queryFn: fetchPageVerdicts });
  const thQ = useQuery({ queryKey: judgeKeys.thresholds, queryFn: fetchJudgeThresholds });
  const ovQ = useQuery({ queryKey: strategiesKeys.overview, queryFn: () => fetchStrategiesOverview() });
  const namesQ = useStrategyNames();
  const nameOf = (id: number) => strategyLabel(namesQ, id).text;

  const all = q.data?.rows ?? [];
  const rows = all.filter((r) => r.current && !r.rejected).sort((a, b) => b.graded_at - a.graded_at || b.id - a.id);
  const picked = rows.find((r) => r.id === view.pickedId) ?? rows[0] ?? null;
  const outs = rows.map(outcomes);
  const sums = sumChecks(rows, outs);
  const graded = new Set(all.map((r) => r.run_id));
  const waiting = (ovQ.data ?? []).filter((s): s is Ran => s.run !== null && !graded.has(s.run.id))
    .sort((a, b) => runSecs(b.run.created_at) - runSecs(a.run.created_at));
  const passed = rows.filter((r) => r.verdict === "pass").length;
  const older = rows.filter((r) => r.measures.some((m) => m.why === OLD)).length;
  const count = (k: CheckSum["kind"]) => sums.filter((s) => s.kind === k).length;
  const sender = useSend(nameOf);
  const checksRef = useRef<HTMLDivElement>(null);
  const toTargets = () => {
    setView((p) => ({ ...p, checks: "bars" }));
    checksRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  };

  let body: ReactNode;
  if (q.isError) body = <Block><p className="empty">{serverDetail(q.error)}</p></Block>;
  else if (!q.data) body = <Block><p className="empty">Loading the verdicts.</p></Block>;
  else {
    const lead = rows.length === 0 ? "No run has been judged yet." : passed === 0 ? "No run can pass the judge yet." : `${passed} of ${rows.length} runs passed the judge.`;
    const more = rows.length === 0 ? undefined : passed === 0 ? (
      <>Only <em className="good">{count("runs")} of the 9 checks</em> give a figure today. <em className="warn">{count("setting")} wait for a setting nobody has chosen</em> and {count("input")} lack an input the lab does not make yet.{waiting.length > 0 && <> <em className="warn">{waiting.length} current strategies</em> wait for their first verdict.</>}</>
    ) : "They wait for your approval before they go on paper.";
    body = (
      <>
        <Say lead={lead} more={more} />
        <div className="jd-stack-page">
          {rows.length > 0 && (
            <div ref={checksRef}>
            <Block title="Judge checks"
              state={<><State tone="good">{`${count("runs")} give a figure`}</State><State tone="warn">{`${count("setting")} wait for a setting`}</State><State tone="dim">{`${count("input")} lack an input`}</State></>}
              right={<Segmented variant="seg" value={view.checks} onChange={(v) => setView((p) => ({ ...p, checks: v === "bars" ? "bars" : "dial" }))}
                options={[{ label: "Dial", value: "dial", hover: "Every check for every graded run" }, { label: "Targets", value: "bars", hover: "What each check asks, its target and the book it comes from" }]} />}>
              {view.checks === "bars"
                ? <PassBars thresholds={thQ.data} rows={rows} sums={sums} />
                : <Dial rows={rows} outs={outs} names={nameOf} sums={sums} onPick={() => setView((p) => ({ ...p, checks: "bars" }))} />}
            </Block>
            </div>
          )}
          <div className="jd-grid">
              <Block title="Graded runs" state={<State tone={passed ? "good" : rows.length ? "bad" : "dim"}>{`${passed} of ${rows.length} passed`}</State>}>
                {rows.length === 0 ? <p className="empty">No verdicts yet</p> : (
                  <div className="jd-cands" data-testid="candidates">
                    {rows.map((r, i) => {
                      const name = strategyLabel(namesQ, r.strategy_id);
                      const word = VERDICT[r.verdict][1];
                      return (
                        <button type="button" key={r.id} data-verdict={r.id} className={picked && picked.id === r.id ? "jd-c sel" : "jd-c"}
                          aria-pressed={picked !== null && picked.id === r.id} onClick={() => setView((p) => ({ ...p, pickedId: r.id }))}>
                          <Ring cells={outs[i]} size={44} />
                          <span className="jd-c-t"><span className="jd-c-n" title={name.hover}>{name.text}</span><span className="jd-c-s">{`${r.ticker}, graded ${gradedDay(r.graded_at)}`}</span></span>
                          <span className="jd-c-x" title={word}><span className="num">{`${clearedCount(outs[i])} of 9`}</span></span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </Block>
              <Block title="Waiting for the judge" state={ovQ.data ? <State tone={waiting.length ? "warn" : "dim"}>{`${waiting.length} strategies`}</State> : undefined}>
                {ovQ.isError ? <p className="empty">{serverDetail(ovQ.error)}</p> : !ovQ.data ? <p className="empty">Loading the strategies.</p> : <Waiting waiting={waiting} names={nameOf} sender={sender} />}
              </Block>
          </div>
          {picked && <Sheet row={picked} />}
          {rows.length > 0 && (
            <Block title="Blocked checks"
              state={<>
                <State tone="warn">{`${count("setting")} settings not chosen`}</State><State tone="dim">{`${count("input")} inputs missing`}</State>
                {older > 0 && <State tone="dim" hover="Graded before the checks kept their figures, so their checks give no reason to count here">{`${older} older runs`}</State>}
              </>}>
              <WhyList rows={rows} sums={sums} />
            </Block>
          )}
        </div>
      </>
    );
  }

  return (
    <div className="v2-page jd-page">
      <PageHeader crumb={app.name} title={section.label} right={q.data ? (
        <PhoneOff><div className="jd-head-acts">
          <button type="button" className="btn ghost" onClick={toTargets}>Where the targets come from</button>
          <button type="button" className="btn primary" disabled={waiting.length === 0 || sender.send.isPending} onClick={() => sender.send.mutate(waiting)}>{sendWords(waiting.length)}</button>
        </div></PhoneOff>
      ) : undefined} />
      {body}
    </div>
  );
}
