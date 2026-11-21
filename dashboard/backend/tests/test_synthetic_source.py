"""Lane GV2-4a: source "synthetic" is an ordinary price source, labelled by the dataset's source column
and hidden from the research lists unless asked for (DECISIONS #440; CLAUDE.md, synthetic data rule).

Each test runs on a scratch research.db (a tmp folder), never the real one.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import researchdb
from dashboard.backend.routes import research

WINDOW = {"start": "2004-01-01T00:00:00Z", "end": "2010-01-01T00:00:00Z"}
WINDOW_START_TS = 1072915200  # 2004-01-01 UTC
WINDOW_END_TS = 1262304000    # 2010-01-01 UTC


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


def _post_dataset(client, source, symbol, name=None):
    return client.post("/api/research/datasets", json={
        "name": name or symbol, "source": source, "symbol": symbol, "timeframe": "1d", **WINDOW})


def _insert_dataset(source, symbol, dataset_type="price_dataset"):
    now = int(time.time())
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, "
            "dataset_type, created_at) VALUES (?, ?, ?, '1d', ?, ?, 'ready', ?, ?);",
            (symbol, source, symbol, WINDOW_START_TS, WINDOW_END_TS, dataset_type, now))
        return int(cur.lastrowid)


def _insert_strategy(name="s1"):
    now = int(time.time())
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO research_strategies (name, source_code, created_at, updated_at) VALUES (?, 'x', ?, ?);",
            (name, now, now))
        return int(cur.lastrowid)


def _insert_run(strategy_id, dataset_id, name, sharpe):
    now = int(time.time())
    with researchdb.transaction() as conn:
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at) VALUES ('run_backtest', '{}', ?);",
            (now,)).lastrowid
        run = conn.execute(
            "INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "settings_snapshot, name, created_at, status) VALUES (?, ?, 1, ?, 'h', '[]', ?, ?, 'succeeded');",
            (job, strategy_id, dataset_id, name, now)).lastrowid
        conn.execute(
            "INSERT INTO research_run_stats (run_id, total_return, sharpe_ratio, max_drawdown, num_trades, "
            "total_fees_paid, total_slippage_cost) VALUES (?, 0.1, ?, 0.1, 3, 0, 0);", (run, sharpe))
        return int(run)


def test_t1_synthetic_dataset_creates_row_and_job(client):
    resp = _post_dataset(client, "synthetic", "SYN_VOL_01")
    assert resp.status_code == 201, resp.text
    with researchdb.get_conn() as conn:
        row = conn.execute("SELECT source, symbol FROM research_datasets WHERE id = ?;",
                           (resp.json()["id"],)).fetchone()
        jobs = conn.execute("SELECT payload_json FROM research_jobs WHERE kind = 'fetch_dataset';").fetchall()
    assert (row["source"], row["symbol"]) == ("synthetic", "SYN_VOL_01")
    assert len(jobs) == 1
    assert json.loads(jobs[0]["payload_json"])["source"] == "synthetic"


def test_t2_symbol_and_source_must_agree(client):
    a = _post_dataset(client, "synthetic", "AAPL")
    b = _post_dataset(client, "eodhd", "SYN_VOL_01")
    for resp in (a, b):
        assert resp.status_code == 422, resp.text
        assert "synthetic-symbol-mismatch" in json.dumps(resp.json())


def test_t3_asset_class_and_window_helpers():
    from dashboard.backend import dataset_sources
    assert researchdb.asset_class_of("synthetic", "price_dataset") == "equities"
    now = 1_800_000_000
    assert (dataset_sources.earliest_floor_epoch("synthetic", "1d", now)
            == dataset_sources.earliest_floor_epoch("eodhd", "1d", now))
    assert dataset_sources.latest_end_epoch("synthetic", now) == dataset_sources.latest_end_epoch("eodhd", now)


def test_t4_datasets_list_hides_synthetic(client):
    real = _insert_dataset("eodhd", "AAPL")
    syn = _insert_dataset("synthetic", "SYN_VOL_01")
    default = {d["id"] for d in client.get("/api/research/datasets").json()}
    both = {d["id"] for d in client.get("/api/research/datasets?include_synthetic=true").json()}
    assert default == {real}
    assert both == {real, syn}


def test_t5_runs_views_hide_synthetic(client):
    real = _insert_dataset("eodhd", "AAPL")
    syn = _insert_dataset("synthetic", "SYN_VOL_01")
    uni = _insert_dataset("synthetic", "u", dataset_type="price_universe")  # a synthetic universe row, as a sweep run's dataset
    sid = _insert_strategy()
    r_real = _insert_run(sid, real, "real", 1.0)
    _insert_run(sid, syn, "syn", 2.0)
    _insert_run(sid, uni, "sweep", 3.0)
    for sort in ("created_at", "name", "sharpe"):
        default = [r["id"] for r in client.get(f"/api/research/runs?sort={sort}").json()]
        both = [r["id"] for r in client.get(f"/api/research/runs?sort={sort}&include_synthetic=true").json()]
        assert default == [r_real], sort
        assert len(both) == 3, sort
    summary = client.get("/api/research/runs/summary").json()
    assert summary["total"] == 1 and summary["by_status"] == {"succeeded": 1}
    summary_all = client.get("/api/research/runs/summary?include_synthetic=true").json()
    assert summary_all["total"] == 3 and summary_all["by_status"] == {"succeeded": 3}
    assert summary["best_sharpe"]["run_id"] == r_real
    own = client.get(f"/api/research/strategies/{sid}/runs").json()
    assert [r["id"] for r in own] == [r_real]
    own_all = client.get(f"/api/research/strategies/{sid}/runs?include_synthetic=true").json()
    assert len(own_all) == 3


def test_t5b_guard_real_run_listed_with_and_without_flag(client):
    real = _insert_dataset("eodhd", "AAPL")
    sid = _insert_strategy()
    run = _insert_run(sid, real, "real", 1.0)
    # research_runs.dataset_id is NOT NULL in the schema, so a NULL-dataset run cannot exist; the filter is
    # written as NOT EXISTS so a run with no matching dataset row would stay listed. A real-source run is the guard.
    assert [r["id"] for r in client.get("/api/research/runs").json()] == [run]
    assert [r["id"] for r in client.get("/api/research/runs?include_synthetic=true").json()] == [run]


def test_t6_price_universe_synthetic(db):
    result = researchdb.create_price_universe("syn-u", ["SYN_VOL_01", "SYN_VOL_02"], WINDOW_START_TS, WINDOW_END_TS)
    assert sorted(result["resolved"]) == ["SYN_VOL_01", "SYN_VOL_02"]
    with researchdb.get_conn() as conn:
        src = conn.execute("SELECT source FROM research_datasets WHERE id = ?;", (result["universe_id"],)).fetchone()
        n_index = conn.execute("SELECT COUNT(*) AS n FROM research_eodhd_tickers WHERE ticker IN ('SYN_VOL_01', 'SYN_VOL_02');").fetchone()
    assert src["source"] == "synthetic"
    assert n_index["n"] == 0
    with pytest.raises(researchdb.PriceUniverseMixedSource):
        researchdb.create_price_universe("mixed", ["SYN_VOL_01", "AAPL"], WINDOW_START_TS, WINDOW_END_TS)


def test_t6b_route_mixed_universe_is_422(client):
    resp = client.post("/api/research/price-universes", json={
        "name": "mixed", "tickers": ["SYN_VOL_01", "AAPL"],
        "window_start_ts": WINDOW_START_TS, "window_end_ts": WINDOW_END_TS})
    assert resp.status_code == 422, resp.text
    text = json.dumps(resp.json())
    assert "SYN_VOL_01" in text and "AAPL" in text


def test_t7_member_dataset_for_synthetic_ticker(db):
    now = int(time.time())
    with researchdb.transaction() as conn:
        dataset_id, ready = researchdb._resolve_or_create_member_dataset(
            conn, "SYN_VOL_01", now, window_start_date="2010-01-01", window_end_date="2012-01-01")
    assert ready is False
    with researchdb.get_conn() as conn:
        row = conn.execute("SELECT source, status FROM research_datasets WHERE id = ?;", (dataset_id,)).fetchone()
        job = conn.execute("SELECT payload_json FROM research_jobs WHERE kind = 'fetch_dataset';").fetchone()
    assert (row["source"], row["status"]) == ("synthetic", "pending")
    assert json.loads(job["payload_json"])["source"] == "synthetic"
