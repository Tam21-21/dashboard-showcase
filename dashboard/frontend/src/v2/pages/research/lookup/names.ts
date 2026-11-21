// Names and times for the ticker lookup (display_methods.md, "Ticker lookup window"). Display data only.

/** One company as the lookup hands it on: the ticker, its name in plain case, the SEC id and the history route's code. */
export type Company = { ticker: string; name: string; cik: number; symbol: string };

/** The SEC list writes many names in capitals with a state code after a slash ("APPLIED MATERIALS INC /DE"): shown in plain case. */
export function tidy(n: string): string {
  const s = n.replace(/\s*\/[A-Za-z]{2,3}\/?\s*$/, "");
  return s === s.toUpperCase() ? s.toLowerCase().replace(/(^|[\s.&-])([a-z])/g, (_m, a: string, b: string) => a + b.toUpperCase()) : s;
}

/** A company's name without its legal ending ("Apple Inc." gives "Apple"). */
export function coShort(n: string): string {
  let s = n;
  let t: string;
  do {
    t = s;
    s = s.replace(/[,.]?\s+(Inc|Incorporated|Corp|Corporation|Co|Company|Ltd|Limited|plc|Holdings|Group)\.?$/i, "");
  } while (s !== t);
  return s;
}

const PARIS = "Europe/Paris";
const pick = (d: Date, o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-GB", { timeZone: PARIS, ...o }).format(d);

/** A headline's time as "16 Nov, 13:45", Paris time. */
export function newsWhen(iso: string): string {
  const d = new Date(iso);
  return `${pick(d, { day: "numeric", month: "short" })}, ${pick(d, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}`;
}

/** An hourly bar is stamped with its start; its price is the one at the hour's end, so it is shown at its end, Paris time. */
export function hourLabel(iso: string, regular: boolean): string {
  const d = new Date(Date.parse(iso) + 3_600_000);
  const day = `${pick(d, { weekday: "short" })} ${pick(d, { day: "numeric" })} ${pick(d, { month: "short" })}`;
  return `${day}, ${pick(d, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}${regular ? "" : ", outside regular hours"}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2026-11-13" as "13 Nov 2026". */
export function dayWord(iso: string): string {
  return `${Number(iso.slice(8, 10))} ${MONTHS[Number(iso.slice(5, 7)) - 1]} ${iso.slice(0, 4)}`;
}

/** The sign word for a change: "up 0.1%", "down 2.0%" or "unchanged". */
export function moved(v: number): string {
  return v > 0 ? `up ${v.toFixed(1)}%` : v < 0 ? `down ${Math.abs(v).toFixed(1)}%` : "unchanged";
}

/** An entity the publisher left in its text ("Apple&#39;s") read back as the character it stands for. */
export function plain(s: string): string {
  const t = document.createElement("textarea");
  t.innerHTML = s;
  return t.value;
}
