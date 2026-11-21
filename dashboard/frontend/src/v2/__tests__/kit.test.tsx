import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  Cols, Facts, Kpis, KzSeg, KzSteps, PageHeader, Panel, Rows, Segmented, StateWord, Table, Track, isNum,
} from "../kit";
import { AMBER, GREEN, RED } from "../kit/parts";

describe("isNum", () => {
  it("is true for values with a digit and only unit words", () => {
    for (const v of ["+5.7%", "$105,708", "9 of 9", "37.4 km", "12 min"]) {
      expect(isNum(v), v).toBe(true);
    }
  });
  it("is false for words", () => {
    for (const v of ["S&P 500 fund", "WORKING", "Liquidity gap"]) {
      expect(isNum(v), v).toBe(false);
    }
  });
});

describe("Table", () => {
  it("puts num on a numeric cell but never on the first column", () => {
    const { container } = render(
      <Table head={["Name", "Value"]} rows={[["2026 plan", "$1,200"], ["Fund", "WORKING"]]} />,
    );
    const rows = container.querySelectorAll("tr");
    const first = rows[1].querySelectorAll("td");
    expect(first[0]).not.toHaveClass("num");
    expect(first[1]).toHaveClass("num");
    expect(rows[2].querySelectorAll("td")[1]).not.toHaveClass("num");
  });
  it("adds the cell's own class", () => {
    const { container } = render(<Table head={["A", "B"]} rows={[["x", { v: "+3.0%", cls: "g" }]]} />);
    const td = container.querySelectorAll("td")[1];
    expect(td).toHaveClass("g");
    expect(td).toHaveClass("num");
  });
});

describe("Kpis", () => {
  const items = [
    { label: "ACCOUNT", value: "$105,708", pill: "+5.7%", pillTone: "g" as const },
    { label: "DAY", value: "+$312", tone: "g" as const },
    { label: "STATE", value: "WORKING", pill: "9 of 9" },
    { label: "BELOW HIGH", value: "-2.1%", tone: "rd" as const, pill: "-$2,300" },
  ];
  const card = (c: HTMLElement) => c.querySelector(".kp .cd") as HTMLElement;
  it("renders four cards with label, value and pill", () => {
    const { container } = render(<Kpis items={items} />);
    expect(container.querySelectorAll(".kp > .cd")).toHaveLength(4);
    for (const label of ["Account", "Day", "State", "Below high"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    for (const it of items) {
      expect(screen.getByText(it.value)).toBeInTheDocument();
      if (it.pill) expect(screen.getByText(it.pill)).toBeInTheDocument();
    }
  });
  it("puts num on a numeric value only", () => {
    const { container } = render(<Kpis items={items} />);
    expect(screen.getByText("$105,708")).toHaveClass("num");
    expect(screen.getByText("WORKING")).not.toHaveClass("num");
    expect(screen.getByText("+$312")).toHaveClass("num");
    expect(screen.getByText("+$312")).not.toHaveClass("g");
    expect(container.querySelectorAll(".kp .cd")[1].querySelector(".mch")?.textContent).toBe("");
  });
  it("t1 writes the label in plain case", () => {
    render(
      <Kpis items={["NEEDS YOU", "BEST SHARPE", "S&P 500, TODAY", "VIX"].map((label) => ({ label, value: "1" }))} />,
    );
    for (const t of ["Needs you", "Best Sharpe", "S&P 500, today", "VIX"]) expect(screen.getByText(t)).toBeInTheDocument();
  });
  it("t2 colours the line, never the value", () => {
    const { container, rerender } = render(<Kpis items={[{ label: "A", value: "+5.0%", tone: "g", pill: "+$312" }]} />);
    expect(container.querySelector(".kv span")).not.toHaveClass("g");
    expect(container.querySelector(".mch .pill")).toHaveClass("g");
    rerender(<Kpis items={[{ label: "A", value: "1", tone: "g", pill: "x", pillTone: "rd" }]} />);
    expect(container.querySelector(".mch .pill")).toHaveClass("rd");
  });
  it("t3 shows a state word on the right only when given", () => {
    const { container, rerender } = render(<Kpis items={[{ label: "A", value: "1", state: "WORKING", stateTone: "g" }]} />);
    expect(container.querySelector(".mch .state.g")?.textContent).toBe("WORKING");
    rerender(<Kpis items={[{ label: "A", value: "1" }]} />);
    expect(container.querySelector(".state")).toBeNull();
  });
  it("t4 viz replaces the spark", () => {
    const { container } = render(
      <Kpis items={[{ label: "A", value: "1", viz: <i data-testid="v" />, spark: <b /> }]} />,
    );
    expect(container.querySelector(".kviz [data-testid='v']")).not.toBeNull();
    expect(container.querySelector(".sp")).toBeNull();
  });
  it("t5 foot is label and value spans", () => {
    const { container } = render(
      <Kpis items={[{ label: "A", value: "1", foot: [["Best", "1.42", "g"], ["Runs", "12"]] }]} />,
    );
    const spans = container.querySelectorAll(".mft > span");
    expect(spans).toHaveLength(2);
    expect(spans[0].querySelector("b")).toHaveClass("g");
    expect(spans[0].textContent).toBe("Best 1.42");
  });
  it("t6 every card is a ktl tile", () => {
    const { container } = render(<Kpis items={items} />);
    expect(card(container).classList.contains("ktl")).toBe(true);
    expect(container.querySelectorAll(".kp .cd.ktl")).toHaveLength(4);
  });
});

describe("KzSeg and KzSteps", () => {
  it("t7 KzSeg draws parts in ratio with hovers and a label when wide", () => {
    const { container } = render(<KzSeg parts={[[3, GREEN, "passed"], [1, RED, "failed"]]} />);
    const r = Array.from(container.querySelectorAll("rect"));
    expect(r).toHaveLength(2);
    expect(r.every((x) => x.getAttribute("height") === "6")).toBe(true);
    const w = r.map((x) => Number(x.getAttribute("width")));
    expect(Math.abs((w[0] + 2) / (w[1] + 2) - 3)).toBeLessThan(2 / 60);
    expect(Array.from(container.querySelectorAll("title")).map((t) => t.textContent)).toEqual(["passed: 3", "failed: 1"]);
    expect(container.textContent).toContain("PASSED");
    expect(container.querySelector("svg")?.getAttribute("viewBox")).toBe("0 0 260 36");
  });
  it("t7 KzSeg skips a zero part", () => {
    const { container } = render(<KzSeg parts={[[0, GREEN, "x"], [2, RED, "y"]]} />);
    expect(container.querySelectorAll("rect")).toHaveLength(1);
  });
  it("t8 KzSteps counts the green steps", () => {
    const items: Array<[string, string]> = [["Data", GREEN], ["Search", GREEN], ["Judge", AMBER]];
    const { container, rerender } = render(<KzSteps items={items} />);
    expect(container.querySelectorAll("rect")).toHaveLength(3);
    expect(container.textContent).toContain("2 OF 3 READY");
    rerender(<KzSteps items={items} word="DONE" />);
    expect(container.textContent).toContain("2 OF 3 DONE");
  });
});

describe("Track", () => {
  it("uses green, amber and red by fraction", () => {
    const { container, rerender } = render(<Track fraction={0.5} />);
    const bar = () => container.querySelector(".track b") as HTMLElement;
    expect(bar()).toHaveStyle({ backgroundColor: "#2fe08a" });
    rerender(<Track fraction={0.7} />);
    expect(bar()).toHaveStyle({ backgroundColor: "#ffb020" });
    rerender(<Track fraction={0.9} />);
    expect(bar()).toHaveStyle({ backgroundColor: "#ff5a63" });
  });
  it("keeps a visible sliver at zero and caps at 100 percent", () => {
    const { container, rerender } = render(<Track fraction={0} />);
    const bar = () => container.querySelector(".track b") as HTMLElement;
    expect(bar().style.width).toBe("2%");
    rerender(<Track fraction={3} />);
    expect(bar().style.width).toBe("100%");
  });
});

describe("StateWord", () => {
  it("carries its tone class", () => {
    render(<StateWord word="STALE" tone="a" />);
    const el = screen.getByText("STALE");
    expect(el).toHaveClass("state");
    expect(el).toHaveClass("a");
  });
  it("has no tone class for none", () => {
    render(<StateWord word="IDLE" tone="none" />);
    expect(screen.getByText("IDLE").className).toBe("state");
  });
});

describe("Segmented", () => {
  it("calls onChange with the clicked value and marks the chosen one on", () => {
    const onChange = vi.fn();
    render(
      <Segmented
        options={[{ label: "1M", value: "1m" }, { label: "3M", value: "3m" }]}
        value="1m"
        onChange={onChange}
      />,
    );
    expect(screen.getByText("1M")).toHaveClass("on");
    expect(screen.getByText("3M")).not.toHaveClass("on");
    fireEvent.click(screen.getByText("3M"));
    expect(onChange).toHaveBeenCalledWith("3m");
  });
});

describe("PageHeader", () => {
  it("shows the crumb in capitals, the title and the right slot", () => {
    render(<PageHeader crumb="Trading / Paper" title="Paper" right={<span>RIGHT SLOT</span>} />);
    expect(screen.getByText("TRADING / PAPER")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Paper" })).toBeInTheDocument();
    expect(screen.getByText("RIGHT SLOT")).toBeInTheDocument();
  });
});

describe("Panel, Facts, Rows, Cols", () => {
  it("Panel shows title, note and body", () => {
    render(<Panel title="Open trades" note="3 now"><p>BODY</p></Panel>);
    expect(screen.getByText("Open trades")).toBeInTheDocument();
    expect(screen.getByText("3 now")).toBeInTheDocument();
    expect(screen.getByText("BODY")).toBeInTheDocument();
  });
  it("Facts gives num to numeric values", () => {
    render(<Facts items={[["Cash", "$5,000"], ["Mode", "Paper", "g"]]} />);
    expect(screen.getByText("$5,000")).toHaveClass("num");
    expect(screen.getByText("Paper")).toHaveClass("g");
    expect(screen.getByText("Paper")).not.toHaveClass("num");
  });
  it("Rows renders each row's cells", () => {
    const { container } = render(<Rows items={[["Win rate", "58%"], ["Trades", { v: "12", cls: "g" }]]} />);
    expect(container.querySelectorAll(".rows > div")).toHaveLength(2);
    expect(screen.getByText("58%")).toHaveClass("num");
    expect(screen.getByText("12")).toHaveClass("g");
  });
  it("Cols renders a cap, name, state, big value and sub line", () => {
    const { container } = render(
      <Cols items={[{ cap: "#2fe08a", name: "Momentum", state: "WORKING", stateTone: "g", big: "+4.2%", sub: "since April", body: <i>BODY</i> }]} />,
    );
    expect(container.querySelector(".col .cap")).toHaveStyle({ backgroundColor: "#2fe08a" });
    expect(screen.getByText("Momentum")).toBeInTheDocument();
    expect(screen.getByText("WORKING")).toHaveClass("g");
    expect(screen.getByText("+4.2%")).toHaveClass("num");
    expect(screen.getByText("since April")).toBeInTheDocument();
    expect(screen.getByText("BODY")).toBeInTheDocument();
  });
});

describe("hover reasons on dashes", () => {
  it("Kpis puts hover as the title on the value, not the label", () => {
    const { container } = render(<Kpis items={[{ label: "SINCE START", value: "-", hover: "No starting value yet" }]} />);
    expect(container.querySelector(".kv > span")).toHaveAttribute("title", "No starting value yet");
    expect(container.querySelector(".kl")).not.toHaveAttribute("title");
  });
  it("Facts puts the fourth entry as the title on the value, not the label", () => {
    const { container } = render(<Facts items={[["TRADES", "-", undefined, "No closed trades counted yet"]]} />);
    expect(container.querySelector("b")).toHaveAttribute("title", "No closed trades counted yet");
    expect(container.querySelector("span")).not.toHaveAttribute("title");
  });
  it("Cols puts bigHover as the title on the big value, not the name", () => {
    const { container } = render(<Cols items={[{ cap: "#fff", name: "Momentum", big: "-", bigHover: "No account figures yet" }]} />);
    expect(container.querySelector(".r")).toHaveAttribute("title", "No account figures yet");
    expect(container.querySelector(".nm")).not.toHaveAttribute("title");
  });
});
