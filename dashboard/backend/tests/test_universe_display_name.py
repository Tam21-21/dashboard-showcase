"""Lane UNI1: a price universe gets a typed display name (DECISIONS #462 item 4)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import eventsdb, researchdb
from dashboard.backend.routes import research

WIN_START = 1072915200
WIN_END = 1262304000


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    with researchdb.transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO research_eodhd_tickers(ticker) VALUES (?);",
                         [("AAPL",), ("MSFT",), ("NVDA",)])
    return researchdb


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _shown(uid):
    with researchdb.get_conn() as conn:
        return conn.execute("SELECT display_name FROM research_datasets WHERE id = ?;", (uid,)).fetchone()[0]


def _body(name="u1", **extra):
    return {"name": name, "tickers": ["AAPL"], "window_start_ts": WIN_START, "window_end_ts": WIN_END, **extra}


# ---- d1: the column and its one-time backfill -----------------------------------------

def test_column_added_once_and_existing_universes_named(db):
    a = researchdb.create_price_universe("strategy_runs_universe_2015_2020", ["AAPL"], WIN_START, WIN_END)["universe_id"]
    b = researchdb.create_price_universe("MAG-7", ["MSFT"], WIN_START, WIN_END)["universe_id"]
    with researchdb.transaction() as conn:
        conn.execute("ALTER TABLE research_datasets DROP COLUMN display_name;")
    researchdb.init_db()
    assert _shown(a) == "Strategy runs universe 2015 2020"
    assert _shown(b) == "MAG 7"
    c = researchdb.create_price_universe("later", ["NVDA"], WIN_START, WIN_END)["universe_id"]
    assert _shown(c) is None
    researchdb.init_db()
    assert _shown(c) is None
    assert _shown(a) == "Strategy runs universe 2015 2020"


# ---- d2: create -----------------------------------------------------------------------

def test_create_stores_and_echoes_display_name(client):
    r = client.post("/api/research/price-universes", json=_body(display_name="  Big tech  "))
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Big tech"
    assert _shown(r.json()["universe_id"]) == "Big tech"
    r = client.post("/api/research/price-universes", json=_body(name="u2"))
    assert r.status_code == 200 and r.json()["display_name"] is None
    assert _shown(r.json()["universe_id"]) is None
    r = client.post("/api/research/price-universes", json=_body(name="u3", display_name="x" * 61))
    assert r.status_code == 422 and "price_universe_display_name_invalid" in r.text
    with researchdb.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_datasets WHERE name = 'u3';").fetchone()[0] == 0


# ---- d3: edit -------------------------------------------------------------------------

def test_patch_display_name_name_both_neither(client):
    uid = client.post("/api/research/price-universes", json=_body(display_name="First")).json()["universe_id"]
    url = f"/api/research/price-universes/{uid}"
    r = client.patch(url, json={"display_name": "Second"})
    assert r.status_code == 200 and r.json() == {"universe_id": uid, "name": "u1", "display_name": "Second"}
    r = client.patch(url, json={"name": "renamed"})
    assert r.status_code == 200 and r.json() == {"universe_id": uid, "name": "renamed", "display_name": "Second"}
    r = client.patch(url, json={"name": "again", "display_name": "Third"})
    assert r.status_code == 200 and r.json() == {"universe_id": uid, "name": "again", "display_name": "Third"}
    r = client.patch(url, json={})
    assert r.status_code == 422 and "price_universe_edit_empty" in r.text
    r = client.patch(url, json={"display_name": "y" * 61})
    assert r.status_code == 422 and "price_universe_display_name_invalid" in r.text
    assert _shown(uid) == "Third"
    assert client.patch("/api/research/price-universes/99999", json={"display_name": "Nope"}).status_code == 404
    assert client.patch("/api/research/price-universes/99999", json={"name": "Nope"}).status_code == 404


# ---- d4: the listing ------------------------------------------------------------------

def test_datasets_listing_carries_display_name(client):
    uid = client.post("/api/research/price-universes", json=_body(display_name="Listed")).json()["universe_id"]
    rows = {r["id"]: r for r in client.get("/api/research/datasets").json()}
    assert rows[uid]["display_name"] == "Listed"
