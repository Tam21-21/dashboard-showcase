"""Proof that the provenance-parser extraction (OPS-1) changed nothing about the research
provenance check's behaviour, and that the boot fact it ships with (boot_provenance_check_ran)
genuinely reflects whether the check ran.

Companion in shape to test_catalog_vbt_indicators.py: one test against the real committed input,
one test against a small deterministic fixture for the failure path, one test mechanically pinning
the specific regression this extraction could introduce (a probe losing an attribute on
researchdb). See docs/handoffs/ops1_provenance_parser_extraction.md for the brief this proves and
docs/briefs/ops_build_brief.md section 12.1 for the two tests it mandates.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from dashboard.backend import researchdb  # noqa: E402 (conftest hoists the repo root)


def test_live_provenance_file_still_passes() -> None:
    """Brief section 12.1's first mandated test (acceptance criterion 4): the REAL committed
    provenance file still validates after the extraction. Not a fixture: researchdb.PROVENANCE_PATH
    defaults to the committed docs/research_lab/parameter_provenance.md, and this calls the check
    against exactly that file. A raise here IS the test failure; the function returns nothing on
    success, so "did not raise" is the whole assertion.
    """
    researchdb._check_parameter_provenance()  # must not raise


def test_missing_subsection_raises_the_same_error_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Brief section 12.1's second mandated test (acceptance criterion 5): a provenance entry
    missing one of the four required subsections still raises ProvenanceCheckError, naming the
    offending key and the missing subsection. The settings layout is monkeypatched to one throwaway
    key so the test does not have to replicate every real setting just to keep the
    layout-vs-documented comparison balanced; the parser actually under test
    (_parse_provenance_headings, now imported from provenance_parse.py rather than defined here) is
    the real one, never mocked.
    """
    fixture = tmp_path / "broken_provenance.md"  # a throwaway file, never the real committed one
    fixture.write_text(
        "## probe.extraction_test_key\n"
        "### Source\n"
        "A test-only key that exists only inside this test, never in the real settings layout.\n"
        "### Mechanism\n"
        "Fabricated to exercise the missing-subsection path; not derived from any real setting.\n"
        "### Sensitivity\n"
        "N/A: this key is never read outside this test.\n",
        # Deliberately no "### Bounds" section: this is the one gap the test proves is still caught.
        encoding="utf-8",
    )
    monkeypatch.setattr(researchdb, "PROVENANCE_PATH", fixture)  # point the check at the fixture
    monkeypatch.setattr(researchdb, "_layout_keys", lambda: {"probe.extraction_test_key"})  # the only key expected
    with pytest.raises(researchdb.ProvenanceCheckError) as exc_info:
        researchdb._check_parameter_provenance()
    message = str(exc_info.value)
    assert "probe.extraction_test_key" in message  # the offending key is still named
    assert "Bounds" in message  # the missing subsection is still named


def test_provenance_symbols_resolve_on_researchdb() -> None:
    """Acceptance criterion 6, and criterion 2 made mechanical: after the extraction, all three
    names still resolve as module attributes on researchdb (the exact shape eleven probe files
    across the repo depend on), and each one genuinely comes FROM provenance_parse rather than
    being silently redefined here, which would still pass a bare hasattr check but would reopen the
    duplication this extraction exists to remove. A future "tidy" to `from . import
    provenance_parse` plus qualified calls would delete these attributes, and this test fails
    before the next probe run does.
    """
    assert hasattr(researchdb, "_parse_provenance_headings")
    assert hasattr(researchdb, "_parse_provenance_bodies")
    assert hasattr(researchdb, "_REQUIRED_PROVENANCE_SECTIONS")
    assert researchdb._REQUIRED_PROVENANCE_SECTIONS == ("Source", "Mechanism", "Sensitivity", "Bounds")
    # __module__ proves the name is IMPORTED from provenance_parse, not a look-alike redefined here.
    assert researchdb._parse_provenance_headings.__module__ == "dashboard.backend.provenance_parse"
    assert researchdb._parse_provenance_bodies.__module__ == "dashboard.backend.provenance_parse"


def test_boot_fact_reflects_whether_init_db_actually_ran_the_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Beyond the eight numbered acceptance criteria: proving the ops routing law's new registry
    row (boot_provenance_check_ran) is a real signal rather than a decorative counter. The fact
    reads passed=False / checked_at=None until init_db has actually run the check, and
    passed=True with a fresh timestamp immediately after, against a real throwaway database (the
    same env-override shape the existing probes already use for a fresh init_db()).
    """
    monkeypatch.setattr(researchdb, "_provenance_check_passed_at", None)  # start from "never ran"
    before = researchdb.provenance_check_boot_fact()
    assert before == {"passed": False, "checked_at": None}
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")  # a throwaway DB, never the real one
    now = int(time.time())  # a floor: the recorded timestamp must be from this call, not a stale one
    researchdb.init_db()  # runs _check_parameter_provenance() against the real file, then records the fact
    after = researchdb.provenance_check_boot_fact()
    assert after["passed"] is True
    assert isinstance(after["checked_at"], int)
    assert after["checked_at"] >= now


def test_parse_provenance_bodies_actually_parses_the_real_file() -> None:
    """OPS-1 code-quality SF1: the OTHER moved function's BEHAVIOUR, which nothing exercised.

    `_parse_provenance_bodies` travelled with `_parse_provenance_headings` because they parse one
    grammar over one file, but the three tests above only reach `_parse_provenance_headings` (via
    `_check_parameter_provenance`) or check the extraction's SHAPE. This function has exactly one
    caller, `provenance_entry`, which backs the live provenance drawer -- so a silent behaviour
    change here renders every parameter's tooltip blank in production while every other test passes.

    The gate that found the gap also proposed the wrong implementation it admits: mistyping the H3
    branch as `line[3:]` instead of `line[4:]`. THAT ONE IS NOT A DEFECT, checked by running it:
    `'### Source'[3:].strip()` and `[4:].strip()` both yield `'Source'`, because the `.strip()`
    absorbs the extra space. The critic has no shell and reasoned the slice out instead of executing
    it. The gap it named is real even though its example was not, which is why this test exists and
    why the example is corrected here rather than repeated.

    A slice that IS a defect: `line[5:]`, which yields `'ource'`. Proven in a throwaway sandbox --
    injecting it fails THIS test and only this test, five passed one failed. Removing the boot
    fact's clear (the companion test below) likewise fails only its own. So both new tests are
    targeted rather than incidentally sensitive.
    """
    text = researchdb.PROVENANCE_PATH.read_text(encoding="utf-8")  # the real committed file
    entries = researchdb._parse_provenance_bodies(text)
    assert entries, "the real provenance file parsed to zero entries"
    # EVERY entry, not one anchor key. An earlier version of this test checked only
    # `fees.crypto.default`, which is the FIRST of the file's entries -- so the function's trailing
    # `flush()`, the only thing that commits the LAST entry's final subsection at end of file, was
    # exercised by nothing. Deleting that one line would have silently blanked the last parameter's
    # Bounds tooltip in production with all six tests green. Found by this unit's code-quality gate.
    # Iterating also removes the anchor-key-churn risk the single-key version carried, at no cost.
    # THE KEYS, cross-checked against the OTHER parser rather than against a hardcoded list. The
    # earlier version of this test asserted only on subsection SHAPE, so slicing the H2 key one
    # character short -- `line[4:]` instead of `line[3:]`, turning "fees.crypto.default" into
    # "ees.crypto.default" -- left every entry with four correctly-parsed subsections under a key
    # nothing looks up. `provenance_entry` does `.get(dotted_key)`, so every tooltip in production
    # would go blank while all six tests passed. `.strip()` cannot restore a dropped character.
    # `_parse_provenance_headings` is the oracle because it is separately and heavily exercised by
    # `_check_parameter_provenance`, which runs at boot against this same file.
    headings = researchdb._parse_provenance_headings(text)
    assert set(entries) == set(headings), (
        f"the two parsers disagree on the entry keys, so one of them is mis-slicing. "
        f"only in bodies: {sorted(set(entries) - set(headings))}; "
        f"only in headings: {sorted(set(headings) - set(entries))}"
    )
    for key, sections in entries.items():
        for name in researchdb._REQUIRED_PROVENANCE_SECTIONS:  # the same four the check requires
            assert name in sections, f"{key} lost its '{name}' subsection to the parser"
            body = sections[name]
            assert body, f"{key}'s '{name}' body came back empty"
            # Asserted on the RAW body, not on `body.strip()`. Re-stripping here would mean the test
            # normalises what it is meant to be checking the parser normalised, so dropping the
            # parser's own `.strip()` would be invisible to it.
            assert body == body.strip(), f"{key}'s '{name}' body is not stripped: {body!r}"
        # A mis-sliced prefix would produce names like '#Source' rather than 'Source'.
        assert not any(n.startswith("#") for n in sections), f"{key}: names carry '#': {sorted(sections)}"

        # AND each body must be scoped to its OWN subsection's lines. Delete the `buf = []` reset on
        # the parser's '### ' branch -- and only that branch -- and every subsection after the first
        # in an entry silently accumulates its predecessors' text: Mechanism becomes Source+Mechanism,
        # Bounds becomes all four. Measured against the real file: EVERY other assertion here still
        # passes, and the two newline checks pass MORE easily, because accumulation only ever adds
        # newlines and bullet markers, never removes them. In the drawer that renders the Bounds
        # tooltip with the Source, Mechanism and Sensitivity text glued underneath its heading --
        # wrong CONTENT, not merely wrong formatting, which is worse than the defect the newline
        # checks above were added to catch. Containment is the cheap detector: no subsection's body
        # is a substring of a sibling's unless one leaked into the other.
        pairs = list(sections.items())
        for i, (name_a, body_a) in enumerate(pairs):
            for name_b, body_b in pairs[i + 1:]:
                assert body_a not in body_b and body_b not in body_a, (
                    f"{key}: '{name_a}' and '{name_b}' share content, so a subsection body leaked "
                    f"across a buffer that was not reset between subsections"
                )

    # THE BODIES' INTERNAL LINE STRUCTURE, which every assertion above is blind to. Join the buffered
    # lines with " " instead of "\n" and each body is still non-empty, still correctly keyed, still
    # stripped at its edges -- so all six tests pass -- while every multi-line body collapses to one
    # line. That is not cosmetic: `ProvenanceInfo.tsx` renders these strings through CommonMark with
    # CSS targeting list output, and a "-" is only a list marker when it STARTS a line, so the
    # sensitivity sweeps stop being bullets and render as a run-on sentence with stray hyphens in the
    # middle of it. Measured, not argued: injecting the space-join turned a real body into
    # "... max drawdown 37.32%. - -50% (0.0005): to..." with all six tests green.
    #
    # Counted rather than anchored on a named key, because anchoring on one key is exactly what let
    # the previous two gaps through: the FIRST entry hid the missing tail flush, and the subsection
    # loop hid the mis-sliced key.
    multiline = [f"{k}.{n}" for k, v in entries.items() for n, body in v.items() if "\n" in body]
    assert multiline, (
        "not one parsed body contains a newline, so the parser is collapsing multi-line bodies onto "
        "one line. The provenance drawer renders these through CommonMark; a bullet list flattened "
        "this way becomes a run-on paragraph with stray hyphens."
    )
    # And specifically the shape the drawer depends on: a list marker anchored to the start of a line.
    bulleted = [k for k, v in entries.items() for body in v.values() if "\n- " in body]
    assert bulleted, "no body retains a newline-anchored list marker, so no bullet list can render"


def test_boot_fact_is_cleared_by_a_failing_check_not_left_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """OPS-1 code-quality SF2: the fact's FAILURE path, and the one-way latch it used to be.

    The test above proves the fact turns True after a passing check. Nothing proved what happens
    when the check FAILS -- and the original shape wrote on success only, so a second init_db() in
    the same process whose check failed would raise straight past the write and leave the FIRST
    run's `passed: True` standing. That is the precise false positive the monitor reading this fact
    exists to catch: it would report provenance as verified while the most recent verification
    failed. init_db is idempotent and its own comment says it can run more than once, so this is a
    reachable state rather than a theoretical one.

    The fix is that the fact is CLEARED before the check runs, so it means "the most recent check in
    this process passed" rather than "some check once passed". This test is what holds that shape:
    it starts from a fact that is already True, fails the check, and asserts the fact went back to
    False rather than staying stale.
    """
    monkeypatch.setattr(researchdb, "_provenance_check_passed_at", int(time.time()))  # a previous PASS
    assert researchdb.provenance_check_boot_fact()["passed"] is True  # the stale-True starting state

    fixture = tmp_path / "broken_provenance.md"  # a throwaway file, never the real committed one
    fixture.write_text(
        "## probe.second_run_failure_key\n"
        "### Source\n"
        "A test-only key, used to make the SECOND init_db call fail its provenance check.\n",
        # No Mechanism, Sensitivity or Bounds: three gaps, so the check cannot pass.
        encoding="utf-8",
    )
    monkeypatch.setattr(researchdb, "PROVENANCE_PATH", fixture)  # point the check at the broken file
    monkeypatch.setattr(researchdb, "_layout_keys", lambda: {"probe.second_run_failure_key"})
    monkeypatch.setattr(researchdb, "DB_PATH", tmp_path / "research.db")  # a throwaway DB, never the real one

    with pytest.raises(researchdb.ProvenanceCheckError):
        researchdb.init_db()  # must raise: the provenance file it now points at is broken

    after = researchdb.provenance_check_boot_fact()
    assert after == {"passed": False, "checked_at": None}, (
        f"a failing check left the boot fact stale: {after}. The monitor would report provenance "
        f"as verified when the most recent verification failed."
    )
