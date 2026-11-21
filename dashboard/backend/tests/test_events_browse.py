"""Lane UI38-0: the Events tab server reads (search, news-top, rating-actions).

Each test runs on a scratch events.db in a tmp folder, never the real one.
"""
from __future__ import annotations

import itertools
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import eventsdb, researchdb
from dashboard.backend.routes import research

_keys = itertools.count(1)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    return tmp_path


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _batch(source="benzinga", kind="load", status="succeeded"):
    now = int(time.time())
    with eventsdb.events_transaction() as conn:
        cur = conn.execute(
            """INSERT INTO research_event_batches
                   (kind, source, policy, source_descriptor,
                    started_at, last_heartbeat_at, status, event_count, quarantine_count)
               VALUES (?, ?, 'quarantine', 'ui38-0 test', ?, ?, ?, 1, 0)""",
            (kind, source, now, now, status))
        return cur.lastrowid


def _event(batch_id, event_type, knowledge_at, payload=None, source="benzinga",
           status="active", key=None):
    key = key or f"k{next(_keys)}"
    with eventsdb.events_transaction() as conn:
        cur = conn.execute(
            """INSERT INTO research_events
                   (event_type, status, producer_id, natural_key,
                    knowledge_at, ingested_at, source, origin,
                    batch_id, payload, payload_schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'loaded', ?, ?, 1)""",
            (event_type, status, f"p_{key}", key, knowledge_at, knowledge_at, source,
             batch_id, json.dumps(payload or {})))
        return cur.lastrowid


def _news(b, t, headline, symbols, status="active"):
    return _event(b, "news", t, {"headline": headline, "symbols": symbols, "url": "u"},
                  status=status)


def _rating(b, t, ticker, action, firm="Morgan Stanley"):
    return _event(b, "analyst_rating", t, {
        "raw_ticker": ticker, "action_company": action, "analyst_firm": firm,
        "new_grade": "Buy", "old_grade": "Hold"})


def _map(cik, ticker, source="sec_company_tickers", effective_to=None):
    with eventsdb.events_transaction() as conn:
        conn.execute(
            """INSERT INTO research_cik_ticker_map
                   (cik, ticker, effective_to, source, loaded_at) VALUES (?, ?, ?, ?, 0)""",
            (cik, ticker, effective_to, source))


def _13d(b, t, cik, filer="Big Fund LP", subject="Apple Inc."):
    eid = _event(b, "sec_13d", t, {"filer_name": filer, "subject_name": subject,
                                   "percent_owned": 5.1}, source="sec_edgar")
    with eventsdb.events_transaction() as conn:
        conn.execute("INSERT INTO research_event_companies (event_id, cik, role) "
                     "VALUES (?, ?, 'primary_issuer')", (eid, cik))
    return eid


ALL = "news,analyst_rating,sec_13d"


def _seed():
    b = _batch()
    _map(320193, "AAPL")
    n1 = _news(b, 1000, "Apple beats", ["AAPL"])
    n2 = _news(b, 2000, "Fed holds", [])
    n3 = _news(b, 3000, "apple supplier", ["AAPL", "TSM"])
    r1 = _rating(b, 4000, "AAPL", "Upgrades")
    r2 = _rating(b, 5000, "MSFT", "Downgrades")
    d1 = _13d(b, 6000, 320193)
    return dict(n1=n1, n2=n2, n3=n3, r1=r1, r2=r2, d1=d1)


def test_b1_search_q_across_types(client):
    s = _seed()
    r = client.get("/api/research/events/search", params={"types": ALL, "q": "apple"})
    assert r.status_code == 200
    body = r.json()
    assert [e["event_id"] for e in body["events"]] == [s["d1"], s["n3"], s["n1"]]
    assert body["total_matching"] == 3
    assert body["next"] is None


def test_b2_search_ticker(client):
    s = _seed()
    r = client.get("/api/research/events/search", params={"types": ALL, "ticker": "aapl"})
    body = r.json()
    got = {e["event_id"]: e["tickers"] for e in body["events"]}
    assert got == {s["d1"]: ["AAPL"], s["r1"]: ["AAPL"], s["n3"]: ["AAPL", "TSM"],
                   s["n1"]: ["AAPL"]}
    assert body["total_matching"] == 4


def test_b3_search_action(client):
    s = _seed()
    r = client.get("/api/research/events/search", params={"types": ALL, "action": "Upgrades"})
    body = r.json()
    assert [e["event_id"] for e in body["events"]] == [s["r1"]]
    assert body["total_matching"] == 1


def test_b4_paging(client):
    b = _batch()
    ids = [_news(b, 1000 + i, f"apple {i}", ["AAPL"]) for i in range(5)]
    p = {"types": "news", "q": "apple", "limit": 2}
    one = client.get("/api/research/events/search", params=p).json()
    assert [e["event_id"] for e in one["events"]] == [ids[4], ids[3]]
    assert one["next"] == {"after_cursor": f"1003_{ids[3]}"}
    assert one["total_matching"] == 5
    two = client.get("/api/research/events/search",
                     params={**p, "limit": 10, "after_cursor": one["next"]["after_cursor"]}).json()
    assert [e["event_id"] for e in two["events"]] == [ids[2], ids[1], ids[0]]
    assert two["next"] is None
    assert two["total_matching"] == 5


def test_b5_rolled_back_never_counted(client):
    b = _batch()
    _news(b, 1000, "apple gone", ["AAPL"], status="rolled_back")
    keep = _news(b, 2000, "apple here", ["AAPL"])
    body = client.get("/api/research/events/search", params={"types": "news", "q": "apple"}).json()
    assert [e["event_id"] for e in body["events"]] == [keep]
    assert body["total_matching"] == 1


def test_b6_bad_inputs(client):
    r = client.get("/api/research/events/search", params={"types": "news,bogus"})
    assert r.status_code == 422 and "bogus" in r.text
    assert client.get("/api/research/events/search", params={"types": ""}).status_code == 422
    r = client.get("/api/research/events/search", params={"types": "news", "after_cursor": "x"})
    assert r.status_code == 400


def test_b7_news_top(client):
    b = _batch()
    now = int(time.time())
    _news(b, now - 10 * 86400, "a", ["AAPL", "TSM"])
    _news(b, now - 20 * 86400, "b", ["AAPL", "AAPL"])
    _news(b, now - 40 * 86400, "c", ["MSFT"])
    _news(b, now - 5 * 86400, "gone", ["MSFT"], status="rolled_back")
    r = client.get("/api/research/events/news-top", params={"days": 30})
    assert r.status_code == 200
    assert r.json() == [{"ticker": "AAPL", "count": 2}, {"ticker": "TSM", "count": 1}]


def test_b8_rating_actions(client):
    b = _batch()
    for i, a in enumerate(["Upgrades", "Upgrades", "Downgrades", "Downgrades", "Maintains"]):
        _rating(b, 1000 + i, "AAPL", a)
    body = client.get("/api/research/events/rating-actions").json()
    assert body["total"] == 5
    assert body["actions"] == [{"action": "Downgrades", "count": 2},
                               {"action": "Upgrades", "count": 2},
                               {"action": "Maintains", "count": 1}]


def test_b9_empty_store(client):
    body = client.get("/api/research/events/search", params={"types": ALL}).json()
    assert body == {"events": [], "next": None, "total_matching": 0}
    assert client.get("/api/research/events/news-top").json() == []
    assert client.get("/api/research/events/rating-actions").json() == {"total": 0, "actions": []}
