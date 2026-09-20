"""agents.firewall — the schema firewall (P06, spec doc 02 §6, doc 03 §3).

EVERY model response passes this pipeline before anything canonical:

    raw content → JSON parse (LLM_003 / JSON_INVALID)
               → Pydantic validation against the FROZEN schema
                 (schemas.agent.AgentResult + schemas.claims.ClaimCandidate;
                 LLM_004 / SCHEMA_INVALID, one constrained repair attempt)
               → semantic validation (doc 03 §3 persistence rules):
                   * evidence ID existence (EVIDENCE_001) and paper-version
                     scope (EVIDENCE_002) — hard rejections;
                   * unsupported FACT statements, including unsupported
                     factual numbers (CLAIM_001 — the candidate is dropped,
                     the response survives);
                   * duplicated claims, tag/observation spam, overlong
                     fields, model refusal (LLM_005 — the output is
                     rejected).

No paper-specific agent may bypass this module (doc 05 P06): agents.base
routes every model call through validate_response().
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from paperintel.database.models import EvidenceRow
from paperintel.errors import DomainError
from paperintel.schemas.agent import AgentResult
from paperintel.schemas.claims import ClaimCandidate
from paperintel.schemas.enums import ClaimType, EvidenceRole, SchemaStatus

SchemaModel = TypeVar("SchemaModel", bound=BaseModel)

#: Semantic policy limits (doc 07 §3 analysis policy defaults; tighter than
#: the transport-level schema bounds so garbage never reaches persistence).
MAX_CLAIMS_PER_RESPONSE = 40
MAX_STATEMENT_CHARS = 2000
MAX_OBSERVATIONS = 100
MAX_UNCERTAINTIES = 50
MAX_WARNINGS = 50

#: Refusal markers (case-insensitive).
_REFUSAL_RE = re.compile(
    r"\b(i cannot|i can't|i'm unable to|as an ai language model|"
    r"i'm sorry, but|i apologize, but|cannot assist|cannot help)\b",
    re.IGNORECASE,
)

#: Surrounding markdown fences are stripped exactly once (recorded, never
#: silent).
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


@dataclass(slots=True)
class FirewallOutcome:
    """Firewall result for one model response."""

    status: SchemaStatus
    payload: BaseModel | None
    #: Non-fatal notes (fences stripped, repairs, dropped candidates...).
    notes: list[str]

    @property
    def accepted(self) -> bool:
        return self.payload is not None


def parse_json_payload(content: str) -> dict:
    """Parse the model response as JSON (LLM_003 on failure)."""
    text = content.strip()
    if not text:
        raise DomainError(
            "LLM_003",
            message="Model response is empty.",
            details={"content_length": 0},
        )
    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    for candidate in (text, cleaned):
        if candidate.startswith("{"):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise DomainError(
        "LLM_003",
        message="Model response is not valid JSON.",
        details={"content_excerpt": content[:200]},
    )


def validate_schema(payload: dict, schema: type[SchemaModel]) -> SchemaModel:  # noqa: UP047
    """Validate the parsed payload against the declared schema (LLM_004)."""
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise DomainError(
            "LLM_004",
            message="Model response violates the output schema.",
            details={
                "errors": [
                    {"loc": list(err["loc"]), "type": err["type"]} for err in exc.errors()[:10]
                ],
            },
        ) from exc


def detect_refusal(content: str) -> bool:
    """Detect explicit model refusal in the raw response."""
    return bool(_REFUSAL_RE.search(content))


def _normalized(statement: str) -> str:
    return " ".join(statement.lower().split())


def semantic_validation(
    result: AgentResult,
    *,
    resolve_evidence: Callable[[list[str]], list[EvidenceRow]],
) -> tuple[AgentResult, list[str]]:
    """Semantic checks over the schema-validated result.

    Returns (possibly rebuilt) result + warnings. Hard rejections raise:
    EVIDENCE_001 (unknown evidence ID), EVIDENCE_002 (evidence of another
    paper version), CLAIM_001 drops the single unsupported FACT candidate
    (response survives), LLM_005 rejects the whole output (duplicates,
    spam, overlong fields).
    """
    warnings: list[str] = []

    if len(result.claims) > MAX_CLAIMS_PER_RESPONSE:
        raise DomainError(
            "LLM_005",
            message="Agent output exceeds the claim-count limit.",
            details={"claim_count": len(result.claims), "limit": MAX_CLAIMS_PER_RESPONSE},
        )
    if len(result.observations) > MAX_OBSERVATIONS:
        raise DomainError(
            "LLM_005",
            message="Agent output exceeds the observation-count limit (spam).",
            details={
                "observation_count": len(result.observations),
                "limit": MAX_OBSERVATIONS,
            },
        )
    if len(result.uncertainties) > MAX_UNCERTAINTIES:
        raise DomainError(
            "LLM_005",
            message="Agent output exceeds the uncertainty-count limit (spam).",
            details={
                "uncertainty_count": len(result.uncertainties),
                "limit": MAX_UNCERTAINTIES,
            },
        )
    if len(result.warnings) > MAX_WARNINGS:
        raise DomainError(
            "LLM_005",
            message="Agent output exceeds the warning-count limit (spam).",
            details={"warning_count": len(result.warnings), "limit": MAX_WARNINGS},
        )

    seen_statements: set[str] = set()
    for claim in result.claims:
        key = _normalized(claim.statement)
        if key in seen_statements:
            raise DomainError(
                "LLM_005",
                message="Duplicated claim statement in agent output.",
                details={"statement": claim.statement[:200]},
            )
        seen_statements.add(key)

        if len(claim.statement) > MAX_STATEMENT_CHARS:
            raise DomainError(
                "LLM_005",
                message="Claim statement exceeds the field length limit.",
                details={
                    "statement_length": len(claim.statement),
                    "limit": MAX_STATEMENT_CHARS,
                },
            )

    # Evidence existence + scope first (hard reject on any violation).
    for claim in result.claims:
        if claim.evidence:
            resolve_evidence([ref.evidence_id for ref in claim.evidence])

    # Unsupported FACT candidates are dropped individually (CLAIM_001);
    # the rest of the response survives.
    kept: list[ClaimCandidate] = []
    for claim in result.claims:
        if claim.claim_type is ClaimType.EXTERNAL or not claim.evidence:
            warnings.append(f"CLAIM_001: candidate lacks trusted provenance: {claim.statement[:120]}")
            continue
        if claim.claim_type is ClaimType.FACT:
            support_ids = [ref.evidence_id for ref in claim.evidence if ref.role is EvidenceRole.SUPPORT]
            if not support_ids:
                warnings.append(f"CLAIM_001: FACT lacks SUPPORT evidence: {claim.statement[:120]}")
                continue
            rows = resolve_evidence(support_ids)
            from paperintel.agents.tools import statement_supported_by_evidence

            if not statement_supported_by_evidence(claim.statement, rows):
                warnings.append(
                    f"CLAIM_001: unsupported FACT candidate dropped: {claim.statement[:120]}"
                )
                continue
        kept.append(claim)

    if len(kept) != len(result.claims):
        result = result.model_copy(update={"claims": kept})
    # Normalize only presentation; preserve scientific case and symbols.
    import unicodedata

    normalized = []
    for claim in result.claims:
        data = claim.model_dump()
        data["statement"] = " ".join(unicodedata.normalize("NFC", claim.statement).split())
        data["category"] = claim.category.strip()
        data["evidence"] = list(
            {(ref.evidence_id, ref.role): ref.model_dump() for ref in claim.evidence}.values()
        )
        normalized.append(ClaimCandidate.model_validate(data))
    result = result.model_copy(update={"claims": normalized})
    return result, warnings


def validate_response(  # noqa: UP047
    raw_content: str,
    schema: type[SchemaModel],
    *,
    repair: Callable[[DomainError], str] | None = None,
) -> FirewallOutcome:
    """Full firewall pipeline for one raw model response.

    ``repair``: optional callable(validation_error) -> repaired RAW
    response; invoked exactly ONCE on LLM_004 (schema violation) per doc
    02 §6 constrained repair.
    """
    notes: list[str] = []
    if detect_refusal(raw_content):
        raise DomainError(
            "LLM_005",
            message="Model refused to answer.",
            details={"content_excerpt": raw_content[:200]},
        )

    payload = parse_json_payload(raw_content)
    try:
        validated = validate_schema(payload, schema)
    except DomainError as exc:
        if exc.code != "LLM_004" or repair is None:
            raise
        repaired_raw = repair(exc)
        notes.append("one constrained repair attempt executed after schema violation")
        payload = parse_json_payload(repaired_raw)
        try:
            validated = validate_schema(payload, schema)
            notes.append("schema repair succeeded")
        except DomainError as repair_exc:
            raise repair_exc from exc

    status = SchemaStatus.PASSED
    if "schema repair succeeded" in notes:
        status = SchemaStatus.PASSED_AFTER_REPAIR
    return FirewallOutcome(status=status, payload=validated, notes=notes)


def run_semantic_validation(
    result: AgentResult,
    *,
    paper_version_id: str,
    session,
) -> tuple[AgentResult, list[str]]:
    """Semantic validation bound to a DB session scope."""
    from paperintel.agents.tools import resolve_evidence_ids

    def _resolve(evidence_ids: list[str]) -> list[EvidenceRow]:
        return resolve_evidence_ids(session, paper_version_id, evidence_ids)

    return semantic_validation(result, resolve_evidence=_resolve)


def schema_status_for_error(code: str) -> SchemaStatus:
    """Map a firewall/semantic error code to its SchemaStatus for the
    model-call audit record (failures are audited too)."""
    if code == "LLM_003":
        return SchemaStatus.JSON_INVALID
    if code == "LLM_004":
        return SchemaStatus.SCHEMA_INVALID
    if code in ("LLM_005", "CLAIM_001", "EVIDENCE_001", "EVIDENCE_002"):
        return SchemaStatus.SEMANTIC_INVALID
    return SchemaStatus.NOT_RUN
