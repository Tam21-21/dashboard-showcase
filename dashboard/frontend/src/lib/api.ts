import type {
  BudgetConfig,
  CalendarEvent,
  EmailFull,
  EmailStatus,
  EmailSummary,
  EveningSummary,
  PushSent,
  PushSubscribeBody,
  SummarySettings,
  EventTemplate,
  WeatherData,
  WeatherForecastDay,
  Expense,
  GarminActivity,
  GarminActivityBundle,
  GarminSnapshot,
  NewsItem,
  Todo,
} from "../types";
import type {
  CreateDatasetBody,
  CreateDatasetResponse,
  CreateRunResponse,
  CreateShareBody,
  CreateShareResponse,
  Dataset,
  DatasetPreview,
  DatasetRefetchResponse,
  DatasetRestoreResponse,
  BenzingaStatus,
  FundamentalsStatus,
  FundamentalsFacts,
  FundamentalsHistory,
  FundamentalsCompare,
  FundamentalsDefinitions,
  BulkReparseResult,
  ReapOrphanedResult,
  CancelResult,
  CikTickerMapResult,
  CikTickerSearchRow,
  JobEvent,
  JobLog,
  EventBatch,
  EventBatchPage,
  EventBatchQuality,
  EventBatchShare,
  CreateBatchShareBody,
  CreateBatchShareResponse,
  CorrectionResult,
  RollbackResult,
  ReconcileResult,
  EventsPage,
  EventSearchPage,
  NewsTopRow,
  RatingActions,
  BoundsSpec,
  EventFeedSummary,
  PatchShareBody,
  Provenance,
  Run,
  RunBundle,
  RunComparison,
  RunKindFilter,
  RunsSummary,
  RunSort,
  RunChainResponse,
  JudgeVerdictWord,
  Setting,
  SettingHistoryRow,
  SettingScope,
  StrategyCatalog,
  SettingValue,
  StrategyResolvedSetting,
  Share,
  ShareWithRun,  // owner-introspect shape (share + run summary)
  StrategyListItem,
  StrategyMutationResponse,
  StrategyDetail,
  StrategyStatus,
  StrategyVersionMeta,
  StrategyVersionSource,
  ValidateStrategyResult,
  UniverseLoadResult,
  PriceUniverseCreateResult,
  PriceUniverseMembers,
  PriceUniverseMembersAdded,
  PriceUniverseMemberRemoved,
  PriceUniverseMemberDetail,
  PriceUniverseRuns,
  PriceUniverseSectorMix,
  SweepDispatchResult,
  SweepCells,
  FullMetricsResponse,
  ChainList,
  ChainDetail,
  ChainCreateBody,
  ChainWriteResult,
} from "../types/research";
import { registerSync } from "./refresh-registry";

// The four proposals POST /api/life/ask can return (dashboard/backend/ask_claude.py).
export type AskProposal =
  | { kind: "add"; title: string; date: string; start: string; end: string; all_day: boolean }
  | { kind: "series"; title: string; days: string[]; start: string; end: string; first_date: string; until: string }
  | { kind: "remove"; event_ids: string[]; titles: string[] }
  | { kind: "move"; event_id: string; title: string; date: string; start: string; end: string; all_day: boolean };
// Lane EA3-0's answer for GET /api/research/events/earnings. The shape is this one route's own, so it sits here.
export type EarningsFigures = {
  adjusted: boolean; eps_actual: number; eps_estimate: number; eps_word: string;
  sales_actual: number; sales_estimate: number; sales_word: string; headline: string;
};
export type EarningsCloses = {
  dates: string[]; values: number[]; before_index: number | null; after_index: number | null;
};
export type EarningsRow = {
  ticker: string; company: string; quarter: string; release_at: string; adjusted: boolean;
  eps_actual: number; eps_estimate: number; eps_surprise: number | null;
  sales_actual: number; sales_estimate: number; sales_surprise: number | null;
  result: string; headline: string;
  corrected: boolean; first_printed: EarningsFigures | null; corrected_at: string | null;
  reaction: number | null; closes: EarningsCloses;
};
export type EarningsAnswer = { rows: EarningsRow[]; unread: number };
// Lane OP: the seven display-only market types lane OB's backend already serves. A separate
// import (not folded into the "../types" block above) because these live in their own file,
// types/display.ts, matching how DisplayBar/DisplayBarsResponse below already sit apart from
// types/research.ts for the same reason (display data is its own boundary, root CLAUDE.md).
import type {
  CommodityRow,
  HistoryResponse,
  MarketNewsItem,
  MarketQuote,
  MarketSessionResponse,
  NewsResponse,
  SearchResult,
  YieldTenor,
} from "../types/display";

// A typed fetch error carrying the HTTP status, the parsed JSON body (when the
// response was JSON, else undefined) and the request url. `.message` is the status
// and its text only (plus the server's detail for a write), with no url, so a page
// that shows it never prints an address. Callers read `.status` / `.body` / `.url`.
export class ApiError extends Error {
  status: number;
  body: unknown;
  readonly url: string;
  constructor(message: string, status: number, body: unknown, url: string = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.url = url;
  }
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) {
    // Parse the JSON body for callers that need its structured fields.
    let parsed: unknown;
    try {
      // Best-effort parse of the error body for callers that read ApiError.body. A
      // failure here (res.json throws SyntaxError on a non-JSON body) is deliberately
      // swallowed: it must never mask the HTTP error we always throw just below.
      parsed = await res.json();
    } catch (e: unknown) {
      /* body wasn't JSON; leave parsed undefined and still throw the HTTP error */
    }
    throw new ApiError(`${res.status} ${res.statusText}`, res.status, parsed, url);
  }
  return res.json();
}

async function mutate<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body != null ? { "Content-Type": "application/json" } : {},
    body: body != null ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = "";
    let parsed: unknown;
    try {
      // Best-effort parse of the error body. Deliberately swallowed on failure so a
      // non-JSON body cannot mask the HTTP error thrown below (res.json throws SyntaxError).
      const j = await res.json();
      parsed = j;  // keep the full parsed body for ApiError consumers
      if (j && typeof j === "object" && "detail" in j && j.detail) {
        detail = `: ${typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)}`;
      }
    } catch (e: unknown) {
      /* body wasn't JSON; ignore and still throw the HTTP error */
    }
    throw new ApiError(`${res.status} ${res.statusText}${detail}`, res.status, parsed, url);
  }
  return res.json();
}

const post = <T>(url: string, body: unknown) => mutate<T>("POST", url, body);
const put  = <T>(url: string, body: unknown) => mutate<T>("PUT",  url, body);
const patch = <T>(url: string, body: unknown) => mutate<T>("PATCH", url, body);
const del   = <T>(url: string) => mutate<T>("DELETE", url);

// Night one (#372): named exports so a page folder (routes/judge, routes/paper) keeps its own typed
// calls over the shared fetch helpers without editing this file.
export const apiGet = get;
export const apiPost = post;
export const apiPut = put;
export const apiDelete = del;

// NO-OP (lane JC round 3, CS Amendment 1's own L-4 sweep): judgeRun below is a thin generic POST
// wrapper over /api/judge/runs/{run_id}/judge. It asserts no status-code-specific behaviour and
// branches on no W-1 word; every refusal reaches the caller as a plain ApiError whose `detail`
// text is rendered verbatim (RunExplorerPage.tsx, its own NO-OP marker). Amendment 1 changed which
// words the ROUTE can answer and in what order, never the SHAPE of a POST call or an error, so
// this file needs no edit for R1-R8/A1-A5 to reach production correctly.
// RS (U.6, set 5 day): POST /api/judge/runs/{run_id}/judge's 200 body (routes/judge.py's own
// judge_run: "return {verdict_id, verdict, battery_version, check_outcomes}"). Named here rather
// than in types/research.ts since it is this one mutation's own result shape, not a stored row.
type JudgeRunResult = {
  verdict_id: number;
  verdict: JudgeVerdictWord;
  battery_version: string;
  check_outcomes: { check: number; outcome: string }[];
};

// Lane RP (set 16b, `#423`), docs/units/RP/criteria.md AC-1: POST /api/judge/runs/{run_id}/replay's
// own 200/202 body (routes/judge.py's own replay_cell: "{status, run_id, variation_id, ticker}" on
// 200 replay-exists, plus "job_id" on 202 replay-queued). `status` is one of the closed word set
// replayWords.ts maps to plain sentences; this file never branches on it itself.
type ReplayCellResult = {
  status: string;
  run_id: number;
  variation_id: number;
  ticker: string;
  job_id?: number;
};

// GET /api/settings/connections/live: what is true now for each service (lane UI32-0).
// A part with configured false has nulls and no call was made. Any part may also carry an
// error sentence; the server adds one to the Garmin part when that part could not be read.
export type ConnectionsLive = {
  checked_at: string; // ISO 8601 UTC, ends in Z
  garmin: {
    configured: boolean;
    last_sync_at: string | null; // UTC, ends in Z
    last_sync_error: string | null;
    watch_data_until: string | null; // YYYY-MM-DD
    error?: string | null;
  };
  alpaca: {
    configured: boolean;
    paper: boolean;
    status: string | null;
    trading_blocked: boolean | null;
    error: string | null;
  };
  eodhd: {
    configured: boolean;
    requests_today: number | null;
    daily_limit: number | null;
    as_of: string | null; // YYYY-MM-DD
    error: string | null;
  };
};

export const api = {
  settings: {
    connections: () => get<{ alpaca: boolean; eodhd: boolean; garmin: boolean }>("/api/settings/connections"),
    connectionsLive: () => get<ConnectionsLive>("/api/settings/connections/live"),
  },
  life: {
    getExpenses: (date: string) => get<Expense[]>(`/api/life/expenses?date=${date}`),
    addExpense: (body: { date: string; label: string; amount: number; category: string }) =>
      post<Expense>("/api/life/expenses", body),
    deleteExpense: (id: number) => del<{ ok: boolean }>(`/api/life/expenses/${id}`),
    getMonthlyExpenseTotal: (month: string) =>
      get<{ total: number }>(`/api/life/expenses/monthly-total?month=${month}`),

    getBudgetConfig: () => get<BudgetConfig>("/api/life/budget-config"),
    updateBudgetConfig: (body: {
      monthly_budget: number;
      fixed_expenses: number;
      manual_monthly_spent?: number | null;
    }) => put<BudgetConfig>("/api/life/budget-config", body),

    getTodos: (date: string) => get<Todo[]>(`/api/life/todos?date=${date}`),
    createTodo: (body: { date: string; text: string }) => post<Todo>("/api/life/todos", body),
    openTodos: (before: string) => get<Todo[]>(`/api/life/todos/open?before=${before}`),
    doneLateTodos: (day: string) => get<Todo[]>(`/api/life/todos/done-late?day=${day}`),
    patchTodo: (id: number, body: { text?: string; done?: boolean; position?: number; date?: string }) =>
      patch<Todo>(`/api/life/todos/${id}`, body),
    deleteTodo: (id: number) => del<{ ok: boolean }>(`/api/life/todos/${id}`),
    reorderTodos: (ordered_ids: number[]) =>
      post<{ ok: boolean }>("/api/life/todos/reorder", { ordered_ids }),

    garminToday: () =>
      get<{ configured: boolean; snapshot: GarminSnapshot | null }>("/api/life/garmin/today"),
    garminWeek: () => get<GarminSnapshot[]>("/api/life/garmin/week"),
    garminDays: (n = 28) =>
      get<{ configured: boolean; snapshots: GarminSnapshot[] }>(`/api/life/garmin/days?n=${n}`),
    garminRefresh: () => post<GarminSnapshot>("/api/life/garmin/refresh", {}),
    eveningSummary: () => get<{ summary: EveningSummary | null }>("/api/life/summary"),
    summarySettings: () => get<SummarySettings>("/api/life/summary/settings"),
    saveSummarySettings: (body: { steps_goal: number }) =>
      put<{ steps_goal: number }>("/api/life/summary/settings", body),
    pushKey: () => get<{ key: string }>("/api/life/push/key"),
    pushSubscribe: (body: PushSubscribeBody) => post<{ ok: boolean }>("/api/life/push/subscribe", body),
    pushUnsubscribe: (body: { endpoint: string }) => post<{ ok: boolean }>("/api/life/push/unsubscribe", body),
    pushState: (body: { endpoint: string }) => post<{ known: boolean }>("/api/life/push/state", body),
    pushTest: () => post<PushSent>("/api/life/push/test", {}),

    garminActivities: (limit = 3) =>
      get<{ configured: boolean; activities: GarminActivity[] }>(
        `/api/life/garmin/activities?limit=${limit}`,
      ),
    garminActivityDetail: (activityId: string, refresh = false) =>
      get<GarminActivityBundle>(
        `/api/life/garmin/activities/${encodeURIComponent(activityId)}${refresh ? "?refresh=true" : ""}`,
      ),
    garminActivitiesRefresh: () =>
      post<{ count: number }>("/api/life/garmin/activities/refresh", {}),

    calendarStatus: () =>
      get<{ configured: boolean; connected: boolean; last_synced: string | null }>(
        "/api/life/calendar/status",
      ),
    calendarToday: () => get<CalendarEvent[]>("/api/life/calendar/today"),
    calendarRange: (from: string, to: string) =>
      get<CalendarEvent[]>(`/api/life/calendar/range?from=${from}&to=${to}`),
    calendarCreate: (body: {
      title: string;
      start_dt: string;
      end_dt: string;
      all_day?: boolean;
      color_id?: string;
      recurrence?: string[];
    }) => post<CalendarEvent & { sync_error: string | null }>("/api/life/calendar/events", body),
    ask: (question: string, history: { role: "user" | "assistant"; text: string }[]) =>
      post<{ answer: string; proposals: AskProposal[] }>("/api/life/ask", { question, history }),
    calendarCreateLocal: (body: {
      title: string;
      start_dt: string;
      end_dt: string;
      all_day?: boolean;
      color_id?: string;
    }) => post<CalendarEvent>("/api/life/calendar/events/local", body),
    calendarPatchEvent: (event_id: string, body: { end_dt: string }) =>
      patch<CalendarEvent>(`/api/life/calendar/events/${encodeURIComponent(event_id)}`, body),
    calendarMoveEvent: (event_id: string, body: { start_dt: string; end_dt: string }) =>
      patch<CalendarEvent>(`/api/life/calendar/events/${encodeURIComponent(event_id)}/move`, body),
    deleteCalendarEvent: (event_id: string) =>
      del<{ ok: boolean }>(`/api/life/calendar/events/${encodeURIComponent(event_id)}`),
    calendarSync: () => post<{ synced: number; pushed: number }>("/api/life/calendar/sync", {}),

    getTemplates: () => get<EventTemplate[]>("/api/life/calendar/templates"),
    createTemplate: (body: { title: string; color: string }) =>
      post<EventTemplate>("/api/life/calendar/templates", body),
    deleteTemplate: (id: number) =>
      del<{ ok: boolean }>(`/api/life/calendar/templates/${id}`),

    googleAuthUrl: () => get<{ auth_url: string }>("/api/auth/google"),
    googleDisconnect: () => post<{ ok: boolean }>("/api/auth/google/disconnect", {}),

    weather: () => get<WeatherData>("/api/life/weather"),
    weatherForecast: () => get<WeatherForecastDay[]>("/api/life/weather/forecast"),
    news: () => get<NewsItem[]>("/api/life/news"),

    emailsStatus: () => get<EmailStatus>("/api/emails/status"),
    emailsRecent: () => get<EmailSummary[]>("/api/emails/recent"),
    emailGet: (id: string, allowImages = false) =>
      get<EmailFull>(`/api/emails/${encodeURIComponent(id)}?allow_images=${allowImages}`),
  },

  research: {
    listDatasets: (status: "active" | "deleted" | "all" = "active", includeSynthetic = false) =>
      get<Dataset[]>(`/api/research/datasets?status=${status}${includeSynthetic ? "&include_synthetic=true" : ""}`),
    getDataset: (id: number) => get<Dataset>(`/api/research/datasets/${id}`),
    createDataset: (body: CreateDatasetBody) =>
      post<CreateDatasetResponse>("/api/research/datasets", body),
    deleteDataset: (id: number) =>
      del<{ ok: boolean }>(`/api/research/datasets/${id}`),
    // ── pre-M4 Brief A1 / D7 — dataset lifecycle controls ──
    restoreDataset: (id: number) =>
      post<DatasetRestoreResponse>(`/api/research/datasets/${id}/restore`, {}),
    refetchDataset: (id: number) =>
      post<DatasetRefetchResponse>(`/api/research/datasets/${id}/refetch`, {}),
    // ── W8 — "Update to latest" (extend the window to now; the worker re-cuts the whole window for a
    //    source that adjusts past prices, eodhd and alpaca_sip, and fetches only the gap for binance) ──
    updateDatasetToLatest: (id: number) =>
      post<{ id: number; status: string; action?: string; reason?: string; job_id?: number; new_end_ts?: number }>(
        `/api/research/datasets/${id}/update-to-latest`, {}),
    // W8 — the server-authoritative "Full / earliest-to-latest" window for (source, timeframe).
    getSourceFloor: (source: string, timeframe: string) =>
      get<{ start: string; end: string }>(
        `/api/research/datasets/source-floor?source=${encodeURIComponent(source)}&timeframe=${encodeURIComponent(timeframe)}`),
    // ── Brief B (D2) — the dataset inspector reads ──
    getDatasetPreview: (id: number) =>
      get<DatasetPreview>(`/api/research/datasets/${id}/preview`),
    listEvents: (params: { event_type?: string; batch_id?: number; after_cursor?: string; limit?: number }) => {
      const q = new URLSearchParams();
      if (params.event_type) q.set("event_type", params.event_type);
      if (params.batch_id != null) q.set("batch_id", String(params.batch_id));
      if (params.after_cursor) q.set("after_cursor", params.after_cursor);
      if (params.limit != null) q.set("limit", String(params.limit));
      const qs = q.toString();
      return get<EventsPage>(`/api/research/events${qs ? `?${qs}` : ""}`);
    },
    // Events tab (lane UI38-0): `types` is the comma list the route reads; unset filters are not sent.
    searchEvents: (params: { types: string; q?: string; ticker?: string; action?: string; after_cursor?: string; limit?: number }) => {
      const q = new URLSearchParams({ types: params.types });
      if (params.q) q.set("q", params.q);
      if (params.ticker) q.set("ticker", params.ticker);
      if (params.action) q.set("action", params.action);
      if (params.after_cursor) q.set("after_cursor", params.after_cursor);
      if (params.limit != null) q.set("limit", String(params.limit));
      return get<EventSearchPage>(`/api/research/events/search?${q.toString()}`);
    },
    newsTop: () => get<NewsTopRow[]>("/api/research/events/news-top"),
    ratingActions: () => get<RatingActions>("/api/research/events/rating-actions"),
    // Earnings (lane EA3): the newest releases with surprise and price reaction (lane EA3-0's read).
    earnings: (limit: number) => get<EarningsAnswer>(`/api/research/events/earnings?limit=${limit}`),
    // Hard-delete the parquet of a soft-deleted dataset (backend gated on status='deleted').
    hardDeleteDatasetFile: (id: number) =>
      del<{ id: number; status: string; ok?: boolean; job_id?: number }>(
        `/api/research/datasets/${id}/file`,
      ),
    // ── pre-M4 Brief A1 / D4 — reference-data loaders ──
    addUniverse: (body: { name: string; tickers: string[]; source_label: string }) =>
      post<UniverseLoadResult>("/api/research/universes", body),
    // ── M5 U1 (R-UNIV) — price universes: create-by-typing + manage ──
    // Create: the server validates each typed ticker against the EODHD availability index (U0), saves
    // the resolvable ones as members, and reports the excluded (absent) ones. An all-excluded body
    // throws 422 (price_universe_empty); a duplicate name throws 409 — both surface as an ApiError.
    // M5 UU B1.3: the create contract carries the test window (epoch seconds, UTC day-aligned),
    // written atomically with the universe. Lane DX round 4 (SF1, code-quality pass 1): a NULL
    // bound is never full history, it is unknown, and the tracker's own R-T15 refuses a draw
    // against a universe carrying one; the server itself now refuses a create with either bound
    // absent (PriceUniverseWindowRequired, 422), so a caller that passes null here gets that
    // refusal, never a silently-created windowless universe.
    createPriceUniverse: (
      name: string,
      tickers: string[],
      windowStartTs?: number | null,
      windowEndTs?: number | null,
      displayName?: string | null,
    ) =>
      post<PriceUniverseCreateResult>("/api/research/price-universes", {
        name,
        tickers,
        window_start_ts: windowStartTs ?? null,
        window_end_ts: windowEndTs ?? null,
        display_name: displayName ?? null,
      }),
    // UNI1: today's members of every price universe counted by sector (the Universes tab's colour key).
    priceUniverseSectorMix: (includeSynthetic = false) =>
      get<PriceUniverseSectorMix>(`/api/research/price-universes/sector-mix${includeSynthetic ? "?include_synthetic=true" : ""}`),
    // The members (bare tickers) of one price universe, sorted (the prefix read).
    priceUniverseMembers: (id: number) =>
      get<PriceUniverseMembers>(`/api/research/price-universes/${id}/members`),
    // UI40: add tickers to a price universe. The result names the added, the excluded (not in the
    // EODHD index) and the already-members; 404 unknown universe, 422 nothing to add or a mixed list.
    addPriceUniverseMembers: (id: number, tickers: string[]) =>
      post<PriceUniverseMembersAdded>(`/api/research/price-universes/${id}/members`, { tickers }),
    // UI40: remove one member (the universe keeps the dated record). 404 not a member; 422 the last member.
    removePriceUniverseMember: (id: number, ticker: string) =>
      del<PriceUniverseMemberRemoved>(`/api/research/price-universes/${id}/members/${encodeURIComponent(ticker)}`),
    // UI40: each member now with its company name and daily price coverage.
    priceUniverseMemberDetail: (id: number) =>
      get<PriceUniverseMemberDetail>(`/api/research/price-universes/${id}/members/detail`),
    // UI40: the runs on a price universe, newest first (at most 500).
    priceUniverseRuns: (id: number) =>
      get<PriceUniverseRuns>(`/api/research/price-universes/${id}/runs`),
    // Rename a price universe's catalog row (PATCH; 404 if no price_universe row with that id).
    renamePriceUniverse: (id: number, name: string) =>
      patch<{ universe_id: number; name: string }>(`/api/research/price-universes/${id}`, { name }),
    // UNI1: set a price universe's display name (PATCH; 404 unknown id, 422 blank or over 60 characters).
    setPriceUniverseDisplayName: (id: number, displayName: string) =>
      patch<{ universe_id: number; name: string; display_name: string | null }>(
        `/api/research/price-universes/${id}`, { display_name: displayName }),
    // M5 UU B1.3: set/clear a price universe's saved test window AFTER create, a DISTINCT sub-route
    // from the rename PATCH (which it must not collide with). This PATCH route, unlike create, still
    // admits a NULL bound (the admin-correction path); lane DX round 4 (SF1): a NULL bound here is
    // never full history either, it is unknown, and the tracker's own R-T15 refuses a draw against
    // it exactly as it does for a create-time NULL. 404 on a missing id, 422 on start > end (both
    // surface as an ApiError).
    updatePriceUniverseWindow: (id: number, windowStartTs: number | null, windowEndTs: number | null) =>
      patch<{ universe_id: number; window_start_ts: number | null; window_end_ts: number | null }>(
        `/api/research/price-universes/${id}/window`,
        { window_start_ts: windowStartTs, window_end_ts: windowEndTs },
      ),
    // Lane UD (U.3b, set 6 day): soft-delete a price universe through the shared del() helper, the
    // same DELETE-verb idiom deleteDataset already uses above. 404 unknown id; 409 while fetching
    // or while a run still names it as its dataset_id (the server's own sentence, rendered
    // verbatim by the page, never a code); 200 idempotent on a repeat.
    deletePriceUniverse: (id: number) => del<{ ok: boolean }>(`/api/research/price-universes/${id}`),
    // M5 U2 (loop surface 1) — dispatch a universe sweep over a saved price universe. param_grid is the
    // {param: [values]} grid (one value per param = a plain backtest, registered_n=1). Returns the new run
    // id; the caller navigates to /research/runs/{run_id} to watch IN FLIGHT then RESULTS. A bad grid throws
    // 400, an empty universe 422, a scope mismatch 422, a deleted strategy 409 — all surface as an ApiError.
    // M5 UU B3.3: selection_scope is no longer a request field — the backend stamps 'in_isolation'. The
    // client never sends it (the request contract and the client ship together so they never disagree).
    dispatchSweep: (
      universeId: number,
      body: { strategy_id: number; param_grid: Record<string, (number | string)[]>; name?: string | null },
    ) => post<SweepDispatchResult>(`/api/research/price-universes/${universeId}/dispatch`, body),
    // M5 U2 (loop surface 3) — the succeeded sweep's per-ticker cell grid + the distribution-led summary.
    // 404 unknown; 409 if the run is not a sweep or has not finished (its in-flight progress is on getRun).
    getRunCells: (id: number) => get<SweepCells>(`/api/research/runs/${id}/cells`),
    // M5 Track C — the full-metrics orchestration read. A single run passes no variation/ticker; a universe
    // cell passes BOTH; the universe AGGREGATE passes scope="aggregate" + variationId (no ticker — it is across
    // every member). Returns {status: ready|computing|unavailable}; the page polls while computing. retry
    // re-dispatches after a failed compute (the try-again affordance).
    getRunFullMetrics: (
      id: number, variationId?: number, ticker?: string, retry = false, scope?: "aggregate",
    ) => {
      const p = new URLSearchParams();
      if (scope === "aggregate" && variationId === undefined) {
        // fail loud, never a silent fall-through to a single-run read (no fallback that papers over a real bug)
        throw new Error("getRunFullMetrics: scope='aggregate' requires variationId");
      }
      if (scope === "aggregate") {
        p.set("scope", "aggregate");
        p.set("variation_id", String(variationId)); // the aggregate is variation_id only (no ticker)
      } else if (variationId !== undefined && ticker !== undefined) {
        p.set("variation_id", String(variationId));
        p.set("ticker", ticker);
      }
      if (retry) p.set("retry", "1");
      const qs = p.toString();
      return get<FullMetricsResponse>(`/api/research/runs/${id}/full-metrics${qs ? `?${qs}` : ""}`);
    },
    topUpCikTickerMap: (source_file?: string) =>
      post<CikTickerMapResult>("/api/research/cik-ticker-map", { source_file: source_file ?? null }),
    // ANA-1 §1.3 — the catalog search: case-insensitive prefix match on ticker or company name,
    // de-duped to one row per (cik, ticker). Unwraps the route's { q, results } envelope to the rows.
    cikTickerSearch: (q: string, limit?: number) =>
      get<{ q: string; results: CikTickerSearchRow[] }>(
        `/api/research/cik-ticker-map/search?q=${encodeURIComponent(q)}` +
          (limit != null ? `&limit=${limit}` : ""),
      ).then((r) => r.results),
    // M5 U0 (AD-M5-14) — the EODHD availability-index search: case-insensitive prefix match on the
    // bare ticker, exact-first. Unwraps the { q, results } envelope to the bare-ticker rows.
    eodhdTickerSearch: (q: string, limit?: number) =>
      get<{ q: string; results: { ticker: string }[] }>(
        `/api/research/eodhd-tickers/search?q=${encodeURIComponent(q)}` +
          (limit != null ? `&limit=${limit}` : ""),
      ).then((r) => r.results),
    reparseBatch: (
      batchId: number,
      body: { reparse_dir: string; apply?: boolean; max_events?: number | null },
    ) => post<BulkReparseResult>(`/api/research/event-batches/${batchId}/reparse`, body),

    listSettings: () => get<Setting[]>("/api/research/settings"),
    listSettingScopes: () => get<SettingScope[]>("/api/research/settings/scopes"),
    getSetting: (scope: string, key: string) =>
      get<Setting>(
        `/api/research/settings/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
      ),
    putSetting: (scope: string, key: string, value: SettingValue) =>
      put<{ ok: boolean }>(
        `/api/research/settings/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
        { value },
      ),
    settingHistory: (scope: string, key: string, limit = 50) =>
      get<SettingHistoryRow[]>(
        `/api/research/settings/history?scope=${encodeURIComponent(scope)}&key=${encodeURIComponent(key)}&limit=${limit}`,
      ),

    // ── TERM M4.5 3a B2: per-strategy setting overrides ──
    getStrategySettings: (id: number) =>
      get<StrategyResolvedSetting[]>(`/api/research/strategies/${id}/settings`),
    // 3b: value is bounds-checked server-side; source_note (optional) is the D4 override justification.
    putStrategyOverride: (id: number, scope: string, key: string, value: SettingValue, sourceNote?: string | null) =>
      put<{ ok: boolean }>(
        `/api/research/strategies/${id}/settings/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
        { value, source_note: sourceNote ?? null },
      ),
    deleteStrategyOverride: (id: number, scope: string, key: string) =>
      del<{ ok: boolean }>(
        `/api/research/strategies/${id}/settings/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
      ),

    // ── M3 strategies / runs / bundle / provenance (briefs J, K, L) ──
    listStrategies: () => get<StrategyListItem[]>("/api/research/strategies"),
    // ── pre-M4 Brief A1 / D5 — strategy create (409 on an existing name) + edit ──
    createStrategy: (body: { name: string; source_code: string }) =>
      post<StrategyMutationResponse>("/api/research/strategies", body),
    editStrategy: (id: number, source_code: string) =>
      put<StrategyMutationResponse>(`/api/research/strategies/${id}`, { source_code }),
    // TERM F2 — delete a strategy (keep + orphan its runs). Returns the action taken: a strategy with
    // runs is "tombstoned" (hidden, runs kept); a zero-run strategy is "removed"; an already-deleted
    // one is a "noop".
    deleteStrategy: (id: number) =>
      del<{ action: "tombstoned" | "removed" | "noop"; id: number; orphaned_runs?: number }>(
        `/api/research/strategies/${id}`,
      ),
    // ── M4 Phase 2 — editor core + library (R3, R6, R7, R11, R12) ──
    // R3 — load one strategy's full current state (incl. source_code) for the editor.
    getStrategy: (id: number) => get<StrategyDetail>(`/api/research/strategies/${id}`),
    // R6 — Validate (no write). A validation failure throws a 400 ApiError, identical to Save's
    // mapping, so the editor renders one error surface for both.
    validateStrategy: (source_code: string) =>
      post<ValidateStrategyResult>("/api/research/strategies/validate", { source_code }),
    // R7 — the version-history list (newest first) and one version's source for the diff editor.
    listStrategyVersions: (id: number) =>
      get<StrategyVersionMeta[]>(`/api/research/strategies/${id}/versions`),
    getStrategyVersion: (id: number, version: number) =>
      get<StrategyVersionSource>(`/api/research/strategies/${id}/versions/${version}`),
    // R11 — the status badge read (DB version vs last-run version vs mirror match).
    getStrategyStatus: (id: number) =>
      get<StrategyStatus>(`/api/research/strategies/${id}/status`),
    // R12 — one strategy's run history (served by ix_runs_strategy); same Run shape as listRuns.
    listStrategyRuns: (id: number, limit = 50, offset = 0) =>
      get<Run[]>(`/api/research/strategies/${id}/runs?limit=${limit}&offset=${offset}`),
    // R13/R14 — the in-editor reference catalog (the contract surface, helpers, settings, event types,
    // fundamental definitions, the five parsed indicators + confluence notes). Virtual view, computed
    // on read; cached client-side (it changes only on a deploy).
    getCatalog: () => get<StrategyCatalog>("/api/research/strategies/catalog"),
    // M4 R8/D5: `kind` filters the list (scratch | standard); "all" sends no filter (every run).
    listRuns: (sort: RunSort = "created_at", limit = 50, offset = 0, kind: RunKindFilter = "all") =>
      get<Run[]>(
        `/api/research/runs?sort=${sort}&limit=${limit}&offset=${offset}` +
          (kind === "all" ? "" : `&kind=${kind}`),
      ),
    getRun: (id: number) => get<Run>(`/api/research/runs/${id}`),
    // RS (U.6, set 5 day): THE CHAIN read over a run's own draw history (plan section 10
    // criterion 1). Never 404 on a run with no chain -- read the response's own "reason" field.
    getRunChain: (id: number) => get<RunChainResponse>(`/api/research/runs/${id}/chain`),
    // Lane P7: the Chains page. Refusals arrive as "<word>: <plain sentence>" in the ApiError's body.detail.
    chains: () => get<ChainList>("/api/research/chains"),
    chain: (id: number) => get<ChainDetail>(`/api/research/chains/${id}`),
    createChain: (body: ChainCreateBody) => post<ChainWriteResult>("/api/research/chains", body),
    chooseChainCell: (id: number, body: { variation_id: number; ticker: string }) =>
      post<ChainWriteResult>(`/api/research/chains/${id}/cell`, body),
    stopChain: (id: number) => post<ChainWriteResult>(`/api/research/chains/${id}/stop`, {}),
    retryChain: (id: number) => post<ChainWriteResult>(`/api/research/chains/${id}/retry`, {}),
    // RS (U.6): JUDGE THIS RUN, the Results explorer's own trigger for a grading pass (plan
    // section 10 criterion 3). Grouped under research (not routes/judge/api.ts, a DIFFERENT
    // lane's own file for this night set) because the action lives on the run explorer page.
    // 404/409 surface as an ApiError whose .body.detail is the server's own refusal sentence.
    // Lane RP (set 16b, `#423`): widened with optional (variationId, ticker) so JUDGE THIS REPLAY
    // (a sweep cell's own grading trigger, ReplayControl.tsx) can reuse this one call rather than a
    // second, competing wrapper over the SAME route. Called with one argument (the existing
    // RunExplorerPage.tsx call site), the URL is byte-identical to before this lane.
    judgeRun: (runId: number, variationId?: number, ticker?: string) => {
      const p = new URLSearchParams();
      if (variationId !== undefined && ticker !== undefined) {
        p.set("variation_id", String(variationId));
        p.set("ticker", ticker);
      }
      const qs = p.toString();
      return post<JudgeRunResult>(`/api/judge/runs/${runId}/judge${qs ? `?${qs}` : ""}`, {});
    },
    // Lane RP (set 16b, `#423`), docs/units/RP/criteria.md AC-1: request (or read the state of)
    // one sweep cell's saved replay. `retry=true` sends retry=1, the one query parameter that
    // unblocks a previously failed replay (AC-4); an ordinary REPLAY press omits it.
    replayCell: (runId: number, variationId: number, ticker: string, retry = false) => {
      const p = new URLSearchParams();
      p.set("variation_id", String(variationId));
      p.set("ticker", ticker);
      if (retry) p.set("retry", "1");
      return post<ReplayCellResult>(`/api/judge/runs/${runId}/replay?${p.toString()}`, {});
    },
    // TERM Runs rebuild — the global summary aggregate (counts by status, success rate, best Sharpe,
    // the in-flight run). `kind` scopes it to the same slice the list shows; "all" sends no filter.
    runsSummary: (kind: RunKindFilter = "all") =>
      get<RunsSummary>(`/api/research/runs/summary` + (kind === "all" ? "" : `?kind=${kind}`)),
    // Unit-4 D1 — overlay N completed runs (capped at 6, reject-don't-truncate). The backend
    // returns the runs in the requested order; over-cap throws 422 compare_too_large, and a
    // non-succeeded id throws 409 run_not_comparable.
    compareRuns: (runIds: number[]) =>
      post<{ runs: RunComparison[] }>("/api/research/runs/compare", { run_ids: runIds }),
    getRunBundle: (id: number) => get<RunBundle>(`/api/research/runs/${id}/bundle`),
    // M4 R8: `scratch` tags the run kind='scratch' (an editor test run) through the SAME dispatch.
    createRun: (strategy_id: number, dataset_id: number, name?: string | null, scratch = false) =>
      post<CreateRunResponse>("/api/research/jobs", {
        kind: "run_backtest",
        strategy_id,
        dataset_id,
        // Backend trims + collapses "" -> NULL, so it's safe to forward an empty string here too.
        name: name ?? null,
        scratch,
      }),
    // M4 R8 (§9 friction 3): the daily-price preflight for an editor test run — which of these symbols
    // have a ready daily price dataset. The editor blocks the run when `unresolved` is non-empty.
    testRunPreflight: (symbols: string[]) =>
      post<{ resolved: Record<string, string>; unresolved: string[] }>(
        "/api/research/test-run/preflight",
        { symbols },
      ),
    getProvenance: (scope: string, key: string) =>
      get<Provenance>(
        `/api/research/provenance/${encodeURIComponent(scope)}.${encodeURIComponent(key)}`,
      ),
    // 3b B1.4 — author/edit a USER-AUTHORED setting's provenance (the four prose fields + a structured
    // bounds_spec). The route refuses non-empty-four-field + well-formed-bounds_spec + non-built-in-scope.
    putProvenance: (
      scope: string,
      key: string,
      body: { source: string; mechanism: string; sensitivity: string; bounds: string; bounds_spec: BoundsSpec },
    ) =>
      put<{ ok: boolean }>(
        `/api/research/provenance/${encodeURIComponent(scope)}.${encodeURIComponent(key)}`,
        body,
      ),

    // ── Pre-M4 Brief D (D6) — job-scoped run controls (cancel + logs) ──
    // Cancel a job (DELETE /jobs/{id}, backend cancel_job). The key is the JOB id
    // (Run.job_id), NOT the run id. Returns 200 {cancelled:true,...} for a pending
    // job finalized directly OR 202 {status:'cancelling',...} for a running backtest;
    // both are res.ok so del<T> resolves on both — the caller branches on the BODY.
    // 409 (terminal) / 404 (unknown) throw via mutate<T>.
    cancelJob: (jobId: number) => del<CancelResult>(`/api/research/jobs/${jobId}`),
    // Log lines for a job (GET /jobs/{id}/logs, backend get_job_logs). since_id pages
    // only new lines; limit caps the batch. No 404 — an unknown job returns an empty array.
    getJobLogs: (jobId: number, opts: { sinceId?: number; limit?: number } = {}) => {
      const q = new URLSearchParams();
      if (opts.sinceId != null) q.set("since_id", String(opts.sinceId));
      if (opts.limit != null) q.set("limit", String(opts.limit));
      const qs = q.toString();
      return get<JobLog[]>(`/api/research/jobs/${jobId}/logs${qs ? `?${qs}` : ""}`);
    },
    // Job lifecycle audit trail (GET /jobs/{id}/events, backend get_job_events).
    // Bare array, chronological; no 404 — an unknown job returns an empty array.
    getJobEvents: (jobId: number) =>
      get<JobEvent[]>(`/api/research/jobs/${jobId}/events`),
    // Dispatch an event-driven backtest (POST /event-runs, backend create_event_run,
    // status 201). Sibling of createRun — its own path so the price-run dispatch is
    // byte-unchanged. knowledge_from/knowledge_to are epoch SECONDS UTC, knowledge_to
    // exclusive. Reuses CreateRunResponse ({id, run_id, status}); post<T> resolves on
    // any res.ok so 201 is handled transparently. 404 (missing strategy/universe) and
    // 422 (not event-driven / bad window) throw via mutate<T>.
    createEventRun: (body: {
      strategy_id: number;
      universe_id: number;
      knowledge_from: number;
      knowledge_to: number;
      name?: string | null;
      // M4 R8: tag the event run kind='scratch' (an editor test run) through the same dispatch.
      scratch?: boolean;
    }) => post<CreateRunResponse>("/api/research/event-runs", body),

    // ── M3.5 event feeds (brief K): the live feeds page ──
    // Bare array, one entry per canonical event type (sec_13d, analyst_rating, news). recentN
    // defaults to 20 so a feed can show its 20 most-recent rows (the news feed reveals 5, then
    // the rest via "load more"); the route clamps recentN to [1, 25].
    getEventFeeds: (recentN = 20) =>
      get<EventFeedSummary[]>(`/api/research/event-feeds?recent_n=${recentN}`),

    // ── Benzinga analyst-ratings ingestion controls (brief BZ5.3) ──
    benzingaBackfill: (body: { max_lookback_months: number }) =>
      post<{ dispatched: boolean; max_lookback_months: number }>(
        "/api/research/benzinga/backfill", body),
    benzingaPoll: () =>
      post<{ dispatched: boolean }>("/api/research/benzinga/poll", {}),
    benzingaStatus: () => get<BenzingaStatus>("/api/research/benzinga/status"),

    // ── M3.75 FUND-4 §10: SEC XBRL fundamentals controls ──
    // Backfill (fetch) / re-fetch (refresh) fundamentals for an explicit CIK list OR a universe
    // over a window. The backend dispatches the supervised systemd fetcher; 202 on dispatch, 409
    // if a fundamentals backfill is already running. kind is supplied by the caller (fetch | refresh).
    fundamentalsBackfill: (
      body: { ciks?: number[]; universe_id?: number; knowledge_from?: number; knowledge_to?: number },
    ) =>
      post<{ dispatched: boolean; job: string; kind: string; cik_count: number }>(
        "/api/research/fundamentals/backfill", { ...body, kind: "fetch" }),
    // Re-fetch is the SAME route with kind=refresh (idempotent vintage-keeping re-fetch, R7).
    fundamentalsRefresh: (
      body: { ciks?: number[]; universe_id?: number; knowledge_from?: number; knowledge_to?: number },
    ) =>
      post<{ dispatched: boolean; job: string; kind: string; cik_count: number }>(
        "/api/research/fundamentals/backfill", { ...body, kind: "refresh" }),
    // ANA-2 §2.2: the static plain-English definitions of every figure and ratio. Cached forever
    // by the caller (staleTime: Infinity) — it never changes between fetches.
    fundamentalsDefinitions: () =>
      get<FundamentalsDefinitions>("/api/research/fundamentals/definitions"),
    // The recent fundamentals batches + the store's newest filing date (the status line + the
    // coverage view's source). quality_report on each row is the §3.4 report as a JSON STRING.
    fundamentalsStatus: () => get<FundamentalsStatus>("/api/research/fundamentals/status"),
    // The ticker lookup's yearly accounts of one company (lane LK1): ten fiscal years, per-share figures in today's shares.
    fundamentalsHistory: (cik: number, ticker: string) =>
      get<FundamentalsHistory>(`/api/research/fundamentals/history?cik=${cik}&ticker=${encodeURIComponent(ticker)}`),
    // The point-in-time fact inspector: the as-known-on-D read for each core concept (no lookahead).
    // as_of is epoch seconds (the UI converts a date input before calling).
    fundamentalsFacts: (cik: number, asOf: number) =>
      get<FundamentalsFacts>(`/api/research/fundamentals/facts?cik=${cik}&as_of=${asOf}`),
    // The bounded compare grid (ANA-6 §6.1): one {facts, ratios} cell per (cik, as_of). The
    // company-and-date product is capped server-side (422 compare_too_large); the UI enforces the
    // same cap client-side so the 422 is a backstop, not the normal path.
    fundamentalsCompare: (ciks: number[], asOfs: number[]) =>
      post<FundamentalsCompare>("/api/research/fundamentals/compare", { ciks, as_ofs: asOfs }),
    // Clear a fundamentals backfill stuck in 'running' after a crash/restart (the §3.3 reaper),
    // which would otherwise 409 every future fetch. Safe: flips only stale-heartbeat batches.
    fundamentalsReapOrphaned: () =>
      post<{ batches_reaped: number[] }>("/api/research/fundamentals/reap-orphaned", {}),

    // ── Dashboard-triggered EDGAR 13D ingestion (brief A2 / D3, §A2.2) ──
    // The cap (max_filings) is REQUIRED: the backend fetcher fails closed without it, so a
    // dashboard backfill is always bounded. quarter_range is e.g. "2024Q1-2024Q1".
    startEdgarBackfill: (body: { quarter_range: string; max_filings: number }) =>
      post<{ dispatched: boolean; quarter_range: string; max_filings: number }>(
        "/api/research/event-batches/backfill", body),
    // W7 "Fetch all": the server-authoritative plan (earliest 1994Q3 -> current quarter + the
    // locked fail-closed cap). The UI posts this verbatim to startEdgarBackfill.
    getFetchAllPlan: () =>
      get<{ quarter_range: string; max_filings: number; current_quarter: string }>(
        "/api/research/event-batches/fetch-all-plan"),
    // Item 5a: clear a stuck `running` batch orphaned by process death (deploy SIGTERM / OOM /
    // crash), which would otherwise 409 every future dispatch. Safe: the reaper only flips
    // stale-heartbeat batches, never a live run. Returns what it cleared (empty when nothing stuck).
    reapOrphanedBatches: () =>
      post<ReapOrphanedResult>("/api/research/event-batches/reap-orphaned", {}),
    // Load an operator-staged canonical JSON-lines file (analyst_rating); returns the terminal batch state.
    loadEventBatch: (body: {
      source_file_path: string;
      policy?: "quarantine" | "abort_on_first_error";
    }) =>
      post<{ batch_id: number; status: string; event_count: number; quarantine_count: number }>(
        "/api/research/event-batches/load", body),

    // ── Pre-M4 Brief C (D1) — the event-operations console reads + actions ──
    // The operational batch list (GET /event-batches): the envelope shape, with
    // optional source/status/since filters + limit/offset paging (the listEvents
    // URLSearchParams precedent). Each value is only set when present so an empty
    // opts object lists everything.
    listEventBatches: (
      opts: { source?: string; status?: string; since?: number; limit?: number; offset?: number } = {},
    ) => {
      const q = new URLSearchParams();
      if (opts.source) q.set("source", opts.source);
      if (opts.status) q.set("status", opts.status);
      if (opts.since != null) q.set("since", String(opts.since));
      if (opts.limit != null) q.set("limit", String(opts.limit));
      if (opts.offset != null) q.set("offset", String(opts.offset));
      const qs = q.toString();
      return get<EventBatchPage>(`/api/research/event-batches${qs ? `?${qs}` : ""}`);
    },
    // One batch's row (GET /event-batches/{id}); 404 -> ApiError {error: "batch_not_found"}.
    getEventBatch: (id: number) =>
      get<EventBatch>(`/api/research/event-batches/${id}`),
    // The batch's parsed quality report (GET /event-batches/{id}/quality); the
    // 404/409 bodies ({error}) are read via (err as ApiError).body.
    getEventBatchQuality: (id: number) =>
      get<EventBatchQuality>(`/api/research/event-batches/${id}/quality`),
    // Apply a single-row correction (POST /event-batches/correction). Collection
    // POST — the event_id is in the BODY, not the path. The 409/422 bodies (the
    // company-link diff, the validation class/message) are read via ApiError.body.
    postEventBatchCorrection: (body: {
      event_id: number;
      corrected_payload: Record<string, unknown>;
      companies: Array<Record<string, unknown>>;
      trigger_note: string;
    }) =>
      post<CorrectionResult>("/api/research/event-batches/correction", body),
    // Roll back every active event in a batch (POST /event-batches/{id}/rollback).
    // `confirm` must EXACTLY equal "ROLLBACK BATCH <id>"; the 400 mismatch body
    // echoes `expected`, read via ApiError.body.
    postEventBatchRollback: (id: number, body: { confirm: string; trigger_note: string }) =>
      post<RollbackResult>(`/api/research/event-batches/${id}/rollback`, body),
    // Manually trigger the catalog reconciliation backstop (POST /admin/reconcile-event-catalog);
    // empty body, returns the counts + any orphans found.
    reconcileEventCatalog: () =>
      post<ReconcileResult>("/api/research/admin/reconcile-event-catalog", {}),
    // Create a share link for a batch (POST /event-batches/{id}/share); returns
    // the public URL + the persisted row.
    createEventBatchShare: (id: number, body: CreateBatchShareBody = {}) =>
      post<CreateBatchShareResponse>(`/api/research/event-batches/${id}/share`, body),
    // List a batch's active (non-revoked) shares (GET /event-batches/{id}/shares); bare array.
    listEventBatchShares: (id: number) =>
      get<EventBatchShare[]>(`/api/research/event-batches/${id}/shares`),
    // NB: there is NO exportEventBatch method — GET /event-batches/{id}/export returns a
    // file attachment, so BatchDetail uses a raw download anchor href (C.2 item 8), never get<T>.

    // ── M3 K.6 permalink shares (spec §5.6; DECISIONS #29) ──
    // Per-run active-shares list (additive over spec §5.6 to back the K.6
    // management section in RunExplorer; routed at GET /runs/{id}/shares).
    listActiveSharesForRun: (runId: number) =>
      get<Share[]>(`/api/research/runs/${runId}/shares`),
    createShare: (runId: number, body: CreateShareBody = {}) =>
      post<CreateShareResponse>(`/api/research/runs/${runId}/share`, body),
    getShare: (uuid: string) =>
      get<ShareWithRun>(`/api/research/shares/${encodeURIComponent(uuid)}`),
    patchShare: (uuid: string, body: PatchShareBody) =>
      patch<Share>(`/api/research/shares/${encodeURIComponent(uuid)}`, body),
    deleteShare: (uuid: string) =>
      del<Share>(`/api/research/shares/${encodeURIComponent(uuid)}`),
  },

  // ── Lane DL (night plan 2026-09-09 section 10): the Data Library Overview's display-only Alpaca
  // bars route. A separate top-level key, not nested under `research`, because dashboard/backend/
  // routes/display.py is deliberately its own file with no database and no research_lab import
  // (display data must never enter a research path, root CLAUDE.md, "Parameter provenance and
  // data boundaries"), so its one typed call lives apart from every `research.*` call too.
  display: {
    // GET /api/display/bars: cached, display-only bars for one symbol + timeframe. Defaults match
    // the route's own defaults (SPY, 1Hour) so a caller asking for the index chart needs no args.
    // Lane DX round 3 (F1, plan item 2 criterion 4): a third, optional `start` argument, an ISO
    // date (YYYY-MM-DD), appended to the query string only when the caller actually supplies one.
    // The route itself already accepts and clamps `start` (routes/display.py); this was the missing
    // client half, so the daily index chart below can ask for a five-year window instead of the
    // route's own ten-day hourly default.
    getBars: (symbol = "SPY", timeframe = "1Hour", start?: string) =>
      get<DisplayBarsResponse>(
        `/api/display/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}` +
          (start ? `&start=${encodeURIComponent(start)}` : ""),
      ),

    // Lane OP (Overview terminal): the seven read-only EODHD market routes lane OB shipped and
    // merged (dashboard/backend/routes/display.py). Every one of these is a plain GET with no
    // body, matching the pattern above; the route itself owns caching and the daily call budget,
    // so this file adds no cache of its own.
    // GET /api/display/quotes, symbols=A,B,C: one row per symbol, chunked and isolated server
    // side, so a caller always gets one entry back per symbol it asked for.
    getQuotes: (symbols: string[]) =>
      get<{ quotes: MarketQuote[] }>(`/api/display/quotes?symbols=${encodeURIComponent(symbols.join(","))}`),
    // GET /api/display/history, symbol and period: OHLC plus volume for one instrument.
    // interval "hour" asks for Alpaca's hourly bars (periods 1d and 1w); left out, the daily bars.
    getHistory: (symbol: string, period = "1y", interval?: "hour") =>
      get<HistoryResponse>(`/api/display/history?symbol=${encodeURIComponent(symbol)}&period=${encodeURIComponent(period)}${interval ? `&interval=${interval}` : ""}`),
    // GET /api/display/news, ticker=: the headlines that name the company, newest first.
    getNews: (ticker: string) => get<NewsResponse>(`/api/display/news?ticker=${encodeURIComponent(ticker)}`),
    // GET /api/display/yield-curve: every tenor with its level and its move in basis points.
    getYieldCurve: () => get<{ tenors: YieldTenor[] }>("/api/display/yield-curve"),
    // GET /api/display/commodities: exactly the codes EODHD answered, each with its own grain.
    getCommodities: () => get<{ commodities: CommodityRow[] }>("/api/display/commodities"),
    // GET /api/display/search, q=: name, exchange, currency, type and ISIN, covering equities,
    // funds, FX and crypto (the two existing local ticker searches cannot).
    search: (q: string) => get<{ results: SearchResult[] }>(`/api/display/search?q=${encodeURIComponent(q)}`),
    // GET /api/display/market-news: projected straight from the events store, no upstream call.
    getMarketNews: (limit = 20) => get<{ news: MarketNewsItem[] }>(`/api/display/market-news?limit=${limit}`),
    // GET /api/display/market-session: derived from the newest quote timestamp this server
    // process has actually seen, never an invented holiday calendar.
    getMarketSession: () => get<MarketSessionResponse>("/api/display/market-session"),
  },
};

// One display bar, exactly as dashboard/backend/routes/display.py's _to_display_bar returns it:
// the ISO time Alpaca sent (for a human-readable hover) plus epoch_seconds (a plain UNIX-timestamp
// number) a chart needs for an intraday series. Kept here, not in types/research.ts, because this
// lane's areas do not include that file and this type has exactly one caller today.
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

// Item 7 / D13: register the external server syncs with the global-refresh registry at module
// init. api.ts is eagerly imported across the app, so this fires on load and the one header
// refresh re-triggers each (then React Query refetches every query). Throttled so rapid refresh
// clicks do not hammer the rate-limited upstream APIs (Garmin / Google Calendar / Benzinga).
const SYNC_THROTTLE_MS = 15_000; // minimum gap between forced external syncs
registerSync("Garmin health", () => api.life.garminRefresh(), SYNC_THROTTLE_MS);
registerSync("Garmin activities", () => api.life.garminActivitiesRefresh(), SYNC_THROTTLE_MS);
registerSync("Calendar", () => api.life.calendarSync(), SYNC_THROTTLE_MS);
registerSync("Analyst ratings", () => api.research.benzingaPoll(), SYNC_THROTTLE_MS);
