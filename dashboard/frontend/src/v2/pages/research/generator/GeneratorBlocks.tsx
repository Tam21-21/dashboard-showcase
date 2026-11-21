import { useState } from "react";
import type { CSSProperties } from "react";
import { Block, State } from "../../../kit";
import { PhoneOff } from "../../../kit/phone";
import type { NlTone } from "../../../kit";
import type { GenBatch, GenTree, TrialsDay } from "../../../data/generatorApi";
import { dateToEpoch } from "../../../../lib/format";
import { Share } from "../strategies/StrategyList";
import { nearestOf, NUMS } from "./Closeness";
import type { Nearest } from "./Closeness";
import "../strategies.css";

// The Generator page's blocks, after the look trial's lab-p-generator.js (2026-11-14): what stops it, the searches as tiles,
// every theory as a row of written figures, and the years the searches use. The settings-per-day chart is in Closeness.tsx.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function dayOf(epoch: number | null, year = false): string {
  if (epoch === null) return "-";
  const d = new Date(epoch * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}${year ? ` ${d.getUTCFullYear()}` : ""}`;
}
export const dayIso = (iso: string) => dayOf(dateToEpoch(iso), false);
const num = (n: number) => n.toLocaleString("en-GB");

export type Stopper = { tone: NlTone; word: string; title: string; detail: string; share: Array<[NlTone, number]>; caption: string; button: string; act: () => void };

/** What stops the generator: each reason with a bar that says how much of the whole it covers, and the button that deals with it. */
export function NeedsYou({ stoppers, loading }: { stoppers: Stopper[]; loading: boolean }) {
  return (
    <Block className="sx-first" title="Needs you">
      {loading ? <p className="empty">Reading the searches.</p> : stoppers.length === 0 ? <p className="empty">Nothing stops the generator.</p> : (
        <div className="gx-stops" data-testid="needs-you">
          {stoppers.map((s) => (
            <div key={s.title} className="gx-stop">
              <State tone={s.tone}>{s.word}</State>
              <div className="gx-why"><b>{s.title}</b><span>{s.detail}</span></div>
              <div className="gx-bar"><Share parts={s.share} /><span>{s.caption}</span></div>
              <button type="button" className="btn" onClick={s.act}>{s.button}</button>
            </div>
          ))}
        </div>
      )}
    </Block>
  );
}

const S_PAGE = 8;

/** One tile per search: a family of theories run over one set of stocks. Pressing one opens its controls. */
export function SearchTiles({ trees, onOpen, onAddTemplate }: { trees: GenTree[]; onOpen: (id: number) => void; onAddTemplate: () => void }) {
  const [shown, setShown] = useState(S_PAGE);
  return (
    <Block className="sx-fams" title="Searches"
      state={trees.length > 0 ? <State tone="run">{`${trees.length} ${trees.length === 1 ? "search" : "searches"}`}</State> : undefined}
      right={<PhoneOff><button type="button" className="btn" onClick={onAddTemplate}>Add a template</button></PhoneOff>}>
      {trees.length === 0 ? <p className="empty">No search yet. Start a theory and the generator tries its settings on the early years.</p> : (
        <div className="sx-wall" data-testid="search-tiles">
          {trees.slice(0, shown).map((t, k) => {
            const b = t.batch;
            const kept = t.theories.reduce((a, th) => a + th.saved.length, 0);
            const hit = t.theories.filter((th) => th.saved.length > 0).length;
            const tried = t.theories.reduce((a, th) => a + (th.search?.rounds?.rounds ?? []).reduce((x, r) => x + r.new_trials, 0), 0);
            const n = t.theories.length;
            return (
              <button key={b.id} type="button" data-batch={b.id} data-hue={hit ? "good" : "bad"} className="sx-tile" style={{ "--i": k } as CSSProperties}
                title={`${t.template.display_name}: ${t.theories.map((th) => `${th.display_name} ${th.saved.length ? `kept ${th.saved.length}` : "nothing kept"}`).slice(0, 6).join(", ")}. Kept back: stocks the search never sees, saved for the final exam`}
                onClick={() => onOpen(b.id)}>
                <span className="sx-toprow"><span className="sx-tname">{t.template.display_name}</span><State tone={hit ? "good" : "bad"}>{hit ? `Kept ${kept}` : "Nothing kept"}</State></span>
                <span className="sx-tfig"><b className="num">{num(n)}</b><span className="sx-unit">{n === 1 ? "theory" : "theories"}</span></span>
                <Share parts={[["bad", n - hit], ["good", hit]]} />
                <span className="sx-chg">
                  <span><i>Settings tried</i><b className="num">{num(tried)}</b></span>
                  <span><i>Stocks</i><b className="num">{num(b.members.length - b.kept_back.length)}</b></span>
                  <span><i>Kept back</i><b className="num">{num(b.kept_back.length)}</b></span>
                  <span><i>Started</i><b className="num">{dayOf(b.approved_at, false)}</b></span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {trees.length > shown && <button type="button" className="btn sx-more" onClick={() => setShown(trees.length)}>{`Show all ${trees.length} searches`}</button>}
    </Block>
  );
}

const short = (name: string, family: string) => (name.startsWith(`${family}, `) ? name.slice(family.length + 2) : name);

type Row = { id: number; name: string; n: Nearest | null | "odd"; why: string };

/** Every theory as a row of written figures. Green past its bar, red short of it, grey shown and no bar. Most bars met first. */
export function TheoryResults({ trees }: { trees: GenTree[] }) {
  const rows: Row[] = trees.flatMap((t) => t.theories.map((th) => {
    const n = nearestOf(th);
    const screen = th.runs.find((r) => r.stage === "screen");
    const why = n === "odd" ? "The server sent the search rounds in a shape this page does not know"
      : n == null ? (screen == null ? "No screen run yet" : screen.status === "failed" || screen.status === "cancelled" ? `The screen run ${screen.status}, so the search has no rounds` : "No search rounds yet: the search has not finished")
        : "No strategy it tried had 30 trades or more and a Sharpe, so none could be held to the bars";
    return { id: th.id, name: th.display_name || short(th.name, t.template.display_name), n, why };
  }));
  const read = (r: Row): Nearest | null => (r.n !== null && r.n !== "odd" && !Number.isNaN(r.n.sharpe) ? r.n : null);
  rows.sort((a, b) => (read(b)?.met ?? -1) - (read(a)?.met ?? -1) || (read(b)?.sharpe ?? -1) - (read(a)?.sharpe ?? -1) || a.name.localeCompare(b.name));
  const [sort, setSort] = useState<{ col: string; dir: 1 | -1 } | null>(null);
  const shown = sort === null ? rows : [...rows].sort((a, b) => {
    const val = (r: Row) => (sort.col === "name" ? r.name.toLowerCase() : sort.col === "met" ? (read(r)?.met ?? -1) : (read(r)?.m[sort.col as keyof Nearest["m"]] ?? -1));
    const x = val(a), y = val(b);
    return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
  });
  const head = (col: string, label: string, cls?: string, why?: string) => (
    <th className={cls}><button type="button" className="sx-sort" title={why} onClick={() => setSort({ col, dir: sort?.col === col ? (sort.dir === 1 ? -1 : 1) : col === "name" ? 1 : -1 })}>{label}</button></th>
  );
  const met = rows.filter((r) => read(r) !== null && (read(r) as Nearest).answer === "yes").length;
  return (
    <Block id="close" className="sx-fams"
      title={<span title={`${met} of ${rows.length} theories meet every target. The server sends the figures of each theory's best attempt and which targets it missed; the target values themselves are not sent`}>Theory results</span>}
      state={rows.length > 0 ? <State tone="run">{`${rows.length} ${rows.length === 1 ? "theory" : "theories"}`}</State> : undefined}>
      {rows.length === 0 ? <p className="empty">No theory has a result yet.</p> : (
        <table className="sx-table gx-table" data-testid="closeness">
          <thead>
            <tr>
              {head("name", "Theory")}
              {NUMS.map(([k, label]) => head(k, label, "num"))}
              {head("met", "Targets met")}
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const n = read(r);
              if (n === null) {
                return (
                  <tr key={r.id} data-theory={r.id}>
                    <td>{r.name}</td>
                    <td colSpan={NUMS.length + 1} className="gx-none" title={r.why}>-</td>
                  </tr>
                );
              }
              const bars = n.met + n.failed.length;
              return (
                <tr key={r.id} data-theory={r.id}>
                  <td title={`Nearest of the best ${n.pool}, Sharpe ${n.sharpe.toFixed(2)}`}>{r.name}</td>
                  {NUMS.map(([k, label, fmt]) => {
                    const v = n.m[k];
                    const barred = bars >= 5 || k !== "recovery";
                    const state = !barred ? "soft" : n.failed.includes(k) ? "bad" : "good";
                    return (
                      <td key={k} data-k={k} className={`num gx-${state}`}
                        title={v == null ? `${label}: none` : `${label}: ${fmt(v)}. ${state === "soft" ? "Shown, not a target" : state === "bad" ? "Short of its target" : "Past its target"}`}>
                        {v == null ? "-" : fmt(v)}
                      </td>
                    );
                  })}
                  <td title={`${n.met} of ${bars} targets met. ${n.answer === "yes" ? "The theory is suitable" : "Not suitable: the search stopped"}`}>
                    <span className="sx-met"><Share kind="parts" parts={[["good", n.met], ["bad", bars - n.met]]} /><span className="num">{`${n.met} of ${bars}`}</span></span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {rows.length > 0 && <p className="sx-note gx-daynote">Most targets met first.</p>}
    </Block>
  );
}

/** The settings tried on each of the last days, one bar a day, with the line under it that says what the bars add up to. */
export function DaysNote({ days }: { days: TrialsDay[] }) {
  const busy = days.filter((d) => d.trials > 0);
  const total = busy.reduce((a, d) => a + d.trials, 0);
  const last = busy[busy.length - 1];
  return (
    <p className="sx-note gx-daynote">
      {busy.length > 0
        ? `${num(total)} settings tried on ${busy.length} day${busy.length === 1 ? "" : "s"} of the last ${days.length}. The last search day was ${dayIso(last.day)}.`
        : `No setting was tried in the last ${days.length} days.`}
    </p>
  );
}

const yearOf = (iso: string) => iso.slice(0, 4);

/** The years every search uses, as one axis: the lead-in, the three stretches a search works on, and the years kept back. */
export function YearsUsed({ batches }: { batches: GenBatch[] }) {
  const b = batches[0];
  const key = (x: GenBatch) => JSON.stringify([x.data_start, x.screen_start, x.screen_end, x.optimise_start, x.optimise_end, x.walkforward, x.later_start, x.later_end]);
  const same = batches.every((x) => key(x) === key(b));
  const rows: Array<[string, string, string, "soft" | "data" | "hold", string]> = [
    ["Lead-in", b.data_start, b.screen_start, "soft", "Prices only, so the first signals have history behind them"],
    ["Screen", b.screen_start, b.screen_end, "data", "The search tries its settings here"],
    ["Optimise", b.optimise_start, b.optimise_end, "data", "The kept strategies are tuned here"],
    ["Walk-forward", b.walkforward[0], b.walkforward[1], "data", "The tuned strategies are tested on years they have not seen"],
    ["Kept back", b.later_start, b.later_end, "hold", `Never used by a search. These years and ${b.kept_back.length} of ${b.members.length} stocks wait for the final exam`],
  ];
  const t = (iso: string) => Date.parse(`${iso}T00:00:00Z`);
  const from = t(rows[0][1]);
  const span = t(rows[rows.length - 1][2]) - from;
  return (
    <Block title="Years used by the searches">
      <div className="gx-years" data-testid="years-used">
        {rows.map(([name, a, z, tone, why]) => (
          <div key={name} className="gx-yr" style={{ flexGrow: Math.max(1, (t(z) - t(a)) / span * 100) } as CSSProperties} title={`${name}: ${a} to ${z}. ${why}`}>
            <span className="gx-yn">{name}</span>
            <i className={tone} />
            <span className="gx-yd num">{`${yearOf(a)} to ${yearOf(z)}`}</span>
          </div>
        ))}
      </div>
      <p className="sx-note gx-daynote">{`${same ? "Every search uses the same years." : "The first search is shown; the others differ."} The filled years are used by the searches, the outlined years are kept back.`}</p>
    </Block>
  );
}
