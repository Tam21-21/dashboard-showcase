import { useQuery } from "@tanstack/react-query";
import { Block, PageHeader, Say, State } from "../../kit";
import type { NlTone } from "../../kit";
import type { PageProps } from "../../shell/sections";
import { getOverview } from "../../data/moneyApi";
import type { MoneyAccount } from "../../data/moneyApi";
import { fmtDate } from "../../../lib/format";
import { Card, Share, Wall, dotStyle, useHide } from "../trading/parts";
import { eur as eurPlain, parisStamp, pct0, readTitle } from "./moneyCharts";
import RefreshFinary from "./RefreshFinary";
import "./portfolio.css";

// Positions: what sits in the investment accounts, how big each is, and how the money is split. It reads the money
// overview the Money page reads. Finary's overview gives each account's balance, not what is inside it and not what
// was put in, so the rows are accounts, and "Put in" and "Earned" say so instead of showing a figure.

const NOT_INVESTED = new Set(["checking", "saving", "savings", "credit"]);
const KIND: Record<string, string> = { investment: "Investments", crypto: "Crypto" };
const RGB = ["150 128 255", "77 155 255", "255 146 110", "47 224 138", "236 110 205"];
const kindName = (t: string): string => KIND[t] ?? (t.charAt(0).toUpperCase() + t.slice(1).replace(/_/g, " "));
const NO_COST = "Finary's overview does not give the money put in, so what was earned cannot be worked out";

export default function Positions({ app, section }: PageProps) {
  const q = useQuery({ queryKey: ["money", "overview"], queryFn: getOverview });
  const [hide, toggleHide] = useHide();
  const eur = (n: number | null | undefined) => (hide ? "€•••" : eurPlain(n));
  const hideBtn = <button type="button" className="btn" aria-pressed={hide} onClick={toggleHide}>{hide ? "Show amounts" : "Hide amounts"}</button>;
  const header = <PageHeader crumb={app.name} title={section.label} right={<div className="nl-btns"><RefreshFinary />{hideBtn}</div>} />;

  if (q.isError) return <div className="v2-page">{header}<Block><p className="empty">Could not load the investment accounts.</p></Block></div>;
  if (!q.data) return <div className="v2-page">{header}<Block><p className="empty">Loading the investment accounts.</p></Block></div>;
  const o = q.data;
  if (!o.synced || o.read_at === null) {
    return (
      <div className="v2-page">
        {header}
        <Say lead="Your holdings were not read." more="Finary has not synced yet, so there is nothing to list." />
        <div className="tp-stack">
          <Block title="Summary">
            <Wall>
              {["Worth now", "Earned", "Biggest account", "Kinds"].map((n) => (
                <Card key={n} name={n} chip={["dim", "Not synced yet"]} fig={<span title="Finary has not synced yet">-</span>} pic={<Share parts={[["soft", 1, 0.15]]} />} />
              ))}
            </Wall>
          </Block>
        </div>
      </div>
    );
  }

  const readAt = o.read_at;
  const inv: MoneyAccount[] = o.accounts.filter((a) => !a.is_debt && !NOT_INVESTED.has(a.account_type)).sort((a, b) => b.value_eur - a.value_eur);
  const worth = inv.reduce((s, a) => s + Math.max(0, a.value_eur), 0);
  const kinds = [...new Set(inv.map((a) => a.account_type))].map((t, i) => ({ t, name: kindName(t), rgb: RGB[i % RGB.length], v: inv.filter((a) => a.account_type === t).reduce((s, a) => s + Math.max(0, a.value_eur), 0) }));
  const rgbOf = (t: string) => kinds.find((k) => k.t === t)?.rgb ?? "141 152 168";
  const big = inv[0];
  const bad = inv.filter((a) => a.sync_status !== null && a.sync_status !== "success");
  const stale = o.stale === true;
  const syncTone = (a: MoneyAccount): [NlTone, string] => (stale ? ["warn", "Stale"] : a.sync_status === null ? ["run", "By hand"] : a.sync_status === "success" ? ["good", "In sync"] : ["warn", "Error"]);

  const lead = inv.length === 0 ? "You hold no positions yet." : `You hold ${inv.length} investment ${inv.length === 1 ? "account" : "accounts"} worth ${eur(worth)}.`;
  const more = inv.length === 0
    ? <>Finary lists no investment account in its read of {fmtDate(readAt)}.</>
    : <>Finary gives each account&apos;s balance, not what is inside it. The biggest is <em className="run">{big.name}{big.institution ? ` at ${big.institution}` : ""}, {worth > 0 ? pct0(big.value_eur / worth) : "-"} of the whole</em>. The money put in is not given, so what they earned is not shown.</>;

  return (
    <div className="v2-page">
      {header}
      <Say lead={lead} more={more} />
      <div className="tp-stack">
        <Block title="Summary">
          <Wall>
            <Card name="Worth now" hue="run" hover={readTitle("The investment accounts Finary lists, summed", readAt)} fig={eur(worth)}
              sub={`in ${inv.length} ${inv.length === 1 ? "account" : "accounts"}, as Finary counts them`}
              pic={<Share parts={inv.length ? inv.map((a, i): ["data", number, number] => ["data", Math.max(0, a.value_eur), Math.max(0.25, 0.9 - i * 0.15)]) : [["soft", 1, 0.15]]} />}
              facts={[["Financial assets", o.financial_assets === null ? "-" : eur(o.financial_assets)]]} />
            <Card name="Earned" chip={["dim", "Not read"]} fig={<span title={NO_COST}>-</span>} sub="worth now, less the money you put in"
              pic={<Share parts={[["soft", 1, 0.15]]} />} hover={NO_COST} />
            <Card name="Biggest account" chip={big ? undefined : ["dim", "Nothing held"]} hue={big ? "run" : ""} fig={big && worth > 0 ? pct0(big.value_eur / worth) : <span>-</span>}
              sub={big ? `${big.name}, ${eur(big.value_eur)}` : "its share of everything you hold"}
              pic={<Share parts={inv.length ? inv.map((a, i): ["data", number, number] => ["data", Math.max(0, a.value_eur), Math.max(0.25, 0.9 - i * 0.15)]) : [["soft", 1, 0.15]]} />} />
            <Card name="Investment accounts" hue={bad.length || stale ? "warn" : inv.length ? "good" : ""} chip={stale ? ["warn", "Stale"] : bad.length ? ["warn", "Sync error"] : inv.length ? ["good", "In sync"] : undefined}
              fig={String(inv.length)} sub={inv.map((a) => a.name).join(", ") || "none listed"}
              pic={<Share parts={[["good", inv.length - bad.length], ["warn", bad.length]]} />} />
          </Wall>
        </Block>

        <Block title="All positions" state={<State tone="run">{String(inv.length)}</State>}>
          {kinds.length > 0 && (
            <>
              <div className="tp-kbar">{kinds.map((k) => <i key={k.t} style={{ ...dotStyle(k.rgb), flex: `${k.v} 1 0` }} title={`${k.name}: ${eur(k.v)}`} />)}</div>
              <div className="tp-kinds">{kinds.map((k) => <span key={k.t} className="tp-kind"><i className="tp-dot" style={dotStyle(k.rgb)} /><span>{k.name}</span><b>{eur(k.v)}</b></span>)}</div>
            </>
          )}
          {inv.length === 0 ? (
            <p className="tp-empty">Your investment accounts hold nothing, or Finary lists none.</p>
          ) : (
            <div className="tp-scroll">
              <table className="tp-tbl" data-testid="positions">
                <thead><tr><th>Account</th><th>Bank</th><th>Share</th><th className="r" title={NO_COST}>Put in</th><th className="r">Worth</th><th /></tr></thead>
                <tbody>
                  {inv.map((a) => {
                    const s = worth > 0 ? Math.max(0, a.value_eur) / worth : null;
                    const [tone, word] = syncTone(a);
                    return (
                      <tr key={a.account_id}>
                        <td><i className="tp-dot" style={dotStyle(rgbOf(a.account_type))} /> {a.name}</td>
                        <td title={`${kindName(a.account_type)} account`}>{a.institution ?? "-"}</td>
                        <td className="num">{s === null ? "-" : <><Share parts={[["data", s], ["soft", 1 - s, 0.12]]} />{pct0(s)}</>}</td>
                        <td className="r num"><span title={NO_COST}>-</span></td>
                        <td className="r num">{eur(a.value_eur)}</td>
                        <td className="r"><span title={a.last_successful_sync_at ? `Last successful sync ${parisStamp(a.last_successful_sync_at)}` : "No connection: this account is entered by hand"}><State tone={tone}>{word}</State></span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Block>
      </div>
    </div>
  );
}
