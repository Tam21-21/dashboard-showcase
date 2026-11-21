import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { serverDetail } from "../../lib/errorText";
import { runAllRefreshers } from "../../lib/refresh-registry";
import { StateWord, type Tone } from "../kit";
import type { LiveState } from "./live";
import { APPS } from "./sections";
import "./palette.css";

// The command palette (Ctrl+K or Cmd+K): go to any page, refresh everything, hard refresh.

interface Command {
  label: string;
  run: () => Promise<void> | void;
}

const WORD: Record<LiveState, { word: string; tone: Tone }> = {
  open: { word: "LIVE", tone: "g" },
  connecting: { word: "CONNECTING", tone: "bl" },
  closed: { word: "OFF", tone: "a" },
  off: { word: "OFF", tone: "a" },
};

async function hardRefresh() {
  if (navigator.serviceWorker) {
    for (const reg of await navigator.serviceWorker.getRegistrations()) await reg.unregister();
  }
  if (typeof caches !== "undefined") {
    for (const key of await caches.keys()) await caches.delete(key);
  }
  window.location.reload();
}

export default function Palette({
  live, go, onClose,
}: { live: LiveState; go: (to: string) => void; onClose: () => void }) {
  const qc = useQueryClient();
  const [typed, setTyped] = useState("");
  const [at, setAt] = useState(0);
  const [note, setNote] = useState<{ text: string; failed: boolean } | null>(null);
  const box = useRef<HTMLInputElement>(null);

  const commands = useMemo<Command[]>(() => {
    const pages = APPS.flatMap((a) =>
      a.sections.map((s) => ({
        label: `Go to ${a.name} / ${s.label}`,
        run: () => { go(`/app/${a.id}/${s.id}`); onClose(); },
      })),
    );
    return [
      ...pages,
      {
        label: "Refresh everything",
        run: async () => {
          const out = await runAllRefreshers();
          void qc.invalidateQueries({ refetchType: "all" });
          setNote(out.failures.length
            ? { text: `Could not refresh: ${out.failures.join(", ")}`, failed: true }
            : { text: "Refreshed", failed: false });
        },
      },
      { label: "Hard refresh", run: hardRefresh },
    ];
  }, [go, onClose, qc]);

  const shown = commands.filter((c) => c.label.toLowerCase().includes(typed.trim().toLowerCase()));
  const active = Math.min(at, Math.max(shown.length - 1, 0));
  const { word, tone } = WORD[live];

  useEffect(() => { box.current?.focus(); }, []);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (shown.length) setAt((active + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length);
      return;
    }
    if (e.key === "Enter" && shown[active]) {
      e.preventDefault();
      void Promise.resolve(shown[active].run()).catch((err: unknown) => {
        setNote({ text: `Could not run: ${serverDetail(err)}`, failed: true });
      });
    }
  };

  return (
    <div className="pal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }} onKeyDown={onKey}>
      <div className="cd palb" role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="palt">
          <StateWord word={word} tone={tone} />
          <button type="button" className="btn q" onClick={onClose}>CLOSE</button>
        </div>
        <input
          ref={box}
          className="pal-search"
          aria-label="Search commands"
          placeholder="Type a command"
          value={typed}
          onChange={(e) => { setTyped(e.target.value); setAt(0); }}
        />
        <div role="listbox" aria-label="Commands" className="pal-list">
          {shown.map((c, i) => (
            <button
              key={c.label}
              type="button"
              role="option"
              aria-selected={i === active}
              className={i === active ? "pal-row on" : "pal-row"}
              onMouseEnter={() => setAt(i)}
              onClick={() => { void Promise.resolve(c.run()); }}
            >
              {c.label}
            </button>
          ))}
        </div>
        {note && <div className={note.failed ? "a" : "g"}>{note.text}</div>}
      </div>
    </div>
  );
}
