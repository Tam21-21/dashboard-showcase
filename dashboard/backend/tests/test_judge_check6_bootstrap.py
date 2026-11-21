"""Lane JG1: judge check 6 reads its lower bound from the block bootstrap on every run (DECISIONS #450).

The judge db is a real research.db file made by judgedb.init_judge_db (it seeds the frozen
threshold rows), never a hand-rolled fake. b2 and b4 are guards: they pass at base too.
"""
from __future__ import annotations

import sqlite3

import pytest

pd = pytest.importorskip("pandas")

from dashboard.backend import judge_checks, judgedb  # noqa: E402

SINGLE_BAR = pd.DataFrame([{"entry_index": 2, "exit_index": 2, "pnl": 1.0, "return_pct": 0.01, "direction": "long"}])

# 60 bars, none beyond 3 standard deviations from the mean: today's "t" case.
BARS = [0.004 if i % 2 == 0 else -0.001 for i in range(60)]


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _grade(conn, bars=BARS, run_id=7):
    return judge_checks.check_6_monte_carlo_bounds(run_id, conn, judgedb.BATTERY_VERSION, list(bars), SINGLE_BAR)


def test_b1_single_bar_hold_no_extreme_uses_bootstrap(conn):
    result = _grade(conn)
    q = result.quantities
    assert q["method"] == "bootstrap"
    assert "has_multi_bar_hold" not in q and "has_wild_extreme" not in q
    rng, seed = judge_checks._seeded_random(7, 6)
    expected_bound, _ = judge_checks._bootstrap_bound(list(BARS), q["percentile"], rng)
    assert q["seed"] == seed
    assert q["bound"] == expected_bound


def test_b2_same_run_twice_same_bound_and_seed(conn):
    a, b = _grade(conn), _grade(conn)
    assert a.quantities["bound"] == b.quantities["bound"]
    assert a.quantities.get("seed") == b.quantities.get("seed")


def test_b3_routing_helpers_are_gone():
    for name in ("_check6_route", "_t_bound", "_CHECK6_OUTLIER_SIGMA", "_has_wild_extreme", "_t_ppf"):
        assert not hasattr(judge_checks, name), name


def test_b4_empty_bar_returns_stays_not_evaluable(conn):
    result = judge_checks.check_6_monte_carlo_bounds(7, conn, judgedb.BATTERY_VERSION, [], SINGLE_BAR)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert result.reason == (
        "the candidate's bar-level return series is the missing input: equity.parquet is "
        "absent or empty"
    )
