import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Block, PageHeader, Say, State, Window } from "../../kit";
import type { NlTone } from "../../kit";
import { PhoneOff } from "../../kit/phone";
import type { PageProps } from "../../shell/sections";
import { deleteTarget, getBudget, putTarget } from "../../data/moneyApi";
import type { BudgetCategory, BudgetMonth, MoneyBudget } from "../../data/moneyApi";
import { ApiError } from "../../../lib/api";
import { Card, NeedsBlock, Share, Wall, ZeroBars, dotStyle, useHide } from "../trading/parts";
import type { Stop } from "../trading/parts";
import { categoryLabel, eur as eurPlain, monthName, pct0, readTitle } from "./moneyCharts";
import "./portfolio.css";

// Budget: each category's spending this month against Tamer's own plan and against where it should
// be by today, where last month's money went, and twelve months in and out. The plan is Tamer's
// (budget_targets), edited here; Finary's own targets are not used.

const NO_SYNC = "Finary has not synced yet";
const SRC_BUDGET = "Finary's budget for this month, against your plan";
const RGB = ["47 224 138", "236 110 205", "77 155 255", "150 128 255", "255 146 110", "40 200 235", "232 212 154", "150 160 180"];

function planWords(e: unknown): string {
  if (e instanceof ApiError) {
    const b = e.body;
    if (b && typeof b === "object" && "detail" in b && typeof (b as { detail: unknown }).detail === "string") {
      return (b as { detail: string }).detail;
    }
    return "The plan could not be saved.";
  }
  throw e;
}

function PlanDialog({ categories, onClose, onChanged }: {
  categories: BudgetCategory[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(categories.map((c) => [c.slug, c.target === null ? "" : String(c.target)])),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (slug: string, act: () => Promise<unknown>, after?: () => void) => {
    setBusy(slug);
    setError(null);
    try {
      await act();
      after?.();
      onChanged();
    } catch (e: unknown) {
      setError(planWords(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Window label="Monthly plan" size="l" className="mn-dlg" backdropCloses={false} escapeCloses={false} onClose={onClose}>
        <h2>Monthly plan</h2>
        <p>Your own plan for each category, in euros a month. Saving a plan moves no money. Use copies the average of the last 12 months into the field.</p>
        {categories.map((c) => {
          const label = categoryLabel(c.label);
          const text = values[c.slug] ?? "";
          const n = Number(text);
          const valid = text.trim() !== "" && Number.isFinite(n) && n >= 0;
          return (
            <div className="mn-row" key={c.slug}>
              <span title={c.label ? undefined : c.slug}>{label}</span>
              <input
                className="fin"
                type="number"
                min={0}
                step="any"
                aria-label={`Plan for ${label}`}
                value={text}
                onChange={(e) => setValues({ ...values, [c.slug]: e.target.value })}
              />
              <span className="mn-avg" title="The average monthly spending over the last 12 months">
                {c.suggestion === null ? (
                  <span title="Finary gave no 12-month average for this category">-</span>
                ) : (
                  <>
                    <span className="num">{eurPlain(c.suggestion)}</span>
                    <button
                      type="button"
                      className="btn q"
                      aria-label={`Use ${label}`}
                      onClick={() => setValues({ ...values, [c.slug]: String(Math.round(c.suggestion as number)) })}
                    >
                      Use
                    </button>
                  </>
                )}
              </span>
              <button
                type="button"
                className="btn"
                aria-label={`Save ${label}`}
                disabled={!valid || busy !== null}
                onClick={() => run(c.slug, () => putTarget(c.slug, n))}
              >
                Save
              </button>
              {c.target !== null ? (
                <button
                  type="button"
                  className="btn danger"
                  aria-label={`Clear ${label}`}
                  disabled={busy !== null}
                  onClick={() => run(c.slug, () => deleteTarget(c.slug), () => setValues((v) => ({ ...v, [c.slug]: "" })))}
                >
                  Clear
                </button>
              ) : (
                <span />
              )}
            </div>
          );
        })}
        {error !== null && <div className="mn-err">{error}</div>}
        <div className="mn-foot">
          <button type="button" className="btn q" onClick={onClose}>Close</button>
        </div>
    </Window>
  );
}

type Status = "ok" | "ahead" | "over" | "none";
const STATUS: Record<Status, [NlTone, string]> = { ok: ["good", "On pace"], ahead: ["warn", "Ahead of pace"], over: ["bad", "Over the plan"], none: ["dim", "No plan"] };

function statusOf(c: BudgetCategory): Status {
  if (c.target === null) return "none";
  if (c.spent > c.target) return "over";
  return c.warn || (c.ahead !== null && c.ahead > 0.1) ? "ahead" : "ok";
}

function YearBars({ months, eur }: { months: BudgetMonth[]; eur: (n: number) => string }) {
  if (months.length === 0) return <p className="tp-empty">No month with income or spending yet.</p>;
  const hi = Math.max(1, ...months.map((m) => Math.max(m.income, m.expenses))) * 1.08;
  return (
    <div className="bg-year" data-testid="year-bars">
      {months.map((m) => {
        const low = Math.min(m.income, m.expenses);
        const kept = m.income - m.expenses;
        return (
          <div className={m.complete ? "bg-ym" : "bg-ym part"} key={m.month}
            title={`${monthName(m.month)} ${m.month.slice(0, 4)}${m.complete ? "" : " so far"}: came in ${eur(m.income)}, went out ${eur(m.expenses)}, ${kept >= 0 ? `kept ${eur(kept)}` : `spent ${eur(-kept)} beyond it`}`}>
            <span className="bg-ycol">
              {kept !== 0 && <i className={kept > 0 ? "good" : "bad"} style={{ height: `${(Math.abs(kept) / hi) * 100}%` }} />}
              <i className="grey" style={{ height: `${(low / hi) * 100}%` }} />
            </span>
            <small>{monthName(m.month)}</small>
          </div>
        );
      })}
    </div>
  );
}

function SplitBar({ parts, eur }: { parts: Array<[string, number, string]>; eur: (n: number) => string }) {
  const live = parts.filter((p) => p[1] > 0);
  return (
    <>
      <div className="tp-kbar">{live.map(([n, v, rgb]) => <i key={n} style={{ ...dotStyle(rgb), flex: `${v} 1 0` }} title={`${n}: ${eur(v)}`} />)}</div>
      <div className="tp-kinds">{live.map(([n, v, rgb]) => <span key={n} className="tp-kind"><i className="tp-dot" style={dotStyle(rgb)} /><span>{n}</span><b>{eur(v)}</b></span>)}</div>
    </>
  );
}

export default function Budget({ app, section }: PageProps) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["money", "budget"], queryFn: getBudget });
  const [editing, setEditing] = useState(false);
  const [hide, toggleHide] = useHide();

  const eur = (n: number | null | undefined, opts: { sign?: boolean } = {}) => (hide ? "€•••" : eurPlain(n, opts));
  const hideBtn = <button type="button" className="btn" aria-pressed={hide} onClick={toggleHide}>{hide ? "Show amounts" : "Hide amounts"}</button>;
  const header = <PageHeader crumb={app.name} title={section.label} right={hideBtn} />;
  if (q.isError) return <div className="v2-page">{header}<Block><p className="empty">Could not load the budget.</p></Block></div>;
  if (!q.data) return <div className="v2-page">{header}<Block><p className="empty">Loading the budget.</p></Block></div>;
  const b: MoneyBudget = q.data;
  if (!b.synced) {
    return (
      <div className="v2-page">
        {header}
        <Say lead="Your spending was not read." more={NO_SYNC} />
        <div className="tp-stack">
          <Block title="Summary">
            <Wall>
              {["Spent this month", "Recurring charges", "Kept", "Safety cushion"].map((n) => (
                <Card key={n} name={n} chip={["dim", "Not synced yet"]} fig={<span title={NO_SYNC}>-</span>} pic={<Share parts={[["soft", 1, 0.15]]} />} />
              ))}
            </Wall>
          </Block>
        </div>
      </div>
    );
  }

  const view = b.budget;
  const stale = b.stale === true;
  const readAt = b.read_at;
  const cats = view ? view.categories : [];
  const statuses = cats.map(statusOf);
  const hot = cats.filter((_, i) => statuses[i] === "ahead" || statuses[i] === "over");
  const noPlan = cats.filter((c, i) => statuses[i] === "none" && c.spent > 0);
  const over = cats.filter((_, i) => statuses[i] === "over").length;
  const hasPlan = cats.some((c) => c.target !== null);
  const planTotal = cats.reduce((a, c) => a + (c.target ?? 0), 0);
  const spent = view ? view.expenses : 0;
  const day = view ? view.elapsed_days : 0;
  const dim = view ? view.total_days : 0;
  const left = Math.max(0, dim - day);
  const pace = hasPlan && dim > 0 ? (planTotal * day) / dim : null;
  const safe = hasPlan ? Math.max(0, planTotal - spent) / Math.max(1, left) : null;
  const done = b.months.filter((m) => m.complete).slice(-11);
  const income = done.reduce((a, m) => a + m.income, 0);
  const kept = done.reduce((a, m) => a + m.income - m.expenses, 0);
  const keptWith = done.length > 0 && income > 0;
  const rgbOf = (i: number) => RGB[i % RGB.length];
  const last = b.last_month;

  const open = () => setEditing(true);
  const stops: Stop[] = [
    ...hot.map((c): Stop => {
      const i = cats.indexOf(c);
      const st = statuses[i];
      return { key: c.slug, tone: st === "over" ? "bad" : "warn", word: STATUS[st][1], title: `${categoryLabel(c.label)}: ${eur(c.spent)} spent of ${eur(c.target)}`,
        detail: c.pace !== null ? `By day ${day} the plan allows ${eur(c.pace)}${c.ahead !== null ? `, so this is ${Math.round(c.ahead * 100)}% ahead` : ""}${c.target !== null ? `. ${eur(Math.max(0, c.target - c.spent))} is left for ${left} ${left === 1 ? "day" : "days"}` : ""}` : undefined,
        bar: c.target ? <Share parts={[[st === "over" ? "bad" : "warn", Math.min(c.spent, c.target)], ["soft", Math.max(0, c.target - c.spent), 0.15]]} /> : undefined, cap: c.target ? `${Math.round((c.spent / c.target) * 100)}% of the plan used` : undefined };
    }),
    ...noPlan.map((c): Stop => ({ key: `np${c.slug}`, tone: "warn", word: "Waits for you", title: `${categoryLabel(c.label)} has no plan`,
      detail: `${eur(c.spent)} spent so far${c.suggestion !== null ? `. Its average over 12 months is ${eur(c.suggestion)}` : ""}`, btn: "Set a plan", act: open })),
  ];
  if (stale) stops.unshift({ key: "stale", tone: "warn", word: "Waits for you", title: "Finary's last read is old", detail: "Spending shown is as of the last read, not today" });

  let lead: string;
  let more: ReactNode;
  if (view === null) {
    lead = "Your spending could not be read.";
    more = b.reason ?? "There is no budget for this month.";
  } else if (!hasPlan) {
    lead = `You have spent ${eur(spent)} this month.`;
    more = <>It is day {day} of {dim}. <em className="warn">No plan is set yet</em>, so there is nothing to measure it against. Edit the plan to set one.</>;
  } else {
    lead = `You have spent ${eur(spent)} of the ${eur(planTotal)} planned this month.`;
    more = (
      <>
        It is day {day} of {dim}: <em className="run">{eur(pace)} would be on pace</em>, so you are <em className={spent > (pace ?? 0) * 1.1 ? "warn" : "good"}>{eur(Math.abs(spent - (pace ?? 0)))} {spent > (pace ?? 0) ? "ahead" : "under"}</em>.
        {hot.length > 0 ? <> <em className="warn">{hot.length} {hot.length === 1 ? "category is" : "categories are"} ahead of pace.</em></> : null}
        {last ? <> Last month you {last.net_cashflow >= 0 ? "kept" : "went over by"} <em className={last.net_cashflow >= 0 ? "good" : "bad"}>{eur(Math.abs(last.net_cashflow))}</em>.</> : null}
      </>
    );
  }

  return (
    <div className="v2-page">
      {header}
      <Say lead={lead} more={more} />
      <div className="tp-stack">
        <NeedsBlock stops={stops} empty="Nothing waits for you: every category is on pace." />
        <Block title="Summary">
          <Wall>
            {view === null ? (
              <Card name="Spent this month" chip={["dim", "Not read"]} fig={<span title={b.reason ?? "No budget for this month"}>-</span>} pic={<Share parts={[["soft", 1, 0.15]]} />} />
            ) : (
              <Card name="Spent this month" hue={spent > planTotal && hasPlan ? "bad" : pace !== null && spent > pace * 1.1 ? "warn" : hasPlan ? "good" : "run"}
                hover={readTitle("Finary's spending this month", readAt)}
                chip={stale ? ["warn", "Stale"] : !hasPlan ? ["dim", "No plan set yet"] : spent > planTotal ? ["bad", "Over"] : pace !== null && spent > pace * 1.1 ? ["warn", "Ahead of pace"] : ["good", "On pace"]}
                fig={eur(spent)} sub={hasPlan ? `of ${eur(planTotal)} planned, on day ${day} of ${dim}` : `on day ${day} of ${dim}`}
                pic={<Share parts={hasPlan ? [[spent > planTotal ? "bad" : "data", Math.min(spent, planTotal)], ["soft", Math.max(0, planTotal - spent), 0.15]] : [["data", 1, 0.5]]} />}
                facts={hasPlan && pace !== null && day > 0 ? [["On pace by today", eur(pace)], ["Month end at this pace", eur((spent / day) * dim), (spent / day) * dim > planTotal ? "warn" : ""]] : [["Recurring", b.recurring ? eur(b.recurring.monthly_equivalent) : "-"], ["Income so far", eur(view.income)]]} />
            )}
            <Card name="Safe to spend" hue="run" fig={safe === null ? <span title="Safe to spend needs a plan">-</span> : eur(safe)}
              sub={safe === null ? "a day, once a plan is set" : `a day for the ${left} ${left === 1 ? "day" : "days"} left, to end on plan`}
              pic={<Share parts={[["data", Math.max(0, day), 0.45], ["data", Math.max(0, left)]]} />}
              facts={[["Left in the plan", hasPlan ? eur(Math.max(0, planTotal - spent)) : "-"], ...(hasPlan ? [["Weekly goal", eur((planTotal * 12) / 52)] as [string, string]] : [])]} />
            <Card name="Kept" hue={keptWith ? (kept >= 0 ? "good" : "bad") : ""}
              hover={readTitle(`Income minus spending over the last ${done.length} complete months, as a share of income`, readAt)}
              fig={keptWith ? pct0(kept / income) : <span title="No complete month with income yet">-</span>}
              sub={keptWith ? `of what came in over ${done.length} full months: ${eur(kept)}` : "needs a complete month with income"}
              pic={<ZeroBars values={done.map((m) => m.income - m.expenses)} none="No full month yet" title={(i) => `${monthName(done[i].month)}: ${eurPlain(done[i].income - done[i].expenses, { sign: true })}`} />}
              facts={[["Months kept", String(done.filter((m) => m.income >= m.expenses).length), "good"], ["Months over", String(done.filter((m) => m.income < m.expenses).length), done.some((m) => m.income < m.expenses) ? "bad" : ""]]} />
            <Card name="Safety cushion" hue={b.cushion_months === null ? "" : "good"}
              fig={b.cushion_months === null ? <span title={b.savings_eur === null ? "No savings account in the snapshot" : "No complete month of spending yet"}>-</span> : b.cushion_months.toFixed(1)}
              sub="months of spending your savings cover"
              pic={<Share parts={b.cushion_months === null ? [["soft", 1, 0.15]] : [["good", Math.min(12, b.cushion_months)], ["soft", Math.max(0, 12 - b.cushion_months), 0.15]]} />}
              facts={[["Savings", b.savings_eur === null ? "-" : eur(b.savings_eur)], ...(b.savings_eur !== null && b.cushion_months ? [["Spending a month", eur(b.savings_eur / b.cushion_months)] as [string, string]] : [])]} />
          </Wall>
        </Block>

        <Block title="This month against the plan"
          state={view ? (hasPlan ? <State tone={hot.length ? "warn" : "good"}>{hot.length ? `${hot.length} ahead of pace` : "All on pace"}</State> : <State tone="dim">No plan set</State>) : undefined}
          right={view ? <PhoneOff><button type="button" className="btn" onClick={open}>Edit plan</button></PhoneOff> : undefined}>
          {view === null ? (
            <p className="empty">{b.reason ?? "There is no budget for this month."}</p>
          ) : cats.length === 0 ? (
            <p className="empty">No spending categories this month.</p>
          ) : (
            <>
            <div className="bg-key"><span><i className="spent" />Spent</span><span><i className="pace" />Pace by today</span></div>
            <div title={readTitle(`${SRC_BUDGET}. The thick bar is what you have spent, the thin one is where spending would be on day ${day} of ${dim} if it ran evenly`, readAt)} data-testid="plan-rows">
              {cats.map((c, i) => {
                const st = statuses[i];
                const whole = c.target ?? Math.max(c.suggestion ?? 0, c.spent, 1);
                const f = whole > 0 ? Math.min(1, c.spent / whole) : 0;
                const rgb = st === "over" ? "255 90 99" : st === "ahead" ? "255 176 32" : st === "none" ? "141 152 168" : "47 224 138";
                return (
                  <div className="tp-barrow" key={c.slug} title={`${categoryLabel(c.label)}: ${eurPlain(c.spent)} spent${c.target !== null ? `, plan ${eurPlain(c.target)}` : ", no plan"}${c.pace !== null ? `, on pace by day ${day}: ${eurPlain(c.pace)}` : ""}`}>
                    <span className="tp-n"><i className="tp-dot" style={dotStyle(rgbOf(i))} />{categoryLabel(c.label)}</span>
                    <span className="tp-track" style={dotStyle(rgb)}>
                      <i style={{ width: `${f * 100}%` }} />
                      {c.pace !== null && <u style={{ width: `${Math.min(1, c.pace / whole) * 100}%` }} />}
                    </span>
                    <span className="tp-amt"><b>{eur(c.spent)}</b> <small>{c.target !== null ? `of ${eur(c.target)}` : c.suggestion !== null ? `average ${eur(c.suggestion)}` : "no plan"}</small></span>
                    <span className="tp-side"><State tone={STATUS[st][0]}>{st === "ahead" && c.ahead !== null ? `Ahead ${Math.round(c.ahead * 100)}%` : STATUS[st][1]}</State></span>
                  </div>
                );
              })}
              {over > 0 && <p className="tp-cap">{over} over the plan</p>}
            </div>
            </>
          )}
        </Block>

        <div className="tp-two even">
          <Block title="Where last month's money went">
            {last ? (
              <>
                <p className="tp-cap">Came in: {eur(last.income)}</p>
                {!last.categories.some((c) => c.income > 0) && <p className="tp-empty">No income categories last month.</p>}
                <SplitBar eur={eur} parts={last.categories.filter((c) => c.income > 0).map((c, i): [string, number, string] => [categoryLabel(c.label), c.income, i === 0 ? "210 215 225" : "150 160 180"])} />
                <p className="tp-cap">Went out: {eur(last.expenses)}{last.net_cashflow >= 0 ? `, kept ${eur(last.net_cashflow)}` : `, ${eur(-last.net_cashflow)} more than came in`}</p>
                <SplitBar eur={eur} parts={[...last.categories.filter((c) => c.spent > 0).map((c, i): [string, number, string] => [categoryLabel(c.label), c.spent, rgbOf(i)]),
                  ...(last.net_cashflow > 0 ? [["Kept", last.net_cashflow, "47 224 138"] as [string, number, string]] : [])]} />
              </>
            ) : (
              <p className="empty">The snapshot has no figures for last month.</p>
            )}
          </Block>
          <Block title="Money in and out, 12 months">
            <YearBars months={b.months.slice(-12)} eur={eur} />
            <div className="tp-kinds">
              <span className="tp-kind"><i className="tp-dot" style={dotStyle("141 152 168")} />Spent</span>
              <span className="tp-kind"><i className="tp-dot" style={dotStyle("47 224 138")} />Kept</span>
              <span className="tp-kind"><i className="tp-dot" style={dotStyle("255 90 99")} />Beyond what came in</span>
            </div>
          </Block>
        </div>
      </div>
      {editing && view && (
        <PlanDialog
          categories={view.categories}
          onClose={() => setEditing(false)}
          onChanged={() => qc.invalidateQueries({ queryKey: ["money", "budget"] })}
        />
      )}
    </div>
  );
}
