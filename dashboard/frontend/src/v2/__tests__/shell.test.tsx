import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";
import V2App from "../shell/V2App";
import { APPS } from "../shell/sections";

const fs = nodeFs as unknown as { readFileSync: (p: string, enc: string) => string };
const ctx = { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <V2App />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// jsdom has no layout: give the rail's page buttons the offsets the highlight reads.
function spyOffsets() {
  const labelOf = (el: HTMLElement) => el.getAttribute("aria-label");
  const isSec = (el: HTMLElement) => el.classList.contains("sec");
  vi.spyOn(HTMLElement.prototype, "offsetTop", "get").mockImplementation(function (this: HTMLElement) {
    return labelOf(this) === "Overview" ? 40 : labelOf(this) === "Runs" ? 120 : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) { return isSec(this) ? 7 : 0; });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) { return isSec(this) ? 42 : 0; });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) { return isSec(this) ? 36 : 0; });
}

const rail = () => document.querySelector(".rail") as HTMLElement;
const where = () => screen.getByTestId("where").textContent;

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete (document as unknown as Record<string, unknown>).startViewTransition;
});

describe("V2 shell", () => {
  it("/app lands on /app/home/overview with the title Overview", () => {
    mount("/app");
    expect(where()).toBe("/app/home/overview");
    expect(screen.getByRole("heading", { name: "Home" })).toBeTruthy();
  });

  it("the rail shows five apps and the gear", () => {
    mount("/app/home/overview");
    for (const n of ["Home", "Research", "Calendar", "Trading", "Portfolio", "Settings"]) {
      expect(within(rail()).getByRole("button", { name: n })).toBeTruthy();
    }
  });

  it("an app click shows its sections and a section click navigates", () => {
    mount("/app/home/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Research" }));
    expect(where()).toBe("/app/research/overview");
    for (const s of ["Overview", "Data library", "Strategies", "Runs", "Judge", "Forward testing", "Operations"]) {
      expect(within(rail()).getByRole("button", { name: s })).toBeTruthy();
    }
    fireEvent.click(within(rail()).getByRole("button", { name: "Data library" }));
    expect(where()).toBe("/app/research/data-library");
  });

  it("the gear opens /app/settings", () => {
    mount("/app/home/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Settings" }));
    expect(where()).toBe("/app/settings");
  });

  it("the rail starts folded, a press on it does not open it, its toggle does, a page click folds it", () => {
    mount("/app/home/overview");
    expect(rail().classList.contains("mini")).toBe(true);
    fireEvent.pointerDown(rail());
    expect(rail().classList.contains("mini")).toBe(true);
    fireEvent.click(rail().querySelector(".tog") as HTMLElement);
    expect(rail().classList.contains("mini")).toBe(false);
    fireEvent.click(document.querySelector("main") as HTMLElement);
    expect(rail().classList.contains("mini")).toBe(true);
  });

  it("a page icon on the folded rail changes page without opening the rail; on the open rail it folds it", () => {
    mount("/app/research/overview");
    const runs = within(rail()).getByRole("button", { name: "Runs" });
    fireEvent.pointerDown(runs);
    fireEvent.click(runs);
    expect(where()).toBe("/app/research/runs");
    expect(rail().classList.contains("mini")).toBe(true);
    fireEvent.click(rail().querySelector(".tog") as HTMLElement);
    fireEvent.click(within(rail()).getByRole("button", { name: "Judge" }));
    expect(where()).toBe("/app/research/judge");
    expect(rail().classList.contains("mini")).toBe(true);
  });

  it("Escape folds the open rail", () => {
    mount("/app/home/overview");
    fireEvent.click(rail().querySelector(".tog") as HTMLElement);
    expect(rail().classList.contains("mini")).toBe(false);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(rail().classList.contains("mini")).toBe(true);
  });

  it("with the rail setting open it never folds", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ rail: "open" }));
    mount("/app/home/overview");
    expect(rail().classList.contains("mini")).toBe(false);
    fireEvent.click(document.querySelector("main") as HTMLElement);
    expect(rail().classList.contains("mini")).toBe(false);
  });

  it("the root has railopen while the rail is unfolded and not after a page click", () => {
    mount("/app/home/overview");
    const root = () => document.querySelector(".v2") as HTMLElement;
    expect(root().classList.contains("railopen")).toBe(false);
    fireEvent.click(rail().querySelector(".tog") as HTMLElement);
    expect(root().classList.contains("railopen")).toBe(true);
    fireEvent.click(document.querySelector("main") as HTMLElement);
    expect(root().classList.contains("railopen")).toBe(false);
  });

  it("with the rail setting open the root has railopen from the first render", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ rail: "open" }));
    mount("/app/home/overview");
    expect((document.querySelector(".v2") as HTMLElement).classList.contains("railopen")).toBe(true);
  });

  it("the css lays the open rail over the page, moves the page only when pinned, and markets.css gives a pinned rail 2 wall columns", () => {
    const shell = fs.readFileSync("src/v2/shell/shell.css", "utf-8");
    const mk = fs.readFileSync("src/v2/pages/research/markets.css", "utf-8");
    expect(shell).not.toMatch(/\.v2\.railopen main\s*\{/);
    expect(shell).toMatch(/\.v2 main\s*\{[^}]*margin-left:\s*var\(--rail\)/);
    expect(shell).toMatch(/\.v2\.pinned main\s*\{[^}]*margin-left:\s*calc\(var\(--rail\) \+ 196px\)/);
    // The mockup switches on the window width; a pinned rail narrows the page, so a container rule backs it up.
    expect(mk).toMatch(/@media \(max-width: 1180px\) \{ \.v2 \.mk \.mw-wall \{ grid-template-columns: repeat\(2/);
    expect(mk).toMatch(/@container \(max-width: 1008px\) \{ \.v2 \.mk \.mw-wall \{ grid-template-columns: repeat\(2/);
  });

  it("the folded rail keeps the open app's pages as icons, each with its name on hover", () => {
    mount("/app/research/overview");
    expect(rail().classList.contains("mini")).toBe(true);
    for (const s of ["Overview", "Data library", "Judge", "Operations"]) expect(within(rail()).getByRole("button", { name: s })).toBeTruthy();
    const shell = fs.readFileSync("src/v2/shell/shell.css", "utf-8");
    expect(shell).not.toMatch(/\.rail\.mini \.secs\s*\{[^}]*display:\s*none/);
    // Folded, each page and app carries its name as a hover text (the hover box shows it); open, the names are written.
    expect(within(rail()).getByRole("button", { name: "Judge" }).getAttribute("title")).toBe("Judge");
    expect(within(rail()).getByRole("button", { name: "Research" }).getAttribute("title")).toBe("Research");
    fireEvent.click(within(rail()).getByRole("button", { name: "Open the side bar" }));
    expect(within(rail()).getByRole("button", { name: "Judge" }).getAttribute("title")).toBeNull();
  });

  it("an unknown page says so", () => {
    mount("/app/x/y");
    expect(screen.getByText("This page does not exist.")).toBeTruthy();
  });

  it("a section click changes page without a view transition", () => {
    const vt = vi.fn((cb: () => void) => { cb(); return { finished: Promise.resolve() }; });
    (document as unknown as Record<string, unknown>).startViewTransition = vt;
    mount("/app/research/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Runs" }));
    expect(vt).not.toHaveBeenCalled();
    expect(where()).toBe("/app/research/runs");
  });

  it("s1 the rail has one highlight on the open page that slides to the next", () => {
    spyOffsets();
    mount("/app/research/overview");
    const marks = document.querySelectorAll(".secs > .mark");
    expect(marks.length).toBe(1);
    expect(document.querySelectorAll(".sec .mark").length).toBe(0);
    let mk = marks[0] as HTMLElement;
    expect(mk.style.transform).toBe("translate(7px, 40px)");
    expect(mk.style.width).toBe("42px");
    expect(mk.style.height).toBe("36px");
    expect(mk.classList.contains("now")).toBe(true);
    fireEvent.click(within(rail()).getByRole("button", { name: "Runs" }));
    mk = document.querySelector(".secs > .mark") as HTMLElement;
    expect(mk.style.transform).toBe("translate(7px, 120px)");
    expect(mk.classList.contains("now")).toBe(false);
  });

  it("s2 another app's group places its highlight at once with no view transition", () => {
    spyOffsets();
    const vt = vi.fn((cb: () => void) => { cb(); return { finished: Promise.resolve() }; });
    (document as unknown as Record<string, unknown>).startViewTransition = vt;
    mount("/app/research/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Portfolio" }));
    const mk = document.querySelector(".secs > .mark") as HTMLElement;
    expect(mk).toBeTruthy();
    expect(mk.classList.contains("now")).toBe(true);
    expect(vt).not.toHaveBeenCalled();
  });

  it("never uses the view transition when motion is off", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ motion: "off" }));
    const vt = vi.fn((cb: () => void) => { cb(); return { finished: Promise.resolve() }; });
    (document as unknown as Record<string, unknown>).startViewTransition = vt;
    mount("/app/research/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Runs" }));
    expect(vt).not.toHaveBeenCalled();
    expect(where()).toBe("/app/research/runs");
  });

  it("still navigates when the browser has no view transition", () => {
    mount("/app/research/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Judge" }));
    expect(where()).toBe("/app/research/judge");
  });

  it("with the rail open every rail button shows its name as text", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ rail: "open" }));
    mount("/app/home/overview");
    for (const n of ["Home", "Research", "Calendar", "Trading", "Portfolio", "Settings"]) {
      expect(within(rail()).getByText(n)).toBeTruthy();
    }
  });

  it("Calendar has one section so it shows no section list, and its button opens its page", () => {
    mount("/app/calendar/calendar");
    expect(within(rail()).getAllByRole("button", { name: "Calendar" })).toHaveLength(1);
    fireEvent.click(within(rail()).getByRole("button", { name: "Home" }));
    expect(where()).toBe("/app/home/overview");
    fireEvent.click(within(rail()).getByRole("button", { name: "Calendar" }));
    expect(where()).toBe("/app/calendar/calendar");
  });

  it("the css fades app names in on the open rail only, by opacity and transform; hover names use the hover box", () => {
    const shell = fs.readFileSync("src/v2/shell/shell.css", "utf-8");
    expect(shell).toMatch(/\.v2 \.appname, \.v2 \.sec \.sname \{[^}]*opacity:\s*0/);
    expect(shell).toMatch(/\.rail:not\(\.mini\) \.appname[^{]*\{[^}]*opacity:\s*1/);
    expect(shell).not.toMatch(/\.appname[^{]*\{[^}]*display:\s*none/);
    // The hover name is the shared hover box (HoverTip), not a label drawn in the rail's css.
    expect(shell).not.toMatch(/\.app\[title\]:hover::after/);
    expect(shell).toMatch(/\.v2 \.htip-in\s*\{[^}]*background: #141a23/);
  });
});

describe("V2 shell, mockup shell", () => {
  const btn = (name: string) => screen.queryByRole("button", { name });

  it("(a) page arrows show the neighbours of the open section", () => {
    mount("/app/research/overview");
    expect(btn("Next page: Data library")).toBeTruthy();
    expect(btn("Previous page: Operations")).toBeTruthy();
    fireEvent.click(btn("Next page: Data library") as HTMLElement);
    expect(where()).toBe("/app/research/data-library");
    expect(btn("Previous page: Overview")).toBeTruthy();
    expect(btn("Next page: Strategies")).toBeTruthy();
    cleanup();
    mount("/app/research/operations");
    expect(btn("Previous page: Forward testing")).toBeTruthy();
    expect(btn("Next page: Overview")).toBeTruthy();
    cleanup();
    mount("/app/calendar/calendar");
    expect(screen.queryByRole("button", { name: /^(Previous|Next) page/ })).toBeNull();
  });

  it("(b) left and right arrow keys step pages, unless typing, a modifier or a dialog", () => {
    mount("/app/research/overview");
    fireEvent.keyDown(document, { key: "ArrowRight" });
    expect(where()).toBe("/app/research/data-library");
    fireEvent.keyDown(document, { key: "ArrowLeft" });
    expect(where()).toBe("/app/research/overview");
    const input = document.createElement("input");
    document.body.appendChild(input);
    fireEvent.keyDown(input, { key: "ArrowRight" });
    expect(where()).toBe("/app/research/overview");
    input.remove();
    fireEvent.keyDown(document, { key: "ArrowRight", ctrlKey: true });
    expect(where()).toBe("/app/research/overview");
    const dlg = document.createElement("div");
    dlg.setAttribute("role", "dialog");
    document.body.appendChild(dlg);
    fireEvent.keyDown(document, { key: "ArrowRight" });
    expect(where()).toBe("/app/research/overview");
    dlg.remove();
  });

  it("(c) pressing an app button on the folded rail does not open the rail", () => {
    mount("/app/research/overview");
    const home = within(rail()).getByRole("button", { name: "Home" });
    fireEvent.pointerDown(home);
    fireEvent.click(home);
    expect(where()).toBe("/app/home/overview");
    expect(rail().classList.contains("mini")).toBe(true);
  });

  it("(d) the panel button is first, toggles the rail, and is absent when the rail is open", () => {
    mount("/app/home/overview");
    const first = rail().querySelector("button") as HTMLElement;
    expect(first.getAttribute("aria-label")).toBe("Open the side bar");
    expect(first.title).toBe("Open the side bar");
    fireEvent.click(first);
    expect(rail().classList.contains("mini")).toBe(false);
    expect(first.getAttribute("aria-label")).toBe("Close the side bar");
    fireEvent.click(first);
    expect(rail().classList.contains("mini")).toBe(true);
    cleanup();
    localStorage.setItem("v2-settings", JSON.stringify({ rail: "open" }));
    mount("/app/home/overview");
    expect(document.querySelector(".tog")).toBeNull();
  });

  it("(e) the root carries the mockup defaults and the open page's aurora and colour", () => {
    mount("/app/research/chains");
    const root = document.querySelector(".v2") as HTMLElement;
    expect(root.dataset.bg).toBe("aurora");
    expect(root.dataset.btn).toBe("glass");
    expect(root.dataset.aur).toBe("still");
    expect(root.dataset.motion).toBe("full");
    expect(root.dataset.page).toBe("chain");
    expect(root.style.getPropertyValue("--b")).toBe("#848cfa");
    expect(root.querySelectorAll(".aur > i")).toHaveLength(3);
  });

  it("(e2) a saved look and page colour reach the root", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ bg: "glass", btn: "white", aur: "move", motion: "off", accent: { chain: 8 } }));
    mount("/app/research/chains");
    const root = document.querySelector(".v2") as HTMLElement;
    expect(root.dataset.bg).toBe("glass");
    expect(root.dataset.btn).toBe("white");
    expect(root.dataset.aur).toBe("move");
    expect(root.dataset.motion).toBe("calm");
    expect(root.style.getPropertyValue("--b")).toBe("#28c8eb");
    expect(root.querySelector(".aur")).toBeNull();
  });

  it("(f) the css has round six's closed rail width, bars, slide, short-screen and small-screen rules", () => {
    const css = fs.readFileSync("src/v2/shell/shell.css", "utf-8");
    expect(css).toMatch(/\.v2 \.rail\s*\{[^}]*width:\s*var\(--rail\)/);
    expect(css).toMatch(/\.secs \.mark\s*\{[^}]*position:\s*absolute[^}]*transition:\s*transform/);
    expect(css).toMatch(/\.v2\.still \.secs \.mark[^{]*\{[^}]*transition:\s*none/);
    expect(css).not.toMatch(/\.sec \.mark/);
    expect(css).not.toMatch(/\.sec\.on\s*\{[^}]*background/);
    expect(css).not.toMatch(/v2-pgout/);
    // The mockup marks only the open page, at the rail's edge; a short screen packs the rows; Research's groups get a line.
    expect(css).not.toMatch(/\.app::before/);
    expect(css).toMatch(/@media \(max-height: 900px\)[\s\S]*\.v2 \.sec \{ height: 31px; \}/);
    expect(css).toMatch(/\.v2 \.rsec::before\s*\{[^}]*background: var\(--line3\)/);
    expect(css).toMatch(/html\[data-dir=next\]::view-transition/);
    expect(css).toMatch(/html\[data-dir=prev\]::view-transition/);
    expect(css).toMatch(/@media \(max-width:\s*700px\)[\s\S]*--rail:\s*64px/);
  });

  it("(g) an arrow click sets data-dir before the change and clears it after finished", async () => {
    let seen: string | undefined;
    let resolve!: () => void;
    const finished = new Promise<void>((r) => { resolve = r; });
    const vt = vi.fn((cb: () => void) => { seen = document.documentElement.dataset.dir; cb(); return { finished }; });
    (document as unknown as Record<string, unknown>).startViewTransition = vt;
    mount("/app/research/overview");
    fireEvent.click(screen.getByRole("button", { name: "Next page: Data library" }));
    expect(seen).toBe("next");
    expect(document.documentElement.dataset.dir).toBe("next");
    resolve();
    await waitFor(() => expect(document.documentElement.dataset.dir).toBeUndefined());
  });

  it("(g) with motion off nothing is set", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ motion: "off" }));
    const vt = vi.fn((cb: () => void) => { cb(); return { finished: Promise.resolve() }; });
    (document as unknown as Record<string, unknown>).startViewTransition = vt;
    mount("/app/research/overview");
    fireEvent.click(screen.getByRole("button", { name: "Next page: Data library" }));
    expect(vt).not.toHaveBeenCalled();
    expect(document.documentElement.dataset.dir).toBeUndefined();
    expect(where()).toBe("/app/research/data-library");
  });

  it("(h) Positions is a page of its own, no longer a stub, with a previous arrow", () => {
    mount("/app/portfolio/positions");
    expect(screen.getByRole("heading", { name: "Positions" })).toBeTruthy();
    expect(screen.queryByText("This page is not rebuilt yet.")).toBeNull();
    expect(screen.getByRole("button", { name: "Previous page: Budget" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Next page: Money" })).toBeTruthy();
  });

  it("(i) trading's sections are Paper and Capital, and Capital has its title", () => {
    expect(APPS.find((a) => a.id === "trading")!.sections.map((s) => s.label)).toEqual(["Paper", "Capital"]);
    mount("/app/trading/capital");
    expect(screen.getByRole("heading", { name: "Capital" })).toBeTruthy();
  });
});

describe("V2 shell, arrows loop", () => {
  const press = (key: string, init: Record<string, unknown> = {}) => fireEvent.keyDown(document, { key, ...init });

  it("(a) ArrowDown and ArrowUp move between the mains and loop", () => {
    mount("/app/research/judge");
    press("ArrowDown");
    expect(where()).toBe("/app/calendar/calendar");
    press("ArrowDown");
    expect(where()).toBe("/app/trading/paper");
    cleanup();
    mount("/app/home/overview");
    press("ArrowUp");
    expect(where()).toBe("/app/portfolio/positions");
    cleanup();
    mount("/app/portfolio/money");
    press("ArrowDown");
    expect(where()).toBe("/app/home/overview");
  });

  it("(b) a main opens on the section last open there, else its first", () => {
    mount("/app/research/judge");
    press("ArrowDown");
    expect(where()).toBe("/app/calendar/calendar");
    press("ArrowUp");
    expect(where()).toBe("/app/research/judge");
  });

  it("(c) left and right loop inside a main and the page arrows name the looped neighbour", () => {
    mount("/app/research/operations");
    expect(screen.getByRole("button", { name: "Next page: Overview" })).toBeTruthy();
    press("ArrowRight");
    expect(where()).toBe("/app/research/overview");
    expect(screen.getByRole("button", { name: "Previous page: Operations" })).toBeTruthy();
    press("ArrowLeft");
    expect(where()).toBe("/app/research/operations");
    cleanup();
    mount("/app/calendar/calendar");
    expect(screen.queryByRole("button", { name: /^(Previous|Next) page/ })).toBeNull();
    press("ArrowLeft");
    press("ArrowRight");
    expect(where()).toBe("/app/calendar/calendar");
  });

  it("(d) up and down are ignored while typing, with a modifier, with a dialog, on Settings and on an unknown path", () => {
    mount("/app/research/judge");
    const input = document.createElement("input");
    document.body.appendChild(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    input.remove();
    press("ArrowDown", { shiftKey: true });
    const dlg = document.createElement("div");
    dlg.setAttribute("role", "dialog");
    document.body.appendChild(dlg);
    press("ArrowDown");
    dlg.remove();
    expect(where()).toBe("/app/research/judge");
    press("ArrowDown");
    expect(where()).toBe("/app/calendar/calendar");
    cleanup();
    for (const path of ["/app/settings", "/app/x/y"]) {
      mount(path);
      press("ArrowDown");
      press("ArrowUp");
      press("ArrowLeft");
      press("ArrowRight");
      expect(where()).toBe(path);
      cleanup();
    }
  });

  it("(e) up and down set data-dir before the change and clear it after", async () => {
    const seen: Array<string | undefined> = [];
    const vt = vi.fn((cb: () => void) => { seen.push(document.documentElement.dataset.dir); cb(); return { finished: Promise.resolve() }; });
    (document as unknown as Record<string, unknown>).startViewTransition = vt;
    mount("/app/research/judge");
    press("ArrowDown");
    press("ArrowUp");
    expect(seen).toEqual(["down", "up"]);
    await waitFor(() => expect(document.documentElement.dataset.dir).toBeUndefined());
  });
});
