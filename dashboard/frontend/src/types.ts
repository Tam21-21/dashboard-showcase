// Life / Calendar / Weather / Garmin / News types only.
// v2 (newsbot) types were removed when the Research + Trading tabs were retired.

export type Expense = {
  id: number;
  date: string;
  label: string;
  amount: number;
  category: string;
  created_at: string;
};

export type BudgetConfig = {
  id: 1;
  monthly_budget: number;
  fixed_expenses: number;
  /** Null = total this-month spent is computed from the expenses list.
   *  A number = override the computed total with this value. */
  manual_monthly_spent: number | null;
  updated_at: string;
};

export type Todo = {
  id: number;
  text: string;
  done: 0 | 1;
  position: number;
  date: string;
  done_at: string | null;
  created_at: string;
  updated_at: string;
};

export type WeatherForecastDay = {
  date: string;
  temp_max: number;
  temp_min: number;
  description: string;
  icon: string;
  wind_kph: number;
  humidity: number;
  rain_chance: number;
};

export type WeatherData = {
  temp: number;
  feels_like: number;
  temp_min: number;
  temp_max: number;
  description: string;
  icon: string;
  humidity: number;
  wind_kph: number;
  city: string;
};

export type CalendarEvent = {
  event_id: string;
  calendar_id: string;
  title: string;
  start_dt: string;
  end_dt: string;
  all_day: 0 | 1;
  location: string | null;
  description: string | null;
  color_id: string | null;
  html_link: string | null;
  synced_at: string;
  local_only: 0 | 1;
};

export type EventTemplate = {
  id: number;
  title: string;
  color: string;
  created_at: string;
};

export type GarminSnapshot = {
  date: string;
  sleep_score: number | null;
  sleep_duration_sec: number | null;
  sleep_deep_sec: number | null;
  sleep_light_sec: number | null;
  sleep_rem_sec: number | null;
  body_battery_pct: number | null;
  resting_hr: number | null;
  steps: number | null;
  readiness: number | null;
  stress: number | null;
  total_calories: number | null;
  active_calories: number | null;
  resting_calories: number | null;
  hrv_overnight: number | null;
  // P1 enrichment (#151 follow-on): the four omitted HEALTH fields. Intensity is the daily
  // moderate/vigorous minute split; recovery_minutes is the daily recovery-time advisor (minutes,
  // rendered as hours); vo2max + vo2max_as_of are the latest activity-derived fitness estimate (no
  // daily series exists); sleep_levels_json is the real per-segment hypnogram timeline.
  intensity_moderate_min: number | null;
  intensity_vigorous_min: number | null;
  recovery_minutes: number | null;
  vo2max: number | null;
  vo2max_as_of: string | null;
  sleep_levels_json: string | null;
  // UI26: the fields the Sports page draws. The *_json columns hold JSON text.
  body_battery_high: number | null;
  body_battery_low: number | null;
  body_battery_at_wake: number | null;
  acute_load: number | null;
  steps_goal: number | null;
  hrv_balanced_low: number | null;
  hrv_balanced_high: number | null;
  sleep_awake_sec: number | null;
  sleep_need_min: number | null;
  endurance_score: number | null;
  race_5k_sec: number | null;
  race_10k_sec: number | null;
  race_half_sec: number | null;
  race_marathon_sec: number | null;
  hill_score: number | null;
  lactate_threshold_hr: number | null;
  threshold_power_w: number | null;
  intensity_goal: number | null;
  vo2max_garmin: number | null;
  fitness_age: number | null;
  readiness_level: string | null;
  hrv_status: string | null;
  sleep_score_word: string | null;
  training_status: string | null;
  sleep_start_local: string | null;
  sleep_end_local: string | null;
  readiness_factors_json: string | null;
  load_focus_json: string | null;
  endurance_limits_json: string | null;
  body_battery_series_json: string | null;
  raw_json: string | null;
  fetch_error: string | null;
  fetched_at: string;
};

// One segment of the Garmin sleep hypnogram (raw sleepLevels). activityLevel maps
// {0:deep, 1:light, 2:rem, 3:awake} — verified against the DTO stage-duration totals.
export type SleepLevelSegment = {
  startGMT: string;
  endGMT: string;
  activityLevel: number;
};

export type GarminActivity = {
  activity_id: string;
  start_time_local: string;
  start_time_gmt: string | null;
  type_key: string | null;
  parent_type_key: string | null;
  name: string | null;
  duration_sec: number | null;
  elapsed_sec: number | null;
  moving_sec: number | null;
  distance_m: number | null;
  avg_hr: number | null;
  max_hr: number | null;
  total_calories: number | null;
  active_calories: number | null;
  avg_speed_mps: number | null;
  max_speed_mps: number | null;
  avg_pace_min_per_km: number | null;
  elevation_gain_m: number | null;
  elevation_loss_m: number | null;
  min_elevation_m: number | null;
  max_elevation_m: number | null;
  avg_cadence: number | null;
  max_cadence: number | null;
  avg_power: number | null;
  max_power: number | null;
  aerobic_te: number | null;
  anaerobic_te: number | null;
  training_load: number | null;
  vo2max: number | null;
  avg_stride_m: number | null;
  avg_ground_contact_ms: number | null;
  avg_vertical_osc_cm: number | null;
  avg_vertical_ratio: number | null;
  avg_respiration_rpm: number | null;
  avg_temp_c: number | null;
  fetched_at: string;
};

export type GarminActivitySample = {
  directTimestamp?: number;
  directHeartRate?: number;
  directSpeed?: number;
  directElevation?: number;
  directRunCadence?: number;
  directBikeCadence?: number;
  directPower?: number;
  directTemperature?: number;
  directRespirationRate?: number;
  directStrideLength?: number;
  directGroundContactTime?: number;
  directVerticalOscillation?: number;
  directVerticalRatio?: number;
  directLatitude?: number;
  directLongitude?: number;
};

export type GarminActivitySplit = {
  lap: number | null;
  distance_m: number | null;
  duration_sec: number | null;
  avg_hr: number | null;
  max_hr: number | null;
  avg_speed_mps: number | null;
  pace_min_per_km: number | null;
  elevation_gain_m: number | null;
  calories: number | null;
};

export type GarminHrZone = {
  zone: number | null;
  low_bpm: number | null;
  high_bpm: number | null;
  seconds: number | null;
};

export type GarminActivityWeather = {
  temp_c: number | null;
  feels_like_c: number | null;
  humidity: number | null;
  wind_kph: number | null;
  wind_dir: string | null;
  conditions: string | null;
  dew_point_c: number | null;
};

export type GarminExerciseSet = {
  name: string | null;
  set_type: string | null;
  reps: number | null;
  weight_kg: number | null;
  duration_sec: number | null;
  rest_sec: number | null;
};

export type GarminGear = {
  uuid: string | null;
  name: string | null;
  type: string | null;
};

export type GarminActivityDetail = {
  activity_id: string;
  samples: GarminActivitySample[];
  polyline: { lat: number; lon: number }[];
  splits: GarminActivitySplit[];
  hr_zones: GarminHrZone[];
  weather?: GarminActivityWeather;
  exercise_sets: GarminExerciseSet[];
  gear: GarminGear[];
  summary_extras: {
    recovery_hours: number | null;
    training_effect_msg: string | null;
    device_name: string | null;
  };
};

export type GarminActivityBundle = {
  summary: GarminActivity | null;
  detail: GarminActivityDetail;
};

export type NewsItem = {
  title: string;
  url: string;
  published: string;
  source: string;
};

export type EmailSummary = {
  id: string;
  thread_id: string;
  from_name: string;
  from_email: string;
  subject: string;
  snippet: string;
  date_iso: string;
  is_unread: boolean;
};

export type EmailFull = {
  id: string;
  thread_id: string;
  from_name: string;
  from_email: string;
  to: string;
  subject: string;
  date_iso: string;
  html_safe: string;
  has_images_blocked: boolean;
};

export type EmailStatus = {
  configured: boolean;
  connected: boolean;
  can_modify: boolean;
};

// The evening Summary, as GET /api/life/summary serves it (lane ES1a): one stored row per day, with its day and made_at.
export type EveningDay = { d: string; steps: number | null; rest: number | null; move: number | null };
export type EveningBody = {
  days: EveningDay[]; steps: number; goal: number; reached: boolean; short: number; share: number; prev: number | null;
  avg_steps: number | null; avg_kcal: number | null; kcal: number; rest: number; move: number;
};
export type EveningMoney = {
  today: number; in: number; budget: number; before: number; spent: number; left: number; share: number; gap: number;
  under: boolean; pace: number; st: "ok" | "ahead" | "over"; since: string | null; read_at: string; bank_as_of: string;
};
export type EveningEvent = { title: string; all_day: boolean; start: string; end: string };
export type EveningPlan = { events: EveningEvent[]; first: string | null; todos: string[]; late: string[] };
export type EveningSummary = {
  day: string; made_at: string; goal: number; body: EveningBody | null; watch_last: string | null;
  money: EveningMoney | null; plan: EveningPlan; verdict: { budget: boolean | null; steps: boolean | null };
  round: { today: number; month: number; total: number; days: number[] } | null;
};

/** GET /api/life/summary/settings: the step goal, and the monthly budget (null when none is stored). */
export type SummarySettings = { steps_goal: number; monthly_budget: number | null };
/** POST /api/life/push/subscribe: what a phone's push subscription holds. */
export type PushSubscribeBody = { endpoint: string; keys: { p256dh: string; auth: string } };
/** POST /api/life/push/test: how many phones took the test, were gone, or failed. */
export type PushSent = { sent: number; gone: number; failed: number };
