import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BurnColumns, Stair, StepRing } from "../kit";
import { stairYs } from "../kit/EveningCharts";

// SYNTHETIC figures: the mockup's own example days.
const DAYS = [2512, 2578, 2424, 3038, 2376, 2545, 2309, 2620].map((k, i) => ({ d: `2026-11-${String(7 + i).padStart(2, "0")}`, rest: 1878, move: k - 1878 }));

// jsdom cannot draw on a canvas: this stand-in takes every drawing call and ignores it.
const pen: unknown = new Proxy(function () {}, { get: () => pen, set: () => true, apply: () => pen });
function stubCanvas() { vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((() => pen) as never); }

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("BurnColumns", () => {
  it("draws one column a day, the last marked as the day, and an average line only when one is given", () => {
    const { container, rerender } = render(<BurnColumns days={DAYS} avg={2540.29} />);
    const cols = Array.from(container.querySelectorAll("[data-col]"));
    expect(cols.length).toBe(8);
    expect(cols.filter((c) => c.hasAttribute("data-today"))).toEqual([cols[7]]);
    expect(container.querySelector("[data-avg]")).not.toBeNull();
    rerender(<BurnColumns days={DAYS} avg={null} />);
    expect(container.querySelector("[data-avg]")).toBeNull();
  });
});

describe("StepRing", () => {
  it("writes the share of the goal and the words under it", () => {
    render(<StepRing steps={11460} goal={10000} />);
    expect(screen.getByText("115%")).toBeTruthy();
    expect(screen.getByText("of your goal")).toBeTruthy();
  });
});

describe("Stair", () => {
  it("draws one chart for the month's changes and a total, and nothing for no changes", () => {
    stubCanvas();
    const { container, rerender } = render(<Stair days={[0.42, 1.1, 0, 0.65]} total={10.17} />);
    expect(container.querySelectorAll("canvas").length).toBe(1);
    expect(() => rerender(<Stair days={[]} total={10.17} />)).not.toThrow();
    expect(container.querySelector("canvas")).toBeNull();
  });
});

describe("stairYs", () => {
  it("a normal month starts at H - 4 and ends at 8", () => {
    const ys = stairYs([0.42, 1.1, 0, 0.65], 10.17, 46);
    expect(ys[0]).toBe(42);
    expect(ys[ys.length - 1]).toBe(8);
  });
  it("a month summing below zero, and a flat month, stay inside 8 to H - 4", () => {
    for (const [days, total] of [[[-15.74, 4.43, 2.58], 18.19], [[0, 0], 5]] as Array<[number[], number]>) {
      const ys = stairYs(days, total, 46);
      expect(ys.length).toBeGreaterThan(0);
      for (const y of ys) { expect(y).toBeGreaterThanOrEqual(8); expect(y).toBeLessThanOrEqual(42); }
    }
  });
});
