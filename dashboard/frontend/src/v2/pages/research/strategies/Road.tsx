import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { State } from "../../../kit";
import type { NlTone } from "../../../kit";
import { PhoneOff } from "../../../kit/phone";
import type { GenTheory, GenTree } from "../../../data/generatorApi";
import type { JudgeVerdictRow } from "../../../data/judgeApi";
import { fetchStrategyParameters, strategiesKeys } from "../../../data/strategiesApi";
import type { StrategyOverviewRow } from "../../../data/strategiesApi";
import { plainStrategyName } from "../../../../lib/format";
import { NUMS, nearestOf } from "../generator/Closeness";

// Round six (Tamer 2026-11-14), drawn first in the look trial (Desktop/overview_trial, lab-p-strategies.js). The question:
// how far has each strategy got on the road to paper money? A theory strategy is found in the generator's trees by its
// stored name (theory_strategy.py: "theory__" and the theory's name); where it stands is read from its theory's runs and
// search answer, a judge verdict and a paper run. Nothing is guessed: what is not stored shows as not reached.

export const THEORY_PREFIX = "theory__";

const STAGE_AT: Record<string, number> = { screen: 0, optimise: 1, walkforward: 2, later: 2 };
const STAGE_WORD: Record<string, string> = { screen: "Screen", optimise: "Optimise", walkforward: "Walk-forward", later: "Later years" };
// A theory's stop record names the step it stopped at (generator_runner.py _stop: a stage, or "judge").
const STOP_AT: Record<string, number> = { ...STAGE_AT, judge: 3 };
const STOP_WORD: Record<string, string> = {
  screen: "Stopped at the screen", optimise: "Stopped while tuning", walkforward: "Stopped at the walk-forward", later: "Stopped on the later years",
  judge: "Stopped at the judge",
};
const own = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k);

/** `unread`: stations before `at` that this page does not read, drawn hollow rather than done. `pool`: the search's answer was
 *  no, because none of its pool met every bar. */
export type Where = { at: number; tone: NlTone; word: string; why: string; unread?: number[]; pool?: true };
export type Found = { theory: GenTheory; family: string; approvedAt: number | null };

/** The theory a strategy was made from, by its stored name; the newest theory when two searches share a name. */
export function theoryOf(s: StrategyOverviewRow, trees: GenTree[]): Found | null {
  if (!s.name.startsWith(THEORY_PREFIX)) return null;
  const key = s.name.slice(THEORY_PREFIX.length);
  let best: Found | null = null;
  for (const t of trees) {
    for (const th of t.theories) {
      if (th.name === key && (best === null || th.id > best.theory.id)) best = { theory: th, family: t.template.display_name, approvedAt: typeof t.batch.approved_at === "number" ? t.batch.approved_at : null };
    }
  }
  return best;
}

const VERDICT_WHERE: Record<JudgeVerdictRow["verdict"], [NlTone, string]> = {
  pass: ["good", "Passed the judge"],
  fail: ["bad", "Failed the judge"],
  unjudgeable: ["warn", "The judge could not grade it"],
};

/** Where a strategy stands: paper first, then the judge's newest verdict, then its theory's furthest run. */
export function whereOf(s: StrategyOverviewRow, found: Found | null, verdict: JudgeVerdictRow | null, onPaper: boolean): Where {
  if (onPaper) return { at: 6, tone: "good", word: "On paper", why: "A paper run of it is running", unread: [4, 5] };
  if (verdict) {
    const [tone, word] = VERDICT_WHERE[verdict.verdict];
    const cleared = verdict.check_outcomes.filter((c) => c.outcome === "cleared").length;
    return { at: 3, tone, word, why: `${cleared} of ${verdict.check_outcomes.length} checks cleared` };
  }
  if (found === null) {
    if (s.name.startsWith(THEORY_PREFIX)) return { at: 0, tone: "dim", word: "No search found", why: "Its theory is not in the generator's searches" };
    return s.run !== null
      ? { at: 0, tone: "dim", word: "Backtested", why: "Written by hand: it has a backtest and no search, the judge is next" }
      : { at: 0, tone: "dim", word: "Not run yet", why: "Written by hand and not backtested yet" };
  }
  const th = found.theory;
  const runs = th.runs.filter((r) => own(STAGE_AT, r.stage));
  const last = runs.length ? runs.reduce((a, r) => (STAGE_AT[r.stage] >= STAGE_AT[a.stage] ? r : a)) : null;
  // The stop record leads: it names the step itself, and a stop also cancels the runs still queued.
  if (th.stop) {
    const why = th.stop.reason.replace(/_/g, " ");
    const at = own(STOP_AT, th.stop.step) ? STOP_AT[th.stop.step] : last ? STAGE_AT[last.stage] : 0;
    if (why.startsWith("Stopped by you")) return { at, tone: "dim", word: "Stopped by you", why };
    return { at, tone: "bad", word: own(STOP_WORD, th.stop.step) ? STOP_WORD[th.stop.step] : "Stopped", why };
  }
  if (last === null) return { at: 0, tone: "dim", word: "Not started", why: "No screen run yet" };
  const at = STAGE_AT[last.stage];
  const stage = STAGE_WORD[last.stage];
  if (last.status === "pending" || last.status === "running") return { at, tone: "run", word: `${stage} running`, why: `Run ${last.run_id} is ${last.status}` };
  if (last.status === "cancelled") return { at, tone: "dim", word: `${stage} cancelled`, why: `Run ${last.run_id} was cancelled` };
  if (last.status !== "succeeded") return { at, tone: "bad", word: `${stage} failed`, why: `Run ${last.run_id} ${last.status}` };
  const n = nearestOf(th);
  if (at === 0 && n !== null && n !== "odd" && n.answer === "no") {
    const rounds = th.search?.rounds?.rounds?.length ?? 0;
    // quality.py answers over its pool only: the best 5% by Sharpe of the strategies with 30 trades or more.
    return { at: 0, tone: "bad", word: "Stopped at the screen", pool: true, why: `Its search stopped after ${rounds === 1 ? "round 1" : `round ${rounds}`}: none of its best ${n.pool} attempts met every target` };
  }
  if (at === 0) return n !== null && n !== "odd" && n.answer === "yes"
    ? { at: 0, tone: "good", word: "Passed the screen", why: "Its search found strategies that met every bar" }
    : { at: 0, tone: "run", word: "Searching", why: `Run ${last.run_id} finished, the search goes on` };
  return { at, tone: "dim", word: `${stage} done`, why: `Run ${last.run_id} finished` };
}

export type RoadRow = { s: StrategyOverviewRow; found: Found | null; where: Where | null; stateWhy: string | null; name: { text: string; title: string } };

type Box = { k: string; from: number; to: number; step: number; n: number };
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** A search round's box of values, read defensively: null when the server sent no box or one of another shape. */
export function boxOf(round: unknown): Box[] | null {
  const box = (round as { box?: unknown } | null)?.box;
  if (typeof box !== "object" || box === null || Array.isArray(box)) return null;
  const out: Box[] = [];
  for (const [k, v] of Object.entries(box as Record<string, unknown>)) {
    const b = v as { from?: unknown; to?: unknown; step?: unknown; values?: unknown };
    if (!isNum(b.from) || !isNum(b.to) || !isNum(b.step) || !Array.isArray(b.values)) return null;
    out.push({ k, from: b.from, to: b.to, step: b.step, n: b.values.length });
  }
  return out;
}

/** The picked theory strategy: what its search tried and how its first screen went. The steps after it (judge, reserve exam,
 *  second engine, paper) are the card below, From the judge to paper, with their buttons. */
export function SearchCard({ s, found, where, onCode, middle }: { s: StrategyOverviewRow; found: Found; where: Where; onCode: () => void; middle?: ReactNode }) {
  const navigate = useNavigate();
  const paramsQ = useQuery({ queryKey: strategiesKeys.parameters(s.id), queryFn: () => fetchStrategyParameters(s.id) });
  const th = found.theory;
  const rounds = th.search?.rounds?.rounds ?? [];
  const r0 = rounds[0];
  const box = r0 ? boxOf(r0) : null;
  const named = new Map((paramsQ.data?.parameters ?? []).map((p) => [p.name, p]));
  const screen = th.runs.find((r) => r.stage === "screen") ?? null;
  const n = nearestOf(th);

  const settings = r0 && box !== null ? box : null;
  let tried: ReactNode;
  if (!r0) tried = <p className="sr-none">No search round is stored yet.</p>;
  else if (settings === null) tried = <p className="sr-none" title="The server sent the round's box in a shape this page does not know">-</p>;
  else {
    tried = (
      <table className="sr-tbl" data-testid="search-settings">
        <thead><tr><th>Setting</th><th className="num">From</th><th className="num">To</th><th className="num">Step</th><th className="sr-w">Why this range</th></tr></thead>
        <tbody>
          {settings.map((b) => {
            const p = named.get(b.k);
            const label = p ? plainStrategyName((p.range ?? p.name).replace(/\./g, " ")) : b.k;
            const why = p?.source?.why;
            return (
              <tr key={b.k} data-k={b.k} title={`${label}: ${b.n} value${b.n === 1 ? "" : "s"}`}>
                <td className="sr-bxn">{label}</td>
                <td className="num">{b.from}</td><td className="num">{b.to}</td><td className="num">{b.step}</td>
                <td className="sr-w">{why ?? <span title="No reason is stored for this range">-</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    );
  }

  let first: ReactNode;
  if (n === null) first = <p className="sr-none">No search answer is stored yet.</p>;
  else if (n === "odd") first = <p className="sr-none" title="The server sent the search answer in a shape this page does not know">-</p>;
  else if (Number.isNaN(n.sharpe)) first = <p className="sr-none">{"No strategy it tried had 30 trades or more and a Sharpe, so none could be held to the bars."}</p>;
  else {
    const bars = n.met + n.failed.length;
    first = (
      <>
        <table className="sr-tbl" data-testid="search-targets">
          <thead><tr><th>Target</th><th className="num">Its best attempt</th><th className="num">Needed</th><th>Result</th></tr></thead>
          <tbody>
            {NUMS.map(([k, label, fmt]) => {
              const v = n.m[k];
              const state = bars < 5 && k === "recovery" ? "off" : n.failed.includes(k) ? "bad" : "good";
              return (
                <tr key={k} data-k={k}
                  title={`${label}: ${v == null ? "none" : fmt(v)}. ${state === "off" ? "Shown, not a bar" : state === "bad" ? "Short of its bar" : "Past its bar"}`}>
                  <td>{label}</td>
                  <td className={`num ${state === "off" ? "" : state}`}>{v == null ? "-" : fmt(v)}</td>
                  <td className="num"><span title="The target itself is not served by the dashboard yet">-</span></td>
                  <td>{state === "off" ? <State tone="dim">Not a target</State> : <State tone={state}>{state === "bad" ? "Missed" : "Met"}</State>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <p className="sr-none">{`${r0 ? `It tried ${r0.new_trials.toLocaleString("en-GB")} of ${r0.valid_strategies.toLocaleString("en-GB")} possible settings. ` : ""}Its best attempt met ${n.met} of ${bars} targets, Sharpe ${n.sharpe.toFixed(2)}${n.answer === "no" ? ", so the search stopped" : ""}.`}</p>
      </>
    );
  }

  return (
    <div data-testid="search-card" className="sr-card">
      <p className="sr-none">{`${where.why}${/[.!?]$/.test(where.why) ? "" : "."}`}</p>
      <h3 className="sr-h3">Its first test{screen && <span className="num">{`Run ${screen.run_id}`}</span>}</h3>
      {first}
      <h3 className="sr-h3">The settings it searches{settings && <span className="num">{`${settings.length} setting${settings.length === 1 ? "" : "s"}`}</span>}</h3>
      {tried}
      {middle}
      <div className="sr-acts">
        <button type="button" className="btn primary" onClick={() => navigate("/app/research/generator")}>Open its search</button>
        {screen && <button type="button" className="btn" onClick={() => navigate(`/app/research/runs?run=${screen.run_id}`)}>Open its run</button>}
        <PhoneOff><button type="button" className="btn" onClick={onCode}>View the code</button></PhoneOff>
      </div>
    </div>
  );
}
