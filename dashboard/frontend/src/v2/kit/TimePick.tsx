import { useLayoutEffect, useRef, useState } from "react";
import { usePopover } from "./DatePick";

// The time picker of the new event dialog: a text field that opens a list of quarter hours.
// Drawn after docs/design/rail_mockup_2026-10-10.html (nePop, the time branch; parseT; lenTxt).
// The value is "HH:MM". Typing 930, 9:30 or 21h works and is rounded to the nearest quarter hour.

const pad = (n: number) => String(n).padStart(2, "0");
const fmt = (min: number) => `${pad(Math.floor(min / 60))}:${pad(min % 60)}`;

function toMin(hhmm: string): number | null {
  const hit = /^(\d{2}):(\d{2})$/.exec(hhmm);
  return hit ? Number(hit[1]) * 60 + Number(hit[2]) : null;
}

// Text to minutes since midnight, or null when it is not a time. 24:00 and later are refused.
function parseTime(text: string): number | null {
  const hit = /^(\d{1,2})(?:[:h.]?(\d{2})?)?$/.exec(text.trim().toLowerCase());
  if (!hit) return null;
  const h = Number(hit[1]);
  const mi = Number(hit[2] ?? 0);
  if (h >= 24 || mi >= 60) return null;
  const total = h * 60 + Math.round(mi / 15) * 15;
  return total >= 24 * 60 ? null : total;
}

// A length for the hover of an end time.
function lenText(m: number): string {
  if (m < 60) return `${m} minutes`;
  if (m % 60) return `${Math.floor(m / 60)} h ${m % 60}`;
  return m === 60 ? "1 hour" : `${m / 60} hours`;
}

export function TimePick({
  value, onChange, ariaLabel, after, open, onOpenChange,
}: {
  value: string;
  onChange: (v: string) => void;
  ariaLabel: string;
  after?: string;
  open?: boolean;
  onOpenChange?: (o: boolean) => void;
}) {
  const { isOpen, setOpen, ref, onKeyDown } = usePopover(open, onOpenChange);
  const [draft, setDraft] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const afterMin = after === undefined ? null : toMin(after);
  const from = afterMin === null ? 0 : afterMin + 15;
  const last = afterMin === null ? 23 * 60 + 30 : 23 * 60 + 45;
  const times: number[] = [];
  for (let t = from; t <= last; t += 15) times.push(t);

  useLayoutEffect(() => {
    if (!isOpen || !listRef.current) return;
    const chosen = listRef.current.querySelector<HTMLElement>('[aria-selected="true"]');
    if (chosen) listRef.current.scrollTop = chosen.offsetTop - 90;
  }, [isOpen]);

  function typed(text: string) {
    setDraft(text);
    const m = parseTime(text);
    if (m !== null) onChange(fmt(m));
  }

  return (
    <span className="pick" ref={ref} onKeyDown={onKeyDown}>
      <input
        type="text"
        className={"fin fpick" + (isOpen ? " open" : "")}
        aria-label={ariaLabel}
        value={draft ?? value}
        onChange={(e) => typed(e.target.value)}
        onBlur={() => setDraft(null)}
        onClick={() => setOpen(!isOpen)}
      />
      {isOpen && (
        <div className="pop">
          <div className="tpk" role="listbox" ref={listRef}>
            {times.map((t) => {
              const label = fmt(t);
              return (
                <span
                  key={t}
                  role="option"
                  aria-selected={label === value}
                  className={label === value ? "sel" : undefined}
                  title={afterMin === null ? undefined : lenText(t - afterMin)}
                  onClick={() => {
                    setDraft(null);
                    onChange(label);
                    setOpen(false);
                  }}
                >
                  {label}
                </span>
              );
            })}
          </div>
        </div>
      )}
    </span>
  );
}
