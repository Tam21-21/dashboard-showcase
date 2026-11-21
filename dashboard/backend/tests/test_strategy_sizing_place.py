"""Lane ED1a: sizing_method is read by the server only from a class body (docs/lanes/ED1a.md).

Tests s1 to s7, s3b and s3c follow the lane file's Tests line. s5 to s7 are guards (green at base).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.backend import researchdb
from dashboard.backend.routes import research

HEAD = '"""A test strategy."""\n\n\nclass S:\n    asset_class_scope = ("equities",)\n'


def _src(body: str) -> str:
    return HEAD + body


S1 = _src('\n    def run(self):\n        sizing_method = "fixed_risk"\n        return 1\n')


def _refused(source: str) -> str:
    with pytest.raises(researchdb.StrategyValidationError) as exc:
        researchdb.validate_strategy_source(source)
    return str(exc.value)


def test_s1_local_in_run_refused():
    msg = _refused(S1)
    assert "line 8" in msg and "function run" in msg and "class body" in msg


def test_s2_self_attr_in_init_refused():
    msg = _refused(_src('\n    def __init__(self):\n        self.sizing_method = "fixed_risk"\n'))
    assert "function __init__" in msg


def test_s3_module_level_refused():
    msg = _refused(HEAD + '\nsizing_method = "x"\n')
    assert "the module" in msg


def test_s3b_block_in_class_refused():
    msg = _refused(_src('\n    if True:\n        sizing_method = "fixed_risk"\n'))
    assert "a block inside class S" in msg


def test_s3c_augmented_in_class_refused():
    msg = _refused(_src('    sizing_method = "fixed_risk"\n    sizing_method += "x"\n'))
    assert "an augmented assignment" in msg


def test_s4_route_answers_the_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")
    researchdb.init_db()
    app = FastAPI()
    app.include_router(research.browser_router)
    r = TestClient(app).post("/api/research/strategies/validate", json={"source_code": S1})
    assert r.status_code == 400
    assert "class body" in r.json()["detail"]


def test_s5_class_body_assign_passes():
    src = _src('    sizing_method = "fixed_risk"\n')
    researchdb.validate_strategy_source(src)
    assert researchdb.extract_sizing_method(src) == "fixed_risk"


def test_s6_class_body_annassign_passes():
    researchdb.validate_strategy_source(_src('    sizing_method: str = "fixed_risk"\n'))


def test_s7_no_sizing_method_passes():
    researchdb.validate_strategy_source(HEAD)
    from research_lab.strategies.base.strategy import Strategy
    assert researchdb.extract_sizing_method(HEAD) == Strategy.sizing_method
