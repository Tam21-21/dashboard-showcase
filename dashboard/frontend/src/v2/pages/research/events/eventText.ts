import { fmtDmy } from "../../../../lib/format";
import type { EventSearchPage } from "../../../../types/research";

// Pure words of the Events tab (mockup: docs/design/rail_mockup_2026-10-10.html, ETY, ACTW, evText, openEvent).

export type SearchedEvent = EventSearchPage["events"][number];

export const GREEN = "#2fe08a";
export const RED = "#ff5a63";
export const BLUE = "#4d9bff";

export type EventKind = { type: string; name: string; colour: string };
// The three kinds the server holds, in the mockup's chip order. Company filings wait for lane EA.
export const KINDS: EventKind[] = [
  { type: "news", name: "News", colour: "#a78bfa" },
  { type: "analyst_rating", name: "Analyst ratings", colour: "#e8d49a" },
  { type: "sec_13d", name: "Large holders", colour: "#2fd1c0" },
];
export const kindOf = (type: string): EventKind => {
  const k = KINDS.find((x) => x.type === type);
  if (!k) throw new Error(`unknown event type ${type}`);
  return k;
};

// The legend colours of the analyst-action donut, in order (the mockup's RAC).
export const ACTION_COLOURS = ["#8d98a8", "#c9d2de", GREEN, RED, BLUE, "#a78bfa", "#e8d49a"];

type Tone = "g" | "rd" | "";
export const ACTW: Record<string, [string, Tone]> = {
  Upgrades: ["UPGRADE", "g"],
  Downgrades: ["DOWNGRADE", "rd"],
  Maintains: ["KEEPS", ""],
  Reiterates: ["REPEATS", ""],
  "Initiates Coverage On": ["STARTS", ""],
  Reinstates: ["RESTARTS", ""],
  Assumes: ["TAKES OVER", ""],
};
export const actionWord = (action: string): [string, Tone] => ACTW[action] ?? [action.toUpperCase(), ""];
export const sentenceCase = (word: string): string => word.charAt(0) + word.slice(1).toLowerCase();

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const two = (n: number) => String(n).padStart(2, "0");

// "21 Oct, 14:04", UTC (the mockup's stamp).
export function rowStamp(epoch: number): string {
  const d = new Date(epoch * 1000);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
}
// "21/10/2026 14:04 UTC".
export function knownAt(epoch: number): string {
  const d = new Date(epoch * 1000);
  return `${fmtDmy(epoch)} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())} UTC`;
}

const str = (v: unknown): string | null => (typeof v === "string" && v.length > 0 ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const dollars = (n: number) => `$${n.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

export type TextPart = { text: string; tone?: Tone; word?: boolean };

// The words of one row, as parts so the page can colour the action word and the percent.
export function eventParts(e: SearchedEvent): TextPart[] {
  const p = e.payload;
  if (e.event_type === "news") return [{ text: str(p.headline) ?? "No headline" }];
  if (e.event_type === "analyst_rating") {
    const [word, tone] = actionWord(str(p.action_company) ?? "");
    const oldG = str(p.old_grade);
    const newG = str(p.new_grade);
    const grades = oldG && newG && oldG !== newG ? `${oldG} to ${newG}` : (newG ?? oldG);
    let lead = [str(p.analyst_firm), grades].filter((x): x is string => x !== null).join(", ");
    const before = num(p.pt_prior);
    const after = num(p.pt_current);
    const parts: TextPart[] = [];
    if (word !== "") parts.push({ text: sentenceCase(word), tone, word: true }, { text: ": " });
    if (before !== null && after !== null) {
      lead += `, target ${dollars(before)} to ${dollars(after)}`;
      parts.push({ text: lead });
      if (before > 0 && after !== before) {
        const change = (after / before - 1) * 100;
        parts.push({ text: " " }, { text: `(${change > 0 ? "+" : ""}${change.toFixed(1)}%)`, tone: after > before ? "g" : "rd" });
      }
    } else {
      parts.push({ text: lead });
    }
    return parts;
  }
  const filer = str(p.filer_name) ?? "Unknown holder";
  const subject = str(p.subject_name) ?? "an unknown company";
  const pct = num(p.percent_owned);
  return [{ text: pct === null ? `${filer} filed on ${subject}` : `${filer} owns ${pct}% of ${subject}` }];
}

export const partsText = (parts: TextPart[]): string => parts.map((x) => x.text).join("");

export const SOURCE_LINE: Record<string, string> = {
  news: "Alpaca news, Benzinga wire",
  analyst_rating: "Benzinga analyst calendar",
  sec_13d: "SEC EDGAR, Schedule 13D",
};

// The label and value rows of a detail, per type (the mockup's openEvent).
export function detailRows(e: SearchedEvent): Array<[string, string]> {
  const p = e.payload;
  const known: [string, string] = ["Known at", knownAt(e.knowledge_at)];
  const source: [string, string] = ["Source", SOURCE_LINE[e.event_type] ?? e.source];
  if (e.event_type === "news") return [known, ["Tickers", e.tickers.join(", ") || "-"], source];
  if (e.event_type === "analyst_rating") {
    const before = num(p.pt_prior);
    const after = num(p.pt_current);
    const target = after === null ? "-" : before === null ? dollars(after) : `${dollars(before)} to ${dollars(after)}`;
    return [known, ["Firm", str(p.analyst_firm) ?? "-"], ["Rating", `${str(p.old_grade) ?? "-"} to ${str(p.new_grade) ?? "-"}`], ["Price target", target], source];
  }
  const pct = num(p.percent_owned);
  return [known, ["Holder", str(p.filer_name) ?? "-"], ["Company", str(p.subject_name) ?? "-"], ["Stake", pct === null ? "-" : `${pct}%`], source];
}

export const articleUrl = (e: SearchedEvent): string | null => (e.event_type === "news" ? str(e.payload.url) : null);
