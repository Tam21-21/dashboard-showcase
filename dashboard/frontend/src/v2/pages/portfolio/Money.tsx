import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Block, LineChart, PageHeader, Say, Segmented, State, usePageMemory } from "../../kit";
import type { NlTone } from "../../kit";
import { useLayout } from "../../kit/phone";
import type { PageProps } from "../../shell/sections";
import { getOverview } from "../../data/moneyApi";
import type { MoneyAccount, MoneyGoal, MoneyOverview } from "../../data/moneyApi";
import { fmtDate } from "../../../lib/format";
import { Card, GainBars, NeedsBlock, PlainFacts, Pic, Share, Wall, ZeroBars, dotStyle, useHide } from "../trading/parts";
import type { GainRow, Stop } from "../trading/parts";
import { eur as eurPlain, monthName, netAxisFormat, parisMonth, parisStamp, pct0, readTitle } from "./moneyCharts";
import RefreshFinary from "./RefreshFinary";
import "./portfolio.css";

// Money: what you have, whether it is going up, what each month added, where it sits, whether every account is
// in sync, and how far along each goal is. Every amount is Finary's own, read by the claude.ai task twice a day and
// on a press of Refresh, and stored by lane MY1. The mockup also draws a net worth line over years and gains by year; the store keeps one value
// per sync, so those parts say what they need until the history is long enough.

const NO_SYNC = "Finary has not synced yet";
const SOURCE_NET = "Finary's household net worth, one value per sync";
const SOURCE_ACCOUNTS = "Finary's accounts, with each account's connection status";

const KIND_NAME: Record<string, string> = {
  checking: "Cash", saving: "Savings", savings: "Savings", investment: "Investments", crypto: "Crypto", credit: "Credit",
};
const KIND_RGB = ["77 155 255", "47 224 138", "150 128 255", "255 146 110", "236 110 205", "40 200 235"];
const kindName = (t: string): string => KIND_NAME[t] ?? (t.charAt(0).toUpperCase() + t.slice(1).replace(/_/g, " "));

type Word = "In sync" | "Needs reconnect" | "By hand" | "Stale";
const accountWord = (a: MoneyAccount, stale: boolean): Word => (stale ? "Stale" : a.sync_status === null ? "By hand" : a.sync_status === "success" ? "In sync" : "Needs reconnect");
const wordTone = (w: Word): NlTone => (w === "In sync" ? "good" : w === "By hand" ? "run" : "warn");

const goalTone = (s: string | null): NlTone =>
  s === "completed" || s === "on_track" ? "good" : s === "overdue" ? "bad" : s === "challenging" || s === "underfunded" ? "warn" : "dim";
const goalWord = (s: string | null): string | null => (s ? (s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ")) : null);

function goalAmount(n: number | null, currency: string | null): string {
  if (n === null) return "-";
  return currency === null || currency === "EUR" ? eurPlain(n) : `${Math.round(n)} ${currency}`;
}

function goalProgress(g: MoneyGoal): number | null {
  if (g.current_amount === null || g.target_amount === null || g.target_amount <= 0) return null;
  return g.current_amount / g.target_amount;
}

/** The last net worth of each Paris month, oldest first. */
function monthEnds(o: MoneyOverview): Array<{ month: string; net: number }> {
  const ends: Array<{ month: string; net: number }> = [];
  for (const p of o.series) {
    const m = parisMonth(p.read_at);
    if (ends.length && ends[ends.length - 1].month === m) ends[ends.length - 1].net = p.net_worth;
    else ends.push({ month: m, net: p.net_worth });
  }
  return ends;
}

export default function Money({ app, section }: PageProps) {
  const q = useQuery({ queryKey: ["money", "overview"], queryFn: getOverview });
  const phone = useLayout() === "phone";
  const [hide, toggleHide] = useHide();
  const [range, setRange] = usePageMemory<"1y" | "3y" | "all">("portfolio/money-range", "all");
  const [gainBy, setGainBy] = usePageMemory<"year" | "month">("portfolio/money-gain", "year");

  const hideBtn = <button type="button" className="btn" aria-pressed={hide} onClick={toggleHide}>{hide ? "Show amounts" : "Hide amounts"}</button>;
  const header = <PageHeader crumb={app.name} title={section.label} right={<div className="nl-btns"><RefreshFinary />{hideBtn}</div>} />;
  const eur = (n: number | null | undefined, opts: { sign?: boolean } = {}) => (hide ? "€•••" : eurPlain(n, opts));

  if (q.isError) return <div className="v2-page">{header}<Block><p className="empty">Could not load the money figures.</p></Block></div>;
  if (!q.data) return <div className="v2-page">{header}<Block><p className="empty">Loading the money figures.</p></Block></div>;
  const o = q.data;
  if (!o.synced || o.net_worth === null || o.read_at === null) {
    return (
      <div className="v2-page">
        {header}
        <Say lead="Your money was not read." more={NO_SYNC} />
        <div className="tp-stack">
          <Block title="Summary">
            <Wall>
              {["Net worth", "This month", "Savings", "Accounts"].map((n) => (
                <Card key={n} name={n} chip={["dim", "Not synced yet"]} fig={<span title={NO_SYNC}>-</span>} pic={<Share parts={[["soft", 1, 0.15]]} />} />
              ))}
            </Wall>
          </Block>
        </div>
      </div>
    );
  }

  const stale = o.stale === true;
  const readAt = o.read_at;
  const net = o.net_worth;
  const gross = o.gross_assets;
  const share = (v: number): number | null => (gross !== null && gross > 0 ? v / gross : null);
  const words = o.accounts.map((a) => accountWord(a, stale));
  const broken = o.accounts.filter((a) => a.sync_status !== null && a.sync_status !== "success");
  const banks = new Set(o.accounts.map((a) => a.institution ?? a.name)).size;
  const series = o.series;
  const nets = series.map((p) => p.net_worth);
  const ends = monthEnds(o);
  const monthPts = series.filter((p) => parisMonth(p.read_at) === parisMonth(series[series.length - 1]?.read_at ?? readAt));
  const thisMonth = monthPts.length >= 2 ? net - monthPts[0].net_worth : null;
  const thisYear = (() => {
    const y = parisMonth(readAt).slice(0, 4);
    const before = [...ends].reverse().find((e) => e.month.slice(0, 4) < y);
    return before ? net - before.net : null;
  })();
  const gains: GainRow[] = ends.slice(1).map((e, i) => {
    const gain = e.net - ends[i].net;
    return { label: monthName(e.month), value: gain, text: eur(gain, { sign: true }), hover: `${monthName(e.month)} ${e.month.slice(0, 4)}: ${eurPlain(gain, { sign: true })}` };
  });
  const yearOf = (m: string) => m.slice(0, 4);
  const thisY = yearOf(parisMonth(readAt));
  const yearRows: GainRow[] = [...new Set(ends.map((e) => yearOf(e.month)))].map((y) => {
    const first = ends.findIndex((e) => yearOf(e.month) === y);
    const lastI = ends.map((e) => yearOf(e.month)).lastIndexOf(y);
    const from = first > 0 ? ends[first - 1].net : ends[0].net;
    const gain = ends[lastI].net - from;
    return { label: y === thisY ? `${y} so far` : y, value: gain, text: eur(gain, { sign: true }), hover: `${y}: ${eurPlain(gain, { sign: true })}` };
  });
  const yearGains = gains.filter((_g, i) => ends[i + 1].month.slice(0, 4) === thisY);
  const yearBase = thisYear === null ? null : net - thisYear;
  const yearPct = thisYear === null || !yearBase ? null : (thisYear / yearBase) * 100;
  const bestOf = (rows: GainRow[], dir: 1 | -1): string => {
    const g = rows.reduce((b, r) => (r.value * dir > b.value * dir ? r : b));
    return `${g.text}, ${g.label}`;
  };
  const kinds = [...new Set(o.accounts.map((a) => a.account_type))]
    .map((t, i) => ({ t, name: kindName(t), rgb: KIND_RGB[i % KIND_RGB.length], v: o.accounts.filter((a) => a.account_type === t).reduce((s, a) => s + Math.max(0, a.value_eur), 0) }))
    .filter((k) => k.v > 0).sort((a, b) => b.v - a.v);
  const rgbOf = (t: string) => kinds.find((k) => k.t === t)?.rgb ?? "141 152 168";

  const rangeFrom = range === "all" || series.length === 0 ? 0 : Date.parse(series[series.length - 1].read_at) - (range === "1y" ? 1 : 3) * 365 * 864e5;
  const shown = series.filter((p) => Date.parse(p.read_at) >= rangeFrom);
  const shownNets = shown.map((p) => p.net_worth);
  const lastIdx = shown.length - 1;
  const xs = Array.from(new Set([0, 1, 2, 3, 4].map((k) => Math.round((k * lastIdx) / 4))));

  const stops: Stop[] = [];
  if (stale) {
    stops.push({ key: "stale", tone: "warn", word: "Waits for you", title: "Finary's last read is old",
      detail: `The figures are from ${parisStamp(readAt)}. Press Refresh to read Finary again`,
      bar: <Share parts={[["warn", 1]]} /> });
  }
  broken.forEach((a) => stops.push({ key: a.account_id, tone: "warn", word: "Waits for you", title: `${a.institution ?? a.name} reported an error at its last sync`,
    detail: a.last_successful_sync_at ? `The last good read of ${a.name} was ${parisStamp(a.last_successful_sync_at)}. Its balance may be out of date. Reconnect it in Finary if the error stays` : `${a.name} has never synced. Reconnect it in Finary`,
    bar: <Share parts={[["good", o.accounts.length - broken.length], ["warn", broken.length]]} />, cap: `${o.accounts.length - broken.length} of ${o.accounts.length} accounts in sync` }));

  const below = o.accounts.filter((a) => a.value_eur < 0).length;
  const sav = o.savings_eur;
  const lead = `Your net worth is ${eur(net)}.`;
  const more = (
    <>
      Finary read it on {fmtDate(readAt)}{o.liabilities ? `, and ${eur(o.liabilities)} is owed` : ", and nothing is owed"}. It sits in <em className="run">{o.accounts.length} {o.accounts.length === 1 ? "account" : "accounts"} at {banks} {banks === 1 ? "bank" : "banks"}</em>
      {below ? <>, and <em className="bad">{below} {below === 1 ? "is" : "are"} below zero</em></> : null}.
      {stale ? <> <em className="warn">The read is old.</em></> : null}
    </>
  );

  const catFacts = o.allocation.slice(0, 3).map((a): [string, ReactNode] => [kindName(a.category.replace(/_accounts$/, "").replace(/_/g, " ")), eur(a.amount)]);

  return (
    <div className="v2-page">
      {header}
      <Say lead={lead} more={more} />
      <div className="tp-stack">
        <NeedsBlock stops={stops} empty="Nothing waits for you: every account is in sync." />
        <Block title="Summary">
          <Wall>
            <Card name="Net worth" hue={thisMonth === null ? "run" : thisMonth >= 0 ? "good" : "bad"} hover={readTitle(SOURCE_NET, readAt)}
              chip={stale ? ["warn", "Stale"] : thisMonth === null ? ["dim", "One month so far"] : [thisMonth >= 0 ? "good" : "bad", thisMonth >= 0 ? "Up this month" : "Down this month"]}
              fig={eur(net)} sub="everything you have, less what you owe"
              pic={<Pic values={nets} tone={nets.length > 1 && nets[nets.length - 1] < nets[0] ? "bad" : "data"} none="One reading so far" />}
              facts={[["This month", thisMonth === null ? "-" : eur(thisMonth, { sign: true }), thisMonth === null ? "" : thisMonth >= 0 ? "good" : "bad"], ["This year", thisYear === null ? "-" : eur(thisYear, { sign: true }), thisYear === null ? "" : thisYear >= 0 ? "good" : "bad"]]} />
            <Card name="This year" hue={thisYear === null ? "" : thisYear >= 0 ? "good" : "bad"} chip={thisYear === null ? ["dim", "No history"] : undefined}
              hover={readTitle("Net worth now minus the last reading of last year, Paris time", readAt)}
              fig={thisYear === null ? <span title="No reading from before this year, so there is no change to show yet">-</span> : eur(thisYear, { sign: true })}
              sub={thisYear === null ? "needs a reading from before this year" : `since 1 January, ${yearPct === null ? "-" : `${yearPct >= 0 ? "+" : "−"}${Math.abs(yearPct).toFixed(1)}%`} on ${eur(yearBase)}`}
              pic={<ZeroBars values={yearGains.map((g) => g.value)} none="No month to compare yet" title={(i) => yearGains[i].hover ?? ""} />}
              facts={[["Best month", yearGains.length ? bestOf(yearGains, 1) : "-"], ["Worst month", yearGains.length ? bestOf(yearGains, -1) : "-"]]} />
            {sav !== null ? (
              <Card name="Savings" hue="good" hover={readTitle("The accounts Finary types as saving, summed", readAt)} fig={eur(sav)}
                sub={`${share(sav) !== null ? pct0(share(sav) as number) : "-"} of what you have sits in savings`}
                pic={<Share parts={o.accounts.map((a): ["good" | "soft", number, number] => [a.account_type === "saving" || a.account_type === "savings" ? "good" : "soft", Math.max(0, a.value_eur), a.account_type === "saving" || a.account_type === "savings" ? 0.9 : 0.25])} />}
                facts={kinds.filter((k) => k.t !== "saving" && k.t !== "savings").slice(0, 3).map((k): [string, ReactNode] => [k.name, eur(k.v)])} />
            ) : (
              <Card name="Financial assets" hue="run" fig={o.financial_assets === null ? <span title="Finary gave no total">-</span> : eur(o.financial_assets)}
                sub="savings, shares, funds and crypto, as Finary counts them" hover="No account is typed as saving in the snapshot"
                pic={<Share parts={[["data", 1, 0.5]]} />} facts={catFacts.length ? catFacts.map(([k, v]) => [k, v] as [string, ReactNode]) : undefined} />
            )}
            <Card name="Accounts" hue={broken.length ? "warn" : "good"} hover={readTitle(SOURCE_ACCOUNTS, readAt)}
              chip={stale ? ["warn", "Stale"] : broken.length ? ["warn", `${broken.length} needs you`] : ["good", "All in sync"]} fig={String(o.accounts.length)}
              sub={`${o.accounts.length - broken.length - o.accounts.filter((a) => a.sync_status === null).length} in sync, ${o.accounts.filter((a) => a.sync_status === null).length} entered by hand`}
              pic={<Share parts={[["good", o.accounts.length - broken.length], ["warn", broken.length]]} />}
              facts={[["In sync", String(o.accounts.length - broken.length - o.accounts.filter((a) => a.sync_status === null).length), "good"], ["Reconnect", String(broken.length), broken.length ? "warn" : ""]]} />
          </Wall>
        </Block>

        <Block title="Net worth over time" state={<State tone="dim">{series.length === 1 ? "One reading" : `${series.length} readings`}</State>}
          right={series.length > 7 ? (
            <Segmented variant="seg" value={range} onChange={(v) => setRange(v === "1y" ? "1y" : v === "3y" ? "3y" : "all")}
              options={[{ label: "1 year", value: "1y" }, { label: "3 years", value: "3y" }, { label: "All", value: "all" }]} />
          ) : undefined}>
          {series.length < 2 ? (
            <p className="empty" title="The line needs at least two syncs">Too few points to draw yet.</p>
          ) : (
            <>
              <div className="mn-line" title={readTitle(SOURCE_NET, readAt)}>
                <LineChart
                  series={[{ v: shownNets, c: "var(--b)", sw: 2.8, fill: 0.24 }]}
                  yfmt={hide ? () => "•••" : netAxisFormat(shownNets)}
                  {...(phone ? { minHeight: 230 } : {})}
                  xlabels={xs.map((i) => [i, `${monthName(parisMonth(shown[i].read_at))} ${parisMonth(shown[i].read_at).slice(0, 4)}`] as [number, string])}
                  rows={(i) => [parisStamp(shown[i].read_at), ["Net worth", eur(shown[i].net_worth)]]}
                />
              </div>
              <PlainFacts sans facts={(() => {
                const ch = net - shownNets[0];
                const months = Math.max(1, (Date.parse(shown[lastIdx].read_at) - Date.parse(shown[0].read_at)) / (30.4375 * 864e5));
                const pc = shownNets[0] ? (ch / shownNets[0]) * 100 : null;
                const t = (n: number): "good" | "bad" => (n >= 0 ? "good" : "bad");
                return [
                  [`In ${monthName(parisMonth(shown[0].read_at))} ${parisMonth(shown[0].read_at).slice(0, 4)}`, eur(shownNets[0])],
                  ["Now", eur(net)],
                  ["Change", eur(ch, { sign: true }), t(ch)],
                  ["In percent", pc === null ? "-" : `${pc >= 0 ? "+" : "−"}${Math.abs(pc).toFixed(1)}%`, pc === null ? "" : t(pc)],
                  ["A month, on average", eur(ch / months, { sign: true }), t(ch)],
                ] as Array<[string, ReactNode, ("good" | "bad" | "")?]>;
              })()} />
            </>
          )}
        </Block>

        <Block title={gainBy === "year" ? "Gained by year" : "Gained by month"} state={<State tone="dim">{String(gainBy === "year" ? yearRows.length : gains.length)}</State>}
          right={<Segmented variant="seg" value={gainBy} onChange={(v) => setGainBy(v === "month" ? "month" : "year")}
            options={[{ label: "By year", value: "year" }, { label: "By month", value: "month" }]} />}>
          <GainBars rows={gainBy === "year" ? yearRows : gains} none="The bars need readings from more than one month. Finary gives today's figures only, so the history grows from the readings the dashboard saves itself." />
        </Block>

        <div className="tp-two mn-two">
          <Block title="Where it sits" state={<State tone="run">{`${o.accounts.length} ${o.accounts.length === 1 ? "account" : "accounts"}`}</State>}>
            <div className="tp-kbar" data-testid="kinds">
              {kinds.map((k) => <i key={k.t} style={{ ...dotStyle(k.rgb), flex: `${k.v} 1 0` }} title={`${k.name}: ${eur(k.v)}`} />)}
            </div>
            <div className="tp-kinds">
              {kinds.map((k) => <span key={k.t} className="tp-kind"><i className="tp-dot" style={dotStyle(k.rgb)} /><span>{k.name}</span><b>{eur(k.v)}</b></span>)}
            </div>
            <div className="tp-scroll">
              <table className="tp-tbl" data-testid="accounts">
                <thead><tr><th>Account</th><th>Kind</th><th>Share</th><th className="r">Balance</th><th /></tr></thead>
                <tbody>
                  {[...o.accounts.keys()].sort((x, y) => o.accounts[y].value_eur - o.accounts[x].value_eur).map((i) => {
                    const a = o.accounts[i];
                    const s = share(a.value_eur);
                    return (
                      <tr key={a.account_id}>
                        <td title={`${kindName(a.account_type)} account${a.institution ? `, ${a.institution}` : ""}`}><i className="tp-dot" style={dotStyle(rgbOf(a.account_type))} /> {a.name}</td>
                        <td>{kindName(a.account_type)}</td>
                        <td className="num">{s === null ? <span title="Finary gave no total for assets">-</span> : <span title={`${a.name} is ${pct0(s)} of gross assets`}><Share parts={[["data", s], ["soft", 1 - s, 0.12]]} />{pct0(s)}</span>}</td>
                        <td className="r num">{eur(a.value_eur)}</td>
                        <td className="r"><span title={a.last_successful_sync_at ? `Last successful sync ${parisStamp(a.last_successful_sync_at)}${a.institution ? `, ${a.institution}` : ""}` : "No connection: this account is entered by hand"}><State tone={wordTone(words[i])}>{words[i]}</State></span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {o.debts.length > 0 && (
              <div data-testid="owed" className="mn-owed">
                <p className="tp-cap" title="Debts are listed apart and get no share">Owed</p>
                <table className="tp-tbl"><tbody>
                  {o.debts.map((d) => <tr key={d.account_id}><td>{d.name}</td><td className="r num">{eur(Math.abs(d.value_eur))}</td></tr>)}
                </tbody></table>
              </div>
            )}
          </Block>
          <div data-testid="goals">
            <Block title="Goals" state={<State tone={o.goals.length ? "run" : "dim"}>{o.goals.length ? String(o.goals.length) : "None"}</State>}>
              {o.goals.length === 0 ? (
                <p className="tp-empty">No goal is set in Finary. One you add there shows here with its progress and Finary&apos;s own word for how it stands.</p>
              ) : (
                <div className="mn-goals" title={readTitle("Finary's goals, with Finary's own status", readAt)}>
                  {o.goals.map((g) => {
                    const p = goalProgress(g);
                    const w = goalWord(g.status);
                    return (
                      <div className="mn-goal" key={g.goal_id}>
                        <div className="mn-goal-h">
                          <b>{g.name}</b>
                          {w ? <State tone={goalTone(g.status)}>{w}</State> : <span title="Finary gave no status for this goal">-</span>}
                        </div>
                        {p === null ? (
                          <div className="mn-goal-n" title="Finary gave no progress for this goal">-</div>
                        ) : (
                          <>
                            <Share parts={[[p >= 1 ? "good" : "data", Math.min(1, p)], ["soft", Math.max(0, 1 - p), 0.15]]} />
                            <div className="mn-goal-n"><span>{hide ? "€•••" : goalAmount(g.current_amount, g.currency)} of {hide ? "€•••" : goalAmount(g.target_amount, g.currency)}</span><b className="num">{pct0(p)}</b></div>
                          </>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Block>
          </div>
        </div>
      </div>
    </div>
  );
}
