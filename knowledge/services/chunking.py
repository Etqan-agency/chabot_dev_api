"""Document parsing, chunking, and embedding pipeline.

Supports .docx, .txt, and .md out of the box. Chunks are persisted to
``DocumentChunk`` so retrieval is index-based rather than rebuilt in
memory on every request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction

from ..models import DocumentChunk, DocumentStatus, UploadedDocument, UsageKind
from . import billing
from .embeddings import EmbeddingError, embed_texts

logger = logging.getLogger(__name__)


CHUNK_SIZE = 1200
CHUNK_OVERLAP = 200
EMBED_BATCH = 64


@dataclass
class IngestionResult:
    document_id: str
    chunks_created: int


def ingest_document(document: UploadedDocument) -> IngestionResult:
    """Parse, chunk, embed, and persist a document.

    The embedding calls are charged to the document's owner (points). On
    failure marks the document as ``FAILED`` and re-raises.
    """
    with billing.charge_to(document.uploaded_by, UsageKind.DOCUMENT):
        return _ingest_document(document)


def _ingest_document(document: UploadedDocument) -> IngestionResult:
    document.processing_status = DocumentStatus.PROCESSING
    document.error_message = ""
    document.save(update_fields=["processing_status", "error_message", "updated_at"])

    try:
        text = _read_document(document)
        if not text.strip():
            raise ValueError("Document is empty.")

        pieces = _chunk(text)
        embeddings, model = _embed_in_batches(pieces)

        with transaction.atomic():
            DocumentChunk.objects.filter(document=document).delete()
            DocumentChunk.objects.bulk_create(
                [
                    DocumentChunk(
                        document=document,
                        position=idx,
                        content=piece,
                        embedding=vector,
                        embedding_model=model,
                        metadata={"source": document.filename},
                    )
                    for idx, (piece, vector) in enumerate(zip(pieces, embeddings, strict=True))
                ]
            )

        document.processing_status = DocumentStatus.COMPLETED
        document.save(update_fields=["processing_status", "updated_at"])

        # Mirror the JSON embeddings into the pgvector column so the new chunks
        # are searchable immediately (no-op unless RAG_VECTOR_BACKEND=pgvector).
        from .vector_store import backfill_vectors_for_document

        backfill_vectors_for_document(document.id)

        return IngestionResult(document_id=str(document.id), chunks_created=len(pieces))

    except Exception as exc:
        logger.exception("Failed to ingest document %s", document.id)
        document.processing_status = DocumentStatus.FAILED
        document.error_message = str(exc)[:1000]
        document.save(update_fields=["processing_status", "error_message", "updated_at"])
        raise


#: Extensions the parser below can actually read. Upload validation imports
#: this, so the two can never disagree about what is ingestible.
SUPPORTED_SUFFIXES = {".docx", ".txt", ".md"}


def _read_document(document: UploadedDocument) -> str:
    path = document.file.path
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        try:
            import docx2txt  # type: ignore
        except ImportError as exc:
            raise RuntimeError("docx2txt not installed; cannot parse .docx") from exc
        return docx2txt.process(path) or ""
    if suffix in {".txt", ".md"}:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    raise ValueError(f"Unsupported file type: {suffix}")


def _chunk(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph/sentence boundaries."""
    text = text.replace("\r\n", "\n")
    if len(text) <= size:
        return [text.strip()] if text.strip() else []

    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Walk back to a clean break (paragraph > sentence > whitespace)
            for sep in ("\n\n", ". ", "،", "،\n", " "):
                idx = text.rfind(sep, start + size // 2, end)
                if idx != -1:
                    end = idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            out.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def _embed_in_batches(texts: list[str]) -> tuple[list[list[float]], str]:
    if not texts:
        return [], ""
    all_vectors: list[list[float]] = []
    model = ""
    for i in range(0, len(texts), EMBED_BATCH):
        if not billing.scope_has_points():
            # The owner ran out of points part-way; nothing is half-written.
            raise EmbeddingError("Out of points. Contact the administrator to renew your plan.")
        batch = texts[i : i + EMBED_BATCH]
        try:
            vectors, model = embed_texts(batch)
        except EmbeddingError:
            raise
        all_vectors.extend(vectors)
    return all_vectors, model
