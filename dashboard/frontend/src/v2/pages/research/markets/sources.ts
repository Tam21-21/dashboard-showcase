import { useQuery } from "@tanstack/react-query";
import { api } from "../../../../lib/api";
import { serverDetail } from "../../../../lib/errorText";
import type { TileSources } from "./Tiles";
import { useBars } from "./useBars";

// The reads behind the Markets tiles, in one hook so the Markets tab and Home's MARKETS card share
// the same queries (same keys, so one call each in the cache): the daily bars of the symbols asked
// for, the yield curve and the commodities list.
export function useTileSources(symbols: string[]): TileSources {
  const bars = useBars(symbols);
  const yieldQ = useQuery({ queryKey: ["display", "yield-curve"], queryFn: () => api.display.getYieldCurve(), refetchInterval: 300_000 });
  const commoditiesQ = useQuery({ queryKey: ["display", "commodities"], queryFn: () => api.display.getCommodities(), refetchInterval: 300_000 });
  return {
    bars,
    tenors: yieldQ.data?.tenors,
    yieldError: yieldQ.isError ? serverDetail(yieldQ.error) : null,
    commodities: commoditiesQ.data?.commodities,
    commoditiesError: commoditiesQ.isError ? serverDetail(commoditiesQ.error) : null,
  };
}
