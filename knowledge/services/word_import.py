"""Ingest a Word (.docx) file into the RAG document store.

Thin wrapper around the standard document pipeline: it creates an
``UploadedDocument`` from an uploaded Word file and runs the same
parse → chunk → embed flow used by the generic documents endpoint, but
behind an explicit, Word-only entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import DocumentStatus, SourceType, UploadedDocument
from .ingestion import dispatch_ingestion
from .uploads import validate_upload_size

# docx2txt (used by the chunking pipeline) only reads the modern .docx format.
SUPPORTED_SUFFIXES = {".docx"}


@dataclass
class WordImportResult:
    document: UploadedDocument
    chunks_created: int


def import_document_from_word(uploaded_file, *, uploaded_by=None) -> WordImportResult:
    """Create and ingest an ``UploadedDocument`` from a Word file.

    Raises ``ValueError`` for an unsupported file type or empty content.
    On a parsing/embedding failure the document is persisted with status
    ``FAILED`` (by ``ingest_document``) and the underlying error is re-raised.
    """
    suffix = Path(uploaded_file.name or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported file type '{suffix or '(none)'}'. Upload a Word .docx file.")
    validate_upload_size(uploaded_file)

    document = UploadedDocument.objects.create(
        file=uploaded_file,
        filename=uploaded_file.name,
        file_size=getattr(uploaded_file, "size", 0) or 0,
        source_type=SourceType.FILE,
        processing_status=DocumentStatus.PENDING,
        uploaded_by=uploaded_by,
    )

    result = dispatch_ingestion(document)
    document.refresh_from_db()
    # result is None when ingestion was handed off to the background.
    chunks_created = result.chunks_created if result is not None else 0
    return WordImportResult(document=document, chunks_created=chunks_created)
