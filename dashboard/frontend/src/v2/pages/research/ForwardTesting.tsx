import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Block, PageHeader, Say, Segmented, State, usePageMemory } from "../../kit";
import type { NlTone } from "../../kit";
import { Window } from "../../kit/Window";
import type { PageProps } from "../../shell/sections";
import { fetchJudgeSummary } from "../../data/judgeApi";
import { getHistory, getPaperLimits, getRetired, getRunningRuns, getSummary } from "../../data/paperApi";
import type { PaperLimit, PaperRun, PaperSlice, SummaryWindow } from "../../data/paperTypes";
import { getSystems } from "../../data/opsApi";
import { serverDetail } from "../../../lib/errorText";
import { strategyLabel, useStrategyNames } from "../../data/strategyNames";
import { fetchPageVerdicts, judgeKeys, useSeen } from "./Judge";
import { Share } from "./Overview";
import { WORD } from "./Operations";
import "./forward.css";

// Forward testing as the frozen mockup draws it (night of 2026-11-15, page "forward"): paper trading seen from Research.
// What is on paper and how does it do; if nothing, what still stands between a strategy and paper. Every figure is read from
// the dashboard's own GET routes; the limits are set on Trading, Paper. The Example view shows made-up strategies, said so.

const PAPER = "/app/trading/paper";
const BROKER = "Paper broker";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WINDOWS: Array<{ value: SummaryWindow; label: string; hover: string }> = [
  { value: "today", label: "Today", hover: "Since 00:00 UTC today, the paper store's clock" },
  { value: "this_week", label: "This week", hover: "Since Monday 00:00 UTC, the paper store's clock" },
  { value: "since_start", label: "Since the start", hover: "Since each strategy went on paper" },
];
// The six limits in plain words; the server's own wording is on hover.
const PLAIN: Record<string, string> = {
  "paper.per_order_notional_cap": "The most one order may be worth",
  "paper.ticker_net_cap": "The most all strategies together may hold in one stock",
  "paper.total_net_cap": "The most all strategies together may hold in total",
  "paper.loss_limit_window": "The most the account may lose in one day, New York time",
  "paper.loss_limit_since_go": "The most the account may lose since the limits were last saved",
  "paper.order_rate_cap": "How many orders may be sent in one minute",
};
const UNIT: Record<string, string> = { dollars: "Dollars", "orders per 60 seconds": "Orders a minute" };

const vars = (v: Record<string, number>) => v as CSSProperties;
const usd = (n: number) => `$${Math.round(n).toLocaleString("en-US")}`;
const upper = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
const pct = (f: number, dp = 1) => `${f >= 0 ? "+" : "-"}${Math.abs(f * 100).toFixed(dp)}%`;
const range = (f: number) => `${(f * 100).toFixed(0)}%`;
const dayY = (iso: string) => {
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}` : "-";
};
const dur = (d: number) => (d < 1 ? "Under a day" : d === 1 ? "1 day" : `${d} days`);
const sign = (f: number | null) => (f == null ? "" : f >= 0 ? "good" : "bad");

/** A running strategy as the tiles read it: real from the paper route, or made up for the Example view. */
type Tile = {
  id: number; name: string; started_at: string; opening: number; bounds: { low: number; high: number } | null;
  series: Array<[string, number]>; slice: Pick<PaperSlice, "equity" | "cash" | "position_value">; wins: number; losses: number; example: boolean;
};

// Made-up strategies for the Example view: a fixed recipe, so they never change, and every one says it is made up.
function examples(now: number): Tile[] {
  const mk = (id: number, name: string, days: number, drift: number, low: number, high: number, wins: number, losses: number, seed: number): Tile => {
    let x = seed, eq = 10000;
    const rnd = () => { x = (x * 1103515245 + 12345) % 2147483648; return x / 2147483648 - 0.5; };
    const pts: Array<[string, number]> = [];
    for (let d = days; d >= 0; d--) { pts.push([new Date(now - d * 864e5).toISOString(), Math.round(eq * 100) / 100]); eq *= 1 + drift + rnd() * 0.018; }
    const last = pts[pts.length - 1][1], held = Math.round(last * 0.62);
    return { id, name, started_at: pts[0][0], opening: 10000, bounds: { low, high }, series: pts, slice: { equity: last, cash: last - held, position_value: held }, wins, losses, example: true };
  };
  return [mk(-1, "Example strategy A", 41, 0.0016, 0.01, 0.09, 14, 9, 7), mk(-2, "Example strategy B", 23, -0.0012, 0, 0.05, 5, 8, 21), mk(-3, "Example strategy C", 9, 0.0009, -0.01, 0.03, 2, 1, 5)];
}

function realTiles(runs: PaperRun[], name: (id: number) => string): Tile[] {
  return runs.map((r) => ({
    id: r.paper_run_id, name: name(r.strategy_id), started_at: r.started_at, opening: r.slice_opening_cash,
    bounds: r.expected_bounds ? { low: r.expected_bounds.low, high: r.expected_bounds.high } : null,
    series: r.equity_series ?? [], slice: r.slice ?? { equity: r.slice_opening_cash, cash: r.slice_opening_cash, position_value: 0 },
    wins: r.realised_result_counts?.win_count ?? 0, losses: r.realised_result_counts?.loss_count ?? 0, example: false,
  }));
}

function read(t: Tile, now: number) {
  const ret = t.opening ? t.slice.equity / t.opening - 1 : null;
  const days = Math.max(0, Math.round((now - Date.parse(t.started_at)) / 864e5));
  const b = t.bounds;
  const where: [NlTone, string] = ret == null || !b ? ["dim", "No expected range"] : ret < b.low ? ["bad", "Below expected"] : ret > b.high ? ["good", "Above expected"] : ["good", "As expected"];
  return { ret, days, b, where };
}

/** One account line, day by day; green above what it started with, red below. */
function Line({ t, big = false }: { t: Tile; big?: boolean }) {
  const pts = t.series.map(([ts, v]) => [Date.parse(ts), v] as const).filter(([ts]) => Number.isFinite(ts));
  if (pts.length < 2) return <p className="fw7-none">It has no account line yet.</p>;
  const W = 800, H = big ? 240 : 70;
  const bl = t.bounds ? [t.opening * (1 + t.bounds.low), t.opening * (1 + t.bounds.high)] : [];
  const vs = [...pts.map(([, v]) => v), t.opening, ...bl];
  const lo = Math.min(...vs), hi = Math.max(...vs), span = hi - lo || 1;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
  const x = (ts: number) => (t1 === t0 ? 0 : ((ts - t0) / (t1 - t0)) * W);
  const y = (v: number) => 6 + (1 - (v - lo) / span) * (H - 12);
  const last = pts[pts.length - 1][1];
  const guides: Array<[number, string]> = [[t.opening, "Started with"], ...(t.bounds ? ([[bl[0], "Expected low"], [bl[1], "Expected high"]] as Array<[number, string]>) : [])];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className={`fw7-line ${last >= t.opening ? "up" : "down"}${big ? " big" : ""}`} aria-hidden="true">
      {big && guides.map(([v, label]) => <line key={label} className="g" x1={0} x2={W} y1={y(v)} y2={y(v)}><title>{`${label}: ${usd(v)}`}</title></line>)}
      <polyline points={pts.map(([ts, v]) => `${x(ts)},${y(v)}`).join(" ")} />
    </svg>
  );
}

function RunWindow({ t, now, onClose, onPaper }: { t: Tile; now: number; onClose: () => void; onPaper: () => void }) {
  const q = read(t, now);
  const facts: Array<[string, string, string?]> = [
    ["Return since it started", q.ret == null ? "-" : pct(q.ret, 2), sign(q.ret)],
    ["Expected by its backtest", q.b ? `${range(q.b.low)} to ${range(q.b.high)}` : "-"],
    ["Worth now", usd(t.slice.equity)], ["Started with", usd(t.opening)], ["In cash", usd(t.slice.cash)], ["In stocks", usd(t.slice.position_value)],
    ["Trades won", `${t.wins} of ${t.wins + t.losses}`], ["Running for", dur(q.days)],
  ];
  return (
    <Window label={t.name} size="m" className="fw7-win" onClose={onClose}>
      <div className="h2"><span>{t.name}</span></div>
      <p className="fw7-lead"><State tone={q.where[0]}>{q.where[1]}</State>{t.example && <State tone="warn">Example data, made up</State>}<span>{`Running for ${dur(q.days).toLowerCase()}, since ${dayY(t.started_at)}`}</span></p>
      <div className="fw7-facts">{facts.map(([k, v, c]) => <div key={k}><span>{k}</span><b className={`num ${c ?? ""}`}>{v}</b></div>)}</div>
      <h3 className="fw7-h3">Its account, day by day</h3>
      <Line t={t} big />
      <div className="fw7-foot">
        <button type="button" className="btn q" onClick={onClose}>Close</button>
        {!t.example && <button type="button" className="btn" onClick={onPaper}>Open Paper</button>}
      </div>
    </Window>
  );
}

type Step = { k: string; name: string; fig: number; of: string; tone: NlTone; word: string; bar: Array<["good" | "bad" | "warn", number]>; what: string; tip: string; go?: () => void };

/** Five steps on one line, lit as far as everything works. */
function Steps({ steps }: { steps: Step[] }) {
  const [ref, seen] = useSeen<HTMLDivElement>();
  const lit = Math.max(0, steps.findIndex((s) => s.tone !== "good"));
  const at = steps.every((s) => s.tone === "good") ? steps.length - 1 : lit;
  return (
    <div className={seen ? "fw7-steps in" : "fw7-steps"} ref={ref} data-testid="road">
      <div className="track" aria-hidden="true"><i style={{ scale: `${at / (steps.length - 1)} 1` }} /></div>
      {steps.map((s, i) => (
        <button type="button" key={s.k} data-stop={s.k} className={`step t-${s.tone}${s.go ? "" : " still"}${i < at ? " done" : ""}`} style={vars({ "--i": i })}
          title={`${s.name}\n${s.tip}`} onClick={s.go} aria-disabled={!s.go}>
          <span className="node" />
          <span className="nm">{s.name}</span>
          <span className="fig"><b className="num">{s.fig}</b><small>{s.of}</small></span>
          <Share parts={s.bar} />
          <span className="what">{s.what}</span>
          <State tone={s.tone}>{s.word}</State>
        </button>
      ))}
    </div>
  );
}

const Note = ({ children, extra }: { children: ReactNode; extra?: string }) => <p className={extra ? `fw7-note ${extra}` : "fw7-note"}>{children}</p>;

function Limits({ limits }: { limits: PaperLimit[] }) {
  return (
    <div className="fw7-lims" data-testid="limits">
      <div className="hd"><span>What it limits</span><span>Counted in</span><span className="r">Value</span><span>State</span></div>
      {limits.map((l) => (
        <div key={l.key} className="lim" title={`In the server's words\n${upper(l.meaning)}${l.value == null ? "\nUntil it has a value, no paper order is sent" : ""}`}>
          <span>{PLAIN[l.key] ?? upper(l.meaning)}</span>
          <span>{UNIT[l.unit] ?? upper(l.unit)}</span>
          <span className={l.value == null ? "r num warn" : "r num"}>{l.value == null ? "Not set" : l.value.toLocaleString("en-US")}</span>
          <State tone={l.value == null ? "warn" : "good"}>{l.value == null ? "Waits for you" : "Set"}</State>
        </div>
      ))}
    </div>
  );
}

export default function ForwardTesting({ app, section }: PageProps) {
  const navigate = useNavigate();
  const [view, setView] = usePageMemory<{ win: SummaryWindow; mode: "real" | "example" }>("research/forward-testing", { win: "since_start", mode: "real" });
  const [open, setOpen] = useState<Tile | null>(null);
  const verdictsQ = useQuery({ queryKey: judgeKeys.verdicts, queryFn: fetchPageVerdicts });
  const judgeQ = useQuery({ queryKey: ["judge", "summary"], queryFn: fetchJudgeSummary });
  const limitsQ = useQuery({ queryKey: ["paper", "limits"], queryFn: getPaperLimits });
  const runsQ = useQuery({ queryKey: ["paper", "runs"], queryFn: getRunningRuns });
  const historyQ = useQuery({ queryKey: ["paper", "history"], queryFn: getHistory });
  const retiredQ = useQuery({ queryKey: ["paper", "retired"], queryFn: getRetired });
  const sumQ = useQuery({ queryKey: ["paper", "summary", view.win], queryFn: () => getSummary(view.win) });
  const systemsQ = useQuery({ queryKey: ["ops", "systems"], queryFn: getSystems });
  const namesQ = useStrategyNames();
  const nameOf = (id: number) => strategyLabel(namesQ, id).text;
  const now = Date.now();

  const failed = [verdictsQ, judgeQ, limitsQ, runsQ].find((q) => q.isError);
  const setLimits = () => navigate(PAPER);
  let body: ReactNode;
  if (failed) body = <Block><p className="empty">{serverDetail(failed.error)}</p></Block>;
  else if (!verdictsQ.data || !judgeQ.data || !limitsQ.data || !runsQ.data) body = <Block><p className="empty">Loading the road to paper.</p></Block>;
  else {
    const graded = verdictsQ.data.rows.filter((r) => r.current && !r.rejected);
    const passed = graded.filter((r) => r.verdict === "pass").length;
    const awaiting = judgeQ.data.awaiting_ok;
    const limits = limitsQ.data.limits;
    const set = limits.filter((l) => l.value != null).length;
    const runs = runsQ.data;
    const broker = systemsQ.data?.systems.find((s) => s.name === BROKER);
    const brokerOn = broker?.word === "WORKING";
    // The broker's word from the systems read; while it loads, if it fails, or if it lists no paper broker, the step says which.
    const [bTone, bWord] = broker ? WORD[broker.word] : ["dim" as NlTone, systemsQ.isError ? "Could not read" : systemsQ.data ? "Not watched" : "Reading"];
    const bWhy = broker ? (broker.reason ?? bWord) : systemsQ.isError ? serverDetail(systemsQ.error) : systemsQ.data ? "The systems read lists no paper broker" : "Reading the systems";
    const toJudge = () => navigate("/app/research/judge");
    const steps: Step[] = [
      { k: "judge", name: "Passes the judge", fig: passed, of: `of ${graded.length} judged runs`, tone: passed ? "good" : graded.length ? "bad" : "dim", word: passed ? "Passing" : graded.length ? "Stops here" : "Nothing graded",
        bar: [["good", passed], ["bad", graded.length - passed]], what: "A run must pass all nine checks",
        tip: passed ? `${passed} judged runs passed all nine checks` : "No judged run passed all nine checks", go: toJudge },
      { k: "ok", name: "You say yes", fig: awaiting, of: "waiting for you", tone: awaiting ? "warn" : "dim", word: awaiting ? "Waits for you" : "Nothing to approve",
        bar: [["warn", awaiting]], what: "A run that passes waits for your yes", tip: "A run that passes waits here until you approve it for paper", go: awaiting ? toJudge : undefined },
      { k: "limits", name: "Safety limits are set", fig: set, of: `of ${limits.length} set`, tone: set === limits.length ? "good" : "warn", word: set === limits.length ? "Set" : "Waits for you",
        bar: [["good", set], ["warn", limits.length - set]], what: "The most it may buy, hold or lose", tip: "No order is sent until every limit has a value", go: setLimits },
      { k: "broker", name: "Broker is connected", fig: brokerOn ? 1 : 0, of: `of 1 connected${broker?.where ? `, ${broker.where}` : ""}`, tone: bTone, word: bWord,
        bar: [["good", brokerOn ? 1 : 0]], what: "The broker is the service that takes the orders", tip: bWhy, go: () => navigate("/app/research/operations") },
      { k: "paper", name: "It trades on paper", fig: runs.length, of: runs.length === 1 ? "strategy running" : "strategies running", tone: runs.length ? "good" : "dim", word: runs.length ? "Running" : "None yet",
        bar: [["good", runs.length]], what: "Real prices, pretend money", tip: "Strategies trading on the paper account right now", go: () => navigate(PAPER) },
    ];
    const lead = runs.length ? `${runs.length} ${runs.length === 1 ? "strategy is" : "strategies are"} on paper.` : "Nothing is on paper yet.";
    const more = runs.length ? "Paper means real prices and pretend money." : (
      <>Paper means real prices and pretend money. A strategy gets there after it passes the judge and you say yes. <em className={passed ? "good" : "bad"}>{passed} of {graded.length} judged runs passed</em>
        {set < limits.length && <>, <em className="warn">{limits.length - set} of {limits.length} safety limits have no value</em></>}
        {broker && !brokerOn && <>, and the broker is {bWord.toLowerCase()}</>}.</>
    );
    const ex = view.mode === "example";
    const tiles = ex ? examples(now) : realTiles(runs, nameOf);
    const sum = sumQ.data;
    const winLabel = WINDOWS.find((w) => w.value === view.win)?.label ?? "";
    const figs: Array<[string, string, string, string?]> = sum ? [
      ["Worth now", sum.running_count ? usd(sum.total_equity) : "-", sum.running_count ? "All running strategies added up" : "No strategy is on paper"],
      ["Gain or loss", sum.delta_fraction == null ? "-" : pct(sum.delta_fraction, 2), sum.delta_fraction == null ? "Nothing to compare with yet" : view.win === "since_start" ? "Against what each strategy started with" : "Against the start of this window", sign(sum.delta_fraction)],
      ["Trades", String(sum.trades_in_window), "Orders carried out in this window"],
      ["Fees paid", usd(sum.fees_paid_total), "Since paper began"],
      ["Started with", usd(sum.total_opening_cash), "What the running strategies were given"],
    ] : [];
    const hist = historyQ.data;
    body = (
      <>
        <Say lead={lead} more={more} />
        <div className="fw-stack">
          <Block title="Running strategies"
            state={<>
              <State tone={tiles.length ? "good" : "dim"}>{tiles.length ? `${tiles.length} running` : "None running"}</State>
              {ex && <State tone="warn">Example data, made up</State>}
            </>}
            right={<Segmented variant="seg" value={view.mode} onChange={(v) => setView((p) => ({ ...p, mode: v === "example" ? "example" : "real" }))}
              options={[{ label: "Real", value: "real", hover: "What is on paper now" }, { label: "Example", value: "example", hover: "Made-up strategies, to show the layout" }]} />}>
            {tiles.length === 0 ? (
              <Note>No strategy is on paper yet. When one is, it gets a tile here with its return since it started, its account line, how long it has run, and whether it is inside the range its backtest expected. Press Example to see how that will look.</Note>
            ) : (
              <>
                <div className="fw7-wall" data-testid="tiles">
                  {tiles.map((t) => {
                    const q = read(t, now);
                    return (
                      <button key={t.id} type="button" className="fw7-tile" onClick={() => setOpen(t)}
                        title={`${t.name}\nStarted ${dayY(t.started_at)}\nStarted with ${usd(t.opening)}\nWorth now ${usd(t.slice.equity)}${q.b ? `\nExpected by its backtest ${range(q.b.low)} to ${range(q.b.high)}` : ""}\nTrades won ${t.wins}, lost ${t.losses}\nPress for its account line and what it holds`}>
                        <span className="top"><span className="nm">{t.name}</span><State tone={q.where[0]}>{q.where[1]}</State></span>
                        <span className="figrow"><b className={`num ${sign(q.ret)}`}>{q.ret == null ? "-" : pct(q.ret)}</b><small>since start</small></span>
                        <Line t={t} />
                        <span className="chg">
                          <span><i>Running for</i><b className="num">{dur(q.days)}</b></span>
                          <span><i>Account</i><b className="num">{usd(t.slice.equity)}</b></span>
                          <span><i>Expected</i><b className="num">{q.b ? `${range(q.b.low)} to ${range(q.b.high)}` : "-"}</b></span>
                          <span><i>Trades won</i><b className="num">{`${t.wins} of ${t.wins + t.losses}`}</b></span>
                        </span>
                      </button>
                    );
                  })}
                </div>
                {ex && <Note>These three strategies are made up to show the layout. Their names, lines and figures are not real.</Note>}
              </>
            )}
          </Block>
          <Block title="Steps to paper"><Steps steps={steps} /></Block>
          <Block title="Running strategies, total"
            right={<Segmented variant="seg" value={view.win} onChange={(v) => setView((p) => ({ ...p, win: WINDOWS.find((w) => w.value === v)?.value ?? "since_start" }))}
              options={WINDOWS.map((w) => ({ label: w.label, value: w.value, hover: w.hover }))} />}>
            {sumQ.isError ? <p className="empty">{serverDetail(sumQ.error)}</p> : !sum ? <p className="empty">Loading the account.</p> : (
              <div className="fw7-figs" data-testid="figs" aria-label={winLabel}>
                {figs.map(([k, v, h, c], i) => (
                  <span key={k} className="f" style={vars({ "--k": i })}>
                    <small>{k}</small><b className={`num ${c ?? ""}${v === "-" ? " dim" : ""}`}>{v}</b><em>{h}</em>
                  </span>
                ))}
              </div>
            )}
          </Block>
          <Block title="Past strategies"
            state={historyQ.data ? <State tone="dim" hover="Paper runs that ended, killed or promoted">{`${historyQ.data.length} finished`}</State> : undefined}>
            {historyQ.isError ? <p className="empty">{serverDetail(historyQ.error)}</p> : !hist ? <p className="empty">Loading the past strategies.</p> : hist.length === 0 ? (
              <Note extra="fw7-past-none">No strategy has left paper yet. One that stops or moves on to real money is listed here with its result.</Note>
            ) : (
              <div className="fw7-past" data-testid="past">
                <div className="hd"><span>Strategy</span><span>Ran</span><span className="r">Return</span><span className="r">Trades won</span><span>How it ended</span></div>
                {[...hist].sort((a, b) => Date.parse(b.ended_at ?? "") - Date.parse(a.ended_at ?? "")).map((r) => (
                  <div key={r.paper_run_id} className="row">
                    <span>{nameOf(r.strategy_id)}</span>
                    <span className="num">{`${dayY(r.started_at)} to ${r.ended_at ? dayY(r.ended_at) : "-"}`}</span>
                    <span className={`r num ${sign(r.return_fraction ?? null)}`}>{r.return_fraction == null ? "-" : pct(r.return_fraction, 2)}</span>
                    <span className="r num">{`${r.win_count ?? 0} of ${r.trade_count ?? 0}`}</span>
                    <State tone={r.retired_in_paper ? "bad" : "dim"}>{r.retired_in_paper ? "Stopped by you" : upper(r.end_reason ?? "ended")}</State>
                  </div>
                ))}
              </div>
            )}
            {retiredQ.data && retiredQ.data.length > 0 && <p className="fw7-more">{`${retiredQ.data.length} ${retiredQ.data.length === 1 ? "strategy is" : "strategies are"} retired in paper.`}</p>}
          </Block>
          <Block title="Safety limits" id="fw-limits" state={<State tone={set === limits.length ? "good" : "warn"}>{`${set} of ${limits.length} set`}</State>}
            right={<button type="button" className="btn" onClick={setLimits}>Set the limits</button>}>
            <Note>{`Each limit is a ceiling the server enforces on paper orders. Until all ${limits.length} have a value, no order is sent.`}</Note>
            <Limits limits={limits} />
          </Block>
        </div>
        {open && <RunWindow t={open} now={now} onClose={() => setOpen(null)} onPaper={() => navigate(PAPER)} />}
      </>
    );
  }

  return (
    <div className="v2-page fw-page">
      <PageHeader crumb={app.name} title={section.label} />
      {body}
    </div>
  );
}
