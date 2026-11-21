import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, ApiError } from "../../lib/api";
import V2App from "../shell/V2App";
import Settings, { AppPanel } from "../pages/Settings";
import { makeInstallStore, type InstallPromptEvent } from "../install";
import { SettingsProvider } from "../settings";

const ctx = { fillStyle: "", fillRect: vi.fn(), drawImage: vi.fn() };

function mountPage() {
  return render(
    <MemoryRouter initialEntries={["/app/settings"]}>
      <SettingsProvider>
        <Settings />
      </SettingsProvider>
    </MemoryRouter>,
  );
}

type Live = Awaited<ReturnType<typeof api.settings.connectionsLive>>;

// A read where nothing is set up; a case overrides the part it is about.
function live(over: Partial<Live> = {}): Live {
  return {
    checked_at: "2026-10-16T09:30:00Z",
    garmin: { configured: false, last_sync_at: null, last_sync_error: null, watch_data_until: null },
    alpaca: { configured: false, paper: true, status: null, trading_blocked: null, error: null },
    eodhd: { configured: false, requests_today: null, daily_limit: null, as_of: null, error: null },
    ...over,
  };
}
const garminRead = (g: Partial<Live["garmin"]>): Live =>
  live({ garmin: { configured: true, last_sync_at: null, last_sync_error: null, watch_data_until: null, ...g } });
const alpacaRead = (a: Partial<Live["alpaca"]>): Live =>
  live({ alpaca: { configured: true, paper: true, status: "ACTIVE", trading_blocked: false, error: null, ...a } });
const eodhdRead = (e: Partial<Live["eodhd"]>): Live =>
  live({ eodhd: { configured: true, requests_today: 39, daily_limit: 100000, as_of: "2026-10-16", error: null, ...e } });

const saved = () => JSON.parse(localStorage.getItem("v2-settings") ?? "null");
const row = (name: string) => screen.getByText(name).closest("[data-conn]") as HTMLElement;

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: true, last_synced: null });
  vi.spyOn(api.life, "weather").mockResolvedValue({} as never);
  vi.spyOn(api.settings, "connections").mockResolvedValue({ alpaca: false, eodhd: false, garmin: false });
  vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(live());
  vi.spyOn(api.life, "summarySettings").mockResolvedValue({ steps_goal: 10000, monthly_budget: 500 });
  vi.spyOn(api.life, "pushKey").mockResolvedValue({ key: "AAAA" });
  vi.spyOn(api.life, "getBudgetConfig").mockResolvedValue({ id: 1, monthly_budget: 500, fixed_expenses: 0, manual_monthly_spent: null, updated_at: "2026-11-01T00:00:00Z" });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (() => ctx) as unknown as typeof HTMLCanvasElement.prototype.getContext,
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Settings page", () => {
  it("shows the mockup's sections: the finding, page colours, look, connections, app", () => {
    mountPage();
    expect(screen.getByText("Your look and your connections.")).toBeTruthy();
    for (const t of ["Page colours", "Look", "Connections", "App"]) {
      expect(screen.getByRole("heading", { name: t })).toBeTruthy();
    }
    expect(screen.getByText("All as set")).toBeTruthy();
    expect(screen.getByText("5 services")).toBeTruthy();
    for (const n of ["Garmin", "Alpaca paper", "EODHD prices", "Google calendar", "Weather"]) {
      expect(row(n)).toBeTruthy();
    }
  });

  it("the Evening summary section sits before Connections", () => {
    mountPage();
    const eve = screen.getByRole("heading", { name: "Evening summary" });
    const conn = screen.getByRole("heading", { name: "Connections" });
    expect(eve.closest("section")?.id).toBe("stEve");
    expect(eve.compareDocumentPosition(conn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("lists every page with its set colour", () => {
    mountPage();
    expect(document.querySelectorAll("[data-page-row]")).toHaveLength(18);
    const runs = document.querySelector('[data-page-row="runs"]') as HTMLElement;
    expect(within(runs).getByText("Blue")).toBeTruthy();
  });

  it("the Look choices start on the mockup defaults", () => {
    mountPage();
    for (const n of ["Aurora", "Glass", "Still", "Full"]) {
      expect(screen.getAllByRole("button", { name: n }).some((b) => b.className.includes("on"))).toBe(true);
    }
  });

  it("Background, Buttons, Aurora lights and Motion save their choice", () => {
    mountPage();
    fireEvent.click(screen.getByRole("button", { name: "Black" }));
    fireEvent.click(screen.getByRole("button", { name: "Graphite" }));
    fireEvent.click(screen.getByRole("button", { name: "Moving" }));
    expect(saved()).toMatchObject({ bg: "black", btn: "graphite", aur: "move" });
  });

  it("Calm text sets the tone and saves it", () => {
    mountPage();
    const calm = screen.getAllByRole("button", { name: "Calm" });
    fireEvent.click(calm[calm.length - 1]);
    expect(saved().tone).toBe("calm");
  });

  it("a press on a page opens its colours; Teal is saved for that page and marked as the pick", () => {
    mountPage();
    fireEvent.click(screen.getByRole("button", { name: "Colour for Home Sports" }));
    const win = screen.getByRole("dialog");
    fireEvent.click(within(win).getByRole("button", { name: "Teal" }));
    expect(saved().accent.sports).toBe(9);
    expect(screen.queryByRole("dialog")).toBeNull();
    const row = document.querySelector('[data-page-row="sports"]') as HTMLElement;
    expect(within(row).getByText("Teal")).toBeTruthy();
    expect(within(row).getByText("Your pick")).toBeTruthy();
    expect(screen.getByText("1 of your own")).toBeTruthy();
  });

  it("Back to the set colours clears every pick", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ accent: { sports: 9, runs: 3 } }));
    mountPage();
    expect(screen.getByText("2 of your own")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back to the set colours" }));
    expect(saved().accent).toEqual({});
    expect(screen.getByText("All as set")).toBeTruthy();
  });

  it("Back to the set colour in the window clears that one page", () => {
    localStorage.setItem("v2-settings", JSON.stringify({ accent: { sports: 9, runs: 3 } }));
    mountPage();
    fireEvent.click(screen.getByRole("button", { name: "Colour for Home Sports" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Back to the set colour" }));
    expect(saved().accent).toEqual({ runs: 3 });
  });

  it("Always open sets the rail", () => {
    mountPage();
    fireEvent.click(screen.getByRole("button", { name: "Always open" }));
    expect(saved().rail).toBe("open");
  });

  it("Calm motion sets the motion off", () => {
    mountPage();
    const calm = screen.getAllByRole("button", { name: "Calm" });
    fireEvent.click(calm[0]);
    expect(saved().motion).toBe("off");
  });

  it("the Say line names the Garmin key's answer once the live read is in", async () => {
    vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(garminRead({ last_sync_at: "2026-10-16T08:30:00Z", watch_data_until: "2020-01-01" }));
    mountPage();
    await waitFor(() => expect(screen.getByText(/the watch has sent nothing since 1 Jan/)).toBeTruthy());
  });

  it("calendar connected shows CONNECTED", async () => {
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Connected")).toBeTruthy());
  });

  it("calendar error shows its words and never CONNECTED", async () => {
    vi.spyOn(api.life, "calendarStatus").mockRejectedValue(
      new ApiError("500 x - /api/life/calendar/status", 500, { detail: "Calendar key is missing" }),
    );
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Calendar key is missing")).toBeTruthy());
    expect(within(row("Google calendar")).queryByText("Connected")).toBeNull();
  });

  it("calendar not configured shows NOT CONNECTED", async () => {
    vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: false, connected: false, last_synced: null });
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Not connected")).toBeTruthy());
  });

  it("calendar configured but not connected shows Set up, not connected", async () => {
    vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: false, last_synced: null });
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Set up, not connected")).toBeTruthy());
  });

  it("weather shows WORKING on an answer", async () => {
    mountPage();
    await waitFor(() => expect(within(row("Weather")).getByText("Working")).toBeTruthy());
  });

  it("weather failure shows its words, never WORKING", async () => {
    vi.spyOn(api.life, "weather").mockRejectedValue(new Error("Weather service is down"));
    mountPage();
    await waitFor(() => expect(within(row("Weather")).getByText("Weather service is down")).toBeTruthy());
    expect(within(row("Weather")).queryByText("Working")).toBeNull();
  });

  it("Google connected: DISCONNECT asks first, then acts and reads the status again", async () => {
    const status = vi
      .spyOn(api.life, "calendarStatus")
      .mockResolvedValueOnce({ configured: true, connected: true, last_synced: null })
      .mockResolvedValue({ configured: true, connected: false, last_synced: null });
    const out = vi.spyOn(api.life, "googleDisconnect").mockResolvedValue({ ok: true });
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Connected")).toBeTruthy());
    const btn = within(row("Google calendar")).getByRole("button", { name: "Disconnect" });
    expect(btn.className).toContain("btn");
    expect(btn.className).toContain("danger");
    fireEvent.click(btn);
    expect(screen.getByText("Disconnect Google calendar?")).toBeTruthy();
    expect(out).not.toHaveBeenCalled();
    const confirm = screen.getByText("Disconnect Google calendar?").parentElement as HTMLElement;
    fireEvent.click(within(confirm).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(out).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(within(row("Google calendar")).getByText("Set up, not connected")).toBeTruthy());
    expect(status).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Disconnect Google calendar?")).toBeNull();
  });

  it("CANCEL on the disconnect question calls nothing", async () => {
    const out = vi.spyOn(api.life, "googleDisconnect").mockResolvedValue({ ok: true });
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Connected")).toBeTruthy());
    fireEvent.click(within(row("Google calendar")).getByRole("button", { name: "Disconnect" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(out).not.toHaveBeenCalled();
    expect(screen.queryByText("Disconnect Google calendar?")).toBeNull();
  });

  it("a failed disconnect closes the question and shows the server's words", async () => {
    vi.spyOn(api.life, "googleDisconnect").mockRejectedValue(
      new ApiError("500 x - /api/auth/google/disconnect", 500, { detail: "revoke failed" }),
    );
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Connected")).toBeTruthy());
    fireEvent.click(within(row("Google calendar")).getByRole("button", { name: "Disconnect" }));
    const confirm = screen.getByText("Disconnect Google calendar?").parentElement as HTMLElement;
    fireEvent.click(within(confirm).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(within(row("Google calendar")).getByText("revoke failed")).toBeTruthy());
    expect(screen.queryByText("Disconnect Google calendar?")).toBeNull();
  });

  describe("sign-in address", () => {
    const realLocation = window.location;
    beforeEach(() => {
      Object.defineProperty(window, "location", { value: { href: "http://app/" }, writable: true, configurable: true });
      vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: true, connected: false, last_synced: null });
    });
    afterEach(() => {
      Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
    });

    it("CONNECT sends the browser to the server's sign-in address", async () => {
      const url = vi.spyOn(api.life, "googleAuthUrl").mockResolvedValue({ auth_url: "https://accounts.example/auth?x=1" });
      mountPage();
      await waitFor(() => expect(within(row("Google calendar")).getByText("Set up, not connected")).toBeTruthy());
      fireEvent.click(within(row("Google calendar")).getByRole("button", { name: "Connect" }));
      await waitFor(() => expect(window.location.href).toBe("https://accounts.example/auth?x=1"));
      expect(url).toHaveBeenCalledTimes(1);
    });

    it("a refused sign-in shows the server's words, keeps CONNECT and never navigates", async () => {
      vi.spyOn(api.life, "googleAuthUrl").mockRejectedValue(
        new ApiError("503 x - /api/auth/google", 503, { detail: "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set" }),
      );
      mountPage();
      await waitFor(() => expect(within(row("Google calendar")).getByText("Set up, not connected")).toBeTruthy());
      fireEvent.click(within(row("Google calendar")).getByRole("button", { name: "Connect" }));
      await waitFor(() =>
        expect(within(row("Google calendar")).getByText("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")).toBeTruthy(),
      );
      expect(within(row("Google calendar")).getByRole("button", { name: "Connect" })).toBeTruthy();
      expect(window.location.href).toBe("http://app/");
    });
  });

  it("Google not set up on the server: NOT CONNECTED, no button, the hover", async () => {
    vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: false, connected: false, last_synced: null });
    mountPage();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Not connected")).toBeTruthy());
    expect(within(row("Google calendar")).queryByRole("button")).toBeNull();
    expect(row("Google calendar").querySelector('[title="Google sign-in is not set up on the server"]')).toBeTruthy();
  });

  it("no Google button while the status loads or when it fails", async () => {
    vi.spyOn(api.life, "calendarStatus").mockRejectedValue(
      new ApiError("500 x - /api/life/calendar/status", 500, { detail: "Calendar key is missing" }),
    );
    mountPage();
    expect(within(row("Google calendar")).queryByRole("button")).toBeNull();
    await waitFor(() => expect(within(row("Google calendar")).getByText("Calendar key is missing")).toBeTruthy());
    expect(within(row("Google calendar")).queryByRole("button")).toBeNull();
  });

  it("s5: keys not set up on the server read NOT SET UP with today's hover, never CONNECTED, no button", async () => {
    vi.spyOn(api.life, "calendarStatus").mockResolvedValue({ configured: false, connected: false, last_synced: null });
    mountPage();
    for (const n of ["Alpaca paper", "EODHD prices", "Garmin"]) {
      await waitFor(() => expect(within(row(n)).getByText("Not set up")).toBeTruthy());
      expect(within(row(n)).queryByRole("button")).toBeNull();
      expect(within(row(n)).queryByText("Connected")).toBeNull();
    }
    expect(row("Alpaca paper").querySelector('[title="The Alpaca keys are not set on the server"]')).toBeTruthy();
    expect(row("EODHD prices").querySelector('[title="The EODHD key is not set on the server"]')).toBeTruthy();
    expect(row("Garmin").querySelector('[title="Garmin\'s sign-in is not set on the server"]')).toBeTruthy();
  });

  it("s6: a failed live read shows the server's words in the three key rows", async () => {
    vi.spyOn(api.settings, "connectionsLive").mockRejectedValue(
      new ApiError("500 x - /api/settings/connections/live", 500, { detail: "Connections check failed" }),
    );
    mountPage();
    for (const n of ["Alpaca paper", "EODHD prices", "Garmin"]) {
      await waitFor(() => expect(within(row(n)).getByText("Connections check failed")).toBeTruthy());
    }
  });

  it("while the live read loads the three rows show a dash with Checking", () => {
    vi.spyOn(api.settings, "connectionsLive").mockReturnValue(new Promise(() => {}));
    mountPage();
    for (const n of ["Alpaca paper", "EODHD prices", "Garmin"]) {
      expect(row(n).querySelector('[title="Checking"]')?.textContent).toBe("-");
    }
  });

  describe("live connection states (clock 2026-10-16 09:30 UTC, 11:30 in Paris)", () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date("2026-10-16T09:30:00Z"));
    });
    afterEach(() => {
      vi.useRealTimers();
    });
    const isTone = (el: HTMLElement, tone: string) => el.classList.contains(tone);

    it("s1: a sync an hour ago with an error reads SYNCED 10:30, WITH GAPS in amber, the error on hover, watch data amber", async () => {
      const err = "training_status: no primary training device in the answer";
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        garminRead({ last_sync_at: "2026-10-16T08:30:00Z", last_sync_error: err, watch_data_until: "2026-10-14" }),
      );
      mountPage();
      const word = await waitFor(() => within(row("Garmin")).getByText("Synced 10:30, with gaps"));
      expect(isTone(word, "warn")).toBe(true);
      expect(row("Garmin").querySelector(`[title*="${err}"]`)).toBeTruthy();
      const watch = within(row("Garmin")).getByText("Watch data up to 14 Oct");
      expect(isTone(watch, "warn")).toBe(true);
      expect(within(row("Garmin")).queryByText("SET UP")).toBeNull();
    });

    it("a clean sync an hour ago reads Synced 10:30 in green, and watch data up to yesterday is plain", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        garminRead({ last_sync_at: "2026-10-16T08:30:00Z", watch_data_until: "2026-10-15" }),
      );
      mountPage();
      const word = await waitFor(() => within(row("Garmin")).getByText("Synced 10:30"));
      expect(isTone(word, "good")).toBe(true);
      const watch = within(row("Garmin")).getByText("Watch data up to 15 Oct");
      expect(isTone(watch, "warn")).toBe(false);
    });

    it("no watch data at all reads No watch data yet", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(garminRead({ last_sync_at: "2026-10-16T08:30:00Z" }));
      mountPage();
      await waitFor(() => expect(within(row("Garmin")).getByText("No watch data yet")).toBeTruthy());
    });

    it("s2: a sync three hours ago reads NO SYNC SINCE in red", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(garminRead({ last_sync_at: "2026-10-16T06:30:00Z" }));
      mountPage();
      const word = await waitFor(() => within(row("Garmin")).getByText("No sync since 16 Oct 08:30"));
      expect(isTone(word, "bad")).toBe(true);
    });

    it("s2: no sync ever reads NEVER SYNCED in red", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(garminRead({}));
      mountPage();
      const word = await waitFor(() => within(row("Garmin")).getByText("Never synced"));
      expect(isTone(word, "bad")).toBe(true);
    });

    it("s7: a stale sync with an error is red, not amber", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        garminRead({ last_sync_at: "2026-10-16T06:30:00Z", last_sync_error: "steps: x" }),
      );
      mountPage();
      const word = await waitFor(() => within(row("Garmin")).getByText("No sync since 16 Oct 08:30"));
      expect(isTone(word, "bad")).toBe(true);
      expect(isTone(word, "warn")).toBe(false);
    });

    it("a Garmin part the server could not read shows its sentence in red", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        live({
          garmin: {
            configured: true, last_sync_at: null, last_sync_error: null, watch_data_until: null,
            error: "Garmin could not be read, see the server log",
          },
        }),
      );
      mountPage();
      const word = await waitFor(() => within(row("Garmin")).getByText("Garmin could not be read, see the server log"));
      expect(isTone(word, "bad")).toBe(true);
    });

    it("s3: Alpaca ACTIVE reads ACTIVE, PAPER in green", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(alpacaRead({}));
      mountPage();
      const word = await waitFor(() => within(row("Alpaca paper")).getByText("Active, paper"));
      expect(isTone(word, "good")).toBe(true);
    });

    it("s3: ACCOUNT_BLOCKED reads Account blocked in amber, the raw status on hover", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(alpacaRead({ status: "ACCOUNT_BLOCKED" }));
      mountPage();
      const word = await waitFor(() => within(row("Alpaca paper")).getByText("Account blocked"));
      expect(isTone(word, "warn")).toBe(true);
      expect(row("Alpaca paper").querySelector('[title*="ACCOUNT_BLOCKED"]')).toBeTruthy();
      expect(within(row("Alpaca paper")).queryByText("ACCOUNT_BLOCKED")).toBeNull();
    });

    it("s3: ACTIVE with trading blocked reads Trading blocked, paper in amber", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(alpacaRead({ trading_blocked: true }));
      mountPage();
      const word = await waitFor(() => within(row("Alpaca paper")).getByText("Trading blocked, paper"));
      expect(isTone(word, "warn")).toBe(true);
    });

    it("s3: an Alpaca error shows its sentence in red", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        alpacaRead({ status: null, trading_blocked: null, error: "Alpaca answered HTTP 401" }),
      );
      mountPage();
      const word = await waitFor(() => within(row("Alpaca paper")).getByText("Alpaca answered HTTP 401"));
      expect(isTone(word, "bad")).toBe(true);
    });

    it("s4: EODHD 39 of 100000 reads 39 OF 100,000 CALLS TODAY in green", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(eodhdRead({}));
      mountPage();
      const word = await waitFor(() => within(row("EODHD prices")).getByText("39 of 100,000 calls today"));
      expect(isTone(word, "good")).toBe(true);
    });

    it("s4: EODHD at 85000 of 100000 reads amber", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(eodhdRead({ requests_today: 85000 }));
      mountPage();
      const word = await waitFor(() => within(row("EODHD prices")).getByText("85,000 of 100,000 calls today"));
      expect(isTone(word, "warn")).toBe(true);
    });

    it("s4: an EODHD error shows its sentence in red", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        eodhdRead({ requests_today: null, daily_limit: null, as_of: null, error: "EODHD answered HTTP 403" }),
      );
      mountPage();
      const word = await waitFor(() => within(row("EODHD prices")).getByText("EODHD answered HTTP 403"));
      expect(isTone(word, "bad")).toBe(true);
    });

    it("s7: the hovers give the source and when it was checked, and nothing is snake_case", async () => {
      vi.spyOn(api.settings, "connectionsLive").mockResolvedValue(
        garminRead({ last_sync_at: "2026-10-16T08:30:00Z", watch_data_until: "2026-10-15" }),
      );
      mountPage();
      await waitFor(() => expect(within(row("Garmin")).getByText("Synced 10:30")).toBeTruthy());
      const hover = Array.from(row("Garmin").querySelectorAll("[title]")).map((e) => e.getAttribute("title") ?? "").join(" | ");
      expect(hover).toContain("Checked 16 Oct 11:30");
      expect(hover).not.toMatch(/[a-z]+_[a-z]+/);
    });
  });

  it("a save that throws shows the amber sentence", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("full");
    });
    mountPage();
    fireEvent.click(screen.getByRole("button", { name: "Always open" }));
    expect(screen.getByText(/^Settings could not be saved/)).toBeTruthy();
  });

  it("inside V2App, the Look choices reach the .v2 root at once", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/app/settings"]}>
          <V2App />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const root = () => document.querySelector(".v2") as HTMLElement;
    expect(root().classList.contains("vivid")).toBe(true);
    expect(root().dataset.page).toBe("settings");
    const calm = screen.getAllByRole("button", { name: "Calm" });
    fireEvent.click(calm[calm.length - 1]);
    expect(root().classList.contains("vivid")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Black" }));
    fireEvent.click(screen.getByRole("button", { name: "White" }));
    fireEvent.click(screen.getByRole("button", { name: "Moving" }));
    expect(root().dataset.bg).toBe("black");
    expect(root().dataset.btn).toBe("white");
    expect(root().dataset.aur).toBe("move");
    expect(root().querySelector(".aur")).toBeNull();
  });
});

// A browser's install offer: prompt() opens its install window, userChoice is the answer given there.
function installOffer(outcome: "accepted" | "dismissed") {
  const e = new Event("beforeinstallprompt", { cancelable: true }) as InstallPromptEvent;
  const prompt = vi.fn().mockResolvedValue(undefined);
  Object.assign(e, { prompt, userChoice: Promise.resolve({ outcome }) });
  return { e, prompt };
}
const appRow = () => screen.getByText("Installed app").closest("tr") as HTMLElement;

describe("APP panel", () => {
  afterEach(() => {
    Reflect.deleteProperty(window, "matchMedia");
  });

  it("an offer shows NOT INSTALLED and INSTALL; accepting opens the window once and reads INSTALLED", async () => {
    const store = makeInstallStore(window);
    render(<AppPanel store={store} />);
    const { e, prompt } = installOffer("accepted");
    act(() => {
      window.dispatchEvent(e);
    });
    expect(e.defaultPrevented).toBe(true);
    expect(within(appRow()).getByText("Not installed")).toBeTruthy();
    fireEvent.click(within(appRow()).getByRole("button", { name: "Install" }));
    await waitFor(() => expect(within(appRow()).getByText("Installed")).toBeTruthy());
    expect(prompt).toHaveBeenCalledTimes(1);
    expect(within(appRow()).queryByRole("button")).toBeNull();
  });

  it("a dismissed install spends the offer: no button, NOT OFFERED HERE", async () => {
    const store = makeInstallStore(window);
    render(<AppPanel store={store} />);
    const { e } = installOffer("dismissed");
    act(() => {
      window.dispatchEvent(e);
    });
    fireEvent.click(within(appRow()).getByRole("button", { name: "Install" }));
    await waitFor(() => expect(within(appRow()).getByText("Not offered here")).toBeTruthy());
    expect(within(appRow()).queryByRole("button")).toBeNull();
  });

  it("an install window that fails to open shows it in red with the reason on hover", async () => {
    const store = makeInstallStore(window);
    render(<AppPanel store={store} />);
    const { e, prompt } = installOffer("accepted");
    prompt.mockRejectedValue(new Error("not allowed"));
    act(() => {
      window.dispatchEvent(e);
    });
    fireEvent.click(within(appRow()).getByRole("button", { name: "Install" }));
    await waitFor(() => expect(within(appRow()).getByText("The install window did not open")).toBeTruthy());
    expect(within(appRow()).getByTitle("not allowed")).toBeTruthy();
  });

  it("the browser's app installed event reads INSTALLED", () => {
    const store = makeInstallStore(window);
    render(<AppPanel store={store} />);
    act(() => {
      window.dispatchEvent(new Event("appinstalled"));
    });
    expect(within(appRow()).getByText("Installed")).toBeTruthy();
  });

  it("opened as the installed app reads INSTALLED with no button", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (q: string) => ({ matches: q === "(display-mode: standalone)" }),
    });
    render(<AppPanel store={makeInstallStore(window)} />);
    expect(within(appRow()).getByText("Installed")).toBeTruthy();
    expect(within(appRow()).queryByRole("button")).toBeNull();
  });

  it("an iPhone with no offer is told to use the Share menu", () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Mobile/15E148 Safari/604.1",
    );
    render(<AppPanel store={makeInstallStore(window)} />);
    expect(within(appRow()).getByText("Share, then add to home screen")).toBeTruthy();
  });

  it("a desktop browser with no offer reads NOT OFFERED HERE with the menu on hover", () => {
    render(<AppPanel store={makeInstallStore(window)} />);
    const word = within(appRow()).getByText("Not offered here");
    expect(word.closest("span[title]")?.getAttribute("title")).toContain("Install in the browser menu");
  });
});
