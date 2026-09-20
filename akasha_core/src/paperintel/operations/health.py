"""Module health registry (frozen module: operations.health).

Every module exposes health status (spec doc 02 §1.12). Probes are cheap and
cached for a bounded time (spec doc 06 §9). This registry aggregates
ModuleHealthRecords into an overall state for /v1/system/status and paperctl.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from paperintel.schemas.enums import ModuleHealthState
from paperintel.schemas.health import ModuleHealthRecord

#: Ordered from worst to best; the overall state is the worst recorded state.
_STATE_RANK: dict[ModuleHealthState, int] = {
    ModuleHealthState.FAILED: 0,
    ModuleHealthState.UNAVAILABLE: 1,
    ModuleHealthState.MISCONFIGURED: 2,
    ModuleHealthState.DEGRADED: 3,
    ModuleHealthState.UNKNOWN: 4,
    ModuleHealthState.HEALTHY: 5,
}


class HealthRegistry:
    """Collects health probes from modules and aggregates overall state."""

    def __init__(self, cache_ttl_seconds: float = 30.0) -> None:
        self._probes: dict[str, Callable[[], ModuleHealthRecord | Any]] = {}
        self._cache: dict[str, tuple[float, ModuleHealthRecord]] = {}
        self.cache_ttl_seconds = cache_ttl_seconds

    def register(self, module_id: str, probe: Callable[[], ModuleHealthRecord | Any]) -> None:
        self._probes[module_id] = probe

    def unregister(self, module_id: str) -> None:
        self._probes.pop(module_id, None)
        self._cache.pop(module_id, None)

    def check(self, module_id: str, *, force: bool = False) -> ModuleHealthRecord:
        """Run (or reuse cached) health probe for one module.

        A probe that raises is reported as FAILED with the error captured —
        health checks never swallow failures silently (spec doc 06 §16): the
        exception is recorded in ``last_error``.
        """
        now = time.monotonic()
        cached = self._cache.get(module_id)
        if cached is not None and not force and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]
        probe = self._probes.get(module_id)
        if probe is None:
            record = ModuleHealthRecord(
                module_id=module_id,
                state=ModuleHealthState.UNKNOWN,
                last_error={"reason": "no probe registered"},
            )
        else:
            try:
                result = probe()
                if isinstance(result, ModuleHealthRecord):
                    record = result
                else:  # coroutine from async probes must be awaited by caller
                    raise TypeError("synchronous probe required; wrap async probes with an adapter")
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                record = ModuleHealthRecord(
                    module_id=module_id,
                    state=ModuleHealthState.FAILED,
                    last_error={
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
        self._cache[module_id] = (now, record)
        return record

    def check_all(self, *, force: bool = False) -> dict[str, ModuleHealthRecord]:
        return {module_id: self.check(module_id, force=force) for module_id in sorted(self._probes)}

    def overall_state(self, records: dict[str, ModuleHealthRecord]) -> ModuleHealthState:
        if not records:
            return ModuleHealthState.UNKNOWN
        return min(
            (record.state for record in records.values()),
            key=lambda state: _STATE_RANK[state],
        )


__all__ = ["HealthRegistry"]
