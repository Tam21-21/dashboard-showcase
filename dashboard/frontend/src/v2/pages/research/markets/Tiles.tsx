import type { CommodityRow, YieldTenor } from "../../../../types/display";
import { DASH, NOT_SERVED, commodityPct, fx, signedPct, toneOf } from "./data";
import type { TileDef } from "./data";
import { AVERAGE_BARS, MONTH_BARS, YEAR_BARS, average, changeOver, dayChange, lastClose, sma } from "./series";
import type { Fig } from "./series";
import { figFrom } from "./useBars";
import type { BarsMap } from "./useBars";

/** What the chart window needs to open for a tile or a row. A null symbol is a series with no daily bars. */
export type ChartTarget = { name: string; symbol: string | null; dp: number };

type Cell = { text: string; tone: "g" | "rd" | ""; title: string | undefined };
export type View = {
  /** The date the figures stand on (close, level or print); null when there is none. */
  date: string | null;
  name: string;
  target: ChartTarget;
  last: Cell;
  day: Cell;
  m1: Cell;
  y1: Cell;
  word: Cell;
  grain: string | null;
  spark: { closes: number[]; average: Array<number | null> } | null;
};

const dash = (reason: string): Cell => ({ text: DASH, tone: "", title: reason });
const fromFig = (f: Fig, text: (v: number) => string, tone: boolean, title?: string): Cell =>
  f.value === null ? dash(f.reason) : { text: text(f.value), tone: tone ? toneOf(f.value) : "", title };
const pct1 = (v: number) => signedPct(v, 1);

const NO_ROW = "The server does not serve this series yet.";

export type TileSources = {
  bars: BarsMap;
  tenors: YieldTenor[] | undefined;
  yieldError: string | null;
  commodities: CommodityRow[] | undefined;
  commoditiesError: string | null;
};

export function viewOf(def: TileDef, src: TileSources): View {
  if (def.kind === "bars") {
    const state = src.bars.get(def.symbol);
    const last = figFrom(state, lastClose);
    const avg = figFrom(state, (b) => average(b, AVERAGE_BARS));
    let word: Cell;
    if (avg.value === null || last.value === null) word = dash((avg.value === null ? avg.reason : last.reason) as string);
    else word = { text: last.value >= avg.value ? "ABOVE AVERAGE" : "BELOW AVERAGE", tone: "", title: `200-day average ${fx(avg.value, def.dp)}` };
    let spark: View["spark"] = null;
    if (state?.bars && state.bars.length > 0) {
      const closes = state.bars.map((b) => b.close);
      spark = { closes: closes.slice(-YEAR_BARS), average: sma(closes, AVERAGE_BARS).slice(-YEAR_BARS) };
    }
    const closeDate = state?.bars && state.bars.length > 0 ? state.bars[state.bars.length - 1].time : null;
    const date = closeDate === null ? undefined : `Close of ${closeDate}`;
    return {
      date: closeDate,
      name: def.name,
      target: { name: def.name, symbol: def.symbol, dp: def.dp },
      last: fromFig(last, (v) => fx(v, def.dp), false, date),
      day: fromFig(figFrom(state, dayChange), pct1, true),
      m1: fromFig(figFrom(state, (b) => changeOver(b, MONTH_BARS)), pct1, true),
      y1: fromFig(figFrom(state, (b) => changeOver(b, YEAR_BARS)), pct1, true),
      word,
      grain: null,
      spark,
    };
  }
  const target: ChartTarget = { name: def.name, symbol: null, dp: 2 };
  const noHistory = dash(NOT_SERVED);
  if (def.kind === "yield") {
    const t = src.tenors?.find((x) => x.tenor === "10Y");
    const why = src.yieldError ?? (src.tenors === undefined ? "Loading." : "The rates feed did not return the 10 year row.");
    return {
      date: t ? t.observation_date : null,
      name: def.name, target,
      last: t ? { text: `${t.level.toFixed(2)}%`, tone: "", title: `Level of ${t.observation_date}` } : dash(why),
      day: t ? { text: `${t.change_bps > 0 ? "+" : ""}${t.change_bps.toFixed(1)} bp`, tone: toneOf(t.change_bps), title: undefined } : dash(why),
      m1: noHistory, y1: noHistory, word: noHistory, grain: null, spark: null,
    };
  }
  const row = src.commodities?.find((c) => def.match.test(c.name));
  const why = src.commoditiesError ?? (src.commodities === undefined ? "Loading." : NO_ROW);
  if (!row) return { date: null, name: def.name, target, last: dash(why), day: dash(why), m1: noHistory, y1: noHistory, word: noHistory, grain: null, spark: null };
  const monthlyRow = row.grain === "monthly";
  const pct = commodityPct(row);
  return {
    date: row.observation_date,
    name: def.name, target,
    last: { text: fx(row.level, 2), tone: "", title: `Printed ${row.observation_date}` },
    day: monthlyRow
      ? dash("A monthly series has no daily change.")
      : pct === null ? dash("The previous print was zero, so there is no percent.") : { text: pct1(pct), tone: toneOf(pct), title: undefined },
    m1: noHistory, y1: noHistory, word: noHistory,
    grain: monthlyRow ? "MONTHLY" : null,
    spark: null,
  };
}
