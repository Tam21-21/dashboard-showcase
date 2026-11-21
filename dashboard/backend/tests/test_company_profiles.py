"""Lane UNI1: each company's sector and industry, read once from the SEC submissions record.

Each test runs on scratch research.db and events.db files in a tmp folder, never the real ones. The SEC
answers are test dicts of the fields read from data.sec.gov on 2026-10-30 (ownerOrg, sic, sicDescription).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import config, eventsdb, researchdb
from dashboard.backend.events import company_profiles as cp
from dashboard.backend.events.errors import EdgarHttp5xx
from dashboard.backend.routes import research

T0 = 1_700_000_000
WIN_START = 1072915200  # 2004-01-01 UTC
WIN_END = 1262304000    # 2010-01-01 UTC

AAPL_SUB = {"ownerOrg": "06 Technology", "sic": "3571", "sicDescription": "Electronic Computers"}
MSFT_SUB = {"ownerOrg": "06 Technology", "sic": "7372", "sicDescription": "Services-Prepackaged Software"}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    monkeypatch.setattr(eventsdb, "DB_PATH", tmp_path / "events.db")
    researchdb.init_db()
    eventsdb.init_events_db()
    with researchdb.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO research_eodhd_tickers(ticker) VALUES (?);",
            [("AAPL",), ("MSFT",), ("NVDA",), ("BRK-B",), ("ZZZZ",)],
        )
    return researchdb


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _cik_row(cik, ticker, name, loaded_at, effective_to=None, source="sec_company_tickers"):
    with eventsdb.events_transaction() as conn:
        conn.execute(
            "INSERT INTO research_cik_ticker_map(cik, ticker, effective_to, source, name, loaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?);", (cik, ticker, effective_to, source, name, loaded_at))


def _profile(cik, sector, sic, sic_description, read_at=T0):
    with eventsdb.events_transaction() as conn:
        cp.store_profile(conn, cik, {"sector": sector, "sic": sic, "sic_description": sic_description}, read_at)


def _create(name, tickers):
    return researchdb.create_price_universe(name, list(tickers), WIN_START, WIN_END)["universe_id"]


class StubClient:
    """Answers get_submissions_json from a dict; a value that is an exception class is raised."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.asked = []

    def get_submissions_json(self, cik):
        self.asked.append(cik)
        answer = self.answers[cik]
        if isinstance(answer, type) and issubclass(answer, Exception):
            raise answer(f"stub failure for {cik}")
        return answer


# ---- p1: parse_profile ----------------------------------------------------------------

def test_parse_profile_reads_the_three_fields():
    assert cp.parse_profile(AAPL_SUB) == {"sector": "Technology", "sic": "3571", "sic_description": "Electronic Computers"}
    assert cp.parse_profile({"ownerOrg": "01 Energy & Transportation"})["sector"] == "Energy & Transportation"
    assert cp.parse_profile({"ownerOrg": "09 Crypto Assets"})["sector"] == "Crypto Assets"
    assert len(cp.SECTOR_LABELS) == 9


def test_parse_profile_empty_and_absent_give_none():
    for sub in ({}, {"ownerOrg": ""}, {"ownerOrg": None}):
        assert cp.parse_profile(sub) == {"sector": None, "sic": None, "sic_description": None}
    assert cp.parse_profile({"sic": "", "sicDescription": ""})["sic"] is None
    assert cp.parse_profile({"sic": "", "sicDescription": ""})["sic_description"] is None


def test_parse_profile_refuses_unknown_shapes():
    for sub in ({"ownerOrg": "10 Space"}, {"ownerOrg": "Technology"}, {"sic": "35A1"}, {"sic": "357"}):
        with pytest.raises(cp.CompanyProfileShapeError):
            cp.parse_profile(sub)
    assert issubclass(cp.CompanyProfileShapeError, ValueError)


# ---- p2: the table --------------------------------------------------------------------

def test_table_created_twice_is_a_no_op(db):
    eventsdb.init_events_db()
    with eventsdb.get_events_conn() as conn:
        cols = [(r["name"], r["notnull"], r["pk"]) for r in conn.execute("PRAGMA table_info(research_company_profiles);")]
        indexes = conn.execute("PRAGMA index_list(research_company_profiles);").fetchall()
    assert cols == [("cik", 0, 1), ("sector", 0, 0), ("sic", 0, 0), ("sic_description", 0, 0), ("read_at", 1, 0)]
    assert [i["origin"] for i in indexes] == []  # no index beyond the key (INTEGER PRIMARY KEY is the rowid)
    assert eventsdb.SCHEMA_VERSION == 10


# ---- p3: read_missing_profiles --------------------------------------------------------

def test_read_missing_profiles_stores_and_names_failures(db):
    stub = StubClient({1: AAPL_SUB, 2: EdgarHttp5xx})
    out = cp.read_missing_profiles(stub, [1, 2], now=lambda: 4242)
    assert out["asked"] == 2 and out["on_file"] == 0 and out["read"] == 1
    assert len(out["failed"]) == 1 and out["failed"][0][0] == 2 and out["failed"][0][1]
    with eventsdb.get_events_conn() as conn:
        rows = conn.execute("SELECT * FROM research_company_profiles;").fetchall()
    assert [dict(r) for r in rows] == [
        {"cik": 1, "sector": "Technology", "sic": "3571", "sic_description": "Electronic Computers", "read_at": 4242}]
    stub2 = StubClient({1: AAPL_SUB, 2: MSFT_SUB})
    out = cp.read_missing_profiles(stub2, [1, 2], now=lambda: 5000)
    assert out["on_file"] == 1 and out["read"] == 1 and out["failed"] == []
    assert stub2.asked == [2]


def test_reread_replaces_the_row(db):
    cp.read_missing_profiles(StubClient({1: AAPL_SUB}), [1], now=lambda: 10)
    changed = {"ownerOrg": "03 Life Sciences", "sic": "2834", "sicDescription": "Pharmaceutical Preparations"}
    out = cp.read_missing_profiles(StubClient({1: changed}), [1], reread=True, now=lambda: 20)
    assert out["on_file"] == 0 and out["read"] == 1
    with eventsdb.get_events_conn() as conn:
        rows = conn.execute("SELECT sector, sic, read_at FROM research_company_profiles;").fetchall()
    assert [tuple(r) for r in rows] == [("Life Sciences", "2834", 20)]


def test_a_bad_shape_is_a_failure_not_a_crash(db):
    out = cp.read_missing_profiles(StubClient({1: {"ownerOrg": "10 Space"}, 2: AAPL_SUB}), [1, 2], now=lambda: 1)
    assert out["read"] == 1 and [f[0] for f in out["failed"]] == [1]


# ---- p4: member detail ----------------------------------------------------------------

def test_member_detail_carries_sector_and_reasons(db):
    uid = _create("u1", ["AAPL", "MSFT", "BRK-B", "ZZZZ"])
    _cik_row(320193, "AAPL", "Apple Inc.", 5)
    _cik_row(789019, "MSFT", "Microsoft", 5)
    _cik_row(1067983, "BRK-B", "Berkshire Hathaway", 7)
    _profile(320193, "Technology", "3571", "Electronic Computers", read_at=T0)
    _profile(1067983, None, "6331", None, read_at=T0 + 1)
    members = {m["ticker"]: m for m in researchdb.price_universe_member_detail(uid)["members"]}
    keys = ("sector", "sic", "industry", "profile_read_at", "sector_missing", "industry_missing")
    assert {k: members["AAPL"][k] for k in keys} == {
        "sector": "Technology", "sic": "3571", "industry": "Electronic Computers",
        "profile_read_at": T0, "sector_missing": None, "industry_missing": None}
    unread = "The SEC company record has not been read yet"
    assert {k: members["MSFT"][k] for k in keys} == {
        "sector": None, "sic": None, "industry": None, "profile_read_at": None,
        "sector_missing": unread, "industry_missing": unread}
    assert {k: members["BRK-B"][k] for k in keys} == {
        "sector": None, "sic": "6331", "industry": None, "profile_read_at": T0 + 1,
        "sector_missing": "The SEC company record names no sector office",
        "industry_missing": "The SEC company record names no industry"}
    nocik = "No SEC company number is on file for this ticker"
    assert {k: members["ZZZZ"][k] for k in keys} == {
        "sector": None, "sic": None, "industry": None, "profile_read_at": None,
        "sector_missing": nocik, "industry_missing": nocik}


def test_member_detail_uses_the_cik_of_the_row_the_name_comes_from(db):
    uid = _create("u1", ["AAPL"])
    _cik_row(111, "AAPL", "Apple old", 10, effective_to=500)
    _cik_row(320193, "AAPL", "Apple Inc.", 5)
    _profile(111, "Energy & Transportation", "1311", "Crude Petroleum & Natural Gas")
    _profile(320193, "Technology", "3571", "Electronic Computers")
    member = researchdb.price_universe_member_detail(uid)["members"][0]
    assert member["name"] == "Apple Inc." and member["sector"] == "Technology"


# ---- p5: sector mix -------------------------------------------------------------------

def test_sector_mix_counts_orders_and_filters(client, db):
    u1 = _create("u1", ["AAPL", "MSFT", "NVDA", "BRK-B"])
    u2 = _create("u2", ["AAPL"])
    syn = _create("syn", ["SYN_VOL_01"])
    for cik, tk in ((1, "AAPL"), (2, "MSFT"), (3, "NVDA"), (4, "BRK-B")):
        _cik_row(cik, tk, tk + " Inc", 5)
    _profile(1, "Technology", "3571", "Electronic Computers")
    _profile(2, "Technology", "7372", "Software")
    _profile(3, "Manufacturing", "3674", "Semiconductors")
    # BRK-B has a CIK but no profile row: counted under null
    researchdb.remove_price_universe_member(u1, "NVDA", T0 + 10_000_000_000)
    r = client.get("/api/research/price-universes/sector-mix")
    assert r.status_code == 200, r.text
    assert r.json() == {"universes": [
        {"universe_id": u1, "members": 3, "sectors": [{"sector": "Technology", "count": 2}, {"sector": None, "count": 1}]},
        {"universe_id": u2, "members": 1, "sectors": [{"sector": "Technology", "count": 1}]},
    ]}
    r = client.get("/api/research/price-universes/sector-mix?include_synthetic=true")
    ids = [u["universe_id"] for u in r.json()["universes"]]
    assert ids == [u1, u2, syn]
    assert r.json()["universes"][2]["sectors"] == [{"sector": None, "count": 1}]


def test_sector_mix_orders_ties_by_label(client, db):
    _create("u1", ["AAPL", "MSFT", "NVDA"])
    for cik, tk in ((1, "AAPL"), (2, "MSFT"), (3, "NVDA")):
        _cik_row(cik, tk, tk, 5)
    _profile(1, "Technology", None, None)
    _profile(2, "Manufacturing", None, None)
    _profile(3, None, None, None)
    sectors = client.get("/api/research/price-universes/sector-mix").json()["universes"][0]["sectors"]
    assert sectors == [{"sector": "Manufacturing", "count": 1}, {"sector": "Technology", "count": 1},
                       {"sector": None, "count": 1}]


# ---- p6: the backfill script ----------------------------------------------------------

def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill-company-profiles.py"
    spec = importlib.util.spec_from_file_location("backfill_company_profiles", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_prints_the_summary_and_exits_zero(db, capsys, monkeypatch):
    uid = _create("u1", ["AAPL", "MSFT", "ZZZZ"])
    _cik_row(320193, "AAPL", "Apple Inc.", 5)
    _cik_row(789019, "MSFT", "Microsoft", 5)
    monkeypatch.setattr(config, "SEC_USER_AGENT", "Test test@example.com")
    code = _load_script().main(["--universe", str(uid)], client=StubClient({320193: AAPL_SUB, 789019: MSFT_SUB}))
    assert code == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["tickers 3, no company number 1, companies 2, already on file 0, read 2, failed 0"]


def test_script_exits_one_and_names_each_failure(db, capsys, monkeypatch):
    uid = _create("u1", ["AAPL", "MSFT"])
    _cik_row(320193, "AAPL", "Apple Inc.", 5)
    _cik_row(789019, "MSFT", "Microsoft", 5)
    code = _load_script().main(["--universe", str(uid)], client=StubClient({320193: AAPL_SUB, 789019: EdgarHttp5xx}))
    assert code == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "tickers 2, no company number 0, companies 2, already on file 0, read 1, failed 1"
    assert len(out) == 2 and out[1].startswith("789019")


def test_script_exits_two_without_a_usable_user_agent(db, capsys, monkeypatch):
    script = _load_script()
    monkeypatch.setattr(config, "SEC_USER_AGENT", "")
    assert script.main([]) == 2
    assert "SEC contact" in capsys.readouterr().err
    monkeypatch.setattr(config, "SEC_USER_AGENT", "no contact here")
    assert script.main([]) == 2
    assert "@" in capsys.readouterr().err
