"""M2.5 brief F — quality-report renderers (JSON / MD / HTML).

Three sibling modules per spec brief §7.3 / F brief in §11. Each accepts the
bundle dict from `events/quality_bundle.py` and returns rendered bytes:
  render_quality_json(bundle, **kwargs) -> bytes  (application/json)
  render_quality_md(bundle, **kwargs)   -> bytes  (text/markdown)
  render_quality_html(bundle, **kwargs) -> bytes  (text/html)

The MD + HTML renderers consume the bundle's `labels` slot for plain-English
translation of snake_case keys. JSON deliberately keeps snake_case (machine
output). Same single-source-of-truth pattern M3 K.5 uses (the renderers all
read from `render_labels`).
"""
