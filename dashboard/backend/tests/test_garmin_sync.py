"""UI26: the Garmin sync stores the Sports-page fields, fills missed days and never loses a stored value.

A temp database and a fake Garmin client built from tests/fixtures/garmin_2026-10-11.json are used.
Nothing here reaches Garmin or the production database.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import garmin, userdb
from dashboard.backend.routes import life

FIXTURE = Path(__file__).parent / "fixtures" / "garmin_2026-10-11.json"
REAL_OPEN = userdb.open_rw
SYNC_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync-garmin.py"


class FakeDate(date):
    today_value = date(2026, 10, 13)

    @classmethod
    def today(cls):
        return cls.today_value


class FakeDatetime(datetime):
    """The sync script's clock: utcnow() answers a fixed moment (the sync fixture sets it)."""

    now_value = datetime(2026, 10, 13, 12, 0, 0)

    @classmethod
    def utcnow(cls):
        return cls.now_value


class FakeClient:
    """Answers each method from the fixture; a method named in `raising` raises."""

    def __init__(self, answers: dict, raising: set[str] | None = None):
        self.answers = copy.deepcopy(answers)
        self.raising = raising or set()
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if not name.startswith("get_"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append(name)
            if name in self.raising:
                raise RuntimeError(f"{name} boom")
            return copy.deepcopy(self.answers[name])

        return method


@pytest.fixture
def answers():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "user.db"
    userdb.init_db(path)
    monkeypatch.setattr(userdb, "open_rw", partial(REAL_OPEN, path))
    monkeypatch.setattr(garmin, "paris_today", lambda: FakeDate.today_value)
    FakeDate.today_value = date(2026, 10, 13)
    return path


def _row(db_path, day):
    conn = REAL_OPEN(db_path)
    try:
        r = conn.execute("SELECT * FROM garmin_snapshots WHERE date = ?", (day,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def test_a_every_field_is_stored(db, answers, monkeypatch):
    FakeDate.today_value = date(2026, 10, 11)
    garmin.fetch_and_store("2026-10-11", client=FakeClient(answers))
    r = _row(db, "2026-10-11")
    expect = {
        "body_battery_pct": 21, "body_battery_high": 75, "body_battery_low": 10, "body_battery_at_wake": 73,
        "readiness": 58, "readiness_level": "MODERATE", "acute_load": 258, "steps_goal": 14540,
        "hrv_overnight": 81, "hrv_status": "BALANCED", "hrv_balanced_low": 72, "hrv_balanced_high": 97,
        "sleep_awake_sec": 300, "sleep_start_local": "2026-10-10T22:30", "sleep_end_local": "2026-10-11T06:55",
        "sleep_score_word": "FAIR", "sleep_need_min": 480, "training_status": "PRODUCTIVE",
        "endurance_score": 6658, "race_5k_sec": 1271, "race_10k_sec": 2776, "race_half_sec": 6364,
        "race_marathon_sec": 14326, "vo2max_garmin": 57.0, "fitness_age": 18.0, "hill_score": 34,
        "lactate_threshold_hr": 186, "threshold_power_w": 318, "intensity_goal": 150,
    }
    for k, v in expect.items():
        assert r[k] == v, k
    assert r["fetch_error"] is None
    factors = json.loads(r["readiness_factors_json"])
    assert [f["name"] for f in factors] == ["sleep", "recovery time", "load", "HRV", "stress history"]
    assert [f["percent"] for f in factors] == [80, 90, 55, 70, 65]
    assert factors[1]["feedback"] == "VERY_GOOD"
    focus = json.loads(r["load_focus_json"])
    assert focus == [
        {"name": "Low aerobic", "value": 806, "target_min": 433, "target_max": 952},
        {"name": "High aerobic", "value": 457, "target_min": 519, "target_max": 1039},
        {"name": "Anaerobic", "value": 373, "target_min": 173, "target_max": 519},
    ]
    limits = json.loads(r["endurance_limits_json"])
    assert limits["gauge_lower"] == 3570 and limits["gauge_upper"] == 10560
    assert len(limits["classes"]) == 7
    assert limits["classes"][0] == {"name": "Recreational", "lower": 3570}
    series = json.loads(r["body_battery_series_json"])
    assert series == [[1760140800000, 40], [1760144400000, 55]]


def test_b_morning_call_decides_readiness(db, answers):
    garmin.fetch_and_store("2026-10-11", client=FakeClient(answers))
    assert _row(db, "2026-10-11")["readiness"] == 58


def test_c_failed_call_names_itself_and_keeps_stored_value(db, answers):
    garmin.fetch_and_store("2026-10-11", client=FakeClient(answers))
    bad = FakeClient(answers, raising={"get_hrv_data"})
    garmin.fetch_and_store("2026-10-11", client=bad)
    r = _row(db, "2026-10-11")
    assert "hrv" in r["fetch_error"]
    assert r["hrv_overnight"] == 81 and r["hrv_status"] == "BALANCED"
    assert r["steps_goal"] == 14540


def test_c_first_fetch_with_a_failed_call_stores_the_rest(db, answers):
    garmin.fetch_and_store("2026-10-11", client=FakeClient(answers, raising={"get_sleep_data"}))
    r = _row(db, "2026-10-11")
    assert "sleep" in r["fetch_error"]
    assert r["sleep_duration_sec"] is None
    assert r["body_battery_pct"] == 21


def test_c_login_failure_writes_nothing_and_raises(db, monkeypatch):
    monkeypatch.setattr(garmin, "_login", lambda: None)
    with pytest.raises(garmin.GarminLoginError):
        garmin.fetch_and_store("2026-10-11")
    assert _row(db, "2026-10-11") is None


def test_c_past_day_vo2max_uses_activities_on_or_before_that_day(db, answers):
    conn = REAL_OPEN(db)
    for aid, start, v in [("1", "2026-10-05 07:00:00", 55.0), ("2", "2026-10-12 07:00:00", 57.0)]:
        conn.execute(
            "INSERT INTO garmin_activities (activity_id, start_time_local, vo2max, fetched_at) VALUES (?,?,?,datetime('now'))",
            (aid, start, v),
        )
    conn.commit()
    conn.close()
    garmin.fetch_and_store("2026-10-08", client=FakeClient(answers))
    r = _row(db, "2026-10-08")
    assert r["vo2max"] == 55.0 and r["vo2max_as_of"] == "2026-10-05"


def _row_as_sqlite(db_path, day):
    conn = REAL_OPEN(db_path)
    try:
        return conn.execute("SELECT * FROM garmin_snapshots WHERE date = ?", (day,)).fetchone()
    finally:
        conn.close()


def _load_sync_script():
    spec = importlib.util.spec_from_file_location("sync_garmin_script", SYNC_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_rows(db_path):
    conn = REAL_OPEN(db_path)
    empty = {"2026-10-02", "2026-10-03", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12"}
    d = date(2026, 9, 14)
    while d <= date(2026, 10, 13):
        iso = d.isoformat()
        if iso == "2026-09-20":
            conn.execute(
                "INSERT INTO garmin_snapshots (date, steps, fetched_at) VALUES (?, 0, ?)",
                (iso, "2026-09-22T12:00:00"),
            )
        elif iso in empty:
            conn.execute(
                "INSERT INTO garmin_snapshots (date, steps, fetched_at) VALUES (?, 0, ?)",
                (iso, f"{iso}T23:00:00"),
            )
        else:
            conn.execute(
                "INSERT INTO garmin_snapshots (date, steps, sleep_duration_sec, fetched_at) VALUES (?, 9000, 25000, ?)",
                # a final read: noon two days after its day (for 2026-10-11 that is the fixture's now)
                (iso, (d + timedelta(days=2)).isoformat() + "T12:00:00"),
            )
        d += timedelta(days=1)
    conn.commit()
    conn.close()


@pytest.fixture
def sync(db, monkeypatch):
    mod = _load_sync_script()
    log = {"logins": 0, "days": [], "activities": [], "clients": []}
    fake = object()

    def login():
        log["logins"] += 1
        return fake

    def fetch_and_store(target_date=None, client=None):
        log["days"].append(target_date)
        log["clients"].append(client)
        return {"date": target_date, "fetch_error": None}

    def fetch_recent_activities(limit=10, client=None):
        log["activities"].append(limit)
        log["clients"].append(client)
        return []

    monkeypatch.setattr(mod, "datetime", FakeDatetime)
    FakeDatetime.now_value = datetime(2026, 10, 13, 12, 0, 0)
    monkeypatch.setattr(userdb, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(garmin, "_login", login)
    monkeypatch.setattr(garmin, "fetch_and_store", fetch_and_store)
    monkeypatch.setattr(garmin, "fetch_recent_activities", fetch_recent_activities)
    log["fake"] = fake
    return mod, log


def test_d_sync_fills_up_to_five_missing_days_oldest_first(db, sync):
    mod, log = sync
    _seed_rows(db)
    mod.main([])
    assert log["logins"] == 1
    # 2026-09-20 was last fetched 2026-09-22T12:00, more than 24 hours before now, so it is retried.
    assert log["days"] == ["2026-10-13", "2026-09-20", "2026-10-02", "2026-10-03", "2026-10-06", "2026-10-07"]
    assert all(c is log["fake"] for c in log["clients"])


def test_d_empty_day_fetched_within_24_hours_waits(db, sync):
    mod, log = sync
    _seed_rows(db)
    conn = REAL_OPEN(db)
    conn.execute("UPDATE garmin_snapshots SET fetched_at = ? WHERE date = ?", ("2026-10-12T20:00:00", "2026-09-20"))
    conn.commit()
    conn.close()
    mod.main([])
    assert "2026-09-20" not in log["days"]
    assert log["days"] == ["2026-10-13", "2026-10-02", "2026-10-03", "2026-10-06", "2026-10-07", "2026-10-08"]


def test_d_full_day_read_two_days_later_is_final(db, sync):
    mod, log = sync
    _seed_rows(db)
    conn = REAL_OPEN(db)
    conn.execute(
        "UPDATE garmin_snapshots SET steps = 9000, sleep_duration_sec = 25000, fetched_at = ? WHERE date = ?",
        ("2026-10-03T12:00:00", "2026-09-25"),
    )
    conn.commit()
    conn.close()
    assert not mod._is_empty(_row_as_sqlite(db, "2026-09-25"))
    mod.main([])
    assert "2026-09-25" not in log["days"]


def test_d_backfill_days_refetches_every_day(db, sync):
    mod, log = sync
    _seed_rows(db)
    mod.main(["--backfill-days", "30"])
    assert len(log["days"]) == 30
    assert log["days"][0] == "2026-10-13"
    assert sorted(log["days"]) == [(date(2026, 9, 14) + timedelta(days=i)).isoformat() for i in range(30)]


def test_e_activities_default_and_flag(db, sync):
    mod, log = sync
    mod.main([])
    assert log["activities"] == [20]
    mod.main(["--activities", "60"])
    assert log["activities"] == [20, 60]


def test_f_days_route_returns_new_columns(db, answers, monkeypatch):
    _set_calories(answers)
    garmin.fetch_and_store("2026-10-12", client=FakeClient(answers))
    monkeypatch.setattr(garmin, "paris_today", lambda: FakeDate.today_value)
    app = FastAPI()
    app.include_router(life.router)
    r = TestClient(app).get("/api/life/garmin/days?n=3")
    assert r.status_code == 200
    snap = r.json()["snapshots"][-1]
    assert snap["body_battery_high"] == 75
    assert snap["readiness_level"] == "MODERATE"
    assert snap["training_status"] == "PRODUCTIVE"
    assert snap["active_calories"] == 637 and snap["resting_calories"] == 1677


def test_g_refresh_with_failed_login_answers_502_and_writes_no_row(db, monkeypatch):
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    monkeypatch.setattr(garmin, "_login", lambda: None)
    app = FastAPI()
    app.include_router(life.router)
    r = TestClient(app).post("/api/life/garmin/refresh")
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Garmin login failed: ")
    conn = REAL_OPEN(db)
    assert conn.execute("SELECT COUNT(*) FROM garmin_snapshots").fetchone()[0] == 0
    conn.close()


def test_training_status_without_primary_device_is_named_in_fetch_error(db, answers):
    for e in answers["get_training_status"]["mostRecentTrainingStatus"]["latestTrainingStatusData"].values():
        e["primaryTrainingDevice"] = False
    garmin.fetch_and_store("2026-10-11", client=FakeClient(answers))
    r = _row(db, "2026-10-11")
    assert "no primary training device" in r["fetch_error"]
    assert r["training_status"] is None and r["load_focus_json"] is None


def _set_calories(answers, total=2314.0, active=637.0, bmr=1677.0):
    answers["get_stats"]["totalKilocalories"] = total
    answers["get_stats"]["activeKilocalories"] = active
    answers["get_stats"]["bmrKilocalories"] = bmr


def test_k1_calories_stored_in_parts(db, answers):
    _set_calories(answers)
    garmin.fetch_and_store("2026-10-12", client=FakeClient(answers))
    r = _row(db, "2026-10-12")
    assert (r["total_calories"], r["active_calories"], r["resting_calories"]) == (2314, 637, 1677)


def test_k2_total_is_null_when_the_watch_sent_no_total(db, answers):
    _set_calories(answers, total=None, active=637.0, bmr=None)
    garmin.fetch_and_store("2026-10-12", client=FakeClient(answers))
    r = _row(db, "2026-10-12")
    assert r["total_calories"] is None
    assert r["active_calories"] == 637 and r["resting_calories"] is None


def test_k3_backfill_from_raw_json_once(tmp_path):
    path = tmp_path / "k3.db"
    userdb.init_db(path)
    conn = REAL_OPEN(path)
    raw = '{"stats": {"totalKilocalories": 2013.0, "activeKilocalories": 458.0, "bmrKilocalories": 1555.0}}'
    conn.execute("INSERT INTO garmin_snapshots (date, raw_json, fetched_at) VALUES ('2026-11-14', ?, 'x')", (raw,))
    conn.execute("INSERT INTO garmin_snapshots (date, raw_json, fetched_at) VALUES ('2026-11-13', '{}', 'x')")
    conn.commit()
    conn.close()

    def snapshot():
        c = REAL_OPEN(path)
        try:
            return [tuple(r) for r in c.execute(
                "SELECT date, total_calories, active_calories, resting_calories FROM garmin_snapshots ORDER BY date")]
        finally:
            c.close()

    userdb.init_db(path)
    after = snapshot()
    assert after == [("2026-11-13", None, None, None), ("2026-11-14", None, 458, 1555)]
    userdb.init_db(path)
    assert snapshot() == after


def _due_fixture(db, overrides):
    conn = REAL_OPEN(db)
    d = date(2026, 10, 20)
    while d <= date(2026, 11, 17):
        iso = d.isoformat()
        conn.execute(
            "INSERT INTO garmin_snapshots (date, steps, sleep_duration_sec, fetched_at) VALUES (?, 9000, 25200, ?)",
            (iso, overrides.get(iso, "2026-11-18T13:00:00")),
        )
        d += timedelta(days=1)
    conn.commit()
    conn.close()


def test_k4_unfinished_day_is_read_again_at_most_every_six_hours(db):
    mod = _load_sync_script()
    _due_fixture(db, {
        "2026-11-15": "2026-11-15T22:00:09",
        "2026-11-12": "2026-11-15T07:00:14",
        "2026-11-17": "2026-11-18T10:00:00",
    })
    today = date(2026, 11, 18)
    assert mod.days_to_fetch(today, 0, datetime(2026, 11, 18, 14, 0, 0)) == ["2026-11-18", "2026-11-15"]
    assert mod.days_to_fetch(today, 0, datetime(2026, 11, 18, 16, 0, 1)) == ["2026-11-18", "2026-11-15", "2026-11-17"]


def test_k5_five_past_days_a_run_oldest_first(db):
    mod = _load_sync_script()
    _due_fixture(db, {f"2026-11-{n:02d}": f"2026-11-{n:02d}T22:00:00" for n in range(1, 13)})
    got = mod.days_to_fetch(date(2026, 11, 18), 0, datetime(2026, 11, 18, 14, 0, 0))
    assert got == ["2026-11-18"] + [f"2026-11-0{n}" for n in range(1, 6)]
