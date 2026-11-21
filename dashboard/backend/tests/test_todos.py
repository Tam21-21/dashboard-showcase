"""CAL1: to-do validation, the open-items route, and moving a late item to today."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import userdb
from dashboard.backend.deps import get_user_conn
from dashboard.backend.routes import life


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "user.db"
    userdb.init_db(db)
    app = FastAPI()
    app.include_router(life.router)

    def conn_dep():
        conn = userdb.open_rw(db)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_user_conn] = conn_dep
    return TestClient(app)


def _add(client, date, text):
    r = client.post("/api/life/todos", json={"date": date, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_t1_bad_date_or_empty_text_refused(client):
    assert client.post("/api/life/todos", json={"date": "2/11/2026", "text": "x"}).status_code == 422
    assert client.post("/api/life/todos", json={"date": "2026-11-02", "text": "   "}).status_code == 422
    assert client.post("/api/life/todos", json={"date": "2026-11-02", "text": "x" * 201}).status_code == 422


def test_t2_text_is_stored_stripped(client):
    assert _add(client, "2026-11-02", "  Buy milk ")["text"] == "Buy milk"


def test_t3_open_items_before_a_day(client):
    a = _add(client, "2026-10-31", "a")
    b = _add(client, "2026-11-01", "b")
    c = _add(client, "2026-10-31", "c")
    d = _add(client, "2026-11-02", "d")
    client.patch(f"/api/life/todos/{c['id']}", json={"done": True})
    r = client.get("/api/life/todos/open", params={"before": "2026-11-02"})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == [a["id"], b["id"]]
    assert d["id"] not in [t["id"] for t in r.json()]
    assert client.get("/api/life/todos/open", params={"before": "nope"}).status_code == 422


def test_t4_ticking_a_late_item_can_move_it_to_today(client):
    a = _add(client, "2026-10-31", "a")
    r = client.patch(f"/api/life/todos/{a['id']}", json={"done": True, "date": "2026-11-02"})
    assert r.status_code == 200
    rows = client.get("/api/life/todos", params={"date": "2026-11-02"}).json()
    assert [(t["id"], t["done"]) for t in rows] == [(a["id"], 1)]
    assert client.patch(f"/api/life/todos/{a['id']}", json={"date": "bad"}).status_code == 422


def test_t5_refusals_carry_one_plain_sentence(client):
    for body in ({"date": "2/11/2026", "text": "x"}, {"date": "2026-11-02", "text": "   "}):
        r = client.post("/api/life/todos", json=body)
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], str) and r.json()["detail"].endswith(".")
    a = _add(client, "2026-11-02", "a")
    for body in ({"date": "bad"}, {"text": "  "}):
        r = client.patch(f"/api/life/todos/{a['id']}", json=body)
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], str) and r.json()["detail"].endswith(".")


# ── TD1: a ticked late item keeps showing today (done_at) ─────────────────────

def _paris_day(done_at: str) -> str:
    utc = datetime.strptime(done_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return utc.astimezone(ZoneInfo("Europe/Paris")).date().isoformat()


def _done_late(client, day):
    return client.get("/api/life/todos/done-late", params={"day": day})


def test_t6_init_db_adds_done_at_and_keeps_rows(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE todos (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
        "done INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0, "
        "date TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO todos (text, done, date) VALUES ('old done', 1, '2026-10-30')")
    conn.execute("INSERT INTO todos (text, done, date) VALUES ('old open', 0, '2026-10-31')")
    conn.commit()
    conn.close()
    userdb.init_db(db)
    userdb.init_db(db)
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(todos)")]
    rows = conn.execute("SELECT text, done, done_at FROM todos ORDER BY id").fetchall()
    conn.close()
    assert "done_at" in cols
    assert rows == [("old done", 1, None), ("old open", 0, None)]


def test_t7_patch_sets_and_clears_done_at(client):
    a = _add(client, "2026-10-31", "a")
    r = client.patch(f"/api/life/todos/{a['id']}", json={"done": True}).json()
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", r["done_at"])
    again = client.patch(f"/api/life/todos/{a['id']}", json={"done": True}).json()
    assert again["done_at"] == r["done_at"]
    off = client.patch(f"/api/life/todos/{a['id']}", json={"done": False}).json()
    assert off["done_at"] is None


def test_t8_done_late_route(client):
    a = _add(client, "2026-10-31", "a")
    done_at = client.patch(f"/api/life/todos/{a['id']}", json={"done": True}).json()["done_at"]
    d = _paris_day(done_at)
    nxt = (datetime.fromisoformat(d) + timedelta(days=1)).date().isoformat()
    assert [t["id"] for t in _done_late(client, d).json()] == [a["id"]]
    assert _done_late(client, nxt).json() == []
    assert _done_late(client, "2026-10-31").json() == []
    same_day = _add(client, d, "same day")
    client.patch(f"/api/life/todos/{same_day['id']}", json={"done": True})
    assert [t["id"] for t in _done_late(client, d).json()] == [a["id"]]
    assert a["id"] not in [t["id"] for t in client.get("/api/life/todos/open", params={"before": d}).json()]
    assert _done_late(client, "nope").status_code == 422


def test_t9_done_late_paris_day_edges(client, tmp_path):
    conn = sqlite3.connect(tmp_path / "user.db")
    ids = []
    for date, done_at in [
        ("2026-11-01", "2026-11-02 22:59:59"),
        ("2026-11-01", "2026-11-02 23:00:00"),
        ("2026-07-01", "2026-07-14 22:30:00"),
        ("2026-11-01", None),
    ]:
        cur = conn.execute(
            "INSERT INTO todos (text, done, date, done_at) VALUES ('x', 1, ?, ?)", (date, done_at)
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    a, b, c, n = ids

    def listed(day):
        return [t["id"] for t in _done_late(client, day).json()]

    assert listed("2026-11-02") == [a]
    assert listed("2026-11-03") == [b]
    assert listed("2026-07-15") == [c]
    assert n not in listed("2026-11-02") and n not in listed("2026-11-03")
