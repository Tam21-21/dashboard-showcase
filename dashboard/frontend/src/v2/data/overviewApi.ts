// The Research overview's own calls. Everything here is read-only. The runs, datasets, verdicts and
// judge summary routes hide synthetic data unless asked, and these calls never ask; strategies and
// paper have no such filter (paper refuses synthetic data when a run is admitted).
// /api/ops/signals is never read: it counts synthetic rows.
import { api } from "../../lib/api";
import { fetchJudgeSummary, fetchJudgeVerdicts } from "./judgeApi";
import { getSystems } from "./opsApi";
import { getRunningRuns, getSummary } from "./paperApi";
import { fetchRuns, fetchRunsSummary, fetchStrategies } from "./runsApi";

export const VERDICTS_LIMIT = 500;

export const overviewKeys = {
  datasets: ["overview", "datasets"] as const,
  strategies: ["overview", "strategies"] as const,
  runs: ["overview", "runs"] as const,
  runsSummary: ["overview", "runs-summary"] as const,
  judgeSummary: ["overview", "judge-summary"] as const,
  verdicts: ["overview", "verdicts"] as const,
  paperSummary: ["overview", "paper-summary"] as const,
  paperRuns: ["overview", "paper-runs"] as const,
  systems: ["overview", "systems"] as const,
};

export function fetchDatasets() {
  return api.research.listDatasets("active");
}

export function fetchVerdicts() {
  return fetchJudgeVerdicts(VERDICTS_LIMIT, 0);
}

export function fetchPaperSummary() {
  return getSummary("since_start");
}

export function fetchSystems() {
  return getSystems();
}

export { fetchJudgeSummary, fetchRuns, fetchRunsSummary, fetchStrategies, getRunningRuns as fetchPaperRuns };
