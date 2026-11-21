import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// @ts-ignore node:fs has no type declarations here
import * as nodeFs from "node:fs";
import App from "../../App";
import { ApiError, apiGet } from "../../lib/api";
import SectionStub from "../pages/SectionStub";
import { APPS } from "../shell/sections";

// The project has no Node type package, so node:fs is used through a typed local view
// (tests run from dashboard/frontend).
interface Fs {
  existsSync: (p: string) => boolean;
  readdirSync: (p: string) => string[];
  statSync: (p: string) => { isDirectory: () => boolean };
  readFileSync: (p: string, enc?: string) => any;
}
const fs = nodeFs as unknown as Fs;
const EM_DASH = String.fromCharCode(8212);

function walkFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of fs.readdirSync(dir)) {
    const p = `${dir}/${name}`;
    if (fs.statSync(p).isDirectory()) out.push(...walkFiles(p));
    else out.push(p);
  }
  return out;
}

const isTest = (p: string) => /\.test\.tsx?$/.test(p);
const isSource = (p: string) => /\.tsx?$/.test(p) && !/\.d\.ts$/.test(p);

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

function mount(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <App />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: (() => void) | null = null;
  addEventListener() {}
  removeEventListener() {}
  close() {}
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ({ fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() })) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("only the new pages stay", () => {
  it("(a) has no src/routes folder", () => {
    expect(fs.existsSync("src/routes")).toBe(false);
  });

  it.each(["/", "/research/judge"])("(a) %s lands on /app/home/overview", async (path) => {
    mount(path);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/app/home/overview"));
  });

  it("(b) the import walk from main, setup and the v2 tests reaches every source file", () => {
    const all = walkFiles("src");
    expect(all.filter((p) => p.startsWith("src/routes/"))).toEqual([]);
    const exists = (p: string) => (fs.existsSync(p) && !fs.statSync(p).isDirectory() ? p : null);
    const resolve = (from: string, spec: string): string | null => {
      const dir = from.slice(0, from.lastIndexOf("/"));
      const parts = `${dir}/${spec}`.split("/");
      const norm: string[] = [];
      for (const s of parts) {
        if (s === "..") norm.pop();
        else if (s !== ".") norm.push(s);
      }
      const base = norm.join("/");
      return exists(base) ?? exists(`${base}.ts`) ?? exists(`${base}.tsx`) ?? exists(`${base}/index.ts`) ?? exists(`${base}/index.tsx`);
    };
    const roots = ["src/main.tsx", "src/test/setup.ts", ...all.filter((p) => p.startsWith("src/v2/") && isTest(p))];
    const seen = new Set<string>();
    const queue = [...roots];
    const unresolved: string[] = [];
    while (queue.length > 0) {
      const file = queue.pop() as string;
      if (seen.has(file)) continue;
      seen.add(file);
      const text: string = fs.readFileSync(file, "utf-8");
      const re = /(?:import|export)\s[^;]*?from\s*["'](\.[^"']*)["']|import\s*\(\s*["'](\.[^"']*)["']\s*\)|import\s*["'](\.[^"']*)["']/g;
      for (const m of text.matchAll(re)) {
        const spec = m[1] ?? m[2] ?? m[3];
        if (/\.(css|svg|png)$/.test(spec)) continue;
        const target = resolve(file, spec);
        if (target === null) unresolved.push(`${file} -> ${spec}`);
        else queue.push(target);
      }
    }
    expect(unresolved).toEqual([]);
    const unreached = all.filter((p) => isSource(p) && !isTest(p) && !seen.has(p));
    expect(unreached).toEqual([]);
  });

  it("(c) a failed get throws a plain ApiError with the url kept apart", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 500, statusText: "Server Error", json: async () => ({}) }),
    );
    const err = await apiGet("/api/x").then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.message).toBe("500 Server Error");
    expect(apiErr.message).not.toContain(EM_DASH);
    expect(apiErr.message).not.toContain("/api");
    expect(apiErr.url).toBe("/api/x");
  });

  it("(d) sections.ts carries no old-page field and a stub shows no link", () => {
    expect(fs.readFileSync("src/v2/shell/sections.ts", "utf-8")).not.toContain("oldRoute");
    const app = APPS.find((a) => a.id === "research");
    const section = app?.sections.find((s) => s.id === "operations");
    if (!app || !section) throw new Error("research/operations is not in sections.ts");
    render(
      <MemoryRouter>
        <SectionStub app={app} section={section} />
      </MemoryRouter>,
    );
    expect(screen.getByText("This page is not rebuilt yet.")).toBeInTheDocument();
    expect(document.querySelector("a")).toBeNull();
  });

  it("(e) the Reactor icon is wired in", () => {
    const html: string = fs.readFileSync("index.html", "utf-8");
    expect(html).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />');
    expect(html).not.toContain("data:image");
    const manifest = JSON.parse(fs.readFileSync("public/manifest.json", "utf-8"));
    expect(manifest.start_url).toBe("/app/home/overview");
    // Without a scope the installed app's scope is start_url's folder, so every page outside /app/home/ shows a URL bar.
    expect(manifest.scope).toBe("/");
    expect(manifest.id).toBe("/app/home/overview");
    // Standalone is the one display every target opens the same way, and the APP panel detects it.
    expect(manifest.display).toBe("standalone");
    // The retired service worker: nothing registers it, it passes no request through, it removes itself.
    expect(html).not.toContain("serviceWorker");
    const sw: string = fs.readFileSync("public/sw.js", "utf-8");
    expect(sw).not.toContain("'fetch'");
    expect(sw).toContain("self.registration.unregister()");
    // iOS paints see-through pixels black, so the iPhone gets its own opaque tile; Windows and Android read the
    // manifest's see-through icons and show just the logo (Tamer 2026-11-16).
    expect(html).toContain('<link rel="apple-touch-icon" href="/apple-touch-icon.png" />');
    const same = (a: string, b: string) => fs.readFileSync(a).equals(fs.readFileSync(b));
    expect(same("public/favicon.svg", "../../docs/design/logo/reactor_fire_and_ice.svg")).toBe(true);
    expect(same("public/icon-192.png", "../../docs/design/logo/icon-192.png")).toBe(true);
    expect(same("public/icon-512.png", "../../docs/design/logo/icon-512.png")).toBe(true);
    expect(same("public/apple-touch-icon.png", "../../docs/design/logo/apple-touch-icon.png")).toBe(true);
    // A PNG's colour type byte: 6 carries a see-through channel, 2 has none.
    const colourType = (f: string) => fs.readFileSync(f)[25];
    expect(colourType("public/icon-512.png")).toBe(6);
    expect(colourType("public/apple-touch-icon.png")).toBe(2);
  });
});
