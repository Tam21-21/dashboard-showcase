"""Lane DBG-7a: check 6's block length from the series' own correlation (Politis and White, as
corrected by Patton, Politis and White 2009; DECISIONS #478 item 4). Hand values are in the lane file."""
from __future__ import annotations

import math
import random
import sqlite3

import pytest

pd = pytest.importorskip("pandas")

from dashboard.backend import judge_checks, judgedb  # noqa: E402

SINGLE_BAR = pd.DataFrame([{"entry_index": 2, "exit_index": 2, "pnl": 1.0, "return_pct": 0.01, "direction": "long"}])


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _grade(conn, bars):
    return judge_checks.check_6_monte_carlo_bounds(7, conn, judgedb.BATTERY_VERSION, list(bars), SINGLE_BAR)


def test_t1_block_lengths_by_hand():
    b_sb, b_cb = judge_checks._pw_block_lengths({0: 1.0, 1: 0.5, 2: 0.0}, 2, 1000)
    assert b_cb == pytest.approx(0.375 ** (1 / 3) * 10, abs=1e-9)
    assert b_sb == pytest.approx(0.25 ** (1 / 3) * 10, abs=1e-9)
    assert math.ceil(b_cb) == 8


def test_t2_lag_window():
    rho = [0.9, 0.6, 0.3, 0.1, 0.05, 0.02, 0.01] + [0.0] * 8
    assert judge_checks._pw_lag_window(rho, 100) == (4, 8)
    assert judge_checks._pw_lag_window([0.5] * 15, 100) == (15, 15)


def test_t3_non_positive_s2_raises():
    with pytest.raises(judge_checks.BlockLengthUndefined, match="s2"):
        judge_checks._pw_block_lengths({0: 1.0, 1: -0.6}, 2, 1000)


def _ar1(seed: int, n: int = 800) -> list:
    r = random.Random(seed)
    x, out = 0.0, []
    for i in range(500 + n):
        x = 0.7 * x + r.gauss(0, 1)
        if i >= 500:
            out.append(x)
    return out


def test_t4_ar1_mean_block_length_and_cb_ratio():
    sbs = []
    for i in range(300):
        series = _ar1(20261029 * 1000 + i)
        n = len(series)
        mmax = math.ceil(math.sqrt(n)) + 5
        rho = judge_checks._pw_autocorrelations(series, mmax)
        _, M = judge_checks._pw_lag_window(rho, n)
        acv = judge_checks._pw_autocovariances(series, M)
        b_sb, b_cb = judge_checks._pw_block_lengths(acv, M, n)
        assert round(b_cb / b_sb, 5) == round(1.5 ** (1 / 3), 5)
        sbs.append(b_sb)
    assert 0.877 <= (sum(sbs) / len(sbs)) / 18.20 <= 0.977


def test_t5_constant_series_gives_one_and_exact_bound(conn):
    assert judge_checks._block_length_for([0.02] * 30) == 1
    result = _grade(conn, [0.02] * 30)
    assert result.quantities["bound"] == 0.02


def test_t6_too_short_not_evaluable(conn):
    bars = [0.01, -0.02, 0.03, 0.0, 0.015, -0.01, 0.02]
    result = _grade(conn, bars)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "block-length-undefined" in result.reason


def test_t7_zero_over_zero_correlation_not_evaluable(conn):
    bars = [0.01 if i % 2 == 0 else -0.01 for i in range(13)] + [0.0] * 47
    result = _grade(conn, bars)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "block-length-undefined" in result.reason


def test_descriptor_names_the_rule(conn):
    bars = [0.004 if i % 2 == 0 else -0.001 for i in range(60)]
    result = _grade(conn, bars)
    assert result.warnings[0]["detail"]["block_length_rule"] == "politis-white-cb"
