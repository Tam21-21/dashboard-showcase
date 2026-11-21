"""Generate the committed EODHD ticker manifest (M5 U0, AD-M5-14).

The EODHD daily parquet store is laptop-local (~/eodhd/daily/*.US.parquet); the dashboard backend
runs on the server and cannot see those files. So the backend's Add-Dataset ticker search reads a
committed list of "which EODHD tickers exist" instead. This laptop-run script produces that list.

It lists the daily store, strips each filename to its BARE ticker (the one canonical wire/storage
form, AD-M5-14: 'AAPL.US.parquet' -> 'AAPL'), sorts + de-dups, and writes the committed artifact
dashboard/backend/data/eodhd_tickers.txt (newline-delimited, sorted, trailing newline). The backend's
load_eodhd_tickers() reads it at boot into research_eodhd_tickers.

Run from the repo root on the laptop:
    research_lab/worker/.venv/bin/python -m research_lab.fetcher.ops.gen_eodhd_manifest

Idempotent: re-running over the same store produces a byte-identical file. Fails loud (no silent
empty-file write) if the store directory is absent.
"""

import os  # read the EODHD_DAILY_HOST override (mirrors fetch_dataset.py)
from pathlib import Path  # filesystem glob + write

# The daily store dir. Default matches fetch_dataset.py's _EODHD_DAILY_HOST_DEFAULT, env-overridable
# by the SAME variable so the script and the worker resolve one source of truth.
_DAILY_STORE_DEFAULT = "~/eodhd/daily"

# The committed manifest path, resolved relative to THIS file. parents[3] walks ops -> fetcher ->
# research_lab -> repo root, then dashboard/backend/data/eodhd_tickers.txt. The backend resolves the
# same file relative to its own package (researchdb._EODHD_TICKERS_MANIFEST).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = _REPO_ROOT / "dashboard" / "backend" / "data" / "eodhd_tickers.txt"


class DailyStoreNotFoundError(FileNotFoundError):
    """The EODHD daily store directory does not exist. Raised so a missing store fails loud,
    never silently writing an empty manifest that would wipe the server's ticker index."""


def list_bare_tickers(store_dir: Path) -> list[str]:
    """Return the sorted, de-duped bare tickers in the daily store (`AAPL.US.parquet` -> `AAPL`)."""
    if not store_dir.is_dir():  # fail loud — an absent store must not yield an empty manifest
        raise DailyStoreNotFoundError(f"EODHD daily store not found at {store_dir}")
    # Glob only the *.US.parquet files; strip the fixed '.US.parquet' suffix to the bare ticker.
    suffix = ".US.parquet"
    tickers = {p.name[: -len(suffix)] for p in store_dir.glob("*.US.parquet")}
    return sorted(tickers)


def write_manifest(tickers: list[str], manifest_path: Path = _MANIFEST_PATH) -> None:
    """Write the manifest (newline-delimited, sorted, trailing newline). Creates the data/ dir."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)  # data/ may not exist on a clean tree
    manifest_path.write_text("".join(t + "\n" for t in tickers), encoding="utf-8")


def main() -> None:
    store_dir = Path(os.environ.get("EODHD_DAILY_HOST", os.path.expanduser(_DAILY_STORE_DEFAULT)))
    tickers = list_bare_tickers(store_dir)
    if not tickers:  # store dir exists but holds no *.US.parquet — almost certainly a mis-pointed path;
        # fail loud instead of writing an empty manifest that the boot loader would full-replace the
        # server's ticker index with (wiping 58k rows). The deploy gate also asserts a non-empty manifest.
        raise DailyStoreNotFoundError(
            f"EODHD daily store at {store_dir} holds no *.US.parquet files; refusing to write an empty "
            f"manifest (would wipe the server index). Check EODHD_DAILY_HOST / the store path."
        )
    write_manifest(tickers)
    print(f"gen_eodhd_manifest: wrote {len(tickers)} tickers from {store_dir} to {_MANIFEST_PATH}")


if __name__ == "__main__":
    main()
