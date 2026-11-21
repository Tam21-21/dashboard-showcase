"""Lane UI17b-0: secrets in a log line read <redacted> once main.py is imported."""
from __future__ import annotations

import logging

import pytest

import dashboard.backend.main  # noqa: F401  (installs the filter on the root handlers)


def _format(msg):
    rec = logging.LogRecord("httpx", logging.INFO, "x.py", 1, msg, None, None)
    handlers = logging.getLogger().handlers
    assert handlers, "main.py leaves the root logger with no handler"
    handler = handlers[0]
    assert handler.filter(rec), "a root handler filter dropped the record"
    return handler.format(rec)


def test_token_redacted():
    out = _format("HTTP Request: GET https://eodhd.com/api/user?api_token=SYNTH123&fmt=json")
    assert "api_token=<redacted>&fmt=json" in out and "SYNTH123" not in out


@pytest.mark.parametrize("name", ["api_token", "token", "apikey", "api_key", "key", "secret", "password"])
def test_each_name(name):
    out = _format(f"call {name}=ABC999 more")
    assert f"{name}=<redacted>" in out and "ABC999" not in out


@pytest.mark.parametrize("msg", ["tokens=5 ok", "no equals here"])
def test_guards(msg):
    assert _format(msg).endswith(msg)
