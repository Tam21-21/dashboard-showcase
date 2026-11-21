"""DBG-21: refusal details the page shows are plain sentences after the reason word.

The plain rule: no lower-case word joined by underscores, no Python class name, no "Traceback",
and no spec tag (R-T and a digit, AD and a digit, AC- and a digit).
"""
from __future__ import annotations

import contextlib
import logging
import re

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dashboard.backend import judgedb, reserve_exam, stage_runner, tracker
from dashboard.backend.routes import judge, research, reserve
from research_lab import universe as universe_rule

_CODE_WORD = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
_CLASS_NAME = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}\b|Traceback")
_SPEC_TAG = re.compile(r"R-T\d|AD\d|AC-\d")


def _is_plain(text: str) -> bool:
    return not (_CODE_WORD.search(text) or _CLASS_NAME.search(text) or _SPEC_TAG.search(text))


def _chrono():
    return tracker.ChronologyRefused("R-T2/AD7.1 window_start_ts 5 is not after high-water mark 9")


def _missing():
    return tracker.DrawWindowMissing("universe/dataset 1 has a NULL window bound (R-T15)")


def _linkfail():
    return tracker.RunLinkFailed(7, 12, "write-lock contention")


def _unavail():
    return universe_rule.ContaminationListUnavailable("/x/condemned.csv", 42)


def _empty():
    return universe_rule.ContaminationListEmpty("/x/condemned.csv", 42)


def _inert():
    return universe_rule.ContaminationListInert("/x/condemned.csv", 42, 700)


def _owed():
    return universe_rule.UniverseParameterOwed("search.universe_min_names_per_window")


def _holdout():
    return universe_rule.HoldoutNotDrawnYet("real")


# (builder, today's reason word)
TRACKER_CASES = [
    (_chrono, "chronology-refused"),
    (_missing, "draw-window-missing"),
    (_linkfail, "run-link-failed"),
]
UNIVERSE_CASES = [
    (_unavail, "contamination-list-unavailable"),
    (_empty, "contamination-list-empty"),
    (_inert, "contamination-list-inert"),
    (_owed, "universe-parameter-owed"),
    (_holdout, "holdout-not-drawn-yet"),
]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(reserve.router)
    return TestClient(app)


def _post(client, monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(reserve_exam, "dispatch_exam", boom)
    return client.post(
        "/api/research/reserve-exam/dispatch", json={"strategy_id": 1, "seed": 1}
    )


@pytest.mark.parametrize("build,word", TRACKER_CASES + UNIVERSE_CASES)
def test_reserve_route_gives_plain_sentence(client, monkeypatch, caplog, build, word):
    exc = build()
    with caplog.at_level(logging.WARNING):
        resp = _post(client, monkeypatch, exc)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail.startswith(word + ": ")
    sentence = detail[len(word) + 2:]
    assert _is_plain(sentence), sentence
    assert any(str(exc) in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("build,word", TRACKER_CASES)
def test_handle_tracker_refusal_gives_plain_sentence(caplog, build, word):
    exc = build()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as info:
            research._handle_tracker_refusal(exc)
    assert info.value.status_code == 409
    detail = info.value.detail
    assert detail.startswith(word + ": ")
    assert _is_plain(detail[len(word) + 2:]), detail
    assert any(str(exc) in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_run_link_sentence_names_run_and_draw():
    from dashboard.backend import plain_refusals

    s = plain_refusals.sentence(_linkfail())
    assert "7" in s and "12" in s


def test_sentence_refuses_other_classes():
    from dashboard.backend import plain_refusals

    with pytest.raises(TypeError, match="KeyError"):
        plain_refusals.sentence(KeyError("x"))


def test_too_few_names_keeps_text(client, monkeypatch):
    exc = universe_rule.TooFewQualifyingNames(3, 20)
    resp = _post(client, monkeypatch, exc)
    assert resp.status_code == 409
    assert resp.json()["detail"] == f"too-few-qualifying-names: {exc}"


@pytest.mark.parametrize(
    "exc,word",
    [
        (stage_runner.LeadInUnavailable("AAA", 3), "lead-in-unavailable"),
        (stage_runner.WarmupUnsatisfied("AAA", 3, 9), "warmup-unsatisfied"),
        (stage_runner.CandidateVersionMoved(1, 2, 3), "candidate-version-moved"),
        (stage_runner.DailyStoreNotConfigured("nostore"), "daily-store-not-configured"),
    ],
)
def test_plain_details_unchanged(client, monkeypatch, exc, word):
    resp = _post(client, monkeypatch, exc)
    assert resp.status_code == 409
    assert resp.json()["detail"] == f"{word}: {exc}"


def test_cscv_split_count_503_text(monkeypatch):
    @contextlib.contextmanager
    def fake_conn():
        yield object()

    monkeypatch.setattr(judge.researchdb, "get_conn", fake_conn)
    monkeypatch.setattr(judgedb, "read_threshold", lambda *a, **k: None)
    with pytest.raises(HTTPException) as info:
        judge.worker_cscv_split_count()
    assert info.value.status_code == 503
    assert info.value.detail == (
        "cscv-split-count-not-frozen: the judge's split count for check 5 is not set "
        "for the current battery"
    )
