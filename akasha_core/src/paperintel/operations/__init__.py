"""Operations namespace: logging, health, metrics, debug bundles (spec doc 02 §4)."""

from paperintel.operations.health import HealthRegistry
from paperintel.operations.logging import (
    CONTEXT_FIELDS,
    REDACTED_KEYS,
    JsonLogFormatter,
    get_logger,
    redact_value,
    setup_logging,
)

__all__ = [
    "CONTEXT_FIELDS",
    "REDACTED_KEYS",
    "HealthRegistry",
    "JsonLogFormatter",
    "get_logger",
    "redact_value",
    "setup_logging",
]
