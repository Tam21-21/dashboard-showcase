"""Lane EA4 (docs/lanes/EA4.md, DECISIONS #480 item 8): a theory that reads an earnings field gets its members' CIKs in the job payload.

The world is test_theory_jobs.py's (two synthetic tickers, three universes) plus a throwaway events.db whose
research_cik_ticker_map maps the two tickers to CIKs. No worker, no container.
"""
from __future__ import annotations

import json

import pytest

from dashboard.backend import eventsdb, researchdb, stage_runner
from dashboard.backend.tests.test_theory_jobs import (  # noqa: F401  (world is a fixture, used by name)
    COSTS, GROUP, INIT_CASH, NOW, SEARCH, THEORY, VARIATIONS, WINDOWS, _job_payload, _rows, world)

THEORY_E = THEORY + '\n[operand.s]\nkind = "field"\nfield = "eps_surprise_pct"\n'
THEORY_CLOSE = THEORY + '\n[operand.s]\nkind = "field"\nfield = "close"\n'
CIKS = {"SYN_VOL_01": 101, "SYN_VOL_02": 102}


@pytest.fixture()
def ev(world, tmp_path, monkeypatch):
    path = tmp_path / "events.db"
    monkeypatch.setattr(eventsdb, "DB_PATH", path)
    eventsdb.init_events_db(path)
    return path


def _map(path, tickers):
    with eventsdb.events_transaction(path) as conn:
        for ticker in tickers:
            conn.execute(
                "INSERT INTO research_cik_ticker_map (cik, ticker, effective_from, effective_to, source, name, loaded_at) "
                "VALUES (?, ?, NULL, NULL, 'manual', ?, ?);", (CIKS[ticker], ticker, ticker, NOW))


def _search(world, theory, calendar_id="dry1", now=NOW):
    return stage_runner.dispatch_theory_stage(
        "screen", world["universes"]["screen"], now, calendar_id=calendar_id, theory_text=theory, group_label=GROUP,
        data_start="2003-01-02", search=SEARCH, costs=COSTS, init_cash=INIT_CASH)


def _counts():
    return {t: len(_rows("SELECT * FROM %s;" % t)) for t in
            ("research_data_draws", "research_runs", "research_jobs", "research_strategies")}


def test_e1_search_writes_the_earnings_block(world, ev):
    _map(ev, CIKS)
    run = _search(world, THEORY_E)
    _, payload, _ = _job_payload(run["run_id"])
    assert payload["earnings"] == {"ciks": CIKS, "knowledge_from": 0, "knowledge_to": NOW}


def test_e2_stages_carry_the_same_block(world, ev):
    _map(ev, CIKS)
    run = _search(world, THEORY_E)
    for stage, offset in (("optimise", 100), ("walkforward", 101)):
        out = stage_runner.dispatch_theory_stage(
            stage, world["universes"][stage], NOW + offset, search_run_id=run["run_id"], variations=VARIATIONS)
        _, payload, _ = _job_payload(out["run_id"])
        assert payload["earnings"] == {"ciks": CIKS, "knowledge_from": 0, "knowledge_to": NOW}


def test_e2b_stage_of_an_earnings_theory_without_a_block_is_refused(world, ev):
    run = _search(world, THEORY)  # the search itself reads no earnings, so it has no block
    with researchdb.transaction() as conn:
        row = conn.execute("SELECT j.id AS id, j.payload_json AS p FROM research_runs r JOIN research_jobs j ON j.id = r.job_id "
                           "WHERE r.id = ?;", (run["run_id"],)).fetchone()
        payload = json.loads(row["p"])
        payload["theory_toml"] = THEORY_E
        conn.execute("UPDATE research_jobs SET payload_json = ? WHERE id = ?;", (json.dumps(payload), row["id"]))
    _map(ev, CIKS)
    before = _counts()
    with pytest.raises(researchdb.TheoryRunInvalid, match=str(run["run_id"])):
        stage_runner.dispatch_theory_stage("optimise", world["universes"]["optimise"], NOW + 100,
                                           search_run_id=run["run_id"], variations=VARIATIONS)
    assert _counts() == before


def test_e3_unmapped_member_refuses_the_search_and_writes_nothing(world, ev):
    _map(ev, ["SYN_VOL_01"])
    before = _counts()
    with pytest.raises(researchdb.TheoryRunInvalid, match="SYN_VOL_02"):
        _search(world, THEORY_E)
    assert _counts() == before


def test_e4_guard_theory_without_earnings_field_writes_no_key(world, ev):
    run = _search(world, THEORY)
    _, payload, _ = _job_payload(run["run_id"])
    assert "earnings" not in payload


def test_e5_guard_close_field_writes_no_key(world, ev):
    run = _search(world, THEORY_CLOSE)
    _, payload, _ = _job_payload(run["run_id"])
    assert "earnings" not in payload
