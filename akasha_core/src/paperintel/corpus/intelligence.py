"""corpus.intelligence — cross-paper corpus analysis (P11, doc 01 §19).

Every corpus output is provenance-carrying: each finding lists the source
paper(s), claim IDs and evidence IDs it was derived from. Nothing is
aggregated without a trail back to the canonical records.

Design note on "corpus outputs are also claims": the frozen ClaimRow is
per-paper by contract (paper_id + paper_version_id are NOT NULL), so a
cross-paper aggregate cannot be one claim row. Findings that ARE
attributable to a single paper are materialized as ledger claims through
materialize_finding(); cross-paper aggregates are computed views whose
derivation is audited (one AnalysisRunRow per computation, carrying the
config hash) and whose entries carry the full source provenance, so any
aggregate can be re-derived and checked.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.agents.tools import significant_tokens, statement_numbers
from paperintel.config.fingerprint import analysis_config_hash
from paperintel.database.models import (
    AnalysisRunRow,
    ClaimEvidenceRow,
    ClaimRow,
    CollectionPaperRow,
    EntityRow,
    PaperTagRow,
    PaperVersionRow,
    RelationRow,
    TagRow,
)
from paperintel.errors import DomainError
from paperintel.ids import new_run_id
from paperintel.schemas.enums import EntityType, SupportState, TaskState
from paperintel.version import PIPELINE_VERSION

#: Claim-connective / metric vocabulary that appears across unrelated
#: statements. Two claims agreeing on "reaches ... accuracy" do NOT
#: contradict each other, so a disagreement must be carried by at least one
#: token OUTSIDE this set (doc 01 §12: findings must be explainable).
GENERIC_CLAIM_TOKENS: frozenset[str] = frozenset(
    {
        "reaches",
        "reach",
        "reached",
        "accuracy",
        "accuracies",
        "performance",
        "result",
        "results",
        "method",
        "methods",
        "achieves",
        "achieve",
        "achieved",
        "improves",
        "improve",
        "improved",
        "improvement",
        "better",
        "higher",
        "lower",
        "using",
        "used",
        "based",
        "show",
        "shows",
        "shown",
        "report",
        "reports",
        "reported",
        "significant",
        "significantly",
        "substantial",
        "substantially",
        "average",
        "overall",
        "score",
        "scores",
        "value",
        "values",
        "metric",
        "metrics",
        "evaluation",
        "evaluate",
        "test",
        "tests",
        "training",
        "trained",
        "model",
        "models",
        "data",
    }
)

#: Tag namespaces that describe what a paper is ABOUT (topic landscape).
TOPIC_NAMESPACES: tuple[str, ...] = (
    "domain",
    "task",
    "modality",
    "application",
    "method",
)


@dataclass(slots=True)
class Provenance:
    """Source trail every corpus finding carries (doc 01 §19)."""

    paper_ids: list[str] = field(default_factory=list)
    paper_version_ids: list[str] = field(default_factory=list)
    claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)

    def merge(self, other: Provenance) -> Provenance:
        return Provenance(
            paper_ids=list(dict.fromkeys([*self.paper_ids, *other.paper_ids])),
            paper_version_ids=list(
                dict.fromkeys([*self.paper_version_ids, *other.paper_version_ids])
            ),
            claim_ids=list(dict.fromkeys([*self.claim_ids, *other.claim_ids])),
            evidence_ids=list(dict.fromkeys([*self.evidence_ids, *other.evidence_ids])),
        )

    def to_dict(self) -> dict:
        return {
            "paper_ids": self.paper_ids,
            "paper_version_ids": self.paper_version_ids,
            "claim_ids": self.claim_ids,
            "evidence_ids": self.evidence_ids,
        }


@dataclass(slots=True)
class CorpusView:
    """One corpus analysis result.

    ``findings`` is always a flat list of provenance-carrying entries (so
    every entry is checkable with the same rule); graph-shaped views put
    their edges in ``extra`` rather than nesting them inside a finding.
    """

    kind: str
    run_id: str
    scope: str
    findings: list[dict]
    warnings: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "scope": self.scope,
            "finding_count": len(self.findings),
            "findings": self.findings,
            "warnings": self.warnings,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# scope helpers
# ---------------------------------------------------------------------------


def _scoped_papers(session: Session, collection_id: str | None) -> set[str] | None:
    """Paper ID scope: None = whole corpus, else the collection's papers."""
    if collection_id is None:
        return None
    rows = session.scalars(
        select(CollectionPaperRow.paper_id).where(CollectionPaperRow.collection_id == collection_id)
    ).all()
    if not rows:
        # An empty scope means NO papers (never "everything").
        return set()
    return set(rows)


def _paper_filter(paper_ids: set[str] | None):
    return paper_ids


def _claims(
    session: Session,
    *,
    paper_ids: set[str] | None = None,
    categories: set[str] | None = None,
    prefixes: tuple[str, ...] | None = None,
    claim_types: set | None = None,
) -> list[ClaimRow]:
    stmt = select(ClaimRow).order_by(ClaimRow.created_at, ClaimRow.claim_id)
    if paper_ids is not None:
        if not paper_ids:
            return []
        stmt = stmt.where(ClaimRow.paper_id.in_(paper_ids))
    if categories:
        stmt = stmt.where(ClaimRow.category.in_(categories))
    if claim_types:
        stmt = stmt.where(ClaimRow.claim_type.in_(claim_types))
    rows = list(session.scalars(stmt))
    if prefixes:
        rows = [row for row in rows if row.category.startswith(prefixes)]
    return rows


def _evidence_ids(session: Session, claim_ids: list[str]) -> list[str]:
    if not claim_ids:
        return []
    return list(
        dict.fromkeys(
            session.scalars(
                select(ClaimEvidenceRow.evidence_id).where(ClaimEvidenceRow.claim_id.in_(claim_ids))
            )
        )
    )


def _prov(session: Session, claims: list[ClaimRow]) -> Provenance:
    return Provenance(
        paper_ids=list(dict.fromkeys(claim.paper_id for claim in claims)),
        paper_version_ids=list(dict.fromkeys(claim.paper_version_id for claim in claims)),
        claim_ids=[claim.claim_id for claim in claims],
        evidence_ids=_evidence_ids(session, [claim.claim_id for claim in claims]),
    )


def _record_run(
    session: Session,
    kind: str,
    scope: str,
    config_hash: str = "",
    *,
    paper_id: str | None = None,
) -> str:
    """Every corpus computation is audited (who computed what, over which
    scope, with which inputs).

    ``scope`` is a collection id or the literal "corpus"; ``paper_id`` is
    recorded only for paper-attributable runs (materialization), never
    confused with a collection id.
    """
    run_id = new_run_id()
    now = datetime.now(UTC)
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_id=paper_id,
            paper_version_id=None,
            collection_id=None if scope == "corpus" else scope,
            agent_type=f"corpus.{kind}",
            pipeline_version=PIPELINE_VERSION,
            config_hash=analysis_config_hash(stage=f"corpus.{kind}", policy_hash=config_hash),
            model_id="none",
            provider_id="prv_none",
            status=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
        )
    )
    session.flush()
    return run_id


def _scope_label(collection_id: str | None) -> str:
    return collection_id or "corpus"


# ---------------------------------------------------------------------------
# 1. topic landscape
# ---------------------------------------------------------------------------


def _topic_findings(session: Session, *, collection_id: str | None = None) -> list[dict]:
    """Topic aggregation shared by topic_landscape and gap_candidates (no
    nested audited runs: one computation = one run row)."""
    paper_ids = _scoped_papers(session, collection_id)
    stmt = (
        select(PaperTagRow, TagRow, ClaimRow)
        .join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
        .outerjoin(
            ClaimRow,
            (ClaimRow.paper_id == PaperTagRow.paper_id)
            & (ClaimRow.category.startswith(TagRow.namespace + ".")),
        )
        .where(TagRow.namespace.in_(TOPIC_NAMESPACES))
    )
    if paper_ids is not None:
        if not paper_ids:
            return []
        stmt = stmt.where(PaperTagRow.paper_id.in_(paper_ids))

    topics: dict[tuple[str, str], dict] = {}
    for link, tag, claim in session.execute(stmt).all():
        key = (tag.namespace, tag.normalized_name)
        entry = topics.setdefault(
            key,
            {
                "topic": tag.canonical_name,
                "namespace": tag.namespace,
                "paper_ids": set(),
                "claim_ids": [],
                "is_candidate": link.is_candidate,
            },
        )
        entry["paper_ids"].add(link.paper_id)
        if claim is not None:
            entry["claim_ids"].append(claim.claim_id)

    findings = []
    for entry in sorted(topics.values(), key=lambda item: (-len(item["paper_ids"]), item["topic"])):
        findings.append(
            {
                "topic": entry["topic"],
                "namespace": entry["namespace"],
                "paper_count": len(entry["paper_ids"]),
                "candidate_tag": entry["is_candidate"],
                "provenance": Provenance(
                    paper_ids=sorted(entry["paper_ids"]),
                    claim_ids=list(dict.fromkeys(entry["claim_ids"])),
                    evidence_ids=_evidence_ids(session, list(dict.fromkeys(entry["claim_ids"]))),
                ).to_dict(),
            }
        )
    return findings


def topic_landscape(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Topics (tag namespaces describing subject matter) with their papers
    and the claims that put each paper in that topic."""
    findings = _topic_findings(session, collection_id=collection_id)
    warnings = []
    if not findings and collection_id is not None and not _scoped_papers(session, collection_id):
        warnings.append("collection scope contains no papers")

    run_id = _record_run(
        session,
        "topic_landscape",
        _scope_label(collection_id),
        config_hash=hashlib.sha256(f"topics:{','.join(TOPIC_NAMESPACES)}".encode()).hexdigest()[
            :16
        ],
    )
    return CorpusView(
        kind="topic_landscape",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 2. method genealogy
# ---------------------------------------------------------------------------


def method_genealogy(
    session: Session, *, collection_id: str | None = None, root_entity_id: str | None = None
) -> CorpusView:
    """Method graph: which methods exist, which papers use them, and how
    they relate (USES_METHOD / EXTENDS_METHOD / RELATED_TO)."""
    paper_ids = _scoped_papers(session, collection_id)
    relations = list(session.scalars(select(RelationRow)))
    if paper_ids is not None:
        relations = [relation for relation in relations if relation.paper_id in paper_ids]
    method_entities = {
        entity.entity_id: entity
        for entity in session.scalars(
            select(EntityRow).where(EntityRow.entity_type == EntityType.METHOD.value)
        )
    }

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for relation in relations:
        for entity_id in (relation.source_entity_id, relation.target_entity_id):
            entity = method_entities.get(entity_id)
            if entity is None:
                continue
            node = nodes.setdefault(
                entity_id,
                {
                    "entity_id": entity_id,
                    "method": entity.canonical_name,
                    "paper_ids": set(),
                    "relation_types": set(),
                    "provenance": Provenance(),
                },
            )
            if relation.paper_id:
                node["paper_ids"].add(relation.paper_id)
            node["relation_types"].add(relation.relation_type)
            node["provenance"] = node["provenance"].merge(
                Provenance(
                    paper_ids=[relation.paper_id] if relation.paper_id else [],
                    evidence_ids=list(relation.evidence_ids or []),
                )
            )
        edges.append(
            {
                "relation_id": relation.relation_id,
                "relation_type": relation.relation_type,
                "source_entity_id": relation.source_entity_id,
                "target_entity_id": relation.target_entity_id,
                "paper_id": relation.paper_id,
                "confidence": relation.confidence,
                "provenance": Provenance(
                    paper_ids=[relation.paper_id] if relation.paper_id else [],
                    evidence_ids=list(relation.evidence_ids or []),
                ).to_dict(),
            }
        )

    if root_entity_id is not None:
        from paperintel.knowledge.graph import neighbors

        reached = neighbors(session, root_entity_id, depth=2)
        reached_ids = set(reached)
        nodes = {key: value for key, value in nodes.items() if key in reached_ids}
        edges = [
            edge
            for edge in edges
            if edge["source_entity_id"] in reached_ids or edge["target_entity_id"] in reached_ids
        ]

    findings = []
    for node in sorted(nodes.values(), key=lambda item: (-len(item["paper_ids"]), item["method"])):
        findings.append(
            {
                "method": node["method"],
                "entity_id": node["entity_id"],
                "paper_count": len(node["paper_ids"]),
                "relation_types": sorted(node["relation_types"]),
                "provenance": node["provenance"].to_dict(),
            }
        )

    run_id = _record_run(session, "method_genealogy", _scope_label(collection_id))
    return CorpusView(
        kind="method_genealogy",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
        extra={"edges": edges},
    )


# ---------------------------------------------------------------------------
# 3. dataset landscape
# ---------------------------------------------------------------------------


def dataset_landscape(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Datasets across the corpus with their papers and reported metrics."""
    claims = _claims(session, paper_ids=_scoped_papers(session, collection_id))
    dataset_claims = [claim for claim in claims if claim.category.startswith("experiment.dataset")]
    metric_claims = [claim for claim in claims if claim.category.startswith("experiment.metric")]

    datasets: dict[str, dict] = {}
    for claim in dataset_claims:
        from paperintel.knowledge.graph import claim_subject

        name = claim_subject(claim.statement) or claim.statement[:60]
        entry = datasets.setdefault(
            name,
            {"dataset": name, "paper_ids": set(), "claims": [], "metrics": set()},
        )
        entry["paper_ids"].add(claim.paper_id)
        entry["claims"].append(claim)

    # Metrics are associated with the paper's datasets (co-occurrence within
    # a paper version) — the association is explicit in the finding.
    for claim in metric_claims:
        for entry in datasets.values():
            if claim.paper_version_id in {other.paper_version_id for other in entry["claims"]}:
                entry["metrics"].add(claim.statement[:120])
                entry["claims"].append(claim)

    findings = []
    for entry in sorted(
        datasets.values(), key=lambda item: (-len(item["paper_ids"]), item["dataset"])
    ):
        findings.append(
            {
                "dataset": entry["dataset"],
                "paper_count": len(entry["paper_ids"]),
                "metric_statements": sorted(entry["metrics"]),
                "provenance": _prov(session, entry["claims"]).to_dict(),
            }
        )

    run_id = _record_run(session, "dataset_landscape", _scope_label(collection_id))
    return CorpusView(
        kind="dataset_landscape",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 4. benchmark matrix
# ---------------------------------------------------------------------------

#: "84.2%" / "0.91 AUC" / "1,000 samples" style value extraction.
_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?\s*%?)")


def benchmark_matrix(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Dataset × metric matrix. Cells carry the paper, the numeric value,
    the claim that asserted it, its support state and the evidence."""
    paper_ids = _scoped_papers(session, collection_id)
    claims = _claims(session, paper_ids=paper_ids, prefixes=("result.",))
    dataset_claims = _claims(session, paper_ids=paper_ids, prefixes=("experiment.dataset",))

    paper_datasets: dict[str, set[str]] = {}
    from paperintel.knowledge.graph import claim_subject

    dataset_names: dict[str, str] = {}
    for claim in dataset_claims:
        name = claim_subject(claim.statement) or claim.statement[:60]
        dataset_names[claim.claim_id] = name
        paper_datasets.setdefault(claim.paper_id, set()).add(name)

    cells: list[dict] = []
    for claim in claims:
        values = statement_numbers(claim.statement)
        if not values:
            continue
        metric = _metric_phrase(claim.statement)
        for dataset in sorted(paper_datasets.get(claim.paper_id, {"(unspecified)"})):
            cells.append(
                {
                    "dataset": dataset,
                    "metric": metric,
                    "paper_id": claim.paper_id,
                    "paper_version_id": claim.paper_version_id,
                    "value": sorted(values),
                    "statement": claim.statement,
                    "support_state": claim.support_state.value,
                    "provenance": _prov(session, [claim]).to_dict(),
                }
            )

    run_id = _record_run(session, "benchmark_matrix", _scope_label(collection_id))
    return CorpusView(
        kind="benchmark_matrix",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=cells,
    )


def _metric_phrase(statement: str) -> str:
    """The metric-ish phrase of a result statement (words after the value,
    before the next punctuation) — a label, never a fabricated metric."""
    match = _VALUE_RE.search(statement)
    if match is None:
        return statement[:60]
    tail = statement[match.end() :].strip(" ,.;:")
    tokens = [token for token in tail.split() if token]
    return " ".join(tokens[:4]) or statement[:60]


# ---------------------------------------------------------------------------
# 5. contradiction map
# ---------------------------------------------------------------------------


def contradiction_map(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Where the corpus disagrees: cross-paper claims with the same subject
    but different numbers, CONTRADICTS relations, and DISPUTED claims."""
    paper_ids = _scoped_papers(session, collection_id)
    claims = _claims(session, paper_ids=paper_ids)
    # Deduplicate by (paper, normalized statement): re-running the pipeline
    # (or repeated analysis passes) must not inflate token frequencies or
    # duplicate findings.
    unique: dict[tuple[str, str], ClaimRow] = {}
    for claim in claims:
        key = (claim.paper_id, " ".join(claim.statement.lower().split()))
        unique.setdefault(key, claim)
    claims = list(unique.values())
    numeric = [claim for claim in claims if statement_numbers(claim.statement)]

    # Token discrimination (mini-IDF): generic phrasing ("reaches ... accuracy")
    # appears everywhere and must not create "contradictions". A shared token
    # counts only when it is NOT present in most claims of the scope.
    token_df: dict[str, int] = {}
    claim_tokens: dict[str, set[str]] = {}
    for claim in numeric:
        tokens = significant_tokens(claim.statement)
        claim_tokens[claim.claim_id] = tokens
        for token in tokens:
            token_df[token] = token_df.get(token, 0) + 1

    def _discriminating(tokens: set[str]) -> set[str]:
        """Shared SUBJECT tokens: the overlap minus generic claim phrasing.

        Generic vocabulary ("reaches", "accuracy") is shared by unrelated
        statements and must never manufacture a disagreement; only
        subject-carrying tokens ("vision", "imagenet") can. The rule is
        explicit and corpus-size independent.
        """
        return {token for token in tokens if token not in GENERIC_CLAIM_TOKENS}

    findings: list[dict] = []
    for index, claim in enumerate(numeric):
        tokens = claim_tokens[claim.claim_id]
        if not tokens:
            continue
        numbers = statement_numbers(claim.statement)
        for other in numeric[index + 1 :]:
            if other.paper_id == claim.paper_id:
                continue  # cross-paper contradictions only
            other_tokens = claim_tokens[other.claim_id]
            overlap = tokens & other_tokens
            if len(overlap) < max(2, len(tokens) // 2):
                continue
            if not _discriminating(overlap):
                continue  # only generic phrasing in common → not a disagreement
            other_numbers = statement_numbers(other.statement)
            if other_numbers and other_numbers != numbers:
                findings.append(
                    {
                        "kind": "numeric_disagreement",
                        "subject_tokens": sorted(overlap),
                        "paper_ids": [claim.paper_id, other.paper_id],
                        "values": [sorted(numbers), sorted(other_numbers)],
                        "statements": [claim.statement, other.statement],
                        "support_states": [
                            claim.support_state.value,
                            other.support_state.value,
                        ],
                        "provenance": _prov(session, [claim, other]).to_dict(),
                    }
                )

    relations = list(
        session.scalars(select(RelationRow).where(RelationRow.relation_type == "CONTRADICTS"))
    )
    for relation in relations:
        if paper_ids is not None and relation.paper_id not in paper_ids:
            continue
        findings.append(
            {
                "kind": "contradicts_relation",
                "relation_id": relation.relation_id,
                "paper_ids": [relation.paper_id] if relation.paper_id else [],
                "statement": f"{relation.source_entity_id} CONTRADICTS {relation.target_entity_id}",
                "provenance": Provenance(
                    paper_ids=[relation.paper_id] if relation.paper_id else [],
                    evidence_ids=list(relation.evidence_ids or []),
                ).to_dict(),
            }
        )

    disputed = [
        claim
        for claim in claims
        if claim.support_state in (SupportState.DISPUTED, SupportState.UNSUPPORTED)
    ]
    for claim in disputed:
        findings.append(
            {
                "kind": f"claim_{claim.support_state.value.lower()}",
                "paper_ids": [claim.paper_id],
                "statement": claim.statement,
                "category": claim.category,
                "provenance": _prov(session, [claim]).to_dict(),
            }
        )

    run_id = _record_run(session, "contradiction_map", _scope_label(collection_id))
    return CorpusView(
        kind="contradiction_map",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 6. innovation map
# ---------------------------------------------------------------------------


def innovation_map(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """What each paper claims as new (declared/technical contributions) and
    how the corpus tags novelty."""
    paper_ids = _scoped_papers(session, collection_id)
    contribution_claims = _claims(session, paper_ids=paper_ids, prefixes=("contribution.",))
    per_paper: dict[str, list[ClaimRow]] = {}
    for claim in contribution_claims:
        per_paper.setdefault(claim.paper_id, []).append(claim)

    novelty_tags: dict[str, list[str]] = {}
    stmt = (
        select(PaperTagRow.paper_id, TagRow.canonical_name)
        .join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
        .where(TagRow.namespace == "novelty")
    )
    if paper_ids is not None:
        if not paper_ids:
            stmt = stmt.where(PaperTagRow.paper_id.is_(None))
        else:
            stmt = stmt.where(PaperTagRow.paper_id.in_(paper_ids))
    for paper_id, tag_name in session.execute(stmt).all():
        novelty_tags.setdefault(paper_id, []).append(tag_name)

    findings = []
    for paper_id in sorted(set(per_paper) | set(novelty_tags)):
        claims = per_paper.get(paper_id, [])
        findings.append(
            {
                "paper_id": paper_id,
                "declared_contributions": [
                    claim.statement for claim in claims if claim.category == "contribution.declared"
                ],
                "technical_contributions": [
                    claim.statement
                    for claim in claims
                    if claim.category == "contribution.technical"
                ],
                "overstated_flags": [
                    claim.statement
                    for claim in claims
                    if claim.category == "contribution.overstated"
                ],
                "novelty_tags": sorted(set(novelty_tags.get(paper_id, []))),
                "provenance": _prov(session, claims).to_dict(),
            }
        )

    run_id = _record_run(session, "innovation_map", _scope_label(collection_id))
    return CorpusView(
        kind="innovation_map",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 7. technique mining
# ---------------------------------------------------------------------------


def technique_mining(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Experimental/engineering techniques with their problem, procedure,
    effect and cost claims, grouped by technique subject."""
    paper_ids = _scoped_papers(session, collection_id)
    claims = _claims(session, paper_ids=paper_ids, prefixes=("technique.",))
    grouped: dict[str, list[ClaimRow]] = {}
    for claim in claims:
        grouped.setdefault(claim.category, []).append(claim)

    findings = []
    for category in sorted(grouped):
        entries = grouped[category]
        subjects: dict[str, list[ClaimRow]] = {}
        from paperintel.knowledge.graph import claim_subject

        for claim in entries:
            subjects.setdefault(claim_subject(claim.statement) or "unspecified", []).append(claim)
        for subject, subject_claims in sorted(subjects.items()):
            findings.append(
                {
                    "aspect": category,
                    "technique": subject,
                    "paper_ids": sorted({claim.paper_id for claim in subject_claims}),
                    "statements": [claim.statement for claim in subject_claims],
                    "provenance": _prov(session, subject_claims).to_dict(),
                }
            )

    run_id = _record_run(session, "technique_mining", _scope_label(collection_id))
    return CorpusView(
        kind="technique_mining",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 8. reliability landscape
# ---------------------------------------------------------------------------


def reliability_landscape(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Per-paper reliability findings (critique claims by dimension) plus
    the OCR/quality risk signals recorded on evidence."""
    paper_ids = _scoped_papers(session, collection_id)
    claims = _claims(session, paper_ids=paper_ids, prefixes=("reliability.",))
    per_paper: dict[str, dict[str, list[ClaimRow]]] = {}
    for claim in claims:
        per_paper.setdefault(claim.paper_id, {}).setdefault(claim.category, []).append(claim)

    findings = []
    for paper_id in sorted(per_paper):
        dimensions = {
            category: [claim.statement for claim in entries]
            for category, entries in sorted(per_paper[paper_id].items())
        }
        all_claims = [claim for entries in per_paper[paper_id].values() for claim in entries]
        findings.append(
            {
                "paper_id": paper_id,
                "dimensions": dimensions,
                "finding_count": len(all_claims),
                "provenance": _prov(session, all_claims).to_dict(),
            }
        )

    run_id = _record_run(session, "reliability_landscape", _scope_label(collection_id))
    return CorpusView(
        kind="reliability_landscape",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 9. author network
# ---------------------------------------------------------------------------


def author_network(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Authors, their affiliations and co-authorship edges."""
    paper_ids = _scoped_papers(session, collection_id)
    relations = [
        relation
        for relation in session.scalars(select(RelationRow))
        if relation.relation_type in ("AUTHORED_BY", "AFFILIATED_WITH")
    ]
    if paper_ids is not None:
        relations = [relation for relation in relations if relation.paper_id in paper_ids]

    entities = {entity.entity_id: entity for entity in session.scalars(select(EntityRow))}
    paper_authors: dict[str, set[str]] = {}
    authors: dict[str, dict] = {}
    for relation in relations:
        if relation.relation_type == "AUTHORED_BY" and relation.paper_id:
            author = entities.get(relation.target_entity_id) or entities.get(
                relation.source_entity_id
            )
            if author is None:
                continue
            paper_authors.setdefault(relation.paper_id, set()).add(author.entity_id)
            entry = authors.setdefault(
                author.entity_id,
                {
                    "author": author.canonical_name,
                    "entity_id": author.entity_id,
                    "paper_ids": set(),
                    "provenance": Provenance(),
                },
            )
            entry["paper_ids"].add(relation.paper_id)
            entry["provenance"] = entry["provenance"].merge(
                Provenance(
                    paper_ids=[relation.paper_id],
                    evidence_ids=list(relation.evidence_ids or []),
                )
            )

    edges: list[dict] = []
    for paper_id, author_ids in paper_authors.items():
        ordered = sorted(author_ids)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                edges.append(
                    {
                        "paper_id": paper_id,
                        "author_a": entities[left].canonical_name if left in entities else left,
                        "author_b": entities[right].canonical_name if right in entities else right,
                        "provenance": Provenance(paper_ids=[paper_id]).to_dict(),
                    }
                )

    findings = []
    for entry in sorted(
        authors.values(), key=lambda item: (-len(item["paper_ids"]), item["author"])
    ):
        findings.append(
            {
                "author": entry["author"],
                "entity_id": entry["entity_id"],
                "paper_count": len(entry["paper_ids"]),
                "provenance": entry["provenance"].to_dict(),
            }
        )

    run_id = _record_run(session, "author_network", _scope_label(collection_id))
    return CorpusView(
        kind="author_network",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
        extra={"collaborations": edges},
    )


# ---------------------------------------------------------------------------
# 10. gap candidates
# ---------------------------------------------------------------------------


def gap_candidates(
    session: Session, *, collection_id: str | None = None, min_papers: int = 1
) -> CorpusView:
    """Research gaps: explicit gap/unstated-limitation claims plus topics
    the corpus barely covers (thin topics are candidate gaps)."""
    paper_ids = _scoped_papers(session, collection_id)
    gap_claims = _claims(
        session,
        paper_ids=paper_ids,
        categories={"research.gap", "limitation.unstated", "research.question"},
    )
    findings: list[dict] = []
    for claim in gap_claims:
        findings.append(
            {
                "kind": "explicit_gap" if claim.category == "research.gap" else claim.category,
                "paper_ids": [claim.paper_id],
                "statement": claim.statement,
                "provenance": _prov(session, [claim]).to_dict(),
            }
        )

    for entry in _topic_findings(session, collection_id=collection_id):
        if entry["paper_count"] <= min_papers:
            findings.append(
                {
                    "kind": "thin_topic",
                    "topic": entry["topic"],
                    "namespace": entry["namespace"],
                    "paper_count": entry["paper_count"],
                    "provenance": entry["provenance"],
                }
            )

    run_id = _record_run(session, "gap_candidates", _scope_label(collection_id))
    return CorpusView(
        kind="gap_candidates",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 11. negative / failed results
# ---------------------------------------------------------------------------


def negative_results(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Negative and failed results: unsupported/disputed/retracted claims and
    critiques that report failures."""
    paper_ids = _scoped_papers(session, collection_id)
    claims = _claims(session, paper_ids=paper_ids)
    findings: list[dict] = []
    for claim in claims:
        state = claim.support_state
        is_negative_state = state in (
            SupportState.UNSUPPORTED,
            SupportState.DISPUTED,
            SupportState.RETRACTED,
        )
        is_failure_critique = claim.category.startswith("critique.") or any(
            marker in claim.statement.lower()
            for marker in ("fails", "failed", "does not ", "no improvement", "unable to")
        )
        if not (is_negative_state or is_failure_critique):
            continue
        findings.append(
            {
                "kind": (f"claim_{state.value.lower()}" if is_negative_state else "failure_report"),
                "paper_ids": [claim.paper_id],
                "category": claim.category,
                "statement": claim.statement,
                "support_state": state.value,
                "provenance": _prov(session, [claim]).to_dict(),
            }
        )

    run_id = _record_run(session, "negative_results", _scope_label(collection_id))
    return CorpusView(
        kind="negative_results",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 12. temporal trends
# ---------------------------------------------------------------------------


def temporal_trends(session: Session, *, collection_id: str | None = None) -> CorpusView:
    """Corpus change over time: papers, topics and methods per year."""
    paper_ids = _scoped_papers(session, collection_id)
    versions = list(session.scalars(select(PaperVersionRow)))
    if paper_ids is not None:
        versions = [version for version in versions if version.paper_id in paper_ids]

    by_year: dict[int, dict] = {}
    for version in versions:
        year = (version.publication_date or version.created_at).year
        entry = by_year.setdefault(year, {"year": year, "paper_ids": set(), "version_ids": []})
        entry["paper_ids"].add(version.paper_id)
        entry["version_ids"].append(version.paper_version_id)

    tag_rows = session.execute(
        select(PaperTagRow.paper_id, TagRow.namespace, TagRow.canonical_name)
        .join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
        .where(TagRow.namespace.in_(TOPIC_NAMESPACES))
    ).all()
    tags_by_paper: dict[str, list[str]] = {}
    for paper_id, namespace, name in tag_rows:
        tags_by_paper.setdefault(paper_id, []).append(f"{namespace}/{name}")

    findings = []
    for year in sorted(by_year):
        entry = by_year[year]
        topics: dict[str, int] = {}
        for paper_id in entry["paper_ids"]:
            for topic in tags_by_paper.get(paper_id, []):
                topics[topic] = topics.get(topic, 0) + 1
        findings.append(
            {
                "year": year,
                "paper_count": len(entry["paper_ids"]),
                "topics": [
                    {"topic": topic, "paper_count": count}
                    for topic, count in sorted(topics.items(), key=lambda item: (-item[1], item[0]))
                ],
                "provenance": Provenance(
                    paper_ids=sorted(entry["paper_ids"]),
                    paper_version_ids=entry["version_ids"],
                ).to_dict(),
            }
        )

    run_id = _record_run(session, "temporal_trends", _scope_label(collection_id))
    return CorpusView(
        kind="temporal_trends",
        run_id=run_id,
        scope=_scope_label(collection_id),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# materialization: attributable findings become ledger claims
# ---------------------------------------------------------------------------


def materialize_finding(
    session: Session,
    *,
    paper_id: str,
    paper_version_id: str,
    category: str,
    statement: str,
    source_claim_ids: list[str],
    claim_type: str = "INFERENCE",
) -> str:
    """Persist a corpus finding as a ledger claim on the paper it is about.

    Corpus outputs that are attributable to ONE paper become real claims
    (doc 01 §19 "corpus outputs are also claims"), evidence-linked to the
    same evidence their source claims cite — so the corpus statement is
    verifiable through the ordinary verification path.
    """
    from paperintel.ids import new_claim_id
    from paperintel.schemas.enums import ClaimType

    if not source_claim_ids:
        raise DomainError(
            "CLAIM_001",
            message="A corpus claim must cite the source claims it was derived from.",
            details={"category": category},
        )
    sources = list(session.scalars(select(ClaimRow).where(ClaimRow.claim_id.in_(source_claim_ids))))
    if len(sources) != len(set(source_claim_ids)):
        raise DomainError(
            "CFG_002",
            message="Unknown source claim ID in corpus finding provenance.",
            details={"source_claim_ids": source_claim_ids},
        )
    evidence_ids = _evidence_ids(session, [claim.claim_id for claim in sources])
    if not evidence_ids:
        raise DomainError(
            "CLAIM_001",
            message=(
                "Corpus claim refused: its source claims carry no evidence, so the "
                "derived statement would be unverifiable."
            ),
            details={"source_claim_ids": source_claim_ids},
        )

    run_id = _record_run(session, f"materialize.{category}", "corpus", paper_id=paper_id)
    claim = ClaimRow(
        claim_id=new_claim_id(),
        paper_id=paper_id,
        paper_version_id=paper_version_id,
        claim_type=ClaimType(claim_type),
        category=category,
        statement=statement,
        support_state=SupportState.UNVERIFIED,
        created_by_run_id=run_id,
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(claim)
    from paperintel.database.models import ClaimEvidenceRow as _Link
    from paperintel.schemas.enums import EvidenceRole

    for evidence_id in evidence_ids:
        session.add(
            _Link(
                claim_id=claim.claim_id,
                evidence_id=evidence_id,
                role=EvidenceRole.CONTEXT,
            )
        )
    session.flush()
    return claim.claim_id


#: The full corpus function set (doc 01 §19) — used by the module health
#: check and by the corpus CLI to enumerate available views.
CORPUS_FUNCTIONS: dict[str, str] = {
    "topic_landscape": "corpus.topic_landscape",
    "method_genealogy": "corpus.method_genealogy",
    "dataset_landscape": "corpus.dataset_landscape",
    "benchmark_matrix": "corpus.benchmark_matrix",
    "contradiction_map": "corpus.contradiction_map",
    "innovation_map": "corpus.innovation_map",
    "technique_mining": "corpus.technique_mining",
    "reliability_landscape": "corpus.reliability_landscape",
    "author_network": "corpus.author_network",
    "gap_candidates": "corpus.gap_candidates",
    "negative_results": "corpus.negative_results",
    "temporal_trends": "corpus.temporal_trends",
}


def build_all_views(session: Session, *, collection_id: str | None = None) -> dict[str, dict]:
    """Run every corpus view over one scope (the corpus intelligence pass)."""
    return {
        name: func(session, collection_id=collection_id).to_dict()
        for name, func in (
            ("topic_landscape", topic_landscape),
            ("method_genealogy", method_genealogy),
            ("dataset_landscape", dataset_landscape),
            ("benchmark_matrix", benchmark_matrix),
            ("contradiction_map", contradiction_map),
            ("innovation_map", innovation_map),
            ("technique_mining", technique_mining),
            ("reliability_landscape", reliability_landscape),
            ("author_network", author_network),
            ("gap_candidates", gap_candidates),
            ("negative_results", negative_results),
            ("temporal_trends", temporal_trends),
        )
    }
