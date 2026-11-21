import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Window } from "../kit";

function open(props: Record<string, unknown> = {}) {
  const onClose = vi.fn();
  const view = render(
    <Window label="Test window" size="m" onClose={onClose} {...props}>
      <button>INSIDE</button>
    </Window>,
  );
  return { onClose, view };
}

describe("Window", () => {
  it("w1 renders a dialog named by its label with win and size class", () => {
    open();
    const d = screen.getByRole("dialog", { name: "Test window" });
    expect(d.getAttribute("aria-modal")).toBe("true");
    expect(d.classList.contains("win")).toBe(true);
    expect(d.classList.contains("win-m")).toBe(true);
  });
  it("w2 Escape on the document closes once", () => {
    const { onClose } = open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
  it("w3 a press on the overlay closes, a press inside does not", () => {
    const { onClose, view } = open();
    fireEvent.mouseDown(screen.getByText("INSIDE"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.mouseDown(view.container.querySelector(".ov") as Element);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
  it("w4 backdropCloses false: overlay press does nothing, Escape closes", () => {
    const { onClose, view } = open({ backdropCloses: false });
    fireEvent.mouseDown(view.container.querySelector(".ov") as Element);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
  it("w5 each size gives its class", () => {
    for (const s of ["s", "m", "l", "xl"]) {
      const { view } = open({ size: s });
      expect(view.container.querySelector(`.win.win-${s}`)).not.toBeNull();
      view.unmount();
    }
  });
  it("w7 a title draws the mockup's heading with a Close button; without one there is no heading", () => {
    const { onClose, view } = open({ title: "Research" });
    expect(view.container.querySelector(".win-h h2")?.textContent).toBe("Research");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(open().view.container.querySelector(".win-h")).toBeNull();
  });
  it("w6 escapeCloses false: Escape does nothing", () => {
    const { onClose } = open({ escapeCloses: false });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
  it("w7 className lands on the card beside win", () => {
    open({ className: "pp-dlg" });
    const d = screen.getByRole("dialog");
    expect(d.classList.contains("pp-dlg")).toBe(true);
    expect(d.classList.contains("win")).toBe(true);
  });
});
