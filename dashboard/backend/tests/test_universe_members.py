"""Lane DL1: a price universe's members are dated and editable (DECISIONS #455, #454).

Each test runs on a scratch research.db and events.db in a tmp folder, never the real ones.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import eventsdb, researchdb
from dashboard.backend.events import universe as event_universe
from dashboard.backend.routes import research

# The worker's helpers read their two required settings when imported, from ~/.research-worker.env on the
# laptop; a machine without that file (CI) failed to collect this module. Placeholders set first win over
# the file (load_dotenv never overrides), so the import is the same everywhere. Only resolve_universe is
# used, with a fake client, so neither value is ever sent.
with pytest.MonkeyPatch.context() as _mp:
    _mp.setenv("RESEARCH_WORKER_API_KEY", "test-worker-key")
    _mp.setenv("RESEARCH_SERVER_URL", "http://worker-test.invalid")
    from research_lab.worker import helpers

T0 = 1_700_000_000
WIN_START = 1072915200  # 2004-01-01 UTC
WIN_END = 1262304000    # 2010-01-01 UTC


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    with researchdb.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO research_eodhd_tickers(ticker) VALUES (?);",
            [("AAPL",), ("MSFT",), ("NVDA",), ("BRK-B",)],
        )
    return researchdb


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _create(name="u1", tickers=("AAPL", "MSFT"), now=T0, monkeypatch=None):
    # create keeps its own clock (time.time); the tests then read the added_at it wrote.
    result = researchdb.create_price_universe(name, list(tickers), WIN_START, WIN_END)
    with researchdb.get_conn() as conn:
        added = conn.execute(
            "SELECT MIN(added_at) AS a FROM research_price_universe_membership WHERE universe_id = ?;",
            (result["universe_id"],)).fetchone()["a"]
    return result["universe_id"], int(added)


# ---- u1: add, remove, as-of ----------------------------------------------------------

def test_add_remove_and_as_of(db):
    uid, t0 = _create()
    added = researchdb.add_price_universe_members(uid, ["NVDA", "AAPL", "ZZZZ"], t0 + 1000)
    assert added["added"] == ["NVDA"]
    assert added["already"] == ["AAPL"]
    assert added["excluded"] == ["ZZZZ"]
    researchdb.remove_price_universe_member(uid, "MSFT", t0 + 2000)
    assert researchdb.list_price_universe_members(uid) == ["AAPL", "NVDA"]
    asof = researchdb.list_price_universe_members_as_of
    assert asof(uid, t0 + 500) == ["AAPL", "MSFT"]
    assert asof(uid, t0 + 1500) == ["AAPL", "MSFT", "NVDA"]
    assert asof(uid, t0 + 2500) == ["AAPL", "NVDA"]
    researchdb.add_price_universe_members(uid, ["MSFT"], t0 + 3000)
    assert asof(uid, t0 + 2500) == ["AAPL", "NVDA"]
    assert asof(uid, t0 + 3500) == ["AAPL", "MSFT", "NVDA"]
    with researchdb.get_conn() as conn:
        spans = conn.execute(
            "SELECT COUNT(*) AS n FROM research_price_universe_member_spans "
            "WHERE universe_id = ? AND ticker = 'MSFT';", (uid,)).fetchone()["n"]
    assert spans == 2


# ---- u2: refusals --------------------------------------------------------------------

def test_refusals(db):
    uid, t0 = _create(tickers=("AAPL",))
    with pytest.raises(researchdb.PriceUniverseLastMember):
        researchdb.remove_price_universe_member(uid, "AAPL", t0 + 10)
    assert researchdb.list_price_universe_members(uid) == ["AAPL"]
    with pytest.raises(researchdb.MemberNotFound):
        researchdb.remove_price_universe_member(uid, "NVDA", t0 + 10)
    with pytest.raises(researchdb.PriceUniverseNotFound):
        researchdb.add_price_universe_members(99999, ["NVDA"], t0 + 10)
    with pytest.raises(researchdb.PriceUniverseNotFound):
        researchdb.remove_price_universe_member(99999, "AAPL", t0 + 10)
    with pytest.raises(researchdb.PriceUniverseMixedSource):
        researchdb.add_price_universe_members(uid, ["SYN_VOL_01"], t0 + 10)
    with pytest.raises(researchdb.PriceUniverseAddNothing):
        researchdb.add_price_universe_members(uid, ["AAPL", "ZZZZ"], t0 + 10)
    syn_uid, _ = _create(name="syn", tickers=("SYN_VOL_01",))
    with pytest.raises(researchdb.PriceUniverseMixedSource):
        researchdb.add_price_universe_members(syn_uid, ["AAPL"], t0 + 10)
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_datasets SET status = 'deleted' WHERE id = ?;", (uid,))
    with pytest.raises(researchdb.PriceUniverseNotFound):
        researchdb.add_price_universe_members(uid, ["NVDA"], t0 + 10)
    with pytest.raises(researchdb.PriceUniverseNotFound):
        researchdb.remove_price_universe_member(uid, "AAPL", t0 + 10)


def test_routes_status_codes(client, db):
    uid, _ = _create(tickers=("AAPL",))
    r = client.post(f"/api/research/price-universes/{uid}/members", json={"tickers": ["NVDA", "ZZZZ"]})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == ["NVDA"]
    r = client.post(f"/api/research/price-universes/{uid}/members", json={"tickers": ["ZZZZ"]})
    assert r.status_code == 422 and "price_universe_add_nothing" in r.text
    r = client.delete(f"/api/research/price-universes/{uid}/members/NVDA")
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/research/price-universes/{uid}/members/AAPL")
    assert r.status_code == 422 and "price_universe_last_member" in r.text
    r = client.delete(f"/api/research/price-universes/{uid}/members/NVDA")
    assert r.status_code == 404 and "member_not_found" in r.text
    r = client.post("/api/research/price-universes/99999/members", json={"tickers": ["NVDA"]})
    assert r.status_code == 404
    r = client.post(f"/api/research/price-universes/{uid}/members", json={"tickers": ["SYN_VOL_01"]})
    assert r.status_code == 422 and "price_universe_mixed_source" in r.text


# ---- u3: rows written directly (as old probes do) ------------------------------------

def test_direct_rows_and_backfill(db):
    uid, t0 = _create(tickers=("AAPL",))
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_price_universe_membership(universe_id, ticker, added_at) VALUES (?, 'MSFT', ?);",
            (uid, t0 + 100))
    assert researchdb.list_price_universe_members_as_of(uid, t0 + 50) == ["AAPL"]
    assert researchdb.list_price_universe_members_as_of(uid, t0 + 150) == ["AAPL", "MSFT"]
    with researchdb.transaction() as conn:
        conn.execute("DELETE FROM research_price_universe_membership WHERE universe_id = ? AND ticker = 'AAPL';", (uid,))
    assert researchdb.list_price_universe_members_as_of(uid, t0 + 150) == ["MSFT"]
    researchdb.init_db()
    researchdb.init_db()
    with researchdb.get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM research_price_universe_member_spans "
            "WHERE universe_id = ? AND ticker = 'MSFT';", (uid,)).fetchone()["n"]
    assert n == 1


# ---- u4: detail ----------------------------------------------------------------------

def _price_dataset(symbol, source, start, end, cs, ce, rows, status="ready", timeframe="1d"):
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_datasets (name, dataset_type, source, symbol, timeframe, status, start_ts, end_ts, "
            "rows, created_at, covered_start_ts, covered_end_ts) "
            "VALUES (?, 'price_dataset', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?);",
            (f"{symbol}-{start}-{end}", source, symbol, timeframe, status, start, end, rows, cs, ce))


def _cik_row(cik, ticker, name, loaded_at, effective_to=None, source="sec_company_tickers"):
    with eventsdb.events_transaction() as conn:
        conn.execute(
            "INSERT INTO research_cik_ticker_map(cik, ticker, effective_to, source, name, loaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?);", (cik, ticker, effective_to, source, name, loaded_at))


def test_detail_names_and_coverage(db):
    uid, t0 = _create(tickers=("AAPL", "BRK-B", "MSFT"))
    _cik_row(111, "AAPL", "Apple old", 10, effective_to=500)
    _cik_row(320193, "AAPL", "Apple Inc.", 5)
    _cik_row(1067983, "BRK-B", "Berkshire Hathaway", 7)
    _cik_row(789019, "MSFT", "Not from sec file", 9, source="manual")
    # AAPL: two ready daily datasets, the wider requested window wins; a not-ready one and a 1h one are ignored
    _price_dataset("AAPL", "eodhd", 100, 200, 110, 190, 80)
    _price_dataset("AAPL", "eodhd", 100, 900, 120, 880, 700)
    _price_dataset("AAPL", "eodhd", 0, 9999, 1, 2, 3, status="failed")
    _price_dataset("AAPL", "eodhd", 0, 9999, 1, 2, 3, timeframe="1h")
    detail = researchdb.price_universe_member_detail(uid)
    by = {m["ticker"]: m for m in detail["members"]}
    assert by["AAPL"]["name"] == "Apple Inc."
    assert (by["AAPL"]["first_bar_ts"], by["AAPL"]["last_bar_ts"], by["AAPL"]["bars"]) == (120, 880, 700)
    assert by["BRK-B"]["name"] == "Berkshire Hathaway"
    assert by["MSFT"]["name"] is None
    assert (by["MSFT"]["first_bar_ts"], by["MSFT"]["last_bar_ts"], by["MSFT"]["bars"]) == (None, None, None)
    assert by["AAPL"]["added_at"] == t0


def test_detail_as_of_and_route(client, db):
    uid, t0 = _create()
    researchdb.add_price_universe_members(uid, ["NVDA"], t0 + 1000)
    detail = researchdb.price_universe_member_detail(uid, t0 + 500)
    assert [m["ticker"] for m in detail["members"]] == ["AAPL", "MSFT"]
    r = client.get(f"/api/research/price-universes/{uid}/members/detail", params={"as_of": t0 + 1500})
    assert r.status_code == 200, r.text
    got = r.json()
    nv = [m for m in got["members"] if m["ticker"] == "NVDA"][0]
    assert nv["added_at"] == t0 + 1000
    assert client.get("/api/research/price-universes/99999/members/detail").status_code == 404


# ---- u5: runs ------------------------------------------------------------------------

def test_runs_for_universe(client, db):
    uid, t0 = _create()
    other, _ = _create(name="u2", tickers=("NVDA",))
    now = t0
    with researchdb.transaction() as conn:
        conn.execute("INSERT INTO research_strategies (name, source_code, created_at, updated_at) VALUES ('s', 'x', 1, 1);")
        sid = conn.execute("SELECT id FROM research_strategies").fetchone()["id"]
        for i, ds in enumerate([uid, other, uid]):
            job = conn.execute(
                "INSERT INTO research_jobs (kind, payload_json, created_at, status) VALUES ('run_backtest', '', ?, 'pending') RETURNING id;",
                (now,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
                "settings_snapshot, created_at, status) VALUES (?, ?, 1, ?, 'x', '[]', ?, 'pending');",
                (job, sid, ds, now + i))
    runs = researchdb.list_runs_for_universe(uid)
    assert len(runs) == 2
    assert runs[0]["created_at"] > runs[1]["created_at"]
    assert all(r["dataset_id"] == uid for r in runs)
    r = client.get(f"/api/research/price-universes/{uid}/runs")
    assert r.status_code == 200 and len(r.json()["runs"]) == 2
    assert client.get("/api/research/price-universes/99999/runs").status_code == 404


# ---- u6: resolve_universe_ciks as of a date ------------------------------------------

def test_resolve_ciks_as_of(db):
    uid, t0 = _create()
    _cik_row(320193, "AAPL", "Apple Inc.", 1)
    _cik_row(789019, "MSFT", "Microsoft", 1)
    researchdb.remove_price_universe_member(uid, "MSFT", t0 + 2000)
    kw = dict(universe_id=uid, knowledge_from=1, knowledge_to=2)
    assert event_universe.resolve_universe_ciks(**kw) == [320193]
    assert event_universe.resolve_universe_ciks(members_as_of=t0 + 1000, **kw) == [320193, 789019]
    assert event_universe.resolve_universe_ciks(members_as_of=t0 + 2500, **kw) == [320193]


# ---- u7: event runs pin members_as_of ------------------------------------------------

EVENT_SOURCE = "class S:\n    event_types = ('form4',)\n"


def _event_strategy():
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_strategies (name, source_code, created_at, updated_at) VALUES ('ev', ?, 1, 1);",
            (EVENT_SOURCE,))
        return conn.execute("SELECT id FROM research_strategies WHERE name = 'ev'").fetchone()["id"]


def test_event_run_pins_members_as_of(db):
    uid, t0 = _create()
    sid = _event_strategy()
    out = researchdb.dispatch_event_run(sid, uid, 100, 200, now=t0 + 77)
    assert out["payload"]["members_as_of"] == t0 + 77
    with researchdb.get_conn() as conn:
        sha = conn.execute("SELECT dataset_sha256 FROM research_runs WHERE id = ?;", (out["run_id"],)).fetchone()[0]
    assert sha == researchdb._event_run_data_sha256(uid, 100, 200, ("form4",), members_as_of=t0 + 77)
    assert sha != researchdb._event_run_data_sha256(uid, 100, 200, ("form4",))


def test_cik_universe_event_run_unchanged(db):
    plain = researchdb._event_run_data_sha256(1, 100, 200, ("form4",))
    decl = {"universe_id": 1, "knowledge_from": 100, "knowledge_to": 200, "event_types": ["form4"]}
    import hashlib
    canonical = json.dumps(decl, sort_keys=True, separators=(",", ":"))
    assert plain == hashlib.sha256(canonical.encode()).hexdigest()


def test_resolve_route_echoes_members_as_of(client, db, monkeypatch):
    uid, t0 = _create()
    _cik_row(320193, "AAPL", "Apple Inc.", 1)
    _cik_row(789019, "MSFT", "Microsoft", 1)
    researchdb.remove_price_universe_member(uid, "MSFT", t0 + 2000)
    app = FastAPI()
    app.include_router(research.worker_router)
    app.dependency_overrides[research.require_worker_api_key] = lambda: None
    c = TestClient(app)
    r = c.post("/api/research/worker/universes/resolve",
               json={"universe_id": uid, "knowledge_from": 1, "knowledge_to": 2, "members_as_of": t0 + 1000})
    assert r.status_code == 200, r.text
    assert r.json()["ciks"] == [320193, 789019]
    assert r.json()["members_as_of"] == t0 + 1000
    r = c.post("/api/research/worker/universes/resolve",
               json={"universe_id": uid, "knowledge_from": 1, "knowledge_to": 2})
    assert r.json()["ciks"] == [320193]
    assert "members_as_of" not in r.json()


def _fake_http(response_json):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_json)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), sent


def test_worker_resolve_universe_sends_and_checks_echo():
    async def go():
        http, sent = _fake_http({"ciks": [1], "members_as_of": 55})
        assert await helpers.resolve_universe(7, 1, 2, http, members_as_of=55) == [1]
        assert sent["body"]["members_as_of"] == 55
        http, sent = _fake_http({"ciks": [1]})
        assert await helpers.resolve_universe(7, 1, 2, http) == [1]
        assert "members_as_of" not in sent["body"]
        http, _ = _fake_http({"ciks": [1]})  # an old server that ignores the field
        with pytest.raises(httpx.HTTPError):
            await helpers.resolve_universe(7, 1, 2, http, members_as_of=55)

    asyncio.run(go())
