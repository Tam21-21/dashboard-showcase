import { useEffect, useState } from "react";
import { api } from "../../../lib/api";
import { serverDetail } from "../../../lib/errorText";
import type { BudgetConfig } from "../../../types";
import { Block, State, type NlTone } from "../../kit";
import { canPush, readState, turnOff, turnOn, type PushStateWord } from "./push";
import "./evening.css";

// The words of the notification, as the mockup's lineOf writes them (the server's push.py writes the same).
const TITLE = "Evening Summary";
const TAIL = "Open the app for all the details.";
const AT = "22:00";
function lineOf(budget: boolean | null, steps: boolean | null): string {
  const b = budget === null ? null : budget ? "on track with your budget" : "over on your budget";
  const s = steps === null ? null : steps ? "on track with your steps" : "short on your steps";
  const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
  let lead: string;
  if (b !== null && s !== null) {
    if (budget && steps) lead = "On track with your budget and your steps.";
    else if (!budget && !steps) lead = "Over on your budget and short on your steps.";
    else lead = `${cap(budget ? b : s)}, ${budget ? s : b}.`;
  } else if (b !== null) lead = `${cap(b)}. Your watch has not synced.`;
  else if (s !== null) lead = `${cap(s)}. The bank was not read.`;
  else lead = "Nothing could be read tonight.";
  return `${lead} ${TAIL}`;
}
const CASES: Array<[string, string]> = ([
  ["Budget kept, step goal reached", true, true],
  ["Over budget, step goal reached", false, true],
  ["Budget kept, step goal missed", true, false],
  ["Over budget, step goal missed", false, false],
  ["Watch not synced", true, null],
  ["Bank not read", null, true],
  ["Nothing read", null, null],
] as Array<[string, boolean | null, boolean | null]>).map(([when, b, s]) => [when, lineOf(b, s)]);

const STATE: Record<PushStateWord, { tone: NlTone; word: string }> = {
  off: { tone: "dim", word: "Off" },
  on: { tone: "good", word: "On" },
  refused: { tone: "warn", word: "Refused" },
};
const SAY: Record<PushStateWord, string> = {
  off: `Turn it on and your phone gets one notification every evening at ${AT}, when the Summary on Home is refreshed. A tap on it opens Home with the Summary on top.`,
  on: `Your phone gets one notification every evening at ${AT}. A tap on it opens Home with the Summary on top.`,
  refused: "To allow it, open the iPhone's Settings, tap Notifications, pick this app and switch on Allow Notifications.",
};
const BUTTON: Record<PushStateWord, string> = { off: "Turn on evening summary", on: "Turn off evening summary", refused: "Try again" };
const NO_PUSH = "Open the app from its Home Screen icon on your iPhone to turn this on.";

const LONG_DAY = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Paris", weekday: "long", day: "numeric", month: "long" });

// The app's mark for the banner's icon; the ids carry a prefix so no other svg on the page can take them.
function Mark() {
  const arcs: Array<[string, string, string, string, string]> = [
    ["M40.78,32.92 A19,19 0 0 1 30.15,41.98", "40.78", "32.92", "30.15", "41.98"],
    ["M27.24,42.72 A19,19 0 0 1 13.57,39.88", "27.24", "42.72", "13.57", "39.88"],
    ["M11.20,38.04 A19,19 0 0 1 5.06,25.50", "11.20", "38.04", "5.06", "25.50"],
    ["M5.06,22.51 A19,19 0 0 1 11.20,9.96", "5.06", "22.51", "11.20", "9.96"],
    ["M13.56,8.12 A19,19 0 0 1 27.23,5.28", "13.56", "8.12", "27.23", "5.28"],
    ["M30.14,6.02 A19,19 0 0 1 40.77,15.07", "30.14", "6.02", "40.77", "15.07"],
  ];
  const stops = ["#27b4e8", "#28a0e7", "#2a8ce5", "#2b78e4", "#2b6add", "#2a5dd7", "#2a4fd0"];
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true">
      <defs>
        <linearGradient id="evmark-t" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#f7a51f" /><stop offset="1" stopColor="#e85d0f" /></linearGradient>
        {arcs.map(([, x1, y1, x2, y2], i) => (
          <linearGradient key={i} id={`evmark-e${i}`} gradientUnits="userSpaceOnUse" x1={x1} y1={y1} x2={x2} y2={y2}>
            <stop offset="0" stopColor={stops[i]} /><stop offset="1" stopColor={stops[i + 1]} />
          </linearGradient>
        ))}
      </defs>
      <g fill="none">
        {arcs.map(([d], i) => <path key={i} d={d} stroke={`url(#evmark-e${i})`} strokeWidth="4.2" />)}
        <circle cx="24" cy="24" r="12.6" stroke="#2b78e4" strokeWidth="1.2" opacity="0.6" />
      </g>
      <g fill="url(#evmark-t)"><rect x="15.4" y="16.6" width="17.2" height="3.6" /><rect x="22.2" y="16.6" width="3.6" height="15.6" /></g>
    </svg>
  );
}

// Only a plain number is read: a sign or a letter is refused, never dropped (a dropped minus would save a wrong number).
const WHOLE = /^\d+$/;
const MONEY = /^\d+(\.\d+)?$/;

export default function Evening() {
  const can = canPush();
  const [state, setState] = useState<PushStateWord>("off");
  const [keyOk, setKeyOk] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [goal, setGoal] = useState("");
  const [goalProblem, setGoalProblem] = useState<string | null>(null);
  const [budget, setBudget] = useState("");
  const [budgetConfig, setBudgetConfig] = useState<BudgetConfig | null>(null);
  const [budgetProblem, setBudgetProblem] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    if (can) {
      api.life.pushKey().then(
        () => { if (live) setKeyOk(true); },
        (e: unknown) => { if (live) setProblem(serverDetail(e)); },
      );
      readState().then(
        (s) => { if (live) setState(s); },
        (e: unknown) => { if (live) setProblem(serverDetail(e)); },
      );
    }
    api.life.summarySettings().then(
      (v) => { if (live) setGoal(String(v.steps_goal)); },
      (e: unknown) => { if (live) setGoalProblem(serverDetail(e)); },
    );
    api.life.getBudgetConfig().then(
      (v) => { if (live) { setBudgetConfig(v); setBudget(String(v.monthly_budget)); } },
      (e: unknown) => { if (live) setBudgetProblem(serverDetail(e)); },
    );
    return () => { live = false; };
    // The calls are fixed api functions, so the section reads once on mount.
  }, [can]);

  const run = async (work: () => Promise<void>, fail: (words: string) => void) => {
    if (busy) return;
    setBusy(true);
    try { await work(); } catch (e: unknown) { fail(serverDetail(e)); } finally { setBusy(false); }
  };

  const press = () => run(async () => {
    setProblem(null); setNote(null);
    const next = state === "on" ? await turnOff() : await turnOn();
    setState(next);
    if (state !== "on" && next === "off") setNote("The phone did not allow it yet. Press the button to ask again.");
  }, setProblem);
  const test = () => run(async () => {
    setProblem(null); setNote(null);
    const r = await api.life.pushTest();
    setNote(r.sent > 0 ? "Sent. It should arrive in a few seconds." : "Nothing was sent. No phone took it.");
  }, setProblem);
  const saveGoal = () => run(async () => {
    setGoalProblem(null);
    const text = goal.trim();
    if (!WHOLE.test(text)) { setGoalProblem("Type the steps as a whole number, with digits only."); return; }
    const r = await api.life.saveSummarySettings({ steps_goal: Number(text) });
    setGoal(String(r.steps_goal));
  }, setGoalProblem);
  const saveBudget = () => run(async () => {
    setBudgetProblem(null);
    if (budgetConfig === null) return;
    const text = budget.trim();
    if (!MONEY.test(text)) { setBudgetProblem("Type the budget in euros, with digits and at most one dot."); return; }
    const r = await api.life.updateBudgetConfig({
      monthly_budget: Number(text), fixed_expenses: budgetConfig.fixed_expenses, manual_monthly_spent: budgetConfig.manual_monthly_spent,
    });
    setBudgetConfig(r);
    setBudget(String(r.monthly_budget));
  }, setBudgetProblem);

  const st = STATE[state];
  const off = busy || !can || (state !== "on" && !keyOk);
  const say = can ? SAY[state] : NO_PUSH;
  return (
    <Block
      className="st-sec"
      id="stEve"
      title="Evening summary"
      state={<span title="One notification a day. On an iPhone it needs this app on the Home Screen"><State tone={st.tone}>{st.word}</State></span>}
    >
      <div className="sm-set">
        <div>
          <p className="sm-set-say">{say}</p>
          <div className="x-acts">
            <button type="button" className={state === "on" ? "btn" : "btn primary"} disabled={off} onClick={press}>{BUTTON[state]}</button>
            {state === "on" && <button type="button" className="btn" disabled={busy} onClick={test}>Send a test</button>}
          </div>
          {problem !== null && <p className="st-evbad" role="alert">{problem}</p>}
          {note !== null && <p className="st-evnote" role="status">{note}</p>}

          <h3 className="x-h3">Daily step goal</h3>
          <div className="sm-goal">
            <input className="fin search" inputMode="numeric" aria-label="Daily step goal" value={goal} onChange={(e) => setGoal(e.target.value)} />
            <span className="x-unit">steps a day</span>
            <button type="button" className="btn" aria-label="Save the step goal" disabled={busy} onClick={saveGoal}>Save</button>
          </div>
          {goalProblem !== null && <p className="st-evbad" role="alert">{goalProblem}</p>}

          <h3 className="x-h3">Monthly budget</h3>
          <div className="sm-goal">
            <input className="fin search" inputMode="decimal" aria-label="Monthly budget" value={budget} onChange={(e) => setBudget(e.target.value)} />
            <span className="x-unit">euros a month</span>
            <button type="button" className="btn" aria-label="Save the monthly budget" disabled={busy || budgetConfig === null} onClick={saveBudget}>Save</button>
          </div>
          {budgetProblem !== null && <p className="st-evbad" role="alert">{budgetProblem}</p>}

          <h3 className="x-h3">What the notification says</h3>
          <table className="st-tbl sm-words">
            <thead><tr><th>When</th><th>{`The line under "${TITLE}"`}</th></tr></thead>
            <tbody>{CASES.map(([when, line]) => <tr key={when}><td>{when}</td><td>{line}</td></tr>)}</tbody>
          </table>
        </div>
        <div>
          <div className="sm-lock" role="img" aria-label="The notification on an iPhone lock screen">
            <span className="sm-lock-date">{LONG_DAY.format(new Date())}</span>
            <span className="sm-lock-time">{AT}</span>
            <div className="sm-note">
              <span className="sm-note-ic"><Mark /></span>
              <span>
                <span className="sm-note-h"><b>{TITLE}</b><i>now</i></span>
                <span className="sm-note-b">{CASES[0][1]}</span>
              </span>
            </div>
          </div>
          <p className="st-note sm-lock-say">The banner is the iPhone's own and cannot be styled. Only the words are ours.</p>
        </div>
      </div>
    </Block>
  );
}
