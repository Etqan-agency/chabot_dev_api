"""Writing a source back as one block of text.

``knowledge_assembly`` is the read half: it stitches a document's chunks into
the single text a person actually reads. This is the write half. It takes that
text back, re-chunks it the way ingestion would, re-embeds it, and makes it the
document's knowledge — so the page that shows a source can be an editable page
rather than a list of retrieval fragments.

Two things are load-bearing:

* **Embeddings first.** Every piece is embedded *before* a single old chunk is
  deleted, so a provider outage costs the save, never the knowledge — the bot
  keeps answering from exactly what it had.
* **The swap is atomic.** ``(document, position)`` is unique, so the old rows
  and the new ones can never coexist; the delete and the insert share one
  transaction and the document is never half-rewritten.
"""

from __future__ import annotations

import hashlib
import logging

from django.db import transaction
from django.utils import timezone

from ..models import DocumentChunk, DocumentStatus, SourceType, UploadedDocument, UsageKind
from . import billing
from .chunking import _chunk, _embed_in_batches
from .embeddings import EmbeddingError

logger = logging.getLogger(__name__)

#: A document's whole text, per request. Comfortably above any real document
#: (~150k words) and low enough that one request cannot exhaust memory.
MAX_CONTENT_CHARS = 1_000_000


class ContentError(ValueError):
    """The submitted text cannot be stored as knowledge (empty, oversized)."""


class ContentUnavailable(RuntimeError):
    """The text is fine but it could not be embedded right now."""


class ContentNotEditable(ContentError):
    """This source's text is owned by a sync, so a whole-text rewrite is refused."""


def check_editable(document: UploadedDocument) -> None:
    """Refuse a whole-text rewrite of a source an API sync maintains.

    A sync tracks each record by its ``source_id`` and protects the records a
    human corrected (``items_protected``). Re-chunking the whole document throws
    those ids away, so the next sync would not recognise the records it already
    imported: it would re-embed every one of them *next to* the hand-written
    text instead of replacing it, and the tenant would end up with the same
    facts twice. Per-section edits keep the ids, and the sync keeps protecting
    them, so that is where corrections to an API source belong.
    """
    if document.source_type == SourceType.API:
        raise ContentNotEditable(
            "This source is kept up to date by its API sync. Edit its sections one at a "
            "time (PATCH /knowledge/{id}/) so the next sync can protect the correction."
        )


def replace_content(document: UploadedDocument, text: str) -> int:
    """Make ``text`` the document's knowledge. Returns the number of sections."""
    check_editable(document)

    text = (text or "").strip()
    if not text:
        raise ContentError("Content cannot be empty.")
    if len(text) > MAX_CONTENT_CHARS:
        raise ContentError(f"Content is too large (maximum {MAX_CONTENT_CHARS:,} characters).")

    pieces = _chunk(text)
    if not pieces:
        raise ContentError("Content cannot be empty.")

    try:
        with billing.charge_to(document.uploaded_by, UsageKind.KNOWLEDGE):
            vectors, model = _embed_in_batches(pieces)
    except EmbeddingError as exc:
        raise ContentUnavailable("Could not embed the text right now. The previous content is unchanged.") from exc

    edited_at = timezone.now()
    with transaction.atomic():
        DocumentChunk.objects.filter(document=document).delete()
        DocumentChunk.objects.bulk_create(
            [
                DocumentChunk(
                    document=document,
                    position=index,
                    content=piece,
                    embedding=vector,
                    embedding_model=model,
                    content_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                    metadata={"source": document.filename, "manually_edited": True},
                    edited_at=edited_at,
                )
                for index, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
            ]
        )
        # A document that failed to parse can be fixed by pasting its text in,
        # so a successful write is also what clears the failure.
        document.processing_status = DocumentStatus.COMPLETED
        document.error_message = ""
        document.save(update_fields=["processing_status", "error_message", "updated_at"])

    _mirror_vectors(document)
    return len(pieces)


def _mirror_vectors(document: UploadedDocument) -> None:
    """Fill the pgvector mirror for the new rows (no-op on the JSON backend)."""
    from .vector_store import backfill_vectors_for_document

    try:
        backfill_vectors_for_document(document.id)
    except Exception:  # noqa: BLE001 - the rows are written; the mirror is best-effort
        logger.warning("Could not mirror vectors for document %s", document.id, exc_info=True)
