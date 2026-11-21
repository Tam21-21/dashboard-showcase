import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { DatePick, Segmented, Window } from "../../../kit";
import { createSeries, fetchUniverseMembers } from "../../../data/datasetsApi";
import { plainStrategyName } from "../../../../lib/format";
import { plainDetail, serverDetail } from "../../../../lib/errorText";
import type { Dataset, DatasetSource, Timeframe } from "../../../../types/research";

// The ADD DATA dialog, prices only (mockup: docs/design/rail_mockup_2026-10-10.html, addData). News,
// ratings, filings and fundamentals are left out: the server has no create call for them here.

const SOURCES: Array<{ label: string; value: DatasetSource }> = [
  { label: "EODHD", value: "eodhd" },
  { label: "Alpaca", value: "alpaca_sip" },
];
const BARS: Array<{ label: string; value: Timeframe }> = [
  { label: "1 minute", value: "1m" },
  { label: "5 minutes", value: "5m" },
  { label: "15 minutes", value: "15m" },
  { label: "Hourly", value: "1h" },
  { label: "Daily", value: "1d" },
];
// The mockup's first day for a new series; the last day is today (UTC).
const FIRST_DAY = "2000-01-01";

// A server reason that is a machine code shows as words; a sentence stays as the server wrote it.
export function wordsOf(detail: string): string {
  return plainDetail(detail);
}

const todayIso = () => new Date().toISOString().slice(0, 10);

export default function AddData({
  held, universes, onClose, onDone,
}: { held: Dataset[]; universes: Dataset[]; onClose: () => void; onDone: (count: number) => void }) {
  const qc = useQueryClient();
  const [source, setSource] = useState<DatasetSource>("eodhd");
  const [bars, setBars] = useState<Timeframe>("1d");
  const [typed, setTyped] = useState("");
  const [universe, setUniverse] = useState("");
  const [from, setFrom] = useState(FIRST_DAY);
  const [to, setTo] = useState(todayIso);
  const [open, setOpen] = useState<"from" | "to" | null>(null);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<string[]>([]);


  async function run() {
    setBusy(true);
    setNotes([]);
    try {
      let tickers: string[];
      if (universe) {
        try {
          tickers = (await fetchUniverseMembers(Number(universe))).members;
        } catch (e) {
          setNotes([serverDetail(e)]);
          return;
        }
      } else {
        tickers = typed.split(/[\s,;]+/);
      }
      const wanted = [...new Set(tickers.map((t) => t.trim().toUpperCase()).filter(Boolean))];
      if (wanted.length === 0) {
        setNotes(["Type a ticker or choose a universe."]);
        return;
      }
      const isHeld = (t: string) => held.some((d) => d.source === source && d.timeframe === bars && d.symbol === t);
      const todo = wanted.filter((t) => !isHeld(t));
      const skipped = wanted.filter(isHeld);
      const refused: string[] = [];
      let made = 0;
      for (const symbol of todo) {
        try {
          await createSeries({ name: `${symbol} ${source} ${bars}`, source, symbol, timeframe: bars, start: `${from}T00:00:00Z`, end: `${to}T00:00:00Z` });
          made += 1;
        } catch (e) {
          refused.push(`${symbol}: ${wordsOf(serverDetail(e))}`);
        }
      }
      await qc.invalidateQueries({ queryKey: ["datasets"] });
      if (made > 0) onDone(made);
      if (refused.length === 0 && made > 0) {
        onClose();
        return;
      }
      setNotes([...refused, ...(skipped.length > 0 ? [`Already held: ${skipped.join(", ")}`] : [])]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Window label="Add data" title="Add data" size="m" className="ds-add" onClose={onClose}>
        <div className="ds-row"><span>Source</span><Segmented options={SOURCES} value={source} onChange={(v) => setSource(v as DatasetSource)} /></div>
        <div className="ds-row">
          <span>Tickers</span>
          <input
            aria-label="Tickers"
            placeholder="AAPL MSFT"
            value={typed}
            disabled={universe !== ""}
            onChange={(e) => setTyped(e.target.value)}
          />
        </div>
        <div className="ds-row">
          <span />
          <select aria-label="Universe" className="fin" value={universe} onChange={(e) => setUniverse(e.target.value)}>
            <option value="">or every member of a universe</option>
            {universes.map((u) => <option key={u.id} value={String(u.id)}>{plainStrategyName(u.name)}</option>)}
          </select>
        </div>
        <div className="ds-row"><span>Bars</span><Segmented options={BARS} value={bars} onChange={(v) => setBars(v as Timeframe)} /></div>
        <div className="ds-row">
          <span>From</span>
          <DatePick value={from} onChange={setFrom} ariaLabel="From" open={open === "from"} onOpenChange={(o) => setOpen(o ? "from" : null)} />
          <span>To</span>
          <DatePick value={to} onChange={setTo} ariaLabel="To" open={open === "to"} onOpenChange={(o) => setOpen(o ? "to" : null)} />
        </div>
        {notes.length > 0 && (
          <div className="ds-notes" role="alert">
            {notes.map((n) => <div key={n}>{n}</div>)}
          </div>
        )}
        <div className="ds-foot">
          <button type="button" className="btn q" onClick={onClose}>Cancel</button>
          <button type="button" className="btn" disabled={busy} onClick={() => void run()}>Fetch</button>
        </div>
    </Window>
  );
}
