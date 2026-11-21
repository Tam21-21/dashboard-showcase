import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Operations from "../pages/research/Operations";
import { APPS } from "../shell/sections";
import { ApiError } from "../../lib/api";

const getSystems = vi.fn();
const getLog = vi.fn();
const getCounters = vi.fn();

vi.mock("../data/opsApi", () => ({
  getSystems: () => getSystems(),
  getLog: () => getLog(),
  getCounters: () => getCounters(),
}));

const app = APPS.find((a) => a.id === "research")!;
const section = app.sections.find((s) => s.id === "operations")!;

// 48 half-hour marks, the last one starting at 2026-10-23T11:30:00Z (13:30 Paris). The last mark
// has its own finding, the others a common one.
function marks(kind: "ok" | "fail" | "none" = "ok") {
  const base = Date.parse("2026-10-23T12:00:00Z") - 48 * 30 * 60 * 1000;
  return Array.from({ length: 48 }, (_, i) => ({
    start: new Date(base + i * 30 * 60 * 1000).toISOString().replace(".000Z", "Z"),
    mark: kind,
    finding: kind === "none" ? null : i === 47 ? "Heartbeat 12 s ago" : "Checked fine",
  }));
}

const system = (name: string, where: string, word: string, tone: string, extra: Record<string, unknown> = {}) => ({
  name, where, word, tone, reason: null, last_proven_at: "2026-10-23T11:30:00Z", marks: marks(), ...extra,
});

const SYSTEMS = {
  systems: [
    system("Research worker", "Laptop, Docker", "WORKING", "good"),
    system("Dashboard", "Server", "IDLE", "quiet"),
    system("Paper broker", "Alpaca", "STALE", "warn", { marks: marks("fail") }),
    system("News feed", "Server", "DOWN", "bad"),
    system("Garmin sync", "Server", "NOT CONNECTED", "off", { reason: "No Garmin login is set up yet", marks: marks("none") }),
    system("Sweeper", "Server", "WORKING", "good"),
  ],
};
const LOG = {
  log: [
    { at: "2026-10-23T11:00:00Z", time: "13:00", system: "Dashboard", sentence: "Came back up", kind: "transition", state: "OPERATIONAL" },
    { at: "2026-10-23T09:00:00Z", time: "11:00", system: "News feed", sentence: "No answer for 3 checks", kind: "transition", state: "DOWN" },
    { at: "2026-10-23T06:00:00Z", time: "08:00", system: "Research worker", sentence: "Finished a sweep", kind: "event", state: null },
  ],
};

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Operations app={app} section={section} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const COUNTERS = {
  read_at: "2026-10-23T11:40:00Z",
  counters: [
    { module: "paperdb", id: "paper_unwritable_order_refusals", value: 0, declared_only: false },
    { module: "judgedb", id: "stale_battery_version_count", value: 0, declared_only: true },
    { module: "researchdb", id: "B", value: 2, declared_only: false },
    { module: "loop_counters", id: "ops_sweeper_crashes", value: 0, declared_only: false },
  ],
};

beforeEach(() => {
  getSystems.mockResolvedValue(SYSTEMS);
  getLog.mockResolvedValue(LOG);
  getCounters.mockResolvedValue(COUNTERS);
});
afterEach(() => {
  vi.restoreAllMocks();
  getSystems.mockReset();
  getLog.mockReset();
  getCounters.mockReset();
});

describe("Operations page", () => {
  it("p1 lists the six systems in the read's order, each with 48 marks and its word", async () => {
    mount();
    await screen.findAllByText("Research worker");
    const rows = Array.from(document.querySelectorAll("[data-testid='ops-system']"));
    expect(rows).toHaveLength(6);
    expect(rows.map((r) => r.querySelector(".ops-name")?.textContent)).toEqual(
      ["Research worker", "Dashboard", "Paper broker", "News feed", "Garmin sync", "Sweeper"],
    );
    rows.forEach((r) => expect(r.querySelectorAll(".ops-strip i")).toHaveLength(48));
    expect(rows.map((r) => r.querySelector(".nl-st")?.textContent)).toEqual(
      ["Working", "Idle", "Behind", "Down", "Not connected", "Working"],
    );
    expect(within(rows[0] as HTMLElement).getByText("Laptop, Docker")).toBeInTheDocument();
  });

  it("p2 a mark's hover names its half hour in Paris time and its finding", async () => {
    mount();
    await screen.findAllByText("Research worker");
    const rows = document.querySelectorAll("[data-testid='ops-system']");
    const strip = rows[0].querySelectorAll(".ops-strip i");
    expect(strip[47].getAttribute("title")).toBe("13:30 to 14:00: Heartbeat 12 s ago");
    expect(strip[46].getAttribute("title")).toBe("13:00 to 13:30: Checked fine");
    const none = rows[4].querySelectorAll(".ops-strip i");
    expect(none[0].getAttribute("title")).toMatch(/^\d\d:\d\d to \d\d:\d\d: Nothing was checked$/);
    expect(strip[0].className).toContain("ok");
    expect(none[0].className).toContain("none");
  });

  it("p3 Not connected shows its reason only on hover", async () => {
    mount();
    await screen.findAllByText("Garmin sync");
    const word = screen.getByText("Not connected");
    expect(word.closest("[title]")?.getAttribute("title")).toContain("No Garmin login is set up yet");
    expect(document.body.textContent).not.toContain("No Garmin login is set up yet");
  });

  it("p4 the log lists the read's rows with Paris times, newest first", async () => {
    mount();
    await screen.findByText("Finished a sweep");
    const rows = Array.from(document.querySelectorAll("[data-testid='ops-log-row']"));
    expect(rows.map((r) => r.querySelector(".num")?.textContent)).toEqual(["13:00", "11:00", "08:00"]);
    expect(rows[0].textContent).toContain("Dashboard");
    expect(rows[0].textContent).toContain("Came back up");
    expect(rows.map((r) => r.querySelector(".nl-st")?.textContent)).toEqual(["Working again", "Went down", "Event"]);
  });

  it("p4 an empty log says nothing has happened yet today", async () => {
    getLog.mockResolvedValue({ log: [] });
    mount();
    expect(await screen.findByText(/^Nothing has happened today, up to \d\d:\d\d\./)).toBeInTheDocument();
  });

  it("p5 a failed systems read shows the server's detail", async () => {
    getSystems.mockRejectedValue(new ApiError("503 x - /api/ops/systems", 503, { detail: "The ops database is not there yet." }));
    mount();
    expect(await screen.findByText("The ops database is not there yet.")).toBeInTheDocument();
  });

  it("p6 the page's text has no snake_case ids", async () => {
    mount();
    await screen.findAllByText("Research worker");
    await screen.findByText("Finished a sweep");
    expect(document.body.textContent ?? "").not.toMatch(/[a-z]+_[a-z]+/);
  });

  it("p7 a log row names its part in its own column", async () => {
    mount();
    await screen.findByText("Finished a sweep");
    const rows = Array.from(document.querySelectorAll("[data-testid='ops-log-row']"));
    expect(rows[2].children[1].textContent).toBe("Research worker");
    expect(rows[2].children[2].textContent).toBe("Finished a sweep");
  });

  it("p8 the finding names what is down and since when, in Paris time", async () => {
    mount();
    const say = await screen.findByText("The news feed is down.");
    expect(say.closest(".nl-say")?.textContent).toContain("since 23 Oct, 13:30");
    expect(say.closest(".nl-say")?.textContent).toContain("The research worker and sweeper work.");
  });

  it("p9 each system's ring share counts only the checked half hours", async () => {
    mount();
    await screen.findAllByText("Research worker");
    const rows = Array.from(document.querySelectorAll("[data-testid='ops-system']"));
    expect(rows.map((r) => r.querySelector(".op-up .num")?.textContent)).toEqual(["100%", "100%", "0%", "100%", "-", "100%"]);
    expect(rows[4].querySelector(".figrow")?.getAttribute("title")).toBe("Not checked in the last 24 hours");
  });

  it("p10 the safety checks count what fired, by group, and say which are not counted yet", async () => {
    mount();
    const g = await screen.findByTestId("guards");
    expect(screen.getByText("1 fired")).toBeInTheDocument();
    expect(screen.getByText("1 not counted yet")).toBeInTheDocument();
    const rows = Array.from(g.querySelectorAll(".row"));
    expect(rows.map((r) => r.firstElementChild!.textContent)).toEqual(["Paper store", "Judge store", "Research store", "Background jobs"]);
    expect(rows.map((r) => r.children[2].textContent)).toEqual(["0", "0", "1", "0"]);
    fireEvent.click(rows[2]);
    const win = await screen.findByRole("dialog", { name: "Research store guards" });
    expect(win.textContent).toContain("Guard B");
    expect(win.textContent).toContain("Fired");
  });

  it("p12 the mockup's sections are all there, and a tile opens the part's failed checks", async () => {
    mount();
    for (const h of ["System status, last 24 hours", "Today's activity", "Safety checks"]) expect(await screen.findByRole("heading", { name: h })).toBeInTheDocument();
    const rows = document.querySelectorAll("[data-testid='ops-system']");
    fireEvent.click(rows[2]);
    const win = await screen.findByRole("dialog", { name: "Paper broker" });
    expect(win.textContent).toContain("Failed checks");
    expect(win.textContent).toContain("48");
  });

  it("p11 a failed counters read shows the server's detail and leaves the systems alone", async () => {
    getCounters.mockRejectedValue(new ApiError("500 x - /api/ops/counters", 500, { detail: "The counters could not be read." }));
    mount();
    expect(await screen.findByText("The counters could not be read.")).toBeInTheDocument();
    expect(document.querySelectorAll("[data-testid='ops-system']")).toHaveLength(6);
  });
});
