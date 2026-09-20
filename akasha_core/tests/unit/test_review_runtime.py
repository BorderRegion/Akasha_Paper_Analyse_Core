"""Regression checks for the review's previously disconnected runtime paths."""

import asyncio
import hashlib
import multiprocessing
from pathlib import Path

import httpx
import pytest

from paperintel.config.fingerprint import analysis_config_hash
from paperintel.config.settings import get_settings
from paperintel.errors import DomainError
from paperintel.operations.provider_observations import metric_families, snapshots
from paperintel.providers.base import ChatMessage, LlmRequest
from paperintel.providers.http_metadata import HTTPMetadataProvider
from paperintel.providers.mocks.llm import MockLLMProvider
from paperintel.providers.resilience import RetryPolicy
from paperintel.providers.runtime import budget_slot, emit, instrument, read_events
from paperintel.schemas.enums import LlmMockMode


@pytest.mark.parametrize(
    "kind,record",
    [
        (
            "crossref",
            {
                "DOI": "10.1234/demo",
                "title": ["Graph learning"],
                "author": [{"given": "A", "family": "B"}],
            },
        ),
        (
            "openalex",
            {
                "id": "https://openalex.org/W1",
                "title": "Graph learning",
                "doi": "https://doi.org/10.1234/demo",
            },
        ),
        (
            "semantic_scholar",
            {"paperId": "p1", "title": "Graph learning", "externalIds": {"DOI": "10.1234/demo"}},
        ),
    ],
)
def test_metadata_real_adapters_retain_provenance(kind, record):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"message": record} if kind == "crossref" else record)

    provider = HTTPMetadataProvider("source", kind=kind, transport=httpx.MockTransport(handle))
    result = asyncio.run(provider.fetch_by_doi("10.1234/demo"))
    assert result.provider == "source"
    assert result.data["title"] == "Graph learning"
    assert result.data["doi"] == "10.1234/demo"
    assert len(result.content_hash) == 64 and result.retrieved_at.tzinfo
    assert "10.1234" in str(seen[0].url)


@pytest.mark.parametrize(
    "payload",
    [[], "invalid", {"message": []}, {"message": {"DOI": "x", "title": ["x"], "author": [None]}}],
)
def test_metadata_malformed_is_coded(payload):
    provider = HTTPMetadataProvider(
        "source",
        kind="crossref",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(provider.fetch_by_doi("10.1234/demo"))
    assert error.value.code == "PROVIDER_001"


def test_metadata_retry_not_found_and_secret_redaction():
    statuses = iter([429, 200, 404, 403])
    secret = "custom-unprefixed-secret"
    provider = HTTPMetadataProvider(
        "source",
        kind="openalex",
        api_key=secret,
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, max_delay_s=0),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(next(statuses), json={"results": [], "echo": secret})
        ),
    )
    assert asyncio.run(provider.search_by_title("Graph learning")) == []
    assert provider.stats.total_calls == 2
    assert asyncio.run(provider.fetch_by_doi("10.1234/demo")) is None
    with pytest.raises(DomainError) as error:
        asyncio.run(provider.search_by_title("Graph learning"))
    assert secret not in str(error.value.details)


def _hold_budget(root, ready, release):
    async def hold():
        async with budget_slot(root, "llm_analyst", 1):
            ready.set()
            await asyncio.to_thread(release.wait, 10)

    asyncio.run(hold())


def test_budget_is_cross_process_and_independent(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_budget, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(10)

        async def probe():
            with pytest.raises(DomainError, match="exhausted"):
                async with budget_slot(tmp_path, "llm_analyst", 1, timeout=0.05):
                    pytest.fail("A second process exceeded the shared budget")
            for name in ("llm_verifier", "llm_synthesizer", "ocr", "embedding", "metadata"):
                async with budget_slot(tmp_path, name, 1, timeout=0.05):
                    pass

        asyncio.run(probe())
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0

    async def reacquire():
        async with budget_slot(tmp_path, "llm_analyst", 1, timeout=0.05):
            pass

    asyncio.run(reacquire())


def test_cancelled_call_releases_budget(tmp_path):
    async def scenario():
        entered = asyncio.Event()

        async def hold():
            async with budget_slot(tmp_path, "ocr", 1):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with budget_slot(tmp_path, "ocr", 1, timeout=0.05):
            pass

    asyncio.run(scenario())


def test_durable_failures_quality_and_canary(tmp_path):
    settings = get_settings()
    provider = instrument(
        MockLLMProvider("same", mode=LlmMockMode.TIMEOUT), settings, data_dir=tmp_path
    )
    with pytest.raises(DomainError):
        asyncio.run(
            provider.complete(
                LlmRequest(messages=[ChatMessage(role="user", content="do not retain this")])
            )
        )
    # New client starts with empty process-local counters; projection retains all attempts.
    other = instrument(MockLLMProvider("same"), settings, data_dir=tmp_path)
    assert other.stats.total_calls == 0
    other.stats.record_schema(False)
    other.stats.record_citation(False)
    other.stats.record_unsupported_claim()
    emit(tmp_path, "same", "canary", canary_state="FAILED")
    status = snapshots(tmp_path)["same"]
    assert status["total_calls"] == 3
    assert status["availability_state"] == "DEGRADED"
    assert status["schema_pass_rate"] == status["citation_pass_rate"] == 0
    assert status["unsupported_claim_rate"] == 1
    events = read_events(tmp_path)
    assert "do not retain this" not in str(events)
    assert (
        snapshots(tmp_path, now=events[-1]["occurred"] + 86401)["same"]["canary_state"] == "STALE"
    )
    requests = next(f for f in metric_families(tmp_path) if f.name == "provider_requests")
    assert sum(sample.value for sample in requests.samples) == 3


def test_config_snapshot_matches_digest_and_changes_with_policy():
    settings = get_settings()
    digest = analysis_config_hash(settings=settings, model_role="analyst")
    snapshot = Path(settings.core.data_dir) / "operations/configurations" / f"{digest}.json"
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest
    assert analysis_config_hash(settings=settings, model_role="verifier") != digest
    assert analysis_config_hash(settings=settings, model_role="analyst") == digest


def test_celery_cli_discovers_registered_worker():
    from celery.app.utils import find_app

    from paperintel.workflow.celery_app import TASK_NAME

    app = find_app("paperintel.workflow.celery_app")
    assert TASK_NAME in app.tasks
    assert app.conf.broker_transport_options["priority_steps"] == list(range(10))
