import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  registerSync,
  registerStreamRefresher,
  runAllRefreshers,
  __resetRefreshRegistryForTests,
} from "./refresh-registry";

describe("refresh-registry (item 7 / D13)", () => {
  beforeEach(() => {
    __resetRefreshRegistryForTests();
  });

  it("runs every registered sync and stream refresher", async () => {
    const garmin = vi.fn().mockResolvedValue(undefined);
    const calendar = vi.fn().mockResolvedValue(undefined);
    const reconnect = vi.fn();
    registerSync("Garmin", garmin);
    registerSync("Calendar", calendar);
    registerStreamRefresher(reconnect);

    const outcome = await runAllRefreshers(1000);

    expect(garmin).toHaveBeenCalledTimes(1);
    expect(calendar).toHaveBeenCalledTimes(1);
    expect(reconnect).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ ranCount: 2, skippedCount: 0, failures: [] });
  });

  it("throttles a sync re-run inside its min interval, and runs it again after", async () => {
    const garmin = vi.fn().mockResolvedValue(undefined);
    registerSync("Garmin", garmin, 15_000);

    await runAllRefreshers(0); // first run fires
    const second = await runAllRefreshers(10_000); // 10s later, inside the 15s window: skipped
    expect(garmin).toHaveBeenCalledTimes(1);
    expect(second.ranCount).toBe(0);
    expect(second.skippedCount).toBe(1);

    const third = await runAllRefreshers(20_000); // 20s later, past the window: fires again
    expect(garmin).toHaveBeenCalledTimes(2);
    expect(third.ranCount).toBe(1);
  });

  it("reports a failing sync without aborting the others (failures are not swallowed)", async () => {
    const ok = vi.fn().mockResolvedValue(undefined);
    const bad = vi.fn().mockRejectedValue(new Error("upstream 503"));
    registerSync("Calendar", calendarOk(ok));
    registerSync("Analyst ratings", bad);

    const outcome = await runAllRefreshers(1000);

    expect(ok).toHaveBeenCalledTimes(1); // the healthy sync still ran
    expect(outcome.failures).toEqual(["Analyst ratings"]);
    expect(outcome.ranCount).toBe(2);
  });

  it("registerSync is idempotent on label (no duplicate on re-register)", async () => {
    const first = vi.fn().mockResolvedValue(undefined);
    const second = vi.fn().mockResolvedValue(undefined);
    registerSync("Garmin", first);
    registerSync("Garmin", second); // same label: replaces, does not add

    await runAllRefreshers(1000);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});

// Tiny helper so the failing-sync test reads clearly.
function calendarOk(spy: () => void) {
  return async () => {
    spy();
  };
}
