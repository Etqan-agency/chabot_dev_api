"""Shared test helpers for building tenants and their data without hitting any
external service (no OpenAI). Embeddings are tiny fixed vectors."""

from __future__ import annotations

from django.contrib.auth import get_user_model

from Authentication.models import APIKey
from knowledge.models import DocumentChunk, UploadedDocument
from knowledge.services.llm import LLMResult
from knowledge.services.retrieval import ChunkHit

User = get_user_model()


class FakeLLM:
    """Stand-in LLM backend so tests never call OpenAI."""

    model = "gpt-4o"

    def __init__(self, text="fake answer", prompt_tokens=100, completion_tokens=20):
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    def complete(self, system, user, *, temperature=0):
        return self.complete_with_usage(system, user, temperature=temperature).text

    def complete_with_usage(self, system, user, *, temperature=0):
        self.calls += 1
        return LLMResult(
            text=self.text,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )


def make_hit(content="hello", *, score=0.9, position=0, document_id="doc", filename="d.txt"):
    return ChunkHit(
        chunk_id="c1",
        document_id=document_id,
        filename=filename,
        content=content,
        position=position,
        score=score,
    )


def make_tenant(username: str, *, is_staff: bool = False):
    """Create a user (tenant) + an API key. Returns (user, api_key_str)."""
    user = User.objects.create_user(
        username=username,
        password="Test!Pass2026",
        is_staff=is_staff,
        is_superuser=is_staff,
    )
    api_key = APIKey.objects.create(user=user)
    return user, api_key.key


def make_document(user, *, filename="doc.txt", size=1000, is_active=True):
    return UploadedDocument.objects.create(
        uploaded_by=user,
        filename=filename,
        file_size=size,
        is_active=is_active,
        processing_status="completed",
    )


def make_chunk(document, *, position=0, content="hello", embedding=None):
    return DocumentChunk.objects.create(
        document=document,
        position=position,
        content=content,
        embedding=embedding if embedding is not None else [1.0, 0.0, 0.0, 0.0],
        embedding_model="test",
    )
