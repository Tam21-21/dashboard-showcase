// The Runs page's own calls, over the shared api helper in lib/api.ts. Read-only except
// sendToJudge. Synthetic runs are never asked for: listRuns sends no include_synthetic argument,
// so the server hides them (CLAUDE.md, synthetic data stays out of research views).
import { api } from "../../lib/api";

export const RUNS_LIMIT = 500;

export const runsKeys = {
  list: ["runs", "list"] as const,
  summary: ["runs", "summary"] as const,
  strategies: ["strategies", "list"] as const,
};

export function fetchRuns() {
  return api.research.listRuns("created_at", RUNS_LIMIT, 0, "standard");
}

export function fetchRunsSummary() {
  return api.research.runsSummary("standard");
}

export function fetchStrategies() {
  return api.research.listStrategies();
}

export function sendToJudge(runId: number) {
  return api.research.judgeRun(runId);
}
