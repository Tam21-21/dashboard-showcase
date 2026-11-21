// Pure helpers of the Universes tab: members per year, the bars sum, the number and date formats,
// the Prices line's place on the 1962-to-today axis, and the ticker box's words.

export type Covered = { first_bar_ts: number | null; last_bar_ts: number | null; bars: number | null };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const FIRST_YEAR = 1970;
export const AXIS_START = 1962;

export const utcYear = (epoch: number): number => new Date(epoch * 1000).getUTCFullYear();

// "1 Jan 2015", UTC; "-" when there is no date.
export function dayLong(epoch: number | null | undefined): string {
  if (epoch == null) return "-";
  const d = new Date(epoch * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

// "22 Oct 26", UTC; "-" when there is no date.
export function dayShort(epoch: number | null | undefined): string {
  if (epoch == null) return "-";
  const d = new Date(epoch * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`;
}

// The window as words: "1 Jan 2015 to 31 Dec 2024", or "every bar" when both bounds are null.
export function windowText(start: number | null | undefined, end: number | null | undefined): string {
  if (start == null && end == null) return "every bar";
  return `${dayLong(start)} to ${dayLong(end)}`;
}

export const hasPrices = (m: Covered): boolean => m.bars != null && m.first_bar_ts != null && m.last_bar_ts != null;

// The members' bars added up: the read's own count, 0 for a member with none.
export const barsSum = (members: Covered[]): number => members.reduce((a, m) => a + (m.bars ?? 0), 0);

// 1,000,000 and up as "4.45M" (two decimals, trailing zeros dropped), else en-US grouping.
export function bigNumber(n: number): string {
  if (n >= 1_000_000) return `${String(Number((n / 1_000_000).toFixed(2)))}M`;
  return n.toLocaleString("en-US");
}

export type YearCount = { year: number; n: number };

// One count per UTC year from 1970 to the current year: the members whose first-to-last bar span touches it.
export function yearCounts(members: Covered[], nowYear: number): YearCount[] {
  const spans = members.filter(hasPrices).map((m) => [utcYear(m.first_bar_ts!), utcYear(m.last_bar_ts!)] as const);
  const out: YearCount[] = [];
  for (let year = FIRST_YEAR; year <= nowYear; year += 1) {
    out.push({ year, n: spans.filter(([a, b]) => a <= year && year <= b).length });
  }
  return out;
}

// A moment as a year with a fraction (UTC), for the axis.
export function yearFraction(epoch: number): number {
  const d = new Date(epoch * 1000);
  const y = d.getUTCFullYear();
  const start = Date.UTC(y, 0, 1);
  const end = Date.UTC(y + 1, 0, 1);
  return y + (d.getTime() - start) / (end - start);
}

// A moment's place on the Prices axis, 0 to 100 percent (1962 to now).
export function axisPercent(epoch: number, now: number): number {
  const span = yearFraction(now) - AXIS_START;
  return Math.min(100, Math.max(0, ((yearFraction(epoch) - AXIS_START) / span) * 100));
}

export type YearBySector = { year: number; n: number; by: number[] };

// One row per UTC year from 1970 to the current year: the members whose first-to-last bar span touches it, in
// all (n) and per sector key (by, in the order of the keys list).
export function yearCountsBySector<T extends Covered>(
  members: T[], nowYear: number, keys: string[], keyOf: (m: T) => string,
): YearBySector[] {
  const spans = members
    .filter(hasPrices)
    .map((m) => ({ a: utcYear(m.first_bar_ts!), b: utcYear(m.last_bar_ts!), k: keys.indexOf(keyOf(m)) }));
  const out: YearBySector[] = [];
  for (let year = FIRST_YEAR; year <= nowYear; year += 1) {
    const by = keys.map(() => 0);
    let n = 0;
    for (const s of spans) {
      if (s.a <= year && year <= s.b) {
        n += 1;
        if (s.k >= 0) by[s.k] += 1;
      }
    }
    out.push({ year, n, by });
  }
  return out;
}

// A ticker box: split on spaces, commas and semicolons, upper-cased, each once, in typed order.
export function splitTickers(text: string): string[] {
  return [...new Set(text.toUpperCase().split(/[\s,;]+/).filter(Boolean))];
}
