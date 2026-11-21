// Pure helpers of the Datasets tab: when a series is behind, the year strips, the big-number format.
// The method is written in docs/research_lab/display_methods.md, "Data library: Datasets tab, 2026-10-16".
import type { Dataset } from "../../../../types/research";

export const DAY = 86400;
// Tamer 2026-10-16: "Newest data over 7 days old".
export const STALE_AFTER = 7 * DAY;

// Behind: the newest bar, article, rating or filing is more than 7 days older than now (epoch seconds).
export function isStale(newest: number, now: number): boolean {
  return now - newest > STALE_AFTER;
}

// A price series: one ticker's bars, not deleted. Behind is counted among these, on the Datasets tab and on the Overview's Data
// station alike (universes, event feeds and fundamentals have their own freshness).
export const isPriceSeries = (d: Dataset): boolean => d.dataset_type === "price_dataset" && d.status !== "deleted";

export const utcYear = (epoch: number): number => new Date(epoch * 1000).getUTCFullYear();

// A series with bars (start and end set) touches a year when its held span overlaps that UTC year.
export function touchesYear(start: number, end: number, year: number): boolean {
  return utcYear(start) <= year && year <= utcYear(end);
}

export type PriceSpan = { start: number; end: number };

// One cell per UTC year from the earliest start to the current year; n is the series whose span touches it.
export function priceStrip(spans: PriceSpan[], nowYear: number): Array<{ year: number; n: number }> {
  if (spans.length === 0) return [];
  const first = Math.min(...spans.map((s) => utcYear(s.start)));
  const out: Array<{ year: number; n: number }> = [];
  for (let year = first; year <= nowYear; year += 1) {
    out.push({ year, n: spans.filter((s) => touchesYear(s.start, s.end, year)).length });
  }
  return out;
}

// One cell per UTC year from the earliest year with a count to the current year; a year without an entry is 0.
export function countStrip(perYear: Array<{ year: number; count: number }>, nowYear: number): Array<{ year: number; n: number }> {
  const held = perYear.filter((p) => p.count > 0);
  if (held.length === 0) return [];
  const first = Math.min(...held.map((p) => p.year));
  const out: Array<{ year: number; n: number }> = [];
  for (let year = first; year <= nowYear; year += 1) {
    out.push({ year, n: perYear.find((p) => p.year === year)?.count ?? 0 });
  }
  return out;
}

// A price cell's opacity: 0.12 for none of the series, 1 for all of them.
export const shareOpacity = (n: number, total: number): number => 0.12 + (0.88 * n) / total;

// The mockup's own count words: "4.45 M" from a million up, "79 k" from ten thousand up, else en-US grouping.
export function bigNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)} M`;
  if (n >= 10_000) return `${Math.round(n / 1000)} k`;
  return n.toLocaleString("en-US");
}
