"""Canonical Pydantic contracts for PaperIntel (spec version 1.0.0, FROZEN).

Every machine-consumed LLM output and every persisted payload shape is defined
here. Semantics are frozen by the specification; field names map 1:1 to
spec doc 03 unless documented otherwise.
"""

from paperintel.schemas.agent import (
    PAPER_TOOLS,
    AgentConstraints,
    AgentRequest,
    AgentResult,
    EvidenceScope,
)
from paperintel.schemas.claims import (
    CandidateEvidenceRef,
    Claim,
    ClaimCandidate,
    ClaimEvidenceLink,
    ConfidenceComponents,
    QualityDimensionAssessment,
    TechniqueEntity,
    VenueRecord,
    Verification,
)
from paperintel.schemas.common import (
    BBox,
    ExternalProvenance,
    FrozenModel,
    PageRange,
    SchemaVersioned,
    StrictModel,
    utcnow,
)
from paperintel.schemas.enums import (
    PIPELINE_STAGE_ORDER,
    TAG_NAMESPACES,
    TASK_TRANSITIONS,
    TERMINAL_TASK_STATES,
    TIER_ORDER,
    AgentStatus,
    AssetKind,
    CanaryState,
    CheckResult,
    CircuitState,
    ClaimType,
    DataQualityState,
    EntityType,
    EvidenceRole,
    EvidenceType,
    GateResult,
    LlmMockMode,
    ModelRole,
    ModuleHealthState,
    OcrMockMode,
    PageMode,
    PipelineStage,
    ProviderFamily,
    ProviderKind,
    QualityDimension,
    RelationType,
    ResourceTier,
    RetentionClass,
    SanityOutcome,
    SchemaStatus,
    SectionClass,
    SourceMethod,
    SupportState,
    SynthesisSupportClass,
    TaskState,
    TransportStatus,
    VerifierType,
    VerifierVerdict,
    is_valid_task_transition,
    pipeline_stage_index,
)
from paperintel.schemas.errors import ErrorBody, ErrorEnvelope
from paperintel.schemas.evidence import Evidence
from paperintel.schemas.execution import (
    AnalysisRun,
    JobContract,
    JobProgress,
    ModelCall,
    StageStatus,
    TaskContract,
    TraceEvent,
)
from paperintel.schemas.health import (
    AuditSummary,
    DiskBreakdown,
    DiskStatus,
    EvidenceQualityState,
    LlmProviderStatus,
    ModelQualityProfile,
    ModuleHealthRecord,
    OcrProviderStatus,
    PaperContextResponse,
    PipelineStatusResponse,
    QueueStatus,
    SanityCheckResult,
    SystemStatus,
)
from paperintel.schemas.operations import (
    DebugBundleManifest,
    GateCheck,
    GateReport,
    HealthcheckRef,
    ModelRoleConfig,
    ModuleManifest,
    ProviderConfigFile,
    ProviderEntry,
)
from paperintel.schemas.paper import Asset, Paper, PaperVersion, Section
from paperintel.schemas.triage import TriageResult, TriageSignals

__all__ = [n for n in dir() if not n.startswith("_")]
