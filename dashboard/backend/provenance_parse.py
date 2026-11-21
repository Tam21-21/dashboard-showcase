"""Pure text parsing for the parameter-provenance markdown file (docs/research_lab/parameter_provenance.md).

Extracted out of researchdb.py (OPS-1, docs/handoffs/ops1_provenance_parser_extraction.md) so the
operations page can parse its own provenance-shaped markdown without importing a 6,400-line research
database module to get there. This is a behaviour-preserving move: every line of code below is
unchanged from the researchdb.py functions it came from, only the file it lives in has moved.

Both functions take the markdown TEXT as a plain string and return plain dict/set structures. Neither
one opens a file, connects to a database, reads an environment variable, or imports anything from
researchdb.py (there is nothing to import: this module's import block is empty). That purity is what
makes the extraction safe. Nothing here can ever form an import cycle with researchdb.py, because
nothing here needs anything researchdb.py has; researchdb.py imports these three names back instead.

`_parse_provenance_bodies` ships alongside `_parse_provenance_headings` deliberately, even though the
brief this module was built from names only "the heading parser and the required-subsections tuple".
Its own docstring already called itself the "Companion to _parse_provenance_headings": it walks the
same markdown grammar over the same file to build the same {key: ...} shape, differing in returning
subsection BODY text instead of subsection NAMES.

CORRECTED 2026-08-19 by OPS-1's code-quality gate, because an earlier draft of this paragraph said
they run "the identical ... state machine" and they do not. `_parse_provenance_headings` branches on
'### ' and '## ' only; `_parse_provenance_bodies` branches on '### ', '## ' AND '# ', flushing and
clearing its key on a top-level title. So on a document with a mid-file H1 followed by an orphaned
H3 before the next H2, the headings parser would attribute that H3 to the PREVIOUS entry while the
bodies parser correctly discards it. That divergence is inert against the real file, which carries
exactly one '# ' line and it is line 1 -- checked, not assumed. It is inherited unchanged from the
pre-extraction code and is deliberately NOT repaired here: both functions are proven byte-identical
to their originals, and changing what either one computes would break the one property this unit
exists to preserve. The overstated claim mattered because it was this docstring's own justification
for keeping the pair together, and a justification that is not true is not a justification. Splitting one matched pair of functions across two modules, so a future maintainer has to
remember which file owns which half of one parser, is the exact shape this extraction exists to
remove.
"""

# The four mandatory H3 subsections every provenance entry must carry (M3 spec §4). Moved verbatim
# from researchdb.py; researchdb.py now imports this name back into its own namespace rather than
# defining it (see the import-form comment in researchdb.py's import block for why that matters).
_REQUIRED_PROVENANCE_SECTIONS = ('Source', 'Mechanism', 'Sensitivity', 'Bounds')


def _parse_provenance_headings(text: str) -> dict[str, set[str]]:
    """Map each '## ' heading in the markdown to the set of its '### ' sub-headings."""
    sections: dict[str, set[str]] = {}  # heading title -> set of its subsection titles
    current = None  # the H2 heading we are currently inside, or None before the first one
    for line in text.splitlines():  # scan the file line by line
        if line.startswith('### '):  # an H3 subsection line (checked first; '### ' is not a '## ' heading)
            if current is not None:  # ignore stray H3s that appear before any H2
                sections[current].add(line[4:].strip())  # record the subsection title under the current H2
        elif line.startswith('## '):  # an H2 parameter-entry heading
            current = line[3:].strip()  # the heading text is the '{scope}.{key}' identifier
            sections[current] = set()  # start a fresh, empty subsection set for it
    return sections


def _parse_provenance_bodies(text: str) -> dict[str, dict[str, str]]:
    """Map each '## scope.key' to {subsection_title: body_markdown} (the four H3 sections).

    Companion to _parse_provenance_headings, which returns only the subsection NAMES; brief L's
    tooltip needs the body text under each. A line is accumulated into the current H3 body.
    """
    entries: dict[str, dict[str, str]] = {}  # '{scope}.{key}' -> {subsection: body}
    cur_key: str | None = None  # the H2 we are inside
    cur_sub: str | None = None  # the H3 we are inside
    buf: list[str] = []  # body lines for the current H3

    def flush() -> None:
        if cur_key is not None and cur_sub is not None:  # store the accumulated body
            entries[cur_key][cur_sub] = "\n".join(buf).strip()

    for line in text.splitlines():
        if line.startswith("### "):  # new subsection: flush the previous, start a fresh buffer
            flush()
            cur_sub = line[4:].strip()
            buf = []
        elif line.startswith("## "):  # new parameter entry: flush, reset key + subsection
            flush()
            cur_key = line[3:].strip()
            entries[cur_key] = {}
            cur_sub, buf = None, []
        elif line.startswith("# "):  # top-level title: leaves any open entry, no new key
            flush()
            cur_key, cur_sub, buf = None, None, []
        elif cur_sub is not None:  # ordinary body line under the current H3
            buf.append(line)
    flush()
    return entries
