"""Provider-agnostic chat LLM client.

Selected via the ``LLM_PROVIDER`` env var. Currently supports ``openai``
(default) and ``anthropic``.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    """An LLM completion plus the token usage reported by the provider."""

    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _metered(result: LLMResult) -> LLMResult:
    """Charge the call's real cost to the tenant whose work it is (see billing)."""
    from .billing import meter

    meter(result.model, result.prompt_tokens, result.completion_tokens)
    return result


class LLMBackend(Protocol):
    model: str

    def complete(self, system: str, user: str, *, temperature: float = 0) -> str: ...

    def complete_with_usage(self, system: str, user: str, *, temperature: float = 0) -> LLMResult: ...


class OpenAIBackend:
    def __init__(self, model: str | None = None):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("OPENAI_API_KEY is not configured.")
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise LLMError("openai package is not installed.") from exc

        self._client = OpenAI(api_key=api_key)
        self.model = model or _provider_model("openai", os.getenv("LLM_MODEL")) or "gpt-4o"

    def complete(self, system: str, user: str, *, temperature: float = 0) -> str:
        return self.complete_with_usage(system, user, temperature=temperature).text

    def complete_with_usage(self, system: str, user: str, *, temperature: float = 0) -> LLMResult:
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = getattr(response, "usage", None)
        return _metered(
            LLMResult(
                text=response.choices[0].message.content or "",
                model=self.model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
        )


class AnthropicBackend:
    DEFAULT_MODEL = "claude-opus-5-5"
    # Models that accept the server-side refusal fallback (``fallbacks: "default"``):
    # a safety decline is retried on a fallback model inside the same call.
    FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str | None = None):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not configured.")
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as exc:
            raise LLMError("anthropic package is not installed.") from exc

        self._client = Anthropic(api_key=api_key)
        self.model = model or _provider_model("anthropic", os.getenv("LLM_MODEL")) or self.DEFAULT_MODEL
        # Optional output_config.effort (low|medium|high|xhigh|max); unset = the model's default.
        self.effort = os.getenv("LLM_EFFORT", "").strip().lower()

    def complete(self, system: str, user: str, *, temperature: float = 0) -> str:
        return self.complete_with_usage(system, user, temperature=temperature).text

    def complete_with_usage(self, system: str, user: str, *, temperature: float = 0) -> LLMResult:
        # ``temperature`` is accepted for interface parity but not sent: current
        # Claude models reject sampling parameters, and thinking (always on for
        # them) counts toward max_tokens, hence the generous cap.
        kwargs = {}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.model in self.FALLBACK_MODELS:
            kwargs["betas"] = [self.FALLBACK_BETA]
            kwargs["extra_body"] = {"fallbacks": "default"}
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            **kwargs,
        )
        if response.stop_reason == "refusal":
            raise LLMError("The model declined to answer this request.")
        usage = getattr(response, "usage", None)
        # Concatenate any text blocks in the response (thinking blocks are skipped)
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        return _metered(
            LLMResult(
                text=text,
                # After a fallback the answer (and its price) is the fallback model's.
                model=getattr(response, "model", None) or self.model,
                prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
                completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            )
        )


def _provider_model(provider: str, model: str | None) -> str | None:
    """``model`` if it belongs to ``provider``, else None (use the provider default).

    Plans, env vars and stored settings name models like ``gpt-4o``; after a
    provider switch those names would 404 at the new provider.
    """
    if not model:
        return None
    is_claude = model.startswith("claude-")
    return model if is_claude == (provider == "anthropic") else None


_lock = threading.Lock()
# Cache one backend per (provider, model) so per-plan model routing reuses clients.
_backends: dict[tuple[str, str], LLMBackend] = {}


def get_backend(model: str | None = None) -> LLMBackend:
    """Return an LLM backend, optionally for a specific model (e.g. a plan's model).

    ``model=None`` uses the provider's default (``LLM_MODEL`` env / built-in).
    """
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    model = _provider_model(provider, model)
    key = (provider, model or "")
    cached = _backends.get(key)
    if cached is not None:
        return cached
    with _lock:
        if key not in _backends:
            if provider == "openai":
                _backends[key] = OpenAIBackend(model=model)
            elif provider == "anthropic":
                _backends[key] = AnthropicBackend(model=model)
            else:
                raise LLMError(f"Unsupported LLM_PROVIDER: {provider}")
    return _backends[key]
