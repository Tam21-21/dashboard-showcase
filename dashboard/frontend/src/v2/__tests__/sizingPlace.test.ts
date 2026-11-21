import { describe, expect, it } from "vitest";
import { placeSizingMethod } from "../pages/research/strategies/sizingPlace";

const z1 = ["class S(Strategy):", '    """One line."""', '    sizing_method = "vol_target_atr"', "    def run(self):", "        pass", ""];
const NOCLASS = "No class found, so the money method has nowhere to go. Set sizing_method in the strategy's class.";
const TWO = "This source has two classes; set sizing_method in the strategy's class by hand.";

const THEORY = "A theory strategy takes its money method from its theory file, not from this source.";
const HELPER = "Another class in this source already sets sizing_method, and the server reads that one; set it by hand.";

function ok(src: string, id = "fixed_risk", eol = "\n") {
  const r = placeSizingMethod(src, id, eol);
  if (!r.ok) throw new Error(r.why);
  return r.text;
}

describe("placeSizingMethod", () => {
  it("z1 replaces the class body line", () => {
    const out = ok(z1.join("\n")).split("\n");
    expect(out[2]).toBe('    sizing_method = "fixed_risk"');
    expect(out.filter((_, i) => i !== 2)).toEqual(z1.filter((_, i) => i !== 2));
  });
  it("z2 replaces an annotated line", () => {
    const src = z1.join("\n").replace('sizing_method = "vol_target_atr"', 'sizing_method: str = "x"');
    expect(ok(src).split("\n")[2]).toBe('    sizing_method = "fixed_risk"');
  });
  it("z3 adds after a three-line docstring", () => {
    const src = ["class S(Strategy):", '    """a', "    b", '    c"""', "    def run(self):", "        pass"].join("\n");
    expect(ok(src).split("\n")[4]).toBe('    sizing_method = "fixed_risk"');
  });
  it("z4 adds after the class line when no docstring", () => {
    const src = ["class S(Strategy):", "    x = 1"].join("\n");
    expect(ok(src).split("\n")).toEqual(["class S(Strategy):", '    sizing_method = "fixed_risk"', "    x = 1"]);
  });
  it("z5 uses a tab indent", () => {
    const src = ["class S(Strategy):", "\tx = 1"].join("\n");
    expect(ok(src).split("\n")[1]).toBe('\tsizing_method = "fixed_risk"');
  });
  it("z6 no class", () => {
    expect(placeSizingMethod("x = 1\n", "fixed_risk", "\n")).toEqual({ ok: false, why: NOCLASS });
  });
  it("z7 two classes", () => {
    const src = "class A:\n    x = 1\nclass B:\n    y = 1\n";
    expect(placeSizingMethod(src, "fixed_risk", "\n")).toEqual({ ok: false, why: TWO });
  });
  it("z8 leaves a method-level line and adds a body line", () => {
    const src = ["class S(Strategy):", "    x = 1", "    def run(self):", '        sizing_method = "a"'].join("\n");
    expect(ok(src).split("\n")).toEqual([
      "class S(Strategy):", '    sizing_method = "fixed_risk"', "    x = 1", "    def run(self):", '        sizing_method = "a"',
    ]);
  });
  it("z9 keeps CRLF", () => {
    const out = ok(z1.join("\r\n"), "fixed_risk", "\r\n");
    expect(out).toBe(z1.join("\r\n").replace("vol_target_atr", "fixed_risk"));
  });
  it("z10 replaces only the first", () => {
    const src = ["class S:", '    sizing_method = "a"', '    sizing_method = "b"'].join("\n");
    expect(ok(src).split("\n")).toEqual(["class S:", '    sizing_method = "fixed_risk"', '    sizing_method = "b"']);
  });
  it("z11 refuses a theory strategy source", () => {
    const src = [
      '"""Generator v2 theory: Pullback.',
      "",
      "This strategy row stands for a theory file of generator v2 (DECISIONS #445). The theory's TOML text is the",
      "constant THEORY_TOML below. The search, the optimise stage and the walk-forward stage run it through the jobs",
      "run_theory_search and run_theory_stage; run() here refuses every other use by name.",
      '"""',
      "from __future__ import annotations",
      "",
      "from strategies.base import Strategy",
      "",
      "THEORY_NAME = 'Pullback'",
      "THEORY_TOML = 'x = 1'",
      "",
      "",
      "class TheoryRunsOnlyAsJobs(RuntimeError):",
      '    """A theory strategy was handed to an engine that is not a theory job."""',
      "",
      "",
      "class TheoryStrategy(Strategy):",
      '    """The row of one generator v2 theory; it does not run as an ordinary strategy."""',
      "",
      '    asset_class_scope = ("equities",)',
      "",
      "    def run(self, data, settings):",
      "        raise TheoryRunsOnlyAsJobs(",
      '            "theory " + THEORY_NAME + " runs only as the jobs run_theory_search and run_theory_stage"',
      "        )",
      "",
    ].join("\n");
    expect(placeSizingMethod(src, "fixed_risk", "\n")).toEqual({ ok: false, why: THEORY });
  });
  it("z12 writes into the one Strategy subclass beside a helper", () => {
    const src = ["class Helper(RuntimeError):", "    pass", "class S(Strategy):", "    x = 1"].join("\n");
    expect(ok(src).split("\n")).toEqual([
      "class Helper(RuntimeError):", "    pass", "class S(Strategy):", '    sizing_method = "fixed_risk"', "    x = 1",
    ]);
  });
  it("z13 guard: two Strategy subclasses keep the two-class refusal", () => {
    const src = ["class A(Strategy):", "    x = 1", "class B(Strategy):", "    y = 1"].join("\n");
    expect(placeSizingMethod(src, "fixed_risk", "\n")).toEqual({ ok: false, why: TWO });
  });
  it("z14 guard: no Strategy candidate keeps the two-class refusal", () => {
    const src = ["class Helper(RuntimeError):", "    pass", "class Other(Base):", "    y = 1"].join("\n");
    expect(placeSizingMethod(src, "fixed_risk", "\n")).toEqual({ ok: false, why: TWO });
  });
  it("z15 a helper that already sets sizing_method refuses", () => {
    const src = ["class Helper(RuntimeError):", '    sizing_method = "a"', "class S(Strategy):", "    x = 1"].join("\n");
    expect(placeSizingMethod(src, "fixed_risk", "\n")).toEqual({ ok: false, why: HELPER });
  });
});
