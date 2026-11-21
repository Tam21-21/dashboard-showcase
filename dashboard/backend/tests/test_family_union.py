"""Lane GV3-FU2: the merged trial family, server side (docs/lanes/GV3-FU2.md, call 37, AFML p. 275).

Every test runs on a fresh research.db in tmp_path, never a real one. The union.json the worker would post is written by hand here
(GV3-FU1's worker job is not needed). Searches are dispatched with the real dispatch functions; the routes go through a TestClient.
"""
from __future__ import annotations

import json
import math
from statistics import NormalDist

import pytest

from dashboard.backend import judge_checks, researchdb, stage_runner
from dashboard.backend.tests.test_check3_family import (  # noqa: F401 - stores is a fixture the check 3 tests share
    LABEL, MEMBER_SHA, SCREEN, _check3, _theory_run, stores)
from dashboard.backend.tests.test_theory_jobs import (  # noqa: F401 - world is a fixture
    COSTS, GROUP, INIT_CASH, NOW, SEARCH, THEORY, _family_body, _rows, _ts, world)

KEY = ("a" * 64, _ts("2004-01-02"), _ts("2016-12-30"))
POST = "/api/research/worker/runs/%d/theory-family"
UNIONS = "/api/research/worker/family-unions"
GAMMA = 0.5772156649015328459  # Euler-Mascheroni, AFML p. 275


def _search_with_settings(world, calendar_id, now, search):
    return stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], now, calendar_id=calendar_id, theory_text=THEORY, group_label=GROUP,
        data_start="2003-01-02", search=search, costs=COSTS, init_cash=INIT_CASH)


def _record(world, theory, calendar_id, now, search=None, **changes):
    """Dispatch one search run and post its family under the theory name; returns (run_id, family row id)."""
    run = _search_with_settings(world, calendar_id, now, SEARCH if search is None else search)
    r = world["client"].post(POST % run["run_id"], json=_family_body(theory_name=theory, **changes))
    assert r.status_code == 200, r.text
    return run["run_id"], r.json()["id"]


def _union_jobs():
    return [dict(r, payload=json.loads(r["payload_json"])) for r in
            _rows("SELECT * FROM research_jobs WHERE kind = 'run_family_union' ORDER BY id;")]


def _union_body(job_id, run_ids, **changes):
    body = {"job_id": job_id, "run_ids": run_ids, "member_set_sha256": "a" * 64, "window_start": "2004-01-02",
            "window_end": "2016-12-30", "onc_cap_asked": 10, "seed": 20261012, "n_trials": 240, "n_in_onc": 200,
            "k_effective": 9, "onc_cap": 10, "cap_bound": False, "v": 0.02, "reason": None, "selected_frequency": None,
            "extra_key_the_route_ignores": 1}
    body.update(changes)
    return body


def test_u1_two_theories_on_one_key_enqueue_one_union_job(world):
    a, _ = _record(world, "theory_a", "c1", NOW, search=dict(SEARCH, onc_cap=7, seed=111))
    assert _union_jobs() == []                                   # one theory alone enqueues nothing
    b, _ = _record(world, "theory_b", "c2", NOW + 10, search=dict(SEARCH, onc_cap=9, seed=222))
    jobs = _union_jobs()
    assert len(jobs) == 1 and jobs[0]["status"] == "pending"
    assert jobs[0]["payload"] == {"runs": [a, b], "onc_cap": 7, "seed": 111, "member_set_sha256": "a" * 64,
                                  "window_start": "2004-01-02", "window_end": "2016-12-30"}
    assert len(_rows("SELECT id FROM research_trial_families;")) == 2
    c, _ = _record(world, "theory_c", "c3", NOW + 20)
    jobs = _union_jobs()
    assert [j["payload"]["runs"] for j in jobs] == [[a, b], [a, b, c]]
    assert jobs[1]["payload"]["onc_cap"] == 7 and jobs[1]["payload"]["seed"] == 111


def test_u1_a_repeat_of_a_family_is_still_refused_and_enqueues_nothing(world):
    a, _ = _record(world, "theory_a", "c1", NOW)
    _record(world, "theory_b", "c2", NOW + 10)
    again = world["client"].post(POST % a, json=_family_body(theory_name="theory_a"))
    assert again.status_code == 409 and "already has trial family row" in again.json()["detail"]
    assert len(_union_jobs()) == 1


def test_u1_the_same_theory_twice_enqueues_nothing_and_a_rerun_stands_in_the_job(world):
    a1, _ = _record(world, "theory_a", "c1", NOW)
    a2, _ = _record(world, "theory_a", "c2", NOW + 10)
    assert _union_jobs() == [] and a1 != a2
    b, _ = _record(world, "theory_b", "c3", NOW + 20)
    assert [j["payload"]["runs"] for j in _union_jobs()] == [sorted([a2, b])]   # the newest run of each theory


def test_u2_post_a_union_records_a_row_and_refuses_a_repeat_and_a_wrong_run_set(world):
    a, _ = _record(world, "theory_a", "c1", NOW)
    b, _ = _record(world, "theory_b", "c2", NOW + 10)
    job_id = _union_jobs()[0]["id"]
    client = world["client"]
    r = client.post(UNIONS, json=_union_body(job_id, [a, b]))
    assert r.status_code == 200, r.text
    rows = _rows("SELECT * FROM research_trial_family_unions;")
    assert len(rows) == 1 and rows[0]["id"] == r.json()["id"] and rows[0]["job_id"] == job_id
    assert rows[0]["member_set_sha256"] == "a" * 64 and rows[0]["window_start_ts"] == KEY[1] and rows[0]["window_end_ts"] == KEY[2]
    assert json.loads(rows[0]["run_ids_json"]) == [a, b]
    assert (rows[0]["n_trials"], rows[0]["n_in_onc"], rows[0]["k_effective"], rows[0]["onc_cap"], rows[0]["cap_bound"], rows[0]["v"],
            rows[0]["reason"]) == (240, 200, 9, 10, 0, 0.02, None)
    again = client.post(UNIONS, json=_union_body(job_id, [a, b]))
    assert again.status_code == 409 and ("row %d" % rows[0]["id"]) in again.json()["detail"]
    assert len(_rows("SELECT id FROM research_trial_family_unions;")) == 1
    # a second job, then bodies that do not match it
    _record(world, "theory_c", "c3", NOW + 20)
    job2 = _union_jobs()[1]
    abc = job2["payload"]["runs"]
    assert client.post(UNIONS, json=_union_body(job2["id"], [a, b])).status_code == 422                       # not the job's runs
    assert client.post(UNIONS, json=_union_body(job2["id"], abc, window_end="2016-12-29")).status_code == 422  # another key
    assert client.post(UNIONS, json=_union_body(job2["id"], abc, member_set_sha256="z" * 64)).status_code == 422
    assert client.post(UNIONS, json=_union_body(99999, abc)).status_code == 404
    assert len(_rows("SELECT id FROM research_trial_family_unions;")) == 1


def test_u2_too_few_trials_union_is_recorded_with_nulls(world):
    a, _ = _record(world, "theory_a", "c1", NOW)
    b, _ = _record(world, "theory_b", "c2", NOW + 10)
    job_id = _union_jobs()[0]["id"]
    body = _union_body(job_id, [a, b], n_in_onc=None, k_effective=None, onc_cap=None, cap_bound=None, v=None, reason="too-few-trials")
    assert world["client"].post(UNIONS, json=body).status_code == 200
    assert researchdb.family_for(*KEY)["reason"] == "too-few-trials"


def test_u3_family_for_one_theory_two_theories_and_a_union(world):
    a, row_a = _record(world, "theory_a", "c1", NOW, v=0.5)
    one = researchdb.family_for(*KEY)
    assert one["id"] == row_a and one["v"] == 0.5 and "union" not in one
    b, _ = _record(world, "theory_b", "c2", NOW + 10)
    with pytest.raises(researchdb.FamilyUnionPending) as caught:
        researchdb.family_for(*KEY)
    assert "theory_a" in str(caught.value) and "theory_b" in str(caught.value) and caught.value.job_id is None
    job_id = _union_jobs()[0]["id"]
    uid = world["client"].post(UNIONS, json=_union_body(job_id, [a, b])).json()["id"]
    fam = researchdb.family_for(*KEY)
    assert fam["union"] is True and fam["union_id"] == uid and "id" not in fam
    assert (fam["n_trials"], fam["k_effective"], fam["v"], fam["reason"]) == (240, 9, 0.02, None)
    # a third theory makes the union stale
    _record(world, "theory_c", "c3", NOW + 20)
    with pytest.raises(researchdb.FamilyUnionPending) as caught:
        researchdb.family_for(*KEY)
    assert "theory_c" in str(caught.value)


def test_u3_a_failed_union_job_is_named_and_the_next_theory_enqueues_a_new_job(world):
    _record(world, "theory_a", "c1", NOW)
    _record(world, "theory_b", "c2", NOW + 10)
    job_id = _union_jobs()[0]["id"]
    r = world["client"].post("/api/research/worker/jobs/%d/complete" % job_id, json={"status": "failed", "error": "docker exit 1: boom"})
    assert r.status_code == 200
    with pytest.raises(researchdb.FamilyUnionPending) as caught:
        researchdb.family_for(*KEY)
    assert caught.value.job_id == job_id and "docker exit 1: boom" in str(caught.value)
    _record(world, "theory_c", "c3", NOW + 20)
    assert len(_union_jobs()) == 2
    with pytest.raises(researchdb.FamilyUnionPending) as caught:   # the newest job is pending again, so no failure is named
        researchdb.family_for(*KEY)
    assert caught.value.job_id is None


def test_u5_guard_complete_closes_a_union_job_that_has_no_run_row(world):
    _record(world, "theory_a", "c1", NOW)
    _record(world, "theory_b", "c2", NOW + 10)
    job_id = _union_jobs()[0]["id"]
    assert _rows("SELECT id FROM research_runs WHERE job_id = ?;", (job_id,)) == []
    r = world["client"].post("/api/research/worker/jobs/%d/complete" % job_id, json={"status": "succeeded", "result": {"ok": True}})
    assert r.status_code == 200
    assert _rows("SELECT status FROM research_jobs WHERE id = ?;", (job_id,))[0]["status"] == "succeeded"


def _two_theory_families(k=12, v=0.0004):
    """Two single-theory family rows on check 3's fixture key, as test_check3_family._family writes one."""
    for i, theory in enumerate(("th_a", "th_b")):
        with researchdb.transaction() as conn:
            conn.execute(
                "INSERT INTO research_trial_families (run_id, theory_name, group_label, member_set_sha256, window_start_ts, "
                "window_end_ts, n_trials, n_in_onc, k_effective, onc_cap, cap_bound, v, selected_frequency, reason, "
                "trials_sha256, returns_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, 40, 30, ?, 8, 0, ?, 0.1, NULL, 't', 'r', 1700000000);",
                (900 + i, theory, LABEL, MEMBER_SHA, SCREEN[0], SCREEN[1], k, v))


def _union_row(run_ids, k, v, job_id=1):
    with researchdb.transaction() as conn:
        return conn.execute(
            "INSERT INTO research_trial_family_unions (job_id, member_set_sha256, window_start_ts, window_end_ts, run_ids_json, "
            "n_trials, n_in_onc, k_effective, onc_cap, cap_bound, v, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, 80, 60, ?, 10, 0, ?, NULL, 1700000001) RETURNING id;",
            (job_id, MEMBER_SHA, SCREEN[0], SCREEN[1], json.dumps(sorted(run_ids)), k, v)).fetchone()["id"]


def test_u4_check3_with_two_theories_and_no_union_is_not_evaluable(stores):
    run_id = _theory_run()
    _two_theory_families()
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE
    assert r.reason == "family-union-pending: 2 theories searched this data and their merged family is not computed yet"


def test_u4_check3_names_a_failed_union_job(stores):
    run_id = _theory_run()
    _two_theory_families()
    payload = {"runs": [900, 901], "member_set_sha256": MEMBER_SHA,
               "window_start": researchdb._ts_to_date(SCREEN[0]), "window_end": researchdb._ts_to_date(SCREEN[1])}
    with researchdb.transaction() as conn:
        job = conn.execute(
            "INSERT INTO research_jobs (kind, status, payload_json, created_at, error) "
            "VALUES ('run_family_union', 'failed', ?, 1700000002, 'boom') RETURNING id;", (json.dumps(payload),)).fetchone()["id"]
    r = _check3(run_id)
    assert r.outcome == judge_checks.NOT_EVALUABLE and r.reason == "family-union-failed: job %d: boom" % job


def test_u4_check3_with_a_covering_union_uses_its_k_and_v(stores):
    run_id = _theory_run()
    _two_theory_families()
    uid = _union_row([900, 901], 20, 0.0009)
    r = _check3(run_id)
    z = NormalDist()
    star = math.sqrt(0.0009) * ((1 - GAMMA) * z.inv_cdf(1 - 1 / 20) + GAMMA * z.inv_cdf(1 - math.exp(-1) / 20))
    assert r.quantities["k_effective"] == 20 and r.quantities["v"] == 0.0009 and r.quantities["n_trials"] == 80
    assert r.quantities["union_id"] == uid and "family_id" not in r.quantities
    assert r.quantities["sr_star"] == pytest.approx(star, rel=1e-12)
    assert r.outcome in (judge_checks.CLEARED, judge_checks.FAILED)
