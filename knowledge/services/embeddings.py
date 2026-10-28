"""Provider-agnostic text embedding service.

Currently supports OpenAI. Adding another provider means implementing the
`EmbeddingBackend` protocol below and selecting it via `EMBEDDING_PROVIDER`.

The dependency on the OpenAI SDK is imported lazily so the rest of the API
remains importable without it (e.g. in tests, in environments where the
chat features are disabled).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from typing import Protocol


class EmbeddingError(RuntimeError):
    pass


class EmbeddingBackend(Protocol):
    model: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingBackend:
    def __init__(self, model: str | None = None):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EmbeddingError("OPENAI_API_KEY is not configured.")
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise EmbeddingError("openai package is not installed.") from exc

        self._client = OpenAI(api_key=api_key)
        self.model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
        # Approximate dim — only used for sanity checks; real value is in the response.
        self.dim = 3072 if "large" in self.model else 1536

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(model=self.model, input=texts)
        # Charge the call's real cost to the tenant whose work it is (see billing).
        from .billing import meter

        meter(self.model, getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0)
        return [item.embedding for item in response.data]


_lock = threading.Lock()
_backend: EmbeddingBackend | None = None


def get_backend() -> EmbeddingBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is None:
            provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
            if provider == "openai":
                _backend = OpenAIEmbeddingBackend()
            else:
                raise EmbeddingError(f"Unsupported EMBEDDING_PROVIDER: {provider}")
    return _backend


def embed_texts(texts: Iterable[str]) -> tuple[list[list[float]], str]:
    """Return ``(vectors, model_name)`` for the given texts."""
    backend = get_backend()
    cleaned = [t for t in (s.strip() for s in texts) if t]
    if not cleaned:
        return [], backend.model
    return backend.embed(cleaned), backend.model


def embed_one(text: str) -> tuple[list[float], str]:
    vectors, model = embed_texts([text])
    if not vectors:
        raise EmbeddingError("Cannot embed empty text.")
    return vectors[0], model
