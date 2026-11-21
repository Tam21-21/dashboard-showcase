"""Lane GV3-J: check 8 with no losing trade follows Pardo's formula as an exact zero (DECISIONS #469 item 1).

With 45 winners and no loser, every winner sits in one run, so the gross profit minus the biggest run is the sum of
nothing: 0.0 exactly. A running += and Python's sum differ in the last digits, so the old code left a residue of
about 1e-12 that decided CLEARED or FAILED. The five parquet files are GV2-7c SYN_VOL's replay trades, copied unchanged.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

from dashboard.backend import judge_checks  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "gv2_7c_syn_vol"


def _frame(pnls):
    return pd.DataFrame({"entry_index": list(range(len(pnls))), "pnl": pnls})


def test_no_loser_with_float_residue_fails_at_exact_zero():
    pnls = [0.1, 0.2, 0.3, 1e-16, 0.7] * 9
    result = judge_checks.check_8_windfall(_frame(pnls))
    assert result.outcome == judge_checks.FAILED
    assert result.quantities["winning_trade_count"] == 45
    assert result.quantities["losing_trade_count"] == 0
    assert result.quantities["prom_minus_biggest_run"] == 0.0


@pytest.mark.parametrize("variation", [0, 1, 2, 3, 4])
def test_gv2_7c_syn_vol_trades_fail_at_exact_zero(variation):
    trades = pd.read_parquet(FIXTURES / ("replay_trades_v%d_GROUP_SYN_VOL.parquet" % variation))
    result = judge_checks.check_8_windfall(trades)
    assert result.quantities["winning_trade_count"] == 45
    assert result.quantities["losing_trade_count"] == 0
    assert result.quantities["prom_minus_biggest_run"] == 0.0
    assert result.outcome == judge_checks.FAILED


def test_equal_entry_index_keeps_stored_order():
    # entry_index 1 is shared by a 5 and a -1. Stored order 5, -1 gives runs [5, 5] and [5]; stored order -1, 5 gives [5] and [5, 5].
    first = pd.DataFrame({"entry_index": [0, 1, 1, 2], "pnl": [5.0, 5.0, -1.0, 5.0]})
    assert judge_checks.check_8_windfall(first).quantities["biggest_winning_run_profit"] == 10.0
    second = pd.DataFrame({"entry_index": [0, 1, 1, 2], "pnl": [5.0, -1.0, 5.0, 5.0]})
    assert judge_checks.check_8_windfall(second).quantities["biggest_winning_run_profit"] == 10.0
    third = pd.DataFrame({"entry_index": [0, 1, 1, 2], "pnl": [5.0, 7.0, -1.0, 5.0]})
    assert judge_checks.check_8_windfall(third).quantities["biggest_winning_run_profit"] == 12.0
    fourth = pd.DataFrame({"entry_index": [0, 1, 1, 2], "pnl": [5.0, -1.0, 7.0, 5.0]})
    assert judge_checks.check_8_windfall(fourth).quantities["biggest_winning_run_profit"] == 12.0


def test_equal_runs_take_the_first_as_the_biggest():
    result = judge_checks.check_8_windfall(_frame([5.0, 5.0, -1.0, 5.0, 5.0]))
    q = result.quantities
    assert q["biggest_winning_run_profit"] == 10.0
    assert q["gross_profit"] == 20.0
    expected = (4 - math.sqrt(4)) * (10.0 / 4) - (1 + math.sqrt(1)) * 1.0
    assert q["prom_minus_biggest_run"] == expected
