import { describe, it, expect } from "vitest";

import { settingLabel, formatTestWindow, plainStrategyName, NO_DATA } from "./format";

// Improvements brief §2.4a / §2.5: the lab-settings scope.key rows render a plain human label.
describe("settingLabel", () => {
  it("maps known scope.key pairs to faithful plain labels", () => {
    expect(settingLabel("fees.equities", "default")).toBe("Equity trading fee");
    expect(settingLabel("slippage.crypto", "default")).toBe("Crypto slippage");
    expect(settingLabel("event_strategy", "profit_take_mult")).toBe("Profit-take multiple");
    expect(settingLabel("event_strategy", "vol_window")).toBe("Volatility lookback (days)");
    expect(settingLabel("paths", "data_root")).toBe("Data root path");
  });

  it("humanizes an unmapped key from its leaf token, never returning the raw scope.key", () => {
    // A new, unlisted key still reads human (the leaf token, title-cased) rather than "event_strategy.new_dial".
    expect(settingLabel("event_strategy", "new_dial")).toBe("New dial");
    expect(settingLabel("some_scope", "a_brand_new_key")).toBe("A brand new key");
  });

  it("falls back to the combined key only when the leaf token is empty", () => {
    expect(settingLabel("fees", "")).toBe("fees.");
  });
});

// formatTestWindow (lane DX round 5, SF1): the saved test window as plain English. Tracker.request_draw
// refuses any draw whose window carries ANY NULL bound (R-T15), so one-sided NULL cases must display
// refusal just as the both-NULL case does.
describe("formatTestWindow", () => {
  it("displays a normal window when both bounds are set", () => {
    const start = Math.floor(new Date("2020-01-01").getTime() / 1000);
    const end = Math.floor(new Date("2025-12-31").getTime() / 1000);
    expect(formatTestWindow(start, end)).toBe("2020 to 2025");
  });

  it("displays a refusal message when both bounds are NULL", () => {
    expect(formatTestWindow(null, null)).toBe("no window set, refused until set");
    expect(formatTestWindow(undefined, undefined)).toBe("no window set, refused until set");
  });

  it("displays a refusal message when start is NULL only", () => {
    const end = Math.floor(new Date("2025-12-31").getTime() / 1000);
    const result = formatTestWindow(null, end);
    expect(result).toContain("refused when used");
    expect(result).toContain("missing start");
    expect(result).toContain("2025");
  });

  it("displays a refusal message when end is NULL only", () => {
    const start = Math.floor(new Date("2020-01-01").getTime() / 1000);
    const result = formatTestWindow(start, null);
    expect(result).toContain("refused when used");
    expect(result).toContain("missing end");
    expect(result).toContain("2020");
  });
});

describe("plainStrategyName", () => {
  it("turns underscores into spaces and capitalises the first letter only", () => {
    expect(plainStrategyName("strategy_runs_ma_cross")).toBe("Strategy runs ma cross");
    expect(plainStrategyName("theory__capitulation_rebound__v0")).toBe("Theory capitulation rebound v0");
    expect(plainStrategyName("Breakout drift")).toBe("Breakout drift");
  });
  it("gives NO_DATA for null, undefined or empty", () => {
    expect(plainStrategyName(null)).toBe(NO_DATA);
    expect(plainStrategyName("")).toBe(NO_DATA);
  });
});
