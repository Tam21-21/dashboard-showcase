import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Background } from "../Background";
import { SettingsProvider } from "../settings";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("Background", () => {
  it("Aurora draws the three lights and no canvas", () => {
    const { container } = render(<SettingsProvider><Background bg="aurora" /></SettingsProvider>);
    const lights = container.querySelectorAll(".aur > i");
    expect(Array.from(lights).map((i) => i.className)).toEqual(["l1", "l2", "l3"]);
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("the other backgrounds are plain CSS and draw nothing", () => {
    for (const bg of ["glass", "glow", "dots", "charcoal", "black"] as const) {
      const { container, unmount } = render(<SettingsProvider><Background bg={bg} /></SettingsProvider>);
      expect(container.childElementCount).toBe(0);
      unmount();
    }
  });

  it("with no bg given it follows the saved setting", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ bg: "black" }));
    const { container } = render(<SettingsProvider><Background app="research" /></SettingsProvider>);
    expect(container.childElementCount).toBe(0);
  });
});
