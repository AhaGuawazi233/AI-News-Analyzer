"""Unified LLM client with rate limiting intercept.

Supports OpenAI-compatible APIs. Calls rate_limiter.wait_and_acquire()
before each request (may raise RateLimitTimeoutError).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from openai import OpenAI

if TYPE_CHECKING:
    from app.rate_limiter import RateLimiter


class LLMClient:
    """Unified LLM client with rate limiting intercept.

    Supports OpenAI-compatible APIs. Calls rate_limiter.wait_and_acquire()
    before each request (may raise RateLimitTimeoutError).
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str | None,
        base_url: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1000,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if provider.lower() not in {"openai", "openai_compatible"}:
            raise ValueError(
                f"Unsupported provider {provider!r}; use 'openai' or "
                "'openai_compatible'"
            )
        if not api_key:
            raise ValueError(
                "An API key is required. For a local endpoint that ignores keys, "
                "set a non-empty placeholder value."
            )

        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.rate_limiter = rate_limiter

        kwargs: dict[str, str] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)

    def chat(self, system: str, user: str) -> str:
        """Send chat completion request with rate limiting.

        Raises RateLimitTimeoutError if rate limit timeout exceeded.
        """
        if self.rate_limiter:
            self.rate_limiter.wait_and_acquire(timeout=10.0)

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )

        return response.choices[0].message.content

    def chat_json(self, system: str, user: str) -> dict[str, object]:
        """Send chat request expecting JSON response.

        Parses response as JSON. Falls back to extracting JSON from markdown.
        """
        json_system = (
            system
            + "\n\nYou MUST respond with valid JSON only. No markdown, no explanation."
        )

        response_text = self.chat(json_system, user)

        # Direct parse
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass

        # Extract from markdown code block
        json_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?```", response_text, re.DOTALL
        )
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # Find JSON object/array in text
        for pattern in [r"\{.*\}", r"\[.*\]"]:
            match = re.search(pattern, response_text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    continue

        raise ValueError(
            f"Failed to parse JSON from LLM response: {response_text[:200]}"
        )
