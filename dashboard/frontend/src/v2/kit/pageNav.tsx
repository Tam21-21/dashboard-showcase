import { createContext, useContext } from "react";

export interface PageNav {
  prev: { label: string } | null;
  next: { label: string } | null;
  step: (d: -1 | 1) => void;
}

// V2App fills this with the open app's neighbouring sections, looping at both ends. A main of one
// section, and any page outside a main, gets none.
export const PageNavContext = createContext<PageNav | null>(null);

export function PageArrows() {
  const nav = useContext(PageNavContext);
  if (!nav || (!nav.prev && !nav.next)) return null;
  return (
    <div className="pnav">
      {nav.prev && (
        <button type="button" aria-label={`Previous page: ${nav.prev.label}`} title={nav.prev.label} onClick={() => nav.step(-1)}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 6l-6 6 6 6" /></svg>
        </button>
      )}
      {nav.next && (
        <button type="button" aria-label={`Next page: ${nav.next.label}`} title={nav.next.label} onClick={() => nav.step(1)}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
        </button>
      )}
    </div>
  );
}
