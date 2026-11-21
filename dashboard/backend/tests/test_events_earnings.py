"""Lane EA3-0: GET /api/research/events/earnings (releases, surprise, price reaction).

Each test runs on a scratch events.db written through write_core.write_event_batch, the way
lane EA2's derive writes earnings. Alpaca is replaced by a stub that records each request.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import eventsdb, researchdb
from dashboard.backend.events import write_core
from dashboard.backend.research import earnings_reaction
from dashboard.backend.routes import research

_ids = itertools.count(1000)

APPLE_RELEASE = int(dt.datetime(2026, 7, 30, 20, 30, 28, tzinfo=dt.timezone.utc).timestamp())


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    earnings_reaction.clear_cache()
    return tmp_path


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _record(ticker="AAPL", company="Apple", quarter=3, release=APPLE_RELEASE, eps=(2.02, 1.89),
            sales=(109.417e9, 108.651e9), is_update=False, adjusted=False, word="BEAT"):
    article_id = next(_ids)
    prefix = "Update: " if is_update else ""
    headline = f"{prefix}{company} Q{quarter} EPS ${eps[0]} Beats ${eps[1]} Estimate"
    return {
        "event_type": "earnings", "natural_key": str(article_id), "producer_id": str(article_id),
        "knowledge_at": release, "occurred_at": release, "source": "alpaca_news",
        "origin": "fetched", "payload_schema_version": 2,
        "raw_input": json.dumps({"article_id": article_id}),
        "payload": {
            "article_id": article_id, "raw_ticker": ticker, "fiscal_quarter": quarter,
            "adjusted": adjusted, "eps_actual": float(eps[0]), "eps_estimate": float(eps[1]),
            "eps_word": word, "sales_actual": float(sales[0]), "sales_estimate": float(sales[1]),
            "sales_word": "BEAT", "release_at": release, "headline": headline,
            "is_update": is_update,
        },
        "companies": [],
    }


def _write(records):
    result = write_core.write_event_batch(
        eventsdb.DB_PATH, source="alpaca_news", kind="fetch", policy="quarantine",
        records=records, source_descriptor="ea3-0 test", research_db_path=researchdb.DB_PATH)
    assert result.status == "succeeded", result


class Stub:
    """Replaces the one Alpaca GET; records every request."""

    def __init__(self, bars, status=200):
        self.bars = bars
        self.status = status
        self.requests = []

    def __call__(self, url, params, headers):
        self.requests.append((url, dict(params)))
        if self.status != 200:
            return self.status, {}
        return 200, {"bars": self.bars, "next_page_token": None}


def _bar(day, close):
    return {"t": f"{day}T04:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": 1}


@pytest.fixture()
def alpaca(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    stub = Stub({"AAPL": [_bar("2026-07-29", 330.0), _bar("2026-07-30", 333.14),
                          _bar("2026-07-31", 308.64)]})
    monkeypatch.setattr(earnings_reaction, "_http_get", stub)
    return stub


def test_n1_newest_first_with_fields(client, alpaca):
    later = APPLE_RELEASE + 86400 * 90
    _write([_record(), _record(ticker="MSFT", company="Microsoft", release=later, quarter=4),
            _record(ticker="AAPL", quarter=2, release=APPLE_RELEASE - 86400 * 90)])
    alpaca.bars["MSFT"] = []
    body = client.get("/api/research/events/earnings?limit=10").json()
    rows = body["rows"]
    assert [r["ticker"] for r in rows] == ["MSFT", "AAPL", "AAPL"]
    apple = rows[1]
    assert apple["company"] == "Apple" and apple["quarter"] == "Q3"
    assert apple["release_at"] == "2026-07-30T20:30:28Z"
    assert apple["eps_actual"] == 2.02 and apple["eps_estimate"] == 1.89
    assert apple["eps_surprise"] == pytest.approx(0.0688, abs=1e-4)
    assert apple["sales_surprise"] == pytest.approx(0.00705, abs=1e-4)
    assert apple["result"] == "BEAT" and apple["corrected"] is False
    assert body["unread"] == 0


def test_n2_update_replaces_figures(client, alpaca):
    first = _record()
    upd = _record(release=APPLE_RELEASE + 3600, eps=(2.05, 1.89), is_update=True)
    _write([first, upd])
    rows = client.get("/api/research/events/earnings").json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["corrected"] is True and row["eps_actual"] == 2.05
    assert row["first_printed"]["eps_actual"] == 2.02
    assert row["corrected_at"] == "2026-07-30T21:30:28Z"


def test_n3_reaction_after_close_and_pre_open(client, alpaca):
    pre_open = int(dt.datetime(2026, 7, 31, 11, 0, tzinfo=dt.timezone.utc).timestamp())
    _write([_record(), _record(quarter=4, release=pre_open)])
    rows = client.get("/api/research/events/earnings").json()["rows"]
    assert len(rows) == 2
    for row in rows:
        c = row["closes"]
        assert c["dates"][c["before_index"]] == "2026-07-30"
        assert c["dates"][c["after_index"]] == "2026-07-31"
        assert row["reaction"] == -0.0735


def test_n4_no_close_after_yet(client, alpaca):
    alpaca.bars["AAPL"] = [_bar("2026-07-29", 330.0), _bar("2026-07-30", 333.14)]
    _write([_record()])
    row = client.get("/api/research/events/earnings").json()["rows"][0]
    assert row["closes"]["before_index"] == 1
    assert row["closes"]["after_index"] is None and row["reaction"] is None


def test_n5_one_request_then_cached(client, alpaca):
    _write([_record(), _record(ticker="MSFT", company="Microsoft", quarter=4)])
    alpaca.bars["MSFT"] = []
    client.get("/api/research/events/earnings")
    assert len(alpaca.requests) == 1
    params = alpaca.requests[0][1]
    assert sorted(params["symbols"].split(",")) == ["AAPL", "MSFT"]
    assert params["timeframe"] == "1Day" and params["adjustment"] == "all"
    assert params["feed"] == "sip" and params["start"].startswith("2026-07-09")
    client.get("/api/research/events/earnings")
    assert len(alpaca.requests) == 1


def test_n6_alpaca_refuses_gives_503_plain(client, alpaca):
    alpaca.status = 403
    _write([_record()])
    resp = client.get("/api/research/events/earnings")
    assert resp.status_code == 503
    assert not re.search(r"[a-z]+_[a-z]+", resp.json()["detail"])


def test_n7_missing_field_counted_unread(client, alpaca):
    _write([_record()])
    with eventsdb.events_transaction() as conn:
        conn.execute(
            """INSERT INTO research_events
                   (event_type, status, producer_id, natural_key, knowledge_at, ingested_at,
                    source, origin, batch_id, payload, payload_schema_version)
               SELECT 'earnings', 'active', 'bad', 'bad', knowledge_at + 5, ingested_at, source,
                      origin, batch_id, json_remove(payload, '$.eps_actual'), 2
               FROM research_events LIMIT 1""")
    body = client.get("/api/research/events/earnings").json()
    assert body["unread"] == 1 and len(body["rows"]) == 1


@pytest.mark.parametrize("limit", [0, 201])
def test_n8_bad_limit_is_422(client, limit):
    assert client.get(f"/api/research/events/earnings?limit={limit}").status_code == 422


def test_end_is_at_least_15_minutes_ago(client, alpaca):
    _write([_record()])
    client.get("/api/research/events/earnings")
    end = dt.datetime.strptime(alpaca.requests[0][1]["end"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    assert end <= dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)


def test_older_release_is_not_served_from_a_shorter_cached_series(client, alpaca):
    _write([_record()])
    client.get("/api/research/events/earnings")
    older = int(dt.datetime(2026, 7, 1, 20, 30, tzinfo=dt.timezone.utc).timestamp())
    _write([_record(quarter=2, release=older)])
    client.get("/api/research/events/earnings")
    assert len(alpaca.requests) == 2
    assert alpaca.requests[1][1]["start"] < alpaca.requests[0][1]["start"]
