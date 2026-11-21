"""Render and export layer (M3 §5). build_run_bundle is the single canonical
parquet->JSON read path; RunExplorer (brief K) reads through it, and the K.5
renderers (json/md/html) consume the same bundle dict."""
