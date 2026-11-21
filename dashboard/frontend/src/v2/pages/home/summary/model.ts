import type { EveningSummary } from "../../../../types";

// The evening Summary's words, worked out from one stored row. Pure functions, no React (mockup lab-summary.js:
// verdict, block, e2, e0). A part with no data is named, never guessed.

export const AT = "22:00";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export type Tone = "good" | "bad" | "warn" | "data" | "dim";
export type Part = { text: string; tone?: Tone };
export type Chip = [Tone, string];

const noon = (d: string) => Date.parse(`${d}T12:00:00Z`);
const dFmt = (d: string, o: Intl.DateTimeFormatOptions) => new Date(noon(d)).toLocaleDateString("en-GB", { timeZone: "UTC", ...o }).replace(/,/g, "");
export const longDay = (d: string) => dFmt(d, { weekday: "long", day: "numeric", month: "long" });
export const weekday = (d: string) => dFmt(d, { weekday: "long" });
export const dayRow = (d: string) => dFmt(d, { weekday: "long", day: "numeric", month: "short" });
export const dayMon = (d: string) => `${Number(d.slice(8, 10))} ${MONTHS[Number(d.slice(5, 7)) - 1]}`;
export const hm = (s: string) => s.slice(11, 16);

export const whole = (n: number) => Math.round(n).toLocaleString("en-US");
export const euro2 = (n: number) => `${n < 0 ? "−" : ""}€${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
/** The day's change of the round-ups account with its sign: "+€0.87", "−€7.17" and, on zero, "€0.00". */
export const signed2 = (n: number) => (n > 0 ? `+${euro2(n)}` : euro2(n));
export const euro0 =(n: number) => `${Math.round(n) < 0 ? "−" : ""}€${whole(Math.abs(n))}`;

/** The Paris calendar date of a moment, as YYYY-MM-DD. */
export function parisDay(now: Date): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Paris" }).format(now);
}
/** The Paris clock time of a stored moment, as HH:MM. */
export function parisTime(iso: string): string {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return `${p.hour}:${p.minute}`;
}
/** The Paris day and time of a stored moment, as "14 Nov, 18:07". */
export function parisStamp(iso: string): string {
  return `${dayMon(parisDay(new Date(iso)))}, ${parisTime(iso)}`;
}
const addDay = (d: string, n: number) => new Date(noon(d) + n * 864e5).toISOString().slice(0, 10);

/** True when the Summary covers today in Paris, that is, it was made this evening. */
export function isTonight(s: EveningSummary, now: Date): boolean {
  return s.day === parisDay(now);
}

export function updatedWords(s: EveningSummary, now: Date): string {
  const at = parisTime(s.made_at), made = parisDay(new Date(s.made_at)), today = parisDay(now);
  if (made === today) return `Updated ${at}`;
  if (made === addDay(today, -1)) return `Updated yesterday, ${at}`;
  return `Updated ${dayMon(made)}, ${at}`;
}

const watchWhy = (s: EveningSummary) => (s.watch_last ? `Your watch has sent nothing since ${dayMon(s.watch_last)}. Open Garmin Connect on your phone so it can sync.` : "Your watch has not synced yet. Open Garmin Connect on your phone so it can sync.");

/** The day in one sentence, as parts with a tone: on budget or over, moved enough or not. */
export function sentence(s: EveningSummary, now: Date): Part[] {
  const who = isTonight(s, now) ? "You " : `On ${weekday(s.day)} you `;
  const m = s.money, b = s.body;
  const bud: Part | null = m ? (m.under ? { text: "stayed on budget", tone: "good" } : { text: `went ${euro2(-m.gap)} over budget`, tone: "bad" }) : null;
  const stp: Part | null = b ? (b.reached ? { text: "moved enough", tone: "good" } : { text: "moved less than your goal", tone: "warn" }) : null;
  if (bud && stp) return [{ text: who }, bud, { text: ` ${(bud.tone === "good") === (stp.tone === "good") ? "and" : "but"} ` }, stp, { text: "." }];
  if (bud) return [{ text: who }, bud, { text: ". " }, { text: "Your watch has not synced", tone: "warn" }, { text: ", so how much you moved is not known." }];
  if (stp) return [{ text: who }, stp, { text: ". " }, { text: "The bank was not read", tone: "warn" }, { text: ", so what you spent is not known." }];
  return [{ text: "Nothing to sum up tonight.", tone: "warn" }, { text: ` No bank was read and ${s.watch_last ? `your watch has sent nothing since ${dayMon(s.watch_last)}` : "your watch has not synced"}.` }];
}

export type Key = "today" | "month" | "burn" | "steps" | "round";
export type Card = { chip: Chip | null; fig: string; unit: string } | null;
export type States = {
  tone: Tone | ""; today: Card; month: Card; burn: Card; steps: Card; round: Card;
  why: Record<Key, string>; miss: Record<Key, Chip>;
};

/** Each card's state word, figure and unit, and what a part with no data says in place of its picture. */
export function states(s: EveningSummary, now: Date): States {
  const m = s.money, b = s.body, tonight = isTonight(s, now), dn = weekday(s.day);
  const tone: Tone | "" = m ? (m.st === "over" ? "bad" : m.st === "ahead" ? "warn" : "good") : "";
  const diff = b && b.avg_kcal != null ? b.kcal - b.avg_kcal : null;
  const todayChip: Chip | null = m ? (m.today === 0 && m.in > 0 ? ["data", "Money in"] : [m.under ? "good" : "bad", m.under ? `${euro2(m.gap)} left` : `${euro2(-m.gap)} over`]) : null;
  return {
    tone,
    today: m ? { chip: todayChip, fig: euro2(m.today), unit: `of ${euro2(m.share)}` } : null,
    month: m ? { chip: [tone as Tone, { ok: "On pace", ahead: "Ahead of pace", over: "Over budget" }[m.st]], fig: euro0(m.spent), unit: `of ${euro0(m.budget)}` } : null,
    burn: b ? { chip: diff == null || b.avg_kcal == null ? null : ["data", Math.abs(diff) < b.avg_kcal * 0.03 ? "As usual" : diff > 0 ? "Above usual" : "Below usual"], fig: whole(b.kcal), unit: "kcal" } : null,
    steps: b ? { chip: [b.reached ? "good" : "warn", b.reached ? "Goal reached" : `${whole(b.short)} short`], fig: whole(b.steps), unit: `of ${whole(b.goal)}` } : null,
    round: s.round ? { chip: ["data", `Total ${euro2(s.round.total)}`], fig: signed2(s.round.today), unit: tonight ? "today" : `on ${dn}` } : null,
    why: {
      today: `The bank was not read ${tonight ? "today" : `on ${dn}`}, so no spending is shown.`,
      month: "The bank was not read, so the month's spending is not known.",
      burn: watchWhy(s), steps: watchWhy(s),
      round: `No round-up was read ${tonight ? "today" : `on ${dn}`}.`,
    },
    // Amber where it waits for him (the watch syncs when he opens Garmin Connect), grey where there is nothing he can do tonight.
    miss: { today: ["dim", "Not read"], month: ["dim", "Not read"], burn: ["warn", "Watch behind"], steps: ["warn", "Watch behind"], round: ["dim", "Not read"] },
  };
}

export { watchWhy };
