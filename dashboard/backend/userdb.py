"""Read-write SQLite connection to dashboard.db — user-owned data.

Separate from bot/newsbot.db (read-only). This file is the sole writer.
WAL mode is set on first connect and persists.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import USER_DB_PATH

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS expenses (
    id         INTEGER PRIMARY KEY,
    date       TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    amount     REAL    NOT NULL,
    category   TEXT    NOT NULL DEFAULT 'other',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date);

CREATE TABLE IF NOT EXISTS budget_config (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    monthly_budget        REAL    NOT NULL DEFAULT 2000,
    fixed_expenses        REAL    NOT NULL DEFAULT 760,
    -- Optional override for "this month spent". If NULL, the dashboard sums
    -- the expenses table. If set, the override replaces the computed total
    -- (use when the user just wants to type the running total directly).
    manual_monthly_spent  REAL,
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO budget_config (id, monthly_budget, fixed_expenses)
    VALUES (1, 2000, 760);

CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY,
    text       TEXT    NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0,
    date       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_todos_date ON todos(date, position);

CREATE TABLE IF NOT EXISTS user_prefs (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS garmin_snapshots (
    date               TEXT PRIMARY KEY,
    sleep_score        INTEGER,
    sleep_duration_sec INTEGER,
    sleep_deep_sec     INTEGER,
    sleep_light_sec    INTEGER,
    sleep_rem_sec      INTEGER,
    body_battery_pct   INTEGER,
    resting_hr         INTEGER,
    steps              INTEGER,
    readiness          INTEGER,
    stress             INTEGER,
    total_calories     INTEGER,
    hrv_overnight      INTEGER,
    raw_json           TEXT,
    fetch_error        TEXT,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS garmin_activities (
    activity_id          TEXT PRIMARY KEY,
    start_time_local     TEXT NOT NULL,
    start_time_gmt       TEXT,
    type_key             TEXT,
    parent_type_key      TEXT,
    name                 TEXT,
    duration_sec         REAL,
    elapsed_sec          REAL,
    moving_sec           REAL,
    distance_m           REAL,
    avg_hr               INTEGER,
    max_hr               INTEGER,
    total_calories       INTEGER,
    active_calories      INTEGER,
    avg_speed_mps        REAL,
    max_speed_mps        REAL,
    avg_pace_min_per_km  REAL,
    elevation_gain_m     REAL,
    elevation_loss_m     REAL,
    min_elevation_m      REAL,
    max_elevation_m      REAL,
    avg_cadence          REAL,
    max_cadence          REAL,
    avg_power            REAL,
    max_power            REAL,
    aerobic_te           REAL,
    anaerobic_te         REAL,
    training_load        REAL,
    vo2max               REAL,
    avg_stride_m         REAL,
    avg_ground_contact_ms REAL,
    avg_vertical_osc_cm  REAL,
    avg_vertical_ratio   REAL,
    avg_respiration_rpm  REAL,
    avg_temp_c           REAL,
    summary_json         TEXT,
    fetched_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_garmin_activities_start
    ON garmin_activities(start_time_local DESC);

CREATE TABLE IF NOT EXISTS garmin_activity_details (
    activity_id        TEXT PRIMARY KEY,
    detail_json        TEXT,
    splits_json        TEXT,
    hr_zones_json      TEXT,
    weather_json       TEXT,
    exercise_sets_json TEXT,
    gear_json          TEXT,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (activity_id) REFERENCES garmin_activities (activity_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS calendar_events (
    event_id    TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL DEFAULT 'primary',
    title       TEXT NOT NULL DEFAULT '',
    start_dt    TEXT NOT NULL,
    end_dt      TEXT NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    location    TEXT,
    description TEXT,
    color_id    TEXT,
    html_link   TEXT,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now')),
    local_only  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calendar_start ON calendar_events(start_dt);

CREATE TABLE IF NOT EXISTS event_templates (
    id         INTEGER PRIMARY KEY,
    title      TEXT    NOT NULL,
    color      TEXT    NOT NULL DEFAULT '#5AA2FF',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Lane MY1: the money store (what Finary reports, plus the budget targets Tamer sets).
-- DDIA check (docs/books/kleppmann_canon.md, chapter 1 "Systems of record and derived data",
-- chapter 3 "Event sourcing and CQRS"): Finary is the system of record. Each sync is stored whole
-- and never edited, an append-only log of snapshots keyed by sync_id; the Money and Budget pages
-- read views derived from it (the newest snapshot, and the net-worth series over read_at).
-- budget_targets is the dashboard's own record: Tamer sets it, Finary never sees it.
-- Growth: about 100 rows a sync, two syncs a day, about 73,000 rows a year; no pruning.
CREATE TABLE IF NOT EXISTS money_syncs (
    sync_id                 INTEGER PRIMARY KEY,
    batch_id                TEXT NOT NULL UNIQUE,
    read_at                 TEXT NOT NULL,
    received_at             TEXT NOT NULL,
    currency                TEXT NOT NULL,
    net_worth               REAL NOT NULL,
    gross_assets            REAL NOT NULL,
    liabilities             REAL NOT NULL,
    financial_assets        REAL NOT NULL,
    unvalued_liability_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_money_syncs_read_at ON money_syncs(read_at);

CREATE TABLE IF NOT EXISTS money_allocation (
    sync_id  INTEGER NOT NULL REFERENCES money_syncs(sync_id),
    category TEXT    NOT NULL,
    amount   REAL    NOT NULL,
    PRIMARY KEY (sync_id, category)
);

CREATE TABLE IF NOT EXISTS money_accounts (
    sync_id                 INTEGER NOT NULL REFERENCES money_syncs(sync_id),
    account_id              TEXT    NOT NULL,
    name                    TEXT    NOT NULL,
    account_type            TEXT    NOT NULL,
    currency                TEXT    NOT NULL,
    balance                 REAL    NOT NULL,
    value_eur               REAL    NOT NULL,
    institution             TEXT,
    sync_status             TEXT,
    last_successful_sync_at TEXT,
    is_debt                 INTEGER NOT NULL CHECK (is_debt IN (0, 1)),
    PRIMARY KEY (sync_id, account_id)
);

CREATE TABLE IF NOT EXISTS money_holdings (
    sync_id       INTEGER NOT NULL REFERENCES money_syncs(sync_id),
    account_id    TEXT    NOT NULL,
    row_no        INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    holding_type  TEXT    NOT NULL,
    quantity      REAL,
    current_value REAL    NOT NULL,
    currency      TEXT    NOT NULL,
    buying_value  REAL,
    PRIMARY KEY (sync_id, account_id, row_no)
);

CREATE TABLE IF NOT EXISTS money_budget_periods (
    sync_id                    INTEGER NOT NULL REFERENCES money_syncs(sync_id),
    period                     TEXT    NOT NULL CHECK (period IN ('this_month', 'last_month')),
    from_date                  TEXT    NOT NULL,
    to_date                    TEXT    NOT NULL,
    is_partial                 INTEGER NOT NULL CHECK (is_partial IN (0, 1)),
    elapsed_days               INTEGER NOT NULL,
    total_days                 INTEGER NOT NULL,
    income                     REAL    NOT NULL,
    expenses                   REAL    NOT NULL,
    net_cashflow               REAL    NOT NULL,
    savings_rate_percent       REAL,
    recurring_monthly_equivalent REAL  NOT NULL,
    recurring_count            INTEGER NOT NULL,
    PRIMARY KEY (sync_id, period)
);

CREATE TABLE IF NOT EXISTS money_budget_categories (
    sync_id                  INTEGER NOT NULL,
    period                   TEXT    NOT NULL,
    slug                     TEXT    NOT NULL,
    label                    TEXT    NOT NULL,
    expense                  REAL    NOT NULL,
    income                   REAL    NOT NULL,
    monthly_average_expenses REAL,
    PRIMARY KEY (sync_id, period, slug),
    FOREIGN KEY (sync_id, period) REFERENCES money_budget_periods(sync_id, period)
);

CREATE TABLE IF NOT EXISTS money_months (
    sync_id      INTEGER NOT NULL REFERENCES money_syncs(sync_id),
    month        TEXT    NOT NULL,
    income       REAL    NOT NULL,
    expenses     REAL    NOT NULL,
    net_cashflow REAL    NOT NULL,
    PRIMARY KEY (sync_id, month)
);

CREATE TABLE IF NOT EXISTS money_goals (
    sync_id        INTEGER NOT NULL REFERENCES money_syncs(sync_id),
    goal_id        TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    target_amount  REAL,
    current_amount REAL,
    currency       TEXT,
    status         TEXT,
    target_date    TEXT,
    PRIMARY KEY (sync_id, goal_id)
);

CREATE TABLE IF NOT EXISTS budget_targets (
    slug       TEXT PRIMARY KEY,
    amount_eur REAL NOT NULL CHECK (amount_eur >= 0),
    updated_at TEXT NOT NULL
);

-- Lane ES1a: the Evening Summary. DDIA check (docs/books/kleppmann_canon.md, chapter 1, "Systems of
-- record and derived data"): the stored Summary is derived data, a kept copy of what the source tables
-- said at 22:00, never read back as a source. One row a day (day is the Paris date), read by its key,
-- so the primary key is the only index. body is the JSON of evening_summary.build.
CREATE TABLE IF NOT EXISTS evening_summaries (day TEXT PRIMARY KEY, made_at TEXT NOT NULL, body TEXT NOT NULL);

-- Lane ES1b2: the phones that get the evening notification. DDIA check (docs/books/kleppmann_canon.md,
-- chapter 1, "Systems of record and derived data"): a system of record, one row per phone, keyed by the
-- address the phone itself gave and written only by that phone's request. Read whole at each send, so the
-- primary key is the only index.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint     TEXT PRIMARY KEY,
    p256dh       TEXT NOT NULL,
    auth         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_sent_at TEXT,
    last_error   TEXT
);
"""


def open_rw(path: Path = USER_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = USER_DB_PATH) -> None:
    """Create tables and seed defaults. Called once at startup."""
    conn = open_rw(path)
    try:
        conn.executescript(_DDL)
        # Migrate: add new garmin columns if upgrading from older schema
        for col, typ in [("total_calories", "INTEGER"), ("hrv_overnight", "INTEGER")]:
            try:
                conn.execute(f"ALTER TABLE garmin_snapshots ADD COLUMN {col} {typ}")
                conn.commit()
            except Exception:
                pass  # column already exists
        # Migrate: budget_config gets manual_monthly_spent override
        try:
            conn.execute("ALTER TABLE budget_config ADD COLUMN manual_monthly_spent REAL")
            conn.commit()
        except Exception:
            pass  # already migrated
        # Migrate: add local_only to calendar_events
        try:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass
        # Garmin P1 enrichment: the four omitted HEALTH fields the Life page needs (intensity minutes,
        # daily recovery-time advisor, the as-of VO2max from activities, and the per-segment sleep
        # hypnogram as a JSON document). Added via a PRAGMA presence check, NOT the catch-all ALTER above
        # — so a real ALTER failure (locked DB, bad type) surfaces loudly instead of being swallowed as
        # "already exists". Idempotent: a column already present is simply skipped.
        _add_columns_if_absent(
            conn,
            "garmin_snapshots",
            [
                ("intensity_moderate_min", "INTEGER"),
                ("intensity_vigorous_min", "INTEGER"),
                ("recovery_minutes", "INTEGER"),
                ("vo2max", "REAL"),
                ("vo2max_as_of", "TEXT"),
                ("sleep_levels_json", "TEXT"),
                ("active_calories", "INTEGER"),
                ("resting_calories", "INTEGER"),
            ],
        )
        # UI26: the fields the Sports page draws, each one a field Garmin returned in the live check.
        _add_columns_if_absent(
            conn,
            "garmin_snapshots",
            [(c, "INTEGER") for c in (
                "body_battery_high", "body_battery_low", "body_battery_at_wake", "acute_load",
                "steps_goal", "hrv_balanced_low", "hrv_balanced_high", "sleep_awake_sec",
                "sleep_need_min", "endurance_score", "race_5k_sec", "race_10k_sec",
                "race_half_sec", "race_marathon_sec", "hill_score", "lactate_threshold_hr",
                "threshold_power_w", "intensity_goal",
            )]
            + [("vo2max_garmin", "REAL"), ("fitness_age", "REAL")]
            + [(c, "TEXT") for c in (
                "readiness_level", "hrv_status", "sleep_score_word", "training_status",
                "sleep_start_local", "sleep_end_local", "readiness_factors_json",
                "load_focus_json", "endurance_limits_json", "body_battery_series_json",
            )],
        )
        # TD1: when a to-do was ticked (UTC), so a late item ticked today can stay on today's list.
        _add_columns_if_absent(conn, "todos", [("done_at", "TEXT")])
        _backfill_garmin_enrichment(conn)
        _backfill_garmin_calories(conn)
    finally:
        conn.close()


def _backfill_garmin_calories(conn: sqlite3.Connection) -> None:
    """Fill the two calorie parts of stored days from their raw_json, once (idempotent).

    Only rows where both parts are still NULL are touched; a row whose raw_json does not parse or holds no
    "stats" object is skipped. total_calories is not rewritten.
    """
    import json as _json

    rows = conn.execute(
        "SELECT date, raw_json FROM garmin_snapshots "
        "WHERE raw_json IS NOT NULL AND active_calories IS NULL AND resting_calories IS NULL"
    ).fetchall()
    for row in rows:
        try:
            raw = _json.loads(row["raw_json"])
        except (ValueError, TypeError):
            continue  # a malformed raw_json blob is skipped, as in the enrichment backfill
        stats = raw.get("stats") if isinstance(raw, dict) else None
        if not isinstance(stats, dict):
            continue
        active, resting = stats.get("activeKilocalories"), stats.get("bmrKilocalories")
        conn.execute(
            "UPDATE garmin_snapshots SET active_calories = ?, resting_calories = ? WHERE date = ?",
            (int(active) if active is not None else None, int(resting) if resting is not None else None, row["date"]),
        )
    conn.commit()


def _add_columns_if_absent(conn: sqlite3.Connection, table: str, cols: list[tuple[str, str]]) -> None:
    """Add each (name, type) column to `table` only if it is not already present.

    Uses PRAGMA table_info to test presence first, so the happy path raises no exception and a genuine
    ALTER error is NOT swallowed (unlike the legacy `except Exception: pass` migration idiom).
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, typ in cols:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()


def _backfill_garmin_enrichment(conn: sqlite3.Connection) -> None:
    """One-time idempotent backfill of the P1 enrichment columns from data already in the DB.

    Intensity / recovery / sleep-levels are re-derived from each snapshot's stored `raw_json` (no
    Garmin re-fetch needed for history); the as-of VO2max comes from the latest non-null activity. Only
    rows whose enrichment columns are still NULL are touched, so a re-run after the sync has populated
    them is a no-op.
    """
    import json as _json

    # A row qualifies if ANY enrichment column is still null (OR, not AND) — so a row a prior partial
    # sync left half-filled (e.g. intensity set but sleep_levels_json null) still gets its gaps filled.
    rows = conn.execute(
        "SELECT date, raw_json FROM garmin_snapshots "
        "WHERE raw_json IS NOT NULL AND (intensity_moderate_min IS NULL "
        "OR intensity_vigorous_min IS NULL OR recovery_minutes IS NULL OR sleep_levels_json IS NULL)"
    ).fetchall()
    for row in rows:
        try:
            raw = _json.loads(row["raw_json"]) if row["raw_json"] else {}
        except (ValueError, TypeError):
            continue  # a malformed raw_json blob is skipped, not fatal to the migration
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        readiness = raw.get("readiness")
        if isinstance(readiness, list):
            readiness = readiness[0] if readiness else {}
        if not isinstance(readiness, dict):
            readiness = {}
        sleep = raw.get("sleep") if isinstance(raw.get("sleep"), dict) else {}
        levels = sleep.get("sleepLevels")
        # COALESCE(existing, new): fill only the columns still null, never overwrite a value a real sync
        # already wrote. Re-derivation from raw_json yields the same value anyway, so this is safe + idempotent.
        conn.execute(
            "UPDATE garmin_snapshots SET "
            "intensity_moderate_min = COALESCE(intensity_moderate_min, ?), "
            "intensity_vigorous_min = COALESCE(intensity_vigorous_min, ?), "
            "recovery_minutes = COALESCE(recovery_minutes, ?), "
            "sleep_levels_json = COALESCE(sleep_levels_json, ?) WHERE date = ?",
            (
                stats.get("moderateIntensityMinutes"),
                stats.get("vigorousIntensityMinutes"),
                readiness.get("recoveryTime"),
                _json.dumps(levels) if isinstance(levels, list) and levels else None,
                row["date"],
            ),
        )
    # The as-of VO2max: the latest activity that carries one (VO2max is a slowly-changing fitness
    # estimate, updated only by qualifying activities — there is no daily series). Stamp the same as-of
    # value/date onto every snapshot row missing it, so the Life page reads one row, not a join.
    vo2_row = conn.execute(
        "SELECT vo2max, start_time_local FROM garmin_activities "
        "WHERE vo2max IS NOT NULL ORDER BY start_time_local DESC LIMIT 1"
    ).fetchone()
    if vo2_row is not None:
        as_of = (vo2_row["start_time_local"] or "")[:10]  # the activity date (YYYY-MM-DD)
        conn.execute(
            "UPDATE garmin_snapshots SET vo2max = ?, vo2max_as_of = ? WHERE vo2max IS NULL",
            (vo2_row["vo2max"], as_of),
        )
    conn.commit()
