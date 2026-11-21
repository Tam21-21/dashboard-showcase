import { describe, expect, it } from "vitest";
import { newestRunByStrategy, stations, waitingRuns, type StationInput } from "../data/stations";
import type { JudgeSummaryResponse } from "../data/judgeApi";
import type { Dataset, Run, StrategyListItem } from "../../types/research";

// The station colours (Tamer 2026-11-14): green works, red broken, amber waits for him, grey not reached.
const NOW = Date.parse("2026-11-14T12:00:00Z") / 1000;
const DAY = 86_400;
const ds = (id: number, status: string, ageDays: number | null, dataset_type = "price_dataset") =>
  ({ id, status, dataset_type, covered_end_ts: ageDays == null ? null : NOW - ageDays * DAY }) as unknown as Dataset;
const strat = (id: number) => ({ id, name: `s${id}` }) as unknown as StrategyListItem;
const run = (id: number, strategy_id: number, ageDays: number, status: string, verdict: string | null = null, error: string | null = null) =>
  ({ id, strategy_id, created_at: NOW - ageDays * DAY, status, verdict, error }) as unknown as Run;
const judge = (o: Partial<JudgeSummaryResponse> = {}) =>
  ({ store: { table_present: true, row_count: 0 }, graded: 4, survivors: 0, awaiting_ok: 0, in_paper: 0, unjudgeable: 0, ...o }) as JudgeSummaryResponse;
const at = (i: Partial<StationInput>, id: string) => stations({ now: NOW, ...i }).find((s) => s.id === id)!;

describe("stations", () => {
  it("are six, in order, each with its page", () => {
    expect(stations({ now: NOW }).map((s) => [s.id, s.path])).toEqual([
      ["data", "/app/research/data-library"], ["strategies", "/app/research/strategies"], ["runs", "/app/research/runs"],
      ["judge", "/app/research/judge"], ["paper", "/app/research/forward-testing"], ["capital", "/app/trading/capital"],
    ]);
  });

  it("have no colour while their data loads, except capital", () => {
    const list = stations({ now: NOW });
    expect(list.filter((s) => s.tone === null).map((s) => s.id)).toEqual(["data", "strategies", "runs", "judge", "paper"]);
    expect(list.find((s) => s.id === "capital")!.tone).toBe("dim");
  });

  it("data: red on a failed fetch, amber when one is over 7 days old, green otherwise", () => {
    expect(at({ datasets: [ds(1, "ready", 1), ds(2, "failed", 1)] }, "data").tone).toBe("bad");
    const behind = at({ datasets: [ds(1, "ready", 1), ds(2, "ready", 8)] }, "data");
    expect(behind.tone).toBe("warn");
    expect(behind.fact).toBe("1 behind");
    expect(at({ datasets: [ds(1, "ready", 1), ds(2, "ready", null)] }, "data").tone).toBe("good");
    expect(at({ datasets: [] }, "data").tone).toBe("dim");
  });

  it("data: behind counts price series only, as the Datasets tab does", () => {
    // An old universe or event feed has its own freshness; it never makes the price series look behind.
    const s = at({ datasets: [ds(1, "ready", 1), ds(2, "ready", 30, "price_universe"), ds(3, "ready", 30, "event_series"), ds(4, "ready", 9)] }, "data");
    expect(s.fact).toBe("1 behind");
    expect(s.why).toBe("1 of 2 price series are behind: their newest bar is over 7 days old. None failed its last fetch.");
    expect(at({ datasets: [ds(1, "ready", 1), ds(2, "ready", 30, "price_universe")] }, "data").tone).toBe("good");
  });

  it("strategies: red when a newest run failed, amber when one waits for the judge, green when all are judged", () => {
    const st = [strat(1), strat(2)];
    expect(at({ strategies: st, runs: [run(1, 1, 3, "succeeded", "fail"), run(2, 1, 1, "failed")] }, "strategies").tone).toBe("bad");
    expect(at({ strategies: st, runs: [run(1, 1, 3, "succeeded", "fail"), run(2, 2, 1, "succeeded")] }, "strategies").tone).toBe("warn");
    expect(at({ strategies: st, runs: [run(1, 1, 3, "succeeded", "fail"), run(2, 2, 1, "succeeded", "pass")] }, "strategies").tone).toBe("good");
    expect(at({ strategies: [], runs: [] }, "strategies").tone).toBe("dim");
  });

  it("runs: red when the newest run failed, with its error; green when it finished or runs", () => {
    const bad = at({ runs: [run(1, 1, 3, "succeeded"), run(2, 1, 1, "failed", null, "No data")] }, "runs");
    expect(bad.tone).toBe("bad");
    expect(bad.why).toContain("failed: No data.");
    expect(at({ runs: [run(1, 1, 3, "failed"), run(2, 1, 1, "running")] }, "runs").tone).toBe("good");
    expect(at({ runs: [] }, "runs").tone).toBe("dim");
    expect(at({ runs: [run(1, 1, 1, "succeeded")], runsTotal: 52 }, "runs").figure).toBe("52");
  });

  it("runs: the figure and the failed count are the whole log's; without the summary they are not guessed from the list", () => {
    const list = [run(1, 1, 3, "failed"), run(2, 1, 1, "succeeded")];
    const whole = at({ runs: list, runsTotal: 812, runsFailed: 112 }, "runs");
    expect([whole.figure, whole.fact]).toEqual(["812", "112 failed"]);
    expect(whole.why).toContain("112 of 812 failed");
    const listOnly = at({ runs: list }, "runs");
    expect([listOnly.figure, listOnly.fact]).toEqual(["-", ""]);
    expect(listOnly.why).toContain("1 of the 2 runs read failed");
  });

  it("judge: grey before any grading, red on a missing verdict, amber when a pass waits, green otherwise", () => {
    expect(at({ judge: judge({ graded: 0 }) }, "judge").tone).toBe("dim");
    expect(at({ judge: judge({ unjudgeable: 1, awaiting_ok: 1 }) }, "judge").tone).toBe("bad");
    expect(at({ judge: judge({ awaiting_ok: 1 }) }, "judge").tone).toBe("warn");
    expect(at({ judge: judge() }, "judge").tone).toBe("good");
  });

  it("paper: green while runs trade, amber when a pass waits for approval, grey otherwise", () => {
    expect(at({ judge: judge(), paperRunning: 2 }, "paper").tone).toBe("good");
    expect(at({ judge: judge({ awaiting_ok: 1 }), paperRunning: 0 }, "paper").tone).toBe("warn");
    expect(at({ judge: judge(), paperRunning: 0 }, "paper").tone).toBe("dim");
    expect(at({ paperRunning: 0 }, "paper").tone).toBeNull();
  });

  it("the newest run of a strategy is the latest made, then the highest id", () => {
    const m = newestRunByStrategy([run(1, 1, 2, "succeeded"), run(3, 1, 1, "succeeded"), run(2, 1, 1, "succeeded"), run(4, 2, 5, "failed")]);
    expect(m.get(1)!.id).toBe(3);
    expect(m.get(2)!.id).toBe(4);
  });

  it("a strategy waits for the judge only when its newest run finished and has no verdict", () => {
    const st = [strat(1), strat(2), strat(3)];
    const w = waitingRuns(st, [run(1, 1, 1, "succeeded"), run(2, 2, 1, "running"), run(3, 3, 2, "succeeded"), run(4, 3, 1, "succeeded", "fail")]);
    expect(w.map((r) => r.id)).toEqual([1]);
  });
});
