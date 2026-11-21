import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Markets from "../pages/research/Markets";
import { SettingsProvider } from "../settings";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const m = vi.hoisted(() => ({
  listDatasets: vi.fn(),
  priceUniverseMemberDetail: vi.fn(),
  priceUniverseRuns: vi.fn(),
  priceUniverseMembers: vi.fn(),
  renamePriceUniverse: vi.fn(),
  updatePriceUniverseWindow: vi.fn(),
  deletePriceUniverse: vi.fn(),
  addPriceUniverseMembers: vi.fn(),
  removePriceUniverseMember: vi.fn(),
  createPriceUniverse: vi.fn(),
  priceUniverseSectorMix: vi.fn(),
  setPriceUniverseDisplayName: vi.fn(),
  apiGet: vi.fn(),
}));
// The Markets tab's own calls never answer, so it stays in its loading state; every call the
// Universes tab makes is a stand-in.
vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  const wait = () => new Promise(() => undefined);
  const { apiGet, ...research } = m;
  return {
    ...real,
    apiGet,
    api: {
      ...real.api,
      research: { ...real.api.research, ...research },
      display: { getQuotes: wait, getHistory: wait, getYieldCurve: wait, getCommodities: wait, getMarketNews: wait, search: wait },
    },
  };
});

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "data-library")!;

const day = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;
const universe = (id: number, name: string, count: number, from: string | null, to: string | null, made: string) => ({
  id, name: name.toLowerCase().replace(/ /g, "_"), display_name: name, source: "eodhd", symbol: "", timeframe: "1d",
  start_ts: 0, end_ts: 0, status: "ready", rows: null,
  sha256: null, file_path: null, file_bytes: null, last_error: null, created_at: day(made), last_fetched_at: null,
  dataset_type: "price_universe", member_count: count,
  window_start_ts: from ? day(from) : null, window_end_ts: to ? day(to) : null,
});
const NO_NUMBER = "No SEC company number is on file for this ticker";
const NO_INDUSTRY = "The SEC company record names no industry";
const member = (
  ticker: string, name: string | null, first: string | null, last: string | null, bars: number | null,
  sec: { sector?: string | null; sic?: string | null; industry?: string | null; sector_missing?: string | null; industry_missing?: string | null } = {},
) => ({
  ticker, name, added_at: 0, first_bar_ts: first ? day(first) : null, last_bar_ts: last ? day(last) : null, bars,
  sector: null, sic: null, industry: null, sector_missing: null, industry_missing: null, ...sec,
});
const AAPL = member("AAPL", "Apple Inc.", "1980-12-12", "2026-10-20", 11000, { sector: "Technology", sic: "3571", industry: "Electronic Computers" });
const MSFT = member("MSFT", "Microsoft Corp", "1986-03-13", "2026-10-20", 10000, { sector: "Technology", sic: "7372", industry: "Services-Prepackaged Software" });
const ZZZZ = member("ZZZZ", null, null, null, null, { sector_missing: NO_NUMBER, industry_missing: NO_NUMBER });
const KO = member("KO", "Coca-Cola Co", "1962-01-02", "2026-10-20", 15000, { sector: "Trade & Services", sic: "2080", industry: "Beverages" });
const NVDA = member("NVDA", "Nvidia Corp", "1999-01-22", "2026-10-20", 7000, { sector: "Manufacturing", sic: "3674", industry_missing: NO_INDUSTRY });
const TECH = [AAPL, KO, MSFT, NVDA, ZZZZ];
const BANKS = [
  member("BAC", "Bank of America", "1973-02-21", "2026-10-20", 13000, { sector: "Finance", sic: "6021", industry: "National Commercial Banks" }),
  member("JPM", "JPMorgan Chase", "1980-03-17", "2026-10-20", 11500, { sector: "Finance", sic: "6021", industry: "National Commercial Banks" }),
];
const MIX = {
  universes: [{
    universe_id: 7, members: 5,
    sectors: [
      { sector: "Technology", count: 2 }, { sector: "Manufacturing", count: 1 },
      { sector: "Trade & Services", count: 1 }, { sector: null, count: 1 },
    ],
  }],
};
const detail = (id: number, members: unknown[]) => ({ universe_id: id, name: "x", as_of: 0, members });
const run = (id: number, strategy: number, status: string, created: string) => ({
  id, strategy_id: strategy, status, created_at: day(created), dataset_id: 7, name: null,
});
const refused = (status: number, text: string) => new ApiError(`${status} /x`, status, { detail: text }, "/x");

function lists(tech: unknown[] = TECH) {
  m.listDatasets.mockResolvedValue([
    { ...universe(1, "AAPL daily", 0, null, null, "2026-10-05"), dataset_type: "price_dataset", symbol: "AAPL" },
    universe(7, "Tech five", 5, "2015-01-01", "2024-12-31", "2026-10-01"),
    universe(9, "Banks", 2, null, null, "2026-10-10"),
    { ...universe(11, "Test tape", 3, null, null, "2026-10-12"), source: "synthetic" },
  ]);
  m.priceUniverseSectorMix.mockResolvedValue(MIX);
  m.priceUniverseMemberDetail.mockImplementation(async (id: number) => (id === 7 ? detail(7, tech) : detail(9, BANKS)));
  m.priceUniverseRuns.mockImplementation(async (id: number) => ({
    universe_id: id,
    runs: id === 7 ? [run(2, 4, "failed", "2026-10-15"), run(1, 3, "succeeded", "2026-10-12")] : [],
  }));
  m.priceUniverseMembers.mockImplementation(async (id: number) => ({ universe_id: id, members: id === 9 ? ["BAC", "JPM"] : ["AAPL"] }));
}

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
  fireEvent.click(screen.getByRole("button", { name: "Universes" }));
  await screen.findByTestId("u-head");
}
async function openTech() {
  await openTab();
  fireEvent.click(uRow(7));
  await waitFor(() => expect(within(screen.getByTestId("u-head")).getByText("Tech five")).toBeInTheDocument());
  await screen.findByText("AAPL");
}
const uRow = (id: number) => document.querySelector<HTMLElement>(`[data-u="${id}"] button`)!;
const fact = (label: string) =>
  Array.from(document.querySelectorAll<HTMLElement>(".f5 > div")).find((d) => d.querySelector("span")?.textContent === label)!.querySelector("b")!;
const mrows = () => Array.from(screen.getByTestId("u-members").querySelectorAll("tbody tr")) as HTMLElement[];
const mrow = (ticker: string) => mrows().find((r) => within(r).queryByText(ticker))!;
const cells = (r: HTMLElement) => Array.from(r.querySelectorAll("td"));
const dialog = (name: string) => screen.getByRole("dialog", { name });

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-22T12:00:00Z"));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  lists();
  m.apiGet.mockResolvedValue([
    { id: 3, name: "mom_v1", display_name: "Momentum", status: "active" },
    { id: 4, name: "rev_v1", display_name: "Reversal", status: "active" },
  ]);
  m.renamePriceUniverse.mockResolvedValue({ universe_id: 7, name: "Tech six" });
  m.setPriceUniverseDisplayName.mockResolvedValue({ universe_id: 7, name: "tech_five", display_name: "Tech six" });
  m.updatePriceUniverseWindow.mockResolvedValue({ universe_id: 7 });
  m.deletePriceUniverse.mockResolvedValue({ ok: true });
  m.addPriceUniverseMembers.mockResolvedValue({ universe_id: 7, added: ["NVDA", "TSLA"], excluded: [], already: [] });
  m.removePriceUniverseMember.mockResolvedValue({ universe_id: 7, removed: "AAPL" });
  m.createPriceUniverse.mockResolvedValue({ universe_id: 20, name: "Mine", resolved: ["BAC", "JPM"], excluded: [], member_count: 2 });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const f of Object.values(m)) f.mockReset();
});

describe("Data library, Universes tab", () => {
  it("(u1) the tab row reads Markets, Datasets, Universes, Events, Fundamentals", () => {
    mount();
    const labels = Array.from(document.querySelectorAll(".mk-tabs .seg button")).map((b) => b.textContent);
    expect(labels).toEqual(["Markets", "Datasets", "Universes", "Events", "Fundamentals"]);
  });

  it("(u2) newest first, the newest opens, and the hover on each row", async () => {
    await openTab();
    const ids = Array.from(document.querySelectorAll("[data-u]")).map((r) => r.getAttribute("data-u"));
    expect(ids).toEqual(["9", "7"]);
    expect(screen.getByText("2 stock lists, 7 members in all.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Stock lists" })).toBeInTheDocument();
    expect(within(screen.getByTestId("u-head")).getByText("Banks")).toBeInTheDocument();
    expect(uRow(7).getAttribute("title")).toContain("Window: 1 Jan 2015 to 31 Dec 2024");
    expect(uRow(9).getAttribute("title")).toContain("Window: every bar");
    expect(uRow(7).getAttribute("title")).toContain("Members: 5");
    expect(uRow(7).getAttribute("title")).toContain("Made: 1 Oct 2026");
    expect(uRow(7).textContent).toContain("5");
  });

  it("(u2b) the search box, the five-row limit and the toggle", async () => {
    const many = Array.from({ length: 7 }, (_, i) => universe(30 + i, `List ${i}`, 1, null, null, `2026-09-0${i + 1}`));
    m.listDatasets.mockResolvedValue(many);
    m.priceUniverseMemberDetail.mockResolvedValue(detail(36, BANKS));
    await openTab();
    expect(document.querySelectorAll("[data-u]").length).toBe(5);
    fireEvent.click(screen.getByRole("button", { name: "All universes 7" }));
    expect(document.querySelectorAll("[data-u]").length).toBe(7);
    expect(screen.getByRole("button", { name: "Newest five 7" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Find a universe"), { target: { value: "list 3" } });
    expect(document.querySelectorAll("[data-u]").length).toBe(1);
  });

  it("(u2c) with no universe only New list is shown", async () => {
    m.listDatasets.mockResolvedValue([]);
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Universes" }));
    await screen.findByRole("button", { name: "New list" });
    expect(screen.queryByTestId("u-head")).toBeNull();
  });

  it("(u3) the facts of Tech five", async () => {
    await openTech();
    await waitFor(() => expect(fact("Runs").textContent).toBe("2"));
    expect(fact("Members").textContent).toBe("5");
    expect(fact("Window").textContent).toBe("1 Jan 2015 to 31 Dec 2024");
    expect(fact("With daily prices").textContent).toBe("4 of 5");
    expect(fact("With daily prices").className).toContain("a");
    expect(fact("Days of prices saved").textContent).toBe("43,000");
  });

  it("(u3b) all members with prices is green and the window reads every bar", async () => {
    await openTab();
    await waitFor(() => expect(fact("With daily prices").textContent).toBe("2 of 2"));
    expect(fact("With daily prices").className).toContain("g");
    expect(fact("Window").textContent).toBe("every bar");
    expect(fact("Days of prices saved").textContent).toBe("24,500");
    expect(fact("Runs").textContent).toBe("0");
  });

  it("(u4) the year area counts members touching each year", async () => {
    await openTech();
    const title = (y: number) => document.querySelector(`[data-year="${y}"] title`)?.textContent?.split("\n")[0];
    expect(title(1970)).toBe("1970: 1 of 5 with prices");
    expect(title(1990)).toBe("1990: 3 of 5 with prices");
    expect(title(2000)).toBe("2000: 4 of 5 with prices");
    expect(title(2026)).toBe("2026: 4 of 5 with prices");
    expect(document.querySelector('[data-testid="u-area"] [data-window]')).not.toBeNull();
  });

  it("(u5) the members table: a member without bars, the Prices line and the search", async () => {
    await openTech();
    const z = cells(mrow("ZZZZ"));
    expect(z[2].textContent).toBe("");
    expect(z[2].querySelector("b")).toBeNull();
    expect(z[3].textContent).toBe("-");
    expect(cells(mrow("AAPL"))[3].textContent).toBe("11,000");
    expect(cells(mrow("AAPL"))[2].querySelector("b")).not.toBeNull();
    expect(cells(mrow("AAPL"))[2].querySelector(".tl")!.getAttribute("title")).toContain("First bar: 12 Dec 1980");
    expect(cells(mrow("AAPL"))[2].querySelector(".tl em")).not.toBeNull();
    fireEvent.change(screen.getByLabelText("Find a member"), { target: { value: "coca" } });
    expect(mrows().length).toBe(1);
  });

  it("(u5b) 35 members show 30", async () => {
    const many = Array.from({ length: 35 }, (_, i) => member(`T${String(i).padStart(2, "0")}`, `Name ${i}`, "2000-01-03", "2026-10-20", 100));
    lists(many);
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Universes" }));
    await screen.findByTestId("u-head");
    fireEvent.click(uRow(7));
    await screen.findByText("T00");
    expect(mrows().length).toBe(30);
    fireEvent.click(screen.getByRole("button", { name: "Show 5 more" }));
    expect(mrows().length).toBe(35);
  });

  it("(u6) REMOVE, the toast and UNDO", async () => {
    await openTech();
    fireEvent.click(within(mrow("AAPL")).getByRole("button", { name: "Remove AAPL" }));
    await waitFor(() => expect(m.removePriceUniverseMember).toHaveBeenCalledWith(7, "AAPL"));
    const toast = await screen.findByRole("status");
    expect(toast.textContent).toContain("Removed AAPL from Tech five");
    fireEvent.click(within(toast).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(m.addPriceUniverseMembers).toHaveBeenCalledWith(7, ["AAPL"]));
  });

  it("(u6b) a refusal shows the server's words", async () => {
    m.removePriceUniverseMember.mockRejectedValue(refused(422, "price_universe_last_member: a universe keeps at least one member"));
    await openTech();
    fireEvent.click(within(mrow("KO")).getByRole("button", { name: "Remove KO" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("A universe keeps at least one member"));
    m.removePriceUniverseMember.mockRejectedValue(refused(404, "member_not_found: MSFT is not a member"));
    fireEvent.click(within(mrow("MSFT")).getByRole("button", { name: "Remove MSFT" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("MSFT is not a member"));
  });

  it("(u7) the display name, edited in place by click and Enter, calls the server once", async () => {
    await openTech();
    fireEvent.click(within(screen.getByTestId("u-head")).getByText("Tech five"));
    const box = screen.getByLabelText("Universe name");
    fireEvent.change(box, { target: { value: "Tech six" } });
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.blur(box);
    await waitFor(() => expect(m.setPriceUniverseDisplayName).toHaveBeenCalledTimes(1));
    expect(m.setPriceUniverseDisplayName).toHaveBeenCalledWith(7, "Tech six");
    expect(m.renamePriceUniverse).not.toHaveBeenCalled();
  });

  it("(u8) ADD MEMBERS sends the tickers upper-cased and once each", async () => {
    await openTech();
    fireEvent.click(screen.getByRole("button", { name: "Add members" }));
    const d = dialog("Add members to Tech five");
    fireEvent.change(within(d).getByLabelText("Tickers"), { target: { value: "nvda, tsla nvda" } });
    fireEvent.click(within(d).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(m.addPriceUniverseMembers).toHaveBeenCalledWith(7, ["NVDA", "TSLA"]));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("(u8b) a 422 keeps the dialog open with the server's words", async () => {
    m.addPriceUniverseMembers.mockRejectedValue(refused(422, "price_universe_add_nothing: no ticker was added."));
    await openTech();
    fireEvent.click(screen.getByRole("button", { name: "Add members" }));
    const d = dialog("Add members to Tech five");
    fireEvent.change(within(d).getByLabelText("Tickers"), { target: { value: "qqqq" } });
    fireEvent.click(within(d).getByRole("button", { name: "Add" }));
    await within(d).findByText("No ticker was added.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("(u9) EDIT with only the end date changed sends the window and no rename", async () => {
    await openTech();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const d = dialog("Edit Tech five");
    fireEvent.click(within(d).getByRole("button", { name: "To" }));
    fireEvent.click(screen.getByRole("button", { name: "TODAY" }));
    fireEvent.click(within(d).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.updatePriceUniverseWindow).toHaveBeenCalledWith(7, day("2015-01-01"), day("2026-10-22")));
    expect(m.renamePriceUniverse).not.toHaveBeenCalled();
  });

  it("(u10) DELETE says how many runs keep their results and deletes", async () => {
    await openTech();
    await waitFor(() => expect(fact("Runs").textContent).toBe("2"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const d = dialog("Delete Tech five?");
    expect(d.textContent).toContain("Its 2 runs keep their results.");
    fireEvent.click(within(d).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(m.deletePriceUniverse).toHaveBeenCalledWith(7));
  });

  it("(u10b) deleting the last universe in the list opens the one before it", async () => {
    await openTech();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    m.listDatasets.mockResolvedValue([universe(9, "Banks", 2, null, null, "2026-10-10")]);
    fireEvent.click(within(dialog("Delete Tech five?")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(within(screen.getByTestId("u-head")).getByText("Banks")).toBeInTheDocument());
  });

  it("(u11) NEW with no name sends nothing; A copy of Banks posts its two tickers", async () => {
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "New list" }));
    const d = dialog("New universe");
    fireEvent.click(within(d).getByRole("button", { name: "Create" }));
    expect(m.createPriceUniverse).not.toHaveBeenCalled();
    fireEvent.click(within(d).getByRole("button", { name: "A copy" }));
    fireEvent.change(within(d).getByLabelText("Display name"), { target: { value: "My banks" } });
    fireEvent.change(within(d).getByLabelText("Copy of"), { target: { value: "9" } });
    fireEvent.click(within(d).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(m.createPriceUniverse).toHaveBeenCalledWith("my_banks", ["BAC", "JPM"], day("2010-01-01"), day("2026-10-22"), "My banks"));
    expect(m.priceUniverseMembers).toHaveBeenCalledWith(9);
    const toast = await screen.findByRole("status");
    expect(toast.textContent).toContain("Made My banks, 2 members");
  });

  it("(u12) HISTORY: the runs newest first, then Made", async () => {
    await openTech();
    await waitFor(() => expect(screen.getByTestId("u-hist").children.length).toBe(3));
    const rows = Array.from(screen.getByTestId("u-hist").children).map((r) => r.textContent ?? "");
    expect(rows[0]).toContain("Reversal");
    expect(rows[0]).toContain("Failed");
    expect(rows[1]).toContain("Momentum");
    expect(rows[1]).toContain("Done");
    expect(rows[2]).toContain("The list was made");
    expect(rows[2]).toContain("1 Oct 26");
  });

  it("(u13) RUN A SWEEP is enabled and opens the Run a sweep window on this list", async () => {
    await openTech();
    const b = screen.getByRole("button", { name: "Run a sweep" });
    expect(b).toBeEnabled();
    expect(b.getAttribute("title")).toBeNull();
    fireEvent.click(b);
    const win = dialog("Run a sweep");
    await waitFor(() => expect(within(win).getByLabelText("Stocks")).toHaveValue("7"));
    expect(within(win).getByRole("option", { name: "Tech five, 5 stocks" })).toBeInTheDocument();
    expect(within(win).getByText("01/01/2015 to 31/12/2024")).toBeInTheDocument();
  });

  // The sector key: one colour per sector, read from the mockup's own list.
  const TECH_BLUE = "#29c6ff";
  const TECH_RGB = "rgb(41, 198, 255)";
  const arcs = () => Array.from(document.querySelectorAll<HTMLElement>(".un-kbar i"));
  const legendBtns = () => Array.from(document.querySelectorAll<HTMLElement>(".un-kinds button"));
  const legendText = (b: HTMLElement) => Array.from(b.children).filter((c) => c.tagName !== "I").map((c) => c.textContent).join(" ");

  it("(u15) By sector: one bar, its legend and the colour of Technology everywhere", async () => {
    await openTech();
    expect(arcs().map((a) => a.getAttribute("data-sector"))).toEqual(["Technology", "Manufacturing", "Trade & Services", "none"]);
    expect(legendBtns().map(legendText)).toEqual(["Technology 2", "Manufacturing 1", "Trade and services 1", "Not on file 1"]);
    expect(arcs()[0].style.background).toBe(TECH_RGB);
    expect(legendBtns()[0].style.getPropertyValue("--k")).toBe(TECH_BLUE);
    expect((mrow("AAPL").querySelector("i.fd") as HTMLElement).style.background).toBe(TECH_RGB);
  });

  it("(u16) the sector shows as the ticker's colour and hover, and the Prices line colour", async () => {
    await openTech();
    const th = Array.from(screen.getByTestId("u-members").querySelectorAll("th")).map((h) => h.textContent);
    expect(th.slice(0, 4)).toEqual(["Ticker", "Name", `Prices, ${1962} to today`, "Days of prices"]);
    const tick = (t: string) => cells(mrow(t))[0];
    expect(tick("AAPL").getAttribute("title")).toBe("Industry: Electronic Computers, SIC 3571");
    expect(tick("NVDA").getAttribute("title")).toBe(`Industry: ${NO_INDUSTRY}`);
    expect(tick("ZZZZ").getAttribute("title")).toBe(NO_NUMBER);
    expect((cells(mrow("AAPL"))[2].querySelector(".tl b") as HTMLElement).style.background).toBe(TECH_RGB);
  });

  it("(u17) the year area holds one layer per sector, with a hover line for each", async () => {
    await openTech();
    const layers = Array.from(document.querySelectorAll('[data-testid="u-area"] path[data-sector]'));
    expect(layers.map((p) => p.getAttribute("data-sector"))).toEqual(["Technology", "Manufacturing", "Trade & Services", "none"]);
    expect(layers[0].getAttribute("fill")).toBe(TECH_BLUE);
    expect(layers[0].getAttribute("opacity")).toBe(".78");
    const lines = (document.querySelector('[data-year="2000"] title')?.textContent ?? "").split("\n");
    expect(lines[0]).toBe("2000: 4 of 5 with prices");
    expect(lines).toContain("Technology: 2");
    expect(lines).toContain("Manufacturing: 1");
    expect(lines.some((l) => l.startsWith("Not on file"))).toBe(false);
  });

  it("(u18) a sector press keeps that sector's members, dims the other segments, and a second press clears it", async () => {
    await openTech();
    fireEvent.click(legendBtns()[0]);
    expect(mrows().length).toBe(2);
    expect(arcs().map((a) => a.className)).toEqual(["", "off", "off", "off"]);
    expect(legendBtns()[0].className).toContain("on");
    fireEvent.click(legendBtns()[0]);
    expect(mrows().length).toBe(5);
    expect(arcs().map((a) => a.className)).toEqual(["", "", "", ""]);
    fireEvent.click(arcs()[1]);
    expect(mrows().map((r) => cells(r)[0].textContent)).toEqual(["NVDA"]);
  });

  it("(u19) each list row carries a sector share bar", async () => {
    await openTech();
    const bar = uRow(7).querySelector('.lc-bar i[title="Technology: 2"]')!;
    expect(bar).not.toBeNull();
    expect(uRow(7).querySelectorAll(".lc-bar i").length).toBe(4);
  });

  it("(u19b) a refused sector-mix read shows a dash with the server's words", async () => {
    m.priceUniverseSectorMix.mockRejectedValue(refused(500, "sector_mix_unavailable: the sector count is not ready."));
    await openTab();
    await waitFor(() => expect(uRow(7).getAttribute("title")).toContain("The sector count is not ready."));
    expect(uRow(7).querySelector(".lc-bar i")).toBeNull();
  });

  it("(u20) display names only; edits send only what changed", async () => {
    await openTech();
    expect(document.body.textContent).not.toContain("tech_five");
    expect(document.querySelector('[title*="tech_five"]')).toBeNull();
    expect(uRow(7).getAttribute("title")!.split("\n")[0]).toBe("Tech five");
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const d = dialog("Edit Tech five");
    expect(within(d).getByLabelText("Display name")).toHaveValue("Tech five");
    fireEvent.change(within(d).getByLabelText("Display name"), { target: { value: "Tech six" } });
    fireEvent.click(within(d).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.setPriceUniverseDisplayName).toHaveBeenCalledWith(7, "Tech six"));
    expect(m.renamePriceUniverse).not.toHaveBeenCalled();
    expect(m.updatePriceUniverseWindow).not.toHaveBeenCalled();
  });

  it("(u20b) a universe with no display name shows a dash with its reason", async () => {
    m.listDatasets.mockResolvedValue([{ ...universe(7, "Tech five", 5, null, null, "2026-10-01"), display_name: null }]);
    await openTab();
    const head = within(screen.getByTestId("u-head"));
    expect(head.getByText("-").getAttribute("title")).toBe("No display name");
    expect(uRow(7).getAttribute("title")).toContain("No display name");
    fireEvent.click(screen.getByRole("button", { name: "Add members" }));
    expect(dialog("Add members to this universe")).toBeInTheDocument();
  });

  it("(u21) NEW needs a display name, then sends it as the fifth argument", async () => {
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "New list" }));
    const d = dialog("New universe");
    expect(within(d).queryByLabelText("Name")).toBeNull();
    fireEvent.change(within(d).getByLabelText("Tickers"), { target: { value: "bac jpm" } });
    fireEvent.click(within(d).getByRole("button", { name: "Create" }));
    expect(m.createPriceUniverse).not.toHaveBeenCalled();
    fireEvent.change(within(d).getByLabelText("Display name"), { target: { value: "  My Banks & Co.  " } });
    fireEvent.click(within(d).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(m.createPriceUniverse).toHaveBeenCalledWith("my_banks_co", ["BAC", "JPM"], day("2010-01-01"), day("2026-10-22"), "My Banks & Co."));
  });

  it("(u21b) a refused create shows the server's words under the display name and keeps the window open", async () => {
    m.createPriceUniverse.mockRejectedValue(refused(409, "price_universe_name_exists: a universe with this name already exists."));
    await openTab();
    fireEvent.click(screen.getByRole("button", { name: "New list" }));
    const d = dialog("New universe");
    fireEvent.change(within(d).getByLabelText("Display name"), { target: { value: "Banks" } });
    fireEvent.change(within(d).getByLabelText("Tickers"), { target: { value: "bac" } });
    fireEvent.click(within(d).getByRole("button", { name: "Create" }));
    await within(d).findByText("A universe with this name already exists.");
    expect(screen.getByRole("dialog", { name: "New universe" })).toBeInTheDocument();
  });

  it("(u24) EDIT's request body holds the display name and no name", async () => {
    const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
    const fetchStub = vi.fn().mockResolvedValue(new Response(JSON.stringify({ universe_id: 7, name: "tech_five", display_name: "Tech six" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchStub);
    await actual.api.research.setPriceUniverseDisplayName(7, "Tech six");
    const [url, init] = fetchStub.mock.calls[0];
    expect(url).toBe("/api/research/price-universes/7");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ display_name: "Tech six" });
    await openTech();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(within(dialog("Edit Tech five")).queryByLabelText("Name")).toBeNull();
  });

  it("(u22) test universes are asked for, hidden by default and shown on request", async () => {
    await openTab();
    expect(m.listDatasets).toHaveBeenCalledWith("active", true);
    expect(uRow(11)).toBeNull();
    expect(screen.getByRole("button", { name: "All universes 2" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show test universes 1" }));
    expect(uRow(11)).not.toBeNull();
    expect(screen.getByRole("button", { name: "Hide test universes 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All universes 3" })).toBeInTheDocument();
  });

  it("(u23) a universe with no members draws no ring, share bar or area, and says why", async () => {
    m.listDatasets.mockResolvedValue([
      universe(12, "Empty list", 0, null, null, "2026-10-20"),
      universe(9, "Banks", 2, null, null, "2026-10-10"),
    ]);
    m.priceUniverseSectorMix.mockResolvedValue({ universes: [{ universe_id: 12, members: 0, sectors: [] }] });
    m.priceUniverseMemberDetail.mockImplementation(async (id: number) => (id === 12 ? detail(12, []) : detail(9, BANKS)));
    await openTab();
    await waitFor(() => expect(within(screen.getByTestId("u-head")).getByText("Empty list")).toBeInTheDocument());
    await waitFor(() => expect(uRow(12).getAttribute("title")).toContain("This universe has no members today"));
    expect(document.querySelectorAll("[data-arc]").length).toBe(0);
    expect(uRow(12).querySelector(".lc-bar i")).toBeNull();
    expect(document.querySelectorAll("path[data-sector]").length).toBe(0);
    expect(uRow(9).getAttribute("title")).toContain("No sector count is held for this universe");
  });


  it("(u14) no snake_case and no em dash on the tab", async () => {
    await openTech();
    await waitFor(() => expect(screen.getByTestId("u-hist").children.length).toBe(3));
    const text = document.querySelector(".mk")!.textContent!;
    expect(text).not.toMatch(/[a-z]+_[a-z0-9_]+/);
    expect(text).not.toContain(String.fromCharCode(8212));
  });
});
