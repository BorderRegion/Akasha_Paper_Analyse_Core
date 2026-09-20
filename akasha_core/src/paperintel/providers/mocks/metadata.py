"""providers.mocks.metadata — deterministic metadata provider (P08).

Frozen mock for tests and offline operation (spec doc 06 §3 pattern:
deterministic per input, no network). Unknown identifiers return None —
absence is an honest answer, never fabricated metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime

from paperintel.providers.base import MetadataProvider, MetadataRecord
from paperintel.schemas.health import ModuleHealthRecord

#: Deterministic fixtures for well-known test identifiers.
_FIXTURES: dict[str, dict] = {
    "10.1234/paperintel.f01": {
        "title": "Native Extraction: A Clean Digital Paper",
        "authors": ["A. Author", "B. Author"],
        "venue": "Journal of Deterministic Fixtures",
        "year": 2024,
        "doi": "10.1234/paperintel.f01",
    },
    "10.1234/paperintel.f04": {
        "title": "Rich Structure: Tables, Figures and Equations",
        "authors": ["C. Author"],
        "venue": "Proceedings of FixtureConf",
        "year": 2023,
        "doi": "10.1234/paperintel.f04",
    },
}


class MockMetadataProvider(MetadataProvider):
    """Deterministic DOI/metadata provider (offline)."""

    def __init__(self, provider_id: str = "prv_mockmeta") -> None:
        super().__init__(provider_id)

    async def fetch_by_doi(self, doi: str) -> MetadataRecord | None:
        fixture = _FIXTURES.get(doi.strip().lower())
        if fixture is None:
            return None
        return self._record(doi, fixture)

    async def search_by_title(self, title: str, limit: int = 5) -> list[MetadataRecord]:
        """Deterministic title search: exact normalized match first, then
        substring matches, capped at ``limit``. No fuzzy invention: a miss
        returns an empty list (honest absence)."""
        normalized = " ".join(title.lower().split())
        exact: list[MetadataRecord] = []
        partial: list[MetadataRecord] = []
        for doi, fixture in _FIXTURES.items():
            candidate = " ".join(fixture["title"].lower().split())
            if candidate == normalized:
                exact.append(self._record(doi, fixture))
            elif normalized and (normalized in candidate or candidate in normalized):
                partial.append(self._record(doi, fixture))
        return (exact + partial)[:limit]

    async def health(self) -> ModuleHealthRecord:
        return ModuleHealthRecord(
            module_id="providers.metadata.mock",
            state="HEALTHY",
            checks={"deterministic": "PASS"},
            metrics={"fixtures_total": len(_FIXTURES)},
        )

    def _record(self, identifier: str, fixture: dict) -> MetadataRecord:
        return MetadataRecord(
            provider=self.provider_id,
            identifier=identifier,
            data=dict(fixture),
            retrieved_at=datetime.now(UTC),
            source_url=f"https://example.invalid/{identifier}",
        )
