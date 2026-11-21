import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Strategies from "../pages/research/Strategies";
import { markerLines } from "../pages/research/strategies/template";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const fetchOverview = vi.fn();
const fetchSource = vi.fn();
const validateCode = vi.fn();
const saveCode = vi.fn();

vi.mock("../data/strategiesApi", () => ({
  strategiesKeys: {
    overview: ["strategies", "overview"],
    parameters: (id: number) => ["strategies", "parameters", id],
    runs: (id: number) => ["strategies", "runs", id],
    versions: (id: number) => ["strategies", "versions", id],
    settings: (id: number) => ["strategies", "settings", id],
    source: (id: number) => ["strategies", "source", id],
    supply: ["strategies", "reserve-supply"],
    retest: (runId: number) => ["strategies", "retest", runId],
    fills: (id: number) => ["strategies", "retest-fills", id],
  },
  RUNS_AT_MOST: 500,
  fetchStrategiesOverview: (...a: unknown[]) => fetchOverview(...a),
  fetchStrategyParameters: (id: number) => Promise.resolve({ strategy_id: id, kind: "standard", run_id: null, parameters: [] }),
  fetchStrategyRuns: () => Promise.resolve([]),
  fetchStrategyVersions: () => Promise.resolve([]),
  fetchStrategySettings: () => Promise.resolve([]),
  fetchStrategyVersion: vi.fn(),
  setStrategySetting: vi.fn(),
  clearStrategySetting: vi.fn(),
  fetchStrategySource: (...a: unknown[]) => fetchSource(...a),
  validateStrategyCode: (...a: unknown[]) => validateCode(...a),
  saveStrategyCode: (...a: unknown[]) => saveCode(...a),
  renameStrategy: vi.fn(),
  deleteStrategy: vi.fn(),
  fetchReserveSupply: () => Promise.resolve({ enumerated: 0, spent: 0, windows: [] }),
  takeReserveExam: vi.fn(),
  fetchRetest: () => Promise.resolve({ state: "never-retested", retests: [] }),
  startRetest: vi.fn(),
  fetchRetestFills: () => Promise.resolve({ fills: [] }),
  replayResult: vi.fn(),
}));
vi.mock("../data/strategyNames", async () => {
  const real = await vi.importActual<typeof import("../data/strategyNames")>("../data/strategyNames");
  const rows = [1, 2].map((id) => ({ id, name: `s${id}`, display_name: `S${id}`, status: "active" }));
  return { ...real, useStrategyNames: () => ({ data: rows, isError: false, error: null }) };
});
vi.mock("../data/judgeApi", () => ({
  fetchJudgeVerdicts: () => Promise.resolve({ store: { table_present: true, row_count: 0 }, rows: [] }),
  approveVerdictForPaper: vi.fn(),
  rejectVerdict: vi.fn(),
}));
vi.mock("../data/paperApi", () => ({ getRunningRuns: () => Promise.resolve([]) }));
vi.mock("../data/runsApi", () => ({
  runsKeys: { list: ["runs", "list"], summary: ["runs", "summary"], strategies: ["strategies", "list"] },
  sendToJudge: vi.fn(),
}));
vi.mock("../data/sweepApi", () => ({
  sweepKeys: { lists: ["sweep", "lists"], params: (id: number) => ["sweep", "params", id] },
  fetchStockLists: () => Promise.resolve([]),
  fetchSweepParams: () => Promise.resolve({ sweep_params: [], param_mode: "zero" }),
  saveListWindow: vi.fn(),
  startSweep: vi.fn(),
  startWalkForward: vi.fn(),
}));
// A textarea stands in for Monaco, which needs a real browser.
vi.mock("../kit/CodeEditor", () => ({
  default: (p: {
    label: string; value: string; onChange?: (v: string) => void; readOnly?: boolean;
    openAt?: { from: number; to: number } | null; errorLine?: number | null;
  }) => (
    <textarea
      aria-label={p.label}
      value={p.value}
      readOnly={p.readOnly}
      onChange={(e) => p.onChange?.(e.target.value)}
      data-open-at={p.openAt ? `${p.openAt.from}-${p.openAt.to}` : ""}
      data-error-line={p.errorLine == null ? "" : String(p.errorLine)}
    />
  ),
}));

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "strategies")!;
const EM_DASH = String.fromCharCode(8212);

const MARKED = "a\n  # ENTRY: when to buy\nb\n  # EXIT: when to sell\nc";
const OVERVIEW = [1, 2].map((id) => ({
  id, name: `s${id}`, display_name: `S${id}`, version: 1, description: null, run: null,
}));

let qc: QueryClient;
function mount() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/app/research/strategies"]}>
        <Routes>
          <Route path="/app/research/strategies" element={<Strategies app={app} section={section} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const press = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
const code = () => screen.getByLabelText("Code") as HTMLTextAreaElement;
async function openEditor() {
  mount();
  await waitFor(() => expect(document.querySelector('[data-strategy="2"]')).not.toBeNull());
  fireEvent.click(document.querySelector('[data-strategy="2"]') as HTMLElement);
  await waitFor(() => expect(screen.getByRole("button", { name: "EDIT CODE" })).toBeEnabled());
  press("EDIT CODE");
  await waitFor(() => expect(screen.getByLabelText("Code")).toBeInTheDocument());
}
const edit = (text: string) => fireEvent.change(code(), { target: { value: text } });

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  fetchOverview.mockResolvedValue(OVERVIEW);
  fetchSource.mockResolvedValue({
    id: 2, name: "breakout_volume", display_name: "Breakout on volume", version: 3, status: "active",
    description: "Buys a dip", source_code: MARKED, sizing_method: "vol_target",
  });
  validateCode.mockResolvedValue({ ok: true, description: "Buys a dip" });
  saveCode.mockResolvedValue({ id: 2, name: "breakout_volume", display_name: "Breakout on volume", version: 4, action: "updated", mirror_status: "ok" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [fetchOverview, fetchSource, validateCode, saveCode]) m.mockReset();
});

describe("Strategy code editor", () => {
  it("e1 markerLines finds the ENTRY and EXIT lines, null when either is missing", () => {
    expect(markerLines(MARKED)).toEqual({ entry: 2, exit: 4 });
    expect(markerLines("a\n  # ENTRY: x\nb")).toBeNull();
    expect(markerLines("a\n  # EXIT: x\nb")).toBeNull();
    expect(markerLines("plain")).toBeNull();
  });

  it("e2 EDIT CODE loads the source once and opens at the marker lines", async () => {
    await openEditor();
    expect(fetchSource).toHaveBeenCalledTimes(1);
    expect(fetchSource).toHaveBeenCalledWith(2);
    expect(code().value).toBe(MARKED);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Breakout on volume");
    expect(document.body.textContent).toContain("Code");
    expect(document.body.textContent).toContain("Version 3");
    expect(code().getAttribute("data-open-at")).toBe("2-5");
  });

  it("e3 VALIDATE checks once, shows the description, saves nothing", async () => {
    await openEditor();
    press("Validate");
    await waitFor(() => expect(document.body.textContent).toContain("Description found: Buys a dip"));
    expect(validateCode).toHaveBeenCalledTimes(1);
    expect(validateCode).toHaveBeenCalledWith(MARKED);
    expect(document.body.textContent).toContain("VALID");
    expect(saveCode).not.toHaveBeenCalled();
  });

  it("e4 a refusal shows LINE n or INVALID in the server's words, and an edit clears it", async () => {
    await openEditor();
    validateCode.mockRejectedValueOnce(new ApiError("400 /x", 400, {
      detail: "strategy source does not compile: 'return' outside function (<strategy>, line 4)",
    }));
    press("Validate");
    await waitFor(() => expect(document.body.textContent).toContain("LINE 4"));
    expect(document.body.textContent).toContain("strategy source does not compile: 'return' outside function (<strategy>, line 4)");
    expect(code().getAttribute("data-error-line")).toBe("4");
    edit(MARKED + "\nd");
    expect(document.body.textContent).not.toContain("LINE 4");
    expect(code().getAttribute("data-error-line")).toBe("");

    validateCode.mockRejectedValueOnce(new ApiError("400 /x", 400, {
      detail: "strategy module docstring is empty; it is required as the strategy description (L29)",
    }));
    press("Validate");
    await waitFor(() => expect(document.body.textContent).toContain("INVALID"));
    expect(document.body.textContent).toContain("strategy module docstring is empty; it is required as the strategy description (L29)");
    expect(code().getAttribute("data-error-line")).toBe("");
    edit(MARKED + "\ne");
    expect(document.body.textContent).not.toContain("INVALID");
  });

  it("e5 SAVE is held until an edit, then saves once as the next version", async () => {
    await openEditor();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" }).title).toBe("Nothing changed since version 3");
    edit(MARKED + "\nd");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    press("Save");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved as version 4"));
    expect(saveCode).toHaveBeenCalledTimes(1);
    expect(saveCode).toHaveBeenCalledWith(2, MARKED + "\nd");
    expect(document.body.textContent).toContain("version 4");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("e6 a refused save shows the server's words", async () => {
    await openEditor();
    saveCode.mockRejectedValueOnce(new ApiError("409 /x", 409, { detail: "strategy is deleted; it cannot be edited" }));
    edit(MARKED + "\nd");
    press("Save");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("strategy is deleted; it cannot be edited"));
  });

  it("e7 BACK: unchanged leaves; changed asks first; after a save it leaves with the strategy picked", async () => {
    await openEditor();
    press("Back");
    await waitFor(() => expect(screen.getByTestId("road")).toBeInTheDocument());
    expect(screen.queryByLabelText("Code")).toBeNull();

    press("EDIT CODE");
    await waitFor(() => expect(screen.getByLabelText("Code")).toBeInTheDocument());
    edit(MARKED + "\nd");
    press("Back");
    const win = screen.getByRole("dialog", { name: "Leave the editor?" });
    expect(win.textContent).toContain("Changes since the last save are lost.");
    fireEvent.click(within(win).getByRole("button", { name: "CANCEL" }));
    expect(screen.getByLabelText("Code")).toBeInTheDocument();
    press("Back");
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "LEAVE" }));
    await waitFor(() => expect(screen.getByTestId("road")).toBeInTheDocument());

    press("EDIT CODE");
    await waitFor(() => expect(screen.getByLabelText("Code")).toBeInTheDocument());
    edit(MARKED + "\nd");
    press("Save");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved as version 4"));
    press("Back");
    await waitFor(() => expect(screen.getByTestId("road")).toBeInTheDocument());
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("e8 no em dash and no snake_case in the page's text outside the code", async () => {
    await openEditor();
    validateCode.mockRejectedValueOnce(new ApiError("400 /x", 400, { detail: "strategy source does not compile (line 4)" }));
    press("Validate");
    await waitFor(() => expect(document.body.textContent).toContain("LINE 4"));
    const leaves = Array.from(document.body.querySelectorAll("*"))
      .filter((e) => e.tagName !== "TEXTAREA" && e.children.length === 0)
      .map((e) => `${e.textContent ?? ""} ${e.getAttribute("title") ?? ""}`);
    for (const t of leaves) {
      expect(t).not.toContain(EM_DASH);
      expect(t).not.toMatch(/\b[a-z0-9]+_[a-z0-9_]+\b/);
    }
  });

  it("e-sweep SAVE AND RUN A SWEEP saves once, then opens the window on the saved strategy", async () => {
    await openEditor();
    const bar = Array.from(document.querySelectorAll(".se-bar button")).map((b) => b.textContent ?? "");
    expect(bar.map((t) => t.replace(/ \d+$/, ""))).toEqual(
      ["Validate", "Quick test", "Versions", "Settings", "Back", "Save", "Save and run a sweep"],
    );
    edit(MARKED + "\nd");
    press("Save and run a sweep");
    const win = await screen.findByRole("dialog", { name: "Run a sweep" });
    expect(saveCode).toHaveBeenCalledTimes(1);
    expect(saveCode).toHaveBeenCalledWith(2, MARKED + "\nd");
    await waitFor(() => expect((within(win).getByLabelText("Strategy") as HTMLSelectElement).value).toBe("2"));
  });

  it("e-sweep with nothing changed it opens the window with no save; a refused save opens nothing", async () => {
    await openEditor();
    press("Save and run a sweep");
    await screen.findByRole("dialog", { name: "Run a sweep" });
    expect(saveCode).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "CANCEL" }));
    saveCode.mockRejectedValueOnce(new ApiError("409 /x", 409, { detail: "strategy is deleted; it cannot be edited" }));
    edit(MARKED + "\nd");
    press("Save and run a sweep");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("strategy is deleted; it cannot be edited"));
    expect(screen.queryByRole("dialog", { name: "Run a sweep" })).toBeNull();
    expect(saveCode).toHaveBeenCalledTimes(1);
  });
});
