import { useQueries } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { HistoryBar } from "../../../../types/display";
import type { Fig } from "./series";

// Two years of daily bars for each symbol, from /api/display/history. The 252-day figures and the
// 200-day average need more than a year of closes (display_methods.md, Tiles), so every element
// reads the same "2y" answer and the server's cache serves them all from one call per symbol.
export const HISTORY_PERIOD = "2y";

export type BarsState = { bars: HistoryBar[]; reason: null } | { bars: null; reason: string };
export type BarsMap = Map<string, BarsState>;

export function useBars(symbols: string[]): BarsMap {
  const results = useQueries({
    queries: symbols.map((symbol) => ({
      queryKey: ["display", "history", symbol, HISTORY_PERIOD],
      queryFn: () => api.display.getHistory(symbol, HISTORY_PERIOD),
      retry: false,
      refetchInterval: 300_000,
    })),
  });
  const out: BarsMap = new Map();
  symbols.forEach((symbol, i) => {
    const q = results[i];
    if (q.isError) out.set(symbol, { bars: null, reason: serverDetail(q.error) });
    else if (q.data) out.set(symbol, { bars: q.data.bars, reason: null });
    else out.set(symbol, { bars: null, reason: "Loading." });
  });
  return out;
}

/** A figure from a series' state: the state's own reason when there are no bars, else the figure. */
export function figFrom(state: BarsState | undefined, fn: (bars: HistoryBar[]) => Fig): Fig {
  if (!state) return { value: null, reason: "This series was not asked for." };
  if (state.bars === null) return { value: null, reason: state.reason };
  return fn(state.bars);
}
