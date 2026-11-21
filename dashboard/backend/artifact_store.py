"""Server-side store for run artifacts (brief K.0).

The container writes a run's parquet artifacts on the laptop; the dashboard runs on the
server and cannot read laptop files. The worker uploads the parquets to the server (Option
A, arch §8); this module is where the server keeps them, and RunExplorer (K) / export (K.5)
read them back through it.

DDIA Ch 12 ("Keeping systems in sync", line 3033 "Need one source of truth, with others as
followers"): the system of record for the artifact bytes is THIS store. The laptop copy is a
non-authoritative cache (it is retained until disk pressure, arch §8, but not authoritative).
The K.0 invariant — a run reaches status='succeeded' only after its bytes are confirmed in
the store — is enforced in routes/research.py: the /result handler checks exists() before
complete_run flips the status, so the DB never records 'succeeded' without the bytes present.

ArtifactStore is an interface so the M3 local-filesystem impl can be swapped for an object
store (S3/MinIO) at M7 with no change to the readers, which only call open()/exists(). put()
takes a stream or path (not in-memory bytes) so a future portfolio.vbt pin upload (arch §8,
up to 5 GB) streams rather than buffering the whole file in memory.
"""
from __future__ import annotations

import hashlib
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from dashboard.backend import config

_CHUNK = 1024 * 1024  # 1 MiB streaming chunks — bounded memory even for a multi-GB pin file


class ArtifactIntegrityError(Exception):
    """A stored file's computed sha256 did not match the declared digest."""


class ArtifactNotFound(Exception):
    """open() was called for a (run_id, name) that is not in the store."""


class ArtifactStore(ABC):
    """Where a run's artifact bytes live on the server. M3 impl: local filesystem."""

    @abstractmethod
    def put(self, run_id: int, name: str, src: "BinaryIO | str | Path", *, sha256: str) -> None:
        """Stream `src` into the store under (run_id, name), verifying sha256 as it writes.

        Raises ArtifactIntegrityError on a digest mismatch and leaves nothing behind, so a
        corrupted upload never becomes exists()-true. Idempotent: a re-upload overwrites.
        """

    @abstractmethod
    def open(self, run_id: int, name: str) -> BinaryIO:
        """Open a stored artifact for binary reading. Raises ArtifactNotFound if absent."""

    @abstractmethod
    def exists(self, run_id: int, name: str) -> bool:
        """True if (run_id, name) is in the store. The /result gate's presence check."""

    @abstractmethod
    def delete_run(self, run_id: int) -> None:
        """Remove all of a run's artifacts (the run-delete cleanup hook, brief K.0 §6)."""

    # --- dataset previews (Brief B, R9): a dataset-scoped namespace over the same store ---
    # A dataset preview has no run, so it gets a dataset-scoped key (a sibling of the run key);
    # the literal `datasets/` segment can never collide with a numeric run-id dir, so existing
    # run artifacts are untouched and no migration is needed.

    @abstractmethod
    def put_dataset_preview(self, dataset_id: int, name: str, src: "BinaryIO | str | Path", *, sha256: str) -> None:
        """Stream `src` into the dataset-preview namespace under (dataset_id, name), verifying sha256.

        Raises ArtifactIntegrityError on a digest mismatch and leaves nothing behind, so a
        corrupted upload never becomes exists_dataset_preview()-true. Idempotent: a re-upload
        overwrites, so every fetch / re-fetch / update-to-latest refreshes the preview.
        """

    @abstractmethod
    def open_dataset_preview(self, dataset_id: int, name: str) -> BinaryIO:
        """Open a stored dataset preview for binary reading. Raises ArtifactNotFound if absent."""

    @abstractmethod
    def exists_dataset_preview(self, dataset_id: int, name: str) -> bool:
        """True if (dataset_id, name) is in the preview namespace. The read endpoint's available gate."""

    @abstractmethod
    def delete_dataset_preview(self, dataset_id: int, name: str) -> bool:
        """Remove a dataset's preview (hard-delete cleanup, Brief B.1.4).

        Idempotent: returns True if a file was removed, False if it was already absent.
        """


class LocalArtifactStore(ArtifactStore):
    """Filesystem impl: ``<root>/{run_id}/{name}``. Server-side, gitignored."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)  # one subdir per run_id underneath

    @staticmethod
    def _check_name(name: str) -> None:
        # name must be a bare filename; reject separators / traversal so a crafted name
        # cannot escape the dir (defense in depth — the worker only sends fixed names).
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ValueError(f"illegal artifact name: {name!r}")

    def _run_dir(self, run_id: int) -> Path:
        return self._root / str(run_id)

    def _path(self, run_id: int, name: str) -> Path:
        self._check_name(name)
        return self._run_dir(run_id) / name

    def _dataset_dir(self, dataset_id: int) -> Path:
        # the literal `datasets/` segment can never collide with a numeric run-id dir (Brief B)
        return self._root / "datasets" / str(dataset_id)

    def _dataset_path(self, dataset_id: int, name: str) -> Path:
        self._check_name(name)  # same traversal guard the run path uses
        return self._dataset_dir(dataset_id) / name

    def _write_verified(self, dest: Path, src, sha256: str, ctx: str) -> None:
        # The shared streaming-write+verify+atomic-rename body, used by both put (run artifacts)
        # and put_dataset_preview, so the integrity path is identical and not duplicated.
        dest.parent.mkdir(parents=True, exist_ok=True)  # parents=True: datasets/{id}/ is two levels deep, absent on first upload
        tmp = dest.parent / (dest.name + ".part")  # write to a sidecar, rename on success (atomic)
        h = hashlib.sha256()
        opened = open(src, "rb") if isinstance(src, (str, Path)) else None  # close only what we open
        reader = opened if opened is not None else src
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = reader.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)  # hash and write in the same pass over the stream
                    out.write(chunk)
        finally:
            if opened is not None:
                opened.close()
        digest = h.hexdigest()
        if digest != sha256:
            tmp.unlink(missing_ok=True)  # never leave a corrupt file exists()-true
            raise ArtifactIntegrityError(f"{ctx}: declared sha256 {sha256}, computed {digest}")
        tmp.replace(dest)  # atomic rename; overwrites a prior copy, so re-upload is idempotent

    def put(self, run_id, name, src, *, sha256):
        self._write_verified(self._path(run_id, name), src, sha256, f"run {run_id} {name}")

    def open(self, run_id, name):
        p = self._path(run_id, name)
        if not p.is_file():
            raise ArtifactNotFound(f"run {run_id} artifact {name!r} not in store")
        return open(p, "rb")

    def exists(self, run_id, name):
        return self._path(run_id, name).is_file()

    def delete_run(self, run_id):
        shutil.rmtree(self._run_dir(run_id), ignore_errors=True)  # absent dir is a no-op

    # --- dataset previews (Brief B, R9): same store, dataset-scoped namespace ---

    def put_dataset_preview(self, dataset_id, name, src, *, sha256):
        self._write_verified(self._dataset_path(dataset_id, name), src, sha256,
                             f"dataset {dataset_id} preview {name}")

    def open_dataset_preview(self, dataset_id, name):
        p = self._dataset_path(dataset_id, name)
        if not p.is_file():
            raise ArtifactNotFound(f"dataset {dataset_id} preview {name!r} not in store")
        return open(p, "rb")

    def exists_dataset_preview(self, dataset_id, name):
        return self._dataset_path(dataset_id, name).is_file()

    def delete_dataset_preview(self, dataset_id, name):
        p = self._dataset_path(dataset_id, name)
        existed = p.is_file()
        p.unlink(missing_ok=True)  # idempotent: a missing preview is a clean no-op
        try:
            self._dataset_dir(dataset_id).rmdir()  # drop the now-empty datasets/{id}/ dir
        except OSError:
            pass  # dir not empty (a future second preview name) or already gone — fine
        return existed


# Module-level singleton (mirrors sse.broadcaster). Built from config so probes can point
# RESEARCH_ARTIFACT_ROOT at a throwaway dir before importing this module.
store: ArtifactStore = LocalArtifactStore(config.RESEARCH_ARTIFACT_ROOT)
