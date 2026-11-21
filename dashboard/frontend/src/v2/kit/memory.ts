import { useCallback, useEffect, useRef, useState } from "react";

// A page's typed or picked state, kept in the browser tab's session storage for 15 minutes after the
// viewer leaves the page. It is a per-viewer convenience: it is never sent to the server.
// Storage holds JSON {v, at, leftAt} under "v2-memory:<key>": v is the value, at the time of its last
// change, leftAt the time the page was left (null while the page is open or if it was never stamped).

const PREFIX = "v2-memory:";
const KEEP_MS = 15 * 60 * 1000;

interface Stored<T> {
  v: T;
  at: number;
  leftAt: number | null;
}

export function usePageMemory<T>(key: string, initial: T): [T, (next: T | ((prev: T) => T)) => void, () => void] {
  const storeKey = PREFIX + key;
  const logged = useRef(false);

  // Every storage call goes through here: an error is logged once for this key and the value then
  // lives in React state only, so the page keeps working for this visit.
  const guard = useCallback(
    <R,>(work: () => R): R | undefined => {
      try {
        return work();
      } catch (e) {
        if (!logged.current) {
          logged.current = true;
          console.error(`page memory ${key}: ${e instanceof Error ? e.message : String(e)}`);
        }
        return undefined;
      }
    },
    [key],
  );

  const [value, setValue] = useState<T>(() => {
    const kept = guard(() => {
      const raw = sessionStorage.getItem(storeKey);
      if (raw === null) return undefined;
      const s = JSON.parse(raw) as Stored<T>;
      if (Date.now() - (s.leftAt ?? s.at) > KEEP_MS) {
        sessionStorage.removeItem(storeKey);
        return undefined;
      }
      return s;
    });
    return kept ? kept.v : initial;
  });
  const latest = useRef(value);
  latest.current = value;
  const initialRef = useRef(initial);

  const stamp = useCallback(
    (leftAt: number | null) => {
      guard(() => {
        const raw = sessionStorage.getItem(storeKey);
        if (raw === null) return;
        const s = JSON.parse(raw) as Stored<T>;
        sessionStorage.setItem(storeKey, JSON.stringify({ ...s, leftAt }));
      });
    },
    [guard, storeKey],
  );

  // Leaving (unmount) or closing the tab (pagehide) starts the 15 minutes; coming back stops them.
  useEffect(() => {
    stamp(null);
    const left = () => stamp(Date.now());
    window.addEventListener("pagehide", left);
    return () => {
      window.removeEventListener("pagehide", left);
      left();
    };
  }, [stamp]);

  const set = useCallback(
    (next: T | ((prev: T) => T)) => {
      const v = typeof next === "function" ? (next as (prev: T) => T)(latest.current) : next;
      latest.current = v;
      setValue(v);
      guard(() => {
        const s: Stored<T> = { v, at: Date.now(), leftAt: null };
        sessionStorage.setItem(storeKey, JSON.stringify(s));
      });
    },
    [guard, storeKey],
  );

  const clear = useCallback(() => {
    latest.current = initialRef.current;
    setValue(initialRef.current);
    guard(() => sessionStorage.removeItem(storeKey));
  }, [guard, storeKey]);

  return [value, set, clear];
}
