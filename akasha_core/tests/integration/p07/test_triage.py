"""P07 triage tests: resource-tier decision policy (doc 03 §16).

Triage is resource allocation only — never paper quality. Manual
override always wins; decisions carry explainable signals + reason codes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.fixtures.generators import build_f01_native

from paperintel.database.models import CollectionPaperRow, CollectionRow, TriageResultRow
from paperintel.ingest.service import import_pdf
from paperintel.schemas.enums import ResourceTier
from paperintel.storage.object_store import LocalObjectStore
from paperintel.triage.service import compute_triage, latest_triage


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def imported(session, store, data_dir, tmp_path):
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    session.commit()
    return result


def _collection(session: Session, name: str = "test-collection") -> CollectionRow:
    from paperintel.ids import new_collection_id

    row = CollectionRow(collection_id=new_collection_id(), name=name, purpose="testing")
    session.add(row)
    session.flush()
    return row


@pytest.mark.needs_db
def test_default_triage_uses_requested_tier(session, imported) -> None:
    row = compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T1_SCAN)
    assert row.recommended_tier is ResourceTier.T1_SCAN
    assert row.effective_tier is ResourceTier.T1_SCAN
    assert row.manual_override is False
    assert "REQUESTED_TIER" in row.reason_codes
    # Signals are recorded explicitly (neutral defaults), never invented.
    assert set(row.signals) == {
        "user_relevance",
        "novelty_signal",
        "method_transferability",
        "research_importance",
        "uncertainty_value",
        "venue_prior",
    }
    assert all(value == 0.0 for value in row.signals.values())


@pytest.mark.needs_db
def test_pinned_paper_upgrades_to_full_tier(session, imported) -> None:
    collection = _collection(session)
    session.add(
        CollectionPaperRow(
            collection_id=collection.collection_id,
            paper_id=imported.paper_id,
            pinned=True,
        )
    )
    session.flush()

    row = compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T1_SCAN)
    assert "USER_PINNED" in row.reason_codes
    assert row.effective_tier is ResourceTier.T2_FULL
    assert row.signals["user_relevance"] == 1.0


@pytest.mark.needs_db
def test_manual_override_always_wins(session, imported) -> None:
    """Doc 02 §11: manual override beats every automatic decision."""
    collection = _collection(session)
    session.add(
        CollectionPaperRow(
            collection_id=collection.collection_id,
            paper_id=imported.paper_id,
            pinned=True,  # would push to T2
            priority_override_tier=ResourceTier.T0_INDEX,  # operator says index-only
        )
    )
    session.flush()

    row = compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T3_DEEP)
    assert row.effective_tier is ResourceTier.T0_INDEX
    assert row.manual_override is True
    assert "COLLECTION_PRIORITY_OVERRIDE" in row.reason_codes


@pytest.mark.needs_db
def test_unknown_paper_rejected(session) -> None:
    from paperintel.errors import DomainError

    with pytest.raises(DomainError) as excinfo:
        compute_triage(
            session, paper_id="pap_01UNKNOWN000000000000000000", requested_tier=ResourceTier.T1_SCAN
        )
    assert excinfo.value.code == "CFG_002"


@pytest.mark.needs_db
def test_triage_runs_are_audited(session, imported) -> None:
    """Each triage decision links to an analysis run (who decided)."""
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T1_SCAN)
    row = latest_triage(session, imported.paper_id)
    assert row is not None
    assert row.created_by_run_id is not None
    assert row.created_by_run_id.startswith("run_")

    # A second decision is a NEW row (history preserved), latest wins.
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T2_FULL)
    assert session.scalar(select(func.count()).select_from(TriageResultRow)) == 2
    assert latest_triage(session, imported.paper_id).effective_tier is ResourceTier.T2_FULL
