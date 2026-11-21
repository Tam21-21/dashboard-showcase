"""M2.5 brief K container-boundary materializer package.

Spec brief v4 §6.10 / §8.1-§8.4 / §8.6 / EL24-EL26 / EL44; arch v6 §9.1-§9.5.
The materializer is the single-source-of-truth -> derived-follower bridge
(DDIA Ch 12): it reads events.db (the system of record) and emits a flat,
denormalized parquet (the derived data) the M3.5 backtest container mounts
read-only. It never writes back to events.db.

Two modules:
  output_schemas.py - the per-event-type output-column registry + the §8.4
                      validator cross-check (what M3.5 sees in the parquet).
  build.py          - the §8.6 SQL builder + the pyarrow table -> parquet
                      bytes engine (materialize_event_slice).
"""
