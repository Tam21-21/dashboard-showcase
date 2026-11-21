import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RefreshFinary from "../pages/portfolio/RefreshFinary";
import { ApiError } from "../../lib/api";

const getOverview = vi.fn();
const postRefresh = vi.fn();
vi.mock("../data/moneyApi", () => ({
  getOverview: () => getOverview(),
  postRefresh: () => postRefresh(),
}));

const FIRST = "2026-11-18T06:08:00Z";
const NEWER = "2026-11-18T14:20:00Z";
const RUN = "https://claude.ai/code/scheduled/run1";
const overview = (read_at: string) => ({ synced: true, read_at });
const TITLE = "Reads what Finary holds now. An account Finary has not synced stays as it is";

let qc: QueryClient;
const tree = (key: string) => <QueryClientProvider client={qc}><RefreshFinary key={key} /></QueryClientProvider>;
const btn = () => screen.getByRole("button") as HTMLButtonElement;
const step = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

// Fake timers from the start: the overview loads and the button is read after a step.
async function mount() {
  const r = render(tree("a"));
  await step(0);
  return r;
}

beforeEach(() => {
  vi.useFakeTimers();
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  getOverview.mockResolvedValue(overview(FIRST));
  postRefresh.mockResolvedValue({ asked_at: "2026-11-18T14:00:00Z", run_url: RUN });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  getOverview.mockReset();
  postRefresh.mockReset();
});

describe("RefreshFinary", () => {
  it("(r1) reads Refresh and is enabled once the overview is in, with its hover", async () => {
    getOverview.mockReturnValue(new Promise(() => {}));
    render(tree("a"));
    await step(0);
    expect(btn()).toBeDisabled();
    cleanup();
    getOverview.mockResolvedValue(overview(FIRST));
    qc.clear();
    await mount();
    expect(btn()).toHaveTextContent("Refresh");
    expect(btn()).toBeEnabled();
    expect(btn()).toHaveAttribute("title", TITLE);
  });

  it("(r2) a press waits, polls every 10 seconds and says when a newer read lands", async () => {
    await mount();
    const calls = getOverview.mock.calls.length;
    await act(async () => { btn().click(); });
    expect(postRefresh).toHaveBeenCalledTimes(1);
    expect(btn()).toHaveTextContent("Reading Finary");
    expect(btn()).toBeDisabled();
    getOverview.mockResolvedValue(overview(NEWER));
    await step(10000);
    await step(1); // react-query hands its result to the page on a zero-delay timer
    expect(getOverview.mock.calls.length).toBeGreaterThan(calls);
    expect(btn()).toHaveTextContent("Refresh");
    expect(screen.getByRole("status")).toHaveTextContent("Finary was read again.");
  });

  it("(r3) a refusal shows the server's words and does not poll", async () => {
    postRefresh.mockRejectedValue(new ApiError("503", 503, { detail: "The Finary refresh is not set up on the server yet." }, "/api/money/refresh"));
    await mount();
    const calls = getOverview.mock.calls.length;
    await act(async () => { btn().click(); });
    expect(screen.getByRole("status").textContent).toBe("The Finary refresh is not set up on the server yet.");
    expect(btn()).toHaveTextContent("Refresh");
    await step(30000);
    expect(getOverview.mock.calls.length).toBe(calls);
  });

  it("(r4) no new read for 5 minutes says so, links the run and stops polling", async () => {
    await mount();
    await act(async () => { btn().click(); });
    for (let i = 0; i < 30; i++) await step(10000);
    expect(screen.getByRole("status")).toHaveTextContent("No new read after 5 minutes.");
    const link = screen.getByRole("link", { name: "Open the run" });
    expect(link).toHaveAttribute("href", RUN);
    expect(link).toHaveAttribute("target", "_blank");
    expect(btn()).toHaveTextContent("Refresh");
    const calls = getOverview.mock.calls.length;
    await step(30000);
    expect(getOverview.mock.calls.length).toBe(calls);
  });

  it("(r5) a second button on the same client also waits", async () => {
    await mount();
    await act(async () => { btn().click(); });
    const second = render(tree("b"));
    await step(0);
    const buttons = screen.getAllByRole("button") as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    for (const b of buttons) {
      expect(b).toHaveTextContent("Reading Finary");
      expect(b).toBeDisabled();
    }
    second.unmount();
  });

  it("(r6) the status closes by itself after 6 seconds", async () => {
    postRefresh.mockRejectedValue(new ApiError("503", 503, { detail: "Not set up." }, "/api/money/refresh"));
    await mount();
    await act(async () => { btn().click(); });
    expect(screen.getByRole("status")).toBeInTheDocument();
    await step(6000);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("(r7) an overview that fails on every poll still ends the wait at 5 minutes", async () => {
    await mount();
    await act(async () => { btn().click(); });
    getOverview.mockRejectedValue(new Error("down"));
    for (let i = 0; i < 30; i++) await step(10000);
    expect(screen.getByRole("status")).toHaveTextContent("No new read after 5 minutes.");
    expect(btn()).toHaveTextContent("Refresh");
    const calls = getOverview.mock.calls.length;
    await step(30000);
    expect(getOverview.mock.calls.length).toBe(calls);
  });
});
