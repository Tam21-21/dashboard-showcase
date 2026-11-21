import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useLayoutMode } from "../shell/layout";
import { loadSettings, type AppId } from "../settings";
import type { IconName } from "../shell/icons";

// The phone frame's parts (DECISIONS #484). The Shell provides LayoutContext; a page rendered alone
// follows the stored override instead.
export type LayoutMode = "phone" | "desktop";

export const LayoutContext = createContext<LayoutMode | null>(null);

export function useLayout(): LayoutMode {
  const ctx = useContext(LayoutContext);
  const own = useLayoutMode(loadSettings().settings.layout);
  return ctx ?? own;
}

export interface PhoneNavValue {
  app: AppId;
  name: string;
  sections: ReadonlyArray<{ id: string; label: string; icon: IconName }>;
  section: string | undefined;
}

export const PhoneNav = createContext<PhoneNavValue | null>(null);

/** Laptop-only controls: drawn on desktop, gone on a phone. */
export function PhoneOff({ children }: { children: ReactNode }) {
  return useLayout() === "desktop" ? <>{children}</> : null;
}

/** A red phone action that fires only after a press held for `ms`; the fill shows the press. */
export function HoldButton({
  label, onHold, ms = 1500, disabled = false,
}: { label: string; onHold: () => void; ms?: number; disabled?: boolean }) {
  const [holding, setHolding] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hold = useRef(onHold);
  hold.current = onHold;
  const stop = () => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    setHolding(false);
  };
  useEffect(() => stop, []);
  const start = () => {
    if (disabled || timer.current !== null) return;
    setHolding(true);
    timer.current = setTimeout(() => {
      timer.current = null;
      setHolding(false);
      hold.current();
    }, ms);
  };
  return (
    <button
      type="button"
      className={`hold${holding ? " holding" : ""}`}
      style={{ ["--hold-ms" as string]: `${ms}ms` }}
      disabled={disabled}
      onPointerDown={start}
      onPointerUp={stop}
      onPointerLeave={stop}
      onPointerCancel={stop}
      onContextMenu={(e) => e.preventDefault()}
    >
      <span>{label}</span>
    </button>
  );
}
