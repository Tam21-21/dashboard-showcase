import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Generator from "../pages/research/generator/Generator";
import { APPS } from "../shell/sections";

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "generator")!;
const EM_DASH = String.fromCharCode(8212);
const V3 = "/api/research/generator/v3";

type Call = { method: string; url: string; body: any };
type Answer = { status?: number; json: unknown };
let calls: Call[] = [];
let answers: Record<string, Answer> = {};

const SAVED_AT = 1761782400; // 30/10/2025
const TPL = (name: string, display_name: string, sha256: string, theory_count: number) => ({ name, display_name, sha256, saved_at: SAVED_AT, theory_count });
const TEMPLATES = [
  TPL("avg_cross", "Average crossover", "sha-a", 2),
  TPL("pullback", "Pullback in a trend", "sha-c", 2),
  TPL("rebound", "Rebound after a crash", "sha-d", 4),
];
const GROUPS = [
  { id: 11, name: "MAG-7", display_name: "MAG-7", needs_name: false, dataset_type: "price_universe", member_count: 7 },
  { id: 12, name: "Large stocks", display_name: "Large stocks", needs_name: false, dataset_type: "price_universe", member_count: 120 },
  { id: 5, name: "AAPL daily", dataset_type: "price", member_count: null },
  { id: 6, name: "Filings list", dataset_type: "event_universe", member_count: 40 },
];
const range = (count: number, from: number, to: number, step: number) => ({ count, from, to, step });
const PREVIEW = {
  template: { name: "rebound", display_name: "Rebound after a crash", sha256: "sha-d" },
  theories: [
    { name: "t1", display_name: "Rebound after a crash, Bollinger lower band, RSI", choices: {}, letters: ["N", "W", "k", "R", "L", "ST", "TG", "H"], values_per_number: 7,
      round_one: { N: range(7, 10, 40, 5), ST: range(19, 0.01, 0.1, 0.005) } },
    { name: "t2", display_name: "Rebound after a crash, Bollinger lower band, MACD", choices: {}, letters: ["N"], values_per_number: 7, round_one: { N: range(7, 10, 40, 5) } },
    { name: "t3", display_name: "Rebound after a crash, Keltner band, RSI", choices: {}, letters: ["N", "W"], values_per_number: null, round_one: null },
    { name: "t4", display_name: "Rebound after a crash, Keltner band, MACD", choices: {}, letters: ["N", "W"], values_per_number: 3, round_one: { N: range(3, 10, 20, 5), W: range(2, 5, 10, 5) } },
  ],
  stocks: { searched: ["AAPL", "AMZN", "GOOG", "META", "TSLA"], kept_back: ["MSFT", "NVDA"] },
  years: {
    screen: ["2013-11-01", "2019-09-01"], optimise: ["2019-09-03", "2020-12-31"],
    walkforward: ["2021-01-01", "2022-12-31"], later: ["2023-01-03", "2025-12-31"],
  },
  run_time_estimate: null,
};
const PREVIEW_URL = `${V3}/templates/sha-d/preview`;
const APPROVE_URL = `${V3}/templates/sha-d/approve`;

function reset(over: Record<string, Answer> = {}) {
  calls = [];
  answers = {
    "GET /api/research/generator/worker": { json: { status: "online" } },
    [`GET ${V3}/templates`]: { json: TEMPLATES },
    [`GET ${V3}/batches`]: { json: [] },
    [`GET ${V3}/trials-per-day`]: { json: { time_zone: "Europe/Paris", today: "2026-10-05", days: [] } },
    "GET /api/research/datasets?status=active": { json: GROUPS },
    [`POST ${PREVIEW_URL}`]: { json: PREVIEW },
    [`POST ${APPROVE_URL}`]: { json: { batch_id: 3, run_ids: [31, 32, 33, 34] } },
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
    const status = hit.status ?? 200;
    return { ok: status < 400, status, statusText: status === 200 ? "OK" : "Error", json: async () => hit.json };
  }));
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Generator app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  // Starting a theory and the templates live in a window the header button opens.
  fireEvent.click(screen.getAllByRole("button", { name: "Start a theory" })[0]);
  return view;
}

const posts = () => calls.filter((c) => c.method === "POST");
const batchLoads = () => calls.filter((c) => c.method === "GET" && c.url === `${V3}/batches`).length;
const press = (name: string, root: HTMLElement) => fireEvent.click(within(root).getByRole("button", { name }));
const pill = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;
const previewButton = () => document.querySelector("[data-preview-go]") as HTMLButtonElement;

async function ready() {
  await screen.findByRole("option", { name: /MAG-7/ });
  await screen.findAllByText("Rebound after a crash", { selector: "td" });
}

// The picker offers TODAY only, so each date is set by moving the clock first (Date only is faked).
function pickDate(label: string, iso: string) {
  vi.setSystemTime(new Date(`${iso}T12:00:00Z`));
  fireEvent.click(screen.getByRole("button", { name: label }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "Choose a date" })).getByRole("button", { name: "TODAY" }));
}

async function fillAndPreview() {
  await ready();
  fireEvent.click(pill("Rebound after a crash"));
  fireEvent.change(screen.getByLabelText("Stock group"), { target: { value: "11" } });
  fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "7" } });
  pickDate("Optimise from", "2019-09-03");
  pickDate("Optimise to", "2020-12-31");
  pickDate("Later from", "2023-01-03");
  pickDate("Later to", "2025-12-31");
  fireEvent.click(previewButton());
  await screen.findByText("146 combinations");
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.useFakeTimers({ toFake: ["Date"] });
  reset();
  stubFetch();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Generator page, start a theory", () => {
  it("a) Needs you: templates no search was made from wait, and the button opens the start window", async () => {
    mount();
    await ready();
    const stop = screen.getByTestId("needs-you");
    expect(stop.textContent).toContain("3 templates have not been started");
    expect(stop.textContent).toContain("Rebound after a crash");
    expect(posts()).toHaveLength(0);
  });

  it("a2) nothing waiting says so when every template has a batch", async () => {
    // Whole rows, as the server sends them: section 2 draws every batch it is given.
    const batchRows = [1, 2, 3].map((id) => ({
      id, template_sha256: ["sha-a", "sha-c", "sha-d"][id - 1], universe_id: 3, searched_universe_id: 4, group_label: `Group ${id}`,
      members: ["AAA", "BBB"], kept_back: ["BBB"], seed: 7, data_start: "2004-01-01", screen_start: "2004-01-01", screen_end: "2006-12-31",
      optimise_start: "2007-01-01", optimise_end: "2012-12-31", later_start: "2017-01-01", later_end: "2020-12-31", calendar_id: `cal-${id}`,
      approved_at: 1761782400, control: { state: "running", since: null }, synthetic: false, walkforward: ["2013-01-01", "2016-12-31"],
    }));
    reset({
      [`GET ${V3}/batches`]: { json: batchRows },
      ...Object.fromEntries([1, 2, 3].map((i) => [`GET ${V3}/batches/${i}/tree`, { status: 500, json: { detail: "x" } }])),
    });
    mount();
    expect(await screen.findByText("Nothing stops the generator.")).toBeInTheDocument();
  });

  it("b) the stock group list holds only stock groups", async () => {
    mount();
    await ready();
    const options = Array.from((screen.getByLabelText("Stock group") as HTMLSelectElement).options).map((o) => o.textContent);
    expect(options).toContain("MAG-7, 7 stocks");
    expect(options).toContain("Large stocks, 120 stocks");
    expect(options.join("|")).not.toMatch(/AAPL daily|Filings list/);
  });

  describe("typed names in the stock group picker", () => {
    const optionsOf = () => Array.from((screen.getByLabelText("Stock group") as HTMLSelectElement).options);
    const withGroups = (extra: unknown[]) =>
      reset({ "GET /api/research/datasets?status=active": { json: [...GROUPS, ...extra] } });
    const PU = { dataset_type: "price_universe" };

    it("s-n1) typed names, NEEDS A NAME, a disabled count of the unnamed, no stored name", async () => {
      withGroups([
        { id: 21, name: "strategy_runs_universe_2015_2020", display_name: "Strategy runs 2015 to 2020", needs_name: false, member_count: 3, ...PU },
        { id: 22, name: "mag_7", display_name: "MAG 7", needs_name: true, member_count: 7, ...PU },
        { id: 23, name: "strategy_runs_universe_2010_2012", display_name: null, needs_name: true, member_count: 5, ...PU },
      ]);
      mount();
      await ready();
      const texts = optionsOf().map((o) => o.textContent!);
      expect(texts).toContain("Strategy runs 2015 to 2020, 3 stocks");
      expect(texts).toContain("MAG 7, 7 stocks, NEEDS A NAME");
      expect(texts.join("|")).not.toContain("_");
      expect(texts.join("|")).not.toContain("2010_2012");
      const last = optionsOf().at(-1)!;
      expect(last.textContent).toBe("Not listed, no name yet: 1. Name them on the Universes tab.");
      expect(last.disabled).toBe(true);
      expect(optionsOf().some((o) => o.hasAttribute("title"))).toBe(false);
    });

    it("s-n2) a null member count reads a dash, never 0", async () => {
      withGroups([{ id: 24, name: "x", display_name: "No count", needs_name: false, member_count: null, ...PU }]);
      mount();
      await ready();
      expect(optionsOf().map((o) => o.textContent)).toContain("No count, - stocks");
    });

    it("s-n3) with no group left out there is no disabled option", async () => {
      mount();
      await ready();
      expect(optionsOf().some((o) => o.disabled)).toBe(false);
    });

    it("s-n4) with every group unnamed the only group option is the disabled one", async () => {
      reset({
        "GET /api/research/datasets?status=active": { json: [
          { id: 31, name: "a_b", display_name: null, needs_name: true, member_count: 1, ...PU },
          { id: 32, name: "c_d", display_name: null, needs_name: true, member_count: 2, ...PU },
        ] },
      });
      mount();
      await screen.findByRole("option", { name: /Not listed/ });
      const texts = optionsOf().map((o) => o.textContent);
      expect(texts).toEqual(["Pick a stock group", "Not listed, no name yet: 2. Name them on the Universes tab."]);
      expect(optionsOf()[1].disabled).toBe(true);
    });
  });

  it("c) PREVIEW is disabled until a template and a stock group are picked, then sends one POST with the exact body", async () => {
    mount();
    await ready();
    expect(previewButton().disabled).toBe(true);
    fireEvent.click(pill("Rebound after a crash"));
    expect(previewButton().disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Stock group"), { target: { value: "11" } });
    expect(previewButton().disabled).toBe(false);
    fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "7" } });
    pickDate("Optimise from", "2019-09-03");
    pickDate("Optimise to", "2020-12-31");
    pickDate("Later from", "2023-01-03");
    pickDate("Later to", "2025-12-31");
    fireEvent.click(previewButton());
    fireEvent.click(previewButton());
    await screen.findByText("146 combinations");
    expect(posts()).toHaveLength(1);
    expect(posts()[0].url).toBe(PREVIEW_URL);
    expect(posts()[0].body).toEqual({
      universe_id: 11, seed: 7, optimise_window: ["2019-09-03", "2020-12-31"], later_window: ["2023-01-03", "2025-12-31"],
    });
  });

  it("c2) empty dates and an empty seed are sent as null; words in the seed are sent as typed", async () => {
    mount();
    await ready();
    fireEvent.click(pill("Rebound after a crash"));
    fireEvent.change(screen.getByLabelText("Stock group"), { target: { value: "12" } });
    fireEvent.click(previewButton());
    await screen.findByText("146 combinations");
    expect(posts()[0].body).toEqual({ universe_id: 12, seed: null, optimise_window: [null, null], later_window: [null, null] });
    fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "abc" } });
    fireEvent.click(previewButton());
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body.seed).toBe("abc");
  });

  it("d) the preview card: combinations, facts, share bar, four stretches and the letters table", async () => {
    mount();
    await fillAndPreview();
    const facts = (k: string) => screen.getByText(k, { selector: "span" }).parentElement!.querySelector("b") as HTMLElement;
    expect(facts("THEORIES").textContent).toBe("4");
    expect(facts("STOCKS SEARCHED").textContent).toBe("5");
    expect(facts("KEPT BACK").textContent).toBe("2");
    expect(facts("KEPT BACK").title).toBe("MSFT, NVDA");
    expect(facts("TIME").textContent).toBe("-");
    expect(facts("TIME").title).toBe("The server does not estimate run time yet");
    expect(document.querySelector('[data-share="Searched"]')).toBeTruthy();
    expect(document.querySelector('[data-share="Kept back for later"]')).toBeTruthy();
    expect(screen.getByText("THE FOUR STRETCHES")).toBeTruthy();
    const stretches = Array.from(document.querySelectorAll("[data-stretch]")).map((e) => e.getAttribute("title"));
    expect(stretches).toEqual([
      "Screen: 01/11/2013 to 01/09/2019", "Optimise: 03/09/2019 to 31/12/2020",
      "Walk-forward: 01/01/2021 to 31/12/2022", "Later: 03/01/2023 to 31/12/2025",
    ]);
    expect(document.querySelector("[data-cut]")?.getAttribute("title")).toBe("Reserve from 01/01/2023");
    const cells = (tr: Element) => Array.from(tr.querySelectorAll("td")).map((c) => c.textContent);
    const rows = Array.from(document.querySelectorAll("table tr")).filter((tr) => tr.querySelector("td"));
    const n1 = rows.find((tr) => cells(tr)[0] === "Rebound after a crash, Bollinger lower band, RSI")!;
    expect(cells(n1)).toEqual(["Rebound after a crash, Bollinger lower band, RSI", "N", "-", "7", "10 to 40, step 5"]);
    const st = rows.find((tr) => cells(tr)[1] === "ST")!;
    expect(cells(st)).toEqual(["", "ST", "-", "19", "0.01 to 0.1, step 0.005"]);
    expect(screen.getAllByTitle("The preview does not say what a letter is yet").length).toBeGreaterThan(8);
  });

  it("e) a theory without search settings shows Values as a dash with its hover", async () => {
    mount();
    await fillAndPreview();
    const hovers = screen.getAllByTitle("This theory has no search settings, so its search will be refused");
    expect(hovers).toHaveLength(2);
    expect(hovers[0].textContent).toBe("-");
  });

  it("f) a refusal shows the server's words, with no status and no address", async () => {
    reset({ [`POST ${PREVIEW_URL}`]: { status: 422, json: { detail: "The optimise window must start after the screen years end on 2019-09-01." } } });
    mount();
    await ready();
    fireEvent.click(pill("Rebound after a crash"));
    fireEvent.change(screen.getByLabelText("Stock group"), { target: { value: "11" } });
    fireEvent.click(previewButton());
    const text = await screen.findByText("The optimise window must start after the screen years end on 2019-09-01.");
    expect(text).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/422|\/api\//);
    expect(screen.queryByText(/combinations/)).toBeNull();
  });

  it("g) changing the seed after a preview removes the preview card", async () => {
    mount();
    await fillAndPreview();
    fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "8" } });
    expect(screen.queryByText(/combinations/)).toBeNull();
    expect(screen.queryByText("THE FOUR STRETCHES")).toBeNull();
    expect(posts()).toHaveLength(1);
  });

  it("h) APPROVE AND RUN: one POST with the preview body plus the group, the data start and a calendar; the batches reload", async () => {
    mount();
    await fillAndPreview();
    pickDate("Data from", "2012-01-02");
    fireEvent.change(screen.getByLabelText("Group name"), { target: { value: "GROUP_D" } });
    const loadsBefore = batchLoads();
    press("APPROVE AND RUN", document.getElementById("gs1-approve") as HTMLElement);
    const dialog = screen.getByRole("dialog", { name: "Approve and run" });
    expect(dialog.textContent).toContain("Rebound after a crash");
    expect(dialog.textContent).toContain("4 theories");
    expect(dialog.textContent).toContain("5 searched, 2 kept back");
    expect(dialog.textContent).toContain("GROUP_D");
    expect(posts()).toHaveLength(1); // the preview only
    const go = within(dialog).getByRole("button", { name: "APPROVE AND RUN" });
    fireEvent.click(go);
    fireEvent.click(go);
    await within(dialog).findByText("STARTED");
    expect(within(dialog).getByText("4 searches started")).toBeTruthy();
    expect(posts()).toHaveLength(2);
    expect(posts()[1].url).toBe(APPROVE_URL);
    expect(posts()[1].body).toMatchObject({
      universe_id: 11, seed: 7, optimise_window: ["2019-09-03", "2020-12-31"], later_window: ["2023-01-03", "2025-12-31"],
      group_label: "GROUP_D", data_start: "2012-01-02",
    });
    expect(posts()[1].body.calendar_id).toMatch(/^gen-group_d-\d+$/);
    await waitFor(() => expect(batchLoads()).toBeGreaterThan(loadsBefore));
    press("CLOSE", dialog);
    expect(screen.queryByRole("dialog", { name: "Approve and run" })).toBeNull();
  });

  it("h2) a refused approve shows the server's words and still reloads the batches", async () => {
    reset({
      [`POST ${APPROVE_URL}`]: {
        status: 422,
        json: { detail: "The search for Theory one was refused: no data. Batch 3 is saved with the searches made before it; start the rest from the batch." },
      },
    });
    mount();
    await fillAndPreview();
    pickDate("Data from", "2012-01-02");
    fireEvent.change(screen.getByLabelText("Group name"), { target: { value: "GROUP_D" } });
    const loadsBefore = batchLoads();
    press("APPROVE AND RUN", document.getElementById("gs1-approve") as HTMLElement);
    const dialog = screen.getByRole("dialog", { name: "Approve and run" });
    fireEvent.click(within(dialog).getByRole("button", { name: "APPROVE AND RUN" }));
    await within(dialog).findByText(/The search for Theory one was refused/);
    expect(dialog.textContent).not.toMatch(/422|\/api\//);
    await waitFor(() => expect(batchLoads()).toBeGreaterThan(loadsBefore));
    expect(posts()).toHaveLength(2);
  });

  it("i) USE on a template row presses its pill", async () => {
    mount();
    await ready();
    const row = screen.getByText("Pullback in a trend", { selector: "td" }).closest("tr") as HTMLElement;
    expect(pill("Pullback in a trend").getAttribute("aria-pressed")).toBe("false");
    press("USE", row);
    expect(pill("Pullback in a trend").getAttribute("aria-pressed")).toBe("true");
    expect(posts()).toHaveLength(0);
  });

  it("j) no em dash and no snake_case word on screen, before and after a preview", async () => {
    mount();
    await ready();
    let text = document.body.textContent ?? "";
    expect(text).not.toContain(EM_DASH);
    expect(text).not.toMatch(/[a-z]+_[a-z]+/);
    cleanup();
    mount();
    await fillAndPreview();
    text = document.body.textContent ?? "";
    expect(text).not.toContain(EM_DASH);
    expect(text).not.toMatch(/[a-z]+_[a-z]+/);
  });
});
