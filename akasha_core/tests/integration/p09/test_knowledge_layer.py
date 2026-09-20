"""P09 knowledge-layer tests: tag normalization/aliases, entities,
relations, graph expansion, collections."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.generators import build_f01_native
from tests.integration.p08.fixtures import ensure_run, make_claim, make_evidence

from paperintel.database.models import EntityRow, RelationRow, TagRow
from paperintel.errors import DomainError
from paperintel.ingest.service import import_pdf
from paperintel.knowledge.collections import (
    add_paper,
    collection_paper_ids,
    collection_stats,
    create_collection,
    get_collection,
    list_collections,
    remove_paper,
)
from paperintel.knowledge.graph import (
    add_relation,
    claim_subject,
    entity_type_for_category,
    link_claim_entities,
    neighbors,
    normalize_entity_name,
    upsert_entity,
)
from paperintel.knowledge.tags import (
    TAG_NAMESPACES,
    attach_tag,
    normalize_tag_name,
    paper_tags,
    promote_candidate,
    resolve_tag,
    tags_in_namespace,
)
from paperintel.schemas.enums import EntityType, EvidenceRole, ResourceTier, SectionClass
from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def imported(session, store, data_dir, tmp_path):
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    session.commit()
    return result


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def test_tag_normalization_is_deterministic() -> None:
    assert normalize_tag_name("Vision  Transformer") == "vision-transformer"
    assert normalize_tag_name("vision_transformer") == "vision-transformer"
    assert normalize_tag_name("  Vision/Transformer!  ") == "vision-transformer"
    assert normalize_tag_name("Self--Supervised") == "self-supervised"
    assert len(TAG_NAMESPACES) == 19


@pytest.mark.needs_db
def test_alias_resolution_never_duplicates_tags(session) -> None:
    first = resolve_tag(
        session, namespace="architecture", name="Vision Transformer", aliases=["ViT", "vit-16"]
    )
    assert first.created is True

    # Same canonical name (different case/separators) → same tag.
    second = resolve_tag(session, namespace="architecture", name="vision_transformer")
    assert second.tag_id == first.tag_id

    # An alias resolves to the canonical tag.
    third = resolve_tag(session, namespace="architecture", name="ViT")
    assert third.tag_id == first.tag_id
    assert third.matched_alias == "ViT"

    assert session.scalar(select(TagRow).where(TagRow.namespace == "architecture")) is not None
    assert len(tags_in_namespace(session, "architecture")) == 1


@pytest.mark.needs_db
def test_unknown_namespace_rejected(session) -> None:
    with pytest.raises(DomainError) as excinfo:
        resolve_tag(session, namespace="made-up-namespace", name="whatever")
    assert excinfo.value.code == "CFG_002"


@pytest.mark.needs_db
def test_conflicting_alias_cannot_be_repointed(session) -> None:
    resolve_tag(session, namespace="method", name="Distillation", aliases=["KD"])
    with pytest.raises(DomainError) as excinfo:
        resolve_tag(session, namespace="method", name="Knowledge Distillation", aliases=["KD"])
    assert excinfo.value.code == "CFG_002"


@pytest.mark.needs_db
def test_candidate_tags_require_explicit_promotion(session, imported) -> None:
    """Free-text model tags are candidates only (doc 01 §15)."""
    tag_id, created = attach_tag(
        session,
        paper_id=imported.paper_id,
        namespace="novelty",
        name="first-to-combine",
        is_candidate=True,
    )
    assert created is True
    tags = paper_tags(session, imported.paper_id)
    assert tags[0]["is_candidate"] is True

    # Candidates are excluded from the canonical listing.
    assert paper_tags(session, imported.paper_id, include_candidates=False) == []

    assert promote_candidate(session, paper_id=imported.paper_id, tag_id=tag_id) is True
    assert (
        paper_tags(session, imported.paper_id, include_candidates=False)[0]["is_candidate"] is False
    )
    # Promoting twice is a no-op, not an error.
    assert promote_candidate(session, paper_id=imported.paper_id, tag_id=tag_id) is False


@pytest.mark.needs_db
def test_attach_tag_is_idempotent(session, imported) -> None:
    _, created_first = attach_tag(
        session, paper_id=imported.paper_id, namespace="dataset", name="ImageNet"
    )
    _, created_second = attach_tag(
        session, paper_id=imported.paper_id, namespace="dataset", name="imagenet"
    )
    assert created_first is True
    assert created_second is False


# ---------------------------------------------------------------------------
# entities + relations
# ---------------------------------------------------------------------------


def test_entity_normalization_and_category_mapping() -> None:
    assert normalize_entity_name("ResNet-50") == "resnet 50"
    assert normalize_entity_name("ResNet 50") == "resnet 50"
    assert entity_type_for_category("method.component") is EntityType.METHOD
    assert entity_type_for_category("experiment.dataset") is EntityType.DATASET
    assert entity_type_for_category("experiment.metric") is EntityType.METRIC
    assert entity_type_for_category("technique.name") is EntityType.TECHNIQUE
    assert entity_type_for_category("unknown.category") is None


def test_claim_subject_extraction() -> None:
    assert claim_subject("Method A reaches 82.5% accuracy on the benchmark.") == "Method A"
    assert claim_subject("The dataset contains 1,000 samples.") == "dataset"
    assert claim_subject("No verb here at all") is None


@pytest.mark.needs_db
def test_entity_upsert_is_idempotent_and_alias_aware(session) -> None:
    # Aliases declared at creation make alternative spellings resolve to
    # the same entity instead of fragmenting it.
    first = upsert_entity(
        session, entity_type=EntityType.METHOD, name="ResNet-50", aliases=["ResNet50"]
    )
    assert first.created is True

    # Same name modulo case/separators → same entity.
    second = upsert_entity(session, entity_type=EntityType.METHOD, name="resnet 50")
    assert second.entity_id == first.entity_id
    assert second.created is False

    # A registered alias resolves to the canonical entity.
    third = upsert_entity(session, entity_type=EntityType.METHOD, name="ResNet50")
    assert third.entity_id == first.entity_id
    assert third.created is False

    # A DIFFERENT normal form that was never declared an alias is a new
    # entity (no silent fuzzy merging).
    fourth = upsert_entity(session, entity_type=EntityType.METHOD, name="ResNet-101")
    assert fourth.entity_id != first.entity_id

    # Different types never collide even with the same name.
    dataset = upsert_entity(session, entity_type=EntityType.DATASET, name="ResNet-50")
    assert dataset.entity_id != first.entity_id


@pytest.mark.needs_db
def test_relations_are_idempotent_and_evidence_linked(session, imported) -> None:
    paper_entity = upsert_entity(session, entity_type=EntityType.PAPER, name=imported.paper_id)
    method = upsert_entity(session, entity_type=EntityType.METHOD, name="Method A")
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy.",
        section_class=SectionClass.RESULT,
    )

    relation_id, created = add_relation(
        session,
        source_entity_id=paper_entity.entity_id,
        relation_type="USES_METHOD",
        target_entity_id=method.entity_id,
        paper_id=imported.paper_id,
        evidence_ids=[evidence.evidence_id],
        confidence=0.8,
        run_id=ensure_run(session, imported.paper_version_id),
    )
    assert created is True

    again, created_again = add_relation(
        session,
        source_entity_id=paper_entity.entity_id,
        relation_type="USES_METHOD",
        target_entity_id=method.entity_id,
        paper_id=imported.paper_id,
        evidence_ids=[evidence.evidence_id],
        confidence=0.9,
    )
    assert again == relation_id
    assert created_again is False
    relation = session.get(RelationRow, relation_id)
    assert relation.confidence == 0.9  # highest confidence wins
    assert relation.evidence_ids == [evidence.evidence_id]


@pytest.mark.needs_db
def test_self_relations_and_unknown_entities_rejected(session, imported) -> None:
    entity = upsert_entity(session, entity_type=EntityType.METHOD, name="Solo")
    with pytest.raises(DomainError):
        add_relation(
            session,
            source_entity_id=entity.entity_id,
            relation_type="RELATED_TO",
            target_entity_id=entity.entity_id,
        )
    with pytest.raises(DomainError):
        add_relation(
            session,
            source_entity_id=entity.entity_id,
            relation_type="RELATED_TO",
            target_entity_id="ent_01UNKNOWN000000000000000000",
        )


@pytest.mark.needs_db
def test_graph_expansion_is_bounded_and_cycle_safe(session, imported) -> None:
    a = upsert_entity(session, entity_type=EntityType.METHOD, name="Method A")
    b = upsert_entity(session, entity_type=EntityType.DATASET, name="Dataset B")
    c = upsert_entity(session, entity_type=EntityType.METRIC, name="Metric C")
    add_relation(
        session,
        source_entity_id=a.entity_id,
        relation_type="USES_DATASET",
        target_entity_id=b.entity_id,
        paper_id=imported.paper_id,
    )
    add_relation(
        session,
        source_entity_id=b.entity_id,
        relation_type="REPORTS_METRIC",
        target_entity_id=c.entity_id,
        paper_id=imported.paper_id,
    )
    # A cycle back to the seed must not loop forever.
    add_relation(
        session,
        source_entity_id=c.entity_id,
        relation_type="RELATED_TO",
        target_entity_id=a.entity_id,
        paper_id=imported.paper_id,
    )

    # One hop from `a` reaches b (a→b) and also c, because the cycle edge
    # c→a touches the seed as well.
    one_hop = neighbors(session, a.entity_id, depth=1)
    assert {a.entity_id, b.entity_id, c.entity_id} <= set(one_hop)
    # Restricting the relation types narrows the expansion exactly.
    dataset_only = neighbors(session, a.entity_id, depth=1, relation_types={"USES_DATASET"})
    assert set(dataset_only) == {a.entity_id, b.entity_id}

    two_hops = neighbors(session, a.entity_id, depth=2)
    assert {a.entity_id, b.entity_id, c.entity_id} <= set(two_hops)

    # Relation-type filtering blocks paths that traverse other edge types:
    # from `a` there is no REPORTS_METRIC edge, so the walk cannot reach c.
    assert set(neighbors(session, a.entity_id, depth=2, relation_types={"REPORTS_METRIC"})) == {
        a.entity_id
    }
    # From `b` (which owns the REPORTS_METRIC edge) the filter reaches c.
    filtered = neighbors(session, b.entity_id, depth=1, relation_types={"REPORTS_METRIC"})
    assert c.entity_id in filtered


@pytest.mark.needs_db
def test_claim_graph_carries_provenance(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
        section_class=SectionClass.RESULT,
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.5% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        category="method.component",
    )
    stats = link_claim_entities(session, paper_id=imported.paper_id, claims=[claim])
    assert stats["relations_created"] >= 1
    assert stats["claims_linked"] == 1

    relation = session.scalar(select(RelationRow).where(RelationRow.paper_id == imported.paper_id))
    assert relation.relation_type == "USES_METHOD"
    assert relation.evidence_ids == [evidence.evidence_id]
    assert relation.created_by_run_id == claim.created_by_run_id

    entities = session.scalars(
        select(EntityRow).where(EntityRow.entity_type == EntityType.METHOD)
    ).all()
    assert any(entity.canonical_name == "Method A" for entity in entities)


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_collection_lifecycle_and_scope(session, imported) -> None:
    collection = create_collection(
        session,
        name="Vision project",
        purpose="track vision papers",
        research_questions=["How do ViTs scale?"],
        preferred_tags=[{"namespace": "architecture", "name": "vision-transformer"}],
    )
    link, created = add_paper(
        session,
        collection_id=collection.collection_id,
        paper_id=imported.paper_id,
        pinned=True,
        priority_override_tier=ResourceTier.T3_DEEP,
        relevance_note="core paper",
    )
    assert created is True
    assert link.pinned is True

    stats = collection_stats(session, collection.collection_id)
    assert stats.paper_count == 1
    assert stats.pinned_count == 1
    assert stats.override_count == 1

    assert collection_paper_ids(session, collection.collection_id) == [imported.paper_id]

    # Idempotent add: no duplicate row, but flags update.
    _, created_again = add_paper(
        session,
        collection_id=collection.collection_id,
        paper_id=imported.paper_id,
        relevance_note="updated",
    )
    assert created_again is True  # note changed
    assert collection_stats(session, collection.collection_id).paper_count == 1

    assert (
        remove_paper(session, collection_id=collection.collection_id, paper_id=imported.paper_id)
        is True
    )
    assert collection_paper_ids(session, collection.collection_id) == []


@pytest.mark.needs_db
def test_collection_validates_papers_and_tags(session) -> None:
    with pytest.raises(DomainError):
        create_collection(
            session,
            name="bad tags",
            preferred_tags=[{"namespace": "not-a-namespace", "name": "x"}],
        )
    collection = create_collection(session, name="ok")
    with pytest.raises(DomainError):
        add_paper(
            session,
            collection_id=collection.collection_id,
            paper_id="pap_01UNKNOWN000000000000000000",
        )
    with pytest.raises(DomainError):
        get_collection(session, "col_01UNKNOWN000000000000000000")


@pytest.mark.needs_db
def test_list_collections_reports_stats(session) -> None:
    create_collection(session, name="one")
    create_collection(session, name="two")
    stats = list_collections(session)
    assert [entry.name for entry in stats] == ["one", "two"]
    assert all(entry.paper_count == 0 for entry in stats)
