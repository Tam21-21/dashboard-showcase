import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

// Settings are kept in this browser only. The look options and the page colours follow the mockup
// (trading_lab_new_ui.html: lab-app.js PAL and ACCENT, lab-p-settings.js LOOK).

export type AppId = "home" | "research" | "calendar" | "trading" | "portfolio";
export type BgId = "aurora" | "glass" | "glow" | "dots" | "charcoal" | "black";
export type BtnId = "glass" | "white" | "graphite";
export type AurId = "still" | "move";

export const BG_OPTIONS: ReadonlyArray<[BgId, string]> = [
  ["aurora", "Aurora"], ["glass", "Glass"], ["glow", "Glow"], ["dots", "Dots"], ["charcoal", "Charcoal"], ["black", "Black"],
];
export const BTN_OPTIONS: ReadonlyArray<[BtnId, string]> = [["glass", "Glass"], ["white", "White"], ["graphite", "Graphite"]];
export const AUR_OPTIONS: ReadonlyArray<[AurId, string]> = [["still", "Still"], ["move", "Moving"]];

// The colours a page can wear (the mockup's PAL): one for plain information on the page. Green, amber and red are not
// offered, they mean works, waits for you and broken.
export type Rgb = readonly [number, number, number];
export const PAL: ReadonlyArray<readonly [name: string, rgb: Rgb]> = [
  ["Blue", [77, 155, 255]], ["Periwinkle", [122, 162, 255]], ["Indigo", [132, 140, 250]], ["Violet", [150, 128, 255]],
  ["Purple", [190, 124, 255]], ["Orchid", [214, 118, 238]], ["Pink", [236, 110, 205]], ["Rose", [240, 108, 190]],
  ["Cyan", [40, 200, 235]], ["Teal", [46, 204, 222]],
];

// The set colour of each page, an index into PAL (the mockup's ACCENT).
export const ACCENT: Record<string, number> = {
  home: 1, sports: 6, calendar: 3, overview2: 3, markets: 9, strategies: 4, generator: 7, runs: 0, chain: 2, judge: 5,
  forward: 8, operations: 4, paper: 8, capital: 2, positions: 9, money: 3, budget: 6, settings: 1,
};

export interface PageDef {
  key: string;
  name: string;
  part: string;
}

// Every page in the rail's order: its key (the mockup's page id), its name and the app it belongs to.
export const PAGE_LIST: readonly PageDef[] = [
  { key: "home", name: "Overview", part: "Home" },
  { key: "sports", name: "Sports", part: "Home" },
  { key: "overview2", name: "Overview", part: "Research" },
  { key: "markets", name: "Data library", part: "Research" },
  { key: "strategies", name: "Strategies", part: "Research" },
  { key: "generator", name: "Generator", part: "Research" },
  { key: "runs", name: "Runs", part: "Research" },
  { key: "chain", name: "Chain", part: "Research" },
  { key: "judge", name: "Judge", part: "Research" },
  { key: "forward", name: "Forward testing", part: "Research" },
  { key: "operations", name: "Operations", part: "Research" },
  { key: "calendar", name: "Calendar", part: "Calendar" },
  { key: "paper", name: "Paper", part: "Trading" },
  { key: "capital", name: "Capital", part: "Trading" },
  { key: "positions", name: "Positions", part: "Portfolio" },
  { key: "money", name: "Money", part: "Portfolio" },
  { key: "budget", name: "Budget", part: "Portfolio" },
  { key: "settings", name: "Settings", part: "Settings" },
];

const ROUTE_KEY: Record<string, string> = {
  "home/overview": "home", "home/sports": "sports", "research/overview": "overview2", "research/data-library": "markets",
  "research/strategies": "strategies", "research/generator": "generator", "research/runs": "runs", "research/chains": "chain",
  "research/judge": "judge", "research/forward-testing": "forward", "research/operations": "operations",
  "calendar/calendar": "calendar", "trading/paper": "paper", "trading/capital": "capital",
  "portfolio/positions": "positions", "portfolio/money": "money", "portfolio/budget": "budget",
};

/** The page key of a route: "research/chains" is "chain". Settings has the one key "settings". */
export function pageKey(app: string | undefined, section: string | undefined): string {
  if (app === "settings") return "settings";
  return ROUTE_KEY[`${app}/${section}`] ?? "home";
}

export interface Settings {
  tone: "calm" | "vivid";
  rail: "auto" | "open";
  motion: "on" | "off";
  layout: "auto" | "phone" | "desktop";
  bg: BgId;
  btn: BtnId;
  aur: AurId;
  /** The pages whose colour the viewer picked: page key to an index into PAL. Pages not listed wear their set colour. */
  accent: Record<string, number>;
}

export const SETTINGS_DEFAULT: Settings = {
  tone: "vivid",
  rail: "auto",
  motion: "on",
  layout: "auto",
  bg: "aurora",
  btn: "glass",
  aur: "still",
  accent: {},
};

export const STORAGE_KEY = "v2-settings";

/** The index into PAL a page wears: the viewer's pick, else the set colour. */
export function accentIndex(key: string, s: Settings): number {
  const own = s.accent[key];
  if (own !== undefined && PAL[own] !== undefined) return own;
  return ACCENT[key] ?? 0;
}

/** The colour of a page as a name, as "#rrggbb" and as "r g b". */
export function accentOf(key: string, s: Settings): { name: string; hex: string; rgb: string } {
  const [name, c] = PAL[accentIndex(key, s)];
  return { name, hex: "#" + c.map((n) => n.toString(16).padStart(2, "0")).join(""), rgb: c.join(" ") };
}

/** The colour an app wears: its first page's colour. */
export function appAccent(app: AppId, s: Settings): string {
  const first = PAGE_LIST.find((p) => p.part.toLowerCase() === app);
  return accentOf(first === undefined ? "home" : first.key, s).hex;
}

export interface Loaded {
  settings: Settings;
  storageError: string | null;
}

function cloneDefault(): Settings {
  return { ...SETTINGS_DEFAULT, accent: {} };
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function loadSettings(): Loaded {
  const settings = cloneDefault();
  let raw: string | null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch (e) {
    return { settings, storageError: `Settings could not be read in this browser: ${message(e)}` };
  }
  if (raw === null) return { settings, storageError: null };
  let parsed: Partial<Settings>;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return { settings, storageError: `Settings could not be read in this browser: ${message(e)}` };
  }
  if (parsed.tone === "calm" || parsed.tone === "vivid") settings.tone = parsed.tone;
  if (parsed.rail === "auto" || parsed.rail === "open") settings.rail = parsed.rail;
  if (parsed.motion === "on" || parsed.motion === "off") settings.motion = parsed.motion;
  if (parsed.layout === "auto" || parsed.layout === "phone" || parsed.layout === "desktop") settings.layout = parsed.layout;
  const bg = BG_OPTIONS.find((o) => o[0] === parsed.bg);
  if (bg !== undefined) settings.bg = bg[0];
  const btn = BTN_OPTIONS.find((o) => o[0] === parsed.btn);
  if (btn !== undefined) settings.btn = btn[0];
  if (parsed.aur === "still" || parsed.aur === "move") settings.aur = parsed.aur;
  const bad: string[] = [];
  for (const [key, i] of Object.entries(parsed.accent ?? {})) {
    if (PAGE_LIST.some((p) => p.key === key) && Number.isInteger(i) && PAL[i] !== undefined) settings.accent[key] = i;
    else bad.push(`${key}: ${String(i)}`);
  }
  const storageError = bad.length
    ? `Settings could not be read in this browser: unknown page colour (${bad.join(", ")}), using the set colour for that page`
    : null;
  return { settings, storageError };
}

/** Returns null when saved, or a sentence naming the failure. */
export function saveSettings(s: Settings): string | null {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
    return null;
  } catch (e) {
    return `Settings could not be saved in this browser: ${message(e)}`;
  }
}

interface Ctx {
  settings: Settings;
  setSettings: (s: Settings) => void;
  storageError: string | null;
}

const SettingsContext = createContext<Ctx | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Loaded>(() => loadSettings());
  const setSettings = useCallback((s: Settings) => {
    setState({ settings: s, storageError: saveSettings(s) });
  }, []);
  const value = useMemo(
    () => ({ settings: state.settings, setSettings, storageError: state.storageError }),
    [state, setSettings],
  );
  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): Ctx {
  const c = useContext(SettingsContext);
  if (c === null) throw new Error("useSettings outside SettingsProvider");
  return c;
}
