import { Suspense, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CodeEditor, Table, Window } from "../../../kit";
import {
  clearStrategySetting, fetchStrategySettings, fetchStrategyVersion, fetchStrategyVersions, setStrategySetting,
  strategiesKeys,
} from "../../../data/strategiesApi";
import type { StrategySetting } from "../../../data/strategiesApi";
import { serverDetail } from "../../../../lib/errorText";
import { fmtDmy, plainStrategyName } from "../../../../lib/format";

// Mockup: docs/design/rail_mockup_2026-10-10.html, the "Versions" and "Settings" dlModal calls of
// editorAfter (list items E8 and E9).

// ---- VERSIONS ----

export function VersionsWindow({ id, onClose }: { id: number; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: strategiesKeys.versions(id), queryFn: () => fetchStrategyVersions(id) });
  const [opened, setOpened] = useState<number | null>(null);
  const code = useQuery({
    queryKey: ["strategies", "version-code", id, opened],
    queryFn: () => fetchStrategyVersion(id, opened as number),
    enabled: opened !== null,
    gcTime: 0,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const open = (v: number) => setOpened(v);
  const back = () => {
    qc.removeQueries({ queryKey: ["strategies", "version-code", id] });
    setOpened(null);
  };

  let body;
  if (opened !== null) {
    body = (
      <>
        <div className="h2"><span>Version {opened}</span></div>
        {code.isError && <p className="rd ew-note">{serverDetail(code.error)}</p>}
        {!code.isError && !code.data && <p className="empty">Loading the code.</p>}
        {code.data && (
          <Suspense fallback={<p className="empty">Loading the editor.</p>}>
            <CodeEditor label={`Version ${opened} code`} value={code.data.source_code} readOnly />
          </Suspense>
        )}
        <div className="se-foot">
          <button type="button" className="btn q" onClick={back}>BACK</button>
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        </div>
      </>
    );
  } else if (q.isError) {
    body = <p className="rd ew-note">{serverDetail(q.error)}</p>;
  } else if (!q.data) {
    body = <p className="empty">Loading the versions.</p>;
  } else if (q.data.length === 0) {
    body = <p className="empty">No version is recorded yet.</p>;
  } else {
    body = (
      <Table
        head={["Version", "Saved", "Description", ""]}
        rows={q.data.map((v) => {
          const first = v.description ? (v.description.split("\n")[0] ?? "") : "";
          return [
            String(v.version),
            fmtDmy(v.edited_at),
            first
              ? <span title={v.description ?? undefined}>{first}</span>
              : <span title="No description">-</span>,
            <button key="o" type="button" className="btn q ew-btn" onClick={() => open(v.version)}>OPEN</button>,
          ];
        })}
      />
    );
  }
  return (
    <Window label="Versions" size="l" className="ew-tbl" onClose={onClose}>
      <div className="h2"><span>VERSIONS</span></div>
      {body}
      {opened === null && (
        <div className="se-foot">
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        </div>
      )}
    </Window>
  );
}

// ---- SETTINGS ----

const words = (key: string) => plainStrategyName(key.replace(/[._]/g, " "));

function shown(v: unknown): { text: string; empty: boolean } {
  if (typeof v === "number") return { text: String(v), empty: false };
  if (typeof v === "string") return v === "" ? { text: "-", empty: true } : { text: v, empty: false };
  if (typeof v === "boolean") return { text: v ? "yes" : "no", empty: false };
  return { text: JSON.stringify(v), empty: false };
}

type Editing = { key: string; value: string; note: string };

export function SettingsWindow({ id, onClose }: { id: number; onClose: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: strategiesKeys.settings(id), queryFn: () => fetchStrategySettings(id) });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<{ key: string; text: string } | null>(null);

  const rowKey = (s: StrategySetting) => `${s.scope}/${s.key}`;
  const refetch = () => qc.invalidateQueries({ queryKey: strategiesKeys.settings(id) });

  function startSet(s: StrategySetting) {
    setRefusal(null);
    setEditing({ key: rowKey(s), value: shown(s.value).empty ? "" : shown(s.value).text, note: s.source_note ?? "" });
  }

  async function save(s: StrategySetting) {
    if (!editing) return;
    setBusy(true);
    setRefusal(null);
    try {
      await setStrategySetting(id, s.scope, s.key, editing.value, editing.note.trim() || null);
      setEditing(null);
      await refetch();
    } catch (e) {
      setRefusal({ key: rowKey(s), text: serverDetail(e) });
    }
    setBusy(false);
  }

  async function clear(s: StrategySetting) {
    setBusy(true);
    setRefusal(null);
    try {
      await clearStrategySetting(id, s.scope, s.key);
      if (editing?.key === rowKey(s)) setEditing(null);
      await refetch();
    } catch (e) {
      setRefusal({ key: rowKey(s), text: serverDetail(e) });
    }
    setBusy(false);
  }

  const rows = (q.data ?? []).filter((s) => s.overridable);
  let body;
  if (q.isError) {
    body = <p className="rd ew-note">{serverDetail(q.error)}</p>;
  } else if (!q.data) {
    body = <p className="empty">Loading the settings.</p>;
  } else if (rows.length === 0) {
    body = <p className="empty">No setting can be set for one strategy</p>;
  } else {
    const editRow = editing ? rows.find((r) => rowKey(r) === editing.key) : undefined;
    const refusedRow = refusal ? rows.find((r) => rowKey(r) === refusal.key) : undefined;
    body = (
      <>
        <Table
          head={["Setting", "Value", "Source", ""]}
          rows={rows.map((s) => {
            const v = shown(s.value);
            return [
              `${words(s.scope)}, ${s.label ?? words(s.key)}`,
              <span key="v" className="num" title={v.empty ? "Empty" : undefined}>{v.text}</span>,
              <span key="s" className="ew-src">
                {s.source === "override" ? (s.source_note ? `override: ${s.source_note}` : "override") : "global default"}
              </span>,
              <span key="a" className="ew-acts">
                <button type="button" className="btn q ew-btn" disabled={busy} onClick={() => startSet(s)}>SET</button>
                {s.source === "override" && (
                  <button type="button" className="btn q ew-btn" disabled={busy} onClick={() => void clear(s)}>CLEAR</button>
                )}
              </span>,
            ];
          })}
        />
        {editing && editRow && (
          <div className="ew-edit">
            <div className="ew-for">Setting: {words(editRow.scope)}, {editRow.label ?? words(editRow.key)}</div>
            <div className="ew-form">
              <input
                aria-label="Value"
                className="fin num"
                value={editing.value}
                onChange={(e) => setEditing({ ...editing, value: e.target.value })}
              />
              <input
                aria-label="Note"
                className="fin"
                placeholder="Why this value"
                value={editing.note}
                onChange={(e) => setEditing({ ...editing, note: e.target.value })}
              />
              <button type="button" className="btn" disabled={busy} onClick={() => void save(editRow)}>SAVE</button>
              <button type="button" className="btn q" disabled={busy} onClick={() => { setEditing(null); setRefusal(null); }}>CANCEL</button>
            </div>
          </div>
        )}
        {refusal && refusedRow && (
          <p className="rd ew-note">{words(refusedRow.scope)}, {refusedRow.label ?? words(refusedRow.key)}: {refusal.text}</p>
        )}
      </>
    );
  }
  return (
    <Window label="Settings" size="l" className="ew-tbl" onClose={onClose}>
      <div className="h2"><span>SETTINGS</span></div>
      {body}
      <div className="se-foot">
        <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
      </div>
    </Window>
  );
}
