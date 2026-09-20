"""Triage result contract (spec doc 03 §16).

The triage score is resource allocation only, not paper quality. Manual
override always wins over automatic triage (spec doc 02 §11).
"""

from __future__ import annotations

from pydantic import Field

from paperintel.schemas.common import FrozenModel, LoosePaperId
from paperintel.schemas.enums import ResourceTier


class TriageSignals(FrozenModel):
    """Automatic triage signals (spec doc 07 §3)."""

    user_relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    collection_relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    novelty_signal: float | None = Field(default=None, ge=0.0, le=1.0)
    method_transferability: float | None = Field(default=None, ge=0.0, le=1.0)
    research_importance: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_value: float | None = Field(default=None, ge=0.0, le=1.0)
    venue_prior: float | None = Field(default=None, ge=0.0, le=1.0)


class TriageResult(FrozenModel):
    paper_id: LoosePaperId
    recommended_tier: ResourceTier
    effective_tier: ResourceTier
    manual_override: bool = False
    signals: TriageSignals = Field(default_factory=TriageSignals)
    reason_codes: list[str] = Field(default_factory=list)


__all__ = ["TriageResult", "TriageSignals"]
