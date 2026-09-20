"""providers.mocks.embedding — deterministic embedding provider (P09).

Frozen mock pattern (spec doc 06 §3): the same text always yields the same
vector, and semantically similar texts (shared vocabulary) yield similar
vectors, so semantic retrieval is exercised deterministically offline.

Construction: hashed bag-of-words — each token maps to a signed dimension
via blake2b; the vector is L2-normalized. Shared tokens ⇒ high cosine
similarity; disjoint vocabulary ⇒ near-orthogonal.
"""

from __future__ import annotations

import hashlib
import math
import re

from paperintel.errors import DomainError
from paperintel.providers.base import EmbeddingProvider, EmbeddingResult
from paperintel.schemas.health import ModuleHealthRecord

#: Must match migrations/versions/a7c4e91b2f38_p09_search_projection.py.
EMBEDDING_DIMENSIONS = 64

_TOKEN_RE = re.compile(r"[A-Za-z0-9_%-]{2,}")


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic hashed bag-of-words embeddings (offline)."""

    def __init__(
        self,
        provider_id: str = "prv_mockembed",
        *,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ) -> None:
        super().__init__(provider_id)
        self.dimensions = dimensions
        self.calls = 0

    def vector_for(self, text_value: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN_RE.findall(text_value.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # Empty/unknown content: a stable unit vector, never NaN.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            raise DomainError(
                "PROVIDER_002",
                message="Embedding request with empty input rejected (never a silent empty result).",
                details={"count": 0},
            )
        self.calls += 1
        return EmbeddingResult(
            vectors=[self.vector_for(value) for value in texts],
            model_id="mock-embed-64",
            dimensions=self.dimensions,
        )

    async def health(self) -> ModuleHealthRecord:
        return ModuleHealthRecord(
            module_id="providers.embedding.mock",
            state="HEALTHY",
            checks={"deterministic": "PASS"},
            metrics={"calls_total": self.calls, "dimensions": self.dimensions},
        )
