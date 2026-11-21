// The paper trading page's own typed HTTP calls, kept in this folder rather than growing the
// shared lib/api.ts (night one's own convention, #372 plan section 3.1: apiGet/apiPost are named
// exports of the shared fetch helpers so a page folder can keep its own typed calls).
import { apiGet, apiPost, apiPut } from "../../lib/api";
import type {
  ApproveRealMoneyResponse,
  DisplayBarsResponse,
  FillsResponse,
  HoldingsResponse,
  KillResponse,
  PaperLimitsResponse,
  PaperRun,
  PaperSummary,
  PromoteResponse,
  RetiredStrategy,
  SummaryWindow,
} from "./paperTypes";

// GET /api/paper/runs: every currently running strategy, oldest first. Each row's own "venue"
// field is always "stub" today (no live venue adapter exists, #307): RunCard renders that word
// beside every running card's state word, not only after a Kill is pressed (read_1 finding 1).
export function getRunningRuns(): Promise<PaperRun[]> {
  return apiGet<PaperRun[]>("/api/paper/runs");
}

// GET /api/paper/history: every past paper run, newest ended first, widened (lane PP) with the
// slice, the return fraction, the trade/W-L counts and RETIRED IN PAPER.
export function getHistory(): Promise<PaperRun[]> {
  return apiGet<PaperRun[]>("/api/paper/history");
}

// GET /api/paper/summary?window=: the page's summary strip figures. window selects the span UP OR
// DOWN and TRADES are computed over (brief section 6.2); FEES PAID SO FAR is never window-scoped.
export function getSummary(window: SummaryWindow): Promise<PaperSummary> {
  return apiGet<PaperSummary>(`/api/paper/summary?window=${encodeURIComponent(window)}`);
}

// POST /api/paper/runs/{id}/kill: press Kill on one run. Always a stub-venue close (#307); the
// response's own "venue" field says so, and the card renders it rather than trusting silently.
export function killRun(paperRunId: number): Promise<KillResponse> {
  return apiPost<KillResponse>(`/api/paper/runs/${paperRunId}/kill`, {});
}

// POST /api/paper/runs/{id}/promote: request promotion to real-capital trading for one run. Never
// itself turns real capital on (the Money path law): it only records a request row.
export function promoteRun(paperRunId: number): Promise<PromoteResponse> {
  return apiPost<PromoteResponse>(`/api/paper/runs/${paperRunId}/promote`, {});
}

// The approve route for one run: closes the run out as STOP does, then ends it as
// approved. Called only from the confirm window on the Paper page. It sends no real order.
export function approveForRealMoney(paperRunId: number): Promise<ApproveRealMoneyResponse> {
  return apiPost<ApproveRealMoneyResponse>(`/api/paper/runs/${paperRunId}/approve-real-money`, {});
}

// GET /api/paper/runs/{id}/fills: DETAILS' own data, every fill plus the W/L trade stats
// (paperdb.list_fills + paperdb.trade_stats, merged by the route).
export function getRunFills(paperRunId: number): Promise<FillsResponse> {
  return apiGet<FillsResponse>(`/api/paper/runs/${paperRunId}/fills`);
}

// GET /api/paper/retired: every strategy RETIRED IN PAPER (#376 ruling 5).
export function getRetired(): Promise<RetiredStrategy[]> {
  return apiGet<RetiredStrategy[]>("/api/paper/retired");
}

// GET /api/display/bars: DETAILS' own chart bars for one ticker, display-only (lane DL's route,
// dashboard/backend/routes/display.py). Called directly here rather than through
// ../../lib/api.ts's own api.display.getBars, because that shared helper does not (and, outside
// this lane's declared area, cannot here) carry the optional `start` param display.py gained in
// this same build (plan section 9 item 2: "display.py may gain an optional bounded start").
export function getDisplayBars(symbol: string, start?: string): Promise<DisplayBarsResponse> {
  const qs = new URLSearchParams({ symbol, timeframe: "1Hour" });
  if (start) qs.set("start", start);
  return apiGet<DisplayBarsResponse>(`/api/display/bars?${qs.toString()}`);
}

// GET /api/paper/runs/{id}/holdings: what one run holds, marked at Alpaca's latest IEX trade.
export function getHoldings(paperRunId: number): Promise<HoldingsResponse> {
  return apiGet<HoldingsResponse>(`/api/paper/runs/${paperRunId}/holdings`);
}

// GET /api/paper/limits: the six account-wide paper limits.
export function getPaperLimits(): Promise<PaperLimitsResponse> {
  return apiGet<PaperLimitsResponse>("/api/paper/limits");
}

// PUT /api/paper/limits: set any subset of the six; a refusal is a 422 whose detail starts with its word.
export function putPaperLimits(values: Record<string, number>): Promise<PaperLimitsResponse> {
  return apiPut<PaperLimitsResponse>("/api/paper/limits", values);
}
