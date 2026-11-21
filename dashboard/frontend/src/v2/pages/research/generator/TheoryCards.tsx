import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { DayBars, Facts, KzSteps, Legend, Panel, StateWord, Table } from "../../../kit";
import type { Cell, DayBar } from "../../../kit";
import { fetchJudgeVerdict } from "../../../data/judgeApi";
import type { GenTheory } from "../../../data/generatorApi";
import { serverDetail } from "../../../../lib/errorText";
import { heldCount, picksText, stepWord, verdictWord } from "./searchState";
import type { Word } from "./searchState";

// Section 2, THE SEARCHES, second half: the picked theory's search rounds, families, stop card and saved
// strategies. Mockup: docs/design/rail_mockup_2026-10-10.html at 05ac8de4, genBatches (G7 to G10).

const BLUE = "#4d9bff";
const BLUE_LIGHT = "rgba(77,155,255,.35)";
const GREEN = "#2fe08a";
const RED = "#ff5a63";
const GREY = "rgba(255,255,255,.14)";

interface Round {
  round: number;
  valid: number;
  tried: number;
  ran: string;
  kept: number;
}

interface Pick {
  picks: unknown;
  finalist_rank: number;
  optimise_sharpe: number;
  reason: string;
}

// The server sends rounds, best_trial_ids and finalists as loose JSON; read them here and say so when they are not the shape the page expects.
function readRounds(theory: GenTheory): { rounds: Round[]; stop: string; finalists: number; pick: Pick } | string {
  const search = theory.search;
  if (search == null) return "No search rounds yet: the search has not finished";
  const raw = search.rounds?.rounds;
  if (!Array.isArray(raw) || !Array.isArray(search.finalists) || typeof search.rounds.stop !== "string") {
    return "The server sent the search rounds in a shape this page does not know";
  }
  const rounds: Round[] = [];
  for (const r of raw) {
    if (!Array.isArray(r.best_trial_ids)) return "The server sent the search rounds in a shape this page does not know";
    rounds.push({
      round: r.round, valid: r.valid_strategies, tried: Number(r.strategies), ran: String(r.ran), kept: r.best_trial_ids.length,
    });
  }
  return { rounds, stop: search.rounds.stop, finalists: search.finalists.length, pick: search.pick };
}

const en = (n: number) => n.toLocaleString("en-US");

function Dash({ hover }: { hover: string }) {
  return <span className="gs-dash" title={hover}>-</span>;
}

function WordView({ w }: { w: Word }) {
  return (
    <span title={w.hover}>
      <StateWord word={w.word} tone={w.tone} />
    </span>
  );
}

function RoundsCard({ theory }: { theory: GenTheory }) {
  const read = readRounds(theory);
  const title = <span className="gs-hd">SEARCH ROUNDS</span>;
  if (typeof read === "string") {
    return (
      <Panel title={title} note={theory.display_name}>
        <Facts items={[["ROUNDS", "-", undefined, read], ["ENDED", "-", undefined, read], ["PICK", "-", undefined, read]]} />
      </Panel>
    );
  }
  const bars: DayBar[] = read.rounds.map((r) => ({
    parts: [
      { value: r.kept, colour: BLUE, name: "kept" },
      { value: Math.max(0, r.tried - r.kept), colour: BLUE_LIGHT, name: "tried" },
    ],
    hover: `Round ${r.round}: ${en(r.valid)} in the box, ${en(r.tried)} tried (${r.ran}), ${en(r.kept)} kept for the next round`,
  }));
  bars.push({ parts: [{ value: read.finalists, colour: GREEN, name: "finalists" }], hover: `Finalists: ${read.finalists}` });
  const tried = read.rounds.reduce((a, r) => a + r.tried, 0);
  const kept = read.rounds.reduce((a, r) => a + r.kept, 0);
  const pick = read.pick;
  const pickFact: [string, string, undefined, string] = pick.picks == null
    ? ["PICK", "-", undefined, pick.reason]
    : ["PICK", `#${pick.finalist_rank} ${picksText(pick.picks)}`, undefined, `Optimise Sharpe ${pick.optimise_sharpe.toFixed(2)}`];
  return (
    <Panel title={title} note={theory.display_name}>
      <DayBars bars={bars} size="card" />
      <Legend items={[
        ["TRIED", BLUE_LIGHT, { count: tried }],
        ["KEPT", BLUE, { count: kept }],
        ["FINALISTS", GREEN, { count: read.finalists }],
      ]} />
      <Facts items={[
        ["ROUNDS", String(read.rounds.length)],
        ["ENDED", read.stop.toUpperCase()],
        pickFact,
      ]} />
    </Panel>
  );
}

function NineChecks({ id }: { id: number }) {
  const q = useQuery({ queryKey: ["generator", "verdict", id], queryFn: () => fetchJudgeVerdict(id) });
  if (q.isError) return <Dash hover={serverDetail(q.error)} />;
  if (!q.data) return <Dash hover="Reading the judge's checks" />;
  const byCheck = new Map(q.data.check_outcomes.map((c) => [c.check, c.outcome]));
  const items: Array<[string, string]> = Array.from({ length: 9 }, (_, i) => {
    const o = byCheck.get(i + 1);
    return [`Check ${i + 1}`, o === "cleared" ? GREEN : o === "failed" ? RED : GREY];
  });
  return <KzSteps items={items} word="CLEARED" />;
}

function nothingSavedHover(theory: GenTheory): string {
  if (theory.stop != null) return "Nothing saved: the theory stopped";
  if (theory.final !== "not run" && heldCount(theory) === 0) return "Nothing saved: no strategy held up";
  return "Nothing saved yet";
}

function SavedCard({ theory }: { theory: GenTheory }) {
  let body: ReactNode;
  if (theory.saved.length === 0) {
    body = <Dash hover={nothingSavedHover(theory)} />;
  } else {
    const rows: Cell[][] = theory.saved.map((s) => {
      const judge = verdictWord(s);
      const nine: Cell = s.verdict == null
        ? <Dash hover="The judge refused, so no check ran" />
        : <NineChecks id={s.verdict.id} />;
      return [
        { v: <span title={s.display_name}>{`#${s.rank}`}</span> },
        { v: picksText(s.picks) },
        nine,
        <WordView key="w" w={judge} />,
      ];
    });
    body = <Table head={["Rank", "Values picked", "Nine checks", "Judge"]} rows={rows} />;
  }
  return (
    <div data-testid="gs-saved">
      <Panel title={<span className="gs-hd">SAVED STRATEGIES<span className="num">{theory.saved.length}</span></span>}>
        {body}
      </Panel>
    </div>
  );
}

export function TheoryCards({ theory }: { theory: GenTheory }) {
  return (
    <div data-testid="gs-theory" style={{ display: "grid", gap: 14, minWidth: 0 }}>
      <div data-testid="gs-cards" className="gs-cards" style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.5fr) minmax(0, 1fr)", gap: 14, alignItems: "start" }}>
        <RoundsCard theory={theory} />
        <Panel title={<span className="gs-hd">FAMILIES</span>}>
          <Dash hover="The server does not send the families yet (G10)" />
        </Panel>
      </div>
      {theory.stop != null && (
        <Panel style={{ borderColor: "rgba(255,90,99,.33)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <StateWord word={`STOPPED AT ${stepWord(theory.stop.step)}`} tone="rd" />
            <span>{theory.stop.reason}</span>
          </div>
        </Panel>
      )}
      <SavedCard theory={theory} />
    </div>
  );
}
