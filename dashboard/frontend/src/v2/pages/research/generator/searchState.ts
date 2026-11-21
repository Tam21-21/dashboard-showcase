// The words of section 2, THE SEARCHES, as pure functions: what a run, a theory and a batch is called on the page.
// No requests and no React here, so the rules can be tested with hand-built trees.
import type { GenBatch, GenRun, GenTheory, GenTree } from "../../../data/generatorApi";
import type { Tone } from "../../../kit";
import { fmtDmy } from "../../../../lib/format";

export interface Word {
  word: string;
  tone: Tone;
  hover?: string;
}

/** A stage's run is the last one in the theory's runs for that stage. */
export function stageRun(theory: GenTheory, stage: string): GenRun | undefined {
  const runs = theory.runs.filter((r) => r.stage === stage);
  return runs[runs.length - 1];
}

export function runWord(status: string): Word {
  switch (status) {
    case "pending":
      return { word: "RUNNING", tone: "bl", hover: "Queued or running: the server does not tell them apart yet" };
    case "running":
      return { word: "RUNNING", tone: "bl" };
    case "succeeded":
      return { word: "DONE", tone: "g" };
    case "failed":
      return { word: "FAILED", tone: "rd" };
    case "cancelled":
      return { word: "CANCELLED", tone: "rd" };
    case "missing":
      return { word: "MISSING", tone: "rd" };
    default:
      return { word: status.toUpperCase(), tone: "a" };
  }
}

const isLive = (r: GenRun) => r.status === "pending" || r.status === "running";

/** Strategies of a theory that held up on the kept-back stocks; none before the final exam. */
export function heldCount(theory: GenTheory): number {
  return theory.final === "not run" ? 0 : theory.final.strategies.filter((s) => s.held_up).length;
}

export function passedCount(theory: GenTheory): number {
  return theory.saved.filter((s) => s.verdict?.word === "pass").length;
}

export function theoryState(theory: GenTheory): Word {
  if (theory.stop != null) return { word: "STOPPED", tone: "rd" };
  const screen = stageRun(theory, "screen");
  if (screen == null) return { word: "NOT STARTED", tone: "a" };
  if (theory.runs.some(isLive)) return { word: "RUNNING", tone: "bl" };
  if (screen.status !== "succeeded") return { word: "FAILED", tone: "rd" };
  if (theory.final === "not run") return { word: "SEARCHED", tone: "none" };
  const held = heldCount(theory);
  if (held === 0) return { word: "NOTHING HELD UP", tone: "none" };
  if (theory.saved.length < held) return { word: "RUNNING", tone: "bl" };
  if (passedCount(theory) > 0) return { word: "PASSED", tone: "g" };
  return { word: "JUDGED", tone: "none" };
}

/** The batch's word, or null while its tree is still being read and its control does not already say. */
export function batchState(batch: GenBatch, tree: GenTree | undefined): Word | null {
  const since = batch.control.since;
  const sinceHover = since == null ? undefined : `Since ${fmtDmy(since)}`;
  if (batch.control.state === "stopped") return { word: "STOPPED", tone: "rd", hover: sinceHover };
  if (batch.control.state === "paused") return { word: "PAUSED", tone: "a", hover: sinceHover };
  if (tree == null) return null;
  const states = tree.theories.map((t) => theoryState(t).word);
  if (states.length === 0) return { word: "NO THEORIES", tone: "none" };
  if (states.includes("RUNNING")) return { word: "RUNNING", tone: "bl" };
  if (states.includes("NOT STARTED")) return { word: "NOT STARTED", tone: "a" };
  const passed = tree.theories.reduce((n, t) => n + passedCount(t), 0);
  if (passed > 0) return { word: `${passed} PASSED`, tone: "g" };
  if (states.every((s) => s === "STOPPED")) return { word: "STOPPED", tone: "rd" };
  return { word: "NOTHING FOUND", tone: "none" };
}

/** The theories that are neither running nor waiting to start, and the total. */
export function endedCount(tree: GenTree): { ended: number; total: number } {
  const total = tree.theories.length;
  const ended = tree.theories.filter((t) => {
    const w = theoryState(t).word;
    return w !== "RUNNING" && w !== "NOT STARTED";
  }).length;
  return { ended, total };
}

/** Theories with no screen run: the ones RESUME would start. */
export function withoutSearch(tree: GenTree): number {
  return tree.theories.filter((t) => stageRun(t, "screen") == null).length;
}

/** The values a strategy picked, as "key value" in the object's order. */
export function picksText(picks: unknown): string {
  if (picks == null || typeof picks !== "object") return "-";
  return Object.entries(picks as Record<string, unknown>).map(([k, v]) => `${k} ${String(v)}`).join(", ");
}

/** A step's name in capitals; walk-forward keeps its hyphen. */
export function stepWord(step: string): string {
  return step === "walkforward" ? "WALK-FORWARD" : step.toUpperCase();
}

/** The judge's word for a saved strategy: its verdict, or REFUSED with the refusal on hover. */
export function verdictWord(saved: { verdict: { word: string } | null; refusal: string | null }): Word {
  if (saved.verdict == null) return { word: "REFUSED", tone: "a", hover: saved.refusal ?? undefined };
  switch (saved.verdict.word) {
    case "pass":
      return { word: "PASS", tone: "g" };
    case "fail":
      return { word: "FAIL", tone: "rd" };
    case "unjudgeable":
      return { word: "UNJUDGEABLE", tone: "a" };
    case "missing":
      return { word: "MISSING", tone: "rd" };
    default:
      return { word: saved.verdict.word.toUpperCase(), tone: "a" };
  }
}

export const TONE_COLOUR: Record<Tone, string> = {
  bl: "#4d9bff",
  g: "#2fe08a",
  rd: "#ff5a63",
  a: "#ffb020",
  none: "#8d98a8",
};
