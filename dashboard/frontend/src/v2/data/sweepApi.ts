// The calls behind the Run a sweep window (lane SW1). Every call goes through lib/api. The stage type
// has no "reserve": both routes accept it, but the reserve is used only by the reserve exam (#302), so
// no body built here can carry it.
import { api, apiPost } from "../../lib/api";
import type { Dataset, StrategyStatus } from "../../types/research";

export const sweepKeys = {
  lists: ["sweep", "lists"] as const,
  params: (id: number) => ["sweep", "params", id] as const,
};

export type SweepStage = "screen" | "optimise";
export type SweepGrid = Record<string, number[]>;

/** What both start routes answer on 201. */
export type SweepStarted = {
  id: number;
  run_id: number;
  status: string;
  registered_n: number;
  member_count: number;
  all_members_ready: boolean;
  finalized: boolean;
};

export type SweepParams = Pick<StrategyStatus, "sweep_params" | "param_mode">;

/** The stock lists: active datasets, no synthetic ones. */
export function fetchStockLists(): Promise<Dataset[]> {
  return api.research.listDatasets("active");
}

export async function fetchSweepParams(id: number): Promise<SweepParams> {
  const s = await api.research.getStrategyStatus(id);
  return { sweep_params: s.sweep_params, param_mode: s.param_mode };
}

export function saveListWindow(id: number, start: number | null, end: number | null) {
  return api.research.updatePriceUniverseWindow(id, start, end);
}

export function startSweep(
  universeId: number,
  body: { strategy_id: number; param_grid: SweepGrid; name: string; stage: SweepStage },
): Promise<SweepStarted> {
  return apiPost<SweepStarted>(`/api/research/price-universes/${universeId}/dispatch`, body);
}

export function startWalkForward(body: {
  strategy_id: number;
  universe_id: number;
  stage: "walkforward";
  calendar_id: "real";
  param_grid: SweepGrid;
}): Promise<SweepStarted> {
  return apiPost<SweepStarted>("/api/research/stage-runner/dispatch", body);
}
