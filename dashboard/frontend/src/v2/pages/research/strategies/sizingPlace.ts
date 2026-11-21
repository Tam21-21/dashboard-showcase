// Sets the strategy's money method in the one place the server reads: a sizing_method line in the
// class body. It replaces the first such line, or adds one under the docstring (else under the class line).

export type Placed = { ok: true; text: string } | { ok: false; why: string };

const CLASS_LINE = /^class\s+\w+[^:]*:\s*(#.*)?$/;
const NO_CLASS = "No class found, so the money method has nowhere to go. Set sizing_method in the strategy's class.";
const TWO_CLASSES = "This source has two classes; set sizing_method in the strategy's class by hand.";
const THEORY = "A theory strategy takes its money method from its theory file, not from this source.";
const HELPER = "Another class in this source already sets sizing_method, and the server reads that one; set it by hand.";
const NO_BODY = "No class found, so the money method has nowhere to go. The class has no body.";

function isBlankOrComment(line: string): boolean {
  const t = line.trim();
  return t === "" || t.startsWith("#");
}

export function placeSizingMethod(source: string, id: string, eol: string): Placed {
  const lines = source.split(/\r?\n/);
  if (lines.some((l) => /^THEORY_TOML\s*=/.test(l))) return { ok: false, why: THEORY };
  const classes: number[] = [];
  lines.forEach((l, i) => { if (CLASS_LINE.test(l)) classes.push(i); });
  if (classes.length === 0) return { ok: false, why: NO_CLASS };
  let start = classes[0];
  if (classes.length > 1) {
    // A helper class may sit beside the one Strategy subclass; the server reads the first sizing_method in any class (#445, ED1c).
    const candidates = classes.filter((i) => {
      const m = /^class\s+\w+\s*\(([^)]*)\)/.exec(lines[i]);
      return m !== null && /\b(Theory)?Strategy\b/.test(m[1]);
    });
    if (candidates.length !== 1) return { ok: false, why: TWO_CLASSES };
    start = candidates[0];
    const anySizing = /^\s+sizing_method\s*(:[^=]*)?=/;
    for (const c of classes) {
      if (c === start) continue;
      for (let i = c + 1; i < lines.length; i++) {
        if (!isBlankOrComment(lines[i]) && !/^\s/.test(lines[i])) break;
        if (anySizing.test(lines[i])) return { ok: false, why: HELPER };
      }
    }
  }

  // The body runs to the next non-blank, non-comment line at column 0.
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    if (!isBlankOrComment(lines[i]) && !/^\s/.test(lines[i])) { end = i; break; }
  }
  const first = lines.findIndex((l, i) => i > start && i < end && !isBlankOrComment(l));
  if (first < 0) return { ok: false, why: NO_BODY };
  const indent = /^\s*/.exec(lines[first])![0];
  const wanted = `${indent}sizing_method = "${id}"`;

  const own = new RegExp(`^${indent}sizing_method\\s*(:[^=]*)?=`);
  for (let i = start + 1; i < end; i++) {
    if (own.test(lines[i])) {
      lines[i] = wanted;
      return { ok: true, text: lines.join(eol) };
    }
  }

  let at = start + 1;
  const rest = lines[first].slice(indent.length);
  const quote = rest.startsWith('"""') ? '"""' : rest.startsWith("'''") ? "'''" : null;
  if (quote) {
    let close = first;
    if (rest.indexOf(quote, 3) < 0) {
      close = -1;
      for (let i = first + 1; i < end; i++) {
        if (lines[i].includes(quote)) { close = i; break; }
      }
      if (close < 0) return { ok: false, why: "The class docstring never closes, so the money method has nowhere to go." };
    }
    at = close + 1;
  }
  lines.splice(at, 0, wanted);
  return { ok: true, text: lines.join(eol) };
}
