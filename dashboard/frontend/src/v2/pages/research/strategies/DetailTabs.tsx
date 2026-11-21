import { useState } from "react";
import type { CSSProperties, KeyboardEvent, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { StateWord, Window } from "../../../kit";
import {
  RUNS_AT_MOST, fetchStrategyRuns, fetchStrategySettings, fetchStrategySource, fetchStrategyVersions, strategiesKeys,
} from "../../../data/strategiesApi";
import type { StrategyOverviewRow, StrategySetting, StrategyParameters } from "../../../data/strategiesApi";
import type { JudgeVerdictRow } from "../../../data/judgeApi";
import { serverDetail } from "../../../../lib/errorText";
import { fmtDmy, plainStrategyName } from "../../../../lib/format";
import { VERDICT_STATE } from "./RoadToPaper";

// The three tabs the mockup draws besides OVERVIEW and PARAMETERS (docs/design/rail_mockup_2026-10-10.html
// at 05ac8de4, research/STRATEGIES, stabBody): RULES, WHERE IT TRADES, HISTORY. What the server does
// not record yet is a dash with its reason on hover.

export const STOCKS_WHY = "The server does not record which stocks a strategy trades yet";
const WINDOWS_WHY = "The server does not record a strategy's windows yet";
const MONEY_WHY = "The server does not report how money is split yet";
const THEORY_RULES_WHY = "The server does not send a theory's rules in words yet";
const THEORY_PILL_WHY = "This strategy is a generator theory; its rules are its theory file";
const ENTRY_BLUE = "#4d9bff";
const EXIT_GREEN = "#2fe08a";
const THEORY_VIOLET = "#a78bfa";
const RUNS_CYAN = "#29c6ff";
const VERSION_BLUE = "#4d9bff";
const GREY = "#8d98a8";
const VERDICT_COLOUR: Record<JudgeVerdictRow["verdict"], string> = { pass: "#2fe08a", fail: "#ff5a63", unjudgeable: "#ffb020" };

function Dash({ why }: { why: string }) {
  return <span title={why}>-</span>;
}

const plain = (key: string) => plainStrategyName(key.replace(/\./g, " "));

// ---- RULES ----

export function RulesTab({
  s, kind, kindError, hue,
}: {
  s: StrategyOverviewRow; kind: StrategyParameters["kind"] | undefined; kindError: unknown; hue: string;
}) {
  const [open, setOpen] = useState(false);
  const box = (label: string | null, colour: string, content: ReactNode) => (
    <div className="grule" style={{ "--c": colour } as CSSProperties}>
      {label && <span className="kl">{label}</span>}
      {content}
    </div>
  );
  if (kindError != null) return <p className="empty">{serverDetail(kindError)}</p>;
  if (kind === undefined) return <p className="empty">Loading the rules.</p>;
  if (kind === "theory") {
    return (
      <>
        <div className="sc-pills"><span className="pill" title={THEORY_PILL_WHY}>THEORY</span></div>
        {box("ENTRY", ENTRY_BLUE, <Dash why={THEORY_RULES_WHY} />)}
        {box("EXIT", EXIT_GREEN, <Dash why={THEORY_RULES_WHY} />)}
      </>
    );
  }
  return (
    <>
      {box(null, hue, s.description == null ? <Dash why="No description in the code" /> : s.description)}
      <div className="sc-pills">
        <span className="pill" title="A strategy written by hand has no theory: its code is its rule">WRITTEN BY HAND</span>
        <button type="button" className="btn q sc-view" onClick={() => setOpen(true)}>VIEW CODE</button>
      </div>
      {open && <CodeWindow id={s.id} onClose={() => setOpen(false)} />}
    </>
  );
}

function CodeWindow({ id, onClose }: { id: number; onClose: () => void }) {
  const q = useQuery({ queryKey: strategiesKeys.source(id), queryFn: () => fetchStrategySource(id) });
  let body: ReactNode;
  if (q.isError) body = <p className="empty">{serverDetail(q.error)}</p>;
  else if (!q.data) body = <p className="empty">Loading the code.</p>;
  else body = <pre className="fin sc-code">{q.data.source_code}</pre>;
  return (
    <Window label="Code" size="l" onClose={onClose}>
      <div className="h2"><span>CODE</span></div>
      {body}
      <div className="rp-foot">
        <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
      </div>
    </Window>
  );
}

// ---- WHERE IT TRADES ----

// 0.0003 as "0.03%": up to 2 decimals.
function pct(v: number): string {
  return `${Number((v * 100).toFixed(2))}%`;
}

function Tile({ label, value, title }: { label: string; value: ReactNode; title?: string }) {
  return (
    <div className="gct" title={title}>
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}

const FEE_KEYS = ["alpaca.sec_fee_rate", "alpaca.taf_per_share", "alpaca.cat_per_share"];

export function WhereTab({ s }: { s: StrategyOverviewRow }) {
  const settingsQ = useQuery({ queryKey: strategiesKeys.settings(s.id), queryFn: () => fetchStrategySettings(s.id) });
  const sourceQ = useQuery({ queryKey: strategiesKeys.source(s.id), queryFn: () => fetchStrategySource(s.id) });
  const rows: StrategySetting[] = settingsQ.data ?? [];
  const find = (scope: string, key: string) => rows.find((r) => r.scope === scope && r.key === key);
  const settingsWhy = settingsQ.isError ? serverDetail(settingsQ.error) : "Reading the settings";

  let fees: ReactNode;
  let feesTitle: string | undefined;
  if (!settingsQ.data) {
    fees = <Dash why={settingsWhy} />;
  } else {
    const lines = FEE_KEYS.map((key) => {
      const row = find("fees", key);
      return row ? `${row.label ?? plain(key)}: ${String(row.value)}` : `No ${plain(key)} setting`;
    });
    fees = FEE_KEYS.every((key) => find("fees", key) === undefined) ? <Dash why={lines.join("\n")} /> : "Alpaca";
    feesTitle = lines.join("\n");
  }

  const slip = (key: string): { text: string; why?: string } => {
    if (!settingsQ.data) return { text: "-", why: settingsWhy };
    const row = find("slippage", key);
    if (!row || typeof row.value !== "number") return { text: "-", why: `No ${row?.label ?? plain(key)} setting` };
    return { text: pct(row.value) };
  };
  const equities = slip("equities.default");
  const crypto = slip("crypto.default");

  let size: ReactNode;
  let sizeTitle: string | undefined;
  if (sourceQ.isError) size = <Dash why={serverDetail(sourceQ.error)} />;
  else if (!sourceQ.data) size = <Dash why="Reading the sizing method" />;
  else {
    size = plain(sourceQ.data.sizing_method);
    sizeTitle = `Stored as ${sourceQ.data.sizing_method}`;
  }

  return (
    <>
      <div className="sc-block">
        <div className="h2">STOCKS</div>
        <div className="sc-dash"><Dash why={STOCKS_WHY} /></div>
      </div>
      <div className="sc-block">
        <div className="h2">WINDOWS</div>
        <div className="sc-dash"><Dash why={WINDOWS_WHY} /></div>
      </div>
      <div className="sc-block">
        <div className="h2">COSTS AND SIZE</div>
        <div className="gcts">
          <Tile label="FEES" value={fees} title={feesTitle} />
          <Tile
            label="SLIPPAGE"
            value={equities.why ? <Dash why={equities.why} /> : equities.text}
            title={`Crypto: ${crypto.why ?? crypto.text}`}
          />
          <Tile label="SIZE" value={size} title={sizeTitle} />
          <Tile label="MONEY" value={<Dash why={MONEY_WHY} />} />
        </div>
        {settingsQ.isError && <p className="rd" role="status">{serverDetail(settingsQ.error)}</p>}
      </div>
    </>
  );
}

// ---- HISTORY ----

type Event = { colour: string; label: string; value: ReactNode; date?: string; title?: string; go?: () => void };

function Entry({ e }: { e: Event }) {
  const onKey = (ev: KeyboardEvent) => {
    if (e.go && (ev.key === "Enter" || ev.key === " ")) {
      ev.preventDefault();
      e.go();
    }
  };
  return (
    <div
      className="gte"
      style={{ "--c": e.colour } as CSSProperties}
      title={e.title}
      data-go={e.go ? "" : undefined}
      role={e.go ? "link" : undefined}
      tabIndex={e.go ? 0 : undefined}
      onClick={e.go}
      onKeyDown={onKey}
    >
      <i />
      <span className="kl">{e.label}</span>
      <span className="gtv">{e.value}</span>
      {e.date !== undefined && <span className="gtd num">{e.date}</span>}
    </div>
  );
}

export function HistoryTab({
  s, kind, verdict, verdictsWhy,
}: {
  s: StrategyOverviewRow; kind: StrategyParameters["kind"] | undefined; verdict: JudgeVerdictRow | null; verdictsWhy: string | null;
}) {
  const navigate = useNavigate();
  const versionsQ = useQuery({ queryKey: strategiesKeys.versions(s.id), queryFn: () => fetchStrategyVersions(s.id) });
  const runsQ = useQuery({ queryKey: strategiesKeys.runs(s.id), queryFn: () => fetchStrategyRuns(s.id) });
  if (versionsQ.isError) return <p className="empty">{serverDetail(versionsQ.error)}</p>;
  if (!versionsQ.data) return <p className="empty">Loading the history.</p>;

  const events: Event[] = [];
  if (kind === "theory") {
    events.push({ colour: THEORY_VIOLET, label: "THEORY", value: s.display_name ?? <Dash why="No display name" /> });
  } else {
    events.push({ colour: GREY, label: "THEORY", value: <Dash why="Written by hand, not from a theory" /> });
  }
  // The server sends newest first; the timeline reads oldest first, newest last.
  for (const v of [...versionsQ.data].reverse()) {
    events.push({
      colour: VERSION_BLUE, label: `VERSION ${v.version}`, value: "", date: fmtDmy(v.edited_at), title: v.description ?? undefined,
    });
  }
  let runs: ReactNode;
  if (runsQ.isError) runs = <Dash why={serverDetail(runsQ.error)} />;
  else if (!runsQ.data) runs = <Dash why="Reading the runs" />;
  else {
    const n = runsQ.data.length;
    runs = n >= RUNS_AT_MOST ? `${RUNS_AT_MOST}+ runs` : `${n} ${n === 1 ? "run" : "runs"}`;
  }
  events.push({ colour: RUNS_CYAN, label: "RUNS", value: runs, title: "Open its runs", go: () => navigate("/app/research/runs") });
  if (verdict) {
    const st = VERDICT_STATE[verdict.verdict];
    events.push({
      colour: VERDICT_COLOUR[verdict.verdict], label: "JUDGE", value: <StateWord word={st.word} tone={st.tone} />, date: fmtDmy(verdict.graded_at),
    });
  } else {
    events.push({ colour: GREY, label: "JUDGE", value: <Dash why={verdictsWhy ?? "Not sent to the judge yet"} /> });
  }
  return <div className="gtl">{events.map((e) => <Entry key={e.label} e={e} />)}</div>;
}
