/**
 * The global-refresh registry (item 7 / D13).
 *
 * The header's one refresh button must refresh genuinely everything: re-trigger the
 * server-side external syncs (Garmin, Calendar, Benzinga — fetches the dashboard would
 * otherwise never re-run), reconnect a dead live stream, and refetch every React Query.
 * Hardcoding that list in App.tsx does not scale: every new syncable feature would have to
 * edit App.tsx. Instead each feature registers its own refresher ONCE at module init, and
 * App.tsx just calls runAllRefreshers(). Adding a feature is then one registerSync call.
 *
 * Module-init registration only fires for EAGERLY imported modules. The current syncs live in
 * lib/api.ts, which is eagerly imported by the whole app, so they register on load (Cowork M2).
 * A future lazily-loaded feature must call registerSync from its own mount effect instead.
 */

// A refresher is any zero-arg async action: a server sync, or a stream reconnect.
export type Refresher = () => Promise<unknown> | void;

interface SyncRegistration {
  label: string; // human label, used to report which sync failed and as the throttle key
  run: Refresher; // the actual sync call
  minIntervalMs: number; // throttle: skip a re-run within this window (rate-limited upstream APIs)
}

// Two distinct lists: server syncs (their failures are user-visible) and stream refreshers
// (a failed reconnect just leaves the connection badge as-is — not a "data failed" error).
const syncs: SyncRegistration[] = []; // external server syncs, throttled
const streamRefreshers: Refresher[] = []; // live-stream reconnectors, not throttled
const lastRunAt = new Map<string, number>(); // label -> last attempt epoch ms (throttle bookkeeping)

/**
 * Register an external server sync. Idempotent on `label` so a re-import (HMR) or a remount
 * replaces the entry rather than duplicating it. minIntervalMs throttles rapid refresh clicks
 * so they do not hammer the rate-limited upstream API (Garmin / Google Calendar / Benzinga).
 */
export function registerSync(label: string, run: Refresher, minIntervalMs = 0): void {
  const reg: SyncRegistration = { label, run, minIntervalMs };
  const existing = syncs.findIndex((s) => s.label === label);
  if (existing >= 0) syncs[existing] = reg; // replace (no duplicate on re-register)
  else syncs.push(reg);
}

/**
 * Register a live-stream refresher (e.g. an SSE reconnect). Returns an unregister function
 * because the SSE instance is created inside a React effect and must be unregistered on
 * unmount, unlike the module-level syncs which live for the app's lifetime.
 */
export function registerStreamRefresher(run: Refresher): () => void {
  streamRefreshers.push(run);
  return () => {
    const i = streamRefreshers.indexOf(run);
    if (i >= 0) streamRefreshers.splice(i, 1);
  };
}

// What runAllRefreshers reports back so the caller can surface failures instead of swallowing them.
export interface RefreshOutcome {
  ranCount: number; // how many syncs actually fired (not throttle-skipped)
  skippedCount: number; // how many syncs were throttle-skipped this run
  failures: string[]; // labels of syncs that rejected
}

/**
 * Fire every registered refresher. External syncs that ran within their throttle window are
 * skipped; the rest fire concurrently alongside every stream refresher. Settles all of them
 * (one failure never aborts the others) and returns which syncs failed. The caller is expected
 * to follow this with a React Query invalidation to refetch every query.
 *
 * `now` is injectable purely so the throttle is testable without faking the clock globally.
 */
export async function runAllRefreshers(now: number = Date.now()): Promise<RefreshOutcome> {
  const due = syncs.filter((s) => {
    const last = lastRunAt.get(s.label);
    return last === undefined || now - last >= s.minIntervalMs; // never-run is always due
  });
  // Stamp BEFORE awaiting so a second click during the in-flight sync is throttled, not a re-fire.
  due.forEach((s) => lastRunAt.set(s.label, now));

  const results = await Promise.allSettled([
    ...due.map((s) => Promise.resolve(s.run())), // syncs first, so result[i] maps to due[i]
    ...streamRefreshers.map((r) => Promise.resolve(r())),
  ]);

  // Only the first due.length results are syncs (the rest are stream refreshers, whose failures
  // are non-fatal). Report the labels of the syncs that rejected.
  const failures = due
    .filter((_, i) => results[i].status === "rejected")
    .map((s) => s.label);

  return { ranCount: due.length, skippedCount: syncs.length - due.length, failures };
}

// Test-only reset so each Vitest case starts from an empty registry (no cross-test leakage).
export function __resetRefreshRegistryForTests(): void {
  syncs.length = 0;
  streamRefreshers.length = 0;
  lastRunAt.clear();
}
