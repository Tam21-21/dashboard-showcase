export type DatasetSource = "binance" | "alpaca_sip" | "eodhd" | "synthetic";

export type DatasetStatus =
  | "pending"
  | "fetching"
  | "ready"
  | "failed"
  | "deleted";

export type Timeframe = "1m" | "5m" | "15m" | "1h" | "4h" | "1d";

export const SOURCE_LABEL: Record<DatasetSource, string> = {
  binance: "Binance",
  alpaca_sip: "Alpaca",
  eodhd: "EODHD daily",
  synthetic: "Made up",
};

export const STATUS_LABEL: Record<DatasetStatus, string> = {
  pending: "Pending",
  fetching: "Fetching",
  ready: "Ready",
  failed: "Failed",
  deleted: "Deleted",
};

export type Dataset = {
  id: number;
  name: string;
  // UNI1: the typed name a page shows; null where none was typed.
  display_name?: string | null;
  source: DatasetSource;
  symbol: string;
  timeframe: Timeframe;
  start_ts: number;
  end_ts: number;
  status: DatasetStatus;
  rows: number | null;
  sha256: string | null;
  file_path: string | null;
  file_bytes: number | null;
  last_error: string | null;
  created_at: number;
  last_fetched_at: number | null;
  // W3 true coverage: the ACTUAL fetched bar range (unix s UTC). Optional / null on
  // legacy rows and row-backed datasets (universes, event-series) — CoverageCell then
  // falls back to start_ts/end_ts (the requested window).
  covered_start_ts?: number | null;
  covered_end_ts?: number | null;
  // Brief B.3.1: the dataset's registered type (the inspector branches on it; D8 enumerates
  // universes) and, for an event-series, which event type it catalogs. Optional so a pre-B
  // payload still type-checks; the live list_datasets SELECT always returns them.
  dataset_type?: DatasetType;
  event_type?: string | null;
  // Item 1 / D2 visibility: current member count for a universe row (distinct current
  // members), null for price / event-series rows where membership does not apply. Lets the
  // list show an empty universe as "0 companies" rather than indistinguishable from absent.
  member_count?: number | null;
  // M5 UU B1.4: a price_universe's saved test window (epoch seconds, UTC day-aligned), or null
  // when the bound is unknown. Lane DX round 4 (SF1): a null bound is never full history; the
  // tracker's own R-T15 refuses a draw against a universe carrying one. Returned by list_datasets
  // so the row can render + edit it; null for every non-price-universe row.
  window_start_ts?: number | null;
  window_end_ts?: number | null;
};

// The registered dataset types (researchdb.DATASET_TYPE_REGISTRY). A plain string-union rather than
// an enum so a future registered type still type-checks; the inspector's else-branch renders any
// unknown type as metadata only. M5 U1 adds price_universe — a ticker-keyed, row-backed price
// universe (the CIK "universe" stays the company-keyed event universe; the two are distinct kinds).
export type DatasetType =
  | "price_dataset"
  | "universe"
  | "event_series"
  | "fundamentals"
  | "price_universe";

export type CreateDatasetBody = {
  name: string;
  source: DatasetSource;
  symbol: string;
  timeframe: Timeframe;
  start: string;
  end: string;
};

export type CreateDatasetResponse =
  | { id: number; status: DatasetStatus; existed?: false }
  | { id: number; status: DatasetStatus; existed: true };

export type DatasetCreatedEvent = {
  id: number;
  name: string;
  source: DatasetSource;
  symbol: string;
  timeframe: Timeframe;
  status: DatasetStatus;
};

export type DatasetStatusChangedEvent = {
  id: number;
  status: DatasetStatus;
  rows?: number;
  sha256?: string;
  file_path?: string;
  file_bytes?: number;
  last_error?: string;
};

export type SettingType = "number" | "string" | "boolean" | "json";

// Kept for compatibility with history-strip callers that still receive TEXT values
// from the SSE stream. New code should use SettingType.
export type SettingValueType = SettingType;

export type SettingUnit = "percent" | "currency" | "path" | "decimal" | "text";

export type SettingValue = number | string | boolean | object | null;

// TERM M4.5 3a B2: a per-strategy resolved setting (the GET /strategies/{id}/settings row).
export type SettingSource = "override" | "inherited";
export type StrategyResolvedSetting = {
  scope: string;
  key: string;
  value: SettingValue; // resolved: the strategy's override if present, else the inherited global
  value_type: SettingValueType;
  unit: string | null;
  label: string | null;
  source: SettingSource; // "override" = a per-strategy value; "inherited" = the lab global
  global_value: SettingValue; // the inherited reference (null for an authored-only key, 3b)
  overridable: boolean;
  source_note: string | null; // 3b override justification; always null in 3a
};

export type Setting = {
  scope: string;
  key: string;
  value: SettingValue;
  type: SettingType;
  unit: SettingUnit;
  label: string;
  description: string | null;
  default_value: SettingValue;
  changed_at: number;
  // lane RV (docs/units/RV/criteria.md AC-2): the DECISIONS ruling number that fixes this row's
  // value ("#392"), or null when no ruling fixes it. Read from the real backend layout, never
  // guessed on the frontend.
  ruled_by: string | null;
};

export type SettingScope = {
  scope: string;
  title: string;
  description: string | null;
  display_order: number;
};

export type SettingHistoryRow = {
  id: number;
  scope: string;
  key: string;
  old_value: string;
  new_value: string;
  changed_at: number;
  changed_by: string;
};

export type SettingChangedEvent = {
  scope: string;
  key: string;
  old_value: string;
  new_value: string;
};

export const VALUE_TYPE_LABEL: Record<SettingType, string> = {
  number: "Number",
  string: "Text",
  boolean: "Boolean",
  json: "JSON",
};

// ── M3 strategies / runs / bundle / provenance (briefs J, K, L) ──

export type StrategyListItem = {
  id: number;
  name: string;
  version: number;
  description: string | null;
};

// ── pre-M4 Brief A1 response shapes ──────────────────────────────────────────

// D5 — strategy create/edit (POST /strategies, PUT /strategies/{id}).
export type StrategyMutationResponse = {
  id: number;
  name: string;
  version: number;
  action: "created" | "updated" | "unchanged";
  mirror_status: string;
};

// ── M4 Phase 2 — editor core + library (R3, R6, R7, R11) ────────────────────

// R3 — one strategy's full current state, incl. source_code (GET /strategies/{id}).
export type StrategyDetail = {
  id: number;
  name: string;
  source_code: string;
  description: string | null;
  version: number;
  disk_mirror_path: string | null;
  last_mirror_status: string;
  last_mirror_error: string | null;
};

// R6 — Validate button result (POST /strategies/validate). A validation FAILURE comes back as a
// 400 ApiError (one error surface with Save), so this success shape carries only ok + description.
export type ValidateStrategyResult = {
  ok: true;
  description: string;
};

// R7 — one entry in the version-history list (GET /strategies/{id}/versions). No source (the diff
// editor fetches a single version's source on demand to keep the list cheap).
export type StrategyVersionMeta = {
  version: number;
  description: string | null;
  edited_at: number; // unix seconds
};

// R7 — one historical version's source, for the Monaco diff (GET /strategies/{id}/versions/{v}).
export type StrategyVersionSource = {
  source_code: string;
  description: string | null;
};

// R11 — the status badge read (GET /strategies/{id}/status): which version the DB holds, which the
// last run used, and whether the derived disk mirror matches the source of truth.
export type StrategyStatus = {
  db_version: number;
  last_run_version: number | null; // null when the strategy has never run
  mirror_status: string;
  mirror_present: boolean;
  mirror_matches: boolean;
  // M4 R8/R9: the AST-read declared metadata. event_types is non-empty for an event strategy (the
  // editor's test run then dispatches an event run); empty for a price strategy (dispatch a price run).
  // asset_class_scope is the declared {crypto, equities} subset the dispatch guard checks (D1).
  event_types: string[];
  asset_class_scope: string[];
  // M5 U2: the declared universe-sweep grid fields ({name, type}), or [] for a strategy that declares none.
  // The dispatch panel renders one comma-separated value input per param; type drives the int/float cast.
  sweep_params?: { name: string; type: string }[];
  // M5 C: which sweep mode the strategy declared. 'inline' (self.param) runs run() once per combination x
  // ticker, so the dispatch grid shows an honest "runs N separate backtests" cost note; 'vectorized'
  // (sweep_signals) simulates the grid in one pass; 'zero' declares no parameters at all (item 6, set 9
  // night: the sweep dispatch panel's own sentence for 'zero' names both ways to add one, self.param or
  // sweep_params, rather than only saying what is absent).
  param_mode?: "zero" | "vectorized" | "inline";
};

// D4 — Add Universe (POST /universes). Unresolvable/ambiguous tickers are counted, never dropped.
export type UniverseLoadResult = {
  universe_id: number;
  created: boolean;
  loaded: number;
  constituents_in: number;
  unresolvable: string[];
  ambiguous: string[];
};

// M5 U1 (R-UNIV) — the result of POST /price-universes. Typed tickers are validated against the EODHD
// availability index (U0): the resolvable ones become members; the absent ones come back in `excluded`,
// reported and never silently dropped. `resolved` is the bare-ticker member list (the durable identity);
// member_count is its length. The CIK UniverseLoadResult is a different shape (companies, not tickers).
export type PriceUniverseCreateResult = {
  universe_id: number;
  name: string;
  resolved: string[];   // bare tickers that were in the index and became members
  excluded: string[];   // typed-but-absent tickers, reported for honesty
  member_count: number; // === resolved.length
};

// M5 U1 — the members read (GET /price-universes/{id}/members): the bare tickers, sorted.
export type PriceUniverseMembers = {
  universe_id: number;
  members: string[];
};

// UI40 — the add and remove results of a price universe's members (POST and DELETE .../members).
export type PriceUniverseMembersAdded = {
  universe_id: number;
  added: string[];
  excluded: string[];
  already: string[];
};
export type PriceUniverseMemberRemoved = { universe_id: number; removed: string };

// UI40 — GET /price-universes/{id}/members/detail: each member now, with the company name and the
// daily price coverage (null when no ready 1d price series is held for the ticker).
export type PriceUniverseMemberDetailRow = {
  ticker: string;
  name: string | null;
  added_at: number;
  first_bar_ts: number | null;
  last_bar_ts: number | null;
  bars: number | null;
  // UNI1: the SEC company record (sector office label, SIC code, industry words) and, when a value is
  // missing, the server's reason in words. Optional so a payload from before UNI1 still type-checks.
  sector?: string | null;
  sic?: string | null;
  industry?: string | null;
  profile_read_at?: number | null;
  sector_missing?: string | null;
  industry_missing?: string | null;
};
export type PriceUniverseMemberDetail = {
  universe_id: number;
  name: string;
  as_of: number;
  members: PriceUniverseMemberDetailRow[];
};

// UNI1 — GET /price-universes/sector-mix: per universe, today's members counted by sector (null sector =
// a member with none), by count descending, then label, null last.
export type PriceUniverseSectorMix = {
  universes: Array<{
    universe_id: number;
    members: number;
    sectors: Array<{ sector: string | null; count: number }>;
  }>;
};

// UI40 — GET /price-universes/{id}/runs: the runs on a universe, newest first (the Run shape).
export type PriceUniverseRuns = {
  universe_id: number;
  runs: Run[];
};

// D4 — Top up the CIK↔ticker map (POST /cik-ticker-map).
export type CikTickerMapResult = {
  rows_in_payload: number;
  inserted: number;
  skipped_existing: number;
  names_backfilled: number;  // ANA-1 §1.2: existing rows whose name was filled or renamed
};

// ANA-1 §1.3 — one catalog search hit (the de-duped (cik, ticker) with its backfilled name).
export type CikTickerSearchRow = {
  cik: number;
  ticker: string;
  name: string | null;  // null when the (cik, ticker) is known only under a non-sec source
};

// D4 — Re-parse a batch's 13D events (POST /event-batches/{id}/reparse).
export type BulkReparseResult = {
  scanned: number;
  skipped_missing_file: number;
  skipped_parse_failure: number;
  correction_candidates: unknown[];
  rollback_candidates: unknown[];
  corrections_applied: number;
  correction_batch_ids: number[];
  correction_failures: unknown[];
};

// Item 5a — clear a stuck running batch (POST /event-batches/reap-orphaned). Empty lists = nothing stuck.
export type ReapOrphanedResult = {
  batches_reaped: number[]; // ids flipped from running -> failed (orphaned by process death)
  events_rolled_back: number; // active events rolled back behind the cleared batches
  batches_swept: number[]; // already-failed batches whose stranded events were rolled back
};

// D7 — dataset lifecycle (POST /datasets/{id}/restore, /refetch).
export type DatasetRestoreResponse = { id: number; status: DatasetStatus };
export type DatasetRefetchResponse = { id: number; status: DatasetStatus; job_id: number };

export type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  pending: "Pending",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

// A run row with its stats joined (GET /runs, GET /runs/{id}). Stats are null until completion.
export type Run = {
  id: number;
  job_id: number;
  name: string | null;
  strategy_id: number;
  strategy_version: number;
  dataset_id: number;
  dataset_sha256: string;
  vbt_version: string | null;
  python_version: string | null;
  build_provenance: string | null;
  hostname: string | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  status: RunStatus;
  error: string | null;
  total_return: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
  num_trades: number | null;
  win_rate: number | null;
  total_fees_paid: number | null;
  total_slippage_cost: number | null;
  profit_factor: number | null;
  // Unit-4 D2: a materialized equity sparkline — up to 32 raw equity values (LTTB), or null for a
  // non-succeeded run (and a legacy run not yet backfilled). Served from the row, no parquet read.
  equity_sparkline: number[] | null;
  // M4 D5: the trial-provenance marker. The backend COALESCEs NULL/legacy to "standard"; "scratch"
  // marks an editor test run (R8). Always present on a GET /runs row.
  kind: RunKind;
  // M5 U2: true iff this run is a universe sweep (its dataset is a price_universe). RunExplorer branches
  // on it — a sweep reads /cells (per-ticker grid), never the single-run /bundle. Absent on legacy rows.
  is_sweep?: boolean;
  // M5 U2: the IN-FLIGHT loop read, attached only while an is_sweep run is pending/running (a succeeded
  // sweep reads /cells; a single run never has it). Rides the same 2.5s run poll RunExplorer already runs.
  sweep_progress?: SweepProgress;
  // RS (U.6, set 5 day): the run's CURRENT judge verdict word, or null when it has never been
  // graded (or the graded row was superseded by a later grading of the same candidate, in which
  // case the CURRENT row's verdict is what shows here, per GET /api/research/runs/{id}'s own
  // widening). Always present on a GET /runs and GET /runs/{id} row from this build onward.
  verdict?: "pass" | "fail" | "unjudgeable" | null;
  // Lane WV (AC-1, `#417`/`#420`): true iff this run's own draw is a GRANTED, LINKED
  // walk-forward draw (the same test GET .../bundle already uses to admit one), derived by the
  // backend so the run page can never read a walk-forward run as an ordinary sweep. Always
  // present on a GET /runs/{id} row from this build onward (a legacy fixture that predates it
  // reads it as JavaScript `undefined`, which `=== true` below reads as false, never as a crash).
  is_walkforward?: boolean;
};

// RS (U.6, set 5 day plan section 10): the judge's three verdict words, named once so every
// consumer of a verdict field shares the same type rather than a bare string.
export type JudgeVerdictWord = "pass" | "fail" | "unjudgeable";

// One stage box of THE CHAIN (GET /api/research/runs/{run_id}/chain): a granted draw for the
// run's own candidate, its bars, and the run that draw was linked to (a stage IS a run).
export type ChainStage = {
  draw_id: number;
  stage: "screen" | "optimise" | "walkforward" | "reserve";
  window_start_ts: number;
  window_end_ts: number;
  bars_observed: number | null; // null until the worker's own result callback records it
  run_id: number | null; // null until tracker.link_run has fired for this draw
  run_status: RunStatus | null; // null when run_id is null, OR when that run has since been deleted
  verdict: JudgeVerdictWord | null;
};

// GET /api/research/runs/{run_id}/chain's own two response shapes: a real chain, or an honest
// absence with the reason in words (never a 404 -- the run is real, only its chain is absent).
export type RunChainResponse =
  | { chain: ChainStage[] }
  | { chain: null; reason: string };

// M5 U2 (loop surface 2) — the IN-FLIGHT progress of a universe sweep: cell done/total + per-member status.
export type SweepMemberStatus = "ready" | "pending" | "fetching" | "error" | "stale";
export type SweepMember = {
  ticker: string;
  status: SweepMemberStatus | string; // the dataset's live status; a string fallback so a new status renders
};
export type SweepProgress = {
  run_id: number;
  run_status: RunStatus;
  job_status: string; // "materializing" while members fetch, else the claimed/running job status
  done: number; // committed cells (the job progress_json; 0 before the first chunk)
  total: number | null; // the cell denominator (combinations x members); null only mid-dispatch
  members: SweepMember[]; // the frozen member set joined to each dataset's live status
};

// M5 U2 (loop surface 3) — one per-ticker (x combo) cell of a sweep's results grid. The 8 stored metrics
// plus the Track-B distribution scalars (skew/kurtosis); a nullable ratio is present-with-null, never absent.
export type SweepCell = {
  variation_id: number;
  ticker: string;
  param_json: string | null;
  total_return: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
  num_trades: number | null;
  win_rate: number | null;
  total_fees_paid: number | null;
  total_slippage_cost: number | null;
  profit_factor: number | null;
  skew: number | null; // Track B (F7): per-cell return-distribution skew; null on a too-short window
  kurtosis: number | null; // Track B (F7): per-cell excess kurtosis; null on a too-short window
};

// One metric's distribution across the sweep's cells (the distribution-led R-SUMMARY). n is the non-null
// count; every field is null for an all-undefined metric (an honest empty distribution).
export type SweepMetricDistribution = {
  n: number;
  min: number | null;
  p25: number | null;
  median: number | null;
  p75: number | null;
  max: number | null;
  mean: number | null;
};
export type SweepSummary = {
  metrics: Record<string, SweepMetricDistribution>; // keyed by metric name (sharpe_ratio, total_return, ..., skew)
  pct_profitable: number | null; // share of cells with a positive total return
};
export type SweepCells = {
  run_id: number;
  cell_count: number;
  member_count: number; // distinct tickers
  variation_count: number; // distinct parameter combinations
  cells: SweepCell[];
  summary: SweepSummary;
};

// M5 Track C — the full-metrics payload (the compute_full_metrics output) + the orchestration response.
// One daily-return histogram bin (the DISTRIBUTION centerpiece; the worker buckets the returns series).
export type FullMetricsHistogramBin = { lo: number; hi: number; count: number };
// Every value is null when undefined (honest absence — the page renders a dash, never a fabricated 0).
export type FullMetricsPayload = {
  // DISTRIBUTION
  skew: number | null;
  kurtosis: number | null;
  var_95: number | null;
  cvar_95: number | null;
  best_day: number | null;
  worst_day: number | null;
  returns_histogram: FullMetricsHistogramBin[] | null;
  returns_mean: number | null;
  // RETURNS
  total_return: number | null;
  cagr: number | null;
  // RISK + RATIOS
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  omega: number | null;
  annualized_volatility: number | null;
  downside_deviation: number | null;
  // DRAWDOWN
  max_drawdown: number | null;
  avg_drawdown: number | null;
  max_drawdown_duration_days: number | null;
  recovery_factor: number | null;
  // TRADES
  total_trades: number | null;
  expectancy: number | null;
  sqn: number | null;
  best_trade: number | null;
  worst_trade: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  // EXPOSURE + COST (F6)
  max_gross_exposure: number | null;
  turnover: number | null;
  total_fees_paid: number | null;
  total_slippage_cost: number | null;
  // PERIOD + CAPITAL
  start: string | null;
  end: string | null;
  bars: number | null;
  start_value: number | null;
  end_value: number | null;
  // BENCHMARK (C-bench; honest absence when no benchmark / too-little overlap)
  benchmark_return: number | null;
  alpha: number | null;
  beta: number | null;
  r_squared: number | null;
  information_ratio: number | null;
  up_capture: number | null;
  down_capture: number | null;
  benchmark_overlap_bars: number | null;
};
// The orchestration route's three states (C2 §4): a ready payload, a computing-in-flight, or honest
// unavailable (worker offline / source gone / a failed compute). Never a number when not ready.
export type FullMetricsResponse = {
  status: "ready" | "computing" | "unavailable";
  payload?: FullMetricsPayload;
  computed_at?: number;
  reason?: string;
  formula_version: string;
};

// M5 U2 (loop surface 1) — the dispatch response: the new run id + the registered combination/member counts.
export type SweepDispatchResult = {
  id: number; // job id
  run_id: number;
  status: string;
  registered_n: number; // registered combinations
  member_count: number;
  all_members_ready: boolean;
  finalized: boolean;
};

// M4 D5: the run's trial-provenance axis (orthogonal to price-vs-event). "standard" is an ordinary or
// legacy run; "scratch" is an editor test run (R8). The runs list can filter on it.
export type RunKind = "standard" | "scratch";

// M4 R8/D5: the runs-list kind filter — "all" (no filter), or one kind.
export type RunKindFilter = "all" | RunKind;

// M4 R13/R14 — the in-editor reference catalog (GET /strategies/catalog), a virtual view of the live
// contract surface, helpers, settings, registered event types, fundamental definitions, and the parsed
// indicator reference. Every field is computed on read, so it cannot drift from the code.
export type CatalogSymbol = {
  name: string;
  kind: string; // "class" | "function" | a type name
  signature: string | null;
  doc: string | null;
};
export type CatalogIndicator = {
  name: string;
  summary: string;
  default_parameters: Record<string, string>;
  honest_assessment: string[];
  pine_source: string;
};
// TERM F3 — one VBT-native indicator, introspected from the pinned build and parsed at boot from the
// committed reference (no Pine source / honest-assessment; that is the curated house set's content).
export type VbtIndicator = {
  name: string;
  description: string;
  inputs: string[];
  parameters: Record<string, string>;
  outputs: string[];
  // AC-4 (docs/briefs/lookahead_guardrail_spec_brief.md): true for the nine future-reading label
  // generators, derived server-side from the authority list only, never from the description text.
  look_ahead: boolean;
};
// One ANA definitions entry (a figure or a ratio): the registry carries more fields, but the catalog
// renders the human label + the plain-English definition.
export type CatalogDefinition = { name: string; label: string; definition: string };
export type StrategyCatalog = {
  base_surface: CatalogSymbol[];
  helpers: CatalogSymbol[];
  settings: { scope: string; key: string; value: unknown; value_type: string }[];
  event_types: string[];
  // The ANA fundamentals definitions registry: { figures, ratios }, each a {concept: entry} map.
  definitions: { figures: Record<string, CatalogDefinition>; ratios: Record<string, CatalogDefinition> };
  indicators: { indicators: CatalogIndicator[]; confluence: Record<string, string> };
  // TERM F3: the full VBT-native indicator suite (generated offline from the pinned build, parsed at boot).
  vbt_indicators: { version: string; indicators: VbtIndicator[] };
  // M4.5 R16: the visual templates (manifests + skeletons, parsed at boot), price first + event last.
  templates: StrategyTemplate[];
};

// M4.5 3b — a structured enforcement bound (the B2.1 schema; mirrors researchdb.validate_bounds_spec).
// Either a numeric range (at least one of min/max; absent side = unbounded; integer rejects a fraction)
// or a categorical allowed-set.
export type BoundsSpec =
  | { type: "number" | "integer"; min?: number; max?: number; exclusiveMin?: boolean; exclusiveMax?: boolean }
  | { allowed: (string | number)[] };

// M4.5 R16 — one parameter of a template's form schema. `target` decides what instantiation does with it:
// "code" → rendered into the skeleton as a literal; "setting" → seeds a per-strategy override (has a
// global); "authored" → a new strategy-namespaced param (seeds an override AND authors four-field provenance).
export type TemplateParam = {
  name: string;
  label: string;
  type: "integer" | "number" | "enum";
  default: string | number;
  target: "code" | "setting" | "authored";
  options?: { value: string | number; label: string }[]; // present for type "enum"
  scope?: string; // present for target "setting"/"authored"
  key?: string;
  bounds?: BoundsSpec;
};

// M4.5 R16 — a committed visual template (a parameterized scaffold over the catalog, R-TMPL3).
export type StrategyTemplate = {
  id: string;
  name: string;
  kind: "price" | "event";
  order: number;
  summary: string;
  params: TemplateParam[];
  skeleton: string; // the code skeleton with {{token}} placeholders (the pure render substitutes params)
};

// Unit-4 D1 — one run's slice of the POST /runs/compare response (overlay + side-by-side stats).
export type RunComparison = {
  id: number;
  name: string | null;
  stats: {
    total_return: number | null;
    sharpe_ratio: number | null;
    max_drawdown: number | null;
    num_trades: number | null;
    win_rate: number | null;
    total_fees_paid: number | null;
    total_slippage_cost: number | null;
    profit_factor: number | null;
  };
  equity: [number, number][];  // full-fidelity [ts, value] pairs, the build_run_bundle shape
};

export type RunSort = "created_at" | "name" | "sharpe" | "return" | "drawdown" | "trades";

// TERM Runs rebuild — the global aggregate for the runs-list summary grid (GET /runs/summary). The
// list is paginated, so the header totals come from this one aggregate read, never a count over the
// loaded page (DECISIONS #143). `by_status` is the live status -> count map (a future status arrives
// for free, like the open-string discipline elsewhere); the derived fields ride alongside so the UI
// renders without re-counting. Every "could be absent" field is nullable, never a fake zero.
export type RunsSummary = {
  total: number;
  by_status: Record<string, number>; // status -> count (succeeded/failed/running/pending/cancelled)
  succeeded: number;
  success_rate: number | null; // succeeded / total, or null when the log is empty (divide-safe)
  in_flight_count: number; // running + pending
  best_sharpe: { run_id: number; name: string | null; sharpe_ratio: number } | null;
  // The most-recent in-flight run (no progress signal exists, so no percent — started_at drives the
  // elapsed-time display). null when nothing is running.
  in_flight: { run_id: number; name: string | null; status: RunStatus; started_at: number } | null;
};

export type CreateRunResponse = { id: number; run_id: number; status: RunStatus };

// ── Pre-M4 Brief D (D6) — job-scoped run controls (cancel + logs) ────────────

// One log line of GET /jobs/{id}/logs. `id` is optional so the SAME type covers
// both the job-logs endpoint rows (which carry an id) and the succeeded bundle's
// embedded logs (which omit it) — one shape, no drift (D-D5). The render reads
// only ts/level/line.
export type JobLog = { id?: number; ts: number; level: string; line: string };  // GET /jobs/{id}/logs row

// One row of GET /jobs/{id}/events — the JOB lifecycle audit trail (worker
// claimed, cancellation requested, finalized), DISTINCT from the event STORE feed
// (13D filings / analyst ratings). Named JobEvent to avoid the overloaded "event"
// confusion. `event` is open free text (the backend writes many strings), so the
// timeline renders an unknown event string gracefully.
export type JobEvent = { id: number; ts: number; event: string; payload: Record<string, unknown> | null };

// 200/202 body of DELETE /jobs/{id} (cancel_job). 200 = {cancelled:true, id,
// run_id?} for a pending job finalized directly (or an idempotent already-cancelled
// job, with no run_id); 202 = {status:'cancelling', id, run_id} for a running
// backtest the worker hard-kills. del<T> hides the status code, so the caller
// branches on the BODY: cancelled===true (immediate) vs status==='cancelling'.
export type CancelResult = { cancelled?: boolean; status?: string; id: number; run_id?: number | null };

// The canonical run bundle (GET /runs/{id}/bundle, build_run_bundle §5.1).
export type RunBundle = {
  schema_version: number;
  run: Record<string, number | string | null>;
  stats: Record<string, number | null>;
  strategy: { name: string | null; version: number; description: string | null; source?: string };
  dataset: {
    id: number | null; source: string | null; symbol: string | null; timeframe: string | null;
    start_ts: number | null; end_ts: number | null; sha256: string | null;
    // C2.5/C2: an event run's bundle emits a universe block instead — kind:"universe", a universe
    // name, and the event window + types (export.py is_event branch). Optional so a price run's
    // block (no kind) and an event run's block share one type.
    kind?: string; name?: string | null; window?: [number, number] | null; event_types?: string[] | null;
  };
  equity: [number, number][];
  // V1: buy-and-hold benchmark, derived from price + equity[0]; same shape as equity.
  benchmark: [number, number][];
  drawdowns: Record<string, number | string | null>[];
  price: { timestamp: number; open: number; high: number; low: number; close: number; volume: number }[];
  indicators: Record<string, [number, number | null][]>;
  // C2: the rule-based regime band(s) the report renders; [] for a price run (no regimes).
  // start/end are epoch seconds; label is the wire string "pre_2024_02_05"/"post_2024_02_05".
  regimes: { kind: string; start: number; end: number; label: string }[];
  // C2.5 D4: true only when meta.json is genuinely absent from the store (a legacy run predating
  // the upload fix). The Regime panel shows a distinct pipeline-gap notice for this vs honest-empty.
  meta_missing?: boolean;
  environment: Record<string, string | null>;
  settings_snapshot?: unknown;
  // A4: settings-drift report (omitted when include_settings=false on a public share).
  settings_drift?: {
    drifted: boolean;
    changes: { scope: string; key: string; run_value: string; current_value: string }[];
  };
  trades?: Record<string, number | string | null>[];
  // D-D5: one shape for logs everywhere — the bundle's embedded logs and the
  // GET /jobs/{id}/logs rows both use JobLog (id optional on the bundle's copy).
  logs?: JobLog[];
  // Phase-2 Unit 3 (audit concern #1): the price run's signal funnel, or null for an event/legacy/
  // bespoke run (the funnel is a price-path concept). The UI renders the funnel panel only when present.
  funnel?: RunFunnel | null;
  // Lane WV (AC-2, `#417`/`#420`): present ONLY on a walk-forward run's bundle
  // (export.py's build_run_bundle appends it last, is_walkforward-gated). Absent on every other
  // run's bundle, never an empty object standing in for "not a walk-forward run".
  walkforward?: WalkforwardBlock;
};

// Lane WV (AC-3/AC-5/AC-6): one research_walkforward_slices row, exactly the columns
// export.py::_read_walkforward_block reads (dashboard/backend/research/export.py). Every
// timestamp is epoch seconds, rendered as a date by this page's own date formatter, never bare.
// fill_reach_ts and entries_masked_at_end are nullable (lane LB, `5.8e`): a pre-merge slice row
// reads null for both, which this page renders as a stated absence, never a fabricated 0.
export type WalkforwardSlice = {
  slice_index: number;
  in_sample_start_ts: number;
  in_sample_end_ts: number;
  out_of_sample_start_ts: number;
  out_of_sample_end_ts: number;
  param_config_hash: string;
  in_sample_profit: number | null;
  out_of_sample_profit: number | null;
  trade_count: number | null;
  fill_reach_ts: number | null;
  entries_masked_at_end: number | null;
};

// Lane WV (AC-2/AC-5/AC-6): the bundle's "walkforward" block, exactly the shape
// export.py::_read_walkforward_block returns. absent_artifacts maps an artifact name to the
// PLAIN reason string the backend already stores, for example the price artifact's own reason:
// this page renders that reason in words, never the raw machine string (#211).
export type WalkforwardBlock = {
  kind: "walkforward";
  slices: WalkforwardSlice[];
  in_sample_profit: number | null;
  out_of_sample_profit: number | null;
  wfa_boundary_buffer_bars: number | null;
  slice_count: number;
  absent_artifacts: Record<string, string>;
};

// Phase-2 Unit 3: the per-run signal funnel (one research_run_funnel row). The stage counts plus the
// WHY-ZERO bucket — what makes a valid-zero distinguishable from a no-data zero (audit concern #1).
export type RunFunnel = {
  bars_total: number;
  bars_after_warmup: number;
  raw_entries: number;
  entries_after_dedup: number;
  raw_exits: number;
  fills: number;
  total_trades: number;
  exits_by_signal: number;
  exits_by_eod: number;
  zero_reason:
    | "NO-DATA"
    | "ALL-NaN-WARMUP"
    | "THRESHOLD-NEVER-CROSSED"
    | "SIGNALS-BUT-NO-FILLS"
    | "ENTRIES-DEDUPED-AWAY"
    | "N/A";
};

export type Provenance = { source: string; mechanism: string; sensitivity: string; bounds: string };

// K.6 share types — see DECISIONS #29 + spec §3.6 / §5.6.
export type Share = {
  uuid: string;
  run_id: number;
  include_source: 0 | 1;
  include_settings: 0 | 1;
  include_indicators: 0 | 1;
  include_trades: 0 | 1;
  include_logs: 0 | 1;
  view_limit: number | null; // null = unlimited
  view_count: number;
  created_at: number;
  revoked_at: number | null;
  last_viewed_at: number | null;
};

export type ShareRunSummary = {
  id: number | null;
  name: string | null;
  created_at: number | null;
  status: RunStatus | null;
  strategy_name: string | null;
};

export type ShareWithRun = { share: Share; run: ShareRunSummary };

export type CreateShareBody = {
  include_source?: boolean;
  include_settings?: boolean;
  include_indicators?: boolean;
  include_trades?: boolean;
  include_logs?: boolean;
  view_limit?: number | null;
};

export type CreateShareResponse = { uuid: string; url: string };

export type PatchShareBody = Partial<CreateShareBody>;

// ── M3.5 brief K: the live feeds page (per-event-type store summary, EL60) ──
// event_type is typed as a plain string (not a closed union) on purpose: the
// backend enumerates eventsdb.EVENT_TYPES, so a future event type arrives here
// for free, and the page renders it with a fallback label rather than crashing.
export const EVENT_TYPE_LABEL: Record<string, string> = {
  sec_13d: "13D filings",
  analyst_rating: "Analyst ratings",
  news: "News",
};

// One recent event as the live-feeds read returns it (the spine + the parsed
// per-type payload; the page selects which payload fields to show per type).
export type EventFeedRow = {
  event_id: number;
  event_type: string;
  knowledge_at: number;          // unix seconds — the "as of" time the feed sorts by
  occurred_at: number | null;    // unix seconds, or null (the real-world event time)
  source: string;                // sec_edgar | canonical_loader | benzinga
  natural_key: string;
  payload: Record<string, unknown>;  // per-type fields; see formatEventSummary in the page
};

// ── Brief B (D2): the dataset inspector reads ──

// One reduced OHLC candle from GET /datasets/{id}/preview (server-side downsampled, R10).
// `time` is unix SECONDS (the lightweight-charts UTCTimestamp PriceChart consumes).
export type PreviewCandle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
};

// GET /datasets/{id}/preview: either the bounded candle view, or an honest "not generated yet"
// state (a pre-Brief-B dataset or a failed upload — a re-fetch regenerates it). Discriminated on
// `available`: when absent/true the candle fields are present; when false only the reason is.
export type DatasetPreview =
  | { dataset_id: number; available?: undefined; source_rows: number; bucket_count: number; candles: PreviewCandle[] }
  | { dataset_id: number; available: false; reason: string };

// GET /events: a keyset page of event rows (R11). order_key tells the UI which key it paged on;
// next.after_cursor is the opaque cursor for the following page (null when exhausted).
export type EventsPage = {
  rows: EventFeedRow[];
  order_key: "knowledge_at" | "event_id";
  next: { after_cursor: string } | null;
};

// GET /events/search (lane UI38-0): one event of the Events tab with its tickers and parsed payload.
export type EventSearchPage = {
  events: Array<{
    event_id: number;
    event_type: string;
    knowledge_at: number;
    occurred_at: number | null;
    source: string;
    tickers: string[];
    payload: Record<string, unknown>;
  }>;
  next: { after_cursor: string } | null;
  total_matching: number;
};
// GET /events/news-top: one row per ticker, most named first.
export type NewsTopRow = { ticker: string; count: number };
// GET /events/rating-actions: every held rating counted by its action, most common first.
export type RatingActions = { total: number; actions: Array<{ action: string; count: number }> };

// Benzinga ingestion status (GET /api/research/benzinga/status, brief BZ5.3): the most
// recent benzinga-source batches, newest first, for the live-feeds controls panel.
export type BenzingaBatchRow = {
  batch_id: number;
  kind: string;
  status: string;
  started_at: number | null;
  finished_at: number | null;
  event_count: number;
  quarantine_count: number;
};
export type BenzingaStatus = {
  source: string;
  recent_batches: BenzingaBatchRow[];
};

// ── M3.75 FUND-4 §10: the fundamentals control surface ───────────────────────

// One row of GET /fundamentals/status (research_fundamental_batches). `quality_report` is the
// §3.4 coverage report as a JSON STRING (the TEXT column, returned raw) — parse it for the
// coverage view. `status`/`kind`/`failure_class` stay plain strings (open-string discipline, the
// store CHECK can widen without breaking the type).
export type FundamentalsBatchRow = {
  batch_id: number;
  kind: string;                 // fetch | refresh
  source: string;               // sec_edgar
  started_at: number | null;
  finished_at: number | null;
  status: string;               // running | succeeded | succeeded_with_quarantines | failed | cancelled_partial
  cik_count: number;
  fact_count: number;
  quarantine_count: number;
  quality_report: string | null;  // the §3.4 coverage report JSON (a STRING; JSON.parse for the view)
  failure_class: string | null;
};

// GET /fundamentals/status: the recent fundamentals batches + the store's newest filing date.
// latest_filing_date is null when the store is empty (an honest empty, never a fake zero).
export type FundamentalsStatus = {
  source: string;
  recent_batches: FundamentalsBatchRow[];
  latest_filing_date: number | null;  // epoch s UTC, the newest filing the store holds
  total_facts_on_record: number;      // ANA-3 §3.1: serve-time COUNT(*) of the whole fact store
  per_year: Array<{ year: number; count: number }>;  // facts by UTC year of filing date, ascending; [] when empty
};

// GET /fundamentals/history (lanes LK1, LK1c, LK1e): each yearly figure and ratio of one company, newest fiscal year first.
// on_record false is a company never fetched (empty lists). A per-share value counted in today's shares keeps the filed
// number in as_filed; splits is null without a ticker; split_error says why the figures stay as filed.
export type FundamentalsHistory = {
  cik: number;
  on_record: boolean;
  periods: string[];
  concepts: Array<{
    concept: string;
    label: string;
    unit: string;
    values: Array<{ period_end: string; value: number; filing_date: string; as_filed?: number; note?: string }>;
  }>;
  ratios: Array<{
    name: string;
    label: string;
    definition: string;
    formula: string;
    source: string;
    values: Array<{ period_end: string; value: number | null; reason: string | null; debt?: number }>;
  }>;
  splits: Array<{ ex_date: string; ratio: number }> | null;
  split_error: string | null;
};

// The parsed §3.4 fundamentals coverage report (the JSON in FundamentalsBatchRow.quality_report).
// Loose like QualityReport: the named sections are read explicitly, the totals as a key/value grid.
export type FundamentalCoverageReport = {
  shape: string;                                         // "fundamentals_coverage"
  totals: Record<string, number>;                        // ciks_attempted, facts_written, quarantines, ...
  per_company?: Array<Record<string, unknown>>;          // per-CIK concepts found/missing, shares fallback
  quarantines_by_class?: Record<string, number>;
  quarantines_samples?: Array<Record<string, unknown>>;
  source_contradictions_samples?: Array<Record<string, unknown>>;
  [k: string]: unknown;
};

// One concept's as-known-on-D read from GET /fundamentals/facts. `found: false` (value null) is the
// honest empty per-concept state for a company with no fact public by the date (NFR-7).
export type FundamentalFact = {
  concept: string;
  found: boolean;
  value: number | null;
  fiscal_period_code?: string;
  fiscal_year?: number;
  period_end?: number;
  period_start?: number | null;
  filing_date?: number;
  source_form?: string;
  accession_number?: string | null;
};

// ANA-5 §5.2 — one leg of a ratio: the concept and (for a present leg) its value + both dates, so
// the UI can show which period each side came from (the D1 cross-period mismatch). A missing leg
// carries nulls.
export type FundamentalRatioLeg = {
  concept: string;
  value: number | null;
  filing_date: number | null;
  period_end: number | null;
};

// ANA-5 §5.2 — one computed ratio. `available` true carries `value`; false carries a plain `reason`
// (a missing leg, a reported-zero denominator, or a non-finite result — the divide-safe rule). Both
// legs always ride along.
export type FundamentalRatio = {
  name: string;
  label: string;
  available: boolean;
  value?: number;
  reason?: string;
  legs: FundamentalRatioLeg[];
};

// GET /fundamentals/facts?cik=&as_of=: one entry per shipped concept, in vocabulary order, plus the
// additive ratios array (ANA-5 §5.2).
export type FundamentalsFacts = {
  cik: number;
  as_of: number;
  facts: FundamentalFact[];
  ratios: FundamentalRatio[];
  on_record: boolean;   // true if the company has ANY fact in the store; false = never fetched (vs honest-missing as-of)
};

// POST /fundamentals/compare: the bounded grid of cells. A compare cell IS the inspector read for
// that company and date (read_company_asof, the same backend path), so it reuses FundamentalsFacts —
// not a parallel type that could silently drift from the inspector's shape.
export type FundamentalsCompare = {
  cells: FundamentalsFacts[];
};

// ANA-2 §2.2 — the plain-English definition of one figure or ratio. `source` is set for figures
// (the statement the figure comes from); `formula` is set for ratios (the plain-text formula).
export type FundamentalDefinition = {
  name: string;
  label: string;
  definition: string;
  source?: string;
  formula?: string;
};

// GET /fundamentals/definitions: the static definitions map, figures + ratios keyed by name.
export type FundamentalsDefinitions = {
  figures: Record<string, FundamentalDefinition>;
  ratios: Record<string, FundamentalDefinition>;
};

// One per-event-type summary entry (the bare array the endpoint returns, one per
// canonical type, present even at total_count 0 for the honest empty state).
export type EventFeedSummary = {
  event_type: string;
  total_count: number;
  latest_knowledge_at: number | null;
  recent_events: EventFeedRow[];
  per_year: Array<{ year: number; count: number }>;  // active events by UTC year of knowledge time, ascending; [] when none
};

// ── Pre-M4 Brief C (D1): the event-operations console reads ──────────────────

// One row of GET /event-batches (the _BATCH_COLUMNS_NO_QR column set, backend
// list_event_batches). `status`/`kind`/`source` stay plain strings (not closed
// unions) so a future store enum still type-checks and renders via the label
// fallbacks — the same open-string discipline as EVENT_TYPE_LABEL.
export type EventBatch = {
  batch_id: number;
  request_id: string | null;
  kind: string;                 // fetch | load | correction | rollback
  parent_batch_id: number | null;
  source: string;               // sec_edgar | benzinga | canonical_loader
  policy: string | null;
  realized_start_at: number | null;   // unix seconds — the realized data window start
  realized_end_at: number | null;     // unix seconds — the realized data window end
  source_descriptor: string | null;
  content_hash: string | null;
  started_at: number | null;          // unix seconds
  finished_at: number | null;         // unix seconds
  last_heartbeat_at: number | null;   // unix seconds
  status: string;               // running | succeeded | succeeded_with_quarantines | failed | rolled_back | cancelled_partial
  quarantine_count: number;
  event_count: number;
  failure_class: string | null;
  failure_context: string | null;
};

// The envelope GET /event-batches returns: a page of rows + the unfiltered total
// + the next offset (integer when more rows remain, null at the end).
export type EventBatchPage = {
  batches: EventBatch[];
  total: number;
  next_offset: number | null;
};

// The parsed quality_report JSON of GET /event-batches/{id}/quality. The exact
// per-shape field set is written by the backend's write_core and is NOT
// enumerated, so the type is deliberately loose and the UI renders defensively:
// the named arrays are read explicitly, the rest as a key/value grid.
export type QualityReport = {
  shape: string | null;                          // "record_level" for closed batches; null pre-sweep
  reason?: string;                               // present when shape is null
  new_version?: number;
  quarantines_samples?: Array<Record<string, unknown>>;
  dedup_collisions?: Array<Record<string, unknown>>;
  [k: string]: unknown;                          // per-shape fields rendered as a key/value grid
};

// GET /event-batches/{id}/quality: the report when written, or the honest
// null-with-reason shape when the secondary sweep has not run yet.
export type EventBatchQuality = {
  quality_report: QualityReport | null;
  reason?: string;
};

// One active share row of GET /event-batches/{id}/shares (the create route also
// returns it in its `share` slot). Mirrors the persisted _SHARE_COLS column set
// (researchdb._SHARE_COLS): run_id is NULL for a batch share, batch_id carries
// it. The include_* flags are SQLite integer booleans (0 | 1), matching the run
// Share type, NOT a reused/overloaded Share (its flags are run-bundle specific).
export type EventBatchShare = {
  uuid: string;
  run_id: number | null;         // NULL for a batch share (shared research_shared_runs table)
  batch_id: number | null;
  include_source: 0 | 1;         // SQLite integer boolean, matches the run Share type
  include_settings: 0 | 1;
  include_indicators: 0 | 1;
  include_trades: 0 | 1;
  include_logs: 0 | 1;
  view_limit: number | null;     // null = unlimited
  view_count: number;
  created_at: number;            // unix seconds
  revoked_at: number | null;     // unix seconds, or null while active
  last_viewed_at: number | null; // unix seconds
};

// Request body for POST /event-batches/{id}/share (all optional; the backend
// applies its own defaults: logs OFF, the rest ON, view_limit 10).
export type CreateBatchShareBody = {
  include_source?: boolean;
  include_settings?: boolean;
  include_indicators?: boolean;
  include_trades?: boolean;
  include_logs?: boolean;
  view_limit?: number | null;
};

// The 201 response of POST /event-batches/{id}/share: the public URL the share
// resolves to, plus the persisted row.
export type CreateBatchShareResponse = {
  uuid: string;
  url: string;
  share: EventBatchShare;
};

// 200 body of POST /event-batches/correction (write_core.apply_correction):
// the correction batch id, the corrected event, its new version, and the hash.
export type CorrectionResult = {
  batch_id: number;
  event_id: number;
  new_version: number;
  content_hash: string;
};

// 200 body of POST /event-batches/{id}/rollback (write_core.apply_rollback):
// the rollback batch id, how many active events it reversed, and the hash.
export type RollbackResult = {
  rollback_batch_id: number;
  rolled_back_event_count: number;
  content_hash: string;
};

// 200 body of POST /admin/reconcile-event-catalog (the catalog backstop): the
// counts plus any orphans found in either direction. orphan_catalog_rows is a
// list of rows (each a list of cell values); orphan_batches a list of values.
export type ReconcileResult = {
  series_walked: number;
  series_updated: number;
  orphan_catalog_rows: unknown[][];
  orphan_batches: unknown[];
  duration_ms: number;
};

// Lane P7: the Chains page's shapes, as lane P7-0's routes answer them (dashboard/backend/routes/chain.py).
export type ChainState =
  | "waiting" | "screening" | "optimising" | "choosing-cell" | "replaying" | "walking-forward"
  | "judging" | "done" | "refused" | "failed" | "stopped";
export type ChainGroup = "active" | "waiting-on-you" | "ended" | "retryable";
export type ChainStep = "screen" | "optimise" | "choose" | "replay" | "walkforward" | "judge";

export type Chain = {
  id: number;
  strategy_id: number;
  strategy_version: number;
  calendar_id: string;
  screen_start_ts: number;
  optimise_start_ts: number;
  walkforward_start_ts: number;
  walkforward_end_ts: number;
  state: ChainState;
  state_word: string;
  group: ChainGroup;
  step: ChainStep | null;
  ended_at_step: ChainStep | null;
  stop_requested: number;
  note: string | null;
  note_word: string | null;
  reason_word: string | null;
  reason_sentence: string | null;
  display_name: string | null;
  duel_chain_id: number | null;
  chosen_variation_id: number | null;
  chosen_ticker: string | null;
  created_at: number;
  updated_at: number;
};

export type ChainList = { chains: Chain[]; reserve_cut_ts: number };

export type ChainTransition = {
  id: number;
  chain_id: number;
  from_state: ChainState | null;
  to_state: ChainState;
  to_word: string;
  actor: string;
  reason_word: string | null;
  reason_sentence: string | null;
  created_at: number;
};

export type ChainDetail = Chain & {
  optimise_run_id: number | null;
  walkforward_run_id: number | null;
  transitions: ChainTransition[];
};

export type ChainCreateBody = {
  strategy_id: number;
  screen_start_ts: number;
  optimise_start_ts: number;
  walkforward_start_ts: number;
  walkforward_end_ts: number;
  param_grid: Record<string, number[]>;
};
export type ChainWriteResult = { status: string; chain_id: number };

// GET /api/research/strategies also carries display_name; StrategyListItem predates it.
export type ChainStrategyOption = StrategyListItem & { display_name: string | null };
