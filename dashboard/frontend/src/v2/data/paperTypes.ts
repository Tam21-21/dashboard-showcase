// The paper trading page's own types, matching dashboard/backend/routes/paper.py's JSON shapes
// byte for byte (field names copied from that file's own response dicts, never guessed).

// A [timestamp, equity] point from the paper_equity series, capped at the newest 500 by the route.
export type EquityPoint = [string, number];

// The judged backtest's Monte Carlo bounds (#215), parsed from expected_bounds_json. The shape is
// whatever the judge stored; only "low" and "high" are read here because that is all section 7
// asks the card to show (EXPECTED beside EFFECTIVE).
export type ExpectedBounds = {
  low: number;
  high: number;
  [key: string]: unknown;
};

// paper_slice_current, exactly as the route serialises it.
export type PaperSlice = {
  cash: number;
  position_value: number;
  equity: number;
  realised_pnl: number;
  committed_capital: number;
  updated_at: string;
};

// The newest paper_promotions row for a run, or null when it has never asked to promote.
export type PromotionRequest = {
  promotion_id: number;
  requested_at: string;
  state: "requested" | "granted" | "declined" | "withdrawn";
  decided_at: string | null;
  decision_ledger_entry: string | null;
};

// The win/loss counts a running or ended run's own realised closes produced (night plan
// 2026-09-12, set 5 day, lane PP: paperdb.trade_stats makes this real instead of the placeholder
// `null` night one shipped). Always real integers, 0 on a run with no closing sell fill yet.
export type RealisedResultCounts = { win_count: number; loss_count: number };

// One row from GET /api/paper/runs (a running strategy) or GET /api/paper/history (an ended one).
// ended_at / end_reason are null on a running run and set on an ended one; slice / equity_series /
// latest_promotion / realised_result_counts / venue are ONLY present on the /runs shape, never on
// /history's plain paper_runs rows, so they are optional here rather than assumed present.
export type PaperRun = {
  paper_run_id: number;
  strategy_id: number;
  strategy_name: string;
  strategy_version: number;
  source_run_id: number;
  slice_opening_cash: number;
  expected_bounds: ExpectedBounds | null;
  started_at: string;
  ended_at: string | null;
  end_reason: "killed" | "promoted" | null;
  kill_started_at: string | null;
  kill_step:
    | "marked"
    | "orders_cancelled"
    | "positions_closed"
    | "fills_written"
    | "slice_returned"
    | null;
  slice?: PaperSlice;
  equity_series?: EquityPoint[];
  latest_promotion?: PromotionRequest | null;
  // The real per-run win/loss counts (see RealisedResultCounts above); present on /runs today,
  // and also merged onto each /history row by the route (lane PP widening it).
  realised_result_counts?: RealisedResultCounts;
  // Every running run's Kill can only ever run through paperdb.StubVenue (no live venue adapter
  // exists yet, #307). "stub" is the only value this can ever hold today; the card renders it
  // beside the state word so nobody reads a running card's Kill as a real close (read_1 finding 1).
  venue?: "stub";
  // /history-only fields (lane PP, plan section 9 item 4): return since admission, total closing
  // trade count, and whether this run's own strategy is now RETIRED IN PAPER (#376 ruling 5).
  // Optional because /runs never carries them (a running run has no "return since admission" that
  // is not just the card's own EFFECTIVE figure, and retirement only ever follows a Kill that has
  // already ended the run).
  return_fraction?: number | null;
  win_count?: number;
  loss_count?: number;
  trade_count?: number;
  retired_in_paper?: boolean;
};

// The Kill route's own response: the run row after pressing Kill, plus which venue ran it. Always
// "stub" today because no live broker adapter exists in this repo (#307).
export type KillResponse = PaperRun & { venue: "stub" };

// The Promote route's own response.
export type PromoteResponse = {
  promotion_id: number;
  state: "requested";
};

// The Approve for real money route's own response. The run ends as promoted; "venue" is "stub"
// today because no paper broker is connected.
export type ApproveRealMoneyResponse = {
  paper_run_id: number;
  end_reason: "promoted";
  ended_at: string;
  kill_step: string;
  venue: string;
};

// GET /api/paper/summary's own shape. Widened (lane PP, plan section 9 item 1) with the window
// selector's own two figures and FEES PAID SO FAR (a lifetime total, never window-scoped).
export type SummaryWindow = "today" | "this_week" | "since_start";
export type PaperSummary = {
  total_equity: number;
  total_opening_cash: number;
  delta_fraction: number | null;
  running_count: number;
  promotions_requested_count: number;
  trades_in_window: number;
  fees_paid_total: number;
  window: SummaryWindow;
};

// One row from GET /api/paper/runs/{id}/fills' own "fills" list: a paper_fills row (both filled
// and refused shapes) plus two fields the route merges in (routes/paper.py's own get_run_fills).
// filled_at / fill_price / fees / venue_order_id are all null on a refusal row (section 10.3);
// refusal_reason is null on everything else. realised_gain is null on a buy, on any refusal, and
// on a sell whose fill_id trade_stats did not walk (should not happen for a filled sell, kept
// nullable rather than assumed because this is a server-computed field, never invented here).
export type PaperFill = {
  fill_id: number;
  paper_run_id: number;
  symbol: string;
  side: "buy" | "sell";
  intended_quantity: number;
  intended_notional: number;
  submitted_quantity: number | null;
  filled_quantity: number | null;
  fill_price: number | null;
  fees: number | null;
  venue_order_id: string | null;
  recorded_at: string;
  submitted_at: string | null;
  filled_at: string | null;
  refusal_reason: string | null;
  realised_gain: number | null;
  position_effect: "opened" | "closed" | null;
};

// GET /api/paper/runs/{id}/fills' own "trade_stats" object (paperdb.trade_stats, minus the
// per_fill_realised_gain map the route already folds onto each fill above).
export type TradeStats = {
  win_count: number;
  loss_count: number;
  average_win: number | null;
  average_loss: number | null;
  largest_win: number | null;
  largest_loss: number | null;
};

export type FillsResponse = { fills: PaperFill[]; trade_stats: TradeStats };

// GET /api/paper/retired's own row shape (paperdb.list_retired_strategies).
export type RetiredStrategy = { strategy_id: number; strategy_name: string; retired_at: string };

// GET /api/display/bars' own shape (dashboard/backend/routes/display.py's _to_display_bar),
// copied here rather than imported from ../../lib/api.ts (outside this lane's declared area):
// this page folder keeps its own typed call over the shared apiGet helper, the frontend map's own
// convention ("Each keeps its typed calls in its own api.ts").
export type DisplayBar = {
  time: string;
  epoch_seconds: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};
export type DisplayBarsResponse = {
  symbol: string;
  timeframe: string;
  bars: DisplayBar[];
  cached_at: string;
};

// GET /api/paper/runs/{id}/holdings (routes/paper.py, get_run_holdings). "share" is a fraction 0..1
// of marked equity; every mark-derived field is null while Alpaca has no price.
export type PaperHolding = {
  symbol: string;
  quantity: number;
  average_cost: number;
  mark_price: number | null;
  mark_source: string | null;
  mark_time: string | null;
  value: number | null;
  open_pnl: number | null;
  share: number | null;
  mark_unavailable_reason: string | null;
};
export type PaperReconcile = {
  symbol: string;
  ours_quantity: number;
  alpaca_quantity: number | null;
  mismatch: boolean | null;
};
export type HoldingsResponse = {
  paper_run_id: number;
  cash: number;
  marked_equity: number | null;
  rows: PaperHolding[];
  share_unavailable_reason: string | null;
  reconcile: PaperReconcile[];
  alpaca_positions_unavailable_reason: string | null;
};

// GET and PUT /api/paper/limits: the six limits in a fixed order; value is null while unset.
export type PaperLimit = {
  key: string;
  value: number | null;
  unit: string;
  meaning: string;
};
export type PaperLimitsResponse = { limits: PaperLimit[] };
