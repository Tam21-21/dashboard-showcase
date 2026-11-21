"""Lane GV2-4b: paper refuses synthetic data, and the judge lists hide it by default
(CLAUDE.md: synthetic data never reaches a broker; the final test, step 1).

Every test runs on scratch databases in a tmp folder, never the real ones. The research side is
built with the real researchdb.init_db; the verdict, paper and reserve stores with their own init
functions. Rows are inserted with plain SQL, the way scripts/probes/judge_page_2/_route_lib.py does.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import judgedb, paperdb, researchdb, reservedb, retestdb
from dashboard.backend.routes import judge as judge_routes

SYNTHETIC_DETAIL = (
    "synthetic-data-refused: run {run_id} used synthetic data; synthetic data never reaches paper"
)


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


def _add_dataset(source, symbol, dataset_type="price_dataset") -> int:
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, "
            "status, dataset_type, created_at) VALUES (?, ?, ?, '1d', 1072915200, 1262304000, "
            "'ready', ?, 1700000000);",
            (symbol, source, symbol, dataset_type),
        )
        return int(cur.lastrowid)


def _add_run(run_id, dataset_id, init_cash=5000.0) -> None:
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_jobs (id, kind, payload_json, status, created_at) "
            "VALUES (?, 'run_sweep', ?, 'succeeded', 1700000000);",
            (run_id, json.dumps({"init_cash": init_cash})),
        )
        conn.execute(
            "INSERT INTO research_runs (id, job_id, strategy_id, strategy_version, dataset_id, "
            "dataset_sha256, settings_snapshot, created_at, status) "
            "VALUES (?, ?, 1, 1, ?, ?, '[]', 1700000000, 'succeeded');",
            (run_id, run_id, dataset_id, "b" * 64),
        )


def _add_verdict(run_id, ticker, sha="b") -> int:
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        return judgedb.record_verdict(
            conn,
            run_id=run_id,
            judging_pass_id=f"{run_id:08d}-1111-4111-8111-111111111111",
            variation_id=0,
            ticker=ticker,
            graded_formula_version="1|3|docker:research-runner:m3-latest@sha256:" + "a" * 64,
            strategy_id=1,
            strategy_version=1,
            dataset_sha256=sha * 64,
            verdict="pass",
            is_advisory=0,
            battery_version=judgedb.BATTERY_VERSION,
            evidence_floor_outcome="cleared",
            check_outcomes_json=json.dumps([{"check": n, "outcome": "cleared"} for n in range(1, 10)]),
            decided_by="initial",
            warnings_json="[]",
            graded_at=1700000000 + run_id,
        )


def _paper_rows(paper_db) -> dict:
    conn = sqlite3.connect(str(paper_db))
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
            for t in ("paper_runs", "paper_slice_current", "paper_equity")
        }
    finally:
        conn.close()


def test_p1_run_data_is_synthetic(stores):
    syn_ds = _add_dataset("synthetic", "SYN_AAA")
    syn_universe = _add_dataset("synthetic", "syn universe", dataset_type="price_universe")
    real_ds = _add_dataset("eodhd", "AAPL")
    _add_run(1, syn_ds)
    _add_run(2, syn_universe)
    _add_run(3, real_ds)
    # research_runs.dataset_id is NOT NULL and a foreign key in the real table, so a NULL cannot be
    # stored; a run whose dataset row is gone is the nearest real case (the join treats both the same).
    gone_ds = _add_dataset("eodhd", "MSFT")
    _add_run(4, gone_ds)
    raw = sqlite3.connect(str(researchdb.DB_PATH))  # foreign keys are off on a plain connection
    raw.execute("DELETE FROM research_datasets WHERE id = ?;", (gone_ds,))
    raw.commit()
    raw.close()
    assert researchdb.run_data_is_synthetic(1) is True
    assert researchdb.run_data_is_synthetic(2) is True
    assert researchdb.run_data_is_synthetic(3) is False
    with pytest.raises(researchdb.RunDatasetMissing, match="run 404"):
        researchdb.run_data_is_synthetic(404)
    with pytest.raises(researchdb.RunDatasetMissing, match="run 4"):
        researchdb.run_data_is_synthetic(4)


def test_p2_approve_refuses_synthetic(stores, client):
    _, paper_db = stores
    _add_run(1, _add_dataset("synthetic", "SYN_AAA"))
    vid = _add_verdict(1, "SYN_AAA")
    r = client.post(f"/api/judge/verdicts/{vid}/approve", json={"slice_opening_cash": 5000.0})
    assert r.status_code == 409
    assert r.json()["detail"] == SYNTHETIC_DETAIL.format(run_id=1)
    assert _paper_rows(paper_db) == {"paper_runs": 0, "paper_slice_current": 0, "paper_equity": 0}


def test_p3_guard_real_run_answers_as_at_base(stores, client):
    """Guard: a passing verdict of an eodhd run gets the answer it got before this lane. With
    StubVenue an approve never admits; here the first refusal reached is the reserve exam."""
    _, paper_db = stores
    _add_run(1, _add_dataset("eodhd", "AAPL"))
    vid = _add_verdict(1, "AAPL")
    r = client.post(f"/api/judge/verdicts/{vid}/approve", json={"slice_opening_cash": 5000.0})
    assert r.status_code == 409
    assert r.json()["detail"] == (
        f"reserve-exam-not-passed: this candidate has no passed reserve exam yet (verdict {vid})"
    )
    assert _paper_rows(paper_db) == {"paper_runs": 0, "paper_slice_current": 0, "paper_equity": 0}


def test_p4_lists_and_counts_hide_synthetic(stores, client):
    _add_run(1, _add_dataset("synthetic", "SYN_AAA"))
    _add_run(2, _add_dataset("eodhd", "AAPL"))
    _add_verdict(1, "SYN_AAA", sha="c")
    _add_verdict(2, "AAPL", sha="d")

    v = client.get("/api/judge/verdicts").json()
    assert [row["run_id"] for row in v["rows"]] == [2]
    assert v["store"]["row_count"] == 1
    s = client.get("/api/judge/summary").json()
    assert s["graded"] == 1 and s["survivors"] == 1 and s["store"]["row_count"] == 1

    v = client.get("/api/judge/verdicts", params={"include_synthetic": "true"}).json()
    assert sorted(row["run_id"] for row in v["rows"]) == [1, 2]
    assert v["store"]["row_count"] == 2
    s = client.get("/api/judge/summary", params={"include_synthetic": "true"}).json()
    assert s["graded"] == 2 and s["survivors"] == 2 and s["store"]["row_count"] == 2


def test_p5_approve_refuses_when_dataset_row_is_gone(stores, client):
    _, paper_db = stores
    ds = _add_dataset("eodhd", "AAPL")
    _add_run(1, ds)
    vid = _add_verdict(1, "AAPL")
    raw = sqlite3.connect(str(researchdb.DB_PATH))  # foreign keys are off on a plain connection
    raw.execute("DELETE FROM research_datasets WHERE id = ?;", (ds,))
    raw.commit()
    raw.close()
    r = client.post(f"/api/judge/verdicts/{vid}/approve", json={"slice_opening_cash": 5000.0})
    assert r.status_code == 409
    assert r.json()["detail"] == f"run-dataset-missing: run 1 names dataset {ds}, which has no row"
    assert _paper_rows(paper_db) == {"paper_runs": 0, "paper_slice_current": 0, "paper_equity": 0}
