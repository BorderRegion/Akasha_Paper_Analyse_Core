"""Frozen error catalog (spec doc 03 §12).

Every error code carries:
- ``retryable``: whether a bounded retry is meaningful;
- ``severity``: default severity for logs/envelopes;
- ``operator_action``: what an operator should do;
- ``message``: user-safe message (never leaks stack traces or secrets).

Codes may be added within the frozen namespaces; codes may never be reused or
silently removed while the spec is frozen at 1.0.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


#: Frozen error namespaces (spec doc 03 §12).
ERROR_NAMESPACES: tuple[str, ...] = (
    "CFG",
    "AUTH",
    "STORAGE",
    "DB",
    "REDIS",
    "QUEUE",
    "PDF",
    "OCR",
    "EXTRACT",
    "STRUCTURE",
    "PROVIDER",
    "LLM",
    "EMBED",
    "SCHEMA",
    "EVIDENCE",
    "CLAIM",
    "VERIFY",
    "SEARCH",
    "GRAPH",
    "CORPUS",
    "RESOURCE",
    "INTERNAL",
)


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """Catalog entry describing one error code."""

    code: str
    message: str
    retryable: bool
    severity: Severity
    operator_action: str


def _spec(
    code: str,
    message: str,
    *,
    retryable: bool,
    severity: Severity,
    operator_action: str,
) -> ErrorSpec:
    namespace = code.split("_", 1)[0]
    if namespace not in ERROR_NAMESPACES:
        raise ValueError(f"error code {code!r} uses non-frozen namespace {namespace!r}")
    return ErrorSpec(
        code=code,
        message=message,
        retryable=retryable,
        severity=severity,
        operator_action=operator_action,
    )


#: The canonical error catalog. The spec-mandated minimum codes are all present;
#: additional codes extend the same frozen namespaces.
ERROR_CATALOG: dict[str, ErrorSpec] = {
    spec.code: spec
    for spec in (
        # --- Configuration ---
        _spec(
            "CFG_001",
            "Missing required configuration value.",
            retryable=False,
            severity=Severity.CRITICAL,
            operator_action="Provide the missing key in the config file or environment, then re-run paperctl config validate.",
        ),
        _spec(
            "CFG_002",
            "Invalid configuration value.",
            retryable=False,
            severity=Severity.CRITICAL,
            operator_action="Correct the offending value; run paperctl config validate to confirm.",
        ),
        # --- Auth ---
        _spec(
            "AUTH_001",
            "Missing or invalid API token.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="Send a valid API token in the Authorization header.",
        ),
        # --- Database ---
        _spec(
            "DB_001",
            "Database unavailable.",
            retryable=True,
            severity=Severity.CRITICAL,
            operator_action="Check the PostgreSQL container/service and DATABASE_URL, then retry.",
        ),
        _spec(
            "DB_002",
            "Database schema migration mismatch.",
            retryable=False,
            severity=Severity.CRITICAL,
            operator_action="Run alembic upgrade head (paperctl db status shows expected vs actual revision).",
        ),
        _spec(
            "DB_003",
            "Database constraint violation.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="Inspect the rejected write; this normally indicates a programming error, not an operational one.",
        ),
        # --- Redis / queue ---
        _spec(
            "REDIS_001",
            "Redis unavailable.",
            retryable=True,
            severity=Severity.CRITICAL,
            operator_action="Check the Redis container/service and REDIS_URL, then retry.",
        ),
        _spec(
            "QUEUE_001",
            "Job queue unavailable.",
            retryable=True,
            severity=Severity.ERROR,
            operator_action="Verify broker connectivity and worker heartbeats (paperctl status).",
        ),
        # --- Object storage ---
        _spec(
            "STORAGE_001",
            "Object write failed.",
            retryable=True,
            severity=Severity.ERROR,
            operator_action="Check disk space and data/objects permissions (paperctl disk), then retry the task.",
        ),
        _spec(
            "STORAGE_002",
            "Object content hash mismatch.",
            retryable=False,
            severity=Severity.CRITICAL,
            operator_action="Do not trust the affected object; re-import the source asset and file a bug report with the debug bundle.",
        ),
        _spec(
            "STORAGE_003",
            "Object not found.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="The object was deleted or never committed; re-run the producing stage (paperctl rerun).",
        ),
        # --- PDF ---
        _spec(
            "PDF_001",
            "Unreadable PDF file.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="Verify the file is a well-formed PDF; re-export or re-download the document.",
        ),
        _spec(
            "PDF_002",
            "Encrypted or unsupported PDF.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="Provide an unencrypted PDF or supply credentials via the import options.",
        ),
        # --- OCR ---
        _spec(
            "OCR_001",
            "OCR provider timeout.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="Retry later; if persistent, check the OCR service health (paperctl providers status).",
        ),
        _spec(
            "OCR_002",
            "Invalid OCR provider response.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="Capture a debug bundle and check the OCR service version/configuration.",
        ),
        _spec(
            "OCR_003",
            "Low OCR confidence for extracted text.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="Treat OCR-derived evidence from these pages as degraded; consider a better scan.",
        ),
        # --- Extraction / structure ---
        _spec(
            "EXTRACT_001",
            "Extraction produced no usable text.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="Inspect the PDF (paperctl inspect); the document may be empty or fully image-based without OCR.",
        ),
        _spec(
            "STRUCTURE_001",
            "Section structure reconstruction failed.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="The paper is analyzed with a flat structure; review extraction quality.",
        ),
        # --- Providers ---
        _spec(
            "PROVIDER_001",
            "Provider unavailable.",
            retryable=True,
            severity=Severity.ERROR,
            operator_action="Check provider status and credentials (paperctl providers status).",
        ),
        _spec(
            "PROVIDER_002",
            "Provider rate limited.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="Reduce configured concurrency or wait for the rate-limit window to reset.",
        ),
        _spec(
            "PROVIDER_003",
            "Provider circuit breaker open.",
            retryable=True,
            severity=Severity.ERROR,
            operator_action="The provider failed repeatedly and is temporarily disabled; investigate provider health, then run paperctl providers canary.",
        ),
        # --- LLM ---
        _spec(
            "LLM_001",
            "Model call timed out.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="Retry later; consider raising the provider timeout for large evidence scopes.",
        ),
        _spec(
            "LLM_002",
            "Model call rate limited.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="Reduce concurrency or wait; persistent 429s indicate a plan limit.",
        ),
        _spec(
            "LLM_003",
            "Model response is not valid JSON.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="A constrained repair attempt runs automatically; persistent failures suggest a weaker model or a prompt regression.",
        ),
        _spec(
            "LLM_004",
            "Model response violates the output schema.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="A constrained repair attempt runs automatically; check the model quality profile if persistent.",
        ),
        _spec(
            "LLM_005",
            "Model response failed semantic validation.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="The output was rejected (for example unsupported FACT); review the claim candidates in the debug bundle.",
        ),
        # --- Embedding ---
        _spec(
            "EMBED_001",
            "Embedding provider failure.",
            retryable=True,
            severity=Severity.WARNING,
            operator_action="Check embedding provider configuration; semantic search is degraded until resolved.",
        ),
        # --- Schema firewall ---
        _spec(
            "SCHEMA_001",
            "Persisted payload does not match its declared schema version.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="Do not write unvalidated payloads; check schema_name/schema_version bindings.",
        ),
        # --- Evidence ---
        _spec(
            "EVIDENCE_001",
            "Unknown evidence ID.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="The referenced evidence does not exist; reject the claim candidate and inspect the producing model call.",
        ),
        _spec(
            "EVIDENCE_002",
            "Evidence scope mismatch.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="Evidence belongs to a different paper version than the declared scope; reject the claim candidate.",
        ),
        # --- Claims ---
        _spec(
            "CLAIM_001",
            "Unsupported FACT claim rejected.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="A FACT requires direct supporting evidence; the candidate was not persisted.",
        ),
        # --- Verification ---
        _spec(
            "VERIFY_001",
            "Numeric conflict detected during verification.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="Review the conflicting numbers and their evidence via paperctl audit.",
        ),
        _spec(
            "VERIFY_002",
            "Claim contradiction detected during verification.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="Review the contradicting claims and evidence via paperctl audit.",
        ),
        # --- Search / graph / corpus ---
        _spec(
            "SEARCH_001",
            "Search backend unavailable or not ready.",
            retryable=True,
            severity=Severity.ERROR,
            operator_action="Check database FTS/pgvector readiness (paperctl doctor).",
        ),
        _spec(
            "GRAPH_001",
            "Unknown entity reference.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="The relation references a nonexistent entity; reject the link.",
        ),
        _spec(
            "CORPUS_001",
            "Corpus selection is empty.",
            retryable=False,
            severity=Severity.WARNING,
            operator_action="Widen the collection/query/time-window selection before running corpus analysis.",
        ),
        # --- Resources ---
        _spec(
            "RESOURCE_001",
            "Disk low-water mark reached.",
            retryable=False,
            severity=Severity.CRITICAL,
            operator_action="Run paperctl gc, free disk space, or expand the volume; nonessential work is blocked.",
        ),
        # --- Internal ---
        _spec(
            "INTERNAL_001",
            "Unexpected internal error.",
            retryable=False,
            severity=Severity.CRITICAL,
            operator_action="Capture a debug bundle (paperctl debug-bundle) and file a bug report.",
        ),
        _spec(
            "INTERNAL_002",
            "Invalid state transition.",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="A component attempted an illegal state transition; capture a debug bundle and file a bug report.",
        ),
    )
}


#: The minimum code set mandated by spec doc 03 §12. Gate/tests assert presence.
REQUIRED_MINIMUM_CODES: frozenset[str] = frozenset(
    {
        "CFG_001",
        "CFG_002",
        "DB_001",
        "DB_002",
        "STORAGE_001",
        "PDF_001",
        "PDF_002",
        "OCR_001",
        "OCR_002",
        "OCR_003",
        "PROVIDER_001",
        "PROVIDER_002",
        "PROVIDER_003",
        "LLM_001",
        "LLM_002",
        "LLM_003",
        "LLM_004",
        "LLM_005",
        "EVIDENCE_001",
        "EVIDENCE_002",
        "CLAIM_001",
        "VERIFY_001",
        "VERIFY_002",
        "RESOURCE_001",
    }
)


def get_error_spec(code: str) -> ErrorSpec:
    """Return the catalog entry for ``code``.

    Raises:
        KeyError: if the code is not in the frozen catalog. Callers must never
            invent codes at runtime; unknown codes are programming errors.
    """
    return ERROR_CATALOG[code]


def namespace_of(code: str) -> str:
    return code.split("_", 1)[0]


__all__ = [
    "ERROR_CATALOG",
    "ERROR_NAMESPACES",
    "REQUIRED_MINIMUM_CODES",
    "ErrorSpec",
    "Severity",
    "get_error_spec",
    "namespace_of",
]
