"""Lane GV2-8b: the sweep-result route takes the bars observed from the worker (DECISIONS #459 item 2).

A theory's search and walk-forward runs write no funnel rows, so their draws used to stay unobserved. The worker now sends the
count of bars it read in the sweep-result body. Every test runs on a scratch research.db built the way test_theory_jobs.py builds it.
"""
from __future__ import annotations

from dashboard.backend import researchdb
from dashboard.backend.tests.test_theory_jobs import (  # noqa: F401 - world is a pytest fixture
    GROUP, NOW, _cell, _rows, _search, _stage, world)

ENV = {"vbt_version": "1+x", "python_version": "3.12.0", "build_provenance": "git:x+vbt:y"}


def _post(world, run_id, **extra):
    body = {"annualization": None, "environment": ENV, "total_cells": 0}
    body.update(extra)
    return world["client"].post("/api/research/worker/runs/%d/sweep-result" % run_id, json=body)


def _observed(run_id):
    return _rows(
        "SELECT o.bars_observed AS bars FROM research_data_draws o JOIN research_data_draws l ON l.draw_id = o.draw_id "
        "WHERE o.event_kind = 'bars_observed' AND l.event_kind = 'run_linked' AND l.run_id = ? ORDER BY o.id;", (run_id,))


def _status(run_id):
    return researchdb.get_run(run_id)["status"]


def test_b1_a_run_with_a_draw_and_no_funnel_rows_records_the_bars_it_was_given(world):
    search = _search(world)
    assert _post(world, search["run_id"], bars_observed=123).status_code == 200
    assert _observed(search["run_id"]) == [{"bars": 123}]
    assert _status(search["run_id"]) == "succeeded"


def test_b2_zero_is_recorded_as_zero(world):
    search = _search(world)
    assert _post(world, search["run_id"], bars_observed=0).status_code == 200
    assert _observed(search["run_id"]) == [{"bars": 0}]


def test_b3_a_run_with_funnel_rows_refuses_the_field_and_stays_unfinalised(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    r = world["client"].post("/api/research/worker/runs/%d/sweep-chunk" % opt["run_id"], json={"cells": [_cell(0, "e" * 64, GROUP)]})
    assert r.status_code == 200, r.text
    r = _post(world, opt["run_id"], bars_observed=50, total_cells=1)
    assert r.status_code == 422
    assert "has funnel rows; its bars observed come from them" in r.json()["detail"]
    assert _status(opt["run_id"]) != "succeeded"
    assert _observed(opt["run_id"]) == []


def test_b4_a_second_post_with_the_field_is_refused_and_leaves_one_row(world):
    search = _search(world)
    assert _post(world, search["run_id"], bars_observed=123).status_code == 200
    r = _post(world, search["run_id"], bars_observed=456)
    assert r.status_code == 409
    assert "already records bars observed" in r.json()["detail"]
    assert _observed(search["run_id"]) == [{"bars": 123}]


def test_b5_a_negative_count_is_refused(world):
    search = _search(world)
    assert _post(world, search["run_id"], bars_observed=-1).status_code == 422
    assert _status(search["run_id"]) != "succeeded"


def test_b6_guard_without_the_field_the_funnel_sum_is_still_recorded(world):
    search = _search(world)
    opt = _stage(world, "optimise", search["run_id"], NOW + 1)
    cells = [_cell(0, "e" * 64, GROUP), _cell(1, "e" * 64, GROUP)]
    r = world["client"].post("/api/research/worker/runs/%d/sweep-chunk" % opt["run_id"], json={"cells": cells})
    assert r.status_code == 200, r.text
    assert _post(world, opt["run_id"], total_cells=2).status_code == 200
    assert _observed(opt["run_id"]) == [{"bars": 1500}]


def test_b7_guard_without_the_field_and_no_funnel_rows_records_nothing(world):
    search = _search(world)
    assert _post(world, search["run_id"]).status_code == 200
    assert _observed(search["run_id"]) == []
