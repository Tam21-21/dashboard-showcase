"""The sizing-method registry (#376 ruling 4; the dashboard UI plan's 2.3.3 item 2, option (a),
ruled by Tamer 2026-09-07, over option (b) which would have made helpers editable store rows and
broken provenance). Baked into the image by Dockerfile line 38, exactly like ``sizing.py`` itself,
so a new method module ships only through a repo edit, a commit and an image rebake -- never a
live-editable row -- which is the whole reason this design keeps every saved run's cost math
traceable to one exact commit.

A "method" is one named, ready-to-use ``SizingSpec`` (docs/design/dashboard_ui_plan_2026-09-07.md
line 113): a strategy declares which one it wants by id (``Strategy.sizing_method``, a plain string
class attribute next to ``sweep_params``); the container resolves that id through this registry
before it reads a single bar, and an id the registry does not carry fails the run right there,
never a silent fall-back to some other method.

Discovery, and why it is FILE-based, not package-relative import: every ``*.py`` file directly
inside this package (except ``__init__.py`` and a private ``_``-prefixed helper, none of which
exist today) is loaded with ``importlib.util.spec_from_file_location``, the exact idiom
``run_backtest_entrypoint._load_strategy_class`` already uses for the mounted strategy file. A
second module declaring an id already registered raises ``DuplicateSizingMethodError`` naming BOTH
files, at the moment this package is first imported -- before any container reads a setting, let
alone a bar. File-based loading (rather than ``importlib.import_module`` on this package's own
dotted name) is what lets a probe point ``_discover_methods`` at a throwaway fixture directory and
prove that refusal without touching the real, one-method registry at all.
"""
from __future__ import annotations  # lazy annotations: importing this package pulls in no vbt, no pandas

import importlib.util  # the same file-path loader _load_strategy_class already uses
from dataclasses import dataclass  # SizingMethod is a small, frozen, comparable record
from pathlib import Path  # the package's own directory, walked for method modules

from strategies.helpers.sizing import SizingSpec  # the five-field config every method resolves to


@dataclass(frozen=True)  # frozen: a registered method is read many times and never mutated
class SizingMethod:
    """One registered sizing method: a stable id, a human description, and the SizingSpec it resolves to.

    id: the string a strategy's ``sizing_method`` class attribute names, and the string every saved
        run's ``research_runs.sizing_method`` column records (#376 ruling 4).
    description: shown by the workspace's SETTINGS tab and the ``GET /api/research/sizing-methods``
        listing route, so a human picking a method sees what it does, not just its id.
    spec: the resolved SizingSpec this method hands to ``compute_position_sizes`` (via
        ``portfolio.py``'s / ``sweep_portfolio.py``'s own ``sizing=`` keyword).
    """

    id: str
    description: str
    spec: SizingSpec


class DuplicateSizingMethodError(ValueError):
    """Two method modules in this package (or a fixture directory under test) declare the same id.

    Refused at DISCOVERY time (this package's own import, or a probe's direct call to
    ``_discover_methods``), never silently overwritten: a registry where the second definition wins
    quietly is exactly the kind of drift AC-4 (sizing.py's own docstring) exists to prevent for the
    spec's OWN fields, and the same argument applies one level up, to which spec an id even names.
    """


class UnknownSizingMethodError(ValueError):
    """A strategy (or any other caller) named a sizing method id this registry does not carry.

    Raised by ``resolve()`` naming both the unknown id and the full set of ids that ARE registered,
    so the container's clean machine-readable error line (every entrypoint's own except-clause) is
    enough on its own to see what a strategy author should have written instead.
    """


def _discover_methods(directory: "Path") -> "dict[str, SizingMethod]":
    """Load every method module directly inside ``directory`` and return {id: SizingMethod}.

    A "method module" is any ``*.py`` file that is neither ``__init__.py`` nor ``_``-prefixed (a
    private helper; none exist in this package today, but the exclusion is symmetric with
    ``run_backtest_entrypoint``'s own strategy-file convention) AND that defines a module-level
    ``METHOD`` attribute of type ``SizingMethod``. A file with no ``METHOD`` attribute is skipped
    (a future private helper module would fall through here harmlessly); a ``METHOD`` of the wrong
    type is refused loudly, never coerced. Files are visited in sorted order so the duplicate-id
    error names the two files in a deterministic, reproducible order.
    """
    registry: "dict[str, SizingMethod]" = {}
    origin_by_id: "dict[str, str]" = {}  # method id -> the file path that first declared it (for the error message)
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue  # never re-load this file itself, and never treat a private helper as a method
        spec = importlib.util.spec_from_file_location(f"sizing_method_{path.stem}", path)
        if spec is None or spec.loader is None:  # a malformed path or loader; mirrors _load_strategy_class's own guard
            raise ImportError(f"cannot load a module spec from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # run the file (defines METHOD)
        method = getattr(module, "METHOD", None)
        if method is None:  # no METHOD attribute at all: not a method module (none exist today, defensive only)
            continue
        if not isinstance(method, SizingMethod):  # a METHOD of the wrong shape is a build-time bug, never coerced
            raise TypeError(f"{path}: METHOD must be a SizingMethod, got {type(method).__name__}")
        if method.id in origin_by_id:  # the same id declared twice: refuse loudly, naming both files
            raise DuplicateSizingMethodError(
                f"sizing method id {method.id!r} is declared twice: {origin_by_id[method.id]} and {path}"
            )
        origin_by_id[method.id] = str(path)
        registry[method.id] = method
    return registry


_THIS_DIR = Path(__file__).resolve().parent  # this package's own directory (never a caller-supplied path)
REGISTRY: "dict[str, SizingMethod]" = _discover_methods(_THIS_DIR)  # built once, at first import of this package


def resolve(method_id: str) -> "SizingMethod":
    """Return the registered SizingMethod for ``method_id``.

    Raises ``UnknownSizingMethodError`` naming the unknown id and the full registered id set when
    ``method_id`` is not in the registry -- never a silent fall-back to any other method. Every
    entrypoint calls this BEFORE it reads a single bar (the container is the authority, OD-7), so an
    id typo on a strategy class fails the run at the gate, not partway through a sized backtest.
    """
    try:
        return REGISTRY[method_id]
    except KeyError:
        raise UnknownSizingMethodError(
            f"unknown sizing method {method_id!r}; registered ids: {sorted(REGISTRY)}"
        ) from None
