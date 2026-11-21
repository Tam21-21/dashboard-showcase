"""Dashboard configuration — env vars only, no secrets checked in."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
USER_DB_PATH = Path(os.environ.get("DASHBOARD_USER_DB_PATH", PROJECT_ROOT / "dashboard" / "dashboard.db"))
RESEARCH_DB_PATH = Path(os.environ.get("RESEARCH_DB_PATH", PROJECT_ROOT / "dashboard" / "research.db"))
# M2.5 EL1: events live in a separate SQLite file on the server, distinct from research.db
# (per-database single-writer property of SQLite WAL plus operational backup blast radius).
# Env-overridable so probes can target a throwaway file (spec brief §13.1 pattern).
EVENTS_DB_PATH = Path(os.environ.get("EVENTS_DB_PATH", PROJECT_ROOT / "dashboard" / "events.db"))
# OPS-3 (ops_build_brief.md §7.1, §12.2): the operations page's own store, a fourth SQLite file
# separate from the three above so monitor state never shares a write lock or a backup unit with
# research data. Env-overridable so a probe can target a throwaway file instead of the real ops.db.
OPS_DB_PATH = Path(os.environ.get("OPS_DB_PATH", PROJECT_ROOT / "dashboard" / "ops.db"))
# Night six, lane P (docs/briefs/paper_trading_spec_brief.md §10.1): the fifth SQLite file,
# paper.db, separate from the four above for the same two reasons OPS_DB_PATH already is
# (per-database single-writer property of SQLite plus a smaller backup blast radius), with more
# force here because this store sits on the money path: the paper node writes on every bar for
# every running strategy, and that traffic must never share a write lock with research dispatch.
# Env-overridable so a probe can target a throwaway file instead of the real paper.db.
PAPER_DB_PATH = Path(os.environ.get("PAPER_DB_PATH", PROJECT_ROOT / "dashboard" / "paper.db"))
# Night one, lane RT (real-trading plumbing spec, section 18 decision 1): the sixth SQLite file,
# a store separate from the five above for the same two reasons OPS_DB_PATH and PAPER_DB_PATH
# already are (per-database single-writer property of SQLite plus a smaller backup blast radius),
# with more force here because this store is the system of record for the kill switch, the kill
# drill and the enabling record: a bug reaching this file must not be able to reach anything else.
# Env-overridable so a probe can target a throwaway file instead of the real store. Building this
# store never turns real trading on: no function in this lane's build ever writes the enabling
# record; that append only happens by hand, out of band, at the go (section 18 decision 4).
REAL_DB_PATH = Path(os.environ.get("REAL_DB_PATH", PROJECT_ROOT / "dashboard" / "real.db"))
# Lane UI20-0a (DECISIONS #477 item 6, #478 item 2): the one real account the Capital page reads
# about. No default anywhere: unset, empty or only whitespace means None ("no real account
# configured"). Routes read it through the module attribute (config.REAL_ACCOUNT_ID), so a probe
# can monkeypatch it. Setting it authorises nothing: the enabling record is still appended by hand.
_real_account_raw = os.environ.get("REAL_ACCOUNT_ID", "")
REAL_ACCOUNT_ID = _real_account_raw if _real_account_raw.strip() else None
# OPS-3 (§8.2): the four-field (Source, Mechanism, Sensitivity, Bounds) provenance file for every
# operations cadence, decay window and threshold, kept separate from
# docs/research_lab/parameter_provenance.md because that file's own boot check requires its
# heading set to equal the research settings layout exactly, and operations keys are not research
# keys. Env-overridable so a probe can target a throwaway copy instead of the real committed file.
OPS_PROVENANCE_PATH = Path(os.environ.get(
    "OPS_PROVENANCE_PATH", PROJECT_ROOT / "docs" / "ops" / "ops_parameter_provenance.md"))
# Disk mirror of strategy source (derived data, arch §8). PUT (brief G) writes here best-effort
# and startup reconciliation (brief H) rewrites it from the DB. Env-overridable so probes target
# a throwaway dir instead of the git-tracked tree.
# M4 R1: the default lives OUTSIDE the checkout (PROJECT_ROOT.parent, e.g. /home/<user>/strategy_mirror).
# The old default pointed at research_lab/strategies/ INSIDE the checkout, where boot reconciliation
# (DB -> disk) could overwrite a version-controlled strategy source (the footgun: an editor save that
# diverged from the committed file got clobbered on the next restart). Repointed, not removed (R1): the
# mirror keeps its git-history / human-inspection role, just on a tree that is not the source of truth.
# Prod sets RESEARCH_STRATEGY_DIR explicitly in /home/newsbot/trading/.env.
RESEARCH_STRATEGY_DIR = Path(os.environ.get(
    "RESEARCH_STRATEGY_DIR", PROJECT_ROOT.parent / "strategy_mirror"))
# Parameter-provenance markdown; init_db() cross-checks it against the settings layout (arch §19).
# Env-overridable so a probe can point the check at a throwaway copy (brief A probe step 7).
PARAMETER_PROVENANCE_PATH = Path(os.environ.get(
    "RESEARCH_PROVENANCE_PATH",
    PROJECT_ROOT / "docs" / "research_lab" / "parameter_provenance.md",
))
# Ranges file of generator v2 (one table per range); the strategy-parameters read points a theory pick at it.
# Env-overridable so a test can point the read at a throwaway copy.
RANGES_PATH = Path(os.environ.get(
    "RESEARCH_RANGES_PATH", PROJECT_ROOT / "research_lab" / "theories" / "ranges.toml"))
# Server-side store for run artifacts the worker uploads (brief K.0). The container writes
# parquets on the laptop; the dashboard runs on the server, so the worker ships them here and
# RunExplorer (K) / export (K.5) read them locally. Gitignored; env-overridable so probes write
# to a throwaway dir instead of the real store.
RESEARCH_ARTIFACT_ROOT = Path(os.environ.get(
    "RESEARCH_ARTIFACT_ROOT", PROJECT_ROOT / "dashboard" / "run_artifacts"))

# M2.5 brief E canonical loader: source files must live under this prefix per
# spec brief §6.1. The endpoint validates the prefix and returns 400 on a path
# outside it. The default is the server's import staging dir; env-overridable
# so a probe can target a tmpdir-staged JSONL file. Trailing slash is
# preserved by the loader at the comparison site so a path that matches the
# prefix as a substring but not a directory boundary is rejected.
EVENT_LOADER_SOURCE_PREFIX = Path(os.environ.get(
    "EVENT_LOADER_SOURCE_PREFIX", "/home/newsbot/imports",
))

# K.6 share URL base: the absolute origin a created-share URL points at. Default
# loopback for dev / TestClient; the server .env must set
# https://privatelifedashboard.com so POST /share returns the public URL the
# bypass on Caddy is reachable at. Derived-from-request.base_url is wrong here:
# uvicorn launches without --proxy-headers (verified live 2026-05-24), so
# request.base_url is always http://127.0.0.1:8001 regardless of how the client
# reached the server. The explicit env var is the right knob.
RESEARCH_DASHBOARD_BASE_URL = os.environ.get(
    "RESEARCH_DASHBOARD_BASE_URL", "http://127.0.0.1:8001",
).rstrip("/")

# Google Calendar OAuth2 (create credentials at console.cloud.google.com)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://privatelifedashboard.com/api/auth/google/callback",
)

# Garmin Connect credentials (unofficial lib — no public OAuth2 exists)
GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")
# Optional TOTP seed if MFA ever gets enabled on the account
GARMIN_MFA_SECRET = os.environ.get("GARMIN_MFA_SECRET", "")
# Token cache — avoids re-authenticating on every hourly sync
GARMIN_TOKEN_STORE = Path(os.environ.get("GARMIN_TOKEN_STORE", PROJECT_ROOT / "dashboard" / ".garmin_tokens"))

# OpenWeatherMap (free tier — https://openweathermap.org/api)
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
OPENWEATHER_CITY = os.environ.get("OPENWEATHER_CITY", "Lyon,FR")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# CORS origins for local dev (Vite on :5173)
CORS_ORIGINS = os.environ.get(
    "DASHBOARD_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")

# The SEC User-Agent every SEC request sends (fair-access policy: a declared name and a contact email).
# This is the one setting; every SEC caller reads it and nothing else. The EDGAR_USER_AGENT environment value
# wins (production's .env sets it); without it the line below is used. It is the project's contact address,
# never a personal one (lane FA1b-1).
SEC_USER_AGENT = os.environ.get("EDGAR_USER_AGENT") or "TradingResearch research@example.com"

# Brief A2 (D3): per-instance EDGAR-backfill cap files the dashboard writes before dispatching
# `newsbot-edgar-backfill@<range>.service`; the unit loads <dir>/backfill-%i.env -> EDGAR_MAX_FILINGS.
# This MUST match the unit's `EnvironmentFile=-` path on the server, so set EDGAR_BACKFILL_JOB_DIR in
# the server .env to /home/newsbot/edgar-backfill-jobs (the unit's literal). The default below is a
# laptop-local dir for dev/probes (the probe overrides it via env-before-import). A mismatch is SAFE,
# not silent: the unit finds no cap file, and the fetcher fails CLOSED (refuses, not uncapped).
EDGAR_BACKFILL_JOB_DIR = Path(os.environ.get(
    "EDGAR_BACKFILL_JOB_DIR", PROJECT_ROOT / "dashboard" / ".edgar_backfill_jobs"))

# M3.75 FUND-1 (§3.3): per-instance fundamentals-backfill scope files the dashboard writes before
# dispatching `newsbot-fundamentals-backfill@<token>.service`; the unit loads <dir>/backfill-%i.env
# -> FUND_KIND + FUND_CIKS (the resolved CIK set, NEVER on the command line — the injection-safety
# property, §3.3). This MUST match the unit's `EnvironmentFile=-` path on the server, so set
# FUNDAMENTALS_JOB_DIR in the server .env to /home/newsbot/fundamentals-backfill-jobs (the unit's
# literal). The default below is a laptop-local dir for dev/probes (the probe overrides it via
# env-before-import). A mismatch is SAFE, not silent: the unit finds no scope file, and the fetcher
# fails CLOSED (refuses for lack of a scope), never an empty/default-scope run.
FUNDAMENTALS_JOB_DIR = Path(os.environ.get(
    "FUNDAMENTALS_JOB_DIR", PROJECT_ROOT / "dashboard" / ".fundamentals_backfill_jobs"))

# Dataset-preview chart downsampling (Brief B, R10): the max OHLC candles the preview read endpoint
# returns to the browser. A display knob (no parameter provenance, spec §3.6) — it bounds the browser
# payload independent of the dataset's row count. RATIFIED at 2000 (DECISIONS #74 item #4): a
# candlestick chart shows a few hundred candles legibly and lightweight-charts handles a few thousand.
PREVIEW_MAX_BUCKETS = 2000
