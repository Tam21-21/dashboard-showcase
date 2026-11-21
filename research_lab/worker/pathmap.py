"""The /data<->host path mapping shared by the worker job handlers (Brief B, D-1).

Pure on purpose: pathlib only, no httpx, no env reads. It lives apart from helpers.py
because helpers.py imports httpx and reads two REQUIRED env vars at module load
(RESEARCH_WORKER_API_KEY / RESEARCH_SERVER_URL), so importing helpers on a plain interpreter
raises. hard_delete_dataset.py is deliberately import-light so its module-level
remove_dataset_file can be exercised by a plain-interpreter probe; routing this mapping through
helpers would pull httpx + a KeyError into that probe. Keeping it here lets both fetch_dataset
and hard_delete_dataset import it without either depending on the other or on helpers.
"""
from __future__ import annotations

from pathlib import Path


def to_host_path(file_path: str, data_root: Path) -> Path:
    """Map a container-relative '/data/...' path to its host path under data_root (arch §8).

    research_datasets.file_path is stored container-relative (the fetch/backtest containers run
    with --out-root /data), so the leading '/data' must be rebased to the worker's data root before
    any host-side read or unlink — otherwise '/data/...' never resolves under data_root. A
    non-'/data' path is returned unchanged (defensive, for any legacy host-style row); the caller's
    containment guard still applies either way.
    """
    prefix = "/data"
    if file_path.startswith(prefix + "/"):  # the stored convention
        return data_root / file_path[len(prefix) + 1:]  # strip '/data/', rebase under the host root
    return Path(file_path)  # already host-style — leave it for the caller to check
