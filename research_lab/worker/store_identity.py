"""Computes a comparable identity for an EODHD store root (M5 fetcher job 10, AC-7: "a form a later
reader can compare between two runs. A probe proves two runs over the same store report the same
identity, and that a run over a different store reports a different one").

Pure os + pathlib + hashlib + datetime, no httpx, no env read at import time, matching store_root.py's own
and store_sync.py's own reasoning for staying import-light enough for a plain-interpreter probe.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path


def compute_store_identity(store_root) -> dict:
    """Return {"store_id": <str>, "store_fetched_at": <str>} describing the store rooted at
    store_root.

    store_root may be a str or a Path (both shapes appear across this build's own callers); coerced
    to a Path here so either works without the caller needing to know which.

    store_id is a sha256 hex digest over the sorted (relative path, size in bytes) pairs of every
    file under store_root -- content-shaped, not path-shaped, matching this unit's own design
    constraint ("make the store identity something a reader can compare, not prose"): two DIFFERENT
    directories holding the SAME set of (relative path, size) pairs hash to the SAME id, and any file
    added, removed or resized changes it. Size rather than a full byte hash on purpose: the real store
    holds tens of gigabytes -- RA-5 gate-review SHOULD-FIX 7, CORRECTED 2026-09-04: an earlier draft
    of this comment attributed a "61G" figure to root CLAUDE.md as a quotation; that text does not
    appear there (`grep -c '61G' CLAUDE.md` returns 0), so the citation was false and is removed. The
    underlying point stands, measured directly rather than quoted: `du -sh /home/tamer/market_data/
    eodhd` reported 39G over 141,287 files on 2026-09-04, and the store only grows, so re-run that
    command for the current figure rather than trusting this comment's own number later -- hashing
    every byte on every call would turn this function itself into a multi-minute operation; a size
    mismatch already proves the content differs, and a same-size-different-content forgery is not a
    real risk for a laptop-local research price store nothing else writes to.

    store_fetched_at is the newest file mtime found anywhere under store_root, as an ISO-8601 UTC
    string -- a proxy for "when this copy was last updated": a store only gains new content through a
    sync or a fetch writing a file, which stamps that file's own mtime with the write time, so the
    single newest mtime across the whole tree is the store's own most recent change. An empty store
    (no file at all, e.g. a fresh scratch destination before its first sync) is a legitimate state,
    not a caller error: RA-5 gate-review SHOULD-FIX 6 (2026-09-04) replaced a fixed epoch-zero
    fallback (which made every empty store's fetch date identical) with the store ROOT directory's
    own mtime, so two different empty stores, created at different times, still report different
    fetch dates rather than colliding on the same constant.
    """
    root = Path(store_root)
    size_pairs = []  # (relative_path_as_posix, size_in_bytes) pairs, one per file under root
    newest_mtime = None  # the largest st_mtime seen so far, or None until the first file is seen

    # os.scandir, not Path.rglob (lane W1, measured 2026-10-08): the rglob walk built a Path object
    # per file and cost 3.7 s per fetch job on the ~141k-file store. Same walk as rglob's default:
    # symlinked directories are not descended (rglob's recurse_symlinks=False), a symlinked file
    # counts through its target, exactly as p.is_file() and p.stat() did.
    stack = [(root, "")]  # (directory, its path relative to root as a posix prefix, "" for root)
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                rel = f"{prefix}{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, f"{rel}/"))
                    continue
                if not entry.is_file():
                    continue  # only files carry content; sockets, fifos and broken links add nothing
                file_stat = entry.stat()
                size_pairs.append((rel, file_stat.st_size))
                if newest_mtime is None or file_stat.st_mtime > newest_mtime:
                    newest_mtime = file_stat.st_mtime

    size_pairs.sort()  # deterministic order: the same file set hashes the same way every time, on
    # every OS, regardless of the order the directory walk happened to return.
    digest_input = "\n".join(f"{rel}\t{size}" for rel, size in size_pairs).encode("utf-8")
    store_id = hashlib.sha256(digest_input).hexdigest()

    if newest_mtime is None:
        # RA-5 gate-review SHOULD-FIX 6, CORRECTED BY THE LEAD 2026-09-04: a fixed epoch-zero
        # fallback made every empty store's store_fetched_at identical, contradicting AC-7's own
        # "a run over a different store reports a different identity" guarantee for the one input
        # shape (a genuinely empty store) where two different stores could otherwise collide. Fixed
        # HERE ONLY, never on store_id above: store_id must stay purely content-shaped (the sorted
        # size_pairs alone), because ac7_store_identity_comparable_across_stores.probe.py's own
        # guarantee is that two DIFFERENT directories holding the SAME content report the SAME
        # store_id -- keying store_id off the root directory's own mtime would break that for two
        # populated stores whose directory entries were written at genuinely different wall-clock
        # moments (mkdir/write calls bump a directory's own mtime). The store ROOT directory's own
        # mtime is the next most honest proxy for "when this copy was last touched" when no file
        # exists to derive one from: two different empty directories, created at different times,
        # differ by it naturally, closing the "every empty store looks identical" gap.
        newest_mtime = root.stat().st_mtime
    store_fetched_at = datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()

    return {"store_id": store_id, "store_fetched_at": store_fetched_at}
