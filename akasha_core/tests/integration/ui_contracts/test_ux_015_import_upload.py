"""UX-015 — browser multipart upload: duplicate / corrupt / oversize / per-item retry.

Requirement (docs/06 §导入):
- one file per request, independent idempotency key per file, at most 2 uploads in
  flight (client-side), 100 MiB per file / 50 files / 500 MiB per batch as the
  product defaults advertised through /v1/ui/capabilities;
- the server counts while receiving, then checks size → free space → magic bytes →
  PDF parse, and rejects with a real error code; Content-Length and the filename
  are never trusted;
- **dedup is by sha256**, not by idempotency key;
- ONE failed item must not affect the others, the failed item keeps its error in
  the batch view, and `/retry` covers only the failed item ids.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.fixtures.generators import build_f01_native, build_f03_mixed, build_f04_rich


def _batch(client, *, headers=None, **payload) -> str:
    response = client.post("/v1/ui/import-batches", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["batch_id"]


def _upload(client, batch_id: str, name: str, data: bytes, **extra):
    return client.post(
        f"/v1/ui/import-batches/{batch_id}/files",
        files={"file": (name, data, "application/pdf")},
        **extra,
    )


def _batch_view(client, batch_id: str, *, headers=None) -> dict:
    response = client.get(f"/v1/ui/import-batches/{batch_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_capabilities_advertise_the_upload_limits(requirement: str, token_env, bearer) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    limits = token_env["client"].get("/v1/ui/capabilities", headers=bearer).json()["data"]["limits"]
    assert limits["upload_file_bytes"] == 100 * 1024 * 1024
    assert limits["upload_files"] == 50
    assert limits["upload_batch_bytes"] == 500 * 1024 * 1024
    assert limits["library_page_size"] == 50


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_upload_imports_a_real_version_and_plans_a_job(requirement: str, api_env) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = api_env["client"]
    batch_id = _batch(client, requested_tier="T2_FULL")
    payload = build_f01_native()

    response = _upload(client, batch_id, "paper.pdf", payload)
    assert response.status_code == 200, response.text
    item = response.json()["data"]
    assert item["state"] == "IMPORTED", item
    assert item["sha256"] == hashlib.sha256(payload).hexdigest()
    assert item["size_bytes"] == len(payload)
    assert item["paper_id"].startswith("pap_")
    assert item["paper_version_id"].startswith("pver_")
    assert item["job_id"].startswith("job_")

    # The upload really reached the corpus: the version is addressable and the
    # document is byte-identical to what the browser sent.
    workspace = client.get(f"/v1/ui/papers/{item['paper_id']}/workspace")
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["data"]["selected_version_id"] == item["paper_version_id"]
    document = client.get(f"/v1/ui/paper-versions/{item['paper_version_id']}/document")
    assert document.status_code == 200
    assert hashlib.sha256(document.content).hexdigest() == item["sha256"]


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_repeating_the_same_content_is_a_duplicate_not_a_second_version(
    requirement: str, api_env
) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = api_env["client"]
    batch_id = _batch(client)
    payload = build_f04_rich()

    first = _upload(client, batch_id, "original.pdf", payload).json()["data"]
    second = _upload(client, batch_id, "renamed-copy.pdf", payload).json()["data"]

    assert first["state"] == "IMPORTED"
    assert second["state"] == "DUPLICATE", (
        "dedup must be decided by content hash, not by filename or idempotency key"
    )
    assert second["sha256"] == first["sha256"]
    assert second["paper_version_id"] == first["paper_version_id"]

    versions = client.get(f"/v1/ui/papers/{first['paper_id']}/versions").json()["data"]["items"]
    assert len(versions) == 1, "a duplicate upload must not create a second version"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_the_same_idempotency_key_reuses_the_item_slot(requirement: str, api_env) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = api_env["client"]
    batch_id = _batch(client)
    payload = build_f03_mixed()

    first = _upload(client, batch_id, "one.pdf", payload, params={"idempotency_key": "slot-1"})
    assert first.json()["data"]["created"] is True
    repeat = _upload(client, batch_id, "one.pdf", payload, params={"idempotency_key": "slot-1"})
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["data"]["created"] is False
    assert repeat.json()["data"]["replayed"] is True, (
        "replaying a finished upload must be idempotent success, not an error"
    )
    assert repeat.json()["data"]["item_id"] == first.json()["data"]["item_id"]

    items = _batch_view(client, batch_id)["items"]
    assert len(items) == 1, "a repeated idempotency key must not create a second item"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_a_corrupt_file_fails_alone_and_keeps_its_error(requirement: str, api_env) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = api_env["client"]
    batch_id = _batch(client)

    good = _upload(client, batch_id, "good.pdf", build_f01_native())
    broken = _upload(client, batch_id, "broken.pdf", b"NOT A PDF AT ALL")
    another = _upload(client, batch_id, "another.pdf", build_f04_rich())

    assert good.json()["data"]["state"] == "IMPORTED"
    assert another.json()["data"]["state"] == "IMPORTED"
    assert broken.status_code == 400, broken.text
    error = broken.json()["error"]
    assert error["code"] == "PDF_001"
    assert "item_id" in error["details"], "the client must know WHICH file failed"

    items = {item["filename"]: item for item in _batch_view(client, batch_id)["items"]}
    assert items["broken.pdf"]["state"] == "FAILED"
    assert items["broken.pdf"]["error_code"] == "PDF_001"
    assert items["good.pdf"]["state"] == "IMPORTED"
    assert items["another.pdf"]["state"] == "IMPORTED"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_oversize_upload_is_refused_while_receiving(requirement: str, api_env, monkeypatch) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    # The limit is configurable and may be smaller than the default (docs/06):
    # the server must enforce exactly what it advertises.
    _set_limits(monkeypatch, api_env, upload_file_bytes=1024)
    client = api_env["client"]

    advertised = client.get("/v1/ui/capabilities").json()["data"]["limits"]
    assert advertised["upload_file_bytes"] == 1024, (
        "capabilities must advertise the deployment's real limit"
    )

    batch_id = _batch(client)
    payload = build_f03_mixed()
    assert len(payload) > 1024

    refused = _upload(client, batch_id, "too-big.pdf", payload)
    assert refused.status_code == 413, refused.text
    assert refused.json()["error"]["code"] == "STORAGE_001"
    assert refused.json()["error"]["details"]["limit"] == 1024

    items = _batch_view(client, batch_id)["items"]
    assert items[0]["state"] == "FAILED"
    # Nothing was persisted: no paper was created for the refused file.
    assert items[0]["paper_id"] is None


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_file_count_and_batch_bytes_limits_are_enforced(
    requirement: str, api_env, monkeypatch
) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    _set_limits(monkeypatch, api_env, upload_files=1)
    client = api_env["client"]
    batch_id = _batch(client)
    assert _upload(client, batch_id, "first.pdf", build_f01_native()).status_code == 200
    second = _upload(client, batch_id, "second.pdf", build_f04_rich())
    assert second.status_code == 400, second.text
    assert second.json()["error"]["code"] == "CFG_002"
    assert second.json()["error"]["details"]["limit"] == 1

    _set_limits(monkeypatch, api_env, upload_batch_bytes=8)
    other_batch = _batch(client)
    refused = _upload(client, other_batch, "batch-limit.pdf", build_f01_native())
    assert refused.status_code == 413, refused.text
    assert refused.json()["error"]["code"] == "STORAGE_001"
    assert refused.json()["error"]["details"]["limit"] == 8


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_retry_covers_only_the_failed_items(requirement: str, api_env) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = api_env["client"]
    batch_id = _batch(client)

    imported = _upload(client, batch_id, "ok.pdf", build_f01_native()).json()["data"]
    failed_response = _upload(client, batch_id, "bad.pdf", b"still not a PDF")
    assert failed_response.status_code == 400
    failed_id = failed_response.json()["error"]["details"]["item_id"]

    retried = client.post(f"/v1/ui/import-batches/{batch_id}/retry", json={"item_ids": [failed_id]})
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["retried"] == [failed_id]

    items = {item["item_id"]: item for item in _batch_view(client, batch_id)["items"]}
    # The failed file goes back to PENDING (its bytes were never kept); the
    # successfully imported one is untouched.
    assert items[failed_id]["state"] == "PENDING"
    assert items[failed_id]["error_code"] is None
    assert items[imported["item_id"]]["state"] == "IMPORTED"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_cancel_refuses_an_item_that_already_became_a_paper(requirement: str, api_env) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = api_env["client"]
    batch_id = _batch(client)
    imported = _upload(client, batch_id, "keep.pdf", build_f01_native()).json()["data"]

    refused = client.delete(f"/v1/ui/import-batches/{batch_id}/items/{imported['item_id']}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "CFG_002"
    assert refused.json()["error"]["details"]["state"] == "IMPORTED"
    assert "IMPORTED" in refused.json()["error"]["message"]

    # A pending item can be cancelled.
    pending_id = _upload(
        client, batch_id, "drop.pdf", b"not a pdf", params={"idempotency_key": "drop"}
    ).json()["error"]["details"]["item_id"]
    cancelled = client.delete(f"/v1/ui/import-batches/{batch_id}/items/{pending_id}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["state"] == "CANCELLED"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-015"], ids=["UX-015"])
def test_upload_requires_authentication_and_csrf(requirement: str, token_env, bearer) -> None:
    assert requirement == "UX-015", "test/requirement mapping drift"
    client = token_env["client"]

    assert client.post("/v1/ui/import-batches", json={}).status_code == 401
    assert client.post("/v1/ui/import-batches", json={}, headers=bearer).status_code == 200

    batch_id = _batch(client, headers=bearer)
    # No credential at all.
    assert _upload(client, batch_id, "anonymous.pdf", build_f01_native()).status_code == 401

    # A cookie session without the CSRF header is authenticated but NOT allowed
    # to write.
    assert (
        client.post("/v1/ui/session", json={"token": token_env["client"].app_token}).status_code
        == 200
    )
    without_csrf = _upload(client, batch_id, "no-csrf.pdf", build_f01_native())
    assert without_csrf.status_code == 403, without_csrf.text
    assert without_csrf.json()["error"]["code"] == "AUTH_001"

    with_bearer = _upload(client, batch_id, "ok.pdf", build_f01_native(), headers=bearer)
    assert with_bearer.status_code == 200, with_bearer.text
    assert with_bearer.json()["data"]["state"] == "IMPORTED"


def _set_limits(monkeypatch, api_env, **values) -> None:
    """Reconfigure the deployment limits the way an operator does.

    Limits are configuration (docs/06 §导入), read from settings on every
    request, so the advertised value and the enforced value cannot drift.
    """
    env_names = {
        "upload_file_bytes": "PAPERINTEL_UI_UPLOAD_FILE_BYTES",
        "upload_batch_bytes": "PAPERINTEL_UI_UPLOAD_BATCH_BYTES",
        "upload_files": "PAPERINTEL_UI_UPLOAD_FILES",
        "library_page_size": "PAPERINTEL_UI_LIBRARY_PAGE_SIZE",
    }
    for key, value in values.items():
        monkeypatch.setenv(env_names[key], str(value))

    from paperintel.config.settings import load_config, reset_settings_cache

    reset_settings_cache()
    api_env["state"].settings = load_config()
