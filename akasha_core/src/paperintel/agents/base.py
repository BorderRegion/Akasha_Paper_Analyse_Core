"""agents.base — the base agent contract (P06, spec doc 02 §6, doc 03 §2).

EVERY paper-specific agent (P07) subclasses BaseAgent: there is no path
around the framework. run() always

  1. builds evidence-scoped context through agents.tools (the ONLY
     retrieval surface; scope-locked to the declared paper version);
  2. renders a REGISTERED prompt version (immutable, hash-verified);
  3. calls the provider with response_json=True;
  4. routes the response through the schema firewall (parse → schema →
     semantics, one constrained repair on LLM_004);
  5. writes the ModelCallRow audit record (doc 03 §1.8) on BOTH success
     and failure paths — the request manifest references immutable
     evidence IDs + the prompt version, never duplicates evidence text.

Agent registration is explicit (AGENTS registry); run_agent refuses
unknown agent types (CFG_002) — no agent runs outside the registry, and no
agent bypasses the firewall.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from paperintel.agents import prompts as prompt_registry
from paperintel.agents.firewall import (
    FirewallOutcome,
    run_semantic_validation,
    schema_status_for_error,
    validate_response,
)
from paperintel.agents.tools import build_evidence_context
from paperintel.database.models import AnalysisRunRow, ModelCallRow
from paperintel.errors import DomainError
from paperintel.ids import new_model_call_id, new_run_id
from paperintel.providers.base import ChatMessage, LLMProvider, LlmRequest, LlmResponse
from paperintel.schemas.agent import AgentRequest, AgentResult
from paperintel.schemas.enums import (
    AgentStatus,
    ModelRole,
    SchemaStatus,
    TaskState,
    TransportStatus,
)

_active_run = contextvars.ContextVar("agent_analysis_run", default=None)

#: Registered agent classes (P07 fills this through register_agent).
AGENTS: dict[str, type[BaseAgent]] = {}


def register_agent(agent_class: type[BaseAgent]) -> type[BaseAgent]:
    """Explicit registration — the only way an agent becomes runnable."""
    agent_type = agent_class.agent_type
    if agent_type in AGENTS:
        raise ValueError(f"Agent type already registered: {agent_type}")
    AGENTS[agent_type] = agent_class
    return agent_class


@dataclass(slots=True)
class AgentRunOutcome:
    """Framework-level wrapper: the frozen AgentResult plus the audit
    linkage (model call id) and framework warnings collected on the way."""

    result: AgentResult
    #: None when the agent short-circuited before any model call (e.g.
    # the synthesizer without verified claims) — there was nothing to
    # audit.
    model_call_id: str | None
    run_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: Evidence IDs actually included in the prompt context (audit trail).
    context_evidence_ids: list[str] = field(default_factory=list)


async def run_agent(
    request: AgentRequest,
    session: Session,
    *,
    provider: LLMProvider,
    model_role: ModelRole | None = None,
) -> AgentRunOutcome:
    """Run the REGISTERED agent named by request.agent_type.

    Unknown types fail loudly (CFG_002).
    """
    agent_class = AGENTS.get(request.agent_type)
    if agent_class is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown agent type: {request.agent_type!r} (not registered).",
            details={"agent_type": request.agent_type, "registered": sorted(AGENTS)},
        )
    agent = agent_class()
    if model_role is not None:
        agent.model_role = model_role
    return await agent.run(request, session, provider=provider)


class BaseAgent:
    """Framework contract. Subclasses declare identity + prompt binding;
    they may override context_focus() and build_variables() for domain
    scoping — they cannot override the pipeline itself."""

    #: Unique agent type string (e.g. "agents.method" in P07).
    agent_type: str = ""
    #: Registered prompt name + version (must exist in prompt registry).
    prompt_name: str = ""
    prompt_version: str = ""
    #: Output schema validated by the firewall (frozen AgentResult unless a
    #: P07 agent needs a stricter subclass).
    output_schema: type[AgentResult] = AgentResult
    model_role: ModelRole = ModelRole.ANALYST

    def __init__(self) -> None:
        if not self.agent_type or not self.prompt_name:
            raise ValueError(f"{type(self).__name__} must declare agent_type and prompt_name")

    # -- context scoping hooks ------------------------------------------------

    def context_focus(self) -> dict[str, Any]:
        """Evidence-scope narrowing (types/sections) for this agent.

        Keys subset of: focus_types, focus_sections, include_quality,
        max_units."""
        return {}

    def build_variables(
        self, session: Session, request: AgentRequest, context: Any
    ) -> dict[str, str]:
        """Prompt variables; subclasses may extend (never bypass)."""
        return {
            "paper_id": request.paper_id,
            "paper_version_id": request.paper_version_id,
            "agent_type": self.agent_type,
            "evidence_context": context.rendered,
        }

    # -- the framework pipeline ------------------------------------------------

    def maybe_short_circuit(
        self, request: AgentRequest, session: Session
    ) -> AgentRunOutcome | None:
        """Optional guard evaluated BEFORE any provider call.

        Returning an outcome short-circuits the run with a valid
        analytical result (no model call, no audit row). Used by agents
        whose inputs are structurally insufficient — e.g. the synthesizer
        without verified claims. Never a substitute for validation: the
        returned result must still be an honest analytical answer.
        """
        return None

    async def run(
        self,
        request: AgentRequest,
        session: Session,
        *,
        provider: LLMProvider,
    ) -> AgentRunOutcome:
        """One evidence-scoped, firewall-validated, audited agent run."""
        from paperintel.config.fingerprint import analysis_config_hash
        from paperintel.schemas.common import utcnow
        from paperintel.version import PIPELINE_VERSION

        run = AnalysisRunRow(
            run_id=new_run_id(),
            paper_id=request.paper_id,
            paper_version_id=request.paper_version_id,
            agent_type=self.agent_type,
            pipeline_version=PIPELINE_VERSION,
            prompt_version=self.prompt_version,
            config_hash=analysis_config_hash(
                agent=self.agent_type, model_role=self.model_role.value
            ),
            model_id="not_called",
            provider_id=provider.provider_id,
            status=TaskState.RUNNING,
            started_at=utcnow(),
            trace_id=request.trace_id,
        )
        session.add(run)
        session.flush()
        token = _active_run.set(run.run_id)
        try:
            outcome = await self._run_impl(request, session, provider=provider)
            outcome.run_id = run.run_id
            run.status = (
                TaskState.SUCCEEDED_WITH_WARNINGS if outcome.warnings else TaskState.SUCCEEDED
            )
            return outcome
        except Exception:
            run.status = TaskState.FAILED
            raise
        finally:
            run.finished_at = utcnow()
            # The caller owns flushing/rollback. Flushing here can replace an
            # original database error with PendingRollbackError.
            _active_run.reset(token)

    async def _run_impl(self, request, session, *, provider):
        short = self.maybe_short_circuit(request, session)
        if short is not None:
            return short

        # 1. Evidence-scoped context (agents.tools — the only retrieval path).
        context = build_evidence_context(
            session,
            request.paper_version_id,
            **self.context_focus(),
        )
        if request.evidence_scope.evidence_ids:
            context = context.restrict_to(request.evidence_scope.evidence_ids)

        # 2. Registered prompt (versioned, immutable).
        prompt_row = prompt_registry.get_prompt(session, self.prompt_name, self.prompt_version)
        from paperintel.config.fingerprint import analysis_config_hash

        run = session.get(AnalysisRunRow, _active_run.get())
        run.config_hash = analysis_config_hash(
            agent=self.agent_type,
            model_role=self.model_role.value,
            provider=provider.provider_id,
            model=provider.model_for(self.model_role) if hasattr(provider, "model_for") else "mock",
            prompt_version_id=prompt_row.prompt_version_id,
            prompt_sha256=hashlib.sha256(prompt_row.template_body.encode()).hexdigest(),
        )
        variables = self.build_variables(session, request, context)
        rendered = prompt_registry.render_template(prompt_row.template_body, variables)

        system = f"agent={self.agent_type} prompt={self.prompt_name}@{self.prompt_version}"
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=rendered),
        ]
        llm_request = LlmRequest(
            messages=messages,
            model_role=self.model_role,
            response_json=True,
            context={
                "agent_type": self.agent_type,
                "paper_version_id": request.paper_version_id,
                "prompt_version_id": prompt_row.prompt_version_id,
                "trace_id": request.trace_id,
                "task_id": request.task_id,
                "run_id": _active_run.get(),
            },
        )

        # 3. Provider call (P02 resilience wraps transport errors).
        async def invoke(call_request):
            started = time.monotonic()
            try:
                return await provider.complete(call_request)
            except DomainError as exc:
                transport = {
                    "LLM_001": TransportStatus.TIMEOUT,
                    "LLM_002": TransportStatus.RATE_LIMITED,
                    "PROVIDER_002": TransportStatus.RATE_LIMITED,
                }.get(exc.code, TransportStatus.UNKNOWN)
                model = (
                    provider.model_for(call_request.model_role)
                    if hasattr(provider, "model_for")
                    else "mock"
                )
                self._record_model_call(
                    session,
                    request=request,
                    llm_request=call_request,
                    response=LlmResponse(
                        content="",
                        provider_id=provider.provider_id,
                        model_id=model,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    ),
                    prompt_version_id=prompt_row.prompt_version_id,
                    schema_status=SchemaStatus.NOT_RUN,
                    evidence_ids=context.evidence_ids,
                    transport_status=transport,
                )
                raise

        response = await invoke(llm_request)

        # 4. Schema firewall (parse → schema → semantics; exactly ONE
        # constrained repair attempt on LLM_004 — doc 02 §6). The repair
        # is itself a model call and is awaited in this async context;
        # every model call (failed or accepted) leaves an audit row
        # (doc 03 §1.8).
        audit_kwargs = {
            "request": request,
            "llm_request": llm_request,
            "prompt_version_id": prompt_row.prompt_version_id,
            "evidence_ids": context.evidence_ids,
        }
        accepted_response = response
        stats = getattr(provider, "stats", None)
        try:
            outcome: FirewallOutcome = validate_response(response.content, self.output_schema)
        except DomainError as exc:
            if stats:
                stats.record_schema(False)
            if exc.code != "LLM_004":
                self._record_model_call(
                    session,
                    response=response,
                    schema_status=schema_status_for_error(exc.code),
                    **audit_kwargs,
                )
                raise
            # Audit the schema-violating call, then one repair attempt.
            self._record_model_call(
                session,
                response=response,
                schema_status=schema_status_for_error(exc.code),
                **audit_kwargs,
            )
            repair_request = LlmRequest(
                messages=messages
                + [
                    ChatMessage(role="assistant", content=response.content),
                    ChatMessage(
                        role="user",
                        content=(
                            "Your previous response violated the JSON schema: "
                            f"{exc.details.get('errors', [])}. "
                            "Return the corrected JSON object only."
                        ),
                    ),
                ],
                model_role=self.model_role,
                response_json=True,
                context=llm_request.context,
            )
            repair_response = await invoke(repair_request)
            audit_kwargs["llm_request"] = repair_request
            try:
                outcome = validate_response(repair_response.content, self.output_schema)
            except DomainError as repair_exc:
                if stats:
                    stats.record_schema(False)
                self._record_model_call(
                    session,
                    response=repair_response,
                    schema_status=schema_status_for_error(repair_exc.code),
                    **audit_kwargs,
                )
                raise
            accepted_response = repair_response
            outcome = FirewallOutcome(
                status=SchemaStatus.PASSED_AFTER_REPAIR,
                payload=outcome.payload,
                notes=outcome.notes + ["schema repair succeeded"],
            )

        if stats:
            stats.record_schema(True)
        result = outcome.payload
        if not isinstance(result, AgentResult):
            raise TypeError("Agent response must be an AgentResult")

        # 5. Semantic validation bound to the version scope.
        try:
            result, semantic_warnings = run_semantic_validation(
                result, paper_version_id=request.paper_version_id, session=session
            )
        except DomainError as exc:
            if stats:
                stats.record_citation(False)
            self._record_model_call(
                session,
                request=request,
                llm_request=audit_kwargs["llm_request"],
                response=accepted_response,
                prompt_version_id=prompt_row.prompt_version_id,
                schema_status=schema_status_for_error(exc.code),
                evidence_ids=context.evidence_ids,
            )
            raise

        if stats:
            stats.record_citation(True)
            for warning in semantic_warnings:
                if warning.startswith("CLAIM_001"):
                    stats.record_unsupported_claim()

        # 6. Audit record for the ACCEPTED call.
        model_call_id = self._record_model_call(
            session,
            response=accepted_response,
            schema_status=outcome.status,
            **audit_kwargs,
        )

        warnings = list(outcome.notes) + semantic_warnings
        if context.truncated:
            warnings.append(f"evidence context truncated ({context.total_available} available)")
        if warnings and result.status is AgentStatus.SUCCESS:
            result = result.model_copy(update={"status": AgentStatus.SUCCESS_WITH_WARNINGS})

        return AgentRunOutcome(
            result=result,
            model_call_id=model_call_id,
            warnings=warnings,
            context_evidence_ids=context.evidence_ids,
        )

    # -- audit ------------------------------------------------------------------

    def _record_model_call(
        self,
        session: Session,
        *,
        request: AgentRequest,
        llm_request: LlmRequest,
        response: LlmResponse,
        prompt_version_id: str,
        schema_status: SchemaStatus,
        evidence_ids: list[str],
        transport_status: TransportStatus = TransportStatus.OK,
    ) -> str:
        """Persist the immutable ModelCallRow audit record (doc 03 §1.8).

        The request manifest references evidence IDs + prompt version —
        never duplicates evidence text.
        """
        request_manifest = {
            "agent_type": self.agent_type,
            "paper_version_id": request.paper_version_id,
            "messages": [{"role": m.role, "content": m.content} for m in llm_request.messages],
            "evidence_ids": evidence_ids,
            "prompt_version_id": prompt_version_id,
            "model_role": llm_request.model_role.value,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_manifest, sort_keys=True).encode("utf-8")
        ).hexdigest()
        response_hash = hashlib.sha256(response.content.encode("utf-8")).hexdigest()
        model_call_id = new_model_call_id()
        run = session.get(AnalysisRunRow, _active_run.get()) if _active_run.get() else None
        if run is not None:
            run.model_id = response.model_id or "unknown"
            run.provider_id = response.provider_id or "unknown"
        session.add(
            ModelCallRow(
                model_call_id=model_call_id,
                provider_id=response.provider_id or "unknown",
                model_id=response.model_id or "unknown",
                task_id=request.task_id,
                run_id=_active_run.get(),
                trace_id=request.trace_id,
                prompt_version_id=prompt_version_id,
                request_manifest_json=request_manifest,
                request_hash=request_hash,
                response_object_hash=None,
                response_hash=response_hash if transport_status is TransportStatus.OK else None,
                transport_status=transport_status,
                schema_status=schema_status,
                latency_ms=response.latency_ms,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )
        session.flush()
        from paperintel.operations.tracing import record_trace_event

        record_trace_event(
            session,
            trace_id=request.trace_id,
            kind="validation",
            task_id=request.task_id,
            run_id=_active_run.get(),
            model_call_id=model_call_id,
            data={"schema_status": schema_status.value, "transport_status": transport_status.value},
        )
        return model_call_id
