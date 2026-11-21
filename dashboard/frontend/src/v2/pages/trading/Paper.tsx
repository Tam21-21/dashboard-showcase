import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Block, LineChart, PageHeader, Say, State, Window, usePageMemory } from "../../kit";
import type { NlTone } from "../../kit";
import { HoldButton, PhoneOff, useLayout } from "../../kit/phone";
import type { PageProps } from "../../shell/sections";
import { approveForRealMoney, getHoldings, getPaperLimits, getRunFills, getRunningRuns, getSummary, killRun, promoteRun, putPaperLimits } from "../../data/paperApi";
import type { HoldingsResponse, PaperFill, PaperLimit, PaperRun } from "../../data/paperTypes";
import { getSystems } from "../../data/opsApi";
import { ApiError } from "../../../lib/api";
import { fmtDate, fmtPctSigned, fmtTime, fmtUsd } from "../../../lib/format";
import { strategyLabel, useStrategyNames } from "../../data/strategyNames";
import { Card, LinesPic, NeedsBlock, PlainFacts, Pic, RowsPic, Share, Wall } from "./parts";
import type { SharePart, Stop } from "./parts";
import "./paper.css";

// This page reads the paper routes. Its writes are the Limits block's Edit dialog (PUT /api/paper/limits) and,
// under the picked strategy, Promote (records a request only), Approve for real money and Stop (the paper kill:
// sells that strategy's own shares and ends its run). Each sits behind its confirm dialog, the phone's Stop
// behind a hold. Nothing here sends an order.

const TOO_FEW = "Too few points to draw yet.";
const NO_START = "No starting value yet";
const NO_FIGURES = "No account figures for this strategy yet";
const NO_COUNTS = "No closed trades counted yet";
const DAY = 864e5;

const dash = (why: string): ReactNode => <span title={why}>-</span>;
const usd = (n: number) => `$${Math.round(n).toLocaleString("en-US")}`;
const usdSigned = (n: number) => `${n > 0 ? "+" : n < 0 ? "-" : ""}$${Math.abs(Math.round(n)).toLocaleString("en-US")}`;
const toneOf = (n: number | null | undefined): "good" | "bad" | "" => (n == null || n === 0 ? "" : n > 0 ? "good" : "bad");

function returnOf(run: PaperRun): number | null {
  const eq = run.slice?.equity;
  if (eq == null || !(run.slice_opening_cash > 0)) return null;
  return eq / run.slice_opening_cash - 1;
}

function counts(run: PaperRun): { wins: number; losses: number } | null {
  const c = run.realised_result_counts;
  return c ? { wins: c.win_count, losses: c.loss_count } : null;
}

const daysOn = (run: PaperRun): number => Math.max(0, Math.round((Date.now() - Date.parse(run.started_at)) / DAY));
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const PROMO: Record<string, [NlTone, string]> = {
  none: ["run", "On paper"], requested: ["warn", "Promotion asked"], granted: ["good", "Promoted"],
};
const promoOf = (run: PaperRun): [NlTone, string] => PROMO[run.latest_promotion?.state === "requested" || run.latest_promotion?.state === "granted" ? run.latest_promotion.state : "none"];

const LIMITS: Array<{ key: string; name: string; whole?: boolean }> = [
  { key: "paper.per_order_notional_cap", name: "Largest order" },
  { key: "paper.ticker_net_cap", name: "Largest position in one stock" },
  { key: "paper.total_net_cap", name: "Total invested" },
  { key: "paper.loss_limit_window", name: "Largest loss in a day" },
  { key: "paper.loss_limit_since_go", name: "Largest loss since start" },
  { key: "paper.order_rate_cap", name: "Orders per minute", whole: true },
];

const valueOf = (limits: PaperLimit[], key: string): number | null =>
  limits.find((l) => l.key === key)?.value ?? null;

export default function Paper({ app, section }: PageProps) {
  const navigate = useNavigate();
  const runsQ = useQuery({ queryKey: ["paper", "runs"], queryFn: getRunningRuns });
  const sinceQ = useQuery({ queryKey: ["paper", "summary", "since_start"], queryFn: () => getSummary("since_start") });
  const weekQ = useQuery({ queryKey: ["paper", "summary", "this_week"], queryFn: () => getSummary("this_week") });
  const limitsQ = useQuery({ queryKey: ["paper", "limits"], queryFn: getPaperLimits });
  const systemsQ = useQuery({ queryKey: ["ops", "systems"], queryFn: getSystems });
  const namesQ = useStrategyNames();
  const [pickedId, setPickedId] = usePageMemory<number | null>("trading/paper", null);
  const [editing, setEditing] = useState(false);

  const header = (
    <PageHeader crumb={app.name} title={section.label}
      right={<>
        <span title="Practice money, no real orders"><State tone="run">Paper money</State></span>
      </>} />
  );
  if (runsQ.isError) return <div className="v2-page pp-page">{header}<Block><p className="empty">Could not load the strategies on paper.</p></Block></div>;
  if (!runsQ.data) return <div className="v2-page pp-page">{header}<Block><p className="empty">Loading the strategies on paper.</p></Block></div>;

  const runs = runsQ.data;
  const since = sinceQ.data;
  const week = weekQ.data;
  const limits = limitsQ.data?.limits ?? null;
  const unset = limits ? LIMITS.filter((l) => valueOf(limits, l.key) === null).length : null;
  const broker = systemsQ.data?.systems.find((s) => s.name === "Paper broker");
  const brokerOn = broker?.word === "WORKING";
  const picked = runs.find((r) => r.paper_run_id === pickedId) ?? runs[0];
  const asked = runs.filter((r) => r.latest_promotion?.state === "requested");
  const nameOf = (r: PaperRun) => strategyLabel(namesQ, r.strategy_id);
  const pick = (r: PaperRun) => {
    setPickedId(r.paper_run_id);
    document.getElementById("pp-run")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  };

  // Needs you: what waits for Tamer, and what is not connected.
  const stops: Stop[] = [];
  if (limits && unset) {
    stops.push({ key: "limits", tone: "warn", word: "Waits for you", title: `Paper orders are blocked: ${unset} of the ${LIMITS.length} limits ${unset === 1 ? "is" : "are"} not set`,
      detail: "The server sends no paper order until every limit has a value",
      bar: <Share parts={[["good", LIMITS.length - unset], ["warn", unset]]} />, cap: `${LIMITS.length - unset} of ${LIMITS.length} set`, btn: "Edit limits", act: () => setEditing(true) });
  }
  asked.forEach((r) => {
    const c = counts(r);
    stops.push({ key: `ask${r.paper_run_id}`, tone: "warn", word: "Waits for you", title: `${nameOf(r).text} asks to move toward real money`,
      detail: "Approving closes its paper positions and ends the paper run as approved. It sends no real order",
      bar: c ? <Share parts={[["good", c.wins], ["bad", c.losses]]} /> : undefined, cap: c ? `${c.wins} won, ${c.losses} lost on paper` : undefined, btn: "Review", act: () => pick(r) });
  });
  if (systemsQ.data && !brokerOn) {
    stops.push({ key: "broker", tone: "dim", word: "Not connected", title: "The paper broker (Alpaca) is not connected",
      detail: "Until it is, no paper share is actually bought or sold", btn: "Open Operations", act: () => navigate("/app/research/operations") });
  }

  const lead = runs.length === 0
    ? "No strategy is on paper right now."
    : (() => {
        const d = since?.delta_fraction;
        return `${plural(runs.length, "strategy runs", "strategies run")} on paper${d == null ? "." : `, ${d >= 0 ? "up" : "down"} ${Math.abs(d * 100).toFixed(2)}% since ${runs.length === 1 ? "it" : "they"} started.`}`;
      })();
  const more = runs.length === 0 ? (
    <>
      {unset ? <>Paper orders are blocked: <em className="warn">{unset} of the {LIMITS.length} limits {unset === 1 ? "is" : "are"} not set</em>. </> : null}
      {systemsQ.data && !brokerOn ? "The paper broker is not connected. " : null}
    </>
  ) : (
    <>
      The account is worth <em className="data">{since ? usd(since.total_equity) : "-"}</em>, started at {since ? usd(since.total_opening_cash) : "-"}.
      {week ? <> {week.trades_in_window} {week.trades_in_window === 1 ? "order was" : "orders were"} filled this week.</> : null}
      {asked.length ? <> <em className="warn">{asked.length} {asked.length === 1 ? "asks" : "ask"} to move toward real money.</em></> : null}
    </>
  );

  return (
    <div className="v2-page pp-page">
      {header}
      <Say lead={lead} more={more} />
      <div className="tp-stack">
        <NeedsBlock stops={stops} empty="Nothing waits for you on paper." />
        {runs.length === 0 ? (
          <Block title="Strategies on paper" state={<State tone="dim">None</State>}>
            <p className="pp-note">A strategy gets here after the judge passes it, the retest agrees and you approve it. Forward testing shows which of those steps is still open.</p>
            <div className="tp-acts"><button type="button" className="btn" onClick={() => navigate("/app/research/forward-testing")}>Open Forward testing</button></div>
          </Block>
        ) : (
          <>
            <Block title="Summary">
              <Summary runs={runs} nameOf={(r) => nameOf(r).text} since={since} week={week} sinceErr={sinceQ.isError} weekErr={weekQ.isError} />
            </Block>
            <Block title="Strategies on paper" state={<State tone="run">{String(runs.length)}</State>}>
              <Wall n={runs.length >= 3 ? 3 : 2}>
                {runs.map((r) => {
                  const name = nameOf(r);
                  const ret = returnOf(r);
                  const c = counts(r);
                  const values = (r.equity_series ?? []).map((p) => p[1]);
                  return (
                    <Card key={r.paper_run_id} name={name.text} hover={name.hover} hue={toneOf(ret) || "run"} chip={promoOf(r)} pressed={r === picked}
                      onClick={() => pick(r)}
                      fig={ret === null ? dash(NO_FIGURES) : fmtPctSigned(ret, 2)}
                      sub={`since it started, ${plural(daysOn(r), "day", "days")} ago`}
                      pic={<Pic values={values} tone={ret !== null && ret < 0 ? "bad" : "good"} none="No line yet" />}
                      facts={[["Trades", c ? String(c.wins + c.losses) : dash(NO_COUNTS)], ["Won", c ? String(c.wins) : dash(NO_COUNTS), c && c.wins > 0 ? "good" : ""], ["Lost", c ? String(c.losses) : dash(NO_COUNTS), c && c.losses > 0 ? "bad" : ""]]} />
                  );
                })}
              </Wall>
            </Block>
            {picked && <RunSection run={picked} name={nameOf(picked)} />}
            <div className="pp-cols">
              {picked && <HoldingsBlock run={picked} name={nameOf(picked)} />}
              <LimitsBlock limitsQ={limitsQ} editing={editing} setEditing={setEditing} wide={false} />
            </div>
            {picked && <OrdersBlock run={picked} />}
          </>
        )}
        {runs.length === 0 && <LimitsBlock limitsQ={limitsQ} editing={editing} setEditing={setEditing} wide />}
      </div>
    </div>
  );
}

function Summary({ runs, nameOf, since, week, sinceErr, weekErr }: {
  runs: PaperRun[]; nameOf: (r: PaperRun) => string; since: Awaited<ReturnType<typeof getSummary>> | undefined; week: Awaited<ReturnType<typeof getSummary>> | undefined; sinceErr: boolean; weekErr: boolean;
}) {
  const sinceWhy = sinceErr ? "The summary could not be loaded" : "Loading";
  const weekWhy = weekErr ? "The summary could not be loaded" : "Loading";
  // The account's result in dollars day by day: each strategy's worth less what it started with, added up from the newest end.
  const span = Math.max(0, ...runs.map((r) => (r.equity_series ?? []).length));
  const result = Array.from({ length: span }, (_, i) =>
    runs.reduce((t, r) => {
      const s = r.equity_series ?? [];
      const k = i - (span - s.length);
      return t + (k >= 0 ? s[k][1] - r.slice_opening_cash : 0);
    }, 0));
  const rets = runs.map((r) => ({ r, ret: returnOf(r) })).filter((x): x is { r: PaperRun; ret: number } => x.ret !== null);
  const best = [...rets].sort((a, b) => b.ret - a.ret)[0];
  const worst = [...rets].sort((a, b) => a.ret - b.ret)[0];
  const d = since?.delta_fraction ?? null;
  const asked = runs.filter((r) => r.latest_promotion?.state === "requested").length;
  const granted = runs.filter((r) => r.latest_promotion?.state === "granted").length;
  const longest = Math.max(0, ...runs.map(daysOn));
  const upN = rets.filter((x) => x.ret > 0).length;
  const downN = rets.filter((x) => x.ret < 0).length;
  return (
    <Wall>
      <Card name="Account value" hue={d === null ? "" : d >= 0 ? "good" : "bad"} chip={d === null ? undefined : d >= 0 ? ["good", "Up"] : ["bad", "Down"]}
        fig={since ? usd(since.total_equity) : dash(sinceWhy)} sub={since ? `in paper money, started at ${usd(since.total_opening_cash)}` : undefined}
        pic={<Pic values={result} tone={d !== null && d < 0 ? "bad" : "good"} none="No line yet" />}
        facts={since ? [["Gained", usdSigned(since.total_equity - since.total_opening_cash), toneOf(since.total_equity - since.total_opening_cash)], ["In percent", d === null ? "-" : fmtPctSigned(d, 2), toneOf(d)]] : undefined} />
      <Card name="Return" hue={toneOf(d) || ""} fig={d === null ? dash(since ? NO_START : sinceWhy) : fmtPctSigned(d, 2)}
        sub={`since the strategies started. ${upN} ${upN === 1 ? "is" : "are"} up, ${downN} ${downN === 1 ? "is" : "are"} down`}
        pic={<LinesPic none="No return yet" lines={runs.map((r) => ({ values: (r.equity_series ?? []).map((p) => p[1] / r.slice_opening_cash - 1), up: (returnOf(r) ?? 0) >= 0 }))} />}
        facts={[["Best", best ? fmtPctSigned(best.ret, 2) : "-", toneOf(best?.ret)], ["Worst", worst ? fmtPctSigned(worst.ret, 2) : "-", toneOf(worst?.ret)]]} />
      <Card name="Strategies" hue={asked ? "warn" : "run"} chip={asked ? ["warn", `${asked} asking`] : undefined} fig={String(runs.length)}
        sub={`on paper, the longest for ${plural(longest, "day", "days")}`}
        pic={<RowsPic none="No strategy yet" rows={runs.map((r) => ({ value: Math.max(1, daysOn(r)), tone: r.latest_promotion?.state === "requested" ? "warn" : r.latest_promotion?.state === "granted" ? "good" : "data", title: `${nameOf(r)}: ${plural(daysOn(r), "day", "days")}` }))} />}
        facts={[["Promotion asked", String(asked), asked ? "warn" : ""], ["Promoted", String(granted), granted ? "good" : ""]]} />
      <Card name="Orders" hue="run" fig={week ? String(week.trades_in_window) : dash(weekWhy)} sub="sent in the last 7 days"
        pic={<Share parts={[["data", week ? Math.max(1, week.trades_in_window) : 0]]} />}
        facts={[["Filled", week ? String(week.trades_in_window) : "-", week && week.trades_in_window ? "good" : ""]]} />
    </Wall>
  );
}

function RunSection({ run, name }: { run: PaperRun; name: { text: string; hover?: string } }) {
  const pts = run.equity_series ?? [];
  const values = pts.map((p) => p[1]);
  const start = run.slice_opening_cash > 0 ? run.slice_opening_cash : values[0];
  const last = values.length - 1;
  const retAt = (v: number) => (start > 0 ? v / start - 1 : null);
  let peak = -Infinity;
  const below = values.map((v) => { peak = Math.max(peak, v); return v / peak - 1; });
  const ddNow = below.length ? below[below.length - 1] : null;
  const ddMax = below.length ? Math.min(...below) : null;
  const xs = Array.from(new Set([0, Math.floor(last / 2), last]));
  const xl = xs.map((i) => [i, fmtDate(pts[i]?.[0])] as [number, string]);
  const dropText = (v: number | null) => (v === null ? "-" : v === 0 ? "At its high" : `${(v * 100).toFixed(2)}%`);
  const s = run.slice;
  const c = counts(run);
  const ret = returnOf(run);
  const promo = promoOf(run);
  const facts: Array<[string, ReactNode, ("good" | "bad" | "warn" | "")?, string?]> = [
    ["Started with", usd(run.slice_opening_cash)],
    ["Worth now", s ? usd(s.equity) : dash(NO_FIGURES)],
    ["Closed result", s ? usdSigned(s.realised_pnl) : dash(NO_FIGURES), s ? toneOf(s.realised_pnl) : "", "From trades already closed"],
    ["Return", ret === null ? dash(NO_FIGURES) : fmtPctSigned(ret, 2), toneOf(ret)],
    ["Below its high now", dropText(ddNow)],
    ["Deepest drop", dropText(ddMax), "", "The furthest it has been below its high"],
    ["Trades closed", c ? String(c.wins + c.losses) : dash(NO_COUNTS), "", c ? `${c.wins} won, ${c.losses} lost` : "None counted yet"],
  ];
  return (
    <>
      <Block title={<span data-testid="main-title">Account value, <span title={name.hover}>{name.text}</span></span>} id="pp-run"
        state={<State tone={promo[0]}>{promo[1]}</State>} right={<RunFoot run={run} name={name.text} />}>
        {values.length < 2 ? (
          <p className="empty">{TOO_FEW}</p>
        ) : (
          <>
            <div className="pp-chart">
              <LineChart
                series={start > 0
                  ? [{ v: values, c: "var(--b)", sw: 2.6, fill: 0.22 }, { v: values.map(() => start), c: "#e3e7ed", sw: 1, op: 0.3, glow: false }]
                  : [{ v: values, c: "var(--b)", sw: 2.6, fill: 0.22 }]}
                yfmt={(v) => `$${Math.round(v).toLocaleString("en-US")}`}
                xlabels={xl}
                extra={start > 0 ? (_X, Y, w, _h, pad) => (
                  <text x={w - pad[1] - 4} y={Y(start) + (values[last] >= start ? 14 : -6)} textAnchor="end" fill="rgba(141,152,168,.95)"
                    style={{ fontFamily: "var(--sans)", fontSize: 11.5, fontWeight: 500 }}>{`Started at ${usd(start)}`}</text>
                ) : undefined}
                rows={(i) => {
                  const r = retAt(values[i]);
                  return [fmtDate(pts[i][0]), ["Worth", fmtUsd(values[i])], ["Since it started", r === null ? "-" : fmtPctSigned(r, 2), toneOf(r) === "bad" ? "rd" : toneOf(r) === "good" ? "g" : undefined]];
                }}
              />
            </div>
            <p className="tp-cap">Below its highest worth so far</p>
            <div className="pp-chart drop">
              <LineChart series={[{ v: below, c: "var(--b)", sw: 1.8, fill: 0.2 }]} minHeight={120} yfmt={(v) => (v ? `${(v * 100).toFixed(1)}%` : "0%")}
                xlabels={xl} rows={(i) => [fmtDate(pts[i][0]), ["Below its highest worth", dropText(below[i])]]} />
            </div>
          </>
        )}
        <PlainFacts facts={facts} />
      </Block>
    </>
  );
}

const PART_DONE = "The stop is only part done. Press Stop again to finish it.";

// The sentence for a refused paper action: the server's own words when it wrote a sentence, else a plain fallback.
function actionRefusal(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return fallback;
}

// The one stop request: the dialog on the laptop and the hold button on the phone both send it.
// Returns the sentence to show when it did not finish, or null when the run ended.
async function sendStop(runId: number): Promise<string | null> {
  let answer;
  try {
    answer = await killRun(runId);
  } catch (e) {
    return actionRefusal(e, "Could not stop this strategy.");
  }
  return answer.end_reason !== "killed" ? PART_DONE : null;
}

function RunFoot({ run, name }: { run: PaperRun; name: string }) {
  const qc = useQueryClient();
  const phone = useLayout() === "phone";
  const [dialog, setDialog] = useState<"stop" | "promote" | "approve" | null>(null);
  const [holdBusy, setHoldBusy] = useState(false);
  const [holdProblem, setHoldProblem] = useState<string | null>(null);
  const state = run.latest_promotion?.state;
  const refresh = async () => {
    setDialog(null);
    await qc.invalidateQueries({ queryKey: ["paper", "runs"] });
    await qc.invalidateQueries({ queryKey: ["real", "readiness"] });
  };
  async function holdStop() {
    setHoldBusy(true);
    setHoldProblem(null);
    const problem = await sendStop(run.paper_run_id);
    setHoldBusy(false);
    if (problem !== null) {
      setHoldProblem(problem);
      return;
    }
    await refresh();
  }
  return (
    <div className="tp-acts pp-foot">
      {state === "requested" ? (
        <PhoneOff>
          <button
            type="button"
            className="btn"
            title="Closes its paper positions as Stop does, then ends its paper run as approved; no real order is sent and real money stays off until your go"
            onClick={() => setDialog("approve")}
          >
            Approve for real money
          </button>
        </PhoneOff>
      ) : state === "granted" ? null : (
        <PhoneOff>
          <button
            type="button"
            className="btn"
            title="Records a request only; real money stays off until your go"
            onClick={() => setDialog("promote")}
          >
            Promote
          </button>
        </PhoneOff>
      )}
      {phone ? (
        <>
          {holdProblem && <div className="pp-problem rd" role="alert">{holdProblem}</div>}
          <HoldButton label="Hold to stop" disabled={holdBusy} onHold={() => void holdStop()} />
        </>
      ) : (
        <button
          type="button"
          className="btn danger"
          title="Sells this strategy's own shares and ends its paper run; it comes back only through the judge"
          onClick={() => setDialog("stop")}
        >
          Stop
        </button>
      )}
      {dialog === "stop" && <StopDialog runId={run.paper_run_id} name={name} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === "promote" && <PromoteDialog runId={run.paper_run_id} name={name} onClose={() => setDialog(null)} onDone={refresh} />}
      {dialog === "approve" && <ApproveDialog runId={run.paper_run_id} name={name} onClose={() => setDialog(null)} onDone={refresh} />}
    </div>
  );
}

type ActionDialogProps = { runId: number; name: string; onClose: () => void; onDone: () => Promise<void> };

function StopDialog({ runId, name, onClose, onDone }: ActionDialogProps) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send() {
    setBusy(true);
    setProblem(null);
    const problem = await sendStop(runId);
    if (problem !== null) {
      setProblem(problem);
      setBusy(false);
      return;
    }
    await onDone();
  }

  return (
    <Window label="Stop this strategy" size="s" className="pp-dlg" onClose={onClose}>
        <div className="h2"><span>Stop {name} on paper</span></div>
        <p>Stop sells this strategy&apos;s own paper shares and ends its paper run. The strategy comes back only through the judge.</p>
        {problem && <div className="pp-problem rd" role="alert">{problem}</div>}
        <div className="pp-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            title="Sells this strategy's own shares and ends its paper run; it comes back only through the judge"
            onClick={() => void send()}
          >
            Stop
          </button>
        </div>
    </Window>
  );
}

function PromoteDialog({ runId, name, onClose, onDone }: ActionDialogProps) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send() {
    setBusy(true);
    setProblem(null);
    try {
      await promoteRun(runId);
    } catch (e) {
      setProblem(actionRefusal(e, "Could not promote this strategy."));
      setBusy(false);
      return;
    }
    await onDone();
  }

  return (
    <Window label="Promote this strategy" size="s" className="pp-dlg" onClose={onClose}>
        <div className="h2"><span>Promote {name}</span></div>
        <p>Promote records a request to move this strategy toward real money. It does not turn real money on and it sends no order.</p>
        {problem && <div className="pp-problem rd" role="alert">{problem}</div>}
        <div className="pp-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            title="Records a request only; real money stays off until your go"
            onClick={() => void send()}
          >
            Promote
          </button>
        </div>
    </Window>
  );
}

// The one place that approves a strategy for real money: it sends nothing until Approve is pressed.
function ApproveDialog({ runId, name, onClose, onDone }: ActionDialogProps) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send() {
    setBusy(true);
    setProblem(null);
    try {
      await approveForRealMoney(runId);
    } catch (e) {
      setProblem(actionRefusal(e, "Could not approve this strategy."));
      setBusy(false);
      return;
    }
    await onDone();
  }

  return (
    <Window label="Approve for real money" size="s" className="pp-dlg" onClose={onClose}>
        <div className="h2"><span>Approve {name} for real money</span></div>
        <p>Its paper positions are closed as Stop closes them, then its paper run ends as approved. No real order is sent; real money stays off until your go is recorded in the decisions log.</p>
        <p>The paper broker is not connected yet, so no paper shares are actually sold.</p>
        {problem && <div className="pp-problem rd" role="alert">{problem}</div>}
        <div className="pp-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            title="Closes its paper positions as Stop does, then ends its paper run as approved; no real order is sent and real money stays off until your go"
            onClick={() => void send()}
          >
            Approve
          </button>
        </div>
    </Window>
  );
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const whenParts = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
// "13 Nov, 21:43" in UTC, as the mockup writes a time.
function whenOf(iso: string): string {
  const p = Object.fromEntries(whenParts.formatToParts(Date.parse(iso)).map((x) => [x.type, x.value]));
  return `${p.day} ${MONTHS[Number(p.month) - 1]}`;
}
const timeOf = (iso: string): string => whenParts.format(Date.parse(iso)).split(", ")[1] ?? "";
const NO_REASON = "The server gave no reason";

function holdingsState(d: HoldingsResponse): ReactNode {
  if (d.rows.length === 0) return undefined;
  const unpriced = d.rows.find((r) => r.mark_price === null);
  if (unpriced) return dash(unpriced.mark_unavailable_reason ?? NO_REASON);
  if (d.rows.some((r) => r.share === null)) return dash(d.share_unavailable_reason ?? NO_REASON);
  return `${Number((d.rows.reduce((sum, r) => sum + (r.share ?? 0), 0) * 100).toFixed(1))}% invested`;
}

function ShareCell({ share, tone, why }: { share: number | null; tone: "data" | "soft"; why: string }): ReactNode {
  if (share === null) return dash(why);
  return (
    <>
      <Share parts={[[tone, Math.min(1, Math.max(0, share))], ["soft", 1 - Math.min(1, Math.max(0, share)), 0.1]]} />
      {(share * 100).toFixed(1)}%
    </>
  );
}

function HoldingsBlock({ run, name }: { run: PaperRun; name: { text: string; hover?: string } }) {
  const q = useQuery({
    queryKey: ["paper", "holdings", run.paper_run_id],
    queryFn: () => getHoldings(run.paper_run_id),
  });
  const title = <span data-testid="holdings-title">Holdings, <span title={name.hover}>{name.text}</span></span>;
  let state: ReactNode;
  let body: ReactNode;
  if (q.isError) {
    body = <div className="pp-none" title={q.error instanceof Error ? q.error.message : undefined}>Could not load the holdings.</div>;
  } else if (!q.data) {
    body = <div className="pp-none">Loading the holdings.</div>;
  } else if (q.data.rows.length === 0) {
    body = <div className="pp-none">No holdings for this strategy yet.</div>;
  } else {
    const d = q.data;
    const shareWhy = d.share_unavailable_reason ?? NO_REASON;
    const note = holdingsState(d);
    state = <span className="nl-st dim">{note}</span>;
    const cashShare = d.marked_equity === null || d.marked_equity <= 0 ? null : d.cash / d.marked_equity;
    body = (
      <div className="tp-scroll">
        <div className="pp-alloc"><Share parts={[...d.rows.map((r, i): SharePart => ["data", r.value ?? 0, Math.max(0.4, 0.9 - i * 0.2)]), ["soft", d.cash, 0.3]]} /></div>
        <table className="tp-tbl">
          <thead><tr><th>Ticker</th><th className="pp-w">Side</th><th>Share of its worth</th><th className="r pp-w">Value</th><th className="r">Open result</th></tr></thead>
          <tbody>
            {d.rows.map((r) => {
              const rec = d.reconcile.find((x) => x.symbol === r.symbol);
              const differs =
                rec?.mismatch === true ? (
                  <span
                    className="a warn"
                    title={
                      rec.alpaca_quantity === null
                        ? `ours ${rec.ours_quantity}, Alpaca could not be read${d.alpaca_positions_unavailable_reason ? `, ${d.alpaca_positions_unavailable_reason}` : ""}`
                        : `differs from Alpaca: ours ${rec.ours_quantity}, Alpaca ${rec.alpaca_quantity}`
                    }
                  >
                    Differs
                  </span>
                ) : null;
              const ticker = <td>{r.symbol} {differs}</td>;
              if (r.mark_price === null) {
                const why = r.mark_unavailable_reason ?? NO_REASON;
                const none = <span title={why}>no price</span>;
                return <tr key={r.symbol}>{ticker}<td className="pp-w">Long</td><td>{none}</td><td className="r pp-w">{none}</td><td className="r">{none}</td></tr>;
              }
              return (
                <tr key={r.symbol}>
                  {ticker}<td className="pp-w">Long</td>
                  <td className="num"><ShareCell share={r.share} tone="data" why={shareWhy} /></td>
                  <td className="r num pp-w"><span title={`Alpaca IEX latest trade, ${fmtTime(r.mark_time)}`}>{fmtUsd(r.value)}</span></td>
                  <td className={`r num ${toneOf(r.open_pnl)}`}>{fmtUsd(r.open_pnl, { sign: true })}</td>
                </tr>
              );
            })}
            <tr>
              <td>Cash</td><td className="pp-w" />
              <td className="num"><ShareCell share={cashShare} tone="soft" why={shareWhy} /></td>
              <td className="r num pp-w">{fmtUsd(d.cash)}</td><td />
            </tr>
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <div data-testid="holdings">
      <Block title={title} state={state}>{body}</Block>
    </div>
  );
}

function OrdersBlock({ run }: { run: PaperRun }) {
  const q = useQuery({
    queryKey: ["paper", "fills", run.paper_run_id],
    queryFn: () => getRunFills(run.paper_run_id),
  });
  let body: ReactNode;
  let state: ReactNode;
  if (q.isError) body = <div className="pp-none">Could not load the orders.</div>;
  else if (!q.data) body = <div className="pp-none">Loading the orders.</div>;
  else {
    const newest: PaperFill[] = [...q.data.fills]
      .sort((a, b) => (a.recorded_at < b.recorded_at ? 1 : a.recorded_at > b.recorded_at ? -1 : b.fill_id - a.fill_id))
      .slice(0, 8);
    state = <State tone="dim">{String(q.data.fills.length)}</State>;
    body = newest.length === 0 ? <div className="pp-none">No orders yet.</div> : (
      <div className="tp-scroll">
        <table className="tp-tbl">
          <thead><tr><th>When</th><th>Side</th><th>Ticker</th><th className="r pp-w">Price</th><th /></tr></thead>
          <tbody>
            {newest.map((f) => (
              <tr key={f.fill_id}>
                <td className="num">{whenOf(f.recorded_at)}<span className="pp-w">, {timeOf(f.recorded_at)}</span></td>
                <td>{f.side === "buy" ? "Buy" : "Sell"}</td>
                <td>{f.symbol}</td>
                <td className="r num pp-w">{f.fill_price === null ? "-" : `$${f.fill_price.toFixed(2)}`}</td>
                <td className="r">{f.fill_price === null
                  ? <span className="nl-st warn" title={f.refusal_reason ?? NO_REASON}>Refused</span>
                  : <span className="nl-st good">Filled</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return <Block title="Orders" state={state}>{body}</Block>;
}

function LimitsBlock({ limitsQ, editing, setEditing, wide }: {
  limitsQ: { data?: { limits: PaperLimit[] }; isError: boolean; error: unknown }; editing: boolean; setEditing: (b: boolean) => void; wide: boolean;
}) {
  let state: ReactNode;
  let right: ReactNode;
  let body: ReactNode;
  if (limitsQ.isError) {
    body = <div className="pp-none" title={limitsQ.error instanceof Error ? limitsQ.error.message : undefined}>Could not load the limits.</div>;
  } else if (!limitsQ.data) {
    body = <div className="pp-none">Loading the limits.</div>;
  } else {
    const limits = limitsQ.data.limits;
    const unset = LIMITS.filter((l) => valueOf(limits, l.key) === null).length;
    state = unset > 0
      ? <span title={`${unset} of ${LIMITS.length} not set`}><State tone="warn">Orders blocked</State></span>
      : <State tone="good">All set</State>;
    right = (
      <PhoneOff>
        <button type="button" className="btn" onClick={() => setEditing(true)}>Edit</button>
      </PhoneOff>
    );
    body = (
      <>
        <div className={wide ? "pp-lims wide" : "pp-lims"}>
          {LIMITS.map((l) => {
            const v = valueOf(limits, l.key);
            return (
              <div className="pp-lim" key={l.key}>
                <span title={limits.find((x) => x.key === l.key)?.meaning}>{l.name}</span>
                <span className="pp-lm" />
                {v === null ? <b className="warn">Not set</b> : <b className="num">{l.whole ? `${v.toLocaleString("en-US", { maximumFractionDigits: 0 })} orders` : usd(v)}</b>}
              </div>
            );
          })}
        </div>
        {editing && <LimitsDialog limits={limits} onClose={() => setEditing(false)} />}
      </>
    );
  }
  return (
    <div data-testid="limits">
      <Block title="Limits" state={state} right={right}>{body}</Block>
    </div>
  );
}

const BLOCKED = "Each limit needs a number; a limit cannot be emptied once set.";
const REFUSALS: Record<string, string> = {
  "paper-limit-value-invalid": "Each limit must be a number above 0, and orders per minute a whole number.",
  "paper-limit-unknown-key": "That limit is not one the server knows.",
};

// The server answers 422 with detail "<refusal word>: <why>"; only the word is read here.
function refusalSentence(err: unknown): string {
  if (err instanceof ApiError && err.status === 422) {
    const detail = (err.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      const word = detail.split(":")[0].trim();
      return REFUSALS[word] ?? `The server refused: ${word.replace(/-/g, " ")}`;
    }
  }
  return "Could not save the limits.";
}

function LimitsDialog({ limits, onClose }: { limits: PaperLimit[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [typed, setTyped] = useState<Record<string, string>>(() =>
    Object.fromEntries(LIMITS.map((l) => [l.key, String(valueOf(limits, l.key) ?? "")])),
  );
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);


  async function save() {
    const changed: Record<string, number> = {};
    for (const l of LIMITS) {
      const before = valueOf(limits, l.key);
      const raw = typed[l.key].trim();
      if (raw === "") {
        if (before !== null) return setProblem(BLOCKED);
        continue;
      }
      const n = Number(raw);
      if (!Number.isFinite(n)) return setProblem(BLOCKED);
      if (n !== before) changed[l.key] = n;
    }
    if (Object.keys(changed).length === 0) return onClose();
    setBusy(true);
    setProblem(null);
    try {
      await putPaperLimits(changed);
    } catch (e) {
      setProblem(refusalSentence(e));
      setBusy(false);
      return;
    }
    onClose();
    await qc.invalidateQueries({ queryKey: ["paper", "limits"] });
  }

  return (
    <Window label="Paper limits" size="s" className="pp-dlg" onClose={onClose}>
        <div className="h2"><span>Limits</span></div>
        <p>The server sends no paper order until all six limits have a value. Only the fields you change are sent.</p>
        {LIMITS.map((l) => (
          <div className="pp-frow" key={l.key}>
            <label htmlFor={`pp-${l.key}`}>{l.name}</label>
            <input
              id={`pp-${l.key}`}
              type="text"
              inputMode="decimal"
              value={typed[l.key]}
              onChange={(e) => setTyped({ ...typed, [l.key]: e.target.value })}
            />
          </div>
        ))}
        {problem && <div className="pp-problem rd" role="alert">{problem}</div>}
        <div className="pp-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button type="button" className="btn" disabled={busy} onClick={() => void save()}>Save</button>
        </div>
    </Window>
  );
}
