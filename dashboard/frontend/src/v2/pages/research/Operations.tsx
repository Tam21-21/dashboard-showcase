import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Block, PageHeader, Say, State } from "../../kit";
import type { NlTone } from "../../kit";
import { Window } from "../../kit/Window";
import type { PageProps } from "../../shell/sections";
import { getCounters, getLog, getSystems } from "../../data/opsApi";
import type { OpsCounter, OpsLogRow, OpsMark, OpsSystem } from "../../data/opsApi";
import { serverDetail } from "../../../lib/errorText";
import { Share } from "./Overview";
import "./operations.css";

// Operations as the frozen mockup draws it (night of 2026-11-15): the finding said once; each part of the lab as a tile with
// its last 24 hours, one bar per half hour, and the share of checked half hours that were fine; today's activity as a plain
// list; the guard counters by group. /api/ops/signals is not read: it counts synthetic rows.

const REFRESH_MS = 60_000;
const HALF_HOUR_MS = 30 * 60 * 1000;
const PAGE = 10;

export const WORD: Record<OpsSystem["word"], [NlTone, string]> = {
  WORKING: ["good", "Working"], IDLE: ["dim", "Idle"], STALE: ["warn", "Behind"], DOWN: ["bad", "Down"], "NOT CONNECTED": ["dim", "Not connected"],
};
const MEANS: Record<OpsSystem["word"], string> = {
  WORKING: "It works", IDLE: "It had nothing to do", STALE: "It is behind", DOWN: "It is down", "NOT CONNECTED": "It is not connected",
};
const TILE_TONE: Record<OpsSystem["tone"], NlTone> = { good: "good", bad: "bad", warn: "warn", quiet: "dim", off: "dim" };
const GROUP: Record<string, string> = {
  paperdb: "Paper store", judgedb: "Judge store", researchdb: "Research store", chain_runner: "Chain runner", loop_counters: "Background jobs",
};
const LOG_WORD: Record<string, [NlTone, string]> = { OPERATIONAL: ["good", "Working again"], DEGRADED: ["warn", "Behind"], DOWN: ["bad", "Went down"] };

const paris = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const parisDay = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", day: "numeric", month: "short" });

function markTitle(m: OpsMark): string {
  const from = Date.parse(m.start);
  return `${paris.format(from)} to ${paris.format(from + HALF_HOUR_MS)}: ${m.finding ?? "Nothing was checked"}`;
}

const since = (iso: string | null) => (iso ? `${parisDay.format(Date.parse(iso))}, ${paris.format(Date.parse(iso))}` : "never");
const names = (list: OpsSystem[]) => list.map((s) => s.name.toLowerCase()).join(" and ");
const vars = (v: Record<string, number>) => v as CSSProperties;
const logKind = (r: OpsLogRow): [NlTone, string] => (r.kind === "event" ? ["run", "Event"] : (r.state !== null && LOG_WORD[r.state]) || ["dim", "Change"]);

// A counter's id in words: single letters are the research store's guard letters; the rest lose their underscores.
const plain = (id: string) => (/^[A-Z]$/.test(id) ? `Guard ${id}` : id.replace(/_/g, " ").toLowerCase().replace(/ count$/, "").replace(/^./, (c) => c.toUpperCase()));

/** The page's finding: what is down and since when, else what is behind, else all running. */
function Finding({ systems }: { systems: OpsSystem[] }) {
  const down = systems.filter((s) => s.word === "DOWN");
  const stale = systems.filter((s) => s.word === "STALE");
  const working = systems.filter((s) => s.word === "WORKING");
  const idle = systems.filter((s) => s.word === "IDLE");
  if (down.length > 0) {
    return (
      <Say
        lead={`The ${names(down)} ${down.length > 1 ? "are" : "is"} down.`}
        more={<>
          {down.map((s) => <span key={s.name}><em className="bad">{s.reason ?? "Down"}</em> since {since(s.last_proven_at)}. </span>)}
          {working.length > 0 && `The ${names(working)} work. `}
          {idle.length > 0 && `The ${names(idle)} had nothing to do.`}
        </>}
      />
    );
  }
  if (stale.length > 0) return <Say lead={`The ${names(stale)} ${stale.length > 1 ? "are" : "is"} behind.`} />;
  return <Say lead="Every part of the lab is running." />;
}

function marksOf(s: OpsSystem) {
  const ok = s.marks.filter((m) => m.mark === "ok").length;
  const fail = s.marks.filter((m) => m.mark === "fail").length;
  return { ok, fail, none: s.marks.length - ok - fail, share: ok + fail ? Math.round((ok / (ok + fail)) * 100) : null };
}

/** The last 24 hours as one bar a half hour, oldest on the left, with its hours under it. */
function Strip({ s }: { s: OpsSystem }) {
  const first = Date.parse(s.marks[0]?.start ?? "");
  return (
    <>
      <span className="ops-strip">
        {s.marks.map((m, i) => <i key={m.start} className={m.mark} title={markTitle(m)} style={vars({ "--i": i })} />)}
      </span>
      {Number.isFinite(first) && <span className="op-ax num"><span>{paris.format(first)}</span><span>{paris.format(first + 24 * HALF_HOUR_MS)}</span><span>now</span></span>}
    </>
  );
}

/** One part of the lab: its state in words, the share of its checks that were fine, and its last 24 hours. */
function SystemTile({ s, r, onPick }: { s: OpsSystem; r: number; onPick: () => void }) {
  const m = marksOf(s);
  const [tone, word] = WORD[s.word];
  return (
    <button type="button" className={`op-tile h-${TILE_TONE[s.tone]}`} data-testid="ops-system" style={vars({ "--ri": r })} onClick={onPick}
      title={`${s.name}${s.where ? `, ${s.where}` : ""}\n${s.reason ?? MEANS[s.word]}\nPress for its failed checks`}>
      <span className="top">
        <span className="id"><b className="ops-name">{s.name}</b>{s.where && <small className="ops-where">{s.where}</small>}</span>
        <State tone={tone}>{word}</State>
      </span>
      <span className="figrow" title={m.ok + m.fail ? `${m.ok} of ${m.ok + m.fail} checked half hours were fine` : "Not checked in the last 24 hours"}>
        <b className="op-up"><span className="num">{m.share == null ? "-" : `${m.share}%`}</span></b>
        <small>{m.share == null ? "never checked" : "of checks fine"}</small>
      </span>
      <Strip s={s} />
      <span className="chg">
        <span><i>Last seen working</i><b className="num">{since(s.last_proven_at)}</b></span>
        <span><i>Failed checks</i><b className={m.fail ? "num bad" : "num"}>{m.ok + m.fail ? `${m.fail} of ${m.ok + m.fail}` : "-"}</b></span>
      </span>
    </button>
  );
}

/** A list in pages, newest first; the controls show only when there is more than one page. */
function Pager({ n, page, per, set }: { n: number; page: number; per: number; set: (p: number) => void }) {
  if (n <= per) return null;
  const last = Math.ceil(n / per) - 1;
  return (
    <div className="op-pager">
      <span className="num">{`${page * per + 1} to ${Math.min(n, (page + 1) * per)} of ${n}`}</span>
      <button type="button" className="btn q" disabled={page === 0} onClick={() => set(page - 1)}>Newer</button>
      <button type="button" className="btn q" disabled={page >= last} onClick={() => set(page + 1)}>Older</button>
    </div>
  );
}

function SystemWindow({ s, onClose }: { s: OpsSystem; onClose: () => void }) {
  const [page, setPage] = useState(0);
  const m = marksOf(s);
  const [tone, word] = WORD[s.word];
  const fails = s.marks.filter((x) => x.mark === "fail").reverse();
  const shown = fails.slice(page * 8, page * 8 + 8);
  const facts: Array<[string, string, string]> = [
    ["Checks that were fine", m.share == null ? "-" : `${m.share}%`, m.fail ? "bad" : ""],
    ["Failed checks", String(m.fail), m.fail ? "bad" : ""], ["Half hours not checked", String(m.none), ""], ["Last seen working", since(s.last_proven_at), ""],
  ];
  return (
    <Window label={s.name} size="m" className="op-win" onClose={onClose}>
      <div className="h2"><span>{s.name}</span></div>
      <p className="op-lead2"><State tone={tone}>{word}</State><span>{s.where ?? ""}</span></p>
      <p className="op-note">{s.reason ?? MEANS[s.word]}</p>
      <div className="op-facts">{facts.map(([k, v, c]) => <div key={k}><span>{k}</span><b className={`num ${c}`}>{v}</b></div>)}</div>
      <h3 className="op-h3">The last 24 hours, half hour by half hour</h3>
      <div className="op-big"><Strip s={s} /></div>
      <h3 className="op-h3">Failed checks <span className="op-cnt">{fails.length}</span></h3>
      {fails.length === 0 ? <p className="op-note">No check failed in the last 24 hours.</p> : (
        <div className="op-tbl">
          <div className="hd"><span>Half hour</span><span>What the check said</span></div>
          {shown.map((x) => <div key={x.start} className="row"><span className="num">{`${parisDay.format(Date.parse(x.start))}, ${paris.format(Date.parse(x.start))}`}</span><span>{x.finding ?? "Failed"}</span></div>)}
        </div>
      )}
      <Pager n={fails.length} page={page} per={8} set={setPage} />
      <div className="op-foot"><button type="button" className="btn q" onClick={onClose}>Close</button></div>
    </Window>
  );
}

function GuardWindow({ mod, list, onClose }: { mod: string; list: OpsCounter[]; onClose: () => void }) {
  const sorted = [...list].sort((a, b) => b.value - a.value);
  return (
    <Window label={`${GROUP[mod] ?? mod} guards`} size="m" className="op-win" onClose={onClose}>
      <div className="h2"><span>{`${GROUP[mod] ?? mod} guards`}</span></div>
      <p className="op-note">Each guard counts the times it had to step in. Zero means it never fired.</p>
      <div className="op-tbl g3">
        <div className="hd"><span>Guard</span><span className="r">Times fired</span><span>State</span></div>
        {sorted.map((c) => (
          <div key={c.id} className="row">
            <span>{plain(c.id)}</span><span className={c.value > 0 ? "r num bad" : "r num"}>{c.value}</span>
            <State tone={c.value > 0 ? "bad" : c.declared_only ? "dim" : "good"}>{c.value > 0 ? "Fired" : c.declared_only ? "Not counted yet" : "Quiet"}</State>
          </div>
        ))}
      </div>
      <div className="op-foot"><button type="button" className="btn q" onClick={onClose}>Close</button></div>
    </Window>
  );
}

/** A row for each group, with how many of its guards fired. A press lists the group's guards by name. */
function Guards({ counters, onPick }: { counters: OpsCounter[]; onPick: (mod: string) => void }) {
  const groups = new Map<string, OpsCounter[]>();
  for (const c of counters) groups.set(c.module, [...(groups.get(c.module) ?? []), c]);
  return (
    <div className="op-guards" data-testid="guards">
      <div className="hd"><span>Group</span><span className="r">Guards</span><span className="r">Fired</span><span>Quiet, fired, not counted yet</span></div>
      {[...groups.entries()].map(([mod, list]) => {
        const hit = list.filter((c) => c.value > 0).length;
        const off = list.filter((c) => c.declared_only && !(c.value > 0)).length;
        return (
          <button type="button" key={mod} className="row" data-group={mod} onClick={() => onPick(mod)}
            title="Green never had to step in. Red stepped in at least once. Grey is written down but no code adds to it yet">
            <span>{GROUP[mod] ?? plain(mod)}</span><span className="r num">{list.length}</span><span className={hit ? "r num bad" : "r num"}>{hit}</span>
            <Share parts={[["good", list.length - hit - off], ["bad", hit], ["soft", off]]} />
          </button>
        );
      })}
      <p className="op-more">Press a group for its guards</p>
    </div>
  );
}

export default function Operations(_props: PageProps) {
  const systemsQ = useQuery({ queryKey: ["ops", "systems"], queryFn: getSystems, refetchInterval: REFRESH_MS });
  const logQ = useQuery({ queryKey: ["ops", "log"], queryFn: getLog, refetchInterval: REFRESH_MS });
  const countersQ = useQuery({ queryKey: ["ops", "counters"], queryFn: getCounters, refetchInterval: REFRESH_MS });
  const [openSys, setOpenSys] = useState<string | null>(null);
  const [openGuards, setOpenGuards] = useState<string | null>(null);
  const [logPage, setLogPage] = useState(0);
  const list = systemsQ.data?.systems ?? [];
  const counters = countersQ.data?.counters ?? [];

  let systems: ReactNode;
  if (systemsQ.isError) systems = <p className="empty">{serverDetail(systemsQ.error)}</p>;
  else if (!systemsQ.data) systems = <p className="empty">Loading the systems.</p>;
  else if (list.length === 0) systems = <p className="empty">No system is watched yet.</p>;
  else systems = <div className="op-wall">{list.map((s, r) => <SystemTile key={s.name} s={s} r={r} onPick={() => setOpenSys(s.name)} />)}</div>;

  let log: ReactNode;
  if (logQ.isError) log = <p className="empty">{serverDetail(logQ.error)}</p>;
  else if (!logQ.data) log = <p className="empty">Loading the log.</p>;
  else if (logQ.data.log.length === 0) log = <p className="op-text">{`Nothing has happened today, up to ${paris.format(Date.now())}. A part that goes down or comes back is listed here.`}</p>;
  else {
    const rows = logQ.data.log;
    const shown = rows.slice(logPage * PAGE, logPage * PAGE + PAGE);
    log = (
      <div className="op-log" data-testid="ops-log">
        <div className="hd"><span>Time</span><span>Part</span><span>What happened</span><span>Kind</span></div>
        {shown.map((r, i) => {
          const [t, w] = logKind(r);
          return (
            <div key={`${r.at}-${i}`} className="row" data-testid="ops-log-row">
              <span className="num">{r.time}</span><span>{r.system}</span><span>{r.sentence}</span><State tone={t}>{w}</State>
            </div>
          );
        })}
        <Pager n={rows.length} page={logPage} per={PAGE} set={setLogPage} />
      </div>
    );
  }

  let guards: ReactNode;
  if (countersQ.isError) guards = <p className="empty">{serverDetail(countersQ.error)}</p>;
  else if (!countersQ.data) guards = <p className="empty">Loading the guards.</p>;
  else if (counters.length === 0) guards = <p className="empty">The server lists no guards.</p>;
  else guards = <Guards counters={counters} onPick={setOpenGuards} />;

  const count = (w: OpsSystem["word"]) => list.filter((s) => s.word === w).length;
  const states = (["WORKING", "DOWN", "STALE", "IDLE", "NOT CONNECTED"] as const).filter((w) => count(w) > 0)
    .map((w) => <State key={w} tone={WORD[w][0]}>{`${count(w)} ${WORD[w][1].toLowerCase()}`}</State>);
  const fired = counters.filter((c) => c.value > 0).length;
  const unwired = counters.filter((c) => c.declared_only).length;
  const events = logQ.data?.log.length;
  const sysOpen = list.find((s) => s.name === openSys);
  const grpOpen = openGuards ? counters.filter((c) => c.module === openGuards) : [];

  return (
    <div className="v2-page op-page">
      <PageHeader crumb="Research" title="Operations" />
      {list.length > 0 && <Finding systems={list} />}
      <div className="op-stack">
        <Block title="System status, last 24 hours" state={<>{states}</>}
          right={<div className="op-key"><span><i className="ok" />Check was fine</span><span><i className="fail" />Check failed</span><span><i className="none" />Not checked</span></div>}>
          {systems}
        </Block>
        <div className="op-two">
          <Block title="Today's activity" state={events != null ? <State tone={events ? "run" : "dim"}>{`${events} ${events === 1 ? "event" : "events"}`}</State> : undefined}>{log}</Block>
          <Block title="Safety checks"
            state={countersQ.data ? <><State tone={fired ? "bad" : "good"}>{fired ? `${fired} fired` : "None fired"}</State>{unwired > 0 && <State tone="dim">{`${unwired} not counted yet`}</State>}</> : undefined}>
            <p className="op-text">{`A guard is a safety check in the code: it refuses a bad write or catches a background job that crashed, and counts each time it had to step in.${fired ? "" : " None has had to."}`}</p>
            {guards}
          </Block>
        </div>
      </div>
      {sysOpen && <SystemWindow s={sysOpen} onClose={() => setOpenSys(null)} />}
      {openGuards && <GuardWindow mod={openGuards} list={grpOpen} onClose={() => setOpenGuards(null)} />}
    </div>
  );
}
