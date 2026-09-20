"""Error catalog and domain exceptions (frozen namespaces, spec doc 03 §11-12)."""

from paperintel.errors.catalog import (
    ERROR_CATALOG,
    ERROR_NAMESPACES,
    REQUIRED_MINIMUM_CODES,
    ErrorSpec,
    Severity,
    get_error_spec,
    namespace_of,
)
from paperintel.errors.exceptions import (
    DomainError,
    InvalidStateTransitionError,
    PaperIntelError,
)

__all__ = [
    "ERROR_CATALOG",
    "ERROR_NAMESPACES",
    "REQUIRED_MINIMUM_CODES",
    "DomainError",
    "ErrorSpec",
    "InvalidStateTransitionError",
    "PaperIntelError",
    "Severity",
    "get_error_spec",
    "namespace_of",
]
