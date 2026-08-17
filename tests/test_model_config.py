"""Tests for independently configurable small and large model endpoints."""

from __future__ import annotations

import pytest

from app import llm_client
from app.llm_client import LLMClient
from app.model_config import resolve_model_runtime_config


MODEL_SECTION = {
    "provider": "openai",
    "provider_env": "TEST_PROVIDER",
    "model": "default-model",
    "model_env": "TEST_MODEL_NAME",
    "api_key_env": "TEST_API_KEY",
    "base_url_env": "TEST_BASE_URL",
    "temperature": 0.25,
    "max_tokens": 321,
}


def test_model_config_uses_environment_overrides() -> None:
    resolved = resolve_model_runtime_config(
        MODEL_SECTION,
        environment={
            "TEST_PROVIDER": "openai_compatible",
            "TEST_MODEL_NAME": "custom-model",
            "TEST_API_KEY": "custom-key",
            "TEST_BASE_URL": "https://llm.example/v1",
        },
    )

    assert resolved.provider == "openai_compatible"
    assert resolved.model == "custom-model"
    assert resolved.api_key == "custom-key"
    assert resolved.base_url == "https://llm.example/v1"
    assert resolved.temperature == 0.25
    assert resolved.max_tokens == 321


def test_model_config_keeps_yaml_defaults_when_overrides_are_blank() -> None:
    resolved = resolve_model_runtime_config(
        MODEL_SECTION,
        environment={
            "TEST_MODEL_NAME": " ",
            "TEST_BASE_URL": "",
        },
    )

    assert resolved.provider == "openai"
    assert resolved.model == "default-model"
    assert resolved.api_key is None
    assert resolved.base_url is None


def test_llm_client_passes_custom_base_url_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(llm_client, "OpenAI", FakeOpenAI)

    client = LLMClient(
        provider="openai_compatible",
        model="vendor/model-name",
        api_key="secret-key",
        base_url="https://llm.example/v1",
    )

    assert client.model == "vendor/model-name"
    assert client.base_url == "https://llm.example/v1"
    assert captured == {
        "api_key": "secret-key",
        "base_url": "https://llm.example/v1",
    }


def test_llm_client_rejects_missing_api_key() -> None:
    with pytest.raises(ValueError, match="API key is required"):
        LLMClient(provider="openai", model="model", api_key=None)
