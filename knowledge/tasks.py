"""Celery tasks for the knowledge app."""

from __future__ import annotations

import logging

from celery import shared_task

from .models import UploadedDocument

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def ingest_document_task(self, document_id):
    """Parse, chunk and embed a document in the background.

    Status transitions (pending -> processing -> completed/failed) are persisted
    by ``ingest_document`` itself, so the API can poll the document for progress.
    """
    # Import here to avoid loading the embedding stack at worker import time.
    from .services.chunking import ingest_document

    try:
        document = UploadedDocument.objects.get(pk=document_id)
    except UploadedDocument.DoesNotExist:
        logger.error("ingest_document_task: document %s no longer exists", document_id)
        return None

    result = ingest_document(document)
    return {"document_id": str(document_id), "chunks_created": getattr(result, "chunks_created", 0)}


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def capture_unanswered_task(self, user_id, question, language="", reason=""):
    """Record an in-domain, unanswered chat question as a knowledge gap."""
    from django.contrib.auth import get_user_model

    from .services.unanswered import capture_unanswered

    try:
        user = get_user_model().objects.get(pk=user_id)
    except get_user_model().DoesNotExist:
        logger.error("capture_unanswered_task: user %s no longer exists", user_id)
        return None

    obj = capture_unanswered(user=user, question=question, language=language, reason=reason)
    return {"captured": bool(obj), "id": str(obj.pk) if obj else None}


@shared_task(bind=True, max_retries=0)
def sync_api_content_task(self, run_id, user_id, params):
    """Fetch, analyse and embed an external API's content on a worker.

    Deliberately never retries. A sync writes to the tenant's live knowledge
    base, so a second automatic attempt on top of a half-applied first one is
    more dangerous than a failure the tenant can see and re-trigger. Every
    outcome -- success, refusal, error -- is already on the ``APIIngestionRun``
    row that ``run_id`` names, which is what the tenant polls.
    """
    from django.contrib.auth import get_user_model

    from .services.api_ingest_run import IngestionRunRecorder
    from .services.api_sync import SyncParams, run_api_sync

    user = None
    if user_id:
        try:
            user = get_user_model().objects.get(pk=user_id)
        except get_user_model().DoesNotExist:
            logger.error("sync_api_content_task: user %s no longer exists", user_id)
            return None

    recorder = IngestionRunRecorder.resume(run_id)
    try:
        result = run_api_sync(SyncParams(**params), recorder=recorder, user=user)
    except Exception as exc:
        # run_api_sync has already recorded the outcome on the run row; the
        # tenant reads it from there. Re-raising would only mark the Celery
        # result failed, which nothing reads.
        logger.warning("API sync run %s did not complete: %s", run_id, exc)
        return {"run_id": str(run_id), "status": "failed"}

    return {"run_id": str(run_id), "status": "success", "chunks_created": result.stats.get("chunks_created", 0)}
