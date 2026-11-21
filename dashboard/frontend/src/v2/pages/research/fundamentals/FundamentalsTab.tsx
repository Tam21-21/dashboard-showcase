import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Block, DatePick, Say, Segmented, Window } from "../../../kit";
import { serverDetail } from "../../../../lib/errorText";
import { dateToEpoch } from "../../../../lib/format";
import {
  fetchCompare, fetchDefinitions, fetchFacts, fetchPollStatus, fetchStatus, fetchUniverses, fundamentalsKeys, reapStuck,
  startFetch, startRefresh,
} from "../../../data/fundamentalsApi";
import { PhoneOff } from "../../../kit/phone";
import { isStale } from "../datasets/stale";
import { LibCard } from "../markets/LibCard";
import type { CardTone } from "../markets/LibCard";
import "./fundamentals.css";

// The Fundamentals tab of the Data library (mockup: the frozen mockup of 2026-11-15, lab-p-fundamentals.js). Company figures
// from SEC filings, each shown as it was known on a date the way a backtest must see it. Every figure comes from the server's
// fundamentals routes: status (facts held, by filing year, recent fetches), the daily SEC check, definitions, facts and compare.
// Where the server returns nothing for a part, the part says so instead of drawing a number. The mockup's made-up Example view
// is not drawn: the page shows what the server holds.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const count = (n: number) => n.toLocaleString("en-US");
const dayYear = (epoch: number) => { const d = new Date(epoch * 1000); return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`; };
const clock = (epoch: number) => { const d = new Date(epoch * 1000); return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`; };
const iso = (epoch: number) => new Date(epoch * 1000).toISOString().slice(0, 10);
const big = (n: number) => (n >= 1e6 ? `${Number((n / 1e6).toFixed(2))}M` : n >= 1e3 ? `${Number((n / 1e3).toFixed(0))}k` : String(n));
const money = (v: number) => (Math.abs(v) >= 1e9 ? `$${(v / 1e9).toFixed(2)}bn` : Math.abs(v) >= 1e6 ? `$${Math.round(v / 1e6)}m` : v.toLocaleString("en-US"));
const BATCH_WORD: Record<string, [CardTone, string]> = {
  running: ["run", "Running"], succeeded: ["good", "Finished"], succeeded_with_quarantines: ["warn", "Finished, some figures held back"],
  failed: ["bad", "Failed"], cancelled_partial: ["warn", "Stopped part way"],
};
const KIND_WORD: Record<string, string> = { fetch: "First fetch", refresh: "Refresh" };
const ciksOf = (typed: string): number[] => typed.split(/[\s,]+/).filter(Boolean).map(Number).filter((n) => Number.isInteger(n) && n > 0);

function FetchWindow({ stuck, onClose, onDone }: { stuck: boolean; onClose: () => void; onDone: (text: string) => void }) {
  const [kind, setKind] = useState("refresh");
  const [who, setWho] = useState("list");
  const [typed, setTyped] = useState("");
  const [uid, setUid] = useState<string>("");
  const [note, setNote] = useState<string | null>(null);
  const uq = useQuery({ queryKey: fundamentalsKeys.universes, queryFn: () => fetchUniverses() });
  const lists = (uq.data ?? []).filter((d) => d.dataset_type === "price_universe" && d.status !== "deleted");
  const go = useMutation({
    mutationFn: () => {
      const body = who === "list" ? { universe_id: Number(uid) } : { ciks: ciksOf(typed) };
      return kind === "refresh" ? startRefresh(body) : startFetch(body);
    },
    onSuccess: (r) => { onDone(`Started: ${r.cik_count} companies`); onClose(); },
    onError: (e) => setNote(serverDetail(e)),
  });
  const ready = who === "list" ? uid !== "" : ciksOf(typed).length > 0;
  return (
    <Window label="Fetch or refresh company figures" size="m" onClose={onClose}>
      <div className="h2"><span>Fetch or refresh company figures</span></div>
      <p className="fd-note">A first fetch reads every report of the companies you pick. A refresh reads only what was filed since the last fetch. Both keep the day each figure became public.</p>
      <div className="fd-form">
        <div><span>Kind</span><Segmented variant="seg" value={kind} onChange={setKind} options={[{ label: "Refresh", value: "refresh" }, { label: "First fetch", value: "fetch" }]} /></div>
        <div><span>Companies</span><Segmented variant="seg" value={who} onChange={setWho} options={[{ label: "A stock list", value: "list" }, { label: "Company numbers", value: "ciks" }]} /></div>
        {who === "list" ? (
          <label><span>Stock list</span>
            <select className="fin" aria-label="Stock list" value={uid} onChange={(e) => setUid(e.target.value)}>
              <option value="">Pick a stock list</option>
              {lists.map((d) => <option key={d.id} value={d.id}>{d.display_name ?? d.name}</option>)}
            </select>
          </label>
        ) : (
          <label><span>SEC company numbers</span>
            <input className="fin" aria-label="SEC company numbers" placeholder="One or more, with commas between" value={typed} onChange={(e) => setTyped(e.target.value)} />
          </label>
        )}
      </div>
      {stuck && <div className="fd-warn">A fetch is stuck in running. Clear it first, under Needs you.</div>}
      {note && <div className="fd-err" role="alert">{note}</div>}
      <div className="fd-foot">
        <button type="button" className="btn q" onClick={onClose}>Cancel</button>
        <button type="button" className="btn" disabled={!ready || go.isPending || stuck} onClick={() => go.mutate()}>Start</button>
      </div>
    </Window>
  );
}

export default function FundamentalsTab(_props: { slot?: HTMLElement | null } = {}) {
  const qc = useQueryClient();
  const statusQ = useQuery({ queryKey: fundamentalsKeys.status, queryFn: () => fetchStatus() });
  const pollQ = useQuery({ queryKey: fundamentalsKeys.poll, queryFn: () => fetchPollStatus() });
  const defsQ = useQuery({ queryKey: fundamentalsKeys.definitions, queryFn: () => fetchDefinitions(), staleTime: Infinity });

  const [fetching, setFetching] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [cikText, setCikText] = useState("");
  const [asOf, setAsOf] = useState(() => new Date().toISOString().slice(0, 10));
  const [asked, setAsked] = useState<{ ciks: number[]; asOf: number } | null>(null);

  const reap = useMutation({
    mutationFn: () => reapStuck(),
    onSuccess: (r) => { setToast(`Cleared ${r.batches_reaped.length} stuck ${r.batches_reaped.length === 1 ? "fetch" : "fetches"}`); void qc.invalidateQueries({ queryKey: ["fundamentals"] }); },
    onError: (e) => setToast(serverDetail(e)),
  });

  const now = Math.floor(Date.now() / 1000);
  const st = statusQ.data;
  const poll = pollQ.data;
  const statusError = statusQ.isError ? serverDetail(statusQ.error) : null;
  const pollError = pollQ.isError ? serverDetail(pollQ.error) : null;
  const batches = st?.recent_batches ?? [];
  const stuckBatches = batches.filter((b) => b.status === "running" && b.started_at != null && now - b.started_at > 86400);
  const held = st?.total_facts_on_record ?? 0;
  const years = (st?.per_year ?? []).filter((p) => p.count > 0);
  const topYear = Math.max(1, ...years.map((p) => p.count));
  const newest = st?.latest_filing_date ?? null;
  const age = newest == null ? null : Math.floor((now - newest) / 86400);

  const asOfEpoch = dateToEpoch(asOf);
  const factsQ = useQuery({
    queryKey: asked && asked.ciks.length === 1 ? fundamentalsKeys.facts(asked.ciks[0], asked.asOf) : ["fundamentals", "none"],
    queryFn: () => fetchFacts(asked!.ciks[0], asked!.asOf),
    enabled: asked != null && asked.ciks.length === 1,
    retry: false,
  });
  const cmpQ = useQuery({
    queryKey: asked && asked.ciks.length > 1 ? fundamentalsKeys.compare(asked.ciks, asked.asOf) : ["fundamentals", "none-compare"],
    queryFn: () => fetchCompare(asked!.ciks, asked!.asOf),
    enabled: asked != null && asked.ciks.length > 1,
    retry: false,
  });
  function ask() {
    const ciks = ciksOf(cikText);
    if (ciks.length === 0 || asOfEpoch == null) return;
    setAsked({ ciks, asOf: asOfEpoch + 86399 });
  }

  let lead: string;
  let more: string;
  if (statusQ.isPending) { lead = "Loading the company figures."; more = ""; }
  else if (statusError) { lead = "The company figures could not be read."; more = statusError; }
  else if (held === 0) { lead = "No company figures are stored."; more = "They come from the reports companies file with the SEC, the US market regulator."; }
  else {
    lead = `${count(held)} company figures are stored.`;
    more = `They come from the reports companies file with the SEC, the US market regulator.${newest != null ? ` The newest filing is from ${dayYear(newest)}.` : ""}`;
  }

  const freshChip: { tone: CardTone; text: string } = newest == null ? { tone: "dim", text: "Not read" } : isStale(newest, now) ? { tone: "warn", text: "Stale" } : { tone: "good", text: "Working" };
  const defs = defsQ.data;

  const buttons = (
    <PhoneOff>
      <button type="button" className="btn" onClick={() => setFetching(true)}>Fetch or refresh</button>
    </PhoneOff>
  );

  return (
    <div className="fd">
      <Say lead={lead} more={more || undefined} />

      <Block title="Needs you" state={<span className={`nl-st ${stuckBatches.length || poll?.late ? "warn" : "good"}`}>{stuckBatches.length ? `${stuckBatches.length} thing${stuckBatches.length === 1 ? "" : "s"}` : poll?.late ? "1 thing" : "All clear"}</span>}>
        {stuckBatches.length === 0 && !poll?.late && <div className="fd-none">Nothing waits for you. No fetch is stuck and the daily SEC check is on time.</div>}
        {stuckBatches.length > 0 && (
          <div className="fd-stop">
            <span className="nl-st warn">Stuck</span>
            <div><b>{`A fetch has been running since ${dayYear(stuckBatches[0].started_at!)}`}</b><span>It stopped without finishing, and no new fetch can start while it counts as running. Clearing it marks it as failed. No figure is deleted.</span></div>
            <button type="button" className="btn" disabled={reap.isPending} onClick={() => reap.mutate()}>Clear it</button>
          </div>
        )}
        {poll?.late && <div className="fd-stop"><span className="nl-st warn">Late</span><div><b>The daily SEC check is late</b><span>It has not finished a pass in over a day.</span></div></div>}
      </Block>

      <Block title={<span title="What is held and how fresh it is. The data counts as stale once the newest filing is over 7 days old">Summary</span>}>
        <div className="lc-wall fd-four">
          <LibCard
            name="Facts held" hue={held > 0 ? "good" : ""} chip={statusQ.isPending ? { tone: "dim", text: "Loading" } : held > 0 ? { tone: "good", text: "Held" } : { tone: "dim", text: "Nothing held" }}
            fig={statusQ.isPending ? "-" : big(held)} unit="figures from SEC filings"
            pic={years.length > 0 ? <span className="fd-mini">{years.map((p) => <i key={p.year} style={{ height: `${Math.max(8, (p.count / topYear) * 100)}%` }} title={`Filed in ${p.year}: ${count(p.count)} figures`} />)}</span> : undefined}
            bar={[{ tone: "soft", n: held > 0 ? 1 : 0.0001, colour: "#ec6eaa" }]}
            facts={[{ label: "From", value: years.length ? String(years[0].year) : "-" }, { label: "To", value: years.length ? String(years[years.length - 1].year) : "-" }]}
            hover="Source: SEC EDGAR" testId="fd-card"
          />
          <LibCard
            name="Companies" chip={pollQ.isPending ? { tone: "dim", text: "Loading" } : poll ? { tone: "data", text: "Followed" } : { tone: "dim", text: "Not read" }}
            fig={poll ? count(poll.companies_followed) : "-"} unit="companies with figures"
            facts={[{ label: "Filings waiting", value: poll ? count(poll.filings_pending) : "-" }, { label: "Not in facts", value: poll ? count(poll.filings_not_in_facts) : "-" }]}
            hover={pollError ?? "Companies whose filings the daily check follows"} testId="fd-card"
          />
          <LibCard
            name="Newest filing" hue={freshChip.tone} chip={freshChip}
            fig={age == null ? "-" : `${age} days`} unit={age == null ? "needed to say fresh or stale" : "ago"}
            bar={age == null ? [] : [{ tone: age > 7 ? "warn" : "good", n: Math.min(7, age) || 0.0001 }, { tone: "soft", n: Math.max(0, 7 - age) }]}
            facts={[{ label: "Filed", value: newest == null ? "-" : dayYear(newest) }, { label: "Stale after", value: "7 days" }]}
            hover="Source: SEC EDGAR filing dates" testId="fd-card"
          />
          <LibCard
            name="Daily SEC check" hue={poll ? (poll.late ? "warn" : "good") : ""} chip={poll ? (poll.late ? { tone: "warn", text: "Late" } : { tone: "good", text: "On time" }) : { tone: "dim", text: pollError ? "Not read" : "Loading" }}
            fig={poll?.last_success_at != null ? clock(poll.last_success_at) : "-"} unit={poll?.last_success_at != null ? `last pass, ${dayYear(poll.last_success_at)}` : "last pass and next due time"}
            facts={[{ label: "Next due", value: poll?.next_poll_at != null ? clock(poll.next_poll_at) : "-" }]}
            hover={pollError ?? "When the server last looked for new filings, and when it looks next"} testId="fd-card"
          />
        </div>
      </Block>

      <Block title={<span title="Type one SEC company number, or several to compare, and pick a date. The page shows only what had been filed by that date">One company, as it was known</span>} state={asked && asked.ciks.length === 1 ? <span className="nl-st run">{`${asked.ciks[0]}, known on ${iso(asked.asOf)}`}</span> : undefined}
>
        <div className="fd-ctl">
          <input className="fin" aria-label="SEC company numbers" placeholder="SEC company numbers, with commas between" value={cikText} onChange={(e) => setCikText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
          <span className="fd-when"><span>Known on</span><DatePick value={asOf} onChange={setAsOf} ariaLabel="Known on" /></span>
          <button type="button" className="btn" disabled={ciksOf(cikText).length === 0 || asOfEpoch == null} onClick={ask}>Look up</button>
        </div>
        {!asked && <div className="fd-none">Nothing looked up yet.</div>}
        {asked && asked.ciks.length === 1 && (factsQ.isError ? <div className="fd-err">{serverDetail(factsQ.error)}</div> : factsQ.isPending ? <div className="fd-none">Loading.</div> : (
          <>
            {!factsQ.data.on_record && <div className="fd-none">No figure of this company is held.</div>}
            <table className="fd-tbl" data-testid="fd-facts">
              <thead><tr><th>Figure</th><th className="r">As known</th><th>Year</th><th>Filed</th></tr></thead>
              <tbody>
                {factsQ.data.facts.map((f) => (
                  <tr key={f.concept}>
                    <td>{defs?.figures[f.concept]?.label ?? f.concept}</td>
                    <td className="num r">{f.found && f.value != null ? money(f.value) : <span title="No figure had been filed by this date">-</span>}</td>
                    <td className="num">{f.fiscal_year ?? "-"}</td>
                    <td className="num">{f.filing_date != null ? dayYear(f.filing_date) : "-"}</td>
                  </tr>
                ))}
                {factsQ.data.ratios.map((r) => (
                  <tr key={r.name}>
                    <td title={defs?.ratios[r.name]?.formula}>{r.label}</td>
                    <td className="num r">{r.available && r.value != null ? r.value.toFixed(3) : <span title={r.reason}>-</span>}</td>
                    <td /><td />
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ))}
        {asked && asked.ciks.length > 1 && (cmpQ.isError ? <div className="fd-err">{serverDetail(cmpQ.error)}</div> : cmpQ.isPending ? <div className="fd-none">Loading.</div> : (
          <table className="fd-tbl" data-testid="fd-compare">
            <thead><tr><th>Figure</th>{cmpQ.data.cells.map((c) => <th key={c.cik} className="r">{c.cik}</th>)}</tr></thead>
            <tbody>
              {(cmpQ.data.cells[0]?.facts ?? []).map((f, i) => (
                <tr key={f.concept}>
                  <td>{defs?.figures[f.concept]?.label ?? f.concept}</td>
                  {cmpQ.data.cells.map((c) => { const x = c.facts[i]; return <td key={c.cik} className="num r">{x && x.found && x.value != null ? money(x.value) : "-"}</td>; })}
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      </Block>

      <Block title="Facts by filing year" state={years.length ? <span className="nl-st run">{`${years[0].year} to ${years[years.length - 1].year}`}</span> : undefined}>
        {years.length === 0 ? <div className="fd-none">{statusQ.isPending ? "Loading." : "No filing year is held yet."}</div> : (
          <>
            <div className="fd-plot"><div className="fd-yax num"><span>{count(topYear)}</span><span>{count(Math.round(topYear / 2))}</span><span>0</span></div><div className="fd-years" data-testid="fd-years" role="img" aria-label="Company figures filed in each year">
              {years.map((p) => <i key={p.year} style={{ height: `${Math.max(3, (p.count / topYear) * 100)}%` }} title={`Filed in ${p.year}: ${count(p.count)} figures (${Math.round((p.count / held) * 100)}% of all)`} />)}
            </div></div>
            <div className="fd-axis num">{years.map((p) => <span key={p.year}>{p.year}</span>)}</div>
          </>
        )}
      </Block>

      <Block title="What each figure means" state={defs ? <span className="nl-st run">{`${Object.keys(defs.figures).length} figures, ${Object.keys(defs.ratios).length} ratios`}</span> : undefined}>
        {defsQ.isError ? <div className="fd-err">{serverDetail(defsQ.error)}</div> : !defs ? <div className="fd-none">Loading.</div> : (
          <div className="fd-defs">
            <div><p className="fd-cap">Figures, as filed</p>{Object.values(defs.figures).map((d) => <div key={d.name}><b>{d.label}</b><span>{d.definition}</span></div>)}</div>
            <div><p className="fd-cap">Ratios, worked out from the figures</p>{Object.values(defs.ratios).map((d) => <div key={d.name}><b>{d.label}</b><span>{d.definition}</span>{d.formula && <code>{d.formula}</code>}</div>)}</div>
          </div>
        )}
      </Block>

      <Block title="Fetches" state={<span className="nl-st run">{`${batches.length} newest`}</span>} right={buttons}>
        {statusError ? <div className="fd-err">{statusError}</div> : batches.length === 0 ? <div className="fd-none">No fetch yet.</div> : (
          <table className="fd-tbl" data-testid="fd-batches">
            <thead><tr><th>Started</th><th>Kind</th><th className="r">Companies</th><th className="r">Figures added</th><th>State</th></tr></thead>
            <tbody>
              {batches.map((b) => {
                const w = BATCH_WORD[b.status] ?? ["dim" as CardTone, b.status];
                return (
                  <tr key={b.batch_id}>
                    <td className="num">{b.started_at != null ? dayYear(b.started_at) : "-"}</td>
                    <td>{KIND_WORD[b.kind] ?? b.kind}</td>
                    <td className="num r">{count(b.cik_count)}</td>
                    <td className="num r">{count(b.fact_count)}</td>
                    <td><span className={`nl-st ${w[0]}`} title={b.failure_class ?? undefined}>{w[1]}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Block>

      {fetching && <FetchWindow stuck={stuckBatches.length > 0} onClose={() => setFetching(false)} onDone={(t) => { setToast(t); void qc.invalidateQueries({ queryKey: ["fundamentals"] }); }} />}
      {toast && <div className="fd-toast" role="status"><span>{toast}</span><button type="button" className="btn q" onClick={() => setToast(null)}>OK</button></div>}
    </div>
  );
}
