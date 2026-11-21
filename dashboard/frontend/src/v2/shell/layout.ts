import { useEffect, useState } from "react";
import type { Settings } from "../settings";

// A narrow touch screen is a phone (DECISIONS #484). Without a touch screen a narrow desktop window stays desktop.
export const PHONE_QUERY = "(pointer: coarse) and (max-width: 700px), (pointer: coarse) and (max-height: 500px)";

function mediaList(): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(PHONE_QUERY) : null;
}

export function useLayoutMode(layout: Settings["layout"]): "phone" | "desktop" {
  const [auto, setAuto] = useState<boolean>(() => mediaList()?.matches ?? false);
  useEffect(() => {
    if (layout !== "auto") return;
    const mql = mediaList();
    if (mql === null) return;
    const read = () => setAuto(mql.matches);
    read();
    mql.addEventListener("change", read);
    return () => mql.removeEventListener("change", read);
  }, [layout]);
  if (layout === "phone") return "phone";
  if (layout === "desktop") return "desktop";
  return auto ? "phone" : "desktop";
}
