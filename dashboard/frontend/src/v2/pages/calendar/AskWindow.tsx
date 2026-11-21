import { useEffect, useRef } from "react";
import { api } from "../../../lib/api";
import type { AskProposal } from "../../../lib/api";
import { serverDetail } from "../../../lib/errorText";
import { Window } from "../../kit";
import { addDays, weekdayIndex } from "./data";

// The Ask Claude window: the whole conversation, one row per proposal, APPLY and APPLY ALL.
// The state lives in AskClaude (it must survive closing the window); this file draws it and
// holds the plain-words labels and the calls a row makes.

export type Row = {
  proposal: AskProposal;
  done: boolean;
  // Words shown beside DONE (a create whose calendar refresh failed).
  note?: string;
  // The server's words when the last try was refused.
  error?: string;
  // Remove only: how many events are already gone, so a retry starts after them.
  removed?: number;
};
export type Turn = { role: "user" | "assistant"; text: string; rows?: Row[] };

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const RULE_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

// "2026-10-14" to "Wed 14 Oct", built from the text so no zone can move the day.
const dayLabel = (iso: string) =>
  `${DAY_NAMES[weekdayIndex(iso)]} ${Number(iso.slice(8, 10))} ${MONTH_NAMES[Number(iso.slice(5, 7)) - 1]}`;

export function proposalLabel(p: AskProposal): string {
  switch (p.kind) {
    case "add":
      return p.all_day
        ? `Add: ${p.title}, ${dayLabel(p.date)}, all day`
        : `Add: ${p.title}, ${dayLabel(p.date)} ${p.start} to ${p.end}`;
    case "series": {
      const days = p.days.map((d) => DAY_NAMES[RULE_DAYS.indexOf(d)]).join(", ");
      const until = p.until ? `, until ${dayLabel(p.until)}` : "";
      return `Every ${days}: ${p.title} ${p.start} to ${p.end}, from ${dayLabel(p.first_date)}${until}`;
    }
    case "remove":
      return `Remove ${p.event_ids.length} ${p.event_ids.length === 1 ? "event" : "events"}: ${p.titles.join(", ")}`;
    case "move":
      return p.all_day
        ? `Move: ${p.title} to ${dayLabel(p.date)}, all day`
        : `Move: ${p.title} to ${dayLabel(p.date)} ${p.start} to ${p.end}`;
  }
}

// What one try on a row came to: done (maybe with a note), or refused with the server's words.
export type Outcome = { done: true; note?: string } | { done: false; error: string; removed?: number };

const created = (res: { sync_error: string | null }): Outcome => {
  const why = res.sync_error;
  return why ? { done: true, note: `Added; calendar refresh failed: ${why}` } : { done: true };
};

// Calls the calendar routes for one row, one after another. A refusal comes back as words, never thrown.
export async function applyRow(row: Row): Promise<Outcome> {
  const p = row.proposal;
  let removed = row.removed ?? 0;
  try {
    switch (p.kind) {
      case "add":
        return created(
          await api.life.calendarCreate(
            p.all_day
              ? { title: p.title, start_dt: `${p.date}T00:00:00`, end_dt: `${addDays(p.date, 1)}T00:00:00`, all_day: true }
              : { title: p.title, start_dt: `${p.date}T${p.start}:00`, end_dt: `${p.date}T${p.end}:00`, all_day: false },
          ),
        );
      case "series": {
        const until = p.until ? `;UNTIL=${p.until.replaceAll("-", "")}T235959Z` : "";
        return created(
          await api.life.calendarCreate({
            title: p.title,
            start_dt: `${p.first_date}T${p.start}:00`,
            end_dt: `${p.first_date}T${p.end}:00`,
            all_day: false,
            recurrence: [`RRULE:FREQ=WEEKLY;BYDAY=${p.days.join(",")}${until}`],
          }),
        );
      }
      case "remove":
        for (; removed < p.event_ids.length; removed++) await api.life.deleteCalendarEvent(p.event_ids[removed]);
        return { done: true };
      case "move":
        await api.life.calendarMoveEvent(
          p.event_id,
          p.all_day
            ? { start_dt: `${p.date}T00:00:00`, end_dt: `${addDays(p.date, 1)}T00:00:00` }
            : { start_dt: `${p.date}T${p.start}:00`, end_dt: `${p.date}T${p.end}:00` },
        );
        return { done: true };
    }
  } catch (err) {
    const words = serverDetail(err);
    if (p.kind === "remove" && removed > 0) {
      return { done: false, removed, error: `Removed ${removed} of ${p.event_ids.length}: ${words}` };
    }
    return { done: false, removed, error: words };
  }
}

function RowLine({ row, busy, onApply }: { row: Row; busy: boolean; onApply: () => void }) {
  return (
    <div className="askp">
      <span className="askpt">{proposalLabel(row.proposal)}</span>
      {row.done ? (
        <span className="askok">Done</span>
      ) : (
        <button type="button" className="btn q" disabled={busy} onClick={onApply}>Apply</button>
      )}
      {row.done && row.note && <div className="askok askpn">{row.note}</div>}
      {!row.done && row.error && <div className="ferr askpn" role="alert">{row.error}</div>}
    </div>
  );
}

export default function AskWindow({
  turns, pendingQuestion, askError, question, busy, onQuestion, onSend, onApplyRow, onApplyAll, onNew, onClose,
}: {
  turns: Turn[];
  pendingQuestion: string | null;
  askError: string | null;
  question: string;
  busy: boolean;
  onQuestion: (v: string) => void;
  onSend: () => void;
  onApplyRow: (turn: number, row: number) => void;
  onApplyAll: () => void;
  onNew: () => void;
  onClose: () => void;
}) {
  const log = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [turns, pendingQuestion, askError]);

  const waiting = turns.reduce((n, t) => n + (t.rows ?? []).filter((r) => !r.done).length, 0);
  return (
    <Window label="Ask Claude" title="Ask Claude" size="l" className="cwin askw" onClose={onClose}>
        <p className="cw-note">Claude reads the next 60 days of your calendar and answers with changes it proposes: add, move, remove or repeat an event. Nothing changes until you press Apply on a proposal.</p>
        {(turns.length > 0 || pendingQuestion !== null || askError) && (
          <div className="asklog" ref={log}>
            {turns.map((t, ti) => (
              <div key={ti} className={`askt ${t.role}`}>
                <div className="askwho">{t.role === "user" ? "You" : "Claude"}</div>
                <div className="asktx">{t.text}</div>
                {(t.rows ?? []).map((r, ri) => (
                  <RowLine key={ri} row={r} busy={busy} onApply={() => onApplyRow(ti, ri)} />
                ))}
              </div>
            ))}
            {pendingQuestion !== null && (
              <>
                <div className="askt user">
                  <div className="askwho">You</div>
                  <div className="asktx">{pendingQuestion}</div>
                </div>
                <div className="askwait">Claude is working on it</div>
              </>
            )}
            {askError && <div className="ferr" role="alert">{askError}</div>}
          </div>
        )}
        <div className="cw-form">
          <label className="cw-field">
            <span>Your question</span>
            <textarea
              className="cw-in"
              aria-label="Question for Claude"
              placeholder="Plan my week"
              rows={1}
              autoFocus
              value={question}
              onChange={(e) => onQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  onSend();
                }
              }}
            />
          </label>
          <div className="cw-acts">
            <button type="button" className="btn" disabled={pendingQuestion !== null} onClick={onSend}>Send</button>
            {waiting > 1 && <button type="button" className="btn" disabled={busy} onClick={onApplyAll}>Apply all</button>}
            <button type="button" className="btn" disabled={turns.length === 0 || busy} onClick={onNew}>New conversation</button>
            <span className="cw-note">Each proposal comes back as one sentence with its own Apply button.</span>
          </div>
        </div>
    </Window>
  );
}
