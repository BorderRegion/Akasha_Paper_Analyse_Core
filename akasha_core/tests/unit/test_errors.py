"""Error catalog and envelope tests (P00 gate check P00-C09)."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from paperintel.errors import (
    ERROR_CATALOG,
    ERROR_NAMESPACES,
    REQUIRED_MINIMUM_CODES,
    DomainError,
    InvalidStateTransitionError,
    Severity,
    get_error_spec,
    namespace_of,
)
from paperintel.errors.catalog import _spec  # noqa: PLC2701 - whitebox uniqueness check
from paperintel.schemas.errors import ErrorEnvelope

CATALOG_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "paperintel" / "errors" / "catalog.py"
)


def test_error_codes_unique_in_source() -> None:
    """Codes must be unique in the catalog source itself (a dict would
    silently swallow a redefinition, so check the literal definitions)."""
    source = CATALOG_SOURCE.read_text(encoding="utf-8")
    codes = re.findall(r'_spec\(\s*\n?\s*"([A-Z]+_\d{3})"', source)
    duplicates = [code for code, count in Counter(codes).items() if count > 1]
    assert not duplicates, f"duplicate error codes defined: {duplicates}"
    assert set(codes) == set(ERROR_CATALOG)


def test_all_codes_use_frozen_namespaces() -> None:
    for code in ERROR_CATALOG:
        assert namespace_of(code) in ERROR_NAMESPACES
        assert re.fullmatch(r"[A-Z]+_\d{3}", code), code


def test_frozen_namespace_list_matches_spec() -> None:
    """Spec doc 03 §12 frozen namespace list."""
    expected = (
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
    assert ERROR_NAMESPACES == expected


def test_required_minimum_codes_present() -> None:
    missing = REQUIRED_MINIMUM_CODES - set(ERROR_CATALOG)
    assert not missing, f"spec-mandated minimum codes missing: {sorted(missing)}"
    # Spec doc 03 §12 lists exactly these as the minimum initial set.
    assert len(REQUIRED_MINIMUM_CODES) == 24


@pytest.mark.parametrize("code", sorted(ERROR_CATALOG))
def test_every_code_has_full_metadata(code: str) -> None:
    spec = get_error_spec(code)
    assert spec.code == code
    assert spec.message and spec.message.strip()
    assert isinstance(spec.retryable, bool)
    assert isinstance(spec.severity, Severity)
    assert spec.operator_action and spec.operator_action.strip()


def test_spec_retryable_semantics() -> None:
    """Spot-check retryability mandated by the spec text."""
    assert ERROR_CATALOG["DB_001"].retryable is True
    assert ERROR_CATALOG["LLM_001"].retryable is True
    assert ERROR_CATALOG["LLM_002"].retryable is True
    assert ERROR_CATALOG["CFG_001"].retryable is False
    assert ERROR_CATALOG["CLAIM_001"].retryable is False
    assert ERROR_CATALOG["RESOURCE_001"].retryable is False


def test_unknown_code_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        get_error_spec("LLM_999")
    with pytest.raises(KeyError, match="frozen catalog"):
        DomainError("NOT_A_CODE")


def test_spec_namespace_enforced_at_construction() -> None:
    with pytest.raises(ValueError, match="non-frozen namespace"):
        _spec(
            "BOGUS_001",
            "x",
            retryable=False,
            severity=Severity.ERROR,
            operator_action="y",
        )


def test_domain_error_envelope_shape() -> None:
    """Envelope must match spec doc 03 §11 exactly and validate as a schema."""
    error = DomainError(
        "LLM_003",
        trace_id="trc_01JTESTTESTTESTTESTTESTTEST",
        details={"model_id": "mock"},
    )
    envelope = error.to_envelope()
    assert set(envelope) == {"error"}
    body = envelope["error"]
    assert set(body) == {"code", "message", "retryable", "severity", "trace_id", "details"}
    assert body["code"] == "LLM_003"
    assert body["message"] == "Model response is not valid JSON."
    assert body["retryable"] is True
    assert body["severity"] == "WARNING"
    assert body["trace_id"] == "trc_01JTESTTESTTESTTESTTESTTEST"
    assert body["details"] == {"model_id": "mock"}
    # Round-trip through JSON and the envelope schema.
    validated = ErrorEnvelope.model_validate(json.loads(json.dumps(envelope)))
    assert validated.error.code == "LLM_003"


def test_domain_error_message_override_and_severity_override() -> None:
    error = DomainError("DB_001", message="Custom safe message", severity=Severity.WARNING)
    assert error.message == "Custom safe message"
    assert error.severity is Severity.WARNING
    assert error.retryable is True  # from catalog


def test_invalid_state_transition_error() -> None:
    error = InvalidStateTransitionError("SUCCEEDED", "RUNNING")
    assert error.code == "INTERNAL_002"
    assert error.details == {
        "machine": "task",
        "from_state": "SUCCEEDED",
        "to_state": "RUNNING",
    }
    assert "SUCCEEDED -> RUNNING" in (error.message or "")


def test_envelope_rejects_unknown_code() -> None:
    with pytest.raises(ValueError):
        ErrorEnvelope.model_validate(
            {
                "error": {
                    "code": "NOPE_999",
                    "message": "x",
                    "retryable": False,
                    "severity": "ERROR",
                    "trace_id": None,
                    "details": {},
                }
            }
        )
