"""Regression tests for cookie-authenticated routes and durable tag mutations."""

import pytest
from sqlalchemy import func, select

from paperintel.database.models import PaperTagRow, TagAliasRow
from paperintel.knowledge.tags import resolve_tag

pytestmark = pytest.mark.needs_db


@pytest.mark.parametrize("tier", ["T0_INDEX", "T1_SCAN", "T2_FULL"])
def test_browser_length_upload_key_replays_and_requested_tier_reaches_job(api_env, tier):
    from tests.fixtures.generators import build_f01_native

    from paperintel.database.models import JobRow, TaskRow
    from paperintel.schemas.enums import ResourceTier, TaskState
    from paperintel.triage.service import latest_triage

    client = api_env["client"]
    batch = client.post("/v1/ui/import-batches", json={"requested_tier": tier}).json()["data"]["batch_id"]
    # Same shape as UploadQueue, including an ordinary real filename.
    key = f"{batch}:01_Roofline_en.pdf:2430563:1789749581454"
    assert len(f"{batch}:{key}") > 96
    data = build_f01_native() + f"\n% tier={tier}\n".encode()
    route = f"/v1/ui/import-batches/{batch}/files"
    headers = {"Idempotency-Key": key}
    files = {"file": ("01_Roofline_en.pdf", data, "application/pdf")}
    first = client.post(route, files=files, headers=headers)
    assert first.status_code == 200, first.text
    item = first.json()["data"]
    assert item["state"] == "IMPORTED"
    replay = client.post(route, files=files, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["item_id"] == item["item_id"]
    assert replay.json()["data"]["replayed"] is True
    with api_env["state"].session_factory() as session:
        job = session.get(JobRow, item["job_id"])
        assert job.requested_tier == ResourceTier(tier)
        assert job.effective_tier == ResourceTier(tier)
        assert latest_triage(session, item["paper_id"]).effective_tier == ResourceTier(tier)
        tasks = session.scalars(select(TaskRow).where(TaskRow.job_id == job.job_id)).all()
        queued = {task.task_type for task in tasks if task.state is TaskState.QUEUED}
        if tier == "T0_INDEX":
            assert "agents.run_suite" not in queued
        if tier in {"T0_INDEX", "T1_SCAN"}:
            assert "verification.run" not in queued


def test_invalid_upload_tier_is_rejected_before_creating_batch(api_env):
    response = api_env["client"].post("/v1/ui/import-batches", json={"requested_tier": "bogus"})
    assert response.status_code == 400


def test_long_section_heading_is_not_misused_as_a_printed_page_label(api_env, import_pdf_into):
    from paperintel.database.models import EvidenceRow, SectionRow, UiEvidenceLocatorRow

    imported = import_pdf_into()
    with api_env["state"].session_factory() as session:
        section = session.scalar(select(SectionRow).where(
            SectionRow.paper_version_id == imported.paper_version_id))
        section.original_heading = "A genuine long scientific section title " * 5
        evidence = session.scalar(select(EvidenceRow).where(
            EvidenceRow.paper_version_id == imported.paper_version_id))
        evidence.section_id = section.section_id
        page = evidence.page_start
        session.commit()
    response = api_env["client"].get(
        f"/v1/ui/paper-versions/{imported.paper_version_id}/pages/{page}/evidence")
    assert response.status_code == 200, response.text
    locators = response.json()["data"]["evidence"]
    assert locators
    assert all(row["page_label"] is None for row in locators)
    with api_env["state"].session_factory() as session:
        assert session.scalar(select(UiEvidenceLocatorRow).where(
            UiEvidenceLocatorRow.paper_version_id == imported.paper_version_id)) is not None


def test_browser_claim_evidence_contains_full_original_not_only_audit_excerpt(api_env, fresh_paper, add_claim):
    import hashlib

    from paperintel.database.models import ClaimEvidenceRow, EvidenceRow
    from paperintel.ids import new_evidence_id
    from paperintel.schemas.enums import EvidenceRole

    imported = fresh_paper()
    claim_id = add_claim(imported.paper_version_id)
    with api_env["state"].session_factory() as session:
        source = session.scalar(select(EvidenceRow).where(
            EvidenceRow.paper_version_id == imported.paper_version_id,
            EvidenceRow.text.is_not(None)))
        original = "Original evidence, never silently shortened. " * 15
        source = EvidenceRow(
            evidence_id=new_evidence_id(), paper_version_id=source.paper_version_id,
            evidence_type=source.evidence_type, page_start=source.page_start,
            page_end=source.page_end, source_method=source.source_method,
            text=original, content_sha256=hashlib.sha256(original.encode()).hexdigest(),
            extraction_run_id=source.extraction_run_id,
        )
        session.add(source)
        session.flush()
        session.add(ClaimEvidenceRow(claim_id=claim_id, evidence_id=source.evidence_id,
                                     role=EvidenceRole.SUPPORT))
        session.commit()
    response = api_env["client"].get(f"/v1/ui/claims/{claim_id}/evidence")
    assert response.status_code == 200, response.text
    evidence = response.json()["data"]["evidence"][0]
    assert evidence["text"] == original
    assert evidence["paper_version_id"] == imported.paper_version_id
    assert len(evidence["text_excerpt"]) == 300


def test_operation_long_idempotency_key_and_payload_conflict(api_env):
    from paperintel.errors import DomainError
    from paperintel.services.ui import operations_service

    with api_env["state"].session_factory() as session:
        arguments = dict(kind="set_tier", payload={"paper_ids": [], "tier": "T2_FULL"},
                         settings=api_env["state"].settings, idempotency_key="batch:" + "pver_123," * 100)
        first = operations_service.submit_operation(session, **arguments)
        session.commit()
        assert operations_service.submit_operation(session, **arguments) == first
        arguments["payload"] = {"paper_ids": [], "tier": "T3_DEEP"}
        with pytest.raises(DomainError, match="different request"):
            operations_service.submit_operation(session, **arguments)


def test_session_reload_restores_csrf_and_logout_requires_it(browser):
    client = browser["client"]
    restored = client.get("/v1/ui/session")
    assert restored.status_code == 200
    assert restored.headers["cache-control"] == "no-store"
    csrf = restored.json()["data"]["csrf_token"]
    assert csrf == browser["session"]["csrf_token"]
    assert client.patch("/v1/ui/preferences", json={"theme": "DARK"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.delete("/v1/ui/session").status_code == 403
    assert client.delete("/v1/ui/session", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/v1/ui/session").status_code == 401


@pytest.mark.parametrize("path", ["/v1/ui/jobs", "/v1/ui/collections", "/v1/ui/system/status"])
def test_browser_read_adapters_require_and_accept_cookie(token_env, path):
    client = token_env["client"]
    assert client.get(path).status_code == 401
    assert client.post("/v1/ui/session", json={"token": client.app_token}).status_code == 200
    assert client.get(path).status_code == 200
    # The core automation surface remains Bearer-only.
    assert client.get(path.replace("/ui", "", 1)).status_code == 401


def test_tag_preview_then_merge_is_durable_and_idempotent(api_env, import_pdf_into):
    imported = import_pdf_into()
    with api_env["state"].session_factory() as session:
        source = resolve_tag(session, namespace="method", name="Source method")
        target = resolve_tag(session, namespace="method", name="Target method")
        session.add(PaperTagRow(paper_id=imported.paper_id, tag_id=source.tag_id, is_candidate=True))
        session.commit()
    payload = {"action": "merge", "tag_ids": [source.tag_id], "target_tag_id": target.tag_id}
    client = api_env["client"]
    preview = client.post("/v1/ui/tags/actions", json=payload)
    assert preview.status_code == 202
    assert preview.json()["data"]["state"] == "PREVIEW"
    with api_env["state"].session_factory() as session:
        assert session.get(PaperTagRow, (imported.paper_id, source.tag_id)) is not None
        assert session.get(PaperTagRow, (imported.paper_id, target.tag_id)) is None
    payload.update(preview_only=False, idempotency_key="merge-regression")
    result = client.post("/v1/ui/tags/actions", json=payload)
    assert result.status_code == 202, result.text
    assert result.json()["data"]["state"] == "COMPLETED"
    repeat = client.post("/v1/ui/tags/actions", json=payload)
    assert repeat.json()["data"] == result.json()["data"]
    with api_env["state"].session_factory() as session:
        assert session.get(PaperTagRow, (imported.paper_id, source.tag_id)) is None
        assert session.get(PaperTagRow, (imported.paper_id, target.tag_id)).is_candidate is False
        assert resolve_tag(session, namespace="method", name="Source method").tag_id == target.tag_id
        assert session.scalar(select(func.count()).select_from(TagAliasRow).where(TagAliasRow.tag_id == target.tag_id)) == 1
