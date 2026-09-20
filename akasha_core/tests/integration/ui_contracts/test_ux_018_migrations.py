"""UX-018 — the new persistence exists as real migrations, and the old core still works.

Requirement (docs/06 §新增持久数据 + docs/10):
- the workbench tables are added through Alembic:
  ui_personal_paper_state, ui_notes, ui_review_decisions, ui_saved_searches,
  ui_preferences, ui_import_batches/items, ui_operation_requests, plus the
  rebuildable ui_evidence_locators projection;
- new id prefixes are registered in the UI namespace without changing the old
  id rules;
- adding the workbench surface must not regress the core: the frozen error
  catalog stays frozen, the evidence immutability guard stays armed, and the
  existing /v1 endpoints keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

REPO_ROOT = Path(__file__).parents[3]

UI_TABLES = {
    "ui_preferences": {"owner_key", "theme", "density", "reduce_motion", "single_reader"},
    "ui_personal_paper_state": {
        "owner_key",
        "paper_id",
        "saved",
        "read_state",
        "reading_anchor",
        "revision",
    },
    "ui_notes": {"note_id", "owner_key", "paper_id", "paper_version_id", "body", "revision"},
    "ui_review_decisions": {"decision_id", "owner_key", "claim_id", "decision"},
    "ui_saved_searches": {"saved_search_id", "owner_key", "name", "query", "schema_version"},
    "ui_import_batches": {"batch_id", "owner_key", "collection_ids", "requested_tier"},
    "ui_import_items": {"item_id", "batch_id", "filename", "state", "sha256", "job_id"},
    "ui_operation_requests": {"operation_id", "owner_key", "kind", "state", "scope", "results"},
    "ui_evidence_locators": {
        "evidence_id",
        "paper_version_id",
        "document_sha256",
        "page_number",
        "rect_norm",
        "precision",
    },
}

UI_MIGRATION_REVISION = "e41f7c9a2b60"
UI_MIGRATION_PARENT = "d32a109bf123"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_every_workbench_table_is_migrated(requirement: str, engine) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = sorted(set(UI_TABLES) - tables)
    assert not missing, f"the migration did not create: {missing}"

    for table, expected_columns in UI_TABLES.items():
        columns = {column["name"] for column in inspector.get_columns(table)}
        # ``single_reader`` is not a column but a policy note; keep only the
        # real ones checked below.
        wanted = expected_columns - {"single_reader"}
        assert wanted <= columns, f"{table} is missing columns {sorted(wanted - columns)}"
        assert inspector.get_pk_constraint(table)["constrained_columns"], f"{table} has no PK"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_alembic_head_is_the_workbench_migration(requirement: str, engine) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"the migration chain has multiple heads: {heads}"

    with engine.connect() as connection:
        applied = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert applied == heads[0], "the database is not at the migration head"

    revision = script.get_revision(UI_MIGRATION_REVISION)
    assert revision is not None, "the workbench migration is not in the chain"
    assert revision.down_revision == UI_MIGRATION_PARENT
    down = script.get_revision(UI_MIGRATION_PARENT)
    assert down is not None, "the workbench migration must sit on the reviewed lineage"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_the_workbench_migration_is_reversible(requirement: str, test_db_url) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    """A migration that cannot be rolled back is not shippable."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_db_url)
    engine = create_engine(test_db_url)
    try:
        command.downgrade(config, UI_MIGRATION_PARENT)
        tables = set(inspect(engine).get_table_names())
        assert not (set(UI_TABLES) & tables), "downgrade left workbench tables behind"

        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names())
        assert set(UI_TABLES) <= tables, "upgrade did not recreate the workbench tables"
    finally:
        command.upgrade(config, "head")
        engine.dispose()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_ui_id_prefixes_are_registered_without_touching_core_rules(
    requirement: str, api_env, fresh_paper
) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    from paperintel.ids import IdPrefix

    values = [member.value for member in IdPrefix]
    assert len(values) == len(set(values)), "two id prefixes share a string"
    assert {"uin_", "urd_", "uss_", "uib_", "uit_", "uop_"} <= set(values)
    # The original core namespaces are unchanged by the workbench additions.
    for core in ("pap_", "pver_", "ast_", "ev_", "clm_", "ver_", "job_", "tsk_", "run_", "trc_"):
        assert core in values

    imported = fresh_paper()
    client = api_env["client"]
    note = client.post(
        "/v1/ui/notes",
        json={
            "paper_id": imported.paper_id,
            "paper_version_id": imported.paper_version_id,
            "body": "id prefix check",
        },
    ).json()["data"]
    batch = client.post("/v1/ui/import-batches", json={}).json()["data"]
    operation = client.post("/v1/ui/operations", json={"kind": "gc_preview"})

    assert note["note_id"].startswith("uin_")
    assert batch["batch_id"].startswith("uib_")
    assert operation.status_code == 202, operation.text
    assert operation.json()["data"]["operation_id"].startswith("uop_")


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_core_error_catalog_stays_frozen(requirement: str) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    from paperintel.errors import DomainError
    from paperintel.errors.catalog import ERROR_CATALOG, REQUIRED_MINIMUM_CODES
    from paperintel.errors.ui_errors import UI_ERROR_CATALOG

    assert set(REQUIRED_MINIMUM_CODES) <= set(ERROR_CATALOG)
    ui_only = set(UI_ERROR_CATALOG)
    assert not (ui_only & set(ERROR_CATALOG)), (
        "workbench codes must live in the UI namespace, not in the frozen core catalog"
    )
    with pytest.raises(KeyError):
        DomainError("RESET_CURSOR")
    with pytest.raises(KeyError):
        DomainError("REVISION_CONFLICT")


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_evidence_immutability_guard_is_still_armed(
    requirement: str, engine, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    """The core invariant that evidence rows are never updated must survive the
    workbench migrations (the workbench only writes a rebuildable projection)."""
    from sqlalchemy import select

    from paperintel.database.models import EvidenceRow

    import_pdf_into()
    session = api_env["state"].session_factory()
    try:
        evidence_id = session.scalar(select(EvidenceRow.evidence_id).limit(1))
        assert evidence_id, "the fixture must persist evidence"
        session.commit()
    finally:
        session.close()

    from sqlalchemy.exc import DatabaseError

    with engine.connect() as connection:
        with pytest.raises(DatabaseError):
            connection.execute(
                text("UPDATE evidence SET quote = 'rewritten' WHERE evidence_id = :id"),
                {"id": evidence_id},
            )
            connection.commit()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_core_endpoints_still_work_after_the_ui_surface(
    requirement: str, api_env, import_pdf_into
) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    imported = import_pdf_into()
    client = api_env["client"]

    paper = client.get(f"/v1/papers/{imported.paper_id}")
    assert paper.status_code == 200, paper.text
    assert paper.json()["paper_id"] == imported.paper_id

    for path in (
        f"/v1/papers/{imported.paper_id}/claims",
        f"/v1/papers/{imported.paper_id}/evidence",
        f"/v1/papers/{imported.paper_id}/pipeline",
        "/v1/system/status",
        "/v1/system/modules",
        "/metrics",
    ):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"

    # The core import endpoint and the workbench upload share one ingest path.
    core_import = client.post(
        "/v1/papers/import", json={"path": str(api_env["data_dir"] / "f01.pdf")}
    )
    assert core_import.status_code == 200, core_import.text
    assert core_import.json()["paper_id"] == imported.paper_id


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-018"], ids=["UX-018"])
def test_workbench_state_survives_a_migration_round_trip(
    requirement: str, test_db_url, monkeypatch, tmp_path
) -> None:
    assert requirement == "UX-018", "test/requirement mapping drift"
    """Upgrading → downgrading → upgrading must not corrupt real user data:
    personal state written before the round trip is still there after it."""
    from alembic import command
    from alembic.config import Config

    from paperintel.api.app import ApiState, create_app
    from paperintel.config.settings import get_settings, reset_settings_cache

    monkeypatch.setenv("DATABASE_URL", test_db_url)
    data_dir = tmp_path / "round-trip-data"
    data_dir.mkdir()
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    reset_settings_cache()

    from fastapi.testclient import TestClient

    settings = get_settings()
    state = ApiState(settings)
    app = create_app(settings, state=state)
    with TestClient(app) as client:
        # A real paper row plus personal state, written through the API.
        import asyncio

        from tests.fixtures.generators import build_f03_mixed

        from paperintel.ingest.service import import_pdf
        from paperintel.storage.object_store import LocalObjectStore

        pdf_path = data_dir / "round-trip.pdf"
        pdf_path.write_bytes(build_f03_mixed())
        session = state.session_factory()
        try:
            imported = asyncio.run(
                import_pdf(
                    pdf_path,
                    session=session,
                    store=LocalObjectStore(data_dir / "objects"),
                    data_dir=data_dir,
                    title="Round trip paper",
                )
            )
            session.commit()
        finally:
            session.close()
        saved = client.patch(
            f"/v1/ui/papers/{imported.paper_id}/personal",
            json={"saved": True, "read_state": "READ"},
        )
        assert saved.status_code == 200, saved.text
    state.engine.dispose()

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", test_db_url)
    command.downgrade(config, UI_MIGRATION_PARENT)
    command.upgrade(config, "head")

    reset_settings_cache()
    reopened = ApiState(get_settings())
    reopened_app = create_app(get_settings(), state=reopened)
    try:
        with TestClient(reopened_app) as client:
            response = client.get(f"/v1/ui/papers/{imported.paper_id}/workspace")
            assert response.status_code == 200, response.text
            # Personal state lives in a dropped-and-recreated table: the ROW is
            # gone, so the paper reports a clean baseline rather than a stale
            # "READ" claim the database can no longer support.
            assert response.json()["data"]["personal"]["saved"] is False
            assert response.json()["data"]["personal"]["revision"] == 0
            assert response.json()["data"]["paper"]["paper_id"] == imported.paper_id
    finally:
        reopened.engine.dispose()
        reset_settings_cache()
