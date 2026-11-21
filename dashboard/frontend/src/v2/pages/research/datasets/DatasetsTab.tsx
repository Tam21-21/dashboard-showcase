import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Block, Say, StateWord, Table, Window } from "../../../kit";
import type { Cell, Tone } from "../../../kit";
import {
  datasetsKeys, deleteSeries, fetchDatasets, fetchEventFeeds, fetchFundamentalsStatus, fetchToLatest, restoreSeries,
} from "../../../data/datasetsApi";
import { fmtDmy } from "../../../../lib/format";
import { plainDetail, serverDetail } from "../../../../lib/errorText";
import type { Dataset } from "../../../../types/research";
import { LibCard } from "../markets/LibCard";
import type { CardFact, CardTone } from "../markets/LibCard";
import AddData, { wordsOf } from "./AddData";
import { bigNumber, DAY, isPriceSeries, isStale, priceStrip, STALE_AFTER, utcYear } from "./stale";
import "./datasets.css";
import { PhoneOff } from "../../../kit/phone";

// The Datasets tab of the Data library (mockup: the frozen mockup of 2026-11-15, lab-library.js). The question: is the data fresh
// enough to trust? A headline says how many price series are behind; a wall has one card for each kind of data; pressing a kind
// opens its datasets in a window, most behind first, with FETCH and DELETE on each row; the search box in the tab row finds a
// dataset across every kind; Price history says how many series have prices in each year; Last update says where the series
// stopped. Behind is amber. The method for STALE is in docs/research_lab/display_methods.md, "Data library: Datasets tab,
// 2026-10-16". Left out because the server has nothing behind them: ADD DATA for news, ratings, filings and fundamentals, and a
// row click opening the company chart.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const SHOWN = 30;
const TOAST_MS = 6000;
const POLL_MS = 10_000;
const BLUE = "#3d7bff";
const GREEN = "#7fd9a8";

const BARS_WORD: Record<string, string> = {
  "1m": "1 minute", "5m": "5 minutes", "15m": "15 minutes", "1h": "Hourly", "4h": "4 hours", "1d": "Daily",
};
const SOURCE_WORD: Record<string, string> = { eodhd: "EODHD", alpaca_sip: "Alpaca", binance: "Binance" };
const SOURCE_COLOUR: Record<string, string> = { eodhd: BLUE, alpaca_sip: GREEN };

const PRICE_KINDS: Array<{ name: string; source: string; bars: string }> = [
  { name: "Daily prices, US", source: "eodhd", bars: "1d" },
  { name: "Daily prices, Alpaca", source: "alpaca_sip", bars: "1d" },
  { name: "15-minute prices", source: "alpaca_sip", bars: "15m" },
];

// Kinds the system plans but holds nothing of yet; each says so on its card.
const PLANNED: Array<[string, string]> = [
  ["1-minute prices", "One price bar for every minute of trading"],
  ["5-minute prices", "One price bar for every five minutes of trading"],
  ["Dollar bars", "A new price bar each time a set amount of money has changed hands, in place of a set time"],
  ["Futures prices", "Prices of futures contracts, one series for each contract"],
  ["Company filings", "The reports companies file with the regulator"],
];

const NO_REASON = "no reason recorded";
const STALE_DAYS = STALE_AFTER / DAY;

const dash = (why: string): ReactNode => <span title={why}>-</span>;
const count = (n: number) => n.toLocaleString("en-US");

// "10 Oct" in the current year, else "18 Jun 2025".
function dayText(epoch: number, nowYear: number): string {
  const d = new Date(epoch * 1000);
  const base = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return d.getUTCFullYear() === nowYear ? base : `${base} ${d.getUTCFullYear()}`;
}
const dayYear = (epoch: number) => {
  const d = new Date(epoch * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
};
const hoverDay = (epoch: number | null | undefined) => (epoch == null ? "none" : fmtDmy(epoch));

const isBusy = (d: Dataset) => d.status === "pending" || d.status === "fetching";

function newest(rows: Dataset[]): number | null {
  const ends = rows.map((r) => r.covered_end_ts).filter((v): v is number => v != null);
  return ends.length === 0 ? null : Math.max(...ends);
}

// Most behind first: the oldest newest-bar leads; a series with no bars goes last, then by name.
const byBehind = (a: Dataset, b: Dataset) =>
  (a.covered_end_ts ?? Infinity) - (b.covered_end_ts ?? Infinity) || a.symbol.localeCompare(b.symbol);

type Word = { word: string; tone: Tone };
type Toast = { text: string; undo?: () => void };

// A series' state word, with the sentence that goes on its hover; null when nothing is held.
function rowState(d: Dataset, now: number, nowYear: number): { word: Word | null; why: string } {
  if (d.status === "failed") return { word: { word: "FAILED", tone: "rd" }, why: d.last_error ? wordsOf(d.last_error) : NO_REASON };
  if (d.status === "pending") return { word: { word: "PENDING", tone: "bl" }, why: "Waiting to be fetched" };
  if (d.status === "fetching") return { word: { word: "FETCHING", tone: "bl" }, why: "Being fetched now" };
  if (d.covered_end_ts == null) return { word: null, why: "No bars are held for this series yet" };
  const stale = isStale(d.covered_end_ts, now);
  const date = dayText(d.covered_end_ts, nowYear);
  return stale
    ? { word: { word: "STALE", tone: "a" }, why: `Newest bar ${date}, behind more than a week` }
    : { word: { word: "WORKING", tone: "g" }, why: `Newest bar ${date}` };
}

// "1.2 GB": decimal units, as disks are sold.
function sizeText(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(0)} kB`;
  return `${bytes} bytes`;
}

function HeldLine({ d, from, to }: { d: Dataset; from: number | null; to: number }) {
  const a = d.covered_start_ts;
  const b = d.covered_end_ts;
  if (a == null || b == null || from == null) return dash("No bars are held for this series yet");
  const span = Math.max(to - from, DAY);
  const left = ((a - from) / span) * 100;
  const width = Math.max(((b - a) / span) * 100, 0);
  const why = `First bar ${hoverDay(a)}\nNewest bar ${hoverDay(b)}\nFetched ${hoverDay(d.last_fetched_at)}`;
  return (
    <span className="ds-tl" title={why}>
      <b style={{ left: `${left}%`, width: `${width}%`, background: SOURCE_COLOUR[d.source] ?? "#c3cbd6" }} />
    </span>
  );
}

// FETCH and DELETE show when the row is hovered or when focus is inside them (tabbing onto FETCH).
function Acts({ d, busy, onFetch, onDelete }: { d: Dataset; busy: boolean; onFetch: () => void; onDelete: () => void }) {
  const [on, setOn] = useState(false);
  return (
    <span
      className={`ds-acts${on ? " on" : ""}`}
      onFocus={() => setOn(true)}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOn(false); }}
    >
      <PhoneOff>
        <button type="button" className="btn act" aria-label="FETCH" disabled={busy} onClick={onFetch}>FETCH</button>
        <button type="button" className="btn danger act" aria-label="DELETE" data-symbol={d.symbol} onClick={onDelete}>DELETE</button>
      </PhoneOff>
    </span>
  );
}

function Confirm({
  title, text, go, danger, busy, onGo, onCancel,
}: { title: string; text?: string; go: string; danger?: boolean; busy?: boolean; onGo: () => void; onCancel: () => void }) {
  return (
    <Window label={title} size="s" className="ds-add" onClose={onCancel}>
        <div className="h2"><span>{title}</span></div>
        {text && <div className="ds-ctext">{text}</div>}
        <div className="ds-foot">
          <button type="button" className="btn q" onClick={onCancel}>CANCEL</button>
          <button type="button" className={`btn${danger ? " danger" : ""}`} disabled={busy} onClick={onGo}>{go}</button>
        </div>
    </Window>
  );
}

type Kind = {
  name: string;
  chip: { tone: CardTone; text: string; hover?: string };
  hue: CardTone;
  fig: ReactNode;
  unit: string;
  bar: Array<{ tone: CardTone | "soft"; n: number; hover?: string }>;
  facts: CardFact[];
  rows: Dataset[] | null;
  hover?: string;
  detail?: string[];
};

export default function DatasetsTab({ slot = null, find = "", onClear }: { slot?: HTMLElement | null; find?: string; onClear?: () => void }) {
  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: datasetsKeys.list,
    queryFn: () => fetchDatasets(),
    refetchInterval: (q) => ((q.state.data ?? []).some((d) => isPriceSeries(d) && isBusy(d)) ? POLL_MS : false),
  });
  const feedsQ = useQuery({ queryKey: datasetsKeys.feeds, queryFn: () => fetchEventFeeds() });
  const fundQ = useQuery({ queryKey: datasetsKeys.fundamentals, queryFn: () => fetchFundamentalsStatus() });

  const [open, setOpen] = useState<string | null>(null);
  const [inKind, setInKind] = useState("");
  const [all, setAll] = useState(false);
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<Dataset | null>(null);
  const [confirmingRefresh, setConfirmingRefresh] = useState(false);
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState<Toast | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  function say(t: Toast) {
    if (timer.current) clearTimeout(timer.current);
    setToast(t);
    timer.current = setTimeout(() => setToast(null), TOAST_MS);
  }

  const refresh = () => qc.invalidateQueries({ queryKey: ["datasets"] });
  const fetchMore = useMutation({
    mutationFn: (d: Dataset) => fetchToLatest(d.id),
    onSuccess: (res, d) => {
      say({ text: res.action === "noop" ? `${d.symbol} is already up to date` : `Fetching new bars for ${d.symbol}` });
      void refresh();
    },
    onError: (e, d) => say({ text: `${d.symbol}: ${wordsOf(serverDetail(e))}` }),
  });
  const restore = useMutation({
    mutationFn: (d: Dataset) => restoreSeries(d.id),
    onSuccess: (_r, d) => { say({ text: `Restored ${d.symbol}` }); void refresh(); },
    onError: (e, d) => say({ text: `${d.symbol}: ${wordsOf(serverDetail(e))}` }),
  });
  const remove = useMutation({
    mutationFn: (d: Dataset) => deleteSeries(d.id),
    onSuccess: (_r, d) => { say({ text: `Deleted ${d.symbol}`, undo: () => restore.mutate(d) }); void refresh(); },
    onError: (e, d) => say({ text: `${d.symbol}: ${wordsOf(serverDetail(e))}` }),
  });

  const listError = listQ.isError ? serverDetail(listQ.error) : null;
  const feedsError = feedsQ.isError ? serverDetail(feedsQ.error) : null;
  const fundError = fundQ.isError ? serverDetail(fundQ.error) : null;

  const nowMs = Date.now();
  const now = Math.floor(nowMs / 1000);
  const nowYear = utcYear(now);

  const everything = listQ.data ?? [];
  const prices = everything.filter(isPriceSeries);
  const universes = everything.filter((d) => d.dataset_type === "price_universe" && d.status !== "deleted");
  const sorted = [...prices].sort(byBehind);
  const starts = prices.map((d) => d.covered_start_ts).filter((v): v is number => v != null);
  const from = starts.length === 0 ? null : Math.min(...starts);
  const staleRows = sorted.filter((d) => rowState(d, now, nowYear).word?.word === "STALE");
  const dated = prices.filter((d) => d.covered_end_ts != null);

  async function sendStale() {
    setConfirmingRefresh(false);
    setSending(true);
    let accepted = 0;
    const refused: string[] = [];
    try {
      for (const d of staleRows) {
        try {
          await fetchToLatest(d.id);
          accepted += 1;
        } catch (e) {
          refused.push(`${d.symbol}: ${wordsOf(serverDetail(e))}`);
        }
      }
    } finally {
      setSending(false);
    }
    const noun = (n: number) => `${n} stale series`;
    let text: string;
    if (refused.length === 0) text = `Fetching new bars for ${noun(accepted)}`;
    else if (accepted === 0) text = `${noun(refused.length)} refused, ${refused.join("; ")}`;
    else text = `Fetching new bars for ${noun(accepted)}, ${refused.length} refused, ${refused.join("; ")}`;
    say({ text });
    void refresh();
  }

  // ---- The kinds: a card each. The bar is the share of its datasets that are behind.
  function priceKind(k: (typeof PRICE_KINDS)[number]): Kind {
    const rows = prices.filter((d) => d.source === k.source && d.timeframe === k.bars).sort(byBehind);
    const words = rows.map((d) => rowState(d, now, nowYear).word?.word ?? null);
    const n = (w: string) => words.filter((x) => x === w).length;
    const behind = n("STALE");
    const failed = n("FAILED");
    const busy = n("PENDING") + n("FETCHING");
    const end = newest(rows);
    const counted = rows.filter((d) => d.rows != null);
    const sized = rows.filter((d) => d.file_bytes != null);
    const firsts = rows.map((d) => d.covered_start_ts).filter((v): v is number => v != null);
    let chip: Kind["chip"];
    let hue: CardTone;
    if (rows.length === 0) { chip = { tone: "dim", text: "None yet" }; hue = ""; }
    else if (failed > 0) { chip = { tone: "bad", text: `${failed} failed`, hover: rows.filter((d) => d.status === "failed").map((d) => `${d.symbol}: ${d.last_error ? wordsOf(d.last_error) : NO_REASON}`).join("\n") }; hue = "bad"; }
    else if (busy > 0) { chip = { tone: "run", text: `${busy} fetching` }; hue = "run"; }
    else if (behind > 0) { chip = { tone: "warn", text: `${behind} behind` }; hue = "warn"; }
    else if (end == null) { chip = { tone: "dim", text: "No dates" }; hue = ""; }
    else { chip = { tone: "good", text: "Up to date" }; hue = "good"; }
    return {
      name: k.name, chip, hue, fig: listQ.isPending ? dash("Loading.") : String(rows.length), unit: rows.length === 1 ? "dataset" : "datasets",
      bar: rows.length === 0 ? [] : [{ tone: "bad", n: failed, hover: `${failed} failed` }, { tone: "warn", n: behind, hover: `${behind} behind` }, { tone: "run", n: busy }, { tone: "good", n: rows.length - behind - failed - busy, hover: "up to date" }],
      facts: [
        { label: "Rows", value: counted.length === 0 ? dash("No bar count is saved yet") : bigNumber(counted.reduce((a, d) => a + d.rows!, 0)) },
        { label: "Size", value: sized.length === 0 ? dash("No size is saved for these series yet") : sizeText(sized.reduce((a, d) => a + d.file_bytes!, 0)) },
        { label: "Prices from", value: firsts.length === 0 ? "None" : String(utcYear(Math.min(...firsts))), hover: firsts.length ? `First bar ${hoverDay(Math.min(...firsts))}` : undefined },
        { label: "Newest price", value: end == null ? "-" : dayText(end, nowYear), tone: behind > 0 ? "warn" : "" },
      ],
      rows, hover: `Source: ${SOURCE_WORD[k.source]}. Press to list its datasets.`,
    };
  }

  const feeds = (feedsQ.data ?? []).filter((f) => f.total_count > 0);
  const kinds: Kind[] = [
    ...PRICE_KINDS.map(priceKind),
    {
      name: "Stock baskets", chip: { tone: "dim", text: "No dates" }, hue: "", fig: listQ.isPending ? dash("Loading.") : String(universes.length),
      unit: universes.length === 1 ? "dataset" : "datasets", bar: [],
      facts: [
        { label: "Rows", value: "-" }, { label: "Size", value: "-" }, { label: "Prices", value: "None" },
        { label: "Last download", value: universes.length === 0 ? "-" : dayText(Math.max(...universes.map((u) => u.last_fetched_at ?? 0)), nowYear) },
      ],
      rows: null, hover: "Stock lists: sets of tickers with a first and a last day", detail: universes.map((u) => u.symbol),
    },
    {
      name: "Events", chip: { tone: "dim", text: feedsError ? "Not read" : "No dates", hover: feedsError ?? undefined }, hue: "",
      fig: feedsQ.isPending && !feedsError ? dash("Loading.") : String(feeds.length), unit: feeds.length === 1 ? "dataset" : "datasets", bar: [],
      facts: [
        { label: "Rows", value: feeds.length === 0 ? "-" : bigNumber(feeds.reduce((a, f) => a + f.total_count, 0)) }, { label: "Size", value: "-" }, { label: "Prices", value: "None" },
        { label: "Last download", value: feeds.length === 0 ? "-" : dayText(Math.max(...feeds.map((f) => f.latest_knowledge_at ?? 0)), nowYear) },
      ],
      rows: null, hover: "News and analyst ratings held as events",
      detail: feeds.map((f) => `${f.event_type}: ${count(f.total_count)}`),
    },
    {
      name: "Company accounts", chip: { tone: "dim", text: fundError ? "Not read" : "No dates", hover: fundError ?? undefined }, hue: "",
      fig: fundQ.isPending && !fundError ? dash("Loading.") : fundQ.data && fundQ.data.total_facts_on_record > 0 ? "1" : "0", unit: fundQ.data && fundQ.data.total_facts_on_record > 0 ? "dataset" : "datasets", bar: [],
      facts: [
        { label: "Rows", value: fundQ.data && fundQ.data.total_facts_on_record > 0 ? bigNumber(fundQ.data.total_facts_on_record) : "-" }, { label: "Size", value: "-" }, { label: "Prices", value: "None" },
        { label: "Last download", value: fundQ.data?.recent_batches[0]?.started_at != null ? dayText(fundQ.data.recent_batches[0].started_at, nowYear) : "-" },
      ],
      rows: null, hover: "Company figures from SEC filings",
    },
    ...PLANNED.map(([name, what]): Kind => ({
      name, chip: { tone: "dim", text: "None yet" }, hue: "", fig: "0", unit: "datasets", bar: [],
      facts: [{ label: "Rows", value: "-" }, { label: "Size", value: "-" }, { label: "Prices", value: "None" }, { label: "Last download", value: "-" }],
      rows: null, hover: `${what}. No dataset of this kind yet.`,
    })),
  ];
  const totalRows = prices.reduce((a, d) => a + (d.rows ?? 0), 0) + feeds.reduce((a, f) => a + f.total_count, 0) + (fundQ.data?.total_facts_on_record ?? 0);
  const totalBytes = prices.reduce((a, d) => a + (d.file_bytes ?? 0), 0);
  const totalSets = prices.length + universes.length + feeds.length + (fundQ.data && fundQ.data.total_facts_on_record > 0 ? 1 : 0);

  // ---- One list of series, with FETCH and DELETE on each row.
  function seriesTable(list: Dataset[], showAll: boolean, toggle: () => void) {
    const shown = showAll ? list : list.slice(0, SHOWN);
    const head: ReactNode[] = [
      "Ticker", "Source", "Bars",
      <span key="held" title={from == null ? undefined : `Bars held, ${utcYear(from)} to today`}>Held</span>,
      "Count", "Newest", "State", "",
    ];
    const body: Cell[][] = shown.map((d) => {
      const st = rowState(d, now, nowYear);
      return [
        { v: d.symbol, cls: "num ds-sym" },
        {
          v: (
            <span className="ds-src" title={SOURCE_WORD[d.source] ? undefined : d.source}>
              <i style={{ background: SOURCE_COLOUR[d.source] ?? "#c3cbd6" }} />{SOURCE_WORD[d.source] ?? plainDetail(d.source)}
            </span>
          ),
          cls: "ds-l",
        },
        { v: BARS_WORD[d.timeframe] ? BARS_WORD[d.timeframe] : <span title={d.timeframe}>{plainDetail(d.timeframe)}</span>, cls: "ds-l" },
        { v: <HeldLine d={d} from={from} to={now} />, cls: "ds-l ds-heldc" },
        { v: d.rows == null ? dash("No bar count is saved for this series yet") : count(d.rows), cls: "num" },
        { v: d.covered_end_ts == null ? dash("No bars are held for this series yet") : dayText(d.covered_end_ts, nowYear), cls: "num" },
        {
          v: st.word ? (
            <span title={st.word.word === "WORKING" || st.word.word === "PENDING" || st.word.word === "FETCHING" ? undefined : st.why}>
              <StateWord word={st.word.word} tone={st.word.tone} />
            </span>
          ) : dash(st.why),
        },
        { v: <Acts d={d} busy={fetchMore.isPending} onFetch={() => fetchMore.mutate(d)} onDelete={() => setDeleting(d)} /> },
      ];
    });
    return (
      <>
        <div data-testid="ds-table" className="ds-tbl">
          {list.length === 0 ? <div className="empty">No dataset matches.</div> : <Table head={head} rows={body} />}
        </div>
        {list.length > SHOWN && (
          <button type="button" className="btn q ds-more" onClick={toggle}>
            {showAll ? "SHOW FEWER" : `SHOW ALL ${list.length}`}
          </button>
        )}
      </>
    );
  }

  // ---- The headline: how many price series are behind.
  const stopGroups = new Map<number, Dataset[]>();
  for (const d of dated) {
    const day = Math.floor(d.covered_end_ts! / DAY);
    stopGroups.set(day, [...(stopGroups.get(day) ?? []), d]);
  }
  const stops = [...stopGroups.entries()].sort((a, b) => b[0] - a[0]).slice(0, 8);
  const biggest = [...stopGroups.entries()].sort((a, b) => b[1].length - a[1].length || b[0] - a[0])[0];
  const rule = `A series is behind once its newest price is over ${STALE_DAYS} days old.`;
  let lead: ReactNode;
  let more: ReactNode = null;
  if (listQ.isPending) lead = "Loading the price series.";
  else if (listError) lead = <span title={listError}>The price series could not be read.</span>;
  else if (dated.length === 0) lead = "No price series yet.";
  else if (staleRows.length === dated.length && biggest) {
    lead = "Every price series is behind.";
    more = <><em className="warn">{`${count(biggest[1].length)} of ${count(dated.length)} stopped on ${dayText(biggest[0] * DAY, nowYear)}`}</em>{`, ${Math.round((now - biggest[0] * DAY) / DAY)} days ago. ${rule}`}</>;
  } else if (staleRows.length === 0) {
    lead = "No price series is behind.";
    more = rule;
  } else {
    lead = <><em className="warn">{`${count(staleRows.length)} of ${count(dated.length)}`}</em>{" price series are behind."}</>;
    more = rule;
  }

  // ---- The search across every kind, and the open kind's window.
  const q = find.trim().toLowerCase();
  const hits = q ? sorted.filter((d) => d.symbol.toLowerCase().includes(q)) : [];
  const openKind = kinds.find((k) => k.name === open) ?? null;
  const inRows = openKind?.rows ? openKind.rows.filter((d) => !inKind.trim() || d.symbol.toLowerCase().includes(inKind.trim().toLowerCase())) : [];

  const strip = priceStrip(dated.map((d) => ({ start: d.covered_start_ts ?? d.covered_end_ts!, end: d.covered_end_ts! })), nowYear);
  const widest = Math.max(1, ...strip.map((s) => s.n));

  const buttons = (
    <>
      <PhoneOff>
        <button type="button" className="btn" onClick={() => setAdding(true)}>Add data</button>
        <button
          type="button" className="btn q" disabled={sending || staleRows.length === 0}
          title={staleRows.length === 0 ? "No series is behind" : undefined} onClick={() => setConfirmingRefresh(true)}
        >
          {`Refresh ${staleRows.length} behind`}
        </button>
      </PhoneOff>
    </>
  );

  return (
    <div className="ds">
      {slot && createPortal(buttons, slot)}

      <div className="page-say"><Say lead={lead} more={more} /></div>

      {q && (
        <Block title="Search results" right={<button type="button" className="btn q" onClick={() => onClear?.()}>Clear the search</button>}>
          {listError ? <div className="ds-err">{listError}</div> : seriesTable(hits, all, () => setAll(!all))}
        </Block>
      )}

      <Block
        title={<span title="Press a kind to list its datasets">Datasets by kind</span>}
        state={
          <span className="lc-chips">
            <span className="nl-st run">{`${count(totalSets)} datasets`}</span>
            <span className="nl-st run">{`${bigNumber(totalRows)} rows`}</span>
            <span className="nl-st run">{totalBytes > 0 ? sizeText(totalBytes) : "no size saved"}</span>
          </span>
        }
      >
        {listError ? <div className="ds-err">{listError}</div> : (
          <div className="lc-wall">
            {kinds.map((k) => (
              <LibCard
                key={k.name} name={k.name} chip={k.chip} hue={k.hue} fig={k.fig} unit={k.unit} bar={k.bar} facts={k.facts}
                hover={k.hover} testId="kind-card" onClick={k.rows || k.detail ? () => { setOpen(k.name); setInKind(""); setAll(false); } : undefined}
              />
            ))}
          </div>
        )}
      </Block>

      <div className="lay mk-lay">
        <Block title="Price history" state={<span className="nl-st run">{`${count(dated.length)} price series`}</span>}>
          {strip.length === 0 ? <div className="empty">No price series has dates yet.</div> : (
            <>
              <div className="dl-years" data-testid="price-history" role="img" aria-label="Price series with prices in each year">
                {strip.map((s) => (
                  <i key={s.year} style={{ height: `${Math.max(3, (s.n / widest) * 100)}%` }} title={`${s.year}: ${count(s.n)} price series have prices that year`} />
                ))}
              </div>
              <div className="dl-axis num"><span>{strip[0].year}</span><span>{strip[strip.length >> 1].year}</span><span>{strip[strip.length - 1].year}</span></div>
            </>
          )}
        </Block>
        <Block title="Last update" state={<span className={`nl-st ${staleRows.length ? "warn" : "good"}`}>{staleRows.length ? `${count(staleRows.length)} behind` : "Up to date"}</span>}>
          {stops.length === 0 ? <div className="empty">No price series has stopped.</div> : (
            <div className="dl-stops" data-testid="last-update">
              {stops.map(([day, list]) => (
                <div key={day} title={`${list.slice(0, 3).map((d) => d.symbol).join("; ")}${list.length > 3 ? " and more" : ""}`}>
                  <div className="top"><span>{`${dayYear(day * DAY)}, ${count(Math.round((now - day * DAY) / DAY))} days ago`}</span><span className="num">{`${count(list.length)} series`}</span></div>
                  <div className="trk"><b className={isStale(day * DAY, now) ? "warn" : "good"} style={{ width: `${Math.max(0.6, (list.length / dated.length) * 100)}%` }} /></div>
                </div>
              ))}
            </div>
          )}
        </Block>
      </div>

      {openKind && (
        <Window label={openKind.name} size="xl" className="ds-kind" onClose={() => setOpen(null)}>
          <div className="h2"><span>{openKind.name}</span></div>
          <p className="ds-lead"><span className={`nl-st ${openKind.chip.tone || "dim"}`}>{openKind.chip.text}</span><span>{`${openKind.fig} ${openKind.unit}`}</span></p>
          {openKind.rows ? (
            <>
              <input className="fin ds-search" aria-label="Find in this kind" placeholder="Find in this kind" value={inKind} onChange={(e) => { setInKind(e.target.value); setAll(false); }} />
              {seriesTable(inRows, all, () => setAll(!all))}
            </>
          ) : (
            <div className="ds-detail">
              {(openKind.detail ?? []).length === 0 ? <div className="empty">Nothing is held yet.</div> : (openKind.detail ?? []).map((t) => <div key={t}>{t}</div>)}
            </div>
          )}
          <div className="ds-foot"><button type="button" className="btn q" onClick={() => setOpen(null)}>CLOSE</button></div>
        </Window>
      )}

      {adding && <AddData held={prices} universes={universes} onClose={() => setAdding(false)} onDone={(n) => say({ text: `Fetching ${n} series` })} />}

      {deleting && (
        <Confirm
          title={`Delete ${deleting.symbol}?`}
          text="Runs that used it keep their results."
          go="DELETE"
          danger
          onGo={() => { remove.mutate(deleting); setDeleting(null); }}
          onCancel={() => setDeleting(null)}
        />
      )}

      {confirmingRefresh && (
        <Confirm
          title={`Fetch new bars for ${staleRows.length} stale series?`}
          go="FETCH"
          onGo={() => void sendStale()}
          onCancel={() => setConfirmingRefresh(false)}
        />
      )}

      {toast && (
        <div className="ds-toast" role="status">
          <span>{toast.text}</span>
          {toast.undo && <PhoneOff><button type="button" className="btn q" onClick={() => { toast.undo?.(); setToast(null); }}>UNDO</button></PhoneOff>}
        </div>
      )}
    </div>
  );
}
