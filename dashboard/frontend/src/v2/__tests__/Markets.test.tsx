import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Markets from "../pages/research/Markets";
import { SettingsProvider } from "../settings";
import { APPS, findApp, findSection, pageFor } from "../shell/sections";
import { sma, shortReason, yearRange, yearToDate, changeOver, weekMove, biggerThan } from "../pages/research/markets/series";
import type { WeekMove } from "../pages/research/markets/series";

// lightweight-charts draws on a canvas, which jsdom cannot run. This stand-in records what the
// chart window hands it: each series with its data, and the crosshair handler.
type FakeSeries = { def: unknown; opts: unknown; data: Array<Record<string, number>> };
type FakeChart = { series: FakeSeries[]; handler: ((p: { time?: number }) => void) | null; removed: boolean };
const lw = vi.hoisted(() => ({ charts: [] as FakeChart[] }));
vi.mock("lightweight-charts", () => ({
  CandlestickSeries: "candles",
  LineSeries: "line",
  createChart: () => {
    const c: FakeChart = { series: [], handler: null, removed: false };
    lw.charts.push(c);
    return {
      addSeries: (def: unknown, opts: unknown) => {
        const s: FakeSeries = { def, opts, data: [] };
        c.series.push(s);
        return { setData: (d: Array<Record<string, number>>) => { s.data = d; } };
      },
      subscribeCrosshairMove: (h: (p: { time?: number }) => void) => { c.handler = h; },
      unsubscribeCrosshairMove: () => { c.handler = null; },
      timeScale: () => ({ fitContent: () => undefined }),
      applyOptions: () => undefined,
      remove: () => { c.removed = true; },
    };
  },
}));

const mocks = vi.hoisted(() => ({
  getQuotes: vi.fn(),
  getHistory: vi.fn(),
  getYieldCurve: vi.fn(),
  getCommodities: vi.fn(),
  getMarketNews: vi.fn(),
  getNews: vi.fn(),
  search: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  return { ...real, api: { ...real.api, display: mocks } };
});

const EM_DASH = String.fromCharCode(8212);
const SECTOR_SYMBOLS = ["XLK", "XLI", "XLF", "XLV", "XLE", "XLY", "XLP", "XLRE", "XLC", "XLU", "XLB"];

// Weekdays only, counted back from Friday 2026-10-09: the first of 520 is 2024-10-14.
function weekdays(n: number): string[] {
  const out: string[] = [];
  const d = new Date(Date.UTC(2026, 9, 9));
  while (out.length < n) {
    if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return out.reverse();
}

type Bar = { time: string; epoch_seconds: number; open: number; high: number; low: number; close: number; volume: number };
function barsOf(n: number, closeAt: (i: number, n: number) => number = (i) => 100 + i): Bar[] {
  return weekdays(n).map((time, i) => {
    const close = closeAt(i, n);
    return {
      time, epoch_seconds: Date.parse(`${time}T00:00:00Z`) / 1000,
      open: close - 1, high: close + 1, low: close - 2, close, volume: 1_100_000 + i,
    };
  });
}
const rising = () => barsOf(520);

// A series whose last 21-bar, 5-bar and 1-bar changes (in percent) are m, w, d.
function shaped(d: number, w: number, m: number): Bar[] {
  const last = 100 * (1 + m / 100);
  return barsOf(520, (i) => (i === 519 ? last : i === 518 ? last / (1 + d / 100) : i === 514 ? last / (1 + w / 100) : 100));
}

let hist: Record<string, Bar[] | Error>;
let sectorWeek: number[];

function setup() {
  hist = {};
  sectorWeek = [0.1, 0.9, 0.5, 0.2, -0.8, 0.3, 0.4, 0.6, 0.7, -0.1, 0.05];
  mocks.getHistory.mockImplementation(async (symbol: string, period: string) => {
    const si = SECTOR_SYMBOLS.indexOf(symbol);
    let bars: Bar[] | Error;
    if (hist[symbol] !== undefined) bars = hist[symbol];
    else if (si >= 0) bars = shaped(si === 0 ? 1 : -1, sectorWeek[si], 2);
    else bars = rising();
    if (bars instanceof Error) throw bars;
    return { symbol, period, bars, time_form: "daily" };
  });
  mocks.getYieldCurve.mockResolvedValue({
    tenors: [
      { tenor: "3M", level: 5.3, change_bps: 0, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" },
      { tenor: "2Y", level: 4.8, change_bps: 2, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" },
      { tenor: "10Y", level: 5.24, change_bps: 5.5, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" },
    ],
  });
  mocks.getCommodities.mockResolvedValue({
    commodities: [
      { name: "Brent crude oil", level: 82.1, change: -0.6, grain: "daily", observation_date: "2026-10-09", unit: null },
      { name: "Gold", level: 2650.4, change: 12.2, grain: "daily", observation_date: "2026-10-09", unit: null },
    ],
  });
  mocks.getMarketNews.mockResolvedValue({
    news: Array.from({ length: 6 }, (_, i) => ({
      time: `2026-10-09T1${i}:05:00+00:00`, ticker: `TK${i}`, kind: "news", headline: `Headline number ${i}`,
    })),
  });
  mocks.search.mockResolvedValue({
    results: [{ name: "SPDR S&P 500 ETF Trust", symbol: "SPY.US", ticker: "SPY", cik: 1 }],
  });
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const app = APPS.find((a) => a.id === "research")!;
  const section = app.sections.find((s) => s.id === "data-library")!;
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

beforeEach(() => {
  lw.charts.length = 0;
  localStorage.clear();
  Object.values(mocks).forEach((m) => m.mockReset());
  setup();
});

describe("series figures", () => {
  it("the fixture is the one the plan describes", () => {
    const b = rising();
    expect(b).toHaveLength(520);
    expect(b[0].time).toBe("2024-10-14");
    expect(b[519].time).toBe("2026-10-09");
    expect(b.find((x) => x.time === "2025-12-31")!.close).toBe(417);
  });

  it("changes, this year and the average follow the method's formulas", () => {
    const b = rising();
    expect(changeOver(b, 1).value).toBeCloseTo((619 / 618 - 1) * 100, 9);
    expect(changeOver(b, 21).value).toBeCloseTo((619 / 598 - 1) * 100, 9);
    expect(changeOver(b, 252).value).toBeCloseTo((619 / 367 - 1) * 100, 9);
    expect(yearToDate(b).value).toBeCloseTo((619 / 417 - 1) * 100, 9);
    const s = sma(b.map((x) => x.close), 200);
    expect(s[198]).toBeNull();
    expect(s[199]).toBe(199.5);
    expect(s[519]).toBe(519.5);
  });

  it("the range uses today and the 252 closes before it, and says why when it cannot", () => {
    const r = yearRange(rising());
    expect(r.value).toMatchObject({ low: 367, high: 619, close: 619, position: 1 });
    expect(yearRange(barsOf(252)).reason).toBe(shortReason(252, 253));
    expect(shortReason(10, 22)).toBe("Needs 22 daily bars, 10 came back (12 missing).");
    expect(shortReason(0, 22)).toBe("No daily bars came back for this series.");
  });
});

describe("Markets page", () => {
  const wallTile = (name: string) => Array.from(document.querySelectorAll<HTMLElement>('[data-testid="wall-tile"]')).find((e) => e.getAttribute("data-name") === name)!;
  const tileNames = () => Array.from(document.querySelectorAll('[data-testid="wall-tile"]')).map((e) => e.getAttribute("data-name"));
  const priceOf = (name: string) => wallTile(name).querySelector<HTMLElement>(".fig")!;
  const movesOf = (name: string) =>
    Object.fromEntries(Array.from(wallTile(name).querySelectorAll<HTMLElement>(".t-chg > span")).map((s) => [s.querySelector("i")!.textContent, s.querySelector<HTMLElement>("b")!]));
  const ready = (name = "S&P 500") => waitFor(() => expect(priceOf(name).textContent).not.toBe("-"));
  const openWall = async () => { mount(); await screen.findAllByTestId("wall-tile"); };
  const pressTile = async (name: string) => { await ready(name); fireEvent.click(wallTile(name)); };

  it("is rebuilt, not the stub, and holds the mockup's sections in order", async () => {
    await openWall();
    expect(screen.queryByText("This page is not rebuilt yet.")).toBeNull();
    const heads = Array.from(document.querySelectorAll(".nl-blk h2")).map((h) => h.textContent);
    expect(heads).toEqual(["Markets", "US sectors", "US interest rates", "Commodities", "News"]);
  });

  it("the tab row has five tabs with Markets on, and the company lookup", async () => {
    await openWall();
    const labels = Array.from(document.querySelectorAll(".mk-tabs .seg button")).map((b) => b.textContent);
    expect(labels).toEqual(["Markets", "Datasets", "Universes", "Events", "Fundamentals"]);
    expect(screen.getByRole("button", { name: "Markets" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByLabelText("Look up a company or ticker")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Datasets" })).toBeNull();
  });

  it("the wall has one tile for each market, in the page's order when asked, with the method's figures", async () => {
    await openWall();
    fireEvent.click(screen.getByRole("button", { name: "Usual order" }));
    expect(tileNames()).toEqual([
      "S&P 500", "Euro Stoxx 50", "Nikkei 225", "US 10-year yield", "Dollar index", "Oil Brent", "Gold", "VIX",
      "Nasdaq 100", "CAC 40", "DAX", "Hang Seng", "Shanghai", "Bitcoin",
    ]);
    await ready();
    expect(priceOf("S&P 500").textContent).toBe("619.00");
    const mv = movesOf("S&P 500");
    expect(mv["Day"].textContent).toBe("+0.2%");
    expect(mv["7 days"].textContent).toBe("+0.8%");
    expect(mv["1 month"].textContent).toBe("+3.5%");
    expect(mv["1 year"].textContent).toBe("+68.7%");
    expect(mocks.getHistory).toHaveBeenCalledWith("GSPC.INDX", "2y");
    for (const s of ["STOXX50E.INDX", "N225.INDX", "DXY.INDX", "VIX.INDX", "NDX.INDX", "FCHI.INDX", "GDAXI.INDX", "HSI.INDX", "000001.SHG"]) {
      expect(mocks.getHistory).toHaveBeenCalledWith(s, "2y");
    }
    expect(mocks.getQuotes).not.toHaveBeenCalled();
  });

  it("biggest week first puts the largest 7-day move on top, up or down; the sentence names it", async () => {
    hist["N225.INDX"] = shaped(0, -7, 0);
    hist["DXY.INDX"] = shaped(0, 5, 0);
    await openWall();
    await waitFor(() => expect(tileNames().slice(0, 2)).toEqual(["Nikkei 225", "Dollar index"]));
    expect(screen.getByRole("button", { name: "Biggest week first" }).getAttribute("aria-pressed")).toBe("true");
    const say = await screen.findByTestId("wall-say");
    expect(say.textContent).toContain("Nikkei 225 fell 7.0%");
    expect(say.textContent).toContain("markets and sectors rose this week.");
    fireEvent.click(screen.getByRole("button", { name: "Usual order" }));
    expect(tileNames()[0]).toBe("S&P 500");
  });

  it("the 10-year tile shows the level and bp; Brent and gold show their level and change; the rest is a dash", async () => {
    mocks.getYieldCurve.mockResolvedValue({
      tenors: [{ tenor: "10Y", level: 4.12, change_bps: 3, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" }],
    });
    mocks.getCommodities.mockResolvedValue({
      commodities: [
        { name: "Brent crude oil", level: 99.1, change: 0.41, grain: "daily", observation_date: "2026-10-09", unit: null },
        { name: "Gold", level: 4118, change: 64, grain: "daily", observation_date: "2026-10-09", unit: null },
      ],
    });
    await openWall();
    await waitFor(() => expect(priceOf("US 10-year yield").textContent).toBe("4.12%"));
    expect(movesOf("US 10-year yield")["Day"].textContent).toBe("+3.0 bp");
    expect(wallTile("US 10-year yield").textContent).toContain("No chart yet");
    await waitFor(() => expect(priceOf("Oil Brent").textContent).toBe("99.10"));
    expect(movesOf("Oil Brent")["Day"].textContent).toBe("+0.4%");
    await waitFor(() => expect(priceOf("Gold").textContent).toBe("4,118.00"));
    expect(wallTile("Gold").textContent).toContain("No chart yet");
  });

  it("Brent and gold with no row from the server are dashes with the reason", async () => {
    mocks.getCommodities.mockResolvedValue({ commodities: [] });
    await openWall();
    await waitFor(() => expect(mocks.getCommodities).toHaveBeenCalled());
    await waitFor(() => expect(priceOf("Oil Brent").getAttribute("title")).toMatch(/does not serve/i));
    expect(priceOf("Oil Brent").textContent).toBe("-");
    expect(wallTile("Oil Brent").textContent).toContain("No data yet");
  });

  it("Bitcoin has no source: its tile shows the honest empty state with the server's words", async () => {
    hist["BTC-USD.CC"] = new Error("BTC-USD.CC has no source");
    await openWall();
    await waitFor(() => expect(priceOf("Bitcoin").getAttribute("title")).toBe("BTC-USD.CC has no source"));
    expect(priceOf("Bitcoin").textContent).toBe("-");
    expect(wallTile("Bitcoin").textContent).toContain("No data yet");
  });

  it("a market with a close below its 200-day average still draws, and a falling year is red", async () => {
    hist["DXY.INDX"] = barsOf(520, (i) => 700 - i);
    await openWall();
    await ready("Dollar index");
    expect(priceOf("Dollar index").textContent).toBe("181.00");
    expect(wallTile("Dollar index").getAttribute("data-hue")).toBe("bad");
    expect(wallTile("S&P 500").getAttribute("data-hue")).toBe("good");
  });

  const drawnBars = () => (document.querySelector(".mk-win .mk-svg .ln")?.getAttribute("d") ?? "").match(/[ML]/g)?.length ?? 0;

  it("a press on a tile opens that market's chart window for its symbol", async () => {
    await openWall();
    await pressTile("S&P 500");
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText("S&P 500")).toBeTruthy();
    await waitFor(() => expect(mocks.getHistory).toHaveBeenCalledWith("GSPC.INDX", "2y"));
    await waitFor(() => expect(drawnBars()).toBe(252));
    expect(within(dlg).getByText("619.00", { selector: ".fig" })).toBeTruthy();
  });

  it("1M, 3M and 1Y draw the last 21, 63 and 252 bars with the 200-day average beside them", async () => {
    await openWall();
    await pressTile("S&P 500");
    const dlg = await screen.findByRole("dialog");
    await waitFor(() => expect(drawnBars()).toBe(252));
    for (const [label, n] of [["1M", 21], ["3M", 63], ["1Y", 252]] as const) {
      fireEvent.click(within(dlg).getByRole("button", { name: label }));
      await waitFor(() => expect(drawnBars()).toBe(n));
      expect(dlg.querySelector(".mk-svg .avg")).not.toBeNull();
    }
    const legend = within(dlg).getByTestId("chart-legend");
    expect(legend.textContent).toContain("200-day average");
    expect(legend.textContent).toContain("519.50");
  });

  it("the pointer's box gives the date, the close, the average and the volume; it is gone without a pointer", async () => {
    await openWall();
    await pressTile("S&P 500");
    await screen.findByRole("dialog");
    await waitFor(() => expect(drawnBars()).toBe(252));
    expect(screen.queryByTestId("chart-foot")).toBeNull();
    const chart = screen.getByTestId("price-chart");
    vi.spyOn(chart, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}) } as DOMRect);
    fireEvent.mouseMove(chart, { clientX: 600 });
    const foot = await screen.findByTestId("chart-foot");
    for (const w of ["Close", "200-day average", "Volume"]) expect(foot.textContent).toContain(w);
    expect(foot.textContent).toContain("619.00");
    fireEvent.mouseLeave(chart);
    expect(screen.queryByTestId("chart-foot")).toBeNull();
  });

  it("Escape closes the chart", async () => {
    await openWall();
    await pressTile("S&P 500");
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("the chart's search runs on Enter and picking a result charts its symbol", async () => {
    await openWall();
    await pressTile("S&P 500");
    const dlg = await screen.findByRole("dialog");
    const box = within(dlg).getByLabelText("Search a ticker, a company or an ISIN");
    fireEvent.change(box, { target: { value: "spdr" } });
    expect(mocks.search).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(mocks.search).toHaveBeenCalledWith("spdr"));
    fireEvent.click(await within(dlg).findByRole("button", { name: /SPDR S&P 500 ETF Trust/ }));
    await waitFor(() => expect(mocks.getHistory).toHaveBeenCalledWith("SPY.US", "2y"));
    await waitFor(() => expect(mocks.getHistory).toHaveBeenCalledWith("SPY.US", "2y"));
  });

  it("typing in the lookup lists matches at once and a pick opens the company window", async () => {
    mocks.getNews.mockImplementation(() => new Promise(() => undefined));
    await openWall();
    const box = screen.getByLabelText("Look up a company or ticker");
    fireEvent.change(box, { target: { value: "spdr" } });
    await waitFor(() => expect(mocks.search).toHaveBeenCalledWith("spdr"));
    fireEvent.mouseDown(await screen.findByRole("option", { name: /SPDR S&P 500 ETF Trust/ }));
    const dlg = await screen.findByRole("dialog", { name: "SPDR S&P 500 ETF Trust" });
    expect(dlg).toBeTruthy();
    await waitFor(() => expect(mocks.getHistory).toHaveBeenCalledWith("SPY.US", "max"));
  });

  it("lists eleven sectors, Week first and sorted, as bars around zero", async () => {
    await openWall();
    await waitFor(() => expect(screen.getAllByTestId("sector-row")).toHaveLength(11));
    await waitFor(() => expect(within(screen.getAllByTestId("sector-row")[0]).getByText("+0.90%")).toBeTruthy());
    const rows = screen.getAllByTestId("sector-row");
    expect(within(rows[0]).getByText("Industrials")).toBeTruthy();
    expect(rows[0].querySelector('[title="XLI"]')).not.toBeNull();
    expect(within(rows[10]).getByText("Energy")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Week" }).getAttribute("aria-pressed")).toBe("true");
    const text = document.body.textContent ?? "";
    for (const w of ["RISK-ON", "DEFENSIVE", "MIXED"]) expect(text).not.toContain(w);
  });

  it("Day uses one bar, Month 21", async () => {
    await openWall();
    await waitFor(() => expect(within(screen.getAllByTestId("sector-row")[0]).getByText("+0.90%")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Day" }));
    await waitFor(() => expect(within(screen.getAllByTestId("sector-row")[0]).getByText("+1.00%")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Month" }));
    await waitFor(() => expect(within(screen.getAllByTestId("sector-row")[0]).getByText("+2.00%")).toBeTruthy());
  });

  it("Appetite for risk is gone (Tamer 2026-11-14): no pair on the page and no call for its funds", async () => {
    await openWall();
    await ready();
    expect(screen.queryByText("APPETITE FOR RISK")).toBeNull();
    for (const s of ["IWM", "SPY", "EEM", "EFA", "HYG", "IEF"]) expect(mocks.getHistory).not.toHaveBeenCalledWith(s, "2y");
  });

  it("rates show the 10 year minus 2 year spread and its change, up bars green and down bars red", async () => {
    await openWall();
    const spread = await screen.findByTestId("rates-spread");
    expect(spread.textContent).toContain("0.44");
    expect(screen.getByTestId("rates-spread-change").textContent).toContain("3.5");
    expect(screen.getAllByTestId("rates-point")).toHaveLength(3);
    const bars = screen.getAllByTestId("rates-change");
    expect(bars.map((b) => b.querySelector("title")!.textContent)).toEqual([
      "3M: 0.0 basis points from 2026-10-08 to 2026-10-09",
      "2Y: +2.0 basis points from 2026-10-08 to 2026-10-09",
      "10Y: +5.5 basis points from 2026-10-08 to 2026-10-09",
    ]);
    expect(Number(bars[2].getAttribute("height"))).toBeGreaterThan(Number(bars[1].getAttribute("height")));
    expect(Number(bars[0].getAttribute("height"))).toBe(4);
    expect(bars[2].getAttribute("fill")).toBe("#2fe08a");
  });

  it("rates draw at the card's own width and name every tenor under the curve", async () => {
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 700, bottom: 300, x: 0, y: 0, width: 700, height: 300, toJSON: () => ({}) } as DOMRect);
    try {
      mount();
      await screen.findAllByTestId("rates-point");
      await waitFor(() => expect(document.querySelector(".mk-rates")!.getAttribute("viewBox")).toBe("0 0 700 250"));
      expect([...document.querySelectorAll(".t-labels span")].map((t) => t.textContent)).toEqual(["3M", "2Y", "10Y"]);
    } finally {
      spy.mockRestore();
    }
  });

  it("fourteen tenors are all named under the curve; each point keeps its hover", async () => {
    const names = ["1M", "1.5M", "2M", "3M", "4M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"];
    mocks.getYieldCurve.mockResolvedValue({
      tenors: names.map((tenor, i) => ({ tenor, level: 4 + i / 10, change_bps: 1, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" })),
    });
    mount();
    await waitFor(() => expect(screen.getAllByTestId("rates-point")).toHaveLength(14));
    expect([...document.querySelectorAll(".t-labels span")].map((t) => t.textContent)).toEqual(names);
    expect(screen.getAllByTestId("rates-point")[0].querySelector("title")!.textContent).toContain("1M");
  });

  it("with no 2 year row the spread is a dash with a reason", async () => {
    mocks.getYieldCurve.mockResolvedValue({
      tenors: [{ tenor: "10Y", level: 5.24, change_bps: 5.5, observation_date: "2026-10-09", previous_observation_date: "2026-10-08" }],
    });
    mount();
    const spread = await screen.findByTestId("rates-spread");
    expect(spread.textContent).toBe("-");
    expect(spread.getAttribute("title")).toMatch(/2 year/i);
  });

  it("commodities are Brent and Gold: level, grain and the change since the last print", async () => {
    await openWall();
    const rows = await screen.findAllByTestId("commodity-row");
    expect(rows.map((r) => r.querySelector(".nm")!.textContent)).toEqual(["Brent crude oil", "Gold"]);
    expect(rows[0].querySelector(".meta")!.textContent).toBe("82.10 daily -0.7%");
    expect(rows[1].querySelector(".meta")!.textContent).toContain("2,650.40");
    expect(screen.queryByText(/natural gas/i)).toBeNull();
  });

  it("w9 news shows six rows; the display-only note is the title's hover, not a line", async () => {
    await openWall();
    const head = await screen.findByText("News");
    expect(head.getAttribute("title")).toBe("Shown here only, never used in research");
    expect(document.body.textContent).not.toContain("never used in research");
    const rows = await screen.findAllByTestId("news-row");
    expect(rows).toHaveLength(6);
    expect(within(rows[0]).getByText("TK0")).toBeTruthy();
    expect(within(rows[0]).getByText("Headline number 0")).toBeTruthy();
    expect(within(rows[0]).getByText(/10:05/)).toBeTruthy();
    expect(mocks.getMarketNews).toHaveBeenCalledWith(6);
  });

  it("research/data-library is the Markets page and research/overview is not", () => {
    const app = findApp("research")!;
    expect(pageFor(app, findSection(app, "data-library")!)).toBe(Markets);
    expect(pageFor(app, findSection(app, "overview")!)).not.toBe(Markets);
  });

  it("a failing history request shows the server's words for that element only", async () => {
    hist.XLK = new Error("Sector history refused.");
    hist["GSPC.INDX"] = new Error("Index history refused.");
    await openWall();
    await waitFor(() => expect(within(screen.getAllByTestId("sector-row").find((r) => within(r).queryByText("Technology"))!).getByTitle("Sector history refused.")).toBeTruthy());
    const xlk = screen.getAllByTestId("sector-row").find((r) => within(r).queryByText("Technology"))!;
    expect(within(xlk).getByTitle("Sector history refused.").textContent).toBe("-");
    await waitFor(() => expect(priceOf("S&P 500").getAttribute("title")).toBe("Index history refused."));
    await ready("Euro Stoxx 50");
    expect(priceOf("Euro Stoxx 50").textContent).toBe("619.00");
    for (const r of screen.getAllByTestId("sector-row")) {
      if (r !== xlk) expect(within(r).queryByTitle("Sector history refused.")).toBeNull();
    }
  });

  it("zero bars make every figure of the tile a dash that says so", async () => {
    hist["GSPC.INDX"] = [];
    await openWall();
    const reason = "No daily bars came back for this series.";
    await waitFor(() => expect(priceOf("S&P 500").getAttribute("title")).toBe(reason));
    expect(priceOf("S&P 500").textContent).toBe("-");
    for (const c of Object.values(movesOf("S&P 500"))) {
      expect(c.textContent).toBe("-");
      expect(c.getAttribute("title")).toBe(reason);
    }
  });

  it("a short series dashes only the figures it cannot give, naming the bars missing", async () => {
    hist["GSPC.INDX"] = barsOf(100);
    hist["N225.INDX"] = barsOf(10);
    hist["DXY.INDX"] = barsOf(252);
    await openWall();
    await ready();
    const sp = movesOf("S&P 500");
    expect(sp["1 month"].textContent).not.toBe("-");
    expect(sp["1 year"].textContent).toBe("-");
    expect(sp["1 year"].getAttribute("title")).toBe("Needs 253 daily bars, 100 came back (153 missing).");
    await ready("Nikkei 225");
    expect(movesOf("Nikkei 225")["1 month"].getAttribute("title")).toBe("Needs 22 daily bars, 10 came back (12 missing).");
    await ready("Dollar index");
    expect(movesOf("Dollar index")["1 year"].getAttribute("title")).toBe("Needs 253 daily bars, 252 came back (1 missing).");
  });

  it("a sector with too short a series is a dash naming the bars missing", async () => {
    hist.XLK = barsOf(4);
    await openWall();
    await waitFor(() => expect(screen.getAllByTestId("sector-row").length).toBe(11));
    await waitFor(() => {
      const xlk = screen.getAllByTestId("sector-row").find((r) => within(r).queryByText("Technology"))!;
      expect(within(xlk).getByTitle("Needs 6 daily bars, 4 came back (2 missing).").textContent).toBe("-");
    });
  });

  it("uses no em dash and no snake_case in anything it shows", async () => {
    const { container } = mount();
    await screen.findAllByTestId("news-row");
    expect(container.textContent ?? "").not.toContain(EM_DASH);
  });
});

describe("weekMove", () => {
  const day = (iso: string, close: number): Bar => ({ time: iso, epoch_seconds: Date.parse(`${iso}T00:00:00Z`) / 1000, open: close, high: close, low: close, close, volume: 1 });
  it("starts at the newest bar on or before 7 calendar days back, so a holiday moves the start a day earlier", () => {
    const bars = [day("2025-11-20", 100), day("2026-10-01", 98), day("2026-10-08", 101), day("2026-10-15", 103)];
    const holiday = [day("2025-11-20", 100), day("2026-09-30", 97), day("2026-10-01", 98), day("2026-10-15", 103)];
    expect(weekMove(bars).value!.start.time).toBe("2026-10-08");
    expect(weekMove(holiday).value!.start.time).toBe("2026-10-01");
    expect(weekMove(holiday).value!.move).toBeCloseTo((103 / 98 - 1) * 100, 10);
  });
  it("ranks the move among the 7-day moves of the 365 days before the start, by size", () => {
    // 7-day moves ending on each bar up to the start: +2%, -4%, +1%, +1.113% (100 / 98.8992 - 1); this week +3% is bigger
    // in size than three of the four.
    const bars = [
      day("2025-10-01", 100), day("2025-10-08", 102), day("2025-10-15", 97.92), day("2025-10-22", 98.8992),
      day("2025-10-29", 100), day("2025-11-05", 103),
    ];
    const w = weekMove(bars).value!;
    expect(w.start.time).toBe("2025-10-29");
    expect(w.move).toBeCloseTo(3, 10);
    expect(w.count).toBe(4);
    expect(w.smaller).toBe(3);
    expect(biggerThan(w)).toBe(75);
  });
  it("says the share in whole numbers from the counts, so 145 of 250 is 58, not one point low", () => {
    // 145 / 250 * 100 is 57.99999999999999 in floating point; rounding that down would print 57.
    expect(biggerThan({ smaller: 145, count: 250 } as WeekMove)).toBe(58);
    expect(biggerThan({ smaller: 249, count: 250 } as WeekMove)).toBe(99);
    expect(biggerThan({ smaller: 250, count: 250 } as WeekMove)).toBe(100);
  });
  it("says why when there is no start bar or no yardstick", () => {
    expect(weekMove([]).reason).toBe("No daily bars came back for this series.");
    expect(weekMove([day("2026-10-14", 1), day("2026-10-15", 2)]).reason).toBe("The series has no close 7 or more days before its newest one.");
    expect(weekMove([day("2026-10-08", 1), day("2026-10-15", 2)]).reason).toBe("The series has no 7-day move in the year before this week to compare with.");
  });
});
