// Typed calls for the judge page, over the shared fetch helpers in lib/api.ts (apiGet/apiPost).
// Kept beside the page rather than growing lib/api.ts's own object, matching the night-one
// pre-dispatch commit's own reasoning for exporting apiGet/apiPost in the first place: "each page
// keeps its typed calls in its own folder and the shared helper stays shared."
import { apiGet, apiPost } from "../../lib/api";

// One check_outcomes entry as dashboard/backend/judgedb.py stores it. Originally a bare
// {check, outcome} pair (architecture section 2); WIDENED by lane QR (`6.1f`, set 6 day plan
// section 7 criterion 1) to also carry the check's own "quantities" dict and "reason" string.
// Both new fields are OPTIONAL: a row graded before this lane's own fix carries neither key at
// all, absent by omission (never {} or ""), and every reader must treat that omission as "this
// check never recorded a figure or a reason" rather than crash on a missing field.
export type CheckOutcomeEntry = {
  check: number;
  outcome: string;
  // quantities: the check's own dict (judge_checks.CheckResult.quantities). Values are whatever
  // that check computed: a number, a string (check 6's own "method": "t"/"block"), a boolean, or
  // null -- the sanitiser's own stand-in for a non-finite float (NaN, +/-infinity) the check
  // computed but which JSON itself cannot represent.
  quantities?: Record<string, number | string | boolean | null>;
  // reason: the check's own plain-English sentence fragment (CheckResult.reason).
  reason?: string;
};

// One row of research_judge_verdicts as the router (dashboard/backend/routes/judge.py) serialises
// it: every column of the real table, with the two JSON columns already parsed into lists, the
// strategy's name joined in, and "current" computed server-side (architecture section 2b item 3).
export type JudgeVerdictRow = {
  id: number;
  run_id: number;
  judging_pass_id: string;
  variation_id: number;
  ticker: string;
  graded_formula_version: string;
  strategy_id: number;
  strategy_version: number;
  dataset_sha256: string;
  verdict: "pass" | "fail" | "unjudgeable";
  is_advisory: boolean;
  battery_version: string;
  evidence_floor_outcome: string;
  check_outcomes: CheckOutcomeEntry[];
  decided_by: string;
  warnings: string[];
  graded_at: number;
  current: boolean;
  strategy_name: string;
  // Set 5 day, lane JP: trials from research_run_cells; the four run-stats figures survivor cards
  // read, each an honest null when research_run_stats has no row for this run.
  trials: number;
  total_return: number | null;
  max_drawdown: number | null;
  num_trades: number | null;
  sharpe_ratio: number | null;
  // DBG-17: true when research_verdict_rejections holds a row for this verdict; rejected_at is that
  // row's press time (ISO-8601 UTC), null when the verdict was never rejected.
  rejected: boolean;
  rejected_at: string | null;
  // Lane UI8b-0: each check's figures against their bars (judge_display.check_measures), nine
  // entries in check order, and the capital the run was judged with (what approval sends).
  measures: JudgeCheckMeasures[];
  judged_capital: number;
};

// One figure a check graded. value and bar are null when the judge stored none; comparison and
// units are null when no bar is stored.
export type JudgeMeasure = {
  key: string;
  label: string;
  value: number | null;
  bar: number | null;
  comparison: "at_or_above" | "strictly_above" | "at_or_below" | "strictly_below" | null;
  units: string | null;
};

export type JudgeCheckMeasures = {
  check: number;
  outcome: string | null;
  measures: JudgeMeasure[];
  // The plain reason a check has no figure (judge_display.check_measures), null when it has none.
  why: string | null;
};

export type JudgeStoreStatus = {
  table_present: boolean;
  row_count: number;
};

export type JudgeVerdictsResponse = {
  store: JudgeStoreStatus;
  rows: JudgeVerdictRow[];
};

// WIDENED (set 5 day, lane JP): five numbers now, not four. "failed" is dropped from the strip
// (RECENTLY GRADED's own STOPPED BY column carries a failed check's name instead); awaiting_ok and
// in_paper are new, computed server-side over paper.db as well as research.db.
export type JudgeSummaryResponse = {
  store: JudgeStoreStatus;
  graded: number;
  survivors: number;
  awaiting_ok: number;
  in_paper: number;
  unjudgeable: number;
};

// One frozen pass bar for a check, as GET /api/judge/thresholds serialises it.
export type JudgeThresholdBar = {
  quantity_key: string;
  value_text: string;
  source_book: string;
  source_folio: string;
  source_quote: string;
};

export type JudgeThresholdsResponse = {
  battery_version: string;
  // Keyed by check number as a string (the route's own JSON object shape); "1" is always {bars:[]}.
  checks: Record<string, { bars: JudgeThresholdBar[] }>;
};

// The single verdict row FULL RESULT reads: the same shape as a list row (GET /verdicts/{id}
// serialises through the identical _serialise_row on the backend).
export type JudgeVerdictDetail = JudgeVerdictRow;

// GET /api/judge/verdicts. limit/offset default to the route's own defaults; this page always
// asks for one page's worth (the RECENTLY GRADED table is not paginated in this build).
export function fetchJudgeVerdicts(limit = 100, offset = 0): Promise<JudgeVerdictsResponse> {
  return apiGet<JudgeVerdictsResponse>(`/api/judge/verdicts?limit=${limit}&offset=${offset}`);
}

// GET /api/judge/verdicts/{id}: FULL RESULT's own read.
export function fetchJudgeVerdict(verdictId: number): Promise<JudgeVerdictDetail> {
  return apiGet<JudgeVerdictDetail>(`/api/judge/verdicts/${verdictId}`);
}

// GET /api/judge/summary, the fact strip's five numbers.
export function fetchJudgeSummary(): Promise<JudgeSummaryResponse> {
  return apiGet<JudgeSummaryResponse>("/api/judge/summary");
}

// GET /api/judge/thresholds, PASS RATES' own hover source.
export function fetchJudgeThresholds(): Promise<JudgeThresholdsResponse> {
  return apiGet<JudgeThresholdsResponse>("/api/judge/thresholds");
}

// POST /api/judge/verdicts/{id}/reject. reason is optional free text.
export function rejectVerdict(verdictId: number, reason?: string): Promise<{ rejection_id: number; verdict_id: number }> {
  return apiPost(`/api/judge/verdicts/${verdictId}/reject`, reason ? { reason } : {});
}

// POST /api/judge/verdicts/{id}/approve. Throws ApiError on any of the four named refusals; the
// caller reads ApiError.body.detail for the server's own refusal sentence (frontend map trap 7:
// never render ApiError.message raw, it carries the request URL).
export function approveVerdictForPaper(
  verdictId: number,
  sliceOpeningCash: number,
): Promise<{ paper_run_id: number; verdict_id: number }> {
  return apiPost(`/api/judge/verdicts/${verdictId}/approve`, { slice_opening_cash: sliceOpeningCash });
}
