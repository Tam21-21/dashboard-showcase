"""M3.75 fundamentals (XBRL CompanyFacts) — the dedicated bitemporal fact store and its
acquisition / parse / read / catalog surface.

This package is the data-side half of M3.75 (Track 2B, the fundamentals data type). It is a
SEPARATE store from the event store (arch §2, DECISIONS #91): fundamentals are a tall,
concept-as-data, bitemporal fact table in events.db, NOT rows in research_events. The
engine-side half (the multi-source feature channel, Track 2A) lives in
research_lab/strategies/base/fundamental_features.py.

Modules:
  concepts  — the normalized concept vocabulary (which XBRL tags map to which internal name).
  parse     — the CompanyFacts JSON -> normalized FactRow parser + the coverage report.
  reads     — the three point-in-time reads over the kept-every-vintage store (R4 / §5).
  write     — the idempotent batch write path + the orphan-batch reaper (§3).
  catalog   — the one row-backed research_datasets catalog handle (§4).
"""
