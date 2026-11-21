import { useEffect, useState } from "react";
import type { RefObject } from "react";

// How a run ended, in the Runs page's one colour key: green finished with figures, amber waits for the judge, grey
// finished with no figures saved, red failed, blue running, an outlined grey stick cancelled.
export type RunKind = "good" | "warn" | "soft" | "bad" | "run" | "off";

export const KIND_ORDER: RunKind[] = ["good", "warn", "soft", "bad", "run", "off"];

export const KIND_WORD: Record<RunKind, string> = {
  good: "Finished, with figures",
  warn: "Waits for the judge",
  soft: "Finished, no figures saved",
  bad: "Failed",
  run: "Running",
  off: "Cancelled",
};

// The same words in the week chart's header, where each says how many.
export const KIND_COUNT_WORD: Record<RunKind, string> = {
  good: "finished, with figures",
  warn: "waiting for the judge",
  soft: "finished, no figures saved",
  bad: "failed",
  run: "running",
  off: "cancelled",
};

/** The state word's tone for a kind. */
export const KIND_TONE: Record<RunKind, "good" | "warn" | "dim" | "bad" | "run"> = {
  good: "good", warn: "warn", soft: "dim", bad: "bad", run: "run", off: "dim",
};

/** The width of a box, kept up to date as it resizes. A box with no width yet (hidden, or a test without layout) draws at 600. */
export function useWidth(ref: RefObject<HTMLElement | null>): number {
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setW(Math.floor(el.getBoundingClientRect().width) || 600);
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

/** Round axis marks: a step of 1, 2, 5 or 10 times a power of ten, about `want` marks from lo to hi. */
export function niceTicks(lo: number, hi: number, want = 4): number[] {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / want;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 5, 10].find((m) => m * mag >= raw) ?? 10) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v / step) * step);
  return out;
}

/** Axis marks at a fixed step from lo up to hi. */
export function stepTicks(lo: number, hi: number, step: number): number[] {
  const out: number[] = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v / step) * step);
  return out;
}
