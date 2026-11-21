import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Facts, Panel, StateWord, STRETCHES, Table, Track, Window, YearsBand } from "../../../kit";
import type { Cell, Stage } from "../../../kit";
import { bandAxis } from "../../../kit/YearsBand";
import {
  deleteBatch, fetchBatches, fetchTree, generatorKeys, pauseBatch, resumeBatch, stopBatch,
} from "../../../data/generatorApi";
import type { GenBatch, GenTheory, GenTree, StopAnswer } from "../../../data/generatorApi";
import { serverDetail } from "../../../../lib/errorText";
import { fmtDmy } from "../../../../lib/format";
import { batchState, endedCount, passedCount, runWord, stageRun, TONE_COLOUR, withoutSearch } from "./searchState";
import type { Word } from "./searchState";
import { PhoneOff } from "../../../kit/phone";
import { TheoryCards } from "./TheoryCards";
import "./searches.css";

// Section 2, THE SEARCHES, first half: the batches, the picked batch's facts and years, its theories with their
// runs per stage, and RESUME, PAUSE and STOP. Mockup: docs/design/rail_mockup_2026-10-10.html at 05ac8de4, genBatches.
// The runner passes every 30 seconds, so the page reads again as often.

const REFRESH_MS = 30_000;
const STAGES: Stage[] = ["screen", "optimise", "walkforward", "later"];
const RUN_STAGES = [
  { stage: "screen", label: "screen" },
  { stage: "optimise", label: "optimise" },
  { stage: "walkforward", label: "walk-forward" },
] as const;

const epoch = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;

type Action = "resume" | "pause" | "stop" | "delete";

function WordView({ w }: { w: Word }) {
  return (
    <span title={w.hover}>
      <StateWord word={w.word} tone={w.tone} />
    </span>
  );
}

function Dash({ hover }: { hover: string }) {
  return <span className="gs-dash" title={hover}>-</span>;
}

/** The stretches of a batch, in the order the colour key shows them. */
function stretchesOf(batch: GenBatch) {
  const windows: Record<Stage, [string, string]> = {
    screen: [batch.screen_start, batch.screen_end],
    optimise: [batch.optimise_start, batch.optimise_end],
    walkforward: batch.walkforward,
    later: [batch.later_start, batch.later_end],
  };
  return STAGES.map((stage) => ({ stage, from: epoch(windows[stage][0]), to: epoch(windows[stage][1]) }));
}

function ActionWindow({
  action, batch, tree, word, onClose, onDeleted,
}: {
  action: Action; batch: GenBatch; tree: GenTree | undefined; word: Word | null; onClose: () => void; onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const [stopped, setStopped] = useState<StopAnswer | null>(null);

  const label = { resume: "Resume the batch", pause: "Pause the batch", stop: "Stop the batch", delete: "Delete the batch" }[action];
  const verb = action.toUpperCase();

  async function go() {
    if (busy) return;
    setBusy(true);
    setRefused(null);
    try {
      if (action === "stop") {
        setStopped(await stopBatch(batch.id));
      } else if (action === "delete") {
        await deleteBatch(batch.id);
      } else {
        await (action === "resume" ? resumeBatch(batch.id) : pauseBatch(batch.id));
      }
      void qc.invalidateQueries({ queryKey: generatorKeys.all });
      if (action === "delete") onDeleted();
      if (action !== "stop") onClose();
    } catch (err) {
      setRefused(serverDetail(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Window label={label} size="s" onClose={onClose}>
      <div className="h2">{label.toUpperCase()}</div>
      {stopped == null && action === "resume" && (
        <>
          <div className="gs-lines"><div>Starts a search for every theory that has none.</div></div>
          <Facts items={[
            ["STATE", word?.word ?? "-"],
            ["NO SEARCH YET", tree == null ? "-" : String(withoutSearch(tree))],
          ]} />
        </>
      )}
      {action === "pause" && <div className="gs-lines"><div>No new run starts until RESUME.</div></div>}
      {stopped == null && action === "stop" && (
        <div className="gs-lines"><div>Every unfinished theory ends. A stopped batch cannot be resumed.</div></div>
      )}
      {stopped != null && (
        <>
          <div className="gs-result"><StateWord word="STOPPED" tone="rd" /></div>
          <Facts items={[
            ["RUNS CANCELLED", String(stopped.cancelled_runs.length)],
            ["STILL RUNNING", String(stopped.still_running.length), undefined, "These runs had started and finish on their own"],
          ]} />
        </>
      )}
      {action === "delete" && (
        <div className="gs-lines">
          <div>The batch leaves this page. Its runs, their files and the record of the data it read are kept.</div>
        </div>
      )}
      {refused != null && <div className="gs-fail">{refused}</div>}
      <div className="gs-win-actions">
        {stopped == null ? (
          <>
            <button type="button" className="btn q" onClick={onClose}>CANCEL</button>
            <button type="button" className={action === "stop" || action === "delete" ? "btn danger" : "btn"} disabled={busy} onClick={() => void go()}>
              {verb}
            </button>
          </>
        ) : (
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        )}
      </div>
    </Window>
  );
}

function BatchItem({
  batch, tree, failed, on, onPick,
}: { batch: GenBatch; tree: GenTree | undefined; failed: unknown; on: boolean; onPick: () => void }) {
  const word = batchState(batch, tree);
  const colour = word == null ? TONE_COLOUR.none : TONE_COLOUR[word.tone];
  let state: ReactNode;
  if (word != null) {
    const { ended, total } = tree == null ? { ended: 0, total: 0 } : endedCount(tree);
    state = (
      <>
        <WordView w={word} />
        <span className="gs-grow"><Track fraction={total === 0 ? 0 : ended / total} colour={colour} /></span>
      </>
    );
  } else {
    state = <><Dash hover={failed != null ? serverDetail(failed) : "Reading this batch"} /><span className="gs-grow" /></>;
  }
  return (
    <button
      type="button" className={`gs-item${on ? " on" : ""}`} data-batch={batch.id}
      style={{ "--k": colour } as CSSProperties} onClick={onPick}
    >
      <span className="gs-line">
        <span className="gs-name">{batch.display_name}</span>
        {tree != null && <span className="gs-tpl">{tree.template.display_name}</span>}
      </span>
      <span className="gs-line">
        {state}
        <span className="gs-date num">{fmtDmy(batch.approved_at)}</span>
      </span>
    </button>
  );
}

function runCell(theory: GenTheory, stage: string, label: string): Cell {
  const run = stageRun(theory, stage);
  if (run == null) return <Dash hover={`No ${label} run yet`} />;
  return <WordView w={runWord(run.status)} />;
}

function Detail({
  batch, tree, treeError, theoryId, onTheory, onAction,
}: {
  batch: GenBatch; tree: GenTree | undefined; treeError: unknown; theoryId: number | null;
  onTheory: (id: number) => void; onAction: (a: Action) => void;
}) {
  const word = batchState(batch, tree);
  const control = batch.control;
  const since = control.since == null ? null : fmtDmy(control.since);
  const stoppedHover = since == null ? "The batch was stopped" : `The batch was stopped on ${since}`;
  const searched = batch.members.filter((m) => !batch.kept_back.includes(m));

  const waiting = tree != null && withoutSearch(tree) > 0;
  let resumeOff: string | null = null;
  if (control.state === "stopped") resumeOff = stoppedHover;
  else if (control.state === "running") {
    if (tree == null) resumeOff = "Reading this batch";
    else if (!waiting) resumeOff = "Every theory in this batch already has its search";
  }
  let pauseOff: string | null = null;
  if (control.state === "stopped") pauseOff = stoppedHover;
  else if (control.state === "paused") pauseOff = "The batch is paused";
  const stopOff = control.state === "stopped" ? stoppedHover : null;
  let deleteOff: string | null = null;
  if (tree == null) deleteOff = "Reading this batch";
  else if (control.state !== "stopped") deleteOff = "Stop the batch first";
  else if (tree.theories.some((th) => th.runs.some((r) => r.status === "pending" || r.status === "running"))) {
    deleteOff = "Runs of this batch are still going";
  } else if (tree.theories.some((th) => th.saved.some((sv) => sv.verdict != null))) {
    deleteOff = "The judge graded a strategy of this batch; its record stays";
  }

  const stretches = stretchesOf(batch);
  const axis = bandAxis(stretches, null);
  const picked = tree?.theories.find((t) => t.id === theoryId) ?? tree?.theories[0];

  let table: ReactNode;
  if (tree == null) {
    table = treeError != null ? <div className="gs-fail">{serverDetail(treeError)}</div> : <Dash hover="Reading this batch" />;
  } else if (tree.theories.length === 0) {
    table = <Dash hover="This batch has no theories" />;
  } else {
    const head: ReactNode[] = [
      "Theory",
      ...RUN_STAGES.map((s) => {
        const meta = STRETCHES.find((m) => m.stage === s.stage)!;
        return <span key={s.stage}><i className="gs-key" style={{ background: meta.colour }} />{meta.label}</span>;
      }),
      "Passed",
    ];
    const rows: Cell[][] = tree.theories.map((t) => {
      const p = passedCount(t);
      return [
        {
          v: (
            <button type="button" className={`gs-th${picked?.id === t.id ? " on" : ""}`} onClick={() => onTheory(t.id)}>
              {t.display_name}
            </button>
          ),
        },
        ...RUN_STAGES.map((s) => runCell(t, s.stage, s.label)),
        t.saved.length === 0
          ? <Dash hover="Nothing is saved yet" />
          : { v: `${p} of ${t.saved.length}`, cls: p > 0 ? "g" : undefined },
      ];
    });
    table = <Table head={head} rows={rows} />;
  }

  return (
    <div className="gs-main" data-testid="gs-detail">
      <Panel>
        <div className="gs-head">
          <span className="gs-title">{batch.display_name}</span>
          {word != null && <WordView w={word} />}
          <span className="gs-actions">
            <button type="button" className="btn" disabled={resumeOff != null} title={resumeOff ?? undefined} onClick={() => onAction("resume")}>RESUME</button>
            <button type="button" className="btn q" disabled={pauseOff != null} title={pauseOff ?? undefined} onClick={() => onAction("pause")}>PAUSE</button>
            <button type="button" className="btn danger" disabled={stopOff != null} title={stopOff ?? undefined} onClick={() => onAction("stop")}>STOP</button>
            <PhoneOff>
              <button type="button" className="btn danger" disabled={deleteOff != null} title={deleteOff ?? undefined} onClick={() => onAction("delete")}>DELETE</button>
            </PhoneOff>
          </span>
        </div>
        <div className="gs-facts">
          <Facts items={[
            ["TEMPLATE", tree?.template.display_name ?? "-"],
            [
              "STOCKS", `${searched.length} + ${batch.kept_back.length} kept back`, undefined,
              `Searched: ${searched.join(", ")}. Kept back: ${batch.kept_back.join(", ")}.`,
            ],
            ["SEED", String(batch.seed)],
            ["APPROVED", fmtDmy(batch.approved_at)],
            ["THEORIES", tree == null ? "-" : String(tree.theories.length)],
          ]} />
        </div>
        <div className="gs-block">
          <span className="kl">THE FOUR STRETCHES</span>
          <YearsBand stretches={stretches} cut={null} from={axis.from} to={axis.to} />
        </div>
        <div className="gs-runs">{table}</div>
      </Panel>
      {picked != null && <TheoryCards theory={picked} />}
    </div>
  );
}

export function Searches({ initialBatch = null }: { initialBatch?: number | null }) {
  const batches = useQuery({ queryKey: generatorKeys.batches, queryFn: fetchBatches, refetchInterval: REFRESH_MS });
  const list = batches.data ?? [];
  const trees = useQueries({
    queries: list.map((b) => ({
      queryKey: generatorKeys.tree(b.id), queryFn: () => fetchTree(b.id), refetchInterval: REFRESH_MS,
    })),
  });
  const [batchId, setBatchId] = useState<number | null>(initialBatch);
  const [theoryId, setTheoryId] = useState<number | null>(null);
  const [action, setAction] = useState<Action | null>(null);

  const index = Math.max(0, list.findIndex((b) => b.id === batchId));
  const batch = list[index];
  const tree = trees[index]?.data;

  let left: ReactNode;
  if (batches.isError) left = <div className="gs-fail">{serverDetail(batches.error)}</div>;
  else if (batches.isPending) left = <Dash hover="Reading the batches" />;
  else if (list.length === 0) {
    left = (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, padding: "0 6px" }}>
        <span title="Approve a theory in section 1 to start a batch"><StateWord word="NO BATCH YET" tone="none" /></span>
        <button type="button" className="btn" onClick={() => document.getElementById("gs1")?.scrollIntoView({ behavior: "smooth" })}>
          START A THEORY
        </button>
      </div>
    );
  } else {
    left = list.map((b, i) => (
      <BatchItem
        key={b.id} batch={b} tree={trees[i]?.data} failed={trees[i]?.error ?? null} on={i === index}
        onPick={() => { setBatchId(b.id); setTheoryId(null); }}
      />
    ));
  }

  return (
    <div className="gs-grid">
      <Panel
        title={<span className="gs-hd">BATCHES{batches.data && <span className="num">{list.length}</span>}</span>}
        style={{ padding: "14px 10px" }}
      >
        {left}
      </Panel>
      {batch != null ? (
        <Detail
          batch={batch} tree={tree} treeError={trees[index]?.error ?? null} theoryId={theoryId}
          onTheory={setTheoryId} onAction={setAction}
        />
      ) : (
        <Panel><Dash hover="Pick a batch on the left" /></Panel>
      )}
      {action != null && batch != null && (
        <ActionWindow
          action={action} batch={batch} tree={tree} word={batchState(batch, tree)} onClose={() => setAction(null)}
          onDeleted={() => { setBatchId(null); setTheoryId(null); }}
        />
      )}
    </div>
  );
}
