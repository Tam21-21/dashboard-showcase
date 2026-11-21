import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createPortal } from "react-dom";
import { Block, DatePick, Say, Segmented, Window } from "../../../kit";
import type { Tone } from "../../../kit";
import { api } from "../../../../lib/api";
import { dateToEpoch } from "../../../../lib/format";
import { serverDetail } from "../../../../lib/errorText";
import { strategyLabel, useStrategyNames } from "../../../data/strategyNames";
import type { Dataset, PriceUniverseMemberDetailRow, Run, RunStatus } from "../../../../types/research";
import {
  AXIS_START, FIRST_YEAR, axisPercent, barsSum, bigNumber, dayLong, dayShort, hasPrices, splitTickers, utcYear,
  windowText, yearCountsBySector, yearFraction,
} from "./years";
import { bySector, orderSectors, sectorInfo, sectorKey } from "./sectors";
import type { SectorCount } from "./sectors";
import SweepWindow from "../sweep/SweepWindow";
import { LibCard } from "../markets/LibCard";
import "./universes.css";
import { PhoneOff } from "../../../kit/phone";

// The Universes tab of the Data library (mockup: docs/design/rail_mockup_2026-10-10.html, dlUniv, newUniverse
// and dlUnivAfter). The sector (SEC sector office) is the page's one colour key: the list's sector bars, the
// year area, BY SECTOR and the members table. Left out because the server has nothing behind them: a member
// opening a company chart and the Screen mode of NEW.

const SHOWN_UNIVERSES = 5;
const SHOWN_MEMBERS = 30;
const TOAST_MS = 6000;
const FIRST_DAY = "2010-01-01";
const AREA_W = 1000;
const AREA_H = 200;

const keys = {
  list: ["universes", "list"] as const,
  detail: (id: number) => ["universes", "detail", id] as const,
  runs: (id: number) => ["universes", "runs", id] as const,
  mix: (showTest: boolean) => ["universes", "mix", showTest] as const,
};

const count = (n: number) => n.toLocaleString("en-US");
const todayIso = () => new Date().toISOString().slice(0, 10);
const isoOf = (epoch: number | null | undefined) => (epoch == null ? "" : new Date(epoch * 1000).toISOString().slice(0, 10));

type Toast = { text: string; undo?: () => void };

const RUN_WORD: Record<RunStatus, { word: string; tone: Tone; colour: string; text: string; pill: string }> = {
  succeeded: { word: "DONE", tone: "g", colour: "#2fe08a", text: "Done", pill: "good" },
  failed: { word: "FAILED", tone: "rd", colour: "#ff5a63", text: "Failed", pill: "bad" },
  pending: { word: "QUEUED", tone: "bl", colour: "#4d9bff", text: "Queued", pill: "run" },
  running: { word: "RUNNING", tone: "bl", colour: "#4d9bff", text: "Running", pill: "run" },
  cancelled: { word: "CANCELLED", tone: "none", colour: "#8d98a8", text: "Cancelled", pill: "dim" },
};

const dash = (why: string): ReactNode => <span title={why}>-</span>;

// The window as the mockup's card says it: "2010 to 2026", or "every day there is" with no window.
const yearsText = (start: number | null | undefined, end: number | null | undefined): string =>
  start == null || end == null ? "every day there is" : `${utcYear(start)} to ${utcYear(end)}`;

// The stored name of a universe is made from the display name the user typed: lower case, every run of
// characters other than a-z and 0-9 one underscore, none at the ends.
export const storedNameOf = (display: string): string => display.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

const NO_MEMBERS = "This universe has no members today";
const NO_COUNT = "No sector count is held for this universe";
// The name a page shows for a universe: its display name, or these words in a sentence.
const named = (d: Dataset): string => d.display_name ?? "this universe";

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <Window label={title} size="m" className="un-dlg" onClose={onClose}>
        <div className="h2"><span>{title}</span></div>
        {children}
    </Window>
  );
}

function Foot({ children }: { children: ReactNode }) {
  return <div className="un-foot">{children}</div>;
}

function Notes({ text }: { text: string | null }) {
  return text ? <div className="un-notes" role="alert">{text}</div> : null;
}

// ADD MEMBERS: a ticker box. A refusal stays in the dialog in the server's words.
function AddMembers({ u, onClose, onDone }: { u: Dataset; onClose: () => void; onDone: (text: string) => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function add() {
    const tickers = splitTickers(typed);
    if (tickers.length === 0) {
      setNote("Type a ticker.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const res = await api.research.addPriceUniverseMembers(u.id, tickers);
      const missed = res.excluded.length > 0 ? `, not found: ${res.excluded.join(" ")}` : "";
      onDone(`Added ${res.added.length} to ${named(u)}${missed}`);
      onClose();
    } catch (e) {
      setNote(serverDetail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Add members to ${named(u)}`} onClose={onClose}>
      <div className="un-row">
        <span>Tickers</span>
        <input aria-label="Tickers" placeholder="AAPL MSFT NVDA" value={typed} onChange={(e) => setTyped(e.target.value)} />
      </div>
      <Notes text={note} />
      <Foot>
        <button type="button" className="btn q" onClick={onClose}>Cancel</button>
        <button type="button" className="btn" disabled={busy} onClick={() => void add()}>Add</button>
      </Foot>
    </Dialog>
  );
}

// EDIT: the display name and the window. Only what changed is sent; the stored name never changes; the window goes with both bounds.
function EditUniverse({ u, onClose, onDone }: { u: Dataset; onClose: () => void; onDone: (text: string) => void }) {
  const [display, setDisplay] = useState(u.display_name ?? "");
  const [from, setFrom] = useState(isoOf(u.window_start_ts));
  const [to, setTo] = useState(isoOf(u.window_end_ts));
  const [open, setOpen] = useState<"from" | "to" | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function save() {
    const newDisplay = display.trim();
    const start = dateToEpoch(from);
    const end = dateToEpoch(to);
    if (newDisplay === "" && u.display_name != null) {
      setNote("Type a display name.");
      return;
    }
    const windowChanged = start !== (u.window_start_ts ?? null) || end !== (u.window_end_ts ?? null);
    if (windowChanged && (start == null || end == null)) {
      setNote("Choose both dates.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      if (newDisplay !== "" && newDisplay !== (u.display_name ?? "")) await api.research.setPriceUniverseDisplayName(u.id, newDisplay);
      if (windowChanged) await api.research.updatePriceUniverseWindow(u.id, start, end);
      onDone("Saved");
      onClose();
    } catch (e) {
      setNote(serverDetail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Edit ${named(u)}`} onClose={onClose}>
      <div className="un-row">
        <span>Display name</span>
        <input aria-label="Display name" value={display} onChange={(e) => setDisplay(e.target.value)} />
      </div>
      <div className="un-row">
        <span>Window</span>
        <DatePick value={from} onChange={setFrom} ariaLabel="From" open={open === "from"} onOpenChange={(o) => setOpen(o ? "from" : null)} />
        <span className="un-to">to</span>
        <DatePick value={to} onChange={setTo} ariaLabel="To" open={open === "to"} onOpenChange={(o) => setOpen(o ? "to" : null)} />
      </div>
      <Notes text={note} />
      <Foot>
        <button type="button" className="btn q" onClick={onClose}>Cancel</button>
        <button type="button" className="btn" disabled={busy} onClick={() => void save()}>Save</button>
      </Foot>
    </Dialog>
  );
}

// NEW: a name, the members (typed, or a copy of another universe) and the window, which the server requires.
function NewUniverse({
  pool, onClose, onMade,
}: { pool: Dataset[]; onClose: () => void; onMade: (id: number, text: string) => void }) {
  const [display, setDisplay] = useState("");
  const [badDisplay, setBadDisplay] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [mode, setMode] = useState("type");
  const [typed, setTyped] = useState("");
  const [copy, setCopy] = useState(pool.length > 0 ? String(pool[0].id) : "");
  const [from, setFrom] = useState(FIRST_DAY);
  const [to, setTo] = useState(todayIso);
  const [open, setOpen] = useState<"from" | "to" | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function create() {
    const shown = display.trim();
    if (shown === "") {
      setBadDisplay(true);
      return;
    }
    const title = storedNameOf(shown);
    setRefusal(null);
    const start = dateToEpoch(from);
    const end = dateToEpoch(to);
    if (start == null || end == null) {
      setNote("Choose both dates.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      let tickers: string[];
      if (mode === "copy") {
        if (copy === "") {
          setNote("There is no universe to copy.");
          return;
        }
        tickers = (await api.research.priceUniverseMembers(Number(copy))).members;
      } else {
        tickers = splitTickers(typed);
      }
      if (tickers.length === 0) {
        setNote("Type a ticker.");
        return;
      }
      const res = await api.research.createPriceUniverse(title, tickers, start, end, shown);
      const missed = res.excluded.length > 0 ? `, not found: ${res.excluded.join(" ")}` : "";
      onMade(res.universe_id, `Made ${shown}, ${res.member_count} members${missed}`);
      onClose();
    } catch (e) {
      setRefusal(serverDetail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title="New universe" onClose={onClose}>
      <div className="un-row">
        <span>Display name</span>
        <input
          aria-label="Display name"
          className={badDisplay ? "bad" : undefined}
          placeholder="Display name"
          value={display}
          onChange={(e) => { setDisplay(e.target.value); setBadDisplay(false); setRefusal(null); }}
        />
      </div>
      <Notes text={refusal} />
      <div className="un-row">
        <span>From</span>
        <Segmented options={[{ label: "Tickers I type", value: "type" }, { label: "A copy", value: "copy" }]} value={mode} onChange={setMode} />
      </div>
      {mode === "type" ? (
        <div className="un-row">
          <span>Tickers</span>
          <input aria-label="Tickers" placeholder="AAPL MSFT NVDA" value={typed} onChange={(e) => setTyped(e.target.value)} />
        </div>
      ) : (
        <div className="un-row">
          <span>Copy of</span>
          <select aria-label="Copy of" className="fin" value={copy} onChange={(e) => setCopy(e.target.value)}>
            {pool.map((p) => <option key={p.id} value={String(p.id)}>{p.display_name ?? "No display name"}, {p.member_count ?? 0} members</option>)}
          </select>
        </div>
      )}
      <div className="un-row">
        <span>Window</span>
        <DatePick value={from} onChange={setFrom} ariaLabel="Window from" open={open === "from"} onOpenChange={(o) => setOpen(o ? "from" : null)} />
        <span className="un-to">to</span>
        <DatePick value={to} onChange={setTo} ariaLabel="Window to" open={open === "to"} onOpenChange={(o) => setOpen(o ? "to" : null)} />
      </div>
      <Notes text={note} />
      <Foot>
        <button type="button" className="btn q" onClick={onClose}>Cancel</button>
        <button type="button" className="btn" disabled={busy} onClick={() => void create()}>Create</button>
      </Foot>
    </Dialog>
  );
}

function DeleteUniverse({
  u, runs, onClose, onDone,
}: { u: Dataset; runs: number | null; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const text = runs == null ? "Its runs keep their results." : runs === 1 ? "Its 1 run keeps its results." : `Its ${runs} runs keep their results.`;

  async function go() {
    setBusy(true);
    setNote(null);
    try {
      await api.research.deletePriceUniverse(u.id);
      onDone();
      onClose();
    } catch (e) {
      setNote(serverDetail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog title={`Delete ${named(u)}?`} onClose={onClose}>
      <div className="un-ctext">{text}</div>
      <Notes text={note} />
      <Foot>
        <button type="button" className="btn q" onClick={onClose}>Cancel</button>
        <button type="button" className="btn danger" disabled={busy} onClick={() => void go()}>Delete</button>
      </Foot>
    </Dialog>
  );
}

// Members with daily prices in each year, as one area with a layer per sector (the legend's order, one colour
// each); the universe's window is the dashed box.
function YearArea({
  members, u, nowYear, sectors,
}: { members: PriceUniverseMemberDetailRow[]; u: Dataset; nowYear: number; sectors: SectorCount[] }) {
  const keys = sectors.map((c) => c.key);
  const years = yearCountsBySector(members, nowYear, keys, (m) => sectorKey(m.sector));
  const total = members.length;
  const top = Math.max(1, ...years.map((y) => y.n));
  const span = nowYear - FIRST_YEAR;
  const x = (year: number) => ((year - FIRST_YEAR) / span) * AREA_W;
  const y = (n: number) => AREA_H - (n / top) * (AREA_H - 12);
  const cell = AREA_W / span;
  const ws = u.window_start_ts;
  const we = u.window_end_ts;
  let box: { left: number; width: number } | null = null;
  if (ws != null && we != null) {
    const a = x(Math.min(nowYear, Math.max(FIRST_YEAR, yearFraction(ws))));
    const b = x(Math.min(nowYear, Math.max(FIRST_YEAR, yearFraction(we))));
    box = { left: a, width: Math.max(2, b - a) };
  }
  let low = years.map(() => 0);
  const layers = sectors.map((c, k) => {
    const up = years.map((p, i) => low[i] + p.by[k]);
    const upper = years.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.year).toFixed(1)},${y(up[i]).toFixed(1)}`).join("");
    const lower = years.map((p, i) => `L${x(p.year).toFixed(1)},${y(low[i]).toFixed(1)}`).reverse().join("");
    low = up;
    return <path key={c.key} data-sector={c.key} d={`${upper}${lower}Z`} fill={sectorInfo(c.edgar).colour} opacity=".78" />;
  });
  const labels = [1970, 1980, 1990, 2000, 2010, 2020].filter((l) => l <= nowYear);
  return (
    <div className="un-area" data-testid="u-area">
      <div className="un-plot">
      <svg viewBox={`0 0 ${AREA_W} ${AREA_H}`} preserveAspectRatio="none" role="img" aria-label="Members with daily prices, by year">
        <line x1="0" x2={AREA_W} y1={y(0)} y2={y(0)} stroke="rgba(255,255,255,.05)" vectorEffect="non-scaling-stroke" />
        <line x1="0" x2={AREA_W} y1={y(top / 2)} y2={y(top / 2)} stroke="rgba(255,255,255,.05)" vectorEffect="non-scaling-stroke" />
        <line x1="0" x2={AREA_W} y1={y(top)} y2={y(top)} stroke="rgba(255,255,255,.05)" vectorEffect="non-scaling-stroke" />
        {box && (
          <rect
            data-window
            x={box.left} y={4} width={box.width} height={AREA_H - 4} rx="3"
            fill="rgba(238,242,247,.05)" stroke="rgba(238,242,247,.35)" strokeDasharray="3 3" vectorEffect="non-scaling-stroke"
          />
        )}
        {layers}
        {years.map((p) => (
          <g key={p.year} data-year={p.year}>
            <title>
              {[
                `${p.year}: ${p.n} of ${total} with prices`,
                ...sectors.flatMap((c, k) => (p.by[k] > 0 ? [`${sectorInfo(c.edgar).label}: ${p.by[k]}`] : [])),
              ].join("\n")}
            </title>
            <rect x={x(p.year) - cell / 2} y={0} width={cell} height={AREA_H} fill="transparent" />
          </g>
        ))}
      </svg>
      {box && <span className="un-wl" style={{ left: `${(box.left / AREA_W) * 100}%` }}>The list's window</span>}
      </div>
      <span className="un-ytop num">{top}</span>
      <span className="un-ymid num">{Math.round(top / 2)}</span>
      <span className="un-ybot num">0</span>
      <div className="un-xl num">
        {labels.map((l) => <span key={l} style={{ left: `${((l - FIRST_YEAR) / span) * 100}%` }}>{l}</span>)}
      </div>
    </div>
  );
}

// The Sector cell's hover: the industry and its SIC code, or the server's reason for having none.
function industryHover(m: PriceUniverseMemberDetailRow): string {
  if (m.industry == null) return `Industry: ${m.industry_missing ?? "none on file"}`;
  return `Industry: ${m.industry}${m.sic != null ? `, SIC ${m.sic}` : ""}`;
}

// One member's held span on the 1962-to-today axis; the box is the universe's window.
function PricesLine({ m, u, now, colour }: { m: PriceUniverseMemberDetailRow; u: Dataset; now: number; colour: string }) {
  const ws = u.window_start_ts;
  const we = u.window_end_ts;
  if (!hasPrices(m)) return <span className="tl" title={`${m.ticker}\nNo daily prices are held`} />;
  const a = axisPercent(m.first_bar_ts!, now);
  const b = axisPercent(m.last_bar_ts!, now);
  const why = `${m.ticker}\nFirst bar: ${dayLong(m.first_bar_ts)}\nNewest bar: ${dayLong(m.last_bar_ts)}\nBars: ${count(m.bars!)}`;
  return (
    <span className="tl" title={why}>
      <b style={{ left: `${a}%`, width: `${Math.max(b - a, 0)}%`, background: colour }} />
      {ws != null && we != null && (
        <em style={{ left: `${axisPercent(ws, now)}%`, width: `${Math.max(axisPercent(we, now) - axisPercent(ws, now), 0)}%` }} />
      )}
    </span>
  );
}

export default function UniversesTab({ slot = null }: { slot?: HTMLElement | null } = {}) {
  const qc = useQueryClient();
  const [showTest, setShowTest] = useState(false);
  const listQ = useQuery({ queryKey: keys.list, queryFn: () => api.research.listDatasets("active", true) });
  const mixQ = useQuery({ queryKey: keys.mix(showTest), queryFn: () => api.research.priceUniverseSectorMix(showTest) });
  const namesQ = useStrategyNames();

  const [openId, setOpenId] = useState<number | null>(null);
  const [find, setFind] = useState("");
  const [allList, setAllList] = useState(false);
  const [memberFind, setMemberFind] = useState("");
  const [allMembers, setAllMembers] = useState(false);
  const [sectorPick, setSectorPick] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"new" | "add" | "edit" | "delete" | "sweep" | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const renameDone = useRef(false);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  function say(t: Toast) {
    if (timer.current) clearTimeout(timer.current);
    setToast(t);
    timer.current = setTimeout(() => setToast(null), TOAST_MS);
  }

  const universes = (listQ.data ?? []).filter((d) => d.dataset_type === "price_universe" && d.status !== "deleted");
  const testCount = universes.filter((d) => d.source === "synthetic").length;
  const pool = universes
    .filter((d) => showTest || d.source !== "synthetic")
    .sort((a, b) => b.created_at - a.created_at || b.id - a.id);
  const needle = find.trim().toLowerCase();
  const matched = needle
    ? pool.filter((d) => d.name.toLowerCase().includes(needle) || (d.display_name ?? "").toLowerCase().includes(needle))
    : pool;
  const listed = needle || allList ? matched : matched.slice(0, SHOWN_UNIVERSES);
  const u: Dataset | undefined = pool.find((d) => d.id === openId) ?? listed[0] ?? pool[0];
  const uid = u?.id ?? null;

  const detailQ = useQuery({
    queryKey: keys.detail(uid ?? 0),
    queryFn: () => api.research.priceUniverseMemberDetail(uid!),
    enabled: uid != null,
  });
  const runsQ = useQuery({
    queryKey: keys.runs(uid ?? 0),
    queryFn: () => api.research.priceUniverseRuns(uid!),
    enabled: uid != null,
  });

  const now = Math.floor(Date.now() / 1000);
  const nowYear = utcYear(now);
  const refresh = () => qc.invalidateQueries({ queryKey: ["universes"] });

  const rename = useMutation({
    mutationFn: (v: { id: number; name: string }) => api.research.setPriceUniverseDisplayName(v.id, v.name),
    onSuccess: () => { say({ text: "Renamed" }); void refresh(); },
    onError: (e) => say({ text: serverDetail(e) }),
  });
  const addBack = useMutation({
    mutationFn: (v: { id: number; ticker: string }) => api.research.addPriceUniverseMembers(v.id, [v.ticker]),
    onSuccess: () => void refresh(),
    onError: (e) => say({ text: serverDetail(e) }),
  });
  const removeMember = useMutation({
    mutationFn: (v: { id: number; ticker: string; from: string }) => api.research.removePriceUniverseMember(v.id, v.ticker),
    onSuccess: (_r, v) => {
      say({ text: `Removed ${v.ticker} from ${v.from}`, undo: () => addBack.mutate({ id: v.id, ticker: v.ticker }) });
      void refresh();
    },
    onError: (e) => say({ text: serverDetail(e) }),
  });
  const restore = useMutation({
    mutationFn: (id: number) => api.research.restoreDataset(id),
    onSuccess: (_r, id) => { setOpenId(id); void refresh(); },
    onError: (e) => say({ text: serverDetail(e) }),
  });

  function pick(id: number) {
    setOpenId(id);
    setMemberFind("");
    setAllMembers(false);
    setSectorPick(null);
  }

  function pickSector(key: string) {
    setSectorPick(sectorPick === key ? null : key);
    setAllMembers(false);
  }

  function finishRename(value: string) {
    if (renameDone.current || !u) return;
    renameDone.current = true;
    setRenaming(null);
    const next = value.trim();
    if (next !== "" && next !== (u.display_name ?? "")) rename.mutate({ id: u.id, name: next });
  }

  function afterDelete(deleted: Dataset) {
    const at = pool.findIndex((d) => d.id === deleted.id);
    const next = pool[at + 1] ?? pool[at - 1] ?? null;
    qc.setQueryData<Dataset[]>(keys.list, (rows) => (rows ?? []).filter((d) => d.id !== deleted.id));
    setOpenId(next ? next.id : null);
    say({ text: `Deleted ${named(deleted)}`, undo: () => restore.mutate(deleted.id) });
    void refresh();
  }

  const listError = listQ.isError ? serverDetail(listQ.error) : null;

  const newButton = <PhoneOff><button type="button" className="btn" onClick={() => setDialog("new")}>New list</button></PhoneOff>;

  if (listQ.isPending) return <div className="empty">Loading.</div>;
  if (listError) return <div className="un-err">{listError}</div>;

  const dialogs = (
    <>
      {dialog === "new" && (
        <NewUniverse
          pool={pool}
          onClose={() => setDialog(null)}
          onMade={(id, text) => { pick(id); say({ text }); void refresh(); }}
        />
      )}
      {u && dialog === "add" && (
        <AddMembers u={u} onClose={() => setDialog(null)} onDone={(text) => { say({ text }); void refresh(); }} />
      )}
      {u && dialog === "edit" && (
        <EditUniverse u={u} onClose={() => setDialog(null)} onDone={(text) => { say({ text }); void refresh(); }} />
      )}
      {u && dialog === "sweep" && <SweepWindow universeId={u.id} onClose={() => setDialog(null)} />}
      {u && dialog === "delete" && (
        <DeleteUniverse
          u={u}
          runs={runsQ.data ? runsQ.data.runs.length : null}
          onClose={() => setDialog(null)}
          onDone={() => afterDelete(u)}
        />
      )}
      {toast && (
        <div className="un-toast" role="status">
          <span>{toast.text}</span>
          {toast.undo && <PhoneOff><button type="button" className="btn q" onClick={() => { toast.undo?.(); setToast(null); }}>Undo</button></PhoneOff>}
        </div>
      )}
    </>
  );

  if (!u) {
    return (
      <div className="ug">
        <div className="cd un-empty"><span className="un-none">No universe yet</span>{newButton}</div>
        {dialogs}
      </div>
    );
  }

  const members = detailQ.data?.members ?? null;
  const runs = runsQ.data?.runs ?? null;
  const detailError = detailQ.isError ? serverDetail(detailQ.error) : null;
  const runsError = runsQ.isError ? serverDetail(runsQ.error) : null;
  const withPrices = members ? members.filter(hasPrices).length : null;
  const memberCount = members ? members.length : (u.member_count ?? null);
  const q = memberFind.trim().toLowerCase();
  const sectors = members ? bySector(members) : [];
  const found = (members ?? []).filter(
    (m) => (sectorPick === null || sectorKey(m.sector) === sectorPick)
      && (!q || m.ticker.toLowerCase().startsWith(q) || (m.name ?? "").toLowerCase().includes(q)),
  );
  const shownMembers = allMembers ? found : found.slice(0, SHOWN_MEMBERS);
  const loadingWhy = detailError ?? "Loading.";

  const facts: Array<{ label: string; value: ReactNode; tone?: string }> = [
    { label: "Members", value: memberCount == null ? dash("No member count is held") : count(memberCount) },
    { label: "Window", value: windowText(u.window_start_ts, u.window_end_ts) },
    {
      label: "With daily prices",
      value: members && withPrices != null ? `${withPrices} of ${members.length}` : dash(loadingWhy),
      tone: members && withPrices != null ? (withPrices < members.length ? "a" : "g") : undefined,
    },
    { label: "Days of prices saved", value: members ? bigNumber(barsSum(members)) : dash(loadingWhy) },
    { label: "Runs", value: runs ? String(runs.length) : dash(runsError ?? "Loading.") },
  ];

  const mixRow = (id: number) => mixQ.data?.universes.find((x) => x.universe_id === id);
  const rowBar = (id: number): Array<{ tone: "soft"; n: number; colour: string; hover: string }> => {
    const entry = mixRow(id);
    if (!entry || entry.members === 0 || entry.sectors.length === 0) return [];
    return orderSectors(entry.sectors.map((c) => ({ edgar: c.sector, key: sectorKey(c.sector), count: c.count })))
      .map((c) => ({ tone: "soft" as const, n: c.count, colour: sectorInfo(c.edgar).colour, hover: `${sectorInfo(c.edgar).label}: ${c.count}` }));
  };
  const rowWhy = (id: number): string | undefined => {
    if (mixQ.isError) return serverDetail(mixQ.error);
    if (!mixQ.data) return "Loading.";
    const entry = mixRow(id);
    if (!entry) return NO_COUNT;
    return entry.members === 0 || entry.sectors.length === 0 ? NO_MEMBERS : undefined;
  };
  const totalMembers = pool.reduce((a, d) => a + (d.member_count ?? 0), 0);

  const history: Run[] = [...(runs ?? [])].sort((a, b) => b.created_at - a.created_at || b.id - a.id);

  return (
    <div className="ug">
      {slot && createPortal(newButton, slot)}
      <Say
        lead={`${count(pool.length)} stock ${pool.length === 1 ? "list" : "lists"}, ${count(totalMembers)} members in all.`}
        more={<><em className="warn">Each list holds today's members</em>{": a company that left earlier is not in it. A stock list is the set of stocks a test runs on, with a first and a last day."}</>}
      />
      <Block
        title="Stock lists"
        state={<span className="nl-st run">{count(pool.length)}</span>}
        right={
          <span className="un-ctl">
            <input className="fin un-find" aria-label="Find a universe" placeholder="Find a universe" value={find} onChange={(e) => setFind(e.target.value)} />
            <button type="button" className="btn un-po" onClick={() => setAllList(!allList)}>
              {allList ? "Newest five" : "All universes"} <span className="num">{pool.length}</span>
            </button>
            <button type="button" className="btn un-po" onClick={() => setShowTest(!showTest)}>
              {showTest ? "Hide" : "Show"} test universes <span className="num">{testCount}</span>
            </button>
          </span>
        }
      >
        <div className="lc-wall" data-testid="u-wall">
          {listed.map((x) => {
            const hover = [
              x.display_name ?? "No display name",
              `Window: ${windowText(x.window_start_ts, x.window_end_ts)}`,
              `Members: ${x.member_count ?? "-"}`,
              `Made: ${dayLong(x.created_at)}`,
              rowWhy(x.id) ?? "",
            ].filter(Boolean).join("\n");
            return (
              <span key={x.id} className="un-cardwrap" data-u={x.id}>
                <LibCard
                  name={x.display_name ?? "No display name"} hue="data" fig={x.member_count == null ? "-" : count(x.member_count)}
                  unit={`members, ${yearsText(x.window_start_ts, x.window_end_ts)}`} bar={rowBar(x.id)}
                  facts={[{ label: "Made", value: dayShort(x.created_at) }]}
                  hover={hover} pressed={x.id === u.id} onClick={() => pick(x.id)}
                />
              </span>
            );
          })}
        </div>
      </Block>

      <div className="un-main">
        <Block
          title={<span data-testid="u-head">{renaming == null ? (
            <span
              className="nm2"
              role="button"
              tabIndex={0}
              title={u.display_name == null ? "No display name" : "Click to rename"}
              onClick={() => { renameDone.current = false; setRenaming(u.display_name ?? ""); }}
              onKeyDown={(e) => { if (e.key === "Enter") { renameDone.current = false; setRenaming(u.display_name ?? ""); } }}
            >
              {u.display_name ?? "-"}
            </span>
          ) : (
            <input
              className="nm2 un-nm"
              aria-label="Universe name"
              autoFocus
              value={renaming}
              onChange={(e) => setRenaming(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") finishRename(renaming);
                if (e.key === "Escape") { renameDone.current = true; setRenaming(null); }
              }}
              onBlur={() => finishRename(renaming)}
            />
          )}</span>}
          state={<span title="The members are today's list, counted as present from the first day" className="nl-st warn">Today's list</span>}
          right={(
            <PhoneOff>
              <span className="un-acts">
                <button type="button" className="btn" onClick={() => setDialog("sweep")}>Run a sweep</button>
                <button type="button" className="btn" onClick={() => setDialog("add")}>Add members</button>
                <button type="button" className="btn" onClick={() => setDialog("edit")}>Edit</button>
                <button type="button" className="btn" onClick={() => setDialog("delete")}>Delete</button>
              </span>
            </PhoneOff>
          )}
        >
          <div className="f5">
            {facts.map((f) => (
              <div key={f.label}><span>{f.label}</span><b className={`num${f.tone ? ` ${f.tone}` : ""}`}>{f.value}</b></div>
            ))}
          </div>

          <div className="un-chart">
            <p className="un-cap" title="Members with daily prices in each year, by sector office (SEC EDGAR). The box is the universe's window.">Members with prices, by year</p>
            {detailError ? (
              <div className="un-err">{detailError}</div>
            ) : !members ? (
              <div className="empty">Loading.</div>
            ) : members.length === 0 ? (
              <div className="empty">{dash(NO_MEMBERS)}</div>
            ) : withPrices === 0 ? (
              <div className="empty">No member has daily prices yet</div>
            ) : (
              <YearArea members={members} u={u} nowYear={nowYear} sectors={sectors} />
            )}
          </div>
        </Block>

        <div className="un-two">
          <Block title="Members" state={<span className="nl-st run">{count(found.length)}</span>}>
            {members && members.length > 0 && (
              <div className="un-sec" title="Today's members by sector office (SEC EDGAR). Press a sector to see only its members.">
                <div className="un-kbar" aria-label="Members by sector">
                  {sectors.map((c) => (
                    <i
                      key={c.key} data-sector={c.key} className={sectorPick && sectorPick !== c.key ? "off" : ""}
                      style={{ background: sectorInfo(c.edgar).colour, flex: `${c.count} 1 0` }}
                      title={`${sectorInfo(c.edgar).label}: ${c.count}, ${Math.round((c.count / members.length) * 100)}%`}
                      onClick={() => pickSector(c.key)}
                    />
                  ))}
                </div>
                <div className="un-kinds">
                  {sectors.map((c) => (
                    <button
                      key={c.key} type="button" data-sector={c.key} aria-pressed={sectorPick === c.key}
                      className={sectorPick === c.key ? "on" : sectorPick ? "off" : ""}
                      style={{ ["--k" as string]: sectorInfo(c.edgar).colour }}
                      onClick={() => pickSector(c.key)}
                    >
                      <i /><span>{sectorInfo(c.edgar).label}</span><b className="num">{c.count}</b>
                    </button>
                  ))}
                </div>
              </div>
            )}
            <input
              className="fin un-find"
              aria-label="Find a member"
              placeholder="Find a member by ticker or name"
              value={memberFind}
              onChange={(e) => { setMemberFind(e.target.value); setAllMembers(false); }}
            />
            {detailError ? (
              <div className="un-err">{detailError}</div>
            ) : !members ? (
              <div className="empty">Loading.</div>
            ) : (
              <table className="un-tbl" data-testid="u-members">
                <thead>
                  <tr>
                    <th>Ticker</th>
                    <th>Name</th>
                    <th title={`Daily prices held, ${AXIS_START} to today; the box is the window`}>{`Prices, ${AXIS_START} to today`}</th>
                    <th>Days of prices</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {shownMembers.map((m) => (
                    <tr key={m.ticker}>
                      <td className="un-sym" title={m.sector != null ? industryHover(m) : (m.sector_missing ?? "No sector is on file")}><i className="fd" style={{ background: sectorInfo(m.sector).colour }} /><b>{m.ticker}</b></td>
                      <td className="un-nmc">{m.name ?? dash("No company name on record")}</td>
                      <td className="un-pc"><PricesLine m={m} u={u} now={now} colour={sectorInfo(m.sector).colour} /></td>
                      <td className="num">{m.bars ? count(m.bars) : dash("No daily bars are held")}</td>
                      <td className="un-act">
                        <PhoneOff>
                          <button
                            type="button"
                            className="btn act"
                            aria-label={`Remove ${m.ticker}`}
                            onClick={() => removeMember.mutate({ id: u.id, ticker: m.ticker, from: named(u) })}
                          >
                            Remove
                          </button>
                        </PhoneOff>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {found.length > SHOWN_MEMBERS && (
              <div className="un-mfoot">
                <span>{`${allMembers ? found.length : SHOWN_MEMBERS} of ${found.length} shown`}</span>
                <button type="button" className="btn q" onClick={() => setAllMembers(!allMembers)}>
                  {allMembers ? "Show fewer" : `Show ${found.length - SHOWN_MEMBERS} more`}
                </button>
              </div>
            )}
          </Block>

          <Block title="History" state={<span className="nl-st run">{String(history.length)}</span>}>
            <div className="hist" data-testid="u-hist">
              {runsError && <div className="un-err">{runsError}</div>}
              {history.map((r) => {
                const w = RUN_WORD[r.status];
                const label = strategyLabel(namesQ, r.strategy_id);
                return (
                  <div key={r.id}>
                    <span className="num un-date">{dayShort(r.created_at)}</span>
                    <i style={{ background: w.colour }} />
                    <span className="un-what" title={label.hover}>{label.text}</span>
                    <span className={`nl-st ${w.pill}`}>{w.text}</span>
                  </div>
                );
              })}
              <div>
                <span className="num un-date">{dayShort(u.created_at)}</span>
                <i style={{ background: "#8d98a8" }} />
                <span className="un-what" title={`Made on ${dayLong(u.created_at)}`}>The list was made</span>
                <span />
              </div>
            </div>
          </Block>
        </div>
      </div>
      {dialogs}
    </div>
  );
}
