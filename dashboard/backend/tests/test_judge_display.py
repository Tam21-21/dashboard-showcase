"""Lane UI8b-0: the verdict routes give each check's figure, its bar and the judged capital.

The judge store is a real file made by judgedb.init_judge_db (it seeds battery 2026-10-10.a), and the
research side is a real file made by researchdb.init_db with DB_PATH patched, as scripts/probes/draw_void/_lib.py does.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("fastapi")

from dashboard.backend import judgedb, researchdb  # noqa: E402
from dashboard.backend import judge_display  # noqa: E402

BV = judgedb.BATTERY_VERSION


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "research.db"
    judgedb.init_judge_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _outcomes():
    return [
        {"check": 1, "outcome": "cleared", "quantities": {"rdf_percent": 95.0, "kappa": 0.1, "trade_count": 40}},
        {"check": 2, "outcome": "cleared", "quantities": {"wfe_percent": 62.5}},
        {"check": 3, "outcome": "failed", "quantities": {"dsr": 0.4}},
        {"check": 4, "outcome": "cleared",
         "quantities": {"peak_sharpe": 2.0, "neighbourhood_mean": 1.5, "neighbourhood_std": 0.25}},
        {"check": 5, "outcome": "cleared", "quantities": {"pbo": 0.25}},
        {"check": 6, "outcome": "cleared", "quantities": {"bound": 0.002}},
        {"check": 7, "outcome": "failed", "quantities": {"p_value": 0.2}},
        {"check": 8, "outcome": "cleared", "quantities": {"prom_minus_biggest_run": 1.5}},
        {"check": 9, "outcome": "cleared", "quantities": {"sharpe_margin": 0.3, "p_value": 0.01}},
    ]


def _by(result, check):
    return result[check - 1], {m["key"]: m for m in result[check - 1]["measures"]}


def test_m1_all_nine(conn):
    res = judge_display.check_measures(_outcomes(), conn, BV)
    assert [r["check"] for r in res] == list(range(1, 10))
    _, m = _by(res, 1)
    assert m["rdf_percent"]["value"] == 95.0 and m["rdf_percent"]["bar"] == 90.0
    assert m["rdf_percent"]["comparison"] == "at_or_above" and m["rdf_percent"]["units"] == "percent"
    assert m["kappa"]["value"] == 0.1 and m["kappa"]["bar"] == 0.15
    assert m["kappa"]["comparison"] == "at_or_below" and m["kappa"]["units"] == "ratio"
    _, m = _by(res, 2)
    assert m["wfe_percent"]["value"] == 62.5 and m["wfe_percent"]["bar"] == 50.0
    assert m["wfe_percent"]["comparison"] == "at_or_above"
    _, m = _by(res, 3)
    assert m["dsr"]["value"] == 0.4 and m["dsr"]["bar"] == 0.95
    _, m = _by(res, 4)
    assert m["flatness"]["value"] == pytest.approx(2.0) and m["flatness"]["bar"] == 1.0
    _, m = _by(res, 5)
    assert m["pbo"]["value"] == 0.25 and m["pbo"]["bar"] == 0.5
    _, m = _by(res, 6)
    assert m["bound"]["value"] == 0.002 and m["bound"]["bar"] == 0.0
    _, m = _by(res, 7)
    assert m["p_value"]["value"] == 0.2 and m["p_value"]["bar"] == 0.05
    assert m["p_value"]["comparison"] == "strictly_below"
    _, m = _by(res, 8)
    assert m["prom_minus_biggest_run"]["value"] == 1.5 and m["prom_minus_biggest_run"]["bar"] == 0.0
    _, m = _by(res, 9)
    assert m["sharpe_margin"]["value"] == 0.3 and m["sharpe_margin"]["bar"] == 0.0
    assert m["p_value"]["value"] == 0.01 and m["p_value"]["bar"] == 0.05
    assert res[2]["outcome"] == "failed"


def test_m2_zero_std_gives_null(conn):
    outs = _outcomes()
    outs[3]["quantities"]["neighbourhood_std"] = 0.0
    _, m = _by(judge_display.check_measures(outs, conn, BV), 4)
    assert m["flatness"]["value"] is None and m["flatness"]["bar"] == 1.0


def test_m3_empty_quantities(conn):
    outs = _outcomes()
    outs[1] = {"check": 2, "outcome": "not-evaluable", "quantities": {}}
    outs[2] = {"check": 3, "outcome": "not-evaluable"}
    res = judge_display.check_measures(outs, conn, BV)
    _, m = _by(res, 2)
    assert m["wfe_percent"]["value"] is None and m["wfe_percent"]["bar"] == 50.0
    _, m = _by(res, 3)
    assert m["dsr"]["value"] is None and m["dsr"]["bar"] == 0.95


def test_m4_no_frozen_row_and_none_version(conn):
    res = judge_display.check_measures(_outcomes(), conn, "no-such-battery")
    _, m = _by(res, 2)
    assert m["wfe_percent"]["bar"] is None and m["wfe_percent"]["value"] == 62.5
    res = judge_display.check_measures(_outcomes(), conn, None)
    for check in range(2, 10):
        assert all(x["bar"] is None for x in res[check - 1]["measures"])
    _, m = _by(res, 1)
    assert m["rdf_percent"]["bar"] == 90.0 and m["kappa"]["bar"] == 0.15


def test_m6_missing_checks(conn):
    outs = _outcomes()[:6]
    del outs[0]["quantities"]
    res = judge_display.check_measures(outs, conn, BV)
    assert len(res) == 9
    assert all(res[i]["outcome"] is None for i in (6, 7, 8))
    assert all(x["value"] is None for i in (6, 7, 8) for x in res[i]["measures"])
    _, m = _by(res, 1)
    assert m["rdf_percent"]["value"] is None


def test_m5_check9_random_arm_first(conn):
    outs = _outcomes()
    outs[8] = {"check": 9, "outcome": "failed", "quantities": {"p_value": 0.3, "k": 1500, "m": 4999}}
    _, m = _by(judge_display.check_measures(outs, conn, BV), 9)
    assert m["sharpe_margin"]["value"] is None
    assert m["p_value"]["value"] == 0.3


# --- lane UI35-0: a plain "why" sentence for each missing figure ------------------------------------

GENERIC = "This check could not be graded yet: an input it needs is missing."
STORED_WITHOUT = "This verdict was stored without this check's figures."
B_SENTENCE = "The pass mark for this check is not set for this version of the checks."
G_SENTENCE = "The run this verdict grades is no longer in the research store."


def _ne(check, reason, quantities=None):
    return {"check": check, "outcome": "not-evaluable", "quantities": quantities if quantities is not None else {},
            "reason": reason}


def _why_of(conn, check, entry):
    outs = [e for e in _outcomes() if e["check"] != check] + [entry]
    outs.sort(key=lambda e: e["check"])
    res = judge_display.check_measures(outs, conn, BV)
    return res[check - 1]["why"]


# w1: each not-evaluable reason stored in production gives its table sentence.
_W1_CASES = [
    (2, "in_sample_profit and out_of_sample_profit are the missing inputs: this run's own research_run_stats row "
        "has not recorded a walk-forward write yet",
     "The test on data it was not tuned on has not run yet."),
    (3, "trial family is the missing input: no search recorded a family on this member set and screen window, "
        "so the trial-multiplicity correction is not computable",
     "No search recorded the group of tries this strategy belongs to."),
    (3, "k_effective is 1, below 2: the trial-multiplicity correction needs at least 2 independent trials",
     "Fewer than 2 really different tries were found; the luck test needs 2."),
    (3, "too-few-trials: the search tried too few strategies to cluster, so K and V are not known",
     "Too few strategies were tried to count the real tries."),
    (3, "trial-count-scope-owed", "How to count tries for this kind of run is not decided yet."),
    (4, "fewer neighbouring cells than plateau_bandwidth needs on at least one side of the peak",
     "Too few nearby settings were tested on one side of the chosen one."),
    (6, "the candidate's bar-level return series is the missing input: equity.parquet is absent or empty",
     "The run's bar by bar returns were not stored."),
    (7, "the re-run used 4999 fake markets but the frozen replication count is 5000 (#376 decision 2, D7): "
        "a different count is not graded",
     "The shuffled-prices test ran the wrong number of fake markets."),
    (9, "the buy-and-hold arm's own computation failed inside the container: boom",
     "The buy and hold comparison failed while it ran."),
    (1, "degrees-of-freedom-remaining is the missing input: Pardo's Rdf (pages 130-131) needs the strategy's used "
        "degrees of freedom and the test window's bar count, neither reachable from a trade record alone",
     "The number of settings the strategy tunes is not recorded, so the freedom left cannot be counted."),
    (5, "cscv_split_count is the missing input: CSCV's own split count has no book-sourced candidate value "
        "(judge_spec_brief.md section 7), and the per-period performance matrix CSCV needs is not a shape this "
        "schema carries today (researchdb.py, where it would need a new column and write path, is outside this "
        "lane's own area fence)",
     "This version of the checks could not run this test yet."),
    (2, "wfe_pass_bar is the missing input: not frozen for this battery_version", B_SENTENCE),
    (1, "run 7 not found", G_SENTENCE),
]


@pytest.mark.parametrize("check,reason,sentence", _W1_CASES)
def test_w1_table_sentence(conn, check, reason, sentence):
    assert _why_of(conn, check, _ne(check, reason)) == sentence


def test_w2_unknown_reason_gives_generic(conn):
    assert _why_of(conn, 4, _ne(4, "something_new_is_the_missing_input: nobody wrote this one")) == GENERIC
    assert _why_of(conn, 4, _ne(4, "brand-new-code: with some text")) == GENERIC
    # the same text under another check is a different key
    assert _why_of(conn, 8, _ne(8, "fewer neighbouring cells than plateau_bandwidth needs on at least one side of the peak")) == GENERIC


def test_w3_no_reason_no_quantities(conn):
    for outcome in ("failed", "not-evaluable"):
        for entry in ({"check": 5, "outcome": outcome},
                      {"check": 5, "outcome": outcome, "quantities": {}},
                      {"check": 5, "outcome": outcome, "quantities": None, "reason": None},
                      {"check": 5, "outcome": outcome, "quantities": {}, "reason": ""}):
            assert _why_of(conn, 5, entry) == STORED_WITHOUT


def test_w4_cleared_and_failed_give_none(conn):
    for outcome in ("cleared", "failed"):
        with_value = {"check": 3, "outcome": outcome, "quantities": {"dsr": 0.4}, "reason": "dsr 0.4 is below the bar"}
        with_none = {"check": 3, "outcome": outcome, "quantities": {"dsr": None, "n": 3}, "reason": "dsr 0.4 is below the bar"}
        for entry in (with_value, with_none):
            outs = [e for e in _outcomes() if e["check"] != 3] + [entry]
            outs.sort(key=lambda e: e["check"])
            res = judge_display.check_measures(outs, conn, BV)
            assert "why" in res[2] and res[2]["why"] is None
    # every entry carries the key
    assert all("why" in r for r in judge_display.check_measures(_outcomes(), conn, BV))


def test_w6_absent_check_gives_none(conn):
    res = judge_display.check_measures(_outcomes()[:6], conn, BV)
    assert all("why" in res[i] and res[i]["why"] is None for i in (6, 7, 8))
    res = judge_display.check_measures(None, conn, BV)
    assert all("why" in r and r["why"] is None for r in res)


def _literal_text(node):
    """The reason as text: a literal as is, an f-string or a sum with every non-literal part written {}."""
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    if isinstance(node, ast.Name):
        from dashboard.backend import judge_checks
        value = getattr(judge_checks, node.id, None)
        if isinstance(value, str):
            return value
        return "{}"
    if isinstance(node, (ast.IfExp, ast.Call, ast.Subscript, ast.Attribute)):
        return "{}"
    raise ValueError(f"cannot read a reason of kind {type(node).__name__}")


def _key_for(text, node):
    import ast
    import re

    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.FormattedValue):
        # check 3's f"{family['reason']}: the search tried ...": the one value researchdb.py allows in that column
        return "too-few-trials"
    match = re.match(r"^([a-z]+(?:-[a-z]+)+)(?::|$)", text)
    return match.group(1) if match else text


def test_w5_every_not_evaluable_site_has_a_sentence():
    import ast
    from pathlib import Path

    from dashboard.backend import judge_checks

    tree = ast.parse(Path(judge_checks.__file__).read_text())
    sites, problems = [], []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "CheckResult"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        args = node.args
        check = kw.get("check_id", args[0] if args else None)
        outcome = kw.get("outcome", args[1] if len(args) > 1 else None)
        if not (isinstance(outcome, ast.Name) and outcome.id == "NOT_EVALUABLE"):
            continue
        reason = kw.get("reason", args[3] if len(args) > 3 else None)
        if reason is None or not isinstance(check, ast.Constant):
            problems.append(f"line {node.lineno}: cannot read the check or the reason")
            continue
        try:
            text = _literal_text(reason)
        except ValueError as exc:
            problems.append(f"check {check.value} line {node.lineno}: {exc}")
            continue
        sites.append((check.value, node.lineno, text, _key_for(text, reason)))
    assert len(sites) >= 63
    for check, line, text, key in sites:
        if (check, key) not in judge_display._WHY:
            problems.append(f"check {check} line {line}: no _WHY entry for key {key!r}")
            continue
        # the reason as the check would store it: placeholders filled in with some text
        stored = text.replace("{}", "X")
        if key == "too-few-trials":
            stored = "too-few-trials: the search tried too few strategies to cluster, so K and V are not known"
        got = judge_display._why({"check": check, "outcome": "not-evaluable", "quantities": {}, "reason": stored})
        if got != judge_display._WHY[(check, key)]:
            problems.append(f"check {check} line {line}: lookup of {stored!r} gave {got!r}")
    assert not problems, "\n".join(problems)
    for (check, key), sentence in judge_display._WHY.items():
        assert "_" not in sentence, f"check {check} key {key!r}: sentence holds an underscore"
        assert sentence.endswith(".") and sentence[0].isupper()
    for sentence in (GENERIC, STORED_WITHOUT):
        assert "_" not in sentence


# --- routes ---------------------------------------------------------------------------------

@pytest.fixture()
def routes(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dashboard.backend.routes import judge as judge_routes

    path = tmp_path / "research.db"
    monkeypatch.setattr(researchdb, "DB_PATH", path)
    monkeypatch.setattr(judgedb, "DB_PATH", path)
    researchdb.init_db()
    judgedb.init_judge_db(path)
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA foreign_keys = OFF;")
    c.execute("INSERT INTO research_strategies (id, name, source_code, version, last_mirror_status, created_at, updated_at)"
              " VALUES (1, 'ma_cross', 'x', 1, 'pending', 1700000000, 1700000000)")
    for run_id, payload in ((1, {"init_cash": 25000.0}), (2, {})):
        c.execute("INSERT INTO research_jobs (id, kind, payload_json, status, created_at) VALUES (?, 'run_sweep', ?, 'succeeded', 1700000000)",
                  (run_id, json.dumps(payload)))
        c.execute("INSERT INTO research_runs (id, job_id, strategy_id, strategy_version, dataset_id, dataset_sha256,"
                  " settings_snapshot, created_at, status) VALUES (?, ?, 1, 1, 1, ?, '[]', 1700000000, 'succeeded')",
                  (run_id, run_id, "b" * 64))
    c.commit()
    c.close()
    for run_id in (1, 2):
        with judgedb.get_judge_conn(path) as jc:
            judgedb.record_verdict(
                jc, run_id=run_id, judging_pass_id=f"1111111{run_id}-1111-4111-8111-111111111111", variation_id=0,
                ticker="AAPL", graded_formula_version="1|3|docker:x@sha256:" + "a" * 64, strategy_id=1,
                strategy_version=1, dataset_sha256="b" * 64, verdict="fail", is_advisory=0, battery_version=BV,
                evidence_floor_outcome="cleared", check_outcomes_json=json.dumps(_outcomes()),
                decided_by="initial", warnings_json="[]", graded_at=1700000000 + run_id)
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


def test_r1_list_and_detail(routes):
    rows = {r["run_id"]: r for r in routes.get("/api/judge/verdicts").json()["rows"]}
    assert len(rows[1]["measures"]) == 9 and rows[1]["judged_capital"] == 25000.0
    assert rows[2]["judged_capital"] == 10000.0
    one = routes.get(f"/api/judge/verdicts/{rows[1]['id']}").json()
    assert len(one["measures"]) == 9 and one["judged_capital"] == 25000.0
    assert routes.get("/api/judge/verdicts/9999").status_code == 404


def test_r2_other_keys_unchanged(routes):
    row = routes.get("/api/judge/verdicts").json()["rows"][0]
    expected = {"id", "run_id", "judging_pass_id", "variation_id", "ticker", "graded_formula_version",
                "strategy_id", "strategy_version", "dataset_sha256", "verdict", "is_advisory", "battery_version",
                "evidence_floor_outcome", "check_outcomes", "decided_by", "warnings", "graded_at", "current",
                "trials", "total_return", "max_drawdown", "num_trades", "sharpe_ratio", "strategy_name",
                "rejected", "rejected_at"}
    assert set(row) - {"measures", "judged_capital"} == expected
