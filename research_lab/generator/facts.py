"""Reads the compile-facts JSON file compile_facts.py produces inside
research-runner:m3-latest (section 5.2's two-process shape). A facts file that lacks any
filler the theory names, or lacks the pinned image/vbt_version pair, is a refusal naming
the missing key -- never a silent compile attempted on the host, which has no vbt at all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# The pinned build this unit is written against (root CLAUDE.md: "VBT Pro: frozen
# permanently at commit b1727ac6 (2026.4.7...)"). A facts file compiled under any other
# build is stale against the pin and is refused before generation touches it.
PINNED_IMAGE = "research-runner:m3-latest"
PINNED_VBT_VERSION = "2026.4.7"


class FactsLoadError(ValueError):
    """Raised when the facts file is missing a required top-level key, is missing a filler
    the theory names, or was compiled under a build other than the pinned one."""


@dataclass(frozen=True)
class Facts:
    image: str
    vbt_version: str
    # filler_id -> {class, param_names, input_names, output_names, expression, warmup_bars,
    # warmup_series_length, warmup_series_formula}. warmup_bars arrived with lane SK (`3.2b`,
    # AD3.2's warmup dimension): the filler's own measured leading-undefined-row count,
    # required below so a stale facts file compiled before this lane cannot silently reach
    # emit.py's own required-key read.
    fillers: dict[str, dict]


def load_facts(path: "str | Path", *, required_filler_ids: "tuple[str, ...]" = ()) -> Facts:
    """Load and validate the compile-facts JSON. required_filler_ids is normally a theory's
    own `.fillers` tuple: every one of them must have a facts entry, or the file is stale
    or incomplete and generation refuses rather than guessing."""
    path = Path(path)
    if not path.exists():
        raise FactsLoadError(f"facts file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactsLoadError(f"facts file {path} is not valid JSON: {exc}") from exc

    for key in ("image", "vbt_version", "fillers"):
        if key not in raw:
            raise FactsLoadError(f"facts file {path} is missing required key {key!r}")

    if raw["image"] != PINNED_IMAGE or raw["vbt_version"] != PINNED_VBT_VERSION:
        raise FactsLoadError(
            f"facts file {path} was compiled under image={raw['image']!r} "
            f"vbt_version={raw['vbt_version']!r}; this build is pinned to "
            f"image={PINNED_IMAGE!r} vbt_version={PINNED_VBT_VERSION!r}. A facts file "
            f"compiled under a different build is stale against the pin (a drift is a "
            f"refusal before generation, never a silent run)."
        )

    fillers = raw["fillers"]
    missing = [f for f in required_filler_ids if f not in fillers]
    if missing:
        raise FactsLoadError(f"facts file {path} is missing filler(s) the theory names: {missing}")

    for filler_id, entry in fillers.items():
        # AC-7 (lane GR): a filler compile_facts.py itself could not compile carries the
        # refused shape ({"decision": "refused", "reason": ...}) instead of the usual
        # compiled-class shape below. It needs only its own reason, never the compiled
        # fields no compile ever produced (there is no "class" to require here, honestly);
        # a refused entry missing even its own reason is still refused loud, by name.
        if entry.get("decision") == "refused":
            if "reason" not in entry:
                raise FactsLoadError(
                    f"facts file {path} filler {filler_id!r} is refused but carries no 'reason'"
                )
            continue
        for required_field in (
            "class", "param_names", "input_names", "output_names", "expression",
            "warmup_bars",  # lane SK (`3.2b`): a facts file compiled before this lane is stale, refused by name here
        ):
            if required_field not in entry:
                raise FactsLoadError(
                    f"facts file {path} filler {filler_id!r} is missing required field {required_field!r}"
                )

    return Facts(image=raw["image"], vbt_version=raw["vbt_version"], fillers=fillers)
