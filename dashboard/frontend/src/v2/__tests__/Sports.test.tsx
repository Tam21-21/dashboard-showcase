import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../lib/api";
import type { GarminActivity, GarminActivityBundle, GarminSnapshot } from "../../types";
import Sports from "../pages/home/Sports";
import { sportOf } from "../pages/home/data";
import { activityWord, asOfLabel, newestWith, parisClock } from "../pages/home/sports/derive";
import { SettingsProvider } from "../settings";
import { APPS, pageFor } from "../shell/sections";

const app = APPS.find((a) => a.id === "home")!;
const section = app.sections.find((s) => s.id === "sports")!;
const LONG_DASH = String.fromCharCode(0x2014);

// Today is Tuesday 2026-10-13. The 84 snapshot days run 2026-07-22 to 2026-10-13.
const iso = (t: number) => new Date(t).toISOString().slice(0, 10);
const DAY = 86400000;
const TODAY = Date.UTC(2026, 9, 13);
const DATES = Array.from({ length: 84 }, (_, i) => iso(TODAY - (83 - i) * DAY));
const recent = (d: string) => d >= "2026-10-07"; // the 7 newest nights
const prev = (d: string) => iso(Date.parse(d) - DAY);

const FOCUS = [["Low aerobic", 806, 433, 952], ["High aerobic", 457, 519, 1039], ["Anaerobic", 373, 173, 519]].map(
  ([name, value, target_min, target_max]) => ({ name, value, target_min, target_max }),
);
const CLASSES = [["Recreational", 3570], ["Intermediate", 5100], ["Trained", 5800], ["Well trained", 6600], ["Expert", 7300], ["Superior", 8100], ["Elite", 8800]].map(
  ([name, lower]) => ({ name, lower }),
);
const FACTORS = [["sleep", 80], ["recovery time", 90], ["load", 55], ["HRV", 70], ["stress history", 65]].map(([name, percent]) => ({
  name, percent, feedback: "GOOD",
}));
const BATTERY = [[1760140800000, 40], [1760144400000, 55], [1760148000000, 61], [1760151600000, 58], [1760155200000, 52]];

function snap(date: string, over: Partial<GarminSnapshot> = {}): GarminSnapshot {
  const newest = date === "2026-10-13";
  const base: Record<string, unknown> = {
    date,
    sleep_duration_sec: 21600, sleep_deep_sec: recent(date) ? 4800 : 3600, sleep_light_sec: 13200, sleep_rem_sec: 3600,
    sleep_awake_sec: 300, sleep_score: recent(date) ? 78 : 70, sleep_score_word: "FAIR", sleep_need_min: 480,
    sleep_start_local: `${prev(date)}T${recent(date) ? "23:40" : "23:00"}`,
    sleep_end_local: `${date}T${recent(date) ? "06:20" : "05:20"}`,
    resting_hr: recent(date) ? 48 : 52, hrv_overnight: recent(date) ? 78 : 70, hrv_status: "BALANCED",
    hrv_balanced_low: 72, hrv_balanced_high: 97, steps: newest ? 9000 : 8000, steps_goal: 14540,
    intensity_moderate_min: 20, intensity_vigorous_min: 10, intensity_goal: 150,
    acute_load: 100 + Number(date.slice(8, 10)), readiness: 58, readiness_level: "MODERATE", recovery_minutes: 399,
    body_battery_pct: 21, body_battery_high: 75, body_battery_low: 10, body_battery_at_wake: 73,
    vo2max_garmin: 57, fitness_age: 18, hill_score: 34, lactate_threshold_hr: 186, threshold_power_w: 318,
    training_status: newest ? "PRODUCTIVE" : null,
    endurance_score: null,
    load_focus_json: newest ? JSON.stringify(FOCUS) : null,
    endurance_limits_json: newest ? JSON.stringify({ gauge_lower: 3570, gauge_upper: 10560, classes: CLASSES }) : null,
    readiness_factors_json: newest ? JSON.stringify(FACTORS) : null,
    body_battery_series_json: newest ? JSON.stringify(BATTERY) : null,
    race_5k_sec: newest ? 1271 : null, race_10k_sec: newest ? 2776 : null,
    race_half_sec: newest ? 6364 : null, race_marathon_sec: newest ? 14326 : null,
  };
  // Endurance score: week of 5 Oct has 6600 on the 6th and 6620 on the 8th; the current week 6658.
  if (date === "2026-10-06") base.endurance_score = 6600;
  if (date === "2026-10-08") base.endurance_score = 6620;
  if (newest) base.endurance_score = 6658;
  return { ...base, ...over } as GarminSnapshot;
}
const week = (over: (d: string) => Partial<GarminSnapshot> = () => ({})): GarminSnapshot[] => DATES.map((d) => snap(d, over(d)));

const act1 = (id: string, type: string | null, at: string, m: number | null, sec: number, name: string | null) =>
  ({
    activity_id: id, type_key: type, start_time_local: at, distance_m: m, duration_sec: sec, name,
    avg_hr: 150, max_hr: 170, avg_pace_min_per_km: 5, aerobic_te: 3.4, training_load: 120, elevation_gain_m: 64, total_calories: 640,
  }) as GarminActivity;
const ACTS = [
  act1("1", "running", "2026-10-12T08:00:00", 10000, 3000, "Morning run"),
  act1("2", "lap_swimming", "2026-10-13T07:00:00", 1500, 1800, "Pool swim"),
  act1("3", "strength_training", "2026-10-13T18:00:00", null, 3600, null),
  act1("4", "meditation", "2026-10-12T21:00:00", null, 600, "Breathing"),
  act1("5", "cycling", "2026-10-11T09:00:00", 40000, 5400, "Ride"),
  act1("6", "running", "2026-10-06T08:00:00", 5000, 1500, "Old run"),
];

const BUNDLE: GarminActivityBundle = {
  summary: ACTS[0],
  detail: {
    activity_id: "1",
    samples: [{ directHeartRate: 120 }, { directHeartRate: 150 }, { directHeartRate: 165 }, {}],
    polyline: [],
    splits: [1, 2, 3].map((lap) => ({
      lap, distance_m: 1000, duration_sec: 300, avg_hr: 150 + lap, max_hr: 170, avg_speed_mps: 3.3, pace_min_per_km: 5, elevation_gain_m: 2, calories: 60,
    })),
    hr_zones: [1, 2, 3, 4, 5].map((zone) => ({ zone, low_bpm: 100 + zone * 10, high_bpm: 110 + zone * 10, seconds: zone * 300 })),
    exercise_sets: [],
    gear: [],
    summary_extras: { recovery_hours: null, training_effect_msg: null, device_name: null },
  },
};

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsProvider>
        <MemoryRouter>
          <Sports app={app} section={section} />
        </MemoryRouter>
      </SettingsProvider>
    </QueryClientProvider>,
  );
}
const days = (snapshots: GarminSnapshot[], configured = true) =>
  vi.spyOn(api.life, "garminDays").mockResolvedValue({ configured, snapshots });
const acts = (activities: GarminActivity[], configured = true) =>
  vi.spyOn(api.life, "garminActivities").mockResolvedValue({ configured, activities });

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 13, 12, 0));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0, top: 0, right: 600, bottom: 300, x: 0, y: 0, width: 600, height: 300, toJSON: () => ({}),
  } as DOMRect);
  days(week((d) => (d === "2026-10-05" ? { sleep_duration_sec: null } : d === "2026-10-12" ? { acute_load: null } : {})));
  acts(ACTS);
  vi.spyOn(api.life, "garminActivityDetail").mockResolvedValue(BUNDLE);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const card = (title: string) => screen.getByRole("heading", { name: title }).closest("section") as HTMLElement;
const ready = () => waitFor(() => expect(screen.getByTestId("sessions")).toBeTruthy());

describe("Home Sports, as the mockup", () => {
  it("(a) is registered for home/sports (guard)", () => {
    expect(pageFor(app, section)).toBe(Sports);
  });

  it("(a) sportOf groups the types", () => {
    for (const k of ["running", "treadmill_running", "trail_running"]) expect(sportOf(k)).toBe("Running");
    for (const k of ["cycling", "indoor_cycling", "road_biking", "mountain_biking", "gravel_cycling", "virtual_ride"]) expect(sportOf(k)).toBe("Cycling");
    for (const k of ["lap_swimming", "open_water_swimming"]) expect(sportOf(k)).toBe("Swimming");
    for (const k of ["strength_training", "yoga", "walking", null]) expect(sportOf(k)).toBe("Gym and other");
  });

  it("s1 the mockup's sections, in order: Needs you, Summary, Fitness, Training by week, Recovery by night, All sessions", async () => {
    mount();
    await ready();
    expect(document.querySelector("h1")!.textContent).toBe("Sports");
    expect(Array.from(document.querySelectorAll("h2")).map((h) => h.textContent)).toEqual(
      ["Needs you", "Summary", "Fitness, fatigue and form", "Training by week", "Recovery by night", "All sessions"],
    );
  });

  it("s2 the finding: rest, go easy or train, each reason from the page's own numbers", async () => {
    mount();
    await ready();
    const say = document.querySelector(".nl-say")!.textContent!;
    expect(say).toContain("Rest today.");
    expect(say).toContain("Your heart rate variability has been above your normal range for 7 days, fatigue is far above your fitness and you slept");
    expect(say).toContain("2 h 00 less than you need");
    expect(say).toMatch(/This week: 2 h 30 in 4 sessions\./);
  });

  it("s3 the six summary tiles in order; burn and body say nothing yet", async () => {
    mount();
    await ready();
    const wall = screen.getByTestId("wall");
    expect(Array.from(wall.querySelectorAll("[data-tile]")).map((t) => t.getAttribute("data-tile"))).toEqual(["fit", "week", "rec", "sleep", "burn", "body"]);
    const week = wall.querySelector('[data-tile="week"]')!;
    expect(week.textContent).toContain("2h30");
    expect(week.textContent).toContain("Sessions4");
    expect(week.textContent).toContain("Active minutes80 of 150");
    expect(week.textContent).toContain("Strength days1 of 2");
    expect(wall.querySelector('[data-tile="sleep"]')!.textContent).toContain("6h00");
    expect(wall.querySelector('[data-tile="food"]')).toBeNull();
    expect(wall.querySelector('[data-tile="burn"]')!.textContent).toContain("Calories burned");
    expect(wall.querySelector('[data-tile="burn"]')!.textContent).toContain("Nothing yet");
    expect(wall.querySelector('[data-tile="body"]')!.textContent).toContain("Nothing logged");
  });

  it("s4 training by week: 26 bars, the white one is this week, a sport chip changes the unit", async () => {
    mount();
    await ready();
    expect(document.querySelectorAll('[data-testid="weeks-chart"] rect[data-week]')).toHaveLength(26);
    expect(document.querySelector('[data-testid="weeks-chart"] text')!.textContent).toMatch(/ h$/);
    fireEvent.click(within(card("Training by week")).getByRole("button", { name: "Running" }));
    expect(document.querySelector('[data-testid="weeks-chart"] text')!.textContent).toMatch(/ km$/);
    fireEvent.click(within(card("Training by week")).getByRole("button", { name: "Swimming" }));
    expect(within(card("Training by week")).getByRole("button", { name: "Swimming" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("s5 recovery by night: three readings, each its own chart", async () => {
    mount();
    await ready();
    const box = card("Recovery by night");
    expect(box.querySelector('[data-testid="nights-chart"] text')!.textContent).toMatch(/ ms$/);
    fireEvent.click(within(box).getByRole("button", { name: "Resting heart rate" }));
    expect(box.querySelector('[data-testid="nights-chart"] text')!.textContent).toMatch(/ bpm$/);
    fireEvent.click(within(box).getByRole("button", { name: "Sleep" }));
    expect(box.querySelector('[data-testid="nights-chart"] text')!.textContent).toMatch(/ h$/);
  });

  it("s6 all sessions: newest first, a search and a sport chip narrow the list, a row opens the window", async () => {
    mount();
    await ready();
    const rows = () => Array.from(document.querySelectorAll('[data-testid="sessions"] tbody tr'));
    expect(rows()).toHaveLength(6);
    expect(rows()[0].textContent).toContain("Strength training");
    expect(card("All sessions").textContent).toContain("6 of 6 shown");
    fireEvent.change(screen.getByLabelText("Search sessions"), { target: { value: "swim" } });
    expect(rows()).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("Search sessions"), { target: { value: "" } });
    fireEvent.click(within(card("All sessions")).getByRole("button", { name: "Running" }));
    expect(rows()).toHaveLength(2);
    fireEvent.click(document.querySelector('tr[data-act="1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector('[role="dialog"]')).not.toBeNull());
  });

  it("s7 a watch that has gone quiet: the finding says so and Needs you names the days missing", async () => {
    days(week().filter((r) => r.date <= "2026-10-08"));
    mount();
    await ready();
    expect(document.querySelector(".nl-say")!.textContent).toContain("Your watch has sent nothing since 8 Oct.");
    expect(card("Needs you").textContent).toContain("The watch has sent nothing since 8 Oct");
    expect(card("Needs you").textContent).toContain("5 of the last 28 days missing");
  });

  it("s8 Garmin not set up: one sentence, no tiles", async () => {
    days([], false);
    acts([], false);
    mount();
    expect(await screen.findByText("Garmin is not set up on the server")).toBeTruthy();
    expect(document.querySelector('[data-testid="wall"]')).toBeNull();
  });

  it("s9 guard: no long dash on the page", async () => {
    mount();
    await ready();
    expect(document.body.textContent).not.toContain(LONG_DASH);
  });
});

// KC1b: the Calories burned tile. Today is 2026-10-13; the two parts sit on 16 rows.
const PARTS: Record<string, [number, number]> = {
  "2026-09-28": [1800, 400], "2026-09-29": [1800, 400], "2026-09-30": [1800, 400], "2026-10-01": [1800, 400], "2026-10-02": [1800, 400],
  "2026-10-03": [1800, 400], "2026-10-04": [1800, 400], "2026-10-05": [1800, 600], "2026-10-06": [1800, 700], "2026-10-07": [1800, 700],
  "2026-10-08": [1800, 700], "2026-10-09": [1800, 700], "2026-10-10": [1800, 700], "2026-10-11": [1800, 700], "2026-10-12": [1700, 610],
  "2026-10-13": [900, 150],
};
const burnRows = (parts: Record<string, [number | null, number | null]>) =>
  week((d) => ({ resting_calories: parts[d]?.[0] ?? null, active_calories: parts[d]?.[1] ?? null }) as Partial<GarminSnapshot>);
const burnTile = async () => {
  mount();
  await ready();
  return screen.getByTestId("wall").querySelector('[data-tile="burn"]') as HTMLElement;
};
const factOf = (t: HTMLElement, name: string) => Array.from(t.querySelectorAll(".sp-facts span")).find((s) => s.querySelector("i")!.textContent === name)!.textContent;

describe("Calories burned tile", () => {
  it("b1 the 7-day average, the chip against the 7 days before, the facts and 14 bars", async () => {
    days(burnRows(PARTS));
    const t = await burnTile();
    expect(t.querySelector(".sp-fig")!.firstChild!.textContent).toBe("2,470");
    expect(t.querySelector(".sp-unit")!.textContent).toBe("kcal a day");
    expect(t.querySelector(".sp-th")!.textContent).toContain("Up from 2,230");
    expect(factOf(t, "Today so far")).toBe("Today so far1,050");
    expect(factOf(t, "Yesterday")).toBe("Yesterday2,310");
    expect(factOf(t, "By moving")).toBe("By moving690");
    expect(factOf(t, "At rest")).toBe("At rest1,790");
    expect(t.querySelectorAll(".sp-spark rect")).toHaveLength(14);
  });

  it("b2 a day with a missing part is left out; fewer than 7 days before means no chip", async () => {
    const parts = { ...PARTS, "2026-10-07": [null, 700], "2026-09-28": [null, 400] } as Record<string, [number | null, number | null]>;
    days(burnRows(parts));
    const t = await burnTile();
    expect(t.querySelector(".sp-fig")!.firstChild!.textContent).toBe("2,460");
    expect(factOf(t, "By moving")).toBe("By moving670");
    expect(t.querySelector(".sp-th")!.textContent).not.toMatch(/Up from|Down from/);
    expect(t.querySelectorAll(".sp-spark rect")).toHaveLength(13);
  });

  it("b3 no row for today: Today so far is a dash", async () => {
    const parts = { ...PARTS };
    delete parts["2026-10-13"];
    days(burnRows(parts));
    const t = await burnTile();
    expect(factOf(t, "Today so far")).toBe("Today so far-");
  });

  it("b4 the hover says what it counts; no hue", async () => {
    days(burnRows(PARTS));
    const t = await burnTile();
    expect(t.getAttribute("title")).toContain("at rest plus by moving");
    expect(t.getAttribute("title")).toContain("A day counts once it is over");
    expect(t.className).toBe("sp-tile");
  });

  it("b5 a lower week reads Down from", async () => {
    const parts = { ...PARTS } as Record<string, [number, number]>;
    for (const d of ["06", "07", "08", "09", "10", "11"]) parts[`2026-10-${d}`] = [1800, 300];
    days(burnRows(parts));
    const t = await burnTile();
    expect(t.querySelector(".sp-fig")!.firstChild!.textContent).toBe("2,130");
    expect(t.querySelector(".sp-th")!.textContent).toContain("Down from 2,230");
  });
});

// UI30a: the same page when the watch has stopped sending. Now is 2026-10-16 10:00 UTC; the newest data is 2026-10-14.
const KEYS = [
  "sleep_score", "sleep_duration_sec", "sleep_deep_sec", "sleep_light_sec", "sleep_rem_sec", "body_battery_pct", "resting_hr", "steps",
  "readiness", "stress", "total_calories", "active_calories", "resting_calories", "hrv_overnight", "intensity_moderate_min", "intensity_vigorous_min", "recovery_minutes", "vo2max",
  "vo2max_as_of", "sleep_levels_json", "body_battery_high", "body_battery_low", "body_battery_at_wake", "acute_load", "steps_goal",
  "hrv_balanced_low", "hrv_balanced_high", "sleep_awake_sec", "sleep_need_min", "endurance_score", "race_5k_sec", "race_10k_sec",
  "race_half_sec", "race_marathon_sec", "hill_score", "lactate_threshold_hr", "threshold_power_w", "intensity_goal", "vo2max_garmin",
  "fitness_age", "readiness_level", "hrv_status", "sleep_score_word", "training_status", "sleep_start_local", "sleep_end_local",
  "readiness_factors_json", "load_focus_json", "endurance_limits_json", "body_battery_series_json", "raw_json", "fetch_error",
];
function stale(date: string, fetched_at: string, over: Partial<GarminSnapshot> = {}): GarminSnapshot {
  return { ...Object.fromEntries(KEYS.map((k) => [k, null])), date, fetched_at, ...over } as GarminSnapshot;
}

describe("Sports helpers", () => {
  it("(t) activityWord names the types", () => {
    const map: Record<string, string> = {
      soccer: "Football", strength_training: "Strength", lap_swimming: "Pool swim", open_water_swimming: "Open water swim", running: "Running",
      treadmill_running: "Treadmill run", cycling: "Cycling", walking: "Walking", paddelball: "Padel", table_tennis: "Table tennis",
      stair_climbing: "Stair climbing", multi_sport: "Multisport", meditation: "Meditation", other: "Other", trail_running: "Trail running",
    };
    for (const [k, w] of Object.entries(map)) expect(activityWord(k)).toBe(w);
  });
  it("the helpers: newestWith, asOfLabel, parisClock", () => {
    const rows = [stale("2026-10-13", "x", { steps: 5 }), stale("2026-10-14", "x"), stale("2026-10-15", "x", { steps: 0 })];
    expect(newestWith(rows, (r) => r.steps)?.date).toBe("2026-10-15");
    expect(newestWith(rows, (r) => r.hill_score)).toBeNull();
    expect(newestWith([], (r) => r.steps)).toBeNull();
    expect(asOfLabel("2026-10-16", "2026-10-16")).toBe("");
    expect(asOfLabel("2026-10-14", "2026-10-16")).toBe("as of 14 Oct");
    expect(parisClock("2026-10-14T23:00:00")).toBe("15 Oct 01:00");
    expect(parisClock("2026-01-14T23:30:00")).toBe("15 Jan 00:30");
  });
});

// UI30b: the activity window is its own layer on the body, with plain words in it.
describe("UI30b: the activity window", () => {
  const FOOTBALL = {
    ...act1("21", "soccer", "2026-10-14T19:00:00", 3200, 5400, "Lyon Soccer/Football"),
    avg_pace_min_per_km: 21.27, aerobic_te: 3.0999990463256836,
  } as GarminActivity;
  const STRENGTH = { ...act1("22", "strength_training", "2026-10-14T18:00:00", 0, 3600, "Gym"), avg_pace_min_per_km: null } as GarminActivity;
  beforeEach(() => {
    acts([...ACTS, FOOTBALL, STRENGTH]);
  });

  const dialog = () =>
    waitFor(() => {
      const d = document.querySelector('[role="dialog"]') as HTMLElement | null;
      expect(d).not.toBeNull();
      return d as HTMLElement;
    });
  const open = async (id: string) => {
    mount();
    await ready();
    fireEvent.click(document.querySelector(`tr[data-act="${id}"]`) as HTMLElement);
    return dialog();
  };
  const labels = (d: HTMLElement) => Array.from(d.querySelectorAll(".kl")).map((e) => e.textContent);
  const value = (d: HTMLElement, label: string) =>
    Array.from(d.querySelectorAll(".kl")).find((e) => e.textContent === label)?.nextElementSibling?.textContent;

  it("(w1) the overlay is a direct child of the body with the classes v2 and spov", async () => {
    await open("1");
    const ov = document.querySelector(".spov") as HTMLElement;
    expect(ov.parentElement).toBe(document.body);
    expect(ov.classList.contains("v2")).toBe(true);
    expect(ov.querySelector('[role="dialog"]')).not.toBeNull();
  });

  it("(w2) the title is the short word and the date, Garmin's own name on hover", async () => {
    const d = await open("21");
    const h = d.querySelector(".spovt .h2") as HTMLElement;
    expect(h.textContent).toBe("FOOTBALL, 14 OCT");
    expect(h.querySelector("[title]")?.getAttribute("title")).toBe("Lyon Soccer/Football");
  });

  it("(w3) training effect shows one decimal", async () => {
    const d = await open("21");
    expect(value(d, "TRAINING EFFECT")).toBe("3.1 aerobic");
    expect(d.textContent).not.toContain("3.0999");
  });

  it("(w4) pace only for running, treadmill, walking and hiking", async () => {
    const f = await open("21");
    expect(labels(f)).not.toContain("PACE");
    expect(f.textContent).not.toContain("21:16");
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(document.querySelector('tr[data-act="1"]') as HTMLElement);
    const r = await dialog();
    expect(value(r, "PACE")).toBe("5:00 /km");
  });

  it("(w5) a distance of zero leaves the Distance row out", async () => {
    const d = await open("22");
    expect(labels(d)).not.toContain("DISTANCE");
    expect(d.textContent).not.toContain("0.0 km");
  });

  it("(w6) CLOSE and Escape remove the overlay (guard)", async () => {
    const d = await open("1");
    fireEvent.click(within(d).getByRole("button", { name: "CLOSE" }));
    expect(document.querySelector(".spov")).toBeNull();
    fireEvent.click(document.querySelector('tr[data-act="1"]') as HTMLElement);
    await waitFor(() => expect(document.querySelector(".spov")).not.toBeNull());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector(".spov")).toBeNull();
  });
});

