// The Fundamentals tab's own calls. Most go through the shared api helper in lib/api.ts; the daily SEC check has no
// method there yet, so this module reads it with one plain GET and fails with the status and the server's words.
import { api } from "../../lib/api";

export const fundamentalsKeys = {
  status: ["fundamentals", "status"] as const,
  poll: ["fundamentals", "poll-status"] as const,
  definitions: ["fundamentals", "definitions"] as const,
  facts: (cik: number, asOf: number) => ["fundamentals", "facts", cik, asOf] as const,
  compare: (ciks: number[], asOf: number) => ["fundamentals", "compare", ciks.join(","), asOf] as const,
  universes: ["fundamentals", "universes"] as const,
  history: (cik: number) => ["fundamentals", "history", cik] as const,
};

/** The daily SEC check, as GET /api/research/fundamentals/poll-status answers (times are epoch seconds UTC). */
export type PollStatus = {
  last_poll: { finished_at?: number | null; started_at?: number | null; status?: string; failures?: unknown[] } | null;
  last_success_at: number | null;
  next_poll_at: number | null;
  late: boolean;
  companies_followed: number;
  filings_pending: number;
  filings_not_in_facts: number;
};

export async function fetchPollStatus(): Promise<PollStatus> {
  const url = "/api/research/fundamentals/poll-status";
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`The daily SEC check could not be read: ${res.status} ${res.statusText}`);
  return (await res.json()) as PollStatus;
}

export const fetchStatus = () => api.research.fundamentalsStatus();
export const fetchHistory = (cik: number, ticker: string) => api.research.fundamentalsHistory(cik, ticker);
export const fetchDefinitions = () => api.research.fundamentalsDefinitions();
export const fetchFacts = (cik: number, asOf: number) => api.research.fundamentalsFacts(cik, asOf);
export const fetchCompare = (ciks: number[], asOf: number) => api.research.fundamentalsCompare(ciks, [asOf]);
export const reapStuck = () => api.research.fundamentalsReapOrphaned();
export const startFetch = (body: { ciks?: number[]; universe_id?: number }) => api.research.fundamentalsBackfill(body);
export const startRefresh = (body: { ciks?: number[]; universe_id?: number }) => api.research.fundamentalsRefresh(body);
export const fetchUniverses = () => api.research.listDatasets("active", true);
