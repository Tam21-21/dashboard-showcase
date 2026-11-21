"""Pytest unit suite for export.py pure helpers (A6).

_benchmark_pairs is pure (price + equity in → list out, no I/O); we can test it
directly. _settings_drift reads from researchdb; here it runs against a REAL
in-memory SQLite database built from researchdb's own _DDL, so the tests
exercise the genuine _live_settings read path (cursor protocol, sqlite3.Row
access, value_type parsing) instead of hand-rolled fakes. Hand-rolled fakes are
exactly what broke on 2026-06-17: the M4.5 3a rewrite pointed _settings_drift
at researchdb._live_settings, whose .fetchall()/value_type the old fakes never
carried, and CI went red for seven weeks. A real connection cannot drift from
the interface. The K.5 HTTP probe stays the end-to-end gate on the server.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

# export.py imports pandas at module scope (it reads parquets); skip the file on
# any machine where pandas is not installed (laptop iteration without the full
# server venv). The K.5/K.6 HTTP probes exercise the full export module against
# the real server stack — that is the end-to-end gate.
pytest.importorskip("pandas")

from dashboard.backend import researchdb  # noqa: E402
from dashboard.backend.research import export  # noqa: E402


def _settings_conn(rows: list[dict]) -> sqlite3.Connection:
    """A real in-memory research.db slice seeded with the given research_settings rows.

    The schema comes from researchdb._DDL itself — never a restated copy — so the
    table the tests read is byte-identical to the one production reads. Each row
    dict carries scope/key/value/value_type; value is the TEXT form exactly as the
    real table stores it (parse_value re-types it on read, same as production).
    """
    conn = sqlite3.connect(":memory:")           # throwaway DB, one per test
    conn.row_factory = sqlite3.Row               # dict-style row access, matching researchdb._connect
    conn.executescript(researchdb._DDL)          # the real schema, from the module that owns it
    conn.executemany(                            # seed the settings rows the test wants "live"
        "INSERT INTO research_settings (scope, key, value, value_type, updated_at)"
        " VALUES (?, ?, ?, ?, 0)",
        [(r["scope"], r["key"], r["value"], r["value_type"]) for r in rows],
    )
    return conn


class TestBenchmarkPairs:
    """V1: buy-and-hold benchmark from price + initial equity."""

    def test_normalises_to_starting_equity(self):
        # equity[0] = 10000, first close = 100, so the multiplier is 100.
        price = [{"timestamp": 1, "close": 100.0}, {"timestamp": 2, "close": 110.0}]
        equity = [[1, 10000.0], [2, 10100.0]]
        out = export._benchmark_pairs(price, equity)
        assert out == [[1, 10000.0], [2, 11000.0]]

    def test_empty_price_yields_empty(self):
        assert export._benchmark_pairs([], [[1, 10000.0]]) == []

    def test_empty_equity_yields_empty(self):
        assert export._benchmark_pairs([{"timestamp": 1, "close": 100.0}], []) == []

    def test_zero_first_close_yields_empty(self):
        # Degenerate input — emit nothing rather than divide-by-zero.
        price = [{"timestamp": 1, "close": 0.0}]
        equity = [[1, 10000.0]]
        assert export._benchmark_pairs(price, equity) == []

    def test_negative_first_close_yields_empty(self):
        # Pathological data — same guard as zero.
        price = [{"timestamp": 1, "close": -5.0}]
        equity = [[1, 10000.0]]
        assert export._benchmark_pairs(price, equity) == []


class TestSettingsDrift:
    """A4: snapshot vs live-table value comparison."""

    def test_no_snapshot_returns_no_drift(self):
        # When the snapshot string is None (include_settings=False on a share),
        # we emit "no drift" without touching the DB.
        assert export._settings_drift(None) == {"drifted": False, "changes": []}

    def test_empty_snapshot_returns_no_drift(self):
        assert export._settings_drift("") == {"drifted": False, "changes": []}

    def test_matching_values_no_drift(self):
        # Snapshot values are TYPED (the snapshot serializes _live_settings output),
        # so a number setting is stored as 0.001, not the string "0.001".
        snapshot = json.dumps([
            {"scope": "fees", "key": "crypto.default", "value": 0.001, "value_type": "number"},
        ])
        conn = _settings_conn([                  # live table carries the same value
            {"scope": "fees", "key": "crypto.default", "value": "0.001", "value_type": "number"},
        ])
        with patch.object(export.researchdb, "_connect", return_value=conn):
            result = export._settings_drift(snapshot)
        conn.close()                             # the product code never closes; the test owns the conn
        assert result == {"drifted": False, "changes": []}

    def test_differing_values_reported_as_drift(self):
        snapshot = json.dumps([
            {"scope": "fees", "key": "crypto.default", "value": 0.001, "value_type": "number"},
        ])
        conn = _settings_conn([                  # live table has since moved to 0.005
            {"scope": "fees", "key": "crypto.default", "value": "0.005", "value_type": "number"},
        ])
        with patch.object(export.researchdb, "_connect", return_value=conn):
            result = export._settings_drift(snapshot)
        conn.close()
        assert result["drifted"] is True
        # Both sides come back TYPED: run_value from the snapshot JSON, current_value
        # from the real _live_settings read (parse_value re-types the stored TEXT).
        assert result["changes"] == [
            {"scope": "fees", "key": "crypto.default", "run_value": 0.001, "current_value": 0.005},
        ]

    def test_keys_present_only_in_live_are_not_reported(self):
        # The snapshot freezes the layout that existed at dispatch time. Layout drift
        # (new keys added since) is separate from value drift; _settings_drift only
        # reports value changes for keys present in both sides.
        snapshot = json.dumps([
            {"scope": "fees", "key": "crypto.default", "value": 0.001, "value_type": "number"},
        ])
        conn = _settings_conn([
            {"scope": "fees", "key": "crypto.default", "value": "0.001", "value_type": "number"},
            {"scope": "fees", "key": "equity.default", "value": "0.005", "value_type": "number"},  # added since
        ])
        with patch.object(export.researchdb, "_connect", return_value=conn):
            result = export._settings_drift(snapshot)
        conn.close()
        assert result["drifted"] is False
        assert result["changes"] == []
