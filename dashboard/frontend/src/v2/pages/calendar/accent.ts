// Calendar colours and the weather kind, moved unchanged from the old calendar page.
export const KIT_ACCENTS = [
  "#3ddbb4", // eq · teal
  "#ffb000", // amb · amber
  "#9d7bff", // vio · violet
  "#36d6ea", // cyn · cyan
  "#4d8df5", // evt · blue
  "#ef5ba6", // fnd · pink
  "#29e07c", // grn · green
] as const;
const DEFAULT_ACCENT = "#4d8df5"; // evt blue

// Google's 11 colour ids mapped to the nearest kit accent by hue (no red).
const GCAL_TO_ACCENT: Record<string, string> = {
  "1": "#ffb000",  // tomato → amber
  "2": "#ef5ba6",  // flamingo → pink
  "3": "#ffb000",  // tangerine → amber
  "4": "#ffb000",  // banana → amber
  "5": "#29e07c",  // sage → green
  "6": "#3ddbb4",  // basil → teal
  "7": "#3ddbb4",  // peacock → teal
  "8": "#4d8df5",  // blueberry → blue
  "9": "#9d7bff",  // lavender → violet
  "10": "#9d7bff", // grape → violet
  "11": "#36d6ea", // graphite → cyan
};

// Deterministic accent for an arbitrary stored colour string (legacy template hexes
// etc.) — stable per value, never red.
function hashAccent(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return KIT_ACCENTS[h % KIT_ACCENTS.length];
}

export function toAccent(raw: string | null | undefined): string {
  if (!raw) return DEFAULT_ACCENT;
  if (/^\d+$/.test(raw)) return GCAL_TO_ACCENT[raw] ?? DEFAULT_ACCENT; // Google color_id
  const lower = raw.toLowerCase();
  if ((KIT_ACCENTS as readonly string[]).includes(lower)) return lower; // already a kit accent
  return hashAccent(raw); // legacy hex or unknown → stable kit accent
}

export type WxKind = "sun" | "part" | "cloud" | "rain" | "snow";
export function owmKind(icon: string | undefined): WxKind {
  const code = (icon ?? "").slice(0, 2);
  switch (code) {
    case "01": return "sun";
    case "02": return "part";
    case "03":
    case "04": return "cloud";
    case "09":
    case "10":
    case "11": return "rain";
    case "13": return "snow";
    default:   return "cloud"; // 50 (mist) + anything unmapped
  }
}

// The kinds of calendar event, each with its colour (the mockup's own list, shared with Home). An event with a colour picked in
// Google keeps it; one without takes its colour from its name: CM is a lecture and TD a tutorial in the university's timetable,
// a name that ends in a room number is a class.
export const KINDS = [
  { key: "lecture", name: "Lecture", hex: "#4d9bff" },
  { key: "tutorial", name: "Tutorial", hex: "#ec6ecd" },
  { key: "class", name: "Class", hex: "#9680ff" },
  { key: "run", name: "Run", hex: "#ff926e" },
  { key: "swim", name: "Swim", hex: "#28c8eb" },
  { key: "football", name: "Football", hex: "#bee15f" },
  { key: "other", name: "Other", hex: "#96a0b4" },
] as const;
export type Kind = (typeof KINDS)[number];
export function kindOf(title: string): Kind {
  const key = /^CM\b/.test(title) ? "lecture" : /^TD\b/.test(title) ? "tutorial" : /\brun\b/i.test(title) ? "run" : /\bswim/i.test(title) ? "swim"
    : /\bfootball\b/i.test(title) ? "football" : /\s-\s[A-Z]\d+\s*$/.test(title) ? "class" : "other";
  return KINDS.find((k) => k.key === key) as Kind;
}
export function eventAccent(e: { title: string; color_id: string | null }): string {
  return e.color_id ? toAccent(e.color_id) : kindOf(e.title).hex;
}
