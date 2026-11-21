import type { YieldTenor, CommodityRow } from "../../../../types/display";

// Lists and small helpers for the Markets page. Symbols follow docs/research_lab/display_methods.md
// (Markets page): the codes are EODHD's own, so an index carries .INDX.

export const NOT_SERVED = "Daily history for this series is not served yet";

export type TileDef =
  | { kind: "bars"; name: string; symbol: string; dp: number }
  | { kind: "yield"; name: string }
  | { kind: "commodity"; name: string; match: RegExp };

export const TILES: TileDef[] = [
  { kind: "bars", name: "S&P 500", symbol: "GSPC.INDX", dp: 2 },
  { kind: "bars", name: "Euro Stoxx 50", symbol: "STOXX50E.INDX", dp: 2 },
  { kind: "bars", name: "Nikkei 225", symbol: "N225.INDX", dp: 0 },
  { kind: "yield", name: "US 10-year yield" },
  { kind: "bars", name: "Dollar index", symbol: "DXY.INDX", dp: 2 },
  { kind: "commodity", name: "Oil Brent", match: /brent/i },
  { kind: "commodity", name: "Gold", match: /gold/i },
  { kind: "bars", name: "VIX", symbol: "VIX.INDX", dp: 2 },
];

export const WORLD: { name: string; symbol: string; dp: number }[] = [
  { name: "S&P 500", symbol: "GSPC.INDX", dp: 2 },
  { name: "Nasdaq 100", symbol: "NDX.INDX", dp: 2 },
  { name: "Euro Stoxx 50", symbol: "STOXX50E.INDX", dp: 2 },
  { name: "CAC 40", symbol: "FCHI.INDX", dp: 2 },
  { name: "DAX", symbol: "GDAXI.INDX", dp: 2 },
  { name: "Nikkei 225", symbol: "N225.INDX", dp: 0 },
  { name: "Hang Seng", symbol: "HSI.INDX", dp: 2 },
  { name: "Shanghai", symbol: "000001.SHG", dp: 2 },
  { name: "Bitcoin", symbol: "BTC-USD.CC", dp: 0 },
];

export const VIX_SYMBOL = "VIX.INDX";

export const SECTORS: { symbol: string; name: string }[] = [
  { symbol: "XLK", name: "Technology" },
  { symbol: "XLI", name: "Industrials" },
  { symbol: "XLF", name: "Banks and finance" },
  { symbol: "XLV", name: "Health care" },
  { symbol: "XLE", name: "Energy" },
  { symbol: "XLY", name: "Shops and leisure" },
  { symbol: "XLP", name: "Everyday goods" },
  { symbol: "XLRE", name: "Property" },
  { symbol: "XLC", name: "Media and telecoms" },
  { symbol: "XLU", name: "Utilities" },
  { symbol: "XLB", name: "Materials" },
];

export const SECTOR_PERIODS: { label: string; value: string; bars: number }[] = [
  { label: "Day", value: "day", bars: 1 },
  { label: "Week", value: "week", bars: 5 },
  { label: "Month", value: "month", bars: 21 },
];

export const CHART_PERIODS: { label: string; value: string; bars: number }[] = [
  { label: "1M", value: "1m", bars: 21 },
  { label: "3M", value: "3m", bars: 63 },
  { label: "1Y", value: "1y", bars: 252 },
];

/** Every symbol the page reads daily bars for. */
export const BAR_SYMBOLS: string[] = [
  ...new Set([
    ...TILES.flatMap((t) => (t.kind === "bars" ? [t.symbol] : [])),
    ...WORLD.map((w) => w.symbol),
    ...SECTORS.map((s) => s.symbol),
  ]),
];

export const DASH = "-";

/** "+0.82%" or "-0.47%"; a dash when there is no number. */
export function signedPct(v: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return DASH;
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function toneOf(v: number | null): "g" | "rd" | "" {
  if (v == null || v === 0) return "";
  return v > 0 ? "g" : "rd";
}

export function fx(v: number, dp: number): string {
  return v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

export function clockWord(when: string | number): string {
  const d = new Date(typeof when === "number" ? when * 1000 : when);
  if (Number.isNaN(d.getTime())) return DASH;
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

/** 63900000 reads 63.9M; the exact count is for a hover. */
export function compact(n: number): string {
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** 10 year minus 2 year, from the same response; null with the reason when a tenor is missing. */
export function spreadOf(
  tenors: YieldTenor[],
): { points: number; bps: number; reason: null } | { points: null; bps: null; reason: string } {
  const t2 = tenors.find((t) => t.tenor === "2Y");
  const t10 = tenors.find((t) => t.tenor === "10Y");
  if (!t2 || !t10) {
    const missing = !t2 && !t10 ? "the 2 year and 10 year rows" : !t2 ? "the 2 year row" : "the 10 year row";
    return { points: null, bps: null, reason: `The rates feed did not return ${missing}.` };
  }
  return { points: t10.level - t2.level, bps: t10.change_bps - t2.change_bps, reason: null };
}

/** A short name from the route's own text: "Crude Oil Prices: WTI" becomes "WTI". */
export function commodityName(full: string): string {
  const afterColon = full.includes(":") ? (full.split(":").pop() ?? full).trim() : full;
  const afterDash = afterColon.split(" - ")[0].trim();
  return afterDash
    .replace(/^Global Price( Index)? of\s*/i, "")
    .replace(/^Henry Hub\s*/i, "")
    .replace(/\s*Spot Price$/i, "")
    .replace(/^Natural\s+/i, "")
    .replace(/^All Commodities$/i, "All commodities")
    .trim();
}

/** The change as a percent of the previous print: the route's change is newest minus previous. */
export function commodityPct(c: CommodityRow): number | null {
  const previous = c.level - c.change;
  return previous === 0 ? null : (c.change / previous) * 100;
}
