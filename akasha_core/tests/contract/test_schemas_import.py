"""All schemas import (P00 gate check P00-C06).

Walks the paperintel package tree and imports every module: import errors,
circular imports and enum typos all surface here.
"""

from __future__ import annotations

import importlib
import pkgutil

import paperintel


def test_every_package_module_imports() -> None:
    failures: list[tuple[str, str]] = []
    imported = 0
    for module_info in pkgutil.walk_packages(paperintel.__path__, prefix="paperintel."):
        name = module_info.name
        try:
            importlib.import_module(name)
            imported += 1
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    assert imported >= 15, f"expected a real package tree, imported only {imported}"
    assert not failures, f"modules failed to import: {failures}"


def test_schema_module_exports_nonempty() -> None:
    from paperintel import schemas

    missing = [name for name in schemas.__all__ if not hasattr(schemas, name)]
    assert not missing
    assert len(schemas.__all__) >= 60


def test_key_contracts_are_present() -> None:
    from paperintel import schemas

    required = [
        # doc 03 §1
        "Paper",
        "PaperVersion",
        "Asset",
        "Evidence",
        "Claim",
        "ClaimEvidenceLink",
        "Verification",
        "ModelCall",
        "AnalysisRun",
        "ExternalProvenance",
        # doc 03 §2-3
        "AgentRequest",
        "AgentResult",
        "ClaimCandidate",
        # doc 03 §5-7
        "Section",
        "TaskContract",
        "JobContract",
        # doc 03 §10-11
        "ModuleHealthRecord",
        "ErrorEnvelope",
        # doc 03 §14-16
        "TechniqueEntity",
        "QualityDimensionAssessment",
        "TriageResult",
        # doc 04
        "SystemStatus",
        "AuditSummary",
        "PaperContextResponse",
        "PipelineStatusResponse",
        "LlmProviderStatus",
        "OcrProviderStatus",
        "JobProgress",
        # doc 05 / templates
        "GateReport",
        "GateCheck",
        "DebugBundleManifest",
        "ModuleManifest",
        "ProviderConfigFile",
        # doc 06
        "ModelQualityProfile",
        "SanityCheckResult",
        "TraceEvent",
    ]
    missing = [name for name in required if not hasattr(schemas, name)]
    assert not missing, f"missing contracts: {missing}"
