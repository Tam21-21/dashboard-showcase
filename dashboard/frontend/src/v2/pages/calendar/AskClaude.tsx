import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { serverDetail } from "../../../lib/errorText";
import { usePageMemory } from "../../kit";
import AskWindow, { applyRow } from "./AskWindow";
import type { Outcome, Row, Turn } from "./AskWindow";

// The Ask Claude card: one button that opens the conversation window. The conversation (turns,
// proposal rows, DONE marks) is kept by page memory for 15 minutes. Nothing reaches Google until
// APPLY is pressed on a row; it calls the same calendar routes as the rest of the page.

const HISTORY_LIMIT = 20;

export default function AskClaude() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [mem, setMem, clearMem] = usePageMemory<{ turns: Turn[] }>("calendar/ask", { turns: [] });
  const [question, setQuestion] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [askError, setAskError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const latest = useRef(mem);
  latest.current = mem;
  const working = useRef(false);

  const send = async () => {
    const q = question.trim();
    if (!q || working.current) return;
    working.current = true;
    setPendingQuestion(q);
    setAskError(null);
    const history = latest.current.turns
      .map((t) => ({ role: t.role, text: t.text }))
      .slice(-HISTORY_LIMIT);
    try {
      const res = await api.life.ask(q, history);
      setMem((m) => ({
        turns: [
          ...m.turns,
          { role: "user", text: q },
          { role: "assistant", text: res.answer, rows: res.proposals.map((proposal) => ({ proposal, done: false })) },
        ],
      }));
      setQuestion("");
    } catch (err) {
      setAskError(serverDetail(err));
    } finally {
      working.current = false;
      setPendingQuestion(null);
    }
  };

  const patch = (ti: number, ri: number, out: Outcome) =>
    setMem((m) => ({
      turns: m.turns.map((t, i) =>
        i !== ti || !t.rows
          ? t
          : {
              ...t,
              rows: t.rows.map((r, j): Row =>
                j !== ri
                  ? r
                  : out.done
                    ? { proposal: r.proposal, done: true, note: out.note }
                    : { ...r, error: out.error, removed: out.removed },
              ),
            },
      ),
    }));

  // Applies the given rows one after another and stops at the first refusal. A row already done is
  // skipped, so nothing applies twice. The range is refreshed once at the end.
  const applyRows = async (cells: [number, number][]) => {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    let touched = false;
    try {
      for (const [ti, ri] of cells) {
        const row = latest.current.turns[ti]?.rows?.[ri];
        if (!row || row.done) continue;
        touched = true;
        const out = await applyRow(row);
        patch(ti, ri, out);
        if (!out.done) break;
      }
    } finally {
      working.current = false;
      setBusy(false);
      if (touched) void qc.invalidateQueries({ queryKey: ["calendar", "range"] });
    }
  };
  const applyAll = () => {
    const cells: [number, number][] = [];
    latest.current.turns.forEach((t, ti) => (t.rows ?? []).forEach((r, ri) => { if (!r.done) cells.push([ti, ri]); }));
    void applyRows(cells);
  };

  return (
    <div className="cask">
      <button type="button" className="btn" title="Claude reads the next 60 days of your calendar" onClick={() => setOpen(true)}>
        Ask Claude
      </button>
      {open && (
        <AskWindow
          turns={mem.turns}
          pendingQuestion={pendingQuestion}
          askError={askError}
          question={question}
          busy={busy || pendingQuestion !== null}
          onQuestion={setQuestion}
          onSend={() => void send()}
          onApplyRow={(ti, ri) => void applyRows([[ti, ri]])}
          onApplyAll={applyAll}
          onNew={() => { clearMem(); setAskError(null); }}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
