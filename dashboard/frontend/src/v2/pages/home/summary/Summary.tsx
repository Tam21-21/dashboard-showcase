import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { BurnColumns, Facts, Stair, State, StepRing, Window } from "../../../kit";
import type { NlTone } from "../../../kit";
import { serverDetail } from "../../../../lib/errorText";
import type { EveningSummary } from "../../../../types";
import { useEveningSummary } from "../data";
import { kindOf, short } from "../DayParts";
import { AT, dayMon, dayRow, euro0, euro2, hm, isTonight, longDay, parisStamp, sentence, signed2, states, updatedWords, watchWhy, weekday, whole } from "./model";
import type { Card, Chip, Key, Tone } from "./model";
import "./summary.css";

// The evening Summary, the first block on Home (Tamer 2026-11-19, DECISIONS #496), built as the mockup he chose
// (lab-summary.js block and detail). Made this evening it is open with its cards; made on an earlier day it is folded to the
// head, the sentence and four results. A press on a card or a result opens the numbers behind it.

const RGB: Record<string, string> = { good: "47 224 138", bad: "255 90 99", warn: "255 176 32", soft: "141 152 168", data: "77 155 255" };
const tone = (t: Tone): NlTone => (t === "data" ? "run" : t);
const sty = (v: Record<string, string | number>) => v as CSSProperties;
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
const mins = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

/** A share bar: each part a tone, a size and a strength (the mockup's R2.share). */
function Share({ parts }: { parts: Array<[string, number, number?]> }) {
  const live = parts.filter((p) => p[1] > 0);
  return (
    <span className={`o2-share sm-share${live.length ? "" : " none"}`}>
      {live.map(([c, n, lv], i) => <i key={i} style={sty({ flex: n, "--c": RGB[c], background: `rgb(${RGB[c]} / ${lv ?? (c === "soft" ? 0.3 : 0.85)})` })} />)}
    </span>
  );
}

const Top = ({ name, chip }: { name: string; chip: Chip | null }) => (
  <span className="x-toprow"><span className="t-name">{name}</span>{chip && <State tone={tone(chip[0])}>{chip[1]}</State>}</span>
);
const Figrow = ({ fig, unit }: { fig: string; unit?: string }) => (
  <span className="x-figrow"><span className="fig num">{fig}</span>{unit ? <span className="x-unit">{unit}</span> : null}</span>
);
const Facts3 = ({ list }: { list: Array<[string, string]> }) => (
  <span className={`t-chg x-f${list.length}`}>{list.map(([a, v]) => <span key={a}><i>{a}</i><b>{v}</b></span>)}</span>
);

export default function Summary() {
  const q = useEveningSummary();
  const [fold, setFold] = useState<"open" | "shut" | null>(null);
  const [picked, setPicked] = useState<Key | "plan" | null>(null);
  const s = q.data?.summary ?? null;
  return (
    <section className="nl-blk sm" id="hmSum" aria-label="Summary">
      {s === null && <Head />}
      {q.isError ? <p className="sm-say sm-note">{serverDetail(q.error)}</p>
        : q.isPending ? <p className="sm-say sm-note">Loading</p>
        : s === null ? <p className="sm-say sm-note">The first Summary is made tonight at {AT}.</p>
        : <Filled s={s} fold={fold} setFold={setFold} onPick={setPicked} />}
      {picked && s && <Detail s={s} k={picked} onClose={() => setPicked(null)} />}
    </section>
  );
}

function Head({ s, words, tonight, lost }: { s?: EveningSummary; words?: string; tonight?: boolean; lost?: string | null }) {
  return (
    <div className="sec-head sm-head">
      <div className="h">
        <h2>Summary</h2>
        {s && <span className="sm-date">{longDay(s.day)}</span>}
        {words && <State tone={tonight ? "good" : "dim"} hover={tonight ? `Refreshed every evening at ${AT}, when your phone gets the notification` : `Last evening's Summary. The next one comes tonight at ${AT}`}>{words}</State>}
        {lost && <State tone="warn">{lost}</State>}
      </div>
    </div>
  );
}

function Filled({ s, fold, setFold, onPick }: { s: EveningSummary; fold: "open" | "shut" | null; setFold: (f: "open" | "shut") => void; onPick: (k: Key | "plan") => void }) {
  const now = new Date();
  const tonight = isTonight(s, now), open = fold === "open" || (fold !== "shut" && tonight);
  const st = states(s, now), m = s.money, b = s.body, dn = weekday(s.day);
  const words = updatedWords(s, now);
  const lost = !m && !b ? "Bank and watch not read" : !m ? "Bank not read" : !b ? "Watch not synced" : null;
  const N = { today: "Out of your accounts", month: "Out this month", burn: tonight ? "Calories burned" : `Burned on ${dn}`, steps: tonight ? "Steps" : `Steps on ${dn}`, plan: tonight ? "Tomorrow's plan" : "Today's plan", round: "Round-ups" };
  const bank = m ? `What left your current accounts between two reads of Finary. Banks as of ${parisStamp(m.bank_as_of)}.` : "";
  const TIP: Record<string, string> = {
    today: bank, month: bank,
    burn: "One column a day: the seven days before, then the day itself in white. The pale part was burned at rest, the bright part by moving. The dashed line is your average over the seven days before",
    steps: "A full ring is your goal. A second, lighter lap is what you walked beyond it. The goal is yours to set in Settings",
    plan: "The strip is the day from 06:00 to midnight, each event in the colour of its kind. Under it: the events in time order, the day's to-dos, then late to-dos carried over",
    round: "Your bank rounds each card payment up and invests the difference. The line is the total, one step a day this month",
  };
  const miss = (k: Key, hue: string) => (
    <button key={k} type="button" className={`mk-tile x-tile sm-card sm-empty${k === "round" ? " sm-small" : " sm-num"}`} data-k={k} data-hue={hue} title={TIP[k]} onClick={() => onPick(k)}>
      <Top name={N[k]} chip={st.miss[k]} /><Figrow fig={"–"} /><span className="sm-why">{st.why[k]}</span>
    </button>
  );

  const cards = () => {
    const toneM = st.tone as Tone;
    const at = m ? Math.min(100, (m.pace / m.budget) * 100) : 0;
    const today = m && st.today ? (
      <button type="button" className="mk-tile x-tile sm-card sm-num" data-k="today" data-hue={st.today.chip?.[0]} title={TIP.today} onClick={() => onPick("today")}>
        <Top name={N.today} chip={st.today.chip} /><Figrow fig={st.today.fig} unit={st.today.unit} />
        <span className="sm-pic">{m.under ? <Share parts={[["good", m.today], ["soft", Math.max(0, m.share - m.today), 0.15]]} /> : <Share parts={[["bad", m.share, 0.5], ["bad", m.today - m.share, 1]]} />}</span>
      </button>
    ) : miss("today", "");
    const month = m && st.month ? (
      <button type="button" className="mk-tile x-tile sm-card sm-num" data-k="month" data-hue={toneM} title={TIP.month} onClick={() => onPick("month")}>
        <Top name={N.month} chip={st.month.chip} /><Figrow fig={st.month.fig} unit={st.month.unit} />
        {m.since && <span className="sm-since">{`since ${dayMon(m.since)}`}</span>}
        <span className="sm-pic sm-pace">
          <span className={`sm-pin${at < 26 ? " l" : at > 74 ? " r" : ""}`} style={{ left: `${at.toFixed(1)}%` }}><b>{`By ${tonight ? "today" : dn} `}<span className="num">{euro0(m.pace)}</span></b></span>
          <Share parts={[[toneM, Math.min(m.spent, m.budget)], ["soft", Math.max(0, m.budget - m.spent), 0.15]]} />
        </span>
      </button>
    ) : miss("month", "");
    const burn = b && st.burn ? (
      <button type="button" className="mk-tile x-tile sm-card sm-num" data-k="burn" data-hue="data" title={TIP.burn} onClick={() => onPick("burn")}>
        <Top name={N.burn} chip={st.burn.chip} /><Figrow fig={st.burn.fig} unit={st.burn.unit} />
        <span className="sm-pic sm-cols"><BurnColumns days={b.days} avg={b.avg_kcal} /></span>
        <Facts3 list={[["At rest", whole(b.rest)], ["By moving", whole(b.move)], ...(b.avg_kcal != null ? [["Your average", whole(b.avg_kcal)] as [string, string]] : [])]} />
      </button>
    ) : miss("burn", "warn");
    const steps = b && st.steps ? (
      <button type="button" className="mk-tile x-tile sm-card sm-num" data-k="steps" data-hue={st.steps.chip?.[0]} title={TIP.steps} onClick={() => onPick("steps")}>
        <Top name={N.steps} chip={st.steps.chip} /><Figrow fig={st.steps.fig} unit={st.steps.unit} />
        <span className="sm-pic sm-ring"><StepRing steps={b.steps} goal={b.goal} /></span>
        <Facts3 list={[...(b.avg_steps != null ? [["Your average", whole(b.avg_steps)] as [string, string]] : []), ...(b.prev != null ? [["The day before", whole(b.prev)] as [string, string]] : [])]} />
      </button>
    ) : miss("steps", "warn");
    const r = s.round;
    const round = r && st.round ? (
      <button type="button" className="mk-tile x-tile sm-card sm-small" data-k="round" data-hue="data" title={TIP.round} onClick={() => onPick("round")}>
        <Top name={N.round} chip={st.round.chip} />
        <span className="sm-rrow"><Figrow fig={st.round.fig} unit={st.round.unit} /><span className="sm-pic sm-stair"><Stair days={r.days} total={r.total} /></span></span>
      </button>
    ) : miss("round", "");
    return <div className="sm-band"><div className="sm-col">{today}{month}</div><div className="sm-col">{burn}</div><div className="sm-col">{steps}</div><div className="sm-col"><PlanCard s={s} name={N.plan} tip={TIP.plan} onPick={() => onPick("plan")} />{round}</div></div>;
  };

  const strip = () => (
    <div className="sm-strip">
      {([["today", "Out"], ["month", "This month"], ["burn", "Burned"], ["steps", "Steps"]] as Array<[Key, string]>).map(([k, name], i) => {
        const c: Card = st[k];
        const hue = c ? (c.chip && c.chip[0] !== "data" ? c.chip[0] : "data") : st.miss[k][0] === "warn" ? "warn" : "";
        return (
          <button key={k} type="button" className="mk-tile x-tile sm-cell" data-k={k} data-hue={hue || undefined} style={sty({ "--i": i })} onClick={() => onPick(k)}>
            <Top name={name} chip={null} /><Figrow fig={c ? c.fig : "–"} unit={c ? c.unit : ""} />
            {c ? (c.chip ? <State tone={tone(c.chip[0])}>{c.chip[1]}</State> : null) : <State tone={tone(st.miss[k][0])}>{st.miss[k][1]}</State>}
          </button>
        );
      })}
    </div>
  );

  return (
    <>
      <Head s={s} words={words} tonight={tonight} lost={lost} />
      <button type="button" className={`btn sm-fold${open ? "" : " primary"}`} aria-expanded={open} onClick={() => setFold(open ? "shut" : "open")}>{open ? "Hide the cards" : "Show the cards"}</button>
      <p className="sm-say">{sentence(s, now).map((p, i) => (p.tone ? <em key={i} className={p.tone}>{p.text}</em> : <span key={i}>{p.text}</span>))}</p>
      {open ? cards() : strip()}
    </>
  );
}

/** Tomorrow's plan: the day as one strip, then what must be read. */
function PlanCard({ s, name, tip, onPick }: { s: EveningSummary; name: string; tip: string; onPick: () => void }) {
  const p = s.plan;
  const timed = p.events.filter((e) => !e.all_day);
  const first = timed[0] ?? null;
  const rows: Array<[string, string, "ev" | "todo" | "late"]> = [
    ...p.events.map((e): [string, string, "ev"] => [e.all_day ? "All day" : hm(e.start), short(e.title), "ev"]),
    ...p.todos.map((t): [string, string, "todo"] => ["To do", t, "todo"]),
    ...p.late.map((t): [string, string, "late"] => ["Late", t, "late"]),
  ];
  const shown = rows.length > 4 ? rows.slice(0, 3) : rows, more = rows.length - shown.length;
  const pos = (q: number) => Math.max(0, Math.min(100, ((q - 360) / 1080) * 100));
  const chip: Chip = p.late.length ? ["warn", `${p.late.length} late`] : first ? ["data", `Starts ${hm(first.start)}`] : rows.length ? ["data", plural(rows.length, "thing")] : ["dim", "Free"];
  return (
    <button type="button" className="mk-tile x-tile sm-card sm-plan" data-k="plan" data-hue={p.late.length ? "warn" : "data"} title={tip} onClick={onPick}>
      <Top name={name} chip={chip} />
      {timed.length > 0 && (
        <>
          <span className="sm-day">{timed.map((e, i) => {
            const a = mins(hm(e.start)), z = mins(hm(e.end)) || 1440;
            return <i key={i} style={sty({ left: `${pos(a).toFixed(1)}%`, width: `${Math.max(2, pos(z) - pos(a)).toFixed(1)}%`, "--k": kindOf(e.title)[2] })} />;
          })}</span>
          <span className="sm-hours"><i>06:00</i><i>12:00</i><i>18:00</i><i>24:00</i></span>
        </>
      )}
      {rows.length ? (
        <span className="sm-list">
          {shown.map(([w, t, kind], i) => <span key={i} className="sm-li"><b className={kind === "ev" ? "num" : kind === "late" ? "warn" : ""}>{w}</b><span>{t}</span></span>)}
          {more > 0 && <span className="sm-li more"><b /><span>{`and ${more} more`}</span></span>}
        </span>
      ) : <span className="sm-why">Nothing is on the calendar and no to-do is open.</span>}
    </button>
  );
}

const Lead = ({ chip, children }: { chip: Chip | null; children: ReactNode }) => (
  <p className="sm-lead">{chip && <State tone={tone(chip[0])}>{chip[1]}</State>}<span>{children}</span></p>
);

/** The numbers behind a card, in the kit's window: what the mockup's detail lists for that key. */
function Detail({ s, k, onClose }: { s: EveningSummary; k: Key | "plan"; onClose: () => void }) {
  const navigate = useNavigate();
  const now = new Date();
  const st = states(s, now), tonight = isTonight(s, now), dn = weekday(s.day), m = s.money, b = s.body, p = s.plan;
  const names = { today: "Out of your accounts", month: "Out this month", burn: tonight ? "Calories burned" : `Burned on ${dn}`, steps: tonight ? "Steps" : `Steps on ${dn}`, plan: tonight ? "Tomorrow's plan" : "Today's plan", round: "Round-ups" };
  const go = (to: string, label: string) => <button type="button" className="btn" onClick={() => { onClose(); navigate(to); }}>{label}</button>;
  const BUDGET = "/app/portfolio/budget", SPORTS = "/app/home/sports", POSITIONS = "/app/portfolio/positions";
  let body: ReactNode;
  if (k === "today" || k === "month") {
    if (!m) body = <><Lead chip={["dim", "Not read"]}>{longDay(s.day)}</Lead><p className="sm-note">No bank has been read yet, so nothing is shown and nothing is guessed. Your budget for the month is known and is on the Budget page.</p><div className="sm-acts">{go(BUDGET, "Open Budget")}</div></>;
    else if (k === "today") body = (
      <>
        <Lead chip={st.today?.chip ?? null}>{longDay(s.day)}</Lead>
        <Facts items={[["Out of your accounts", euro2(m.today)], ["Share for the day", euro2(m.share)], [m.under ? "Left" : "Over", euro2(Math.abs(m.gap)), m.under ? "good" : "bad"]]} />
        <h3 className="sm-h3">How the share is worked out</h3>
        <Facts items={[["Budget for the month", euro0(m.budget)], [`Out before ${tonight ? "today" : dn}`, euro2(m.before)], ["Days left, this one included", String(m.left)], ["Share for the day", euro2(m.share)]]} />
        <p className="sm-note">What left your current accounts between two reads of Finary, banks as of {parisStamp(m.bank_as_of)}. The share is what is left of the month's budget, divided by the days left. It shrinks after an expensive day and grows after a cheap one.</p>
        <div className="sm-acts">{go(BUDGET, "Open Budget")}</div>
      </>
    );
    else {
      const dom = Number(s.day.slice(8, 10)), dim = new Date(Date.UTC(Number(s.day.slice(0, 4)), Number(s.day.slice(5, 7)), 0)).getUTCDate();
      const proj = (m.spent / dom) * dim;
      body = (
        <>
          <Lead chip={st.month?.chip ?? null}>{longDay(s.day)}</Lead>
          <Facts items={[["Out so far", euro0(m.spent)], ["Budget for the month", euro0(m.budget)], [`An even pace by day ${dom} of ${dim}`, euro0(m.pace)], ["Month end at this pace", euro0(proj), proj > m.budget ? "warn" : "good"], ["Left in the budget", euro0(Math.max(0, m.budget - m.spent))], ["Days left", String(dim - dom)]]} />
          <p className="sm-note">An even pace is the budget spread over the days of the month. "Ahead of pace" starts 10% above it, the rule the Budget page uses.{m.since ? ` Counted since ${dayMon(m.since)}.` : ""}</p>
          <div className="sm-acts">{go(BUDGET, "Open Budget")}</div>
        </>
      );
    }
  } else if (k === "burn" || k === "steps") {
    if (!b) body = <><Lead chip={["warn", "Watch behind"]}>{longDay(s.day)}</Lead><p className="sm-note">{watchWhy(s)} Nothing is filled in for the days it has not sent.</p><div className="sm-acts">{go(SPORTS, "Open Sports")}</div></>;
    else {
      const days = [...b.days].reverse();
      const rowsOf = (cols: string[], cells: (d: (typeof days)[number]) => ReactNode[]) => (
        <table className="sm-tbl">
          <thead><tr>{cols.map((c, i) => <th key={c} className={i ? "num" : undefined}>{c}</th>)}</tr></thead>
          <tbody>{days.map((d, i) => <tr key={d.d}><td>{i === 0 ? <b>{dayRow(d.d)}</b> : dayRow(d.d)}</td>{cells(d).map((c, j) => <td key={j} className="num">{c}</td>)}</tr>)}</tbody>
        </table>
      );
      if (k === "burn") body = (
        <>
          <Lead chip={st.burn?.chip ?? null}>{longDay(s.day)}</Lead>
          <Facts items={[["Burned", `${whole(b.kcal)} kcal`], ["At rest", whole(b.rest)], ["By moving", whole(b.move)], ...(b.avg_kcal != null ? [["Average of the 7 days before", whole(b.avg_kcal)] as [string, string]] : [])]} />
          <h3 className="sm-h3">Day by day</h3>
          {rowsOf(["Day", "At rest", "By moving", "Burned"], (d) => (d.rest == null || d.move == null ? ["–", "–", "–"] : [whole(d.rest), whole(d.move), <b key="t">{whole(d.rest + d.move)}</b>]))}
          <p className="sm-note">As the watch counts it. At rest is what your body burns doing nothing; by moving is everything on top.</p>
          <div className="sm-acts">{go(SPORTS, "Open Sports")}</div>
        </>
      );
      else body = (
        <>
          <Lead chip={st.steps?.chip ?? null}>{longDay(s.day)}</Lead>
          <Facts items={[["Steps", whole(b.steps)], ["Your goal", whole(b.goal)], [b.reached ? "Beyond the goal" : "Short of the goal", whole(Math.abs(b.short)), b.reached ? "good" : "warn"], ...(b.avg_steps != null ? [["Average of the 7 days before", whole(b.avg_steps)] as [string, string]] : []), ...(b.prev != null ? [["The day before", whole(b.prev)] as [string, string]] : [])]} />
          <h3 className="sm-h3">Day by day</h3>
          {rowsOf(["Day", "Steps", "Against the goal"], (d) => (d.steps == null ? ["–", "–"] : [whole(d.steps), <span key="g" className={d.steps >= b.goal ? "good" : "warn"}>{d.steps >= b.goal ? "+" : "−"}{whole(Math.abs(d.steps - b.goal))}</span>]))}
          <div className="sm-acts">{go(SPORTS, "Open Sports")}{go("/app/settings", "Change the goal")}</div>
        </>
      );
    }
  } else if (k === "round") {
    const r = s.round;
    body = r ? (
      <>
        <Lead chip={st.round?.chip ?? null}>{longDay(s.day)}</Lead>
        <Facts items={[[tonight ? "Invested today" : `Invested on ${dn}`, signed2(r.today)], ["Invested this month", euro2(r.month)], ["In total", euro2(r.total)]]} />
        <p className="sm-note">Your bank rounds each card payment up to the next euro and invests the difference. The figures are the change of your round-ups account between two reads of Finary, so a rise or fall in its value counts too.</p>
        <div className="sm-acts">{go(POSITIONS, "Open Positions")}</div>
      </>
    ) : (
      <>
        <Lead chip={["dim", "Not read"]}>{longDay(s.day)}</Lead>
        <p className="sm-note">{st.why.round} Your round-ups account was not in a Finary read on this day and an earlier one, so none is shown and none is guessed.</p>
        <div className="sm-acts">{go(POSITIONS, "Open Positions")}</div>
      </>
    );
  } else {
    const whole2 = p.events.filter((e) => e.all_day), timed = p.events.filter((e) => !e.all_day);
    const first = timed[0] ?? null;
    const next = new Date(Date.parse(`${s.day}T12:00:00Z`) + 864e5).toISOString().slice(0, 10);
    const dur = (n: number) => (n < 60 ? `${n} min` : n % 60 ? `${Math.floor(n / 60)} h ${String(n % 60).padStart(2, "0")}` : `${n / 60} h`);
    body = (
      <>
        <p className="sm-lead">{first && <State tone="run">{`Starts ${hm(first.start)}`}</State>}{p.late.length > 0 && <State tone="warn">{`${p.late.length} late`}</State>}<span>{longDay(next)}</span></p>
        <h3 className="sm-h3">On the calendar</h3>
        {p.events.length ? (
          <table className="sm-tbl">
            <thead><tr><th>When</th><th>What</th><th className="num">How long</th></tr></thead>
            <tbody>
              {whole2.map((e, i) => <tr key={`a${i}`}><td><span className="num">All day</span></td><td>{short(e.title)}</td><td className="num">{"–"}</td></tr>)}
              {timed.map((e, i) => { const a = mins(hm(e.start)), z = mins(hm(e.end)) || 1440; return <tr key={`t${i}`}><td><span className="num">{`${hm(e.start)} to ${hm(e.end)}`}</span></td><td>{short(e.title)}</td><td className="num">{dur(z - a)}</td></tr>; })}
            </tbody>
          </table>
        ) : <p className="sm-note">Nothing is on the calendar.</p>}
        <h3 className="sm-h3">To do</h3>
        {p.todos.length || p.late.length ? (
          <div className="sm-list">
            {p.todos.map((t, i) => <span key={`d${i}`} className="sm-li"><b>To do</b><span>{t}</span></span>)}
            {p.late.map((t, i) => <span key={`l${i}`} className="sm-li"><b className="warn">Late</b><span>{t}</span></span>)}
          </div>
        ) : <p className="sm-note">No to-do is open.</p>}
        <div className="sm-acts">{go("/app/calendar/calendar", "Open the calendar")}</div>
      </>
    );
  }
  return <Window label={names[k]} title={names[k]} size="l" className="sm-win" onClose={onClose}>{body}</Window>;
}
