import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { fmtDmy } from "../../lib/format";

// The date picker of the new event dialog: a field that shows DD/MM/YYYY and opens a month grid.
// Drawn after docs/design/rail_mockup_2026-10-10.html (nePop, the date branch). All date math is UTC,
// so the day clicked is the day stored. The value and onChange are ISO "YYYY-MM-DD".

const DAY_HEADS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const pad = (n: number) => String(n).padStart(2, "0");
const isoOf = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;

function todayIso(): string {
  const now = new Date();
  return `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}`;
}

// The first of the month shown, as UTC year and month (0-based).
function monthOf(iso: string): { y: number; m: number } {
  const hit = /^(\d{4})-(\d{2})-\d{2}$/.exec(iso);
  if (!hit) {
    const t = todayIso();
    return { y: Number(t.slice(0, 4)), m: Number(t.slice(5, 7)) - 1 };
  }
  return { y: Number(hit[1]), m: Number(hit[2]) - 1 };
}

// Open state that works with or without a parent holding it, closes on a press outside the field.
export function usePopover(open: boolean | undefined, onOpenChange: ((o: boolean) => void) | undefined) {
  const [own, setOwn] = useState(false);
  const isOpen = open ?? own;
  const ref = useRef<HTMLSpanElement>(null);
  const setOpen = (o: boolean) => {
    setOwn(o);
    onOpenChange?.(o);
  };
  const setOpenRef = useRef(setOpen);
  setOpenRef.current = setOpen;
  useEffect(() => {
    if (!isOpen) return;
    const away = (e: Event) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpenRef.current(false);
    };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [isOpen]);
  // Escape closes an open popover and goes no further.
  const onKeyDown = (e: KeyboardEvent) => {
    if (isOpen && e.key === "Escape") {
      e.stopPropagation();
      setOpen(false);
    }
  };
  return { isOpen, setOpen, ref, onKeyDown };
}

export function DatePick({
  value, onChange, ariaLabel, open, onOpenChange,
}: {
  value: string;
  onChange: (iso: string) => void;
  ariaLabel: string;
  open?: boolean;
  onOpenChange?: (o: boolean) => void;
}) {
  const { isOpen, setOpen, ref, onKeyDown } = usePopover(open, onOpenChange);
  const [view, setView] = useState(() => monthOf(value));

  function toggle() {
    if (!isOpen) setView(monthOf(value));
    setOpen(!isOpen);
  }
  function step(delta: number) {
    setView((v) => {
      const t = v.y * 12 + v.m + delta;
      return { y: Math.floor(t / 12), m: ((t % 12) + 12) % 12 };
    });
  }
  function pick(iso: string) {
    onChange(iso);
    setOpen(false);
  }

  const first = new Date(Date.UTC(view.y, view.m, 1));
  const lead = (first.getUTCDay() + 6) % 7;
  const today = todayIso();
  const cells = Array.from({ length: 42 }, (_, i) => new Date(Date.UTC(view.y, view.m, 1 - lead + i)));
  const shown = /^\d{4}-\d{2}-\d{2}$/.test(value) ? fmtDmy(Date.parse(`${value}T00:00:00Z`) / 1000) : "";

  return (
    <span className="pick" ref={ref} onKeyDown={onKeyDown}>
      <button type="button" className={"fin fpick" + (isOpen ? " open" : "")} aria-label={ariaLabel} onClick={toggle}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15" rx="2.5" /><path d="M3.5 10h17M8 3v4M16 3v4" /></svg>
        {shown}
      </button>
      {isOpen && (
        <div className="pop" role="dialog" aria-label="Choose a date">
          <div className="dph">
            <span className="dpn">
              <button type="button" aria-label="Previous year" onClick={() => step(-12)}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6l-6 6 6 6M11 6l-6 6 6 6" /></svg>
              </button>
              <button type="button" aria-label="Previous month" onClick={() => step(-1)}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6" /></svg>
              </button>
            </span>
            <b>{MONTHS[view.m]} {view.y}</b>
            <span className="dpn">
              <button type="button" aria-label="Next month" onClick={() => step(1)}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
              </button>
              <button type="button" aria-label="Next year" onClick={() => step(12)}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l6 6-6 6M13 6l6 6-6 6" /></svg>
              </button>
            </span>
          </div>
          <div className="dpg">
            {DAY_HEADS.map((h) => <span key={h} className="hd">{h}</span>)}
            {cells.map((d) => {
              const iso = isoOf(d);
              const cls = [d.getUTCMonth() !== view.m ? "out" : "", iso === today ? "tdy" : "", iso === value ? "sel" : ""]
                .filter(Boolean).join(" ");
              return (
                <button key={iso} type="button" className={cls || undefined} aria-pressed={iso === value} onClick={() => pick(iso)}>
                  {d.getUTCDate()}
                </button>
              );
            })}
          </div>
          <div className="dpf">
            <button type="button" onClick={() => pick(today)}>TODAY</button>
          </div>
        </div>
      )}
    </span>
  );
}
