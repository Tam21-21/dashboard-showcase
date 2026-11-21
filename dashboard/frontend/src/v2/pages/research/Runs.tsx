import { useEffect, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { PhoneOff } from "../../kit/phone";
import { Block, PageHeader, Say, Segmented, Spark, State, Table, usePageMemory } from "../../kit";
import type { Cell } from "../../kit";
import type { PageProps } from "../../shell/sections";
import { RUNS_LIMIT, fetchRuns, fetchRunsSummary, runsKeys, sendToJudge } from "../../data/runsApi";
import { strategyLabel, useStrategyNames } from "../../data/strategyNames";
import type { StrategyLabel } from "../../data/strategyNames";
import { serverDetail } from "../../../lib/errorText";
import type { Run } from "../../../types/research";
import { KIND_COUNT_WORD, KIND_ORDER, KIND_TONE, KIND_WORD } from "./runs/kinds";
import type { RunKind } from "./runs/kinds";
import { WeekBars } from "./runs/WeekBars";
import type { Week } from "./runs/WeekBars";
import { RunScatter } from "./runs/RunScatter";
import { MoneyLine, pct } from "./runs/RunFigures";
import "./runs.css";

// Round seven (Tamer 2026-11-15), a replica of the mockup's Runs page (lab-p-runs.js). The question: what ran, how did
// each run end, and which runs are worth a closer look? The finding in one sentence; runs by week; the runs with
// results as a scatter; the picked run in full; every run as a row.

const RANGES = [
  { label: "All runs", value: "all" },
  { label: "This week", value: "week" },
  { label: "This month", value: "month" },
];
const SHOWN = 12;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const TZ = "Europe/Paris";
const WEEK = 604800;
const GREEN = "#2fe08a";
const RED = "#ff5a63";

const dash = (why: string): ReactNode => <span title={why} className="dim">-</span>;

const isDone = (r: Run) => r.status === "succeeded";
const hasFigures = (r: Run) => isDone(r) && !r.is_sweep && r.sharpe_ratio != null && r.max_drawdown != null;

// Times show on the Paris clock.
const PARIS = new Intl.DateTimeFormat("en-GB", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  weekday: "short", hourCycle: "h23",
});
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
type Parts = { y: number; m: number; d: number; hh: string; mm: string; ss: number; wd: number };

function parisParts(ms: number): Parts {
  const p: Record<string, string> = {};
  for (const x of PARIS.formatToParts(new Date(ms))) p[x.type] = x.value;
  return { y: Number(p.year), m: Number(p.month), d: Number(p.day), hh: p.hour, mm: p.minute, ss: Number(p.second), wd: WEEKDAYS.indexOf(p.weekday) };
}

// How far the Paris clock is ahead of UTC at this instant, in milliseconds.
function parisOffset(ms: number): number {
  const whole = Math.floor(ms / 1000) * 1000;
  const p = parisParts(whole);
  return Date.UTC(p.y, p.m - 1, p.d, Number(p.hh), Number(p.mm), p.ss) - whole;
}

// The instant (ms) when the Paris clock reads 00:00 on this calendar day. Two passes so a day with a
// clock change still lands on midnight.
function parisMidnight(y: number, m: number, d: number): number {
  const wall = Date.UTC(y, m - 1, d);
  const first = wall - parisOffset(wall);
  return wall - parisOffset(first);
}

/** The epoch second a range starts at (Monday 00:00 or the 1st 00:00, Paris time); null for all runs. */
export function rangeStart(value: string, nowMs: number): number | null {
  if (value !== "week" && value !== "month") return null;
  const p = parisParts(nowMs);
  const first = new Date(Date.UTC(p.y, p.m - 1, value === "week" ? p.d - p.wd : 1));
  return parisMidnight(first.getUTCFullYear(), first.getUTCMonth() + 1, first.getUTCDate()) / 1000;
}

/** The epoch second of the Monday 00:00 (Paris) of the week this second falls in. */
function weekOf(sec: number): number {
  const p = parisParts(sec * 1000);
  const first = new Date(Date.UTC(p.y, p.m - 1, p.d - p.wd));
  return parisMidnight(first.getUTCFullYear(), first.getUTCMonth() + 1, first.getUTCDate()) / 1000;
}

function whenLabel(sec: number, nowMs: number): string {
  const p = parisParts(sec * 1000);
  const t = parisParts(nowMs);
  const yest = new Date(Date.UTC(t.y, t.m - 1, t.d - 1));
  const sameDay = (y: number, m: number, d: number) => p.y === y && p.m === m && p.d === d;
  const name = sameDay(t.y, t.m, t.d)
    ? "Today"
    : sameDay(yest.getUTCFullYear(), yest.getUTCMonth() + 1, yest.getUTCDate())
      ? "Yesterday"
      : `${p.d} ${MONTHS[p.m - 1]}`;
  return `${name} ${p.hh}:${p.mm}`;
}

const dayLabel = (sec: number) => {
  const p = parisParts(sec * 1000);
  return `${p.d} ${MONTHS[p.m - 1]}`;
};
const dayYear = (sec: number) => `${dayLabel(sec)} ${parisParts(sec * 1000).y}`;

// How long a run took, from its start (or, when the start was never stamped, from when it was made) to its end.
function seconds(r: Run): number | null {
  if (r.finished_at == null) return null;
  return Math.max(0, Math.round(r.finished_at - (r.started_at ?? r.created_at)));
}
function lengthText(s: number | null): string | null {
  if (s == null) return null;
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}

const dropText = (dd: number | null) => (dd == null ? "-" : pct(-dd));
const signOf = (n: number | null | undefined): "good" | "bad" | "soft" => (n == null || n === 0 ? "soft" : n > 0 ? "good" : "bad");

// The server keeps the error as "ErrorName: message". The page says the cause in plain words.
function plainWhy(error: string | null): string {
  if (!error) return "no reason was saved";
  const cut = error.indexOf(": ");
  const name = cut > 0 ? error.slice(0, cut) : "";
  const msg = cut > 0 ? error.slice(cut + 2) : error;
  if (name === "UndefinedVolatilityError") return "volatility undefined at entry";
  if (name === "InsufficientLeadInError") return "not enough history before the start";
  return msg.length > 70 ? msg.slice(0, 70).trim() : msg;
}

type Judgeable = { kind: RunKind; cur: boolean; gone: boolean; why: string | null };

function RunCard({ run, name, info, nowMs, peers }: { run: Run; name: StrategyLabel; info: Judgeable; nowMs: number; peers: Run[] }) {
  const navigate = useNavigate();
  const [sent, setSent] = useState<{ id: number; ok: boolean; text: string } | null>(null);
  const judge = useMutation({
    mutationFn: (id: number) => sendToJudge(id),
    onSuccess: (_d, id) => setSent({ id, ok: true, text: "Sent to the judge" }),
    onError: (e, id) => setSent({ id, ok: false, text: serverDetail(e) }),
  });
  const len = lengthText(seconds(run));
  const spark = run.equity_sparkline ?? [];
  const msg = sent && sent.id === run.id ? sent : null;
  const figs = hasFigures(run);
  const judged = run.verdict != null;
  const none =
    run.status === "failed" ? `This run stopped with an error: ${info.why}.`
    : run.status === "pending" || run.status === "running" ? "This run has not finished."
    : run.status === "cancelled" ? "This run was cancelled, so it has no figures."
    : info.cur ? "A first screen of a current strategy. Its results are in the Generator's search for it."
    : "This run saved no single set of figures: it was a sweep across many stocks, or a test.";
  const rank = (get: (p: Run) => number | null | undefined, mine: number, word: string) => {
    const all = peers.map(get).filter((v): v is number => v != null);
    return `${word} than ${all.filter((v) => v < mine).length} of the ${all.length} runs with figures`;
  };
  const facts: Array<[string, string, string, string]> = figs
    ? [
        ["Return", run.total_return == null ? "-" : pct(run.total_return), signOf(run.total_return) === "soft" ? "" : signOf(run.total_return) === "good" ? "g" : "rd", run.total_return == null ? "No return was saved" : rank((p) => p.total_return, run.total_return, "Higher")],
        ["Sharpe", (run.sharpe_ratio as number).toFixed(2), "", rank((p) => p.sharpe_ratio, run.sharpe_ratio as number, "Higher")],
        ["Deepest drop", dropText(run.max_drawdown), "rd", rank((p) => p.max_drawdown, run.max_drawdown as number, "Deeper")],
        ["Trades", run.num_trades == null ? "-" : run.num_trades.toLocaleString("en-GB"), "", run.num_trades == null ? "No trade count was saved" : rank((p) => p.num_trades, run.num_trades, "Higher")],
      ]
    : [];
  if (figs && run.win_rate != null) facts.push(["Winning trades", `${Math.round(run.win_rate * 100)}%`, "", "The share of its trades that made money"]);
  if (figs && run.profit_factor != null) facts.push(["Won for each 1 lost", run.profit_factor.toFixed(2), "", "All gains divided by all losses"]);
  return (
    <div data-testid="run-card" className="rs-card">
      <Block title={`Run ${run.id}`} state={<State tone={KIND_TONE[info.kind]}>{KIND_WORD[info.kind]}</State>} right={<span className="rs-when">{`${dayYear(run.created_at)}, ${len ?? "-"}`}</span>}>
        <p className="rnm" title={name.hover} data-when={whenLabel(run.created_at, nowMs)}>
          {name.text}
          {info.gone && <span className="rs-gone">deleted</span>}
        </p>
        {spark.length >= 2 ? <MoneyLine values={spark} /> : <p className="rs-none">{none}</p>}
        {figs && (
          <div className="rs-fa">
            {facts.map(([label, value, tone, why]) => (
              <div key={label} className="rf-fact" title={why}>
                <span className="rf-l">{label}</span>
                <b className={`num${tone ? ` ${tone}` : ""}`}>{value}</b>
              </div>
            ))}
          </div>
        )}
        <div className="rs-act">
          {judged ? (
            <button type="button" className="btn" onClick={() => navigate("/app/research/judge")}>Open the judge</button>
          ) : (
            isDone(run) && (
              <PhoneOff>
                <button type="button" className="btn" disabled={judge.isPending} onClick={() => judge.mutate(run.id)}>Send to the judge</button>
              </PhoneOff>
            )
          )}
          {info.cur && <button type="button" className="btn" onClick={() => navigate("/app/research/generator")}>Open its search</button>}
          {msg && <span className={msg.ok ? "g" : "rd"} role="status">{msg.text}</span>}
        </div>
      </Block>
    </div>
  );
}

export default function Runs({ app, section }: PageProps) {
  // `days` keeps its old name so a memory written by the earlier page ("1", "7", "30") still parses; it reads as all runs.
  const [view, setView] = usePageMemory<{ days: string; pickedId: number | null; showAll: boolean; filter?: string }>(
    "research/runs", { days: "all", pickedId: null, showAll: false, filter: "all" },
  );
  const { pickedId, showAll } = view;
  const range = RANGES.some((o) => o.value === view.days) ? view.days : "all";
  const filter = ["all", "figs", "wait", "failed"].includes(view.filter ?? "") ? (view.filter as string) : "all";
  const pick = (id: number) => setView((v) => ({ ...v, pickedId: id }));

  // A link from another page (?run=<id>) opens that run, with every row listed.
  const [params] = useSearchParams();
  const asked = Number(params.get("run"));
  const wanted = Number.isInteger(asked) && asked > 0 ? asked : null;
  useEffect(() => {
    if (wanted != null) setView((v) => ({ ...v, pickedId: wanted, showAll: true, days: "all", filter: "all" }));
  }, [wanted, setView]);

  const summaryQ = useQuery({
    queryKey: runsKeys.summary,
    queryFn: fetchRunsSummary,
    refetchInterval: (q) => ((q.state.data?.in_flight_count ?? 0) > 0 ? 10_000 : false),
  });
  const busy = (summaryQ.data?.in_flight_count ?? 0) > 0;
  const listQ = useQuery({ queryKey: runsKeys.list, queryFn: fetchRuns, refetchInterval: busy ? 10_000 : false });
  const namesQ = useStrategyNames();

  const nowMs = Date.now();
  const all = listQ.data;
  const start = rangeStart(range, nowMs);
  const rows = (all ?? [])
    .filter((r) => start == null || r.created_at >= start)
    .sort((a, b) => b.created_at - a.created_at || b.id - a.id);
  const labelOf = (r: Run): StrategyLabel => strategyLabel(namesQ, r.strategy_id);
  const ready = all !== undefined && !namesQ.isPending;
  const failedList = listQ.isError;

  // A strategy still in the list is a current one; its newest sweeps wait for the judge. A deleted one is gone.
  const nameRow = (r: Run) => namesQ.data?.find((n) => n.id === r.strategy_id);
  const kindOf = (r: Run): RunKind => {
    if (r.status === "failed") return "bad";
    if (r.status === "running" || r.status === "pending") return "run";
    if (r.status === "cancelled") return "off";
    if (hasFigures(r)) return "good";
    const row = nameRow(r);
    return row != null && row.status !== "deleted" ? "warn" : "soft";
  };
  const infoOf = (r: Run): Judgeable => {
    const row = nameRow(r);
    return { kind: kindOf(r), cur: row != null && row.status !== "deleted", gone: row?.status === "deleted", why: r.status === "failed" ? plainWhy(r.error) : null };
  };

  const plotted = rows.filter(hasFigures);
  const failed = rows.filter((r) => r.status === "failed");
  const waiting = rows.filter((r) => kindOf(r) === "warn");
  const finished = rows.filter(isDone);
  const picked = rows.find((r) => r.id === pickedId) ?? plotted[0] ?? rows[0] ?? null;
  const notListed = ready && wanted != null && pickedId === wanted && !(all ?? []).some((r) => r.id === wanted);

  const reasons = new Map<string, number>();
  for (const r of failed) reasons.set(plainWhy(r.error), (reasons.get(plainWhy(r.error)) ?? 0) + 1);
  const topWhy = [...reasons.entries()].sort((a, b) => b[1] - a[1])[0];

  // Runs by week, Monday to Monday on the Paris clock, from the first run's week to the newest.
  const weeks: Week[] = [];
  if (rows.length) {
    const t0 = weekOf(Math.min(...rows.map((r) => r.created_at)));
    const t1 = weekOf(Math.max(nowMs / 1000, ...rows.map((r) => r.created_at))); // to the week the data was read, as the mockup does
    // Each Monday is found from the one before it by the clock, so a week with a clock change still starts at 00:00.
    for (let t = t0; t <= t1; t = weekOf(t + WEEK + WEEK / 14)) weeks.push({ start: t, label: dayLabel(t), full: dayYear(t), n: 0, by: {} });
    for (const r of rows) {
      const w = weeks.find((x) => x.start === weekOf(r.created_at));
      if (!w) continue;
      const k = kindOf(r);
      w.by[k] = (w.by[k] ?? 0) + 1;
      w.n += 1;
    }
  }
  const kindCount = (k: RunKind) => rows.filter((r) => kindOf(r) === k).length;

  const listed = filter === "figs" ? plotted : filter === "failed" ? failed : filter === "wait" ? waiting : rows;
  const shown = showAll ? listed : listed.slice(0, SHOWN);
  const onTableClick = (e: MouseEvent<HTMLDivElement>) => {
    const tr = (e.target as Element).closest("tbody tr");
    if (!tr || !tr.parentElement) return;
    const i = Array.from(tr.parentElement.children).indexOf(tr);
    if (i >= 0 && i < shown.length) pick(shown[i].id);
  };
  const maxLen = Math.max(1, ...rows.map((r) => seconds(r) ?? 0));
  const word = (r: Run, k: RunKind) =>
    r.status === "failed" ? "Failed" : r.verdict != null ? "Judged" : k === "warn" ? "Waits" : r.status === "running" ? "Running" : r.status === "pending" ? "Waiting" : r.status === "cancelled" ? "Cancelled" : "Done";
  const tableRows: Cell[][] = shown.map((r) => {
    const k = kindOf(r);
    const s = seconds(r);
    const figs = hasFigures(r);
    const lab = labelOf(r);
    const missing = r.status === "failed" ? "This run failed, so it has no figures" : r.is_sweep ? "A sweep across a universe: open it for each ticker's figures" : "No figures were saved for this run";
    const tip = `Run ${r.id}, ${dayYear(r.created_at)}\n${lab.text}\n${KIND_WORD[k]}${s != null ? `\nRan for ${lengthText(s)}` : ""}${r.status === "failed" ? `\nWhy: ${plainWhy(r.error)}` : ""}${r.verdict != null ? "\nGraded by the judge" : ""}`;
    const tone = signOf(r.total_return);
    return [
      <span key="i" className={`rs-id ${k}`} title={tip}>{r.id}</span>,
      <span key="n" data-run={r.id} title={lab.hover} className={`rn${picked && picked.id === r.id ? " sel" : ""}`}>{lab.text}</span>,
      dayYear(r.created_at),
      s == null ? dash("This run has no length yet") : (
        <span key="l" className="rs-len"><span className="rs-track"><i style={{ width: `${Math.max(4, (Math.log(s + 1) / Math.log(maxLen + 1)) * 100).toFixed(0)}%` }} /></span>{lengthText(s)}</span>
      ),
      figs && r.equity_sparkline && r.equity_sparkline.length >= 2 ? (
        <span className="rs-line">
          <Spark values={r.equity_sparkline} colour={(r.total_return ?? 0) < 0 ? RED : GREEN} height={22} dot={false} />
        </span>
      ) : (
        dash(missing)
      ),
      isDone(r) && r.total_return != null ? <span key="r" className={`rs-heat ${tone}`}>{pct(r.total_return)}</span> : dash(missing),
      figs && r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : dash(missing),
      figs && r.max_drawdown != null ? dropText(r.max_drawdown) : dash(missing),
      <span key="s" title={tip}><State tone={KIND_TONE[k]}>{word(r, k)}</State></span>,
    ];
  });

  let tableBody: ReactNode;
  if (failedList) tableBody = <p className="empty">{serverDetail(listQ.error)}</p>;
  else if (!ready) tableBody = <p className="empty">Loading the runs.</p>;
  else if (rows.length === 0) tableBody = <p className="empty">No runs in this range</p>;
  else if (listed.length === 0) tableBody = <p className="empty">No runs of this kind in this range</p>;
  else {
    tableBody = (
      <>
        <div data-testid="runs-table" className="rs-tbl" onClick={onTableClick}>
          <Table head={["Run", "Strategy", "Started", "Length", "Result", "Return", "Sharpe", "Deepest drop", "State"]} rows={tableRows} />
        </div>
        {!showAll && listed.length > SHOWN && (
          <button type="button" className="btn rs-more" onClick={() => setView((v) => ({ ...v, showAll: true }))}>{`Show all ${listed.length}`}</button>
        )}
      </>
    );
  }

  const summary = summaryQ.data;
  const flight = summary == null ? null : summary.in_flight_count > 0
    ? <State tone="run">{`${summary.in_flight_count} running`}</State>
    : <State tone="dim">Nothing running</State>;
  // The list holds the newest RUNS_LIMIT runs. Over all runs the summary counts every run, so the figures read from the
  // list say which runs they cover when the list is shorter than the log (or may be, when the summary could not be read).
  const total = range === "all" && summary ? summary.total : rows.length;
  const finishedN = range === "all" && summary ? summary.succeeded : finished.length;
  const partial = range === "all" && (summary ? summary.total > rows.length : rows.length >= RUNS_LIMIT);
  const among = partial ? ` among the newest ${rows.length}` : "";
  const shownAny = ready && !failedList && rows.length > 0;

  return (
    <div className="v2-page rs6">
      <PageHeader
        crumb={app.name}
        title={section.label}
        right={<div className="rs-head">{flight}<Segmented options={RANGES} value={range} onChange={(v) => setView((cur) => ({ ...cur, days: v, showAll: false }))} /></div>}
      />
      {shownAny && (
        <Say
          lead={`${finishedN} of ${total} runs finished. ${plotted.length} left figures to judge by${among}.`}
          more={<>
            {partial && `Among the newest ${rows.length}: `}
            {waiting.length > 0 && <>{`The ${waiting.length} newest are `}<em className="warn">first screens of your current strategies</em>{": their results live in the Generator's searches. "}</>}
            {failed.length > 0 && <><em className="bad">{`${failed.length} failed`}</em>{topWhy ? `, ${topWhy[1]} of them on "${topWhy[0]}".` : "."}</>}
          </>}
        />
      )}
      {notListed && <Block title="Run"><p className="empty">This run is not among the newest 500 runs.</p></Block>}
      {shownAny && (
        <Block
          title="Runs by week"
          state={<span className="rs-counts">{KIND_ORDER.filter((k) => kindCount(k) > 0).map((k) => <State key={k} tone={KIND_TONE[k]}>{`${kindCount(k)} ${KIND_COUNT_WORD[k]}`}</State>)}</span>}
          className="rs-first"
        >
          <WeekBars weeks={weeks} />
        </Block>
      )}
      {ready && picked && !notListed && (
        <>
          {plotted.length > 0 ? (
            <Block title="Runs with results">
              <RunScatter
                runs={plotted.map((r) => ({
                  id: r.id,
                  sharpe: r.sharpe_ratio as number,
                  drop: r.max_drawdown as number,
                  sign: signOf(r.total_return),
                  title: `Run ${r.id}, ${dayYear(r.created_at)}: ${labelOf(r).text}. Sharpe ${(r.sharpe_ratio as number).toFixed(2)}, deepest drop ${dropText(r.max_drawdown)}, return ${r.total_return == null ? "-" : pct(r.total_return)}, ${r.num_trades ?? "-"} trades`,
                }))}
                picked={picked.id}
                onPick={pick}
              />
              <div className="rs-key" data-testid="scatter-key"><span><i className="good" />Made money</span><span><i className="bad" />Lost money</span></div>
            </Block>
          ) : (
            <Block title="Runs with results"><p className="empty">No run in this range saved figures.</p></Block>
          )}
          <RunCard run={picked} name={labelOf(picked)} info={infoOf(picked)} nowMs={nowMs} peers={plotted} />
        </>
      )}
      <Block
        title={ready && !failedList ? <span title={`${rows.length} in this range`}>All runs</span> : "All runs"}
        right={
          <Segmented
            options={[
              { label: `All ${rows.length}`, value: "all" },
              { label: `With figures ${plotted.length}`, value: "figs" },
              { label: `Waiting ${waiting.length}`, value: "wait" },
              { label: `Failed ${failed.length}`, value: "failed" },
            ]}
            value={filter}
            onChange={(v) => setView((cur) => ({ ...cur, filter: v, showAll: false }))}
          />
        }
        className="rs-all"
      >
        {tableBody}
      </Block>
    </div>
  );
}
