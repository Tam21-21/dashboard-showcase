import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { PhoneOff } from "../../../kit/phone";
import { Facts, KzSteps, Spark, StateWord, Table, Window, ZeroBar } from "../../../kit";
import type { Tone } from "../../../kit";
import {
  RUNS_AT_MOST, deleteStrategy, fetchReserveSupply, fetchRetest, fetchRetestFills, fetchStrategyRuns,
  fetchStrategySource, renameStrategy, replayResult, startRetest, strategiesKeys, takeReserveExam,
} from "../../../data/strategiesApi";
import type { RetestRow, StrategyOverviewRow, StrategyRun } from "../../../data/strategiesApi";
import { approveVerdictForPaper, rejectVerdict } from "../../../data/judgeApi";
import type { JudgeVerdictRow } from "../../../data/judgeApi";
import { sendToJudge } from "../../../data/runsApi";
import { strategyNamesKey } from "../../../data/strategyNames";
import { ApiError } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import { fmtDmy, fmtPctSigned } from "../../../../lib/format";
import { judgeKeys } from "../Judge";
import SweepWindow from "../sweep/SweepWindow";

// Mockup: docs/design/rail_mockup_2026-10-10.html (at 05ac8de4), roadToPaper: "From the judge to paper".
// Every write sits behind a window; a refusal shows in the server's own words.

export const paperKeys = { paper: ["paper", "runs"] as const };

export const VERDICT_STATE: Record<JudgeVerdictRow["verdict"], { word: string; tone: Tone }> = {
  pass: { word: "PASSED", tone: "g" },
  fail: { word: "FAILED", tone: "rd" },
  unjudgeable: { word: "INCOMPLETE", tone: "a" },
};

const GREEN = "#2fe08a";
const RED = "#ff5a63";
const GREY = "#3a424d";
const NOT_JUDGED = "Not judged yet";

function Dash({ why }: { why: string }) {
  return <span title={why}>-</span>;
}

// The four words the server gives when it refuses a retest, in plain sentences.
const RETEST_REFUSALS: Record<string, string> = {
  "ungraded-candidate": "This run has no judge verdict yet",
  "unsupported-timeframe": "Only daily runs can be retested",
  "missing-bars": "The run's price file is not stored",
  "missing-configuration-statement": "The run's settings were not recorded",
};

function retestRefusal(e: unknown): string {
  if (e instanceof ApiError && typeof e.body === "object" && e.body !== null && "detail" in e.body) {
    const detail = (e.body as { detail?: unknown }).detail;
    const m = typeof detail === "string" ? /^refused: (.+)$/.exec(detail) : null;
    if (m && RETEST_REFUSALS[m[1]]) return RETEST_REFUSALS[m[1]];
  }
  return serverDetail(e);
}

type Action = { label: string; danger?: boolean; disabled?: boolean; title?: string; onClick: () => void };

// One window: a heading, the problem or the answer, then CANCEL and the action. After an answer only CLOSE is left.
function Shell({
  label, title, size = "s", onClose, problem, message, action, children,
}: {
  label: string; title: string; size?: "s" | "m" | "l" | "xl"; onClose: () => void; problem?: string | null;
  message?: string | null; action?: Action | null; children?: ReactNode;
}) {
  return (
    <Window label={label} size={size} onClose={onClose}>
      <div className="h2"><span>{title}</span></div>
      {children}
      {problem && <div className="rp-problem rd" role="alert">{problem}</div>}
      {message && <div className="rp-answer g" role="status">{message}</div>}
      <div className="rp-foot">
        {message || !action ? (
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        ) : (
          <>
            <button type="button" className="btn q" onClick={onClose}>CANCEL</button>
            <button
              type="button"
              className={action.danger ? "btn danger" : "btn"}
              disabled={action.disabled}
              title={action.title}
              onClick={action.onClick}
            >
              {action.label}
            </button>
          </>
        )}
      </div>
    </Window>
  );
}

// The state of one press: busy while the call runs, then the server's problem or the answer.
function useAction(explain: (e: unknown) => string = serverDetail) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  async function go(call: () => Promise<string | null>, onClose: () => void) {
    setBusy(true);
    setProblem(null);
    try {
      const answer = await call();
      if (answer === null) {
        onClose();
        return;
      }
      setMessage(answer);
    } catch (e) {
      setProblem(explain(e));
    }
    setBusy(false);
  }
  return { busy, problem, message, go };
}

type WindowProps = { onClose: () => void };

function RenameWindow({ s, onClose }: WindowProps & { s: StrategyOverviewRow }) {
  const qc = useQueryClient();
  const [name, setName] = useState(s.display_name ?? "");
  const act = useAction();
  return (
    <Shell
      label="Rename this strategy"
      title="RENAME"
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "SAVE",
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            const src = await fetchStrategySource(s.id);
            await renameStrategy(s.id, src.source_code, name);
            await Promise.all([
              qc.invalidateQueries({ queryKey: strategiesKeys.overview }),
              qc.invalidateQueries({ queryKey: strategyNamesKey }),
            ]);
            return null;
          }, onClose),
      }}
    >
      <label className="rp-fld">
        <span className="kl">DISPLAY NAME</span>
        <input className="fin" aria-label="Display name" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
    </Shell>
  );
}

function DeleteWindow({ s, onClose }: WindowProps & { s: StrategyOverviewRow }) {
  const qc = useQueryClient();
  const act = useAction();
  return (
    <Shell
      label="Delete this strategy"
      title={`Delete ${s.display_name ?? "this strategy"}?`}
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "DELETE",
        danger: true,
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            const r = await deleteStrategy(s.id);
            await Promise.all([
              qc.invalidateQueries({ queryKey: strategiesKeys.overview }),
              qc.invalidateQueries({ queryKey: strategyNamesKey }),
            ]);
            if (r.action === "tombstoned") return `Deleted; its ${r.orphaned_runs ?? 0} runs are kept`;
            return r.action === "removed" ? "Deleted" : "Already deleted";
          }, onClose),
      }}
    />
  );
}

function ReplayWindow({ v, onClose }: WindowProps & { v: JudgeVerdictRow }) {
  const act = useAction();
  return (
    <Shell
      label="Replay this result"
      title="REPLAY THIS RESULT"
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "REPLAY",
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            const r = await replayResult(v.run_id, v.variation_id, v.ticker);
            return r.status === "replay-exists" ? "A saved replay of this result already exists" : "Replay queued";
          }, onClose),
      }}
    />
  );
}

function RejectWindow({ v, onClose }: WindowProps & { v: JudgeVerdictRow }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const act = useAction();
  return (
    <Shell
      label="Reject this verdict"
      title="REJECT THIS VERDICT"
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "REJECT",
        danger: true,
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            await rejectVerdict(v.id, reason.trim() === "" ? undefined : reason.trim());
            await qc.invalidateQueries({ queryKey: judgeKeys.verdicts });
            return null;
          }, onClose),
      }}
    >
      <label className="rp-fld">
        <span className="kl">REASON</span>
        <input className="fin" aria-label="Reason" placeholder="Optional" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
    </Shell>
  );
}

// 31 bits from the browser's random source, drawn once when the window opens.
function drawSeed(): number {
  return crypto.getRandomValues(new Uint32Array(1))[0] >>> 1;
}

function ExamWindow({ s, onClose }: WindowProps & { s: StrategyOverviewRow }) {
  const qc = useQueryClient();
  const [seed] = useState(drawSeed);
  const act = useAction();
  return (
    <Shell
      label="Take the next reserve exam"
      title="TAKE THE NEXT EXAM"
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "START",
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            const r = await takeReserveExam(s.id, seed);
            await qc.invalidateQueries({ queryKey: strategiesKeys.supply });
            return `Exam started on ${fmtDmy(r.exam.window_start_ts)} to ${fmtDmy(r.exam.window_end_ts)}`;
          }, onClose),
      }}
    >
      <div className="rp-line"><span className="kl">SEED</span><span className="num">Seed {seed}</span></div>
    </Shell>
  );
}

function RetestWindow({ runId, onClose }: WindowProps & { runId: number }) {
  const qc = useQueryClient();
  const act = useAction(retestRefusal);
  return (
    <Shell
      label="Retest in the second engine"
      title="RETEST IN THE SECOND ENGINE"
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "RETEST",
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            await startRetest(runId);
            await qc.invalidateQueries({ queryKey: strategiesKeys.retest(runId) });
            return "Retest queued";
          }, onClose),
      }}
    />
  );
}

const money = (n: number | null) => (n === null ? "-" : n.toFixed(2));

function FillsWindow({ retestId, onClose }: WindowProps & { retestId: number }) {
  const q = useQuery({ queryKey: strategiesKeys.fills(retestId), queryFn: () => fetchRetestFills(retestId) });
  let body: ReactNode;
  if (q.isError) body = <p className="empty">{serverDetail(q.error)}</p>;
  else if (!q.data) body = <p className="empty">Loading the fills.</p>;
  else if (q.data.fills.length === 0) body = <p className="empty">No fills in this retest</p>;
  else {
    const fills = q.data.fills;
    const widest = Math.max(0, ...fills.map((f) => (f.diff_vbt === null ? 0 : Math.abs(f.diff_vbt))));
    body = (
      <Table
        head={["DATE", "SIDE", "BACKTEST", "NAUTILUS", "SHADOW", "GAP"]}
        rows={fills.map((f) => [
          fmtDmy(f.signal_date),
          f.side.toUpperCase(),
          money(f.vbt_fill_price),
          money(f.real_fill_price),
          money(f.shadow_fill_price),
          f.diff_vbt === null ? (
            <Dash why="No next bar to compare with" />
          ) : (
            <span className="rp-gap"><ZeroBar value={f.diff_vbt} widest={widest} /><span className="num">{f.diff_vbt.toFixed(2)}</span></span>
          ),
        ])}
      />
    );
  }
  return <Shell label="Fills side by side" title="FILLS" size="l" onClose={onClose}>{body}</Shell>;
}

const RUN_STATE: Record<string, { word: string; tone: Tone }> = {
  succeeded: { word: "DONE", tone: "g" },
  failed: { word: "FAILED", tone: "rd" },
};

function RunsWindow({ runs, error, onClose }: WindowProps & { runs: StrategyRun[] | undefined; error: unknown }) {
  const navigate = useNavigate();
  let body: ReactNode;
  if (error != null) body = <p className="empty">{serverDetail(error)}</p>;
  else if (!runs) body = <p className="empty">Loading the runs.</p>;
  else if (runs.length === 0) body = <p className="empty">No real run yet</p>;
  else {
    body = (
      <div className="rp-runs">
        <Table
          head={["DATE", "STATE", "SHARPE", "TRADES", "RETURN", "CURVE"]}
          rows={runs.map((r) => {
            const st = RUN_STATE[r.status] ?? { word: r.status.toUpperCase(), tone: "bl" as Tone };
            const up = r.total_return === null || r.total_return >= 0;
            return [
              fmtDmy(r.created_at),
              <StateWord key="s" word={st.word} tone={st.tone} />,
              r.sharpe_ratio === null ? <Dash why="No Sharpe for this run" /> : r.sharpe_ratio.toFixed(2),
              r.num_trades === null ? <Dash why="No trade count for this run" /> : String(r.num_trades),
              r.total_return === null ? <Dash why="No total return for this run" /> : fmtPctSigned(r.total_return, 2),
              r.equity_sparkline !== null && r.equity_sparkline.length >= 2 ? (
                <div key="c" className="rp-spark"><Spark values={r.equity_sparkline} colour={up ? GREEN : RED} height={22} dot={false} /></div>
              ) : (
                <Dash why="No curve stored for this run" />
              ),
            ];
          })}
        />
      </div>
    );
  }
  return (
    <Shell
      label="This strategy's runs"
      title="RUNS"
      size="xl"
      onClose={onClose}
      action={{ label: "OPEN THE RUNS PAGE", onClick: () => navigate("/app/research/runs") }}
    >
      {body}
    </Shell>
  );
}

// The second engine's word for the state the server derives, and what its hover says.
function engineWord(state: string, reason: string | null): { word: string; tone: Tone; why?: string } | null {
  switch (state) {
    case "never-retested": return { word: "NOT RETESTED", tone: "none" };
    case "passed": return { word: "PASSED", tone: "g" };
    case "report-only":
      return { word: "NOT PASSED", tone: "a", why: "It ran; its gaps are outside the allowed gap, or no allowed gap is set" };
    case "could-not-run":
      return { word: "COULD NOT RUN", tone: "rd", why: reason === null ? undefined : `Reason: ${reason.replace(/[-_]/g, " ")}` };
    case "unreadable-record": return { word: "UNREADABLE", tone: "a" };
    default: return null;
  }
}

const signed2 = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}`;

type Open = "rename" | "delete" | "replay" | "reject" | "exam" | "retest" | "fills" | "runs" | "approve" | "sweep" | null;

export default function RoadToPaper({ s, verdict, onEditCode }: { s: StrategyOverviewRow; verdict: JudgeVerdictRow | null; onEditCode: (id: number) => void }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState<Open>(null);
  const [sent, setSent] = useState<{ ok: boolean; text: string } | null>(null);
  const [busySend, setBusySend] = useState(false);
  const close = () => setOpen(null);

  const runsQ = useQuery({ queryKey: strategiesKeys.runs(s.id), queryFn: () => fetchStrategyRuns(s.id) });
  const supplyQ = useQuery({ queryKey: strategiesKeys.supply, queryFn: () => fetchReserveSupply() });
  const runId = verdict?.run_id ?? null;
  const retestQ = useQuery({
    queryKey: strategiesKeys.retest(runId ?? 0),
    queryFn: () => fetchRetest(runId as number),
    enabled: runId !== null,
  });

  const name = s.display_name;
  const run = s.run;

  // JUDGE
  let judgeSteps: ReactNode = <Dash why={NOT_JUDGED} />;
  let judgeWord: ReactNode = null;
  if (verdict) {
    const st = VERDICT_STATE[verdict.verdict];
    judgeSteps = (
      <KzSteps
        word="CLEARED"
        items={verdict.check_outcomes.map((c): [string, string] => [
          `Check ${c.check}: ${c.outcome}`,
          c.outcome === "cleared" ? GREEN : c.outcome === "failed" ? RED : GREY,
        ])}
      />
    );
    judgeWord = <StateWord word={st.word} tone={st.tone} />;
  }

  // RESERVE EXAM
  const windows = supplyQ.data?.windows ?? [];
  const untouched = windows.some((w) => !w.spent);
  let examSteps: ReactNode;
  if (supplyQ.isError) examSteps = <Dash why={serverDetail(supplyQ.error)} />;
  else if (!supplyQ.data) examSteps = <Dash why="Loading the reserve stretches" />;
  else if (windows.length === 0) examSteps = <Dash why="No reserve stretch is set" />;
  else {
    examSteps = (
      <KzSteps
        word="LEFT"
        items={windows.map((w): [string, string] => [
          `${fmtDmy(w.start_ts)} to ${fmtDmy(w.end_ts)}: ${w.spent ? "used" : "untouched"}`,
          w.spent ? GREY : GREEN,
        ])}
      />
    );
  }

  // SECOND ENGINE
  const rows: RetestRow[] = retestQ.data?.retests ?? [];
  const newest: RetestRow | null = rows.length > 0 ? rows[rows.length - 1] : null;
  let engineState: ReactNode = <Dash why={NOT_JUDGED} />;
  if (verdict) {
    if (retestQ.isError) engineState = <Dash why={serverDetail(retestQ.error)} />;
    else if (!retestQ.data) engineState = <Dash why="Loading the retest" />;
    else {
      const w = engineWord(retestQ.data.state, newest?.could_not_run_reason ?? null);
      engineState = w ? <span title={w.why}><StateWord word={w.word} tone={w.tone} /></span> : <Dash why="Unknown retest state" />;
    }
  }
  const gapWhy = newest === null ? "Not retested yet" : "No gap recorded for this retest";
  const fillsHeld = !verdict ? NOT_JUDGED : newest === null || newest.export_path === null ? "No fills recorded for this retest" : undefined;

  // FOOT
  const runCount = runsQ.data?.length;
  const canApprove = verdict !== null && verdict.verdict === "pass" && verdict.current && !verdict.rejected;

  async function send() {
    if (run === null) return;
    setBusySend(true);
    try {
      await sendToJudge(run.id);
      setSent({ ok: true, text: "Sent to the judge" });
      void qc.invalidateQueries({ queryKey: judgeKeys.verdicts });
    } catch (e) {
      setSent({ ok: false, text: serverDetail(e) });
    }
    setBusySend(false);
  }

  return (
    <div className="cd rp" data-testid="road-to-paper">
      <div className="rp-head" data-testid="rp-head">
        {name === null ? <span className="rp-name" title="No display name">-</span> : <span className="rp-name">{name}</span>}
        <span className="num rp-ver">version {s.version}</span>
        <PhoneOff>
          <span className="rp-acts">
            <button type="button" className="btn q" onClick={() => setOpen("sweep")}>RUN A SWEEP</button>
            <button type="button" className="btn q" onClick={() => setOpen("rename")}>RENAME</button>
            <button type="button" className="btn q" onClick={() => onEditCode(s.id)}>EDIT CODE</button>
            <button type="button" className="btn danger" onClick={() => setOpen("delete")}>DELETE</button>
          </span>
        </PhoneOff>
      </div>

      <div className="rp-cols">
        <div data-testid="rp-judge">
          <div className="kl">JUDGE</div>
          <div className="rp-viz">{judgeSteps}</div>
          <div className="rp-state">{judgeWord ?? null}</div>
          <PhoneOff>
            <div className="rp-btns">
              <button type="button" className="btn q" disabled={!verdict} title={verdict ? undefined : NOT_JUDGED} onClick={() => setOpen("replay")}>REPLAY</button>
              <button type="button" className="btn q rp-bad" disabled={!verdict} title={verdict ? undefined : NOT_JUDGED} onClick={() => setOpen("reject")}>REJECT</button>
            </div>
          </PhoneOff>
        </div>

        <div data-testid="rp-exam">
          <div className="kl" title="A test on the next untouched stretch of history">RESERVE EXAM</div>
          <div className="rp-viz">{examSteps}</div>
          <div className="rp-state">
            <span className="kl">RESULT</span>
            <Dash why="The server does not report a strategy's exam result yet" />
          </div>
          <PhoneOff>
            <div className="rp-btns">
              <button
                type="button"
                className="btn q"
                disabled={!untouched}
                title={untouched ? undefined : "No untouched stretch is left"}
                onClick={() => setOpen("exam")}
              >
                TAKE THE NEXT EXAM
              </button>
            </div>
          </PhoneOff>
        </div>

        <div data-testid="rp-engine">
          <div className="kl" title="The same result run again in NautilusTrader">SECOND ENGINE</div>
          <div className="rp-viz rp-facts">
            <Facts
              items={[
                ["RETURN GAP", newest?.gap_final_equity == null ? "-" : fmtPctSigned(newest.gap_final_equity, 2), undefined, newest?.gap_final_equity == null ? gapWhy : undefined],
                ["SHARPE GAP", newest?.gap_sharpe == null ? "-" : signed2(newest.gap_sharpe), undefined, newest?.gap_sharpe == null ? gapWhy : undefined],
                ["BAR", "-", undefined, "The server does not send the allowed gap yet"],
              ]}
            />
          </div>
          <div className="rp-state">{engineState}</div>
          <div className="rp-btns">
            <PhoneOff>
              <button type="button" className="btn q" disabled={!verdict} title={verdict ? undefined : NOT_JUDGED} onClick={() => setOpen("retest")}>RETEST</button>
            </PhoneOff>
            <button type="button" className="btn q" disabled={fillsHeld !== undefined} title={fillsHeld} onClick={() => setOpen("fills")}>FILLS</button>
          </div>
        </div>
      </div>

      <div className="rp-bottom" data-testid="rp-foot">
        <span className="rp-count">
          RUNS{" "}
          {runsQ.isError ? (
            <Dash why={serverDetail(runsQ.error)} />
          ) : runCount === undefined ? (
            <Dash why="Loading the runs" />
          ) : runCount >= RUNS_AT_MOST ? (
            <b className="num" title={`The server sends at most ${RUNS_AT_MOST} runs at a time`}>{RUNS_AT_MOST}+</b>
          ) : (
            <b className="num">{runCount}</b>
          )}
        </span>
        <button type="button" className="btn" onClick={() => setOpen("runs")}>OPEN RUNS</button>
        <span className="rp-fill" />
        {sent && <span className={sent.ok ? "g" : "rd"} role="status">{sent.text}</span>}
        <PhoneOff>
          <button
            type="button"
            className="btn q"
            disabled={run === null || busySend}
            title={run === null ? "No real run to send" : undefined}
            onClick={() => void send()}
          >
            SEND TO THE JUDGE
          </button>
          <button
            type="button"
            className="btn"
            disabled={!canApprove}
            title={canApprove ? undefined : "Only a passed, current verdict can go to paper"}
            onClick={() => setOpen("approve")}
          >
            APPROVE FOR PAPER
          </button>
        </PhoneOff>
      </div>

      {open === "rename" && <RenameWindow s={s} onClose={close} />}
      {open === "sweep" && <SweepWindow strategyId={s.id} onClose={close} />}
      {open === "delete" && <DeleteWindow s={s} onClose={close} />}
      {open === "replay" && verdict && <ReplayWindow v={verdict} onClose={close} />}
      {open === "reject" && verdict && <RejectWindow v={verdict} onClose={close} />}
      {open === "exam" && <ExamWindow s={s} onClose={close} />}
      {open === "retest" && runId !== null && <RetestWindow runId={runId} onClose={close} />}
      {open === "fills" && newest !== null && <FillsWindow retestId={newest.id} onClose={close} />}
      {open === "runs" && <RunsWindow runs={runsQ.data} error={runsQ.isError ? runsQ.error : null} onClose={close} />}
      {open === "approve" && verdict && <ApproveWindow s={s} v={verdict} onClose={close} />}
    </div>
  );
}

function ApproveWindow({ s, v, onClose }: WindowProps & { s: StrategyOverviewRow; v: JudgeVerdictRow }) {
  const qc = useQueryClient();
  const act = useAction();
  return (
    <Shell
      label="Send this strategy to paper"
      title={`Send ${s.display_name ?? "this strategy"} to paper?`}
      onClose={onClose}
      problem={act.problem}
      message={act.message}
      action={{
        label: "APPROVE FOR PAPER",
        disabled: act.busy,
        onClick: () =>
          void act.go(async () => {
            await approveVerdictForPaper(v.id, v.judged_capital);
            await Promise.all([
              qc.invalidateQueries({ queryKey: judgeKeys.verdicts }),
              qc.invalidateQueries({ queryKey: paperKeys.paper }),
            ]);
            return "Sent to paper";
          }, onClose),
      }}
    />
  );
}
