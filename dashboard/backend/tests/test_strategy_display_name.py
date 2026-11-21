"""Lane DN1: every strategy has a display name (docs/lanes/DN1.md).

Each test runs on a scratch research.db in a tmp folder, never the real one. Checks d1 to d9 follow the
lane file's Tests line: the backfill, the storage rules, the HTTP doors, the reads, the names read, the
theory screen door, the command line door and the generator v1 door.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import cli, researchdb, stage_runner
from dashboard.backend.routes import research
from dashboard.backend.tests.test_theory_jobs import COSTS, GROUP, INIT_CASH, SEARCH, world  # noqa: F401 (world is a fixture)

REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "research_lab" / "strategies" / "ma_cross.py").read_text(encoding="utf-8")
SOURCE_2 = SOURCE + "\n# a second version\n"


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


def _rows(sql, args=()):
    with researchdb.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _columns(table):
    return {r["name"] for r in _rows(f"PRAGMA table_info({table})")}


def test_d1_backfill_gives_todays_rows_plain_words_once(db):
    now = int(time.time())
    names = ["strategy_runs_ma_cross", "gen__weak_close_bounce", "theory__capitulation_rebound__v0"]
    with researchdb.transaction() as conn:
        for n in names:
            conn.execute("INSERT INTO research_strategies (name, source_code, created_at, updated_at) VALUES (?, 'x', ?, ?);",
                         (n, now, now))
        conn.execute("UPDATE research_strategies SET display_name = NULL;")
    researchdb.init_db()
    got = {r["name"]: r["display_name"] for r in _rows("SELECT name, display_name FROM research_strategies;")}
    assert got == {"strategy_runs_ma_cross": "Strategy runs ma cross", "gen__weak_close_bounce": "Weak close bounce",
                   "theory__capitulation_rebound__v0": "Capitulation rebound v0"}
    assert "display_name" in _columns("research_strategies") and "display_name" in _columns("research_strategies_history")
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET display_name = 'Mine' WHERE name = 'gen__weak_close_bounce';")
    researchdb.init_db()  # a second run finds no NULL row and changes nothing
    again = {r["name"]: r["display_name"] for r in _rows("SELECT name, display_name FROM research_strategies;")}
    assert again["gen__weak_close_bounce"] == "Mine" and again["strategy_runs_ma_cross"] == "Strategy runs ma cross"


def test_d2_create_without_a_name_stores_null_and_bad_lengths_are_refused(db):
    sid, version, action = researchdb.upsert_strategy("ma_a", SOURCE)
    assert (version, action) == (1, "created")
    names = researchdb.list_strategy_names()
    assert names == [{"id": sid, "name": "ma_a", "display_name": None, "status": "active",
                      "needs_name": True}]
    for bad in ("  ", "x" * 61):
        with pytest.raises(researchdb.StrategyValidationError):
            researchdb.upsert_strategy("ma_b", SOURCE, display_name=bad)
    assert _rows("SELECT id FROM research_strategies WHERE name = 'ma_b';") == []
    sid2, _, _ = researchdb.upsert_strategy("ma_c", SOURCE, display_name="  Moving cross  ")
    assert researchdb.get_strategy(sid2)["display_name"] == "Moving cross"


def test_d3_update_keeps_the_name_and_a_rename_leaves_the_version(db):
    sid, _, _ = researchdb.upsert_strategy("ma_a", SOURCE, display_name="First")
    _, v2, action = researchdb.upsert_strategy("ma_a", SOURCE_2)
    assert (v2, action) == (2, "updated")
    assert researchdb.get_strategy(sid)["display_name"] == "First"
    hist = _rows("SELECT version, display_name FROM research_strategies_history WHERE strategy_id = ? ORDER BY id;", (sid,))
    assert [h["display_name"] for h in hist] == ["First", "First"]
    _, v3, action = researchdb.upsert_strategy("ma_a", SOURCE_2, display_name="Second")
    assert (v3, action) == (2, "renamed")
    assert researchdb.get_strategy(sid)["display_name"] == "Second"
    hist = _rows("SELECT version, display_name FROM research_strategies_history WHERE strategy_id = ? ORDER BY id;", (sid,))
    assert len(hist) == 3 and hist[-1]["version"] == 2 and hist[-1]["display_name"] == "Second"
    versions = researchdb.list_strategy_versions(sid)
    assert [v["version"] for v in versions] == [2, 1]


def test_d4_post_and_put_doors(client):
    body = {"name": "ma_a", "source_code": SOURCE}
    assert client.post("/api/research/strategies", json=body).status_code == 422
    assert client.post("/api/research/strategies", json={**body, "display_name": "   "}).status_code == 400
    assert client.post("/api/research/strategies", json={**body, "display_name": "x" * 61}).status_code == 400
    assert _rows("SELECT id FROM research_strategies;") == []
    ok = client.post("/api/research/strategies", json={**body, "display_name": "Moving cross"})
    assert ok.status_code == 201 and ok.json()["display_name"] == "Moving cross"
    sid = ok.json()["id"]
    renamed = client.put(f"/api/research/strategies/{sid}", json={"source_code": SOURCE, "display_name": "Cross two"})
    assert renamed.status_code == 200 and renamed.json()["action"] == "renamed" and renamed.json()["version"] == 1
    assert researchdb.get_strategy(sid)["display_name"] == "Cross two"
    kept = client.put(f"/api/research/strategies/{sid}", json={"source_code": SOURCE_2})
    assert kept.status_code == 200 and researchdb.get_strategy(sid)["display_name"] == "Cross two"
    assert client.put(f"/api/research/strategies/{sid}", json={"display_name": "Only"}).status_code == 422


def test_d5_reads_carry_the_name_and_a_deleted_row_keeps_it(client, db):
    sid, _, _ = researchdb.upsert_strategy("ma_a", SOURCE, display_name="Moving cross")
    assert client.get("/api/research/strategies").json()[0]["display_name"] == "Moving cross"
    assert client.get(f"/api/research/strategies/{sid}").json()["display_name"] == "Moving cross"
    assert client.get("/api/research/strategies/overview").json()[0]["display_name"] == "Moving cross"
    # a run keeps the strategy from being erased, so the delete is a tombstone
    now = int(time.time())
    with researchdb.transaction() as conn:
        ds = conn.execute("INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, dataset_type, "
                          "created_at) VALUES ('d', 'x', 'X', '1d', 1, 2, 'ready', 'price_dataset', ?);", (now,)).lastrowid
        job = conn.execute("INSERT INTO research_jobs (kind, payload_json, created_at) VALUES ('run_backtest', '{}', ?);", (now,)).lastrowid
        conn.execute("INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
                     "settings_snapshot, name, created_at, status) VALUES (?, ?, 1, ?, 'h', '[]', 'r', ?, 'succeeded');",
                     (job, sid, ds, now))
    assert researchdb.soft_delete_strategy(sid)["action"] == "tombstoned"
    assert researchdb.get_strategy(sid)["display_name"] == "Moving cross"


def test_d6_names_read_lists_deleted_rows_too(client, db):
    a, _, _ = researchdb.upsert_strategy("ma_a", SOURCE, display_name="Moving cross")
    b, _, _ = researchdb.upsert_strategy("ma_b", SOURCE_2, display_name="Other")
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET status = 'deleted' WHERE id = ?;", (b,))
    resp = client.get("/api/research/strategies/names")
    assert resp.status_code == 200
    assert resp.json() == [{"id": a, "name": "ma_a", "display_name": "Moving cross", "status": "active",
                            "needs_name": False},
                           {"id": b, "name": "ma_b", "display_name": "Other", "status": "deleted",
                            "needs_name": False}]


def _screen(world, theory_text):  # noqa: F811
    return stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], 1_800_000_000, calendar_id="dn1", theory_text=theory_text,
        group_label=GROUP, data_start="2003-01-02", search=SEARCH, costs=COSTS, init_cash=INIT_CASH)


def test_d7_theory_screen_needs_a_display_name(world):  # noqa: F811
    with pytest.raises(researchdb.TheoryRunInvalid, match="display_name"):
        _screen(world, '[theory]\nname = "gap_fade_test"\nbar = "1d"\n')
    assert _rows("SELECT id FROM research_data_draws;") == []
    assert _rows("SELECT id FROM research_strategies;") == []
    done = _screen(world, '[theory]\nname = "gap_fade_test"\nbar = "1d"\ndisplay_name = "Gap fade test"\n')
    assert researchdb.get_strategy(done["strategy_id"])["display_name"] == "Gap fade test"


def _register(tmp_path, display_name=None):
    f = tmp_path / "ma_cross.py"
    f.write_text(SOURCE, encoding="utf-8")
    ns = argparse.Namespace(file=str(f), name=None, display_name=display_name)
    return cli.cmd_register_strategy(ns)


def test_d8_cli_register_needs_a_display_name_for_a_new_name(db, tmp_path, capsys):
    assert _register(tmp_path) == 2
    assert "a new strategy needs --display-name" in capsys.readouterr().err
    assert _rows("SELECT id FROM research_strategies;") == []
    assert _register(tmp_path, "Moving cross") == 0
    (tmp_path / "ma_cross.py").write_text(SOURCE_2, encoding="utf-8")
    ns = argparse.Namespace(file=str(tmp_path / "ma_cross.py"), name=None, display_name=None)
    assert cli.cmd_register_strategy(ns) == 0
    row = _rows("SELECT display_name, version FROM research_strategies;")[0]
    assert row == {"display_name": "Moving cross", "version": 2}


def test_d9_generator_v1_write_needs_the_heading(db, tmp_path):
    from research_lab.generator import loop
    from research_lab.generator.facts import Facts
    from research_lab.generator.theory import TheoryLoadError, load_theory
    from research_lab.probes.generator import _lib

    base = _lib.write_theory_file(tmp_path, "plain_theory")
    lookahead = _lib.fake_lookahead_module()
    facts_dict = _lib.fixture_facts_dict()
    facts = Facts(image=facts_dict["image"], vbt_version=facts_dict["vbt_version"], fillers=facts_dict["fillers"])
    theory = load_theory(base, lookahead_module=lookahead)
    with pytest.raises(TheoryLoadError, match="needs a '## Display name' heading to write strategies"):
        loop.generate(theory, facts, write=True, lookahead_module=lookahead)
    assert _rows("SELECT id FROM research_generated_candidates;") == []
    assert _rows("SELECT id FROM research_strategies;") == []
    named = tmp_path / "named_theory.md"
    named.write_text("## Display name\nWeak close bounce\n\n" + base.read_text(encoding="utf-8"), encoding="utf-8")
    theory2 = load_theory(named, lookahead_module=lookahead)
    out = loop.generate(theory2, facts, write=True, lookahead_module=lookahead)
    assert out[0].decision == "accepted"
    assert researchdb.get_strategy(out[0].strategy_id)["display_name"] == "Weak close bounce, band cap, wqa101 101"
