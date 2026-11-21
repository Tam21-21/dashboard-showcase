import "./lookup.css";
import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { SearchResult } from "../../../../types/display";
import { tidy } from "./names";
import type { Company } from "./names";

// The loupe box and its menu (mockup: the frozen mockup of 2026-11-18, section 30, paintMenu and goCo). Every letter typed asks
// the server (/api/display/search); the rows are shown as served, twelve at most, led by "Closest matches" when the server says
// they are near misses. A pick clears the box and hands the company on.

const toCompany = (r: SearchResult): Company => ({ ticker: r.ticker, name: tidy(r.name), cik: r.cik, symbol: r.symbol });

export default function CompanyFind({ label, onPick }: { label: string; onPick: (c: Company) => void }) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState(0);
  const closeTimer = useRef<number | undefined>(undefined);
  const q = text.trim();
  const found = useQuery({
    queryKey: ["display", "search", q],
    queryFn: () => api.display.search(q),
    enabled: q !== "",
    placeholderData: keepPreviousData,
    retry: false,
  });
  useEffect(() => () => window.clearTimeout(closeTimer.current), []);

  const hits = q !== "" && found.isSuccess ? found.data.results.slice(0, 12) : [];
  const answered = found.isSuccess && !found.isPlaceholderData;
  const active = Math.min(at, Math.max(0, hits.length - 1));
  const near = hits.length > 0 && hits[0].closest === true;

  function go(r: SearchResult) {
    setText("");
    setAt(0);
    setOpen(false);
    onPick(toCompany(r));
  }
  function hl(s: string, mark: boolean) {
    if (!mark || !s.toUpperCase().startsWith(q.toUpperCase())) return s;
    return <><mark>{s.slice(0, q.length)}</mark>{s.slice(q.length)}</>;
  }

  let body: React.ReactNode = null;
  if (q !== "" && found.isError) body = <div className="lk-none">{serverDetail(found.error)}</div>;
  else if (q !== "" && answered && hits.length === 0) body = <div className="lk-none">{`No company or ticker starts with “${q}”`}</div>;
  else if (hits.length > 0) {
    body = (
      <>
        {near && <div className="lk-near">Closest matches</div>}
        {hits.map((r, i) => (
          <button
            key={r.symbol} type="button" role="option" id={`lk-opt-${i}`} className="lk-opt" aria-selected={i === active}
            onMouseDown={(e) => { e.preventDefault(); go(r); }}
          >
            <b>{hl(r.ticker, !r.closest)}</b>
            <span>{hl(tidy(r.name), !r.closest)}</span>
          </button>
        ))}
      </>
    );
  }
  const shown = open && q !== "" && body !== null;

  return (
    <div className="lk-find">
      <svg className="lk-loupe" viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.5 15.5 21 21" /></svg>
      <input
        className="mk-find search" type="search" role="combobox" aria-label={label} placeholder={label}
        aria-expanded={shown} aria-controls="lk-menu" aria-autocomplete="list" aria-activedescendant={shown && hits.length ? `lk-opt-${active}` : undefined}
        autoComplete="off" spellCheck={false} value={text}
        onChange={(e) => { setText(e.target.value); setAt(0); setOpen(true); }}
        onFocus={() => { window.clearTimeout(closeTimer.current); setOpen(true); }}
        onBlur={() => { closeTimer.current = window.setTimeout(() => setOpen(false), 150); }}
        onKeyDown={(e) => {
          if ((e.key === "ArrowDown" || e.key === "ArrowUp") && hits.length) {
            e.preventDefault();
            setAt((active + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length);
          } else if (e.key === "Enter" && hits[active]) {
            e.preventDefault();
            go(hits[active]);
          } else if (e.key === "Escape") {
            setText("");
            setAt(0);
          }
        }}
      />
      <div className="lk-menu" id="lk-menu" role="listbox" hidden={!shown}>{shown ? body : null}</div>
    </div>
  );
}
