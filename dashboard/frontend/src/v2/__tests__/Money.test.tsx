import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Money from "../pages/portfolio/Money";
import { netAxisFormat } from "../pages/portfolio/moneyCharts";
import { APPS, pageFor } from "../shell/sections";
import { STORAGE_KEY } from "../settings";

const getOverview = vi.fn();
const lineProps = vi.fn();

// The kit's LineChart is wrapped so a test can read the props Money gives it; it still draws.
vi.mock("../kit", async (importOriginal) => {
  const real = await importOriginal<typeof import("../kit")>();
  return { ...real, LineChart: (props: Record<string, unknown>) => { lineProps(props); return real.LineChart(props as never); } };
});

vi.mock("../data/moneyApi", () => ({
  getOverview: () => getOverview(),
  postRefresh: () => Promise.resolve({ asked_at: "2026-11-18T14:00:00Z", run_url: "https://example.test/run" }),
}));

const app = APPS.find((a) => a.id === "portfolio")!;
const section = app.sections.find((s) => s.id === "money")!;
const TS = ",";

const acct = (id: string, name: string, type: string, value: number, extra: Record<string, unknown> = {}) => ({
  account_id: id, name, account_type: type, currency: "EUR", balance: value, value_eur: value,
  institution: "Bank A", sync_status: "success", last_successful_sync_at: "2026-10-22T17:55:00Z",
  is_debt: false, share_of_net_worth: value / 100000, ...extra,
});

const OK_ACCOUNTS = [
  acct("a1", "Current account", "checking", 20000),
  acct("a2", "Livret A", "saving", 40000, { institution: "Bank B" }),
  acct("a3", "Cash wallet", "cash", 5000, { institution: null, sync_status: null, last_successful_sync_at: null }),
];

const SERIES = [
  { read_at: "2026-09-30T22:30:00Z", net_worth: 97500 },
  { read_at: "2026-10-01T05:00:00Z", net_worth: 98000 },
  { read_at: "2026-10-10T05:00:00Z", net_worth: 99000 },
  { read_at: "2026-10-22T18:00:00Z", net_worth: 100000 },
];

const synced = (extra: Record<string, unknown> = {}) => ({
  synced: true, read_at: "2026-10-22T18:00:00Z", stale: false,
  net_worth: 100000, gross_assets: 120000, liabilities: 20000, financial_assets: 60000,
  unvalued_liability_count: 0, allocation: [], accounts: OK_ACCOUNTS,
  debts: [{ ...acct("d1", "Credit card", "credit", -3000, { is_debt: true }) }],
  savings_eur: 40000, series: SERIES,
  goals: [{ goal_id: "g1", name: "House", target_amount: 50000, current_amount: 20000, currency: "EUR", status: "on_track", target_date: "2030-01-01" }],
  ...extra,
});

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Money app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const cards = () => Array.from(document.querySelectorAll(".tp-card")) as HTMLElement[];
const ready = () => screen.findByText("Livret A", { selector: "td" });
const row = (name: string) => within(screen.getByTestId("accounts")).getByText(name, { selector: "td" }).closest("tr") as HTMLElement;

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  getOverview.mockResolvedValue(synced());
});
afterEach(() => {
  localStorage.clear();
  lineProps.mockReset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  getOverview.mockReset();
});

describe("Money page", () => {
  it("is wired into the Portfolio section list", () => {
    expect(app.sections.map((s) => s.id)).toEqual(["positions", "money", "budget"]);
    expect(pageFor(app, section)).toBe(Money);
  });

  it("has the mockup's sections: needs you, summary, the line, gains, where it sits, goals", async () => {
    mount();
    await ready();
    for (const h of ["Needs you", "Summary", "Net worth over time", "Gained by year", "Where it sits", "Goals"]) {
      expect(screen.getByRole("heading", { name: h })).toBeInTheDocument();
    }
    expect(cards().map((c) => c.querySelector(".tp-name")!.textContent)).toEqual(["Net worth", "This year", "Savings", "Accounts"]);
  });

  it("(a1) says not synced yet and shows no euro figure before the first sync", async () => {
    getOverview.mockResolvedValue({
      synced: false, read_at: null, stale: null, net_worth: null, gross_assets: null, liabilities: null,
      financial_assets: null, unvalued_liability_count: null, allocation: [], accounts: [], debts: [],
      savings_eur: null, series: [], goals: [],
    });
    mount();
    expect((await screen.findAllByText("Not synced yet"))).toHaveLength(4);
    expect(document.body.textContent).not.toContain("€");
  });

  it("(a2) shows the net worth, accounts with share, owed, and one state word per account", async () => {
    mount();
    await ready();
    expect(cards()[0].textContent).toContain(`€100${TS}000`);
    const livret = row("Livret A");
    expect(livret.textContent).toContain("33%");
    expect(livret.textContent).toContain(`€40${TS}000`);
    expect(within(screen.getByTestId("accounts")).queryByText("Credit card")).toBeNull();
    const owed = screen.getByTestId("owed");
    expect(owed.textContent).toContain("Owed");
    expect(owed.textContent).toContain("Credit card");
    expect(owed.textContent).toContain(`€3${TS}000`);
    expect(within(screen.getByTestId("accounts")).getAllByRole("row")).toHaveLength(4);
    const word = (name: string) => row(name).querySelector(".nl-st")!.textContent;
    expect(word("Current account")).toBe("In sync");
    expect(word("Livret A")).toBe("In sync");
    expect(word("Cash wallet")).toBe("By hand");
  });

  it("(a2) a connection that is not success needs reconnecting", async () => {
    getOverview.mockResolvedValue(
      synced({ accounts: [acct("a1", "Current account", "checking", 20000, { sync_status: "login_error" }), OK_ACCOUNTS[2]] }),
    );
    mount();
    await screen.findByText("Current account", { selector: "td" });
    expect(row("Current account").querySelector(".nl-st")!.textContent).toBe("Needs reconnect");
    expect(cards()[3].textContent).toContain("1 needs you");
  });

  it("(a3) a stale snapshot puts Stale on every account", async () => {
    getOverview.mockResolvedValue(synced({ stale: true }));
    mount();
    await ready();
    const words = Array.from(document.querySelectorAll("[data-testid='accounts'] .nl-st")).map((s) => s.textContent);
    expect(words).toEqual(["Stale", "Stale", "Stale"]);
  });

  it("(a3) the stale stop tells him to press Refresh", async () => {
    getOverview.mockResolvedValue(synced({ stale: true }));
    mount();
    await ready();
    expect(document.body.textContent).toContain("Press Refresh to read Finary again");
  });

  it("the header's buttons are Refresh then Hide amounts", async () => {
    mount();
    await ready();
    const head = document.querySelector(".ph") as HTMLElement;
    expect(Array.from(head.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Refresh", "Hide amounts"]);
  });

  it("(a4) net worth, savings and accounts cards", async () => {
    mount();
    await ready();
    expect(cards()[0].textContent).toContain(`+€2${TS}500`);
    expect(cards()[2].textContent).toContain(`€40${TS}000`);
    expect(cards()[2].textContent).toContain("33%");
    expect(cards()[3].textContent).toContain("3");
    expect(cards()[3].textContent).toContain("All in sync");
  });

  it("(a4) This year is a dash with no reading from last year", async () => {
    getOverview.mockResolvedValue(synced({ series: [SERIES[3]] }));
    mount();
    await ready();
    expect(cards()[1].querySelector(".tp-fig span")!.textContent).toBe("-");
    expect(cards()[1].querySelector(".tp-fig span")!.getAttribute("title")).toMatch(/before this year/i);
  });

  it("(a5) the net worth line has one point per sync", async () => {
    mount();
    await ready();
    const d = document.querySelector(".lcbox path.ln")!.getAttribute("d")!;
    expect(d.match(/[ML]/g)).toHaveLength(SERIES.length);
  });

  it("(a6) a goal shows its progress track and Finary's status word", async () => {
    mount();
    await screen.findByText("House");
    const goal = screen.getByTestId("goals");
    expect(goal.textContent).toContain("On track");
    expect(goal.querySelector(".tp-share")).not.toBeNull();
    expect(goal.textContent).toContain("40%");
  });

  it("(a6) no goals says so", async () => {
    getOverview.mockResolvedValue(synced({ goals: [] }));
    mount();
    await ready();
    expect(screen.getByTestId("goals").textContent).toContain("None");
    expect(screen.getByTestId("goals").textContent).toContain("No goal is set in Finary");
  });

  it("(a7) a card's hover gives the read time in Paris time", async () => {
    mount();
    await ready();
    expect(screen.getAllByTitle(/Read from Finary 22 Oct, 20:00/).length).toBeGreaterThanOrEqual(4);
  });

  it("(b6) the visible text has no snake_case", async () => {
    mount();
    await ready();
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("title").forEach((t) => t.remove());
    expect(clone.textContent).not.toMatch(/[a-z]+_[a-z]+/);
    await waitFor(() => expect(document.body.textContent).not.toContain("on_track"));
  });

  it("(m8) netAxisFormat gives each tick of the six hand cases its own true text", () => {
    const ticks = (from: number, to: number, step: number) => {
      const out: number[] = [];
      for (let v = from; v <= to + step * 1e-6; v += step) out.push(+v.toFixed(8));
      return out;
    };
    const cases: Array<[number[], number[], string[]]> = [
      [[120, 176], ticks(120, 160, 20), ["€120", "€140", "€160"]],
      [[50000, 64000], ticks(50000, 60000, 5000), ["€50k", "€55k", "€60k"]],
      [[9000, 12500], ticks(9000, 12000, 1000), ["€9k", "€10k", "€11k", "€12k"]],
      [[176, 176.4], ticks(176, 176.4, 0.1), ["€176.0", "€176.1", "€176.2", "€176.3", "€176.4"]],
      [[176, 176.01], ticks(176, 176.01, 0.002), ["€176.000", "€176.002", "€176.004", "€176.006", "€176.008", "€176.010"]],
      [[-12000, -2000], ticks(-12000, -2000, 2000), ["-€12k", "-€10k", "-€8k", "-€6k", "-€4k", "-€2k"]],
    ];
    for (const [values, tickList, want] of cases) {
      const f = netAxisFormat(values);
      const got = tickList.map((t) => f(t));
      expect(got).toEqual(want);
      expect(new Set(got).size).toBe(got.length);
    }
  });

  it("(m10) the phone gives the net worth line room for its date labels, the desktop keeps the kit's size", async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout: "phone" }));
    mount();
    await ready();
    const last = () => lineProps.mock.calls[lineProps.mock.calls.length - 1][0];
    expect(last().pad).toBeUndefined();
    expect(last().minHeight).toBe(230);
    cleanup();
    lineProps.mockReset();
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout: "desktop" }));
    mount();
    await ready();
    expect(last().pad).toBeUndefined();
    expect(last().minHeight).toBeUndefined();
  });

  it("(m9)a 120 then 176 euro balance reads €120 on the axis, never a rounded k", async () => {
    getOverview.mockResolvedValue(synced({
      net_worth: 176,
      series: [{ read_at: "2026-10-10T05:00:00Z", net_worth: 120 }, { read_at: "2026-10-22T18:00:00Z", net_worth: 176 }],
    }));
    mount();
    await ready();
    const axis = Array.from(document.querySelectorAll(".lcbox svg text")).map((t) => t.textContent ?? "");
    expect(axis).toContain("€120");
    expect(axis.filter((t) => /€\dk/.test(t))).toEqual([]);
  });
});
