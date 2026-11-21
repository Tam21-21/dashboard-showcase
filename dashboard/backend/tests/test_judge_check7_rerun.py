"""Lane P4: check 7 grades from a re-run result (k and m) when the route passes one.

The judge db is a real research.db file made by judgedb.init_judge_db (it seeds the frozen threshold
rows, so the 0.05 bar is the real one, battery 2026-10-10.a), never a hand-rolled fake.
Guards (t2, t3) pin the behaviour without a re-run result to today's.
"""
from __future__ import annotations

import hashlib
import io
import sqlite3

import pytest

pd = pytest.importorskip("pandas")

from dashboard.backend import artifact_store, judge_checks, judgedb  # noqa: E402
from dashboard.backend.routes import judge as judge_routes  # noqa: E402

MULTI_BAR = pd.DataFrame([{"entry_index": 0, "exit_index": 4, "pnl": 1.0, "return_pct": 0.01, "direction": "long"}])
SINGLE_BAR = pd.DataFrame([{"entry_index": 2, "exit_index": 2, "pnl": 1.0, "return_pct": 0.01, "direction": "long"}])

TODAYS_MULTI_BAR_REASON = (
    "the MCPT shuffle flavor for a multi-bar hold is the missing input: #213's own "
    "'What is deliberately left to spec time' paragraph reserves which permutation scheme "
    "survives a multi-day hold (judge_spec_brief.md), and this build does not invent one"
)


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _grade(conn, rerun, bar_returns=(0.01, -0.02, 0.015, 0.0, 0.01), trades=MULTI_BAR):
    bars = list(bar_returns) if bar_returns is not None else None
    if rerun is None:  # the guard cases run on main too, so they do not name the new keyword
        return judge_checks.check_7_mcpt(1, conn, judgedb.BATTERY_VERSION, bars, trades)
    return judge_checks.check_7_mcpt(1, conn, judgedb.BATTERY_VERSION, bars, trades, rerun_result=rerun)


def _rerun(k, m=4999, **extra):
    out = {"k": k, "m": m, "p_value": (k + 1) / (m + 1), "seed": 20261013, "statistic": "sharpe",
           "original_performance": 1.5}
    out.update(extra)
    return out


def test_t1_multi_bar_rerun_k3_is_cleared(conn):
    result = _grade(conn, _rerun(3))
    assert result.outcome == judge_checks.CLEARED
    assert result.quantities["k"] == 3 and result.quantities["m"] == 4999
    assert result.quantities["p_value"] == pytest.approx(0.0008)
    assert result.quantities["seed"] == 20261013
    assert result.quantities["pass_bar"] == 0.05


def test_t1_multi_bar_rerun_k400_is_failed(conn):
    result = _grade(conn, _rerun(400))
    assert result.outcome == judge_checks.FAILED
    assert result.quantities["p_value"] == pytest.approx(401 / 5000)


def test_t1_strictly_below_bar_k249_fails(conn):
    # (249 + 1) / 5000 = 0.05 exactly: the frozen bar is strictly below, so it fails.
    assert _grade(conn, _rerun(249)).outcome == judge_checks.FAILED
    assert _grade(conn, _rerun(248)).outcome == judge_checks.CLEARED


def test_t2_multi_bar_without_rerun_keeps_todays_reason(conn):
    result = _grade(conn, None)
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert result.reason == TODAYS_MULTI_BAR_REASON


def test_t3_single_bar_without_rerun_is_todays_shuffle(conn):
    series = [0.01, -0.02, 0.015, 0.0, 0.01, -0.005, 0.02, -0.01]
    first = judge_checks.check_7_mcpt(7, conn, judgedb.BATTERY_VERSION, series, SINGLE_BAR)
    again = judge_checks.check_7_mcpt(7, conn, judgedb.BATTERY_VERSION, series, SINGLE_BAR)
    assert first.quantities == again.quantities and first.reason == again.reason
    # Pinned from main (run id 7 fixes the seed): a change to the shuffle loop shows here.
    assert first.quantities["k"] == 2066 and first.quantities["m"] == 4999
    assert first.quantities["seed"] == 7000028
    assert first.outcome == judge_checks.FAILED


def test_t5_wrong_replication_count_is_not_evaluable(conn):
    result = _grade(conn, _rerun(3, m=4000))
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "4000" in result.reason and "4999" in result.reason


def test_t6_rerun_without_bar_returns_is_still_graded(conn):
    result = _grade(conn, _rerun(3), bar_returns=None)
    assert result.outcome == judge_checks.CLEARED
    assert result.quantities["k"] == 3


def test_missing_threshold_still_wins_over_rerun(conn):
    conn.execute("DELETE FROM research_judge_thresholds WHERE check_id = 7;")
    result = _grade(conn, _rerun(3))
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "mcpt_pvalue_threshold" in result.reason


def _replay_payload(mcpt_name, mcpt_sha, equity_name, equity_sha, trades_name, trades_sha):
    return {
        "provenance": {"python_version": "3.12"},
        "replay": {"member_dataset_sha256": "sha_AAPL", "artifacts": {
            "equity": {"name": equity_name, "sha256": equity_sha},
            "trades": {"name": trades_name, "sha256": trades_sha},
            "mcpt": {"name": mcpt_name, "sha256": mcpt_sha},
        }},
    }


def _put(store, run_id, name, data: bytes):
    store.put(run_id, name, io.BytesIO(data), sha256=hashlib.sha256(data).hexdigest())
    return hashlib.sha256(data).hexdigest()


def test_t4_route_refuses_mcpt_artifact_with_wrong_sha(tmp_path, monkeypatch):
    store = artifact_store.LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(judge_routes.artifact_store, "store", store)
    equity_sha = _put(store, 5, "e.parquet", b"equity")
    trades_sha = _put(store, 5, "t.parquet", b"trades")
    _put(store, 5, "m.parquet", b"the real mcpt bytes")
    wrong = hashlib.sha256(b"something else").hexdigest()
    reason = judge_routes._replay_unverifiable_reason(
        5, _replay_payload("m.parquet", wrong, "e.parquet", equity_sha, "t.parquet", trades_sha))
    assert reason is not None and "artifacts.mcpt" in reason
    right = hashlib.sha256(b"the real mcpt bytes").hexdigest()
    assert judge_routes._replay_unverifiable_reason(
        5, _replay_payload("m.parquet", right, "e.parquet", equity_sha, "t.parquet", trades_sha)) is None


def test_t4_declared_but_incomplete_mcpt_entry_is_refused(tmp_path, monkeypatch):
    store = artifact_store.LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(judge_routes.artifact_store, "store", store)
    equity_sha = _put(store, 5, "e.parquet", b"equity")
    trades_sha = _put(store, 5, "t.parquet", b"trades")
    payload = _replay_payload("m.parquet", "x", "e.parquet", equity_sha, "t.parquet", trades_sha)
    payload["replay"]["artifacts"]["mcpt"] = {"name": "m.parquet"}
    assert "artifacts.mcpt" in judge_routes._replay_unverifiable_reason(5, payload)


def test_no_mcpt_declared_is_still_verifiable_as_today(tmp_path, monkeypatch):
    store = artifact_store.LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(judge_routes.artifact_store, "store", store)
    equity_sha = _put(store, 5, "e.parquet", b"equity")
    trades_sha = _put(store, 5, "t.parquet", b"trades")
    payload = _replay_payload("m.parquet", "x", "e.parquet", equity_sha, "t.parquet", trades_sha)
    del payload["replay"]["artifacts"]["mcpt"]
    assert judge_routes._replay_unverifiable_reason(5, payload) is None


def _parquet_bytes(df):
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def test_route_reads_the_artifact_the_library_writes(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from research_lab.judge_arms.mcpt_bars import write_mcpt_artifact

    store = artifact_store.LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(judge_routes.artifact_store, "store", store)
    path = tmp_path / "m.parquet"
    write_mcpt_artifact({"k": 3, "m": 4999, "p_value": 0.0008, "original": 1.5, "seed": 20261013,
                         "statistic": "sharpe", "dropped_dates": 0}, path)
    _put(store, 5, "m.parquet", path.read_bytes())
    got = judge_routes._read_mcpt_artifact(5, "m.parquet")
    assert got["k"] == 3 and got["m"] == 4999 and got["seed"] == 20261013 and got["statistic"] == "sharpe"
    _put(store, 5, "bad.parquet", _parquet_bytes(pd.DataFrame({"k": [1]})))
    with pytest.raises(HTTPException) as err:
        judge_routes._read_mcpt_artifact(5, "bad.parquet")
    assert err.value.status_code == 409 and "artifacts.mcpt" in err.value.detail


def test_t7_rerun_error_is_not_evaluable_and_never_the_bar_shuffle(conn):
    result = judge_checks.check_7_mcpt(1, conn, judgedb.BATTERY_VERSION, [0.01, -0.02, 0.015, 0.0, 0.01], SINGLE_BAR,
                                       rerun_error="x missing")
    assert result.outcome == judge_checks.NOT_EVALUABLE
    assert "x missing" in result.reason and "#451" in result.reason
    assert result.quantities == {}


def test_t8_rerun_result_with_rerun_error_raises(conn):
    with pytest.raises(ValueError):
        judge_checks.check_7_mcpt(1, conn, judgedb.BATTERY_VERSION, [0.01, -0.02], SINGLE_BAR,
                                  rerun_result=_rerun(3), rerun_error="x missing")


def _declared(tmp_path, monkeypatch):
    from research_lab.judge_arms.mcpt_bars import write_mcpt_artifact

    store = artifact_store.LocalArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(judge_routes.artifact_store, "store", store)
    path = tmp_path / "m.parquet"
    write_mcpt_artifact({"k": 3, "m": 4999, "p_value": 0.0008, "original": 1.5, "seed": 1,
                         "statistic": "sharpe_after_costs", "dropped_dates": 0}, path)
    _put(store, 5, "m.parquet", path.read_bytes())
    return {"mcpt": {"name": "m.parquet", "sha256": "x"}}


def _payload(artifacts=None, engine="research_lab.theory_stage", **replay):
    provenance = {"python_version": "3.12"}
    if engine is not None:
        provenance["engine"] = engine
    return {"provenance": provenance, "replay": dict({"artifacts": artifacts or {}}, **replay)}


def test_t9_check7_rerun_inputs(tmp_path, monkeypatch):
    from fastapi import HTTPException

    made = _declared(tmp_path, monkeypatch)
    for bad in (None, {}, "x"):
        with pytest.raises(HTTPException) as e0:
            judge_routes._check7_rerun_inputs(5, {"provenance": bad, "replay": {"artifacts": {}}})
        assert e0.value.status_code == 409 and "provenance" in e0.value.detail
        assert e0.value.detail.startswith(judge_routes.WORD_REPLAY_UNVERIFIABLE)
    assert judge_routes._check7_rerun_inputs(5, _payload(mcpt_error="x missing")) == (None, "x missing")
    got, err = judge_routes._check7_rerun_inputs(5, _payload(made))
    assert err is None and got["k"] == 3 and got["m"] == 4999
    with pytest.raises(HTTPException) as e1:
        judge_routes._check7_rerun_inputs(5, _payload(made, mcpt_error="x missing"))
    assert e1.value.status_code == 409 and e1.value.detail.startswith(judge_routes.WORD_REPLAY_UNVERIFIABLE)
    assert "artifacts.mcpt" in e1.value.detail and "mcpt_error" in e1.value.detail
    for bad in (None, "", 7):
        with pytest.raises(HTTPException) as e2:
            judge_routes._check7_rerun_inputs(5, _payload(mcpt_error=bad))
        assert e2.value.status_code == 409 and "mcpt_error" in e2.value.detail
        assert e2.value.detail.startswith(judge_routes.WORD_REPLAY_UNVERIFIABLE)
    assert judge_routes._check7_rerun_inputs(5, _payload()) == (
        None, "made before check 7 re-ran theory strategies (GV3-J7); re-run its optimise step")
    assert judge_routes._check7_rerun_inputs(5, _payload(engine="vbt_sweep")) == (None, None)
    assert judge_routes._check7_rerun_inputs(5, _payload(engine=None)) == (None, None)
