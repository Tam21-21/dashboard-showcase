"""Strategy source disk mirror (derived data, arch §8 / DDIA Ch 12 derived-data discipline).

The DB (research_strategies.source_code) is the source of truth. The on-disk
research_lab/strategies/{name}.py files are a derived mirror for git history and human
inspection — the worker NEVER reads them (it gets source from the job payload, brief E).

- Brief G PUT writes the mirror best-effort after the DB commit and runs a best-effort git
  hook. A mirror or hook failure logs a warning and never fails the request (L9): the DB
  already holds the truth.
- Brief H startup reconciliation rewrites every file from the DB so disk matches after any
  prior mirror failure. Steady-state it writes byte-identical content (empty diff, git sees
  nothing); it only churns git on a real recovery.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import RESEARCH_STRATEGY_DIR

log = logging.getLogger(__name__)


def _target(name: str, mirror_dir: Path | None) -> Path:
    """The mirror file path for a strategy name, under the (overridable) strategy dir."""
    base = RESEARCH_STRATEGY_DIR if mirror_dir is None else mirror_dir
    return base / f"{name}.py"


def _git_commit(path: Path, message: str) -> None:
    """Best-effort git add + commit of one mirror file. Failures are logged, never raised (L9)."""
    try:
        subprocess.run(["git", "-C", str(path.parent), "add", str(path)],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(path.parent), "commit", "-m", message, "--", str(path)],
                       check=True, capture_output=True, text=True)
    except FileNotFoundError:
        log.warning("git not found; skipped strategy mirror commit", extra={"path": str(path)})
    except subprocess.CalledProcessError as exc:
        # Non-zero git (nothing to commit, not a repo, hook reject) is non-fatal — the file is mirrored.
        log.warning("git commit of strategy mirror failed (non-fatal)",
                    extra={"path": str(path), "returncode": exc.returncode, "stderr": exc.stderr})


def mirror_strategy(name: str, source_code: str, *, mirror_dir: Path | None = None,
                    run_git: bool = True) -> tuple[str, str, str | None]:
    """Write one strategy's source to its mirror file. Returns (status, path, error).

    status is 'synced' when the file write succeeds (git is best-effort and does not affect
    it), 'failed' when the write itself fails (last_mirror_error carries the reason). Never
    raises — the caller persists the outcome and always returns 2xx (L9).
    """
    path = _target(name, mirror_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)  # ensure the strategy dir exists
        path.write_text(source_code, encoding="utf-8")   # the mirror write
    except OSError as exc:  # disk full, permission, read-only mount, etc.
        log.warning("strategy mirror write failed", extra={"path": str(path), "error": str(exc)})
        return ("failed", str(path), str(exc))
    if run_git:
        _git_commit(path, f"research: mirror strategy {name}")
    return ("synced", str(path), None)


def read_mirror(name: str, *, mirror_dir: Path | None = None) -> str | None:
    """Read a strategy's on-disk mirror file content, or None if it is absent (M4 R11).

    The status badge compares this against the DB source_code to report whether the derived mirror
    matches the source of truth. A missing file returns None (the badge reads that as "no mirror yet"),
    never an exception — the DB always holds the truth, the mirror is derived (arch §8). An OSError on a
    present-but-unreadable file DOES propagate: that is a real disk fault the operator should see, not a
    silent "no mirror".
    """
    path = _target(name, mirror_dir)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def reconcile_mirror(strategies: list[dict], *, mirror_dir: Path | None = None) -> list[str]:
    """Rewrite the mirror from the DB rows (brief H). Returns the names whose on-disk content changed.

    Each item is a dict with 'name' and 'source_code'. Idempotent: a file already byte-identical
    is left untouched (no rewrite, so git sees nothing) and is absent from the returned diff list.
    """
    changed: list[str] = []
    for s in strategies:
        path = _target(s["name"], mirror_dir)
        new = s["source_code"]
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == new:
            continue  # steady state — byte-identical, no write, no churn
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        changed.append(s["name"])
    if changed:
        log.info("strategy mirror reconciled", extra={"rewritten": changed})
    return changed


def reconcile_on_startup(*, mirror_dir: Path | None = None) -> list[str]:
    """Read every strategy from the DB and reconcile the disk mirror (brief H startup hook).

    Best-effort and derived (arch §8): a failure here must never block startup, so the caller
    wraps it. Returns the list of rewritten names (empty in steady state).
    """
    from . import researchdb  # local import avoids a module-load cycle (researchdb never imports this)
    return reconcile_mirror(researchdb.all_strategies(), mirror_dir=mirror_dir)
