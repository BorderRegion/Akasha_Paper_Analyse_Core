"""Backend support for the review workbench and agent handoff (F05 surface).

These are API-level tests for the endpoints the F05 pages call: the review queue
grouping (derived from REAL verification outcomes), the human decision guard, and
the handoff bundle. The UX-033..UX-037 acceptance tests live with the frontend
pages; here we prove the server side they depend on.
"""

from __future__ import annotations

import pytest
from tests.fixtures.generators import build_f03_mixed


def _seed_claim(api_env, version_id: str, *, statement: str, state):
    from paperintel.database.models import AnalysisRunRow, ClaimRow, PaperVersionRow
    from paperintel.ids import new_claim_id, new_run_id
    from paperintel.schemas.common import utcnow
    from paperintel.schemas.enums import ClaimType, TaskState

    session = api_env["state"].session_factory()
    try:
        version = session.get(PaperVersionRow, version_id)
        run = AnalysisRunRow(
            run_id=new_run_id(),
            paper_id=version.paper_id,
            paper_version_id=version_id,
            agent_type="agents.analyst",
            pipeline_version="1.0.0",
            config_hash="a" * 64,
            model_id="mock-analyst",
            provider_id="prv_mockllm",
            status=TaskState.SUCCEEDED,
            started_at=utcnow(),
            finished_at=utcnow(),
        )
        session.add(run)
        session.flush()
        claim = ClaimRow(
            claim_id=new_claim_id(),
            paper_id=version.paper_id,
            paper_version_id=version_id,
            claim_type=ClaimType.FACT,
            category="result.main",
            statement=statement,
            support_state=state,
            created_by_run_id=run.run_id,
            pipeline_version="1.0.0",
        )
        session.add(claim)
        session.commit()
        return claim.claim_id
    finally:
        session.close()


@pytest.mark.needs_db
def test_review_queue_is_scoped_and_grouped_by_real_outcomes(api_env, fresh_paper) -> None:
    from paperintel.schemas.enums import SupportState

    imported = fresh_paper(builder=build_f03_mixed)
    disputed = _seed_claim(
        api_env, imported.paper_version_id, statement="mAP 41.3 → 43.4", state=SupportState.DISPUTED
    )
    supported = _seed_claim(
        api_env, imported.paper_version_id, statement="clean claim", state=SupportState.SUPPORTED
    )

    client = api_env["client"]
    payload = client.post(
        "/v1/ui/review/query", json={"paper_version_id": imported.paper_version_id}
    ).json()["data"]
    ids = [item["claim"]["claim_id"] for item in payload["items"]]
    assert disputed in ids
    assert supported not in ids, "the default queue holds claims that need a look"

    item = next(entry for entry in payload["items"] if entry["claim"]["claim_id"] == disputed)
    # No verification rows exist for this claim: that is NOT_VERIFIED, which is a
    # different state from "verified and clean".
    assert item["group"] == "NOT_VERIFIED"
    assert item["claim_revision"]
    assert item["paper_version_id"] == imported.paper_version_id

    # "all" includes the clean claim WITH its real state, so "0 risky" and
    # "the audit returned nothing" stay distinguishable.
    everything = client.post(
        "/v1/ui/review/query",
        json={"paper_version_id": imported.paper_version_id, "include_all": True},
    ).json()["data"]
    assert everything["total"] == 2


@pytest.mark.needs_db
def test_review_decision_never_touches_support_state_and_guards_the_revision(
    api_env, fresh_paper
) -> None:
    from paperintel.schemas.enums import SupportState

    imported = fresh_paper(builder=build_f03_mixed)
    claim_id = _seed_claim(
        api_env, imported.paper_version_id, statement="disputed line", state=SupportState.DISPUTED
    )
    client = api_env["client"]

    item = client.post(
        "/v1/ui/review/query", json={"paper_version_id": imported.paper_version_id}
    ).json()["data"]["items"][0]
    revision = item["claim_revision"]

    wrong = client.post(
        "/v1/ui/review/decisions",
        json={"claim_id": claim_id, "decision": "SEEN", "expected_claim_revision": "stale-token"},
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["error"]["code"] == "REVISION_CONFLICT"

    ok = client.post(
        "/v1/ui/review/decisions",
        json={
            "claim_id": claim_id,
            "decision": "SEEN",
            "expected_claim_revision": revision,
            "idempotency_key": "seen-once",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["decision"] == "SEEN"

    repeat = client.post(
        "/v1/ui/review/decisions",
        json={"claim_id": claim_id, "decision": "NEEDS_REVIEW", "idempotency_key": "seen-once"},
    )
    assert repeat.json()["data"]["created"] is False, "a repeated key must not create a second row"

    session = api_env["state"].session_factory()
    try:
        from paperintel.database.models import ClaimRow

        claim = session.get(ClaimRow, claim_id)
        assert claim.support_state is SupportState.DISPUTED, (
            "a human decision must never rewrite the scientific support state"
        )
    finally:
        session.close()

    # Seen items leave the default queue but are still reachable.
    after = client.post(
        "/v1/ui/review/query", json={"paper_version_id": imported.paper_version_id}
    ).json()["data"]
    assert after["items"] == []
    with_seen = client.post(
        "/v1/ui/review/query",
        json={"paper_version_id": imported.paper_version_id, "include_seen": True},
    ).json()["data"]
    assert [entry["claim"]["claim_id"] for entry in with_seen["items"]] == [claim_id]
    assert with_seen["items"][0]["personal_decision"] == "SEEN"


@pytest.mark.needs_db
def test_handoff_bundle_carries_manifest_hashes_and_respects_limits(api_env, fresh_paper) -> None:
    imported = fresh_paper(builder=build_f03_mixed)
    client = api_env["client"]

    response = client.post(
        "/v1/ui/handoffs",
        json={
            "paper_version_ids": [imported.paper_version_id],
            "include": ["brief", "claims", "audit"],
            "max_bytes": 500000,
        },
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    manifest = data["manifest"]
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["generated_at"]
    entry = manifest["papers"][0]
    assert entry["paper_version_id"] == imported.paper_version_id
    session = api_env["state"].session_factory()
    try:
        from paperintel.database.models import PaperVersionRow

        document_sha256 = session.get(PaperVersionRow, imported.paper_version_id).content_sha256
    finally:
        session.close()
    assert entry["source_hashes"]["document_sha256"] == document_sha256
    assert "pdf" not in entry, "PDF binaries are excluded unless explicitly requested"
    assert data["size_bytes"] > 0

    # An explicit scope is required: "the whole library" is not an option.
    assert client.post("/v1/ui/handoffs", json={"include": ["brief"]}).status_code == 400
    # Unknown include entries are rejected, not ignored.
    assert (
        client.post(
            "/v1/ui/handoffs",
            json={"paper_version_ids": [imported.paper_version_id], "include": ["everything"]},
        ).status_code
        == 400
    )
    # A bundle that cannot fit is refused with the real numbers.
    too_small = client.post(
        "/v1/ui/handoffs",
        json={
            "paper_version_ids": [imported.paper_version_id],
            "include": ["claims"],
            "max_bytes": 10,
        },
    )
    assert too_small.status_code == 413, too_small.text
    assert too_small.json()["error"]["details"]["max_bytes"] == 10


@pytest.mark.needs_db
def test_reverify_operation_is_accepted_not_reported_as_complete(api_env, fresh_paper) -> None:
    from paperintel.schemas.enums import SupportState

    imported = fresh_paper(builder=build_f03_mixed)
    claim_id = _seed_claim(
        api_env, imported.paper_version_id, statement="recheck me", state=SupportState.DISPUTED
    )
    unselected_id = _seed_claim(
        api_env, imported.paper_version_id, statement="leave me alone", state=SupportState.DISPUTED
    )
    client = api_env["client"]

    response = client.post(
        "/v1/ui/operations",
        json={
            "kind": "reverify",
            "payload": {"claim_ids": [claim_id]},
            "idempotency_key": "reverify-once",
        },
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["state"] == "ACCEPTED", "accepting the work is not completing it"
    assert data["job_ids"], "the accepted work must be traceable to a job"

    repeat = client.post(
        "/v1/ui/operations",
        json={
            "kind": "reverify",
            "payload": {"claim_ids": [claim_id]},
            "idempotency_key": "reverify-once",
        },
    )
    assert repeat.status_code == 202
    assert repeat.json()["data"]["operation_id"] == data["operation_id"], (
        "the same idempotency key must not create a second operation"
    )

    # Accepting the work must NOT have changed the scientific state yet: the
    # support state only moves when the queued job actually completes.
    session = api_env["state"].session_factory()
    try:
        from paperintel.database.models import ClaimRow

        claim = session.get(ClaimRow, claim_id)
        assert claim.support_state is SupportState.DISPUTED, (
            "an accepted re-verification must not rewrite support_state"
        )
        from sqlalchemy import select

        from paperintel.database.models import TaskRow, VerificationRow
        from paperintel.workflow.handlers import handle_verify_claims

        task = session.scalar(select(TaskRow).where(TaskRow.job_id == data["job_ids"][0]))
        assert task.input_manifest["claim_ids"] == [claim_id]
        report = handle_verify_claims(session, task)
        assert report["claims_verified"] == 1
        assert session.get(ClaimRow, unselected_id).support_state is SupportState.DISPUTED
        assert not session.scalars(select(VerificationRow).where(
            VerificationRow.claim_id == unselected_id)).all()
        from paperintel.schemas.enums import ResourceTier
        from paperintel.verification.service import run_verification

        assert run_verification(session, paper_version_id=imported.paper_version_id,
                                tier=ResourceTier.T3_DEEP, claim_ids=[]).claims_verified == 0
    finally:
        session.close()
