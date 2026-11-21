"""Pytest conftest: make the repo root importable so `dashboard.backend.*` resolves.

The test suite lives at dashboard/backend/tests/, so the import root is two parents
up. Same logic the executable probes use (see probes/m3_brief_k5_local_probe.py),
hoisted to a conftest so every test file gets it without boilerplate.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../trading
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
