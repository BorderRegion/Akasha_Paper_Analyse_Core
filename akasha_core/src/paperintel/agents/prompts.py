"""agents.prompts — prompt registry + versioning (P06, doc 03 §1.7).

Prompts are CODE artifacts: every agent ships its prompt templates as
built-in registry entries (name + version + body) and seeds them into the
prompt_versions table idempotently. Templates use ``${variable}``
substitution (no str.format brace collisions with JSON examples).

Immutability: a registered (name, version) never changes — registering the
same version with a different body is CFG_002. New behavior = new version.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import PromptVersionRow
from paperintel.errors import DomainError
from paperintel.ids import new_prompt_version_id

_VARIABLE_RE = re.compile(r"\$\{(?P<name>[a-z_][a-z0-9_]*)\}")


def template_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_template(body: str, variables: dict[str, str]) -> str:
    """Substitute ${variables}; unknown variables in the template raise
    CFG_002 (a template referencing a missing fact is a programming error,
    never silently rendered)."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in variables:
            raise DomainError(
                "CFG_002",
                message=f"Prompt template references unknown variable ${{{name}}}.",
                details={"variable": name},
            )
        return variables[name]

    return _VARIABLE_RE.sub(_replace, body)


@dataclass(slots=True, frozen=True)
class PromptSpec:
    """A built-in prompt definition (code-owned)."""

    name: str
    version: str
    body: str
    description: str = ""


# ---------------------------------------------------------------------------
# Built-in prompts (P06 framework + generic claim extraction; P07 agents
# register their domain prompts through the same mechanism).
# ---------------------------------------------------------------------------

#: System-level instructions shared by every agent prompt.
_FIREWALL_PREAMBLE = """You are a precise paper-analysis agent.
Rules you MUST follow:
1. Answer with a single JSON object. No prose, no markdown fences.
2. Every factual claim MUST cite evidence by its evidence_id from the
   provided context. Facts without evidence are forbidden.
3. Use the exact field names of the requested schema.
4. If the evidence is insufficient to support any claim, set
   "status": "INSUFFICIENT_EVIDENCE" and provide an empty claims list.
5. Never invent evidence IDs, numbers, or section names."""

_SCHEMA_BLOCK = """
Schema (respond with EXACTLY this shape):
{
  "status": "SUCCESS" | "INSUFFICIENT_EVIDENCE",
  "claims": [
    {
      "claim_type": "FACT" | "INFERENCE" | "CRITIQUE",
      "category": "short category from the vocabulary below",
      "statement": "the claim",
      "evidence": [{"evidence_id": "ev_...", "role": "SUPPORT" | "COUNTER_EVIDENCE" | "CONTEXT"}],
      "uncertainties": []
    }
  ],
  "observations": ["free-form analytical observations"],
  "uncertainties": [],
  "requests_for_more_evidence": [],
  "warnings": []
}
FACT claims MUST cite at least one SUPPORT evidence reference.
If the evidence cannot support any claim, use "status": "INSUFFICIENT_EVIDENCE"
with an empty claims list — that is a valid answer."""

_CONTEXT_BLOCK = """

Paper: ${paper_id}
Paper version: ${paper_version_id}

## Evidence context (the ONLY admissible source)
${evidence_context}

## Task
"""


def _agent_prompt(instructions: str, categories: str) -> str:
    """Build one domain agent prompt: preamble + context + task."""
    return (
        _FIREWALL_PREAMBLE
        + _CONTEXT_BLOCK
        + instructions.strip()
        + "\n\n## Category vocabulary (use these category values)\n"
        + categories.strip()
        + _SCHEMA_BLOCK
    )


#: Domain instructions per built-in analysis agent (doc 01 §9).
AGENT_PROMPT_INSTRUCTIONS: dict[str, tuple[str, str]] = {
    "agents.structural": (
        """Classify the paper and map its structure: paper type
(research article, survey, position paper, workshop short paper, ...), the
normalized section organization, and which sections carry the analysis
value. Report structural facts as FACT claims (e.g. paper type with the
title/abstract as evidence) and put the high-level paper map into
observations.""",
        """- structure.paper_type
- structure.section_organization
- structure.analysis_scope""",
    ),
    "agents.research_question": (
        """Identify what the paper actually investigates: the target
problem, the motivating gap, any stated hypotheses, the declared scope,
and what the paper establishes (not what it promises). Distinguish
author-stated questions (FACT) from your reading of the gap
(INFERENCE).""",
        """- research.question
- research.gap
- research.hypothesis
- research.scope
- research.established""",
    ),
    "agents.contribution": (
        """Separate the contribution layers: author-DECLARED contributions
(quote-level, FACT with the declarations section as evidence), the actual
technical contribution, conceptual contribution, engineering contribution,
experimental contribution, and any contribution that appears OVERSTATED
relative to the presented evidence (CRITIQUE).""",
        """- contribution.declared
- contribution.technical
- contribution.conceptual
- contribution.engineering
- contribution.experimental
- contribution.overstated""",
    ),
    "agents.method": (
        """Extract the method as a reusable recipe: inputs, outputs,
components, algorithm steps, objective/loss, training procedure, inference
procedure, stated assumptions, computational characteristics, and
dependencies. Each component is a FACT claim citing its section.""",
        """- method.input
- method.output
- method.component
- method.algorithm
- method.objective
- method.training
- method.inference
- method.assumption
- method.compute
- method.dependency""",
    ),
    "agents.experiment": (
        """Extract the experimental setup: datasets, splits, baselines,
metrics, training budget, hardware, seeds, hyperparameters, evaluation
protocol, main results, ablations, sensitivity analysis, robustness
results, and error analysis. Numbers MUST come from the cited evidence
verbatim.""",
        """- experiment.dataset
- experiment.split
- experiment.baseline
- experiment.metric
- experiment.budget
- experiment.hardware
- experiment.seed
- experiment.hyperparameter
- experiment.protocol
- experiment.ablation
- experiment.sensitivity
- experiment.robustness
- experiment.error_analysis""",
    ),
    "agents.result": (
        """Separate the result classes: directly reported numerical
results (FACT, exact numbers from evidence), comparative results,
statistical reporting (significance, variance), claims of superiority
(mark CRITIQUE if the support is weaker than the wording), and the scope
of evidence the results actually cover.""",
        """- result.main
- result.comparative
- result.statistical
- result.superiority
- result.scope""",
    ),
    "agents.technique": (
        """Extract experimental/engineering TECHNIQUES as first-class
entities: technique name, the problem it addresses, its procedure,
applicable conditions, measured effect, cost, and transferability. Every
technique claim cites the passage describing it.""",
        """- technique.name
- technique.problem
- technique.procedure
- technique.conditions
- technique.effect
- technique.cost
- technique.transferability""",
    ),
    "agents.reliability": (
        """Audit experimental reliability: leakage, contamination, unfair
baselines, unequal training budgets, unequal pretraining data, tuning
fairness, metric selection, random seeds, variance/std, significance,
ablation completeness, dataset selection, compute/parameter/inference
comparison, data filtering, evaluation protocol, test-time augmentation,
prompt differences, benchmark saturation. Findings are CRITIQUE claims
that MUST cite the passage they critique.""",
        """- reliability.leakage
- reliability.contamination
- reliability.baseline_fairness
- reliability.budget
- reliability.pretraining
- reliability.tuning
- reliability.metric_selection
- reliability.seeds
- reliability.variance
- reliability.significance
- reliability.ablation
- reliability.dataset_selection
- reliability.compute_comparison
- reliability.protocol
- reliability.saturation""",
    ),
    "agents.reproduction": (
        """Assess reproducibility: required assets (code, data, model
weights), missing information, estimated implementation difficulty,
reproducibility blockers, required compute, and dependency risks. Mark
clearly what is INFERENCE versus what the paper states.""",
        """- reproduction.required_asset
- reproduction.missing_information
- reproduction.difficulty
- reproduction.blocker
- reproduction.compute
- reproduction.dependency_risk""",
    ),
    "agents.limitation": (
        """Extract limitations in two classes: author-STATED limitations
(FACT citing the limitations/discussion section) and unstated limitations
that the evidence itself supports (INFERENCE — boundary conditions,
likely failure modes). Never blur the two.""",
        """- limitation.stated
- limitation.unstated
- limitation.boundary
- limitation.failure_mode""",
    ),
    "agents.people": (
        """Extract people and project facts: authors, affiliations,
corresponding author, ORCID when available, project/code/data links.
Only report what the paper states; do NOT invent external identifiers.""",
        """- people.author
- people.affiliation
- people.corresponding
- people.orcid
- people.project_link""",
    ),
    "agents.critic": (
        """Attempt to INVALIDATE or weaken the paper's claims: novelty
claims, causal interpretations, fairness claims, generalization claims,
efficiency claims, SOTA claims. Every critique MUST cite the evidence it
attacks. If a claim survives scrutiny, record that as an observation.""",
        """- critique.novelty
- critique.causality
- critique.fairness
- critique.generalization
- critique.efficiency
- critique.sota""",
    ),
    "agents.synthesizer": (
        """You synthesize ONLY from the verified claims listed below.
You may not invent new facts: every statement must trace to a verified
claim (cite its claim_id in an observation). If the verified claim set is
empty or insufficient, answer "status": "INSUFFICIENT_EVIDENCE".

## Verified claims (the ONLY synthesis input)
${verified_claims}""",
        """- synthesis.summary
- synthesis.comparison
- synthesis.open_question""",
    ),
}

BUILTIN_PROMPTS: dict[tuple[str, str], PromptSpec] = {
    spec_key: spec
    for spec_key, spec in (
        (
            ("agents.claim_extraction", "1.0.0"),
            PromptSpec(
                name="agents.claim_extraction",
                version="1.0.0",
                description="Generic evidence-scoped claim extraction (framework P06).",
                body=(
                    _FIREWALL_PREAMBLE
                    + _CONTEXT_BLOCK
                    + "Extract claim candidates supported by the evidence above."
                    + _SCHEMA_BLOCK
                ),
            ),
        ),
    )
}

# Domain agent prompts (P07): one prompt per built-in agent, same version.
for _agent_name, (_instructions, _categories) in AGENT_PROMPT_INSTRUCTIONS.items():
    BUILTIN_PROMPTS[(_agent_name, "1.0.0")] = PromptSpec(
        name=_agent_name,
        version="1.0.0",
        description=f"Built-in analysis agent prompt: {_agent_name}.",
        body=_agent_prompt(_instructions, _categories),
    )


def ensure_builtin_prompts(session: Session) -> int:
    """Seed all built-in prompts idempotently. Returns how many were added."""
    added = 0
    for spec in BUILTIN_PROMPTS.values():
        _, created = register_prompt(session, name=spec.name, version=spec.version, body=spec.body)
        added += 1 if created else 0
    return added


def register_prompt(
    session: Session, *, name: str, version: str, body: str
) -> tuple[PromptVersionRow, bool]:
    """Register (or reuse) one prompt version. Same body → reuse; same
    version with a DIFFERENT body → CFG_002 (versions are immutable)."""
    existing = session.scalar(
        select(PromptVersionRow).where(
            PromptVersionRow.name == name, PromptVersionRow.version == version
        )
    )
    if existing is not None:
        if existing.template_sha256 != template_sha256(body):
            raise DomainError(
                "CFG_002",
                message=(
                    f"Prompt {name}@{version} is already registered with a "
                    "different template; prompts are immutable — bump the version."
                ),
                details={"name": name, "version": version},
            )
        return existing, False
    row = PromptVersionRow(
        prompt_version_id=new_prompt_version_id(),
        name=name,
        version=version,
        template_sha256=template_sha256(body),
        template_body=body,
    )
    session.add(row)
    session.flush()
    return row, True


def get_prompt(session: Session, name: str, version: str) -> PromptVersionRow:
    row = session.scalar(
        select(PromptVersionRow).where(
            PromptVersionRow.name == name, PromptVersionRow.version == version
        )
    )
    if row is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown prompt {name}@{version}.",
            details={"name": name, "version": version},
        )
    return row


def render_prompt(
    session: Session,
    name: str,
    version: str,
    variables: dict[str, str],
) -> tuple[str, PromptVersionRow]:
    """Render a registered prompt with variables → (rendered, row)."""
    row = get_prompt(session, name, version)
    return render_template(row.template_body, variables), row
