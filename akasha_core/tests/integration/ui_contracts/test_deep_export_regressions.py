"""Personal-use exports must be downloadable, fresh and explicitly complete."""

from tests.integration.ui_contracts.test_review_handoff_api import _seed_claim

from paperintel.schemas.enums import SupportState


def test_exports_download_exact_scope_and_do_not_overwrite(api_env, fresh_paper):
    paper = fresh_paper()
    client = api_env["client"]
    claim_id = _seed_claim(api_env, paper.paper_version_id, statement="first export",
                           state=SupportState.UNVERIFIED)
    payload = {"kind": "export", "payload": {"paper_version_ids": [paper.paper_version_id]}}
    first = client.post("/v1/ui/operations", json={**payload, "idempotency_key": "export-first"})
    assert first.status_code == 202, first.text
    url = f'/v1/ui/operations/{first.json()["data"]["operation_id"]}/download'
    downloaded = client.get(url)
    assert downloaded.status_code == 200, downloaded.text
    assert "attachment" in downloaded.headers["content-disposition"]
    entry = downloaded.json()["papers"][0]
    assert entry["paper_version_id"] == paper.paper_version_id
    assert entry["claims"][0]["claim_id"] == claim_id
    assert "evidence_refs" in entry and "brief" in entry
    _seed_claim(api_env, paper.paper_version_id, statement="newer export",
                state=SupportState.UNVERIFIED)
    second = client.post("/v1/ui/operations", json={**payload, "idempotency_key": "export-second"})
    newer = client.get(f'/v1/ui/operations/{second.json()["data"]["operation_id"]}/download')
    assert len(newer.json()["papers"][0]["claims"]) == 2
    assert client.get(url).content == downloaded.content


def test_handoff_refuses_to_silently_truncate_claims(api_env, fresh_paper):
    paper = fresh_paper()
    for number in range(2):
        _seed_claim(api_env, paper.paper_version_id, statement=f"claim {number}",
                    state=SupportState.UNVERIFIED)
    response = api_env["client"].post("/v1/ui/handoffs", json={
        "paper_version_ids": [paper.paper_version_id], "include": ["claims"], "limit": 1,
    })
    assert response.status_code == 413, response.text
    assert response.json()["error"]["details"]["claims_at_least"] == 2
