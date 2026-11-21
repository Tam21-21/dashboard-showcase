// The Generator page's own calls, over apiGet and apiPost of lib/api.ts. Every query key sits under
// ["generator"], so one invalidation refetches the whole page. Shapes follow
// dashboard/backend/routes/generator_v3.py (get_templates, post_template, get_batches, get_tree,
// get_trials_per_day) and routes/generator.py get_generator_worker_status.
import { apiDelete, apiGet, apiPost } from "../../lib/api";

export const V3 = "/api/research/generator/v3";

export const generatorKeys = {
  all: ["generator"] as const,
  templates: ["generator", "templates"] as const,
  batches: ["generator", "batches"] as const,
  tree: (id: number) => ["generator", "tree", id] as const,
  worker: ["generator", "worker"] as const,
  trials: ["generator", "trials"] as const,
};

export interface GenTemplate {
  name: string;
  display_name: string;
  sha256: string;
  saved_at: number;
  theory_count: number;
}

export interface SavedTemplate {
  name: string;
  display_name: string;
  sha256: string;
  created: boolean;
}

export interface GenControl {
  state: "running" | "paused" | "stopped";
  since: number | null;
}

export interface GenBatch {
  id: number;
  template_sha256: string;
  universe_id: number;
  searched_universe_id: number;
  group_label: string;
  display_name: string;
  members: string[];
  kept_back: string[];
  seed: number;
  data_start: string;
  screen_start: string;
  screen_end: string;
  optimise_start: string;
  optimise_end: string;
  later_start: string;
  later_end: string;
  calendar_id: string;
  approved_at: number;
  control: GenControl;
  removed_at: number | null;
  synthetic: boolean;
  walkforward: [string, string];
}

export interface GenRun {
  stage: string;
  run_id: number;
  status: string;
}

export interface GenSaved {
  rank: number;
  display_name: string;
  picks: unknown;
  verdict: { word: string; id: number } | null;
  refusal: string | null;
}

export interface GenSearch {
  rounds: {
    rounds: Array<{ round: number; valid_strategies: number; ran: number; strategies: unknown; new_trials: number; best_trial_ids: unknown }>;
    quality: unknown;
    stop: unknown;
  };
  finalists: unknown[];
  pick: { picks: unknown; finalist_rank: number; optimise_sharpe: number; reason: string };
}

export interface GenFinal {
  kept_back: unknown;
  later_window: unknown;
  strategies: Array<{ rank: number; held_up: boolean }>;
}

export interface GenTheory {
  id: number;
  name: string;
  display_name: string;
  choices: unknown;
  runs: GenRun[];
  search: GenSearch | null;
  final: "not run" | GenFinal;
  saved: GenSaved[];
  stop: { step: string; reason: string } | null;
}

export interface GenTree {
  batch: GenBatch;
  template: { name: string; display_name: string; sha256: string };
  theories: GenTheory[];
}

export interface TrialsDay {
  day: string;
  trials: number;
  searches: number;
}

export interface TrialsPerDay {
  time_zone: string;
  today: string;
  days: TrialsDay[];
}

export function fetchTemplates() {
  return apiGet<GenTemplate[]>(`${V3}/templates`);
}

export function saveTemplate(content: string) {
  return apiPost<SavedTemplate>(`${V3}/templates`, { content });
}

// Never asks for synthetic batches: the page shows real research only.
export function fetchBatches() {
  return apiGet<GenBatch[]>(`${V3}/batches`);
}

export function fetchTree(id: number) {
  return apiGet<GenTree>(`${V3}/batches/${id}/tree`);
}

export function fetchWorker() {
  return apiGet<{ status: "online" | "offline" }>("/api/research/generator/worker");
}

export function fetchTrialsPerDay() {
  return apiGet<TrialsPerDay>(`${V3}/trials-per-day`);
}

// ---- start a theory (GV3-P1b) ----------------------------------------------------------------------------------------

export const groupsKey = ["generator", "groups"] as const;

export interface StockGroup {
  id: number;
  name: string;
  display_name: string | null;
  needs_name: boolean;
  member_count: number | null;
}

// The stock groups a theory can run on: price universes only, from the datasets listing (which hides made-up data).
export async function fetchStockGroups(): Promise<StockGroup[]> {
  const rows = await apiGet<Array<{ id: number; name: string; dataset_type: string; display_name: string | null; needs_name: boolean; member_count: number | null }>>(
    "/api/research/datasets?status=active",
  );
  return rows
    .filter((r) => r.dataset_type === "price_universe")
    .map((r) => ({ id: r.id, name: r.name, display_name: r.display_name, needs_name: r.needs_name, member_count: r.member_count }));
}

export interface RoundOneRange {
  count: number;
  from: number;
  to: number;
  step: number;
}

export interface PreviewTheory {
  name: string;
  display_name: string;
  choices: unknown;
  letters: string[];
  values_per_number: number | null;
  round_one: Record<string, RoundOneRange> | null;
}

export type IsoWindow = [string, string];

export interface GenPreview {
  template: { name: string; display_name: string; sha256: string };
  theories: PreviewTheory[];
  stocks: { searched: string[]; kept_back: string[] };
  years: { screen: IsoWindow; optimise: IsoWindow; walkforward: IsoWindow; later: IsoWindow };
  run_time_estimate: null;
}

export interface PreviewBody {
  universe_id: number;
  seed: number | string | null;
  optimise_window: [string | null, string | null];
  later_window: [string | null, string | null];
}

export interface ApproveBody extends PreviewBody {
  group_label: string;
  data_start: string;
  calendar_id: string;
}

export interface Approved {
  batch_id: number;
  run_ids: number[];
}

export function previewTemplate(sha: string, body: PreviewBody) {
  return apiPost<GenPreview>(`${V3}/templates/${sha}/preview`, body);
}

export function approveTemplate(sha: string, body: ApproveBody) {
  return apiPost<Approved>(`${V3}/templates/${sha}/approve`, body);
}

// ---- pause, resume and stop a batch (GV3-P2a) ------------------------------------------------------------------------

export interface ResumeAnswer {
  batch_id: number;
  state: "running";
  run_ids: number[];
}

export interface PauseAnswer {
  batch_id: number;
  state: "paused";
  since: number;
}

export interface DeleteAnswer {
  batch_id: number;
  removed_at: number;
}

export interface StopAnswer {
  batch_id: number;
  state: "stopped";
  since: number;
  cancelled_runs: number[];
  still_running: number[];
}

export function resumeBatch(id: number) {
  return apiPost<ResumeAnswer>(`${V3}/batches/${id}/resume`, {});
}

export function pauseBatch(id: number) {
  return apiPost<PauseAnswer>(`${V3}/batches/${id}/pause`, {});
}

export function stopBatch(id: number) {
  return apiPost<StopAnswer>(`${V3}/batches/${id}/stop`, {});
}

export function deleteBatch(id: number) {
  return apiDelete<DeleteAnswer>(`${V3}/batches/${id}`);
}
