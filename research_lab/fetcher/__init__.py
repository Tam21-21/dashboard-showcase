"""EODHD raw fetcher (M5 data-library Unit 1).

A standalone, concurrent, resumable, idempotent fetcher that backfills the US
universe's raw market data onto the external SSD behind a SQLite manifest, per
docs/briefs/M5_eodhd_raw_fetcher_brief.md. Not the backtest worker; shares no
process with it. Run as: python -m research_lab.fetcher <subcommand>.
"""
