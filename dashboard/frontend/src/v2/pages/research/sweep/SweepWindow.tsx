import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DatePick, Segmented, StateWord, Window } from "../../../kit";
import { fetchStockLists, fetchSweepParams, saveListWindow, startSweep, startWalkForward, sweepKeys } from "../../../data/sweepApi";
import type { SweepGrid } from "../../../data/sweepApi";
import { useStrategyNames } from "../../../data/strategyNames";
import { ApiError } from "../../../../lib/api";
import { plainDetail, serverDetail } from "../../../../lib/errorText";
import { dateToEpoch, fmtDmy, plainStrategyName } from "../../../../lib/format";
import "./sweep.css";

// Mockup: docs/design/rail_mockup_2026-10-10.html, runSweep (list items S1 to S7). The window always sweeps
// its stock list's own window (S2); the time estimate is a dash because the server has none yet (S7).
// The Reserve stage is drawn but off: the reserve is used only by the reserve exam (#302), so no body
// built here can carry it.

type Stage = "screen" | "optimise" | "walkforward" | "reserve";
const STAGE_WORDS: Record<Stage, string> = { screen: "screen", optimise: "optimise", walkforward: "walk-forward", reserve: "reserve" };
const STAGE_OPTIONS = [
  { label: "Screen", value: "screen" },
  { label: "Optimise", value: "optimise" },
  { label: "Walk-forward", value: "walkforward" },
  { label: "Reserve", value: "reserve", disabled: true, hover: "The reserve is used only by the reserve exam, on the strategy page" },
];

const NO_WINDOW = "This stock list has no window yet";
const NO_PARAMS = "This strategy declares no parameters";
const isoOf = (epoch: number | null | undefined) => (epoch == null ? "" : new Date(epoch * 1000).toISOString().slice(0, 10));

type Parsed = { values: number[]; error: string | null };

// Values split on commas, trimmed, blanks and repeats dropped; every value must fit the declared type.
function parseValues(raw: string, type: string): Parsed {
  if (type !== "int" && type !== "float") return { values: [], error: `This type cannot be swept: ${type}` };
  const parts = raw.split(",").map((v) => v.trim()).filter(Boolean);
  const values: number[] = [];
  for (const v of parts) {
    if (type === "int") {
      if (!/^-?\d+$/.test(v)) return { values: [], error: `${v} is not a whole number` };
    } else if (!Number.isFinite(Number(v))) {
      return { values: [], error: `${v} is not a number` };
    }
    const n = Number(v);
    if (!values.includes(n)) values.push(n);
  }
  return { values, error: null };
}

const REFUSAL_WORDS: Array<[(d: string) => boolean, string]> = [
  [(d) => d.startsWith("sweep_grid_too_large:"), "GRID TOO LARGE"],
  [(d) => d.startsWith("frozen_sweep_parameter:"), "PARAMETER FROZEN"],
  [(d) => d.startsWith("undeclared_sweep_parameter:"), "NOT DECLARED"],
  [(d) => d.startsWith("price_universe_empty:"), "EMPTY LIST"],
  [(d) => d.startsWith("the sizing lead-in cannot be satisfied") || d.includes("quantile warmup needs"), "NOT ENOUGH HISTORY"],
];

// A refusal in the server's own words: its detail, else a string `error` (the stage runner's 409), else a plain sentence.
function refusalOf(e: unknown): { word: string; text: string } {
  if (!(e instanceof ApiError)) return { word: "REFUSED", text: serverDetail(e) };
  const body = e.body && typeof e.body === "object" ? (e.body as { detail?: unknown; error?: unknown }) : {};
  if (typeof body.detail === "string" && body.detail.length > 0) {
    const hit = REFUSAL_WORDS.find(([test]) => test(body.detail as string));
    return { word: hit ? hit[1] : "REFUSED", text: plainDetail(body.detail) };
  }
  if (typeof body.error === "string" && body.error.length > 0) return { word: "REFUSED", text: plainDetail(body.error) };
  if (e.status >= 500) return { word: "REFUSED", text: "The server failed on this request and sent no reason." };
  return { word: "REFUSED", text: "The server refused this request." };
}

export default function SweepWindow({
  strategyId, universeId, onClose,
}: { strategyId?: number; universeId?: number; onClose: () => void }) {
  const qc = useQueryClient();
  const names = useStrategyNames();
  const listQ = useQuery({ queryKey: sweepKeys.lists, queryFn: fetchStockLists });

  const [sid, setSid] = useState<number | null>(strategyId ?? null);
  const [uid, setUid] = useState<number | null>(universeId ?? null);
  const [raw, setRaw] = useState<Record<string, string>>({});
  const [stage, setStage] = useState<Stage>("screen");
  const [typed, setTyped] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [winNote, setWinNote] = useState<string | null>(null);
  const [winBusy, setWinBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const pressing = useRef(false);
  const [refusal, setRefusal] = useState<{ word: string; text: string } | null>(null);
  const [started, setStarted] = useState<string | null>(null);

  const strategies = (names.data ?? []).filter((r) => r.status === "active");
  const picked = strategies.find((r) => r.id === sid) ?? null;
  const strategyName = picked ? (picked.display_name ?? plainStrategyName(picked.name)) : "";

  const lists = (listQ.data ?? [])
    .filter((d) => d.dataset_type === "price_universe" && d.status !== "deleted")
    .sort((a, b) => b.created_at - a.created_at);
  // A preset that is not among the lists picks nothing, never another list.
  const list = lists.find((d) => d.id === (uid ?? (universeId === undefined ? lists[0]?.id : undefined))) ?? null;
  const members = list?.member_count ?? 0;
  const hasWindow = list != null && list.window_start_ts != null && list.window_end_ts != null;

  const paramsQ = useQuery({
    queryKey: sweepKeys.params(picked?.id ?? 0),
    queryFn: () => fetchSweepParams(picked!.id),
    enabled: picked !== null,
  });
  const declared = paramsQ.data?.sweep_params ?? [];
  const rows = declared.map((p) => ({ p, ...parseValues(raw[p.name] ?? "", p.type) }));
  const rowsOk = rows.every((r) => r.error === null && r.values.length > 0);
  const combos = rows.reduce((n, r) => n * r.values.length, 1);

  const defaultName = `${strategyName}, ${STAGE_WORDS[stage]}`;
  const name = typed ?? defaultName;
  const walk = stage === "walkforward";

  let blocked: string | null = null;
  if (picked === null) blocked = "Pick a strategy first";
  else if (list === null) blocked = "Pick a stock list first";
  else if (!hasWindow) blocked = NO_WINDOW;
  else if (members < 1) blocked = "This stock list has no stocks";
  else if (paramsQ.isError) blocked = "The strategy's parameters could not be read";
  else if (!paramsQ.data) blocked = "Reading the strategy's parameters";
  else if (!rowsOk) {
    const bad = rows.find((r) => r.error !== null || r.values.length === 0)!;
    blocked = `Type valid values for ${plainStrategyName(bad.p.name)}`;
  } else if (!walk && name.trim() === "") blocked = "Type a name";
  else if (busy) blocked = "Starting";

  function openEdit() {
    setFrom(isoOf(list?.window_start_ts));
    setTo(isoOf(list?.window_end_ts));
    setWinNote(null);
    setEditing(true);
  }

  async function saveWindow() {
    if (!list) return;
    const start = dateToEpoch(from);
    const end = dateToEpoch(to);
    if (start == null || end == null) {
      setWinNote("Choose both dates.");
      return;
    }
    setWinBusy(true);
    setWinNote(null);
    try {
      await saveListWindow(list.id, start, end);
      await Promise.all([qc.invalidateQueries({ queryKey: ["sweep"] }), qc.invalidateQueries({ queryKey: ["universes"] })]);
      setEditing(false);
    } catch (e) {
      setWinNote(serverDetail(e));
    } finally {
      setWinBusy(false);
    }
  }

  async function go() {
    if (blocked !== null || pressing.current || !picked || !list) return;
    pressing.current = true;
    setBusy(true);
    setRefusal(null);
    const grid: SweepGrid = {};
    for (const r of rows) grid[r.p.name] = r.values;
    try {
      if (walk) {
        await startWalkForward({ strategy_id: picked.id, universe_id: list.id, stage: "walkforward", calendar_id: "real", param_grid: grid });
        setStarted("Walk-forward started");
      } else {
        const trimmed = name.trim();
        await startSweep(list.id, { strategy_id: picked.id, param_grid: grid, name: trimmed, stage: stage as "screen" | "optimise" });
        setStarted(`Sweep started: ${trimmed}`);
      }
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["universes"] }),
        qc.invalidateQueries({ queryKey: ["strategies"] }),
        qc.invalidateQueries({ queryKey: ["runs"] }),
      ]);
    } catch (e) {
      setRefusal(refusalOf(e));
    } finally {
      pressing.current = false;
      setBusy(false);
    }
  }

  const windowText = hasWindow ? `${fmtDmy(list!.window_start_ts)} to ${fmtDmy(list!.window_end_ts)}` : null;
  const total = `${combos.toLocaleString("en-US")} ${combos === 1 ? "combination" : "combinations"} × ${members.toLocaleString("en-US")} stocks = ${(combos * members).toLocaleString("en-US")} backtests`;

  const showTotal = list !== null && paramsQ.data !== undefined && rowsOk;

  return (
    <Window label="Run a sweep" size="m" className="sw" onClose={onClose}>
      <div className="h2"><span>Run a sweep</span></div>

      <div className="sw-row">
        <span className="sw-l">Strategy</span>
        <select
          className="fin"
          aria-label="Strategy"
          value={picked ? String(picked.id) : ""}
          disabled={started !== null}
          onChange={(e) => { setSid(e.target.value === "" ? null : Number(e.target.value)); setRaw({}); }}
        >
          {strategies.length === 0 ? (
            <option value="">{names.isError ? "Strategies could not be read" : "No strategy yet"}</option>
          ) : (
            <>
              {picked === null && <option value="">Pick a strategy</option>}
              {strategies.map((r) => <option key={r.id} value={r.id}>{r.display_name ?? "No display name"}</option>)}
            </>
          )}
        </select>
      </div>

      <div className="sw-row">
        <span className="sw-l">Stocks</span>
        <select
          className="fin"
          aria-label="Stocks"
          value={list ? String(list.id) : ""}
          disabled={started !== null}
          onChange={(e) => setUid(e.target.value === "" ? null : Number(e.target.value))}
        >
          {lists.length === 0 ? (
            <option value="">{listQ.isError ? "Stock lists could not be read" : "No stock list yet"}</option>
          ) : (
            <>
              {list === null && <option value="">Pick a stock list</option>}
              {lists.map((d) => <option key={d.id} value={d.id}>{`${d.display_name ?? "No display name"}, ${d.member_count ?? 0} stocks`}</option>)}
            </>
          )}
        </select>
      </div>

      <div className="sw-row">
        <span className="sw-l">Window</span>
        {editing ? (
          <div className="sw-col">
            <div className="sw-line">
              <DatePick value={from} onChange={setFrom} ariaLabel="From" />
              <span className="sw-to">to</span>
              <DatePick value={to} onChange={setTo} ariaLabel="To" />
              <span className="sw-grow" />
              <button type="button" className="btn q sw-small" onClick={() => setEditing(false)}>BACK</button>
              <button type="button" className="btn sw-small" disabled={winBusy} onClick={() => void saveWindow()}>SAVE</button>
            </div>
            {winNote && <div className="sw-err rd" role="alert">{winNote}</div>}
          </div>
        ) : (
          <div className="sw-line">
            {windowText ? (
              <span className="num sw-win">{windowText}</span>
            ) : (
              <span className="num sw-win" title={list ? NO_WINDOW : "No stock list yet"}>-</span>
            )}
            <span className="sw-grow" />
            <button type="button" className="btn q sw-small" disabled={list === null || started !== null} onClick={openEdit}>EDIT WINDOW</button>
          </div>
        )}
      </div>

      <div className="sw-row sw-top">
        <span className="sw-l">Values</span>
        <div className="sw-col">
          {picked === null ? (
            <span className="num sw-win" title="Pick a strategy first">-</span>
          ) : paramsQ.isError ? (
            <span className="sw-err rd" role="alert"><span className="num" title="The strategy's parameters could not be read">-</span> {serverDetail(paramsQ.error)}</span>
          ) : !paramsQ.data ? (
            <span className="empty">Reading the parameters.</span>
          ) : declared.length === 0 ? (
            <div className="sw-vrow">
              <span className="sw-pl num" title={NO_PARAMS}>-</span>
              <span className="sw-grow" />
            </div>
          ) : (
            rows.map((r) => (
              <div className="sw-v" key={r.p.name}>
                <div className="sw-vrow">
                  <span className="sw-pl" title={r.p.name}>{plainStrategyName(r.p.name)}</span>
                  <input
                    className="fin num"
                    aria-label={plainStrategyName(r.p.name)}
                    value={raw[r.p.name] ?? ""}
                    disabled={started !== null}
                    onChange={(e) => setRaw({ ...raw, [r.p.name]: e.target.value })}
                  />
                  <span className="sw-n num" title={r.error ?? undefined}>{r.error ? "-" : r.values.length}</span>
                </div>
                {r.error && <div className="sw-err rd">{r.error}</div>}
              </div>
            ))
          )}
          <div className="sw-total num">
            {showTotal ? <span>{total}</span> : <span title="Totals appear once every value is valid">-</span>}
            <span> · time </span>
            <span title="The server does not estimate run time yet">-</span>
          </div>
        </div>
      </div>

      <div className="sw-row">
        <span className="sw-l">Stage</span>
        <div className="sw-stage">
          <Segmented variant="seg" options={STAGE_OPTIONS} value={stage} onChange={(v) => { if (started === null) setStage(v as Stage); }} />
        </div>
      </div>

      <div className="sw-row">
        <span className="sw-l">Name</span>
        <input
          className="fin"
          aria-label="Name"
          value={name}
          disabled={walk || started !== null}
          title={walk ? "The server names a walk-forward run" : undefined}
          onChange={(e) => setTyped(e.target.value)}
        />
      </div>

      {refusal && (
        <div className="sw-ref" role="alert">
          <StateWord word={refusal.word} tone="rd" />
          <span>{refusal.text}</span>
        </div>
      )}
      {started && (
        <div className="sw-ok" role="status">
          <StateWord word="STARTED" tone="bl" />
          <span>{started}</span>
        </div>
      )}

      <div className="sw-foot">
        <button type="button" className="btn q" onClick={onClose}>{started ? "CLOSE" : "CANCEL"}</button>
        {!started && (
          <button type="button" className="btn" disabled={blocked !== null} title={blocked ?? undefined} onClick={() => void go()}>START</button>
        )}
      </div>
    </Window>
  );
}
