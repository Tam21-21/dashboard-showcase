"""Lane P7-0: the Chains page's server half. The list route, the detail route's added keys, the
closed word map with a sentence for every reason word, the live event after each chain write, and
the loop's publish after a runner pass. Built on a fresh research.db in tmp_path, with chains
inserted by raw SQL (the way scripts/probes/chain_runner/_lib.py insert_chain_row writes them) and
broadcaster.publish replaced by a recorder."""
from __future__ import annotations

import asyncio
import ast
import re
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import chain_runner, chain_words, researchdb, reservedb, sse, stage_runner
from dashboard.backend.routes import chain as chain_routes

STRATEGY_SOURCE = (
    '"""A minimal equities strategy for the chains list tests."""\n'
    "class ProbeStrat:\n"
    "    asset_class_scope = ('equities',)\n"
    "\n"
    "    def lookback_bars(self):\n"
    "        return 5\n"
)
WINDOWS = dict(screen_start_ts=1_400_000_000, optimise_start_ts=1_420_070_400,
               walkforward_start_ts=1_451_606_400, walkforward_end_ts=1_483_228_800)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    researchdb.init_db()
    return researchdb


@pytest.fixture()
def events(monkeypatch):
    """Every broadcaster.publish call, as (event_type, payload)."""
    seen: list = []

    async def recorder(event_type, payload):
        seen.append((event_type, payload))

    monkeypatch.setattr(sse.broadcaster, "publish", recorder)
    return seen


@pytest.fixture()
def client(db, events):
    app = FastAPI()
    app.include_router(chain_routes.router)
    return TestClient(app)


@pytest.fixture()
def strategy(db):
    strategy_id, version, _action = researchdb.upsert_strategy("probe_strat", STRATEGY_SOURCE, display_name="Probe strategy")
    return strategy_id, version


def _chain(strategy, *, state="waiting", calendar="chain-a", now=1_700_000_000, **extra):
    strategy_id, version = strategy
    cols = dict(strategy_id=strategy_id, strategy_version=version, calendar_id=calendar,
                param_grid_json="{}", state=state, stop_requested=0, retry_requested=0,
                created_at=now, updated_at=now, **WINDOWS)
    cols.update(extra)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    with researchdb.transaction() as conn:
        row = conn.execute(f"INSERT INTO research_chains ({names}) VALUES ({marks}) RETURNING id;",
                           tuple(cols.values())).fetchone()
    return int(row["id"])


def _transition(chain_id, from_state, to_state, *, reason_word=None, detail=None, created_at=1_700_000_000):
    with researchdb.transaction() as conn:
        conn.execute(
            "INSERT INTO research_chain_transitions "
            "    (chain_id, from_state, to_state, reason_word, detail, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'runner', ?);",
            (chain_id, from_state, to_state, reason_word, detail, created_at))


def _three(strategy):
    a = _chain(strategy, state="optimising", calendar="chain-a")
    b = _chain(strategy, state="choosing-cell", calendar="chain-b")
    c = _chain(strategy, state="refused", calendar="chain-c", ended_at_step="screen",
               reason_word="warmup-unsatisfied", reason_detail="warmup-unsatisfied: not enough bars")
    return a, b, c


# ---------------------------------------------------------------------------------------- c1

def test_c1_list_newest_first_with_words(client, strategy):
    a, b, c = _three(strategy)
    resp = client.get("/api/research/chains")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reserve_cut_ts"] == reservedb.read_reserve_cut_date_ts()
    chains = body["chains"]
    assert [x["id"] for x in chains] == [c, b, a]
    assert [x["state_word"] for x in chains] == ["REFUSED", "CHOOSE A CELL", "OPTIMISING"]
    assert [x["group"] for x in chains] == ["retryable", "waiting-on-you", "active"]
    assert [x["step"] for x in chains] == ["screen", "choose", "optimise"]
    with researchdb.get_conn() as conn:
        expected_name = conn.execute("SELECT display_name FROM research_strategies WHERE id = ?;",
                                     (strategy[0],)).fetchone()["display_name"]
    assert expected_name == "Probe strategy" and all(x["display_name"] == expected_name for x in chains)
    assert chains[0]["reason_sentence"] == chain_words.REASON_SENTENCES["warmup-unsatisfied"]
    assert chains[1]["reason_sentence"] is None and chains[2]["reason_sentence"] is None
    assert all("param_grid_json" not in x for x in chains)
    assert chains[0]["note"] is None and chains[0]["note_word"] is None


def test_c1_done_chain_step_is_judge(client, strategy):
    _chain(strategy, state="done", chosen_variation_id=1, chosen_ticker="AAPL", chosen_at=1_700_000_000)
    chain = client.get("/api/research/chains").json()["chains"][0]
    assert chain["step"] == "judge" and chain["group"] == "ended" and chain["state_word"] == "DONE"


def test_c1_stopped_chain_step_is_ended_at_step(client, strategy):
    _chain(strategy, state="stopped", ended_at_step="optimise")
    chain = client.get("/api/research/chains").json()["chains"][0]
    assert chain["step"] == "optimise" and chain["group"] == "ended"


def test_c1_missing_strategy_gives_null_name(client, db):
    _chain((9999, 1))
    chain = client.get("/api/research/chains").json()["chains"][0]
    assert chain["display_name"] is None


def test_c1_limit_bounds(client, strategy):
    _three(strategy)
    assert client.get("/api/research/chains?limit=0").status_code == 422
    assert client.get("/api/research/chains?limit=501").status_code == 422
    assert len(client.get("/api/research/chains?limit=2").json()["chains"]) == 2


def test_c1_no_chains(client, db):
    body = client.get("/api/research/chains").json()
    assert body == {"chains": [], "reserve_cut_ts": reservedb.read_reserve_cut_date_ts()}


def test_c1_reserve_cut_owed_is_503(client, db):
    owed = reservedb.ReserveParameterOwed("reserve.cut_date")
    with mock.patch.object(reservedb, "read_reserve_cut_date_ts", side_effect=owed):
        resp = client.get("/api/research/chains")
    assert resp.status_code == 503
    assert resp.json()["detail"] == f"reserve-cut-owed: {owed}"


# ---------------------------------------------------------------------------------------- c2

def test_c2_notes(client, strategy):
    stopping = _chain(strategy, state="optimising", calendar="chain-a", stop_requested=1)
    ended = _chain(strategy, state="stopped", calendar="chain-b", ended_at_step="optimise", stop_requested=1)
    offline = _chain(strategy, state="screening", calendar="chain-c")
    sweeping = _chain(strategy, state="walking-forward", calendar="chain-d", chosen_variation_id=1,
                      chosen_ticker="AAPL", chosen_at=1_700_000_000)
    stalled = [{"id": offline, "stage": "screen", "reason": "no-worker-online"},
               {"id": sweeping, "stage": "walkforward", "reason": "sweep-materializing"}]
    with mock.patch.object(chain_runner, "chain_runner_stalled_chains", return_value=stalled):
        by_id = {x["id"]: x for x in client.get("/api/research/chains").json()["chains"]}
    assert by_id[stopping]["note"] == "stop-requested" and by_id[stopping]["note_word"] == "STOPPING"
    assert by_id[ended]["note"] is None
    assert by_id[offline]["note"] == "no-worker-online" and by_id[offline]["note_word"] == "WORKER OFFLINE"
    assert by_id[sweeping]["note"] is None and by_id[sweeping]["note_word"] is None


# ---------------------------------------------------------------------------------------- c3

def test_c3_detail_keeps_keys_and_gains_transitions(client, strategy):
    cid = _chain(strategy, state="refused", ended_at_step="screen", reason_word="warmup-unsatisfied",
                 reason_detail="warmup-unsatisfied: not enough bars")
    _transition(cid, None, "waiting", created_at=1_700_000_001)
    _transition(cid, "waiting", "screening", created_at=1_700_000_002)
    _transition(cid, "screening", "refused", reason_word="warmup-unsatisfied",
                detail="warmup-unsatisfied: not enough bars", created_at=1_700_000_003)
    body = client.get(f"/api/research/chains/{cid}").json()
    assert "optimise_run_id" in body and "walkforward_run_id" in body
    assert body["param_grid_json"] == "{}" and body["calendar_id"] == "chain-a"
    assert body["state_word"] == "REFUSED" and body["group"] == "retryable" and body["step"] == "screen"
    assert body["reason_sentence"] == chain_words.REASON_SENTENCES["warmup-unsatisfied"]
    transitions = body["transitions"]
    assert [t["to_state"] for t in transitions] == ["waiting", "screening", "refused"]
    assert [t["to_word"] for t in transitions] == ["WAITING TO START", "SCREENING", "REFUSED"]
    assert transitions[0]["reason_sentence"] is None
    assert transitions[2]["reason_sentence"] == chain_words.REASON_SENTENCES["warmup-unsatisfied"]
    assert client.get("/api/research/chains/424242").status_code == 404


# ---------------------------------------------------------------------------------------- c4

def _chain_runner_reason_literals() -> set:
    source = Path(chain_runner.__file__).read_text(encoding="utf-8")
    words = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name == "_end_chain" and len(node.args) > 3 and isinstance(node.args[3], ast.Constant) \
                    and isinstance(node.args[3].value, str):
                words.add(node.args[3].value)
            for kw in node.keywords:
                if kw.arg == "reason_word" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    words.add(kw.value.value)
    return words


def test_c4_every_reason_word_has_a_plain_sentence():
    words = set(chain_runner._R1_EXACT_TYPE_TO_WORD.values())
    words |= {"sweep-request-invalid", "chronology-refused", "run-link-failed"}
    for _wait, fail, refuse in chain_runner._P1_WORDS.values():
        words |= set(fail) | set(refuse)
    words |= {"stage-draw-orphaned", "stage-draw-duplicated", "stage-link-refused", "stage-run-failed",
              "stage-run-cancelled", "stage-run-deleted", "duel-inputs-differ", "route-answer-unworded",
              "step-crashed"}
    literals = _chain_runner_reason_literals()
    assert {"duel-inputs-differ", "universe-name-taken", "step-crashed"} <= literals
    words |= literals
    missing = sorted(w for w in words if w not in chain_words.REASON_SENTENCES)
    assert missing == []
    for word in words:
        sentence = chain_words.REASON_SENTENCES[word]
        assert sentence.strip() and sentence.endswith(".")
        assert not re.search(r"[a-z]+[-_][a-z]+", sentence), (word, sentence)
        assert "—" not in sentence, word


def test_c4_unknown_word_gets_fallback_and_is_counted():
    chain_words.UNKNOWN_REASON_WORDS.clear()
    sentence = chain_words.reason_sentence("not-a-real-word")
    assert sentence == "This chain ended for a reason the page has no sentence for yet."
    assert chain_words.UNKNOWN_REASON_WORDS["not-a-real-word"] == 1
    chain_words.reason_sentence("not-a-real-word")
    assert chain_words.UNKNOWN_REASON_WORDS["not-a-real-word"] == 2
    assert chain_words.reason_sentence("warmup-unsatisfied") == chain_words.REASON_SENTENCES["warmup-unsatisfied"]
    assert "warmup-unsatisfied" not in chain_words.UNKNOWN_REASON_WORDS
    chain_words.UNKNOWN_REASON_WORDS.clear()


def test_c4_state_maps_cover_the_eleven_states():
    assert len(chain_words.STATE_WORDS) == 11
    assert set(chain_words.STATE_GROUPS) == set(chain_words.STATE_WORDS)
    assert len(chain_words.NOTE_WORDS) == 3


# ---------------------------------------------------------------------------------------- c5

def test_c5_stop_publishes_once_and_refusal_publishes_nothing(client, strategy, events):
    cid = _chain(strategy, state="optimising")
    resp = client.post(f"/api/research/chains/{cid}/stop")
    assert resp.status_code == 202
    assert events == [("research.chain_changed", {"chain_ids": [cid]})]
    done = _chain(strategy, state="stopped", calendar="chain-z", ended_at_step="optimise")
    events.clear()
    assert client.post(f"/api/research/chains/{done}/stop").status_code == 409
    assert client.post("/api/research/chains/424242/stop").status_code == 404
    assert events == []


def test_c5_pick_publishes(client, strategy, events, monkeypatch):
    cid = _chain(strategy, state="choosing-cell")
    monkeypatch.setattr(chain_runner, "_find_stage_run", lambda *a, **k: 1)
    monkeypatch.setattr(researchdb, "get_run_cell", lambda *a, **k: {"ticker": "AAPL"})
    resp = client.post(f"/api/research/chains/{cid}/cell", json={"variation_id": 3, "ticker": "AAPL"})
    assert resp.status_code == 202, resp.text
    assert events == [("research.chain_changed", {"chain_ids": [cid]})]
    events.clear()
    again = client.post(f"/api/research/chains/{cid}/cell", json={"variation_id": 3, "ticker": "AAPL"})
    assert again.status_code == 409
    assert events == []


def test_c5_retry_publishes(client, strategy, events):
    cid = _chain(strategy, state="refused", ended_at_step="replay", reason_word="replay-failed",
                 reason_detail="replay-failed: x", chosen_variation_id=3, chosen_ticker="AAPL",
                 chosen_at=1_700_000_000)
    resp = client.post(f"/api/research/chains/{cid}/retry")
    assert resp.status_code == 202, resp.text
    assert events == [("research.chain_changed", {"chain_ids": [cid]})]
    events.clear()
    assert client.post(f"/api/research/chains/{cid}/retry").status_code == 409
    assert events == []


def test_c5_create_with_duel_publishes_both_ids(client, strategy, events, monkeypatch):
    partner_id, _v, _a = researchdb.upsert_strategy("probe_partner", STRATEGY_SOURCE)
    monkeypatch.setattr(stage_runner, "daily_store_path", lambda: Path("/tmp/unused-daily-store"))
    body = dict(strategy_id=strategy[0], param_grid={"entry_quantile": [0.5]},
                duel_with_strategy_id=partner_id, **WINDOWS)
    resp = client.post("/api/research/chains", json=body)
    assert resp.status_code == 201, resp.text
    made = resp.json()
    assert events == [("research.chain_changed", {"chain_ids": [made["chain_id"], made["duel_chain_id"]]})]
    events.clear()
    single = client.post("/api/research/chains", json=dict(strategy_id=strategy[0],
                                                            param_grid={"entry_quantile": [0.5]}, **WINDOWS))
    assert single.status_code == 201, single.text
    assert events == [("research.chain_changed", {"chain_ids": [single.json()["chain_id"]]})]


def test_c5_refused_create_publishes_nothing(client, strategy, events):
    body = dict(strategy_id=strategy[0], param_grid={"entry_quantile": 0.5}, **WINDOWS)
    assert client.post("/api/research/chains", json=body).status_code == 422
    assert events == []


# ---------------------------------------------------------------------------------------- c6

def test_c6_chains_changed_since(db, strategy):
    old = _chain(strategy, calendar="chain-a", now=1_700_000_000)
    edge = _chain(strategy, calendar="chain-b", now=1_700_000_100)
    new = _chain(strategy, calendar="chain-c", now=1_700_000_200)
    assert chain_runner.chains_changed_since(1_700_000_100) == [edge, new]
    assert chain_runner.chains_changed_since(1_700_000_201) == []
    assert old not in chain_runner.chains_changed_since(1_700_000_001)


def _run_loop_one_pass(events):
    from dashboard.backend import main as dashboard_main

    class _Stop(Exception):
        pass

    sleeps: list = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop()

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with mock.patch.object(chain_runner, "run_pass_once", return_value={"chains_processed": 0}), \
         mock.patch("asyncio.sleep", side_effect=fake_sleep), \
         mock.patch("asyncio.to_thread", side_effect=fake_to_thread):
        try:
            asyncio.run(dashboard_main._chain_runner_loop())
        except _Stop:
            pass


def test_c6_loop_publishes_changed_chains_once_after_a_pass(db, strategy, events):
    now = int(time.time())
    _chain(strategy, calendar="chain-old", now=1_700_000_000)
    fresh = _chain(strategy, calendar="chain-new", now=now + 3600)
    _run_loop_one_pass(events)
    assert events == [("research.chain_changed", {"chain_ids": [fresh]})]


def test_c6_loop_publishes_nothing_when_no_chain_moved(db, strategy, events):
    _chain(strategy, calendar="chain-old", now=1_700_000_000)
    _run_loop_one_pass(events)
    assert events == []
