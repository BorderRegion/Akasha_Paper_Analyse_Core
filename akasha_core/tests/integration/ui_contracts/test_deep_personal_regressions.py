"""Personal-use data safety: real concurrent PostgreSQL writes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from paperintel.database.models import UiNoteRow
from paperintel.errors.ui_errors import UiError
from paperintel.services.ui import personal


def test_lost_create_response_and_two_tabs_cannot_duplicate_paper_note(api_env, fresh_paper):
    paper = fresh_paper()
    client = api_env["client"]
    payload = {"paper_id": paper.paper_id, "paper_version_id": paper.paper_version_id,
               "body": "first draft", "paper_wide": True}
    first = client.post("/v1/ui/notes", json=payload)
    assert first.status_code == 200
    retry = client.post("/v1/ui/notes", json=payload)
    assert retry.json()["data"]["note_id"] == first.json()["data"]["note_id"]
    stale = client.post("/v1/ui/notes", json={**payload, "body": "second tab draft"})
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["note_id"] == first.json()["data"]["note_id"]
    assert stale.json()["error"]["details"]["server_body"] == "first draft"
    cleared = client.patch(f'/v1/ui/notes/{first.json()["data"]["note_id"]}',
                           json={"body": "", "expected_revision": 1})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["body"] == ""


def test_gc_keeps_extraction_reports_needed_for_later_replay(api_env, fresh_paper):
    from paperintel.operations.gc import run_gc

    fresh_paper()
    directory = api_env["data_dir"]
    reports = list((directory / "cache" / "extraction_reports").glob("*.json"))
    assert reports, "fixture must exercise a real report"
    before = {path: path.read_bytes() for path in reports}
    with api_env["state"].session_factory() as session:
        report = run_gc(session, directory, dry_run=False, settings=api_env["settings"])
        session.commit()
    assert set(map(str, reports)) <= {item.object_path for item in report.protected}
    assert all(path.read_bytes() == content for path, content in before.items())


def test_concurrent_note_edits_refuse_stale_revision(api_env, fresh_paper):
    paper = fresh_paper()
    factory = api_env["state"].session_factory
    with factory() as session:
        note = personal.create_note(session, paper_id=paper.paper_id,
                                    paper_version_id=paper.paper_version_id, body="original")
        session.commit()
        note_id = note.note_id
    loaded = Event()
    proceed = Event()

    def stale_editor():
        with factory() as session:
            row = session.get(UiNoteRow, note_id)
            assert row.revision == 1
            loaded.set()
            assert proceed.wait(5)
            with pytest.raises(UiError) as error:
                personal.patch_note(session, note_id, body="stale overwrite", expected_revision=1)
                session.commit()
            assert error.value.code == "REVISION_CONFLICT"
            assert error.value.details["server_body"] == "first editor"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(stale_editor)
        assert loaded.wait(5)
        with factory() as session:
            personal.patch_note(session, note_id, body="first editor", expected_revision=1)
            proceed.set()
            session.commit()
        future.result(timeout=5)
    with factory() as session:
        row = session.get(UiNoteRow, note_id)
        assert (row.body, row.revision) == ("first editor", 2)
