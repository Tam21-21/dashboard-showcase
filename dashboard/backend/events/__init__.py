"""M2.5 event-store shared write core + supporting modules.

Submodules:
  errors        Named exception hierarchy (EventStoreError + subclasses).
  hash_queries  Single-source close-time and history-inclusive SQL per §8.5.
  types/<X>     Per-event-type validators (`sec_13d`, `analyst_rating`).
  write_core    `write_event_batch`, `apply_correction`, `apply_rollback`.

Brief B per spec brief v4 §11 / EL36. Touches only events.db (the loader,
fetcher, and admin endpoints sit on top in briefs D/E/G).
"""
