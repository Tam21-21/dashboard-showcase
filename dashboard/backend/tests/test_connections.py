"""UI23: GET /api/settings/connections and the Google callback's return address."""
from __future__ import annotations

import functools
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import calendar_sync, garmin, userdb
from dashboard.backend.routes import auth, connections

ALPACA_KEY = "alpaca-key-value-111"
ALPACA_SECRET = "alpaca-secret-value-222"
EODHD_KEY = "eodhd-key-value-333"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(connections.router)
    app.include_router(auth.router)
    return TestClient(app)


def _env(monkeypatch, alpaca_key=ALPACA_KEY, alpaca_secret=ALPACA_SECRET, eodhd=None):
    for name, value in (
        ("ALPACA_API_KEY", alpaca_key),
        ("ALPACA_SECRET_KEY", alpaca_secret),
        ("EODHD_API_KEY", eodhd),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_connections_flags(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    r = _client().get("/api/settings/connections")
    assert r.status_code == 200
    assert r.json() == {"alpaca": True, "eodhd": False, "garmin": True}


def test_missing_or_empty_alpaca_key_is_false(monkeypatch):
    monkeypatch.setattr(garmin, "is_configured", lambda: False)
    _env(monkeypatch, alpaca_secret=None, eodhd=EODHD_KEY)
    assert _client().get("/api/settings/connections").json() == {"alpaca": False, "eodhd": True, "garmin": False}
    _env(monkeypatch, alpaca_key="", eodhd="")
    assert _client().get("/api/settings/connections").json() == {"alpaca": False, "eodhd": False, "garmin": False}


def test_response_holds_no_key_value(monkeypatch):
    _env(monkeypatch, eodhd=EODHD_KEY)
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    text = _client().get("/api/settings/connections").text
    for secret in (ALPACA_KEY, ALPACA_SECRET, EODHD_KEY):
        assert secret not in text


def test_google_callback_lands_on_settings(monkeypatch):
    monkeypatch.setattr(calendar_sync, "exchange_and_store", lambda code, state: None)
    r = _client().get("/api/auth/google/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/settings"


# ── UI32-0: GET /api/settings/connections/live ───────────────────────────────

PAPER_KEY = "PK-paper-key-value-444"
PAPER_HOST = "https://paper-api.alpaca.markets/v2/account"
EODHD_URL = "https://eodhd.com/api/user"
GARMIN_ERROR = "training_status: no primary training device in the answer"


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _Net:
    """Stands in for httpx.get: answers by host, records every call."""

    def __init__(self, alpaca=None, eodhd=None):
        self.alpaca = alpaca
        self.eodhd = eodhd
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.alpaca if url.startswith("https://paper-api.alpaca.markets") else self.eodhd
        if isinstance(answer, Exception):
            raise answer
        assert answer is not None, f"unexpected call to {url}"
        return answer


@pytest.fixture(autouse=True)
def _fresh_cache():
    connections._CACHE.clear()
    yield
    connections._CACHE.clear()


@pytest.fixture
def user_db(tmp_path, monkeypatch):
    path = tmp_path / "dashboard.db"
    userdb.init_db(path)
    monkeypatch.setattr(userdb, "open_rw", functools.partial(userdb.open_rw, path))
    return path


def _net(monkeypatch, **kw):
    net = _Net(**kw)
    monkeypatch.setattr(connections.httpx, "get", net)
    return net


def _live():
    r = _client().get("/api/settings/connections/live")
    assert r.status_code == 200
    return r


def _insert(path, date, steps, fetched_at, error=None, sleep=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO garmin_snapshots (date, steps, sleep_duration_sec, fetch_error, fetched_at) VALUES (?,?,?,?,?)",
        (date, steps, sleep, error, fetched_at),
    )
    conn.commit()
    conn.close()


def _good_net(monkeypatch):
    return _net(
        monkeypatch,
        alpaca=_Resp(200, {"status": "ACTIVE", "trading_blocked": False}),
        eodhd=_Resp(200, {"apiRequests": 39, "dailyRateLimit": 100000, "apiRequestsDate": "2026-10-16"}),
    )


def test_c1_garmin_rows(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=PAPER_KEY, eodhd=EODHD_KEY)
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    _good_net(monkeypatch)
    _insert(user_db, "2026-10-14", 19227, "2026-10-14T23:00:11")
    _insert(user_db, "2026-10-16", None, "2026-10-16T09:00:12", GARMIN_ERROR)
    g = _live().json()["garmin"]
    assert g == {
        "configured": True,
        "last_sync_at": "2026-10-16T09:00:12Z",
        "last_sync_error": GARMIN_ERROR,
        "watch_data_until": "2026-10-14",
    }


def test_c1_default_fetched_at_form_gets_a_Z(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=None, alpaca_secret=None)
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    _insert(user_db, "2026-10-15", None, "2026-10-15 07:30:00", None, sleep=25000)
    g = _live().json()["garmin"]
    assert g["last_sync_at"] == "2026-10-15T07:30:00Z"
    assert g["last_sync_error"] is None
    assert g["watch_data_until"] == "2026-10-15"


def test_c2_empty_table_gives_nulls(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=None, alpaca_secret=None)
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    g = _live().json()["garmin"]
    assert g == {"configured": True, "last_sync_at": None, "last_sync_error": None, "watch_data_until": None}


def test_c3_alpaca_ok_uses_paper_host(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=PAPER_KEY, eodhd=EODHD_KEY)
    net = _good_net(monkeypatch)
    a = _live().json()["alpaca"]
    assert a == {"configured": True, "paper": True, "status": "ACTIVE", "trading_blocked": False, "error": None}
    alpaca_calls = [c for c in net.calls if c[0] == PAPER_HOST]
    assert len(alpaca_calls) == 1
    assert alpaca_calls[0][1]["timeout"] == 5
    assert all(c[0].startswith("https://paper-api.alpaca.markets/") or "alpaca" not in c[0] for c in net.calls)


def test_c4_non_paper_key_makes_no_call(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key="AK-live-looking-key", eodhd=None)
    net = _net(monkeypatch)
    a = _live().json()["alpaca"]
    assert a["configured"] is True
    assert a["error"] == "The Alpaca key is not a paper key"
    assert a["status"] is None
    assert net.calls == []


def test_c5_alpaca_401_and_timeout_leave_others_alone(monkeypatch, user_db):
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    _env(monkeypatch, alpaca_key=PAPER_KEY, eodhd=EODHD_KEY)
    _net(
        monkeypatch,
        alpaca=_Resp(401, {"message": "unauthorized"}),
        eodhd=_Resp(200, {"apiRequests": 1, "dailyRateLimit": 2, "apiRequestsDate": "2026-10-16"}),
    )
    body = _live().json()
    assert body["alpaca"]["error"] == "Alpaca paper answered HTTP 401"
    assert body["alpaca"]["status"] is None
    assert body["eodhd"]["requests_today"] == 1
    assert body["garmin"]["configured"] is True

    connections._CACHE.clear()
    _net(monkeypatch, alpaca=httpx.ReadTimeout("slow"), eodhd=httpx.ConnectTimeout("slow"))
    body = _live().json()
    assert body["alpaca"]["error"] == "Alpaca paper did not answer in 5 s"
    assert body["eodhd"]["error"] == "EODHD did not answer in 5 s"
    assert body["garmin"]["configured"] is True


def test_c6_eodhd_mapping_and_cache(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=None, alpaca_secret=None, eodhd=EODHD_KEY)
    net = _good_net(monkeypatch)
    e = _live().json()["eodhd"]
    assert e == {
        "configured": True,
        "requests_today": 39,
        "daily_limit": 100000,
        "as_of": "2026-10-16",
        "error": None,
    }
    _live()
    eodhd_calls = [c for c in net.calls if c[0].startswith(EODHD_URL)]
    assert len(eodhd_calls) == 1
    assert eodhd_calls[0][1]["timeout"] == 5


def test_c6_cache_expires_after_60_s(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=PAPER_KEY, eodhd=EODHD_KEY)
    net = _good_net(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(connections.time, "monotonic", lambda: now[0])
    _live()
    now[0] += 59
    _live()
    assert len(net.calls) == 2
    now[0] += 2
    _live()
    assert len(net.calls) == 4


def test_c7_no_key_value_in_any_body(monkeypatch, user_db):
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    _env(monkeypatch, alpaca_key=PAPER_KEY, eodhd=EODHD_KEY)
    _good_net(monkeypatch)
    text = _live().text
    for secret in (PAPER_KEY, ALPACA_SECRET, EODHD_KEY):
        assert secret not in text
    connections._CACHE.clear()
    _net(monkeypatch, alpaca=_Resp(401), eodhd=httpx.ConnectError("boom " + EODHD_KEY))
    text = _live().text
    for secret in (PAPER_KEY, ALPACA_SECRET, EODHD_KEY):
        assert secret not in text


def test_c8_unset_keys_make_no_call(monkeypatch, user_db):
    monkeypatch.setattr(garmin, "is_configured", lambda: False)
    _env(monkeypatch, alpaca_key=None, alpaca_secret=None, eodhd=None)
    net = _net(monkeypatch)
    body = _live().json()
    assert body["alpaca"] == {
        "configured": False, "paper": None, "status": None, "trading_blocked": None, "error": None,
    }
    assert body["eodhd"] == {
        "configured": False, "requests_today": None, "daily_limit": None, "as_of": None, "error": None,
    }
    assert body["garmin"]["configured"] is False
    assert net.calls == []
    assert body["checked_at"].endswith("Z")


def test_c9_fresh_database_without_garmin_table(monkeypatch, tmp_path):
    path = tmp_path / "fresh.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(userdb, "open_rw", functools.partial(userdb.open_rw, path))
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    _env(monkeypatch, alpaca_key=None, alpaca_secret=None, eodhd=None)
    r = _client().get("/api/settings/connections/live")
    assert r.status_code == 200
    assert r.json()["garmin"] == {
        "configured": True, "last_sync_at": None, "last_sync_error": None, "watch_data_until": None,
    }


def test_c10_failed_alpaca_call_is_cached(monkeypatch, user_db):
    _env(monkeypatch, alpaca_key=PAPER_KEY, eodhd=None)
    net = _net(monkeypatch, alpaca=_Resp(401))
    first = _live().json()["alpaca"]["error"]
    second = _live().json()["alpaca"]["error"]
    assert first == second == "Alpaca paper answered HTTP 401"
    assert len(net.calls) == 1


def test_c11_old_route_unchanged_and_makes_no_call(monkeypatch):
    _env(monkeypatch, eodhd=EODHD_KEY)
    monkeypatch.setattr(garmin, "is_configured", lambda: True)
    net = _net(monkeypatch)
    assert _client().get("/api/settings/connections").json() == {"alpaca": True, "eodhd": True, "garmin": True}
    assert net.calls == []
