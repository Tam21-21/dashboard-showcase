import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Judge from "../pages/research/Judge";
import { APPS } from "../shell/sections";

const fetchVerdicts = vi.fn();
const approve = vi.fn();
const fetchNames = vi.fn();
const fetchThresholds = vi.fn();
const fetchOverview = vi.fn();
const send = vi.fn();

vi.mock("../data/judgeApi", () => ({
  fetchJudgeVerdicts: (...a: unknown[]) => fetchVerdicts(...a),
  approveVerdictForPaper: (...a: unknown[]) => approve(...a),
  fetchJudgeThresholds: () => fetchThresholds(),
}));
vi.mock("../data/strategiesApi", async (orig) => {
  const real = await orig<typeof import("../data/strategiesApi")>();
  return { ...real, fetchStrategiesOverview: () => fetchOverview() };
});
vi.mock("../data/runsApi", async (orig) => {
  const real = await orig<typeof import("../data/runsApi")>();
  return { ...real, sendToJudge: (id: number) => send(id) };
});
vi.mock("../data/strategyNames", async (orig) => {
  const real = await orig<typeof import("../data/strategyNames")>();
  const { useQuery } = await import("@tanstack/react-query");
  return { ...real, useStrategyNames: () => useQuery({ queryKey: real.strategyNamesKey, queryFn: () => fetchNames() }) };
});
import { ApiError } from "../../lib/api";

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "judge")!;

const EM_DASH = String.fromCharCode(8212);
const NAMES = ["Evidence floor", "Walk forward", "Trial count", "Plateau", "Overfitting test", "Monte Carlo bounds", "Permutation test", "Windfall", "Beats buy and hold"];

type Spec = { value: number | null; bar: number; comparison: string; units: string };
const DEFAULT: Spec = { value: 0.9, bar: 0.5, comparison: "at_or_above", units: "ratio" };

function measures(outcomes: string[], over: Record<number, Spec> = {}) {
  return outcomes.map((outcome, i) => {
    const s = over[i + 1] ?? DEFAULT;
    return { check: i + 1, outcome, why: null, measures: [{ key: "k", label: "the figure", ...s }] };
  });
}
function row(id: number, verdict: string, outcomes: string[], extra: Record<string, unknown> = {}, over: Record<number, Spec> = {}) {
  return {
    id, run_id: id, judging_pass_id: "p", variation_id: 1, ticker: "AAA", graded_formula_version: "v", strategy_id: id,
    strategy_version: 1, dataset_sha256: "x", verdict, is_advisory: false, battery_version: "b", evidence_floor_outcome: "x",
    check_outcomes: outcomes.map((o, i) => ({ check: i + 1, outcome: o })), decided_by: "judge", warnings: [],
    graded_at: 1000 + id, current: true, strategy_name: `Strategy ${id}`, trials: 1, total_return: null, max_drawdown: null,
    num_trades: null, sharpe_ratio: null, rejected: false, rejected_at: null,
    measures: measures(outcomes, over), judged_capital: 25000, ...extra,
  };
}
const ALL = Array(9).fill("cleared");
const with7Failed = ALL.map((o, i) => (i === 6 ? "failed" : o));
const with1NotEval = ALL.map((o, i) => (i === 0 ? "not-evaluable" : o));
const ROWS = [
  row(10, "pass", ALL),
  row(11, "fail", with7Failed, {}, { 7: { value: 0.21, bar: 0.05, comparison: "strictly_below", units: "ratio" } }),
  row(12, "unjudgeable", with1NotEval, {}, { 1: { value: null, bar: 50, comparison: "at_or_above", units: "percent" } }),
  row(13, "pass", ALL, { current: false }),
  row(14, "pass", ALL, { rejected: true, rejected_at: "2026-10-01T00:00:00Z" }),
];

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Judge app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 5 }, rows: ROWS });
  fetchNames.mockResolvedValue([]);
  fetchThresholds.mockResolvedValue({ battery_version: "b", checks: { "1": { bars: [] }, "2": { bars: [{ quantity_key: "wfe", value_text: "walk-forward efficiency at least 50 percent", source_book: "Pardo, The Evaluation and Optimization of Trading Strategies", source_folio: "239", source_quote: "robust strategies above 50 or 60 percent" }] }, "3": { bars: [{ quantity_key: "dsr", value_text: "The deflated Sharpe ratio above 0.95", source_book: "Advances in Financial Machine Learning", source_folio: "none printed in this copy", source_quote: "q" }] } } });
  fetchOverview.mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  fetchVerdicts.mockReset();
  approve.mockReset();
  fetchNames.mockReset();
  fetchThresholds.mockReset();
  fetchOverview.mockReset();
  send.mockReset();
});

const candidates = () => screen.getByTestId("candidates");
const sheet = () => screen.getByTestId("sheet");
const pick = (name: string) => fireEvent.click(within(candidates()).getByText(name));
const checkRow = (n: number) => sheet().querySelector(`[data-check="${n}"]`) as HTMLElement;
async function open() {
  mount();
  await screen.findByTestId("sheet");
}

describe("Judge page", () => {
  it("(a) lists the current, not rejected verdicts with their words", async () => {
    await open();
    expect(fetchVerdicts).toHaveBeenCalledWith(500, 0);
    const items = Array.from(candidates().querySelectorAll("[data-verdict]")).map((e) => e.getAttribute("data-verdict"));
    expect(items).toEqual(["12", "11", "10"]);
    const t = candidates().textContent!;
    for (const w of ["9 of 9", "8 of 9", "graded"]) expect(t).toContain(w);
  });

  it("(b) the dial draws every check for every graded run, and each check's tally is on hover", async () => {
    await open();
    const dial = screen.getByTestId("dial");
    expect(dial.querySelectorAll("svg.jd-dial-svg path")).toHaveLength(27);
    expect(dial.querySelectorAll("svg.jd-dial-svg path.o-failed")).toHaveLength(1);
    expect(dial.querySelectorAll("svg.jd-dial-svg path.o-other")).toHaveLength(1);
    const ck = (name: string) => within(dial).getByText(name).closest("button")!.getAttribute("title")!;
    expect(ck("Permutation test")).toContain("Cleared 2, failed 1, could not run 0");
    expect(ck("Evidence floor")).toContain("Cleared 2, failed 0, could not run 1");
    expect(dial.querySelector(".jd-mid")!.textContent).toBe("1of 3 passed");
  });

  it("(c) a failed verdict shows its figure, bar and comparison", async () => {
    await open();
    pick("Strategy 11");
    const r = checkRow(7);
    expect(r.textContent).toContain("0.21");
    expect(r.textContent).toContain("It needs below 0.05.");
    expect(r.textContent).toContain("Failed");
    expect(sheet().textContent).toContain("Failed at least one check, so it stops here.");
  });

  it("(d) a check that could not be computed shows dashes and Could not run", async () => {
    await open();
    pick("Strategy 12");
    const r = checkRow(1);
    expect(r.textContent).toContain("Could not run");
    expect(r.querySelector("[data-fill]")).toBeNull();
    expect(sheet().textContent).toContain("Some checks could not be computed");
  });

  it("(e) Approve for paper works on the passed verdict only and sends its capital", async () => {
    approve.mockResolvedValue({ paper_run_id: 1, verdict_id: 10 });
    await open();
    pick("Strategy 11");
    expect(screen.getByRole("button", { name: "Approve for paper" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Approve for paper" }).closest("[title]")!.getAttribute("title")).toBe("Only a passed, current verdict can go to paper");
    pick("Strategy 12");
    expect(screen.getByRole("button", { name: "Approve for paper" })).toBeDisabled();
    pick("Strategy 10");
    fireEvent.click(screen.getByRole("button", { name: "Approve for paper" }));
    await screen.findByText("Sent to paper");
    expect(approve).toHaveBeenCalledWith(10, 25000);
  });

  it("(f) a refusal shows its sentence and never its code", async () => {
    approve.mockRejectedValue(
      new ApiError("409 /x", 409, { detail: "retest-not-passed: the newest retest of this run is not passed" }),
    );
    await open();
    pick("Strategy 10");
    fireEvent.click(screen.getByRole("button", { name: "Approve for paper" }));
    await screen.findByText("The newest retest of this run is not passed");
    expect(document.body.textContent).not.toContain("retest-not-passed");
  });

  it("(g) an empty list says so", async () => {
    fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 0 }, rows: [] });
    mount();
    await screen.findByText("No verdicts yet");
  });

  it("(h) a failed fetch shows the error state", async () => {
    fetchVerdicts.mockRejectedValue(new ApiError("500 /x", 500, { detail: "The judge store is unreadable" }));
    mount();
    await screen.findByText("The judge store is unreadable");
  });

  it("(j) a snake_case strategy name shows in plain form, the stored name on hover", async () => {
    fetchVerdicts.mockResolvedValue({
      store: { table_present: true, row_count: 1 },
      rows: [row(20, "pass", ALL, { strategy_name: "strategy_runs_ma_cross" })],
    });
    fetchNames.mockResolvedValue([{ id: 20, name: "strategy_runs_ma_cross", display_name: "Moving average cross", status: "active" }]);
    await open();
    await waitFor(() => expect(within(candidates()).getByText("Moving average cross").getAttribute("title")).toBe("strategy_runs_ma_cross"));
    expect(within(sheet()).getByText("Moving average cross").getAttribute("title")).toBe("strategy_runs_ma_cross");
    expect(document.body.textContent).not.toMatch(/[a-z]+_[a-z]+/);
  });

  it("(k) a check with two measures is one row, the second measure in the hover", async () => {
    const two = row(21, "pass", ALL);
    two.measures[0].measures = [
      { key: "a", label: "degrees of freedom left", value: 80, bar: 50, comparison: "at_or_above", units: "percent" },
      { key: "b", label: "fat tails (kappa)", value: 0.4, bar: 0.5, comparison: "at_or_below", units: "ratio" },
    ];
    fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 1 }, rows: [two] });
    await open();
    expect(checkRow(1).querySelectorAll(".vm").length).toBe(0);
    expect(checkRow(1).textContent).toContain("80.0%");
    expect(checkRow(1).textContent).not.toContain("kappa");
    expect(checkRow(1).getAttribute("title")).toContain("fat tails (kappa)");
    expect(checkRow(1).getAttribute("title")).toContain("0.40");
  });

  it("(i) no snake_case and no em dash on the page", async () => {
    await open();
    for (const name of ["Strategy 10", "Strategy 11", "Strategy 12"]) {
      pick(name);
      const text = document.body.textContent!;
      expect(text).not.toMatch(/[a-z]+_[a-z]+/);
      expect(text).not.toContain(EM_DASH);
    }
    for (const n of NAMES) expect(sheet().textContent).toContain(n);
    await waitFor(() => expect(document.querySelectorAll("[data-check]").length).toBe(9));
  });
});

type M = { key: string; label: string; value: number | null; bar: number | null; comparison: string | null; units: string | null };
const m = (key: string, label: string, value: number | null, bar: number | null, comparison: string | null, units: string | null): M => ({ key, label, value, bar, comparison, units });
function one(n: number, ms: M[], extra: Record<string, unknown> = {}, outcome: string | null = null, why: string | null = null) {
  const r = row(40 + n, "fail", ALL.map((o, i) => (i === n - 1 ? (outcome ?? "failed") : o)), extra);
  r.measures[n - 1] = { check: n, outcome, why, measures: ms } as never;
  if (outcome === null) r.check_outcomes = r.check_outcomes.filter((c) => c.check !== n);
  return r;
}
async function openWith(r: unknown) {
  fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 1 }, rows: [r] });
  await open();
}
const bar = (n: number) => checkRow(n).querySelector(".jd-fig span") as HTMLElement;

describe("Judge page rows (UI35)", () => {
  it("u1 a check with two measures shows the failing one, the other in the hover", async () => {
    const r = one(1, [
      m("rdf", "degrees of freedom left", 80, 50, "at_or_above", "percent"),
      m("kappa", "fat tails (kappa)", 0.73, 0.5, "at_or_below", "ratio"),
    ]);
    await openWith(r);
    const t = checkRow(1).textContent!;
    expect(t).toContain("0.73");
    expect(t).toContain("It needs at most 0.50.");
    expect(t).not.toContain("80.0%");
    expect(checkRow(1).querySelectorAll(".vm").length).toBe(0);
    expect(checkRow(1).getAttribute("title")).toContain("80.0%");
  });

  it("u2 a null value is not the failing measure", async () => {
    const r = one(1, [
      m("a", "first", null, 50, "at_or_above", "percent"),
      m("b", "second", 0.2, 0.5, "at_or_above", "ratio"),
    ]);
    await openWith(r);
    expect(checkRow(1).textContent).toContain("0.20");
    expect(checkRow(1).querySelectorAll(".vm").length).toBe(0);
    const q = one(1, [m("a", "first", 0.2, 0.5, "at_or_above", "ratio"), m("b", "second", null, 0.5, "at_or_above", "ratio")]);
    cleanup();
    await openWith(q);
    expect(checkRow(1).textContent).toContain("0.20");
    expect(checkRow(1).querySelectorAll(".vm").length).toBe(0);
  });

  it("u3 each row says what the check needs in words, and the target says when it passes on hover", async () => {
    const r = row(30, "pass", ALL, {}, {
      2: { value: 0.9, bar: 0.15, comparison: "at_or_above", units: "ratio" },
      3: { value: 0.9, bar: 0.15, comparison: "strictly_above", units: "ratio" },
      4: { value: 0.1, bar: 0.15, comparison: "at_or_below", units: "ratio" },
      5: { value: 0.1, bar: 0.15, comparison: "strictly_below", units: "ratio" },
    });
    await openWith(r);
    expect(bar(2).textContent).toContain("It needs at least 0.15.");
    expect(bar(3).textContent).toContain("It needs above 0.15.");
    expect(bar(2).getAttribute("title")).toBe("passes at 0.15 or more");
    expect(bar(3).getAttribute("title")).toBe("passes above 0.15");
    expect(bar(4).getAttribute("title")).toBe("passes at 0.15 or less");
    expect(bar(5).getAttribute("title")).toBe("passes below 0.15");
  });

  it("u4 a bar of 0 prints through its unit's formatter (bar 0.00), and a small bar prints a plain decimal", async () => {
    const r = row(30, "pass", ALL, {}, {
      2: { value: 0.9, bar: 0, comparison: "at_or_above", units: "ratio" },
      3: { value: 0.9, bar: 0.0042, comparison: "at_or_above", units: "ratio" },
    });
    await openWith(r);
    expect(bar(2).textContent).toContain("It needs at least 0.00.");
    expect(bar(3).textContent).toContain("It needs at least 0.0042.");
  });

  it("u5 check 8 prints dollars, whole", async () => {
    const r = row(30, "pass", ALL, {}, { 8: { value: 324.2505, bar: 100, comparison: "at_or_below", units: "ratio" } });
    await openWith(r);
    expect(checkRow(8).textContent).toContain("$324");
    cleanup();
    await openWith(row(31, "pass", ALL, {}, { 8: { value: -12.4, bar: 100, comparison: "at_or_below", units: "ratio" } }));
    expect(checkRow(8).textContent).toContain("-$12");
  });

  it("u6 a figure with a target is written out in words with no track", async () => {
    const r = row(30, "pass", ALL, {}, {
      3: { value: 130, bar: 50, comparison: "at_or_above", units: "percent" },
      8: { value: 324, bar: 100, comparison: "at_or_below", units: "none" },
    });
    await openWith(r);
    expect(checkRow(3).querySelector(".jd-track")).toBeNull();
    expect(checkRow(3).textContent).toContain("130.0%");
    expect(checkRow(3).textContent).toContain("It needs at least 50.0%.");
    expect(checkRow(8).textContent).toContain("$324");
  });

  it("u7 a check that cannot be computed says why, in the judge's own words", async () => {
    const r = one(3, [m("a", "the figure", null, 0.5, "at_or_above", "ratio")], {}, "not-evaluable", "the trial count was not stored");
    await openWith(r);
    expect(within(checkRow(3)).getByText("the trial count was not stored")).toBeInTheDocument();
    expect(checkRow(3).textContent).toContain("Could not run");
  });

  it("u8 a check the judge did not run says Not run yet, with no hover, and Not run yet as its state", async () => {
    const r = one(4, [], {}, null, null);
    await openWith(r);
    const el = within(checkRow(4)).getByText("Not run yet", { selector: ".jd-lo" });
    expect(el.getAttribute("title")).toBeNull();
    expect(within(checkRow(4)).getByText("Not run yet", { selector: ".nl-st" }).closest("[title]")!.getAttribute("title")).toBe("Not run yet");
  });

  it("u14 a zero prints through its unit's formatter: 0.0% for a percent, $0 for check 8, 0.00 for a ratio", async () => {
    const r = row(31, "pass", ALL, {}, {
      1: { value: 0, bar: 50, comparison: "at_or_above", units: "percent" },
      2: { value: 0, bar: 0.5, comparison: "at_or_above", units: "ratio" },
      8: { value: 0, bar: 100, comparison: "at_or_below", units: "dollars" },
    });
    await openWith(r);
    const v = (n: number) => checkRow(n).querySelectorAll(".num")[1].textContent;
    expect(v(1)).toBe("0.0%");
    expect(v(2)).toBe("0.00");
    expect(v(8)).toBe("$0");
  });

  it("u9 an old verdict with no figures keeps its state word and says why there is no figure", async () => {
    const r = one(2, [], {}, "failed", "this verdict was graded before figures were kept");
    await openWith(r);
    expect(checkRow(2).textContent).toContain("Failed");
    expect(within(checkRow(2)).getByText("this verdict was graded before figures were kept")).toBeInTheDocument();
  });

  it("u10 (guard) a snake_case name shows in plain form in the list and the sheet, no underscore in the rows", async () => {
    fetchNames.mockResolvedValue([{ id: 20, name: "strategy_runs_ma_cross", display_name: "Moving average cross", status: "active" }]);
    await openWith(row(20, "pass", ALL, { strategy_name: "strategy_runs_ma_cross" }));
    await waitFor(() => expect(within(candidates()).getByText("Moving average cross").getAttribute("title")).toBe("strategy_runs_ma_cross"));
    expect(within(sheet()).getByText("Moving average cross").getAttribute("title")).toBe("strategy_runs_ma_cross");
    expect(sheet().querySelector(".vrow")!.textContent).not.toContain("_");
  });

  it("u11 the targets show each check's target with its book and page, the quote on hover; check 1 its stored bars", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Targets" }));
    const bars = await screen.findByTestId("bars");
    const t = within(bars).getByText("Walk-forward efficiency at least 50 percent");
    expect(t.getAttribute("title")).toBe('"robust strategies above 50 or 60 percent"');
    expect(bars.textContent).toContain("Pardo, The Evaluation and Optimization of Trading Strategies, page 239");
    expect(bars.textContent).toContain("The figure at least 0.50");
    expect(bars.textContent).toContain("No book is stored with this target");
    expect(within(bars).getByText("Advances in Financial Machine Learning")).toBeTruthy();
    expect(bars.textContent).not.toContain("page none");
  });

  it("u12 (guard) a value with no bar prints the value, a dash with its reason, and no track", async () => {
    await openWith(row(30, "pass", ALL, {}, { 2: { value: 0.4, bar: null, comparison: null, units: null } as never }));
    const r = checkRow(2);
    expect(r.textContent).toContain("0.40");
    expect(r.querySelector("[title='No target is stored for this run']")).not.toBeNull();
    expect(r.querySelector(".tick")).toBeNull();
    expect(r.textContent).not.toContain("It needs");
  });

  it("u13 a measure with no bar goes in the hover when another measure fails", async () => {
    const r = one(1, [
      m("rdf", "degrees of freedom left", 0.4, null, null, null),
      m("kappa", "fat tails (kappa)", 0.73, 0.5, "at_or_below", "ratio"),
    ]);
    await openWith(r);
    expect(checkRow(1).textContent).toContain("0.73");
    expect(checkRow(1).querySelectorAll(".vm").length).toBe(0);
    const t = checkRow(1).getAttribute("title")!;
    expect(t).toContain("0.40");
    expect(t).toContain("target -");
  });
});

describe("Judge page, reads and figures (round six)", () => {
  it("v1 reads every page of verdicts until the store's count is covered", async () => {
    const r = (id: number) => row(id, "pass", ALL);
    fetchVerdicts.mockImplementation((_limit: number, offset: number) => Promise.resolve(offset === 0
      ? { store: { table_present: true, row_count: 3 }, rows: [r(70), r(71)] }
      : { store: { table_present: true, row_count: 3 }, rows: [r(72)] }));
    await open();
    expect(screen.getByTestId("candidates").querySelectorAll("[data-verdict]")).toHaveLength(3);
    expect(fetchVerdicts.mock.calls).toEqual([[500, 0], [500, 2]]);
  });

  it("v2 a figure that differs from its bar never prints the same as the bar", async () => {
    await openWith(row(80, "pass", ALL, {}, { 7: { value: 0.0496, bar: 0.05, comparison: "strictly_below", units: "probability" } }));
    expect(checkRow(7).querySelector(".jd-fig b")!.textContent).toBe("0.0496");
    expect(checkRow(7).textContent).toContain("It needs below 0.0500.");
    expect(checkRow(1).querySelector(".jd-fig b")!.textContent).toBe("0.90");
  });
});

describe("Judge page, waiting and why (round six)", () => {
  const strat = (id: number, runId: number | null) => ({ id, name: `s_${id}`, display_name: `Strategy ${id}`, version: 1, description: null,
    run: runId === null ? null : { id: runId, created_at: 1793490553, strategy_version: 1, equity_sparkline: null, sharpe_ratio: null, num_trades: null, total_return: null, max_drawdown: null } });

  it("w1 lists the strategies whose newest run has no verdict (not those with no run), and sends their runs to the judge", async () => {
    fetchOverview.mockResolvedValue([strat(10, 10), strat(50, 56), strat(51, 57), strat(52, null)]);
    fetchNames.mockResolvedValue([50, 51, 52, 10].map((id) => ({ id, name: `s_${id}`, display_name: `Strategy ${id}`, status: "active" })));
    send.mockImplementation((id: number) => (id === 56 ? Promise.resolve({ verdict_id: 9 }) : Promise.reject(new ApiError("409 /x", 409, { detail: "run-not-succeeded: the run did not finish" }))));
    await open();
    const w = await screen.findByTestId("waiting");
    expect(Array.from(w.querySelectorAll(".jd-wt-n")).map((e) => e.textContent)).toEqual(["Strategy 50", "Strategy 51"]);
    expect(w.textContent).toContain("ran ");
    fireEvent.click(within(w).getByRole("button", { name: "Send 2 runs to the judge" }));
    const status = await within(w).findByRole("status");
    expect(status.textContent).toBe("1 judged, 1 refused");
    expect(status.getAttribute("title")).toBe("Strategy 51: The run did not finish");
    expect(send.mock.calls.map((c) => c[0])).toEqual([56, 57]);
  });

  it("w2 what keeps the checks from running: settings not chosen first, in amber, each reason once with its count", async () => {
    const a = row(60, "unjudgeable", ALL.map((o, i) => (i === 3 || i === 1 ? "not-evaluable" : o)));
    a.measures[3] = { check: 4, outcome: "not-evaluable", why: "How far around the chosen setting to look is not set yet.", measures: [] } as never;
    a.measures[1] = { check: 2, outcome: "not-evaluable", why: "The test on data it was not tuned on has not run yet.", measures: [] } as never;
    const b = row(61, "unjudgeable", ALL.map((o, i) => (i === 3 || i === 1 ? "not-evaluable" : o)));
    b.measures[3] = { check: 4, outcome: "not-evaluable", why: "How far around the chosen setting to look is not set yet.", measures: [] } as never;
    b.measures[1] = { check: 2, outcome: "not-evaluable", why: "The test on data it was not tuned on has not run yet.", measures: [] } as never;
    fetchVerdicts.mockResolvedValue({ store: { table_present: true, row_count: 2 }, rows: [a, b] });
    await open();
    const why = await screen.findByTestId("why");
    const rows = Array.from(why.querySelectorAll(".jd-w"));
    expect(rows.map((r) => r.querySelector(".jd-w-t")!.textContent)).toEqual([
      "How far around the chosen setting to look is not set yet.", "The test on data it was not tuned on has not run yet.",
    ]);
    expect(rows[0].querySelector(".jd-w-bar i")!.className).toBe("warn");
    expect(rows[0].querySelector(".jd-w-n")!.textContent).toBe("2 of 2");
    expect(rows[1].querySelector(".jd-w-bar i")!.className).toBe("soft");
    const plateau = within(screen.getByTestId("dial")).getByText("Plateau").closest("button")!;
    expect(plateau.textContent).toContain("Setting not chosen");
    expect(within(screen.getByTestId("dial")).getByText("Walk forward").closest("button")!.textContent).toContain("Input missing");
    expect(document.querySelector(".nl-say")!.textContent).toContain("No run can pass the judge yet.");
  });
});
