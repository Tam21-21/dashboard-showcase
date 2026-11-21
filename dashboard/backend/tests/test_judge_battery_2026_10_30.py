"""Lane DBG-7b: battery 2026-10-30.a records check 9's buy-and-hold construction as the ruling #451
(buy on the first bar, sell on the last), kept by #479 item 8. Every other row equals 2026-10-10.a's."""
from __future__ import annotations

import sqlite3

import pytest

pd = pytest.importorskip("pandas")

from dashboard.backend import judge_checks, judgedb  # noqa: E402

DATES = list(range(1_700_000_000, 1_700_000_000 + 10 * 86_400, 86_400))
GOOD = [0.01, 0.02, -0.005, 0.015, 0.01, 0.02, 0.0, 0.01, 0.02, 0.01]
FLAT = [0.0, 0.001, -0.001, 0.0, 0.001, -0.001, 0.0, 0.001, 0.0, -0.001]
POOR = [-0.01, 0.0, -0.02, 0.005, -0.01, 0.0, -0.01, 0.0, -0.01, 0.005]
SHORT_TRADES = pd.DataFrame([{"entry_index": 2, "exit_index": 2, "pnl": 1.0, "return_pct": 0.01, "direction": "long"}])
NEW, OLD = "2026-10-30.a", "2026-10-10.a"
KEY = (9, "buy_hold_arm_construction")
SQL = "SELECT * FROM research_judge_thresholds WHERE battery_version = ? ORDER BY check_id, quantity_key;"


def _open(path):
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    return c


def _seed_old(path):
    with judgedb._judge_transaction(path) as conn:
        judgedb.seed_thresholds(conn, battery_version=OLD)


def test_b1_current_version_is_registered():
    assert judgedb.BATTERY_VERSION == NEW
    assert NEW in judgedb._SEED_ROWS_BY_BATTERY_VERSION


def test_b2_rows_equal_previous_except_construction():
    new_rows = judgedb._SEED_ROWS_BY_BATTERY_VERSION[NEW]
    old_rows = judgedb._SEED_ROWS_BY_BATTERY_VERSION[OLD]
    assert len(new_rows) == len(old_rows) == 16
    for new, old in zip(new_rows, old_rows):
        if (new[0], new[1]) == KEY:
            assert new == judgedb._BUY_HOLD_CONSTRUCTION_451
            assert new != old
        else:
            assert new == old


def test_b3_new_version_leaves_earlier_rows_byte_identical(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    _seed_old(path)
    c = _open(path)
    before = [tuple(r) for r in c.execute(SQL, (OLD,)).fetchall()]
    c.close()
    judgedb.init_judge_db(path)
    c = _open(path)
    after = [tuple(r) for r in c.execute(SQL, (OLD,)).fetchall()]
    new_rows = c.execute(SQL, (NEW,)).fetchall()
    assert len(before) == 16 and after == before
    assert len(new_rows) == 16 and all(r["status"] == "frozen" for r in new_rows)
    row = judgedb.read_threshold(c, 9, "buy_hold_arm_construction")
    assert row["source_kind"] == "ruling" and row["frozen_by_entry"] == "#451"
    c.close()


def test_b4_check_9_grades_the_same_under_both_versions(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    _seed_old(path)
    c = _open(path)
    c.execute("CREATE TABLE IF NOT EXISTS research_runs (id INTEGER PRIMARY KEY, annualization_factor REAL);")
    c.execute("INSERT INTO research_runs (id, annualization_factor) VALUES (1, 252.0);")
    c.commit()
    for candidate, hold in ((GOOD, FLAT), (POOR, GOOD), (GOOD, GOOD)):
        results = [
            judge_checks.check_9_controls(
                1, c, version, candidate, SHORT_TRADES, buy_hold_bar_returns=hold,
                buy_hold_error=None, bar_returns_dates=DATES, buy_hold_bar_returns_dates=DATES)
            for version in (OLD, NEW)
        ]
        assert (results[0].outcome, results[0].reason, results[0].quantities) == \
               (results[1].outcome, results[1].reason, results[1].quantities)
    c.close()
