"""Lane GV2-7b: generator v2 theory jobs in the research store (docs/lanes/GV2-7b.md, DECISIONS #445).

Every test runs on a fresh research.db in tmp_path, never a real one. The research side is built with
the real researchdb.init_db; rows come from the real dispatch functions, and the relay of an optimise
stage goes through the real worker routes with a FastAPI TestClient. No worker, no container.
"""
from __future__ import annotations

import calendar
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import artifact_store, judgedb, researchdb, reservedb, retestdb, stage_runner, tracker
from dashboard.backend.routes import judge as judge_routes
from dashboard.backend.routes import research as research_routes

NOW = 1_800_000_000
WORKER_KEY = "test-worker-key"
THEORY = '[theory]\nname = "gap_fade_test"\nbar = "1d"\ndisplay_name = "Gap fade test"\n'
GROUP = "GROUP_TEST"
TICKERS = ["SYN_VOL_01", "SYN_VOL_02"]
COSTS = {"fees": "none", "slippage": 0.0005}
INIT_CASH = 100000.0
SEARCH = {"seed": 20261012, "n_iter": 50, "onc_cap": 8, "optimise_window": ["2017-01-03", "2019-12-31"]}
VARIATIONS = [
    {"id": 0, "picks": {"A": 5, "B": 10}, "exits": {"calibration": {"sigma": 0.01, "buckets": {"0": {"stop_sigma": 2.0, "target_sigma": 3.0}}}}},
    {"id": 1, "picks": {"A": 7, "B": 12}, "exits": {"calibration": {"sigma": 0.01, "buckets": {"0": {"stop_sigma": 2.0, "target_sigma": 3.0}}}}},
]
WINDOWS = {
    "screen": ("2004-01-02", "2016-12-30"),
    "optimise": ("2017-01-03", "2019-12-31"),
    "walkforward": ("2020-01-02", "2022-12-30"),
}


def _ts(day: str) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0))


@pytest.fixture()
def world(tmp_path, monkeypatch):
    path = tmp_path / "research.db"
    monkeypatch.setattr(researchdb, "DB_PATH", path)
    monkeypatch.setattr(judgedb, "DB_PATH", path)
    monkeypatch.setattr(reservedb, "DB_PATH", path)
    researchdb.init_db()
    judgedb.init_judge_db(path)
    reservedb.init_reserve_db(path)
    retestdb.init_retest_db(path)
    monkeypatch.setenv("RESEARCH_WORKER_API_KEY", WORKER_KEY)
    monkeypatch.setattr(artifact_store, "store", artifact_store.LocalArtifactStore(tmp_path / "artifacts"))
    shas = {}
    for ticker in TICKERS:
        shas[ticker] = hashlib.sha256(ticker.encode()).hexdigest()
        with researchdb.transaction() as conn:
            conn.execute(
                "INSERT INTO research_datasets (name, source, symbol, timeframe, start_ts, end_ts, status, dataset_type, "
                "created_at, file_path, sha256, rows) VALUES (?, 'synthetic', ?, '1d', ?, ?, 'ready', 'price_dataset', ?, ?, ?, 4000);",
                (ticker, ticker, _ts("2003-01-02"), _ts("2022-12-30"), NOW, "/data/synthetic/%s.1d.parquet" % ticker, shas[ticker]),
            )
    universes = {}
    for stage, (start, end) in WINDOWS.items():
        universes[stage] = researchdb.create_price_universe("U_" + stage, TICKERS, _ts(start), _ts(end))["universe_id"]
    app = FastAPI()
    app.include_router(research_routes.worker_router)
    client = TestClient(app, headers={"X-Worker-API-Key": WORKER_KEY})
    return {"universes": universes, "shas": shas, "client": client}


def _search(world, calendar_id="dry1", now=NOW):
    return stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], now, calendar_id=calendar_id, theory_text=THEORY, group_label=GROUP,
        data_start="2003-01-02", search=SEARCH, costs=COSTS, init_cash=INIT_CASH)


def _stage(world, stage, search_run_id, now):
    return stage_runner.dispatch_theory_stage(stage, world["universes"][stage], now, search_run_id=search_run_id, variations=VARIATIONS)


def _job_payload(run_id):
    with researchdb.get_conn() as conn:
        row = conn.execute("SELECT j.kind AS kind, j.payload_json AS p, j.status AS status FROM research_runs r "
                           "JOIN research_jobs j ON j.id = r.job_id WHERE r.id = ?;", (run_id,)).fetchone()
    return row["kind"], json.loads(row["p"]), row["status"]


def _rows(sql, args=()):
    with researchdb.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def test_j1_optimise_dispatch_writes_draw_run_registration_job_link(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    draws = _rows("SELECT * FROM research_data_draws WHERE stage = 'optimise' AND event_kind = 'draw_requested';")
    assert len(draws) == 1 and draws[0]["decision"] == "granted"
    assert draws[0]["strategy_id"] == search["strategy_id"] and draws[0]["calendar_id"] == "dry1"
    assert draws[0]["counts_against_budget"] == 0
    links = _rows("SELECT * FROM research_data_draws WHERE event_kind = 'run_linked' AND run_id = ?;", (opt["run_id"],))
    assert len(links) == 1 and links[0]["decision"] == "granted" and links[0]["draw_id"] == draws[0]["id"]
    assert len(_rows("SELECT id FROM research_runs WHERE id = ?;", (opt["run_id"],))) == 1
    reg = _rows("SELECT * FROM research_sweep_registrations WHERE run_id = ?;", (opt["run_id"],))
    assert len(reg) == 1 and reg[0]["registered_n"] == 2
    assert json.loads(reg[0]["grid_json"]) == {"0": VARIATIONS[0]["picks"], "1": VARIATIONS[1]["picks"]}
    kind, payload, status = _job_payload(opt["run_id"])
    assert kind == "run_theory_stage" and status == "pending"
    assert payload["run_id"] == opt["run_id"] and payload["stage"] == "optimise"
    assert payload["theory_toml"] == THEORY and payload["group_label"] == GROUP
    assert payload["members"] == [
        {"ticker": t, "dataset_path": "/data/synthetic/%s.1d.parquet" % t, "sha256": world["shas"][t]} for t in TICKERS]
    assert (payload["window_start"], payload["window_end"]) == WINDOWS["optimise"]
    assert payload["data_start"] == "2003-01-02"
    assert payload["variations"] == VARIATIONS and payload["seed"] == SEARCH["seed"]
    assert payload["costs"] == COSTS and payload["init_cash"] == INIT_CASH
    assert payload["cscv_s"] == 16 and payload.get("walkforward") is None
    assert payload["strategy_id"] == search["strategy_id"] and payload["calendar_id"] == "dry1"


def test_j1_walkforward_payload_carries_the_three_ruled_values(world):
    search = _search(world)
    _stage(world, "optimise", search["run_id"], NOW + 1)
    wf = _stage(world, "walkforward", search["run_id"], NOW + 2)
    _, payload, _ = _job_payload(wf["run_id"])
    assert payload["walkforward"] == researchdb.read_walkforward_parameters()
    assert payload["stage"] == "walkforward" and payload.get("cscv_s") is None
    assert (payload["window_start"], payload["window_end"]) == WINDOWS["walkforward"]


def test_j1_search_payload_and_one_strategy_for_all_stages(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    wf = _stage(world, "walkforward", search["run_id"], NOW + 2)
    kind, payload, _ = _job_payload(search["run_id"])
    assert kind == "run_theory_search" and payload["screen_window"] == list(WINDOWS["screen"])
    assert payload["optimise_window"] == SEARCH["optimise_window"] and payload["seed"] == SEARCH["seed"]
    assert payload["calendar_id"] == "dry1"
    runs = _rows("SELECT strategy_id, strategy_version FROM research_runs ORDER BY id;")
    assert len({(r["strategy_id"], r["strategy_version"]) for r in runs}) == 1
    assert len(_rows("SELECT id FROM research_strategies;")) == 1
    stages = [r["stage"] for r in _rows("SELECT stage FROM research_data_draws WHERE event_kind = 'draw_requested' ORDER BY id;")]
    assert stages == ["screen", "optimise", "walkforward"]
    assert opt["strategy_id"] == wf["strategy_id"] == search["strategy_id"]


def test_j1_a_stage_earlier_than_the_last_is_refused(world):
    search = _search(world)
    _stage(world, "walkforward", search["run_id"], NOW + 1)
    with pytest.raises(tracker.ChronologyRefused):
        _stage(world, "optimise", search["run_id"], NOW + 2)
    refused = _rows("SELECT * FROM research_data_draws WHERE decision = 'refused';")
    assert len(refused) == 1 and refused[0]["refusal_reason"] == "window_not_strictly_later"


def test_j1_a_member_without_a_ready_dataset_is_refused_before_any_row(world):
    with researchdb.transaction() as conn:
        conn.execute("UPDATE research_datasets SET status = 'pending' WHERE symbol = 'SYN_VOL_02';")
    with pytest.raises(researchdb.TheoryMemberNotReady):
        _search(world)
    assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []


def test_j2_run_nature_of_both_kinds_is_sweep(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    assert researchdb.run_nature(search["run_id"]) == "sweep"
    assert researchdb.run_nature(opt["run_id"]) == "sweep"


def test_j3_the_strategy_row_validates_and_refuses_to_run(world, monkeypatch):
    search = _search(world)
    row = researchdb.get_strategy(search["strategy_id"])
    description = researchdb.validate_strategy_source(row["source_code"])
    assert "gap_fade_test" in description
    assert THEORY in row["source_code"] or repr(THEORY) in row["source_code"]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "research_lab"))
    namespace: dict = {}
    exec(compile(row["source_code"], "<theory strategy>", "exec"), namespace)
    strategy_classes = [v for v in namespace.values() if isinstance(v, type) and v.__name__ != "TheoryRunsOnlyAsJobs"
                        and any(b.__name__ == "Strategy" for b in v.__mro__[1:])]
    assert len(strategy_classes) == 1
    with pytest.raises(Exception) as caught:
        strategy_classes[0]().run(None, None)
    assert type(caught.value).__name__ == "TheoryRunsOnlyAsJobs"


def _family_body(**changes):
    body = {
        "theory_name": "gap_fade_test", "group_label": GROUP, "member_set_sha256": "a" * 64,
        "window_start": "2004-01-02", "window_end": "2016-12-30", "n_trials": 120, "n_in_onc": 100, "k_effective": 6,
        "onc_cap": 8, "cap_bound": False, "v": 0.012, "selected_frequency": 252.0, "reason": None,
        "trials_sha256": "b" * 64, "returns_sha256": "c" * 64, "n_cleared_bar": 50, "n_zero_variance": 3, "cluster_sizes": [10, 90],
    }
    body.update(changes)
    return body


def test_j4_theory_family_route(world):
    client = world["client"]
    first = _search(world)["run_id"]
    r = client.post("/api/research/worker/runs/%d/theory-family" % first, json=_family_body())
    assert r.status_code == 200, r.text
    key = ("a" * 64, _ts("2004-01-02"), _ts("2016-12-30"))
    row = researchdb.family_for(*key)
    assert row["id"] == r.json()["id"] and row["run_id"] == first and row["k_effective"] == 6 and row["cap_bound"] == 0

    missing = _family_body()
    del missing["v"]
    assert client.post("/api/research/worker/runs/%d/theory-family" % first, json=missing).status_code == 422

    # a repeat POST for the same run is refused and names the existing row
    again = client.post("/api/research/worker/runs/%d/theory-family" % first, json=_family_body())
    assert again.status_code == 409 and ("row %d" % row["id"]) in again.json()["detail"]

    # a re-run of the same theory name appends a row; family_for returns the newest
    second = _search(world, calendar_id="dry2", now=NOW + 10)["run_id"]
    r2 = client.post("/api/research/worker/runs/%d/theory-family" % second, json=_family_body(v=0.5))
    assert r2.status_code == 200
    assert researchdb.family_for(*key)["id"] == r2.json()["id"] and researchdb.family_for(*key)["v"] == 0.5
    assert len(_rows("SELECT id FROM research_trial_families;")) == 2

    # a different theory name on the same key is recorded too (GV3-FU2: the merged family is the union's job)
    third = _search(world, calendar_id="dry3", now=NOW + 20)["run_id"]
    r3 = client.post("/api/research/worker/runs/%d/theory-family" % third, json=_family_body(theory_name="another_theory"))
    assert r3.status_code == 200, r3.text
    assert len(_rows("SELECT id FROM research_trial_families;")) == 3

    # too few trials: the five numbers are null
    fourth = _search(world, calendar_id="dry4", now=NOW + 30)["run_id"]
    tiny = _family_body(member_set_sha256="d" * 64, n_in_onc=None, k_effective=None, onc_cap=None, cap_bound=None, v=None, reason="too-few-trials")
    assert client.post("/api/research/worker/runs/%d/theory-family" % fourth, json=tiny).status_code == 200
    assert researchdb.family_for("d" * 64, _ts("2004-01-02"), _ts("2016-12-30"))["reason"] == "too-few-trials"
    # unknown run
    assert client.post("/api/research/worker/runs/9999/theory-family", json=_family_body()).status_code == 404


def test_j4_the_check_refuses_nulls_without_the_reason_and_the_reason_with_numbers(world):
    run_id = _search(world)["run_id"]
    with pytest.raises(sqlite3.IntegrityError):
        researchdb.record_trial_family(run_id, _family_body(k_effective=None))
    with pytest.raises(sqlite3.IntegrityError):
        researchdb.record_trial_family(run_id, _family_body(reason="too-few-trials"))
    assert _rows("SELECT id FROM research_trial_families;") == []


def test_j5_a_fresh_database_has_the_table_and_its_index(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "fresh.db")
    researchdb.init_db()
    researchdb.init_db()
    names = {(r["type"], r["name"]) for r in _rows("SELECT type, name FROM sqlite_master;")}
    assert ("table", "research_trial_families") in names
    index = [r for r in _rows("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'research_trial_families';")
             if "member_set_sha256" in (r["sql"] or "")]
    assert len(index) == 1
    assert "(member_set_sha256, window_start_ts, window_end_ts, id)" in index[0]["sql"].replace("\n", " ")


def _cell(variation_id, sha, label):
    cell = {"variation_id": variation_id, "ticker": label, "row_scope": "cell", "param_json": json.dumps(VARIATIONS[variation_id]["picks"], sort_keys=True, separators=(",", ":")),
            "dataset_sha256": sha}
    cell.update({"total_return": 0.1 + variation_id, "sharpe_ratio": 0.5, "max_drawdown": 0.2, "num_trades": 30, "win_rate": 0.5,
                 "total_fees_paid": 0.0, "total_slippage_cost": 12.5, "profit_factor": 1.4, "skew": 0.1, "kurtosis": 2.0})
    cell.update({"bars_total": 750, "bars_after_warmup": 700, "raw_entries": 40, "entries_after_dedup": 30, "raw_exits": 30, "fills": 60,
                 "total_trades": 30, "exits_by_signal": 30, "exits_by_eod": 0, "zero_reason": "N/A"})
    return cell


def _relay_optimise(world, run_id):
    client = world["client"]
    base = "/api/research/worker/runs/%d" % run_id
    sha = "e" * 64
    cells = [_cell(0, sha, GROUP), _cell(1, sha, GROUP)]
    r = client.post(base + "/sweep-chunk", json={"cells": cells})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == 2
    files = {}
    for name in ("replay_equity_v0_%s.parquet" % GROUP, "replay_trades_v0_%s.parquet" % GROUP):
        data = ("bytes of " + name).encode()
        files[name] = hashlib.sha256(data).hexdigest()
        r = client.post(base + "/artifacts", data={"manifest": json.dumps({name: files[name]})}, files=[("files", (name, io.BytesIO(data), "application/octet-stream"))])
        assert r.status_code == 200, r.text
    names = list(files)
    cell_metrics = {k: cells[0][k] for k in ("total_return", "sharpe_ratio", "max_drawdown", "num_trades", "win_rate", "total_fees_paid",
                                              "total_slippage_cost", "profit_factor", "skew", "kurtosis")}
    payload = {"sharpe": 0.5, "provenance": {"engine": "research_lab.theory_stage"}, "replay": {
        "cell_metrics": cell_metrics, "expected_cell_metrics": cell_metrics, "tolerance": "exact", "member_dataset_sha256": sha,
        "artifacts": {"equity": {"name": names[0], "sha256": files[names[0]]}, "trades": {"name": names[1], "sha256": files[names[1]]}},
        "buy_hold_error": None, "buy_hold_artifacts": None, "replayed_at": NOW}}
    r = client.post(base + "/full-metrics-cache", json={"variation_id": 0, "ticker": GROUP, "formula_version": "1|1|git:x+vbt:y", "payload": payload})
    assert r.status_code == 200, r.text
    r = client.post(base + "/sweep-result", json={"annualization": {"annualization_factor": 252.0, "annualization_freq": "1 days 00:00:00",
                                                                     "annualization_year_freq": "252 days 00:00:00"},
                                                  "environment": {"vbt_version": "1+x", "python_version": "3.12.0", "build_provenance": "git:x+vbt:y"},
                                                  "total_cells": 2})
    assert r.status_code == 200, r.text


def test_j6_the_judge_route_admits_the_relayed_optimise_cell(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    _relay_optimise(world, opt["run_id"])
    assert researchdb.get_run(opt["run_id"])["status"] == "succeeded"
    cells = _rows("SELECT variation_id, ticker FROM research_run_cells WHERE run_id = ? ORDER BY variation_id;", (opt["run_id"],))
    assert cells == [{"variation_id": 0, "ticker": GROUP}, {"variation_id": 1, "ticker": GROUP}]
    assert [json.loads(r["param_json"]) for r in _rows("SELECT param_json FROM research_run_variations WHERE run_id = ? ORDER BY variation_id;", (opt["run_id"],))] \
        == [VARIATIONS[0]["picks"], VARIATIONS[1]["picks"]]
    with researchdb.get_conn() as conn:
        run, variation_id, ticker, is_reserve = judge_routes._resolve_admission(opt["run_id"], 0, GROUP, conn, for_replay=False)
    assert (variation_id, ticker, is_reserve) == (0, GROUP, False) and run["id"] == opt["run_id"]


def test_j7_check2_finds_the_walkforward_run_dispatched_after_the_optimise_one(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    wf = _stage(world, "walkforward", search["run_id"], NOW + 2)
    researchdb.complete_sweep(wf["run_id"], None, {})
    with researchdb.get_conn() as conn:
        assert judge_routes._resolve_check2_walkforward_run(conn, opt["run_id"]) == (None, wf["run_id"])


@pytest.mark.parametrize("label", ["SYN_VOL", "group_test", "GROUP_", "GROUP_a", "GROUP-X", ""])
def test_j8_a_group_label_the_stage_container_would_refuse_is_refused_before_any_draw(world, label):
    with pytest.raises(researchdb.TheoryRunInvalid) as caught:
        stage_runner.dispatch_theory_stage(
            "screen", world["universes"]["screen"], NOW, calendar_id="dry1", theory_text=THEORY, group_label=label,
            data_start="2003-01-02", search=SEARCH, costs=COSTS, init_cash=INIT_CASH)
    assert "GROUP_" in str(caught.value)
    assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []


def test_j9_fees_ibkr_fixed_is_refused_before_any_draw_and_alpaca_is_accepted(world):
    """FEE1a: Alpaca is the broker (DECISIONS #456), so a theory run priced with IBKR's fixed fee is refused up front."""
    with pytest.raises(researchdb.TheoryRunInvalid) as caught:
        stage_runner.dispatch_theory_stage(
            "screen", world["universes"]["screen"], NOW, calendar_id="dry1", theory_text=THEORY, group_label=GROUP,
            data_start="2003-01-02", search=SEARCH, costs={"fees": "ibkr_fixed", "slippage": 0.0005}, init_cash=INIT_CASH)
    assert "#456" in str(caught.value) and "ibkr_fixed" in str(caught.value) and "alpaca" in str(caught.value)
    assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []
    alpaca = {"fees": "alpaca", "slippage": 0.0005}
    run = stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], NOW, calendar_id="dry1", theory_text=THEORY, group_label=GROUP,
        data_start="2003-01-02", search=SEARCH, costs=alpaca, init_cash=INIT_CASH)
    _, payload, _ = _job_payload(run["run_id"])
    assert payload["costs"] == alpaca


def _search_ending(world, last_day):
    return stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], NOW, calendar_id="dry1", theory_text=THEORY, group_label=GROUP,
        data_start="2003-01-02", search=dict(SEARCH, optimise_window=["2017-01-03", last_day]), costs=COSTS, init_cash=INIT_CASH)


def test_j10_a_search_whose_optimise_window_reaches_the_reserve_is_refused_before_any_draw(world):
    """SL1 (DECISIONS #318, #399 item 4): the search reads its optimise window, so that window may not touch the reserve."""
    assert reservedb.read_reserve_cut_date_ts() == _ts("2023-01-01")
    for last_day in ("2023-01-03", "2023-01-01"):
        with pytest.raises(researchdb.TheoryRunInvalid) as caught:
            _search_ending(world, last_day)
        assert "2023-01-01" in str(caught.value) and "reserve" in str(caught.value) and last_day in str(caught.value)
        assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []


def test_j10_an_optimise_window_ending_before_the_reserve_cut_is_accepted(world):
    """SL1 guard: the last session before the cut (2022-12-30) is accepted."""
    run = _search_ending(world, "2022-12-30")
    _, payload, _ = _job_payload(run["run_id"])
    assert payload["optimise_window"] == ["2017-01-03", "2022-12-30"]


@pytest.mark.parametrize("window", [["2017-01-03"], "2017-01-03", ["2017-01-03", 20191231], ["2017-01-03", "31/12/2019"], None])
def test_j10_a_malformed_optimise_window_is_refused_by_name(world, window):
    with pytest.raises(researchdb.TheoryRunInvalid) as caught:
        stage_runner.dispatch_theory_stage(
            "screen", world["universes"]["screen"], NOW, calendar_id="dry1", theory_text=THEORY, group_label=GROUP,
            data_start="2003-01-02", search=dict(SEARCH, optimise_window=window), costs=COSTS, init_cash=INIT_CASH)
    assert "optimise_window" in str(caught.value)
    assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []


# ---- GV3-F: the final test's settings (kept-back stocks and the later years) ---------------------------------------------------

GOOD_FINAL = {"kept_back": ["SYN_VOL_02"], "later_window": ["2023-01-03", "2026-06-30"]}


def _search_with_final(world, final):
    return stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], NOW, calendar_id="dry1", theory_text=THEORY, group_label=GROUP,
        data_start="2003-01-02", search=dict(SEARCH, final=final), costs=COSTS, init_cash=INIT_CASH)


def test_f1_a_good_final_is_accepted_and_rides_the_search_payload(world):
    run = _search_with_final(world, GOOD_FINAL)
    _, payload, _ = _job_payload(run["run_id"])
    assert payload["final"] == GOOD_FINAL


@pytest.mark.parametrize("final, word", [
    ({"kept_back": ["SYN_VOL_02", "SYN_XXX_09"], "later_window": GOOD_FINAL["later_window"]}, "SYN_XXX_09"),   # a ticker not in the group
    ({"kept_back": ["SYN_VOL_01", "SYN_VOL_02"], "later_window": GOOD_FINAL["later_window"]}, "whole group"),   # the whole group
    ({"kept_back": ["SYN_VOL_02"], "later_window": ["2016-12-30", "2026-06-30"]}, "2016-12-30"),               # starts on the screen end
    ({"kept_back": ["SYN_VOL_02"], "later_window": ["2010-01-04", "2026-06-30"]}, "2010-01-04"),               # starts before the screen end
    ({"kept_back": ["SYN_VOL_02"], "later_window": ["2023-01-03"]}, "two dates"),                              # not two dates
    ({"kept_back": ["SYN_VOL_02"], "later_window": ["2023-01-03", "soon"]}, "soon"),                           # not a date
    ({"kept_back": ["SYN_VOL_02"]}, "later_window"),                                                           # a key missing
    ({"kept_back": [], "later_window": GOOD_FINAL["later_window"]}, "kept_back"),                              # nothing kept back
    ({"kept_back": "SYN_VOL_02", "later_window": GOOD_FINAL["later_window"]}, "kept_back"),                    # not a list
    ({"kept_back": ["SYN_VOL_02"], "later_window": GOOD_FINAL["later_window"], "extra": 1}, "extra"),          # an unknown key
])
def test_f2_a_bad_final_is_refused_by_name_before_any_draw(world, final, word):
    with pytest.raises(researchdb.TheoryRunInvalid) as caught:
        _search_with_final(world, final)
    assert word in str(caught.value)
    assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []


def test_f3_a_later_window_that_starts_before_the_reserve_cut_is_refused(world):
    """#473: the final test reads the newest years after the walk-forward, so its later window may not overlap an earlier stage's years."""
    assert reservedb.read_reserve_cut_date_ts() == _ts("2023-01-01")
    with pytest.raises(researchdb.TheoryRunInvalid) as caught:
        _search_with_final(world, {"kept_back": ["SYN_VOL_02"], "later_window": ["2022-06-01", "2026-06-30"]})
    assert "2022-06-01" in str(caught.value) and "reserve cut" in str(caught.value) and "2023-01-01" in str(caught.value)
    assert _rows("SELECT id FROM research_data_draws;") == [] and _rows("SELECT id FROM research_runs;") == []
