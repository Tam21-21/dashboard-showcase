import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StrategyEditor from "../pages/research/strategies/StrategyEditor";
import { ApiError } from "../../lib/api";

const fetchSource = vi.fn();
const fetchVersions = vi.fn();
const fetchVersion = vi.fn();
const fetchSettings = vi.fn();
const setSetting = vi.fn();
const clearSetting = vi.fn();
const saveCode = vi.fn();

vi.mock("../data/strategiesApi", () => ({
  strategiesKeys: {
    overview: ["strategies", "overview"],
    versions: (id: number) => ["strategies", "versions", id],
    settings: (id: number) => ["strategies", "settings", id],
    source: (id: number) => ["strategies", "source", id],
  },
  fetchStrategySource: (...a: unknown[]) => fetchSource(...a),
  fetchStrategyVersions: (...a: unknown[]) => fetchVersions(...a),
  fetchStrategyVersion: (...a: unknown[]) => fetchVersion(...a),
  fetchStrategySettings: (...a: unknown[]) => fetchSettings(...a),
  setStrategySetting: (...a: unknown[]) => setSetting(...a),
  clearStrategySetting: (...a: unknown[]) => clearSetting(...a),
  saveStrategyCode: (...a: unknown[]) => saveCode(...a),
  createStrategy: vi.fn(),
  validateStrategyCode: vi.fn(),
}));
vi.mock("../data/strategyNames", () => ({ strategyNamesKey: ["strategy-names"] }));
// A textarea stands in for Monaco, which needs a real browser.
vi.mock("../kit/CodeEditor", () => ({
  default: (p: { label: string; value: string; onChange?: (v: string) => void; readOnly?: boolean }) => (
    <textarea
      aria-label={p.label}
      value={p.value}
      readOnly={p.readOnly}
      data-readonly={p.readOnly ? "yes" : "no"}
      onChange={(e) => p.onChange?.(e.target.value)}
    />
  ),
}));

const EM_DASH = String.fromCharCode(8212);
const VERSIONS = [
  { version: 2, description: "Breakout on volume\nBuys a 20-day high", edited_at: 1793318400 },
  { version: 1, description: null, edited_at: 1793232000 },
];
const SETTINGS = [
  { scope: "slippage", key: "equities.default", value: 0.0005, value_type: "number", unit: "percent", label: "Equities (default)", source: "override", global_value: 0.0003, overridable: true, source_note: "thin stocks" },
  { scope: "walkforward", key: "anchoring", value: "rolling", value_type: "string", unit: "scheme", label: "Anchoring", source: "inherited", global_value: "rolling", overridable: false, source_note: null },
  { scope: "event_strategy", key: "max_concurrent", value: 5, value_type: "number", unit: "count", label: "Max concurrent positions", source: "inherited", global_value: 5, overridable: true, source_note: null },
];

function mount(target: Parameters<typeof StrategyEditor>[0]["target"] = { kind: "edit", id: 2 }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <StrategyEditor target={target} onBack={() => {}} />
    </QueryClientProvider>,
  );
}
const press = (name: string | RegExp) => fireEvent.click(screen.getByRole("button", { name }));
async function ready() {
  mount();
  await waitFor(() => expect(screen.getByLabelText("Code")).toBeInTheDocument());
}
async function openVersions() {
  await ready();
  await waitFor(() => expect(screen.getByRole("button", { name: "Versions 2" })).toBeEnabled());
  press("Versions 2");
  return screen.getByRole("dialog", { name: "Versions" });
}
async function openSettings() {
  await ready();
  await waitFor(() => expect(screen.getByRole("button", { name: "Settings" })).toBeEnabled());
  press("Settings");
  const win = await screen.findByRole("dialog", { name: "Settings" });
  await waitFor(() => expect(win.textContent).not.toContain("Loading the settings"));
  return win;
}
const rowOf = (win: HTMLElement, text: string) =>
  Array.from(win.querySelectorAll("tr")).find((r) => r.textContent?.includes(text)) as HTMLElement;

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  fetchSource.mockResolvedValue({
    id: 2, name: "breakout_volume", display_name: "Breakout on volume", version: 2, status: "active",
    description: "x", source_code: "code v2", sizing_method: "vol_target",
  });
  fetchVersions.mockResolvedValue(VERSIONS);
  fetchVersion.mockResolvedValue({ source_code: "code of version 1", description: null });
  fetchSettings.mockResolvedValue(SETTINGS);
  setSetting.mockResolvedValue({ ok: true });
  clearSetting.mockResolvedValue({ ok: true });
  saveCode.mockResolvedValue({ id: 2, name: "breakout_volume", display_name: "Breakout on volume", version: 3, action: "updated", mirror_status: "ok" });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  for (const m of [fetchSource, fetchVersions, fetchVersion, fetchSettings, setSetting, clearSetting, saveCode]) m.mockReset();
});

describe("Editor versions window", () => {
  it("v1 the bar reads VERSIONS 2; both are held on a new unsaved strategy", async () => {
    await ready();
    await waitFor(() => expect(screen.getByRole("button", { name: "Versions 2" })).toBeEnabled());
    const names = Array.from(document.querySelectorAll(".se-bar button")).map((b) => b.textContent);
    expect(names.indexOf("Quick test")).toBe(names.indexOf("Validate") + 1);
    expect(names.indexOf("Versions 2")).toBe(names.indexOf("Quick test") + 1);
    expect(names.indexOf("Settings")).toBe(names.indexOf("Versions 2") + 1);
    cleanup();
    mount({ kind: "new", name: "n1", display: "N one", code: "c" });
    for (const n of ["Versions", "Settings"]) {
      const b = screen.getByRole("button", { name: n });
      expect(b).toBeDisabled();
      expect(b.title).toBe("Save the strategy first");
    }
  });

  it("v2 the window lists 2 then 1 with dates and the first description line", async () => {
    const win = await openVersions();
    const rows = Array.from(win.querySelectorAll("tbody tr"));
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("2");
    expect(rows[0].textContent).toContain("30/10/2026");
    expect(rows[1].textContent).toContain("29/10/2026");
    const d = within(rows[0] as HTMLElement).getByText("Breakout on volume");
    expect(d.getAttribute("title")).toBe("Breakout on volume\nBuys a 20-day high");
    const none = within(rows[1] as HTMLElement).getByText("-");
    expect(none.getAttribute("title")).toBe("No description");
  });

  it("v3 OPEN shows that version's code read-only, BACK returns, a 404 shows its words", async () => {
    const win = await openVersions();
    fireEvent.click(within(rowOf(win, "29/10/2026")).getByRole("button", { name: "OPEN" }));
    await waitFor(() => expect(within(win).getByText("Version 1")).toBeInTheDocument());
    expect(fetchVersion).toHaveBeenCalledTimes(1);
    expect(fetchVersion).toHaveBeenCalledWith(2, 1);
    const ed = (await within(win).findByLabelText("Version 1 code")) as HTMLTextAreaElement;
    expect(ed.value).toBe("code of version 1");
    expect(ed.getAttribute("data-readonly")).toBe("yes");
    fireEvent.click(within(win).getByRole("button", { name: "BACK" }));
    expect(win.querySelectorAll("tbody tr").length).toBe(2);

    fetchVersion.mockRejectedValueOnce(new ApiError("404 /x", 404, { detail: "no version 1 recorded for strategy 2" }));
    fireEvent.click(within(rowOf(win, "29/10/2026")).getByRole("button", { name: "OPEN" }));
    await waitFor(() => expect(win.textContent).toContain("no version 1 recorded for strategy 2"));
  });

  it("v4 after an editor SAVE the versions are fetched again", async () => {
    await ready();
    await waitFor(() => expect(screen.getByRole("button", { name: "Versions 2" })).toBeEnabled());
    fetchVersions.mockResolvedValue([{ version: 3, description: null, edited_at: 1793404800 }, ...VERSIONS]);
    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "code v3" } });
    press("Save");
    await waitFor(() => expect(screen.getByRole("button", { name: "Versions 3" })).toBeInTheDocument());
  });
});

describe("Editor settings window", () => {
  it("s1 lists the overridable rows with value and source, CLEAR only on the override", async () => {
    const win = await openSettings();
    const rows = Array.from(win.querySelectorAll("tbody tr"));
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("Slippage, Equities (default)");
    expect(rows[0].textContent).toContain("0.0005");
    expect(rows[0].textContent).toContain("override: thin stocks");
    expect(within(rows[0] as HTMLElement).getByRole("button", { name: "CLEAR" })).toBeInTheDocument();
    expect(rows[1].textContent).toContain("Event strategy, Max concurrent positions");
    expect(rows[1].textContent).toContain("global default");
    expect(within(rows[1] as HTMLElement).queryByRole("button", { name: "CLEAR" })).toBeNull();
    expect(win.textContent).not.toContain("Anchoring");
  });

  it("s2 SET opens Value and Note; SAVE sends the text once and refetches; an empty note is null", async () => {
    const win = await openSettings();
    const before = fetchSettings.mock.calls.length;
    fireEvent.click(within(rowOf(win, "Max concurrent")).getByRole("button", { name: "SET" }));
    const value = within(win).getByLabelText("Value") as HTMLInputElement;
    expect(value.value).toBe("5");
    expect(within(win).getByLabelText("Note")).toBeInTheDocument();
    fireEvent.change(value, { target: { value: "3" } });
    fireEvent.change(within(win).getByLabelText("Note"), { target: { value: "fewer at once" } });
    fireEvent.click(within(win).getByRole("button", { name: "SAVE" }));
    await waitFor(() => expect(setSetting).toHaveBeenCalledTimes(1));
    expect(setSetting).toHaveBeenCalledWith(2, "event_strategy", "max_concurrent", "3", "fewer at once");
    await waitFor(() => expect(fetchSettings.mock.calls.length).toBeGreaterThan(before));

    fireEvent.click(within(rowOf(win, "Max concurrent")).getByRole("button", { name: "SET" }));
    fireEvent.change(within(win).getByLabelText("Value"), { target: { value: "4" } });
    fireEvent.change(within(win).getByLabelText("Note"), { target: { value: "  " } });
    fireEvent.click(within(win).getByRole("button", { name: "SAVE" }));
    await waitFor(() => expect(setSetting).toHaveBeenCalledTimes(2));
    expect(setSetting).toHaveBeenLastCalledWith(2, "event_strategy", "max_concurrent", "4", null);
  });

  it("s3 a refused value shows the server's words under the row", async () => {
    const win = await openSettings();
    setSetting.mockRejectedValueOnce(new ApiError("400 /x", 400, {
      detail: "override out of bounds for slippage.equities.default: 5.0 must be at most 0.001",
    }));
    fireEvent.click(within(rowOf(win, "Slippage")).getByRole("button", { name: "SET" }));
    fireEvent.change(within(win).getByLabelText("Value"), { target: { value: "5" } });
    fireEvent.click(within(win).getByRole("button", { name: "SAVE" }));
    await waitFor(() => expect(win.textContent).toContain("override out of bounds for slippage.equities.default: 5.0 must be at most 0.001"));
  });

  it("s4 CLEAR deletes the override once and refetches", async () => {
    const win = await openSettings();
    const before = fetchSettings.mock.calls.length;
    fireEvent.click(within(rowOf(win, "Slippage")).getByRole("button", { name: "CLEAR" }));
    await waitFor(() => expect(clearSetting).toHaveBeenCalledTimes(1));
    expect(clearSetting).toHaveBeenCalledWith(2, "slippage", "equities.default");
    await waitFor(() => expect(fetchSettings.mock.calls.length).toBeGreaterThan(before));
  });

  it("s5 no em dash and no snake_case in either window's leaf text", async () => {
    const check = (root: HTMLElement) => {
      const leaves = Array.from(root.querySelectorAll("*"))
        .filter((e) => e.tagName !== "TEXTAREA" && e.children.length === 0)
        .map((e) => `${e.textContent ?? ""} ${e.getAttribute("title") ?? ""} ${e.getAttribute("aria-label") ?? ""}`);
      expect(leaves.length).toBeGreaterThan(0);
      for (const t of leaves) {
        expect(t).not.toContain(EM_DASH);
        expect(t).not.toMatch(/\b[a-z0-9]+_[a-z0-9_]+\b/);
      }
    };
    const s = await openSettings();
    fireEvent.click(within(rowOf(s, "Max concurrent")).getByRole("button", { name: "SET" }));
    check(s);
    fireEvent.click(within(s).getByRole("button", { name: "CLOSE" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Settings" })).toBeNull());
    press("Versions 2");
    check(screen.getByRole("dialog", { name: "Versions" }));
  });

  it("s6 with no overridable setting there is no table, only a line", async () => {
    fetchSettings.mockResolvedValue([SETTINGS[1]]);
    const win = await openSettings();
    expect(win.querySelector("table")).toBeNull();
    expect(win.textContent).toContain("No setting can be set for one strategy");
    cleanup();
    fetchSettings.mockResolvedValue([]);
    const w2 = await openSettings();
    expect(w2.querySelector("table")).toBeNull();
    expect(w2.textContent).toContain("No setting can be set for one strategy");
  });
});
