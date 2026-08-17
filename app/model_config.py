"""Runtime configuration for independently selectable LLM endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ModelRuntimeConfig:
    """Resolved settings for one OpenAI-compatible model endpoint."""

    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    temperature: float
    max_tokens: int


def _resolve_value(
    section: Mapping[str, object],
    name: str,
    environment: Mapping[str, str],
) -> str | None:
    env_name = section.get(f"{name}_env")
    if env_name:
        override = environment.get(str(env_name))
        if override and override.strip():
            return override.strip()

    default = section.get(name)
    if default is None:
        return None
    value = str(default).strip()
    return value or None


def resolve_model_runtime_config(
    section: Mapping[str, object],
    *,
    environment: Mapping[str, str] | None = None,
) -> ModelRuntimeConfig:
    """Resolve YAML defaults with per-model environment overrides."""
    env = os.environ if environment is None else environment
    provider = _resolve_value(section, "provider", env) or "openai"
    model = _resolve_value(section, "model", env)
    if not model:
        env_name = section.get("model_env", "MODEL_NAME")
        raise ValueError(f"Model name is required; set {env_name}")

    return ModelRuntimeConfig(
        provider=provider,
        model=model,
        api_key=_resolve_value(section, "api_key", env),
        base_url=_resolve_value(section, "base_url", env),
        temperature=float(section.get("temperature", 0.2)),
        max_tokens=int(section.get("max_tokens", 1000)),
    )
