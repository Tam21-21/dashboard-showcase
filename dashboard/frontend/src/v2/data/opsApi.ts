// The Operations page's reads: systems and log from lane UI18-0 (dashboard/backend/routes/ops_systems.py), and the
// guard counters (dashboard/backend/routes/ops.py, GET /api/ops/counters).
import { apiGet } from "../../lib/api";

export type MarkKind = "ok" | "fail" | "none";

// One half hour of the last 24 hours. start is UTC, finding is null when nothing was checked.
export interface OpsMark {
  start: string;
  mark: MarkKind;
  finding: string | null;
}

export interface OpsSystem {
  name: string;
  where: string | null;
  word: "WORKING" | "IDLE" | "STALE" | "DOWN" | "NOT CONNECTED";
  tone: "good" | "quiet" | "warn" | "bad" | "off";
  reason: string | null;
  last_proven_at: string | null;
  marks: OpsMark[];
}

export interface OpsLogRow {
  at: string;
  time: string;
  system: string;
  sentence: string;
  kind: "event" | "transition";
  state: string | null;
}

// GET /api/ops/systems: every system, in the server's order, with 48 marks oldest first.
export function getSystems(): Promise<{ systems: OpsSystem[] }> {
  return apiGet<{ systems: OpsSystem[] }>("/api/ops/systems");
}

// GET /api/ops/log: today's events and state changes (Paris day), newest first.
export function getLog(): Promise<{ log: OpsLogRow[] }> {
  return apiGet<{ log: OpsLogRow[] }>("/api/ops/log");
}

// One counter the backend keeps for a refused write or a crashed loop. declared_only: the counter exists but no code
// adds to it yet.
export interface OpsCounter {
  module: string;
  id: string;
  value: number;
  declared_only: boolean;
}

// GET /api/ops/counters: every counter, read live on each call.
export function getCounters(): Promise<{ read_at: string; counters: OpsCounter[] }> {
  return apiGet<{ read_at: string; counters: OpsCounter[] }>("/api/ops/counters");
}
