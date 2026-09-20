"""Real cookie/CSRF collection mutations used by the personal workbench."""


def test_collection_membership_preserves_paper_and_requires_csrf(browser, import_pdf_token_env):
    paper = import_pdf_token_env()
    client = browser["client"]
    assert client.post("/v1/ui/collections", json={"name": "missing csrf"}).status_code == 403
    response = client.post("/v1/ui/collections", json={"name": "personal collection"},
                           headers=browser["csrf_headers"])
    assert response.status_code == 200, response.text
    collection_id = response.json()["data"]["collection_id"]
    membership = f"/v1/ui/collections/{collection_id}/papers/{paper.paper_id}"
    for _ in range(2):
        added = client.post(membership, headers=browser["csrf_headers"])
        assert added.status_code == 200, added.text
    workspace = f"/v1/ui/collections/{collection_id}/workspace"
    assert client.get(workspace).json()["data"]["selected_paper_count"] == 1
    assert client.delete(membership, headers=browser["csrf_headers"]).json()["data"]["removed"]
    assert client.get(workspace).json()["data"]["selected_paper_count"] == 0
    assert client.get(f"/v1/ui/papers/{paper.paper_id}/workspace").status_code == 200
