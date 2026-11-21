"""Lane UI17-0: one read gives every active strategy with its newest real run's curve and stats.

Each test runs on a scratch research.db (a tmp folder), never the real one.
"""
from __future__ import annotations

import contextlib
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import researchdb
from dashboard.backend.routes import research

BASE_TS = 1_700_000_000


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    researchdb.init_db()
    return researchdb


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _dataset(source):
    now = int(time.time())
    with researchdb.transaction() as conn:
        return int(conn.execute(
            "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, "
            "dataset_type, created_at) VALUES ('d', ?, 'X', '1d', 1, 2, 'ready', 'price_dataset', ?);",
            (source, now)).lastrowid)


def _strategy(name, status="active"):
    now = int(time.time())
    with researchdb.transaction() as conn:
        return int(conn.execute(
            "INSERT INTO research_strategies (name, source_code, status, created_at, updated_at) "
            "VALUES (?, 'x', ?, ?, ?);", (name, status, now, now)).lastrowid)


def _run(strategy_id, dataset_id, created_at, status="succeeded", kind="standard", sparkline=None,
         with_stats=True, sharpe=1.5, trades=7):
    with researchdb.transaction() as conn:
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at) VALUES ('run_backtest', '{}', ?);",
            (created_at,)).lastrowid
        run = conn.execute(
            "INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "settings_snapshot, name, created_at, status, kind, equity_sparkline) "
            "VALUES (?, ?, 1, ?, 'h', '[]', 'r', ?, ?, ?, ?);",
            (job, strategy_id, dataset_id, created_at, status, kind, sparkline)).lastrowid
        if with_stats:
            conn.execute(
                "INSERT INTO research_run_stats (run_id, total_return, sharpe_ratio, max_drawdown, num_trades, "
                "total_fees_paid, total_slippage_cost) VALUES (?, 0.25, ?, 0.1, ?, 0, 0);",
                (run, sharpe, trades))
        return int(run)


def _get(client, **params):
    resp = client.get("/api/research/strategies/overview", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_o1_newest_real_standard_succeeded_run(client):
    real, syn = _dataset("eodhd"), _dataset("synthetic")
    a = _strategy("A")
    _run(a, real, BASE_TS + 1, sparkline="[1, 2]", sharpe=0.5)
    r2 = _run(a, real, BASE_TS + 2, sparkline="[1.0, 2.0, 3.0]", sharpe=1.5, trades=7)
    _run(a, real, BASE_TS + 3, status="failed")
    _run(a, real, BASE_TS + 4, kind="scratch")
    _run(a, syn, BASE_TS + 5)
    row = _by_name(_get(client))["A"]
    assert row["run"]["id"] == r2
    assert row["run"]["equity_sparkline"] == [1.0, 2.0, 3.0]
    assert row["run"]["sharpe_ratio"] == 1.5
    assert row["run"]["num_trades"] == 7
    assert row["run"]["total_return"] == 0.25
    assert row["run"]["max_drawdown"] == 0.1
    assert set(row) == {"id", "name", "display_name", "version", "description", "run"}
    assert {"id", "created_at", "strategy_version"} <= set(row["run"])


def test_o2_no_qualifying_run_gives_null(client):
    real = _dataset("eodhd")
    _strategy("B")
    c = _strategy("C")
    _run(c, real, BASE_TS, status="failed")
    rows = _by_name(_get(client))
    assert rows["B"]["run"] is None
    assert rows["C"]["run"] is None


def test_o3_deleted_hidden_and_empty_store(client):
    assert _get(client) == []
    real = _dataset("eodhd")
    d = _strategy("D", status="deleted")
    _run(d, real, BASE_TS + 99)
    assert _get(client) == []


def test_o4_run_without_stats_or_sparkline(client):
    real = _dataset("eodhd")
    e = _strategy("E")
    rid = _run(e, real, BASE_TS, with_stats=False, sparkline=None)
    run = _by_name(_get(client))["E"]["run"]
    assert run["id"] == rid
    assert run["equity_sparkline"] is None
    for key in ("sharpe_ratio", "num_trades", "total_return", "max_drawdown"):
        assert run[key] is None


def test_o5_same_created_at_larger_id_wins(client):
    real = _dataset("eodhd")
    f = _strategy("F")
    _run(f, real, BASE_TS)
    second = _run(f, real, BASE_TS)
    assert _by_name(_get(client))["F"]["run"]["id"] == second


def test_o6_include_synthetic_picks_synthetic_run(client):
    real, syn = _dataset("eodhd"), _dataset("synthetic")
    a = _strategy("A")
    _run(a, real, BASE_TS + 1)
    s = _run(a, syn, BASE_TS + 2)
    assert _by_name(_get(client, include_synthetic="true"))["A"]["run"]["id"] == s


def test_o7_ordered_by_name(client):
    for n in ("zeta", "alpha", "mid"):
        _strategy(n)
    assert [r["name"] for r in _get(client)] == ["alpha", "mid", "zeta"]


def test_o8_one_statement(db, monkeypatch):
    real = _dataset("eodhd")
    for n in ("a", "b", "c"):
        _run(_strategy(n), real, BASE_TS, sparkline="[1]")
    calls = []
    orig = researchdb.get_conn

    class Counting:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            calls.append(args[0])
            return self._conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextlib.contextmanager
    def wrapped():
        with orig() as conn:
            yield Counting(conn)

    monkeypatch.setattr(researchdb, "get_conn", wrapped)
    out = researchdb.list_strategies_overview()
    assert len(out) == 3
    assert len(calls) == 1


def test_o9_strategy_by_id_route_still_works(client):
    a = _strategy("A")
    resp = client.get(f"/api/research/strategies/{a}")
    assert resp.status_code == 200, resp.text
