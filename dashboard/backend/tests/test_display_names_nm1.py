"""Lane NM1a: typed names, server rules (docs/lanes/NM1a.md).

Each test runs on a scratch research.db in a tmp folder, never the real one. Checks n1 to n5 follow the lane
file's Tests line: code_like, the flag migration, the doors, rename_strategy, and the needs_name reads.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import researchdb
from dashboard.backend.routes import research

REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "research_lab" / "strategies" / "ma_cross.py").read_text(encoding="utf-8")
SOURCE_2 = SOURCE + "\n# a second version\n"

STRATEGY_CUTOFF = 1792504172   # DN1 merged, 2026-10-20 15:49:32 +0200
UNIVERSE_CUTOFF = 1793461958   # UNI1 deployed, 2026-10-31 15:52:38 UTC

PRODUCTION_NAMES = [
    "Pullback in a trend, RSI", "Pullback in a trend, Stochastic",
    "Average crossover, simple averages", "Average crossover, exponential averages",
    "Channel breakout",
    "Rebound after a crash, Bollinger lower band, RSI", "Rebound after a crash, Bollinger lower band, Stochastic",
    "Rebound after a crash, Donchian low, RSI", "Rebound after a crash, Donchian low, Stochastic",
]


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


def _insert_strategy(name, display_name, created_at):
    with researchdb.transaction() as conn:
        return conn.execute(
            "INSERT INTO research_strategies (name, source_code, created_at, updated_at, display_name) "
            "VALUES (?, 'x', ?, ?, ?);", (name, created_at, created_at, display_name)).lastrowid


def _insert_universe(name, display_name, created_at):
    with researchdb.transaction() as conn:
        return conn.execute(
            "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, dataset_type, "
            "created_at, display_name) VALUES (?, 'eodhd', ?, '1d', 0, 0, 'ready', 'price_universe', ?, ?);",
            (name, name, created_at, display_name)).lastrowid


def _drop_flag_columns():
    """Make the database look like one made before the column existed."""
    with researchdb.transaction() as conn:
        conn.execute("ALTER TABLE research_strategies DROP COLUMN display_name_from_name;")
        conn.execute("ALTER TABLE research_datasets DROP COLUMN display_name_from_name;")


def _flag(table, row_id):
    return _rows(f"SELECT display_name, display_name_from_name AS f FROM {table} WHERE id = ?;", (row_id,))[0]


def test_n1_code_like():
    assert "capitals" in researchdb.code_like("GROUP_NASDAQ_100")
    assert "underscore" in researchdb.code_like("ma_cross")
    assert researchdb.code_like(None) is None
    for ok in ["Strategy runs ma cross", "Channel breakout", "Nasdaq-100 since 2009", *PRODUCTION_NAMES]:
        assert researchdb.code_like(ok) is None, ok


def test_n2_migration_flags_only_old_rows_that_equal_the_plain_words(db):
    old_equal = _insert_strategy("ma_cross_old", "Ma cross old", STRATEGY_CUTOFF - 10)
    old_typed = _insert_strategy("ma_typed_old", "A name I typed", STRATEGY_CUTOFF - 10)
    new_equal = _insert_strategy("channel_breakout", "Channel breakout", STRATEGY_CUTOFF + 10)
    u_old_equal = _insert_universe("mag_seven", "Mag seven", UNIVERSE_CUTOFF - 10)
    u_old_typed = _insert_universe("big_tech", "Big tech names", UNIVERSE_CUTOFF - 10)
    u_new_equal = _insert_universe("fresh_one", "Fresh one", UNIVERSE_CUTOFF + 10)
    _drop_flag_columns()
    researchdb.init_db()
    assert _flag("research_strategies", old_equal)["f"] == 1
    assert _flag("research_strategies", old_typed)["f"] == 0
    assert _flag("research_strategies", new_equal)["f"] == 0
    assert _flag("research_datasets", u_old_equal)["f"] == 1
    assert _flag("research_datasets", u_old_typed)["f"] == 0
    assert _flag("research_datasets", u_new_equal)["f"] == 0
    # a second init changes nothing, even after a person retypes a flagged name's row to flag 0
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET display_name_from_name = 0 WHERE id = ?;", (old_equal,))
    researchdb.init_db()
    assert _flag("research_strategies", old_equal)["f"] == 0
    assert _flag("research_strategies", old_typed)["f"] == 0


def test_n2_null_display_name_is_backfilled_with_the_flag(db):
    sid = _insert_strategy("rsi_mean_test", None, int(time.time()))
    researchdb.init_db()
    assert _flag("research_strategies", sid) == {"display_name": "Rsi mean test", "f": 1}


def test_n2_old_database_with_a_null_row_and_old_rows(db):
    null_row = _insert_strategy("atss_strategy", None, STRATEGY_CUTOFF + 500)
    old_row = _insert_strategy("rsi_ex", "Rsi ex", STRATEGY_CUTOFF - 500)
    typed_row = _insert_strategy("some_name", "Typed by me", STRATEGY_CUTOFF - 500)
    _drop_flag_columns()
    researchdb.init_db()
    assert _flag("research_strategies", null_row) == {"display_name": "Atss strategy", "f": 1}
    assert _flag("research_strategies", old_row)["f"] == 1
    assert _flag("research_strategies", typed_row)["f"] == 0


def test_n3_doors_refuse_a_code_like_name(client, db):
    body = {"name": "ma_a", "source_code": SOURCE, "display_name": "my_strategy"}
    resp = client.post("/api/research/strategies", json=body)
    assert resp.status_code == 400 and "underscore" in resp.json()["detail"]
    assert _rows("SELECT id FROM research_strategies;") == []
    sid, _, _ = researchdb.upsert_strategy("ma_b", SOURCE, display_name="Moving cross")
    put = client.put(f"/api/research/strategies/{sid}", json={"source_code": SOURCE, "display_name": "my_strategy"})
    assert put.status_code == 400 and "underscore" in put.json()["detail"]
    assert researchdb.get_strategy(sid)["display_name"] == "Moving cross"
    created = client.post("/api/research/price-universes", json={
        "name": "uni_a", "tickers": ["AAA"], "window_start_ts": 0, "window_end_ts": 86400, "display_name": "my_universe"})
    assert created.status_code == 422 and created.json()["detail"].startswith("price_universe_display_name_invalid: ")
    patched = client.patch("/api/research/price-universes/1", json={"display_name": "my_universe"})
    assert patched.status_code == 422 and patched.json()["detail"].startswith("price_universe_display_name_invalid: ")


def test_n4_rename_strategy_on_a_deleted_strategy(db):
    sid, _, _ = researchdb.upsert_strategy("ma_a", SOURCE, display_name="Moving cross")
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET status = 'deleted' WHERE id = ?;", (sid,))
        conn.execute("UPDATE research_strategies SET display_name_from_name = 1 WHERE id = ?;", (sid,))
    hist = lambda: _rows("SELECT version, display_name FROM research_strategies_history WHERE strategy_id = ?;", (sid,))  # noqa: E731
    before = len(hist())
    researchdb.rename_strategy(sid, "Cross two", int(time.time()))
    got = researchdb.get_strategy(sid)
    assert got["display_name"] == "Cross two" and got["status"] == "deleted" and got["version"] == 1
    assert len(hist()) == before + 1 and hist()[-1] == {"version": 1, "display_name": "Cross two"}
    assert _flag("research_strategies", sid)["f"] == 0
    # the same text retyped: flag only, no history row
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET display_name_from_name = 1 WHERE id = ?;", (sid,))
    n = len(hist())
    researchdb.rename_strategy(sid, "Cross two", int(time.time()))
    assert _flag("research_strategies", sid)["f"] == 0 and len(hist()) == n
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET display_name_from_name = 1 WHERE id = ?;", (sid,))
    action = researchdb.upsert_strategy("ma_a", SOURCE, display_name="Cross two")
    assert action[2] == "unchanged"
    assert _flag("research_strategies", sid)["f"] == 0 and len(hist()) == n
    with pytest.raises(researchdb.StrategyNotFound):
        researchdb.rename_strategy(99999, "Anything", int(time.time()))
    with pytest.raises(researchdb.StrategyValidationError, match="reads like code"):
        researchdb.rename_strategy(sid, "my_strategy", int(time.time()))


def test_n4_patch_route(client, db):
    sid, _, _ = researchdb.upsert_strategy("ma_a", SOURCE, display_name="Moving cross")
    ok = client.patch(f"/api/research/strategies/{sid}/display-name", json={"display_name": "Cross two"})
    assert ok.status_code == 200 and ok.json() == {"id": sid, "display_name": "Cross two"}
    assert client.patch("/api/research/strategies/99999/display-name", json={"display_name": "X y"}).status_code == 404
    bad = client.patch(f"/api/research/strategies/{sid}/display-name", json={"display_name": "my_strategy"})
    assert bad.status_code == 400 and "underscore" in bad.json()["detail"]


def test_n5_needs_name_reads(client, db):
    now = int(time.time())
    flagged = _insert_strategy("ma_a", "Ma a", now)
    nameless = _insert_strategy("ma_b", None, now)
    typed = _insert_strategy("ma_c", "Moving cross", now)
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET display_name_from_name = 1 WHERE id = ?;", (flagged,))
    got = {r["id"]: r["needs_name"] for r in client.get("/api/research/strategies/names").json()}
    assert got == {flagged: True, nameless: True, typed: False}
    client.patch(f"/api/research/strategies/{flagged}/display-name", json={"display_name": "Typed now"})
    got = {r["id"]: r["needs_name"] for r in client.get("/api/research/strategies/names").json()}
    assert got[flagged] is False
    u_flag = _insert_universe("mag_seven", "Mag seven", now)
    u_null = _insert_universe("no_name", None, now)
    u_typed = _insert_universe("big_tech", "Big tech", now)
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_datasets SET display_name_from_name = 1 WHERE id = ?;", (u_flag,))
    rows = {r["id"]: r["needs_name"] for r in client.get("/api/research/datasets").json()
            if r["dataset_type"] == "price_universe"}
    assert rows == {u_flag: True, u_null: True, u_typed: False}
    researchdb.set_price_universe_display_name(u_flag, "Magnificent seven")
    rows = {r["id"]: r["needs_name"] for r in client.get("/api/research/datasets").json()
            if r["dataset_type"] == "price_universe"}
    assert rows[u_flag] is False
