"""Lane UI36-0: the Data library feed reads.

Each test runs on a scratch research.db and events.db in a tmp folder, never the real ones.
"""
from __future__ import annotations

import calendar
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import eventsdb, researchdb
from dashboard.backend.events import catalog as event_catalog
from dashboard.backend.events import feeds
from dashboard.backend.routes import research


def _ts(y, m, d, hh=0, mm=0):
    return calendar.timegm((y, m, d, hh, mm, 0))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    return tmp_path


def _batch(source="benzinga", kind="load", status="succeeded"):
    now = int(time.time())
    with eventsdb.events_transaction() as conn:
        cur = conn.execute(
            """INSERT INTO research_event_batches
                   (kind, source, policy, source_descriptor,
                    started_at, last_heartbeat_at, status, event_count, quarantine_count)
               VALUES (?, ?, 'quarantine', 'ui36-0 test', ?, ?, ?, 1, 0)""",
            (kind, source, now, now, status))
        return cur.lastrowid


def _event(batch_id, event_type, knowledge_at, source="benzinga", status="active", key=None):
    key = key or f"{event_type}_{knowledge_at}_{status}"
    with eventsdb.events_transaction() as conn:
        conn.execute(
            """INSERT INTO research_events
                   (event_type, status, producer_id, natural_key,
                    knowledge_at, ingested_at, source, origin,
                    batch_id, payload, payload_schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'loaded', ?, '{}', 1)""",
            (event_type, status, f"p_{key}", key, knowledge_at, knowledge_at, source, batch_id))


def _set_row(status, event_type="analyst_rating", source="benzinga"):
    # create the catalog row (zero events yet), then force its status and error
    event_catalog.refresh_event_catalog_for(event_type=event_type, source=source)
    with researchdb.transaction() as conn:
        conn.execute(
            "UPDATE research_datasets SET status=?, last_error='docker exited -7' "
            "WHERE dataset_type='event_series' AND event_type=? AND source=?",
            (status, event_type, source))


def _row(event_type="analyst_rating", source="benzinga"):
    with researchdb.get_conn() as conn:
        return dict(conn.execute(
            "SELECT status, last_error, rows FROM research_datasets "
            "WHERE dataset_type='event_series' AND event_type=? AND source=?",
            (event_type, source)).fetchone())


def _two_events(batch_id):
    _event(batch_id, "analyst_rating", _ts(2026, 10, 1), key="a")
    _event(batch_id, "analyst_rating", _ts(2026, 10, 2), key="b")


def test_d1_failed_row_heals(db):
    _set_row("failed")
    _two_events(_batch())
    event_catalog.refresh_event_catalog_for(event_type="analyst_rating", source="benzinga")
    row = _row()
    assert row["status"] == "ready"
    assert row["last_error"] == "docker exited -7"
    assert row["rows"] == 2


def test_d2_failed_row_without_events_stays_failed(db):
    _set_row("failed")
    _batch()
    event_catalog.refresh_event_catalog_for(event_type="analyst_rating", source="benzinga")
    assert _row()["status"] == "failed"


@pytest.mark.parametrize("status", ["deleted", "pending", "fetching"])
def test_other_statuses_untouched(db, status):
    _set_row(status)
    _two_events(_batch())
    event_catalog.refresh_event_catalog_for(event_type="analyst_rating", source="benzinga")
    assert _row()["status"] == status


def test_d2b_newest_batch_failed_stays_failed(db):
    _set_row("failed")
    _two_events(_batch(status="succeeded"))
    _batch(status="failed")
    event_catalog.refresh_event_catalog_for(event_type="analyst_rating", source="benzinga")
    assert _row()["status"] == "failed"


def test_d2c_no_batch_stays_failed(db):
    _set_row("failed")
    _two_events(_batch(source="alpaca_news"))
    event_catalog.refresh_event_catalog_for(event_type="analyst_rating", source="benzinga")
    assert _row()["status"] == "failed"


def test_d3_reconcile_heals(db):
    _set_row("failed")
    _two_events(_batch())
    event_catalog.reconcile_event_catalog()
    row = _row()
    assert row["status"] == "ready"
    assert row["last_error"] == "docker exited -7"


def _feeds_by_type():
    with eventsdb.get_events_conn() as conn:
        return {f["event_type"]: f for f in feeds.summarize_event_feeds(conn, 3)}


def test_d4_news_per_year(db):
    b = _batch()
    _event(b, "news", _ts(2024, 3, 1))
    _event(b, "news", _ts(2024, 7, 1))
    _event(b, "news", _ts(2025, 2, 1))
    _event(b, "news", _ts(2023, 5, 1), status="rolled_back")
    out = _feeds_by_type()
    assert out["news"]["per_year"] == [{"year": 2024, "count": 2}, {"year": 2025, "count": 1}]
    assert out["analyst_rating"]["per_year"] == []
    assert out["news"]["total_count"] == 3


def test_d5_utc_year_edge(db):
    _event(_batch(), "news", _ts(2024, 12, 31, 23, 30))
    assert _feeds_by_type()["news"]["per_year"] == [{"year": 2024, "count": 1}]


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _insert_facts(filing_dates):
    now = int(time.time())
    with eventsdb.events_transaction() as conn:
        bid = conn.execute(
            "INSERT INTO research_fundamental_batches (kind, source, started_at, last_heartbeat_at, status) "
            "VALUES ('fetch', 'sec_edgar', ?, ?, 'succeeded')", (now, now)).lastrowid
        for i, fd in enumerate(filing_dates):
            conn.execute(
                "INSERT INTO research_fundamental_facts (cik, concept, taxonomy, xbrl_tag, unit, "
                "fiscal_year, fiscal_period_code, period_end, filing_date, value, source_form, "
                "batch_id, ingested_at) VALUES (1, 'net_income', 'us-gaap', 'NetIncomeLoss', 'USD', "
                "2009, 'FY', ?, ?, 1.0, '10-K', ?, ?)", (fd - 86400 * 40 + i, fd, bid, now))


def test_d6_fundamentals_per_year(db, client):
    assert client.get("/api/research/fundamentals/status").json()["per_year"] == []
    _insert_facts([_ts(2010, 3, 1), _ts(2010, 8, 1), _ts(2012, 2, 1)])
    body = client.get("/api/research/fundamentals/status").json()
    assert body["per_year"] == [{"year": 2010, "count": 2}, {"year": 2012, "count": 1}]
    assert body["total_facts_on_record"] == 3
