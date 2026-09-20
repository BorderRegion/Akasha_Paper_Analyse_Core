"""Agent request/result contracts (spec doc 03 §2).

Validated against templates/agent_result.example.json by the P00 gate.
"""

from __future__ import annotations

from pydantic import Field

from paperintel.schemas.claims import ClaimCandidate
from paperintel.schemas.common import (
    FrozenModel,
    LoosePaperId,
    LoosePaperVersionId,
    SchemaVersioned,
    TaskId,
    TraceId,
)
from paperintel.schemas.enums import AgentStatus

#: Tools that may be offered to agents (spec doc 03 §2 example).
PAPER_TOOLS: tuple[str, ...] = (
    "paper.search",
    "paper.get_section",
    "paper.get_evidence",
    "paper.get_table",
    "paper.get_figure",
)


class EvidenceScope(FrozenModel):
    """The evidence an agent is allowed to reason over."""

    section_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class AgentConstraints(FrozenModel):
    must_cite_evidence: bool = True
    allow_external_claims: bool = False


class AgentRequest(FrozenModel):
    """Typed request handed to every agent (spec doc 03 §2)."""

    paper_id: LoosePaperId
    paper_version_id: LoosePaperVersionId
    agent_type: str = Field(min_length=1)
    task_id: TaskId | None = None
    trace_id: TraceId | None = None
    evidence_scope: EvidenceScope = Field(default_factory=EvidenceScope)
    available_tools: list[str] = Field(default_factory=list)
    constraints: AgentConstraints = Field(default_factory=AgentConstraints)


class AgentResult(SchemaVersioned):
    """Typed result returned by every agent (spec doc 03 §2).

    INSUFFICIENT_EVIDENCE is a valid analytical result, not a software
    failure. The system supports it without forcing an answer (doc 00 §7.15).
    """

    status: AgentStatus
    claims: list[ClaimCandidate] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    requests_for_more_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "PAPER_TOOLS",
    "AgentConstraints",
    "AgentRequest",
    "AgentResult",
    "EvidenceScope",
]
