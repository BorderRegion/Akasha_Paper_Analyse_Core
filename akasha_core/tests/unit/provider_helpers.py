"""Shared helpers for HTTP provider tests (httpx.MockTransport based —
fully offline and deterministic)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx


class FakeClock:
    """Monotonic clock the tests control explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleeper:
    """asyncio.sleep replacement: records delays and advances a FakeClock."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.delays: list[float] = []
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)


def json_response(payload: Any, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def chat_response(
    content: str | None,
    *,
    model: str = "test-model",
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    response_id: str = "chatcmpl-test",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return json_response(
        {
            "id": response_id,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def raw_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body.encode(), headers={"Content-Type": "text/plain"})


def make_transport(handler: Callable[[httpx.Request], Any]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def embeddings_response(vectors: list[list[float]], model: str = "embed-model") -> httpx.Response:
    return json_response(
        {
            "model": model,
            "data": [
                {"index": i, "object": "embedding", "embedding": vec}
                for i, vec in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    )


class SequenceHandler:
    """Returns queued responses in order; repeats the last one forever."""

    def __init__(self, *responses: Any) -> None:
        self.queue: list[Any] = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> Any:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    @property
    def call_count(self) -> int:
        return len(self.requests)
