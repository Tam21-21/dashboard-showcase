import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Budget from "../pages/portfolio/Budget";
import { APPS, pageFor } from "../shell/sections";

const getBudget = vi.fn();
const putTarget = vi.fn();
const deleteTarget = vi.fn();

vi.mock("../data/moneyApi", () => ({
  getBudget: () => getBudget(),
  putTarget: (slug: string, amount: number) => putTarget(slug, amount),
  deleteTarget: (slug: string) => deleteTarget(slug),
}));

const app = APPS.find((a) => a.id === "portfolio")!;
const section = app.sections.find((s) => s.id === "budget")!;
const TS = ",";

const cat = (slug: string, label: string | null, spent: number, target: number | null, extra: Record<string, unknown> = {}) => ({
  slug, label, income: 0, spent, target,
  pace: target === null ? null : +((target * 22) / 31).toFixed(2),
  ahead: null, warn: false, suggestion: null, ...extra,
});

const THIS = {
  from: "2026-10-01", to: "2026-10-31", is_partial: true, elapsed_days: 22, total_days: 31,
  income: 3000, expenses: 1413.15, net_cashflow: 1586.85, savings_rate_percent: 52,
  categories: [
    cat("food_and_beverage", "Food and Beverage", 323.39, 320, { ahead: 0.424, warn: true, suggestion: 300 }),
    cat("transport", "Transport", 50, 200, { ahead: -0.648, suggestion: 180 }),
    cat("leisure", "Leisure", 80, null, { suggestion: 120 }),
    cat("misc_unlabelled", null, 10, null),
  ],
};

const LAST = {
  from: "2026-09-01", to: "2026-09-30", income: 3000, expenses: 2400, net_cashflow: 600,
  categories: [
    { slug: "salary", label: "Salary", spent: 0, income: 3000 },
    { slug: "rent", label: "Rent", spent: 1200, income: 0 },
    { slug: "food_and_beverage", label: "Food and Beverage", spent: 600, income: 0 },
    { slug: "leisure", label: "Leisure", spent: 600, income: 0 },
  ],
};

const month = (m: string, expenses: number, complete = true) => ({
  month: m, income: 3000, expenses, net_cashflow: 3000 - expenses, complete,
});

const budget = (extra: Record<string, unknown> = {}) => ({
  synced: true, read_at: "2026-10-22T18:00:00Z", stale: false, budget: THIS, reason: null,
  savings_eur: 40000, cushion_months: 8.3,
  months: [month("2026-05", 2400), month("2026-06", 2400), month("2026-07", 2400), month("2026-08", 2400),
    month("2026-09", 2400), month("2026-10", 1413.15, false)],
  last_month: LAST, recurring: { monthly_equivalent: 210, count: 6 }, ...extra,
});

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Budget app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const cards = () => Array.from(document.querySelectorAll(".tp-card")) as HTMLElement[];
const ready = () => screen.findByTestId("plan-rows");
const planRow = (name: string) => within(screen.getByTestId("plan-rows")).getByText(name).closest(".tp-barrow") as HTMLElement;

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  getBudget.mockResolvedValue(budget());
  putTarget.mockResolvedValue({});
  deleteTarget.mockResolvedValue({});
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  getBudget.mockReset();
  putTarget.mockReset();
  deleteTarget.mockReset();
});

describe("Budget page", () => {
  it("is wired into the Portfolio section list", () => {
    expect(section.label).toBe("Budget");
    expect(section.icon).toBe("plan");
    expect(pageFor(app, section)).toBe(Budget);
  });

  it("has the mockup's sections: needs you, summary, the plan, last month, twelve months", async () => {
    mount();
    await ready();
    for (const h of ["Needs you", "Summary", "This month against the plan", "Where last month's money went", "Money in and out, 12 months"]) {
      expect(screen.getByRole("heading", { name: h })).toBeInTheDocument();
    }
    expect(cards().map((c) => c.querySelector(".tp-name")!.textContent)).toEqual(["Spent this month", "Safe to spend", "Kept", "Safety cushion"]);
  });

  it("says not synced yet and shows no euro figure before the first sync", async () => {
    getBudget.mockResolvedValue({
      synced: false, read_at: null, stale: null, budget: null, reason: null, savings_eur: null,
      cushion_months: null, months: [], last_month: null, recurring: null,
    });
    mount();
    expect(await screen.findAllByText("Not synced yet")).toHaveLength(4);
    expect(document.body.textContent).not.toContain("€");
  });

  it("says why there is no budget for the month", async () => {
    getBudget.mockResolvedValue(budget({ budget: null, recurring: null, reason: "The newest Finary snapshot has no budget for this month." }));
    mount();
    expect((await screen.findAllByText("The newest Finary snapshot has no budget for this month.")).length).toBeGreaterThan(0);
  });

  it("(b1) a category over its plan shows the state word in red and the over count", async () => {
    mount();
    await ready();
    expect(within(planRow("Food and Beverage")).getByText("Over the plan").className).toContain("bad");
    expect(screen.getByText("1 over the plan")).toBeInTheDocument();
  });

  it("(b2) a category with no target shows no plan and no pace mark", async () => {
    mount();
    await ready();
    const leisure = planRow("Leisure");
    expect(leisure.textContent).toContain("average €120");
    expect(leisure.textContent).toContain("No plan");
    expect(leisure.querySelector("u")).toBeNull();
    expect(planRow("Food and Beverage").querySelector("u")).not.toBeNull();
  });

  it("(b3) Edit plan saves, uses the average and clears", async () => {
    mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Edit plan" }));
    fireEvent.change(screen.getByLabelText("Plan for Transport"), { target: { value: "300" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Transport" }));
    await waitFor(() => expect(putTarget).toHaveBeenCalledWith("transport", 300));
    fireEvent.click(screen.getByRole("button", { name: "Use Leisure" }));
    expect((screen.getByLabelText("Plan for Leisure") as HTMLInputElement).value).toBe("120");
    fireEvent.click(screen.getByRole("button", { name: "Save Leisure" }));
    await waitFor(() => expect(putTarget).toHaveBeenCalledWith("leisure", 120));
    fireEvent.click(screen.getByRole("button", { name: "Clear Transport" }));
    await waitFor(() => expect(deleteTarget).toHaveBeenCalledWith("transport"));
    expect(screen.queryByRole("button", { name: "Clear Leisure" })).toBeNull();
  });

  it("(b3) a refusal is shown in the server's own words", async () => {
    const { ApiError } = await import("../../lib/api");
    putTarget.mockRejectedValue(new ApiError("422 Unprocessable", 422, { detail: "A target must be a number, zero or more." }));
    mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Edit plan" }));
    fireEvent.change(screen.getByLabelText("Plan for Transport"), { target: { value: "300" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Transport" }));
    expect(await screen.findByText("A target must be a number, zero or more.")).toBeInTheDocument();
  });

  it("(b4) last month shows what came in, where it went and what was kept", async () => {
    mount();
    await ready();
    const last = screen.getByRole("heading", { name: "Where last month's money went" }).closest(".nl-blk") as HTMLElement;
    expect(last.textContent).toContain(`Came in: €3${TS}000`);
    expect(last.textContent).toContain(`Went out: €2${TS}400, kept €600`);
    expect(last.textContent).toContain("Rent");
    expect(last.querySelectorAll(".tp-kbar")).toHaveLength(2);
  });

  it("(b4) income below spending says so and draws no Kept part", async () => {
    const last = {
      ...LAST, expenses: 3500, net_cashflow: -500,
      categories: [LAST.categories[0], { ...LAST.categories[1], spent: 2000 }, { ...LAST.categories[2], spent: 900 }, LAST.categories[3]],
    };
    getBudget.mockResolvedValue(budget({ last_month: last }));
    mount();
    await ready();
    const box = screen.getByRole("heading", { name: "Where last month's money went" }).closest(".nl-blk") as HTMLElement;
    expect(box.textContent).toContain("€500 more than came in");
    expect(within(box).queryByText("Kept")).toBeNull();
  });

  it("(b4) income with no income categories says so", async () => {
    const last = { ...LAST, categories: LAST.categories.slice(1) };
    getBudget.mockResolvedValue(budget({ last_month: last }));
    mount();
    await ready();
    expect(screen.getByText("No income categories last month.")).toBeInTheDocument();
  });

  it("(b7) spent, safe to spend, kept and cushion cards", async () => {
    mount();
    await ready();
    expect(cards()[0].textContent).toContain(`€1${TS}413`);
    expect(cards()[0].textContent).toContain("of €520 planned");
    expect(cards()[0].textContent).toContain("On pace by today");
    expect(cards()[0].textContent).toContain("€369");
    expect(cards()[2].textContent).toContain("20%");
    expect(cards()[3].textContent).toContain("8.3");
  });

  it("(b7) the cushion is a dash with its reason when null", async () => {
    getBudget.mockResolvedValue(budget({ cushion_months: null, months: [month("2026-10", 1413.15, false)] }));
    mount();
    await ready();
    const v = cards()[3].querySelector(".tp-fig span")!;
    expect(v.textContent).toBe("-");
    expect(v.getAttribute("title")).toMatch(/No complete month of spending yet/);
    expect(cards()[2].querySelector(".tp-fig span")!.textContent).toBe("-");
  });

  it("(b7) spent this month with no target says No plan set yet", async () => {
    const noTargets = { ...THIS, categories: THIS.categories.map((c) => ({ ...c, target: null, pace: null, ahead: null, warn: false })) };
    getBudget.mockResolvedValue(budget({ budget: noTargets }));
    mount();
    await screen.findByText("No plan set yet");
    expect(cards()[0].textContent).toContain(`€1${TS}413`);
    expect(screen.queryByText("1 over the plan")).toBeNull();
  });

  it("(b5) a category with no label is Unnamed category, the slug only on hover", async () => {
    mount();
    await ready();
    expect(screen.getByText("Unnamed category")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("misc_unlabelled");
  });

  it("(b6) the visible text has no snake_case", async () => {
    mount();
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "Edit plan" }));
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("title").forEach((t) => t.remove());
    expect(clone.textContent).not.toMatch(/[a-z]+_[a-z]+/);
  });

  it("a hover gives the read time in Paris time", async () => {
    mount();
    await ready();
    expect(screen.getAllByTitle(/Read from Finary 22 Oct, 20:00/).length).toBeGreaterThanOrEqual(3);
  });
});
