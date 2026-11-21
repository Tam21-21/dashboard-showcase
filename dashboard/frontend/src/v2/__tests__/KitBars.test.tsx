import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DayBars, Legend, ShareBar, ZeroBar } from "../kit";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const near = (el: Element | null, name: string, want: number) =>
  expect(Math.abs(parseFloat(el!.getAttribute(name)!) - want)).toBeLessThan(0.01);

describe("DayBars", () => {
  it("(b1) card size draws stacked bars with data marks and hover titles", () => {
    const { container } = render(
      <DayBars
        size="card"
        title="T"
        bars={[
          { parts: [{ value: 2, colour: "#c3cbd6", name: "ran" }, { value: 1, colour: "#ff5a63", name: "failed" }], hover: "h0" },
          { parts: [{ value: 3, colour: "#c3cbd6", name: "ran" }, { value: 0, colour: "#ff5a63", name: "failed" }], hover: "h1", unread: true },
        ]}
      />,
    );
    const svg = container.querySelector("div.kz > svg")!;
    expect(svg).toHaveAttribute("viewBox", "0 0 260 36");
    expect(svg.firstElementChild!.tagName).toBe("title");
    expect(svg.firstElementChild!.textContent).toBe("T");
    const bars = container.querySelectorAll("[data-bar]");
    expect(bars).toHaveLength(2);
    expect(bars[0]).not.toHaveAttribute("data-unread");
    expect(bars[1]).toHaveAttribute("data-unread", "1");
    const ran0 = bars[0].querySelector('[data-part="ran"]');
    const fail0 = bars[0].querySelector('[data-part="failed"]');
    near(ran0, "height", 18.67);
    near(ran0, "y", 13.33);
    near(fail0, "height", 9.33);
    near(fail0, "y", 4);
    expect(ran0).toHaveAttribute("opacity", "0.55");
    const ran1 = bars[1].querySelector('[data-part="ran"]')!;
    expect(ran1).toHaveAttribute("opacity", "1");
    expect(bars[1].querySelector('[data-part="failed"]')).toBeNull();
    const titles = [...container.querySelectorAll("g[data-bar] > rect > title")].map((t) => t.textContent);
    expect(titles).toEqual(["h0", "h1"]);
  });

  it("(b2) tile size measures its box and draws one rect per value", () => {
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0, top: 0, right: 100, bottom: 44, x: 0, y: 0, width: 100, height: 44, toJSON: () => ({}),
    } as DOMRect);
    const { container } = render(
      <DayBars size="tile" opacity={0.8} bars={[4, 0, 1].map((value) => ({ parts: [{ value, colour: "#e8d49a" }] }))} />,
    );
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("width", "100");
    expect(svg).toHaveAttribute("height", "44");
    const rects = container.querySelectorAll("rect");
    expect(rects).toHaveLength(3);
    [38, 0, 9.5].forEach((h, i) => near(rects[i], "height", h));
    [5, 38.33, 71.67].forEach((x, i) => near(rects[i], "x", x));
    rects.forEach((r) => {
      near(r, "width", 23.33);
      expect(r).toHaveAttribute("fill", "#e8d49a");
      expect(r).toHaveAttribute("opacity", "0.8");
    });
  });
});

describe("ShareBar", () => {
  it("(b3) draws a rect per non-empty part with its title", () => {
    const { container } = render(
      <ShareBar parts={[[3, "#aaa", "Finished"], [1, "#f00", "Failed"], [0, "#00f", "Running"]]} />,
    );
    const rects = container.querySelectorAll("rect[data-share]");
    expect(rects).toHaveLength(2);
    near(rects[0], "x", 0);
    near(rects[0], "width", 193);
    near(rects[1], "x", 195);
    near(rects[1], "width", 63);
    expect(rects[0].querySelector("title")!.textContent).toBe("Finished: 3");
    expect(rects[1].querySelector("title")!.textContent).toBe("Failed: 1");
  });
});

describe("ZeroBar", () => {
  it("(b4) draws a bar from the middle, or nothing", () => {
    const pos = render(<ZeroBar value={0.02} widest={0.04} />).container;
    expect((pos.querySelector("span.dbar > b.pos") as HTMLElement).style.width).toBe("25%");
    const neg = render(<ZeroBar value={-0.04} widest={0.04} />).container;
    expect((neg.querySelector("span.dbar > b.neg") as HTMLElement).style.width).toBe("50%");
    const none = render(<ZeroBar value={null} widest={0.04} />).container;
    expect(none.querySelector("span.dbar")!.children).toHaveLength(0);
    const zero = render(<ZeroBar value={0.01} widest={0} />).container;
    expect(zero.querySelector("span.dbar")!.children).toHaveLength(0);
  });
});

describe("Legend", () => {
  it("(b5) names each item with its colour", () => {
    const { container } = render(<Legend items={[["Run", "rgb(1, 2, 3)"], ["Ride", "rgb(4, 5, 6)"]]} />);
    const spans = container.querySelectorAll("div.lgd > span");
    expect(spans).toHaveLength(2);
    expect(spans[0].textContent).toBe("Run");
    expect(spans[1].textContent).toBe("Ride");
    expect((spans[0].querySelector("i") as HTMLElement).style.background).toBe("rgb(1, 2, 3)");
    expect((spans[1].querySelector("i") as HTMLElement).style.background).toBe("rgb(4, 5, 6)");
  });
});

describe("source scan", () => {
  const files = import.meta.glob("../pages/**/*.{tsx,css}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
  const read = (p: string) => {
    const src = files[`../${p}`];
    if (src === undefined) throw new Error(`source file not found: ${p}`);
    return src;
  };
  it("(b6) the old copies are gone and the pages import from the kit", () => {
    const sc = read("pages/home/sports/charts.tsx");
    expect(sc).not.toContain("function MiniBars");
    expect(sc).not.toContain("function Legend");
    expect(read("pages/research/markets/Sectors.tsx")).not.toContain("dbar");
    expect(read("pages/research/markets.css")).not.toContain(".dbar");
    expect(read("pages/home/sports.css")).not.toContain("spleg");
    const fromKit = (src: string, name: string) =>
      new RegExp(`import\\s*\\{[^}]*\\b${name}\\b[^}]*\\}\\s*from\\s*"(\\.\\./)+kit"`).test(src);
    expect(fromKit(read("pages/home/sports/tiles.tsx"), "DayBars")).toBe(true);
    expect(fromKit(read("pages/home/sports/activity.tsx"), "Legend")).toBe(true);
    expect(fromKit(read("pages/home/sports/panels.tsx"), "Legend")).toBe(true);
    expect(fromKit(read("pages/research/markets/Sectors.tsx"), "ZeroBar")).toBe(true);
  });
});
