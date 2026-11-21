import { useState } from "react";
import type { CSSProperties, KeyboardEvent, ReactNode } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { PhoneOff } from "../../kit/phone";
import { Facts, LineChart, PageHeader, Panel, Say, State, StateWord, Window, usePageMemory } from "../../kit";
import type { Tone } from "../../kit";
import type { PageProps } from "../../shell/sections";
import { fetchStrategiesOverview, fetchStrategyParameters, fetchStrategyVersions, strategiesKeys } from "../../data/strategiesApi";
import type { StrategyOverviewRow, StrategyParameter, StrategyParameters } from "../../data/strategiesApi";
import { strategyHue, useStrategyNames } from "../../data/strategyNames";
import { fetchJudgeVerdicts } from "../../data/judgeApi";
import type { JudgeVerdictRow } from "../../data/judgeApi";
import { getRunningRuns } from "../../data/paperApi";
import { fetchBatches, fetchTree, generatorKeys } from "../../data/generatorApi";
import type { GenTree } from "../../data/generatorApi";
import { serverDetail } from "../../../lib/errorText";
import { fmtPctSigned, plainStrategyName } from "../../../lib/format";
import { judgeKeys } from "./Judge";
import RoadToPaper, { VERDICT_STATE, paperKeys } from "./strategies/RoadToPaper";
import StrategyEditor from "./strategies/StrategyEditor";
import type { EditorTarget } from "./strategies/StrategyEditor";
import NewStrategy from "./strategies/NewStrategy";
import { HistoryTab, RulesTab, STOCKS_WHY, WhereTab } from "./strategies/DetailTabs";
import { SearchCard, THEORY_PREFIX, theoryOf, whereOf } from "./strategies/Road";
import { Families, STAGES, StageLine, StrategyTable, day, itemOf } from "./strategies/StrategyList";
import type { RoadRow } from "./strategies/Road";
import "./strategies.css";

// Round six (Tamer 2026-11-14, the look trial's lab-p-strategies.js): the finding, the road to paper with every strategy
// on it (the road is the picker), and the picked theory strategy's search. Below them the detail with its tabs (mockup
// docs/design/rail_mockup_2026-10-10.html, research/STRATEGIES, stabs and stabBody; RULES, WHERE IT TRADES and HISTORY
// live in strategies/DetailTabs.tsx) and the steps from the judge to paper (strategies/RoadToPaper.tsx).

const VERDICT_LIMIT = 500;
const GREEN = "#2fe08a";
const RED = "#ff5a63";

type State = { word: string; tone: Tone } | null;
type Tab = "over" | "rules" | "par" | "where" | "hist";

// What the page shows for a strategy: its typed display name, or a dash when it has none. The hover
// is the description (when there is one) and the stored name.
function nameOf(s: StrategyOverviewRow): { text: string; title: string } {
  if (s.display_name == null) return { text: "-", title: "No display name" };
  const stored = `Stored as ${s.name}`;
  return { text: s.display_name, title: s.description == null ? stored : `${s.description} ${stored}` };
}

// Whole numbers on the chart's y axis once the curve spans 4 or more, else two decimals.
export function axisDecimals(curve: number[]): number {
  return Math.max(...curve) - Math.min(...curve) >= 4 ? 0 : 2;
}

// A stored name in plain words: dots and underscores become spaces, first letter upper case.
export function plainWords(s: string): string {
  return plainStrategyName(s.replace(/\./g, " "));
}

// The verdict rows the Judge page counts: current, not advisory, not rejected. Newest first.
function newestVerdict(rows: JudgeVerdictRow[], strategyId: number): JudgeVerdictRow | null {
  const mine = rows.filter((r) => r.strategy_id === strategyId && r.current && !r.is_advisory && !r.rejected);
  mine.sort((a, b) => b.graded_at - a.graded_at || b.id - a.id);
  return mine[0] ?? null;
}

// The state word of a strategy, in this order (DECISIONS #459):
//   1. ON PAPER (green) when a running paper run has the strategy's id;
//   2. else its newest eligible verdict: PASSED (green), FAILED (red), INCOMPLETE (amber);
//   3. else BACKTESTED (blue) when its overview run is not null;
//   4. else null, shown as a dash.
// AT THE JUDGE never shows: judging is synchronous, so no pending state exists to read.
export function strategyState(
  strategy: StrategyOverviewRow,
  verdicts: JudgeVerdictRow[],
  paperIds: ReadonlySet<number>,
): State {
  if (paperIds.has(strategy.id)) return { word: "ON PAPER", tone: "g" };
  const v = newestVerdict(verdicts, strategy.id);
  if (v) return VERDICT_STATE[v.verdict];
  if (strategy.run !== null) return { word: "BACKTESTED", tone: "bl" };
  return null;
}

const clearedOf = (v: JudgeVerdictRow) => v.check_outcomes.filter((c) => c.outcome === "cleared").length;

const NO_RUN = "No real run yet";
const NO_SHARPE = "No Sharpe for this run";

function Dash({ why }: { why: string }) {
  return <span title={why}>-</span>;
}

type Shown = { text: string; title?: string };

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// A stored value as the page reads it. A string is put in plain words with the stored text on hover.
function shownValue(v: unknown): Shown {
  if (typeof v === "number") return { text: String(v) };
  if (typeof v === "boolean") return { text: v ? "yes" : "no" };
  if (typeof v === "string") return { text: plainWords(v), title: v };
  if (Array.isArray(v)) return { text: v.join(", ") };
  if (isRecord(v) && Array.isArray(v.values)) return { text: v.values.join(", ") };
  if (isRecord(v) && typeof v.from === "number" && typeof v.to === "number") {
    return { text: `${v.from} to ${v.to}${typeof v.step === "number" ? `, step ${v.step}` : ""}` };
  }
  return { text: "-", title: JSON.stringify(v ?? null) };
}

function ParameterRow({ p, kind }: { p: StrategyParameter; kind: StrategyParameters["kind"] }) {
  const theory = kind === "theory";
  const value = shownValue(p.value);
  const src = p.source;
  return (
    <div className="gpr">
      <span className="gpn" title={p.name}>{plainWords(theory && p.range ? p.range : p.name)}</span>
      <span className="gbig num" title={value.title}>{value.text}</span>
      {src ? (
        <span className="gpv" title={`${src.file}, ${src.key}${src.why ? `: ${src.why}` : ""}`}>
          {`${theory ? "ranges.toml" : "provenance"}, line ${src.line}`}
        </span>
      ) : (
        <span className="gpv">
          <Dash why={theory ? "No ranges.toml line for this pick" : "No line in the provenance file for this parameter"} />
        </span>
      )}
    </div>
  );
}

function parametersSub(data: StrategyParameters | undefined): string {
  if (!data) return "-";
  const n = data.parameters.length;
  if (n === 0) return "none";
  return `${n} ${data.kind === "theory" ? "pick" : "value"}${n === 1 ? "" : "s"}`;
}

function Detail({
  s, verdict, state, stateWhy, verdictsWhy, hue, tab, setTab,
}: {
  s: StrategyOverviewRow; verdict: JudgeVerdictRow | null; state: State; stateWhy: string; verdictsWhy: string | null;
  hue: string; tab: Tab; setTab: (t: Tab) => void;
}) {
  const paramsQ = useQuery({ queryKey: strategiesKeys.parameters(s.id), queryFn: () => fetchStrategyParameters(s.id) });
  const run = s.run;
  const curve = run?.equity_sparkline ?? null;
  const colour = run !== null && run.sharpe_ratio !== null && run.sharpe_ratio < 0 ? RED : GREEN;
  const checksWhy = verdictsWhy ?? "Not judged yet";
  const name = nameOf(s);
  const ret = run?.total_return ?? null;
  const overSub = run === null ? "no run" : run.sharpe_ratio === null ? "no Sharpe" : `Sharpe ${run.sharpe_ratio.toFixed(2)}`;
  const versionsQ = useQuery({ queryKey: strategiesKeys.versions(s.id), queryFn: () => fetchStrategyVersions(s.id) });
  const kind = paramsQ.data?.kind;
  const nv = versionsQ.data?.length;
  const tabs: Array<[Tab, string, string, string | undefined]> = [
    ["over", "OVERVIEW", overSub, undefined],
    ["rules", "RULES", kind === undefined ? "-" : kind === "theory" ? "theory" : "by hand", paramsQ.isError ? serverDetail(paramsQ.error) : undefined],
    ["par", "PARAMETERS", parametersSub(paramsQ.data), paramsQ.isError ? serverDetail(paramsQ.error) : undefined],
    ["where", "WHERE IT TRADES", "-", STOCKS_WHY],
    ["hist", "HISTORY", nv === undefined ? "-" : `${nv} ${nv === 1 ? "version" : "versions"}`, versionsQ.isError ? serverDetail(versionsQ.error) : undefined],
  ];
  const onKey = (t: Tab) => (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setTab(t);
    }
  };

  let body: ReactNode;
  if (tab === "over") {
    body = (
      <>
        {curve !== null && curve.length >= 2 ? (
          <div className="sd-chart">
            <LineChart
              series={[{ v: curve, c: colour, sw: 1.8, fill: 0.16 }]}
              rows={(i) => [`Point ${i + 1}`, ["Equity", curve[i].toFixed(2)]]}
              yfmt={(v) => v.toFixed(axisDecimals(curve))}
              minHeight={170}
            />
          </div>
        ) : (
          <p className="empty">No curve stored for this strategy</p>
        )}
        <Facts
          items={[
            ["SHARPE", run?.sharpe_ratio != null ? run.sharpe_ratio.toFixed(2) : "-", undefined, run === null ? NO_RUN : run.sharpe_ratio == null ? NO_SHARPE : undefined],
            ["TRADES", run?.num_trades != null ? String(run.num_trades) : "-", undefined, run === null ? NO_RUN : run.num_trades == null ? "No trade count for this run" : undefined],
            ["RETURN", ret !== null ? fmtPctSigned(ret, 2) : "-", ret === null || ret === 0 ? undefined : ret > 0 ? "g" : "rd", run === null ? NO_RUN : ret === null ? "No total return for this run" : undefined],
            ["CHECKS", verdict ? `${clearedOf(verdict)} of 9` : "-", undefined, verdict ? undefined : checksWhy],
          ]}
        />
      </>
    );
  } else if (tab === "rules") {
    body = <RulesTab s={s} kind={kind} kindError={paramsQ.isError ? paramsQ.error : null} hue={hue} />;
  } else if (tab === "where") {
    body = <WhereTab s={s} />;
  } else if (tab === "hist") {
    body = <HistoryTab s={s} kind={kind} verdict={verdict} verdictsWhy={verdictsWhy} />;
  } else if (paramsQ.isError) {
    body = <p className="empty">{serverDetail(paramsQ.error)}</p>;
  } else if (!paramsQ.data) {
    body = <p className="empty">Loading the parameters.</p>;
  } else if (paramsQ.data.parameters.length === 0) {
    body = (
      <p className="empty">
        {paramsQ.data.kind === "theory" ? "This theory picks no parameters." : "No parameters recorded: this strategy has no single real run yet."}
      </p>
    );
  } else {
    const kind = paramsQ.data.kind;
    body = (
      <div className="gpk">
        {paramsQ.data.parameters.map((p) => <ParameterRow key={p.name} p={p} kind={kind} />)}
      </div>
    );
  }

  return (
    <div data-testid="strategy-detail">
      <Panel
        title={<span title={name.title}>{name.text.toUpperCase()}</span>}
        note={state ? <StateWord word={state.word} tone={state.tone} /> : <Dash why={stateWhy} />}
      >
        <div className="stb" style={{ "--c": hue } as CSSProperties}>
          {tabs.map(([v, label, sub, why]) => (
            <span
              key={v}
              role="tab"
              tabIndex={0}
              aria-selected={tab === v}
              className={tab === v ? "on" : undefined}
              onClick={() => setTab(v)}
              onKeyDown={onKey(v)}
            >
              <b>{label}</b>
              <i title={why}>{sub}</i>
            </span>
          ))}
        </div>
        {body}
      </Panel>
    </div>
  );
}

export default function Strategies({ app, section }: PageProps) {
  const [pickedId, setPickedId] = useState<number | null>(null);
  const [tab, setTab] = usePageMemory<Tab>("research/strategies/tab", "over");
  const [editing, setEditing] = useState<EditorTarget | null>(null);
  const [creating, setCreating] = useState(false);
  const [stage, setStage] = useState(-1);
  const [family, setFamily] = useState("");
  const overviewQ = useQuery({ queryKey: strategiesKeys.overview, queryFn: () => fetchStrategiesOverview() });
  const verdictsQ = useQuery({ queryKey: judgeKeys.verdicts, queryFn: () => fetchJudgeVerdicts(VERDICT_LIMIT, 0) });
  const paperQ = useQuery({ queryKey: paperKeys.paper, queryFn: () => getRunningRuns() });
  const namesQ = useStrategyNames();
  // The generator's trees, the same reads the Generator page makes: where a theory strategy stands comes from its theory.
  const batchesQ = useQuery({ queryKey: generatorKeys.batches, queryFn: fetchBatches });
  const treesQ = useQueries({
    queries: (batchesQ.data ?? []).map((b) => ({ queryKey: generatorKeys.tree(b.id), queryFn: () => fetchTree(b.id) })),
  });

  const verdicts = verdictsQ.data?.rows ?? [];
  const paperIds = new Set((paperQ.data ?? []).map((r) => r.strategy_id));
  const verdictsWhy = verdictsQ.isError ? "Judge verdicts could not be read" : null;
  const paperWhy = paperQ.isError ? "Paper runs could not be read" : null;
  const settled = !verdictsQ.isPending && !paperQ.isPending;
  const treesFailed = batchesQ.isError || treesQ.some((t) => t.isError);
  const treesReady = batchesQ.isSuccess && treesQ.every((t) => t.isSuccess);
  const trees: GenTree[] = treesQ.flatMap((t) => (t.data ? [t.data] : []));
  const nameIds = (namesQ.data ?? []).map((r) => r.id);
  const hueOf = (id: number) => strategyHue(id, nameIds);

  const all = [...(overviewQ.data ?? [])].sort((a, b) => {
    if (a.display_name == null || b.display_name == null) {
      if (a.display_name == null && b.display_name == null) return a.id - b.id;
      return a.display_name == null ? 1 : -1;
    }
    return a.display_name.toLowerCase().localeCompare(b.display_name.toLowerCase()) || a.id - b.id;
  });

  // Where each strategy stands. A source that failed shows as a dash with its reason; one still loading shows nothing yet.
  const roadRow = (s: StrategyOverviewRow): RoadRow => {
    const found = theoryOf(s, trees);
    const name = nameOf(s);
    const blank = { s, found, name, where: null };
    if (paperWhy !== null) return { ...blank, stateWhy: paperWhy };
    if (verdictsWhy !== null) return { ...blank, stateWhy: verdictsWhy };
    if (!settled) return { ...blank, stateWhy: null };
    if (s.name.startsWith(THEORY_PREFIX) && !treesReady) {
      return { ...blank, stateWhy: treesFailed ? "The generator's searches could not be read" : null };
    }
    return { ...blank, where: whereOf(s, found, newestVerdict(verdicts, s.id), paperIds.has(s.id)), stateWhy: null };
  };
  const everyRow = all.map(roadRow);

  // The day a strategy was last changed: a theory strategy was written when its search was approved; one written by hand
  // keeps its versions, and the newest version's day is read.
  const byHand = everyRow.filter((r) => r.found === null && !r.s.name.startsWith(THEORY_PREFIX)).map((r) => r.s.id);
  const versionsQ = useQueries({
    queries: byHand.map((id) => ({ queryKey: strategiesKeys.versions(id), queryFn: () => fetchStrategyVersions(id) })),
  });
  const handChanged = new Map(byHand.map((id, i) => {
    const v = versionsQ[i].data;
    return [id, v && v.length > 0 ? Math.max(...v.map((x) => x.edited_at)) : null] as const;
  }));
  const items = everyRow.map((r) => itemOf(r, r.found ? r.found.approvedAt : handChanged.get(r.s.id) ?? null));

  // The finding, once every source has answered.
  const placed = items.filter((i) => i.kind !== null);
  let say: ReactNode = null;
  if (all.length > 0 && placed.length === all.length) {
    const n = all.length;
    const stopped = placed.filter((i) => i.kind === "stopped").length;
    const first = placed.filter((i) => i.kind === "stopped" && i.at === 0).length;
    const moving = placed.filter((i) => i.kind === "moving").length;
    const paper = placed.filter((i) => i.kind === "paper").length;
    const head = stopped === n ? "None got past its first test." : paper > 0 ? `${paper} ${paper === 1 ? "is" : "are"} on paper.` : `${stopped} stopped, ${moving} still moving.`;
    say = (
      <Say
        lead={`${n} strateg${n === 1 ? "y" : "ies"}. ${head}`}
        more={<>
          {first > 0 && <><em className="bad">{`${first} stopped at the first test`}</em>{": the best attempt of each missed at least one target. "}</>}
          {first === 0 && stopped > 0 && <><em className="bad">{`${stopped} stopped`}</em>{". "}</>}
          {paper === 0 && "Nothing is on paper yet."}
        </>}
      />
    );
  }

  const newButton = <PhoneOff><button type="button" className="btn" onClick={() => setCreating(true)}>New strategy</button></PhoneOff>;
  const picked = all.find((s) => s.id === pickedId) ?? null;
  const pickedItem = items.find((i) => i.r.s.id === pickedId) ?? null;
  const pickedVerdict = picked ? newestVerdict(verdicts, picked.id) : null;
  const pickedState = picked && settled && paperWhy === null && verdictsWhy === null
    ? strategyState(picked, verdicts, paperIds)
    : null;

  // The editor replaces the page; BACK clears it and, after a save, picks the strategy.
  if (editing !== null) {
    return (
      <StrategyEditor
        target={editing}
        onBack={(savedId) => {
          setEditing(null);
          if (savedId !== null) setPickedId(savedId);
        }}
      />
    );
  }

  const road = picked ? <RoadToPaper key={picked.id} s={picked} verdict={pickedVerdict} onEditCode={(id) => setEditing({ kind: "edit", id })} /> : null;
  const toList = () => document.getElementById("all-strategies")?.scrollIntoView?.({ block: "start" });
  let body: ReactNode;
  if (overviewQ.isError) body = <p className="empty">{serverDetail(overviewQ.error)}</p>;
  else if (!overviewQ.data) body = <p className="empty">Loading the strategies.</p>;
  else if (all.length === 0) body = <p className="empty">No strategies yet. A strategy appears here once the Generator writes one or you add one yourself.</p>;
  else {
    body = (
      <>
        <StageLine items={items} stage={stage} onStage={(i) => { setStage(i); if (i >= 0) toList(); }} />
        <Families items={items} family={family} onFamily={(f) => { setFamily(f); if (f) toList(); }} />
        <StrategyTable items={items} stage={stage} family={family}
          onClear={(what) => (what === "stage" ? setStage(-1) : setFamily(""))} onOpen={setPickedId} />
      </>
    );
  }

  return (
    <div className="v2-page sr6">
      <PageHeader crumb={app.name} title={section.label} right={newButton} />
      {say}
      {verdictsQ.isError && <p className="rd" role="status">{serverDetail(verdictsQ.error)}</p>}
      {paperQ.isError && <p className="rd" role="status">{serverDetail(paperQ.error)}</p>}
      {treesFailed && <p className="rd" role="status">{serverDetail(batchesQ.error ?? treesQ.find((t) => t.isError)?.error)}</p>}
      {body}
      {picked && pickedItem && (
        <Window label={nameOf(picked).text} title={nameOf(picked).text} size="xl" className="sx-win" onClose={() => setPickedId(null)}>
          <p className="sx-lead">
            {pickedItem.r.where ? <State tone={pickedItem.r.where.tone} hover={pickedItem.r.where.why}>{pickedItem.kind === "stopped" && pickedItem.r.where.word.startsWith("Stopped") ? "Stopped" : pickedItem.r.where.word}</State> : <span title={pickedItem.r.stateWhy ?? "Still reading"}>-</span>}
            {pickedItem.at !== null && <State tone="dim">{STAGES[pickedItem.at][0]}</State>}
            <span>{`${pickedItem.r.found ? `${pickedItem.family} family` : "Written by hand"}, version ${picked.version}${pickedItem.changed === null ? "" : `, written ${day(pickedItem.changed, true)}`}`}</span>
          </p>
          {pickedItem.r.found && pickedItem.r.where ? (
            <SearchCard key={`search-${picked.id}`} s={picked} found={pickedItem.r.found} where={pickedItem.r.where}
              onCode={() => setEditing({ kind: "edit", id: picked.id })}
              middle={<><h3 className="sr-h3">What is left</h3>{road}</>} />
          ) : <><h3 className="sr-h3">What is left</h3>{road}</>}
          <div className="sr-more">
            <Detail key={picked.id} s={picked} verdict={pickedVerdict} state={pickedState}
              stateWhy={verdictsWhy ?? paperWhy ?? "Not backtested yet"} verdictsWhy={verdictsWhy} hue={hueOf(picked.id)} tab={tab} setTab={setTab} />
          </div>
        </Window>
      )}
      {creating && (
        <NewStrategy
          rows={all}
          app={app}
          onClose={() => setCreating(false)}
          onOpen={(target) => {
            setCreating(false);
            setEditing(target);
          }}
        />
      )}
    </div>
  );
}
