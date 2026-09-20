"""agents.builtin — the core paper analysis agents (P07, spec doc 01 §9).

Every agent subclasses BaseAgent: the framework pipeline (evidence-scoped
context → registered prompt → provider → schema firewall → audit) is
structural. Domain behavior lives in exactly two places: the prompt
(instructions + category vocabulary, registered immutable) and the
context focus (which sections/types the agent sees).

The synthesizer is registered here too, but it is GUARDED: it may only
synthesize verified/allowed structured outputs (doc 01 §9.13) — with no
verified claims available it reports INSUFFICIENT_EVIDENCE (a valid
analytical result), never invents facts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paperintel.agents.base import AgentRunOutcome, BaseAgent, register_agent
from paperintel.schemas.agent import AgentRequest
from paperintel.schemas.enums import SectionClass


def _classes(*classes: SectionClass) -> dict[str, Any]:
    return {"focus_classes": set(classes)}


@register_agent
class StructuralAgent(BaseAgent):
    agent_type = "agents.structural"
    prompt_name = "agents.structural"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        # The whole paper IS the structural agent's scope.
        return {}


@register_agent
class ResearchQuestionAgent(BaseAgent):
    agent_type = "agents.research_question"
    prompt_name = "agents.research_question"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(SectionClass.TITLE, SectionClass.ABSTRACT, SectionClass.INTRODUCTION)


@register_agent
class ContributionAgent(BaseAgent):
    agent_type = "agents.contribution"
    prompt_name = "agents.contribution"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(
            SectionClass.TITLE,
            SectionClass.ABSTRACT,
            SectionClass.INTRODUCTION,
            SectionClass.CONCLUSION,
        )


@register_agent
class MethodAgent(BaseAgent):
    agent_type = "agents.method"
    prompt_name = "agents.method"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(SectionClass.METHOD, SectionClass.THEORY, SectionClass.BACKGROUND)


@register_agent
class ExperimentAgent(BaseAgent):
    agent_type = "agents.experiment"
    prompt_name = "agents.experiment"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(SectionClass.EXPERIMENT, SectionClass.RESULT)


@register_agent
class ResultAgent(BaseAgent):
    agent_type = "agents.result"
    prompt_name = "agents.result"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(SectionClass.RESULT, SectionClass.EXPERIMENT, SectionClass.ABSTRACT)


@register_agent
class TechniqueAgent(BaseAgent):
    agent_type = "agents.technique"
    prompt_name = "agents.technique"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(
            SectionClass.METHOD,
            SectionClass.EXPERIMENT,
            SectionClass.APPENDIX,
            SectionClass.SUPPLEMENTARY,
        )


@register_agent
class ReliabilityAgent(BaseAgent):
    agent_type = "agents.reliability"
    prompt_name = "agents.reliability"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(
            SectionClass.EXPERIMENT,
            SectionClass.RESULT,
            SectionClass.METHOD,
            SectionClass.APPENDIX,
        )


@register_agent
class ReproductionAgent(BaseAgent):
    agent_type = "agents.reproduction"
    prompt_name = "agents.reproduction"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return {}


@register_agent
class LimitationAgent(BaseAgent):
    agent_type = "agents.limitation"
    prompt_name = "agents.limitation"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(
            SectionClass.LIMITATION,
            SectionClass.DISCUSSION,
            SectionClass.CONCLUSION,
        )


@register_agent
class PeopleAgent(BaseAgent):
    agent_type = "agents.people"
    prompt_name = "agents.people"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return {}


@register_agent
class CriticAgent(BaseAgent):
    agent_type = "agents.critic"
    prompt_name = "agents.critic"
    prompt_version = "1.0.0"

    def context_focus(self) -> dict[str, Any]:
        return _classes(
            SectionClass.ABSTRACT,
            SectionClass.INTRODUCTION,
            SectionClass.RESULT,
            SectionClass.CONCLUSION,
        )


@register_agent
class SynthesizerAgent(BaseAgent):
    """May only synthesize verified/allowed structured outputs (doc 01
    §9.13). Without a verified claim set it reports INSUFFICIENT_EVIDENCE —
    the honest analytical result — instead of inventing facts."""

    agent_type = "agents.synthesizer"
    prompt_name = "agents.synthesizer"
    prompt_version = "1.0.0"

    def maybe_short_circuit(
        self, request: AgentRequest, session: Session
    ) -> AgentRunOutcome | None:
        """No verified claims → INSUFFICIENT_EVIDENCE without a model call.

        The synthesizer may not invent facts (doc 01 §9.13): with an empty
        verified claim set there is nothing to synthesize, and calling a
        model would only invite fabrication.
        """
        from paperintel.database.models import ClaimRow
        from paperintel.schemas.agent import AgentResult
        from paperintel.schemas.enums import AgentStatus, SupportState

        verified = session.scalar(
            select(func.count())
            .select_from(ClaimRow)
            .where(
                ClaimRow.paper_version_id == request.paper_version_id,
                ClaimRow.support_state.in_(
                    [SupportState.SUPPORTED, SupportState.PARTIALLY_SUPPORTED]
                ),
            )
        )
        if verified:
            return None
        return AgentRunOutcome(
            result=AgentResult(
                status=AgentStatus.INSUFFICIENT_EVIDENCE,
                observations=["no verified claims available for synthesis"],
            ),
            model_call_id=None,
            warnings=[],
        )

    def build_variables(
        self, session: Session, request: AgentRequest, context: Any
    ) -> dict[str, str]:
        from paperintel.database.models import ClaimRow
        from paperintel.schemas.enums import SupportState

        verified = session.scalars(
            select(ClaimRow)
            .where(
                ClaimRow.paper_version_id == request.paper_version_id,
                ClaimRow.support_state.in_(
                    [SupportState.SUPPORTED, SupportState.PARTIALLY_SUPPORTED]
                ),
            )
            .order_by(ClaimRow.created_at, ClaimRow.claim_id)
        ).all()
        lines = [
            f"[{claim.claim_id}] ({claim.claim_type.value}/{claim.category}) {claim.statement}"
            for claim in verified
        ]
        return {
            **super().build_variables(session, request, context),
            "verified_claims": "\n".join(lines) if lines else "(no verified claims)",
        }
