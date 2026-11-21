import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import V2App from "../shell/V2App";
import { useLayoutMode } from "../shell/layout";
import { HoldButton, PhoneOff } from "../kit/phone";
import Settings from "../pages/Settings";
import { SettingsProvider, STORAGE_KEY, loadSettings } from "../settings";

const ctx = { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
const setLayout = (layout: string) => localStorage.setItem(STORAGE_KEY, JSON.stringify({ layout }));

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("phone layout", () => {
  it("p1 loadSettings keeps layout phone and turns an unknown value into auto", () => {
    setLayout("phone");
    expect(loadSettings().settings.layout).toBe("phone");
    setLayout("tablet");
    expect(loadSettings().settings.layout).toBe("auto");
  });

  it("p2 useLayoutMode follows the setting, then matchMedia, then desktop without it", () => {
    expect(renderHook(() => useLayoutMode("phone")).result.current).toBe("phone");
    expect(renderHook(() => useLayoutMode("desktop")).result.current).toBe("desktop");
    expect(renderHook(() => useLayoutMode("auto")).result.current).toBe("desktop");
    let listener: (() => void) | null = null;
    const mql = {
      matches: true,
      addEventListener: (_: string, f: () => void) => { listener = f; },
      removeEventListener: () => { listener = null; },
    };
    const mm = vi.fn(() => mql);
    vi.stubGlobal("matchMedia", mm);
    const { result } = renderHook(() => useLayoutMode("auto"));
    expect(result.current).toBe("phone");
    expect(mm).toHaveBeenCalledWith(
      "(pointer: coarse) and (max-width: 700px), (pointer: coarse) and (max-height: 500px)",
    );
    mql.matches = false;
    act(() => { listener?.(); });
    expect(result.current).toBe("desktop");
  });

  it("p3 on phone there is no rail and a bottom bar of five apps", () => {
    setLayout("phone");
    mount("/app/home/overview");
    expect(document.querySelector(".rail")).toBeNull();
    expect(document.querySelector(".v2.phone")).not.toBeNull();
    for (const n of ["Home", "Research", "Calendar", "Trading", "Portfolio"]) {
      expect(screen.getByRole("button", { name: n })).toBeTruthy();
    }
    expect(document.querySelectorAll(".tabbar button").length).toBe(5);
    fireEvent.click(screen.getByRole("button", { name: "Research" }));
    expect(where()).toBe("/app/research/overview");
  });

  it("p4 on phone the strip of pages comes before the title, and the gear sits on the title row; on desktop neither", () => {
    setLayout("phone");
    mount("/app/home/overview");
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("href")).toBe("/app/settings");
    const strip = document.querySelector(".subnav") as HTMLElement;
    expect(strip.textContent).toContain("Overview");
    expect(strip.compareDocumentPosition(document.querySelector(".ph h1") as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(strip.querySelector('[aria-current="page"]')?.textContent).toBe("Overview");
    fireEvent.click(screen.getByRole("button", { name: "Sports" }));
    expect(where()).toBe("/app/home/sports");
    cleanup();
    setLayout("desktop");
    mount("/app/home/overview");
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    expect(document.querySelector(".subnav")).toBeNull();
  });

  it("p4b pages that do not fit the strip become one button that lists them all in their groups", () => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("subnav") ? 900 : 0;
    });
    setLayout("phone");
    mount("/app/research/runs");
    const pick = document.querySelector(".subnav .pick") as HTMLElement;
    expect(pick.textContent).toBe("Runs");
    fireEvent.click(pick);
    const list = screen.getByRole("dialog", { name: "Research pages" });
    expect([...list.querySelectorAll(".pg-grp")].map((g) => g.textContent)).toEqual(["Data", "Build", "Test", "System"]);
    expect(list.querySelector('[aria-current="page"]')?.textContent).toContain("Runs");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: "Research pages" })).toBeNull();
    // A second press on the open app in the bottom bar opens the same list instead of leaving the page.
    fireEvent.click(document.querySelector(".tabbar .on") as HTMLElement);
    expect(where()).toBe("/app/research/runs");
    fireEvent.click(screen.getByRole("dialog", { name: "Research pages" }).querySelector("button:not(.btn)") as HTMLElement);
    expect(where()).toBe("/app/research/overview");
  });

  it("p5 PhoneOff hides on phone and shows on desktop, also alone", () => {
    setLayout("phone");
    render(<PhoneOff><span>laptop only</span></PhoneOff>);
    expect(screen.queryByText("laptop only")).toBeNull();
    cleanup();
    setLayout("desktop");
    render(<PhoneOff><span>laptop only</span></PhoneOff>);
    expect(screen.getByText("laptop only")).toBeTruthy();
  });

  it("p6 HoldButton fires only after the full hold", () => {
    vi.useFakeTimers();
    const onHold = vi.fn();
    render(<HoldButton label="KILL" onHold={onHold} />);
    const b = screen.getByRole("button", { name: "KILL" });
    fireEvent.pointerDown(b);
    act(() => { vi.advanceTimersByTime(1000); });
    fireEvent.pointerUp(b);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(onHold).not.toHaveBeenCalled();
    fireEvent.pointerDown(b);
    act(() => { vi.advanceTimersByTime(1500); });
    expect(onHold).toHaveBeenCalledTimes(1);
    fireEvent.pointerUp(b);
    cleanup();
    const off = vi.fn();
    render(<HoldButton label="STOP" onHold={off} disabled />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "STOP" }));
    act(() => { vi.advanceTimersByTime(3000); });
    expect(off).not.toHaveBeenCalled();
  });

  it("p7 Settings LAYOUT: Phone is saved", () => {
    render(
      <MemoryRouter initialEntries={["/app/settings"]}>
        <SettingsProvider><Settings /></SettingsProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Phone" }));
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) as string).layout).toBe("phone");
  });

  it("p8 Settings has no gear on the phone, every other page keeps it", () => {
    setLayout("phone");
    mount("/app/settings");
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    cleanup();
    mount("/app/trading/paper");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/app/settings");
  });
});
