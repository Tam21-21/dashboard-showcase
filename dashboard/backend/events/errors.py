"""M2.5 event-store named exception hierarchy.

Every quarantine row, every batch-level failure_class, and every admin
endpoint 4xx error body maps back to a class in this file. No catch-all
`except Exception` paths in the shared core (CLAUDE.md): the write_core
catches one named class and routes it; an unexpected class propagates.

Hierarchy (per spec brief §3.4, §3.6, §4.3, §7.2):

EventStoreError
  PayloadValidationError              per-record class raised by per-type validators (§4.3)
    MissingRequiredField
    WrongFieldType
    UnknownField
    InvalidEnumValue
  RecordValidationAbort               batch-level escalation form of any PayloadValidationError
                                      under policy='abort_on_first_error' (BL2)
  LoaderEnvelopeInvalid               per-line R23 envelope failure (per-record or escalated, §7.2 duality)
  LoaderJsonDecodeError               per-line JSON parse failure (per-record or escalated)
  LoaderFileUnreadable                whole-file structural failure (RB1; batch-level only)
  TickerNotFoundInBridge              loader's raw_ticker → cik lookup miss (per-record or escalated)
  EdgarHttp5xx                        EDGAR HTTP 5xx after retries exhausted (batch-level only)
  EdgarHttp429Persistent              second 429 after the backoff retry (batch-level only)
  EdgarNetworkTimeout                 TCP/TLS / read timeout (batch-level only)
  EdgarParseFatal                     EDGAR response parsed as non-form.idx content (batch-level only)
  OrphanedRunningBatch                reaper-driven flip from running → failed (batch-level only)
  Cancelled                           SIGTERM-driven flip; status='cancelled_partial' (batch-level only)
  CorrectionAgainstRunningBatch       SFv4-1: correction target's batch must not be running
  CorrectionAltersCompanyLinks        round-5 correction 2: corrections cannot mutate the company-link set

This file declares all classes; M2.5 briefs B-N raise the subset relevant to
their scope. Brief B raises the payload-validation classes, RecordValidationAbort,
CorrectionAgainstRunningBatch, and CorrectionAltersCompanyLinks; the loader
classes are raised by brief E (canonical loader); the EDGAR classes are raised
by brief D (fetcher); OrphanedRunningBatch / Cancelled by brief I (reaper / SIGTERM).
"""
from __future__ import annotations


class EventStoreError(Exception):
    """Base class for every M2.5 event-store named exception.

    Catch-all parent so probes and admin endpoint handlers have one isinstance
    check to bucket "any event-store-originated failure" without losing the
    specific subclass on the exception object.
    """


# ---------------------------------------------------------------------------
# Per-record payload validation
# ---------------------------------------------------------------------------


class PayloadValidationError(EventStoreError):
    """A per-event-type validator (events/types/<X>.py) rejected a record's payload.

    The shared core's catch path maps the specific subclass to a quarantine row's
    `failure_class` value (§3.6, no CHECK enum on that column for the per-record
    side per MN6). Under `policy='abort_on_first_error'` the same exception
    raises `RecordValidationAbort` after the prior-chunk rollback.
    """


class MissingRequiredField(PayloadValidationError):
    """A REQUIRED_FIELDS key was absent or had an explicit None value."""


class WrongFieldType(PayloadValidationError):
    """A field's value did not match the declared type in REQUIRED_FIELDS / OPTIONAL_FIELDS."""


class UnknownField(PayloadValidationError):
    """A payload key was not in REQUIRED_FIELDS ∪ OPTIONAL_FIELDS (strict validator)."""


class InvalidEnumValue(PayloadValidationError):
    """A field's value was outside the per-type allowed-values set."""


# ---------------------------------------------------------------------------
# Batch-level escalation (BL2)
# ---------------------------------------------------------------------------


class RecordValidationAbort(EventStoreError):
    """A per-record PayloadValidationError escalated by policy='abort_on_first_error'.

    Carries the underlying class in `.underlying_class_name` so the quality
    report's `terminal_failure.underlying_class` field is populated faithfully
    per §7.1.1. Distinct from the per-record `PayloadValidationError` subclass
    so the §3.4 CHECK enum on batches.failure_class stays bounded.
    """

    def __init__(self, message: str, *, underlying_class_name: str,
                 record_index_in_batch: int, natural_key: str | None = None,
                 raw_input_excerpt: str | None = None,
                 rolled_back_event_count: int = 0):
        super().__init__(message)
        # The PayloadValidationError subclass name (e.g. "MissingRequiredField")
        # that fired before this escalation. Faithfully recorded so §7.1.1's
        # terminal_failure sub-object can carry it.
        self.underlying_class_name = underlying_class_name
        self.record_index_in_batch = record_index_in_batch
        self.natural_key = natural_key
        self.raw_input_excerpt = raw_input_excerpt
        # Set by the writer after the prior-chunk rollback completes, so the
        # quality report can report a true count rather than an estimate.
        self.rolled_back_event_count = rolled_back_event_count


# ---------------------------------------------------------------------------
# Loader-side classes (brief E raises; brief B's write_core does not invoke directly)
# ---------------------------------------------------------------------------


class LoaderEnvelopeInvalid(EventStoreError):
    """R23 envelope rejected a line. Per-record under quarantine; escalates under abort (§7.2)."""


class LoaderJsonDecodeError(EventStoreError):
    """A canonical-file line was not valid JSON."""


class LoaderFileUnreadable(EventStoreError):
    """Whole-file structural failure (empty, not UTF-8, single JSON array). RB1; batch-level only."""


class TickerNotFoundInBridge(EventStoreError):
    """Loader's raw_ticker → CIK lookup against research_cik_ticker_map returned no row."""


# ---------------------------------------------------------------------------
# EDGAR fetcher classes (brief D raises)
# ---------------------------------------------------------------------------


class EdgarHttp5xx(EventStoreError):
    """EDGAR 5xx after `client_429_max_retries` exhausted."""


class EdgarHttp429Persistent(EventStoreError):
    """Second 429 after the backoff retry."""


class EdgarNetworkTimeout(EventStoreError):
    """TCP/TLS handshake or read timeout."""


class EdgarParseFatal(EventStoreError):
    """An EDGAR response parsed as non-form.idx content (e.g. HTML error page)."""


# ---------------------------------------------------------------------------
# Fundamentals (XBRL) parser class (M3.75 FUND-1 §3.2 raises)
# ---------------------------------------------------------------------------


class FundamentalParseError(EventStoreError):
    """A CompanyFacts document could not be parsed/normalized into FactRows (M3.75 FUND-1 §3.2).

    Raised on the fundamentals fetch/parse path when one company's document is structurally
    unusable (e.g. not a dict, missing the 'facts' key). A SPECIFIC class, not a catch-all:
    the write path (write.py) catches it to quarantine the one CIK (failure_step='parse') and
    continues with the rest of the batch (§3.2), exactly as the Edgar* classes above bound the
    13D fetch failures. The per-record genuine-ambiguity cases the parser itself detects are
    NOT this class — they ride CoverageContribution.normalize_quarantines (failure_step=
    'normalize'); this class is for a whole-document parse failure.
    """


# ---------------------------------------------------------------------------
# Operational classes (briefs I, J, K)
# ---------------------------------------------------------------------------


class OrphanedRunningBatch(EventStoreError):
    """Reaper-driven flip from `running` to `failed`. Brief I raises."""


class Cancelled(EventStoreError):
    """SIGTERM-driven flip; status='cancelled_partial'. Brief D raises."""


# ---------------------------------------------------------------------------
# Correction-front-end classes (brief G's HTTP layer raises; brief B's
# apply_correction is the producer)
# ---------------------------------------------------------------------------


class CorrectionAgainstRunningBatch(EventStoreError):
    """The correction target's batch is still in status='running' (SFv4-1)."""

    def __init__(self, message: str, *, event_id: int, batch_id: int):
        super().__init__(message)
        self.event_id = event_id
        self.batch_id = batch_id


class CorrectionAltersCompanyLinks(EventStoreError):
    """Correction's `companies` array diffs from stored research_event_companies (round-5 correction 2).

    Carries the set-diff payload the §6.8 admin endpoint surfaces back to
    the operator as the 409 error body.
    """

    def __init__(self, message: str, *, event_id: int,
                 added: list[dict], removed: list[dict]):
        super().__init__(message)
        self.event_id = event_id
        self.added = added
        self.removed = removed


# ---------------------------------------------------------------------------
# Reverse lookup: subclass NAME → CHECK-enum value on batches.failure_class
# ---------------------------------------------------------------------------

# Helper for the writer's failure-class-resolution path. Per-record classes
# escalate to RecordValidationAbort under abort_on_first_error; the four
# loader/network classes keep their own name; the operational classes
# (OrphanedRunningBatch, Cancelled) are set by their owning subsystem.
BATCH_LEVEL_FAILURE_CLASSES = {
    "EdgarHttp5xx",
    "EdgarHttp429Persistent",
    "EdgarNetworkTimeout",
    "EdgarParseFatal",
    "LoaderFileUnreadable",
    "LoaderEnvelopeInvalid",
    "LoaderJsonDecodeError",
    "TickerNotFoundInBridge",
    "RecordValidationAbort",
    "OrphanedRunningBatch",
    "Cancelled",
}
