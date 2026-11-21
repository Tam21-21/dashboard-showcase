import { Block, Segmented, Spark, usePageMemory } from "../../../kit";
import { DASH, SECTORS, TILES, WORLD, signedPct, toneOf } from "./data";
import { MONTH_BARS, YEAR_BARS, biggerThan, changeOver, dayChange, weekMove } from "./series";
import type { WeekMove } from "./series";
import { viewOf } from "./Tiles";
import type { ChartTarget, TileSources } from "./Tiles";
import { figFrom } from "./useBars";
import type { BarsMap } from "./useBars";
import type { TileDef } from "./data";

// The Markets wall (mockup: lab-markets.js, the wall). One tile for every market: its last close, its year as a line, and four
// moves (day, 7 days, 1 month, 1 year). Pressing a tile opens its chart. The order is the biggest 7-day move first, or the
// page's usual order. The headline says the week in one sentence, by the This week method in
// docs/research_lab/display_methods.md (Markets page: This week). Every figure is computed from the daily closes the server
// serves; a figure with no source is a dash with the reason on hover, and a tile with no line says so.

const GOOD = "#2fe08a";
const BAD = "#ff5a63";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dayMon = (s: number) => { const d = new Date(s * 1000); return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`; };
const dateOf = (s: number) => `${dayMon(s)} ${new Date(s * 1000).getUTCFullYear()}`;
// One decimal, signed by the shown value: a move that rounds to 0.0% is neither up nor down.
const shown = (v: number) => Number(v.toFixed(1));
const pct1 = (v: number) => `${shown(v) > 0 ? "+" : shown(v) < 0 ? "-" : ""}${Math.abs(shown(v)).toFixed(1)}%`;
const hueOf = (v: number) => (shown(v) > 0 ? "good" : shown(v) < 0 ? "bad" : "dim");

type Cellv = { label: string; value: number | null; text: string; why?: string };
type Item = {
  key: string;
  name: string;
  target: ChartTarget;
  price: string;
  priceWhy?: string;
  hue: "good" | "bad" | "";
  closes: number[] | null;
  cells: Cellv[];
  week: WeekMove | null;
  none: string;
  hover: string;
};

const DEFS: TileDef[] = [
  ...TILES,
  ...WORLD.filter((w) => !TILES.some((t) => t.kind === "bars" && t.symbol === w.symbol)).map((w): TileDef => ({ kind: "bars", name: w.name, symbol: w.symbol, dp: w.dp })),
];

function cell(label: string, v: number | null, why: string | null): Cellv {
  return v === null ? { label, value: null, text: DASH, why: why ?? undefined } : { label, value: v, text: signedPct(v, 1) };
}

function itemOf(def: TileDef, src: TileSources): Item {
  const v = viewOf(def, src);
  if (def.kind === "bars") {
    const state = src.bars.get(def.symbol);
    const day = figFrom(state, dayChange);
    const m1 = figFrom(state, (b) => changeOver(b, MONTH_BARS));
    const y1 = figFrom(state, (b) => changeOver(b, YEAR_BARS));
    const wk = state?.bars ? weekMove(state.bars) : { value: null, reason: state?.reason ?? "Loading." };
    const week = wk.value;
    const closes = v.spark && v.spark.closes.length > 1 ? v.spark.closes : null;
    return {
      key: def.symbol, name: def.name, target: v.target,
      price: v.last.text, priceWhy: v.last.title,
      hue: y1.value === null ? "" : y1.value >= 0 ? "good" : "bad",
      closes,
      cells: [cell("Day", day.value, day.reason), cell("7 days", week ? week.move : null, wk.reason), cell("1 month", m1.value, m1.reason), cell("1 year", y1.value, y1.reason)],
      week,
      none: state?.bars === null && state.reason !== "Loading." ? "No data yet" : state?.bars ? "No chart yet" : "Loading",
      hover: [`Source: the display API's daily closes (${def.symbol}).`, week ? `7 days: ${dateOf(week.start.epoch_seconds)} to ${dateOf(week.end.epoch_seconds)}, bigger than ${biggerThan(week)}% of its weeks of the year before.` : "", "Press for the chart."].filter(Boolean).join("\n"),
    };
  }
  if (def.kind === "yield") {
    const t = src.tenors?.find((x) => x.tenor === "10Y");
    return {
      key: "yield", name: def.name, target: v.target,
      price: v.last.text, priceWhy: v.last.title, hue: "", closes: null,
      cells: [{ label: "Day", value: t ? t.change_bps : null, text: v.day.text, why: v.day.title }],
      week: null, none: t ? "No chart yet" : "No data yet",
      hover: `Source: the US Treasury 10-year yield from FRED${t ? `, level of ${t.observation_date}` : ""}.`,
    };
  }
  return {
    key: def.name, name: def.name, target: v.target,
    price: v.last.text, priceWhy: v.last.title, hue: "", closes: null,
    cells: [{ label: "Day", value: v.day.text === DASH ? null : Number(v.day.text.replace("%", "")), text: v.day.text, why: v.day.title }],
    week: null, none: v.last.text === DASH ? "No data yet" : "No chart yet",
    hover: `Source: ${def.name} from FMP, printed ${v.date ?? "-"}.`,
  };
}

function Tile({ it, onPick }: { it: Item; onPick: (t: ChartTarget) => void }) {
  const hasChart = it.closes !== null;
  const yieldDay = it.key === "yield";
  return (
    <button
      type="button" className="mw-tile" data-testid="wall-tile" data-hue={it.hue || undefined} data-name={it.name}
      title={it.hover} onClick={() => it.target.symbol && onPick(it.target)} disabled={!it.target.symbol}
    >
      <span className="t-name">{it.name}</span>
      <span className="fig num" title={it.priceWhy}>{it.price}</span>
      <span className="t-chart">
        {hasChart ? <Spark values={it.closes!} colour={it.hue === "bad" ? BAD : GOOD} fill={0.36} height={84} strokeWidth={2} dot={false} /> : <span className="t-none">{it.none}</span>}
      </span>
      <span className="t-chg">
        {it.cells.map((c) => (
          <span key={c.label}>
            <i>{c.label}</i>
            <b className={`num ${c.value === null ? "dim" : toneOf(c.value) === "g" ? "good" : toneOf(c.value) === "rd" ? "bad" : ""}`} title={c.why}>
              {yieldDay && c.value !== null ? `${c.text}` : c.text}
            </b>
          </span>
        ))}
      </span>
    </button>
  );
}

export default function Wall({ sources, onPick }: { sources: TileSources; onPick: (t: ChartTarget) => void }) {
  const [order, setOrder] = usePageMemory<string>("research/markets/wall-order", "week");
  const items = DEFS.map((d) => itemOf(d, sources));
  const listed = order === "week"
    ? [...items.keys()].sort((x, y) => (items[y].week ? Math.abs(items[y].week!.move) : -1) - (items[x].week ? Math.abs(items[x].week!.move) : -1) || x - y).map((i) => items[i])
    : items;

  const sectors = weeksOf(SECTORS.map((s) => ({ name: s.name, symbol: s.symbol })), sources.bars);
  const markets = items.filter((i) => i.week).map((i) => ({ name: i.name, w: i.week! }));
  const drawn = [...markets, ...sectors];
  const ends = [...new Set(drawn.map((r) => r.w.end.epoch_seconds))].sort((a, b) => a - b);

  let say;
  if (drawn.length === 0) {
    const why = items.find((i) => i.cells[0].why)?.cells[0].why;
    say = <p className="mw-say">{!why || why === "Loading." ? "Loading the week." : <>No market's week can be drawn yet: <span className="dim">{why}</span></>}</p>;
  } else {
    const top = [...drawn].sort((a, b) => b.w.rank - a.w.rank || Math.abs(b.w.move) - Math.abs(a.w.move));
    const verb = (m: number) => (shown(m) === 0 ? "held flat" : `${m < 0 ? "fell" : "rose"} ${pct1(Math.abs(m)).replace("+", "")}`);
    const lead = (r: { name: string; w: WeekMove }) => <em className={hueOf(r.w.move)}>{`${r.name} ${verb(r.w.move)}`}</em>;
    const calmer = (w: WeekMove) => (w.smaller === w.count ? "its biggest week in a year" : `${biggerThan(w)}% of its weeks were calmer`);
    const up = drawn.filter((r) => shown(r.w.move) > 0).length;
    say = (
      <p className="mw-say" data-testid="wall-say">
        {lead(top[0])}{`: ${calmer(top[0].w)}. `}
        {top[1] && <>{lead(top[1])}{". "}</>}
        {`${up} of ${drawn.length} markets and sectors rose this week.`}
      </p>
    );
  }

  return (
    <Block
      title={ends.length > 0
        ? <span title={`7 days to ${dayMon(ends[ends.length - 1])}. Each market from its newest close (${ends.map(dayMon).join(", ")}) back to its newest close 7 or more days before`}>Markets</span>
        : "Markets"}
      className="mw"
      right={<Segmented variant="seg" value={order} onChange={setOrder} options={[
        { label: "Biggest week first", value: "week", hover: "The largest 7-day move comes first, up or down" },
        { label: "Usual order", value: "usual", hover: "The page's own order" },
      ]} />}
    >
      {say}
      <div className="mw-wall">
        {listed.map((it) => <Tile key={it.key} it={it} onPick={onPick} />)}
      </div>
    </Block>
  );
}

function weeksOf(defs: Array<{ name: string; symbol: string }>, bars: BarsMap): Array<{ name: string; w: WeekMove }> {
  return defs.flatMap((d) => {
    const st = bars.get(d.symbol);
    const r = st?.bars ? weekMove(st.bars) : null;
    return r?.value ? [{ name: d.name, w: r.value }] : [];
  });
}
