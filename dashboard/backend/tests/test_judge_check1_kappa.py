"""Lane DBG-7a: check 1's kappa never crashes; each missing-kappa case names its own reason."""
from __future__ import annotations

import random

import pytest

from dashboard.backend import judge_checks


def test_k1_equal_m1_m2_gives_a_reason_not_a_crash():
    kappa, reason = judge_checks._taleb_kappa_1([2.0, -1.0, -1.0, 0.0])
    assert kappa is None
    assert "M(2) equals M(1)" in reason


def test_k2_three_trades():
    kappa, reason = judge_checks._taleb_kappa_1([0.1, 0.2, 0.3])
    assert kappa is None
    assert reason == "fewer than 4 trades"


def test_k3_dof_p02_fixture_unchanged():
    r = random.Random(2)
    returns = [r.gauss(0.001, 0.002) for _ in range(20)]
    kappa, reason = judge_checks._taleb_kappa_1(returns)
    assert reason is None
    assert kappa == pytest.approx(-0.415, abs=0.001)


# --- DBG-7b (#479 item 7): a negative kappa estimate reads as 0 -----------------------------------

pd = pytest.importorskip("pandas")

from dashboard.backend import judgedb, paperdb, researchdb, reservedb, retestdb  # noqa: E402


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
    return research_db, paper_db


def test_k4_negative_estimate_reads_as_zero(stores):
    # By hand: M(1) = 0.012, M(2) = 0.016, kappa = 2 - ln 2 / ln(4/3) = -0.409421.
    trades = pd.DataFrame({"return_pct": [0.02, -0.004, 0.004, -0.02]})
    with researchdb.transaction() as conn:
        result = judge_checks.check_1_evidence_floor(1, conn, trades)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "bars-total-absent" in result.reason
    assert result.quantities["kappa"] == 0.0
    assert result.quantities["kappa_estimate"] == pytest.approx(-0.409421, abs=1e-6)


def test_k5_positive_kappa_has_no_estimate_key(stores):
    r = random.Random(1)
    returns = [r.gauss(0.001, 0.005) for _ in range(60)]
    trades = pd.DataFrame({"return_pct": returns})
    with researchdb.transaction() as conn:
        result = judge_checks.check_1_evidence_floor(1, conn, trades)
    assert result.quantities["kappa"] == pytest.approx(0.052119, abs=1e-6)
    assert "kappa_estimate" not in result.quantities
