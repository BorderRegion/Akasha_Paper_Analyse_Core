"""External novelty audit: retrieve possible related work, retain source provenance.

Title/metadata overlap is a risk signal, not proof of anticipation. Missing
results can never prove novelty. Every such limit is explicit in the verdict.
"""

import asyncio
import re

from paperintel.agents.tools import significant_tokens
from paperintel.config.provider_config import load_provider_config
from paperintel.config.settings import get_settings
from paperintel.database.models import PaperRow
from paperintel.errors import DomainError
from paperintel.providers.factory import build_registry
from paperintel.schemas.enums import ClaimType, ProviderFamily, VerifierType
from paperintel.verification.base import VerifierResult


def audit_novelty(session, claim):
    kind = VerifierType.EXTERNAL_NOVELTY
    if claim.claim_type is ClaimType.EXTERNAL or not re.search(
        r"\b(novel|novelty|first|unprecedented|new method|new approach)\b", claim.statement, re.I
    ):
        return VerifierResult(
            kind, "INCONCLUSIVE", "not a novelty assertion; external novelty not applicable", {}
        )
    settings = get_settings()
    if settings.providers_file is None:
        return VerifierResult(kind, "INCONCLUSIVE", "no external novelty source configured", {})
    registry = build_registry(load_provider_config(settings.providers_file))
    providers = registry.by_family(ProviderFamily.METADATA)
    if not providers:
        return VerifierResult(kind, "INCONCLUSIVE", "no metadata retrieval source configured", {})
    paper = session.get(PaperRow, claim.paper_id)
    query = claim.statement[:500]
    sources, failures = [], []
    tokens = significant_tokens(query) - {"novel", "novelty", "first", "new", "approach", "method"}
    for provider in providers.values():
        try:
            records = asyncio.run(provider.search_by_title(query, limit=10))
        except DomainError as exc:
            failures.append({"provider": provider.provider_id, "error_code": exc.code})
            continue
        for record in records:
            title = str(record.data.get("title", ""))
            if paper and (
                (paper.doi and str(record.data.get("doi", "")).casefold() == paper.doi.casefold())
                or title.casefold() == paper.canonical_title.casefold()
            ):
                continue
            overlap = tokens & significant_tokens(title)
            if len(overlap) < 2 or len(overlap) / max(1, len(tokens)) < 0.5:
                continue
            sources.append(
                {
                    "source_provider": record.provider,
                    "source_identifier": record.identifier,
                    "source_url": record.source_url,
                    "retrieved_at": record.retrieved_at.isoformat(),
                    "content_hash": record.content_hash,
                    "title": title,
                    "year": record.data.get("year"),
                    "overlap_terms": sorted(overlap),
                }
            )
    details = {
        "query": query,
        "related_work_candidates": sources,
        "provider_failures": failures,
        "limitation": "metadata overlap does not establish priority, equivalence or novelty",
    }
    if sources:
        return VerifierResult(
            kind, "WARN", "possible related work requires full-text novelty review", details
        )
    return VerifierResult(
        kind,
        "INCONCLUSIVE",
        "retrieval found no comparable source; novelty is not established",
        details,
    )
