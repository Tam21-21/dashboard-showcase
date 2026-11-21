"""Backend probes for the look-ahead guardrail (docs/briefs/lookahead_guardrail_spec_brief.md,
section 6 / AC-3, AC-4).

DECISIONS #17 splits backend verification in two: domain logic gets "a local probe in plain python3
against a throwaway SQLite DB, no FastAPI, no new laptop dependencies", while the HTTP route and boot
layer gets "a committed FastAPI TestClient probe that runs on the server venv... over SSH". This file
carries the probes registered in `_PROBES` at the bottom of this file, spanning both categories,
named per probe below.

WHY THIS FILE DOES NOT IMPORT PYTEST, unlike its sibling test_catalog_vbt_indicators.py. The laptop's
system python3 has no pytest module (checked 2026-08-31: `python3 -m pytest --version` reports "No
module named pytest"), and installing one is the exact mistake this file exists to undo: an earlier
draft only ran because pytest, fastapi, bleach and httpx were installed into
/home/tamer/nautilus-probe/.venv, an unrelated NautilusTrader probe environment, which made the suite
reproducible only inside that one contaminated virtual environment. Plain assert statements plus a
small `_raises` helper do the same job with zero third-party imports, so every probe in this file
(the `catalog` import itself is stdlib-plus-config only, per its own `noqa` comment below) runs under
ANY CPython 3 interpreter with nothing installed beyond the standard library. Each `test_*` function
still takes no arguments and uses no fixture, so a future clean pytest install could collect and run
them unchanged; that is a bonus, not the load-bearing path. The load-bearing path is `main()` at the
bottom, invoked by running this file as a script.

FUNCTION NAMES this file assumes on catalog.py: parse_lookahead_indicators(path=...) and
load_lookahead_indicators(), the second one explicitly named by the brief's own text ("a
load_lookahead_indicators() call in the boot lifespan beside catalog.load_vbt_indicators()"). The
first is derived by strict analogy to the file's own existing pattern
(parse_vbt_indicators_reference/load_vbt_indicators, parse_indicators_reference/load_indicators),
which the brief itself invokes: "gains a parse and cache for the new file in the same fail-loud
family as the two it already has." The two exception class names, LookaheadReferenceMissing and
LookaheadReferenceError, are quoted verbatim from the brief's own layer-2 text, not invented here.
This file's own probe count is the length of the `_PROBES` tuple at the bottom, not a number stated
in prose anywhere in this docstring: an earlier draft stated the count in three separate places and
one of those three had already gone stale before a findings dispatch added a seventh probe, which is
why every count in this file now either reads `_PROBES` directly or was rewritten to avoid stating a
number that could drift out from under it (findings dispatch, 2026-08-31 night campaign).

WHY PROBE 6 (test_main_lifespan_calls_load_lookahead_indicators) IS A SOURCE-LEVEL PROOF, NOT A BOOT
TEST. dashboard/backend/main.py imports FastAPI plus a dozen sibling modules (routes, sse,
calendar_sync, eventsdb, opsdb, userdb, researchdb, strategy_mirror, events.*), none of which are
installed on this laptop outside a contaminated venv, so a probe that imports dashboard.backend.main
cannot execute here at all (checked 2026-08-31: main.py's own import block names fastapi directly).
Under DECISIONS #17 this probe's real subject, the boot lifespan, is ROUTE and BOOT layer, and its
real proof is the FastAPI TestClient run on the server venv over SSH; running anything against the
server is out of scope for this dispatch. So locally this probe reads dashboard/backend/main.py as
TEXT, parses it with the stdlib `ast` module (no import of the module itself, so no FastAPI dependency
at all), isolates the exact source of the function named `lifespan`, and asserts the loader call
appears INSIDE that function's own source, not merely somewhere in the file. This is a static proof
that the call site exists in the right function; it is not a dynamic proof that lifespan runs it
successfully at boot. That dynamic proof is owed to the deploy gate under DECISIONS #17's split, and
is not this probe's job.
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

_THIS = Path(__file__).resolve()  # .../dashboard/backend/tests/test_lookahead_guardrail.py
_REPO_ROOT = _THIS.parents[3]  # .../trading (repo root); same math as tests/conftest.py's own comment
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))  # self-contained: this file runs standalone, not only via pytest

from dashboard.backend import catalog  # noqa: E402 -- stdlib + config only, no vbt, no pandas, no fastapi

_MAIN_PY = _REPO_ROOT / "dashboard" / "backend" / "main.py"

NINE = {"BOLB", "FIXLB", "FMAX", "FMEAN", "FMIN", "FSTD", "MEANLB", "PIVOTLB", "TRENDLB"}


def _raises(exc_type, fn, *args, **kwargs) -> bool:
    """True iff calling fn(*args, **kwargs) raises exactly exc_type (or a subclass)."""
    try:
        fn(*args, **kwargs)
    except exc_type:
        return True
    return False


def test_real_committed_authority_and_payload_flags_counted() -> None:
    """REAL ARTIFACT: the payload's look_ahead is true for exactly the nine, false for the other 27,
    counted (len()), not sampled, and derived from the authority list rather than description text.

    Calls load_vbt_indicators() directly rather than build_catalog(): build_catalog() also opens a
    live research.db connection (researchdb.get_conn(), for the unrelated `settings` field) which
    this probe has no need to depend on, and build_catalog()'s own "vbt_indicators" key is exactly
    load_vbt_indicators()'s return value with no further transformation
    (`"vbt_indicators": load_vbt_indicators()` in catalog.py), so testing the smaller function proves
    the same payload content without the database dependency.
    """
    catalog._reset_vbt_indicators_cache()
    parsed = catalog.load_vbt_indicators()
    indicators = parsed["indicators"]
    assert len(indicators) == 36  # the full committed VBT-native suite

    flagged = [i["name"] for i in indicators if i["look_ahead"] is True]
    unflagged = [i["name"] for i in indicators if i["look_ahead"] is False]
    assert len(flagged) == 9
    assert len(unflagged) == 27
    assert set(flagged) == NINE
    # PIVOTLB in particular: flagged despite its own description never saying "look-ahead" (AC-2's
    # gap is exactly why this must be derived from the authority list, never the description text).
    assert "PIVOTLB" in flagged


def test_parser_raises_on_missing_file() -> None:
    with tempfile.TemporaryDirectory(prefix="lookahead_missing_") as d:
        missing = Path(d) / "does_not_exist.md"
        assert _raises(catalog.LookaheadReferenceMissing, catalog.parse_lookahead_indicators, missing), \
            "parse_lookahead_indicators on an absent path did not raise LookaheadReferenceMissing"


def test_parser_raises_on_empty_file() -> None:
    """Present, readable, zero rows: LookaheadReferenceMissing, never LookaheadReferenceError, so
    the two stay distinguishable in a log (brief's own words)."""
    with tempfile.TemporaryDirectory(prefix="lookahead_empty_") as d:
        fixture = Path(d) / "lookahead_indicators.md"
        fixture.write_text("# Look-ahead indicators\n\nHeader prose only, no rows.\n", encoding="utf-8")
        assert _raises(catalog.LookaheadReferenceMissing, catalog.parse_lookahead_indicators, fixture), \
            "parse_lookahead_indicators on a zero-row file did not raise LookaheadReferenceMissing"


def test_parser_raises_on_malformed_file_no_row_skipped() -> None:
    """A block missing its `**Evidence:**` line raises LookaheadReferenceError rather than silently
    returning a partial set with that row skipped (layer 2: "No row is ever skipped.")."""
    with tempfile.TemporaryDirectory(prefix="lookahead_malformed_") as d:
        fixture = Path(d) / "lookahead_indicators.md"
        fixture.write_text(
            "# Look-ahead indicators\n\n"
            "## FOOLB\n"
            "**Reads:** A fixture-only reason.\n",  # Evidence line missing entirely
            encoding="utf-8",
        )
        assert _raises(catalog.LookaheadReferenceError, catalog.parse_lookahead_indicators, fixture), \
            "parse_lookahead_indicators on a block missing **Evidence:** did not raise " \
            "LookaheadReferenceError"


def test_parser_raises_on_malformed_file_multiword_heading() -> None:
    """DECLARED REGRESSION GUARD (findings dispatch, 2026-08-31 night campaign, added by the
    probe-writer, not a new-defect probe). ADD ONLY: this function is new; nothing above it in this
    file was changed to add it.

    parse_lookahead_indicators's shape guard `if not name or re.search(r"\\s", name)` does two jobs:
    reject an EMPTY heading and reject a MULTI-WORD one, both raising the same LookaheadReferenceError.
    The matching guard in the shell parser (scripts/check_lookahead_flags.sh) was found silently
    broken on its EMPTY half tonight while this file's Python guard stayed correct; a shell regression
    guard already covers the shell parser's MULTI-WORD half
    (scripts/probes/lookahead_guardrail/code_malformed_authority_file_multiword_heading.probe.sh).
    This case is the matching guard for THIS parser's multi-word half, which had no probe before this
    dispatch. It is already correct at this unit's base commit and is proven to PASS here, not fail or
    drift; its job is to make a future weakening of this branch visible, the same way nothing caught
    the shell bug until a judgment-class read noticed it. Proven apart from a fail-at-base run by a
    guard-removal experiment recorded in the probe-writer's report (weaken the condition to
    `if not name:`, confirm this case alone goes red, discard the edit).

    PINNED TO THE MULTI-WORD BRANCH, not to malformedness in general: the fixture below stages exactly
    one defective block, well-formed apart from its own two-word heading ("PROBE FIXTURE"), so the
    raised error's own message is asserted to carry that literal two-word text. catalog.py builds the
    message as `f"...(raw heading text: {heading!r})"`, where `heading` is the block's raw, unstripped
    first line; a multi-word heading's message therefore contains `'PROBE FIXTURE'` (the two words,
    space intact), while an empty heading's message would instead contain `''` and never those two
    words. So this assertion proves the MULTI-WORD branch fired specifically, not merely that some
    malformed condition fired somewhere in the block.
    """
    with tempfile.TemporaryDirectory(prefix="lookahead_multiword_") as d:
        fixture = Path(d) / "lookahead_indicators.md"
        fixture.write_text(
            "# Look-ahead indicators\n\n"
            "## PROBE FIXTURE\n"
            "**Reads:** Fixture-only row whose heading carries two words, staged to prove the "
            "multi-word half of the shape guard trips on its own, separately from the empty-heading "
            "half.\n"
            "**Evidence:** This block is well-formed apart from its two-word heading, so if the "
            "malformed report fires for any other reason this probe is proving the wrong thing.\n",
            encoding="utf-8",
        )
        message = ""
        raised_reference_error = False
        try:
            catalog.parse_lookahead_indicators(fixture)
        except catalog.LookaheadReferenceError as exc:
            raised_reference_error = True
            message = str(exc)
        assert raised_reference_error, (
            "parse_lookahead_indicators on a two-word heading did not raise LookaheadReferenceError"
        )
        assert "PROBE FIXTURE" in message, (
            "LookaheadReferenceError's own message did not name the literal two-word heading text "
            f"'PROBE FIXTURE'; it read: {message!r}. This is what proves the MULTI-WORD branch of "
            "the shape guard fired, rather than the empty-heading branch or some other malformed "
            "condition."
        )


def test_load_lookahead_indicators_caches_and_reset_clears() -> None:
    """AC-3's own words: catalog.py "parses the file, caches it". Mirrors the existing
    test_load_caches_and_reset_clears convention for load_vbt_indicators."""
    catalog._reset_lookahead_indicators_cache()
    first = catalog.load_lookahead_indicators()
    second = catalog.load_lookahead_indicators()
    assert first is second
    catalog._reset_lookahead_indicators_cache()
    third = catalog.load_lookahead_indicators()
    assert third is not first


def test_main_lifespan_calls_load_lookahead_indicators() -> None:
    """AC-3: "The boot lifespan calls it, so any of the three stops startup." SOURCE-LEVEL proof only:
    see the module docstring for why this reads main.py as text instead of importing it, and why the
    dynamic boot proof stays owed to the deploy gate under DECISIONS #17."""
    source = _MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_MAIN_PY))
    lifespan_node = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "lifespan":
            lifespan_node = node
            break
    assert lifespan_node is not None, "dashboard/backend/main.py defines no function named 'lifespan'"

    body_source = ast.get_source_segment(source, lifespan_node)
    assert body_source is not None, "could not isolate lifespan's own source segment"
    assert "load_lookahead_indicators" in body_source, (
        "load_lookahead_indicators() is not called inside the lifespan function body; a call "
        "elsewhere in main.py would not satisfy AC-3's 'the boot lifespan calls it'"
    )


_PROBES = (
    test_real_committed_authority_and_payload_flags_counted,
    test_parser_raises_on_missing_file,
    test_parser_raises_on_empty_file,
    test_parser_raises_on_malformed_file_no_row_skipped,
    test_load_lookahead_indicators_caches_and_reset_clears,
    test_main_lifespan_calls_load_lookahead_indicators,
    test_parser_raises_on_malformed_file_multiword_heading,
)


def main() -> int:
    """Run every probe registered in `_PROBES` above, independently (one probe's failure never hides
    another's result), print a PASS/FAIL line per probe, and exit 1 if any failed."""
    failed = 0
    for fn in _PROBES:
        try:
            fn()
        except Exception as exc:  # a probe reports FAIL for ANY exception, not only AssertionError
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    total = len(_PROBES)
    print(f"\n{total - failed}/{total} passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
