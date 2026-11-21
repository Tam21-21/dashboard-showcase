import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Segmented, StateWord, Window } from "../../../kit";
import { fetchStrategySource } from "../../../data/strategiesApi";
import type { StrategyOverviewRow } from "../../../data/strategiesApi";
import { useStrategyNames } from "../../../data/strategyNames";
import { ApiError } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { EditorTarget } from "./StrategyEditor";
import { blankTemplate } from "./template";
import "./editor.css";

// Mockup: docs/design/rail_mockup_2026-10-10.html, newStrategy2 (the New strategy window). The name
// check reads every strategy's stored name, a deleted one included, because the server refuses a
// name a deleted strategy still holds.

type Section = { id: string };
type AppLike = { id: string; sections: readonly Section[] };

// True once the Generator page reads ?name= and fills its name box. Today it does not, so BUILD WITH
// THE GENERATOR stays held rather than drop the typed name without a word.
const GENERATOR_READS_NAME = false;

// The button works when the research app has a Generator section and that page takes the name.
export function generatorReady(apps: readonly AppLike[]): boolean {
  return GENERATOR_READS_NAME && apps.some((a) => a.id === "research" && a.sections.some((s) => s.id === "generator"));
}

type Mode = "blank" | "copy";

export default function NewStrategy({
  rows, app, onClose, onOpen,
}: {
  rows: readonly StrategyOverviewRow[];
  app: AppLike;
  onClose: () => void;
  onOpen: (target: EditorTarget) => void;
}) {
  const navigate = useNavigate();
  const namesQ = useStrategyNames();
  const [name, setName] = useState("");
  const [display, setDisplay] = useState("");
  const [about, setAbout] = useState("");
  const [mode, setMode] = useState<Mode>("blank");
  const [copyPick, setCopyPick] = useState("");
  const [lost, setLost] = useState<ReadonlySet<number>>(new Set());
  const [reading, setReading] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  const typed = name.trim();
  const shown = display.trim();
  const names = namesQ.data;
  const taken = typed !== "" && names !== undefined && names.some((r) => r.name === typed);
  const copyId = rows.length === 0 ? null : Number(copyPick || rows[0].id);
  const copyGone = copyId !== null
    && (lost.has(copyId) || (names ?? []).some((r) => r.id === copyId && r.status === "deleted"));
  const ready = generatorReady([app]);

  let why: string | null = null;
  if (typed === "") why = "Type a name";
  else if (taken) why = "This name is taken";
  else if (shown === "") why = "Type a display name";
  else if (mode === "copy" && copyId === null) why = "No strategy to copy yet";
  else if (mode === "copy" && copyGone) why = "This strategy was deleted; pick another";

  function pickCopy(value: string) {
    setCopyPick(value);
    setRefusal(null);
  }

  async function write() {
    if (why !== null) return;
    setRefusal(null);
    if (mode === "blank") {
      onOpen({ kind: "new", name: typed, display: shown, code: blankTemplate(about.trim() || shown) });
      return;
    }
    if (copyId === null) return;
    setReading(true);
    try {
      const src = await fetchStrategySource(copyId);
      onOpen({ kind: "new", name: typed, display: shown, code: src.source_code });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setLost(new Set([...lost, copyId]));
      setRefusal(serverDetail(e));
    }
    setReading(false);
  }

  function generate() {
    const label = shown || typed;
    navigate(`/app/research/generator${label === "" ? "" : `?name=${encodeURIComponent(label)}`}`);
    onClose();
  }

  let state = null;
  if (typed !== "") {
    if (namesQ.isError) {
      state = <span title={`Strategy names could not be read: ${serverDetail(namesQ.error)}`}>-</span>;
    } else if (names !== undefined) {
      state = taken ? <StateWord word="NAME TAKEN" tone="rd" /> : <StateWord word="NAME FREE" tone="g" />;
    }
  }

  return (
    <Window label="New strategy" size="m" className="ns" onClose={onClose}>
      <div className="h2"><span>New strategy</span></div>
      <div className="ns-row">
        <span>Name</span>
        <input aria-label="Name" className="fin num" placeholder="Stored name" value={name} onChange={(e) => { setName(e.target.value); setRefusal(null); }} />
      </div>
      <div className="ns-state">{state}</div>
      <div className="ns-row">
        <span>Display</span>
        <input aria-label="Display" className="fin" maxLength={60} placeholder="Breakout on volume" value={display} onChange={(e) => setDisplay(e.target.value)} />
      </div>
      <div className="ns-row">
        <span>About</span>
        <textarea
          aria-label="About"
          className="fin ns-about"
          placeholder="Buys a 20-day high when volume doubles"
          value={about}
          disabled={mode === "copy"}
          title={mode === "copy" ? "A copy keeps the description written in its code" : undefined}
          onChange={(e) => setAbout(e.target.value)}
        />
      </div>
      <div className="ns-row">
        <span>Start</span>
        <Segmented
          options={[{ label: "Blank template", value: "blank" }, { label: "A copy of", value: "copy" }]}
          value={mode}
          onChange={(v) => { setMode(v as Mode); setRefusal(null); }}
        />
      </div>
      {mode === "copy" && (
        <div className="ns-row">
          <span>Copy of</span>
          <select aria-label="Copy of" className="fin" disabled={rows.length === 0} value={copyId === null ? "" : String(copyId)} onChange={(e) => pickCopy(e.target.value)}>
            {rows.map((r) => <option key={r.id} value={String(r.id)}>{r.display_name ?? "Unnamed strategy"}</option>)}
          </select>
        </div>
      )}
      {refusal !== null && <p className="rd ns-note" role="status">{refusal}</p>}
      <div className="ns-cards">
        <button
          type="button"
          className="ns-card"
          disabled={!ready}
          title={ready ? undefined : "The Generator does not take the name yet"}
          onClick={generate}
        >
          BUILD WITH THE GENERATOR
        </button>
        <button
          type="button"
          className="ns-card"
          disabled={why !== null || reading}
          title={why ?? undefined}
          onClick={() => void write()}
        >
          WRITE IT MYSELF
        </button>
      </div>
      <div className="se-foot">
        <button type="button" className="btn q" onClick={onClose}>CANCEL</button>
      </div>
    </Window>
  );
}
