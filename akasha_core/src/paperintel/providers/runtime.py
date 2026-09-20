"""Local deployment-wide provider budgets and durable operational events.

OS file locks release on process death; no expiring lease can accidentally
admit a second in-flight call. SQLite WAL is an operational event store, not
canonical paper data. All API/worker processes must share core.data_dir.
"""

import asyncio
import contextvars
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from paperintel.errors import DomainError

call_context = contextvars.ContextVar("provider_call_context", default=None)
runtime_root = contextvars.ContextVar("provider_runtime_root", default=None)


def database_path(data_dir):
    return Path(data_dir) / "operations" / "provider-events.sqlite3"


def emit(data_dir, provider_id, kind, **values):
    path = database_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    context = call_context.get() or {}
    # Allow-list only: never retain request text, headers, URLs or API keys.
    payload = {
        key: value
        for key, value in {**context, **values}.items()
        if key
        in {
            "model",
            "trace_id",
            "task_id",
            "run_id",
            "model_call_id",
            "status",
            "value",
            "canary_state",
            "circuit_breaker_state",
            "error_code",
            "page_number",
            "success",
        }
    }
    with sqlite3.connect(path, timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, occurred REAL NOT NULL, provider TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO events(occurred,provider,kind,payload) VALUES(?,?,?,?)",
            (time.time(), provider_id, kind, json.dumps(payload)),
        )


def read_events(data_dir):
    path = database_path(data_dir)
    if not path.is_file():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as db:
        return [
            {
                "id": row[0],
                "occurred": row[1],
                "provider": row[2],
                "kind": row[3],
                **json.loads(row[4]),
            }
            for row in db.execute(
                "SELECT id,occurred,provider,kind,payload FROM events ORDER BY id"
            )
        ]


@asynccontextmanager
async def budget_slot(data_dir, name, capacity, timeout=120):
    root = Path(data_dir) / "operations" / "budgets" / name
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    lock = None
    while lock is None:
        for index in range(capacity):
            candidate = FileLock(root / f"{index}.lock")
            try:
                candidate.acquire(timeout=0)
            except Timeout:
                continue
            lock = candidate
            break
        if lock is None:
            if time.monotonic() >= deadline:
                raise DomainError("RESOURCE_001", message=f"Provider budget {name} is exhausted.")
            await asyncio.sleep(0.025)
    try:
        yield
    finally:
        lock.release()


def instrument(provider, settings, *, data_dir=None):
    """Install shared budgets at the configured-provider boundary, once."""
    if getattr(provider, "_instrumented", False):
        return provider
    provider._instrumented = True
    root = Path(runtime_root.get() or data_dir or settings.core.data_dir)
    from paperintel.providers.resilience import ProviderStats

    if not hasattr(provider, "stats"):
        provider.stats = ProviderStats()
    provider.stats.observer = lambda kind, **values: emit(
        root, provider.provider_id, kind, **values
    )
    methods = {
        "complete": "llm_analyst",
        "recognize_page": "ocr",
        "embed": "embedding",
        "fetch_by_doi": "metadata",
        "search_by_title": "metadata",
        "search": "metadata",
    }
    for method, pool in methods.items():
        if not hasattr(provider, method):
            continue
        original = getattr(provider, method)

        async def wrapped(*args, _original=original, _pool=pool, _method=method, **kwargs):
            context = {}
            pool_name = _pool
            if _method == "complete":
                request = args[0] if args else kwargs["request"]
                pool_name = "llm_" + request.model_role.value
                context = dict(request.context)
                context["model"] = (
                    provider.model_for(request.model_role)
                    if hasattr(provider, "model_for")
                    else "mock"
                )
            else:
                context["model"] = getattr(provider, "model", "none")
            token = call_context.set(context)
            try:
                async with budget_slot(root, pool_name, getattr(settings.concurrency, pool_name)):
                    result = await _original(*args, **kwargs)
                if _method == "recognize_page":
                    emit(
                        root,
                        provider.provider_id,
                        "ocr",
                        success=True,
                        value=getattr(result, "mean_confidence", None),
                    )
                return result
            except DomainError as exc:
                emit(root, provider.provider_id, "call_failed", error_code=exc.code)
                if _method == "recognize_page":
                    emit(root, provider.provider_id, "ocr", success=False, value=None)
                raise
            finally:
                if hasattr(provider, "breaker"):
                    emit(
                        root,
                        provider.provider_id,
                        "breaker",
                        circuit_breaker_state=provider.breaker.state.value,
                    )
                call_context.reset(token)

        setattr(provider, method, wrapped)
    if hasattr(provider, "run_canary"):
        original_canary = provider.run_canary

        async def canary(*args, **kwargs):
            record = await original_canary(*args, **kwargs)
            emit(root, provider.provider_id, "canary", canary_state=record.state.value)
            return record

        provider.run_canary = canary
    return provider
