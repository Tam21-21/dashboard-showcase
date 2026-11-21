import { useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { api, ApiError, type ConnectionsLive } from "../../lib/api";
import { installStore, type InstallState, type InstallStore } from "../install";
import { Block, PageHeader, Say, Segmented, State, Window, type NlTone, type Tone } from "../kit";
import {
  AUR_OPTIONS, BG_OPTIONS, BTN_OPTIONS, PAGE_LIST, PAL, accentIndex, accentOf, useSettings,
  type Settings as SettingsShape,
} from "../settings";
import Evening from "./settings/Evening";
import "./settings.css";

type Check<T> = { kind: "loading" } | { kind: "ok"; value: T } | { kind: "error"; words: string };

// The server's own words when it wrote some, else the error's message, else a plain sentence.
function errorWords(e: unknown): string {
  if (e instanceof ApiError) {
    const b = e.body as { detail?: unknown } | null;
    if (b !== null && typeof b === "object" && typeof b.detail === "string" && b.detail !== "") return b.detail;
    return "The server could not answer.";
  }
  if (e instanceof Error && e.message !== "") return e.message;
  return "The check could not run.";
}

function useCheck<T>(call: () => Promise<T>): Check<T> {
  const [state, setState] = useState<Check<T>>({ kind: "loading" });
  useEffect(() => {
    let live = true;
    call().then(
      (value) => { if (live) setState({ kind: "ok", value }); },
      (e: unknown) => { if (live) setState({ kind: "error", words: errorWords(e) }); },
    );
    return () => { live = false; };
    // The call is a fixed api function, so the check runs once on mount.
  }, []);
  return state;
}

// The page's old tone letters, read as the new state word colours.
const NL: Record<Tone, NlTone> = { g: "good", rd: "bad", a: "warn", bl: "run", none: "dim" };
const word = (text: string, tone: Tone, hover?: string) => <State tone={NL[tone]} hover={hover}>{text}</State>;
const checking = () => <span title="Checking">-</span>;

type CalendarStatus = { configured: boolean; connected: boolean };

// One service as a tile: its state word, one figure with its unit, and what else the last read says.
interface Conn {
  name: string;
  state: ReactNode;
  fig: string;
  unit: string;
  facts: Array<[string, ReactNode]>;
  hue: "good" | "warn" | "bad" | "dim";
  action?: ReactNode;
}

function calendarState(c: Check<CalendarStatus>, problem: string | null): { state: ReactNode; hue: Conn["hue"] } {
  if (c.kind === "loading") return { state: checking(), hue: "dim" };
  if (c.kind === "error") return { state: word(c.words, "rd"), hue: "bad" };
  if (problem !== null) return { state: word(problem, "rd"), hue: "bad" };
  if (c.value.connected) return { state: word("Connected", "g"), hue: "good" };
  if (!c.value.configured) {
    return { state: <span title="Google sign-in is not set up on the server">{word("Not connected", "none")}</span>, hue: "dim" };
  }
  return { state: word("Set up, not connected", "a"), hue: "warn" };
}

// The Garmin sync timer runs hourly, so no sync for 2 hours means it has stopped (lane UI32, the
// planning session's default of 2026-10-16, told to Tamer with no objection).
const GARMIN_STALE_MS = 2 * 60 * 60 * 1000;
// EODHD counts calls against a daily limit; at 80 percent of it the state turns amber (same default).
const CALLS_AMBER_SHARE = 0.8;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const PARIS = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Paris",
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

// A moment as Paris shows it: the calendar day (YYYY-MM-DD), "D Mon" and "HH:MM".
function parisParts(d: Date): { ymd: string; dayMon: string; hhmm: string } {
  const p: Record<string, string> = {};
  for (const x of PARIS.formatToParts(d)) p[x.type] = x.value;
  const month = Number(p.month);
  return {
    ymd: `${p.year}-${String(month).padStart(2, "0")}-${String(Number(p.day)).padStart(2, "0")}`,
    dayMon: `${Number(p.day)} ${MONTHS[month - 1]}`,
    hhmm: `${p.hour}:${p.minute}`,
  };
}

// "2026-10-14" as "14 Oct" without a time zone: it is a calendar day, not a moment.
function dayMonOf(ymd: string): string {
  const [, m, d] = ymd.split("-");
  return `${Number(d)} ${MONTHS[Number(m) - 1]}`;
}

function checkedWords(checkedAt: string): string {
  const p = parisParts(new Date(checkedAt));
  return `Checked ${p.dayMon} ${p.hhmm}`;
}

function yesterdayParis(now: Date): string {
  const [y, m, d] = parisParts(now).ymd.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d - 1)).toISOString().slice(0, 10);
}

const grouped = (n: number) => n.toLocaleString("en-US");

function liveRead(c: Check<ConnectionsLive>, whenRead: (v: ConnectionsLive) => Omit<Conn, "name">): Omit<Conn, "name"> {
  if (c.kind === "loading") return { state: checking(), fig: "-", unit: "", facts: [], hue: "dim" };
  if (c.kind === "error") return { state: word(c.words, "rd"), fig: "-", unit: "", facts: [], hue: "bad" };
  return whenRead(c.value);
}

function garminRead(c: Check<ConnectionsLive>, now: Date): Omit<Conn, "name"> {
  return liveRead(c, (v) => {
    const g = v.garmin;
    if (!g.configured) {
      return { state: <span title="Garmin's sign-in is not set on the server">{word("Not set up", "none")}</span>, fig: "-", unit: "", facts: [], hue: "dim" };
    }
    const source = `Source: the Garmin sync on the server. ${checkedWords(v.checked_at)}.`;
    if (g.error) return { state: <span title={source}>{word(g.error, "rd")}</span>, fig: "-", unit: "", facts: [], hue: "bad" };
    const gaps = g.last_sync_error ? ` Gaps: ${g.last_sync_error}` : "";
    let state: ReactNode;
    let hue: Conn["hue"];
    let fig = "-";
    if (g.last_sync_at === null) {
      state = word("Never synced", "rd");
      hue = "bad";
    } else {
      const at = new Date(g.last_sync_at);
      const p = parisParts(at);
      fig = p.hhmm;
      if (now.getTime() - at.getTime() > GARMIN_STALE_MS) {
        state = word(`No sync since ${p.dayMon} ${p.hhmm}`, "rd");
        hue = "bad";
      } else if (g.last_sync_error) {
        state = word(`Synced ${p.hhmm}, with gaps`, "a");
        hue = "warn";
      } else {
        state = word(`Synced ${p.hhmm}`, "g");
        hue = "good";
      }
    }
    let watch: ReactNode;
    if (g.watch_data_until === null) watch = "No watch data yet";
    else if (g.watch_data_until < yesterdayParis(now)) {
      watch = word(`Watch data up to ${dayMonOf(g.watch_data_until)}`, "a");
      if (hue === "good") hue = "warn";
    } else watch = `Watch data up to ${dayMonOf(g.watch_data_until)}`;
    return {
      state: <span title={source + gaps}>{state}</span>,
      fig,
      unit: g.last_sync_at === null ? "" : "last sync",
      facts: [["Watch", watch]],
      hue,
    };
  });
}

// Alpaca's account status in everyday words: ACCOUNT_BLOCKED reads "Account blocked".
function statusWords(status: string): string {
  const s = status.toLowerCase().replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function alpacaRead(c: Check<ConnectionsLive>): Omit<Conn, "name"> {
  return liveRead(c, (v) => {
    const a = v.alpaca;
    if (!a.configured) {
      return { state: <span title="The Alpaca keys are not set on the server">{word("Not set up", "none")}</span>, fig: "0", unit: "key set", facts: [], hue: "dim" };
    }
    const source = `Source: the Alpaca paper account. ${checkedWords(v.checked_at)}.`;
    const base = { fig: "1", unit: "key set", facts: [["Money", "Pretend only"]] as Array<[string, ReactNode]> };
    if (a.error) return { ...base, state: <span title={source}>{word(a.error, "rd")}</span>, hue: "bad" };
    if (a.status === null) return { ...base, state: <span title={`${source} Alpaca gave no account status.`}>-</span>, hue: "dim" };
    const hover = `${source} Status: ${a.status}.`;
    if (a.status !== "ACTIVE") return { ...base, state: <span title={hover}>{word(statusWords(a.status), "a")}</span>, hue: "warn" };
    if (a.trading_blocked) return { ...base, state: <span title={hover}>{word("Trading blocked, paper", "a")}</span>, hue: "warn" };
    return { ...base, state: <span title={hover}>{word("Active, paper", "g")}</span>, hue: "good" };
  });
}

function eodhdRead(c: Check<ConnectionsLive>): Omit<Conn, "name"> {
  return liveRead(c, (v) => {
    const e = v.eodhd;
    if (!e.configured) {
      return { state: <span title="The EODHD key is not set on the server">{word("Not set up", "none")}</span>, fig: "0", unit: "key set", facts: [], hue: "dim" };
    }
    const source = `Source: the EODHD account. ${checkedWords(v.checked_at)}.`;
    if (e.error) return { state: <span title={source}>{word(e.error, "rd")}</span>, fig: "1", unit: "key set", facts: [], hue: "bad" };
    if (e.requests_today === null || e.daily_limit === null) {
      return { state: <span title={`${source} EODHD gave no call count.`}>-</span>, fig: "1", unit: "key set", facts: [], hue: "dim" };
    }
    const busy = e.requests_today >= e.daily_limit * CALLS_AMBER_SHARE;
    return {
      state: <span title={source}>{word(`${grouped(e.requests_today)} of ${grouped(e.daily_limit)} calls today`, busy ? "a" : "g")}</span>,
      fig: grouped(e.requests_today),
      unit: "calls today",
      facts: [["Daily limit", grouped(e.daily_limit)]],
      hue: busy ? "warn" : "good",
    };
  });
}

function weatherRead(c: Check<unknown>): Omit<Conn, "name"> {
  if (c.kind === "loading") return { state: checking(), fig: "-", unit: "", facts: [["Used on", "Calendar"]], hue: "dim" };
  if (c.kind === "error") return { state: word(c.words, "rd"), fig: "-", unit: "", facts: [["Used on", "Calendar"]], hue: "bad" };
  return { state: word("Working", "g"), fig: "-", unit: "", facts: [["Used on", "Calendar"]], hue: "good" };
}

const NOT_OFFERED_HOVER =
  "This browser has not offered to install the app on this page. In Chrome or Edge, look for Install in the browser menu. " +
  "A browser does not offer it once the app is installed: open it from its icon.";

function appState(state: InstallState, problem: string | null): ReactNode {
  if (problem !== null) return <span title={problem}>{word("The install window did not open", "rd")}</span>;
  if (state === "installed") return word("Installed", "g");
  if (state === "ready") return word("Not installed", "none");
  if (state === "iphone") return word("Share, then add to home screen", "bl");
  return <span title={NOT_OFFERED_HOVER}>{word("Not offered here", "a")}</span>;
}

export function AppPanel({ store }: { store: InstallStore }) {
  const state = useSyncExternalStore(store.subscribe, store.state);
  const [problem, setProblem] = useState<string | null>(null);
  const install = () => {
    setProblem(null);
    store.install().catch((e: unknown) => setProblem(e instanceof Error ? e.message : String(e)));
  };
  return (
    <Block title="App">
      <table className="st-tbl st-app">
        <thead>
          <tr><th /><th>State</th><th /></tr>
        </thead>
        <tbody>
          <tr>
            <td>Installed app</td>
            <td>{appState(state, problem)}</td>
            <td>{state === "ready" ? <button type="button" className="btn" onClick={install}>Install</button> : null}</td>
          </tr>
        </tbody>
      </table>
    </Block>
  );
}

function Swatch({ rgb }: { rgb: string }) {
  return <i className="st-sw" style={{ background: `rgb(${rgb})` }} />;
}

// What the Say line reads about the watch: the Garmin key's last answer, in the amber or blue of the mockup.
function garminSentence(keys: Check<ConnectionsLive>, now: Date): ReactNode {
  if (keys.kind !== "ok") return null;
  const g = keys.value.garmin;
  if (!g.configured) return <em className="run">The Garmin key is not set on the server</em>;
  if (g.watch_data_until !== null && g.watch_data_until < yesterdayParis(now)) {
    return <em className="warn">Garmin answers your key, and the watch has sent nothing since {dayMonOf(g.watch_data_until)}</em>;
  }
  return <em className="run">Garmin answers your key</em>;
}

export default function Settings() {
  const { settings, setSettings, storageError } = useSettings();
  const [calendar, setCalendar] = useState<Check<CalendarStatus>>({ kind: "loading" });
  const [problem, setProblem] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const [picking, setPicking] = useState<string | null>(null);
  const readCalendar = () =>
    api.life.calendarStatus().then(
      (value) => setCalendar({ kind: "ok", value }),
      (e: unknown) => setCalendar({ kind: "error", words: errorWords(e) }),
    );
  useEffect(() => {
    void readCalendar();
    // The call is a fixed api function, so the status is read once on mount and after a disconnect.
  }, []);
  const keys = useCheck(api.settings.connectionsLive);
  const weather = useCheck(api.life.weather);

  const connect = () => {
    setProblem(null);
    api.life.googleAuthUrl().then(
      (r) => { window.location.href = r.auth_url; },
      (e: unknown) => setProblem(errorWords(e)),
    );
  };
  const disconnect = () => {
    setAsking(false);
    setProblem(null);
    api.life.googleDisconnect().then(
      () => readCalendar(),
      (e: unknown) => setProblem(errorWords(e)),
    );
  };
  let googleAction: ReactNode = null;
  if (calendar.kind === "ok" && calendar.value.connected) {
    googleAction = <button type="button" className="btn danger" onClick={() => setAsking(true)}>Disconnect</button>;
  } else if (calendar.kind === "ok" && calendar.value.configured) {
    googleAction = <button type="button" className="btn" onClick={connect}>Connect</button>;
  }

  const now = new Date();
  const set = (patch: Partial<SettingsShape>) => setSettings({ ...settings, ...patch });
  const setAccent = (key: string, index: number | null) => {
    const accent = { ...settings.accent };
    if (index === null) delete accent[key];
    else accent[key] = index;
    setSettings({ ...settings, accent });
  };
  const mine = Object.keys(settings.accent).length;

  const cal = calendarState(calendar, problem);
  const conns: Conn[] = [
    { name: "Garmin", ...garminRead(keys, now) },
    { name: "Alpaca paper", ...alpacaRead(keys) },
    { name: "EODHD prices", ...eodhdRead(keys) },
    {
      name: "Google calendar", state: cal.state, hue: cal.hue,
      fig: calendar.kind === "ok" ? (calendar.value.connected ? "1" : "0") : "-", unit: "account connected", facts: [],
      action: googleAction,
    },
    { name: "Weather", ...weatherRead(weather) },
  ];

  const field = (label: string, control: ReactNode) => (
    <div className="st-field"><span>{label}</span>{control}</div>
  );
  const seg = <V extends string>(value: V, options: ReadonlyArray<[V, string]>, onChange: (v: V) => void) => (
    <Segmented
      variant="seg"
      value={value}
      onChange={(v) => onChange(v as V)}
      options={options.map(([v, label]) => ({ label, value: v }))}
    />
  );
  const pickedPage = PAGE_LIST.find((p) => p.key === picking);

  return (
    <div className="v2-page">
      <PageHeader crumb="Settings" title="Settings" gear={false} />
      <Say
        lead="Your look and your connections."
        more={<>Each choice applies at once and is kept in this browser. {garminSentence(keys, now)}</>}
      />
      {storageError !== null && <p className="set-warn">{storageError}</p>}

      <Block
        className="st-sec first"
        id="stCol"
        title="Page colours"
        state={<State tone="run">{mine === 0 ? "All as set" : `${mine} of your own`}</State>}
        right={
          <button type="button" className="btn" onClick={() => set({ accent: {} })}>Back to the set colours</button>
        }
      >
        <p className="st-note">
          Each page has one colour for plain information. Green, amber and red are not offered here: they mean works, waits for you and broken on every page.
        </p>
        <table className="st-tbl st-pages">
          <thead>
            <tr><th>Page</th><th>Part of</th><th>Colour</th><th /></tr>
          </thead>
          <tbody>
            {PAGE_LIST.map((p) => {
              const c = accentOf(p.key, settings);
              return (
                <tr key={p.key} onClick={() => setPicking(p.key)} data-page-row={p.key}>
                  <td>
                    <button type="button" className="st-link" aria-label={`Colour for ${p.part} ${p.name}`} onClick={(e) => { e.stopPropagation(); setPicking(p.key); }}>
                      {p.name}
                    </button>
                  </td>
                  <td>{p.part}</td>
                  <td><span className="st-swn"><Swatch rgb={c.rgb} />{c.name}</span></td>
                  <td>{settings.accent[p.key] !== undefined && <State tone="run">Your pick</State>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Block>

      <Block className="st-sec" id="stLook" title="Look">
        <div className="st-form">
          {field("Background", seg(settings.bg, BG_OPTIONS, (bg) => set({ bg })))}
          {field("Buttons", seg(settings.btn, BTN_OPTIONS, (btn) => set({ btn })))}
          {field("Aurora lights", seg(settings.aur, AUR_OPTIONS, (aur) => set({ aur })))}
          {field("Motion", seg(settings.motion, [["on", "Full"], ["off", "Calm"]], (motion) => set({ motion })))}
          {field("Text", seg(settings.tone, [["calm", "Calm"], ["vivid", "Vivid"]], (tone) => set({ tone })))}
          {field("Side rail", seg(settings.rail, [["auto", "Closes when you click the page"], ["open", "Always open"]], (rail) => set({ rail })))}
          {field("Layout", seg(settings.layout, [["auto", "Automatic"], ["phone", "Phone"], ["desktop", "Desktop"]], (layout) => set({ layout })))}
        </div>
      </Block>

      <Evening />

      <Block className="st-sec" id="stConn" title="Connections" state={<State tone="run">{conns.length} services</State>}>
        <div className="st-wall">
          {conns.map((c) => (
            <div className={`st-tile h-${c.hue}`} key={c.name} data-conn={c.name}>
              <div className="st-top">
                <span className="st-name">{c.name}</span>
                <span className="st-state">{c.state}</span>
              </div>
              <div className="st-fig"><b className="num">{c.fig}</b><span>{c.unit}</span></div>
              {c.facts.length > 0 && (
                <div className="st-facts">
                  {c.facts.map(([k, v]) => (
                    <div key={k}><i>{k}</i><span>{v}</span></div>
                  ))}
                </div>
              )}
              {c.action != null && <div className="st-act">{c.action}</div>}
              {c.name === "Google calendar" && asking && (
                <div className="conf">
                  <span>Disconnect Google calendar?</span>
                  <button type="button" className="btn q" onClick={() => setAsking(false)}>Cancel</button>
                  <button type="button" className="btn danger" onClick={disconnect}>Disconnect</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </Block>

      <div className="st-sec"><AppPanel store={installStore} /></div>

      {pickedPage !== undefined && (
        <Window label={`Colour for ${pickedPage.part} ${pickedPage.name}`} size="m" onClose={() => setPicking(null)}>
          <h2 className="st-wt">Colour for {pickedPage.part}, {pickedPage.name}</h2>
          <p className="st-note">The colour of charts, counts and plain chips on this page.</p>
          <div className="st-sws">
            {PAL.map(([name, rgb], i) => (
              <button
                type="button"
                key={name}
                data-colour={name}
                className={`btn${accentIndex(pickedPage.key, settings) === i ? " pressed" : ""}`}
                aria-pressed={accentIndex(pickedPage.key, settings) === i}
                onClick={() => { setAccent(pickedPage.key, i); setPicking(null); }}
              >
                <Swatch rgb={rgb.join(" ")} />{name}
              </button>
            ))}
          </div>
          <div className="st-acts">
            <button type="button" className="btn" onClick={() => { setAccent(pickedPage.key, null); setPicking(null); }}>Back to the set colour</button>
          </div>
        </Window>
      )}
    </div>
  );
}
