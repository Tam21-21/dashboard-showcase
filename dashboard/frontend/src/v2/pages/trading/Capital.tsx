import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Block, PageHeader, Say, State, Window } from "../../kit";
import type { NlTone } from "../../kit";
import { HoldButton, PhoneOff, useLayout } from "../../kit/phone";
import type { PageProps } from "../../shell/sections";
import { armKill, disarmKill, getKillState, verifyKill, getRealAccount, getReadiness, getRealLimits, putRealLimits } from "../../data/realApi";
import type { ArmAction, KillState, PutLimitsResponse, ReadinessItem, ReadinessItemName, RealLimit } from "../../data/realTypes";
import { ApiError } from "../../../lib/api";
import { Card, NeedsBlock, Share, Wall } from "./parts";
import type { CardFact, Hue, Stop } from "./parts";
import "./capital.css";

// This page reads the real-money routes. Its writes are the limits (PUT /api/real/limits) and the
// kill switch arm, verify and disarm (POST /api/real/kill/...). None places an order; arm and
// verify refuse with a 503 while no broker is wired. Nothing here enables real trading: a go is
// recorded by hand.

const NO_ACCOUNT = "No real account configured";
const NO_ACCOUNT_WHY = "No real account id is set on the server";

const CHECKLIST: Array<{ item: ReadinessItemName; label: string; good: string }> = [
  { item: "go", label: "Your go, recorded in the decisions log", good: "recorded" },
  { item: "kill_switch", label: "Kill switch on this dashboard", good: "ready" },
  { item: "limits", label: "Hard limits held by the server", good: "set" },
  { item: "broker", label: "Broker account (Alpaca)", good: "connected" },
  { item: "approved_strategy", label: "A strategy approved for capital", good: "approved" },
];

const LIMIT_NAMES: Record<string, string> = {
  "real_trading.per_order_notional_cap": "Largest order",
  "real_trading.ticker_net_cap": "Largest position in one stock",
  "real_trading.total_net_cap": "Total invested",
  "real_trading.loss_limit_window": "Largest loss in a window",
  "real_trading.loss_limit_since_go": "Largest loss since go",
  "real_trading.order_rate_cap": "Orders per window",
  "real_trading.reconciliation_max_age": "Broker check at most",
  "real_trading.account_value_max_age": "Account value at most",
  "real_trading.kill_drill_max_age": "Kill drill at most",
};
const GROUPS: Array<{ title: string; keys: string[] }> = [
  {
    title: "Order limits",
    keys: [
      "real_trading.per_order_notional_cap",
      "real_trading.ticker_net_cap",
      "real_trading.total_net_cap",
      "real_trading.loss_limit_window",
      "real_trading.loss_limit_since_go",
      "real_trading.order_rate_cap",
    ],
  },
  {
    title: "Freshness",
    keys: [
      "real_trading.reconciliation_max_age",
      "real_trading.account_value_max_age",
      "real_trading.kill_drill_max_age",
    ],
  },
];
const ALL_KEYS = GROUPS.flatMap((g) => g.keys);
const nameOf = (key: string): string => LIMIT_NAMES[key] ?? key.replace(/^real_trading\./, "").replace(/_/g, " ");

const words = (w: string): string => w.replace(/-/g, " ");
const sentence = (w: string): string => { const t = w.replace(/-/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
const dash = (why: string): ReactNode => <span title={why}>-</span>;

// The server's own words for a failed read or write: the route's detail when it is a sentence.
function detailOf(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
  }
  return "the server did not answer, try again";
}
const NO_BROKER = "No broker is connected yet, so the switch cannot act.";

// The sentence for a refused kill switch action: the no-broker line for a 503 (arm and verify
// only), the server's own words when it wrote a sentence, else a plain fallback.
function killRefusal(e: unknown, fallback: string, noBrokerOn503: boolean): string {
  if (noBrokerOn503 && e instanceof ApiError && e.status === 503) return NO_BROKER;
  if (e instanceof ApiError && typeof (e.body as { detail?: unknown } | null)?.detail === "string") return detailOf(e);
  return fallback;
}
const couldNotRead = (what: string, e: unknown): string => `could not read ${what}: ${detailOf(e)}`;

// The kill switch's five states: tone, plain word, what it means, how far along the path it sits (0 to 3).
const KILL: Record<string, readonly [NlTone, string, string, number]> = {
  "never-armed": ["dim", "Never armed", "The switch has never been used. Orders would pass.", 0],
  "disarmed-verified-clear": ["good", "Off, checked clear", "The switch is off, and a check saw orders pass again.", 1],
  "armed-unverified": ["warn", "Armed, not checked", "The stop was sent. No check has seen it work yet, so press Check.", 2],
  "armed-verified-effective": ["good", "Armed and working", "A check saw the stop work: no order passes.", 3],
  "armed-verified-ineffective": ["bad", "Armed but not working", "A check saw orders still pass. This must be fixed before anything else.", 3],
};
const KILL_SHORT: Record<string, string> = {
  "never-armed": "Unused", "disarmed-verified-clear": "Off", "armed-unverified": "Armed", "armed-verified-effective": "Armed", "armed-verified-ineffective": "Failing",
};
const STEP_TITLE: Record<ReadinessItemName, string> = {
  go: "Your go is not recorded", kill_switch: "The kill switch is not ready", limits: "The server's limits are not all set",
  broker: "The broker account is not connected", approved_strategy: "No strategy is approved for real money",
};
const STEP_SHORT: Record<ReadinessItemName, string> = {
  go: "your go", kill_switch: "the kill switch", limits: "the server's limits", broker: "the broker account", approved_strategy: "an approved strategy",
};

const itemTone = (it: ReadinessItem, good: string): NlTone =>
  it.state === good || /^\d+ approved/.test(it.state) ? "good" : it.state === "none" ? "dim" : "warn";

// The count the server gives, or null when the answer carries none.
function approvedCount(r: { approved_count?: unknown } | undefined): number | null {
  return typeof r?.approved_count === "number" ? r.approved_count : null;
}

function approvedWord(it: ReadinessItem, count: number | null): string {
  if (it.item === "approved_strategy" && it.state === "approved") {
    return count === null ? "Approved" : `${count} approved`;
  }
  return sentence(it.state);
}

export default function Capital({ app, section }: PageProps) {
  const navigate = useNavigate();
  const accountQ = useQuery({ queryKey: ["real", "account"], queryFn: getRealAccount });
  const accountId = accountQ.data?.account_id ?? null;
  const hasAccount = typeof accountId === "string";
  const readinessQ = useQuery({ queryKey: ["real", "readiness", accountId], queryFn: getReadiness, enabled: accountQ.data !== undefined });
  const limitsQ = useQuery({ queryKey: ["real", "limits"], queryFn: getRealLimits });
  const killQ = useQuery({
    queryKey: ["real", "kill", accountId],
    queryFn: () => getKillState(accountId as string),
    enabled: typeof accountId === "string",
  });
  const [editing, setEditing] = useState(false);
  const phone = useLayout() === "phone";

  const noAccount = accountQ.data !== undefined && !hasAccount;
  // Why an account-based figure has no value yet, or null when it has one.
  const accountWhy: string | null = accountQ.isError
    ? couldNotRead("the account", accountQ.error)
    : !accountQ.data
      ? "Loading"
      : noAccount
        ? null
        : readinessQ.isError
          ? couldNotRead("the checklist", readinessQ.error)
          : !readinessQ.data
            ? "Loading"
            : null;
  const items = readinessQ.data?.items ?? [];
  const find = (name: ReadinessItemName) => items.find((i) => i.item === name);
  const read = !noAccount && accountWhy === null && readinessQ.data !== undefined;
  const limitRows = limitsQ.data?.limits ?? null;
  const limSet = limitRows ? limitRows.filter((l) => l.value !== null).length : null;
  const limTotal = limitRows ? limitRows.length : ALL_KEYS.length;
  const done = items.filter((i) => itemTone(i, CHECKLIST.find((c) => c.item === i.item)?.good ?? "") === "good").length;
  const approved = approvedCount(readinessQ.data);
  const kill = killQ.data;

  // Needs you: every checklist step that is not done, in the server's own words.
  const stops: Stop[] = [];
  if (read) {
    if (kill && kill.state === "armed-unverified") {
      stops.push({ key: "check", tone: "warn", word: "Waits for you", title: "The armed kill switch has not been checked",
        detail: "Press Check so the server confirms that no order passes",
        btn: "Open the kill switch", act: () => document.getElementById("cap-kill")?.scrollIntoView?.({ behavior: "smooth", block: "start" }) });
    }
    if (kill && kill.state === "armed-verified-ineffective") {
      stops.push({ key: "broken", tone: "bad", word: "Broken", title: "The kill switch is armed but not working", detail: "A check saw orders still pass. Fix this before anything else" });
    }
    for (const c of CHECKLIST) {
      const it = find(c.item);
      if (!it || itemTone(it, c.good) === "good") continue;
      const extra: Partial<Stop> =
        c.item === "limits" ? { btn: "Edit limits", act: () => setEditing(true),
          bar: limSet !== null ? <Share parts={[["good", limSet], ["warn", limTotal - limSet]]} /> : undefined, cap: limSet !== null ? `${limSet} of ${limTotal} set` : undefined }
        : c.item === "approved_strategy" ? { btn: "Open Paper", act: () => navigate("/app/trading/paper") } : {};
      const unsetN = c.item === "limits" ? limTotal - (limSet ?? 0) : 0;
      stops.push(c.item === "limits" && limSet !== null
        ? { key: c.item, tone: "warn", word: "Waits for you", title: `${unsetN} of the ${limTotal} server limits ${unsetN === 1 ? "is" : "are"} not set`, detail: "The checklist stays open until the server holds every limit", ...extra }
        : { key: c.item, tone: "warn", word: "Waits for you", title: STEP_TITLE[c.item], detail: it.evidence, ...extra });
    }
  }

  const killWord = kill ? KILL[kill.state] : undefined;
  const header = (
    <PageHeader crumb={app.name} title={section.label}
      right={<>
        <span title="The real-money account: its checklist, limits and kill switch"><State tone="run">Real money</State></span>
      </>} />
  );

  let lead: string;
  let more: ReactNode;
  if (accountQ.isError) {
    lead = "The real-money account could not be read.";
    more = couldNotRead("the account", accountQ.error);
  } else if (!accountQ.data) {
    lead = "Reading the real-money account.";
    more = null;
  } else if (noAccount) {
    lead = "No real-money account is set up.";
    more = <>No real account id is set on the server, so the checklist and the kill switch have nothing to read. Nothing on this page turns real trading on.</>;
  } else if (!readinessQ.data) {
    lead = readinessQ.isError ? "The checklist could not be read." : "Reading the checklist.";
    more = readinessQ.isError ? couldNotRead("the checklist", readinessQ.error) : null;
  } else {
    lead = `Real money is off: ${done} of the ${CHECKLIST.length} steps before the first real order are done.`;
    const open = CHECKLIST.filter((c) => { const it = find(c.item); return !it || itemTone(it, c.good) !== "good"; });
    more = (
      <>
        Still open: {open.length === 0 ? "nothing" : open.map((c, i) => <span key={c.item}>{i > 0 ? ", " : ""}<em className="warn">{STEP_SHORT[c.item]}</em></span>)}.
        {killWord ? <> The kill switch is <em className={killWord[0] === "good" ? "good" : killWord[0] === "bad" ? "bad" : killWord[0] === "warn" ? "warn" : ""}>{killWord[1].toLowerCase()}</em>.</> : null} Nothing on this page turns real trading on.
      </>
    );
  }

  const none = (word: string) => ({ chip: ["dim", word] as [NlTone, string], fig: "-", pic: <Share parts={[["soft", 1, 0.15]]} /> });
  const noWord = noAccount ? "No account" : "Not read";
  const noWhy = noAccount ? NO_ACCOUNT_WHY : accountWhy ?? "Loading";
  const killShort = kill ? KILL_SHORT[kill.state] ?? words(kill.state) : null;

  return (
    <div className="v2-page cap-page">
      {header}
      <Say lead={lead} more={more} />
      <div className="tp-stack">
        {phone && <KillBlock accountId={accountId} accountError={accountQ.isError ? couldNotRead("the account", accountQ.error) : null} loaded={accountQ.data !== undefined} />}
        <NeedsBlock stops={stops} notRead={!read} empty={read ? "Nothing waits for you: every step is done." : "Nothing was read, so nothing can be listed here."} />
        <Block title="Summary">
          <Wall>
            <Card name="Steps done" hover={read ? undefined : noWhy} sub={`of the ${CHECKLIST.length} steps before the first real order`}
              {...(read ? { hue: done === CHECKLIST.length ? "good" as Hue : "warn" as Hue, chip: done === CHECKLIST.length ? ["good", "Ready"] as [NlTone, string] : ["warn", "Not ready"] as [NlTone, string], fig: String(done),
                pic: <Share parts={[["good", done], ["warn", CHECKLIST.length - done]]} />, facts: [["Done", String(done), done ? "good" : ""], ["Open", String(CHECKLIST.length - done), CHECKLIST.length - done ? "warn" : ""]] as CardFact[] } : none(noWord))} />
            <Card name="Kill switch" hover={read ? undefined : noWhy} word={read} sub={read ? `${killWord ? killWord[2] : "No kill switch state yet"}` : "the emergency stop for real money"}
              {...(read && kill && killWord ? { hue: killWord[0] === "dim" ? "" as Hue : killWord[0], chip: [killWord[0], killWord[0] === "good" ? "Ready" : killWord[0] === "bad" ? "Broken" : killWord[0] === "warn" ? "Check it" : "Unused"] as [NlTone, string],
                facts: [["Last drill", DRILL_WORD[kill.drill_state] ?? "-", DRILL_TONE[kill.drill_state] as "good" | "bad" | ""], ["Last check", kill.last_verified_at ? dayTime(kill.last_verified_at).split(",")[0] : "Never"]] as CardFact[],
                fig: killShort, pic: <Share parts={[[killWord[0] === "dim" ? "soft" : killWord[0] === "run" ? "data" : killWord[0], Math.max(1, killWord[3])], ["soft", 3 - Math.max(1, killWord[3]), 0.15]]} /> } : none(noWord))} />
            <Card name="Server limits" sub={`of the ${limTotal} hard limits are set`}
              {...(limSet !== null ? { hue: limSet === limTotal ? "good" as Hue : "warn" as Hue, chip: limSet === limTotal ? ["good", "All set"] as [NlTone, string] : ["warn", `${limTotal - limSet} not set`] as [NlTone, string], fig: String(limSet),
                pic: <Share parts={[["good", limSet], ["warn", limTotal - limSet]]} />,
                facts: [["Order limits", `${limitRows!.filter((l) => GROUPS[0].keys.includes(l.key) && l.value !== null).length} of ${GROUPS[0].keys.length}`], ["Freshness", `${limitRows!.filter((l) => GROUPS[1].keys.includes(l.key) && l.value !== null).length} of ${GROUPS[1].keys.length}`]] as CardFact[] }
                : { ...none("Not read"), hover: limitsQ.isError ? couldNotRead("the limits", limitsQ.error) : "Loading" })} />
            <Card name="Strategies" sub="approved for real money"
              {...(approved !== null ? { hue: approved ? "good" as Hue : "warn" as Hue, chip: approved ? ["good", "Approved"] as [NlTone, string] : ["dim", "None yet"] as [NlTone, string], fig: String(approved),
                pic: <Share parts={[["good", approved], ["warn", approved ? 0 : 1]]} />, facts: [["Approved", String(approved), approved ? "good" : ""]] as CardFact[] }
                : { ...none(noWord), hover: readinessQ.isError ? couldNotRead("the checklist", readinessQ.error) : readinessQ.data ? "The checklist gave no count" : noWhy })} />
          </Wall>
        </Block>
        <div className="tp-two even">
          <ChecklistBlock
            accountQ={{ isError: accountQ.isError, error: accountQ.error, loaded: accountQ.data !== undefined }}
            noAccount={noAccount}
            readiness={readinessQ.data?.items}
            approvedTotal={approved}
            readinessError={readinessQ.isError ? couldNotRead("the checklist", readinessQ.error) : null}
          />
          {!phone && <KillBlock accountId={accountId} accountError={accountQ.isError ? couldNotRead("the account", accountQ.error) : null} loaded={accountQ.data !== undefined} />}
        </div>
        <LimitsBlock editing={editing} setEditing={setEditing} />
      </div>
    </div>
  );
}

function ChecklistBlock(props: {
  accountQ: { isError: boolean; error: unknown; loaded: boolean };
  noAccount: boolean;
  readiness: ReadinessItem[] | undefined;
  approvedTotal: number | null;
  readinessError: string | null;
}) {
  const { accountQ, noAccount, readiness, approvedTotal, readinessError } = props;
  let body: ReactNode;
  let state: ReactNode;
  if (accountQ.isError) {
    body = <div className="empty">{couldNotRead("the account", accountQ.error)}</div>;
  } else if (!accountQ.loaded) {
    body = <div className="empty">Loading.</div>;
  } else if (noAccount) {
    state = <State tone="warn">{NO_ACCOUNT}</State>;
    body = (
      <div className="cap-none" title={NO_ACCOUNT_WHY}>
        <Share parts={[["soft", 1, 0.15]]} />
        <div className="cap-steps">
          {CHECKLIST.map((c, i) => (
            <div key={c.item}><span className="cap-n num">{i + 1}</span><span className="cap-t"><b>{c.label}</b></span><State tone="dim">Not read</State></div>
          ))}
        </div>
      </div>
    );
  } else if (readinessError !== null) {
    body = <div className="empty">{readinessError}</div>;
  } else if (!readiness) {
    body = <div className="empty">Loading.</div>;
  } else {
    const nDone = readiness.filter((i) => itemTone(i, CHECKLIST.find((c) => c.item === i.item)?.good ?? "") === "good").length;
    state = <State tone={nDone === CHECKLIST.length ? "good" : "warn"}>{`${nDone} of ${CHECKLIST.length} done`}</State>;
    body = (
      <>
        <Share parts={[["good", nDone], ["warn", CHECKLIST.length - nDone]]} />
        <div className="cap-steps">
          {CHECKLIST.map((c, i) => {
            const it = readiness.find((x) => x.item === c.item);
            if (!it) return <div key={c.item}><span className="cap-n num">{i + 1}</span><span className="cap-t"><b>{c.label}</b></span>{dash("The checklist has no such item")}</div>;
            const tone = itemTone(it, c.good);
            return (
              <div key={c.item} title={it.evidence}>
                <span className="cap-n num">{i + 1}</span>
                <span className="cap-t"><b>{c.label}</b><small>{it.evidence}</small></span>
                <State tone={tone}>{approvedWord(it, approvedTotal)}</State>
              </div>
            );
          })}
        </div>
      </>
    );
  }
  return <Block title="Before the first real order" state={state}>{body}</Block>;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const utcParts = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
// "11 Nov, 19:43", in UTC, as the mockup writes a time.
function dayTime(iso: string): string {
  const p = Object.fromEntries(utcParts.formatToParts(Date.parse(iso)).map((x) => [x.type, x.value]));
  return `${p.day} ${MONTHS[Number(p.month) - 1]}, ${p.hour}:${p.minute}`;
}
const fmtNum = (n: number): string => n.toLocaleString("en-US", { maximumFractionDigits: 6 });

function LimitsBlock({ editing, setEditing }: { editing: boolean; setEditing: (b: boolean) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["real", "limits"], queryFn: getRealLimits });
  const [superseded, setSuperseded] = useState<{ reason: string | null } | null>(null);

  let body: ReactNode;
  let right: ReactNode;
  let state: ReactNode;
  if (q.isError) {
    body = <div className="empty">{couldNotRead("the limits", q.error)}</div>;
  } else if (!q.data) {
    body = <div className="empty">Loading.</div>;
  } else {
    const limits = q.data.limits;
    const set = limits.filter((l) => l.value !== null).length;
    state = <State tone={set === limits.length ? "good" : "warn"}>{set === limits.length ? "All set" : `${set} of ${limits.length} set`}</State>;
    right = (
      <PhoneOff>
        <button type="button" className="btn" onClick={() => setEditing(true)}>Edit</button>
      </PhoneOff>
    );
    const byKey = new Map(limits.map((l) => [l.key, l]));
    body = (
      <>
        <div className="tp-two even cap-lims">
          {GROUPS.map((g) => (
            <div key={g.title}>
              <p className="tp-cap">{g.title}</p>
              {g.keys.map((key) => {
                const l = byKey.get(key);
                const name = <span title={l?.meaning}>{nameOf(key)}</span>;
                return (
                  <div className="pp-lim" key={key}>
                    {name}
                    {!l ? dash("The server sent no row for this limit")
                      : l.value === null ? <b className="warn">Not set</b>
                      : <b className="num">{l.unit === "dollars" ? `$${fmtNum(l.value)}` : `${fmtNum(l.value)} ${l.unit}`}</b>}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        {superseded && (
          <div className="cap-super" title={superseded.reason ?? undefined}>
            <State tone="warn">Your go must be given again</State>
          </div>
        )}
        {editing && (
          <LimitsDialog
            limits={limits}
            onClose={() => setEditing(false)}
            onSaved={(answer) => {
              qc.setQueryData(["real", "limits"], { limits: answer.limits });
              if (answer.superseded_enabling_record_ids.length > 0) setSuperseded({ reason: answer.enabled_reason });
              void qc.invalidateQueries({ queryKey: ["real", "limits"] });
            }}
          />
        )}
      </>
    );
  }
  return (
    <Block title="Server limits" state={state} right={right}>
      {body}
    </Block>
  );
}

const BLOCKED = "Each limit needs a number; a limit cannot be emptied once set.";
const REFUSALS: Record<string, string> = {
  "real-limit-value-invalid": "Each limit must be a number above 0, and orders per window a whole number.",
  "real-limit-unknown-key": "That limit is not one the server knows.",
};

// The server answers 422 with detail "<refusal word>: <why>"; only the word is read here.
function refusalSentence(err: unknown): string {
  if (err instanceof ApiError && err.status === 422) {
    const detail = (err.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      const word = detail.split(":")[0].trim();
      return REFUSALS[word] ?? `The server refused: ${word.replace(/-/g, " ")}`;
    }
  }
  if (err instanceof ApiError && typeof (err.body as { detail?: unknown } | null)?.detail === "string") {
    return detailOf(err);
  }
  return "Could not save the limits.";
}

function LimitsDialog({ limits, onClose, onSaved }: {
  limits: RealLimit[];
  onClose: () => void;
  onSaved: (answer: PutLimitsResponse) => void;
}) {
  const valueOf = (key: string): number | null => limits.find((l) => l.key === key)?.value ?? null;
  const [typed, setTyped] = useState<Record<string, string>>(() =>
    Object.fromEntries(ALL_KEYS.map((k) => [k, String(valueOf(k) ?? "")])),
  );
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);


  async function save() {
    const changed: Record<string, number> = {};
    for (const key of ALL_KEYS) {
      const before = valueOf(key);
      const raw = typed[key].trim();
      if (raw === "") {
        if (before !== null) return setProblem(BLOCKED);
        continue;
      }
      const n = Number(raw);
      if (!Number.isFinite(n)) return setProblem(BLOCKED);
      if (n !== before) changed[key] = n;
    }
    if (Object.keys(changed).length === 0) return onClose();
    setBusy(true);
    setProblem(null);
    let answer: PutLimitsResponse;
    try {
      answer = await putRealLimits(changed);
    } catch (e) {
      setProblem(refusalSentence(e));
      setBusy(false);
      return;
    }
    onClose();
    onSaved(answer);
  }

  return (
    <Window label="Real money limits" size="s" className="cap-dlg" onClose={onClose}>
        <div className="h2"><span>Real money limits</span></div>
        <p>These are the hard limits the server holds for real money. If a save replaces limits that an earlier go was given under, the server asks for your go again. Only the fields you change are sent.</p>
        {ALL_KEYS.map((key) => (
          <div className="cap-frow" key={key}>
            <label htmlFor={`cap-${key}`}>{nameOf(key)}</label>
            <input
              id={`cap-${key}`}
              type="text"
              inputMode="decimal"
              value={typed[key]}
              onChange={(e) => setTyped({ ...typed, [key]: e.target.value })}
            />
          </div>
        ))}
        {problem && <div className="cap-problem rd" role="alert">{problem}</div>}
        <div className="cap-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button type="button" className="btn" disabled={busy} onClick={() => void save()}>Save</button>
        </div>
    </Window>
  );
}

function PhoneOnly({ children }: { children: ReactNode }) {
  return useLayout() === "phone" ? <>{children}</> : null;
}

const DRILL_WORD: Record<string, string> = { "never-drilled": "Never", "ran-and-passed": "Passed", "ran-and-failed": "Failed" };
const DRILL_TONE: Record<string, string> = {
  "never-drilled": "",
  "ran-and-passed": "good",
  "ran-and-failed": "bad",
};

function KillBlock({ accountId, accountError, loaded }: { accountId: string | null; accountError: string | null; loaded: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["real", "kill", accountId],
    queryFn: () => getKillState(accountId as string),
    enabled: typeof accountId === "string",
  });
  const [arming, setArming] = useState(false);
  const [disarming, setDisarming] = useState(false);
  const [verifyProblem, setVerifyProblem] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [holdBusy, setHoldBusy] = useState(false);
  const [holdProblem, setHoldProblem] = useState<string | null>(null);

  // A phone hold sends the same request as ArmDialog, then reads the state again.
  async function holdArm(id: string, action: ArmAction) {
    setHoldBusy(true);
    setHoldProblem(null);
    const refused = await sendArm(id, action);
    if (refused !== null) {
      setHoldProblem(refused);
      setHoldBusy(false);
      return;
    }
    setHoldBusy(false);
    await qc.invalidateQueries({ queryKey: ["real", "kill", id] });
  }

  async function verify(id: string) {
    setVerifying(true);
    setVerifyProblem(null);
    try {
      await verifyKill({ account_id: id });
    } catch (e) {
      setVerifyProblem(killRefusal(e, "Could not check the kill switch.", true));
      setVerifying(false);
      return;
    }
    setVerifying(false);
    await qc.invalidateQueries({ queryKey: ["real", "kill", id] });
  }

  const doing = (
    <div className="cap-does">
      <div><b>Halt</b><p>Stops new orders and cancels open ones. Positions stay as they are.</p></div>
      <div><b>Flatten</b><p>Stops new orders, cancels open ones and also closes every position.</p></div>
    </div>
  );
  const notRead = (word: string, why: string) => (
    <div className="cap-none" title={why}>
      <p className="cap-kword warn">{word}</p>
      <div className="cap-path">
        <Share parts={[["soft", 1, 0.15]]} />
        <div><span>Off</span><span>Armed, not checked</span><span>Armed and checked</span></div>
      </div>
      <div className="tp-fstrip plain"><span><i>Last kill drill</i><b>Not read</b></span><span><i>Last checked</i><b>Not read</b></span><span><i>Last command</i><b>Not read</b></span></div>
      {doing}
    </div>
  );

  let state: ReactNode;
  let body: ReactNode;
  if (accountError !== null) {
    body = <div className="empty">{accountError}</div>;
  } else if (!loaded) {
    body = <div className="empty">Loading.</div>;
  } else if (accountId === null) {
    state = <State tone="dim">Not read</State>;
    body = notRead("No real account configured", NO_ACCOUNT_WHY);
  } else if (q.isError) {
    body = <div className="empty">{couldNotRead("the kill switch", q.error)}</div>;
  } else if (!q.data) {
    body = <div className="empty">Loading.</div>;
  } else {
    const k: KillState = q.data;
    const w = KILL[k.state] ?? ["dim" as NlTone, words(k.state), "", 0] as const;
    const lit = w[3];
    const tone = w[0];
    state = <State tone={tone}>{w[1]}</State>;
    const isArmed = k.state.startsWith("armed-");
    const id: string = accountId;
    body = (
      <>
        <p className={`cap-kword ${tone === "dim" ? "" : tone}`}>{w[1]}</p>
        <p className="cap-plain">{w[2]}</p>
        <div className="cap-path">
          <Share parts={[[tone === "dim" ? "soft" : tone === "run" ? "data" : tone, lit, tone === "dim" ? 0.15 : 0.85], ["soft", 3 - lit, 0.15]]} />
          <div><span className={lit === 1 ? "on" : ""}>Off</span><span className={lit === 2 ? "on" : ""}>Armed, not checked</span><span className={lit === 3 ? "on" : ""}>Armed and checked</span></div>
        </div>
        <div className="tp-fstrip plain">
          <span><i>Last kill drill</i><b className={DRILL_TONE[k.drill_state] ?? ""}>{sentence(k.drill_state)}</b></span>
          <span><i>Last checked</i><b className="num">{k.last_verified_at ? dayTime(k.last_verified_at) : "Never"}</b><small>UTC</small></span>
          <span><i>Last command</i><b>{k.commanded_action ? sentence(k.commanded_action) : "None"}</b></span>
        </div>
        {doing}
        {verifyProblem && <div className="cap-problem rd" role="alert">{verifyProblem}</div>}
        {holdProblem && <div className="cap-problem rd" role="alert">{holdProblem}</div>}
        <PhoneOff>
          <div className="tp-acts">
            {isArmed && (
              <>
                <button type="button" className="btn" disabled={verifying} onClick={() => void verify(k.account_id)}>Check</button>
                <button
                  type="button"
                  className="btn"
                  disabled={k.state !== "armed-verified-effective"}
                  title={k.state !== "armed-verified-effective" ? "Only after a check has seen the switch work" : undefined}
                  onClick={() => setDisarming(true)}
                >
                  Disarm
                </button>
              </>
            )}
            <button type="button" className="btn danger" onClick={() => setArming(true)}>Arm</button>
          </div>
        </PhoneOff>
        <PhoneOnly>
          {isArmed ? (
            <div className="cap-armed">Already armed: disarm it on the laptop.</div>
          ) : (
            <div className="cap-holds">
              <HoldButton label="Hold to halt" disabled={holdBusy} onHold={() => void holdArm(id, "halt")} />
              <HoldButton label="Hold to flatten" disabled={holdBusy} onHold={() => void holdArm(id, "flatten")} />
            </div>
          )}
        </PhoneOnly>
      </>
    );
  }
  return (
    <Block title="Kill switch" state={state} id="cap-kill">
      {body}
      {arming && typeof accountId === "string" && (
        <ArmDialog
          accountId={accountId}
          onClose={() => setArming(false)}
          onDone={async () => {
            setArming(false);
            await qc.invalidateQueries({ queryKey: ["real", "kill", accountId] });
          }}
        />
      )}
      {disarming && typeof accountId === "string" && (
        <DisarmDialog
          accountId={accountId}
          onClose={() => setDisarming(false)}
          onDone={async () => {
            setDisarming(false);
            await qc.invalidateQueries({ queryKey: ["real", "kill", accountId] });
          }}
        />
      )}
    </Block>
  );
}


function DisarmDialog({ accountId, onClose, onDone }: { accountId: string; onClose: () => void; onDone: () => Promise<void> }) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);


  async function send() {
    setBusy(true);
    setProblem(null);
    try {
      await disarmKill({ account_id: accountId });
    } catch (e) {
      setProblem(killRefusal(e, "Could not disarm the kill switch.", false));
      setBusy(false);
      return;
    }
    await onDone();
  }

  return (
    <Window label="Disarm the kill switch" size="s" className="cap-dlg" onClose={onClose}>
        <div className="h2"><span>Disarm the kill switch</span></div>
        {problem && <div className="cap-problem rd" role="alert">{problem}</div>}
        <div className="cap-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            title="Lets orders through again"
            onClick={() => void send()}
          >
            Disarm
          </button>
        </div>
    </Window>
  );
}

// The one arm request: the dialog on the laptop and the hold buttons on the phone both send it.
// Returns the sentence to show when the server refuses, or null when the command went through.
async function sendArm(accountId: string, action: ArmAction): Promise<string | null> {
  try {
    await armKill({ account_id: accountId, action });
  } catch (e) {
    return killRefusal(e, "Could not arm the kill switch.", true);
  }
  return null;
}

function ArmDialog({ accountId, onClose, onDone }: { accountId: string; onClose: () => void; onDone: () => Promise<void> }) {
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);


  async function send(action: ArmAction) {
    setBusy(true);
    setProblem(null);
    const refused = await sendArm(accountId, action);
    if (refused !== null) {
      setProblem(refused);
      setBusy(false);
      return;
    }
    await onDone();
  }

  return (
    <Window label="Arm the kill switch" size="s" className="cap-dlg" onClose={onClose}>
        <div className="h2"><span>Arm the kill switch</span></div>
        {problem && <div className="cap-problem rd" role="alert">{problem}</div>}
        <div className="cap-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            title="Stops new orders and cancels open ones"
            onClick={() => void send("halt")}
          >
            Halt
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            title="Stops new orders, cancels open ones and also closes every position"
            onClick={() => void send("flatten")}
          >
            Flatten
          </button>
        </div>
    </Window>
  );
}
