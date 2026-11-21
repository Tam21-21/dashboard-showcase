import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Searches } from "../pages/research/generator/Searches";
import { batchState, theoryState } from "../pages/research/generator/searchState";
import type { GenBatch, GenTheory, GenTree } from "../data/generatorApi";
import { STORAGE_KEY } from "../settings";

const EM_DASH = String.fromCharCode(8212);
const V3 = "/api/research/generator/v3";

type Call = { method: string; url: string; body: unknown };
type Answer = { status?: number; json: unknown };
let calls: Call[] = [];
let answers: Record<string, Answer> = {};

const batch = (id: number, over: Partial<GenBatch> = {}): GenBatch => ({
  id, template_sha256: `sha-${id}`, universe_id: 3, searched_universe_id: 4, group_label: `GROUP_STORED_${id}`, display_name: `Group ${id}`,
  members: ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III"], kept_back: ["GGG", "HHH", "III"],
  seed: 7, data_start: "2004-01-01", screen_start: "2004-01-01", screen_end: "2006-12-31", optimise_start: "2007-01-01", optimise_end: "2012-12-31",
  later_start: "2017-01-01", later_end: "2020-12-31", calendar_id: `cal-${id}`, approved_at: 1761782400 + id * 86400,
  control: { state: "running", since: null }, removed_at: null, synthetic: false, walkforward: ["2013-01-01", "2016-12-31"], ...over,
});
type Run = [string, string];
const theory = (id: number, runs: Run[], over: Partial<GenTheory> = {}): GenTheory => ({
  id, name: `theory_${id}`, display_name: `Theory ${id}`, choices: {}, search: null, final: "not run", saved: [], stop: null,
  runs: runs.map(([stage, status], i) => ({ stage, run_id: id * 10 + i, status })), ...over,
});
const tree = (b: GenBatch, name: string, theories: GenTheory[]): GenTree => ({
  batch: b, template: { name: name.toLowerCase().replace(/ /g, "_"), display_name: name, sha256: b.template_sha256 }, theories,
});
const held = (n: number, of: number): GenTheory["final"] => ({
  kept_back: [], later_window: [], strategies: Array.from({ length: of }, (_, i) => ({ rank: i + 1, held_up: i < n })),
} as GenTheory["final"]);
const saved = (verdicts: Array<string | null>): GenTheory["saved"] =>
  verdicts.map((w, i) => ({ rank: i + 1, display_name: `S${i + 1}`, picks: {}, verdict: w ? { word: w, id: i + 1 } : null, refusal: null }));
const OK: Run[] = [["screen", "succeeded"], ["optimise", "succeeded"], ["walkforward", "succeeded"]];

const B1 = batch(1);
const B2 = batch(2);
const TREE1 = tree(B1, "Rebound after a crash", [
  theory(1, [["screen", "succeeded"], ["optimise", "running"]]),
  theory(2, [["screen", "pending"]]),
]);
const TREE2 = tree(B2, "Pullback in a trend", [
  theory(3, OK, { final: held(2, 2), saved: saved(["pass", "fail"]) }),
  theory(4, OK, { final: held(2, 2), saved: saved(["fail", null]) }),
]);

function reset(over: Record<string, Answer> = {}) {
  calls = [];
  answers = {
    [`GET ${V3}/batches`]: { json: [B1, B2] },
    [`GET ${V3}/batches/1/tree`]: { json: TREE1 },
    [`GET ${V3}/batches/2/tree`]: { json: TREE2 },
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
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Searches />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const count = (method: string, url: string) => calls.filter((c) => c.method === method && c.url === url).length;
const rowOf = (label: string) => screen.getByText(label).closest("button") as HTMLElement;
const one = (b: GenBatch, theories: GenTheory[]) =>
  reset({ [`GET ${V3}/batches`]: { json: [b] }, [`GET ${V3}/batches/${b.id}/tree`]: { json: tree(b, "Template", theories) } });

beforeEach(() => {
  reset();
  stubFetch();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Generator, section 2, the searches", () => {
  it("g-n1) the list item and the detail title show the display name, never the stored group label", async () => {
    const name = "Nasdaq-100 since 2009, Rebound after a crash search, 1 Nov";
    reset({
      [`GET ${V3}/batches`]: { json: [batch(1, { group_label: "GROUP_NASDAQ_100_REBOUND", display_name: name })] },
    });
    mount();
    await screen.findByText(name, { selector: ".gs-name" });
    expect(document.querySelector("[data-batch] .gs-name")!.textContent).toBe(name);
    expect(document.querySelector(".gs-title")!.textContent).toBe(name);
    expect(document.body.textContent).not.toContain("GROUP_");
  });

  it("a) the batches in server order, each with group label, template, state word and date", async () => {
    mount();
    await screen.findByText("Pullback in a trend");
    const buttons = Array.from(document.querySelectorAll<HTMLElement>("[data-batch]"));
    expect(buttons.map((b) => b.getAttribute("data-batch"))).toEqual(["1", "2"]);
    const first = within(buttons[0]);
    expect(first.getByText("Group 1")).toBeTruthy();
    expect(first.getByText("Rebound after a crash")).toBeTruthy();
    expect(first.getByText("RUNNING").classList.contains("bl")).toBe(true);
    expect(first.getByText("31/10/2025")).toBeTruthy();
    const second = within(buttons[1]);
    expect(second.getByText("1 PASSED").classList.contains("g")).toBe(true);
    expect(second.getByText("01/11/2025")).toBeTruthy();
    expect(buttons[0].className).toContain("on");
  });

  it("b) every theory state word and batch state word", () => {
    const word = (t: GenTheory) => { const s = theoryState(t); return `${s.word}:${s.tone}`; };
    expect(word(theory(1, [["screen", "succeeded"]], { stop: { step: "screen", reason: "x" } }))).toBe("STOPPED:rd");
    expect(word(theory(1, []))).toBe("NOT STARTED:a");
    expect(word(theory(1, [["optimise", "succeeded"]]))).toBe("NOT STARTED:a");
    expect(word(theory(1, [["screen", "running"]]))).toBe("RUNNING:bl");
    expect(word(theory(1, [["screen", "succeeded"], ["optimise", "pending"]]))).toBe("RUNNING:bl");
    expect(word(theory(1, [["screen", "failed"]]))).toBe("FAILED:rd");
    expect(word(theory(1, [["screen", "succeeded"]]))).toBe("SEARCHED:none");
    expect(word(theory(1, OK, { final: held(0, 3) }))).toBe("NOTHING HELD UP:none");
    expect(word(theory(1, OK, { final: held(2, 3), saved: saved(["pass"]) }))).toBe("RUNNING:bl");
    expect(word(theory(1, OK, { final: held(1, 1), saved: saved(["pass"]) }))).toBe("PASSED:g");
    expect(word(theory(1, OK, { final: held(1, 1), saved: saved(["fail"]) }))).toBe("JUDGED:none");

    const bw = (b: GenBatch, ts: GenTheory[]) => { const s = batchState(b, tree(b, "T", ts)); return `${s!.word}:${s!.tone}`; };
    const running = theory(1, [["screen", "running"]]);
    const fresh = theory(2, []);
    const done = theory(3, [["screen", "succeeded"]]);
    const passed = theory(4, OK, { final: held(1, 1), saved: saved(["pass", "pass"]) });
    const stopped = theory(5, [["screen", "succeeded"]], { stop: { step: "screen", reason: "x" } });
    expect(bw(batch(1, { control: { state: "stopped", since: null } }), [running])).toBe("STOPPED:rd");
    expect(bw(batch(1, { control: { state: "paused", since: null } }), [running])).toBe("PAUSED:a");
    expect(bw(batch(1), [])).toBe("NO THEORIES:none");
    expect(bw(batch(1), [running, fresh])).toBe("RUNNING:bl");
    expect(bw(batch(1), [fresh, done])).toBe("NOT STARTED:a");
    expect(bw(batch(1), [done, passed, theory(6, OK, { final: held(1, 1), saved: saved(["pass"]) })])).toBe("3 PASSED:g");
    expect(bw(batch(1), [stopped, stopped])).toBe("STOPPED:rd");
    expect(bw(batch(1), [done, stopped])).toBe("NOTHING FOUND:none");
    expect(batchState(batch(1), undefined)).toBeNull();
    expect(batchState(batch(1, { control: { state: "paused", since: 1761782400 } }), undefined)!.hover).toBe("Since 30/10/2025");
  });

  it("c) picking the second batch shows its facts, theories and four stretches", async () => {
    mount();
    await screen.findByText("Pullback in a trend");
    fireEvent.click(rowOf("Group 2"));
    const panel = await screen.findByTestId("gs-detail");
    await within(panel).findByRole("button", { name: "Theory 3" });
    expect(within(panel).getByText("6 + 3 kept back").getAttribute("title")).toBe("Searched: AAA, BBB, CCC, DDD, EEE, FFF. Kept back: GGG, HHH, III.");
    expect(within(panel).getByText("Pullback in a trend")).toBeTruthy();
    expect(within(panel).getByText("01/11/2025")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "Theory 4" })).toBeTruthy();
    expect(panel.querySelectorAll("[data-stretch]").length).toBe(4);
    expect(panel.querySelector('[data-stretch="walkforward"]')!.getAttribute("title")).toBe("Walk-forward: 01/01/2013 to 31/12/2016");
    expect(panel.querySelector("[data-cut]")).toBeNull();
    expect(within(panel).getByText("1 of 2").classList.contains("g")).toBe(true);
    expect(within(panel).getByText("0 of 2")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Theory 4" }));
    expect(within(panel).getByRole("button", { name: "Theory 4" }).className).toContain("on");
  });

  it("d) the run word of each status and of no run", async () => {
    const mk = (id: number, status: string) => theory(id, [["screen", status]]);
    one(batch(1), [
      theory(1, [["screen", "pending"], ["optimise", "running"], ["walkforward", "succeeded"]]),
      mk(2, "failed"), mk(3, "cancelled"), mk(4, "missing"), mk(5, "weird"),
      theory(6, [["screen", "failed"], ["screen", "succeeded"]]), theory(7, []),
    ]);
    mount();
    const panel = within(await screen.findByTestId("gs-detail"));
    await panel.findByRole("button", { name: "Theory 1" });
    const row = (n: number) => within(panel.getByRole("button", { name: `Theory ${n}` }).closest("tr") as HTMLElement);
    const running = row(1).getAllByText("RUNNING");
    expect(running.length).toBe(2);
    expect(running.every((e) => e.classList.contains("bl"))).toBe(true);
    expect(running[0].closest("[title]")!.getAttribute("title")).toBe("Queued or running: the server does not tell them apart yet");
    expect(row(1).getByText("DONE").classList.contains("g")).toBe(true);
    expect(row(2).getByText("FAILED").classList.contains("rd")).toBe(true);
    expect(row(3).getByText("CANCELLED").classList.contains("rd")).toBe(true);
    expect(row(4).getByText("MISSING").classList.contains("rd")).toBe(true);
    expect(row(5).getByText("WEIRD").classList.contains("a")).toBe(true);
    expect(row(6).getByText("DONE")).toBeTruthy();
    const none = row(7).getAllByText("-").filter((e) => e.getAttribute("title")?.startsWith("No "));
    expect(none.map((e) => e.getAttribute("title"))).toEqual(["No screen run yet", "No optimise run yet", "No walk-forward run yet"]);
  });

  describe("e) RESUME, PAUSE and STOP", () => {
    const buttons = () => {
      const panel = within(screen.getByTestId("gs-detail"));
      return {
        resume: panel.getByRole("button", { name: "RESUME" }) as HTMLButtonElement,
        pause: panel.getByRole("button", { name: "PAUSE" }) as HTMLButtonElement,
        stop: panel.getByRole("button", { name: "STOP" }) as HTMLButtonElement,
      };
    };
    const withBatch = async (b: GenBatch, theories: GenTheory[], extra: Record<string, Answer> = {}) => {
      one(b, theories);
      Object.assign(answers, extra);
      mount();
      await screen.findByRole("button", { name: "Theory 1" });
    };

    it("enabled and disabled by the batch state, with hovers", async () => {
      await withBatch(batch(1), [theory(1, [["screen", "running"]])]);
      let b = buttons();
      expect(b.resume.disabled).toBe(true);
      expect(b.resume.title).toBe("Every theory in this batch already has its search");
      expect(b.pause.disabled).toBe(false);
      expect(b.stop.disabled).toBe(false);
      expect(b.stop.className).toContain("danger");
      cleanup();
      await withBatch(batch(1), [theory(1, [])]);
      expect(buttons().resume.disabled).toBe(false);
      cleanup();
      await withBatch(batch(1, { control: { state: "paused", since: 1761782400 } }), [theory(1, [["screen", "running"]])]);
      b = buttons();
      expect(b.resume.disabled).toBe(false);
      expect(b.pause.disabled).toBe(true);
      expect(b.pause.title).toBe("The batch is paused");
      expect(b.stop.disabled).toBe(false);
      cleanup();
      await withBatch(batch(1, { control: { state: "stopped", since: 1761782400 } }), [theory(1, [["screen", "running"]])]);
      b = buttons();
      for (const k of [b.resume, b.pause, b.stop]) {
        expect(k.disabled).toBe(true);
        expect(k.title).toBe("The batch was stopped on 30/10/2025");
      }
    });

    it("nothing is written on load", async () => {
      await withBatch(batch(1), [theory(1, [])]);
      expect(calls.filter((c) => c.method !== "GET")).toEqual([]);
    });

    it("pause: one POST after the confirm, the button waits, the window closes", async () => {
      await withBatch(batch(1), [theory(1, [["screen", "running"]])], {
        [`POST ${V3}/batches/1/pause`]: { json: { batch_id: 1, state: "paused", since: 1761782500 } },
      });
      fireEvent.click(buttons().pause);
      const win = await screen.findByRole("dialog", { name: "Pause the batch" });
      expect(within(win).getByText(/no new run starts until resume/i)).toBeTruthy();
      expect(count("POST", `${V3}/batches/1/pause`)).toBe(0);
      fireEvent.click(within(win).getByRole("button", { name: "PAUSE" }));
      expect((within(win).getByRole("button", { name: "PAUSE" }) as HTMLButtonElement).disabled).toBe(true);
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(count("POST", `${V3}/batches/1/pause`)).toBe(1);
      expect(calls.find((c) => c.method === "POST")!.body).toEqual({});
    });

    it("resume: shows the state and the theories with no search, one POST, closes", async () => {
      await withBatch(batch(1, { control: { state: "paused", since: 1761782400 } }), [theory(1, []), theory(2, [["screen", "running"]])], {
        [`POST ${V3}/batches/1/resume`]: { json: { batch_id: 1, state: "running", run_ids: [5] } },
      });
      fireEvent.click(buttons().resume);
      const win = await screen.findByRole("dialog", { name: "Resume the batch" });
      expect(within(win).getByText("STATE").nextElementSibling!.textContent).toBe("PAUSED");
      expect(within(win).getByText("NO SEARCH YET").nextElementSibling!.textContent).toBe("1");
      fireEvent.click(within(win).getByRole("button", { name: "CANCEL" }));
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(count("POST", `${V3}/batches/1/resume`)).toBe(0);
      fireEvent.click(buttons().resume);
      fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "RESUME" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(count("POST", `${V3}/batches/1/resume`)).toBe(1);
    });

    it("a 409 detail is shown as sent and the window stays", async () => {
      await withBatch(batch(1), [theory(1, [["screen", "running"]])], {
        [`POST ${V3}/batches/1/pause`]: { status: 409, json: { detail: "Batch 1 is already paused." } },
      });
      fireEvent.click(buttons().pause);
      const win = await screen.findByRole("dialog");
      fireEvent.click(within(win).getByRole("button", { name: "PAUSE" }));
      expect(await within(win).findByText("Batch 1 is already paused.")).toBeTruthy();
      expect(screen.getByRole("dialog")).toBeTruthy();
    });

    it("stop: the answer shows runs cancelled and still running", async () => {
      await withBatch(batch(1), [theory(1, [["screen", "running"]])], {
        [`POST ${V3}/batches/1/stop`]: { json: { batch_id: 1, state: "stopped", since: 1761782500, cancelled_runs: [4, 5], still_running: [6] } },
      });
      fireEvent.click(buttons().stop);
      const win = await screen.findByRole("dialog", { name: "Stop the batch" });
      expect(within(win).getByText(/a stopped batch cannot be resumed/i)).toBeTruthy();
      const go = within(win).getByRole("button", { name: "STOP" });
      expect(go.className).toContain("danger");
      fireEvent.click(go);
      expect(await within(win).findByText("RUNS CANCELLED")).toBeTruthy();
      expect(within(win).getByText("RUNS CANCELLED").nextElementSibling!.textContent).toBe("2");
      const still = within(win).getByText("STILL RUNNING").nextElementSibling!;
      expect(still.textContent).toBe("1");
      expect(still.getAttribute("title")).toBe("These runs had started and finish on their own");
      expect(within(win).getByText("STOPPED").classList.contains("rd")).toBe(true);
      expect(count("POST", `${V3}/batches/1/stop`)).toBe(1);
      fireEvent.click(within(win).getByRole("button", { name: "CLOSE" }));
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    const del = () => {
      const panel = within(screen.getByTestId("gs-detail"));
      return panel.getByRole("button", { name: "DELETE" }) as HTMLButtonElement;
    };
    const STOPPED = { state: "stopped", since: 1761782400 } as const;
    const SAVED_OK = saved([null]);

    it("e5 DELETE is the last button of the actions, red, after STOP", async () => {
      await withBatch(batch(1, { control: STOPPED }), [theory(1, OK)]);
      const row = screen.getByRole("button", { name: "STOP" }).parentElement!;
      const names = Array.from(row.querySelectorAll("button")).map((b) => b.textContent);
      expect(names).toEqual(["RESUME", "PAUSE", "STOP", "DELETE"]);
      expect(del().className).toContain("danger");
    });

    it("e6 DELETE is off with its reason, first match wins, and on for a finished stopped batch", async () => {
      one(batch(1), [theory(1, OK)]);
      answers[`GET ${V3}/batches/1/tree`] = { status: 500, json: { detail: "Nope." } };
      mount();
      await screen.findByText("Nope.");
      expect(del().disabled).toBe(true);
      expect(del().title).toBe("Reading this batch");
      cleanup();
      await withBatch(batch(1), [theory(1, OK)]);
      expect(del().disabled).toBe(true);
      expect(del().title).toBe("Stop the batch first");
      cleanup();
      await withBatch(batch(1, { control: { state: "paused", since: 1761782400 } }), [theory(1, OK)]);
      expect(del().title).toBe("Stop the batch first");
      cleanup();
      await withBatch(batch(1, { control: STOPPED }), [theory(1, [["screen", "running"]])]);
      expect(del().disabled).toBe(true);
      expect(del().title).toBe("Runs of this batch are still going");
      cleanup();
      await withBatch(batch(1, { control: STOPPED }), [theory(1, [["screen", "pending"]])]);
      expect(del().title).toBe("Runs of this batch are still going");
      cleanup();
      await withBatch(batch(1, { control: STOPPED }), [theory(1, OK, { saved: saved(["PASS"]) })]);
      expect(del().disabled).toBe(true);
      expect(del().title).toBe("The judge graded a strategy of this batch; its record stays");
      cleanup();
      await withBatch(batch(1, { control: STOPPED }), [theory(1, OK, { saved: SAVED_OK })]);
      expect(del().disabled).toBe(false);
      expect(del().title).toBe("");
    });

    it("e7 CANCEL sends nothing; DELETE sends one DELETE, reads the batches again and closes", async () => {
      await withBatch(batch(1, { control: STOPPED }), [theory(1, OK)], {
        [`DELETE ${V3}/batches/1`]: { json: { batch_id: 1, removed_at: 1761782500 } },
      });
      fireEvent.click(del());
      const win = await screen.findByRole("dialog", { name: "Delete the batch" });
      expect(within(win).getByText("The batch leaves this page. Its runs, their files and the record of the data it read are kept.")).toBeTruthy();
      expect(within(win).getAllByRole("button").map((b) => b.textContent)).toEqual(["CANCEL", "DELETE"]);
      expect(within(win).getByRole("button", { name: "DELETE" }).className).toContain("danger");
      fireEvent.click(within(win).getByRole("button", { name: "CANCEL" }));
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(count("DELETE", `${V3}/batches/1`)).toBe(0);
      fireEvent.click(del());
      const before = count("GET", `${V3}/batches`);
      fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "DELETE" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(count("DELETE", `${V3}/batches/1`)).toBe(1);
      await waitFor(() => expect(count("GET", `${V3}/batches`)).toBe(before + 1));
    });

    it("e8 a refusal shows the server's sentence and the window stays", async () => {
      await withBatch(batch(1, { control: STOPPED }), [theory(1, OK)], {
        [`DELETE ${V3}/batches/1`]: { status: 409, json: { detail: "Batch 1 still has 2 runs going. Wait for them to end." } },
      });
      fireEvent.click(del());
      const win = await screen.findByRole("dialog");
      fireEvent.click(within(win).getByRole("button", { name: "DELETE" }));
      expect(await within(win).findByText("Batch 1 still has 2 runs going. Wait for them to end.")).toBeTruthy();
      expect(screen.getByRole("dialog")).toBeTruthy();
    });

    it("e9 (guard) on the phone there is no DELETE and STOP stays", async () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout: "phone" }));
      try {
        await withBatch(batch(1, { control: STOPPED }), [theory(1, OK)]);
        expect(screen.queryAllByRole("button", { name: "DELETE" }).length).toBe(0);
        expect(screen.queryAllByRole("button", { name: "STOP" }).length).toBe(1);
      } finally {
        localStorage.removeItem(STORAGE_KEY);
      }
    });
  });

  it("f) no batch yet", async () => {
    reset({ [`GET ${V3}/batches`]: { json: [] } });
    mount();
    const word = await screen.findByText("NO BATCH YET");
    expect(word.classList.contains("state")).toBe(true);
    expect(word.closest("[title]")!.getAttribute("title")).toBe("Approve a theory in section 1 to start a batch");
  });

  it("f1) no batch: START A THEORY scrolls section 1 into view", async () => {
    reset({ [`GET ${V3}/batches`]: { json: [] } });
    const target = document.createElement("div");
    target.id = "gs1";
    const scroll = vi.fn();
    target.scrollIntoView = scroll;
    document.body.appendChild(target);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "START A THEORY" }));
    expect(scroll).toHaveBeenCalledTimes(1);
    target.remove();
  });

  it("f2) a failed read shows the server's words", async () => {
    reset({ [`GET ${V3}/batches`]: { status: 500, json: { detail: "The generator store cannot be read." } } });
    mount();
    expect(await screen.findByText("The generator store cannot be read.")).toBeTruthy();
  });

  it("g) no em dash and no snake_case word in the section", async () => {
    mount();
    await screen.findByText("Pullback in a trend");
    fireEvent.click(rowOf("Group 2"));
    const panel = within(await screen.findByTestId("gs-detail"));
    await panel.findByRole("button", { name: "Theory 3" });
    const text = document.body.textContent ?? "";
    expect(text.includes(EM_DASH)).toBe(false);
    expect(/\b[a-z0-9]+_[a-z0-9_]+\b/.test(text)).toBe(false);
    const titles = Array.from(document.querySelectorAll("[title]")).map((e) => e.getAttribute("title") ?? "").join(" ");
    expect(titles.includes(EM_DASH)).toBe(false);
    expect(/\b[a-z0-9]+_[a-z0-9_]+\b/.test(titles)).toBe(false);
  });

  it("g1) no em dash and no snake_case with the delete window open", async () => {
    one(batch(1, { control: { state: "stopped", since: 1761782400 } }), [theory(1, OK)]);
    mount();
    await screen.findByRole("button", { name: "Theory 1" });
    const open = screen.queryByRole("button", { name: "DELETE" });
    if (open) {
      fireEvent.click(open);
      await screen.findByRole("dialog");
    }
    const text = document.body.textContent ?? "";
    expect(text.includes(EM_DASH)).toBe(false);
    expect(/\b[a-z0-9]+_[a-z0-9_]+\b/.test(text)).toBe(false);
  });

  it("h) the batches and the picked tree are read again 30 s after their first answers", async () => {
    vi.useFakeTimers();
    mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(count("GET", `${V3}/batches`)).toBe(1);
    expect(count("GET", `${V3}/batches/1/tree`)).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(28_000); });
    expect(count("GET", `${V3}/batches`)).toBe(1);
    expect(count("GET", `${V3}/batches/1/tree`)).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500); });
    expect(count("GET", `${V3}/batches`)).toBe(2);
    expect(count("GET", `${V3}/batches/1/tree`)).toBe(2);
  });
});
