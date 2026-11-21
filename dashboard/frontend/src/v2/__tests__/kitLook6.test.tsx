import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";
import { Block, Say, State } from "../kit";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };

describe("the new look's kit parts (round six)", () => {
  it("Block is a card with its title, state word and control", () => {
    render(<div className="v2"><Block title="Waiting for the judge" state={<State tone="warn">9 strategies</State>} right={<button type="button">Open the judge</button>}><p>body</p></Block></div>);
    const card = document.querySelector(".nl-blk") as HTMLElement;
    expect(card.querySelector("h2")?.textContent).toBe("Waiting for the judge");
    expect(screen.getByText("9 strategies").className).toBe("nl-st warn");
    expect(screen.getByRole("button", { name: "Open the judge" })).toBeTruthy();
    expect(screen.getByText("body")).toBeTruthy();
  });

  it("Say puts the finding first and the detail after it", () => {
    render(<div className="v2"><Say lead="Nothing has reached paper yet." more="The judge graded 6 runs." /></div>);
    const p = document.querySelector(".nl-say") as HTMLElement;
    expect(p.querySelector("b")?.textContent).toBe("Nothing has reached paper yet.");
    expect(p.querySelector(".more")?.textContent).toBe("The judge graded 6 runs.");
  });

  it("a counted legend without onPick lays its rows out like the pressable one (ui2's wish, 03:05)", () => {
    const css = fs.readFileSync("src/v2/kit/kit.css", "utf-8");
    const rule = css.split("\n").find((l) => l.startsWith(".v2 .lgd-n > button, .v2 .lgd-n > span {"));
    expect(rule).toContain("display: grid");
    expect(rule).toContain("grid-template-columns: 10px minmax(0, 1fr) auto 38px");
    expect(rule).not.toContain("cursor: pointer");
  });

  it("motion in the kit moves or fades only: no clip-path, width, filter or blur animated", () => {
    const motion = fs.readFileSync("src/v2/kit/motion.ts", "utf-8");
    const css = fs.readFileSync("src/v2/kit/look6.css", "utf-8");
    for (const src of [motion, css]) expect(src).not.toMatch(/clip-path|filter|blur|animate\(\[\{ width/);
    expect(motion).toContain("still");
  });
});
