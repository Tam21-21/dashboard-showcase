import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { Block, Segmented, State } from "../../../kit";
import type { NlTone } from "../../../kit";
import { nearestOf } from "../generator/Closeness";
import type { RoadRow, Where } from "./Road";

// The three parts of the Strategies page that answer "how far did each get, and how do I find one among hundreds?"
// (the look trial's lab-p-strategies.js, 2026-11-14): the line that counts strategies at each stage, the tiles that count
// them by family, and the table with a search and filters. Nothing is drawn once per strategy except the table's rows.
// Where a strategy stands comes from strategies/Road.tsx (whereOf); here it is only counted and listed.

export const STAGES: Array<[string, string]> = [
  ["Screen", "A first test on the early years"],
  ["Optimise", "Its settings are tuned on the next years"],
  ["Walk-forward", "Tested on years it has not seen"],
  ["Judge", "Nine checks say pass or fail"],
  ["Reserve exam", "One last run on years kept back"],
  ["Second engine", "The same trades, replayed by a second program"],
  ["Paper", "Trading with pretend money, after you approve"],
];

export type Kind = "stopped" | "moving" | "paper" | "new";

/** Stopped, still moving, on paper, or not started, from the word the page already says for the strategy. */
export function kindOf(w: Where): Kind {
  if (w.at === 6) return "paper";
  if (w.tone === "bad" || w.word === "Stopped by you") return "stopped";
  if (w.tone === "dim") return "new";
  return "moving";
}

const HAND = "Written by hand";

/** One strategy as the three parts read it. `kind` and `at` are null while a source that places it is unread. */
export type Item = {
  r: RoadRow;
  family: string;
  kind: Kind | null;
  at: number | null;
  met: number | null;
  bars: number;
  runs: number | null;
  changed: number | null;
};

export function itemOf(r: RoadRow, changed: number | null): Item {
  const th = r.found?.theory ?? null;
  const n = th ? nearestOf(th) : null;
  const met = n !== null && n !== "odd" && !Number.isNaN(n.sharpe) ? n : null;
  return {
    r,
    family: r.found?.family ?? HAND,
    kind: r.where ? kindOf(r.where) : null,
    at: r.where ? r.where.at : null,
    met: met ? met.met : null,
    bars: met ? met.met + met.failed.length : 0,
    runs: th ? th.runs.length : null,
    changed,
  };
}

export const nameOfItem = (i: Item) => i.r.name.text;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function day(epoch: number | null, year: boolean): string {
  if (epoch === null) return "-";
  const d = new Date(epoch * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}${year ? ` ${d.getUTCFullYear()}` : ""}`;
}
const num = (n: number) => n.toLocaleString("en-GB");
const plural = (n: number, one: string, many: string) => `${num(n)} ${n === 1 ? one : many}`;

/** A bar cut into parts, one colour each; a part with nothing in it is not drawn. */
export function Share({ parts, kind = "bar" }: { parts: Array<[NlTone, number]>; kind?: "bar" | "parts" }) {
  const total = parts.reduce((a, p) => a + p[1], 0);
  if (total === 0) return <span className="sx-share none" />;
  return (
    <span className={`sx-share ${kind}`}>
      {parts.map(([tone, v]) => (v > 0 ? <i key={tone} className={tone} style={{ flexGrow: v }} /> : null))}
    </span>
  );
}

function stopState(here: Item[]): { tone: NlTone; word: string } {
  const stop = here.filter((i) => i.kind === "stopped").length;
  if (here.length === 0) return { tone: "dim", word: "None here" };
  if (stop === here.length) return { tone: "bad", word: `${stop} stopped here` };
  if (stop > 0) return { tone: "warn", word: `${stop} stopped, ${here.length - stop} moving` };
  return { tone: "good", word: `${here.length} moving` };
}

/** The line: how many strategies stand at each stage now. A stage is a button that narrows the table below. */
export function StageLine({ items, stage, onStage }: { items: Item[]; stage: number; onStage: (i: number) => void }) {
  const placed = items.filter((i) => i.at !== null);
  const far = Math.max(0, ...placed.map((i) => i.at as number));
  return (
    <Block className="sx-first" title={<span title="Press a stage to list its strategies">Strategies by stage</span>}>
      <div className="sx-line" data-testid="stage-line">
        <div className="sx-track"><i className="lit" style={{ width: `${(far / 7) * 100}%` }} /><i className="rest" style={{ left: `${(far / 7) * 100}%`, width: `${((6 - far) / 7) * 100}%` }} /></div>
        <div className="sx-stops">
          {STAGES.map(([name, what], i) => {
            const here = placed.filter((p) => p.at === i);
            const s = stopState(here);
            const stop = here.filter((p) => p.kind === "stopped").length;
            const reached = placed.filter((p) => (p.at as number) >= i).length;
            return (
              <button key={name} type="button" data-stage={i} aria-pressed={stage === i}
                className={`sx-stop ${s.tone}`} style={{ "--i": i } as CSSProperties}
                title={`${name}: ${what}. Here now: ${num(here.length)}. Stopped here: ${num(stop)}. Got this far or further: ${num(reached)}`}
                onClick={() => onStage(stage === i ? -1 : i)}>
                <span className="sx-node" />
                <span className="sx-sname">{name}</span>
                <span className="sx-fig num">{num(here.length)}</span>
                <span className="sx-svis"><Share parts={[["bad", stop], ["good", here.length - stop]]} /></span>
                <span className="sx-sfact">{what}</span>
                <State tone={s.tone}>{s.word}</State>
              </button>
            );
          })}
        </div>
      </div>
    </Block>
  );
}

const FAM_PAGE = 8;

/** One tile for each trading idea (family), with how its strategies stand. Pressing one narrows the table below. */
export function Families({ items, family, onFamily }: { items: Item[]; family: string; onFamily: (f: string) => void }) {
  const [shown, setShown] = useState(FAM_PAGE);
  const names = [...new Set(items.map((i) => i.family))].sort((a, b) => (a === HAND ? 1 : b === HAND ? -1 : a.localeCompare(b)));
  return (
    <Block className="sx-fams" title={<span title="A family is one trading idea. Press one to list its strategies">Strategy families</span>}
      state={names.length > 0 ? <State tone="run" hover="A family is one trading idea">{plural(names.length, "family", "families")}</State> : undefined}>
      {names.length === 0 ? <p className="empty">No strategy to group yet</p> : (
        <div className="sx-wall" data-testid="families">
          {names.slice(0, shown).map((f, k) => {
            const mine = items.filter((i) => i.family === f);
            const c = { stopped: 0, moving: 0, paper: 0, new: 0 };
            for (const i of mine) if (i.kind) c[i.kind] += 1;
            const best = mine.reduce<Item | null>((b, i) => (i.met !== null && (b === null || i.met > (b.met as number)) ? i : b), null);
            const edits = mine.flatMap((i) => (i.changed === null ? [] : [i.changed]));
            const chip: [NlTone, string] = c.paper ? ["good", `${c.paper} on paper`] : c.moving ? ["good", `${c.moving} moving`]
              : c.stopped === mine.length ? ["bad", "All stopped"] : ["dim", "Not started"];
            const hue = c.paper || c.moving ? "good" : c.stopped === mine.length ? "bad" : "";
            return (
              <button key={f} type="button" data-family={f} data-hue={hue} aria-pressed={family === f}
                className="sx-tile" style={{ "--i": k } as CSSProperties}
                title={`${f}: ${mine.slice(0, 6).map((i) => `${nameOfItem(i)} ${i.r.where?.word ?? "-"}`).join(", ")}${mine.length > 6 ? `, and ${mine.length - 6} more` : ""}. Closest: the most targets its best attempt met in the first test`}
                onClick={() => onFamily(family === f ? "" : f)}>
                <span className="sx-toprow"><span className="sx-tname">{f}</span><State tone={chip[0]}>{chip[1]}</State></span>
                <span className="sx-tfig"><b className="num">{num(mine.length)}</b><span className="sx-unit">{mine.length === 1 ? "strategy" : "strategies"}</span></span>
                <Share parts={[["bad", c.stopped], ["good", c.moving + c.paper], ["dim", c.new]]} />
                <span className="sx-chg">
                  <span><i>Stopped</i><b className={`num${c.stopped ? " bad" : ""}`}>{num(c.stopped)}</b></span>
                  <span><i>Moving</i><b className="num">{num(c.moving)}</b></span>
                  <span><i>Closest</i><b className="num">{best ? `${best.met} of ${best.bars}` : "-"}</b></span>
                  <span><i>Last change</i><b className="num">{day(edits.length ? Math.max(...edits) : null, false)}</b></span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {names.length > shown && <button type="button" className="btn sx-more" onClick={() => setShown(names.length)}>{`Show all ${names.length} families`}</button>}
    </Block>
  );
}

type Col = "name" | "family" | "stage" | "state" | "closest" | "runs" | "changed";
const PAGE = 25;

const sortKey: Record<Col, (i: Item) => string | number> = {
  name: (i) => nameOfItem(i).toLowerCase(),
  family: (i) => i.family.toLowerCase(),
  stage: (i) => i.at ?? -1,
  state: (i) => i.kind ?? "",
  closest: (i) => i.met ?? -1,
  runs: (i) => i.runs ?? -1,
  changed: (i) => i.changed ?? -1,
};

/** Every strategy as a row: a state filter, a search, and the stage or family picked above narrow it. Press a row to open it. */
export function StrategyTable({ items, stage, family, onClear, onOpen }: {
  items: Item[]; stage: number; family: string; onClear: (what: "stage" | "family") => void; onOpen: (id: number) => void;
}) {
  const [state, setState] = useState("all");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<{ col: Col; dir: 1 | -1 } | null>(null);
  const [limit, setLimit] = useState(PAGE);
  const count = (k: Kind) => items.filter((i) => i.kind === k).length;
  const needle = q.trim().toLowerCase();
  const shown = items
    .filter((i) => (!needle || nameOfItem(i).toLowerCase().includes(needle) || i.family.toLowerCase().includes(needle))
      && (state === "all" || i.kind === state) && (family === "" || i.family === family) && (stage < 0 || i.at === stage))
    .sort((a, b) => {
      if (sort) {
        const x = sortKey[sort.col](a), y = sortKey[sort.col](b);
        return (x < y ? -1 : x > y ? 1 : 0) * sort.dir || nameOfItem(a).localeCompare(nameOfItem(b));
      }
      return (b.at ?? -1) - (a.at ?? -1) || (b.met ?? -1) - (a.met ?? -1) || nameOfItem(a).localeCompare(nameOfItem(b));
    });
  const head = (col: Col, label: string, cls?: string, why?: string): ReactNode => (
    <th className={cls} aria-sort={sort?.col === col ? (sort.dir === 1 ? "ascending" : "descending") : undefined}>
      <button type="button" className="sx-sort" title={why} onClick={() => setSort({ col, dir: sort?.col === col ? (sort.dir === 1 ? -1 : 1) : col === "name" || col === "family" ? 1 : -1 })}>{label}</button>
    </th>
  );
  return (
    <Block className="sx-all" id="all-strategies" title="All strategies"
      state={<>
        {family && <button type="button" className="sx-chip" onClick={() => onClear("family")}>{family}<b aria-hidden="true">×</b></button>}
        {stage >= 0 && <button type="button" className="sx-chip" onClick={() => onClear("stage")}>{`Stage: ${STAGES[stage][0]}`}<b aria-hidden="true">×</b></button>}
      </>}
      right={<Segmented variant="seg" value={state} onChange={setState} options={[
        { label: `All ${items.length}`, value: "all" }, { label: `Stopped ${count("stopped")}`, value: "stopped" },
        { label: `Moving ${count("moving")}`, value: "moving" }, { label: `On paper ${count("paper")}`, value: "paper" },
      ]} />}>
      <input type="search" className="sx-find" aria-label="Find a strategy by name" placeholder="Find a strategy by name" value={q}
        onChange={(e) => { setQ(e.target.value); setLimit(PAGE); }} />
      {shown.length === 0 ? <p className="empty">No strategy matches. Clear the search or the filters.</p> : (
        <table className="sx-table" data-testid="road">
          <thead>
            <tr>
              {head("name", "Strategy")}{head("family", "Family", "sx-w")}{head("stage", "Stage", undefined, "The stage it stands at now")}
              {head("state", "State")}{head("closest", "Closest in its first test", "sx-w", "How many of the targets its best attempt met. It needs all of them to move on")}
              {head("runs", "Runs", "sx-w num")}{head("changed", "Last change", "sx-w num")}
            </tr>
          </thead>
          <tbody>
            {shown.slice(0, limit).map((i) => {
              const w = i.r.where;
              return (
                <tr key={i.r.s.id} data-strategy={i.r.s.id} tabIndex={0} onClick={() => onOpen(i.r.s.id)}
                  onKeyDown={(e) => { if (e.key === "Enter") onOpen(i.r.s.id); }}>
                  <td title={i.r.name.title}>{nameOfItem(i)}</td>
                  <td className="sx-w">{i.family}</td>
                  <td>{i.at === null ? "-" : STAGES[i.at][0]}</td>
                  <td className="st">{w ? <State tone={w.tone} hover={w.why}>{i.kind === "stopped" && w.word.startsWith("Stopped") ? "Stopped" : w.word}</State> : <span title={i.r.stateWhy ?? "Still reading"}>-</span>}</td>
                  <td className="sx-w">{i.met === null ? <span title="No best attempt is stored for this strategy">-</span>
                    : <span className="sx-met"><Share kind="parts" parts={[["good", i.met], ["bad", i.bars - i.met]]} /><span className="num">{`${i.met} of ${i.bars} targets`}</span></span>}</td>
                  <td className="sx-w num">{i.runs === null ? <span title="Runs are counted from a generator search">-</span> : num(i.runs)}</td>
                  <td className="sx-w num">{day(i.changed, true)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {shown.length > limit && <button type="button" className="btn sx-more" onClick={() => setLimit(limit + PAGE)}>{`Show ${Math.min(PAGE, shown.length - limit)} more`}</button>}
      {shown.length > 0 && <p className="sx-note sx-press">Press a row for the detail</p>}
    </Block>
  );
}
