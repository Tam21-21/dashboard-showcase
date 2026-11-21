import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LineChart } from "../kit";

const pts = (f: (i: number) => number) => Array.from({ length: 101 }, (_, i) => f(i));
const SERIES = [
  { v: pts((i) => 100 + i), c: "#2fe08a", sw: 2 },
  { v: pts((i) => 100 + i / 2), c: "#c9d2de", dash: "4 4" },
];
const rows = (i: number): Array<string | [string, string, string?]> => [`Day ${i}`, ["Account", `$${i}`]];

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LineChart", () => {
  it("draws two paths for two series at the measured size", () => {
    const { container } = render(<LineChart series={SERIES} rows={rows} yfmt={(v) => `$${v}`} />);
    const svg = container.querySelector("svg") as SVGElement;
    expect(svg.getAttribute("width")).toBe("600");
    expect(svg.getAttribute("height")).toBe("300");
    expect(container.querySelectorAll("path.ln")).toHaveLength(2);
  });

  it("shows the hover box with the row text of the mockup's index and a line, no dots", () => {
    const { container } = render(<LineChart series={SERIES} rows={rows} yfmt={(v) => `$${v}`} />);
    const box = container.querySelector(".lcbox") as HTMLElement;
    expect(screen.queryByText("Day 46")).toBeNull();
    fireEvent.mouseMove(box, { clientX: 300 });
    expect(screen.getByText("Day 46")).toBeInTheDocument();
    expect(screen.getByText("$46")).toBeInTheDocument();
    expect(container.querySelectorAll("circle.hvc")).toHaveLength(0);
    expect(container.querySelector("line.hvl")).not.toBeNull();
  });

  it("hides the hover on mouseleave", () => {
    const { container } = render(<LineChart series={SERIES} rows={rows} yfmt={(v) => `$${v}`} />);
    const box = container.querySelector(".lcbox") as HTMLElement;
    fireEvent.mouseMove(box, { clientX: 300 });
    fireEvent.mouseLeave(box);
    expect(screen.queryByText("Day 46")).toBeNull();
    expect(container.querySelectorAll("circle.hvc")).toHaveLength(0);
  });

  it("shows No data for an empty series array", () => {
    render(<LineChart series={[]} rows={rows} yfmt={(v) => `${v}`} />);
    expect(screen.getByText("No data")).toBeInTheDocument();
  });
});
