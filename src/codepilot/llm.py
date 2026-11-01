"""Module 5a: LLM provider abstraction.

Callers depend on the `LLMProvider` protocol only, so providers can be swapped and tests
can inject a fake. Keys come from the environment and are never logged.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Protocol

import httpx

from .config import Settings
from .errors import LLMUnavailableError

log = logging.getLogger("codepilot.llm")


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, prompt: str) -> str: ...


class NullProvider:
    name = "none"

    def complete(self, system: str, prompt: str) -> str:
        raise LLMUnavailableError("No LLM provider is configured (set CODEPILOT_LLM_PROVIDER)")


class _HTTPProvider:
    name = "http"
    url = ""

    def __init__(self, api_key: str, model: str, timeout_s: float, transport: httpx.BaseTransport | None = None,
                 retries: int = 1):
        if not api_key:
            raise LLMUnavailableError(f"{self.name}: API key is missing")
        self._key, self.model, self.retries = api_key, model, retries
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, system: str, prompt: str) -> dict:
        raise NotImplementedError

    def _text(self, data: dict) -> str:
        raise NotImplementedError

    def complete(self, system: str, prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.post(self.url, headers=self._headers(), json=self._body(system, prompt))
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return self._text(resp.json())
            except httpx.TimeoutException as exc:
                last = exc
                log.warning("llm timeout", extra={"provider": self.name, "attempt": attempt})
            except httpx.HTTPStatusError as exc:
                raise LLMUnavailableError(f"{self.name} returned HTTP {exc.response.status_code}") from exc
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                raise LLMUnavailableError(f"{self.name} request failed: {type(exc).__name__}") from exc
        raise LLMUnavailableError(f"{self.name} timed out") from last


class OpenAIProvider(_HTTPProvider):
    name = "openai"
    url = "https://api.openai.com/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    def _body(self, system: str, prompt: str) -> dict:
        return {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}

    def _text(self, data: dict) -> str:
        return data["choices"][0]["message"]["content"]


class AnthropicProvider(_HTTPProvider):
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._key, "anthropic-version": "2023-06-01"}

    def _body(self, system: str, prompt: str) -> dict:
        return {"model": self.model, "max_tokens": 1500, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": prompt}]}

    def _text(self, data: dict) -> str:
        return "".join(b.get("text", "") for b in data["content"])


def make_provider(settings: Settings, transport: httpx.BaseTransport | None = None) -> LLMProvider:
    kind = settings.llm_provider.lower()
    if kind == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        return OpenAIProvider(key, settings.llm_model, settings.llm_timeout_s, transport)
    if kind == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        return AnthropicProvider(key, settings.llm_model, settings.llm_timeout_s, transport)
    return NullProvider()
