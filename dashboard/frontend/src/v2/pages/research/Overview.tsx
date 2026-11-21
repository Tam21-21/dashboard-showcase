import { useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Block, PageHeader, Say, State, useUnveil } from "../../kit";
import type { NlTone } from "../../kit";
import type { PageProps } from "../../shell/sections";
import {
  fetchDatasets, fetchJudgeSummary, fetchPaperSummary, fetchRuns, fetchRunsSummary,
  fetchStrategies, fetchSystems, fetchVerdicts, overviewKeys,
} from "../../data/overviewApi";
import { RUNS_LIMIT, sendToJudge } from "../../data/runsApi";
import { stations, waitingRuns, type Station, type StationId } from "../../data/stations";
import { strategyLabel, useStrategyNames } from "../../data/strategyNames";
import { CHECKS, outcomes, sumChecks, type CheckSum } from "./Judge";
import { isPriceSeries, isStale } from "./datasets/stale";
import { serverDetail } from "../../../lib/errorText";
import type { Run } from "../../../types/research";
import "./overview.css";

// The Research overview as the frozen mockup draws it (night of 2026-11-15, page "overview2"). The questions, in this order:
// what stops the lab and whose move is it, how far does work get, and why does the judge pass nothing? Every figure is read
// from the same routes as before; nothing is typed in. One colour, one meaning: green works, red is broken or failed, amber
// waits for you, grey is not reached. Left out because the server has nothing behind them: any capital figure.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const COUNT = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"];
const DAY_MS = 86_400_000;
const WEEK_MS = 7 * DAY_MS;
const MAX_WEEKS = 52;
const SHOWN = 8; // the newest waiting runs listed; the rest are a count, so a long queue stays one card tall
const FAIL_SHOWN = 5;
const dayMon = (ms: number) => `${new Date(ms).getUTCDate()} ${MONTHS[new Date(ms).getUTCMonth()]}`;
const day = (s: number) => dayMon(s * 1000);
const plural = (n: number, one: string, many: string) => (n === 1 ? one : many);
const count = (n: number) => COUNT[n] ?? String(n);
const paris = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const parisDay = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", day: "numeric", month: "short" });

// Weeks start on Monday, in UTC, the clock the server stores.
function weekStartMs(ms: number): number {
  const d = new Date(ms);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - ((d.getUTCDay() + 6) % 7) * DAY_MS;
}

export interface WeekCount { start: number; ok: number; failed: number }

/** Finished and failed runs per week, from the first run's week (at most the last 52) to this week. */
export function weekCounts(runs: Run[], nowMs: number): WeekCount[] {
  const done = runs.filter((r) => r.status === "succeeded" || r.status === "failed");
  if (done.length === 0) return [];
  const last = weekStartMs(nowMs);
  const first = Math.max(weekStartMs(Math.min(...done.map((r) => r.created_at * 1000))), last - (MAX_WEEKS - 1) * WEEK_MS);
  const n = Math.round((last - first) / WEEK_MS) + 1;
  const weeks: WeekCount[] = Array.from({ length: n }, (_, i) => ({ start: first + i * WEEK_MS, ok: 0, failed: 0 }));
  for (const r of done) {
    const i = Math.round((weekStartMs(r.created_at * 1000) - first) / WEEK_MS);
    if (i < 0 || i >= n) continue;
    if (r.status === "succeeded") weeks[i].ok += 1;
    else weeks[i].failed += 1;
  }
  return weeks;
}

// A Python error line starts with its class ("KeyError: ..."); runs that failed with the same class are one reason.
const ERROR_CLASS = /^([A-Z][A-Za-z0-9]*(?:Error|Exception)): /;

// The few error lines the lab itself writes, said in plain words (each is recognised by the code or phrase the lab puts in it);
// any other line is shown as the run wrote it.
const PLAIN: Array<[RegExp, string]> = [
  [/reason=undefined_warmup/, "Volatility undefined at entry"],
  [/reason=insufficient_lead_in/, "Not enough history before the start"],
  [/deliberate container failure/, "A planned failure test"],
  [/quantile value .* not in \[0, ?1\]/, "A setting out of range"],
];
const plainOf = (line: string): string | null => PLAIN.find(([re]) => re.test(line))?.[1] ?? null;

export interface FailReason { key: string; text: string; n: number; lines: string[] }

/**
 * Failed runs grouped by their error's class, or by the whole first line when it names none, most first. The text is the
 * newest run's first line without the class; every distinct line of the group goes on the hover.
 */
export function failReasons(runs: Run[]): FailReason[] {
  const m = new Map<string, { n: number; newest: Run; lines: Set<string> }>();
  for (const r of runs) {
    if (r.status !== "failed") continue;
    const line = (r.error ?? "").split("\n")[0].trim() || "No error recorded";
    const key = plainOf(line) ?? ERROR_CLASS.exec(line)?.[1] ?? line;
    const g = m.get(key) ?? { n: 0, newest: r, lines: new Set<string>() };
    g.n += 1;
    g.lines.add(line);
    if (r.created_at > g.newest.created_at || (r.created_at === g.newest.created_at && r.id > g.newest.id)) g.newest = r;
    m.set(key, g);
  }
  return [...m.entries()]
    .map(([key, g]) => {
      const line = (g.newest.error ?? "").split("\n")[0].trim() || "No error recorded";
      return { key, text: plainOf(line) ?? line.replace(ERROR_CLASS, ""), n: g.n, lines: [...g.lines] };
    })
    .sort((a, b) => b.n - a.n || a.key.localeCompare(b.key));
}

// ---- A share bar: each part is [colour, count]; parts with nothing in them are left out, and an empty bar is an outline.
type Tone = "good" | "bad" | "warn" | "soft";
type Part = [Tone, number];
export function Share({ parts, gaps = false, hover }: { parts: Part[]; gaps?: boolean; hover?: string }) {
  const live = parts.filter((p) => p[1] > 0);
  return (
    <span className={`ov7-share${gaps ? " parts" : ""}${live.length === 0 ? " none" : ""}`} title={hover}>
      {live.map(([t, n], k) => <i key={k} className={t} style={{ flexGrow: n }} />)}
    </span>
  );
}

// ---- The line: six stations, lit as far as work has got; each station's colour by Tamer's rule, the reason on hover.
function StationLine({ list, pics, onGo }: { list: Station[]; pics: Partial<Record<StationId, ReactNode>>; onGo: (path: string) => void }) {
  const reached = list.reduce((k, s, i) => (s.tone !== null && s.tone !== "dim" ? i : k), 0);
  return (
    <div className="ov6-line" style={{ ["--lit" as string]: reached / (list.length - 1) }}>
      <div className="ov6-track" aria-hidden="true"><i /></div>
      <div className="ov6-stops">
        {list.map((s, i) => {
          const tone: NlTone = s.tone ?? "dim";
          return (
            <button
              key={s.id} type="button" className={`ov6-stop t-${tone}`} data-stage={s.id} style={{ ["--i" as string]: i }}
              title={`${s.name}: ${s.word}. ${s.why} ${s.rule}`} onClick={() => onGo(s.path)}
            >
              <span className="node" />
              <span className="nm">{s.name}</span>
              <span className="fig num">{s.figure}</span>
              <span className="pic">{pics[s.id]}</span>
              <span className="fact">{s.fact}</span>
              <State tone={tone}>{s.word}</State>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---- What stops the lab. Red is the lab's to fix, amber is your move.
interface Stopper {
  key: string; tone: "bad" | "warn"; title: string; detail: string; bar: ReactNode; cap: string;
  act: string; onAct: () => void; busy?: boolean; error?: string | null;
}

const KIND: Record<CheckSum["kind"], [NlTone, string]> = { setting: ["warn", "Needs a value"], input: ["bad", "No data"], runs: ["good", "Works"] };
const KIND_ORDER: Record<CheckSum["kind"], number> = { setting: 0, input: 1, runs: 2 };

// ---- Runs, week by week: finished green, failed red on top; the bars grow from their base (transform only).
function WeekBars({ weeks, capped }: { weeks: WeekCount[]; capped: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useUnveil(ref, "y");
  const top = Math.max(1, ...weeks.map((w) => w.ok + w.failed));
  const peak = weeks.find((w) => w.ok + w.failed === top);
  return (
    <div className="ov6-weeks">
      {peak && <p className="ov7-note"><b className="num">{top}</b>{` ${plural(top, "run", "runs")} in the busiest week, the week of ${dayMon(peak.start)}.${capped ? ` The newest ${MAX_WEEKS} weeks are shown.` : ""}`}</p>}
      <div className="bars" ref={ref}>
        {weeks.map((w) => (
          <div key={w.start} className="wk" title={`Week of ${dayMon(w.start)}: ${w.ok} finished, ${w.failed} failed`}>
            {w.ok + w.failed === 0 ? <i className="none" /> : (
              <>
                {w.ok > 0 && <i className="good" style={{ height: `${(w.ok / top) * 100}%` }} />}
                {w.failed > 0 && <i className="bad" style={{ height: `${(w.failed / top) * 100}%` }} />}
              </>
            )}
          </div>
        ))}
      </div>
      <div className="axis"><span>{dayMon(weeks[0].start)}</span><span>{dayMon(weeks[weeks.length >> 1].start)}</span><span>{dayMon(weeks[weeks.length - 1].start)}</span></div>
    </div>
  );
}

const Note = ({ children }: { children: ReactNode }) => <p className="ov6-empty">{children}</p>;
const pending = (q: { isError: boolean; error: unknown }) => <Note>{q.isError ? serverDetail(q.error) : "Loading"}</Note>;

export default function Overview({ app, section }: PageProps) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const namesQ = useStrategyNames();
  const datasetsQ = useQuery({ queryKey: overviewKeys.datasets, queryFn: fetchDatasets });
  const strategiesQ = useQuery({ queryKey: overviewKeys.strategies, queryFn: fetchStrategies });
  const runsQ = useQuery({ queryKey: overviewKeys.runs, queryFn: fetchRuns });
  const summaryQ = useQuery({ queryKey: overviewKeys.runsSummary, queryFn: fetchRunsSummary });
  const judgeQ = useQuery({ queryKey: overviewKeys.judgeSummary, queryFn: fetchJudgeSummary });
  const verdictsQ = useQuery({ queryKey: overviewKeys.verdicts, queryFn: fetchVerdicts });
  const paperQ = useQuery({ queryKey: overviewKeys.paperSummary, queryFn: fetchPaperSummary });
  const systemsQ = useQuery({ queryKey: overviewKeys.systems, queryFn: fetchSystems });
  const [sending, setSending] = useState<{ done: number; of: number; error: string | null } | null>(null);

  const nowMs = Date.now();
  const ds = datasetsQ.data, st = strategiesQ.data, runs = runsQ.data, j = judgeQ.data, paper = paperQ.data?.running_count;
  // A station whose route failed says so on hover, in the server's words.
  const failedRead: Partial<Record<StationId, unknown>> = {
    data: datasetsQ.error, strategies: strategiesQ.error ?? runsQ.error, runs: runsQ.error ?? summaryQ.error, judge: judgeQ.error, paper: paperQ.error,
  };
  // The run counts are the whole log's (the summary groups every run by status, so a missing status counted none).
  const sum = summaryQ.data;
  const list = stations({
    datasets: ds, strategies: st, runs, runsTotal: sum?.total, runsFailed: sum ? (sum.by_status.failed ?? 0) : undefined,
    judge: j, paperRunning: paper, now: Math.floor(nowMs / 1000),
  })
    .map((s) => (failedRead[s.id] ? { ...s, word: "Could not read", why: serverDetail(failedRead[s.id]) } : s));
  const waiting = st && runs ? waitingRuns(st, runs) : null;

  // The judge's checks, over the graded runs that still count (current, not rejected), as the Judge page reads them.
  const graded = verdictsQ.data ? verdictsQ.data.rows.filter((v) => v.current && !v.rejected) : null;
  const sums = graded ? sumChecks(graded, graded.map(outcomes)) : null;
  const judged = graded !== null && graded.length > 0;
  const ofKind = (k: CheckSum["kind"]) => (sums ? sums.map((s, i) => ({ s, i })).filter((x) => x.s.kind === k) : []);
  const settings = ofKind("setting"), inputs = ofKind("input"), figured = ofKind("runs");
  const names = (xs: Array<{ i: number }>) => xs.map((x) => CHECKS[x.i].name).join(", ");

  // ---- The share bars under each station's figure.
  const prices = ds ? ds.filter(isPriceSeries) : [];
  const behind = prices.filter((d) => d.covered_end_ts != null && isStale(d.covered_end_ts, nowMs / 1000));
  const failedSets = ds ? ds.filter((d) => d.status === "failed").length : 0;
  const pics: Partial<Record<StationId, ReactNode>> = {};
  if (ds) pics.data = <Share parts={[["bad", failedSets], ["warn", behind.length], ["soft", Math.max(0, ds.length - behind.length - failedSets)]]} />;
  if (st && waiting) pics.strategies = <Share parts={[["warn", waiting.length], ["soft", Math.max(0, st.length - waiting.length)]]} />;
  if (sum) pics.runs = <Share parts={[["good", sum.by_status.succeeded ?? 0], ["bad", sum.by_status.failed ?? 0]]} />;
  if (j) pics.judge = <Share parts={[["good", j.survivors], ["bad", j.unjudgeable]]} />;
  if (paper != null) pics.paper = <Share parts={[["good", paper]]} />;
  pics.capital = <Share parts={[]} hover="Real money needs three things first: a kill switch on the dashboard, hard limits on the server, your recorded go" />;

  // ---- The send: one run after the next; the button comes back only once the lists are read again.
  const send = async () => {
    if (!waiting || waiting.length === 0) return;
    for (let k = 0; k < waiting.length; k++) {
      setSending({ done: k, of: waiting.length, error: null });
      try {
        await sendToJudge(waiting[k].id);
      } catch (e) {
        // Read the list again first: the runs sent before this one leave it, so the button that comes back cannot resend them.
        const error = `Run ${waiting[k].id} was not sent: ${serverDetail(e)}`;
        await qc.invalidateQueries({ queryKey: ["overview"] });
        setSending({ done: k, of: waiting.length, error });
        return;
      }
    }
    await qc.invalidateQueries({ queryKey: ["overview"] });
    setSending(null);
  };
  const busy = sending !== null && sending.error === null;

  // ---- What stops the lab, in the order work flows: what runs it, what feeds it, what judges it, what waits behind that.
  const stoppers: Stopper[] = [];
  for (const s of (systemsQ.data?.systems ?? []).filter((x) => x.tone === "bad")) {
    const ok = s.marks.filter((m) => m.mark === "ok").length, fail = s.marks.filter((m) => m.mark === "fail").length;
    const since = s.last_proven_at ? Date.parse(s.last_proven_at) : NaN;
    stoppers.push({
      key: `sys-${s.name}`, tone: "bad", title: `The ${s.name.toLowerCase()} is down`,
      detail: `${s.reason || "No reason recorded"}${Number.isFinite(since) ? ` since ${parisDay.format(since)}, ${paris.format(since)}` : ""}.`,
      bar: <Share parts={[["good", ok], ["bad", fail]]} />,
      cap: ok + fail > 0 ? `up for ${Math.round((ok / (ok + fail)) * 100)}% of the last 24 hours` : "not checked in the last 24 hours",
      act: "Open Operations", onAct: () => navigate("/app/research/operations"),
    });
  }
  if (failedSets > 0) {
    stoppers.push({
      key: "failed-sets", tone: "bad", title: `${failedSets} ${plural(failedSets, "dataset failed its", "datasets failed their")} last fetch`, detail: "The fetch stopped with an error.",
      bar: <Share parts={[["bad", failedSets], ["soft", (ds?.length ?? 0) - failedSets]]} />, cap: `${failedSets} of ${ds?.length ?? 0} datasets`,
      act: "Open the data library", onAct: () => navigate("/app/research/data-library"),
    });
  }
  if (behind.length > 0) {
    // The day most of the late series stopped on.
    const byDay = new Map<string, number>();
    for (const d of behind) { const k = String(Math.floor((d.covered_end_ts as number) / 86400)); byDay.set(k, (byDay.get(k) ?? 0) + 1); }
    const [topDay, topN] = [...byDay.entries()].sort((a, b) => b[1] - a[1])[0];
    const ago = Math.floor((nowMs / 1000 - Number(topDay) * 86400) / 86400);
    stoppers.push({
      key: "behind", tone: "warn", title: `${behind.length} price ${plural(behind.length, "series is", "series are")} behind`,
      detail: `${topN} of them stopped on ${day(Number(topDay) * 86400)}, ${ago} days ago.`,
      bar: <Share parts={[["warn", behind.length], ["soft", Math.max(0, prices.length - behind.length)]]} />, cap: `${behind.length} of ${prices.length} price series`,
      act: "Open the data library", onAct: () => navigate("/app/research/data-library"),
    });
  }
  if (judged && sums && settings.length > 0) {
    stoppers.push({
      key: "settings", tone: "warn", title: `${settings.length} ${plural(settings.length, "check needs", "checks need")} a value from you`,
      detail: `${names(settings)}. ${settings.length === 1 ? "It cannot" : "None can"} run until you give it one.`,
      bar: <Share gaps parts={sums.map((s): Part => [s.kind === "setting" ? "warn" : "soft", 1])} />, cap: `${settings.length} of the ${sums.length} checks`,
      act: "Open the judge", onAct: () => navigate("/app/research/judge"),
    });
  }
  if (judged && sums && inputs.length > 0) {
    stoppers.push({
      key: "inputs", tone: "bad", title: `${inputs.length} ${plural(inputs.length, "check has", "checks have")} no data to read`,
      detail: `${names(inputs)}. The runs did not save what ${inputs.length === 1 ? "it needs" : "they need"}.`,
      bar: <Share gaps parts={sums.map((s): Part => [s.kind === "input" ? "bad" : "soft", 1])} />, cap: `${inputs.length} of the ${sums.length} checks`,
      act: "Open the judge", onAct: () => navigate("/app/research/judge"),
    });
  }
  if (waiting && waiting.length > 0 && st) {
    stoppers.push({
      key: "waiting", tone: "warn", title: `${waiting.length} ${plural(waiting.length, "run waits", "runs wait")} for the judge`,
      detail: `One for each strategy.${sums && figured.length < sums.length ? ` No run can pass yet, because only ${figured.length} of the ${sums.length} checks work today.` : ""}`,
      bar: <Share parts={[["warn", waiting.length], ["soft", Math.max(0, st.length - waiting.length)]]} />, cap: `${waiting.length} of ${st.length} strategies`,
      act: busy ? `Sending ${sending!.done + 1} of ${sending!.of}` : `Send ${waiting.length} ${plural(waiting.length, "run", "runs")} to the judge`,
      onAct: () => void send(), busy, error: sending?.error,
    });
  }
  const mine = stoppers.filter((s) => s.tone === "warn").length, broken = stoppers.length - mine;
  const stopReads = datasetsQ.isSuccess && runsQ.isSuccess && strategiesQ.isSuccess && verdictsQ.isSuccess && systemsQ.isSuccess;

  // ---- The finding, said once: only what the counts say.
  let say: ReactNode = null;
  if (j && paper != null && stopReads) {
    const reach = paper > 0 ? `${paper} on paper.` : j.survivors > 0 ? `${j.survivors} of ${j.graded} judged runs passed.` : "Nothing has reached paper yet.";
    say = stoppers.length === 0
      ? <Say lead="Nothing stops the lab." more={reach} />
      : (
        <Say
          lead={`${count(stoppers.length)} ${plural(stoppers.length, "thing stops", "things stop")} the lab.`}
          more={<>
            {mine > 0 && <em className="warn">{`${count(mine)} ${plural(mine, "waits", "wait")} for you`}</em>}
            {mine > 0 && broken > 0 && " and "}
            {broken > 0 && <em className="bad">{`${mine > 0 ? count(broken).toLowerCase() : count(broken)} ${plural(broken, "is", "are")} broken`}</em>}
            {`. ${reach}`}
          </>}
        />
      );
  }

  const weeks = runs ? weekCounts(runs, nowMs) : null;
  const reasons = runs ? failReasons(runs) : null;
  const shownReasons = reasons ? reasons.slice(0, FAIL_SHOWN) : [];
  const otherN = reasons ? reasons.slice(FAIL_SHOWN).reduce((a, f) => a + f.n, 0) : 0;
  const reasonRows = otherN > 0 ? [...shownReasons, { key: "other", text: "Other reasons", n: otherN, lines: reasons!.slice(FAIL_SHOWN).flatMap((f) => f.lines) }] : shownReasons;
  const reasonTop = Math.max(1, ...reasonRows.map((f) => f.n));
  const tickStep = Math.max(1, Math.ceil(reasonTop / 3));
  const ticks = Array.from({ length: Math.floor(reasonTop / tickStep) + 1 }, (_, v) => v * tickStep);
  // The bars read the runs list, which stops at its newest RUNS_LIMIT; past that, the card says so.
  const capped = runs != null && runs.length >= RUNS_LIMIT
    ? <State tone="dim" hover={`These count the newest ${RUNS_LIMIT} runs${sum ? ` of the ${sum.total} in the log` : ""}.`}>{`Newest ${RUNS_LIMIT}`}</State>
    : null;
  const okRuns = sum ? (sum.by_status.succeeded ?? 0) : 0, failedRuns = sum ? (sum.by_status.failed ?? 0) : 0;

  return (
    <div className="v2-page ov6">
      <PageHeader crumb={app.name} title={section.label} />
      {say}
      <Block title="Pipeline overview" className="ov6-pipe">
        <StationLine list={list} pics={pics} onGo={navigate} />
      </Block>

      <Block
        title="Needs you" className="ov7-needs"
        state={stopReads ? <>{mine > 0 && <State tone="warn">{`${mine} wait for you`}</State>}{broken > 0 && <State tone="bad">{`${broken} broken`}</State>}</> : undefined}
      >
        {!stopReads ? pending(systemsQ.isError ? systemsQ : verdictsQ.isError ? verdictsQ : runsQ.isError ? runsQ : datasetsQ.isError ? datasetsQ : strategiesQ)
          : stoppers.length === 0 ? <Note>Every part is running, the data is fresh and the judge can decide.</Note> : (
            <div className="ov7-stops">
              {stoppers.map((s) => (
                <div key={s.key} className="ov7-stop" data-stopper={s.key}>
                  <State tone={s.tone}>{s.tone === "bad" ? "Broken" : "Waits for you"}</State>
                  <div className="what"><b>{s.title}</b><small>{s.detail}</small></div>
                  <div className="meter">{s.bar}<small>{s.cap}</small></div>
                  <button type="button" className="btn" disabled={s.busy} onClick={s.onAct}>{s.act}</button>
                  {s.error && <p className="ov6-err" role="alert">{s.error}</p>}
                </div>
              ))}
            </div>
          )}
      </Block>

      <div className="ov6-cols">
        <Block
          title="Judge summary"
          state={sums ? <>
            <State tone="good">{`${figured.length} work`}</State>
            {settings.length > 0 && <State tone="warn">{`${settings.length} need a value`}</State>}
            {inputs.length > 0 && <State tone="bad">{`${inputs.length} have no data`}</State>}
          </> : undefined}
        >
          {!graded || !sums ? pending(verdictsQ) : graded.length === 0 ? <Note>No run has been judged yet.</Note> : (
            <>
              <div className="ov7-checks">
                {sums.map((s, i) => ({ s, i })).sort((a, b) => KIND_ORDER[a.s.kind] - KIND_ORDER[b.s.kind] || a.i - b.i).map(({ s, i }) => {
                  const tally = [s.cleared && `${s.cleared} cleared`, s.failed && `${s.failed} failed`, s.other && `${s.other} could not run`].filter(Boolean).join(", ") || "no run looked at yet";
                  return (
                    <div key={i} className="ov7-check" data-check={i + 1}
                      title={`${CHECKS[i].name}\n${CHECKS[i].question}\nCleared ${s.cleared}, failed ${s.failed}, could not run ${s.other}${s.top ? `\n${s.top}` : ""}`}>
                      <span className="num no">{String(i + 1).padStart(2, "0")}</span>
                      <span className="cn"><b>{CHECKS[i].name}</b><small>{CHECKS[i].question}</small></span>
                      <span className="cv"><Share parts={[["good", s.cleared], ["bad", s.failed], ["soft", s.other]]} /><small>{tally}</small></span>
                      <State tone={KIND[s.kind][0]}>{KIND[s.kind][1]}</State>
                    </div>
                  );
                })}
              </div>
              <div className="ov6-key">
                <span><i className="c good" />Cleared</span><span><i className="c bad" />Failed</span><span><i className="c none" />Could not run</span>
                <span>{`Each bar is the ${graded.length} runs the judge has looked at`}</span>
              </div>
            </>
          )}
        </Block>
        <Block
          title="Needs approval" className="ov6-wait"
          state={waiting ? <State tone={waiting.length > 0 ? "warn" : "dim"}>{`${waiting.length} ${plural(waiting.length, "run", "runs")}`}</State> : undefined}
        >
          {!waiting ? pending(runsQ.isError ? runsQ : strategiesQ) : waiting.length === 0 ? <Note>No finished run waits for the judge.</Note> : (
            <>
              <div className="ov6-list">
                {[...waiting].sort((a, b) => b.created_at - a.created_at || b.id - a.id).slice(0, SHOWN).map((r, k) => {
                  const n = strategyLabel(namesQ, r.strategy_id);
                  return (
                    <div key={r.id} className={busy && k < sending!.done ? "li sent" : "li"} style={{ ["--k" as string]: k }}>
                      <span className="nm" title={n.hover}>{n.text}</span>
                      <span className="ov6-when">{`run ${r.id}, ${day(r.created_at)}`}</span>
                    </div>
                  );
                })}
              </div>
              {waiting.length > SHOWN && <p className="ov7-more">{`The ${SHOWN} newest. ${waiting.length - SHOWN} more on Strategies.`}</p>}
            </>
          )}
          <div className="ov6-acts"><button type="button" className="btn" onClick={() => navigate("/app/research/strategies")}>Open Strategies</button></div>
        </Block>
      </div>

      <div className="ov6-cols">
        <Block title="Runs summary" state={<>{sum && <><State tone="good" hover={`Of all ${sum.total} runs in the log`}>{`${okRuns} finished`}</State>{failedRuns > 0 && <State tone="bad" hover={`Of all ${sum.total} runs in the log`}>{`${failedRuns} failed`}</State>}</>}{capped}</>}>
          {!weeks ? pending(runsQ) : weeks.length === 0 ? <Note>No run has finished yet.</Note> : <WeekBars weeks={weeks} capped={weeks.length >= MAX_WEEKS} />}
        </Block>
        <Block title="Reasons of failure" state={<>{sum && failedRuns > 0 && <State tone="bad">{`${failedRuns} of ${sum.total} runs`}</State>}{capped}</>}>
          {!runs ? pending(runsQ) : reasonRows.length === 0 ? <Note>No run has failed.</Note> : (
            <div className="ov7-why">
              {reasonRows.map((f, k) => (
                <div key={f.key} style={{ ["--k" as string]: k }} title={[...(f.key === f.lines[0] ? [] : [f.key]), ...f.lines].join("\n")}>
                  <div className="lab">{f.text}</div>
                  <div className="row"><div className="trk" style={{ ["--p" as string]: f.n / reasonTop }}><i /><span className="num">{f.n}</span></div></div>
                </div>
              ))}
              <div className="gl" aria-hidden="true">{ticks.map((v) => <i key={v} />)}</div>
              <div className="axis" aria-hidden="true">{ticks.map((v) => <span key={v}>{v}</span>)}</div>
            </div>
          )}
        </Block>
      </div>
    </div>
  );
}
