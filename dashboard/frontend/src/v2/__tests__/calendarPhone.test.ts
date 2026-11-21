import { describe, expect, it } from "vitest";
// The test setup blanks CSS imports, so the files are read with Node's fs (tests run from dashboard/frontend).
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };
const calendarCss = fs.readFileSync("src/v2/pages/calendar/calendar.css", "utf-8");
const todayCss = fs.readFileSync("src/v2/pages/calendar/today.css", "utf-8");

// The body of the rule whose selector is exactly `sel`, or null.
function rule(src: string, sel: string): string | null {
  const text = src.replace(/\/\*[\s\S]*?\*\//g, "");
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m[1].trim().split(/\s*,\s*/).includes(sel)) return m[2];
  }
  return null;
}

describe("calendar phone and past-row styles", () => {
  it("c1 the phone button row wraps two to a row", () => {
    expect(rule(calendarCss, ".v2.phone .mact")).toContain("flex-wrap: wrap");
    expect(rule(calendarCss, ".v2.phone .mact > *")).toContain("flex: 1 1 calc(50% - 4px)");
  });

  it("c2 a past row dims its bar only, the time stays readable", () => {
    expect(rule(todayCss, ".v2 .agn .ar.past") ?? "").not.toContain("opacity");
    expect(rule(todayCss, ".v2 .agn .ar.past .n")).toContain("color: var(--soft)");
    expect(rule(todayCss, ".v2 .agn .ar.past .k")).toContain("color: var(--soft)");
    expect(rule(todayCss, ".v2 .agn .ar.past i")).toContain("opacity: .45");
  });

  it("c4 the NEW DATE popup opens leftward from the row end", () => {
    const body = rule(todayCss, ".v2 .td .tdend .pop") ?? "";
    expect(body).toContain("left: auto");
    expect(body).toContain("right: 0");
  });

  it("c5 the NEW DATE popup opens upward, so the phone bottom nav never covers its grid", () => {
    const body = rule(todayCss, ".v2 .td .tdend .pop") ?? "";
    expect(body).toContain("top: auto");
    expect(body).toContain("bottom: calc(100% + 6px)");
  });

  it("c6 on the phone the TO DO panel shows its popup instead of clipping it", () => {
    expect(rule(todayCss, ".v2.phone .cd:has(.td)")).toContain("overflow: visible");
  });

  it("c3 guard: the desktop button row is unchanged", () => {
    expect(rule(calendarCss, ".v2 .mact")).toContain("display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px;");
  });
});
