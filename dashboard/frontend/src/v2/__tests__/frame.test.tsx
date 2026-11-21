import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import V2App from "../shell/V2App";
import { __resetRefreshRegistryForTests, registerSync } from "../../lib/refresh-registry";

const ctx = { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };

// A minimal EventSource stub: records construction, listeners and close, and lets a test fire frames.
class FakeEventSource {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  url: string;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  listeners = new Map<string, Array<(e: unknown) => void>>();
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, fn: (e: unknown) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn]);
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  fireOpen() {
    this.readyState = FakeEventSource.OPEN;
    act(() => this.onopen?.());
  }
  fireError() {
    this.readyState = FakeEventSource.CONNECTING;
    act(() => this.onerror?.());
  }
  emit(name: string) {
    act(() => (this.listeners.get(name) ?? []).forEach((fn) => fn({ data: "{}" })));
  }
}

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

let qc: QueryClient;
function mount(path = "/app/home/overview") {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <V2App />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const where = () => screen.getByTestId("where").textContent;
const es = () => FakeEventSource.instances[0];
const ctrlK = (target: Element | Document = document) => fireEvent.keyDown(target, { key: "k", ctrlKey: true });
const dialog = () => screen.getByRole("dialog", { name: "Command palette" });

const NAMES = [
  "calendar_synced", "todo_added", "todo_toggled", "todo_updated", "todo_deleted", "todos_reordered", "garmin_synced", "garmin_activities_synced",
  "paper.run_killed", "paper.run_promoted", "paper.run_admitted",
  "research.run_created", "research.run_started", "research.run_completed", "research.run_failed", "research.run_cancelled",
  "research.dataset_created", "research.dataset_status_changed",
  "research.strategy_registered", "research.strategy_deleted", "research.chain_changed",
];

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  __resetRefreshRegistryForTests();
});

describe("live feed", () => {
  it("l1: one EventSource for /api/stream with exactly the mapped names", () => {
    mount();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(es().url).toBe("/api/stream");
    expect([...es().listeners.keys()].sort()).toEqual([...NAMES].sort());
  });

  it("l2: garmin_synced invalidates the garmin keys and leaves the calendar range alone", () => {
    mount();
    qc.setQueryData(["garmin-today"], 1);
    qc.setQueryData(["garmin-days", 7], 1);
    qc.setQueryData(["calendar", "range"], 1);
    es().emit("garmin_synced");
    expect(qc.getQueryState(["garmin-today"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["garmin-days", 7])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["calendar", "range"])?.isInvalidated).toBe(false);
  });

  it("l3: paper.run_admitted invalidates the paper, overview and judge keys", () => {
    mount();
    const keys = [["paper", "runs"], ["overview", "paper-runs"], ["judge", "summary"]];
    keys.forEach((k) => qc.setQueryData(k, 1));
    es().emit("paper.run_admitted");
    keys.forEach((k) => expect(qc.getQueryState(k)?.isInvalidated).toBe(true));
  });

  it("l4: without EventSource the shell renders and the palette says OFF", () => {
    vi.stubGlobal("EventSource", undefined);
    mount();
    expect(screen.getByRole("heading", { name: "Home" })).toBeTruthy();
    ctrlK();
    expect(dialog().textContent).toContain("OFF");
  });

  it("l5: unmounting closes the source", () => {
    const view = mount();
    view.unmount();
    expect(es().closed).toBe(true);
  });

  it("l6: a first open invalidates nothing, open after an error invalidates every query once", () => {
    mount();
    const spy = vi.spyOn(qc, "invalidateQueries");
    es().fireOpen();
    expect(spy).not.toHaveBeenCalled();
    es().fireError();
    es().fireOpen();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith();
  });
});

describe("palette", () => {
  it("p1: Ctrl+K opens, Esc closes, Ctrl+K inside an input opens", () => {
    mount();
    expect(screen.queryByRole("dialog")).toBeNull();
    ctrlK();
    expect(dialog()).toBeTruthy();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    ctrlK(input);
    expect(dialog()).toBeTruthy();
    input.remove();
  });

  it("p2: typing runs leaves one Go to command and Enter navigates", () => {
    mount();
    ctrlK();
    const box = screen.getByRole("textbox", { name: "Search commands" });
    fireEvent.change(box, { target: { value: "runs" } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Go to Research / Runs"]);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(where()).toBe("/app/research/runs");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("p3: Refresh everything names a failed sync and refetches all queries", async () => {
    registerSync("Bad sync", () => Promise.reject(new Error("no")));
    mount();
    const spy = vi.spyOn(qc, "invalidateQueries");
    ctrlK();
    fireEvent.click(screen.getByRole("option", { name: "Refresh everything" }));
    await waitFor(() => expect(dialog().textContent).toContain("Could not refresh: Bad sync"));
    expect(spy).toHaveBeenCalledWith({ refetchType: "all" });
  });

  it("p4: the palette shows LIVE once the source is open", () => {
    mount();
    es().fireOpen();
    ctrlK();
    expect(dialog().textContent).toContain("LIVE");
  });

  it("p5: arrow keys change page while the palette is closed and do nothing while it is open", () => {
    mount();
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(where()).toBe("/app/research/overview");
    ctrlK();
    fireEvent.keyDown(document, { key: "ArrowDown" });
    expect(where()).toBe("/app/research/overview");
  });
});
