"""operations.debug — redacted debug bundles (P12, doc 04 §13).

A debug bundle answers "what happened" without leaking secrets:
- job/task state, attempts, error codes and structured error details;
- trace events for the job (or trace);
- model-call audit rows (provider/model/prompt-version/hashes);
- provider configuration SUMMARY (families, kinds) — never keys or headers.

Redaction is a whitelist discipline: only known-safe fields are copied out
of stored JSON, and a final scrub pass removes anything that looks like a
credential (sk-..., bearer tokens, api key fields).
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import JobRow, ModelCallRow, TaskRow
from paperintel.errors import DomainError
from paperintel.schemas.enums import RetentionClass

#: Patterns that must never survive into a bundle.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("provider_api_key", re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED_API_KEY]"),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (
        "credential_assignment",
        re.compile(r"(?i)(api[_-]?key|token|password|secret)\"?\s*[:=]\s*\"?[^\s\",}]{4,}"),
        r"\1: [REDACTED]",
    ),
)

#: Field names whose VALUES are always dropped.
_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "headers",
        "token",
        "password",
        "secret",
        "credentials",
        "env",
        "request_headers",
    }
)


def redact(value: Any) -> Any:
    """Recursively redact secrets from a JSON-ish structure."""
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if sensitive_key(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for _name, pattern, replacement in _SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    return value


def sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return key.lower() in _SENSITIVE_FIELDS or any(
        marker in normalized for marker in (
            "apikey", "secret", "password", "credential", "authorization", "cookie",
            "accesstoken", "refreshtoken", "accesskey", "csrftoken",
        )
    )


def redact_text(text: str) -> str:
    """Redact a plain string (log lines, exception messages)."""
    return redact(text)


def build_debug_bundle(session: Session, identifier: str) -> dict[str, Any]:
    """Bundle for a job id (job_...) or trace id (trc_...)."""
    if identifier.startswith("job_"):
        job = session.get(JobRow, identifier)
        if job is None:
            raise DomainError(
                "CFG_002", message=f"Unknown job ID: {identifier}", details={"job_id": identifier}
            )
        return _job_bundle(session, job)
    if identifier.startswith("trc_"):
        from paperintel.operations import tracing

        events = tracing.read_trace(session, identifier)
        if not events:
            raise DomainError(
                "CFG_002",
                message=f"Unknown trace ID: {identifier}",
                details={"trace_id": identifier},
            )
        job_ids = sorted({event.job_id for event in events if event.job_id})
        jobs = [session.get(JobRow, job_id) for job_id in job_ids]
        return {
            "kind": "trace",
            "trace_id": identifier,
            "events": [
                {
                    "kind": event.kind,
                    "message": redact_text(event.message),
                    "task_id": event.task_id,
                    "job_id": event.job_id,
                    "occurred_at": event.occurred_at.isoformat(),
                    "data": redact(event.data or {}),
                }
                for event in events
            ],
            "jobs": [job.job_id for job in jobs if job is not None],
            "redaction": _redaction_metadata(),
        }
    raise DomainError(
        "CFG_002",
        message=(
            f"Unsupported debug bundle identifier {identifier!r}: expected a job_ or trc_ ID."
        ),
        details={"identifier": identifier},
    )


def _job_bundle(session: Session, job: JobRow) -> dict[str, Any]:
    from paperintel.operations import tracing

    tasks = session.scalars(
        select(TaskRow).where(TaskRow.job_id == job.job_id).order_by(TaskRow.created_at)
    ).all()
    events = tracing.read_trace(session, job.trace_id) if job.trace_id else []

    model_calls = session.scalars(
        select(ModelCallRow)
        .where(ModelCallRow.task_id.in_([task.task_id for task in tasks] or [""]))
        .order_by(ModelCallRow.created_at)
    ).all()

    return {
        "kind": "job",
        "job": {
            "job_id": job.job_id,
            "paper_id": job.paper_id,
            "paper_version_id": job.paper_version_id,
            "state": job.state.value,
            "current_stage": job.current_stage.value,
            "trace_id": job.trace_id,
            "created_at": job.created_at.isoformat(),
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "module_id": task.module_id,
                "state": task.state.value,
                "attempt": task.attempt,
                "max_attempts": task.max_attempts,
                "error_code": task.error_code,
                "error_details": redact(task.error_details or {}),
                "input_manifest": redact(task.input_manifest or {}),
                "output_manifest": redact(task.output_manifest or {}),
            }
            for task in tasks
        ],
        "trace_events": [
            {
                "kind": event.kind,
                "message": redact_text(event.message),
                "task_id": event.task_id,
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in events
        ],
        "model_calls": [
            {
                "model_call_id": call.model_call_id,
                "provider_id": call.provider_id,
                "model_id": call.model_id,
                "prompt_version_id": call.prompt_version_id,
                "transport_status": call.transport_status.value,
                "schema_status": call.schema_status.value,
                "request_hash": call.request_hash,
                "response_hash": call.response_hash,
                "latency_ms": call.latency_ms,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                # The stored manifest may contain prompt text; secrets are
                # scrubbed and provider headers are never included.
                "request_manifest": redact(call.request_manifest_json or {}),
            }
            for call in model_calls
        ],
        "retention": {
            "class": RetentionClass.DEBUG_TTL.value,
            "note": "debug bundles expire per disk.debug_ttl_days (P10 GC)",
        },
        "redaction": _redaction_metadata(),
    }


def _redaction_metadata() -> dict[str, Any]:
    """What redaction was applied — by NAME, never by pattern text."""
    return {
        "applied": True,
        "patterns": sorted(name for name, _, _ in _SECRET_PATTERNS),
        "sensitive_fields": sorted(_SENSITIVE_FIELDS),
    }


def bundle_to_json(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, ensure_ascii=False, default=str)


def write_debug_zip(session: Session, identifier: str, output, *, settings=None) -> None:
    """Write a sanitized, self-describing ZIP; never include arbitrary files."""
    import zipfile

    from sqlalchemy import text

    from paperintel.config.fingerprint import analysis_config_hash
    from paperintel.services import read_models
    from paperintel.version import PIPELINE_VERSION, SPEC_VERSION

    bundle = build_debug_bundle(session, identifier)
    if bundle["kind"] == "trace":
        bundle["job_details"] = [build_debug_bundle(session, job_id) for job_id in bundle["jobs"]]
    jobs = [bundle] if bundle["kind"] == "job" else bundle.get("job_details", [])
    answers = []
    for job in jobs:
        tasks = job["tasks"]
        failures = [t for t in tasks if t["error_code"] or t["state"] == "FAILED"]
        last_success = None
        for task in tasks:
            if task["state"] in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"):
                last_success = task["task_id"]
            if task in failures:
                answers.append(
                    {
                        "what_failed": task["task_type"],
                        "where": task["module_id"],
                        "last_successful_predecessor": last_success,
                        "error_code": task["error_code"],
                        "retry_count": max(0, task["attempt"] - 1),
                        "paper_id": job["job"]["paper_id"],
                        "job_id": job["job"]["job_id"],
                        "reproduction_command": f"paperctl replay {task['task_id']}",
                    }
                )
        if not failures:
            answers.append(
                {
                    "job_id": job["job"]["job_id"],
                    "paper_id": job["job"]["paper_id"],
                    "what_failed": "No recorded failure",
                    "where": None,
                    "last_successful_predecessor": last_success,
                    "error_code": None,
                    "retry_count": 0,
                    "reproduction_command": "Not applicable",
                }
            )
    metadata = {
        "config_fingerprint": analysis_config_hash(settings=settings),
        "schema_versions": {"spec": SPEC_VERSION, "pipeline": PIPELINE_VERSION},
        "migration_revision": session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar(),
        "module_health": read_models.modules_status(),
        "providers": read_models.providers_status(settings=settings),
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "README.txt",
            "Akasha diagnostic bundle\n"
            "Replay commands create new runs; review before executing.\n\n"
            + bundle_to_json(redact(answers)),
        )
        archive.writestr("bundle.json", bundle_to_json(redact(bundle)))
        archive.writestr("environment.json", bundle_to_json(redact(metadata)))


__all__ = ["bundle_to_json", "build_debug_bundle", "redact", "redact_text"]
