"""schemas.ui.session — bootstrap, session and capability DTOs (docs/06)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from paperintel.schemas.common import FrozenModel
from paperintel.schemas.ui.envelope import CapabilityAvailability, RunMode


class Bootstrap(FrozenModel):
    """GET /v1/ui/bootstrap — deliberately minimal while unauthenticated.

    It must NOT reveal papers, paths or provider details (docs/06).
    """

    app_version: str
    ui_contract_version: str
    auth_required: bool
    mode: RunMode


class SessionInfo(FrozenModel):
    """POST/DELETE /v1/ui/session result."""

    authenticated: bool
    csrf_token: str | None = None
    expires_at: datetime | None = None


class Capability(FrozenModel):
    code: str = Field(min_length=1)
    availability: CapabilityAvailability
    reason: str | None = None


class CapabilityLimits(FrozenModel):
    upload_file_bytes: int = Field(ge=1)
    upload_batch_bytes: int = Field(ge=1)
    upload_files: int = Field(ge=1)
    library_page_size: int = Field(ge=1, le=100)


class Capabilities(FrozenModel):
    ui_contract_version: str
    capabilities: list[Capability] = Field(default_factory=list)
    limits: CapabilityLimits
    mode: RunMode


__all__ = ["Bootstrap", "Capabilities", "Capability", "CapabilityLimits", "SessionInfo"]
