// The Strategies page's own read, over the shared fetch helper in lib/api.ts. include_synthetic is
// never sent, so the server hides synthetic strategies' runs (CLAUDE.md, synthetic data stays out of
// research views). The key is its own: runsApi's ["strategies", "list"] holds a different shape.
import { api, apiGet, apiPost, apiPut } from "../../lib/api";

export const strategiesKeys = {
  overview: ["strategies", "overview"] as const,
  parameters: (id: number) => ["strategies", "parameters", id] as const,
  runs: (id: number) => ["strategies", "runs", id] as const,
  versions: (id: number) => ["strategies", "versions", id] as const,
  settings: (id: number) => ["strategies", "settings", id] as const,
  source: (id: number) => ["strategies", "source", id] as const,
  supply: ["strategies", "reserve-supply"] as const,
  retest: (runId: number) => ["strategies", "retest", runId] as const,
  fills: (retestId: number) => ["strategies", "retest-fills", retestId] as const,
};

// The newest real run of a strategy, as GET /api/research/strategies/overview serialises it.
export type StrategyOverviewRun = {
  id: number;
  created_at: string;
  strategy_version: number;
  equity_sparkline: number[] | null;
  sharpe_ratio: number | null;
  num_trades: number | null;
  total_return: number | null;
  max_drawdown: number | null;
};

export type StrategyOverviewRow = {
  id: number;
  name: string;
  display_name: string | null;
  version: number;
  description: string | null;
  run: StrategyOverviewRun | null;
};

export function fetchStrategiesOverview(): Promise<StrategyOverviewRow[]> {
  return apiGet<StrategyOverviewRow[]>("/api/research/strategies/overview");
}

// Where a parameter's number comes from: a line of the provenance file (a standard strategy) or of
// ranges.toml (a theory pick, with its one-line reason).
export type ParameterSource = { file: string; key: string; line: number; why?: string };

export type StrategyParameter = {
  name: string;
  range: string | null;
  value: unknown;
  source: ParameterSource | null;
};

export type StrategyParameters = {
  strategy_id: number;
  kind: "standard" | "theory";
  run_id: number | null;
  parameters: StrategyParameter[];
};

export function fetchStrategyParameters(id: number): Promise<StrategyParameters> {
  return apiGet<StrategyParameters>(`/api/research/strategies/${id}/parameters`);
}

// One strategy's runs, newest first, as GET /api/research/strategies/{id}/runs serialises them. The
// server caps one answer at 500 and hides synthetic runs unless include_synthetic is sent, which this
// page never does.
export const RUNS_AT_MOST = 500;

export type StrategyRun = {
  id: number;
  created_at: number;
  status: string;
  sharpe_ratio: number | null;
  num_trades: number | null;
  total_return: number | null;
  equity_sparkline: number[] | null;
};

export function fetchStrategyRuns(id: number): Promise<StrategyRun[]> {
  return apiGet<StrategyRun[]>(`/api/research/strategies/${id}/runs?limit=${RUNS_AT_MOST}`);
}

// A rename sends the stored source back unchanged with the new name, so the server records the same
// version under the typed display name.
// The same read gives the strategy's declared sizing method (its registry id).
// One strategy's current row, as GET /api/research/strategies/{id} serialises it.
export type StrategySource = {
  id: number;
  name: string;
  display_name: string | null;
  source_code: string;
  description: string | null;
  version: number;
  status: string;
  sizing_method: string;
};

export function fetchStrategySource(id: number): Promise<StrategySource> {
  return apiGet<StrategySource>(`/api/research/strategies/${id}`);
}

// One saved version of a strategy, newest first; edited_at is epoch seconds.
export type StrategyVersion = { version: number; description: string | null; edited_at: number };

export function fetchStrategyVersions(id: number): Promise<StrategyVersion[]> {
  return apiGet<StrategyVersion[]>(`/api/research/strategies/${id}/versions`);
}

// One setting as the strategy resolves it: its own override or the inherited global. The server
// answers 404 for a deleted strategy.
export type StrategySetting = {
  scope: string;
  key: string;
  value: unknown;
  value_type: string;
  unit: string | null;
  label: string | null;
  source: "override" | "inherited";
  global_value: unknown;
  overridable: boolean;
  source_note: string | null;
};

export function fetchStrategySettings(id: number): Promise<StrategySetting[]> {
  return apiGet<StrategySetting[]>(`/api/research/strategies/${id}/settings`);
}

// One stored version's code, for reading in the editor's Versions window.
export type StrategyVersionCode = { source_code: string; description: string | null };

export function fetchStrategyVersion(id: number, version: number): Promise<StrategyVersionCode> {
  return apiGet<StrategyVersionCode>(`/api/research/strategies/${id}/versions/${version}`);
}

// SET in the Settings window: the typed text is the value (the server turns it into the setting's
// kind and checks its bounds), with the optional reason.
export function setStrategySetting(
  id: number, scope: string, key: string, value: string, sourceNote: string | null,
): Promise<{ ok: boolean }> {
  return apiPut<{ ok: boolean }>(
    `/api/research/strategies/${id}/settings/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
    { value, source_note: sourceNote },
  );
}

// CLEAR in the Settings window: the strategy goes back to the global value.
export function clearStrategySetting(id: number, scope: string, key: string): Promise<{ ok: boolean }> {
  return api.research.deleteStrategyOverride(id, scope, key);
}

export type RenameAnswer = { id: number; display_name: string | null; version: number; action: string };

export function renameStrategy(id: number, sourceCode: string, displayName: string): Promise<RenameAnswer> {
  return apiPut<RenameAnswer>(`/api/research/strategies/${id}`, { source_code: sourceCode, display_name: displayName });
}

// SAVE in the editor: the next version of the code, or "unchanged" when it equals the stored one.
export type SaveAnswer = {
  id: number;
  name: string;
  display_name: string | null;
  version: number;
  action: "updated" | "unchanged" | "renamed";
  mirror_status: string;
};

export function saveStrategyCode(id: number, sourceCode: string): Promise<SaveAnswer> {
  return apiPut<SaveAnswer>(`/api/research/strategies/${id}`, { source_code: sourceCode });
}

// The first SAVE of a new strategy: the server makes it version 1 under a stored name nobody else
// (a deleted strategy included) holds.
export type CreateAnswer = {
  id: number;
  name: string;
  display_name: string | null;
  version: number;
  action: string;
  mirror_status: string;
};

export function createStrategy(name: string, displayName: string, sourceCode: string): Promise<CreateAnswer> {
  return apiPost<CreateAnswer>("/api/research/strategies", { name, display_name: displayName, source_code: sourceCode });
}

// VALIDATE in the editor: the same check as SAVE, nothing written.
export type ValidateAnswer = { ok: boolean; description: string };

export function validateStrategyCode(sourceCode: string): Promise<ValidateAnswer> {
  return apiPost<ValidateAnswer>("/api/research/strategies/validate", { source_code: sourceCode });
}

export function deleteStrategy(id: number) {
  return api.research.deleteStrategy(id);
}

// The reserve exam's stretches of history and which ones a verdict has already used.
export type ReserveWindow = { index: number; name: string; start_ts: number; end_ts: number; spent: boolean };
export type ReserveSupply = { enumerated: number; spent: number; windows: ReserveWindow[] };

export function fetchReserveSupply(): Promise<ReserveSupply> {
  return apiGet<ReserveSupply>("/api/research/reserve-exam/supply");
}

export type ExamStarted = {
  exam: { run_id: number; window_start_ts: number; window_end_ts: number };
  window_name: string;
};

export function takeReserveExam(strategyId: number, seed: number): Promise<ExamStarted> {
  return apiPost<ExamStarted>("/api/research/reserve-exam/dispatch", { strategy_id: strategyId, seed });
}

// The second-engine retest of one run: the state word and every record, oldest first.
export type RetestRow = {
  id: number;
  status: string;
  could_not_run_reason: string | null;
  export_path: string | null;
  gap_final_equity: number | null;
  gap_sharpe: number | null;
};

export type RetestDetail = { state: string; retests: RetestRow[] };

export function fetchRetest(runId: number): Promise<RetestDetail> {
  return apiGet<RetestDetail>(`/api/research/retests/runs/${runId}`);
}

export function startRetest(runId: number): Promise<{ job_id: number }> {
  return apiPost<{ job_id: number }>(`/api/research/retests/runs/${runId}/retest`, {});
}

export type RetestFill = {
  signal_date: number;
  side: string;
  real_fill_price: number;
  shadow_fill_price: number;
  vbt_fill_price: number | null;
  diff_vbt: number | null;
};

export function fetchRetestFills(retestId: number): Promise<{ fills: RetestFill[] }> {
  return apiGet<{ fills: RetestFill[] }>(`/api/research/retests/${retestId}/fills`);
}

// REPLAY of the judged cell: "replay-exists" when a saved replay is already there, "replay-queued"
// when the server queued one.
export function replayResult(runId: number, variationId: number, ticker: string) {
  return api.research.replayCell(runId, variationId, ticker);
}
