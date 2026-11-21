"""Lane P5: check 9 grades the random-entry arm from a stored result (k and m) beside the buy-and-hold arm.

The judge db is a real research.db file made by judgedb.init_judge_db (it seeds the frozen threshold rows, so the
0.05 bar, the 5,000 count and the (k + 1) / (m + 1) statistic are the real ones, battery 2026-10-10.a), never a
hand-rolled fake. One test per row of the plan's step 5 table. Guards (no random_arm_result) pin today's behaviour.
"""
from __future__ import annotations

import hashlib
import io
import sqlite3

import pytest

pd = pytest.importorskip("pandas")

from dashboard.backend import artifact_store, judge_checks, judgedb  # noqa: E402
from dashboard.backend.routes import judge as judge_routes  # noqa: E402

DATES = list(range(1_700_000_000, 1_700_000_000 + 10 * 86_400, 86_400))
GOOD = [0.01, 0.02, -0.005, 0.015, 0.01, 0.02, 0.0, 0.01, 0.02, 0.01]
FLAT = [0.0, 0.001, -0.001, 0.0, 0.001, -0.001, 0.0, 0.001, 0.0, -0.001]
POOR = [-0.01, 0.0, -0.02, 0.005, -0.01, 0.0, -0.01, 0.0, -0.01, 0.005]
SHORT_TRADES = pd.DataFrame([{"entry_index": 2, "exit_index": 2, "pnl": 1.0, "return_pct": 0.01, "direction": "long"}])

TODAYS_BH_PASSED_REASON_START = "the buy-and-hold arm passed (Sharpe margin "
TODAYS_BH_ABSENT_START = "the buy-and-hold arm is absent for this replay"


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS research_runs (id INTEGER PRIMARY KEY, annualization_factor REAL);")
    c.execute("INSERT INTO research_runs (id, annualization_factor) VALUES (1, 252.0);")
    c.execute("INSERT INTO research_runs (id, annualization_factor) VALUES (2, NULL);")
    c.commit()
    yield c
    c.close()


def _arm(k, m=5000, **extra):
    out = {"k": k, "m": m, "p_value": (k + 1) / (m + 1), "original_statistic": 0.001, "seed": 20261013}
    out.update(extra)
    return out


def _grade(conn, arm, *, run_id=1, candidate=GOOD, hold=FLAT, candidate_dates=DATES, hold_dates=DATES,
           hold_error=None):
    kwargs = dict(buy_hold_bar_returns=hold, buy_hold_error=hold_error,
                  bar_returns_dates=candidate_dates, buy_hold_bar_returns_dates=hold_dates)
    if arm is not None:  # the guard cases run on main too, so they never name the new keyword
        kwargs["random_arm_result"] = arm
    return judge_checks.check_9_controls(run_id, conn, judgedb.BATTERY_VERSION, candidate, SHORT_TRADES, **kwargs)


# --- guards: no random_arm_result, today's behaviour ---------------------------------------------

def test_guard_without_result_both_arm_pass_is_todays_not_evaluable(conn):
    result = _grade(conn, None)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert result.reason.startswith(TODAYS_BH_PASSED_REASON_START)
    assert "the random-entry arm alone is unbuilt" in result.reason
    assert result.quantities["buy_hold_verdict"] == "passed"


def test_guard_without_result_failed_buy_hold_is_failed(conn):
    result = _grade(conn, None, candidate=POOR, hold=GOOD)
    assert result.outcome == judge_checks.FAILED
    assert "the buy-and-hold arm's own Sharpe margin" in result.reason


def test_guard_without_result_absent_buy_hold_is_todays_reason(conn):
    result = _grade(conn, None, hold=None, hold_dates=None)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert result.reason.startswith(TODAYS_BH_ABSENT_START)


# --- step 5 (a): a missing threshold row, today's exit -------------------------------------------

def test_a_missing_threshold_row_is_todays_exit_even_with_a_result(conn):
    conn.execute("DELETE FROM research_judge_thresholds WHERE check_id = 9 AND quantity_key = 'random_arm_pass_bar';")
    result = _grade(conn, _arm(3))
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "random_arm_pass_bar" in result.reason


# --- (b) buy_hold_error ----------------------------------------------------------------------------

def test_b_buy_hold_error_is_todays_exit_even_with_a_result(conn):
    result = _grade(conn, _arm(3), hold=None, hold_dates=None, hold_error="the container ran out of memory")
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "the buy-and-hold arm's own computation failed inside the container: the container ran out of memory" in result.reason


# --- (c) m differs from the frozen replication count ---------------------------------------------

def test_c_wrong_replication_count_names_both_numbers(conn):
    result = _grade(conn, _arm(3, m=4000))
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "4000" in result.reason and "5000" in result.reason


# --- (d) the random arm fails: FAILED whatever the buy-and-hold arm shows ----------------------------

def test_d_random_arm_fails_with_buy_hold_passing(conn):
    # (249 + 1) / 5001 is under 0.05; (250 + 1) / 5001 = 0.0502 is not
    result = _grade(conn, _arm(250))
    assert result.outcome == judge_checks.FAILED
    assert result.quantities["random_arm_verdict"] == "failed"
    assert "random-entry" in result.reason


def test_d_random_arm_fails_with_buy_hold_absent(conn):
    result = _grade(conn, _arm(2000), hold=None, hold_dates=None)
    assert result.outcome == judge_checks.FAILED


def test_d_random_arm_fails_with_buy_hold_failing(conn):
    result = _grade(conn, _arm(2000), candidate=POOR, hold=GOOD)
    assert result.outcome == judge_checks.FAILED
    assert "random-entry" in result.reason


def test_d_strictly_below_bar_at_the_edge(conn):
    # m 5000: (k + 1) / 5001 < 0.05 holds for k up to 249 (250 / 5001 = 0.04999)
    assert _grade(conn, _arm(249)).outcome == judge_checks.CLEARED
    assert _grade(conn, _arm(250)).outcome == judge_checks.FAILED


# --- (e) the buy-and-hold arm's own other exits, random arm passing -------------------------------

def test_e_buy_hold_absent_with_random_passing_is_todays_not_evaluable(conn):
    result = _grade(conn, _arm(3), hold=None, hold_dates=None)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert result.reason.startswith(TODAYS_BH_ABSENT_START)


def test_e_buy_hold_without_dates_is_todays_not_evaluable(conn):
    result = _grade(conn, _arm(3), hold_dates=None)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "bar dates are missing" in result.reason


def test_e_under_two_shared_dates_is_todays_not_evaluable(conn):
    result = _grade(conn, _arm(3), hold_dates=[d + 10_000_000 for d in DATES])
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "fewer than two overlapping dates" in result.reason


def test_e_no_annualization_factor_is_todays_not_evaluable(conn):
    result = _grade(conn, _arm(3), run_id=2)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "annualization_factor is the missing input" in result.reason


def test_e_zero_deviation_is_todays_not_evaluable(conn):
    result = _grade(conn, _arm(3), hold=[0.001] * 10)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "zero standard deviation" in result.reason


# --- (f) the buy-and-hold arm fails, random passing -----------------------------------------------

def test_f_buy_hold_fails_with_random_passing_is_todays_failed_reason(conn):
    result = _grade(conn, _arm(3), candidate=POOR, hold=GOOD)
    assert result.outcome == judge_checks.FAILED
    assert "the buy-and-hold arm's own Sharpe margin" in result.reason
    assert result.quantities["buy_hold_verdict"] == "failed"


# --- (g) both pass --------------------------------------------------------------------------------

def test_g_both_arms_pass_is_cleared_with_both_in_quantities(conn):
    result = _grade(conn, _arm(3))
    assert result.outcome == judge_checks.CLEARED
    q = result.quantities
    assert q["buy_hold_verdict"] == "passed" and q["random_arm_verdict"] == "passed"
    assert q["k"] == 3 and q["m"] == 5000
    assert q["p_value"] == pytest.approx(4 / 5001)
    assert q["random_arm_pass_bar"] == 0.05
    assert q["seed"] == 20261013
    assert q["original_statistic"] == 0.001
    assert "sharpe_margin" in q and "candidate_sharpe" in q and "buy_hold_sharpe" in q


# --- the route: a declared random_arm artifact is verified like equity and trades ------------------

def _payload(arm_name, arm_sha, equity_sha, trades_sha):
    return {
        "provenance": {"python_version": "3.12"},
        "replay": {"member_dataset_sha256": "sha_AAPL", "artifacts": {
            "equity": {"name": "e.parquet", "sha256": equity_sha},
            "trades": {"name": "t.parquet", "sha256": trades_sha},
            "random_arm": {"name": arm_name, "sha256": arm_sha},
        }},
    }


def _put(store, run_id, name, data: bytes):
    store.put(run_id, name, io.BytesIO(data), sha256=hashlib.sha256(data).hexdigest())
    return hashlib.sha256(data).hexdigest()


def _store(tmp_path, monkeypatch):
    store = artifact_store.LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(judge_routes.artifact_store, "store", store)
    return store


def test_route_refuses_random_arm_artifact_with_wrong_sha(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    equity_sha = _put(store, 5, "e.parquet", b"equity")
    trades_sha = _put(store, 5, "t.parquet", b"trades")
    _put(store, 5, "ra.parquet", b"the real arm bytes")
    wrong = hashlib.sha256(b"something else").hexdigest()
    reason = judge_routes._replay_unverifiable_reason(5, _payload("ra.parquet", wrong, equity_sha, trades_sha))
    assert reason is not None and "artifacts.random_arm" in reason
    right = hashlib.sha256(b"the real arm bytes").hexdigest()
    assert judge_routes._replay_unverifiable_reason(5, _payload("ra.parquet", right, equity_sha, trades_sha)) is None


def test_route_refuses_a_declared_but_incomplete_entry(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    equity_sha = _put(store, 5, "e.parquet", b"equity")
    trades_sha = _put(store, 5, "t.parquet", b"trades")
    payload = _payload("ra.parquet", "x", equity_sha, trades_sha)
    payload["replay"]["artifacts"]["random_arm"] = {"name": "ra.parquet"}
    assert "artifacts.random_arm" in judge_routes._replay_unverifiable_reason(5, payload)


def test_route_guard_no_random_arm_declared_is_verifiable_as_today(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    equity_sha = _put(store, 5, "e.parquet", b"equity")
    trades_sha = _put(store, 5, "t.parquet", b"trades")
    payload = _payload("ra.parquet", "x", equity_sha, trades_sha)
    del payload["replay"]["artifacts"]["random_arm"]
    assert judge_routes._replay_unverifiable_reason(5, payload) is None


def _parquet_bytes(df):
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def test_route_reads_the_artifact_the_library_writes(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from research_lab.judge_arms.random_arm import write_random_arm_artifact

    store = _store(tmp_path, monkeypatch)
    path = tmp_path / "ra.parquet"
    write_random_arm_artifact({"k": 3, "m": 5000, "p_value": 4 / 5001, "original_statistic": 0.001, "seed": 20261013}, path)
    _put(store, 5, "ra.parquet", path.read_bytes())
    got = judge_routes._read_random_arm_artifact(5, "ra.parquet")
    assert got["k"] == 3 and got["m"] == 5000 and got["seed"] == 20261013
    assert got["original_statistic"] == pytest.approx(0.001)
    _put(store, 5, "bad.parquet", _parquet_bytes(pd.DataFrame({"k": [1]})))
    with pytest.raises(HTTPException) as err:
        judge_routes._read_random_arm_artifact(5, "bad.parquet")
    assert err.value.status_code == 409 and "artifacts.random_arm" in err.value.detail
