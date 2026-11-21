import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { DatePick, Window } from "../../../kit";
import { chainsKeys, chooseCell, fetchStrategyOptions, fetchSweepParams, retryChain, startChain, stopChain } from "../../../data/chainsApi";
import { fmtDmy } from "../../../../lib/format";
import { serverDetail } from "../../../../lib/errorText";
import type { Chain, ChainWriteResult, SweepCell } from "../../../../types/research";

// The four writes of the Chain page, each in a kit Window. One press is one call: a ref guard shuts the
// verb from the press to the answer, a success closes the window and says so on the page, and a refusal
// is shown in the open window in the server's own words.

type Done = (text: string, result: ChainWriteResult) => void;

function useWrite(onDone: Done) {
  const sent = useRef(false);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const run = (call: () => Promise<ChainWriteResult>, text: string) => {
    if (sent.current) return;
    sent.current = true;
    setPending(true);
    setProblem(null);
    call().then(
      (result) => onDone(text, result),
      (e: unknown) => {
        sent.current = false;
        setPending(false);
        setProblem(serverDetail(e));
      },
    );
  };
  return { pending, problem, setProblem, run };
}

const nameOf = (c: Chain) => c.display_name ?? "Unnamed strategy";

function Foot({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  return (
    <div className="ch-wfoot">
      <button type="button" className="btn q" onClick={onClose}>CANCEL</button>
      {children}
    </div>
  );
}

function Problem({ text }: { text: string | null }) {
  return text ? <p role="status" className="ch-wproblem">{text}</p> : null;
}

export function StopWindow({ chain, onClose, onDone }: { chain: Chain; onClose: () => void; onDone: Done }) {
  const w = useWrite(onDone);
  return (
    <Window label="Stop the chain" size="s" onClose={onClose}>
      <div className="h2"><span className="ch-h2">STOP THE CHAIN</span></div>
      <div className="ch-wfact"><span>{nameOf(chain)}</span><b>{chain.state_word}</b></div>
      <Problem text={w.problem} />
      <Foot onClose={onClose}>
        <button type="button" className="btn danger" disabled={w.pending} onClick={() => w.run(() => stopChain(chain.id), "Stop requested.")}>STOP</button>
      </Foot>
    </Window>
  );
}

export function RetryWindow({ chain, onClose, onDone }: { chain: Chain; onClose: () => void; onDone: Done }) {
  const w = useWrite(onDone);
  return (
    <Window label="Retry the chain" size="s" onClose={onClose}>
      <div className="h2"><span className="ch-h2">RETRY THE CHAIN</span></div>
      <div className="ch-wfact"><span>{nameOf(chain)}</span><b>{chain.state_word}</b></div>
      {chain.reason_sentence && <p className="ch-wtext">{chain.reason_sentence}</p>}
      <Problem text={w.problem} />
      <Foot onClose={onClose}>
        <button type="button" className="btn" disabled={w.pending} onClick={() => w.run(() => retryChain(chain.id), "Retry accepted.")}>RETRY</button>
      </Foot>
    </Window>
  );
}

export function ChooseWindow({ chain, cell, onClose, onDone }: { chain: Chain; cell: SweepCell; onClose: () => void; onDone: Done }) {
  const w = useWrite(onDone);
  return (
    <Window label="Choose this cell" size="s" onClose={onClose}>
      <div className="h2"><span className="ch-h2">CHOOSE THIS CELL</span></div>
      <div className="ch-wfact"><span>{nameOf(chain)}</span></div>
      <div className="ch-wfact"><span>Stock</span><b>{cell.ticker}</b></div>
      <div className="ch-wfact"><span>Variation</span><b className="num">{`v${cell.variation_id}`}</b></div>
      <div className="ch-wfact"><span>Sharpe</span><b className="num">{cell.sharpe_ratio == null ? "-" : cell.sharpe_ratio.toFixed(2)}</b></div>
      <Problem text={w.problem} />
      <Foot onClose={onClose}>
        <button type="button" className="btn" disabled={w.pending} onClick={() => w.run(() => chooseCell(chain.id, cell.variation_id, cell.ticker), "Cell chosen.")}>CHOOSE</button>
      </Foot>
    </Window>
  );
}

// ---- START -----------------------------------------------------------------------------------------

const DATE_FIELDS = [
  { key: "screen", label: "Screen from" },
  { key: "optimise", label: "Optimise from" },
  { key: "walkforward", label: "Walk forward from" },
  { key: "end", label: "Walk forward to" },
] as const;
type DateKey = (typeof DATE_FIELDS)[number]["key"];

const NO_STRATEGIES = "There are no strategies to start a chain from.";
const cap = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const paramLabel = (name: string) => cap(name.replace(/_/g, " "));
const epochOf = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / 1000;

// One comma-separated entry cast by the type the strategy declares; anything else is refused in words.
function castValues(text: string, type: string, label: string): number[] | string {
  const parts = text.split(",").map((p) => p.trim()).filter((p) => p !== "");
  if (parts.length === 0) return `${label} needs at least one value.`;
  if (type !== "int" && type !== "float") return `${label} is declared as ${type}, which this form cannot send.`;
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isFinite(n) || (type === "int" && !Number.isInteger(n))) {
      return `Every value of ${label} must be ${type === "int" ? "a whole number" : "a number"}; "${p}" is not.`;
    }
    out.push(n);
  }
  return out;
}

export function StartWindow({ reserveCut, onClose, onDone }: { reserveCut: number | null; onClose: () => void; onDone: Done }) {
  const w = useWrite(onDone);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [dates, setDates] = useState<Record<DateKey, string>>({ screen: "", optimise: "", walkforward: "", end: "" });
  const [values, setValues] = useState<Record<string, string>>({});

  const strategiesQ = useQuery({ queryKey: chainsKeys.strategies, queryFn: fetchStrategyOptions });
  const statusQ = useQuery({
    queryKey: chainsKeys.status(strategyId ?? 0),
    queryFn: () => fetchSweepParams(strategyId!),
    enabled: strategyId != null,
  });
  const params = statusQ.data?.sweep_params ?? [];
  const noStrategies = strategiesQ.data !== undefined && strategiesQ.data.length === 0;

  const submit = () => {
    if (strategyId == null) return w.setProblem("Pick a strategy first.");
    const missing = DATE_FIELDS.find((f) => dates[f.key] === "");
    if (missing) return w.setProblem(`Pick a day for "${missing.label}".`);
    if (statusQ.isPending || statusQ.isError) return w.setProblem("The strategy's parameters are not loaded yet.");
    const grid: Record<string, number[]> = {};
    for (const p of params) {
      const cast = castValues(values[p.name] ?? "", p.type, paramLabel(p.name));
      if (typeof cast === "string") return w.setProblem(cast);
      grid[p.name] = cast;
    }
    w.run(
      () => startChain({
        strategy_id: strategyId,
        screen_start_ts: epochOf(dates.screen),
        optimise_start_ts: epochOf(dates.optimise),
        walkforward_start_ts: epochOf(dates.walkforward),
        walkforward_end_ts: epochOf(dates.end),
        param_grid: grid,
      }),
      "Chain started.",
    );
  };

  return (
    <Window label="Start a chain" size="m" onClose={onClose}>
      <div className="h2"><span className="ch-h2" title="Walks one strategy through screen, optimise, your choice of cell, replay, walk forward and the judge; no date is filled in for you">START A CHAIN</span></div>
      <div className="ch-wform">
        <div className="ch-wwide">
          <span className="ch-lab">Strategy</span>
          {noStrategies ? (
            <p className="empty">{NO_STRATEGIES}</p>
          ) : (
            <select
              className="fin"
              aria-label="Strategy"
              value={strategyId ?? ""}
              onChange={(e) => {
                setStrategyId(e.target.value === "" ? null : Number(e.target.value));
                setValues({});
              }}
            >
              <option value="">Pick a strategy</option>
              {(strategiesQ.data ?? []).map((s) => (
                <option key={s.id} value={s.id}>{s.display_name ?? `Strategy ${s.id}`}</option>
              ))}
            </select>
          )}
        </div>
        {DATE_FIELDS.map((f) => (
          <div key={f.key}>
            <span className="ch-lab" title={f.key === "end" && reserveCut != null ? `Must end before the reserve cut, ${fmtDmy(reserveCut)}` : undefined}>{f.label}</span>
            <DatePick ariaLabel={f.label} value={dates[f.key]} onChange={(iso) => setDates((d) => ({ ...d, [f.key]: iso }))} />
          </div>
        ))}
      </div>
      {strategiesQ.isError && <p className="empty">{serverDetail(strategiesQ.error)}</p>}
      {strategyId != null && (
        statusQ.isError ? (
          <p className="empty">{serverDetail(statusQ.error)}</p>
        ) : statusQ.isPending ? (
          <p className="empty">Loading the parameters.</p>
        ) : params.length === 0 ? (
          <p className="empty">This strategy declares no parameters to try.</p>
        ) : (
          <div className="ch-wform">
            {params.map((p) => (
              <div key={p.name}>
                <span className="ch-lab">{paramLabel(p.name)}</span>
                <input
                  className="fin"
                  aria-label={paramLabel(p.name)}
                  placeholder="values, comma separated"
                  value={values[p.name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                />
              </div>
            ))}
          </div>
        )
      )}
      <Problem text={w.problem} />
      <Foot onClose={onClose}>
        <button type="button" className="btn" disabled={w.pending || noStrategies} title={noStrategies ? NO_STRATEGIES : undefined} onClick={submit}>START THE CHAIN</button>
      </Foot>
    </Window>
  );
}
