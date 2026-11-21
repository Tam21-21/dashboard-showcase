"""The generation loop (AD4.1's seven steps), run once per (skeleton, filler) pair drawn
from a loaded theory's two lists. Runs on the HOST: it needs the dashboard package for
the strategy write (upsert_strategy, create_only=True) and sqlite for the candidate-row
write, but no vbt at all (the compiled facts already came from the image, section 5.2).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from dashboard.backend import researchdb

from research_lab.generator import emit
from research_lab.generator.facts import Facts
from research_lab.generator.gate import check_allowlist, check_lookahead
from research_lab.generator.theory import Theory, TheoryLoadError


@dataclass(frozen=True)
class GenerationOutcome:
    """One (skeleton, filler) pair's result, reported back to the CLI/caller."""

    skeleton_id: str
    filler_id: str
    decision: str  # 'accepted' | 'refused' | 'skipped'
    reason: "str | None" = None  # refusal_reason, or None for accepted/skipped
    strategy_id: "int | None" = None
    strategy_name: "str | None" = None
    candidate_id: "int | None" = None
    # PREVIEW's own would-be list (UI plan 2.3) shows the expression text beside each pair's
    # decision; carried on every branch that actually evaluated the pair (never on a
    # 'not-selected' skip, which never even looks at the facts file for that pair).
    expression: "str | None" = None


def _find_existing_candidate(conn, theory_sha256: str, skeleton_id: str, filler_id: str, expr_sha: str):
    """AD4.1 step 6: read the identity index before any write. A hit means a rerun of the
    exact same theory bytes, skeleton and filler: the loop writes nothing at all."""
    row = conn.execute(
        "SELECT id FROM research_generated_candidates "
        "WHERE theory_sha256 = ? AND skeleton_id = ? AND filler_id = ? AND expression_sha256 = ?;",
        (theory_sha256, skeleton_id, filler_id, expr_sha),
    ).fetchone()
    return row["id"] if row is not None else None


def _write_refused_candidate(theory: Theory, skeleton_id: str, filler_id: str, expression: str,
                              expr_sha: str, reason: str, now: int) -> int:
    """A refused candidate row alone (AD4.1 step 7's refused branch): candidate write only,
    never a strategy write. Its own transaction (AD0: two writes that cannot compose into
    one nested transaction, the same reasoning that forces the accepted branch's split)."""
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO research_generated_candidates "
            "(theory_id, theory_sha256, skeleton_id, filler_id, expression, expression_sha256, "
            " decision, refusal_reason, strategy_id, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'refused', ?, NULL, ?);",
            (theory.theory_id, theory.theory_sha256, skeleton_id, filler_id, expression, expr_sha, reason, now),
        )
        return cur.lastrowid


def _write_accepted_candidate(theory: Theory, skeleton_id: str, filler_id: str, expression: str,
                               expr_sha: str, strategy_id: int, now: int) -> int:
    """The candidate half of the accepted branch (AD4.1 step 7), written SECOND, after the
    strategy write already returned its id (the CHECK on this table forbids the reverse
    order: 'accepted' requires a non-NULL strategy_id, which does not exist until then)."""
    with researchdb.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO research_generated_candidates "
            "(theory_id, theory_sha256, skeleton_id, filler_id, expression, expression_sha256, "
            " decision, refusal_reason, strategy_id, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'accepted', NULL, ?, ?);",
            (theory.theory_id, theory.theory_sha256, skeleton_id, filler_id, expression, expr_sha, strategy_id, now),
        )
        return cur.lastrowid


def _plain_pair_words(text: str) -> str:
    """A skeleton or filler id in plain words: '_' and ':' become spaces ('band_cap' to 'band cap')."""
    return " ".join(w for w in re.split(r"[_:]+", text) if w)


def strategy_display_name(theory: Theory, skeleton_id: str, filler_id: str) -> str:
    """DN1: the theory's '## Display name' text, ', ', then the skeleton and filler ids in plain words.

    Example: 'Weak close bounce, band cap, wqa101 54'. Raises TheoryLoadError for a theory without the heading.
    """
    if theory.display_name is None:
        raise TheoryLoadError(f"{theory.path} needs a '## Display name' heading to write strategies")
    return f"{theory.display_name}, {_plain_pair_words(skeleton_id)}, {_plain_pair_words(filler_id)}"


def generate_one(theory: Theory, facts: Facts, skeleton_id: str, filler_id: str,
                  *, write: bool = True, lookahead_module=None, now: "int | None" = None) -> GenerationOutcome:
    """AD4.1's seven steps for one (skeleton, filler) pair.

    `write` is the PREVIEW gate (UI plan 2.3, "PREVIEW, the gate"): every step below still runs
    when write is False, so the returned decision and reason are the real would-be outcome, but
    the two write helpers (_write_refused_candidate, _write_accepted_candidate) and
    researchdb.upsert_strategy are each called only inside an `if write:` guard, so a probe can
    grep this function's own source for that shape rather than trust a comment. The identity read
    (AD4.1 step 6) always runs unconditionally, in both modes, because it is a READ, not a write:
    a rerun of the exact same theory bytes must report 'skipped' the same way whether or not this
    particular call would have written anything new.

    `write` defaults to True (rather than being required, unlike `generate`'s own signature,
    which this brief pins exactly): every caller in research_lab/probes/generator/ that predates
    this lane's own PREVIEW gate calls generate_one with no `write=` argument at all, on the
    always-write assumption that was this function's only behaviour before this build, and this
    default is what keeps those regression guards passing unchanged rather than editing 11 probe
    files to add a keyword that changes nothing about what they are testing.
    """
    now = int(time.time()) if now is None else now

    # Round-2 finding F1: the identity read (originally step 6) must run BEFORE any write, in
    # EVERY branch, not only ahead of the accepted branch's own write. The two refused
    # branches below (look-ahead, allow-list) each write a candidate row too, and a rerun of
    # the exact same theory bytes, skeleton and filler that was refused last time collided on
    # ux_gencand_identity's own unique index instead of reporting skipped, the way an
    # accepted rerun already did. `emit.emit_module_source` never edits the raw expression
    # (its own body returns `facts_entry["expression"]` unchanged, so this skeleton's
    # embedded expression and the raw facts entry's expression are always byte-identical);
    # that is what makes deriving `expr_sha` from the raw facts entry here, before either
    # gate has run, a safe stand-in for the value either write path below would use.
    facts_entry = facts.fillers.get(filler_id, {"expression": "", "input_names": [], "param_names": []})
    expression = facts_entry.get("expression", "")
    expr_sha = emit.expression_sha256(expression)
    with researchdb.get_conn() as conn:
        existing_id = _find_existing_candidate(conn, theory.theory_sha256, skeleton_id, filler_id, expr_sha)
    if existing_id is not None:
        return GenerationOutcome(skeleton_id, filler_id, "skipped", candidate_id=existing_id,
                                  expression=expression)

    # AC-7 (lane GR): a filler compile_facts.py itself could not compile carries a
    # {"decision": "refused", "reason": ...} entry (facts.py's own load_facts tolerates
    # exactly this shape) instead of the usual compiled-class shape. Checked here, before
    # EITHER structural gate below, because a filler that never compiled has no expression
    # and no compiled indicator for the look-ahead or allow-list gate to read: refusing
    # immediately, with the SAME reason compile_facts.py itself recorded, is the honest
    # outcome, never a re-derived or guessed one, and RUN alike (write=True) still writes a
    # durable refused-candidate row, exactly as the other two refusal branches below do, so
    # this filler is never silently dropped.
    if facts_entry.get("decision") == "refused":
        reason = facts_entry["reason"]  # load_facts already guarantees this key on a refused entry
        candidate_id = None
        if write:
            candidate_id = _write_refused_candidate(
                theory, skeleton_id, filler_id, expression, expr_sha, reason, now)
        return GenerationOutcome(skeleton_id, filler_id, "refused", reason,
                                  candidate_id=candidate_id, expression=expression)

    # Code-quality round 1, SF3: a filler whose compiled facts carry a non-empty
    # param_names is declared into the emitted class's own sweep_params (emit.py's
    # extra_sweep_params) but this build's run()/sweep_signals() never thread a per-filler
    # grid value through self.param for any of those extra names -- the exact "declared but
    # unread" defect this whole lane exists to fix, AC-1/AC-3's own concern, and today it was
    # masked rather than fixed because both committed theories' fillers happen to have empty
    # param_names. Refused here, before either structural gate below, so no such filler ever
    # reaches emit.emit_module_source at all: threading per-filler knobs through the compiled
    # class at runtime is separate, still-to-be-ruled work (the formula-vocabulary work sheet
    # item 3 names), not something this loop can silently paper over with an unread
    # declaration. Reason carries the unread names themselves, plain and reproducible, never
    # a bare boolean.
    facts_param_names = facts_entry.get("param_names") or []
    if facts_param_names:
        reason = f"filler_declares_unread_params:{','.join(facts_param_names)}"
        candidate_id = None
        if write:
            candidate_id = _write_refused_candidate(
                theory, skeleton_id, filler_id, expression, expr_sha, reason, now)
        return GenerationOutcome(skeleton_id, filler_id, "refused", reason,
                                  candidate_id=candidate_id, expression=expression)

    # Step 1: the write-gate look-ahead re-check (R-G21 site 2). Normally a no-op, since the
    # theory already passed this at load (AD4.1's own words); still run, because it is the
    # one site a bug in this loop cannot bypass.
    lookahead_result = check_lookahead(filler_id, lookahead_module=lookahead_module)
    if not lookahead_result.ok:
        # write=False: report the would-be refusal with no row written (candidate_id stays None,
        # honestly, since nothing was written for this pair).
        candidate_id = None
        if write:
            candidate_id = _write_refused_candidate(
                theory, skeleton_id, filler_id, expression, expr_sha, lookahead_result.reason, now)
        return GenerationOutcome(skeleton_id, filler_id, "refused", lookahead_result.reason,
                                  candidate_id=candidate_id, expression=expression)

    # Step 2-4: read the facts, build the swept parameter set (implicit in emit's own
    # template), emit the expression and the module source.
    facts_entry = facts.fillers[filler_id]
    source, expression = emit.emit_module_source(theory, skeleton_id, filler_id, facts_entry)
    expr_sha = emit.expression_sha256(expression)

    # Step 5: the allow-list gate (R-G17/R-G18).
    allowlist_result = check_allowlist(expression, theory, facts_entry)
    if not allowlist_result.ok:
        candidate_id = None  # write=False: same would-be-refusal shape as the look-ahead branch above
        if write:
            candidate_id = _write_refused_candidate(
                theory, skeleton_id, filler_id, expression, expr_sha, allowlist_result.reason, now)
        return GenerationOutcome(skeleton_id, filler_id, "refused", allowlist_result.reason,
                                  candidate_id=candidate_id, expression=expression)

    # Step 6: the identity check, re-read here defensively. The pre-check above already runs
    # this same read before any branch's write and normally catches a rerun first; this
    # second read only matters if a future skeleton ever embeds an expression that differs
    # from the raw facts entry (band_cap does not), in which case expr_sha here could differ
    # from the pre-check's value and this is the read that must catch that rerun. A hit means
    # an exact rerun: write nothing, call the strategy write zero times (AC-41).
    with researchdb.get_conn() as conn:
        existing_id = _find_existing_candidate(conn, theory.theory_sha256, skeleton_id, filler_id, expr_sha)
    if existing_id is not None:
        return GenerationOutcome(skeleton_id, filler_id, "skipped", candidate_id=existing_id,
                                  expression=expression)

    # Step 7: decide the outcome and write in the one order the schema permits. The strategy
    # write is FIRST, create_only=True always (R-G26): an existing derived name refuses here
    # rather than silently overwriting a different candidate's source.
    name = emit.derive_strategy_name(theory.theory_id, skeleton_id, filler_id)
    if not write:
        # write=False: derive the same would-be decision WITHOUT calling upsert_strategy (which
        # both checks and writes atomically inside its own transaction). A plain read-only SELECT
        # against the same uniqueness key (research_strategies.name, upsert_strategy's own check)
        # answers the identical question a real write would have raised StrategyNameExists on,
        # with no row touched either way.
        with researchdb.get_conn() as conn:
            collision = conn.execute(
                "SELECT id FROM research_strategies WHERE name = ?;", (name,)
            ).fetchone()
        if collision is not None:
            return GenerationOutcome(skeleton_id, filler_id, "refused", "derived_name_collision",
                                      expression=expression)
        return GenerationOutcome(skeleton_id, filler_id, "accepted", strategy_name=name,
                                  expression=expression)

    # Lane G2 fold (U.4 second half): wrapped in an explicit `if write:`, purely structural and
    # never a behaviour change (control already cannot reach this point with write=False, since
    # the block above returns on every one of ITS OWN branches first) -- this is what makes the
    # module docstring's own claim ("the two write helpers and upsert_strategy are each called
    # only inside an `if write:` guard, so a probe can grep this function's own source for that
    # shape") literally true for every write call site, not just the two refusal branches above.
    if write:
        try:
            shown_name = theory.display_name and strategy_display_name(theory, skeleton_id, filler_id)
            strategy_id, _version, _action = researchdb.upsert_strategy(name, source, create_only=True, display_name=shown_name)
        except researchdb.StrategyNameExists:
            candidate_id = _write_refused_candidate(
                theory, skeleton_id, filler_id, expression, expr_sha, "derived_name_collision", now)
            return GenerationOutcome(skeleton_id, filler_id, "refused", "derived_name_collision",
                                      candidate_id=candidate_id, expression=expression)

        candidate_id = _write_accepted_candidate(theory, skeleton_id, filler_id, expression, expr_sha, strategy_id, now)
        return GenerationOutcome(skeleton_id, filler_id, "accepted", strategy_id=strategy_id,
                                  strategy_name=name, candidate_id=candidate_id, expression=expression)


def generate(theory: Theory, facts: Facts, *, write: bool,
             selected: "set[tuple[str, str]] | None" = None,
             lookahead_module=None) -> "list[GenerationOutcome]":
    """AD4.1: for each skeleton in the theory's first list, for each filler in the second.

    `write` is threaded straight through to generate_one (the PREVIEW gate). `selected`, when
    given, restricts an ACTUAL write to only the named (skeleton_id, filler_id) pairs (the RUN
    GENERATOR action after a PREVIEW where Tamer ticked a subset): a pair outside `selected` is
    reported 'skipped: not-selected' and generate_one is never even called for it, so a pair
    Tamer un-ticked is never re-evaluated, only reported skipped. `selected=None` (PREVIEW itself,
    and a RUN GENERATOR with nothing to restrict) means every pair the theory names is evaluated.
    """
    if write:
        # DN1: a write run needs the theory's display name, checked for every pair before any candidate row,
        # so a missing heading or a name over 60 characters leaves nothing half written.
        for skeleton_id in theory.skeletons:
            for filler_id in theory.fillers:
                shown = strategy_display_name(theory, skeleton_id, filler_id)
                if len(shown) > 60:
                    raise TheoryLoadError(
                        f"{theory.path}: the display name '{shown}' is {len(shown)} characters; the most allowed is 60")
    outcomes = []
    for skeleton_id in theory.skeletons:
        for filler_id in theory.fillers:
            if selected is not None and (skeleton_id, filler_id) not in selected:
                outcomes.append(GenerationOutcome(skeleton_id, filler_id, "skipped", reason="not-selected"))
                continue
            outcomes.append(generate_one(theory, facts, skeleton_id, filler_id,
                                          write=write, lookahead_module=lookahead_module))
    return outcomes
