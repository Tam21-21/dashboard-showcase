import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Generator from "../pages/research/generator/Generator";
import { APPS, pageFor } from "../shell/sections";
import { LIVE_EVENTS } from "../shell/live";

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "generator")!;
const EM_DASH = String.fromCharCode(8212);

type Call = { method: string; url: string; body: unknown };
type Answer = { status?: number; json: unknown };
let calls: Call[] = [];
let answers: Record<string, Answer | (() => Answer)> = {};

const SAVED_AT = 1761782400; // 30/10/2025
const TPL = (name: string, display_name: string, sha256: string, theory_count: number) => ({ name, display_name, sha256, saved_at: SAVED_AT, theory_count });
const TEMPLATES = [
  TPL("avg_cross", "Average crossover", "sha-a", 2),
  TPL("chan_break", "Channel breakout", "sha-b", 1),
  TPL("pullback", "Pullback in a trend", "sha-c", 2),
  TPL("rebound", "Rebound after a crash", "sha-d", 4),
];
const batch = (id: number, sha: string) => ({
  id, template_sha256: sha, universe_id: 3, searched_universe_id: 4, group_label: "GROUP_LARGE_STOCKS", display_name: "Large stocks", members: ["AAA", "BBB"], kept_back: ["BBB"],
  seed: 7, data_start: "2004-01-01", screen_start: "2004-01-01", screen_end: "2006-12-31", optimise_start: "2007-01-01", optimise_end: "2012-12-31",
  later_start: "2013-01-01", later_end: "2020-12-31", calendar_id: `cal-${id}`, approved_at: 1761782400, control: { state: "running", since: null },
  synthetic: false, walkforward: ["2013-01-01", "2016-12-31"],
});
const theory = (id: number, runs: Array<[string, string]>, saved: unknown[] = []) => ({
  id, name: `theory_${id}`, display_name: `Theory ${id}`, choices: {}, runs: runs.map(([stage, status], i) => ({ stage, run_id: id * 10 + i, status })),
  search: null, final: "not run", saved, stop: null,
});
const tree = (b: ReturnType<typeof batch>, theories: unknown[]) => ({
  batch: b, template: { name: "rebound", display_name: "Rebound after a crash", sha256: b.template_sha256 }, theories,
});
const B1 = batch(1, "sha-d");
const B2 = batch(2, "sha-d");
const TREE1 = tree(B1, [
  theory(1, [["screen", "succeeded"], ["optimise", "succeeded"], ["walkforward", "pending"]]),
  theory(2, [["screen", "running"]]),
]);
const TREE2 = tree(B2, [
  theory(3, [["screen", "succeeded"], ["optimise", "succeeded"], ["walkforward", "succeeded"]], [
    { rank: 1, display_name: "Strategy one", picks: {}, verdict: { word: "pass", id: 1 }, refusal: null },
    { rank: 2, display_name: "Strategy two", picks: {}, verdict: { word: "fail", id: 2 }, refusal: null },
    { rank: 3, display_name: "Strategy three", picks: {}, verdict: null, refusal: "The stocks were too few." },
  ]),
]);
const day = (i: number) => new Date(Date.UTC(2026, 8, 1 + i)).toISOString().slice(0, 10);
const DAYS = Array.from({ length: 35 }, (_, i) => ({ day: day(i), trials: i === 34 ? 1234 : i + 1, searches: 1 }));

function reset(over: Record<string, Answer | (() => Answer)> = {}) {
  calls = [];
  answers = {
    "GET /api/research/generator/worker": { json: { status: "online" } },
    "GET /api/research/generator/v3/templates": { json: TEMPLATES },
    "GET /api/research/generator/v3/batches": { json: [B1, B2] },
    "GET /api/research/generator/v3/batches/1/tree": { json: TREE1 },
    "GET /api/research/generator/v3/batches/2/tree": { json: TREE2 },
    "GET /api/research/generator/v3/trials-per-day": { json: { time_zone: "Europe/Paris", today: day(34), days: DAYS } },
    "GET /api/research/datasets?status=active": { json: [] },
    ...over,
  };
}

function stubFetch() {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });
    const hit = answers[`${method} ${url}`];
    if (!hit) throw new Error(`no answer for ${method} ${url}`);
    const a = typeof hit === "function" ? hit() : hit;
    const status = a.status ?? 200;
    return { ok: status < 400, status, statusText: status === 200 ? "OK" : "Error", json: async () => a.json };
  }));
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Generator app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const openStart = () => fireEvent.click(screen.getAllByRole("button", { name: "Start a theory" })[0]);
const say = () => document.querySelector(".nl-say")?.textContent ?? "";
const SEARCH = (quality: unknown) => ({
  rounds: { rounds: [{ round: 1, valid_strategies: 3638880, ran: "sample of 500", strategies: 500, new_trials: 500, best_trial_ids: [53] }], quality, stop: "not suitable" },
  finalists: [], pick: { picks: null, finalist_rank: 0, optimise_sharpe: 0, reason: "not suitable after round 1" },
});
const NEAR = {
  answer: "no", ids: [], pool: 19,
  closest: { trial_id: 53, sharpe: 1.193, met: 3, failed: ["avg_month", "recovery"], metrics: { closed: 122, win_share: 0.6475, max_drawdown: 0.0118, avg_month: 0.00076, recovery: 5.53 } },
};

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  reset();
  stubFetch();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Generator page, header and templates", () => {
  it("a) the rail lists Generator right after Strategies, with its icon and page", () => {
    const labels = app.sections.map((s) => s.label);
    expect(labels[labels.indexOf("Strategies") + 1]).toBe("Generator");
    expect(section.icon).toBe("gen");
    expect(section.id).toBe("generator");
    expect(pageFor(app, section)).toBe(Generator);
    expect(LIVE_EVENTS).toBeTruthy();
  });

  it("b) the worker state: online green, offline red, a failed read is a dash with the detail", async () => {
    mount();
    const word = await screen.findByText("Worker online");
    expect(word.classList.contains("good")).toBe(true);
    expect(screen.getByTitle("The laptop that runs the searches")).toBeTruthy();
    cleanup();
    reset({ "GET /api/research/generator/worker": { json: { status: "offline" } } });
    mount();
    const off = await screen.findByText("Worker offline");
    expect(off.classList.contains("bad")).toBe(true);
    cleanup();
    reset({ "GET /api/research/generator/worker": { status: 500, json: { detail: "The worker table cannot be read." } } });
    mount();
    await waitFor(() => expect(screen.getByTitle("The worker table cannot be read.").textContent).toBe("-"));
  });

  it("c) the finding: searches, theories, saved, running and the newest trials", async () => {
    mount();
    await waitFor(() => expect(say()).toContain("2 searches, 3 theories, 3 kept."));
    await waitFor(() => expect(say()).toContain("1,234 settings were tried on 5 Oct"));
    expect(say()).not.toContain("none since");
  });

  it("c2) how close each theory came: the nearest strategy's numbers, passed or short by the server's word", async () => {
    reset({
      "GET /api/research/generator/v3/batches/2/tree": { json: tree(B2, [
        { ...theory(3, [["screen", "succeeded"]]), search: SEARCH(NEAR) },
        { ...theory(4, [["screen", "succeeded"]]), search: SEARCH({ answer: 7 }) },
      ]) },
    });
    mount();
    const box = await screen.findByTestId("closeness");
    const row = await waitFor(() => box.querySelector('[data-theory="3"]') as HTMLElement);
    const cell = (k: string) => row.querySelector(`[data-k="${k}"]`) as HTMLElement;
    expect(cell("closed").className).toContain("gx-good");
    expect(cell("closed").textContent).toContain("122");
    expect(cell("win_share").className).toContain("gx-good");
    expect(cell("avg_month").className).toContain("gx-bad");
    expect(cell("avg_month").getAttribute("title")).toBe("Average month: 0.08%. Short of its target");
    expect(cell("recovery").className).toContain("gx-bad");
    expect(row.querySelector("td")!.getAttribute("title")).toBe("Nearest of the best 19, Sharpe 1.19");
    expect(row.textContent).toContain("3 of 5");
    expect(within(box.querySelector('[data-theory="4"]') as HTMLElement).getByTitle("The server sent the search rounds in a shape this page does not know").textContent).toBe("-");
    expect(within(box.querySelector('[data-theory="1"]') as HTMLElement).getByTitle("No search rounds yet: the search has not finished").textContent).toBe("-");
    expect(say()).toContain("1 theory stopped after its first round");
  });

  it("c3) a search that stopped after two rounds is not said to have stopped after its first", async () => {
    const two = SEARCH(NEAR);
    two.rounds.rounds.push({ ...two.rounds.rounds[0], round: 2 });
    reset({
      "GET /api/research/generator/v3/batches/2/tree": { json: tree(B2, [{ ...theory(3, [["screen", "succeeded"]]), search: two }]) },
    });
    mount();
    await waitFor(() => expect(say()).toContain("1 theory stopped: its best attempt missed at least one target."));
    expect(say()).not.toContain("first round");
  });

  it("d) trials per day: one bar per day for the last 30 days, each with its count on hover", async () => {
    mount();
    const box = await screen.findByTestId("trial-days");
    expect(box.querySelectorAll("[data-bar]").length).toBe(30);
    const titles = Array.from(box.querySelectorAll("title")).map((t) => t.textContent);
    expect(titles).toContain("05/10/2026: 1,234 trials, 1 searches");
    cleanup();
    reset({ "GET /api/research/generator/v3/trials-per-day": { json: { time_zone: "Europe/Paris", today: "2026-10-05", days: [] } } });
    mount();
    await waitFor(() => expect(screen.getByTitle("No search has run yet").textContent).toBe("-"));
  });

  it("e) TEMPLATES: one row per template", async () => {
    reset({ "GET /api/research/generator/v3/batches": { json: [B1] } });
    mount();
    openStart();
    const row = (await screen.findByText("Rebound after a crash", { selector: "td" })).closest("tr") as HTMLElement;
    const cells = Array.from(row.querySelectorAll("td")).map((c) => c.textContent);
    expect(cells).toEqual(["Rebound after a crash", "-", "4", "30/10/2025", "USE"]);
    const other = screen.getByText("Average crossover", { selector: "td" }).closest("tr") as HTMLElement;
    expect(Array.from(other.querySelectorAll("td")).map((c) => c.textContent)).toEqual(["Average crossover", "-", "0", "30/10/2025", "USE"]);
    expect(screen.getAllByTitle("The template list does not send its letters yet; the preview shows them")).toHaveLength(4);
  });

  it("f) ADD TEMPLATE: save once, close and reload; a repeat and a refusal show words", async () => {
    mount();
    const loads = () => calls.filter((c) => c.method === "GET" && c.url.endsWith("/v3/templates")).length;
    await waitFor(() => expect(loads()).toBe(1));

    fireEvent.click(screen.getByRole("button", { name: "Add a template" }));
    const dialog = screen.getByRole("dialog", { name: "Add a template" });
    const save = within(dialog).getByRole("button", { name: "SAVE" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    const box = within(dialog).getByLabelText("Template text") as HTMLTextAreaElement;
    expect(box.rows).toBe(18);

    answers["POST /api/research/generator/v3/templates"] = { json: { name: "rebound", display_name: "Rebound after a crash", sha256: "sha-d", created: false } };
    fireEvent.change(box, { target: { value: "the text" } });
    fireEvent.click(save);
    await within(dialog).findByText("This text is already saved as Rebound after a crash");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(calls.filter((c) => c.method === "POST")[0].body).toEqual({ content: "the text" });

    answers["POST /api/research/generator/v3/templates"] = { status: 422, json: { detail: "The template is not valid: a slot is missing (in the entry)." } };
    fireEvent.click(within(dialog).getByRole("button", { name: "SAVE" }));
    const text = await within(dialog).findByText("The template is not valid: a slot is missing (in the entry).");
    expect(text).toBeTruthy();
    expect(dialog.textContent).not.toMatch(/422|\/api\//);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(2);

    answers["POST /api/research/generator/v3/templates"] = { json: { name: "rebound", display_name: "Rebound after a crash", sha256: "sha-e", created: true } };
    fireEvent.click(within(dialog).getByRole("button", { name: "SAVE" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(loads()).toBe(2));
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(3);
  });

  it("g) an empty store: the empty words, ADD TEMPLATE, no finding and no closeness", async () => {
    reset({
      "GET /api/research/generator/v3/templates": { json: [] },
      "GET /api/research/generator/v3/batches": { json: [] },
    });
    mount();
    expect(await screen.findByText("No searches yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a template" })).toBeInTheDocument();
    openStart();
    await screen.findByText("No template saved yet.");
    expect(screen.getAllByRole("button", { name: "ADD TEMPLATE" }).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("closeness")).toBeNull();
  });

  it("i) the mockup's sections in its order", async () => {
    mount();
    await screen.findByTestId("closeness");
    await waitFor(() => expect(screen.getByTestId("search-tiles")).toBeInTheDocument());
    await screen.findByTestId("years-used");
    const heads = Array.from(document.querySelectorAll(".nl-blk-t h2")).map((h) => h.textContent);
    expect(heads).toEqual(["Needs you", "Searches", "Theory results", "Settings tried per day", "Years used by the searches"]);
  });

  it("h) no em dash and no snake_case word on screen", async () => {
    mount();
    await screen.findByTestId("closeness");
    await waitFor(() => expect(say()).toContain("3 kept"));
    openStart();
    await screen.findByText("Rebound after a crash", { selector: "td" });
    const text = document.body.textContent ?? "";
    expect(text).not.toContain(EM_DASH);
    expect(text).not.toMatch(/[a-z]+_[a-z]+/);
  });
});
