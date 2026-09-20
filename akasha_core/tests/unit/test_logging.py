"""Structured logging + redaction tests."""

from __future__ import annotations

import json
import logging

from paperintel.operations.logging import (
    CONTEXT_FIELDS,
    JsonLogFormatter,
    get_logger,
    redact_value,
    setup_logging,
)


def _capture(record_kwargs: dict) -> dict:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name=record_kwargs.pop("name", "test.module"),
        level=record_kwargs.pop("level", logging.INFO),
        pathname=__file__,
        lineno=1,
        msg=record_kwargs.pop("msg", "event happened"),
        args=None,
        exc_info=None,
    )
    for key, value in record_kwargs.items():
        setattr(record, key, value)
    return json.loads(formatter.format(record))


def test_json_line_required_fields() -> None:
    payload = _capture({"msg": "extraction finished"})
    assert payload["level"] == "INFO"
    assert payload["module"] == "test.module"
    assert payload["event"] == "extraction finished"
    assert "timestamp" in payload


def test_context_fields_promoted() -> None:
    payload = _capture(
        {
            "msg": "task state change",
            "paper_id": "pap_x",
            "job_id": "job_x",
            "task_id": "tsk_x",
            "trace_id": "trc_x",
            "error_code": "LLM_003",
            "retryable": True,
            "duration_ms": 42,
        }
    )
    assert payload["paper_id"] == "pap_x"
    assert payload["job_id"] == "job_x"
    assert payload["task_id"] == "tsk_x"
    assert payload["trace_id"] == "trc_x"
    assert payload["error_code"] == "LLM_003"
    assert payload["retryable"] is True
    assert payload["duration_ms"] == 42


def test_required_context_field_names_cover_spec() -> None:
    """Spec doc 06 §6 required context fields."""
    expected = {
        "paper_id",
        "paper_version_id",
        "job_id",
        "task_id",
        "run_id",
        "trace_id",
        "provider_id",
        "model_id",
        "error_code",
        "retryable",
        "duration_ms",
        "module",
        "event",
    }
    assert expected <= set(CONTEXT_FIELDS)


def test_secret_keys_redacted() -> None:
    payload = _capture(
        {
            "msg": "calling provider",
            "api_key": "sk-SUPERSECRETVALUE",
            "authorization": "Bearer abc123",
            "nested": {"password": "hunter2", "safe": 1},
        }
    )
    dumped = json.dumps(payload)
    assert "sk-SUPERSECRETVALUE" not in dumped
    assert "hunter2" not in dumped
    assert "abc123" not in dumped
    assert payload["api_key"] == "***REDACTED***"
    assert payload["nested"]["password"] == "***REDACTED***"
    assert payload["nested"]["safe"] == 1


def test_secret_patterns_scrubbed_from_messages() -> None:
    assert "SECRET" not in redact_value("msg", "header Authorization: Bearer SECRET-TOKEN")
    assert "sk-abcdefgh12345678" not in redact_value("msg", "key was sk-abcdefgh12345678 ok")


def test_redact_value_recursion() -> None:
    value = {"a": [{"token": "t0k"}, "plain"], "b": {"api_key": "k"}}
    redacted = redact_value("root", value)
    assert redacted["a"][0]["token"] == "***REDACTED***"
    assert redacted["a"][1] == "plain"
    assert redacted["b"]["api_key"] == "***REDACTED***"


def test_get_logger_adapter_binds_context() -> None:
    logger = get_logger("extraction.pdf", paper_id="pap_y", trace_id="trc_y")
    assert logger.extra["module_id"] == "extraction.pdf"  # type: ignore[index]
    assert logger.extra["paper_id"] == "pap_y"  # type: ignore[index]


def test_setup_logging_does_not_crash() -> None:
    setup_logging("DEBUG", json_output=True)
    logging.getLogger("paperintel.test").info("hello")
    setup_logging("INFO", json_output=False)
