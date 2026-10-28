"""Hand-editing of what the bot actually learned.

Retrieval matches on a chunk's *embedding*, not on its text. So changing a
chunk's wording without re-embedding it would be worse than useless: the bot
would show the corrected text while still matching — and being retrieved for —
the wording you just removed. Every write path here re-embeds, and every one of
them refreshes the pgvector mirror too (the bulk backfill only fills NULLs, so
an edited row would otherwise keep its stale vector).
"""

from __future__ import annotations

import hashlib
import logging

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ..models import DocumentChunk, UsageKind
from . import billing
from .embeddings import EmbeddingError, embed_one

logger = logging.getLogger(__name__)


class KnowledgeEditError(RuntimeError):
    """Raised when a chunk cannot be written (almost always: embedding failed)."""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sync_vector(chunk_id) -> None:
    from .vector_store import refresh_vector_for_chunk

    try:
        refresh_vector_for_chunk(chunk_id)
    except Exception:  # noqa: BLE001 - the row is written; the mirror is best-effort
        logger.warning("Could not refresh the vector mirror for chunk %s", chunk_id, exc_info=True)


def _embed(text: str, owner) -> tuple[list[float], str]:
    """Embed ``text``, charged to ``owner`` (points)."""
    try:
        with billing.charge_to(owner, UsageKind.KNOWLEDGE):
            return embed_one(text)
    except EmbeddingError as exc:
        raise KnowledgeEditError(f"Could not embed the text: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise KnowledgeEditError("Could not embed the text.") from exc


def next_position(document) -> int:
    current = DocumentChunk.objects.filter(document=document).aggregate(m=Max("position")).get("m")
    return (current + 1) if current is not None else 0


def create_chunk(document, content: str, *, metadata: dict | None = None) -> DocumentChunk:
    """Add a new piece of knowledge to a document, embedded and searchable."""
    content = (content or "").strip()
    if not content:
        raise KnowledgeEditError("Content cannot be empty.")

    vector, model = _embed(content, document.uploaded_by)
    with transaction.atomic():
        chunk = DocumentChunk.objects.create(
            document=document,
            position=next_position(document),
            content=content,
            embedding=vector,
            embedding_model=model,
            content_hash=_hash(content),
            metadata={**(metadata or {}), "manually_added": True},
            edited_at=timezone.now(),
        )
    _sync_vector(chunk.id)
    return chunk


def update_chunk(chunk: DocumentChunk, content: str, *, metadata: dict | None = None) -> DocumentChunk:
    """Correct a chunk's text and re-embed it so retrieval follows the correction."""
    content = (content or "").strip()
    if not content:
        raise KnowledgeEditError("Content cannot be empty.")

    if content == chunk.content and metadata is None:
        return chunk  # nothing to do; don't pay for an embedding

    vector, model = _embed(content, chunk.document.uploaded_by)
    chunk.content = content
    chunk.embedding = vector
    chunk.embedding_model = model
    chunk.content_hash = _hash(content)
    chunk.edited_at = timezone.now()
    if metadata is not None:
        chunk.metadata = {**(chunk.metadata or {}), **metadata}
    chunk.metadata = {**(chunk.metadata or {}), "manually_edited": True}
    chunk.save(
        update_fields=[
            "content",
            "embedding",
            "embedding_model",
            "content_hash",
            "metadata",
            "edited_at",
            "updated_at",
        ]
    )
    _sync_vector(chunk.id)
    return chunk
