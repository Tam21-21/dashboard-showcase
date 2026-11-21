import type { HistoryBar } from "../../../../types/display";

// Pure figures from a list of daily bars, oldest first. Every function either returns a number or
// the reason there is none (a short series is a dash with a hover, never an invented figure).
// Formulas: docs/research_lab/display_methods.md, Markets page, Tiles and Fear gauge.

export type Fig = { value: number; reason: null } | { value: null; reason: string };

const ok = (value: number): Fig => ({ value, reason: null });
const no = (reason: string): Fig => ({ value: null, reason });

export const DAY_BARS = 1;
export const MONTH_BARS = 21;
export const YEAR_BARS = 252;
export const AVERAGE_BARS = 200;

/** The reason a series is too short for a figure that needs `need` bars, or null when it is long enough. */
export function shortReason(have: number, need: number): string | null {
  if (have === 0) return "No daily bars came back for this series.";
  if (have >= need) return null;
  return `Needs ${need} daily bars, ${have} came back (${need - have} missing).`;
}

export function lastClose(bars: HistoryBar[]): Fig {
  const r = shortReason(bars.length, 1);
  return r ? no(r) : ok(bars[bars.length - 1].close);
}

/** Change in percent over `n` bars: close / close n bars earlier - 1. Needs n + 1 bars. */
export function changeOver(bars: HistoryBar[], n: number): Fig {
  const r = shortReason(bars.length, n + 1);
  if (r) return no(r);
  const now = bars[bars.length - 1].close;
  const then = bars[bars.length - 1 - n].close;
  if (then === 0) return no("The close to compare with is zero, so there is no percent.");
  return ok((now / then - 1) * 100);
}

export const dayChange = (bars: HistoryBar[]): Fig => changeOver(bars, DAY_BARS);

/** This year: last close over the last close of the previous calendar year, minus 1, in percent. */
export function yearToDate(bars: HistoryBar[]): Fig {
  const r = shortReason(bars.length, 1);
  if (r) return no(r);
  const last = bars[bars.length - 1];
  const prevYear = String(Number(last.time.slice(0, 4)) - 1);
  let base: HistoryBar | null = null;
  for (const b of bars) if (b.time.slice(0, 4) === prevYear) base = b;
  if (!base) return no(`No close in ${prevYear} in this series.`);
  return ok((last.close / base.close - 1) * 100);
}

/** The n-day simple average at every bar; null where fewer than n closes exist yet. */
export function sma(closes: number[], n: number): Array<number | null> {
  const out: Array<number | null> = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= n) sum -= closes[i - n];
    out.push(i >= n - 1 ? sum / n : null);
  }
  return out;
}

/** The latest n-day simple average. */
export function average(bars: HistoryBar[], n: number): Fig {
  const r = shortReason(bars.length, n);
  if (r) return no(r);
  const v = sma(bars.map((b) => b.close), n)[bars.length - 1];
  return v == null ? no(`Needs ${n} daily bars.`) : ok(v);
}

export type Range = { low: number; high: number; close: number; position: number };

/** Lowest and highest close over the last 252 trading days (today and the 252 before), and where today sits. */
export function yearRange(bars: HistoryBar[]): { value: Range; reason: null } | { value: null; reason: string } {
  const r = shortReason(bars.length, YEAR_BARS + 1);
  if (r) return { value: null, reason: r };
  const window = bars.slice(-(YEAR_BARS + 1)).map((b) => b.close);
  const low = Math.min(...window);
  const high = Math.max(...window);
  const close = window[window.length - 1];
  if (high === low) return { value: null, reason: "The lowest and highest close are equal, so there is no position." };
  return { value: { low, high, close, position: (close - low) / (high - low) }, reason: null };
}

export const WEEK_DAYS = 7;
export const YARDSTICK_DAYS = 365;
const DAY_S = 86_400;

export type WeekMove = {
  start: HistoryBar;
  end: HistoryBar;
  /** close(end) / close(start) - 1, in percent. */
  move: number;
  /** How many 7-day moves the yardstick holds. */
  count: number;
  /** The yardstick's biggest fall, lower quartile, upper quartile and biggest rise, in percent. */
  lo: number;
  q1: number;
  q3: number;
  hi: number;
  /** How many of the yardstick's moves are smaller in size than this week's. */
  smaller: number;
  /** smaller / count, 0 to 1: for ordering only; the page says the share with biggerThan, in whole numbers. */
  rank: number;
};

/** The share of its past year's weeks this week is bigger than, in whole percent, rounded down. Integer maths, so 145 of 250
 * reads 58, never 57 (145 / 250 * 100 is 57.99999999999999 in floating point). */
export const biggerThan = (w: WeekMove) => Math.floor((100 * w.smaller) / w.count);

/** The index of the newest bar dated on or before t (epoch seconds), or -1. Bars oldest first. */
function onOrBefore(bars: HistoryBar[], t: number): number {
  let lo = 0, hi = bars.length - 1, k = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (bars[m].epoch_seconds <= t) { k = m; lo = m + 1; } else hi = m - 1;
  }
  return k;
}

/**
 * This week (display_methods.md, Markets page: This week): the move from the newest bar dated 7 or more calendar days before the
 * newest bar, and where it sits among the series' own 7-day moves ending on each bar of the 365 days before the start.
 */
export function weekMove(bars: HistoryBar[]): { value: WeekMove; reason: null } | { value: null; reason: string } {
  if (bars.length === 0) return { value: null, reason: "No daily bars came back for this series." };
  const end = bars[bars.length - 1];
  const s = onOrBefore(bars, end.epoch_seconds - WEEK_DAYS * DAY_S);
  if (s < 0) return { value: null, reason: "The series has no close 7 or more days before its newest one." };
  const start = bars[s];
  if (start.close === 0) return { value: null, reason: "The close to compare with is zero, so there is no percent." };
  const ref: number[] = [];
  for (let i = 0; i <= s; i++) {
    if (bars[i].epoch_seconds <= start.epoch_seconds - YARDSTICK_DAYS * DAY_S) continue;
    const j = onOrBefore(bars, bars[i].epoch_seconds - WEEK_DAYS * DAY_S);
    if (j >= 0 && bars[j].close !== 0) ref.push((bars[i].close / bars[j].close - 1) * 100);
  }
  if (ref.length === 0) return { value: null, reason: "The series has no 7-day move in the year before this week to compare with." };
  const move = (end.close / start.close - 1) * 100;
  const sorted = [...ref].sort((a, b) => a - b);
  const q = (p: number) => sorted[Math.round(p * (sorted.length - 1))];
  const smaller = ref.filter((r) => Math.abs(r) < Math.abs(move)).length;
  return {
    value: {
      start, end, move, count: ref.length,
      lo: sorted[0], q1: q(0.25), q3: q(0.75), hi: sorted[sorted.length - 1],
      smaller, rank: smaller / ref.length,
    },
    reason: null,
  };
}
