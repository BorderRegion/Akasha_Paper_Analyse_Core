"""UX-017 — personal state and notes live on the SERVER, not in the browser.

Requirement (docs/06 §文献库与工作台 + docs/02):
- PATCH /v1/ui/papers/{id}/personal sets saved / read_state / reading_anchor with
  `expected_revision`; a lost race is 409 REVISION_CONFLICT and the client keeps
  its draft instead of overwriting;
- the reading anchor includes version/page/section and "read" is NEVER derived
  from "analysis finished";
- notes are authored by the local user, record paper_version and optional
  claim/evidence anchors, return a revision, and a conflict is 409;
- personal state is separated from scientific state by construction: no UI write
  can change a claim's support_state.
"""

from __future__ import annotations

import pytest
from tests.fixtures.generators import build_f03_mixed


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_personal_state_round_trip_with_revision_guard(
    requirement: str, api_env, fresh_paper
) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    imported = fresh_paper()
    client = api_env["client"]
    url = f"/v1/ui/papers/{imported.paper_id}/personal"

    fresh = client.post("/v1/ui/library/query", json={"limit": 50}).json()["data"]
    mine = next(item for item in fresh["items"] if item["paper_id"] == imported.paper_id)
    assert mine["personal"]["saved"] is False
    assert mine["personal"]["read_state"] == "UNREAD"
    assert mine["personal"]["revision"] == 0

    saved = client.patch(url, json={"saved": True, "read_state": "READING"})
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["saved"] is True
    assert data["read_state"] == "READING"
    assert data["revision"] == 1

    anchor = {
        "paper_version_id": imported.paper_version_id,
        "page_number": 2,
        "section_id": None,
        "scroll_offset": 0.25,
    }
    anchored = client.patch(
        url,
        json={"reading_anchor": anchor, "expected_revision": 1},
    )
    assert anchored.status_code == 200, anchored.text
    body = anchored.json()["data"]
    assert body["revision"] == 2
    assert body["reading_anchor"]["page_number"] == 2
    assert body["reading_anchor"]["paper_version_id"] == imported.paper_version_id

    # A stale expected_revision is a conflict, never a silent overwrite.
    stale = client.patch(url, json={"saved": False, "expected_revision": 1})
    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "REVISION_CONFLICT"
    assert error["details"]["expected_revision"] == 1
    assert error["details"]["current_revision"] == 2

    # And the stored state survived the rejected write.
    after = client.get(f"/v1/ui/papers/{imported.paper_id}/workspace").json()["data"]
    assert after["personal"]["saved"] is True
    assert after["personal"]["revision"] == 2


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_personal_state_must_reference_a_version_of_the_same_paper(
    requirement: str, api_env, fresh_paper
) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    first = fresh_paper()
    second = fresh_paper(name="other.pdf", builder=build_f03_mixed)
    response = api_env["client"].patch(
        f"/v1/ui/papers/{first.paper_id}/personal",
        json={"reading_anchor": {"paper_version_id": second.paper_version_id, "page": 1}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "CFG_002"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_read_state_is_not_derived_from_analysis_completion(
    requirement: str, api_env, fresh_paper, add_claim
) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    """Marking a paper READ must not fabricate claim support, and an analysed
    paper must not be reported as READ by itself."""
    imported = fresh_paper()
    client = api_env["client"]

    workspace = client.get(f"/v1/ui/papers/{imported.paper_id}/workspace").json()["data"]
    assert workspace["personal"]["read_state"] == "UNREAD", (
        "an imported/analysed paper is not automatically 'read' by the user"
    )
    add_claim(imported.paper_version_id)
    support_before = _claim_states(api_env, imported.paper_version_id)
    assert support_before, "the fixture must persist a claim to make this meaningful"

    client.patch(
        f"/v1/ui/papers/{imported.paper_id}/personal",
        json={"read_state": "READ", "saved": True},
    )
    assert _claim_states(api_env, imported.paper_version_id) == support_before, (
        "reading a paper must never change its scientific support state"
    )

    # The workspace keeps reporting analysis state independently.
    refreshed = client.get(f"/v1/ui/papers/{imported.paper_id}/workspace").json()["data"]
    assert refreshed["personal"]["read_state"] == "READ"
    assert refreshed["modules"], "module availability is analysis state, not read state"


def _claim_states(api_env, version_id: str) -> dict[str, str]:
    from sqlalchemy import select

    from paperintel.database.models import ClaimRow

    session = api_env["state"].session_factory()
    try:
        return {
            row.claim_id: row.support_state.value
            for row in session.scalars(
                select(ClaimRow).where(ClaimRow.paper_version_id == version_id)
            )
        }
    finally:
        session.close()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_notes_persist_on_the_server_across_clients(requirement: str, api_env, fresh_paper) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    """A note written by one client is readable by a NEW app instance on the
    same database: the durable copy is server-side (docs/05 §笔记)."""
    imported = fresh_paper()
    client = api_env["client"]

    created = client.post(
        "/v1/ui/notes",
        json={
            "paper_id": imported.paper_id,
            "paper_version_id": imported.paper_version_id,
            "body": "checked the sampling section",
        },
    )
    assert created.status_code == 200, created.text
    note = created.json()["data"]
    assert note["note_id"].startswith("uin_")
    assert note["revision"] == 1
    assert note["paper_version_id"] == imported.paper_version_id

    listed = client.get(f"/v1/ui/papers/{imported.paper_id}/notes")
    assert [item["note_id"] for item in listed.json()["data"]["items"]] == [note["note_id"]]

    # A brand-new app object over the same database (no shared memory).
    with _second_client(api_env) as other:
        again = other.get(f"/v1/ui/papers/{imported.paper_id}/notes")
        assert again.status_code == 200, again.text
        items = again.json()["data"]["items"]
        assert [item["note_id"] for item in items] == [note["note_id"]]
        assert items[0]["body"] == "checked the sampling section"

        updated = other.patch(
            f"/v1/ui/notes/{note['note_id']}",
            json={"body": "now with a correction", "expected_revision": 1},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["revision"] == 2

        conflict = other.patch(
            f"/v1/ui/notes/{note['note_id']}",
            json={"body": "racing draft", "expected_revision": 1},
        )
        assert conflict.status_code == 409, conflict.text
        error = conflict.json()["error"]
        assert error["code"] == "REVISION_CONFLICT"
        assert error["details"]["server_body"] == "now with a correction", (
            "the client must receive the server copy so it can merge instead of overwrite"
        )

    # Back on the first client the edited body is visible: one durable record.
    final = client.get(f"/v1/ui/papers/{imported.paper_id}/notes").json()["data"]["items"]
    assert final[0]["body"] == "now with a correction"
    assert final[0]["revision"] == 2

    deleted = client.delete(f"/v1/ui/notes/{note['note_id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True
    assert client.get(f"/v1/ui/papers/{imported.paper_id}/notes").json()["data"]["items"] == []


def _second_client(api_env):
    """A second, independent app+client over the SAME test database."""
    from fastapi.testclient import TestClient

    from paperintel.api.app import ApiState, create_app

    state = ApiState(api_env["settings"])
    return TestClient(create_app(api_env["settings"], state=state))


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_notes_validate_their_scope_and_body(requirement: str, api_env, fresh_paper) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    first = fresh_paper()
    second = fresh_paper(name="second.pdf", builder=build_f03_mixed)
    client = api_env["client"]

    empty = client.post(
        "/v1/ui/notes",
        json={
            "paper_id": first.paper_id,
            "paper_version_id": first.paper_version_id,
            "body": "   ",
        },
    )
    assert empty.status_code == 400, empty.text

    foreign = client.post(
        "/v1/ui/notes",
        json={
            "paper_id": first.paper_id,
            "paper_version_id": second.paper_version_id,
            "body": "wrong version",
        },
    )
    assert foreign.status_code == 400, foreign.text
    assert foreign.json()["error"]["code"] == "CFG_002"

    unknown = client.patch("/v1/ui/notes/uin_missing", json={"body": "x"})
    assert unknown.status_code == 404, unknown.text


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_review_decisions_never_touch_support_state(
    requirement: str, api_env, fresh_paper, add_claim
) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    """A human decision is recorded separately (docs/06 §审查与外部Agent)."""
    imported = fresh_paper()
    client = api_env["client"]
    claim_id = add_claim(imported.paper_version_id)
    before = _claim_states(api_env, imported.paper_version_id)

    response = client.post(
        "/v1/ui/review/decisions",
        json={"claim_id": claim_id, "decision": "NEEDS_REVIEW", "note": "check table 2"},
    )
    assert response.status_code == 200, response.text
    decision = response.json()["data"]
    assert decision["decision_id"].startswith("urd_")
    assert decision["claim_id"] == claim_id

    repeat = client.post(
        "/v1/ui/review/decisions",
        json={"claim_id": claim_id, "decision": "NEEDS_REVIEW", "idempotency_key": "same-click"},
    )
    again = client.post(
        "/v1/ui/review/decisions",
        json={"claim_id": claim_id, "decision": "SEEN", "idempotency_key": "same-click"},
    )
    assert repeat.json()["data"]["created"] is True
    assert again.json()["data"]["created"] is False
    assert again.json()["data"]["decision"] == "NEEDS_REVIEW"

    assert _claim_states(api_env, imported.paper_version_id) == before, (
        "a review decision must never rewrite the verification result"
    )


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_preferences_persist_and_reject_unknown_keys(requirement: str, api_env) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    client = api_env["client"]

    updated = client.patch(
        "/v1/ui/preferences",
        json={"theme": "DARK", "density": "COMPACT", "reader_font_px": 19},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["reader_font_px"] == 19

    reread = client.get("/v1/ui/preferences").json()["data"]
    assert reread["theme"] == "DARK"
    assert reread["density"] == "COMPACT"
    assert reread["reader_font_px"] == 19

    rejected = client.patch("/v1/ui/preferences", json={"data_dir": "/etc"})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "CFG_002"
    assert "/etc" not in rejected.text or "data_dir" in rejected.text

    out_of_range = client.patch("/v1/ui/preferences", json={"reader_font_px": 99})
    assert out_of_range.status_code == 400


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_first_write_race_is_detected_across_clients(
    requirement: str, api_env, fresh_paper
) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    """Two clients that both read revision 0 must not silently overwrite: the
    second write is a 409 and keeps its own draft."""
    imported = fresh_paper()
    url = f"/v1/ui/papers/{imported.paper_id}/personal"

    with _second_client(api_env) as other:
        first = api_env["client"].patch(url, json={"saved": True, "expected_revision": 0})
        assert first.status_code == 200, first.text
        assert first.json()["data"]["revision"] == 1

        second = other.patch(url, json={"saved": False, "expected_revision": 0})
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "REVISION_CONFLICT"
        assert second.json()["error"]["details"]["current_revision"] == 1


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-017"], ids=["UX-017"])
def test_reading_anchor_is_validated_when_written(requirement: str, api_env, fresh_paper) -> None:
    assert requirement == "UX-017", "test/requirement mapping drift"
    """A malformed anchor must be refused at write time; storing it would break
    every later read of the paper."""
    imported = fresh_paper()
    client = api_env["client"]
    url = f"/v1/ui/papers/{imported.paper_id}/personal"

    malformed = client.patch(
        url,
        json={"reading_anchor": {"paper_version_id": imported.paper_version_id, "page": 3}},
    )
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["error"]["code"] == "CFG_002"

    # The paper is still readable afterwards.
    workspace = client.get(f"/v1/ui/papers/{imported.paper_id}/workspace")
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["data"]["personal"]["reading_anchor"] is None
