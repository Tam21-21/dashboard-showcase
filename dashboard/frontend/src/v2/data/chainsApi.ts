// The Chains page's own calls, over the shared api helper in lib/api.ts. Every query key sits under
// ["chains"], so the live event research.chain_changed (shell/live.ts) refetches all of them at once.
import { api } from "../../lib/api";
import type { ChainCreateBody, ChainStrategyOption } from "../../types/research";

export const chainsKeys = {
  list: ["chains", "list"] as const,
  detail: (id: number) => ["chains", "detail", id] as const,
  cells: (runId: number) => ["chains", "cells", runId] as const,
  strategies: ["chains", "strategies"] as const,
  status: (strategyId: number) => ["chains", "status", strategyId] as const,
};

export function fetchChains() {
  return api.research.chains();
}

export function fetchChain(id: number) {
  return api.research.chain(id);
}

export function fetchCells(runId: number) {
  return api.research.getRunCells(runId);
}

// The route answers with display_name; the shared StrategyListItem type does not name it yet.
export function fetchStrategyOptions(): Promise<ChainStrategyOption[]> {
  return api.research.listStrategies() as Promise<ChainStrategyOption[]>;
}

export function fetchSweepParams(strategyId: number) {
  return api.research.getStrategyStatus(strategyId);
}

// The four writes: each one call to the shared api, so one press is one request.
export function startChain(body: ChainCreateBody) {
  return api.research.createChain(body);
}

export function chooseCell(id: number, variationId: number, ticker: string) {
  return api.research.chooseChainCell(id, { variation_id: variationId, ticker });
}

export function stopChain(id: number) {
  return api.research.stopChain(id);
}

export function retryChain(id: number) {
  return api.research.retryChain(id);
}
