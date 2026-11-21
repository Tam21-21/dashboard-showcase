import { describe, expect, it } from "vitest";
// Reads the CSS files with Node's fs exactly as look.test.ts does (css: false blanks CSS imports).
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };

// Blank comments, then return the declaration body of the rule whose selector list holds `selector`.
function rule(file: string, selector: string): string | null {
  const text = fs.readFileSync(file, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m[1].split(",").some((s) => s.trim() === selector)) return m[2];
  }
  return null;
}

function has(body: string | null, prop: string, value: string): boolean {
  if (body === null) return false;
  return body.split(";").some((d) => {
    const [k, v] = d.split(":");
    return k !== undefined && v !== undefined && k.trim() === prop && v.trim() === value;
  });
}

const PAPER = "src/v2/pages/trading/paper.css";
const PARTS = "src/v2/pages/trading/parts.css";
const GEN = "src/v2/pages/research/generator/generator.css";

describe("phone headers and gaps (PF2)", () => {
  it("Paper and Capital leave less room above their blocks on the phone", () => {
    expect(has(rule(PARTS, ".v2.phone .tp-stack"), "margin-top", "24px")).toBe(true);
    expect(has(rule(PARTS, ".v2.phone .tp-stack"), "gap", "14px")).toBe(true);
  });
  it("Generator's finding has a gap below it (round six: the finding took the card row's place)", () => {
    expect(has(rule(GEN, ".v2 .gn .nl-say"), "margin-bottom", "44px")).toBe(true);
  });
  it("Paper's lists scroll inside their block and its limits take one column on the phone", () => {
    expect(has(rule(PARTS, ".v2 .tp-scroll"), "overflow-x", "auto")).toBe(true);
    expect(has(rule(PAPER, ".v2.phone .pp-lims"), "grid-template-columns", "minmax(0, 1fr)")).toBe(true);
  });
});
