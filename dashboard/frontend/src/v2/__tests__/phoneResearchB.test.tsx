import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Generator from "../pages/research/generator/Generator";
import Chains from "../pages/research/Chains";
import Markets from "../pages/research/Markets";
import { SettingsProvider, STORAGE_KEY } from "../settings";
import { APPS } from "../shell/sections";

// The research tools on a phone (DECISIONS #484): only PAUSE, STOP and RESUME on a search and STOP on a chain
// stay; every other write button is laptop only. The layout is forced with the stored layout setting.

const m = vi.hoisted(() => ({
  // Chains
  chains: vi.fn(), chain: vi.fn(), createChain: vi.fn(), chooseChainCell: vi.fn(), stopChain: vi.fn(), retryChain: vi.fn(),
  getRunCells: vi.fn(), listStrategies: vi.fn(), getStrategyStatus: vi.fn(),
  // Universes
  listDatasets: vi.fn(), priceUniverseMemberDetail: vi.fn(), priceUniverseRuns: vi.fn(), priceUniverseMembers: vi.fn(),
  priceUniverseSectorMix: vi.fn(), apiGet: vi.fn(),
}));
const datasetsApi = vi.hoisted(() => ({
  fetchDatasets: vi.fn(), fetchEventFeeds: vi.fn(), fetchFundamentalsStatus: vi.fn(), fetchUniverseMembers: vi.fn(),
  createSeries: vi.fn(), fetchToLatest: vi.fn(), deleteSeries: vi.fn(), restoreSeries: vi.fn(),
}));
vi.mock("../data/datasetsApi", async (orig) => ({ ...(await orig<typeof import("../data/datasetsApi")>()), ...datasetsApi }));
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const wait = () => new Promise(() => undefined);
  const { apiGet, ...research } = m;
  return {
    ...real,
    apiGet: (...a: Parameters<typeof real.apiGet>) => apiGet(...a),
    api: {
      ...real.api,
      research: { ...real.api.research, ...research },
      display: { getQuotes: wait, getHistory: wait, getYieldCurve: wait, getCommodities: wait, getMarketNews: wait, search: wait },
    },
  };
});

const app = APPS.find((a) => a.id === "research")!;
const gen = app.sections.find((s) => s.id === "generator")!;
const chainSection = app.sections.find((s) => s.id === "chains")!;
const lib = app.sections.find((s) => s.id === "data-library")!;
const setLayout = (layout: string) => localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout }));
const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const has = (name: string) => screen.queryAllByRole("button", { name }).length > 0;
const qc = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

// ---- Generator: fetch stub ----
type Call = { method: string; url: string };
let calls: Call[] = [];
const V3 = "/api/research/generator/v3";
const batch = {
  id: 1, template_sha256: "sha-d", universe_id: 3, searched_universe_id: 4, group_label: "GROUP_LARGE_STOCKS", display_name: "Large stocks", members: ["AAA", "BBB"], kept_back: ["BBB"],
  seed: 7, data_start: "2004-01-01", screen_start: "2004-01-01", screen_end: "2006-12-31", optimise_start: "2007-01-01", optimise_end: "2012-12-31",
  later_start: "2013-01-01", later_end: "2020-12-31", calendar_id: "cal-1", approved_at: 1761782400, control: { state: "running", since: null },
  synthetic: false, walkforward: ["2013-01-01", "2016-12-31"],
};
const tpl = (name: string, sha: string) => ({ name, display_name: name, sha256: sha, saved_at: 1761782400, theory_count: 2 });
const answers: Record<string, unknown> = {
  "GET /api/research/generator/worker": { status: "online" },
  [`GET ${V3}/templates`]: [tpl("Rebound", "sha-d"), tpl("Waiting one", "sha-w")],
  [`GET ${V3}/batches`]: [batch],
  [`GET ${V3}/batches/1/tree`]: {
    batch, template: { name: "rebound", display_name: "Rebound", sha256: "sha-d" },
    theories: [{ id: 1, name: "t1", display_name: "Theory 1", choices: {}, runs: [{ stage: "screen", run_id: 10, status: "running" }], search: null, final: "not run", saved: [], stop: null }],
  },
  [`GET ${V3}/trials-per-day`]: { time_zone: "Europe/Paris", today: "2026-09-01", days: [{ day: "2026-09-01", trials: 5, searches: 1 }] },
  "GET /api/research/datasets?status=active": [],
  [`POST ${V3}/batches/1/pause`]: { state: "paused" },
};
function stubFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ method, url });
    const hit = answers[`${method} ${url}`];
    if (hit === undefined) throw new Error(`no answer for ${method} ${url}`);
    return { ok: true, status: 200, statusText: "OK", json: async () => hit };
  }));
}
async function mountGenerator() {
  // The Generator reads through the real apiGet (which uses the fetch stub); the Universes tab uses the stand-in.
  const real = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  m.apiGet.mockImplementation(real.apiGet);
  return render(
    <QueryClientProvider client={qc()}><MemoryRouter><Generator app={app} section={gen} /></MemoryRouter></QueryClientProvider>,
  );
}

// ---- Chains ----
const ts = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const mkChain = (id: number, state: string, word: string, group: string, step: string, extra: Record<string, unknown> = {}) => ({
  id, strategy_id: 10 + id, strategy_version: 1, calendar_id: `chain-${id}`,
  screen_start_ts: ts("2004-01-01"), optimise_start_ts: ts("2009-01-01"), walkforward_start_ts: ts("2014-01-01"), walkforward_end_ts: ts("2018-01-01"),
  state, state_word: word, group, step, ended_at_step: null, stop_requested: 0, note: null, note_word: null, reason_word: null, reason_sentence: null,
  display_name: `Chain ${id}`, created_at: ts("2026-10-20"), updated_at: ts("2026-10-23"), duel_chain_id: null, ...extra,
});
const REFUSED = { ended_at_step: "walkforward", reason_word: "x", reason_sentence: "Too short." };
const CHAINS = [
  mkChain(7, "walking-forward", "WALKING FORWARD", "active", "walkforward"),
  mkChain(5, "choosing-cell", "CHOOSE A CELL", "waiting-on-you", "choose"),
  mkChain(2, "refused", "REFUSED", "retryable", "walkforward", REFUSED),
];
function mountChains() {
  return render(
    <QueryClientProvider client={qc()}><MemoryRouter><Chains app={app} section={chainSection} /></MemoryRouter></QueryClientProvider>,
  );
}
const CELL = {
  variation_id: 14, ticker: "AAPL", param_json: null, total_return: 0.3, sharpe_ratio: 1.4, max_drawdown: -0.1, num_trades: 60, win_rate: 0.5,
  total_fees_paid: 1, total_slippage_cost: 1, profit_factor: 1.2, skew: null, kurtosis: null,
};

// ---- Data library ----
function mountLibrary(tab: string) {
  const r = render(
    <QueryClientProvider client={qc()}>
      <SettingsProvider><MemoryRouter><Markets app={app} section={lib} /></MemoryRouter></SettingsProvider>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: tab }));
  return r;
}
const price = (id: number, symbol: string) => ({
  id, name: `${symbol} eodhd 1d`, source: "eodhd", symbol, timeframe: "1d", start_ts: 0, end_ts: 0, status: "ready", rows: 100,
  covered_start_ts: day("2020-01-01"), covered_end_ts: day("2026-10-10"), last_fetched_at: day("2026-10-11"), dataset_type: "price_dataset",
});
const uni = {
  ...price(7, ""), name: "tech_five", display_name: "Tech five", symbol: "", dataset_type: "price_universe", member_count: 1,
  window_start_ts: null, window_end_ts: null,
};
const MEMBER = {
  ticker: "AAPL", name: "Apple Inc.", added_at: 0, first_bar_ts: day("1980-12-12"), last_bar_ts: day("2026-10-20"), bars: 11000,
  sector: "Technology", sic: "3571", industry: "Electronic Computers", sector_missing: null, industry_missing: null,
};

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  stubFetch();
  m.chains.mockResolvedValue({ chains: CHAINS, reserve_cut_ts: 1672531200 });
  m.chain.mockImplementation(async (id: number) => ({
    ...CHAINS.find((c) => c.id === id)!, optimise_run_id: id === 5 ? 50 : null, walkforward_run_id: null, transitions: [],
  }));
  m.getRunCells.mockResolvedValue({ run_id: 50, cell_count: 1, member_count: 1, variation_count: 1, summary: {}, cells: [CELL] });
  m.listStrategies.mockResolvedValue([]);
  m.getStrategyStatus.mockResolvedValue({ sweep_params: [] });
  datasetsApi.fetchDatasets.mockResolvedValue([price(1, "AAPL"), price(2, "MSFT")]);
  datasetsApi.fetchEventFeeds.mockResolvedValue([]);
  datasetsApi.fetchFundamentalsStatus.mockResolvedValue({
    source: "sec", recent_batches: [], latest_filing_date: day("2026-08-26"), total_facts_on_record: 1, per_year: [],
  });
  datasetsApi.fetchUniverseMembers.mockResolvedValue({ universe_id: 7, members: ["AAPL"] });
  m.listDatasets.mockResolvedValue([uni]);
  m.priceUniverseSectorMix.mockResolvedValue({ universes: [] });
  m.priceUniverseMemberDetail.mockResolvedValue({ universe_id: 7, name: "x", as_of: 0, members: [MEMBER] });
  m.priceUniverseRuns.mockResolvedValue({ universe_id: 7, runs: [] });
  m.priceUniverseMembers.mockResolvedValue({ universe_id: 7, members: ["AAPL"] });
  m.apiGet.mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  for (const f of [...Object.values(m), ...Object.values(datasetsApi)]) f.mockReset();
});

describe("research tools on a phone", () => {
  it("b1 Generator on phone keeps PAUSE and STOP, drops the write buttons; PAUSE posts the pause call", async () => {
    setLayout("phone");
    await mountGenerator();
    fireEvent.click((await screen.findAllByTestId("search-tiles")).length ? document.querySelector(".sx-tile[data-batch=\"1\"]")! : document.body);
    await waitFor(() => expect(has("PAUSE")).toBe(true));
    expect(has("STOP")).toBe(true);
    expect(has("RESUME")).toBe(true);
    for (const name of ["APPROVE AND RUN", "PREVIEW", "ADD TEMPLATE", "USE"]) expect(has(name)).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "PAUSE" }));
    const win = screen.getByRole("dialog", { name: "Pause the batch" });
    fireEvent.click(within(win).getByRole("button", { name: "PAUSE" }));
    await waitFor(() => expect(calls).toContainEqual({ method: "POST", url: `${V3}/batches/1/pause` }));
  });

  it("b2 Chains on phone keeps STOP and drops START, RETRY and CHOOSE", async () => {
    setLayout("phone");
    mountChains();
    await waitFor(() => expect(document.querySelector("[data-chain]")).not.toBeNull());
    fireEvent.click(document.querySelector('[data-chain="7"]')!);
    await screen.findByTestId("chain-actions");
    await waitFor(() => expect(has("STOP")).toBe(true));
    expect(has("Start a chain")).toBe(false);
    fireEvent.click(document.querySelector('[data-chain="2"]')!);
    await waitFor(() => expect(m.chain).toHaveBeenCalledWith(2));
    expect(has("RETRY")).toBe(false);
    fireEvent.click(document.querySelector('[data-chain="5"]')!);
    await waitFor(() => expect(m.getRunCells).toHaveBeenCalled());
    await screen.findByText("AAPL");
    expect(has("CHOOSE")).toBe(false);
  });

  it("b3 the Datasets tab on phone has no ADD DATA, FETCH, DELETE or REFRESH STALE", async () => {
    setLayout("phone");
    mountLibrary("Datasets");
    fireEvent.click((await screen.findAllByTestId("kind-card")).find((c) => c.getAttribute("data-name") === "Daily prices, US")!);
    await screen.findByTestId("ds-table");
    expect(screen.getAllByText("AAPL").length).toBeGreaterThan(0);
    for (const name of ["Add data", "FETCH", "DELETE"]) expect(has(name)).toBe(false);
    expect(screen.queryAllByRole("button", { name: /^Refresh/ })).toHaveLength(0);
  });

  it("b4 the Universes tab on phone has no NEW, ADD MEMBERS, EDIT, DELETE or REMOVE", async () => {
    setLayout("phone");
    mountLibrary("Universes");
    await screen.findByTestId("u-head");
    fireEvent.click(document.querySelector<HTMLElement>('[data-u="7"] button')!);
    await screen.findByTestId("u-members");
    await screen.findByText("AAPL");
    for (const name of ["New list", "ADD MEMBERS", "EDIT", "DELETE", "REMOVE AAPL"]) expect(has(name)).toBe(false);
  });

  it("b5 on desktop the buttons hidden in b1 to b4 are present (guard)", async () => {
    setLayout("desktop");
    const g = await mountGenerator();
    await screen.findByTestId("search-tiles");
    fireEvent.click(screen.getAllByRole("button", { name: "Start a theory" })[0]);
    for (const name of ["Add a template", "ADD TEMPLATE"]) expect(has(name)).toBe(true);
    expect(screen.getAllByRole("button", { name: "USE" }).length).toBeGreaterThan(0);
    g.unmount();

    const c = mountChains();
    await waitFor(() => expect(document.querySelector("[data-chain]")).not.toBeNull());
    expect(has("Start a chain")).toBe(true);
    fireEvent.click(document.querySelector('[data-chain="2"]')!);
    await waitFor(() => expect(has("RETRY")).toBe(true));
    c.unmount();

    const d = mountLibrary("Datasets");
    fireEvent.click((await screen.findAllByTestId("kind-card")).find((c) => c.getAttribute("data-name") === "Daily prices, US")!);
    await screen.findByTestId("ds-table");
    expect(has("Add data")).toBe(true);
    expect(screen.queryAllByRole("button", { name: /^Refresh/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "FETCH" }).length).toBeGreaterThan(0);
    d.unmount();

    mountLibrary("Universes");
    await screen.findByTestId("u-head");
    fireEvent.click(document.querySelector<HTMLElement>('[data-u="7"] button')!);
    await screen.findByText("AAPL");
    for (const name of ["New list", "Add members", "Edit", "Delete"]) expect(has(name)).toBe(true);
  });
});
