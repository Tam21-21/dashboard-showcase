import "./chartWindow.css";
import { useCallback, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { HistoryBar, SearchResult } from "../../../../types/display";
import { Segmented, Window, usePageMemory, useBoxSize } from "../../../kit";
import { CHART_PERIODS, DASH, NOT_SERVED, compact, fx } from "./data";
import { YEAR_BARS, MONTH_BARS, average, biggerThan, changeOver, dayChange, shortReason, sma, weekMove, yearToDate } from "./series";
import type { Fig } from "./series";
import type { ChartTarget } from "./Tiles";
import { useBars } from "./useBars";

// The chart window (display_methods.md, Chart window): daily candles, the 50 and 200-day simple
// averages computed over the whole two years so both cover every bar drawn, and a footer for the
// bar under the pointer. Display data only.

const GOOD = "#2fe08a";
const BAD = "#ff5a63";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dayOf = (iso: string) => `${Number(iso.slice(8, 10))} ${MONTHS[Number(iso.slice(5, 7)) - 1]} ${iso.slice(0, 4)}`;
const H = 300;

/** The closes as one line with the 200-day average dashed; the pointer gives the date and both values (mockup: bigLine). */
function PriceChart({ drawn, avg, tone, dp }: { drawn: HistoryBar[]; avg: Array<number | null>; tone: string; dp: number }) {
  const box = useRef<HTMLDivElement>(null);
  const { w } = useBoxSize(box);
  const W = w > 0 ? w : 600;
  const [hov, setHov] = useState<number | null>(null);
  const n = drawn.length;
  const vals = [...drawn.map((b) => b.close), ...avg.filter((v): v is number => v !== null)];
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const x = (i: number) => (n < 2 ? 0 : (i / (n - 1)) * W);
  const y = (v: number) => 18 + (1 - (v - lo) / (hi - lo || 1)) * (H - 26);
  const path = (arr: Array<number | null>) => {
    let d = "";
    let pen = false;
    arr.forEach((v, i) => {
      if (v === null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const closes = drawn.map((b) => b.close);
  const fillPath = `${path(closes)}L${x(n - 1).toFixed(1)} ${H}L${x(0).toFixed(1)} ${H}Z`;
  const at = hov !== null ? drawn[hov] : null;
  const avgAt = hov !== null ? avg[hov] : null;
  return (
    <div
      ref={box} className="mk-chart dt-chart" style={{ height: H }} data-testid="price-chart"
      onMouseMove={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        setHov(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1)))));
      }}
      onMouseLeave={() => setHov(null)}
    >
      <svg className="mk-svg" viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
        <defs><linearGradient id="mk-win-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={tone} stopOpacity=".4" /><stop offset="1" stopColor={tone} stopOpacity="0" /></linearGradient></defs>
        <path d={fillPath} fill="url(#mk-win-fill)" stroke="none" />
        <path className="avg" d={path(avg)} />
        <path className="ln" d={path(closes)} style={{ color: tone }} />
        <circle className="end" cx={W - 1} cy={y(closes[n - 1])} r="4.5" style={{ color: tone }} />
        {hov !== null && at && <line className="xh" x1={x(hov)} x2={x(hov)} y1="0" y2={H} />}
        {hov !== null && at && <circle className="dot" cx={x(hov)} cy={y(at.close)} r="5" style={{ fill: tone }} />}
      </svg>
      {at && (
        <div className="mk-tipbox" data-testid="chart-foot" style={{ left: x(hov as number) > W / 2 ? undefined : x(hov as number) + 12, right: x(hov as number) > W / 2 ? W - x(hov as number) + 12 : undefined }}>
          <b>{dayOf(at.time)}</b>
          <span>Close <em className="num">{fx(at.close, dp)}</em></span>
          {avgAt != null && <span>200-day average <em className="num">{fx(avgAt, dp)}</em></span>}
          <span title={at.volume.toLocaleString("en-US")}>Volume <em className="num">{compact(at.volume)}</em></span>
        </div>
      )}
    </div>
  );
}

export default function ChartWindow({ start, onClose }: { start: ChartTarget; onClose: () => void }) {
  const [win, setWin, clearWin] = usePageMemory<{ target: ChartTarget; period: string; typed: string; searched: string | null }>(
    "research/markets/window", { target: start, period: "1y", typed: "", searched: null },
  );
  const { target, period, typed, searched } = win;
  const setTarget = (t: ChartTarget) => setWin((w) => ({ ...w, target: t }));
  const setPeriod = (p: string) => setWin((w) => ({ ...w, period: p }));
  const setTyped = (t: string) => setWin((w) => ({ ...w, typed: t }));
  const setSearched = (q: string | null) => setWin((w) => ({ ...w, searched: q }));
  // Closing the window forgets it, so the next tile opens fresh; leaving the page keeps it.
  const close = useCallback(() => { clearWin(); onClose(); }, [clearWin, onClose]);

  const symbols = useMemo(() => (target.symbol ? [target.symbol] : []), [target.symbol]);
  const state = useBars(symbols).get(target.symbol ?? "");
  const searchQ = useQuery({
    queryKey: ["display", "search", searched],
    queryFn: () => api.display.search(searched as string),
    enabled: !!searched,
    retry: false,
  });

  const bars = state?.bars ?? null;
  const n = CHART_PERIODS.find((p) => p.value === period)?.bars ?? 252;
  const view = useMemo(() => {
    if (!bars || bars.length === 0) return null;
    const first = Math.max(0, bars.length - n);
    return { drawn: bars.slice(first), avg: sma(bars.map((b) => b.close), 200).slice(first) };
  }, [bars, n]);

  let emptyWords: string | null = null;
  if (target.symbol === null) emptyWords = NOT_SERVED;
  else if (state?.bars === null) emptyWords = state.reason;
  else if (bars && bars.length === 0) emptyWords = shortReason(0, 1);

  const price = (v: number) => fx(v, target.dp);
  const found = (searchQ.data?.results ?? []).filter((r) => !!r.symbol);
  const last = bars && bars.length > 0 ? bars[bars.length - 1] : null;
  const dayF = bars ? dayChange(bars) : null;
  const yearBars = bars ? bars.slice(-(YEAR_BARS + 1)) : [];
  const yHi = yearBars.length ? yearBars.reduce((m, b) => (b.close > m.close ? b : m)) : null;
  const yLo = yearBars.length ? yearBars.reduce((m, b) => (b.close < m.close ? b : m)) : null;
  const avg200 = bars ? average(bars, 200) : null;
  const week = bars ? weekMove(bars) : null;
  const yearF = bars ? changeOver(bars, YEAR_BARS) : null;
  const above = last && avg200 && avg200.value !== null ? last.close >= avg200.value : null;
  const tone = yearF && yearF.value !== null && yearF.value < 0 ? BAD : GOOD;
  const pct = (f: Fig | null) => (f && f.value !== null ? `${f.value > 0 ? "+" : ""}${f.value.toFixed(1)}%` : DASH);
  const cls = (f: Fig | null) => (f && f.value !== null && f.value !== 0 ? (f.value > 0 ? "good" : "bad") : "");
  const fact = (label: string, value: string, c = "", hover?: string, note?: string) => (
    <div key={label} title={hover ?? `Source: the display API's daily closes (${target.symbol ?? ""})`}><dt>{label}</dt><dd className={`num ${c}`.trim()}>{value}{note && <small>{note}</small>}</dd></div>
  );

  function runSearch() {
    const q = typed.trim();
    if (q) setSearched(q);
  }
  function pick(r: SearchResult) {
    setTarget({ name: r.name ?? r.symbol, symbol: r.symbol, dp: 2 });
    setSearched(null);
    setTyped("");
  }

  return (
    <Window label={`${target.name} chart`} size="xl" className="mk-win" onClose={close}>
      <div className="win-h">
        <h2>{target.name}</h2>
        <button type="button" className="btn q" onClick={close}>Close</button>
      </div>
      <div className="ovs">
        <input
          className="mk-search"
          aria-label="Search a ticker, a company or an ISIN"
          placeholder="Search a ticker or name"
          value={typed}
          autoFocus={!target.symbol}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") runSearch(); }}
        />
        {searched && (
          <div className="sug">
            {searchQ.isPending && searchQ.fetchStatus !== "idle" && <div className="empty">Searching.</div>}
            {searchQ.isError && <div className="empty">{`${serverDetail(searchQ.error)} Until the price service accepts this server's key again, no ticker can be looked up.`}</div>}
            {searchQ.isSuccess && found.length === 0 && <div className="empty">No match for that word. Try the ticker or more of the name.</div>}
            {found.map((r) => (
              <button key={r.symbol} type="button" className="sugrow" onClick={() => pick(r)}>
                <span>{r.name ?? r.symbol}</span>
                <span className="num">{r.symbol}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      {emptyWords && !searched && <div className="empty">{emptyWords}</div>}
      {!emptyWords && !view && <div className="empty">Loading.</div>}
      {view && last && (
        <div className="dt">
          <div className="dt-top">
            <div className="mk-fig"><span className="fig">{price(last.close)}</span><span className={`num mk-day ${cls(dayF)}`}>{pct(dayF)}</span></div>
            {above !== null && <span className={`st ${above ? "good" : "bad"}`} title={`200-day average ${avg200 && avg200.value !== null ? price(avg200.value) : DASH}`}>{above ? "Above its 200-day average" : "Below its 200-day average"}</span>}
          </div>
          <PriceChart drawn={view.drawn} avg={view.avg} tone={tone} dp={target.dp} />
          <div className="axis"><span>{dayOf(view.drawn[0].time)}</span><Segmented options={CHART_PERIODS.map((p) => ({ label: p.label, value: p.value }))} value={period} onChange={setPeriod} /><span>{dayOf(last.time)}</span></div>
          <dl className="dt-facts" data-testid="chart-legend">
            {fact(`Close, ${dayOf(last.time)}`, price(last.close))}
            {fact("Day", pct(dayF), cls(dayF))}
            {week && week.value ? fact(`7 days, from ${dayOf(week.value.start.time)}`, `${week.value.move > 0 ? "+" : ""}${week.value.move.toFixed(1)}%`, week.value.move > 0 ? "good" : week.value.move < 0 ? "bad" : "", undefined, week.value.smaller === week.value.count ? "its biggest week in a year" : `${biggerThan(week.value)}% of its weeks were calmer`) : fact("7 days", DASH, "", week?.reason)}
            {fact("1 month", pct(bars ? changeOver(bars, MONTH_BARS) : null), cls(bars ? changeOver(bars, MONTH_BARS) : null))}
            {fact("1 year", pct(yearF), cls(yearF))}
            {fact("This year", pct(bars ? yearToDate(bars) : null), cls(bars ? yearToDate(bars) : null))}
            {fact("200-day average", avg200 && avg200.value !== null ? price(avg200.value) : DASH, "", avg200?.reason ?? undefined)}
            {yHi && fact(`Highest close of the year, ${dayOf(yHi.time)}`, price(yHi.close))}
            {yLo && fact(`Lowest close of the year, ${dayOf(yLo.time)}`, price(yLo.close))}
          </dl>
        </div>
      )}
    </Window>
  );
}
