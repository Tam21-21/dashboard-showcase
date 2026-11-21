// One read that maps a strategy id to its typed display name (lane DN1: GET /api/research/strategies/names,
// every strategy, active and deleted). Every page shows a strategy by id through strategyLabel, never by
// the stored name and never by a name derived from it.
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../../lib/api";
import { serverDetail } from "../../lib/errorText";

export const strategyNamesKey = ["strategies", "names"] as const;

export type StrategyNameRow = {
  id: number;
  name: string;
  display_name: string | null;
  status: string;
};

export function fetchStrategyNames(): Promise<StrategyNameRow[]> {
  return apiGet<StrategyNameRow[]>("/api/research/strategies/names");
}

export function useStrategyNames() {
  return useQuery({ queryKey: strategyNamesKey, queryFn: fetchStrategyNames });
}

export type StrategyNamesRead = { data: StrategyNameRow[] | undefined; isError: boolean; error: unknown };
export type StrategyLabel = { text: string; hover: string };

export function strategyLabel(q: StrategyNamesRead, id: number): StrategyLabel {
  if (q.isError) return { text: "-", hover: `Strategy names could not be read: ${serverDetail(q.error)}` };
  if (!q.data) return { text: "-", hover: "Loading the strategy names" };
  const row = q.data.find((r) => r.id === id);
  if (!row) return { text: `Strategy ${id}`, hover: "This strategy is not in the strategy list" };
  if (row.display_name == null) return { text: "-", hover: "No display name" };
  return { text: row.display_name, hover: row.status === "deleted" ? `${row.name}, deleted` : row.name };
}

// One colour per strategy, stable for that strategy (display_methods.md "Dot colour"). `ids` is the
// strategy ids in the order the names route serves them (by id); an id not in it, or names not yet
// loaded, gets the neutral grey. Hues skip the pass green and the fail red.
export const NEUTRAL_HUE = "#8d98a8";

export function strategyHue(id: number, ids: readonly number[]): string {
  const i = ids.indexOf(id);
  if (i < 0) return NEUTRAL_HUE;
  const f = (i * 0.618034 + 0.35) % 1;
  const a = f * 200;
  const hue = a < 40 ? 20 + a : 175 + (a - 40);
  return `hsl(${hue.toFixed(0)},78%,66%)`;
}
