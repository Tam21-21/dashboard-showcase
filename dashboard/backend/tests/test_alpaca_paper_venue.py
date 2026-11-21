"""Lane P6a: Alpaca paper reads, and the paper step refuses synthetic data.

No test reaches the network: alpaca-py's TradingClient is replaced by a fake everywhere, and the
approve route gets its venue through a monkeypatched get_paper_venue. Scratch databases only.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import (
    alpaca_paper_venue as apv,
    judgedb, paperdb, researchdb, reservedb, retestdb,
)
from dashboard.backend.routes import judge as judge_routes

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "scripts" / "probes" / "nautilus_retest"))
import _lib as nr  # noqa: E402


def _api_error(status_code: int) -> APIError:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status_code), request=None)
    return APIError(json.dumps({"code": status_code, "message": "fake"}), http_error)


class FakeClient:
    """Stands in for alpaca.trading.client.TradingClient. Records every call."""

    def __init__(self, account=None, asset=None, account_error=None, asset_error=None):
        self.account = account
        self.asset = asset
        self.account_error = account_error
        self.asset_error = asset_error
        self.calls: list = []

    def get_account(self):
        self.calls.append("get_account")
        if self.account_error is not None:
            raise self.account_error
        return self.account

    def get_asset(self, symbol):
        self.calls.append(("get_asset", symbol))
        if self.asset_error is not None:
            raise self.asset_error
        return self.asset


def _venue(**kw) -> apv.AlpacaPaperVenue:
    return apv.AlpacaPaperVenue(FakeClient(**kw))


def _account(equity="10000.5", status="ACTIVE"):
    return SimpleNamespace(equity=equity, status=status)


def _asset(tradable=True, status="active"):
    return SimpleNamespace(tradable=tradable, status=status)


# t1
def test_t1_not_configured(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(apv.PaperVenueNotConfigured, match="ALPACA_API_KEY"):
        apv.get_paper_venue()
    monkeypatch.setenv("ALPACA_API_KEY", "PKabc")
    with pytest.raises(apv.PaperVenueNotConfigured, match="ALPACA_SECRET_KEY"):
        apv.get_paper_venue()
    monkeypatch.setenv("ALPACA_API_KEY", "AKlive")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(apv.PaperVenueNotConfigured, match="ALPACA_API_KEY"):
        apv.get_paper_venue()


# t2
def test_t2_equity_string_becomes_float():
    assert _venue(account=_account("10000.5")).get_account() == {"equity": 10000.5, "status": "ACTIVE"}


# t3
def test_t3_account_unreachable_and_not_swallowed():
    with pytest.raises(apv.PaperVenueUnreachable):
        _venue(account_error=_api_error(500)).get_account()
    with pytest.raises(apv.PaperVenueUnreachable):
        _venue(account_error=requests.exceptions.ConnectionError("down")).get_account()
    for bad in ("nan", "inf", "-1"):
        with pytest.raises(apv.PaperVenueUnreachable):
            _venue(account=_account(bad)).get_account()
    with pytest.raises(ValueError):
        _venue(account_error=ValueError("bug")).get_account()


# t4
def test_t4_resolve_symbol():
    assert _venue(asset=_asset(True, "active")).resolve_symbol("AAPL") is True
    assert _venue(asset=_asset(False, "active")).resolve_symbol("AAPL") is False
    assert _venue(asset=_asset(True, "inactive")).resolve_symbol("AAPL") is False
    assert _venue(asset_error=_api_error(404)).resolve_symbol("ZZZZQX") is False
    with pytest.raises(apv.PaperVenueUnreachable):
        _venue(asset_error=_api_error(503)).resolve_symbol("AAPL")
    with pytest.raises(apv.PaperVenueUnreachable):
        _venue(asset_error=requests.exceptions.Timeout("slow")).resolve_symbol("AAPL")


# ---------------------------------------------------------------- t5: the approve route

class FakeVenue:
    def __init__(self, equity=10000.0, status="ACTIVE", tradable=True,
                 account_error=None, symbol_error=None):
        self.equity, self.status, self.tradable = equity, status, tradable
        self.account_error, self.symbol_error = account_error, symbol_error
        self.calls: list = []

    def get_account(self):
        self.calls.append("get_account")
        if self.account_error is not None:
            raise self.account_error
        return {"equity": self.equity, "status": self.status}

    def resolve_symbol(self, symbol):
        self.calls.append(("resolve_symbol", symbol))
        if self.symbol_error is not None:
            raise self.symbol_error
        return self.tradable


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    research_db = tmp_path / "research.db"
    paper_db = tmp_path / "paper.db"
    monkeypatch.setattr(researchdb, "DB_PATH", research_db)
    monkeypatch.setattr(judgedb, "DB_PATH", research_db)
    monkeypatch.setattr(reservedb, "DB_PATH", research_db)
    monkeypatch.setattr(paperdb, "DB_PATH", paper_db)
    researchdb.init_db()
    judgedb.init_judge_db(research_db)
    reservedb.init_reserve_db(research_db)
    retestdb.init_retest_db(research_db)
    retestdb.set_drift_allowance(research_db, 0.11, 0.12, "test")
    paperdb.init_paper_db(paper_db)
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_strategies (id, name, source_code, version, last_mirror_status, "
            "created_at, updated_at) VALUES (1, 'ma_cross', 'x', 1, 'pending', 1700000000, 1700000000);"
        )
    return research_db, paper_db


@pytest.fixture()
def client(stores):
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


def _add_run(run_id, ticker="AAPL", init_cash=5000.0):
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_datasets (id, name, source, symbol, timeframe, start_ts, end_ts, "
            "status, dataset_type, created_at) VALUES (?, ?, 'eodhd', ?, '1d', 1072915200, "
            "1262304000, 'ready', 'price_dataset', 1700000000);",
            (run_id, f"ds{run_id}", ticker),
        )
        conn.execute(
            "INSERT INTO research_jobs (id, kind, payload_json, status, created_at) "
            "VALUES (?, 'run_sweep', ?, 'succeeded', 1700000000);",
            (run_id, json.dumps({"init_cash": init_cash})),
        )
        conn.execute(
            "INSERT INTO research_runs (id, job_id, strategy_id, strategy_version, dataset_id, "
            "dataset_sha256, settings_snapshot, created_at, status) "
            "VALUES (?, ?, 1, 1, ?, ?, '[]', 1700000000, 'succeeded');",
            (run_id, run_id, run_id, "b" * 64),
        )


def _verdict(run_id, ticker):
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        return judgedb.record_verdict(
            conn, run_id=run_id,
            judging_pass_id=f"{run_id:08d}-1111-4111-8111-111111111111",
            variation_id=0, ticker=ticker,
            graded_formula_version="1|3|docker:research-runner:m3-latest@sha256:" + "a" * 64,
            strategy_id=1, strategy_version=1, dataset_sha256="b" * 64, verdict="pass",
            is_advisory=0, battery_version=judgedb.BATTERY_VERSION,
            evidence_floor_outcome="cleared",
            check_outcomes_json=json.dumps([{"check": n, "outcome": "cleared"} for n in range(1, 10)]),
            decided_by="initial", warnings_json="[]", graded_at=1700000000 + run_id,
        )


def _passing_candidate(research_db, ticker="AAPL", slice_cash=5000.0):
    """A verdict that passes refusals 0 to 5: real-data run, current pass, passed reserve exam
    (run 2, another ticker), passed retest of run 1."""
    _add_run(1, ticker, slice_cash)
    _add_run(2, "MSFT", slice_cash)
    vid = _verdict(1, ticker)
    _verdict(2, "MSFT")
    c = sqlite3.connect(str(research_db))
    c.execute(
        "INSERT INTO research_reserve_exams (draw_id, strategy_id, strategy_version, window_index, "
        " window_start_ts, window_end_ts, seed, run_id, created_at) "
        "VALUES (1, 1, 1, 1, 1600000000, 1610000000, 7, 2, 1700000000)")
    c.commit()
    c.close()
    retestdb.record_retest(
        run_id=1, verdict_id=None, created_at=1_700_000_300,
        shadow_copy_completed_at=1_700_000_100, fills_read_at=1_700_000_200,
        status="report-only", engine_version="1.231.0", python_version="3.12.13",
        matched_configuration=nr.complete_matched_configuration(),
        export_path="/artifacts/1/retest_fills.parquet",
        divergence_vs_shadow=0.005, divergence_vs_shadow_absent_reason=None,
        divergence_vs_vbt=0.5, divergence_vs_vbt_absent_reason=None,
        gap_final_equity=0.05, gap_sharpe=0.05, research_db_path=research_db,
    )
    return vid


def _counts(paper_db) -> dict:
    c = sqlite3.connect(str(paper_db))
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
                for t in ("paper_runs", "paper_slice_current", "paper_equity")}
    finally:
        c.close()


def _approve(client, vid, slice_cash=5000.0):
    return client.post(f"/api/judge/verdicts/{vid}/approve", json={"slice_opening_cash": slice_cash})


def _use(monkeypatch, venue):
    monkeypatch.setattr(apv, "get_paper_venue", lambda: venue)


def _moved(key, fn):
    before = judge_routes.JUDGE_ADMISSION_COUNTERS[key]
    out = fn()
    return out, judge_routes.JUDGE_ADMISSION_COUNTERS[key] - before


def test_t5_not_configured_and_unreachable_and_closed(stores, client, monkeypatch):
    research_db, paper_db = stores
    vid = _passing_candidate(research_db)

    def raise_not_configured():
        raise apv.PaperVenueNotConfigured("ALPACA_API_KEY is not set")
    monkeypatch.setattr(apv, "get_paper_venue", raise_not_configured)
    r, n = _moved("pot_unreadable_from_venue_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"] == "pot-unreadable-from-venue: ALPACA_API_KEY is not set"

    _use(monkeypatch, FakeVenue(account_error=apv.PaperVenueUnreachable("timeout")))
    r, n = _moved("pot_unreadable_from_venue_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"].startswith("pot-unreadable-from-venue: ")

    _use(monkeypatch, FakeVenue(status="ACCOUNT_CLOSED"))
    r, n = _moved("pot_unreadable_from_venue_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"] == "pot-unreadable-from-venue: the paper account is ACCOUNT_CLOSED"
    assert _counts(paper_db) == {"paper_runs": 0, "paper_slice_current": 0, "paper_equity": 0}


def test_t5_symbol_refusals(stores, client, monkeypatch):
    research_db, paper_db = stores
    vid = _passing_candidate(research_db)
    # Not tradable.
    _use(monkeypatch, FakeVenue(tradable=False))
    r, n = _moved("symbol_not_tradable_at_venue_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"] == "symbol-not-tradable-at-venue: AAPL"
    # Unreachable on the symbol read.
    _use(monkeypatch, FakeVenue(symbol_error=apv.PaperVenueUnreachable("503")))
    r, n = _moved("symbol_resolution_unavailable_from_venue_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"].startswith("symbol-resolution-unavailable-from-venue: ")
    assert _counts(paper_db) == {"paper_runs": 0, "paper_slice_current": 0, "paper_equity": 0}


def test_t5_empty_ticker_makes_no_network_call(stores, client, monkeypatch):
    research_db, paper_db = stores
    vid = _passing_candidate(research_db)
    c = sqlite3.connect(str(research_db))
    c.execute("UPDATE research_judge_verdicts SET ticker = '' WHERE id = ?;", (vid,))
    c.commit()
    c.close()
    venue = FakeVenue()
    _use(monkeypatch, venue)
    r, n = _moved("symbol_not_tradable_at_venue_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"] == "symbol-not-tradable-at-venue: no ticker"
    assert not any(isinstance(c, tuple) for c in venue.calls)
    assert _counts(paper_db)["paper_runs"] == 0


def test_t5_remainder_and_admission(stores, client, monkeypatch):
    research_db, paper_db = stores
    vid = _passing_candidate(research_db)
    _use(monkeypatch, FakeVenue(equity=10000.0))
    # One running slice worth 8,000: remainder 2,000, 5,000 asked.
    with paperdb.paper_transaction(paper_db) as conn:
        running = paperdb.admit_run(
            conn, strategy_id=9, strategy_name="other", strategy_version=1, source_run_id=99,
            slice_opening_cash=8000.0, started_at="2026-10-01T00:00:00Z",
            source_data_synthetic=False)
    r, n = _moved("pot_remainder_too_small_count", lambda: _approve(client, vid))
    assert r.status_code == 409 and n == 1
    assert r.json()["detail"].startswith("pot-remainder-too-small: ")
    assert "5,000" in r.json()["detail"] or "5000" in r.json()["detail"]
    assert "2,000" in r.json()["detail"] or "2000" in r.json()["detail"]
    assert _counts(paper_db)["paper_runs"] == 1  # only the pre-existing one
    # The same slice once ended no longer counts.
    c = sqlite3.connect(str(paper_db))
    c.execute("UPDATE paper_runs SET ended_at = '2026-10-02T00:00:00Z', end_reason = 'killed' "
              "WHERE paper_run_id = ?;", (running,))
    c.commit()
    c.close()
    r, n = _moved("admitted_count", lambda: _approve(client, vid))
    assert r.status_code == 200 and n == 1
    assert _counts(paper_db)["paper_runs"] == 2


def test_t5_all_passing_admits_one(stores, client, monkeypatch):
    research_db, paper_db = stores
    vid = _passing_candidate(research_db)
    venue = FakeVenue(equity=10000.0)
    _use(monkeypatch, venue)
    r = _approve(client, vid)
    assert r.status_code == 200
    assert _counts(paper_db) == {"paper_runs": 1, "paper_slice_current": 1, "paper_equity": 1}
    assert ("resolve_symbol", "AAPL") in venue.calls


# ---------------------------------------------------------------- t6, t7: admit_run

def test_t6_admit_run_refuses_synthetic(tmp_path):
    paper_db = tmp_path / "paper.db"
    paperdb.init_paper_db(paper_db)
    with pytest.raises(paperdb.PaperSyntheticDataRefused):
        with paperdb.paper_transaction(paper_db) as conn:
            paperdb.admit_run(conn, strategy_id=1, strategy_name="s", strategy_version=1,
                              source_run_id=1, slice_opening_cash=1000.0,
                              started_at="2026-10-01T00:00:00Z", source_data_synthetic=True)
    assert _counts(paper_db) == {"paper_runs": 0, "paper_slice_current": 0, "paper_equity": 0}


def test_t7_admit_run_keyword_is_required(tmp_path):
    paper_db = tmp_path / "paper.db"
    paperdb.init_paper_db(paper_db)
    with pytest.raises(TypeError):
        with paperdb.paper_transaction(paper_db) as conn:
            paperdb.admit_run(conn, strategy_id=1, strategy_name="s", strategy_version=1,
                              source_run_id=1, slice_opening_cash=1000.0,
                              started_at="2026-10-01T00:00:00Z")


# ---------------------------------------------------------------- lane UI19-0: latest_trades, positions

class _FakeHttpClient:
    """Stands in for httpx.Client: records the request, answers a set response or raises."""

    last_request: dict = {}

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __call__(self, *args, **kwargs):  # httpx.Client(timeout=10)
        _FakeHttpClient.last_request = {"client_kwargs": kwargs}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None):
        _FakeHttpClient.last_request.update(url=url, params=params, headers=headers)
        if self._error is not None:
            raise self._error
        return self._response


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _http(monkeypatch, response=None, error=None):
    monkeypatch.setenv("ALPACA_API_KEY", "PKSECRETKEYID")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SECRETVALUE123")
    monkeypatch.setattr(apv.httpx, "Client", _FakeHttpClient(response, error))


def test_latest_trades_reads_iex_and_leaves_missing_symbol_out(monkeypatch):
    payload = {"trades": {"AAPL": {"t": "2026-10-29T15:00:00.1Z", "p": 112.5, "s": 1}}}
    _http(monkeypatch, _Resp(200, payload))
    got = _venue().latest_trades(["AAPL", "MSFT"])
    assert got == {"AAPL": {"price": 112.5, "time": "2026-10-29T15:00:00.1Z"}}
    req = _FakeHttpClient.last_request
    assert req["url"] == "https://data.alpaca.markets/v2/stocks/trades/latest"
    assert req["params"] == {"symbols": "AAPL,MSFT", "feed": "iex"}
    assert req["client_kwargs"] == {"timeout": 10}
    assert req["headers"]["APCA-API-KEY-ID"] == "PKSECRETKEYID"


def test_latest_trades_http_error_carries_no_header_text(monkeypatch):
    import httpx
    _http(monkeypatch, error=httpx.ConnectError("connection refused"))
    with pytest.raises(apv.PaperVenueUnreachable) as err:
        _venue().latest_trades(["AAPL"])
    assert "connection refused" in str(err.value)
    assert "PKSECRETKEYID" not in str(err.value) and "SECRETVALUE123" not in str(err.value)


def test_latest_trades_non_200_names_the_status(monkeypatch):
    _http(monkeypatch, _Resp(403, {"message": "forbidden"}))
    with pytest.raises(apv.PaperVenueUnreachable, match="403"):
        _venue().latest_trades(["AAPL"])


def test_latest_trades_json_without_trades_is_unreachable(monkeypatch):
    _http(monkeypatch, _Resp(200, {"other": 1}))
    with pytest.raises(apv.PaperVenueUnreachable, match="trades"):
        _venue().latest_trades(["AAPL"])


class _PositionsClient:
    def __init__(self, positions=None, error=None):
        self._positions = positions
        self._error = error

    def get_all_positions(self):
        if self._error is not None:
            raise self._error
        return self._positions


def test_position_quantities_signed():
    positions = [SimpleNamespace(symbol="AAPL", qty="15"), SimpleNamespace(symbol="TSLA", qty="-3")]
    got = apv.AlpacaPaperVenue(_PositionsClient(positions)).position_quantities()
    assert got == {"AAPL": 15.0, "TSLA": -3.0}


def test_position_quantities_api_error_is_unreachable():
    venue = apv.AlpacaPaperVenue(_PositionsClient(error=_api_error(500)))
    with pytest.raises(apv.PaperVenueUnreachable):
        venue.position_quantities()
