import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DatePick, Gauge, HeatStrip, Ring, Scatter } from "../kit";

afterEach(cleanup);

describe("Ring", () => {
  const rows = [
    { key: "a", value: 2, colour: "#111111", title: "A: 2" },
    { key: "b", value: 1, colour: "#222222", title: "B: 1" },
    { key: "c", value: 1, colour: "#333333", title: "C: 1" },
  ];
  it("(c1) draws one arc per row, sized by share, dimming the others", () => {
    const onPick = vi.fn();
    const { container, rerender } = render(<Ring label="Things" rows={rows} total={4} active={null} onPick={onPick} />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveClass("kit-ring");
    expect(svg).toHaveAttribute("aria-label", "Things");
    const arc = (k: string) => container.querySelector(`[data-arc="${k}"]`)!;
    expect(container.querySelectorAll("circle")).toHaveLength(3);
    const half = (2 * Math.PI * 60.2) / 2;
    expect(Math.abs(parseFloat(arc("a").getAttribute("stroke-dasharray")!.split(" ")[0]) - half)).toBeLessThan(0.01);
    expect(Math.abs(parseFloat(arc("b").getAttribute("stroke-dashoffset")!) + half)).toBeLessThan(0.01);
    rerender(<Ring label="Things" rows={rows} total={4} active="b" onPick={onPick} />);
    expect(arc("b")).toHaveAttribute("opacity", "1");
    expect(arc("a")).toHaveAttribute("opacity", "0.35");
    expect(arc("c")).toHaveAttribute("opacity", "0.35");
    fireEvent.click(arc("c"));
    expect(onPick).toHaveBeenCalledWith("c");
  });
});

describe("Gauge", () => {
  it("(c2) draws 96 arcs and the text", () => {
    const { container } = render(
      <Gauge id="t" value={5} min={0} max={10} bands={[[0, 10, "#fff"]]} text="five" />,
    );
    const g = container.querySelector('[data-gauge="t"]')!;
    expect(g.querySelectorAll("path")).toHaveLength(96);
    expect(g).toHaveTextContent("five");
  });
});

describe("Scatter", () => {
  it("(c3) draws points, the zone, the axes and picks on click", () => {
    const onPick = vi.fn();
    const { container } = render(
      <Scatter
        testid="sc" label="Every thing"
        x={{ lo: 0, hi: 1, ticks: [0, 1], text: (v) => `X${v}`, name: "xname" }}
        y={{ lo: 0, hi: 1, ticks: [0, 0.5, 1], text: (v) => `Y${v}`, name: "yname" }}
        zone={{ x0: 0, x1: 0.5, y0: 0.5, y1: 1, colour: "#0f0", title: "the zone" }}
        points={[
          { id: 7, x: 0.2, y: 0.4, r: 4, colour: "#f00", title: "seven" },
          { id: 8, x: 0.6, y: 0.9, r: 5, colour: "#00f", title: "eight" },
        ]}
        picked={7} pointAttr="data-run" onPick={onPick}
      />,
    );
    const wrap = screen.getByTestId("sc");
    expect(wrap).toHaveClass("scat");
    expect(container.querySelector('[data-run="7"]')!.querySelectorAll("circle")).toHaveLength(3);
    expect(container.querySelector('[data-run="8"]')!.querySelectorAll("circle")).toHaveLength(2);
    expect(container.querySelector('[data-run="7"] [data-dot]')).toHaveAttribute("r", "4");
    expect(container.querySelector('[data-run="8"] [data-dot]')).toHaveAttribute("r", "5");
    fireEvent.click(container.querySelector('[data-run="8"]')!);
    expect(onPick).toHaveBeenCalledWith(8);
    const zone = within(wrap).getByText("the zone").parentElement!;
    expect(zone.querySelector("rect")).not.toBeNull();
    for (const t of ["xname", "yname", "X0", "X1", "Y0", "Y0.5", "Y1"]) expect(within(wrap).getByText(t)).toBeInTheDocument();
  });
});

describe("Scatter at its box's width", () => {
  it("(c3b) draws at the measured width so its words keep their size; 700 wide when nothing is measured", () => {
    const axis = { lo: 0, hi: 1, ticks: [0, 1], text: (v: number) => String(v), name: "n" };
    const spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, right: 390, bottom: 300, x: 0, y: 0, width: 390, height: 300, toJSON: () => ({}) } as DOMRect);
    const { container, unmount } = render(<Scatter testid="n" label="n" x={axis} y={axis} points={[]} picked={null} pointAttr="data-p" onPick={() => {}} />);
    expect(container.querySelector("svg")).toHaveAttribute("viewBox", "0 0 390 280");
    unmount();
    spy.mockReturnValue({ left: 0, top: 0, right: 0, bottom: 0, x: 0, y: 0, width: 0, height: 0, toJSON: () => ({}) } as DOMRect);
    const again = render(<Scatter testid="m" label="m" x={axis} y={axis} points={[]} picked={null} pointAttr="data-p" onPick={() => {}} />);
    expect(again.container.querySelector("svg")).toHaveAttribute("viewBox", "0 0 700 340");
    spy.mockRestore();
  });
});

describe("HeatStrip", () => {
  it("(c4) draws a cell per entry with the first and last label, or a dash with its reason", () => {
    const cells = [1, 2, 3].map((k) => ({ key: k, title: `t${k}`, opacity: k / 4 }));
    const { container, rerender } = render(<HeatStrip cells={cells} colour="#abcdef" first={2004} last={2026} why="no" />);
    const is = container.querySelectorAll(".ds-cov i");
    expect(is).toHaveLength(3);
    expect(is[1]).toHaveAttribute("title", "t2");
    expect((is[1] as HTMLElement).style.opacity).toBe("0.5");
    const l = container.querySelector(".ds-covl")!;
    expect(l.children[0]).toHaveTextContent("2004");
    expect(l.children[1]).toHaveTextContent("2026");
    rerender(<HeatStrip cells={null} colour="#abcdef" first={2004} last={2026} why="nothing held" />);
    const none = container.querySelector(".ds-covnone")!;
    expect(none).toHaveTextContent("-");
    expect(none.querySelector("span")).toHaveAttribute("title", "nothing held");
  });
});

describe("DatePick year buttons", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
  });
  afterEach(() => vi.useRealTimers());
  it("(c5) a year button moves the view 12 months", () => {
    const f = vi.fn();
    render(<DatePick value="2026-10-08" onChange={f} ariaLabel="Date" />);
    fireEvent.click(screen.getByRole("button", { name: "Date" }));
    fireEvent.click(screen.getByRole("button", { name: "Previous year" }));
    expect(screen.getByText("October 2025")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next year" }));
    fireEvent.click(screen.getByRole("button", { name: "Next year" }));
    expect(screen.getByText("October 2027")).toBeInTheDocument();
    const d = Array.from(document.querySelectorAll<HTMLButtonElement>(".dpg button")).find((b) => b.textContent === "15" && !b.classList.contains("out"))!;
    fireEvent.click(d);
    expect(f).toHaveBeenCalledWith("2027-10-15");
  });
});

describe("source scan", () => {
  const files = import.meta.glob<string>("../pages/**/*.{tsx,css}", { query: "?raw", import: "default", eager: true });
  const read = (p: string) => {
    const hit = files[`../${p}`];
    if (hit === undefined) throw new Error(`no such source file: ${p}`);
    return hit;
  };
  it("(c6) the pages use the kit parts and the old copies are gone", () => {
    expect(read("pages/research/events/EventsTab.tsx")).not.toContain("function Donut");
    expect(read("pages/research/Runs.tsx")).not.toContain("<svg");
    const ds = read("pages/research/datasets/DatasetsTab.tsx");
    expect(ds).not.toContain("ds-cov");
    expect(ds).not.toContain("ds-confirm");
    expect(Object.keys(files)).not.toContain("../pages/home/sports/gauge.tsx");
    const kitImport = (src: string, name: string) =>
      new RegExp(`import\\s*\\{[^}]*\\b${name}\\b[^}]*\\}\\s*from\\s*"(\\.\\./)+kit"`).test(src);
    expect(read("pages/research/events/EventsTab.tsx")).toContain("ev-kbar");
    expect(kitImport(read("pages/home/sports/panels.tsx"), "Gauge")).toBe(true);
    expect(read("pages/research/Runs.tsx")).toContain("./runs/RunScatter");
    expect(kitImport(ds, "Block")).toBe(true);
    for (const [css, words] of [
      ["pages/research/events/events.css", ["ev-donut"]],
      ["pages/research/runs.css", ["rs-sc"]],
      ["pages/research/datasets/datasets.css", ["ds-cov", "ds-confirm"]],
    ] as Array<[string, string[]]>) {
      for (const w of words) expect(read(css), `${css} ${w}`).not.toContain(w);
    }
  });
});
