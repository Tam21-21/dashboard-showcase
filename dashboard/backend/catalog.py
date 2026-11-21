"""The strategy reference catalog (M4 R13/R14, §6 / D4): a virtual view computed on read.

Everything here is introspected/parsed from the LIVE modules, registries, and committed reference file
on each open — never a stored snapshot (AD7, canon `:888`). So the catalog cannot drift from the code:
add a base symbol or a helper and it appears; edit a setting and the row updates; the indicator file is
the build dependency the parse reads. The base-surface introspection imports ONLY `strategies.base`
(pure Python, no vbt) and `strategies.helpers`; `vbt_extraction` is never imported here, so its exclusion
from the catalog is structural, not a runtime guard to remember (D4).
"""
from __future__ import annotations  # lazy annotations

import importlib  # import the contract packages by name for introspection
import inspect  # class/function names, signatures, docstrings
import json  # parse the R16 template manifests (data-only, no exec — the sandbox boundary)
import re  # the heading-structural indicator parse
import sys  # put research_lab on the path so strategies.base/helpers import
from pathlib import Path  # the indicator reference path

from . import config  # PROJECT_ROOT, the repo root the reference file hangs off

# The committed indicator reference (R14): its repo home (requirements R14). A build dependency, not
# optional — the parse fails loud if it is absent (D4), surfaced at process start by load_indicators().
_INDICATORS_PATH = config.PROJECT_ROOT / "docs" / "reference" / "indicators_reference.md"

# The committed VBT-native indicator reference (TERM F3): a sibling of the house file above, generated
# offline by research_lab/tools/generate_vbt_indicator_reference.py from the pinned VBT build and parsed
# here at boot. The backend still never imports VBT (AD-F3.1) — it only reads this static file.
_VBT_INDICATORS_PATH = config.PROJECT_ROOT / "docs" / "reference" / "vbt_indicators_reference.md"

# The look-ahead guardrail's authority list (docs/briefs/lookahead_guardrail_spec_brief.md section 4,
# layer 1): the ONE committed, hand-authored file naming every future-reading VBT indicator. Every
# `look_ahead` value in the payload below is derived from this file, never from description text.
_LOOKAHEAD_INDICATORS_PATH = config.PROJECT_ROOT / "docs" / "reference" / "lookahead_indicators.md"

# The committed R16 strategy templates (M4.5 3b B4.1): each is a `<id>.json` manifest (metadata + the
# parameter form schema) plus a sibling `<id>.py.tmpl` code skeleton with `{{token}}` placeholders. Parsed
# at boot like the indicator references (the R14 committed-artifact model); served by build_catalog so the
# frontend renders the skeleton off the served manifest (NFR-4, no per-keystroke backend round-trip).
_TEMPLATES_DIR = config.PROJECT_ROOT / "research_lab" / "strategies" / "templates"

# The committed WQA101 alpha reference (U.4, lane GP, criterion 6): a sibling of the VBT-native
# reference above, generated offline by research_lab/tools/generate_alpha_reference.py from the
# pinned build's real vbt.indicators.expr.wqa101_expr_config and parsed here at boot. Same
# never-import-vbt discipline as the VBT reference: this module only reads the static file.
_ALPHA_REFERENCE_PATH = config.PROJECT_ROOT / "docs" / "reference" / "wqa101_alphas_reference.md"


class IndicatorReferenceMissing(FileNotFoundError):
    """The committed indicator reference file is absent — a missing build dependency (M4 R14/D4)."""


class VbtIndicatorReferenceMissing(FileNotFoundError):
    """The generated VBT indicator reference is absent — a missing build dependency (TERM F3/AD-F3.2).

    Same class of failure as IndicatorReferenceMissing: the file is generated and committed, so its
    absence is a build error caught at boot, not an empty catalog. Restore it by re-running the generator.
    """


class LookaheadReferenceMissing(FileNotFoundError):
    """The look-ahead authority list is absent, or present but carries zero rows.

    Folded into one class on purpose, mirroring IndicatorReferenceMissing above: the brief's own words
    are "we know nine exist; a file that says none do is a corrupted file, not good news," so an empty
    file is exactly as loud a failure as a missing one (docs/briefs/lookahead_guardrail_spec_brief.md
    section 4, layer 1).
    """


class LookaheadReferenceError(RuntimeError):
    """The look-ahead authority list exists and is non-empty but breaks its strict row format.

    Raised on a repeated name, a heading carrying anything but one name, a missing **Reads:** or
    **Evidence:** field line, a field out of order, or either field's value being empty after
    trimming. No row is ever silently skipped: unlike parse_vbt_indicators_reference's within-block
    leniency (safe there only because that file is machine-written), this file is hand-authored, so a
    broken row must stop the parse rather than surface as a quietly shortened flagged set.
    """


class AlphaReferenceMissing(FileNotFoundError):
    """The generated WQA101 alpha reference is absent, a missing build dependency (U.4 criterion 6).

    Same class of failure as VbtIndicatorReferenceMissing: the file is generated and committed, so
    its absence is a build error caught at boot, not an empty alpha list. Restore it by re-running
    research_lab/tools/generate_alpha_reference.py inside the pinned research-runner image.
    """


class StrategyTemplateError(RuntimeError):
    """An R16 template manifest is malformed or missing its skeleton — a build dependency error (3b B4.1).

    Same fail-loud class as the indicator references: the templates are committed artifacts, so a manifest
    with no sibling skeleton (or a manifest missing a required field) is a build error caught at boot.
    """


def _ensure_strategies_on_path() -> None:
    """Put `research_lab/` on `sys.path` so `import strategies.base`/`strategies.helpers` resolve.

    The contract packages live under `research_lab/strategies/`; the dashboard process does not normally
    import strategy code (the AST/no-exec sandbox boundary). The catalog is the ONE consumer that
    introspects the contract surface, and the base + helpers packages are pure Python (no vbt/pandas at
    import, verified), so importing them in the long-running process is safe and cheap.
    """
    rl = str(config.PROJECT_ROOT / "research_lab")  # the package root holding `strategies/`
    if rl not in sys.path:  # idempotent: only inserted once per process
        sys.path.insert(0, rl)


def introspect_contract_package(module_name: str) -> list[dict]:
    """Introspect a contract package's `__all__`: name, kind, signature, docstring (D4 base surface).

    Used for both `strategies.base` (the locked contract surface) and `strategies.helpers` (R15). Reads
    the explicit `__all__` so only the public surface is shown. `vbt_extraction` is not a member of
    `strategies.base.__all__`, so it is excluded by construction (the structural exclusion D4 requires).
    """
    _ensure_strategies_on_path()  # make the package importable in this process
    mod = importlib.import_module(module_name)  # strategies.base or strategies.helpers
    out: list[dict] = []
    for name in getattr(mod, "__all__", []):  # the explicit public surface only
        obj = getattr(mod, name)
        if inspect.isclass(obj):  # a contract class (Strategy, EventStrategy, RunResult, ...)
            kind = "class"
        elif inspect.isfunction(obj):  # a helper function (get_setting, default_costs, ...)
            kind = "function"
        else:  # a typed alias or other exported value
            kind = type(obj).__name__
        try:
            signature = str(inspect.signature(obj))  # the call/constructor signature
        except (ValueError, TypeError):  # some builtins/aliases have no signature — show none, never guess
            signature = None
        out.append({
            "name": name,  # the exported symbol name
            "kind": kind,  # class | function | <type>
            "signature": signature,  # the signature string, or None when unavailable
            "doc": inspect.getdoc(obj),  # the docstring (the human-readable contract)
        })
    return out


def _parse_bullets(block: str) -> list[str]:
    """The `- ...` bullet lines of a section, in order (the honest-assessment list, D4)."""
    return [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ")]


def _parse_param_bullets(block: str) -> dict[str, str]:
    """The `- Key: value` bullets of `## Default parameters` into an ordered {key: value} map (D4).

    A bullet with no colon (rare) maps its whole text to an empty string, so nothing is silently
    dropped. The value keeps any inner colons (e.g. "ON, threshold -0.1").
    """
    params: dict[str, str] = {}
    for ln in block.splitlines():
        if not ln.startswith("- "):
            continue
        body = ln[2:].strip()
        key, sep, value = body.partition(":")  # split on the FIRST colon only
        params[key.strip()] = value.strip() if sep else ""  # no-colon bullet -> empty value, key kept
    return params


def _section(block_text: str, heading: str) -> str:
    """The text under a `## <heading>` up to the next `## ` (or end), within one indicator block."""
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", block_text, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_indicators_reference(path: Path = _INDICATORS_PATH) -> dict:
    """Parse the indicator reference into catalog entries against its real heading structure (R14/D4).

    Heading-structural (engine-neutral; the formulas are descriptive Pine, not vbt). Splits on
    `^# N. <name>` to get the five indicator blocks; per block extracts the one-line summary, the
    `## Default parameters` {key: value} map, the `## Honest assessment` bullet list (the critical
    honesty the catalog must surface), and the `## Source code` pine block verbatim. The front-matter
    correlation map + the `# Confluence design notes` tail parse into one catalog-level confluence
    section. Raises IndicatorReferenceMissing if the file is absent (a missing build dependency).
    """
    if not path.exists():  # a missing committed reference is a build error, not an empty catalog
        raise IndicatorReferenceMissing(
            f"indicator reference not found at {path}; it is a committed build dependency the catalog "
            f"parses (M4 R14). Restore docs/reference/indicators_reference.md."
        )
    text = path.read_text(encoding="utf-8")

    # Split into the per-indicator blocks (`# N. name`) and the trailing confluence notes (`# Confluence`).
    # The leading split chunk (front matter before `# 1.`) is dropped — the catalog surfaces the named
    # blocks + the confluence tail, not the doc's intro prose.
    parts = re.split(r"^# (?=\d+\.\s|Confluence design notes)", text, flags=re.M)
    indicators: list[dict] = []
    confluence: dict[str, str] = {}
    for part in parts:
        if re.match(r"^\d+\.\s", part):  # an indicator block: "N. <name>\n..."
            heading, _, body = part.partition("\n")
            name = heading.split(".", 1)[1].strip()  # drop the leading "N." -> the indicator name
            one_liner = ""
            ml = re.search(r"^\*\*What it does in one line:\*\*\s*(.+)$", body, re.M)
            if ml:
                one_liner = ml.group(1).strip()
            pine = ""
            pm = re.search(r"## Source code\s*```pine\s*(.*?)```", body, re.S)
            if pm:
                pine = pm.group(1).strip("\n")
            # The catalog surfaces exactly the brief-specified per-indicator sections (D4: name, the
            # one-line summary, Default parameters, Honest assessment, Source code). Any other section a
            # single indicator happens to carry (e.g. ATP MACD's `## Preset values` named-preset table) is
            # DELIBERATELY not surfaced here — the catalog shows the common contract, not every bespoke
            # section; the full file remains the source for those. This is an explicit exclusion, not a
            # silent drop: widen the section set here if a bespoke section becomes catalog-worthy.
            indicators.append({
                "name": name,  # the indicator name (from the `# N. <name>` heading)
                "summary": one_liner,  # the `**What it does in one line:**` value
                "default_parameters": _parse_param_bullets(_section(body, "Default parameters")),
                "honest_assessment": _parse_bullets(_section(body, "Honest assessment")),  # the critical honesty
                "pine_source": pine,  # the `## Source code` pine block, verbatim
            })
        elif part.startswith("Confluence design notes"):  # the catalog-level confluence tail
            for sub in ("What each indicator can be used as", "Suggested confluence groups",
                        "Entry timing window", "What we have NOT built yet"):
                confluence[sub] = _section(part, sub)
    return {"indicators": indicators, "confluence": confluence}


# Parse once at process start (the file is immutable between deploys, AD7) and cache; fail loud here so a
# missing reference is caught at boot, not on the first catalog open.
_INDICATORS_CACHE: dict | None = None


def load_indicators() -> dict:
    """Return the parsed indicator reference, parsing + caching on first call (R14, AD7 process cache).

    Populated once at boot (main.py lifespan) so a missing file fails startup, then read-only. A test that
    needs to exercise a different file (e.g. the IndicatorReferenceMissing path) calls
    parse_indicators_reference(path=...) directly, or _reset_indicators_cache() to clear this singleton.
    """
    global _INDICATORS_CACHE
    if _INDICATORS_CACHE is None:
        _INDICATORS_CACHE = parse_indicators_reference()
    return _INDICATORS_CACHE


def _reset_indicators_cache() -> None:
    """Clear the cached indicator parse (test-only: lets a test re-parse a different reference file)."""
    global _INDICATORS_CACHE
    _INDICATORS_CACHE = None


def _split_csv(value: str) -> list[str]:
    """Split a `**Inputs:**`/`**Outputs:**` comma list into clean tokens, dropping the `none` sentinel."""
    parts = [p.strip() for p in value.split(",")]  # split the comma list and trim each token
    return [p for p in parts if p and p.lower() != "none"]  # drop empties and the explicit "none" marker


def parse_vbt_indicators_reference(path: Path = _VBT_INDICATORS_PATH) -> dict:
    """Parse the generated VBT-native indicator reference into catalog entries (TERM F3/AD-F3.3).

    Heading-structural, the same family as the house parser: split on `^## ` to get one block per
    indicator, and read each `**Label:**` field by an anchored regex. The file is generated and
    committed (immutable between deploys, AD7), so within-block leniency is safe — a missing field
    yields an empty value, and only an absent FILE is a hard, fail-loud build error (caught at boot).
    """
    if not path.exists():  # a missing generated reference is a build error, not an empty catalog
        raise VbtIndicatorReferenceMissing(
            f"VBT indicator reference not found at {path}; it is a committed build dependency the catalog "
            f"parses (TERM F3). Regenerate it with research_lab/tools/generate_vbt_indicator_reference.py."
        )
    text = path.read_text(encoding="utf-8")

    # The VBT build the file was generated from (the `Generated from VBT <version>` header line). Empty if
    # the header is missing — the file is generated so it is present, but never crash on a missing header.
    vm = re.search(r"^Generated from VBT (\S+)", text, re.M)
    version = vm.group(1) if vm else ""

    # Split into per-indicator blocks on the `## NAME` headings; the leading chunk (the title + header
    # before the first `## `) is dropped — it carries no indicator.
    blocks = re.split(r"^## ", text, flags=re.M)[1:]
    indicators: list[dict] = []
    for block in blocks:
        heading, _, body = block.partition("\n")  # the first line is the indicator name
        name = heading.strip()
        if not name:  # a malformed empty heading — skip it rather than emit a nameless entry
            continue

        def _field(label: str) -> str:
            """The text after a `**<label>:**` line within this block, or empty when absent (lenient)."""
            m = re.search(rf"^\*\*{re.escape(label)}:\*\*\s*(.*)$", body, re.M)
            return m.group(1).strip() if m else ""

        # The `**Parameters:**` line is `key=value, ...` or the literal `none`; parse to an ordered dict.
        params: dict[str, str] = {}
        params_line = _field("Parameters")
        if params_line and params_line.lower() != "none":  # "none" / empty -> no parameters
            for token in params_line.split(","):  # each "key=value" token
                key, sep, val = token.partition("=")  # split on the FIRST '=' only
                key = key.strip()
                if key:  # keep any token that carries a key; a value-less key maps to "" (never dropped)
                    params[key] = val.strip() if sep else ""

        indicators.append({
            "name": name,  # the indicator name (from the `## <name>` heading)
            "description": _field("What it does"),  # the human-readable description (no robot dumps)
            "inputs": _split_csv(_field("Inputs")),  # the input price series
            "parameters": params,  # the {key: default} map
            "outputs": _split_csv(_field("Outputs")),  # the output series
            # AC-4: derived from the authority list only, NEVER from the description text above (the
            # exact gap PIVOTLB's own row exists to close: its description never says "look-ahead").
            "look_ahead": name in load_lookahead_indicators(),
        })
    return {"version": version, "indicators": indicators}


# Parse once at process start (the file is immutable between deploys, AD7) and cache; fail loud here so a
# missing reference is caught at boot, not on the first catalog open. Mirrors the house-file cache above.
_VBT_INDICATORS_CACHE: dict | None = None


def load_vbt_indicators() -> dict:
    """Return the parsed VBT indicator reference, parsing + caching on first call (TERM F3, AD7 cache).

    Populated once at boot (main.py lifespan) so a missing file fails startup, then read-only. A test that
    needs a different file calls parse_vbt_indicators_reference(path=...) directly, or
    _reset_vbt_indicators_cache() to clear this singleton.
    """
    global _VBT_INDICATORS_CACHE
    if _VBT_INDICATORS_CACHE is None:
        _VBT_INDICATORS_CACHE = parse_vbt_indicators_reference()
    return _VBT_INDICATORS_CACHE


def _reset_vbt_indicators_cache() -> None:
    """Clear the cached VBT indicator parse (test-only: lets a test re-parse a different reference file)."""
    global _VBT_INDICATORS_CACHE
    _VBT_INDICATORS_CACHE = None


def parse_alpha_reference(path: Path = _ALPHA_REFERENCE_PATH) -> dict:
    """Parse the generated WQA101 alpha reference into catalog entries (U.4 criterion 6).

    Heading-structural, the same family as parse_vbt_indicators_reference above: split on `^## `
    to get one block per alpha, read the single `**Expression:**` field verbatim. The file is
    generated and committed (immutable between deploys), so a missing FIELD within a block is
    lenient (an empty string, never a crash); only an absent FILE is a hard, fail-loud build error.
    """
    if not path.exists():  # a missing generated reference is a build error, not an empty list
        raise AlphaReferenceMissing(
            f"WQA101 alpha reference not found at {path}; it is a committed build dependency "
            f"(U.4 criterion 6). Regenerate it with research_lab/tools/generate_alpha_reference.py."
        )
    text = path.read_text(encoding="utf-8")

    # The VBT build the file was generated from (the `Generated from VBT <version>` header line),
    # the same header shape parse_vbt_indicators_reference reads above.
    vm = re.search(r"^Generated from VBT (\S+)", text, re.M)
    version = vm.group(1) if vm else ""

    # Split into per-alpha blocks on the `## alpha_N` headings; the leading chunk (the title +
    # header before the first `## `) is dropped, it carries no alpha.
    blocks = re.split(r"^## ", text, flags=re.M)[1:]
    alphas: list[dict] = []
    for block in blocks:
        heading, _, body = block.partition("\n")  # the first line is the "alpha_N" name
        name = heading.strip()
        if not name:  # a malformed empty heading, skip it rather than emit a nameless entry
            continue
        m = re.search(r"^\*\*Expression:\*\*\s*(.*)$", body, re.M)
        alphas.append({
            "name": name,  # e.g. "alpha_1", from the `## alpha_N` heading
            "expression": m.group(1).strip() if m else "",  # the raw expression text, verbatim
        })
    return {"version": version, "alphas": alphas}


# Parse once at process start (the file is immutable between deploys) and cache; fail loud here so a
# missing reference is caught at boot. Mirrors the two indicator-reference caches above.
_ALPHA_REFERENCE_CACHE: dict | None = None


def load_alpha_reference() -> dict:
    """Return the parsed alpha reference, parsing + caching on first call (U.4 criterion 6).

    Populated once at boot (main.py lifespan) so a missing committed reference fails startup, then
    read-only. A test that needs a different file calls parse_alpha_reference(path=...) directly,
    or _reset_alpha_reference_cache() to clear this singleton.
    """
    global _ALPHA_REFERENCE_CACHE
    if _ALPHA_REFERENCE_CACHE is None:
        _ALPHA_REFERENCE_CACHE = parse_alpha_reference()
    return _ALPHA_REFERENCE_CACHE


def _reset_alpha_reference_cache() -> None:
    """Clear the cached alpha reference parse (test-only: lets a test re-parse a different file)."""
    global _ALPHA_REFERENCE_CACHE
    _ALPHA_REFERENCE_CACHE = None


def parse_lookahead_indicators(path: Path = _LOOKAHEAD_INDICATORS_PATH) -> dict[str, dict[str, str]]:
    """Parse the look-ahead authority list into {NAME: {"reads": ..., "evidence": ...}}, strictly.

    STRICT, unlike parse_vbt_indicators_reference's within-block leniency above: that file is
    machine-written, "so within-block leniency is safe" there. This file is hand-authored (brief
    section 4, layer 1), so a mistyped or missing field must raise rather than yield an empty reason.
    Absence and a present-but-zero-row file both raise LookaheadReferenceMissing ("Empty is not
    'nothing is look-ahead'."); any other broken shape raises LookaheadReferenceError, naming the
    offending block, and no row is ever skipped.
    """
    if not path.exists():  # a missing committed authority list is a build error, not an empty catalog
        raise LookaheadReferenceMissing(
            f"look-ahead authority list not found at {path}; it is a committed, hand-authored file "
            f"docs/briefs/lookahead_guardrail_spec_brief.md section 4 requires. Restore "
            f"docs/reference/lookahead_indicators.md."
        )
    text = path.read_text(encoding="utf-8")
    # Same heading-structural split idiom as parse_vbt_indicators_reference above: one block per "## " heading.
    blocks = re.split(r"^## ", text, flags=re.M)[1:]
    if not blocks:  # present, readable, zero rows: still corrupted, never "nothing is look-ahead"
        raise LookaheadReferenceMissing(
            f"look-ahead authority list at {path} carries zero rows; the nine known future-reading "
            f"indicators would all silently stop being flagged."
        )
    rows: dict[str, dict[str, str]] = {}
    for block in blocks:
        heading, _, body = block.partition("\n")  # the first line is the "NAME" half of "## NAME"
        name = heading.strip()
        if not name or re.search(r"\s", name):  # empty heading, or one carrying more than one name
            raise LookaheadReferenceError(
                f"look-ahead authority list at {path} carries a block with an empty or multi-word "
                f"heading (raw heading text: {heading!r})"
            )
        if name in rows:  # the name is the block's key; a repeat is malformed, never a merge of rows
            raise LookaheadReferenceError(
                f"look-ahead authority list at {path} repeats the name {name!r}; a name must be "
                f"unique across the file"
            )
        lines = [ln for ln in body.splitlines() if ln.strip()]  # this block's non-blank lines, in order
        if len(lines) != 2:  # exactly two field lines are required: **Reads:** then **Evidence:**
            raise LookaheadReferenceError(
                f"look-ahead authority list block {name!r} at {path} carries {len(lines)} non-blank "
                f"line(s); exactly two are required (**Reads:** then **Evidence:**)"
            )
        reads_match = re.match(r"^\*\*Reads:\*\*\s*(.*)$", lines[0])  # the first field, in order
        evidence_match = re.match(r"^\*\*Evidence:\*\*\s*(.*)$", lines[1])  # the second field, in order
        if not reads_match or not evidence_match:  # wrong label text or wrong order both land here
            raise LookaheadReferenceError(
                f"look-ahead authority list block {name!r} at {path} does not carry its two field "
                f"lines in the required order (**Reads:** then **Evidence:**)"
            )
        reads_value = reads_match.group(1).strip()
        evidence_value = evidence_match.group(1).strip()
        if not reads_value or not evidence_value:  # a present field with an empty value is still broken
            raise LookaheadReferenceError(
                f"look-ahead authority list block {name!r} at {path} carries an empty **Reads:** or "
                f"**Evidence:** value after trimming; every row needs a non-empty reason"
            )
        rows[name] = {"reads": reads_value, "evidence": evidence_value}  # a fully valid row: keep it
    return rows


# Parse once at process start (the file is committed and hand-ruled, immutable between deploys) and
# cache; fail loud here so a missing/empty/malformed authority list is caught at boot. Mirrors the two
# indicator-reference caches above.
_LOOKAHEAD_INDICATORS_CACHE: dict[str, dict[str, str]] | None = None


def load_lookahead_indicators() -> dict[str, dict[str, str]]:
    """Return the parsed look-ahead authority list, parsing + caching on first call (AC-3).

    Populated once at boot (main.py lifespan, beside load_vbt_indicators()) so a missing, empty or
    malformed authority list fails startup, then read-only. A test that needs a different file calls
    parse_lookahead_indicators(path=...) directly, or _reset_lookahead_indicators_cache() to clear
    this singleton.
    """
    global _LOOKAHEAD_INDICATORS_CACHE
    if _LOOKAHEAD_INDICATORS_CACHE is None:
        _LOOKAHEAD_INDICATORS_CACHE = parse_lookahead_indicators()
    return _LOOKAHEAD_INDICATORS_CACHE


def _reset_lookahead_indicators_cache() -> None:
    """Clear the cached look-ahead authority parse (test-only: lets a test re-parse a different file)."""
    global _LOOKAHEAD_INDICATORS_CACHE
    _LOOKAHEAD_INDICATORS_CACHE = None


_REQUIRED_TEMPLATE_FIELDS = ("id", "name", "kind", "order", "summary", "params")  # every manifest carries these


def parse_strategy_templates(templates_dir: Path = _TEMPLATES_DIR) -> list[dict]:
    """Parse the R16 template manifests into catalog entries (M4.5 3b B4.1).

    Each `<id>.json` holds the metadata + the parameter form schema; its sibling `<id>.py.tmpl` holds the
    code skeleton (read in as the `skeleton` string the frontend pure-render substitutes into). Manifests
    are committed artifacts: a missing directory, a manifest missing a required field, or a manifest with
    no sibling skeleton is a fail-loud build error (StrategyTemplateError), never a silently-empty gallery.
    The list is sorted by `order` so the gallery renders price templates first and the event template last
    (R-TMPL1). JSON-only (no exec): the manifests are data, never imported as code (the sandbox boundary).
    """
    if not templates_dir.is_dir():  # a missing committed templates directory is a build error
        raise StrategyTemplateError(
            f"strategy templates directory not found at {templates_dir}; the R16 templates are committed "
            f"build dependencies the catalog parses (M4.5 3b B4.1)."
        )
    templates: list[dict] = []
    for manifest_path in sorted(templates_dir.glob("*.json")):  # one manifest per template
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))  # data only, no exec
        except json.JSONDecodeError as exc:
            raise StrategyTemplateError(f"template manifest {manifest_path.name} is not valid JSON: {exc}") from exc
        missing = [f for f in _REQUIRED_TEMPLATE_FIELDS if f not in manifest]  # the required-field check
        if missing:
            raise StrategyTemplateError(
                f"template manifest {manifest_path.name} is missing required field(s): {missing}"
            )
        skeleton_path = templates_dir / f"{manifest['id']}.py.tmpl"  # the sibling code skeleton
        if not skeleton_path.exists():
            raise StrategyTemplateError(
                f"template {manifest['id']!r} has no skeleton at {skeleton_path.name}; every manifest needs "
                f"its sibling .py.tmpl (3b B4.1)."
            )
        manifest["skeleton"] = skeleton_path.read_text(encoding="utf-8")  # fold the skeleton text in
        templates.append(manifest)
    templates.sort(key=lambda t: t["order"])  # price templates first, event template last (R-TMPL1)
    return templates


# Parse once at process start (the manifests are immutable between deploys) and cache; fail loud here so a
# malformed/missing manifest is caught at boot. Mirrors the indicator-reference caches above.
_TEMPLATES_CACHE: list[dict] | None = None


def load_strategy_templates() -> list[dict]:
    """Return the parsed R16 templates, parsing + caching on first call (3b B4.1, the boot-parse model)."""
    global _TEMPLATES_CACHE
    if _TEMPLATES_CACHE is None:
        _TEMPLATES_CACHE = parse_strategy_templates()
    return _TEMPLATES_CACHE


def _reset_strategy_templates_cache() -> None:
    """Clear the cached template parse (test-only: lets a test re-parse a different templates directory)."""
    global _TEMPLATES_CACHE
    _TEMPLATES_CACHE = None


def build_catalog() -> dict:
    """Assemble the full strategy catalog from the live modules + registries + reference (R13/D4).

    Virtual view: every field is computed on this call. `settings` lists the live `research_settings`
    rows (scope, key, value, value_type) — the frontend reuses the provenance information-icon idiom to
    fetch each row's `parameter_provenance.md` entry lazily (GET /provenance/{scope.key}). `definitions`
    + `event_types` reuse the ANA definitions registry + the canonical event-type set (no new source).
    """
    from . import researchdb  # local import: avoid a module-load cycle (researchdb is heavy)
    from . import eventsdb  # the canonical event-type registry (EVENT_TYPES owner)
    from .events.fundamentals import definitions as fundamentals_definitions  # ANA's definitions registry

    with researchdb.get_conn() as conn:  # the live settings rows (read-only)
        settings = researchdb._live_settings(conn)

    return {
        "base_surface": introspect_contract_package("strategies.base"),  # the locked contract surface
        "helpers": introspect_contract_package("strategies.helpers"),    # R15 shared helpers
        "settings": settings,  # live research_settings; provenance fetched lazily by the ⓘ idiom
        "event_types": sorted(eventsdb.EVENT_TYPES),  # the registered canonical event types
        "definitions": fundamentals_definitions.definitions_payload(),  # fundamental concepts + ratios (ANA)
        "indicators": load_indicators(),  # the R14 parse (five indicators + the confluence tail)
        "vbt_indicators": load_vbt_indicators(),  # TERM F3: the full VBT-native suite (generated, parsed at boot)
        "wqa101_alphas": load_alpha_reference(),  # U.4 criterion 6: the 101 alphas (generated, parsed at boot)
        "templates": load_strategy_templates(),  # M4.5 R16: the visual templates (manifests + skeletons, parsed at boot)
    }
