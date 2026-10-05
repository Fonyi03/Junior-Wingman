"""Swappable LLM provider: Claude (default) or a local Ollama model.

Both providers expose one method, `structured()`, which returns a validated
Pydantic object so the rest of the app never parses free-form model text.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from .config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def structured(self, system: str, prompt: str, schema: type[T], *, fast: bool = False) -> T: ...


class ClaudeLLM:
    def __init__(self) -> None:
        import anthropic

        s = get_settings()
        if not s.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        self.model = s.claude_model
        self.fast_model = s.claude_scoring_model
        self.fallbacks = s.claude_fallbacks

    def structured(self, system: str, prompt: str, schema: type[T], *, fast: bool = False) -> T:
        kwargs: dict = dict(
            model=self.fast_model if fast else self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        if self.fallbacks:
            # Server-side fallback: if a safety classifier declines, the API retries on a fallback model.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            resp = self.client.beta.messages.parse(**kwargs)
        except self._anthropic.RateLimitError as e:
            raise LLMError(f"Claude rate limit: {e}") from e
        except self._anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except self._anthropic.APIConnectionError as e:
            raise LLMError(f"Claude connection error: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined the request")
        if resp.parsed_output is None:
            raise LLMError(f"Claude returned no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output


class OllamaLLM:
    def __init__(self) -> None:
        s = get_settings()
        self.url = s.ollama_url.rstrip("/")
        self.model = s.ollama_model

    def structured(self, system: str, prompt: str, schema: type[T], *, fast: bool = False) -> T:
        body = {
            "model": self.model,
            "stream": False,
            "format": schema.model_json_schema(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=600)
            r.raise_for_status()
            content = r.json()["message"]["content"]
            return schema.model_validate(json.loads(content))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise LLMError(f"Ollama error: {e}") from e


_instance: LLM | None = None


def get_llm() -> LLM:
    global _instance
    if _instance is None:
        provider = get_settings().llm_provider.lower()
        _instance = OllamaLLM() if provider == "ollama" else ClaudeLLM()
    return _instance


def llm_status() -> tuple[bool, str]:
    s = get_settings()
    if s.llm_provider.lower() == "ollama":
        return True, f"Ollama · {s.ollama_model} @ {s.ollama_url}"
    if not s.anthropic_api_key:
        return False, "Claude · ANTHROPIC_API_KEY missing"
    return True, f"Claude · {s.claude_model} (scoring: {s.claude_scoring_model})"
