"""UX-004 — the paper pipeline view scopes in SQL before limiting
(spec docs/01 B07) and UX-005 — has_evidence only judges the version that is
being analyzed (spec docs/01 B09).

Both were real defects in the baseline: the pipeline view fetched the newest
200 jobs of the WHOLE library and filtered in Python (an old paper's job could
vanish), and reanalyze asked "does ANY evidence exist in the database?" so a
different version's evidence made an unanswered version look analyzed.
"""

from __future__ import annotations

import asyncio

import pytest

from paperintel.schemas.enums import PipelineStage, ResourceTier


def _make_job(state, imported, *, stage=PipelineStage.ANALYZED):
    """Create a real job row for the imported version through the engine."""
    from paperintel.workflow import engine

    session = state.session_factory()
    try:
        job = engine.create_job(
            session,
            paper_id=imported.paper_id,
            paper_version_id=imported.paper_version_id,
            current_stage=stage,
        )
        session.commit()
        return job.job_id
    finally:
        session.close()


def _flood_other_papers(state, count: int) -> None:
    """Create many NEWER jobs for other papers (the old 200-row window).

    Rows are inserted in dependency order with explicit flushes: paper →
    version → job. The asset FK reuses a real asset so the fixture never
    fabricates a dangling reference.
    """
    from sqlalchemy import select

    from paperintel.database.models import AssetRow, JobRow, PaperRow, PaperVersionRow
    from paperintel.ids import new_job_id, new_paper_id, new_paper_version_id
    from paperintel.schemas.common import utcnow
    from paperintel.schemas.enums import ResourceTier, TaskState

    session = state.session_factory()
    try:
        asset_id = session.scalar(select(AssetRow.asset_id).limit(1))
        assert asset_id, "flood fixture needs one real asset to reference"
        now = utcnow()
        papers: list[tuple[str, str]] = []
        for index in range(count):
            paper_id = new_paper_id()
            version_id = new_paper_version_id()
            session.add(
                PaperRow(
                    paper_id=paper_id,
                    canonical_title=f"Flood paper {index}",
                    normalized_title=f"flood paper {index}",
                )
            )
            papers.append((paper_id, version_id))
            session.flush()
            session.add(
                PaperVersionRow(
                    paper_version_id=version_id,
                    paper_id=paper_id,
                    version_label="v1",
                    source_type="local_file",
                    content_sha256=f"{index:064d}",
                    asset_id=asset_id,
                )
            )
            session.flush()
            session.add(
                JobRow(
                    job_id=new_job_id(),
                    paper_id=paper_id,
                    paper_version_id=version_id,
                    state=TaskState.QUEUED,
                    current_stage=PipelineStage.IMPORTED,
                    requested_tier=ResourceTier.T2_FULL,
                    effective_tier=ResourceTier.T2_FULL,
                    priority=0,
                    trace_id=None,
                    created_at=now,
                )
            )
            session.flush()
        session.commit()
    finally:
        session.close()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-004"], ids=["UX-004"])
def test_ux_004_pipeline_view_survives_more_than_200_newer_jobs(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-004", "test/requirement mapping drift"
    imported = import_pdf_into()
    job_id = _make_job(api_env["state"], imported)

    # 210 newer jobs for other papers: a "latest 200 jobs then filter" view
    # would drop this paper's job entirely.
    _flood_other_papers(api_env["state"], 210)

    client = api_env["client"]
    response = client.get(f"/v1/papers/{imported.paper_id}/pipeline")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["job_id"] == job_id, (
        "pipeline view lost the paper's job behind the global 200-row window "
        "(scope must be applied in SQL before any limit)"
    )
    assert payload["state"] in {
        "PENDING",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "SUCCEEDED_WITH_WARNINGS",
    }
    assert payload["requested_tier"] is None or isinstance(payload["requested_tier"], str)


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-004"], ids=["UX-004"])
def test_ux_004_pipeline_view_is_per_paper(requirement: str, api_env, import_pdf_into) -> None:
    assert requirement == "UX-004", "test/requirement mapping drift"
    first = import_pdf_into(name="p1.pdf")
    second = import_pdf_into(
        builder=__import__(
            "tests.fixtures.generators", fromlist=["build_f03_mixed"]
        ).build_f03_mixed,
        name="p2.pdf",
    )
    first_job = _make_job(api_env["state"], first)
    second_job = _make_job(api_env["state"], second)

    client = api_env["client"]
    assert client.get(f"/v1/papers/{first.paper_id}/pipeline").json()["job_id"] == first_job
    assert client.get(f"/v1/papers/{second.paper_id}/pipeline").json()["job_id"] == second_job


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-005"], ids=["UX-005"])
def test_ux_005_has_evidence_is_version_scoped(
    requirement: str, api_env, import_pdf_into, tmp_path
) -> None:
    assert requirement == "UX-005", "test/requirement mapping drift"
    """A version WITHOUT evidence must report has_evidence=False even when a
    sibling version of the same paper has evidence."""
    from tests.fixtures.generators import build_f04_rich

    # Version A: imported WITHOUT evidence persistence (import only).
    version_a = import_pdf_into(name="a.pdf", persist_evidence=False)
    # Version B: a second, different version of the same paper WITH evidence.
    version_b = import_pdf_into(builder=build_f04_rich, name="b.pdf", persist_evidence=True)
    assert version_a.paper_version_id != version_b.paper_version_id

    client = api_env["client"]
    response = client.post(
        f"/v1/papers/{version_a.paper_id}/reanalyze",
        params={"paper_version_id": version_a.paper_version_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["paper_version_id"] == version_a.paper_version_id, (
        "the analyzed version must be reported back so the client can pin it"
    )
    assert payload["has_evidence"] is False, (
        "has_evidence looked at the whole database instead of the selected version"
    )

    # The version that DOES have evidence reports True.
    with_evidence = client.post(
        f"/v1/papers/{version_b.paper_id}/reanalyze",
        params={"paper_version_id": version_b.paper_version_id},
    ).json()
    assert with_evidence["has_evidence"] is True


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-005"], ids=["UX-005"])
def test_ux_005_reanalyze_rejects_a_foreign_version(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-005", "test/requirement mapping drift"
    first = import_pdf_into(name="x.pdf")
    second = import_pdf_into(
        builder=__import__(
            "tests.fixtures.generators", fromlist=["build_f03_mixed"]
        ).build_f03_mixed,
        name="y.pdf",
    )
    response = api_env["client"].post(
        f"/v1/papers/{first.paper_id}/reanalyze",
        params={"paper_version_id": second.paper_version_id},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CFG_002"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-005"], ids=["UX-005"])
def test_ux_005_reanalyze_defaults_to_latest_and_reports_it(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-005", "test/requirement mapping drift"
    imported = import_pdf_into()
    response = api_env["client"].post(f"/v1/papers/{imported.paper_id}/reanalyze")
    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_version_id"] == imported.paper_version_id
    assert payload["job_id"].startswith("job_")


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-005"], ids=["UX-005"])
def test_ux_005_tier_endpoint_still_records_manual_choice(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-005", "test/requirement mapping drift"
    """Guarding B09 must not break the tier endpoint the workbench uses."""
    imported = import_pdf_into()
    response = api_env["client"].post(
        f"/v1/papers/{imported.paper_id}/tier", json={"tier": ResourceTier.T3_DEEP.value}
    )
    assert response.status_code == 200
    assert response.json()["effective_tier"] == "T3_DEEP"
    assert imported.paper_id
    assert asyncio is not None
