"""Lane UI17b-0: one read gives every parameter of one strategy with its value and where it comes from.

Each test runs on a scratch research.db (a tmp folder), never the real one.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import researchdb, theory_strategy
from dashboard.backend.routes import research

PROV_REL = "docs/research_lab/parameter_provenance.md"
RANGES_REL = "research_lab/theories/ranges.toml"
BASE_TS = 1_700_000_000

RANGES_TEXT = (
    '# header\n'                                  # line 1
    '[rebound_bb_window]\n'                       # 2
    'from = 10\nto = 40\nstep = 5\n'              # 3-5
    'why = "bb"\n'                                # 6
    '\n'                                          # 7
    '[dolz_list]\n'                               # 8
    'values = ["1min", "5min", "1h"]\n'           # 9
    'why = "list"\n'                              # 10
    '\n'                                          # 11
    '[rebound_max_hold]\n'                        # 12
    'from = 5\nto = 20\nstep = 5\n'
    'why = "hold why"\n'
    '\n'
    '[dolz_n]\n'
    'from = 1\nto = 3\nstep = 1\n'
    'why = "dotted"\n'
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    researchdb.init_db()
    prov = tmp_path / "prov.md"
    prov.write_text("# Provenance\n\n\n\n\n\n## fast_window\n\ntext\n\n## other\n", encoding="utf-8")
    ranges = tmp_path / "ranges.toml"
    ranges.write_text(RANGES_TEXT, encoding="utf-8")
    monkeypatch.setattr(researchdb, "PROVENANCE_PATH", prov)
    monkeypatch.setattr(researchdb, "RANGES_PATH", ranges)
    return researchdb


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(research.browser_router)
    return TestClient(app)


def _dataset(source="eodhd"):
    now = int(time.time())
    with researchdb.transaction() as conn:
        return int(conn.execute(
            "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, "
            "dataset_type, created_at) VALUES ('d', ?, 'X', '1d', 1, 2, 'ready', 'price_dataset', ?);",
            (source, now)).lastrowid)


def _strategy(name, source_code="x", status="active"):
    now = int(time.time())
    with researchdb.transaction() as conn:
        return int(conn.execute(
            "INSERT INTO research_strategies (name, source_code, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?);", (name, source_code, status, now, now)).lastrowid)


def _run(strategy_id, dataset_id, created_at, params_list, status="succeeded"):
    with researchdb.transaction() as conn:
        job = conn.execute(
            "INSERT INTO research_jobs (kind, payload_json, created_at) VALUES ('run_backtest', '{}', ?);",
            (created_at,)).lastrowid
        run = conn.execute(
            "INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "settings_snapshot, name, created_at, status, kind) "
            "VALUES (?, ?, 1, ?, 'h', '[]', 'r', ?, ?, 'standard');",
            (job, strategy_id, dataset_id, created_at, status)).lastrowid
        for i, p in enumerate(params_list):
            conn.execute("INSERT INTO research_run_variations (run_id, variation_id, param_json) VALUES (?, ?, ?);",
                         (run, i, json.dumps(p)))
        return int(run)


def _get(client, sid, **params):
    return client.get(f"/api/research/strategies/{sid}/parameters", params=params)


def _theory(text, name="t"):
    src = theory_strategy.emit_theory_strategy(name, text)
    return _strategy(theory_strategy.strategy_name(name), source_code=src)


def _std_strategy():
    sid = _strategy("ma_cross")
    real, synth = _dataset(), _dataset("synthetic")
    _run(sid, real, BASE_TS, [{"fast_window": 10, "slow_window": 50}])
    second = _run(sid, real, BASE_TS + 10, [{"fast_window": 12, "slow_window": 40}])
    _run(sid, real, BASE_TS + 20, [{"fast_window": 1, "slow_window": 2}, {"fast_window": 3, "slow_window": 4}])
    _run(sid, real, BASE_TS + 30, [{"fast_window": 5, "slow_window": 6}], status="failed")
    syn = _run(sid, synth, BASE_TS + 40, [{"fast_window": 7, "slow_window": 8}])
    return sid, second, syn


def test_p1_newest_real_single_run(client, tmp_path, monkeypatch):
    empty = tmp_path / "empty_prov.md"
    empty.write_text("# nothing here\n", encoding="utf-8")
    monkeypatch.setattr(researchdb, "PROVENANCE_PATH", empty)
    sid, second, _ = _std_strategy()
    body = _get(client, sid).json()
    assert body["strategy_id"] == sid and body["kind"] == "standard" and body["run_id"] == second
    assert body["parameters"] == [
        {"name": "fast_window", "range": None, "value": 12, "source": None},
        {"name": "slow_window", "range": None, "value": 40, "source": None},
    ]


def test_p2_provenance_heading_line(client):
    sid, _, _ = _std_strategy()
    params = _get(client, sid).json()["parameters"]
    assert params[0]["source"] == {"file": PROV_REL, "key": "fast_window", "line": 7}
    assert params[1]["source"] is None


THEORY_P3 = """
[theory]
name = "t"

[parameter_picks.N]
range = "rebound_bb_window"
from = 15
to = 30

[parameter_picks.H]
range = "rebound_max_hold"
"""


def test_p3_theory_narrowed_and_whole(client):
    sid = _theory(THEORY_P3)
    body = _get(client, sid).json()
    assert body["kind"] == "theory" and body["run_id"] is None
    n, h = body["parameters"]
    assert n["name"] == "N" and n["range"] == "rebound_bb_window"
    assert n["value"] == {"from": 15, "to": 30, "step": 5}
    assert h["value"] == {"from": 5, "to": 20, "step": 5}
    assert h["source"] == {"file": RANGES_REL, "key": "rebound_max_hold", "line": 12, "why": "hold why"}


def test_p4_listed_range(client):
    sid = _theory('[parameter_picks.I]\nrange = "dolz_list"\n')
    p = _get(client, sid).json()["parameters"][0]
    assert p["value"] == {"values": ["1min", "5min", "1h"]}
    assert p["source"]["line"] == 8


def test_p5_no_qualifying_run(client):
    sid = _strategy("lonely")
    body = _get(client, sid).json()
    assert body["run_id"] is None and body["parameters"] == []


def test_p6_unknown_and_deleted(client):
    r = _get(client, 9999)
    assert r.status_code == 404 and "strategy not found" in r.text
    sid = _strategy("gone", status="deleted")
    r = _get(client, sid)
    assert r.status_code == 404 and "strategy not found" in r.text


def test_p7_include_synthetic(client):
    sid, _, syn = _std_strategy()
    assert _get(client, sid, include_synthetic="true").json()["run_id"] == syn


def test_p8_theory_without_picks(client):
    sid = _theory('[theory]\nname = "t"\n')
    assert _get(client, sid).json()["parameters"] == []


def test_p9_dotted_absent_and_missing_file(client, tmp_path, monkeypatch):
    sid = _theory('[parameter_picks."open.bbw.n"]\nrange = "dolz_n"\n\n'
                  '[parameter_picks.Z]\nrange = "not_in_file"\nfrom = 2\nto = 4\n')
    a, z = _get(client, sid).json()["parameters"]
    assert a["name"] == "open.bbw.n" and a["range"] == "dolz_n"
    assert a["value"] == {"from": 1, "to": 3, "step": 1}
    assert z["value"] == {"from": 2, "to": 4} and z["source"] is None
    monkeypatch.setattr(researchdb, "RANGES_PATH", tmp_path / "nope.toml")
    with pytest.raises(Exception, match="nope.toml"):
        researchdb.strategy_parameters(sid)


def test_p10_source_never_executed(client):
    src = theory_strategy.emit_theory_strategy("t", THEORY_P3)
    sid = _strategy(theory_strategy.strategy_name("t2"), source_code="raise SystemExit(3)\n" + src)
    assert [p["name"] for p in _get(client, sid).json()["parameters"]] == ["N", "H"]
