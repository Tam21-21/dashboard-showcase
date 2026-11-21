"""SEC 13D form.idx + full-submission .txt parsers.

Brief D per spec brief v4 §10 + arch v6 §5.2 step 3.

Public surface
--------------
parse_form_idx(text: str) -> Iterator[FormIdxRow]
  Yield one FormIdxRow per SC 13D, SC 13D/A, SCHEDULE 13D or SCHEDULE 13D/A row in a form.idx file.

extract_accession_from_idx_filename(filename: str) -> str
  Pull the 18-char dashed accession_number out of an idx Filename column
  (`edgar/data/{cik}/{accession}-index.htm` or
  `edgar/data/{cik}/{accession}.txt`).

parse_13d_submission(submission_text: str) -> dict
  Take a full-submission .txt (SGML header + cover-page body) and return
  the canonical record dict the brief D fetcher hands to write_event_batch.
  Raises EdgarParseFatal if any required field cannot be extracted; the
  fetcher quarantines the row at the parse step (§3.6 failure_step='parse').

Design notes
------------
- form.idx columns are fixed-position-ish. SEC's spec is ASCII text with
  whitespace padding. We split each row on the first 2+ consecutive
  whitespace characters per column, which survives the case where a
  company name has internal single spaces and the case where the row's
  filename has its own internal characters.
- SGML headers (`<SEC-HEADER>...</SEC-HEADER>`) are deterministic; we
  regex them. The body cover-page text varies by filer template; we run
  a small set of robust regexes for percent_owned and fall through to a
  generic "Item 4 percent" search if the templated regex misses.
- We never depend on bs4 / lxml. stdlib only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator, Optional

from ..clients.edgar import parse_acceptance_datetime
from ..errors import EdgarParseFatal


def _date_filed_to_epoch(date_filed: str) -> int:
    """Convert a form.idx `YYYY-MM-DD` filing date to a UTC-midnight epoch.

    The 13D-parser fallback for old (pre-~1996) filings whose SGML header carries no
    ACCEPTANCE-DATETIME tag: the index filing date is the best available knowledge_at
    (the day SEC received the filing). Raises EdgarParseFatal on an unparseable date.
    """
    try:
        d = dt.datetime.strptime(date_filed.strip(), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise EdgarParseFatal(f"form.idx date_filed did not parse: {date_filed!r} ({exc})") from exc
    return int(d.timestamp())


# ---------------------------------------------------------------------------
# form.idx parsing
# ---------------------------------------------------------------------------

# The header line of a form.idx file. The text varies across years; we look
# for the divider line of dashes that always follows the header. Everything
# above the divider is the human header; everything below is data rows.
_FORM_IDX_DIVIDER_RE = re.compile(r"^-{20,}\s*$")

# Form types we care about: the original and amendment forms. Since SEC's change of
# 18 Dec 2024 a Schedule 13D is filed as "SCHEDULE 13D" / "SCHEDULE 13D/A" with an XML cover.
_SC_13D_FORM_TYPES = {"SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"}


@dataclass(frozen=True)
class FormIdxRow:
    """One parsed form.idx row for SC 13D / SC 13D/A."""
    form_type: str           # 'SC 13D' or 'SC 13D/A'
    company_name: str        # the filer's company name as listed in idx
    filer_cik: int           # CIK column (the FILING entity, not the subject)
    date_filed: str          # YYYY-MM-DD per idx convention
    filename: str            # e.g. 'edgar/data/1234567/0001234567-24-000001-index.htm'
    accession_number: str    # extracted from `filename`


def extract_accession_from_idx_filename(filename: str) -> str:
    """Extract the 18-char dashed accession_number from an idx filename.

    Two known shapes appear in the historical record:
      edgar/data/{cik}/{accession}-index.htm
      edgar/data/{cik}/{accession}.txt
    (and `.html`, `.htm` variants on the index page.)
    """
    base = filename.rstrip().rsplit("/", 1)[-1]
    # Strip the -index.htm[l] / .txt suffix.
    for suffix in ("-index.htm", "-index.html", "-index.json", ".txt"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    # The remainder should be an 18-char dashed accession `\d{10}-\d{2}-\d{6}`.
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", base):
        raise EdgarParseFatal(
            f"form.idx filename did not yield an 18-char dashed accession: "
            f"{filename!r} -> {base!r}"
        )
    return base


def parse_form_idx(text: str) -> Iterator[FormIdxRow]:
    """Yield FormIdxRow for every SC 13D / SC 13D/A row in `text`.

    Discipline:
      1. Skip everything above the first divider line (the human header).
      2. For each subsequent line:
         - Strip; skip blank lines.
         - Split on the first 2+ consecutive whitespace runs into 5 fields.
         - Filter to form_types in _SC_13D_FORM_TYPES.
         - Extract accession_number from the filename column.
      3. A malformed row (wrong field count, non-int CIK, no parseable
         accession) is **skipped with a logged warning** rather than
         raised: the form.idx contains thousands of rows for many form
         types and one bad row should not cap an entire quarter. The
         brief D probe verifies the skip behaviour with a synthetic
         malformed row in its fixture.
    """
    seen_divider = False
    for raw_line in text.splitlines():
        if not seen_divider:
            if _FORM_IDX_DIVIDER_RE.match(raw_line):
                seen_divider = True
            continue
        line = raw_line.strip()
        if not line:
            continue
        # Split on 2+ consecutive whitespace runs (handles single internal
        # spaces inside Company Name without dropping the surrounding
        # column boundaries). Strip first — SEC's form.idx pads each line's
        # filename column to a fixed width with trailing spaces; without
        # the strip the split would yield a phantom empty trailing element
        # and shift the parts[-1] / parts[-2] / parts[-3] indices off by
        # one (the empty would become `filename`, the real filename would
        # become `date_filed`, etc., and every row would fail the
        # accession-extraction step).
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 5:
            # Malformed row — skip; do not raise.
            continue
        # SEC's exact column ordering: Form Type, Company Name, CIK, Date Filed,
        # Filename. The split above is robust against extra interior whitespace,
        # but the filename column may itself contain spaces in historical years
        # (it does not in current ones). For SC 13D rows the filename is always
        # a simple path; take the LAST element as filename, the second-to-last
        # as date_filed, the third-to-last as CIK, the second as company name,
        # and the first as form_type.
        form_type = parts[0].strip()
        if form_type not in _SC_13D_FORM_TYPES:
            continue
        filename = parts[-1].strip()
        date_filed = parts[-2].strip()
        cik_str = parts[-3].strip()
        company_name = " ".join(p.strip() for p in parts[1:-3]).strip()
        try:
            filer_cik = int(cik_str)
        except ValueError:
            continue
        try:
            accession_number = extract_accession_from_idx_filename(filename)
        except EdgarParseFatal:
            continue
        yield FormIdxRow(
            form_type=form_type,
            company_name=company_name,
            filer_cik=filer_cik,
            date_filed=date_filed,
            filename=filename,
            accession_number=accession_number,
        )


# ---------------------------------------------------------------------------
# SGML header + cover-page body parsing
# ---------------------------------------------------------------------------

# All SGML-header regex patterns operate on the header block between
# <SEC-HEADER>...</SEC-HEADER> (or the first <DOCUMENT> when the closing
# tag is missing, which it sometimes is in older filings).

_HEADER_BLOCK_RE = re.compile(
    r"<SEC-HEADER>(.*?)(?:</SEC-HEADER>|<DOCUMENT>)",
    re.DOTALL | re.IGNORECASE,
)

# Accession_number from the header (back-up; we typically already have it
# from the idx row, but we cross-check to detect any mis-routing).
_ACCESSION_NUMBER_RE = re.compile(
    r"^ACCESSION[\s_-]NUMBER:\s*(\S+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# ACCEPTANCE-DATETIME (YYYYMMDDHHMMSS Eastern wall-clock; R14 conversion lives
# in `clients.edgar.parse_acceptance_datetime`).
_ACCEPTANCE_DATETIME_RE = re.compile(
    r"^<?ACCEPTANCE-DATETIME>?\s*[:=>]?\s*(\d{14})\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# SGML header sub-blocks. SEC filings carry a `SUBJECT COMPANY:` block on
# 13D filings naming the target company, and a `FILED BY:` (or `FILER:`)
# block naming the reporting entity. Each block contains COMPANY DATA, FILING
# VALUES, BUSINESS ADDRESS, MAIL ADDRESS, FORMER COMPANY sub-blocks. We pull
# the COMPANY CONFORMED NAME + CENTRAL INDEX KEY from each.

_SUBJECT_BLOCK_RE = re.compile(
    r"SUBJECT\s+COMPANY:(.*?)(?=^[A-Z][A-Z\s]*:\s*$|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_FILER_BLOCK_RE = re.compile(
    r"(?:^FILED\s+BY:|^FILER:)(.*?)(?=^[A-Z][A-Z\s]*:\s*$|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)

# Within a block (subject or filer):
_COMPANY_NAME_RE = re.compile(
    r"COMPANY\s+CONFORMED\s+NAME:\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CIK_RE = re.compile(
    r"CENTRAL\s+INDEX\s+KEY:\s*(\d+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Cover-page body patterns. Item 1 carries a CUSIP somewhere; Item 4 / row 11
# carries the percent ownership. EDGAR templates vary; we attempt several
# robust patterns in order.
_CUSIP_RE = re.compile(
    r"CUSIP\s*(?:Number|No\.?|#)?\s*[:.\-]?\s*([0-9A-Z]{9})",
    re.IGNORECASE,
)
# Percent of class represented by amount in row (11): common 13D wording.
_PERCENT_OWNED_PATTERNS = [
    # "Percent of class represented by amount in Row (11): 5.4%"
    re.compile(
        r"Percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+Row\s*\(?11\)?\s*[:.]?\s*([\d.]+)\s*%?",
        re.IGNORECASE,
    ),
    # "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11): 5.4"
    re.compile(
        r"Percent\s+of\s+Class\s*[:.]?\s*([\d.]+)\s*%?",
        re.IGNORECASE,
    ),
    # Table layouts that place the value a short distance AFTER "Row (11)" /
    # "in Row 11" (the cover-page row-11 box, where the label and the number sit
    # in adjacent cells): allow up to ~40 non-digit chars between the row marker
    # and the percentage. Bounded so it cannot wander into an unrelated number.
    re.compile(
        r"(?:amount\s+in\s+)?Row\s*\(?\s*11\s*\)?[^%\d]{0,40}?([\d.]+)\s*%",
        re.IGNORECASE,
    ),
    # "(13) ... represents 5.4% of the class" / "5.4% of the [common] [stock|shares]".
    # The negative lookbehind anchors the LEFT of the number so it cannot capture the tail
    # of a longer figure ("...1234.5% of the class" would otherwise match "4.5").
    re.compile(
        r"(?<!\d)([\d.]+)\s*%\s+of\s+the\s+(?:outstanding\s+)?(?:class|common|shares|stock)",
        re.IGNORECASE,
    ),
]


def _find_block(block_re: re.Pattern, header_block: str) -> str:
    """Locate a subject/filer sub-block; return its text or empty string."""
    m = block_re.search(header_block)
    return m.group(1) if m else ""


def _extract_name_and_cik(block: str) -> tuple[Optional[str], Optional[int]]:
    """Pull COMPANY CONFORMED NAME and CENTRAL INDEX KEY from a sub-block."""
    name_m = _COMPANY_NAME_RE.search(block)
    cik_m = _CIK_RE.search(block)
    name = name_m.group(1).strip() if name_m else None
    cik = int(cik_m.group(1)) if cik_m else None
    return name, cik


def _strip_html_tags(text: str) -> str:
    """Strip HTML tags + collapse whitespace for cover-page regex matching.

    The cover-page body is typically HTML wrapped in a <TEXT> SGML container.
    Real templates use tables, font tags, and inline styles; the percent
    field and the CUSIP are leaves in the markup. Stripping tags reduces the
    body to a flat string the regex can match against without doing real
    HTML parsing.
    """
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


# ---------------------------------------------------------------------------
# XML cover page (filings from 18 Dec 2024)
# ---------------------------------------------------------------------------

_DOCUMENT_SPLIT_RE = re.compile(r"<DOCUMENT>", re.IGNORECASE)
_DOC_TYPE_RE = re.compile(r"<TYPE>\s*([^\r\n]+)", re.IGNORECASE)
_PURPOSE_MAX_CHARS = 2000


def _local(tag: str) -> str:
    """Tag name without its namespace: '{uri}name' or 'sch:name' or 'name' -> 'name'."""
    return tag.split("}")[-1].split(":")[-1]


def _first(node: ET.Element, name: str) -> Optional[ET.Element]:
    """First element (document order, node itself excluded) whose local name is `name`."""
    for el in node.iter():
        if el is not node and _local(el.tag) == name:
            return el
    return None


def _text_of(node: Optional[ET.Element]) -> Optional[str]:
    if node is None or node.text is None:
        return None
    t = node.text.strip()
    return t or None


def _number_of(node: Optional[ET.Element], what: str, accession: str) -> Optional[float]:
    t = _text_of(node)
    if t is None:
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError as exc:
        raise EdgarParseFatal(f"13D XML cover {what} is not a number ({t!r}): {accession}") from exc


def _xml_cover(body: str, accession: str) -> Optional[dict]:
    """Read the XML cover of a Schedule 13D submission, or None when it has none (old text cover).

    Looks at the first <DOCUMENT> whose <TYPE> is one of the four 13D forms. When that document
    holds <XML>...</XML>, the text between is parsed with ElementTree and read by local name
    (the cover may use a sch: prefix or none). A missing element is None, never a guess.
    Raises EdgarParseFatal naming the accession when the XML does not parse.
    """
    for doc in _DOCUMENT_SPLIT_RE.split(body)[1:]:
        type_m = _DOC_TYPE_RE.search(doc)
        if not type_m or type_m.group(1).strip().upper() not in _SC_13D_FORM_TYPES:
            continue
        start = re.search(r"<XML>", doc, re.IGNORECASE)
        if not start:
            return None
        end = re.search(r"</XML>", doc[start.end():], re.IGNORECASE)
        if not end:
            raise EdgarParseFatal(f"13D XML cover did not parse: {accession} (no closing </XML>)")
        xml_text = doc[start.end(): start.end() + end.start()].strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise EdgarParseFatal(f"13D XML cover did not parse: {accession} ({exc})") from exc

        persons = [el for el in root.iter() if _local(el.tag) == "reportingPersonInfo"]
        first = persons[0] if persons else None
        percent = _number_of(_first(first, "percentOfClass"), "percentOfClass", accession) if first is not None else None
        shares = _number_of(_first(first, "aggregateAmountOwned"), "aggregateAmountOwned", accession) if first is not None else None

        event_date: Optional[str] = None
        raw_date = _text_of(_first(root, "dateOfEvent"))
        if raw_date:
            try:
                event_date = dt.datetime.strptime(raw_date, "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError as exc:
                raise EdgarParseFatal(f"13D XML cover dateOfEvent did not parse ({raw_date!r}): {accession}") from exc

        cusip = _text_of(_first(root, "issuerCusipNumber"))
        item4 = _first(root, "item4")
        purpose = _text_of(_first(item4, "transactionPurpose")) if item4 is not None else None
        return {
            "percent_owned": percent,
            "subject_cusip": cusip.upper() if cusip else None,
            "shares_owned": shares,
            "reporting_persons": len(persons),
            "event_date": event_date,
            "purpose": purpose[:_PURPOSE_MAX_CHARS] if purpose else None,
        }
    return None


def parse_13d_submission(submission_text: str,
                         *,
                         expected_filer_cik: Optional[int] = None,
                         expected_accession_number: Optional[str] = None,
                         expected_date_filed: Optional[str] = None) -> dict:
    """Parse a full-submission .txt; return the canonical record dict.

    Returns a dict with the §4.1 payload fields the brief D fetcher hands to
    write_event_batch's records iterable:
        natural_key, knowledge_at, occurred_at (None for 13D MVP),
        source='sec_edgar', origin='fetched', payload_schema_version=1,
        raw_input (parsed cover-page extract, <64 KiB),
        payload={percent_owned, filer_name, subject_name},
        companies=[{cik=subject_cik, role='primary_issuer', cusip=subject_cusip}],
        accession_number, producer_id=None, event_type='sec_13d'.

    expected_filer_cik / expected_accession_number, when provided (the EDGAR
    fetcher gets both from the form.idx row), are cross-checked against what
    the SGML header reports; a mismatch raises EdgarParseFatal (the file was
    mis-routed at fetch time).

    Raises EdgarParseFatal if any of the still-required fields below cannot be
    extracted:
      - ACCEPTANCE-DATETIME on the header, OR an `expected_date_filed` fallback
      - SUBJECT COMPANY block (with name + CIK)
      - FILED BY / FILER block (with name)
    `percent_owned` is NULLABLE (the parser fix): an unextractable cover-page %
    stores None rather than discarding the filing. `subject_cusip` is optional;
    absent → companies row carries cusip=None. An idx-vs-header filer-CIK
    difference is NOT fatal (mis-routing is caught by the accession cross-check).
    """
    # 1) Header block
    header_m = _HEADER_BLOCK_RE.search(submission_text)
    if not header_m:
        raise EdgarParseFatal(
            "13D submission has no <SEC-HEADER> block; not a valid submission"
        )
    header_block = header_m.group(1)

    # 2) ACCEPTANCE-DATETIME → knowledge_at (R14 UTC epoch). Old (pre-~1996) headers
    #    predate this tag; fall back to the form.idx filing date when it is missing
    #    (the parser fix — otherwise the whole 1994-1996 slice is discarded). A present
    #    tag is still authoritative (intraday precision); the fallback is date-only.
    acc_m = _ACCEPTANCE_DATETIME_RE.search(header_block)
    if acc_m:
        try:
            knowledge_at = parse_acceptance_datetime(acc_m.group(1))
        except ValueError as exc:
            raise EdgarParseFatal(
                f"13D submission ACCEPTANCE-DATETIME did not parse: {exc}"
            ) from exc
    elif expected_date_filed:
        knowledge_at = _date_filed_to_epoch(expected_date_filed)
    else:
        raise EdgarParseFatal(
            "13D submission header missing ACCEPTANCE-DATETIME tag and no idx date_filed fallback"
        )

    # 3) Accession cross-check (header vs idx)
    header_accession_m = _ACCESSION_NUMBER_RE.search(header_block)
    if expected_accession_number and header_accession_m:
        header_accession = header_accession_m.group(1).strip()
        if header_accession != expected_accession_number:
            raise EdgarParseFatal(
                f"13D submission accession mismatch: idx={expected_accession_number!r} "
                f"header={header_accession!r}"
            )

    # 4) Subject company name + CIK + (optional) CUSIP
    subject_block = _find_block(_SUBJECT_BLOCK_RE, header_block)
    if not subject_block.strip():
        raise EdgarParseFatal("13D submission has no SUBJECT COMPANY block")
    subject_name, subject_cik = _extract_name_and_cik(subject_block)
    if not subject_name or not subject_cik:
        raise EdgarParseFatal(
            f"13D SUBJECT COMPANY block missing name or CIK; "
            f"name={subject_name!r} cik={subject_cik!r}"
        )

    # 5) Filer name + CIK (CIK cross-check against idx, if provided)
    filer_block = _find_block(_FILER_BLOCK_RE, header_block)
    if not filer_block.strip():
        raise EdgarParseFatal("13D submission has no FILED BY / FILER block")
    filer_name, filer_cik_header = _extract_name_and_cik(filer_block)
    if not filer_name:
        raise EdgarParseFatal("13D FILED BY block missing COMPANY CONFORMED NAME")
    # The parser fix: an idx-CIK vs header-FILED-BY-CIK difference is NOT fatal. For 13D this
    # differs routinely — activist/group filings carry several FILED-BY blocks (we read the
    # first), and EDGAR also indexes a 13D under the SUBJECT company's CIK. Mis-routing is
    # already caught by the accession cross-check above (header accession vs idx accession),
    # so this equality check only discarded valid filings. Prefer the header's FILED-BY CIK
    # (the submission's own statement of who filed) when present, else the idx CIK.
    filer_cik = filer_cik_header or expected_filer_cik
    if not filer_cik:
        raise EdgarParseFatal("13D submission has no filer CIK on header or via expected_filer_cik")

    # 6) Cover-page body: percent_owned + (optional) CUSIP
    # The body lives after the header; we de-tag and regex.
    body = submission_text[header_m.end():]
    body_text = _strip_html_tags(body)

    # percent_owned is NULLABLE: when no pattern matches we store None rather than discarding
    # the whole filing (the only consumer is a nullable float64 materializer column).
    accession_for_errors = expected_accession_number or (
        header_accession_m.group(1).strip() if header_accession_m else "unknown accession"
    )
    xml = _xml_cover(body, accession_for_errors)

    percent_owned: Optional[float] = None
    cusip: Optional[str] = None
    if xml is not None:
        percent_owned = xml["percent_owned"]
        cusip = xml["subject_cusip"]
    else:
        for pat in _PERCENT_OWNED_PATTERNS:
            m = pat.search(body_text)
            if m:
                try:
                    percent_owned = float(m.group(1))
                except ValueError:
                    continue
                break
        cusip_m = _CUSIP_RE.search(body_text)
        if cusip_m:
            cusip = cusip_m.group(1).upper()

    # 7) Build the record. raw_input is bounded to 64 KiB by the shared core
    # at quarantine time, but we keep the raw_input field reasonably small
    # here too — a parsed extract, not the whole submission.
    accession_number = expected_accession_number or (
        header_accession_m.group(1).strip() if header_accession_m else None
    )
    if not accession_number:
        raise EdgarParseFatal("13D submission has no accession_number on header or via expected_accession_number")
    natural_key = f"{filer_cik}_{accession_number}"
    raw_extract = json.dumps(
        {
            "accession_number": accession_number,
            "filer_cik": filer_cik,
            "filer_name": filer_name,
            "subject_cik": subject_cik,
            "subject_name": subject_name,
            "subject_cusip": cusip,
            "percent_owned": percent_owned,
            "knowledge_at_epoch_utc": knowledge_at,
            "cover_format": "xml" if xml is not None else "text",
            "shares_owned": xml["shares_owned"] if xml is not None else None,
            "reporting_persons": xml["reporting_persons"] if xml is not None else None,
            "event_date": xml["event_date"] if xml is not None else None,
            "purpose": xml["purpose"] if xml is not None else None,
        },
        separators=(",", ":"),
    )
    return {
        "event_type": "sec_13d",
        "natural_key": natural_key,
        "producer_id": None,
        "knowledge_at": knowledge_at,
        "occurred_at": None,
        "source": "sec_edgar",
        "origin": "fetched",
        "payload_schema_version": 1,
        "raw_input": raw_extract,
        "payload": {
            "percent_owned": float(percent_owned) if percent_owned is not None else None,
            "filer_name": filer_name,
            "subject_name": subject_name,
        },
        "companies": [
            {"cik": int(subject_cik), "role": "primary_issuer", "cusip": cusip}
        ],
        "accession_number": accession_number,
    }
