import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Strategies from "../pages/research/Strategies";
import { generatorReady } from "../pages/research/strategies/NewStrategy";
import { blankTemplate, markerLines } from "../pages/research/strategies/template";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const fetchOverview = vi.fn();
const fetchSource = vi.fn();
const createStrategy = vi.fn();
const saveCode = vi.fn();
const navigate = vi.fn();
let nameRows: Array<{ id: number; name: string; display_name: string | null; status: string }> = [];

vi.mock("react-router-dom", async () => {
  const real = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...real, useNavigate: () => navigate };
});
vi.mock("../shell/sections", async () => {
  const real = await vi.importActual<typeof import("../shell/sections")>("../shell/sections");
  const apps = real.APPS.map((a) =>
    a.id === "research" && !a.sections.some((s) => s.id === "generator")
      ? { ...a, sections: [...a.sections, { id: "generator", label: "Generator", icon: "gen" }] }
      : a,
  );
  return { ...real, APPS: apps };
});
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
  validateStrategyCode: vi.fn(),
  saveStrategyCode: (...a: unknown[]) => saveCode(...a),
  createStrategy: (...a: unknown[]) => createStrategy(...a),
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
  return { ...real, useStrategyNames: () => ({ data: nameRows, isError: false, error: null }) };
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

const OVERVIEW = [{ id: 2, name: "breakout_volume", display_name: "Breakout on volume", version: 3, description: "Buys a dip", run: null }];

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const write = () => screen.getByRole("button", { name: "WRITE IT MYSELF" }) as HTMLButtonElement;
const code = () => screen.getByLabelText("Code") as HTMLTextAreaElement;
async function openWindow() {
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "New strategy" })).toBeInTheDocument());
  press("New strategy");
  expect(screen.getByRole("dialog", { name: "New strategy" })).toBeInTheDocument();
}
async function fillAndWrite(about = "Buys a dip") {
  await openWindow();
  type("Name", "new_one");
  type("Display", "My breakout");
  if (about) type("About", about);
  fireEvent.click(write());
  await waitFor(() => expect(screen.getByLabelText("Code")).toBeInTheDocument());
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  nameRows = [
    { id: 1, name: "atss_strategy", display_name: "Atss strategy", status: "deleted" },
    { id: 2, name: "breakout_volume", display_name: "Breakout on volume", status: "active" },
  ];
  fetchOverview.mockResolvedValue(OVERVIEW);
  fetchSource.mockResolvedValue({
    id: 2, name: "breakout_volume", display_name: "Breakout on volume", version: 3, status: "active",
    description: "Buys a dip", source_code: "x = 1", sizing_method: "vol_target",
  });
  createStrategy.mockResolvedValue({ id: 21, name: "new_one", display_name: "My breakout", version: 1, action: "created", mirror_status: "synced" });
  saveCode.mockResolvedValue({ id: 21, name: "new_one", display_name: "My breakout", version: 2, action: "updated", mirror_status: "synced" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [fetchOverview, fetchSource, createStrategy, saveCode, navigate]) m.mockReset();
});

describe("New strategy window", () => {
  it("w1 NEW STRATEGY sits in the page header, with strategies and without", async () => {
    mount();
    const btn = await screen.findByRole("button", { name: "New strategy" });
    expect(btn.closest(".ph")).not.toBeNull();
    press("New strategy");
    expect(screen.getByRole("dialog", { name: "New strategy" })).toBeInTheDocument();
    cleanup();

    fetchOverview.mockResolvedValue([]);
    mount();
    await screen.findByText(/No strategies yet/);
    expect(screen.getByRole("button", { name: "New strategy" }).closest(".ph")).not.toBeNull();
  });

  it("w2 the name check and the held button say why", async () => {
    await openWindow();
    expect(write().disabled).toBe(true);
    expect(write().title).toBe("Type a name");
    type("Name", "atss_strategy");
    expect(screen.getByText("NAME TAKEN")).toBeInTheDocument();
    expect(write().disabled).toBe(true);
    expect(write().title).toBe("This name is taken");
    type("Name", "new_one");
    expect(screen.getByText("NAME FREE")).toBeInTheDocument();
    expect(screen.queryByText("NAME TAKEN")).toBeNull();
    expect(write().disabled).toBe(true);
    expect(write().title).toBe("Type a display name");
    expect((screen.getByLabelText("Display") as HTMLInputElement).maxLength).toBe(60);
    type("Display", "My breakout");
    expect(write().disabled).toBe(false);
  });

  it("w3 blankTemplate is the 26-line scaffold with its markers", () => {
    const t = blankTemplate("x");
    expect(t.split("\n")).toHaveLength(26);
    expect(t).toContain("close.rolling(20).mean()");
    expect(t).toContain("close.rolling(50).mean()");
    expect(markerLines(t)).toEqual({ entry: 16, exit: 19 });
    expect(blankTemplate('Buys the "dip"').split("\n")[0]).toBe('"""Buys the \\"dip\\""""');
    expect(blankTemplate("a\nb").split("\n")[0]).toBe('"""a b"""');
  });

  it("w4 WRITE IT MYSELF opens the editor on version 1 (new) with the template", async () => {
    await fillAndWrite();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("My breakout");
    expect(document.body.textContent).toContain("Version 1, new");
    expect(code().value.split("\n")[0]).toBe('"""Buys a dip"""');
    expect(code().getAttribute("data-open-at")).toBe("16-20");
    expect(fetchSource).not.toHaveBeenCalled();
    cleanup();

    await fillAndWrite("");
    expect(code().value.split("\n")[0]).toBe('"""My breakout"""');
  });

  it("w5 the first SAVE creates the strategy, later ones save versions, refusals in the server's words", async () => {
    await fillAndWrite();
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved as version 1"));
    expect(createStrategy).toHaveBeenCalledTimes(1);
    expect(createStrategy).toHaveBeenCalledWith("new_one", "My breakout", blankTemplate("Buys a dip"));
    expect(document.body.textContent).toContain("version 1");
    expect(document.body.textContent).not.toContain("(new)");
    expect(saveCode).not.toHaveBeenCalled();

    const edited = blankTemplate("Buys a dip") + "\n";
    fireEvent.change(code(), { target: { value: edited } });
    press("Save");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved as version 2"));
    expect(saveCode).toHaveBeenCalledTimes(1);
    expect(saveCode).toHaveBeenCalledWith(21, edited);
    expect(createStrategy).toHaveBeenCalledTimes(1);
    cleanup();

    createStrategy.mockReset();
    createStrategy.mockRejectedValueOnce(new ApiError("409 /x", 409, {
      detail: "strategy_name_exists: 'new_one' already exists (id=3); edit it via PUT /strategies/3",
    }));
    await fillAndWrite();
    press("Save");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("'new_one' already exists (id=3); edit it via PUT /strategies/3"));
  });

  it("w6 a copy reads the source once; deleted and missing strategies are held with a reason", async () => {
    await openWindow();
    type("Name", "new_copy");
    type("Display", "Copy one");
    press("A copy of");
    const pick = screen.getByLabelText("Copy of") as HTMLSelectElement;
    expect(Array.from(pick.options).map((o) => o.textContent)).toEqual(["Breakout on volume"]);
    const about = screen.getByLabelText("About") as HTMLTextAreaElement;
    expect(about.disabled).toBe(true);
    expect(about.title).toBe("A copy keeps the description written in its code");
    fireEvent.click(write());
    await waitFor(() => expect(screen.getByLabelText("Code")).toBeInTheDocument());
    expect(fetchSource).toHaveBeenCalledTimes(1);
    expect(fetchSource).toHaveBeenCalledWith(2);
    expect(code().value).toBe("x = 1");
    expect(code().getAttribute("data-open-at")).toBe("");
    cleanup();

    fetchSource.mockReset();
    fetchSource.mockRejectedValue(new ApiError("404 /x", 404, { detail: "strategy not found" }));
    await openWindow();
    type("Name", "new_copy");
    type("Display", "Copy one");
    press("A copy of");
    fireEvent.click(write());
    const win = screen.getByRole("dialog", { name: "New strategy" });
    await waitFor(() => expect(win.textContent).toContain("strategy not found"));
    expect(screen.queryByLabelText("Code")).toBeNull();
    expect(write().disabled).toBe(true);
    expect(write().title).toBe("This strategy was deleted; pick another");
    cleanup();

    fetchSource.mockReset();
    nameRows = [{ id: 2, name: "breakout_volume", display_name: "Breakout on volume", status: "deleted" }];
    await openWindow();
    type("Name", "new_copy");
    type("Display", "Copy one");
    press("A copy of");
    expect(write().disabled).toBe(true);
    expect(write().title).toBe("This strategy was deleted; pick another");
    expect(fetchSource).not.toHaveBeenCalled();
    cleanup();

    fetchOverview.mockResolvedValue([]);
    await openWindow();
    type("Name", "new_copy");
    type("Display", "Copy one");
    press("A copy of");
    expect((screen.getByLabelText("Copy of") as HTMLSelectElement).disabled).toBe(true);
    expect(write().disabled).toBe(true);
    expect(write().title).toBe("No strategy to copy yet");
  });

  it("w7 BUILD WITH THE GENERATOR is held while the Generator does not read the name", async () => {
    // The Generator page does not read ?name= yet, so even with its section present the button is held.
    expect(generatorReady([app])).toBe(false);
    expect(generatorReady([{ ...app, sections: app.sections.filter((s) => s.id !== "generator") }])).toBe(false);

    await openWindow();
    type("Display", "My breakout");
    const go = screen.getByRole("button", { name: "BUILD WITH THE GENERATOR" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    expect(go.title).toBe("The Generator does not take the name yet");
    fireEvent.click(go);
    expect(navigate).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "New strategy" })).toBeInTheDocument();
  });

  it("w8 no em dash and no snake_case in the window's text", async () => {
    await openWindow();
    type("Name", "atss_strategy");
    press("A copy of");
    const win = screen.getByRole("dialog", { name: "New strategy" });
    const leaves = Array.from(win.querySelectorAll("*"))
      .filter((e) => e.tagName !== "INPUT" && e.children.length === 0)
      .map((e) => `${e.textContent ?? ""} ${e.getAttribute("title") ?? ""}`);
    expect(leaves.length).toBeGreaterThan(5);
    for (const t of leaves) {
      expect(t).not.toContain(EM_DASH);
      expect(t).not.toMatch(/\b[a-z0-9]+_[a-z0-9_]+\b/);
    }
    expect(within(win).getByRole("button", { name: "CANCEL" })).toBeInTheDocument();
  });
});
