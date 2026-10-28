"""Capture knowledge gaps: in-domain questions the bot couldn't answer from data.

The in-domain classification is decided upstream by the answering model itself
(see ``rag.answer_question`` → ``answer_status == "gap"``), because that model is
the one holding the retrieved data and the tenant's identity. So this module does
NOT run a separate filter LLM call — it just:

1. Normalises the question into a stable ``question_key`` for de-duplication.
2. ``get_or_create`` per tenant on ``question_key``; a repeat bumps
   ``occurrences`` and ``last_asked_at`` instead of inserting a duplicate.

Dispatch mode mirrors document ingestion (``UNANSWERED_CAPTURE_MODE``):
``sync`` (default, in-request), ``thread`` (daemon thread), or ``celery``.
Capture must never break a chat reply, so callers wrap it defensively.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading

from django.conf import settings
from django.db.models import F, Max
from django.utils import timezone

from ..models import (
    DocumentChunk,
    DocumentStatus,
    SourceType,
    UnansweredQuestion,
    UnansweredStatus,
    UploadedDocument,
    UsageKind,
)
from .embeddings import embed_one
from .vector_store import backfill_vectors_for_document

logger = logging.getLogger(__name__)

# One shared per-tenant document holds every answer a reviewer resolves, so the
# RAG pipeline retrieves them like any other knowledge (see resolve_to_knowledge).
QA_DOC_FILENAME = "Resolved Questions (Q&A)"

# Keep the normalised key within the model's column width.
_KEY_MAX = 500
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """A stable de-dup key: lowercased, punctuation stripped, whitespace collapsed.

    Deliberately simple (no LLM/embeddings) so it is deterministic and cheap.
    "What are your hours??" and "what are your hours" collapse to one key.
    """
    text = (text or "").strip().lower()
    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text[:_KEY_MAX]


def capture_unanswered(*, user, question: str, language: str = "", reason: str = "") -> UnansweredQuestion | None:
    """Record/increment an in-domain knowledge gap for ``user``.

    The caller has already decided this is a genuine in-domain gap (the answering
    model classified it as "gap"), so there is no filtering here — just normalise
    and de-duplicate. Returns the row (created or bumped), or ``None`` for empty
    input. Safe to call synchronously in tests.
    """
    key = normalize_question(question)
    if not key:
        return None

    obj, created = UnansweredQuestion.objects.get_or_create(
        user=user,
        question_key=key,
        defaults={
            "question": question.strip(),
            "language": language or "",
            "reason": reason,
            "last_asked_at": timezone.now(),
        },
    )
    if not created:
        # Bump atomically; don't overwrite the reviewer's status or edits.
        UnansweredQuestion.objects.filter(pk=obj.pk).update(
            occurrences=F("occurrences") + 1,
            last_asked_at=timezone.now(),
        )
        obj.refresh_from_db(fields=["occurrences", "last_asked_at"])
    return obj


def _run_in_thread(user_id, question, language, reason) -> None:
    from django.contrib.auth import get_user_model
    from django.db import connection

    try:
        user = get_user_model().objects.get(pk=user_id)
        capture_unanswered(user=user, question=question, language=language, reason=reason)
    except Exception:
        logger.exception("Background unanswered capture failed for user %s", user_id)
    finally:
        connection.close()


def dispatch_capture(*, user, question: str, language: str = "", reason: str = "") -> None:
    """Record a knowledge gap per ``UNANSWERED_CAPTURE_MODE``; never raises.

    ``sync`` (default) records in-request; ``thread``/``celery`` push it off the
    request path. The capture itself is cheap now (no LLM call).
    """
    mode = getattr(settings, "UNANSWERED_CAPTURE_MODE", "sync").lower()
    try:
        if mode == "celery":
            from ..tasks import capture_unanswered_task

            capture_unanswered_task.delay(str(user.pk), question, language, reason)
            return
        if mode == "thread":
            threading.Thread(
                target=_run_in_thread,
                args=(user.pk, question, language, reason),
                daemon=True,
            ).start()
            return
        capture_unanswered(user=user, question=question, language=language, reason=reason)
    except Exception:
        # Capturing a gap must never break the chat reply.
        logger.exception("dispatch_capture failed for user %s", getattr(user, "pk", None))


def _qa_document(user) -> UploadedDocument:
    """The tenant's single 'Resolved Q&A' knowledge document (created on demand)."""
    doc, _ = UploadedDocument.objects.get_or_create(
        uploaded_by=user,
        filename=QA_DOC_FILENAME,
        defaults={
            "source_type": SourceType.FILE,
            "processing_status": DocumentStatus.COMPLETED,
            "is_active": True,
        },
    )
    # Keep it active/completed so retrieval always includes it.
    if not doc.is_active or doc.processing_status != DocumentStatus.COMPLETED:
        doc.is_active = True
        doc.processing_status = DocumentStatus.COMPLETED
        doc.save(update_fields=["is_active", "processing_status", "updated_at"])
    return doc


def resolve_to_knowledge(*, unanswered: UnansweredQuestion, answer: str, user) -> DocumentChunk:
    """Turn a reviewer's answer into retrievable knowledge and mark the gap answered.

    Writes a ``Q: ... A: ...`` chunk into the tenant's Resolved-Q&A document and
    embeds it with the same pipeline as uploads, so the next time the question is
    asked the RAG search finds it. Re-resolving the same gap updates its chunk in
    place (keyed by the gap id) instead of duplicating.

    Raises ``EmbeddingError`` if the answer can't be embedded (caller decides how
    to surface it); in that case nothing is marked answered.
    """
    answer = (answer or "").strip()
    if not answer:
        raise ValueError("An answer is required to resolve into knowledge.")

    content = f"Q: {unanswered.question}\nA: {answer}"
    from . import billing

    with billing.charge_to(user, UsageKind.KNOWLEDGE):
        vector, model = embed_one(content)  # may raise EmbeddingError

    doc = _qa_document(user)
    key = f"unanswered:{unanswered.pk}"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    existing = doc.chunks.filter(source_id=key).first()
    if existing is not None:
        existing.content = content
        existing.content_hash = content_hash
        existing.embedding = vector
        existing.embedding_model = model
        existing.save(update_fields=["content", "content_hash", "embedding", "embedding_model", "updated_at"])
        chunk = existing
    else:
        next_pos = (doc.chunks.aggregate(m=Max("position"))["m"] or -1) + 1
        chunk = DocumentChunk.objects.create(
            document=doc,
            position=next_pos,
            content=content,
            content_hash=content_hash,
            source_id=key,
            embedding=vector,
            embedding_model=model,
            metadata={"origin": "resolved_unanswered", "unanswered_id": str(unanswered.pk)},
        )

    # Make it immediately searchable under the pgvector backend (no-op on numpy).
    backfill_vectors_for_document(doc.pk)

    unanswered.status = UnansweredStatus.ANSWERED
    unanswered.save(update_fields=["status", "updated_at"])
    return chunk
