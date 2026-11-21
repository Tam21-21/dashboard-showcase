import { useMemo, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { HistoryBar, HistoryResponse } from "../../../../types/display";
import { Block, Segmented, State, useLayout } from "../../../kit";
import { LibCard } from "../markets/LibCard";
import { fx } from "../markets/data";
import { MONTH_BARS, YEAR_BARS, average, changeOver, dayChange, lastClose, sma, yearRange, yearToDate } from "../markets/series";
import type { Fig } from "../markets/series";
import PriceLine from "./PriceLine";
import type { LinePoint } from "./PriceLine";
import { dayWord, moved } from "./names";

// The share price part of the lookup window: the first sentence, the Share price card and the panel with its line (display_methods.md,
// "Ticker lookup window", Price and Price chart). Every figure is a function of the closes served; a figure with a reason is a
// dash with the reason on hover and its clause left out of the sentence. Display data only.

const FIVE_YEARS = 1260;
const DASH = "-";
const BACK: Record<string, number> = { "1m": MONTH_BARS, "3m": 63, "1y": YEAR_BARS, "5y": FIVE_YEARS };
const MAX_POINTS = 1400;
const PERIODS = [
  { label: "1D", value: "1d", hover: "Hourly, the last session and any trading since" },
  { label: "1W", value: "1w", hover: "Hourly, the last seven days" },
  { label: "1M", value: "1m" },
  { label: "3M", value: "3m" },
  { label: "1Y", value: "1y" },
  { label: "5Y", value: "5y" },
  { label: "Max", value: "max" },
];

const tone = (v: number | null) => (v === null || v === 0 ? "" : v > 0 ? "good" : "bad");
const pct = (f: Fig) => (f.value === null ? DASH : `${f.value > 0 ? "+" : ""}${f.value.toFixed(1)}%`);

type CardProps = ComponentProps<typeof LibCard>;

export type PriceParts = { lead: ReactNode; more: ReactNode; card: CardProps; counts: { closes: number; since: string; source: string } };

/** The sentence, the card and the panel's head, from the daily closes (oldest first). */
export function priceParts(short: string, bars: HistoryBar[], answer: HistoryResponse): PriceParts {
  const last = lastClose(bars);
  const day = dayChange(bars);
  const year = changeOver(bars, YEAR_BARS);
  const avg = average(bars, 200);
  const range = yearRange(bars);
  const lastBar = bars[bars.length - 1];
  const above = last.value !== null && avg.value !== null ? last.value >= avg.value : null;
  const lead = (
    <>
      {short} closed at {last.value === null ? DASH : fx(last.value, 2)}
      {day.value !== null && <>, <em className={tone(day.value)}>{moved(day.value)} on the day</em></>}.
    </>
  );
  const yearClause = year.value === null ? null : <em className={tone(year.value)}>{moved(year.value)} over a year</em>;
  const avgClause = above === null ? null : <em className={above ? "good" : "bad"}>{above ? "above" : "below"} its 200-day average</em>;
  const more = yearClause && avgClause ? <>It is {yearClause} and sits {avgClause}.</>
    : yearClause ? <>It is {yearClause}.</>
      : avgClause ? <>It sits {avgClause}.</>
        : null;

  const closes = bars.slice(-YEAR_BARS).map((b) => b.close);
  const lo = Math.min(...closes);
  const hi = Math.max(...closes);
  const line = closes.map((x, k) => `${k ? "L" : "M"}${((k / Math.max(1, closes.length - 1)) * 300).toFixed(1)} ${(3 + (1 - (x - lo) / (hi - lo || 1)) * 50).toFixed(1)}`).join("");
  const spkTone = year.value === null ? "" : year.value >= 0 ? "good" : "bad";
  const fact = (label: string, v: number | null, reason: string | null, hover?: string) => ({ label, value: v === null ? DASH : fx(v, 2), hover: v === null ? (reason ?? undefined) : hover });
  const card: CardProps = {
    name: "Share price",
    hue: above === null ? "data" : above ? "good" : "bad",
    chip: above === null
      ? { tone: "dim", text: "No 200-day average", hover: avg.reason ?? undefined }
      : { tone: above ? "good" : "bad", text: `${above ? "Above" : "Below"} its 200-day average` },
    fig: last.value === null ? DASH : fx(last.value, 2),
    unit: `close of ${dayWord(answer.last_day ?? lastBar.time)}, in US dollars`,
    pic: <span className={`lk-spk ${spkTone}`}><svg viewBox="0 0 300 56" preserveAspectRatio="none" aria-hidden="true"><path d={line} /></svg></span>,
    facts: [
      fact("Lowest in a year", range.value?.low ?? null, range.reason, "The lowest close of the last 252 sessions"),
      fact("200-day average", avg.value, avg.reason, "The average of the last 200 closes"),
      fact("Highest in a year", range.value?.high ?? null, range.reason, "The highest close of the last 252 sessions"),
    ],
    testId: "lk-card-prices",
  };
  return { lead, more, card, counts: { closes: bars.length, since: dayWord(bars[0].time), source: sourceWords(answer) } };
}

/** Where the closes come from, for the panel title's hover. */
function sourceWords(a: HistoryResponse): string {
  const left = Object.values(a.left_out ?? {}).reduce((s, n) => s + n, 0);
  const parts = [
    `Source: ${a.source ?? "the display API"}${a.last_day ? ` to ${dayWord(a.last_day)}` : ""}`,
    a.join ? `joined on ${dayWord(a.join.day)} (the store's closes times ${a.join.scale})` : null,
    a.fill_error ? `no join: ${a.fill_error}` : null,
    `${left} days left out by the cleaning`,
  ];
  return parts.filter(Boolean).join(", ");
}

/** The points to draw for a daily period: counted back from the last bar, thinned to at most 1400, the last one always kept. */
export function seriesFor(bars: HistoryBar[], period: string): { points: LinePoint[]; avg: Array<number | null> } {
  const n = bars.length;
  const from = period === "max" ? 0 : Math.max(0, n - 1 - BACK[period]);
  const step = Math.max(1, Math.ceil((n - from) / MAX_POINTS));
  const idx: number[] = [];
  for (let i = from; i < n; i += step) idx.push(i);
  if (idx[idx.length - 1] !== n - 1) idx.push(n - 1);
  const all = sma(bars.map((b) => b.close), 200);
  return { points: idx.map((i) => ({ time: bars[i].time, close: bars[i].close, regular: true })), avg: idx.map((i) => all[i]) };
}

/** The price panel: the line with its period buttons, and the row of changes. */
export function PricePanel({ symbol, bars, answer }: { symbol: string; bars: HistoryBar[]; answer: HistoryResponse }) {
  const [period, setPeriod] = useState("1y");
  const phone = useLayout() === "phone";
  const hourlyAsk = period === "1d" || period === "1w";
  const hours = useQuery({
    queryKey: ["display", "history", symbol, period, "hour"],
    queryFn: () => api.display.getHistory(symbol, period, "hour"),
    enabled: hourlyAsk,
    retry: false,
  });
  const daily = useMemo(() => (hourlyAsk ? null : seriesFor(bars, period)), [bars, period, hourlyAsk]);
  const hourly = useMemo<LinePoint[] | null>(
    () => (hours.data ? hours.data.bars.map((b) => ({ time: b.time, close: b.close, regular: b.regular !== false })) : null),
    [hours.data],
  );

  const day = dayChange(bars);
  const ytd = yearToDate(bars);
  const prev = bars.length ? Number(bars[bars.length - 1].time.slice(0, 4)) - 1 : 0;
  const yEnd = [...bars].reverse().find((b) => b.time.slice(0, 4) === String(prev));
  const rows: Array<[string, Fig, string?]> = [
    ["Day", day],
    ["1 month", changeOver(bars, MONTH_BARS)],
    [yEnd ? `This year, since ${dayWord(yEnd.time)}` : "This year", ytd],
    ["1 year", changeOver(bars, YEAR_BARS)],
    ["5 years", changeOver(bars, FIVE_YEARS)],
  ];

  let chart: ReactNode;
  if (hourlyAsk && hours.isError) chart = <div className="empty">{serverDetail(hours.error)}</div>;
  else if (hourlyAsk && !hourly) chart = <i className="lk-bone b-chart" aria-hidden="true" />;
  else if (hourlyAsk && hourly && hourly.length === 0) chart = <div className="empty">No hourly bars came back for this period.</div>;
  else if (hourly && hourlyAsk) chart = <PriceLine points={hourly} hourly height={phone ? 230 : 320} />;
  else if (daily) chart = <PriceLine points={daily.points} avg={daily.avg} hourly={false} height={phone ? 230 : 320} />;

  const src = sourceWords(answer);
  return (
    <Block
      id="lk-prices"
      title={<span title={`The line is each day's close, green when the stretch shown ends above its start and red when below. The thin grey line is the average of the last 200 closes. On 1D and 1W the line is hourly, in Paris time, and fainter outside regular hours. ${src}`}>Share price</span>}
      state={<><State tone="run">{`${bars.length.toLocaleString("en-US")} daily closes`}</State><State tone="dim">{`since ${dayWord(bars[0].time)}`}</State></>}
      right={<Segmented variant="seg" options={PERIODS} value={period} onChange={setPeriod} />}
    >
      {chart}
      <dl className="lk-kv">
        {rows.map(([label, f]) => (
          <div key={label} title={f.value === null ? f.reason : undefined}><dt>{label}</dt><dd className={`num ${tone(f.value)}`.trim()}>{pct(f)}</dd></div>
        ))}
      </dl>
    </Block>
  );
}
