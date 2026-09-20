"""P11 corpus intelligence tests (doc 01 §19, doc 05 P11).

The corpus fixture is three papers in one collection with controlled
overlaps (shared topics, a shared dataset, a numeric disagreement, an
author link, a technique and a gap), so every corpus view has a decidable
expectation.

Gate requirement: cross-paper outputs must retain source paper/claim/
evidence provenance — asserted for EVERY finding of every view.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.fixtures.generators import build_f01_native, build_f03_mixed, build_f04_rich

from paperintel.corpus.intelligence import (
    CORPUS_FUNCTIONS,
    author_network,
    benchmark_matrix,
    build_all_views,
    contradiction_map,
    dataset_landscape,
    gap_candidates,
    innovation_map,
    materialize_finding,
    method_genealogy,
    negative_results,
    reliability_landscape,
    technique_mining,
    temporal_trends,
    topic_landscape,
)
from paperintel.database.models import (
    AnalysisRunRow,
    ClaimEvidenceRow,
    ClaimRow,
    EvidenceRow,
    SectionRow,
)
from paperintel.errors import DomainError
from paperintel.ids import IdPrefix, new_claim_id, new_evidence_id, new_id
from paperintel.ingest.service import import_pdf
from paperintel.knowledge.collections import add_paper, create_collection
from paperintel.knowledge.graph import add_relation, upsert_entity
from paperintel.knowledge.tags import attach_tag
from paperintel.schemas.enums import (
    ClaimType,
    DataQualityState,
    EntityType,
    EvidenceRole,
    EvidenceType,
    SectionClass,
    SourceMethod,
    SupportState,
)
from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _evidence(session: Session, version_id: str, text: str, run_id: str) -> EvidenceRow:
    section = SectionRow(
        section_id=new_id(IdPrefix.EVIDENCE).replace("ev_", "sec_"),
        paper_version_id=version_id,
        original_heading="RESULT section",
        normalized_class=SectionClass.RESULT,
        page_start=1,
        page_end=1,
        ordinal=0,
    )
    session.add(section)
    row = EvidenceRow(
        evidence_id=new_evidence_id(),
        paper_version_id=version_id,
        evidence_type=EvidenceType.PARAGRAPH,
        page_start=1,
        page_end=1,
        section_id=section.section_id,
        text=text,
        source_method=SourceMethod.PDF_NATIVE,
        quality_state=DataQualityState.GOOD,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        extraction_run_id=run_id,
    )
    session.add(row)
    session.flush()
    return row


def _claim(
    session: Session,
    imported,
    *,
    statement: str,
    category: str,
    evidence: EvidenceRow,
    claim_type: ClaimType = ClaimType.FACT,
    support_state: SupportState = SupportState.SUPPORTED,
    run_id: str | None = None,
) -> ClaimRow:
    from tests.integration.p08.fixtures import ensure_run

    run_id = run_id or ensure_run(session, imported.paper_version_id)
    claim = ClaimRow(
        claim_id=new_claim_id(),
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        claim_type=claim_type,
        category=category,
        statement=statement,
        support_state=support_state,
        created_by_run_id=run_id,
        pipeline_version="1.0.0",
    )
    session.add(claim)
    session.add(
        ClaimEvidenceRow(
            claim_id=claim.claim_id, evidence_id=evidence.evidence_id, role=EvidenceRole.SUPPORT
        )
    )
    session.flush()
    return claim


@pytest.fixture()
def corpus(session, store, data_dir, tmp_path):
    """Three papers in one collection with deliberate cross-paper overlap."""
    from tests.integration.p08.fixtures import ensure_run

    # Three DISTINCT papers (different fixtures → different titles →
    # distinct paper identities; appending bytes would only create a new
    # VERSION of the same paper).
    papers = {}
    builders = (
        ("p1.pdf", build_f01_native),
        ("p2.pdf", build_f04_rich),
        ("p3.pdf", build_f03_mixed),
    )
    for name, builder in builders:
        path = tmp_path / name
        path.write_bytes(builder())
        papers[name] = asyncio.run(
            import_pdf(path, session=session, store=store, data_dir=data_dir)
        )

    collection = create_collection(session, name="corpus test", purpose="P11")
    for imported in papers.values():
        add_paper(session, collection_id=collection.collection_id, paper_id=imported.paper_id)

    p1, p2, p3 = papers["p1.pdf"], papers["p2.pdf"], papers["p3.pdf"]

    # Publication dates for temporal trends.
    from paperintel.database.models import PaperVersionRow

    session.get(PaperVersionRow, p1.paper_version_id).publication_date = datetime(
        2022, 5, 1, tzinfo=UTC
    )
    session.get(PaperVersionRow, p2.paper_version_id).publication_date = datetime(
        2023, 6, 1, tzinfo=UTC
    )
    session.get(PaperVersionRow, p3.paper_version_id).publication_date = datetime(
        2024, 7, 1, tzinfo=UTC
    )

    # Evidence + claims.
    run1 = ensure_run(session, p1.paper_version_id)
    run2 = ensure_run(session, p2.paper_version_id)
    run3 = ensure_run(session, p3.paper_version_id)
    ev1 = _evidence(
        session,
        p1.paper_version_id,
        "Vision transformers reach 84.2% top-1 accuracy on ImageNet.",
        run1,
    )
    ev2 = _evidence(
        session, p2.paper_version_id, "Graph networks reach 79.1% top-1 accuracy on ImageNet.", run2
    )
    ev3 = _evidence(
        session,
        p3.paper_version_id,
        "Vision transformers reach 86.0% top-1 accuracy on ImageNet.",
        run3,
    )

    claims = {
        "method1": _claim(
            session,
            p1,
            statement="The hybrid attention block reaches 84.2% accuracy.",
            category="method.component",
            evidence=ev1,
        ),
        "method2": _claim(
            session,
            p2,
            statement="The graph network reaches 79.1% accuracy.",
            category="method.component",
            evidence=ev2,
        ),
        "dataset1": _claim(
            session,
            p1,
            statement="ImageNet contains 1.3 million labelled images.",
            category="experiment.dataset",
            evidence=ev1,
        ),
        "dataset2": _claim(
            session,
            p2,
            statement="ImageNet contains 1.3 million labelled images.",
            category="experiment.dataset",
            evidence=ev2,
        ),
        "result1": _claim(
            session,
            p1,
            statement="Vision transformers reach 84.2% top-1 accuracy on ImageNet.",
            category="result.main",
            evidence=ev1,
        ),
        # Same subject, DIFFERENT number, different paper → numeric disagreement.
        "result3": _claim(
            session,
            p3,
            statement="Vision transformers reach 86.0% top-1 accuracy on ImageNet.",
            category="result.main",
            evidence=ev3,
        ),
        "technique1": _claim(
            session,
            p1,
            statement="Mixed precision training reduces memory cost.",
            category="technique.procedure",
            evidence=ev1,
        ),
        "reliability2": _claim(
            session,
            p2,
            statement="The baseline was trained with 4x more compute.",
            category="reliability.budget",
            evidence=ev2,
            claim_type=ClaimType.CRITIQUE,
        ),
        "gap1": _claim(
            session,
            p1,
            statement="Cross-domain transfer remains unexplored.",
            category="research.gap",
            evidence=ev1,
            claim_type=ClaimType.INFERENCE,
        ),
        "limitation2": _claim(
            session,
            p2,
            statement="Evaluation covers a single dataset.",
            category="limitation.unstated",
            evidence=ev2,
            claim_type=ClaimType.INFERENCE,
        ),
        "contribution1": _claim(
            session,
            p1,
            statement="We introduce a hybrid attention block.",
            category="contribution.declared",
            evidence=ev1,
        ),
        "failure2": _claim(
            session,
            p2,
            statement="The method fails on long sequences.",
            category="critique.generalization",
            evidence=ev2,
            claim_type=ClaimType.CRITIQUE,
        ),
        "unsupported3": _claim(
            session,
            p3,
            statement="The gain generalizes to all vision tasks.",
            category="result.superiority",
            evidence=ev3,
            support_state=SupportState.UNSUPPORTED,
        ),
    }

    # Topics (tags) across papers.
    for imported in (p1, p2, p3):
        attach_tag(session, paper_id=imported.paper_id, namespace="domain", name="vision")
    attach_tag(session, paper_id=p1.paper_id, namespace="method", name="transformer")
    attach_tag(session, paper_id=p3.paper_id, namespace="method", name="transformer")
    attach_tag(session, paper_id=p2.paper_id, namespace="method", name="graph-network")
    attach_tag(session, paper_id=p1.paper_id, namespace="novelty", name="new-architecture")

    # Graph: methods, authors, datasets.
    author = upsert_entity(session, entity_type=EntityType.AUTHOR, name="A. Author")
    from paperintel.knowledge.graph import link_claim_entities

    link_claim_entities(
        session,
        paper_id=p1.paper_id,
        claims=[claims["method1"], claims["dataset1"], claims["technique1"]],
    )
    link_claim_entities(
        session,
        paper_id=p2.paper_id,
        claims=[claims["method2"], claims["dataset2"]],
    )
    paper_entity1 = upsert_entity(session, entity_type=EntityType.PAPER, name=p1.paper_id)
    add_relation(
        session,
        source_entity_id=paper_entity1.entity_id,
        relation_type="AUTHORED_BY",
        target_entity_id=author.entity_id,
        paper_id=p1.paper_id,
        evidence_ids=[ev1.evidence_id],
    )
    paper_entity2 = upsert_entity(session, entity_type=EntityType.PAPER, name=p2.paper_id)
    add_relation(
        session,
        source_entity_id=paper_entity2.entity_id,
        relation_type="AUTHORED_BY",
        target_entity_id=author.entity_id,
        paper_id=p2.paper_id,
        evidence_ids=[ev2.evidence_id],
    )

    session.commit()
    return {
        "papers": papers,
        "collection": collection,
        "claims": claims,
        "p1": p1,
        "p2": p2,
        "p3": p3,
    }


# ---------------------------------------------------------------------------
# provenance is universal
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_every_view_retains_source_provenance(session, corpus) -> None:
    """Doc 05 P11: cross-paper outputs must retain source paper/claim/
    evidence provenance."""
    views = build_all_views(session, collection_id=corpus["collection"].collection_id)
    assert set(views) == set(CORPUS_FUNCTIONS)

    for name, view in views.items():
        assert view["finding_count"] == len(view["findings"])
        for finding in view["findings"]:
            assert "provenance" in finding, f"{name} finding lacks provenance"
            provenance = finding["provenance"]
            for key in ("paper_ids", "paper_version_ids", "claim_ids", "evidence_ids"):
                assert key in provenance, f"{name} provenance lacks {key}"
            assert provenance["paper_ids"] or provenance["evidence_ids"], (
                f"{name} finding has empty provenance"
            )


@pytest.mark.needs_db
def test_every_view_is_audited_with_a_run(session, corpus) -> None:
    views = build_all_views(session, collection_id=corpus["collection"].collection_id)
    for view in views.values():
        assert view["run_id"].startswith("run_")
        assert session.get(AnalysisRunRow, view["run_id"]) is not None
    corpus_runs = session.scalars(
        select(AnalysisRunRow).where(AnalysisRunRow.agent_type.like("corpus.%"))
    ).all()
    assert len(corpus_runs) == len(CORPUS_FUNCTIONS)


# ---------------------------------------------------------------------------
# individual views
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_topic_landscape_groups_papers_by_topic(session, corpus) -> None:
    view = topic_landscape(session, collection_id=corpus["collection"].collection_id)
    by_topic = {finding["topic"]: finding for finding in view.findings}
    assert by_topic["vision"]["paper_count"] == 3
    assert by_topic["transformer"]["paper_count"] == 2
    assert by_topic["graph-network"]["paper_count"] == 1
    # Findings cite the papers they summarize.
    assert len(by_topic["vision"]["provenance"]["paper_ids"]) == 3


@pytest.mark.needs_db
def test_method_genealogy_exposes_nodes_and_edges(session, corpus) -> None:
    view = method_genealogy(session, collection_id=corpus["collection"].collection_id)
    methods = {node["method"] for node in view.findings}
    assert methods, "expected method nodes from claim-derived relations"
    for node in view.findings:
        assert node["paper_count"] >= 1
        assert node["provenance"]["evidence_ids"]
    # Edges carry their own provenance and live alongside the nodes.
    assert view.extra["edges"]
    assert all(edge["provenance"] for edge in view.extra["edges"])


@pytest.mark.needs_db
def test_dataset_landscape_links_datasets_to_papers(session, corpus) -> None:
    view = dataset_landscape(session, collection_id=corpus["collection"].collection_id)
    imagenet = next(
        (finding for finding in view.findings if "imagenet" in finding["dataset"].lower()),
        None,
    )
    assert imagenet is not None
    assert imagenet["paper_count"] >= 2
    assert imagenet["provenance"]["claim_ids"]


@pytest.mark.needs_db
def test_benchmark_matrix_has_values_and_provenance(session, corpus) -> None:
    view = benchmark_matrix(session, collection_id=corpus["collection"].collection_id)
    assert view.findings
    for cell in view.findings:
        assert cell["value"], "matrix cells must carry the reported value"
        assert cell["paper_id"]
        assert cell["support_state"] in {state.value for state in SupportState}
        assert cell["provenance"]["evidence_ids"]


@pytest.mark.needs_db
def test_contradiction_map_finds_cross_paper_numeric_disagreement(session, corpus) -> None:
    view = contradiction_map(session, collection_id=corpus["collection"].collection_id)
    disagreements = [
        finding for finding in view.findings if finding["kind"] == "numeric_disagreement"
    ]
    assert disagreements, "expected the 84.2 vs 86.0 accuracy disagreement"
    disagreement = next(
        finding
        for finding in disagreements
        if {"84.2" in finding["values"][0], "86.0" in finding["values"][1]} == {True}
    )
    assert len(disagreement["paper_ids"]) == 2
    assert "84.2" in disagreement["values"][0]
    assert "86.0" in disagreement["values"][1]
    # The generic-phrasing pair ("reaches ... accuracy") is NOT reported.
    for finding in disagreements:
        assert "reaches" not in finding["subject_tokens"] or len(finding["subject_tokens"]) > 2
    assert disagreement["provenance"]["claim_ids"]

    # Unsupported claims are surfaced as well.
    assert any(finding["kind"] == "claim_unsupported" for finding in view.findings)


@pytest.mark.needs_db
def test_innovation_map_reports_contributions_and_novelty(session, corpus) -> None:
    view = innovation_map(session, collection_id=corpus["collection"].collection_id)
    p1 = next(finding for finding in view.findings if finding["paper_id"] == corpus["p1"].paper_id)
    assert any("hybrid attention" in statement for statement in p1["declared_contributions"])
    assert p1["novelty_tags"] == ["new-architecture"]


@pytest.mark.needs_db
def test_technique_mining_groups_by_aspect(session, corpus) -> None:
    view = technique_mining(session, collection_id=corpus["collection"].collection_id)
    assert any(finding["aspect"] == "technique.procedure" for finding in view.findings)
    for finding in view.findings:
        assert finding["statements"]
        assert finding["provenance"]["claim_ids"]


@pytest.mark.needs_db
def test_reliability_landscape_summarizes_per_paper(session, corpus) -> None:
    view = reliability_landscape(session, collection_id=corpus["collection"].collection_id)
    p2 = next(finding for finding in view.findings if finding["paper_id"] == corpus["p2"].paper_id)
    assert "reliability.budget" in p2["dimensions"]
    assert p2["finding_count"] >= 1
    assert p2["provenance"]["evidence_ids"]


@pytest.mark.needs_db
def test_author_network_links_coauthors(session, corpus) -> None:
    view = author_network(session, collection_id=corpus["collection"].collection_id)
    assert any(author["author"] == "A. Author" for author in view.findings)
    author_entry = next(author for author in view.findings if author["author"] == "A. Author")
    assert author_entry["paper_count"] >= 2
    assert author_entry["provenance"]["paper_ids"]


@pytest.mark.needs_db
def test_gap_candidates_include_explicit_and_thin_topics(session, corpus) -> None:
    view = gap_candidates(session, collection_id=corpus["collection"].collection_id)
    kinds = {finding["kind"] for finding in view.findings}
    assert "explicit_gap" in kinds  # research.gap claim
    assert "thin_topic" in kinds  # graph-network covers one paper
    explicit = next(finding for finding in view.findings if finding["kind"] == "explicit_gap")
    assert explicit["provenance"]["evidence_ids"]


@pytest.mark.needs_db
def test_negative_results_surface_failures_and_unsupported(session, corpus) -> None:
    view = negative_results(session, collection_id=corpus["collection"].collection_id)
    kinds = {finding["kind"] for finding in view.findings}
    assert "claim_unsupported" in kinds
    assert "failure_report" in kinds


@pytest.mark.needs_db
def test_temporal_trends_group_by_year(session, corpus) -> None:
    view = temporal_trends(session, collection_id=corpus["collection"].collection_id)
    years = {finding["year"]: finding for finding in view.findings}
    assert {2022, 2023, 2024} <= set(years)
    assert years[2022]["paper_count"] >= 1
    assert years[2022]["topics"]


# ---------------------------------------------------------------------------
# scope + materialization
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_empty_collection_scope_returns_nothing(session, corpus) -> None:
    """An empty scope is NOT the whole corpus — an empty collection must
    never widen into a global query."""
    empty = create_collection(session, name="empty")
    session.flush()
    view = topic_landscape(session, collection_id=empty.collection_id)
    assert view.findings == []
    assert view.warnings
    assert topic_landscape(session).findings  # corpus scope still works


@pytest.mark.needs_db
def test_corpus_scope_covers_all_papers(session, corpus) -> None:
    global_view = topic_landscape(session)
    scoped = topic_landscape(session, collection_id=corpus["collection"].collection_id)
    assert len(global_view.findings) >= len(scoped.findings)


@pytest.mark.needs_db
def test_materialize_finding_creates_evidence_linked_claim(session, corpus) -> None:
    """Doc 01 §19: corpus outputs are also claims — attributable findings
    become ledger claims linked to their source evidence."""
    source = corpus["claims"]["result1"]
    claim_id = materialize_finding(
        session,
        paper_id=corpus["p1"].paper_id,
        paper_version_id=corpus["p1"].paper_version_id,
        category="result.corpus_context",
        statement="This paper's result is part of a corpus-wide accuracy trend.",
        source_claim_ids=[source.claim_id],
    )
    session.flush()
    claim = session.get(ClaimRow, claim_id)
    assert claim is not None
    assert claim.support_state is SupportState.UNVERIFIED
    links = session.scalars(
        select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id == claim_id)
    ).all()
    assert links, "materialized corpus claim must carry evidence links"
    run = session.get(AnalysisRunRow, claim.created_by_run_id)
    assert run.agent_type.startswith("corpus.")


@pytest.mark.needs_db
def test_materialize_finding_refuses_unverifiable_claims(session, corpus) -> None:
    with pytest.raises(DomainError) as excinfo:
        materialize_finding(
            session,
            paper_id=corpus["p1"].paper_id,
            paper_version_id=corpus["p1"].paper_version_id,
            category="result.corpus_context",
            statement="A corpus statement without sources.",
            source_claim_ids=[],
        )
    assert excinfo.value.code == "CLAIM_001"

    with pytest.raises(DomainError):
        materialize_finding(
            session,
            paper_id=corpus["p1"].paper_id,
            paper_version_id=corpus["p1"].paper_version_id,
            category="result.corpus_context",
            statement="A corpus statement with an unknown source.",
            source_claim_ids=["clm_01UNKNOWN000000000000000000"],
        )


@pytest.mark.needs_db
def test_corpus_functions_are_all_implemented() -> None:
    """The doc 01 §19 function list is complete (no stubs)."""
    expected = {
        "topic_landscape",
        "method_genealogy",
        "dataset_landscape",
        "benchmark_matrix",
        "contradiction_map",
        "innovation_map",
        "technique_mining",
        "reliability_landscape",
        "author_network",
        "gap_candidates",
        "negative_results",
        "temporal_trends",
    }
    assert set(CORPUS_FUNCTIONS) == expected
    for value in CORPUS_FUNCTIONS.values():
        assert value.startswith("corpus.")
