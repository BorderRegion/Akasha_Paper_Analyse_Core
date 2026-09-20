"""UX-016 — authenticated originals: Range/206/416, ETag and hash agreement.

Requirement (docs/06 §原文资源 + docs/07):
- GET /v1/ui/paper-versions/{id}/document is authenticated, supports Range/206,
  ETag, Content-Length and Accept-Ranges, and can never be pointed at an
  arbitrary disk path;
- the served bytes, the version record and the evidence locators must agree on
  the SAME document hash: if the hash/version does not match, the reader must
  refuse to draw boxes instead of placing them on the wrong page;
- a locator without usable geometry degrades to PAGE/TEXT_ONLY with a reason —
  never a fabricated rectangle.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.fixtures.generators import build_f03_mixed


def _document_url(version_id: str) -> str:
    return f"/v1/ui/paper-versions/{version_id}/document"


def _version_hash(token_env, version_id: str) -> str:
    """The hash recorded on the version row — the authoritative document id."""
    from paperintel.database.models import PaperVersionRow

    session = token_env["state"].session_factory()
    try:
        version = session.get(PaperVersionRow, version_id)
        assert version is not None
        return version.content_sha256
    finally:
        session.close()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_document_requires_authentication(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]

    anonymous = client.get(_document_url(imported.paper_version_id))
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error"]["code"] == "AUTH_001"

    authorised = client.get(
        _document_url(imported.paper_version_id),
        headers={"Authorization": f"Bearer {client.app_token}"},
    )
    assert authorised.status_code == 200
    assert authorised.headers["Content-Type"] == "application/pdf"
    assert authorised.headers["X-Content-Type-Options"] == "nosniff"
    assert authorised.headers["Accept-Ranges"] == "bytes"
    assert int(authorised.headers["Content-Length"]) == len(authorised.content)
    assert authorised.headers["Content-Disposition"].startswith("inline")


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_served_bytes_match_the_version_hash_and_the_etag(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}

    response = client.get(_document_url(imported.paper_version_id), headers=headers)
    served_hash = hashlib.sha256(response.content).hexdigest()
    assert served_hash == _version_hash(token_env, imported.paper_version_id), (
        "the served PDF is not the version's document"
    )
    assert response.headers["ETag"].strip('"') == served_hash


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_range_requests_return_exactly_the_requested_bytes(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}
    url = _document_url(imported.paper_version_id)

    whole = client.get(url, headers=headers)
    total = len(whole.content)

    first = client.get(url, headers={**headers, "Range": "bytes=0-99"})
    assert first.status_code == 206, first.text
    assert first.headers["Content-Range"] == f"bytes 0-99/{total}"
    assert len(first.content) == 100
    assert first.content.startswith(b"%PDF-")

    tail = client.get(url, headers={**headers, "Range": "bytes=100-"})
    assert tail.status_code == 206
    assert tail.headers["Content-Range"] == f"bytes 100-{total - 1}/{total}"
    assert first.content + tail.content == whole.content, (
        "a ranged read must reassemble into the exact document"
    )

    suffix = client.get(url, headers={**headers, "Range": "bytes=-16"})
    assert suffix.status_code == 206
    assert suffix.content == whole.content[-16:]


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_unsatisfiable_and_malformed_ranges_are_refused(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}
    url = _document_url(imported.paper_version_id)
    total = len(client.get(url, headers=headers).content)

    beyond = client.get(url, headers={**headers, "Range": f"bytes={total + 10}-"})
    assert beyond.status_code == 416, beyond.text
    assert beyond.headers["Content-Range"] == f"bytes */{total}"

    malformed = client.get(url, headers={**headers, "Range": "bytes=abc-def"})
    assert malformed.status_code == 416

    # A 304 keeps the client from re-downloading unchanged bytes.
    etag = client.get(url, headers=headers).headers["ETag"]
    unchanged = client.get(url, headers={**headers, "If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_document_reads_are_addressed_by_id_never_by_path(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}

    unknown = client.get(_document_url("pver_does_not_exist"), headers=headers)
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] in {"STORAGE_003", "CFG_002"}

    # A traversal-shaped identifier is not a path: it is simply unknown.
    traversal = client.get(_document_url("../../etc/passwd"), headers=headers)
    assert traversal.status_code in {404, 400}, traversal.text
    assert b"root:" not in traversal.content

    # The asset route also reads by DB id and never exposes a storage key.
    body = client.get(
        f"/v1/ui/paper-versions/{import_pdf_token_env().paper_version_id}/document", headers=headers
    )
    assert "storage_key" not in body.headers
    assert b"/objects/" not in body.content[:64]


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_page_evidence_is_version_scoped_and_hash_consistent(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}

    document = client.get(_document_url(imported.paper_version_id), headers=headers)
    served_hash = hashlib.sha256(document.content).hexdigest()

    # Import a SECOND document of the same kind: its locators must never leak
    # into this version's page.
    import_pdf_token_env(builder=build_f03_mixed, name="other.pdf")

    response = client.get(
        f"/v1/ui/paper-versions/{imported.paper_version_id}/pages/1/evidence", headers=headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["paper_version_id"] == imported.paper_version_id
    assert (
        data["document_sha256"]
        == _version_hash(token_env, imported.paper_version_id)
        == served_hash
    )
    assert data["page_number"] == 1
    assert data["page_display_width"] > 0 and data["page_display_height"] > 0

    for locator in data["evidence"]:
        assert locator["paper_version_id"] == imported.paper_version_id, (
            "a locator from another version would draw boxes on the wrong document"
        )
        assert locator["document_sha256"] == served_hash
        assert locator["coordinate_space"] == "DISPLAY_NORMALIZED_V1"
        assert locator["page_number"] == 1
        if locator["precision"] == "REGION":
            assert locator["rect_norm"] is not None
            assert len(locator["rect_norm"]) == 4
            assert all(0.0 <= value <= 1.0 for value in locator["rect_norm"])
        else:
            assert locator["rect_norm"] is None, "no fabricated boxes without geometry"
            assert locator["reason"], "a degraded locator must explain itself"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_page_without_evidence_returns_an_empty_list(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}

    response = client.get(
        f"/v1/ui/paper-versions/{imported.paper_version_id}/pages/99/evidence", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["evidence"] == []


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-016"], ids=["UX-016"])
def test_asset_route_serves_by_db_id_with_a_typed_content_type(
    requirement: str, token_env, import_pdf_token_env
) -> None:
    assert requirement == "UX-016", "test/requirement mapping drift"
    from sqlalchemy import select

    from paperintel.database.models import PaperVersionRow

    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {"Authorization": f"Bearer {client.app_token}"}

    session = token_env["state"].session_factory()
    try:
        version = session.get(PaperVersionRow, imported.paper_version_id)
        asset_id = version.asset_id
    finally:
        session.close()
    assert asset_id

    response = client.get(f"/v1/ui/assets/{asset_id}/content", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"] == "application/pdf"
    assert hashlib.sha256(response.content).hexdigest() == _version_hash(
        token_env, imported.paper_version_id
    )

    assert client.get("/v1/ui/assets/ast_missing/content", headers=headers).status_code == 404
    assert session is not None and select is not None
