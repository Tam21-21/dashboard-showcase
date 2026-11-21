// The Datasets tab's own calls, over the shared api helper in lib/api.ts. Synthetic datasets are
// never asked for: fetchDatasets sends no include_synthetic argument, so the server hides them
// (CLAUDE.md, synthetic data stays out of research views).
import { api } from "../../lib/api";
import type { CreateDatasetBody } from "../../types/research";

export const datasetsKeys = {
  list: ["datasets", "list"] as const,
  feeds: ["datasets", "event-feeds"] as const,
  fundamentals: ["datasets", "fundamentals"] as const,
  members: (id: number) => ["datasets", "members", id] as const,
};

export function fetchDatasets() {
  return api.research.listDatasets();
}

// The tiles read only each feed's count and newest time, so one recent event per feed is enough.
export function fetchEventFeeds() {
  return api.research.getEventFeeds(1);
}

export function fetchFundamentalsStatus() {
  return api.research.fundamentalsStatus();
}

export function fetchUniverseMembers(id: number) {
  return api.research.priceUniverseMembers(id);
}

export function createSeries(body: CreateDatasetBody) {
  return api.research.createDataset(body);
}

export function fetchToLatest(id: number) {
  return api.research.updateDatasetToLatest(id);
}

export function deleteSeries(id: number) {
  return api.research.deleteDataset(id);
}

export function restoreSeries(id: number) {
  return api.research.restoreDataset(id);
}
