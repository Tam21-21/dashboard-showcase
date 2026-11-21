"""UI22: GET /api/life/garmin/days returns the cached snapshots of the last n calendar days."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import partial

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import garmin, userdb
from dashboard.backend.routes import life


_REAL_PARIS_TODAY = getattr(garmin, "paris_today", None)


class FakeDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 12)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "user.db"
    userdb.init_db(db)
    real_open = userdb.open_rw
    monkeypatch.setattr(garmin.userdb, "open_rw", partial(real_open, db))
    monkeypatch.setattr(garmin, "paris_today", lambda: FakeDate.today())
    conn = real_open(db)
    d = date(2026, 9, 10)
    while d <= date(2026, 10, 13):
        if d != date(2026, 10, 1):
            conn.execute(
                "INSERT INTO garmin_snapshots (date, sleep_duration_sec) VALUES (?, ?)",
                (d.isoformat(), 25200),
            )
        d += timedelta(days=1)
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(life.router)
    return TestClient(app)


def test_28_days_oldest_first_without_invented_rows(client, monkeypatch):
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    r = client.get("/api/life/garmin/days?n=28")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    dates = [s["date"] for s in body["snapshots"]]
    assert len(dates) == 27
    assert dates[0] == "2026-09-15"
    assert dates[-1] == "2026-10-12"
    assert dates == sorted(dates)
    assert "2026-10-01" not in dates
    assert "2026-10-13" not in dates


@pytest.mark.parametrize("n", [0, 91])
def test_n_out_of_range_is_422(client, n):
    assert client.get(f"/api/life/garmin/days?n={n}").status_code == 422


def test_unconfigured_still_returns_cached_rows(client, monkeypatch):
    monkeypatch.setattr(garmin, "is_configured", lambda: False)
    body = client.get("/api/life/garmin/days?n=28").json()
    assert body["configured"] is False
    assert len(body["snapshots"]) == 27


class _FrozenDatetime(datetime):
    """A clock frozen at 2026-11-02 23:30 UTC, which is 00:30 on 3 Nov in Paris."""

    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)
        return base.astimezone(tz) if tz else base


def test_g1_days_route_uses_the_paris_date(client, monkeypatch):
    """g1: at 23:30 UTC the Paris day is already 3 Nov, so n=1 asks the store for 2026-11-03."""
    monkeypatch.setattr(garmin, "paris_today", _REAL_PARIS_TODAY or (lambda: date.today()))
    monkeypatch.setattr(garmin, "datetime", _FrozenDatetime)
    asked = []
    monkeypatch.setattr(garmin, "get_snapshots_since", lambda a, b: asked.append((a, b)) or [])
    r = client.get("/api/life/garmin/days?n=1")
    assert r.status_code == 200
    assert asked == [("2026-11-03", "2026-11-03")]


def test_g2_paris_today_in_summer(monkeypatch):
    """g2: 22:30 UTC on 1 Jul is 00:30 on 2 Jul in Paris (summer time, UTC+2)."""

    class Summer(_FrozenDatetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 1, 22, 30, tzinfo=timezone.utc)
            return base.astimezone(tz) if tz else base

    monkeypatch.setattr(garmin, "datetime", Summer)
    assert garmin.paris_today() == date(2026, 7, 2)
