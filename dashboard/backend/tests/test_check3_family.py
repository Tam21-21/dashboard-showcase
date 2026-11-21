"""Lane GV2-5c: judge check 3 on a generator v2 trial family, and the approve route refusing a group label.

Every test runs on a fresh research.db in tmp_path, never a real one. Rows are inserted with plain SQL, the way
research_lab/probes/bar_size_behaviour/_judge_step.py and test_paper_synthetic_refusal.py do. The expected numbers
are written out by hand here with statistics.NormalDist, not taken from the code under test.
"""
from __future__ import annotations

import json
import math
import sqlite3
from statistics import NormalDist

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import judge_checks, judgedb, paperdb, researchdb, reservedb, retestdb
from dashboard.backend.routes import judge as judge_routes

LABEL = "GROUP_SYN_VOL"
MEMBER_SHA = "m" * 64
SCREEN = (1_072_915_200, 1_483_142_400)
OPTIMISE = (1_483_228_800, 1_577_750_400)
SHARPE, ANN, SKEW, EXCESS_KURT, BARS = 1.2, 252.0, -0.3, 1.5, 750
GAMMA = 0.5772156649015328459  # Euler-Mascheroni, AFML p. 275


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
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_strategies (id, name, source_code, version, last_mirror_status, "
            "created_at, updated_at) VALUES (1, 'ma_cross', 'x', 1, 'pending', 1700000000, 1700000000);"
        )
    return research_db, paper_db


def _dataset(source="synthetic", symbol="SYN_VOL_01") -> int:
    with researchdb.transaction() as conn:
        return int(conn.execute(
            "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, dataset_type, "
            "created_at) VALUES (?, ?, ?, '1d', 1072915200, 1262304000, 'ready', 'price_dataset', 1700000000);",
            (symbol, source, symbol)).lastrowid)


def _run(kind: str, dataset_id: int, ann: "float | None" = ANN, init_cash: float = 5000.0) -> int:
    with researchdb.transaction() as conn:
        job = conn.execute(
            "INSERT INTO research_jobs (kind, status, payload_json, created_at) VALUES (?, 'succeeded', ?, 1700000000) "
            "RETURNING id;", (kind, json.dumps({"init_cash": init_cash}))).fetchone()
        return int(conn.execute(
            "INSERT INTO research_runs (job_id, strategy_id, strategy_version, dataset_id, dataset_sha256, "
            "settings_snapshot, created_at, status, annualization_factor) "
            "VALUES (?, 1, 1, ?, ?, '[]', 1700000000, 'succeeded', ?) RETURNING id;",
            (job["id"], dataset_id, "b" * 64, ann)).fetchone()["id"])


def _draws(run_id: int, with_screen=True) -> None:
    with researchdb.transaction() as conn:
        if with_screen:
            conn.execute(
                "INSERT INTO research_data_draws (event_kind, strategy_id, strategy_version, calendar_id, "
                "counts_against_budget, stage, window_start_ts, window_end_ts, decision, created_at) "
                "VALUES ('draw_requested', 1, 1, 'cal', 1, 'screen', ?, ?, 'granted', 1700000000);", SCREEN)
        draw = conn.execute(
            "INSERT INTO research_data_draws (event_kind, strategy_id, strategy_version, calendar_id, "
            "counts_against_budget, stage, window_start_ts, window_end_ts, decision, created_at) "
            "VALUES ('draw_requested', 1, 1, 'cal', 1, 'optimise', ?, ?, 'granted', 1700000001) RETURNING id;",
            OPTIMISE).fetchone()
        conn.execute(
            "INSERT INTO research_data_draws (event_kind, strategy_id, strategy_version, calendar_id, "
            "counts_against_budget, decision, run_id, draw_id, created_at) "
            "VALUES ('run_linked', 1, 1, 'cal', 1, 'granted', ?, ?, 1700000002);", (run_id, draw["id"]))


def _cell(run_id: int, ticker: str, variation_id=0) -> None:
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_run_cells (run_id, variation_id, ticker, row_scope, total_return, sharpe_ratio, "
            "max_drawdown, num_trades, win_rate, total_fees_paid, total_slippage_cost, profit_factor, skew, kurtosis) "
            "VALUES (?, ?, ?, 'cell', 0.1, ?, 0.05, 40, 0.5, 1.0, 1.0, 1.2, ?, ?);",
            (run_id, variation_id, ticker, SHARPE, SKEW, EXCESS_KURT))
        conn.execute(
            "INSERT INTO research_run_funnel (run_id, variation_id, ticker, row_scope, bars_total, bars_after_warmup, "
            "raw_entries, entries_after_dedup, raw_exits, fills, total_trades, exits_by_signal, exits_by_eod, zero_reason) "
            "VALUES (?, ?, ?, 'cell', ?, ?, 10, 10, 10, 20, 10, 10, 0, 'N/A');",
            (run_id, variation_id, ticker, BARS + 20, BARS))


def _payload(run_id: int, variation_id: int, ticker: str) -> None:
    researchdb.catalog._ensure_strategies_on_path()
    from strategies.helpers.full_metrics import resolve_formula_version  # noqa: PLC0415

    run = researchdb.get_run(run_id)
    fv = resolve_formula_version(run["strategy_version"], run["build_provenance"] or "")
    researchdb.put_full_metrics_cache(
        run_id, variation_id, ticker, fv,
        json.dumps({"replay": {"member_dataset_sha256": MEMBER_SHA, "artifacts": {}}}), now=1700000000)


def _family(k, v, reason=None, n_trials=40) -> None:
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_trial_families (run_id, theory_name, group_label, member_set_sha256, window_start_ts, "
            "window_end_ts, n_trials, n_in_onc, k_effective, onc_cap, cap_bound, v, selected_frequency, reason, "
            "trials_sha256, returns_sha256, created_at) VALUES (999, 'th', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.1, ?, 't', 'r', 1700000000);",
            (LABEL, MEMBER_SHA, SCREEN[0], SCREEN[1], n_trials,
             None if reason else 30, k, None if reason else 8, None if reason else 0, v, reason))


def _theory_run(with_screen=True) -> int:
    run_id = _run("run_theory_stage", _dataset())
    _draws(run_id, with_screen)
    _cell(run_id, LABEL)
    _payload(run_id, 0, LABEL)
    return run_id


def _check3(run_id, variation_id=0, ticker=LABEL):
    with researchdb.get_conn() as conn:
        return judge_checks.check_3_trial_count(run_id, conn, judgedb.BATTERY_VERSION, variation_id=variation_id, ticker=ticker)


def _bar() -> float:
    with researchdb.get_conn() as conn:
        return judgedb.read_threshold(conn, 3, "dsr_pass_bar", judgedb.BATTERY_VERSION)["value_numeric"]


def _hand(k, v):
    z = NormalDist()
    sr = SHARPE / math.sqrt(ANN)
    g4 = EXCESS_KURT + 3.0
    star = math.sqrt(v) * ((1 - GAMMA) * z.inv_cdf(1 - 1 / k) + GAMMA * z.inv_cdf(1 - math.exp(-1) / k))
    stat = (sr - star) * math.sqrt(BARS - 1) / math.sqrt(1 - SKEW * sr + (g4 - 1) / 4 * sr * sr)
    return star, stat, z.cdf(stat)


def test_c1_family_path_matches_hand_computation(stores):
    run_id = _theory_run()
    _family(12, 0.0004)
    r = _check3(run_id)
    star, stat, dsr = _hand(12, 0.0004)
    assert r.quantities["sr_star"] == pytest.approx(star, rel=1e-12)
    assert r.quantities["psr_statistic"] == pytest.approx(stat, rel=1e-12)
    assert r.quantities["dsr"] == pytest.approx(dsr, rel=1e-12)
    assert r.quantities["k_effective"] == 12 and r.quantities["v"] == 0.0004 and r.quantities["n_trials"] == 40
    assert r.outcome == (judge_checks.CLEARED if dsr > _bar() else judge_checks.FAILED)


def test_c2_k_below_two(stores):
    run_id = _theory_run()
    _family(1, 0.0004)
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE and "k_effective" in r.reason


def test_c3_no_family_row(stores):
    run_id = _theory_run()
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE and "trial family" in r.reason


def test_c4_v_zero(stores):
    run_id = _theory_run()
    _family(12, 0.0)
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE and r.reason.startswith("v is")


def test_c4b_too_few_trials(stores):
    run_id = _theory_run()
    _family(None, None, reason="too-few-trials", n_trials=3)
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE and r.reason.startswith("too-few-trials")


def test_c4c_no_screen_draw(stores):
    run_id = _theory_run(with_screen=False)
    _family(12, 0.0004)
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE and r.reason.startswith("screen-window-unresolved")


def test_c5_guard_sweep_run_keeps_scope_owed_word(stores):
    run_id = _run("run_sweep", _dataset("eodhd", "AAPL"))
    _cell(run_id, "AAPL")
    with researchdb.get_conn() as conn:
        r = judge_checks.check_3_trial_count(run_id, conn, judgedb.BATTERY_VERSION, variation_id=0)
    assert r.outcome == judge_checks.NOT_EVALUABLE and r.reason == judge_checks.WORD_TRIAL_COUNT_SCOPE_OWED


def test_c6_guard_single_run_graded_on_trial_rows(stores):
    ds = _dataset("eodhd", "AAPL")
    run_id = _run("run_backtest", ds)
    _cell(run_id, "AAPL")
    with researchdb.transaction() as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO research_trials (run_id, variation_id, ticker, strategy_id, strategy_version, dataset_id, "
                "dataset_sha256, split_id, param_config_hash, sharpe, trial_kind, selection_scope, created_at) "
                "VALUES (?, ?, '', 1, 1, ?, ?, 'FULL-SAMPLE', ?, 0.5, 'sweep_combo', 'cumulative', 1700000000);",
                (run_id, i, ds, "b" * 64, f"h{i}"))
    with researchdb.get_conn() as conn:
        r = judge_checks.check_3_trial_count(run_id, conn, judgedb.BATTERY_VERSION)
    z = NormalDist()
    sqrt_v = 1 / math.sqrt(BARS - 1)
    star = sqrt_v * ((1 - GAMMA) * z.inv_cdf(1 - 1 / 5) + GAMMA * z.inv_cdf(1 - math.exp(-1) / 5))
    assert r.quantities["n_trials"] == 5
    assert r.quantities["sr_star"] == pytest.approx(star, rel=1e-12)
    assert "k_effective" not in r.quantities


@pytest.fixture()
def client(stores):
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


def test_c7_approve_refuses_group_label(stores, client):
    _, paper_db = stores
    run_id = _run("run_sweep", _dataset("eodhd", "AAPL"))
    with judgedb.get_judge_conn(judgedb.DB_PATH) as conn:
        vid = judgedb.record_verdict(
            conn, run_id=run_id, judging_pass_id="00000001-1111-4111-8111-111111111111", variation_id=0, ticker=LABEL,
            graded_formula_version="1|3|x", strategy_id=1, strategy_version=1, dataset_sha256="b" * 64, verdict="pass",
            is_advisory=0, battery_version=judgedb.BATTERY_VERSION, evidence_floor_outcome="cleared",
            check_outcomes_json=json.dumps([{"check": n, "outcome": "cleared"} for n in range(1, 10)]),
            decided_by="initial", warnings_json="[]", graded_at=1700000001)
    r = client.post(f"/api/judge/verdicts/{vid}/approve", json={"slice_opening_cash": 5000.0})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("group-label-not-tradable")
    raw = sqlite3.connect(str(paper_db))
    try:
        counts = [raw.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
                  for t in ("paper_runs", "paper_slice_current", "paper_equity")]
    finally:
        raw.close()
    assert counts == [0, 0, 0]


def test_c8_guard_fresh_database_init_twice(stores):
    researchdb.init_db()
    researchdb.init_db()
    run_id = _theory_run()
    _family(12, 0.0004)
    r = _check3(run_id)
    assert r.quantities["dsr"] == pytest.approx(_hand(12, 0.0004)[2], rel=1e-12)
