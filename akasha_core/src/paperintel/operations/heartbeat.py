"""operations.heartbeat — worker liveness without guessing (docs/06 §运维).

A worker writes a small JSON heartbeat under ``data/operations/workers/``.
The workbench reads those files and reports FRESH/STALE/UNOBSERVED — it never
derives "how many workers are online" from running task counts.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

FRESH_SECONDS = 60
STALE_SECONDS = 300
RETENTION_SECONDS = 86400


@dataclass(slots=True)
class WorkerHeartbeat:
    identity: str
    state: str
    last_seen: float
    age_seconds: float
    detail: dict | None = None


def workers_dir(data_dir: str | os.PathLike[str]) -> Path:
    path = Path(data_dir) / "operations" / "workers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_heartbeat(
    data_dir: str | os.PathLike[str],
    *,
    identity: str | None = None,
    state: str = "IDLE",
    detail: dict | None = None,
    now: float | None = None,
) -> Path:
    identity = identity or f"{socket.gethostname()}-{os.getpid()}"
    if not identity or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in identity):
        raise ValueError("Unsafe worker identity")
    payload = {
        "identity": identity,
        "state": state,
        "last_seen": now if now is not None else time.time(),
        "detail": detail or {},
    }
    path = workers_dir(data_dir) / f"{identity}.json"
    # Atomic replace: a reader never sees a half-written heartbeat.
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(path)
    return path


def read_heartbeats(
    data_dir: str | os.PathLike[str], *, now: float | None = None
) -> list[WorkerHeartbeat]:
    now = now if now is not None else time.time()
    heartbeats: list[WorkerHeartbeat] = []
    for path in sorted(workers_dir(data_dir).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            last_seen = float(payload["last_seen"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(last_seen):
            continue
        age = max(0, now - last_seen)
        if age > RETENTION_SECONDS:
            continue
        if age <= FRESH_SECONDS:
            state = "FRESH"
        elif age <= STALE_SECONDS:
            state = "STALE"
        else:
            state = "STALE"
        heartbeats.append(
            WorkerHeartbeat(
                identity=str(payload.get("identity", path.stem)),
                state=state,
                last_seen=last_seen,
                age_seconds=age,
                detail=payload.get("detail") or {},
            )
        )
    return heartbeats


def prune_heartbeats(data_dir: str | os.PathLike[str], *, now: float | None = None) -> None:
    """Only unlink old heartbeat files; fresh process identities are untouched."""
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    for path in workers_dir(data_dir).glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def freshest(
    data_dir: str | os.PathLike[str], *, now: float | None = None
) -> WorkerHeartbeat | None:
    heartbeats = read_heartbeats(data_dir, now=now)
    if not heartbeats:
        return None
    return min(heartbeats, key=lambda item: item.age_seconds)


__all__ = [
    "FRESH_SECONDS",
    "STALE_SECONDS",
    "WorkerHeartbeat",
    "freshest",
    "read_heartbeats",
    "workers_dir",
    "write_heartbeat",
]
