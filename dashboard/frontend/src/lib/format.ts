export const NO_DATA = "No data yet";
// The kit's own dash (lane NV, `U.9`, 2026-09-14, docs/units/NV/criteria.md AC-8e): a plain
// hyphen, the same character StatusBar.tsx already uses for an absent segment value, never the
// em-dash glyph a few older call sites typed by hand. NO_DATA is a STATE-WORD sentence for a whole
// empty section or store ("no strategy is in paper trading yet" reads fine as prose); NO_VALUE is
// for a single NUMERIC cell or tile that has no figure to show, where a sentence would read as
// wrong. Never conflate the two: a numeric column or tile reads NO_VALUE, never NO_DATA.
export const NO_VALUE = "-";

/** Display name for a strategy id. Keep them readable. */
const STRATEGY_NAMES: Record<string, string> = {
  newsbot_v2: "Newsbot",
};

export function displayStrategy(id: string | null | undefined): string {
  if (!id) return NO_DATA;
  return STRATEGY_NAMES[id] ?? id;
}

/** A stored strategy name in plain form: runs of underscores become one space, first letter upper case. */
export function plainStrategyName(name: string | null | undefined): string {
  if (!name) return NO_DATA;
  const spaced = name.replace(/_+/g, " ").trim();
  if (!spaced) return NO_DATA;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function fmtUsd(
  n: number | null | undefined,
  opts: { sign?: boolean } = {},
): string {
  if (n == null || Number.isNaN(n)) return NO_DATA;
  const sign = opts.sign && n > 0 ? "+" : "";
  return (
    sign +
    n.toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

/** Compact currency — whole dollars, no cents, with $ and commas. */
export function fmtUsdCompact(
  n: number | null | undefined,
  opts: { sign?: boolean } = {},
): string {
  if (n == null || Number.isNaN(n)) return NO_DATA;
  const sign = opts.sign && n > 0 ? "+" : "";
  return (
    sign +
    n.toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    })
  );
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return NO_DATA;
  return `${(n * 100).toFixed(digits)}%`;
}

// Like fmtPct but always shows the sign (+ for positive) — for return / PnL columns that scan by
// sign. Pair with signToneClass for the matching colour.
export function fmtPctSigned(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return NO_DATA;
  return `${n > 0 ? "+" : ""}${(n * 100).toFixed(digits)}%`;
}

// Sign → Tailwind text-colour class: positive green, negative red, flat/absent muted. One home for
// the "positive is green, negative is red" rule so tables don't each re-derive it.
export function signToneClass(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n) || n === 0) return "text-muted";
  return n > 0 ? "text-pos" : "text-neg";
}

/**
 * Format a value already in percentage units (e.g. 4.33 → "4.3%").
 * Use for DB fields where the backend already multiplied by 100, like pnl_pct.
 */
export function fmtPctRaw(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return NO_DATA;
  return `${n.toFixed(digits)}%`;
}

export function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return NO_DATA;
  return n.toFixed(digits);
}

/** Full-word duration: "4 minutes", "1 hour 12 minutes", "48 seconds". */
export function humanDuration(minutes: number | null | undefined): string {
  if (minutes == null || Number.isNaN(minutes)) return NO_DATA;
  if (minutes < 1) {
    const s = Math.round(minutes * 60);
    return s === 1 ? "1 second" : `${s} seconds`;
  }
  if (minutes < 60) {
    const rounded = Math.round(minutes);
    return rounded === 1 ? "1 minute" : `${rounded} minutes`;
  }
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes - h * 60);
  const hs = h === 1 ? "1 hour" : `${h} hours`;
  if (m === 0) return hs;
  const ms = m === 1 ? "1 minute" : `${m} minutes`;
  return `${hs} ${ms}`;
}

/** Back-compat short duration; keep for table density where full words hurt. */
export function fmtDuration(minutes: number | null | undefined): string {
  if (minutes == null || Number.isNaN(minutes)) return NO_DATA;
  if (minutes < 1) return `${Math.round(minutes * 60)} sec`;
  if (minutes < 60) return `${minutes.toFixed(1)} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes - h * 60;
  return `${h} hr${m > 0 ? ` ${Math.round(m)} min` : ""}`;
}

// One date convention across the whole dashboard: day/month/year (DD/MM/YYYY). The "en-GB" locale
// emits exactly that order; using it in the shared formatters (here, ONE place) keeps every date
// display consistent without per-page formats. Payloads/URLs/storage stay ISO — this is display-only.
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return NO_DATA;
  const d = parseTimestamp(iso);
  if (!d) return NO_DATA;
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function fmtTimeShort(iso: string | null | undefined): string {
  if (!iso) return NO_DATA;
  const d = parseTimestamp(iso);
  if (!d) return NO_DATA;
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return NO_DATA;
  const d = parseTimestamp(iso);
  if (!d) return NO_DATA;
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric" });
}

/** A fundamental ratio value for display: 2-3 decimals (a small multiple like ROE 0.83, or per-share
 *  dollars like book value 4.15), never a fake precision. Large values get thousands separators. A
 *  non-finite or null value returns NO_DATA — the divide-safe backend never emits one and every call
 *  site guards, but the formatter defends itself too (matches the NO_DATA discipline of its siblings,
 *  so a future caller that forgets the guard can never print "NaN"/"Infinity"). */
export function fmtRatio(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return NO_DATA;
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
}

/** A "YYYY-MM-DD" date input (UTC) -> epoch seconds, or null on empty/unparseable. The fundamentals
 *  reads key on epoch-seconds-UTC-midnight, so a date input is pinned to UTC midnight here. */
export function dateToEpoch(value: string): number | null {
  if (!value) return null;
  const ms = Date.parse(`${value}T00:00:00Z`);
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

/** Epoch seconds (UTC midnight) -> "DD/MM/YYYY", the dashboard's one date convention. Returns "not
 *  set" when null/undefined. UTC-pinned (the epoch is a UTC-midnight calendar date), so a stored date
 *  renders as the same calendar day in every browser timezone — no off-by-one shift. */
export function fmtDmy(epoch: number | null | undefined): string {
  if (epoch == null) return "not set";
  const iso = new Date(epoch * 1000).toISOString().slice(0, 10); // YYYY-MM-DD (UTC, no tz shift)
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y}`;
}

/** The A4 terminal date stamp: "01 NOV 2024" (UTC, day-first, mono-caps). Same day-first ORDER as the
 *  DD/MM/YYYY convention (#111), in the terminal mono-caps grammar the A4 surfaces use for SURFACED
 *  display dates (the date PICKER triggers keep DD/MM/YYYY). UTC, no timezone shift. */
export function fmtEpochDayMon(epoch: number | null | undefined): string {
  if (epoch == null) return "—";
  const d = new Date(epoch * 1000);
  const mon = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" }).toUpperCase();
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${day} ${mon} ${d.getUTCFullYear()}`;
}

/** Full-word age: "Just now", "3 minutes ago", "2 hours ago", "4 days ago". */
export function fmtAge(ageSec: number | null | undefined): string {
  if (ageSec == null || Number.isNaN(ageSec)) return NO_DATA;
  if (ageSec < 10) return "Just now";
  if (ageSec < 60) return `${Math.round(ageSec)} seconds ago`;
  if (ageSec < 120) return "1 minute ago";
  if (ageSec < 3600) return `${Math.round(ageSec / 60)} minutes ago`;
  if (ageSec < 7200) return "1 hour ago";
  if (ageSec < 86400) return `${Math.round(ageSec / 3600)} hours ago`;
  if (ageSec < 172800) return "1 day ago";
  return `${Math.round(ageSec / 86400)} days ago`;
}

/** Parse a timestamp that may be SQLite style ("YYYY-MM-DD HH:MM:SS" UTC)
 *  or Python isoformat (possibly with a TZ offset). */
export function parseTimestamp(iso: string): Date | null {
  if (!iso) return null;
  const withT = iso.includes("T") ? iso : iso.replace(" ", "T");
  const withTz = /[Zz]|[+-]\d\d:?\d\d$/.test(withT) ? withT : `${withT}Z`;
  const d = new Date(withTz);
  return Number.isNaN(d.getTime()) ? null : d;
}

/* ---------- Trading-specific helpers ---------- */

/** Turn the bot's raw exit_reason into a clean human label.
 *  "Take Profit (tp=6.00)" → "Take Profit"
 *  "Hard Floor VaR"        → "Hard Floor"
 */
export function humanReason(
  reason: string | null | undefined,
): { label: string; tone: "pos" | "neg" | "muted" | "warn" } {
  const r = (reason ?? "").toLowerCase();
  if (!r) return { label: NO_DATA, tone: "muted" };
  if (r.includes("take profit")) return { label: "Take Profit", tone: "pos" };
  if (r.includes("hard floor")) return { label: "Hard Floor", tone: "neg" };
  if (r.includes("trailing")) return { label: "Trailing Stop", tone: "muted" };
  if (r.includes("health")) return { label: "Health Score", tone: "warn" };
  if (r.includes("end of day")) return { label: "End of Day", tone: "muted" };
  if (r.includes("time")) return { label: "Time Backstop", tone: "warn" };
  // Fallback: Title-case first segment up to any parenthesis.
  const first = (reason ?? "").split("(")[0].trim();
  return { label: first || NO_DATA, tone: "muted" };
}

/** GPT numeric label → full word. */
export function humanGptLabel(label: number | string | null | undefined): string {
  if (label == null) return NO_DATA;
  if (typeof label === "string") {
    if (label === "N/A") return NO_DATA;
    return label;
  }
  switch (label) {
    case 0:
      return "Bearish";
    case 1:
      return "Neutral";
    case 2:
      return "Mild Bullish";
    case 3:
      return "Strong Bullish";
    case -1:
      return NO_DATA;
    default:
      return NO_DATA;
  }
}

/** Event level → "Info", "Warning", "Error" (full words) for display. */
export function humanLevel(level: string): string {
  switch (level) {
    case "INFO":
      return "Info";
    case "WARNING":
      return "Warning";
    case "ERROR":
      return "Error";
    default:
      return level;
  }
}

/** Capitalize a source-field token ("main" → "Main"). */
export function titleSource(source: string): string {
  if (!source) return "";
  return source
    .split("_")
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

/** Parse bot heartbeat message "Heartbeat | capital=100000.00"
 *  → "Capital: $100,000" (or null if unparseable). */
export function parseHeartbeatCapital(
  message: string | null | undefined,
): string | null {
  if (!message) return null;
  const m = /capital\s*=\s*([-\d.]+)/i.exec(message);
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n)) return null;
  return `Capital: ${fmtUsdCompact(n)}`;
}

/* ---------- Signal-pipeline headline parsing (item 5 format) ---------- */

export type ParsedHeadline = {
  ticker: string;
  headline: string;
  p3Passed: boolean;
  gptLabel: string; // human-readable
  probability: number | null; // 0..1, null if rejected/N/A
  raw: string;
};

function decodeHtmlEntities(str: string): string {
  const txt = document.createElement("textarea");
  txt.innerHTML = str;
  return txt.value;
}

/**
 * Parse the structured event message:
 *   "HEADLINE|{ticker}|{headline}|P3={0|1}|GPT={label}|PROB={float|N/A}"
 * Returns null if the message isn't in that format.
 */
export function parseHeadlineMessage(
  message: string,
): ParsedHeadline | null {
  if (!message || !message.startsWith("HEADLINE|")) return null;
  // Split on "|" — headline text may contain "|" itself in pathological cases;
  // we split to at most 6 parts (prefix + 5 fields) and reattach the rest.
  const parts = message.split("|");
  if (parts.length < 6) return null;
  const [, ticker, ...rest] = parts;
  // The tail may be longer than 4 if the headline contained "|"; reconstruct.
  const p3Idx = rest.findIndex((p) => p.startsWith("P3="));
  if (p3Idx < 0) return null;
  const headline = decodeHtmlEntities(rest.slice(0, p3Idx).join("|"));
  const p3 = rest[p3Idx];
  const gpt = rest[p3Idx + 1] ?? "";
  const prob = rest[p3Idx + 2] ?? "";
  const p3Passed = p3 === "P3=1";
  const gptVal = gpt.startsWith("GPT=") ? gpt.slice(4) : "";
  const probVal = prob.startsWith("PROB=") ? prob.slice(5) : "";
  const probability = probVal === "" || probVal === "N/A" ? null : Number(probVal);
  return {
    ticker: ticker ?? "",
    headline,
    p3Passed,
    gptLabel:
      gptVal === "" || gptVal === "N/A"
        ? "No data yet"
        : gptVal, // already human words per item 5 spec
    probability: Number.isFinite(probability) ? probability : null,
    raw: message,
  };
}

/* ---------- Data humanization (redesign brief §7) ---------------------------
 * Machine values must not headline. These demote raw keys to a readable title
 * while keeping the raw slug available as a join key (rendered via <Slug/>).   */

const SENTINEL_PREFIX = "__"; // e.g. "__event_series__" — not a real ticker

/** Known dataset-slug stems → human title. Longest match wins. */
const DATASET_TITLES: Array<[RegExp, string]> = [
  [/^analyst[_-]?rating/i, "Analyst ratings"],
  [/^sec[_-]?13d/i, "SEC 13D filings"],
];

/**
 * "analyst_rating_benzinga_2025-05-12_2026-06-05" → "Analyst ratings".
 * Falls back to stripping a trailing date range and title-casing the stem.
 */
export function humanizeDatasetName(slug: string | null | undefined): string {
  if (!slug) return NO_DATA;
  for (const [re, title] of DATASET_TITLES) {
    if (re.test(slug)) return title;
  }
  // Strip a trailing "_YYYY-MM-DD_YYYY-MM-DD" range, then humanize words. If the
  // stem is only a date range, fall back to humanizing the whole slug so a raw
  // snake_case value can never headline.
  const stem = slug.replace(/_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}.*$/, "");
  const words =
    stem.replace(/[_-]+/g, " ").trim() || slug.replace(/[_-]+/g, " ").trim();
  if (!words) return slug;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Plain-English labels for the lab-settings `scope.key` rows (improvements brief §2.4a + §2.5).
 * Keyed by the combined `${scope}.${key}` exactly as the catalog renders it. The labels are faithful
 * to docs/research_lab/parameter_provenance.md (a multiplier reads "multiple", a window reads
 * "lookback", etc.) — display only, no value/provenance change. Anything not listed falls back to a
 * humanized token so a new key never renders blank or raw.
 */
const SETTING_LABELS: Record<string, string> = {
  "fees.crypto.default": "Crypto trading fee",
  "fees.equities.default": "Equity trading fee",
  "fees.equities.fixed": "Equity fixed fee",
  "slippage.crypto.default": "Crypto slippage",
  "slippage.equities.default": "Equity slippage",
  "paths.data_root": "Data root path",
  "event_strategy.profit_take_mult": "Profit-take multiple",
  "event_strategy.stop_loss_mult": "Stop-loss multiple",
  "event_strategy.vertical_horizon": "Holding horizon (days)",
  "event_strategy.vol_window": "Volatility lookback (days)",
  "event_strategy.position_fraction": "Position size fraction",
  "event_strategy.max_concurrent": "Max concurrent positions",
  "event_strategy.peer_window_before": "Peer lookback window",
  "event_strategy.regime_mode": "Regime mode",
  "event_strategy.regime_select": "Regime selector",
  "event_strategy.peer_missingness": "Missing peer rule",
};

/** "fees.equities.default" → "Equity trading fee"; unknown keys are humanized, never blank. */
export function settingLabel(scope: string, key: string): string {
  const combined = `${scope}.${key}`;
  // `in`, not truthiness: a mapped entry could legitimately be falsy one day; the key's presence is
  // the right test, so a real mapping never silently falls through to the humanizer.
  if (combined in SETTING_LABELS) return SETTING_LABELS[combined];
  // Fallback: humanize the leaf-most token (drop the scope path), so an unmapped key still reads
  // human (e.g. "event_strategy.new_dial" → "New dial") rather than the raw scope.key.
  const words = key.replace(/[_.-]+/g, " ").trim();
  if (!words) return combined;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// ── Phase-2 Unit 3: the signal-funnel labels + glosses ───────────────────────────────────────────
// The stage labels are mono-caps, human (never the snake_case column names). The WHY-ZERO glosses are
// plain-English (the CLAUDE.md writing rule + the de-gray rule A7: the readable explanation lives in the
// open, the code-identifier bucket id rides a hover). `tone` drives the callout colour: a valid-zero
// reads calm (informational), a data/logic problem reads amber/red via the staleness grammar.

/** The seven funnel stages, in funnel order: the research_run_funnel count column → its mono-caps label. */
export const FUNNEL_STAGE_LABELS: { key: string; label: string }[] = [
  { key: "bars_total", label: "BARS" },
  { key: "bars_after_warmup", label: "AFTER WARMUP" },
  { key: "raw_entries", label: "RAW ENTRIES" },
  { key: "entries_after_dedup", label: "ENTRIES KEPT" },
  { key: "raw_exits", label: "RAW EXITS" },
  { key: "fills", label: "FILLS" },
  { key: "total_trades", label: "TRADES" },
];

/** A WHY-ZERO bucket → its plain-English gloss + a tone for the callout. "N/A" carries no callout. */
export function funnelZeroReason(reason: string): { headline: string; gloss: string; tone: "calm" | "warn" | "bad" } {
  switch (reason) {
    case "THRESHOLD-NEVER-CROSSED":
      return {
        headline: "Valid zero",
        gloss: "The rule's threshold was never crossed. The data is present, the signal just did not fire.",
        tone: "calm",
      };
    case "ENTRIES-DEDUPED-AWAY":
      return {
        headline: "Entries suppressed",
        gloss: "Entry signals fired but every one landed while a position was already open, so none opened a trade.",
        tone: "warn",
      };
    case "SIGNALS-BUT-NO-FILLS":
      return {
        headline: "Signals, no fills",
        gloss: "Entries survived but produced no trade. Check whether entries and exits collide on the same bar.",
        tone: "warn",
      };
    case "ALL-NaN-WARMUP":
      return {
        headline: "All bars in warmup",
        gloss: "Every bar fell inside the indicator warmup, so no signal could be evaluated. The series is too short.",
        tone: "bad",
      };
    case "NO-DATA":
      return {
        headline: "No data",
        gloss: "The price series was empty. This is a data problem, not a valid zero. Fetch the dataset before re-running.",
        tone: "bad",
      };
    default:
      return { headline: reason, gloss: "", tone: "warn" };
  }
}

/** A sentinel symbol like "__event_series__" is not a ticker — show a dash. */
export function displaySymbol(symbol: string | null | undefined): string {
  if (!symbol || symbol.startsWith(SENTINEL_PREFIX)) return "—";
  return symbol;
}

/** Asset-class tag for the Type pill. Tone follows the semantic map (brief §5). */
export function assetClassPill(
  symbol: string | null | undefined,
  source: string | null | undefined,
): { label: string; tone: "warn" | "cat" | "info"; isEvent: boolean } {
  if (!symbol || symbol.startsWith(SENTINEL_PREFIX)) {
    return { label: "Event", tone: "cat", isEvent: true }; // categorical → violet
  }
  const s = (source ?? "").toLowerCase();
  if (s.includes("binance") || /USDT?$/.test(symbol)) {
    return { label: "Crypto", tone: "warn", isEvent: false }; // crypto → amber
  }
  return { label: "Equity", tone: "info", isEvent: false };
}

const MONTH_YEAR: Intl.DateTimeFormatOptions = { month: "short", year: "numeric" };

/**
 * Humanize a date span: "2025-05-12 → 2026-06-05" becomes
 * { label: "May 2025 → Jun 2026", iso: "2025-05-12 → 2026-06-05",
 *   duration: "13 months" }. ISO goes in a tooltip; duration is a chip.
 */
export function formatRange(
  start: string | null | undefined,
  end: string | null | undefined,
): { label: string; iso: string; duration: string | null } {
  const a = start ? parseTimestamp(start) : null;
  const b = end ? parseTimestamp(end) : null;
  if (!a || !b) return { label: NO_DATA, iso: "", duration: null };
  const label = `${a.toLocaleDateString("en-US", MONTH_YEAR)} → ${b.toLocaleDateString("en-US", MONTH_YEAR)}`;
  const iso = `${start} → ${end}`;
  const months = Math.max(
    1,
    Math.round((b.getTime() - a.getTime()) / (1000 * 60 * 60 * 24 * 30.44)),
  );
  const duration =
    months < 12
      ? `${months} month${months === 1 ? "" : "s"}`
      : (() => {
          const y = Math.floor(months / 12);
          const m = months % 12;
          const ys = `${y} year${y === 1 ? "" : "s"}`;
          return m === 0 ? ys : `${ys} ${m} month${m === 1 ? "" : "s"}`;
        })();
  return { label, iso, duration };
}

/** Relative age of an ISO timestamp from now, in full words ("2 hours ago"). */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return NO_DATA;
  const d = parseTimestamp(iso);
  if (!d) return NO_DATA;
  return fmtAge((Date.now() - d.getTime()) / 1000);
}

// ── M5 UU: the price-universe test window (shared by the create dialog, the Data Library row + edit, and
// the workspace run-over-a-universe panel — one owner so the conversions cannot drift). The window is epoch
// seconds at UTC midnight (the backend form); an ISO "YYYY-MM-DD" is read/written as UTC.

// An ISO "YYYY-MM-DD" date -> UTC-midnight epoch seconds; "" or malformed -> null (open on that side).
export function isoToEpochUtc(iso: string): number | null {
  if (!iso) return null;
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return null;
  return Math.floor(Date.UTC(y, m - 1, d) / 1000);
}

// A UTC-midnight epoch (or null) -> an ISO "YYYY-MM-DD" date (UTC); null -> "" (an empty date).
export function epochToIsoUtc(ts?: number | null): string {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return `${d.getUTCFullYear()}-${mm}-${dd}`;
}

// The saved test window as plain English (years for compactness). Lane DX round 4 (SF1,
// code-quality pass 1): a both-NULL window used to render as "full history", which was never
// true at the point the universe is actually used, since tracker.request_draw's own R-T15 refuses
// any draw whose window carries a NULL bound (server-side, dashboard/backend/tracker.py). The
// honest reading is the one create_price_universe's own docstring now states: the window is not
// set, and the universe is refused until it is. Round 5 (SF1, code-quality pass 2): one-sided
// NULL bounds now also display a refusal notice, matching the both-NULL case, since R-T15 refuses
// any draw with ANY NULL bound, not only both.
export function formatTestWindow(startTs?: number | null, endTs?: number | null): string {
  const yr = (ts: number) => new Date(ts * 1000).getUTCFullYear();
  if (startTs == null && endTs == null) return "no window set, refused until set";
  if (startTs != null && endTs != null) return `${yr(startTs)} to ${yr(endTs)}`;
  if (startTs != null) return `from ${yr(startTs)}, missing end, refused when used`;
  return `until ${yr(endTs as number)}, missing start, refused when used`;
}
