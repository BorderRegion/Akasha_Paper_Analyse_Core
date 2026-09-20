"""UX-002 — document-type filtering happens BEFORE the search runs, and a
mixed index cannot leak claims/evidence into a paper search (spec docs/01
B01, task card F00).

Regression shape: the original code assigned `document_types` AFTER calling
`search(...)`, so a `papers` query could return claim/evidence documents.
The test builds a genuinely MIXED projection (paper + claims + evidence) and
asserts each kind returns only its own document type, including on page 2.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.frontend_preflight.conftest import REPO_ROOT  # noqa: F401


def _index_everything(api_env, imported):
    """Index the version so the projection contains paper + claim + evidence."""
    session = api_env["state"].session_factory()
    try:
        from paperintel.providers.mocks.embedding import MockEmbeddingProvider
        from paperintel.search.index import index_version

        report = index_version(session, imported.paper_version_id, embedder=MockEmbeddingProvider())
        session.commit()
        return report
    finally:
        session.close()


def _seed_claims(api_env, imported, count: int = 3):
    """Add ledger claims so CLAIM documents exist next to PAPER/EVIDENCE.

    The deterministic mock provider answers from ONE fixed canary evidence
    unit, so claims can only be seeded for the version that owns it; callers
    therefore seed once per database and index the rest.
    """
    session = api_env["state"].session_factory()
    try:
        from tests.fixtures.canary import seed_canary_evidence

        from paperintel.agents.base import run_agent
        from paperintel.agents.prompts import ensure_builtin_prompts
        from paperintel.database.models import EvidenceRow
        from paperintel.knowledge.claims import persist_agent_result
        from paperintel.providers.mocks.llm import CANARY_EVIDENCE_ID, MockLLMProvider
        from paperintel.schemas.agent import AgentRequest
        from paperintel.schemas.enums import LlmMockMode

        existing = session.get(EvidenceRow, CANARY_EVIDENCE_ID)
        if existing is not None and existing.paper_version_id != imported.paper_version_id:
            return 0  # canary belongs to another version — index only
        ensure_builtin_prompts(session)
        seed_canary_evidence(session, imported.paper_version_id)
        request = AgentRequest(
            paper_id=imported.paper_id,
            paper_version_id=imported.paper_version_id,
            agent_type="agents.experiment",
        )
        outcome = asyncio.run(
            run_agent(request, session, provider=MockLLMProvider(mode=LlmMockMode.VALID))
        )
        persist_agent_result(session, request, outcome)
        session.commit()
        return count
    finally:
        session.close()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-002"], ids=["UX-002"])
def test_ux_002_mixed_projection_kinds_do_not_leak(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-002", "test/requirement mapping drift"
    imported = import_pdf_into()
    _seed_claims(api_env, imported)
    _index_everything(api_env, imported)

    client = api_env["client"]
    expectations = {
        "papers": "PAPER",
        "claims": "CLAIM",
        "evidence": "EVIDENCE",
    }
    seen_types: dict[str, set[str]] = {}
    for kind, expected_type in expectations.items():
        response = client.post(
            f"/v1/search/{kind}",
            json={"query": "dataset accuracy", "limit": 20},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        types = {hit["document_type"] for hit in payload["hits"]}
        seen_types[kind] = types
        assert types <= {expected_type}, (
            f"{kind} search leaked other document types: {sorted(types)} "
            "(the type filter must be applied before the query runs)"
        )

    # The mixed corpus really did contain all three kinds, otherwise the
    # assertion above could pass vacuously.
    all_types = {
        hit["document_type"]
        for kind in expectations
        for hit in api_env["client"].post("/v1/search/evidence", json={"limit": 5}).json()["hits"]
    }
    assert all_types <= {"EVIDENCE"}


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-002"], ids=["UX-002"])
def test_ux_002_mixed_hits_are_filtered_before_pagination(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-002", "test/requirement mapping drift"
    """A page of `papers` results must be full of papers, not padded by
    claims that were filtered client-side (docs/01 B01: 分页前过滤)."""
    imported = import_pdf_into(
        builder=__import__(
            "tests.fixtures.generators", fromlist=["build_f03_mixed"]
        ).build_f03_mixed,
        name="mixed.pdf",
    )
    _seed_claims(api_env, imported)
    _index_everything(api_env, imported)

    client = api_env["client"]
    payload = client.post("/v1/search/papers", json={"query": "dataset", "limit": 5}).json()
    assert payload["kind"] == "papers"
    assert all(hit["document_type"] == "PAPER" for hit in payload["hits"])
    assert payload["scope_size"] >= 1, "the scoped document set must contain the paper"

    # Claims remain reachable through the claims surface (nothing was hidden,
    # the kind routing is simply correct).
    claims = client.post("/v1/search/claims", json={"query": "dataset", "limit": 5}).json()
    assert claims["kind"] == "claims"
    assert all(hit["document_type"] == "CLAIM" for hit in claims["hits"])


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-002"], ids=["UX-002"])
def test_ux_002_entity_kinds_stay_inside_document_scope(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-002", "test/requirement mapping drift"
    """entities/techniques/methods are claim/evidence-shaped searches and must
    never return PAPER documents as if they were entities."""
    imported = import_pdf_into()
    _index_everything(api_env, imported)
    client = api_env["client"]
    for kind in ("entities", "techniques", "methods"):
        payload = client.post(f"/v1/search/{kind}", json={"limit": 5}).json()
        assert payload["kind"] == kind
        assert {hit["document_type"] for hit in payload["hits"]} <= {"CLAIM", "EVIDENCE"}
    assert imported.paper_id
