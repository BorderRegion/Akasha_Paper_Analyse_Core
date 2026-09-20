"""Error catalog integrity (P00 gate check P00-C09: all error codes unique)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from paperintel.errors.catalog import (
    ERROR_CATALOG,
    ERROR_NAMESPACES,
    REQUIRED_MINIMUM_CODES,
)

CATALOG_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "paperintel" / "errors" / "catalog.py"
)


def test_no_duplicate_code_definitions_in_source() -> None:
    """A dict comprehension would silently swallow duplicate codes, so the
    literal _spec(...) definitions are scanned directly."""
    source = CATALOG_SOURCE.read_text(encoding="utf-8")
    codes = re.findall(r'_spec\(\s*\n?\s*"([A-Z]+_\d{3})"', source)
    counts = Counter(codes)
    duplicates = sorted(code for code, count in counts.items() if count > 1)
    assert not duplicates, f"duplicate error code definitions: {duplicates}"
    assert set(codes) == set(ERROR_CATALOG), "catalog keys drifted from definitions"


def test_codes_use_frozen_namespaces_only() -> None:
    for code in ERROR_CATALOG:
        namespace, _, number = code.partition("_")
        assert namespace in ERROR_NAMESPACES, code
        assert re.fullmatch(r"\d{3}", number), code


def test_all_spec_minimum_codes_present() -> None:
    """Spec doc 03 §12 'Minimum required initial codes' — all 24."""
    expected = {
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
    assert REQUIRED_MINIMUM_CODES == expected
    missing = expected - set(ERROR_CATALOG)
    assert not missing


def test_every_entry_has_the_four_required_attributes() -> None:
    """Each code has: retryable, default severity, operator action,
    user-safe message (spec doc 03 §12)."""
    for code, spec in ERROR_CATALOG.items():
        assert spec.code == code
        assert spec.message.strip(), code
        assert isinstance(spec.retryable, bool), code
        assert spec.severity in {"INFO", "WARNING", "ERROR", "CRITICAL"}, code
        assert spec.operator_action.strip(), code


def test_messages_are_user_safe() -> None:
    """Messages must not embed stack-trace-like or secret-like content."""
    for code, spec in ERROR_CATALOG.items():
        lowered = spec.message.lower()
        assert "traceback" not in lowered, code
        assert "api_key=" not in lowered, code
        assert "${" not in spec.message, code
