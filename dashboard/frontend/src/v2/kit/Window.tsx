import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

export type WindowSize = "s" | "m" | "l" | "xl";

type Props = {
  label: string;
  /** The window's own heading with its Close button, as the mockup's windows have (K.win); left out, the page draws its own. */
  title?: string;
  size: WindowSize;
  onClose: () => void;
  className?: string;
  backdropCloses?: boolean;
  escapeCloses?: boolean;
  children: ReactNode;
};

// One solid window for every page: a dimmed overlay and a card with its own width.
export function Window({
  label, title, size, onClose, className, backdropCloses = true, escapeCloses = true, children,
}: Props) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!escapeCloses) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [escapeCloses]);

  const cls = ["win", `win-${size}`, className].filter(Boolean).join(" ");
  return (
    <div
      className="ov"
      onMouseDown={(e) => {
        if (backdropCloses && e.target === e.currentTarget) closeRef.current();
      }}
    >
      <div className={cls} role="dialog" aria-modal="true" aria-label={label}>
        {title != null && (
          <div className="win-h"><h2>{title}</h2><button type="button" className="btn" onClick={() => closeRef.current()}>Close</button></div>
        )}
        {children}
      </div>
    </div>
  );
}
