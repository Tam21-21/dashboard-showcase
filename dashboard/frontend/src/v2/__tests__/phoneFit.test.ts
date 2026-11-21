import { describe, expect, it } from "vitest";
// Reads the CSS with node:fs as look.test.ts does (tests run from dashboard/frontend).
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };
const read = (f: string) => fs.readFileSync(`src/v2/${f}`, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");

// The declarations of every rule whose selector list is exactly `sel`.
function bodies(css: string, sel: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    const head = m[1].split(",").map((s) => s.trim().replace(/\s+/g, " ")).join(", ");
    if (head === sel) out.push(m[2]);
  }
  return out;
}
const has = (css: string, sel: string, decl: RegExp) => bodies(css, sel).some((b) => decl.test(b));

describe("phone fit (PF4)", () => {
  it("f1 overview.css stacks the stations in one timeline (the mockup) and wraps the waiting names", () => {
    const css = read("pages/research/overview.css");
    expect(css).toMatch(/@media \(max-width: 720px\)\s*\{\s*\.v2 \.ov6-stops \{ grid-template-columns: minmax\(0, 1fr\)/);
    expect(has(css, ".v2.phone .ov6-cols", /grid-template-columns:\s*minmax\(0, 1fr\)/)).toBe(true);
    expect(has(css, ".v2.phone .ov6-list .nm", /white-space:\s*normal/)).toBe(true);
  });

  it("f2 strategies.css lays the stages one under another and the tiles two across on the phone", () => {
    const css = read("pages/research/strategies.css");
    expect(has(css, ".v2.phone .sx-stops", /grid-template-columns:\s*minmax\(0, 1fr\)/)).toBe(true);
    expect(has(css, ".v2.phone .sx-wall", /grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/)).toBe(true);
    expect(has(css, ".v2.phone .sx-w", /display:\s*none/)).toBe(true);
  });

  it("f3 markets.css scrolls the world table sideways with a fixed first column", () => {
    const css = read("pages/research/markets.css");
    expect(has(css, ".v2.phone .mk .mk-world", /overflow-x:\s*auto/)).toBe(true);
    const sel = ".v2.phone .mk .mk-world th:first-child, .v2.phone .mk .mk-world td:first-child";
    expect(has(css, sel, /position:\s*sticky/)).toBe(true);
    expect(has(css, sel, /left:\s*0/)).toBe(true);
    expect(has(css, sel, /background:\s*var\(--solid\)/)).toBe(true);
  });

  it("f4 look.css defines --solid and kit.css wraps the card foot on a phone", () => {
    expect(read("look.css")).toMatch(/\.v2 \{\s*--solid:\s*#0a0d11;/);
    expect(has(read("kit/kit.css"), ".v2.phone .kp .ktl .mft", /flex-wrap:\s*wrap/)).toBe(true);
  });
});
