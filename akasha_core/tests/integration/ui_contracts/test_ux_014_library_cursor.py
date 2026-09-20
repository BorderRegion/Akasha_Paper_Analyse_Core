"""UX-014 — pagination cursors are bound to the filter, the scope and the sort.

Requirement (docs/06 §通用响应): the cursor is an opaque value bound to
filter_hash / scope_revision / sort; page size defaults to 50 with a hard cap of
100; a stale cursor returns **409 + RESET_CURSOR** and the client keeps its
filters and restarts from the first page — it must never silently receive page 1
as if it were page 2, and must never see a duplicate or a dropped row because
the scope moved underneath it.

Every test scopes its query by a collection it created itself, so the assertions
are about THIS test's corpus (the integration database is module-scoped).
"""

from __future__ import annotations

import pytest
from tests.fixtures.generators import build_f03_mixed


def _query(client, collection_id: str, **extra) -> dict:
    payload = {"limit": 50, "filters": {"collection_ids": [collection_id]}, **extra}
    response = client.post("/v1/ui/library/query", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_cursor_walks_the_whole_scope_without_gaps_or_repeats(
    requirement: str, api_env, scoped_library
) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    scope = scoped_library(documents=3)
    client = api_env["client"]
    ids = [result.paper_id for result in scope["imported"]]

    first = _query(client, scope["collection_id"], limit=2, sort="TITLE")
    assert first["kind"] == "PAPERS"
    assert first["total"] == 3
    assert first["total_kind"] == "EXACT"
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert first["next_cursor"]

    second = _query(
        client, scope["collection_id"], limit=2, sort="TITLE", cursor=first["next_cursor"]
    )
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert second["total"] == 3

    seen = [item["paper_id"] for item in first["items"]] + [
        item["paper_id"] for item in second["items"]
    ]
    assert sorted(seen) == sorted(ids), "pagination dropped or duplicated a paper"
    assert len(seen) == len(set(seen))


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_cursor_from_another_filter_set_is_refused(
    requirement: str, api_env, scoped_library
) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    scope = scoped_library(documents=3)
    client = api_env["client"]
    cid = scope["collection_id"]

    page = _query(client, cid, limit=1)
    cursor = page["next_cursor"]
    assert cursor

    # Same cursor, DIFFERENT filters: the filter hash no longer matches.
    shifted = client.post(
        "/v1/ui/library/query",
        json={
            "limit": 1,
            "cursor": cursor,
            "filters": {"collection_ids": [cid], "read_states": ["READ"]},
        },
    )
    assert shifted.status_code == 409, shifted.text
    error = shifted.json()["error"]
    assert error["code"] == "RESET_CURSOR"
    assert error["retryable"] is False
    assert "filter_hash" in error["details"], "the client needs the current scope to restart"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_cursor_from_another_sort_order_is_refused(
    requirement: str, api_env, scoped_library
) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    scope = scoped_library(documents=3)
    client = api_env["client"]
    cid = scope["collection_id"]

    page = _query(client, cid, limit=1, sort="RECENT")
    moved = client.post(
        "/v1/ui/library/query",
        json={
            "limit": 1,
            "sort": "TITLE",
            "cursor": page["next_cursor"],
            "filters": {"collection_ids": [cid]},
        },
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "RESET_CURSOR"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_cursor_is_refused_when_the_scope_changed_underneath(
    requirement: str, api_env, scoped_library, import_pdf_into
) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    """A new import changes scope_revision: the old cursor is no longer valid."""
    scope = scoped_library(documents=2)
    client = api_env["client"]
    cid = scope["collection_id"]

    page = _query(client, cid, limit=1)
    scope_before = page["scope_revision"]
    assert page["total"] == 2
    assert page["next_cursor"], "a second page must exist before a cursor is meaningful"

    import_pdf_into(builder=build_f03_mixed, name="after.pdf")

    stale = client.post(
        "/v1/ui/library/query",
        json={"limit": 1, "cursor": page["next_cursor"], "filters": {"collection_ids": [cid]}},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "RESET_CURSOR"
    assert stale.json()["error"]["details"]["scope_revision"] != scope_before

    # Restarting from the first page with the SAME filters works; the collection
    # scope is unchanged (2 papers) even though the corpus grew.
    restart = _query(client, cid, limit=10)
    assert restart["total"] == 2


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_malformed_cursor_is_a_rejected_request_not_a_silent_page_one(
    requirement: str, api_env
) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    response = api_env["client"].post("/v1/ui/library/query", json={"cursor": "@@@not-a-cursor@@@"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "CFG_002"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_page_size_is_capped_by_the_schema(requirement: str, api_env, scoped_library) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    scope = scoped_library(documents=3)
    client = api_env["client"]

    assert client.post("/v1/ui/library/query", json={"limit": 0}).status_code == 422
    assert client.post("/v1/ui/library/query", json={"limit": 101}).status_code == 422

    default_page = client.post("/v1/ui/library/query", json={}).json()["data"]
    assert len(default_page["items"]) <= 50

    biggest = _query(client, scope["collection_id"], limit=100)
    assert len(biggest["items"]) == 3


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-014"], ids=["UX-014"])
def test_result_kind_separates_claims_from_papers(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-014", "test/requirement mapping drift"
    """A search hit must not masquerade as a paper card (docs/06 §文献库)."""
    imported = import_pdf_into()
    client = api_env["client"]

    papers = client.post(
        "/v1/ui/library/query", json={"kind": "PAPERS", "query": "Native Extraction"}
    ).json()["data"]
    assert papers["kind"] == "PAPERS"
    for item in papers["items"]:
        assert "title" in item and "claim_id" not in item
    assert imported.paper_id in {item["paper_id"] for item in papers["items"]}

    claims = client.post(
        "/v1/ui/library/query", json={"kind": "CLAIMS", "query": "Native Extraction"}
    ).json()["data"]
    assert claims["kind"] == "CLAIMS"
    for item in claims["items"]:
        assert "claim_id" in item and "title" not in item
        assert item["support_state"]
