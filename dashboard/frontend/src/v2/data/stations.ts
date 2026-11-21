// The six stations from data to capital and the colour of each (Tamer 2026-11-14: "Each point should be green if the
// page is all working, and red if it's not, and yellow if it's waiting for me"). Grey only where no work has reached the
// stage yet, so green never claims a stage that has never run. Every rule reads only what the dashboard's own API reports,
// and each station carries the reason the rule fired and the rule itself, for its hover. One pure function, so the
// Research overview and any page that shows the line (Home) cannot drift.
import type { Dataset, Run, StrategyListItem } from "../../types/research";
import type { JudgeSummaryResponse } from "./judgeApi";
import { isPriceSeries, isStale } from "../pages/research/datasets/stale";

export type StationId = "data" | "strategies" | "runs" | "judge" | "paper" | "capital";
export type StationTone = "good" | "bad" | "warn" | "dim";

export interface Station {
  id: StationId;
  name: string;
  path: string;
  /** null while its data loads or when it could not be read; why then says which. */
  tone: StationTone | null;
  word: string;
  why: string;
  rule: string;
  figure: string;
  fact: string;
}

export interface StationInput {
  datasets?: Dataset[];
  strategies?: StrategyListItem[];
  runs?: Run[];
  /** The whole run log's count and its failed runs (the runs summary); the runs list may stop at its newest 500. */
  runsTotal?: number;
  runsFailed?: number;
  judge?: JudgeSummaryResponse;
  paperRunning?: number;
  /** Epoch seconds. */
  now: number;
}

export const STATION_WORD: Record<StationTone, string> = { good: "Works", bad: "Broken", warn: "Waits for you", dim: "Not reached" };

export const STATION_RULE: Record<StationId, string> = {
  data: "Red if a dataset failed its last fetch, amber if a price series is behind (its newest bar is over 7 days old: Refresh waits for you), green if neither.",
  strategies: "Red if a strategy's newest run failed, amber if its newest run finished and was never sent to the judge, green if neither.",
  runs: "Red if the newest run failed, green if it finished or is running.",
  judge: "Red if a graded run got no verdict (a check could not run), amber if a run passed and waits for your approval, green if every graded run got a verdict.",
  paper: "Amber if a passed strategy waits for your approval, green while paper runs trade; grey until a strategy gets here.",
  capital: "Grey until real money is wired: it needs a kill switch, hard limits on the server and your recorded go.",
};

const PATH: Record<StationId, string> = {
  data: "/app/research/data-library",
  strategies: "/app/research/strategies",
  runs: "/app/research/runs",
  judge: "/app/research/judge",
  paper: "/app/research/forward-testing",
  capital: "/app/trading/capital",
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const day = (s: number) => `${new Date(s * 1000).getUTCDate()} ${MONTHS[new Date(s * 1000).getUTCMonth()]}`;
const plural = (n: number, one: string, many: string) => (n === 1 ? one : many);

/** The newest run of each strategy (by when it was made, then by id). */
export function newestRunByStrategy(runs: Run[]): Map<number, Run> {
  const m = new Map<number, Run>();
  for (const r of runs) {
    const had = m.get(r.strategy_id);
    if (!had || r.created_at > had.created_at || (r.created_at === had.created_at && r.id > had.id)) m.set(r.strategy_id, r);
  }
  return m;
}

/** The strategies whose newest run finished and has never been judged: they wait for the judge. */
export function waitingRuns(strategies: StrategyListItem[], runs: Run[]): Run[] {
  const newest = newestRunByStrategy(runs);
  return strategies.map((s) => newest.get(s.id)).filter((r): r is Run => !!r && r.status === "succeeded" && r.verdict == null);
}

export function stations(i: StationInput): Station[] {
  const make = (id: StationId, name: string, figure: string, fact: string, rule: [StationTone, string] | null, loading: string): Station => ({
    id, name, path: PATH[id], figure, fact, rule: STATION_RULE[id],
    tone: rule ? rule[0] : null, word: rule ? STATION_WORD[rule[0]] : "-", why: rule ? rule[1] : loading,
  });
  const out: Station[] = [];

  const ds = i.datasets;
  const failed = ds ? ds.filter((d) => d.status === "failed").length : 0;
  const prices = ds ? ds.filter(isPriceSeries) : [];
  const behind = prices.filter((d) => d.covered_end_ts != null && isStale(d.covered_end_ts, i.now)).length;
  out.push(make("data", "Data", ds ? String(ds.length) : "-", ds ? `${behind} behind` : "", !ds ? null
    : failed > 0 ? ["bad", `${failed} ${plural(failed, "dataset", "datasets")} failed the last fetch.`]
    : ds.length === 0 ? ["dim", "No dataset yet."]
    : behind > 0 ? ["warn", `${behind} of ${prices.length} price series are behind: their newest bar is over 7 days old. None failed its last fetch.`]
    : ["good", "Every dataset is up to date and none failed its last fetch."], "The datasets are loading"));

  const st = i.strategies, runs = i.runs;
  let stRule: [StationTone, string] | null = null, stFact = "";
  if (st && runs) {
    const newest = newestRunByStrategy(runs);
    const broke = st.filter((s) => newest.get(s.id)?.status === "failed").length;
    const waiting = waitingRuns(st, runs).length;
    const judged = st.filter((s) => newest.get(s.id)?.verdict != null).length;
    stFact = `${judged} judged`;
    stRule = broke > 0 ? ["bad", `${broke} ${plural(broke, "strategy's", "strategies'")} newest run failed.`]
      : waiting > 0 ? ["warn", `${waiting} of ${st.length} strategies have a finished run that waits for you to send it to the judge.`]
      : st.length === 0 ? ["dim", "No strategy yet."] : ["good", "Every strategy's newest run has been judged."];
  }
  out.push(make("strategies", "Strategies", st ? String(st.length) : "-", stFact, stRule, "The strategies are loading"));

  let rRule: [StationTone, string] | null = null, rFact = "";
  if (runs) {
    const sorted = [...runs].sort((a, b) => b.created_at - a.created_at || b.id - a.id);
    const top = sorted[0], fails = runs.filter((r) => r.status === "failed"), lastFail = sorted.find((r) => r.status === "failed");
    // The counts come from the whole log when the summary is read; the list alone says how many of the runs it read failed.
    const whole = i.runsFailed != null && i.runsTotal != null;
    rFact = i.runsFailed != null ? `${i.runsFailed} failed` : "";
    const failText = whole ? `${i.runsFailed} of ${i.runsTotal} failed` : `${fails.length} of the ${runs.length} runs read failed`;
    rRule = !top ? ["dim", "No run yet."]
      : top.status === "failed" ? ["bad", `The newest run, ${top.id} of ${day(top.created_at)}, failed${top.error ? `: ${top.error}` : ""}.`]
      : ["good", `The newest run, ${top.id} of ${day(top.created_at)}, ${top.status === "succeeded" ? "finished" : "is running"}. ${failText}${lastFail ? `, the last on ${day(lastFail.created_at)}` : ""}.`];
  }
  out.push(make("runs", "Runs", i.runsTotal != null ? String(i.runsTotal) : "-", rFact, rRule, "The runs are loading"));

  const j = i.judge;
  out.push(make("judge", "Judge", j ? String(j.graded) : "-", j ? `${j.survivors} passed` : "", !j ? null
    : j.graded === 0 ? ["dim", "No run has been sent to the judge yet."]
    : j.unjudgeable > 0 ? ["bad", `${j.unjudgeable} of ${j.graded} graded runs got no verdict: a check could not run because an input was missing.`]
    : j.awaiting_ok > 0 ? ["warn", `${j.awaiting_ok} ${plural(j.awaiting_ok, "run passed and waits", "runs passed and wait")} for your approval.`]
    : ["good", "The judge reached a verdict on every graded run."], "The judge's count is loading"));

  const p = i.paperRunning;
  out.push(make("paper", "Paper", p != null ? String(p) : "-", p ? "on paper" : "nothing on paper", p == null || !j ? null
    : p > 0 ? ["good", `${p} paper ${plural(p, "run is", "runs are")} trading.`]
    : j.awaiting_ok > 0 ? ["warn", `${j.awaiting_ok} passed ${plural(j.awaiting_ok, "strategy waits", "strategies wait")} for your approval to go on paper.`]
    : ["dim", "No strategy has passed the judge and been approved, so nothing has reached paper."], "The paper count is loading"));

  out.push(make("capital", "Capital", "-", "real money is off", ["dim", "Real money is not wired yet."], ""));
  return out;
}
