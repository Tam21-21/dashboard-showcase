"""Typed lab settings passed to ``run()``, plus the helpers strategies use to read them.

A strategy receives ``settings`` as a ``list[SettingRow]`` mirroring the
``research_settings`` table: each row keeps its ``scope``, ``key``, ``value``,
and ``value_type``. Keeping the rows typed (rather than flattening to a dict) is
what lets ``clone_with_zero_slippage`` select slippage rows structurally — by
``scope`` and ``value_type`` — instead of fragile string-matching on dotted key
names (M3 spec §3.5).

Pure Python (stdlib only): importing this module never pulls in pandas or vbt.
"""
from __future__ import annotations  # lazy annotations, consistent with the rest of base/

from dataclasses import dataclass, replace  # SettingRow is a dataclass; replace() copies one field
from typing import Any  # a setting value can be number / string / bool / parsed json

# Sentinel so get_setting can tell "caller passed no default" from "default is None".
_NO_DEFAULT = object()  # private marker; never leaks to callers


@dataclass(frozen=True)  # a setting row is immutable
class SettingRow:
    """One lab setting, shaped like a research_settings row."""

    scope: str  # e.g. 'fees', 'slippage', 'paths'
    key: str  # e.g. 'crypto.default'
    value: Any  # the typed Python value (number rows carry a float, etc.)
    value_type: str  # 'number' | 'string' | 'boolean' | 'json' (mirrors the DB column)


def get_setting(settings: list[SettingRow], scope: str, key: str, default: Any = _NO_DEFAULT) -> Any:
    """Return the value of the (scope, key) setting; raise KeyError if absent and no default given."""
    for row in settings:  # linear scan: settings lists are tiny (single digits in M3)
        if row.scope == scope and row.key == key:  # match on the composite key
            return row.value  # found: hand back the typed value
    if default is _NO_DEFAULT:  # caller did not supply a default → this is a hard error
        raise KeyError(f"setting not found: scope={scope!r} key={key!r}")  # no silent fallback
    return default  # caller explicitly opted into a default


def clone_with_zero_slippage(settings: list[SettingRow]) -> list[SettingRow]:
    """Return a copy of settings with every numeric slippage row's value set to 0.

    The zero-slippage shadow run for the two-pass slippage-cost diff (§3.5). The
    selection rule is structural: scope == 'slippage' AND value_type == 'number'.
    Non-numeric slippage rows (e.g. a future string slippage 'model') are left as-is.
    """
    cloned: list[SettingRow] = []  # build a fresh list; never mutate the caller's rows
    for row in settings:  # walk every row once
        if row.scope == "slippage" and row.value_type == "number":  # numeric slippage row
            cloned.append(replace(row, value=0.0))  # copy the row with the value zeroed
        else:  # any other row
            cloned.append(row)  # carried through unchanged (rows are frozen, safe to share)
    return cloned  # the shadow settings
