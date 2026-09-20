"""Provider factory tests: config → concrete providers + registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperintel.config.provider_config import load_provider_config
from paperintel.errors import DomainError
from paperintel.providers.factory import build_provider, build_registry
from paperintel.providers.http_embeddings import OpenAICompatibleEmbeddingProvider
from paperintel.providers.http_llm import OpenAICompatibleLLMProvider
from paperintel.providers.mocks.llm import MockLLMProvider
from paperintel.providers.mocks.ocr import MockOCRProvider
from paperintel.providers.paddle_ocr import PaddleOCRHttpProvider
from paperintel.schemas.enums import LlmMockMode, OcrMockMode, ProviderKind
from paperintel.schemas.operations import ModelRoleConfig, ProviderConfigFile, ProviderEntry
from paperintel.storage.object_store import LocalObjectStore


def entry(kind: ProviderKind, **kwargs) -> ProviderEntry:
    return ProviderEntry(kind=kind, **kwargs)


def test_mock_llm_and_ocr_build() -> None:
    llm = build_provider(
        "mock_llm",
        entry(ProviderKind.MOCK_LLM, options={"mode": "INVENTED_NUMBER", "seed": 7}),
    )
    assert isinstance(llm, MockLLMProvider)
    assert llm.mode is LlmMockMode.INVENTED_NUMBER
    assert llm.seed == 7
    assert llm.provider_id == "prv_mock_llm"

    ocr = build_provider("mock_ocr", entry(ProviderKind.MOCK_OCR))
    assert isinstance(ocr, MockOCRProvider)
    assert ocr.mode is OcrMockMode.VALID


def test_unknown_mock_mode_fails_cfg002() -> None:
    with pytest.raises(DomainError) as excinfo:
        build_provider("m", entry(ProviderKind.MOCK_LLM, options={"mode": "NOT_A_MODE"}))
    assert excinfo.value.code == "CFG_002"


def test_openai_compatible_builds_with_env_key_only() -> None:
    env = {"TEST_LLM_KEY": "sk-from-env-only"}
    provider = build_provider(
        "deepseek",
        entry(
            ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://api.example.invalid/v1",
            api_key_env="TEST_LLM_KEY",
            timeout_seconds=60,
            max_concurrency=16,
            models={
                "analyst": ModelRoleConfig(model="analyst-model", temperature=0.1),
                "verifier": ModelRoleConfig(model="verifier-model"),
            },
            options={"retry_max_attempts": 5, "breaker_failure_threshold": 3},
        ),
        env=env,
    )
    assert isinstance(provider, OpenAICompatibleLLMProvider)
    assert provider.provider_id == "prv_deepseek"
    assert provider.models == {"analyst": "analyst-model", "verifier": "verifier-model"}
    assert provider.temperatures["analyst"] == 0.1
    assert provider.retry.max_attempts == 5
    assert provider.breaker.failure_threshold == 3
    assert provider.limiter.configured == 16
    # The key itself never appears in any serialized surface.
    assert "sk-from-env-only" not in repr(provider)


def test_declared_missing_api_key_fails_cfg001() -> None:
    with pytest.raises(DomainError) as excinfo:
        build_provider(
            "deepseek",
            entry(
                ProviderKind.OPENAI_COMPATIBLE,
                base_url="https://api.example.invalid/v1",
                api_key_env="DEFINITELY_NOT_SET_IN_ENV",
                models={"analyst": ModelRoleConfig(model="m")},
            ),
            env={},
        )
    assert excinfo.value.code == "CFG_001"
    assert "DEFINITELY_NOT_SET_IN_ENV" in excinfo.value.message


def test_openai_compatible_without_models_fails_cfg002() -> None:
    with pytest.raises(DomainError) as excinfo:
        build_provider(
            "x",
            entry(ProviderKind.OPENAI_COMPATIBLE, base_url="https://e.example.invalid/v1"),
            env={},
        )
    assert excinfo.value.code == "CFG_002"


def test_paddle_and_embedding_model_resolution() -> None:
    ocr = build_provider(
        "ocr_primary",
        entry(
            ProviderKind.PADDLE_HTTP,
            base_url="http://127.0.0.1:9999/v1",
            models={"ocr": ModelRoleConfig(model="PaddleOCR-VL-X")},
        ),
        env={},
    )
    assert isinstance(ocr, PaddleOCRHttpProvider)
    assert ocr.model == "PaddleOCR-VL-X"

    embed = build_provider(
        "embedder",
        entry(
            ProviderKind.HTTP_EMBEDDING,
            base_url="http://127.0.0.1:9999/v1",
            options={"model": "Qwen3-Embedding-X", "expected_dimensions": 4096},
        ),
        env={},
    )
    assert isinstance(embed, OpenAICompatibleEmbeddingProvider)
    assert embed.expected_dimensions == 4096

    # Ambiguous model configuration is a config error, never a guess.
    with pytest.raises(DomainError) as excinfo:
        build_provider(
            "bad",
            entry(
                ProviderKind.PADDLE_HTTP,
                base_url="http://127.0.0.1:9999/v1",
                models={
                    "a": ModelRoleConfig(model="one"),
                    "b": ModelRoleConfig(model="two"),
                },
            ),
            env={},
        )
    assert excinfo.value.code == "CFG_002"


def test_paddle_response_mode_wiring_and_validation() -> None:
    provider = build_provider(
        "ocr_plain",
        entry(
            ProviderKind.PADDLE_HTTP,
            base_url="http://127.0.0.1:9999/v1",
            models={"ocr": ModelRoleConfig(model="PaddleOCR-VL-X")},
            options={"response_mode": "plain_text", "max_output_tokens": 2048},
        ),
        env={},
    )
    assert isinstance(provider, PaddleOCRHttpProvider)
    assert provider.response_mode == "plain_text"
    assert provider.max_output_tokens == 2048

    with pytest.raises(DomainError) as excinfo:
        build_provider(
            "ocr_bad",
            entry(
                ProviderKind.PADDLE_HTTP,
                base_url="http://127.0.0.1:9999/v1",
                models={"ocr": ModelRoleConfig(model="PaddleOCR-VL-X")},
                options={"response_mode": "magic"},
            ),
            env={},
        )
    assert excinfo.value.code == "CFG_002"


def test_metadata_adapters_are_available() -> None:
    from paperintel.providers.http_metadata import HTTPMetadataProvider

    for kind in (ProviderKind.CROSSREF, ProviderKind.OPENALEX, ProviderKind.SEMANTIC_SCHOLAR):
        provider = build_provider(
            "meta",
            entry(kind, base_url="https://api.example.invalid"),
            env={},
        )
        assert isinstance(provider, HTTPMetadataProvider)
        assert provider.kind == kind.value


def test_local_object_store_kind(tmp_path: Path) -> None:
    provider = build_provider(
        "objects",
        entry(ProviderKind.LOCAL_OBJECT_STORE),
        data_dir=tmp_path,
    )
    assert isinstance(provider, LocalObjectStore)
    assert provider.root == tmp_path / "objects"


def test_invalid_retry_option_fails_cfg002() -> None:
    with pytest.raises(DomainError) as excinfo:
        build_provider(
            "x",
            entry(
                ProviderKind.OPENAI_COMPATIBLE,
                base_url="https://e.example.invalid/v1",
                models={"analyst": ModelRoleConfig(model="m")},
                options={"retry_max_attempts": 0},
            ),
            env={},
        )
    assert excinfo.value.code == "CFG_002"


def test_build_registry_from_mock_config_file() -> None:
    config = load_provider_config(Path("config/providers.mock.yaml"), env={})
    registry = build_registry(config, env={}, data_dir="/tmp/paperintel-test-registry")
    assert set(registry.names()) == {
        "mock_embed",
        "mock_llm",
        "mock_llm_hostile",
        "mock_metadata",
        "mock_ocr",
        "object_store",
    }
    assert registry.get("mock_metadata").provider_id == "prv_mock_metadata"
    assert registry.get_llm("mock_llm").provider_id == "prv_mock_llm"
    assert registry.get_ocr("mock_ocr").provider_id == "prv_mock_ocr"


def test_build_registry_validates_family_types() -> None:
    config = ProviderConfigFile(
        spec_version="1.0.0",
        providers={
            "mock_llm": entry(ProviderKind.MOCK_LLM),
            "mock_ocr": entry(ProviderKind.MOCK_OCR),
        },
    )
    registry = build_registry(config, env={})
    with pytest.raises((DomainError, TypeError, KeyError)):
        registry.get_llm("mock_ocr")  # wrong family must not pass as LLM
