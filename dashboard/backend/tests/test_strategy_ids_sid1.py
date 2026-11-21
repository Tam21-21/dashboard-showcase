"""Lane SID1: strategy ids are never reused (docs/lanes/SID1.md, DECISIONS #488 ruling 10).

Each test runs on a scratch research.db in a tmp folder, never the real one. Checks s1 to s7 follow the
lane file's Tests line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dashboard.backend import cli, researchdb

REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "research_lab" / "strategies" / "ma_cross.py").read_text(encoding="utf-8")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    researchdb.init_db()
    return researchdb


def _rows(sql, args=()):
    with researchdb.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _counter():
    return [r["high_id"] for r in _rows("SELECT high_id FROM research_strategy_id_counter;")]


def _raw_strategy(sid, name):
    with researchdb.transaction() as conn:
        conn.execute("INSERT INTO research_strategies (id, name, source_code, created_at, updated_at) "
                     "VALUES (?, ?, 'x', 1, 1);", (sid, name))


def _candidate(n, decision, strategy_id):
    reason = None if decision == "accepted" else "refused for a test"
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_generated_candidates (theory_id, theory_sha256, skeleton_id, filler_id, expression, "
            "expression_sha256, decision, refusal_reason, strategy_id, generated_at) "
            "VALUES ('t', 'ts', ?, 'f', 'e', 'es', ?, ?, ?, 1);", (f"sk{n}", decision, reason, strategy_id))


def test_s1_removed_strategy_id_is_not_given_again(db):
    db.upsert_strategy("a", SOURCE, display_name="A")
    b, _, _ = db.upsert_strategy("b", SOURCE, display_name="B")
    assert db.soft_delete_strategy(b)["action"] == "removed"
    c, _, _ = db.upsert_strategy("c", SOURCE, display_name="C")
    assert c == 3


def test_s2_pre_lane_database_is_seeded_from_every_strategy_id(db):
    with researchdb.transaction() as conn:
        conn.execute("DROP TRIGGER trg_strategy_id_counter;")
        conn.execute("DROP TABLE research_strategy_id_counter;")
    _raw_strategy(5, "five")
    with researchdb.transaction() as conn:
        conn.execute("INSERT INTO research_deletions (strategy_id, strategy_name, orphaned_runs, deleted_at) "
                     "VALUES (9, 'nine', 0, 1);")
    _candidate(1, "accepted", 12)
    db.init_db()
    assert _counter() == [12]
    sid, _, _ = db.upsert_strategy("n", SOURCE, display_name="N")
    assert sid == 13


def test_s3_init_twice_keeps_one_row_and_never_lowers(db):
    db.init_db()
    assert _counter() == [0]
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategy_id_counter SET high_id = 50;")
    db.init_db()
    assert _counter() == [50]


def test_s4_raw_insert_raises_the_counter(db):
    _raw_strategy(70, "seventy")
    assert _counter() == [70]
    sid, _, _ = db.upsert_strategy("n", SOURCE, display_name="N")
    assert sid == 71


def test_s5_missing_counter_row_raises_and_writes_nothing(db):
    with researchdb.transaction() as conn:
        conn.execute("DELETE FROM research_strategy_id_counter;")
    with pytest.raises(researchdb.StrategyIdCounterMissing):
        db.upsert_strategy("n", SOURCE, display_name="N")
    assert _rows("SELECT id FROM research_strategies;") == []


def _four_candidates(db):
    live, _, _ = db.upsert_strategy("live", SOURCE, display_name="Live")
    tomb, _, _ = db.upsert_strategy("tomb", SOURCE, display_name="Tomb")
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_strategies SET status = 'deleted' WHERE id = ?;", (tomb,))
    _candidate(1, "accepted", live)
    _candidate(2, "accepted", tomb)
    _candidate(3, "accepted", 99)
    _candidate(4, "refused", None)


def test_s6_dry_run_lists_the_orphan_and_deletes_nothing(db, capsys):
    _four_candidates(db)
    assert cli.main(["clean-orphan-candidates"]) == 0
    out = capsys.readouterr().out
    assert "99" in out and "dry run" in out
    assert len(_rows("SELECT id FROM research_generated_candidates;")) == 4


def test_s7_apply_removes_only_the_orphan(db, capsys):
    _four_candidates(db)
    with researchdb.transaction() as conn:
        conn.execute("INSERT INTO research_deletions (strategy_id, strategy_name, orphaned_runs, deleted_at) "
                     "VALUES (9, 'nine', 0, 1);")
    assert cli.main(["clean-orphan-candidates", "--apply"]) == 0
    assert "removed 1 rows" in capsys.readouterr().out
    left = _rows("SELECT strategy_id FROM research_generated_candidates ORDER BY id;")
    assert 99 not in [r["strategy_id"] for r in left] and len(left) == 3
    assert len(_rows("SELECT id FROM research_deletions;")) == 1
    assert cli.main(["clean-orphan-candidates", "--apply"]) == 0
    assert "removed 0 rows" in capsys.readouterr().out
