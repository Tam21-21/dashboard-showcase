import { useEffect } from "react";
import type { ReactNode } from "react";

// A short message that closes itself (the mockup's .toast). The timer restarts when the message changes
// and is cleared when the toast goes away.
export function Toast({ children, onClose, ms = 6000 }: { children: ReactNode; onClose: () => void; ms?: number }) {
  useEffect(() => {
    const t = setTimeout(onClose, ms);
    return () => clearTimeout(t);
  }, [children, ms]);
  return <div className="nl-toast" role="status">{children}</div>;
}
