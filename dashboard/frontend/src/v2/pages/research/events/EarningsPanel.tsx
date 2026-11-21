import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import type { EarningsCloses, EarningsRow } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import { Block, Segmented, StateWord, Window } from "../../../kit";
import type { Tone } from "../../../kit";
import { USE_IN_RUN_WHY } from "./EventDetail";
import { GREEN, RED } from "./eventText";

// The EARNINGS card of the Events tab (mockup: docs/design/rail_mockup_2026-10-10.html, dlEarn and openEarn).
// Method: docs/research_lab/display_methods.md, "Data library: Earnings" and its source change for EA3.
// The release time is shown in Paris time with UTC on hover; closes are display data from Alpaca.

const LIMIT = 60; // the route's own default
const SHOWN = 12;
const SURPRISE_CAP = 0.2;
const REACTION_CAP = 0.1;
const AMBER = "#ffb020";
const GREY = "#c3cbd6";
const FROM = "the Benzinga Newsdesk headline in Alpaca news";
const REACTION_SOURCE = "last close before to first close after";
const NOT_YET = "Not yet: the first close after the release is not in yet";
const NO_SURPRISE = "No surprise with an estimate of zero";
const CLOSES_SOURCE = "daily closes, Alpaca";
const SOURCE_LINE = "Benzinga Newsdesk in Alpaca news; closes from Alpaca";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const FILTERS = [
  { label: "All", value: "all" },
  { label: "Beats", value: "beat" },
  { label: "Misses", value: "miss" },
];

const PARIS = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Paris", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
function paris(iso: string): { day: string; time: string } {
  const p = Object.fromEntries(PARIS.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return { day: `${p.day} ${p.month}`, time: `${p.hour}:${p.minute}` };
}
const parisText = (iso: string) => {
  const p = paris(iso);
  return `${p.day} ${p.time}`;
};
const utcText = (iso: string) => `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`;
const dayText = (date: string) => `${Number(date.slice(8, 10))} ${MONTHS[Number(date.slice(5, 7)) - 1]}`;

// A signed percent to one decimal, rounded on the thousandths so 0.0735 reads 7.4.
function pct(v: number | null): string {
  if (v === null) return "-";
  const tenths = Math.round(Math.abs(v) * 1000) / 10;
  if (tenths === 0) return "0.0%";
  return `${v > 0 ? "+" : "-"}${tenths.toFixed(1)}%`;
}
const tone = (v: number | null): "g" | "rd" | "" => (v === null || v === 0 ? "" : v > 0 ? "g" : "rd");
const eps = (n: number) => `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}`;

// Sales come in dollars; both figures of a row use one unit, set by the larger.
function salesPair(actual: number, estimate: number, digits: number): [string, string] {
  const big = Math.max(Math.abs(actual), Math.abs(estimate));
  const [div, unit] = big >= 1e9 ? [1e9, "B"] : big >= 1e6 ? [1e6, "M"] : [1e3, "K"];
  const f = (n: number) => `${n < 0 ? "-" : ""}$${(Math.abs(n) / div).toFixed(digits)}${unit}`;
  return [f(actual), f(estimate)];
}

function Bar({ v, cap }: { v: number | null; cap: number }) {
  if (v === null) return <span className="ebar" />;
  const half = Math.max(2, Math.min(1, Math.abs(v) / cap) * 50);
  const up = v >= 0;
  const style = v === 0
    ? { left: "50%", width: "2%", background: GREY }
    : { [up ? "left" : "right"]: "50%", width: `${half}%`, background: up ? GREEN : RED };
  return <span className="ebar"><b style={style} /></span>;
}

function resultTone(word: string): Tone {
  return word === "BEAT" ? "g" : word === "MISS" ? "rd" : "none";
}

function reactionColour(reaction: number | null): string {
  return reaction === null ? GREY : reaction >= 0 ? GREEN : RED;
}

// The closes around a release as a small line with a dashed amber line between the two reaction closes.
function Sparkline({ closes, reaction }: { closes: EarningsCloses; reaction: number | null }) {
  const c = closes.values;
  const n = c.length;
  if (n === 0) return <span className="num">-</span>;
  const W = 96, H = 26;
  const mn = Math.min(...c), mx = Math.max(...c);
  const X = (i: number) => 2 + (i * (W - 4)) / Math.max(1, n - 1);
  const Y = (v: number) => H - 3 - ((v - mn) / (mx - mn || 1)) * (H - 6);
  const b = closes.before_index;
  const a = closes.after_index;
  const xr = b === null ? X(0) : (X(b) + X(a ?? b)) / 2;
  const d = c.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="esp">
      <line x1={xr} x2={xr} y1={1} y2={H - 1} stroke={AMBER} strokeDasharray="2 2" opacity={0.8} />
      <path d={d} fill="none" stroke={reactionColour(reaction)} strokeWidth={1.4} />
    </svg>
  );
}

function ticks(mn: number, mx: number, count: number): number[] {
  return Array.from({ length: count }, (_, i) => mn + ((mx - mn) * i) / (count - 1));
}

// The detail's chart: the closes, the dashed amber release line with its Paris time, the two reaction closes circled.
function DetailChart({ row }: { row: EarningsRow }) {
  const { dates, values: c, before_index: ib, after_index: ia } = row.closes;
  const n = c.length;
  if (n === 0) return <div className="ev-none">No closes came back for this ticker.</div>;
  const w = 860, H = 230, L = 8, R = 64, T = 14, B = 26;
  const mn = Math.min(...c), mx = Math.max(...c);
  const pad = (mx - mn) * 0.12 || 1;
  const X = (i: number) => L + (i * (w - L - R)) / Math.max(1, n - 1);
  const Y = (v: number) => T + ((mx + pad - v) * (H - T - B)) / (mx - mn + 2 * pad);
  const xr = ib === null ? X(0) : ia === null ? X(ib) + (w - R - X(ib)) / 2 : (X(ib) + X(ia)) / 2;
  const col = reactionColour(row.reaction);
  const d = c.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const labelled = Array.from(new Set([0, ib ?? 0, n - 1]));
  return (
    <svg viewBox={`0 0 ${w} ${H}`} className="ev-echart" role="img" aria-label="Closes around the release">
      {ticks(mn, mx, 4).map((v) => (
        <g key={v}>
          <line x1={L} x2={w - R} y1={Y(v)} y2={Y(v)} stroke="rgba(255,255,255,.05)" />
          <text x={w - R + 8} y={Y(v) + 4} fontSize={11} fill="#8d98a8">{`$${v.toFixed(2)}`}</text>
        </g>
      ))}
      <path d={d} fill="none" stroke="#c9d2de" strokeWidth={1.8} />
      <line x1={xr} x2={xr} y1={T} y2={H - B} stroke={AMBER} strokeDasharray="4 4" opacity={0.8} />
      <text x={xr} y={T - 2} fontSize={11} fill={AMBER} textAnchor="middle">{`${paris(row.release_at).time} Paris`}</text>
      {[ib, ia].map((i) => i !== null && (
        <circle key={i} data-reaction-close={i} cx={X(i)} cy={Y(c[i])} r={4.5} fill={col} stroke="#060809" strokeWidth={2} />
      ))}
      {c.map((v, i) => (
        <circle key={`h${i}`} cx={X(i)} cy={Y(v)} r={8} fill="transparent"><title>{`${dates[i]}: close $${v.toFixed(2)}`}</title></circle>
      ))}
      {labelled.map((i) => (
        <text key={`t${i}`} x={X(i)} y={H - 6} fontSize={11} fill="#8d98a8" textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>
          {dayText(dates[i])}
        </text>
      ))}
    </svg>
  );
}

function Detail({ row, onClose, onChart }: { row: EarningsRow; onClose: () => void; onChart: (ticker: string) => void }) {
  const title = `${row.company}, ${row.quarter}, ${parisText(row.release_at)}`;
  const [sa, se] = salesPair(row.sales_actual, row.sales_estimate, 3);
  const first = row.first_printed;
  const rows: Array<[string, string, string?]> = [
    ["Released", `${parisText(row.release_at)} Paris, ${row.release_at.slice(11, 19)} UTC`],
    ["Reaction", pct(row.reaction), row.reaction === null ? NOT_YET : undefined],
    ["EPS", row.adjusted ? "adjusted, the company's own figure" : "as reported"],
  ];
  if (row.corrected && row.corrected_at && first) {
    const [fa] = salesPair(first.sales_actual, first.sales_estimate, 3);
    rows.push(["Corrected", `${utcText(row.corrected_at)}, first printed EPS ${eps(first.eps_actual)}, sales ${fa}`]);
  }
  rows.push(["Headline", row.headline], ["Source", SOURCE_LINE]);
  return (
    <Window label={title} size="xl" className="ev-detail" onClose={onClose}>
        <div className="ev-dhead">
          <span className="ev-dtitle">{title.toUpperCase()}</span>
          <span className="ev-grow" />
          <button type="button" className="btn q" onClick={() => onChart(row.ticker)}>OPEN CHART</button>
          <button type="button" className="btn" disabled title={USE_IN_RUN_WHY}>Use in a run</button>
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        </div>
        <div className="ev-big">
          {`EPS ${eps(row.eps_actual)} against ${eps(row.eps_estimate)}, `}
          <span className={tone(row.eps_surprise) || undefined}>{pct(row.eps_surprise)}</span>
          {`. Sales ${sa} against ${se}, `}
          <span className={tone(row.sales_surprise) || undefined}>{pct(row.sales_surprise)}</span>.
        </div>
        <DetailChart row={row} />
        <div className="ev-rows">
          {rows.map(([label, value, why]) => (
            <div key={label}>
              <span>{label}</span>
              <b title={why} className={label === "Reaction" ? tone(row.reaction) || undefined : undefined}>{value}</b>
            </div>
          ))}
        </div>
    </Window>
  );
}

function priceHover(row: EarningsRow): string {
  const { dates, values, before_index: b, after_index: a } = row.closes;
  const lines = ["Closes around the release", `Line: ${CLOSES_SOURCE}`, "Amber: the release"];
  if (b !== null) lines.push(`Before: ${dates[b]}, $${values[b].toFixed(2)}`);
  if (a !== null) lines.push(`After: ${dates[a]}, $${values[a].toFixed(2)}`);
  return lines.join("\n");
}

function releasedHover(row: EarningsRow): string {
  const lines = [`${parisText(row.release_at)} Paris`, utcText(row.release_at)];
  if (row.corrected && row.corrected_at) lines.push(`Corrected ${utcText(row.corrected_at)}`);
  return lines.join("\n");
}

function epsHover(row: EarningsRow): string {
  const lines = [row.adjusted ? "Adjusted EPS" : "EPS", `Actual: ${eps(row.eps_actual)}`, `Estimate: ${eps(row.eps_estimate)}`];
  if (row.first_printed && row.first_printed.eps_actual !== row.eps_actual) lines.push(`First printed ${eps(row.first_printed.eps_actual)}`);
  return lines.join("\n");
}

function salesHover(row: EarningsRow): string {
  const [a, e] = salesPair(row.sales_actual, row.sales_estimate, 3);
  const lines = ["Sales", `Actual: ${a}`, `Estimate: ${e}`, `Surprise: ${pct(row.sales_surprise)}`];
  if (row.first_printed && row.first_printed.sales_actual !== row.sales_actual) {
    lines.push(`First printed ${salesPair(row.first_printed.sales_actual, row.first_printed.sales_estimate, 3)[0]}`);
  }
  return lines.join("\n");
}

function reactionHover(row: EarningsRow): string {
  const { dates, values, before_index: b, after_index: a } = row.closes;
  if (row.reaction === null || b === null || a === null) return NOT_YET;
  return ["Reaction", `Before: ${dates[b]}, $${values[b].toFixed(2)}`, `After: ${dates[a]}, $${values[a].toFixed(2)}`].join("\n");
}

export default function EarningsPanel({ onChart }: { onChart: (ticker: string) => void }) {
  const q = useQuery({ queryKey: ["events", "earnings", LIMIT], queryFn: () => api.research.earnings(LIMIT) });
  const [filter, setFilter] = useState("all");
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<EarningsRow | null>(null);

  const rows = q.data?.rows ?? [];
  const unread = q.data?.unread ?? 0;
  const list = rows.filter((r) => filter === "all" || (filter === "beat" ? r.eps_actual > r.eps_estimate : r.eps_actual < r.eps_estimate));
  const shown = all ? list : list.slice(0, SHOWN);
  const tip = ["Earnings releases", `From: ${FROM}`, `Reaction: ${REACTION_SOURCE}`];
  if (unread > 0) tip.push(`${unread} ${unread === 1 ? "headline" : "headlines"} could not be read`);

  return (
    <Block
      title={<span title={tip.join("\n")}>Earnings</span>}
      state={<span className="nl-st run">{q.isSuccess ? list.length : "-"}</span>}
      right={<Segmented options={FILTERS} value={filter} onChange={setFilter} />}
      className="ev-earnb"
    >
      <div data-earnings>
      {q.isError ? (
        <div className="ev-err">{serverDetail(q.error)}</div>
      ) : q.isPending ? (
        <div className="ev-none">Loading.</div>
      ) : rows.length === 0 ? (
        <div className="ev-none">No earnings releases stored yet.</div>
      ) : list.length === 0 ? (
        <div className="ev-none">No releases match.</div>
      ) : (
        <div className="ev-tscroll">
          <table className="ev-earn">
            <thead>
              <tr>
                <th>Company</th><th>Quarter</th><th>Released</th><th>Result</th>
                <th className="r">EPS</th><th className="r">Surprise</th><th className="r">Sales</th><th className="r">Reaction</th><th>Price</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const [sa, se] = salesPair(r.sales_actual, r.sales_estimate, 1);
                return (
                  <tr
                    key={`${r.ticker}-${r.release_at}`}
                    data-earn={r.ticker}
                    tabIndex={0}
                    onClick={() => setOpen(r)}
                    onKeyDown={(k) => { if (k.key === "Enter") setOpen(r); }}
                  >
                    <td>
                      <span className="ev-co">{r.company}</span>{" "}
                      <button
                        type="button"
                        className="ev-tk"
                        title={`${r.ticker}: opens its chart`}
                        onClick={(e) => { e.stopPropagation(); onChart(r.ticker); }}
                      >{r.ticker}</button>
                    </td>
                    <td>{r.quarter}</td>
                    <td className="num" title={releasedHover(r)}>{parisText(r.release_at)}</td>
                    <td><StateWord word={r.result} tone={resultTone(r.result)} /></td>
                    <td className="num r" title={epsHover(r)}>{eps(r.eps_actual)} <span className="ev-vs">{`vs ${eps(r.eps_estimate)}`}</span></td>
                    <td className={`num r ${tone(r.eps_surprise)}`} title={r.eps_surprise === null ? NO_SURPRISE : undefined}>
                      <div className="ev-cellbar"><Bar v={r.eps_surprise} cap={SURPRISE_CAP} /><span>{pct(r.eps_surprise)}</span></div>
                    </td>
                    <td className="num r" title={salesHover(r)}>{sa} <span className="ev-vs">{`vs ${se}`}</span></td>
                    <td className={`num r ${tone(r.reaction)}`} title={reactionHover(r)}>
                      <div className="ev-cellbar"><Bar v={r.reaction} cap={REACTION_CAP} /><span>{pct(r.reaction)}</span></div>
                    </td>
                    <td title={priceHover(r)}><Sparkline closes={r.closes} reaction={r.reaction} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {list.length > SHOWN && (
        <button type="button" className="btn q ev-more" onClick={() => setAll(!all)}>
          {all ? "SHOW FEWER" : `SHOW ALL ${list.length}`}
        </button>
      )}
      {open && <Detail row={open} onClose={() => setOpen(null)} onChart={onChart} />}
      </div>
    </Block>
  );
}
