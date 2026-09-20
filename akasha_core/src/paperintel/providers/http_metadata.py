"""Crossref, OpenAlex and Semantic Scholar metadata with retained provenance."""

import hashlib
import json
from urllib.parse import quote

import httpx

from paperintel.errors import DomainError
from paperintel.providers.base import MetadataProvider, MetadataRecord
from paperintel.providers.http_common import classify_http_failure
from paperintel.providers.resilience import (
    CircuitBreaker,
    ConcurrencyLimiter,
    ProviderStats,
    RetryPolicy,
    run_with_resilience,
)
from paperintel.schemas.common import utcnow
from paperintel.schemas.health import ModuleHealthRecord


class HTTPMetadataProvider(MetadataProvider):
    ROOTS = {
        "crossref": "https://api.crossref.org",
        "openalex": "https://api.openalex.org",
        "semantic_scholar": "https://api.semanticscholar.org/graph/v1",
    }

    def __init__(
        self,
        provider_id,
        *,
        kind,
        base_url=None,
        api_key=None,
        timeout_seconds=30,
        transport=None,
        retry=None,
        max_concurrency=2,
    ):
        super().__init__(provider_id)
        self.kind = kind
        self.base_url = (base_url or self.ROOTS[kind]).rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.retry = retry or RetryPolicy()
        self.breaker = CircuitBreaker()
        self.limiter = ConcurrencyLimiter(max_concurrency)
        self.stats = ProviderStats()

    async def _get(self, path, params=None):
        async def operation():
            headers = (
                {"x-api-key": self.api_key}
                if self.kind == "semantic_scholar" and self.api_key
                else {}
            )
            query = dict(params or {})
            if self.kind == "openalex" and self.api_key:
                query["api_key"] = self.api_key
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.get(self.base_url + path, params=query, headers=headers)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise DomainError(
                        "PROVIDER_001", message="Metadata response is not JSON."
                    ) from exc
                if not isinstance(payload, dict):
                    raise DomainError(
                        "PROVIDER_001", message="Metadata response must be an object."
                    )
                return payload

        return await run_with_resilience(
            operation,
            retry=self.retry,
            breaker=self.breaker,
            limiter=self.limiter,
            stats=self.stats,
            classify=self._classify,
        )

    def _classify(self, exc):
        # The adapter's configured secret must never survive error classification.
        failure = classify_http_failure(
            exc,
            timeout_code="PROVIDER_001",
            rate_limit_code="PROVIDER_002",
            provider_label="Metadata provider",
        )
        # Upstreams can echo credentials in arbitrary response text.
        failure.error.details.pop("body_excerpt", None)
        return failure

    def _record(self, raw):
        try:
            return self._parse_record(raw)
        except (TypeError, AttributeError, IndexError, ValueError) as exc:
            raise DomainError("PROVIDER_001", message="Malformed metadata record.") from exc

    def _parse_record(self, raw):
        if not isinstance(raw, dict):
            raise DomainError("PROVIDER_001", message="Metadata record must be an object.")
        if self.kind == "crossref":
            doi = raw.get("DOI")
            identifier = doi
            titles = raw.get("title") or []
            title = titles[0] if isinstance(titles, list) and titles else ""
            authors = [
                " ".join(filter(None, (a.get("given"), a.get("family"))))
                for a in raw.get("author", [])
            ]
            venue = (raw.get("container-title") or [None])[0]
            dates = (raw.get("published") or raw.get("issued") or {}).get("date-parts", [])
            year = dates[0][0] if dates and dates[0] else None
            url = raw.get("URL")
        elif self.kind == "openalex":
            identifier, title = raw.get("id"), raw.get("title") or raw.get("display_name")
            doi = (raw.get("doi") or "").removeprefix("https://doi.org/") or None
            authors = [
                (a.get("author") or {}).get("display_name") for a in raw.get("authorships", [])
            ]
            venue = ((raw.get("primary_location") or {}).get("source") or {}).get("display_name")
            year, url = raw.get("publication_year"), raw.get("id")
        else:
            identifier, title = raw.get("paperId"), raw.get("title")
            doi = (raw.get("externalIds") or {}).get("DOI")
            authors = [a.get("name") for a in raw.get("authors", [])]
            venue, year, url = raw.get("venue"), raw.get("year"), raw.get("url")
        if not isinstance(identifier, str) or not isinstance(title, str) or not title.strip():
            raise DomainError(
                "PROVIDER_001", message="Metadata record lacks an identifier or title."
            )
        if any(not isinstance(author, str) for author in authors):
            raise DomainError("PROVIDER_001", message="Malformed metadata authors.")
        data = {
            key: value
            for key, value in {
                "title": title,
                "doi": doi,
                "authors": authors,
                "venue": venue,
                "year": year,
            }.items()
            if value is not None
        }
        digest = hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return MetadataRecord(self.provider_id, str(identifier), data, utcnow(), url, digest)

    async def fetch_by_doi(self, doi):
        doi = doi.strip().removeprefix("https://doi.org/").removeprefix("doi:")
        if not doi.startswith("10.") or "/" not in doi:
            raise DomainError("CFG_002", message="Invalid DOI.")
        encoded = quote(doi, safe="")
        if self.kind == "crossref":
            payload = await self._get(f"/works/{encoded}")
            raw = payload.get("message") if payload is not None else None
            if payload is not None and not isinstance(raw, dict):
                raise DomainError("PROVIDER_001", message="Crossref response lacks a record.")
        elif self.kind == "openalex":
            raw = await self._get(f"/works/https://doi.org/{encoded}")
        else:
            raw = await self._get(
                f"/paper/DOI:{encoded}", {"fields": "title,authors,year,venue,url,externalIds"}
            )
        return self._record(raw) if raw is not None else None

    async def search_by_title(self, title, limit=5):
        if not title.strip() or not 1 <= limit <= 100:
            raise DomainError(
                "CFG_002", message="Metadata search requires a title and limit 1..100."
            )
        if self.kind == "crossref":
            payload = await self._get("/works", {"query.title": title, "rows": limit})
            message = (payload or {}).get("message", {})
            if not isinstance(message, dict):
                raise DomainError("PROVIDER_001", message="Malformed Crossref search response.")
            rows = message.get("items", [])
        elif self.kind == "openalex":
            payload = await self._get("/works", {"search": title, "per-page": limit})
            rows = (payload or {}).get("results", [])
        else:
            payload = await self._get(
                "/paper/search",
                {
                    "query": title,
                    "limit": limit,
                    "fields": "title,authors,year,venue,url,externalIds",
                },
            )
            rows = (payload or {}).get("data", [])
        if not isinstance(rows, list):
            raise DomainError("PROVIDER_001", message="Metadata search results must be an array.")
        return [self._record(row) for row in rows[:limit]]

    async def health(self):
        from paperintel.providers.resilience import availability_from

        return ModuleHealthRecord(
            module_id=self.provider_id,
            state=availability_from(
                self.breaker.state, self.stats.transport_error_rate, bool(self.stats.total_calls)
            ),
            checked_at=utcnow(),
            version="1.0.0",
            checks={"configured": "PASS"},
        )
