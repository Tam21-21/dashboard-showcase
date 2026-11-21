import { describe, expect, it } from "vitest";
// The project has no Node type package and the test setup blanks CSS imports (css: false),
// so the file is read with Node's fs through a typed local view (tests run from dashboard/frontend).
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };
const css = fs.readFileSync("src/v2/look.css", "utf-8");

// Blank out comments, then split into top-level blocks.
function topLevel(src: string): { head: string; body: string }[] {
  const text = src.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: { head: string; body: string }[] = [];
  let i = 0;
  while (i < text.length) {
    const brace = text.indexOf("{", i);
    const semi = text.indexOf(";", i);
    if (brace === -1) break;
    if (semi !== -1 && semi < brace) {
      i = semi + 1;
      continue;
    }
    let depth = 1;
    let j = brace + 1;
    while (depth > 0 && j < text.length) {
      if (text[j] === "{") depth++;
      if (text[j] === "}") depth--;
      j++;
    }
    out.push({ head: text.slice(i, brace).trim(), body: text.slice(brace + 1, j - 1) });
    i = j;
  }
  return out;
}

describe("look.css", () => {
  it("imports both font packages", () => {
    expect(css).toContain('@import "@fontsource-variable/instrument-sans"');
    expect(css).toContain('@import "@fontsource-variable/azeret-mono"');
  });

  it("scopes every rule under .v2", () => {
    const rules: string[] = [];
    for (const b of topLevel(css)) {
      if (b.head.startsWith("@font-face") || b.head.startsWith("@keyframes") || b.head.startsWith("@property")) continue;
      if (b.head.startsWith("@media")) continue;
      rules.push(b.head);
    }
    expect(rules.length).toBeGreaterThan(5);
    for (const head of rules) {
      for (const sel of head.split(",")) {
        expect(sel.trim().startsWith(".v2"), `selector ${sel.trim()}`).toBe(true);
      }
    }
  });

  it("holds the template values", () => {
    for (const v of [
      "#e3e7ed", "#d3d9e2", "#a3adbb", "#7c8696", "#2fe08a", "#ff5a63", "#ffb020",
      "rgba(255,255,255,.09)", "22px",
    ]) {
      expect(css, v).toContain(v);
    }
  });

  it("holds the mockup tokens: the Aurora card, the Glass button, never a blue button", () => {
    expect(css).toMatch(/--card:\s*#0a0e17/);
    expect(css).toMatch(/--btn:\s*rgb\(255 255 255 \/ \.085\)/);
    expect(css).toMatch(/data-btn="white"\] \{ --btn: #e3e7ed/);
    expect(css).toMatch(/\.v2 \.panel, \.v2 \.cd, \.v2 \.nl-blk \{[^}]*background: var\(--card-fill\)/);
    expect(css).not.toMatch(/--btn:\s*#4d9bff/);
  });
});
