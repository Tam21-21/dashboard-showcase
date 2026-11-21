"""Lane JG1: record_retest reaches "passed" only by the measured drift (DECISIONS #452).

A real research.db made by retestdb.init_retest_db. d2, d3 and d4 are guards: they pass at base too.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from dashboard.backend import retestdb

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "scripts" / "probes" / "nautilus_retest"))
import _lib as nr  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "research.db"
    retestdb.init_retest_db(path)
    return path


def _record(db, run_id, shadow=0.005, gap_eq=None, gap_sh=None):
    rid = retestdb.record_retest(
        run_id=run_id, verdict_id=None, created_at=1_700_000_300,
        shadow_copy_completed_at=1_700_000_100, fills_read_at=1_700_000_200,
        status="report-only", engine_version="1.231.0", python_version="3.12.13",
        matched_configuration=nr.complete_matched_configuration(),
        export_path=f"/artifacts/{run_id}/retest_fills.parquet",
        divergence_vs_shadow=shadow, divergence_vs_shadow_absent_reason=None,
        divergence_vs_vbt=0.5, divergence_vs_vbt_absent_reason=None,
        gap_final_equity=gap_eq, gap_sharpe=gap_sh, research_db_path=db,
    )
    c = sqlite3.connect(str(db))
    status = c.execute("SELECT status FROM research_retests WHERE id = ?;", (rid,)).fetchone()[0]
    c.close()
    return status


def test_d1_residue_allowance_alone_no_longer_passes(db):
    c = sqlite3.connect(str(db))
    c.execute("UPDATE research_retest_parameters SET residue_allowance = 0.01 WHERE id = 1;")
    c.commit()
    c.close()
    assert _record(db, 1, shadow=0.005) == "report-only"


def test_d2_drift_row_and_small_gaps_pass(db):
    retestdb.set_drift_allowance(db, 0.11, 0.12, "test")
    assert _record(db, 2, gap_eq=0.05, gap_sh=0.05) == "passed"


def test_d3_gap_above_drift_stays_report_only(db):
    retestdb.set_drift_allowance(db, 0.11, 0.12, "test")
    assert _record(db, 3, gap_eq=0.2, gap_sh=0.05) == "report-only"


def test_d4_missing_gaps_stay_report_only(db):
    retestdb.set_drift_allowance(db, 0.11, 0.12, "test")
    assert _record(db, 4, gap_eq=None, gap_sh=None) == "report-only"
