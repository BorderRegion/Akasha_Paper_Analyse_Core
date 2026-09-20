"""triage.budget — analysis budget policy (P10, doc 07 §2).

The tier decides which stages and which agents run. This is the single
place that maps tier → allowed work, so "T0 does not schedule T3-only
work" is structural rather than scattered conditionals.

Stage ladder (doc 07 §2):
- T0_INDEX: ingest, structure, evidence, search index (identity, source,
  PDF, metadata, abstract, references, basic tags, search) — no deep
  method analysis, no multi-agent verification, no corpus audits.
- T1_SCAN: adds triage + the overview agents (research question,
  contribution, method/result overview, lightweight reliability).
- T2_FULL: adds metadata resolution, the full analysis suite,
  verification, entity linking.
- T3_DEEP: adds falsification depth, numeric audit, external novelty,
  figure/table inspection — i.e. the complete applicable verifier set and
  the complete agent suite.
"""

from __future__ import annotations

from dataclasses import dataclass

from paperintel.errors import DomainError
from paperintel.schemas.enums import PipelineStage, ResourceTier

#: Minimum tier required to run each pipeline stage (doc 07 §2 ladder).
#: Stages absent from this map are ingest-tier work (always allowed).
STAGE_MIN_TIER: dict[PipelineStage, ResourceTier] = {
    PipelineStage.TRIAGED: ResourceTier.T1_SCAN,
    PipelineStage.ANALYZED: ResourceTier.T1_SCAN,
    PipelineStage.METADATA_RESOLVED: ResourceTier.T2_FULL,
    PipelineStage.VERIFIED: ResourceTier.T2_FULL,
    PipelineStage.SYNTHESIZED: ResourceTier.T2_FULL,
    PipelineStage.LINKED: ResourceTier.T2_FULL,
    # Search indexing is T0 scope: identity/metadata/abstract must be
    # findable even for papers that get no analysis (doc 07 §2 T0).
    PipelineStage.SEARCH_INDEXED: ResourceTier.T0_INDEX,
    PipelineStage.CORPUS_READY: ResourceTier.T3_DEEP,
}

#: Agents allowed per tier (overview agents at T1; the full suite at T2+).
_T1_AGENTS: tuple[str, ...] = (
    "agents.structural",
    "agents.research_question",
    "agents.contribution",
    "agents.method",
    "agents.result",
    "agents.reliability",
)
_ALL_AGENTS_SENTINEL = "*"


@dataclass(slots=True, frozen=True)
class AnalysisBudget:
    """What one paper at a given tier is allowed to consume."""

    tier: ResourceTier
    stages: frozenset[PipelineStage]
    agent_types: tuple[str, ...] | None  # None = all registered agents
    max_claims: int
    context_units: int
    verifier_depth: ResourceTier
    description: str

    def allows_stage(self, stage: PipelineStage) -> bool:
        return stage in self.stages


#: Per-tier budget table (doc 07 §2 + §3 policy defaults).
_BUDGETS: dict[ResourceTier, AnalysisBudget] = {
    ResourceTier.T0_INDEX: AnalysisBudget(
        tier=ResourceTier.T0_INDEX,
        stages=frozenset(
            {
                PipelineStage.IMPORTED,
                PipelineStage.FINGERPRINTED,
                PipelineStage.PDF_INSPECTED,
                PipelineStage.EXTRACTED,
                PipelineStage.STRUCTURED,
                PipelineStage.EVIDENCE_INDEXED,
                PipelineStage.SEARCH_INDEXED,
            }
        ),
        agent_types=(),
        max_claims=0,
        context_units=0,
        verifier_depth=ResourceTier.T0_INDEX,
        description="identity, source, metadata, abstract, references, basic tags, search index",
    ),
    ResourceTier.T1_SCAN: AnalysisBudget(
        tier=ResourceTier.T1_SCAN,
        stages=frozenset(
            {
                PipelineStage.IMPORTED,
                PipelineStage.FINGERPRINTED,
                PipelineStage.PDF_INSPECTED,
                PipelineStage.EXTRACTED,
                PipelineStage.STRUCTURED,
                PipelineStage.EVIDENCE_INDEXED,
                PipelineStage.TRIAGED,
                PipelineStage.ANALYZED,
                PipelineStage.SEARCH_INDEXED,
            }
        ),
        agent_types=_T1_AGENTS,
        max_claims=20,
        context_units=30,
        verifier_depth=ResourceTier.T1_SCAN,
        description="overview analysis: RQ, contribution, method/result overview, light reliability",
    ),
    ResourceTier.T2_FULL: AnalysisBudget(
        tier=ResourceTier.T2_FULL,
        stages=frozenset(
            {
                PipelineStage.IMPORTED,
                PipelineStage.FINGERPRINTED,
                PipelineStage.PDF_INSPECTED,
                PipelineStage.EXTRACTED,
                PipelineStage.STRUCTURED,
                PipelineStage.EVIDENCE_INDEXED,
                PipelineStage.TRIAGED,
                PipelineStage.ANALYZED,
                PipelineStage.METADATA_RESOLVED,
                PipelineStage.VERIFIED,
                PipelineStage.SYNTHESIZED,
                PipelineStage.LINKED,
                PipelineStage.SEARCH_INDEXED,
            }
        ),
        agent_types=None,  # full registered suite
        max_claims=60,
        context_units=60,
        verifier_depth=ResourceTier.T2_FULL,
        description="full analysis, verification and entity linking",
    ),
    ResourceTier.T3_DEEP: AnalysisBudget(
        tier=ResourceTier.T3_DEEP,
        stages=frozenset(PipelineStage),
        agent_types=None,
        max_claims=120,
        context_units=120,
        verifier_depth=ResourceTier.T3_DEEP,
        description="deep analysis: complete agent suite and complete applicable verifier set",
    ),
}


def budget_for_tier(tier: ResourceTier) -> AnalysisBudget:
    try:
        return _BUDGETS[tier]
    except KeyError as exc:  # pragma: no cover - enum is frozen
        raise DomainError(
            "CFG_002",
            message=f"No analysis budget defined for tier {tier.value}",
            details={"tier": tier.value},
        ) from exc


def stage_allowed(tier: ResourceTier, stage: PipelineStage) -> bool:
    """Whether the tier's budget runs this stage."""
    return budget_for_tier(tier).allows_stage(stage)


def minimum_tier_for_stage(stage: PipelineStage) -> ResourceTier:
    """The lowest tier that runs a stage (T0 for ingest-tier work)."""
    return STAGE_MIN_TIER.get(stage, ResourceTier.T0_INDEX)


def agent_allowed(tier: ResourceTier, agent_type: str) -> bool:
    """Whether the tier's budget runs a given agent."""
    allowed = budget_for_tier(tier).agent_types
    if allowed is None:
        return True
    return agent_type in allowed


def tier_skip_reason(tier: ResourceTier, stage: PipelineStage) -> str:
    """Human-readable reason a stage is skipped at a tier (recorded on the
    SKIPPED task so the scope decision is explainable)."""
    required = minimum_tier_for_stage(stage)
    return (
        f"stage {stage.value} requires {required.value}; "
        f"this paper's effective tier is {tier.value} "
        "(policy scope, not an unexplained missing stage)"
    )
