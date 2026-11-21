import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Block, Panel, Scatter, Segmented, StageLegend, State, StateWord, StepLine, Table, usePageMemory } from "../../../kit";
import type { Cell, ScatterAxis, ScatterPoint, Tone } from "../../../kit";
import { stepSharpes } from "./stepSharpe";
import type { StepSharpe } from "./stepSharpe";
import { YearsBand, bandAxis } from "../../../kit/YearsBand";
import { chainsKeys, fetchCells, fetchChain, fetchChains } from "../../../data/chainsApi";
import { fmtDmy } from "../../../../lib/format";
import { serverDetail } from "../../../../lib/errorText";
import type { Chain, ChainDetail, ChainGroup, ChainState, ChainStep, SweepCell, SweepCells } from "../../../../types/research";
import { ChooseWindow, RetryWindow, StartWindow, StopWindow } from "./ChainWindows";
import "./chain.css";
import { PhoneOff } from "../../../kit/phone";

// The Chain section: chains by group on the left; on the right the picked chain's steps, years, ending,
// story and cells, with its buttons. Mockup: docs/design/rail_mockup_2026-10-10.html at 05ac8de4,
// research/CHAIN. The Generator page reuses this section.

const STEPS: ChainStep[] = ["screen", "optimise", "choose", "replay", "walkforward", "judge"];
const STEP_CAPS: Record<ChainStep, string> = {
  screen: "SCREEN", optimise: "OPTIMISE", choose: "CHOOSE", replay: "REPLAY", walkforward: "WALK-FORWARD", judge: "JUDGE",
};
const STEP_NAME: Record<ChainStep, string> = {
  screen: "Screen", optimise: "Optimise", choose: "Choose", replay: "Replay", walkforward: "Walk-forward", judge: "Judge",
};
const TONE: Record<ChainState, Tone> = {
  waiting: "bl", screening: "bl", optimising: "bl", replaying: "bl", "walking-forward": "bl", judging: "bl",
  "choosing-cell": "a", done: "g", refused: "a", failed: "rd", stopped: "none",
};
const TONE_VAR: Record<Tone, string> = {
  g: "var(--g)", rd: "var(--r)", a: "var(--a)", bl: "var(--b)", none: "var(--soft)",
};
const NOT_REACHED = "#3a424d";
const GROUPS: Array<{ label: string; value: string }> = [
  { label: "All", value: "all" },
  { label: "Running", value: "active" },
  { label: "Waiting on you", value: "waiting-on-you" },
  { label: "Can retry", value: "retryable" },
  { label: "Ended", value: "ended" },
];
const CELLS_SHOWN = 20;
const CELL_HEAD = ["Stock", "Variation", "Sharpe", "Return", "Deepest drop", "Trades", "Win rate", "Fees", "Slippage", ""];
const DUEL_REASON = "The server does not pair a chain's first and last equity curves yet";

// The state a story row leaves, and the step that state ran.
const LEAVES: Partial<Record<ChainState, ChainStep>> = {
  screening: "screen", optimising: "optimise", "choosing-cell": "choose", replaying: "replay", "walking-forward": "walkforward", judging: "judge",
};
const OPTIONS_COLOUR = "#4d9bff";
const two = (n: number) => n.toFixed(2);
const RUNS_PATH = "/app/research/runs";

const PARIS_TIME =new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const dayAndTime = (sec: number) => `${fmtDmy(sec)} ${PARIS_TIME.format(new Date(sec * 1000))}`;
const nameOf = (c: Chain) => c.display_name ?? "Unnamed strategy";
const percent = (v: number) => `${Math.round(v * 100)}%`;

type View = { group: string; pickedId: number | null };
type Write = { kind: "stop" | "retry" | "start" } | { kind: "choose"; cell: SweepCell };

function stepNumber(c: Chain): number {
  return c.step == null ? -1 : STEPS.indexOf(c.step);
}

function stepTitle(c: Chain): string {
  return c.step == null ? "Not started yet" : `Step ${stepNumber(c) + 1} of 6, ${STEP_CAPS[c.step]}`;
}

function Marks({ chain, runs }: { chain: Chain; runs: { optimise: number | null; walkforward: number | null } }) {
  const navigate = useNavigate();
  const at = chain.state === "done" ? STEPS.length : stepNumber(chain);
  const tone = TONE[chain.state];
  return (
    <div className="ch-marks" data-testid="chain-marks">
      {STEPS.map((s, i) => {
        const kind = i < at ? "done" : i === at ? "on" : "ahead";
        const colour = kind === "done" ? TONE_VAR.g : kind === "on" ? TONE_VAR[tone] : NOT_REACHED;
        const title = `${STEP_NAME[s]}: ${kind === "done" ? "done" : kind === "on" ? chain.state_word.toLowerCase() : "not reached"}`;
        const run = s === "optimise" ? runs.optimise : s === "walkforward" ? runs.walkforward : null;
        const common = {
          className: "ch-chip", "data-mark": kind, "data-step": s, "data-tone": kind === "on" ? tone : undefined,
          style: { ["--k" as string]: colour },
        };
        if (run != null && kind !== "ahead") {
          return (
            <button key={s} type="button" {...common} title={`${title}; open its run`} onClick={() => navigate(`${RUNS_PATH}?run=${run}`)}>
              <i />{STEP_CAPS[s]}
            </button>
          );
        }
        return (
          <span key={s} {...common} title={title}>
            <i />{STEP_CAPS[s]}
          </span>
        );
      })}
    </div>
  );
}

function Ending({ chain }: { chain: Chain }) {
  let word: ReactNode;
  if (chain.state === "refused" || chain.state === "failed" || chain.state === "stopped") {
    const at = chain.ended_at_step ?? chain.step;
    word = <StateWord word={at ? `${chain.state_word} AT ${STEP_CAPS[at]}` : chain.state_word} tone={TONE[chain.state]} />;
  } else if (chain.state === "done") word = <StateWord word="DONE" tone="g" />;
  else if (chain.group === "waiting-on-you") word = <StateWord word="WAITING ON YOU" tone="a" />;
  else word = <StateWord word="STILL MOVING" tone="bl" />;
  return (
    <span className="ch-ending" data-testid="chain-ending">
      {word}
      {chain.reason_sentence && <span className="ch-soft">{chain.reason_sentence}</span>}
      {chain.note_word && <span className="ch-note">{chain.note_word}</span>}
    </span>
  );
}

// What a story row shows at its right when its from-state ended a step: before and after, and the change.
function Figure({ sharpe }: { sharpe: StepSharpe }) {
  if (sharpe.value == null) return <span data-figure className="ch-fig"><span title={sharpe.why ?? undefined}>-</span></span>;
  if (sharpe.change == null) return <span data-figure className="ch-fig num">{two(sharpe.value)}</span>;
  const before = sharpe.value - sharpe.change;
  const moved = Number(two(sharpe.change)) !== 0;
  const up = sharpe.change > 0;
  return (
    <span data-figure className="ch-fig num">
      {`${two(before)} → ${two(sharpe.value)}`}
      {moved
        ? <span data-change className={up ? "g" : "rd"}>{up ? `▲ +${two(sharpe.change)}` : `▼ ${two(sharpe.change)}`}</span>
        : <span data-change className="ch-soft">=</span>}
    </span>
  );
}

function Story({ detail, loading, error, sharpes }: { detail: ChainDetail | null; loading: boolean; error: string | null; sharpes: StepSharpe[] | null }) {
  let body: ReactNode;
  if (error) body = <p className="empty">{error}</p>;
  else if (loading || !detail) body = <p className="empty">Loading the story.</p>;
  else if (detail.transitions.length === 0) body = <p className="empty">No steps are recorded yet.</p>;
  else {
    body = (
      <div className="ch-hist">
        {detail.transitions.slice().sort((a, b) => b.created_at - a.created_at || b.id - a.id).map((t) => {
          const leaves = t.from_state == null ? undefined : LEAVES[t.from_state];
          const sharpe = leaves && sharpes ? sharpes.find((s) => s.step === leaves) : undefined;
          return (
          <div key={t.id} data-transition>
            <span className="num ch-soft" data-date title={dayAndTime(t.created_at)}>{fmtDmy(t.created_at)}</span>
            <i className={t.actor === "page" ? "page" : "runner"} />
            <span>
              <b>{t.to_word}</b>
              {t.reason_sentence && <> {t.reason_sentence}</>}
              {t.actor === "page" && <> <StateWord word="YOU" tone="bl" /></>}
            </span>
            {sharpe && <Figure sharpe={sharpe} />}
          </div>
          );
        })}
      </div>
    );
  }
  return <div data-testid="chain-story"><Panel title={<span className="ch-h2">THE STORY</span>}>{body}</Panel></div>;
}

type CellsQuery = UseQueryResult<SweepCells>;

function Cells({
  chain, detail, cellsQ, onChoose,
}: { chain: Chain; detail: ChainDetail | null; cellsQ: CellsQuery; onChoose: (cell: SweepCell) => void }) {
  const [showAll, setShowAll] = useState(false);
  const run = detail?.optimise_run_id ?? null;
  const sorted = (cellsQ.data?.cells ?? []).slice().sort((a, b) => (b.sharpe_ratio ?? -Infinity) - (a.sharpe_ratio ?? -Infinity));
  const shown = showAll ? sorted : sorted.slice(0, CELLS_SHOWN);

  const dash = (reason: string) => <span title={reason}>-</span>;
  const metric = (v: number | null, column: string, show: (n: number) => string): Cell => ({
    v: v == null ? dash(`No ${column.toLowerCase()} was saved for this cell`) : show(v),
    cls: "num",
  });

  let body: ReactNode;
  if (detail == null) body = <p className="empty">Loading the cells.</p>;
  else if (run == null) body = <div className="ch-dash" title="This chain has no optimise run yet">-</div>;
  else if (cellsQ.isError) body = <p className="empty">{serverDetail(cellsQ.error)}</p>;
  else if (cellsQ.isPending) body = <p className="empty">Loading the cells.</p>;
  else if (sorted.length === 0) body = <div className="ch-dash" title="The optimise run saved no cells">-</div>;
  else {
    const rows: Cell[][] = shown.map((c) => {
      const chosen = chain.chosen_variation_id === c.variation_id && chain.chosen_ticker === c.ticker;
      return [
        { v: c.ticker, cls: "ch-hi" },
        { v: `v${c.variation_id}`, cls: "num" },
        metric(c.sharpe_ratio, "Sharpe", (n) => n.toFixed(2)),
        metric(c.total_return, "Return", percent),
        metric(c.max_drawdown, "Deepest drop", (n) => `-${percent(Math.abs(n))}`),
        metric(c.num_trades, "Trades", (n) => String(n)),
        metric(c.win_rate, "Win rate", percent),
        metric(c.total_fees_paid, "Fees", (n) => n.toFixed(2)),
        metric(c.total_slippage_cost, "Slippage", (n) => n.toFixed(2)),
        chosen ? <StateWord word="CHOSEN" tone="g" />
          : chain.state === "choosing-cell" ? <PhoneOff><button type="button" className="btn q ch-small" onClick={() => onChoose(c)}>CHOOSE</button></PhoneOff>
          : "",
      ];
    });
    body = (
      <div data-testid="chain-cells">
        <Table head={CELL_HEAD} rows={rows} />
        {!showAll && sorted.length > CELLS_SHOWN && (
          <button type="button" className="ch-more" onClick={() => setShowAll(true)}>{`SHOW ALL ${sorted.length}`}</button>
        )}
      </div>
    );
  }
  return (
    <div data-testid="chain-cells-panel">
      <Panel
        title={<span className="ch-h2">CHOOSE THE CELL</span>}
        note={sorted.length > 0 ? <span className="num" data-testid="chain-cells-title">{sorted.length}</span> : undefined}
      >
        {body}
      </Panel>
    </div>
  );
}

const roundUp = (v: number, step: number) => Math.ceil(v / step - 1e-9) * step;
const roundDown = (v: number, step: number) => Math.floor(v / step + 1e-9) * step;

function optionsView(cells: SweepCell[]) {
  const drawn = cells.filter((c) => c.sharpe_ratio != null && c.max_drawdown != null);
  const drops = drawn.map((c) => Math.abs(c.max_drawdown as number));
  const sharpes = drawn.map((c) => c.sharpe_ratio as number);
  const xHi = Math.max(0.25, roundUp(Math.max(0, ...drops), 0.05));
  const yLo = Math.min(-0.5, roundDown(Math.min(0, ...sharpes), 0.5));
  const yHi = Math.max(1, roundUp(Math.max(0, ...sharpes), 0.5));
  const x: ScatterAxis = {
    lo: 0, hi: xHi, ticks: [0, 1, 2, 3, 4].map((i) => (xHi * i) / 4),
    text: (v) => (v === 0 ? "0" : `-${Number((v * 100).toFixed(1))}%`), name: "deepest drop",
  };
  const y: ScatterAxis = {
    lo: yLo, hi: yHi, ticks: Array.from({ length: Math.round((yHi - yLo) / 0.5) + 1 }, (_, i) => yLo + i * 0.5),
    text: (v) => v.toFixed(1), name: "Sharpe",
  };
  const points: ScatterPoint[] = drawn.map((c) => ({
    id: `${c.variation_id}-${c.ticker}`,
    x: Math.abs(c.max_drawdown as number),
    y: c.sharpe_ratio as number,
    r: 3.5 + Math.min(6, (c.num_trades ?? 0) / 30),
    colour: OPTIONS_COLOUR,
    title: `${c.ticker}, v${c.variation_id}: Sharpe ${two(c.sharpe_ratio as number)}, deepest drop -${Math.round(Math.abs(c.max_drawdown as number) * 100)}%`,
  }));
  return { x, y, points, left: cells.length - drawn.length };
}

function Numbers({ chain, detail, cellsQ, sharpes, onChoose }: {
  chain: Chain; detail: ChainDetail | null; cellsQ: CellsQuery; sharpes: StepSharpe[] | null; onChoose: (cell: SweepCell) => void;
}) {
  const run = detail?.optimise_run_id ?? null;
  const cells = cellsQ.data?.cells ?? [];

  let line: ReactNode;
  if (detail == null) line = <p className="empty">Loading the Sharpe.</p>;
  else if (run != null && cellsQ.isError) line = <p className="empty">{serverDetail(cellsQ.error)}</p>;
  else if (sharpes == null) line = <p className="empty">Loading the Sharpe.</p>;
  else {
    line = (
      <StepLine
        testid="chain-sharpe"
        fmt={two}
        steps={sharpes.map((s) => ({ step: s.step, label: STEP_NAME[s.step], value: s.value, why: s.why, change: s.change }))}
      />
    );
  }

  let options: ReactNode;
  let count: ReactNode;
  if (detail == null) options = <p className="empty">Loading the cells.</p>;
  else if (run == null) options = <div className="ch-dash" title="This chain has no optimise run yet">-</div>;
  else if (cellsQ.isError) options = <p className="empty">{serverDetail(cellsQ.error)}</p>;
  else if (cellsQ.isPending) options = <p className="empty">Loading the cells.</p>;
  else if (cells.length === 0) options = <div className="ch-dash" title="The optimise run saved no cells">-</div>;
  else {
    const view = optionsView(cells);
    const chosenId = chain.chosen_variation_id != null && chain.chosen_ticker != null ? `${chain.chosen_variation_id}-${chain.chosen_ticker}` : null;
    count = (
      <span
        className="num"
        data-testid="chain-options-count"
        title={view.left === 0 ? undefined : `${view.left} ${view.left === 1 ? "cell has" : "cells have"} no Sharpe or no deepest drop and ${view.left === 1 ? "is" : "are"} not drawn`}
      >
        {view.points.length}
      </span>
    );
    options = (
      <Scatter
        testid="chain-options"
        label="The options at the choose step"
        x={view.x}
        y={view.y}
        points={view.points}
        picked={chosenId}
        pointAttr="data-cell"
        onPick={(id) => {
          if (chain.state !== "choosing-cell") return;
          const hit = cells.find((c) => `${c.variation_id}-${c.ticker}` === id);
          if (hit) onChoose(hit);
        }}
      />
    );
  }

  return (
    <div className="ch-row3">
      <Panel title={<span className="ch-h2">SHARPE, STEP BY STEP</span>}>{line}</Panel>
      <Panel title={<span className="ch-h2">OPTIONS AT CHOOSE</span>} note={count}>{options}</Panel>
    </div>
  );
}

function ChainPick({ list, view, setView, onWrite, years }: {
  years: ReactNode;
  list: { chains: Chain[]; cut: number | null; pending: boolean; error: string | null };
  view: View;
  setView: (next: View | ((prev: View) => View)) => void;
  onWrite: (w: Write) => void;
}) {
  const count = (g: string) => list.chains.filter((c) => g === "all" || c.group === (g as ChainGroup)).length;
  const pool = list.chains.filter((c) => view.group === "all" || c.group === (view.group as ChainGroup));
  const picked = pool.find((c) => c.id === view.pickedId) ?? pool[0] ?? null;

  const detailQ = useQuery({
    queryKey: chainsKeys.detail(picked?.id ?? 0),
    queryFn: () => fetchChain(picked!.id),
    enabled: picked != null,
  });
  const detail = detailQ.data && picked && detailQ.data.id === picked.id ? detailQ.data : null;
  const navigate = useNavigate();
  const optimiseRun = detail?.optimise_run_id ?? null;
  const cellsQ = useQuery({
    queryKey: chainsKeys.cells(optimiseRun ?? 0),
    queryFn: () => fetchCells(optimiseRun!),
    enabled: optimiseRun != null,
  });
  const sharpes: StepSharpe[] | null =
    detail == null ? null : optimiseRun == null ? stepSharpes(detail, null) : cellsQ.isSuccess ? stepSharpes(detail, cellsQ.data.cells) : null;
  const openRun = detail == null ? null : detail.walkforward_run_id ?? detail.optimise_run_id;
  const openWhy = detail == null ? "Loading the chain" : openRun == null ? "This chain has no run yet" : detail.walkforward_run_id != null ? "Open the walk-forward run" : "Open the optimise run";

  const tone = (c: Chain): "warn" | "good" | "dim" => (c.group === "waiting-on-you" ? "warn" : c.group === "active" ? "good" : "dim");
  let items: ReactNode;
  if (list.error) items = <p className="empty">{list.error}</p>;
  else if (list.pending) items = <p className="empty">Loading the chains.</p>;
  else if (list.chains.length === 0) {
    items = (
      <div className="cn-none">
        <p className="empty">No chains yet. Start one and it shows here.</p>
        <PhoneOff><button type="button" className="btn" onClick={() => onWrite({ kind: "start" })}>Start a chain</button></PhoneOff>
      </div>
    );
  } else if (pool.length === 0) items = <p className="empty">No chains in this group.</p>;
  else {
    items = (
      <Table
        head={["Chain", "Step", "State"]}
        rows={pool.map((c): Cell[] => [
          <button key="n" type="button" className={`ch-item${c.id === picked?.id ? " on" : ""}`} data-chain={c.id} title={stepTitle(c)} onClick={() => setView((v) => ({ ...v, pickedId: c.id }))}>
            <span className="ch-iname" title={c.display_name == null ? "This strategy has no display name" : undefined}>{nameOf(c)}</span>
          </button>,
          c.step == null ? "Not started" : STEP_NAME[c.step],
          <span key="s" className="ch-istate"><State tone={tone(c)}>{c.state_word}</State>{c.note_word && <span className="ch-note">{c.note_word}</span>}</span>,
        ])}
      />
    );
  }

  const left = (
    <Block
      title="All chains"
      right={
        <Segmented
          variant="seg"
          options={GROUPS.map((g) => ({ label: `${g.label} ${count(g.value)}`, value: g.value }))}
          value={view.group}
          onChange={(group) => setView((v) => ({ ...v, group }))}
        />
      }
    >
      <div data-testid="chain-list">{items}</div>
    </Block>
  );

  const stretches = picked
    ? [
        { stage: "screen" as const, from: picked.screen_start_ts, to: picked.optimise_start_ts },
        { stage: "optimise" as const, from: picked.optimise_start_ts, to: picked.walkforward_start_ts },
        { stage: "walkforward" as const, from: picked.walkforward_start_ts, to: picked.walkforward_end_ts },
      ]
    : [];
  const axis = picked ? bandAxis(stretches, list.cut) : null;
  const canRetry = picked?.group === "retryable";
  const canStop = picked != null && (picked.group === "active" || picked.group === "waiting-on-you");

  const right = picked && axis && (
    <div className="ch-right">
      <Panel>
        <div className="ch-top">
          <span className="ch-name">{nameOf(picked)}</span>
          <Ending chain={picked} />
          <span className="ch-btns" data-testid="chain-actions">
            <button type="button" className="btn q" disabled={openRun == null} title={openWhy} onClick={() => navigate(`${RUNS_PATH}?run=${openRun}`)}>OPEN RUN</button>
            {canRetry && <PhoneOff><button type="button" className="btn" onClick={() => onWrite({ kind: "retry" })}>RETRY</button></PhoneOff>}
            {canStop && (
              <button
                type="button"
                className="btn danger"
                disabled={picked.stop_requested === 1}
                title={picked.stop_requested === 1 ? "A stop is already asked for" : undefined}
                onClick={() => onWrite({ kind: "stop" })}
              >
                STOP
              </button>
            )}
          </span>
        </div>
        <Marks chain={picked} runs={{ optimise: detail?.optimise_run_id ?? null, walkforward: detail?.walkforward_run_id ?? null }} />
        <div className="ch-years" data-testid="chain-years">
          <StageLegend />
          <YearsBand stretches={stretches} cut={list.cut} from={axis.from} to={axis.to} />
        </div>
      </Panel>
      <Numbers chain={picked} detail={detail} cellsQ={cellsQ} sharpes={sharpes} onChoose={(cell) => onWrite({ kind: "choose", cell })} />
      <div className="ch-row2">
        <Story detail={detail} loading={detailQ.isPending} error={detailQ.isError ? serverDetail(detailQ.error) : null} sharpes={sharpes} />
        <div data-testid="chain-duel">
          <Panel title={<span className="ch-h2" title={DUEL_REASON}>FIRST AGAINST LAST</span>}>
            <div className="ch-dash ch-big" title={DUEL_REASON}>-</div>
          </Panel>
        </div>
      </div>
      <Cells key={picked.id} chain={picked} detail={detail} cellsQ={cellsQ} onChoose={(cell) => onWrite({ kind: "choose", cell })} />
    </div>
  );

  return (
    <>
      <div className="cn-pair">
        {years}
        {left}
      </div>
      {right}
    </>
  );
}

export function ChainSection({ memoryKey, years, startSignal }: { memoryKey: string; years: ReactNode; startSignal: number }) {
  const [view, setView] = usePageMemory<View>(memoryKey, { group: "all", pickedId: null });
  const group = GROUPS.some((g) => g.value === view.group) ? view.group : "all";
  const [open, setOpen] = useState<Write | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const qc = useQueryClient();
  // The page's Start a chain button asks for the window by raising this number.
  useEffect(() => {
    if (startSignal > 0) setOpen({ kind: "start" });
  }, [startSignal]);

  const listQ = useQuery({ queryKey: chainsKeys.list, queryFn: fetchChains });
  const list = {
    chains: listQ.data?.chains ?? [],
    cut: listQ.data?.reserve_cut_ts ?? null,
    pending: listQ.isPending,
    error: listQ.isError ? serverDetail(listQ.error) : null,
  };
  const picked = (() => {
    const pool = list.chains.filter((c) => group === "all" || c.group === (group as ChainGroup));
    return pool.find((c) => c.id === view.pickedId) ?? pool[0] ?? null;
  })();

  const done = (text: string, result: { chain_id: number }) => {
    setOpen(null);
    setStatus(text);
    if (open?.kind === "start") setView({ group: "all", pickedId: result.chain_id });
    void qc.invalidateQueries({ queryKey: ["chains"] });
  };

  return (
    <div>
      {status && <p role="status" className="ch-status">{status}</p>}
      <ChainPick years={years} list={list} view={{ ...view, group }} setView={setView} onWrite={setOpen} />
      {open?.kind === "stop" && picked && <StopWindow chain={picked} onClose={() => setOpen(null)} onDone={done} />}
      {open?.kind === "retry" && picked && <RetryWindow chain={picked} onClose={() => setOpen(null)} onDone={done} />}
      {open?.kind === "choose" && picked && <ChooseWindow chain={picked} cell={open.cell} onClose={() => setOpen(null)} onDone={done} />}
      {open?.kind === "start" && <StartWindow reserveCut={list.cut} onClose={() => setOpen(null)} onDone={done} />}
    </div>
  );
}
