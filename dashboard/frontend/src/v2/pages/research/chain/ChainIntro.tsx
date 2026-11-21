import type { CSSProperties } from "react";
import { Block, State } from "../../../kit";
import type { ReserveWindow } from "../../../data/strategiesApi";
import type { Chain, ChainStep } from "../../../../types/research";
import { fmtDmy } from "../../../../lib/format";

// Round seven (Tamer 2026-11-15), a replica of the mockup's Chain page (lab-p-chain.js). Two questions before the chains
// themselves: what does a chain do, and which years may it use? The steps are the runner's own order
// (dashboard/backend/chain_runner.py); the cut and the exam stretches are what the server serves.

const STEPS: Array<{ step: ChainStep; name: string; what: string; you: boolean; keeps: string }> = [
  { step: "screen", name: "Screen", what: "A first run of the strategy on the early years", you: false, keeps: "No Sharpe of its own" },
  { step: "optimise", name: "Optimise", what: "Tries every setting on every stock of the list", you: false, keeps: "The Sharpe of the best cell" },
  { step: "choose", name: "Choose", what: "You pick one result to carry on with: a setting and a stock", you: true, keeps: "The Sharpe of the cell you pick" },
  { step: "replay", name: "Replay", what: "Runs your pick again in full, trade by trade", you: false, keeps: "No Sharpe sent" },
  { step: "walkforward", name: "Walk-forward", what: "Tests it on years it has not seen", you: false, keeps: "No Sharpe sent" },
  { step: "judge", name: "Judge", what: "Nine checks say pass or fail", you: false, keeps: "A verdict, not a Sharpe" },
];

/** The six steps on one line, each with who moves it on and how many chains stand at it now. */
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** 1 Jan 2023, the way the mockup writes a day. */
const dayYear = (sec: number) => { const d = new Date(sec * 1000); return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`; };

export function ChainSteps({ chains }: { chains: Chain[] }) {
  return (
    <Block title={<span title="Each step starts the next by itself, except the one that waits for you">Chain steps</span>}>
      <div className="cn-steps" data-testid="chain-steps">
        <i className="cn-line" />
        {STEPS.map((s, i) => {
          const here = chains.filter((c) => c.step === s.step && c.group !== "ended").length;
          const tone = s.you ? "you" : here > 0 ? "live" : "ahead";
          return (
            <div key={s.step} className={`cn-st ${tone}`} data-step={s.step} style={{ "--i": i } as CSSProperties}
              title={`${s.name}: ${s.what}. ${s.you ? "Waits for you" : "The chain moves on by itself"}. Keeps: ${s.keeps}`}>
              <span className="cn-no" />
              <b className="cn-nm">{`${i + 1}. ${s.name}`}</b>
              <span className="cn-big num" title={`Chains at this step now: ${here}`}>{here}</span>
              <span className="cn-what">{s.what}</span>
              <State tone={s.you ? "warn" : "dim"}>{s.you ? "Waits for you" : "Runs by itself"}</State>
            </div>
          );
        })}
      </div>
    </Block>
  );
}

const yearOf = (t: number) => new Date(t * 1000).getUTCFullYear();
const jan1 = (y: number) => Date.UTC(y, 0, 1) / 1000;

/**
 * The years a chain may use: every window ends by the reserve cut, and after the cut come the exam stretches, kept for the
 * end. `start` is the first day the generator's searches use; with none known the part before the cut is drawn without years.
 * `windows` is null while the reserve exam's stretches are loading.
 */
export function ChainYears({ cut, cutError, windows, start, error }: {
  cut: number | null; cutError: string | null; windows: ReserveWindow[] | null; start: number | null; error: string | null;
}) {
  let body;
  if (cutError != null) body = <p className="empty" title={cutError}>The reserve cut could not be read.</p>;
  else if (cut == null) body = <p className="empty">Loading the reserve cut.</p>;
  else {
    const ex = [...(windows ?? [])].sort((a, b) => a.start_ts - b.start_ts);
    const end = ex.length ? Math.max(...ex.map((w) => w.end_ts)) : cut;
    const BEFORE = 60; // the share of the axis before the cut when no first day is known
    const x = start != null && start < cut
      ? (t: number) => ((t - start) / (end - start || 1)) * 100
      : (t: number) => (t <= cut || end === cut ? BEFORE : BEFORE + ((t - cut) / (end - cut)) * (100 - BEFORE));
    const first = start != null && start < cut ? yearOf(start) + 1 : yearOf(cut);
    const years: number[] = [];
    for (let y = first; jan1(y) <= cut; y += 4) years.push(jan1(y));
    const used = ex.filter((w) => w.spent).length;
    const openFrom = start != null && start < cut ? yearOf(start) : null;
    const keptTo = ex.length ? yearOf(end - 1) : null;
    body = (
      <>
        <div className="cn-rows" data-testid="chain-allowed-years">
          <span className="cn-rl">Open to chains</span>
          <div className="cn-track">
            <i className="cn-open" style={{ left: 0, width: `${x(cut)}%` }} title={`Before the cut: every chain window must end by ${fmtDmy(cut)}`} />
          </div>
          <span className="cn-rt">{openFrom != null ? `${openFrom} to ${yearOf(cut - 1)}` : `to ${yearOf(cut - 1)}`}</span>
          <span className="cn-rl">Kept for the exam</span>
          <div className="cn-track">
            <i className="cn-kept" style={{ left: `${x(cut)}%`, width: `${100 - x(cut)}%` }} title={ex.length ? "Each stretch is used once, for one final exam" : "No exam stretch is set"}>
              {ex.map((w, k) => (
                <u key={w.index} className={`exam${w.spent ? " spent" : ""}`} data-exam={w.index}
                  style={{ left: `${((w.start_ts - cut) / (end - cut || 1)) * 100}%`, width: `${((w.end_ts - w.start_ts) / (end - cut || 1)) * 100}%`, "--k": k } as CSSProperties}
                  title={`Exam stretch ${k + 1}: ${fmtDmy(w.start_ts)} to ${fmtDmy(w.end_ts)}, ${w.spent ? "used" : "not used yet"}`} />
              ))}
            </i>
          </div>
          <span className="cn-rt">{keptTo != null ? `${yearOf(cut)} to ${keptTo}` : "None set"}</span>
          <span className="cn-sp" />
          <div className="cn-ya">{years.map((t) => <span key={t} style={{ left: `${x(t)}%` }}>{yearOf(t)}</span>)}</div>
          <span className="cn-sp" />
        </div>
        <div className="cn-facts">
          <div title="A chain that needs prices after it is refused"><span className="cn-fl">Cut-off date</span><b>{dayYear(cut)}</b><small>A chain that needs prices after it is refused</small></div>
          <div title={error ?? (ex.length ? `Each stretch is used once, for one final exam. ${ex.length - used} are left` : undefined)}>
            <span className="cn-fl">Exam stretches used</span>
            <b>{error ? "-" : windows == null ? "Loading" : ex.length ? `${used} of ${ex.length}` : "None set"}</b>
            {ex.length > 0 && !error && <small>{`Each stretch is used once, for one final exam. ${ex.length - used} are left`}</small>}
          </div>
        </div>
      </>
    );
  }
  return <Block title="Years used by a chain">{body}</Block>;
}
