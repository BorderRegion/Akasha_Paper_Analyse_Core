"""Health registry tests (operations.health)."""

from __future__ import annotations

import time

from paperintel.operations.health import HealthRegistry
from paperintel.schemas.enums import ModuleHealthState
from paperintel.schemas.health import ModuleHealthRecord


def _record(module_id: str, state: ModuleHealthState) -> ModuleHealthRecord:
    return ModuleHealthRecord(module_id=module_id, state=state)


def test_check_returns_probe_record() -> None:
    registry = HealthRegistry()
    registry.register("database", lambda: _record("database", ModuleHealthState.HEALTHY))
    record = registry.check("database")
    assert record.state is ModuleHealthState.HEALTHY
    assert record.module_id == "database"


def test_unknown_module_is_unknown_state() -> None:
    registry = HealthRegistry()
    record = registry.check("not.registered")
    assert record.state is ModuleHealthState.UNKNOWN
    assert record.last_error is not None


def test_probe_failure_is_recorded_not_swallowed() -> None:
    registry = HealthRegistry()

    def boom() -> ModuleHealthRecord:
        raise RuntimeError("probe exploded")

    registry.register("broken", boom)
    record = registry.check("broken")
    assert record.state is ModuleHealthState.FAILED
    assert record.last_error is not None
    assert record.last_error["type"] == "RuntimeError"
    assert "probe exploded" in record.last_error["message"]


def test_overall_state_is_worst_state() -> None:
    registry = HealthRegistry()
    records = {
        "a": _record("a", ModuleHealthState.HEALTHY),
        "b": _record("b", ModuleHealthState.DEGRADED),
        "c": _record("c", ModuleHealthState.HEALTHY),
    }
    assert registry.overall_state(records) is ModuleHealthState.DEGRADED
    records["d"] = _record("d", ModuleHealthState.FAILED)
    assert registry.overall_state(records) is ModuleHealthState.FAILED
    assert registry.overall_state({}) is ModuleHealthState.UNKNOWN


def test_cache_is_bounded_in_time() -> None:
    registry = HealthRegistry(cache_ttl_seconds=0.05)
    calls = {"count": 0}

    def probe() -> ModuleHealthRecord:
        calls["count"] += 1
        return _record("cached", ModuleHealthState.HEALTHY)

    registry.register("cached", probe)
    registry.check("cached")
    registry.check("cached")
    assert calls["count"] == 1  # served from cache
    registry.check("cached", force=True)
    assert calls["count"] == 2
    time.sleep(0.06)
    registry.check("cached")
    assert calls["count"] == 3  # cache expired


def test_check_all_covers_registered_modules() -> None:
    registry = HealthRegistry()
    registry.register("m1", lambda: _record("m1", ModuleHealthState.HEALTHY))
    registry.register("m2", lambda: _record("m2", ModuleHealthState.DEGRADED))
    results = registry.check_all()
    assert set(results) == {"m1", "m2"}
    assert registry.overall_state(results) is ModuleHealthState.DEGRADED
