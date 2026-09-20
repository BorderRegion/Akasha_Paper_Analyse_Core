"""Operations contracts: gate reports, debug bundles, module manifests,
provider configuration files.

Validated against the frozen templates in ``spec/paperintel_final_spec/templates``:
- gate_report.schema.example.json
- debug_bundle_manifest.example.json
- module_manifest.example.yaml
- provider_config.example.yaml
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from paperintel.errors.catalog import ERROR_NAMESPACES
from paperintel.schemas.common import FrozenModel, StrictModel
from paperintel.schemas.enums import CheckResult, GateResult, ProviderKind

# ---------------------------------------------------------------------------
# Gate reports (spec doc 05)
# ---------------------------------------------------------------------------

_PHASE_RE = re.compile(r"^(P\d{2}|FINAL)$")
_CHECK_ID_RE = re.compile(r"^[A-Z0-9]+-C\d{2,}$")


class GateCheck(FrozenModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    result: CheckResult
    #: Where the machine-readable proof comes from, e.g.
    #: ``pytest::tests/contract/test_x.py::test_y`` or ``cmd::paperctl version``.
    evidence: str = ""

    @field_validator("id")
    @classmethod
    def _check_id_shape(cls, value: str) -> str:
        if not _CHECK_ID_RE.match(value):
            raise ValueError(f"check id {value!r} must look like PXX-CNN")
        return value


class GateReport(StrictModel):
    """Gate report file contract (spec doc 05 "Gate report format").

    The gate script determines result from command exit codes and assertions;
    nobody hand-edits ``result: PASS``.
    """

    phase: str = Field(min_length=1)
    spec_version: str = Field(min_length=1)
    result: GateResult
    started_at: datetime
    finished_at: datetime
    checks: list[GateCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    @field_validator("phase")
    @classmethod
    def _phase_shape(cls, value: str) -> str:
        if not _PHASE_RE.match(value):
            raise ValueError(f"phase {value!r} must be PXX or FINAL")
        return value

    @model_validator(mode="after")
    def _result_consistency(self) -> GateReport:
        has_fail = any(c.result is CheckResult.FAIL for c in self.checks)
        if has_fail and self.result is GateResult.PASS:
            raise ValueError("gate report with a FAIL check cannot be PASS")
        if self.checks and not has_fail and self.result is GateResult.FAIL and not self.blockers:
            raise ValueError("gate report FAIL requires a failed check or explicit blockers")
        for check in self.checks:
            if check.result is CheckResult.SKIP and not self.warnings:
                raise ValueError("SKIPPED checks require an explaining warning entry")
        return self


# ---------------------------------------------------------------------------
# Debug bundles (spec doc 04 §13)
# ---------------------------------------------------------------------------


class DebugBundleManifest(FrozenModel):
    """Manifest of a debug bundle ZIP.

    Mandatory redactions: api_keys, authorization_headers, cookies, .env
    (spec doc 01 §22).
    """

    bundle_version: str = Field(min_length=1)
    target_type: Literal["job", "trace", "task", "paper"]
    target_id: str = Field(min_length=1)
    spec_version: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    included: list[str] = Field(default_factory=list)
    redactions: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _mandatory_redactions(self) -> DebugBundleManifest:
        required = {"api_keys", "authorization_headers", "cookies", ".env"}
        missing = required - set(self.redactions)
        if missing:
            raise ValueError(f"debug bundle must declare redactions: {sorted(missing)}")
        return self


# ---------------------------------------------------------------------------
# Module manifests (spec templates/module_manifest.example.yaml)
# ---------------------------------------------------------------------------

_MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class HealthcheckRef(FrozenModel):
    type: Literal["internal", "command", "http"]
    name: str = Field(min_length=1)


class ModuleManifest(FrozenModel):
    """Per-module manifest; module IDs use dot notation (spec doc 02 §4)."""

    module_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    healthcheck: HealthcheckRef
    idempotent: bool = False
    required_checks: list[str] = Field(default_factory=list)
    error_namespaces: list[str] = Field(default_factory=list)

    @field_validator("module_id")
    @classmethod
    def _module_id_shape(cls, value: str) -> str:
        if not _MODULE_ID_RE.match(value):
            raise ValueError(
                f"module_id {value!r} must be dot-notation lowercase, e.g. extraction.pdf"
            )
        return value

    @field_validator("error_namespaces")
    @classmethod
    def _namespaces_frozen(cls, value: list[str]) -> list[str]:
        unknown = [ns for ns in value if ns not in ERROR_NAMESPACES]
        if unknown:
            raise ValueError(f"non-frozen error namespaces in manifest: {unknown}")
        return value


# ---------------------------------------------------------------------------
# Provider configuration (spec templates/provider_config.example.yaml)
# ---------------------------------------------------------------------------


class ModelRoleConfig(FrozenModel):
    model: str = Field(min_length=1)
    max_concurrency: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0)
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderEntry(FrozenModel):
    """One configured provider. API keys are referenced by environment
    variable name only — never inlined (spec doc 01 §22)."""

    kind: ProviderKind
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_seconds: int = Field(default=120, ge=1)
    max_concurrency: int = Field(default=8, ge=1)
    models: dict[str, ModelRoleConfig] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_key_env")
    @classmethod
    def _env_var_shape(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^[A-Z][A-Z0-9_]*$", value):
            raise ValueError(f"api_key_env {value!r} must be an environment variable name")
        return value

    @model_validator(mode="after")
    def _remote_needs_url(self) -> ProviderEntry:
        remote_kinds = {
            ProviderKind.OPENAI_COMPATIBLE,
            ProviderKind.PADDLE_HTTP,
            ProviderKind.HTTP_EMBEDDING,
            ProviderKind.CROSSREF,
            ProviderKind.OPENALEX,
            ProviderKind.SEMANTIC_SCHOLAR,
        }
        if self.kind in remote_kinds:
            if not self.base_url or not re.match(r"^https?://", self.base_url):
                raise ValueError(
                    f"provider kind {self.kind.value} requires an absolute http(s) base_url"
                )
        return self


class ProviderConfigFile(FrozenModel):
    """providers.yaml root document."""

    spec_version: str = Field(min_length=1)
    providers: dict[str, ProviderEntry] = Field(default_factory=dict)

    @field_validator("providers")
    @classmethod
    def _provider_names(cls, value: dict[str, ProviderEntry]) -> dict[str, ProviderEntry]:
        for name in value:
            if not re.match(r"^[a-z][a-z0-9_]*$", name):
                raise ValueError(f"provider name {name!r} must be lowercase snake_case")
        return value


__all__ = [
    "DebugBundleManifest",
    "GateCheck",
    "GateReport",
    "HealthcheckRef",
    "ModelRoleConfig",
    "ModuleManifest",
    "ProviderConfigFile",
    "ProviderEntry",
]
