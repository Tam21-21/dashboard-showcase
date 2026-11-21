import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CompanyFind from "../pages/research/lookup/CompanyFind";
import LookupWindow from "../pages/research/lookup/LookupWindow";
import { hourLabel } from "../pages/research/lookup/names";
import type { Company } from "../pages/research/lookup/names";
import { ApiError } from "../../lib/api";

const mocks = vi.hoisted(() => ({ search: vi.fn(), getHistory: vi.fn(), getNews: vi.fn() }));
const fund = vi.hoisted(() => ({ fundamentalsHistory: vi.fn(), fundamentalsStatus: vi.fn(), fundamentalsBackfill: vi.fn() }));
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  return { ...real, api: { ...real.api, display: mocks, research: { ...real.api.research, ...fund } } };
});

const NEVER = () => new Promise(() => undefined);

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}

const APPLE: Company = { ticker: "AAPL", name: "Apple Inc.", cik: 320193, symbol: "AAPL.US" };

// Weekdays only, counted back from Friday 2026-11-13.
function weekdays(n: number): string[] {
  const out: string[] = [];
  const d = new Date(Date.UTC(2026, 10, 13));
  while (out.length < n) {
    if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return out.reverse();
}
function dailyAnswer() {
  const bars = weekdays(1300).map((time, i) => {
    const close = 50 + 0.25 * i;
    return { time, epoch_seconds: Date.parse(`${time}T00:00:00Z`) / 1000, open: close, high: close, low: close, close, volume: 1000 };
  });
  return { symbol: "AAPL.US", period: "max", bars, time_form: "daily", source: "EODHD, then Alpaca", last_day: "2026-11-13", join: { day: "2026-11-05", scale: 1.0007 }, fill_error: null, left_out: {} };
}
function hourlyAnswer() {
  const stamps = ["2026-11-13T14:00:00Z", "2026-11-13T15:00:00Z", "2026-11-13T16:00:00Z", "2026-11-13T20:00:00Z", "2026-11-13T21:00:00Z"];
  const bars = stamps.map((time, i) => ({ time, epoch_seconds: Date.parse(time) / 1000, open: 370 + i, high: 371 + i, low: 369 + i, close: 370 + i, volume: 5, regular: i < 4 }));
  return { symbol: "AAPL.US", period: "1d", bars, time_form: "utc_hour", session: { zone: "America/New_York", open: "09:30", close: "16:00" }, source: "Alpaca", last_day: "2026-11-13", left_out: {} };
}
function newsAnswer(n: number, leftOut: number, name: string | null = "Apple") {
  const news = Array.from({ length: n }, (_, i) => ({
    time: i === 0 ? "2026-11-16T12:45:52Z" : `2026-11-15T${String(10 + (i % 10)).padStart(2, "0")}:00:00Z`,
    headline: `Apple&#39;s plan ${i + 1}`, url: `https://news.example/${i + 1}`, publisher: "Benzinga", summary: "S&amp;P note",
  }));
  return { ticker: "AAPL", source: "Benzinga, through Alpaca", company_name: name, left_out: leftOut, news };
}

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset());
  Object.values(fund).forEach((m) => m.mockReset());
  fund.fundamentalsHistory.mockImplementation(NEVER);
});

describe("k1 CompanyFind", () => {
  const typed = (label = "Look up") => screen.getByLabelText(label) as HTMLInputElement;
  const two = { results: [
    { symbol: "AAPL.US", ticker: "AAPL", name: "Apple Inc.", cik: 320193 },
    { symbol: "AMAT.US", ticker: "AMAT", name: "APPLIED MATERIALS INC /DE", cik: 2 },
  ] };

  it("lists the matches as typed, with the typed start in a mark, and picks with the keys", async () => {
    mocks.search.mockResolvedValue(two);
    const onPick = vi.fn();
    wrap(<CompanyFind label="Look up" onPick={onPick} />);
    fireEvent.change(typed(), { target: { value: "app" } });
    await waitFor(() => expect(mocks.search).toHaveBeenCalledWith("app"));
    const opts = await screen.findAllByRole("option");
    expect(opts.map((o) => o.querySelector("b")!.textContent)).toEqual(["AAPL", "AMAT"]);
    expect(opts.map((o) => o.querySelector("span")!.textContent)).toEqual(["Apple Inc.", "Applied Materials Inc"]);
    expect(opts[0].querySelector("mark")!.textContent).toBe("App");
    expect(screen.getByRole("combobox")).toBeTruthy();
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(typed(), { key: "ArrowDown" });
    fireEvent.keyDown(typed(), { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith({ ticker: "AMAT", name: "Applied Materials Inc", cik: 2, symbol: "AMAT.US" });
    expect(typed().value).toBe("");
  });

  it("Escape empties the box", async () => {
    mocks.search.mockResolvedValue(two);
    wrap(<CompanyFind label="Look up" onPick={vi.fn()} />);
    fireEvent.change(typed(), { target: { value: "app" } });
    await screen.findAllByRole("option");
    fireEvent.keyDown(typed(), { key: "Escape" });
    expect(typed().value).toBe("");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("says so when nothing starts with the text", async () => {
    mocks.search.mockResolvedValue({ results: [] });
    wrap(<CompanyFind label="Look up" onPick={vi.fn()} />);
    fireEvent.change(typed(), { target: { value: "zzz" } });
    expect(await screen.findByText("No company or ticker starts with “zzz”")).toBeTruthy();
  });

  it("leads closest matches with their own line and marks nothing", async () => {
    mocks.search.mockResolvedValue({ results: [{ symbol: "NVDA.US", ticker: "NVDA", name: "NVIDIA CORP", cik: 1045810, closest: true }] });
    wrap(<CompanyFind label="Look up" onPick={vi.fn()} />);
    fireEvent.change(typed(), { target: { value: "nvdia" } });
    const opt = await screen.findByRole("option");
    const menu = screen.getByRole("listbox");
    const first = menu.firstElementChild as HTMLElement;
    expect(first.tagName).toBe("DIV");
    expect(first.classList.contains("lk-near")).toBe(true);
    expect(first.textContent).toBe("Closest matches");
    expect(first.getAttribute("role")).toBeNull();
    expect(opt.querySelector("span")!.textContent).toBe("Nvidia Corp");
    expect(opt.querySelector("mark")).toBeNull();
  });
});

describe("k2 the window while nothing has answered", () => {
  it("opens at once with the name, grey cards and both asks made", async () => {
    mocks.getHistory.mockImplementation(NEVER);
    mocks.getNews.mockImplementation(NEVER);
    wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />);
    const dlg = screen.getByRole("dialog", { name: "Apple Inc." });
    const head = within(dlg).getByRole("heading", { name: /Apple Inc\./ });
    expect(head.textContent).toContain("AAPL");
    expect(within(dlg).getByText("Fetching Apple.")).toBeTruthy();
    expect(within(dlg).getAllByText("Fetching")).toHaveLength(3);
    expect(mocks.getHistory).toHaveBeenCalledWith("AAPL.US", "max");
    expect(mocks.getNews).toHaveBeenCalledWith("AAPL");
  });
});

describe("k3 share price", () => {
  it("says the finding, fills the card and the panel, and asks for hours on 1D", async () => {
    mocks.getHistory.mockImplementation(async (_s: string, period: string, interval?: string) => (interval === "hour" ? hourlyAnswer() : { ...dailyAnswer(), period }));
    mocks.getNews.mockImplementation(NEVER);
    wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />);
    const dlg = screen.getByRole("dialog");
    await waitFor(() => expect(dlg.querySelector(".nl-say b")!.textContent).toBe("Apple closed at 374.75, up 0.1% on the day."));
    expect(dlg.querySelector(".nl-say .more")!.textContent).toBe("It is up 20.2% over a year and sits above its 200-day average.");
    const card = dlg.querySelector<HTMLElement>('[data-name="Share price"]')!;
    expect(card.textContent).toContain("374.75");
    expect(within(card).getByText("Above its 200-day average")).toBeTruthy();
    const facts = Object.fromEntries(Array.from(card.querySelectorAll(".lc-facts > span")).map((s) => [s.querySelector("i")!.textContent, s.querySelector("b")!.textContent]));
    expect(facts).toEqual({ "Lowest in a year": "311.75", "200-day average": "349.88", "Highest in a year": "374.75" });
    const panel = screen.getByRole("heading", { name: "Share price" }).closest("section")!;
    const moves = Object.fromEntries(Array.from(panel.querySelectorAll(".lk-kv > div")).map((d) => [d.querySelector("dt")!.textContent!.split(",")[0], d.querySelector("dd")!.textContent]));
    expect(moves["Day"]).toBe("+0.1%");
    expect(moves["1 month"]).toBe("+1.4%");
    expect(moves["1 year"]).toBe("+20.2%");
    expect(moves["5 years"]).toBe("+527.2%");
    expect(within(panel).getByRole("button", { name: "1Y" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(within(panel).getByRole("button", { name: "1D" }));
    await waitFor(() => expect(mocks.getHistory).toHaveBeenCalledWith("AAPL.US", "1d", "hour"));
  });
});

describe("k4 hour labels", () => {
  it("shows an hourly bar at its end, in Paris time", () => {
    expect(hourLabel("2026-11-13T20:00:00Z", true)).toBe("Fri 13 Nov, 22:00");
    expect(hourLabel("2026-11-13T21:00:00Z", false)).toBe("Fri 13 Nov, 23:00, outside regular hours");
  });
});

describe("k5 news", () => {
  const rows = (dlg: HTMLElement) => dlg.querySelectorAll("table tbody tr");
  it("fills the card and ten rows, and shows more on request", async () => {
    mocks.getHistory.mockImplementation(NEVER);
    mocks.getNews.mockResolvedValue(newsAnswer(12, 19));
    wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />);
    const dlg = screen.getByRole("dialog");
    const card = await waitFor(() => {
      const c = dlg.querySelector<HTMLElement>('[data-name="News"]')!;
      expect(c.textContent).toContain("headlines that name Apple, of 31 tagged AAPL");
      return c;
    });
    expect(card.querySelector(".lc-fig .num")!.textContent).toBe("12");
    expect(rows(dlg)).toHaveLength(10);
    expect(rows(dlg)[0].querySelector("td")!.textContent).toBe("16 Nov, 13:45");
    const link = within(dlg).getByRole("link", { name: "Apple's plan 1" });
    expect(link.getAttribute("href")).toBe("https://news.example/1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("title")).toBe("S&P note");
    fireEvent.click(within(dlg).getByRole("button", { name: "Show 2 more" }));
    expect(rows(dlg)).toHaveLength(12);
  });

  it("says so when no headline names the company", async () => {
    mocks.getHistory.mockImplementation(NEVER);
    mocks.getNews.mockResolvedValue(newsAnswer(0, 5));
    wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />);
    expect(await screen.findByText("No headline names it in the last 30 days.")).toBeTruthy();
  });
});

describe("k6 prices refused", () => {
  it("says not read, gives the words, and the news still fills", async () => {
    mocks.getHistory.mockRejectedValue(new ApiError("x", 503, { detail: "Prices are down" }));
    mocks.getNews.mockResolvedValue(newsAnswer(3, 0));
    wrap(<LookupWindow company={APPLE} onClose={vi.fn()} />);
    const dlg = screen.getByRole("dialog");
    await waitFor(() => expect(within(dlg).getByText("Not read")).toBeTruthy());
    expect(within(dlg).getByText("Prices are down")).toBeTruthy();
    expect(dlg.querySelector(".nl-say b")!.textContent).toBe("Apple's prices could not be read.");
    await waitFor(() => expect(dlg.querySelector('[data-name="News"]')!.textContent).toContain("headlines that name Apple"));
  });
});
