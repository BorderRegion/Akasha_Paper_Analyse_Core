"""Real keep-alive sockets and deployment lifecycle regressions (no paid API)."""

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from paperintel.errors import DomainError
from paperintel.operations import doctor
from paperintel.operations.heartbeat import (
    RETENTION_SECONDS,
    prune_heartbeats,
    read_heartbeats,
    write_heartbeat,
)
from paperintel.operations.worker_heartbeat import HeartbeatWriter
from paperintel.providers.base import ChatMessage, LlmRequest
from paperintel.providers.http_llm import OpenAICompatibleLLMProvider
from paperintel.providers.resilience import ConcurrencyLimiter, RetryPolicy
from paperintel.schemas.enums import ModelRole


@pytest.mark.parametrize("legacy_pool", [False, True])
def test_provider_uses_distinct_pools_across_closed_event_loops(legacy_pool, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({
                "model": "local", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = OpenAICompatibleLLMProvider(
        "local", base_url=f"http://127.0.0.1:{server.server_port}",
        models={ModelRole.ANALYST: "local"}, retry=RetryPolicy(max_attempts=1),
    )
    pools = []
    loops = []

    async def request():
        loops.append(asyncio.get_running_loop())
        pools.append(provider._client.current())
        result = await provider.complete(LlmRequest(
            messages=[ChatMessage(role="user", content="hello")], model_role=ModelRole.ANALYST,
        ))
        assert result.content == "{}"
        assert provider._client.current() is pools[-1]

    try:
        asyncio.run(request())
        assert loops[0].is_closed()
        if legacy_pool:
            # Control: force the original implementation's single shared pool.
            monkeypatch.setattr(provider._client, "current", lambda: pools[0])
            with pytest.raises(DomainError) as error:
                asyncio.run(request())
            assert error.value.code == "PROVIDER_001"
            assert error.value.details["exception_type"] == "RuntimeError"
        else:
            asyncio.run(request())
            assert pools[0] is not pools[1]
    finally:
        # Deliberately closed owner loops above to reproduce the original bug.
        # Closing these stale transports may itself raise after closing the socket.
        for pool in pools:
            try:
                asyncio.run(pool.aclose())
            except RuntimeError:
                pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_concurrency_limiter_can_contend_on_successive_loops():
    limiter = ConcurrencyLimiter(1)

    async def compete():
        async def operation():
            async with limiter.slot():
                await asyncio.sleep(0)
        await asyncio.gather(operation(), operation(), operation())
        assert limiter.active == 0

    asyncio.run(compete())
    asyncio.run(compete())


@pytest.mark.parametrize("location", ["/doctor.py", "/opt/lib/python/site-packages/paperintel/operations/doctor.py"])
def test_doctor_without_source_checkout_is_unknown(tmp_path, monkeypatch, location):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "__file__", location)
    assert doctor.required_fixtures_check().startswith("UNKNOWN:")


def test_worker_heartbeat_updates_during_long_task_and_cleans_old_files(tmp_path):
    writer = HeartbeatWriter(tmp_path, interval=0.02)
    writer.start()
    try:
        writer.task_started("task-one")
        path = next((tmp_path / "operations/workers").glob("*.json"))
        before = json.loads(path.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            after = json.loads(path.read_text())
            if after["last_seen"] > before["last_seen"]:
                break
            time.sleep(0.01)
        assert after["last_seen"] > before["last_seen"]
        assert after["state"] == "RUNNING"
        assert after["detail"]["task_ids"] == ["task-one"]
        writer.task_finished("task-one")
        assert json.loads(path.read_text())["state"] == "IDLE"
        stale = write_heartbeat(tmp_path, identity="expired", now=1)
        os.utime(stale, (1, 1))
        prune_heartbeats(tmp_path, now=RETENTION_SECONDS + 2)
        assert not stale.exists()
        assert read_heartbeats(tmp_path)[0].state == "FRESH"
    finally:
        writer.stop()


def test_celery_signals_write_heartbeat_and_task_states(tmp_path, monkeypatch):
    from celery import signals

    from paperintel.config import settings
    from paperintel.operations import worker_heartbeat

    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(tmp_path))
    settings.reset_settings_cache()
    worker_heartbeat.install_signals()
    signals.worker_process_init.send(sender=None)
    try:
        assert len(read_heartbeats(tmp_path)) == 1
        signals.task_prerun.send(sender=None, task_id="celery-task")
        path = next((tmp_path / "operations/workers").glob("*.json"))
        assert json.loads(path.read_text())["state"] == "RUNNING"
        signals.task_postrun.send(sender=None, task_id="celery-task")
        assert json.loads(path.read_text())["state"] == "IDLE"
    finally:
        signals.worker_process_shutdown.send(sender=None)
    assert worker_heartbeat._writer is None
