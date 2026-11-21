import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ACCENT, PAGE_LIST, PAL, SETTINGS_DEFAULT, SettingsProvider, accentOf, appAccent, loadSettings, pageKey, saveSettings, useSettings,
} from "../settings";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("settings", () => {
  it("has the mockup defaults with empty storage", () => {
    const r = loadSettings();
    expect(r.storageError).toBeNull();
    expect(r.settings).toEqual({
      tone: "vivid", rail: "auto", layout: "auto", motion: "on", bg: "aurora", btn: "glass", aur: "still", accent: {},
    });
    expect(SETTINGS_DEFAULT.bg).toBe("aurora");
    expect(SETTINGS_DEFAULT.btn).toBe("glass");
  });

  it("gives each page its set colour and the viewer's pick over it", () => {
    expect(accentOf("home", SETTINGS_DEFAULT)).toEqual({ name: "Periwinkle", hex: "#7aa2ff", rgb: "122 162 255" });
    expect(accentOf("runs", SETTINGS_DEFAULT).name).toBe("Blue");
    const own = { ...SETTINGS_DEFAULT, accent: { runs: 8 } };
    expect(accentOf("runs", own).name).toBe("Cyan");
    expect(appAccent("trading", SETTINGS_DEFAULT)).toBe(accentOf("paper", SETTINGS_DEFAULT).hex);
  });

  it("names a colour for every page and offers no green, amber or red", () => {
    for (const p of PAGE_LIST) expect(ACCENT[p.key], p.key).toBeGreaterThanOrEqual(0);
    expect(PAL).toHaveLength(10);
    expect(PAL.map((c) => c[0])).not.toEqual(expect.arrayContaining(["Green", "Amber", "Red"]));
  });

  it("maps a route to its page key", () => {
    expect(pageKey("research", "chains")).toBe("chain");
    expect(pageKey("research", "overview")).toBe("overview2");
    expect(pageKey("research", "data-library")).toBe("markets");
    expect(pageKey("home", "overview")).toBe("home");
    expect(pageKey("settings", undefined)).toBe("settings");
  });

  it("round-trips a save", () => {
    const s = { ...SETTINGS_DEFAULT, tone: "calm" as const, bg: "glass" as const, accent: { sports: 9 } };
    expect(saveSettings(s)).toBeNull();
    expect(loadSettings().settings).toEqual(s);
  });

  it("reports a storage read failure in words", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const r = loadSettings();
    expect(r.settings).toEqual(SETTINGS_DEFAULT);
    expect(r.storageError).toContain("Settings could not be read in this browser");
    expect(r.storageError).toContain("blocked");
  });

  it("reports an unknown page colour and keeps the set colour", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ accent: { runs: 99, nowhere: 1 } }));
    const r = loadSettings();
    expect(accentOf("runs", r.settings).name).toBe("Blue");
    expect(r.storageError).toContain("runs: 99");
    expect(r.storageError).toContain("nowhere: 1");
  });

  it("an unknown background or button falls back to the default", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ bg: "dither", btn: "amber" }));
    const r = loadSettings();
    expect(r.settings.bg).toBe("aurora");
    expect(r.settings.btn).toBe("glass");
  });

  it("reports a storage write failure", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
    const e = saveSettings(SETTINGS_DEFAULT);
    expect(e).toContain("saved");
    expect(e).toContain("full");
  });

  it("provider holds settings and the hook throws outside it", () => {
    function Probe() {
      const { settings, storageError, setSettings } = useSettings();
      return (
        <button onClick={() => setSettings({ ...settings, tone: "calm" })}>{settings.tone}|{storageError ?? "ok"}</button>
      );
    }
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Probe />)).toThrow("useSettings outside SettingsProvider");
    render(<SettingsProvider><Probe /></SettingsProvider>);
    const b = screen.getByRole("button");
    expect(b.textContent).toBe("vivid|ok");
    act(() => b.click());
    expect(b.textContent).toBe("calm|ok");
    expect(loadSettings().settings.tone).toBe("calm");
  });
});
