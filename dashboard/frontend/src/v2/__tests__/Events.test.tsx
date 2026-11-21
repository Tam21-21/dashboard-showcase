import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Markets from "../pages/research/Markets";
import { SettingsProvider } from "../settings";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const m = vi.hoisted(() => ({
  listDatasets: vi.fn(),
  getEventFeeds: vi.fn(),
  searchEvents: vi.fn(),
  newsTop: vi.fn(),
  ratingActions: vi.fn(),
  earnings: vi.fn(),
}));
// The Markets tab's own calls never answer, so it stays in its loading state; every call the
// Events tab makes is a stand-in.
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const wait = () => new Promise(() => undefined);
  return {
    ...real,
    api: {
      ...real.api,
      research: { ...real.api.research, ...m },
      display: { getQuotes: wait, getHistory: wait, getYieldCurve: wait, getCommodities: wait, getMarketNews: wait, search: wait },
    },
  };
});
// The chart window is not under test: a stand-in that shows what it was asked to open.
vi.mock("../pages/research/markets/ChartWindow", () => ({
  default: ({ start }: { start: { name: string; symbol: string | null; dp: number } }) => (
    <div data-testid="chart-window" data-symbol={start.symbol ?? ""} data-name={start.name} data-dp={start.dp} />
  ),
}));

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "data-library")!;

const at = (iso: string) => Date.parse(iso) / 1000;
const NEWS_AT = at("2026-10-21T14:05:00Z");

const FEEDS = [
  { event_type: "news", total_count: 1200, latest_knowledge_at: at("2026-10-21T08:00:00Z"), recent_events: [], per_year: [] },
  { event_type: "analyst_rating", total_count: 10, latest_knowledge_at: at("2026-10-01T09:30:00Z"), recent_events: [], per_year: [] },
  { event_type: "sec_13d", total_count: 3, latest_knowledge_at: at("2026-10-20T10:00:00Z"), recent_events: [], per_year: [] },
];
const CATALOG = [
  {
    id: 91, name: "Holders", source: "sec", symbol: "", timeframe: "1d", start_ts: 0, end_ts: 0, status: "failed", rows: null,
    dataset_type: "event_series", event_type: "sec_13d", last_error: "docker exited -7",
  },
];

type Ev = { event_id: number; event_type: string; knowledge_at: number; occurred_at: number | null; source: string; tickers: string[]; payload: Record<string, unknown> };
const newsEv = (id: number, tickers: string[], headline: string, url: string | null = "https://news.test/a"): Ev => ({
  event_id: id, event_type: "news", knowledge_at: NEWS_AT - id * 60, occurred_at: null, source: "alpaca", tickers,
  payload: { headline, url, symbols: tickers },
});
const ratingEv = (id: number, p: Record<string, unknown>): Ev => ({
  event_id: id, event_type: "analyst_rating", knowledge_at: NEWS_AT - id * 60, occurred_at: null, source: "benzinga", tickers: ["MS"],
  payload: { raw_ticker: "MS", ...p },
});
const holdEv = (id: number, p: Record<string, unknown>): Ev => ({
  event_id: id, event_type: "sec_13d", knowledge_at: NEWS_AT - id * 60, occurred_at: null, source: "sec_edgar", tickers: [], payload: p,
});
const page = (events: Ev[], total = events.length, next: string | null = null) => ({
  events, next: next ? { after_cursor: next } : null, total_matching: total,
});

const ALL = [
  newsEv(1, ["AAPL", "MSFT"], "Apple unveils a new chip"),
  ratingEv(2, { action_company: "Upgrades", analyst_firm: "Morgan Stanley", old_grade: "Hold", new_grade: "Buy", pt_prior: 180, pt_current: 200 }),
  ratingEv(3, { action_company: "Maintains", analyst_firm: "Firm", old_grade: "Buy", new_grade: "Buy" }),
  holdEv(4, { filer_name: "Vanguard Group", subject_name: "Acme Corp", percent_owned: 7.2 }),
  holdEv(5, { filer_name: "Quiet Fund", subject_name: "Beta Inc", percent_owned: null }),
];

// An answer shaped like lane EA3-0's: rows newest first, closes with the before and after indexes.
type EarnRow = Record<string, unknown>;
const closesOf = (before: number | null, after: number | null) => {
  const dates = Array.from({ length: 12 }, (_, i) => `2026-07-${String(20 + i).padStart(2, "0")}`);
  const values = [330, 331, 332, 331, 333, 334, 335, 336, 337, 338, 339, 340].map((v, i) => v + i * 0.1);
  return { dates, values, before_index: before, after_index: after };
};
const earnRow = (o: EarnRow): EarnRow => ({
  ticker: "TST", company: "Test Co", quarter: "Q3", release_at: "2026-07-29T20:30:28Z", adjusted: true,
  eps_actual: 1.1, eps_estimate: 1.0, eps_surprise: 0.1, sales_actual: 5e9, sales_estimate: 4.9e9, sales_surprise: 0.0204,
  result: "BEAT", headline: "Test Co Q3 Adj. EPS $1.10 Beats $1.00 Estimate", corrected: false, first_printed: null, corrected_at: null,
  reaction: 0.02, closes: closesOf(5, 6), ...o,
});
const APPLE = earnRow({
  ticker: "AAPL", company: "Apple", release_at: "2026-07-30T20:30:28Z", eps_actual: 2.02, eps_estimate: 1.89, eps_surprise: 0.0688,
  sales_actual: 109417000000, sales_estimate: 108651000000, sales_surprise: 0.0071, reaction: -0.0735, closes: closesOf(5, 6),
  headline: "Apple Q3 Adj. EPS $2.02 Beats $1.89 Estimate, Sales $109.417B Beat $108.651B Estimate",
});
const NOT_YET = earnRow({ ticker: "NYT", company: "Not Yet Inc", release_at: "2026-07-30T10:00:00Z", reaction: null, closes: closesOf(5, null) });
const CORRECTED = earnRow({
  ticker: "COR", company: "Corrected Co", release_at: "2026-07-30T09:00:00Z", corrected: true, corrected_at: "2026-07-30T12:15:00Z",
  first_printed: { adjusted: true, eps_actual: 0.9, eps_estimate: 1.0, eps_word: "MISS", sales_actual: 4.8e9, sales_estimate: 4.9e9, sales_word: "MISS", headline: "Corrected Co Q3 Adj. EPS $0.90 Misses $1.00 Estimate" }, eps_actual: 1.1,
});
const MISSES = [1, 2, 3, 4].map((i) => earnRow({
  ticker: `MIS${i}`, company: `Miss ${i}`, release_at: `2026-07-2${i}T20:30:28Z`, eps_actual: 0.8, eps_estimate: 1.0, eps_surprise: -0.2, result: "MISS", reaction: -0.01,
}));
const INLINE = earnRow({ ticker: "INL", company: "Inline Co", release_at: "2026-07-20T20:30:28Z", eps_actual: 1.0, eps_estimate: 1.0, eps_surprise: 0, result: "INLINE" });
const MORE_BEATS = [1, 2, 3, 4, 5, 6].map((i) => earnRow({ ticker: `BT${i}`, company: `Beat ${i}`, release_at: `2026-07-1${i}T20:30:28Z` }));
const EARN_ROWS = [APPLE, NOT_YET, CORRECTED, ...MISSES, INLINE, ...MORE_BEATS]; // 14 in all: 9 beats, 4 misses, 1 inline
const EARN_ANSWER = { rows: EARN_ROWS, unread: 0 };

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsProvider>
        <MemoryRouter>
          <Markets app={app} section={section} />
        </MemoryRouter>
      </SettingsProvider>
    </QueryClientProvider>,
  );
}
async function openTab() {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Events" }));
  await screen.findByTestId("ev-feed");
}
const strip = (name: string) => document.querySelector<HTMLElement>(`[data-testid="feed-card"][data-name="${name}"]`)!;
const stateOf = (name: string) => strip(name).querySelector(".nl-st")?.textContent ?? null;
const chipOf = (label: string) => screen.getByRole("button", { name: new RegExp(`^${label}`) });
const feedRows = () => Array.from(screen.getByTestId("ev-feed").querySelectorAll<HTMLElement>("[data-ev]"));
const lastCall = () => m.searchEvents.mock.calls[m.searchEvents.mock.calls.length - 1][0];

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-22T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  m.listDatasets.mockResolvedValue(CATALOG);
  m.getEventFeeds.mockResolvedValue(FEEDS);
  m.newsTop.mockResolvedValue([{ ticker: "AAPL", count: 40 }, { ticker: "TSM", count: 10 }]);
  m.ratingActions.mockResolvedValue({
    total: 10,
    actions: [{ action: "Upgrades", count: 4 }, { action: "Downgrades", count: 3 }, { action: "Initiates Coverage On", count: 3 }],
  });
  m.earnings.mockResolvedValue(EARN_ANSWER);
  m.searchEvents.mockImplementation(async (a: { ticker?: string }) => (a.ticker ? page(ALL.slice(0, 3), 3) : page(ALL, 5)));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const f of Object.values(m)) f.mockReset();
});

describe("Data library, Events tab", () => {
  it("(e1) the tab row reads Markets, Datasets, Universes, Events, Fundamentals", () => {
    mount();
    const labels = Array.from(document.querySelectorAll(".mk-tabs .seg button")).map((b) => b.textContent);
    expect(labels).toEqual(["Markets", "Datasets", "Universes", "Events", "Fundamentals"]);
  });

  it("(e2) the feeds: one card per type with its state word, count and newest event", async () => {
    await openTab();
    await waitFor(() => expect(stateOf("News")).toBe("Working"));
    expect(stateOf("Analyst ratings")).toBe("Stale");
    expect(stateOf("Large holders")).toBe("Failed");
    expect(strip("News").querySelector(".lc-fig .num")!.textContent).toBe("1,200");
    expect(strip("Large holders").querySelector(".lc-fig .num")!.textContent).toBe("3");
    expect(strip("News").textContent).toContain("Newest");
    expect(Array.from(document.querySelectorAll('[data-testid="feed-card"]')).map((s) => s.getAttribute("data-name"))).toEqual([
      "News", "Analyst ratings", "Large holders",
    ]);
    for (const h of ["Feeds", "Most in the news, 30 days", "Analyst actions", "Earnings", "All events"]) {
      expect(screen.getByRole("heading", { name: h })).toBeInTheDocument();
    }
    expect(document.querySelector(".nl-say")!.textContent).toContain("events are stored");
  });

  it("(e3) most in the news: the tracks are count over the first count, and a ticker opens the chart", async () => {
    await openTab();
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('[data-top="TSM"]');
      expect(r).not.toBeNull();
      return r!;
    });
    expect(row.querySelector<HTMLElement>(".ev-hbar i")!.style.width).toBe("25%");
    expect(document.querySelector<HTMLElement>('[data-top="AAPL"] .ev-hbar i')!.style.width).toBe("100%");
    fireEvent.click(document.querySelector<HTMLElement>('[data-top="AAPL"]')!);
    const win = await screen.findByTestId("chart-window");
    expect(win.getAttribute("data-symbol")).toBe("AAPL.US");
    expect(win.getAttribute("data-name")).toBe("AAPL");
    expect(win.getAttribute("data-dp")).toBe("2");
  });

  it("(e3b) no news in 30 days says so", async () => {
    m.newsTop.mockResolvedValue([]);
    await openTab();
    expect(await screen.findByText("No news in the last 30 days")).toBeInTheDocument();
  });

  it("(e4) analyst actions: the legend, and a click filters and turns the ratings chip on", async () => {
    await openTab();
    const up = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('[data-act="Upgrades"]');
      expect(r).not.toBeNull();
      return r!;
    });
    const words = (r: HTMLElement) => Array.from(r.querySelectorAll("span, b, small")).map((s) => s.textContent);
    expect(words(up)).toEqual(["Upgrade", "4", "40%"]);
    expect(words(document.querySelector<HTMLElement>('[data-act="Initiates Coverage On"]')!)).toEqual(["Starts", "3", "30%"]);
    expect(document.querySelectorAll(".ev-kbar i").length).toBe(3);
    fireEvent.click(chipOf("Analyst ratings"));
    await waitFor(() => expect(chipOf("Analyst ratings")).toHaveAttribute("aria-pressed", "false"));
    fireEvent.click(up);
    await waitFor(() => expect(lastCall().action).toBe("Upgrades"));
    expect(lastCall().types).toContain("analyst_rating");
    expect(chipOf("Analyst ratings")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "upgrade ✕" })).toBeInTheDocument();
    fireEvent.click(document.querySelector<HTMLElement>('[data-act="Upgrades"]')!);
    await waitFor(() => expect(lastCall().action).toBeUndefined());
    expect(screen.queryByRole("button", { name: "upgrade ✕" })).toBeNull();
  });

  it("(e5) the row text for a rating, a rating without targets, and a 13D with and without a percent", async () => {
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    const text = (id: number) => document.querySelector(`[data-ev="${id}"] .tx`)!.textContent;
    expect(text(2)).toBe("Upgrade: Morgan Stanley, Hold to Buy, target $180 to $200 (+11.1%)");
    expect(text(3)).toBe("Keeps: Firm, Buy");
    expect(text(4)).toBe("Vanguard Group owns 7.2% of Acme Corp");
    expect(text(5)).toBe("Quiet Fund filed on Beta Inc");
    expect(text(1)).toBe("Apple unveils a new chip");
    expect(document.querySelector('[data-ev="2"] .tx .state')!.className).toContain("g");
    expect(document.querySelector('[data-ev="1"] .num')!.textContent).toBe("21 Oct, 14:04");
  });

  it("(e6) typing waits for the pause and sends q only", async () => {
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    const before = m.searchEvents.mock.calls.length;
    fireEvent.change(screen.getByPlaceholderText("Search headlines, firms, filings, tickers"), { target: { value: "apple" } });
    expect(m.searchEvents.mock.calls.length).toBe(before);
    await waitFor(() => expect(m.searchEvents.mock.calls.filter((c) => c[0].q === "apple")).toHaveLength(1), { timeout: 2000 });
    const call = lastCall();
    expect(call.q).toBe("apple");
    expect(call.ticker).toBeUndefined();
    expect(call.action).toBeUndefined();
    expect(call.limit).toBe(12);
  });

  it("(e7) the chips toggle types and the last one on cannot be turned off", async () => {
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    expect(chipOf("News").textContent).toContain("1,200");
    fireEvent.click(chipOf("News"));
    await waitFor(() => expect(lastCall().types).toBe("analyst_rating,sec_13d"));
    fireEvent.click(chipOf("Analyst ratings"));
    await waitFor(() => expect(lastCall().types).toBe("sec_13d"));
    const calls = m.searchEvents.mock.calls.length;
    fireEvent.click(chipOf("Large holders"));
    expect(chipOf("Large holders")).toHaveAttribute("aria-pressed", "true");
    expect(m.searchEvents.mock.calls.length).toBe(calls);
  });

  it("(e8) the total is grouped and Show more sends the cursor and appends", async () => {
    const first = Array.from({ length: 12 }, (_, i) => newsEv(100 + i, ["AAPL"], `Headline ${i}`));
    const second = [newsEv(300, ["AAPL"], "Older headline")];
    m.searchEvents.mockImplementation(async (a: { after_cursor?: string }) =>
      a.after_cursor === "c1" ? page(second, 1234, null) : page(first, 1234, "c1"));
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(12));
    expect(screen.getByTestId("ev-total").textContent).toBe("1,234");
    fireEvent.click(screen.getByRole("button", { name: "Show 48 more" }));
    await waitFor(() => expect(feedRows().length).toBe(13));
    expect(lastCall().after_cursor).toBe("c1");
    expect(lastCall().limit).toBe(48);
    expect(screen.queryByRole("button", { name: /^Show \d+ more$/ })).toBeNull();
  });

  it("(e8b) no match says so", async () => {
    m.searchEvents.mockResolvedValue(page([], 0));
    await openTab();
    expect(await screen.findByText("No events match")).toBeInTheDocument();
  });

  it("(e9) a news row opens its detail with OPEN ARTICLE and more on its first ticker", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    fireEvent.click(document.querySelector<HTMLElement>('[data-ev="1"]')!);
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText("NEWS, 21/10/2026")).toBeInTheDocument();
    expect(dlg.textContent).toContain("Alpaca news, Benzinga wire");
    expect(dlg.textContent).toContain("AAPL, MSFT");
    expect(dlg.textContent).toContain("21/10/2026 14:04 UTC");
    fireEvent.click(within(dlg).getByRole("button", { name: "OPEN ARTICLE" }));
    expect(open).toHaveBeenCalledWith("https://news.test/a", "_blank", "noopener");
    expect(await within(dlg).findByText("MORE ON AAPL")).toBeInTheDocument();
    const more = m.searchEvents.mock.calls.map((c) => c[0]).find((c) => c.ticker === "AAPL");
    expect(more.limit).toBe(7);
    await waitFor(() => expect(dlg.querySelectorAll("[data-ev]").length).toBe(2));
    expect(dlg.querySelector('[data-ev="1"]')).toBeNull();
    fireEvent.click(dlg.querySelector<HTMLElement>('[data-ev="2"]')!);
    await waitFor(() => expect(screen.getByRole("dialog").textContent).toContain("Benzinga analyst calendar"));
    expect(screen.getByRole("dialog").textContent).toContain("$180 to $200");
  });

  it("(e9b) a 13D detail has Holder, Company and Stake and no article button", async () => {
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    fireEvent.click(document.querySelector<HTMLElement>('[data-ev="4"]')!);
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText("LARGE HOLDERS, 21/10/2026")).toBeInTheDocument();
    for (const t of ["Holder", "Company", "Stake", "SEC EDGAR, Schedule 13D", "7.2%"]) expect(dlg.textContent).toContain(t);
    expect(within(dlg).queryByRole("button", { name: "OPEN ARTICLE" })).toBeNull();
  });

  it("(e10) Use in a run is disabled and says why", async () => {
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    fireEvent.click(feedRows()[0]);
    const b = await screen.findByRole("button", { name: "Use in a run" });
    expect(b).toBeDisabled();
    expect(b.getAttribute("title")).toBe("An event run needs an event strategy; building one from here comes in a later lane");
  });

  it("(e11) a failing news-top read shows its words in its own card and the feed still draws", async () => {
    m.newsTop.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The news table is locked." }, "/x"));
    await openTab();
    expect(await screen.findByText("The news table is locked.")).toBeInTheDocument();
    await waitFor(() => expect(feedRows().length).toBe(5));
    expect(document.querySelector('[data-act="Upgrades"]')).not.toBeNull();
  });

  it("(e12) no snake_case and no em dash on the tab", async () => {
    await openTab();
    await waitFor(() => expect(feedRows().length).toBe(5));
    await act(async () => { await Promise.resolve(); });
    const root = screen.getByTestId("ev-feed").closest(".v2-page")!;
    const titles = Array.from(root.querySelectorAll("[title]")).map((e) => e.getAttribute("title")).join(" ");
    const all = `${root.textContent} ${titles}`;
    expect(all).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
    expect(all).not.toContain(String.fromCharCode(8212));
  });
});

describe("Data library, Events tab, earnings", () => {
  const panel = () => document.querySelector<HTMLElement>(".ev-earnb")!;
  const earnRows = () => Array.from(panel().querySelectorAll<HTMLElement>("tr[data-earn]"));
  const cell = (row: HTMLElement, n: number) => row.querySelectorAll("td")[n] as HTMLElement;
  const ready = async () => {
    await openTab();
    await waitFor(() => expect(document.querySelector(".ev-earnb tr[data-earn]")).not.toBeNull());
  };

  it("(e13) the panel head reads EARNINGS 14 and its hover holds the From and Reaction lines", async () => {
    await ready();
    const head = panel().querySelector<HTMLElement>(".nl-blk-h")!;
    expect(head.textContent).toContain("Earnings");
    expect(head.textContent).toContain("14");
    const tip = head.querySelector("h2 span")!.getAttribute("title")!;
    expect(tip).toContain("Earnings releases");
    expect(tip).toContain("From: the Benzinga Newsdesk headline in Alpaca news");
    expect(tip).toContain("Reaction: last close before to first close after");
    expect(tip).not.toContain("could not be read");
    expect(m.earnings).toHaveBeenCalled();
  });

  it("(e14) Beats and Misses filter the rows by EPS", async () => {
    await ready();
    fireEvent.click(within(panel()).getByRole("button", { name: "Misses" }));
    expect(earnRows().length).toBe(4);
    expect(panel().querySelector(".nl-blk-h")!.textContent).toContain("4");
    fireEvent.click(within(panel()).getByRole("button", { name: "Beats" }));
    expect(earnRows().length).toBe(9);
    fireEvent.click(within(panel()).getByRole("button", { name: "All" }));
    expect(earnRows().length).toBe(12);
  });

  it("(e15) the Apple row: BEAT, EPS against estimate, both bars, Paris time with UTC on hover", async () => {
    await ready();
    const row = earnRows()[0];
    expect(cell(row, 0).textContent).toContain("Apple");
    expect(cell(row, 0).textContent).toContain("AAPL");
    expect(cell(row, 1).textContent).toBe("Q3");
    expect(cell(row, 2).textContent).toBe("30 Jul 22:30");
    expect(cell(row, 2).getAttribute("title")).toContain("2026-07-30 20:30:28 UTC");
    const word = cell(row, 3).querySelector(".state")!;
    expect(word.textContent).toBe("BEAT");
    expect(word.className).toContain("g");
    expect(cell(row, 4).textContent).toBe("$2.02 vs $1.89");
    expect(cell(row, 4).getAttribute("title")).toContain("Adjusted EPS");
    expect(cell(row, 5).textContent).toContain("+6.9%");
    expect(cell(row, 6).textContent).toBe("$109.4B vs $108.7B");
    expect(cell(row, 6).getAttribute("title")).toContain("$109.417B");
    expect(cell(row, 7).textContent).toContain("-7.4%");
    const bar = cell(row, 7).querySelector<HTMLElement>(".ebar b")!;
    expect(bar.style.background).toBe("rgb(255, 90, 99)");
    expect(cell(row, 5).querySelector<HTMLElement>(".ebar b")!.style.background).toBe("rgb(47, 224, 138)");
    expect(cell(row, 8).querySelector("svg path")).not.toBeNull();
    expect(cell(row, 8).querySelector("line")!.getAttribute("stroke-dasharray")).toBeTruthy();
    expect(cell(row, 8).getAttribute("title")).toContain("Closes around the release");
    expect(cell(row, 8).getAttribute("title")).toContain("daily closes, Alpaca");
  });

  it("(e16) a reaction not in yet shows a dash and says why; a correction shows on the Released hover", async () => {
    await ready();
    const nyt = earnRows()[1];
    expect(cell(nyt, 7).textContent).toContain("-");
    expect(cell(nyt, 7).getAttribute("title")).toContain("Not yet: the first close after the release is not in yet");
    expect(cell(nyt, 7).querySelector(".ebar b")).toBeNull();
    const cor = earnRows()[2];
    expect(cell(cor, 2).getAttribute("title")).toContain("Corrected 2026-07-30 12:15:00 UTC");
    expect(cell(cor, 4).getAttribute("title")).toContain("First printed $0.90");
    expect(cell(earnRows()[0], 2).getAttribute("title")).not.toContain("Corrected");
  });

  it("(e17) 12 rows, then SHOW ALL 14, then SHOW FEWER", async () => {
    await ready();
    expect(earnRows().length).toBe(12);
    fireEvent.click(within(panel()).getByRole("button", { name: "SHOW ALL 14" }));
    expect(earnRows().length).toBe(14);
    fireEvent.click(within(panel()).getByRole("button", { name: "SHOW FEWER" }));
    expect(earnRows().length).toBe(12);
  });

  it("(e18) a row click opens the detail with its rows and the release line label", async () => {
    await ready();
    fireEvent.click(earnRows()[0]);
    const dlg = await screen.findByRole("dialog");
    expect(dlg.getAttribute("aria-label")).toBe("Apple, Q3, 30 Jul 22:30");
    expect(dlg.textContent).toContain("EPS $2.02 against $1.89");
    expect(dlg.textContent).toContain("Sales $109.417B against $108.651B");
    expect(dlg.textContent).toContain("22:30 Paris");
    expect(dlg.querySelector("line[stroke-dasharray]")).not.toBeNull();
    expect(dlg.querySelectorAll("circle[data-reaction-close]").length).toBe(2);
    for (const label of ["Released", "Reaction", "EPS", "Headline", "Source"]) expect(dlg.textContent).toContain(label);
    expect(dlg.textContent).not.toContain("Corrected");
    expect(dlg.textContent).toContain("Benzinga Newsdesk in Alpaca news; closes from Alpaca");
    expect(dlg.textContent).toContain("Apple Q3 Adj. EPS $2.02 Beats");
    fireEvent.click(within(dlg).getByRole("button", { name: "CLOSE" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(earnRows()[2]);
    const fixed = await screen.findByRole("dialog");
    expect(fixed.textContent).toContain("Corrected");
    expect(fixed.textContent).toContain("2026-07-30 12:15:00 UTC, first printed EPS $0.90");
  });

  it("(e19) no releases says so; a failed read shows the server's words; unread shows on the hover", async () => {
    m.earnings.mockResolvedValue({ rows: [], unread: 0 });
    await openTab();
    expect(await screen.findByText("No earnings releases stored yet.")).toBeInTheDocument();
    cleanup();
    m.earnings.mockRejectedValue(new ApiError("503 /x", 503, { detail: "Alpaca could not be reached for the closing prices." }, "/x"));
    await openTab();
    expect(await screen.findByText("Alpaca could not be reached for the closing prices.")).toBeInTheDocument();
    cleanup();
    m.earnings.mockResolvedValue({ rows: EARN_ROWS, unread: 3 });
    await ready();
    expect(panel().querySelector(".nl-blk-h h2 span")!.getAttribute("title")).toContain("3 headlines could not be read");
  });

  it("(e20) the tab's text and hovers hold no snake_case and no em dash", async () => {
    await ready();
    fireEvent.click(earnRows()[0]);
    await screen.findByRole("dialog");
    const titles = Array.from(document.querySelectorAll("[title]")).map((e) => e.getAttribute("title")).join(" ");
    const all = `${document.body.textContent} ${titles}`;
    expect(all).not.toMatch(/[a-z]+_[a-z]+/);
    expect(all).not.toContain(String.fromCharCode(8212));
  });
});
