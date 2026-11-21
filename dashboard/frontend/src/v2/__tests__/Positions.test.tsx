import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Positions from "../pages/portfolio/Positions";
import { APPS, pageFor } from "../shell/sections";

const getOverview = vi.fn();
vi.mock("../data/moneyApi", () => ({
  getOverview: () => getOverview(),
  postRefresh: () => Promise.resolve({ asked_at: "2026-11-18T14:00:00Z", run_url: "https://example.test/run" }),
}));

const app = APPS.find((a) => a.id === "portfolio")!;
const section = app.sections.find((s) => s.id === "positions")!;

const acct = (id: string, name: string, type: string, value: number, extra: Record<string, unknown> = {}) => ({
  account_id: id, name, account_type: type, currency: "EUR", balance: value, value_eur: value,
  institution: "Bank A", sync_status: "success", last_successful_sync_at: "2026-10-22T17:55:00Z",
  is_debt: false, share_of_net_worth: 0.1, ...extra,
});

const synced = (accounts: unknown[]) => ({
  synced: true, read_at: "2026-10-22T18:00:00Z", stale: false, net_worth: 1000, gross_assets: 1000, liabilities: 0,
  financial_assets: 600, unvalued_liability_count: 0, allocation: [], accounts, debts: [], savings_eur: null, series: [], goals: [],
});

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Positions app={app} section={section} /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { getOverview.mockReset(); });

describe("Positions page", () => {
  it("is wired into the Portfolio section list", () => {
    expect(pageFor(app, section)).toBe(Positions);
  });

  it("the header's buttons are Refresh then Hide amounts", async () => {
    getOverview.mockResolvedValue(synced([acct("a2", "Brokerage", "investment", 450)]));
    mount();
    await screen.findByRole("heading", { name: "All positions" });
    const head = document.querySelector(".ph") as HTMLElement;
    expect(Array.from(head.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Refresh", "Hide amounts"]);
  });

  it("has the mockup's sections and lists only the investment accounts", async () => {
    getOverview.mockResolvedValue(synced([
      acct("a1", "Current account", "checking", 400),
      acct("a2", "Brokerage", "investment", 450, { institution: "Broker B" }),
      acct("a3", "Coins", "crypto", 150, { institution: "Chain C" }),
    ]));
    mount();
    expect(await screen.findByRole("heading", { name: "All positions" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Summary" })).toBeInTheDocument();
    const names = Array.from(document.querySelectorAll(".tp-card .tp-name")).map((n) => n.textContent);
    expect(names).toEqual(["Worth now", "Earned", "Biggest account", "Investment accounts"]);
    const table = within(screen.getByTestId("positions"));
    expect(table.getByText("Brokerage")).toBeInTheDocument();
    expect(table.getByText("Coins")).toBeInTheDocument();
    expect(table.queryByText("Current account")).toBeNull();
    expect(screen.getByText("Brokerage", { selector: "td" }).closest("tr")!.textContent).toContain("75%");
  });

  it("says Earned is not read, because Finary gives no money put in", async () => {
    getOverview.mockResolvedValue(synced([acct("a2", "Brokerage", "investment", 450)]));
    mount();
    await screen.findByTestId("positions");
    const earned = Array.from(document.querySelectorAll(".tp-card")).find((c) => c.querySelector(".tp-name")?.textContent === "Earned")!;
    expect(earned.textContent).toContain("Not read");
    expect(earned.getAttribute("title")).toMatch(/money put in/);
  });

  it("with no investment account says so", async () => {
    getOverview.mockResolvedValue(synced([acct("a1", "Current account", "checking", 400)]));
    mount();
    expect(await screen.findByText("You hold no positions yet.")).toBeInTheDocument();
  });

  it("before the first sync shows four not-synced cards and no euro figure", async () => {
    getOverview.mockResolvedValue({ ...synced([]), synced: false, read_at: null, net_worth: null });
    mount();
    expect(await screen.findAllByText("Not synced yet")).toHaveLength(4);
    expect(document.body.textContent).not.toContain("€");
  });
});
