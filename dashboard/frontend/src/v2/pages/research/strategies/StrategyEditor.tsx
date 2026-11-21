import { Suspense, useRef, useState } from "react";
import type { DragEvent } from "react";
import type { editor as MonacoEditor } from "monaco-editor";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CodeEditor, PageHeader, Panel, State, StateWord, Window } from "../../../kit";
import { createStrategy, fetchStrategySource, fetchStrategyVersions, saveStrategyCode, strategiesKeys, validateStrategyCode } from "../../../data/strategiesApi";
import { strategyNamesKey } from "../../../data/strategyNames";
import { serverDetail } from "../../../../lib/errorText";
import { day } from "./StrategyList";
import { markerLines } from "./template";
import { SettingsWindow, VersionsWindow } from "./EditorWindows";
import { CataloguePanel, MoneyPanel, ParametersPanel, QuickTestDash, RiskPanel } from "./EditorPanels";
import { placeSizingMethod } from "./sizingPlace";
import SweepWindow from "../sweep/SweepWindow";
import "./editor.css";

// Mockup: docs/design/rail_mockup_2026-10-10.html, editorPage and editorAfter. This lane builds the
// header, the code card with its VALIDATE strip, and the bar's VALIDATE, BACK and SAVE. The
// catalogue, the money and exit cards, QUICK TEST, VERSIONS and SETTINGS come with their own lanes.
// SAVE AND RUN A SWEEP (lane SW1) runs SAVE's own save, then opens the Run a sweep window.

export type EditorTarget =
  | { kind: "edit"; id: number }
  | { kind: "new"; name: string; display: string; code: string };

type Check =
  | { kind: "ok"; description: string }
  | { kind: "bad"; text: string; line: number | null };
type Saved = { ok: boolean; text: string };

// The server names a failing line as "(<strategy>, line 4)".
const LINE_IN_WORDS = /line (\d+)\)/;

export default function StrategyEditor({ target, onBack }: { target: EditorTarget; onBack: (savedId: number | null) => void }) {
  if (target.kind === "new") {
    return (
      <Loaded
        init={{ id: null, title: target.display, version: null, code: target.code, name: target.name, display: target.display }}
        onBack={onBack}
      />
    );
  }
  return <StoredEditor id={target.id} onBack={onBack} />;
}

function StoredEditor({ id, onBack }: { id: number; onBack: (savedId: number | null) => void }) {
  const q = useQuery({
    queryKey: ["strategies", "editor", id],
    queryFn: () => fetchStrategySource(id),
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  if (q.isError) {
    return (
      <div className="v2-page">
        <PageHeader crumb="Research · Strategies" title="Strategy code" />
        <Panel>
          <p className="empty">{serverDetail(q.error)}</p>
          <div className="se-bar">
            <button type="button" className="btn q" onClick={() => onBack(null)}>BACK</button>
          </div>
        </Panel>
      </div>
    );
  }
  if (!q.data) {
    return (
      <div className="v2-page">
        <PageHeader crumb="Research · Strategies" title="Strategy code" />
        <p className="empty">Loading the code.</p>
      </div>
    );
  }
  const row = q.data;
  return (
    <Loaded
      init={{ id: row.id, title: row.display_name ?? "Strategy code", version: row.version, code: row.source_code, name: null, display: null }}
      onBack={onBack}
    />
  );
}

// What the editor opens on. A stored strategy has its id and version; a new one has neither until
// its first SAVE, which creates it under `name` and `display`.
type Start = {
  id: number | null; title: string; version: number | null; code: string; name: string | null; display: string | null;
};

function Loaded({ init, onBack }: { init: Start; onBack: (savedId: number | null) => void }) {
  const qc = useQueryClient();
  const [opened] = useState(init.code);
  const [code, setCode] = useState(init.code);
  const [savedCode, setSavedCode] = useState(init.code);
  const [version, setVersion] = useState(init.version);
  const [id, setId] = useState(init.id);
  const [everSaved, setEverSaved] = useState(false);
  const [check, setCheck] = useState<Check | null>(null);
  const [saved, setSaved] = useState<Saved | null>(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [sweepFor, setSweepFor] = useState<number | null>(null);
  const [windowOpen, setWindowOpen] = useState<"versions" | "settings" | null>(null);
  const monacoRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const [mounted, setMounted] = useState(false);
  const [pos, setPos] = useState<{ line: number; col: number } | null>({ line: 1, col: 1 });
  const [hidden, setHidden] = useState<{ cat: boolean; side: boolean }>({ cat: false, side: false });
  const versions = useQuery({
    queryKey: strategiesKeys.versions(id ?? 0),
    queryFn: () => fetchStrategyVersions(id as number),
    enabled: id !== null,
  });

  const marks = markerLines(opened);
  const openAt = marks ? { from: marks.entry, to: marks.exit + 1 } : null;
  const title = init.title;
  const changed = code !== savedCode;
  const canSave = changed || id === null;

  function edit(next: string) {
    setCode(next);
    setCheck(null);
    setSaved(null);
  }

  async function validate() {
    setChecking(true);
    setSaved(null);
    try {
      const r = await validateStrategyCode(code);
      setCheck({ kind: "ok", description: r.description });
    } catch (e) {
      const text = serverDetail(e);
      const m = LINE_IN_WORDS.exec(text);
      setCheck({ kind: "bad", text, line: m ? Number(m[1]) : null });
    }
    setChecking(false);
  }

  // Saves the code; gives back the saved strategy's id, or null when the server refused (its words are shown).
  async function save(): Promise<number | null> {
    setSaving(true);
    setCheck(null);
    try {
      const r = id === null
        ? await createStrategy(init.name ?? "", init.display ?? "", code)
        : await saveStrategyCode(id, code);
      setId(r.id);
      setSavedCode(code);
      setVersion(r.version);
      setEverSaved(true);
      setSaved({ ok: true, text: r.action === "unchanged" ? `No change since version ${r.version}` : `Saved as version ${r.version}` });
      await Promise.all([
        qc.invalidateQueries({ queryKey: strategiesKeys.overview }),
        qc.invalidateQueries({ queryKey: strategyNamesKey }),
        qc.invalidateQueries({ queryKey: strategiesKeys.source(r.id) }),
        qc.invalidateQueries({ queryKey: strategiesKeys.versions(r.id) }),
      ]);
      setSaving(false);
      return r.id;
    } catch (e) {
      setSaved({ ok: false, text: serverDetail(e) });
      setSaving(false);
      return null;
    }
  }

  // Nothing changed and an id held: the window opens with no save. Otherwise SAVE's own save runs once first,
  // and a refused save opens nothing.
  async function saveAndSweep() {
    if (id !== null && !changed) {
      setSweepFor(id);
      return;
    }
    const savedId = await save();
    if (savedId !== null) setSweepFor(savedId);
  }

  // A catalogue or money click lands its text at the cursor, or over the selection.
  function insert(text: string) {
    const ed = monacoRef.current;
    const range = ed?.getSelection();
    if (!ed || !range) return;
    ed.executeEdits("catalogue", [{ range, text, forceMoveMarkers: true }]);
    ed.focus();
  }

  // A money row sets the one class-body sizing_method line, wherever the cursor is; one undo reverts it.
  function setSizing(id: string) {
    const ed = monacoRef.current;
    const model = ed?.getModel();
    if (!ed || !model) return;
    const r = placeSizingMethod(model.getValue(), id, model.getEOL());
    if (!r.ok) {
      setSaved({ ok: false, text: r.why });
      return;
    }
    ed.pushUndoStop();
    ed.executeEdits("money", [{ range: model.getFullModelRange(), text: r.text, forceMoveMarkers: true }]);
    ed.pushUndoStop();
    ed.focus();
  }

  // A drop is handled here, not by Monaco: its own drop treats the text as a snippet and writes a
  // literal "$0". The text goes on its own line under the line it fell on, with that line's indent.
  function allowDrop(e: DragEvent<HTMLDivElement>) {
    const t = e.dataTransfer.types;
    if (t.includes("text/plain") || t.includes("application/x-sizing-method")) e.preventDefault();
  }
  function drop(e: DragEvent<HTMLDivElement>) {
    const money = e.dataTransfer.getData("application/x-sizing-method");
    if (money) {
      e.preventDefault();
      e.stopPropagation();
      setSizing(money);
      return;
    }
    const text = e.dataTransfer.getData("text/plain");
    const ed = monacoRef.current;
    if (!text || !ed) return;
    e.preventDefault();
    e.stopPropagation();
    const model = ed.getModel();
    const at = ed.getTargetAtClientPoint(e.clientX, e.clientY);
    if (!model || !at?.position) return;
    const line = at.position.lineNumber;
    const indent = /^\s*/.exec(model.getLineContent(line))?.[0] ?? "";
    const col = model.getLineMaxColumn(line);
    ed.executeEdits("catalogue", [{
      range: { startLineNumber: line, startColumn: col, endLineNumber: line, endColumn: col },
      text: `\n${indent}${text}`,
      forceMoveMarkers: true,
    }]);
    ed.focus();
  }

  function back() {
    if (changed) setLeaving(true);
    else onBack(everSaved ? id : null);
  }

  const firstLine = check?.kind === "ok" ? (check.description.split("\n")[0] ?? "") : "";
  // The line under the title: where the code stands against its last save.
  const writtenAt = versions.data?.length ? Math.max(...versions.data.map((v) => v.edited_at)) : null;
  const line = id === null ? (
    <p className="se-line"><b>A new strategy, not saved yet.</b></p>
  ) : changed ? (
    <p className="se-line"><b>{`Changes since version ${version ?? 1}.`}</b><span><em className="warn">Not saved yet.</em>{` Validate checks the text. Save stores it as version ${(version ?? 1) + 1}.`}</span></p>
  ) : (
    <p className="se-line"><b>{`Version ${version ?? 1}${writtenAt === null ? "" : `, written ${day(writtenAt, true)}`}. Nothing changed since the last save.`}</b><span>Press a row of the catalogue to write it into the code where the cursor is.</span></p>
  );

  return (
    <div className="v2-page se" data-testid="strategy-editor">
      <button type="button" className="se-crumb" onClick={back}>Strategies</button>
      <PageHeader crumb="Strategies" title={title} />
      {line}
      <div className={`se-grid${hidden.cat ? " no-cat" : ""}${hidden.side ? " no-side" : ""}`}>
      <CataloguePanel ready={mounted} onInsert={insert} />
      <div className="se-card">
        <Panel title="Code" note={<>
          <State tone="run">{version === null ? "Version 1, new" : `Version ${version}`}</State>
          {pos && <span className="se-pos">{`Line ${pos.line}, column ${pos.col}`}</span>}
          <span className="se-tg">
            <button type="button" className="se-tb" aria-pressed={!hidden.cat} onClick={() => setHidden({ ...hidden, cat: !hidden.cat })}>Catalogue</button>
            <button type="button" className="se-tb" aria-pressed={!hidden.side} onClick={() => setHidden({ ...hidden, side: !hidden.side })}>Side panels</button>
          </span>
        </>}>
          <div onDragOverCapture={allowDrop} onDropCapture={drop}>
          <Suspense fallback={<p className="empty">Loading the editor.</p>}>
            <CodeEditor
              label="Code"
              value={code}
              onChange={edit}
              onMount={(ed) => {
                monacoRef.current = ed; setMounted(true);
                ed.onDidChangeCursorPosition((e) => setPos({ line: e.position.lineNumber, col: e.position.column }));
              }}
              openAt={openAt}
              errorLine={check?.kind === "bad" ? check.line : null}
              errorText={check?.kind === "bad" ? check.text : ""}
            />
          </Suspense>
          </div>
          {!check && (
            <div className="se-strip">
              <State tone="dim">Not checked</State>
              <span>Press Validate to check the text as it stands.</span>
            </div>
          )}
          {check && (
            <div className={`se-strip ${check.kind === "ok" ? "ok" : "bad"}`} role="status" aria-label="Validation">
              {check.kind === "ok" ? (
                <>
                  <StateWord word="VALID" tone="g" />
                  <span title={check.description}>Description found: {firstLine}</span>
                </>
              ) : (
                <>
                  <StateWord word={check.line === null ? "INVALID" : `LINE ${check.line}`} tone="rd" />
                  <span>{check.text}</span>
                </>
              )}
            </div>
          )}
        </Panel>
      </div>
      <div className="se-side">
        <MoneyPanel strategyId={id} version={version} ready={mounted} onMoney={setSizing} />
        <RiskPanel />
        <ParametersPanel strategyId={id} version={version} />
      </div>
      </div>
      <div className="se-bar">
        <button type="button" className="btn" disabled={checking} onClick={() => void validate()}>Validate</button>
        <QuickTestDash />
        <button
          type="button"
          className="btn q"
          disabled={id === null}
          title={id === null ? "Save the strategy first" : undefined}
          onClick={() => setWindowOpen("versions")}
        >
          {versions.data ? `Versions ${versions.data.length}` : "Versions"}
        </button>
        <button
          type="button"
          className="btn q"
          disabled={id === null}
          title={id === null ? "Save the strategy first" : undefined}
          onClick={() => setWindowOpen("settings")}
        >
          Settings
        </button>
        <span className="se-fill" />
        {saved && <span className={saved.ok ? "g" : "rd"} role="status">{saved.text}</span>}
        <button type="button" className="btn q" onClick={back}>Back</button>
        <button
          type="button"
          className="btn"
          disabled={saving || !canSave}
          title={canSave ? undefined : `Nothing changed since version ${version ?? 1}`}
          onClick={() => void save()}
        >
          Save
        </button>
        <button type="button" className="btn" disabled={saving} onClick={() => void saveAndSweep()}>Save and run a sweep</button>
      </div>
      {sweepFor !== null && <SweepWindow strategyId={sweepFor} onClose={() => setSweepFor(null)} />}
      {id !== null && windowOpen === "versions" && <VersionsWindow id={id} onClose={() => setWindowOpen(null)} />}
      {id !== null && windowOpen === "settings" && <SettingsWindow id={id} onClose={() => setWindowOpen(null)} />}
      {leaving && (
        <Window label="Leave the editor?" size="s" onClose={() => setLeaving(false)}>
          <div className="h2"><span>Leave the editor?</span></div>
          <p>Changes since the last save are lost.</p>
          <div className="se-foot">
            <button type="button" className="btn q" onClick={() => setLeaving(false)}>CANCEL</button>
            <button type="button" className="btn danger" onClick={() => onBack(everSaved ? id : null)}>LEAVE</button>
          </div>
        </Window>
      )}
    </div>
  );
}
