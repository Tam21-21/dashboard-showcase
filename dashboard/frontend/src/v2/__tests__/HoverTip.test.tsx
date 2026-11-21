import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HoverTip from "../shell/HoverTip";

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: () => {}, removeEventListener: () => {} })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount() {
  return render(
    <div className="v2">
      <HoverTip />
      <button type="button" title={"Judge: Broken\nA graded run got no verdict"}>Judge</button>
      <span title="Paris time">Today</span>
    </div>,
  );
}

describe("the hover box (the mockup's K.tip)", () => {
  it("shows a title in the box, bold first line, and gives the title back when the pointer leaves", () => {
    const view = mount();
    const b = view.getByRole("button", { name: "Judge" });
    act(() => { fireEvent.pointerOver(b, { clientX: 40, clientY: 40 }); });
    const box = view.container.querySelector(".htip") as HTMLElement;
    expect(box.querySelector("b")?.textContent).toBe("Judge: Broken");
    expect(box.textContent).toContain("A graded run got no verdict");
    expect(b.getAttribute("title")).toBeNull();
    act(() => { fireEvent.pointerOut(b, { relatedTarget: document.body }); });
    expect(view.container.querySelector(".htip")).toBeNull();
    expect(b.getAttribute("title")).toBe("Judge: Broken\nA graded run got no verdict");
  });

  it("writes a one-line title plainly, and moves to the next element's title", () => {
    const view = mount();
    act(() => { fireEvent.pointerOver(view.getByText("Today"), { clientX: 10, clientY: 10 }); });
    const box = view.container.querySelector(".htip") as HTMLElement;
    expect(box.textContent).toBe("Paris time");
    expect(box.querySelector("b")).toBeNull();
    act(() => { fireEvent.pointerOver(view.getByRole("button", { name: "Judge" }), { clientX: 10, clientY: 10 }); });
    expect(view.getByText("Today").getAttribute("title")).toBe("Paris time");
  });

  it("does nothing on a screen without a mouse", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} })));
    const view = mount();
    act(() => { fireEvent.pointerOver(view.getByText("Today"), { clientX: 10, clientY: 10 }); });
    expect(view.container.querySelector(".htip")).toBeNull();
    expect(view.getByText("Today").getAttribute("title")).toBe("Paris time");
  });
});
