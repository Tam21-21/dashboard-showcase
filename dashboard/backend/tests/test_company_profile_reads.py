"""Lane UNI1b: sectors read when members join.

Scratch research.db and events.db in a tmp folder. No test reaches the SEC: every read goes through a stub
client factory (or the factory is never reached).
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import config, eventsdb, loop_counters, researchdb
from dashboard.backend.events import company_profiles as cp
from dashboard.backend.events.errors import EdgarNetworkTimeout
from dashboard.backend.routes import research

T0 = 1_700_000_000
DAY = 86_400
WIN_START = 1072915200
WIN_END = 1262304000
AAPL_SUB = {"ownerOrg": "06 Technology", "sic": "3571", "sicDescription": "Electronic Computers"}
MSFT_SUB = {"ownerOrg": "06 Technology", "sic": "7372", "sicDescription": "Services-Prepackaged Software"}
TIMEOUT_WORDS = "SEC did not answer in time"
NO_CONTACT = "No SEC contact address is set on the server"
BAD_CONTACT = "The SEC contact address on the server is not valid"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    with researchdb.transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO research_eodhd_tickers(ticker) VALUES (?);",
                         [("AAPL",), ("MSFT",), ("NVDA",), ("ZZZZ",)])
    return researchdb


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _cik_row(cik, ticker, name):
    with eventsdb.events_transaction() as conn:
        conn.execute("INSERT INTO research_cik_ticker_map(cik, ticker, effective_to, source, name, loaded_at) "
                     "VALUES (?, ?, NULL, 'sec_company_tickers', ?, ?);", (cik, ticker, name, T0))


def _profile(cik, read_at=T0):
    with eventsdb.events_transaction() as conn:
        cp.store_profile(conn, cik, {"sector": "Technology", "sic": "3571", "sic_description": "Electronic Computers"}, read_at)


def _failure(cik, kind, reason, failed_at):
    with eventsdb.events_transaction() as conn:
        conn.execute("INSERT INTO research_company_profile_failures(cik, kind, reason, failed_at) VALUES (?, ?, ?, ?);",
                     (cik, kind, reason, failed_at))


def _failures():
    with eventsdb.get_events_conn() as conn:
        return {r["cik"]: (r["kind"], r["reason"], r["failed_at"])
                for r in conn.execute("SELECT * FROM research_company_profile_failures;")}


def _create(name, tickers):
    return researchdb.create_price_universe(name, list(tickers), WIN_START, WIN_END)["universe_id"]


class StubClient:
    def __init__(self, answers):
        self.answers = dict(answers)
        self.asked = []

    def get_submissions_json(self, cik):
        self.asked.append(cik)
        answer = self.answers[cik]
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer(f"stub failure for {cik}")
        return answer


def test_r1_due_ciks(db):
    _create("U", ["AAPL", "MSFT", "NVDA"])
    _cik_row(1, "AAPL", "Apple Inc.")
    _cik_row(2, "MSFT", "Microsoft")          # NVDA has no CIK: never due
    assert cp.due_ciks(T0) == [1, 2]
    _profile(1)
    assert cp.due_ciks(T0) == [2]
    _failure(2, "company", TIMEOUT_WORDS, T0 - 3600)
    assert cp.due_ciks(T0) == []
    with eventsdb.events_transaction() as conn:
        conn.execute("UPDATE research_company_profile_failures SET failed_at = ?;", (T0 - DAY,))
    assert cp.due_ciks(T0) == [2]
    with eventsdb.events_transaction() as conn:
        conn.execute("UPDATE research_company_profile_failures SET kind = 'contact', failed_at = ?;", (T0,))
    assert cp.due_ciks(T0) == [2]


def test_r2_company_failure_then_good_read(db):
    _create("U", ["AAPL"])
    _cik_row(1, "AAPL", "Apple Inc.")
    out = cp.run_due_reads(T0, lambda: StubClient({1: EdgarNetworkTimeout}))
    assert out["read"] == 0
    assert _failures() == {1: ("company", TIMEOUT_WORDS, T0)}
    cp.run_due_reads(T0 + DAY, lambda: StubClient({1: AAPL_SUB}))
    assert _failures() == {}
    with eventsdb.get_events_conn() as conn:
        assert cp.profiles_for(conn, [1])[1]["sector"] == "Technology"


def test_r3_member_detail_reason(db):
    uid = _create("U", ["AAPL", "MSFT"])
    _cik_row(1, "AAPL", "Apple Inc.")
    _cik_row(2, "MSFT", "Microsoft")
    _failure(1, "company", TIMEOUT_WORDS, T0)
    _failure(2, "company", TIMEOUT_WORDS, T0)
    _profile(2)
    members = {m["ticker"]: m for m in researchdb.price_universe_member_detail(uid, None)["members"]}
    words = f"The SEC company record could not be read: {TIMEOUT_WORDS}"
    assert members["AAPL"]["sector_missing"] == words and members["AAPL"]["industry_missing"] == words
    assert members["MSFT"]["sector"] == "Technology" and members["MSFT"]["sector_missing"] is None


def test_r4_contact_causes(db, monkeypatch):
    _create("U", ["AAPL", "MSFT", "NVDA"])
    _cik_row(1, "AAPL", "Apple Inc.")
    _cik_row(2, "MSFT", "Microsoft")
    _cik_row(3, "NVDA", "Nvidia")
    real_client = cp.EdgarClient
    built = []
    monkeypatch.setattr(cp, "EdgarClient", lambda **kw: built.append(kw))
    monkeypatch.setattr(config, "SEC_USER_AGENT", "")
    cp.run_due_reads(T0)
    assert built == []
    assert _failures() == {c: ("contact", NO_CONTACT, T0) for c in (1, 2, 3)}
    monkeypatch.setattr(cp, "EdgarClient", real_client)
    monkeypatch.setattr(config, "SEC_USER_AGENT", "TradingResearch")
    cp.run_due_reads(T0 + 10)
    assert _failures() == {c: ("contact", BAD_CONTACT, T0 + 10) for c in (1, 2, 3)}
    with eventsdb.events_transaction() as conn:
        conn.execute("DELETE FROM research_company_profile_failures WHERE cik = 3;")
    _failure(3, "company", TIMEOUT_WORDS, T0 + 20 - 3600)
    stub = StubClient({1: AAPL_SUB, 2: MSFT_SUB})
    cp.run_due_reads(T0 + 20, lambda: stub)
    assert stub.asked == [1, 2]
    assert _failures() == {3: ("company", TIMEOUT_WORDS, T0 + 20 - 3600)}


def test_r5_routes_wake_after_success_only(client, monkeypatch):
    calls = []
    monkeypatch.setattr(cp, "wake", lambda: calls.append(1))
    r = client.post("/api/research/price-universes",
                    json={"name": "W", "tickers": ["AAPL"], "window_start_ts": WIN_START, "window_end_ts": WIN_END})
    assert r.status_code == 200 and calls == [1]
    uid = r.json()["universe_id"]
    r = client.post("/api/research/price-universes",
                    json={"name": "Bad", "tickers": ["QQQQ"], "window_start_ts": WIN_START, "window_end_ts": WIN_END})
    assert r.status_code == 422 and calls == [1]
    assert client.post(f"/api/research/price-universes/{uid}/members", json={"tickers": ["MSFT"]}).status_code == 200
    assert calls == [1, 1]
    assert client.post(f"/api/research/price-universes/{uid}/members", json={"tickers": ["QQQQ"]}).status_code == 422
    assert calls == [1, 1]


class _Cut(BaseException):
    pass


def test_r6_restart_after_cut_off_pass(db):
    _create("U", ["AAPL"])
    _cik_row(1, "AAPL", "Apple Inc.")
    with pytest.raises(_Cut):
        cp.run_due_reads(T0, lambda: StubClient({1: _Cut}))
    assert _failures() == {}
    with eventsdb.get_events_conn() as conn:
        assert cp.profiles_for(conn, [1]) == {}
    stub = StubClient({1: AAPL_SUB})
    cp.run_due_reads(T0 + 5, lambda: stub)
    assert stub.asked == [1]


def test_r7_loop_counts_a_crash_and_goes_on():
    from dashboard.backend import main as dashboard_main

    class _Stop(Exception):
        pass

    waits, passes = [], []

    async def fake_wait(timeout):
        waits.append(timeout)
        if len(waits) >= 2:
            raise _Stop()

    def boom(now):
        passes.append(now)
        raise RuntimeError("pass failed")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    before = loop_counters.LOOP_CRASH_COUNTERS["company_profiles_crashes"]
    with mock.patch.object(cp, "run_due_reads", side_effect=boom), \
         mock.patch.object(cp, "wait_woken", side_effect=fake_wait), \
         mock.patch("asyncio.to_thread", side_effect=fake_to_thread):
        with pytest.raises(_Stop):
            asyncio.run(dashboard_main._company_profiles_task())
    assert len(passes) == 2 and waits == [3600, 3600]
    assert loop_counters.LOOP_CRASH_COUNTERS["company_profiles_crashes"] == before + 2
