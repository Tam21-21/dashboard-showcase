import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TheoryCards } from "../pages/research/generator/TheoryCards";
import type { GenTheory } from "../data/generatorApi";

const EM_DASH = String.fromCharCode(8212);
let urls: string[] = [];
let verdicts: Record<number, unknown> = {};

const VALID = [3638880, 368640, 345600, 89856, 62208];
const rounds = () => ({
  rounds: VALID.map((v, i) => ({
    round: i + 1, valid_strategies: v, ran: "sample of 500", strategies: 500, new_trials: 500, best_trial_ids: [1, 2, 3, 4, 5],
  })),
  quality: { note: "x" },
  stop: "max rounds",
});
const PICK = { picks: { H: 10, L: 30 }, finalist_rank: 1, optimise_sharpe: 0.84, reason: "Best finalist on the optimise years" };

function make(over: Record<string, unknown> = {}): GenTheory {
  return {
    id: 1, name: "theory_1", display_name: "Theory one", choices: {}, runs: [],
    search: { rounds: rounds(), finalists: [{}, {}, {}], pick: PICK },
    final: "not run", saved: [], stop: null, ...over,
  } as unknown as GenTheory;
}

const outcomes = (cleared: number) =>
  Array.from({ length: 9 }, (_, i) => ({ check: i + 1, outcome: i < cleared ? "cleared" : "failed" }));

function mount(theory: GenTheory) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TheoryCards theory={theory} />
    </QueryClientProvider>,
  );
}

const fact = (k: string) => screen.getByText(k, { selector: "span" }).parentElement!.querySelector("b") as HTMLElement;
const titles = (root: ParentNode) => Array.from(root.querySelectorAll("title")).map((t) => t.textContent);

beforeEach(() => {
  urls = [];
  verdicts = { 1: { check_outcomes: outcomes(9) }, 2: { check_outcomes: outcomes(7) } };
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    urls.push(url);
    const id = Number(url.split("/").pop());
    const hit = verdicts[id];
    if (!hit) throw new Error(`no answer for ${url}`);
    return { ok: true, status: 200, statusText: "OK", json: async () => hit };
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Generator, section 2, the picked theory", () => {
  it("a) six bars, round hovers, legend", () => {
    mount(make());
    const cards = within(screen.getByTestId("gs-cards"));
    expect(document.querySelectorAll("[data-bar]").length).toBe(6);
    const t = titles(screen.getByTestId("gs-cards"));
    expect(t).toContain("Round 1: 3,638,880 in the box, 500 tried (sample of 500), 5 kept for the next round");
    expect(t).toContain("Finalists: 3");
    expect(cards.getByText("Theory one")).toBeTruthy();
    const lg = (k: string) => cards.getByText(k).parentElement!.querySelector(".num")!.textContent;
    expect(lg("TRIED")).toBe("2,500");
    expect(lg("KEPT")).toBe("25");
    expect(lg("FINALISTS")).toBe("3");
  });

  it("b) the facts and the pick", () => {
    mount(make());
    expect(fact("ROUNDS").textContent).toBe("5");
    expect(fact("ENDED").textContent).toBe("MAX ROUNDS");
    expect(fact("PICK").textContent).toBe("#1 H 10, L 30");
    expect(fact("PICK").getAttribute("title")).toBe("Optimise Sharpe 0.84");
    cleanup();
    mount(make({ search: { rounds: rounds(), finalists: [], pick: { picks: null, finalist_rank: 0, optimise_sharpe: 0, reason: "No finalist cleared the bar" } } }));
    expect(fact("PICK").textContent).toBe("-");
    expect(fact("PICK").getAttribute("title")).toBe("No finalist cleared the bar");
  });

  it("c) no search yet", () => {
    mount(make({ search: null }));
    expect(fact("ROUNDS").textContent).toBe("-");
    expect(fact("ROUNDS").getAttribute("title")).toBe("No search rounds yet: the search has not finished");
    expect(document.querySelectorAll("[data-bar]").length).toBe(0);
  });

  it("d) families are a dash with the reason", () => {
    mount(make());
    const dash = screen.getByText("FAMILIES").closest(".cd")!.querySelector("[title]")!;
    expect(dash.textContent).toBe("-");
    expect(dash.getAttribute("title")).toBe("The server does not send the families yet (G10)");
  });

  it("e) the stop card", () => {
    mount(make({ stop: { step: "walkforward", reason: "No stock had enough bars" } }));
    expect(screen.getByText("STOPPED AT WALK-FORWARD").classList.contains("rd")).toBe(true);
    expect(screen.getByText("No stock had enough bars")).toBeTruthy();
    cleanup();
    mount(make());
    expect(screen.queryByText(/STOPPED AT/)).toBeNull();
    cleanup();
    mount(make({ stop: { step: "screen", reason: "r" } }));
    expect(screen.getByText("STOPPED AT SCREEN")).toBeTruthy();
  });

  it("f) saved strategies with their nine checks and the judge's word", async () => {
    mount(make({
      saved: [
        { rank: 1, display_name: "Strat A", picks: { H: 10, L: 30 }, verdict: { word: "pass", id: 1 }, refusal: null },
        { rank: 2, display_name: "Strat B", picks: { H: 12 }, verdict: { word: "fail", id: 2 }, refusal: null },
        { rank: 3, display_name: "Strat C", picks: { H: 14 }, verdict: null, refusal: "No bars for the kept-back stocks" },
      ],
    }));
    const saved = within(screen.getByTestId("gs-saved"));
    expect(saved.getByText("#1").getAttribute("title")).toBe("Strat A");
    expect(saved.getByText("H 10, L 30")).toBeTruthy();
    expect(saved.getByText("PASS").classList.contains("g")).toBe(true);
    expect(saved.getByText("FAIL").classList.contains("rd")).toBe(true);
    const refused = saved.getByText("REFUSED");
    expect(refused.classList.contains("a")).toBe(true);
    expect(refused.closest("[title]")!.getAttribute("title")).toBe("No bars for the kept-back stocks");
    await screen.findByText("9 OF 9 CLEARED");
    expect(await screen.findByText("7 OF 9 CLEARED")).toBeTruthy();
    expect([...urls].sort()).toEqual(["/api/judge/verdicts/1", "/api/judge/verdicts/2"]);
    expect(saved.getAllByText("-").some((e) => e.closest("[title]")?.getAttribute("title") === "The judge refused, so no check ran")).toBe(true);
  });

  it("g) the three nothing-saved hovers", () => {
    const hover = () => screen.getByTestId("gs-saved").querySelector("[title]")!.getAttribute("title");
    mount(make({ stop: { step: "screen", reason: "r" } }));
    expect(hover()).toBe("Nothing saved: the theory stopped");
    cleanup();
    mount(make({ final: { kept_back: [], later_window: [], strategies: [{ rank: 1, held_up: false }] } }));
    expect(hover()).toBe("Nothing saved: no strategy held up");
    cleanup();
    mount(make());
    expect(hover()).toBe("Nothing saved yet");
  });

  it("h) no em dash and no snake_case word", async () => {
    mount(make({
      stop: { step: "walkforward", reason: "r" },
      saved: [{ rank: 1, display_name: "Strat A", picks: { H: 10 }, verdict: { word: "pass", id: 1 }, refusal: null }],
    }));
    await waitFor(() => expect(screen.getByText("9 OF 9 CLEARED")).toBeTruthy());
    const root = screen.getByTestId("gs-theory");
    const text = root.textContent ?? "";
    const hovers = Array.from(root.querySelectorAll("[title]")).map((e) => e.getAttribute("title")).join(" ");
    const all = `${text} ${hovers} ${titles(root).join(" ")}`;
    expect(all.includes(EM_DASH)).toBe(false);
    expect(/\b[a-z0-9]+_[a-z0-9_]+\b/.test(all)).toBe(false);
  });
});
